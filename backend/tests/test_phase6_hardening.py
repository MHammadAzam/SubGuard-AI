"""Phase 6 Adversarial and Regression Hardening Test Suite.

Verifies:
1. Hardened quotation resolution (elimination of single-bigram matching).
2. Hardened endorsement semantic support (rejection of generic 'insured' / 'additional').
3. Document-set completeness symmetry across coverage and entity evaluation.
4. Deterministic multi-endorsement aggregation policy.
5. Protection against silent compliance on ExtractionStatus.PARTIAL.
6. Safe handling of omitted confidence without treating missing confidence as verified evidence.
"""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from app.compliance.evaluator import ComplianceEvaluator
from app.documents.processor import PDFProcessor
from app.extraction.pipeline import ExtractionPipelineService
from app.extraction.providers.mock import MockExtractionProvider
from app.schemas.insurance import (
    ComplianceStatus,
    CoverageExtraction,
    CoverageType,
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
)
from app.schemas.raw_extraction import (
    RawCoverageSection,
    RawEndorsement,
    RawEntityExtraction,
    RawEvidenceQuote,
    RawInsuranceExtraction,
)
from evaluation.fixtures.synthetic_generator import generate_all_synthetic_fixtures, get_fixtures_dir


class TestPhase6Hardening(unittest.TestCase):
    """Adversarial and regression test suite for Phase 6 hardening."""

    @classmethod
    def setUpClass(cls):
        generate_all_synthetic_fixtures()
        cls.fixtures_dir = get_fixtures_dir()
        cls.pdf_processor = PDFProcessor(render_dpi=72)
        cls.evaluator = ComplianceEvaluator()
        cls.eval_date = date(2025, 6, 15)

    def _read_fixture(self, filename: str) -> bytes:
        pdf_path = self.fixtures_dir / filename
        self.assertTrue(pdf_path.exists(), f"Missing required synthetic fixture: {pdf_path}")
        return pdf_path.read_bytes()

    # =========================================================================
    # Task 1: Adversarial Quotation and Semantic Verification Tests
    # =========================================================================

    def test_adversarial_fabricated_endorsement_citing_only_insured_field_rejected(self):
        """Fabricated endorsement citing only an 'INSURED:' box text must NOT receive verified semantic support."""
        pdf_bytes = self._read_fixture("synthetic_standard_acord25.pdf")
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="P6-ADV-01")

        # LLM claims Additional Insured but cites only the insured party header
        canned_extraction = RawInsuranceExtraction(
            document_type="coi",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number=None,
                    confidence=0.95,
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="INSURED: Apex Electrical Contractors LLC",
                    ),
                )
            ],
            is_document_set_complete=True,
        )

        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=canned_extraction)
        )
        pipeline_result = pipeline.process_inspection(inspection, subcontractor_id="SUB-ADV-01")
        doc_set = pipeline_result.document_set
        self.assertIsNotNone(doc_set)

        # Endorsement text exists on page, but lacks endorsement semantic indicators
        end = doc_set.endorsements[0]
        self.assertEqual(end.extraction_status, ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT)

        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        finding = self.evaluator.evaluate(doc_set, [req], self.eval_date).findings[0]

        # Must NOT be COMPLIANT; must route to human review
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_EVIDENCE_SUPPORT_UNVERIFIED")
        self.assertTrue(finding.requires_human_review)

    def test_adversarial_fabricated_quotation_sharing_single_bigram_fails_resolution(self):
        """Fabricated quotation sharing only one coincidental bigram ('liability insurance') fails resolution entirely."""
        pdf_bytes = self._read_fixture("synthetic_standard_acord25.pdf")
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="P6-ADV-02")

        # Fabricated quote that contains 'liability insurance' (which is on the ACORD 25 header),
        # but the remaining 8 words are completely fabricated.
        canned_extraction = RawInsuranceExtraction(
            document_type="coi",
            endorsements=[
                RawEndorsement(
                    endorsement_type="waiver_of_subrogation",
                    form_number="CG 24 04",
                    confidence=0.95,
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="Apex is granted complete waiver of subrogation for liability insurance",
                    ),
                )
            ],
            is_document_set_complete=True,
        )

        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=canned_extraction)
        )
        pipeline_result = pipeline.process_inspection(inspection, subcontractor_id="SUB-ADV-02")
        doc_set = pipeline_result.document_set
        self.assertIsNotNone(doc_set)

        # Quote must fail resolution: no EvidenceReference created, status becomes UNCERTAIN
        end = doc_set.endorsements[0]
        self.assertIsNone(end.evidence)
        self.assertEqual(end.extraction_status, ExtractionStatus.UNCERTAIN)

        req = InsuranceRequirement(
            requirement_id="REQ_WOS",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Waiver of subrogation required",
            required_endorsement_type=EndorsementType.WAIVER_OF_SUBROGATION,
        )
        finding = self.evaluator.evaluate(doc_set, [req], self.eval_date).findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_EVIDENCE_TEXT_MISSING")
        self.assertTrue(finding.requires_human_review)

    def test_adversarial_correct_verbatim_endorsement_quotation_passes(self):
        """Verbatim quotation from page 2 of multipage fixture resolves cleanly and passes compliance."""
        pdf_bytes = self._read_fixture("synthetic_multipage_coi_endorsement.pdf")
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="P6-ADV-03")

        canned_extraction = RawInsuranceExtraction(
            document_type="coi",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    confidence=0.95,
                    is_standard_form=True,
                    manuscript_clause_detected=False,
                    manuscript_status="NOT_DETECTED",
                    manuscript_verification_basis="unmodified_standard_form",
                    evidence=RawEvidenceQuote(
                        page_number=2,
                        source_text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13",
                    ),
                )
            ],
            is_document_set_complete=True,
        )

        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=canned_extraction)
        )
        pipeline_result = pipeline.process_inspection(inspection, subcontractor_id="SUB-ADV-03")
        doc_set = pipeline_result.document_set
        self.assertIsNotNone(doc_set)

        # Mark independent integrity verified to simulate verified unmodified standard form
        doc_set.endorsements[0].independent_integrity_verified = True

        end = doc_set.endorsements[0]
        self.assertIsNotNone(end.evidence)
        self.assertEqual(end.extraction_status, ExtractionStatus.EXTRACTED)

        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        finding = self.evaluator.evaluate(doc_set, [req], self.eval_date).findings[0]
        self.assertEqual(finding.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_VERIFIED")

    def test_adversarial_partial_quotation_missing_critical_wording_rejected(self):
        """Short non-contiguous quotation ('Apex waiver') does not exist on page and is rejected."""
        pdf_bytes = self._read_fixture("synthetic_standard_acord25.pdf")
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="P6-ADV-04")

        canned_extraction = RawInsuranceExtraction(
            document_type="coi",
            endorsements=[
                RawEndorsement(
                    endorsement_type="waiver_of_subrogation",
                    confidence=0.95,
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="Apex waiver",
                    ),
                )
            ],
            is_document_set_complete=True,
        )

        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=canned_extraction)
        )
        pipeline_result = pipeline.process_inspection(inspection, subcontractor_id="SUB-ADV-04")
        doc_set = pipeline_result.document_set
        self.assertIsNone(doc_set.endorsements[0].evidence)
        self.assertEqual(doc_set.endorsements[0].extraction_status, ExtractionStatus.UNCERTAIN)

    # =========================================================================
    # Task 2: Incomplete Document-Set Symmetry Tests
    # =========================================================================

    def test_incomplete_document_set_missing_coverage_returns_unable_to_determine(self):
        """When is_document_set_complete=False, missing coverage returns UNABLE_TO_DETERMINE."""
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-INC-01",
            is_document_set_complete=False,  # Incomplete upload
            coverages=[],  # Workers' Comp missing
        )

        req = InsuranceRequirement(
            requirement_id="REQ_WC",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="Workers' Compensation coverage required",
            coverage_type=CoverageType.WORKERS_COMP,
            limit_field=LimitField.EACH_OCCURRENCE,
            min_amount_cents=50_000_000,
        )

        review = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = review.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(finding.reason_code, "INCOMPLETE_DOCUMENT_SET_COVERAGE_UNKNOWN")
        self.assertTrue(finding.requires_human_review)
        self.assertEqual(review.overall_status, ComplianceStatus.UNABLE_TO_DETERMINE)

    def test_complete_document_set_genuinely_missing_coverage_returns_missing_evidence(self):
        """When is_document_set_complete=True, missing coverage returns MISSING_EVIDENCE (COVERAGE_NOT_FOUND)."""
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-COM-01",
            is_document_set_complete=True,  # Complete upload
            coverages=[],  # Workers' Comp missing
        )

        req = InsuranceRequirement(
            requirement_id="REQ_WC",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="Workers' Compensation coverage required",
            coverage_type=CoverageType.WORKERS_COMP,
            limit_field=LimitField.EACH_OCCURRENCE,
            min_amount_cents=50_000_000,
        )

        review = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = review.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "COVERAGE_NOT_FOUND")
        self.assertTrue(finding.requires_human_review)

    # =========================================================================
    # Task 3: Multiple Endorsement Evaluation Tests
    # =========================================================================

    def test_multiple_matching_endorsements_with_conflicting_policies_requires_human_review(self):
        """Multiple endorsements claiming coverage under conflicting policy numbers must trigger CONTRADICTORY_ENDORSEMENTS_FOUND."""
        ev1 = EvidenceReference(
            document_id="DOC-1",
            page_number=1,
            source_text="CG 20 10 04 13 Additional Insured",
        )
        ev2 = EvidenceReference(
            document_id="DOC-1",
            page_number=2,
            source_text="CG 20 10 04 13 Additional Insured",
        )

        e1 = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            applicable_policy="POL-ALPHA-1234",
            evidence=ev1,
            extraction_status=ExtractionStatus.EXTRACTED,
            confidence=0.95,
        )
        e2 = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            applicable_policy="POL-BETA-9999",  # Conflicting policy
            evidence=ev2,
            extraction_status=ExtractionStatus.EXTRACTED,
            confidence=0.95,
        )

        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-MULT-01",
            is_document_set_complete=True,
            endorsements=[e1, e2],
        )

        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )

        finding = self.evaluator.evaluate(doc_set, [req], self.eval_date).findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "CONTRADICTORY_ENDORSEMENTS_FOUND")
        self.assertTrue(finding.requires_human_review)

    def test_multiple_matching_endorsements_with_low_confidence_in_second_routes_to_unable_to_determine(self):
        """If first endorsement is high confidence but second is low confidence, uncertainty must NOT be suppressed."""
        ev = EvidenceReference(document_id="D1", page_number=1, source_text="CG 20 10 Additional Insured")
        e1 = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            confidence=0.95,
            evidence=ev,
            extraction_status=ExtractionStatus.EXTRACTED,
        )
        e2 = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            confidence=0.40,  # Low confidence
            evidence=ev,
            extraction_status=ExtractionStatus.EXTRACTED,
        )

        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-MULT-02",
            is_document_set_complete=True,
            endorsements=[e1, e2],
        )

        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )

        finding = self.evaluator.evaluate(doc_set, [req], self.eval_date).findings[0]
        self.assertEqual(finding.status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_CONFIDENCE_LOW")
        self.assertTrue(finding.requires_human_review)

    # =========================================================================
    # Task 4: Partial Extraction Protection Tests
    # =========================================================================

    def test_partial_coverage_extraction_with_sufficient_limit_requires_human_review(self):
        """Coverage limit meeting amount but flagged with PARTIAL extraction status must NOT pass as COMPLIANT."""
        ev = EvidenceReference(document_id="D1", page_number=1, source_text="EACH OCCURRENCE $2,000,000")
        cov = CoverageExtraction(
            coverage_type=CoverageType.GENERAL_LIABILITY,
            each_occurrence=MoneyValue(amount_cents=200_000_000, raw_text="$2,000,000"),
            evidence=ev,
            extraction_status=ExtractionStatus.PARTIAL,  # Incomplete / partial extraction
            confidence=0.95,
        )
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-PART-01",
            is_document_set_complete=True,
            coverages=[cov],
        )

        req = InsuranceRequirement(
            requirement_id="REQ_GL",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="GL Each Occurrence >= $1M",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            limit_field=LimitField.EACH_OCCURRENCE,
            min_amount_cents=100_000_000,
        )

        finding = self.evaluator.evaluate(doc_set, [req], self.eval_date).findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "COVERAGE_PARTIAL_EXTRACTION")
        self.assertTrue(finding.requires_human_review)

    def test_partial_named_insured_extraction_requires_human_review(self):
        """Named insured matching required name but flagged with PARTIAL status routes to human review."""
        ev = EvidenceReference(document_id="D1", page_number=1, source_text="Apex Electrical Contractors")
        ni = EntityInfo(
            name="Apex Electrical Contractors LLC",
            evidence=ev,
            extraction_status=ExtractionStatus.PARTIAL,
            confidence=0.95,
        )
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-PART-02",
            is_document_set_complete=True,
            named_insured=ni,
        )

        req = InsuranceRequirement(
            requirement_id="REQ_NI",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured match",
            required_entity_name="Apex Electrical Contractors LLC",
        )

        finding = self.evaluator.evaluate(doc_set, [req], self.eval_date).findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "NAMED_INSURED_PARTIAL_EXTRACTION")
        self.assertTrue(finding.requires_human_review)

    def test_missing_confidence_with_verified_and_unverified_evidence(self):
        """Confidence None evaluates cleanly without TypeError, but unverified evidence still routes to human review."""
        ev = EvidenceReference(document_id="D1", page_number=1, source_text="CG 20 10 Additional Insured")
        
        # Subcase A: Verified evidence quote with confidence=None -> COMPLIANT
        e_valid = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            confidence=None,
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=True,
            evidence=ev,
            extraction_status=ExtractionStatus.EXTRACTED,
        )
        doc_valid = ExtractedDocumentSet(subcontractor_id="S1", endorsements=[e_valid])
        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        f_valid = self.evaluator.evaluate(doc_valid, [req], self.eval_date).findings[0]
        self.assertEqual(f_valid.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(f_valid.reason_code, "ENDORSEMENT_VERIFIED")

        # Subcase B: Unverified support with confidence=None -> REQUIRES_HUMAN_REVIEW
        e_unverified = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            confidence=None,
            evidence=ev,
            extraction_status=ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT,
        )
        doc_unverified = ExtractedDocumentSet(subcontractor_id="S2", endorsements=[e_unverified])
        f_unverified = self.evaluator.evaluate(doc_unverified, [req], self.eval_date).findings[0]
        self.assertEqual(f_unverified.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(f_unverified.reason_code, "ENDORSEMENT_EVIDENCE_SUPPORT_UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
