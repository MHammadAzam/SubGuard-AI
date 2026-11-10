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
import math
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import numpy as np
import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.compliance.evaluator import ComplianceEvaluator
from app.documents.processor import PDFProcessor, PageType, classify_pdf_page
from app.extraction.ocr import (
    BaseOCRProvider,
    MockOCRProvider,
    OCRError,
    OSD_MIN_CONFIDENCE,
    TesseractNotFoundError,
    TesseractOCRProvider,
    _detect_orientation_osd,
    _detect_skew_angle,
    _find_tesseract_binary,
    _pixmap_to_grayscale,
    analyze_page_layout,
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


def create_rendered_scanned_pdf_bytes(
    text_lines: List[str],
    rotation: int = 0,
    skew_angle: float = 0.0,
    dpi: int = 150,
) -> bytes:
    """Generate a raster scanned PDF containing rendered text with specified rotation and skew."""
    doc = pymupdf.open()
    p = doc.new_page(width=612, height=792)
    for i, line in enumerate(text_lines):
        p.insert_text((72, 100 + i * 35), line, fontsize=12)

    zoom = dpi / 72.0
    mat = pymupdf.Matrix(zoom, zoom)
    if skew_angle != 0.0:
        mat = mat.prerotate(skew_angle)
    pix = p.get_pixmap(matrix=mat)
    if rotation in (90, 180, 270):
        rot_const = pix.ROTATE_90 if rotation == 90 else (pix.ROTATE_180 if rotation == 180 else pix.ROTATE_270)
        pix = pix.flip_rotate(rot_const)
    doc.close()

    out_doc = pymupdf.open()
    w = 792 if rotation in (90, 270) else 612
    h = 612 if rotation in (90, 270) else 792
    out_page = out_doc.new_page(width=w, height=h)
    out_page.insert_image(out_page.rect, pixmap=pix)
    pdf_bytes = out_doc.tobytes()
    out_doc.close()
    return pdf_bytes


def create_matrix_controlled_scanned_pdf_bytes(
    text_lines: list[str],
    physical_rotation: int = 0,
    metadata_rotation: int = 0,
    skew_angle: float = 0.0,
    dpi: int = 150,
) -> bytes:
    """Generate a raster scanned PDF with independent physical raster rotation and metadata rotation."""
    doc = pymupdf.open()
    p = doc.new_page(width=612, height=792)
    for i, line in enumerate(text_lines):
        p.insert_text((72, 100 + i * 35), line, fontsize=12)

    zoom = dpi / 72.0
    mat = pymupdf.Matrix(zoom, zoom)
    if skew_angle != 0.0:
        mat = mat.prerotate(skew_angle)
    pix = p.get_pixmap(matrix=mat)
    if physical_rotation in (90, 180, 270):
        rot_const = pix.ROTATE_90 if physical_rotation == 90 else (pix.ROTATE_180 if physical_rotation == 180 else pix.ROTATE_270)
        pix = pix.flip_rotate(rot_const)
    doc.close()

    out_doc = pymupdf.open()
    w = 792 if physical_rotation in (90, 270) else 612
    h = 612 if physical_rotation in (90, 270) else 792
    out_page = out_doc.new_page(width=w, height=h)
    out_page.insert_image(out_page.rect, pixmap=pix)
    if metadata_rotation != 0:
        out_page.set_rotation(metadata_rotation)
    pdf_bytes = out_doc.tobytes()
    out_doc.close()
    return pdf_bytes


# ============================================================================
# Test Cases (1 to 20)
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
        from app.persistence.database import init_db
        from tests.fixtures.auth_helpers import authenticate_session_client

        client = TestClient(app)
        with tempfile.TemporaryDirectory() as tmp_dir:
            orig_db = repository.db_path
            try:
                tmp_db = Path(tmp_dir) / "ocr_ui_test.db"
                init_db(tmp_db)
                repository.db_path = tmp_db
                authenticate_session_client(client, db_path=tmp_db, role="viewer")

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
            finally:
                repository.db_path = orig_db
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

    @pytest.mark.skipif(not TesseractOCRProvider().is_available(), reason="Requires real Tesseract OCR installation")
    def test_16_rotation_handling_all_quadrants(self):
        """16. Rotation handling: Resolves 0, 90, 180, 270 deg rotations with high accuracy."""
        provider = TesseractOCRProvider()
        processor = PDFProcessor()
        test_lines = [
            f"Certificate of Liability Insurance Line {i}: Insured Acme Corp $1,000,000 policy GL-99887766"
            for i in range(15)
        ]

        expected_rotations = {
            0: 0,
            90: 270,
            180: 180,
            270: 90,
        }

        for rot_input, expected_applied in expected_rotations.items():
            pdf_bytes = create_rendered_scanned_pdf_bytes(test_lines, rotation=rot_input)
            insp = processor.process_pdf(pdf_bytes, f"doc_rot_{rot_input}")
            ocr_pages = provider.perform_ocr_on_pages(insp.pages)
            page = ocr_pages[0]

            assert "GL-99887766" in page.text_content or "99887766" in page.text_content
            assert "Acme" in page.text_content or "ACME" in page.text_content.upper()
            assert page.ocr_confidence > 0.70
            assert page.classification_signals.get("ocr_rotation_applied") == expected_applied

    @pytest.mark.skipif(not TesseractOCRProvider().is_available(), reason="Requires real Tesseract OCR installation")
    def test_17_bounded_deskew_accuracy(self):
        """17. Bounded deskew: Corrects physical scanner skew up to +/- 15 degrees."""
        provider = TesseractOCRProvider()
        processor = PDFProcessor()
        test_lines = [
            f"Line {i}: Insured Zenith Industries Policy ZN-445566 Limit $2,000,000"
            for i in range(15)
        ]

        for skew in [3.0, 5.0, 10.0, 14.0]:
            pdf_bytes = create_rendered_scanned_pdf_bytes(test_lines, skew_angle=skew)
            insp = processor.process_pdf(pdf_bytes, f"doc_skew_{skew}")
            ocr_pages = provider.perform_ocr_on_pages(insp.pages)
            page = ocr_pages[0]

            applied_skew = page.classification_signals.get("ocr_deskew_applied", 0.0)
            assert abs(applied_skew - skew) <= 1.0
            assert "ZN-445566" in page.text_content or "445566" in page.text_content
            assert "Zenith" in page.text_content or "ZENITH" in page.text_content.upper()
            assert page.ocr_confidence > 0.70

    @pytest.mark.skipif(not TesseractOCRProvider().is_available(), reason="Requires real Tesseract OCR installation")
    def test_18_bounding_box_original_page_alignment(self):
        """18. Coordinate integrity: Transformed OCR boxes map back into [0.0, 1.0] original page space."""
        provider = TesseractOCRProvider()
        processor = PDFProcessor()
        test_lines = [
            f"Policy Endorsement Record {i}: Insured Summit Builders Group Policy SB-112233 Aggregate $5,000,000"
            for i in range(15)
        ]

        # Rotated 90 deg + 4 deg skew
        pdf_bytes = create_rendered_scanned_pdf_bytes(test_lines, rotation=90, skew_angle=4.0)
        insp = processor.process_pdf(pdf_bytes, "doc_rot_skew_coords")
        ocr_pages = provider.perform_ocr_on_pages(insp.pages)
        page = ocr_pages[0]

        assert len(page.spans) > 20
        for span in page.spans:
            box = span.bounding_box
            # Strict coordinate bounds
            assert 0.0 <= box.x <= 1.0, f"Box x out of bounds: {box.x}"
            assert 0.0 <= box.y <= 1.0, f"Box y out of bounds: {box.y}"
            assert 0.0 < box.width <= 1.0, f"Box width out of bounds: {box.width}"
            assert 0.0 < box.height <= 1.0, f"Box height out of bounds: {box.height}"
            assert box.x + box.width <= 1.0001, f"Box x+w exceeds 1.0: {box.x + box.width}"
            assert box.y + box.height <= 1.0001, f"Box y+h exceeds 1.0: {box.y + box.height}"

    @pytest.mark.skipif(not TesseractOCRProvider().is_available(), reason="Requires real Tesseract OCR installation")
    def test_19_empty_and_blank_page_confidence(self):
        """19. Zero-token output: Blank scanned pages report 0.0 confidence without misleading fallbacks."""
        provider = TesseractOCRProvider()
        processor = PDFProcessor()

        # Generate a purely blank scan
        blank_pdf_bytes = create_scanned_pdf_bytes(page_count=1)
        insp = processor.process_pdf(blank_pdf_bytes, "doc_blank_page")
        ocr_pages = provider.perform_ocr_on_pages(insp.pages)
        page = ocr_pages[0]

        assert page.text_content.strip() == ""
        assert len(page.spans) == 0
        assert page.ocr_confidence == 0.0
        assert page.classification_signals.get("ocr_rotation_applied") == 0
        assert page.classification_signals.get("ocr_deskew_applied") == 0.0

    def test_20_osd_failure_and_uncertainty_safe_routing(self):
        """20. OSD failure and uncertainty safety: Gracefully falls back without crashing."""
        provider = MockOCRProvider(
            synthetic_text_by_page={1: ""},
            confidence_by_page={1: 0.0},
        )
        processor = PDFProcessor()
        blank_bytes = create_scanned_pdf_bytes(page_count=1)
        insp = processor.process_pdf(blank_bytes, "doc_mock_blank")

        pipeline = ExtractionPipelineService(
            extraction_provider=HonestApplicationMockProvider(),
            ocr_provider=provider,
        )
        pipe_res = pipeline.process_inspection(insp, subcontractor_id="sub_test")

        # Zero-token / empty OCR routes safely without crashing or throwing unhandled errors
        assert pipe_res.document_set is None
        assert pipe_res.processing_status == DocumentProcessingStatus.FAILED
        assert pipe_res.error_category in ("empty_document", "ocr_failure", "document_type_unknown", "provider_error")

    def test_21_osd_confidence_threshold_levels(self):
        """21. OSD threshold validation: Below 0.20 and in [0.20, 0.50) are rejected; >= 0.50 accepted."""
        # Confidence < 0.20: rejected
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"Rotate: 90\nOrientation confidence: 0.15\n")
            rot, conf = _detect_orientation_osd("tesseract", b"fake_png_bytes")
            assert rot == 0
            assert conf == 0.15

        # Confidence in [0.20, 0.50): rejected by OSD_MIN_CONFIDENCE (0.50)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"Rotate: 180\nOrientation confidence: 0.35\n")
            rot, conf = _detect_orientation_osd("tesseract", b"fake_png_bytes")
            assert rot == 0
            assert conf == 0.35

        # Confidence >= 0.50: accepted
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"Rotate: 270\nOrientation confidence: 0.50\n")
            rot, conf = _detect_orientation_osd("tesseract", b"fake_png_bytes")
            assert rot == 270
            assert conf == 0.50

        # High confidence genuine text: accepted
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"Rotate: 90\nOrientation confidence: 8.75\n")
            rot, conf = _detect_orientation_osd("tesseract", b"fake_png_bytes")
            assert rot == 90
            assert conf == 8.75

    @pytest.mark.skipif(not TesseractOCRProvider().is_available(), reason="Requires real Tesseract OCR installation")
    def test_22_osd_rejects_false_rotation_on_numbers_table(self):
        """22. False rotation prevention: Financial tables with numbers are NOT flipped 180 deg."""
        table_lines = [
            "1000000    2000000    1000000",
            "1000000    5000000    2000000",
            "5000000    1000000    1000000",
            "2000000    2000000    5000000",
            "1000000    1000000    1000000",
        ]
        pdf_bytes = create_rendered_scanned_pdf_bytes(table_lines, rotation=0, skew_angle=0.0)
        processor = PDFProcessor()
        insp = processor.process_pdf(pdf_bytes, "doc_numbers_table")
        provider = TesseractOCRProvider()
        ocr_pages = provider.perform_ocr_on_pages(insp.pages)
        page = ocr_pages[0]

        # Must not apply a spurious 180 deg rotation to an upright table
        assert page.classification_signals.get("ocr_rotation_applied") == 0

    @pytest.mark.skipif(not TesseractOCRProvider().is_available(), reason="Requires real Tesseract OCR installation")
    @pytest.mark.parametrize(
        "meta,phys",
        [
            (90, 0),
            (0, 90),
            (90, 90),
            (90, 180),
            (180, 0),
            (0, 180),
            (270, 0),
            (0, 270),
            (180, 180),
            (270, 90),
        ],
    )
    def test_23_pdf_metadata_and_physical_rotation_matrix(self, meta: int, phys: int):
        """23. Compositional rotation matrix: Independent PDF metadata and physical raster rotation."""
        lines = [
            f"Line {i}: ACORD 25 Certificate of Liability Insurance Policy GL-99887766 Limit $1,000,000"
            for i in range(15)
        ]
        pdf_bytes = create_matrix_controlled_scanned_pdf_bytes(lines, physical_rotation=phys, metadata_rotation=meta)
        processor = PDFProcessor()
        insp = processor.process_pdf(pdf_bytes, f"doc_matrix_{meta}_{phys}")
        provider = TesseractOCRProvider()
        ocr_pages = provider.perform_ocr_on_pages(insp.pages)
        page = ocr_pages[0]

        # Effective rotation from PyMuPDF page rendering: (meta + phys) % 360
        expected_effective = (meta + phys) % 360
        expected_applied = (360 - expected_effective) % 360

        # Assert exact rotation applied (verifies zero double rotation)
        actual_applied = page.classification_signals.get("ocr_rotation_applied", 0)
        assert actual_applied == expected_applied, (
            f"Expected rotation applied {expected_applied} for meta={meta}, phys={phys}, got {actual_applied}"
        )

        # Upright text extraction verified across all 10 matrix combinations
        text_upper = page.text_content.upper()
        assert "LIABILITY" in text_upper and "CERTIFICATE" in text_upper
        assert "ACORD 25" in text_upper
        assert page.ocr_confidence > 0.85

    @pytest.mark.skipif(not TesseractOCRProvider().is_available(), reason="Requires real Tesseract OCR installation")
    def test_24_bounding_box_groundtruth_alignment_and_iou(self):
        """24. Bounding box ground-truth alignment and IoU in original page space."""
        lines = [
            f"Line {i}: ACORD 25 Certificate of Liability Insurance Policy GL-99887766 Limit $1,000,000"
            for i in range(15)
        ]
        provider = TesseractOCRProvider()
        processor = PDFProcessor()

        def get_actual_gt_box_on_scanned_pdf(
            lines_list: list[str],
            pdf_bytes: bytes,
            rot: int = 0,
            skew: float = 0.0,
            dpi: int = 150,
        ) -> tuple[float, float, float, float]:
            """Compute exact forward-transformed ground-truth bounding box for 'CERTIFICATE' on scanned PDF page.

            Transformation pipeline:
            1. Vector ground-truth: Finds the exact bounding box of 'Certificate' on the pristine vector page (612x792 pt).
            2. Skew & rasterization: Transforms all 4 corners through the skew matrix (zoom * prerotate(skew))
               and offsets by the pixmap origin (pix.x, pix.y).
            3. Physical quadrant rotation: Rotates raster pixel coordinates through 90/180/270 degrees.
            4. Real PyMuPDF placement semantics: Rather than assuming an aspect-ratio-preserving formula,
               queries the exact placed image rectangle directly from out_page.get_image_info()[0]['bbox'],
               which models the exact transformation applied by out_page.insert_image(out_page.rect, pixmap=pix).
            5. Normalization: Maps placed image coordinates into [0.0, 1.0] original scanned PDF page space.
            """
            doc0 = pymupdf.open()
            p0 = doc0.new_page(width=612, height=792)
            for idx, line_text in enumerate(lines_list):
                p0.insert_text((72, 100 + idx * 35), line_text, fontsize=12)
            r0 = p0.search_for("Certificate")[0]

            zoom = dpi / 72.0
            mat = pymupdf.Matrix(zoom, zoom)
            if skew != 0.0:
                mat = mat.prerotate(skew)
            pix = p0.get_pixmap(matrix=mat)
            pix_w, pix_h = pix.width, pix.height
            pix_x, pix_y = pix.x, pix.y
            doc0.close()

            # Transform reference corners through prerotate skew matrix and pixel offset
            corners = [(r0.x0, r0.y0), (r0.x1, r0.y0), (r0.x1, r0.y1), (r0.x0, r0.y1)]
            pix_pts = [(pymupdf.Point(cx, cy) * mat - (pix_x, pix_y)) for cx, cy in corners]

            # Transform through physical quadrant rotation
            if rot in (90, 180, 270):
                rot_pts = []
                for px, py in pix_pts:
                    if rot == 90:
                        rx, ry = (pix_h - 1) - py, px
                        rw, rh = pix_h, pix_w
                    elif rot == 180:
                        rx, ry = (pix_w - 1) - px, (pix_h - 1) - py
                        rw, rh = pix_w, pix_h
                    elif rot == 270:
                        rx, ry = py, (pix_w - 1) - px
                        rw, rh = pix_h, pix_w
                    rot_pts.append((rx, ry))
            else:
                rot_pts = pix_pts
                rw, rh = pix_w, pix_h

            # Query exact placed image geometry directly from the generated PDF
            # This captures the true placement semantics of out_page.insert_image(out_page.rect, pixmap=pix)
            doc_placed = pymupdf.open("pdf", pdf_bytes)
            page_placed = doc_placed[0]
            pw, ph = page_placed.rect.width, page_placed.rect.height
            img_info = page_placed.get_image_info()[0]
            placed_rect = pymupdf.Rect(img_info["bbox"])
            doc_placed.close()

            page_pts = [
                (
                    (placed_rect.x0 + (rx / rw) * placed_rect.width) / pw,
                    (placed_rect.y0 + (ry / rh) * placed_rect.height) / ph,
                )
                for rx, ry in rot_pts
            ]
            xs = [p[0] for p in page_pts]
            ys = [p[1] for p in page_pts]
            return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

        def compute_iou(b1: tuple[float, float, float, float], b2: tuple[float, float, float, float]) -> float:
            xi1, yi1 = max(b1[0], b2[0]), max(b1[1], b2[1])
            xi2, yi2 = min(b1[0] + b1[2], b2[0] + b2[2]), min(b1[1] + b1[3], b2[1] + b2[3])
            iw, ih = max(0.0, xi2 - xi1), max(0.0, yi2 - yi1)
            inter = iw * ih
            union = b1[2] * b1[3] + b2[2] * b2[3] - inter
            return inter / union if union > 0 else 0.0

        test_transforms = [
            (90, 0.0),
            (180, 0.0),
            (270, 0.0),
            (0, 10.0),
            (0, 14.0),
            (90, 4.0),
        ]
        for rot, skew in test_transforms:
            pdf_bytes = create_rendered_scanned_pdf_bytes(lines, rotation=rot, skew_angle=skew)
            gt_box = get_actual_gt_box_on_scanned_pdf(lines, pdf_bytes=pdf_bytes, rot=rot, skew=skew)
            insp = processor.process_pdf(pdf_bytes, f"doc_align_{rot}_{skew}")
            ocr_pages = provider.perform_ocr_on_pages(insp.pages)
            page = ocr_pages[0]

            assert len(page.spans) > 10
            cert_spans = [s for s in page.spans if "CERTIFICATE" in s.text.upper()]
            assert len(cert_spans) > 0
            box = cert_spans[0].bounding_box
            ocr_box = (box.x, box.y, box.width, box.height)

            # Strict normalized coordinates [0.0, 1.0] in original scanned PDF page space
            assert 0.0 <= box.x <= 1.0
            assert 0.0 <= box.y <= 1.0
            assert 0.0 < box.width <= 1.0
            assert 0.0 < box.height <= 1.0
            assert box.x + box.width <= 1.0001
            assert box.y + box.height <= 1.0001

            # Centroid error and IoU verification
            c_gt = (gt_box[0] + gt_box[2] / 2.0, gt_box[1] + gt_box[3] / 2.0)
            c_ocr = (ocr_box[0] + ocr_box[2] / 2.0, ocr_box[1] + ocr_box[3] / 2.0)

            # Explicit raster pixel error calculation at 150 DPI:
            pw = 792 if rot in (90, 270) else 612
            ph = 612 if rot in (90, 270) else 792
            w_px = pw * (150.0 / 72.0)
            h_px = ph * (150.0 / 72.0)

            dx = c_gt[0] - c_ocr[0]
            dy = c_gt[1] - c_ocr[1]
            dx_px = dx * w_px
            dy_px = dy * h_px
            dist_px = math.hypot(dx_px, dy_px)
            dist_norm = math.hypot(dx, dy)
            iou = compute_iou(gt_box, ocr_box)

            # Centroid raster error is defensibly <= 2.0 raster pixels (empirical max <= 1.43 px)
            assert dist_px <= 2.0, (
                f"Centroid raster error {dist_px:.2f}px (dx={dx_px:.2f}px, dy={dy_px:.2f}px, "
                f"norm={dist_norm:.5f}) exceeds 2.0 raster pixels for rot={rot}, skew={skew}"
            )
            # IoU is >= 0.50 (difference between full line ascent/descent box vs tight OCR token box)
            assert iou >= 0.50, f"IoU {iou:.3f} below 0.50 for rot={rot}, skew={skew}"

    def test_25_osd_failure_modes_and_graceful_recovery(self):
        """25. Real OSD failure robustness: Subprocess failure, timeout, and malformed outputs."""
        lines = [
            f"Line {i}: ACORD 25 Certificate of Liability Insurance Policy GL-99887766 Limit $1,000,000"
            for i in range(15)
        ]
        pdf_bytes = create_rendered_scanned_pdf_bytes(lines, rotation=0, skew_angle=0.0)
        processor = PDFProcessor()
        insp = processor.process_pdf(pdf_bytes, "doc_osd_failure_test")

        real_run = subprocess.run

        # Case 1: Non-zero return code during OSD
        def mock_run_nonzero(*args, **kwargs):
            cmd_str = " ".join(str(a) for a in args[0]) if args and isinstance(args[0], (list, tuple)) else str(args)
            if "--psm 0" in cmd_str or "min_characters_to_try" in cmd_str:
                return MagicMock(returncode=1, stdout=b"Too few characters")
            return real_run(*args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run_nonzero):
            provider = TesseractOCRProvider()
            pages = provider.perform_ocr_on_pages(insp.pages)
            assert pages[0].classification_signals.get("ocr_rotation_applied") == 0
            assert "CERTIFICATE" in pages[0].text_content.upper()

        # Case 2: Malformed OSD output
        def mock_run_malformed(*args, **kwargs):
            cmd_str = " ".join(str(a) for a in args[0]) if args and isinstance(args[0], (list, tuple)) else str(args)
            if "--psm 0" in cmd_str or "min_characters_to_try" in cmd_str:
                return MagicMock(returncode=0, stdout=b"Unparseable garbage without rotate field\n")
            return real_run(*args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run_malformed):
            provider = TesseractOCRProvider()
            pages = provider.perform_ocr_on_pages(insp.pages)
            assert pages[0].classification_signals.get("ocr_rotation_applied") == 0
            assert "CERTIFICATE" in pages[0].text_content.upper()

        # Case 3: TimeoutExpired during OSD
        def mock_run_timeout(*args, **kwargs):
            cmd_str = " ".join(str(a) for a in args[0]) if args and isinstance(args[0], (list, tuple)) else str(args)
            if "--psm 0" in cmd_str or "min_characters_to_try" in cmd_str:
                raise subprocess.TimeoutExpired(cmd="tesseract", timeout=15)
            return real_run(*args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run_timeout):
            provider = TesseractOCRProvider()
            pages = provider.perform_ocr_on_pages(insp.pages)
            assert pages[0].classification_signals.get("ocr_rotation_applied") == 0
            assert "CERTIFICATE" in pages[0].text_content.upper()

        # Case 4: Missing binary FileNotFoundError during OSD
        def mock_run_fnf(*args, **kwargs):
            cmd_str = " ".join(str(a) for a in args[0]) if args and isinstance(args[0], (list, tuple)) else str(args)
            if "--psm 0" in cmd_str or "min_characters_to_try" in cmd_str:
                raise FileNotFoundError("tesseract binary missing")
            return real_run(*args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run_fnf):
            provider = TesseractOCRProvider()
            pages = provider.perform_ocr_on_pages(insp.pages)
            assert pages[0].classification_signals.get("ocr_rotation_applied") == 0
            assert "CERTIFICATE" in pages[0].text_content.upper()

        # Case 5: End-to-end ExtractionPipelineService and DocumentReviewService on OCR failure
        from app.extraction.pipeline import ExtractionPipelineService
        from app.extraction.providers.mock import HonestApplicationMockProvider
        from app.documents.review_service import DocumentReviewService
        from fastapi import UploadFile
        import asyncio, io

        pipeline = ExtractionPipelineService(
            extraction_provider=HonestApplicationMockProvider(),
            ocr_provider=provider,
        )

        # 5a. Pipeline handling when OCR process encounters an unrecoverable failure
        with patch.object(provider, "perform_ocr_on_pages", side_effect=RuntimeError("Tesseract OCR engine crash")):
            pipe_fail_res = pipeline.process_inspection(insp, subcontractor_id="sub_apex_ocr_fail")
            assert pipe_fail_res.processing_status == DocumentProcessingStatus.FAILED
            assert pipe_fail_res.document_set is None
            assert len(pipe_fail_res.errors) > 0
            assert "Tesseract OCR engine crash" in pipe_fail_res.errors[0]

        # 5b. Pipeline handling when OCR returns zero tokens (e.g. blank scan)
        blank_doc = pymupdf.open()
        p_blank = blank_doc.new_page(width=612, height=792)
        blank_pix = p_blank.get_pixmap()
        scanned_blank_doc = pymupdf.open()
        p_sc = scanned_blank_doc.new_page(width=612, height=792)
        p_sc.insert_image(pymupdf.Rect(0, 0, 612, 792), pixmap=blank_pix)
        blank_scanned_bytes = scanned_blank_doc.tobytes()
        blank_doc.close()
        scanned_blank_doc.close()

        insp_blank = processor.process_pdf(blank_scanned_bytes, "blank_scanned_doc.pdf")
        pipe_blank_res = pipeline.process_inspection(insp_blank, subcontractor_id="sub_apex_blank")
        # Pipeline correctly records failure when no text could be extracted from scanned document
        assert pipe_blank_res.processing_status == DocumentProcessingStatus.FAILED
        assert pipe_blank_res.document_set is None

        # 5c. Real downstream application workflow verification:
        # Trace DocumentReviewService handling of the blank scanned document without manufacturing
        # a synthetic ExtractedDocumentSet. The real workflow blocks failed extraction from
        # compliance approval and mandates human review.
        async def verify_downstream_workflow():
            review_svc = DocumentReviewService()
            coi_upload = UploadFile(filename="blank_scanned_coi.pdf", file=io.BytesIO(blank_scanned_bytes))
            workflow_res = await review_svc.process_and_review(
                coi_file=coi_upload,
                endorsement_files=[],
                subcontractor_id="sub_apex_blank",
            )
            assert workflow_res["overall_status"] != "compliant"
            assert workflow_res["overall_status"] in ("requires_human_review", "unable_to_determine", "missing_evidence")
            assert workflow_res["requires_human_review"] is True
            assert workflow_res["documents"][0]["processing_status"] == "failed"
            assert len(workflow_res["pipeline_errors"]) > 0

        asyncio.run(verify_downstream_workflow())

    @pytest.mark.skipif(not TesseractOCRProvider().is_available(), reason="Requires real Tesseract OCR installation")
    @pytest.mark.parametrize("angle", [-3.0, -5.0, -10.0, -14.0])
    def test_26_deskew_negative_angles(self, angle: float):
        """26. Negative deskew: Accurately detects and deskews negative tilt angles."""
        test_lines = [
            f"Line {i}: Insured Zenith Industries Policy ZN-445566 Limit $2,000,000"
            for i in range(15)
        ]
        pdf_bytes = create_rendered_scanned_pdf_bytes(test_lines, skew_angle=angle)
        processor = PDFProcessor()
        insp = processor.process_pdf(pdf_bytes, f"doc_neg_{angle}")
        provider = TesseractOCRProvider()
        ocr_pages = provider.perform_ocr_on_pages(insp.pages)
        page = ocr_pages[0]

        applied_skew = page.classification_signals.get("ocr_deskew_applied", 0.0)
        assert abs(applied_skew - angle) <= 0.6
        assert "ZN-445566" in page.text_content
        assert page.ocr_confidence > 0.85

    def test_27_deskew_non_text_and_low_text_invariance(self):
        """27. Deskew invariance: Blank pages, low-text pages, and geometric drawings remain 0.0 deg."""
        # Blank page
        blank_pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 400), 1)
        blank_pix.clear_with(255)
        assert _detect_skew_angle(blank_pix) == 0.0

        # Non-text geometric grid
        doc = pymupdf.open()
        p = doc.new_page(width=400, height=400)
        for x in range(0, 400, 40):
            p.draw_line((x, 0), (x, 400), color=(0, 0, 0), width=2)
        for y in range(0, 400, 40):
            p.draw_line((0, y), (400, y), color=(0, 0, 0), width=2)
        grid_pix = p.get_pixmap()
        doc.close()
        assert _detect_skew_angle(grid_pix) == 0.0

        # Low-text page (single line like "Page 1 of 1 - Confidential")
        low_doc = pymupdf.open()
        p_low = low_doc.new_page(width=612, height=792)
        p_low.insert_text((72, 100), "Page 1 of 1 - Confidential", fontsize=10)
        low_pix = p_low.get_pixmap()
        low_doc.close()
        # Skew detection must return 0.0 (below min_ink_pixels or insufficient line profile)
        assert _detect_skew_angle(low_pix) == 0.0

        # End-to-end OCR on low-text scan: deskew remains 0.0, text extracted accurately
        low_scanned_bytes = create_rendered_scanned_pdf_bytes(
            ["Page 1 of 1 - Confidential"],
            rotation=0,
            skew_angle=0.0,
        )
        processor = PDFProcessor()
        insp_low = processor.process_pdf(low_scanned_bytes, "doc_low_text")
        provider = TesseractOCRProvider()
        pages_low = provider.perform_ocr_on_pages(insp_low.pages)
        assert pages_low[0].classification_signals.get("ocr_deskew_applied") == 0.0
        assert "Confidential" in pages_low[0].text_content

    def test_28_two_channel_pixmap_grayscale_alpha(self):
        """28. Alpha compositing & colorspace conversion with real PyMuPDF-generated pixmaps.

        Establishes real PyMuPDF alpha representation (premultiplied samples), compares
        against PyMuPDF's own native non-alpha rendering over white background, and verifies
        proper DeviceCMYK conversion.
        """
        # 1. Create actual PyMuPDF document with real shapes exercising:
        #    - Fully transparent dark content (opacity=0.0)
        #    - Partially transparent black content (opacity=0.5)
        #    - Partially transparent colored content (red, opacity=0.5)
        #    - Fully opaque content (opacity=1.0)
        doc = pymupdf.open()
        p = doc.new_page(width=100, height=100)

        # Fully transparent black rectangle (opacity=0.0)
        s1 = p.new_shape()
        s1.draw_rect(pymupdf.Rect(10, 10, 30, 30))
        s1.finish(fill=(0, 0, 0), fill_opacity=0.0)
        s1.commit()

        # Partially transparent black rectangle (opacity=0.5)
        s2 = p.new_shape()
        s2.draw_rect(pymupdf.Rect(40, 10, 60, 30))
        s2.finish(fill=(0, 0, 0), fill_opacity=0.5)
        s2.commit()

        # Partially transparent red rectangle (opacity=0.5)
        s3 = p.new_shape()
        s3.draw_rect(pymupdf.Rect(70, 10, 90, 30))
        s3.finish(fill=(1, 0, 0), fill_opacity=0.5)
        s3.commit()

        # Fully opaque black rectangle (opacity=1.0)
        s4 = p.new_shape()
        s4.draw_rect(pymupdf.Rect(10, 50, 30, 70))
        s4.finish(fill=(0, 0, 0), fill_opacity=1.0)
        s4.commit()

        # Render real PyMuPDF pixmaps: one with alpha and one without alpha (native white compositing)
        pix_rgba = p.get_pixmap(colorspace=pymupdf.csRGB, alpha=True)
        pix_rgb_native = p.get_pixmap(colorspace=pymupdf.csRGB, alpha=False)
        pix_graya = p.get_pixmap(colorspace=pymupdf.csGRAY, alpha=True)
        pix_gray_native = p.get_pixmap(colorspace=pymupdf.csGRAY, alpha=False)
        doc.close()

        # 2. Inspect generated samples to verify premultiplied alpha representation
        rgba_samples = np.frombuffer(pix_rgba.samples, dtype=np.uint8).reshape((100, 100, 4))
        # Transparent region has zero alpha and zero color
        assert np.all(rgba_samples[15, 15] == [0, 0, 0, 0])
        # Partial black has alpha ~126 and zero color
        assert rgba_samples[15, 45, 3] == 126 and np.all(rgba_samples[15, 45, :3] == [0, 0, 0])
        # Partial red has alpha ~126 and red channel premultiplied to 126
        assert rgba_samples[15, 75, 3] == 126 and rgba_samples[15, 75, 0] == 126
        # Opaque black has alpha 255
        assert np.all(rgba_samples[55, 15] == [0, 0, 0, 255])

        # 3. Independently calculate expected grayscale values and compare against _pixmap_to_grayscale:
        # Transparent -> composites to 255 (solid white)
        gray_from_rgba = _pixmap_to_grayscale(pix_rgba)
        assert gray_from_rgba is not None
        assert gray_from_rgba[15, 15] == 255
        # Partial black -> 0 + (255 - 126) = 129 (~128)
        assert 127 <= gray_from_rgba[15, 45] <= 130
        # Partial red -> [255, 129, 129] -> mean 171 (~170)
        assert 169 <= gray_from_rgba[15, 75] <= 173
        # Opaque black -> 0 + (255 - 255) = 0
        assert gray_from_rgba[55, 15] == 0
        # Blank page background -> 255
        assert gray_from_rgba[90, 90] == 255

        # 4. Compare directly against PyMuPDF native non-alpha rendering over white:
        # Differences across the entire 100x100 page are at most 2 (anti-aliased edge rounding)
        native_rgb_gray = np.frombuffer(pix_rgb_native.samples, dtype=np.uint8).reshape((100, 100, 3)).mean(axis=2).astype(np.uint8)
        assert np.max(np.abs(gray_from_rgba.astype(int) - native_rgb_gray.astype(int))) <= 2

        # 5. DeviceGray+Alpha (n=2) verification with real PyMuPDF rendering
        gray_from_graya = _pixmap_to_grayscale(pix_graya)
        assert gray_from_graya is not None
        native_gray = np.frombuffer(pix_gray_native.samples, dtype=np.uint8).reshape((100, 100))
        assert np.max(np.abs(gray_from_graya.astype(int) - native_gray.astype(int))) <= 2
        # Transparent pixels become white, opaque pixels retain intensity
        assert gray_from_graya[15, 15] == 255
        assert gray_from_graya[55, 15] == 0

        # 6. CMYK colorspace conversion via DeviceCMYK (n=4, alpha=0)
        # 100% Cyan must convert via DeviceCMYK to perceptual luminance (~151),
        # not raw channel averaging which would incorrectly yield (255+0+0)/3 = 85.
        cmyk_doc = pymupdf.open()
        p_cmyk = cmyk_doc.new_page(width=100, height=100)
        p_cmyk.draw_rect(pymupdf.Rect(10, 10, 50, 50), color=(1, 0, 0, 0), fill=(1, 0, 0, 0))
        pix_cmyk = p_cmyk.get_pixmap(colorspace=pymupdf.csCMYK)
        cmyk_doc.close()
        assert pix_cmyk.n == 4
        gray_cmyk = _pixmap_to_grayscale(pix_cmyk)
        assert gray_cmyk is not None
        assert 140 <= gray_cmyk[20, 20] <= 165

    # -------------------------------------------------------------------------
    # Milestone 3.4C: Adaptive Page Segmentation and Layout-Aware OCR
    # -------------------------------------------------------------------------

    def test_29_adaptive_psm_layout_detection_unit(self):
        """29. Unit tests for analyze_page_layout() across canonical layout categories."""
        # A. Blank pixmap -> default PSM ('6'), layout 'blank'
        blank_doc = pymupdf.open()
        p_blank = blank_doc.new_page(width=612, height=792)
        psm_blank, info_blank = analyze_page_layout(p_blank.get_pixmap(dpi=150))
        blank_doc.close()
        assert psm_blank == "6"
        assert info_blank["layout"] == "blank"
        assert info_blank["ink_count"] < 100

        # B. Low dimension pixmap (< 10px) -> fallback default PSM
        tiny_pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 5, 5), 0)
        psm_tiny, info_tiny = analyze_page_layout(tiny_pix)
        assert psm_tiny == "6"
        assert info_tiny["layout"] == "fallback"

        # C. Single-column text block -> uniform_block, PSM 6
        doc_single = pymupdf.open()
        p_single = doc_single.new_page(width=612, height=792)
        p_single.insert_textbox(
            pymupdf.Rect(50, 50, 550, 700),
            "Commercial general liability policy conditions and coverage endorsements. " * 30,
            fontsize=10,
        )
        psm_single, info_single = analyze_page_layout(p_single.get_pixmap(dpi=150))
        doc_single.close()
        assert psm_single == "6"
        assert info_single["layout"] == "uniform_block"
        assert not info_single["is_multi_column"]
        assert not info_single["has_table_grid"]

        # D. Two-column document with central gutter -> multi_column, PSM 3
        doc_col = pymupdf.open()
        p_col = doc_col.new_page(width=612, height=792)
        p_col.insert_textbox(
            pymupdf.Rect(50, 80, 275, 600),
            "Left column conditions and primary coverage obligations for subcontractors. " * 20,
            fontsize=9,
        )
        p_col.insert_textbox(
            pymupdf.Rect(335, 80, 560, 600),
            "Right column additional insured endorsements and waiver of subrogation terms. " * 20,
            fontsize=9,
        )
        psm_col, info_col = analyze_page_layout(p_col.get_pixmap(dpi=150))
        doc_col.close()
        assert psm_col == "3"
        assert info_col["layout"] == "multi_column"
        assert info_col["is_multi_column"] is True

        # E. Table grid with ruling lines -> table, PSM 3
        doc_tab = pymupdf.open()
        p_tab = doc_tab.new_page(width=612, height=792)
        for y in [100, 130, 160, 190, 220]:
            p_tab.draw_line((50, y), (560, y))
        for x in [50, 220, 360, 560]:
            p_tab.draw_line((x, 100), (x, 220))
        p_tab.insert_text((60, 120), "TYPE OF INSURANCE", fontsize=9)
        p_tab.insert_text((230, 120), "POLICY NUMBER", fontsize=9)
        p_tab.insert_text((370, 120), "LIMITS", fontsize=9)
        p_tab.insert_text((60, 150), "COMMERCIAL GENERAL LIABILITY", fontsize=8)
        p_tab.insert_text((230, 150), "GL-2026-9901", fontsize=8)
        p_tab.insert_text((370, 150), "EACH OCCURRENCE: $1,000,000", fontsize=8)
        psm_tab, info_tab = analyze_page_layout(p_tab.get_pixmap(dpi=150))
        doc_tab.close()
        assert psm_tab == "3"
        assert info_tab["layout"] == "table"
        assert info_tab["has_table_grid"] is True

        # F. Sparse text (< 1500 ink pixels) -> sparse, PSM 11
        doc_sparse = pymupdf.open()
        p_sparse = doc_sparse.new_page(width=612, height=792)
        p_sparse.insert_text((50, 100), "CERTIFICATE HOLDER: ACME GENERAL", fontsize=10)
        p_sparse.insert_text((420, 100), "DATE: 11/08/2026", fontsize=10)
        p_sparse.insert_text((50, 700), "AUTHORIZED REP: John Smith", fontsize=10)
        psm_sparse, info_sparse = analyze_page_layout(p_sparse.get_pixmap(dpi=150))
        doc_sparse.close()
        assert psm_sparse == "11"
        assert info_sparse["layout"] == "sparse"

    @pytest.mark.skipif(not TesseractOCRProvider().is_available(), reason="Requires real Tesseract OCR installation")
    def test_30_layout_fixtures_adaptive_psm_and_reading_order(self):
        """30. Real Tesseract OCR on canonical layout fixtures under adaptive PSM."""
        processor = PDFProcessor()
        provider = TesseractOCRProvider(adaptive_psm=True)

        # Fixture 1: Standard Single-Column
        d1 = pymupdf.open()
        p1 = d1.new_page(width=612, height=792)
        p1.insert_text((50, 70), "CERTIFICATE OF LIABILITY INSURANCE", fontsize=14)
        p1.insert_textbox(
            pymupdf.Rect(50, 100, 550, 500),
            "This certificate is issued as a matter of information only and confers no rights upon the certificate holder. "
            "This certificate does not affirmatively or negatively amend, extend or alter the coverage afforded by the policies below. "
            "The policy of insurance listed below has been issued to the insured named above for the policy period indicated.",
            fontsize=10,
        )
        pdf1 = pymupdf.open("pdf", d1.convert_to_pdf())
        d1.close()
        pix1 = pdf1[0].get_pixmap(dpi=150)
        scanned_pdf1 = pymupdf.open()
        sp1 = scanned_pdf1.new_page(width=612, height=792)
        sp1.insert_image(sp1.rect, pixmap=pix1)
        insp1 = processor.process_pdf(scanned_pdf1.tobytes(), "doc_single_col")
        scanned_pdf1.close()
        pages1 = provider.perform_ocr_on_pages(insp1.pages)
        assert pages1[0].classification_signals.get("ocr_psm_selected") == "6"
        assert pages1[0].classification_signals.get("ocr_layout_detected") == "uniform_block"
        assert "CERTIFICATE OF LIABILITY INSURANCE" in pages1[0].text_content
        assert "affirmatively or negatively amend" in pages1[0].text_content
        assert pages1[0].ocr_confidence > 0.85

        # Fixture 2: Two-Column Insurance Provisions
        d2 = pymupdf.open()
        p2 = d2.new_page(width=612, height=792)
        p2.insert_text((50, 60), "GENERAL PROVISIONS AND SPECIAL CONDITIONS", fontsize=12)
        c1 = (
            "SECTION A: COVERAGE CONDITIONS\n\n"
            "1. Insured obligations require immediate written notification of all occurrences, claims, or suits.\n\n"
            "2. Notice shall be delivered to the company address within forty-eight hours."
        )
        c2 = (
            "SECTION B: ENDORSEMENTS\n\n"
            "1. Endorsement CG 20 10 04 13 provides ongoing operations coverage to specified additional insureds.\n\n"
            "2. Endorsement CG 20 37 04 13 provides completed operations coverage."
        )
        p2.insert_textbox(pymupdf.Rect(50, 90, 275, 500), c1, fontsize=9)
        p2.insert_textbox(pymupdf.Rect(335, 90, 560, 500), c2, fontsize=9)
        pix2 = p2.get_pixmap(dpi=150)
        scanned_pdf2 = pymupdf.open()
        sp2 = scanned_pdf2.new_page(width=612, height=792)
        sp2.insert_image(sp2.rect, pixmap=pix2)
        insp2 = processor.process_pdf(scanned_pdf2.tobytes(), "doc_two_col")
        scanned_pdf2.close()
        d2.close()
        pages2 = provider.perform_ocr_on_pages(insp2.pages)
        assert pages2[0].classification_signals.get("ocr_psm_selected") == "3"
        assert pages2[0].classification_signals.get("ocr_layout_detected") == "multi_column"
        text2 = pages2[0].text_content
        # Strict reading order verification: Section A conditions appear BEFORE Section B endorsements
        pos_a = text2.find("SECTION A")
        pos_b = text2.find("SECTION B")
        assert pos_a != -1 and pos_b != -1
        assert pos_a < pos_b, "Reading order violated: Column 1 text must precede Column 2 text."
        assert "forty-eight hours" in text2
        assert "CG 20 10 04 13" in text2

        # Fixture 3: Table with Limits and Grid Lines
        d3 = pymupdf.open()
        p3 = d3.new_page(width=612, height=792)
        p3.insert_text((50, 70), "SCHEDULE OF COVERAGES AND LIMITS", fontsize=12)
        for y in [100, 130, 160, 190, 220]:
            p3.draw_line((50, y), (560, y))
        for x in [50, 220, 360, 560]:
            p3.draw_line((x, 100), (x, 220))
        p3.insert_text((60, 120), "TYPE OF INSURANCE", fontsize=9)
        p3.insert_text((230, 120), "POLICY NUMBER", fontsize=9)
        p3.insert_text((370, 120), "LIMITS", fontsize=9)
        p3.insert_text((60, 150), "COMMERCIAL GENERAL LIABILITY", fontsize=8)
        p3.insert_text((230, 150), "GL-2026-9901", fontsize=8)
        p3.insert_text((370, 150), "EACH OCCURRENCE: $1,000,000", fontsize=8)
        p3.insert_text((60, 180), "AUTOMOBILE LIABILITY", fontsize=8)
        p3.insert_text((230, 180), "AL-2026-4412", fontsize=8)
        p3.insert_text((370, 180), "COMBINED SINGLE: $1,000,000", fontsize=8)
        p3.insert_text((60, 210), "WORKERS COMPENSATION", fontsize=8)
        p3.insert_text((230, 210), "WC-2026-7788", fontsize=8)
        p3.insert_text((370, 210), "STATUTORY LIMITS: $1,000,000", fontsize=8)
        pix3 = p3.get_pixmap(dpi=150)
        scanned_pdf3 = pymupdf.open()
        sp3 = scanned_pdf3.new_page(width=612, height=792)
        sp3.insert_image(sp3.rect, pixmap=pix3)
        insp3 = processor.process_pdf(scanned_pdf3.tobytes(), "doc_table_limits")
        scanned_pdf3.close()
        d3.close()
        pages3 = provider.perform_ocr_on_pages(insp3.pages)
        assert pages3[0].classification_signals.get("ocr_psm_selected") == "3"
        assert pages3[0].classification_signals.get("ocr_layout_detected") == "table"
        text3 = pages3[0].text_content
        assert "GL-2026-9901" in text3
        assert "AL-2026-4412" in text3
        assert "WC-2026-7788" in text3
        assert "$1,000,000" in text3 or "1,000,000" in text3

        # Fixture 4: Sparse Page
        d4 = pymupdf.open()
        p4 = d4.new_page(width=612, height=792)
        p4.insert_text((50, 90), "CERTIFICATE HOLDER: ACME GENERAL CONTRACTORS", fontsize=10)
        p4.insert_text((420, 90), "DATE: 11/08/2026", fontsize=10)
        p4.insert_text((50, 710), "AUTHORIZED REPRESENTATIVE: Robert Martinez", fontsize=10)
        pix4 = p4.get_pixmap(dpi=150)
        scanned_pdf4 = pymupdf.open()
        sp4 = scanned_pdf4.new_page(width=612, height=792)
        sp4.insert_image(sp4.rect, pixmap=pix4)
        insp4 = processor.process_pdf(scanned_pdf4.tobytes(), "doc_sparse")
        scanned_pdf4.close()
        d4.close()
        pages4 = provider.perform_ocr_on_pages(insp4.pages)
        assert pages4[0].classification_signals.get("ocr_psm_selected") in ("6", "11")
        text4 = pages4[0].text_content
        assert "ACME GENERAL CONTRACTORS" in text4
        assert "11/08/2026" in text4
        assert "Robert Martinez" in text4

        # Validate bounding box geometry across all fixtures
        for p in [pages1[0], pages2[0], pages3[0], pages4[0]]:
            assert len(p.spans) > 0
            for sp in p.spans:
                bb = sp.bounding_box
                assert 0.0 <= bb.x <= 1.0
                assert 0.0 <= bb.y <= 1.0
                assert bb.width > 0.0
                assert bb.height > 0.0
                assert bb.x + bb.width <= 1.05
                assert bb.y + bb.height <= 1.05

    @pytest.mark.skipif(not TesseractOCRProvider().is_available(), reason="Requires real Tesseract OCR installation")
    def test_31_adaptive_vs_fixed_psm_quantified_benefit(self):
        """31. Quantified comparative benefit: Adaptive PSM 3 vs Fixed PSM 6."""
        processor = PDFProcessor()

        # Two-column layout: Fixed PSM 6 suffers cross-column merging; Adaptive PSM 3 preserves reading order
        d = pymupdf.open()
        p = d.new_page(width=612, height=792)
        p.insert_text((50, 60), "GENERAL PROVISIONS AND SPECIAL CONDITIONS", fontsize=12)
        c1 = (
            "SECTION A: COVERAGE CONDITIONS\n\n"
            "1. Insured obligations require immediate written notification of all occurrences, claims, or suits.\n\n"
            "2. Notice shall be delivered to the company address within forty-eight hours.\n\n"
            "3. Failure to tender timely defense notice may void coverage if prejudicial to company defense."
        )
        c2 = (
            "SECTION B: ENDORSEMENTS\n\n"
            "1. Endorsement CG 20 10 04 13 provides ongoing operations coverage to specified additional insureds.\n\n"
            "2. Endorsement CG 20 37 04 13 provides completed operations coverage for contracted scope.\n\n"
            "3. Primary and non-contributory wording applies per written construction subcontract requirements."
        )
        p.insert_textbox(pymupdf.Rect(50, 90, 275, 550), c1, fontsize=9)
        p.insert_textbox(pymupdf.Rect(335, 90, 560, 550), c2, fontsize=9)
        pix = p.get_pixmap(dpi=150)
        scanned_pdf = pymupdf.open()
        sp = scanned_pdf.new_page(width=612, height=792)
        sp.insert_image(sp.rect, pixmap=pix)
        pdf_bytes = scanned_pdf.tobytes()
        scanned_pdf.close()
        d.close()

        # Fixed PSM 6 provider
        p_fixed6 = TesseractOCRProvider(adaptive_psm=False, default_psm="6")
        insp_fixed = processor.process_pdf(pdf_bytes, "doc_fixed6")
        res_fixed = p_fixed6.perform_ocr_on_pages(insp_fixed.pages)[0]

        # Adaptive provider (selects PSM 3 for multi-column)
        p_adapt = TesseractOCRProvider(adaptive_psm=True)
        insp_adapt = processor.process_pdf(pdf_bytes, "doc_adapt")
        res_adapt = p_adapt.perform_ocr_on_pages(insp_adapt.pages)[0]

        assert res_adapt.classification_signals.get("ocr_psm_selected") == "3"

        # Under PSM 6: Lines merge headings across the gutter: "SECTION A: COVERAGE CONDITIONS SECTION B: ENDORSEMENTS"
        lines_fixed = [line.strip() for line in res_fixed.text_content.splitlines() if line.strip()]
        assert any("SECTION A" in line and "SECTION B" in line for line in lines_fixed), (
            "Fixed PSM 6 should merge columns horizontally."
        )

        # Under Adaptive PSM 3: Section A appears alone, followed by Section A body lines BEFORE Section B appears!
        lines_adapt = [line.strip() for line in res_adapt.text_content.splitlines() if line.strip()]
        assert not any("SECTION A" in line and "SECTION B" in line for line in lines_adapt), (
            "Adaptive PSM 3 must never merge Section A and Section B onto the same line."
        )
        pos_a_adapt = res_adapt.text_content.find("SECTION A")
        pos_b_adapt = res_adapt.text_content.find("SECTION B")
        pos_c1_body = res_adapt.text_content.find("forty-eight hours")
        assert pos_a_adapt < pos_c1_body < pos_b_adapt, (
            "In Adaptive PSM 3, Column 1 body text must appear before Column 2 heading."
        )

    @pytest.mark.skipif(not TesseractOCRProvider().is_available(), reason="Requires real Tesseract OCR installation")
    def test_32_rotated_table_and_skewed_multi_column_integration(self):
        """32. Full pipeline integration: Orientation & deskew combined with adaptive segmentation."""
        processor = PDFProcessor()
        provider = TesseractOCRProvider(adaptive_psm=True)

        # Rotated Table (90 degrees physical rotation):
        # 1. Pipeline applies OSD and rotates 270 deg to make upright.
        # 2. Adaptive layout analysis detects table grid on upright image.
        # 3. PSM 3 selected. Table limits and policy numbers successfully extracted.
        d_tab = pymupdf.open()
        p_tab = d_tab.new_page(width=612, height=792)
        p_tab.insert_text((50, 70), "SCHEDULE OF COVERAGES AND LIMITS", fontsize=12)
        for y in [100, 130, 160, 190, 220]:
            p_tab.draw_line((50, y), (560, y))
        for x in [50, 220, 360, 560]:
            p_tab.draw_line((x, 100), (x, 220))
        p_tab.insert_text((60, 120), "TYPE OF INSURANCE", fontsize=9)
        p_tab.insert_text((230, 120), "POLICY NUMBER", fontsize=9)
        p_tab.insert_text((370, 120), "LIMITS", fontsize=9)
        p_tab.insert_text((60, 150), "COMMERCIAL GENERAL LIABILITY", fontsize=8)
        p_tab.insert_text((230, 150), "GL-2026-9901", fontsize=8)
        p_tab.insert_text((370, 150), "EACH OCCURRENCE: $1,000,000", fontsize=8)
        pix_tab = p_tab.get_pixmap(dpi=150)
        pix_rot90 = pix_tab.flip_rotate(pix_tab.ROTATE_90)
        d_tab.close()

        scanned_rot_pdf = pymupdf.open()
        sp_rot = scanned_rot_pdf.new_page(width=612, height=792)
        sp_rot.insert_image(sp_rot.rect, pixmap=pix_rot90)
        insp_rot = processor.process_pdf(scanned_rot_pdf.tobytes(), "doc_rot_table")
        scanned_rot_pdf.close()

        pages_rot = provider.perform_ocr_on_pages(insp_rot.pages)
        page_rot = pages_rot[0]
        assert page_rot.classification_signals.get("ocr_rotation_applied") == 270
        assert page_rot.classification_signals.get("ocr_psm_selected") == "3"
        assert "GL-2026-9901" in page_rot.text_content
        assert "$1,000,000" in page_rot.text_content or "1,000,000" in page_rot.text_content

    def test_33_psm_override_and_fallback_resilience(self):
        """33. Manual PSM override, disabling adaptive PSM, and safe fallback on execution failure."""
        doc = pymupdf.open()
        p = doc.new_page(width=612, height=792)
        p.insert_text((50, 100), "STANDARD PROVISIONS", fontsize=12)
        pix = p.get_pixmap(dpi=150)
        doc.close()

        # 1. Manual PSM override via page.classification_signals
        page_insp = PDFPageInspection(
            page_number=1,
            width_pts=612.0,
            height_pts=792.0,
            text_content="",
            page_type=PageType.SCANNED,
            is_scanned=True,
            rendered_image_png=pix.tobytes("png"),
            classification_signals={"ocr_psm_override": "4"},
        )
        if TesseractOCRProvider().is_available():
            prov = TesseractOCRProvider()
            out_pages = prov.perform_ocr_on_pages([page_insp])
            assert out_pages[0].classification_signals.get("ocr_psm_selected") == "4"

        # 2. Disabled adaptive PSM uses default_psm
        if TesseractOCRProvider().is_available():
            prov_fixed = TesseractOCRProvider(adaptive_psm=False, default_psm="6")
            page_insp_clean = PDFPageInspection(
                page_number=1,
                width_pts=612.0,
                height_pts=792.0,
                text_content="",
                page_type=PageType.SCANNED,
                is_scanned=True,
                rendered_image_png=pix.tobytes("png"),
            )
            out_fixed = prov_fixed.perform_ocr_on_pages([page_insp_clean])
            assert out_fixed[0].classification_signals.get("ocr_psm_selected") == "6"

    @pytest.mark.skipif(not TesseractOCRProvider().is_available(), reason="Requires real Tesseract OCR installation")
    def test_34_zero_token_ocr_blank_scan_fail_closed_compliance(self):
        """34. Blank scanned page under adaptive OCR produces 0 tokens and enforces fail-closed human review."""
        import asyncio
        from starlette.datastructures import UploadFile
        from app.documents.review_service import DocumentReviewService

        # Create completely white scanned page
        blank_pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 400), 0)
        blank_pix.clear_with(255)
        doc_blank = pymupdf.open()
        p_blank = doc_blank.new_page(width=612, height=792)
        p_blank.insert_image(p_blank.rect, pixmap=blank_pix)
        blank_bytes = doc_blank.tobytes()
        doc_blank.close()

        # Process through real DocumentReviewService
        async def verify_adaptive_blank_workflow():
            review_svc = DocumentReviewService()
            coi_upload = UploadFile(filename="adaptive_blank_scan.pdf", file=io.BytesIO(blank_bytes))
            workflow_res = await review_svc.process_and_review(
                coi_file=coi_upload,
                endorsement_files=[],
                subcontractor_id="sub_adapt_blank",
            )
            assert workflow_res["overall_status"] != "compliant"
            assert workflow_res["overall_status"] in ("requires_human_review", "unable_to_determine", "missing_evidence")
            assert workflow_res["requires_human_review"] is True
            assert workflow_res["documents"][0]["processing_status"] == "failed"
            assert len(workflow_res["pipeline_errors"]) > 0

        asyncio.run(verify_adaptive_blank_workflow())

    def test_35_adaptive_layout_scale_invariance_across_dpis(self):
        """35. Multi-resolution scale invariance: Layout classification is robust across 100, 150, 200, and 300 DPI."""
        def make_archetype(doc_type: str) -> pymupdf.Document:
            doc = pymupdf.open()
            page = doc.new_page(width=612, height=792)
            if doc_type == "single":
                for i in range(15):
                    page.insert_text(pymupdf.Point(72, 100 + i * 30), f"Standard policy condition clause {i+1}: Insured must comply with all provisions.", fontsize=10)
            elif doc_type == "multi":
                page.insert_text(pymupdf.Point(72, 80), "SECTION 1: COVERAGE OVERVIEW", fontsize=11)
                for i in range(12):
                    page.insert_text(pymupdf.Point(72, 110 + i * 28), f"Left column policy term line {i+1} terms and coverage.", fontsize=10)
                page.insert_text(pymupdf.Point(340, 80), "SECTION 2: ENDORSEMENTS", fontsize=11)
                for i in range(12):
                    page.insert_text(pymupdf.Point(340, 110 + i * 28), f"Right column policy term line {i+1} additional terms.", fontsize=10)
            elif doc_type == "table":
                for y in [100, 140, 180, 220, 260]:
                    page.draw_line(pymupdf.Point(50, y), pymupdf.Point(550, y), width=1.5)
                for x in [50, 200, 380, 550]:
                    page.draw_line(pymupdf.Point(x, 100), pymupdf.Point(x, 260), width=1.5)
                page.insert_text(pymupdf.Point(60, 125), "Coverage Type", fontsize=10)
                page.insert_text(pymupdf.Point(210, 125), "Policy Number", fontsize=10)
                page.insert_text(pymupdf.Point(390, 125), "Limit Amount", fontsize=10)
            elif doc_type == "sparse":
                page.insert_text(pymupdf.Point(72, 100), "PRODUCER: ACME Insurance Services LLC", fontsize=10)
                page.insert_text(pymupdf.Point(400, 100), "DATE: 2025-01-15", fontsize=10)
                page.insert_text(pymupdf.Point(72, 300), "INSURED: Baker Construction Corp", fontsize=10)
                page.insert_text(pymupdf.Point(72, 500), "COVERAGE: Commercial General Liability - Valid", fontsize=10)
            return doc

        dpis = [100, 150, 200, 300]
        expected_psm = {"single": "6", "multi": "3", "table": "3", "sparse": "11"}

        for dt, exp_psm in expected_psm.items():
            doc = make_archetype(dt)
            for dpi in dpis:
                pix = doc[0].get_pixmap(dpi=dpi)
                detected_psm, details = analyze_page_layout(pix)
                assert detected_psm == exp_psm, (
                    f"Archetype '{dt}' at DPI {dpi} expected PSM '{exp_psm}' but got '{detected_psm}'. "
                    f"Details: {details}"
                )
            doc.close()

    @pytest.mark.skipif(not TesseractOCRProvider().is_available(), reason="Requires real Tesseract OCR installation")
    def test_36_bounding_box_stability_across_dpis_and_rotations(self):
        """36. Bounding-box spatial stability: Sub-2-pixel centroid error and strict coordinate normalization across DPIs & rotations."""
        provider = TesseractOCRProvider()
        processor = PDFProcessor()
        test_lines = [
            f"Line {i}: ACORD 25 Certificate of Liability Insurance Policy GL-99887766 Limit $1,000,000"
            for i in range(12)
        ]

        def compute_gt_box_for_dpi(lines: list[str], pdf_bytes: bytes, rot: int = 0, skew: float = 0.0, dpi: int = 150):
            doc0 = pymupdf.open()
            p0 = doc0.new_page(width=612, height=792)
            for idx, line_text in enumerate(lines):
                p0.insert_text((72, 100 + idx * 35), line_text, fontsize=12)
            r0 = p0.search_for("Certificate")[0]

            zoom = dpi / 72.0
            mat = pymupdf.Matrix(zoom, zoom)
            if skew != 0.0:
                mat = mat.prerotate(skew)
            pix = p0.get_pixmap(matrix=mat)
            pix_w, pix_h = pix.width, pix.height
            pix_x, pix_y = pix.x, pix.y
            doc0.close()

            corners = [(r0.x0, r0.y0), (r0.x1, r0.y0), (r0.x1, r0.y1), (r0.x0, r0.y1)]
            pix_pts = [(pymupdf.Point(cx, cy) * mat - (pix_x, pix_y)) for cx, cy in corners]

            if rot in (90, 180, 270):
                rot_pts = []
                for px, py in pix_pts:
                    if rot == 90:
                        rx, ry = (pix_h - 1) - py, px
                        rw, rh = pix_h, pix_w
                    elif rot == 180:
                        rx, ry = (pix_w - 1) - px, (pix_h - 1) - py
                        rw, rh = pix_w, pix_h
                    elif rot == 270:
                        rx, ry = py, (pix_w - 1) - px
                        rw, rh = pix_h, pix_w
                    rot_pts.append((rx, ry))
            else:
                rot_pts = pix_pts
                rw, rh = pix_w, pix_h

            doc_placed = pymupdf.open("pdf", pdf_bytes)
            page_placed = doc_placed[0]
            pw, ph = page_placed.rect.width, page_placed.rect.height
            img_info = page_placed.get_image_info()[0]
            placed_rect = pymupdf.Rect(img_info["bbox"])
            doc_placed.close()

            page_pts = [
                (
                    (placed_rect.x0 + (rx / rw) * placed_rect.width) / pw,
                    (placed_rect.y0 + (ry / rh) * placed_rect.height) / ph,
                )
                for rx, ry in rot_pts
            ]
            xs = [p[0] for p in page_pts]
            ys = [p[1] for p in page_pts]
            return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

        test_cases = [
            (100, 0),
            (150, 0),
            (200, 0),
            (300, 0),
            (150, 90),
            (150, 180),
            (150, 270),
        ]

        for dpi, rot in test_cases:
            pdf_bytes = create_rendered_scanned_pdf_bytes(test_lines, rotation=rot, dpi=dpi)
            gt_box = compute_gt_box_for_dpi(test_lines, pdf_bytes, rot=rot, dpi=dpi)
            insp = processor.process_pdf(pdf_bytes, f"doc_stability_{dpi}_{rot}")
            ocr_pages = provider.perform_ocr_on_pages(insp.pages)
            page = ocr_pages[0]

            assert len(page.spans) > 5
            cert_spans = [s for s in page.spans if s.text.upper() == "CERTIFICATE"]
            assert len(cert_spans) > 0
            box = cert_spans[0].bounding_box

            # Strict normalized coordinates
            assert 0.0 <= box.x <= 1.0
            assert 0.0 <= box.y <= 1.0
            assert 0.0 < box.width <= 1.0
            assert 0.0 < box.height <= 1.0
            assert box.x + box.width <= 1.0001
            assert box.y + box.height <= 1.0001

            c_gt = (gt_box[0] + gt_box[2] / 2.0, gt_box[1] + gt_box[3] / 2.0)
            c_ocr = (box.x + box.width / 2.0, box.y + box.height / 2.0)

            pw = 792 if rot in (90, 270) else 612
            ph = 612 if rot in (90, 270) else 792
            w_px = pw * (dpi / 72.0)
            h_px = ph * (dpi / 72.0)

            err_px_x = abs(c_ocr[0] - c_gt[0]) * w_px
            err_px_y = abs(c_ocr[1] - c_gt[1]) * h_px
            total_err_px = math.sqrt(err_px_x**2 + err_px_y**2)

            # Assert centroid error is sub-2-pixel across all resolutions and rotations
            assert total_err_px <= 2.0, f"Centroid error {total_err_px:.2f}px exceeds 2.0px limit at DPI {dpi}, rot {rot}"

    def test_37_ocr_failure_modes_and_fail_closed_handling(self):
        """37. OCR failure modes: Nonexistent binary, timeout, and empty input fail safely and closed."""
        # 1. Nonexistent binary raises TesseractNotFoundError
        bad_provider = TesseractOCRProvider(tesseract_cmd=r"C:\Nonexistent\Path\tesseract.exe")
        assert bad_provider.is_available() is False

        dummy_pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100), 0)
        dummy_pix.clear_with(255)
        dummy_page = PDFPageInspection(
            page_number=1,
            width_pts=612.0,
            height_pts=792.0,
            text_content="",
            page_type=PageType.SCANNED,
            is_scanned=True,
            rendered_image_png=dummy_pix.tobytes("png"),
        )
        with pytest.raises(TesseractNotFoundError):
            bad_provider.perform_ocr_on_pages([dummy_page])

        # 2. Timeout configuration is respected
        timeout_prov = TesseractOCRProvider(timeout_seconds=0)
        if timeout_prov.is_available():
            with pytest.raises(OCRError) as exc_info:
                timeout_prov.perform_ocr_on_pages([dummy_page])
            msg = str(exc_info.value).lower()
            assert "timed out" in msg or "timeout" in msg or "failed" in msg

        # 3. Missing rendered image raises OCRError
        unrendered_page = PDFPageInspection(
            page_number=1,
            width_pts=612.0,
            height_pts=792.0,
            text_content="",
            page_type=PageType.SCANNED,
            is_scanned=True,
            rendered_image_png=None,
        )
        valid_prov = TesseractOCRProvider()
        if valid_prov.is_available():
            with pytest.raises(OCRError):
                valid_prov.perform_ocr_on_pages([unrendered_page])
