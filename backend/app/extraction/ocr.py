"""OCR Provider Interface and Implementations for SubGuard AI.

Provides:
- BaseOCRProvider: Abstract contract for OCR engines.
- TesseractOCRProvider: Production local OCR engine via Tesseract CLI with bounding boxes.
- MockOCRProvider: Deterministic mock OCR engine for offline testing.
"""

from __future__ import annotations

import csv
import io
import math
import os
import re
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pymupdf

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
    if custom_path:
        if Path(custom_path).is_file():
            return str(Path(custom_path).resolve())
        return None

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


# Minimum OSD confidence required to accept orientation correction.
# Empirically, genuine text documents yield confidence >= 6.0.
# Threshold 0.50 reliably rejects false rotations (e.g. number-only tables which can yield ~0.21).
OSD_MIN_CONFIDENCE: float = 0.50


def _detect_orientation_osd(
    tesseract_cmd: str,
    image_bytes: bytes,
    timeout_seconds: int = 15,
) -> Tuple[int, float]:
    """Detect page rotation using Tesseract OSD (--psm 0).

    Returns:
        (rot_deg, confidence): rot_deg is the clockwise rotation needed to make
        the image upright (0, 90, 180, 270). If detection fails or confidence
        is below OSD_MIN_CONFIDENCE, returns (0, conf).
    """
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(image_bytes)
            tf_path = tf.name

        try:
            cmd = [tesseract_cmd, tf_path, "stdout", "--psm", "0"]
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
            )
        finally:
            try:
                os.unlink(tf_path)
            except OSError:
                pass

        if res.returncode != 0:
            return 0, 0.0

        out_text = res.stdout.decode("utf-8", errors="replace")
        rot_deg = 0
        conf = 0.0
        for line in out_text.splitlines():
            line_str = line.strip()
            if line_str.startswith("Rotate:"):
                try:
                    rot_deg = int(line_str.split(":", 1)[1].strip())
                except ValueError:
                    rot_deg = 0
            elif line_str.startswith("Orientation confidence:"):
                try:
                    conf = float(line_str.split(":", 1)[1].strip())
                except ValueError:
                    conf = 0.0

        if rot_deg in (90, 180, 270) and conf >= OSD_MIN_CONFIDENCE:
            return rot_deg, conf
        return 0, conf
    except Exception:
        return 0, 0.0


def _pixmap_to_grayscale(pixmap: pymupdf.Pixmap) -> Optional[np.ndarray]:
    """Convert a PyMuPDF pixmap to an 8-bit single-channel 2D numpy array.

    Handles Gray (n=1), Gray+Alpha (n=2), RGB (n=3), RGBA (n=4), and CMYK/other.
    In PyMuPDF, color samples with alpha are stored premultiplied by alpha.
    Compositing premultiplied samples over a solid white background (255) uses:
        C_out = C_premult + (255.0 - alpha)
    Non-Gray/non-RGB pixmaps (e.g. DeviceCMYK) are converted via PyMuPDF's
    color engine to ensure accurate perceptual luminance.
    """
    try:
        w, h = pixmap.width, pixmap.height
        if w < 1 or h < 1:
            return None

        # Convert non-standard colorspaces (e.g. DeviceCMYK) via PyMuPDF
        cs = getattr(pixmap, "colorspace", None)
        if cs is not None and cs.name not in ("DeviceGray", "DeviceRGB"):
            target_cs = pymupdf.csRGB if getattr(pixmap, "alpha", 0) else pymupdf.csGRAY
            pixmap = pymupdf.Pixmap(target_cs, pixmap)

        samples = np.frombuffer(pixmap.samples, dtype=np.uint8)
        n = pixmap.n

        if n == 1:
            return samples.reshape((h, w))
        elif n == 2:
            # Grayscale + Alpha (premultiplied): composite over solid white background (255)
            arr = samples.reshape((h, w, 2)).astype(np.float32)
            gray = arr[:, :, 0]
            alpha = arr[:, :, 1]
            comp = gray + (255.0 - alpha)
            return np.clip(np.round(comp), 0, 255).astype(np.uint8)
        elif n == 3:
            return samples.reshape((h, w, 3)).mean(axis=2).astype(np.uint8)
        elif n == 4:
            arr = samples.reshape((h, w, 4))
            if getattr(pixmap, "alpha", 0):
                # RGBA (premultiplied): composite RGB over solid white background (255)
                rgb = arr[:, :, :3].astype(np.float32)
                alpha = arr[:, :, 3:4].astype(np.float32)
                comp = rgb + (255.0 - alpha)
                return np.clip(np.round(comp.mean(axis=2)), 0, 255).astype(np.uint8)
            else:
                return arr[:, :, :3].astype(np.float32).mean(axis=2).astype(np.uint8)
        else:
            arr = samples.reshape((h, w, n))
            num_channels = n - 1 if getattr(pixmap, "alpha", 0) else n
            return arr[:, :, :num_channels].astype(np.float32).mean(axis=2).astype(np.uint8)
    except Exception:
        return None


def _detect_skew_angle(
    pixmap: pymupdf.Pixmap,
    max_angle: float = 15.0,
    coarse_step: float = 0.5,
    fine_step: float = 0.1,
    min_ink_pixels: int = 100,
) -> float:
    """Detect skew angle in degrees using vertical shear projection profile.

    Sweeps candidate angles in [-max_angle, max_angle], shears ink coordinates,
    and finds the angle maximizing adjacent bin squared differences (line alignment).
    A fine-grained sweep (+/- 0.5 deg around peak) refines the angle.

    Returns:
        Detected skew angle in degrees (positive = clockwise tilt).
        Returns 0.0 if ink count is below min_ink_pixels or peak is not prominent.
    """
    try:
        w, h = pixmap.width, pixmap.height
        if w < 10 or h < 10:
            return 0.0

        gray = _pixmap_to_grayscale(pixmap)
        if gray is None:
            return 0.0

        ink_y, ink_x = np.where(gray < 180)
        ink_count = len(ink_y)
        if ink_count < min_ink_pixels:
            return 0.0

        # Subsample for speed if document has extremely dense rasterization
        if ink_count > 60000:
            idx = np.random.RandomState(42).choice(ink_count, 60000, replace=False)
            ink_y = ink_y[idx]
            ink_x = ink_x[idx]

        ink_y_f = ink_y.astype(np.float64)
        ink_x_f = ink_x.astype(np.float64)

        best_score = -1.0
        best_angle = 0.0
        scores: dict[float, float] = {}

        coarse_angles = np.arange(-max_angle, max_angle + coarse_step / 2.0, coarse_step)
        for ang in coarse_angles:
            rad = math.radians(ang)
            y_proj = ink_y_f - ink_x_f * math.tan(rad)
            y_bins = np.round(y_proj).astype(np.int32)
            counts = np.bincount(y_bins - np.min(y_bins))
            diff2 = float(np.sum(np.diff(counts) ** 2))
            scores[round(float(ang), 2)] = diff2
            if diff2 > best_score:
                best_score = diff2
                best_angle = float(ang)

        base_score = scores.get(0.0, 1.0)
        prominence = best_score / (base_score + 1e-6)

        # Require significant peak prominence (15% above 0 deg) and non-negligible angle
        if prominence < 1.15 or abs(best_angle) < 0.5:
            return 0.0

        # Fine sweep around candidate peak
        fine_best_score = best_score
        fine_angle = best_angle
        fine_angles = np.arange(best_angle - coarse_step, best_angle + coarse_step + fine_step / 2.0, fine_step)
        for ang in fine_angles:
            if abs(ang) > max_angle:
                continue
            rad = math.radians(ang)
            y_proj = ink_y_f - ink_x_f * math.tan(rad)
            y_bins = np.round(y_proj).astype(np.int32)
            counts = np.bincount(y_bins - np.min(y_bins))
            diff2 = float(np.sum(np.diff(counts) ** 2))
            if diff2 > fine_best_score:
                fine_best_score = diff2
                fine_angle = float(ang)

        return round(float(fine_angle), 2)
    except Exception:
        return 0.0


def _deskew_pixmap(
    pixmap: pymupdf.Pixmap,
    skew_angle: float,
) -> Tuple[pymupdf.Pixmap, pymupdf.Matrix]:
    """Rotate pixmap by -skew_angle using an in-memory PyMuPDF page.

    Returns:
        (deskewed_pixmap, deskew_matrix)
    """
    temp_doc = pymupdf.open()
    temp_page = temp_doc.new_page(width=pixmap.width, height=pixmap.height)
    temp_page.insert_image(temp_page.rect, pixmap=pixmap)
    deskew_mat = pymupdf.Matrix(-skew_angle)
    deskew_pix = temp_page.get_pixmap(matrix=deskew_mat)
    temp_doc.close()
    return deskew_pix, deskew_mat


def _map_corners_back(
    corners: List[Tuple[float, float]],
    deskew_pix: pymupdf.Pixmap,
    deskew_mat: Optional[pymupdf.Matrix],
    upright_w: float,
    upright_h: float,
    rotation_applied: int,
) -> Tuple[float, float, float, float]:
    """Map pixel corner coordinates back through inverse deskew and inverse rotation.

    Returns:
        (norm_x, norm_y, norm_w, norm_h) in [0.0, 1.0] original page space.
    """
    pts_upright: List[Tuple[float, float]] = []
    for px, py in corners:
        if deskew_mat is not None:
            pt_u = pymupdf.Point(px + deskew_pix.x, py + deskew_pix.y) * ~deskew_mat
            pts_upright.append((pt_u.x / upright_w, pt_u.y / upright_h))
        else:
            pts_upright.append((px / upright_w, py / upright_h))

    pts_orig: List[Tuple[float, float]] = []
    for u1, v1 in pts_upright:
        if rotation_applied == 0:
            u0, v0 = u1, v1
        elif rotation_applied == 90:
            u0, v0 = v1, 1.0 - u1
        elif rotation_applied == 180:
            u0, v0 = 1.0 - u1, 1.0 - v1
        elif rotation_applied == 270:
            u0, v0 = 1.0 - v1, u1
        else:
            u0, v0 = u1, v1
        pts_orig.append((u0, v0))

    u_vals = [p[0] for p in pts_orig]
    v_vals = [p[1] for p in pts_orig]
    min_u = max(0.0, min(1.0, min(u_vals)))
    max_u = max(0.0, min(1.0, max(u_vals)))
    min_v = max(0.0, min(1.0, min(v_vals)))
    max_v = max(0.0, min(1.0, max(v_vals)))

    norm_x = min_u
    norm_y = min_v
    norm_w = max(0.0, min(1.0 - norm_x, max_u - min_u))
    norm_h = max(0.0, min(1.0 - norm_y, max_v - min_v))

    return round(norm_x, 4), round(norm_y, 4), round(norm_w, 4), round(norm_h, 4)


def analyze_page_layout(pixmap: pymupdf.Pixmap, default_psm: str = "6") -> Tuple[str, dict]:
    """Analyze page visual layout and select an optimal Tesseract Page Segmentation Mode (PSM).

    Candidate modes:
    - 3: Fully automatic page segmentation (no OSD). Selected for multi-column documents
         and tables with grid lines, preventing horizontal line merging across columns/cells.
    - 6: Single uniform text block. Safe baseline default for standard single-column text,
         certificates, uniform clauses, and ambiguous layouts.
    - 11: Sparse text. Selected when text ink is minimal and isolated across wide blank areas.

    Observable characteristics inspected:
    - Total dark ink density and pixel count (using consistent grayscale conversion).
    - Presence of table grid ruling lines (horizontal >= 80px, vertical >= 60px).
    - Vertical ink projection and central column gutter identification.
    - Horizontal and vertical whitespace distribution.

    Fail-safe: Returns (default_psm, {"layout": "fallback", ...}) on any unexpected error.
    """
    try:
        w, h = pixmap.width, pixmap.height
        if w < 10 or h < 10:
            return default_psm, {"layout": "fallback", "reason": "image_too_small"}

        gray = _pixmap_to_grayscale(pixmap)
        if gray is None:
            return default_psm, {"layout": "fallback", "reason": "grayscale_failed"}

        ink_mask = (gray < 180)
        ink_count = int(np.sum(ink_mask))
        total_pixels = max(1.0, float(w * h))
        ink_density = ink_count / total_pixels

        # Blank or near-empty page (resolution-scaled threshold)
        blank_thresh = max(50, int(0.00008 * total_pixels))
        if ink_count < blank_thresh:
            return default_psm, {
                "layout": "blank",
                "density": round(ink_density, 5),
                "ink_count": ink_count,
                "selected_psm": default_psm,
            }

        # Resolution-adaptive line detection:
        # Keep row/col steps small so thin ruling lines (1-3px) are reliably detected across 100-300 DPI.
        stride_x = max(5, int(w * 0.008))
        stride_y = max(5, int(h * 0.008))
        step_y = max(1, int(h * 0.0015))
        step_x = max(1, int(w * 0.0015))

        # Horizontal line requires length >= 6% of page width
        K_h = max(4, int((w * 0.06) / stride_x))
        sampled_h = ink_mask[::step_y, ::stride_x]
        if sampled_h.shape[1] >= K_h:
            h_lines = np.ones((sampled_h.shape[0], sampled_h.shape[1] - K_h + 1), dtype=bool)
            for i in range(K_h):
                h_lines &= sampled_h[:, i : sampled_h.shape[1] - K_h + 1 + i]
            h_line_rows = np.any(h_lines, axis=1)
            h_line_count = int(np.sum(h_line_rows[1:] & ~h_line_rows[:-1])) + (1 if h_line_rows[0] else 0)
        else:
            h_line_count = 0

        # Vertical line requires length >= 4% of page height
        K_v = max(4, int((h * 0.04) / stride_y))
        sampled_v = ink_mask[::stride_y, ::step_x]
        if sampled_v.shape[0] >= K_v:
            v_lines = np.ones((sampled_v.shape[0] - K_v + 1, sampled_v.shape[1]), dtype=bool)
            for i in range(K_v):
                v_lines &= sampled_v[i : sampled_v.shape[0] - K_v + 1 + i, :]
            v_line_cols = np.any(v_lines, axis=0)
            v_line_count = int(np.sum(v_line_cols[1:] & ~v_line_cols[:-1])) + (1 if v_line_cols[0] else 0)
        else:
            v_line_count = 0

        has_table_grid = bool(h_line_count >= 3 and v_line_count >= 2)

        # Vertical ink projection across page width to identify column gutters
        col_ink = np.sum(ink_mask, axis=0)
        window = max(5, int(w * 0.02))
        kernel = np.ones(window) / window
        smoothed = np.convolve(col_ink, kernel, mode="same")

        mid_start = int(w * 0.40)
        mid_end = int(w * 0.60)
        mid_region = smoothed[mid_start:mid_end]

        left_ink = np.sum(col_ink[int(w * 0.08) : mid_start])
        right_ink = np.sum(col_ink[mid_end : int(w * 0.92)])

        min_mid = np.min(mid_region) if len(mid_region) > 0 else 9999.0
        max_side = (
            max(np.max(smoothed[int(w * 0.08) : mid_start]), np.max(smoothed[mid_end : int(w * 0.92)]))
            if max(left_ink, right_ink) > 0
            else 1.0
        )

        min_text_ink = max(1000, int(0.003 * total_pixels))
        # Column gutter analysis across page width to identify multi-column documents
        # True multi-column documents have substantial text in both columns (relative to page size)
        is_multi_column = bool(
            ink_count >= min_text_ink
            and ink_density >= 0.004
            and not has_table_grid
            and min_mid < 0.25 * max_side
            and left_ink > 0.15 * ink_count
            and right_ink > 0.15 * ink_count
        )

        is_sparse = bool((ink_count < min_text_ink or ink_density < 0.004) and not has_table_grid and not is_multi_column)

        if is_multi_column:
            selected = "3"
            layout = "multi_column"
        elif has_table_grid:
            # Tables with grid lines require PSM 3 to avoid dropping cell contents
            selected = "3"
            layout = "table"
        elif is_sparse:
            selected = "11"
            layout = "sparse"
        else:
            selected = default_psm
            layout = "uniform_block"

        details = {
            "layout": layout,
            "selected_psm": selected,
            "ink_count": ink_count,
            "ink_density": round(ink_density, 5),
            "has_table_grid": has_table_grid,
            "h_lines": h_line_count,
            "v_lines": v_line_count,
            "is_multi_column": is_multi_column,
        }
        return selected, details
    except Exception as exc:
        return default_psm, {"layout": "fallback", "error": str(exc), "selected_psm": default_psm}


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
        default_psm: str = "6",
        adaptive_psm: bool = True,
    ):
        self.tesseract_cmd = _find_tesseract_binary(tesseract_cmd)
        self.lang = lang
        self.timeout_seconds = timeout_seconds
        self.default_psm = default_psm
        self.adaptive_psm = adaptive_psm

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

            png_bytes = page.rendered_image_png
            orig_pix = pymupdf.Pixmap(png_bytes)
            orig_w_px = max(1.0, float(orig_pix.width))
            orig_h_px = max(1.0, float(orig_pix.height))

            # Step 1: Detect ink density using consistent grayscale conversion
            gray = _pixmap_to_grayscale(orig_pix)
            ink_count = int(np.sum(gray < 180)) if gray is not None else 0

            rotation_applied = 0
            deskew_applied = 0.0
            deskew_mat = None

            if ink_count >= 100:
                # Step 2a: Check initial horizontal skew in [-15, 15] degrees.
                # Pre-deskewing level lines prevents steep tilt from confusing Tesseract OSD.
                skew_initial = _detect_skew_angle(orig_pix)
                initial_deskew_mat = None
                if abs(skew_initial) >= 0.5 and abs(skew_initial) <= 15.0:
                    level_pix, initial_deskew_mat = _deskew_pixmap(orig_pix, skew_initial)
                    osd_bytes = level_pix.tobytes("png")
                else:
                    level_pix = orig_pix
                    osd_bytes = png_bytes

                # Step 2b: Orientation detection via OSD on level_pix
                rot_osd, conf_osd = _detect_orientation_osd(
                    self.tesseract_cmd,
                    osd_bytes,
                    timeout_seconds=min(15, self.timeout_seconds),
                )

                if rot_osd in (90, 180, 270) and conf_osd >= OSD_MIN_CONFIDENCE:
                    # Quadrant rotation applied to orig_pix
                    rot_map = {
                        90: orig_pix.ROTATE_90,
                        180: orig_pix.ROTATE_180,
                        270: orig_pix.ROTATE_270,
                    }
                    current_pix = orig_pix.flip_rotate(rot_map[rot_osd])
                    rotation_applied = rot_osd

                    # Step 3: Check residual skew after quadrant rotation
                    skew_after = _detect_skew_angle(current_pix)
                    if abs(skew_after) >= 0.5 and abs(skew_after) <= 15.0:
                        deskew_pix, deskew_mat = _deskew_pixmap(current_pix, skew_after)
                        deskew_applied = skew_after
                    else:
                        deskew_pix = current_pix
                    upright_w = max(1.0, float(current_pix.width))
                    upright_h = max(1.0, float(current_pix.height))
                else:
                    # No quadrant rotation needed
                    if initial_deskew_mat is not None:
                        deskew_pix = level_pix
                        deskew_applied = skew_initial
                        deskew_mat = initial_deskew_mat
                    else:
                        deskew_pix = orig_pix
                    upright_w = max(1.0, float(orig_pix.width))
                    upright_h = max(1.0, float(orig_pix.height))
            else:
                deskew_pix = orig_pix
                upright_w = max(1.0, float(orig_pix.width))
                upright_h = max(1.0, float(orig_pix.height))

            # Step 4: Adaptive Page Segmentation & Layout Analysis
            override_psm = None
            if page.classification_signals and "ocr_psm_override" in page.classification_signals:
                override_psm = str(page.classification_signals["ocr_psm_override"]).strip()

            if override_psm:
                selected_psm = override_psm
                layout_details = {"layout": "override", "selected_psm": override_psm}
            elif self.adaptive_psm:
                selected_psm, layout_details = analyze_page_layout(deskew_pix, default_psm=self.default_psm)
            else:
                selected_psm = self.default_psm
                layout_details = {"layout": "fixed", "selected_psm": self.default_psm}

            # Step 5: Run Tesseract TSV OCR on deskew_pix
            ocr_png_bytes = deskew_pix.tobytes("png")

            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                input_png = tmp_path / f"page_{page.page_number}.png"
                output_base = tmp_path / f"ocr_{page.page_number}"
                output_tsv = tmp_path / f"ocr_{page.page_number}.tsv"

                input_png.write_bytes(ocr_png_bytes)

                cmd = [
                    self.tesseract_cmd,
                    str(input_png),
                    str(output_base),
                    "-l",
                    self.lang,
                    "--psm",
                    selected_psm,
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

                # Safe fallback: If adaptive PSM failed, fall back to default_psm
                if result.returncode != 0 and selected_psm != self.default_psm:
                    fallback_cmd = [
                        self.tesseract_cmd,
                        str(input_png),
                        str(output_base),
                        "-l",
                        self.lang,
                        "--psm",
                        self.default_psm,
                        "tsv",
                    ]
                    try:
                        fallback_res = subprocess.run(
                            fallback_cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            timeout=self.timeout_seconds,
                        )
                        if fallback_res.returncode == 0:
                            result = fallback_res
                            selected_psm = self.default_psm
                            layout_details["fallback_applied"] = True
                    except Exception:
                        pass

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

                        corners = [
                            (left, top),
                            (left + width, top),
                            (left + width, top + height),
                            (left, top + height),
                        ]
                        norm_x, norm_y, norm_w, norm_h = _map_corners_back(
                            corners=corners,
                            deskew_pix=deskew_pix,
                            deskew_mat=deskew_mat,
                            upright_w=upright_w,
                            upright_h=upright_h,
                            rotation_applied=rotation_applied,
                        )

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
                avg_conf = (sum(confidences) / len(confidences)) if confidences else 0.0

                updated_signals = dict(page.classification_signals) if page.classification_signals else {}
                updated_signals["ocr_rotation_applied"] = rotation_applied
                updated_signals["ocr_deskew_applied"] = deskew_applied
                updated_signals["ocr_psm_selected"] = selected_psm
                updated_signals["ocr_layout_detected"] = layout_details.get("layout", "unknown")

                updated_page = page.model_copy(
                    update={
                        "text_content": full_text,
                        "spans": spans,
                        "ocr_provider": self.provider_name,
                        "ocr_confidence": round(avg_conf, 3),
                        "is_scanned": True,
                        "requires_ocr": False,
                        "classification_signals": updated_signals,
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
