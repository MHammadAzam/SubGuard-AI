"""Unit and integration tests for SubGuard AI Phase 7.2 Benchmark Infrastructure.

Tests cover:
1. Ground-truth schema validation & constraints (duplicates, missing docs, invalid pages, invalid evidence).
2. Manifest hash calculation and tamper detection.
3. Safety metric calculations (FCR, UCR, zero-denominator handling).
4. Four-class confusion matrix and submission metrics.
5. Finding-level metrics and human-review escalation metrics.
6. Evidence grounding and support classification.
7. Runner error isolation and deterministic execution.
8. Adversarial metric test cases (A, B, C, D, E, F).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date as dt_date
from pathlib import Path

from app.schemas.insurance import (
    ComplianceStatus,
    CoverageType,
    DocumentType,
    EndorsementType,
    InsuranceRequirement,
    LimitField,
    RequirementType,
    Severity,
)
from evaluation.cohorts.manifest import compute_file_sha256, verify_manifest
from evaluation.metrics.compliance_metrics import (
    calculate_evidence_metrics,
    calculate_finding_metrics,
    calculate_human_review_metrics,
    calculate_safety_metrics,
    calculate_submission_metrics,
)
from evaluation.runners.scenario_runner import execute_cohort_benchmark
from evaluation.schemas.ground_truth import (
    DocumentGroundTruth,
    DocumentSourceType,
    ExpectedFinding,
    FieldPresenceStatus,
    ScenarioEvaluationDataset,
    ScenarioGroundTruth,
)
from evaluation.schemas.reports import (
    ScenarioEvaluationResult,
    ScenarioFindingResult,
)


def _make_dummy_scenario(
    scenario_id: str,
    status: ComplianceStatus = ComplianceStatus.COMPLIANT,
    requires_hr: bool = False,
    page_count: int = 1,
) -> ScenarioGroundTruth:
    doc = DocumentGroundTruth(
        document_id=f"{scenario_id}-DOC1",
        filename=f"{scenario_id}_coi.pdf",
        source_type=DocumentSourceType.SYNTHETIC,
        document_type=DocumentType.COI,
        page_count=page_count,
    )
    req = InsuranceRequirement(
        requirement_id="REQ_GL_OCC",
        rule_type=RequirementType.COVERAGE_LIMIT,
        description="GL limit >= $1M",
        coverage_type=CoverageType.GENERAL_LIABILITY,
        limit_field=LimitField.EACH_OCCURRENCE,
        min_amount_cents=100_000_000,
    )
    finding = ExpectedFinding(
        requirement_id="REQ_GL_OCC",
        rule_type=RequirementType.COVERAGE_LIMIT,
        expected_status=status,
        expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM" if status == ComplianceStatus.COMPLIANT else "LIMIT_BELOW_MINIMUM",
        requires_human_review=requires_hr,
        supporting_document_id=f"{scenario_id}-DOC1",
        supporting_page=1,
    )
    return ScenarioGroundTruth(
        scenario_id=scenario_id,
        title=f"Test Scenario {scenario_id}",
        description="Test description",
        subcontractor_id="SUB-001",
        evaluation_date=dt_date(2025, 6, 1),
        document_ids=[f"{scenario_id}-DOC1"],
        documents=[doc],
        requirements=[req],
        expected_findings=[finding],
        expected_overall_status=status,
        expected_requires_human_review=requires_hr,
    )


class TestBenchmarkSchemaValidation(unittest.TestCase):
    """Test schema contracts and integrity validations."""

    def test_valid_scenario_instantiation(self):
        sc = _make_dummy_scenario("SCEN-TEST-01")
        self.assertEqual(sc.scenario_id, "SCEN-TEST-01")
        self.assertEqual(sc.expected_overall_status, ComplianceStatus.COMPLIANT)
        self.assertFalse(sc.expected_requires_human_review)

    def test_duplicate_scenario_rejection(self):
        sc1 = _make_dummy_scenario("SCEN-DUP")
        sc2 = _make_dummy_scenario("SCEN-DUP")
        with self.assertRaises(ValueError) as ctx:
            ScenarioEvaluationDataset(
                dataset_id="test_ds",
                name="Test Dataset",
                scenarios=[sc1, sc2],
            )
        self.assertIn("Duplicate scenario_id 'SCEN-DUP'", str(ctx.exception))

    def test_missing_document_reference_rejection(self):
        doc = DocumentGroundTruth(
            document_id="DOC-A",
            filename="doc_a.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            page_count=1,
        )
        with self.assertRaises(ValueError) as ctx:
            ScenarioGroundTruth(
                scenario_id="SCEN-MISSING-DOC",
                title="Title",
                description="Desc",
                subcontractor_id="SUB-1",
                evaluation_date=dt_date(2025, 6, 1),
                document_ids=["DOC-A", "DOC-B_NONEXISTENT"],
                documents=[doc],
                expected_overall_status=ComplianceStatus.COMPLIANT,
                expected_requires_human_review=False,
            )
        self.assertIn("not found in documents", str(ctx.exception))

    def test_invalid_page_reference_rejection(self):
        doc = DocumentGroundTruth(
            document_id="DOC-1PAGE",
            filename="doc1.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            page_count=1,
        )
        finding = ExpectedFinding(
            requirement_id="REQ_1",
            rule_type=RequirementType.COVERAGE_LIMIT,
            expected_status=ComplianceStatus.COMPLIANT,
            expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
            requires_human_review=False,
            supporting_document_id="DOC-1PAGE",
            supporting_page=5,  # Exceeds doc page count of 1!
        )
        with self.assertRaises(ValueError) as ctx:
            ScenarioGroundTruth(
                scenario_id="SCEN-PAGE-ERR",
                title="Title",
                description="Desc",
                subcontractor_id="SUB-1",
                evaluation_date=dt_date(2025, 6, 1),
                document_ids=["DOC-1PAGE"],
                documents=[doc],
                expected_findings=[finding],
                expected_overall_status=ComplianceStatus.COMPLIANT,
                expected_requires_human_review=False,
            )
        self.assertIn("exceeding document page count 1", str(ctx.exception))

    def test_contradictory_annotation_status_rejection(self):
        # Scenario cannot have expected_overall_status=COMPLIANT and expected_requires_human_review=True
        with self.assertRaises(ValueError) as ctx:
            ScenarioGroundTruth(
                scenario_id="SCEN-CONTRADICTORY-FLAGS",
                title="Title",
                description="Desc",
                subcontractor_id="SUB-1",
                evaluation_date=dt_date(2025, 6, 1),
                document_ids=[],
                documents=[],
                expected_overall_status=ComplianceStatus.COMPLIANT,
                expected_requires_human_review=True,
            )
        self.assertIn("cannot have expected_overall_status=COMPLIANT and expected_requires_human_review=True", str(ctx.exception))


class TestManifestVerification(unittest.TestCase):
    """Test frozen manifest hashing and tamper detection."""

    def test_cohort_v1_manifest_validity(self):
        manifest_path = Path("evaluation/cohorts/manifests/cohort_v1_manifest.json")
        self.assertTrue(manifest_path.exists(), f"Manifest file missing: {manifest_path}")
        is_valid, errors = verify_manifest(manifest_path)
        self.assertTrue(is_valid, f"Cohort manifest verification failed: {errors}")
        self.assertEqual(len(errors), 0)

    def test_tamper_detection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            test_file = tmppath / "test.pdf"
            test_file.write_bytes(b"dummy pdf content")
            real_hash = compute_file_sha256(test_file)

            manifest_content = {
                "manifest_version": "test_v1",
                "documents": [
                    {
                        "scenario_id": "SCEN-01",
                        "document_id": "DOC-1",
                        "relative_path": "test.pdf",
                        "sha256": "wrong_hash_tampered_00000000000000000000000000000000000000000000",
                    }
                ],
            }
            manifest_file = tmppath / "manifest.json"
            manifest_file.write_text(json.dumps(manifest_content))

            is_valid, errors = verify_manifest(manifest_file, base_dir=tmppath)
            self.assertFalse(is_valid)
            self.assertTrue(any("hash mismatch" in err for err in errors))


class TestSafetyAndComplianceMetrics(unittest.TestCase):
    """Test mathematical formulas and edge cases for safety and compliance metrics."""

    def test_zero_denominator_handling(self):
        # When there are 0 non-compliant scenarios, FCR must report N/A, not crash or 0%
        compliant_sc = _make_dummy_scenario("SCEN-COMP", ComplianceStatus.COMPLIANT, requires_hr=False)
        results = {
            "SCEN-COMP": ScenarioEvaluationResult(
                scenario_id="SCEN-COMP",
                title="Compliant",
                expected_status="compliant",
                predicted_status="compliant",
                status_matched=True,
                processing_status="success",
            )
        }
        fcr, ucr = calculate_safety_metrics([compliant_sc], results)
        self.assertEqual(fcr.denominator, 0)
        self.assertIsNone(fcr.rate_percentage)
        self.assertEqual(fcr.rate_string, "N/A")

        self.assertEqual(ucr.denominator, 0)
        self.assertIsNone(ucr.rate_percentage)
        self.assertEqual(ucr.rate_string, "N/A")

    def test_four_class_confusion_matrix(self):
        sc_comp = _make_dummy_scenario("S1", ComplianceStatus.COMPLIANT, requires_hr=False)
        sc_non = _make_dummy_scenario("S2", ComplianceStatus.NON_COMPLIANT, requires_hr=True)

        results = {
            "S1": ScenarioEvaluationResult(
                scenario_id="S1", title="", expected_status="compliant",
                predicted_status="compliant", status_matched=True, processing_status="success",
            ),
            "S2": ScenarioEvaluationResult(
                scenario_id="S2", title="", expected_status="non_compliant",
                predicted_status="non_compliant", status_matched=True, processing_status="success",
            ),
        }
        sub_metrics = calculate_submission_metrics([sc_comp, sc_non], results)
        cm = sub_metrics.confusion_matrix_4class
        self.assertEqual(cm["compliant"]["compliant"], 1)
        self.assertEqual(cm["non_compliant"]["non_compliant"], 1)
        self.assertEqual(cm["compliant"]["non_compliant"], 0)
        self.assertEqual(sub_metrics.overall_accuracy, 1.0)
        self.assertEqual(sub_metrics.macro_f1, 1.0)

    def test_finding_level_metrics(self):
        f1 = ScenarioFindingResult(
            requirement_id="R1", rule_type="coverage_limit",
            expected_status="compliant", predicted_status="compliant",
            status_matched=True, expected_reason_code="LIMIT_OK", predicted_reason_code="LIMIT_OK",
            reason_code_matched=True, expected_human_review=False, predicted_human_review=False,
            human_review_matched=True,
        )
        f2 = ScenarioFindingResult(
            requirement_id="R2", rule_type="coverage_limit",
            expected_status="non_compliant", predicted_status="non_compliant",
            status_matched=True, expected_reason_code="LIMIT_LOW", predicted_reason_code="LIMIT_LOW",
            reason_code_matched=True, expected_human_review=True, predicted_human_review=True,
            human_review_matched=True,
        )
        metrics = calculate_finding_metrics([f1, f2])
        self.assertEqual(metrics.total_findings, 2)
        self.assertEqual(metrics.exact_status_accuracy, 1.0)
        self.assertEqual(metrics.reason_code_accuracy, 1.0)
        self.assertEqual(metrics.human_review_accuracy, 1.0)

    def test_human_review_escalation_metrics(self):
        sc_clean = _make_dummy_scenario("SC-CLEAN", ComplianceStatus.COMPLIANT, requires_hr=False)
        sc_review = _make_dummy_scenario("SC-REV", ComplianceStatus.REQUIRES_HUMAN_REVIEW, requires_hr=True)

        # Both predicted correctly
        results = {
            "SC-CLEAN": ScenarioEvaluationResult(
                scenario_id="SC-CLEAN", title="", expected_status="compliant",
                predicted_status="compliant", status_matched=True, processing_status="success",
                predicted_requires_human_review=False,
            ),
            "SC-REV": ScenarioEvaluationResult(
                scenario_id="SC-REV", title="", expected_status="requires_human_review",
                predicted_status="requires_human_review", status_matched=True, processing_status="success",
                predicted_requires_human_review=True,
            ),
        }
        hr_stats = calculate_human_review_metrics([sc_clean, sc_review], results)
        self.assertEqual(hr_stats.uncertainty_recall, 1.0)
        self.assertEqual(hr_stats.unnecessary_review_rate, 0.0)
        self.assertEqual(hr_stats.missed_review_rate, 0.0)
        self.assertEqual(hr_stats.human_review_precision, 1.0)


class TestAdversarialMetrics(unittest.TestCase):
    """Adversarial scenario tests (A, B, C, D, E, F) specified in Phase 7.2 requirements."""

    def test_adversarial_A_non_compliant_predicted_compliant_increases_fcr_and_ucr(self):
        """Case A: A NON_COMPLIANT scenario predicted COMPLIANT must increase both FCR and UCR."""
        sc = _make_dummy_scenario("SCEN-A", ComplianceStatus.NON_COMPLIANT, requires_hr=True)
        results = {
            "SCEN-A": ScenarioEvaluationResult(
                scenario_id="SCEN-A",
                title="",
                expected_status="non_compliant",
                predicted_status="compliant",  # Unsafe false compliant pass!
                status_matched=False,
                processing_status="success",
            )
        }
        fcr, ucr = calculate_safety_metrics([sc], results)
        self.assertEqual(fcr.numerator, 1)
        self.assertEqual(fcr.rate_percentage, 100.0)
        self.assertEqual(ucr.numerator, 1)
        self.assertEqual(ucr.rate_percentage, 100.0)
        self.assertIn("SCEN-A", fcr.affected_scenario_ids)
        self.assertIn("SCEN-A", ucr.affected_scenario_ids)

    def test_adversarial_B_review_predicted_compliant_increases_ucr_not_fcr(self):
        """Case B: A REQUIRES_HUMAN_REVIEW scenario predicted COMPLIANT must increase UCR but NOT FCR."""
        sc = _make_dummy_scenario("SCEN-B", ComplianceStatus.REQUIRES_HUMAN_REVIEW, requires_hr=True)
        results = {
            "SCEN-B": ScenarioEvaluationResult(
                scenario_id="SCEN-B",
                title="",
                expected_status="requires_human_review",
                predicted_status="compliant",  # Unsafe pass on review!
                status_matched=False,
                processing_status="success",
            )
        }
        fcr, ucr = calculate_safety_metrics([sc], results)
        # FCR denominator is 0 (no NON_COMPLIANT cases), rate is N/A
        self.assertEqual(fcr.numerator, 0)
        self.assertEqual(fcr.rate_string, "N/A")
        # UCR denominator is 1, numerator is 1 (100% unsafe rate)
        self.assertEqual(ucr.numerator, 1)
        self.assertEqual(ucr.rate_percentage, 100.0)
        self.assertIn("SCEN-B", ucr.affected_scenario_ids)

    def test_adversarial_C_compliant_predicted_review_increases_unnecessary_review_rate(self):
        """Case C: A COMPLIANT scenario predicted REQUIRES_HUMAN_REVIEW must increase unnecessary review rate."""
        sc = _make_dummy_scenario("SCEN-C", ComplianceStatus.COMPLIANT, requires_hr=False)
        results = {
            "SCEN-C": ScenarioEvaluationResult(
                scenario_id="SCEN-C",
                title="",
                expected_status="compliant",
                predicted_status="requires_human_review",  # Over-escalated!
                status_matched=False,
                processing_status="success",
                predicted_requires_human_review=True,
            )
        }
        hr_stats = calculate_human_review_metrics([sc], results)
        self.assertEqual(hr_stats.unnecessary_review_count, 1)
        self.assertEqual(hr_stats.unnecessary_review_rate, 1.0)

    def test_adversarial_D_extraction_failure_never_becomes_compliant(self):
        """Case D: An extraction failure must not become COMPLIANT or count as false compliant."""
        sc = _make_dummy_scenario("SCEN-D", ComplianceStatus.NON_COMPLIANT, requires_hr=True)
        results = {
            "SCEN-D": ScenarioEvaluationResult(
                scenario_id="SCEN-D",
                title="",
                expected_status="non_compliant",
                predicted_status=None,
                processing_status="error",
                error_type="ExtractionPipelineError",
                error_message="Model parsing exception",
            )
        }
        fcr, ucr = calculate_safety_metrics([sc], results)
        self.assertEqual(fcr.numerator, 0)
        self.assertEqual(ucr.numerator, 0)

    def test_adversarial_E_wrong_page_attribution_fails_page_accuracy(self):
        """Case E: A quotation appearing on the wrong page must fail page attribution."""
        claims = [
            {
                "scenario_id": "SCEN-E",
                "evidence_status": "wrong_page",
                "exact_quote_match": True,
                "document_correct": True,
                "page_correct": False,
            }
        ]
        ev_stats = calculate_evidence_metrics(claims)
        self.assertEqual(ev_stats.wrong_page_count, 1)
        self.assertEqual(ev_stats.page_attribution_accuracy, 0.0)
        self.assertEqual(ev_stats.quotation_match_rate, 1.0)

    def test_adversarial_F_unsupported_quote_fails_claim_to_evidence_support(self):
        """Case F: A genuine quotation that does not semantically support the claim must fail claim-to-evidence support."""
        claims = [
            {
                "scenario_id": "SCEN-F",
                "evidence_status": "unsupported_claim",
                "exact_quote_match": True,
                "document_correct": True,
                "page_correct": True,
            }
        ]
        ev_stats = calculate_evidence_metrics(claims)
        self.assertEqual(ev_stats.text_present_claim_unsupported_count, 1)
        self.assertEqual(ev_stats.claim_to_evidence_support_rate, 0.0)
        self.assertEqual(ev_stats.unsupported_claim_rate, 1.0)


class TestBenchmarkExecutionDeterminism(unittest.TestCase):
    """Test full cohort execution repeatability and determinism."""

    def test_mock_benchmark_runs_deterministically(self):
        manifest_path = Path("evaluation/cohorts/manifests/cohort_v1_manifest.json")
        annotations_path = Path("evaluation/annotations/cohort_scenarios_v1.json")
        out_dir = Path("evaluation/reports/generated")

        rep1 = execute_cohort_benchmark(
            manifest_path=manifest_path,
            annotations_path=annotations_path,
            output_dir=out_dir,
            scenario_filter="SCEN-01",
        )
        rep2 = execute_cohort_benchmark(
            manifest_path=manifest_path,
            annotations_path=annotations_path,
            output_dir=out_dir,
            scenario_filter="SCEN-01",
        )
        self.assertEqual(rep1.submission_metrics.overall_accuracy, rep2.submission_metrics.overall_accuracy)
        self.assertEqual(rep1.fcr_metric.rate_string, rep2.fcr_metric.rate_string)
        self.assertEqual(rep1.ucr_metric.rate_string, rep2.ucr_metric.rate_string)


if __name__ == "__main__":
    unittest.main()
