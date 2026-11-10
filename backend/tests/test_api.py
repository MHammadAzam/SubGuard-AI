"""Integration tests for the minimal FastAPI compliance endpoint.

Tests both:
1. Direct Python function invocations (preserving baseline unit testing)
2. Actual HTTP requests via FastAPI TestClient verifying HTTP status codes,
   serialization, and validation boundaries.
"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app, evaluate_compliance, EvaluationRequest, health_check, repository
from app.persistence.database import init_db
from app.schemas.insurance import ComplianceStatus
from tests.fixtures.auth_helpers import authenticate_session_client
from tests.fixtures.sample_data import (
    EVAL_DATE,
    sample_gl_coverage,
    sample_gl_requirement,
    sample_subcontractor_document_set,
)


class TestComplianceAPI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "api_test.db"
        self._orig_repo_db = repository.db_path
        init_db(self.db_path)
        repository.db_path = self.db_path
        self.client = TestClient(app)
        authenticate_session_client(self.client, db_path=self.db_path, role="analyst")

    def tearDown(self):
        repository.db_path = self._orig_repo_db
        self.temp_dir.cleanup()

    # =========================================================================
    # Direct Function Invocations
    # =========================================================================

    def test_health_check(self):
        res = health_check()
        self.assertEqual(res, {"status": "ok", "service": "compliance-evaluator"})

    def test_api_evaluate_compliant(self):
        req = sample_gl_requirement(min_cents=100_000_000)
        cov = sample_gl_coverage(occ_cents=100_000_000)
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        request_payload = EvaluationRequest(
            document_set=doc_set,
            requirements=[req],
            evaluation_date=EVAL_DATE,
        )

        result = evaluate_compliance(request_payload)

        self.assertEqual(result.overall_status, ComplianceStatus.COMPLIANT)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].status, ComplianceStatus.COMPLIANT)
        self.assertEqual(result.findings[0].reason_code, "LIMIT_MEETS_OR_EXCEEDS_MINIMUM")

    def test_api_evaluate_non_compliant(self):
        req = sample_gl_requirement(min_cents=100_000_000)
        cov = sample_gl_coverage(occ_cents=50_000_000)  # Below min
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        request_payload = EvaluationRequest(
            document_set=doc_set,
            requirements=[req],
            evaluation_date=EVAL_DATE,
        )

        result = evaluate_compliance(request_payload)

        self.assertEqual(result.overall_status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(result.findings[0].status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(result.findings[0].reason_code, "LIMIT_BELOW_MINIMUM")

    # =========================================================================
    # HTTP TestClient Integration Tests
    # =========================================================================

    def test_http_health_check_200(self):
        """GET /health returns HTTP 200 with service status payload."""
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "service": "compliance-evaluator"})

    def test_http_evaluate_compliant_200(self):
        """Valid compliant request over HTTP returns HTTP 200 and COMPLIANT status."""
        req = sample_gl_requirement(min_cents=100_000_000)
        cov = sample_gl_coverage(occ_cents=100_000_000)
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        request_obj = EvaluationRequest(
            document_set=doc_set,
            requirements=[req],
            evaluation_date=EVAL_DATE,
        )
        payload = request_obj.model_dump(mode="json")

        response = self.client.post("/api/v1/compliance/evaluate", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["overall_status"], "compliant")
        self.assertFalse(data["requires_human_review"])
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["status"], "compliant")
        self.assertEqual(data["findings"][0]["reason_code"], "LIMIT_MEETS_OR_EXCEEDS_MINIMUM")

    def test_http_evaluate_non_compliant_200(self):
        """Valid non-compliant request over HTTP returns HTTP 200 and NON_COMPLIANT status."""
        req = sample_gl_requirement(min_cents=100_000_000)
        cov = sample_gl_coverage(occ_cents=50_000_000)
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        request_obj = EvaluationRequest(
            document_set=doc_set,
            requirements=[req],
            evaluation_date=EVAL_DATE,
        )
        payload = request_obj.model_dump(mode="json")

        response = self.client.post("/api/v1/compliance/evaluate", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["overall_status"], "non_compliant")
        self.assertTrue(data["requires_human_review"])
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["status"], "non_compliant")
        self.assertEqual(data["findings"][0]["reason_code"], "LIMIT_BELOW_MINIMUM")

    def test_http_evaluate_missing_required_request_fields_422(self):
        """Payload missing required fields (e.g. missing document_set) returns HTTP 422."""
        invalid_payload = {
            "requirements": [sample_gl_requirement().model_dump(mode="json")],
            "evaluation_date": "2025-06-15",
            # document_set is missing!
        }
        response = self.client.post("/api/v1/compliance/evaluate", json=invalid_payload)
        self.assertEqual(response.status_code, 422)
        err = response.json()
        self.assertIn("detail", err)

    def test_http_evaluate_empty_requirements_returns_422(self):
        """Empty requirements list ([]) violates min_length=1 and returns HTTP 422."""
        doc_set = sample_subcontractor_document_set()
        invalid_payload = {
            "document_set": doc_set.model_dump(mode="json"),
            "requirements": [],  # Violates min_length=1
            "evaluation_date": "2025-06-15",
        }
        response = self.client.post("/api/v1/compliance/evaluate", json=invalid_payload)
        self.assertEqual(response.status_code, 422)

    def test_http_evaluate_invalid_date_format_returns_422(self):
        """Invalid date string for evaluation_date returns HTTP 422."""
        doc_set = sample_subcontractor_document_set()
        req = sample_gl_requirement()
        invalid_payload = {
            "document_set": doc_set.model_dump(mode="json"),
            "requirements": [req.model_dump(mode="json")],
            "evaluation_date": "invalid-not-a-date",
        }
        response = self.client.post("/api/v1/compliance/evaluate", json=invalid_payload)
        self.assertEqual(response.status_code, 422)

    def test_http_evaluate_unsupported_method_returns_405(self):
        """Calling GET or PUT on /api/v1/compliance/evaluate returns HTTP 405 Method Not Allowed."""
        get_response = self.client.get("/api/v1/compliance/evaluate")
        self.assertEqual(get_response.status_code, 405)

        put_response = self.client.put("/api/v1/compliance/evaluate", json={})
        self.assertEqual(put_response.status_code, 405)


if __name__ == "__main__":
    unittest.main()
