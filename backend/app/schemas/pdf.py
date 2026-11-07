"""PDF processing models for document ingestion, page inspection, and classification."""

from __future__ import annotations

from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field

from app.schemas.insurance import BoundingBox, DocumentType


class DocumentProcessingStatus(str, Enum):
    """Processing state of an ingested document."""
    PROCESSED = "processed"          # Successfully parsed text and structure
    REQUIRES_OCR = "requires_ocr"    # Scanned / raster PDF with no usable digital text layer
    PARTIAL_SUCCESS = "partial_success" # Some pages succeeded, some encountered errors
    FAILED = "failed"                # Terminal processing error (corrupt, encrypted, etc.)


class PageType(str, Enum):
    """Classification of an individual PDF page."""
    DIGITAL = "digital"                          # Digitally generated text layer with good density
    SCANNED = "scanned"                          # Image-only / raster scan requiring OCR
    SPARSE_DIGITAL = "sparse_digital"            # Digital text but sparse content (e.g. signature page)
    UNCERTAIN_LOW_QUALITY = "uncertain_low_quality" # Low-confidence or degraded text requiring OCR assist


class PDFTextSpan(BaseModel):
    """Extracted text fragment with exact bounding box on the page."""
    text: str
    bounding_box: BoundingBox


class PDFPageInspection(BaseModel):
    """Inspected details for an individual document page."""
    page_number: int = Field(ge=1, description="1-indexed user-facing page number")
    width_pts: float = Field(description="Page width in typographic points")
    height_pts: float = Field(description="Page height in typographic points")
    rotation: int = Field(default=0, description="Page rotation in degrees (0, 90, 180, 270)")
    text_content: str = Field(description="Full text extracted from page")
    spans: List[PDFTextSpan] = Field(default_factory=list, description="Positional text fragments")
    page_type: PageType = Field(default=PageType.DIGITAL, description="Page classification: digital, scanned, sparse_digital, uncertain_low_quality")
    is_scanned: bool = Field(default=False, description="True if page is scanned/raster and requires OCR")
    requires_ocr: bool = Field(default=False, description="True if this page requires OCR")
    classification_confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Confidence of page classification")
    classification_signals: dict = Field(default_factory=dict, description="Multi-signal diagnostic metrics")
    ocr_provider: Optional[str] = Field(default=None, description="Name of OCR provider used on this page")
    ocr_confidence: Optional[float] = Field(default=None, description="Average OCR confidence for extracted text")
    rendered_image_png: Optional[bytes] = Field(
        default=None,
        repr=False,
        description="Rendered raster PNG bytes (used for downstream vision and OCR)"
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Error details if this specific page encountered a parsing/rendering issue"
    )


class PDFDocumentInspection(BaseModel):
    """Comprehensive inspection result for a submitted PDF file."""
    document_id: str
    filename: str
    file_size_bytes: int
    content_hash_sha256: str
    page_count: int
    processing_status: DocumentProcessingStatus = DocumentProcessingStatus.PROCESSED
    is_scanned: bool = Field(
        default=False,
        description="True if document has no usable text layer and requires OCR/multimodal vision"
    )
    is_mixed: bool = Field(
        default=False,
        description="True if document contains both digital and scanned pages"
    )
    scanned_page_numbers: List[int] = Field(
        default_factory=list,
        description="1-indexed page numbers identified as scanned/requiring OCR"
    )
    digital_page_numbers: List[int] = Field(
        default_factory=list,
        description="1-indexed page numbers identified as native digital"
    )
    classified_type: DocumentType
    classification_confidence: float = Field(ge=0.0, le=1.0)
    classification_reason: str
    pages: List[PDFPageInspection] = Field(default_factory=list)
    processing_errors: List[str] = Field(
        default_factory=list,
        description="List of errors or warnings encountered during processing"
    )
