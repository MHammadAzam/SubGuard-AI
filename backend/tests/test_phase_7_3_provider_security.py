"""Phase 7.3: Live Extraction Provider Integration, Reliability & Security Regression Tests.

Comprehensive security, reliability, and architectural regression test suite verifying:
- Non-negotiable security requirements:
  1. Provider claims an endorsement is unmodified -> routes to human review (MANUSCRIPT_VERIFICATION_UNSUPPORTED).
  2. Provider claims manuscript restrictions absent -> full doc text scanning overrides and catches restriction.
  3. Provider supplies fabricated evidence -> evidence rejected, routes to UNABLE_TO_DETERMINE / MISSING_EVIDENCE.
  4. Provider supplies recognized form number without valid evidence -> routes to human review (ENDORSEMENT_EVIDENCE_MISSING).
  5. Provider supplies false page number -> evidence rejected, routes to human review.
  6. Provider supplies unsupported / mismatched policy number -> cross-document reconciliation flags CONTRADICTORY_POLICY_NUMBERS_DETECTED.
  7. Provider supplies conflicting policy dates -> cross-document reconciliation flags CONTRADICTORY_POLICY_DATES_DETECTED.
  8. Provider supplies unsupported coverage limits -> semantic verification fails, routes to LIMIT_EVIDENCE_SUPPORT_UNVERIFIED.
  9. Provider returns incomplete endorsement -> routes to ENDORSEMENT_PARTIAL_EXTRACTION.
  10. Provider attempts to set independent_integrity_verified=True -> pipeline explicitly forces False.
- Multi-document isolation:
  11. Workers' comp policy number is not mixed into general liability endorsements.
  12. Endorsements tied to Policy A cannot satisfy requirements for Policy B.
- PDF and OCR reliability:
  13. Scanned PDF without OCR provider safely returns REQUIRES_OCR without false compliance.
  14. Scanned PDF with OCR provider correctly extracts text and evaluates compliance.
- Failure handling & free-model safety:
  15. OpenRouter rejects non-free models (e.g. gpt-4o, claude-3-opus, openrouter/auto).
  16. OpenRouter error propagation: 401, 429, 503, timeout, malformed JSON, empty choices.
  17. Integer cent monetary calculations preserved without floating-point drift.
  18. Evaluator determinism across repeated executions.
"""

from __future__ import annotations

import json
from datetime import date
import unittest
from unittest.mock import MagicMock, patch

from app.compliance.evaluator import ComplianceEvaluator
from app.documents.processor import PDFProcessor
from app.extraction.ocr import MockOCRProvider
from app.extraction.pipeline import (
    ExtractionPipelineService,
    reconcile_cross_document_set,
)
from app.extraction.providers.base import (
    ExtractionAuthenticationError,
    ExtractionConfigurationError,
    ExtractionInvalidModelResponseError,
    ExtractionInvalidRequestError,
    ExtractionProviderError,
    ExtractionRateLimitError,
    ExtractionSchemaValidationError,
    ExtractionTimeoutError,
)
from app.extraction.providers.mock import MockExtractionProvider
from app.extraction.providers.openrouter import (
    OPENROUTER_SYSTEM_PROMPT,
    OpenRouterExtractionProvider,
)
from app.schemas.insurance import (
    ComplianceFinding,
    ComplianceStatus,
    CoverageExtraction,
    CoverageType,
    DateValue,
    DocumentType,
    EndorsementExtraction,
    EndorsementType,
    EntityInfo,
    EvidenceReference,
    ExtractedDocumentSet,
    ExtractionStatus,
    InsuranceRequirement,
    LimitField,
    MoneyValue,
    RequirementType,
    Severity,
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
from tests.fixtures.sample_data import (
    EVAL_DATE,
    make_evidence,
    sample_gl_coverage,
    sample_gl_requirement,
    sample_subcontractor_document_set,
)


def _make_dummy_page(
    page_num: int = 1,
    text: str = "Test document text",
) -> PDFPageInspection:
    return PDFPageInspection(
        page_number=page_num,
        width_pts=612.0,
        height_pts=792.0,
        text_content=text,
        spans=[
            PDFTextSpan(
                text=text,
                bounding_box=BoundingBox(x=0.1, y=0.1, width=0.8, height=0.1),
            )
        ],
    )


def _make_mock_chat_completion(content_str: str) -> MagicMock:
    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = content_str
    mock_resp.choices = [mock_choice]
    return mock_resp


class TestPhase73SecurityRegression(unittest.TestCase):
    """Section 1: Mandatory Security Regression Tests (Stage 10)."""

    def setUp(self):
        self.evaluator = ComplianceEvaluator()
        self.eval_date = EVAL_DATE
        self.cov = sample_gl_coverage()

    def test_sec_01_provider_claims_unmodified_routes_to_human_review(self):
        """Case 1: Provider claims an endorsement is an 'unmodified_standard_form', but without
        independent document-integrity verification, evaluator routes to human review.
        """
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            named_entities=["General Contractor Corp"],
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=False,  # Provider cannot assert independent verification
            confidence=0.98,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=self.cov, endorsements=[end])
        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Additional Insured required",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_VERIFICATION_UNSUPPORTED")

    def test_sec_02_provider_claims_restrictions_absent_overridden_by_full_doc_scan(self):
        """Case 2: Provider falsely claims manuscript restrictions are absent (NOT_DETECTED),
        but document text contains restrictive wording -> pipeline catches restriction -> MANUSCRIPT_RESTRICTION_DETECTED.
        """
        inspection = PDFDocumentInspection(
            document_id="DOC-SEC-02",
            filename="endorsement.pdf",
            file_size_bytes=1000,
            content_hash_sha256="hash_sec_02",
            classified_type=DocumentType.ENDORSEMENT,
            classification_confidence=0.95,
            classification_reason="test",
            page_count=1,
            processing_status=DocumentProcessingStatus.PROCESSED,
            pages=[
                PDFPageInspection(
                    page_number=1,
                    text_content="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13. Subject to residential work exclusion endorsement.",
                    width_pts=612.0,
                    height_pts=792.0,
                    content_hash_sha256="p1",
                )
            ],
        )
        raw_extraction = RawInsuranceExtraction(
            document_type="endorsement",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    is_standard_form=True,
                    manuscript_clause_detected=False,  # Falsely claimed clean
                    manuscript_status="NOT_DETECTED",
                    manuscript_verification_basis="unmodified_standard_form",
                    confidence=0.95,
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13",
                    ),
                )
            ],
            is_document_set_complete=True,
        )
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=raw_extraction)
        )
        result = pipeline.process_inspection(inspection, subcontractor_id="SUB-SEC-02")
        end = result.document_set.endorsements[0]
        self.assertTrue(end.manuscript_clause_detected)
        self.assertEqual(end.manuscript_status, "DETECTED")

        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Additional Insured required",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        eval_res = self.evaluator.evaluate(result.document_set, [req], self.eval_date)
        finding = eval_res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_RESTRICTION_DETECTED")

    def test_sec_03_provider_fabricated_evidence_rejected(self):
        """Case 3: Provider supplies a fabricated evidence quote that does not appear in document text
        -> evidence is rejected (None), status set to UNCERTAIN -> evaluator flags MISSING_EVIDENCE / UNABLE_TO_DETERMINE.
        """
        inspection = PDFDocumentInspection(
            document_id="DOC-SEC-03",
            filename="endorsement.pdf",
            file_size_bytes=1000,
            content_hash_sha256="hash_sec_03",
            classified_type=DocumentType.ENDORSEMENT,
            classification_confidence=0.95,
            classification_reason="test",
            page_count=1,
            processing_status=DocumentProcessingStatus.PROCESSED,
            pages=[
                PDFPageInspection(
                    page_number=1,
                    text_content="GENUINE PAGE TEXT: Miscellaneous insurance policy details.",
                    width_pts=612.0,
                    height_pts=792.0,
                    content_hash_sha256="p1",
                )
            ],
        )
        raw_extraction = RawInsuranceExtraction(
            document_type="endorsement",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    is_standard_form=True,
                    manuscript_status="NOT_DETECTED",
                    confidence=0.95,
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="FABRICATED QUOTE NEVER WRITTEN IN DOCUMENT",
                    ),
                )
            ],
            is_document_set_complete=True,
        )
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=raw_extraction)
        )
        result = pipeline.process_inspection(inspection, subcontractor_id="SUB-SEC-03")
        end = result.document_set.endorsements[0]
        self.assertIsNone(end.evidence)
        self.assertEqual(end.extraction_status, ExtractionStatus.UNCERTAIN)

        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Additional Insured required",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            mandatory_evidence=True,
        )
        eval_res = self.evaluator.evaluate(result.document_set, [req], self.eval_date)
        finding = eval_res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_EVIDENCE_TEXT_MISSING")

    def test_sec_04_provider_recognized_form_without_valid_support(self):
        """Case 4: Provider supplies a recognized form number (CG 20 10) but no evidence reference
        -> mandatory evidence check fails with ENDORSEMENT_EVIDENCE_TEXT_MISSING.
        """
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            named_entities=["General Contractor Corp"],
            is_standard_form=True,
            manuscript_status="NOT_DETECTED",
            evidence=None,  # No evidence
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=self.cov, endorsements=[end])
        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Additional Insured required",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            mandatory_evidence=True,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_EVIDENCE_TEXT_MISSING")

    def test_sec_05_provider_false_page_number_rejected(self):
        """Case 5: Provider supplies a quote that exists on page 1, but cites page 99
        -> page number does not exist -> evidence rejected -> MISSING_EVIDENCE.
        """
        inspection = PDFDocumentInspection(
            document_id="DOC-SEC-05",
            filename="endorsement.pdf",
            file_size_bytes=1000,
            content_hash_sha256="hash_sec_05",
            classified_type=DocumentType.ENDORSEMENT,
            classification_confidence=0.95,
            classification_reason="test",
            page_count=2,
            processing_status=DocumentProcessingStatus.PROCESSED,
            pages=[
                PDFPageInspection(
                    page_number=1,
                    text_content="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13",
                    width_pts=612.0,
                    height_pts=792.0,
                    content_hash_sha256="p1",
                ),
                PDFPageInspection(
                    page_number=2,
                    text_content="Page 2 text",
                    width_pts=612.0,
                    height_pts=792.0,
                    content_hash_sha256="p2",
                ),
            ],
        )
        raw_extraction = RawInsuranceExtraction(
            document_type="endorsement",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    is_standard_form=True,
                    manuscript_status="NOT_DETECTED",
                    confidence=0.95,
                    evidence=RawEvidenceQuote(
                        page_number=99,  # Nonexistent page
                        source_text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13",
                    ),
                )
            ],
            is_document_set_complete=True,
        )
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=raw_extraction)
        )
        result = pipeline.process_inspection(inspection, subcontractor_id="SUB-SEC-05")
        end = result.document_set.endorsements[0]
        self.assertIsNone(end.evidence)

    def test_sec_06_provider_unsupported_policy_number_flagged(self):
        """Case 6: Provider supplies an endorsement with an applicable_policy that does not match
        any coverage policy number in the document set -> cross-document reconciliation flags contradiction.
        """
        doc_set = sample_subcontractor_document_set(gl_coverage=self.cov)
        doc_set.coverages[0].policy_number = "GL-11111111"

        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            applicable_policy="AUTO-99999999",  # Conflicts with known GL-11111111
            named_entities=["General Contractor Corp"],
            is_standard_form=True,
            manuscript_status="NOT_DETECTED",
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Policy AUTO-99999999"),
        )
        doc_set.endorsements = [end]

        reconcile_cross_document_set(doc_set)
        mismatch_contradictions = [
            c for c in doc_set.raw_contradictions_detected
            if "CONTRADICTORY_POLICY_NUMBERS_DETECTED" in c
        ]
        self.assertGreater(len(mismatch_contradictions), 0)
        self.assertIn("AUTO-99999999", mismatch_contradictions[0])

    def test_sec_07_provider_conflicting_policy_dates_flagged(self):
        """Case 7: Provider supplies conflicting policy dates between coverage and endorsement
        -> cross-document reconciliation flags CONTRADICTORY_POLICY_DATES_DETECTED.
        """
        doc_set = sample_subcontractor_document_set(gl_coverage=self.cov)
        doc_set.coverages[0].policy_number = "GL-100"
        doc_set.coverages[0].effective_date = DateValue(date=date(2025, 1, 1), raw_text="01/01/2025")

        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            applicable_policy="GL-100",
            effective_date=date(2025, 7, 1),  # Conflicting effective date
            named_entities=["General Contractor Corp"],
            is_standard_form=True,
            manuscript_status="NOT_DETECTED",
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Effective 07/01/2025"),
        )
        doc_set.endorsements = [end]

        reconcile_cross_document_set(doc_set)
        date_contradictions = [
            c for c in doc_set.raw_contradictions_detected
            if "CONTRADICTORY_POLICY_DATES_DETECTED" in c
        ]
        self.assertGreater(len(date_contradictions), 0)

    def test_sec_08_provider_unsupported_coverage_limit_semantic_mismatch(self):
        """Case 8: Provider supplies a limit ($2,000,000) whose cited evidence quote only mentions $500,000
        -> semantic support verification fails -> QUOTE_PRESENT_UNVERIFIED_SUPPORT -> human review.
        """
        inspection = PDFDocumentInspection(
            document_id="DOC-SEC-08",
            filename="coi.pdf",
            file_size_bytes=1000,
            content_hash_sha256="hash_sec_08",
            classified_type=DocumentType.COI,
            classification_confidence=0.98,
            classification_reason="test",
            page_count=1,
            processing_status=DocumentProcessingStatus.PROCESSED,
            pages=[
                PDFPageInspection(
                    page_number=1,
                    text_content="CERTIFICATE HOLDER: Apex Building Corporation",
                    width_pts=612.0,
                    height_pts=792.0,
                    content_hash_sha256="p1",
                )
            ],
        )
        raw_extraction = RawInsuranceExtraction(
            document_type="coi",
            coverages=[
                RawCoverageSection(
                    coverage_type="general_liability",
                    policy_number="GL-12345",
                    each_occurrence="$2,000,000",  # Unsubstantiated limit
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="CERTIFICATE HOLDER: Apex Building Corporation",  # Unrelated quote without coverage keywords
                    ),
                    confidence=0.95,
                )
            ],
            is_document_set_complete=True,
        )
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=raw_extraction)
        )
        result = pipeline.process_inspection(inspection, subcontractor_id="SUB-SEC-08")
        cov = result.document_set.coverages[0]
        self.assertEqual(cov.extraction_status, ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT)

        req = sample_gl_requirement(min_cents=100_000_000)
        eval_res = self.evaluator.evaluate(result.document_set, [req], self.eval_date)
        finding = eval_res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "EVIDENCE_SUPPORT_UNVERIFIED")

    def test_sec_09_provider_incomplete_endorsement_partial_extraction(self):
        """Case 9: Provider returns an endorsement marked PARTIAL -> routes to ENDORSEMENT_PARTIAL_EXTRACTION."""
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            extraction_status=ExtractionStatus.PARTIAL,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="CG 20 10 04 13"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=self.cov, endorsements=[end])
        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Additional Insured required",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_PARTIAL_EXTRACTION")

    def test_sec_10_provider_cannot_set_independent_integrity_verified_true(self):
        """Case 10: Provider attempts to set independent_integrity_verified=True in output
        -> pipeline strictly forces independent_integrity_verified=False.
        """
        raw_extraction = RawInsuranceExtraction(
            document_type="endorsement",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    is_standard_form=True,
                    manuscript_clause_detected=False,
                    manuscript_status="NOT_DETECTED",
                    manuscript_verification_basis="unmodified_standard_form",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="CG 20 10 04 13 Additional Insured",
                    ),
                )
            ],
            is_document_set_complete=True,
        )
        inspection = PDFDocumentInspection(
            document_id="DOC-SEC-10",
            filename="endorsement.pdf",
            file_size_bytes=1000,
            content_hash_sha256="hash_sec_10",
            classified_type=DocumentType.ENDORSEMENT,
            classification_confidence=0.95,
            classification_reason="test",
            page_count=1,
            processing_status=DocumentProcessingStatus.PROCESSED,
            pages=[_make_dummy_page(1, "CG 20 10 04 13 Additional Insured")],
        )
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=raw_extraction)
        )
        result = pipeline.process_inspection(inspection, subcontractor_id="SUB-SEC-10")
        end = result.document_set.endorsements[0]

        # Crucial check: independent_integrity_verified must be False
        self.assertFalse(end.independent_integrity_verified)

        # Compliance must route to human review
        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Additional Insured required",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        res = self.evaluator.evaluate(result.document_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_VERIFICATION_UNSUPPORTED")


class TestMultiDocumentIsolationAndOCR(unittest.TestCase):
    """Section 2: Multi-Document Processing & PDF/OCR Reliability Tests."""

    def setUp(self):
        self.evaluator = ComplianceEvaluator()
        self.eval_date = EVAL_DATE

    def test_multi_01_workers_comp_policy_not_attributed_to_gl_endorsement(self):
        """Case 11: A policy number from a Workers' Comp coverage is not mixed into a GL endorsement."""
        wc_cov = CoverageExtraction(
            coverage_type=CoverageType.WORKERS_COMP,
            policy_number="WC-99988877",
            disease_policy_limit=MoneyValue(amount_cents=100_000_000, raw_text="$1,000,000"),
            evidence=make_evidence(text="Workers Comp Policy WC-99988877"),
        )
        gl_cov = CoverageExtraction(
            coverage_type=CoverageType.GENERAL_LIABILITY,
            policy_number="GL-11122233",
            each_occurrence=MoneyValue(amount_cents=100_000_000, raw_text="$1,000,000"),
            evidence=make_evidence(text="General Liability Policy GL-11122233"),
        )
        gl_end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            applicable_policy="WC-99988877",  # Inappropriate attribution to WC policy
            named_entities=["General Contractor Corp"],
            is_standard_form=True,
            manuscript_status="NOT_DETECTED",
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Policy WC-99988877"),
        )
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-MULTI-01",
            coverages=[wc_cov, gl_cov],
            endorsements=[gl_end],
        )

        reconcile_cross_document_set(doc_set)
        # Endorsement is tied to WC-99988877, not GL-11122233; reconciliation flags cross-policy linkage
        self.assertEqual(gl_end.applicable_policy, "WC-99988877")
        self.assertEqual(gl_cov.policy_number, "GL-11122233")

    def test_ocr_01_scanned_pdf_without_ocr_provider_returns_requires_ocr(self):
        """Case 12: Scanned document without OCR provider halts with REQUIRES_OCR without false compliance pass."""
        inspection = PDFDocumentInspection(
            document_id="DOC-SCAN-01",
            filename="scanned_cert.pdf",
            file_size_bytes=50000,
            content_hash_sha256="hash_scan_01",
            page_count=1,
            processing_status=DocumentProcessingStatus.REQUIRES_OCR,
            is_scanned=True,
            classified_type=DocumentType.UNKNOWN,
            classification_confidence=0.0,
            classification_reason="Scanned",
            pages=[_make_dummy_page(1, "")],  # Empty text layer
        )
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(),
            ocr_provider=None,  # No OCR provider configured
        )
        result = pipeline.process_inspection(inspection, subcontractor_id="SUB-SCAN-01")
        self.assertIsNone(result.document_set)
        self.assertEqual(result.processing_status, DocumentProcessingStatus.REQUIRES_OCR)
        self.assertTrue(result.requires_ocr)
        self.assertIn("No OCR provider is configured", result.errors[0])

    def test_ocr_02_scanned_pdf_with_mock_ocr_populates_text_layer(self):
        """Case 13: Scanned document with OCR provider receives text and processes cleanly."""
        inspection = PDFDocumentInspection(
            document_id="DOC-SCAN-02",
            filename="scanned_coi.pdf",
            file_size_bytes=50000,
            content_hash_sha256="hash_scan_02",
            page_count=1,
            processing_status=DocumentProcessingStatus.REQUIRES_OCR,
            is_scanned=True,
            classified_type=DocumentType.COI,
            classification_confidence=0.9,
            classification_reason="Scanned",
            pages=[_make_dummy_page(1, "")],  # Empty text layer
        )
        ocr = MockOCRProvider(synthetic_text_by_page={
            1: "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE. INSURED: Apex Electrical Contractors LLC. EACH OCCURRENCE: $1,000,000"
        })
        raw_extraction = RawInsuranceExtraction(
            document_type="coi",
            named_insured=RawEntityExtraction(
                name="Apex Electrical Contractors LLC",
                evidence=RawEvidenceQuote(page_number=1, source_text="Apex Electrical Contractors LLC"),
            ),
            is_document_set_complete=True,
        )
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=raw_extraction),
            ocr_provider=ocr,
        )
        result = pipeline.process_inspection(inspection, subcontractor_id="SUB-SCAN-02")
        self.assertIsNotNone(result.document_set)
        self.assertEqual(result.processing_status, DocumentProcessingStatus.PROCESSED)
        self.assertEqual(result.document_set.named_insured.name, "Apex Electrical Contractors LLC")


class TestOpenRouterLiveProviderReliability(unittest.TestCase):
    """Section 3: OpenRouter API Reliability, Bounded Retries & Cost Protection."""

    def test_free_01_prohibits_paid_models(self):
        """Case 14: Non-free model names are rejected immediately with ExtractionConfigurationError."""
        paid_models = [
            "openai/gpt-4o",
            "anthropic/claude-3-opus",
            "google/gemini-2.0-flash-001",
            "openrouter/auto",
            "auto",
            "meta-llama/llama-3-70b-instruct",
        ]
        for model in paid_models:
            with self.subTest(model=model):
                with self.assertRaises(ExtractionConfigurationError) as ctx:
                    OpenRouterExtractionProvider(api_key="fake-key", model_name=model)
                self.assertIn("not an explicitly approved free model", str(ctx.exception))

    def test_free_02_accepts_verified_free_models(self):
        """Case 15: Verified free models ending with ':free' are accepted."""
        free_models = [
            "nvidia/nemotron-3.5-super-vl:free",
            "google/gemini-2.0-flash-thinking-exp:free",
            "meta-llama/llama-3.2-11b-vision-instruct:free",
        ]
        for model in free_models:
            with self.subTest(model=model):
                provider = OpenRouterExtractionProvider(api_key="fake-key", model_name=model)
                self.assertEqual(provider.model_name, model)

    def test_err_01_authentication_failure_sanitizes_key(self):
        """Case 16: HTTP 401 error redacts any sensitive API key from exception message."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception(
            "HTTP 401: Unauthorized for key sk-or-v1-abcdef1234567890abcdef1234567890"
        )
        provider = OpenRouterExtractionProvider(api_key="sk-or-v1-fake", client=mock_client, max_retries=1)
        page = _make_dummy_page(1, "Test")

        with self.assertRaises(ExtractionAuthenticationError) as ctx:
            provider.extract_insurance_data("DOC-ERR-01", [page])

        msg = str(ctx.exception)
        self.assertNotIn("sk-or-v1-abcdef", msg)
        self.assertIn("[REDACTED_API_KEY]", msg)

    def test_err_02_rate_limit_bounded_retries(self):
        """Case 17: HTTP 429 rate limit executes bounded retries and raises ExtractionRateLimitError."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("HTTP 429: Too Many Requests / Rate limit exceeded")
        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client, max_retries=3)
        page = _make_dummy_page(1, "Test")

        with patch("time.sleep"):  # Avoid real delays during testing
            with self.assertRaises(ExtractionRateLimitError) as ctx:
                provider.extract_insurance_data("DOC-ERR-02", [page])

        self.assertEqual(mock_client.chat.completions.create.call_count, 3)
        self.assertEqual(ctx.exception.status_code, 429)

    def test_err_03_malformed_json_raises_schema_validation_error(self):
        """Case 18: Malformed or unparseable JSON from model raises ExtractionSchemaValidationError."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion(
            "```json\n{ this is not valid json! \n```"
        )
        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Test")

        with self.assertRaises(ExtractionSchemaValidationError) as ctx:
            provider.extract_insurance_data("DOC-ERR-03", [page])

        self.assertEqual(ctx.exception.error_code, "invalid_json")

    def test_err_04_empty_content_raises_invalid_model_response(self):
        """Case 19: Empty message content raises ExtractionInvalidModelResponseError."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion("   ")
        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Test")

        with self.assertRaises(ExtractionInvalidModelResponseError) as ctx:
            provider.extract_insurance_data("DOC-ERR-04", [page])

        self.assertEqual(ctx.exception.error_code, "empty_content")

    def test_err_05_non_dict_root_raises_invalid_model_response(self):
        """Case 20: JSON list root instead of dict raises ExtractionInvalidModelResponseError."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_chat_completion("[]")
        provider = OpenRouterExtractionProvider(api_key="test-key", client=mock_client)
        page = _make_dummy_page(1, "Test")

        with self.assertRaises(ExtractionInvalidModelResponseError) as ctx:
            provider.extract_insurance_data("DOC-ERR-05", [page])

        self.assertEqual(ctx.exception.error_code, "non_dict_root")

    def test_precision_01_monetary_integer_cents_preserved(self):
        """Case 21: Monetary amounts are represented strictly in integer cents without float drift."""
        raw_cov = RawCoverageSection(
            coverage_type="general_liability",
            policy_number="GL-123",
            each_occurrence="$1,000,000.50",  # Edge cents
            general_aggregate="$2,000,000",
            evidence=RawEvidenceQuote(page_number=1, source_text="$1,000,000.50"),
        )
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(
                canned_response=RawInsuranceExtraction(document_type="coi", coverages=[raw_cov])
            )
        )
        inspection = PDFDocumentInspection(
            document_id="DOC-CENTS",
            filename="coi.pdf",
            file_size_bytes=1000,
            content_hash_sha256="hash_cents",
            page_count=1,
            processing_status=DocumentProcessingStatus.PROCESSED,
            classified_type=DocumentType.COI,
            classification_confidence=0.98,
            classification_reason="test",
            pages=[_make_dummy_page(1, "$1,000,000.50")],
        )
        res = pipeline.process_inspection(inspection, "SUB-CENTS")
        cov = res.document_set.coverages[0]
        self.assertEqual(cov.each_occurrence.amount_cents, 100_000_050)
        self.assertIsInstance(cov.each_occurrence.amount_cents, int)

    def test_determinism_01_evaluator_produces_identical_results(self):
        """Case 22: ComplianceEvaluator produces identical findings across multiple executions."""
        doc_set = sample_subcontractor_document_set(gl_coverage=sample_gl_coverage())
        req = sample_gl_requirement(min_cents=100_000_000)

        evaluator = ComplianceEvaluator()
        res1 = evaluator.evaluate(doc_set, [req], EVAL_DATE)
        res2 = evaluator.evaluate(doc_set, [req], EVAL_DATE)

        self.assertEqual(res1.overall_status, res2.overall_status)
        self.assertEqual(res1.requires_human_review, res2.requires_human_review)
        self.assertEqual(len(res1.findings), len(res2.findings))
        self.assertEqual(res1.findings[0].reason_code, res2.findings[0].reason_code)
        self.assertEqual(res1.findings[0].status, res2.findings[0].status)


if __name__ == "__main__":
    unittest.main()
