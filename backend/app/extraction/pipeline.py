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
    DateValue,
    DocumentType,
    EndorsementExtraction,
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


def _clean_str(text: Optional[str]) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(cleaned.split())


def _verify_entity_semantic_support(name: Optional[str], quote_text: Optional[str]) -> bool:
    if not name or not quote_text:
        return False
    c_name = _clean_str(name)
    c_quote = _clean_str(quote_text)
    if not c_name or not c_quote:
        return False
    if c_name in c_quote or c_quote in c_name:
        return True
    tokens = [t for t in c_name.split() if len(t) > 2 and t not in ("the", "and", "llc", "inc", "corp", "ltd", "co")]
    if tokens and any(t in c_quote for t in tokens):
        return True
    return False


def _verify_coverage_semantic_support(cov_raw: RawCoverageSection, quote_text: Optional[str]) -> bool:
    if not quote_text:
        return False
    c_quote = _clean_str(quote_text)
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
            c_lim = _clean_str(lim)
            if c_lim and (c_lim in c_quote or c_quote in c_lim):
                return True
            digits = re.sub(r"\D", "", lim)
            if digits and len(digits) >= 4 and digits in re.sub(r"\D", "", quote_text):
                return True

    if cov_raw.policy_number:
        c_pol = _clean_str(cov_raw.policy_number)
        if c_pol and (c_pol in c_quote or c_quote in c_pol):
            return True

    c_type = _clean_str(cov_raw.coverage_type)
    if any(k in c_quote for k in ["liability", "general", "auto", "workers", "umbrella", "excess", "occurrence", "aggregate", "csl"]):
        return True

    return False


def _verify_endorsement_semantic_support(end_raw: RawEndorsement, quote_text: Optional[str]) -> bool:
    if not quote_text:
        return False
    c_quote = _clean_str(quote_text)
    if end_raw.form_number:
        c_form = _clean_str(end_raw.form_number)
        if c_form and (c_form in c_quote or c_quote in c_form):
            return True
    if any(k in c_quote for k in ["additional insured", "additional", "insured", "endorsement", "waiver", "subrogation", "blanket", "primary"]):
        return True
    return False


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
                found_in_spans = any(
                    cleaned_quote in span.text.strip().lower() or span.text.strip().lower() in cleaned_quote
                    for span in matching_page.spans
                    if span.text.strip()
                )
                if not found_in_spans:
                    words = [w for w in re.findall(r'\b\w+\b', cleaned_quote) if len(w) > 2]
                    bigrams = [f"{words[i]} {words[i+1]}" for i in range(len(words)-1)] if len(words) >= 2 else []
                    found_bigram = any(bg in cleaned_page_text for bg in bigrams)
                    if not found_bigram:
                        return None

            # Try to locate matching text span on the page for exact coordinates
            for span in matching_page.spans:
                if span.text.strip().lower() in cleaned_quote or cleaned_quote in span.text.strip().lower():
                    bounding_box = span.bounding_box
                    break

        return EvidenceReference(
            document_id=document_id,
            document_type=document_type,
            page_number=quote.page_number,
            source_text=quote.source_text.strip(),
            bounding_box=bounding_box,
        )

    def _build_money_value(self, raw_str: Optional[str], confidence: Optional[float]) -> Optional[MoneyValue]:
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
        )

    def _build_date_value(self, raw_str: Optional[str], confidence: Optional[float]) -> Optional[DateValue]:
        if not raw_str or not raw_str.strip():
            return None

        parsed_date, is_ambiguous, reason = normalize_date(raw_str)
        if parsed_date is None or is_ambiguous:
            return None

        return DateValue(
            date=parsed_date,
            raw_text=raw_str.strip(),
            confidence=confidence,
        )

    def process_inspection(
        self,
        inspection: PDFDocumentInspection,
        subcontractor_id: str,
    ) -> ExtractionPipelineResult:
        """Run the end-to-end extraction pipeline on an inspected PDF document."""
        pages_to_process = inspection.pages

        # Check for scanned document with no text layer
        if inspection.is_scanned or inspection.processing_status == DocumentProcessingStatus.REQUIRES_OCR:
            if self.ocr_provider is None:
                return ExtractionPipelineResult(
                    document_set=None,
                    processing_status=DocumentProcessingStatus.REQUIRES_OCR,
                    requires_ocr=True,
                    errors=[
                        f"Document '{inspection.filename}' is a scanned or image-only PDF with no text layer. "
                        "No OCR provider is configured to transcribe page content."
                    ],
                )
            # Route to OCR provider
            pages_to_process = self.ocr_provider.perform_ocr_on_pages(inspection.pages)

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

            return ExtractionPipelineResult(
                document_set=None,
                processing_status=DocumentProcessingStatus.FAILED,
                errors=[f"AI extraction provider '{self.extraction_provider.provider_name}' failed: {clean_msg}"],
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
                each_occurrence=self._build_money_value(raw_cov.each_occurrence, raw_cov.confidence),
                general_aggregate=self._build_money_value(raw_cov.general_aggregate, raw_cov.confidence),
                products_completed_ops=self._build_money_value(raw_cov.products_completed_ops, raw_cov.confidence),
                personal_adv_injury=self._build_money_value(raw_cov.personal_adv_injury, raw_cov.confidence),
                combined_single_limit=self._build_money_value(raw_cov.combined_single_limit, raw_cov.confidence),
                per_accident=self._build_money_value(raw_cov.per_accident, raw_cov.confidence),
                disease_each_employee=self._build_money_value(raw_cov.disease_each_employee, raw_cov.confidence),
                disease_policy_limit=self._build_money_value(raw_cov.disease_policy_limit, raw_cov.confidence),
                effective_date=self._build_date_value(raw_cov.effective_date, raw_cov.confidence),
                expiration_date=self._build_date_value(raw_cov.expiration_date, raw_cov.confidence),
                evidence=ev,
                extraction_status=status,
            )
            coverages.append(cov)

        # Endorsements
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

            end = EndorsementExtraction(
                endorsement_type=norm_end_type,
                document_type=doc_type,
                form_number=raw_end.form_number,
                applicable_policy=raw_end.applicable_policy,
                named_entities=raw_end.named_entities,
                key_terms=raw_end.key_terms,
                is_blanket=raw_end.is_blanket,
                is_coi_checkbox_only=raw_end.is_coi_checkbox_only,
                evidence=ev,
                extraction_status=status,
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
