"""Milestone 3.2A Evaluator Semantics and Safety Metrics Unit and Adversarial Tests.

Covers all 12 Phase 7 test items:
1. Missing required coverage in a verified complete package.
2. Missing coverage in an incomplete package.
3. Missing endorsement document.
4. Ambiguous endorsement wording.
5. Verified standard endorsement.
6. Unverified standard endorsement.
7. Modified endorsement.
8. Contradictory endorsement evidence.
9. Policy linkage mismatch.
10. Reason-code consistency (POLICY_ACTIVE vs POLICY_ACTIVE_AND_CURRENT).
11. Four-class finding metrics (confusion matrix, per-class metrics, macro F1, explicit binary F1).
12. Zero automated pass rate reporting and throughput rates.
"""

from __future__ import annotations

import unittest
from datetime import date as dt_date
from typing import Dict, List

from app.compliance.evaluator import ComplianceEvaluator
from app.schemas.insurance import (
    ComplianceFinding,
    ComplianceStatus,
    CoverageExtraction,
    CoverageType,
    DateValue,
    DocumentType,
    EndorsementExtraction,
    EndorsementType,
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
    calculate_finding_metrics,
    calculate_safety_metrics,
    calculate_submission_metrics,
)
from evaluation.runners.scenario_runner import are_reason_codes_equivalent
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


class TestMilestone32AEvaluatorSemantics(unittest.TestCase):
    """Evaluator semantics and provenance verification."""

    def setUp(self):
        self.evaluator = ComplianceEvaluator()
        self.eval_date = dt_date(2025, 6, 1)

    def _make_gl_req(self) -> InsuranceRequirement:
        return InsuranceRequirement(
            requirement_id="REQ_GL_OCC",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="GL Each Occurrence limit >= $1M",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            limit_field=LimitField.EACH_OCCURRENCE,
            min_amount_cents=100_000_000,
            severity=Severity.CRITICAL,
        )

    def _make_wc_req(self) -> InsuranceRequirement:
        return InsuranceRequirement(
            requirement_id="REQ_WC_ACC",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="Workers Comp limit >= $1M",
            coverage_type=CoverageType.WORKERS_COMP,
            limit_field=LimitField.PER_ACCIDENT,
            min_amount_cents=100_000_000,
            severity=Severity.CRITICAL,
        )

    def _make_ai_req(self) -> InsuranceRequirement:
        return InsuranceRequirement(
            requirement_id="REQ_AI",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured endorsement mandatory",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            severity=Severity.CRITICAL,
        )

    # 1. Missing required coverage in a verified complete package
    def test_item_01_missing_coverage_in_complete_package(self):
        """Missing required coverage on a complete upload returns MISSING_EVIDENCE (COVERAGE_NOT_FOUND) with review."""
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

    # 2. Missing coverage in an incomplete package
    def test_item_02_missing_coverage_in_incomplete_package(self):
        """Missing coverage in an unverified/incomplete package returns UNABLE_TO_DETERMINE."""
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

    # 3. Missing endorsement document
    def test_item_03_missing_endorsement_document(self):
        """Endorsement omitted on complete package returns MISSING_EVIDENCE (ENDORSEMENT_NOT_FOUND)."""
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

    # 4. Ambiguous endorsement wording
    def test_item_04_ambiguous_endorsement_wording(self):
        """Endorsement with unverified semantic support returns REQUIRES_HUMAN_REVIEW."""
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            extraction_status=ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT,
            evidence=EvidenceReference(document_id="D1", page_number=1, source_text="Uncertain text"),
        )
        doc_set = ExtractedDocumentSet(subcontractor_id="SUB-04", is_document_set_complete=True, endorsements=[end])
        res = self.evaluator.evaluate(doc_set, [self._make_ai_req()], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_EVIDENCE_SUPPORT_UNVERIFIED")

    # 5. Verified standard endorsement
    def test_item_05_verified_standard_endorsement(self):
        """Verified standard ISO form with independent integrity proof passes as COMPLIANT."""
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=True,
            confidence=0.98,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=EvidenceReference(document_id="D1", page_number=1, source_text="CG 20 10 04 13 Additional Insured"),
        )
        doc_set = ExtractedDocumentSet(subcontractor_id="SUB-05", is_document_set_complete=True, endorsements=[end])
        res = self.evaluator.evaluate(doc_set, [self._make_ai_req()], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_VERIFIED")
        self.assertFalse(finding.requires_human_review)

    # 6. Unverified standard endorsement
    def test_item_06_unverified_standard_endorsement(self):
        """Standard form lacking independent integrity verification routes to REQUIRES_HUMAN_REVIEW."""
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=False,
            confidence=0.98,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=EvidenceReference(document_id="D1", page_number=1, source_text="CG 20 10 04 13 Additional Insured"),
        )
        doc_set = ExtractedDocumentSet(subcontractor_id="SUB-06", is_document_set_complete=True, endorsements=[end])
        res = self.evaluator.evaluate(doc_set, [self._make_ai_req()], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_VERIFICATION_UNSUPPORTED")

    # 7. Modified endorsement
    def test_item_07_modified_endorsement(self):
        """Endorsement with detected manuscript restriction routes to REQUIRES_HUMAN_REVIEW."""
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            is_standard_form=False,
            manuscript_clause_detected=True,
            manuscript_status="DETECTED",
            restriction_description="Excludes residential work",
            restriction_reason_code="MANUSCRIPT_RESTRICTION_DETECTED",
            evidence=EvidenceReference(document_id="D1", page_number=1, source_text="Residential work excluded"),
        )
        doc_set = ExtractedDocumentSet(subcontractor_id="SUB-07", is_document_set_complete=True, endorsements=[end])
        res = self.evaluator.evaluate(doc_set, [self._make_ai_req()], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_RESTRICTION_DETECTED")

    # 8. Contradictory endorsement evidence
    def test_item_08_contradictory_endorsement_evidence(self):
        """Multiple endorsements linked to different policies triggers CONTRADICTORY_ENDORSEMENTS_FOUND."""
        end1 = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            applicable_policy="POL-111",
            evidence=EvidenceReference(document_id="D1", page_number=1, source_text="POL-111"),
        )
        end2 = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            applicable_policy="POL-222",
            evidence=EvidenceReference(document_id="D2", page_number=1, source_text="POL-222"),
        )
        doc_set = ExtractedDocumentSet(subcontractor_id="SUB-08", is_document_set_complete=True, endorsements=[end1, end2])
        res = self.evaluator.evaluate(doc_set, [self._make_ai_req()], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "CONTRADICTORY_ENDORSEMENTS_FOUND")

    # 9. Policy linkage mismatch
    def test_item_09_policy_linkage_mismatch(self):
        """Multiple endorsements with conflicting entity schedules triggers CONTRADICTORY_ENDORSEMENTS_FOUND."""
        end1 = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            named_entities=["Prime Contractor Corp"],
            evidence=EvidenceReference(document_id="D1", page_number=1, source_text="Prime Contractor Corp"),
        )
        end2 = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            named_entities=["Different Entity Inc"],
            evidence=EvidenceReference(document_id="D2", page_number=1, source_text="Different Entity Inc"),
        )
        doc_set = ExtractedDocumentSet(subcontractor_id="SUB-09", is_document_set_complete=True, endorsements=[end1, end2])
        res = self.evaluator.evaluate(doc_set, [self._make_ai_req()], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "CONTRADICTORY_ENDORSEMENTS_FOUND")

    # 10. Reason-code consistency
    def test_item_10_reason_code_consistency(self):
        """are_reason_codes_equivalent treats POLICY_ACTIVE and POLICY_ACTIVE_AND_CURRENT as equivalent."""
        self.assertTrue(are_reason_codes_equivalent("POLICY_ACTIVE", "POLICY_ACTIVE_AND_CURRENT"))
        self.assertTrue(are_reason_codes_equivalent("POLICY_ACTIVE_AND_CURRENT", "POLICY_ACTIVE"))
        self.assertTrue(are_reason_codes_equivalent("POLICY_ACTIVE", "POLICY_ACTIVE"))
        self.assertFalse(are_reason_codes_equivalent("POLICY_ACTIVE", "POLICY_EXPIRED"))
        self.assertFalse(are_reason_codes_equivalent("LIMIT_MEETS_OR_EXCEEDS_MINIMUM", "LIMIT_BELOW_MINIMUM"))


class TestMilestone32AMetricsAndSafetyReporting(unittest.TestCase):
    """Metrics calculation, four-class macro F1, and safety throughput reporting tests."""

    # 11. Four-class finding metrics
    def test_item_11_four_class_finding_metrics(self):
        """calculate_finding_metrics calculates four-class macro F1, per-class stats, and explicit binary metrics."""
        findings = [
            ScenarioFindingResult(
                requirement_id="REQ_1",
                rule_type="coverage_limit",
                expected_status="compliant",
                predicted_status="compliant",
                status_matched=True,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                predicted_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                reason_code_matched=True,
            ),
            ScenarioFindingResult(
                requirement_id="REQ_2",
                rule_type="coverage_limit",
                expected_status="non_compliant",
                predicted_status="non_compliant",
                status_matched=True,
                expected_reason_code="LIMIT_BELOW_MINIMUM",
                predicted_reason_code="LIMIT_BELOW_MINIMUM",
                reason_code_matched=True,
            ),
            ScenarioFindingResult(
                requirement_id="REQ_3",
                rule_type="endorsement_presence",
                expected_status="requires_human_review",
                predicted_status="requires_human_review",
                status_matched=True,
                expected_reason_code="MANUSCRIPT_RESTRICTION_DETECTED",
                predicted_reason_code="MANUSCRIPT_RESTRICTION_DETECTED",
                reason_code_matched=True,
            ),
            ScenarioFindingResult(
                requirement_id="REQ_4",
                rule_type="policy_expiration",
                expected_status="unable_to_determine",
                predicted_status="unable_to_determine",
                status_matched=True,
                expected_reason_code="EXTRACTION_CONFIDENCE_LOW",
                predicted_reason_code="EXTRACTION_CONFIDENCE_LOW",
                reason_code_matched=True,
            ),
        ]

        metrics = calculate_finding_metrics(findings)
        self.assertEqual(metrics.total_findings, 4)
        self.assertEqual(metrics.exact_status_matches, 4)
        self.assertEqual(metrics.exact_status_accuracy, 1.0)
        self.assertEqual(metrics.four_class_status_accuracy, 1.0)
        self.assertEqual(metrics.four_class_macro_f1, 1.0)

        # Check per-class metrics
        for cls in FOUR_CLASSES:
            self.assertIn(cls, metrics.per_class_metrics)
            self.assertEqual(metrics.per_class_metrics[cls]["f1"], 1.0)

        # Check explicit binary compliant metrics
        self.assertEqual(metrics.binary_compliant_precision, 1.0)
        self.assertEqual(metrics.binary_compliant_recall, 1.0)
        self.assertEqual(metrics.binary_compliant_f1, 1.0)
        # Check backward-compatible legacy fields
        self.assertEqual(metrics.precision, 1.0)
        self.assertEqual(metrics.recall, 1.0)
        self.assertEqual(metrics.f1_score, 1.0)

    # 12. Zero automated pass rate reporting and safety throughput metrics
    def test_item_12_zero_automated_pass_rate_reporting(self):
        """calculate_submission_metrics tracks automated pass throughput alongside safety metrics."""
        doc1 = DocumentGroundTruth(
            document_id="D1",
            filename="doc1.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            document_type=DocumentType.COI,
            page_count=1,
        )
        doc2 = DocumentGroundTruth(
            document_id="D2",
            filename="doc2.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            document_type=DocumentType.COI,
            page_count=1,
        )

        scenarios = [
            ScenarioGroundTruth(
                scenario_id="SCEN-01",
                title="Compliant Scenario",
                description="test",
                subcontractor_id="S1",
                document_ids=["D1"],
                documents=[doc1],
                evaluation_date=dt_date(2025, 6, 1),
                expected_overall_status=ComplianceStatus.COMPLIANT,
                expected_requires_human_review=False,
            ),
            ScenarioGroundTruth(
                scenario_id="SCEN-02",
                title="Non-compliant Scenario",
                description="test",
                subcontractor_id="S2",
                document_ids=["D2"],
                documents=[doc2],
                evaluation_date=dt_date(2025, 6, 1),
                expected_overall_status=ComplianceStatus.NON_COMPLIANT,
                expected_requires_human_review=True,
            ),
        ]

        # Simulate system that passed ZERO submissions (routed all to review)
        results = {
            "SCEN-01": ScenarioEvaluationResult(
                scenario_id="SCEN-01",
                title="Compliant Scenario",
                expected_status="compliant",
                predicted_status="requires_human_review",
                status_matched=False,
            ),
            "SCEN-02": ScenarioEvaluationResult(
                scenario_id="SCEN-02",
                title="Non-compliant Scenario",
                expected_status="non_compliant",
                predicted_status="requires_human_review",
                status_matched=False,
            ),
        }

        sub_stats = calculate_submission_metrics(scenarios, results)
        fcr, ucr = calculate_safety_metrics(scenarios, results)

        # Safety metrics trivially 0.00%
        self.assertEqual(fcr.numerator, 0)
        self.assertEqual(ucr.numerator, 0)
        self.assertEqual(fcr.rate_percentage, 0.0)
        self.assertEqual(ucr.rate_percentage, 0.0)

        # Operational throughput reflects zero approvals
        self.assertEqual(sub_stats.automated_pass_rate, 0.0)
        self.assertEqual(sub_stats.human_review_rate, 1.0)
        self.assertEqual(sub_stats.correct_automated_pass_rate, 0.0)
        self.assertEqual(sub_stats.false_approval_count, 0)
        self.assertEqual(sub_stats.false_rejection_count, 0)


if __name__ == "__main__":
    unittest.main()
