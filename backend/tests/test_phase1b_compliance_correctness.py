"""Phase 1B Compliance Evaluation Correctness & Evidence Grounding Tests.

Covers:
- ISS-09: Multi-coverage matching, explicit target_policy_number linkage, no cross-policy borrowing
- ISS-10: Source verification of named_insured and endorsement named_entities
- ISS-11: Mandatory evidence enforcement (even when mandatory_evidence=False) and requirement-profile validation
- ISS-12: Decimal currency parsing, ambiguous/impossible date handling, and non-substring evidence validation
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import List
from uuid import uuid4

import pymupdf
from fastapi.testclient import TestClient

from app.compliance.evaluator import ComplianceEvaluator
from app.documents.processor import PDFProcessor
from app.extraction.normalizer import (
    is_ambiguous_numeric_date,
    normalize_currency_to_cents,
    normalize_date,
)
from app.extraction.pipeline import (
    ExtractionPipelineService,
    _verify_date_field_support,
    _verify_entity_semantic_support,
    _verify_money_field_support,
)
from app.extraction.providers.base import BaseExtractionProvider
from app.main import app, repository
from app.persistence.database import init_db
from tests.fixtures.auth_helpers import authenticate_session_client
from app.schemas.insurance import (
    ComplianceStatus,
    CoverageExtraction,
    CoverageType,
    DateValue,
    DocumentType,
    EndorsementExtraction,
    EndorsementType,
    EntityInfo,
    EvidenceReference,
    ExtractedDocumentSet,
    ExtractionStatus,
    InsuranceRequirement,
    LimitField,
    MoneyValue,
    RequirementType,
    Severity,
    validate_requirement_profile,
)
from app.schemas.pdf import PDFPageInspection
from app.schemas.raw_extraction import (
    RawCoverageSection,
    RawEndorsement,
    RawEntityExtraction,
    RawEvidenceQuote,
    RawInsuranceExtraction,
)
from tests.fixtures.sample_data import EVAL_DATE, make_evidence, sample_gl_coverage, sample_subcontractor_document_set


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
        return "phase1b-scripted"

    def extract_insurance_data(
        self,
        document_id: str,
        pages: List[PDFPageInspection],
        timeout_seconds: float = 30.0,
    ) -> RawInsuranceExtraction:
        return self._extraction


def _cov(
    *,
    cov_type: CoverageType = CoverageType.GENERAL_LIABILITY,
    policy_number: str | None = "GL-100",
    eff: date | None = date(2025, 1, 1),
    exp: date | None = date(2026, 1, 1),
    occ_cents: int | None = 100_000_000,
    agg_cents: int | None = 200_000_000,
    status: ExtractionStatus = ExtractionStatus.EXTRACTED,
    confidence: float = 0.95,
    doc_id: str = "doc_coi",
) -> CoverageExtraction:
    eff_str = eff.strftime("%m/%d/%Y") if eff else None
    exp_str = exp.strftime("%m/%d/%Y") if exp else None
    date_quote_parts = []
    if eff_str:
        date_quote_parts.append(f"POLICY EFF: {eff_str}")
    if exp_str:
        date_quote_parts.append(f"POLICY EXP: {exp_str}")
    date_quote = " ".join(date_quote_parts) or f"POLICY {policy_number or 'UNLINKED'}"

    return CoverageExtraction(
        coverage_type=cov_type,
        policy_number=policy_number,
        each_occurrence=(
            MoneyValue(
                amount_cents=occ_cents,
                raw_text=f"${occ_cents / 100:,.0f}",
                confidence=confidence,
                evidence=make_evidence(doc_id=doc_id, text=f"EACH OCCURRENCE: ${occ_cents / 100:,.0f}"),
            )
            if occ_cents is not None
            else None
        ),
        general_aggregate=(
            MoneyValue(
                amount_cents=agg_cents,
                raw_text=f"${agg_cents / 100:,.0f}",
                confidence=confidence,
                evidence=make_evidence(doc_id=doc_id, text=f"GENERAL AGGREGATE: ${agg_cents / 100:,.0f}"),
            )
            if agg_cents is not None
            else None
        ),
        effective_date=(
            DateValue(
                date=eff,
                raw_text=eff_str,
                confidence=confidence,
                evidence=make_evidence(doc_id=doc_id, text=date_quote),
            )
            if eff is not None
            else None
        ),
        expiration_date=(
            DateValue(
                date=exp,
                raw_text=exp_str,
                confidence=confidence,
                evidence=make_evidence(doc_id=doc_id, text=date_quote),
            )
            if exp is not None
            else None
        ),
        evidence=make_evidence(doc_id=doc_id, text=f"EACH OCCURRENCE: ${occ_cents / 100:,.0f}" if occ_cents else date_quote),
        extraction_status=status,
        confidence=confidence,
    )


# =============================================================================
# ISS-09: Multiple Matching Coverages & Policy Linkage
# =============================================================================

class TestISS09MultipleMatchingCoverages(unittest.TestCase):
    def setUp(self):
        self.evaluator = ComplianceEvaluator()
        self.eval_date = EVAL_DATE  # 2025-06-15

    def _exp_req(self, target_policy: str | None = None) -> InsuranceRequirement:
        return InsuranceRequirement(
            requirement_id="REQ_GL_EXP",
            rule_type=RequirementType.POLICY_EXPIRATION,
            description="GL policy active",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            target_policy_number=target_policy,
        )

    def _eff_req(self, target_policy: str | None = None) -> InsuranceRequirement:
        return InsuranceRequirement(
            requirement_id="REQ_GL_EFF",
            rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
            description="GL policy effective",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            target_policy_number=target_policy,
        )

    def test_first_expired_second_valid_without_linkage_escalates_instead_of_failing_or_guessing(self):
        cov_expired = _cov(policy_number="GL-OLD", eff=date(2024, 1, 1), exp=date(2025, 1, 1))
        cov_valid = _cov(policy_number="GL-NEW", eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        doc_set = sample_subcontractor_document_set()
        doc_set.coverages = [cov_expired, cov_valid]

        res = self.evaluator.evaluate(doc_set, [self._exp_req()], self.eval_date)
        self.assertEqual(res.findings[0].status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(res.findings[0].reason_code, "AMBIGUOUS_APPLICABLE_POLICY")

    def test_first_valid_second_expired_without_linkage_cannot_silently_pass(self):
        cov_valid = _cov(policy_number="GL-NEW", eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        cov_expired = _cov(policy_number="GL-OLD", eff=date(2024, 1, 1), exp=date(2025, 1, 1))
        doc_set = sample_subcontractor_document_set()
        doc_set.coverages = [cov_valid, cov_expired]

        res = self.evaluator.evaluate(doc_set, [self._exp_req()], self.eval_date)
        self.assertNotEqual(res.findings[0].status, ComplianceStatus.COMPLIANT)
        self.assertEqual(res.findings[0].status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(res.findings[0].reason_code, "AMBIGUOUS_APPLICABLE_POLICY")

    def test_two_distinct_policies_do_not_combine_dates_to_manufacture_compliance(self):
        # Policy A has only effective date; Policy B has only expiration date
        cov_eff_only = _cov(policy_number="GL-A", eff=date(2025, 1, 1), exp=None)
        cov_exp_only = _cov(policy_number="GL-B", eff=None, exp=date(2026, 1, 1))
        doc_set = sample_subcontractor_document_set()
        doc_set.coverages = [cov_eff_only, cov_exp_only]

        res = self.evaluator.evaluate(doc_set, [self._eff_req(), self._exp_req()], self.eval_date)
        self.assertNotEqual(res.overall_status, ComplianceStatus.COMPLIANT)
        self.assertTrue(all(f.status != ComplianceStatus.COMPLIANT for f in res.findings))

    def test_explicit_target_policy_linkage_ignores_unrelated_expired_policy(self):
        cov_expired = _cov(policy_number="GL-OLD", eff=date(2024, 1, 1), exp=date(2025, 1, 1))
        cov_valid = _cov(policy_number="GL-NEW", eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        doc_set = sample_subcontractor_document_set()
        doc_set.coverages = [cov_expired, cov_valid]

        res = self.evaluator.evaluate(
            doc_set,
            [self._eff_req(target_policy="GL-NEW"), self._exp_req(target_policy="GL-NEW")],
            self.eval_date,
        )
        self.assertEqual(res.overall_status, ComplianceStatus.COMPLIANT)
        self.assertEqual(res.findings[0].reason_code, "EFFECTIVE_DATE_VALID")
        self.assertEqual(res.findings[1].reason_code, "POLICY_ACTIVE_AND_CURRENT")

    def test_target_policy_cannot_borrow_date_from_unrelated_policy(self):
        cov_with_date = _cov(policy_number="GL-OLD", eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        cov_missing_exp = _cov(policy_number="GL-NEW", eff=date(2025, 1, 1), exp=None)
        doc_set = sample_subcontractor_document_set()
        doc_set.coverages = [cov_with_date, cov_missing_exp]

        res = self.evaluator.evaluate(doc_set, [self._exp_req(target_policy="GL-NEW")], self.eval_date)
        self.assertEqual(res.findings[0].status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(res.findings[0].reason_code, "EXPIRATION_DATE_MISSING")

    def test_unlinked_ambiguous_coverages_flag_review(self):
        cov1 = _cov(policy_number=None, eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        cov2 = _cov(policy_number=None, eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        doc_set = sample_subcontractor_document_set()
        doc_set.coverages = [cov1, cov2]

        res = self.evaluator.evaluate(doc_set, [self._exp_req()], self.eval_date)
        self.assertEqual(res.findings[0].status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(res.findings[0].reason_code, "AMBIGUOUS_APPLICABLE_POLICY")

    def test_conflicting_dates_for_same_policy_returns_contradictory_dates(self):
        cov1 = _cov(policy_number="GL-100", eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        cov2 = _cov(policy_number="GL-100", eff=date(2025, 2, 1), exp=date(2026, 6, 1))
        doc_set = sample_subcontractor_document_set()
        doc_set.coverages = [cov1, cov2]

        res = self.evaluator.evaluate(doc_set, [self._eff_req(), self._exp_req()], self.eval_date)
        self.assertEqual(res.findings[0].status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(res.findings[0].reason_code, "CONTRADICTORY_POLICY_DATES")
        self.assertEqual(res.findings[1].status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(res.findings[1].reason_code, "CONTRADICTORY_POLICY_DATES")

    def test_multiple_coverages_limit_inspects_second_uncertain_coverage(self):
        cov1 = _cov(policy_number="GL-100", occ_cents=100_000_000, status=ExtractionStatus.EXTRACTED)
        cov2 = _cov(policy_number="GL-100", occ_cents=100_000_000, status=ExtractionStatus.UNCERTAIN, confidence=0.40)
        doc_set = sample_subcontractor_document_set()
        doc_set.coverages = [cov1, cov2]

        req = InsuranceRequirement(
            requirement_id="REQ_GL_OCC",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="GL Occ >= $1M",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            limit_field=LimitField.EACH_OCCURRENCE,
            min_amount_cents=100_000_000,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        self.assertEqual(res.findings[0].status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(res.findings[0].reason_code, "EXTRACTION_CONFIDENCE_LOW")

    def test_missing_target_policy_when_other_policy_exists_returns_target_not_found(self):
        cov_other = _cov(policy_number="GL-OTHER", eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        doc_set = sample_subcontractor_document_set()
        doc_set.coverages = [cov_other]

        res = self.evaluator.evaluate(doc_set, [self._exp_req(target_policy="GL-TARGET")], self.eval_date)
        self.assertEqual(res.findings[0].status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(res.findings[0].reason_code, "TARGET_POLICY_NOT_FOUND")


# =============================================================================
# ISS-10: Source Verification of Named Entities
# =============================================================================

class TestISS10NamedEntityVerification(unittest.TestCase):
    def setUp(self):
        self.processor = PDFProcessor(render_dpi=72)
        self.evaluator = ComplianceEvaluator()
        self.eval_date = EVAL_DATE

    def test_exact_and_harmless_formatting_entity_supported(self):
        self.assertTrue(_verify_entity_semantic_support("Apex Electrical Contractors LLC", "INSURED: Apex Electrical Contractors LLC"))
        self.assertTrue(_verify_entity_semantic_support("Apex Electrical Contractors, L.L.C.", "INSURED: Apex Electrical Contractors LLC"))
        self.assertTrue(_verify_entity_semantic_support("General Contractor Corp.", "Additional Insured: General Contractor Corporation"))

    def test_similar_distinct_company_names_rejected_no_single_token_match(self):
        self.assertFalse(_verify_entity_semantic_support("Apex Construction Group LLC", "INSURED: Apex Electrical Contractors LLC"))
        self.assertFalse(_verify_entity_semantic_support("Apex Electrical Contractors Inc", "INSURED: Apex Electrical Contractors LLC"))

    def test_fabricated_endorsement_named_entity_does_not_get_entity_evidence_and_cannot_pass(self):
        pdf_bytes = _make_pdf_bytes(
            "ENDORSEMENT CG 20 10 04 13\n"
            "This endorsement modifies Commercial General Liability Policy GL-98765432.\n"
            "Additional Insured: Owners, Lessees or Contractors - Scheduled Person.\n"
            "Scheduled Entity: Unrelated Builder Inc\n"
        )
        insp = self.processor.process_pdf(pdf_bytes, document_id="doc_end_fab")
        scripted = RawInsuranceExtraction(
            document_type="endorsement",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    named_entities=["General Contractor Corp"],  # Fabricated! Not in PDF!
                    is_standard_form=True,
                    manuscript_clause_detected=False,
                    manuscript_status="NOT_DETECTED",
                    confidence=0.95,
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="Additional Insured: Owners, Lessees or Contractors - Scheduled Person.",
                    ),
                )
            ],
        )
        pipe_res = ExtractionPipelineService(_ScriptedRawProvider(scripted)).process_inspection(insp, "sub_fab")
        self.assertIsNotNone(pipe_res.document_set)
        end = pipe_res.document_set.endorsements[0]
        # Original claimed named_entities preserved for auditability, but entity_evidence is empty
        self.assertEqual(end.named_entities, ["General Contractor Corp"])
        self.assertEqual(end.entity_evidence, ())

        # Evaluator cannot pass as COMPLIANT even if independent_integrity_verified were set
        end_verified_form = end.model_copy(update={"independent_integrity_verified": True})
        doc_set = sample_subcontractor_document_set(endorsements=[end_verified_form])
        req = InsuranceRequirement(
            requirement_id="REQ_AI_ENT",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="AI naming General Contractor Corp",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            required_entity_name="General Contractor Corp",
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        self.assertEqual(res.findings[0].status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(res.findings[0].reason_code, "ENDORSEMENT_ENTITY_EVIDENCE_UNVERIFIED")

    def test_source_supported_endorsement_named_entity_populates_entity_evidence_and_passes(self):
        pdf_bytes = _make_pdf_bytes(
            "ENDORSEMENT CG 20 10 04 13\n"
            "Additional Insured - Scheduled Person or Organization: General Contractor Corp\n"
        )
        insp = self.processor.process_pdf(pdf_bytes, document_id="doc_end_ok")
        scripted = RawInsuranceExtraction(
            document_type="endorsement",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    named_entities=["General Contractor Corp"],
                    is_standard_form=True,
                    manuscript_clause_detected=False,
                    manuscript_status="NOT_DETECTED",
                    confidence=0.95,
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="Additional Insured - Scheduled Person or Organization: General Contractor Corp",
                    ),
                )
            ],
        )
        pipe_res = ExtractionPipelineService(_ScriptedRawProvider(scripted)).process_inspection(insp, "sub_ok")
        end = pipe_res.document_set.endorsements[0]
        self.assertEqual(len(end.entity_evidence), 1)
        self.assertIn("General Contractor Corp", end.entity_evidence[0].source_text)

        end_verified = end.model_copy(update={"independent_integrity_verified": True})
        doc_set = sample_subcontractor_document_set(endorsements=[end_verified])
        req = InsuranceRequirement(
            requirement_id="REQ_AI_ENT",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="AI naming General Contractor Corp",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            required_entity_name="General Contractor Corp",
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        self.assertEqual(res.findings[0].status, ComplianceStatus.COMPLIANT)

    def test_named_insured_with_unrelated_or_missing_evidence_flagged(self):
        req = InsuranceRequirement(
            requirement_id="REQ_NI",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named Insured",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        # Unrelated quote text
        doc_bad_quote = sample_subcontractor_document_set()
        doc_bad_quote.named_insured = EntityInfo(
            name="Apex Electrical Contractors LLC",
            evidence=make_evidence(text="CERTIFICATE HOLDER: General Contractor Corp"),
        )
        res1 = self.evaluator.evaluate(doc_bad_quote, [req], self.eval_date)
        self.assertEqual(res1.findings[0].status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(res1.findings[0].reason_code, "NAMED_INSURED_EVIDENCE_SUPPORT_UNVERIFIED")

        # Missing evidence
        doc_no_ev = sample_subcontractor_document_set()
        doc_no_ev.named_insured = EntityInfo(name="Apex Electrical Contractors LLC", evidence=None)
        res2 = self.evaluator.evaluate(doc_no_ev, [req], self.eval_date)
        self.assertEqual(res2.findings[0].status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(res2.findings[0].reason_code, "NAMED_INSURED_EVIDENCE_MISSING")

    def test_multiple_and_conflicting_named_insured_entities_across_documents(self):
        req = InsuranceRequirement(
            requirement_id="REQ_NI",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named Insured",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        doc_set = sample_subcontractor_document_set()
        doc_set.additional_named_insureds = [
            EntityInfo(
                name="Apex Mechanical Plumbing Inc",
                evidence=make_evidence(doc_id="doc_end", doc_type=DocumentType.ENDORSEMENT, text="INSURED: Apex Mechanical Plumbing Inc"),
            )
        ]
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        self.assertEqual(res.findings[0].status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(res.findings[0].reason_code, "CONFLICTING_NAMED_INSURED_ENTITIES")


# =============================================================================
# ISS-11: Mandatory Evidence & Requirement Profile Validation
# =============================================================================

class TestISS11MandatoryEvidenceAndProfileValidation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / f"iss11_{uuid4().hex[:8]}.db"
        self.upload_dir = Path(self.temp_dir.name) / "uploads"
        init_db(self.db_path)
        self._orig_db = repository.db_path
        self._orig_env_db = os.environ.get("SUBGUARD_DB_PATH")
        self._orig_env_up = os.environ.get("SUBGUARD_UPLOAD_DIR")
        os.environ["SUBGUARD_DB_PATH"] = str(self.db_path)
        os.environ["SUBGUARD_UPLOAD_DIR"] = str(self.upload_dir)
        repository.db_path = self.db_path
        self.client = TestClient(app)
        authenticate_session_client(self.client, db_path=self.db_path, role="analyst")
        self.evaluator = ComplianceEvaluator()

    def tearDown(self):
        repository.db_path = self._orig_db
        if self._orig_env_db is None:
            os.environ.pop("SUBGUARD_DB_PATH", None)
        else:
            os.environ["SUBGUARD_DB_PATH"] = self._orig_env_db
        if self._orig_env_up is None:
            os.environ.pop("SUBGUARD_UPLOAD_DIR", None)
        else:
            os.environ["SUBGUARD_UPLOAD_DIR"] = self._orig_env_up
        self.temp_dir.cleanup()

    def test_omitted_requirements_uses_defaults_while_explicit_empty_is_rejected_422(self):
        coi_pdf = _make_pdf_bytes(
            "ACORD 25 (2016/03) CERTIFICATE OF LIABILITY INSURANCE\n"
            "INSURED: Apex Electrical Contractors LLC\n"
            "COMMERCIAL GENERAL LIABILITY POLICY NUMBER: GL-98765432\n"
            "POLICY EFF: 01/01/2025   POLICY EXP: 01/01/2026\n"
            "EACH OCCURRENCE: $1,000,000\nGENERAL AGGREGATE: $2,000,000\n"
        )
        # Omitted requirements -> uses defaults cleanly (5 defaults + injected GL effective date)
        r_omitted = self.client.post(
            "/api/v1/compliance/review-documents",
            files={"coi_file": ("coi.pdf", io.BytesIO(coi_pdf), "application/pdf")},
            data={"submission_name": "Default Profile Test", "evaluation_date": "2025-06-15"},
        )
        self.assertEqual(r_omitted.status_code, 200)
        req_ids = [f["requirement_id"] for f in r_omitted.json()["review_result"]["findings"]]
        self.assertIn("REQ_NAMED_INSURED", req_ids)
        self.assertIn("REQ_GENERAL_LIABILITY_EFFECTIVE_DATE", req_ids)

        # Explicit empty list "[]" -> MUST reject 422, NEVER silently fall back to defaults
        r_empty = self.client.post(
            "/api/v1/compliance/review-documents",
            files={"coi_file": ("coi.pdf", io.BytesIO(coi_pdf), "application/pdf")},
            data={"submission_name": "Empty Profile Test", "evaluation_date": "2025-06-15", "requirements": "[]"},
        )
        self.assertEqual(r_empty.status_code, 422)

        # Explicit empty string "" -> rejected 422
        r_blank = self.client.post(
            "/api/v1/compliance/review-documents",
            files={"coi_file": ("coi.pdf", io.BytesIO(coi_pdf), "application/pdf")},
            data={"submission_name": "Blank Profile Test", "evaluation_date": "2025-06-15", "requirements": "   "},
        )
        self.assertEqual(r_blank.status_code, 422)

    def test_duplicate_ids_invalid_ids_and_unsupported_rules_rejected(self):
        with self.assertRaises(ValueError):
            validate_requirement_profile([])

        req_a = InsuranceRequirement(
            requirement_id="REQ_DUP",
            rule_type=RequirementType.NAMED_INSURED,
            description="NI 1",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        req_b = InsuranceRequirement(
            requirement_id="REQ_DUP",
            rule_type=RequirementType.POLICY_EXPIRATION,
            description="Exp 2",
            coverage_type=CoverageType.GENERAL_LIABILITY,
        )
        with self.assertRaises(ValueError):
            validate_requirement_profile([req_a, req_b])

        # Via /api/v1/compliance/evaluate -> 422
        doc_set = sample_subcontractor_document_set()
        res_dup = self.client.post(
            "/api/v1/compliance/evaluate",
            json={
                "document_set": doc_set.model_dump(mode="json"),
                "requirements": [req_a.model_dump(mode="json"), req_b.model_dump(mode="json")],
                "evaluation_date": "2025-06-15",
            },
        )
        self.assertEqual(res_dup.status_code, 422)

        # Empty requirement_id rejected
        with self.assertRaises(ValueError):
            InsuranceRequirement(
                requirement_id="   ",
                rule_type=RequirementType.NAMED_INSURED,
                description="Blank ID",
                required_entity_name="Apex Electrical Contractors LLC",
            )

        # POLICY_EXPIRATION missing coverage_type rejected
        with self.assertRaises(ValueError):
            InsuranceRequirement(
                requirement_id="REQ_NO_COV",
                rule_type=RequirementType.POLICY_EXPIRATION,
                description="Missing coverage type",
            )

        # Unsupported rule_type rejected by validate_requirement_profile
        unsupported = InsuranceRequirement.model_construct(
            requirement_id="REQ_BAD",
            rule_type=RequirementType.COMPLETE_DOCUMENT_SET,
            description="Unsupported",
            severity=Severity.MINOR,
        )
        with self.assertRaises(ValueError):
            validate_requirement_profile([unsupported])

        # Direct ComplianceEvaluator.evaluate([]) raises ValueError
        with self.assertRaises(ValueError):
            self.evaluator.evaluate(doc_set, [], EVAL_DATE)

    def test_mandatory_evidence_false_cannot_bypass_core_rules_without_evidence(self):
        cov_no_ev = sample_gl_coverage()
        cov_no_ev.evidence = None
        cov_no_ev.each_occurrence.evidence = None
        cov_no_ev.effective_date.evidence = None
        cov_no_ev.expiration_date.evidence = None
        doc_set = sample_subcontractor_document_set(gl_coverage=cov_no_ev)
        doc_set.named_insured.evidence = None

        end_no_ev = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            independent_integrity_verified=True,
            evidence=None,
        )
        doc_set.endorsements = [end_no_ev]

        reqs = [
            InsuranceRequirement(
                requirement_id="R_LIM",
                rule_type=RequirementType.COVERAGE_LIMIT,
                description="Limit",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                limit_field=LimitField.EACH_OCCURRENCE,
                min_amount_cents=100_000_000,
                mandatory_evidence=False,
            ),
            InsuranceRequirement(
                requirement_id="R_EXP",
                rule_type=RequirementType.POLICY_EXPIRATION,
                description="Exp",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                mandatory_evidence=False,
            ),
            InsuranceRequirement(
                requirement_id="R_EFF",
                rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
                description="Eff",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                mandatory_evidence=False,
            ),
            InsuranceRequirement(
                requirement_id="R_NI",
                rule_type=RequirementType.NAMED_INSURED,
                description="NI",
                required_entity_name="Apex Electrical Contractors LLC",
                mandatory_evidence=False,
            ),
            InsuranceRequirement(
                requirement_id="R_END",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE,
                description="AI",
                required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                mandatory_evidence=False,
            ),
        ]
        res = self.evaluator.evaluate(doc_set, reqs, EVAL_DATE)
        self.assertEqual(res.overall_status, ComplianceStatus.MISSING_EVIDENCE)
        for finding in res.findings:
            self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE, f"Bypassed {finding.requirement_id}")
            self.assertEqual(finding.evidence, [])


# =============================================================================
# ISS-12: Currency, Date, and Evidence Validation Edge Cases
# =============================================================================

class TestISS12CurrencyDateEvidenceEdgeCases(unittest.TestCase):
    def test_currency_exact_decimal_and_rejection_cases(self):
        valid_cases = [
            ("$1,000,000", 100_000_000),
            ("1000000", 100_000_000),
            ("1,000.50", 100_050),
            ("1.5M", 150_000_000),
            ("2M", 200_000_000),
            ("$500K", 50_000_000),
            ("$1.15", 115),
            ("$19.99", 1_999),
            ("1.005M", 100_500_000),
        ]
        for raw, expected in valid_cases:
            cents, is_amb, _ = normalize_currency_to_cents(raw)
            self.assertFalse(is_amb, f"Unexpected ambiguity on {raw}")
            self.assertEqual(cents, expected, f"Mismatch on {raw}")

        invalid_or_ambiguous = [
            "-$1,000",
            "($1,000)",
            "-500K",
            "invalid currency",
            "$1,000 / $2,000",
            "$1,000.555",      # Excessive precision
            "$999,999.999",    # Must NOT round up to $1,000,000
            "1.234567891M",    # Sub-cent precision (9 decimal places > 10^-8 M)
            "1,00,000",        # Ambiguous grouping separators
            "1.000.000",       # Multiple decimal points
            "1.000,50",        # Ambiguous EU separator
        ]
        for raw in invalid_or_ambiguous:
            cents, is_amb, _ = normalize_currency_to_cents(raw)
            self.assertTrue(is_amb, f"Should flag invalid/ambiguous: {raw}")
            self.assertIsNone(cents, f"Should return None cents for: {raw}")

    def test_date_iso_us_ambiguous_impossible_and_partial(self):
        # ISO & unambiguous dates
        self.assertEqual(normalize_date("2025-06-30")[0], date(2025, 6, 30))
        self.assertEqual(normalize_date("01/15/2025")[0], date(2025, 1, 15))
        self.assertEqual(normalize_date("January 15, 2025")[0], date(2025, 1, 15))

        # Ambiguous numeric date detection
        self.assertTrue(is_ambiguous_numeric_date("01/02/2025"))
        self.assertFalse(is_ambiguous_numeric_date("01/15/2025"))
        self.assertFalse(is_ambiguous_numeric_date("2025-01-02"))

        # Under explicit US_MDY policy, parses consistently; without explicit policy (STRICT/None), fails closed
        d_us, amb_us, _ = normalize_date("01/02/2025", date_format_policy="US_MDY")
        self.assertFalse(amb_us)
        self.assertEqual(d_us, date(2025, 1, 2))

        for strict_policy in (None, "STRICT"):
            d_strict, amb_strict, reason = normalize_date("01/02/2025", date_format_policy=strict_policy)
            self.assertTrue(amb_strict, f"Policy {strict_policy} must flag 01/02/2025 ambiguous")
            self.assertIsNone(d_strict)
            self.assertIn("AMBIGUOUS", reason)

        # Impossible & partial dates rejected
        for bad_date in ("02/30/2025", "13/01/2025", "2025-02-29", "2025", "01/2025", "January 2025", "01/15/2025 extra"):
            d_bad, amb_bad, _ = normalize_date(bad_date)
            self.assertTrue(amb_bad, f"Should reject bad/partial date: {bad_date}")
            self.assertIsNone(d_bad)

    def test_weak_digit_or_substring_checks_do_not_falsely_verify_evidence(self):
        # Currency: 100,000 must NOT verify against $1,000,000 or Policy #GL-1000000
        self.assertFalse(_verify_money_field_support("each_occurrence", 10_000_000, "$100,000", "EACH OCCURRENCE: $1,000,000"))
        self.assertFalse(_verify_money_field_support("each_occurrence", 100_050, "$1,000.50", "EACH OCCURRENCE: $100,050"))
        self.assertFalse(_verify_money_field_support("each_occurrence", 100_000_000, "$1,000,000", "POLICY NUMBER: GL-10000002"))
        self.assertTrue(_verify_money_field_support("each_occurrence", 100_000_000, "$1,000,000", "EACH OCCURRENCE: $1M"))

        # Date: Jan 1 must NOT match Jan 31 via '1' in '31'
        self.assertFalse(_verify_date_field_support("effective", date(2025, 1, 1), "2025-01-01", "POLICY EFF: January 31, 2025"))
        self.assertTrue(_verify_date_field_support("effective", date(2025, 1, 1), "2025-01-01", "POLICY EFF: January 1, 2025"))

    def test_endorsement_date_digit_only_rejected_and_real_date_populates_date_evidence(self):
        # Digit-only policy number '0101202500' must NOT validate fabricated date '01/01/2025'
        pdf_fake = _make_pdf_bytes("ENDORSEMENT CG 20 10\nPolicy #0101202500\nAdditional Insured: General Contractor Corp\n")
        insp_fake = PDFProcessor(render_dpi=72).process_pdf(pdf_fake, document_id="doc_end_digit")
        raw_fake = RawInsuranceExtraction(
            document_type="endorsement",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10",
                    effective_date="01/01/2025",
                    evidence=RawEvidenceQuote(page_number=1, source_text="Policy #0101202500"),
                )
            ],
        )
        res_fake = ExtractionPipelineService(_ScriptedRawProvider(raw_fake)).process_inspection(insp_fake, "sub_fake")
        self.assertIsNone(res_fake.document_set.endorsements[0].effective_date)
        self.assertEqual(res_fake.document_set.endorsements[0].date_evidence, ())

        # Real date on endorsement populates date_evidence
        pdf_real = _make_pdf_bytes(
            "ENDORSEMENT CG 20 10 04 13\n"
            "Effective Date: January 1, 2025  Expiration Date: January 1, 2026\n"
            "Additional Insured: General Contractor Corp\n"
        )
        insp_real = PDFProcessor(render_dpi=72).process_pdf(pdf_real, document_id="doc_end_real")
        raw_real = RawInsuranceExtraction(
            document_type="endorsement",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    effective_date="2025-01-01",
                    expiration_date="2026-01-01",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="Effective Date: January 1, 2025  Expiration Date: January 1, 2026",
                    ),
                )
            ],
        )
        res_real = ExtractionPipelineService(_ScriptedRawProvider(raw_real)).process_inspection(insp_real, "sub_real")
        end_real = res_real.document_set.endorsements[0]
        self.assertEqual(end_real.effective_date, date(2025, 1, 1))
        self.assertEqual(end_real.expiration_date, date(2026, 1, 1))
        self.assertEqual(len(end_real.date_evidence), 1)


if __name__ == "__main__":
    unittest.main()
