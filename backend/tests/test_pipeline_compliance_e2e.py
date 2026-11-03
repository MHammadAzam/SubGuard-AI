"""End-to-end integration tests connecting the extraction pipeline and compliance evaluator.

Validates the full data pipeline:
PDF bytes -> PDFProcessor -> MockExtractionProvider -> ExtractionPipelineService -> ExtractedDocumentSet -> ComplianceEvaluator -> ComplianceReviewResult

Exercises:
1. Seven core end-to-end compliance scenarios using synthetic PDF fixtures.
2. Endorsement confidence propagation regression tests verifying that raw model
   confidence survives pipeline normalization and influences deterministic compliance.
"""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from app.compliance.evaluator import ComplianceEvaluator
from app.documents.processor import PDFProcessor
from app.extraction.pipeline import ExtractionPipelineService
from app.extraction.providers.mock import MockExtractionProvider
from app.schemas.insurance import (
    ComplianceStatus,
    CoverageType,
    EndorsementType,
    ExtractionStatus,
    InsuranceRequirement,
    LimitField,
    RequirementType,
)
from app.schemas.raw_extraction import (
    RawCoverageSection,
    RawEndorsement,
    RawEntityExtraction,
    RawEvidenceQuote,
    RawInsuranceExtraction,
)
from evaluation.fixtures.synthetic_generator import generate_all_synthetic_fixtures, get_fixtures_dir


class TestPipelineComplianceE2E(unittest.TestCase):
    """End-to-end pipeline-to-evaluator integration test suite."""

    @classmethod
    def setUpClass(cls):
        generate_all_synthetic_fixtures()
        cls.fixtures_dir = get_fixtures_dir()
        cls.pdf_processor = PDFProcessor(render_dpi=72)
        cls.evaluator = ComplianceEvaluator()
        cls.evaluation_date = date(2025, 6, 15)

    def _read_fixture(self, filename: str) -> bytes:
        pdf_path = self.fixtures_dir / filename
        self.assertTrue(pdf_path.exists(), f"Missing required synthetic fixture: {pdf_path}")
        return pdf_path.read_bytes()

    # =========================================================================
    # Task 2: Seven End-to-End Synthetic Scenarios
    # =========================================================================

    def test_e2e_scenario_1_fully_compliant_document_set(self):
        """Scenario 1: Clean ACORD 25 with full limits, active dates, and matching entity -> COMPLIANT."""
        pdf_bytes = self._read_fixture("synthetic_standard_acord25.pdf")
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="E2E-DOC-01")

        canned_extraction = RawInsuranceExtraction(
            document_type="coi",
            named_insured=RawEntityExtraction(
                name="Apex Electrical Contractors LLC",
                evidence=RawEvidenceQuote(
                    page_number=1,
                    source_text="INSURED: Apex Electrical Contractors LLC",
                ),
            ),
            coverages=[
                RawCoverageSection(
                    coverage_type="general_liability",
                    policy_number="GL-98765432",
                    each_occurrence="$1,000,000",
                    general_aggregate="$2,000,000",
                    effective_date="01/01/2025",
                    expiration_date="01/01/2026",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="EACH OCCURRENCE: $1,000,000",
                    ),
                )
            ],
            endorsements=[],
            raw_contradictions=[],
            is_document_set_complete=True,
        )

        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=canned_extraction)
        )
        pipeline_result = pipeline.process_inspection(inspection, subcontractor_id="SUB-01")
        self.assertIsNotNone(pipeline_result.document_set)

        requirements = [
            InsuranceRequirement(
                requirement_id="REQ_GL_LIMIT",
                rule_type=RequirementType.COVERAGE_LIMIT,
                description="GL Each Occurrence >= $1,000,000",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                limit_field=LimitField.EACH_OCCURRENCE,
                min_amount_cents=100_000_000,
            ),
            InsuranceRequirement(
                requirement_id="REQ_GL_EXP",
                rule_type=RequirementType.POLICY_EXPIRATION,
                description="GL policy must not be expired",
                coverage_type=CoverageType.GENERAL_LIABILITY,
            ),
            InsuranceRequirement(
                requirement_id="REQ_NI",
                rule_type=RequirementType.NAMED_INSURED,
                description="Named insured must match Apex Electrical Contractors LLC",
                required_entity_name="Apex Electrical Contractors LLC",
            ),
        ]

        review_result = self.evaluator.evaluate(
            document_set=pipeline_result.document_set,
            requirements=requirements,
            evaluation_date=self.evaluation_date,
        )

        self.assertEqual(review_result.overall_status, ComplianceStatus.COMPLIANT)
        self.assertFalse(review_result.requires_human_review)
        self.assertEqual(len(review_result.findings), 3)
        self.assertTrue(all(f.status == ComplianceStatus.COMPLIANT for f in review_result.findings))
        self.assertEqual(review_result.findings[0].reason_code, "LIMIT_MEETS_OR_EXCEEDS_MINIMUM")
        self.assertEqual(review_result.findings[1].reason_code, "POLICY_ACTIVE_AND_CURRENT")
        self.assertEqual(review_result.findings[2].reason_code, "NAMED_INSURED_EXACT_MATCH")

    def test_e2e_scenario_2_insufficient_coverage_limit(self):
        """Scenario 2: Extracted limit ($1M) is below configured required minimum ($2M) -> NON_COMPLIANT."""
        pdf_bytes = self._read_fixture("synthetic_standard_acord25.pdf")
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="E2E-DOC-02")

        canned_extraction = RawInsuranceExtraction(
            document_type="coi",
            coverages=[
                RawCoverageSection(
                    coverage_type="general_liability",
                    policy_number="GL-98765432",
                    each_occurrence="$1,000,000",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="EACH OCCURRENCE: $1,000,000",
                    ),
                )
            ],
        )

        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=canned_extraction)
        )
        pipeline_result = pipeline.process_inspection(inspection, subcontractor_id="SUB-02")

        requirement = InsuranceRequirement(
            requirement_id="REQ_GL_LIMIT_2M",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="GL Each Occurrence must be at least $2,000,000",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            limit_field=LimitField.EACH_OCCURRENCE,
            min_amount_cents=200_000_000,  # Higher than $1M on certificate
        )

        review_result = self.evaluator.evaluate(
            document_set=pipeline_result.document_set,
            requirements=[requirement],
            evaluation_date=self.evaluation_date,
        )

        self.assertEqual(review_result.overall_status, ComplianceStatus.NON_COMPLIANT)
        self.assertTrue(review_result.requires_human_review)
        self.assertEqual(review_result.findings[0].status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(review_result.findings[0].reason_code, "LIMIT_BELOW_MINIMUM")

    def test_e2e_scenario_3_expired_policy(self):
        """Scenario 3: Policy expired prior to evaluation date -> NON_COMPLIANT (POLICY_EXPIRED)."""
        pdf_bytes = self._read_fixture("synthetic_expired_and_inverted_dates.pdf")
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="E2E-DOC-03")

        canned_extraction = RawInsuranceExtraction(
            document_type="coi",
            coverages=[
                RawCoverageSection(
                    coverage_type="general_liability",
                    policy_number="GL-98765432",
                    each_occurrence="$1,000,000",
                    effective_date="01/01/2023",
                    expiration_date="01/01/2024",  # Expired as of 2025-06-15
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="COMMERCIAL GENERAL LIABILITY - POLICY NUMBER: GL-98765432",
                    ),
                )
            ],
        )

        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=canned_extraction)
        )
        pipeline_result = pipeline.process_inspection(inspection, subcontractor_id="SUB-03")

        requirement = InsuranceRequirement(
            requirement_id="REQ_GL_EXP",
            rule_type=RequirementType.POLICY_EXPIRATION,
            description="Policy must be unexpired",
            coverage_type=CoverageType.GENERAL_LIABILITY,
        )

        review_result = self.evaluator.evaluate(
            document_set=pipeline_result.document_set,
            requirements=[requirement],
            evaluation_date=self.evaluation_date,
        )

        self.assertEqual(review_result.overall_status, ComplianceStatus.NON_COMPLIANT)
        self.assertTrue(review_result.requires_human_review)
        self.assertEqual(review_result.findings[0].status, ComplianceStatus.NON_COMPLIANT)
        self.assertEqual(review_result.findings[0].reason_code, "POLICY_EXPIRED")

    def test_e2e_scenario_4_missing_required_endorsement(self):
        """Scenario 4: Required endorsement omitted on complete document set -> MISSING_EVIDENCE (ENDORSEMENT_NOT_FOUND)."""
        pdf_bytes = self._read_fixture("synthetic_standard_acord25.pdf")
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="E2E-DOC-04")

        canned_extraction = RawInsuranceExtraction(
            document_type="coi",
            endorsements=[],  # No Waiver of Subrogation endorsement
            is_document_set_complete=True,
        )

        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=canned_extraction)
        )
        pipeline_result = pipeline.process_inspection(inspection, subcontractor_id="SUB-04")

        requirement = InsuranceRequirement(
            requirement_id="REQ_WOS",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Waiver of Subrogation endorsement required",
            required_endorsement_type=EndorsementType.WAIVER_OF_SUBROGATION,
        )

        review_result = self.evaluator.evaluate(
            document_set=pipeline_result.document_set,
            requirements=[requirement],
            evaluation_date=self.evaluation_date,
        )

        self.assertEqual(review_result.overall_status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertTrue(review_result.requires_human_review)
        self.assertEqual(review_result.findings[0].status, ComplianceStatus.MISSING_EVIDENCE)
        self.assertEqual(review_result.findings[0].reason_code, "ENDORSEMENT_NOT_FOUND")

    def test_e2e_scenario_5_low_confidence_coverage_extraction(self):
        """Scenario 5: Model extracts limit with low confidence (0.40 < 0.70) -> UNABLE_TO_DETERMINE."""
        pdf_bytes = self._read_fixture("synthetic_standard_acord25.pdf")
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="E2E-DOC-05")

        canned_extraction = RawInsuranceExtraction(
            document_type="coi",
            coverages=[
                RawCoverageSection(
                    coverage_type="general_liability",
                    policy_number="GL-98765432",
                    each_occurrence="$1,000,000",
                    confidence=0.40,  # Low confidence
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="EACH OCCURRENCE: $1,000,000",
                    ),
                )
            ],
        )

        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=canned_extraction)
        )
        pipeline_result = pipeline.process_inspection(inspection, subcontractor_id="SUB-05")

        requirement = InsuranceRequirement(
            requirement_id="REQ_GL_LIMIT",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="GL Occurrence >= $1,000,000",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            limit_field=LimitField.EACH_OCCURRENCE,
            min_amount_cents=100_000_000,
        )

        review_result = self.evaluator.evaluate(
            document_set=pipeline_result.document_set,
            requirements=[requirement],
            evaluation_date=self.evaluation_date,
        )

        self.assertEqual(review_result.overall_status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertTrue(review_result.requires_human_review)
        self.assertEqual(review_result.findings[0].status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(review_result.findings[0].reason_code, "EXTRACTION_CONFIDENCE_LOW")

    def test_e2e_scenario_6_contradictory_policy_information(self):
        """Scenario 6: Document set with recorded contradiction -> overall review forces REQUIRES_HUMAN_REVIEW."""
        pdf_bytes = self._read_fixture("synthetic_standard_acord25.pdf")
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="E2E-DOC-06")

        canned_extraction = RawInsuranceExtraction(
            document_type="coi",
            named_insured=RawEntityExtraction(
                name="Apex Electrical Contractors LLC",
                evidence=RawEvidenceQuote(
                    page_number=1,
                    source_text="INSURED: Apex Electrical Contractors LLC",
                ),
            ),
            coverages=[
                RawCoverageSection(
                    coverage_type="general_liability",
                    policy_number="GL-98765432",
                    each_occurrence="$1,000,000",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="EACH OCCURRENCE: $1,000,000",
                    ),
                )
            ],
            raw_contradictions=["Policy number on COI is GL-98765432 but endorsement references GL-00000000"],
            is_document_set_complete=True,
        )

        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=canned_extraction)
        )
        pipeline_result = pipeline.process_inspection(inspection, subcontractor_id="SUB-06")

        requirement = InsuranceRequirement(
            requirement_id="REQ_GL_LIMIT",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="GL Occurrence >= $1,000,000",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            limit_field=LimitField.EACH_OCCURRENCE,
            min_amount_cents=100_000_000,
        )

        review_result = self.evaluator.evaluate(
            document_set=pipeline_result.document_set,
            requirements=[requirement],
            evaluation_date=self.evaluation_date,
        )

        # Finding passed individually
        self.assertEqual(review_result.findings[0].status, ComplianceStatus.COMPLIANT)
        # But contradiction cascade forces overall status to REQUIRES_HUMAN_REVIEW
        self.assertEqual(review_result.overall_status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertTrue(review_result.requires_human_review)

    def test_e2e_scenario_7_incomplete_document_set(self):
        """Scenario 7: Missing endorsement when is_document_set_complete=False -> UNABLE_TO_DETERMINE."""
        pdf_bytes = self._read_fixture("synthetic_standard_acord25.pdf")
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="E2E-DOC-07")

        canned_extraction = RawInsuranceExtraction(
            document_type="coi",
            endorsements=[],
            is_document_set_complete=False,  # Referenced endorsement pages missing
        )

        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=canned_extraction)
        )
        pipeline_result = pipeline.process_inspection(inspection, subcontractor_id="SUB-07")

        requirement = InsuranceRequirement(
            requirement_id="REQ_AI",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured endorsement required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )

        review_result = self.evaluator.evaluate(
            document_set=pipeline_result.document_set,
            requirements=[requirement],
            evaluation_date=self.evaluation_date,
        )

        self.assertEqual(review_result.overall_status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertTrue(review_result.requires_human_review)
        self.assertEqual(review_result.findings[0].status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(review_result.findings[0].reason_code, "INCOMPLETE_DOCUMENT_SET_ENDORSEMENT_UNKNOWN")

    # =========================================================================
    # Task 3: Endorsement Confidence Regression Tests
    # =========================================================================

    def test_endorsement_confidence_low_survives_normalization_and_routes_to_unable_to_determine(self):
        """Raw endorsement confidence of 0.40 survives pipeline normalization and prevents COMPLIANT status."""
        pdf_bytes = self._read_fixture("synthetic_multipage_coi_endorsement.pdf")
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="E2E-END-CONF-01")

        canned_extraction = RawInsuranceExtraction(
            document_type="coi",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    confidence=0.40,  # Low confidence
                    evidence=RawEvidenceQuote(
                        page_number=2,
                        source_text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13",
                    ),
                )
            ],
            is_document_set_complete=True,
        )

        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=canned_extraction)
        )
        pipeline_result = pipeline.process_inspection(inspection, subcontractor_id="SUB-END-CONF-01")
        doc_set = pipeline_result.document_set
        self.assertIsNotNone(doc_set)

        # 1. Verify confidence survived pipeline normalization into EndorsementExtraction
        self.assertEqual(len(doc_set.endorsements), 1)
        end = doc_set.endorsements[0]
        self.assertEqual(end.confidence, 0.40)
        self.assertEqual(end.extraction_status, ExtractionStatus.EXTRACTED)

        # 2. Verify ComplianceEvaluator does not return COMPLIANT for low confidence
        req = InsuranceRequirement(
            requirement_id="REQ_AI_CONF",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        review_result = self.evaluator.evaluate(doc_set, [req], self.evaluation_date)

        self.assertEqual(review_result.overall_status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertTrue(review_result.requires_human_review)
        self.assertEqual(len(review_result.findings), 1)
        self.assertEqual(review_result.findings[0].status, ComplianceStatus.UNABLE_TO_DETERMINE)
        self.assertEqual(review_result.findings[0].reason_code, "ENDORSEMENT_CONFIDENCE_LOW")

    def test_endorsement_confidence_high_survives_normalization_and_produces_compliant(self):
        """Raw endorsement confidence of 0.95 produces COMPLIANT with reason code ENDORSEMENT_VERIFIED."""
        pdf_bytes = self._read_fixture("synthetic_multipage_coi_endorsement.pdf")
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="E2E-END-CONF-02")

        canned_extraction = RawInsuranceExtraction(
            document_type="coi",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    confidence=0.95,  # High confidence
                    evidence=RawEvidenceQuote(
                        page_number=2,
                        source_text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13",
                    ),
                )
            ],
            is_document_set_complete=True,
        )

        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=canned_extraction)
        )
        pipeline_result = pipeline.process_inspection(inspection, subcontractor_id="SUB-END-CONF-02")
        doc_set = pipeline_result.document_set
        self.assertIsNotNone(doc_set)

        # 1. Verify confidence survived normalization
        self.assertEqual(len(doc_set.endorsements), 1)
        end = doc_set.endorsements[0]
        self.assertEqual(end.confidence, 0.95)

        # 2. Verify compliance result is COMPLIANT
        req = InsuranceRequirement(
            requirement_id="REQ_AI_CONF",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        review_result = self.evaluator.evaluate(doc_set, [req], self.evaluation_date)

        self.assertEqual(review_result.overall_status, ComplianceStatus.COMPLIANT)
        self.assertFalse(review_result.requires_human_review)
        self.assertEqual(review_result.findings[0].status, ComplianceStatus.COMPLIANT)
        self.assertEqual(review_result.findings[0].reason_code, "ENDORSEMENT_VERIFIED")

    def test_endorsement_confidence_omitted_preserves_optional_field_behavior(self):
        """When raw endorsement confidence is None, confidence defaults to None and passes if evidence is verified."""
        pdf_bytes = self._read_fixture("synthetic_multipage_coi_endorsement.pdf")
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="E2E-END-CONF-03")

        canned_extraction = RawInsuranceExtraction(
            document_type="coi",
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    confidence=None,  # Omitted confidence
                    evidence=RawEvidenceQuote(
                        page_number=2,
                        source_text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13",
                    ),
                )
            ],
            is_document_set_complete=True,
        )

        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=canned_extraction)
        )
        pipeline_result = pipeline.process_inspection(inspection, subcontractor_id="SUB-END-CONF-03")
        doc_set = pipeline_result.document_set
        self.assertIsNotNone(doc_set)

        # 1. Verify confidence is None
        self.assertIsNone(doc_set.endorsements[0].confidence)

        # 2. Verify compliance evaluation succeeds when evidence is verified and no confidence penalty applies
        req = InsuranceRequirement(
            requirement_id="REQ_AI_CONF",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured required",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
        )
        review_result = self.evaluator.evaluate(doc_set, [req], self.evaluation_date)

        self.assertEqual(review_result.overall_status, ComplianceStatus.COMPLIANT)
        self.assertFalse(review_result.requires_human_review)
        self.assertEqual(review_result.findings[0].reason_code, "ENDORSEMENT_VERIFIED")


if __name__ == "__main__":
    unittest.main()
