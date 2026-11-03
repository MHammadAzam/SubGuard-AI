"""Comprehensive tests for the AI-powered insurance data extraction layer."""

import unittest
from datetime import date

from app.compliance.evaluator import ComplianceEvaluator
from app.documents.processor import PDFProcessor
from app.extraction.normalizer import (
    normalize_coverage_type,
    normalize_currency_to_cents,
    normalize_date,
    normalize_endorsement_type,
)
from app.extraction.ocr import MockOCRProvider
from app.extraction.pipeline import ExtractionPipelineService
from app.extraction.providers.mock import MockExtractionProvider
from app.schemas.insurance import (
    ComplianceStatus,
    CoverageType,
    DocumentType,
    EndorsementType,
    InsuranceRequirement,
    LimitField,
    RequirementType,
)
from app.schemas.pdf import (
    BoundingBox,
    DocumentProcessingStatus,
    PDFDocumentInspection,
    PDFPageInspection,
    PDFTextSpan,
)
from app.schemas.raw_extraction import (
    RawCoverageSection,
    RawEndorsement,
    RawEntityExtraction,
    RawEvidenceQuote,
    RawInsuranceExtraction,
)
from tests.test_pdf_processor import create_minimal_pdf_bytes


class TestExtractionLayer(unittest.TestCase):
    def setUp(self):
        self.pdf_processor = PDFProcessor(render_dpi=72)
        self.mock_provider = MockExtractionProvider()
        self.pipeline = ExtractionPipelineService(extraction_provider=self.mock_provider)

    # ========================================================================
    # 1. Normalization Tests
    # ========================================================================

    def test_normalize_currency_variations(self):
        """Valid currency strings should convert precisely to integer cents."""
        test_cases = [
            ("$1,000,000", 100_000_000),
            ("1,000,000", 100_000_000),
            ("$2,000,000.50", 200_000_050),
            ("$500K", 50_000_000),
            ("2M", 200_000_000),
            ("$100.00", 10_000),
        ]
        for raw, expected_cents in test_cases:
            cents, is_amb, reason = normalize_currency_to_cents(raw)
            self.assertFalse(is_amb, f"Failed on {raw}")
            self.assertEqual(cents, expected_cents, f"Mismatch for {raw}")

    def test_normalize_currency_ambiguous_or_split(self):
        """Split limits or malformed amounts must flag ambiguity instead of guessing."""
        cents, is_amb, reason = normalize_currency_to_cents("$1,000,000 / $2,000,000")
        self.assertTrue(is_amb)
        self.assertIsNone(cents)

        cents2, is_amb2, reason2 = normalize_currency_to_cents("unclear amount")
        self.assertTrue(is_amb2)
        self.assertIsNone(cents2)

    def test_normalize_date_variations(self):
        """Standard US and ISO date representations should parse to date objects."""
        test_cases = [
            ("01/15/2025", date(2025, 1, 15)),
            ("1/5/2025", date(2025, 1, 5)),
            ("2025-06-30", date(2025, 6, 30)),
            ("January 15, 2025", date(2025, 1, 15)),
            ("Oct 31, 2026", date(2026, 10, 31)),
        ]
        for raw, expected_date in test_cases:
            parsed, is_amb, reason = normalize_date(raw)
            self.assertFalse(is_amb, f"Failed on {raw}")
            self.assertEqual(parsed, expected_date, f"Mismatch for {raw}")

    def test_normalize_date_out_of_bounds_or_invalid(self):
        """Dates with absurd year ranges or bad formats must flag ambiguity."""
        parsed, is_amb, reason = normalize_date("01/01/1850")
        self.assertTrue(is_amb)

        parsed2, is_amb2, reason2 = normalize_date("not a date")
        self.assertTrue(is_amb2)

    # ========================================================================
    # 2. End-to-End Extraction & Evidence Association
    # ========================================================================

    def test_successful_coi_extraction_with_evidence(self):
        """Extraction pipeline extracts named insured, limits, dates, and links evidence spans."""
        coi_lines = [
            "ACORD 25 (2016/03) CERTIFICATE OF LIABILITY INSURANCE",
            "PRODUCER: Apex Insurance Agency",
            "INSURED: Apex Electrical Contractors LLC",
            "GENERAL LIABILITY: EACH OCCURRENCE $1,000,000",
            "GENERAL AGGREGATE: $2,000,000",
            "POLICY EFF: 01/01/2025  POLICY EXP: 01/01/2026",
        ]
        pdf_bytes = create_minimal_pdf_bytes(coi_lines)
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="doc_coi_test")

        result = self.pipeline.process_inspection(inspection, subcontractor_id="sub_apex_01")

        self.assertEqual(result.processing_status, DocumentProcessingStatus.PROCESSED)
        self.assertFalse(result.requires_ocr)
        self.assertIsNotNone(result.document_set)

        doc_set = result.document_set
        # Named Insured
        self.assertIsNotNone(doc_set.named_insured)
        self.assertEqual(doc_set.named_insured.name, "Apex Electrical Contractors LLC")
        self.assertIsNotNone(doc_set.named_insured.evidence)
        self.assertEqual(doc_set.named_insured.evidence.page_number, 1)

        # Coverage limits & dates
        self.assertEqual(len(doc_set.coverages), 1)
        cov = doc_set.coverages[0]
        self.assertEqual(cov.coverage_type, CoverageType.GENERAL_LIABILITY)
        self.assertEqual(cov.each_occurrence.amount_cents, 100_000_000)
        self.assertEqual(cov.general_aggregate.amount_cents, 200_000_000)
        self.assertEqual(cov.effective_date.date, date(2025, 1, 1))
        self.assertEqual(cov.expiration_date.date, date(2026, 1, 1))

        # Endorsements
        self.assertEqual(len(doc_set.endorsements), 1)
        self.assertEqual(doc_set.endorsements[0].endorsement_type, EndorsementType.ADDITIONAL_INSURED)
        self.assertEqual(doc_set.endorsements[0].form_number, "CG 20 10 04 13")

    def test_missing_fields_remain_null_without_fabrication(self):
        """Missing coverage fields or entity addresses must remain None."""
        sparse_raw = RawInsuranceExtraction(
            document_type="coi",
            named_insured=RawEntityExtraction(
                name="Minimal Contracting LLC",
                address=None,
                evidence=RawEvidenceQuote(page_number=1, source_text="Minimal Contracting LLC"),
            ),
            coverages=[
                RawCoverageSection(
                    coverage_type="auto_liability",
                    policy_number=None,
                    combined_single_limit="$1,000,000",
                    each_occurrence=None,
                    effective_date=None,
                    expiration_date=None,
                    evidence=RawEvidenceQuote(page_number=1, source_text="AUTO CSL $1M"),
                )
            ],
        )

        custom_pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=sparse_raw)
        )
        fake_inspection = PDFDocumentInspection(
            document_id="doc_sparse",
            filename="sparse.pdf",
            file_size_bytes=1000,
            content_hash_sha256="abc123hash",
            page_count=1,
            classified_type=DocumentType.COI,
            classification_confidence=0.95,
            classification_reason="COI header",
            pages=[
                PDFPageInspection(
                    page_number=1,
                    width_pts=612,
                    height_pts=792,
                    text_content="Sparse text",
                )
            ],
        )

        result = custom_pipeline.process_inspection(fake_inspection, subcontractor_id="sub_sparse")
        doc_set = result.document_set

        self.assertIsNone(doc_set.named_insured.address)
        self.assertIsNone(doc_set.coverages[0].policy_number)
        self.assertIsNone(doc_set.coverages[0].each_occurrence)
        self.assertIsNone(doc_set.coverages[0].effective_date)
        self.assertEqual(doc_set.coverages[0].combined_single_limit.amount_cents, 100_000_000)

    def test_invalid_evidence_page_number_rejected_without_crashing(self):
        """Evidence quoting page 99 on a 1-page document must be rejected from evidence reference."""
        bad_evidence_raw = RawInsuranceExtraction(
            document_type="coi",
            named_insured=RawEntityExtraction(
                name="Hallucinated Page Inc",
                evidence=RawEvidenceQuote(page_number=99, source_text="Hallucinated text on page 99"),
            ),
        )
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=bad_evidence_raw)
        )
        inspection = PDFDocumentInspection(
            document_id="doc_1p",
            filename="test.pdf",
            file_size_bytes=500,
            content_hash_sha256="hash123",
            page_count=1,
            classified_type=DocumentType.COI,
            classification_confidence=0.9,
            classification_reason="COI",
            pages=[PDFPageInspection(page_number=1, width_pts=612, height_pts=792, text_content="Text")],
        )

        result = pipeline.process_inspection(inspection, subcontractor_id="sub_test")
        # Evidence was rejected because page 99 does not exist
        self.assertIsNone(result.document_set.named_insured.evidence)

    # ========================================================================
    # 3. Provider Failure & Robustness Handling
    # ========================================================================

    def test_provider_timeout_handled_gracefully(self):
        """Provider timeout returns FAILED status without crashing pipeline."""
        timeout_pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(simulate_timeout=True)
        )
        inspection = self.pdf_processor.process_pdf(
            create_minimal_pdf_bytes(["Test line"]), document_id="doc_timeout"
        )

        result = timeout_pipeline.process_inspection(inspection, subcontractor_id="sub_timeout")
        self.assertEqual(result.processing_status, DocumentProcessingStatus.FAILED)
        self.assertIsNone(result.document_set)
        self.assertTrue(any("timed out" in e.lower() for e in result.errors))

    def test_provider_rate_limit_handled_gracefully(self):
        """Provider rate limit returns FAILED status with clear message."""
        rate_limit_pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(simulate_rate_limit=True)
        )
        inspection = self.pdf_processor.process_pdf(
            create_minimal_pdf_bytes(["Test line"]), document_id="doc_rate_limit"
        )

        result = rate_limit_pipeline.process_inspection(inspection, subcontractor_id="sub_rate_limit")
        self.assertEqual(result.processing_status, DocumentProcessingStatus.FAILED)
        self.assertTrue(any("rate limit" in e.lower() for e in result.errors))

    def test_provider_schema_malformed_response_handled_gracefully(self):
        """Malformed provider JSON returns FAILED status with diagnostic error."""
        malformed_pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(simulate_malformed=True)
        )
        inspection = self.pdf_processor.process_pdf(
            create_minimal_pdf_bytes(["Test line"]), document_id="doc_malformed"
        )

        result = malformed_pipeline.process_inspection(inspection, subcontractor_id="sub_malformed")
        self.assertEqual(result.processing_status, DocumentProcessingStatus.FAILED)
        self.assertTrue(any("malformed" in e.lower() for e in result.errors))

    # ========================================================================
    # 4. Scanned Document & OCR Routing
    # ========================================================================

    def test_scanned_pdf_without_ocr_provider_returns_requires_ocr(self):
        """Scanned PDF with no OCR provider configured must return requires_ocr status."""
        import pymupdf
        doc = pymupdf.open()
        doc.new_page(width=612, height=792)  # Blank page / image only
        scanned_bytes = doc.tobytes()
        doc.close()

        scanned_inspection = self.pdf_processor.process_pdf(scanned_bytes, document_id="doc_scanned")
        self.assertTrue(scanned_inspection.is_scanned)

        result = self.pipeline.process_inspection(scanned_inspection, subcontractor_id="sub_scanned")
        self.assertEqual(result.processing_status, DocumentProcessingStatus.REQUIRES_OCR)
        self.assertTrue(result.requires_ocr)
        self.assertIsNone(result.document_set)

    def test_scanned_pdf_with_mock_ocr_transcribes_and_extracts(self):
        """Scanned PDF routed through MockOCRProvider populates text and extracts cleanly."""
        import pymupdf
        doc = pymupdf.open()
        doc.new_page(width=612, height=792)
        scanned_bytes = doc.tobytes()
        doc.close()

        scanned_inspection = self.pdf_processor.process_pdf(scanned_bytes, document_id="doc_scanned_ocr")
        ocr_pipeline = ExtractionPipelineService(
            extraction_provider=self.mock_provider,
            ocr_provider=MockOCRProvider(),
        )

        result = ocr_pipeline.process_inspection(scanned_inspection, subcontractor_id="sub_scanned_success")
        self.assertEqual(result.processing_status, DocumentProcessingStatus.PROCESSED)
        self.assertFalse(result.requires_ocr)
        self.assertIsNotNone(result.document_set)
        self.assertEqual(result.document_set.named_insured.name, "Apex Electrical Contractors LLC")

    # ========================================================================
    # 5. Integration with Existing Compliance Evaluator
    # ========================================================================

    def test_extracted_document_set_evaluates_in_compliance_evaluator(self):
        """ExtractedDocumentSet produced by pipeline feeds directly into ComplianceEvaluator without friction."""
        coi_lines = [
            "CERTIFICATE OF LIABILITY INSURANCE",
            "INSURED: Apex Electrical Contractors LLC",
            "GENERAL LIABILITY: EACH OCCURRENCE $1,000,000",
        ]
        pdf_bytes = create_minimal_pdf_bytes(coi_lines)
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="doc_e2e_coi")

        pipeline_result = self.pipeline.process_inspection(inspection, subcontractor_id="sub_e2e")
        doc_set = pipeline_result.document_set

        # Define requirements
        req_gl = InsuranceRequirement(
            requirement_id="REQ_GL_EACH_OCC",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="General Liability $1M required",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            limit_field=LimitField.EACH_OCCURRENCE,
            min_amount_cents=100_000_000,
        )
        req_ni = InsuranceRequirement(
            requirement_id="REQ_NI",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured match",
            required_entity_name="Apex Electrical Contractors LLC",
        )

        evaluator = ComplianceEvaluator()
        review_result = evaluator.evaluate(
            document_set=doc_set,
            requirements=[req_gl, req_ni],
            evaluation_date=date(2025, 6, 15),
        )

        self.assertEqual(review_result.overall_status, ComplianceStatus.COMPLIANT)
        self.assertFalse(review_result.requires_human_review)
        self.assertEqual(len(review_result.findings), 2)
        self.assertEqual(review_result.findings[0].status, ComplianceStatus.COMPLIANT)
        self.assertEqual(review_result.findings[1].status, ComplianceStatus.COMPLIANT)


if __name__ == "__main__":
    unittest.main()
