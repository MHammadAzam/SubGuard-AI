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

import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from fastapi import UploadFile

from app.compliance.evaluator import ComplianceEvaluator
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
)
from app.schemas.pdf import DocumentProcessingStatus, PDFDocumentInspection


# Safe upload directory
DEFAULT_UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "uploads"
MAX_UPLOAD_SIZE = 15 * 1024 * 1024  # 15 MB per file


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
        init_db()
        self.repository = repository or SubmissionRepository()
        self.pdf_processor = pdf_processor or PDFProcessor()
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
        """Execute full review workflow: upload -> extraction -> deterministic compliance -> SQLite persistence."""
        # 0. Defensive input validation before starting pipeline
        if not coi_file:
            raise ValueError("COI file is required.")

        coi_pre_bytes = await coi_file.read()
        if not coi_pre_bytes:
            raise ValueError(f"Uploaded file '{coi_file.filename or 'coi.pdf'}' is empty (0 bytes).")
        if len(coi_pre_bytes) > MAX_UPLOAD_SIZE:
            raise ValueError(f"Uploaded file '{coi_file.filename or 'coi.pdf'}' exceeds limit of {MAX_UPLOAD_SIZE:,} bytes.")
        if not coi_pre_bytes.startswith(b"%PDF"):
            raise ValueError(f"Uploaded file '{coi_file.filename or 'coi.pdf'}' is not a valid PDF document (missing %PDF magic header).")
        await coi_file.seek(0)

        for end_file in endorsement_files:
            if end_file and getattr(end_file, "filename", None):
                end_pre_bytes = await end_file.read()
                if not end_pre_bytes:
                    raise ValueError(f"Uploaded file '{end_file.filename}' is empty (0 bytes).")
                if len(end_pre_bytes) > MAX_UPLOAD_SIZE:
                    raise ValueError(f"Uploaded file '{end_file.filename}' exceeds limit of {MAX_UPLOAD_SIZE:,} bytes.")
                if not end_pre_bytes.startswith(b"%PDF"):
                    raise ValueError(f"Uploaded file '{end_file.filename}' is not a valid PDF document (missing %PDF magic header).")
                await end_file.seek(0)

        eval_date = evaluation_date or date.today()
        sub_id = f"sub_{uuid4().hex[:12]}"
        sub_name = submission_name or f"Submission {sub_id}"
        sub_contractor = subcontractor_id or "sub_contractor_unknown"

        upload_dir = get_upload_dir() / sub_id
        upload_dir.mkdir(parents=True, exist_ok=True)

        reqs = requirements or get_default_requirements()

        # Select provider: honest mock is default for local application safety
        if provider_name in ("mock", "honest_mock", "application_mock"):
            extraction_provider: BaseExtractionProvider = HonestApplicationMockProvider()
        else:
            from app.extraction.providers import get_extraction_provider
            extraction_provider = get_extraction_provider(provider_name=provider_name)

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
        doc_sets: List[ExtractedDocumentSet] = []
        pipeline_errors: List[str] = []
        inspections: List[PDFDocumentInspection] = []

        # 1. Process COI file
        coi_doc_id = f"doc_{uuid4().hex[:12]}"
        try:
            coi_bytes, coi_path, coi_orig_name = await self.save_uploaded_file(
                coi_file, upload_dir, coi_doc_id
            )

            coi_inspection = self.pdf_processor.process_pdf(
                coi_bytes, document_id=coi_doc_id
            )
            inspections.append(coi_inspection)

            # Route to pipeline
            coi_pipe_result = pipeline_service.process_inspection(
                coi_inspection, subcontractor_id=sub_contractor
            )

            if coi_pipe_result.document_set:
                doc_sets.append(coi_pipe_result.document_set)
                coi_status = "processed"
                coi_error = None
            else:
                coi_status = "failed"
                coi_error = (
                    coi_pipe_result.error_message
                    or (coi_pipe_result.errors[0] if coi_pipe_result.errors else "Extraction failed")
                )
                pipeline_errors.append(f"COI extraction failed: {coi_error}")

            doc_records.append({
                "id": coi_doc_id,
                "submission_id": sub_id,
                "original_filename": coi_orig_name,
                "document_type": "coi",
                "storage_path": str(coi_path),
                "file_size_bytes": len(coi_bytes),
                "page_count": coi_inspection.page_count,
                "processing_status": coi_status,
                "error_message": coi_error,
            })

        except Exception as exc:
            err_msg = str(exc)
            pipeline_errors.append(f"COI processing error: {err_msg}")
            doc_records.append({
                "id": coi_doc_id,
                "submission_id": sub_id,
                "original_filename": getattr(coi_file, "filename", "coi.pdf") or "coi.pdf",
                "document_type": "coi",
                "storage_path": "",
                "file_size_bytes": 0,
                "page_count": 0,
                "processing_status": "failed",
                "error_message": err_msg,
            })

        # 2. Process Endorsement files
        for end_file in endorsement_files:
            if not end_file or not getattr(end_file, "filename", None):
                continue
            end_doc_id = f"doc_{uuid4().hex[:12]}"
            try:
                end_bytes, end_path, end_orig_name = await self.save_uploaded_file(
                    end_file, upload_dir, end_doc_id
                )

                end_inspection = self.pdf_processor.process_pdf(
                    end_bytes, document_id=end_doc_id
                )
                inspections.append(end_inspection)

                end_pipe_result = pipeline_service.process_inspection(
                    end_inspection, subcontractor_id=sub_contractor
                )

                if end_pipe_result.document_set:
                    doc_sets.append(end_pipe_result.document_set)
                    end_status = "processed"
                    end_error = None
                else:
                    end_status = "failed"
                    end_error = (
                        end_pipe_result.error_message
                        or (end_pipe_result.errors[0] if end_pipe_result.errors else "Extraction failed")
                    )
                    pipeline_errors.append(f"Endorsement '{end_orig_name}' extraction failed: {end_error}")

                doc_records.append({
                    "id": end_doc_id,
                    "submission_id": sub_id,
                    "original_filename": end_orig_name,
                    "document_type": "endorsement",
                    "storage_path": str(end_path),
                    "file_size_bytes": len(end_bytes),
                    "page_count": end_inspection.page_count,
                    "processing_status": end_status,
                    "error_message": end_error,
                })
            except Exception as exc:
                err_msg = str(exc)
                pipeline_errors.append(f"Endorsement file error: {err_msg}")
                doc_records.append({
                    "id": end_doc_id,
                    "submission_id": sub_id,
                    "original_filename": getattr(end_file, "filename", "endorsement.pdf"),
                    "document_type": "endorsement",
                    "storage_path": "",
                    "file_size_bytes": 0,
                    "page_count": 0,
                    "processing_status": "failed",
                    "error_message": err_msg,
                })

        # 3. Assemble composite document set & evaluate compliance
        named_subcontractor = None
        if doc_sets:
            # Build composite ExtractedDocumentSet
            combined_coverages = []
            combined_endorsements = []
            named_insured_info = None
            cert_holder_info = None
            contradictions = []

            for ds in doc_sets:
                if ds.named_insured and (not named_insured_info or not named_insured_info.name):
                    named_insured_info = ds.named_insured
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
                certificate_holder=cert_holder_info,
                coverages=combined_coverages,
                endorsements=combined_endorsements,
                raw_contradictions_detected=list(set(contradictions)),
            )

            # Reconcile cross-document contradictions
            reconcile_cross_document_set(composite_set)

            # Run deterministic evaluation
            review_result = self.evaluator.evaluate(
                document_set=composite_set,
                requirements=reqs,
                evaluation_date=eval_date,
            )

            # Invariant: If any document failed pipeline processing, submission can NEVER be COMPLIANT
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
            # Zero documents succeeded extraction -> Hard failure, NEVER COMPLIANT
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

        # 4. Persist to SQLite
        serialized_result = {
            "review": review_result.model_dump(mode="json"),
            "document_set": composite_set.model_dump(mode="json") if composite_set else None,
            "pipeline_errors": pipeline_errors,
        }

        self.repository.create_submission(
            submission_id=sub_id,
            name=sub_name,
            overall_status=review_result.overall_status.value,
            processing_status="completed" if not pipeline_errors else "completed_with_errors",
            provider=provider_name,
            evaluation_date=eval_date.isoformat(),
            result_data=serialized_result,
            subcontractor_name=named_subcontractor,
        )

        for d in doc_records:
            self.repository.add_document(
                document_id=d["id"],
                submission_id=d["submission_id"],
                original_filename=d["original_filename"],
                document_type=d["document_type"],
                storage_path=d["storage_path"],
                file_size_bytes=d["file_size_bytes"],
                page_count=d["page_count"],
                processing_status=d["processing_status"],
                error_message=d["error_message"],
            )

        # 5. Return complete payload
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
