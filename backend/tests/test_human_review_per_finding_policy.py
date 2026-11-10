"""Phase 1A (ISS-01): Adversarial Human-Review Per-Finding Resolution Integrity Tests.

Verifies the strict per-finding resolution policy, blocking finding invariants
(NON_COMPLIANT, MISSING_EVIDENCE, UNABLE_TO_DETERMINE, REQUIRES_HUMAN_REVIEW),
non-waivability boundaries, server-authoritative final status calculation,
atomic SQLite persistence rollback, and immutable audit preservation.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app, repository
from app.persistence.database import get_connection, init_db
from app.schemas.insurance import ComplianceStatus, RequirementType, Severity
from tests.fixtures.auth_helpers import authenticate_session_client


def _make_finding(
    finding_id: str,
    requirement_id: str,
    *,
    rule_type: str = RequirementType.ENDORSEMENT_PRESENCE.value,
    status: str = ComplianceStatus.NON_COMPLIANT.value,
    reason_code: str = "ENDORSEMENT_NOT_FOUND",
    requires_human_review: bool = False,
    severity: str = Severity.MAJOR.value,
    explanation: str = "Automated rule evaluation outcome.",
) -> dict:
    return {
        "finding_id": finding_id,
        "requirement_id": requirement_id,
        "rule_type": rule_type,
        "status": status,
        "severity": severity,
        "reason_code": reason_code,
        "requires_human_review": requires_human_review,
        "explanation": explanation,
    }


class TestHumanReviewPerFindingPolicy(unittest.TestCase):
    """Adversarial tests blocking ISS-01 bypass paths and verifying atomic per-finding auditability."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / f"iss01_review_{uuid4().hex[:8]}.db"
        init_db(self.db_path)
        self._orig_db_path = repository.db_path
        repository.db_path = self.db_path
        # Disable raise_server_exceptions only where we explicitly assert DB rollback behavior
        self.client = TestClient(app)
        authenticate_session_client(
            self.client,
            db_path=self.db_path,
            username="auditor_iss01",
            display_name="Auditor",
            role="reviewer",
        )

    def tearDown(self):
        repository.db_path = self._orig_db_path
        self.temp_dir.cleanup()

    def _seed_submission(
        self,
        findings: list,
        overall_status: str,
        sub_id: str | None = None,
    ) -> str:
        sid = sub_id or f"sub_iss01_{uuid4().hex[:8]}"
        repository.create_submission(
            submission_id=sid,
            name=f"ISS-01 Submission {sid}",
            overall_status=overall_status,
            processing_status="completed",
            provider="mock",
            evaluation_date="2025-06-15",
            result_data={
                "review": {
                    "subcontractor_id": "sub_iss01",
                    "evaluation_date": "2025-06-15",
                    "overall_status": overall_status,
                    "requires_human_review": any(
                        f.get("requires_human_review", False)
                        or f.get("status") != ComplianceStatus.COMPLIANT.value
                        for f in findings
                    ),
                    "findings": findings,
                },
                "document_set": {
                    "subcontractor_id": "sub_iss01",
                    "documents_submitted": ["coi"],
                    "coverages": [{"coverage_type": "general_liability", "policy_number": "GL-100"}],
                    "endorsements": [],
                },
            },
            subcontractor_name="Apex Electrical Contractors LLC",
        )
        return sid

    def test_01_non_compliant_cannot_become_compliant_by_waiving_another_finding(self):
        """1. Waiving finding B cannot suppress/bypass NON_COMPLIANT finding A."""
        findings = [
            _make_finding(
                "f_exp",
                "REQ_GL_EXPIRATION",
                rule_type=RequirementType.POLICY_EXPIRATION.value,
                status=ComplianceStatus.NON_COMPLIANT.value,
                reason_code="POLICY_EXPIRED",
            ),
            _make_finding(
                "f_wos",
                "REQ_WAIVER_OF_SUBROGATION",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE.value,
                status=ComplianceStatus.NON_COMPLIANT.value,
                reason_code="ENDORSEMENT_NOT_FOUND",
            ),
        ]
        sid = self._seed_submission(findings, ComplianceStatus.NON_COMPLIANT.value)

        # Attempt A/B/C: Waive B while requesting compliant, resolved_compliant, or waived submission status
        for bad_target_status in ("compliant", "resolved_compliant", "waived"):
            res = self.client.post(
                f"/api/v1/submissions/{sid}/review",
                json={
                    "review_status": "waived",
                    "rule_id": "REQ_WAIVER_OF_SUBROGATION",
                    "reviewer_name": "Auditor",
                    "reviewer_notes": "Waiving WOS only.",
                    "update_submission_status": bad_target_status,
                },
            )
            self.assertEqual(res.status_code, 422, f"Expected 422 for update_submission_status={bad_target_status}")
            persisted = repository.get_submission_by_id(sid)
            self.assertEqual(persisted["overall_status"], "non_compliant")
            self.assertEqual(len(persisted["reviews"]), 0)

        # Attempt D: Record valid waiver on B with no spoofed status -> only B waived, server keeps non_compliant
        ok_res = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "waived",
                "rule_id": "REQ_WAIVER_OF_SUBROGATION",
                "reviewer_name": "Risk Director",
                "reviewer_notes": "Explicit variance granted for WOS only.",
            },
        )
        self.assertEqual(ok_res.status_code, 200)
        self.assertEqual(ok_res.json()["overall_status"], "non_compliant")
        persisted = repository.get_submission_by_id(sid)
        self.assertEqual(persisted["overall_status"], "non_compliant")
        self.assertEqual(len(persisted["reviews"]), 1)
        self.assertEqual(persisted["reviews"][0]["finding_id"], "f_wos")

        # Subsequent blanket accepted_exception still blocked because REQ_GL_EXPIRATION remains non_compliant
        blk_res = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "accepted_exception",
                "reviewer_name": "Auditor",
                "reviewer_notes": "Attempting approval after partial waiver.",
                "update_submission_status": "compliant",
            },
        )
        self.assertEqual(blk_res.status_code, 422)

    def test_02_two_non_compliant_findings_cannot_become_compliant_after_resolving_one(self):
        """2. Two waivable NON_COMPLIANT findings require individual resolution of both; never pure compliant."""
        findings = [
            _make_finding(
                "f_wos",
                "REQ_WAIVER_OF_SUBROGATION",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE.value,
                status=ComplianceStatus.NON_COMPLIANT.value,
                reason_code="ENDORSEMENT_NOT_FOUND",
            ),
            _make_finding(
                "f_pnc",
                "REQ_PRIMARY_NONCONTRIBUTORY",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE.value,
                status=ComplianceStatus.NON_COMPLIANT.value,
                reason_code="ENDORSEMENT_NOT_FOUND",
            ),
        ]
        sid = self._seed_submission(findings, ComplianceStatus.NON_COMPLIANT.value)

        # Resolve first finding without forcing compliant
        res1 = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "waived",
                "finding_id": "f_wos",
                "reviewer_name": "Risk Director",
                "reviewer_notes": "WOS waived for minor trade.",
            },
        )
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.json()["overall_status"], "non_compliant")

        # Cannot resolve submission to compliant/resolved_compliant while second remains
        res_bad = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "accepted_exception",
                "reviewer_name": "Risk Director",
                "reviewer_notes": "Trying early approval.",
                "update_submission_status": "compliant",
            },
        )
        self.assertEqual(res_bad.status_code, 422)

        # Waive second finding with spoofed update_submission_status="compliant" -> rejected (status is waived)
        res_bad_compliant = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "waived",
                "finding_id": "f_pnc",
                "reviewer_name": "Risk Director",
                "reviewer_notes": "PNC waived too.",
                "update_submission_status": "compliant",
            },
        )
        self.assertEqual(res_bad_compliant.status_code, 422)

        # Waive second finding properly -> server calculates 'waived'
        res2 = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "waived",
                "finding_id": "f_pnc",
                "reviewer_name": "Risk Director",
                "reviewer_notes": "PNC variance documented and approved.",
            },
        )
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json()["overall_status"], "waived")
        sub = repository.get_submission_by_id(sid)
        self.assertEqual(sub["overall_status"], "waived")
        self.assertEqual(len(sub["reviews"]), 2)

    def test_03_missing_evidence_protected_until_valid_per_finding_resolution(self):
        """3. MISSING_EVIDENCE cannot become compliant via deficiency/rejection/blanket approval/unrelated waiver."""
        findings = [
            _make_finding(
                "f_miss",
                "REQ_ENDORSEMENT_DATES",
                rule_type=RequirementType.CONTRACTUAL_DURATION.value,
                status=ComplianceStatus.MISSING_EVIDENCE.value,
                reason_code="MISSING_ENDORSEMENT_DATES",
                requires_human_review=True,
            ),
            _make_finding(
                "f_wos",
                "REQ_WAIVER_OF_SUBROGATION",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE.value,
                status=ComplianceStatus.NON_COMPLIANT.value,
                reason_code="ENDORSEMENT_NOT_FOUND",
            ),
        ]
        sid = self._seed_submission(findings, ComplianceStatus.MISSING_EVIDENCE.value)

        # Bypass 1: deficiency_notice_required + update_submission_status="compliant" (previously bypassed line 373!)
        r_def = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "deficiency_notice_required",
                "reviewer_name": "Reviewer",
                "reviewer_notes": "Trying deficiency notice with compliant override.",
                "update_submission_status": "compliant",
            },
        )
        self.assertEqual(r_def.status_code, 422)

        # Bypass 2: rejected + update_submission_status="resolved_compliant"
        r_rej = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "rejected",
                "reviewer_name": "Reviewer",
                "reviewer_notes": "Trying rejected with compliant override.",
                "update_submission_status": "resolved_compliant",
            },
        )
        self.assertEqual(r_rej.status_code, 422)

        # Waive f_wos legitimately -> server still sees MISSING_EVIDENCE on f_miss
        r_waive = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "waived",
                "finding_id": "f_wos",
                "reviewer_name": "Risk Director",
                "reviewer_notes": "WOS waived only.",
            },
        )
        self.assertEqual(r_waive.status_code, 200)
        self.assertEqual(r_waive.json()["overall_status"], "missing_evidence")

        # Bypass 3: untargeted accepted_exception on remaining MISSING_EVIDENCE must fail
        r_untargeted = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "accepted_exception",
                "reviewer_name": "Reviewer",
                "reviewer_notes": "Blanket acceptance without target rule.",
                "update_submission_status": "compliant",
            },
        )
        self.assertEqual(r_untargeted.status_code, 422)

        # Valid targeted resolution on f_miss resolves remaining missing_evidence -> server calculates 'waived' (since f_wos was waived)
        r_ok = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "accepted_exception",
                "finding_id": "f_miss",
                "reviewer_name": "Compliance Officer",
                "reviewer_notes": "Verified full endorsement term addendum received separately.",
            },
        )
        self.assertEqual(r_ok.status_code, 200)
        self.assertEqual(r_ok.json()["overall_status"], "waived")

        # Standalone single MISSING_EVIDENCE submission resolves to 'compliant' only with explicit target
        sid_single = self._seed_submission(
            [
                _make_finding(
                    "f_only_miss",
                    "REQ_AI_DATES",
                    rule_type=RequirementType.ENDORSEMENT_PRESENCE.value,
                    status=ComplianceStatus.MISSING_EVIDENCE.value,
                    reason_code="MISSING_ENDORSEMENT_DATES",
                    requires_human_review=True,
                )
            ],
            ComplianceStatus.MISSING_EVIDENCE.value,
        )
        self.assertEqual(
            self.client.post(
                f"/api/v1/submissions/{sid_single}/review",
                json={
                    "review_status": "accepted_exception",
                    "reviewer_name": "Officer",
                    "reviewer_notes": "Untargeted.",
                    "update_submission_status": "compliant",
                },
            ).status_code,
            422,
        )
        r_single_ok = self.client.post(
            f"/api/v1/submissions/{sid_single}/review",
            json={
                "review_status": "accepted_exception",
                "rule_id": "REQ_AI_DATES",
                "reviewer_name": "Officer",
                "reviewer_notes": "Verified dates on signed policy binder.",
                "update_submission_status": "compliant",
            },
        )
        self.assertEqual(r_single_ok.status_code, 200)
        self.assertEqual(r_single_ok.json()["overall_status"], "compliant")

    def test_04_unable_to_determine_protected_until_valid_per_finding_resolution(self):
        """4. UNABLE_TO_DETERMINE cannot become compliant without explicit individual resolution."""
        findings = [
            _make_finding(
                "f_utd",
                "REQ_GL_LIMIT_UTD",
                rule_type=RequirementType.COVERAGE_LIMIT.value,
                status=ComplianceStatus.UNABLE_TO_DETERMINE.value,
                reason_code="OCR_UNREADABLE_LIMIT",
                requires_human_review=True,
            ),
            _make_finding(
                "f_hr",
                "REQ_AI_FLAG",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE.value,
                status=ComplianceStatus.REQUIRES_HUMAN_REVIEW.value,
                reason_code="MANUSCRIPT_VERIFICATION_UNSUPPORTED",
                requires_human_review=True,
            ),
        ]
        sid = self._seed_submission(findings, ComplianceStatus.UNABLE_TO_DETERMINE.value)

        # Untargeted acceptance blocked
        self.assertEqual(
            self.client.post(
                f"/api/v1/submissions/{sid}/review",
                json={
                    "review_status": "accepted_exception",
                    "reviewer_name": "Officer",
                    "reviewer_notes": "Blanket attempt.",
                },
            ).status_code,
            422,
        )

        # Resolving only f_hr leaves server status at unable_to_determine
        r1 = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "accepted_exception",
                "finding_id": "f_hr",
                "reviewer_name": "Officer",
                "reviewer_notes": "Reviewed AI endorsement language.",
            },
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()["overall_status"], "unable_to_determine")

        # Trying compliant while f_utd unresolved is still blocked
        self.assertEqual(
            self.client.post(
                f"/api/v1/submissions/{sid}/review",
                json={
                    "review_status": "deficiency_notice_required",
                    "reviewer_name": "Officer",
                    "reviewer_notes": "Invalid compliant status spoof.",
                    "update_submission_status": "compliant",
                },
            ).status_code,
            422,
        )

        # Resolving f_utd specifically allows compliant
        r2 = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "accepted_exception",
                "finding_id": "f_utd",
                "reviewer_name": "Officer",
                "reviewer_notes": "Confirmed $1,000,000 limit from clear scan.",
            },
        )
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["overall_status"], "compliant")

    def test_05_requires_human_review_multi_finding_cannot_bypass_individually(self):
        """5. Multiple REQUIRES_HUMAN_REVIEW findings cannot be dismissed by blanket or partial approval."""
        findings = [
            _make_finding(
                "f_hr1",
                "REQ_AI_1",
                status=ComplianceStatus.REQUIRES_HUMAN_REVIEW.value,
                reason_code="BLANKET_ENDORSEMENT_REQUIRES_CONTRACT_CHECK",
                requires_human_review=True,
            ),
            _make_finding(
                "f_hr2",
                "REQ_ENTITY_2",
                rule_type=RequirementType.NAMED_INSURED.value,
                status=ComplianceStatus.REQUIRES_HUMAN_REVIEW.value,
                reason_code="CORPORATE_SUFFIX_DISCREPANCY",
                requires_human_review=True,
            ),
        ]
        sid = self._seed_submission(findings, ComplianceStatus.REQUIRES_HUMAN_REVIEW.value)

        # Blanket accepted_exception without target fails 422 when >1 blocking findings exist
        r_blanket = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "accepted_exception",
                "reviewer_name": "Officer",
                "reviewer_notes": "Blanket dismissal.",
                "update_submission_status": "compliant",
            },
        )
        self.assertEqual(r_blanket.status_code, 422)

        # Resolve first flag only -> still requires_human_review
        r_partial = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "accepted_exception",
                "finding_id": "f_hr1",
                "reviewer_name": "Officer",
                "reviewer_notes": "Contract requires AI.",
            },
        )
        self.assertEqual(r_partial.status_code, 200)
        self.assertEqual(r_partial.json()["overall_status"], "requires_human_review")

        # Resolving second flag completes review -> compliant
        r_final = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "accepted_exception",
                "finding_id": "f_hr2",
                "reviewer_name": "Officer",
                "reviewer_notes": "Verified LLC vs Inc DBA filing.",
            },
        )
        self.assertEqual(r_final.status_code, 200)
        self.assertEqual(r_final.json()["overall_status"], "compliant")

    def test_06_valid_waiver_applies_strictly_to_designated_finding_only(self):
        """6. Waiving one finding leaves unrelated non_compliant and requires_human_review untouched."""
        findings = [
            _make_finding("f_wos", "REQ_WOS", status=ComplianceStatus.NON_COMPLIANT.value, reason_code="ENDORSEMENT_NOT_FOUND"),
            _make_finding("f_agg", "REQ_GL_AGG", rule_type=RequirementType.COVERAGE_LIMIT.value, status=ComplianceStatus.NON_COMPLIANT.value, reason_code="LIMIT_BELOW_MINIMUM"),
            _make_finding("f_hr", "REQ_AI", status=ComplianceStatus.REQUIRES_HUMAN_REVIEW.value, reason_code="MANUSCRIPT_VERIFICATION_UNSUPPORTED", requires_human_review=True),
        ]
        sid = self._seed_submission(findings, ComplianceStatus.NON_COMPLIANT.value)

        r1 = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "waived",
                "finding_id": "f_wos",
                "reviewer_name": "Risk Director",
                "reviewer_notes": "Waived WOS only.",
            },
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()["review"]["finding_id"], "f_wos")
        self.assertEqual(r1.json()["overall_status"], "non_compliant")

        r2 = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "waived",
                "finding_id": "f_agg",
                "reviewer_name": "Risk Director",
                "reviewer_notes": "Waived GL Aggregate shortfall.",
            },
        )
        self.assertEqual(r2.status_code, 200)
        # f_hr is STILL unresolved
        self.assertEqual(r2.json()["overall_status"], "requires_human_review")

    def test_07_non_waivable_rules_and_reason_codes_rejected(self):
        """7. Attempting to waive non-waivable rules or reason codes is always rejected with 422."""
        non_waivable_cases = [
            ("policy_expiration", "POLICY_EXPIRED"),
            ("policy_effective_date", "POLICY_FUTURE_INCEPTION"),
            ("named_insured", "NAMED_INSURED_MISMATCH"),
            ("entity_identity", "CONFIRMED_ENTITY_MISMATCH"),
            ("coverage_limit", "COVERAGE_NOT_FOUND"),
            ("endorsement_presence", "ENDORSEMENT_EXPIRED"),
            ("endorsement_presence", "ENDORSEMENT_FUTURE_INCEPTION"),
            ("policy_expiration", "INVALID_DATE_RANGE_INVERTED"),
        ]
        for rule_type, reason_code in non_waivable_cases:
            sid = self._seed_submission(
                [
                    _make_finding(
                        "f_nw",
                        "REQ_NON_WAIVABLE",
                        rule_type=rule_type,
                        status=ComplianceStatus.NON_COMPLIANT.value,
                        reason_code=reason_code,
                    )
                ],
                ComplianceStatus.NON_COMPLIANT.value,
            )
            res = self.client.post(
                f"/api/v1/submissions/{sid}/review",
                json={
                    "review_status": "waived",
                    "finding_id": "f_nw",
                    "reviewer_name": "Risk Director",
                    "reviewer_notes": f"Attempting forbidden waiver for {reason_code}.",
                },
            )
            self.assertEqual(res.status_code, 422, f"Expected 422 for non-waivable ({rule_type}, {reason_code})")
            self.assertEqual(len(repository.get_submission_by_id(sid)["reviews"]), 0)

    def test_08_unresolved_blocking_finding_blocks_compliant_even_if_caller_spoofs_status(self):
        """8. Server calculates final status independently and blocks compliant override for every blocking state."""
        for blocking_status, reason in [
            (ComplianceStatus.NON_COMPLIANT.value, "LIMIT_BELOW_MINIMUM"),
            (ComplianceStatus.MISSING_EVIDENCE.value, "MISSING_POLICY_DATES"),
            (ComplianceStatus.UNABLE_TO_DETERMINE.value, "UNPARSEABLE_LIMIT"),
            (ComplianceStatus.REQUIRES_HUMAN_REVIEW.value, "LOW_CONFIDENCE"),
        ]:
            findings = [
                _make_finding("f_pass", "REQ_PASS", status=ComplianceStatus.COMPLIANT.value, reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM"),
                _make_finding("f_blk", "REQ_BLOCK", rule_type=RequirementType.COVERAGE_LIMIT.value, status=blocking_status, reason_code=reason, requires_human_review=True),
            ]
            sid = self._seed_submission(findings, blocking_status)

            # Even targeting an already-compliant finding with update_submission_status="compliant" must fail
            res = self.client.post(
                f"/api/v1/submissions/{sid}/review",
                json={
                    "review_status": "accepted_exception",
                    "finding_id": "f_pass",
                    "reviewer_name": "Spoofer",
                    "reviewer_notes": "Targeting compliant rule to force overall compliant.",
                    "update_submission_status": "compliant",
                },
            )
            self.assertEqual(res.status_code, 422, f"Should block spoof for {blocking_status}")
            self.assertEqual(repository.get_submission_by_id(sid)["overall_status"], blocking_status)

    def test_09_already_compliant_submission_continues_working(self):
        """9. An already-compliant submission continues supporting review sign-off normally."""
        sid = self._seed_submission(
            [_make_finding("f_ok", "REQ_GL_OCC", status=ComplianceStatus.COMPLIANT.value, reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM")],
            ComplianceStatus.COMPLIANT.value,
        )
        res = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "accepted_exception",
                "reviewer_name": "QA Lead",
                "reviewer_notes": "Final audit sign-off on clean submission.",
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["overall_status"], "compliant")
        self.assertEqual(len(repository.get_submission_by_id(sid)["reviews"]), 1)

    def test_10_deficiency_and_rejection_workflows_remain_functional(self):
        """10. Existing deficiency and rejection workflows function properly and re-open prior resolutions if targeted."""
        findings = [
            _make_finding("f_hr", "REQ_AI", status=ComplianceStatus.REQUIRES_HUMAN_REVIEW.value, reason_code="BLANKET_CHECK", requires_human_review=True),
        ]
        sid = self._seed_submission(findings, ComplianceStatus.REQUIRES_HUMAN_REVIEW.value)

        # Resolve first -> compliant
        r1 = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "accepted_exception",
                "finding_id": "f_hr",
                "reviewer_name": "Analyst A",
                "reviewer_notes": "Initially accepted.",
            },
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()["overall_status"], "compliant")

        # Audit correction issues deficiency notice -> deficiency_pending
        r2 = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "deficiency_notice_required",
                "finding_id": "f_hr",
                "reviewer_name": "Supervisor B",
                "reviewer_notes": "Wrong project addendum attached; deficiency issued.",
                "update_submission_status": "deficiency_pending",
            },
        )
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["overall_status"], "deficiency_pending")

        # Rejection sets rejected even if update_submission_status omitted
        r3 = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "rejected",
                "reviewer_name": "Supervisor B",
                "reviewer_notes": "Vendor failed to cure deficiency within deadline.",
            },
        )
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(r3.json()["overall_status"], "rejected")

    def test_11_failed_persistence_rolls_back_atomically_with_no_partial_state(self):
        """11. DB failure during submission status update rolls back the review_records insert cleanly."""
        sid = self._seed_submission(
            [_make_finding("f_hr", "REQ_AI", status=ComplianceStatus.REQUIRES_HUMAN_REVIEW.value, reason_code="BLANKET_CHECK", requires_human_review=True)],
            ComplianceStatus.REQUIRES_HUMAN_REVIEW.value,
        )

        # Install a real SQLite trigger that aborts UPDATE on submissions right after INSERT INTO review_records succeeds
        conn = get_connection(self.db_path)
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TRIGGER trg_fail_submission_update
                    BEFORE UPDATE ON submissions
                    BEGIN
                        SELECT RAISE(ABORT, 'simulated persistence failure on submission update');
                    END;
                    """
                )
        finally:
            conn.close()

        failing_client = TestClient(app, raise_server_exceptions=False)
        res = failing_client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "accepted_exception",
                "finding_id": "f_hr",
                "reviewer_name": "Auditor",
                "reviewer_notes": "Testing atomic rollback.",
            },
        )
        self.assertEqual(res.status_code, 500)

        # Verify ZERO partial review_records remain and overall_status is untouched
        persisted = repository.get_submission_by_id(sid)
        self.assertEqual(persisted["overall_status"], "requires_human_review")
        self.assertEqual(len(persisted["reviews"]), 0)

    def test_12_audit_records_and_original_evaluation_remain_immutable(self):
        """12. Append-only audit records capture exact finding/reason/reviewer and never mutate result_json."""
        findings = [
            _make_finding("f_wos", "REQ_WOS", status=ComplianceStatus.NON_COMPLIANT.value, reason_code="ENDORSEMENT_NOT_FOUND"),
            _make_finding("f_hr", "REQ_AI", status=ComplianceStatus.REQUIRES_HUMAN_REVIEW.value, reason_code="MANUSCRIPT_VERIFICATION_UNSUPPORTED", requires_human_review=True),
        ]
        sid = self._seed_submission(findings, ComplianceStatus.NON_COMPLIANT.value)
        snapshot_before = repository.get_submission_by_id(sid)["result"]

        self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "waived",
                "rule_id": "REQ_WOS",
                "reviewer_name": "Risk Director",
                "reviewer_notes": "Variance #901 approved.",
            },
        )
        self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "accepted_exception",
                "rule_id": "REQ_AI",
                "reviewer_name": "Senior Counsel",
                "reviewer_notes": "Manually confirmed ISO CG 20 10 wording.",
            },
        )

        sub_after = repository.get_submission_by_id(sid)
        self.assertEqual(sub_after["result"], snapshot_before)
        self.assertEqual(sub_after["overall_status"], "waived")
        self.assertEqual(len(sub_after["reviews"]), 2)
        self.assertEqual(sub_after["reviews"][0]["finding_id"], "f_hr")
        self.assertEqual(sub_after["reviews"][0]["reason_code"], "MANUSCRIPT_VERIFICATION_UNSUPPORTED")
        self.assertEqual(sub_after["reviews"][1]["finding_id"], "f_wos")
        self.assertEqual(sub_after["reviews"][1]["reason_code"], "ENDORSEMENT_NOT_FOUND")

    def test_13_conflicting_or_ambiguous_target_identifiers_rejected(self):
        """13. Conflicting finding_id/rule_id, duplicate rule_id ambiguity, and unknown IDs are rejected."""
        findings = [
            _make_finding("f_dup_1", "REQ_DUP", status=ComplianceStatus.REQUIRES_HUMAN_REVIEW.value, reason_code="REASON_1", requires_human_review=True),
            _make_finding("f_dup_2", "REQ_DUP", status=ComplianceStatus.REQUIRES_HUMAN_REVIEW.value, reason_code="REASON_2", requires_human_review=True),
        ]
        sid = self._seed_submission(findings, ComplianceStatus.REQUIRES_HUMAN_REVIEW.value)

        # Ambiguous rule_id matching 2 findings without finding_id
        r_amb = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "accepted_exception",
                "rule_id": "REQ_DUP",
                "reviewer_name": "Officer",
                "reviewer_notes": "Ambiguous target.",
            },
        )
        self.assertEqual(r_amb.status_code, 422)

        # Conflicting rule_id vs finding_id
        r_conf = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "accepted_exception",
                "finding_id": "f_dup_1",
                "rule_id": "REQ_NONEXISTENT",
                "reviewer_name": "Officer",
                "reviewer_notes": "Conflicting target.",
            },
        )
        self.assertEqual(r_conf.status_code, 404)

        # Disambiguated with exact finding_id resolves one at a time
        r_one = self.client.post(
            f"/api/v1/submissions/{sid}/review",
            json={
                "review_status": "accepted_exception",
                "finding_id": "f_dup_1",
                "rule_id": "REQ_DUP",
                "reviewer_name": "Officer",
                "reviewer_notes": "Resolved first finding only.",
            },
        )
        self.assertEqual(r_one.status_code, 200)
        self.assertEqual(r_one.json()["overall_status"], "requires_human_review")


if __name__ == "__main__":
    unittest.main()
