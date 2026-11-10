"""FastAPI authentication, RBAC, CSRF, and rate-limiting dependencies."""

from __future__ import annotations

import hmac
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, status

from app.security.audit_log import log_security_event
from app.security.config import (
    SESSION_COOKIE_NAME,
    get_api_mutation_rate_limits,
)
from app.security.rate_limit import api_mutation_rate_limiter
from app.security.service import AuthService, AuthenticatedPrincipal

SAFE_HTTP_METHODS = {"GET", "HEAD", "OPTIONS"}


class BrowserAuthRedirectRequired(Exception):
    """Raised when an unauthenticated browser UI request must redirect to /login."""

    def __init__(self, next_url: str = "/dashboard") -> None:
        self.next_url = sanitize_next_url(next_url)
        super().__init__(f"Authentication required; redirect to /login?next={self.next_url}")


def sanitize_next_url(next_url: Optional[str]) -> str:
    """Ensure post-login redirect targets are strictly same-origin relative paths."""
    if not next_url:
        return "/dashboard"
    candidate = next_url.strip()
    if not candidate.startswith("/") or candidate.startswith("//") or candidate.startswith("/\\"):
        return "/dashboard"
    if "://" in candidate or "\r" in candidate or "\n" in candidate:
        return "/dashboard"
    return candidate


def get_client_ip(request: Request) -> str:
    """Return direct peer host string for auditing and rate limiting."""
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _extract_bearer_token(request: Request) -> Optional[str]:
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return None
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def create_auth_dependencies(auth_service: AuthService) -> Dict[str, Any]:
    """Build request-bound authentication, CSRF, and RBAC dependencies."""

    async def _resolve_optional_principal(request: Request) -> Optional[AuthenticatedPrincipal]:
        cached = getattr(request.state, "_auth_checked", False)
        if cached:
            return getattr(request.state, "current_user", None)

        principal: Optional[AuthenticatedPrincipal] = None
        bearer = _extract_bearer_token(request)
        if request.headers.get("Authorization") is not None:
            # Explicit Authorization header must validate cleanly; never fall back to session cookie
            if bearer:
                principal = auth_service.resolve_token(bearer, expected_kind="api_token")
        else:
            session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
            if session_cookie:
                principal = auth_service.resolve_token(session_cookie, expected_kind="session")

        request.state._auth_checked = True
        request.state.current_user = principal
        return principal

    async def _verify_csrf_if_cookie_session(
        request: Request,
        principal: AuthenticatedPrincipal,
    ) -> None:
        if request.method in SAFE_HTTP_METHODS:
            return
        if principal.auth_method != "session":
            return

        expected = principal.csrf_token or ""
        provided = request.headers.get("X-CSRF-Token")
        if not provided:
            content_type = (request.headers.get("content-type") or "").lower()
            if (
                "application/x-www-form-urlencoded" in content_type
                or "multipart/form-data" in content_type
            ):
                form = await request.form()
                raw_field = form.get("csrf_token")
                if isinstance(raw_field, str):
                    provided = raw_field

        if not provided or not expected or not hmac.compare_digest(provided, expected):
            log_security_event(
                "csrf_failure",
                outcome="denied",
                username=principal.username,
                user_id=principal.user_id,
                role=principal.role,
                client_ip=get_client_ip(request),
                path=request.url.path,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF token missing or invalid.",
            )

    def _enforce_mutation_rate_limit(
        request: Request,
        principal: AuthenticatedPrincipal,
    ) -> None:
        if request.method in SAFE_HTTP_METHODS:
            return
        max_req, window = get_api_mutation_rate_limits()
        allowed, retry_after = api_mutation_rate_limiter.check_and_record(
            f"mut:{principal.user_id}",
            limit=max_req,
            window_seconds=window,
        )
        if not allowed:
            log_security_event(
                "api_rate_limited",
                outcome="rate_limited",
                username=principal.username,
                user_id=principal.user_id,
                role=principal.role,
                client_ip=get_client_ip(request),
                path=request.url.path,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please slow down and retry later.",
                headers={"Retry-After": str(retry_after)},
            )

    def require_api_roles(*allowed_roles: str) -> Callable[..., AuthenticatedPrincipal]:
        allowed_set = set(allowed_roles)

        async def _dep(request: Request) -> AuthenticatedPrincipal:
            principal = await _resolve_optional_principal(request)
            if not principal:
                log_security_event(
                    "auth_failure",
                    outcome="denied",
                    client_ip=get_client_ip(request),
                    path=request.url.path,
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required.",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if principal.role not in allowed_set:
                log_security_event(
                    "forbidden_role",
                    outcome="denied",
                    username=principal.username,
                    user_id=principal.user_id,
                    role=principal.role,
                    client_ip=get_client_ip(request),
                    path=request.url.path,
                    details={"required_roles": sorted(allowed_set)},
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Insufficient permissions for this operation.",
                )
            await _verify_csrf_if_cookie_session(request, principal)
            _enforce_mutation_rate_limit(request, principal)
            return principal

        return _dep

    def require_ui_roles(*allowed_roles: str) -> Callable[..., AuthenticatedPrincipal]:
        allowed_set = set(allowed_roles)

        async def _dep(request: Request) -> AuthenticatedPrincipal:
            principal = await _resolve_optional_principal(request)
            if not principal:
                next_target = request.url.path
                if request.url.query:
                    next_target = f"{next_target}?{request.url.query}"
                raise BrowserAuthRedirectRequired(next_url=next_target)
            if principal.role not in allowed_set:
                log_security_event(
                    "forbidden_role",
                    outcome="denied",
                    username=principal.username,
                    user_id=principal.user_id,
                    role=principal.role,
                    client_ip=get_client_ip(request),
                    path=request.url.path,
                    details={"required_roles": sorted(allowed_set)},
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Insufficient permissions to view this page.",
                )
            await _verify_csrf_if_cookie_session(request, principal)
            return principal

        return _dep

    return {
        "resolve_optional_principal": _resolve_optional_principal,
        "require_api_roles": require_api_roles,
        "require_ui_roles": require_ui_roles,
    }


def login_redirect_location(next_url: str) -> str:
    safe_next = sanitize_next_url(next_url)
    return f"/login?next={quote(safe_next, safe='/?=&')}"
