"""Package marker and factory for extraction providers."""

import os
from typing import Optional

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
from app.extraction.providers.mock import MockExtractionProvider
from app.extraction.providers.gemini import GeminiExtractionProvider
from app.extraction.providers.openrouter import OpenRouterExtractionProvider


def get_extraction_provider(
    provider_name: Optional[str] = None,
    api_key: Optional[str] = None,
    model_name: Optional[str] = None,
) -> BaseExtractionProvider:
    """Factory to retrieve the configured extraction provider."""
    name = (provider_name or os.getenv("EXTRACTION_PROVIDER", "mock")).strip().lower()

    if name == "mock":
        return MockExtractionProvider()
    elif name == "gemini":
        return GeminiExtractionProvider(api_key=api_key, model_name=model_name or "gemini-2.0-flash")
    elif name == "openrouter":
        return OpenRouterExtractionProvider(api_key=api_key, model_name=model_name or "nvidia/nemotron-3.5-super-vl:free")
    else:
        raise ValueError(f"Unsupported extraction provider: '{name}'. Supported: 'mock', 'gemini', 'openrouter'.")


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
    "get_extraction_provider",
]
