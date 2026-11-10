"""Milestone 3.5D — WP4: Application Pipeline Integration Tests.

Verifies end-to-end production integration connecting WP1 (evidence integrity),
WP2 (document relationship target linkage), and WP3 (contractual duration enforcement)
into:
- ComplianceEvaluator.evaluate()
- DocumentReviewService.process_and_review()
- FastAPI HTTP endpoints (/api/v1/compliance/evaluate, /api/v1/compliance/review-documents,
  /api/v1/submissions/{id}, /api/v1/submissions/{id}/review)
- SQLite persistence and human-review audit records.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pymupdf
from fastapi import UploadFile
from fastapi.testclient import TestClient

from app.compliance.evaluator import ComplianceEvaluator
from app.compliance.reconciliation import (
    DeterministicReconciliationEngine,
    classify_document_relationship,
    reconcile_dates,
    reconcile_entity_identities,
    reconcile_extracted_document_set,
)
from app.documents.review_service import DocumentReviewService
from app.main import app, repository
from app.persistence.database import init_db
from app.persistence.repository import SubmissionRepository
from app.schemas.insurance import (
    ComplianceStatus,
    CoverageExtraction,
    CoverageType,
    DateValue,
    DocumentType,
    EndorsementExtraction,
    EndorsementType,
    EntityInfo,
    ExtractedDocumentSet,
    InsuranceRequirement,
    LimitField,
    RequirementType,
    Severity,
)
from app.schemas.reconciliation import (
    DiscrepancyCategory,
    DocumentDiscrepancy,
    DocumentManifestEntry,
    DocumentRelationshipType,
    EdgeClassification,
    EntityRole,
)
from tests.fixtures.sample_data import (
    EVAL_DATE,
    make_evidence,
    sample_gl_coverage,
    sample_gl_requirement,
    sample_subcontractor_document_set,
)


def _make_pdf_bytes(lines: list[str]) -> bytes:
    """Create a valid in-memory PDF containing the supplied text lines."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 18
    data = doc.tobytes()
    doc.close()
    return data


def _make_upload_file(filename: str, content: bytes) -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(content))


class TestWP4PipelineIntegration(unittest.TestCase):
    """Production pipeline, API, and human-review integration tests for WP4."""

    def setUp(self):
        self.evaluator = ComplianceEvaluator()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / f"wp4_{uuid4().hex[:8]}.db"
        self.upload_dir = Path(self.temp_dir.name) / "uploads"
        self._orig_repo_db = repository.db_path
        self.orig_upload_env = os.environ.get("SUBGUARD_UPLOAD_DIR")
        os.environ["SUBGUARD_UPLOAD_DIR"] = str(self.upload_dir)
        init_db(self.db_path)
        repository.db_path = self.db_path
        self.repo = SubmissionRepository(db_path=self.db_path)
        self.client = TestClient(app)
        from tests.fixtures.auth_helpers import authenticate_session_client

        authenticate_session_client(
            self.client,
            db_path=self.db_path,
            username="wp4_reviewer",
            display_name="Senior Compliance Analyst",
            role="reviewer",
        )

        self.ref_coi_dates = make_evidence(
            doc_id="doc_coi_01",
            doc_type=DocumentType.COI,
            page=1,
            text="GL EACH OCCURRENCE $1,000,000 Policy Period: Effective 01/01/2025 Expiration Date: 12/31/2026",
        )
        self.ref_end_dates = make_evidence(
            doc_id="doc_end_01",
            doc_type=DocumentType.ENDORSEMENT,
            page=1,
            text="Effective Date: 01/01/2025 Expiration Date: 12/31/2026 Form CG 20 10 04 13 for Policy GL-987654-01",
        )

    def tearDown(self):
        repository.db_path = self._orig_repo_db
        if self.orig_upload_env is None:
            os.environ.pop("SUBGUARD_UPLOAD_DIR", None)
        else:
            os.environ["SUBGUARD_UPLOAD_DIR"] = self.orig_upload_env
        self.temp_dir.cleanup()

    def test_wp4_01_normal_compliant_packet_no_artificial_discrepancies(self):
        """1. Normal compliant packet passes through /api/v1/compliance/evaluate without artificial discrepancies."""
        cov = sample_gl_coverage(exp=date(2026, 12, 31))
        cov.evidence = self.ref_coi_dates
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            applicable_policy="GL-987654-01",
            effective_date=date(2025, 1, 1),
            expiration_date=date(2026, 12, 31),
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            independent_integrity_verified=True,
            evidence=self.ref_end_dates,
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])
        reqs = [
            sample_gl_requirement(min_cents=100_000_000),
            InsuranceRequirement(
                requirement_id="REQ_GL_EXP",
                rule_type=RequirementType.POLICY_EXPIRATION,
                description="GL unexpired",
                coverage_type=CoverageType.GENERAL_LIABILITY,
            ),
            InsuranceRequirement(
                requirement_id="REQ_NI",
                rule_type=RequirementType.NAMED_INSURED,
                description="Named Insured match",
                required_entity_name="Apex Electrical Contractors LLC",
            ),
            InsuranceRequirement(
                requirement_id="REQ_AI_DUR",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE,
                description="AI through 12/31/2026",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                required_coverage_end_date=date(2026, 12, 31),
                is_duration_requirement_validated=True,
            ),
        ]

        res = self.client.post(
            "/api/v1/compliance/evaluate",
            json={
                "document_set": doc_set.model_dump(mode="json"),
                "requirements": [r.model_dump(mode="json") for r in reqs],
                "evaluation_date": EVAL_DATE.isoformat(),
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["overall_status"], "compliant")
        self.assertFalse(data["requires_human_review"])
        self.assertIsNotNone(data["reconciliation_summary"])
        self.assertEqual(data["reconciliation_summary"]["discrepancies_count"], 0)
        self.assertEqual(data["reconciliation_summary"]["reason_codes"], [])
        self.assertEqual(len(data["findings"]), 4)

    def test_wp4_02_confirmed_reconciliation_violation_affects_final_result_and_blocks_approval(self):
        """2. Confirmed reconciliation violation overrides compliant limits and blocks blanket review approval."""
        cov = sample_gl_coverage(exp=date(2026, 12, 31))
        cov.evidence = self.ref_coi_dates
        ref_end_short = make_evidence(
            doc_id="doc_end_01",
            doc_type=DocumentType.ENDORSEMENT,
            page=1,
            text="Effective Date: 01/01/2025 Expiration Date: 06/30/2026 Form CG 20 10",
        )

        # Build explicit CrossDocumentReconciliationResult with a confirmed contractual violation
        manifest = (
            DocumentManifestEntry(
                document_id="doc_coi_01",
                sha256_hash=hashlib.sha256(b"coi").hexdigest(),
                page_count=1,
                detected_document_type=DocumentType.COI,
            ),
            DocumentManifestEntry(
                document_id="doc_end_01",
                sha256_hash=hashlib.sha256(b"end").hexdigest(),
                page_count=1,
                detected_document_type=DocumentType.ENDORSEMENT,
            ),
        )
        drec = reconcile_dates(
            policy_document_id="doc_coi_01",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 12, 31),
            policy_date_evidence=(self.ref_coi_dates,),
            endorsement_document_id="doc_end_01",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2026, 6, 30),
            endorsement_date_evidence=(ref_end_short,),
            evaluation_date=EVAL_DATE,
            evidence_chain=(self.ref_coi_dates, ref_end_short),
            contractual_duration_requirement_id="REQ_CONTRACT_SPAN",
            required_coverage_end_date=date(2026, 12, 31),
            is_duration_requirement_validated=True,
        )
        disc = DocumentDiscrepancy(
            discrepancy_id="disc_wp4_02",
            category=DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION,
            severity=Severity.CRITICAL,
            reason_code="PREMATURE_EXPIRATION_VIOLATION",
            description="Endorsement expires 06/30/2026 prior to contractual deadline 12/31/2026.",
            source_document_id="doc_end_01",
            conflicting_document_id="doc_coi_01",
            applicable_requirement_id="REQ_CONTRACT_SPAN",
            applicable_coverage_type=CoverageType.GENERAL_LIABILITY,
            evidence_chain=(self.ref_coi_dates, ref_end_short),
            requires_human_review=True,
        )
        engine = DeterministicReconciliationEngine()
        rec_result = engine.reconcile_packet(
            document_packet_id="pkt_wp4_02",
            packet_manifest=manifest,
            primary_coi_id="doc_coi_01",
            date_records=(drec,),
            discrepancies=(disc,),
        )

        # Even if only GL limit requirement is sent to /evaluate, attached reconciliation violation MUST propagate!
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)
        doc_set.reconciliation_result = rec_result
        req_limit = sample_gl_requirement(min_cents=100_000_000)

        res = self.client.post(
            "/api/v1/compliance/evaluate",
            json={
                "document_set": doc_set.model_dump(mode="json"),
                "requirements": [req_limit.model_dump(mode="json")],
                "evaluation_date": EVAL_DATE.isoformat(),
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["overall_status"], "non_compliant")
        self.assertTrue(data["requires_human_review"])
        reason_codes = [f["reason_code"] for f in data["findings"]]
        self.assertIn("PREMATURE_EXPIRATION_VIOLATION", reason_codes)

        # Verify persisted submission cannot be blanket-approved via human-review API
        sub_id = f"sub_wp4_02_{uuid4().hex[:8]}"
        repository.create_submission(
            submission_id=sub_id,
            name="WP4 Confirmed Violation",
            overall_status=data["overall_status"],
            processing_status="completed",
            provider="mock",
            evaluation_date=EVAL_DATE.isoformat(),
            result_data={"review": data},
            subcontractor_name="Apex Electrical Contractors LLC",
        )
        rev_res = self.client.post(
            f"/api/v1/submissions/{sub_id}/review",
            json={
                "review_status": "accepted_exception",
                "reviewer_name": "Auditor",
                "reviewer_notes": "Attempted override",
            },
        )
        self.assertEqual(rev_res.status_code, 422)

    def test_wp4_03_conflicting_date_evidence_results_in_human_review_not_compliant(self):
        """3. Conflicting date evidence within COI/endorsement snippet routes to REQUIRES_HUMAN_REVIEW via /evaluate."""
        ref_conflict = make_evidence(
            doc_id="doc_end_01",
            doc_type=DocumentType.ENDORSEMENT,
            page=1,
            text="Effective 01/01/2025 Expiration Date: 12/31/2026. Amended Expiration Date: 08/15/2026.",
        )
        cov = sample_gl_coverage(exp=date(2026, 12, 31))
        cov.evidence = self.ref_coi_dates
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            effective_date=date(2025, 1, 1),
            expiration_date=date(2026, 12, 31),
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            independent_integrity_verified=True,
            evidence=ref_conflict,
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])
        req = InsuranceRequirement(
            requirement_id="REQ_AI_CONFLICT",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured through 12/31/2026",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            required_coverage_end_date=date(2026, 12, 31),
            is_duration_requirement_validated=True,
        )
        res = self.client.post(
            "/api/v1/compliance/evaluate",
            json={
                "document_set": doc_set.model_dump(mode="json"),
                "requirements": [sample_gl_requirement().model_dump(mode="json"), req.model_dump(mode="json")],
                "evaluation_date": EVAL_DATE.isoformat(),
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertNotEqual(data["overall_status"], "compliant")
        self.assertEqual(data["overall_status"], "requires_human_review")
        self.assertTrue(data["requires_human_review"])
        self.assertIn("CONFLICTING_DATE_EVIDENCE", [f["reason_code"] for f in data["findings"]])

    def test_wp4_04_unsupported_endorsement_date_rejected_through_production_path(self):
        """4. Unsupported endorsement date (invoice date only) cannot be accepted through /evaluate."""
        ref_invoice_only = make_evidence(
            doc_id="doc_end_01",
            doc_type=DocumentType.ENDORSEMENT,
            page=1,
            text="Invoice #5512 billed 12/31/2026 for endorsement CG 20 10 04 13.",
        )
        cov = sample_gl_coverage(exp=date(2026, 12, 31))
        cov.evidence = self.ref_coi_dates
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            effective_date=date(2025, 1, 1),
            expiration_date=date(2026, 12, 31),
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            independent_integrity_verified=True,
            evidence=ref_invoice_only,
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])
        req = InsuranceRequirement(
            requirement_id="REQ_AI_UNSUP",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured through 12/31/2026",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            required_coverage_end_date=date(2026, 12, 31),
            is_duration_requirement_validated=True,
        )
        res = self.client.post(
            "/api/v1/compliance/evaluate",
            json={
                "document_set": doc_set.model_dump(mode="json"),
                "requirements": [req.model_dump(mode="json")],
                "evaluation_date": EVAL_DATE.isoformat(),
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["overall_status"], "requires_human_review")
        self.assertTrue(data["requires_human_review"])
        self.assertEqual(data["findings"][0]["reason_code"], "ENDORSEMENT_DATE_EVIDENCE_UNSUPPORTED")

    def test_wp4_05_ambiguous_multi_policy_selection_remains_human_review(self):
        """5. Ambiguous multi-policy selection remains REQUIRES_HUMAN_REVIEW through /api/v1/compliance/evaluate."""
        gl_cov = sample_gl_coverage(exp=date(2026, 12, 31))
        gl_cov.evidence = self.ref_coi_dates
        auto_cov = CoverageExtraction(
            coverage_type=CoverageType.AUTO_LIABILITY,
            policy_number="CA-200",
            effective_date=DateValue(date=date(2025, 1, 1), raw_text="01/01/2025", confidence=0.95),
            expiration_date=DateValue(date=date(2026, 6, 30), raw_text="06/30/2026", confidence=0.95),
            evidence=make_evidence(doc_id="doc_coi_01", text="Auto Policy Period: 01/01/2025 to 06/30/2026"),
        )
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            effective_date=date(2025, 1, 1),
            expiration_date=date(2026, 12, 31),
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            independent_integrity_verified=True,
            evidence=self.ref_end_dates,
        )
        doc_set = ExtractedDocumentSet(
            subcontractor_id="sub_wp4_05",
            coverages=[gl_cov, auto_cov],
            endorsements=[end],
        )
        req = InsuranceRequirement(
            requirement_id="REQ_WP4_05",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Endorsement through policy expiration (coverage_type omitted)",
            coverage_type=None,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            requires_coverage_through_policy_expiration=True,
            is_duration_requirement_validated=True,
        )
        res = self.client.post(
            "/api/v1/compliance/evaluate",
            json={
                "document_set": doc_set.model_dump(mode="json"),
                "requirements": [req.model_dump(mode="json")],
                "evaluation_date": EVAL_DATE.isoformat(),
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["overall_status"], "requires_human_review")
        self.assertTrue(data["requires_human_review"])
        self.assertEqual(data["findings"][0]["reason_code"], "AMBIGUOUS_APPLICABLE_POLICY")

    def test_wp4_06_generic_or_conflicting_amendment_document_not_silently_accepted(self):
        """6. Ambiguous/generic amendment wording on endorsement PDF reaches compliance result and review workflow."""
        # Case A: Generic boilerplate ("This endorsement amends the policy") in separate document
        ref_generic_amend = make_evidence(
            doc_id="doc_end_01",
            doc_type=DocumentType.ENDORSEMENT,
            page=1,
            text="This endorsement amends the policy to include Additional Insured coverage.",
        )
        cov = sample_gl_coverage(exp=date(2026, 12, 31))
        cov.evidence = self.ref_coi_dates
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            independent_integrity_verified=True,
            evidence=ref_generic_amend,
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])
        reqs = [
            sample_gl_requirement(),
            InsuranceRequirement(
                requirement_id="REQ_AI_BASIC",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE,
                description="Additional Insured required",
                required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            ),
        ]
        result = self.evaluator.evaluate(doc_set, reqs, EVAL_DATE)
        self.assertEqual(result.overall_status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertTrue(result.requires_human_review)
        rel_findings = [
            f for f in result.findings
            if f.reason_code == "GENERIC_RELATIONSHIP_WITHOUT_TARGET_IDENTITY"
        ]
        self.assertEqual(len(rel_findings), 1)
        self.assertEqual(rel_findings[0].evidence[0].document_id, "doc_end_01")

        # Case B: Orphaned replacement document (no target coverage document present)
        ref_orphan_rep = make_evidence(
            doc_id="doc_end_orphan",
            doc_type=DocumentType.ENDORSEMENT,
            page=1,
            text="This endorsement replaces prior policy Policy GL-987654-01.",
        )
        end_orphan = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            independent_integrity_verified=True,
            evidence=ref_orphan_rep,
        )
        doc_set_orphan = ExtractedDocumentSet(
            subcontractor_id="sub_orphan",
            coverages=[],
            endorsements=[end_orphan],
        )
        res_orphan = self.evaluator.evaluate(doc_set_orphan, [reqs[1]], EVAL_DATE)
        self.assertEqual(res_orphan.overall_status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertIn("MISSING_TARGET_DOCUMENT", [f.reason_code for f in res_orphan.findings])

    def test_wp4_07_entity_identity_conflict_through_real_document_review_service(self):
        """7. Full PDF upload via DocumentReviewService propagates cross-document entity identity mismatch to SQLite & review API."""
        coi_bytes = _make_pdf_bytes([
            "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE",
            "INSURED: Apex Electrical Contractors LLC",
            "CERTIFICATE HOLDER: General Contractor Corp",
            "COMMERCIAL GENERAL LIABILITY",
            "POLICY NUMBER: GL-98765432",
            "POLICY EFF: 01/01/2025  EXP: 12/31/2026",
            "EACH OCCURRENCE: $1,000,000",
            "GENERAL AGGREGATE: $2,000,000",
        ])
        end_bytes = _make_pdf_bytes([
            "POLICY ENDORSEMENT - ADDITIONAL INSURED",
            "INSURED: Zenith Plumbing Inc",
            "FORM CG 20 10 04 13",
            "POLICY NUMBER: GL-98765432",
            "ADDITIONAL INSURED: General Contractor Corp",
        ])

        service = DocumentReviewService(repository=self.repo)
        reqs = [
            sample_gl_requirement(),
            InsuranceRequirement(
                requirement_id="REQ_NI",
                rule_type=RequirementType.NAMED_INSURED,
                description="Named insured must match Apex Electrical Contractors LLC",
                required_entity_name="Apex Electrical Contractors LLC",
            ),
            InsuranceRequirement(
                requirement_id="REQ_AI",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE,
                description="Additional Insured required",
                required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            ),
        ]

        out = asyncio.run(
            service.process_and_review(
                coi_file=_make_upload_file("coi.pdf", coi_bytes),
                endorsement_files=[_make_upload_file("endorsement.pdf", end_bytes)],
                requirements=reqs,
                submission_name="WP4 Entity Mismatch Packet",
                evaluation_date=EVAL_DATE,
                provider_name="mock",
            )
        )

        self.assertEqual(out["overall_status"], "non_compliant")
        self.assertTrue(out["requires_human_review"])
        review_res = out["review_result"]
        self.assertIsNotNone(review_res.reconciliation_summary)
        self.assertEqual(review_res.reconciliation_summary["entity_records_count"], 1)
        self.assertIn("AFFIRMATIVE_CONTRACTUAL_ENTITY_MISMATCH", [f.reason_code for f in review_res.findings])

        # Check SQLite persisted submission preserves the entity mismatch finding & document evidence
        persisted = self.repo.get_submission_by_id(out["submission_id"])
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["overall_status"], "non_compliant")
        persisted_findings = persisted["result"]["review"]["findings"]
        mismatch_finding = next(
            f for f in persisted_findings if f["reason_code"] == "AFFIRMATIVE_CONTRACTUAL_ENTITY_MISMATCH"
        )
        self.assertEqual(len(mismatch_finding["evidence"]), 2)
        self.assertIn("reconciliation_summary", persisted["result"]["review"])

    def test_wp4_08_human_review_reason_codes_and_evidence_preserved_through_api(self):
        """8. Human-review reason codes and document/evidence references survive upload -> GET submission -> POST review."""
        coi_bytes = _make_pdf_bytes([
            "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE",
            "INSURED: Apex Electrical Contractors LLC",
            "CERTIFICATE HOLDER: General Contractor Corp",
            "COMMERCIAL GENERAL LIABILITY",
            "POLICY NUMBER: GL-98765432",
            "POLICY EFF: 01/01/2025  EXP: 12/31/2026",
            "EACH OCCURRENCE: $1,000,000",
        ])
        # Suffix discrepancy ('Apex Electrical Contractors Inc' vs 'LLC') triggers LEGAL_SUFFIX_DISCREPANCY -> human review
        end_bytes = _make_pdf_bytes([
            "POLICY ENDORSEMENT - ADDITIONAL INSURED",
            "INSURED: Apex Electrical Contractors Inc",
            "FORM CG 20 10 04 13",
            "POLICY NUMBER: GL-98765432",
            "ADDITIONAL INSURED: General Contractor Corp",
        ])

        reqs = [
            sample_gl_requirement(),
            InsuranceRequirement(
                requirement_id="REQ_NI",
                rule_type=RequirementType.NAMED_INSURED,
                description="Named insured match",
                required_entity_name="Apex Electrical Contractors LLC",
            ),
        ]

        res = self.client.post(
            "/api/v1/compliance/review-documents",
            files=[
                ("coi_file", ("coi.pdf", io.BytesIO(coi_bytes), "application/pdf")),
                ("endorsement_files", ("end_suffix.pdf", io.BytesIO(end_bytes), "application/pdf")),
            ],
            data={
                "requirements": "[" + ",".join(r.model_dump_json() for r in reqs) + "]",
                "submission_name": "WP4 Suffix Discrepancy Review",
                "evaluation_date": EVAL_DATE.isoformat(),
                "provider": "mock",
            },
        )
        self.assertEqual(res.status_code, 200)
        upload_data = res.json()
        sub_id = upload_data["submission_id"]
        self.assertEqual(upload_data["overall_status"], "requires_human_review")
        self.assertTrue(upload_data["requires_human_review"])

        # GET persisted submission and confirm reason_code + evidence provenance survived
        sub_res = self.client.get(f"/api/v1/submissions/{sub_id}")
        self.assertEqual(sub_res.status_code, 200)
        sub_data = sub_res.json()
        findings = sub_data["result"]["review"]["findings"]
        suffix_finding = next(
            (f for f in findings if f["reason_code"] == "CORPORATE_SUFFIX_DISCREPANCY"),
            None,
        )
        self.assertIsNotNone(suffix_finding)
        self.assertTrue(suffix_finding["requires_human_review"])
        self.assertEqual(len(suffix_finding["evidence"]), 2)

        # Record a human review decision referencing the finding_id and confirm audit trail
        rev_res = self.client.post(
            f"/api/v1/submissions/{sub_id}/review",
            json={
                "review_status": "deficiency_notice_required",
                "reviewer_name": "Senior Compliance Analyst",
                "reviewer_notes": "Endorsement lists Inc instead of LLC; correction requested.",
                "finding_id": suffix_finding["finding_id"],
                "update_submission_status": "deficiency_pending",
            },
        )
        self.assertEqual(rev_res.status_code, 200)
        rev_payload = rev_res.json()["review"]
        self.assertEqual(rev_payload["finding_id"], suffix_finding["finding_id"])
        self.assertEqual(rev_payload["reason_code"], "CORPORATE_SUFFIX_DISCREPANCY")

    def test_wp4_09_reconciliation_findings_are_not_duplicated_or_reexecuted(self):
        """9. Attached reconciliation_result is not re-computed and discrepancies are not duplicated."""
        cov = sample_gl_coverage(exp=date(2026, 12, 31))
        cov.evidence = self.ref_coi_dates
        ref_end_short = make_evidence(
            doc_id="doc_end_01",
            doc_type=DocumentType.ENDORSEMENT,
            page=1,
            text="Effective Date: 01/01/2025 Expiration Date: 06/30/2026 Form CG 20 10",
        )
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            effective_date=date(2025, 1, 1),
            expiration_date=date(2026, 6, 30),
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            independent_integrity_verified=True,
            evidence=ref_end_short,
        )
        req_dur = InsuranceRequirement(
            requirement_id="REQ_AI_NODUP",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured through 12/31/2026",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            required_coverage_end_date=date(2026, 12, 31),
            is_duration_requirement_validated=True,
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])

        # Pre-compute reconciliation once (simulating DocumentReviewService orchestration)
        precomputed = reconcile_extracted_document_set(doc_set, [req_dur], EVAL_DATE)
        self.assertIsNotNone(precomputed)
        doc_set.reconciliation_result = precomputed

        with patch("app.compliance.reconciliation.reconcile_extracted_document_set") as spy:
            result = self.evaluator.evaluate(doc_set, [req_dur], EVAL_DATE)
            spy.assert_not_called()

        # Exactly 1 finding for REQ_AI_NODUP (no duplicate PREMATURE_EXPIRATION_VIOLATION alongside ENDORSEMENT_PREMATURE_EXPIRATION)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].requirement_id, "REQ_AI_NODUP")
        self.assertEqual(result.findings[0].reason_code, "ENDORSEMENT_PREMATURE_EXPIRATION")
        self.assertEqual(result.overall_status, ComplianceStatus.NON_COMPLIANT)

        # Also verify when evaluating directly without precomputation: still 1 finding, no duplicate
        doc_set_fresh = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])
        result_direct = self.evaluator.evaluate(doc_set_fresh, [req_dur], EVAL_DATE)
        self.assertEqual(len(result_direct.findings), 1)
        self.assertEqual(result_direct.findings[0].reason_code, "ENDORSEMENT_PREMATURE_EXPIRATION")

    def test_wp4_10_existing_non_reconciliation_compliance_behavior_unchanged(self):
        """10. Standard non-reconciliation limit/expiration/named-insured behavior remains unchanged."""
        req_limit = sample_gl_requirement(min_cents=100_000_000)
        req_exp = InsuranceRequirement(
            requirement_id="REQ_EXP",
            rule_type=RequirementType.POLICY_EXPIRATION,
            description="Policy unexpired",
            coverage_type=CoverageType.GENERAL_LIABILITY,
        )
        req_ni = InsuranceRequirement(
            requirement_id="REQ_NI",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured match",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        doc_set = sample_subcontractor_document_set()
        res = self.evaluator.evaluate(doc_set, [req_limit, req_exp, req_ni], EVAL_DATE)
        self.assertEqual(res.overall_status, ComplianceStatus.COMPLIANT)
        self.assertFalse(res.requires_human_review)
        self.assertEqual(
            [f.reason_code for f in res.findings],
            ["LIMIT_MEETS_OR_EXCEEDS_MINIMUM", "POLICY_ACTIVE_AND_CURRENT", "NAMED_INSURED_EXACT_MATCH"],
        )

    def test_wp4_11_existing_review_first_policy_preserved(self):
        """11. Review-first manuscript & blanket endorsement safeguards remain intact alongside reconciliation."""
        cov = sample_gl_coverage(exp=date(2026, 12, 31))
        cov.evidence = self.ref_coi_dates
        end_unverified_manuscript = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            effective_date=date(2025, 1, 1),
            expiration_date=date(2026, 12, 31),
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            independent_integrity_verified=False,  # Must trigger review-first safeguard 8b
            evidence=self.ref_end_dates,
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end_unverified_manuscript])
        req = InsuranceRequirement(
            requirement_id="REQ_AI_REVIEW_FIRST",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured through 12/31/2026",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            required_coverage_end_date=date(2026, 12, 31),
        )
        res = self.evaluator.evaluate(doc_set, [req], EVAL_DATE)
        self.assertEqual(res.overall_status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertTrue(res.requires_human_review)
        self.assertEqual(res.findings[0].reason_code, "MANUSCRIPT_VERIFICATION_UNSUPPORTED")

    def test_wp4_12_repeated_evaluation_identical_inputs_deterministic(self):
        """12. Repeated evaluation of identical inputs via evaluator and /evaluate produces consistent deterministic outputs."""
        cov = sample_gl_coverage(exp=date(2026, 12, 31))
        cov.evidence = self.ref_coi_dates
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            applicable_policy="GL-WRONG-999",
            effective_date=date(2025, 1, 1),
            expiration_date=date(2026, 12, 31),
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            independent_integrity_verified=True,
            evidence=self.ref_end_dates,
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])
        reqs = [
            sample_gl_requirement(),
            InsuranceRequirement(
                requirement_id="REQ_AI_DET",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE,
                description="Additional Insured through 12/31/2026",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                required_coverage_end_date=date(2026, 12, 31),
            ),
        ]

        snapshots = []
        for _ in range(3):
            res = self.evaluator.evaluate(doc_set.model_copy(deep=True), reqs, EVAL_DATE)
            snapshots.append((
                res.overall_status,
                res.requires_human_review,
                [(f.requirement_id, f.status, f.reason_code, f.finding_id) for f in res.findings if f.finding_id.startswith("rec_")],
                res.reconciliation_summary,
            ))

        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[1], snapshots[2])


if __name__ == "__main__":
    unittest.main()
