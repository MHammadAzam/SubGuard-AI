"""FastAPI application boundary for SubGuard AI compliance evaluator and web interface.

Provides:
- Deterministic evaluation service for insurance compliance documents.
- Multi-document upload, inspection, and verification pipeline.
- SQLite persistence and audit trail for submissions, documents, and reviews.
- Enterprise SaaS dashboard, submission workflow, and evidence viewer.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.compliance.evaluator import ComplianceEvaluator
from app.documents.review_service import DocumentReviewService, get_default_requirements
from app.persistence.database import init_db
from app.persistence.repository import SubmissionRepository
from app.schemas.insurance import (
    ComplianceFinding,
    ComplianceReviewResult,
    ComplianceStatus,
    ExtractedDocumentSet,
    InsuranceRequirement,
    RequirementType,
    Severity,
)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="SubGuard AI - Compliance Engine API",
    version="0.2.0",
    description="Deterministic evaluation service and web interface for insurance compliance documents.",
)

# Initialize SQLite database schema
init_db()

# Mount static files and templates
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Global service instances
evaluator = ComplianceEvaluator()
repository = SubmissionRepository()
review_service = DocumentReviewService(repository=repository)


# ============================================================================
# Schemas
# ============================================================================

class EvaluationRequest(BaseModel):
    """Payload for on-demand deterministic compliance evaluation."""
    document_set: ExtractedDocumentSet
    requirements: List[InsuranceRequirement] = Field(min_length=1)
    evaluation_date: date = Field(description="Explicit evaluation date for policy expiration checks")


class HumanReviewActionRequest(BaseModel):
    """Payload for recording a human review decision."""
    review_status: str = Field(description="Action: accepted_exception, deficiency_notice_required, waived, rejected")
    reviewer_notes: str = Field(min_length=1, description="Audit justification notes")
    reviewer_name: str = Field(default="Compliance Coordinator", description="Name of the human reviewer")
    update_submission_status: Optional[str] = Field(default=None, description="Optional override status")
    rule_id: Optional[str] = Field(default=None, description="Target requirement/rule ID when waiving a requirement")
    finding_id: Optional[str] = Field(default=None, description="Target finding ID")


# ============================================================================
# Core API Endpoints
# ============================================================================

@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "compliance-evaluator"}


@app.post(
    "/api/v1/compliance/evaluate",
    response_model=ComplianceReviewResult,
    status_code=status.HTTP_200_OK,
    summary="Evaluate an extracted document set against insurance requirements deterministically",
)
def evaluate_compliance(request: EvaluationRequest) -> ComplianceReviewResult:
    """Evaluate insurance documents against requirements.
    
    This endpoint is stateless and pure:
    - AI extraction does not decide compliance.
    - All requirements are evaluated deterministically using Python business logic.
    - Uncertainty, missing fields, and ambiguities are surfaced explicitly.
    """
    try:
        return evaluator.evaluate(
            document_set=request.document_set,
            requirements=request.requirements,
            evaluation_date=request.evaluation_date,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Evaluation failed: {str(exc)}",
        ) from exc


@app.post(
    "/api/v1/compliance/review-documents",
    status_code=status.HTTP_200_OK,
    summary="Upload COI and endorsements, extract data, deterministically evaluate compliance, and persist",
)
async def review_documents(
    coi_file: UploadFile = File(..., description="Primary Certificate of Insurance PDF (ACORD 25)"),
    endorsement_files: List[UploadFile] = File(default=[], description="Optional endorsement PDFs"),
    requirements: Optional[str] = Form(None, description="Optional JSON list of custom InsuranceRequirements"),
    submission_name: Optional[str] = Form(None, description="Human-readable submission reference"),
    evaluation_date: Optional[str] = Form(None, description="Evaluation date (YYYY-MM-DD)"),
    provider: str = Form("mock", description="Extraction provider: 'mock' (default)"),
):
    """Ingest uploaded PDF documents, extract information, evaluate compliance, and store in SQLite."""
    # 1. Parse evaluation date
    eval_d = None
    if evaluation_date:
        try:
            eval_d = date.fromisoformat(evaluation_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid evaluation_date format: '{evaluation_date}'. Expected YYYY-MM-DD.",
            )

    # 2. Parse custom requirements if provided
    parsed_reqs = None
    if requirements:
        try:
            req_data = json.loads(requirements)
            if isinstance(req_data, list):
                parsed_reqs = [InsuranceRequirement.model_validate(r) for r in req_data]
            else:
                raise ValueError("Requirements must be a JSON array")
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid requirements JSON payload: {exc}",
            )

    # 3. Execute document review workflow
    try:
        result = await review_service.process_and_review(
            coi_file=coi_file,
            endorsement_files=endorsement_files,
            requirements=parsed_reqs,
            submission_name=submission_name,
            evaluation_date=eval_d,
            provider_name=provider,
        )
        return result
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Submission processing failed: {str(exc)}",
        )


@app.get("/api/v1/documents/{document_id}/download")
def download_document(document_id: str):
    """Secure document download endpoint with path-traversal prevention."""
    conn = repository.get_submission_by_id
    # Query document by ID
    from app.persistence.database import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT storage_path, original_filename FROM documents WHERE id = ?",
            (document_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

        file_path = Path(row["storage_path"])
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document file missing on disk")

        return FileResponse(
            path=str(file_path),
            filename=row["original_filename"],
            media_type="application/pdf",
        )
    finally:
        conn.close()


@app.get("/api/v1/submissions")
def list_submissions_api(status: Optional[str] = None, q: Optional[str] = None):
    """Query past submissions with optional status filter and text search."""
    return repository.list_submissions(status_filter=status, search_query=q)


@app.get("/api/v1/submissions/{submission_id}")
def get_submission_api(submission_id: str):
    """Retrieve full submission details and findings by ID."""
    sub = repository.get_submission_by_id(submission_id)
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    return sub


VALID_REVIEW_STATUSES = {
    "accepted_exception",
    "deficiency_notice_required",
    "waived",
    "rejected",
}

VALID_SUBMISSION_STATUSES = {
    "compliant",
    "non_compliant",
    "requires_human_review",
    "waived",
    "rejected",
    "deficiency_pending",
    "resolved_compliant",
}

NON_WAIVABLE_RULE_TYPES = {
    RequirementType.POLICY_EXPIRATION,
    RequirementType.POLICY_EFFECTIVE_DATE,
    RequirementType.NAMED_INSURED,
}

NON_WAIVABLE_REASON_CODES = {
    "POLICY_EXPIRED",
    "INVALID_DATE_RANGE_INVERTED",
    "POLICY_FUTURE_INCEPTION",
    "NAMED_INSURED_MISMATCH",
    "COVERAGE_NOT_FOUND",
}


@app.post("/api/v1/submissions/{submission_id}/review")
def record_human_review_api(submission_id: str, payload: HumanReviewActionRequest):
    """Record an auditable human review decision for a submission.

    Architecture principles enforced:
    1. AI extracts; deterministic rules evaluate; humans resolve uncertainty.
    2. Deterministic hard-failure rules cannot be silently bypassed or approved.
    3. Waivers must specify target rule/finding IDs and cannot waive non-waivable rules.
    4. Reviewer identity, justification notes, and timestamps are strictly recorded.
    5. Original automated evaluation results are permanently preserved in result_json.
    """
    sub = repository.get_submission_by_id(submission_id)
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

    # 1. Validate decision value
    if payload.review_status not in VALID_REVIEW_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid review_status '{payload.review_status}'. Allowed values: {sorted(VALID_REVIEW_STATUSES)}",
        )

    # 2. Validate reviewer identity
    reviewer_name = (payload.reviewer_name or "").strip()
    if not reviewer_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Reviewer name / identity cannot be empty.",
        )

    # 3. Validate reviewer notes
    reviewer_notes = (payload.reviewer_notes or "").strip()
    if not reviewer_notes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Review notes cannot be empty.",
        )

    # 4. Extract automated findings to inspect deterministic compliance state
    review = sub.get("result", {}).get("review", {})
    findings = review.get("findings", [])
    non_compliant_findings = [f for f in findings if f.get("status") == "non_compliant"]

    target_rule = (payload.rule_id or payload.finding_id or "").strip() or None
    target_finding = None
    target_reason_code = None

    if target_rule and findings:
        for f in findings:
            if f.get("requirement_id") == target_rule or f.get("finding_id") == target_rule:
                target_finding = f
                target_reason_code = f.get("reason_code")
                break

    # 5. Prevent approval when hard-failure rules exist
    if payload.review_status == "accepted_exception":
        if non_compliant_findings:
            failed_ids = [f.get("requirement_id", "unknown") for f in non_compliant_findings]
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Cannot approve submission: deterministic hard failure exists on requirements: {failed_ids}. "
                    "Hard compliance failures cannot be accepted via blanket approval. "
                    "Explicit rule waiver or corrected documentation is required."
                ),
            )

    # 6. Enforce waiver rules and non-waivability
    if payload.review_status == "waived":
        if not target_rule:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Waiver action requires specifying a target rule_id or finding_id.",
            )

        if findings and not target_finding:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Requirement or finding '{target_rule}' not found in submission.",
            )

        if target_finding:
            r_type = target_finding.get("rule_type")
            r_code = target_finding.get("reason_code")
            if r_type in [rt.value for rt in NON_WAIVABLE_RULE_TYPES] or r_code in NON_WAIVABLE_REASON_CODES:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Requirement '{target_rule}' ({r_code}) is non-waivable. "
                        "Active coverage dates, valid policy terms, and verified legal entity "
                        "identity cannot be waived."
                    ),
                )

    # 7. Validate and apply operational submission status
    update_status = payload.update_submission_status
    if update_status:
        update_status = update_status.strip().lower()
        if update_status not in VALID_SUBMISSION_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid update_submission_status '{update_status}'. Allowed values: {sorted(VALID_SUBMISSION_STATUSES)}",
            )
        # Prevent setting 'compliant' if hard failures remain
        if update_status in ("compliant", "resolved_compliant") and non_compliant_findings and payload.review_status != "waived":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot set submission status to 'compliant' while deterministic non-compliant findings exist.",
            )

    review_rec = repository.add_review_record(
        submission_id=submission_id,
        review_status=payload.review_status,
        reviewer_notes=reviewer_notes,
        reviewer_name=reviewer_name,
        finding_id=target_finding.get("finding_id") if target_finding else target_rule,
        reason_code=target_reason_code,
        update_submission_status=update_status,
    )
    return {"status": "ok", "review": review_rec}


@app.get("/api/v1/dashboard/stats")
def get_dashboard_stats_api():
    """Return real aggregate statistics for the dashboard."""
    return repository.get_dashboard_stats()


@app.post("/api/v1/compliance/demo-run/{scenario_id}")
def run_synthetic_demo_scenario(scenario_id: str):
    """Run a bundled synthetic scenario from cohort_scenarios_v1.json with one click."""
    fixtures_dir = BASE_DIR.parent / "evaluation" / "fixtures" / "pdfs"
    cohort_fixtures_dir = BASE_DIR.parent / "evaluation" / "cohorts" / "fixtures"
    ann_file = BASE_DIR.parent / "evaluation" / "annotations" / "cohort_scenarios_v1.json"

    if not ann_file.exists():
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Annotations file missing")

    with open(ann_file, "r") as f:
        data = json.load(f)

    target_scen = None
    for s in data.get("scenarios", []):
        if s["scenario_id"] == scenario_id:
            target_scen = s
            break

    if not target_scen:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Scenario '{scenario_id}' not found")

    # Map scenario to fixture files
    scen_pdf_dir = cohort_fixtures_dir / scenario_id
    if not scen_pdf_dir.exists():
        scen_pdf_dir = fixtures_dir

    sub_id = f"demo_{scenario_id.lower()}_{uuid4().hex[:6]}"
    eval_d = date(2025, 6, 15)

    # Use standard requirements or scenario requirements
    reqs = get_default_requirements(
        subcontractor_name="Apex Electrical Contractors LLC",
        certificate_holder_name="General Contractor Corp",
    )

    # Process documents through review service using file paths
    doc_records = []
    doc_sets = []
    inspections = []

    for doc_info in target_scen.get("documents", []):
        fname = doc_info["filename"]
        pdf_path = scen_pdf_dir / fname
        if not pdf_path.exists():
            pdf_path = fixtures_dir / fname
        if not pdf_path.exists():
            continue

        pdf_bytes = pdf_path.read_bytes()
        doc_id = f"doc_{uuid4().hex[:12]}"

        # Save to demo uploads
        sub_upload_dir = review_service.repository.db_path or (review_service.pdf_processor.max_pdf_size_bytes)
        target_store = review_service.pdf_processor
        demo_dest = BASE_DIR.parent / "data" / "uploads" / sub_id
        demo_dest.mkdir(parents=True, exist_ok=True)
        stored_path = demo_dest / f"{doc_id}_{fname}"
        stored_path.write_bytes(pdf_bytes)

        insp = review_service.pdf_processor.process_pdf(pdf_bytes, document_id=doc_id)
        inspections.append(insp)

        pipe_res = ExtractionPipelineService(
            extraction_provider=review_service.evaluator and HonestApplicationMockProvider()
        ).process_inspection(insp, subcontractor_id=target_scen.get("subcontractor_id", "sub_apex"))

        if pipe_res.document_set:
            doc_sets.append(pipe_res.document_set)

        doc_records.append({
            "id": doc_id,
            "submission_id": sub_id,
            "original_filename": fname,
            "document_type": doc_info.get("document_type", "coi"),
            "storage_path": str(stored_path),
            "file_size_bytes": len(pdf_bytes),
            "page_count": insp.page_count,
            "processing_status": "processed" if pipe_res.document_set else "failed",
            "error_message": pipe_res.error_message,
        })

    # Assemble composite set
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

    composite_set = ExtractedDocumentSet(
        subcontractor_id=target_scen.get("subcontractor_id", "sub_apex"),
        documents_submitted=[i.classified_type for i in inspections],
        is_document_set_complete=True,
        named_insured=named_insured_info,
        certificate_holder=cert_holder_info,
        coverages=combined_coverages,
        endorsements=combined_endorsements,
        raw_contradictions_detected=contradictions,
    )

    review_result = review_service.evaluator.evaluate(
        document_set=composite_set,
        requirements=reqs,
        evaluation_date=eval_d,
    )

    serialized = {
        "review": review_result.model_dump(mode="json"),
        "document_set": composite_set.model_dump(mode="json"),
        "pipeline_errors": [],
    }

    repository.create_submission(
        submission_id=sub_id,
        name=f"Demo {scenario_id}: {target_scen.get('title', '')}",
        overall_status=review_result.overall_status.value,
        processing_status="completed",
        provider="mock",
        evaluation_date=eval_d.isoformat(),
        result_data=serialized,
        subcontractor_name=named_insured_info.name if named_insured_info else "Apex Electrical Contractors LLC",
    )

    for d in doc_records:
        repository.add_document(
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

    return {"status": "ok", "submission_id": sub_id, "scenario_id": scenario_id}


# ============================================================================
# Web UI Pages
# ============================================================================

@app.get("/", response_class=HTMLResponse)
def index_redirect():
    """Root redirects to Dashboard."""
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    """Page 1: Executive Dashboard showing real statistics and recent submissions."""
    stats = repository.get_dashboard_stats()
    recent = repository.list_submissions(limit=10)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "stats": stats,
            "recent_submissions": recent,
            "active_nav": "dashboard",
        },
    )


@app.get("/submissions/new", response_class=HTMLResponse)
def new_submission_page(request: Request):
    """Page 2: Document upload and review configuration form."""
    return templates.TemplateResponse(
        request=request,
        name="new_submission.html",
        context={
            "active_nav": "new",
        },
    )


@app.get("/submissions/{submission_id}", response_class=HTMLResponse)
def submission_detail_page(request: Request, submission_id: str):
    """Page 3: Submission review results, statuses, and findings."""
    sub = repository.get_submission_by_id(submission_id)
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

    return templates.TemplateResponse(
        request=request,
        name="submission_detail.html",
        context={
            "submission": sub,
            "active_nav": "history",
        },
    )


@app.get("/submissions/{submission_id}/evidence", response_class=HTMLResponse)
def evidence_viewer_page(request: Request, submission_id: str):
    """Page 4: Evidence trace viewer showing source quotes, page numbers, and verification."""
    sub = repository.get_submission_by_id(submission_id)
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

    return templates.TemplateResponse(
        request=request,
        name="evidence_viewer.html",
        context={
            "submission": sub,
            "active_nav": "history",
        },
    )


@app.get("/submissions/{submission_id}/review", response_class=HTMLResponse)
def human_review_page(request: Request, submission_id: str):
    """Page 5: Human review resolution form for pending flags or exceptions."""
    sub = repository.get_submission_by_id(submission_id)
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

    return templates.TemplateResponse(
        request=request,
        name="human_review.html",
        context={
            "submission": sub,
            "active_nav": "history",
        },
    )


@app.get("/submissions", response_class=HTMLResponse)
def submission_history_page(
    request: Request,
    status: Optional[str] = None,
    q: Optional[str] = None,
):
    """Page 6: Auditable submission history with search and status filtering."""
    submissions = repository.list_submissions(status_filter=status, search_query=q)
    return templates.TemplateResponse(
        request=request,
        name="submission_history.html",
        context={
            "submissions": submissions,
            "status_filter": status,
            "search_query": q,
            "active_nav": "history",
        },
    )
