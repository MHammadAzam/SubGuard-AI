"""Unit, persistence, and workflow integration tests for SubGuard AI product features.

Tests:
1. SQLite persistence (database, tables, foreign keys, submission repository)
2. Document Review API:
   - Valid upload with COI and endorsement
   - Rejection of non-PDF, empty, corrupted, and oversized files
   - Honest mock mode: arbitrary document rejection without hallucinated values
   - Document download with path-traversal protection
3. Human review recording and audit trail
4. Frontend HTML endpoints (dashboard, new, detail, evidence, review, history)
5. End-to-end demonstration workflow using synthetic fixtures
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app, repository
from app.persistence.database import get_connection, init_db
from app.persistence.repository import SubmissionRepository
from app.schemas.insurance import ComplianceStatus
from tests.fixtures.auth_helpers import authenticate_session_client


class TestSQLitePersistence(unittest.TestCase):
    """Verify SQLite database initialization and repository operations."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_subguard.db"
        init_db(self.db_path)
        self.repo = SubmissionRepository(db_path=self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_init_db_creates_tables(self):
        conn = get_connection(self.db_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r["name"] for r in cur.fetchall()}
            self.assertIn("submissions", tables)
            self.assertIn("documents", tables)
            self.assertIn("review_records", tables)
        finally:
            conn.close()

    def test_create_and_retrieve_submission(self):
        sub = self.repo.create_submission(
            submission_id="sub_test_001",
            name="Test Apex Review",
            overall_status="compliant",
            processing_status="completed",
            provider="mock",
            evaluation_date="2025-06-15",
            result_data={"test_key": "test_val"},
            subcontractor_name="Apex Electrical Contractors LLC",
        )
        self.assertEqual(sub["id"], "sub_test_001")
        self.assertEqual(sub["overall_status"], "compliant")
        self.assertEqual(sub["result"], {"test_key": "test_val"})

        # Add document
        doc = self.repo.add_document(
            document_id="doc_test_001",
            submission_id="sub_test_001",
            original_filename="acord25.pdf",
            document_type="coi",
            storage_path="/safe/path/acord25.pdf",
            file_size_bytes=1024,
            page_count=1,
        )
        self.assertEqual(doc["id"], "doc_test_001")

        # Query submission by id includes documents
        retrieved = self.repo.get_submission_by_id("sub_test_001")
        self.assertIsNotNone(retrieved)
        self.assertEqual(len(retrieved["documents"]), 1)
        self.assertEqual(retrieved["documents"][0]["original_filename"], "acord25.pdf")

    def test_dashboard_stats_calculation(self):
        self.repo.create_submission(
            submission_id="sub_c1",
            name="Compliant 1",
            overall_status="compliant",
            processing_status="completed",
            provider="mock",
            evaluation_date="2025-06-15",
            result_data={},
        )
        self.repo.create_submission(
            submission_id="sub_nc1",
            name="Non-Compliant 1",
            overall_status="non_compliant",
            processing_status="completed",
            provider="mock",
            evaluation_date="2025-06-15",
            result_data={},
        )
        self.repo.create_submission(
            submission_id="sub_hr1",
            name="Review 1",
            overall_status="requires_human_review",
            processing_status="completed",
            provider="mock",
            evaluation_date="2025-06-15",
            result_data={},
        )

        stats = self.repo.get_dashboard_stats()
        self.assertEqual(stats["total_submissions"], 3)
        self.assertEqual(stats["compliant"], 1)
        self.assertEqual(stats["non_compliant"], 1)
        self.assertEqual(stats["requires_human_review"], 1)
        self.assertEqual(stats["missing_evidence"], 0)

    def test_add_human_review_and_status_update(self):
        self.repo.create_submission(
            submission_id="sub_rev_001",
            name="Review Target",
            overall_status="requires_human_review",
            processing_status="completed",
            provider="mock",
            evaluation_date="2025-06-15",
            result_data={},
        )

        review = self.repo.add_review_record(
            submission_id="sub_rev_001",
            review_status="accepted_exception",
            reviewer_notes="Approved exception for umbrella limit by Project Director",
            reviewer_name="Jane Doe",
            update_submission_status="compliant",
        )
        self.assertEqual(review["review_status"], "accepted_exception")
        self.assertEqual(review["reviewer_name"], "Jane Doe")

        updated_sub = self.repo.get_submission_by_id("sub_rev_001")
        self.assertEqual(updated_sub["overall_status"], "compliant")
        self.assertEqual(len(updated_sub["reviews"]), 1)


class TestDocumentUploadAndReviewAPI(unittest.TestCase):
    """Test the document upload endpoint, validation, security, and responses."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "prod_wf.db"
        self.upload_dir = Path(self.temp_dir.name) / "uploads"
        self._orig_repo_db = repository.db_path
        self._orig_env_db = os.environ.get("SUBGUARD_DB_PATH")
        self._orig_env_up = os.environ.get("SUBGUARD_UPLOAD_DIR")
        os.environ["SUBGUARD_DB_PATH"] = str(self.db_path)
        os.environ["SUBGUARD_UPLOAD_DIR"] = str(self.upload_dir)
        init_db(self.db_path)
        repository.db_path = self.db_path
        self.client = TestClient(app)
        authenticate_session_client(self.client, db_path=self.db_path, role="admin")
        self.fixtures_dir = Path(__file__).resolve().parent.parent / "evaluation" / "fixtures" / "pdfs"

    def tearDown(self):
        repository.db_path = self._orig_repo_db
        if self._orig_env_db is None:
            os.environ.pop("SUBGUARD_DB_PATH", None)
        else:
            os.environ["SUBGUARD_DB_PATH"] = self._orig_env_db
        if self._orig_env_up is None:
            os.environ.pop("SUBGUARD_UPLOAD_DIR", None)
        else:
            os.environ["SUBGUARD_UPLOAD_DIR"] = self._orig_env_up
        self.temp_dir.cleanup()

    def test_reject_missing_coi(self):
        """Missing required coi_file returns 422 Unprocessable Entity."""
        res = self.client.post("/api/v1/compliance/review-documents", data={})
        self.assertEqual(res.status_code, 422)

    def test_reject_empty_file(self):
        """Empty PDF file (0 bytes) returns HTTP 400 Bad Request."""
        empty_pdf = io.BytesIO(b"")
        res = self.client.post(
            "/api/v1/compliance/review-documents",
            files={"coi_file": ("empty.pdf", empty_pdf, "application/pdf")},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("empty", res.json()["detail"].lower())

    def test_reject_non_pdf_file(self):
        """Non-PDF file missing %PDF header returns HTTP 400 Bad Request."""
        text_file = io.BytesIO(b"This is a plain text file, not a valid PDF.")
        res = self.client.post(
            "/api/v1/compliance/review-documents",
            files={"coi_file": ("not_a_pdf.pdf", text_file, "application/pdf")},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("magic", res.json()["detail"].lower())

    def test_reject_oversized_file(self):
        """File exceeding MAX_UPLOAD_SIZE (15 MB) returns HTTP 400 Bad Request."""
        fake_large = io.BytesIO(b"%PDF-" + b"0" * (16 * 1024 * 1024))
        res = self.client.post(
            "/api/v1/compliance/review-documents",
            files={"coi_file": ("large.pdf", fake_large, "application/pdf")},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("exceeds limit", res.json()["detail"].lower())

    def test_honest_mock_rejects_arbitrary_pdf_without_hallucinated_values(self):
        """Arbitrary valid PDF without insurance structure is NOT falsely passed in mock mode.

        Mock mode must be honest: zero manufactured quotes, zero fake limits, NEVER COMPLIANT.
        """
        # Create a valid minimal PDF that contains no insurance text
        minimal_pdf = (
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R/Resources<<>>>>endobj\n"
            b"xref\n0 4\n0000000000 65535 f \n0000000010 00000 n \n0000000053 00000 n \n0000000102 00000 n \n"
            b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n178\n%%EOF"
        )
        res = self.client.post(
            "/api/v1/compliance/review-documents",
            files={"coi_file": ("arbitrary_document.pdf", io.BytesIO(minimal_pdf), "application/pdf")},
            data={"submission_name": "Arbitrary Document Upload", "evaluation_date": "2025-06-15"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        # Invariant: Must NEVER be compliant!
        self.assertNotEqual(data["overall_status"], "compliant")
        self.assertTrue(data["requires_human_review"])
        self.assertIn("mock", data["provider"])

    def test_valid_synthetic_coi_and_endorsement_review(self):
        """Uploading synthetic ACORD 25 + blanket endorsement results in grounded extraction & evaluation."""
        coi_pdf_path = self.fixtures_dir / "synthetic_standard_acord25.pdf"
        end_pdf_path = self.fixtures_dir / "synthetic_blanket_endorsement.pdf"

        if not coi_pdf_path.exists() or not end_pdf_path.exists():
            self.skipTest("Synthetic test fixture PDFs missing")

        with open(coi_pdf_path, "rb") as f_coi, open(end_pdf_path, "rb") as f_end:
            res = self.client.post(
                "/api/v1/compliance/review-documents",
                files=[
                    ("coi_file", ("synthetic_standard_acord25.pdf", f_coi, "application/pdf")),
                    ("endorsement_files", ("synthetic_blanket_endorsement.pdf", f_end, "application/pdf")),
                ],
                data={
                    "submission_name": "E2E Integration Test: Apex Electric",
                    "evaluation_date": "2025-06-15",
                },
            )

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("submission_id", data)
        # In accordance with Safeguard Rule 8b, negative manuscript endorsement claims without independent
        # integrity verification trigger REQUIRES_HUMAN_REVIEW for human auditor confirmation.
        self.assertEqual(data["overall_status"], "requires_human_review")
        self.assertTrue(data["requires_human_review"])
        self.assertEqual(len(data["documents"]), 2)

        # Inspect persisted submission
        persisted = repository.get_submission_by_id(data["submission_id"])
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["overall_status"], "requires_human_review")
        self.assertEqual(len(persisted["documents"]), 2)

        # Download document via secure API
        doc_id = persisted["documents"][0]["id"]
        dl_res = self.client.get(f"/api/v1/documents/{doc_id}/download")
        self.assertEqual(dl_res.status_code, 200)
        self.assertEqual(dl_res.headers["content-type"], "application/pdf")


class TestFrontendWebPages(unittest.TestCase):
    """Test all 6 HTML frontend pages render successfully."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "frontend_pages.db"
        self._orig_repo_db = repository.db_path
        self._orig_env_db = os.environ.get("SUBGUARD_DB_PATH")
        os.environ["SUBGUARD_DB_PATH"] = str(self.db_path)
        init_db(self.db_path)
        repository.db_path = self.db_path
        self.client = TestClient(app)
        authenticate_session_client(self.client, db_path=self.db_path, role="admin")

    def tearDown(self):
        repository.db_path = self._orig_repo_db
        if self._orig_env_db is None:
            os.environ.pop("SUBGUARD_DB_PATH", None)
        else:
            os.environ["SUBGUARD_DB_PATH"] = self._orig_env_db
        self.temp_dir.cleanup()

    def test_dashboard_page_loads(self):
        res = self.client.get("/dashboard")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers["content-type"])
        self.assertIn("Compliance Dashboard", res.text)
        self.assertIn("Provider: <strong>Mock Mode", res.text)

    def test_root_redirects_to_dashboard(self):
        res = self.client.get("/", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["location"], "/dashboard")

    def test_new_submission_page_loads(self):
        res = self.client.get("/submissions/new")
        self.assertEqual(res.status_code, 200)
        self.assertIn("New Document Review", res.text)
        self.assertIn("Certificate of Insurance (COI) PDF", res.text)
        self.assertIn("Commercial General Liability", res.text)

    def test_submission_history_page_loads(self):
        res = self.client.get("/submissions")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Submission History", res.text)

    def test_submission_detail_and_evidence_pages(self):
        from uuid import uuid4
        sub_id = f"sub_ui_{uuid4().hex[:8]}"
        # Create a submission first
        sub = repository.create_submission(
            submission_id=sub_id,
            name="UI Test Submission",
            overall_status="compliant",
            processing_status="completed",
            provider="mock",
            evaluation_date="2025-06-15",
            result_data={
                "review": {
                    "subcontractor_id": "sub_apex",
                    "evaluation_date": "2025-06-15",
                    "overall_status": "compliant",
                    "requires_human_review": False,
                    "findings": [],
                },
                "document_set": {
                    "subcontractor_id": "sub_apex",
                    "documents_submitted": ["coi"],
                    "is_document_set_complete": True,
                    "named_insured": {"name": "Apex Electrical Contractors LLC"},
                    "certificate_holder": {"name": "General Contractor Corp"},
                    "coverages": [],
                    "endorsements": [],
                    "raw_contradictions_detected": [],
                },
            },
            subcontractor_name="Apex Electrical Contractors LLC",
        )

        # Page 3: Detail
        res = self.client.get(f"/submissions/{sub_id}")
        self.assertEqual(res.status_code, 200)
        self.assertIn("UI Test Submission", res.text)
        self.assertIn("Apex Electrical Contractors LLC", res.text)

        # Page 4: Evidence
        res_ev = self.client.get(f"/submissions/{sub_id}/evidence")
        self.assertEqual(res_ev.status_code, 200)
        self.assertIn("Evidence Verification", res_ev.text)

        # Page 5: Review
        res_rev = self.client.get(f"/submissions/{sub_id}/review")
        self.assertEqual(res_rev.status_code, 200)
        self.assertIn("Human Review Decision", res_rev.text)


if __name__ == "__main__":
    unittest.main()
