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


FIVE_CLASSES = [
    ComplianceStatus.COMPLIANT.value,
    ComplianceStatus.NON_COMPLIANT.value,
    ComplianceStatus.REQUIRES_HUMAN_REVIEW.value,
    ComplianceStatus.UNABLE_TO_DETERMINE.value,
    ComplianceStatus.MISSING_EVIDENCE.value,
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
    """Calculate 4-class and 5-class confusion matrices, overall accuracy, Macro F1, and Weighted F1."""
    # 4-class confusion matrix (missing_evidence projected into unable_to_determine)
    confusion_4: Dict[str, Dict[str, int]] = {
        gt: {pred: 0 for pred in FOUR_CLASSES} for gt in FOUR_CLASSES
    }

    # 5-class confusion matrix (preserving missing_evidence explicitly)
    confusion_5: Dict[str, Dict[str, int]] = {
        gt: {pred: 0 for pred in FIVE_CLASSES} for gt in FIVE_CLASSES
    }

    exact_matches = 0
    total = len(scenarios)

    for sc in scenarios:
        gt_status = sc.expected_overall_status.value
        res = results.get(sc.scenario_id)
        raw_pred = res.predicted_status if (res and res.predicted_status) else ComplianceStatus.UNABLE_TO_DETERMINE.value

        # 5-class recording
        if gt_status in confusion_5 and raw_pred in confusion_5[gt_status]:
            confusion_5[gt_status][raw_pred] += 1

        # 4-class recording (projecting missing_evidence -> unable_to_determine)
        pred_4 = raw_pred if raw_pred in FOUR_CLASSES else ComplianceStatus.UNABLE_TO_DETERMINE.value
        if gt_status in confusion_4 and pred_4 in confusion_4[gt_status]:
            confusion_4[gt_status][pred_4] += 1

        if res and res.status_matched:
            exact_matches += 1

    overall_acc = (exact_matches / total) if total > 0 else 0.0

    # Per-class precision, recall, F1 computed over 4-class projection
    class_metrics: Dict[str, Dict[str, float]] = {}
    f1_list: List[float] = []
    support_list: List[int] = []

    for cls in FOUR_CLASSES:
        tp = confusion_4[cls][cls]
        fp = sum(confusion_4[other][cls] for other in FOUR_CLASSES if other != cls)
        fn = sum(confusion_4[cls][other] for other in FOUR_CLASSES if other != cls)
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
        confusion_matrix_4class=confusion_4,
        confusion_matrix_5class=confusion_5,
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
    """Calculate fine-grained uncertainty routing, unsafe non-pass rate, and recall metrics."""
    # 1. Unsafe Non-Pass Rate
    # Ground-truth unsafe: non_compliant, requires_human_review, unable_to_determine (N=30)
    unsafe_statuses = {
        ComplianceStatus.NON_COMPLIANT,
        ComplianceStatus.REQUIRES_HUMAN_REVIEW,
        ComplianceStatus.UNABLE_TO_DETERMINE,
    }
    unsafe_gt_scenarios = [s for s in scenarios if s.expected_overall_status in unsafe_statuses]
    total_unsafe_gt = len(unsafe_gt_scenarios)
    unsafe_non_pass_count = sum(
        1 for s in unsafe_gt_scenarios
        if results.get(s.scenario_id) and results[s.scenario_id].predicted_status != ComplianceStatus.COMPLIANT.value
    )
    unsafe_non_pass_rate = (unsafe_non_pass_count / total_unsafe_gt) if total_unsafe_gt > 0 else 1.0

    # 2. Uncertainty Recall (True Uncertainty Routing)
    # Ground truth cases in {REQUIRES_HUMAN_REVIEW, UNABLE_TO_DETERMINE} predicted as either of those two
    uncertain_statuses = {
        ComplianceStatus.REQUIRES_HUMAN_REVIEW,
        ComplianceStatus.UNABLE_TO_DETERMINE,
    }
    uncertain_gt_scenarios = [s for s in scenarios if s.expected_overall_status in uncertain_statuses]
    total_uncertain_gt = len(uncertain_gt_scenarios)
    uncertain_correct_count = sum(
        1 for s in uncertain_gt_scenarios
        if results.get(s.scenario_id) and results[s.scenario_id].predicted_status in {
            ComplianceStatus.REQUIRES_HUMAN_REVIEW.value,
            ComplianceStatus.UNABLE_TO_DETERMINE.value,
        }
    )
    uncertainty_recall = (uncertain_correct_count / total_uncertain_gt) if total_uncertain_gt > 0 else 1.0

    # 3. Human-Review Recall
    # Ground-truth REQUIRES_HUMAN_REVIEW predicted strictly REQUIRES_HUMAN_REVIEW
    review_gt_scenarios = [s for s in scenarios if s.expected_overall_status == ComplianceStatus.REQUIRES_HUMAN_REVIEW]
    total_review_gt = len(review_gt_scenarios)
    review_correct_count = sum(
        1 for s in review_gt_scenarios
        if results.get(s.scenario_id) and results[s.scenario_id].predicted_status == ComplianceStatus.REQUIRES_HUMAN_REVIEW.value
    )
    human_review_recall = (review_correct_count / total_review_gt) if total_review_gt > 0 else 1.0

    # 4. Unable-to-Determine Recall
    # Ground-truth UNABLE_TO_DETERMINE predicted strictly UNABLE_TO_DETERMINE
    unable_gt_scenarios = [s for s in scenarios if s.expected_overall_status == ComplianceStatus.UNABLE_TO_DETERMINE]
    total_unable_gt = len(unable_gt_scenarios)
    unable_correct_count = sum(
        1 for s in unable_gt_scenarios
        if results.get(s.scenario_id) and results[s.scenario_id].predicted_status == ComplianceStatus.UNABLE_TO_DETERMINE.value
    )
    unable_to_determine_recall = (unable_correct_count / total_unable_gt) if total_unable_gt > 0 else 1.0

    # 5. Human-Review Precision (Predicted review that was truly GT review)
    total_pred_review = sum(
        1 for s in scenarios
        if results.get(s.scenario_id) and results[s.scenario_id].predicted_status == ComplianceStatus.REQUIRES_HUMAN_REVIEW.value
    )
    hr_precision = (review_correct_count / total_pred_review) if total_pred_review > 0 else 1.0

    # 6. Unnecessary Review Rate (Clean compliant predicted review)
    compliant_scenarios = [s for s in scenarios if s.expected_overall_status == ComplianceStatus.COMPLIANT]
    total_compliant = len(compliant_scenarios)
    unnecessary_review_count = sum(
        1 for s in compliant_scenarios
        if results.get(s.scenario_id) and results[s.scenario_id].predicted_status in {
            ComplianceStatus.REQUIRES_HUMAN_REVIEW.value,
            ComplianceStatus.UNABLE_TO_DETERMINE.value,
        }
    )
    unnecessary_rate = (unnecessary_review_count / total_compliant) if total_compliant > 0 else 0.0

    # 7. Missed Review Rate
    missed_review_count = total_review_gt - review_correct_count
    missed_rate = (missed_review_count / total_review_gt) if total_review_gt > 0 else 0.0

    return HumanReviewMetricStats(
        uncertainty_recall=round(uncertainty_recall, 4),
        human_review_precision=round(hr_precision, 4),
        unnecessary_review_rate=round(unnecessary_rate, 4),
        missed_review_rate=round(missed_rate, 4),
        total_human_review_ground_truth=total_uncertain_gt,
        escalated_to_human_review_count=uncertain_correct_count,
        unnecessary_review_count=unnecessary_review_count,
        missed_review_count=missed_review_count,
        unsafe_non_pass_rate=round(unsafe_non_pass_rate, 4),
        unsafe_non_pass_count=unsafe_non_pass_count,
        total_unsafe_ground_truth=total_unsafe_gt,
        human_review_recall=round(human_review_recall, 4),
        human_review_recall_count=review_correct_count,
        total_requires_human_review_gt=total_review_gt,
        unable_to_determine_recall=round(unable_to_determine_recall, 4),
        unable_to_determine_recall_count=unable_correct_count,
        total_unable_to_determine_gt=total_unable_gt,
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
