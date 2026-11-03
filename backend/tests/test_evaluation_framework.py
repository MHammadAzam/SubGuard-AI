"""Unit tests for the SubGuard AI Evaluation and Benchmarking Framework.

Verifies:
1. Ground-truth dataset schema validation, serialization, and deserialization.
2. Independent evidence-grounding verification:
   - Valid verbatim quotation on cited page.
   - Cross-page attribution detection (quote on page 2, citation claims page 1).
   - Fabricated / hallucinated quote detection (text does not exist in PDF).
3. Field-level metric computations:
   - Exact match vs normalized match (currency in cents, ISO dates, entity name suffixes).
   - True negative detection on absent fields (no false penalties).
   - False positive / hallucination detection.
   - Precision, recall, and F1 calculations.
4. End-to-end benchmark execution and environment sanitization.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.compliance.evaluator import ComplianceEvaluator
from app.documents.processor import PDFProcessor
from app.extraction.pipeline import ExtractionPipelineService
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
    MoneyValue,
    RequirementType,
)
from app.schemas.raw_extraction import RawEntityExtraction, RawEvidenceQuote, RawInsuranceExtraction
from app.schemas.pdf import PDFDocumentInspection, PDFPageInspection, PDFTextSpan
from evaluation.datasets.loader import (
    build_default_synthetic_dataset,
    load_dataset_from_json,
    save_dataset_to_json,
)
from evaluation.fixtures.synthetic_generator import generate_all_synthetic_fixtures, get_fixtures_dir
from evaluation.metrics.evaluator import MetricsEvaluator, _normalize_entity_name
from evaluation.runners.benchmark import BenchmarkMockProvider, BenchmarkRunner, sanitize_environment
from evaluation.schemas.ground_truth import (
    DocumentGroundTruth,
    DocumentSourceType,
    EvaluationDataset,
    ExpectedCoverage,
    ExpectedDate,
    ExpectedEndorsement,
    ExpectedEntity,
    ExpectedEvidence,
    ExpectedMoney,
)


class TestEvaluationFramework(unittest.TestCase):
    """Test suite for evaluation schemas, metrics engine, and benchmark runner."""

    @classmethod
    def setUpClass(cls):
        # Generate all 8 synthetic PDFs once for the test run
        generate_all_synthetic_fixtures()
        cls.fixtures_dir = get_fixtures_dir()
        cls.pdf_processor = PDFProcessor()
        cls.evaluator = MetricsEvaluator()

    def test_dataset_serialization_and_round_trip(self):
        """Dataset models should serialize to JSON and deserialize with zero fidelity loss."""
        dataset = build_default_synthetic_dataset()
        self.assertEqual(dataset.dataset_id, "subguard-synthetic-v1")
        self.assertEqual(len(dataset.documents), 8)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "test_dataset.json"
            save_dataset_to_json(dataset, tmp_path)
            self.assertTrue(tmp_path.exists())

            loaded = load_dataset_from_json(tmp_path)
            self.assertEqual(loaded.dataset_id, dataset.dataset_id)
            self.assertEqual(len(loaded.documents), 8)
            # Verify cents integrity
            doc1 = loaded.documents[0]
            gl = next(c for c in doc1.coverages if c.coverage_type == CoverageType.GENERAL_LIABILITY)
            self.assertEqual(gl.each_occurrence.amount_cents, 100_000_000)

    def test_evidence_grounding_valid_quote_on_correct_page(self):
        """Evidence citation with text actually present on the cited page must be verified."""
        multipage_pdf = self.fixtures_dir / "synthetic_multipage_coi_endorsement.pdf"
        with open(multipage_pdf, "rb") as f:
            pdf_bytes = f.read()

        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="DOC-002", filename=multipage_pdf.name)

        # Quote exists on Page 1: "INSURED: Apex Electrical Contractors LLC"
        ev = EvidenceReference(
            document_id="DOC-002",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="INSURED: Apex Electrical Contractors LLC",
        )
        expected_ev = ExpectedEvidence(page_number=1, source_text="INSURED: Apex Electrical Contractors LLC")

        res = self.evaluator._verify_evidence(
            field_name="named_insured_evidence",
            evidence=ev,
            expected_evidence=expected_ev,
            pdf_inspection=inspection,
        )

        self.assertTrue(res.page_correct)
        self.assertTrue(res.quote_found_on_page)
        self.assertTrue(res.quote_found_in_document)
        self.assertFalse(res.is_cross_page_attribution)
        self.assertFalse(res.is_hallucinated)

    def test_evidence_grounding_cross_page_attribution_detected(self):
        """Citation claiming page 1 when the text only exists on page 2 must be flagged as cross-page attribution."""
        multipage_pdf = self.fixtures_dir / "synthetic_multipage_coi_endorsement.pdf"
        with open(multipage_pdf, "rb") as f:
            pdf_bytes = f.read()

        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="DOC-002", filename=multipage_pdf.name)

        # "ADDITIONAL INSURED - OWNERS, LESSEES OR CONTRACTORS" is on Page 2, but citation claims Page 1
        ev_cross_page = EvidenceReference(
            document_id="DOC-002",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,  # WRONG PAGE! (Text is on page 2)
            source_text="ADDITIONAL INSURED - OWNERS, LESSEES OR CONTRACTORS",
        )
        expected_ev = ExpectedEvidence(page_number=2, source_text="ADDITIONAL INSURED - OWNERS, LESSEES OR CONTRACTORS")

        res = self.evaluator._verify_evidence(
            field_name="endorsement_evidence",
            evidence=ev_cross_page,
            expected_evidence=expected_ev,
            pdf_inspection=inspection,
        )

        self.assertFalse(res.page_correct)
        self.assertFalse(res.quote_found_on_page)
        self.assertTrue(res.quote_found_in_document)
        self.assertTrue(res.is_cross_page_attribution)
        self.assertFalse(res.is_hallucinated)

    def test_evidence_grounding_fabricated_quote_detected_as_hallucination(self):
        """Citation with a fabricated quote that appears nowhere in the PDF must be flagged as hallucinated."""
        multipage_pdf = self.fixtures_dir / "synthetic_multipage_coi_endorsement.pdf"
        with open(multipage_pdf, "rb") as f:
            pdf_bytes = f.read()

        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="DOC-002", filename=multipage_pdf.name)

        # Completely fabricated text
        fabricated_ev = EvidenceReference(
            document_id="DOC-002",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Coverage includes nuclear hazard and catastrophic orbital debris clause",
        )
        expected_ev = ExpectedEvidence(page_number=1, source_text="INSURED: Apex Electrical Contractors LLC")

        res = self.evaluator._verify_evidence(
            field_name="coverage_evidence",
            evidence=fabricated_ev,
            expected_evidence=expected_ev,
            pdf_inspection=inspection,
        )

        self.assertTrue(res.page_correct)  # claimed 1, expected 1
        self.assertFalse(res.quote_found_on_page)
        self.assertFalse(res.quote_found_in_document)
        self.assertTrue(res.is_hallucinated)
        self.assertFalse(res.is_cross_page_attribution)

    def test_entity_name_normalization_and_suffix_matching(self):
        """Entity normalization should equate standard abbreviations (Inc, Incorporated, LLC, Co)."""
        self.assertEqual(
            _normalize_entity_name("Apex Electrical Inc"),
            _normalize_entity_name("Apex Electrical Incorporated"),
        )
        self.assertEqual(
            _normalize_entity_name("Travelers Indemnity Company"),
            _normalize_entity_name("Travelers Indemnity Co"),
        )
        # Distinct companies should not equate
        self.assertNotEqual(
            _normalize_entity_name("Apex Electrical LLC"),
            _normalize_entity_name("Zenith Plumbing Inc"),
        )

    def test_true_negative_detection_on_absent_sections(self):
        """When ground truth asserts a section is absent, extracted absence must count as true negative, not penalty."""
        gt = DocumentGroundTruth(
            document_id="TEST-TN-01",
            filename="synthetic_missing_sections.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            document_type=DocumentType.COI,
            named_insured=ExpectedEntity(name=None, is_absent=True),
            absent_coverages=[CoverageType.WORKERS_COMP],
            absent_endorsements=[EndorsementType.WAIVER_OF_SUBROGATION],
        )

        # Extractor correctly found no named insured, no workers comp, and no waiver endorsement
        extracted = ExtractedDocumentSet(
            subcontractor_id="TEST-TN-01",
            named_insured=EntityInfo(name=None, extraction_status=ExtractionStatus.NOT_FOUND),
            coverages=[],
            endorsements=[],
        )

        res = self.evaluator.evaluate_document(
            ground_truth=gt,
            extracted_doc_set=extracted,
            pdf_inspection=None,
            processing_status="completed",
        )

        # All absent assertions should be normalized_match=True, error_type="none"
        named_ins_res = next(f for f in res.field_results if f.field_name == "named_insured")
        self.assertTrue(named_ins_res.normalized_match)
        self.assertEqual(named_ins_res.error_type, "none")

        wc_res = next(f for f in res.field_results if f.field_name == "coverage_presence_workers_comp")
        self.assertTrue(wc_res.normalized_match)
        self.assertEqual(wc_res.error_type, "none")

        wos_res = next(f for f in res.field_results if f.field_name == "endorsement_presence_waiver_of_subrogation")
        self.assertTrue(wos_res.normalized_match)
        self.assertEqual(wos_res.error_type, "none")

    def test_false_positive_hallucination_penalty(self):
        """When ground truth asserts a section is absent, an extracted value must be flagged as a false positive."""
        gt = DocumentGroundTruth(
            document_id="TEST-FP-01",
            filename="synthetic_missing_sections.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            absent_coverages=[CoverageType.WORKERS_COMP],
        )

        # Extractor hallucinated workers comp
        extracted = ExtractedDocumentSet(
            subcontractor_id="TEST-FP-01",
            coverages=[
                CoverageExtraction(
                    coverage_type=CoverageType.WORKERS_COMP,
                    policy_number="WC-GHOST-123",
                )
            ],
        )

        res = self.evaluator.evaluate_document(
            ground_truth=gt,
            extracted_doc_set=extracted,
            pdf_inspection=None,
            processing_status="completed",
        )

        wc_res = next(f for f in res.field_results if f.field_name == "coverage_presence_workers_comp")
        self.assertFalse(wc_res.normalized_match)
        self.assertEqual(wc_res.error_type, "false_positive")

    def test_aggregate_metrics_precision_recall_f1(self):
        """Aggregate metric calculations should match standard statistical definitions."""
        gt = DocumentGroundTruth(
            document_id="TEST-AGG-01",
            filename="synthetic_standard_acord25.pdf",
            coverages=[
                ExpectedCoverage(
                    coverage_type=CoverageType.GENERAL_LIABILITY,
                    each_occurrence=ExpectedMoney(amount_cents=100_000_000),
                )
            ],
        )

        # Perfect match
        extracted = ExtractedDocumentSet(
            subcontractor_id="TEST-AGG-01",
            coverages=[
                CoverageExtraction(
                    coverage_type=CoverageType.GENERAL_LIABILITY,
                    each_occurrence=MoneyValue(amount_cents=100_000_000, raw_text="$1,000,000"),
                )
            ],
        )

        res = self.evaluator.evaluate_document(
            ground_truth=gt,
            extracted_doc_set=extracted,
            pdf_inspection=None,
            processing_status="completed",
        )

        field_stats, _ = self.evaluator.calculate_aggregate_metrics([res])
        gl_stats = field_stats.get("coverage_limits")
        self.assertIsNotNone(gl_stats)
        self.assertEqual(gl_stats.precision, 1.0)
        self.assertEqual(gl_stats.recall, 1.0)
        self.assertEqual(gl_stats.f1_score, 1.0)
        self.assertEqual(gl_stats.normalized_accuracy, 1.0)

    def test_benchmark_runner_mock_provider_end_to_end(self):
        """BenchmarkRunner should run across all 8 synthetic documents and generate reports."""
        dataset = build_default_synthetic_dataset()
        runner = BenchmarkRunner(provider_name="mock")
        report = runner.run_benchmark(dataset)

        self.assertEqual(report.total_documents, 8)
        self.assertEqual(report.processing_success_count, 8)
        self.assertEqual(report.strict_passed_documents_count, 8)
        self.assertEqual(report.thresholded_passed_documents_count, 8)
        self.assertEqual(report.provider_name, "mock")
        self.assertEqual(report.benchmark_type, "mock_framework_validation")
        self.assertEqual(report.dataset_source_type, "synthetic")
        self.assertEqual(report.overall_normalized_accuracy, 1.0)
        self.assertEqual(report.evidence_metrics.unsupported_quote_count, 0)
        self.assertEqual(report.evidence_metrics.hallucinated_quote_count, 0)
        self.assertEqual(report.evidence_metrics.value_supported_rate, 1.0)
        self.assertEqual(report.evidence_metrics.category_aligned_rate, 1.0)

        # Verify environment sanitization
        env = report.sanitized_environment
        for k, v in env.items():
            if any(s in k.lower() for s in ["key", "token", "secret", "password"]):
                self.assertEqual(v, "[REDACTED]")

    def test_environment_sanitization_removes_secrets(self):
        """Sanitization helper must redact all tokens, keys, and passwords from os.environ."""
        env = sanitize_environment()
        self.assertIn("PYTHON_VERSION", env)
        for k, v in env.items():
            if "TOKEN" in k or "KEY" in k or "SECRET" in k:
                self.assertEqual(v, "[REDACTED]")

    # =========================================================================
    # Phase 6.1 Negative-Control Benchmark Tests
    # =========================================================================

    def test_negative_control_wrong_named_insured(self):
        """Framework must detect and penalize a deliberately wrong named insured string."""
        gt = DocumentGroundTruth(
            document_id="NEG-01",
            filename="synthetic_standard_acord25.pdf",
            named_insured=ExpectedEntity(name="Apex Electrical Contractors LLC"),
        )
        extracted = ExtractedDocumentSet(
            subcontractor_id="NEG-01",
            named_insured=EntityInfo(name="Bogus Construction LLC"),
        )
        res = self.evaluator.evaluate_document(gt, extracted, None, "completed")
        f_res = next(f for f in res.field_results if f.field_name == "named_insured")
        self.assertFalse(f_res.exact_match)
        self.assertFalse(f_res.normalized_match)
        self.assertEqual(f_res.error_type, "value_mismatch")

    def test_negative_control_incorrect_policy_number(self):
        """Framework must detect and penalize an incorrect policy number."""
        gt = DocumentGroundTruth(
            document_id="NEG-02",
            filename="synthetic_standard_acord25.pdf",
            coverages=[
                ExpectedCoverage(
                    coverage_type=CoverageType.GENERAL_LIABILITY,
                    policy_number="GL-98765432",
                )
            ],
        )
        extracted = ExtractedDocumentSet(
            subcontractor_id="NEG-02",
            coverages=[
                CoverageExtraction(
                    coverage_type=CoverageType.GENERAL_LIABILITY,
                    policy_number="GL-WRONG-0000",
                )
            ],
        )
        res = self.evaluator.evaluate_document(gt, extracted, None, "completed")
        f_res = next(f for f in res.field_results if f.field_name == "general_liability.policy_number")
        self.assertFalse(f_res.exact_match)
        self.assertFalse(f_res.normalized_match)
        self.assertEqual(f_res.error_type, "value_mismatch")

    def test_negative_control_incorrect_expiration_date(self):
        """Framework must detect and penalize a hallucinated or incorrect expiration date."""
        gt = DocumentGroundTruth(
            document_id="NEG-03",
            filename="synthetic_standard_acord25.pdf",
            coverages=[
                ExpectedCoverage(
                    coverage_type=CoverageType.GENERAL_LIABILITY,
                    expiration_date=ExpectedDate(date=date(2026, 1, 1), raw_text="01/01/2026"),
                )
            ],
        )
        extracted = ExtractedDocumentSet(
            subcontractor_id="NEG-03",
            coverages=[
                CoverageExtraction(
                    coverage_type=CoverageType.GENERAL_LIABILITY,
                    expiration_date=DateValue(date=date(2099, 12, 31), raw_text="12/31/2099"),
                )
            ],
        )
        res = self.evaluator.evaluate_document(gt, extracted, None, "completed")
        f_res = next(f for f in res.field_results if f.field_name == "general_liability.expiration_date")
        self.assertFalse(f_res.exact_match)
        self.assertFalse(f_res.normalized_match)
        self.assertEqual(f_res.error_type, "value_mismatch")

    def test_negative_control_incorrect_coverage_limit(self):
        """Framework must detect and penalize an extracted limit that does not match ground truth."""
        gt = DocumentGroundTruth(
            document_id="NEG-04",
            filename="synthetic_standard_acord25.pdf",
            coverages=[
                ExpectedCoverage(
                    coverage_type=CoverageType.GENERAL_LIABILITY,
                    each_occurrence=ExpectedMoney(amount_cents=100_000_000, raw_text="$1,000,000"),
                )
            ],
        )
        extracted = ExtractedDocumentSet(
            subcontractor_id="NEG-04",
            coverages=[
                CoverageExtraction(
                    coverage_type=CoverageType.GENERAL_LIABILITY,
                    each_occurrence=MoneyValue(amount_cents=50_000_000, raw_text="$500,000"),
                )
            ],
        )
        res = self.evaluator.evaluate_document(gt, extracted, None, "completed")
        f_res = next(f for f in res.field_results if f.field_name == "general_liability.each_occurrence")
        self.assertFalse(f_res.exact_match)
        self.assertFalse(f_res.normalized_match)
        self.assertEqual(f_res.error_type, "value_mismatch")

    def test_negative_control_missing_general_liability_coverage(self):
        """Framework must detect when expected General Liability is omitted (false negative)."""
        gt = DocumentGroundTruth(
            document_id="NEG-05",
            filename="synthetic_standard_acord25.pdf",
            coverages=[
                ExpectedCoverage(
                    coverage_type=CoverageType.GENERAL_LIABILITY,
                    each_occurrence=ExpectedMoney(amount_cents=100_000_000),
                )
            ],
        )
        extracted = ExtractedDocumentSet(
            subcontractor_id="NEG-05",
            coverages=[],  # GL completely omitted
        )
        res = self.evaluator.evaluate_document(gt, extracted, None, "completed")
        f_res = next(f for f in res.field_results if f.field_name == "coverage_presence_general_liability")
        self.assertFalse(f_res.exact_match)
        self.assertFalse(f_res.normalized_match)
        self.assertEqual(f_res.error_type, "false_negative")

    def test_negative_control_fabricated_endorsement(self):
        """Framework must penalize hallucination of an endorsement when explicitly absent."""
        gt = DocumentGroundTruth(
            document_id="NEG-06",
            filename="synthetic_standard_acord25.pdf",
            absent_endorsements=[EndorsementType.WAIVER_OF_SUBROGATION],
        )
        extracted = ExtractedDocumentSet(
            subcontractor_id="NEG-06",
            endorsements=[
                EndorsementExtraction(
                    endorsement_type=EndorsementType.WAIVER_OF_SUBROGATION,
                    form_number="CG 24 04 05 09",
                )
            ],
        )
        res = self.evaluator.evaluate_document(gt, extracted, None, "completed")
        f_res = next(f for f in res.field_results if f.field_name == "endorsement_presence_waiver_of_subrogation")
        self.assertFalse(f_res.exact_match)
        self.assertFalse(f_res.normalized_match)
        self.assertEqual(f_res.error_type, "false_positive")

    def test_negative_control_fabricated_evidence_quote(self):
        """Evidence validator must detect and flag completely fabricated quotes as hallucinated."""
        pdf_path = self.fixtures_dir / "synthetic_standard_acord25.pdf"
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="NEG-07", filename=pdf_path.name)

        fake_ev = EvidenceReference(
            document_id="NEG-07",
            page_number=1,
            source_text="Coverage includes alien abduction and interdimensional portal damage",
        )
        res = self.evaluator._verify_evidence("general_liability_evidence", fake_ev, None, inspection)
        self.assertFalse(res.quote_found_on_page)
        self.assertFalse(res.quote_found_in_document)
        self.assertTrue(res.is_hallucinated)

    def test_negative_control_correct_quote_wrong_page_attribution(self):
        """Evidence validator must catch cross-page attribution when text exists on page 2 but citation claims page 1."""
        pdf_path = self.fixtures_dir / "synthetic_multipage_coi_endorsement.pdf"
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="NEG-08", filename=pdf_path.name)

        # "THIS ENDORSEMENT CHANGES THE POLICY" is on page 2 of this fixture
        ev_wrong_page = EvidenceReference(
            document_id="NEG-08",
            page_number=1,  # Claiming page 1
            source_text="THIS ENDORSEMENT CHANGES THE POLICY. PLEASE READ IT CAREFULLY.",
        )
        expected_ev = ExpectedEvidence(page_number=2, source_text="THIS ENDORSEMENT CHANGES THE POLICY.")

        res = self.evaluator._verify_evidence("endorsement_evidence", ev_wrong_page, expected_ev, inspection)
        self.assertFalse(res.page_correct)
        self.assertFalse(res.quote_found_on_page)
        self.assertTrue(res.quote_found_in_document)
        self.assertTrue(res.is_cross_page_attribution)
        self.assertFalse(res.is_hallucinated)

    def test_negative_control_semantic_category_mismatch(self):
        """Quotation containing '$1,000,000' for Each Occurrence must fail category alignment if cited for General Aggregate."""
        pdf_path = self.fixtures_dir / "synthetic_standard_acord25.pdf"
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="NEG-09", filename=pdf_path.name)

        # Quotation is explicitly Each Occurrence, but cited for general_aggregate
        ev_occ_cited_for_agg = EvidenceReference(
            document_id="NEG-09",
            page_number=1,
            source_text="EACH OCCURRENCE: $1,000,000",
        )
        res = self.evaluator._verify_evidence(
            field_name="general_liability.general_aggregate",
            evidence=ev_occ_cited_for_agg,
            expected_evidence=None,
            pdf_inspection=inspection,
            extracted_value=100_000_000,
            raw_text="$1,000,000",
        )
        # Quote is found on page 1, but fails category alignment because it identifies each occurrence!
        self.assertTrue(res.quote_found_on_page)
        self.assertTrue(res.value_supported)
        self.assertFalse(res.category_aligned)
        self.assertIn("general_aggregate", res.notes or "")

    def test_missing_credentials_reported_as_configuration_error_not_false_negative(self):
        """Missing credentials must yield configuration_error and zero false negatives."""
        dataset = build_default_synthetic_dataset()
        doc_gt = dataset.documents[0]

        res = self.evaluator.evaluate_document(
            ground_truth=doc_gt,
            extracted_doc_set=None,
            pdf_inspection=None,
            processing_status="configuration_error",
        )
        self.assertEqual(res.processing_status, "configuration_error")
        self.assertEqual(len(res.field_results), 1)
        f_res = res.field_results[0]
        self.assertEqual(f_res.category, "execution")
        self.assertEqual(f_res.error_type, "configuration_error")
        self.assertFalse(f_res.ground_truth_present)

        # Aggregate metrics must exclude configuration errors from false negative counts
        field_metrics, _ = self.evaluator.calculate_aggregate_metrics([res])
        all_stats = field_metrics.get("all")
        self.assertIsNotNone(all_stats)
        self.assertEqual(all_stats.false_negatives, 0)
        self.assertEqual(all_stats.total_evaluated, 0)

    def test_provider_failure_reported_as_provider_error_not_false_negative(self):
        """Provider timeout or rate-limit must yield provider_error and zero false negatives."""
        dataset = build_default_synthetic_dataset()
        doc_gt = dataset.documents[0]

        res = self.evaluator.evaluate_document(
            ground_truth=doc_gt,
            extracted_doc_set=None,
            pdf_inspection=None,
            processing_status="provider_error",
        )
        self.assertEqual(res.processing_status, "provider_error")
        self.assertEqual(len(res.field_results), 1)
        self.assertEqual(res.field_results[0].error_type, "provider_error")

        field_metrics, _ = self.evaluator.calculate_aggregate_metrics([res])
        self.assertEqual(field_metrics["all"].false_negatives, 0)

    def test_genuine_extraction_miss_reported_as_true_false_negative(self):
        """When extraction successfully runs but misses a ground-truth field, it must count as a false negative."""
        dataset = build_default_synthetic_dataset()
        # Find SYNTH-004 (has umbrella_excess coverage)
        doc_gt = next(d for d in dataset.documents if d.document_id == "SYNTH-004")

        # Simulate extraction running successfully but missing umbrella_excess
        extracted_doc_set = ExtractedDocumentSet(
            subcontractor_id="SYNTH-004",
            named_insured=EntityInfo(
                name="Apex Electrical Contractors LLC",
                evidence=EvidenceReference(
                    document_id="SYNTH-004",
                    page_number=1,
                    source_text="INSURED: Apex Electrical Contractors LLC",
                ),
            ),
            coverages=[],  # Model completely missed the umbrella coverage!
        )

        res = self.evaluator.evaluate_document(
            ground_truth=doc_gt,
            extracted_doc_set=extracted_doc_set,
            pdf_inspection=None,
            processing_status="processed",
        )
        # Verify that umbrella_excess presence is marked as a true false negative
        fn_fields = [f for f in res.field_results if f.error_type == "false_negative"]
        self.assertTrue(any("umbrella_excess" in f.field_name for f in fn_fields))

        # Verify aggregate metrics count this genuine false negative
        field_metrics, _ = self.evaluator.calculate_aggregate_metrics([res])
        self.assertGreaterEqual(field_metrics["all"].false_negatives, 1)

    def test_quote_present_unverified_support_routes_to_human_review(self):
        """When quote text is on page but lacks semantic support, status is quote_present_unverified_support and routes to human review."""
        canned_raw = RawInsuranceExtraction(
            document_type="coi",
            named_insured=RawEntityExtraction(
                name="Apex Electrical Contractors LLC",
                # Quote text is generic header on page 1, NOT the entity name
                evidence=RawEvidenceQuote(page_number=1, source_text="CERTIFICATE OF LIABILITY INSURANCE"),
            ),
        )
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(canned_response=canned_raw)
        )
        pdf_path = self.fixtures_dir / "synthetic_standard_acord25.pdf"
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        inspection = self.pdf_processor.process_pdf(pdf_bytes, document_id="TEST-UNVERIFIED-SUPP")

        pipeline_res = pipeline.process_inspection(inspection, subcontractor_id="SUB-001")
        doc_set = pipeline_res.document_set
        self.assertIsNotNone(doc_set)

        # Status must be QUOTE_PRESENT_UNVERIFIED_SUPPORT (quote is on page, but does not contain entity name)
        self.assertEqual(
            doc_set.named_insured.extraction_status,
            ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT,
        )
        # Source quote and page number are faithfully preserved
        self.assertEqual(doc_set.named_insured.evidence.page_number, 1)
        self.assertEqual(doc_set.named_insured.evidence.source_text, "CERTIFICATE OF LIABILITY INSURANCE")

        # In ComplianceEvaluator: Must route to REQUIRES_HUMAN_REVIEW
        evaluator = ComplianceEvaluator()
        req = InsuranceRequirement(
            requirement_id="REQ-NI-01",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named insured match",
            required_entity_name="Apex Electrical Contractors LLC",
        )
        result = evaluator.evaluate(
            document_set=doc_set,
            requirements=[req],
            evaluation_date=date(2025, 6, 15),
        )
        self.assertEqual(result.overall_status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        finding = result.findings[0]
        self.assertEqual(finding.status, ComplianceStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(finding.reason_code, "NAMED_INSURED_EVIDENCE_SUPPORT_UNVERIFIED")
        self.assertTrue(finding.requires_human_review)


if __name__ == "__main__":
    unittest.main()
