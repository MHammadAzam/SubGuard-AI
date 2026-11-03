"""Google Gemini multimodal extraction provider for SubGuard AI.

Uses the official modern `google-genai` SDK to perform schema-constrained,
evidence-grounded extraction of insurance certificates and endorsements.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, List, Optional

from app.extraction.providers.base import (
    BaseExtractionProvider,
    ExtractionConfigurationError,
    ExtractionProviderError,
    ExtractionRateLimitError,
    ExtractionSchemaValidationError,
    ExtractionTimeoutError,
)
from app.schemas.pdf import PDFPageInspection
from app.schemas.raw_extraction import RawInsuranceExtraction

logger = logging.getLogger(__name__)

EXTRACTION_SYSTEM_PROMPT = """You are an expert insurance document extraction engine for Certificates of Insurance (COI / ACORD 25) and endorsements.
Your job is to extract structured insurance information with exact source evidence text.

RULES:
1. Extract ONLY information explicitly present in the document.
2. If a field is not present or blank, return null / None. NEVER guess, assume, or fabricate.
3. For every extracted field, quote the exact verbatim text from the page where it was found (source_text) and the 1-indexed page_number.
4. If a value is ambiguous or unclear, provide your best reading and note lower confidence (0.0 to 1.0).
5. Extract all sub-limits in their exact raw string representation (e.g. '$1,000,000', '$2,000,000', '1,000,000', '$500,000').
6. For dates, extract raw date strings as printed (e.g. '01/01/2025', 'Jan 1, 2025').
7. For endorsements, determine whether blanket automatic wording is used ('is_blanket') or if specific entities are scheduled.
8. Output valid JSON matching the requested schema.
"""


class GeminiExtractionProvider(BaseExtractionProvider):
    """Production extraction provider using Google Gemini multimodal models."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: str = "gemini-2.0-flash",
        max_retries: int = 3,
        client: Optional[Any] = None,
    ):
        self._api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model_name = model_name
        self.max_retries = max_retries
        self._injected_client = client

    @property
    def provider_name(self) -> str:
        return "gemini"

    def _get_client(self) -> Any:
        """Initialize or return the google-genai Client."""
        if self._injected_client is not None:
            return self._injected_client

        if not self._api_key:
            raise ExtractionConfigurationError(
                "Gemini API key is not configured. Set the GEMINI_API_KEY environment variable.",
                provider_name=self.provider_name,
            )

        try:
            from google import genai
            return genai.Client(api_key=self._api_key)
        except ImportError as exc:
            raise ExtractionConfigurationError(
                f"google-genai package is not installed: {exc}",
                provider_name=self.provider_name,
            ) from exc

    def _build_contents(self, pages: List[PDFPageInspection]) -> List[Any]:
        """Assemble multimodal contents combining text layers and page images."""
        contents: List[Any] = []

        try:
            from google.genai import types
            has_genai_types = True
        except ImportError:
            has_genai_types = False

        for p in pages:
            page_header = f"=== PAGE {p.page_number} (Dimensions: {p.width_pts}x{p.height_pts} pt) ===\n"
            contents.append(page_header + (p.text_content or "(No text layer found on this page)"))

            # If page image bytes are available, attach as image part
            image_data = getattr(p, "rendered_image_png", None) or getattr(p, "image_bytes", None)
            if image_data and has_genai_types:
                try:
                    contents.append(types.Part.from_bytes(data=image_data, mime_type="image/png"))
                except Exception as img_err:
                    logger.warning("Failed to attach image part for page %d: %s", p.page_number, img_err)

        return contents

    def extract_insurance_data(
        self,
        document_id: str,
        pages: List[PDFPageInspection],
        timeout_seconds: float = 30.0,
    ) -> RawInsuranceExtraction:
        """Call Gemini to extract structured insurance information with bounded retries."""
        client = self._get_client()

        try:
            from google.genai import types
        except ImportError as exc:
            raise ExtractionProviderError(
                f"google-genai types could not be imported: {exc}",
                provider_name=self.provider_name,
                is_retryable=False,
            ) from exc

        contents = self._build_contents(pages)
        config = types.GenerateContentConfig(
            system_instruction=EXTRACTION_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=RawInsuranceExtraction,
            temperature=0.0,
        )

        last_error: Optional[Exception] = None
        backoff_seconds = 1.0

        for attempt in range(1, self.max_retries + 1):
            try:
                response = client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=config,
                )

                # Parse and validate response
                if hasattr(response, "parsed") and isinstance(response.parsed, RawInsuranceExtraction):
                    return response.parsed

                raw_text = getattr(response, "text", "") or ""
                if not raw_text.strip():
                    raise ExtractionSchemaValidationError(
                        "Gemini returned an empty response body.",
                        provider_name=self.provider_name,
                    )

                try:
                    return RawInsuranceExtraction.model_validate_json(raw_text)
                except Exception as val_err:
                    raise ExtractionSchemaValidationError(
                        f"Failed to validate Gemini response against RawInsuranceExtraction schema: {val_err}",
                        provider_name=self.provider_name,
                    ) from val_err

            except ExtractionSchemaValidationError:
                # Do not retry schema validation failures
                raise

            except Exception as exc:
                last_error = exc
                err_msg = str(exc).lower()

                # Rate limits
                if "429" in err_msg or "quota" in err_msg or "rate limit" in err_msg or "resource_exhausted" in err_msg:
                    if attempt < self.max_retries:
                        time.sleep(backoff_seconds)
                        backoff_seconds *= 2.0
                        continue
                    raise ExtractionRateLimitError(
                        f"Gemini API rate limit exceeded after {self.max_retries} attempts: {exc}",
                        provider_name=self.provider_name,
                    ) from exc

                # Timeouts
                if "timeout" in err_msg or "timed out" in err_msg or "deadline" in err_msg:
                    if attempt < self.max_retries:
                        time.sleep(backoff_seconds)
                        backoff_seconds *= 2.0
                        continue
                    raise ExtractionTimeoutError(
                        f"Gemini API request timed out after {attempt} attempts: {exc}",
                        provider_name=self.provider_name,
                    ) from exc

                # Transient server errors (500, 503)
                if ("500" in err_msg or "503" in err_msg or "unavailable" in err_msg or "internal" in err_msg) and attempt < self.max_retries:
                    time.sleep(backoff_seconds)
                    backoff_seconds *= 2.0
                    continue

                # Permanent or non-retryable error
                raise ExtractionProviderError(
                    f"Gemini API call failed on attempt {attempt}: {exc}",
                    provider_name=self.provider_name,
                    is_retryable=False,
                ) from exc

        raise ExtractionProviderError(
            f"Gemini extraction failed after {self.max_retries} attempts: {last_error}",
            provider_name=self.provider_name,
            is_retryable=False,
        ) from last_error
