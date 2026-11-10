"""Core Insurance Extraction Pipeline Service.

Coordinates:
1. Ingesting PDF inspection output
2. Handling scanned / textless documents via swappable OCR provider
3. Invoking configured AI extraction provider with strict schemas
4. Deterministically normalizing values (currency to cents, dates to date objects)
5. Verifying evidence references (preventing hallucinated quotes)
6. Assembling the compliant ExtractedDocumentSet schema
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from app.extraction.normalizer import (
    normalize_coverage_type,
    normalize_currency_to_cents,
    normalize_date,
    normalize_endorsement_type,
)
from app.extraction.ocr import BaseOCRProvider
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
from app.schemas.insurance import (
    CoverageExtraction,
    CoverageType,
    DateValue,
    DocumentType,
    EndorsementExtraction,
    EndorsementType,
    EntityInfo,
    EvidenceReference,
    ExtractedDocumentSet,
    ExtractionStatus,
    MoneyValue,
)
from app.schemas.pdf import DocumentProcessingStatus, PDFDocumentInspection, PDFPageInspection
from app.schemas.raw_extraction import (
    RawCoverageSection,
    RawEndorsement,
    RawEntityExtraction,
    RawEvidenceQuote,
    RawInsuranceExtraction,
)


_ENTITY_SUFFIX_CANON = {
    "llc": "llc",
    "llcs": "llc",
    "inc": "inc",
    "incorporated": "inc",
    "corp": "corp",
    "corporation": "corp",
    "co": "co",
    "company": "co",
    "ltd": "ltd",
    "limited": "ltd",
    "pllc": "pllc",
    "lp": "lp",
}
_ENTITY_STOPWORDS = {"the", "and", "of", "for", "&"}


def _clean_str(text: Optional[str]) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(cleaned.split())


def _canonical_entity_tokens(text: Optional[str]) -> List[str]:
    """Tokenize an entity string after collapsing dotted corporate abbreviations (L.L.C. -> llc)."""
    if not text:
        return []
    # Normalize dotted abbreviations such as L.L.C., P.L.L.C., L.P., Inc., Corp.
    pre = re.sub(r"\b([A-Za-z])(?:\.([A-Za-z])){1,4}\.?", lambda m: re.sub(r"\.", "", m.group(0)), text)
    raw_tokens = re.findall(r"[a-z0-9]+", pre.lower())
    return [_ENTITY_SUFFIX_CANON.get(t, t) for t in raw_tokens if t]


def _verify_entity_semantic_support(name: Optional[str], quote_text: Optional[str]) -> bool:
    """Verify quote_text explicitly contains the claimed entity name without single-token false positives."""
    if not name or not quote_text:
        return False

    name_tokens = _canonical_entity_tokens(name)
    quote_tokens = _canonical_entity_tokens(quote_text)
    if not name_tokens or not quote_tokens:
        return False

    # 1. Exact contiguous canonical token slice match (handles punctuation / case / Inc vs Incorporated)
    n_len = len(name_tokens)
    for i in range(len(quote_tokens) - n_len + 1):
        if quote_tokens[i : i + n_len] == name_tokens:
            return True

    # 2. Core tokens check: all non-suffix, non-stopword tokens of entity must appear in order
    # without contradicting suffix (e.g. Inc vs LLC)
    suffix_set = set(_ENTITY_SUFFIX_CANON.values())
    name_suffix = name_tokens[-1] if name_tokens[-1] in suffix_set else None
    core_tokens = [
        t for t in name_tokens
        if t not in suffix_set and t not in _ENTITY_STOPWORDS
    ]
    if not core_tokens:
        return False

    # Require ALL core tokens in order inside quote_tokens (single-token entity must match exact token)
    q_idx = 0
    matched_positions: List[int] = []
    for ct in core_tokens:
        found = False
        while q_idx < len(quote_tokens):
            if quote_tokens[q_idx] == ct:
                matched_positions.append(q_idx)
                q_idx += 1
                found = True
                break
            q_idx += 1
        if not found:
            return False

    # Ensure matched core tokens occur close together (within the same entity mention window)
    if len(matched_positions) >= 2 and (matched_positions[-1] - matched_positions[0]) > len(core_tokens) + 3:
        return False

    # If entity declares a corporate suffix, disallow an adjacent conflicting corporate suffix in quote
    if name_suffix is not None and matched_positions:
        tail_window = quote_tokens[matched_positions[-1] + 1 : matched_positions[-1] + 4]
        found_suffixes = [t for t in tail_window if t in suffix_set]
        if found_suffixes and name_suffix not in found_suffixes:
            return False

    return True


_LIMIT_FIELD_LABELS = {
    "each_occurrence": ("each occurrence", "per occurrence", "occurrence"),
    "general_aggregate": ("general aggregate", "aggregate"),
    "products_completed_ops": ("products", "completed op", "comp/op"),
    "personal_adv_injury": ("personal", "adv injury"),
    "combined_single_limit": ("combined single", "csl"),
    "per_accident": ("each accident", "per accident"),
    "disease_each_employee": ("each employee",),
    "disease_policy_limit": ("policy limit",),
}


_CURRENCY_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9\-\.])(?:\$\s*)?[0-9][0-9,\.]*(?:\s*(?:million|m|k))?(?![A-Za-z0-9\-\./])",
    re.IGNORECASE,
)

_TEXT_DATE_CANDIDATE_RE = re.compile(
    r"\b\d{4}-\d{1,2}-\d{1,2}\b"
    r"|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"
    r"|\b(?:January|Jan|February|Feb|March|Mar|April|Apr|May|June|Jun|"
    r"July|Jul|August|Aug|September|Sep|Sept|October|Oct|November|Nov|December|Dec)\.?\s+\d{1,2},?\s+\d{4}\b"
    r"|\b\d{1,2}\s+(?:January|Jan|February|Feb|March|Mar|April|Apr|May|June|Jun|"
    r"July|Jul|August|Aug|September|Sep|Sept|October|Oct|November|Nov|December|Dec)\.?\s+\d{4}\b",
    re.IGNORECASE,
)


def _extract_normalized_amounts_cents(text: Optional[str]) -> List[int]:
    """Extract and normalize monetary amount tokens present in text into integer cents."""
    if not text:
        return []
    values: List[int] = []
    for match in _CURRENCY_TOKEN_RE.finditer(text):
        token = match.group(0).strip()
        # Bare numbers without '$' or K/M or commas must have at least 4 digits to represent currency limits
        if "$" not in token and "," not in token and not re.search(r"[mk]\b", token, re.I):
            plain_digits = token.split(".")[0]
            if len(plain_digits) < 4:
                continue
        cents, is_amb, _ = normalize_currency_to_cents(token)
        if not is_amb and cents is not None:
            values.append(cents)
    return values


def _extract_normalized_dates(text: Optional[str]) -> List:
    """Extract and normalize valid date tokens present in text."""
    if not text:
        return []
    parsed_dates = []
    for match in _TEXT_DATE_CANDIDATE_RE.finditer(text):
        parsed_d, is_amb, _ = normalize_date(match.group(0))
        if not is_amb and parsed_d is not None:
            parsed_dates.append(parsed_d)
    return parsed_dates


def _verify_money_field_support(
    field_name: str,
    amount_cents: int,
    raw_str: Optional[str],
    quote_text: Optional[str],
) -> bool:
    """Verify that quote_text genuinely supports a specific coverage limit field by normalized value."""
    if not quote_text or not quote_text.strip():
        return False
    lower_q = quote_text.lower()
    target_labels = _LIMIT_FIELD_LABELS.get(field_name, ())
    has_target_label = any(lbl in lower_q for lbl in target_labels)
    has_other_label = any(
        lbl in lower_q
        for other_field, labels in _LIMIT_FIELD_LABELS.items()
        if other_field != field_name
        for lbl in labels
        if lbl not in target_labels
    )
    if has_other_label and not has_target_label:
        return False

    quote_amounts = _extract_normalized_amounts_cents(quote_text)
    if amount_cents in quote_amounts:
        return True

    # Fallback: exact raw string token match (only if non-numeric / textual match) bounded by word edges
    if raw_str and raw_str.strip():
        escaped = re.escape(raw_str.strip())
        if re.search(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", quote_text, re.I):
            return True
    return False


def _verify_date_field_support(
    date_field: str,
    parsed_date,
    raw_str: Optional[str],
    quote_text: Optional[str],
) -> bool:
    """Verify that quote_text genuinely supports a policy effective or expiration date."""
    if not quote_text or not quote_text.strip() or parsed_date is None:
        return False
    lower_q = quote_text.lower()
    if date_field == "effective":
        other_only = ("expir" in lower_q or "exp:" in lower_q) and not any(
            k in lower_q for k in ("eff:", "effective", "policy eff", "inception", "from")
        )
        if other_only:
            return False
    elif date_field == "expiration":
        other_only = ("eff:" in lower_q or "effective" in lower_q or "policy eff" in lower_q) and not any(
            k in lower_q for k in ("exp:", "expir", "policy exp", "until", "through", "to")
        )
        if other_only:
            return False

    quote_dates = _extract_normalized_dates(quote_text)
    if parsed_date in quote_dates:
        return True

    if raw_str and raw_str.strip():
        escaped = re.escape(raw_str.strip().lower())
        if re.search(rf"(?<![0-9]){escaped}(?![0-9])", lower_q):
            return True
    return False


def _verify_coverage_semantic_support(cov_raw: RawCoverageSection, quote_text: Optional[str]) -> bool:
    if not quote_text:
        return False
    c_quote = _clean_str(quote_text)
    quote_amounts = set(_extract_normalized_amounts_cents(quote_text))
    limits = [
        cov_raw.each_occurrence,
        cov_raw.general_aggregate,
        cov_raw.combined_single_limit,
        cov_raw.products_completed_ops,
        cov_raw.per_accident,
        cov_raw.disease_policy_limit,
    ]
    for lim in limits:
        if lim:
            cents, is_amb, _ = normalize_currency_to_cents(lim)
            if not is_amb and cents is not None and cents in quote_amounts:
                return True
            c_lim = _clean_str(lim)
            if c_lim and re.search(rf"\b{re.escape(c_lim)}\b", c_quote):
                return True

    if cov_raw.policy_number:
        c_pol = _clean_str(cov_raw.policy_number)
        if c_pol and c_pol in c_quote:
            return True

    if any(k in c_quote for k in ["liability", "general", "auto", "workers", "umbrella", "excess", "occurrence", "aggregate", "csl"]):
        return True

    return False


def _verify_endorsement_semantic_support(end_raw: RawEndorsement, quote_text: Optional[str]) -> bool:
    """Verify that cited evidence text specifically supports the claimed endorsement type.
    
    Rejects generic insurance terms (e.g. standalone 'insured', 'additional') that frequently appear
    in COI headers without indicating actual endorsement coverage.
    """
    if not quote_text:
        return False
    c_quote = _clean_str(quote_text)

    # 1. Recognized form number check
    if end_raw.form_number:
        c_form = _clean_str(end_raw.form_number)
        if c_form and len(c_form) >= 4 and (c_form in c_quote or c_quote in c_form):
            return True

    end_type = (end_raw.endorsement_type or "").lower()

    # 2. Additional Insured specific indicators
    if "additional_insured" in end_type:
        ai_indicators = [
            "additional insured",
            "additional insureds",
            "scheduled person",
            "automatic status",
            "owners, lessees or contractors",
            "owners lessees or contractors",
            "who is an insured is amended",
            "completed operations",
            "cg 20 10",
            "cg 20 37",
            "cg 20 26",
            "cg 20 38",
            "cg2010",
            "cg2037",
        ]
        if any(ind in c_quote for ind in ai_indicators):
            return True

    # 3. Waiver of Subrogation specific indicators
    elif "waiver" in end_type or "subrogation" in end_type:
        wos_indicators = [
            "waiver of subrogation",
            "waiver of our right",
            "right of recovery",
            "rights of recovery",
            "subrogation is waived",
            "subrogation waived",
            "transfer of rights of recovery",
            "cg 24 04",
            "cg2404",
            "wc 00 03",
            "wc0003",
        ]
        if any(ind in c_quote for ind in wos_indicators):
            return True

    # 4. Primary and Non-Contributory specific indicators
    elif "primary" in end_type or "noncontributory" in end_type:
        pnc_indicators = [
            "primary and noncontributory",
            "primary and non-contributory",
            "primary/non-contributory",
            "primary & non-contributory",
            "other insurance condition is amended",
        ]
        if any(ind in c_quote for ind in pnc_indicators):
            return True

    # 5. General recognized endorsement heading formulas
    general_endorsement_headings = [
        "this endorsement changes the policy",
        "endorsement changes the policy",
        "policy endorsement",
        "commercial general liability endorsement",
    ]
    if any(h in c_quote for h in general_endorsement_headings):
        return True

    return False


MANUSCRIPT_RESTRICTIVE_PATTERNS = [
    r"\bresidential\b[^\.\n]*?\b(?:exclusion|excluded|limitation)\b",
    r"\b(?:exclusion|excluded)\b[^\.\n]*?\bresidential\b",
    r"\bsubcontractor\b[^\.\n]*?\b(?:exclusion|warranty|limitation)\b",
    r"\baction\s+over\b[^\.\n]*?\bexclusion\b",
    r"\bcross\s+suits?\b[^\.\n]*?\bexclusion\b",
    r"\bdesignated\s+(?:work|operations?)\b[^\.\n]*?\bexclusion\b",
    r"\bheight\b[^\.\n]*?\b(?:limitation|exclusion)\b",
    r"\broofing\b[^\.\n]*?\bexclusion\b",
    r"\bprior\s+acts?\b[^\.\n]*?\bexclusion\b",
    r"\bclassification\s+limitation\b",
    r"\bcustom\s+exclusion\b",
    r"\bnon-standard\s+limitation\b",
    r"\bdoes\s+not\s+apply\s+to\s+(?:any\s+)?(?:claims?|work|operations?)\b",
    r"\bamended\s+to\s+exclude\b",
    r"\bsolely\s+to\s+the\s+extent\b",
]


class ExtractionPipelineResult:
    """Wrapper containing extracted document set and pipeline processing status."""

    def __init__(
        self,
        document_set: Optional[ExtractedDocumentSet],
        processing_status: DocumentProcessingStatus,
        requires_ocr: bool = False,
        errors: Optional[List[str]] = None,
        error_category: Optional[str] = None,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        error_code: Optional[str] = None,
        status_code: Optional[int] = None,
        request_id: Optional[str] = None,
        is_retryable: Optional[bool] = None,
    ):
        self.document_set = document_set
        self.processing_status = processing_status
        self.requires_ocr = requires_ocr
        self.errors = errors or []
        self.error_category = error_category
        self.error_type = error_type
        self.error_message = error_message
        self.error_code = error_code
        self.status_code = status_code
        self.request_id = request_id
        self.is_retryable = is_retryable


class ExtractionPipelineService:
    """Service orchestrating AI extraction, normalization, and evidence resolution."""

    def __init__(
        self,
        extraction_provider: BaseExtractionProvider,
        ocr_provider: Optional[BaseOCRProvider] = None,
    ):
        self.extraction_provider = extraction_provider
        self.ocr_provider = ocr_provider

    def _resolve_evidence(
        self,
        document_id: str,
        document_type: DocumentType,
        quote: Optional[RawEvidenceQuote],
        pages: List[PDFPageInspection],
    ) -> Optional[EvidenceReference]:
        """Verify that an evidence quote actually matches or references a valid page.
        
        Does not fabricate bounding boxes or nonexistent page numbers.
        """
        if not quote or not quote.source_text.strip():
            return None

        # Check valid page number
        valid_page_nums = {p.page_number for p in pages}
        if quote.page_number not in valid_page_nums:
            return None

        matching_page = next((p for p in pages if p.page_number == quote.page_number), None)
        bounding_box = None

        if matching_page:
            # Verify quote is actually present in page text content or spans
            cleaned_page_text = (matching_page.text_content or "").lower()
            cleaned_quote = quote.source_text.strip().lower()

            if cleaned_page_text and cleaned_quote not in cleaned_page_text:
                # Helper for normalized token extraction
                def _normalize_tokens(s: str) -> List[str]:
                    return [w for w in re.findall(r'\b[a-z0-9]+\b', s.lower()) if len(w) > 1]

                quote_tokens = _normalize_tokens(cleaned_quote)
                page_tokens = _normalize_tokens(cleaned_page_text)

                if not quote_tokens:
                    return None

                norm_quote = " ".join(quote_tokens)
                norm_page = " ".join(page_tokens)

                # 1. Normalized continuous text match
                is_matched = norm_quote in norm_page

                # 2. Page span matching
                if not is_matched:
                    for span in matching_page.spans:
                        span_text = span.text.strip().lower()
                        if not span_text:
                            continue
                        span_tokens = _normalize_tokens(span_text)
                        norm_span = " ".join(span_tokens)
                        if norm_quote in norm_span or (len(quote_tokens) >= 3 and norm_span in norm_quote and len(span_tokens) >= 3):
                            is_matched = True
                            bounding_box = span.bounding_box
                            break

                # 3. Calibrated conservative sequence verification (eliminating single-bigram acceptance)
                if not is_matched:
                    # Short quotes (<= 3 tokens) must match as an exact continuous sequence in the document text
                    if len(quote_tokens) <= 3:
                        if norm_quote in norm_page:
                            is_matched = True
                    else:
                        # Longer quotes (>= 4 tokens): require at least a 4-token contiguous sequence from the quote
                        # to exist verbatim in the page text (eliminates acceptance of isolated 2-word bigrams).
                        has_contiguous_4gram = any(
                            " ".join(quote_tokens[i : i + 4]) in norm_page
                            for i in range(len(quote_tokens) - 3)
                        )
                        if has_contiguous_4gram:
                            is_matched = True

                if not is_matched:
                    return None

            # Try to locate matching text span on the page for exact coordinates if not already found
            if not bounding_box:
                for span in matching_page.spans:
                    if span.text.strip().lower() in cleaned_quote or cleaned_quote in span.text.strip().lower():
                        bounding_box = span.bounding_box
                        break

        ocr_prov = getattr(matching_page, "ocr_provider", None) if matching_page else None
        ocr_conf = getattr(matching_page, "ocr_confidence", None) if matching_page else None
        proc_stat = "ocr_extracted" if ocr_prov else "native_digital"

        return EvidenceReference(
            document_id=document_id,
            document_type=document_type,
            page_number=quote.page_number,
            source_text=quote.source_text.strip(),
            bounding_box=bounding_box,
            ocr_provider=ocr_prov,
            confidence=ocr_conf,
            processing_status=proc_stat,
        )

    def _build_money_value(
        self,
        raw_str: Optional[str],
        confidence: Optional[float],
        evidence: Optional[EvidenceReference] = None,
    ) -> Optional[MoneyValue]:
        if not raw_str or not raw_str.strip():
            return None

        cents, is_ambiguous, reason = normalize_currency_to_cents(raw_str)
        if cents is None or is_ambiguous:
            # If ambiguous or unparseable, we still return MoneyValue with low confidence so compliance flags it
            return None

        return MoneyValue(
            amount_cents=cents,
            raw_text=raw_str.strip(),
            confidence=confidence,
            evidence=evidence,
        )

    def _build_date_value(
        self,
        raw_str: Optional[str],
        confidence: Optional[float],
        evidence: Optional[EvidenceReference] = None,
    ) -> Optional[DateValue]:
        if not raw_str or not raw_str.strip():
            return None

        parsed_date, is_ambiguous, reason = normalize_date(raw_str)
        if parsed_date is None or is_ambiguous:
            return None

        return DateValue(
            date=parsed_date,
            raw_text=raw_str.strip(),
            confidence=confidence,
            evidence=evidence,
        )

    def _build_field_money_value(
        self,
        field_name: str,
        raw_str: Optional[str],
        field_quote: Optional[RawEvidenceQuote],
        fallback_cov_ev: Optional[EvidenceReference],
        confidence: Optional[float],
        document_id: str,
        document_type: DocumentType,
        pages: List[PDFPageInspection],
    ) -> Optional[MoneyValue]:
        mv = self._build_money_value(raw_str, confidence)
        if mv is None:
            return None

        resolved_ev: Optional[EvidenceReference] = None
        if field_quote is not None:
            cand = self._resolve_evidence(
                document_id=document_id,
                document_type=document_type,
                quote=field_quote,
                pages=pages,
            )
            if cand and _verify_money_field_support(field_name, mv.amount_cents, raw_str, cand.source_text):
                resolved_ev = cand
        elif fallback_cov_ev is not None and _verify_money_field_support(
            field_name, mv.amount_cents, raw_str, fallback_cov_ev.source_text
        ):
            resolved_ev = fallback_cov_ev

        mv.evidence = resolved_ev
        return mv

    def _build_field_date_value(
        self,
        date_field: str,
        raw_str: Optional[str],
        field_quote: Optional[RawEvidenceQuote],
        fallback_cov_ev: Optional[EvidenceReference],
        confidence: Optional[float],
        document_id: str,
        document_type: DocumentType,
        pages: List[PDFPageInspection],
    ) -> Optional[DateValue]:
        dv = self._build_date_value(raw_str, confidence)
        if dv is None:
            return None

        resolved_ev: Optional[EvidenceReference] = None
        if field_quote is not None:
            cand = self._resolve_evidence(
                document_id=document_id,
                document_type=document_type,
                quote=field_quote,
                pages=pages,
            )
            if cand and _verify_date_field_support(date_field, dv.date, raw_str, cand.source_text):
                resolved_ev = cand
        elif fallback_cov_ev is not None and _verify_date_field_support(
            date_field, dv.date, raw_str, fallback_cov_ev.source_text
        ):
            resolved_ev = fallback_cov_ev

        dv.evidence = resolved_ev
        return dv

    def process_inspection(
        self,
        inspection: PDFDocumentInspection,
        subcontractor_id: str,
    ) -> ExtractionPipelineResult:
        """Run the end-to-end extraction pipeline on an inspected PDF document."""
        pages_to_process = list(inspection.pages)

        # Multi-signal page routing: route only scanned pages requiring OCR
        is_doc_scanned = inspection.is_scanned or inspection.processing_status == DocumentProcessingStatus.REQUIRES_OCR
        scanned_pages = [
            p for p in pages_to_process
            if p.requires_ocr or p.is_scanned or (is_doc_scanned and not (p.text_content or "").strip())
        ]
        if is_doc_scanned and not scanned_pages and not any((p.text_content or "").strip() for p in pages_to_process):
            scanned_pages = list(pages_to_process)

        digital_pages = [p for p in pages_to_process if p not in scanned_pages]

        if scanned_pages:
            if self.ocr_provider is None:
                scanned_nums = [p.page_number for p in scanned_pages]
                return ExtractionPipelineResult(
                    document_set=None,
                    processing_status=DocumentProcessingStatus.REQUIRES_OCR,
                    requires_ocr=True,
                    errors=[
                        f"Document '{inspection.filename}' contains {len(scanned_pages)} scanned page(s) "
                        f"(pages {scanned_nums}) requiring OCR. No OCR provider is configured to transcribe page content."
                    ],
                )

            # Route ONLY scanned pages to OCR, preserving native extraction for digital pages
            try:
                ocr_results = self.ocr_provider.perform_ocr_on_pages(scanned_pages)
                ocr_map = {p.page_number: p for p in ocr_results}
                pages_to_process = [ocr_map.get(p.page_number, p) for p in pages_to_process]
            except Exception as ocr_exc:
                clean_ocr_err = sanitize_error_message(str(ocr_exc))
                return ExtractionPipelineResult(
                    document_set=None,
                    processing_status=DocumentProcessingStatus.FAILED,
                    errors=[f"OCR processing failed for document '{inspection.filename}': {clean_ocr_err}"],
                    error_category="ocr_failure",
                    error_type=ocr_exc.__class__.__name__,
                    error_message=clean_ocr_err,
                )

        # Call extraction provider
        try:
            raw_result = self.extraction_provider.extract_insurance_data(
                document_id=inspection.document_id,
                pages=pages_to_process,
            )
        except Exception as exc:
            clean_msg = sanitize_error_message(str(exc))
            exc_type = exc.__class__.__name__
            status_code = getattr(exc, "status_code", None)
            error_code = getattr(exc, "error_code", None) or (str(status_code) if status_code else None)
            request_id = getattr(exc, "request_id", None)
            is_retryable = getattr(exc, "is_retryable", False)

            # Determine granular category
            category = getattr(exc, "error_category", None)
            if not category:
                if isinstance(exc, ExtractionAuthenticationError) or status_code == 401 or "401" in clean_msg.lower() or "unauthorized" in clean_msg.lower():
                    category = "authentication_error"
                elif isinstance(exc, ExtractionConfigurationError) or "api key is not configured" in clean_msg.lower():
                    category = "configuration_error"
                elif isinstance(exc, ExtractionRateLimitError) or status_code == 429 or "429" in clean_msg.lower():
                    category = "rate_limit_error"
                elif isinstance(exc, ExtractionTimeoutError) or status_code == 408 or "timeout" in clean_msg.lower():
                    category = "timeout_error"
                elif isinstance(exc, ExtractionInvalidRequestError) or status_code in (400, 404, 422):
                    category = "invalid_request"
                elif isinstance(exc, ExtractionInvalidModelResponseError):
                    category = "invalid_model_response"
                elif isinstance(exc, ExtractionSchemaValidationError):
                    category = "schema_validation_error"
                elif isinstance(exc, ExtractionProviderError):
                    category = "provider_error"
                else:
                    category = "provider_error"

            prov_name = self.extraction_provider.provider_name
            if clean_msg.lower().startswith(prov_name.lower()) or "ai extraction provider" in clean_msg.lower():
                err_line = clean_msg
            else:
                err_line = f"AI extraction provider '{prov_name}' failed: {clean_msg}"

            return ExtractionPipelineResult(
                document_set=None,
                processing_status=DocumentProcessingStatus.FAILED,
                errors=[err_line],
                error_category=category,
                error_type=exc_type,
                error_message=clean_msg,
                error_code=error_code,
                status_code=status_code,
                request_id=request_id,
                is_retryable=is_retryable,
            )

        # Map and normalize into ExtractedDocumentSet
        doc_type = inspection.classified_type

        # Named Insured
        named_insured_info = None
        if raw_result.named_insured and raw_result.named_insured.name:
            ev = self._resolve_evidence(
                document_id=inspection.document_id,
                document_type=doc_type,
                quote=raw_result.named_insured.evidence,
                pages=pages_to_process,
            )
            if not ev:
                status = ExtractionStatus.UNCERTAIN
            elif _verify_entity_semantic_support(raw_result.named_insured.name, ev.source_text):
                status = ExtractionStatus.EXTRACTED
            else:
                status = ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT

            named_insured_info = EntityInfo(
                name=raw_result.named_insured.name.strip(),
                address=raw_result.named_insured.address,
                evidence=ev,
                extraction_status=status,
                confidence=raw_result.named_insured.confidence,
            )

        # Certificate Holder
        cert_holder_info = None
        if raw_result.certificate_holder and raw_result.certificate_holder.name:
            ev = self._resolve_evidence(
                document_id=inspection.document_id,
                document_type=doc_type,
                quote=raw_result.certificate_holder.evidence,
                pages=pages_to_process,
            )
            if not ev:
                status = ExtractionStatus.UNCERTAIN
            elif _verify_entity_semantic_support(raw_result.certificate_holder.name, ev.source_text):
                status = ExtractionStatus.EXTRACTED
            else:
                status = ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT

            cert_holder_info = EntityInfo(
                name=raw_result.certificate_holder.name.strip(),
                address=raw_result.certificate_holder.address,
                evidence=ev,
                extraction_status=status,
                confidence=raw_result.certificate_holder.confidence,
            )

        # Coverages
        coverages: List[CoverageExtraction] = []
        for raw_cov in raw_result.coverages:
            norm_cov_type = normalize_coverage_type(raw_cov.coverage_type)
            if not norm_cov_type:
                continue

            ev = self._resolve_evidence(
                document_id=inspection.document_id,
                document_type=doc_type,
                quote=raw_cov.evidence,
                pages=pages_to_process,
            )
            if not ev:
                status = ExtractionStatus.UNCERTAIN
            elif _verify_coverage_semantic_support(raw_cov, ev.source_text):
                status = ExtractionStatus.EXTRACTED
            else:
                status = ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT

            cov = CoverageExtraction(
                coverage_type=norm_cov_type,
                policy_number=raw_cov.policy_number,
                insurer_name=raw_cov.insurer_name,
                each_occurrence=self._build_field_money_value(
                    "each_occurrence", raw_cov.each_occurrence, raw_cov.each_occurrence_evidence,
                    ev, raw_cov.confidence, inspection.document_id, doc_type, pages_to_process,
                ),
                general_aggregate=self._build_field_money_value(
                    "general_aggregate", raw_cov.general_aggregate, raw_cov.general_aggregate_evidence,
                    ev, raw_cov.confidence, inspection.document_id, doc_type, pages_to_process,
                ),
                products_completed_ops=self._build_field_money_value(
                    "products_completed_ops", raw_cov.products_completed_ops, raw_cov.products_completed_ops_evidence,
                    ev, raw_cov.confidence, inspection.document_id, doc_type, pages_to_process,
                ),
                personal_adv_injury=self._build_field_money_value(
                    "personal_adv_injury", raw_cov.personal_adv_injury, raw_cov.personal_adv_injury_evidence,
                    ev, raw_cov.confidence, inspection.document_id, doc_type, pages_to_process,
                ),
                combined_single_limit=self._build_field_money_value(
                    "combined_single_limit", raw_cov.combined_single_limit, raw_cov.combined_single_limit_evidence,
                    ev, raw_cov.confidence, inspection.document_id, doc_type, pages_to_process,
                ),
                per_accident=self._build_field_money_value(
                    "per_accident", raw_cov.per_accident, raw_cov.per_accident_evidence,
                    ev, raw_cov.confidence, inspection.document_id, doc_type, pages_to_process,
                ),
                disease_each_employee=self._build_field_money_value(
                    "disease_each_employee", raw_cov.disease_each_employee, raw_cov.disease_each_employee_evidence,
                    ev, raw_cov.confidence, inspection.document_id, doc_type, pages_to_process,
                ),
                disease_policy_limit=self._build_field_money_value(
                    "disease_policy_limit", raw_cov.disease_policy_limit, raw_cov.disease_policy_limit_evidence,
                    ev, raw_cov.confidence, inspection.document_id, doc_type, pages_to_process,
                ),
                effective_date=self._build_field_date_value(
                    "effective", raw_cov.effective_date, raw_cov.effective_date_evidence,
                    ev, raw_cov.confidence, inspection.document_id, doc_type, pages_to_process,
                ),
                expiration_date=self._build_field_date_value(
                    "expiration", raw_cov.expiration_date, raw_cov.expiration_date_evidence,
                    ev, raw_cov.confidence, inspection.document_id, doc_type, pages_to_process,
                ),
                evidence=ev,
                extraction_status=status,
            )
            coverages.append(cov)

        # Endorsements
        full_doc_text = " ".join(p.text_content for p in pages_to_process)
        endorsements: List[EndorsementExtraction] = []
        for raw_end in raw_result.endorsements:
            norm_end_type = normalize_endorsement_type(raw_end.endorsement_type)
            if not norm_end_type:
                continue

            ev = self._resolve_evidence(
                document_id=inspection.document_id,
                document_type=doc_type,
                quote=raw_end.evidence,
                pages=pages_to_process,
            )
            if not ev:
                status = ExtractionStatus.UNCERTAIN
            elif _verify_endorsement_semantic_support(raw_end, ev.source_text):
                status = ExtractionStatus.EXTRACTED
            else:
                status = ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT

            # Deterministic page-text inspection for restrictive manuscript wording
            manuscript_detected = raw_end.manuscript_clause_detected
            manuscript_status = raw_end.manuscript_status
            restriction_code = raw_end.restriction_reason_code
            restriction_desc = raw_end.restriction_description
            verification_basis = getattr(raw_end, "manuscript_verification_basis", None)

            matched_restrictive = None
            for pat in MANUSCRIPT_RESTRICTIVE_PATTERNS:
                m = re.search(pat, full_doc_text, re.I)
                if m:
                    matched_restrictive = m.group(0)
                    break

            if matched_restrictive:
                manuscript_detected = True
                manuscript_status = "DETECTED"
                restriction_code = restriction_code or "MANUSCRIPT_RESTRICTION_DETECTED"
                restriction_desc = restriction_desc or f"Restrictive manuscript provision or exclusion detected: '{matched_restrictive}'"
                verification_basis = "detected_restriction"

            # Date evidence grounding verification via parsed dates (never digit-only substrings)
            end_date_evidence: List[EvidenceReference] = []

            def _ground_endorsement_date(parsed_d) -> Optional[EvidenceReference]:
                if ev is not None and parsed_d in _extract_normalized_dates(ev.source_text):
                    return ev
                for page in pages_to_process:
                    p_text = page.text_content or ""
                    if parsed_d not in _extract_normalized_dates(p_text):
                        continue
                    for span in page.spans:
                        if span.text and parsed_d in _extract_normalized_dates(span.text):
                            return EvidenceReference(
                                document_id=inspection.document_id,
                                document_type=doc_type,
                                page_number=page.page_number,
                                source_text=span.text.strip(),
                                bounding_box=span.bounding_box,
                                ocr_provider=getattr(page, "ocr_provider", None),
                                confidence=getattr(page, "ocr_confidence", None),
                                processing_status="ocr_extracted" if getattr(page, "ocr_provider", None) else "native_digital",
                            )
                    for line in p_text.splitlines():
                        if parsed_d in _extract_normalized_dates(line):
                            return EvidenceReference(
                                document_id=inspection.document_id,
                                document_type=doc_type,
                                page_number=page.page_number,
                                source_text=line.strip(),
                                ocr_provider=getattr(page, "ocr_provider", None),
                                confidence=getattr(page, "ocr_confidence", None),
                                processing_status="ocr_extracted" if getattr(page, "ocr_provider", None) else "native_digital",
                            )
                return None

            eff_val = self._build_date_value(raw_end.effective_date, raw_end.confidence)
            if eff_val is not None:
                grounded_eff = _ground_endorsement_date(eff_val.date)
                if grounded_eff is None:
                    eff_val = None
                elif grounded_eff not in end_date_evidence:
                    end_date_evidence.append(grounded_eff)

            exp_val = self._build_date_value(raw_end.expiration_date, raw_end.confidence)
            if exp_val is not None:
                grounded_exp = _ground_endorsement_date(exp_val.date)
                if grounded_exp is None:
                    exp_val = None
                elif grounded_exp not in end_date_evidence:
                    end_date_evidence.append(grounded_exp)

            # Ground endorsement named_entities against verified source document text
            verified_entity_evidence: List[EvidenceReference] = []
            for ent_name in raw_end.named_entities:
                if not ent_name or not ent_name.strip():
                    continue
                if ev is not None and _verify_entity_semantic_support(ent_name, ev.source_text):
                    if ev not in verified_entity_evidence:
                        verified_entity_evidence.append(ev)
                    continue
                for page in pages_to_process:
                    p_text = page.text_content or ""
                    if not _verify_entity_semantic_support(ent_name, p_text):
                        continue
                    matched_ref: Optional[EvidenceReference] = None
                    for span in page.spans:
                        if span.text and _verify_entity_semantic_support(ent_name, span.text):
                            matched_ref = EvidenceReference(
                                document_id=inspection.document_id,
                                document_type=doc_type,
                                page_number=page.page_number,
                                source_text=span.text.strip(),
                                bounding_box=span.bounding_box,
                                ocr_provider=getattr(page, "ocr_provider", None),
                                confidence=getattr(page, "ocr_confidence", None),
                                processing_status="ocr_extracted" if getattr(page, "ocr_provider", None) else "native_digital",
                            )
                            break
                    if matched_ref is None:
                        for line in p_text.splitlines():
                            if _verify_entity_semantic_support(ent_name, line):
                                matched_ref = EvidenceReference(
                                    document_id=inspection.document_id,
                                    document_type=doc_type,
                                    page_number=page.page_number,
                                    source_text=line.strip(),
                                    ocr_provider=getattr(page, "ocr_provider", None),
                                    confidence=getattr(page, "ocr_confidence", None),
                                    processing_status="ocr_extracted" if getattr(page, "ocr_provider", None) else "native_digital",
                                )
                                break
                    if matched_ref is not None:
                        if matched_ref not in verified_entity_evidence:
                            verified_entity_evidence.append(matched_ref)
                        break

            end = EndorsementExtraction(
                endorsement_type=norm_end_type,
                document_type=doc_type,
                form_number=raw_end.form_number,
                applicable_policy=raw_end.applicable_policy,
                named_entities=raw_end.named_entities,
                key_terms=raw_end.key_terms,
                is_blanket=raw_end.is_blanket,
                is_coi_checkbox_only=raw_end.is_coi_checkbox_only,
                is_standard_form=raw_end.is_standard_form,
                manuscript_clause_detected=manuscript_detected,
                manuscript_status=manuscript_status,
                manuscript_verification_basis=verification_basis,
                independent_integrity_verified=False,  # Provider extraction cannot independently verify document integrity
                restriction_description=restriction_desc,
                restriction_reason_code=restriction_code,
                effective_date=eff_val.date if eff_val else None,
                expiration_date=exp_val.date if exp_val else None,
                evidence=ev,
                date_evidence=tuple(end_date_evidence),
                entity_evidence=tuple(verified_entity_evidence),
                extraction_status=status,
                confidence=raw_end.confidence,
            )
            endorsements.append(end)

        doc_set = ExtractedDocumentSet(
            subcontractor_id=subcontractor_id,
            documents_submitted=[inspection.classified_type],
            is_document_set_complete=raw_result.is_document_set_complete,
            named_insured=named_insured_info,
            certificate_holder=cert_holder_info,
            coverages=coverages,
            endorsements=endorsements,
            raw_contradictions_detected=raw_result.raw_contradictions,
        )

        return ExtractionPipelineResult(
            document_set=doc_set,
            processing_status=DocumentProcessingStatus.PROCESSED,
            requires_ocr=False,
            errors=[],
        )


def _normalize_entity_for_reconciliation(name: str) -> str:
    """Normalize entity name for cross-document reconciliation."""
    if not name:
        return ""
    cleaned = re.sub(r"[,\.'\"]", "", name.strip()).lower()
    tokens = [t for t in cleaned.split() if t not in ("inc", "llc", "corp", "corporation", "co", "company", "ltd", "limited")]
    return " ".join(tokens)


def reconcile_cross_document_set(doc_set: ExtractedDocumentSet) -> List[str]:
    """Deterministically reconcile cross-document extractions and identify material contradictions.
    
    Checks:
    1. Conflicting effective or expiration dates across coverages for the same coverage type.
    2. Conflicting limit amounts for equivalent coverage types and limit fields.
    3. Endorsement applicable_policy numbers that conflict with active coverage policy numbers.
    4. Conflicting effective or expiration dates between coverages and endorsements for the same policy.
    5. Conflicting dates across multiple endorsements for the same policy.
    6. Conflicting entity schedules across multiple endorsements of the same endorsement type.
    
    Populates doc_set.raw_contradictions_detected and returns the updated list.
    """
    new_contradictions: List[str] = []

    # 1. Date & Limit reconciliation across coverages sharing the same policy_number (or unlinked)
    # Distinct explicit policy numbers (e.g. GL-OLD vs GL-NEW) represent separate policies rather
    # than contradictory dates/limits on the same policy.
    coverages_by_scope: dict = {}
    for c in doc_set.coverages:
        pol_norm = re.sub(r"[\s\-/.]", "", c.policy_number.strip().upper()) if (c.policy_number and c.policy_number.strip()) else None
        scope_key = (c.coverage_type, pol_norm)
        coverages_by_scope.setdefault(scope_key, []).append(c)

    # If any unlinked (policy_number=None) coverage exists for a coverage_type alongside others,
    # compare across all coverages of that coverage_type.
    types_with_unlinked = {cov_type for (cov_type, pol_norm) in coverages_by_scope if pol_norm is None}
    for cov_type in types_with_unlinked:
        all_for_type = [c for c in doc_set.coverages if c.coverage_type == cov_type]
        coverages_by_scope[(cov_type, "__UNLINKED_SCOPE__")] = all_for_type

    for (cov_type, _scope), cov_list in coverages_by_scope.items():
        if len(cov_list) > 1:
            eff_dates = {c.effective_date.date for c in cov_list if c.effective_date and c.effective_date.date}
            if len(eff_dates) > 1:
                new_contradictions.append(
                    f"CONTRADICTORY_POLICY_DATES_DETECTED: Conflicting effective dates for {cov_type.value}: "
                    f"{[d.isoformat() for d in sorted(eff_dates)]}"
                )
            exp_dates = {c.expiration_date.date for c in cov_list if c.expiration_date and c.expiration_date.date}
            if len(exp_dates) > 1:
                new_contradictions.append(
                    f"CONTRADICTORY_POLICY_DATES_DETECTED: Conflicting expiration dates for {cov_type.value}: "
                    f"{[d.isoformat() for d in sorted(exp_dates)]}"
                )

            # 2. Limit reconciliation across equivalent coverages
            limit_fields = [
                "each_occurrence", "general_aggregate", "products_completed_ops",
                "personal_adv_injury", "combined_single_limit", "per_accident",
                "disease_each_employee", "disease_policy_limit"
            ]
            for lf in limit_fields:
                amounts = {
                    getattr(c, lf).amount_cents
                    for c in cov_list
                    if getattr(c, lf, None) and getattr(c, lf).amount_cents is not None
                }
                if len(amounts) > 1:
                    new_contradictions.append(
                        f"CONTRADICTORY_POLICY_LIMITS_DETECTED: Conflicting {lf} limits for {cov_type.value}: "
                        f"{[f'${amt/100:,.2f}' for amt in sorted(amounts)]}"
                    )

    # 3. Policy number reconciliation between endorsements and coverages
    known_policy_numbers = {c.policy_number.strip().upper() for c in doc_set.coverages if c.policy_number and c.policy_number.strip()}
    for end in doc_set.endorsements:
        if end.applicable_policy and end.applicable_policy.strip() and known_policy_numbers:
            end_pol = end.applicable_policy.strip().upper()
            if end_pol not in known_policy_numbers:
                new_contradictions.append(
                    f"CONTRADICTORY_POLICY_NUMBERS_DETECTED: Endorsement specifies policy '{end.applicable_policy}' "
                    f"which does not match known policy numbers {sorted(known_policy_numbers)}"
                )

    # 4. Cross-document date reconciliation between coverages and endorsements (Work Package C)
    for end in doc_set.endorsements:
        matching_covs = []
        if end.applicable_policy and end.applicable_policy.strip():
            end_pol = end.applicable_policy.strip().upper()
            matching_covs = [
                c for c in doc_set.coverages
                if c.policy_number and c.policy_number.strip().upper() == end_pol
            ]
        elif end.endorsement_type == EndorsementType.ADDITIONAL_INSURED:
            # Default linkage to general liability if policy number is omitted
            matching_covs = [
                c for c in doc_set.coverages
                if c.coverage_type == CoverageType.GENERAL_LIABILITY
            ]

        for cov in matching_covs:
            if end.effective_date and cov.effective_date and cov.effective_date.date:
                if end.effective_date != cov.effective_date.date:
                    new_contradictions.append(
                        f"CONTRADICTORY_POLICY_DATES_DETECTED: Conflicting effective dates between coverage {cov.coverage_type.value} "
                        f"({cov.effective_date.date.isoformat()}) and endorsement ({end.effective_date.isoformat()})"
                    )
            if end.expiration_date and cov.expiration_date and cov.expiration_date.date:
                if end.expiration_date != cov.expiration_date.date:
                    new_contradictions.append(
                        f"CONTRADICTORY_POLICY_DATES_DETECTED: Conflicting expiration dates between coverage {cov.coverage_type.value} "
                        f"({cov.expiration_date.date.isoformat()}) and endorsement ({end.expiration_date.isoformat()})"
                    )

    # 5. Cross-endorsement date reconciliation
    ends_by_policy: dict = {}
    for end in doc_set.endorsements:
        pol_key = end.applicable_policy.strip().upper() if (end.applicable_policy and end.applicable_policy.strip()) else f"TYPE_{end.endorsement_type.value}"
        ends_by_policy.setdefault(pol_key, []).append(end)

    for pol_key, elist in ends_by_policy.items():
        if len(elist) > 1:
            eff_dates = {e.effective_date for e in elist if e.effective_date}
            if len(eff_dates) > 1:
                new_contradictions.append(
                    f"CONTRADICTORY_POLICY_DATES_DETECTED: Conflicting effective dates across endorsements for '{pol_key}': "
                    f"{[d.isoformat() for d in sorted(eff_dates)]}"
                )
            exp_dates = {e.expiration_date for e in elist if e.expiration_date}
            if len(exp_dates) > 1:
                new_contradictions.append(
                    f"CONTRADICTORY_POLICY_DATES_DETECTED: Conflicting expiration dates across endorsements for '{pol_key}': "
                    f"{[d.isoformat() for d in sorted(exp_dates)]}"
                )

    # 6. Conflicting entity schedules across multiple endorsements of the same type (Work Package D)
    # Note: Legitimate third-party scheduled entities are preserved as facts without unconditional contradiction
    ends_by_type: dict = {}
    for end in doc_set.endorsements:
        ends_by_type.setdefault(end.endorsement_type, []).append(end)

    for end_type, end_list in ends_by_type.items():
        if len(end_list) > 1:
            schedules = [tuple(sorted(e.named_entities)) for e in end_list if e.named_entities and not e.is_blanket]
            if len(set(schedules)) > 1:
                new_contradictions.append(
                    f"CONTRADICTORY_ENDORSEMENTS_FOUND: Multiple endorsements for {end_type.value} have conflicting schedules: {list(set(schedules))}"
                )

    # Deduplicate and append to document set
    for c in new_contradictions:
        if c not in doc_set.raw_contradictions_detected:
            doc_set.raw_contradictions_detected.append(c)

    return doc_set.raw_contradictions_detected

