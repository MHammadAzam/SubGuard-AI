"""Unit tests for GeminiExtractionProvider using mocked SDK responses.

Ensures zero live API calls occur during automated testing while thoroughly verifying:
1. Gemini request construction (model, contents, config, schema, temperature).
2. Structured response validation against RawInsuranceExtraction.
3. Missing fields preserved as None (no hallucinations).
4. Invalid JSON / schema validation error handling.
5. Timeout, rate-limit, and transient error handling with bounded retries.
6. Missing API key handling.
7. Pipeline integration, evidence verification, and uncertainty status mapping.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
from datetime import date

from app.extraction.pipeline import ExtractionPipelineService
from app.extraction.providers.base import (
    ExtractionProviderError,
    ExtractionRateLimitError,
    ExtractionSchemaValidationError,
    ExtractionTimeoutError,
)
from app.extraction.providers.gemini import EXTRACTION_SYSTEM_PROMPT, GeminiExtractionProvider
from app.schemas.insurance import CoverageType, DocumentType, ExtractionStatus
from app.schemas.pdf import PDFDocumentInspection, PDFPageInspection, PDFTextSpan
from app.schemas.raw_extraction import (
    RawCoverageSection,
    RawEntityExtraction,
    RawEvidenceQuote,
    RawInsuranceExtraction,
)


def _make_dummy_page(page_num: int = 1, text: str = "Test text") -> PDFPageInspection:
    return PDFPageInspection(
        page_number=page_num,
        width_pts=612.0,
        height_pts=792.0,
        text_content=text,
        spans=[
            PDFTextSpan(
                text=text,
                page_number=page_num,
                bounding_box={"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.1},
            )
        ],
    )


class TestGeminiExtractionProvider(unittest.TestCase):
    """Test suite for Gemini extraction provider with mocked google-genai SDK."""

    def test_gemini_request_construction(self):
        """Provider must construct contents, system instructions, and schema config accurately."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = RawInsuranceExtraction(
            document_type="coi",
            named_insured=RawEntityExtraction(
                name="Apex Electrical Contractors LLC",
                evidence=RawEvidenceQuote(page_number=1, source_text="Apex Electrical Contractors LLC"),
            ),
        ).model_dump_json()
        mock_client.models.generate_content.return_value = mock_response

        provider = GeminiExtractionProvider(
            api_key="fake-key-for-test",
            model_name="gemini-2.0-flash",
            client=mock_client,
        )

        page = _make_dummy_page(page_num=1, text="INSURED: Apex Electrical Contractors LLC")
        result = provider.extract_insurance_data(document_id="DOC-123", pages=[page])

        # Verify SDK call parameters
        mock_client.models.generate_content.assert_called_once()
        call_kwargs = mock_client.models.generate_content.call_args.kwargs

        self.assertEqual(call_kwargs["model"], "gemini-2.0-flash")
        self.assertIn("PAGE 1", call_kwargs["contents"][0])
        self.assertIn("INSURED: Apex Electrical Contractors LLC", call_kwargs["contents"][0])

        config = call_kwargs["config"]
        self.assertEqual(config.system_instruction, EXTRACTION_SYSTEM_PROMPT)
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertEqual(config.response_schema, RawInsuranceExtraction)
        self.assertEqual(config.temperature, 0.0)

        # Verify result
        self.assertEqual(result.named_insured.name, "Apex Electrical Contractors LLC")

    def test_gemini_missing_fields_preserved_as_none(self):
        """Missing values in model output must remain None rather than hallucinated."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        # Payload with missing / null fields
        mock_response.text = '{"document_type": "coi", "named_insured": null, "coverages": []}'
        mock_client.models.generate_content.return_value = mock_response

        provider = GeminiExtractionProvider(api_key="fake-key", client=mock_client)
        page = _make_dummy_page(1, "Blank document")
        result = provider.extract_insurance_data("DOC-BLANK", [page])

        self.assertIsNone(result.named_insured)
        self.assertEqual(len(result.coverages), 0)

    def test_gemini_invalid_json_raises_schema_validation_error(self):
        """Invalid or malformed JSON from model must raise ExtractionSchemaValidationError."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '{"invalid_json": True, unclosed...}'
        mock_client.models.generate_content.return_value = mock_response

        provider = GeminiExtractionProvider(api_key="fake-key", client=mock_client)
        page = _make_dummy_page(1, "Document")

        with self.assertRaises(ExtractionSchemaValidationError) as ctx:
            provider.extract_insurance_data("DOC-ERR", [page])

        self.assertIn("schema", str(ctx.exception).lower())
        self.assertEqual(ctx.exception.provider_name, "gemini")

    def test_gemini_timeout_handling(self):
        """Timeout from SDK call must raise ExtractionTimeoutError."""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = Exception("Deadline exceeded / request timed out")

        provider = GeminiExtractionProvider(api_key="fake-key", max_retries=1, client=mock_client)
        page = _make_dummy_page(1, "Text")

        with self.assertRaises(ExtractionTimeoutError):
            provider.extract_insurance_data("DOC-TIMEOUT", [page])

    def test_gemini_rate_limiting_handling(self):
        """HTTP 429 / resource exhausted must raise ExtractionRateLimitError."""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = Exception("HTTP 429: Resource exhausted / rate limit")

        provider = GeminiExtractionProvider(api_key="fake-key", max_retries=1, client=mock_client)
        page = _make_dummy_page(1, "Text")

        with self.assertRaises(ExtractionRateLimitError):
            provider.extract_insurance_data("DOC-RATE-LIMIT", [page])

    def test_gemini_missing_api_key_raises_error(self):
        """When no API key is provided and GEMINI_API_KEY is not in env, raise clear provider error."""
        with patch.dict("os.environ", {}, clear=True):
            provider = GeminiExtractionProvider(api_key=None)
            page = _make_dummy_page(1, "Text")
            with self.assertRaises(ExtractionProviderError) as ctx:
                provider.extract_insurance_data("DOC-NO-KEY", [page])
            self.assertIn("API key is not configured", str(ctx.exception))
            self.assertFalse(ctx.exception.is_retryable)

    def test_gemini_pipeline_integration_and_evidence_verification(self):
        """End-to-end integration: Gemini output flows through ExtractionPipelineService."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = RawInsuranceExtraction(
            document_type="coi",
            named_insured=RawEntityExtraction(
                name="Apex Electrical Contractors LLC",
                evidence=RawEvidenceQuote(
                    page_number=1,
                    source_text="INSURED: Apex Electrical Contractors LLC",
                    confidence=0.99,
                ),
                confidence=0.99,
            ),
            coverages=[
                RawCoverageSection(
                    coverage_type="general_liability",
                    policy_number="GL-98765432",
                    each_occurrence="$1,000,000",
                    general_aggregate="$2,000,000",
                    effective_date="01/01/2025",
                    expiration_date="01/01/2026",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="EACH OCCURRENCE: $1,000,000",
                        confidence=0.98,
                    ),
                    confidence=0.98,
                )
            ],
        ).model_dump_json()
        mock_client.models.generate_content.return_value = mock_response

        provider = GeminiExtractionProvider(api_key="fake-key", client=mock_client)
        pipeline = ExtractionPipelineService(extraction_provider=provider)

        doc_text = (
            "INSURED: Apex Electrical Contractors LLC\n"
            "COMMERCIAL GENERAL LIABILITY GL-98765432\n"
            "EACH OCCURRENCE: $1,000,000  GEN AGG: $2,000,000\n"
            "EFF: 01/01/2025  EXP: 01/01/2026"
        )
        page = _make_dummy_page(1, doc_text)
        inspection = PDFDocumentInspection(
            document_id="DOC-INT-01",
            filename="coi.pdf",
            file_size_bytes=1024,
            content_hash_sha256="dummyhash123",
            page_count=1,
            pages=[page],
            classified_type=DocumentType.COI,
            classification_confidence=1.0,
            classification_reason="test inspection",
        )

        res = pipeline.process_inspection(inspection, subcontractor_id="SUB-001")
        self.assertIsNotNone(res.document_set)
        doc_set = res.document_set

        # Verify entity extraction & status
        self.assertEqual(doc_set.named_insured.name, "Apex Electrical Contractors LLC")
        self.assertEqual(doc_set.named_insured.extraction_status, ExtractionStatus.EXTRACTED)
        self.assertIsNotNone(doc_set.named_insured.evidence)
        self.assertEqual(doc_set.named_insured.evidence.page_number, 1)

        # Verify coverage extraction and normalized values
        gl = next(c for c in doc_set.coverages if c.coverage_type == CoverageType.GENERAL_LIABILITY)
        self.assertEqual(gl.each_occurrence.amount_cents, 100_000_000)
        self.assertEqual(gl.general_aggregate.amount_cents, 200_000_000)
        self.assertEqual(gl.effective_date.date, date(2025, 1, 1))
        self.assertEqual(gl.expiration_date.date, date(2026, 1, 1))

    def test_gemini_unverified_quote_marks_status_uncertain(self):
        """When Gemini returns a quotation not found in the PDF, pipeline sets status to UNCERTAIN."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = RawInsuranceExtraction(
            document_type="coi",
            named_insured=RawEntityExtraction(
                name="Apex Electrical Contractors LLC",
                evidence=RawEvidenceQuote(
                    page_number=1,
                    source_text="THIS TEXT IS NOT IN THE PDF FILE",
                    confidence=0.99,
                ),
            ),
        ).model_dump_json()
        mock_client.models.generate_content.return_value = mock_response

        provider = GeminiExtractionProvider(api_key="fake-key", client=mock_client)
        pipeline = ExtractionPipelineService(extraction_provider=provider)

        page = _make_dummy_page(1, "Real text in document")
        inspection = PDFDocumentInspection(
            document_id="DOC-UNVERIFIED",
            filename="coi.pdf",
            file_size_bytes=1024,
            content_hash_sha256="dummyhash456",
            page_count=1,
            pages=[page],
            classified_type=DocumentType.COI,
            classification_confidence=1.0,
            classification_reason="test inspection",
        )

        res = pipeline.process_inspection(inspection, subcontractor_id="SUB-002")
        doc_set = res.document_set
        # Status must be UNCERTAIN because evidence quote was not confirmed on page
        self.assertEqual(doc_set.named_insured.extraction_status, ExtractionStatus.UNCERTAIN)


if __name__ == "__main__":
    unittest.main()
