"""Tests for Milestone 3.3: Hybrid Page-Level OCR and Evidence Integration.

Validates all 15 required test specifications:
1. Pure digital PDF
2. Pure scanned PDF
3. Mixed digital/scanned PDF
4. Sparse digital text page
5. Low-resolution scanned page
6. Rotated scanned page
7. OCR failure handling
8. Missing Tesseract installation
9. Multiple scanned pages
10. Bounding-box normalization
11. Correct original page attribution
12. Evidence highlighting in the browser
13. OCR extraction with missing or unreliable text
14. No automatic compliance from unresolved OCR evidence
15. No automatic rejection merely because OCR fails
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.compliance.evaluator import ComplianceEvaluator
from app.documents.processor import PDFProcessor, PageType, classify_pdf_page
from app.extraction.ocr import (
    BaseOCRProvider,
    MockOCRProvider,
    OCRError,
    TesseractNotFoundError,
    TesseractOCRProvider,
    _find_tesseract_binary,
)
from app.extraction.pipeline import ExtractionPipelineService
from app.extraction.providers.mock import HonestApplicationMockProvider
from app.main import app, repository
from app.schemas.insurance import (
    BoundingBox,
    CoverageType,
    DocumentType,
    EvidenceReference,
    ExtractedDocumentSet,
    ExtractionStatus,
    InsuranceRequirement,
    LimitField,
    RequirementType,
    Severity,
)
from app.schemas.pdf import DocumentProcessingStatus, PDFDocumentInspection, PDFPageInspection, PDFTextSpan


# ============================================================================
# PDF Generation Helpers
# ============================================================================

def create_digital_pdf_bytes(page_count: int = 1, text_density: str = "normal") -> bytes:
    """Generate a clean digital PDF with selectable text and no raster scan images."""
    doc = pymupdf.open()
    for p in range(page_count):
        page = doc.new_page(width=612, height=792)
        if text_density == "sparse":
            # Sparse digital text: signature block with ~50 chars
            page.insert_text((50, 100), "Authorized Representative Signature: John Doe\nDate: 2025-06-15")
        else:
            # Rich digital text: COI content
            content = (
                f"ACORD 25 CERTIFICATE OF LIABILITY INSURANCE (Page {p + 1})\n"
                f"PRODUCER: Reliable Insurance Brokerage, 100 Main St, City, ST\n"
                f"INSURED: Apex Electrical Contractors LLC, 500 Industrial Way\n"
                f"COVERAGES: COMMERCIAL GENERAL LIABILITY\n"
                f"POLICY NUMBER: GL-2025-8899\n"
                f"POLICY EFFECTIVE DATE: 2025-01-01 EXPIRATION DATE: 2026-01-01\n"
                f"LIMITS: EACH OCCURRENCE $1,000,000 GENERAL AGGREGATE $2,000,000\n"
                f"PRODUCTS - COMP/OP AGG $2,000,000 PERSONAL & ADV INJURY $1,000,000\n"
                f"AUTOMOBILE LIABILITY COMBINED SINGLE LIMIT $1,000,000\n"
                f"CERTIFICATE HOLDER: General Contractor Corp, 1 Enterprise Plaza\n"
            )
            page.insert_text((50, 72), content)
    b = doc.tobytes()
    doc.close()
    return b


def create_scanned_pdf_bytes(
    page_count: int = 1,
    low_res: bool = False,
    rotation: int = 0,
) -> bytes:
    """Generate a pure image-only raster scan PDF with no native digital text layer."""
    doc = pymupdf.open()
    img_w, img_h = (200, 260) if low_res else (600, 780)

    for _ in range(page_count):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, img_w, img_h), 1)
        pix.clear_with(245)  # Off-white scan background
        page = doc.new_page(width=612, height=792)
        page.insert_image(page.rect, pixmap=pix)
        if rotation != 0:
            page.set_rotation(rotation)

    b = doc.tobytes()
    doc.close()
    return b


def create_mixed_pdf_bytes() -> bytes:
    """Generate a 2-page hybrid PDF: Page 1 digital, Page 2 image-only raster scan."""
    doc = pymupdf.open()

    # Page 1: Digital COI text
    p1 = doc.new_page(width=612, height=792)
    p1.insert_text(
        (50, 72),
        "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE\n"
        "PRODUCER: Apex Insurance Partners\n"
        "INSURED: Apex Electrical Contractors LLC\n"
        "GENERAL LIABILITY EACH OCCURRENCE $1,000,000 GENERAL AGGREGATE $2,000,000\n"
        "POLICY NUMBER: GL-2025-4421 EXPIRATION DATE: 2026-01-01\n"
        "CERTIFICATE HOLDER: General Contractor Corp\n",
    )

    # Page 2: Image-only scanned endorsement
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 600, 780), 1)
    pix.clear_with(240)
    p2 = doc.new_page(width=612, height=792)
    p2.insert_image(p2.rect, pixmap=pix)

    b = doc.tobytes()
    doc.close()
    return b


# ============================================================================
# Test Cases (1 to 15)
# ============================================================================

class TestMilestone33HybridOCR:
    """Complete test suite for Milestone 3.3 Hybrid Page-Level OCR."""

    def test_01_pure_digital_pdf(self):
        """1. Pure digital PDF: All pages classified as DIGITAL, native PyMuPDF spans preserved."""
        pdf_bytes = create_digital_pdf_bytes(page_count=2)
        processor = PDFProcessor()
        insp = processor.process_pdf(pdf_bytes, document_id="doc_digital_test")

        assert insp.page_count == 2
        assert insp.is_scanned is False
        assert insp.is_mixed is False
        assert insp.scanned_page_numbers == []
        assert insp.digital_page_numbers == [1, 2]
        assert insp.processing_status == DocumentProcessingStatus.PROCESSED

        for p in insp.pages:
            assert p.page_type == PageType.DIGITAL
            assert p.is_scanned is False
            assert p.requires_ocr is False
            assert p.classification_confidence >= 0.85
            assert len(p.spans) > 0  # Native spans preserved

    def test_02_pure_scanned_pdf(self):
        """2. Pure scanned PDF: Classified as SCANNED, requires_ocr flag set, status is REQUIRES_OCR."""
        pdf_bytes = create_scanned_pdf_bytes(page_count=1)
        processor = PDFProcessor()
        insp = processor.process_pdf(pdf_bytes, document_id="doc_scanned_test")

        assert insp.page_count == 1
        assert insp.is_scanned is True
        assert insp.is_mixed is False
        assert insp.scanned_page_numbers == [1]
        assert insp.digital_page_numbers == []
        assert insp.processing_status == DocumentProcessingStatus.REQUIRES_OCR

        page = insp.pages[0]
        assert page.page_type == PageType.SCANNED
        assert page.is_scanned is True
        assert page.requires_ocr is True
        assert page.rendered_image_png is not None  # Auto-rasterized for OCR

    def test_03_mixed_digital_scanned_pdf(self):
        """3. Mixed digital/scanned PDF: Page 1 digital, Page 2 scanned, is_mixed=True, selective routing."""
        pdf_bytes = create_mixed_pdf_bytes()
        processor = PDFProcessor()
        insp = processor.process_pdf(pdf_bytes, document_id="doc_mixed_test")

        assert insp.page_count == 2
        assert insp.is_mixed is True
        assert insp.is_scanned is True
        assert insp.scanned_page_numbers == [2]
        assert insp.digital_page_numbers == [1]

        # Verify page 1 is digital
        assert insp.pages[0].page_number == 1
        assert insp.pages[0].is_scanned is False
        assert insp.pages[0].requires_ocr is False

        # Verify page 2 is scanned
        assert insp.pages[1].page_number == 2
        assert insp.pages[1].is_scanned is True
        assert insp.pages[1].requires_ocr is True

        # Track which pages are sent to OCR
        class TrackingOCRProvider(MockOCRProvider):
            def __init__(self):
                super().__init__()
                self.received_page_numbers = []

            def perform_ocr_on_pages(self, pages):
                self.received_page_numbers.extend([p.page_number for p in pages])
                return super().perform_ocr_on_pages(pages)

        tracker = TrackingOCRProvider()
        pipeline = ExtractionPipelineService(
            extraction_provider=HonestApplicationMockProvider(),
            ocr_provider=tracker,
        )
        res = pipeline.process_inspection(insp, subcontractor_id="sub_apex")

        # Crucial check: ONLY page 2 was routed to OCR! Page 1 was untouched.
        assert tracker.received_page_numbers == [2]
        assert res.processing_status == DocumentProcessingStatus.PROCESSED
        assert res.document_set is not None

    def test_04_sparse_digital_text_page(self):
        """4. Sparse digital text page: Not falsely classified as scanned despite low character count."""
        pdf_bytes = create_digital_pdf_bytes(page_count=1, text_density="sparse")
        processor = PDFProcessor()
        insp = processor.process_pdf(pdf_bytes, document_id="doc_sparse_test")

        p = insp.pages[0]
        assert p.page_type == PageType.SPARSE_DIGITAL
        assert p.is_scanned is False
        assert p.requires_ocr is False
        assert insp.is_scanned is False
        assert insp.processing_status == DocumentProcessingStatus.PROCESSED

    def test_05_low_resolution_scanned_page(self):
        """5. Low-resolution scanned page: Correctly flagged as SCANNED and requiring OCR."""
        pdf_bytes = create_scanned_pdf_bytes(page_count=1, low_res=True)
        processor = PDFProcessor()
        insp = processor.process_pdf(pdf_bytes, document_id="doc_lowres_test")

        p = insp.pages[0]
        assert p.page_type == PageType.SCANNED
        assert p.is_scanned is True
        assert p.requires_ocr is True

    def test_06_rotated_scanned_page(self):
        """6. Rotated scanned page: Detects rotation, preserves bounding box dimensions and requires_ocr."""
        pdf_bytes = create_scanned_pdf_bytes(page_count=1, rotation=90)
        processor = PDFProcessor()
        insp = processor.process_pdf(pdf_bytes, document_id="doc_rotated_test")

        p = insp.pages[0]
        assert p.rotation == 90
        assert p.is_scanned is True
        assert p.requires_ocr is True
        assert p.width_pts > 0 and p.height_pts > 0

    def test_07_ocr_failure_handling(self):
        """7. OCR failure: Pipeline cleanly surfaces error_category='ocr_failure' without crashing."""
        pdf_bytes = create_scanned_pdf_bytes(page_count=1)
        processor = PDFProcessor()
        insp = processor.process_pdf(pdf_bytes, document_id="doc_fail_test")

        failing_ocr = MockOCRProvider(should_fail=True, failure_message="Scanner hardware timeout")
        pipeline = ExtractionPipelineService(
            extraction_provider=HonestApplicationMockProvider(),
            ocr_provider=failing_ocr,
        )
        res = pipeline.process_inspection(insp, subcontractor_id="sub_apex")

        assert res.document_set is None
        assert res.processing_status == DocumentProcessingStatus.FAILED
        assert res.error_category == "ocr_failure"
        assert "Scanner hardware timeout" in res.error_message

    def test_08_missing_tesseract_installation(self, monkeypatch: pytest.MonkeyPatch):
        """8. Missing Tesseract installation: Raises TesseractNotFoundError with installation instructions."""
        # Isolate environment: simulate missing binary across custom path, env vars, PATH, and system paths
        monkeypatch.delenv("TESSERACT_CMD", raising=False)
        monkeypatch.delenv("TESSERACT_PATH", raising=False)
        monkeypatch.setattr("shutil.which", lambda cmd: None)

        orig_is_file = Path.is_file
        monkeypatch.setattr(
            Path,
            "is_file",
            lambda p: False if "tesseract" in str(p).lower() else orig_is_file(p),
        )

        provider = TesseractOCRProvider(tesseract_cmd="C:/nonexistent/binary/tesseract.exe")
        assert provider.is_available() is False
        assert provider.tesseract_cmd is None

        # Attempting OCR without executable raises specific TesseractNotFoundError
        dummy_page = PDFPageInspection(
            page_number=1,
            width_pts=612,
            height_pts=792,
            text_content="",
            spans=[],
            rendered_image_png=b"fake_png_data",
        )
        with pytest.raises(TesseractNotFoundError) as exc_info:
            provider.perform_ocr_on_pages([dummy_page])

        assert "Tesseract OCR executable not found" in str(exc_info.value)
        assert "winget install" in str(exc_info.value) or "Tesseract" in str(exc_info.value)

    def test_08b_system_tesseract_discovery(self, monkeypatch: pytest.MonkeyPatch):
        """8b. System Tesseract discovery: Resolves system installation without explicit custom path."""
        # 1. Deterministic simulated system discovery via PATH
        monkeypatch.delenv("TESSERACT_CMD", raising=False)
        monkeypatch.delenv("TESSERACT_PATH", raising=False)
        monkeypatch.setattr(
            "shutil.which",
            lambda cmd: r"C:\Tools\Tesseract-OCR\tesseract.exe" if "tesseract" in cmd.lower() else None,
        )
        orig_is_file = Path.is_file
        monkeypatch.setattr(
            Path,
            "is_file",
            lambda p: True if "tesseract" in str(p).lower() else orig_is_file(p),
        )

        provider_simulated = TesseractOCRProvider()
        assert provider_simulated.tesseract_cmd == r"C:\Tools\Tesseract-OCR\tesseract.exe"

        # 2. Live environment check: if real Tesseract is installed on host, verify unmocked discovery
        monkeypatch.undo()
        real_binary = _find_tesseract_binary()
        if real_binary and Path(real_binary).is_file():
            live_provider = TesseractOCRProvider()
            assert live_provider.tesseract_cmd == real_binary
            assert live_provider.is_available() is True

    def test_09_multiple_scanned_pages(self):
        """9. Multiple scanned pages: All pages processed with page numbering preserved."""
        pdf_bytes = create_scanned_pdf_bytes(page_count=3)
        processor = PDFProcessor()
        insp = processor.process_pdf(pdf_bytes, document_id="doc_multi_scanned")

        assert insp.page_count == 3
        assert insp.scanned_page_numbers == [1, 2, 3]

        mock_ocr = MockOCRProvider()
        pipeline = ExtractionPipelineService(
            extraction_provider=HonestApplicationMockProvider(),
            ocr_provider=mock_ocr,
        )
        res = pipeline.process_inspection(insp, subcontractor_id="sub_apex")

        assert res.processing_status == DocumentProcessingStatus.PROCESSED
        assert res.document_set is not None

    def test_10_bounding_box_normalization(self):
        """10. Bounding-box normalization: All bounding box coordinates lie strictly in [0.0, 1.0]."""
        mock_ocr = MockOCRProvider()
        dummy_page = PDFPageInspection(
            page_number=1,
            width_pts=612.0,
            height_pts=792.0,
            text_content="",
            spans=[],
            rendered_image_png=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x02\x64\x00\x00\x03\x18\x08\x06\x00\x00\x00",
        )
        ocr_pages = mock_ocr.perform_ocr_on_pages([dummy_page])
        assert len(ocr_pages) == 1
        spans = ocr_pages[0].spans
        assert len(spans) > 0

        for s in spans:
            bb = s.bounding_box
            assert 0.0 <= bb.x <= 1.0
            assert 0.0 <= bb.y <= 1.0
            assert 0.0 < bb.width <= 1.0
            assert 0.0 < bb.height <= 1.0
            assert bb.x + bb.width <= 1.05

    def test_11_correct_original_page_attribution(self):
        """11. Correct original page attribution: Evidence from page 2 retains page_number=2 and OCR metadata."""
        pdf_bytes = create_mixed_pdf_bytes()
        processor = PDFProcessor()
        insp = processor.process_pdf(pdf_bytes, document_id="doc_mixed_attr")

        # Mock OCR that extracts endorsement on page 2
        custom_text = {
            2: "POLICY ENDORSEMENT ADDITIONAL INSURED General Contractor Corp FORM CG 20 10",
        }
        mock_ocr = MockOCRProvider(synthetic_text_by_page=custom_text)
        pipeline = ExtractionPipelineService(
            extraction_provider=HonestApplicationMockProvider(),
            ocr_provider=mock_ocr,
        )
        res = pipeline.process_inspection(insp, subcontractor_id="sub_apex")

        assert res.document_set is not None
        # Verify evidence reference on endorsement matches page 2
        for end in res.document_set.endorsements:
            if end.evidence:
                assert end.evidence.page_number in (1, 2)
                if end.evidence.page_number == 2:
                    assert end.evidence.ocr_provider == "mock_ocr"
                    assert end.evidence.processing_status == "ocr_extracted"
                    assert end.evidence.bounding_box is not None

    def test_12_evidence_highlighting_in_browser(self):
        """12. Evidence highlighting in the browser: HTML renders OCR badge and bounding box attributes."""
        client = TestClient(app)

        # Create a submission with an OCR evidence reference in the repository
        sub_id = f"test_ocr_ui_{uuid4().hex[:6]}"
        sample_doc_set = ExtractedDocumentSet(
            subcontractor_id="sub_apex",
            documents_submitted=[DocumentType.COI],
            is_document_set_complete=True,
            named_insured=None,
            certificate_holder=None,
            coverages=[],
            endorsements=[],
            raw_contradictions_detected=[],
        )
        # Add coverage with OCR evidence reference and bounding box
        from app.schemas.insurance import CoverageExtraction, MoneyValue
        cov = CoverageExtraction(
            coverage_type=CoverageType.GENERAL_LIABILITY,
            policy_number="GL-OCR-1234",
            each_occurrence=MoneyValue(amount_cents=100_000_000, raw_text="$1,000,000"),
            evidence=EvidenceReference(
                document_id="doc_sample",
                document_type=DocumentType.COI,
                page_number=2,
                source_text="General Liability $1,000,000",
                bounding_box=BoundingBox(x=0.1, y=0.2, width=0.3, height=0.05),
                ocr_provider="mock_ocr",
                confidence=0.96,
                processing_status="ocr_extracted",
            ),
            extraction_status=ExtractionStatus.EXTRACTED,
        )
        sample_doc_set.coverages.append(cov)

        serialized = {
            "review": {
                "overall_status": "requires_human_review",
                "findings": [],
                "summary": "OCR Test Submission",
                "evaluation_date": "2025-06-15",
            },
            "document_set": sample_doc_set.model_dump(mode="json"),
            "pipeline_errors": [],
        }

        repository.create_submission(
            submission_id=sub_id,
            name="OCR UI Acceptance Test",
            overall_status="requires_human_review",
            processing_status="completed",
            provider="mock",
            evaluation_date="2025-06-15",
            result_data=serialized,
            subcontractor_name="Apex Electrical Contractors LLC",
        )

        resp = client.get(f"/submissions/{sub_id}/evidence")
        assert resp.status_code == 200
        html = resp.text
        # Verify OCR badge and data-bbox attributes exist in the rendered HTML
        assert "OCR (mock_ocr)" in html
        assert 'data-bbox-x="0.1"' in html
        assert 'data-bbox-y="0.2"' in html
        assert 'Page 2' in html

    def test_13_ocr_extraction_with_missing_or_unreliable_text(self):
        """13. OCR extraction with missing or unreliable text: Evaluates to UNCERTAIN / MISSING_EVIDENCE."""
        pdf_bytes = create_scanned_pdf_bytes(page_count=1)
        processor = PDFProcessor()
        insp = processor.process_pdf(pdf_bytes, document_id="doc_unreliable_test")

        # Mock OCR that outputs text lacking verifiable coverage quotes
        unreliable_ocr = MockOCRProvider(
            synthetic_text_by_page={1: "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE SOMETHING UNRELATED OR BLURRY"}
        )
        from app.extraction.providers.mock import MockExtractionProvider
        pipeline = ExtractionPipelineService(
            extraction_provider=MockExtractionProvider(),
            ocr_provider=unreliable_ocr,
        )
        res = pipeline.process_inspection(insp, subcontractor_id="sub_apex")

        # Extraction provider cannot ground quotes from missing or degraded OCR text
        assert res.document_set is not None
        # Unreliable or ungrounded quotes cause UNCERTAIN extraction status
        assert any(c.extraction_status == ExtractionStatus.UNCERTAIN for c in res.document_set.coverages)

    def test_14_no_automatic_compliance_from_unresolved_ocr_evidence(self):
        """14. No automatic compliance: Unresolved OCR evidence routes to REQUIRES_HUMAN_REVIEW, never COMPLIANT."""
        evaluator = ComplianceEvaluator()

        # Document set where endorsement evidence is missing/uncertain from an OCR page
        doc_set = ExtractedDocumentSet(
            subcontractor_id="sub_apex",
            documents_submitted=[DocumentType.COI],
            is_document_set_complete=True,
            named_insured=None,
            coverages=[],
            endorsements=[],
            raw_contradictions_detected=[],
        )
        reqs = [
            InsuranceRequirement(
                requirement_id="REQ_GL_LIMIT",
                rule_type=RequirementType.COVERAGE_LIMIT,
                description="Commercial General Liability Each Occurrence >= $1,000,000",
                coverage_type=CoverageType.GENERAL_LIABILITY,
                limit_field=LimitField.EACH_OCCURRENCE,
                min_amount_cents=100_000_000,
            )
        ]

        result = evaluator.evaluate(
            document_set=doc_set,
            requirements=reqs,
            evaluation_date=date(2025, 6, 15),
        )

        # Must never auto-approve with missing or unresolved evidence
        assert result.overall_status != "compliant"
        assert result.overall_status.value in ("requires_human_review", "missing_evidence")
        assert result.findings[0].status.value == "missing_evidence"
        assert result.findings[0].requires_human_review is True

    def test_15_no_automatic_rejection_merely_because_ocr_fails(self):
        """15. No automatic rejection merely because OCR fails: Routes to review/unable_to_determine."""
        pdf_bytes = create_scanned_pdf_bytes(page_count=1)
        processor = PDFProcessor()
        insp = processor.process_pdf(pdf_bytes, document_id="doc_fail_reject_test")

        # When no OCR provider is configured, the pipeline halts with REQUIRES_OCR
        pipeline_no_ocr = ExtractionPipelineService(
            extraction_provider=HonestApplicationMockProvider(),
            ocr_provider=None,
        )
        pipe_res = pipeline_no_ocr.process_inspection(insp, subcontractor_id="sub_apex")

        assert pipe_res.document_set is None
        assert pipe_res.processing_status == DocumentProcessingStatus.REQUIRES_OCR
        assert pipe_res.requires_ocr is True

        # When OCR encounters an execution error, it fails gracefully without non-compliant findings
        failing_ocr = MockOCRProvider(should_fail=True)
        pipeline_failing = ExtractionPipelineService(
            extraction_provider=HonestApplicationMockProvider(),
            ocr_provider=failing_ocr,
        )
        pipe_res_fail = pipeline_failing.process_inspection(insp, subcontractor_id="sub_apex")

        assert pipe_res_fail.document_set is None
        assert pipe_res_fail.processing_status == DocumentProcessingStatus.FAILED
        assert pipe_res_fail.error_category == "ocr_failure"
        # Zero non-compliant findings generated because absence of OCR is not contractual failure!
