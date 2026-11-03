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
        err = Exception("Model nvidia/nemotron-3.5-super-vl:free is currently offline")
        err.status_code = 503
        err.code = "model_offline"
        err.request_id = "req-offline-503"
        mock_client.chat.completions.create.side_effect = err

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
        self.assertEqual(res.request_id, "req-offline-503")
        self.assertIn("Model nvidia/nemotron-3.5-super-vl:free is currently offline", res.error_message)

    def test_d2_benchmark_json_contains_sanitized_diagnostic(self):
        """D2. Benchmark JSON output preserves error_type, error_code, and error_message in document_results."""
        mock_client = MagicMock()
        err404 = Exception("404 Model Not Found: nvidia/nemotron-3.5-super-vl:free")
        err404.status_code = 404
        err404.code = "model_not_found"
        mock_client.chat.completions.create.side_effect = err404

        from evaluation.runners.benchmark import BenchmarkRunner
        runner = BenchmarkRunner(provider_name="openrouter", model_name="nvidia/nemotron-3.5-super-vl:free", openrouter_api_key="test-key")
        runner.provider._injected_client = mock_client
        runner.provider.max_retries = 1
        runner.pipeline_service.extraction_provider = runner.provider

        dataset = build_default_synthetic_dataset()
        report = runner.run_benchmark(dataset, max_documents=1)

        json_str = report.model_dump_json()
        self.assertIn('"error_type":"ExtractionInvalidRequestError"', json_str)
        self.assertIn('"status_code":404', json_str)
        self.assertIn("404 Model Not Found", json_str)
        self.assertTrue(len(report.error_summaries) > 0)
        self.assertIn("404 Model Not Found", report.error_summaries[0])

    def test_d3_console_report_displays_error_summary(self):
        """D3. Console report formatted via format_summary_table displays diagnostic error section."""
        from evaluation.runners.benchmark import BenchmarkRunner, format_summary_table
        mock_client = MagicMock()
        err429 = Exception("Rate limit exceeded")
        err429.status_code = 429
        mock_client.chat.completions.create.side_effect = err429

        runner = BenchmarkRunner(provider_name="openrouter", model_name="nvidia/nemotron-3.5-super-vl:free", openrouter_api_key="test-key")
        runner.provider._injected_client = mock_client
        runner.provider.max_retries = 1
        runner.pipeline_service.extraction_provider = runner.provider

        dataset = build_default_synthetic_dataset()
        report = runner.run_benchmark(dataset, max_documents=1)
        console_table = format_summary_table(report)

        self.assertIn("DIAGNOSTIC ERROR DETAILS (1 failure):", console_table)
        self.assertIn("Type: ExtractionRateLimitError", console_table)
        self.assertIn("HTTP: 429", console_table)
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

    def test_free_only_model_enforcement(self):
        """Provider must reject non-free models and automatic routers, and pin default to free."""
        # Non-free models rejected
        with self.assertRaises(ExtractionConfigurationError) as ctx1:
            OpenRouterExtractionProvider(api_key="key", model_name="openai/gpt-4o")
        self.assertIn("not an explicitly approved free model", str(ctx1.exception))

        with self.assertRaises(ExtractionConfigurationError) as ctx2:
            OpenRouterExtractionProvider(api_key="key", model_name="openrouter/auto")
        self.assertIn("not an explicitly approved free model", str(ctx2.exception))

        with self.assertRaises(ExtractionConfigurationError) as ctx3:
            OpenRouterExtractionProvider(api_key="key", model_name="anthropic/claude-3.5-sonnet")
        self.assertIn("not an explicitly approved free model", str(ctx3.exception))

        # Approved free models allowed
        p1 = OpenRouterExtractionProvider(api_key="key", model_name="nvidia/nemotron-3.5-super-vl:free")
        self.assertEqual(p1.model_name, "nvidia/nemotron-3.5-super-vl:free")

        p2 = OpenRouterExtractionProvider(api_key="key", model_name="meta-llama/llama-3.3-70b-instruct:free")
        self.assertEqual(p2.model_name, "meta-llama/llama-3.3-70b-instruct:free")

        # Default model is nvidia/nemotron-3.5-super-vl:free
        p_default = OpenRouterExtractionProvider(api_key="key", model_name=None)
        self.assertEqual(p_default.model_name, "nvidia/nemotron-3.5-super-vl:free")

    def test_response_format_unsupported_auto_compatibility(self):
        """When a model rejects response_format (400), provider retries without it and succeeds."""
        mock_client = MagicMock()
        err400 = Exception("Provider does not support response_format: json_object")
        err400.status_code = 400

        success_response = _make_mock_chat_completion('{"document_type": "coi", "coverages": []}')

        mock_client.chat.completions.create.side_effect = [err400, success_response]

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Text on page")
        res = provider.extract_insurance_data("DOC-COMPAT", [page])

        self.assertEqual(res.document_type, "coi")
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)
        # First call attempted with response_format
        first_call_kwargs = mock_client.chat.completions.create.call_args_list[0].kwargs
        self.assertIn("response_format", first_call_kwargs)
        # Second call succeeded without response_format
        second_call_kwargs = mock_client.chat.completions.create.call_args_list[1].kwargs
        self.assertNotIn("response_format", second_call_kwargs)

    def test_malformed_json_does_not_echo_raw_completion_snippet(self):
        """Malformed JSON error messages must not include completion snippets or confidential text."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            '{"document_type": "coi", "secret": "CONFIDENTIAL_PAYLOAD_12345", broken_json'
        )

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Text")

        with self.assertRaises(ExtractionSchemaValidationError) as ctx:
            provider.extract_insurance_data("DOC-MALFORMED", [page])

        err_str = str(ctx.exception)
        self.assertNotIn("CONFIDENTIAL_PAYLOAD_12345", err_str)
        self.assertNotIn("Raw content:", err_str)
        self.assertIn("Failed to parse OpenRouter response as JSON", err_str)

    def test_pydantic_validation_error_does_not_echo_submitted_values(self):
        """Pydantic validation failure message must list field names without echoing submitted values."""
        mock_client = MagicMock()
        # Invalid type for named_insured (int instead of object)
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps({"document_type": "coi", "named_insured": 987654321})
        )

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Text")

        with self.assertRaises(ExtractionSchemaValidationError) as ctx:
            provider.extract_insurance_data("DOC-PYDANTIC-ERR", [page])

        err_str = str(ctx.exception)
        self.assertNotIn("987654321", err_str)
        self.assertIn("named_insured", err_str)

    def test_openrouter_failure_never_calls_gemini(self):
        """Failure in OpenRouter must never trigger Gemini or any other provider."""
        mock_client = MagicMock()
        err = Exception("OpenRouter service failure")
        err.status_code = 500
        mock_client.chat.completions.create.side_effect = err

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client, max_retries=1)
        pipeline = ExtractionPipelineService(extraction_provider=provider)

        inspection = PDFDocumentInspection(
            document_id="DOC-NO-FALLBACK",
            filename="test.pdf",
            file_size_bytes=1000,
            content_hash_sha256="hash",
            page_count=1,
            pages=[_make_dummy_page(1, "Text")],
            classified_type=DocumentType.COI,
            classification_confidence=1.0,
            classification_reason="test",
        )

        with patch("app.extraction.providers.gemini.GeminiExtractionProvider.extract_insurance_data") as mock_gemini:
            res = pipeline.process_inspection(inspection, "SUB-01")
            mock_gemini.assert_not_called()
            self.assertEqual(res.processing_status.value, "failed")
            self.assertEqual(res.error_category, "provider_error")

    def test_unrelated_http_400_does_not_retry(self):
        """HTTP 400 error unrelated to response_format must fail immediately with call_count == 1."""
        from app.extraction.providers.base import ExtractionInvalidRequestError

        mock_client = MagicMock()
        err400 = Exception("Bad Request: prompt token limit exceeded")
        err400.status_code = 400

        mock_client.chat.completions.create.side_effect = err400

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client, max_retries=3)
        page = _make_dummy_page(1, "Text on page")

        with self.assertRaises(ExtractionInvalidRequestError) as ctx:
            provider.extract_insurance_data("DOC-UNRELATED-400", [page])

        # Must not retry: exactly 1 call made
        self.assertEqual(mock_client.chat.completions.create.call_count, 1)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("prompt token limit exceeded", str(ctx.exception))

    def test_response_format_retry_at_most_once_on_persistent_failure(self):
        """Response_format 400 retries at most once; persistent 400 halts on attempt 2 without endless loops."""
        from app.extraction.providers.base import ExtractionInvalidRequestError

        mock_client = MagicMock()
        err_rf = Exception("The model does not support response_format: json_object")
        err_rf.status_code = 400

        err_second = Exception("Bad Request: invalid temperature")
        err_second.status_code = 400

        mock_client.chat.completions.create.side_effect = [err_rf, err_second]

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client, max_retries=3)
        page = _make_dummy_page(1, "Text on page")

        with self.assertRaises(ExtractionInvalidRequestError):
            provider.extract_insurance_data("DOC-PERSISTENT-400", [page])

        # Exactly 2 calls: 1st with response_format, 2nd without response_format, then stopped
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)
        call1_kwargs = mock_client.chat.completions.create.call_args_list[0].kwargs
        call2_kwargs = mock_client.chat.completions.create.call_args_list[1].kwargs
        self.assertIn("response_format", call1_kwargs)
        self.assertNotIn("response_format", call2_kwargs)

    def test_response_format_retry_preserves_mandatory_json_and_pydantic_validation(self):
        """When response_format retry succeeds at network layer, schema validation is NOT disabled."""
        mock_client = MagicMock()
        err_rf = Exception("Unsupported parameter: 'response_format'")
        err_rf.status_code = 400

        # Attempt 2 returns plain text that is NOT valid JSON
        non_json_response = _make_mock_chat_completion("Sorry, I cannot return JSON formatted data.")
        mock_client.chat.completions.create.side_effect = [err_rf, non_json_response]

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Text on page")

        with self.assertRaises(ExtractionSchemaValidationError) as ctx:
            provider.extract_insurance_data("DOC-SCHEMA-ENFORCED", [page])

        self.assertEqual(mock_client.chat.completions.create.call_count, 2)
        self.assertIn("Failed to parse OpenRouter response as JSON", str(ctx.exception))

    def test_end_to_end_offline_synthetic_pdf_to_deterministic_compliance(self):
        """End-to-end flow: Real PDF bytes -> PDF inspection -> OpenRouter extraction -> normalization -> evidence verification -> deterministic compliance evaluation."""
        from pathlib import Path
        from app.documents.processor import PDFProcessor
        from app.compliance.evaluator import ComplianceEvaluator
        from app.schemas.insurance import (
            ComplianceStatus,
            InsuranceRequirement,
            LimitField,
            RequirementType,
        )

        pdf_path = Path(__file__).resolve().parent.parent / "evaluation" / "fixtures" / "pdfs" / "synthetic_standard_acord25.pdf"
        self.assertTrue(pdf_path.exists(), f"Synthetic PDF fixture not found at {pdf_path}")
        pdf_bytes = pdf_path.read_bytes()

        # Step 1: PDF Inspection
        pdf_processor = PDFProcessor(render_dpi=72)
        inspection = pdf_processor.process_pdf(pdf_bytes, document_id="E2E-SYNTH-001")
        self.assertFalse(inspection.is_scanned)
        self.assertGreater(inspection.page_count, 0)
        self.assertEqual(inspection.classified_type, DocumentType.COI)

        # Step 2: OpenRouter Mocked Extraction (model returns verbatim text present in synthetic_standard_acord25.pdf)
        mock_client = MagicMock()
        valid_extraction_payload = {
            "document_type": "coi",
            "named_insured": {
                "name": "Apex Electrical Contractors LLC",
                "evidence": {
                    "page_number": 1,
                    "source_text": "INSURED: Apex Electrical Contractors LLC",
                },
            },
            "certificate_holder": {
                "name": "General Contractor Corp",
                "evidence": {
                    "page_number": 1,
                    "source_text": "CERTIFICATE HOLDER: General Contractor Corp",
                },
            },
            "coverages": [
                {
                    "coverage_type": "general_liability",
                    "policy_number": "GL-98765432",
                    "effective_date": "01/01/2025",
                    "expiration_date": "01/01/2026",
                    "each_occurrence": "$1,000,000",
                    "general_aggregate": "$2,000,000",
                    "evidence": {
                        "page_number": 1,
                        "source_text": "POLICY NUMBER: GL-98765432",
                    },
                }
            ],
            "endorsements": [],
        }
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps(valid_extraction_payload)
        )

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        pipeline = ExtractionPipelineService(extraction_provider=provider)

        # Step 3: Pipeline Execution (Extraction + Normalization + Evidence Verification)
        pipeline_result = pipeline.process_inspection(inspection, subcontractor_id="SUB-APEX")
        self.assertEqual(pipeline_result.processing_status.value, "processed")
        self.assertIsNotNone(pipeline_result.document_set)

        doc_set = pipeline_result.document_set
        self.assertEqual(doc_set.named_insured.name, "Apex Electrical Contractors LLC")
        self.assertEqual(doc_set.named_insured.extraction_status, ExtractionStatus.EXTRACTED)
        self.assertIsNotNone(doc_set.named_insured.evidence)
        self.assertEqual(doc_set.named_insured.evidence.page_number, 1)

        # Confirm currency normalized to integer cents
        gl_coverage = doc_set.coverages[0]
        self.assertEqual(gl_coverage.each_occurrence.amount_cents, 100_000_000)
        self.assertEqual(gl_coverage.general_aggregate.amount_cents, 200_000_000)
        self.assertEqual(gl_coverage.effective_date.date, date(2025, 1, 1))
        self.assertEqual(gl_coverage.expiration_date.date, date(2026, 1, 1))

        # Step 4: Deterministic Compliance Engine Evaluation
        req_gl = InsuranceRequirement(
            requirement_id="REQ_GL_EACH_OCC",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="General Liability Each Occurrence >= $1,000,000",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            limit_field=LimitField.EACH_OCCURRENCE,
            min_amount_cents=100_000_000,
        )
        req_ni = InsuranceRequirement(
            requirement_id="REQ_NI",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named Insured must match Apex Electrical Contractors LLC",
            required_entity_name="Apex Electrical Contractors LLC",
        )

        compliance_evaluator = ComplianceEvaluator()
        review = compliance_evaluator.evaluate(
            document_set=doc_set,
            requirements=[req_gl, req_ni],
            evaluation_date=date(2025, 6, 1),
        )

        self.assertEqual(review.overall_status, ComplianceStatus.COMPLIANT)
        self.assertFalse(review.requires_human_review)
        self.assertEqual(len(review.findings), 2)
        self.assertEqual(review.findings[0].status, ComplianceStatus.COMPLIANT)
        self.assertEqual(review.findings[1].status, ComplianceStatus.COMPLIANT)

    def test_evidence_integrity_tampered_page_or_quote_routes_to_human_review(self):
        """Evidence verification prevents unsupported quotes: invalid pages and corrupt quotes become UNCERTAIN."""
        from pathlib import Path
        from app.documents.processor import PDFProcessor
        from app.compliance.evaluator import ComplianceEvaluator
        from app.schemas.insurance import (
            ComplianceStatus,
            InsuranceRequirement,
            RequirementType,
        )

        pdf_path = Path(__file__).resolve().parent.parent / "evaluation" / "fixtures" / "pdfs" / "synthetic_standard_acord25.pdf"
        pdf_processor = PDFProcessor(render_dpi=72)
        inspection = pdf_processor.process_pdf(pdf_path.read_bytes(), document_id="E2E-TAMPER-001")

        # Case 1: Non-existent page (e.g. Page 99)
        mock_client = MagicMock()
        tampered_page_payload = {
            "document_type": "coi",
            "named_insured": {
                "name": "Acme Construction LLC",
                "evidence": {
                    "page_number": 99,
                    "source_text": "Acme Construction LLC",
                },
            },
        }
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(json.dumps(tampered_page_payload))
        provider1 = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        pipeline1 = ExtractionPipelineService(extraction_provider=provider1)
        res1 = pipeline1.process_inspection(inspection, "SUB-01")

        # Page 99 doesn't exist -> UNCERTAIN, evidence unverified
        self.assertEqual(res1.document_set.named_insured.extraction_status, ExtractionStatus.UNCERTAIN)
        self.assertIsNone(res1.document_set.named_insured.evidence)

        # Case 2: Corrupt quote that does not appear in the PDF
        tampered_quote_payload = {
            "document_type": "coi",
            "named_insured": {
                "name": "Acme Construction LLC",
                "evidence": {
                    "page_number": 1,
                    "source_text": "FABRICATED_INSURANCE_QUOTE_TEXT_THAT_DOES_NOT_EXIST",
                },
            },
        }
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(json.dumps(tampered_quote_payload))
        provider2 = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        pipeline2 = ExtractionPipelineService(extraction_provider=provider2)
        res2 = pipeline2.process_inspection(inspection, "SUB-02")

        self.assertEqual(res2.document_set.named_insured.extraction_status, ExtractionStatus.UNCERTAIN)
        self.assertIsNone(res2.document_set.named_insured.evidence)

        # Case 3: Compliance Evaluator with mandatory evidence rejects UNCERTAIN fields
        req_ni = InsuranceRequirement(
            requirement_id="REQ_NI_STRICT",
            rule_type=RequirementType.NAMED_INSURED,
            description="Strict Named Insured",
            required_entity_name="Acme Construction LLC",
            mandatory_evidence=True,
        )
        evaluator = ComplianceEvaluator()
        review = evaluator.evaluate(
            document_set=res2.document_set,
            requirements=[req_ni],
            evaluation_date=date(2025, 6, 1),
        )
        self.assertNotEqual(review.overall_status, ComplianceStatus.COMPLIANT)
        self.assertTrue(review.requires_human_review)

    def test_benchmark_runner_excludes_openrouter_provider_failure_from_accuracy(self):
        """When OpenRouter fails during benchmark run, metrics evaluator excludes it from accuracy denominator with 0 false negatives."""
        from evaluation.runners.benchmark import BenchmarkRunner

        mock_client = MagicMock()
        err = Exception("OpenRouter 502 Bad Gateway: upstream server error")
        err.status_code = 502
        mock_client.chat.completions.create.side_effect = err

        runner = BenchmarkRunner(
            provider_name="openrouter",
            model_name="nvidia/nemotron-3.5-super-vl:free",
            openrouter_api_key="test-key",
        )
        runner.provider._injected_client = mock_client
        runner.provider.max_retries = 1
        runner.pipeline_service.extraction_provider = runner.provider

        dataset = build_default_synthetic_dataset()
        report = runner.run_benchmark(dataset, max_documents=2)

        self.assertEqual(report.total_documents, 2)
        self.assertEqual(report.evaluated_documents_count, 0)
        self.assertEqual(report.provider_errors_count, 2)
        self.assertEqual(report.configuration_errors_count, 0)
        # Verify 0 false negatives in aggregate metrics
        field_stats = report.field_metrics.get("all")
        self.assertEqual(field_stats.false_negatives, 0)
        self.assertEqual(field_stats.total_evaluated, 0)
        self.assertTrue(len(report.error_summaries) == 2)
        self.assertIn("502 Bad Gateway", report.error_summaries[0])

    # ========================================================================
    # Phase 3A: Schema Failure Diagnostic Regression Tests
    # ========================================================================

    def test_schema_diagnostic_1_correctly_structured_insurer_list(self):
        """1. Correctly structured list of insurer strings parses cleanly into list."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps({"document_type": "coi", "insurers": ["Travelers Indemnity Co", "Hartford Fire Insurance"]})
        )
        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Text")
        res = provider.extract_insurance_data("DOC-INS-VALID", [page])

        self.assertIsInstance(res.insurers, list)
        self.assertEqual(res.insurers, ["Travelers Indemnity Co", "Hartford Fire Insurance"])

    def test_schema_diagnostic_2_missing_insurer_information_defaults_empty(self):
        """2. When insurers key is omitted, schema safely defaults to empty list."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps({"document_type": "coi"})
        )
        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Text")
        res = provider.extract_insurance_data("DOC-INS-OMITTED", [page])

        self.assertEqual(res.insurers, [])

    def test_schema_diagnostic_3_null_insurer_value_fails_validation_safely(self):
        """3. Null insurers value ('insurers': null) fails Pydantic validation safely without inventing values."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps({"document_type": "coi", "insurers": None})
        )
        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Text")

        with self.assertRaises(ExtractionSchemaValidationError) as ctx:
            provider.extract_insurance_data("DOC-INS-NULL", [page])

        self.assertIn("insurers (Input should be a valid list)", str(ctx.exception))
        self.assertEqual(ctx.exception.error_code, "schema_validation_failed")

    def test_schema_diagnostic_4_incorrect_insurer_type_string_or_dict_fails(self):
        """4. Incorrect insurer type (single string or key-value dict) fails validation safely."""
        provider = OpenRouterExtractionProvider(api_key="test-key")
        page = _make_dummy_page(1, "Text")

        # Case 4a: Single string instead of list
        mock_client_str = MagicMock()
        mock_client_str.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps({"document_type": "coi", "insurers": "Travelers Indemnity Co"})
        )
        provider._injected_client = mock_client_str
        with self.assertRaises(ExtractionSchemaValidationError) as ctx_str:
            provider.extract_insurance_data("DOC-INS-STR", [page])
        self.assertIn("insurers (Input should be a valid list)", str(ctx_str.exception))

        # Case 4b: Key-value dictionary (e.g. ACORD 25 box {'A': 'Travelers'}) instead of list
        mock_client_dict = MagicMock()
        mock_client_dict.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps({"document_type": "coi", "insurers": {"A": "Travelers Indemnity Co"}})
        )
        provider._injected_client = mock_client_dict
        with self.assertRaises(ExtractionSchemaValidationError) as ctx_dict:
            provider.extract_insurance_data("DOC-INS-DICT", [page])
        self.assertIn("insurers (Input should be a valid list)", str(ctx_dict.exception))

    def test_schema_diagnostic_5_valid_description_string_parses_cleanly(self):
        """5. Valid description_of_operations string parses cleanly."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps({
                "document_type": "coi",
                "description_of_operations": "Project: Commercial Tower Electrical Installation",
            })
        )
        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Text")
        res = provider.extract_insurance_data("DOC-DESC-VALID", [page])

        self.assertEqual(res.description_of_operations, "Project: Commercial Tower Electrical Installation")

    def test_schema_diagnostic_6_missing_description_defaults_none(self):
        """6. When description_of_operations is omitted, schema safely defaults to None."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps({"document_type": "coi"})
        )
        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Text")
        res = provider.extract_insurance_data("DOC-DESC-OMITTED", [page])

        self.assertIsNone(res.description_of_operations)

    def test_schema_diagnostic_7_null_description_parses_cleanly(self):
        """7. Null description_of_operations ('description_of_operations': null) parses cleanly as None."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps({"document_type": "coi", "description_of_operations": None})
        )
        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Text")
        res = provider.extract_insurance_data("DOC-DESC-NULL", [page])

        self.assertIsNone(res.description_of_operations)

    def test_schema_diagnostic_8_incorrect_description_type_dict_fails(self):
        """8. Incorrect description_of_operations (nested object/evidence) fails validation safely."""
        provider = OpenRouterExtractionProvider(api_key="test-key")
        page = _make_dummy_page(1, "Text")

        # Nested evidence object returned by LLM
        mock_client_obj = MagicMock()
        mock_client_obj.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps({
                "document_type": "coi",
                "description_of_operations": {
                    "text": "Project: Commercial Tower",
                    "evidence": {"page_number": 1, "source_text": "Commercial Tower"},
                },
            })
        )
        provider._injected_client = mock_client_obj
        with self.assertRaises(ExtractionSchemaValidationError) as ctx_obj:
            provider.extract_insurance_data("DOC-DESC-OBJ", [page])
        self.assertIn("description_of_operations (Input should be a valid string)", str(ctx_obj.exception))

    def test_schema_diagnostic_9_malformed_json_fails_safely(self):
        """9. Broken non-JSON model output fails safely with code invalid_json."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            "{'document_type': 'coi', unquoted_broken_json"
        )
        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Text")

        with self.assertRaises(ExtractionSchemaValidationError) as ctx:
            provider.extract_insurance_data("DOC-MALFORMED", [page])
        self.assertEqual(ctx.exception.error_code, "invalid_json")

    def test_schema_diagnostic_10_unsupported_evidence_never_invented(self):
        """10. Unsupported evidence (corrupted quote or invalid page) routes to UNCERTAIN without inventing values."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps({
                "document_type": "coi",
                "named_insured": {
                    "name": "Apex Electrical Contractors LLC",
                    "evidence": {
                        "page_number": 1,
                        "source_text": "NON_EXISTENT_QUOTE_THAT_IS_NOT_ON_PAGE",
                    },
                },
            })
        )
        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        pipeline = ExtractionPipelineService(extraction_provider=provider)

        inspection = PDFDocumentInspection(
            document_id="DOC-UNSUPPORTED-EV",
            filename="synthetic.pdf",
            file_size_bytes=1000,
            content_hash_sha256="dummy",
            page_count=1,
            pages=[_make_dummy_page(1, "Different text on page entirely")],
            classified_type=DocumentType.COI,
            classification_confidence=1.0,
            classification_reason="test",
        )
        res = pipeline.process_inspection(inspection, "SUB-01")

        # Must not invent quotes; must route to UNCERTAIN with unverified evidence
        self.assertIsNotNone(res.document_set)
        insured = res.document_set.named_insured
        self.assertEqual(insured.extraction_status, ExtractionStatus.UNCERTAIN)
        self.assertIsNone(insured.evidence)

    # ========================================================================
    # Phase 3B: OpenRouter Prompt Contract Verification Tests
    # ========================================================================

    def test_phase3b_prompt_explicitly_specifies_insurers_array_requirements(self):
        """Phase 3B: Prompt explicitly specifies insurers must be array of strings, [] if none, never null/str/dict."""
        from app.extraction.providers.openrouter import OPENROUTER_SYSTEM_PROMPT

        prompt_lower = OPENROUTER_SYSTEM_PROMPT.lower()
        self.assertIn('"insurers"', prompt_lower)
        self.assertIn("array of strings", prompt_lower)
        self.assertIn("empty array []", prompt_lower)
        self.assertIn("never return null", prompt_lower)
        self.assertIn("single string", prompt_lower)
        self.assertIn("dictionary", prompt_lower)

    def test_phase3b_prompt_explicitly_specifies_description_string_requirements(self):
        """Phase 3B: Prompt explicitly specifies description_of_operations must be plain string or null."""
        from app.extraction.providers.openrouter import OPENROUTER_SYSTEM_PROMPT

        prompt_lower = OPENROUTER_SYSTEM_PROMPT.lower()
        self.assertIn('"description_of_operations"', prompt_lower)
        self.assertIn("plain json string", prompt_lower)
        self.assertIn("null if blank", prompt_lower)
        self.assertIn("never return an evidence object, dictionary, or array", prompt_lower)

    def test_phase3b_prompt_instructs_evidence_strictly_in_designated_structures(self):
        """Phase 3B: Prompt explicitly restricts evidence to designated sub-objects and forbids label-only proof."""
        from app.extraction.providers.openrouter import OPENROUTER_SYSTEM_PROMPT

        prompt_lower = OPENROUTER_SYSTEM_PROMPT.lower()
        self.assertIn("evidence belongs strictly in dedicated \"evidence\" sub-objects", prompt_lower)
        self.assertIn("do not invent quotations", prompt_lower)
        self.assertIn("isolated form labels as proof", prompt_lower)

    def test_phase3b_schema_derived_payload_passes_provider_validation(self):
        """Phase 3B: A payload constructed following the prompt schema validates cleanly against RawInsuranceExtraction."""
        schema_derived_payload = {
            "document_type": "coi",
            "named_insured": {
                "name": "Acme Construction LLC",
                "address": "123 Main St, Austin, TX",
                "evidence": {
                    "page_number": 1,
                    "source_text": "INSURED: Acme Construction LLC",
                    "confidence": 1.0,
                },
                "confidence": 1.0,
            },
            "certificate_holder": {
                "name": "General Contractor Corp",
                "address": "500 Builder Way, Dallas, TX",
                "evidence": {
                    "page_number": 1,
                    "source_text": "CERTIFICATE HOLDER: General Contractor Corp",
                    "confidence": 1.0,
                },
                "confidence": 1.0,
            },
            "producer": None,
            "insurers": ["Travelers Indemnity Co"],
            "coverages": [
                {
                    "coverage_type": "general_liability",
                    "policy_number": "GL-98765432",
                    "insurer_name": "Travelers Indemnity Co",
                    "each_occurrence": "$1,000,000",
                    "general_aggregate": "$2,000,000",
                    "products_completed_ops": "$2,000,000",
                    "personal_adv_injury": "$1,000,000",
                    "effective_date": "01/01/2025",
                    "expiration_date": "01/01/2026",
                    "evidence": {
                        "page_number": 1,
                        "source_text": "COMMERCIAL GENERAL LIABILITY",
                        "confidence": 1.0,
                    },
                    "confidence": 1.0,
                }
            ],
            "endorsements": [],
            "description_of_operations": "Project: Commercial Tower Framing and Drywall",
            "raw_contradictions": [],
            "is_document_set_complete": True,
        }

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            json.dumps(schema_derived_payload)
        )
        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Text")
        res = provider.extract_insurance_data("DOC-SCHEMA-DERIVED", [page])

        self.assertEqual(res.document_type, "coi")
        self.assertEqual(res.named_insured.name, "Acme Construction LLC")
        self.assertEqual(res.insurers, ["Travelers Indemnity Co"])
        self.assertEqual(res.description_of_operations, "Project: Commercial Tower Framing and Drywall")
        self.assertEqual(res.coverages[0].each_occurrence, "$1,000,000")


if __name__ == "__main__":
    unittest.main()
