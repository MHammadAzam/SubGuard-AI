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
    PageType,
    PDFDocumentInspection,
    PDFPageInspection,
    PDFTextSpan,
)


# Internal processor upper bound (supports high-DPI multi-page scans up to 25 MiB);
# HTTP uploads enforce stricter 15 MiB default via DocumentReviewService(max_pdf_size_bytes=...).
MAX_PDF_SIZE_BYTES = 25 * 1024 * 1024  # 25 MiB processor ceiling
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


def classify_pdf_page(page: pymupdf.Page) -> Tuple[PageType, bool, bool, float, dict]:
    """Classify an individual PDF page using multiple orthogonal signals.

    Signals analyzed:
    - Non-whitespace character count and text density (characters / unit area)
    - Text block and span structure
    - Printable character ratio (detects corrupt/garbled font encodings)
    - Average token length (detects OCR noise vs coherent English text)
    - Image coverage ratio and full-page raster scan detection
    - Page dimensions, orientation, and aspect ratio

    Returns:
        (page_type, is_scanned, requires_ocr, confidence, signals_dict)
    """
    rect = page.rect
    width = float(rect.width)
    height = float(rect.height)
    page_area = max(1.0, width * height)
    rotation = int(page.rotation) % 360

    page_text = page.get_text("text") or ""
    non_ws_text = re.sub(r"\s+", "", page_text)
    char_count = len(non_ws_text)
    text_density = (char_count / page_area) * 1000.0

    blocks = page.get_text("blocks") or []
    text_blocks = [b for b in blocks if len(b) >= 5 and b[4].strip() and (len(b) < 7 or b[6] == 0)]
    num_text_blocks = len(text_blocks)

    printable_chars = len(re.findall(r"[A-Za-z0-9\.,;:\-\(\)\$%/&]", page_text))
    printable_ratio = (printable_chars / max(1, char_count)) if char_count > 0 else 0.0

    words = [w.strip() for w in page_text.split() if w.strip()]
    word_count = len(words)
    avg_word_len = (sum(len(w) for w in words) / max(1, word_count)) if word_count > 0 else 0.0

    images = page.get_images(full=True)
    num_images = len(images)

    image_rects = []
    for img_info in images:
        xref = img_info[0]
        try:
            rects = page.get_image_rects(xref)
            image_rects.extend(rects)
        except Exception:
            pass

    total_image_area = sum(r.width * r.height for r in image_rects)
    image_coverage = min(1.0, total_image_area / page_area)
    has_full_page_image = (
        image_coverage >= 0.65
        or any(r.width >= width * 0.75 and r.height >= height * 0.75 for r in image_rects)
    )

    signals = {
        "char_count": char_count,
        "word_count": word_count,
        "text_density": round(text_density, 3),
        "num_text_blocks": num_text_blocks,
        "printable_ratio": round(printable_ratio, 3),
        "avg_word_len": round(avg_word_len, 2),
        "num_images": num_images,
        "image_coverage": round(image_coverage, 3),
        "has_full_page_image": has_full_page_image,
        "page_dimensions": [round(width, 1), round(height, 1)],
        "rotation": rotation,
    }

    # Decision tree using multi-signal heuristics
    # 1. Pure scanned: no text or negligible non-alphanumeric noise, image layer present or blank
    if char_count == 0 or (char_count < 10 and printable_chars < 5):
        return (
            PageType.SCANNED,
            True,
            True,
            0.99 if num_images > 0 else 0.90,
            signals,
        )

    # 2. Scanned with full-page raster image and minimal/noisy text (e.g. low-res scan with margin stamp)
    if has_full_page_image and char_count < 80:
        return (
            PageType.SCANNED,
            True,
            True,
            0.95,
            signals,
        )

    # 3. Uncertain / low quality: degraded font encoding or scrambled text
    if (char_count >= 20 and printable_ratio < 0.50) or (word_count >= 5 and (avg_word_len < 1.8 or avg_word_len > 35.0)):
        return (
            PageType.UNCERTAIN_LOW_QUALITY,
            True,
            True,
            0.70,
            signals,
        )

    # 4. Sparse digital page: clean digital page with low word count (e.g. signature page, brief rider)
    # Characterized by high printable ratio, normal word length, and NO dominant full-page raster scan
    if char_count < 120 and not has_full_page_image and printable_ratio >= 0.80:
        return (
            PageType.SPARSE_DIGITAL,
            False,
            False,
            0.92,
            signals,
        )

    # 5. Native digital page: rich text layer, structured layout
    return (
        PageType.DIGITAL,
        False,
        False,
        0.98 if char_count > 200 else 0.88,
        signals,
    )


class PDFProcessor:
    """Engine for parsing, rendering, and inspecting PDF documents."""

    def __init__(
        self,
        render_dpi: int = 150,
        max_page_count: int = MAX_PAGE_COUNT,
        max_render_dimension_px: int = MAX_RENDER_DIMENSION_PX,
        max_pdf_size_bytes: int = MAX_PDF_SIZE_BYTES,
    ):
        self.render_dpi = render_dpi
        self.max_page_count = max_page_count
        self.max_render_dimension_px = max_render_dimension_px
        self.max_pdf_size_bytes = max_pdf_size_bytes

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
        validate_pdf_bytes(pdf_bytes, max_size_bytes=self.max_pdf_size_bytes)

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

                    # Classify page independently using multi-signal heuristics
                    p_type, p_is_scanned, p_requires_ocr, p_conf, p_signals = classify_pdf_page(page)

                    # Render page image (PNG) if requested OR if page requires OCR/vision processing
                    png_bytes = None
                    if render_images or p_requires_ocr:
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
                            page_type=p_type,
                            is_scanned=p_is_scanned,
                            requires_ocr=p_requires_ocr,
                            classification_confidence=p_conf,
                            classification_signals=p_signals,
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
                            page_type=PageType.UNCERTAIN_LOW_QUALITY,
                            is_scanned=True,
                            requires_ocr=True,
                            classification_confidence=0.0,
                            classification_signals={},
                            rendered_image_png=None,
                            error_message=err_msg,
                        )
                    )

            combined_text = "\n".join(combined_text_list)

            # Document-level multi-page analysis
            has_scanned = any(p.is_scanned for p in inspected_pages)
            has_digital = any(not p.is_scanned for p in inspected_pages)
            is_mixed = has_scanned and has_digital
            all_scanned = all(p.is_scanned for p in inspected_pages) if inspected_pages else False
            scanned_pages = [p.page_number for p in inspected_pages if p.is_scanned]
            digital_pages = [p.page_number for p in inspected_pages if not p.is_scanned]

            # Determine processing status
            if processing_errors:
                if len(processing_errors) == page_count:
                    status = DocumentProcessingStatus.FAILED
                else:
                    status = DocumentProcessingStatus.PARTIAL_SUCCESS
            elif any(p.requires_ocr for p in inspected_pages):
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
                is_scanned=all_scanned or has_scanned,
                is_mixed=is_mixed,
                scanned_page_numbers=scanned_pages,
                digital_page_numbers=digital_pages,
                classified_type=doc_type,
                classification_confidence=confidence,
                classification_reason=reason,
                pages=inspected_pages,
                processing_errors=processing_errors,
            )

        finally:
            doc.close()
