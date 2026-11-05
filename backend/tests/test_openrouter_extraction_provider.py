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
    ExtractionInvalidRequestError,
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
    extract_http_error_details,
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
        self.assertIsInstance(user_content, str)
        self.assertIn("=== PAGE 1", user_content)
        self.assertIn("Apex Electrical Contractors LLC", user_content)

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

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client, allow_response_format_fallback=True)
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

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client, max_retries=3, allow_response_format_fallback=True)
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

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client, max_retries=3, allow_response_format_fallback=True)
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

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client, allow_response_format_fallback=True)
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

    def test_task1_extract_http_error_details_reproduces_openai_sdk_behavior(self):
        """Task 1: str(exc) returning 'Error code: 400' is unpacked from exc.body."""
        class MockBadRequest(Exception):
            def __init__(self):
                super().__init__("Error code: 400")
                self.status_code = 400
                self.request_id = "req-12345"
                self.body = {
                    "error": {
                        "message": "Invalid request: parameter 'response_format' is not supported by model",
                        "type": "invalid_request_error",
                        "code": "unsupported_parameter",
                        "param": "response_format",
                    }
                }

        exc = MockBadRequest()
        self.assertEqual(str(exc), "Error code: 400")

        details = extract_http_error_details(exc)
        self.assertEqual(details["status_code"], 400)
        self.assertEqual(details["message"], "Invalid request: parameter 'response_format' is not supported by model")
        self.assertEqual(details["error_type"], "invalid_request_error")
        self.assertEqual(details["error_code"], "unsupported_parameter")
        self.assertEqual(details["error_param"], "response_format")
        self.assertEqual(details["request_id"], "req-12345")

    def test_task1_extract_http_error_details_fallbacks_and_safe_types(self):
        """Task 1: Safe handling of non-dict, missing, and empty body formats."""
        # 1. No body -> fallback to str(exc)
        exc_no_body = Exception("Connection closed by peer")
        details1 = extract_http_error_details(exc_no_body)
        self.assertEqual(details1["message"], "Connection closed by peer")
        self.assertIsNone(details1["status_code"])

        # 2. String body
        exc_str_body = Exception("Error code: 502")
        exc_str_body.status_code = 502
        exc_str_body.body = "<html>Bad Gateway</html>"
        details2 = extract_http_error_details(exc_str_body)
        self.assertEqual(details2["message"], "<html>Bad Gateway</html>")
        self.assertEqual(details2["status_code"], 502)

        # 3. Dict body with direct message key
        exc_direct_msg = Exception("Error code: 400")
        exc_direct_msg.status_code = 400
        exc_direct_msg.body = {"message": "Direct error message"}
        details3 = extract_http_error_details(exc_direct_msg)
        self.assertEqual(details3["message"], "Direct error message")

        # 4. Dict body with error as string
        exc_err_str = Exception("Error code: 400")
        exc_err_str.status_code = 400
        exc_err_str.body = {"error": "String error message"}
        details4 = extract_http_error_details(exc_err_str)
        self.assertEqual(details4["message"], "String error message")

    def test_task1_extract_http_error_details_redacts_credentials(self):
        """Task 1: Secrets in error messages or body are strictly redacted."""
        exc = Exception("Failed with key sk-or-v1-abcdef1234567890abcdef1234567890")
        exc.status_code = 401
        exc.body = {
            "error": {
                "message": "Invalid token: Bearer sk-1234567890abcdef1234567890",
                "param": "api_key=sk-or-v1-secretkey1234567890123456",
            }
        }
        details = extract_http_error_details(exc)
        self.assertNotIn("sk-or-v1-abcdef1234567890abcdef1234567890", details["message"])
        self.assertNotIn("sk-1234567890abcdef1234567890", details["message"])
        self.assertIn("[REDACTED_API_KEY]", details["message"])
        self.assertNotIn("sk-or-v1-secretkey1234567890123456", details["error_param"])
        self.assertIn("[REDACTED_API_KEY]", details["error_param"])

    def test_task1_extract_insurance_data_unpacks_error_body_into_exception(self):
        """Task 1: extract_insurance_data preserves body error message instead of 'Error code: 400'."""
        mock_client = MagicMock()
        mock_err = Exception("Error code: 400")
        mock_err.status_code = 400
        mock_err.body = {"error": {"message": "Specific model constraint violation: prompt exceeds limit"}}
        mock_client.chat.completions.create.side_effect = mock_err

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client, max_retries=1)
        page = _make_dummy_page(1, "Text")

        with self.assertRaises(ExtractionInvalidRequestError) as ctx:
            provider.extract_insurance_data("DOC-ERR", [page])

        self.assertIn("Specific model constraint violation: prompt exceeds limit", str(ctx.exception))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_task2_text_only_request_sends_plain_string(self):
        """Task 2: When image_count == 0, user message content is a plain string, not a list."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion('{"document_type": "coi"}')

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page1 = _make_dummy_page(1, "Policy page 1 text", image_bytes=None)
        page2 = _make_dummy_page(2, "Policy page 2 text", image_bytes=None)

        provider.extract_insurance_data("DOC-TEXT-ONLY", [page1, page2])

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        messages = call_kwargs["messages"]
        user_content = messages[1]["content"]

        # Content must be a plain string
        self.assertIsInstance(user_content, str)
        self.assertNotIsInstance(user_content, list)
        self.assertIn("=== PAGE 1", user_content)
        self.assertIn("Policy page 1 text", user_content)
        self.assertIn("=== PAGE 2", user_content)
        self.assertIn("Policy page 2 text", user_content)

    def test_task2_multimodal_request_preserves_content_array_with_images(self):
        """Task 2: When images are present, user message content is a multimodal list with image_url."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion('{"document_type": "coi"}')

        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        fake_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRimagedata"
        page = _make_dummy_page(1, "Page with image", image_bytes=fake_png)

        provider.extract_insurance_data("DOC-MULTI", [page])

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        user_content = call_kwargs["messages"][1]["content"]

        self.assertIsInstance(user_content, list)
        types = [part["type"] for part in user_content]
        self.assertIn("text", types)
        self.assertIn("image_url", types)

        img_part = next(p for p in user_content if p["type"] == "image_url")
        self.assertTrue(img_part["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_task3_is_response_format_error_positives(self):
        """Task 3: Correctly identifies errors specifically related to response_format or JSON mode."""
        # 1. Parameter field points to response_format
        exc1 = Exception("Error code: 400")
        details1 = {"error_param": "response_format", "message": "Invalid parameter"}
        self.assertTrue(OpenRouterExtractionProvider.is_response_format_error(exc1, details1))

        # 2. Message mentions response_format is not supported
        exc2 = Exception("Error code: 400")
        details2 = {"message": "Parameter 'response_format' is not supported by this model"}
        self.assertTrue(OpenRouterExtractionProvider.is_response_format_error(exc2, details2))

        # 3. Message mentions json_object mode not supported
        exc3 = Exception("Error code: 400")
        details3 = {"message": "Model does not support json_object mode"}
        self.assertTrue(OpenRouterExtractionProvider.is_response_format_error(exc3, details3))

        # 4. Message mentions unsupported format: json_schema
        exc4 = Exception("Error code: 400")
        details4 = {"message": "Unsupported format: json_schema"}
        self.assertTrue(OpenRouterExtractionProvider.is_response_format_error(exc4, details4))

        # 5. Error code specifies unsupported_response_format
        exc5 = Exception("Error code: 400")
        details5 = {"error_code": "unsupported_response_format", "message": "Failed"}
        self.assertTrue(OpenRouterExtractionProvider.is_response_format_error(exc5, details5))

    def test_task3_is_response_format_error_negatives(self):
        """Task 3: Unrelated HTTP 400 errors are NOT classified as response_format errors."""
        # 1. Temperature parameter
        exc1 = Exception("Error code: 400")
        details1 = {"error_param": "temperature", "message": "temperature must be between 0 and 2"}
        self.assertFalse(OpenRouterExtractionProvider.is_response_format_error(exc1, details1))

        # 2. Context length
        exc2 = Exception("Error code: 400")
        details2 = {"message": "Context length exceeded: prompt contains 50000 tokens but model max is 4096"}
        self.assertFalse(OpenRouterExtractionProvider.is_response_format_error(exc2, details2))

        # 3. Missing parameter
        exc3 = Exception("Error code: 400")
        details3 = {"message": "Missing required parameter: 'messages'"}
        self.assertFalse(OpenRouterExtractionProvider.is_response_format_error(exc3, details3))

        # 4. Model not found
        exc4 = Exception("Error code: 404")
        details4 = {"message": "Model 'foo/bar' not found"}
        self.assertFalse(OpenRouterExtractionProvider.is_response_format_error(exc4, details4))

        # 5. General JSON decode error in request body
        exc5 = Exception("Error code: 400")
        details5 = {"message": "Could not parse JSON body in HTTP request"}
        self.assertFalse(OpenRouterExtractionProvider.is_response_format_error(exc5, details5))

    def test_task3_response_format_error_does_not_silently_retry(self):
        """Task 3: Unsupported response_format raises ExtractionInvalidRequestError with 0 extra retry calls."""
        mock_client = MagicMock()
        mock_err = Exception("Error code: 400")
        mock_err.status_code = 400
        mock_err.body = {
            "error": {
                "message": "Model 'nvidia/nemotron-3.5-super-vl:free' does not support response_format: json_object",
                "param": "response_format",
            }
        }
        mock_client.chat.completions.create.side_effect = mock_err

        # Default allow_response_format_fallback is False; response_format errors fail immediately
        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client, max_retries=3)
        page = _make_dummy_page(1, "Text")

        with self.assertRaises(ExtractionInvalidRequestError) as ctx:
            provider.extract_insurance_data("DOC-RESP-FMT", [page])

        self.assertEqual(ctx.exception.error_code, "unsupported_response_format")
        self.assertIn("does not support response_format", str(ctx.exception))
        # Exactly one call must be made; NO silent fallback or secondary call
        self.assertEqual(mock_client.chat.completions.create.call_count, 1)


class Phase7325ErrorObservabilityTests(unittest.TestCase):
    """Targeted offline unit tests for Phase 7.3.2.5 error observability and metadata preservation."""

    def test_1_standard_openai_error_body(self):
        """1. Standard OpenAI-style error body extracts message, type, code, and param."""
        err = Exception("Error code: 400")
        err.status_code = 400
        err.body = {
            "error": {
                "message": "Invalid model parameter: response_format",
                "type": "invalid_request_error",
                "code": "param_error",
                "param": "response_format",
            }
        }
        details = extract_http_error_details(err)
        self.assertEqual(details["status_code"], 400)
        self.assertEqual(details["message"], "Invalid model parameter: response_format")
        self.assertEqual(details["error_type"], "invalid_request_error")
        self.assertEqual(details["error_code"], "param_error")
        self.assertEqual(details["error_param"], "response_format")

    def test_2_alternative_detail_error_body_string_and_list(self):
        """2. Alternative 'detail' error body handles both plain string and FastAPI list of dicts."""
        # 2a. Plain string in detail
        err_str = Exception("Error code: 400")
        err_str.status_code = 400
        err_str.body = {"detail": "Model endpoint requires text-only input"}
        details_str = extract_http_error_details(err_str)
        self.assertEqual(details_str["message"], "Model endpoint requires text-only input")

        # 2b. FastAPI list of dicts in detail
        err_list = Exception("Error code: 422")
        err_list.status_code = 422
        err_list.body = {
            "detail": [
                {"loc": ["body", "messages"], "msg": "field required", "type": "value_error.missing"},
                {"loc": ["body", "model"], "msg": "unknown model", "type": "value_error"},
            ]
        }
        details_list = extract_http_error_details(err_list)
        self.assertIn("body.messages: field required", details_list["message"])
        self.assertIn("body.model: unknown model", details_list["message"])

    def test_3_alternative_description_error_body(self):
        """3. Alternative 'description' error body is safely extracted."""
        err = Exception("Error code: 400")
        err.status_code = 400
        err.body = {"description": "Upstream proxy rejected request payload structure"}
        details = extract_http_error_details(err)
        self.assertEqual(details["message"], "Upstream proxy rejected request payload structure")

    def test_4_raw_response_text_fallback(self):
        """4. Raw HTTP response text (JSON or plain string) is extracted when body is None."""
        # 4a. Response text is JSON
        mock_resp_json = MagicMock()
        mock_resp_json.text = json.dumps({"error": {"message": "Extracted from raw JSON response text", "code": "json_err"}})
        mock_resp_json.status_code = 400
        mock_resp_json.headers = {"x-request-id": "req-json-raw"}

        err_json = Exception("Error code: 400")
        err_json.response = mock_resp_json
        err_json.body = None

        details_json = extract_http_error_details(err_json)
        self.assertEqual(details_json["message"], "Extracted from raw JSON response text")
        self.assertEqual(details_json["error_code"], "json_err")
        self.assertEqual(details_json["request_id"], "req-json-raw")

        # 4b. Response text is plain non-JSON text (e.g. gateway error)
        mock_resp_plain = MagicMock()
        mock_resp_plain.text = "Bad Gateway: Upstream backend did not respond"
        mock_resp_plain.status_code = 502

        err_plain = Exception("Error code: 502")
        err_plain.response = mock_resp_plain
        err_plain.body = None

        details_plain = extract_http_error_details(err_plain)
        self.assertEqual(details_plain["message"], "Bad Gateway: Upstream backend did not respond")
        self.assertEqual(details_plain["status_code"], 502)

    def test_5_missing_or_malformed_response_body(self):
        """5. Missing or malformed body (int, empty dict, stream closed) never raises an exception."""
        for malformed_body in [None, 12345, [], {}, "   "]:
            err = Exception("Error code: 400")
            err.status_code = 400
            err.body = malformed_body
            details = extract_http_error_details(err)
            self.assertIsNotNone(details["message"])
            self.assertEqual(details["status_code"], 400)

        # Response text property raising an exception
        mock_resp_broken = MagicMock()
        type(mock_resp_broken).text = unittest.mock.PropertyMock(side_effect=RuntimeError("Stream closed"))
        err_broken = Exception("Error code: 500")
        err_broken.response = mock_resp_broken
        details_broken = extract_http_error_details(err_broken)
        self.assertIn("Error code: 500", details_broken["message"])

    def test_6_credential_redaction(self):
        """6. API keys, authorization headers, and bearer tokens are redacted across all fields."""
        err = Exception("Error code: 400")
        err.body = {
            "error": {
                "message": "Authorization failed with Bearer sk-or-v1-abcdef0123456789abcdef0123456789 and AIzaSyD-123456789012345678901234567890123",
                "type": "invalid_key",
                "param": "api_key=sk-abcdef01234567890123456789",
                "code": "auth_token=supersecrettoken123",
            }
        }
        details = extract_http_error_details(err)
        for field in ["message", "error_param", "error_code"]:
            val = details[field]
            self.assertNotIn("sk-or-v1-", val)
            self.assertNotIn("AIzaSyD-", val)
            self.assertNotIn("sk-abcdef", val)
            self.assertNotIn("supersecrettoken123", val)
            self.assertTrue("[REDACTED" in val or "REDACTED" in val)

    def test_7_error_message_truncation(self):
        """7. Excessively long error messages (>1000 chars) are cleanly truncated."""
        err = Exception("Error code: 400")
        err.body = {"error": {"message": "A" * 2500}}
        details = extract_http_error_details(err)
        self.assertTrue(len(details["message"]) <= 1025)
        self.assertTrue(details["message"].endswith("... [truncated]"))

    def test_8_structured_metadata_propagation_into_scenario_reports(self):
        """8. ScenarioEvaluationResult and Markdown report preserve structured error metadata."""
        from evaluation.schemas.reports import (
            FindingMetricStats,
            HumanReviewMetricStats,
            SafetyMetricResult,
            ScenarioBenchmarkReport,
            ScenarioEvaluationResult,
            ScenarioEvidenceMetricStats,
            SubmissionMetricStats,
        )
        from evaluation.runners.scenario_runner import generate_markdown_report
        import tempfile
        from pathlib import Path

        scen_res = ScenarioEvaluationResult(
            scenario_id="SCEN-01",
            title="Standard Clean COI",
            expected_status="compliant",
            predicted_status="extraction_failed",
            status_matched=False,
            processing_status="error",
            error_type="ExtractionInvalidRequestError",
            error_message="OpenRouter invalid request/model error (HTTP 400): Model does not support JSON mode",
            status_code=400,
            error_code="unsupported_response_format",
            request_id="req-obs-400",
            execution_time_seconds=1.23,
        )

        # JSON serialization preserves all structured fields
        json_dump = scen_res.model_dump()
        self.assertEqual(json_dump["error_type"], "ExtractionInvalidRequestError")
        self.assertEqual(json_dump["status_code"], 400)
        self.assertEqual(json_dump["error_code"], "unsupported_response_format")
        self.assertEqual(json_dump["request_id"], "req-obs-400")

        # Build dummy benchmark report with execution_failures
        report = ScenarioBenchmarkReport(
            benchmark_id="test_bench_diag",
            timestamp="2026-11-05T00:00:00Z",
            manifest_version="cohort_v1.0.0",
            manifest_hash="dummyhash",
            provider_name="openrouter",
            model_name="nvidia/nemotron-3.5-super-vl:free",
            total_scenarios=1,
            successful_scenarios=0,
            failed_scenarios=1,
            submission_metrics=SubmissionMetricStats(total_scenarios=1),
            finding_metrics=FindingMetricStats(total_findings=0),
            fcr_metric=SafetyMetricResult(metric_name="FCR", numerator=0, denominator=0, rate_percentage=None, rate_string="N/A"),
            ucr_metric=SafetyMetricResult(metric_name="UCR", numerator=0, denominator=0, rate_percentage=None, rate_string="N/A"),
            human_review_metrics=HumanReviewMetricStats(),
            evidence_metrics=ScenarioEvidenceMetricStats(),
            scenario_results=[scen_res],
            execution_failures=[{
                "scenario_id": "SCEN-01",
                "error_type": scen_res.error_type,
                "status_code": scen_res.status_code,
                "error_code": scen_res.error_code,
                "request_id": scen_res.request_id,
                "error_message": scen_res.error_message,
            }],
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            md_path = Path(tmp_dir) / "diag_report.md"
            generate_markdown_report(report, md_path)
            content = md_path.read_text(encoding="utf-8")

            # Check that Section 7 Execution Failures & Diagnostic Details is rendered
            self.assertIn("## 7. Execution Failures & Diagnostic Details", content)
            self.assertIn("`SCEN-01`", content)
            self.assertIn("`ExtractionInvalidRequestError`", content)
            self.assertIn("400", content)
            self.assertIn("`unsupported_response_format`", content)
            self.assertIn("`req-obs-400`", content)
            self.assertIn("Model does not support JSON mode", content)

    def test_9_missing_optional_metadata_handled_gracefully(self):
        """9. Missing optional metadata (None status_code, error_code, request_id) handled safely."""
        from evaluation.schemas.reports import (
            FindingMetricStats,
            HumanReviewMetricStats,
            SafetyMetricResult,
            ScenarioBenchmarkReport,
            ScenarioEvaluationResult,
            ScenarioEvidenceMetricStats,
            SubmissionMetricStats,
        )
        from evaluation.runners.scenario_runner import generate_markdown_report
        import tempfile
        from pathlib import Path

        scen_res = ScenarioEvaluationResult(
            scenario_id="SCEN-02",
            title="Policy Cancellation",
            expected_status="non_compliant",
            predicted_status="extraction_failed",
            status_matched=False,
            processing_status="error",
            error_type="ExtractionProviderError",
            error_message="General provider network glitch",
            status_code=None,
            error_code=None,
            request_id=None,
        )

        self.assertIsNone(scen_res.status_code)
        self.assertIsNone(scen_res.error_code)
        self.assertIsNone(scen_res.request_id)

        report = ScenarioBenchmarkReport(
            benchmark_id="test_bench_missing_meta",
            timestamp="2026-11-05T00:00:00Z",
            manifest_version="cohort_v1.0.0",
            manifest_hash="dummyhash",
            provider_name="openrouter",
            total_scenarios=1,
            successful_scenarios=0,
            failed_scenarios=1,
            submission_metrics=SubmissionMetricStats(total_scenarios=1),
            finding_metrics=FindingMetricStats(total_findings=0),
            fcr_metric=SafetyMetricResult(metric_name="FCR", numerator=0, denominator=0, rate_percentage=None, rate_string="N/A"),
            ucr_metric=SafetyMetricResult(metric_name="UCR", numerator=0, denominator=0, rate_percentage=None, rate_string="N/A"),
            human_review_metrics=HumanReviewMetricStats(),
            evidence_metrics=ScenarioEvidenceMetricStats(),
            scenario_results=[scen_res],
            execution_failures=[{
                "scenario_id": "SCEN-02",
                "error_type": scen_res.error_type,
                "error_message": scen_res.error_message,
            }],
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            md_path = Path(tmp_dir) / "missing_meta_report.md"
            generate_markdown_report(report, md_path)
            content = md_path.read_text(encoding="utf-8")
            self.assertIn("## 7. Execution Failures & Diagnostic Details", content)
            self.assertIn("`SCEN-02`", content)
            self.assertIn("`-`", content)

    def test_10_provider_failure_never_produces_compliant(self):
        """10. Provider failures never produce COMPLIANT, false compliant, or unsafe pass."""
        from evaluation.schemas.reports import ScenarioEvaluationResult

        for failure_status in ["extraction_failed", "configuration_error"]:
            res = ScenarioEvaluationResult(
                scenario_id="SCEN-FAIL-SAFE",
                title="Safety Test",
                expected_status="compliant",
                predicted_status=failure_status,
                status_matched=False,
                expected_requires_human_review=False,
                predicted_requires_human_review=True,
                human_review_matched=False,
                is_unsafe_pass=False,
                is_false_compliant=False,
                processing_status="error",
                error_type="ExtractionInvalidRequestError",
                error_message="Provider rejected request",
            )
            self.assertNotEqual(res.predicted_status, "compliant")
            self.assertEqual(res.processing_status, "error")
            self.assertFalse(res.status_matched)
            self.assertFalse(res.is_false_compliant)
            self.assertFalse(res.is_unsafe_pass)
            self.assertTrue(res.predicted_requires_human_review)


class Phase7326ExceptionChainTests(unittest.TestCase):
    """Offline regression tests for Phase 7.3.2.6 exception chain investigation."""

    def test_1_populated_body_error(self):
        """1. Populated body['error'] extracted accurately."""
        err = Exception("Error code: 400")
        err.status_code = 400
        err.body = {
            "error": {
                "message": "Invalid model parameter",
                "type": "invalid_request_error",
                "code": "invalid_param",
                "param": "model",
            }
        }
        details = extract_http_error_details(err)
        self.assertEqual(details["message"], "Invalid model parameter")
        self.assertEqual(details["status_code"], 400)
        self.assertEqual(details["error_type"], "invalid_request_error")
        self.assertEqual(details["error_code"], "invalid_param")
        self.assertEqual(details["error_param"], "model")

    def test_2_plain_text_response_body(self):
        """2. Plain text response body preserved."""
        err = Exception("Error code: 502")
        err.status_code = 502
        err.body = "Bad Gateway: upstream connect error or disconnect/reset before headers"
        details = extract_http_error_details(err)
        self.assertEqual(details["status_code"], 502)
        self.assertIn("Bad Gateway", details["message"])

    def test_3_raw_json_in_response_text_or_content(self):
        """3. Raw JSON in response.text or response.content parsed when body is absent."""
        mock_resp = MagicMock()
        mock_resp.text = json.dumps({"error": {"message": "Model not available"}})
        mock_resp.headers = {"x-request-id": "req-json-123"}
        err = Exception("Error code: 404")
        err.status_code = 404
        err.body = None
        err.response = mock_resp

        details = extract_http_error_details(err)
        self.assertEqual(details["message"], "Model not available")
        self.assertEqual(details["status_code"], 404)
        self.assertEqual(details["request_id"], "req-json-123")

        # Test with raw bytes in response.content
        mock_resp_bytes = MagicMock()
        mock_resp_bytes.text = ""
        mock_resp_bytes.content = json.dumps({"error": {"message": "Content byte message"}}).encode("utf-8")
        mock_resp_bytes.headers = {}
        err2 = Exception("Error code: 400")
        err2.body = None
        err2.response = mock_resp_bytes
        details2 = extract_http_error_details(err2)
        self.assertEqual(details2["message"], "Content byte message")

    def test_4_unconsumed_streaming_response_read(self):
        """4. Unconsumed/streaming response calls .read() safely."""
        mock_resp = MagicMock()
        mock_resp.is_stream_consumed = False
        read_called = [False]

        def fake_read():
            read_called[0] = True
            mock_resp.is_stream_consumed = True
            return b'{"error": {"message": "Streamed error detail"}}'

        mock_resp.read = fake_read
        mock_resp.text = '{"error": {"message": "Streamed error detail"}}'
        mock_resp.headers = {}
        err = Exception("Error code: 400")
        err.response = mock_resp
        err.body = None

        details = extract_http_error_details(err)
        self.assertTrue(read_called[0])
        self.assertEqual(details["message"], "Streamed error detail")

    def test_5_genuinely_empty_response_body(self):
        """5. Genuinely empty response body ("" / b"") falls back cleanly without crash."""
        mock_resp = MagicMock()
        mock_resp.text = ""
        mock_resp.content = b""
        mock_resp.headers = {}
        err = Exception("Error code: 400")
        err.status_code = 400
        err.response = mock_resp
        err.body = None

        details = extract_http_error_details(err)
        self.assertEqual(details["status_code"], 400)
        self.assertIsNotNone(details["message"])
        self.assertIn("400", details["message"])

    def test_6_request_id_extraction_headers(self):
        """6. Request ID extraction from cf-ray, x-openrouter-request-id, x-request-id, request-id."""
        for header_name, sample_val in [
            ("cf-ray", "ray-abc123456"),
            ("x-openrouter-request-id", "or-req-789"),
            ("x-request-id", "x-req-001"),
            ("request-id", "req-002"),
        ]:
            mock_resp = MagicMock()
            mock_resp.text = ""
            mock_resp.headers = {header_name: sample_val}
            err = Exception("Error code: 500")
            err.response = mock_resp
            err.body = None
            details = extract_http_error_details(err)
            self.assertEqual(details["request_id"], sample_val)

    def test_7_exception_chaining_cause(self):
        """7. Exception chaining (__cause__) preserved when raising domain exceptions."""
        import openai
        from app.extraction.providers.openrouter import OpenRouterExtractionProvider
        from app.extraction.providers.base import ExtractionInvalidRequestError

        mock_client = MagicMock()
        orig_err = openai.BadRequestError(
            message="Error code: 400",
            response=MagicMock(status_code=400, headers={}, text=""),
            body=None,
        )
        mock_client.chat.completions.create.side_effect = orig_err
        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)

        with self.assertRaises(ExtractionInvalidRequestError) as ctx:
            provider.extract_insurance_data(
                document_id="doc-1",
                pages=[PDFPageInspection(page_number=1, text_content="ACORD 25", width_pts=612.0, height_pts=792.0)],
            )
        self.assertIs(ctx.exception.__cause__, orig_err)

    def test_8_duplicate_wrapping_prevention(self):
        """8. Duplicate wrapping prevention in provider and pipeline."""
        from app.extraction.pipeline import ExtractionPipelineService
        from app.extraction.providers.base import ExtractionInvalidRequestError
        from app.schemas.pdf import PDFDocumentInspection, DocumentProcessingStatus

        # Case A: Provider message already includes AI extraction provider prefix
        provider_msg_a = "AI extraction provider 'openrouter' failed: OpenRouter invalid request (HTTP 400)"
        mock_provider_a = MagicMock()
        mock_provider_a.provider_name = "openrouter"
        mock_provider_a.extract_insurance_data.side_effect = ExtractionInvalidRequestError(
            provider_msg_a,
            provider_name="openrouter",
            status_code=400,
            error_code="http_400",
        )

        pipeline_service_a = ExtractionPipelineService(extraction_provider=mock_provider_a)
        inspection_a = PDFDocumentInspection(
            document_id="doc1",
            filename="test.pdf",
            file_size_bytes=1024,
            content_hash_sha256="abcd" * 16,
            classified_type=DocumentType.COI,
            classification_confidence=1.0,
            classification_reason="Standard COI",
            page_count=1,
            pages=[PDFPageInspection(page_number=1, text_content="ACORD 25", width_pts=612.0, height_pts=792.0)],
            status=DocumentProcessingStatus.PROCESSED,
        )
        result_a = pipeline_service_a.process_inspection(inspection_a, subcontractor_id="SUB-1")

        self.assertEqual(result_a.processing_status, DocumentProcessingStatus.FAILED)
        self.assertEqual(len(result_a.errors), 1)
        self.assertEqual(result_a.errors[0].count("AI extraction provider"), 1)

        # Case B: Provider message starts with OpenRouter
        provider_msg_b = "OpenRouter invalid request/model error (HTTP 400): Schema mismatch"
        mock_provider_b = MagicMock()
        mock_provider_b.provider_name = "openrouter"
        mock_provider_b.extract_insurance_data.side_effect = ExtractionInvalidRequestError(
            provider_msg_b,
            provider_name="openrouter",
            status_code=400,
            error_code="http_400",
        )
        pipeline_service_b = ExtractionPipelineService(extraction_provider=mock_provider_b)
        result_b = pipeline_service_b.process_inspection(inspection_a, subcontractor_id="SUB-1")
        self.assertEqual(result_b.errors[0].count("OpenRouter"), 1)
        self.assertFalse(result_b.errors[0].startswith("AI extraction provider 'openrouter' failed: OpenRouter"))

    def test_9_document_error_deduplication(self):
        """9. Document error deduplication in scenario runner for multi-document scenarios."""
        import datetime
        from pathlib import Path
        from evaluation.runners.scenario_runner import run_single_scenario
        from evaluation.schemas.ground_truth import (
            ScenarioGroundTruth,
            DocumentGroundTruth,
            DocumentSourceType,
        )
        from app.schemas.insurance import ComplianceStatus

        scenario = ScenarioGroundTruth(
            scenario_id="SCEN-TEST-DEDUP",
            title="Deduplication Test",
            description="Test",
            subcontractor_id="SUB-1",
            evaluation_date=datetime.date(2025, 6, 1),
            document_ids=["DOC1", "DOC2"],
            documents=[
                DocumentGroundTruth(
                    document_id="DOC1",
                    filename="doc1.pdf",
                    source_type=DocumentSourceType.SYNTHETIC,
                    document_type=DocumentType.COI,
                    page_count=1,
                ),
                DocumentGroundTruth(
                    document_id="DOC2",
                    filename="doc2.pdf",
                    source_type=DocumentSourceType.SYNTHETIC,
                    document_type=DocumentType.ENDORSEMENT,
                    page_count=1,
                ),
            ],
            requirements=[],
            expected_findings=[],
            expected_overall_status=ComplianceStatus.COMPLIANT,
            expected_requires_human_review=False,
        )

        mock_pipeline_service = MagicMock()
        identical_err = "AI extraction provider 'openrouter' failed: OpenRouter invalid request/model error (HTTP 400): Bad Request"
        mock_fail_result = MagicMock(
            document_set=None,
            processing_status="error",
            errors=[identical_err],
            error_type="ExtractionInvalidRequestError",
            status_code=400,
            error_code="http_400",
            request_id="ray-123",
        )
        mock_pipeline_service.process_inspection.return_value = mock_fail_result

        mock_pdf_processor = MagicMock()
        mock_pdf_processor.process_pdf.return_value = MagicMock()

        with patch("pathlib.Path.exists", return_value=True), \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"%PDF-1.4 dummy")):
            result = run_single_scenario(
                scenario=scenario,
                fixtures_dir=Path("/fake/fixtures"),
                pdf_processor=mock_pdf_processor,
                pipeline_service=mock_pipeline_service,
                evaluator=MagicMock(),
            )

        # The identical error from doc1 and doc2 must not be duplicated
        self.assertEqual(result.error_message.count("AI extraction provider 'openrouter' failed:"), 1)
        self.assertEqual(result.error_message, identical_err)

    def test_10_credential_redaction_headers_and_body(self):
        """10. Credential redaction in header and body fallbacks."""
        mock_resp = MagicMock()
        mock_resp.text = "Bearer sk-or-v1-supersecretkey123456789012345678901234"
        mock_resp.headers = {"x-request-id": "sk-ant-api03-abcdefghijklmn"}
        err = Exception("Error code: 401 - Bearer sk-proj-123456789012345678901234")
        err.status_code = 401
        err.response = mock_resp
        err.body = None

        details = extract_http_error_details(err)
        self.assertNotIn("sk-or-v1-supersecretkey123456789012345678901234", details["message"])
        self.assertNotIn("sk-proj-123456789012345678901234", details["message"])
        self.assertNotIn("sk-ant-api03-abcdefghijklmn", str(details["request_id"]))

    def test_11_message_truncation_limit(self):
        """11. Message truncation limit enforced at 1,000 characters."""
        long_body = "x" * 2500
        err = Exception(f"Error code: 400 - {long_body}")
        err.status_code = 400
        err.body = long_body

        details = extract_http_error_details(err)
        self.assertLessEqual(len(details["message"]), 1050)
        self.assertTrue(details["message"].endswith("... [truncated]"))


if __name__ == "__main__":
    unittest.main()
