"""Phase 7.2.3 Compliance Safeguards and Regression Tests.

Tests the four key work packages implemented in Phase 7.2.3:
- WP-A: Defensive active-term enforcement & future effective date protection.
- WP-B: Manuscript endorsement & restrictive wording detection.
- WP-C: Deterministic cross-document reconciliation engine.
- WP-D: Target scenario regression tests (SCEN-16, SCEN-24, SCEN-27, SCEN-36).
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

    def test_cross_document_scheduled_entity_contradiction(self):
        """Contradiction between COI cert holder / named insured and Endorsement schedule must escalate."""
        cov = sample_gl_coverage()
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            named_entities=["Contradictory Party Inc"],
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Contradictory Party Inc"),
        )

        doc_set = sample_subcontractor_document_set(
            named_insured_name="Apex Electrical Contractors LLC",
            gl_coverage=cov,
            endorsements=[end],
        )
        doc_set.certificate_holder = EntityInfo(
            name="General Contractor Corp",
            confidence=0.95,
            evidence=make_evidence(text="General Contractor Corp"),
        )

        reconcile_cross_document_set(doc_set)
        self.assertTrue(len(doc_set.raw_contradictions_detected) > 0)
        self.assertTrue(any("conflicts with certificate holder" in c for c in doc_set.raw_contradictions_detected))

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

    def test_scen_36_contradictory_schedule_routes_to_human_review(self):
        """SCEN-36: Endorsement scheduling a contradictory party routes to human review."""
        cov = sample_gl_coverage()
        end = EndorsementExtraction(
            endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            form_number="CG 20 10",
            named_entities=["Contradictory Party Inc"],
            confidence=0.95,
            extraction_status=ExtractionStatus.EXTRACTED,
            evidence=make_evidence(doc_type=DocumentType.ENDORSEMENT, text="Contradictory Party Inc"),
        )
        doc_set = sample_subcontractor_document_set(gl_coverage=cov, endorsements=[end])
        doc_set.certificate_holder = EntityInfo(
            name="General Contractor Corp",
            confidence=0.95,
            evidence=make_evidence(text="General Contractor Corp"),
        )
        reconcile_cross_document_set(doc_set)

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


if __name__ == "__main__":
    unittest.main()
