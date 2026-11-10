"""Structured security audit logging without exposing credentials or raw tokens."""

from __future__ import annotations

import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

security_logger = logging.getLogger("subguard.security")

_SENSITIVE_KEYS = {
    "password",
    "password_hash",
    "token",
    "raw_token",
    "api_token",
    "session_token",
    "csrf_token",
    "authorization",
    "cookie",
    "secret",
    "api_key",
    "gemini_api_key",
    "openrouter_api_key",
    "document_text",
    "source_text",
    "raw_text",
}

_SECRET_PATTERNS = (
    re.compile(r"sk-or-v1-[a-zA-Z0-9_\-]+"),
    re.compile(r"\bsk-[a-zA-Z0-9_\-]{20,}\b"),
    re.compile(r"AIza[0-9A-Za-z\-_]{20,}"),
    re.compile(r"\bsg_[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{10,}"),
    re.compile(r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|secret|password)\s*[:=]\s*\S+"),
)


def _redact_string(value: str) -> str:
    cleaned = value
    for pat in _SECRET_PATTERNS:
        cleaned = pat.sub("[REDACTED]", cleaned)
    return cleaned


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _sanitize_details(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _sanitize_details(details: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not details:
        return {}
    safe: Dict[str, Any] = {}
    for k, v in details.items():
        if k.lower() in _SENSITIVE_KEYS:
            safe[k] = "[REDACTED]"
        else:
            safe[k] = _sanitize_value(v)
    return safe


def configure_security_logging(*, audit_log_path: Optional[Path] = None) -> None:
    """Configure INFO-level console + optional rotating file handler without duplicates."""
    security_logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    has_console = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in security_logger.handlers
    )
    if not has_console:
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(formatter)
        security_logger.addHandler(console)

    if audit_log_path is not None:
        resolved = Path(audit_log_path).resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        already_attached = any(
            isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).resolve() == resolved
            for h in security_logger.handlers
        )
        if not already_attached:
            file_handler = RotatingFileHandler(
                str(resolved),
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(formatter)
            security_logger.addHandler(file_handler)


def log_security_event(
    event_type: str,
    *,
    outcome: str,
    username: Optional[str] = None,
    user_id: Optional[str] = None,
    role: Optional[str] = None,
    client_ip: Optional[str] = None,
    path: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit a structured JSON security audit log record."""
    if security_logger.level == logging.NOTSET or security_logger.level > logging.INFO:
        security_logger.setLevel(logging.INFO)

    payload: Dict[str, Any] = {
        "event": event_type,
        "outcome": outcome,
        "username": username,
        "user_id": user_id,
        "role": role,
        "client_ip": client_ip,
        "path": path,
        "details": _sanitize_details(details),
    }
    level = logging.WARNING if outcome in {"denied", "failure", "rate_limited", "blocked", "error"} else logging.INFO
    security_logger.log(level, json.dumps(payload, sort_keys=True, default=str))
