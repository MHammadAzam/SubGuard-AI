"""Document Review Service for SubGuard AI.

Coordinates:
1. File upload validation (extension, size, magic bytes)
2. Safe persistent storage with path-traversal prevention
3. PDF inspection via existing PDFProcessor
4. Extraction via existing ExtractionPipelineService
5. Deterministic compliance evaluation via existing ComplianceEvaluator
6. Complete persistence via SubmissionRepository (SQLite)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from uuid import uuid4

from fastapi import UploadFile
from starlette.concurrency import run_in_threadpool

from app.compliance.evaluator import ComplianceEvaluator
from app.compliance.reconciliation import reconcile_extracted_document_set
from app.documents.processor import MAX_PDF_SIZE_BYTES, PDFProcessingError, PDFProcessor
from app.extraction.ocr import BaseOCRProvider, TesseractOCRProvider
from app.extraction.pipeline import ExtractionPipelineService, reconcile_cross_document_set
from app.extraction.providers.base import BaseExtractionProvider
from app.extraction.providers.mock import HonestApplicationMockProvider, MockExtractionProvider
from app.persistence.database import init_db
from app.persistence.repository import SubmissionRepository
from app.schemas.insurance import (
    ComplianceFinding,
    ComplianceReviewResult,
    ComplianceStatus,
    CoverageType,
    DocumentType,
    EndorsementExtraction,
    EndorsementType,
    EntityInfo,
    EvidenceReference,
    ExtractedDocumentSet,
    ExtractionStatus,
    InsuranceRequirement,
    LimitField,
    RequirementType,
    Severity,
    validate_requirement_profile,
)
from app.schemas.pdf import DocumentProcessingStatus, PDFDocumentInspection
from app.security.config import (
    get_max_concurrent_processing,
    get_max_cumulative_upload_bytes,
    get_max_endorsement_files,
    get_max_upload_file_bytes,
    validate_requested_provider,
)


# Safe upload directory
DEFAULT_UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "uploads"
MAX_UPLOAD_SIZE = 15 * 1024 * 1024  # 15 MiB per file (aligned with processor MAX_PDF_SIZE_BYTES)
UPLOAD_READ_CHUNK_BYTES = 64 * 1024


class ProcessingConcurrencyLimitError(RuntimeError):
    """Raised when concurrent document processing slots are saturated."""


class _BoundedProcessingGate:
    """Caps concurrent heavy PDF/OCR/extraction executions to protect 8 GB host memory/CPU."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self._cond = threading.Condition(self._lock)

    def _timeout_seconds(self) -> float:
        raw = os.getenv("SUBGUARD_PROCESSING_WAIT_SECONDS", "5.0").strip()
        return max(0.01, float(raw))

    @contextmanager
    def acquire(self) -> Iterator[None]:
        limit = get_max_concurrent_processing()
        timeout = self._timeout_seconds()
        deadline = None
        with self._cond:
            while self._active >= limit:
                if deadline is None:
                    deadline = time.monotonic() + timeout
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProcessingConcurrencyLimitError(
                        "Document processing queue is busy; please retry shortly."
                    )
                self._cond.wait(timeout=remaining)
                limit = get_max_concurrent_processing()
            self._active += 1
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify()


_processing_gate = _BoundedProcessingGate()


async def _read_bounded_upload(
    upload_file: UploadFile,
    *,
    max_file_bytes: int,
    default_name: str,
) -> Tuple[bytes, str]:
    """Read an UploadFile once in bounded chunks without double-buffering oversized payloads."""
    original_filename = upload_file.filename or default_name
    chunks: List[bytes] = []
    total = 0

    while True:
        chunk = await upload_file.read(UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        if total == 0 and not chunk.startswith(b"%PDF"):
            raise ValueError(
                f"Uploaded file '{original_filename}' is not a valid PDF document (missing %PDF magic header)."
            )
        total += len(chunk)
        if total > max_file_bytes:
            raise ValueError(
                f"Uploaded file '{original_filename}' exceeds limit of {max_file_bytes:,} bytes."
            )
        chunks.append(chunk)

    if total == 0:
        raise ValueError(f"Uploaded file '{original_filename}' is empty (0 bytes).")

    return b"".join(chunks), original_filename


def get_upload_dir() -> Path:
    env_dir = os.getenv("SUBGUARD_UPLOAD_DIR")
    if env_dir:
        return Path(env_dir)
    return DEFAULT_UPLOAD_DIR


def get_default_requirements(
    subcontractor_name: str = "Apex Electrical Contractors LLC",
    certificate_holder_name: str = "General Contractor Corp",
) -> List[InsuranceRequirement]:
    """Return canonical construction contract insurance requirements."""
    return [
        InsuranceRequirement(
            requirement_id="REQ_NAMED_INSURED",
            rule_type=RequirementType.NAMED_INSURED,
            description=f"Named Insured must match '{subcontractor_name}'",
            severity=Severity.CRITICAL,
            required_entity_name=subcontractor_name,
            require_exact_name_match=False,
        ),
        InsuranceRequirement(
            requirement_id="REQ_GL_EACH_OCC",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="Commercial General Liability Each Occurrence >= $1,000,000",
            severity=Severity.CRITICAL,
            coverage_type=CoverageType.GENERAL_LIABILITY,
            limit_field=LimitField.EACH_OCCURRENCE,
            min_amount_cents=100_000_000,
        ),
        InsuranceRequirement(
            requirement_id="REQ_GL_GEN_AGG",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="Commercial General Liability General Aggregate >= $2,000,000",
            severity=Severity.MAJOR,
            coverage_type=CoverageType.GENERAL_LIABILITY,
            limit_field=LimitField.GENERAL_AGGREGATE,
            min_amount_cents=200_000_000,
        ),
        InsuranceRequirement(
            requirement_id="REQ_GL_EXPIRATION",
            rule_type=RequirementType.POLICY_EXPIRATION,
            description="Commercial General Liability policy must be currently active (not expired)",
            severity=Severity.CRITICAL,
            coverage_type=CoverageType.GENERAL_LIABILITY,
        ),
        InsuranceRequirement(
            requirement_id="REQ_ADDL_INSURED",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description=f"Additional Insured endorsement naming '{certificate_holder_name}' or blanket status",
            severity=Severity.CRITICAL,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            required_entity_name=certificate_holder_name,
        ),
    ]


def _sanitize_filename(filename: str) -> str:
    """Sanitize user-provided filename to prevent path traversal and shell injection."""
    base = Path(filename).name
    # Keep only alphanumeric, dashes, dots, underscores
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    if not sanitized.lower().endswith(".pdf"):
        sanitized += ".pdf"
    return sanitized


class DocumentReviewService:
    """Service to orchestrate PDF validation, extraction, deterministic evaluation, and persistence."""

    def __init__(
        self,
        repository: Optional[SubmissionRepository] = None,
        pdf_processor: Optional[PDFProcessor] = None,
        ocr_provider: Optional[BaseOCRProvider] = None,
    ):
        self.repository = repository or SubmissionRepository()
        init_db(self.repository.db_path)
        self.pdf_processor = pdf_processor or PDFProcessor(
            max_pdf_size_bytes=get_max_upload_file_bytes()
        )
        self.ocr_provider = ocr_provider
        self.evaluator = ComplianceEvaluator()

    async def save_uploaded_file(
        self,
        upload_file: UploadFile,
        destination_dir: Path,
        doc_id: str,
    ) -> Tuple[bytes, Path, str]:
        """Validate, read, and save an uploaded PDF safely."""
        destination_dir.mkdir(parents=True, exist_ok=True)

        original_filename = upload_file.filename or "document.pdf"
        sanitized_name = _sanitize_filename(original_filename)
        safe_filename = f"{doc_id}_{sanitized_name}"
        target_path = destination_dir / safe_filename

        # Prevent directory traversal
        if not target_path.resolve().is_relative_to(destination_dir.resolve()):
            raise ValueError(f"Illegal path traversal detected in filename: '{original_filename}'")

        content = await upload_file.read()

        # Enforce validation
        if not content:
            raise ValueError(f"Uploaded file '{original_filename}' is empty (0 bytes).")

        if len(content) > MAX_UPLOAD_SIZE:
            raise ValueError(
                f"Uploaded file '{original_filename}' ({len(content):,} bytes) exceeds limit of {MAX_UPLOAD_SIZE:,} bytes."
            )

        if not content.startswith(b"%PDF"):
            raise ValueError(
                f"Uploaded file '{original_filename}' is not a valid PDF document (missing %PDF magic header)."
            )

        target_path.write_bytes(content)
        return content, target_path, original_filename

    def _write_validated_pdf(
        self,
        content: bytes,
        original_filename: str,
        destination_dir: Path,
        doc_id: str,
    ) -> Path:
        """Write validated PDF bytes to a confined submission directory cleanly."""
        destination_dir.mkdir(parents=True, exist_ok=True)
        sanitized_name = _sanitize_filename(original_filename)
        safe_filename = f"{doc_id}_{sanitized_name}"
        target_path = destination_dir / safe_filename
        if not target_path.resolve().is_relative_to(destination_dir.resolve()):
            raise ValueError(f"Illegal path traversal detected in filename: '{original_filename}'")
        target_path.write_bytes(content)
        return target_path

    async def process_and_review(
        self,
        coi_file: UploadFile,
        endorsement_files: List[UploadFile],
        requirements: Optional[List[InsuranceRequirement]] = None,
        submission_name: Optional[str] = None,
        evaluation_date: Optional[date] = None,
        subcontractor_id: Optional[str] = None,
        provider_name: str = "mock",
    ) -> Dict[str, Any]:
        """Validate uploads & provider allowlist, then offload heavy sync processing off event loop."""
        # 0. Server-side provider allowlist validation BEFORE reading or processing files
        validated_provider = validate_requested_provider(provider_name)

        if not coi_file:
            raise ValueError("COI file is required.")

        active_endorsements = [
            ef for ef in (endorsement_files or []) if ef and getattr(ef, "filename", None)
        ]
        max_endorsements = get_max_endorsement_files()
        if len(active_endorsements) > max_endorsements:
            raise ValueError(
                f"Too many endorsement files ({len(active_endorsements)}). Maximum allowed per submission is {max_endorsements}."
            )

        max_file_bytes = get_max_upload_file_bytes()
        max_total_bytes = get_max_cumulative_upload_bytes()

        # Validate requirements upfront before allocating upload artifacts
        if requirements is None:
            reqs = validate_requirement_profile(
                get_default_requirements(),
                enforce_effective_date_baseline=True,
            )
        else:
            reqs = validate_requirement_profile(
                requirements,
                enforce_effective_date_baseline=False,
            )

        # Single-pass bounded read (eliminates double buffering and catches oversized payloads early)
        coi_bytes, coi_orig_name = await _read_bounded_upload(
            coi_file, max_file_bytes=max_file_bytes, default_name="coi.pdf"
        )
        cumulative_bytes = len(coi_bytes)
        if cumulative_bytes > max_total_bytes:
            raise ValueError(
                f"Total upload size ({cumulative_bytes:,} bytes) exceeds submission limit ({max_total_bytes:,} bytes)."
            )

        endorsement_payloads: List[Tuple[bytes, str]] = []
        for end_file in active_endorsements:
            end_bytes, end_orig_name = await _read_bounded_upload(
                end_file, max_file_bytes=max_file_bytes, default_name="endorsement.pdf"
            )
            cumulative_bytes += len(end_bytes)
            if cumulative_bytes > max_total_bytes:
                raise ValueError(
                    f"Cumulative upload size ({cumulative_bytes:,} bytes) exceeds maximum submission limit "
                    f"({max_total_bytes:,} bytes). Submit fewer documents per review."
                )
            endorsement_payloads.append((end_bytes, end_orig_name))

        return await run_in_threadpool(
            self._process_validated_sync,
            coi_bytes=coi_bytes,
            coi_orig_name=coi_orig_name,
            endorsement_payloads=endorsement_payloads,
            reqs=reqs,
            submission_name=submission_name,
            evaluation_date=evaluation_date,
            subcontractor_id=subcontractor_id,
            provider_name=validated_provider,
        )

    def _process_validated_sync(
        self,
        *,
        coi_bytes: bytes,
        coi_orig_name: str,
        endorsement_payloads: List[Tuple[bytes, str]],
        reqs: List[InsuranceRequirement],
        submission_name: Optional[str],
        evaluation_date: Optional[date],
        subcontractor_id: Optional[str],
        provider_name: str,
    ) -> Dict[str, Any]:
        """Execute bounded synchronous PDF, OCR, extraction, evaluation, and atomic persistence."""
        eval_date = evaluation_date or date.today()
        sub_id = f"sub_{uuid4().hex[:12]}"
        sub_name = submission_name or f"Submission {sub_id}"
        sub_contractor = subcontractor_id or "sub_contractor_unknown"

        upload_dir = get_upload_dir().resolve() / sub_id
        dir_created = False
        persisted = False

        with _processing_gate.acquire():
            try:
                upload_dir.mkdir(parents=True, exist_ok=True)
                dir_created = True

                # Select provider using explicit server allowlist enforcement
                if provider_name in ("mock", "honest_mock", "application_mock"):
                    extraction_provider: BaseExtractionProvider = HonestApplicationMockProvider()
                else:
                    from app.extraction.providers import get_extraction_provider
                    extraction_provider = get_extraction_provider(
                        provider_name=provider_name,
                    )

                # Resolve OCR provider: use configured instance or detect local Tesseract
                active_ocr_provider = self.ocr_provider
                if active_ocr_provider is None:
                    tess = TesseractOCRProvider()
                    if tess.is_available():
                        active_ocr_provider = tess

                pipeline_service = ExtractionPipelineService(
                    extraction_provider=extraction_provider,
                    ocr_provider=active_ocr_provider,
                )

                doc_records: List[Dict[str, Any]] = []
                doc_hashes: Dict[str, str] = {}
                doc_sets: List[ExtractedDocumentSet] = []
                pipeline_errors: List[str] = []
                inspections: List[PDFDocumentInspection] = []

                all_docs: List[Tuple[str, bytes, str]] = [("coi", coi_bytes, coi_orig_name)] + [
                    ("endorsement", eb, en) for eb, en in endorsement_payloads
                ]

                for doc_type_str, doc_bytes, orig_name in all_docs:
                    doc_id = f"doc_{uuid4().hex[:12]}"
                    saved_path = self._write_validated_pdf(
                        doc_bytes, orig_name, upload_dir, doc_id
                    )
                    doc_hashes[doc_id] = hashlib.sha256(doc_bytes).hexdigest()

                    try:
                        inspection = self.pdf_processor.process_pdf(
                            doc_bytes, document_id=doc_id, filename=orig_name
                        )
                        inspections.append(inspection)
                        pipe_result = pipeline_service.process_inspection(
                            inspection, subcontractor_id=sub_contractor
                        )

                        if pipe_result.document_set:
                            doc_sets.append(pipe_result.document_set)
                            proc_status = "processed"
                            proc_error = None
                        else:
                            proc_status = "failed"
                            proc_error = (
                                pipe_result.error_message
                                or (pipe_result.errors[0] if pipe_result.errors else "Extraction failed")
                            )
                            prefix = "COI extraction failed" if doc_type_str == "coi" else f"Endorsement '{orig_name}' extraction failed"
                            pipeline_errors.append(f"{prefix}: {proc_error}")

                        doc_records.append({
                            "id": doc_id,
                            "submission_id": sub_id,
                            "original_filename": orig_name,
                            "document_type": doc_type_str,
                            "storage_path": str(saved_path),
                            "file_size_bytes": len(doc_bytes),
                            "page_count": inspection.page_count,
                            "processing_status": proc_status,
                            "error_message": proc_error,
                        })
                    except Exception as doc_exc:
                        err_msg = str(doc_exc)
                        label = "COI" if doc_type_str == "coi" else "Endorsement file"
                        pipeline_errors.append(f"{label} error: {err_msg}")
                        doc_records.append({
                            "id": doc_id,
                            "submission_id": sub_id,
                            "original_filename": orig_name,
                            "document_type": doc_type_str,
                            "storage_path": str(saved_path),
                            "file_size_bytes": len(doc_bytes),
                            "page_count": 0,
                            "processing_status": "failed",
                            "error_message": err_msg,
                        })

                # Assemble composite document set & evaluate compliance deterministically
                named_subcontractor = None
                if doc_sets:
                    combined_coverages = []
                    combined_endorsements = []
                    named_insured_info = None
                    additional_named_insureds = []
                    cert_holder_info = None
                    contradictions = []

                    for ds in doc_sets:
                        if ds.named_insured and (ds.named_insured.name or ds.named_insured.evidence):
                            if not named_insured_info or not named_insured_info.name:
                                named_insured_info = ds.named_insured
                            else:
                                additional_named_insureds.append(ds.named_insured)
                        if ds.additional_named_insureds:
                            additional_named_insureds.extend(ds.additional_named_insureds)
                        if ds.certificate_holder and (not cert_holder_info or not cert_holder_info.name):
                            cert_holder_info = ds.certificate_holder
                        combined_coverages.extend(ds.coverages)
                        combined_endorsements.extend(ds.endorsements)
                        contradictions.extend(ds.raw_contradictions_detected)

                    if named_insured_info and named_insured_info.name:
                        named_subcontractor = named_insured_info.name

                    composite_set = ExtractedDocumentSet(
                        subcontractor_id=sub_contractor,
                        documents_submitted=[i.classified_type for i in inspections],
                        is_document_set_complete=len(pipeline_errors) == 0,
                        named_insured=named_insured_info,
                        additional_named_insureds=additional_named_insureds,
                        certificate_holder=cert_holder_info,
                        coverages=combined_coverages,
                        endorsements=combined_endorsements,
                        raw_contradictions_detected=list(set(contradictions)),
                    )

                    reconcile_cross_document_set(composite_set)
                    composite_set.reconciliation_result = reconcile_extracted_document_set(
                        document_set=composite_set,
                        requirements=reqs,
                        evaluation_date=eval_date,
                        packet_id=sub_id,
                        document_hashes=doc_hashes,
                    )

                    review_result = self.evaluator.evaluate(
                        document_set=composite_set,
                        requirements=reqs,
                        evaluation_date=eval_date,
                    )

                    if pipeline_errors and review_result.overall_status == ComplianceStatus.COMPLIANT:
                        review_result.overall_status = ComplianceStatus.REQUIRES_HUMAN_REVIEW
                        review_result.requires_human_review = True
                        review_result.findings.append(
                            ComplianceFinding(
                                requirement_id="PIPELINE_ERROR",
                                rule_type=RequirementType.NAMED_INSURED,
                                status=ComplianceStatus.UNABLE_TO_DETERMINE,
                                severity=Severity.CRITICAL,
                                reason_code="DOCUMENT_PROCESSING_FAILED",
                                explanation=f"One or more documents failed pipeline extraction: {'; '.join(pipeline_errors)}",
                                requires_human_review=True,
                                evidence=[],
                            )
                        )
                else:
                    review_result = ComplianceReviewResult(
                        subcontractor_id=sub_contractor,
                        evaluation_date=eval_date,
                        overall_status=ComplianceStatus.UNABLE_TO_DETERMINE,
                        requires_human_review=True,
                        findings=[
                            ComplianceFinding(
                                requirement_id="EXTRACTION_FAILURE",
                                rule_type=RequirementType.NAMED_INSURED,
                                status=ComplianceStatus.UNABLE_TO_DETERMINE,
                                severity=Severity.CRITICAL,
                                reason_code="EXTRACTION_FAILED",
                                explanation=(
                                    "No insurance data could be reliably extracted. "
                                    + ("; ".join(pipeline_errors) if pipeline_errors else "Empty document set.")
                                ),
                                requires_human_review=True,
                                evidence=[],
                            )
                        ],
                    )
                    composite_set = None

                serialized_result = {
                    "review": review_result.model_dump(mode="json"),
                    "document_set": composite_set.model_dump(mode="json") if composite_set else None,
                    "pipeline_errors": pipeline_errors,
                }

                # Single atomic SQLite transaction for submission + all document records
                self.repository.create_submission_with_documents(
                    submission_id=sub_id,
                    name=sub_name,
                    overall_status=review_result.overall_status.value,
                    processing_status="completed" if not pipeline_errors else "completed_with_errors",
                    provider=provider_name,
                    evaluation_date=eval_date.isoformat(),
                    result_data=serialized_result,
                    documents=doc_records,
                    subcontractor_name=named_subcontractor,
                )
                persisted = True

                return {
                    "submission_id": sub_id,
                    "name": sub_name,
                    "overall_status": review_result.overall_status.value,
                    "requires_human_review": review_result.requires_human_review,
                    "provider": provider_name,
                    "evaluation_date": eval_date.isoformat(),
                    "subcontractor_name": named_subcontractor,
                    "review_result": review_result,
                    "documents": doc_records,
                    "pipeline_errors": pipeline_errors,
                }
            except Exception:
                if dir_created and not persisted:
                    shutil.rmtree(upload_dir, ignore_errors=True)
                raise
