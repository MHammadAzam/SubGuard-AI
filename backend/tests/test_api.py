"""Integration tests for the minimal FastAPI compliance endpoint.

Tests the FastAPI endpoint directly via ASGI invocation or function call,
ensuring no reliance on uninstalled optional dependencies like httpx.
"""

import unittest
from datetime import date

from app.main import evaluate_compliance, EvaluationRequest, health_check
from app.schemas.insurance import ComplianceStatus
from tests.fixtures.sample_data import (
    EVAL_DATE,
    sample_gl_coverage,
    sample_gl_requirement,
    sample_subcontractor_document_set,
)


class TestComplianceAPI(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
