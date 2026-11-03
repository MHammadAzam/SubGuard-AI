"""Comprehensive unit tests for the deterministic compliance evaluator.

Tests all required edge cases:
- Limit comparisons (equals, exceeds, below, missing)
- Policy dates (expired, expires today, active, future inception, missing dates, inverted dates)
- Named insured matching (exact, case-insensitive, corporate mismatch, partial match, missing)
- Endorsements (verified, missing, COI checkbox only, blanket wording, incomplete doc set)
- Contradictory evidence
- Low confidence metadata
- Invalid inputs & unsupported requirement types
"""

import unittest
from datetime import date

from app.compliance.evaluator import ComplianceEvaluator, compare_entity_names
from app.schemas.insurance import (
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
from tests.fixtures.sample_data import (
    EVAL_DATE,
    make_evidence,
    sample_gl_coverage,
    sample_gl_requirement,
    sample_subcontractor_document_set,
)


class TestComplianceEvaluator(unittest.TestCase):
    def setUp(self):
        self.evaluator = ComplianceEvaluator()

    # ========================================================================
    # 1. Limit Comparisons
    # ========================================================================

    def test_limit_exactly_equals_minimum(self):
        """Limit exactly equals the required minimum ($1,000,000 == $1,000,000) -> COMPLIANT."""
        req = sample_gl_requirement(min_cents=100_000_000)
        cov = sample_gl_coverage(occ_cents=100_000_000)
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        self.assertEqual(len(result.findings), 1)
        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(finding.reason_code, "LIMIT_MEETS_OR_EXCEEDS_MINIMUM")
        self.assertFalse(finding.requires_human_review)
        self.assertEqual(result.overall_status, ComplianceStatus.COMPLIANT)

    def test_limit_exceeds_minimum(self):
        """Limit exceeds required minimum ($2,000,000 > $1,000,000) -> COMPLIANT."""
        req = sample_gl_requirement(min_cents=100_000_000)
        cov = sample_gl_coverage(occ_cents=200_000_000)
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(finding.reason_code, "LIMIT_MEETS_OR_EXCEEDS_MINIMUM")
        self.assertFalse(finding.requires_human_review)

    def test_limit_is_below_minimum(self):
        """Limit is below required minimum ($500,000 < $1,000,000) -> NON_COMPLIANT."""
        req = sample_gl_requirement(min_cents=100_000_000)
        cov = sample_gl_coverage(occ_cents=50_000_000)
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(finding.reason_code, "LIMIT_BELOW_MINIMUM")
        self.assertTrue(finding.requires_human_review)
        self.assertEqual(result.overall_status, ComplianceStatus.NON_COMPLIANT)

    def test_missing_limit_field_on_existing_coverage(self):
        """GL section is present on COI, but occurrence limit is blank/missing -> MISSING_EVIDENCE."""
        req = sample_gl_requirement(min_cents=100_000_000)
        cov = sample_gl_coverage()
        cov.each_occurrence = None  # Blank field on certificate
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "LIMIT_FIELD_NOT_EXTRACTED")
        self.assertTrue(finding.requires_human_review)
        self.assertEqual(result.overall_status, ComplianceStatus.MISSING_EVIDENCE)

    def test_missing_entire_coverage_type(self):
        """No General Liability coverage was uploaded at all -> MISSING_EVIDENCE."""
        req = sample_gl_requirement()
        doc_set = sample_subcontractor_document_set()
        doc_set.coverages = []  # No coverages uploaded

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "COVERAGE_NOT_FOUND")
        self.assertTrue(finding.requires_human_review)

    # ========================================================================
    # 2. Policy Dates & Expiration
    # ========================================================================

    def test_expired_policy(self):
        """Policy expired before evaluation date (exp: 2025-01-01 < eval: 2025-06-15) -> NON_COMPLIANT."""
        req = InsuranceRequirement(
            requirement_id="REQ_GL_EXPIRATION",
            rule_type=RequirementType.POLICY_EXPIRATION,
            description="Policy must not be expired",
            coverage_type=CoverageType.GENERAL_LIABILITY,
        )
        cov = sample_gl_coverage(exp=date(2025, 1, 1))
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(finding.reason_code, "POLICY_EXPIRED")
        self.assertEqual(result.overall_status, ComplianceStatus.NON_COMPLIANT)

    def test_policy_expires_on_evaluation_date(self):
        """Policy expires ON evaluation date (exp: 2025-06-15 == eval: 2025-06-15) -> NON_COMPLIANT."""
        req = InsuranceRequirement(
            requirement_id="REQ_GL_EXPIRATION",
            rule_type=RequirementType.POLICY_EXPIRATION,
            description="Policy must not be expired",
            coverage_type=CoverageType.GENERAL_LIABILITY,
        )
        cov = sample_gl_coverage(exp=EVAL_DATE)
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(finding.reason_code, "POLICY_EXPIRES_TODAY")

    def test_active_unexpired_policy(self):
        """Policy is unexpired (exp: 2026-01-01 > eval: 2025-06-15) -> COMPLIANT."""
        req = InsuranceRequirement(
            requirement_id="REQ_GL_EXPIRATION",
            rule_type=RequirementType.POLICY_EXPIRATION,
            description="Policy must not be expired",
            coverage_type=CoverageType.GENERAL_LIABILITY,
        )
        cov = sample_gl_coverage(exp=date(2026, 1, 1))
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(finding.reason_code, "POLICY_ACTIVE_AND_CURRENT")

    def test_missing_expiration_date(self):
        """Policy has no expiration date extracted -> MISSING_EVIDENCE."""
        req = InsuranceRequirement(
            requirement_id="REQ_GL_EXPIRATION",
            rule_type=RequirementType.POLICY_EXPIRATION,
            description="Policy must not be expired",
            coverage_type=CoverageType.GENERAL_LIABILITY,
        )
        cov = sample_gl_coverage()
        cov.expiration_date = None
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "EXPIRATION_DATE_MISSING")

    def test_policy_starts_in_the_future(self):
        """Policy inception date is in the future (eff: 2025-07-01 > eval: 2025-06-15) -> NON_COMPLIANT."""
        req = InsuranceRequirement(
            requirement_id="REQ_GL_EFFECTIVE",
            rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
            description="Policy must be in force as of evaluation date",
            coverage_type=CoverageType.GENERAL_LIABILITY,
        )
        cov = sample_gl_coverage(eff=date(2025, 7, 1), exp=date(2026, 7, 1))
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(finding.reason_code, "POLICY_FUTURE_INCEPTION")

    def test_invalid_inverted_date_range(self):
        """Policy effective date is after expiration date (eff: 2026-01-01, exp: 2025-01-01) -> NON_COMPLIANT."""
        req = InsuranceRequirement(
            requirement_id="REQ_GL_EFFECTIVE",
            rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
            description="Policy must be in force",
            coverage_type=CoverageType.GENERAL_LIABILITY,
        )
        cov = sample_gl_coverage(eff=date(2026, 1, 1), exp=date(2025, 1, 1))
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(finding.reason_code, "INVALID_DATE_RANGE_INVERTED")

    # ========================================================================
    # 3. Named Insured Verification
    # ========================================================================

    def test_named_insured_exact_match(self):
        """Named insured matches required legal name exactly -> COMPLIANT."""
        req = InsuranceRequirement(
            requirement_id="REQ_NAMED_INSURED",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured must match contract legal name",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        doc_set = sample_subcontractor_document_set(named_insured_name="Apex Electrical Contractors LLC")

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(finding.reason_code, "NAMED_INSURED_EXACT_MATCH")

    def test_named_insured_case_insensitive_match(self):
        """Named insured matches with case differences (APEX ELECTRICAL CONTRACTORS LLC) -> COMPLIANT."""
        req = InsuranceRequirement(
            requirement_id="REQ_NAMED_INSURED",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured must match",
            required_entity_name="Apex Electrical Contractors LLC",
            require_exact_name_match=False,
        )
        doc_set = sample_subcontractor_document_set(named_insured_name="apex electrical contractors llc")

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(finding.reason_code, "NAMED_INSURED_CASE_INSENSITIVE_MATCH")

    def test_named_insured_case_mismatch_when_exact_required(self):
        """Case mismatch when require_exact_name_match=True -> NON_COMPLIANT."""
        req = InsuranceRequirement(
            requirement_id="REQ_NAMED_INSURED",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured must match exactly",
            required_entity_name="Apex Electrical Contractors LLC",
            require_exact_name_match=True,
        )
        doc_set = sample_subcontractor_document_set(named_insured_name="apex electrical contractors llc")

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(finding.reason_code, "NAMED_INSURED_MISMATCH")

    def test_named_insured_partial_match_requires_human_review(self):
        """Extracted name is partial match (e.g. Apex Electrical vs Apex Electrical Contractors LLC) -> REQUIRES_HUMAN_REVIEW."""
        req = InsuranceRequirement(
            requirement_id="REQ_NAMED_INSURED",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured must match",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        doc_set = sample_subcontractor_document_set(named_insured_name="Apex Electrical")

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "NAMED_INSURED_PARTIAL_MATCH")
        self.assertTrue(finding.requires_human_review)

    def test_named_insured_complete_mismatch(self):
        """Extracted name is completely different company -> NON_COMPLIANT."""
        req = InsuranceRequirement(
            requirement_id="REQ_NAMED_INSURED",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured must match",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        doc_set = sample_subcontractor_document_set(named_insured_name="Zenith Plumbing Inc")

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(finding.reason_code, "NAMED_INSURED_MISMATCH")

    def test_missing_named_insured(self):
        """Named insured section is absent from documents -> MISSING_EVIDENCE."""
        req = InsuranceRequirement(
            requirement_id="REQ_NAMED_INSURED",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured must match",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        doc_set = sample_subcontractor_document_set()
        doc_set.named_insured = None

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "NAMED_INSURED_NOT_FOUND")

    # ========================================================================
    # 4. Endorsement Verification
    # ========================================================================

    def test_required_endorsement_with_supporting_evidence(self):
        """Required Additional Insured endorsement backed by document evidence -> COMPLIANT."""
        req = InsuranceRequirement(
            requirement_id="REQ_ADDITIONAL_INSURED",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional insured endorsement required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        endorsement = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            document_type=DocumentType.ENDORSEMENT,
            form_number="CG 20 10 04 13",
            named_entities=["General Contractor Corp"],
            evidence=make_evidence(
                doc_id="doc_endorsement_001",
                doc_type=DocumentType.ENDORSEMENT,
                page=1,
                text="Form CG 20 10: Additional Insured - Owners, Lessees or Contractors",
            ),
        )
        doc_set = sample_subcontractor_document_set(endorsements=[endorsement])

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_VERIFIED")
        self.assertFalse(finding.requires_human_review)

    def test_required_endorsement_without_evidence_record(self):
        """Endorsement claimed but no document text evidence provided -> MISSING_EVIDENCE."""
        req = InsuranceRequirement(
            requirement_id="REQ_ADDITIONAL_INSURED",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional insured endorsement required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            mandatory_evidence=True,
        )
        endorsement = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            document_type=DocumentType.ENDORSEMENT,
            form_number="CG 20 10",
            evidence=None,  # No evidence linked
        )
        doc_set = sample_subcontractor_document_set(endorsements=[endorsement])

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_EVIDENCE_TEXT_MISSING")

    def test_coi_checkbox_without_separate_endorsement_document(self):
        """COI checkbox checked, but no separate endorsement form submitted -> REQUIRES_HUMAN_REVIEW."""
        req = InsuranceRequirement(
            requirement_id="REQ_ADDITIONAL_INSURED",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional insured endorsement required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            require_separate_endorsement_document=True,
        )
        checkbox_endorsement = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            document_type=DocumentType.COI,
            is_coi_checkbox_only=True,
            evidence=make_evidence(text="ADDL INSR: [X]"),
        )
        doc_set = sample_subcontractor_document_set(endorsements=[checkbox_endorsement])

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "COI_CHECKBOX_WITHOUT_ENDORSEMENT_DOC")
        self.assertTrue(finding.requires_human_review)

    def test_blanket_endorsement_requires_human_review(self):
        """Endorsement is blanket ('as required by written contract') -> REQUIRES_HUMAN_REVIEW."""
        req = InsuranceRequirement(
            requirement_id="REQ_ADDITIONAL_INSURED",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional insured endorsement required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        endorsement = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            document_type=DocumentType.ENDORSEMENT,
            form_number="CG 20 38 04 13",
            is_blanket=True,
            evidence=make_evidence(
                text="Automatic Additional Insured - where required by written contract",
            ),
        )
        doc_set = sample_subcontractor_document_set(endorsements=[endorsement])

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "BLANKET_ENDORSEMENT_REQUIRES_CONTRACT_CHECK")

    def test_incomplete_document_set_does_not_falsely_declare_absence(self):
        """If document set is incomplete and endorsement missing, return UNABLE_TO_DETERMINE."""
        req = InsuranceRequirement(
            requirement_id="REQ_WAIVER_OF_SUB",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Waiver of Subrogation required",
            required_endorsement_type=EndorsementType.WAIVER_OF_SUBROGATION,
        )
        # Endorsement missing, but is_document_set_complete=False
        doc_set = sample_subcontractor_document_set(endorsements=[], is_complete=False)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(finding.reason_code, "INCOMPLETE_DOCUMENT_SET_ENDORSEMENT_UNKNOWN")

    # ========================================================================
    # 5. Uncertainty & Contradictions
    # ========================================================================

    def test_contradictory_evidence_limits_across_documents(self):
        """Two different limits extracted for same coverage -> UNABLE_TO_DETERMINE."""
        req = sample_gl_requirement(min_cents=100_000_000)
        cov1 = sample_gl_coverage(occ_cents=100_000_000)
        cov2 = sample_gl_coverage(occ_cents=50_000_000)
        cov2.policy_number = "GL-CONFLICTING-02"

        doc_set = sample_subcontractor_document_set()
        doc_set.coverages = [cov1, cov2]

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(finding.reason_code, "CONTRADICTORY_LIMITS_FOUND")
        self.assertTrue(finding.requires_human_review)

    def test_low_confidence_extraction_triggers_uncertain(self):
        """Extraction confidence below threshold (0.50 < 0.70) -> UNABLE_TO_DETERMINE."""
        req = sample_gl_requirement(min_cents=100_000_000)
        cov = sample_gl_coverage(occ_cents=100_000_000, confidence=0.50)
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(finding.reason_code, "EXTRACTION_CONFIDENCE_LOW")
        self.assertTrue(finding.requires_human_review)

    def test_raw_contradictions_prevent_overall_compliant_status(self):
        """Document set has recorded contradiction; overall review cannot be COMPLIANT."""
        req = sample_gl_requirement(min_cents=100_000_000)
        cov = sample_gl_coverage(occ_cents=100_000_000)
        doc_set = sample_subcontractor_document_set(
            gl_coverage=cov,
            contradictions=["COI states Policy #GL-100 but Endorsement states #GL-200"],
        )

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        # Finding itself passed
        self.assertEqual(result.findings[0].status, ComplianceStatus.COMPLIANT)
        # But overall review is flagged
        self.assertEqual(result.overall_status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertTrue(result.requires_human_review)

    # ========================================================================
    # 6. Malformed Inputs & Schema Validation
    # ========================================================================

    def test_invalid_negative_limit_rejected_by_pydantic(self):
        """Negative monetary amounts should be rejected at schema boundary."""
        with self.assertRaises(ValueError):
            MoneyValue(amount_cents=-5000, raw_text="-$50")

    def test_invalid_requirement_missing_coverage_type(self):
        """COVERAGE_LIMIT requirement without coverage_type should raise validation error."""
        with self.assertRaises(ValueError):
            InsuranceRequirement(
                requirement_id="INVALID_REQ",
                rule_type=RequirementType.COVERAGE_LIMIT,
                description="Invalid",
                limit_field=LimitField.EACH_OCCURRENCE,
                min_amount_cents=100_000_000,
            )

    def test_overall_status_not_compliant_if_any_finding_is_non_compliant_missing_uncertain_or_review(self):
        """Verify that overall status cannot be COMPLIANT if any item is non-compliant, missing, uncertain, or requires review."""
        req1 = sample_gl_requirement(req_id="REQ_GL_LIMIT", min_cents=100_000_000)
        req2 = InsuranceRequirement(
            requirement_id="REQ_GL_EXP",
            rule_type=RequirementType.POLICY_EXPIRATION,
            description="Must not be expired",
            coverage_type=CoverageType.GENERAL_LIABILITY,
        )

        # Case 1: req1 compliant, req2 expired (NON_COMPLIANT) -> overall NON_COMPLIANT
        cov_expired = sample_gl_coverage(occ_cents=100_000_000, exp=date(2025, 1, 1))
        doc_set1 = sample_subcontractor_document_set(gl_coverage=cov_expired)
        res1 = self.evaluator.evaluate(doc_set1, [req1, req2], EVAL_DATE)
        self.assertEqual(res1.overall_status, ComplianceStatus.NON_COMPLIANT)
        self.assertTrue(res1.requires_human_review)

        # Case 2: req1 compliant, req2 missing date (MISSING_EVIDENCE) -> overall MISSING_EVIDENCE
        cov_no_date = sample_gl_coverage(occ_cents=100_000_000)
        cov_no_date.expiration_date = None
        doc_set2 = sample_subcontractor_document_set(gl_coverage=cov_no_date)
        res2 = self.evaluator.evaluate(doc_set2, [req1, req2], EVAL_DATE)
        self.assertEqual(res2.overall_status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertTrue(res2.requires_human_review)

        # Case 3: req1 compliant, req2 low confidence (UNABLE_TO_DETERMINE) -> overall UNABLE_TO_DETERMINE
        cov_low_conf = sample_gl_coverage(occ_cents=100_000_000)
        cov_low_conf.expiration_date.confidence = 0.40
        doc_set3 = sample_subcontractor_document_set(gl_coverage=cov_low_conf)
        res3 = self.evaluator.evaluate(doc_set3, [req1, req2], EVAL_DATE)
        self.assertEqual(res3.overall_status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertTrue(res3.requires_human_review)

        # Case 4: req1 compliant, blanket endorsement present (REQUIRES_HUMAN_REVIEW) -> overall REQUIRES_HUMAN_REVIEW
        req3 = InsuranceRequirement(
            requirement_id="REQ_AI",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="AI required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        blanket_end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            document_type=DocumentType.ENDORSEMENT,
            is_blanket=True,
            evidence=make_evidence(text="Automatic Additional Insured"),
        )
        cov_valid = sample_gl_coverage(occ_cents=100_000_000)
        doc_set4 = sample_subcontractor_document_set(gl_coverage=cov_valid, endorsements=[blanket_end])
        res4 = self.evaluator.evaluate(doc_set4, [req1, req3], EVAL_DATE)
        self.assertEqual(res4.overall_status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertTrue(res4.requires_human_review)

    def test_absent_or_invalid_evidence_prevents_compliant_result_for_limits_and_entities(self):
        """When mandatory_evidence=True, missing evidence links must yield MISSING_EVIDENCE and cannot be COMPLIANT."""
        req_limit = sample_gl_requirement(req_id="REQ_LIMIT_EVIDENCE", min_cents=100_000_000)
        req_limit.mandatory_evidence = True

        cov_no_evidence = sample_gl_coverage(occ_cents=100_000_000)
        cov_no_evidence.evidence = None  # No document evidence pointer
        doc_set = sample_subcontractor_document_set(gl_coverage=cov_no_evidence)

        res = self.evaluator.evaluate(doc_set, [req_limit], EVAL_DATE)
        self.assertEqual(res.findings[0].status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(res.findings[0].reason_code, "LIMIT_EVIDENCE_MISSING")
        self.assertNotEqual(res.overall_status, ComplianceStatus.COMPLIANT)

        # Test named insured mandatory evidence check
        req_ni = InsuranceRequirement(
            requirement_id="REQ_NI_EVIDENCE",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured",
            required_entity_name="Apex Electrical Contractors LLC",
            mandatory_evidence=True,
        )
        doc_set.named_insured.evidence = None  # Strip evidence
        res_ni = self.evaluator.evaluate(doc_set, [req_ni], EVAL_DATE)
        self.assertEqual(res_ni.findings[0].status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(res_ni.findings[0].reason_code, "NAMED_INSURED_EVIDENCE_MISSING")

    def test_unsupported_requirement_type(self):
        """Unsupported requirement type returns UNABLE_TO_DETERMINE with machine-readable code."""
        # Bypass requirement validator via object creation or custom mock
        req = InsuranceRequirement.model_construct(
            requirement_id="REQ_UNKNOWN",
            rule_type=RequirementType.COMPLETE_DOCUMENT_SET,  # Not in evaluator rule_types
            description="Unknown custom check",
            severity=Severity.MINOR,
        )
        doc_set = sample_subcontractor_document_set()

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)

        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(finding.reason_code, "UNSUPPORTED_RULE_TYPE")

    # =========================================================================
    # Phase 7 Legal Entity Matching Tests
    # =========================================================================

    def test_entity_matching_inc_vs_llc_requires_human_review(self):
        """Discrepancy in corporate designation (Inc vs LLC) must never automatically pass; requires human review."""
        evaluator = ComplianceEvaluator()
        req = InsuranceRequirement(
            requirement_id="REQ-ENTITY-01",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured match",
            required_entity_name="Apex Electrical LLC",
        )
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-INC-LLC",
            named_insured=EntityInfo(
                name="Apex Electrical Inc",  # Inc vs required LLC
                evidence=EvidenceReference(
                    document_id="DOC-1",
                    document_type=DocumentType.COI,
                    page_number=1,
                    source_text="Apex Electrical Inc",
                ),
            ),
        )
        result = evaluator.evaluate(document_set=doc_set, requirements=[req], evaluation_date=EVAL_DATE)
        self.assertEqual(result.overall_status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "NAMED_INSURED_CORPORATE_SUFFIX_DISCREPANCY")
        self.assertTrue(finding.requires_human_review)

    def test_entity_matching_different_entities_similar_wording(self):
        """Entities with similar wording but different business names must be NON_COMPLIANT."""
        evaluator = ComplianceEvaluator()
        req = InsuranceRequirement(
            requirement_id="REQ-ENTITY-02",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured match",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-DIFF-NAME",
            named_insured=EntityInfo(
                name="Apex Plumbing Contractors LLC",  # Different company
                evidence=EvidenceReference(
                    document_id="DOC-1",
                    document_type=DocumentType.COI,
                    page_number=1,
                    source_text="Apex Plumbing Contractors LLC",
                ),
            ),
        )
        result = evaluator.evaluate(document_set=doc_set, requirements=[req], evaluation_date=EVAL_DATE)
        self.assertEqual(result.overall_status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(result.findings[0].reason_code, "NAMED_INSURED_MISMATCH")

    def test_entity_matching_exact_legal_name_compliant(self):
        """Exact match of legal entity name passes as COMPLIANT."""
        evaluator = ComplianceEvaluator()
        req = InsuranceRequirement(
            requirement_id="REQ-ENTITY-03",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured match",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-EXACT",
            named_insured=EntityInfo(
                name="Apex Electrical Contractors LLC",
                evidence=EvidenceReference(
                    document_id="DOC-1",
                    document_type=DocumentType.COI,
                    page_number=1,
                    source_text="Apex Electrical Contractors LLC",
                ),
            ),
        )
        result = evaluator.evaluate(document_set=doc_set, requirements=[req], evaluation_date=EVAL_DATE)
        self.assertEqual(result.overall_status, ComplianceStatus.COMPLIANT)
        self.assertEqual(result.findings[0].reason_code, "NAMED_INSURED_EXACT_MATCH")

    def test_entity_matching_minor_punctuation_differences_compliant(self):
        """Punctuation variations in legal suffix formatting (commas, periods) normalize conservatively."""
        evaluator = ComplianceEvaluator()
        req = InsuranceRequirement(
            requirement_id="REQ-ENTITY-04",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured match",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-PUNCT",
            named_insured=EntityInfo(
                name="Apex Electrical Contractors, L.L.C.",  # Commas and periods in suffix
                evidence=EvidenceReference(
                    document_id="DOC-1",
                    document_type=DocumentType.COI,
                    page_number=1,
                    source_text="Apex Electrical Contractors, L.L.C.",
                ),
            ),
        )
        result = evaluator.evaluate(document_set=doc_set, requirements=[req], evaluation_date=EVAL_DATE)
        self.assertEqual(result.overall_status, ComplianceStatus.COMPLIANT)
        self.assertEqual(result.findings[0].reason_code, "NAMED_INSURED_FORMAT_NORMALIZED_MATCH")

    def test_entity_matching_whitespace_differences_compliant(self):
        """Multiple internal spaces or tabs normalize to single space for match."""
        evaluator = ComplianceEvaluator()
        req = InsuranceRequirement(
            requirement_id="REQ-ENTITY-05",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured match",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-WS",
            named_insured=EntityInfo(
                name="Apex   Electrical   Contractors   LLC",
                evidence=EvidenceReference(
                    document_id="DOC-1",
                    document_type=DocumentType.COI,
                    page_number=1,
                    source_text="Apex   Electrical   Contractors   LLC",
                ),
            ),
        )
        result = evaluator.evaluate(document_set=doc_set, requirements=[req], evaluation_date=EVAL_DATE)
        self.assertEqual(result.overall_status, ComplianceStatus.COMPLIANT)


    # =========================================================================
    # Phase 4 Hardening: Effective Date Regression Tests
    # =========================================================================

    def test_effective_date_active_and_valid_positive(self):
        """Valid effective date prior to evaluation date and before expiration date -> COMPLIANT."""
        req = InsuranceRequirement(
            requirement_id="REQ_EFF_VALID",
            rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
            description="Policy effective date valid",
            coverage_type=CoverageType.GENERAL_LIABILITY,
        )
        cov = sample_gl_coverage(eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)
        self.assertEqual(len(result.findings), 1)
        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(finding.reason_code, "EFFECTIVE_DATE_VALID")
        self.assertFalse(finding.requires_human_review)
        self.assertEqual(result.overall_status, ComplianceStatus.COMPLIANT)

    def test_effective_date_equal_to_evaluation_date(self):
        """Boundary: Policy effective date equals evaluation date -> COMPLIANT (policy starts today)."""
        req = InsuranceRequirement(
            requirement_id="REQ_EFF_TODAY",
            rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
            description="Policy starts today",
            coverage_type=CoverageType.GENERAL_LIABILITY,
        )
        cov = sample_gl_coverage(eff=EVAL_DATE, exp=date(2026, 1, 1))
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)
        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(finding.reason_code, "EFFECTIVE_DATE_VALID")
        self.assertFalse(finding.requires_human_review)

    def test_effective_date_low_confidence(self):
        """Effective date with confidence below threshold (0.45 < 0.70) -> UNABLE_TO_DETERMINE."""
        req = InsuranceRequirement(
            requirement_id="REQ_EFF_LOW_CONF",
            rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
            description="Policy effective date check",
            coverage_type=CoverageType.GENERAL_LIABILITY,
        )
        cov = sample_gl_coverage(eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        cov.effective_date.confidence = 0.45
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)
        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(finding.reason_code, "EFFECTIVE_DATE_CONFIDENCE_LOW")
        self.assertTrue(finding.requires_human_review)

    def test_effective_date_uncertain_extraction_status(self):
        """Effective date coverage with UNCERTAIN extraction status -> UNABLE_TO_DETERMINE."""
        req = InsuranceRequirement(
            requirement_id="REQ_EFF_UNCERTAIN",
            rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
            description="Policy effective date check",
            coverage_type=CoverageType.GENERAL_LIABILITY,
        )
        cov = sample_gl_coverage(eff=date(2025, 1, 1), exp=date(2026, 1, 1), status=ExtractionStatus.UNCERTAIN)
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)
        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(finding.reason_code, "EFFECTIVE_DATE_CONFIDENCE_LOW")
        self.assertTrue(finding.requires_human_review)

    def test_effective_date_mandatory_evidence_missing(self):
        """Effective date with mandatory_evidence=True but no evidence -> MISSING_EVIDENCE."""
        req = InsuranceRequirement(
            requirement_id="REQ_EFF_MAND_EV",
            rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
            description="Effective date requires evidence",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            mandatory_evidence=True,
        )
        cov = sample_gl_coverage(eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        cov.evidence = None
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)
        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "EFFECTIVE_DATE_EVIDENCE_MISSING")
        self.assertTrue(finding.requires_human_review)

    # =========================================================================
    # Phase 4 Hardening: Endorsement Uncertainty Regression Tests
    # =========================================================================

    def test_endorsement_low_confidence(self):
        """Endorsement with confidence below threshold (0.50 < 0.70) -> UNABLE_TO_DETERMINE."""
        req = InsuranceRequirement(
            requirement_id="REQ_AI_LOW_CONF",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            confidence=0.50,
            evidence=make_evidence(text="CG 20 10 Additional Insured"),
        )
        doc_set = sample_subcontractor_document_set(endorsements=[end])

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)
        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_CONFIDENCE_LOW")
        self.assertTrue(finding.requires_human_review)

    def test_endorsement_uncertain_extraction_status(self):
        """Endorsement with UNCERTAIN extraction status -> UNABLE_TO_DETERMINE."""
        req = InsuranceRequirement(
            requirement_id="REQ_AI_UNCERTAIN",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            extraction_status=ExtractionStatus.UNCERTAIN,
            evidence=make_evidence(text="CG 20 10 Additional Insured"),
        )
        doc_set = sample_subcontractor_document_set(endorsements=[end])

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)
        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_CONFIDENCE_LOW")
        self.assertTrue(finding.requires_human_review)

    def test_endorsement_quote_present_unverified_support(self):
        """Endorsement with QUOTE_PRESENT_UNVERIFIED_SUPPORT status -> REQUIRES_HUMAN_REVIEW."""
        req = InsuranceRequirement(
            requirement_id="REQ_AI_UNVERIFIED_SUPP",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            extraction_status=ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT,
            evidence=make_evidence(text="General Liability Policy Schedule"),
        )
        doc_set = sample_subcontractor_document_set(endorsements=[end])

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)
        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_EVIDENCE_SUPPORT_UNVERIFIED")
        self.assertTrue(finding.requires_human_review)

    def test_endorsement_not_found_when_document_set_complete(self):
        """Endorsement missing when document set is complete -> MISSING_EVIDENCE (ENDORSEMENT_NOT_FOUND)."""
        req = InsuranceRequirement(
            requirement_id="REQ_AI_MISSING",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        doc_set = sample_subcontractor_document_set(endorsements=[], is_complete=True)

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)
        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_NOT_FOUND")
        self.assertTrue(finding.requires_human_review)

    # =========================================================================
    # Phase 4 Hardening: Named Insured Empty / Whitespace-Only Tests
    # =========================================================================

    def test_named_insured_empty_string_classified_as_missing_evidence(self):
        """Entity exists with empty string name -> MISSING_EVIDENCE (not mismatch)."""
        req = InsuranceRequirement(
            requirement_id="REQ_NI_EMPTY",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured match",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        doc_set = sample_subcontractor_document_set(named_insured_name="")

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)
        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "NAMED_INSURED_NOT_FOUND")
        self.assertTrue(finding.requires_human_review)

    def test_named_insured_whitespace_only_classified_as_missing_evidence(self):
        """Entity exists with whitespace-only name -> MISSING_EVIDENCE (not mismatch)."""
        req = InsuranceRequirement(
            requirement_id="REQ_NI_WS",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured match",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        doc_set = sample_subcontractor_document_set(named_insured_name="   \t  \n  ")

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)
        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "NAMED_INSURED_NOT_FOUND")
        self.assertTrue(finding.requires_human_review)

    def test_named_insured_genuine_mismatch_preserves_non_compliant(self):
        """A genuine mismatch against a different legal entity remains NON_COMPLIANT."""
        req = InsuranceRequirement(
            requirement_id="REQ_NI_MISMATCH",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured match",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        doc_set = sample_subcontractor_document_set(named_insured_name="Zenith Plumbing Inc")

        result = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)
        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(finding.reason_code, "NAMED_INSURED_MISMATCH")
        self.assertTrue(finding.requires_human_review)

    # =========================================================================
    # Phase 4 Hardening: Determinism & Provider-Independence Tests
    # =========================================================================

    def test_evaluator_five_repeated_evaluations_identical_output(self):
        """Five repeated evaluations of identical inputs produce identical results (idempotence)."""
        req1 = sample_gl_requirement(min_cents=100_000_000)
        req2 = InsuranceRequirement(
            requirement_id="REQ_EXP",
            rule_type=RequirementType.POLICY_EXPIRATION,
            description="Unexpired",
            coverage_type=CoverageType.GENERAL_LIABILITY,
        )
        req3 = InsuranceRequirement(
            requirement_id="REQ_NI",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured match",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        reqs = [req1, req2, req3]
        doc_set = sample_subcontractor_document_set()

        first_res = self.evaluator.evaluate(doc_set, reqs, EVAL_DATE)

        for i in range(4):
            subsequent_res = self.evaluator.evaluate(doc_set, reqs, EVAL_DATE)
            self.assertEqual(subsequent_res.overall_status, first_res.overall_status)
            self.assertEqual(subsequent_res.requires_human_review, first_res.requires_human_review)
            self.assertEqual(len(subsequent_res.findings), len(first_res.findings))
            for f_sub, f_first in zip(subsequent_res.findings, first_res.findings):
                self.assertEqual(f_sub.requirement_id, f_first.requirement_id)
                self.assertEqual(f_sub.status, f_first.status)
                self.assertEqual(f_sub.reason_code, f_first.reason_code)
                self.assertEqual(f_sub.requires_human_review, f_first.requires_human_review)

    def test_evaluator_requirement_ordering_independence(self):
        """Changing the order of requirements produces identical findings and overall status."""
        req_gl = sample_gl_requirement(min_cents=100_000_000)
        req_exp = InsuranceRequirement(
            requirement_id="REQ_EXP",
            rule_type=RequirementType.POLICY_EXPIRATION,
            description="Unexpired",
            coverage_type=CoverageType.GENERAL_LIABILITY,
        )
        req_ni = InsuranceRequirement(
            requirement_id="REQ_NI",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured match",
            required_entity_name="Apex Electrical Contractors LLC",
        )

        reqs_forward = [req_gl, req_exp, req_ni]
        reqs_reversed = [req_ni, req_exp, req_gl]
        reqs_permuted = [req_exp, req_ni, req_gl]

        doc_set = sample_subcontractor_document_set()

        res_fwd = self.evaluator.evaluate(doc_set, reqs_forward, EVAL_DATE)
        res_rev = self.evaluator.evaluate(doc_set, reqs_reversed, EVAL_DATE)
        res_per = self.evaluator.evaluate(doc_set, reqs_permuted, EVAL_DATE)

        self.assertEqual(res_fwd.overall_status, ComplianceStatus.COMPLIANT)
        self.assertEqual(res_rev.overall_status, ComplianceStatus.COMPLIANT)
        self.assertEqual(res_per.overall_status, ComplianceStatus.COMPLIANT)

        # Confirm finding status per requirement_id matches regardless of order
        fwd_map = {f.requirement_id: f.status for f in res_fwd.findings}
        rev_map = {f.requirement_id: f.status for f in res_rev.findings}
        per_map = {f.requirement_id: f.status for f in res_per.findings}

        self.assertEqual(fwd_map, rev_map)
        self.assertEqual(fwd_map, per_map)

    def test_evaluator_provider_independence(self):
        """Compliance outcome depends solely on extracted document set data, not provider origin."""
        req = sample_gl_requirement(min_cents=100_000_000)

        # Simulate doc sets that could have been created by Gemini, OpenRouter, or Mock
        # All represent identical extracted underlying insurance data
        doc_set_gemini_equivalent = ExtractedDocumentSet(
            subcontractor_id="SUB-GEMINI",
            named_insured=EntityInfo(name="Apex Electrical Contractors LLC"),
            coverages=[sample_gl_coverage(occ_cents=100_000_000)],
        )
        doc_set_openrouter_equivalent = ExtractedDocumentSet(
            subcontractor_id="SUB-OPENROUTER",
            named_insured=EntityInfo(name="Apex Electrical Contractors LLC"),
            coverages=[sample_gl_coverage(occ_cents=100_000_000)],
        )

        res_gemini = self.evaluator.evaluate(doc_set_gemini_equivalent, [req], EVAL_DATE)
        res_openrouter = self.evaluator.evaluate(doc_set_openrouter_equivalent, [req], EVAL_DATE)

        self.assertEqual(res_gemini.overall_status, res_openrouter.overall_status)
        self.assertEqual(res_gemini.findings[0].status, res_openrouter.findings[0].status)
        self.assertEqual(res_gemini.findings[0].reason_code, res_openrouter.findings[0].reason_code)


if __name__ == "__main__":
    unittest.main()
