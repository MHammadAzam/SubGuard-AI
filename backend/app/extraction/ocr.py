"""OCR Provider Interface and Implementations for SubGuard AI.

Provides:
- BaseOCRProvider: Abstract contract for OCR engines.
- TesseractOCRProvider: Production local OCR engine via Tesseract CLI with bounding boxes.
- MockOCRProvider: Deterministic mock OCR engine for offline testing.
"""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

from app.schemas.insurance import BoundingBox
from app.schemas.pdf import PDFPageInspection, PDFTextSpan


class OCRError(Exception):
    """Raised when an OCR engine fails."""
    pass


class TesseractNotFoundError(OCRError):
    """Raised when the Tesseract OCR executable cannot be found on the system."""
    pass


class TesseractExecutionError(OCRError):
    """Raised when Tesseract process execution fails."""
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


def _find_tesseract_binary(custom_path: Optional[str] = None) -> Optional[str]:
    """Locate the tesseract executable on Windows or POSIX systems."""
    if custom_path and Path(custom_path).is_file():
        return str(Path(custom_path).resolve())

    # Check environment variables
    env_cmd = os.environ.get("TESSERACT_CMD") or os.environ.get("TESSERACT_PATH")
    if env_cmd and Path(env_cmd).is_file():
        return str(Path(env_cmd).resolve())

    # Check system PATH
    which_cmd = shutil.which("tesseract")
    if which_cmd:
        return which_cmd

    # Standard Windows installation locations
    candidate_paths = [
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
        Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR" / "tesseract.exe",
    ]
    for p in candidate_paths:
        if p.is_file():
            return str(p.resolve())

    return None


class TesseractOCRProvider(BaseOCRProvider):
    """Local Tesseract OCR engine with normalized bounding boxes and confidence metadata.

    Requirements:
    - Local execution (no paid APIs, no cloud dependency).
    - Configurable language support.
    - Normalized bounding boxes (x, y, w, h in 0.0 - 1.0 range).
    - Explicit failure reporting (does not silently substitute mock data).
    """

    def __init__(
        self,
        tesseract_cmd: Optional[str] = None,
        lang: str = "eng",
        timeout_seconds: int = 45,
    ):
        self.tesseract_cmd = _find_tesseract_binary(tesseract_cmd)
        self.lang = lang
        self.timeout_seconds = timeout_seconds

    @property
    def provider_name(self) -> str:
        return "tesseract"

    def is_available(self) -> bool:
        """Check if Tesseract binary is located and executable."""
        if not self.tesseract_cmd:
            return False
        try:
            res = subprocess.run(
                [self.tesseract_cmd, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
            return res.returncode == 0
        except Exception:
            return False

    def perform_ocr_on_pages(
        self,
        pages: List[PDFPageInspection],
    ) -> List[PDFPageInspection]:
        """Perform local Tesseract OCR on each page in pages."""
        if not self.tesseract_cmd or not Path(self.tesseract_cmd).is_file():
            raise TesseractNotFoundError(
                f"Tesseract OCR executable not found. "
                f"Configured path: '{self.tesseract_cmd}'. "
                f"Please install Tesseract OCR on Windows (e.g. from UB-Mannheim/tesseract/wiki "
                f"or 'winget install UB-Mannheim.TesseractOCR') or set the TESSERACT_CMD environment variable."
            )

        ocr_pages: List[PDFPageInspection] = []

        for page in pages:
            if not page.rendered_image_png:
                raise OCRError(
                    f"Page {page.page_number} lacks rendered PNG raster image bytes required for OCR."
                )

            # Determine pixel dimensions from PNG header (IHDR chunk at byte offset 16)
            png_bytes = page.rendered_image_png
            img_w_px = max(1.0, float(int.from_bytes(png_bytes[16:20], "big")))
            img_h_px = max(1.0, float(int.from_bytes(png_bytes[20:24], "big")))

            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                input_png = tmp_path / f"page_{page.page_number}.png"
                output_base = tmp_path / f"ocr_{page.page_number}"
                output_tsv = tmp_path / f"ocr_{page.page_number}.tsv"

                input_png.write_bytes(png_bytes)

                cmd = [
                    self.tesseract_cmd,
                    str(input_png),
                    str(output_base),
                    "-l",
                    self.lang,
                    "--psm",
                    "6",
                    "tsv",
                ]

                try:
                    result = subprocess.run(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=self.timeout_seconds,
                    )
                except subprocess.TimeoutExpired as tex:
                    raise TesseractExecutionError(
                        f"Tesseract OCR timed out after {self.timeout_seconds}s on page {page.page_number}."
                    ) from tex
                except Exception as exc:
                    raise TesseractExecutionError(
                        f"Tesseract execution error on page {page.page_number}: {str(exc)}"
                    ) from exc

                if result.returncode != 0:
                    err_text = result.stderr.decode("utf-8", errors="replace").strip()
                    raise TesseractExecutionError(
                        f"Tesseract process returned error code {result.returncode} on page {page.page_number}: {err_text}"
                    )

                if not output_tsv.exists():
                    raise TesseractExecutionError(
                        f"Tesseract did not produce expected TSV output at '{output_tsv}' on page {page.page_number}."
                    )

                # Parse TSV output for word spans and normalized bounding boxes
                spans: List[PDFTextSpan] = []
                confidences: List[float] = []
                lines: List[str] = []
                current_line_words: List[str] = []
                last_line_id = None

                with open(output_tsv, "r", encoding="utf-8", errors="replace") as f:
                    reader = csv.DictReader(f, delimiter="\t")
                    for row in reader:
                        # level 5 corresponds to word level in Tesseract TSV
                        if row.get("level") != "5":
                            continue

                        word_text = (row.get("text") or "").strip()
                        if not word_text:
                            continue

                        line_id = (row.get("block_num"), row.get("par_num"), row.get("line_num"))
                        if last_line_id is not None and line_id != last_line_id:
                            if current_line_words:
                                lines.append(" ".join(current_line_words))
                                current_line_words = []
                        last_line_id = line_id
                        current_line_words.append(word_text)

                        try:
                            left = float(row.get("left", 0))
                            top = float(row.get("top", 0))
                            width = float(row.get("width", 0))
                            height = float(row.get("height", 0))
                            conf = float(row.get("conf", -1))
                        except (ValueError, TypeError):
                            continue

                        if conf >= 0:
                            confidences.append(conf / 100.0)

                        norm_x = max(0.0, min(1.0, left / img_w_px))
                        norm_y = max(0.0, min(1.0, top / img_h_px))
                        norm_w = max(0.0, min(1.0, width / img_w_px))
                        norm_h = max(0.0, min(1.0, height / img_h_px))

                        spans.append(
                            PDFTextSpan(
                                text=word_text,
                                bounding_box=BoundingBox(
                                    x=norm_x,
                                    y=norm_y,
                                    width=norm_w,
                                    height=norm_h,
                                ),
                            )
                        )

                if current_line_words:
                    lines.append(" ".join(current_line_words))

                full_text = "\n".join(lines)
                avg_conf = (sum(confidences) / max(1, len(confidences))) if confidences else 0.50

                updated_page = page.model_copy(
                    update={
                        "text_content": full_text,
                        "spans": spans,
                        "ocr_provider": self.provider_name,
                        "ocr_confidence": round(avg_conf, 3),
                        "is_scanned": True,
                        "requires_ocr": False,
                    }
                )
                ocr_pages.append(updated_page)

        return ocr_pages


class MockOCRProvider(BaseOCRProvider):
    """Mock OCR provider for deterministic test suites.

    Generates synthetic text and normalized bounding boxes without calling external tools.
    Supports simulating engine failures for testing downstream resilience.
    """

    def __init__(
        self,
        synthetic_text_by_page: Optional[dict[int, str]] = None,
        synthetic_spans_by_page: Optional[dict[int, List[PDFTextSpan]]] = None,
        should_fail: bool = False,
        failure_message: str = "Mock OCR simulated failure.",
        confidence_by_page: Optional[dict[int, float]] = None,
    ):
        self.synthetic_text_by_page = synthetic_text_by_page or {}
        self.synthetic_spans_by_page = synthetic_spans_by_page or {}
        self.should_fail = should_fail
        self.failure_message = failure_message
        self.confidence_by_page = confidence_by_page or {}

    @property
    def provider_name(self) -> str:
        return "mock_ocr"

    def perform_ocr_on_pages(
        self,
        pages: List[PDFPageInspection],
    ) -> List[PDFPageInspection]:
        if self.should_fail:
            raise OCRError(self.failure_message)

        ocr_pages: List[PDFPageInspection] = []
        for p in pages:
            default_text = (
                f"OCR EXTRACTED TEXT FOR PAGE {p.page_number}: ACORD 25 CERTIFICATE OF LIABILITY INSURANCE "
                f"PRODUCER: Reliable Insurance Agency "
                f"INSURED: Apex Electrical Contractors LLC "
                f"GENERAL LIABILITY EACH OCCURRENCE $1,000,000 GENERAL AGGREGATE $2,000,000"
            )
            ocr_text = self.synthetic_text_by_page.get(p.page_number, default_text)

            # Generate normalized spans if not explicitly provided
            if p.page_number in self.synthetic_spans_by_page:
                spans = self.synthetic_spans_by_page[p.page_number]
            else:
                words = ocr_text.split()
                spans = []
                for idx, w in enumerate(words):
                    col = idx % 5
                    row = idx // 5
                    spans.append(
                        PDFTextSpan(
                            text=w,
                            bounding_box=BoundingBox(
                                x=round(0.05 + col * 0.18, 3),
                                y=round(0.05 + row * 0.035, 3),
                                width=0.15,
                                height=0.025,
                            ),
                        )
                    )

            conf = self.confidence_by_page.get(p.page_number, 0.95)
            updated = p.model_copy(
                update={
                    "text_content": ocr_text,
                    "spans": spans,
                    "ocr_provider": self.provider_name,
                    "ocr_confidence": conf,
                    "is_scanned": True,
                    "requires_ocr": False,
                }
            )
            ocr_pages.append(updated)

        return ocr_pages
