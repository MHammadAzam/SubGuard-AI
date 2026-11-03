"""Unit tests for OpenRouterExtractionProvider using mocked OpenAI-compatible SDK responses.

Ensures zero live API calls occur during automated testing while verifying:
1. Successful JSON extraction and schema parsing.
2. Missing API key raises ExtractionConfigurationError.
3. Invalid API key / authentication failure (401) raises ExtractionConfigurationError.
4. HTTP 429 rate limit triggers retries and raises ExtractionRateLimitError.
5. Provider unavailable (500/502/503) triggers retries and raises ExtractionProviderError.
6. Network timeout triggers retries and raises ExtractionTimeoutError.
7. Malformed JSON raises ExtractionSchemaValidationError.
8. Invalid Pydantic response raises ExtractionSchemaValidationError.
9. Missing evidence handled safely without fabrication.
10. Preservation of uncertainty states (UNCERTAIN and QUOTE_PRESENT_UNVERIFIED_SUPPORT).
11. Strict cost protection: no automatic fallback to paid models or paid providers.
12. Existing Gemini and mock extraction providers remain fully operational.
"""

from __future__ import annotations

import json
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from app.extraction.pipeline import ExtractionPipelineService
from app.extraction.providers.base import (
    ExtractionConfigurationError,
    ExtractionProviderError,
    ExtractionRateLimitError,
    ExtractionSchemaValidationError,
    ExtractionTimeoutError,
)
from app.extraction.providers.gemini import GeminiExtractionProvider
from app.extraction.providers.mock import MockExtractionProvider
from app.extraction.providers.openrouter import (
    OPENROUTER_SYSTEM_PROMPT,
    OpenRouterExtractionProvider,
)
from app.schemas.insurance import CoverageType, DocumentType, ExtractionStatus
from app.schemas.pdf import PDFDocumentInspection, PDFPageInspection, PDFTextSpan
from app.schemas.raw_extraction import (
    RawCoverageSection,
    RawEntityExtraction,
    RawEvidenceQuote,
    RawInsuranceExtraction,
)
from evaluation.datasets.loader import build_default_synthetic_dataset
from evaluation.metrics.evaluator import MetricsEvaluator


def _make_dummy_page(
    page_num: int = 1,
    text: str = "Test text",
    image_bytes: bytes | None = None,
) -> PDFPageInspection:
    return PDFPageInspection(
        page_number=page_num,
        width_pts=612.0,
        height_pts=792.0,
        text_content=text,
        rendered_image_png=image_bytes,
        spans=[
            PDFTextSpan(
                text=text,
                page_number=page_num,
                bounding_box={"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.1},
            )
        ],
    )


def _make_mock_chat_completion(content_str: str) -> MagicMock:
    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = content_str
    mock_resp.choices = [mock_choice]
    return mock_resp


class TestOpenRouterExtractionProvider(unittest.TestCase):
    """Test suite for OpenRouter extraction provider with mocked OpenAI SDK."""

    def test_1_successful_json_extraction(self):
        """1. Provider must construct messages, request json_object, and return validated RawInsuranceExtraction."""
        mock_client = MagicMock()
        raw_model_json = RawInsuranceExtraction(
            document_type="coi",
            named_insured=RawEntityExtraction(
                name="Apex Electrical Contractors LLC",
                evidence=RawEvidenceQuote(page_number=1, source_text="Apex Electrical Contractors LLC"),
            ),
        ).model_dump_json()

        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(raw_model_json)

        provider = OpenRouterExtractionProvider(
            api_key="fake-openrouter-key",
            model_name="nvidia/nemotron-3.5-super-vl:free",
            client=mock_client,
        )

        page = _make_dummy_page(page_num=1, text="INSURED: Apex Electrical Contractors LLC")
        result = provider.extract_insurance_data(document_id="DOC-OR-01", pages=[page])

        mock_client.chat.completions.create.assert_called_once()
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs

        self.assertEqual(call_kwargs["model"], "nvidia/nemotron-3.5-super-vl:free")
        self.assertEqual(call_kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(call_kwargs["temperature"], 0.0)

        messages = call_kwargs["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], OPENROUTER_SYSTEM_PROMPT)

        user_content = messages[1]["content"]
        self.assertIn("=== PAGE 1", user_content[0]["text"])
        self.assertIn("Apex Electrical Contractors LLC", user_content[0]["text"])

        # Verify parsed schema
        self.assertEqual(result.named_insured.name, "Apex Electrical Contractors LLC")
        self.assertEqual(result.named_insured.evidence.page_number, 1)

    def test_2_missing_api_key_raises_configuration_error(self):
        """2. When OPENROUTER_API_KEY is not in env and not in constructor, raise ExtractionConfigurationError."""
        with patch.dict("os.environ", {}, clear=True):
            provider = OpenRouterExtractionProvider(api_key=None)
            page = _make_dummy_page(1, "Text")
            with self.assertRaises(ExtractionConfigurationError) as ctx:
                provider.extract_insurance_data("DOC-NO-KEY", [page])
            self.assertIn("OpenRouter API key is not configured", str(ctx.exception))
            self.assertEqual(ctx.exception.provider_name, "openrouter")

    def test_3_invalid_authentication_raises_configuration_error(self):
        """3. HTTP 401 Unauthorized from OpenRouter must raise ExtractionConfigurationError."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("Error code: 401 - {'error': {'message': 'Invalid API Key'}}")

        provider = OpenRouterExtractionProvider(
            api_key="bad-key",
            client=mock_client,
            max_retries=1,
        )
        page = _make_dummy_page(1, "Text")

        with self.assertRaises(ExtractionConfigurationError) as ctx:
            provider.extract_insurance_data("DOC-401", [page])
        self.assertIn("authentication failed", str(ctx.exception).lower())

    def test_4_rate_limiting_triggers_retries_and_raises_rate_limit_error(self):
        """4. HTTP 429 Rate Limit from OpenRouter must retry and raise ExtractionRateLimitError."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("Error code: 429 - {'error': {'message': 'Rate limit exceeded'}}")

        provider = OpenRouterExtractionProvider(
            api_key="test-key",
            client=mock_client,
            max_retries=2,
        )
        page = _make_dummy_page(1, "Text")

        with self.assertRaises(ExtractionRateLimitError) as ctx:
            provider.extract_insurance_data("DOC-429", [page])
        self.assertIn("rate limit exceeded", str(ctx.exception).lower())
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)

    def test_5_provider_unavailable_raises_provider_error(self):
        """5. Provider unavailable (500/502/503/service unavailable) triggers bounded retries then raises ExtractionProviderError."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("Error code: 503 - {'error': {'message': 'Service Unavailable'}}")

        provider = OpenRouterExtractionProvider(
            api_key="test-key",
            client=mock_client,
            max_retries=2,
        )
        page = _make_dummy_page(1, "Text")

        with self.assertRaises(ExtractionProviderError) as ctx:
            provider.extract_insurance_data("DOC-503", [page])
        self.assertIn("OpenRouter extraction failed after 2 attempts", str(ctx.exception))
        self.assertFalse(ctx.exception.is_retryable)
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)

    def test_6_timeout_triggers_retries_and_raises_timeout_error(self):
        """6. Timeout from OpenRouter call must raise ExtractionTimeoutError."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("Request timed out after 30s")

        provider = OpenRouterExtractionProvider(
            api_key="test-key",
            client=mock_client,
            max_retries=2,
        )
        page = _make_dummy_page(1, "Text")

        with self.assertRaises(ExtractionTimeoutError) as ctx:
            provider.extract_insurance_data("DOC-TIMEOUT", [page])
        self.assertIn("timed out", str(ctx.exception).lower())
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)

    def test_7_malformed_json_raises_schema_validation_error(self):
        """7. Broken or non-JSON model output must raise ExtractionSchemaValidationError without crashing."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            "Here is the extraction: {document_type: 'coi', missing_bracket"
        )

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Text")

        with self.assertRaises(ExtractionSchemaValidationError) as ctx:
            provider.extract_insurance_data("DOC-MALFORMED", [page])
        self.assertIn("Failed to parse OpenRouter response as JSON", str(ctx.exception))

    def test_8_pydantic_schema_validation_failure_raises_schema_validation_error(self):
        """8. Valid JSON with invalid schema structure must raise ExtractionSchemaValidationError."""
        mock_client = MagicMock()
        # Invalid: named_insured must be an object, not a list
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps({"document_type": "coi", "named_insured": ["invalid", "list"]})
        )

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Text")

        with self.assertRaises(ExtractionSchemaValidationError) as ctx:
            provider.extract_insurance_data("DOC-INVALID-SCHEMA", [page])
        self.assertIn("failed RawInsuranceExtraction schema validation", str(ctx.exception))

    def test_9_missing_evidence_handled_safely(self):
        """9. When extracted fields lack evidence, pipeline handles safely without fabricating citations."""
        mock_client = MagicMock()
        # Return entity with name but null evidence
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps({
                "document_type": "coi",
                "named_insured": {
                    "name": "Apex Electrical Contractors LLC",
                    "evidence": None,
                },
                "coverages": [],
            })
        )

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Apex Electrical Contractors LLC is insured here.")
        raw_res = provider.extract_insurance_data("DOC-NO-EV", [page])

        self.assertIsNotNone(raw_res.named_insured)
        self.assertIsNone(raw_res.named_insured.evidence)

        # In pipeline, absent evidence routes extraction status to UNCERTAIN without inventing quotes
        pipeline = ExtractionPipelineService(extraction_provider=provider)
        inspection = PDFDocumentInspection(
            document_id="DOC-NO-EV",
            filename="doc.pdf",
            file_size_bytes=1000,
            content_hash_sha256="abc",
            page_count=1,
            pages=[page],
            classified_type=DocumentType.COI,
            classification_confidence=1.0,
            classification_reason="test",
        )
        result = pipeline.process_inspection(inspection, "SUB-01")
        self.assertIsNotNone(result.document_set)
        insured_info = result.document_set.named_insured
        self.assertEqual(insured_info.extraction_status, ExtractionStatus.UNCERTAIN)
        self.assertIsNone(insured_info.evidence)

    def test_10_preservation_of_uncertainty_states(self):
        """10. Preserves UNCERTAIN and QUOTE_PRESENT_UNVERIFIED_SUPPORT states accurately."""
        # Case A: Quote does not exist on page -> UNCERTAIN
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps({
                "document_type": "coi",
                "named_insured": {
                    "name": "Apex Electrical Contractors LLC",
                    "evidence": {
                        "page_number": 1,
                        "source_text": "Non-existent quote text that does not appear anywhere",
                    },
                },
            })
        )
        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        pipeline = ExtractionPipelineService(extraction_provider=provider)
        page = _make_dummy_page(1, "Different text on page")
        inspection = PDFDocumentInspection(
            document_id="DOC-UNC-1",
            filename="doc.pdf",
            file_size_bytes=1000,
            content_hash_sha256="abc",
            page_count=1,
            pages=[page],
            classified_type=DocumentType.COI,
            classification_confidence=1.0,
            classification_reason="test",
        )
        result_a = pipeline.process_inspection(inspection, "SUB-01")
        self.assertEqual(result_a.document_set.named_insured.extraction_status, ExtractionStatus.UNCERTAIN)

        # Case B: Quote exists on page, but does not provide semantic support -> QUOTE_PRESENT_UNVERIFIED_SUPPORT
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps({
                "document_type": "coi",
                "named_insured": {
                    "name": "Apex Electrical Contractors LLC",
                    "evidence": {
                        "page_number": 1,
                        "source_text": "ACORD 25 (2016/03) CERTIFICATE OF LIABILITY INSURANCE",
                    },
                },
            })
        )
        page_b = _make_dummy_page(1, "ACORD 25 (2016/03) CERTIFICATE OF LIABILITY INSURANCE")
        inspection_b = PDFDocumentInspection(
            document_id="DOC-UNC-2",
            filename="doc.pdf",
            file_size_bytes=1000,
            content_hash_sha256="abc2",
            page_count=1,
            pages=[page_b],
            classified_type=DocumentType.COI,
            classification_confidence=1.0,
            classification_reason="test",
        )
        result_b = pipeline.process_inspection(inspection_b, "SUB-01")
        self.assertEqual(
            result_b.document_set.named_insured.extraction_status,
            ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT,
        )

    def test_11_no_paid_model_fallback(self):
        """11. Strict cost protection: never automatically falls back to paid models or paid providers upon failure."""
        mock_client = MagicMock()
        # Simulate free model outage / failure
        mock_client.chat.completions.create.side_effect = Exception("503 Free model unavailable")

        target_model = "nvidia/nemotron-3.5-super-vl:free"
        provider = OpenRouterExtractionProvider(
            api_key="test-key",
            model_name=target_model,
            client=mock_client,
            max_retries=2,
        )
        page = _make_dummy_page(1, "Some text")

        with self.assertRaises(ExtractionProviderError):
            provider.extract_insurance_data("DOC-FAIL", [page])

        # Assert provider model name is NOT switched to a paid model (e.g. gpt-4o, gemini, etc.)
        self.assertEqual(provider.model_name, target_model)
        # Assert all retry attempts used strictly the selected model
        for call in mock_client.chat.completions.create.call_args_list:
            self.assertEqual(call.kwargs["model"], target_model)

    def test_12_existing_gemini_and_mock_providers_still_work(self):
        """12. Gemini and Mock providers remain completely functional and unaffected."""
        # 1. Mock provider
        mock_prov = MockExtractionProvider()
        self.assertEqual(mock_prov.provider_name, "mock")
        page = _make_dummy_page(1, "INSURED: Acme Builders Inc")
        mock_res = mock_prov.extract_insurance_data("DOC-MOCK", [page])
        self.assertIsInstance(mock_res, RawInsuranceExtraction)

        # 2. Gemini provider initialization
        gemini_prov = GeminiExtractionProvider(api_key="fake-gemini-key", model_name="gemini-2.0-flash")
        self.assertEqual(gemini_prov.provider_name, "gemini")
        self.assertEqual(gemini_prov.model_name, "gemini-2.0-flash")

    def test_image_and_text_payload_construction(self):
        """When rasterized image bytes are present, provider must attach base64 image_url part."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            '{"document_type": "coi"}'
        )

        fake_png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRtestpngbytes"
        page = _make_dummy_page(page_num=1, text="Text on page", image_bytes=fake_png_bytes)

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        provider.extract_insurance_data("DOC-IMG", [page])

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        user_content = call_kwargs["messages"][1]["content"]

        types = [part["type"] for part in user_content]
        self.assertIn("text", types)
        self.assertIn("image_url", types)

        img_part = next(part for part in user_content if part["type"] == "image_url")
        self.assertTrue(img_part["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_provider_errors_excluded_from_extraction_metrics(self):
        """OpenRouter failures through pipeline must yield configuration_error/provider_error with 0 false negatives."""
        with patch.dict("os.environ", {}, clear=True):
            provider = OpenRouterExtractionProvider(api_key=None)
            pipeline = ExtractionPipelineService(extraction_provider=provider)

            inspection = PDFDocumentInspection(
                document_id="SYNTH-001",
                filename="synthetic_standard_acord25.pdf",
                file_size_bytes=1024,
                content_hash_sha256="dummyhash",
                page_count=1,
                pages=[_make_dummy_page(1, "Test")],
                classified_type=DocumentType.COI,
                classification_confidence=1.0,
                classification_reason="test",
            )

            result = pipeline.process_inspection(inspection, subcontractor_id="SUB-01")
            self.assertIsNone(result.document_set)
            self.assertEqual(result.error_category, "configuration_error")

            dataset = build_default_synthetic_dataset()
            doc_gt = dataset.documents[0]

            evaluator = MetricsEvaluator()
            doc_eval = evaluator.evaluate_document(
                ground_truth=doc_gt,
                extracted_doc_set=result.document_set,
                pdf_inspection=inspection,
                processing_status=result.error_category,
            )
            self.assertEqual(doc_eval.processing_status, "configuration_error")
            self.assertEqual(doc_eval.field_results[0].error_type, "configuration_error")

            field_metrics, _ = evaluator.calculate_aggregate_metrics([doc_eval])
            all_stats = field_metrics["all"]
            self.assertEqual(all_stats.false_negatives, 0)
            self.assertEqual(all_stats.total_evaluated, 0)

    def test_d1_provider_exception_survives_pipeline_handling(self):
        """D1. Original provider exception type, status code, error code, and message survive pipeline handling."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception(
            "Error code: 503 - {'error': {'message': 'Model nvidia/nemotron-3.5-super-vl:free is currently offline', 'code': 'model_offline'}}"
        )

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client, max_retries=1)
        pipeline = ExtractionPipelineService(extraction_provider=provider)

        inspection = PDFDocumentInspection(
            document_id="DOC-SURVIVE",
            filename="test.pdf",
            file_size_bytes=1000,
            content_hash_sha256="hash",
            page_count=1,
            pages=[_make_dummy_page(1, "Text")],
            classified_type=DocumentType.COI,
            classification_confidence=1.0,
            classification_reason="test",
        )

        res = pipeline.process_inspection(inspection, "SUB-01")
        self.assertIsNone(res.document_set)
        self.assertEqual(res.error_type, "ExtractionProviderError")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.error_code, "model_offline")
        self.assertIn("Model nvidia/nemotron-3.5-super-vl:free is currently offline", res.error_message)

    def test_d2_benchmark_json_contains_sanitized_diagnostic(self):
        """D2. Benchmark JSON output preserves error_type, error_code, and error_message in document_results."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("404 Model Not Found: nvidia/nemotron-3.5-super-vl:free")

        from evaluation.runners.benchmark import BenchmarkRunner
        runner = BenchmarkRunner(provider_name="openrouter", model_name="nvidia/nemotron-3.5-super-vl:free", openrouter_api_key="test-key")
        runner.provider._injected_client = mock_client
        runner.provider.max_retries = 1
        runner.pipeline_service.extraction_provider = runner.provider

        dataset = build_default_synthetic_dataset()
        report = runner.run_benchmark(dataset, max_documents=1)

        json_str = report.model_dump_json()
        self.assertIn('"error_type":"ExtractionProviderError"', json_str)
        self.assertIn('"error_code":"http_404"', json_str)
        self.assertIn("404 Model Not Found", json_str)
        self.assertTrue(len(report.error_summaries) > 0)
        self.assertIn("404 Model Not Found", report.error_summaries[0])

    def test_d3_console_report_displays_error_summary(self):
        """D3. Console report formatted via format_summary_table displays diagnostic error section."""
        from evaluation.runners.benchmark import BenchmarkRunner, format_summary_table
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("429 Too Many Requests: Rate limit exceeded")

        runner = BenchmarkRunner(provider_name="openrouter", model_name="nvidia/nemotron-3.5-super-vl:free", openrouter_api_key="test-key")
        runner.provider._injected_client = mock_client
        runner.provider.max_retries = 1
        runner.pipeline_service.extraction_provider = runner.provider

        dataset = build_default_synthetic_dataset()
        report = runner.run_benchmark(dataset, max_documents=1)
        console_table = format_summary_table(report)

        self.assertIn("DIAGNOSTIC ERROR DETAILS (1 failure):", console_table)
        self.assertIn("Type: ExtractionRateLimitError", console_table)
        self.assertIn("Code: rate_limit_exceeded", console_table)
        self.assertIn("Rate limit exceeded", console_table)

    def test_d4_api_keys_and_authorization_headers_never_appear_in_reports(self):
        """D4. Real API keys, bearer tokens, and auth headers are strictly redacted from all error messages and reports."""
        leaky_error = (
            "OpenRouter error with key sk-or-v1-abcdef1234567890abcdef1234567890 and "
            "Authorization: Bearer secret-auth-token-123456789 and api_key=supersecret123"
        )
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception(leaky_error)

        from evaluation.runners.benchmark import BenchmarkRunner, format_summary_table
        runner = BenchmarkRunner(provider_name="openrouter", openrouter_api_key="test-key")
        runner.provider._injected_client = mock_client
        runner.provider.max_retries = 1
        runner.pipeline_service.extraction_provider = runner.provider

        dataset = build_default_synthetic_dataset()
        report = runner.run_benchmark(dataset, max_documents=1)
        json_output = report.model_dump_json()
        table_output = format_summary_table(report)

        # Ensure sensitive tokens are completely absent
        self.assertNotIn("sk-or-v1-abcdef1234567890abcdef1234567890", json_output)
        self.assertNotIn("secret-auth-token-123456789", json_output)
        self.assertNotIn("supersecret123", json_output)
        self.assertNotIn("sk-or-v1-abcdef1234567890abcdef1234567890", table_output)
        self.assertNotIn("secret-auth-token-123456789", table_output)
        self.assertNotIn("supersecret123", table_output)

        # Ensure redaction markers are present
        self.assertIn("[REDACTED_API_KEY]", json_output)
        self.assertIn("[REDACTED_TOKEN]", json_output)

    def test_d5_existing_mock_and_gemini_behavior_remains_unchanged(self):
        """D5. Existing mock benchmark executes normally with 0 error summaries and correct evaluations."""
        from evaluation.runners.benchmark import BenchmarkRunner, format_summary_table
        runner = BenchmarkRunner(provider_name="mock")
        dataset = build_default_synthetic_dataset()
        report = runner.run_benchmark(dataset, max_documents=1)

        self.assertEqual(report.evaluated_documents_count, 1)
        self.assertEqual(report.configuration_errors_count, 0)
        self.assertEqual(report.provider_errors_count, 0)
        self.assertEqual(len(report.error_summaries), 0)

        table_str = format_summary_table(report)
        self.assertIn("MOCK PROVIDER", table_str)
        self.assertNotIn("DIAGNOSTIC ERROR DETAILS", table_str)


if __name__ == "__main__":
    unittest.main()
