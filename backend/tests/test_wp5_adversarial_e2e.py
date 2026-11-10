"""WP5 Adversarial End-to-End Validation & Release Readiness Test Suite.

Covers real multi-document workflow validation across WP5 Sections 3.A-3.E,
Section 4 (Production Application Workflow), and Section 6 (Explicit Validation
of Documented WP4 Limitations 1 & 2).

Zero live/paid AI API calls. Protected cohort_v1 files are read-only.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

import pymupdf
from fastapi.testclient import TestClient

from app.compliance.evaluator import ComplianceEvaluator
from app.persistence.database import init_db
from tests.fixtures.auth_helpers import authenticate_session_client
from app.compliance.reconciliation import (
    check_contractual_role_satisfaction,
    reconcile_dates,
    reconcile_entity_identities,
    reconcile_extracted_document_set,
)
from app.documents.processor import PDFProcessor
from app.documents.review_service import get_default_requirements
from app.extraction.ocr import BaseOCRProvider
from app.extraction.pipeline import ExtractionPipelineService
from app.extraction.providers.base import BaseExtractionProvider
from app.extraction.providers.mock import HonestApplicationMockProvider
from app.main import app, repository
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
)
from app.schemas.pdf import PDFPageInspection
from app.schemas.raw_extraction import (
    RawCoverageSection,
    RawEntityExtraction,
    RawEvidenceQuote,
    RawInsuranceExtraction,
)
from app.schemas.reconciliation import (
    DocumentRelationshipType,
    EdgeClassification,
    EntityRole,
    PolicyMatchClassification,
)


def _build_pdf_bytes(pages_text: List[str], blank_image_only: bool = False) -> bytes:
    """Create valid deterministic PDF bytes in memory via PyMuPDF."""
    doc = pymupdf.open()
    for text in pages_text:
        page = doc.new_page(width=612, height=792)
        if not blank_image_only and text.strip():
            page.insert_text((50, 72), text, fontsize=11)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


class _FixedRawProvider(BaseExtractionProvider):
    """Test extraction provider returning scripted RawInsuranceExtraction per document_id."""

    def __init__(self, payloads_by_doc_id: dict[str, RawInsuranceExtraction]):
        self._payloads = payloads_by_doc_id

    @property
    def provider_name(self) -> str:
        return "wp5-scripted-mock"

    def extract_insurance_data(
        self,
        document_id: str,
        pages: List[PDFPageInspection],
        timeout_seconds: float = 30.0,
    ) -> RawInsuranceExtraction:
        if document_id in self._payloads:
            return self._payloads[document_id]
        for key, payload in self._payloads.items():
            if key in document_id:
                return payload
        raise RuntimeError(f"No scripted payload for {document_id}")


class _FailingOCRProvider(BaseOCRProvider):
    """Deterministic failing OCR provider simulating unreadable/unrecoverable scanned pages."""

    @property
    def provider_name(self) -> str:
        return "wp5-failing-ocr"

    def perform_ocr_on_pages(self, pages: List[PDFPageInspection]) -> List[PDFPageInspection]:
        raise RuntimeError("Simulated OCR engine crash on unreadable scan")


def _base_compliant_coverages(doc_id: str) -> List[CoverageExtraction]:
    ev_gl_limit = EvidenceReference(
        document_id=doc_id,
        document_type=DocumentType.COI,
        page_number=1,
        source_text="GENERAL LIABILITY GL-98765432 EACH OCCURRENCE: $1,000,000 AGGREGATE: $2,000,000",
    )
    ev_gl_dates = EvidenceReference(
        document_id=doc_id,
        document_type=DocumentType.COI,
        page_number=1,
        source_text="Policy EFF: 2025-01-01 EXP: 2026-01-01",
    )
    return [
        CoverageExtraction(
            coverage_type=CoverageType.GENERAL_LIABILITY,
            policy_number="GL-98765432",
            each_occurrence=MoneyValue(amount_cents=100_000_000, raw_text="$1,000,000", confidence=0.98),
            general_aggregate=MoneyValue(amount_cents=200_000_000, raw_text="$2,000,000", confidence=0.98),
            effective_date=DateValue(date=date(2025, 1, 1), raw_text="2025-01-01", confidence=0.98),
            expiration_date=DateValue(date=date(2026, 1, 1), raw_text="2026-01-01", confidence=0.98),
            confidence=0.98,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=ev_gl_limit,
            date_evidence=[ev_gl_dates],
        ),
        CoverageExtraction(
            coverage_type=CoverageType.AUTO_LIABILITY,
            policy_number="AUTO-112233",
            combined_single_limit=MoneyValue(amount_cents=100_000_000, raw_text="$1,000,000", confidence=0.95),
            effective_date=DateValue(date=date(2025, 1, 1), raw_text="2025-01-01", confidence=0.95),
            expiration_date=DateValue(date=date(2026, 1, 1), raw_text="2026-01-01", confidence=0.95),
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=EvidenceReference(
                document_id=doc_id,
                document_type=DocumentType.COI,
                page_number=1,
                source_text="AUTO LIABILITY AUTO-112233 COMBINED SINGLE LIMIT: $1,000,000",
            ),
        ),
        CoverageExtraction(
            coverage_type=CoverageType.WORKERS_COMP,
            policy_number="WC-445566",
            per_accident=MoneyValue(amount_cents=100_000_000, raw_text="$1,000,000", confidence=0.95),
            effective_date=DateValue(date=date(2025, 1, 1), raw_text="2025-01-01", confidence=0.95),
            expiration_date=DateValue(date=date(2026, 1, 1), raw_text="2026-01-01", confidence=0.95),
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=EvidenceReference(
                document_id=doc_id,
                document_type=DocumentType.COI,
                page_number=1,
                source_text="WORKERS COMPENSATION WC-445566 PER ACCIDENT: $1,000,000",
            ),
        ),
    ]


class TestWP5AdversarialE2E(unittest.TestCase):
    """Adversarial end-to-end tests covering WP5 Sections 3, 4, and 6."""

    def setUp(self):
        self.pdf_processor = PDFProcessor(render_dpi=72)
        self.evaluator = ComplianceEvaluator()
        self.eval_date = date(2025, 6, 15)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / f"wp5_{uuid4().hex[:8]}.db"
        self.upload_dir = Path(self.temp_dir.name) / "uploads"
        self._orig_repo_db = repository.db_path
        self._orig_env_up = os.environ.get("SUBGUARD_UPLOAD_DIR")
        os.environ["SUBGUARD_UPLOAD_DIR"] = str(self.upload_dir)
        init_db(self.db_path)
        repository.db_path = self.db_path
        self.client = TestClient(app)
        authenticate_session_client(self.client, db_path=self.db_path, role="admin")

    def tearDown(self):
        repository.db_path = self._orig_repo_db
        if self._orig_env_up is None:
            os.environ.pop("SUBGUARD_UPLOAD_DIR", None)
        else:
            os.environ["SUBGUARD_UPLOAD_DIR"] = self._orig_env_up
        self.temp_dir.cleanup()

    # =========================================================================
    # Section 3.A: Evidence Integrity & Extraction Boundaries
    # =========================================================================

    def test_3a_01_conflicting_dates_within_single_snippet_escalates_never_compliant(self):
        """Two distinct expiration dates in one snippet must fail closed and force review."""
        pdf_bytes = _build_pdf_bytes([
            "ACORD 25 CERTIFICATE\n"
            "INSURED: Apex Electrical Contractors LLC\n"
            "GENERAL LIABILITY GL-98765432 EACH OCCURRENCE: $1,000,000\n"
            "EFF: 2025-01-01 EXP: 2026-01-01 NOTE EXP: 2025-07-01"
        ])
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="wp5_3a01_coi")
        provider = _FixedRawProvider({
            "wp5_3a01_coi": RawInsuranceExtraction(
                document_type="coi",
                named_insured=RawEntityExtraction(
                    name="Apex Electrical Contractors LLC",
                    evidence=RawEvidenceQuote(page_number=1, source_text="INSURED: Apex Electrical Contractors LLC"),
                ),
                coverages=[
                    RawCoverageSection(
                        coverage_type="general_liability",
                        policy_number="GL-98765432",
                        each_occurrence="$1,000,000",
                        effective_date="2025-01-01",
                        expiration_date="2026-01-01",
                        evidence=RawEvidenceQuote(
                            page_number=1,
                            source_text="EFF: 2025-01-01 EXP: 2026-01-01 NOTE EXP: 2025-07-01",
                        ),
                    )
                ],
                endorsements=[],
            )
        })
        pipe_res = ExtractionPipelineService(provider).process_inspection(inspection, subcontractor_id="sub_3a01")
        self.assertIsNotNone(pipe_res.document_set)
        reqs = [
            InsuranceRequirement(
                requirement_id="REQ_GL_EXP",
                rule_type=RequirementType.POLICY_EXPIRATION,
                description="GL active",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                severity=Severity.CRITICAL,
            )
        ]
        result = self.evaluator.evaluate(pipe_res.document_set, reqs, self.eval_date)
        self.assertNotEqual(result.overall_status, ComplianceStatus.COMPLIANT)
        self.assertTrue(result.requires_human_review)
        self.assertIsNotNone(result.reconciliation_summary)
        self.assertGreater(result.reconciliation_summary["discrepancies_count"], 0)

    def test_3a_02_conflicting_roles_within_single_snippet_unresolved(self):
        """Ambiguous conjoined entities before Additional Insured must not confirm role."""
        ai_ev = EvidenceReference(
            document_id="doc_end_3a02",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Additional Insureds: Apex Electrical Contractors LLC and Delta Electric Inc",
        )
        ni_ev = EvidenceReference(
            document_id="doc_coi_3a02",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="INSURED: Apex Electrical Contractors LLC",
        )
        erec = reconcile_entity_identities(
            primary_entity_name="Apex Electrical Contractors LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi_3a02",
            referenced_entity_name="Apex Electrical Contractors LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end_3a02",
            applicable_contract_requirement_id="REQ_AI",
            evidence_chain=(ni_ev, ai_ev),
            primary_role_evidence=ni_ev,
            referenced_role_evidence=ai_ev,
        )
        role_ok, disc = check_contractual_role_satisfaction(
            entity_record=erec,
            required_entity_name="Apex Electrical Contractors LLC",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_AI",
            is_requirement_validated=True,
        )
        self.assertFalse(role_ok)
        self.assertIsNotNone(disc)
        self.assertIn(disc.reason_code, ("MISSING_ROLE_EVIDENCE", "INSUFFICIENT_ROLE_EVIDENCE"))

    def test_3a_03_ambiguous_numeric_date_rejected_through_production_path(self):
        """Ambiguous 01/02/25 without MM/DD vs DD/MM convention must route to human review."""
        coi_id = "wp5_3a03_coi"
        end_id = "wp5_3a03_end"
        doc_set = ExtractedDocumentSet(
            subcontractor_id="sub_3a03",
            documents_submitted=[DocumentType.COI, DocumentType.ENDORSEMENT],
            is_document_set_complete=True,
            named_insured=EntityInfo(
                name="Apex Electrical Contractors LLC",
                evidence=EvidenceReference(
                    document_id=coi_id,
                    document_type=DocumentType.COI,
                    page_number=1,
                    source_text="INSURED: Apex Electrical Contractors LLC",
                ),
            ),
            coverages=_base_compliant_coverages(coi_id),
            endorsements=[
                EndorsementExtraction(
                    endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                    present=True,
                    form_number="CG 20 10 04 13",
                    applicable_policy="GL-98765432",
                    is_standard_form=True,
                    manuscript_clause_detected=False,
                    manuscript_status="NOT_DETECTED",
                    manuscript_verification_basis="unmodified_standard_form",
                    effective_date=date(2025, 1, 2),
                    expiration_date=date(2026, 1, 2),
                    confidence=0.95,
                    extraction_status=ExtractionStatus.EXTRACTED,
                    evidence=EvidenceReference(
                        document_id=end_id,
                        document_type=DocumentType.ENDORSEMENT,
                        page_number=1,
                        source_text="CG 20 10 04 13 Effective 01/02/25 to 01/02/26",
                    ),
                )
            ],
        )
        reqs = [
            InsuranceRequirement(
                requirement_id="REQ_AI_DUR",
                rule_type=RequirementType.CONTRACTUAL_DURATION,
                description="AI through policy expiration",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                requires_coverage_through_policy_expiration=True,
                is_duration_requirement_validated=True,
                severity=Severity.CRITICAL,
            )
        ]
        result = self.evaluator.evaluate(doc_set, reqs, self.eval_date)
        self.assertNotEqual(result.overall_status, ComplianceStatus.COMPLIANT)
        self.assertTrue(result.requires_human_review)

    def test_3a_04_quote_grounded_on_wrong_page_dropped_and_cannot_approve(self):
        """A citation claiming page 2 when the text only exists on page 1 must drop evidence and escalate."""
        pdf_bytes = _build_pdf_bytes([
            "ACORD 25 PAGE 1\nINSURED: Apex Electrical Contractors LLC\nEACH OCCURRENCE: $1,000,000",
            "ACORD 25 PAGE 2\nUNRELATED GENERAL CONDITIONS TEXT",
        ])
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="wp5_3a04_doc")
        provider = _FixedRawProvider({
            "wp5_3a04_doc": RawInsuranceExtraction(
                document_type="coi",
                named_insured=RawEntityExtraction(
                    name="Apex Electrical Contractors LLC",
                    evidence=RawEvidenceQuote(page_number=1, source_text="INSURED: Apex Electrical Contractors LLC"),
                ),
                coverages=[
                    RawCoverageSection(
                        coverage_type="general_liability",
                        policy_number="GL-98765432",
                        each_occurrence="$1,000,000",
                        effective_date="01/01/2025",
                        expiration_date="01/01/2026",
                        evidence=RawEvidenceQuote(page_number=2, source_text="EACH OCCURRENCE: $1,000,000"),
                    )
                ],
            )
        })
        pipe_res = ExtractionPipelineService(provider).process_inspection(inspection, subcontractor_id="sub_3a04")
        self.assertIsNotNone(pipe_res.document_set)
        gl = pipe_res.document_set.coverages[0]
        self.assertIsNone(gl.evidence)
        reqs = [
            InsuranceRequirement(
                requirement_id="REQ_GL_OCC",
                rule_type=RequirementType.COVERAGE_LIMIT,
                description="GL Occ >= $1M",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                limit_field=LimitField.EACH_OCCURRENCE,
                min_amount_cents=100_000_000,
                severity=Severity.CRITICAL,
            )
        ]
        result = self.evaluator.evaluate(pipe_res.document_set, reqs, self.eval_date)
        self.assertNotEqual(result.overall_status, ComplianceStatus.COMPLIANT)
        self.assertTrue(result.requires_human_review)

    def test_3a_05_ocr_failure_unreadable_page_escalates_never_compliant(self):
        """Image-only/unreadable PDF when OCR fails must fail safely and never produce COMPLIANT."""
        blank_pdf = _build_pdf_bytes([""], blank_image_only=True)
        inspection = self.pdf_processor.process_pdf(blank_pdf, document_id="wp5_3a05_scan")
        pipe_res = ExtractionPipelineService(
            extraction_provider=HonestApplicationMockProvider(),
            ocr_provider=_FailingOCRProvider(),
        ).process_inspection(inspection, subcontractor_id="sub_3a05")
        self.assertIsNone(pipe_res.document_set)
        self.assertTrue(pipe_res.errors or pipe_res.error_message)

    # =========================================================================
    # Section 3.B: Multi-Document Identity & Policy Reconciliation
    # =========================================================================

    def test_3b_01_same_legal_entity_different_roles_with_evidence_compliant(self):
        """Same legal entity across COI (Named Insured) and Endorsement (Additional Insured) reconciles cleanly."""
        coi_id = "wp5_3b01_coi"
        end_id = "wp5_3b01_end"
        ni_ev = EvidenceReference(
            document_id=coi_id,
            document_type=DocumentType.COI,
            page_number=1,
            source_text="NAMED INSURED: Apex Electrical Contractors LLC",
        )
        ai_ev = EvidenceReference(
            document_id=end_id,
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Additional Insured: Apex Electrical Contractors LLC",
        )
        erec = reconcile_entity_identities(
            primary_entity_name="Apex Electrical Contractors LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id=coi_id,
            referenced_entity_name="Apex Electrical Contractors LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id=end_id,
            applicable_contract_requirement_id="REQ_AI",
            evidence_chain=(ni_ev, ai_ev),
            primary_role_evidence=ni_ev,
            referenced_role_evidence=ai_ev,
        )
        role_ok, disc = check_contractual_role_satisfaction(
            entity_record=erec,
            required_entity_name="Apex Electrical Contractors LLC",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_AI",
            is_requirement_validated=True,
        )
        self.assertTrue(role_ok)
        self.assertIsNone(disc)

        doc_set = ExtractedDocumentSet(
            subcontractor_id="sub_3b01",
            documents_submitted=[DocumentType.COI, DocumentType.ENDORSEMENT],
            is_document_set_complete=True,
            named_insured=EntityInfo(name="Apex Electrical Contractors LLC", confidence=0.98, evidence=ni_ev),
            additional_named_insureds=[EntityInfo(name="Apex Electrical Contractors LLC", confidence=0.98, evidence=ni_ev)],
            coverages=_base_compliant_coverages(coi_id),
            endorsements=[
                EndorsementExtraction(
                    endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                    present=True,
                    form_number="CG 20 10 04 13",
                    applicable_policy="GL-98765432",
                    is_standard_form=True,
                    manuscript_clause_detected=False,
                    manuscript_status="NOT_DETECTED",
                    manuscript_verification_basis="unmodified_standard_form",
                    effective_date=date(2025, 1, 1),
                    expiration_date=date(2026, 1, 1),
                    confidence=0.98,
                    extraction_status=ExtractionStatus.EXTRACTED,
                    evidence=ai_ev,
                    date_evidence=[
                        EvidenceReference(
                            document_id=end_id,
                            document_type=DocumentType.ENDORSEMENT,
                            page_number=1,
                            source_text="Policy EFF: 2025-01-01 EXP: 2026-01-01",
                        )
                    ],
                )
            ],
        )
        recon = reconcile_extracted_document_set(doc_set, evaluation_date=self.eval_date)
        self.assertIsNotNone(recon)
        self.assertEqual(len(recon.discrepancies), 0)
        self.assertFalse(recon.has_unresolved_contradictions)
        self.assertFalse(recon.has_missing_evidence)

    def test_3b_02_generic_shared_words_and_same_first_word_conflict(self):
        """Distinct entities sharing first word or generic words ('Apex Electrical' vs 'Apex Plumbing') flag conflict."""
        coi_id = "wp5_3b02_coi"
        end_id = "wp5_3b02_end"
        doc_set = ExtractedDocumentSet(
            subcontractor_id="sub_3b02",
            documents_submitted=[DocumentType.COI, DocumentType.ENDORSEMENT],
            is_document_set_complete=True,
            named_insured=EntityInfo(
                name="Apex Electrical Contractors LLC",
                confidence=0.98,
                evidence=EvidenceReference(
                    document_id=coi_id,
                    document_type=DocumentType.COI,
                    page_number=1,
                    source_text="INSURED: Apex Electrical Contractors LLC",
                ),
            ),
            additional_named_insureds=[
                EntityInfo(
                    name="Apex Plumbing Contractors LLC",
                    confidence=0.98,
                    evidence=EvidenceReference(
                        document_id=end_id,
                        document_type=DocumentType.ENDORSEMENT,
                        page_number=1,
                        source_text="INSURED: Apex Plumbing Contractors LLC",
                    ),
                )
            ],
            coverages=_base_compliant_coverages(coi_id),
            endorsements=[],
        )
        reqs = get_default_requirements("Apex Electrical Contractors LLC", "General Contractor Corp")
        result = self.evaluator.evaluate(doc_set, reqs, self.eval_date)
        self.assertNotEqual(result.overall_status, ComplianceStatus.COMPLIANT)
        self.assertTrue(result.requires_human_review)
        self.assertIsNotNone(result.reconciliation_summary)
        self.assertGreater(result.reconciliation_summary["discrepancies_count"], 0)
        self.assertTrue(any(f.status == ComplianceStatus.NON_COMPLIANT for f in result.findings))

    def test_3b_03_policy_formatting_vs_meaningful_suffix_and_leading_zero(self):
        """Formatting delimiter variations normalize, whereas suffix or leading zero deltas escalate."""
        coi_id = "wp5_3b03_coi"
        end_id = "wp5_3b03_end"
        covs = _base_compliant_coverages(coi_id)

        doc_norm = ExtractedDocumentSet(
            subcontractor_id="sub_3b03_norm",
            documents_submitted=[DocumentType.COI, DocumentType.ENDORSEMENT],
            is_document_set_complete=True,
            named_insured=EntityInfo(
                name="Apex Electrical Contractors LLC",
                evidence=EvidenceReference(document_id=coi_id, document_type=DocumentType.COI, page_number=1, source_text="Apex Electrical Contractors LLC"),
            ),
            coverages=covs,
            endorsements=[
                EndorsementExtraction(
                    endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                    present=True,
                    applicable_policy="GL 9876 5432",
                    is_standard_form=True,
                    manuscript_clause_detected=False,
                    confidence=0.95,
                    evidence=EvidenceReference(document_id=end_id, document_type=DocumentType.ENDORSEMENT, page_number=1, source_text="Policy GL 9876 5432"),
                )
            ],
        )
        recon_norm = reconcile_extracted_document_set(doc_norm, evaluation_date=self.eval_date)
        self.assertIsNotNone(recon_norm)
        self.assertEqual(recon_norm.policy_records[0].classification, PolicyMatchClassification.FORMATTING_NORMALIZED)
        self.assertFalse(recon_norm.policy_records[0].requires_human_review)

        doc_diff = doc_norm.model_copy(deep=True)
        doc_diff.endorsements[0].applicable_policy = "00GL-98765432-B"
        recon_diff = reconcile_extracted_document_set(doc_diff, evaluation_date=self.eval_date)
        self.assertIsNotNone(recon_diff)
        self.assertTrue(recon_diff.policy_records[0].requires_human_review)
        self.assertNotIn(
            recon_diff.policy_records[0].classification,
            (PolicyMatchClassification.EXACT, PolicyMatchClassification.FORMATTING_NORMALIZED),
        )

    def test_3b_04_missing_and_duplicate_document_hashes_in_reconciliation(self):
        """Missing document_hashes uses deterministic fallback; duplicate hashes are represented cleanly."""
        coi_id = "wp5_3b04_coi"
        end_id = "wp5_3b04_end"
        doc_set = ExtractedDocumentSet(
            subcontractor_id="sub_3b04",
            documents_submitted=[DocumentType.COI, DocumentType.ENDORSEMENT],
            is_document_set_complete=True,
            named_insured=EntityInfo(
                name="Apex Electrical Contractors LLC",
                evidence=EvidenceReference(document_id=coi_id, document_type=DocumentType.COI, page_number=1, source_text="Apex Electrical Contractors LLC"),
            ),
            coverages=_base_compliant_coverages(coi_id),
            endorsements=[
                EndorsementExtraction(
                    endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                    present=True,
                    applicable_policy="GL-98765432",
                    is_standard_form=True,
                    manuscript_clause_detected=False,
                    confidence=0.95,
                    evidence=EvidenceReference(document_id=end_id, document_type=DocumentType.ENDORSEMENT, page_number=1, source_text="Policy GL-98765432"),
                )
            ],
        )
        recon_missing_hash = reconcile_extracted_document_set(doc_set, document_hashes=None, evaluation_date=self.eval_date)
        self.assertIsNotNone(recon_missing_hash)
        self.assertEqual(len(recon_missing_hash.packet_manifest), 2)
        for m in recon_missing_hash.packet_manifest:
            self.assertEqual(len(m.sha256_hash), 64)

        dup_hash = "a" * 64
        recon_dup_hash = reconcile_extracted_document_set(
            doc_set,
            document_hashes={coi_id: dup_hash, end_id: dup_hash},
            evaluation_date=self.eval_date,
        )
        self.assertIsNotNone(recon_dup_hash)
        self.assertEqual([m.sha256_hash for m in recon_dup_hash.packet_manifest], [dup_hash, dup_hash])

    # =========================================================================
    # Section 3.C: Endorsement & Contractual Duration Enforcement
    # =========================================================================

    def test_3c_01_contractual_duration_through_policy_expiration_violation_blocks_compliant(self):
        """Validated duration requirement through policy expiration fails when endorsement expires early."""
        coi_id = "wp5_3c01_coi"
        end_id = "wp5_3c01_end"
        doc_set = ExtractedDocumentSet(
            subcontractor_id="sub_3c01",
            documents_submitted=[DocumentType.COI, DocumentType.ENDORSEMENT],
            is_document_set_complete=True,
            named_insured=EntityInfo(
                name="Apex Electrical Contractors LLC",
                evidence=EvidenceReference(document_id=coi_id, document_type=DocumentType.COI, page_number=1, source_text="Apex Electrical Contractors LLC"),
            ),
            coverages=_base_compliant_coverages(coi_id),
            endorsements=[
                EndorsementExtraction(
                    endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                    present=True,
                    form_number="CG 20 10 04 13",
                    applicable_policy="GL-98765432",
                    is_standard_form=True,
                    manuscript_clause_detected=False,
                    manuscript_status="NOT_DETECTED",
                    manuscript_verification_basis="unmodified_standard_form",
                    effective_date=date(2025, 1, 1),
                    expiration_date=date(2025, 10, 1),
                    confidence=0.98,
                    extraction_status=ExtractionStatus.EXTRACTED,
                    evidence=EvidenceReference(
                        document_id=end_id,
                        document_type=DocumentType.ENDORSEMENT,
                        page_number=1,
                        source_text="CG 20 10 04 13 EFF: 2025-01-01 EXP: 2025-10-01",
                    ),
                )
            ],
        )
        reqs = [
            InsuranceRequirement(
                requirement_id="REQ_AI_DURATION",
                rule_type=RequirementType.CONTRACTUAL_DURATION,
                description="AI endorsement must remain active through policy expiration",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                requires_coverage_through_policy_expiration=True,
                is_duration_requirement_validated=True,
                severity=Severity.CRITICAL,
            )
        ]
        result = self.evaluator.evaluate(doc_set, reqs, self.eval_date)
        self.assertEqual(result.overall_status, ComplianceStatus.NON_COMPLIANT)
        self.assertTrue(result.requires_human_review)
        self.assertTrue(any(f.status == ComplianceStatus.NON_COMPLIANT for f in result.findings))

    def test_3c_02_earlier_endorsement_expiration_without_policy_alignment_requirement(self):
        """When contract does NOT mandate policy-expiration alignment, an active earlier endorsement creates no false violation."""
        coi_id = "wp5_3c02_coi"
        end_id = "wp5_3c02_end"
        doc_set = ExtractedDocumentSet(
            subcontractor_id="sub_3c02",
            documents_submitted=[DocumentType.COI, DocumentType.ENDORSEMENT],
            is_document_set_complete=True,
            named_insured=EntityInfo(
                name="Apex Electrical Contractors LLC",
                evidence=EvidenceReference(document_id=coi_id, document_type=DocumentType.COI, page_number=1, source_text="Apex Electrical Contractors LLC"),
            ),
            coverages=_base_compliant_coverages(coi_id),
            endorsements=[
                EndorsementExtraction(
                    endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                    present=True,
                    form_number="CG 20 10 04 13",
                    applicable_policy="GL-98765432",
                    is_standard_form=True,
                    manuscript_clause_detected=False,
                    manuscript_status="NOT_DETECTED",
                    manuscript_verification_basis="unmodified_standard_form",
                    effective_date=date(2025, 1, 1),
                    expiration_date=date(2025, 10, 1),
                    confidence=0.98,
                    extraction_status=ExtractionStatus.EXTRACTED,
                    evidence=EvidenceReference(
                        document_id=end_id,
                        document_type=DocumentType.ENDORSEMENT,
                        page_number=1,
                        source_text="CG 20 10 04 13 EFF: 2025-01-01 EXP: 2025-10-01",
                    ),
                )
            ],
        )
        reqs = [
            InsuranceRequirement(
                requirement_id="REQ_AI_PRESENCE",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE,
                description="Additional Insured required",
                required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                severity=Severity.CRITICAL,
            )
        ]
        result = self.evaluator.evaluate(doc_set, reqs, self.eval_date)
        self.assertFalse(any(f.status == ComplianceStatus.NON_COMPLIANT for f in result.findings))

    def test_3c_03_missing_or_unverified_endorsement_dates_fail_closed(self):
        """Missing endorsement dates under validated contractual duration must never pass COMPLIANT."""
        coi_id = "wp5_3c03_coi"
        end_id = "wp5_3c03_end"
        doc_set = ExtractedDocumentSet(
            subcontractor_id="sub_3c03",
            documents_submitted=[DocumentType.COI, DocumentType.ENDORSEMENT],
            is_document_set_complete=True,
            named_insured=EntityInfo(
                name="Apex Electrical Contractors LLC",
                evidence=EvidenceReference(document_id=coi_id, document_type=DocumentType.COI, page_number=1, source_text="Apex Electrical Contractors LLC"),
            ),
            coverages=_base_compliant_coverages(coi_id),
            endorsements=[
                EndorsementExtraction(
                    endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                    present=True,
                    form_number="CG 20 10 04 13",
                    applicable_policy="GL-98765432",
                    is_standard_form=True,
                    manuscript_clause_detected=False,
                    effective_date=None,
                    expiration_date=None,
                    confidence=0.95,
                    evidence=EvidenceReference(
                        document_id=end_id,
                        document_type=DocumentType.ENDORSEMENT,
                        page_number=1,
                        source_text="CG 20 10 04 13 Additional Insured on Policy GL-98765432",
                    ),
                )
            ],
        )
        reqs = [
            InsuranceRequirement(
                requirement_id="REQ_AI_DURATION",
                rule_type=RequirementType.CONTRACTUAL_DURATION,
                description="AI through 2026-01-01",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                required_coverage_end_date=date(2026, 1, 1),
                is_duration_requirement_validated=True,
                severity=Severity.CRITICAL,
            )
        ]
        result = self.evaluator.evaluate(doc_set, reqs, self.eval_date)
        self.assertNotEqual(result.overall_status, ComplianceStatus.COMPLIANT)
        self.assertTrue(result.requires_human_review)

    def test_3c_04_missing_evaluation_date_fails_closed(self):
        """Missing evaluation_date in reconcile_dates() leaves active state unknown and requires review."""
        pol_ev = (
            EvidenceReference(
                document_id="doc_coi",
                document_type=DocumentType.COI,
                page_number=1,
                source_text="Policy EFF: 2025-01-01 EXP: 2026-01-01",
            ),
        )
        drec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=pol_ev,
            endorsement_effective_date=None,
            endorsement_expiration_date=None,
            endorsement_date_evidence=(),
            evaluation_date=None,
            evidence_chain=pol_ev,
        )
        self.assertIsNone(drec.is_policy_active_on_evaluation_date)
        self.assertTrue(drec.requires_human_review)

    # =========================================================================
    # Section 3.D: Document Relationships (WP2)
    # =========================================================================

    def test_3d_01_valid_target_linked_replaces_and_amends_classifications(self):
        """Endorsement with explicit target policy linkage establishes CONFIRMED REPLACES/AMENDS edge."""
        coi_id = "wp5_3d01_coi"
        end_id = "wp5_3d01_end"
        doc_set = ExtractedDocumentSet(
            subcontractor_id="sub_3d01",
            documents_submitted=[DocumentType.COI, DocumentType.ENDORSEMENT],
            is_document_set_complete=True,
            named_insured=EntityInfo(
                name="Apex Electrical Contractors LLC",
                evidence=EvidenceReference(document_id=coi_id, document_type=DocumentType.COI, page_number=1, source_text="Apex Electrical Contractors LLC"),
            ),
            coverages=_base_compliant_coverages(coi_id),
            endorsements=[
                EndorsementExtraction(
                    endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                    present=True,
                    form_number="CG 20 10 04 13",
                    applicable_policy="GL-98765432",
                    is_standard_form=True,
                    manuscript_clause_detected=False,
                    confidence=0.95,
                    evidence=EvidenceReference(
                        document_id=end_id,
                        document_type=DocumentType.ENDORSEMENT,
                        page_number=1,
                        source_text="This endorsement amends Policy GL-98765432 to include Additional Insured.",
                    ),
                )
            ],
        )
        recon = reconcile_extracted_document_set(doc_set, evaluation_date=self.eval_date)
        self.assertIsNotNone(recon)
        self.assertEqual(len(recon.graph_edges), 1)
        edge = recon.graph_edges[0]
        self.assertEqual(edge.relationship_type, DocumentRelationshipType.AMENDS_DOCUMENT)
        self.assertTrue(edge.is_confirmed)
        self.assertEqual(edge.target_document_id, coi_id)
        self.assertEqual(edge.classification, EdgeClassification.CONFIRMED_EXACT)

    def test_3d_02_generic_or_ambiguous_target_wording_never_confirmed(self):
        """Generic amends wording without target policy/form identity must not confirm relationship."""
        coi_id = "wp5_3d02_coi"
        end_id = "wp5_3d02_end"
        doc_set = ExtractedDocumentSet(
            subcontractor_id="sub_3d02",
            documents_submitted=[DocumentType.COI, DocumentType.ENDORSEMENT],
            is_document_set_complete=True,
            named_insured=EntityInfo(
                name="Apex Electrical Contractors LLC",
                evidence=EvidenceReference(document_id=coi_id, document_type=DocumentType.COI, page_number=1, source_text="Apex Electrical Contractors LLC"),
            ),
            coverages=_base_compliant_coverages(coi_id),
            endorsements=[
                EndorsementExtraction(
                    endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                    present=True,
                    form_number="CG 20 10 04 13",
                    applicable_policy=None,
                    is_standard_form=True,
                    manuscript_clause_detected=False,
                    confidence=0.95,
                    evidence=EvidenceReference(
                        document_id=end_id,
                        document_type=DocumentType.ENDORSEMENT,
                        page_number=1,
                        source_text="This endorsement amends the policy coverage as stated herein.",
                    ),
                )
            ],
        )
        recon = reconcile_extracted_document_set(doc_set, evaluation_date=self.eval_date)
        self.assertIsNotNone(recon)
        self.assertEqual(len(recon.graph_edges), 1)
        edge = recon.graph_edges[0]
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.review_reason_code, "GENERIC_RELATIONSHIP_WITHOUT_TARGET_IDENTITY")

    # =========================================================================
    # Section 3.E: Human-Review & Approval Safety
    # =========================================================================

    def test_3e_01_confirmed_duration_and_entity_violations_block_approval_via_http(self):
        """Confirmed CONTRACTUAL_DURATION and ENTITY_IDENTITY failures block accepted_exception via HTTP 422."""
        for label, rule_type, reason_code in [
            ("dur", "contractual_duration", "CONTRACTUAL_DURATION_VIOLATION"),
            ("ent", "entity_identity", "CONFIRMED_ENTITY_MISMATCH"),
        ]:
            sub_id = f"wp5_block_{label}_{uuid4().hex[:8]}"
            repository.create_submission(
                submission_id=sub_id,
                name=f"WP5 Block Approval ({label})",
                overall_status="non_compliant",
                processing_status="completed",
                provider="mock",
                evaluation_date="2025-06-15",
                result_data={
                    "review": {
                        "subcontractor_id": "sub_wp5",
                        "evaluation_date": "2025-06-15",
                        "overall_status": "non_compliant",
                        "requires_human_review": True,
                        "findings": [
                            {
                                "finding_id": f"f_{label}",
                                "requirement_id": f"REQ_{label.upper()}",
                                "rule_type": rule_type,
                                "status": "non_compliant",
                                "severity": "critical",
                                "reason_code": reason_code,
                                "requires_human_review": True,
                                "explanation": f"Confirmed {reason_code}",
                            }
                        ],
                    },
                    "document_set": {"subcontractor_id": "sub_wp5", "coverages": [], "endorsements": []},
                },
                subcontractor_name="Apex Electrical Contractors LLC",
            )
            res = self.client.post(
                f"/api/v1/submissions/{sub_id}/review",
                json={
                    "review_status": "accepted_exception",
                    "reviewer_name": "WP5 Auditor",
                    "reviewer_notes": "Attempting unauthorized approval on confirmed violation.",
                    "update_submission_status": "compliant",
                },
            )
            self.assertEqual(res.status_code, 422, f"Expected 422 blocking approval for {label}")
            persisted = repository.get_submission_by_id(sub_id)
            self.assertEqual(persisted["overall_status"], "non_compliant")
            self.assertEqual(len(persisted["reviews"]), 0)

    def test_3e_02_waiver_blocked_on_non_waivable_rules_and_reasons(self):
        """Attempting to waive POLICY_EXPIRATION, POLICY_EFFECTIVE_DATE, or NAMED_INSURED returns HTTP 422."""
        for rule_type, reason_code in [
            ("policy_expiration", "POLICY_EXPIRED"),
            ("policy_effective_date", "POLICY_FUTURE_INCEPTION"),
            ("named_insured", "NAMED_INSURED_MISMATCH"),
        ]:
            sub_id = f"wp5_nowaive_{rule_type}_{uuid4().hex[:8]}"
            repository.create_submission(
                submission_id=sub_id,
                name=f"WP5 No Waive {rule_type}",
                overall_status="non_compliant",
                processing_status="completed",
                provider="mock",
                evaluation_date="2025-06-15",
                result_data={
                    "review": {
                        "subcontractor_id": "sub_wp5",
                        "evaluation_date": "2025-06-15",
                        "overall_status": "non_compliant",
                        "requires_human_review": False,
                        "findings": [
                            {
                                "finding_id": "f_nw",
                                "requirement_id": "REQ_NW",
                                "rule_type": rule_type,
                                "status": "non_compliant",
                                "severity": "critical",
                                "reason_code": reason_code,
                                "requires_human_review": False,
                                "explanation": "Non-waivable failure",
                            }
                        ],
                    },
                    "document_set": {"subcontractor_id": "sub_wp5", "coverages": [], "endorsements": []},
                },
                subcontractor_name="Apex Electrical Contractors LLC",
            )
            res = self.client.post(
                f"/api/v1/submissions/{sub_id}/review",
                json={
                    "review_status": "waived",
                    "rule_id": "REQ_NW",
                    "reviewer_name": "WP5 Auditor",
                    "reviewer_notes": "Trying invalid waiver of non-waivable requirement.",
                },
            )
            self.assertEqual(res.status_code, 422)
            persisted = repository.get_submission_by_id(sub_id)
            self.assertEqual(len(persisted["reviews"]), 0)

    def test_3e_03_unresolved_reconciliation_preserves_findings_and_immutable_audit(self):
        """Unresolved submission persisted in DB remains immutable after human review recording."""
        sub_id = f"wp5_immut_{uuid4().hex[:8]}"
        coi_pdf = _build_pdf_bytes([
            "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE\n"
            "INSURED: Apex Electrical Contractors LLC\n"
            "COMMERCIAL GENERAL LIABILITY Policy Number: GL-98765432\n"
            "Effective Date: 01/01/2025 Expiration Date: 01/01/2026\n"
            "EACH OCCURRENCE: $1,000,000 GENERAL AGGREGATE: $2,000,000\n"
            "AUTOMOBILE LIABILITY Policy Number: AUTO-112233 COMBINED SINGLE LIMIT: $1,000,000\n"
            "WORKERS COMPENSATION Policy Number: WC-445566 EACH ACCIDENT: $1,000,000\n"
            "CERTIFICATE HOLDER: General Contractor Corp"
        ])
        end_pdf = _build_pdf_bytes([
            "ENDORSEMENT CG 20 10 04 13\n"
            "This endorsement amends prior policy wording without naming target policy.\n"
            "Additional Insured: General Contractor Corp"
        ])
        res = self.client.post(
            "/api/v1/compliance/review-documents",
            files=[
                ("coi_file", ("coi.pdf", io.BytesIO(coi_pdf), "application/pdf")),
                ("endorsement_files", ("end.pdf", io.BytesIO(end_pdf), "application/pdf")),
            ],
            data={
                "submission_name": f"WP5 Immutability {sub_id}",
                "evaluation_date": "2025-06-15",
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        created_id = data["submission_id"]
        self.assertTrue(data["requires_human_review"])
        orig_findings_count = len(data["review_result"]["findings"])
        self.assertGreater(orig_findings_count, 0)

        rev_res = self.client.post(
            f"/api/v1/submissions/{created_id}/review",
            json={
                "review_status": "deficiency_notice_required",
                "reviewer_name": "Compliance Specialist",
                "reviewer_notes": "Verified unresolved endorsement ambiguity; deficiency issued.",
                "update_submission_status": "deficiency_pending",
            },
        )
        self.assertEqual(rev_res.status_code, 200)
        persisted = self.client.get(f"/api/v1/submissions/{created_id}").json()
        self.assertEqual(persisted["overall_status"], "deficiency_pending")
        self.assertEqual(len(persisted["result"]["review"]["findings"]), orig_findings_count)
        self.assertEqual(len(persisted["reviews"]), 1)

    # =========================================================================
    # Section 4 & Section 6: Real Production Workflow + WP4 Limitations 1 & 2
    # =========================================================================

    def test_4_and_6_real_http_evaluate_and_review_documents_sqlite_roundtrip(self):
        """Exercise /api/v1/compliance/evaluate and /api/v1/compliance/review-documents -> SQLite."""
        coi_id = "wp5_http_coi"
        doc_set = ExtractedDocumentSet(
            subcontractor_id="sub_wp5_http",
            documents_submitted=[DocumentType.COI],
            is_document_set_complete=True,
            named_insured=EntityInfo(
                name="Apex Electrical Contractors LLC",
                confidence=0.99,
                evidence=EvidenceReference(document_id=coi_id, document_type=DocumentType.COI, page_number=1, source_text="Apex Electrical Contractors LLC"),
            ),
            coverages=_base_compliant_coverages(coi_id),
            endorsements=[],
        )
        eval_payload = {
            "document_set": doc_set.model_dump(mode="json"),
            "requirements": [
                InsuranceRequirement(
                    requirement_id="REQ_GL_OCC",
                    rule_type=RequirementType.COVERAGE_LIMIT,
                    description="GL Occ >= $1M",
                    coverage_type=CoverageType.GENERAL_LIABILITY,
                    limit_field=LimitField.EACH_OCCURRENCE,
                    min_amount_cents=100_000_000,
                    severity=Severity.CRITICAL,
                ).model_dump(mode="json")
            ],
            "evaluation_date": "2025-06-15",
        }
        eval_res = self.client.post("/api/v1/compliance/evaluate", json=eval_payload)
        self.assertEqual(eval_res.status_code, 200)
        self.assertEqual(eval_res.json()["overall_status"], "compliant")

        coi_pdf = _build_pdf_bytes([
            "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE\n"
            "INSURED: Apex Electrical Contractors LLC\n"
            "COMMERCIAL GENERAL LIABILITY GL-98765432\n"
            "EACH OCCURRENCE: $1,000,000 GENERAL AGGREGATE: $2,000,000\n"
            "Effective: 01/01/2025 Expiration: 01/01/2026\n"
            "AUTOMOBILE LIABILITY AUTO-112233 COMBINED SINGLE LIMIT: $1,000,000\n"
            "WORKERS COMPENSATION WC-445566 EACH ACCIDENT: $1,000,000"
        ])
        rev_doc_res = self.client.post(
            "/api/v1/compliance/review-documents",
            files={"coi_file": ("wp5_coi.pdf", io.BytesIO(coi_pdf), "application/pdf")},
            data={"submission_name": "WP5 Real Upload Roundtrip", "evaluation_date": "2025-06-15"},
        )
        self.assertEqual(rev_doc_res.status_code, 200)
        sub_id = rev_doc_res.json()["submission_id"]
        fetched = self.client.get(f"/api/v1/submissions/{sub_id}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["id"], sub_id)
        self.assertGreater(len(fetched.json()["documents"]), 0)

    def test_6_limitation_1_standalone_replacement_pdf_without_endorsement_extraction(self):
        """WP4 Limitation 1: Standalone replacement/amendment PDF producing no EndorsementExtraction yields no relationship edge."""
        coi_pdf = _build_pdf_bytes([
            "ACORD 25\nINSURED: Apex Electrical Contractors LLC\nGENERAL LIABILITY GL-98765432 EACH OCCURRENCE: $1,000,000"
        ])
        replacement_notice_pdf = _build_pdf_bytes([
            "NOTICE OF POLICY REPLACEMENT\nThis document replaces policy GL-98765432 effective 07/01/2025."
        ])
        coi_insp = self.pdf_processor.process_pdf(coi_pdf, document_id="lim1_coi")
        rep_insp = self.pdf_processor.process_pdf(replacement_notice_pdf, document_id="lim1_rep")

        provider = _FixedRawProvider({
            "lim1_coi": RawInsuranceExtraction(
                document_type="coi",
                named_insured=RawEntityExtraction(
                    name="Apex Electrical Contractors LLC",
                    evidence=RawEvidenceQuote(page_number=1, source_text="INSURED: Apex Electrical Contractors LLC"),
                ),
                coverages=[
                    RawCoverageSection(
                        coverage_type="general_liability",
                        policy_number="GL-98765432",
                        each_occurrence="$1,000,000",
                        effective_date="01/01/2025",
                        expiration_date="01/01/2026",
                        evidence=RawEvidenceQuote(page_number=1, source_text="EACH OCCURRENCE: $1,000,000"),
                    )
                ],
                endorsements=[],
            ),
            "lim1_rep": RawInsuranceExtraction(
                document_type="endorsement",
                named_insured=RawEntityExtraction(
                    name="Apex Electrical Contractors LLC",
                    evidence=RawEvidenceQuote(page_number=1, source_text="NOTICE OF POLICY REPLACEMENT"),
                ),
                coverages=[],
                endorsements=[],
            ),
        })
        svc = ExtractionPipelineService(provider)
        ds_coi = svc.process_inspection(coi_insp, "sub_lim1").document_set
        ds_rep = svc.process_inspection(rep_insp, "sub_lim1").document_set
        assert ds_coi is not None and ds_rep is not None

        composite = ExtractedDocumentSet(
            subcontractor_id="sub_lim1",
            documents_submitted=[DocumentType.COI, DocumentType.ENDORSEMENT],
            is_document_set_complete=True,
            named_insured=ds_coi.named_insured,
            additional_named_insureds=[ds_rep.named_insured] if ds_rep.named_insured else [],
            coverages=ds_coi.coverages + ds_rep.coverages,
            endorsements=ds_coi.endorsements + ds_rep.endorsements,
        )
        recon = reconcile_extracted_document_set(composite, evaluation_date=self.eval_date)
        self.assertIsNotNone(recon)
        # Documented Limitation 1: Without EndorsementExtraction on lim1_rep, no graph edge is produced.
        self.assertEqual(len(recon.graph_edges), 0)

    def test_6_limitation_2_coverage_evidence_single_quote_limit_vs_dates(self):
        """WP4 Limitation 2: Mock/standard extraction places limit quote in CoverageExtraction.evidence with date_evidence empty."""
        coi_path = Path("evaluation/fixtures/pdfs/synthetic_standard_acord25.pdf")
        if not coi_path.exists():
            coi_pdf = _build_pdf_bytes([
                "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE\n"
                "INSURED: Apex Electrical Contractors LLC\n"
                "COMMERCIAL GENERAL LIABILITY Policy Number: GL-98765432\n"
                "EFF: 01/01/2025 EXP: 01/01/2026\n"
                "EACH OCCURRENCE: $1,000,000 GENERAL AGGREGATE: $2,000,000"
            ])
        else:
            coi_pdf = coi_path.read_bytes()

        insp = self.pdf_processor.process_pdf(coi_pdf, document_id="lim2_coi")
        mock_svc = ExtractionPipelineService(HonestApplicationMockProvider())
        pipe_res = mock_svc.process_inspection(insp, "sub_lim2")
        self.assertIsNotNone(pipe_res.document_set)
        gl = next(c for c in pipe_res.document_set.coverages if c.coverage_type == CoverageType.GENERAL_LIABILITY)
        # HonestApplicationMockProvider selects limit quote for CoverageExtraction.evidence and leaves date_evidence empty
        self.assertIsNotNone(gl.evidence)
        self.assertIn("1,000,000", gl.evidence.source_text)
        self.assertEqual(gl.date_evidence, ())

        # Documented Limitation 2: policy date reconciliation is skipped because CoverageExtraction.evidence holds limit text
        recon = reconcile_extracted_document_set(pipe_res.document_set, evaluation_date=self.eval_date)
        self.assertTrue(recon is None or len(recon.date_records) == 0)

        # Confirm compliance evaluation does NOT inject a spurious missing-date discrepancy
        reqs = [
            InsuranceRequirement(
                requirement_id="REQ_GL_EXP",
                rule_type=RequirementType.POLICY_EXPIRATION,
                description="GL active",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                severity=Severity.CRITICAL,
            )
        ]
        result = self.evaluator.evaluate(pipe_res.document_set, reqs, self.eval_date)
        self.assertEqual(result.overall_status, ComplianceStatus.COMPLIANT)


if __name__ == "__main__":
    unittest.main()
