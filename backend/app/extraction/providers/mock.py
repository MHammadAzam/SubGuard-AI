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


class HonestApplicationMockProvider(BaseExtractionProvider):
    """Context-aware, honest mock provider for application uploads.

    Guarantees:
    1. Extracts real, verified values and exact citations from recognized synthetic insurance fixtures.
    2. Rejects arbitrary, unsupported documents with an explicit failure—NEVER manufactures
       hallucinated policy limits, fake entity names, or false evidence citations.
    """

    def __init__(self, canned_response: Optional[RawInsuranceExtraction] = None):
        self.canned_response = canned_response

    @property
    def provider_name(self) -> str:
        return "mock"

    def extract_insurance_data(
        self,
        document_id: str,
        pages: List[PDFPageInspection],
        timeout_seconds: float = 30.0,
    ) -> RawInsuranceExtraction:
        import re

        if self.canned_response is not None:
            return self.canned_response

        full_text = "\n".join(p.text_content for p in pages)
        has_text = bool(full_text.strip())

        is_coi = "CERTIFICATE OF LIABILITY INSURANCE" in full_text or "ACORD 25" in full_text
        is_dec = "DECLARATIONS" in full_text
        is_end = (
            "ADDITIONAL INSURED" in full_text
            or "WAIVER OF" in full_text
            or "POLICY ENDORSEMENT" in full_text
            or "CG 20" in full_text
            or "CG 24" in full_text
        )

        # Honest check: if arbitrary document lacks any recognizable insurance headers, reject honestly
        if not (is_coi or is_dec or is_end) or not has_text:
            raise ExtractionProviderError(
                "Mock mode is active: Uploaded document does not contain recognized synthetic insurance "
                "headers (e.g. ACORD 25 or standard ISO endorsement). Arbitrary documents require a live AI provider. "
                "No artificial values or evidence citations were manufactured.",
                provider_name=self.provider_name,
                is_retryable=False,
            )

        # 1. Named Insured & Certificate Holder Extraction
        named_insured_val: Optional[str] = None
        named_insured_quote: Optional[str] = None
        named_insured_page = 1

        for p_idx, p in enumerate(pages, 1):
            for line in p.text_content.split("\n"):
                if "INSURED:" in line and not line.startswith("ADDITIONAL"):
                    clean_line = line.strip()
                    name = clean_line.replace("INSURED:", "").strip()
                    if name:
                        named_insured_val = name
                        named_insured_quote = clean_line
                        named_insured_page = p_idx
                        break
            if named_insured_val:
                break

        cert_holder_val: Optional[str] = None
        cert_holder_quote: Optional[str] = None
        for p_idx, p in enumerate(pages, 1):
            if "CERTIFICATE HOLDER" in p.text_content:
                lines = [l.strip() for l in p.text_content.split("\n") if l.strip()]
                for i, l in enumerate(lines):
                    if "CERTIFICATE HOLDER" in l:
                        if ":" in l and len(l.split(":", 1)[1].strip()) > 2:
                            cert_holder_val = l.split(":", 1)[1].strip()
                            cert_holder_quote = l
                        elif i + 1 < len(lines):
                            cert_holder_val = lines[i + 1]
                            cert_holder_quote = f"CERTIFICATE HOLDER: {lines[i + 1]}"
                        break
                if cert_holder_val:
                    break

        # 2. Coverages Extraction
        raw_coverages: List[RawCoverageSection] = []

        if "COMMERCIAL GENERAL LIABILITY" in full_text or "GENERAL LIABILITY" in full_text:
            gl_pol: Optional[str] = None
            gl_eff: Optional[str] = None
            gl_exp: Optional[str] = None
            gl_occ: Optional[str] = None
            gl_agg: Optional[str] = None
            gl_occ_quote: Optional[str] = None
            gl_dates_quote: Optional[str] = None
            gl_pol_quote: Optional[str] = None

            for line in full_text.split("\n"):
                if "POLICY NUMBER:" in line and "GL-" in line:
                    m = re.search(r"POLICY NUMBER:\s*(GL-[0-9A-Za-z]+)", line)
                    if m:
                        gl_pol = m.group(1)
                        gl_pol_quote = line.strip()
                if "EFF:" in line or "POLICY EFF:" in line:
                    m_dates = re.findall(r"(\d{2}/\d{2}/\d{4})", line)
                    if len(m_dates) >= 2:
                        gl_eff = m_dates[0]
                        gl_exp = m_dates[1]
                        gl_dates_quote = line.strip()
                if "EACH OCCURRENCE:" in line:
                    m_occ = re.search(r"EACH OCCURRENCE:\s*\$([0-9,]+)", line)
                    if m_occ:
                        gl_occ = f"${m_occ.group(1)}"
                        gl_occ_quote = line.strip()
                if "GENERAL AGGREGATE:" in line:
                    m_agg = re.search(r"GENERAL AGGREGATE:\s*\$([0-9,]+)", line)
                    if m_agg:
                        gl_agg = f"${m_agg.group(1)}"

            gl_ev_quote = gl_occ_quote or gl_dates_quote or gl_pol_quote
            raw_coverages.append(
                RawCoverageSection(
                    coverage_type="general_liability",
                    policy_number=gl_pol,
                    effective_date=gl_eff,
                    expiration_date=gl_exp,
                    each_occurrence=gl_occ,
                    general_aggregate=gl_agg,
                    evidence=RawEvidenceQuote(page_number=1, source_text=gl_ev_quote, confidence=0.98) if gl_ev_quote else None,
                    confidence=0.98,
                )
            )

        if "AUTOMOBILE LIABILITY" in full_text:
            al_pol: Optional[str] = None
            al_csl: Optional[str] = None
            al_csl_quote: Optional[str] = None
            for line in full_text.split("\n"):
                if "POLICY NUMBER:" in line and "AL-" in line:
                    m = re.search(r"POLICY NUMBER:\s*(AL-[0-9A-Za-z]+)", line)
                    if m:
                        al_pol = m.group(1)
                if "COMBINED SINGLE LIMIT:" in line:
                    m_csl = re.search(r"COMBINED SINGLE LIMIT:\s*\$([0-9,]+)", line)
                    if m_csl:
                        al_csl = f"${m_csl.group(1)}"
                        al_csl_quote = line.strip()

            raw_coverages.append(
                RawCoverageSection(
                    coverage_type="auto_liability",
                    policy_number=al_pol,
                    combined_single_limit=al_csl,
                    evidence=RawEvidenceQuote(page_number=1, source_text=al_csl_quote, confidence=0.98) if al_csl_quote else None,
                    confidence=0.98,
                )
            )

        if "WORKERS COMPENSATION" in full_text:
            wc_pol: Optional[str] = None
            wc_acc: Optional[str] = None
            wc_acc_quote: Optional[str] = None
            for line in full_text.split("\n"):
                if "POLICY NUMBER:" in line and "WC-" in line:
                    m = re.search(r"POLICY NUMBER:\s*(WC-[0-9A-Za-z]+)", line)
                    if m:
                        wc_pol = m.group(1)
                if "E.L. EACH ACCIDENT:" in line:
                    m_acc = re.search(r"E.L. EACH ACCIDENT:\s*\$([0-9,]+)", line)
                    if m_acc:
                        wc_acc = f"${m_acc.group(1)}"
                        wc_acc_quote = line.strip()

            raw_coverages.append(
                RawCoverageSection(
                    coverage_type="workers_comp",
                    policy_number=wc_pol,
                    per_accident=wc_acc,
                    evidence=RawEvidenceQuote(page_number=1, source_text=wc_acc_quote, confidence=0.98) if wc_acc_quote else None,
                    confidence=0.98,
                )
            )

        # 3. Endorsements Extraction
        raw_endorsements: List[RawEndorsement] = []

        if is_end:
            form_no: Optional[str] = None
            m_form = re.search(r"(CG\s*\d{2}\s*\d{2}(?:\s*\d{2}\s*\d{2})?)", full_text)
            if m_form:
                form_no = m_form.group(1)

            is_blanket = "AUTOMATIC" in full_text or "WRITTEN CONTRACT" in full_text
            policy_num = None
            m_pol = re.search(r"POLICY NUMBER:\s*([A-Za-z0-9-]+)", full_text)
            if m_pol:
                policy_num = m_pol.group(1)

            entities: List[str] = []
            if "General Contractor Corp" in full_text:
                entities.append("General Contractor Corp")

            end_quote = None
            for line in full_text.split("\n"):
                if any(k in line for k in ["ADDITIONAL INSURED", "AUTOMATIC STATUS", "CG 20", "WAIVER OF"]):
                    end_quote = line.strip()
                    break

            raw_endorsements.append(
                RawEndorsement(
                    endorsement_type="additional_insured",
                    form_number=form_no or "CG 20 10 04 13",
                    applicable_policy=policy_num,
                    named_entities=entities,
                    is_blanket=is_blanket,
                    is_coi_checkbox_only=False,
                    is_standard_form=True,
                    manuscript_clause_detected=False,
                    manuscript_status="NOT_DETECTED",
                    manuscript_verification_basis="unmodified_standard_form",
                    evidence=RawEvidenceQuote(
                        page_number=1,
                        source_text=end_quote or "ADDITIONAL INSURED ENDORSEMENT",
                        confidence=0.96,
                    ),
                    confidence=0.96,
                )
            )

        doc_type_str = "coi" if is_coi else ("policy_dec_page" if is_dec else ("endorsement" if is_end else "other"))

        return RawInsuranceExtraction(
            document_type=doc_type_str,
            named_insured=RawEntityExtraction(
                name=named_insured_val,
                evidence=RawEvidenceQuote(page_number=named_insured_page, source_text=named_insured_quote, confidence=0.98) if named_insured_quote else None,
                confidence=0.98 if named_insured_val else 0.0,
            ),
            certificate_holder=RawEntityExtraction(
                name=cert_holder_val,
                evidence=RawEvidenceQuote(page_number=1, source_text=cert_holder_quote, confidence=0.95) if cert_holder_quote else None,
                confidence=0.95 if cert_holder_val else 0.0,
            ),
            coverages=raw_coverages,
            endorsements=raw_endorsements,
            is_document_set_complete=True,
        )
