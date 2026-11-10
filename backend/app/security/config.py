"""Security configuration and fail-closed startup checks for SubGuard AI."""

from __future__ import annotations

import ipaddress
import logging
import os
import secrets
from pathlib import Path
from typing import List, Optional

from app.persistence.database import get_connection

logger = logging.getLogger("subguard.security")

VALID_APP_ENVS = {"development", "test", "production"}
SESSION_COOKIE_NAME = "subguard_session"
PRE_LOGIN_CSRF_COOKIE_NAME = "subguard_login_csrf"

_EPHEMERAL_DEV_SECRET: Optional[str] = None
_WEAK_SECRET_LITERALS = {
    "changeme",
    "change-me",
    "secret",
    "subguard",
    "subguard-secret",
    "dev-secret",
    "development",
    "test-secret",
    "password",
    "default",
    "123456",
}


def get_app_env() -> str:
    """Return normalized deployment environment ('development', 'test', or 'production')."""
    raw = os.getenv("SUBGUARD_ENV", "development").strip().lower()
    if raw not in VALID_APP_ENVS:
        raise RuntimeError(
            f"Invalid SUBGUARD_ENV={raw!r}. Must be one of: {sorted(VALID_APP_ENVS)}"
        )
    return raw


def is_production_env() -> bool:
    """Return True when running in production mode."""
    return get_app_env() == "production"


def get_session_secret() -> str:
    """Return signing/HMAC secret for cookie/CSRF operations.

    In production, SUBGUARD_SESSION_SECRET is mandatory (>= 32 chars, non-placeholder)
    and startup fails closed if absent or weak.
    In development/test, if unset, an ephemeral per-process CSPRNG secret is generated;
    if explicitly set, it must still meet minimum length requirements.
    """
    global _EPHEMERAL_DEV_SECRET
    raw = os.getenv("SUBGUARD_SESSION_SECRET", "").strip()
    if raw:
        if len(raw) < 32 or raw.lower() in _WEAK_SECRET_LITERALS:
            raise RuntimeError(
                "SUBGUARD_SESSION_SECRET must be at least 32 non-placeholder characters."
            )
        return raw

    if is_production_env():
        raise RuntimeError(
            "SUBGUARD_SESSION_SECRET is required when SUBGUARD_ENV=production."
        )

    if _EPHEMERAL_DEV_SECRET is None:
        _EPHEMERAL_DEV_SECRET = secrets.token_urlsafe(32)
        logger.warning(
            "SUBGUARD_SESSION_SECRET is not set; using ephemeral per-process secret in %s mode.",
            get_app_env(),
        )
    return _EPHEMERAL_DEV_SECRET


def get_cors_origins() -> List[str]:
    """Return explicit allowed CORS origins. Empty list disables cross-origin access."""
    raw = os.getenv("SUBGUARD_CORS_ORIGINS", "").strip()
    if not raw:
        return []

    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    for origin in origins:
        if origin == "*" or origin.endswith("://*"):
            raise RuntimeError(
                "Wildcard CORS origin '*' is prohibited when authentication cookies are enabled."
            )
        if not (origin.startswith("https://") or origin.startswith("http://")):
            raise RuntimeError(
                f"Invalid CORS origin {origin!r}: expected http:// or https:// scheme."
            )
        if is_production_env() and not origin.startswith("https://"):
            raise RuntimeError(
                f"Non-HTTPS CORS origin {origin!r} is prohibited in production mode."
            )
    return origins


def is_cookie_secure(request_scheme: Optional[str] = None, forwarded_proto: Optional[str] = None) -> bool:
    """Determine whether cookies must carry the Secure attribute."""
    raw = os.getenv("SUBGUARD_COOKIE_SECURE", "").strip().lower()
    if is_production_env():
        if raw in {"0", "false", "no"}:
            raise RuntimeError(
                "SUBGUARD_COOKIE_SECURE cannot be disabled when SUBGUARD_ENV=production."
            )
        return True

    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False

    proto = (forwarded_proto or request_scheme or "http").strip().lower()
    return proto == "https"


def is_https_enforced() -> bool:
    """Return True if plaintext HTTP requests must be rejected."""
    raw = os.getenv("SUBGUARD_ENFORCE_HTTPS", "").strip().lower()
    if is_production_env():
        if raw in {"0", "false", "no"}:
            raise RuntimeError(
                "SUBGUARD_ENFORCE_HTTPS cannot be disabled when SUBGUARD_ENV=production."
            )
        return True
    return raw in {"1", "true", "yes"}


def get_trusted_proxy_ips() -> List[str]:
    """Return configured trusted reverse proxy IP/CIDR entries for X-Forwarded-Proto."""
    raw = os.getenv("SUBGUARD_TRUSTED_PROXIES", "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def is_trusted_proxy_client(client_host: Optional[str]) -> bool:
    """Check whether a direct peer IP is in SUBGUARD_TRUSTED_PROXIES."""
    if not client_host:
        return False
    trusted = get_trusted_proxy_ips()
    if not trusted:
        return False
    try:
        peer_ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False

    for entry in trusted:
        try:
            if "/" in entry:
                if peer_ip in ipaddress.ip_network(entry, strict=False):
                    return True
            elif peer_ip == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def resolve_effective_scheme(client_host: Optional[str], url_scheme: str, x_forwarded_proto: Optional[str]) -> str:
    """Resolve effective request scheme without trusting X-Forwarded-Proto from untrusted peers."""
    if x_forwarded_proto and is_trusted_proxy_client(client_host):
        first_proto = x_forwarded_proto.split(",")[0].strip().lower()
        if first_proto in {"http", "https"}:
            return first_proto
    return (url_scheme or "http").lower()


def get_session_ttl_seconds() -> int:
    """Return absolute session lifetime in seconds (default 8 hours)."""
    raw = os.getenv("SUBGUARD_SESSION_TTL_SECONDS", "28800").strip()
    val = int(raw)
    return max(300, val)


def get_session_idle_seconds() -> int:
    """Return session idle timeout in seconds (default 1 hour)."""
    raw = os.getenv("SUBGUARD_SESSION_IDLE_SECONDS", "3600").strip()
    val = int(raw)
    return max(60, val)


def is_demo_endpoint_enabled() -> bool:
    """Disable synthetic demo-run mutation endpoint in production unless explicitly opted in."""
    raw = os.getenv("SUBGUARD_ENABLE_DEMO_ENDPOINT", "").strip().lower()
    if is_production_env():
        return raw in {"1", "true", "yes"}
    return raw not in {"0", "false", "no"}


def get_login_rate_limits() -> tuple[int, int, int]:
    """Return (per_user_max, per_ip_max, window_seconds) for login throttling."""
    per_user = max(1, int(os.getenv("SUBGUARD_LOGIN_LIMIT_PER_USER", "5").strip()))
    per_ip = max(1, int(os.getenv("SUBGUARD_LOGIN_LIMIT_PER_IP", "25").strip()))
    window = max(1, int(os.getenv("SUBGUARD_LOGIN_LIMIT_WINDOW_SECONDS", "60").strip()))
    return per_user, per_ip, window


def get_api_mutation_rate_limits() -> tuple[int, int]:
    """Return (max_requests, window_seconds) for authenticated state-changing API routes."""
    max_req = max(1, int(os.getenv("SUBGUARD_API_MUTATION_LIMIT", "120").strip()))
    window = max(1, int(os.getenv("SUBGUARD_API_MUTATION_WINDOW_SECONDS", "60").strip()))
    return max_req, window


def validate_production_config(db_path: Optional[Path] = None, *, require_active_admin: bool = True) -> None:
    """Fail closed when mandatory production security settings or admin user are missing."""
    get_session_secret()
    get_cors_origins()
    is_cookie_secure()
    is_https_enforced()

    if is_production_env() and require_active_admin:
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin' AND is_active = 1"
            ).fetchone()
            cnt = int(row["cnt"]) if row else 0
            if cnt < 1:
                raise RuntimeError(
                    "Production startup refused: no active admin user exists in database. "
                    "Provision an administrator via 'python -m app.security.bootstrap' or "
                    "SUBGUARD_BOOTSTRAP_ADMIN_USERNAME / SUBGUARD_BOOTSTRAP_ADMIN_PASSWORD."
                )
        finally:
            conn.close()
