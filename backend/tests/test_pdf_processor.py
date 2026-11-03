"""Unit tests for the hardened PDF processing and classification pipeline."""

import unittest
from unittest.mock import patch
import pymupdf  # PyMuPDF

from app.documents.processor import (
    MAX_PAGE_COUNT,
    MAX_PDF_SIZE_BYTES,
    PDFProcessingError,
    PDFProcessor,
    classify_document_text,
    validate_pdf_bytes,
)
from app.schemas.insurance import DocumentType
from app.schemas.pdf import DocumentProcessingStatus


def create_minimal_pdf_bytes(
    text_lines: list[str],
    page_count: int = 1,
    rotation: int = 0,
    width: float = 612,
    height: float = 792,
) -> bytes:
    """Helper to generate a real synthetic PDF in memory with PyMuPDF."""
    doc = pymupdf.open()
    for _ in range(page_count):
        page = doc.new_page(width=width, height=height)
        if rotation != 0:
            page.set_rotation(rotation)
        y = 50
        for line in text_lines:
            page.insert_text((50, y), line, fontsize=11)
            y += 20
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def create_password_protected_pdf_bytes(password: str = "secret") -> bytes:
    """Generate an encrypted synthetic PDF."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((50, 50), "Confidential Insurance Document")
    pdf_bytes = doc.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner_secret",
        user_pw=password,
    )
    doc.close()
    return pdf_bytes


class TestPDFProcessor(unittest.TestCase):
    def setUp(self):
        self.processor = PDFProcessor(render_dpi=72)

    def test_validate_pdf_empty_bytes_raises_error(self):
        with self.assertRaises(PDFProcessingError) as ctx:
            validate_pdf_bytes(b"")
        self.assertIn("empty", str(ctx.exception).lower())

    def test_validate_pdf_exceeds_max_size_raises_error(self):
        fake_large_payload = b"%PDF" + b"x" * 1024
        with self.assertRaises(PDFProcessingError) as ctx:
            validate_pdf_bytes(fake_large_payload, max_size_bytes=512)
        self.assertIn("exceeds limit", str(ctx.exception).lower())

    def test_validate_pdf_missing_magic_bytes_raises_error(self):
        with self.assertRaises(PDFProcessingError) as ctx:
            validate_pdf_bytes(b"NOT_A_PDF_FILE_HEADER")
        self.assertIn("magic bytes", str(ctx.exception).lower())

    def test_corrupt_pdf_fails_gracefully(self):
        corrupt_bytes = b"%PDF-1.4\nCorrupt garbage data here..."
        with self.assertRaises(PDFProcessingError) as ctx:
            self.processor.process_pdf(corrupt_bytes, document_id="doc_corrupt")
        self.assertIn("corrupt or malformed", str(ctx.exception).lower())

    # ========================================================================
    # Hardening & Uncovered Behavior Tests
    # ========================================================================

    def test_pdf_exceeding_maximum_page_count_raises_error(self):
        """1. PDF exceeding the maximum page count must raise PDFProcessingError."""
        # Create a 5-page PDF with a processor capped at 3 pages
        pdf_bytes = create_minimal_pdf_bytes(["Sample line"], page_count=5)
        strict_processor = PDFProcessor(max_page_count=3)

        with self.assertRaises(PDFProcessingError) as ctx:
            strict_processor.process_pdf(pdf_bytes, document_id="doc_excess_pages")

        self.assertIn("page count (5) exceeds maximum allowed limit (3)", str(ctx.exception).lower())

    def test_password_protected_pdf_detected_and_rejected(self):
        """2. Password-protected / encrypted PDF must be detected and safely rejected."""
        enc_pdf_bytes = create_password_protected_pdf_bytes(password="secret123")

        with self.assertRaises(PDFProcessingError) as ctx:
            self.processor.process_pdf(enc_pdf_bytes, document_id="doc_encrypted")

        self.assertIn("password-protected or encrypted", str(ctx.exception).lower())

    def test_page_number_convention_is_strictly_one_based(self):
        """3. User-facing page numbers must be 1-indexed across all pages."""
        pdf_bytes = create_minimal_pdf_bytes(["Page content"], page_count=3)

        inspection = self.processor.process_pdf(pdf_bytes, document_id="doc_pages_test")

        self.assertEqual(len(inspection.pages), 3)
        self.assertEqual([p.page_number for p in inspection.pages], [1, 2, 3])

    def test_rotated_page_dimensions_and_rotation_metadata(self):
        """4. Rotated pages (e.g. 90 degrees) must report effective dimensions and rotation angle."""
        # Letter width=612, height=792 rotated 90 degrees -> effective width=792, height=612
        pdf_bytes = create_minimal_pdf_bytes(
            ["Rotated endorsement text"],
            page_count=1,
            rotation=90,
            width=612,
            height=792,
        )

        inspection = self.processor.process_pdf(pdf_bytes, document_id="doc_rotated")

        self.assertEqual(len(inspection.pages), 1)
        page = inspection.pages[0]
        self.assertEqual(page.rotation, 90)
        self.assertEqual(page.width_pts, 792.0)
        self.assertEqual(page.height_pts, 612.0)

    def test_bounding_box_normalization_and_validation(self):
        """5. Bounding box coordinates must be strictly normalized [0.0, 1.0]."""
        coi_lines = [
            "ACORD 25 (2016/03) CERTIFICATE OF LIABILITY INSURANCE",
            "INSURED: Apex Electrical Contractors LLC",
            "EACH OCCURRENCE: $1,000,000",
        ]
        pdf_bytes = create_minimal_pdf_bytes(coi_lines)

        inspection = self.processor.process_pdf(pdf_bytes, document_id="doc_bbox_test")

        page = inspection.pages[0]
        self.assertGreater(len(page.spans), 0)
        for span in page.spans:
            bbox = span.bounding_box
            self.assertGreaterEqual(bbox.x, 0.0)
            self.assertLessEqual(bbox.x, 1.0)
            self.assertGreaterEqual(bbox.y, 0.0)
            self.assertLessEqual(bbox.y, 1.0)
            self.assertGreaterEqual(bbox.width, 0.0)
            self.assertLessEqual(bbox.width, 1.0)
            self.assertGreaterEqual(bbox.height, 0.0)
            self.assertLessEqual(bbox.height, 1.0)
            # Ensure box does not exceed page boundary
            self.assertLessEqual(bbox.x + bbox.width, 1.0001)
            self.assertLessEqual(bbox.y + bbox.height, 1.0001)

    def test_scanned_image_only_pdf_requires_ocr_not_compliance_failure(self):
        """6. A scanned/image-only PDF with no text layer returns requires_ocr and is not a compliance failure."""
        # Create PDF with no text layer (e.g. empty page or raw raster)
        doc = pymupdf.open()
        doc.new_page(width=612, height=792)  # Completely blank page (representing pure scan)
        scanned_bytes = doc.tobytes()
        doc.close()

        inspection = self.processor.process_pdf(scanned_bytes, document_id="doc_scanned_test")

        self.assertTrue(inspection.is_scanned)
        self.assertEqual(inspection.processing_status, DocumentProcessingStatus.REQUIRES_OCR)
        self.assertEqual(inspection.classified_type, DocumentType.UNKNOWN)
        self.assertEqual(inspection.classification_confidence, 0.0)
        self.assertIn("scanned or image-only", inspection.classification_reason.lower())

    def test_page_processing_failure_surfaced_without_silent_dropping(self):
        """7. A per-page failure must be explicitly surfaced in errors and page models."""
        pdf_bytes = create_minimal_pdf_bytes(["Normal page 1", "Normal page 2"], page_count=2)

        # Mock page 1 to raise an exception during get_text to simulate corrupted page stream
        original_get_text = pymupdf.Page.get_text

        def mock_get_text(page_self, *args, **kwargs):
            if page_self.number == 0:  # page 1
                raise RuntimeError("Corrupt font/content stream on page 1")
            return original_get_text(page_self, *args, **kwargs)

        with patch.object(pymupdf.Page, "get_text", side_effect=mock_get_text, autospec=True):
            inspection = self.processor.process_pdf(pdf_bytes, document_id="doc_page_err")

        self.assertEqual(inspection.processing_status, DocumentProcessingStatus.PARTIAL_SUCCESS)
        self.assertEqual(len(inspection.processing_errors), 1)
        self.assertIn("Error processing page 1", inspection.processing_errors[0])
        self.assertIn("Corrupt font/content stream", inspection.pages[0].error_message)

    def test_rendering_memory_and_dimension_limits_enforced(self):
        """8. Rendering dimensions exceeding max_render_dimension_px must be caught."""
        # Create a huge page (e.g. 10,000 points wide)
        huge_doc = pymupdf.open()
        huge_doc.new_page(width=3000, height=3000)
        huge_bytes = huge_doc.tobytes()
        huge_doc.close()

        # Processor capped at 1000px max dimension, with 150 DPI (zoom = 150/72 ≈ 2.08 -> 6250px)
        strict_render_processor = PDFProcessor(render_dpi=150, max_render_dimension_px=1000)

        inspection = strict_render_processor.process_pdf(
            huge_bytes,
            document_id="doc_huge",
            render_images=True,
        )

        self.assertEqual(inspection.processing_status, DocumentProcessingStatus.FAILED)
        self.assertEqual(len(inspection.processing_errors), 1)
        self.assertIn("exceed maximum allowed dimension limit", inspection.processing_errors[0])

    # ========================================================================
    # Standard Classification Tests
    # ========================================================================

    def test_process_acord_25_coi_pdf(self):
        coi_lines = [
            "ACORD 25 (2016/03) CERTIFICATE OF LIABILITY INSURANCE",
            "PRODUCER: Apex Insurance Agency 100 Main St",
            "INSURED: Apex Electrical Contractors LLC",
            "GENERAL LIABILITY: EACH OCCURRENCE $1,000,000",
            "GENERAL AGGREGATE: $2,000,000",
            "POLICY NUMBER: GL-98765432",
            "POLICY EFF: 01/01/2025  POLICY EXP: 01/01/2026",
        ]
        pdf_bytes = create_minimal_pdf_bytes(coi_lines)

        inspection = self.processor.process_pdf(
            pdf_bytes=pdf_bytes,
            document_id="doc_coi_001",
            filename="coi_sample.pdf",
            render_images=True,
        )

        self.assertEqual(inspection.document_id, "doc_coi_001")
        self.assertEqual(inspection.page_count, 1)
        self.assertEqual(inspection.classified_type, DocumentType.COI)
        self.assertEqual(inspection.processing_status, DocumentProcessingStatus.PROCESSED)
        self.assertFalse(inspection.is_scanned)
        self.assertGreaterEqual(inspection.classification_confidence, 0.90)

        # Verify page details
        page = inspection.pages[0]
        self.assertEqual(page.page_number, 1)
        self.assertIn("CERTIFICATE OF LIABILITY INSURANCE", page.text_content)
        self.assertIsNotNone(page.rendered_image_png)
        self.assertTrue(page.rendered_image_png.startswith(b"\x89PNG"))

    def test_process_endorsement_pdf(self):
        end_lines = [
            "POLICY NUMBER: GL-98765432",
            "COMMERCIAL GENERAL LIABILITY CG 20 10 04 13",
            "THIS ENDORSEMENT CHANGES THE POLICY. PLEASE READ IT CAREFULLY.",
            "ADDITIONAL INSURED - OWNERS, LESSEES OR CONTRACTORS - SCHEDULED PERSON OR ORGANIZATION",
            "Name of Additional Insured: General Contractor Corp",
        ]
        pdf_bytes = create_minimal_pdf_bytes(end_lines)

        inspection = self.processor.process_pdf(
            pdf_bytes=pdf_bytes,
            document_id="doc_end_001",
            filename="cg2010.pdf",
        )

        self.assertEqual(inspection.classified_type, DocumentType.ENDORSEMENT)
        self.assertEqual(inspection.processing_status, DocumentProcessingStatus.PROCESSED)
        self.assertGreaterEqual(inspection.classification_confidence, 0.90)
        self.assertIn("CG 20 10", inspection.classification_reason)

    def test_process_declaration_page_pdf(self):
        dec_lines = [
            "COMMON POLICY DECLARATIONS",
            "POLICY NUMBER: CGL-100200300",
            "NAMED INSURED: Apex Electrical Contractors LLC",
            "ITEM 1. POLICY PERIOD: FROM 01/01/2025 TO 01/01/2026",
            "BUSINESS DESCRIPTION: Electrical Construction",
        ]
        pdf_bytes = create_minimal_pdf_bytes(dec_lines)

        inspection = self.processor.process_pdf(
            pdf_bytes=pdf_bytes,
            document_id="doc_dec_001",
            filename="dec_page.pdf",
        )

        self.assertEqual(inspection.classified_type, DocumentType.POLICY_DEC_PAGE)
        self.assertEqual(inspection.processing_status, DocumentProcessingStatus.PROCESSED)
        self.assertGreaterEqual(inspection.classification_confidence, 0.80)

    def test_process_unrecognized_pdf(self):
        random_lines = [
            "INVOICE #998811",
            "Deliver 500 lbs of copper wiring to jobsite",
            "Payment terms: Net 30 days",
        ]
        pdf_bytes = create_minimal_pdf_bytes(random_lines)

        inspection = self.processor.process_pdf(
            pdf_bytes=pdf_bytes,
            document_id="doc_invoice_001",
            filename="invoice.pdf",
        )

        self.assertEqual(inspection.classified_type, DocumentType.UNKNOWN)
        self.assertEqual(inspection.processing_status, DocumentProcessingStatus.PROCESSED)
        self.assertLess(inspection.classification_confidence, 0.50)


if __name__ == "__main__":
    unittest.main()
