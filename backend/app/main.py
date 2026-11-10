"""FastAPI application boundary for SubGuard AI compliance evaluator and web interface.

Provides:
- Deterministic evaluation service for insurance compliance documents.
- Multi-document upload, inspection, and verification pipeline.
- SQLite persistence and audit trail for submissions, documents, and reviews.
- Enterprise SaaS dashboard, submission workflow, and evidence viewer.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.compliance.evaluator import ComplianceEvaluator
from app.documents.review_service import (
    DocumentReviewService,
    ProcessingConcurrencyLimitError,
    _processing_gate,
    get_default_requirements,
    get_upload_dir,
)
from app.extraction.providers.base import sanitize_error_message
from app.persistence.database import get_connection, get_schema_version, init_db
from app.persistence.repository import SubmissionRepository
from app.schemas.insurance import (
    ComplianceFinding,
    ComplianceReviewResult,
    ComplianceStatus,
    ExtractedDocumentSet,
    InsuranceRequirement,
    RequirementType,
    Severity,
    validate_requirement_profile,
)
from app.security.audit_log import configure_security_logging, log_security_event
from app.security.bootstrap import bootstrap_admin_from_env
from app.security.config import (
    PRE_LOGIN_CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    get_allowed_providers,
    get_app_env,
    get_audit_log_path,
    get_cors_origins,
    get_login_rate_limits,
    is_cookie_secure,
    is_demo_endpoint_enabled,
    is_https_enforced,
    is_production_env,
    resolve_effective_scheme,
    validate_production_config,
    validate_requested_provider,
)
from app.security.passwords import ScryptConcurrencyLimitError
from app.security.rate_limit import login_rate_limiter
from app.security.rbac import (
    BrowserAuthRedirectRequired,
    create_auth_dependencies,
    get_client_ip,
    login_redirect_location,
    sanitize_next_url,
)
from app.security.service import (
    AuthService,
    AuthenticatedPrincipal,
    generate_login_csrf_token,
    verify_login_csrf_token,
)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
app_logger = logging.getLogger("subguard.app")

# Configure security audit logging and initialize schema cleanly
configure_security_logging(audit_log_path=get_audit_log_path())
init_db()
repository = SubmissionRepository()
evaluator = ComplianceEvaluator()
review_service = DocumentReviewService(repository=repository)
auth_service = AuthService(db_path_provider=lambda: repository.db_path)
_auth_deps = create_auth_dependencies(auth_service)
resolve_optional_principal = _auth_deps["resolve_optional_principal"]
require_api_roles = _auth_deps["require_api_roles"]
require_ui_roles = _auth_deps["require_ui_roles"]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Ensure DB tables exist, configure audit logs, apply bootstrap if set, and fail closed in prod."""
    configure_security_logging(audit_log_path=get_audit_log_path())
    init_db(repository.db_path)
    bootstrap_admin_from_env(repository.db_path)
    validate_production_config(repository.db_path)
    yield


app = FastAPI(
    title="SubGuard AI - Compliance Engine API",
    version="0.2.0",
    description="Deterministic evaluation service and web interface for insurance compliance documents.",
    lifespan=lifespan,
    docs_url=None if is_production_env() else "/docs",
    redoc_url=None if is_production_env() else "/redoc",
    openapi_url=None if is_production_env() else "/openapi.json",
)

# Restrictive explicit CORS allowlist only (disabled by default when empty)
_configured_cors_origins = get_cors_origins()
if _configured_cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_configured_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
    )


@app.exception_handler(BrowserAuthRedirectRequired)
async def _handle_browser_auth_redirect(_request: Request, exc: BrowserAuthRedirectRequired):
    return RedirectResponse(
        url=login_redirect_location(exc.next_url),
        status_code=status.HTTP_302_FOUND,
    )


def _request_effective_scheme(request: Request) -> str:
    return resolve_effective_scheme(
        client_host=request.client.host if request.client else None,
        url_scheme=request.url.scheme,
        x_forwarded_proto=request.headers.get("X-Forwarded-Proto"),
    )


@app.middleware("http")
async def security_headers_and_tls_middleware(request: Request, call_next):
    """Enforce production TLS requirements and attach baseline security headers."""
    csp_nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = csp_nonce
    effective_scheme = _request_effective_scheme(request)

    if is_https_enforced() and effective_scheme != "https":
        log_security_event(
            "tls_required_rejected",
            outcome="denied",
            client_ip=get_client_ip(request),
            path=request.url.path,
        )
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": "HTTPS is required in production mode."},
        )

    if is_production_env() and request.url.path in {
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    }:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Not Found"})

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; "
        f"script-src 'self' 'nonce-{csp_nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'"
    )
    if not request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store, max-age=0")
        response.headers.setdefault("Pragma", "no-cache")
    if is_production_env() or effective_scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# Mount static files and templates
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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
    reviewer_name: Optional[str] = Field(
        default=None,
        description="Ignored for attribution; reviewer identity is derived from the authenticated principal",
    )
    update_submission_status: Optional[str] = Field(default=None, description="Optional override status")
    rule_id: Optional[str] = Field(default=None, description="Target requirement/rule ID when waiving a requirement")
    finding_id: Optional[str] = Field(default=None, description="Target finding ID")


class AdminCreateUserRequest(BaseModel):
    """Payload for administrator provisioning of an account."""
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=256)
    role: str = Field(default="viewer")


class AdminUpdateUserRequest(BaseModel):
    """Payload for administrator updates to role, active status, or display name."""
    role: Optional[str] = None
    is_active: Optional[bool] = None
    display_name: Optional[str] = None


class AdminCreateTokenRequest(BaseModel):
    """Payload for issuing a revocable opaque API Bearer token."""
    user_id: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=100)
    ttl_days: int = Field(default=30, ge=1, le=365)


# ============================================================================
# Authentication & Access Administration Endpoints
# ============================================================================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: Optional[str] = None):
    """Render login form or redirect if already authenticated."""
    existing = await resolve_optional_principal(request)
    safe_next = sanitize_next_url(next)
    if existing:
        return RedirectResponse(url=safe_next, status_code=status.HTTP_302_FOUND)

    csrf_tok = generate_login_csrf_token()
    effective_scheme = _request_effective_scheme(request)
    response = templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "next_url": safe_next,
            "login_csrf_token": csrf_tok,
            "error_message": None,
            "submitted_username": "",
            "current_user": None,
            "csrf_token": None,
            "csp_nonce": getattr(request.state, "csp_nonce", ""),
        },
    )
    response.set_cookie(
        key=PRE_LOGIN_CSRF_COOKIE_NAME,
        value=csrf_tok,
        httponly=True,
        samesite="lax",
        secure=is_cookie_secure(effective_scheme),
        max_age=600,
        path="/login",
    )
    return response


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: Optional[str] = Form(None),
    next: Optional[str] = Form(None),
):
    """Authenticate user credentials, set HttpOnly session cookie, and mitigate brute force."""
    client_ip = get_client_ip(request)
    norm_username = (username or "").strip().lower()
    safe_next = sanitize_next_url(next)
    effective_scheme = _request_effective_scheme(request)
    cookie_secure = is_cookie_secure(effective_scheme)

    per_user_limit, per_ip_limit, window_sec = get_login_rate_limits()
    ip_ok, ip_retry = login_rate_limiter.check_and_record(
        f"login_ip:{client_ip}", limit=per_ip_limit, window_seconds=window_sec
    )
    user_ok, user_retry = login_rate_limiter.check_and_record(
        f"login_user:{client_ip}:{norm_username}", limit=per_user_limit, window_seconds=window_sec
    )
    if not ip_ok or not user_ok:
        retry_after = max(ip_retry, user_retry)
        log_security_event(
            "login_rate_limited",
            outcome="rate_limited",
            username=norm_username or None,
            client_ip=client_ip,
            path=request.url.path,
        )
        fresh_csrf = generate_login_csrf_token()
        resp = templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "next_url": safe_next,
                "login_csrf_token": fresh_csrf,
                "error_message": "Too many login attempts. Please wait and try again.",
                "submitted_username": username,
                "current_user": None,
                "csrf_token": None,
                "csp_nonce": getattr(request.state, "csp_nonce", ""),
            },
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
        )
        resp.set_cookie(
            key=PRE_LOGIN_CSRF_COOKIE_NAME,
            value=fresh_csrf,
            httponly=True,
            samesite="lax",
            secure=cookie_secure,
            max_age=600,
            path="/login",
        )
        return resp

    submitted_csrf = csrf_token or request.headers.get("X-CSRF-Token")
    if not verify_login_csrf_token(request.cookies.get(PRE_LOGIN_CSRF_COOKIE_NAME), submitted_csrf):
        log_security_event(
            "login_csrf_failure",
            outcome="denied",
            username=norm_username or None,
            client_ip=client_ip,
            path=request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing login CSRF token.",
        )

    try:
        user = auth_service.authenticate_credentials(norm_username, password)
    except ScryptConcurrencyLimitError:
        log_security_event(
            "login_concurrency_limited",
            outcome="rate_limited",
            username=norm_username or None,
            client_ip=client_ip,
            path=request.url.path,
        )
        fresh_csrf = generate_login_csrf_token()
        resp = templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "next_url": safe_next,
                "login_csrf_token": fresh_csrf,
                "error_message": "Authentication service busy. Please retry shortly.",
                "submitted_username": username,
                "current_user": None,
                "csrf_token": None,
                "csp_nonce": getattr(request.state, "csp_nonce", ""),
            },
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": "2"},
        )
        resp.set_cookie(
            key=PRE_LOGIN_CSRF_COOKIE_NAME,
            value=fresh_csrf,
            httponly=True,
            samesite="lax",
            secure=cookie_secure,
            max_age=600,
            path="/login",
        )
        return resp
    if not user:
        log_security_event(
            "login_failure",
            outcome="failure",
            username=norm_username or None,
            client_ip=client_ip,
            path=request.url.path,
        )
        fresh_csrf = generate_login_csrf_token()
        resp = templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "next_url": safe_next,
                "login_csrf_token": fresh_csrf,
                "error_message": "Invalid username or password.",
                "submitted_username": username,
                "current_user": None,
                "csrf_token": None,
                "csp_nonce": getattr(request.state, "csp_nonce", ""),
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        resp.set_cookie(
            key=PRE_LOGIN_CSRF_COOKIE_NAME,
            value=fresh_csrf,
            httponly=True,
            samesite="lax",
            secure=cookie_secure,
            max_age=600,
            path="/login",
        )
        return resp

    raw_token, _csrf_tok, _ses_id = auth_service.create_session(user["id"])
    log_security_event(
        "login_success",
        outcome="success",
        username=user["username"],
        user_id=user["id"],
        role=user["role"],
        client_ip=client_ip,
        path=request.url.path,
    )
    redirect = RedirectResponse(url=safe_next, status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie(key=PRE_LOGIN_CSRF_COOKIE_NAME, path="/login")
    redirect.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        samesite="lax",
        secure=cookie_secure,
        path="/",
    )
    return redirect


@app.post("/logout")
def logout_submit(
    request: Request,
    current_user: AuthenticatedPrincipal = Depends(
        require_ui_roles("viewer", "analyst", "reviewer", "admin")
    ),
):
    """Terminate authenticated browser session (requires valid session + CSRF)."""
    raw_cookie = request.cookies.get(SESSION_COOKIE_NAME, "")
    auth_service.revoke_session_by_raw_token(raw_cookie)
    log_security_event(
        "logout",
        outcome="success",
        username=current_user.username,
        user_id=current_user.user_id,
        role=current_user.role,
        client_ip=get_client_ip(request),
        path=request.url.path,
    )
    resp = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return resp


@app.get("/api/v1/auth/me")
def get_current_principal_info(
    current_user: AuthenticatedPrincipal = Depends(
        require_api_roles("viewer", "analyst", "reviewer", "admin")
    ),
):
    """Return authenticated principal metadata without secrets or hashes."""
    return {
        "id": current_user.user_id,
        "username": current_user.username,
        "display_name": current_user.display_name,
        "role": current_user.role,
        "auth_method": current_user.auth_method,
    }


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(
    request: Request,
    current_user: AuthenticatedPrincipal = Depends(require_ui_roles("admin")),
):
    """Administrator UI for provisioning users and managing revocable API tokens."""
    return templates.TemplateResponse(
        request=request,
        name="admin_users.html",
        context={
            "users": auth_service.list_users(),
            "tokens": auth_service.list_tokens(),
            "active_nav": "admin",
            "current_user": current_user,
            "csrf_token": current_user.csrf_token,
            "csp_nonce": getattr(request.state, "csp_nonce", ""),
        },
    )


@app.get("/api/v1/admin/users")
def admin_list_users(
    _current_user: AuthenticatedPrincipal = Depends(require_api_roles("admin")),
):
    """List provisioned user accounts without password hashes."""
    return auth_service.list_users()


@app.post("/api/v1/admin/users", status_code=status.HTTP_201_CREATED)
def admin_create_user(
    request: Request,
    payload: AdminCreateUserRequest,
    current_user: AuthenticatedPrincipal = Depends(require_api_roles("admin")),
):
    """Provision a new single-tenant user with explicit role and scrypt password hash."""
    if auth_service.get_user_by_username(payload.username):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{payload.username.strip().lower()}' already exists.",
        )
    try:
        created = auth_service.create_user(
            username=payload.username,
            display_name=payload.display_name,
            password=payload.password,
            role=payload.role,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    log_security_event(
        "user_created",
        outcome="success",
        username=current_user.username,
        user_id=current_user.user_id,
        role=current_user.role,
        client_ip=get_client_ip(request),
        path=request.url.path,
        details={"target_user_id": created["id"], "target_role": created["role"]},
    )
    return created


@app.patch("/api/v1/admin/users/{user_id}")
def admin_update_user(
    request: Request,
    user_id: str,
    payload: AdminUpdateUserRequest,
    current_user: AuthenticatedPrincipal = Depends(require_api_roles("admin")),
):
    """Update user role, display name, or active state; revoke sessions if demoted/disabled."""
    if user_id == current_user.user_id and (
        payload.is_active is False or (payload.role is not None and payload.role != "admin")
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Administrators cannot disable or demote their own active account.",
        )

    if payload.is_active is False or (payload.role is not None and payload.role != "admin"):
        active_admins = [
            u for u in auth_service.list_users() if u["role"] == "admin" and u["is_active"]
        ]
        if len(active_admins) <= 1 and any(u["id"] == user_id for u in active_admins):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot remove or deactivate the last active administrator.",
            )

    try:
        updated = auth_service.update_user(
            user_id,
            role=payload.role,
            is_active=payload.is_active,
            display_name=payload.display_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    log_security_event(
        "user_updated",
        outcome="success",
        username=current_user.username,
        user_id=current_user.user_id,
        role=current_user.role,
        client_ip=get_client_ip(request),
        path=request.url.path,
        details={"target_user_id": user_id, "role": updated["role"], "is_active": updated["is_active"]},
    )
    return updated


@app.get("/api/v1/admin/tokens")
def admin_list_tokens(
    _current_user: AuthenticatedPrincipal = Depends(require_api_roles("admin")),
):
    """List active and revoked session/API token metadata without secrets."""
    return auth_service.list_tokens()


@app.post("/api/v1/admin/tokens", status_code=status.HTTP_201_CREATED)
def admin_create_token(
    request: Request,
    payload: AdminCreateTokenRequest,
    current_user: AuthenticatedPrincipal = Depends(require_api_roles("admin")),
):
    """Issue a revocable opaque API Bearer token (raw token returned once only)."""
    try:
        token_record = auth_service.create_api_token(
            user_id=payload.user_id,
            label=payload.label,
            ttl_days=payload.ttl_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    log_security_event(
        "api_token_created",
        outcome="success",
        username=current_user.username,
        user_id=current_user.user_id,
        role=current_user.role,
        client_ip=get_client_ip(request),
        path=request.url.path,
        details={"token_id": token_record["id"], "target_user_id": payload.user_id},
    )
    return token_record


@app.post("/api/v1/admin/tokens/{token_id}/revoke")
def admin_revoke_token(
    request: Request,
    token_id: str,
    current_user: AuthenticatedPrincipal = Depends(require_api_roles("admin")),
):
    """Revoke a session or API token by ID."""
    revoked = auth_service.revoke_session_by_id(token_id)
    if not revoked:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active token or session not found")

    log_security_event(
        "api_token_revoked",
        outcome="success",
        username=current_user.username,
        user_id=current_user.user_id,
        role=current_user.role,
        client_ip=get_client_ip(request),
        path=request.url.path,
        details={"token_id": token_id},
    )
    return {"status": "revoked", "id": token_id}


@app.post("/api/v1/admin/sessions/purge")
def admin_purge_expired_sessions(
    request: Request,
    current_user: AuthenticatedPrincipal = Depends(require_api_roles("admin")),
):
    """Delete expired or revoked sessions/tokens without affecting active sessions."""
    purged = auth_service.purge_expired_sessions()
    log_security_event(
        "sessions_purged",
        outcome="success",
        username=current_user.username,
        user_id=current_user.user_id,
        role=current_user.role,
        client_ip=get_client_ip(request),
        path=request.url.path,
        details={"purged_count": purged},
    )
    return {"status": "ok", "purged": purged}


@app.get("/api/v1/admin/system-health")
def admin_system_health(
    _current_user: AuthenticatedPrincipal = Depends(require_api_roles("admin")),
):
    """Authenticated readiness diagnostics checking SQLite connectivity and upload writability."""
    db_ok = False
    schema_ver = 0
    db_err: Optional[str] = None
    try:
        conn = get_connection(repository.db_path)
        try:
            conn.execute("SELECT 1;").fetchone()
            schema_ver = get_schema_version(repository.db_path)
            db_ok = True
        finally:
            conn.close()
    except Exception as exc:
        db_err = sanitize_error_message(str(exc))

    upload_ok = False
    upload_err: Optional[str] = None
    try:
        udir = get_upload_dir().resolve()
        udir.mkdir(parents=True, exist_ok=True)
        probe = udir / f".health_probe_{uuid4().hex[:8]}"
        probe.write_bytes(b"ok")
        probe.unlink(missing_ok=True)
        upload_ok = True
    except Exception as exc:
        upload_err = sanitize_error_message(str(exc))

    overall_ok = db_ok and upload_ok
    payload: Dict[str, Any] = {
        "status": "ok" if overall_ok else "degraded",
        "environment": get_app_env(),
        "database_ok": db_ok,
        "schema_version": schema_ver,
        "upload_storage_ok": upload_ok,
        "allowed_providers": sorted(get_allowed_providers()),
    }
    if db_err:
        payload["database_error"] = db_err
    if upload_err:
        payload["upload_error"] = upload_err
    return JSONResponse(
        status_code=status.HTTP_200_OK if overall_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=payload,
    )


# ============================================================================
# Core API Endpoints
# ============================================================================

@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    """Public health check endpoint exposing minimal service status only."""
    return {"status": "ok", "service": "compliance-evaluator"}


@app.post(
    "/api/v1/compliance/evaluate",
    response_model=ComplianceReviewResult,
    status_code=status.HTTP_200_OK,
    summary="Evaluate an extracted document set against insurance requirements deterministically",
    dependencies=[Depends(require_api_roles("analyst", "reviewer", "admin"))],
)
def evaluate_compliance(request: EvaluationRequest) -> ComplianceReviewResult:
    """Evaluate insurance documents against requirements.
    
    This endpoint is stateless and pure:
    - AI extraction does not decide compliance.
    - All requirements are evaluated deterministically using Python business logic.
    - Uncertainty, missing fields, and ambiguities are surfaced explicitly.
    """
    try:
        validated_reqs = validate_requirement_profile(
            request.requirements,
            enforce_effective_date_baseline=False,
        )
        return evaluator.evaluate(
            document_set=request.document_set,
            requirements=validated_reqs,
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
    dependencies=[Depends(require_api_roles("analyst", "reviewer", "admin"))],
)
async def review_documents(
    request: Request,
    coi_file: UploadFile = File(..., description="Primary Certificate of Insurance PDF (ACORD 25)"),
    endorsement_files: List[UploadFile] = File(default=[], description="Optional endorsement PDFs"),
    requirements: Optional[str] = Form(None, description="Optional JSON list of custom InsuranceRequirements"),
    submission_name: Optional[str] = Form(None, description="Human-readable submission reference"),
    evaluation_date: Optional[str] = Form(None, description="Evaluation date (YYYY-MM-DD)"),
    provider: str = Form("mock", description="Extraction provider: 'mock' (default)"),
):
    """Ingest uploaded PDF documents, extract information, evaluate compliance, and store in SQLite."""
    # 0. Validate provider against server allowlist BEFORE reading uploaded payloads
    try:
        validated_provider = validate_requested_provider(provider)
    except PermissionError as perm_err:
        log_security_event(
            "provider_rejected_policy",
            outcome="denied",
            client_ip=get_client_ip(request),
            path=request.url.path,
            details={"requested_provider": (provider or "").strip().lower()},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(perm_err),
        ) from perm_err
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        ) from val_err

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

    # 2. Parse and validate custom requirements if explicitly provided (never silently replace [] with defaults)
    parsed_reqs = None
    if requirements is not None:
        raw_reqs_text = requirements.strip()
        if not raw_reqs_text:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid requirements payload: explicitly provided requirements string cannot be empty.",
            )
        try:
            req_data = json.loads(raw_reqs_text)
            if not isinstance(req_data, list):
                raise ValueError("Requirements must be a JSON array")
            if len(req_data) == 0:
                raise ValueError(
                    "Requirement profile cannot be empty; omit 'requirements' to use the default policy or provide at least one requirement."
                )
            validated_items = [InsuranceRequirement.model_validate(r) for r in req_data]
            parsed_reqs = validate_requirement_profile(
                validated_items,
                enforce_effective_date_baseline=False,
            )
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
            provider_name=validated_provider,
        )
        return result
    except PermissionError as perm_err:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(perm_err),
        ) from perm_err
    except ProcessingConcurrencyLimitError as busy_err:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(busy_err),
            headers={"Retry-After": "5"},
        ) from busy_err
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        ) from val_err
    except Exception as exc:
        correlation_id = f"err_{uuid4().hex[:10]}"
        safe_exc = sanitize_error_message(str(exc))
        app_logger.exception(
            "Unhandled submission processing error correlation_id=%s detail=%s",
            correlation_id,
            safe_exc,
        )
        log_security_event(
            "submission_processing_error",
            outcome="error",
            client_ip=get_client_ip(request),
            path=request.url.path,
            details={"correlation_id": correlation_id, "error_type": exc.__class__.__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Submission processing failed (reference: {correlation_id}).",
        ) from exc


def _resolve_confined_upload_path(
    raw_storage_path: str,
    *,
    submission_id: Optional[str] = None,
) -> Path:
    """Resolve stored document path and verify strict, symlink-free containment under upload dir."""
    allowed_root = get_upload_dir().resolve()
    allowed_scope = allowed_root
    if submission_id is not None:
        if (
            not submission_id
            or "/" in submission_id
            or "\\" in submission_id
            or ".." in submission_id
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: invalid document submission association.",
            )
        allowed_scope = (allowed_root / submission_id).resolve()
        if not allowed_scope.is_relative_to(allowed_root):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: invalid submission storage scope.",
            )

    raw_path = Path(raw_storage_path)

    if raw_path.is_symlink():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: symbolic document paths are prohibited.",
        )

    # Reject any symlink component inside or above the upload target within allowed_root
    try:
        resolved = raw_path.resolve(strict=False)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: invalid document path.",
        ) from exc

    if not resolved.is_relative_to(allowed_scope):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: document path escapes authorized submission storage.",
        )

    # Check every path component inside allowed_root for symlinks
    rel_parts = resolved.relative_to(allowed_root).parts
    walker = allowed_root
    for part in rel_parts:
        walker = walker / part
        if walker.exists() and walker.is_symlink():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: symbolic link inside storage directory.",
            )

    return resolved


def _sanitize_download_filename(filename: Optional[str], fallback: str = "document.pdf") -> str:
    base = Path(filename or fallback).name
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .")
    if not cleaned:
        cleaned = fallback
    if not cleaned.lower().endswith(".pdf"):
        cleaned = f"{cleaned}.pdf"
    return cleaned[:150]


def _serve_authorized_document(
    document_id: str,
    *,
    expected_submission_id: Optional[str],
    request: Request,
    current_user: AuthenticatedPrincipal,
) -> FileResponse:
    """Verify document DB record, submission association, confined path, and serve attachment."""
    conn = get_connection(repository.db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT d.id, d.submission_id, d.storage_path, d.original_filename
            FROM documents d
            JOIN submissions s ON s.id = d.submission_id
            WHERE d.id = ?
            """,
            (document_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

        if expected_submission_id is not None and row["submission_id"] != expected_submission_id:
            log_security_event(
                "document_download_denied",
                outcome="denied",
                username=current_user.username,
                user_id=current_user.user_id,
                role=current_user.role,
                client_ip=get_client_ip(request),
                path=request.url.path,
                details={
                    "document_id": document_id,
                    "expected_submission_id": expected_submission_id,
                    "actual_submission_id": row["submission_id"],
                },
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Document not found for submission",
            )

        try:
            confined_path = _resolve_confined_upload_path(
                row["storage_path"],
                submission_id=row["submission_id"],
            )
        except HTTPException:
            log_security_event(
                "document_download_denied",
                outcome="denied",
                username=current_user.username,
                user_id=current_user.user_id,
                role=current_user.role,
                client_ip=get_client_ip(request),
                path=request.url.path,
                details={"document_id": document_id, "submission_id": row["submission_id"]},
            )
            raise

        if not confined_path.exists() or not confined_path.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document file missing on disk")

        safe_name = _sanitize_download_filename(row["original_filename"], fallback=f"{document_id}.pdf")
        log_security_event(
            "document_download_allowed",
            outcome="success",
            username=current_user.username,
            user_id=current_user.user_id,
            role=current_user.role,
            client_ip=get_client_ip(request),
            path=request.url.path,
            details={"document_id": document_id, "submission_id": row["submission_id"]},
        )
        return FileResponse(
            path=str(confined_path),
            filename=safe_name,
            media_type="application/pdf",
            content_disposition_type="attachment",
            headers={"X-Content-Type-Options": "nosniff"},
        )
    finally:
        conn.close()


@app.get("/api/v1/documents/{document_id}/download")
def download_document(
    document_id: str,
    request: Request,
    submission_id: Optional[str] = None,
    current_user: AuthenticatedPrincipal = Depends(
        require_api_roles("viewer", "analyst", "reviewer", "admin")
    ),
):
    """Secure authenticated document download endpoint with strict DB and path confinement."""
    return _serve_authorized_document(
        document_id,
        expected_submission_id=submission_id,
        request=request,
        current_user=current_user,
    )


@app.get("/api/v1/submissions/{submission_id}/documents/{document_id}/download")
def download_submission_document(
    submission_id: str,
    document_id: str,
    request: Request,
    current_user: AuthenticatedPrincipal = Depends(
        require_api_roles("viewer", "analyst", "reviewer", "admin")
    ),
):
    """Submission-scoped authenticated document download enforcing submission-document association."""
    return _serve_authorized_document(
        document_id,
        expected_submission_id=submission_id,
        request=request,
        current_user=current_user,
    )


@app.get(
    "/api/v1/submissions",
    dependencies=[Depends(require_api_roles("viewer", "analyst", "reviewer", "admin"))],
)
def list_submissions_api(status: Optional[str] = None, q: Optional[str] = None):
    """Query past submissions with optional status filter and text search."""
    return repository.list_submissions(status_filter=status, search_query=q)


@app.get(
    "/api/v1/submissions/{submission_id}",
    dependencies=[Depends(require_api_roles("viewer", "analyst", "reviewer", "admin"))],
)
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
    "missing_evidence",
    "unable_to_determine",
    "waived",
    "rejected",
    "deficiency_pending",
    "resolved_compliant",
}

BLOCKING_FINDING_STATUSES = {
    ComplianceStatus.NON_COMPLIANT.value,
    ComplianceStatus.MISSING_EVIDENCE.value,
    ComplianceStatus.UNABLE_TO_DETERMINE.value,
    ComplianceStatus.REQUIRES_HUMAN_REVIEW.value,
}

NON_WAIVABLE_RULE_TYPES = {
    RequirementType.POLICY_EXPIRATION,
    RequirementType.POLICY_EFFECTIVE_DATE,
    RequirementType.NAMED_INSURED,
}

NON_WAIVABLE_RULE_TYPE_VALUES = {
    *(rt.value for rt in NON_WAIVABLE_RULE_TYPES),
    "entity_identity",
}

NON_WAIVABLE_REASON_CODES = {
    "POLICY_EXPIRED",
    "INVALID_DATE_RANGE_INVERTED",
    "POLICY_FUTURE_INCEPTION",
    "ENDORSEMENT_EXPIRED",
    "ENDORSEMENT_FUTURE_INCEPTION",
    "NAMED_INSURED_MISMATCH",
    "CONFIRMED_ENTITY_MISMATCH",
    "COVERAGE_NOT_FOUND",
}


def _is_finding_non_waivable(finding: Dict[str, Any]) -> bool:
    """Return True if a finding cannot legally or safely be waived."""
    r_type = finding.get("rule_type")
    r_code = finding.get("reason_code")
    return r_type in NON_WAIVABLE_RULE_TYPE_VALUES or r_code in NON_WAIVABLE_REASON_CODES


def _resolve_target_finding(
    findings: List[Dict[str, Any]],
    raw_finding_id: Optional[str],
    raw_rule_id: Optional[str],
) -> tuple[Optional[int], Optional[Dict[str, Any]]]:
    """Identify the unique target finding by finding_id and/or rule_id."""
    fid = (raw_finding_id or "").strip() or None
    rid = (raw_rule_id or "").strip() or None
    if not fid and not rid:
        return None, None

    fid_matches: List[tuple[int, Dict[str, Any]]] = []
    if fid:
        fid_matches = [(i, f) for i, f in enumerate(findings) if f.get("finding_id") == fid]
        if not fid_matches:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Requirement or finding '{fid}' not found in submission.",
            )

    rid_matches: List[tuple[int, Dict[str, Any]]] = []
    if rid:
        rid_matches = [
            (i, f)
            for i, f in enumerate(findings)
            if f.get("requirement_id") == rid or f.get("finding_id") == rid
        ]
        if not rid_matches:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Requirement or finding '{rid}' not found in submission.",
            )
        if len(rid_matches) > 1 and not fid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Multiple findings match rule_id '{rid}'; specify exact finding_id.",
            )

    if fid and rid:
        fid_idx = fid_matches[0][0]
        if fid_idx not in {idx for idx, _ in rid_matches}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Conflicting finding_id '{fid}' and rule_id '{rid}' specified.",
            )
        return fid_matches[0]

    return fid_matches[0] if fid_matches else rid_matches[0]


def _match_history_finding_indices(
    findings: List[Dict[str, Any]],
    rev_finding_id: Optional[str],
) -> List[int]:
    """Map an existing review_records.finding_id back to its automated finding index."""
    if not rev_finding_id:
        return []
    exact = [i for i, f in enumerate(findings) if f.get("finding_id") == rev_finding_id]
    if exact:
        return exact
    by_req = [i for i, f in enumerate(findings) if f.get("requirement_id") == rev_finding_id]
    return by_req if len(by_req) == 1 else []


def _replay_finding_states(
    findings: List[Dict[str, Any]],
    existing_reviews_asc: List[Dict[str, Any]],
) -> tuple[List[str], List[bool], List[bool]]:
    """Compute effective per-finding statuses from immutable evaluation + append-only review history."""
    effective_status = [
        str(f.get("status") or ComplianceStatus.UNABLE_TO_DETERMINE.value)
        for f in findings
    ]
    effective_hr = [bool(f.get("requires_human_review", False)) for f in findings]
    was_waived = [False] * len(findings)

    for rev in existing_reviews_asc:
        r_status = rev.get("review_status")
        matched = _match_history_finding_indices(findings, rev.get("finding_id"))

        if len(matched) == 1:
            idx = matched[0]
            orig_status = str(findings[idx].get("status") or "")
            if r_status == "waived" and not _is_finding_non_waivable(findings[idx]):
                effective_status[idx] = "waived"
                effective_hr[idx] = False
                was_waived[idx] = True
            elif r_status == "accepted_exception" and orig_status != ComplianceStatus.NON_COMPLIANT.value:
                effective_status[idx] = "resolved_compliant"
                effective_hr[idx] = False
                was_waived[idx] = False
            elif r_status in ("deficiency_notice_required", "rejected"):
                effective_status[idx] = orig_status or ComplianceStatus.REQUIRES_HUMAN_REVIEW.value
                effective_hr[idx] = True
                was_waived[idx] = False
        elif not rev.get("finding_id") and r_status == "accepted_exception":
            blocking = [
                i
                for i in range(len(findings))
                if effective_status[i] in BLOCKING_FINDING_STATUSES or effective_hr[i]
            ]
            if len(blocking) == 1 and effective_status[blocking[0]] == ComplianceStatus.REQUIRES_HUMAN_REVIEW.value:
                idx = blocking[0]
                effective_status[idx] = "resolved_compliant"
                effective_hr[idx] = False
                was_waived[idx] = False

    return effective_status, effective_hr, was_waived


def _compute_server_submission_status(
    action_review_status: str,
    findings: List[Dict[str, Any]],
    effective_status: List[str],
    effective_hr: List[bool],
    was_waived: List[bool],
) -> tuple[str, List[int]]:
    """Derive authoritative overall submission status from all findings after the current action."""
    unresolved = [
        i
        for i in range(len(findings))
        if effective_status[i] in BLOCKING_FINDING_STATUSES or effective_hr[i]
    ]

    if action_review_status == "rejected":
        return "rejected", unresolved
    if action_review_status == "deficiency_notice_required":
        return "deficiency_pending", unresolved

    if unresolved:
        unresolved_statuses = {effective_status[i] for i in unresolved}
        if ComplianceStatus.NON_COMPLIANT.value in unresolved_statuses:
            return ComplianceStatus.NON_COMPLIANT.value, unresolved
        if ComplianceStatus.MISSING_EVIDENCE.value in unresolved_statuses:
            return ComplianceStatus.MISSING_EVIDENCE.value, unresolved
        if ComplianceStatus.UNABLE_TO_DETERMINE.value in unresolved_statuses:
            return ComplianceStatus.UNABLE_TO_DETERMINE.value, unresolved
        return ComplianceStatus.REQUIRES_HUMAN_REVIEW.value, unresolved

    if any(was_waived):
        return "waived", unresolved
    return ComplianceStatus.COMPLIANT.value, unresolved


@app.post("/api/v1/submissions/{submission_id}/review")
def record_human_review_api(
    submission_id: str,
    payload: HumanReviewActionRequest,
    request: Request,
    current_user: AuthenticatedPrincipal = Depends(require_api_roles("reviewer", "admin")),
):
    """Record an auditable per-finding human review decision for a submission.

    Architecture principles enforced:
    1. AI extracts; deterministic rules evaluate; humans resolve uncertainty.
    2. Deterministic hard-failure rules cannot be silently bypassed or approved.
    3. Every blocking status (NON_COMPLIANT, MISSING_EVIDENCE, UNABLE_TO_DETERMINE,
       REQUIRES_HUMAN_REVIEW) is evaluated individually per finding before resolution.
    4. Waivers apply strictly to the targeted waivable finding and never waive others.
    5. Final submission status is calculated server-side from full finding state and
       persisted atomically alongside the append-only audit record; result_json is immutable.
    6. Reviewer identity is derived exclusively from the authenticated principal.
    """
    # 1. Validate decision value
    if payload.review_status not in VALID_REVIEW_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid review_status '{payload.review_status}'. Allowed values: {sorted(VALID_REVIEW_STATUSES)}",
        )

    # 2. Reject explicit blank/whitespace client reviewer_name and ALWAYS bind to server principal
    if payload.reviewer_name is not None and not payload.reviewer_name.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Reviewer name / identity cannot be empty.",
        )

    reviewer_name = (current_user.display_name or "").strip() or current_user.username
    reviewer_user_id = current_user.user_id

    # 3. Validate reviewer notes
    reviewer_notes = (payload.reviewer_notes or "").strip()
    if not reviewer_notes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Review notes cannot be empty.",
        )

    # 4. Validate requested operational submission status format (never trusted blindly)
    requested_status: Optional[str] = None
    if payload.update_submission_status and payload.update_submission_status.strip():
        requested_status = payload.update_submission_status.strip().lower()
        if requested_status not in VALID_SUBMISSION_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid update_submission_status '{requested_status}'. Allowed values: {sorted(VALID_SUBMISSION_STATUSES)}",
            )

    def _evaluate_and_build(
        sub: Optional[Dict[str, Any]],
        existing_reviews_asc: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not sub:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

        review = sub.get("result", {}).get("review", {})
        findings: List[Dict[str, Any]] = review.get("findings", []) or []

        target_idx, target_finding = _resolve_target_finding(
            findings,
            payload.finding_id,
            payload.rule_id,
        )
        effective_status, effective_hr, was_waived = _replay_finding_states(
            findings,
            existing_reviews_asc,
        )

        blocking_before = [
            i
            for i in range(len(findings))
            if effective_status[i] in BLOCKING_FINDING_STATUSES or effective_hr[i]
        ]
        non_compliant_before = [
            i for i in blocking_before if effective_status[i] == ComplianceStatus.NON_COMPLIANT.value
        ]
        recorded_finding = target_finding

        if payload.review_status == "waived":
            if target_finding is None or target_idx is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Waiver action requires specifying a target rule_id or finding_id.",
                )
            if _is_finding_non_waivable(target_finding):
                r_code = target_finding.get("reason_code")
                label = payload.rule_id or payload.finding_id or target_finding.get("requirement_id")
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Requirement '{label}' ({r_code}) is non-waivable. "
                        "Active coverage dates, valid policy terms, and verified legal entity "
                        "identity cannot be waived."
                    ),
                )
            effective_status[target_idx] = "waived"
            effective_hr[target_idx] = False
            was_waived[target_idx] = True

        elif payload.review_status == "accepted_exception":
            if target_finding is not None and target_idx is not None:
                orig_status = str(target_finding.get("status") or "")
                if (
                    effective_status[target_idx] == ComplianceStatus.NON_COMPLIANT.value
                    or (orig_status == ComplianceStatus.NON_COMPLIANT.value and not was_waived[target_idx])
                ):
                    req_id = target_finding.get("requirement_id", "unknown")
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"Cannot approve submission: deterministic hard failure exists on requirements: {[req_id]}. "
                            "Hard compliance failures cannot be accepted via blanket approval. "
                            "Explicit rule waiver or corrected documentation is required."
                        ),
                    )
                effective_status[target_idx] = "resolved_compliant"
                effective_hr[target_idx] = False
                was_waived[target_idx] = False
            else:
                if non_compliant_before:
                    failed_ids = [findings[i].get("requirement_id", "unknown") for i in non_compliant_before]
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"Cannot approve submission: deterministic hard failure exists on requirements: {failed_ids}. "
                            "Hard compliance failures cannot be accepted via blanket approval. "
                            "Explicit rule waiver or corrected documentation is required."
                        ),
                    )
                missing_or_unable = [
                    i
                    for i in blocking_before
                    if effective_status[i]
                    in {
                        ComplianceStatus.MISSING_EVIDENCE.value,
                        ComplianceStatus.UNABLE_TO_DETERMINE.value,
                    }
                ]
                if missing_or_unable:
                    missing_ids = [findings[i].get("requirement_id", "unknown") for i in missing_or_unable]
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"Cannot resolve submission without targeting specific findings for {missing_ids}. "
                            "Specify target rule_id or finding_id to resolve MISSING_EVIDENCE or UNABLE_TO_DETERMINE findings individually."
                        ),
                    )
                if len(blocking_before) > 1:
                    pending_ids = [findings[i].get("requirement_id", "unknown") for i in blocking_before]
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"Multiple unresolved blocking findings remain ({pending_ids}); "
                            "specify target rule_id or finding_id to resolve each finding individually."
                        ),
                    )
                if len(blocking_before) == 1:
                    idx = blocking_before[0]
                    effective_status[idx] = "resolved_compliant"
                    effective_hr[idx] = False
                    was_waived[idx] = False
                    recorded_finding = findings[idx]
                elif not findings and sub.get("overall_status") in {
                    ComplianceStatus.NON_COMPLIANT.value,
                    ComplianceStatus.MISSING_EVIDENCE.value,
                    ComplianceStatus.UNABLE_TO_DETERMINE.value,
                }:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"Cannot mark submission '{sub.get('overall_status')}' compliant "
                            "without underlying verifiable findings."
                        ),
                    )

        elif payload.review_status in ("deficiency_notice_required", "rejected"):
            if target_finding is not None and target_idx is not None:
                effective_status[target_idx] = str(
                    target_finding.get("status") or ComplianceStatus.REQUIRES_HUMAN_REVIEW.value
                )
                effective_hr[target_idx] = True
                was_waived[target_idx] = False

        server_status, remaining_blocking = _compute_server_submission_status(
            action_review_status=payload.review_status,
            findings=findings,
            effective_status=effective_status,
            effective_hr=effective_hr,
            was_waived=was_waived,
        )

        # Reject caller-supplied transitions that conflict with authoritative finding state
        if requested_status in ("compliant", "resolved_compliant"):
            if payload.review_status in ("deficiency_notice_required", "rejected"):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Cannot set submission status to '{requested_status}' when recording '{payload.review_status}'.",
                )
            if remaining_blocking:
                rem_ids = [findings[i].get("requirement_id", "unknown") for i in remaining_blocking]
                rem_statuses = sorted({effective_status[i] for i in remaining_blocking})
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Cannot set submission status to '{requested_status}' while unresolved blocking findings "
                        f"({rem_statuses}) remain on requirements: {rem_ids}."
                    ),
                )
            if server_status == "waived":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Cannot set submission status to 'compliant' when requirements were resolved by waiver.",
                )
        elif requested_status == "waived" and remaining_blocking:
            rem_ids = [findings[i].get("requirement_id", "unknown") for i in remaining_blocking]
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Cannot set submission status to 'waived' while unresolved blocking findings remain: {rem_ids}.",
            )

        audit_finding_id = (
            (recorded_finding.get("finding_id") or recorded_finding.get("requirement_id"))
            if recorded_finding
            else ((payload.finding_id or payload.rule_id or "").strip() or None)
        )
        audit_reason_code = recorded_finding.get("reason_code") if recorded_finding else None

        return {
            "finding_id": audit_finding_id,
            "reason_code": audit_reason_code,
            "review_status": payload.review_status,
            "reviewer_notes": reviewer_notes,
            "reviewer_name": reviewer_name,
            "reviewer_user_id": reviewer_user_id,
            "overall_status": server_status,
        }

    result = repository.record_human_review_atomic(
        submission_id=submission_id,
        evaluate_decision=_evaluate_and_build,
    )
    log_security_event(
        "human_review_recorded",
        outcome="success",
        username=current_user.username,
        user_id=current_user.user_id,
        role=current_user.role,
        client_ip=get_client_ip(request),
        path=request.url.path,
        details={
            "submission_id": submission_id,
            "review_status": payload.review_status,
            "overall_status": result["overall_status"],
        },
    )
    return {"status": "ok", "overall_status": result["overall_status"], "review": result["review"]}


@app.get(
    "/api/v1/dashboard/stats",
    dependencies=[Depends(require_api_roles("viewer", "analyst", "reviewer", "admin"))],
)
def get_dashboard_stats_api():
    """Return real aggregate statistics for the dashboard."""
    return repository.get_dashboard_stats()


def _execute_demo_scenario_sync(scenario_id: str, target_scen: Dict[str, Any]) -> Dict[str, Any]:
    """Execute bundled synthetic scenario synchronously under the processing gate with full cleanup."""
    from app.compliance.reconciliation import reconcile_extracted_document_set
    from app.extraction.pipeline import ExtractionPipelineService, reconcile_cross_document_set
    from app.extraction.providers.mock import HonestApplicationMockProvider
    from app.security.config import validate_requested_provider

    # Enforce server provider policy even for synthetic demo execution
    validate_requested_provider("mock")

    fixtures_dir = BASE_DIR.parent / "evaluation" / "fixtures" / "pdfs"
    cohort_fixtures_dir = BASE_DIR.parent / "evaluation" / "cohorts" / "fixtures"
    scen_pdf_dir = cohort_fixtures_dir / scenario_id
    if not scen_pdf_dir.exists():
        scen_pdf_dir = fixtures_dir

    sub_id = f"demo_{scenario_id.lower()}_{uuid4().hex[:6]}"
    eval_d = date(2025, 6, 15)
    reqs = get_default_requirements(
        subcontractor_name="Apex Electrical Contractors LLC",
        certificate_holder_name="General Contractor Corp",
    )

    demo_dest = get_upload_dir().resolve() / sub_id
    dir_created = False
    persisted = False

    with _processing_gate.acquire():
        try:
            demo_dest.mkdir(parents=True, exist_ok=True)
            dir_created = True

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
                stored_path = demo_dest / f"{doc_id}_{Path(fname).name}"
                stored_path.write_bytes(pdf_bytes)

                insp = review_service.pdf_processor.process_pdf(pdf_bytes, document_id=doc_id)
                inspections.append(insp)

                pipe_res = ExtractionPipelineService(
                    extraction_provider=HonestApplicationMockProvider()
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

            composite_set = ExtractedDocumentSet(
                subcontractor_id=target_scen.get("subcontractor_id", "sub_apex"),
                documents_submitted=[i.classified_type for i in inspections],
                is_document_set_complete=True,
                named_insured=named_insured_info,
                additional_named_insureds=additional_named_insureds,
                certificate_holder=cert_holder_info,
                coverages=combined_coverages,
                endorsements=combined_endorsements,
                raw_contradictions_detected=contradictions,
            )

            reconcile_cross_document_set(composite_set)
            composite_set.reconciliation_result = reconcile_extracted_document_set(
                document_set=composite_set,
                requirements=reqs,
                evaluation_date=eval_d,
                packet_id=sub_id,
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

            repository.create_submission_with_documents(
                submission_id=sub_id,
                name=f"Demo {scenario_id}: {target_scen.get('title', '')}",
                overall_status=review_result.overall_status.value,
                processing_status="completed",
                provider="mock",
                evaluation_date=eval_d.isoformat(),
                result_data=serialized,
                documents=doc_records,
                subcontractor_name=named_insured_info.name if named_insured_info else "Apex Electrical Contractors LLC",
            )
            persisted = True
            return {"status": "ok", "submission_id": sub_id, "scenario_id": scenario_id}
        except Exception:
            if dir_created and not persisted:
                shutil.rmtree(demo_dest, ignore_errors=True)
            raise


@app.post(
    "/api/v1/compliance/demo-run/{scenario_id}",
    dependencies=[Depends(require_api_roles("analyst", "reviewer", "admin"))],
)
async def run_synthetic_demo_scenario(scenario_id: str, request: Request):
    """Run a bundled synthetic scenario from cohort_scenarios_v1.json with one click."""
    if not is_demo_endpoint_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Synthetic demo-run endpoint is disabled in this environment.",
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", scenario_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid scenario identifier.",
        )

    from app.security.config import validate_requested_provider

    try:
        validate_requested_provider("mock")
    except PermissionError as perm_err:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(perm_err),
        ) from perm_err

    ann_file = BASE_DIR.parent / "evaluation" / "annotations" / "cohort_scenarios_v1.json"
    if not ann_file.exists():
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Annotations file missing")

    with open(ann_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    target_scen = None
    for s in data.get("scenarios", []):
        if s["scenario_id"] == scenario_id:
            target_scen = s
            break

    if not target_scen:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Scenario '{scenario_id}' not found")

    try:
        return await run_in_threadpool(_execute_demo_scenario_sync, scenario_id, target_scen)
    except PermissionError as perm_err:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(perm_err),
        ) from perm_err
    except ProcessingConcurrencyLimitError as busy_err:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(busy_err),
            headers={"Retry-After": "5"},
        ) from busy_err
    except Exception as exc:
        correlation_id = f"err_{uuid4().hex[:10]}"
        safe_exc = sanitize_error_message(str(exc))
        app_logger.exception(
            "Demo scenario execution failed correlation_id=%s detail=%s",
            correlation_id,
            safe_exc,
        )
        log_security_event(
            "demo_scenario_error",
            outcome="error",
            client_ip=get_client_ip(request),
            path=request.url.path,
            details={"correlation_id": correlation_id, "error_type": exc.__class__.__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Synthetic scenario execution failed (reference: {correlation_id}).",
        ) from exc


# ============================================================================
# Web UI Pages
# ============================================================================

def _ui_context(request: Request, current_user: AuthenticatedPrincipal, **extra: Any) -> Dict[str, Any]:
    return {
        "current_user": current_user,
        "csrf_token": current_user.csrf_token,
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
        **extra,
    }


@app.get(
    "/",
    response_class=HTMLResponse,
    dependencies=[Depends(require_ui_roles("viewer", "analyst", "reviewer", "admin"))],
)
def index_redirect():
    """Root redirects to Dashboard."""
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(
    request: Request,
    current_user: AuthenticatedPrincipal = Depends(
        require_ui_roles("viewer", "analyst", "reviewer", "admin")
    ),
):
    """Page 1: Executive Dashboard showing real statistics and recent submissions."""
    stats = repository.get_dashboard_stats()
    recent = repository.list_submissions(limit=10)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=_ui_context(
            request,
            current_user,
            stats=stats,
            recent_submissions=recent,
            active_nav="dashboard",
        ),
    )


@app.get("/submissions/new", response_class=HTMLResponse)
def new_submission_page(
    request: Request,
    current_user: AuthenticatedPrincipal = Depends(
        require_ui_roles("analyst", "reviewer", "admin")
    ),
):
    """Page 2: Document upload and review configuration form."""
    return templates.TemplateResponse(
        request=request,
        name="new_submission.html",
        context=_ui_context(
            request,
            current_user,
            active_nav="new",
        ),
    )


@app.get("/submissions/{submission_id}", response_class=HTMLResponse)
def submission_detail_page(
    request: Request,
    submission_id: str,
    current_user: AuthenticatedPrincipal = Depends(
        require_ui_roles("viewer", "analyst", "reviewer", "admin")
    ),
):
    """Page 3: Submission review results, statuses, and findings."""
    sub = repository.get_submission_by_id(submission_id)
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

    return templates.TemplateResponse(
        request=request,
        name="submission_detail.html",
        context=_ui_context(
            request,
            current_user,
            submission=sub,
            active_nav="history",
        ),
    )


@app.get("/submissions/{submission_id}/evidence", response_class=HTMLResponse)
def evidence_viewer_page(
    request: Request,
    submission_id: str,
    current_user: AuthenticatedPrincipal = Depends(
        require_ui_roles("viewer", "analyst", "reviewer", "admin")
    ),
):
    """Page 4: Evidence trace viewer showing source quotes, page numbers, and verification."""
    sub = repository.get_submission_by_id(submission_id)
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

    return templates.TemplateResponse(
        request=request,
        name="evidence_viewer.html",
        context=_ui_context(
            request,
            current_user,
            submission=sub,
            active_nav="history",
        ),
    )


@app.get("/submissions/{submission_id}/review", response_class=HTMLResponse)
def human_review_page(
    request: Request,
    submission_id: str,
    current_user: AuthenticatedPrincipal = Depends(
        require_ui_roles("reviewer", "admin")
    ),
):
    """Page 5: Human review resolution form for pending flags or exceptions."""
    sub = repository.get_submission_by_id(submission_id)
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

    return templates.TemplateResponse(
        request=request,
        name="human_review.html",
        context=_ui_context(
            request,
            current_user,
            submission=sub,
            active_nav="history",
        ),
    )


@app.get("/submissions", response_class=HTMLResponse)
def submission_history_page(
    request: Request,
    status: Optional[str] = None,
    q: Optional[str] = None,
    current_user: AuthenticatedPrincipal = Depends(
        require_ui_roles("viewer", "analyst", "reviewer", "admin")
    ),
):
    """Page 6: Auditable submission history with search and status filtering."""
    submissions = repository.list_submissions(status_filter=status, search_query=q)
    return templates.TemplateResponse(
        request=request,
        name="submission_history.html",
        context=_ui_context(
            request,
            current_user,
            submissions=submissions,
            status_filter=status,
            search_query=q,
            active_nav="history",
        ),
    )
