"""Structured security audit logging without exposing credentials or raw tokens."""

from __future__ import annotations

import json
import logging
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
}


def _sanitize_details(details: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not details:
        return {}
    safe: Dict[str, Any] = {}
    for k, v in details.items():
        if k.lower() in _SENSITIVE_KEYS:
            safe[k] = "[REDACTED]"
        else:
            safe[k] = v
    return safe


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
    level = logging.WARNING if outcome in {"denied", "failure", "rate_limited", "blocked"} else logging.INFO
    security_logger.log(level, json.dumps(payload, sort_keys=True, default=str))
