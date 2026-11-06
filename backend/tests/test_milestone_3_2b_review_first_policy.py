"""Milestone 3.2B Review-First Missing Evidence Policy Unit & Regression Tests.

Validates the approved product owner business policy:
- 'A missing required insurance policy, coverage document, or endorsement should normally trigger document review, not automatic rejection.'
- 'Absence of evidence is not sufficient proof of contractual non-compliance.'
- Distinguishes:
  1. Missing evidence -> MISSING_EVIDENCE / REQUIRES_HUMAN_REVIEW
  2. Unable to determine -> UNABLE_TO_DETERMINE
  3. Confirmed non-compliance -> NON_COMPLIANT
  4. Verified compliance -> COMPLIANT

Covers all 15 test items including cohort scenario regressions for
SCEN-13, 14, 19, 22, 25, 26, 35, 39, and 40.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date as dt_date
from pathlib import Path
from uuid import uuid4

from app.compliance.evaluator import ComplianceEvaluator, compare_entity_names
from app.persistence.database import init_db
from app.persistence.repository import SubmissionRepository
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
from evaluation.metrics.compliance_metrics import (
    FOUR_CLASSES,
    FIVE_CLASSES,
    calculate_finding_metrics,
    calculate_safety_metrics,
    calculate_submission_metrics,
)
from evaluation.schemas.ground_truth import (
    DocumentGroundTruth,
    DocumentSourceType,
    ExpectedFinding,
    ScenarioGroundTruth,
)
from evaluation.schemas.reports import (
    ScenarioEvaluationResult,
    ScenarioFindingResult,
)


class TestMilestone32BReviewFirstPolicy(unittest.TestCase):
    """Test suite covering the 15 requirements of Milestone 3.2B."""

    def setUp(self):
        self.evaluator = ComplianceEvaluator()
        self.eval_date = dt_date(2025, 6, 1)

    def _make_gl_req(self, min_cents: int = 100_000_000) -> InsuranceRequirement:
        return InsuranceRequirement(
            requirement_id="REQ_GL_EACH_OCC",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="GL Each Occurrence limit >= $1M",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            limit_field=LimitField.EACH_OCCURRENCE,
            min_amount_cents=min_cents,
            severity=Severity.CRITICAL,
        )

    def _make_wc_req(self) -> InsuranceRequirement:
        return InsuranceRequirement(
            requirement_id="REQ_WC_ACCIDENT",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="Workers Comp Each Accident limit >= $1M",
            coverage_type=CoverageType.WORKERS_COMP,
            limit_field=LimitField.PER_ACCIDENT,
            min_amount_cents=100_000_000,
            severity=Severity.CRITICAL,
        )

    def _make_exp_req(self) -> InsuranceRequirement:
        return InsuranceRequirement(
            requirement_id="REQ_GL_EXPIRATION",
            rule_type=RequirementType.POLICY_EXPIRATION,
            description="GL policy must be active",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            severity=Severity.CRITICAL,
        )

    def _make_eff_req(self) -> InsuranceRequirement:
        return InsuranceRequirement(
            requirement_id="REQ_GL_EFFECTIVE",
            rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
            description="GL policy effective date must be in force",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            severity=Severity.CRITICAL,
        )

    def _make_ni_req(self, name: str = "Apex Electrical Contractors LLC") -> InsuranceRequirement:
        return InsuranceRequirement(
            requirement_id="REQ_NAMED_INSURED",
            rule_type=RequirementType.NAMED_INSURED,
            description=f"Named Insured must match '{name}'",
            required_entity_name=name,
            severity=Severity.CRITICAL,
        )

    def _make_ai_req(self) -> InsuranceRequirement:
        return InsuranceRequirement(
            requirement_id="REQ_ADDITIONAL_INSURED",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured endorsement mandatory",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            severity=Severity.CRITICAL,
        )

    # -------------------------------------------------------------------------
    # Test 1: Missing coverage on complete package yields MISSING_EVIDENCE
    # -------------------------------------------------------------------------
    def test_01_missing_coverage_complete_package_yields_missing_evidence(self):
        """1. Missing required coverage on complete package yields MISSING_EVIDENCE, not NON_COMPLIANT."""
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-01",
            is_document_set_complete=True,
            coverages=[],
        )
        res = self.evaluator.evaluate(doc_set, [self._make_wc_req()], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "COVERAGE_NOT_FOUND")
        self.assertTrue(finding.requires_human_review)
        self.assertEqual(res.overall_status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertTrue(res.requires_human_review)

    # -------------------------------------------------------------------------
    # Test 2: Missing coverage on incomplete package yields UNABLE_TO_DETERMINE
    # -------------------------------------------------------------------------
    def test_02_missing_coverage_incomplete_package_yields_unable_to_determine(self):
        """2. Missing coverage on incomplete document set yields UNABLE_TO_DETERMINE."""
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-02",
            is_document_set_complete=False,
            coverages=[],
        )
        res = self.evaluator.evaluate(doc_set, [self._make_wc_req()], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(finding.reason_code, "INCOMPLETE_DOCUMENT_SET_COVERAGE_UNKNOWN")
        self.assertTrue(finding.requires_human_review)
        self.assertEqual(res.overall_status, ComplianceStatus.UNABLE_TO_DETERMINE)

    # -------------------------------------------------------------------------
    # Test 3: Missing endorsement form on complete package yields MISSING_EVIDENCE
    # -------------------------------------------------------------------------
    def test_03_missing_endorsement_on_complete_package_yields_missing_evidence(self):
        """3. Missing required endorsement yields MISSING_EVIDENCE with ENDORSEMENT_NOT_FOUND."""
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-03",
            is_document_set_complete=True,
            endorsements=[],
        )
        res = self.evaluator.evaluate(doc_set, [self._make_ai_req()], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_NOT_FOUND")
        self.assertTrue(finding.requires_human_review)
        self.assertEqual(res.overall_status, ComplianceStatus.MISSING_EVIDENCE)

    # -------------------------------------------------------------------------
    # Test 4: Missing policy expiration date on complete package yields MISSING_EVIDENCE
    # -------------------------------------------------------------------------
    def test_04_missing_expiration_date_yields_missing_evidence(self):
        """4. Missing expiration date on complete package yields MISSING_EVIDENCE (EXPIRATION_DATE_MISSING)."""
        cov = CoverageExtraction(
            coverage_type=CoverageType.GENERAL_LIABILITY,
            expiration_date=None,
        )
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-04",
            is_document_set_complete=True,
            coverages=[cov],
        )
        res = self.evaluator.evaluate(doc_set, [self._make_exp_req()], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "EXPIRATION_DATE_MISSING")
        self.assertTrue(finding.requires_human_review)
        self.assertEqual(res.overall_status, ComplianceStatus.MISSING_EVIDENCE)

    # -------------------------------------------------------------------------
    # Test 5: Missing policy effective date on complete package yields MISSING_EVIDENCE
    # -------------------------------------------------------------------------
    def test_05_missing_effective_date_yields_missing_evidence(self):
        """5. Missing effective date on complete package yields MISSING_EVIDENCE (EFFECTIVE_DATE_MISSING)."""
        cov = CoverageExtraction(
            coverage_type=CoverageType.GENERAL_LIABILITY,
            effective_date=None,
        )
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-05",
            is_document_set_complete=True,
            coverages=[cov],
        )
        res = self.evaluator.evaluate(doc_set, [self._make_eff_req()], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "EFFECTIVE_DATE_MISSING")
        self.assertTrue(finding.requires_human_review)
        self.assertEqual(res.overall_status, ComplianceStatus.MISSING_EVIDENCE)

    # -------------------------------------------------------------------------
    # Test 6: Missing named insured entity name yields MISSING_EVIDENCE
    # -------------------------------------------------------------------------
    def test_06_missing_named_insured_yields_missing_evidence(self):
        """6. Missing named insured entity yields MISSING_EVIDENCE (NAMED_INSURED_NOT_FOUND)."""
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-06",
            is_document_set_complete=True,
            named_insured=None,
        )
        res = self.evaluator.evaluate(doc_set, [self._make_ni_req()], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "NAMED_INSURED_NOT_FOUND")
        self.assertTrue(finding.requires_human_review)
        self.assertEqual(res.overall_status, ComplianceStatus.MISSING_EVIDENCE)

    # -------------------------------------------------------------------------
    # Test 7: Missing limit field on coverage section yields MISSING_EVIDENCE
    # -------------------------------------------------------------------------
    def test_07_missing_limit_field_yields_missing_evidence(self):
        """7. Blank limit field yields MISSING_EVIDENCE (LIMIT_FIELD_NOT_EXTRACTED)."""
        cov = CoverageExtraction(
            coverage_type=CoverageType.GENERAL_LIABILITY,
            each_occurrence=None,
        )
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-07",
            is_document_set_complete=True,
            coverages=[cov],
        )
        res = self.evaluator.evaluate(doc_set, [self._make_gl_req()], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "LIMIT_FIELD_NOT_EXTRACTED")
        self.assertTrue(finding.requires_human_review)
        self.assertEqual(res.overall_status, ComplianceStatus.MISSING_EVIDENCE)

    # -------------------------------------------------------------------------
    # Test 8: Multiple missing evidence findings aggregate to MISSING_EVIDENCE
    # -------------------------------------------------------------------------
    def test_08_multiple_missing_evidence_aggregate_to_missing_evidence(self):
        """8. Submission with multiple missing evidence items aggregates to MISSING_EVIDENCE (never NON_COMPLIANT)."""
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-08",
            is_document_set_complete=True,
            coverages=[],
            endorsements=[],
        )
        reqs = [self._make_gl_req(), self._make_wc_req(), self._make_ai_req()]
        res = self.evaluator.evaluate(doc_set, reqs, self.eval_date)
        self.assertEqual(res.overall_status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertTrue(res.requires_human_review)
        self.assertTrue(all(f.status == ComplianceStatus.MISSING_EVIDENCE for f in res.findings))

    # -------------------------------------------------------------------------
    # Test 9: Confirmed non-compliance takes precedence over missing evidence
    # -------------------------------------------------------------------------
    def test_09_confirmed_failure_takes_precedence_over_missing_evidence(self):
        """9. Confirmed failure (e.g. limit below minimum) results in NON_COMPLIANT overall."""
        cov = CoverageExtraction(
            coverage_type=CoverageType.GENERAL_LIABILITY,
            each_occurrence=MoneyValue(amount_cents=50_000_000, raw_text="$500,000", confidence=0.99),
            evidence=EvidenceReference(document_id="D1", page_number=1, source_text="$500,000"),
        )
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-09",
            is_document_set_complete=True,
            coverages=[cov],
            endorsements=[],  # Missing endorsement
        )
        reqs = [self._make_gl_req(min_cents=100_000_000), self._make_ai_req()]
        res = self.evaluator.evaluate(doc_set, reqs, self.eval_date)
        self.assertEqual(res.overall_status, ComplianceStatus.NON_COMPLIANT)
        self.assertTrue(res.requires_human_review)
        self.assertEqual(res.findings[0].status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(res.findings[1].status, ComplianceStatus.MISSING_EVIDENCE)

    # -------------------------------------------------------------------------
    # Test 10: Verified compliance produces COMPLIANT
    # -------------------------------------------------------------------------
    def test_10_verified_compliance_produces_compliant(self):
        """10. Verified supporting evidence produces COMPLIANT with requires_human_review = False."""
        cov = CoverageExtraction(
            coverage_type=CoverageType.GENERAL_LIABILITY,
            each_occurrence=MoneyValue(amount_cents=100_000_000, raw_text="$1,000,000", confidence=0.99),
            expiration_date=DateValue(date=dt_date(2026, 1, 1), raw_text="01/01/2026", confidence=0.99),
            effective_date=DateValue(date=dt_date(2025, 1, 1), raw_text="01/01/2025", confidence=0.99),
            evidence=EvidenceReference(document_id="D1", page_number=1, source_text="GL $1M"),
        )
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            independent_integrity_verified=True,
            evidence=EvidenceReference(document_id="D2", page_number=1, source_text="Additional Insured form"),
        )
        ni = EntityInfo(
            name="Apex Electrical Contractors LLC",
            evidence=EvidenceReference(document_id="D1", page_number=1, source_text="INSURED: Apex Electrical Contractors LLC"),
        )
        doc_set = ExtractedDocumentSet(
            subcontractor_id="SUB-10",
            is_document_set_complete=True,
            named_insured=ni,
            coverages=[cov],
            endorsements=[end],
        )
        reqs = [self._make_gl_req(), self._make_exp_req(), self._make_eff_req(), self._make_ni_req(), self._make_ai_req()]
        res = self.evaluator.evaluate(doc_set, reqs, self.eval_date)
        self.assertEqual(res.overall_status, ComplianceStatus.COMPLIANT)
        self.assertFalse(res.requires_human_review)
        self.assertTrue(all(f.status == ComplianceStatus.COMPLIANT for f in res.findings))

    # -------------------------------------------------------------------------
    # Test 11: Safety metric - automatic rejection for missing evidence is 0.0%
    # -------------------------------------------------------------------------
    def test_11_automatic_rejection_for_missing_evidence_is_zero(self):
        """11. automatic_rejection_for_missing_evidence_rate is exactly 0.0%."""
        doc = DocumentGroundTruth(
            document_id="D1",
            filename="coi.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            document_type=DocumentType.COI,
            page_count=1,
        )
        req = self._make_wc_req()
        sc = ScenarioGroundTruth(
            scenario_id="TEST-ME-01",
            title="Missing WC",
            description="WC omitted",
            subcontractor_id="SUB-01",
            evaluation_date=self.eval_date,
            document_ids=["D1"],
            documents=[doc],
            requirements=[req],
            expected_findings=[ExpectedFinding(
                requirement_id="REQ_WC_ACCIDENT",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                expected_reason_code="COVERAGE_NOT_FOUND",
                requires_human_review=True,
            )],
            expected_overall_status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            expected_requires_human_review=True,
        )
        res = ScenarioEvaluationResult(
            scenario_id="TEST-ME-01",
            title="Missing WC",
            expected_status="requires_human_review",
            predicted_status="missing_evidence",
            status_matched=False,
            predicted_requires_human_review=True,
            findings_results=[ScenarioFindingResult(
                requirement_id="REQ_WC_ACCIDENT",
                rule_type="coverage_limit",
                expected_status="requires_human_review",
                predicted_status="missing_evidence",
                expected_reason_code="COVERAGE_NOT_FOUND",
                predicted_reason_code="COVERAGE_NOT_FOUND",
            )],
        )
        stats = calculate_submission_metrics([sc], {"TEST-ME-01": res})
        self.assertEqual(stats.automatic_rejection_for_missing_evidence_rate, 0.0)
        self.assertEqual(stats.missing_evidence_review_rate, 1.0)
        self.assertEqual(stats.missing_evidence_scenario_count, 1)

    # -------------------------------------------------------------------------
    # Test 12: Safety metric - automatic approval with unresolved evidence is 0.0%
    # -------------------------------------------------------------------------
    def test_12_automatic_approval_with_unresolved_evidence_is_zero(self):
        """12. automatic_approval_with_unresolved_evidence_rate is exactly 0.0%."""
        doc = DocumentGroundTruth(
            document_id="D1",
            filename="coi.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            document_type=DocumentType.COI,
            page_count=1,
        )
        req = self._make_wc_req()
        sc = ScenarioGroundTruth(
            scenario_id="TEST-ME-02",
            title="Missing WC",
            description="WC omitted",
            subcontractor_id="SUB-02",
            evaluation_date=self.eval_date,
            document_ids=["D1"],
            documents=[doc],
            requirements=[req],
            expected_findings=[ExpectedFinding(
                requirement_id="REQ_WC_ACCIDENT",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                expected_reason_code="COVERAGE_NOT_FOUND",
                requires_human_review=True,
            )],
            expected_overall_status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            expected_requires_human_review=True,
        )
        res = ScenarioEvaluationResult(
            scenario_id="TEST-ME-02",
            title="Missing WC",
            expected_status="requires_human_review",
            predicted_status="missing_evidence",
            status_matched=False,
            predicted_requires_human_review=True,
            findings_results=[ScenarioFindingResult(
                requirement_id="REQ_WC_ACCIDENT",
                rule_type="coverage_limit",
                expected_status="requires_human_review",
                predicted_status="missing_evidence",
                expected_reason_code="COVERAGE_NOT_FOUND",
                predicted_reason_code="COVERAGE_NOT_FOUND",
            )],
        )
        stats = calculate_submission_metrics([sc], {"TEST-ME-02": res})
        self.assertEqual(stats.automatic_approval_with_unresolved_evidence_rate, 0.0)

    # -------------------------------------------------------------------------
    # Test 13: Human review immutability preserves original result_json
    # -------------------------------------------------------------------------
    def test_13_human_review_immutability_preserves_result_json(self):
        """13. Recording human review leaves automated result_json completely unmodified in SQLite."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            init_db(db_path)
            repo = SubmissionRepository(db_path=db_path)

            sub_id = f"sub_{uuid4().hex[:12]}"
            initial_result = {
                "review": {
                    "overall_status": "missing_evidence",
                    "requires_human_review": True,
                    "findings": [
                        {
                            "requirement_id": "REQ_AI",
                            "status": "missing_evidence",
                            "reason_code": "ENDORSEMENT_NOT_FOUND",
                        }
                    ],
                }
            }
            repo.create_submission(
                submission_id=sub_id,
                name="Test Submission",
                overall_status="missing_evidence",
                processing_status="completed",
                provider="mock",
                evaluation_date="2025-06-01",
                result_data=initial_result,
            )

            # Record human review decision
            review_rec = repo.add_review_record(
                submission_id=sub_id,
                review_status="accepted_exception",
                reviewer_notes="GC approved exception based on project variance.",
                finding_id="REQ_AI",
                reason_code="ENDORSEMENT_NOT_FOUND",
                reviewer_name="Sarah Jenkins",
                update_submission_status="compliant",
            )
            self.assertIsNotNone(review_rec["id"])
            self.assertEqual(review_rec["reviewer_name"], "Sarah Jenkins")

            # Retrieve submission from database
            stored = repo.get_submission_by_id(sub_id)
            self.assertEqual(stored["overall_status"], "compliant")
            # Result JSON must remain completely unchanged!
            self.assertEqual(stored["result"]["review"]["overall_status"], "missing_evidence")
            self.assertEqual(
                stored["result"]["review"]["findings"][0]["status"],
                "missing_evidence",
            )
            self.assertEqual(len(stored["reviews"]), 1)

    # -------------------------------------------------------------------------
    # Test 14: Four-class and Five-class projections preserve review routing
    # -------------------------------------------------------------------------
    def test_14_four_class_and_five_class_projections_preserve_review_routing(self):
        """14. In 4-class and 5-class metrics, MISSING_EVIDENCE is cleanly tracked and routed to review."""
        doc = DocumentGroundTruth(
            document_id="D1",
            filename="coi.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            document_type=DocumentType.COI,
            page_count=1,
        )
        sc = ScenarioGroundTruth(
            scenario_id="TEST-4CLS",
            title="Missing Endorsement",
            description="Endorsement absent",
            subcontractor_id="SUB-01",
            evaluation_date=self.eval_date,
            document_ids=["D1"],
            documents=[doc],
            requirements=[self._make_ai_req()],
            expected_findings=[ExpectedFinding(
                requirement_id="REQ_ADDITIONAL_INSURED",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE,
                expected_status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                expected_reason_code="ENDORSEMENT_NOT_FOUND",
                requires_human_review=True,
            )],
            expected_overall_status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            expected_requires_human_review=True,
        )
        res = ScenarioEvaluationResult(
            scenario_id="TEST-4CLS",
            title="Missing Endorsement",
            expected_status="requires_human_review",
            predicted_status="missing_evidence",
            status_matched=False,
            predicted_requires_human_review=True,
            findings_results=[ScenarioFindingResult(
                requirement_id="REQ_ADDITIONAL_INSURED",
                rule_type="endorsement_presence",
                expected_status="requires_human_review",
                predicted_status="missing_evidence",
                expected_reason_code="ENDORSEMENT_NOT_FOUND",
                predicted_reason_code="ENDORSEMENT_NOT_FOUND",
            )],
        )
        stats = calculate_submission_metrics([sc], {"TEST-4CLS": res})
        # 4-class projects non-4-class into unable_to_determine
        cm4 = stats.confusion_matrix_4class
        self.assertEqual(cm4["requires_human_review"]["unable_to_determine"], 1)
        # 5-class explicitly distinguishes missing_evidence
        cm5 = stats.confusion_matrix_5class
        self.assertEqual(cm5["requires_human_review"]["missing_evidence"], 1)
        # Missing evidence review rate is 100%
        self.assertEqual(stats.missing_evidence_review_rate, 1.0)
        self.assertEqual(stats.automatic_rejection_for_missing_evidence_rate, 0.0)
        self.assertEqual(stats.automatic_approval_with_unresolved_evidence_rate, 0.0)

    # -------------------------------------------------------------------------
    # Test 15: Scenario regressions for SCEN-13, 14, 19, 22, 25, 26, 35, 39, 40
    # -------------------------------------------------------------------------
    def test_15_cohort_scenario_regressions(self):
        """15. Regressions verifying deterministic rule evaluation across edge cohort scenarios."""
        # SCEN-13: Missing Workers' Comp on complete submission -> MISSING_EVIDENCE
        res_13 = self.evaluator.evaluate(
            ExtractedDocumentSet(subcontractor_id="SCEN-13", is_document_set_complete=True, coverages=[]),
            [self._make_wc_req()],
            self.eval_date,
        )
        self.assertEqual(res_13.overall_status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(res_13.findings[0].reason_code, "COVERAGE_NOT_FOUND")

        # SCEN-14: Missing Additional Insured Endorsement -> MISSING_EVIDENCE
        res_14 = self.evaluator.evaluate(
            ExtractedDocumentSet(subcontractor_id="SCEN-14", is_document_set_complete=True, endorsements=[]),
            [self._make_ai_req()],
            self.eval_date,
        )
        self.assertEqual(res_14.overall_status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(res_14.findings[0].reason_code, "ENDORSEMENT_NOT_FOUND")

        # SCEN-19: Missing Auto Liability and Workers' Comp -> MISSING_EVIDENCE
        res_19 = self.evaluator.evaluate(
            ExtractedDocumentSet(subcontractor_id="SCEN-19", is_document_set_complete=True, coverages=[]),
            [self._make_gl_req(), self._make_wc_req()],
            self.eval_date,
        )
        self.assertEqual(res_19.overall_status, ComplianceStatus.MISSING_EVIDENCE)

        # SCEN-22: Corporate suffix mismatch ('Apex Electrical Inc' vs 'LLC') -> CORPORATE_SUFFIX_DISCREPANCY
        ni_22 = EntityInfo(name="Apex Electrical Inc", evidence=EvidenceReference(document_id="D1", page_number=1, source_text="Apex Electrical Inc"))
        res_22 = self.evaluator.evaluate(
            ExtractedDocumentSet(subcontractor_id="SCEN-22", is_document_set_complete=True, named_insured=ni_22),
            [self._make_ni_req("Apex Electrical Contractors LLC")],
            self.eval_date,
        )
        self.assertEqual(res_22.findings[0].status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(res_22.findings[0].reason_code, "NAMED_INSURED_CORPORATE_SUFFIX_DISCREPANCY")

        # SCEN-25: Low confidence extraction (<0.80) -> UNABLE_TO_DETERMINE (EXTRACTION_CONFIDENCE_LOW)
        cov_25 = CoverageExtraction(
            coverage_type=CoverageType.GENERAL_LIABILITY,
            each_occurrence=MoneyValue(amount_cents=100_000_000, raw_text="$1,000,000", confidence=0.55),
            evidence=EvidenceReference(document_id="D1", page_number=1, source_text="$1,000,000", confidence=0.55),
        )
        res_25 = self.evaluator.evaluate(
            ExtractedDocumentSet(subcontractor_id="SCEN-25", is_document_set_complete=True, coverages=[cov_25]),
            [self._make_gl_req()],
            self.eval_date,
        )
        self.assertEqual(res_25.findings[0].status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(res_25.findings[0].reason_code, "EXTRACTION_CONFIDENCE_LOW")

        # SCEN-26: DBA variation ('Apex Power Solutions LLC dba Apex Electrical') -> PARTIAL_MATCH
        ni_26 = EntityInfo(name="Apex Power Solutions LLC dba Apex Electrical", evidence=EvidenceReference(document_id="D1", page_number=1, source_text="Apex Power Solutions LLC dba Apex Electrical"))
        res_26 = self.evaluator.evaluate(
            ExtractedDocumentSet(subcontractor_id="SCEN-26", is_document_set_complete=True, named_insured=ni_26),
            [self._make_ni_req("Apex Electrical Contractors LLC")],
            self.eval_date,
        )
        self.assertEqual(res_26.findings[0].status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(res_26.findings[0].reason_code, "NAMED_INSURED_PARTIAL_MATCH")

        # SCEN-35: Contradictory limits across documents -> UNABLE_TO_DETERMINE (CONTRADICTORY_LIMITS_FOUND)
        cov_35a = CoverageExtraction(
            coverage_type=CoverageType.GENERAL_LIABILITY,
            each_occurrence=MoneyValue(amount_cents=100_000_000, raw_text="$1,000,000", confidence=0.98),
            evidence=EvidenceReference(document_id="D1", page_number=1, source_text="$1M"),
        )
        cov_35b = CoverageExtraction(
            coverage_type=CoverageType.GENERAL_LIABILITY,
            each_occurrence=MoneyValue(amount_cents=200_000_000, raw_text="$2,000,000", confidence=0.98),
            evidence=EvidenceReference(document_id="D2", page_number=1, source_text="$2M"),
        )
        res_35 = self.evaluator.evaluate(
            ExtractedDocumentSet(subcontractor_id="SCEN-35", is_document_set_complete=True, coverages=[cov_35a, cov_35b]),
            [self._make_gl_req()],
            self.eval_date,
        )
        self.assertEqual(res_35.findings[0].status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(res_35.findings[0].reason_code, "CONTRADICTORY_LIMITS_FOUND")

        # SCEN-39: Fabricated quote / missing evidence text with mandatory evidence -> MISSING_EVIDENCE
        end_39 = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            evidence=None,  # Missing evidence quote
        )
        req_mandatory = InsuranceRequirement(
            requirement_id="REQ_ADDITIONAL_INSURED",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="AI endorsement",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            mandatory_evidence=True,
        )
        res_39 = self.evaluator.evaluate(
            ExtractedDocumentSet(subcontractor_id="SCEN-39", is_document_set_complete=True, endorsements=[end_39]),
            [req_mandatory],
            self.eval_date,
        )
        self.assertEqual(res_39.findings[0].status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(res_39.findings[0].reason_code, "ENDORSEMENT_EVIDENCE_TEXT_MISSING")

        # SCEN-40: Missing evidence reference on mandatory requirement -> MISSING_EVIDENCE
        res_40 = self.evaluator.evaluate(
            ExtractedDocumentSet(subcontractor_id="SCEN-40", is_document_set_complete=True, endorsements=[end_39]),
            [req_mandatory],
            self.eval_date,
        )
        self.assertEqual(res_40.findings[0].status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(res_40.findings[0].reason_code, "ENDORSEMENT_EVIDENCE_TEXT_MISSING")


if __name__ == "__main__":
    unittest.main()
