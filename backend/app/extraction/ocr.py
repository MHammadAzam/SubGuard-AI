"""OCR Provider Interface and Mock implementation for scanned documents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional
from pydantic import BaseModel

from app.schemas.pdf import PDFPageInspection, PDFTextSpan


class OCRError(Exception):
    """Raised when an OCR engine fails."""
    pass


class BaseOCRProvider(ABC):
    """Abstract interface for OCR engines (Tesseract, Cloud Vision, etc.)."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        pass

    @abstractmethod
    def perform_ocr_on_pages(
        self,
        pages: List[PDFPageInspection],
    ) -> List[PDFPageInspection]:
        """Perform OCR on scanned pages, populating text_content and spans."""
        pass


class MockOCRProvider(BaseOCRProvider):
    """Mock OCR provider for test suites."""

    def __init__(self, synthetic_text_by_page: Optional[dict[int, str]] = None):
        self.synthetic_text_by_page = synthetic_text_by_page or {}

    @property
    def provider_name(self) -> str:
        return "mock_ocr"

    def perform_ocr_on_pages(
        self,
        pages: List[PDFPageInspection],
    ) -> List[PDFPageInspection]:
        ocr_pages: List[PDFPageInspection] = []
        for p in pages:
            ocr_text = self.synthetic_text_by_page.get(
                p.page_number,
                f"OCR EXTRACTED TEXT FOR PAGE {p.page_number}: ACORD 25 CERTIFICATE OF LIABILITY INSURANCE"
            )
            # Return updated page with OCR text
            updated = p.model_copy(update={"text_content": ocr_text})
            ocr_pages.append(updated)
        return ocr_pages
