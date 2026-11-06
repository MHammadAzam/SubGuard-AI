"""Milestone 2.1: Human Review Integrity & Compliance Transition Tests.

Tests:
1. Approval when all rules pass.
2. Approval when a rule requires human review.
3. Attempted approval when a hard-failure rule exists (must be rejected with HTTP 422).
4. Waiver of an explicitly waivable rule (with valid rule ID and justification).
5. Attempted waiver of a non-waivable rule (must be rejected with HTTP 422).
6. Attempted review with empty reviewer notes (rejected with HTTP 422).
7. Attempted review with missing reviewer identity (rejected with HTTP 422).
8. Attempted review with invalid decision value (rejected with HTTP 422).
9. Repeated review submissions (append-only audit history).
10. Preservation of original automated evaluation and immutable document set evidence.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app, repository
from app.persistence.database import init_db
from app.persistence.repository import SubmissionRepository
from app.schemas.insurance import ComplianceStatus, RequirementType, Severity


class TestHumanReviewIntegrity(unittest.TestCase):
    """Test human review audit, rule-waiver, and state integrity enforcement."""

    def setUp(self):
        self.client = TestClient(app)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / f"test_review_{uuid4().hex[:8]}.db"
        init_db(self.db_path)
        self.test_repo = SubmissionRepository(db_path=self.db_path)
        # Point the application's repository to this test database during test
        self.orig_repo = app.state if hasattr(app, "state") else None
        # We will test both via client and using submissions created in DB

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_submission_in_repo(self, sub_id: str, findings: list, overall_status: str):
        """Helper to create a structured submission directly in the DB."""
        result_data = {
            "review": {
                "subcontractor_id": "sub_apex",
                "evaluation_date": "2025-06-15",
                "overall_status": overall_status,
                "requires_human_review": any(f.get("requires_human_review", False) for f in findings),
                "findings": findings,
            },
            "document_set": {
                "subcontractor_id": "sub_apex",
                "documents_submitted": ["coi"],
                "coverages": [{"coverage_type": "general_liability", "policy_number": "GL-98765432"}],
                "endorsements": [],
            },
        }
        return repository.create_submission(
            submission_id=sub_id,
            name=f"Test Submission {sub_id}",
            overall_status=overall_status,
            processing_status="completed",
            provider="mock",
            evaluation_date="2025-06-15",
            result_data=result_data,
            subcontractor_name="Apex Electrical Contractors LLC",
        )

    def test_1_approval_when_all_rules_pass(self):
        """1. Approval when all rules pass is permitted and sets operational status."""
        sub_id = f"sub_pass_{uuid4().hex[:8]}"
        findings = [
            {
                "finding_id": "f_gl_occ",
                "requirement_id": "REQ_GL_EACH_OCC",
                "rule_type": "coverage_limit",
                "status": "compliant",
                "severity": "critical",
                "reason_code": "LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                "requires_human_review": False,
                "explanation": "GL limit meets required $1,000,000",
            }
        ]
        self._create_submission_in_repo(sub_id, findings, "compliant")

        payload = {
            "review_status": "accepted_exception",
            "reviewer_name": "Quality Officer",
            "reviewer_notes": "All rules verified passing. Standard sign-off.",
            "update_submission_status": "compliant",
        }
        res = self.client.post(f"/api/v1/submissions/{sub_id}/review", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["review"]["review_status"], "accepted_exception")
        self.assertEqual(data["review"]["reviewer_name"], "Quality Officer")

        # Verify DB record
        sub = repository.get_submission_by_id(sub_id)
        self.assertEqual(sub["overall_status"], "compliant")
        self.assertEqual(len(sub["reviews"]), 1)

    def test_2_approval_when_rule_requires_human_review(self):
        """2. Approval when an uncertainty flag exists resolves human review with audit trail."""
        sub_id = f"sub_hr_{uuid4().hex[:8]}"
        findings = [
            {
                "finding_id": "f_blanket",
                "requirement_id": "REQ_ADDITIONAL_INSURED",
                "rule_type": "endorsement_presence",
                "status": "requires_human_review",
                "severity": "critical",
                "reason_code": "BLANKET_ENDORSEMENT_REQUIRES_CONTRACT_CHECK",
                "requires_human_review": True,
                "explanation": "Blanket endorsement requires checking subcontract agreement.",
            }
        ]
        self._create_submission_in_repo(sub_id, findings, "requires_human_review")

        payload = {
            "review_status": "accepted_exception",
            "reviewer_name": "Senior Safety Officer",
            "reviewer_notes": "Verified underlying subcontract agreement explicitly mandates Additional Insured coverage.",
            "update_submission_status": "compliant",
        }
        res = self.client.post(f"/api/v1/submissions/{sub_id}/review", json=payload)
        self.assertEqual(res.status_code, 200)

        # Check DB
        sub = repository.get_submission_by_id(sub_id)
        self.assertEqual(sub["overall_status"], "compliant")
        # Ensure original automated evaluation finding remains UNCHANGED in result_json
        orig_findings = sub["result"]["review"]["findings"]
        self.assertEqual(orig_findings[0]["status"], "requires_human_review")
        self.assertEqual(orig_findings[0]["reason_code"], "BLANKET_ENDORSEMENT_REQUIRES_CONTRACT_CHECK")

    def test_3_approval_when_hard_failure_exists_is_blocked(self):
        """3. Approval when a deterministic hard failure exists MUST raise HTTP 422."""
        sub_id = f"sub_fail_{uuid4().hex[:8]}"
        findings = [
            {
                "finding_id": "f_limit_fail",
                "requirement_id": "REQ_GL_EACH_OCC",
                "rule_type": "coverage_limit",
                "status": "non_compliant",
                "severity": "critical",
                "reason_code": "LIMIT_BELOW_MINIMUM",
                "requires_human_review": False,
                "explanation": "Limit $500,000 is below required $1,000,000",
            }
        ]
        self._create_submission_in_repo(sub_id, findings, "non_compliant")

        payload = {
            "review_status": "accepted_exception",
            "reviewer_name": "Careless Reviewer",
            "reviewer_notes": "Trying to silently approve a failed limit",
            "update_submission_status": "compliant",
        }
        res = self.client.post(f"/api/v1/submissions/{sub_id}/review", json=payload)
        self.assertEqual(res.status_code, 422)
        err = res.json()["detail"]
        self.assertIn("Cannot approve submission", err)
        self.assertIn("deterministic hard failure", err)
        self.assertIn("REQ_GL_EACH_OCC", err)

        # Submission status in DB must remain non_compliant
        sub = repository.get_submission_by_id(sub_id)
        self.assertEqual(sub["overall_status"], "non_compliant")
        self.assertEqual(len(sub["reviews"]), 0)

    def test_4_waiver_of_explicitly_waivable_rule(self):
        """4. Waiver of an explicitly waivable rule (e.g. Subrogation Waiver) succeeds with target rule ID."""
        sub_id = f"sub_waive_{uuid4().hex[:8]}"
        findings = [
            {
                "finding_id": "f_wos",
                "requirement_id": "REQ_WAIVER_OF_SUBROGATION",
                "rule_type": "endorsement_presence",
                "status": "non_compliant",
                "severity": "major",
                "reason_code": "ENDORSEMENT_NOT_FOUND",
                "requires_human_review": False,
                "explanation": "Waiver of subrogation endorsement missing",
            }
        ]
        self._create_submission_in_repo(sub_id, findings, "non_compliant")

        payload = {
            "review_status": "waived",
            "rule_id": "REQ_WAIVER_OF_SUBROGATION",
            "reviewer_name": "Risk Director",
            "reviewer_notes": "GC granted explicit variance for Subrogation Waiver on this interior finishing trade package.",
            "update_submission_status": "waived",
        }
        res = self.client.post(f"/api/v1/submissions/{sub_id}/review", json=payload)
        self.assertEqual(res.status_code, 200)

        sub = repository.get_submission_by_id(sub_id)
        self.assertEqual(sub["overall_status"], "waived")
        self.assertEqual(len(sub["reviews"]), 1)
        self.assertEqual(sub["reviews"][0]["finding_id"], "f_wos")
        self.assertEqual(sub["reviews"][0]["reason_code"], "ENDORSEMENT_NOT_FOUND")

    def test_5_attempted_waiver_of_non_waivable_rule_is_blocked(self):
        """5. Attempted waiver of a non-waivable rule (policy expiration, named insured) MUST raise HTTP 422."""
        sub_id = f"sub_nowaive_{uuid4().hex[:8]}"
        findings = [
            {
                "finding_id": "f_exp",
                "requirement_id": "REQ_GL_EXPIRATION",
                "rule_type": "policy_expiration",
                "status": "non_compliant",
                "severity": "critical",
                "reason_code": "POLICY_EXPIRED",
                "requires_human_review": False,
                "explanation": "Policy expired on 2024-01-01",
            }
        ]
        self._create_submission_in_repo(sub_id, findings, "non_compliant")

        payload = {
            "review_status": "waived",
            "rule_id": "REQ_GL_EXPIRATION",
            "reviewer_name": "Risk Director",
            "reviewer_notes": "Attempting to waive an expired policy",
            "update_submission_status": "waived",
        }
        res = self.client.post(f"/api/v1/submissions/{sub_id}/review", json=payload)
        self.assertEqual(res.status_code, 422)
        err = res.json()["detail"]
        self.assertIn("non-waivable", err)
        self.assertIn("Active coverage dates", err)

        # Also test named insured mismatch
        sub_id2 = f"sub_nowaive2_{uuid4().hex[:8]}"
        findings2 = [
            {
                "finding_id": "f_name",
                "requirement_id": "REQ_NAMED_INSURED",
                "rule_type": "named_insured",
                "status": "non_compliant",
                "severity": "critical",
                "reason_code": "NAMED_INSURED_MISMATCH",
                "requires_human_review": False,
                "explanation": "Named insured does not match subcontractor",
            }
        ]
        self._create_submission_in_repo(sub_id2, findings2, "non_compliant")

        payload2 = {
            "review_status": "waived",
            "rule_id": "REQ_NAMED_INSURED",
            "reviewer_name": "Risk Director",
            "reviewer_notes": "Attempting to waive named insured identity",
        }
        res2 = self.client.post(f"/api/v1/submissions/{sub_id2}/review", json=payload2)
        self.assertEqual(res2.status_code, 422)
        self.assertIn("non-waivable", res2.json()["detail"])

    def test_6_empty_reviewer_notes_is_rejected(self):
        """6. Review submission with empty or whitespace-only notes returns 422."""
        sub_id = f"sub_notes_{uuid4().hex[:8]}"
        self._create_submission_in_repo(sub_id, [], "compliant")

        # Empty string
        res = self.client.post(
            f"/api/v1/submissions/{sub_id}/review",
            json={"review_status": "accepted_exception", "reviewer_name": "Officer", "reviewer_notes": ""},
        )
        self.assertEqual(res.status_code, 422)

        # Whitespace string
        res2 = self.client.post(
            f"/api/v1/submissions/{sub_id}/review",
            json={"review_status": "accepted_exception", "reviewer_name": "Officer", "reviewer_notes": "   \n  \t "},
        )
        self.assertEqual(res2.status_code, 422)
        self.assertIn("notes cannot be empty", res2.json()["detail"])

    def test_7_missing_reviewer_identity_is_rejected(self):
        """7. Review submission with missing or whitespace reviewer name returns 422."""
        sub_id = f"sub_name_{uuid4().hex[:8]}"
        self._create_submission_in_repo(sub_id, [], "compliant")

        res = self.client.post(
            f"/api/v1/submissions/{sub_id}/review",
            json={"review_status": "accepted_exception", "reviewer_name": "   ", "reviewer_notes": "Valid note."},
        )
        self.assertEqual(res.status_code, 422)
        self.assertIn("identity cannot be empty", res.json()["detail"])

    def test_8_invalid_decision_values_rejected(self):
        """8. Invalid review_status values are rejected with 422."""
        sub_id = f"sub_inv_{uuid4().hex[:8]}"
        self._create_submission_in_repo(sub_id, [], "compliant")

        res = self.client.post(
            f"/api/v1/submissions/{sub_id}/review",
            json={"review_status": "arbitrary_bypass", "reviewer_name": "Officer", "reviewer_notes": "Valid note."},
        )
        self.assertEqual(res.status_code, 422)
        self.assertIn("Invalid review_status", res.json()["detail"])

    def test_9_repeated_review_submissions_preserves_complete_audit_history(self):
        """9. Repeated reviews append immutably to audit records, preserving full chronological log."""
        sub_id = f"sub_repeat_{uuid4().hex[:8]}"
        self._create_submission_in_repo(sub_id, [], "requires_human_review")

        # First review: deficiency notice
        res1 = self.client.post(
            f"/api/v1/submissions/{sub_id}/review",
            json={
                "review_status": "deficiency_notice_required",
                "reviewer_name": "Reviewer A",
                "reviewer_notes": "Issued deficiency request to subcontractor for additional insured endorsement.",
                "update_submission_status": "deficiency_pending",
            },
        )
        self.assertEqual(res1.status_code, 200)

        # Second review: subsequent resolution
        res2 = self.client.post(
            f"/api/v1/submissions/{sub_id}/review",
            json={
                "review_status": "accepted_exception",
                "reviewer_name": "Reviewer B",
                "reviewer_notes": "Received signed GC endorsement addendum. Resolving flag.",
                "update_submission_status": "compliant",
            },
        )
        self.assertEqual(res2.status_code, 200)

        sub = repository.get_submission_by_id(sub_id)
        self.assertEqual(len(sub["reviews"]), 2)
        # Reviews ordered by created_at DESC
        self.assertEqual(sub["reviews"][0]["reviewer_name"], "Reviewer B")
        self.assertEqual(sub["reviews"][0]["review_status"], "accepted_exception")
        self.assertEqual(sub["reviews"][1]["reviewer_name"], "Reviewer A")
        self.assertEqual(sub["reviews"][1]["review_status"], "deficiency_notice_required")

    def test_10_preservation_of_original_evaluation_and_document_evidence(self):
        """10. Human reviews never alter original extracted document set or automated evaluation findings."""
        sub_id = f"sub_preserve_{uuid4().hex[:8]}"
        findings = [
            {
                "finding_id": "f_hr",
                "requirement_id": "REQ_GL_EACH_OCC",
                "rule_type": "coverage_limit",
                "status": "requires_human_review",
                "severity": "critical",
                "reason_code": "EXTRACTION_CONFIDENCE_LOW",
                "requires_human_review": True,
                "explanation": "Confidence score is 0.65",
            }
        ]
        self._create_submission_in_repo(sub_id, findings, "requires_human_review")

        # Snapshot before review
        before = repository.get_submission_by_id(sub_id)

        # Record review
        res = self.client.post(
            f"/api/v1/submissions/{sub_id}/review",
            json={
                "review_status": "accepted_exception",
                "reviewer_name": "Inspector",
                "reviewer_notes": "Verified limit manually from high-res original scan.",
                "update_submission_status": "compliant",
            },
        )
        self.assertEqual(res.status_code, 200)

        # Snapshot after review
        after = repository.get_submission_by_id(sub_id)
        # 1. Automated evaluation findings in result_json must be byte-for-byte identical
        self.assertEqual(before["result"]["review"]["findings"], after["result"]["review"]["findings"])
        # 2. Document set must be byte-for-byte identical
        self.assertEqual(before["result"]["document_set"], after["result"]["document_set"])
        # 3. Automated status in review object remains requires_human_review
        self.assertEqual(after["result"]["review"]["overall_status"], "requires_human_review")
        # 4. Operational status changed in top-level table
        self.assertEqual(after["overall_status"], "compliant")


if __name__ == "__main__":
    unittest.main()
