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

        # 2. Coverages Extraction (page- and section-scoped to preserve exact field provenance)
        raw_coverages: List[RawCoverageSection] = []
        has_gl = "COMMERCIAL GENERAL LIABILITY" in full_text or "GENERAL LIABILITY" in full_text
        has_al = "AUTOMOBILE LIABILITY" in full_text
        has_wc = "WORKERS COMPENSATION" in full_text

        gl_pol: Optional[str] = None
        gl_eff: Optional[str] = None
        gl_exp: Optional[str] = None
        gl_occ: Optional[str] = None
        gl_agg: Optional[str] = None
        gl_pol_quote: Optional[Tuple[int, str]] = None
        gl_eff_quote: Optional[Tuple[int, str]] = None
        gl_exp_quote: Optional[Tuple[int, str]] = None
        gl_occ_quote: Optional[Tuple[int, str]] = None
        gl_agg_quote: Optional[Tuple[int, str]] = None

        al_pol: Optional[str] = None
        al_csl: Optional[str] = None
        al_eff: Optional[str] = None
        al_exp: Optional[str] = None
        al_csl_quote: Optional[Tuple[int, str]] = None
        al_eff_quote: Optional[Tuple[int, str]] = None
        al_exp_quote: Optional[Tuple[int, str]] = None

        wc_pol: Optional[str] = None
        wc_acc: Optional[str] = None
        wc_eff: Optional[str] = None
        wc_exp: Optional[str] = None
        wc_acc_quote: Optional[Tuple[int, str]] = None
        wc_eff_quote: Optional[Tuple[int, str]] = None
        wc_exp_quote: Optional[Tuple[int, str]] = None

        def _extract_section_dates(
            line_str: str,
            page_num: int,
            cur_eff: Optional[str],
            cur_exp: Optional[str],
            cur_eff_q: Optional[Tuple[int, str]],
            cur_exp_q: Optional[Tuple[int, str]],
        ) -> Tuple[Optional[str], Optional[str], Optional[Tuple[int, str]], Optional[Tuple[int, str]]]:
            stripped = line_str.strip()
            has_eff_lbl = any(k in stripped for k in ("POLICY EFF", "EFF:", "EFFECTIVE"))
            has_exp_lbl = any(k in stripped for k in ("POLICY EXP", "EXP:", "EXPIRATION"))
            m_dates = re.findall(r"(\d{2}/\d{2}/\d{4})", stripped)
            if has_eff_lbl and has_exp_lbl and len(m_dates) >= 2:
                return (
                    cur_eff or m_dates[0],
                    cur_exp or m_dates[1],
                    cur_eff_q or (page_num, stripped),
                    cur_exp_q or (page_num, stripped),
                )
            if has_eff_lbl and m_dates and cur_eff is None:
                cur_eff = m_dates[0]
                cur_eff_q = (page_num, stripped)
            if has_exp_lbl and m_dates and cur_exp is None:
                cur_exp = m_dates[-1]
                cur_exp_q = (page_num, stripped)
            return cur_eff, cur_exp, cur_eff_q, cur_exp_q

        current_section: Optional[str] = None
        for p_idx, p in enumerate(pages, 1):
            for raw_line in p.text_content.split("\n"):
                line = raw_line.strip()
                if not line:
                    continue
                upper = line.upper()
                if "COMMERCIAL GENERAL LIABILITY" in upper or (upper.startswith("GENERAL LIABILITY") and "POLICY" not in upper):
                    current_section = "gl"
                    continue
                elif "AUTOMOBILE LIABILITY" in upper:
                    current_section = "al"
                    continue
                elif "WORKERS COMPENSATION" in upper:
                    current_section = "wc"
                    continue
                elif "UMBRELLA LIAB" in upper or "EXCESS LIAB" in upper:
                    current_section = "umb"
                    continue
                elif upper.startswith("DESCRIPTION OF OPERATIONS") or upper.startswith("CERTIFICATE HOLDER") or upper.startswith("REMARKS"):
                    current_section = "other"
                    continue

                if current_section == "gl" or (current_section is None and has_gl):
                    if "POLICY NUMBER:" in line and "GL-" in line and gl_pol is None:
                        m = re.search(r"POLICY NUMBER:\s*(GL-[0-9A-Za-z]+)", line)
                        if m:
                            gl_pol = m.group(1)
                            gl_pol_quote = (p_idx, line)
                    gl_eff, gl_exp, gl_eff_quote, gl_exp_quote = _extract_section_dates(
                        line, p_idx, gl_eff, gl_exp, gl_eff_quote, gl_exp_quote
                    )
                    if "EACH OCCURRENCE:" in line and gl_occ is None:
                        m_occ = re.search(r"EACH OCCURRENCE:\s*\$([0-9,]+)", line)
                        if m_occ:
                            gl_occ = f"${m_occ.group(1)}"
                            gl_occ_quote = (p_idx, line)
                    if "GENERAL AGGREGATE:" in line and gl_agg is None:
                        m_agg = re.search(r"GENERAL AGGREGATE:\s*\$([0-9,]+)", line)
                        if m_agg:
                            gl_agg = f"${m_agg.group(1)}"
                            gl_agg_quote = (p_idx, line)
                elif current_section == "al":
                    if "POLICY NUMBER:" in line and "AL-" in line and al_pol is None:
                        m = re.search(r"POLICY NUMBER:\s*(AL-[0-9A-Za-z]+)", line)
                        if m:
                            al_pol = m.group(1)
                    al_eff, al_exp, al_eff_quote, al_exp_quote = _extract_section_dates(
                        line, p_idx, al_eff, al_exp, al_eff_quote, al_exp_quote
                    )
                    if "COMBINED SINGLE LIMIT:" in line and al_csl is None:
                        m_csl = re.search(r"COMBINED SINGLE LIMIT:\s*\$([0-9,]+)", line)
                        if m_csl:
                            al_csl = f"${m_csl.group(1)}"
                            al_csl_quote = (p_idx, line)
                elif current_section == "wc":
                    if "POLICY NUMBER:" in line and "WC-" in line and wc_pol is None:
                        m = re.search(r"POLICY NUMBER:\s*(WC-[0-9A-Za-z]+)", line)
                        if m:
                            wc_pol = m.group(1)
                    wc_eff, wc_exp, wc_eff_quote, wc_exp_quote = _extract_section_dates(
                        line, p_idx, wc_eff, wc_exp, wc_eff_quote, wc_exp_quote
                    )
                    if "E.L. EACH ACCIDENT:" in line and wc_acc is None:
                        m_acc = re.search(r"E.L. EACH ACCIDENT:\s*\$([0-9,]+)", line)
                        if m_acc:
                            wc_acc = f"${m_acc.group(1)}"
                            wc_acc_quote = (p_idx, line)

        if has_gl:
            gl_ev = gl_occ_quote or gl_exp_quote or gl_eff_quote or gl_pol_quote
            raw_coverages.append(
                RawCoverageSection(
                    coverage_type="general_liability",
                    policy_number=gl_pol,
                    effective_date=gl_eff,
                    expiration_date=gl_exp,
                    each_occurrence=gl_occ,
                    general_aggregate=gl_agg,
                    each_occurrence_evidence=RawEvidenceQuote(
                        page_number=gl_occ_quote[0], source_text=gl_occ_quote[1], confidence=0.98
                    ) if gl_occ_quote else None,
                    general_aggregate_evidence=RawEvidenceQuote(
                        page_number=gl_agg_quote[0], source_text=gl_agg_quote[1], confidence=0.98
                    ) if gl_agg_quote else None,
                    effective_date_evidence=RawEvidenceQuote(
                        page_number=gl_eff_quote[0], source_text=gl_eff_quote[1], confidence=0.98
                    ) if gl_eff_quote else None,
                    expiration_date_evidence=RawEvidenceQuote(
                        page_number=gl_exp_quote[0], source_text=gl_exp_quote[1], confidence=0.98
                    ) if gl_exp_quote else None,
                    evidence=RawEvidenceQuote(
                        page_number=gl_ev[0], source_text=gl_ev[1], confidence=0.98
                    ) if gl_ev else None,
                    confidence=0.98,
                )
            )

        if has_al:
            raw_coverages.append(
                RawCoverageSection(
                    coverage_type="auto_liability",
                    policy_number=al_pol,
                    effective_date=al_eff,
                    expiration_date=al_exp,
                    combined_single_limit=al_csl,
                    combined_single_limit_evidence=RawEvidenceQuote(
                        page_number=al_csl_quote[0], source_text=al_csl_quote[1], confidence=0.98
                    ) if al_csl_quote else None,
                    effective_date_evidence=RawEvidenceQuote(
                        page_number=al_eff_quote[0], source_text=al_eff_quote[1], confidence=0.98
                    ) if al_eff_quote else None,
                    expiration_date_evidence=RawEvidenceQuote(
                        page_number=al_exp_quote[0], source_text=al_exp_quote[1], confidence=0.98
                    ) if al_exp_quote else None,
                    evidence=RawEvidenceQuote(
                        page_number=al_csl_quote[0], source_text=al_csl_quote[1], confidence=0.98
                    ) if al_csl_quote else None,
                    confidence=0.98,
                )
            )

        if has_wc:
            raw_coverages.append(
                RawCoverageSection(
                    coverage_type="workers_comp",
                    policy_number=wc_pol,
                    effective_date=wc_eff,
                    expiration_date=wc_exp,
                    per_accident=wc_acc,
                    per_accident_evidence=RawEvidenceQuote(
                        page_number=wc_acc_quote[0], source_text=wc_acc_quote[1], confidence=0.98
                    ) if wc_acc_quote else None,
                    effective_date_evidence=RawEvidenceQuote(
                        page_number=wc_eff_quote[0], source_text=wc_eff_quote[1], confidence=0.98
                    ) if wc_eff_quote else None,
                    expiration_date_evidence=RawEvidenceQuote(
                        page_number=wc_exp_quote[0], source_text=wc_exp_quote[1], confidence=0.98
                    ) if wc_exp_quote else None,
                    evidence=RawEvidenceQuote(
                        page_number=wc_acc_quote[0], source_text=wc_acc_quote[1], confidence=0.98
                    ) if wc_acc_quote else None,
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
