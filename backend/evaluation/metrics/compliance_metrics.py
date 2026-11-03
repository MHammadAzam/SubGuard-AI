"""Compliance, safety, and evidence metrics calculator for SubGuard AI benchmarks.

Implements:
1. Safety metrics: False Compliance Rate (FCR) and Unsafe Compliance Rate (UCR).
2. Submission-level metrics: 4-class confusion matrix, Macro F1, Weighted F1, overall accuracy.
3. Finding-level metrics: status accuracy, reason-code accuracy, human-review accuracy, F1.
4. Human-review escalation metrics: uncertainty recall, review precision, unnecessary review rate.
5. Evidence-grounding metrics: exact quote match, page/document attribution, claim-to-evidence support.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from app.schemas.insurance import ComplianceStatus
from evaluation.schemas.ground_truth import ScenarioGroundTruth
from evaluation.schemas.reports import (
    FindingMetricStats,
    HumanReviewMetricStats,
    SafetyMetricResult,
    ScenarioEvidenceMetricStats,
    ScenarioEvaluationResult,
    ScenarioFindingResult,
    SubmissionMetricStats,
)

FOUR_CLASSES = [
    ComplianceStatus.COMPLIANT.value,
    ComplianceStatus.NON_COMPLIANT.value,
    ComplianceStatus.REQUIRES_HUMAN_REVIEW.value,
    ComplianceStatus.UNABLE_TO_DETERMINE.value,
]


def calculate_safety_metrics(
    scenarios: List[ScenarioGroundTruth],
    results: Dict[str, ScenarioEvaluationResult],
) -> Tuple[SafetyMetricResult, SafetyMetricResult]:
    """Calculate False Compliance Rate (FCR) and Unsafe Compliance Rate (UCR)."""

    # 1. False Compliance Rate (FCR)
    # Measures false COMPLIANT predictions strictly among definitely NON_COMPLIANT cases.
    non_compliant_scenarios = [
        s for s in scenarios if s.expected_overall_status == ComplianceStatus.NON_COMPLIANT
    ]
    fcr_affected: List[str] = []
    fcr_details: List[Dict[str, Any]] = []

    for sc in non_compliant_scenarios:
        res = results.get(sc.scenario_id)
        if res and res.processing_status == "success" and res.predicted_status == ComplianceStatus.COMPLIANT.value:
            fcr_affected.append(sc.scenario_id)
            fcr_details.append({
                "scenario_id": sc.scenario_id,
                "expected_status": sc.expected_overall_status.value,
                "predicted_status": res.predicted_status,
                "error_type": "FALSE_COMPLIANT_ON_NON_COMPLIANT",
                "reason_codes": [f.predicted_reason_code for f in res.findings_results],
            })

    fcr_num = len(fcr_affected)
    fcr_den = len(non_compliant_scenarios)
    fcr_rate = (fcr_num / fcr_den * 100.0) if fcr_den > 0 else None
    fcr_str = f"{fcr_rate:.2f}%" if fcr_rate is not None else "N/A"

    fcr_metric = SafetyMetricResult(
        metric_name="False Compliance Rate (FCR)",
        numerator=fcr_num,
        denominator=fcr_den,
        rate_percentage=fcr_rate,
        rate_string=fcr_str,
        affected_scenario_ids=fcr_affected,
        incident_details=fcr_details,
    )

    # 2. Unsafe Compliance Rate (UCR)
    # Measures false COMPLIANT predictions across ALL unsafe cases:
    # NON_COMPLIANT, REQUIRES_HUMAN_REVIEW, UNABLE_TO_DETERMINE.
    unsafe_statuses = {
        ComplianceStatus.NON_COMPLIANT,
        ComplianceStatus.REQUIRES_HUMAN_REVIEW,
        ComplianceStatus.UNABLE_TO_DETERMINE,
    }
    unsafe_scenarios = [s for s in scenarios if s.expected_overall_status in unsafe_statuses]
    ucr_affected: List[str] = []
    ucr_details: List[Dict[str, Any]] = []

    for sc in unsafe_scenarios:
        res = results.get(sc.scenario_id)
        if res and res.processing_status == "success" and res.predicted_status == ComplianceStatus.COMPLIANT.value:
            ucr_affected.append(sc.scenario_id)
            ucr_details.append({
                "scenario_id": sc.scenario_id,
                "expected_status": sc.expected_overall_status.value,
                "predicted_status": res.predicted_status,
                "error_type": "UNSAFE_COMPLIANT_PASS",
                "reason_codes": [f.predicted_reason_code for f in res.findings_results],
            })

    ucr_num = len(ucr_affected)
    ucr_den = len(unsafe_scenarios)
    ucr_rate = (ucr_num / ucr_den * 100.0) if ucr_den > 0 else None
    ucr_str = f"{ucr_rate:.2f}%" if ucr_rate is not None else "N/A"

    ucr_metric = SafetyMetricResult(
        metric_name="Unsafe Compliance Rate (UCR)",
        numerator=ucr_num,
        denominator=ucr_den,
        rate_percentage=ucr_rate,
        rate_string=ucr_str,
        affected_scenario_ids=ucr_affected,
        incident_details=ucr_details,
    )

    return fcr_metric, ucr_metric


def calculate_submission_metrics(
    scenarios: List[ScenarioGroundTruth],
    results: Dict[str, ScenarioEvaluationResult],
) -> SubmissionMetricStats:
    """Calculate 4-class confusion matrix, overall accuracy, Macro F1, and Weighted F1."""
    # Initialize 4x4 matrix
    confusion: Dict[str, Dict[str, int]] = {
        gt: {pred: 0 for pred in FOUR_CLASSES} for gt in FOUR_CLASSES
    }

    exact_matches = 0
    total = len(scenarios)

    for sc in scenarios:
        gt_status = sc.expected_overall_status.value
        res = results.get(sc.scenario_id)
        pred_status = res.predicted_status if (res and res.predicted_status in FOUR_CLASSES) else ComplianceStatus.UNABLE_TO_DETERMINE.value

        if gt_status in confusion and pred_status in confusion[gt_status]:
            confusion[gt_status][pred_status] += 1

        if res and res.status_matched:
            exact_matches += 1

    overall_acc = (exact_matches / total) if total > 0 else 0.0

    # Per-class precision, recall, F1
    class_metrics: Dict[str, Dict[str, float]] = {}
    f1_list: List[float] = []
    support_list: List[int] = []

    for cls in FOUR_CLASSES:
        tp = confusion[cls][cls]
        fp = sum(confusion[other][cls] for other in FOUR_CLASSES if other != cls)
        fn = sum(confusion[cls][other] for other in FOUR_CLASSES if other != cls)
        support = tp + fn

        precision = (tp / (tp + fp)) if (tp + fp) > 0 else 0.0
        recall = (tp / (tp + fn)) if (tp + fn) > 0 else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

        class_metrics[cls] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": float(support),
        }
        if support > 0:
            f1_list.append(f1)
            support_list.append(support)

    macro_f1 = (sum(f1_list) / len(f1_list)) if f1_list else 0.0
    total_support = sum(support_list)
    weighted_f1 = (
        sum(f * s for f, s in zip(f1_list, support_list)) / total_support
        if total_support > 0
        else 0.0
    )

    return SubmissionMetricStats(
        total_scenarios=total,
        exact_status_matches=exact_matches,
        overall_accuracy=round(overall_acc, 4),
        macro_f1=round(macro_f1, 4),
        weighted_f1=round(weighted_f1, 4),
        confusion_matrix_4class=confusion,
        class_metrics=class_metrics,
    )


def calculate_finding_metrics(
    finding_results: List[ScenarioFindingResult],
) -> FindingMetricStats:
    """Calculate aggregate finding-level accuracy, reason-code match, and F1."""
    total = len(finding_results)
    if total == 0:
        return FindingMetricStats()

    status_matches = sum(1 for f in finding_results if f.status_matched)
    reason_matches = sum(1 for f in finding_results if f.reason_code_matched)
    hr_matches = sum(1 for f in finding_results if f.human_review_matched)

    # Compliant vs Non-Compliant/Uncertain classification at finding level
    # Positive class: COMPLIANT
    tp = sum(1 for f in finding_results if f.expected_status == ComplianceStatus.COMPLIANT.value and f.predicted_status == ComplianceStatus.COMPLIANT.value)
    fp = sum(1 for f in finding_results if f.expected_status != ComplianceStatus.COMPLIANT.value and f.predicted_status == ComplianceStatus.COMPLIANT.value)
    fn = sum(1 for f in finding_results if f.expected_status == ComplianceStatus.COMPLIANT.value and f.predicted_status != ComplianceStatus.COMPLIANT.value)

    precision = (tp / (tp + fp)) if (tp + fp) > 0 else 0.0
    recall = (tp / (tp + fn)) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    return FindingMetricStats(
        total_findings=total,
        exact_status_matches=status_matches,
        reason_code_matches=reason_matches,
        human_review_matches=hr_matches,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1_score=round(f1, 4),
        exact_status_accuracy=round(status_matches / total, 4),
        reason_code_accuracy=round(reason_matches / total, 4),
        human_review_accuracy=round(hr_matches / total, 4),
    )


def calculate_human_review_metrics(
    scenarios: List[ScenarioGroundTruth],
    results: Dict[str, ScenarioEvaluationResult],
) -> HumanReviewMetricStats:
    """Calculate uncertainty recall, review precision, unnecessary review rate, and missed review rate."""
    total_hr_gt = 0
    escalated_count = 0
    unnecessary_count = 0
    missed_count = 0

    for sc in scenarios:
        res = results.get(sc.scenario_id)
        # Ground truth uncertainty: expected review requested OR status is review/unable
        gt_uncertain = (
            sc.expected_requires_human_review
            or sc.expected_overall_status in {
                ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                ComplianceStatus.UNABLE_TO_DETERMINE,
            }
        )
        # Predicted uncertainty
        pred_uncertain = False
        if res and res.processing_status == "success":
            pred_uncertain = (
                res.predicted_requires_human_review
                or res.predicted_status in {
                    ComplianceStatus.REQUIRES_HUMAN_REVIEW.value,
                    ComplianceStatus.UNABLE_TO_DETERMINE.value,
                }
            )

        if gt_uncertain:
            total_hr_gt += 1
            if pred_uncertain:
                escalated_count += 1
            else:
                missed_count += 1
        else:
            # Clearly compliant or clearly non-compliant without review flag
            if pred_uncertain:
                unnecessary_count += 1

    total_pred_escalated = escalated_count + unnecessary_count

    uncertainty_recall = (escalated_count / total_hr_gt) if total_hr_gt > 0 else 1.0
    hr_precision = (escalated_count / total_pred_escalated) if total_pred_escalated > 0 else 1.0
    unnecessary_rate = (
        unnecessary_count / (len(scenarios) - total_hr_gt)
        if (len(scenarios) - total_hr_gt) > 0
        else 0.0
    )
    missed_rate = (missed_count / total_hr_gt) if total_hr_gt > 0 else 0.0

    return HumanReviewMetricStats(
        uncertainty_recall=round(uncertainty_recall, 4),
        human_review_precision=round(hr_precision, 4),
        unnecessary_review_rate=round(unnecessary_rate, 4),
        missed_review_rate=round(missed_rate, 4),
        total_human_review_ground_truth=total_hr_gt,
        escalated_to_human_review_count=total_pred_escalated,
        unnecessary_review_count=unnecessary_count,
        missed_review_count=missed_count,
    )


def calculate_evidence_metrics(
    evidence_claims: List[Dict[str, Any]],
) -> ScenarioEvidenceMetricStats:
    """Calculate evidence quotation exact match, page/doc attribution, and support rates."""
    total = len(evidence_claims)
    if total == 0:
        return ScenarioEvidenceMetricStats()

    exact_quote_count = 0
    doc_attr_count = 0
    page_attr_count = 0
    supported_count = 0
    unsupported_count = 0
    absent_count = 0
    wrong_page_count = 0
    wrong_doc_count = 0
    conflicting_count = 0

    for claim in evidence_claims:
        category = claim.get("evidence_status", "supported")
        if claim.get("exact_quote_match", False):
            exact_quote_count += 1
        if claim.get("document_correct", False):
            doc_attr_count += 1
        if claim.get("page_correct", False):
            page_attr_count += 1

        if category == "supported":
            supported_count += 1
        elif category == "unsupported_claim":
            unsupported_count += 1
        elif category == "text_absent":
            absent_count += 1
        elif category == "wrong_page":
            wrong_page_count += 1
        elif category == "wrong_document":
            wrong_doc_count += 1
        elif category == "conflicting":
            conflicting_count += 1

    quotation_match_rate = exact_quote_count / total
    doc_attr_rate = doc_attr_count / total
    page_attr_rate = page_attr_count / total
    cesr = supported_count / total
    unsupported_rate = unsupported_count / total
    missing_rate = absent_count / total
    completeness_rate = (total - absent_count) / total

    return ScenarioEvidenceMetricStats(
        total_evidence_claims=total,
        exact_quotation_matches=exact_quote_count,
        quotation_match_rate=round(quotation_match_rate, 4),
        document_attribution_accuracy=round(doc_attr_rate, 4),
        page_attribution_accuracy=round(page_attr_rate, 4),
        claim_to_evidence_support_rate=round(cesr, 4),
        unsupported_claim_rate=round(unsupported_rate, 4),
        missing_evidence_rate=round(missing_rate, 4),
        citation_completeness_rate=round(completeness_rate, 4),
        text_present_claim_supported_count=supported_count,
        text_present_claim_unsupported_count=unsupported_count,
        text_absent_count=absent_count,
        wrong_page_count=wrong_page_count,
        wrong_document_count=wrong_doc_count,
        conflicting_evidence_count=conflicting_count,
    )
