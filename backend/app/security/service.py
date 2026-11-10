"""Authentication, user, session, API token, and CSRF persistence services."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from app.persistence.database import get_connection
from app.security.config import (
    get_session_idle_seconds,
    get_session_secret,
    get_session_ttl_seconds,
)
from app.security.passwords import (
    dummy_password_verify,
    generate_opaque_token,
    hash_opaque_token,
    hash_password,
    verify_password,
)

VALID_ROLES = ("viewer", "analyst", "reviewer", "admin")
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{3,64}$")


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Server-verified identity attached to an authenticated request."""

    user_id: str
    username: str
    display_name: str
    role: str
    auth_method: str  # "session" | "bearer"
    session_id: Optional[str] = None
    csrf_token: Optional[str] = None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_username(username: str) -> str:
    cleaned = (username or "").strip().lower()
    if not USERNAME_PATTERN.match(cleaned):
        raise ValueError(
            "Username must be 3-64 characters and contain only letters, digits, '.', '_', or '-'."
        )
    return cleaned


def _validate_role(role: str) -> str:
    cleaned = (role or "").strip().lower()
    if cleaned not in VALID_ROLES:
        raise ValueError(f"Invalid role {role!r}. Allowed roles: {list(VALID_ROLES)}")
    return cleaned


def generate_login_csrf_token() -> str:
    """Generate signed pre-login synchronizer CSRF token."""
    nonce = secrets.token_urlsafe(24)
    sig = hmac.new(
        get_session_secret().encode("utf-8"),
        nonce.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{nonce}.{sig}"


def verify_login_csrf_token(cookie_token: Optional[str], submitted_token: Optional[str]) -> bool:
    """Verify pre-login CSRF cookie matches submitted token and valid HMAC."""
    if not cookie_token or not submitted_token:
        return False
    if not hmac.compare_digest(cookie_token, submitted_token):
        return False
    if "." not in submitted_token:
        return False
    nonce, provided_sig = submitted_token.rsplit(".", 1)
    expected_sig = hmac.new(
        get_session_secret().encode("utf-8"),
        nonce.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(provided_sig, expected_sig)


class AuthService:
    """Database-backed user, session, and revocable API token manager."""

    def __init__(self, db_path_provider: Optional[Callable[[], Optional[Path]]] = None) -> None:
        self._db_path_provider = db_path_provider

    def _db_path(self, override: Optional[Path] = None) -> Optional[Path]:
        if override is not None:
            return override
        if self._db_path_provider is not None:
            return self._db_path_provider()
        return None

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        role: str = "viewer",
        is_active: bool = True,
        enforce_password_policy: bool = True,
        db_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Create a new user with parameterized scrypt password hash."""
        norm_username = _normalize_username(username)
        norm_display = (display_name or "").strip()
        if not norm_display or len(norm_display) > 120:
            raise ValueError("Display name must be between 1 and 120 characters.")
        norm_role = _validate_role(role)
        pw_hash = hash_password(password, enforce_policy=enforce_password_policy)

        user_id = f"usr_{uuid4().hex[:12]}"
        ts = _now_utc().isoformat()
        conn = get_connection(self._db_path(db_path))
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO users (
                        id, username, display_name, password_hash, role, is_active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        norm_username,
                        norm_display,
                        pw_hash,
                        norm_role,
                        1 if is_active else 0,
                        ts,
                        ts,
                    ),
                )
        finally:
            conn.close()

        return {
            "id": user_id,
            "username": norm_username,
            "display_name": norm_display,
            "role": norm_role,
            "is_active": is_active,
            "created_at": ts,
            "updated_at": ts,
        }

    def list_users(self, *, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
        """Return safe user metadata without password hashes."""
        conn = get_connection(self._db_path(db_path))
        try:
            rows = conn.execute(
                """
                SELECT id, username, display_name, role, is_active, created_at, updated_at
                FROM users
                ORDER BY created_at ASC
                """
            ).fetchall()
            return [
                {
                    **dict(r),
                    "is_active": bool(r["is_active"]),
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_user_by_username(
        self, username: str, *, db_path: Optional[Path] = None
    ) -> Optional[Dict[str, Any]]:
        conn = get_connection(self._db_path(db_path))
        try:
            row = conn.execute(
                """
                SELECT id, username, display_name, role, is_active, created_at, updated_at
                FROM users WHERE username = ?
                """,
                ((username or "").strip().lower(),),
            ).fetchone()
            if not row:
                return None
            return {**dict(row), "is_active": bool(row["is_active"])}
        finally:
            conn.close()

    def update_user(
        self,
        user_id: str,
        *,
        role: Optional[str] = None,
        is_active: Optional[bool] = None,
        display_name: Optional[str] = None,
        db_path: Optional[Path] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update role, active flag, or display name; revokes sessions on deactivation or role change."""
        updates: List[str] = []
        params: List[Any] = []
        revoke_sessions = False

        if role is not None:
            updates.append("role = ?")
            params.append(_validate_role(role))
            revoke_sessions = True
        if is_active is not None:
            updates.append("is_active = ?")
            params.append(1 if is_active else 0)
            if not is_active:
                revoke_sessions = True
        if display_name is not None:
            norm_display = display_name.strip()
            if not norm_display or len(norm_display) > 120:
                raise ValueError("Display name must be between 1 and 120 characters.")
            updates.append("display_name = ?")
            params.append(norm_display)

        if not updates:
            raise ValueError("At least one updatable field is required.")

        ts = _now_utc().isoformat()
        updates.append("updated_at = ?")
        params.append(ts)
        params.append(user_id)

        conn = get_connection(self._db_path(db_path))
        try:
            with conn:
                cur = conn.execute(
                    f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
                    tuple(params),
                )
                if cur.rowcount == 0:
                    return None
                if revoke_sessions:
                    conn.execute(
                        "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                        (ts, user_id),
                    )
                row = conn.execute(
                    """
                    SELECT id, username, display_name, role, is_active, created_at, updated_at
                    FROM users WHERE id = ?
                    """,
                    (user_id,),
                ).fetchone()
                return {**dict(row), "is_active": bool(row["is_active"])} if row else None
        finally:
            conn.close()

    def authenticate_credentials(
        self, username: str, password: str, *, db_path: Optional[Path] = None
    ) -> Optional[Dict[str, Any]]:
        """Verify username and password against stored scrypt hash. Fail closed on inactive users."""
        norm_username = (username or "").strip().lower()
        if not norm_username or not password:
            dummy_password_verify(password or "")
            return None

        conn = get_connection(self._db_path(db_path))
        try:
            row = conn.execute(
                """
                SELECT id, username, display_name, password_hash, role, is_active
                FROM users
                WHERE username = ?
                """,
                (norm_username,),
            ).fetchone()
        finally:
            conn.close()

        if not row:
            dummy_password_verify(password)
            return None

        if not verify_password(password, row["password_hash"]):
            return None

        if not bool(row["is_active"]):
            return None

        return {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
        }

    def create_session(
        self, user_id: str, *, ttl_seconds: Optional[int] = None, db_path: Optional[Path] = None
    ) -> tuple[str, str, str]:
        """Create an opaque browser session and return (raw_session_token, csrf_token, session_id).

        Only the SHA-256 digest of the session token is persisted in SQLite.
        """
        raw_token = generate_opaque_token()
        token_hash = hash_opaque_token(raw_token)
        csrf_token = generate_opaque_token()
        session_id = f"ses_{uuid4().hex[:12]}"

        now = _now_utc()
        ttl = ttl_seconds if ttl_seconds is not None else get_session_ttl_seconds()
        expires_at = (now + timedelta(seconds=ttl)).isoformat()
        now_iso = now.isoformat()

        conn = get_connection(self._db_path(db_path))
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO sessions (
                        id, user_id, token_hash, token_kind, label, csrf_token,
                        created_at, last_seen_at, expires_at, revoked_at
                    ) VALUES (?, ?, ?, 'session', 'browser', ?, ?, ?, ?, NULL)
                    """,
                    (session_id, user_id, token_hash, csrf_token, now_iso, now_iso, expires_at),
                )
        finally:
            conn.close()

        return raw_token, csrf_token, session_id

    def create_api_token(
        self,
        *,
        user_id: str,
        label: str,
        ttl_days: int = 30,
        db_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Issue a revocable opaque API Bearer token; returns plaintext token once."""
        clean_label = (label or "").strip()
        if not clean_label or len(clean_label) > 100:
            raise ValueError("API token label must be 1-100 characters.")
        if ttl_days < 1 or ttl_days > 365:
            raise ValueError("API token ttl_days must be between 1 and 365.")

        raw_token = f"sg_{generate_opaque_token()}"
        token_hash = hash_opaque_token(raw_token)
        token_id = f"tok_{uuid4().hex[:12]}"

        now = _now_utc()
        expires_at = (now + timedelta(days=ttl_days)).isoformat()
        now_iso = now.isoformat()

        conn = get_connection(self._db_path(db_path))
        try:
            with conn:
                user_row = conn.execute(
                    "SELECT id, is_active FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()
                if not user_row or not bool(user_row["is_active"]):
                    raise ValueError("Target user does not exist or is inactive.")
                conn.execute(
                    """
                    INSERT INTO sessions (
                        id, user_id, token_hash, token_kind, label, csrf_token,
                        created_at, last_seen_at, expires_at, revoked_at
                    ) VALUES (?, ?, ?, 'api_token', ?, NULL, ?, ?, ?, NULL)
                    """,
                    (token_id, user_id, token_hash, clean_label, now_iso, now_iso, expires_at),
                )
        finally:
            conn.close()

        return {
            "id": token_id,
            "user_id": user_id,
            "label": clean_label,
            "token": raw_token,  # Shown once to administrator
            "created_at": now_iso,
            "expires_at": expires_at,
        }

    def list_tokens(self, *, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
        """List session/token metadata without exposing raw tokens or hashes."""
        conn = get_connection(self._db_path(db_path))
        try:
            rows = conn.execute(
                """
                SELECT s.id, s.user_id, u.username, u.role, s.token_kind, s.label,
                       s.created_at, s.last_seen_at, s.expires_at, s.revoked_at
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                ORDER BY s.created_at DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def revoke_session_by_id(self, session_id: str, *, db_path: Optional[Path] = None) -> bool:
        """Revoke a session or API token by its ID."""
        ts = _now_utc().isoformat()
        conn = get_connection(self._db_path(db_path))
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                    (ts, session_id),
                )
                return cur.rowcount > 0
        finally:
            conn.close()

    def revoke_session_by_raw_token(self, raw_token: str, *, db_path: Optional[Path] = None) -> bool:
        """Revoke a browser session on logout using its raw token hash."""
        if not raw_token:
            return False
        token_hash = hash_opaque_token(raw_token)
        ts = _now_utc().isoformat()
        conn = get_connection(self._db_path(db_path))
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                    (ts, token_hash),
                )
                return cur.rowcount > 0
        finally:
            conn.close()

    def resolve_token(
        self,
        raw_token: str,
        *,
        expected_kind: str,
        db_path: Optional[Path] = None,
    ) -> Optional[AuthenticatedPrincipal]:
        """Resolve and validate an opaque session or Bearer API token against SQLite."""
        if not raw_token or expected_kind not in {"session", "api_token"}:
            return None

        token_hash = hash_opaque_token(raw_token)
        now = _now_utc()
        conn = get_connection(self._db_path(db_path))
        try:
            row = conn.execute(
                """
                SELECT s.id AS session_id, s.token_kind, s.csrf_token, s.last_seen_at,
                       s.expires_at, s.revoked_at,
                       u.id AS user_id, u.username, u.display_name, u.role, u.is_active
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.token_kind = ?
                """,
                (token_hash, expected_kind),
            ).fetchone()

            if not row:
                return None
            if row["revoked_at"] is not None or not bool(row["is_active"]):
                return None

            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now >= expires_at:
                return None

            if expected_kind == "session":
                last_seen = datetime.fromisoformat(row["last_seen_at"])
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                idle_limit = get_session_idle_seconds()
                if (now - last_seen).total_seconds() > idle_limit:
                    with conn:
                        conn.execute(
                            "UPDATE sessions SET revoked_at = ? WHERE id = ?",
                            (now.isoformat(), row["session_id"]),
                        )
                    return None

            with conn:
                conn.execute(
                    "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
                    (now.isoformat(), row["session_id"]),
                )

            return AuthenticatedPrincipal(
                user_id=row["user_id"],
                username=row["username"],
                display_name=row["display_name"],
                role=row["role"],
                auth_method="session" if expected_kind == "session" else "bearer",
                session_id=row["session_id"],
                csrf_token=row["csrf_token"],
            )
        finally:
            conn.close()
