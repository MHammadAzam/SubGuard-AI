"""Phase 2 (ISS-02): Comprehensive Application Security Test Suite.

Verifies:
- Fail-closed authentication on all API & browser routes
- Role-based access control (viewer, analyst, reviewer, admin)
- Python 3.12 hashlib.scrypt parameterized hashing & password policy
- HttpOnly/SameSite/Secure session cookies & synchronizer-token CSRF protection
- Opaque revocable Bearer API tokens stored only as SHA-256 hashes
- Authoritative server-derived reviewer identity & anti-spoofing
- Document download authorization & symlink-free canonical path confinement
- Login rate limiting & idle/absolute session timeouts
- Production TLS, trusted-proxy, CORS, security headers, and fail-closed startup config
- Safe additive SQLite migration preserving existing records
- Structured security logging free of raw credentials or tokens
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pymupdf
from fastapi.testclient import TestClient

from app.main import app, auth_service, repository
from app.persistence.database import get_connection, init_db
from app.security.bootstrap import bootstrap_admin_from_env
from app.security.config import validate_production_config
from app.security.passwords import hash_password, verify_password
from tests.fixtures.auth_helpers import authenticate_session_client, reset_test_rate_limiters


def _pdf_bytes(text: str = "ACORD 25 Certificate $1,000,000") -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 72), text, fontsize=11)
    raw = doc.tobytes()
    doc.close()
    return raw


class TestPhase2Security(unittest.TestCase):
    """Verify Phase 2 security controls cleanly without test-only bypasses."""

    def setUp(self):
        reset_test_rate_limiters()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / f"sec_{uuid4().hex[:8]}.db"
        self.upload_dir = Path(self.temp_dir.name) / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._orig_repo_db = repository.db_path
        self._orig_env = {
            k: os.environ.get(k)
            for k in (
                "SUBGUARD_ENV",
                "SUBGUARD_SESSION_SECRET",
                "SUBGUARD_CORS_ORIGINS",
                "SUBGUARD_COOKIE_SECURE",
                "SUBGUARD_ENFORCE_HTTPS",
                "SUBGUARD_TRUSTED_PROXIES",
                "SUBGUARD_ENABLE_DEMO_ENDPOINT",
                "SUBGUARD_LOGIN_LIMIT_PER_USER",
                "SUBGUARD_LOGIN_LIMIT_PER_IP",
                "SUBGUARD_SESSION_IDLE_SECONDS",
                "SUBGUARD_UPLOAD_DIR",
                "SUBGUARD_BOOTSTRAP_ADMIN_USERNAME",
                "SUBGUARD_BOOTSTRAP_ADMIN_PASSWORD",
            )
        }
        os.environ["SUBGUARD_UPLOAD_DIR"] = str(self.upload_dir)
        init_db(self.db_path)
        repository.db_path = self.db_path
        self.client = TestClient(app)

    def tearDown(self):
        repository.db_path = self._orig_repo_db
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_test_rate_limiters()
        self.temp_dir.cleanup()

    def _seed_sample_submission(self, sub_id: str = "sub_sec_001") -> tuple[str, str]:
        doc_id = f"doc_{uuid4().hex[:8]}"
        sub_dir = self.upload_dir / sub_id
        sub_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = sub_dir / f"{doc_id}_coi.pdf"
        pdf_bytes = _pdf_bytes()
        pdf_path.write_bytes(pdf_bytes)

        repository.create_submission(
            submission_id=sub_id,
            name="Security Seed Submission",
            overall_status="requires_human_review",
            processing_status="completed",
            provider="mock",
            evaluation_date="2025-06-15",
            result_data={
                "review": {
                    "subcontractor_id": "sub_apex",
                    "evaluation_date": "2025-06-15",
                    "overall_status": "requires_human_review",
                    "requires_human_review": True,
                    "findings": [
                        {
                            "finding_id": "f_hr_1",
                            "requirement_id": "REQ_BLANKET",
                            "rule_type": "endorsement_presence",
                            "status": "requires_human_review",
                            "severity": "major",
                            "reason_code": "BLANKET_CHECK",
                            "requires_human_review": True,
                            "explanation": "Confirm blanket wording.",
                        }
                    ],
                },
                "document_set": {"coverages": [], "endorsements": []},
            },
            subcontractor_name="Apex Electrical Contractors LLC",
        )
        repository.add_document(
            document_id=doc_id,
            submission_id=sub_id,
            original_filename="coi.pdf",
            document_type="coi",
            storage_path=str(pdf_path),
            file_size_bytes=len(pdf_bytes),
            page_count=1,
        )
        return sub_id, doc_id

    # ------------------------------------------------------------------
    # 1. Fail-Closed Authentication Across All Protected Routes
    # ------------------------------------------------------------------

    def test_01_all_protected_routes_fail_closed_when_unauthenticated(self):
        sub_id, doc_id = self._seed_sample_submission()

        # Public health endpoint exposes only minimal non-sensitive status
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok", "service": "compliance-evaluator"})

        api_cases = [
            ("GET", "/api/v1/auth/me"),
            ("GET", "/api/v1/submissions"),
            ("GET", f"/api/v1/submissions/{sub_id}"),
            ("GET", "/api/v1/dashboard/stats"),
            ("GET", f"/api/v1/documents/{doc_id}/download"),
            ("GET", f"/api/v1/submissions/{sub_id}/documents/{doc_id}/download"),
            ("POST", "/api/v1/compliance/evaluate"),
            ("POST", "/api/v1/compliance/review-documents"),
            ("POST", f"/api/v1/submissions/{sub_id}/review"),
            ("POST", "/api/v1/compliance/demo-run/SCEN-01"),
            ("GET", "/api/v1/admin/users"),
            ("POST", "/api/v1/admin/users"),
            ("PATCH", "/api/v1/admin/users/usr_123"),
            ("GET", "/api/v1/admin/tokens"),
            ("POST", "/api/v1/admin/tokens"),
            ("POST", "/api/v1/admin/tokens/tok_123/revoke"),
        ]
        for method, path in api_cases:
            res = self.client.request(method, path)
            self.assertEqual(res.status_code, 401, f"{method} {path} returned {res.status_code}")

        ui_get_routes = [
            "/",
            "/dashboard",
            "/submissions",
            "/submissions/new",
            f"/submissions/{sub_id}",
            f"/submissions/{sub_id}/evidence",
            f"/submissions/{sub_id}/review",
            "/admin/users",
        ]
        for path in ui_get_routes:
            res = self.client.get(path, follow_redirects=False)
            self.assertEqual(res.status_code, 302, f"GET {path} returned {res.status_code}")
            self.assertTrue(
                res.headers["location"].startswith("/login"),
                f"GET {path} redirected to {res.headers['location']}",
            )

        # Logout also rejects unauthenticated browser redirect
        res_logout = self.client.post("/logout", follow_redirects=False)
        self.assertEqual(res_logout.status_code, 302)
        self.assertTrue(res_logout.headers["location"].startswith("/login"))

    def test_02_no_default_credentials_exist(self):
        # Fresh DB has zero default accounts; common default logins fail 401
        login_page = self.client.get("/login")
        self.assertEqual(login_page.status_code, 200)
        import re
        csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text).group(1)

        for user, pw in [("admin", "admin"), ("admin", "password"), ("reviewer", "reviewer")]:
            res = self.client.post(
                "/login",
                data={"username": user, "password": pw, "csrf_token": csrf},
                follow_redirects=False,
            )
            self.assertEqual(res.status_code, 401)
            csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', res.text).group(1)

    # ------------------------------------------------------------------
    # 2. Password Hashing (scrypt), Policy, Sessions & Bearer Token Hygiene
    # ------------------------------------------------------------------

    def test_03_scrypt_hashing_random_salt_and_complexity_policy(self):
        h1 = hash_password("S3cure!SubGuard#2026")
        h2 = hash_password("S3cure!SubGuard#2026")
        self.assertTrue(h1.startswith("scrypt$131072$8$1$"))
        self.assertNotEqual(h1, h2)
        self.assertTrue(verify_password("S3cure!SubGuard#2026", h1))
        self.assertFalse(verify_password("Wrong!SubGuard#2026", h1))

        with self.assertRaises(ValueError):
            hash_password("short1!")
        with self.assertRaises(ValueError):
            hash_password("password1234")
        with self.assertRaises(ValueError):
            hash_password("alllowercaselettersonly")

    def test_04_raw_tokens_never_stored_in_db_and_no_localstorage_in_ui(self):
        client, csrf_tok, user = authenticate_session_client(
            self.client, db_path=self.db_path, role="admin"
        )
        raw_session_cookie = client.cookies.get("subguard_session")
        self.assertIsNotNone(raw_session_cookie)

        # Create an API Bearer token via admin API
        tok_res = client.post(
            "/api/v1/admin/tokens",
            json={"user_id": user["id"], "label": "CI Runner", "ttl_days": 7},
        )
        self.assertEqual(tok_res.status_code, 201)
        raw_api_token = tok_res.json()["token"]
        self.assertTrue(raw_api_token.startswith("sg_"))

        # Verify DB stores ONLY SHA-256 hashes, NEVER plaintext tokens or passwords
        conn = get_connection(self.db_path)
        try:
            rows = conn.execute("SELECT token_hash, token_kind FROM sessions").fetchall()
            hashes = {r["token_hash"] for r in rows}
            self.assertIn(hashlib.sha256(raw_session_cookie.encode()).hexdigest(), hashes)
            self.assertIn(hashlib.sha256(raw_api_token.encode()).hexdigest(), hashes)
            self.assertNotIn(raw_session_cookie, hashes)
            self.assertNotIn(raw_api_token, hashes)
        finally:
            conn.close()

        # Rendered pages must never use localStorage for auth tokens
        for path in ("/dashboard", "/submissions/new", "/admin/users"):
            html_res = client.get(path)
            self.assertEqual(html_res.status_code, 200)
            self.assertNotIn("localStorage", html_res.text)

    def test_05_session_cookie_flags_and_safe_open_redirect_prevention(self):
        auth_service.create_user(
            username="alice",
            display_name="Alice Reviewer",
            password="Alice!Strong#Password9",
            role="reviewer",
            db_path=self.db_path,
        )
        login_page = self.client.get("/login")
        import re
        csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text).group(1)

        res = self.client.post(
            "/login",
            data={
                "username": "alice",
                "password": "Alice!Strong#Password9",
                "csrf_token": csrf,
                "next": "//evil.example/phish",
            },
            follow_redirects=False,
        )
        self.assertEqual(res.status_code, 303)
        self.assertEqual(res.headers["location"], "/dashboard")
        set_cookie = res.headers.get("set-cookie", "").lower()
        self.assertIn("subguard_session=", set_cookie)
        self.assertIn("httponly", set_cookie)
        self.assertIn("samesite=lax", set_cookie)

    # ------------------------------------------------------------------
    # 3. RBAC Matrix, CSRF Protection & Token Kind Separation
    # ------------------------------------------------------------------

    def test_06_rbac_enforces_least_privilege_across_all_roles(self):
        sub_id, doc_id = self._seed_sample_submission()

        roles_and_clients = {}
        for role in ("viewer", "analyst", "reviewer", "admin"):
            c = TestClient(app)
            authenticate_session_client(
                c,
                db_path=self.db_path,
                username=f"user_{role}",
                display_name=f"User {role.title()}",
                role=role,
            )
            roles_and_clients[role] = c

        # Viewer: reads allowed, mutations & review UI denied 403
        v = roles_and_clients["viewer"]
        self.assertEqual(v.get("/api/v1/submissions").status_code, 200)
        self.assertEqual(v.get(f"/api/v1/documents/{doc_id}/download").status_code, 200)
        self.assertEqual(v.get("/submissions/new").status_code, 403)
        self.assertEqual(v.get(f"/submissions/{sub_id}/review").status_code, 403)
        self.assertEqual(v.get("/admin/users").status_code, 403)
        self.assertEqual(v.post("/api/v1/compliance/evaluate", json={}).status_code, 403)
        self.assertEqual(
            v.post(f"/api/v1/submissions/{sub_id}/review", json={"review_status": "rejected", "reviewer_notes": "x"}).status_code,
            403,
        )
        self.assertEqual(v.get("/api/v1/admin/users").status_code, 403)

        # Analyst: upload UI allowed, human review & admin denied 403
        a = roles_and_clients["analyst"]
        self.assertEqual(a.get("/submissions/new").status_code, 200)
        self.assertEqual(a.get(f"/submissions/{sub_id}/review").status_code, 403)
        self.assertEqual(
            a.post(f"/api/v1/submissions/{sub_id}/review", json={"review_status": "rejected", "reviewer_notes": "x"}).status_code,
            403,
        )
        self.assertEqual(a.get("/api/v1/admin/users").status_code, 403)

        # Reviewer: review UI + API allowed, admin denied 403
        r = roles_and_clients["reviewer"]
        self.assertEqual(r.get(f"/submissions/{sub_id}/review").status_code, 200)
        self.assertEqual(
            r.post(
                f"/api/v1/submissions/{sub_id}/review",
                json={"review_status": "accepted_exception", "reviewer_notes": "Approved by reviewer"},
            ).status_code,
            200,
        )
        self.assertEqual(r.get("/api/v1/admin/users").status_code, 403)

        # Admin: admin endpoints allowed
        adm = roles_and_clients["admin"]
        self.assertEqual(adm.get("/admin/users").status_code, 200)
        self.assertEqual(adm.get("/api/v1/admin/users").status_code, 200)

    def test_07_csrf_protection_enforced_for_cookies_and_bearer_token_lifecycle(self):
        sub_id, _ = self._seed_sample_submission()
        client, csrf_tok, user = authenticate_session_client(
            self.client, db_path=self.db_path, username="rev_csrf", display_name="Real Reviewer", role="reviewer"
        )

        # Cookie auth without CSRF header fails 403
        no_csrf = TestClient(app)
        no_csrf.cookies.update(client.cookies)
        res_missing = no_csrf.post(
            f"/api/v1/submissions/{sub_id}/review",
            json={"review_status": "accepted_exception", "reviewer_notes": "No CSRF"},
        )
        self.assertEqual(res_missing.status_code, 403)

        # Cookie auth with forged CSRF header fails 403
        res_bad = no_csrf.post(
            f"/api/v1/submissions/{sub_id}/review",
            headers={"X-CSRF-Token": "forged-token"},
            json={"review_status": "accepted_exception", "reviewer_notes": "Bad CSRF"},
        )
        self.assertEqual(res_bad.status_code, 403)

        # Logout CSRF blocked without valid token
        res_logout_bad = no_csrf.post("/logout", follow_redirects=False)
        self.assertEqual(res_logout_bad.status_code, 403)
        self.assertEqual(no_csrf.get("/api/v1/auth/me").status_code, 200)

        # Session cookie rejected if sent as Bearer token (strict token_kind boundary)
        raw_cookie = client.cookies.get("subguard_session")
        bearer_using_cookie = TestClient(app)
        res_mix = bearer_using_cookie.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {raw_cookie}"}
        )
        self.assertEqual(res_mix.status_code, 401)

        # Valid logout revokes session server-side
        res_logout_ok = client.post("/logout", data={"csrf_token": csrf_tok}, follow_redirects=False)
        self.assertEqual(res_logout_ok.status_code, 303)
        self.assertEqual(no_csrf.get("/api/v1/auth/me").status_code, 401)

        # Opaque revocable Bearer API token works without CSRF and stops working after revoke
        tok = auth_service.create_api_token(user_id=user["id"], label="API Client", db_path=self.db_path)
        api_client = TestClient(app)
        api_client.headers["Authorization"] = f"Bearer {tok['token']}"
        self.assertEqual(api_client.get("/api/v1/auth/me").status_code, 200)
        res_rev = api_client.post(
            f"/api/v1/submissions/{sub_id}/review",
            json={"review_status": "accepted_exception", "reviewer_notes": "Via Bearer API token"},
        )
        self.assertEqual(res_rev.status_code, 200)

        self.assertTrue(auth_service.revoke_session_by_id(tok["id"], db_path=self.db_path))
        self.assertEqual(api_client.get("/api/v1/auth/me").status_code, 401)

    # ------------------------------------------------------------------
    # 4. Reviewer Identity Integrity & Account Deactivation
    # ------------------------------------------------------------------

    def test_08_reviewer_identity_derived_server_side_prevents_spoofing(self):
        sub_id, _ = self._seed_sample_submission()
        client, _, reviewer_user = authenticate_session_client(
            self.client,
            db_path=self.db_path,
            username="jane_rev",
            display_name="Jane Authenticated Reviewer",
            role="reviewer",
        )

        # Attempt to spoof reviewer_name as Risk Director
        res = client.post(
            f"/api/v1/submissions/{sub_id}/review",
            json={
                "review_status": "accepted_exception",
                "reviewer_name": "Spoofed Risk Director",
                "reviewer_notes": "Confirmed endorsement wording.",
            },
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["review"]["reviewer_name"], "Jane Authenticated Reviewer")
        self.assertEqual(body["review"]["reviewer_user_id"], reviewer_user["id"])

        persisted = repository.get_submission_by_id(sub_id)
        self.assertEqual(persisted["reviews"][0]["reviewer_name"], "Jane Authenticated Reviewer")
        self.assertEqual(persisted["reviews"][0]["reviewer_user_id"], reviewer_user["id"])

        # Deactivating user immediately invalidates their active session
        auth_service.update_user(reviewer_user["id"], is_active=False, db_path=self.db_path)
        self.assertEqual(client.get("/api/v1/auth/me").status_code, 401)

    # ------------------------------------------------------------------
    # 5. Document Download Path Confinement & Symlink Protection
    # ------------------------------------------------------------------

    def test_09_document_download_path_confinement_and_headers(self):
        sub_id, doc_id = self._seed_sample_submission()
        client, _, _ = authenticate_session_client(self.client, db_path=self.db_path, role="viewer")

        # Valid confined download
        ok_res = client.get(f"/api/v1/documents/{doc_id}/download")
        self.assertEqual(ok_res.status_code, 200)
        self.assertEqual(ok_res.headers["x-content-type-options"], "nosniff")
        self.assertIn("attachment;", ok_res.headers["content-disposition"].lower())

        # DB row altered to escape upload root -> 403 Forbidden
        outside_file = Path(self.temp_dir.name) / "secret_outside.pdf"
        outside_file.write_bytes(b"%PDF-1.4\nsecret")
        escape_doc_id = f"doc_esc_{uuid4().hex[:6]}"
        repository.add_document(
            document_id=escape_doc_id,
            submission_id=sub_id,
            original_filename='../../evil"\r\nX-Test: 1.pdf',
            document_type="coi",
            storage_path=str(outside_file),
            file_size_bytes=outside_file.stat().st_size,
        )
        esc_res = client.get(f"/api/v1/documents/{escape_doc_id}/download")
        self.assertEqual(esc_res.status_code, 403)

        # Path traversal payload pointing outside upload root -> 403 Forbidden
        trav_doc_id = f"doc_trav_{uuid4().hex[:6]}"
        repository.add_document(
            document_id=trav_doc_id,
            submission_id=sub_id,
            original_filename="coi.pdf",
            document_type="coi",
            storage_path=str(self.upload_dir / sub_id / ".." / ".." / "secret_outside.pdf"),
            file_size_bytes=10,
        )
        self.assertEqual(client.get(f"/api/v1/documents/{trav_doc_id}/download").status_code, 403)

        # Symlink confinement check (skip gracefully if OS denies unprivileged symlink creation)
        sym_path = self.upload_dir / sub_id / "link_outside.pdf"
        try:
            os.symlink(outside_file, sym_path)
        except OSError:
            pass
        else:
            sym_doc_id = f"doc_sym_{uuid4().hex[:6]}"
            repository.add_document(
                document_id=sym_doc_id,
                submission_id=sub_id,
                original_filename="coi.pdf",
                document_type="coi",
                storage_path=str(sym_path),
                file_size_bytes=10,
            )
            self.assertEqual(client.get(f"/api/v1/documents/{sym_doc_id}/download").status_code, 403)

    # ------------------------------------------------------------------
    # 6. Rate Limiting & Session Timeouts
    # ------------------------------------------------------------------

    def test_10_login_rate_limiting_and_session_idle_timeout(self):
        os.environ["SUBGUARD_LOGIN_LIMIT_PER_USER"] = "3"
        os.environ["SUBGUARD_LOGIN_LIMIT_PER_IP"] = "10"

        login_page = self.client.get("/login")
        import re
        csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text).group(1)

        for _ in range(3):
            res = self.client.post(
                "/login",
                data={"username": "victim", "password": "wrong-password-xx", "csrf_token": csrf},
                follow_redirects=False,
            )
            self.assertEqual(res.status_code, 401)
            csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', res.text).group(1)

        limited = self.client.post(
            "/login",
            data={"username": "victim", "password": "wrong-password-xx", "csrf_token": csrf},
            follow_redirects=False,
        )
        self.assertEqual(limited.status_code, 429)
        self.assertIn("retry-after", limited.headers)

        # Verify session idle expiry revokes aged sessions
        reset_test_rate_limiters()
        os.environ["SUBGUARD_SESSION_IDLE_SECONDS"] = "60"
        client, _, user = authenticate_session_client(self.client, db_path=self.db_path, role="viewer")
        stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        conn = get_connection(self.db_path)
        try:
            with conn:
                conn.execute("UPDATE sessions SET last_seen_at = ? WHERE user_id = ?", (stale_ts, user["id"]))
        finally:
            conn.close()

        self.assertEqual(client.get("/api/v1/auth/me").status_code, 401)

    # ------------------------------------------------------------------
    # 7. Production Config Validation, TLS, Trusted Proxy & Demo Guardrail
    # ------------------------------------------------------------------

    def test_11_production_mode_fails_closed_and_enforces_tls(self):
        os.environ["SUBGUARD_ENV"] = "production"
        os.environ.pop("SUBGUARD_SESSION_SECRET", None)
        with self.assertRaises(RuntimeError):
            validate_production_config(self.db_path)

        os.environ["SUBGUARD_SESSION_SECRET"] = "short-weak-secret"
        with self.assertRaises(RuntimeError):
            validate_production_config(self.db_path)

        os.environ["SUBGUARD_SESSION_SECRET"] = "P" * 40
        os.environ["SUBGUARD_CORS_ORIGINS"] = "*"
        with self.assertRaises(RuntimeError):
            validate_production_config(self.db_path)

        os.environ.pop("SUBGUARD_CORS_ORIGINS", None)
        os.environ["SUBGUARD_COOKIE_SECURE"] = "false"
        with self.assertRaises(RuntimeError):
            validate_production_config(self.db_path)

        os.environ["SUBGUARD_COOKIE_SECURE"] = "true"
        # No active admin in DB yet -> fail closed
        with self.assertRaises(RuntimeError):
            validate_production_config(self.db_path)

        # Bootstrap explicit admin via env vars and confirm production validation passes
        os.environ["SUBGUARD_BOOTSTRAP_ADMIN_USERNAME"] = "prod_admin"
        os.environ["SUBGUARD_BOOTSTRAP_ADMIN_PASSWORD"] = "Pr0d!Admin#Secret2026"
        created_admin = bootstrap_admin_from_env(self.db_path)
        self.assertIsNotNone(created_admin)
        validate_production_config(self.db_path)

        # Plaintext HTTP request (untrusted peer / spoofed X-Forwarded-Proto) rejected 403 in prod
        plain_res = self.client.get("/health", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(plain_res.status_code, 403)

        # Trusted TLS proxy peer resolves effective scheme to https (while untrusted peer does not)
        os.environ["SUBGUARD_TRUSTED_PROXIES"] = "127.0.0.1"
        from app.security.config import resolve_effective_scheme
        self.assertEqual(resolve_effective_scheme("203.0.113.5", "http", "https"), "http")
        self.assertEqual(resolve_effective_scheme("127.0.0.1", "http", "https"), "https")

        # Demo-run endpoint is disabled in production mode
        os.environ["SUBGUARD_TRUSTED_PROXIES"] = ""
        os.environ["SUBGUARD_ENFORCE_HTTPS"] = "true"
        tok = auth_service.create_api_token(
            user_id=created_admin["id"], label="Prod Demo Check", db_path=self.db_path
        )
        # Switch temporarily to HTTPS url to check 403 on demo-run while HTTPS is enabled
        https_client = TestClient(app, base_url="https://testserver")
        demo_res = https_client.post(
            "/api/v1/compliance/demo-run/SCEN-01",
            headers={"Authorization": f"Bearer {tok['token']}"},
        )
        self.assertEqual(demo_res.status_code, 403)
        self.assertIn("Strict-Transport-Security".lower(), {k.lower() for k in demo_res.headers.keys()})
        self.assertIn("X-Frame-Options".lower(), {k.lower() for k in demo_res.headers.keys()})

    # ------------------------------------------------------------------
    # 8. Safe Additive SQLite Migration & Clean Security Logs
    # ------------------------------------------------------------------

    def test_12_additive_migration_preserves_legacy_records_and_clean_logs(self):
        legacy_db = Path(self.temp_dir.name) / "legacy_pre_phase2.db"
        conn = sqlite3.connect(str(legacy_db))
        conn.executescript(
            """
            CREATE TABLE submissions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                overall_status TEXT NOT NULL,
                processing_status TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'mock',
                evaluation_date TEXT NOT NULL,
                subcontractor_name TEXT,
                result_json TEXT NOT NULL
            );
            CREATE TABLE documents (
                id TEXT PRIMARY KEY,
                submission_id TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                document_type TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                file_size_bytes INTEGER NOT NULL,
                page_count INTEGER NOT NULL DEFAULT 0,
                upload_timestamp TEXT NOT NULL,
                processing_status TEXT NOT NULL,
                error_message TEXT
            );
            CREATE TABLE review_records (
                id TEXT PRIMARY KEY,
                submission_id TEXT NOT NULL,
                finding_id TEXT,
                reason_code TEXT,
                review_status TEXT NOT NULL,
                reviewer_notes TEXT NOT NULL,
                reviewer_name TEXT NOT NULL DEFAULT 'Compliance Reviewer',
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            INSERT INTO submissions VALUES (
                'sub_legacy_1', 'Legacy Submission', '2025-06-01T00:00:00Z',
                'compliant', 'completed', 'mock', '2025-06-15', 'Legacy Sub LLC', '{}'
            );
            """
        )
        conn.commit()
        conn.close()

        init_db(legacy_db)
        migrated = get_connection(legacy_db)
        try:
            tables = {
                r["name"]
                for r in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            self.assertTrue({"submissions", "documents", "review_records", "users", "sessions"}.issubset(tables))
            legacy_sub = migrated.execute("SELECT name FROM submissions WHERE id='sub_legacy_1'").fetchone()
            self.assertEqual(legacy_sub["name"], "Legacy Submission")
            rev_cols = {
                r["name"]
                for r in migrated.execute("PRAGMA table_info(review_records)").fetchall()
            }
            self.assertIn("reviewer_user_id", rev_cols)
        finally:
            migrated.close()

        # Verify security logs emit login/token events cleanly without raw passwords or tokens
        secret_pw = "AuditLog!Secret#Pass99"
        with self.assertLogs("subguard.security", level=logging.INFO) as captured:
            _, _, usr = authenticate_session_client(
                self.client,
                db_path=self.db_path,
                username="log_tester",
                display_name="Log Tester",
                password=secret_pw,
                role="admin",
            )
            tok = self.client.post(
                "/api/v1/admin/tokens",
                json={"user_id": usr["id"], "label": "Log Check Token"},
            ).json()

        joined_logs = "\n".join(captured.output)
        self.assertIn("login_success", joined_logs)
        self.assertIn("api_token_created", joined_logs)
        self.assertNotIn(secret_pw, joined_logs)
        self.assertNotIn(tok["token"], joined_logs)

    def test_13_admin_provisioning_cli_self_lockout_protection_and_cors(self):
        from app.security.bootstrap import main as bootstrap_main

        os.environ["TEST_CLI_ADMIN_PW"] = "Cli!Bootstrap#Secret99"
        try:
            rc = bootstrap_main(
                [
                    "create-admin",
                    "--username",
                    "cli_admin",
                    "--display-name",
                    "CLI Administrator",
                    "--password-env",
                    "TEST_CLI_ADMIN_PW",
                    "--db-path",
                    str(self.db_path),
                ]
            )
            self.assertEqual(rc, 0)
        finally:
            os.environ.pop("TEST_CLI_ADMIN_PW", None)

        client, _, cli_user = authenticate_session_client(
            self.client,
            db_path=self.db_path,
            username="cli_admin",
            display_name="CLI Administrator",
            password="Cli!Bootstrap#Secret99",
            role="admin",
        )

        # Admin cannot demote or disable self -> 400
        self_demote = client.patch(
            f"/api/v1/admin/users/{cli_user['id']}",
            json={"role": "viewer"},
        )
        self.assertEqual(self_demote.status_code, 400)

        # Create a second admin and verify admin 1 still cannot remove last admin after admin 2 disables self?
        second = client.post(
            "/api/v1/admin/users",
            json={
                "username": "second_admin",
                "display_name": "Second Admin",
                "password": "Sec0nd!Admin#Secret99",
                "role": "admin",
            },
        )
        self.assertEqual(second.status_code, 201)
        sec_id = second.json()["id"]
        # Disable second admin succeeds since cli_admin remains active
        self.assertEqual(
            client.patch(f"/api/v1/admin/users/{sec_id}", json={"is_active": False}).status_code,
            200,
        )

        # With default empty SUBGUARD_CORS_ORIGINS, untrusted Origin receives zero CORS allow headers
        cors_res = self.client.get("/health", headers={"Origin": "https://attacker.example"})
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in cors_res.headers.keys()})

    # ------------------------------------------------------------------
    # 9. Rehash-on-Success, Bounded Concurrency, Document Association & Route Audit
    # ------------------------------------------------------------------

    def test_14_legacy_scrypt_rehash_on_success_and_bounded_concurrency(self):
        # 1. Create a user directly with a legacy N=32768 hash (pre-OWASP upgrade)
        legacy_pw = "Legacy!Admin#Pass2025"
        legacy_hash = hash_password(legacy_pw, n=32768, r=8, p=1)
        self.assertTrue(legacy_hash.startswith("scrypt$32768$8$1$"))

        user_id = f"usr_leg_{uuid4().hex[:8]}"
        conn = get_connection(self.db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO users (id, username, display_name, password_hash, role, is_active, created_at, updated_at)
                    VALUES (?, 'legacy_admin', 'Legacy Admin', ?, 'admin', 1, '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')
                    """,
                    (user_id, legacy_hash),
                )
        finally:
            conn.close()

        # Authenticate cleanly: legacy hash succeeds AND transparently upgrades to N=131072
        auth_result = auth_service.authenticate_credentials("legacy_admin", legacy_pw, db_path=self.db_path)
        self.assertIsNotNone(auth_result)
        conn = get_connection(self.db_path)
        try:
            new_row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
            self.assertTrue(new_row["password_hash"].startswith("scrypt$131072$8$1$"))
            self.assertTrue(verify_password(legacy_pw, new_row["password_hash"]))
        finally:
            conn.close()

        # 2. Rate limiting runs BEFORE expensive scrypt hashing (0 scrypt calls when blocked)
        os.environ["SUBGUARD_LOGIN_LIMIT_PER_USER"] = "1"
        os.environ["SUBGUARD_LOGIN_LIMIT_PER_IP"] = "5"
        login_page = self.client.get("/login")
        import re
        csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text).group(1)

        r1 = self.client.post(
            "/login",
            data={"username": "burst_user", "password": "wrong-password-123", "csrf_token": csrf},
            follow_redirects=False,
        )
        self.assertEqual(r1.status_code, 401)
        csrf2 = re.search(r'name="csrf_token"\s+value="([^"]+)"', r1.text).group(1)

        with patch("app.main.auth_service.authenticate_credentials") as mock_auth:
            r2 = self.client.post(
                "/login",
                data={"username": "burst_user", "password": "wrong-password-123", "csrf_token": csrf2},
                follow_redirects=False,
            )
            self.assertEqual(r2.status_code, 429)
            self.assertIn("retry-after", r2.headers)
            mock_auth.assert_not_called()

        # 3. Bounded concurrency gate returns 429 Retry-After when all scrypt slots are saturated
        reset_test_rate_limiters()
        from app.security.passwords import _scrypt_gate
        os.environ["SUBGUARD_MAX_CONCURRENT_SCRYPT"] = "1"
        os.environ["SUBGUARD_SCRYPT_ACQUIRE_TIMEOUT"] = "0.05"
        try:
            lp = self.client.get("/login")
            lcsrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', lp.text).group(1)
            with _scrypt_gate.acquire():
                saturated = self.client.post(
                    "/login",
                    data={"username": "legacy_admin", "password": legacy_pw, "csrf_token": lcsrf},
                    follow_redirects=False,
                )
                self.assertEqual(saturated.status_code, 429)
                self.assertEqual(saturated.headers.get("retry-after"), "2")
        finally:
            os.environ.pop("SUBGUARD_MAX_CONCURRENT_SCRYPT", None)
            os.environ.pop("SUBGUARD_SCRYPT_ACQUIRE_TIMEOUT", None)

    def test_15_document_submission_association_isolation(self):
        sub_a, doc_a = self._seed_sample_submission("sub_alpha_001")
        sub_b, doc_b = self._seed_sample_submission("sub_beta_002")
        client, _, _ = authenticate_session_client(self.client, db_path=self.db_path, role="viewer")

        # Valid submission-scoped download succeeds
        ok = client.get(f"/api/v1/submissions/{sub_a}/documents/{doc_a}/download")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.headers["x-content-type-options"], "nosniff")

        # Unknown document ID -> 404
        self.assertEqual(
            client.get(f"/api/v1/submissions/{sub_a}/documents/doc_missing_999/download").status_code,
            404,
        )

        # Cross-submission download attempt: doc_b requested under sub_a -> 404 denied
        cross_path = client.get(f"/api/v1/submissions/{sub_a}/documents/{doc_b}/download")
        self.assertEqual(cross_path.status_code, 404)

        cross_query = client.get(f"/api/v1/documents/{doc_b}/download?submission_id={sub_a}")
        self.assertEqual(cross_query.status_code, 404)

        # DB row in sub_a with storage_path inside upload_dir pointing to sub_b's folder -> 403
        sub_b_file = self.upload_dir / sub_b / f"{doc_b}_coi.pdf"
        tampered_doc_id = f"doc_cross_path_{uuid4().hex[:6]}"
        repository.add_document(
            document_id=tampered_doc_id,
            submission_id=sub_a,
            original_filename="coi.pdf",
            document_type="coi",
            storage_path=str(sub_b_file),
            file_size_bytes=sub_b_file.stat().st_size,
        )
        self.assertEqual(
            client.get(f"/api/v1/documents/{tampered_doc_id}/download").status_code,
            403,
        )

    def test_16_complete_route_inventory_bootstrap_idempotency_and_role_revocation(self):
        from fastapi.routing import APIRoute

        # 1. Inspect every registered APIRoute on app and verify zero undocumented public routes exist
        allowed_public_routes = {
            "/health",
            "/login",
            "/docs",
            "/docs/oauth2-redirect",
            "/redoc",
            "/openapi.json",
        }
        discovered = set()
        for route in app.routes:
            if isinstance(route, APIRoute):
                discovered.add(route.path)
                if route.path not in allowed_public_routes:
                    self.assertTrue(
                        len(route.dependant.dependencies) > 0,
                        f"Route {route.methods} {route.path} lacks auth/RBAC dependencies!",
                    )
        self.assertIn("/api/v1/submissions/{submission_id}/documents/{document_id}/download", discovered)

        # 2. Verify production mode blocks /docs, /redoc, /openapi.json with 404
        os.environ["SUBGUARD_ENV"] = "production"
        os.environ["SUBGUARD_SESSION_SECRET"] = "P" * 40
        os.environ["SUBGUARD_BOOTSTRAP_ADMIN_USERNAME"] = "boot_admin"
        os.environ["SUBGUARD_BOOTSTRAP_ADMIN_PASSWORD"] = "B00t!Admin#Secret99"
        first_admin = bootstrap_admin_from_env(self.db_path)
        self.assertIsNotNone(first_admin)
        # Re-running bootstrap when admin already exists is idempotent (no duplicate/error/reset)
        second_admin = bootstrap_admin_from_env(self.db_path)
        self.assertEqual(second_admin["id"], first_admin["id"])
        self.assertEqual(len(auth_service.list_users(db_path=self.db_path)), 1)

        https_client = TestClient(app, base_url="https://testserver")
        for doc_path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(https_client.get(doc_path).status_code, 404)

        os.environ["SUBGUARD_ENV"] = "development"
        os.environ.pop("SUBGUARD_ENFORCE_HTTPS", None)

        # 3. Role-change immediately revokes active reviewer sessions & API tokens
        rev_client = TestClient(app)
        _, _, rev_user = authenticate_session_client(
            rev_client,
            db_path=self.db_path,
            username="temp_rev",
            display_name="Temp Reviewer",
            role="reviewer",
        )
        tok = auth_service.create_api_token(
            user_id=rev_user["id"], label="Role Change Token", db_path=self.db_path
        )
        self.assertEqual(rev_client.get("/api/v1/auth/me").status_code, 200)

        auth_service.update_user(rev_user["id"], role="viewer", db_path=self.db_path)
        self.assertEqual(rev_client.get("/api/v1/auth/me").status_code, 401)
        bearer_client = TestClient(app)
        self.assertEqual(
            bearer_client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {tok['token']}"}
            ).status_code,
            401,
        )


if __name__ == "__main__":
    unittest.main()
