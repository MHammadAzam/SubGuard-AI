"""OpenRouter extraction provider for SubGuard AI.

Uses the OpenAI-compatible Python SDK to connect to OpenRouter (https://openrouter.ai/api/v1),
enabling live extraction benchmarks with free models (default: nvidia/nemotron-3.5-super-vl:free)
without consuming paid Gemini API quota or falling back to paid providers.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from app.extraction.providers.base import (
    BaseExtractionProvider,
    ExtractionAuthenticationError,
    ExtractionConfigurationError,
    ExtractionInvalidModelResponseError,
    ExtractionInvalidRequestError,
    ExtractionProviderError,
    ExtractionRateLimitError,
    ExtractionSchemaValidationError,
    ExtractionTimeoutError,
    sanitize_error_message,
)
from app.schemas.pdf import PDFPageInspection
from app.schemas.raw_extraction import RawInsuranceExtraction

logger = logging.getLogger(__name__)


def _format_pydantic_validation_error(val_err: Exception) -> str:
    """Format sanitized validation error message without echoing submitted document content."""
    if hasattr(val_err, "errors") and callable(val_err.errors):
        loc_summaries = []
        try:
            for err in val_err.errors()[:5]:
                loc = ".".join(str(l) for l in err.get("loc", []))
                msg = err.get("msg", "invalid value")
                loc_summaries.append(f"{loc} ({msg})")
            if loc_summaries:
                return f"Schema validation failed on fields: {', '.join(loc_summaries)}"
        except Exception:
            pass
    return "Schema validation failed against RawInsuranceExtraction contract."


OPENROUTER_SYSTEM_PROMPT = """You are an expert insurance document extraction engine for Certificates of Insurance (COI / ACORD 25) and endorsements.
Your job is to extract structured insurance information with exact source evidence text into a single valid JSON object matching the RawInsuranceExtraction schema.

EXTRACTION & EVIDENCE RULES:
1. Extract ONLY information explicitly present in the document. NEVER guess, assume, or fabricate.
2. For every field with an evidence requirement, quote the exact verbatim text from the page (source_text) and the 1-indexed page_number. Evidence belongs STRICTLY in dedicated "evidence" sub-objects. Do not invent quotations or treat isolated form labels as proof of an extracted value.
3. Limits: Extract all monetary amounts as printed raw strings (e.g. '$1,000,000', '$2,000,000', '1,000,000', '$500,000').
4. Dates: Extract policy dates as printed raw strings (e.g. '01/01/2025', 'Jan 1, 2025').
5. Endorsements: Identify type ('additional_insured', 'waiver_of_subrogation', 'primary_noncontributory', 'other'), form numbers ('CG 20 10 04 13'), and whether wording is automatic/blanket ('is_blanket': true/false).

FIELD TYPE SPECIFICATIONS (CRITICAL):
- "insurers": MUST ALWAYS be a JSON array of strings (e.g. ["Travelers Indemnity Co"]). If no insurers are listed or identified, return an empty array []. NEVER return null, a single string, or a dictionary.
- "description_of_operations": MUST be a plain JSON string (e.g. "Project: Commercial Tower framing") or null if blank. NEVER return an evidence object, dictionary, or array for this field.
- "coverages": Array of coverage section objects. Limits (e.g. "each_occurrence", "general_aggregate") and dates ("effective_date", "expiration_date") are direct fields on each coverage object, NOT inside a nested "limits" dict.
- "raw_contradictions": Array of strings describing contradictory statements if present, else [].
- "is_document_set_complete": Boolean, true if standard COI requirements appear complete.

EXACT JSON SCHEMA STRUCTURE:
{
  "document_type": "coi",
  "named_insured": {
    "name": "string or null",
    "address": "string or null",
    "evidence": {
      "page_number": 1,
      "source_text": "exact quote from document",
      "confidence": 1.0
    },
    "confidence": 1.0
  },
  "certificate_holder": {
    "name": "string or null",
    "address": "string or null",
    "evidence": {
      "page_number": 1,
      "source_text": "exact quote from document",
      "confidence": 1.0
    },
    "confidence": 1.0
  },
  "producer": {
    "name": "string or null",
    "address": "string or null",
    "evidence": {
      "page_number": 1,
      "source_text": "exact quote from document",
      "confidence": 1.0
    },
    "confidence": 1.0
  },
  "insurers": ["Insurer Name 1", "Insurer Name 2"],
  "coverages": [
    {
      "coverage_type": "general_liability",
      "policy_number": "string or null",
      "insurer_name": "string or null",
      "each_occurrence": "$1,000,000",
      "general_aggregate": "$2,000,000",
      "products_completed_ops": "string or null",
      "personal_adv_injury": "string or null",
      "combined_single_limit": "string or null",
      "per_accident": "string or null",
      "disease_each_employee": "string or null",
      "disease_policy_limit": "string or null",
      "damage_to_rented_premises": "string or null",
      "medical_expense": "string or null",
      "effective_date": "01/01/2025",
      "expiration_date": "01/01/2026",
      "evidence": {
        "page_number": 1,
        "source_text": "exact quote from document",
        "confidence": 1.0
      },
      "confidence": 1.0
    }
  ],
  "endorsements": [
    {
      "endorsement_type": "additional_insured",
      "form_number": "CG 20 10 04 13",
      "title": "string or null",
      "wording": "string or null",
      "applicable_policy": "string or null",
      "named_entities": [],
      "key_terms": [],
      "is_blanket": false,
      "is_coi_checkbox_only": false,
      "evidence": {
        "page_number": 1,
        "source_text": "exact quote from document",
        "confidence": 1.0
      },
      "confidence": 1.0
    }
  ],
  "description_of_operations": "plain text string or null",
  "raw_contradictions": [],
  "is_document_set_complete": true
}

Output ONLY the JSON object. Do not include markdown code block formatting or additional conversational commentary.
"""


class OpenRouterExtractionProvider(BaseExtractionProvider):
    """Extraction provider using OpenRouter OpenAI-compatible API."""

    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
    DEFAULT_MODEL = "nvidia/nemotron-3.5-super-vl:free"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
        base_url: Optional[str] = None,
        max_retries: int = 3,
        client: Optional[Any] = None,
    ):
        self._api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        chosen_model = model_name or self.DEFAULT_MODEL
        if not self.is_free_model(chosen_model):
            raise ExtractionConfigurationError(
                f"Model '{chosen_model}' is not an explicitly approved free model (must end with ':free'). "
                "Paid models, paid fallback, and automatic routers are strictly prohibited.",
                provider_name="openrouter",
            )
        self.model_name = chosen_model
        self.base_url = base_url or self.DEFAULT_BASE_URL
        self.max_retries = max_retries
        self._injected_client = client

    @staticmethod
    def is_free_model(model: Optional[str]) -> bool:
        """Check if model name is explicitly identified as free."""
        if not model:
            return False
        clean = model.strip().lower()
        if clean in ("openrouter/auto", "auto", "default"):
            return False
        return clean.endswith(":free")

    @property
    def provider_name(self) -> str:
        return "openrouter"

    def _get_client(self) -> Any:
        """Initialize or return the OpenAI-compatible Client configured for OpenRouter."""
        if self._injected_client is not None:
            return self._injected_client

        if not self._api_key:
            raise ExtractionConfigurationError(
                "OpenRouter API key is not configured. Set the OPENROUTER_API_KEY environment variable.",
                provider_name=self.provider_name,
            )

        try:
            from openai import OpenAI
            return OpenAI(
                base_url=self.base_url,
                api_key=self._api_key,
                default_headers={
                    "HTTP-Referer": "https://github.com/subguard-ai",
                    "X-Title": "SubGuard AI Insurance Extraction",
                },
            )
        except ImportError as exc:
            raise ExtractionConfigurationError(
                f"openai package is not installed: {exc}. Run 'pip install openai'.",
                provider_name=self.provider_name,
            ) from exc

    def _build_user_content(self, pages: List[PDFPageInspection]) -> List[Dict[str, Any]]:
        """Assemble multimodal content parts combining text layers, page boundaries, and raster images."""
        content_parts: List[Dict[str, Any]] = []
        image_count = 0

        for p in pages:
            # Add text representation with explicit 1-indexed page boundaries
            page_text = p.text_content or "(No text layer found on this page)"
            header = f"=== PAGE {p.page_number} (Dimensions: {p.width_pts}x{p.height_pts} pt) ===\n"
            content_parts.append({
                "type": "text",
                "text": header + page_text,
            })

            # If page raster image bytes are available, attach as base64 image_url part
            image_data = getattr(p, "rendered_image_png", None) or getattr(p, "image_bytes", None)
            if image_data:
                try:
                    b64_img = base64.b64encode(image_data).decode("utf-8")
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64_img}",
                        },
                    })
                    image_count += 1
                except Exception as img_err:
                    logger.warning("Failed to encode image for page %d: %s", p.page_number, img_err)

        if image_count > 0:
            logger.info("OpenRouter payload constructed with %d pages and %d raster images.", len(pages), image_count)
        else:
            logger.info("OpenRouter payload constructed with %d pages (text-only, no raster images).", len(pages))

        return content_parts

    def _clean_json_text(self, text: str) -> str:
        """Strip markdown code block fences and trailing text if present."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        return cleaned

    def extract_insurance_data(
        self,
        document_id: str,
        pages: List[PDFPageInspection],
        timeout_seconds: float = 30.0,
    ) -> RawInsuranceExtraction:
        """Call OpenRouter chat completions API with bounded retries and schema validation."""
        client = self._get_client()

        user_content = self._build_user_content(pages)
        messages = [
            {"role": "system", "content": OPENROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        last_error: Optional[Exception] = None
        backoff_seconds = 1.0
        use_response_format = True

        for attempt in range(1, self.max_retries + 1):
            try:
                kwargs = {
                    "model": self.model_name,
                    "messages": messages,
                    "temperature": 0.0,
                    "timeout": timeout_seconds,
                }
                if use_response_format:
                    kwargs["response_format"] = {"type": "json_object"}

                response = client.chat.completions.create(**kwargs)

                # Extract response text
                if not response.choices:
                    raise ExtractionInvalidModelResponseError(
                        "OpenRouter returned empty choices array in chat completion.",
                        provider_name=self.provider_name,
                        error_code="empty_choices",
                    )

                raw_text = response.choices[0].message.content or ""
                if not raw_text.strip():
                    raise ExtractionInvalidModelResponseError(
                        "OpenRouter model returned empty content.",
                        provider_name=self.provider_name,
                        error_code="empty_content",
                    )

                cleaned_json = self._clean_json_text(raw_text)

                try:
                    data = json.loads(cleaned_json)
                except Exception as json_err:
                    raise ExtractionSchemaValidationError(
                        f"Failed to parse OpenRouter response as JSON: {json_err}.",
                        provider_name=self.provider_name,
                        error_code="invalid_json",
                    ) from json_err

                if not isinstance(data, dict):
                    raise ExtractionInvalidModelResponseError(
                        f"OpenRouter JSON response root must be an object/dict, got {type(data).__name__}",
                        provider_name=self.provider_name,
                        error_code="non_dict_root",
                    )

                try:
                    return RawInsuranceExtraction.model_validate(data)
                except Exception as val_err:
                    safe_err_summary = _format_pydantic_validation_error(val_err)
                    raise ExtractionSchemaValidationError(
                        f"OpenRouter JSON response failed RawInsuranceExtraction schema validation: {safe_err_summary}",
                        provider_name=self.provider_name,
                        error_code="schema_validation_failed",
                    ) from val_err

            except ExtractionSchemaValidationError:
                # Schema validation errors are non-transient; do not retry
                raise

            except ExtractionConfigurationError:
                raise

            except Exception as exc:
                last_error = exc
                err_msg = str(exc)
                err_lower = err_msg.lower()

                # Extract structured status_code from SDK exception
                status_code: Optional[int] = getattr(exc, "status_code", None)
                if status_code is None and hasattr(exc, "response") and hasattr(exc.response, "status_code"):
                    status_code = getattr(exc.response, "status_code", None)

                # Extract structured error_code
                error_code: Optional[str] = getattr(exc, "code", None)
                if error_code is None and hasattr(exc, "body") and isinstance(exc.body, dict):
                    err_obj = exc.body.get("error")
                    if isinstance(err_obj, dict):
                        error_code = str(err_obj.get("code") or err_obj.get("type") or "")
                        if not error_code:
                            error_code = None

                # Extract request_id if available
                request_id: Optional[str] = getattr(exc, "request_id", None)
                if request_id is None and hasattr(exc, "response") and hasattr(exc.response, "headers"):
                    try:
                        request_id = exc.response.headers.get("x-request-id")
                    except Exception:
                        request_id = None

                # Compatibility fix: if model rejects response_format, retry request without it
                is_response_format_error = (
                    "response_format" in err_lower
                    or "json_object" in err_lower
                    or ("unsupported" in err_lower and "format" in err_lower)
                )
                if use_response_format and (
                    (status_code == 400 and is_response_format_error)
                    or ("response_format" in err_lower and ("not supported" in err_lower or "unsupported" in err_lower))
                ):
                    logger.info("Model '%s' rejected response_format; retrying without response_format option.", self.model_name)
                    use_response_format = False
                    continue

                # 1. Authentication failure (401)
                if status_code == 401 or "unauthorized" in err_lower or "invalid api key" in err_lower:
                    raise ExtractionAuthenticationError(
                        f"OpenRouter authentication failed (401): {exc}",
                        provider_name=self.provider_name,
                        status_code=401,
                        error_code=error_code or "unauthorized",
                        request_id=request_id,
                    ) from exc

                # 2. Rate limits (429)
                if status_code == 429 or "rate limit" in err_lower or "quota" in err_lower:
                    if attempt < self.max_retries:
                        time.sleep(backoff_seconds)
                        backoff_seconds *= 2.0
                        continue
                    raise ExtractionRateLimitError(
                        f"OpenRouter rate limit exceeded after {self.max_retries} attempts: {exc}",
                        provider_name=self.provider_name,
                        status_code=429,
                        error_code=error_code or "rate_limit_exceeded",
                        request_id=request_id,
                    ) from exc

                # 3. Timeout (408)
                if status_code == 408 or "timeout" in err_lower or "timed out" in err_lower or "deadline" in err_lower:
                    if attempt < self.max_retries:
                        time.sleep(backoff_seconds)
                        backoff_seconds *= 2.0
                        continue
                    raise ExtractionTimeoutError(
                        f"OpenRouter request timed out after {attempt} attempts: {exc}",
                        provider_name=self.provider_name,
                        status_code=status_code or 408,
                        error_code=error_code or "timeout",
                        request_id=request_id,
                    ) from exc

                # 4. Transient server errors (500, 502, 503, 504, provider/service unavailable)
                if (status_code in (500, 502, 503, 504) or any(k in err_lower for k in ["500", "502", "503", "504", "internal", "unavailable", "bad gateway", "service unavailable"])):
                    if attempt < self.max_retries:
                        time.sleep(backoff_seconds)
                        backoff_seconds *= 2.0
                        continue
                    raise ExtractionProviderError(
                        f"OpenRouter extraction failed after {self.max_retries} attempts: {exc}",
                        provider_name=self.provider_name,
                        is_retryable=False,
                        status_code=status_code or 503,
                        error_code=error_code or "service_unavailable",
                        request_id=request_id,
                    ) from exc

                # 5. Invalid request / model capability (HTTP 400, 404, 422) - non-retryable
                if status_code in (400, 404, 422) or "not found" in err_lower or "invalid request" in err_lower:
                    raise ExtractionInvalidRequestError(
                        f"OpenRouter invalid request/model error: {exc}",
                        provider_name=self.provider_name,
                        status_code=status_code or 400,
                        error_code=error_code or (f"http_{status_code}" if status_code else "invalid_request"),
                        request_id=request_id,
                    ) from exc

                # 6. Any other permanent provider error
                raise ExtractionProviderError(
                    f"OpenRouter API call failed on attempt {attempt}: {exc}",
                    provider_name=self.provider_name,
                    is_retryable=False,
                    status_code=status_code,
                    error_code=error_code or (f"http_{status_code}" if status_code else "provider_error"),
                    request_id=request_id,
                ) from exc

        raise ExtractionProviderError(
            f"OpenRouter extraction failed after {self.max_retries} attempts: {last_error}",
            provider_name=self.provider_name,
            is_retryable=False,
        ) from last_error
