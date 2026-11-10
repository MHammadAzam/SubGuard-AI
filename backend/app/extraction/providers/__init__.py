"""Package marker and factory for extraction providers."""

import os
from typing import Optional, Union

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
)
from app.extraction.providers.mock import HonestApplicationMockProvider, MockExtractionProvider
from app.extraction.providers.gemini import GeminiExtractionProvider
from app.extraction.providers.openrouter import OpenRouterExtractionProvider


def _instantiate_provider(
    name: str,
    api_key: Optional[str] = None,
    model_name: Optional[str] = None,
    max_retries: int = 3,
    json_mode: Union[bool, str] = True,
) -> BaseExtractionProvider:
    """Instantiate a concrete extraction provider for a normalized provider name."""
    if name == "mock":
        return MockExtractionProvider()
    elif name in ("honest_mock", "application_mock"):
        return HonestApplicationMockProvider()
    elif name == "gemini":
        return GeminiExtractionProvider(api_key=api_key, model_name=model_name or "gemini-2.0-flash")
    elif name == "openrouter":
        return OpenRouterExtractionProvider(
            api_key=api_key,
            model_name=model_name or "nvidia/nemotron-3.5-super-vl:free",
            max_retries=max_retries,
            json_mode=json_mode,
        )
    else:
        raise ValueError(f"Unsupported extraction provider: '{name}'. Supported: 'mock', 'gemini', 'openrouter'.")


def get_extraction_provider(
    provider_name: Optional[str] = None,
    api_key: Optional[str] = None,
    model_name: Optional[str] = None,
    max_retries: int = 3,
    json_mode: Union[bool, str] = True,
) -> BaseExtractionProvider:
    """Fail-closed provider factory enforcing the configured server allowlist."""
    from app.security.config import validate_requested_provider

    name = (provider_name or os.getenv("EXTRACTION_PROVIDER", "mock")).strip().lower()
    name = validate_requested_provider(name)
    return _instantiate_provider(
        name,
        api_key=api_key,
        model_name=model_name,
        max_retries=max_retries,
        json_mode=json_mode,
    )


def get_benchmark_extraction_provider(
    provider_name: Optional[str] = None,
    api_key: Optional[str] = None,
    model_name: Optional[str] = None,
    max_retries: int = 3,
    json_mode: Union[bool, str] = True,
) -> BaseExtractionProvider:
    """Explicit offline-benchmark/unit-test factory bypassing the runtime server allowlist."""
    name = (provider_name or os.getenv("EXTRACTION_PROVIDER", "mock")).strip().lower()
    return _instantiate_provider(
        name,
        api_key=api_key,
        model_name=model_name,
        max_retries=max_retries,
        json_mode=json_mode,
    )


__all__ = [
    "BaseExtractionProvider",
    "ExtractionAuthenticationError",
    "ExtractionConfigurationError",
    "ExtractionInvalidModelResponseError",
    "ExtractionInvalidRequestError",
    "ExtractionProviderError",
    "ExtractionRateLimitError",
    "ExtractionSchemaValidationError",
    "ExtractionTimeoutError",
    "GeminiExtractionProvider",
    "MockExtractionProvider",
    "OpenRouterExtractionProvider",
    "get_benchmark_extraction_provider",
    "get_extraction_provider",
]
