"""Shared test authentication helpers for authenticated Phase 2 TestClient suites."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

from fastapi.testclient import TestClient

from app.main import auth_service
from app.persistence.database import init_db
from app.security.rate_limit import api_mutation_rate_limiter, login_rate_limiter

_LOGIN_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
_META_CSRF_RE = re.compile(r'<meta\s+name="csrf-token"\s+content="([^"]+)"')


def reset_test_rate_limiters() -> None:
    """Clear per-process rate-limit buckets between isolated test runs."""
    login_rate_limiter.reset()
    api_mutation_rate_limiter.reset()


def authenticate_session_client(
    client: TestClient,
    *,
    db_path: Optional[Path] = None,
    username: str = "test_admin",
    display_name: str = "Test Administrator",
    password: str = "Str0ng!PilotPass#99",
    role: str = "admin",
) -> Tuple[TestClient, str, Dict]:
    """Provision user in isolated DB if needed, perform real /login + CSRF exchange, and attach header."""
    reset_test_rate_limiters()
    init_db(db_path)

    user = auth_service.get_user_by_username(username, db_path=db_path)
    if not user:
        user = auth_service.create_user(
            username=username,
            display_name=display_name,
            password=password,
            role=role,
            db_path=db_path,
        )

    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    client.headers.pop("Authorization", None)

    login_page = client.get("/login")
    assert login_page.status_code == 200, f"GET /login failed: {login_page.status_code}"
    match = _LOGIN_CSRF_RE.search(login_page.text)
    assert match, "Login CSRF token missing from /login page"
    login_csrf = html.unescape(match.group(1))

    resp = client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": login_csrf,
            "next": "/dashboard",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200, f"POST /login failed: {resp.status_code}"
    meta_match = _META_CSRF_RE.search(resp.text)
    assert meta_match, "Authenticated CSRF meta token missing from dashboard response"
    session_csrf = html.unescape(meta_match.group(1))
    client.headers["X-CSRF-Token"] = session_csrf
    return client, session_csrf, user
