"""Benchmark runner and CLI execution engine for SubGuard AI.

Executes end-to-end evaluation runs across benchmark datasets:
1. Ingests PDFs through PDFProcessor.
2. Runs extraction through configured provider (Mock or Gemini).
3. Normalizes and validates evidence references against PDF text layers.
4. Computes field-level accuracy, precision, recall, F1, and grounding metrics.
5. Verifies semantic value support and category alignment.
6. Sanitizes environment and output (never leaks API keys, tokens, or PII).
7. Transparently reports processing success, strict pass rate, and thresholded pass rate.
8. Outputs structured JSON reports, markdown summaries, and ASCII console tables.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from uuid import uuid4

from app.documents.processor import PDFProcessor
from app.extraction.ocr import MockOCRProvider
from app.extraction.pipeline import ExtractionPipelineService
from app.extraction.providers.base import BaseExtractionProvider
from app.extraction.providers.gemini import GeminiExtractionProvider
from app.extraction.providers.mock import MockExtractionProvider
from app.extraction.providers.openrouter import OpenRouterExtractionProvider
from app.schemas.pdf import DocumentProcessingStatus, PDFPageInspection
from app.schemas.raw_extraction import (
    RawCoverageSection,
    RawEndorsement,
    RawEntityExtraction,
    RawEvidenceQuote,
    RawInsuranceExtraction,
)
from evaluation.datasets.loader import (
    build_default_synthetic_dataset,
    load_dataset_from_json,
)
from evaluation.fixtures.synthetic_generator import generate_all_synthetic_fixtures, get_fixtures_dir
from evaluation.metrics.evaluator import MetricsEvaluator
from evaluation.schemas.ground_truth import DocumentGroundTruth, EvaluationDataset
from evaluation.schemas.reports import (
    BenchmarkReport,
    DocumentEvaluationResult,
    EvidenceMetricStats,
    FieldMetricStats,
)


class BenchmarkMockProvider(BaseExtractionProvider):
    """Context-aware mock provider simulating realistic model extraction across benchmark fixtures."""

    @property
    def provider_name(self) -> str:
        return "mock"

    def extract_insurance_data(
        self,
        document_id: str,
        pages: List[PDFPageInspection],
        timeout_seconds: float = 30.0,
    ) -> RawInsuranceExtraction:
        full_text = "\n".join(p.text_content for p in pages)

        # SYNTH-003: Missing Named Insured and Missing Workers' Comp
        if "SYNTH-003" in document_id or "GL-11223344" in full_text:
            return RawInsuranceExtraction(
                document_type="coi",
                named_insured=RawEntityExtraction(name=None, confidence=0.0),
                certificate_holder=RawEntityExtraction(
                    name="General Contractor Corp",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="CERTIFICATE HOLDER: General Contractor Corp",
                        confidence=0.95,
                    ),
                    confidence=0.95,
                ),
                coverages=[
                    RawCoverageSection(
                        coverage_type="general_liability",
                        policy_number="GL-11223344",
                        each_occurrence="$1,000,000",
                        general_aggregate="$2,000,000",
                        effective_date="01/01/2025",
                        expiration_date="01/01/2026",
                        evidence=RawEvidenceQuote(
                            page_number=1,
                            source_text="POLICY NUMBER: GL-11223344",
                            confidence=0.95,
                        ),
                        confidence=0.95,
                    )
                ],
                endorsements=[],
                is_document_set_complete=True,
            )

        # SYNTH-002: Multi-page COI + Endorsement CG 20 10 on Page 2
        if "SYNTH-002" in document_id or "CG 20 10 04 13" in full_text:
            return RawInsuranceExtraction(
                document_type="coi",
                named_insured=RawEntityExtraction(
                    name="Apex Electrical Contractors LLC",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="INSURED: Apex Electrical Contractors LLC",
                        confidence=0.98,
                    ),
                    confidence=0.98,
                ),
                certificate_holder=RawEntityExtraction(
                    name="General Contractor Corp",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="CERTIFICATE HOLDER: General Contractor Corp",
                        confidence=0.95,
                    ),
                    confidence=0.95,
                ),
                coverages=[
                    RawCoverageSection(
                        coverage_type="general_liability",
                        policy_number="GL-98765432",
                        each_occurrence="$1,000,000",
                        general_aggregate="$2,000,000",
                        effective_date="01/01/2025",
                        expiration_date="01/01/2026",
                        evidence=RawEvidenceQuote(
                            page_number=1,
                            source_text="COMMERCIAL GENERAL LIABILITY - POLICY NUMBER: GL-98765432",
                            confidence=0.95,
                        ),
                        confidence=0.95,
                    )
                ],
                endorsements=[
                    RawEndorsement(
                        endorsement_type="additional_insured",
                        form_number="CG 20 10 04 13",
                        named_entities=["General Contractor Corp"],
                        is_blanket=False,
                        evidence=RawEvidenceQuote(
                            page_number=2,  # Verified on Page 2!
                            source_text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13",
                            confidence=0.96,
                        ),
                        confidence=0.96,
                    )
                ],
                is_document_set_complete=True,
            )

        # SYNTH-004: Umbrella Excess Liability
        if "SYNTH-004" in document_id or "UMB-55667788" in full_text:
            return RawInsuranceExtraction(
                document_type="coi",
                named_insured=RawEntityExtraction(
                    name="Apex Electrical Contractors LLC",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="INSURED: Apex Electrical Contractors LLC",
                        confidence=0.98,
                    ),
                    confidence=0.98,
                ),
                certificate_holder=RawEntityExtraction(
                    name="General Contractor Corp",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="CERTIFICATE HOLDER: General Contractor Corp",
                        confidence=0.95,
                    ),
                    confidence=0.95,
                ),
                coverages=[
                    RawCoverageSection(
                        coverage_type="general_liability",
                        policy_number="GL-98765432",
                        each_occurrence="$1,000,000",
                        general_aggregate="$2,000,000",
                        effective_date="01/01/2025",
                        expiration_date="01/01/2026",
                        confidence=0.95,
                    ),
                    RawCoverageSection(
                        coverage_type="umbrella_excess",
                        policy_number="UMB-55667788",
                        each_occurrence="$5,000,000",
                        general_aggregate="$5,000,000",
                        effective_date="01/01/2025",
                        expiration_date="01/01/2026",
                        evidence=RawEvidenceQuote(
                            page_number=1,
                            source_text="POLICY NUMBER: UMB-55667788",
                            confidence=0.95,
                        ),
                        confidence=0.95,
                    ),
                ],
                endorsements=[],
                is_document_set_complete=True,
            )

        # SYNTH-005: Expired GL and Inverted Auto Dates
        if "SYNTH-005" in document_id or "AL-99887766" in full_text:
            return RawInsuranceExtraction(
                document_type="coi",
                named_insured=RawEntityExtraction(
                    name="Apex Electrical Contractors LLC",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="INSURED: Apex Electrical Contractors LLC",
                        confidence=0.98,
                    ),
                    confidence=0.98,
                ),
                certificate_holder=RawEntityExtraction(
                    name="General Contractor Corp",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="CERTIFICATE HOLDER: General Contractor Corp",
                        confidence=0.95,
                    ),
                    confidence=0.95,
                ),
                coverages=[
                    RawCoverageSection(
                        coverage_type="general_liability",
                        policy_number="GL-98765432",
                        each_occurrence="$1,000,000",
                        effective_date="01/01/2023",
                        expiration_date="01/01/2024",
                        confidence=0.95,
                    ),
                    RawCoverageSection(
                        coverage_type="auto_liability",
                        policy_number="AL-99887766",
                        combined_single_limit="$1,000,000",
                        effective_date="01/01/2026",
                        expiration_date="01/01/2025",
                        confidence=0.95,
                    ),
                ],
                endorsements=[],
                is_document_set_complete=True,
            )

        # SYNTH-007: Suffix Variation
        if "SYNTH-007" in document_id or "Apex Electrical Inc" in full_text:
            return RawInsuranceExtraction(
                document_type="coi",
                named_insured=RawEntityExtraction(
                    name="Apex Electrical Inc",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="INSURED: Apex Electrical Inc",
                        confidence=0.98,
                    ),
                    confidence=0.98,
                ),
                certificate_holder=RawEntityExtraction(
                    name="General Contractor Corp",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="CERTIFICATE HOLDER: General Contractor Corp",
                        confidence=0.95,
                    ),
                    confidence=0.95,
                ),
                coverages=[
                    RawCoverageSection(
                        coverage_type="general_liability",
                        policy_number="GL-98765432",
                        each_occurrence="$1,000,000",
                        general_aggregate="$2,000,000",
                        effective_date="01/01/2025",
                        expiration_date="01/01/2026",
                        confidence=0.95,
                    )
                ],
                endorsements=[],
                is_document_set_complete=True,
            )

        # SYNTH-008: Blanket Endorsement
        if "SYNTH-008" in document_id or "automatic status when required in construction agreement" in full_text.lower():
            return RawInsuranceExtraction(
                document_type="endorsement",
                endorsements=[
                    RawEndorsement(
                        endorsement_type="additional_insured",
                        is_blanket=True,
                        evidence=RawEvidenceQuote(
                            page_number=1,
                            source_text="organization whom you have agreed by written contract to add as additional insured",
                            confidence=0.95,
                        ),
                        confidence=0.95,
                    )
                ],
                is_document_set_complete=True,
            )

        # Default / SYNTH-001: Standard ACORD 25 COI
        return RawInsuranceExtraction(
            document_type="coi",
            named_insured=RawEntityExtraction(
                name="Apex Electrical Contractors LLC",
                address="123 Industrial Pkwy, Austin, TX 78701",
                evidence=RawEvidenceQuote(
                    page_number=1,
                    source_text="INSURED: Apex Electrical Contractors LLC",
                    confidence=0.98,
                ),
                confidence=0.98,
            ),
            certificate_holder=RawEntityExtraction(
                name="General Contractor Corp",
                address="500 Builder Way, Dallas, TX 75201",
                evidence=RawEvidenceQuote(
                    page_number=1,
                    source_text="CERTIFICATE HOLDER: General Contractor Corp",
                    confidence=0.95,
                ),
                confidence=0.95,
            ),
            coverages=[
                RawCoverageSection(
                    coverage_type="general_liability",
                    policy_number="GL-98765432",
                    insurer_name="Travelers Indemnity Co",
                    each_occurrence="$1,000,000",
                    general_aggregate="$2,000,000",
                    products_completed_ops="$2,000,000",
                    effective_date="01/01/2025",
                    expiration_date="01/01/2026",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="POLICY NUMBER: GL-98765432",
                        confidence=0.96,
                    ),
                    confidence=0.96,
                ),
                RawCoverageSection(
                    coverage_type="auto_liability",
                    policy_number="AL-12345678",
                    combined_single_limit="$1,000,000",
                    effective_date="01/01/2025",
                    expiration_date="01/01/2026",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="POLICY NUMBER: AL-12345678",
                        confidence=0.95,
                    ),
                    confidence=0.95,
                ),
                RawCoverageSection(
                    coverage_type="workers_comp",
                    policy_number="WC-55554444",
                    insurer_name="Texas Mutual Insurance",
                    per_accident="$1,000,000",
                    disease_each_employee="$1,000,000",
                    disease_policy_limit="$1,000,000",
                    effective_date="01/01/2025",
                    expiration_date="01/01/2026",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="POLICY NUMBER: WC-55554444",
                        confidence=0.95,
                    ),
                    confidence=0.95,
                ),
            ],
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    is_blanket=False,
                    is_coi_checkbox_only=False,
                    named_entities=["General Contractor Corp"],
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="General Contractor Corp is included as Additional Insured on General Liability.",
                        confidence=0.94,
                    ),
                    confidence=0.94,
                )
            ],
            is_document_set_complete=True,
        )


def sanitize_environment() -> Dict[str, str]:
    """Capture non-sensitive execution environment properties, strictly redacting secrets."""
    safe_keys = {
        "OS",
        "PYTHON_VERSION",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
    }
    env = {}
    for k, v in os.environ.items():
        if k in safe_keys:
            env[k] = v
        elif any(secret in k.lower() for secret in ["key", "token", "secret", "password", "auth", "cred"]):
            env[k] = "[REDACTED]"
    env["PYTHON_VERSION"] = sys.version.split()[0]
    return env


class BenchmarkRunner:
    """Executes evaluation benchmarks against a dataset."""

    def __init__(
        self,
        provider_name: str = "mock",
        model_name: Optional[str] = None,
        gemini_api_key: Optional[str] = None,
        openrouter_api_key: Optional[str] = None,
        fixtures_dir: Optional[Path] = None,
        json_mode: Union[bool, str] = "enabled",
    ):
        self.provider_name = provider_name
        if provider_name == "gemini":
            default_model = "gemini-2.0-flash"
        elif provider_name == "openrouter":
            default_model = "nvidia/nemotron-3.5-super-vl:free"
        else:
            default_model = "mock-synthetic"

        self.model_name = model_name or default_model
        self.fixtures_dir = fixtures_dir or get_fixtures_dir()
        self.pdf_processor = PDFProcessor()
        self.metrics_evaluator = MetricsEvaluator()

        # Initialize extraction provider
        if provider_name == "gemini":
            self.provider = GeminiExtractionProvider(api_key=gemini_api_key, model_name=self.model_name)
        elif provider_name == "openrouter":
            self.provider = OpenRouterExtractionProvider(
                api_key=openrouter_api_key,
                model_name=self.model_name,
                json_mode=json_mode,
            )
        else:
            self.provider = BenchmarkMockProvider()

        # Extraction service
        self.pipeline_service = ExtractionPipelineService(
            extraction_provider=self.provider,
            ocr_provider=None,  # No OCR provider attached by default to verify 'requires_ocr' handling
        )

    def run_benchmark(
        self,
        dataset: EvaluationDataset,
        max_documents: Optional[int] = None,
    ) -> BenchmarkReport:
        """Execute benchmark across documents in dataset and compile metrics."""
        start_time = time.time()
        doc_results: List[DocumentEvaluationResult] = []
        processing_success = 0
        strict_passed = 0
        thresholded_passed = 0

        # Ensure synthetic PDFs exist
        generate_all_synthetic_fixtures()

        docs_to_process = dataset.documents
        if max_documents is not None and max_documents > 0:
            docs_to_process = docs_to_process[:max_documents]

        for doc_gt in docs_to_process:
            pdf_path = self.fixtures_dir / doc_gt.filename
            if not pdf_path.exists():
                doc_results.append(
                    DocumentEvaluationResult(
                        document_id=doc_gt.document_id,
                        filename=doc_gt.filename,
                        source_type=doc_gt.source_type,
                        document_type=doc_gt.document_type,
                        processing_status="file_not_found",
                        requires_ocr_matched=False,
                        field_accuracy=0.0,
                        exact_field_accuracy=0.0,
                        evidence_grounding_rate=0.0,
                        is_strictly_passed=False,
                    )
                )
                continue

            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()

            # 1. Process PDF
            pdf_inspection = self.pdf_processor.process_pdf(
                pdf_bytes,
                document_id=doc_gt.document_id,
                filename=doc_gt.filename,
            )

            # 2. Extract Data
            pipeline_result = self.pipeline_service.process_inspection(
                pdf_inspection,
                subcontractor_id=doc_gt.document_id,
            )

            if pipeline_result.processing_status != DocumentProcessingStatus.FAILED:
                processing_success += 1

            # 3. Evaluate against ground truth
            status_str = pipeline_result.processing_status.value
            if pipeline_result.requires_ocr:
                status_str = "requires_ocr"
            elif pipeline_result.processing_status == DocumentProcessingStatus.FAILED:
                if pipeline_result.error_category:
                    status_str = pipeline_result.error_category
                else:
                    status_str = "provider_error"

            diag_message = pipeline_result.error_message
            if not diag_message and pipeline_result.errors:
                diag_message = pipeline_result.errors[0]

            res = self.metrics_evaluator.evaluate_document(
                ground_truth=doc_gt,
                extracted_doc_set=pipeline_result.document_set,
                pdf_inspection=pdf_inspection,
                processing_status=status_str,
                error_type=pipeline_result.error_type,
                error_message=diag_message,
                error_code=pipeline_result.error_code,
                status_code=pipeline_result.status_code,
                request_id=pipeline_result.request_id,
                is_retryable=pipeline_result.is_retryable,
            )

            if res.is_strictly_passed:
                strict_passed += 1

            if res.field_accuracy >= 0.80 and res.evidence_grounding_rate >= 0.80:
                thresholded_passed += 1

            doc_results.append(res)

        # Aggregate metrics
        field_metrics, evidence_metrics = self.metrics_evaluator.calculate_aggregate_metrics(doc_results)
        exec_duration = time.time() - start_time

        all_stats = field_metrics.get("all", FieldMetricStats(category="all"))
        overall_acc = all_stats.normalized_accuracy
        overall_exact = all_stats.exact_match_accuracy
        grounding_score = evidence_metrics.page_accuracy if evidence_metrics.total_citations > 0 else 1.0

        total_docs = len(docs_to_process)
        config_errors = sum(1 for d in doc_results if d.processing_status in ("configuration_error", "authentication_error"))
        provider_errors = sum(
            1 for d in doc_results
            if d.processing_status in (
                "provider_error",
                "rate_limit_error",
                "timeout_error",
                "invalid_request",
                "schema_validation_error",
                "invalid_model_response",
            )
        )
        not_run_docs = sum(1 for d in doc_results if d.processing_status in ("not_run", "file_not_found"))
        evaluated_docs = sum(1 for d in doc_results if d.processing_status in ("processed", "requires_ocr", "partial_success"))

        processing_success_rate = round(processing_success / evaluated_docs, 4) if evaluated_docs > 0 else 0.0
        strict_pass_rate = round(strict_passed / evaluated_docs, 4) if evaluated_docs > 0 else 0.0
        thresholded_pass_rate = round(thresholded_passed / evaluated_docs, 4) if evaluated_docs > 0 else 0.0

        benchmark_type = (
            "mock_framework_validation" if self.provider_name == "mock" else "live_model_evaluation"
        )
        dataset_source = (
            docs_to_process[0].source_type.value if docs_to_process else "synthetic"
        )

        error_summaries: List[str] = []
        for d in doc_results:
            if d.error_message or d.error_type or d.processing_status in (
                "configuration_error", "authentication_error", "provider_error",
                "rate_limit_error", "timeout_error", "invalid_request", "schema_validation_error",
                "invalid_model_response", "file_not_found"
            ):
                summary_line = f"[{d.document_id}] Status: {d.processing_status}"
                if d.error_type:
                    summary_line += f" | Type: {d.error_type}"
                if d.status_code is not None:
                    summary_line += f" | HTTP: {d.status_code}"
                elif d.error_code:
                    summary_line += f" | Code: {d.error_code}"
                if d.is_retryable is not None:
                    summary_line += f" | Retryable: {d.is_retryable}"
                if d.request_id:
                    summary_line += f" | RequestID: {d.request_id}"
                if d.error_message:
                    summary_line += f"\n   Message: {d.error_message}"
                error_summaries.append(summary_line)

        report = BenchmarkReport(
            benchmark_id=str(uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            benchmark_type=benchmark_type,
            dataset_source_type=dataset_source,
            provider_name=self.provider_name,
            model_name=self.model_name,
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.version,
            total_documents=total_docs,
            evaluated_documents_count=evaluated_docs,
            configuration_errors_count=config_errors,
            provider_errors_count=provider_errors,
            not_run_count=not_run_docs,
            processing_success_count=processing_success,
            processing_success_rate=processing_success_rate,
            strict_passed_documents_count=strict_passed,
            strict_document_pass_rate=strict_pass_rate,
            thresholded_passed_documents_count=thresholded_passed,
            thresholded_document_pass_rate=thresholded_pass_rate,
            overall_normalized_accuracy=overall_acc,
            overall_exact_accuracy=overall_exact,
            overall_evidence_grounding_score=grounding_score,
            field_metrics=field_metrics,
            evidence_metrics=evidence_metrics,
            document_results=doc_results,
            error_summaries=error_summaries,
            execution_time_seconds=round(exec_duration, 3),
            sanitized_environment=sanitize_environment(),
        )

        return report


def format_summary_table(report: BenchmarkReport) -> str:
    """Format a clean, multi-dimensional ASCII summary table of benchmark results."""
    mode_label = (
        "MOCK PROVIDER (FRAMEWORK VALIDATION)" if report.benchmark_type == "mock_framework_validation"
        else f"LIVE AI EVALUATION: {report.provider_name.upper()}"
    )
    lines = [
        "=" * 84,
        f" SUBGUARD AI EXTRACTION BENCHMARK REPORT: {mode_label}",
        "=" * 84,
        f" Dataset: {report.dataset_id} (v{report.dataset_version}) | Source Lineage: {report.dataset_source_type.upper()}",
        f" Timestamp: {report.timestamp} | Model: {report.model_name}",
    ]

    if report.evaluated_documents_count > 0:
        lines.extend([
            f" Total Documents: {report.total_documents} | Processing Success: {report.processing_success_count}/{report.evaluated_documents_count} ({report.processing_success_rate*100:.1f}%)",
            f" Strict Document Pass (100% Match): {report.strict_passed_documents_count}/{report.evaluated_documents_count} ({report.strict_document_pass_rate*100:.1f}%)",
            f" Thresholded Pass ({report.threshold_criterion}): {report.thresholded_passed_documents_count}/{report.evaluated_documents_count} ({report.thresholded_document_pass_rate*100:.1f}%)",
            f" Overall Field Accuracy: Normalized={report.overall_normalized_accuracy*100:.1f}% | Exact={report.overall_exact_accuracy*100:.1f}%",
            f" Execution Time: {report.execution_time_seconds:.2f}s",
            "-" * 84,
            f" {'FIELD CATEGORY':<24} | {'EVAL':<5} | {'EXACT%':<7} | {'NORM%':<7} | {'PREC':<6} | {'REC':<6} | {'F1':<6}",
            "-" * 84,
        ])
        for cat, stats in sorted(report.field_metrics.items()):
            lines.append(
                f" {cat:<24} | {stats.total_evaluated:<5} | {stats.exact_match_accuracy*100:>6.1f}% | {stats.normalized_accuracy*100:>6.1f}% | {stats.precision:>6.3f} | {stats.recall:>6.3f} | {stats.f1_score:>6.3f}"
            )
        lines.extend([
            "-" * 84,
            " EVIDENCE GROUNDING & SEMANTIC VERIFICATION:",
            f"   Total Citations Evaluated:     {report.evidence_metrics.total_citations} (Digital: {report.evidence_metrics.digital_citations_count}, OCR: {report.evidence_metrics.ocr_citations_count})",
            f"   Page Citation Accuracy:        {report.evidence_metrics.page_accuracy * 100:.1f}% ({report.evidence_metrics.page_correct_count}/{report.evidence_metrics.total_citations})",
            f"   Quotes Verified on Page:       {report.evidence_metrics.found_on_page_count}/{report.evidence_metrics.total_citations}",
            f"   Value Semantic Support Rate:   {report.evidence_metrics.value_supported_rate * 100:.1f}% ({report.evidence_metrics.value_supported_count}/{report.evidence_metrics.total_citations})",
            f"   Category Alignment Rate:       {report.evidence_metrics.category_aligned_rate * 100:.1f}% ({report.evidence_metrics.category_aligned_count}/{report.evidence_metrics.total_citations})",
            f"   Unsupported Quote Rate:        {report.evidence_metrics.unsupported_rate * 100:.1f}%",
            f"   Cross-Page Attribution Rate:   {report.evidence_metrics.cross_page_rate * 100:.1f}%",
            f"   Hallucinated Quote Rate:       {report.evidence_metrics.hallucination_rate * 100:.1f}%",
            "=" * 84,
        ])
    else:
        lines.extend([
            f" Total Documents: {report.total_documents} | Evaluated Documents: 0",
            f" Configuration Errors: {report.configuration_errors_count} (e.g. missing API key or invalid settings)",
            f" Provider Errors:      {report.provider_errors_count} (e.g. API timeouts or rate limits)",
            f" Execution Time:       {report.execution_time_seconds:.2f}s",
            "-" * 84,
            " STATUS: EVALUATION HALTED",
            " Extraction Accuracy / Precision / Recall: N/A",
            " (Provider/configuration failures are excluded from extraction quality denominators)",
        ])

    if report.error_summaries:
        lines.extend([
            "-" * 84,
            f" DIAGNOSTIC ERROR DETAILS ({len(report.error_summaries)} failure{'s' if len(report.error_summaries) > 1 else ''}):",
        ])
        for summary in report.error_summaries:
            lines.append(f" {summary}")

    lines.append("=" * 84)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="SubGuard AI Extraction Benchmark Runner")
    parser.add_argument("--provider", default="mock", choices=["mock", "gemini", "openrouter"], help="Extraction provider")
    parser.add_argument("--model", default=None, help="Model name (e.g. gemini-2.0-flash or nvidia/nemotron-3.5-super-vl:free)")
    parser.add_argument("--dataset", default=None, help="Path to dataset JSON (defaults to built-in synthetic)")
    parser.add_argument("--max-documents", type=int, default=None, help="Maximum number of documents to evaluate")
    parser.add_argument("--output-dir", default="evaluation/reports", help="Directory for output reports")
    parser.add_argument("--format", default="both", choices=["both", "json", "table"], help="Output format")
    parser.add_argument("--json-mode", default="enabled", choices=["enabled", "disabled"], help="OpenRouter JSON mode: 'enabled' (default) or 'disabled'")
    args = parser.parse_args()

    # Load dataset
    if args.dataset:
        dataset = load_dataset_from_json(Path(args.dataset))
    else:
        dataset = build_default_synthetic_dataset()

    runner = BenchmarkRunner(
        provider_name=args.provider,
        model_name=args.model,
        json_mode=args.json_mode,
    )
    report = runner.run_benchmark(dataset, max_documents=args.max_documents)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"benchmark_{args.provider}_{timestamp_str}.json"
    md_path = output_dir / f"benchmark_{args.provider}_{timestamp_str}.md"

    with open(json_path, "w", encoding="utf-8") as f:
        f.write(report.model_dump_json(indent=2))

    table_str = format_summary_table(report)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Benchmark Report: {args.provider}\n\n```\n{table_str}\n```\n")

    if args.format in ["both", "table"]:
        print(table_str)

    if args.format in ["both", "json"]:
        print(f"\n[INFO] Detailed JSON report written to: {json_path}")
        print(f"[INFO] Markdown summary written to: {md_path}")


if __name__ == "__main__":
    main()
