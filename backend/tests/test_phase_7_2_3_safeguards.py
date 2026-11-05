"""Phase 7.2.3 and 7.2.3.2 Compliance Safeguards and Regression Tests.

Comprehensive test suite covering:
- WP-A: Defensive active-term enforcement & future effective date protection.
- WP-B: Manuscript endorsement & restrictive wording detection, conservative defaults.
- WP-C: Deterministic cross-document reconciliation engine (limits, dates, policies).
- WP-D: Target scenario regression tests (SCEN-16, SCEN-24, SCEN-27, SCEN-36).
- Complete 18-category compliance test matrix with explicit status and reason code assertions.
"""

from datetime import date
import unittest

from app.compliance.evaluator import ComplianceEvaluator
from app.extraction.pipeline import (
    ExtractionPipelineService,
    reconcile_cross_document_set,
)
from app.extraction.providers.mock import MockExtractionProvider
from app.schemas.insurance import (
    ComplianceFinding,
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
from app.schemas.pdf import (
    BoundingBox,
    DocumentProcessingStatus,
    PDFDocumentInspection,
    PDFPageInspection,
)
from app.schemas.raw_extraction import (
    RawCoverageSection,
    RawEndorsement,
    RawEntityExtraction,
    RawEvidenceQuote,
    RawInsuranceExtraction,
)
from tests.fixtures.sample_data import (
    EVAL_DATE,
    make_evidence,
    sample_gl_coverage,
    sample_gl_requirement,
    sample_subcontractor_document_set,
)


class TestPhase723WorkPackageA(unittest.TestCase):
    """Work Package A: Defensive Active-Term Enforcement & Future Effective Date Safeguards."""

    def setUp(self):
        self.evaluator = ComplianceEvaluator()

    def test_validate_requirement_profile_injects_effective_date(self):
        """Standard requirement profiles must mandate POLICY_EFFECTIVE_DATE if POLICY_EXPIRATION is present."""
        reqs = [
            InsuranceRequirement(
                requirement_id="REQ_GL_EXP",
                rule_type=RequirementType.POLICY_EXPIRATION,
                description="GL policy must not be expired",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                severity=Severity.CRITICAL,
            )
        ]
        validated = validate_requirement_profile(reqs)
        rule_types = {r.rule_type for r in validated}
        self.assertIn(RequirementType.POLICY_EFFECTIVE_DATE, rule_types)
        eff_req = [r for r in validated if r.rule_type == RequirementType.POLICY_EFFECTIVE_DATE][0]
        self.assertEqual(eff_req.coverage_type, CoverageType.GENERAL_LIABILITY)

    def test_future_effective_date_prevents_compliant_status_defensively(self):
        """A policy with a future effective date must NEVER produce COMPLIANT,
        even if the requirement profile completely omits POLICY_EFFECTIVE_DATE.
        """
        review_date = date(2025, 6, 1)
        future_eff = date(2025, 9, 1)  # 3 months in future
        future_exp = date(2026, 9, 1)

        cov = sample_gl_coverage(eff=future_eff, exp=future_exp)
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        # Profile that intentionally OMITS POLICY_EFFECTIVE_DATE requirement
        omitted_reqs = [
            InsuranceRequirement(
                requirement_id="REQ_GL_EXP",
                rule_type=RequirementType.POLICY_EXPIRATION,
                description="GL policy must not be expired",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                severity=Severity.CRITICAL,
            ),
            sample_gl_requirement(min_cents=100_000_000),
        ]

        result = self.evaluator.evaluate(
            document_set=doc_set,
            requirements=omitted_reqs,
            evaluation_date=review_date,
        )

        # Must not be COMPLIANT
        self.assertNotEqual(result.overall_status, ComplianceStatus.COMPLIANT)
        self.assertEqual(result.overall_status, ComplianceStatus.NON_COMPLIANT)

        # In addition, when requirement profile is properly validated, the effective date requirement
        # is explicitly injected and evaluated.
        hardened_reqs = validate_requirement_profile(omitted_reqs)
        result_hardened = self.evaluator.evaluate(
            document_set=doc_set,
            requirements=hardened_reqs,
            evaluation_date=review_date,
        )
        self.assertEqual(result_hardened.overall_status, ComplianceStatus.NON_COMPLIANT)
        eff_findings = [
            f for f in result_hardened.findings
            if f.rule_type == RequirementType.POLICY_EFFECTIVE_DATE
        ]
        self.assertEqual(len(eff_findings), 1)
        self.assertEqual(eff_findings[0].status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(eff_findings[0].reason_code, "POLICY_FUTURE_INCEPTION")


class TestPhase723WorkPackageB(unittest.TestCase):
    """Work Package B: Manuscript Endorsement & Restrictive Wording Safeguards."""

    def setUp(self):
        self.evaluator = ComplianceEvaluator()

    def test_pipeline_forwards_manuscript_and_form_attributes(self):
        """Pipeline must forward is_standard_form and manuscript fields from raw extraction."""
        raw = RawInsuranceExtraction(
            document_type="endorsement",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="MANUSCRIPT-CG-2010",
                    is_standard_form=False,
                    manuscript_clause_detected=True,
                    manuscript_status="RESTRICTIVE_MANUSCRIPT",
                    restriction_description="Restricted to work performed strictly within 2024",
                    restriction_reason_code="AMBIGUOUS_ENDORSEMENT_TERMS",
                    confidence=0.9,
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="Restricted to work performed strictly within 2024",
                    ),
                )
            ],
            is_document_set_complete=True,
        )
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=raw)
        )
        inspection = PDFDocumentInspection(
            document_id="DOC-END-01",
            filename="endorsement.pdf",
            file_size_bytes=1000,
            content_hash_sha256="hash_end_01",
            page_count=1,
            classified_type=DocumentType.ENDORSEMENT,
            classification_confidence=0.95,
            classification_reason="Endorsement",
            pages=[
                PDFPageInspection(
                    page_number=1,
                    width_pts=612,
                    height_pts=792,
                    text_content="Policy Endorsement: Additional Insured. Restricted to work performed strictly within 2024.",
                )
            ],
        )

        result = pipeline.process_inspection(inspection, subcontractor_id="SUB-B")
        doc_set = result.document_set
        self.assertIsNotNone(doc_set)
        self.assertEqual(len(doc_set.endorsements), 1)
        end = doc_set.endorsements[0]
        self.assertFalse(end.is_standard_form)
        self.assertTrue(end.manuscript_clause_detected)
        self.assertEqual(end.manuscript_status, "RESTRICTIVE_MANUSCRIPT")
        self.assertEqual(end.restriction_reason_code, "AMBIGUOUS_ENDORSEMENT_TERMS")

    def test_manuscript_clause_triggers_human_review_in_compliance(self):
        """An endorsement with manuscript clauses detected must escalate to REQUIRES_HUMAN_REVIEW."""
        endorsement = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="MANUSCRIPT-AI-01",
            is_standard_form=False,
            manuscript_clause_detected=True,
            manuscript_status="RESTRICTIVE_MANUSCRIPT",
            restriction_description="Restricted coverage",
            restriction_reason_code="AMBIGUOUS_ENDORSEMENT_TERMS",
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(
                doc_type=DocumentType.ENDORSEMENT,
                text="Restricted coverage",
            ),
        )

        doc_set = sample_subcontractor_document_set(endorsements=[endorsement])

        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured endorsement mandatory",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            severity=Severity.CRITICAL,
        )

        result = self.evaluator.evaluate(document_set=doc_set, requirements=[req], evaluation_date=EVAL_DATE)
        self.assertEqual(result.overall_status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        finding = [
            f for f in result.findings
            if f.rule_type == RequirementType.ENDORSEMENT_PRESENCE
        ][0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "AMBIGUOUS_ENDORSEMENT_TERMS")
        self.assertTrue(finding.requires_human_review)


class TestPhase723WorkPackageC(unittest.TestCase):
    """Work Package C: Cross-Document Reconciliation Engine."""

    def test_cross_document_date_contradiction(self):
        """Cross-document effective date discrepancy must be detected and escalated."""
        cov1 = sample_gl_coverage(eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        cov2 = sample_gl_coverage(eff=date(2025, 6, 1), exp=date(2026, 6, 1))  # Mismatched effective date

        doc_set = sample_subcontractor_document_set(gl_coverage=cov1)
        doc_set.coverages.append(cov2)

        reconcile_cross_document_set(doc_set)

        self.assertTrue(len(doc_set.raw_contradictions_detected) > 0)
        self.assertTrue(any("effective dates" in c.lower() for c in doc_set.raw_contradictions_detected))

        evaluator = ComplianceEvaluator()
        result = evaluator.evaluate(doc_set, [sample_gl_requirement()], EVAL_DATE)
        self.assertEqual(result.overall_status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)

    def test_cross_document_coverage_vs_endorsement_date_contradiction(self):
        """Conflicting effective dates between coverage and endorsement must be detected."""
        cov = sample_gl_coverage(eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            applicable_policy=cov.policy_number,
            effective_date=date(2025, 7, 1),  # Conflicting effective date
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=True,
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Additional Insured"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])
        reconcile_cross_document_set(doc_set)

        self.assertTrue(len(doc_set.raw_contradictions_detected) > 0)
        self.assertTrue(any("CONTRADICTORY_POLICY_DATES_DETECTED" in c for c in doc_set.raw_contradictions_detected))

        evaluator = ComplianceEvaluator()
        result = evaluator.evaluate(doc_set, [sample_gl_requirement()], EVAL_DATE)
        self.assertEqual(result.overall_status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)


class TestPhase723WorkPackageD(unittest.TestCase):
    """Work Package D: Target Scenario Validations (SCEN-16, 24, 27, 36)."""

    def setUp(self):
        self.evaluator = ComplianceEvaluator()

    def test_scen_16_future_effective_date_prevents_false_compliance(self):
        """SCEN-16: Future effective date policy MUST NOT be evaluated as COMPLIANT."""
        review_date = date(2025, 6, 1)
        cov = sample_gl_coverage(
            eff=date(2025, 9, 1),  # Future inception
            exp=date(2026, 9, 1),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)
        reqs = [
            InsuranceRequirement(
                requirement_id="REQ_GL_EXP",
                rule_type=RequirementType.POLICY_EXPIRATION,
                description="GL policy must not be expired",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                severity=Severity.CRITICAL,
            ),
            sample_gl_requirement(min_cents=100_000_000),
        ]

        res = self.evaluator.evaluate(doc_set, reqs, evaluation_date=review_date)
        self.assertEqual(res.overall_status, ComplianceStatus.NON_COMPLIANT)

    def test_scen_24_manuscript_limitation_routes_to_human_review(self):
        """SCEN-24: Manuscript exclusion / limitation routes to human review."""
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            is_standard_form=False,
            manuscript_clause_detected=True,
            manuscript_status="RESTRICTIVE_MANUSCRIPT",
            restriction_description="Custom restriction clause",
            restriction_reason_code="AMBIGUOUS_ENDORSEMENT_TERMS",
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Custom restriction clause"),
        )
        doc_set = sample_subcontractor_document_set(endorsements=[end])
        reqs = [
            InsuranceRequirement(
                requirement_id="REQ_AI",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE,
                description="Additional Insured endorsement mandatory",
                required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                severity=Severity.CRITICAL,
            )
        ]
        res = self.evaluator.evaluate(doc_set, reqs, evaluation_date=EVAL_DATE)
        self.assertEqual(res.overall_status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)

    def test_scen_27_agreement_date_mismatch_routes_to_human_review(self):
        """SCEN-27: Agreement date mismatch routes to human review."""
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            is_standard_form=False,
            manuscript_clause_detected=True,
            manuscript_status="RESTRICTIVE_MANUSCRIPT",
            restriction_description="Agreement date mismatch",
            restriction_reason_code="ENDORSEMENT_AGREEMENT_DATE_MISMATCH",
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Agreement date mismatch"),
        )
        doc_set = sample_subcontractor_document_set(endorsements=[end])
        reqs = [
            InsuranceRequirement(
                requirement_id="REQ_AI",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE,
                description="Additional Insured endorsement mandatory",
                required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                severity=Severity.CRITICAL,
            )
        ]
        res = self.evaluator.evaluate(doc_set, reqs, evaluation_date=EVAL_DATE)
        self.assertEqual(res.overall_status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)

    def test_scen_36_actual_date_contradiction_routes_to_human_review(self):
        """SCEN-36: Contradictory policy effective dates between documents routes to human review."""
        cov = sample_gl_coverage(eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        cov.policy_number = "GL-98765432"
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            applicable_policy="GL-98765432",
            effective_date=date(2025, 7, 1),  # Mismatch with coverage 01/01/2025
            named_entities=["Contradictory Party Inc"],
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=True,
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Additional Insured"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])
        doc_set.certificate_holder = EntityInfo(
            name="General Contractor Corp",
            confidence=0.95,
            evidence=make_evidence(text="General Contractor Corp"),
        )
        reconcile_cross_document_set(doc_set)

        self.assertTrue(any("CONTRADICTORY_POLICY_DATES_DETECTED" in c for c in doc_set.raw_contradictions_detected))

        reqs = [
            InsuranceRequirement(
                requirement_id="REQ_AI",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE,
                description="Additional Insured endorsement mandatory",
                required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                severity=Severity.CRITICAL,
            )
        ]
        res = self.evaluator.evaluate(doc_set, reqs, evaluation_date=EVAL_DATE)
        self.assertEqual(res.overall_status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)


# ============================================================================
# Full 18-Category Compliance Coverage Suite (Work Package E)
# ============================================================================

class TestEighteenComplianceCategories(unittest.TestCase):
    """Complete 18-category test matrix mandated by Phase 7.2.3.2."""

    def setUp(self):
        self.evaluator = ComplianceEvaluator()
        self.eval_date = date(2025, 6, 1)

    # -------------------------------------------------------------------------
    # Category 1: Missing effective date
    # -------------------------------------------------------------------------
    def test_cat01_missing_effective_date(self):
        """Cat 1: Omitted effective date produces MISSING_EVIDENCE and EFFECTIVE_DATE_MISSING."""
        cov = sample_gl_coverage()
        cov.effective_date = None
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        req = InsuranceRequirement(
            requirement_id="REQ_EFF",
            description="Test requirement",
            rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
            coverage_type=CoverageType.GENERAL_LIABILITY,
            severity=Severity.CRITICAL,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "EFFECTIVE_DATE_MISSING")
        self.assertTrue(finding.requires_human_review)

    # -------------------------------------------------------------------------
    # Category 2: Future effective date
    # -------------------------------------------------------------------------
    def test_cat02_future_effective_date(self):
        """Cat 2: Future effective date produces NON_COMPLIANT with POLICY_FUTURE_INCEPTION."""
        future_date = date(2025, 9, 1)  # After eval_date 2025-06-01
        cov = sample_gl_coverage(eff=future_date, exp=date(2026, 9, 1))
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        req = InsuranceRequirement(
            requirement_id="REQ_EFF",
            description="Test requirement",
            rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
            coverage_type=CoverageType.GENERAL_LIABILITY,
            severity=Severity.CRITICAL,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(finding.reason_code, "POLICY_FUTURE_INCEPTION")
        self.assertTrue(finding.requires_human_review)

    # -------------------------------------------------------------------------
    # Category 3: Low-confidence effective date
    # -------------------------------------------------------------------------
    def test_cat03_low_confidence_effective_date(self):
        """Cat 3: Effective date confidence < 0.70 produces UNABLE_TO_DETERMINE."""
        cov = sample_gl_coverage(eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        cov.effective_date.confidence = 0.55
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        req = InsuranceRequirement(
            requirement_id="REQ_EFF",
            description="Test requirement",
            rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
            coverage_type=CoverageType.GENERAL_LIABILITY,
            severity=Severity.CRITICAL,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(finding.reason_code, "EFFECTIVE_DATE_CONFIDENCE_LOW")
        self.assertTrue(finding.requires_human_review)

    # -------------------------------------------------------------------------
    # Category 4: Unsupported date evidence
    # -------------------------------------------------------------------------
    def test_cat04_unsupported_date_evidence(self):
        """Cat 4: QUOTE_PRESENT_UNVERIFIED_SUPPORT on effective date routes to human review."""
        cov = sample_gl_coverage()
        cov.extraction_status = ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT
        doc_set = sample_subcontractor_document_set(gl_coverage=cov)

        req = InsuranceRequirement(
            requirement_id="REQ_EFF",
            description="Test requirement",
            rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
            coverage_type=CoverageType.GENERAL_LIABILITY,
            severity=Severity.CRITICAL,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "EFFECTIVE_DATE_EVIDENCE_SUPPORT_UNVERIFIED")
        self.assertTrue(finding.requires_human_review)

    # -------------------------------------------------------------------------
    # Category 5: Conflicting dates across documents
    # -------------------------------------------------------------------------
    def test_cat05_conflicting_dates_across_documents(self):
        """Cat 5: Effective date mismatch between COI and endorsement raises CONTRADICTORY_POLICY_DATES_DETECTED."""
        cov = sample_gl_coverage(eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            applicable_policy=cov.policy_number,
            effective_date=date(2025, 6, 15),  # Contradicts 2025-01-01
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=True,
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Additional Insured"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])
        reconcile_cross_document_set(doc_set)

        self.assertTrue(any("CONTRADICTORY_POLICY_DATES_DETECTED" in c for c in doc_set.raw_contradictions_detected))
        res = self.evaluator.evaluate(doc_set, [sample_gl_requirement()], self.eval_date)
        self.assertEqual(res.overall_status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)

    # -------------------------------------------------------------------------
    # Category 6: Equivalent dates represented differently
    # -------------------------------------------------------------------------
    def test_cat06_equivalent_dates_represented_differently(self):
        """Cat 6: Semantically equal dates represented with different raw text resolve as valid without contradiction."""
        cov = sample_gl_coverage(eff=date(2025, 1, 1), exp=date(2026, 1, 1))
        cov.effective_date.raw_text = "01/01/2025"
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            applicable_policy=cov.policy_number,
            effective_date=date(2025, 1, 1),  # Same date
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=True,
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Additional Insured"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])
        reconcile_cross_document_set(doc_set)

        # No contradiction detected
        self.assertEqual(len(doc_set.raw_contradictions_detected), 0)

        req = InsuranceRequirement(
            requirement_id="REQ_EFF",
            description="Test requirement",
            rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
            coverage_type=CoverageType.GENERAL_LIABILITY,
            severity=Severity.CRITICAL,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(finding.reason_code, "EFFECTIVE_DATE_VALID")

    # -------------------------------------------------------------------------
    # Category 7: Missing endorsement classification
    # -------------------------------------------------------------------------
    def test_cat07_missing_endorsement_classification(self):
        """Cat 7: Completely missing classification fields cannot silently pass; routes to human review."""
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            is_standard_form=None,
            manuscript_clause_detected=None,
            manuscript_status=None,
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Additional Insured"),
        )
        doc_set = sample_subcontractor_document_set(endorsements=[end])
        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Test requirement",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            severity=Severity.CRITICAL,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_FORM_UNCERTAIN")
        self.assertTrue(finding.requires_human_review)

    # -------------------------------------------------------------------------
    # Category 8: Unknown standard-form status
    # -------------------------------------------------------------------------
    def test_cat08_unknown_standard_form_status(self):
        """Cat 8: is_standard_form=None routes to human review with ENDORSEMENT_FORM_UNCERTAIN."""
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            is_standard_form=None,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Additional Insured"),
        )
        doc_set = sample_subcontractor_document_set(endorsements=[end])
        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Test requirement",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            severity=Severity.CRITICAL,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_FORM_UNCERTAIN")

    # -------------------------------------------------------------------------
    # Category 9: Unknown manuscript status
    # -------------------------------------------------------------------------
    def test_cat09_unknown_manuscript_status(self):
        """Cat 9: manuscript_status='UNCERTAIN' routes to human review."""
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            is_standard_form=True,
            manuscript_clause_detected=None,
            manuscript_status="UNCERTAIN",
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Additional Insured"),
        )
        doc_set = sample_subcontractor_document_set(endorsements=[end])
        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Test requirement",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            severity=Severity.CRITICAL,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_FORM_UNCERTAIN")

    # -------------------------------------------------------------------------
    # Category 10: Explicit manuscript restrictions
    # -------------------------------------------------------------------------
    def test_cat10_explicit_manuscript_restrictions(self):
        """Cat 10: Detected manuscript clause routes to human review with specific restriction code."""
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            is_standard_form=True,
            manuscript_clause_detected=True,
            manuscript_status="DETECTED",
            restriction_reason_code="MANUSCRIPT_RESTRICTION_DETECTED",
            restriction_description="Custom residential exclusion clause added",
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Custom residential exclusion"),
        )
        doc_set = sample_subcontractor_document_set(endorsements=[end])
        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Test requirement",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            severity=Severity.CRITICAL,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_RESTRICTION_DETECTED")
        self.assertTrue(finding.requires_human_review)

    # -------------------------------------------------------------------------
    # Category 11: Non-standard endorsement
    # -------------------------------------------------------------------------
    def test_cat11_non_standard_endorsement(self):
        """Cat 11: Proprietary non-standard form (is_standard_form=False) routes to human review."""
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="PROPRIETARY-FORM-99",
            is_standard_form=False,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Proprietary endorsement form"),
        )
        doc_set = sample_subcontractor_document_set(endorsements=[end])
        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Test requirement",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            severity=Severity.CRITICAL,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "NON_STANDARD_ENDORSEMENT_FORM")

    # -------------------------------------------------------------------------
    # Category 12: Low-confidence endorsement extraction
    # -------------------------------------------------------------------------
    def test_cat12_low_confidence_endorsement_extraction(self):
        """Cat 12: Endorsement confidence < 0.70 produces UNABLE_TO_DETERMINE."""
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            confidence=0.55,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Additional Insured"),
        )
        doc_set = sample_subcontractor_document_set(endorsements=[end])
        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Test requirement",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            severity=Severity.CRITICAL,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_CONFIDENCE_LOW")
        self.assertTrue(finding.requires_human_review)

    # -------------------------------------------------------------------------
    # Category 13: Missing endorsement evidence
    # -------------------------------------------------------------------------
    def test_cat13_missing_endorsement_evidence(self):
        """Cat 13: Mandatory evidence required but endorsement has no evidence -> MISSING_EVIDENCE."""
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=True,
            confidence=0.95,
            evidence=None,  # No evidence text
        )
        doc_set = sample_subcontractor_document_set(endorsements=[end])
        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Test requirement",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            mandatory_evidence=True,
            severity=Severity.CRITICAL,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_EVIDENCE_TEXT_MISSING")

    # -------------------------------------------------------------------------
    # Category 14: Equivalent coverage limits
    # -------------------------------------------------------------------------
    def test_cat14_equivalent_coverage_limits(self):
        """Cat 14: Matching coverage limits across multiple documents generate no contradictions."""
        cov1 = sample_gl_coverage(occ_cents=100_000_000, agg_cents=200_000_000)
        cov2 = sample_gl_coverage(occ_cents=100_000_000, agg_cents=200_000_000)
        doc_set = sample_subcontractor_document_set(gl_coverage=cov1)
        doc_set.coverages.append(cov2)

        reconcile_cross_document_set(doc_set)
        limit_contradictions = [c for c in doc_set.raw_contradictions_detected if "LIMITS" in c]
        self.assertEqual(len(limit_contradictions), 0)

    # -------------------------------------------------------------------------
    # Category 15: Contradictory coverage limits
    # -------------------------------------------------------------------------
    def test_cat15_contradictory_coverage_limits(self):
        """Cat 15: Mismatched coverage limits across documents flag CONTRADICTORY_POLICY_LIMITS_DETECTED."""
        cov1 = sample_gl_coverage(occ_cents=100_000_000)
        cov2 = sample_gl_coverage(occ_cents=50_000_000)  # Contradicts $1M
        doc_set = sample_subcontractor_document_set(gl_coverage=cov1)
        doc_set.coverages.append(cov2)

        reconcile_cross_document_set(doc_set)
        self.assertTrue(any("CONTRADICTORY_POLICY_LIMITS_DETECTED" in c for c in doc_set.raw_contradictions_detected))

    # -------------------------------------------------------------------------
    # Category 16: Named insured versus certificate holder distinction
    # -------------------------------------------------------------------------
    def test_cat16_named_insured_vs_cert_holder_distinction(self):
        """Cat 16: System cleanly distinguishes Named Insured from Certificate Holder roles."""
        cov = sample_gl_coverage()
        doc_set = sample_subcontractor_document_set(
            named_insured_name="Apex Electrical Contractors LLC",
            gl_coverage=cov,
        )
        doc_set.certificate_holder = EntityInfo(
            name="General Contractor Corp",
            confidence=0.98,
            evidence=make_evidence(text="CERTIFICATE HOLDER: General Contractor Corp"),
        )

        req_ni = InsuranceRequirement(
            requirement_id="REQ_NI",
            description="Test requirement",
            rule_type=RequirementType.NAMED_INSURED,
            required_entity_name="Apex Electrical Contractors LLC",
            severity=Severity.CRITICAL,
        )
        res = self.evaluator.evaluate(doc_set, [req_ni], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(finding.reason_code, "NAMED_INSURED_EXACT_MATCH")
        self.assertEqual(finding.found_value["extracted_name"], "Apex Electrical Contractors LLC")

    # -------------------------------------------------------------------------
    # Category 17: Scheduled-entity reconciliation
    # -------------------------------------------------------------------------
    def test_cat17_scheduled_entity_reconciliation(self):
        """Cat 17: Third-party scheduled entity does not falsely flag contradiction; contract requirement is enforced."""
        cov = sample_gl_coverage()
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            named_entities=["Project Owner Municipal Authority"],  # Legitimate 3rd party
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=True,
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Project Owner Municipal Authority"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])
        doc_set.certificate_holder = EntityInfo(
            name="General Contractor Corp",
            confidence=0.95,
            evidence=make_evidence(text="General Contractor Corp"),
        )

        # 1. Reconciliation does not unconditionally flag third-party as contradiction
        reconcile_cross_document_set(doc_set)
        sched_contradictions = [c for c in doc_set.raw_contradictions_detected if "conflicts with certificate holder" in c]
        self.assertEqual(len(sched_contradictions), 0)

        # 2. General endorsement presence passes without false escalation
        req_general = InsuranceRequirement(
            requirement_id="REQ_AI_GENERAL",
            description="Test requirement",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            severity=Severity.CRITICAL,
        )
        res_general = self.evaluator.evaluate(doc_set, [req_general], self.eval_date)
        self.assertEqual(res_general.findings[0].status, ComplianceStatus.COMPLIANT)

        # 3. Contract-specific required entity on endorsement is enforced
        req_specific = InsuranceRequirement(
            requirement_id="REQ_AI_SPECIFIC",
            description="Test requirement",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            required_entity_name="General Contractor Corp",  # Explicitly requires GC scheduled
            severity=Severity.CRITICAL,
        )
        res_specific = self.evaluator.evaluate(doc_set, [req_specific], self.eval_date)
        self.assertEqual(res_specific.findings[0].status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(res_specific.findings[0].reason_code, "ENDORSEMENT_ENTITY_MISMATCH")

    # -------------------------------------------------------------------------
    # Category 18: Multiple endorsements with conflicting policy references
    # -------------------------------------------------------------------------
    def test_cat18_multiple_endorsements_conflicting_policies(self):
        """Cat 18: Endorsements for same requirement pointing to conflicting policies flag CONTRADICTORY_ENDORSEMENTS_FOUND."""
        end1 = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            applicable_policy="GL-11111111",
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=True,
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Additional Insured Policy 1"),
        )
        end2 = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            applicable_policy="GL-22222222",  # Different policy
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=True,
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Additional Insured Policy 2"),
        )
        doc_set = sample_subcontractor_document_set(endorsements=[end1, end2])
        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Test requirement",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            severity=Severity.CRITICAL,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "CONTRADICTORY_ENDORSEMENTS_FOUND")



class Phase7234RemediationTests(unittest.TestCase):
    """Specific regression and validation tests for Phase 7.2.3.4 remediations.
    
    Covers:
    - Remediation A: Negative manuscript verification basis and standard form provenance.
    - Remediation A: Full-document text scanning for restrictive manuscript clauses.
    - Remediation B: Physical PDF date grounding and generic extraction for SCEN-36.
    """

    def setUp(self):
        self.evaluator = ComplianceEvaluator()
        self.eval_date = date(2025, 6, 1)

    def test_manuscript_not_detected_without_basis_routes_to_human_review(self):
        """Endorsement claiming NOT_DETECTED without defensible verification basis routes to human review."""
        cov = sample_gl_coverage()
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            named_entities=["General Contractor Corp"],
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis=None,  # Missing basis
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="CG 20 10 04 13"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])
        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Additional Insured required",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_VERIFICATION_UNSUPPORTED")

    def test_manuscript_not_detected_with_non_standard_form_routes_to_human_review(self):
        """Endorsement claiming NOT_DETECTED with non-standard form number routes to human review."""
        cov = sample_gl_coverage()
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CUSTOM-MOD-999",  # Non-standard form claimed as standard
            named_entities=["General Contractor Corp"],
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=True,
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="CUSTOM-MOD-999"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])
        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Additional Insured required",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_VERIFICATION_UNSUPPORTED")

    def test_manuscript_explicit_restriction_in_text(self):
        """Full-document text scanning detects restrictive wording and overrides falsely claimed NOT_DETECTED."""
        inspection = PDFDocumentInspection(
            document_id="TEST-DOC-MANUSCRIPT-01",
            filename="endorsement.pdf",
            file_size_bytes=1000,
            content_hash_sha256="abc1",
            classified_type=DocumentType.ENDORSEMENT,
            classification_confidence=0.95,
            classification_reason="test",
            page_count=1,
            processing_status=DocumentProcessingStatus.PROCESSED,
            pages=[
                PDFPageInspection(
                    page_number=1,
                    text_content="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13. Subject to residential work exclusion endorsement.",
                    width_pts=612.0,
                    height_pts=792.0,
                    content_hash_sha256="abc",
                )
            ],
        )
        raw_extraction = RawInsuranceExtraction(
            document_type="endorsement",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    is_standard_form=True,
                    manuscript_clause_detected=False,  # Provider falsely claimed False
                    manuscript_status="NOT_DETECTED",  # Provider falsely claimed NOT_DETECTED
                    manuscript_verification_basis="unmodified_standard_form",
                    confidence=0.95,
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13",
                    ),
                )
            ],
            is_document_set_complete=True,
        )
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=raw_extraction)
        )
        result = pipeline.process_inspection(inspection, subcontractor_id="SUB-MANUSCRIPT-01")
        doc_set = result.document_set
        self.assertIsNotNone(doc_set)
        self.assertEqual(len(doc_set.endorsements), 1)
        end = doc_set.endorsements[0]
        self.assertTrue(end.manuscript_clause_detected)
        self.assertEqual(end.manuscript_status, "DETECTED")
        self.assertEqual(end.restriction_reason_code, "MANUSCRIPT_RESTRICTION_DETECTED")

        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Additional Insured required",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        eval_res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = eval_res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_RESTRICTION_DETECTED")

    def test_manuscript_restriction_outside_quote(self):
        """Pipeline catches restrictive manuscript clause occurring on a different page outside the AI quote."""
        inspection = PDFDocumentInspection(
            document_id="TEST-DOC-MANUSCRIPT-02",
            filename="endorsement_multipage.pdf",
            file_size_bytes=2000,
            content_hash_sha256="abc2",
            classified_type=DocumentType.ENDORSEMENT,
            classification_confidence=0.95,
            classification_reason="test",
            page_count=2,
            processing_status=DocumentProcessingStatus.PROCESSED,
            pages=[
                PDFPageInspection(
                    page_number=1,
                    text_content="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13. ADDITIONAL INSURED - OWNERS, LESSEES OR CONTRACTORS.",
                    width_pts=612.0,
                    height_pts=792.0,
                    content_hash_sha256="abc1",
                ),
                PDFPageInspection(
                    page_number=2,
                    text_content="ENDORSEMENT SCHEDULE: This coverage does not apply to any designated ongoing operations or subcontractor exclusion clause.",
                    width_pts=612.0,
                    height_pts=792.0,
                    content_hash_sha256="abc2",
                ),
            ],
        )
        raw_extraction = RawInsuranceExtraction(
            document_type="endorsement",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    is_standard_form=True,
                    manuscript_clause_detected=False,
                    manuscript_status="NOT_DETECTED",
                    manuscript_verification_basis="unmodified_standard_form",
                    confidence=0.95,
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13",
                    ),
                )
            ],
            is_document_set_complete=True,
        )
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=raw_extraction)
        )
        result = pipeline.process_inspection(inspection, subcontractor_id="SUB-MANUSCRIPT-02")
        end = result.document_set.endorsements[0]
        self.assertTrue(end.manuscript_clause_detected)
        self.assertEqual(end.manuscript_status, "DETECTED")
        self.assertEqual(end.restriction_reason_code, "MANUSCRIPT_RESTRICTION_DETECTED")

    def test_scen_36_date_present_in_pdf_and_detected(self):
        """SCEN-36 contradictory date is physically on the PDF and triggers cross-document contradiction."""
        import os
        from app.documents.processor import PDFProcessor
        processor = PDFProcessor()
        fixture_path = os.path.join(
            os.path.dirname(__file__), "..", "evaluation", "cohorts", "fixtures", "SCEN-36", "SCEN-36_contradictory_doc.pdf"
        )
        with open(fixture_path, "rb") as f:
            pdf_bytes = f.read()
        inspection = processor.process_pdf(pdf_bytes, document_id="SCEN-36-DOC2")
        self.assertIn("07/01/2025", inspection.pages[0].text_content)
        self.assertIn("CG 20 10 04 13", inspection.pages[0].text_content)

        # Build cross-doc scenario
        cov = sample_gl_coverage()
        cov.effective_date = DateValue(date=date(2025, 1, 1), raw_text="01/01/2025", confidence=0.95)
        cov.expiration_date = DateValue(date=date(2026, 1, 1), raw_text="01/01/2026", confidence=0.95)
        cov.policy_number = "GL-98765432"

        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            applicable_policy="GL-98765432",
            effective_date=date(2025, 7, 1),  # 07/01/2025 from PDF
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=True,
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="EFFECTIVE DATE: 07/01/2025"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])
        reconcile_cross_document_set(doc_set)
        self.assertTrue(any("CONTRADICTORY_POLICY_DATES_DETECTED" in c for c in doc_set.raw_contradictions_detected))

        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Additional Insured required",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        self.assertEqual(res.overall_status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertTrue(res.requires_human_review)

    def test_scen_36_date_absent_prevents_date_contradiction(self):
        """When date text is absent from document text, pipeline suppresses ungrounded AI date."""
        inspection = PDFDocumentInspection(
            document_id="TEST-UNGROUNDED-DATE-01",
            filename="endorsement.pdf",
            file_size_bytes=1000,
            content_hash_sha256="abc1",
            classified_type=DocumentType.ENDORSEMENT,
            classification_confidence=0.95,
            classification_reason="test",
            page_count=1,
            processing_status=DocumentProcessingStatus.PROCESSED,
            pages=[
                PDFPageInspection(
                    page_number=1,
                    text_content="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13. ADDITIONAL INSURED - OWNERS, LESSEES.",
                    width_pts=612.0,
                    height_pts=792.0,
                    content_hash_sha256="abc1",
                )
            ],
        )
        raw_extraction = RawInsuranceExtraction(
            document_type="endorsement",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    effective_date="07/01/2025",  # Hallucinated date digits not in document text
                    is_standard_form=True,
                    manuscript_clause_detected=False,
                    manuscript_status="NOT_DETECTED",
                    manuscript_verification_basis="unmodified_standard_form",
                    confidence=0.95,
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13",
                    ),
                )
            ],
            is_document_set_complete=True,
        )
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=raw_extraction)
        )
        result = pipeline.process_inspection(inspection, subcontractor_id="SUB-UNG-01")
        end = result.document_set.endorsements[0]
        # Since '07012025' does not appear in doc text, pipeline resets effective_date to None
        self.assertIsNone(end.effective_date)

    def test_manuscript_provider_full_text_inspection_claim_unsupported(self):
        """Provider-supplied full_text_inspection claim without verifiable standard form provenance routes to human review."""
        cov = sample_gl_coverage()
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            named_entities=["General Contractor Corp"],
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="full_text_inspection",  # Provider assertion without independent verification
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])
        req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Additional Insured required",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        res = self.evaluator.evaluate(doc_set, [req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_VERIFICATION_UNSUPPORTED")


class Phase7236TrustBoundaryRemediationTests(unittest.TestCase):
    """Phase 7.2.3.6: Exhaustive Adversarial Tests for Manuscript Trust-Boundary Closure.

    Guarantees:
    - Provider assertions never independently prove that a document is an unmodified standard form.
    - Negative manuscript claims ('NOT_DETECTED') without independent document-integrity verification
      fall back conservatively to REQUIRES_HUMAN_REVIEW (MANUSCRIPT_VERIFICATION_UNSUPPORTED).
    - True compliance requires verified standard form provenance via independent integrity verification.
    """

    def setUp(self):
        self.evaluator = ComplianceEvaluator()
        self.eval_date = EVAL_DATE
        self.cov = sample_gl_coverage()
        self.req = InsuranceRequirement(
            requirement_id="REQ_AI",
            description="Additional Insured required",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )

    def test_adv_01_recognized_form_provider_claims_unmodified_routes_to_human_review(self):
        """Case 1: Recognized form (CG 20 10) + provider claims NOT_DETECTED + unmodified_standard_form
        WITHOUT independent integrity verification mechanism -> REQUIRES_HUMAN_REVIEW (MANUSCRIPT_VERIFICATION_UNSUPPORTED).
        """
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            named_entities=["General Contractor Corp"],
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",  # Untrusted provider assertion
            independent_integrity_verified=False,                     # No independent verification
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=self.cov, endorsements=[end])
        res = self.evaluator.evaluate(doc_set, [self.req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_VERIFICATION_UNSUPPORTED")
        self.assertTrue(finding.requires_human_review)

    def test_adv_02_recognized_form_provider_claims_full_text_inspection_routes_to_human_review(self):
        """Case 2: Recognized form (CG 20 10) + provider claims NOT_DETECTED + full_text_inspection
        -> REQUIRES_HUMAN_REVIEW (MANUSCRIPT_VERIFICATION_UNSUPPORTED).
        """
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            named_entities=["General Contractor Corp"],
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="full_text_inspection",  # Untrusted provider assertion
            independent_integrity_verified=False,
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=self.cov, endorsements=[end])
        res = self.evaluator.evaluate(doc_set, [self.req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_VERIFICATION_UNSUPPORTED")
        self.assertTrue(finding.requires_human_review)

    def test_adv_03_recognized_form_provider_claims_no_basis_routes_to_human_review(self):
        """Case 3: Recognized form (CG 20 10) + provider claims NOT_DETECTED + basis=None
        -> REQUIRES_HUMAN_REVIEW (MANUSCRIPT_VERIFICATION_UNSUPPORTED).
        """
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            named_entities=["General Contractor Corp"],
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis=None,
            independent_integrity_verified=False,
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=self.cov, endorsements=[end])
        res = self.evaluator.evaluate(doc_set, [self.req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_VERIFICATION_UNSUPPORTED")

    def test_adv_04_recognized_form_custom_restriction_in_doc_text_routes_to_restriction_detected(self):
        """Case 4: Recognized form (CG 20 10) + provider falsely claims NOT_DETECTED + custom restriction in doc text
        -> pipeline full text scan catches restriction -> REQUIRES_HUMAN_REVIEW (MANUSCRIPT_RESTRICTION_DETECTED).
        """
        inspection = PDFDocumentInspection(
            document_id="TEST-DOC-ADV-04",
            filename="endorsement_with_restriction.pdf",
            file_size_bytes=1000,
            content_hash_sha256="abc_adv_04",
            classified_type=DocumentType.ENDORSEMENT,
            classification_confidence=0.95,
            classification_reason="test",
            page_count=1,
            processing_status=DocumentProcessingStatus.PROCESSED,
            pages=[
                PDFPageInspection(
                    page_number=1,
                    text_content="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13. Subject to residential work exclusion endorsement.",
                    width_pts=612.0,
                    height_pts=792.0,
                    content_hash_sha256="hash_adv_04",
                )
            ],
        )
        raw_extraction = RawInsuranceExtraction(
            document_type="endorsement",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    is_standard_form=True,
                    manuscript_clause_detected=False,  # Provider falsely claims clean
                    manuscript_status="NOT_DETECTED",
                    manuscript_verification_basis="unmodified_standard_form",
                    confidence=0.95,
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13",
                    ),
                )
            ],
            is_document_set_complete=True,
        )
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=raw_extraction)
        )
        result = pipeline.process_inspection(inspection, subcontractor_id="SUB-ADV-04")
        doc_set = result.document_set
        self.assertIsNotNone(doc_set)
        end = doc_set.endorsements[0]
        self.assertTrue(end.manuscript_clause_detected)
        self.assertEqual(end.manuscript_status, "DETECTED")

        eval_res = self.evaluator.evaluate(doc_set, [self.req], self.eval_date)
        finding = eval_res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_RESTRICTION_DETECTED")

    def test_adv_05_recognized_form_fabricated_variation_routes_to_human_review(self):
        """Case 5: Recognized form base with fabricated/unrecognized edition suffix (e.g. CG 20 10 99 99)
        -> REQUIRES_HUMAN_REVIEW (MANUSCRIPT_VERIFICATION_UNSUPPORTED).
        """
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 99 99",  # Fabricated edition
            named_entities=["General Contractor Corp"],
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=False,  # Unverified fabricated edition date cannot have independent verification
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="CG 20 10 99 99"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=self.cov, endorsements=[end])
        res = self.evaluator.evaluate(doc_set, [self.req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_VERIFICATION_UNSUPPORTED")

    def test_adv_06_non_standard_form_provider_claims_not_detected_routes_to_human_review(self):
        """Case 6: Non-standard proprietary form + provider claims NOT_DETECTED
        -> REQUIRES_HUMAN_REVIEW (NON_STANDARD_ENDORSEMENT_FORM or MANUSCRIPT_VERIFICATION_UNSUPPORTED).
        """
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="MANUSCRIPT-PROPRIETARY-2026",
            named_entities=["General Contractor Corp"],
            is_standard_form=False,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="full_text_inspection",
            independent_integrity_verified=False,
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="MANUSCRIPT-PROPRIETARY-2026"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=self.cov, endorsements=[end])
        res = self.evaluator.evaluate(doc_set, [self.req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertIn(finding.reason_code, ["NON_STANDARD_ENDORSEMENT_FORM", "MANUSCRIPT_VERIFICATION_UNSUPPORTED"])

    def test_adv_07_missing_form_number_provider_claims_not_detected_routes_to_human_review(self):
        """Case 7: Missing form number (None) + provider claims NOT_DETECTED
        -> REQUIRES_HUMAN_REVIEW (MANUSCRIPT_VERIFICATION_UNSUPPORTED or ENDORSEMENT_FORM_UNCERTAIN).
        """
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number=None,
            named_entities=["General Contractor Corp"],
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=False,
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Additional Insured Clause"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=self.cov, endorsements=[end])
        res = self.evaluator.evaluate(doc_set, [self.req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertIn(finding.reason_code, ["MANUSCRIPT_VERIFICATION_UNSUPPORTED", "ENDORSEMENT_FORM_UNCERTAIN"])

    def test_adv_08_uncertain_manuscript_status_routes_to_human_review(self):
        """Case 8: Uncertain manuscript classification ('UNCERTAIN')
        -> REQUIRES_HUMAN_REVIEW (ENDORSEMENT_FORM_UNCERTAIN).
        """
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            named_entities=["General Contractor Corp"],
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="UNCERTAIN",
            manuscript_verification_basis=None,
            independent_integrity_verified=False,
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="CG 20 10 04 13"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=self.cov, endorsements=[end])
        res = self.evaluator.evaluate(doc_set, [self.req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_FORM_UNCERTAIN")

    def test_adv_09_restrictive_clause_outside_provider_quoted_text_routes_to_restriction_detected(self):
        """Case 9: Restrictive clause outside the AI-quoted text on another page
        -> pipeline full text scan catches it -> REQUIRES_HUMAN_REVIEW (MANUSCRIPT_RESTRICTION_DETECTED).
        """
        inspection = PDFDocumentInspection(
            document_id="TEST-DOC-ADV-09",
            filename="endorsement_multipage.pdf",
            file_size_bytes=2000,
            content_hash_sha256="abc_adv_09",
            classified_type=DocumentType.ENDORSEMENT,
            classification_confidence=0.95,
            classification_reason="multipage endorsement",
            page_count=2,
            processing_status=DocumentProcessingStatus.PROCESSED,
            pages=[
                PDFPageInspection(
                    page_number=1,
                    text_content="Page 1: COMMERCIAL GENERAL LIABILITY CG 20 10 04 13 Additional Insured",
                    width_pts=612.0,
                    height_pts=792.0,
                    content_hash_sha256="p1_hash",
                ),
                PDFPageInspection(
                    page_number=2,
                    text_content="Page 2: General Provisions. Residential operations are excluded from coverage under this policy.",
                    width_pts=612.0,
                    height_pts=792.0,
                    content_hash_sha256="p2_hash",
                ),
            ],
        )
        raw_extraction = RawInsuranceExtraction(
            document_type="endorsement",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    is_standard_form=True,
                    manuscript_clause_detected=False,  # Quote on page 1 only; claims clean
                    manuscript_status="NOT_DETECTED",
                    manuscript_verification_basis="unmodified_standard_form",
                    confidence=0.95,
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13 Additional Insured",
                    ),
                )
            ],
            is_document_set_complete=True,
        )
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=raw_extraction)
        )
        result = pipeline.process_inspection(inspection, subcontractor_id="SUB-ADV-09")
        doc_set = result.document_set
        self.assertIsNotNone(doc_set)
        end = doc_set.endorsements[0]
        self.assertTrue(end.manuscript_clause_detected)
        self.assertEqual(end.manuscript_status, "DETECTED")

        eval_res = self.evaluator.evaluate(doc_set, [self.req], self.eval_date)
        finding = eval_res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_RESTRICTION_DETECTED")

    def test_adv_10_clean_recognized_form_without_independent_integrity_routes_to_human_review(self):
        """Case 10: Clean recognized standard form without independent integrity verification mechanism
        -> REQUIRES_HUMAN_REVIEW (MANUSCRIPT_VERIFICATION_UNSUPPORTED).
        """
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            named_entities=["General Contractor Corp"],
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=False,  # No independent verification
            confidence=0.99,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=self.cov, endorsements=[end])
        res = self.evaluator.evaluate(doc_set, [self.req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "MANUSCRIPT_VERIFICATION_UNSUPPORTED")
        self.assertTrue(finding.requires_human_review)

    def test_adv_11_positive_control_clean_recognized_form_with_independent_integrity_complies(self):
        """Case 11 (Positive Control): Clean recognized standard form WITH independent integrity verification mechanism
        -> COMPLIANT (ENDORSEMENT_VERIFIED).
        """
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10 04 13",
            named_entities=["General Contractor Corp"],
            is_standard_form=True,
            manuscript_clause_detected=False,
            manuscript_status="NOT_DETECTED",
            manuscript_verification_basis="unmodified_standard_form",
            independent_integrity_verified=True,  # Independent verification mechanism verified standard form
            confidence=0.99,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=self.cov, endorsements=[end])
        res = self.evaluator.evaluate(doc_set, [self.req], self.eval_date)
        finding = res.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.COMPLIANT)
        self.assertEqual(finding.reason_code, "ENDORSEMENT_VERIFIED")
        self.assertFalse(finding.requires_human_review)


if __name__ == "__main__":
    unittest.main()
