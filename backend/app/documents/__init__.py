"""Package marker for documents module."""

from app.documents.processor import (
    MAX_PAGE_COUNT,
    MAX_PDF_SIZE_BYTES,
    MAX_RENDER_DIMENSION_PX,
    PDFProcessingError,
    PDFProcessor,
    classify_document_text,
    validate_pdf_bytes,
)
from app.schemas.pdf import (
    DocumentProcessingStatus,
    PDFDocumentInspection,
    PDFPageInspection,
    PDFTextSpan,
)

__all__ = [
    "MAX_PAGE_COUNT",
    "MAX_PDF_SIZE_BYTES",
    "MAX_RENDER_DIMENSION_PX",
    "DocumentProcessingStatus",
    "PDFDocumentInspection",
    "PDFPageInspection",
    "PDFProcessingError",
    "PDFProcessor",
    "PDFTextSpan",
    "classify_document_text",
    "validate_pdf_bytes",
]
