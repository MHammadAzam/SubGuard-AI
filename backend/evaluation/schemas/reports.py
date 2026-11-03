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
