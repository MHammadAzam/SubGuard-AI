"""Focused regression tests for field-level coverage evidence attribution.

Covers:
- Test 1: GL Each Occurrence ($1,000,000) cites occurrence source text and is verified.
- Test 2: GL General Aggregate ($2,000,000) cites aggregate source text (not occurrence).
- Test 3: GL Policy Expiration (2026-01-01) cites policy-period/expiration text (not occurrence).
- Test 4: Missing aggregate evidence never inherits occurrence evidence; mandatory_evidence escalates.
- Test 5: Missing date evidence never inherits unrelated limit evidence; mandatory_evidence escalates.
- Test 6: Evidence Trace UI HTML and API JSON findings agree on field-specific citations.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import List, Optional

import pymupdf
from fastapi.testclient import TestClient

from app.compliance.evaluator import ComplianceEvaluator
from app.documents.processor import PDFProcessor
from app.extraction.pipeline import ExtractionPipelineService
from app.extraction.providers.base import BaseExtractionProvider
from app.extraction.providers.mock import HonestApplicationMockProvider
from app.schemas.insurance import (
    ComplianceStatus,
    CoverageType,
    DocumentType,
    InsuranceRequirement,
    LimitField,
    RequirementType,
    Severity,
)
from app.schemas.pdf import PDFPageInspection
from app.schemas.raw_extraction import (
    RawCoverageSection,
    RawEntityExtraction,
    RawEvidenceQuote,
    RawInsuranceExtraction,
)


def _make_pdf_bytes(text: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((50, 72), text, fontsize=11)
    raw = doc.tobytes()
    doc.close()
    return raw


class _ScriptedRawProvider(BaseExtractionProvider):
    def __init__(self, extraction: RawInsuranceExtraction):
        self._extraction = extraction

    @property
    def provider_name(self) -> str:
        return "scripted-field-test"

    def extract_insurance_data(
        self,
        document_id: str,
        pages: List[PDFPageInspection],
        timeout_seconds: float = 30.0,
    ) -> RawInsuranceExtraction:
        return self._extraction


STANDARD_COI_TEXT = (
    "ACORD 25 (2016/03) CERTIFICATE OF LIABILITY INSURANCE\n"
    "INSURED: Apex Electrical Contractors LLC\n"
    "COVERAGES - COMMERCIAL GENERAL LIABILITY\n"
    "POLICY NUMBER: GL-98765432\n"
    "POLICY EFF: 01/01/2025   POLICY EXP: 01/01/2026\n"
    "EACH OCCURRENCE: $1,000,000\n"
    "GENERAL AGGREGATE: $2,000,000\n"
    "CERTIFICATE HOLDER: General Contractor Corp\n"
)


class TestFieldLevelEvidenceAttribution(unittest.TestCase):
    """Verify field-specific provenance from extraction through evaluator and Evidence Trace UI."""

    def setUp(self):
        self.processor = PDFProcessor(render_dpi=72)
        self.evaluator = ComplianceEvaluator()
        self.eval_date = date(2025, 6, 15)

    def _run_honest_mock_on_coi(self, mandatory_evidence: bool = False):
        pdf_bytes = _make_pdf_bytes(STANDARD_COI_TEXT)
        insp = self.processor.process_pdf(pdf_bytes, document_id="field_coi_1")
        pipe_res = ExtractionPipelineService(HonestApplicationMockProvider()).process_inspection(
            insp, subcontractor_id="sub_apex"
        )
        self.assertIsNotNone(pipe_res.document_set)
        reqs = [
            InsuranceRequirement(
                requirement_id="REQ_GL_EACH_OCC",
                rule_type=RequirementType.COVERAGE_LIMIT,
                description="GL Each Occurrence >= $1,000,000",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                limit_field=LimitField.EACH_OCCURRENCE,
                min_amount_cents=100_000_000,
                mandatory_evidence=mandatory_evidence,
                severity=Severity.CRITICAL,
            ),
            InsuranceRequirement(
                requirement_id="REQ_GL_GEN_AGG",
                rule_type=RequirementType.COVERAGE_LIMIT,
                description="GL General Aggregate >= $2,000,000",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                limit_field=LimitField.GENERAL_AGGREGATE,
                min_amount_cents=200_000_000,
                mandatory_evidence=mandatory_evidence,
                severity=Severity.MAJOR,
            ),
            InsuranceRequirement(
                requirement_id="REQ_GL_EXPIRATION",
                rule_type=RequirementType.POLICY_EXPIRATION,
                description="GL policy active",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                mandatory_evidence=mandatory_evidence,
                severity=Severity.CRITICAL,
            ),
        ]
        review = self.evaluator.evaluate(pipe_res.document_set, reqs, self.eval_date)
        return pipe_res.document_set, review

    def test_1_occurrence_limit_cites_occurrence_evidence(self):
        """Test 1: GL Each Occurrence ($1,000,000) cites and verifies occurrence source text."""
        doc_set, review = self._run_honest_mock_on_coi()
        gl = next(c for c in doc_set.coverages if c.coverage_type == CoverageType.GENERAL_LIABILITY)
        self.assertEqual(gl.each_occurrence.amount_cents, 100_000_000)
        self.assertIsNotNone(gl.each_occurrence.evidence)
        self.assertEqual(gl.each_occurrence.evidence.source_text, "EACH OCCURRENCE: $1,000,000")
        self.assertEqual(gl.each_occurrence.evidence.page_number, 1)

        f_occ = next(f for f in review.findings if f.requirement_id == "REQ_GL_EACH_OCC")
        self.assertEqual(f_occ.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(len(f_occ.evidence), 1)
        self.assertEqual(f_occ.evidence[0].source_text, "EACH OCCURRENCE: $1,000,000")

    def test_2_general_aggregate_cites_aggregate_evidence_not_occurrence(self):
        """Test 2: GL General Aggregate ($2,000,000) cites aggregate text, never occurrence text."""
        doc_set, review = self._run_honest_mock_on_coi()
        gl = next(c for c in doc_set.coverages if c.coverage_type == CoverageType.GENERAL_LIABILITY)
        self.assertEqual(gl.general_aggregate.amount_cents, 200_000_000)
        self.assertIsNotNone(gl.general_aggregate.evidence)
        self.assertEqual(gl.general_aggregate.evidence.source_text, "GENERAL AGGREGATE: $2,000,000")
        self.assertNotIn("EACH OCCURRENCE", gl.general_aggregate.evidence.source_text)

        f_agg = next(f for f in review.findings if f.requirement_id == "REQ_GL_GEN_AGG")
        self.assertEqual(f_agg.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(len(f_agg.evidence), 1)
        self.assertEqual(f_agg.evidence[0].source_text, "GENERAL AGGREGATE: $2,000,000")
        self.assertNotIn("EACH OCCURRENCE", f_agg.evidence[0].source_text)

    def test_3_policy_expiration_cites_date_evidence_not_occurrence(self):
        """Test 3: GL Policy Expiration (2026-01-01) cites policy-period/expiration text, never occurrence."""
        doc_set, review = self._run_honest_mock_on_coi()
        gl = next(c for c in doc_set.coverages if c.coverage_type == CoverageType.GENERAL_LIABILITY)
        self.assertEqual(gl.expiration_date.date, date(2026, 1, 1))
        self.assertIsNotNone(gl.expiration_date.evidence)
        self.assertEqual(
            gl.expiration_date.evidence.source_text,
            "POLICY EFF: 01/01/2025   POLICY EXP: 01/01/2026",
        )
        self.assertNotIn("EACH OCCURRENCE", gl.expiration_date.evidence.source_text)

        f_exp = next(f for f in review.findings if f.requirement_id == "REQ_GL_EXPIRATION")
        self.assertEqual(f_exp.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(f_exp.reason_code, "POLICY_ACTIVE_AND_CURRENT")
        self.assertEqual(len(f_exp.evidence), 1)
        self.assertEqual(f_exp.evidence[0].source_text, "POLICY EFF: 01/01/2025   POLICY EXP: 01/01/2026")
        self.assertNotIn("EACH OCCURRENCE", f_exp.evidence[0].source_text)

    def test_4_missing_aggregate_evidence_does_not_attach_occurrence_evidence(self):
        """Test 4: Aggregate limit without aggregate source text never inherits occurrence evidence."""
        pdf_bytes = _make_pdf_bytes(
            "ACORD 25\nINSURED: Apex Electrical Contractors LLC\n"
            "COMMERCIAL GENERAL LIABILITY\nEACH OCCURRENCE: $1,000,000\n"
        )
        insp = self.processor.process_pdf(pdf_bytes, document_id="missing_agg_ev")
        scripted = RawInsuranceExtraction(
            document_type="coi",
            coverages=[
                RawCoverageSection(
                    coverage_type="general_liability",
                    each_occurrence="$1,000,000",
                    general_aggregate="$2,000,000",
                    each_occurrence_evidence=RawEvidenceQuote(page_number=1, source_text="EACH OCCURRENCE: $1,000,000"),
                    general_aggregate_evidence=None,
                    evidence=RawEvidenceQuote(page_number=1, source_text="EACH OCCURRENCE: $1,000,000"),
                    confidence=0.95,
                )
            ],
        )
        pipe_res = ExtractionPipelineService(_ScriptedRawProvider(scripted)).process_inspection(insp, "sub_t4")
        self.assertIsNotNone(pipe_res.document_set)
        gl = pipe_res.document_set.coverages[0]
        self.assertEqual(gl.general_aggregate.amount_cents, 200_000_000)
        self.assertIsNone(gl.general_aggregate.evidence)

        req_no_mand = InsuranceRequirement(
            requirement_id="REQ_GL_GEN_AGG",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="GL Gen Agg >= $2M",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            limit_field=LimitField.GENERAL_AGGREGATE,
            min_amount_cents=200_000_000,
            mandatory_evidence=False,
        )
        res_no_mand = self.evaluator.evaluate(pipe_res.document_set, [req_no_mand], self.eval_date)
        self.assertEqual(res_no_mand.findings[0].evidence, [])

        req_mand = req_no_mand.model_copy(update={"mandatory_evidence": True})
        res_mand = self.evaluator.evaluate(pipe_res.document_set, [req_mand], self.eval_date)
        self.assertEqual(res_mand.findings[0].status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(res_mand.findings[0].reason_code, "LIMIT_EVIDENCE_MISSING")
        self.assertTrue(res_mand.findings[0].requires_human_review)
        self.assertEqual(res_mand.findings[0].evidence, [])

    def test_5_missing_date_evidence_does_not_attach_limit_evidence(self):
        """Test 5: Expiration date without date source text never inherits unrelated limit evidence."""
        pdf_bytes = _make_pdf_bytes(
            "ACORD 25\nINSURED: Apex Electrical Contractors LLC\n"
            "COMMERCIAL GENERAL LIABILITY\nEACH OCCURRENCE: $1,000,000\n"
        )
        insp = self.processor.process_pdf(pdf_bytes, document_id="missing_date_ev")
        scripted = RawInsuranceExtraction(
            document_type="coi",
            coverages=[
                RawCoverageSection(
                    coverage_type="general_liability",
                    each_occurrence="$1,000,000",
                    expiration_date="01/01/2026",
                    each_occurrence_evidence=RawEvidenceQuote(page_number=1, source_text="EACH OCCURRENCE: $1,000,000"),
                    expiration_date_evidence=None,
                    evidence=RawEvidenceQuote(page_number=1, source_text="EACH OCCURRENCE: $1,000,000"),
                    confidence=0.95,
                )
            ],
        )
        pipe_res = ExtractionPipelineService(_ScriptedRawProvider(scripted)).process_inspection(insp, "sub_t5")
        self.assertIsNotNone(pipe_res.document_set)
        gl = pipe_res.document_set.coverages[0]
        self.assertEqual(gl.expiration_date.date, date(2026, 1, 1))
        self.assertIsNone(gl.expiration_date.evidence)

        req_no_mand = InsuranceRequirement(
            requirement_id="REQ_GL_EXP",
            rule_type=RequirementType.POLICY_EXPIRATION,
            description="GL policy active",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            mandatory_evidence=False,
        )
        res_no_mand = self.evaluator.evaluate(pipe_res.document_set, [req_no_mand], self.eval_date)
        self.assertEqual(res_no_mand.findings[0].evidence, [])

        req_mand = req_no_mand.model_copy(update={"mandatory_evidence": True})
        res_mand = self.evaluator.evaluate(pipe_res.document_set, [req_mand], self.eval_date)
        self.assertEqual(res_mand.findings[0].status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(res_mand.findings[0].reason_code, "EXPIRATION_DATE_EVIDENCE_MISSING")
        self.assertTrue(res_mand.findings[0].requires_human_review)
        self.assertEqual(res_mand.findings[0].evidence, [])

    def test_6_evidence_trace_ui_and_api_agree_on_field_citations(self):
        """Test 6: API findings and rendered Evidence Trace UI show identical field-specific citations."""
        from app.main import app, repository
        from app.persistence.database import init_db
        from app.persistence.repository import SubmissionRepository

        with tempfile.TemporaryDirectory() as td:
            orig_db = repository.db_path
            orig_env_db = os.environ.get("SUBGUARD_DB_PATH")
            orig_env_up = os.environ.get("SUBGUARD_UPLOAD_DIR")
            try:
                demo_db = Path(td) / "field_test.db"
                demo_up = Path(td) / "uploads"
                os.environ["SUBGUARD_DB_PATH"] = str(demo_db)
                os.environ["SUBGUARD_UPLOAD_DIR"] = str(demo_up)
                init_db(demo_db)
                repository.db_path = demo_db

                from tests.fixtures.auth_helpers import authenticate_session_client

                client = TestClient(app)
                authenticate_session_client(client, db_path=demo_db, role="analyst")
                pdf_bytes = _make_pdf_bytes(STANDARD_COI_TEXT)
                reqs = [
                    {
                        "requirement_id": "REQ_NAMED_INSURED",
                        "rule_type": "named_insured",
                        "description": "Named Insured",
                        "severity": "critical",
                        "required_entity_name": "Apex Electrical Contractors LLC",
                    },
                    {
                        "requirement_id": "REQ_GL_EACH_OCC",
                        "rule_type": "coverage_limit",
                        "description": "GL Occ >= $1M",
                        "severity": "critical",
                        "coverage_type": "general_liability",
                        "limit_field": "each_occurrence",
                        "min_amount_cents": 100_000_000,
                    },
                    {
                        "requirement_id": "REQ_GL_GEN_AGG",
                        "rule_type": "coverage_limit",
                        "description": "GL Agg >= $2M",
                        "severity": "major",
                        "coverage_type": "general_liability",
                        "limit_field": "general_aggregate",
                        "min_amount_cents": 200_000_000,
                    },
                    {
                        "requirement_id": "REQ_GL_EXPIRATION",
                        "rule_type": "policy_expiration",
                        "description": "GL active",
                        "severity": "critical",
                        "coverage_type": "general_liability",
                    },
                ]
                res = client.post(
                    "/api/v1/compliance/review-documents",
                    files={"coi_file": ("field_coi.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
                    data={
                        "submission_name": "Test 6 Field Evidence Consistency",
                        "evaluation_date": "2025-06-15",
                        "requirements": json.dumps(reqs),
                    },
                )
                self.assertEqual(res.status_code, 200)
                body = res.json()
                self.assertEqual(body["overall_status"], "compliant")
                sub_id = body["submission_id"]

                findings_by_id = {f["requirement_id"]: f for f in body["review_result"]["findings"]}
                self.assertEqual(
                    findings_by_id["REQ_GL_EACH_OCC"]["evidence"][0]["source_text"],
                    "EACH OCCURRENCE: $1,000,000",
                )
                self.assertEqual(
                    findings_by_id["REQ_GL_GEN_AGG"]["evidence"][0]["source_text"],
                    "GENERAL AGGREGATE: $2,000,000",
                )
                self.assertEqual(
                    findings_by_id["REQ_GL_EXPIRATION"]["evidence"][0]["source_text"],
                    "POLICY EFF: 01/01/2025   POLICY EXP: 01/01/2026",
                )

                ev_html = client.get(f"/submissions/{sub_id}/evidence")
                self.assertEqual(ev_html.status_code, 200)
                html = ev_html.text
                self.assertIn("Each Occurrence", html)
                self.assertIn("EACH OCCURRENCE: $1,000,000", html)
                self.assertIn("General Aggregate", html)
                self.assertIn("GENERAL AGGREGATE: $2,000,000", html)
                self.assertIn("Expiration Date", html)
                self.assertIn("POLICY EFF: 01/01/2025   POLICY EXP: 01/01/2026", html)
            finally:
                repository.db_path = orig_db
                if orig_env_db is None:
                    os.environ.pop("SUBGUARD_DB_PATH", None)
                else:
                    os.environ["SUBGUARD_DB_PATH"] = orig_env_db
                if orig_env_up is None:
                    os.environ.pop("SUBGUARD_UPLOAD_DIR", None)
                else:
                    os.environ["SUBGUARD_UPLOAD_DIR"] = orig_env_up


if __name__ == "__main__":
    unittest.main()
