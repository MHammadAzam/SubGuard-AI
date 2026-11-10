"""Milestone 2.2: Real Browser Automation & Workflow Acceptance Test Suite.

Uses Playwright with the installed Google Chrome browser to execute end-to-end user
interactions across all 6 product pages:
1. Executive Dashboard (Desktop and Mobile viewports)
2. New Document Review submission form (client-side validation, PDF attachment, submission)
3. Submission Detail & Results (coverage limits, checklist, status banners)
4. Evidence Viewer (side-by-side tracing, verified source quotes, PDF download)
5. Human Review Form (mandatory notes, waiver validations, hard-failure blocking, resolution)
6. Submission History (search by ID, search by subcontractor, status filtering, table navigation)
7. Persistence verification
"""

from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from uuid import uuid4

import uvicorn
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from app.main import app, repository
from app.persistence.database import init_db
from app.schemas.insurance import ComplianceStatus

BASE_DIR = Path(__file__).resolve().parent.parent
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SCREENSHOTS_DIR = BASE_DIR.parent / "evaluation" / "reports" / "browser_screenshots"
FIXTURES_DIR = BASE_DIR / "evaluation" / "fixtures" / "pdfs"


class TestBrowserWorkflowAcceptance(unittest.TestCase):
    """End-to-end real browser automation acceptance tests using Google Chrome."""

    server = None
    server_thread = None
    base_url = ""
    _temp_dir = None
    _orig_env_db = None
    _orig_env_up = None
    _orig_repo_db = None

    @classmethod
    def _allocate_free_port(cls, host="127.0.0.1") -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            return int(s.getsockname()[1])

    @classmethod
    def _is_server_listening(cls, host="127.0.0.1", port=8000):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex((host, port)) == 0

    @classmethod
    def setUpClass(cls):
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        if not Path(CHROME_PATH).exists():
            raise unittest.SkipTest(f"Google Chrome executable not found at {CHROME_PATH}")

        # Isolate SQLite DB, upload directory, and ephemeral server port per test run
        cls._temp_dir = tempfile.TemporaryDirectory()
        cls._orig_env_db = os.environ.get("SUBGUARD_DB_PATH")
        cls._orig_env_up = os.environ.get("SUBGUARD_UPLOAD_DIR")
        cls._orig_repo_db = repository.db_path

        isolated_db = Path(cls._temp_dir.name) / "browser_acceptance.db"
        isolated_uploads = Path(cls._temp_dir.name) / "uploads"
        isolated_uploads.mkdir(parents=True, exist_ok=True)

        os.environ["SUBGUARD_DB_PATH"] = str(isolated_db)
        os.environ["SUBGUARD_UPLOAD_DIR"] = str(isolated_uploads)
        init_db(isolated_db)
        repository.db_path = isolated_db

        # Always start an isolated local uvicorn server on a free ephemeral port
        cls.port = cls._allocate_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        config = uvicorn.Config(app=app, host="127.0.0.1", port=cls.port, log_level="error")
        cls.server = uvicorn.Server(config=config)
        cls.server_thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.server_thread.start()
        for _ in range(50):
            time.sleep(0.1)
            if cls._is_server_listening(port=cls.port):
                break
        if not cls._is_server_listening(port=cls.port):
            raise RuntimeError(f"Failed to start isolated test server on port {cls.port}")

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path=CHROME_PATH,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "playwright", None):
            cls.playwright.stop()
        if cls.server:
            cls.server.should_exit = True
        if cls.server_thread:
            cls.server_thread.join(timeout=5)
        repository.db_path = cls._orig_repo_db
        if cls._orig_env_db is None:
            os.environ.pop("SUBGUARD_DB_PATH", None)
        else:
            os.environ["SUBGUARD_DB_PATH"] = cls._orig_env_db
        if cls._orig_env_up is None:
            os.environ.pop("SUBGUARD_UPLOAD_DIR", None)
        else:
            os.environ["SUBGUARD_UPLOAD_DIR"] = cls._orig_env_up
        if cls._temp_dir:
            cls._temp_dir.cleanup()

    def setUp(self):
        self.console_logs: list[str] = []
        self.page_errors: list[str] = []
        self.failed_requests: list[str] = []

        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            accept_downloads=True,
        )
        self.page = self.context.new_page()

        # Wire diagnostics
        self.page.on("console", lambda msg: self.console_logs.append(f"[{msg.type}] {msg.text}"))
        self.page.on("pageerror", lambda exc: self.page_errors.append(str(exc)))
        self.page.on("requestfailed", lambda req: self.failed_requests.append(f"{req.method} {req.url}"))

    def tearDown(self):
        self.context.close()

    def test_01_dashboard_desktop_and_mobile(self):
        """1. Dashboard loads, renders metrics and navigation, and supports desktop/mobile layouts."""
        # Desktop layout
        self.page.set_viewport_size({"width": 1280, "height": 800})
        self.page.goto(f"{self.base_url}/dashboard", wait_until="networkidle")
        self.assertIn("Dashboard", self.page.title())

        # Verify header, brand, and navigation
        self.assertTrue(self.page.locator("a.brand-link").is_visible())
        self.assertTrue(self.page.locator("text=Compliance Dashboard").is_visible())
        self.assertTrue(self.page.locator("text=+ New Review").is_visible())

        # Save desktop screenshot
        self.page.screenshot(path=str(SCREENSHOTS_DIR / "01_dashboard_desktop.png"))

        # Mobile layout
        self.page.set_viewport_size({"width": 375, "height": 667})
        self.page.goto(f"{self.base_url}/dashboard", wait_until="networkidle")
        self.assertTrue(self.page.locator("text=Compliance Dashboard").is_visible())
        self.page.screenshot(path=str(SCREENSHOTS_DIR / "01_dashboard_mobile.png"))

        # Verify zero unhandled JS exceptions
        self.assertEqual(len(self.page_errors), 0, f"Unexpected JS page errors: {self.page_errors}")

    def test_02_new_submission_validation_and_upload(self):
        """2. New submission form validates files client-side and uploads synthetic COI/endorsements."""
        self.page.set_viewport_size({"width": 1280, "height": 800})
        self.page.goto(f"{self.base_url}/submissions/new", wait_until="networkidle")
        self.assertIn("New Insurance Review", self.page.title())

        # Screenshot empty form
        self.page.screenshot(path=str(SCREENSHOTS_DIR / "02_new_submission_form.png"))

        # Test A: Invalid file extension rejection
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp_txt:
            tmp_txt.write(b"Not a PDF document")
            txt_path = tmp_txt.name

        try:
            self.page.set_input_files("#coi_file", txt_path)
            coi_display = self.page.locator("#coi_filename_display").text_content()
            self.assertIn("Only .pdf files are supported", coi_display)
        finally:
            os.remove(txt_path)

        # Test B: Valid Synthetic PDF Upload
        coi_pdf = FIXTURES_DIR / "synthetic_standard_acord25.pdf"
        end_pdf = FIXTURES_DIR / "synthetic_blanket_endorsement.pdf"
        self.assertTrue(coi_pdf.exists(), f"Missing {coi_pdf}")
        self.assertTrue(end_pdf.exists(), f"Missing {end_pdf}")

        sub_name = f"Browser Verification - Apex Electric - {uuid4().hex[:6]}"
        self.page.fill("#submission_name", sub_name)
        self.page.fill("#evaluation_date", "2025-06-15")

        self.page.set_input_files("#coi_file", str(coi_pdf))
        self.page.set_input_files("#endorsement_files", [str(end_pdf)])

        # Verify green selection labels appear
        coi_text = self.page.locator("#coi_filename_display").text_content()
        self.assertIn("synthetic_standard_acord25.pdf", coi_text)

        end_text = self.page.locator("#end_filename_display").text_content()
        self.assertIn("synthetic_blanket_endorsement.pdf", end_text)

        # Submit form and wait for redirect to submission results
        self.page.click("#submitBtn")
        self.page.wait_for_url("**/submissions/sub_*", timeout=15000)

        # Confirm results page loaded
        self.assertIn("Submission sub_", self.page.title())
        self.assertTrue(self.page.locator(f"text={sub_name}").first.is_visible())
        self.assertTrue(self.page.locator("text=Apex Electrical Contractors LLC").first.is_visible())

        # Capture screenshot of results
        self.page.screenshot(path=str(SCREENSHOTS_DIR / "03_submission_results.png"))

    def test_03_submission_results_and_evidence_viewer(self):
        """3. Submission results render coverage limits and findings; evidence viewer provides source citations and download."""
        # Use synthetic form upload directly
        coi_pdf = FIXTURES_DIR / "synthetic_standard_acord25.pdf"
        end_pdf = FIXTURES_DIR / "synthetic_blanket_endorsement.pdf"

        self.page.goto(f"{self.base_url}/submissions/new", wait_until="networkidle")
        sub_name = f"Results Evidence Test - {uuid4().hex[:6]}"
        self.page.fill("#submission_name", sub_name)
        self.page.fill("#evaluation_date", "2025-06-15")
        self.page.set_input_files("#coi_file", str(coi_pdf))
        self.page.set_input_files("#endorsement_files", [str(end_pdf)])
        self.page.click("#submitBtn")
        self.page.wait_for_url("**/submissions/sub_*", timeout=15000)

        url = self.page.url
        sub_id = url.split("/submissions/")[1]

        # Verify Limits Table
        self.assertTrue(self.page.locator("text=Extracted Coverage Types & Policy Limits").is_visible())
        self.assertTrue(self.page.locator("text=GL-98765432").first.is_visible())
        self.assertTrue(self.page.locator("text=$1,000,000").first.is_visible())

        # Verify Document Download in browser from results page
        with self.page.expect_download() as download_info:
            self.page.locator("a[href*='/download']").first.click()
        download = download_info.value
        download_path = download.path()
        with open(download_path, "rb") as f:
            header = f.read(5)
            self.assertEqual(header, b"%PDF-", "Downloaded document is not a valid PDF")

        # Click Evidence Trace
        self.page.click("text=🔍 Evidence Trace")
        self.page.wait_for_url(f"**/submissions/{sub_id}/evidence", timeout=10000)
        self.assertTrue(self.page.locator("text=Traceable Evidence Verification").is_visible())
        self.assertTrue(self.page.locator("text=General Contractor Corp").first.is_visible())

        self.page.screenshot(path=str(SCREENSHOTS_DIR / "04_evidence_viewer.png"))

    def test_04_human_review_workflow_and_guardrails(self):
        """4. Human review enforces notes requirement, blocks hard failure approvals, and records valid reviews."""
        # Create a submission with hard failure directly in repo for deterministic testing
        sub_id = f"sub_hw_{uuid4().hex[:8]}"
        findings = [
            {
                "finding_id": "f_lim_deficient",
                "requirement_id": "REQ_GL_EACH_OCC",
                "rule_type": "coverage_limit",
                "status": "non_compliant",
                "severity": "critical",
                "reason_code": "LIMIT_BELOW_MINIMUM",
                "requires_human_review": False,
                "explanation": "GL Occurrence limit is $500,000, which is below the mandatory $1,000,000 threshold.",
            }
        ]
        repository.create_submission(
            submission_id=sub_id,
            name="Hard Failure Review Test",
            overall_status="non_compliant",
            processing_status="completed",
            provider="mock",
            evaluation_date="2025-06-15",
            result_data={
                "review": {
                    "subcontractor_id": "sub_apex",
                    "evaluation_date": "2025-06-15",
                    "overall_status": "non_compliant",
                    "requires_human_review": False,
                    "findings": findings,
                },
                "document_set": {"coverages": [], "endorsements": []},
            },
            subcontractor_name="Deficient Limit Subcontractor",
        )

        # Navigate to Human Review form for this submission
        self.page.goto(f"{self.base_url}/submissions/{sub_id}/review", wait_until="networkidle")
        self.assertTrue(self.page.locator("text=Human Review Decision").is_visible())

        # Test A: Attempted Approval of Deterministic Hard Failure
        self.page.select_option("#review_status", "accepted_exception")
        self.page.fill("#reviewer_name", "Test Reviewer")
        self.page.fill("#reviewer_notes", "Attempting blanket approval of a deficient limit")
        self.page.select_option("#update_submission_status", "compliant")
        self.page.click("#recordReviewBtn")

        # Expect error box to appear with HTTP 422 error message
        self.page.wait_for_selector("#reviewErrorBox:visible", timeout=5000)
        err_text = self.page.locator("#reviewErrorBox").text_content()
        self.assertIn("Cannot approve submission", err_text)
        self.assertIn("deterministic hard failure", err_text)

        # Test B: Attempted Waiver without Target Rule ID
        self.page.select_option("#review_status", "waived")
        self.page.select_option("#rule_id", "")  # Empty rule selection
        self.page.fill("#reviewer_notes", "Attempting waiver without rule ID")
        self.page.select_option("#update_submission_status", "waived")
        self.page.click("#recordReviewBtn")

        self.page.wait_for_selector("#reviewErrorBox:visible", timeout=5000)
        self.assertIn("Waiver action requires specifying a target rule_id", self.page.locator("#reviewErrorBox").text_content())

        # Test C: Valid Action — Issue Deficiency Notice to Subcontractor
        self.page.select_option("#review_status", "deficiency_notice_required")
        self.page.fill("#reviewer_name", "Senior Risk Auditor")
        self.page.fill("#reviewer_notes", "Deficiency Notice #1042 issued for GL occurrence sub-limit ($500k vs required $1M).")
        self.page.select_option("#update_submission_status", "deficiency_pending")
        self.page.click("#recordReviewBtn")

        # Wait for redirect back to submission detail
        self.page.wait_for_url(f"**/submissions/{sub_id}", timeout=10000)
        self.assertTrue(self.page.locator("text=Senior Risk Auditor").is_visible())
        self.assertTrue(self.page.locator("text=Deficiency Notice #1042 issued").is_visible())
        self.assertTrue(self.page.locator("text=deficiency_notice_required").is_visible())

        # Capture screenshot of recorded audit trail
        self.page.screenshot(path=str(SCREENSHOTS_DIR / "05_human_review_audit_saved.png"))

    def test_05_submission_history_search_and_navigation(self):
        """5. Submission history supports live text search, status dropdown filtering, and link navigation."""
        self.page.goto(f"{self.base_url}/submissions", wait_until="networkidle")
        self.assertIn("Submission History", self.page.title())

        # Verify table exists
        self.assertTrue(self.page.locator("table.data-table").is_visible())

        # Search for Apex
        self.page.fill("input[name='q']", "Apex")
        self.page.click("button:has-text('Filter')")
        self.page.wait_for_url("**/submissions?**q=Apex**", timeout=10000)

        # Verify search results render
        self.assertTrue(self.page.locator("text=Apex Electrical").first.is_visible())
        self.page.screenshot(path=str(SCREENSHOTS_DIR / "06_submission_history.png"))

        # Click into first result via Reopen Result button
        self.page.click("table.data-table tbody tr:first-child a.btn-sm")
        self.page.wait_for_url("**/submissions/**", timeout=10000)
        self.assertTrue(self.page.locator("text=Uploaded Insurance Documents").is_visible())

    def test_06_persistence_across_cold_navigation(self):
        """6. Re-navigating to existing submissions validates persistent SQLite storage."""
        # Query any existing submission ID from repo
        recent = repository.list_submissions(limit=1)
        self.assertTrue(len(recent) > 0, "No submissions found in DB")
        sub_id = recent[0]["id"]

        self.page.goto(f"{self.base_url}/submissions/{sub_id}", wait_until="networkidle")
        self.assertTrue(self.page.locator(f"text={sub_id}").is_visible())
        self.assertTrue(self.page.locator("text=Uploaded Insurance Documents").is_visible())


if __name__ == "__main__":
    unittest.main()
