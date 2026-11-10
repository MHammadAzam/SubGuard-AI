"""Phase 3D: Verification tests for Critical Reliability & Security Defect Remediation (WP1-WP4).

All tests run strictly against isolated temporary SQLite databases and upload directories.
Zero live AI API calls, zero mutations to frozen evaluation cohorts/OCR/demo server/real DB.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
import pymupdf
from fastapi.testclient import TestClient

from app.extraction.providers import get_benchmark_extraction_provider, get_extraction_provider
from app.main import app, repository, review_service
from app.persistence.backup import (
    BackupError,
    backup_application_data,
    restore_application_data,
    verify_data_integrity,
)
from app.persistence.database import (
    CURRENT_SCHEMA_VERSION,
    get_connection,
    get_schema_version,
    init_db,
)
from tests.fixtures.auth_helpers import authenticate_session_client, reset_test_rate_limiters


def _make_pdf_bytes(text: str = "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE") -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 72), text, fontsize=11)
    raw = doc.tobytes()
    doc.close()
    return raw


class TestPhase3DDefects(unittest.TestCase):
    """Regression coverage for WP1 (backup/restore), WP2 (provider allowlist), WP3 (demo-run), WP4 (migrations)."""

    def setUp(self):
        reset_test_rate_limiters()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / f"p3d_{uuid4().hex[:8]}.db"
        self.upload_dir = self.root / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._orig_repo_db = repository.db_path
        self._orig_env = {
            k: os.environ.get(k)
            for k in (
                "SUBGUARD_DB_PATH",
                "SUBGUARD_UPLOAD_DIR",
                "SUBGUARD_ALLOWED_PROVIDERS",
                "SUBGUARD_MAX_CONCURRENT_REVIEWS",
                "SUBGUARD_PROCESSING_WAIT_SECONDS",
                "SUBGUARD_ENABLE_DEMO_ENDPOINT",
                "EXTRACTION_PROVIDER",
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
        self.client.close()
        self.temp_dir.cleanup()

    def _create_sample_backup(self, *, backup_name: str = "snap_ok") -> tuple[Path, str, str]:
        sub_id = "sub_wp1_safe"
        doc_id = "doc_wp1_safe"
        sub_dir = self.upload_dir / sub_id
        sub_dir.mkdir(parents=True, exist_ok=True)
        pdf_file = sub_dir / f"{doc_id}_coi.pdf"
        pdf_bytes = _make_pdf_bytes()
        pdf_file.write_bytes(pdf_bytes)

        repository.create_submission_with_documents(
            submission_id=sub_id,
            name="WP1 Sample",
            overall_status="requires_human_review",
            processing_status="completed",
            provider="mock",
            evaluation_date="2025-06-15",
            result_data={"review": {"findings": []}, "document_set": {}},
            documents=[
                {
                    "id": doc_id,
                    "submission_id": sub_id,
                    "original_filename": "coi.pdf",
                    "document_type": "coi",
                    "storage_path": str(pdf_file.resolve()),
                    "file_size_bytes": len(pdf_bytes),
                    "page_count": 1,
                    "processing_status": "processed",
                    "error_message": None,
                }
            ],
            subcontractor_name="Sample Sub LLC",
        )

        backup_root = self.root / "backups"
        res = backup_application_data(
            backup_root,
            db_path=self.db_path,
            upload_dir=self.upload_dir,
            name=backup_name,
        )
        return Path(res["backup_dir"]), sub_id, doc_id

    # ------------------------------------------------------------------
    # WP1: Backup, Manifest/DB Containment, URI, Integrity, Promotion
    # ------------------------------------------------------------------

    def test_wp1_manifest_traversal_absolute_and_unexpected_keys_rejected(self):
        snap_dir, _, _ = self._create_sample_backup()
        manifest_path = snap_dir / "manifest.json"
        original_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        malicious_keys = [
            "uploads/../../outside_secret.txt",
            "uploads/../database/subguard.db",
            "/etc/passwd",
            "C:\\Windows\\System32\\drivers\\etc\\hosts",
            "unexpected/arbitrary.txt",
        ]
        for bad_key in malicious_keys:
            evil_dir = self.root / f"evil_{uuid4().hex[:6]}"
            shutil.copytree(snap_dir, evil_dir)
            evil_manifest = dict(original_manifest)
            evil_files = dict(original_manifest["files"])
            evil_files[bad_key] = {"size_bytes": 4, "sha256": "0" * 64}
            evil_manifest["files"] = evil_files
            (evil_dir / "manifest.json").write_text(json.dumps(evil_manifest), encoding="utf-8")

            with self.assertRaises(BackupError, msg=f"verify must reject manifest key {bad_key!r}"):
                verify_data_integrity(
                    db_path=evil_dir / "database" / "subguard.db",
                    upload_dir=evil_dir / "uploads",
                    manifest_path=evil_dir / "manifest.json",
                )
            with self.assertRaises(BackupError, msg=f"restore must reject manifest key {bad_key!r}"):
                restore_application_data(
                    evil_dir,
                    target_db_path=self.root / f"target_{uuid4().hex[:4]}" / "sub.db",
                    target_upload_dir=self.root / f"target_{uuid4().hex[:4]}" / "uploads",
                )

    def test_wp1_db_submission_id_and_raw_storage_path_traversal_rejected(self):
        snap_dir, sub_id, doc_id = self._create_sample_backup()
        snap_db = snap_dir / "database" / "subguard.db"

        # Case 1: Malicious storage_path escaping submission folder / pointing to host secret
        outside_file = self.root / "outside_host_secret.pdf"
        outside_file.write_bytes(b"%PDF-1.4 secret")
        conn = sqlite3.connect(str(snap_db))
        with conn:
            conn.execute(
                "UPDATE documents SET storage_path = ? WHERE id = ?",
                (str(outside_file), doc_id),
            )
        conn.close()

        with self.assertRaises(BackupError):
            verify_data_integrity(db_path=snap_db, upload_dir=snap_dir / "uploads")
        with self.assertRaises(BackupError):
            restore_application_data(
                snap_dir,
                target_db_path=self.root / "restored_bad_sp" / "subguard.db",
                target_upload_dir=self.root / "restored_bad_sp" / "uploads",
            )

        # Case 2: Traversal in storage_path ('..')
        conn = sqlite3.connect(str(snap_db))
        with conn:
            conn.execute(
                "UPDATE documents SET storage_path = ? WHERE id = ?",
                (f"../{sub_id}/{doc_id}_coi.pdf", doc_id),
            )
        conn.close()
        with self.assertRaises(BackupError):
            verify_data_integrity(db_path=snap_db, upload_dir=snap_dir / "uploads")

    def test_wp1_symlink_rejected_in_backup_verify_and_restore(self):
        snap_dir, sub_id, doc_id = self._create_sample_backup()
        target_doc = next((snap_dir / "uploads" / sub_id).iterdir())

        # Deterministically simulate a symlink on any OS/privilege level
        orig_is_symlink = Path.is_symlink

        def fake_is_symlink(path_self: Path) -> bool:
            if path_self.name == target_doc.name:
                return True
            return orig_is_symlink(path_self)

        with patch.object(Path, "is_symlink", autospec=True, side_effect=fake_is_symlink):
            with self.assertRaises(BackupError):
                verify_data_integrity(
                    db_path=snap_dir / "database" / "subguard.db",
                    upload_dir=snap_dir / "uploads",
                    manifest_path=snap_dir / "manifest.json",
                )
            with self.assertRaises(BackupError):
                restore_application_data(
                    snap_dir,
                    target_db_path=self.root / "restored_sym" / "subguard.db",
                    target_upload_dir=self.root / "restored_sym" / "uploads",
                )

    def test_wp1_sqlite_uri_handles_spaces_hash_and_percent_in_path(self):
        special_dir = self.root / "dir with spaces #1 %ok"
        special_dir.mkdir(parents=True, exist_ok=True)
        special_db = special_dir / "subguard #special.db"
        special_uploads = special_dir / "uploads #dir"
        special_uploads.mkdir(parents=True, exist_ok=True)
        init_db(special_db)

        out_dir = self.root / "special_backups"
        res = backup_application_data(
            out_dir,
            db_path=special_db,
            upload_dir=special_uploads,
            name="snap_uri_chars",
        )
        self.assertEqual(res["status"], "ok")
        bdir = Path(res["backup_dir"])
        manifest = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("snapshot_consistency", manifest)
        self.assertTrue(manifest["snapshot_consistency"]["quiet_window_recommended"])

    def test_wp1_restore_verifies_post_rewrite_integrity_fks_and_upload_hashes(self):
        snap_dir, _, doc_id = self._create_sample_backup()
        restored_db = self.root / "restored_ok" / "subguard.db"
        restored_up = self.root / "restored_ok" / "uploads"

        res = restore_application_data(
            snap_dir,
            target_db_path=restored_db,
            target_upload_dir=restored_up,
        )
        self.assertEqual(res["status"], "restored")
        self.assertEqual(res["integrity_check"], "ok")
        self.assertTrue(res["upload_hashes_verified"])
        self.assertEqual(res["schema_version"], CURRENT_SCHEMA_VERSION)

        conn = get_connection(restored_db)
        try:
            row = conn.execute("SELECT storage_path FROM documents WHERE id = ?", (doc_id,)).fetchone()
            sp = Path(row["storage_path"]).resolve()
            self.assertTrue(sp.is_relative_to(restored_up.resolve()))
            self.assertTrue(sp.exists())
            self.assertEqual(conn.execute("PRAGMA foreign_key_check;").fetchall(), [])
        finally:
            conn.close()

    def test_wp1_restore_promotion_failure_rolls_back_dest_uploads(self):
        snap_dir, _, _ = self._create_sample_backup()
        dest_root = self.root / "rollback_target"
        dest_db = dest_root / "subguard.db"
        dest_up = dest_root / "uploads"
        # Pre-create an empty destination upload dir to verify it stays empty after rollback
        dest_up.mkdir(parents=True, exist_ok=True)

        orig_move = shutil.move
        move_calls = {"count": 0}

        def failing_second_move(src: str, dst: str, *args, **kwargs):
            move_calls["count"] += 1
            if move_calls["count"] == 2:
                raise OSError("Simulated cross-device DB promotion failure")
            return orig_move(src, dst, *args, **kwargs)

        with patch("app.persistence.backup.shutil.move", side_effect=failing_second_move):
            with self.assertRaises(OSError):
                restore_application_data(
                    snap_dir,
                    target_db_path=dest_db,
                    target_upload_dir=dest_up,
                )

        self.assertFalse(dest_db.exists())
        self.assertTrue(dest_up.exists())
        self.assertEqual(list(dest_up.iterdir()), [], "Rollback must remove orphaned moved uploads")
        self.assertEqual(
            [p for p in dest_root.iterdir() if p.name.startswith(".restore_staging_")],
            [],
            "Staging directory must be removed after failed promotion",
        )

    # ------------------------------------------------------------------
    # WP2: Fail-closed get_extraction_provider()
    # ------------------------------------------------------------------

    def test_wp2_provider_factory_is_fail_closed_by_default(self):
        os.environ.pop("SUBGUARD_ALLOWED_PROVIDERS", None)
        # Default allowlist is {"mock", "honest_mock", "application_mock"}
        self.assertEqual(get_extraction_provider("mock").provider_name, "mock")

        with self.assertRaises(PermissionError):
            get_extraction_provider("gemini", api_key="fake-key")
        with self.assertRaises(PermissionError):
            get_extraction_provider("openrouter", api_key="fake-key")

        # No general enforce_allowlist bypass kwarg on production factory
        with self.assertRaises(TypeError):
            get_extraction_provider("openrouter", api_key="fake-key", enforce_allowlist=False)  # type: ignore[call-arg]

        # When allowlist explicitly permits openrouter, openrouter works while gemini remains blocked
        os.environ["SUBGUARD_ALLOWED_PROVIDERS"] = "openrouter"
        p_or = get_extraction_provider("openrouter", api_key="fake-key")
        self.assertEqual(p_or.provider_name, "openrouter")
        with self.assertRaises(PermissionError):
            get_extraction_provider("gemini", api_key="fake-key")
        with self.assertRaises(PermissionError):
            get_extraction_provider("mock")

        # Isolated benchmark/test factory works cleanly for evaluation runners regardless of runtime allowlist
        p_bench = get_benchmark_extraction_provider("gemini", api_key="fake-key")
        self.assertEqual(p_bench.provider_name, "gemini")

    # ------------------------------------------------------------------
    # WP3: Bounded, Non-Blocking, Failure-Atomic Demo Endpoint
    # ------------------------------------------------------------------

    def test_wp3_demo_run_nonblocking_bounded_concurrency_and_provider_policy(self):
        client, csrf_tok, _ = authenticate_session_client(self.client, db_path=self.db_path, role="analyst")
        cookies = dict(client.cookies)

        # Provider policy cannot be bypassed when mock is disabled in allowlist
        os.environ["SUBGUARD_ALLOWED_PROVIDERS"] = "openrouter"
        blocked = client.post("/api/v1/compliance/demo-run/SCEN-01")
        self.assertEqual(blocked.status_code, 403)
        os.environ.pop("SUBGUARD_ALLOWED_PROVIDERS", None)

        # Bounded concurrency (limit=1, short wait) + event loop stays responsive during execution
        os.environ["SUBGUARD_MAX_CONCURRENT_REVIEWS"] = "1"
        os.environ["SUBGUARD_PROCESSING_WAIT_SECONDS"] = "0.05"
        import time

        orig_process_pdf = review_service.pdf_processor.process_pdf

        def slow_process_pdf(pdf_bytes: bytes, document_id: str):
            time.sleep(0.45)
            return orig_process_pdf(pdf_bytes, document_id=document_id)

        async def _run_concurrent():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                cookies=cookies,
                headers={"X-CSRF-Token": csrf_tok},
            ) as async_client:
                t1 = asyncio.create_task(async_client.post("/api/v1/compliance/demo-run/SCEN-01"))
                await asyncio.sleep(0.08)
                t0 = time.perf_counter()
                health_res = await async_client.get("/health")
                health_elapsed = time.perf_counter() - t0
                second_res = await async_client.post("/api/v1/compliance/demo-run/SCEN-01")
                first_res = await t1
                return health_res, health_elapsed, second_res, first_res

        with patch.object(review_service.pdf_processor, "process_pdf", side_effect=slow_process_pdf):
            health_res, health_elapsed, second_res, first_res = asyncio.run(_run_concurrent())

        self.assertEqual(health_res.status_code, 200)
        self.assertLess(health_elapsed, 0.25, "Sync demo processing must not block the event loop")
        self.assertEqual(second_res.status_code, 429)
        self.assertEqual(second_res.headers.get("retry-after"), "5")
        self.assertEqual(first_res.status_code, 200)

    def test_wp3_demo_run_cleans_up_on_any_stage_failure_without_touching_siblings(self):
        client, _, _ = authenticate_session_client(self.client, db_path=self.db_path, role="analyst")

        # Pre-existing sibling submission folder that MUST survive untouched
        sibling_dir = self.upload_dir / "existing_sub_keep"
        sibling_dir.mkdir(parents=True, exist_ok=True)
        sibling_file = sibling_dir / "keep.pdf"
        sibling_file.write_bytes(_make_pdf_bytes("Keep me"))

        stages = [
            patch.object(review_service.pdf_processor, "process_pdf", side_effect=RuntimeError("PDF boom")),
            patch("app.extraction.pipeline.ExtractionPipelineService.process_inspection", side_effect=RuntimeError("Extract boom")),
            patch.object(review_service.evaluator, "evaluate", side_effect=RuntimeError("Eval boom")),
            patch.object(repository, "create_submission_with_documents", side_effect=RuntimeError("DB boom")),
        ]

        for stage_patch in stages:
            with stage_patch:
                res = client.post("/api/v1/compliance/demo-run/SCEN-01")
            self.assertEqual(res.status_code, 500)
            detail = res.json()["detail"]
            self.assertIn("reference: err_", detail)
            self.assertNotIn("boom", detail)
            # Zero leftover demo_* directories and zero persisted submissions
            remaining = {p.name for p in self.upload_dir.iterdir()}
            self.assertEqual(remaining, {"existing_sub_keep"})
            self.assertTrue(sibling_file.exists())
            self.assertEqual(len(repository.list_submissions()), 0)

    # ------------------------------------------------------------------
    # WP4: Schema Version Rejection & Transactional Migration Safety
    # ------------------------------------------------------------------

    def test_wp4_rejects_future_schema_version_cleanly(self):
        future_db = self.root / "future_schema.db"
        init_db(future_db)
        conn = get_connection(future_db)
        try:
            with conn:
                conn.execute("PRAGMA user_version = 99;")
        finally:
            conn.close()

        with self.assertRaises(RuntimeError) as ctx:
            init_db(future_db)
        self.assertIn("99", str(ctx.exception))
        self.assertEqual(get_schema_version(future_db), 99)

    def test_wp4_v0_migration_atomic_rollback_and_successful_upgrade(self):
        # Build a realistic pre-Phase-2 v0 database (user_version=0, no reviewer_user_id/users/sessions/schema_migrations)
        v0_db = self.root / "legacy_v0.db"
        raw_conn = sqlite3.connect(str(v0_db))
        try:
            raw_conn.executescript(
                """
                PRAGMA foreign_keys = ON;
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
                    error_message TEXT,
                    FOREIGN KEY (submission_id) REFERENCES submissions(id) ON DELETE CASCADE
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
                    resolved_at TEXT,
                    FOREIGN KEY (submission_id) REFERENCES submissions(id) ON DELETE CASCADE
                );
                INSERT INTO submissions VALUES (
                    'sub_legacy_1', 'Legacy Submission', '2025-01-01T00:00:00Z',
                    'requires_human_review', 'completed', 'mock', '2025-06-15', 'Legacy Co', '{}'
                );
                INSERT INTO documents VALUES (
                    'doc_legacy_1', 'sub_legacy_1', 'coi.pdf', 'coi', '/tmp/coi.pdf',
                    1024, 1, '2025-01-01T00:00:00Z', 'processed', NULL
                );
                INSERT INTO review_records VALUES (
                    'rev_legacy_1', 'sub_legacy_1', 'f1', 'missing_doc', 'pending',
                    'Legacy note', 'Old Reviewer', '2025-01-01T00:00:00Z', NULL
                );
                PRAGMA user_version = 0;
                """
            )
        finally:
            raw_conn.close()

        # Inject a failure AFTER DDL/ALTER statements inside _apply_schema_migrations
        from app.persistence import database as db_mod

        orig_apply = db_mod._apply_schema_migrations

        def fail_mid_migration(conn: sqlite3.Connection, current_uv: int) -> None:
            orig_apply(conn, current_uv)
            raise RuntimeError("Simulated mid-migration power loss")

        with patch("app.persistence.database._apply_schema_migrations", side_effect=fail_mid_migration):
            with self.assertRaises(RuntimeError):
                init_db(v0_db)

        # Verify atomic rollback: no partial schema_migrations/users table, no reviewer_user_id column, user_version == 0
        check_conn = sqlite3.connect(str(v0_db))
        try:
            tables = {
                r[0]
                for r in check_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            self.assertNotIn("users", tables)
            self.assertNotIn("schema_migrations", tables)
            rev_cols = {r[1] for r in check_conn.execute("PRAGMA table_info(review_records)").fetchall()}
            self.assertNotIn("reviewer_user_id", rev_cols)
            self.assertEqual(check_conn.execute("PRAGMA user_version;").fetchone()[0], 0)
            self.assertEqual(check_conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 1)
        finally:
            check_conn.close()

        # Now allow migration to complete cleanly and verify records + FKs + version
        init_db(v0_db)
        post_conn = get_connection(v0_db)
        try:
            self.assertEqual(get_schema_version(v0_db), CURRENT_SCHEMA_VERSION)
            self.assertEqual(post_conn.execute("PRAGMA integrity_check;").fetchone()[0], "ok")
            self.assertEqual(post_conn.execute("PRAGMA foreign_key_check;").fetchall(), [])
            sub_row = post_conn.execute("SELECT name FROM submissions WHERE id = 'sub_legacy_1'").fetchone()
            self.assertEqual(sub_row["name"], "Legacy Submission")
            rev_cols = {r[1] for r in post_conn.execute("PRAGMA table_info(review_records)").fetchall()}
            self.assertIn("reviewer_user_id", rev_cols)
        finally:
            post_conn.close()


if __name__ == "__main__":
    unittest.main()
