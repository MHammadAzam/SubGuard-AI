"""PDF processing and document classification pipeline for SubGuard AI.

Provides:
- Security validation: file size, magic bytes (%PDF), password/encryption detection, page-count caps
- Extraction of text layers, page dimensions, rotation, and normalized bounding boxes via PyMuPDF
- Rendering of page images (PNG) with memory and dimension limits for downstream vision
- Deterministic heuristic document classification (COI / ACORD 25 vs Endorsements vs Dec pages)
- Identification of scanned/raster documents (requires_ocr state) without false compliance failures
"""

from __future__ import annotations

import hashlib
import io
import re
from typing import List, Tuple

import pymupdf  # PyMuPDF

from app.schemas.insurance import BoundingBox, DocumentType
from app.schemas.pdf import (
    DocumentProcessingStatus,
    PDFDocumentInspection,
    PDFPageInspection,
    PDFTextSpan,
)


# Security limits per SECURITY.md and PRODUCT_SPEC.md
MAX_PDF_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB
MAX_PAGE_COUNT = 100                    # Limit excessive pages
MAX_RENDER_DIMENSION_PX = 4096          # Max rasterized pixel dimension (prevent memory bombs)
PDF_MAGIC_BYTES = b"%PDF"


class PDFProcessingError(ValueError):
    """Raised when a PDF fails security or structure validation."""
    pass


def validate_pdf_bytes(pdf_bytes: bytes, max_size_bytes: int = MAX_PDF_SIZE_BYTES) -> None:
    """Validate raw bytes for basic PDF safety before attempting deeper parsing.
    
    Raises:
        PDFProcessingError if file is empty, too large, or lacks PDF magic bytes.
    """
    if not pdf_bytes:
        raise PDFProcessingError("Uploaded document is empty (0 bytes).")

    if len(pdf_bytes) > max_size_bytes:
        raise PDFProcessingError(
            f"File size exceeds limit: {len(pdf_bytes):,} bytes > {max_size_bytes:,} bytes."
        )

    if not pdf_bytes.startswith(PDF_MAGIC_BYTES):
        raise PDFProcessingError("File does not start with valid PDF magic bytes (%PDF).")


def classify_document_text(full_text: str) -> Tuple[DocumentType, float, str]:
    """Classify document type using deterministic domain heuristics on text content.
    
    Returns:
        (document_type, confidence, explanation)
    """
    normalized = re.sub(r"\s+", " ", full_text).upper()

    # If completely empty text layer
    if not normalized.strip():
        return (
            DocumentType.UNKNOWN,
            0.0,
            "Document contains no readable digital text layer (scanned or image-only).",
        )

    # ACORD 25 / Certificate of Insurance patterns
    if "CERTIFICATE OF LIABILITY INSURANCE" in normalized or "ACORD 25" in normalized:
        return (
            DocumentType.COI,
            0.98,
            "Matched standard ACORD 25 Certificate of Liability Insurance headers.",
        )

    if "CERTIFICATE OF INSURANCE" in normalized and (
        "PRODUCER" in normalized or "INSURED" in normalized or "COVERAGES" in normalized
    ):
        return (
            DocumentType.COI,
            0.90,
            "Matched generic Certificate of Insurance layout with Producer/Coverage blocks.",
        )

    # Endorsement patterns
    endorsement_indicators = [
        "ADDITIONAL INSURED",
        "WAIVER OF OUR RIGHT TO RECOVER",
        "WAIVER OF SUBROGATION",
        "PRIMARY AND NON-CONTRIBUTORY",
        "PRIMARY AND NONCONTRIBUTORY",
        "THIS ENDORSEMENT CHANGES THE POLICY",
        "POLICY ENDORSEMENT",
        "CG 20 10",
        "CG 20 38",
        "CG 20 33",
        "CG 20 37",
        "CG 24 04",
    ]
    matched_endorsements = [ind for ind in endorsement_indicators if ind in normalized]
    if matched_endorsements:
        return (
            DocumentType.ENDORSEMENT,
            0.92,
            f"Matched endorsement wording and forms: {matched_endorsements[:3]}.",
        )

    # Policy declaration page patterns
    if "COMMON POLICY DECLARATIONS" in normalized or "DECLARATION PAGE" in normalized or "DECLARATIONS" in normalized:
        if "POLICY NUMBER" in normalized and ("NAMED INSURED" in normalized or "ITEM 1" in normalized):
            return (
                DocumentType.POLICY_DEC_PAGE,
                0.88,
                "Matched Common Policy Declarations structure.",
            )

    return (
        DocumentType.UNKNOWN,
        0.30,
        "No conclusive COI, endorsement, or declaration headers identified in text.",
    )


class PDFProcessor:
    """Engine for parsing, rendering, and inspecting PDF documents."""

    def __init__(
        self,
        render_dpi: int = 150,
        max_page_count: int = MAX_PAGE_COUNT,
        max_render_dimension_px: int = MAX_RENDER_DIMENSION_PX,
    ):
        self.render_dpi = render_dpi
        self.max_page_count = max_page_count
        self.max_render_dimension_px = max_render_dimension_px

    def process_pdf(
        self,
        pdf_bytes: bytes,
        document_id: str,
        filename: str = "document.pdf",
        render_images: bool = False,
    ) -> PDFDocumentInspection:
        """Process PDF bytes into a structured inspection model.
        
        Args:
            pdf_bytes: Raw file content
            document_id: Unique tracking ID
            filename: Original filename
            render_images: If True, rasterizes each page to PNG bytes for AI ingestion
            
        Returns:
            PDFDocumentInspection with page details, text spans, and classification
        """
        validate_pdf_bytes(pdf_bytes)

        sha256_hash = hashlib.sha256(pdf_bytes).hexdigest()

        try:
            doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        except Exception as exc:
            raise PDFProcessingError(f"Corrupt or malformed PDF file could not be opened: {exc}") from exc

        try:
            # Check for encryption / password protection
            if getattr(doc, "is_encrypted", False) and getattr(doc, "needs_pass", False):
                raise PDFProcessingError(
                    "Password-protected or encrypted PDF documents cannot be inspected without credentials."
                )

            page_count = len(doc)
            if page_count > self.max_page_count:
                raise PDFProcessingError(
                    f"Document page count ({page_count}) exceeds maximum allowed limit ({self.max_page_count})."
                )

            inspected_pages: List[PDFPageInspection] = []
            combined_text_list: List[str] = []
            processing_errors: List[str] = []

            for page_idx in range(page_count):
                user_page_number = page_idx + 1  # Strictly 1-based user facing

                try:
                    page = doc[page_idx]
                    rect = page.rect  # Account for rotation (rect gives effective page bounds)
                    width = float(rect.width)
                    height = float(rect.height)
                    rotation = int(page.rotation) % 360

                    # Extract plain text
                    page_text = page.get_text("text") or ""
                    combined_text_list.append(page_text)

                    # Extract positional spans with normalized bounding boxes
                    spans: List[PDFTextSpan] = []
                    blocks = page.get_text("blocks") or []
                    for b in blocks:
                        # b: (x0, y0, x1, y1, text, block_no, block_type)
                        if len(b) >= 5 and b[4].strip():
                            b_text = b[4].strip()
                            bx0, by0, bx1, by1 = b[0], b[1], b[2], b[3]
                            if width > 0 and height > 0:
                                norm_x = max(0.0, min(1.0, bx0 / width))
                                norm_y = max(0.0, min(1.0, by0 / height))
                                norm_w = max(0.0, min(1.0, (bx1 - bx0) / width))
                                norm_h = max(0.0, min(1.0, (by1 - by0) / height))
                                spans.append(
                                    PDFTextSpan(
                                        text=b_text,
                                        bounding_box=BoundingBox(
                                            x=norm_x,
                                            y=norm_y,
                                            width=norm_w,
                                            height=norm_h,
                                        ),
                                    )
                                )

                    # Optionally render page image (PNG) with memory/dimension safeguards
                    png_bytes = None
                    if render_images:
                        zoom = self.render_dpi / 72.0
                        matrix = pymupdf.Matrix(zoom, zoom)
                        pix = page.get_pixmap(matrix=matrix, alpha=False)
                        if (
                            pix.width > self.max_render_dimension_px
                            or pix.height > self.max_render_dimension_px
                        ):
                            raise ValueError(
                                f"Page render dimensions ({pix.width}x{pix.height}) exceed maximum allowed "
                                f"dimension limit of {self.max_render_dimension_px}px."
                            )
                        png_bytes = pix.tobytes("png")

                    inspected_pages.append(
                        PDFPageInspection(
                            page_number=user_page_number,
                            width_pts=width,
                            height_pts=height,
                            rotation=rotation,
                            text_content=page_text,
                            spans=spans,
                            rendered_image_png=png_bytes,
                        )
                    )

                except Exception as page_exc:
                    err_msg = f"Error processing page {user_page_number}: {str(page_exc)}"
                    processing_errors.append(err_msg)
                    # Surface page failure explicitly rather than silently dropping
                    inspected_pages.append(
                        PDFPageInspection(
                            page_number=user_page_number,
                            width_pts=0.0,
                            height_pts=0.0,
                            rotation=0,
                            text_content="",
                            spans=[],
                            rendered_image_png=None,
                            error_message=err_msg,
                        )
                    )

            combined_text = "\n".join(combined_text_list)
            is_scanned = not bool(combined_text.strip())

            # Determine processing status
            if processing_errors:
                if len(processing_errors) == page_count:
                    status = DocumentProcessingStatus.FAILED
                else:
                    status = DocumentProcessingStatus.PARTIAL_SUCCESS
            elif is_scanned:
                status = DocumentProcessingStatus.REQUIRES_OCR
            else:
                status = DocumentProcessingStatus.PROCESSED

            doc_type, confidence, reason = classify_document_text(combined_text)

            return PDFDocumentInspection(
                document_id=document_id,
                filename=filename,
                file_size_bytes=len(pdf_bytes),
                content_hash_sha256=sha256_hash,
                page_count=page_count,
                processing_status=status,
                is_scanned=is_scanned,
                classified_type=doc_type,
                classification_confidence=confidence,
                classification_reason=reason,
                pages=inspected_pages,
                processing_errors=processing_errors,
            )

        finally:
            doc.close()
