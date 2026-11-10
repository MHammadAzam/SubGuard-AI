"""Phase 3B: Controlled Reliability & Operational Readiness Test Suite.

Covers Work Packages 1-5 using strictly isolated temporary SQLite databases
and upload directories. Zero live/paid AI API calls, zero modifications to
frozen evaluation cohort, OCR engine, screenshots, or running port-8010 demo.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pymupdf
from fastapi.testclient import TestClient

from app.documents.processor import MAX_PDF_SIZE_BYTES
from app.documents.review_service import DocumentReviewService, MAX_UPLOAD_SIZE, _processing_gate
from app.extraction.providers.base import ExtractionInvalidRequestError, ExtractionTimeoutError
from app.extraction.providers.gemini import GeminiExtractionProvider
from app.extraction.providers.openrouter import OpenRouterExtractionProvider
from app.main import app, auth_service, repository
from app.persistence.backup import (
    BackupError,
    backup_application_data,
    main as backup_cli_main,
    restore_application_data,
    verify_data_integrity,
)
from app.persistence.database import CURRENT_SCHEMA_VERSION, get_connection, get_schema_version, init_db
from app.schemas.pdf import PDFPageInspection
from app.security.audit_log import configure_security_logging, log_security_event, security_logger
from app.security.bootstrap import main as bootstrap_cli_main
from tests.fixtures.auth_helpers import authenticate_session_client, reset_test_rate_limiters


def _make_pdf_bytes(text: str = "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE") -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 72), text, fontsize=11)
    raw = doc.tobytes()
    doc.close()
    return raw


class TestPhase3BReliability(unittest.TestCase):
    """Focused verification tests for all Phase 3B Work Packages (WP1-WP5)."""

    def setUp(self):
        reset_test_rate_limiters()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / f"p3b_{uuid4().hex[:8]}.db"
        self.upload_dir = Path(self.temp_dir.name) / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._orig_repo_db = repository.db_path
        self._orig_env = {
            k: os.environ.get(k)
            for k in (
                "SUBGUARD_DB_PATH",
                "SUBGUARD_UPLOAD_DIR",
                "SUBGUARD_ALLOWED_PROVIDERS",
                "SUBGUARD_MAX_FILE_BYTES",
                "SUBGUARD_MAX_ENDORSEMENT_FILES",
                "SUBGUARD_MAX_CUMULATIVE_UPLOAD_BYTES",
                "SUBGUARD_MAX_CONCURRENT_REVIEWS",
                "SUBGUARD_PROCESSING_WAIT_SECONDS",
                "SUBGUARD_MAX_PROVIDER_IMAGES",
                "SUBGUARD_MAX_PROVIDER_PAYLOAD_BYTES",
                "SUBGUARD_AUDIT_LOG_PATH",
                "GEMINI_API_KEY",
                "OPENROUTER_API_KEY",
            )
        }
        os.environ["SUBGUARD_DB_PATH"] = str(self.db_path)
        os.environ["SUBGUARD_UPLOAD_DIR"] = str(self.upload_dir)
        os.environ.pop("SUBGUARD_ALLOWED_PROVIDERS", None)
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
        # Close and remove any test rotating file handlers cleanly before temp cleanup
        for h in list(security_logger.handlers):
            if isinstance(h, logging.handlers.RotatingFileHandler):
                h.close()
                security_logger.removeHandler(h)
        reset_test_rate_limiters()
        self.temp_dir.cleanup()

    # ------------------------------------------------------------------
    # WP1A: Dependency declarations & lockfile
    # ------------------------------------------------------------------

    def test_01_pyproject_and_lockfile_declare_verified_runtime_dependencies(self):
        backend_root = Path(__file__).resolve().parent.parent
        pyproject_text = (backend_root / "pyproject.toml").read_text(encoding="utf-8")
        lock_file = backend_root / "requirements.lock"
        self.assertIn("numpy>=", pyproject_text)
        self.assertIn("openai>=", pyproject_text)
        self.assertIn("google-genai>=", pyproject_text)
        self.assertTrue(lock_file.exists())
        lock_text = lock_file.read_text(encoding="utf-8")
        self.assertIn("numpy==", lock_text)
        self.assertIn("fastapi==", lock_text)
        self.assertIn("openai==", lock_text)
        self.assertIn("google-genai==", lock_text)

    # ------------------------------------------------------------------
    # WP1B: Server-side provider allowlist
    # ------------------------------------------------------------------

    def test_02_provider_allowlist_blocks_disabled_or_unknown_providers_without_invocation(self):
        client, _, _ = authenticate_session_client(self.client, db_path=self.db_path, role="analyst")
        pdf_bytes = _make_pdf_bytes()

        with patch("app.main.review_service.process_and_review") as mock_proc:
            # Default SUBGUARD_ALLOWED_PROVIDERS=mock rejects gemini -> 403
            r_gem = client.post(
                "/api/v1/compliance/review-documents",
                files={"coi_file": ("coi.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
                data={"provider": "gemini", "evaluation_date": "2025-06-15"},
            )
            self.assertEqual(r_gem.status_code, 403)
            self.assertIn("disabled", r_gem.json()["detail"].lower())

            # Default rejects openrouter -> 403
            r_or = client.post(
                "/api/v1/compliance/review-documents",
                files={"coi_file": ("coi.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
                data={"provider": "openrouter", "evaluation_date": "2025-06-15"},
            )
            self.assertEqual(r_or.status_code, 403)

            # Unknown provider -> 400
            r_unk = client.post(
                "/api/v1/compliance/review-documents",
                files={"coi_file": ("coi.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
                data={"provider": "unknown_llm", "evaluation_date": "2025-06-15"},
            )
            self.assertEqual(r_unk.status_code, 400)
            mock_proc.assert_not_called()

        # Explicitly allowing openrouter works cleanly with mocked provider; gemini remains 403
        os.environ["SUBGUARD_ALLOWED_PROVIDERS"] = "mock,openrouter"
        r_gem_still_blocked = client.post(
            "/api/v1/compliance/review-documents",
            files={"coi_file": ("coi.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
            data={"provider": "gemini", "evaluation_date": "2025-06-15"},
        )
        self.assertEqual(r_gem_still_blocked.status_code, 403)

        fake_provider = MagicMock()
        fake_provider.provider_name = "openrouter"
        fake_provider.extract_insurance_data.side_effect = ExtractionTimeoutError(
            "Simulated timeout", provider_name="openrouter"
        )
        with patch("app.extraction.providers.get_extraction_provider", return_value=fake_provider) as mock_factory:
            r_or_allowed = client.post(
                "/api/v1/compliance/review-documents",
                files={"coi_file": ("coi.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
                data={"provider": "openrouter", "evaluation_date": "2025-06-15"},
            )
            self.assertEqual(r_or_allowed.status_code, 200)
            mock_factory.assert_called_once()
            self.assertEqual(r_or_allowed.json()["overall_status"], "unable_to_determine")

    # ------------------------------------------------------------------
    # WP1C: Gemini timeout & bounded multimodal payloads
    # ------------------------------------------------------------------

    def test_03_gemini_timeout_configuration_and_bounded_image_payloads(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.parsed = None
        mock_response.text = '{"document_type": "coi", "coverages": [], "endorsements": []}'
        mock_client.models.generate_content.return_value = mock_response

        provider = GeminiExtractionProvider(api_key="fake-key", max_retries=1, client=mock_client)
        page = PDFPageInspection(page_number=1, width_pts=612, height_pts=792, text_content="ACORD 25")
        provider.extract_insurance_data("doc_t", [page], timeout_seconds=12.5)

        call_kwargs = mock_client.models.generate_content.call_args.kwargs
        self.assertEqual(call_kwargs["config"].http_options.timeout, 12500)

        # Timeout exception propagates cleanly into UNABLE_TO_DETERMINE via DocumentReviewService (never COMPLIANT)
        mock_client.models.generate_content.side_effect = Exception("Deadline exceeded / request timed out")
        os.environ["SUBGUARD_ALLOWED_PROVIDERS"] = "mock,gemini"
        with patch("app.extraction.providers.get_extraction_provider", return_value=provider):
            client, _, _ = authenticate_session_client(self.client, db_path=self.db_path, role="analyst")
            res = client.post(
                "/api/v1/compliance/review-documents",
                files={"coi_file": ("coi.pdf", io.BytesIO(_make_pdf_bytes()), "application/pdf")},
                data={"provider": "gemini", "evaluation_date": "2025-06-15"},
            )
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["overall_status"], "unable_to_determine")

        # Image count cap rejects instead of silently truncating
        os.environ["SUBGUARD_MAX_PROVIDER_IMAGES"] = "2"
        img_pages = [
            PDFPageInspection(
                page_number=i, width_pts=612, height_pts=792, text_content="p", rendered_image_png=b"\x89PNG\r\n"
            )
            for i in range(1, 4)
        ]
        with self.assertRaises(ExtractionInvalidRequestError):
            provider.extract_insurance_data("doc_many", img_pages)
        or_prov = OpenRouterExtractionProvider(api_key="fake-or-key", max_retries=1, client=MagicMock())
        with self.assertRaises(ExtractionInvalidRequestError):
            or_prov.extract_insurance_data("doc_many_or", img_pages)

    # ------------------------------------------------------------------
    # WP2A: Atomic submission+document persistence & orphan upload rollback
    # ------------------------------------------------------------------

    def test_04_atomic_submission_persistence_rolls_back_db_and_failed_uploads_only(self):
        client, _, _ = authenticate_session_client(self.client, db_path=self.db_path, role="analyst")
        pdf_bytes = _make_pdf_bytes()

        # 1. Committed baseline submission must NEVER be affected by later failure
        ok_res = client.post(
            "/api/v1/compliance/review-documents",
            files={"coi_file": ("valid_coi.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
            data={"submission_name": "Committed Baseline", "evaluation_date": "2025-06-15"},
        )
        self.assertEqual(ok_res.status_code, 200)
        committed_sub_id = ok_res.json()["submission_id"]
        committed_dir = self.upload_dir / committed_sub_id
        self.assertTrue(committed_dir.exists())

        # 2. Inject failure on 2nd document insert via SQLite trigger -> whole submission must roll back
        conn = get_connection(self.db_path)
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TRIGGER trg_fail_second_doc
                    BEFORE INSERT ON documents
                    WHEN NEW.document_type = 'endorsement'
                    BEGIN
                        SELECT RAISE(ABORT, 'simulated second document failure');
                    END;
                    """
                )
        finally:
            conn.close()

        before_dirs = {p.name for p in self.upload_dir.iterdir()}
        fail_res = client.post(
            "/api/v1/compliance/review-documents",
            files=[
                ("coi_file", ("coi.pdf", io.BytesIO(pdf_bytes), "application/pdf")),
                ("endorsement_files", ("end.pdf", io.BytesIO(pdf_bytes), "application/pdf")),
            ],
            data={"submission_name": "Should Rollback", "evaluation_date": "2025-06-15"},
        )
        self.assertEqual(fail_res.status_code, 500)
        self.assertIn("reference: err_", fail_res.json()["detail"])

        # Committed submission remains intact; zero partial submission row & zero orphan dir left from failed run
        subs = repository.list_submissions()
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["id"], committed_sub_id)
        after_dirs = {p.name for p in self.upload_dir.iterdir()}
        self.assertEqual(before_dirs, after_dirs)
        self.assertTrue(committed_dir.exists())

        # 3. Disk write failure mid-processing cleans up failed directory and leaves committed dir intact
        with patch.object(
            DocumentReviewService, "_write_validated_pdf", side_effect=OSError("simulated disk full")
        ):
            disk_fail_res = client.post(
                "/api/v1/compliance/review-documents",
                files={"coi_file": ("coi.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
                data={"submission_name": "Disk Fail", "evaluation_date": "2025-06-15"},
            )
            self.assertEqual(disk_fail_res.status_code, 500)
        self.assertEqual({p.name for p in self.upload_dir.iterdir()}, before_dirs)
        self.assertEqual(len(repository.list_submissions()), 1)

    # ------------------------------------------------------------------
    # WP2B & WP2C: Online SQLite backup, integrity verify & staged restore
    # ------------------------------------------------------------------

    def test_05_backup_verify_and_staged_restore_cli_workflows(self):
        client, _, _ = authenticate_session_client(self.client, db_path=self.db_path, role="admin")
        pdf_bytes = _make_pdf_bytes()
        res = client.post(
            "/api/v1/compliance/review-documents",
            files={"coi_file": ("backup_coi.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
            data={"submission_name": "Backup Workflow Test", "evaluation_date": "2025-06-15"},
        )
        self.assertEqual(res.status_code, 200)
        sub_id = res.json()["submission_id"]
        doc_id = res.json()["documents"][0]["id"]

        # Place an excluded .env file and an unexpected orphan file inside upload_dir
        (self.upload_dir / ".env.local").write_text("SECRET=never-backup-me", encoding="utf-8")
        orphan_file = self.upload_dir / "unreferenced_orphan.pdf"
        orphan_file.write_bytes(b"%PDF-1.4 orphan")

        backup_root = Path(self.temp_dir.name) / "backups"
        rc_b = backup_cli_main(
            [
                "backup",
                "--output-dir",
                str(backup_root),
                "--db-path",
                str(self.db_path),
                "--upload-dir",
                str(self.upload_dir),
                "--name",
                "snap_01",
            ]
        )
        self.assertEqual(rc_b, 0)
        snap_dir = backup_root / "snap_01"
        manifest_path = snap_dir / "manifest.json"
        self.assertTrue(manifest_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertIn("database/subguard.db", manifest["files"])
        self.assertNotIn("uploads/.env.local", manifest["files"])
        self.assertIn("unreferenced_orphan.pdf", manifest["orphaned_files"])
        self.assertTrue(orphan_file.exists(), "Backup/verify must never delete unexpected orphan files")

        # Duplicate backup name refused cleanly
        with self.assertRaises(BackupError):
            backup_application_data(
                backup_root, db_path=self.db_path, upload_dir=self.upload_dir, name="snap_01"
            )

        # CLI verify succeeds
        rc_v = backup_cli_main(
            [
                "verify",
                "--db-path",
                str(snap_dir / "database" / "subguard.db"),
                "--upload-dir",
                str(snap_dir / "uploads"),
                "--manifest",
                str(manifest_path),
            ]
        )
        self.assertEqual(rc_v, 0)

        # Refuse restoring over non-empty live target
        with self.assertRaises(BackupError):
            restore_application_data(
                snap_dir, target_db_path=self.db_path, target_upload_dir=self.upload_dir
            )

        # Restore cleanly to explicit empty destination
        restored_db = Path(self.temp_dir.name) / "restored" / "subguard.db"
        restored_up = Path(self.temp_dir.name) / "restored" / "uploads"
        rc_r = backup_cli_main(
            [
                "restore",
                "--backup-dir",
                str(snap_dir),
                "--target-db-path",
                str(restored_db),
                "--target-upload-dir",
                str(restored_up),
            ]
        )
        self.assertEqual(rc_r, 0)
        check_restored = verify_data_integrity(db_path=restored_db, upload_dir=restored_up)
        self.assertTrue(check_restored["ok"])
        conn_r = get_connection(restored_db)
        try:
            row = conn_r.execute("SELECT storage_path FROM documents WHERE id = ?", (doc_id,)).fetchone()
            self.assertTrue(Path(row["storage_path"]).exists())
            self.assertTrue(Path(row["storage_path"]).resolve().is_relative_to(restored_up.resolve()))
        finally:
            conn_r.close()

        # Tampered manifest/upload triggers verify failure
        tampered_target = next((snap_dir / "uploads" / sub_id).iterdir())
        tampered_target.write_bytes(b"%PDF-1.4 tampered")
        bad_verify = verify_data_integrity(
            db_path=snap_dir / "database" / "subguard.db",
            upload_dir=snap_dir / "uploads",
            manifest_path=manifest_path,
        )
        self.assertFalse(bad_verify["ok"])
        self.assertTrue(len(bad_verify["hash_mismatches"]) > 0)

    # ------------------------------------------------------------------
    # WP3A: Event-loop responsiveness & bounded processing concurrency
    # ------------------------------------------------------------------

    def test_06_event_loop_stays_responsive_and_processing_concurrency_is_bounded(self):
        client, csrf_tok, _ = authenticate_session_client(self.client, db_path=self.db_path, role="analyst")
        cookies = dict(client.cookies)
        pdf_bytes = _make_pdf_bytes()

        from app.documents import review_service as rs_mod

        def _slow_sync(*args, **kwargs):
            with _processing_gate.acquire():
                time.sleep(0.5)
                return {
                    "submission_id": "sub_slow_test",
                    "name": "Slow Test",
                    "overall_status": "requires_human_review",
                    "requires_human_review": True,
                    "provider": "mock",
                    "evaluation_date": "2025-06-15",
                    "subcontractor_name": None,
                    "review_result": {"overall_status": "requires_human_review", "requires_human_review": True, "findings": []},
                    "documents": [],
                    "pipeline_errors": [],
                }

        async def _probe_concurrent_health():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                cookies=cookies,
                headers={"X-CSRF-Token": csrf_tok},
            ) as async_client:
                upload_task = asyncio.create_task(
                    async_client.post(
                        "/api/v1/compliance/review-documents",
                        files={"coi_file": ("coi.pdf", pdf_bytes, "application/pdf")},
                        data={"evaluation_date": "2025-06-15"},
                    )
                )
                await asyncio.sleep(0.05)
                t0 = time.perf_counter()
                health_res = await async_client.get("/health")
                health_elapsed = time.perf_counter() - t0
                upload_res = await upload_task
                return health_res, health_elapsed, upload_res

        with patch.object(rs_mod.DocumentReviewService, "_process_validated_sync", side_effect=_slow_sync):
            health_res, health_elapsed, upload_res = asyncio.run(_probe_concurrent_health())
        self.assertEqual(health_res.status_code, 200)
        self.assertEqual(upload_res.status_code, 200)
        self.assertLess(health_elapsed, 0.25)

        # Bounded concurrency saturation test -> HTTP 429 Retry-After
        os.environ["SUBGUARD_MAX_CONCURRENT_REVIEWS"] = "1"
        os.environ["SUBGUARD_PROCESSING_WAIT_SECONDS"] = "0.05"
        with _processing_gate.acquire():
            busy_res = client.post(
                "/api/v1/compliance/review-documents",
                files={"coi_file": ("coi.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
                data={"evaluation_date": "2025-06-15"},
            )
            self.assertEqual(busy_res.status_code, 429)
            self.assertEqual(busy_res.headers.get("retry-after"), "5")

    # ------------------------------------------------------------------
    # WP3B: Aligned upload caps, endorsement count & cumulative upload size
    # ------------------------------------------------------------------

    def test_07_upload_limits_consistent_and_cumulative_caps_enforced(self):
        from app.main import review_service as app_review_svc
        from app.security.config import get_max_upload_file_bytes

        self.assertEqual(app_review_svc.pdf_processor.max_pdf_size_bytes, get_max_upload_file_bytes())
        self.assertEqual(MAX_UPLOAD_SIZE, get_max_upload_file_bytes())
        self.assertLessEqual(MAX_UPLOAD_SIZE, MAX_PDF_SIZE_BYTES)
        client, _, _ = authenticate_session_client(self.client, db_path=self.db_path, role="analyst")
        small_pdf = _make_pdf_bytes()

        os.environ["SUBGUARD_MAX_ENDORSEMENT_FILES"] = "2"
        too_many_res = client.post(
            "/api/v1/compliance/review-documents",
            files=[
                ("coi_file", ("coi.pdf", io.BytesIO(small_pdf), "application/pdf")),
                ("endorsement_files", ("e1.pdf", io.BytesIO(small_pdf), "application/pdf")),
                ("endorsement_files", ("e2.pdf", io.BytesIO(small_pdf), "application/pdf")),
                ("endorsement_files", ("e3.pdf", io.BytesIO(small_pdf), "application/pdf")),
            ],
            data={"evaluation_date": "2025-06-15"},
        )
        self.assertEqual(too_many_res.status_code, 400)
        self.assertIn("too many endorsement files", too_many_res.json()["detail"].lower())

        os.environ["SUBGUARD_MAX_ENDORSEMENT_FILES"] = "10"
        os.environ["SUBGUARD_MAX_FILE_BYTES"] = "50000"
        os.environ["SUBGUARD_MAX_CUMULATIVE_UPLOAD_BYTES"] = "60000"
        pad_pdf = b"%PDF-1.4\n" + (b"0" * 35000)
        cum_res = client.post(
            "/api/v1/compliance/review-documents",
            files=[
                ("coi_file", ("coi.pdf", io.BytesIO(pad_pdf), "application/pdf")),
                ("endorsement_files", ("e1.pdf", io.BytesIO(pad_pdf), "application/pdf")),
            ],
            data={"evaluation_date": "2025-06-15"},
        )
        self.assertEqual(cum_res.status_code, 400)
        self.assertIn("cumulative upload size", cum_res.json()["detail"].lower())

    # ------------------------------------------------------------------
    # WP4A & WP4B: Audit logging handlers, redaction, & safe 500 error responses
    # ------------------------------------------------------------------

    def test_08_security_audit_file_logging_redaction_and_safe_500_responses(self):
        log_file = Path(self.temp_dir.name) / "logs" / "security_audit.log"
        # Call configure twice to ensure zero duplicate handlers are attached
        configure_security_logging(audit_log_path=log_file)
        handler_count_1 = len(security_logger.handlers)
        configure_security_logging(audit_log_path=log_file)
        handler_count_2 = len(security_logger.handlers)
        self.assertEqual(handler_count_1, handler_count_2)

        log_security_event(
            "audit_redaction_check",
            outcome="success",
            username="tester",
            details={
                "password": "MySecretPassword123!",
                "note": "Bearer sg_abcdefghijklmnopqrstuvwx sk-or-v1-secretkey123456 AIzaSyA1234567890123456789012345678901",
                "document_text": "Confidential insured SSN 123-45-6789",
            },
        )
        for h in security_logger.handlers:
            h.flush()

        log_text = log_file.read_text(encoding="utf-8")
        self.assertIn("audit_redaction_check", log_text)
        self.assertNotIn("MySecretPassword123!", log_text)
        self.assertNotIn("sg_abcdefghijklmnopqrstuvwx", log_text)
        self.assertNotIn("sk-or-v1-secretkey123456", log_text)
        self.assertNotIn("AIzaSyA1234567890123456789012345678901", log_text)
        self.assertNotIn("Confidential insured SSN", log_text)

        # Safe HTTP 500 response hides raw exception strings while returning correlation ID
        client, _, _ = authenticate_session_client(self.client, db_path=self.db_path, role="analyst")
        with patch(
            "app.main.review_service.process_and_review",
            side_effect=RuntimeError("INTERNAL_SECRET_DB_PATH=C:/private/secret.db"),
        ):
            r500 = client.post(
                "/api/v1/compliance/review-documents",
                files={"coi_file": ("coi.pdf", io.BytesIO(_make_pdf_bytes()), "application/pdf")},
            )
            self.assertEqual(r500.status_code, 500)
            detail = r500.json()["detail"]
            self.assertIn("reference: err_", detail)
            self.assertNotIn("INTERNAL_SECRET_DB_PATH", detail)

    # ------------------------------------------------------------------
    # WP5: System health endpoint, session purge, & PRAGMA user_version migration
    # ------------------------------------------------------------------

    def test_09_admin_system_health_session_purge_and_schema_version_migration(self):
        # 1. Verify schema migration sets PRAGMA user_version and keeps legacy rows
        legacy_db = Path(self.temp_dir.name) / "legacy_uv0.db"
        conn = sqlite3.connect(str(legacy_db))
        conn.executescript(
            """
            CREATE TABLE submissions (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
                overall_status TEXT NOT NULL, processing_status TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'mock', evaluation_date TEXT NOT NULL,
                subcontractor_name TEXT, result_json TEXT NOT NULL
            );
            CREATE TABLE documents (
                id TEXT PRIMARY KEY, submission_id TEXT NOT NULL, original_filename TEXT NOT NULL,
                document_type TEXT NOT NULL, storage_path TEXT NOT NULL, file_size_bytes INTEGER NOT NULL,
                page_count INTEGER NOT NULL DEFAULT 0, upload_timestamp TEXT NOT NULL,
                processing_status TEXT NOT NULL, error_message TEXT
            );
            CREATE TABLE review_records (
                id TEXT PRIMARY KEY, submission_id TEXT NOT NULL, finding_id TEXT, reason_code TEXT,
                review_status TEXT NOT NULL, reviewer_notes TEXT NOT NULL,
                reviewer_name TEXT NOT NULL DEFAULT 'Compliance Reviewer',
                created_at TEXT NOT NULL, resolved_at TEXT
            );
            INSERT INTO submissions VALUES (
                'sub_keep_1', 'Old Valid Submission', '2025-05-01T00:00:00Z',
                'compliant', 'completed', 'mock', '2025-06-15', 'Legacy Sub', '{}'
            );
            """
        )
        conn.commit()
        conn.close()
        self.assertEqual(get_schema_version(legacy_db), 0)

        init_db(legacy_db)
        self.assertEqual(get_schema_version(legacy_db), CURRENT_SCHEMA_VERSION)
        conn2 = get_connection(legacy_db)
        try:
            self.assertEqual(
                conn2.execute("SELECT name FROM submissions WHERE id='sub_keep_1'").fetchone()["name"],
                "Old Valid Submission",
            )
            mig_cnt = conn2.execute("SELECT COUNT(*) AS c FROM schema_migrations").fetchone()["c"]
            self.assertGreaterEqual(mig_cnt, 2)
        finally:
            conn2.close()

        # 2. Admin-only system-health & session purge endpoints
        viewer_c = TestClient(app)
        authenticate_session_client(
            viewer_c, db_path=self.db_path, username="v_p3b", display_name="Viewer P3B", role="viewer"
        )
        self.assertEqual(viewer_c.get("/api/v1/admin/system-health").status_code, 403)
        self.assertEqual(viewer_c.post("/api/v1/admin/sessions/purge").status_code, 403)

        admin_c, _, admin_u = authenticate_session_client(
            self.client, db_path=self.db_path, username="adm_p3b", display_name="Admin P3B", role="admin"
        )
        health_adm = admin_c.get("/api/v1/admin/system-health")
        self.assertEqual(health_adm.status_code, 200)
        hb = health_adm.json()
        self.assertEqual(hb["status"], "ok")
        self.assertTrue(hb["database_ok"])
        self.assertTrue(hb["upload_storage_ok"])
        self.assertEqual(hb["schema_version"], CURRENT_SCHEMA_VERSION)

        # Public /health stays minimal
        pub_h = TestClient(app).get("/health").json()
        self.assertEqual(pub_h, {"status": "ok", "service": "compliance-evaluator"})

        # Seed expired + revoked sessions alongside current active session; purge removes only stale rows
        past_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        conn3 = get_connection(self.db_path)
        try:
            with conn3:
                conn3.execute(
                    """
                    INSERT INTO sessions (id, user_id, token_hash, token_kind, label, csrf_token, created_at, last_seen_at, expires_at, revoked_at)
                    VALUES ('ses_exp', ?, 'hash_exp_1', 'session', 'old', 'c', ?, ?, ?, NULL),
                           ('ses_rev', ?, 'hash_rev_1', 'api_token', 'rev', NULL, ?, ?, '2099-01-01T00:00:00Z', ?)
                    """,
                    (admin_u["id"], past_ts, past_ts, past_ts, admin_u["id"], past_ts, past_ts, past_ts),
                )
        finally:
            conn3.close()

        purge_res = admin_c.post("/api/v1/admin/sessions/purge")
        self.assertEqual(purge_res.status_code, 200)
        self.assertEqual(purge_res.json()["purged"], 2)
        # Active admin session still works!
        self.assertEqual(admin_c.get("/api/v1/auth/me").status_code, 200)
        # CLI purge-sessions works cleanly too
        self.assertEqual(bootstrap_cli_main(["purge-sessions", "--db-path", str(self.db_path)]), 0)


if __name__ == "__main__":
    unittest.main()
