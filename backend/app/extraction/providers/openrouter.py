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
from typing import Any, Dict, List, Optional, Union

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


def extract_http_error_details(exc: Exception) -> Dict[str, Any]:
    """Extract structured details from an SDK/HTTP exception safely without leaking credentials.

    Defensive extraction precedence:
    1. Standard structured error body: body['error']['message'], ['type'], ['code'], ['param']
    2. Alternative structured fields: body['message'], body['detail'], body['description'], alt keys
    3. Raw HTTP response text: exc.response.text or exc.response.content, safely read if unconsumed
    4. Exception string details or explicit empty body limitation
    """
    details: Dict[str, Any] = {
        "status_code": getattr(exc, "status_code", None),
        "message": None,
        "error_type": getattr(exc, "type", None),
        "error_code": getattr(exc, "code", None),
        "error_param": getattr(exc, "param", None),
        "request_id": getattr(exc, "request_id", None),
    }

    # Safely extract status code from response if not on exc directly
    try:
        if details["status_code"] is None and hasattr(exc, "response"):
            details["status_code"] = getattr(exc.response, "status_code", None)
    except Exception:
        pass

    # Safely extract request ID from response headers if not on exc directly
    try:
        if details["request_id"] is None and hasattr(exc, "response"):
            headers = getattr(exc.response, "headers", None)
            if headers and hasattr(headers, "get"):
                for req_hdr in ("x-request-id", "x-openrouter-request-id", "cf-ray", "request-id"):
                    val = headers.get(req_hdr)
                    if val and str(val).strip():
                        details["request_id"] = str(val).strip()
                        break
    except Exception:
        pass

    extracted_msg: Optional[str] = None
    body = getattr(exc, "body", None)

    # 1. Standard structured error body (OpenAI-compatible)
    if isinstance(body, dict):
        err_obj = body.get("error")
        if isinstance(err_obj, dict):
            if err_obj.get("message") is not None and str(err_obj.get("message")).strip():
                extracted_msg = str(err_obj.get("message")).strip()
            if err_obj.get("type"):
                details["error_type"] = str(err_obj.get("type"))
            if err_obj.get("code"):
                details["error_code"] = str(err_obj.get("code"))
            if err_obj.get("param"):
                details["error_param"] = str(err_obj.get("param"))
        elif isinstance(err_obj, str) and err_obj.strip():
            extracted_msg = err_obj.strip()

    # 2. Alternative structured fields in body
    if not extracted_msg and isinstance(body, dict):
        # 2a. body["message"]
        if body.get("message") is not None and str(body.get("message")).strip():
            extracted_msg = str(body.get("message")).strip()
        # 2b. body["detail"]
        elif body.get("detail") is not None:
            detail_val = body.get("detail")
            if isinstance(detail_val, str) and detail_val.strip():
                extracted_msg = detail_val.strip()
            elif isinstance(detail_val, list):
                parts = []
                for item in detail_val:
                    if isinstance(item, dict):
                        loc = ".".join(str(l) for l in item.get("loc", []))
                        msg = item.get("msg", str(item))
                        parts.append(f"{loc}: {msg}" if loc else str(msg))
                    else:
                        parts.append(str(item))
                if parts:
                    extracted_msg = "; ".join(parts)
            elif isinstance(detail_val, dict):
                extracted_msg = json.dumps(detail_val)
            elif str(detail_val).strip():
                extracted_msg = str(detail_val).strip()
        # 2c. body["description"]
        elif body.get("description") is not None and str(body.get("description")).strip():
            extracted_msg = str(body.get("description")).strip()
        # 2d. Other common dict fields (msg, error_description, reason, or full body dump)
        elif body:
            for alt_k in ("msg", "err", "error_description", "reason"):
                if body.get(alt_k) and str(body.get(alt_k)).strip():
                    extracted_msg = str(body.get(alt_k)).strip()
                    break
            if not extracted_msg:
                try:
                    extracted_msg = json.dumps(body)
                except Exception:
                    pass

    # If body is a plain non-empty string
    if not extracted_msg and isinstance(body, str) and body.strip():
        extracted_msg = body.strip()

    # 3. Raw HTTP response text or content, if safely accessible
    if not extracted_msg:
        try:
            resp = getattr(exc, "response", None)
            if resp is not None:
                # Read stream to completion if not already consumed
                if hasattr(resp, "read") and not getattr(resp, "is_stream_consumed", True):
                    try:
                        resp.read()
                    except Exception:
                        pass

                resp_text = None
                if hasattr(resp, "text"):
                    try:
                        resp_text = resp.text
                    except Exception:
                        pass
                if not resp_text and hasattr(resp, "content"):
                    try:
                        raw_bytes = resp.content
                        if raw_bytes:
                            resp_text = raw_bytes.decode("utf-8", errors="replace")
                    except Exception:
                        pass

                if isinstance(resp_text, str) and resp_text.strip():
                    try:
                        parsed_json = json.loads(resp_text)
                        if isinstance(parsed_json, dict):
                            err_sub = parsed_json.get("error")
                            if isinstance(err_sub, dict) and err_sub.get("message"):
                                extracted_msg = str(err_sub.get("message")).strip()
                                if err_sub.get("type") and not details["error_type"]:
                                    details["error_type"] = str(err_sub.get("type"))
                                if err_sub.get("code") and not details["error_code"]:
                                    details["error_code"] = str(err_sub.get("code"))
                                if err_sub.get("param") and not details["error_param"]:
                                    details["error_param"] = str(err_sub.get("param"))
                            elif parsed_json.get("message"):
                                extracted_msg = str(parsed_json.get("message")).strip()
                            elif parsed_json.get("detail"):
                                det = parsed_json.get("detail")
                                extracted_msg = str(det) if not isinstance(det, (dict, list)) else json.dumps(det)
                            elif parsed_json.get("description"):
                                extracted_msg = str(parsed_json.get("description")).strip()
                            else:
                                extracted_msg = json.dumps(parsed_json)
                    except Exception:
                        pass
                    if not extracted_msg:
                        extracted_msg = resp_text.strip()
        except Exception:
            pass

    # 4. Final fallback check: inspect exc string or explicitly report empty body
    if not extracted_msg or not str(extracted_msg).strip():
        exc_str = str(exc).strip() if exc else ""
        if " - " in exc_str:
            # SDK formatted error string like 'Error code: 400 - Actual upstream message'
            extracted_msg = exc_str.split(" - ", 1)[1].strip()
        elif exc_str:
            extracted_msg = exc_str
        else:
            status_desc = f"HTTP {details['status_code']}" if details.get('status_code') else "HTTP error"
            extracted_msg = f"No error details provided by upstream API ({status_desc})"

    # Truncate excessively long error messages (avoid giant HTML / raw dumps)
    MAX_ERROR_MSG_LENGTH = 1000
    if len(extracted_msg) > MAX_ERROR_MSG_LENGTH:
        extracted_msg = extracted_msg[:MAX_ERROR_MSG_LENGTH] + "... [truncated]"

    # Sanitize and redact credentials in all string fields
    details["message"] = sanitize_error_message(extracted_msg)
    if details["request_id"]:
        details["request_id"] = sanitize_error_message(str(details["request_id"]))
    if details["error_type"]:
        details["error_type"] = sanitize_error_message(str(details["error_type"]))
    if details["error_code"]:
        details["error_code"] = sanitize_error_message(str(details["error_code"]))
    if details["error_param"]:
        details["error_param"] = sanitize_error_message(str(details["error_param"]))

    return details


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
        allow_response_format_fallback: bool = False,
        json_mode: Union[bool, str] = True,
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
        env_retries = os.getenv("EXTRACTION_MAX_RETRIES")
        if env_retries is not None:
            try:
                max_retries = int(env_retries)
            except ValueError:
                pass
        self.max_retries = max(1, max_retries)
        self._injected_client = client
        self.allow_response_format_fallback = allow_response_format_fallback

        # Resolve explicit JSON mode configuration (default: enabled)
        env_json_mode = os.getenv("EXTRACTION_JSON_MODE")
        if json_mode is not None:
            if isinstance(json_mode, str):
                self.json_mode: bool = json_mode.strip().lower() not in ("disabled", "false", "0", "no", "off")
            else:
                self.json_mode = bool(json_mode)
        elif env_json_mode is not None:
            clean_env = env_json_mode.strip().lower()
            self.json_mode = clean_env not in ("disabled", "false", "0", "no", "off")
        else:
            self.json_mode = True

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

    @staticmethod
    def is_response_format_error(exc: Exception, details: Optional[Dict[str, Any]] = None) -> bool:
        """Identify whether an exception represents an unsupported response_format/JSON-mode rejection.
        
        Inspects structured body fields (error_param, error_code, message) and exception text.
        Avoids classifying unrelated HTTP 400 errors (e.g. invalid credentials, context length,
        general JSON decode/schema errors, missing messages, etc.) as response_format errors.
        """
        if details is None:
            details = extract_http_error_details(exc)

        param = (details.get("error_param") or "").lower()
        if param in ("response_format", "response_format.type", "response_format.json_schema"):
            return True

        err_code = (details.get("error_code") or "").lower()
        if err_code in ("unsupported_response_format", "invalid_response_format", "response_format_not_supported"):
            return True

        msg = (details.get("message") or "").lower()
        exc_str = str(exc).lower()
        combined_text = f"{msg} {exc_str}"

        format_indicators = [
            "response_format",
            "json_object",
            "json mode",
            "json_mode",
            "json_schema",
            "structured_outputs",
            "structured outputs",
        ]
        rejection_indicators = [
            "not supported",
            "unsupported",
            "not available",
            "disabled",
            "invalid parameter",
            "unrecognized parameter",
            "unknown parameter",
            "unexpected parameter",
            "does not support",
            "is not supported",
            "rejected",
        ]

        for fi in format_indicators:
            if fi in combined_text:
                for ri in rejection_indicators:
                    if ri in combined_text:
                        return True

        explicit_phrases = [
            "unsupported format",
            "format is not supported",
            "schema is not supported",
            "json-mode is not supported",
            "json schema not supported",
            "response_format is not allowed",
        ]
        for phrase in explicit_phrases:
            if phrase in combined_text:
                return True

        return False

    def _build_user_content(self, pages: List[PDFPageInspection]) -> Union[str, List[Dict[str, Any]]]:
        """Assemble user message content.
        
        When raster images are present, returns a multimodal content array with text
        and base64 image_url objects.
        When text-only (image_count == 0), returns a single plain string to ensure
        broad compatibility across OpenAI-compatible text and vision endpoints.
        """
        content_parts: List[Dict[str, Any]] = []
        text_parts: List[str] = []
        image_count = 0

        for p in pages:
            page_text = p.text_content or "(No text layer found on this page)"
            header = f"=== PAGE {p.page_number} (Dimensions: {p.width_pts}x{p.height_pts} pt) ===\n"
            full_page_str = header + page_text
            text_parts.append(full_page_str)
            content_parts.append({
                "type": "text",
                "text": full_page_str,
            })

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
            return content_parts
        else:
            logger.info("OpenRouter payload constructed with %d pages (text-only string format).", len(pages))
            return "\n\n".join(text_parts)

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
        use_response_format = self.json_mode

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
                details = extract_http_error_details(exc)
                status_code = details["status_code"]
                error_code = details["error_code"]
                request_id = details["request_id"]
                server_msg = details["message"]
                err_lower = f"{server_msg} {str(exc)}".lower()

                # Check if error is specifically an unsupported response_format / JSON-mode error
                is_resp_format_err = self.is_response_format_error(exc, details)
                if is_resp_format_err:
                    if self.allow_response_format_fallback and use_response_format and attempt < self.max_retries:
                        logger.info("Model '%s' rejected response_format; retrying without response_format option.", self.model_name)
                        use_response_format = False
                        continue
                    raise ExtractionInvalidRequestError(
                        f"OpenRouter invalid request/model error (HTTP {status_code or 400}): model '{self.model_name}' does not support response_format/JSON mode: {server_msg}",
                        provider_name=self.provider_name,
                        status_code=status_code or 400,
                        error_code="unsupported_response_format",
                        request_id=request_id,
                    ) from exc

                # 1. Authentication failure (401)
                if status_code == 401 or "unauthorized" in err_lower or "invalid api key" in err_lower:
                    raise ExtractionAuthenticationError(
                        f"OpenRouter authentication failed (401): {server_msg}",
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
                        f"OpenRouter rate limit exceeded after {self.max_retries} attempts: {server_msg}",
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
                        f"OpenRouter request timed out after {attempt} attempts: {server_msg}",
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
                        f"OpenRouter extraction failed after {self.max_retries} attempts: {server_msg}",
                        provider_name=self.provider_name,
                        is_retryable=False,
                        status_code=status_code or 503,
                        error_code=error_code or "service_unavailable",
                        request_id=request_id,
                    ) from exc

                # 5. Invalid request / model capability (HTTP 400, 404, 422) - non-retryable
                if status_code in (400, 404, 422) or "not found" in err_lower or "invalid request" in err_lower or is_resp_format_err:
                    if server_msg.startswith("OpenRouter invalid request/model error"):
                        formatted_err = server_msg
                    else:
                        formatted_err = f"OpenRouter invalid request/model error (HTTP {status_code or 400}): {server_msg}"
                    code_to_report = "unsupported_response_format" if is_resp_format_err else (error_code or f"http_{status_code or 400}")
                    raise ExtractionInvalidRequestError(
                        formatted_err,
                        provider_name=self.provider_name,
                        status_code=status_code or 400,
                        error_code=code_to_report,
                        request_id=request_id,
                    ) from exc

                # 6. Any other permanent provider error
                raise ExtractionProviderError(
                    f"OpenRouter API call failed on attempt {attempt}: {server_msg}",
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


__all__ = [
    "OpenRouterExtractionProvider",
    "OPENROUTER_SYSTEM_PROMPT",
    "extract_http_error_details",
]
