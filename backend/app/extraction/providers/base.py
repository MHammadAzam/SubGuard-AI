"""Extraction provider interface and error definitions.

Enables swappable AI backends (Gemini, OpenAI, Anthropic, Mock) without
altering document-processing or compliance-evaluation logic.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import List, Optional

from app.schemas.pdf import PDFPageInspection
from app.schemas.raw_extraction import RawInsuranceExtraction


def sanitize_error_message(message: str) -> str:
    """Sanitize error messages to prevent leaking API keys, auth tokens, or confidential data."""
    if not message:
        return ""

    sanitized = str(message)

    # 1. Redact OpenRouter / OpenAI API keys (sk-or-v1-..., sk-...)
    sanitized = re.sub(r"sk-or-v1-[a-zA-Z0-9_\-]+", "[REDACTED_API_KEY]", sanitized)
    sanitized = re.sub(r"\bsk-[a-zA-Z0-9_\-]{20,}\b", "[REDACTED_API_KEY]", sanitized)

    # 2. Redact Google API keys (AIza...)
    sanitized = re.sub(r"AIza[0-9A-Za-z\-_]{35}", "[REDACTED_API_KEY]", sanitized)

    # 3. Redact Bearer tokens & Authorization headers
    sanitized = re.sub(r"(?i)bearer\s+[a-zA-Z0-9_\-\.]{10,}", "Bearer [REDACTED_TOKEN]", sanitized)
    sanitized = re.sub(r"(?i)authorization[:=]\s*[^\s,;]+", "Authorization: [REDACTED]", sanitized)

    # 4. Redact key / token query params or json fields (requiring : or = separator)
    sanitized = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|secret|password)\s*[:=]\s*['\"]?([a-zA-Z0-9_\-\.]{8,})['\"]?",
        r"\1=[REDACTED]",
        sanitized,
    )

    # 5. Redact raster image base64 data URLs if present in error
    sanitized = re.sub(r"data:image\/[a-zA-Z]+;base64,[a-zA-Z0-9+/=]+", "data:image/png;base64,[IMAGE_BYTES_REDACTED]", sanitized)

    return sanitized


class ExtractionProviderError(Exception):
    """Base exception for extraction provider failures."""
    def __init__(
        self,
        message: str,
        provider_name: str,
        is_retryable: bool = False,
        status_code: Optional[int] = None,
        error_code: Optional[str] = None,
        request_id: Optional[str] = None,
        error_category: Optional[str] = "provider_error",
    ):
        clean_msg = sanitize_error_message(message)
        super().__init__(clean_msg)
        self.message = clean_msg
        self.provider_name = provider_name
        self.is_retryable = is_retryable
        self.status_code = status_code
        self.error_code = error_code
        self.request_id = request_id
        self.error_category = error_category


class ExtractionTimeoutError(ExtractionProviderError):
    """Raised when the provider API call times out."""
    def __init__(
        self,
        message: str,
        provider_name: str,
        status_code: Optional[int] = 408,
        error_code: Optional[str] = "timeout",
        request_id: Optional[str] = None,
    ):
        super().__init__(
            message,
            provider_name,
            is_retryable=True,
            status_code=status_code,
            error_code=error_code,
            request_id=request_id,
            error_category="timeout_error",
        )


class ExtractionRateLimitError(ExtractionProviderError):
    """Raised when the provider rate limits requests (HTTP 429)."""
    def __init__(
        self,
        message: str,
        provider_name: str,
        status_code: Optional[int] = 429,
        error_code: Optional[str] = "rate_limit_exceeded",
        request_id: Optional[str] = None,
    ):
        super().__init__(
            message,
            provider_name,
            is_retryable=True,
            status_code=status_code,
            error_code=error_code,
            request_id=request_id,
            error_category="rate_limit_error",
        )


class ExtractionInvalidRequestError(ExtractionProviderError):
    """Raised when the request parameters or model capabilities are invalid (HTTP 400/404/422)."""
    def __init__(
        self,
        message: str,
        provider_name: str,
        status_code: Optional[int] = 400,
        error_code: Optional[str] = "invalid_request",
        request_id: Optional[str] = None,
    ):
        super().__init__(
            message,
            provider_name,
            is_retryable=False,
            status_code=status_code,
            error_code=error_code,
            request_id=request_id,
            error_category="invalid_request",
        )


class ExtractionSchemaValidationError(ExtractionProviderError):
    """Raised when the provider returns unparseable or schema-violating JSON."""
    def __init__(
        self,
        message: str,
        provider_name: str,
        status_code: Optional[int] = None,
        error_code: Optional[str] = "schema_validation_error",
        request_id: Optional[str] = None,
    ):
        super().__init__(
            message,
            provider_name,
            is_retryable=False,
            status_code=status_code,
            error_code=error_code,
            request_id=request_id,
            error_category="schema_validation_error",
        )


class ExtractionInvalidModelResponseError(ExtractionSchemaValidationError):
    """Raised when the provider returns empty content, no choices, or invalid response root."""
    def __init__(
        self,
        message: str,
        provider_name: str,
        status_code: Optional[int] = None,
        error_code: Optional[str] = "invalid_model_response",
        request_id: Optional[str] = None,
    ):
        super().__init__(
            message,
            provider_name,
            status_code=status_code,
            error_code=error_code,
            request_id=request_id,
        )
        self.error_category = "invalid_model_response"


class ExtractionConfigurationError(ExtractionProviderError):
    """Raised when the provider configuration is missing or invalid (e.g. missing API key, non-free model)."""
    def __init__(
        self,
        message: str,
        provider_name: str,
        status_code: Optional[int] = None,
        error_code: Optional[str] = "configuration_error",
        request_id: Optional[str] = None,
    ):
        super().__init__(
            message,
            provider_name,
            is_retryable=False,
            status_code=status_code,
            error_code=error_code,
            request_id=request_id,
            error_category="configuration_error",
        )


class ExtractionAuthenticationError(ExtractionConfigurationError):
    """Raised when provider authentication fails (e.g. HTTP 401 Unauthorized, invalid API key)."""
    def __init__(
        self,
        message: str,
        provider_name: str,
        status_code: Optional[int] = 401,
        error_code: Optional[str] = "unauthorized",
        request_id: Optional[str] = None,
    ):
        super().__init__(
            message,
            provider_name,
            status_code=status_code,
            error_code=error_code,
            request_id=request_id,
        )
        self.error_category = "authentication_error"


class BaseExtractionProvider(ABC):
    """Abstract interface that all AI extraction providers must implement."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the extraction provider (e.g. 'mock', 'gemini', 'openai')."""
        pass

    @abstractmethod
    def extract_insurance_data(
        self,
        document_id: str,
        pages: List[PDFPageInspection],
        timeout_seconds: float = 30.0,
    ) -> RawInsuranceExtraction:
        """Extract structured insurance data from inspected PDF pages.
        
        Args:
            document_id: Unique identifier of the document
            pages: Inspected pages with plain text and optional rendered raster images
            timeout_seconds: Request timeout in seconds
            
        Returns:
            Validated RawInsuranceExtraction model
            
        Raises:
            ExtractionProviderError subclasses on failure.
        """
        pass
