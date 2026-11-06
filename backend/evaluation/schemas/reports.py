"""Evaluation reporting schemas for SubGuard AI benchmark runs.

Structures:
- Field-level match results (exact, normalized, missing value detection).
- Evidence grounding verification (page accuracy, quote verification, hallucination detection).
- Aggregate metrics (Precision, Recall, F1, exact match, normalized accuracy).
- Sanitized environment run summary.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from app.schemas.insurance import DocumentType
from evaluation.schemas.ground_truth import DocumentSourceType


class FieldEvaluationResult(BaseModel):
    """Detailed evaluation result for a single extracted field."""
    field_name: str
    category: str = "general"
    ground_truth_present: bool
    extracted_present: bool
    exact_match: bool = False
    normalized_match: bool = False
    expected_value: Any = None
    extracted_value: Any = None
    error_type: str = "none"  # "none", "false_positive", "false_negative", "value_mismatch", "configuration_error", "provider_error", "not_run"
    notes: Optional[str] = None


class EvidenceEvaluationResult(BaseModel):
    """Evidence citation grounding and semantic support evaluation result."""
    field_name: str
    cited_page: Optional[int] = None
    expected_page: Optional[int] = None
    page_correct: bool = False
    quote_found_on_page: bool = False
    quote_found_in_document: bool = False
    is_hallucinated: bool = False
    is_cross_page_attribution: bool = False
    value_supported: bool = True
    category_aligned: bool = True
    is_ocr_derived: bool = False
    source_text: Optional[str] = None
    notes: Optional[str] = None


class DocumentEvaluationResult(BaseModel):
    """Evaluation summary for a single document in the benchmark."""
    document_id: str
    filename: str
    source_type: DocumentSourceType
    document_type: DocumentType
    processing_status: str
    requires_ocr_matched: bool = True
    field_results: List[FieldEvaluationResult] = Field(default_factory=list)
    evidence_results: List[EvidenceEvaluationResult] = Field(default_factory=list)
    field_accuracy: float = 0.0
    exact_field_accuracy: float = 0.0
    evidence_grounding_rate: float = 0.0
    is_strictly_passed: bool = False
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    status_code: Optional[int] = None
    request_id: Optional[str] = None
    is_retryable: Optional[bool] = None


class FieldMetricStats(BaseModel):
    """Aggregate precision, recall, F1, and accuracy for a group of fields."""
    category: str
    total_evaluated: int = 0
    exact_matches: int = 0
    normalized_matches: int = 0
    true_positives: int = 0
    false_positives: int = 0
    true_negatives: int = 0
    false_negatives: int = 0
    uncertain_extractions: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1_score: float = 0.0
    exact_match_accuracy: float = 0.0
    normalized_accuracy: float = 0.0


class EvidenceMetricStats(BaseModel):
    """Aggregate evidence grounding and semantic support statistics."""
    total_citations: int = 0
    page_correct_count: int = 0
    page_accuracy: float = 0.0
    found_on_page_count: int = 0
    unsupported_quote_count: int = 0
    unsupported_rate: float = 0.0
    hallucinated_quote_count: int = 0
    hallucination_rate: float = 0.0
    cross_page_count: int = 0
    cross_page_rate: float = 0.0
    value_supported_count: int = 0
    value_supported_rate: float = 0.0
    category_aligned_count: int = 0
    category_aligned_rate: float = 0.0
    digital_citations_count: int = 0
    ocr_citations_count: int = 0


class BenchmarkReport(BaseModel):
    """Complete benchmark execution report with transparent pass-rate semantics."""
    model_config = {"protected_namespaces": ()}

    benchmark_id: str
    timestamp: str
    benchmark_type: str = Field(
        default="mock_framework_validation",
        description="Type of benchmark: 'mock_framework_validation', 'live_model_evaluation', or 'real_world_benchmark'"
    )
    dataset_source_type: str = Field(
        default="synthetic",
        description="Dataset lineage: 'synthetic', 'public_sample', or 'licensed_real'"
    )
    provider_name: str
    model_name: Optional[str] = None
    dataset_id: str
    dataset_version: str
    total_documents: int
    evaluated_documents_count: int = 0
    configuration_errors_count: int = 0
    provider_errors_count: int = 0
    not_run_count: int = 0

    # Transparent, multi-dimensional execution & evaluation semantics
    processing_success_count: int = 0
    processing_success_rate: float = 0.0
    strict_passed_documents_count: int = 0
    strict_document_pass_rate: float = 0.0
    thresholded_passed_documents_count: int = 0
    thresholded_document_pass_rate: float = 0.0
    threshold_criterion: str = "field_acc >= 0.80 and evidence_rate >= 0.80"

    overall_normalized_accuracy: float = 0.0
    overall_exact_accuracy: float = 0.0
    overall_evidence_grounding_score: float = 0.0

    field_metrics: Dict[str, FieldMetricStats] = Field(default_factory=dict)
    evidence_metrics: EvidenceMetricStats
    document_results: List[DocumentEvaluationResult] = Field(default_factory=list)
    error_summaries: List[str] = Field(
        default_factory=list,
        description="Sanitized diagnostic error summaries for documents with execution or provider failures"
    )
    execution_time_seconds: float = 0.0
    sanitized_environment: Dict[str, str] = Field(
        default_factory=dict,
        description="Environment metadata with all secrets and keys strictly redacted"
    )


# ============================================================================
# Multi-Document Scenario & Compliance Reporting Schemas
# ============================================================================

class SafetyMetricResult(BaseModel):
    """Detailed calculation for high-stakes safety rates (FCR and UCR)."""
    metric_name: str
    numerator: int = 0
    denominator: int = 0
    rate_percentage: Optional[float] = None
    rate_string: str = "N/A"
    affected_scenario_ids: List[str] = Field(default_factory=list)
    incident_details: List[Dict[str, Any]] = Field(default_factory=list)


class FindingMetricStats(BaseModel):
    """Aggregate finding-level compliance evaluation statistics."""
    total_findings: int = 0
    exact_status_matches: int = 0
    reason_code_matches: int = 0
    human_review_matches: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1_score: float = 0.0
    exact_status_accuracy: float = 0.0
    reason_code_accuracy: float = 0.0
    human_review_accuracy: float = 0.0

    # Milestone 3.2A Enhanced Multi-Class & Explicit Binary Metrics
    four_class_status_accuracy: float = 0.0
    four_class_macro_f1: float = 0.0
    per_class_metrics: Dict[str, Dict[str, float]] = Field(default_factory=dict)
    binary_compliant_precision: float = 0.0
    binary_compliant_recall: float = 0.0
    binary_compliant_f1: float = 0.0


class SubmissionMetricStats(BaseModel):
    """Aggregate submission-level multi-class compliance metrics."""
    total_scenarios: int = 0
    exact_status_matches: int = 0
    overall_accuracy: float = 0.0
    macro_f1: float = 0.0
    weighted_f1: float = 0.0
    confusion_matrix_4class: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    confusion_matrix_5class: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    class_metrics: Dict[str, Dict[str, float]] = Field(default_factory=dict)

    # Milestone 3.2A Safety & Throughput Metrics
    automated_pass_rate: float = 0.0
    automated_rejection_rate: float = 0.0
    human_review_rate: float = 0.0
    unable_to_determine_rate: float = 0.0
    correct_automated_pass_rate: float = 0.0
    false_approval_count: int = 0
    false_rejection_count: int = 0

    # Milestone 3.2B Review-First Missing Evidence Metrics
    missing_evidence_review_rate: float = 0.0
    automatic_rejection_for_missing_evidence_rate: float = 0.0
    automatic_approval_with_unresolved_evidence_rate: float = 0.0
    missing_evidence_scenario_count: int = 0


class HumanReviewMetricStats(BaseModel):
    """Uncertainty routing and escalation performance statistics."""
    uncertainty_recall: float = 0.0
    human_review_precision: float = 0.0
    unnecessary_review_rate: float = 0.0
    missed_review_rate: float = 0.0
    total_human_review_ground_truth: int = 0
    escalated_to_human_review_count: int = 0
    unnecessary_review_count: int = 0
    missed_review_count: int = 0

    # Fine-grained Phase 7.2.1 audit metrics
    unsafe_non_pass_rate: float = 0.0
    unsafe_non_pass_count: int = 0
    total_unsafe_ground_truth: int = 0
    human_review_recall: float = 0.0
    human_review_recall_count: int = 0
    total_requires_human_review_gt: int = 0
    unable_to_determine_recall: float = 0.0
    unable_to_determine_recall_count: int = 0
    total_unable_to_determine_gt: int = 0


class ScenarioEvidenceMetricStats(BaseModel):
    """Fine-grained evidence grounding and semantic support statistics."""
    total_evidence_claims: int = 0
    exact_quotation_matches: int = 0
    quotation_match_rate: float = 0.0
    document_attribution_accuracy: float = 0.0
    page_attribution_accuracy: float = 0.0
    claim_to_evidence_support_rate: float = 0.0
    unsupported_claim_rate: float = 0.0
    missing_evidence_rate: float = 0.0
    citation_completeness_rate: float = 0.0
    text_present_claim_supported_count: int = 0
    text_present_claim_unsupported_count: int = 0
    text_absent_count: int = 0
    wrong_page_count: int = 0
    wrong_document_count: int = 0
    conflicting_evidence_count: int = 0


class ScenarioFindingResult(BaseModel):
    """Per-finding evaluation comparison."""
    requirement_id: str
    rule_type: str
    expected_status: str
    predicted_status: str
    status_matched: bool = False
    expected_reason_code: str
    predicted_reason_code: str
    reason_code_matched: bool = False
    expected_human_review: bool = False
    predicted_human_review: bool = False
    human_review_matched: bool = False
    evidence_grounding_status: str = "supported"
    explanation: Optional[str] = None


class ScenarioEvaluationResult(BaseModel):
    """Evaluation result for an individual multi-document scenario."""
    scenario_id: str
    title: str
    expected_status: str
    predicted_status: Optional[str] = None
    status_matched: bool = False
    expected_requires_human_review: bool = False
    predicted_requires_human_review: bool = False
    human_review_matched: bool = False
    is_unsafe_pass: bool = False
    is_false_compliant: bool = False
    processing_status: str = "success"
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    status_code: Optional[int] = None
    error_code: Optional[str] = None
    request_id: Optional[str] = None
    findings_results: List[ScenarioFindingResult] = Field(default_factory=list)
    document_failures: List[Dict[str, Any]] = Field(default_factory=list)
    execution_time_seconds: float = 0.0


class ScenarioBenchmarkReport(BaseModel):
    """Complete benchmark execution report across a multi-document cohort."""
    model_config = {"protected_namespaces": ()}

    report_schema_version: str = "1.2.0"
    benchmark_id: str
    timestamp: str
    manifest_version: str
    manifest_hash: str
    provider_name: str
    model_name: Optional[str] = None
    total_scenarios: int
    successful_scenarios: int = 0
    failed_scenarios: int = 0
    submission_metrics: SubmissionMetricStats
    finding_metrics: FindingMetricStats
    fcr_metric: SafetyMetricResult
    ucr_metric: SafetyMetricResult
    human_review_metrics: HumanReviewMetricStats
    evidence_metrics: ScenarioEvidenceMetricStats
    scenario_results: List[ScenarioEvaluationResult] = Field(default_factory=list)
    execution_failures: List[Dict[str, Any]] = Field(default_factory=list)
    execution_time_seconds: float = 0.0
    environment_metadata: Dict[str, str] = Field(default_factory=dict)

