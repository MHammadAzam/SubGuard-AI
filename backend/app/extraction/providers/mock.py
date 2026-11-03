"""Mock extraction provider for testing and offline development.

Allows configuring deterministic canned responses or simulating timeouts,
rate limits, and malformed provider responses without making external API calls.
"""

from __future__ import annotations

from typing import List, Optional

from app.extraction.providers.base import (
    BaseExtractionProvider,
    ExtractionProviderError,
    ExtractionRateLimitError,
    ExtractionSchemaValidationError,
    ExtractionTimeoutError,
)
from app.schemas.pdf import PDFPageInspection
from app.schemas.raw_extraction import (
    RawCoverageSection,
    RawEndorsement,
    RawEntityExtraction,
    RawEvidenceQuote,
    RawInsuranceExtraction,
)


class MockExtractionProvider(BaseExtractionProvider):
    """Configurable mock provider for test isolation."""

    def __init__(
        self,
        canned_response: Optional[RawInsuranceExtraction] = None,
        simulate_timeout: bool = False,
        simulate_rate_limit: bool = False,
        simulate_malformed: bool = False,
        simulate_failure: bool = False,
    ):
        self.canned_response = canned_response
        self.simulate_timeout = simulate_timeout
        self.simulate_rate_limit = simulate_rate_limit
        self.simulate_malformed = simulate_malformed
        self.simulate_failure = simulate_failure

    @property
    def provider_name(self) -> str:
        return "mock"

    def extract_insurance_data(
        self,
        document_id: str,
        pages: List[PDFPageInspection],
        timeout_seconds: float = 30.0,
    ) -> RawInsuranceExtraction:
        if self.simulate_timeout:
            raise ExtractionTimeoutError(
                f"Mock provider request timed out after {timeout_seconds}s.",
                provider_name=self.provider_name,
            )

        if self.simulate_rate_limit:
            raise ExtractionRateLimitError(
                "Mock provider rate limit exceeded (HTTP 429).",
                provider_name=self.provider_name,
            )

        if self.simulate_failure:
            raise ExtractionProviderError(
                "Mock provider internal server error (HTTP 500).",
                provider_name=self.provider_name,
                is_retryable=True,
            )

        if self.simulate_malformed:
            raise ExtractionSchemaValidationError(
                "Provider returned malformed payload missing mandatory keys.",
                provider_name=self.provider_name,
            )

        if self.canned_response is not None:
            return self.canned_response

        # Default synthetic extraction based on text in pages
        combined_text = "\n".join(p.text_content for p in pages)

        # Basic default response
        return RawInsuranceExtraction(
            document_type="coi",
            named_insured=RawEntityExtraction(
                name="Apex Electrical Contractors LLC",
                address="123 Industrial Pkwy, Austin, TX",
                evidence=RawEvidenceQuote(
                    page_number=1,
                    source_text="INSURED: Apex Electrical Contractors LLC",
                    confidence=0.98,
                ),
                confidence=0.98,
            ),
            certificate_holder=RawEntityExtraction(
                name="General Contractor Corp",
                address="500 Builder Way, Dallas, TX",
                evidence=RawEvidenceQuote(
                    page_number=1,
                    source_text="CERTIFICATE HOLDER: General Contractor Corp",
                    confidence=0.95,
                ),
                confidence=0.95,
            ),
            coverages=[
                RawCoverageSection(
                    coverage_type="general_liability",
                    policy_number="GL-98765432",
                    insurer_name="Travelers Indemnity Co",
                    each_occurrence="$1,000,000",
                    general_aggregate="$2,000,000",
                    effective_date="01/01/2025",
                    expiration_date="01/01/2026",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="EACH OCCURRENCE: $1,000,000  POLICY EFF: 01/01/2025  EXP: 01/01/2026",
                        confidence=0.96,
                    ),
                    confidence=0.96,
                )
            ],
            endorsements=[
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number="CG 20 10 04 13",
                    named_entities=["General Contractor Corp"],
                    is_blanket=False,
                    is_coi_checkbox_only=False,
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text="CG 20 10 04 13 Additional Insured - Scheduled Person or Organization",
                        confidence=0.94,
                    ),
                    confidence=0.94,
                )
            ],
            is_document_set_complete=True,
        )
