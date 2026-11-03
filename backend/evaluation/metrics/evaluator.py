"""Evaluation and metrics calculation engine for SubGuard AI.

Calculates:
1. Field-level exact match and normalized accuracy (currency in cents, ISO dates, entities).
2. Precision, Recall, and F1 across coverage types, endorsements, and parties.
3. True negative / missing-value handling (preventing hallucinations on absent sections).
4. Evidence grounding & semantic verification:
   - Verifying citation presence against actual PDF page text layer
   - Detecting cross-page attribution errors
   - Catching completely hallucinated/fabricated quotes
   - Semantic value-support verification (does quote actually contain the extracted value?)
   - Category-alignment verification (does quote align with the coverage limit category rather than another field?)
   - Digital text layer vs OCR-derived provenance tracking
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from app.schemas.insurance import (
    CoverageExtraction,
    CoverageType,
    EndorsementExtraction,
    EndorsementType,
    EntityInfo,
    EvidenceReference,
    ExtractedDocumentSet,
    ExtractionStatus,
    MoneyValue,
)
from app.schemas.pdf import PDFDocumentInspection
from evaluation.schemas.ground_truth import (
    DocumentGroundTruth,
    ExpectedCoverage,
    ExpectedDate,
    ExpectedEndorsement,
    ExpectedEntity,
    ExpectedEvidence,
    ExpectedMoney,
)
from evaluation.schemas.reports import (
    DocumentEvaluationResult,
    EvidenceEvaluationResult,
    EvidenceMetricStats,
    FieldEvaluationResult,
    FieldMetricStats,
)


def _clean_str(text: Optional[str]) -> str:
    """Normalize string for fuzzy/case-insensitive comparison."""
    if not text:
        return ""
    # Collapse multiple whitespace, lowercase, strip punctuation
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(cleaned.split())


def _normalize_entity_name(name: Optional[str]) -> str:
    """Normalize business entity name, treating common suffix variations consistently."""
    if not name:
        return ""
    cleaned = _clean_str(name)
    tokens = cleaned.split()
    standardized = []
    suffix_map = {
        "incorporated": "inc",
        "corporation": "corp",
        "company": "co",
        "limited": "ltd",
    }
    for t in tokens:
        standardized.append(suffix_map.get(t, t))
    return " ".join(standardized)


def _check_semantic_evidence_support(
    field_name: str,
    source_text: str,
    extracted_value: Any,
    raw_text: Optional[str] = None,
) -> Tuple[bool, bool, Optional[str]]:
    """Verify whether evidence text actually supports the value and aligns with field category.
    
    Returns:
        (value_supported, category_aligned, notes)
    """
    if not source_text or not source_text.strip():
        return False, False, "Evidence quote is empty"

    clean_quote = _clean_str(source_text)
    notes_list = []
    value_supported = True
    category_aligned = True

    # 1. Value Support Check
    if extracted_value is not None:
        if isinstance(extracted_value, int) and (
            "cents" in field_name
            or field_name.endswith((
                ".each_occurrence",
                ".general_aggregate",
                ".products_completed_ops",
                ".personal_adv_injury",
                ".combined_single_limit",
                ".per_accident",
                ".disease_each_employee",
                ".disease_policy_limit",
            ))
        ):
            # Amount in cents (e.g. 100_000_000 cents = $1,000,000)
            dollars = extracted_value // 100
            dollar_str = f"{dollars:,}"
            raw_clean = _clean_str(raw_text) if raw_text else ""

            has_amount = (
                dollar_str in source_text
                or str(dollars) in source_text.replace(",", "")
                or (raw_clean and raw_clean in clean_quote)
            )
            # Check shorthand like 1M or $1M
            if not has_amount and dollars >= 1_000_000 and dollars % 1_000_000 == 0:
                m_str = f"{dollars // 1_000_000}m"
                has_amount = m_str in clean_quote

            if not has_amount:
                value_supported = False
                notes_list.append(f"Quote does not contain value {raw_text or dollar_str}")

        elif isinstance(extracted_value, (date, str)):
            val_str = str(extracted_value)
            raw_clean = _clean_str(raw_text) if raw_text else ""
            has_val = (val_str in source_text) or (raw_clean and raw_clean in clean_quote)
            if not has_val and isinstance(extracted_value, date):
                year_str = str(extracted_value.year)
                has_val = year_str in clean_quote
            # For endorsements, quote may corroborate endorsement keywords (e.g. "additional insured", "cg 20")
            if not has_val and ("endorsement" in field_name or "additional_insured" in field_name):
                if any(k in clean_quote for k in ["additional insured", "cg 20", "endorsement", "blanket", "automatic"]):
                    has_val = True
            if not has_val:
                value_supported = False
                notes_list.append(f"Quote does not corroborate {raw_text or val_str}")

    # 2. Category / Terminology Alignment Check
    if "general_aggregate" in field_name:
        has_agg = any(k in clean_quote for k in ["agg", "aggregate"])
        has_occ_only = any(k in clean_quote for k in ["each occ", "each occurrence", "per occurrence"]) and not has_agg
        if has_occ_only:
            category_aligned = False
            notes_list.append("Quote identifies each-occurrence limit, but was cited for general_aggregate")
    elif "each_occurrence" in field_name:
        has_occ = any(k in clean_quote for k in ["occ", "occurrence", "each"])
        has_agg_only = any(k in clean_quote for k in ["general agg", "general aggregate"]) and not has_occ
        if has_agg_only:
            category_aligned = False
            notes_list.append("Quote identifies aggregate limit, but was cited for each_occurrence")
    elif "products_completed_ops" in field_name:
        has_prod = any(k in clean_quote for k in ["prod", "completed", "comp op", "comp/op"])
        if not has_prod and any(k in clean_quote for k in ["each occ", "automobile", "workers"]):
            category_aligned = False
            notes_list.append("Quote lacks products/completed operations terms")
    elif "auto_liability" in field_name:
        has_auto = any(k in clean_quote for k in ["auto", "vehicle", "csl", "combined single"])
        if not has_auto and any(k in clean_quote for k in ["commercial general", "workers comp"]):
            category_aligned = False
            notes_list.append("Quote lacks auto liability terminology")
    elif "workers_comp" in field_name:
        has_wc = any(k in clean_quote for k in ["workers", "comp", "statutory", "accident", "disease", "e l", "employer"])
        if not has_wc and any(k in clean_quote for k in ["commercial general", "automobile liability"]):
            category_aligned = False
            notes_list.append("Quote lacks workers compensation terminology")
    elif "additional_insured" in field_name:
        has_ai = any(k in clean_quote for k in ["additional", "insured", "cg 20", "endorsement", "blanket", "automatic"])
        if not has_ai and any(k in clean_quote for k in ["workers compensation", "automobile liability"]):
            category_aligned = False
            notes_list.append("Quote lacks additional insured terminology")

    notes = "; ".join(notes_list) if notes_list else None
    return value_supported, category_aligned, notes


EXECUTION_FAILURE_STATUSES = {
    "configuration_error",
    "authentication_error",
    "provider_error",
    "rate_limit_error",
    "timeout_error",
    "schema_validation_error",
    "invalid_model_response",
    "not_run",
    "file_not_found",
}


class MetricsEvaluator:
    """Computes field-level accuracy, pass rates, and evidence-grounding metrics."""

    def evaluate_document(
        self,
        ground_truth: DocumentGroundTruth,
        extracted_doc_set: Optional[ExtractedDocumentSet],
        pdf_inspection: Optional[PDFDocumentInspection],
        processing_status: str,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> DocumentEvaluationResult:
        """Run full evaluation on a single extracted document against ground truth."""
        field_results: List[FieldEvaluationResult] = []
        evidence_results: List[EvidenceEvaluationResult] = []

        # 1. OCR Requirement Matching
        requires_ocr_matched = True
        if ground_truth.requires_ocr:
            requires_ocr_matched = (processing_status == "requires_ocr")

        # If document set failed completely or requires OCR
        if extracted_doc_set is None:
            if ground_truth.requires_ocr and processing_status == "requires_ocr":
                field_results.append(
                    FieldEvaluationResult(
                        field_name="document_ocr_detection",
                        category="system",
                        ground_truth_present=True,
                        extracted_present=True,
                        exact_match=True,
                        normalized_match=True,
                        expected_value="requires_ocr",
                        extracted_value="requires_ocr",
                        error_type="none",
                    )
                )
            elif processing_status in EXECUTION_FAILURE_STATUSES:
                # Provider, configuration, and execution failures are NOT document-level false negatives
                note_text = f"Extraction halted due to {processing_status}: {error_message}" if error_message else f"Extraction halted due to {processing_status}"
                field_results.append(
                    FieldEvaluationResult(
                        field_name="document_execution",
                        category="execution",
                        ground_truth_present=False,
                        extracted_present=False,
                        exact_match=False,
                        normalized_match=False,
                        error_type=processing_status,
                        notes=note_text,
                    )
                )
            else:
                field_results.append(
                    FieldEvaluationResult(
                        field_name="document_extraction",
                        category="system",
                        ground_truth_present=True,
                        extracted_present=False,
                        exact_match=False,
                        normalized_match=False,
                        error_type="false_negative",
                        notes=f"Extraction returned None with status {processing_status}",
                    )
                )

            is_passed = (ground_truth.requires_ocr and processing_status == "requires_ocr")
            return DocumentEvaluationResult(
                document_id=ground_truth.document_id,
                filename=ground_truth.filename,
                source_type=ground_truth.source_type,
                document_type=ground_truth.document_type,
                processing_status=processing_status,
                requires_ocr_matched=requires_ocr_matched,
                field_results=field_results,
                evidence_results=evidence_results,
                field_accuracy=1.0 if is_passed else 0.0,
                exact_field_accuracy=1.0 if is_passed else 0.0,
                evidence_grounding_rate=1.0 if is_passed else 0.0,
                is_strictly_passed=is_passed,
                error_type=error_type,
                error_message=error_message,
                error_code=error_code,
            )

        # 2. Evaluate Named Insured
        self._evaluate_entity(
            field_name="named_insured",
            expected=ground_truth.named_insured,
            extracted=extracted_doc_set.named_insured,
            pdf_inspection=pdf_inspection,
            field_results=field_results,
            evidence_results=evidence_results,
        )

        # 3. Evaluate Certificate Holder
        self._evaluate_entity(
            field_name="certificate_holder",
            expected=ground_truth.certificate_holder,
            extracted=extracted_doc_set.certificate_holder,
            pdf_inspection=pdf_inspection,
            field_results=field_results,
            evidence_results=evidence_results,
        )

        # 4. Evaluate Coverages
        self._evaluate_coverages(
            expected_coverages=ground_truth.coverages,
            extracted_coverages=extracted_doc_set.coverages,
            absent_coverages=ground_truth.absent_coverages,
            pdf_inspection=pdf_inspection,
            field_results=field_results,
            evidence_results=evidence_results,
        )

        # 5. Evaluate Endorsements
        self._evaluate_endorsements(
            expected_endorsements=ground_truth.endorsements,
            extracted_endorsements=extracted_doc_set.endorsements,
            absent_endorsements=ground_truth.absent_endorsements,
            pdf_inspection=pdf_inspection,
            field_results=field_results,
            evidence_results=evidence_results,
        )

        # Compute document summary score
        total_fields = len(field_results)
        matching_fields = sum(1 for f in field_results if f.normalized_match)
        exact_fields = sum(1 for f in field_results if f.exact_match)
        field_accuracy = (matching_fields / total_fields) if total_fields > 0 else 1.0
        exact_accuracy = (exact_fields / total_fields) if total_fields > 0 else 1.0

        total_evidence = len(evidence_results)
        grounded_evidence = sum(
            1 for e in evidence_results
            if e.quote_found_on_page and e.page_correct and e.value_supported and e.category_aligned
        )
        evidence_rate = (grounded_evidence / total_evidence) if total_evidence > 0 else 1.0

        # Strict pass: 100% field normalized match AND 100% evidence verification
        is_strictly_passed = (
            requires_ocr_matched
            and all(f.normalized_match for f in field_results)
            and all(
                e.quote_found_on_page and e.page_correct and e.value_supported and e.category_aligned
                for e in evidence_results
            )
        )

        return DocumentEvaluationResult(
            document_id=ground_truth.document_id,
            filename=ground_truth.filename,
            source_type=ground_truth.source_type,
            document_type=ground_truth.document_type,
            processing_status=processing_status,
            requires_ocr_matched=requires_ocr_matched,
            field_results=field_results,
            evidence_results=evidence_results,
            field_accuracy=field_accuracy,
            exact_field_accuracy=exact_accuracy,
            evidence_grounding_rate=evidence_rate,
            is_strictly_passed=is_strictly_passed,
        )

    def _evaluate_entity(
        self,
        field_name: str,
        expected: Optional[ExpectedEntity],
        extracted: Optional[EntityInfo],
        pdf_inspection: Optional[PDFDocumentInspection],
        field_results: List[FieldEvaluationResult],
        evidence_results: List[EvidenceEvaluationResult],
    ) -> None:
        expected_present = expected is not None and not expected.is_absent and bool(expected.name)
        extracted_present = (
            extracted is not None
            and bool(extracted.name)
        )
        is_uncertain = (
            extracted is not None
            and extracted.extraction_status in (
                ExtractionStatus.UNCERTAIN,
                ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT,
            )
        )

        if not expected_present and not extracted_present:
            # True negative (absent entity correctly detected as missing)
            field_results.append(
                FieldEvaluationResult(
                    field_name=field_name,
                    category="entity",
                    ground_truth_present=False,
                    extracted_present=False,
                    exact_match=True,
                    normalized_match=True,
                    error_type="none",
                )
            )
            return

        if not expected_present and extracted_present:
            # False positive (hallucinated entity name)
            field_results.append(
                FieldEvaluationResult(
                    field_name=field_name,
                    category="entity",
                    ground_truth_present=False,
                    extracted_present=True,
                    exact_match=False,
                    normalized_match=False,
                    extracted_value=extracted.name if extracted else None,
                    error_type="false_positive",
                )
            )
            return

        if expected_present and not extracted_present:
            # False negative (missed entity completely)
            field_results.append(
                FieldEvaluationResult(
                    field_name=field_name,
                    category="entity",
                    ground_truth_present=True,
                    extracted_present=False,
                    exact_match=False,
                    normalized_match=False,
                    expected_value=expected.name if expected else None,
                    error_type="false_negative",
                )
            )
            return

        # Both present: compare values
        exp_name = expected.name.strip() if expected and expected.name else ""
        ext_name = extracted.name.strip() if extracted and extracted.name else ""

        exact = (exp_name == ext_name)
        normalized = (_normalize_entity_name(exp_name) == _normalize_entity_name(ext_name))

        notes = None
        if is_uncertain:
            notes = "Extracted with UNCERTAIN status (unverified or missing evidence citation)"

        field_results.append(
            FieldEvaluationResult(
                field_name=field_name,
                category="entity",
                ground_truth_present=True,
                extracted_present=True,
                exact_match=exact,
                normalized_match=normalized,
                expected_value=exp_name,
                extracted_value=ext_name,
                error_type="none" if normalized else "value_mismatch",
                notes=notes,
            )
        )

        # Evidence evaluation
        if extracted and extracted.evidence:
            ev_result = self._verify_evidence(
                field_name=f"{field_name}_evidence",
                evidence=extracted.evidence,
                expected_evidence=expected.evidence if expected else None,
                pdf_inspection=pdf_inspection,
                extracted_value=ext_name,
                raw_text=ext_name,
            )
            evidence_results.append(ev_result)
        elif expected and expected.evidence:
            # Expected evidence was missing from extraction
            evidence_results.append(
                EvidenceEvaluationResult(
                    field_name=f"{field_name}_evidence",
                    expected_page=expected.evidence.page_number,
                    page_correct=False,
                    quote_found_on_page=False,
                    quote_found_in_document=False,
                    value_supported=False,
                    category_aligned=False,
                    notes="Expected evidence was not cited by extraction provider",
                )
            )

    def _evaluate_coverages(
        self,
        expected_coverages: List[ExpectedCoverage],
        extracted_coverages: List[CoverageExtraction],
        absent_coverages: List[CoverageType],
        pdf_inspection: Optional[PDFDocumentInspection],
        field_results: List[FieldEvaluationResult],
        evidence_results: List[EvidenceEvaluationResult],
    ) -> None:
        extracted_map = {c.coverage_type: c for c in extracted_coverages}

        # Check absent coverage assertions (True Negatives / False Positives)
        for absent_type in absent_coverages:
            if absent_type in extracted_map:
                field_results.append(
                    FieldEvaluationResult(
                        field_name=f"coverage_presence_{absent_type.value}",
                        category="coverage_presence",
                        ground_truth_present=False,
                        extracted_present=True,
                        exact_match=False,
                        normalized_match=False,
                        extracted_value=absent_type.value,
                        error_type="false_positive",
                        notes=f"Coverage {absent_type.value} was extracted but marked absent in ground truth",
                    )
                )
            else:
                field_results.append(
                    FieldEvaluationResult(
                        field_name=f"coverage_presence_{absent_type.value}",
                        category="coverage_presence",
                        ground_truth_present=False,
                        extracted_present=False,
                        exact_match=True,
                        normalized_match=True,
                        error_type="none",
                    )
                )

        # Check expected coverages
        for exp in expected_coverages:
            cov_type = exp.coverage_type
            extracted = extracted_map.get(cov_type)

            if extracted is None:
                field_results.append(
                    FieldEvaluationResult(
                        field_name=f"coverage_presence_{cov_type.value}",
                        category="coverage_presence",
                        ground_truth_present=True,
                        extracted_present=False,
                        exact_match=False,
                        normalized_match=False,
                        expected_value=cov_type.value,
                        error_type="false_negative",
                    )
                )
                continue

            field_results.append(
                FieldEvaluationResult(
                    field_name=f"coverage_presence_{cov_type.value}",
                    category="coverage_presence",
                    ground_truth_present=True,
                    extracted_present=True,
                    exact_match=True,
                    normalized_match=True,
                    expected_value=cov_type.value,
                    extracted_value=cov_type.value,
                    error_type="none",
                )
            )

            # Sub-limits
            limit_fields = [
                ("each_occurrence", exp.each_occurrence, extracted.each_occurrence),
                ("general_aggregate", exp.general_aggregate, extracted.general_aggregate),
                ("products_completed_ops", exp.products_completed_ops, extracted.products_completed_ops),
                ("personal_adv_injury", exp.personal_adv_injury, extracted.personal_adv_injury),
                ("combined_single_limit", exp.combined_single_limit, extracted.combined_single_limit),
                ("per_accident", exp.per_accident, extracted.per_accident),
                ("disease_each_employee", exp.disease_each_employee, extracted.disease_each_employee),
                ("disease_policy_limit", exp.disease_policy_limit, extracted.disease_policy_limit),
            ]

            for lim_name, exp_lim, ext_lim in limit_fields:
                self._evaluate_money(
                    field_name=f"{cov_type.value}.{lim_name}",
                    expected=exp_lim,
                    extracted=ext_lim,
                    field_results=field_results,
                )

            # Dates
            self._evaluate_date(
                field_name=f"{cov_type.value}.effective_date",
                expected=exp.effective_date,
                extracted=extracted.effective_date,
                field_results=field_results,
            )
            self._evaluate_date(
                field_name=f"{cov_type.value}.expiration_date",
                expected=exp.expiration_date,
                extracted=extracted.expiration_date,
                field_results=field_results,
            )

            # Policy Number
            if exp.policy_number or extracted.policy_number:
                exp_pol = exp.policy_number or ""
                ext_pol = extracted.policy_number or ""
                exact = (_clean_str(exp_pol) == _clean_str(ext_pol))
                field_results.append(
                    FieldEvaluationResult(
                        field_name=f"{cov_type.value}.policy_number",
                        category="policy_number",
                        ground_truth_present=bool(exp.policy_number),
                        extracted_present=bool(extracted.policy_number),
                        exact_match=exact,
                        normalized_match=exact,
                        expected_value=exp_pol,
                        extracted_value=ext_pol,
                        error_type="none" if exact else "value_mismatch",
                    )
                )

            # Evidence evaluation
            if extracted.evidence:
                ev_result = self._verify_evidence(
                    field_name=f"{cov_type.value}_evidence",
                    evidence=extracted.evidence,
                    expected_evidence=exp.evidence,
                    pdf_inspection=pdf_inspection,
                    extracted_value=extracted.each_occurrence.amount_cents if extracted.each_occurrence else (
                        extracted.combined_single_limit.amount_cents if extracted.combined_single_limit else None
                    ),
                    raw_text=extracted.each_occurrence.raw_text if extracted.each_occurrence else (
                        extracted.combined_single_limit.raw_text if extracted.combined_single_limit else None
                    ),
                )
                evidence_results.append(ev_result)

    def _evaluate_money(
        self,
        field_name: str,
        expected: Optional[ExpectedMoney],
        extracted: Optional[MoneyValue],
        field_results: List[FieldEvaluationResult],
    ) -> None:
        expected_present = expected is not None and not expected.is_absent
        extracted_present = extracted is not None

        if not expected_present and not extracted_present:
            return

        if not expected_present and extracted_present:
            field_results.append(
                FieldEvaluationResult(
                    field_name=field_name,
                    category="coverage_limits",
                    ground_truth_present=False,
                    extracted_present=True,
                    exact_match=False,
                    normalized_match=False,
                    extracted_value=extracted.amount_cents if extracted else None,
                    error_type="false_positive",
                )
            )
            return

        if expected_present and not extracted_present:
            field_results.append(
                FieldEvaluationResult(
                    field_name=field_name,
                    category="coverage_limits",
                    ground_truth_present=True,
                    extracted_present=False,
                    exact_match=False,
                    normalized_match=False,
                    expected_value=expected.amount_cents if expected else None,
                    error_type="false_negative",
                )
            )
            return

        # Both present
        exp_cents = expected.amount_cents if expected else None
        ext_cents = extracted.amount_cents if extracted else None
        normalized = (exp_cents == ext_cents)

        exact = normalized
        if expected and expected.raw_text and extracted and extracted.raw_text:
            exact = exact and (expected.raw_text.strip() == extracted.raw_text.strip())

        field_results.append(
            FieldEvaluationResult(
                field_name=field_name,
                category="coverage_limits",
                ground_truth_present=True,
                extracted_present=True,
                exact_match=exact,
                normalized_match=normalized,
                expected_value=exp_cents,
                extracted_value=ext_cents,
                error_type="none" if normalized else "value_mismatch",
            )
        )

    def _evaluate_date(
        self,
        field_name: str,
        expected: Optional[ExpectedDate],
        extracted: Optional[Any],
        field_results: List[FieldEvaluationResult],
    ) -> None:
        expected_present = expected is not None and not expected.is_absent and expected.date is not None
        extracted_present = extracted is not None and getattr(extracted, "date", None) is not None

        if not expected_present and not extracted_present:
            return

        if not expected_present and extracted_present:
            field_results.append(
                FieldEvaluationResult(
                    field_name=field_name,
                    category="policy_dates",
                    ground_truth_present=False,
                    extracted_present=True,
                    exact_match=False,
                    normalized_match=False,
                    extracted_value=str(extracted.date) if extracted else None,
                    error_type="false_positive",
                )
            )
            return

        if expected_present and not extracted_present:
            field_results.append(
                FieldEvaluationResult(
                    field_name=field_name,
                    category="policy_dates",
                    ground_truth_present=True,
                    extracted_present=False,
                    exact_match=False,
                    normalized_match=False,
                    expected_value=str(expected.date) if expected else None,
                    error_type="false_negative",
                )
            )
            return

        # Both present
        exp_date = expected.date if expected else None
        ext_date = extracted.date if extracted else None
        normalized = (exp_date == ext_date)

        exact = normalized
        if expected and expected.raw_text and extracted and getattr(extracted, "raw_text", None):
            exact = exact and (expected.raw_text.strip() == extracted.raw_text.strip())

        field_results.append(
            FieldEvaluationResult(
                field_name=field_name,
                category="policy_dates",
                ground_truth_present=True,
                extracted_present=True,
                exact_match=exact,
                normalized_match=normalized,
                expected_value=str(exp_date) if exp_date else None,
                extracted_value=str(ext_date) if ext_date else None,
                error_type="none" if normalized else "value_mismatch",
            )
        )

    def _evaluate_endorsements(
        self,
        expected_endorsements: List[ExpectedEndorsement],
        extracted_endorsements: List[EndorsementExtraction],
        absent_endorsements: List[EndorsementType],
        pdf_inspection: Optional[PDFDocumentInspection],
        field_results: List[FieldEvaluationResult],
        evidence_results: List[EvidenceEvaluationResult],
    ) -> None:
        extracted_map = {e.endorsement_type: e for e in extracted_endorsements}

        # Check absent endorsements
        for absent_type in absent_endorsements:
            if absent_type in extracted_map:
                field_results.append(
                    FieldEvaluationResult(
                        field_name=f"endorsement_presence_{absent_type.value}",
                        category="endorsement_presence",
                        ground_truth_present=False,
                        extracted_present=True,
                        exact_match=False,
                        normalized_match=False,
                        extracted_value=absent_type.value,
                        error_type="false_positive",
                    )
                )
            else:
                field_results.append(
                    FieldEvaluationResult(
                        field_name=f"endorsement_presence_{absent_type.value}",
                        category="endorsement_presence",
                        ground_truth_present=False,
                        extracted_present=False,
                        exact_match=True,
                        normalized_match=True,
                        error_type="none",
                    )
                )

        # Check expected endorsements
        for exp in expected_endorsements:
            end_type = exp.endorsement_type
            extracted = extracted_map.get(end_type)

            if extracted is None:
                field_results.append(
                    FieldEvaluationResult(
                        field_name=f"endorsement_presence_{end_type.value}",
                        category="endorsement_presence",
                        ground_truth_present=True,
                        extracted_present=False,
                        exact_match=False,
                        normalized_match=False,
                        expected_value=end_type.value,
                        error_type="false_negative",
                    )
                )
                continue

            field_results.append(
                FieldEvaluationResult(
                    field_name=f"endorsement_presence_{end_type.value}",
                    category="endorsement_presence",
                    ground_truth_present=True,
                    extracted_present=True,
                    exact_match=True,
                    normalized_match=True,
                    expected_value=end_type.value,
                    extracted_value=end_type.value,
                    error_type="none",
                )
            )

            # Blanket flag check
            blanket_match = (exp.is_blanket == extracted.is_blanket)
            field_results.append(
                FieldEvaluationResult(
                    field_name=f"{end_type.value}.is_blanket",
                    category="endorsement_details",
                    ground_truth_present=True,
                    extracted_present=True,
                    exact_match=blanket_match,
                    normalized_match=blanket_match,
                    expected_value=exp.is_blanket,
                    extracted_value=extracted.is_blanket,
                    error_type="none" if blanket_match else "value_mismatch",
                )
            )

            # Form number check
            if exp.form_number or extracted.form_number:
                exp_form = _clean_str(exp.form_number)
                ext_form = _clean_str(extracted.form_number)
                form_match = (exp_form == ext_form)
                field_results.append(
                    FieldEvaluationResult(
                        field_name=f"{end_type.value}.form_number",
                        category="endorsement_details",
                        ground_truth_present=bool(exp.form_number),
                        extracted_present=bool(extracted.form_number),
                        exact_match=form_match,
                        normalized_match=form_match,
                        expected_value=exp.form_number,
                        extracted_value=extracted.form_number,
                        error_type="none" if form_match else "value_mismatch",
                    )
                )

            # Evidence evaluation
            if extracted.evidence:
                ev_result = self._verify_evidence(
                    field_name=f"{end_type.value}_evidence",
                    evidence=extracted.evidence,
                    expected_evidence=exp.evidence,
                    pdf_inspection=pdf_inspection,
                    extracted_value=extracted.form_number,
                    raw_text=extracted.form_number,
                )
                evidence_results.append(ev_result)

    def _verify_evidence(
        self,
        field_name: str,
        evidence: EvidenceReference,
        expected_evidence: Optional[ExpectedEvidence],
        pdf_inspection: Optional[PDFDocumentInspection],
        extracted_value: Any = None,
        raw_text: Optional[str] = None,
    ) -> EvidenceEvaluationResult:
        """Independently verifies an evidence reference against actual PDF text layers."""
        cited_page = evidence.page_number
        source_text = evidence.source_text.strip() if evidence.source_text else ""
        expected_page = expected_evidence.page_number if expected_evidence else None

        page_correct = (expected_page is not None and cited_page == expected_page)
        is_ocr = bool(pdf_inspection and pdf_inspection.is_scanned)

        if not pdf_inspection:
            return EvidenceEvaluationResult(
                field_name=field_name,
                cited_page=cited_page,
                expected_page=expected_page,
                page_correct=page_correct,
                quote_found_on_page=False,
                quote_found_in_document=False,
                is_hallucinated=True,
                value_supported=False,
                category_aligned=False,
                is_ocr_derived=is_ocr,
                source_text=source_text,
                notes="No PDF inspection provided",
            )

        norm_quote = _clean_str(source_text)

        # 1. Search on the cited page
        quote_found_on_page = False
        matching_page = next((p for p in pdf_inspection.pages if p.page_number == cited_page), None)
        if matching_page:
            page_text = _clean_str(matching_page.text_content)
            if norm_quote and norm_quote in page_text:
                quote_found_on_page = True

        # 2. Search across all pages in the document
        quote_found_in_document = quote_found_on_page
        if not quote_found_in_document and norm_quote:
            for p in pdf_inspection.pages:
                if norm_quote in _clean_str(p.text_content):
                    quote_found_in_document = True
                    break

        # 3. Determine if hallucinated or cross-page attribution
        is_hallucinated = not quote_found_in_document
        is_cross_page = quote_found_in_document and not quote_found_on_page

        # 4. Semantic Value Support & Category Alignment
        val_sup, cat_align, sem_notes = _check_semantic_evidence_support(
            field_name=field_name,
            source_text=source_text,
            extracted_value=extracted_value,
            raw_text=raw_text,
        )

        return EvidenceEvaluationResult(
            field_name=field_name,
            cited_page=cited_page,
            expected_page=expected_page,
            page_correct=page_correct,
            quote_found_on_page=quote_found_on_page,
            quote_found_in_document=quote_found_in_document,
            is_hallucinated=is_hallucinated,
            is_cross_page_attribution=is_cross_page,
            value_supported=val_sup,
            category_aligned=cat_align,
            is_ocr_derived=is_ocr,
            source_text=source_text,
            notes=sem_notes,
        )

    def calculate_aggregate_metrics(
        self,
        doc_results: List[DocumentEvaluationResult],
    ) -> Tuple[Dict[str, FieldMetricStats], EvidenceMetricStats]:
        """Aggregate field-level and evidence metrics across all evaluated documents."""
        category_map: Dict[str, List[FieldEvaluationResult]] = {}
        all_evidence: List[EvidenceEvaluationResult] = []

        for d in doc_results:
            # Exclude provider/configuration/execution failures from extraction accuracy, precision, recall denominators
            if d.processing_status in EXECUTION_FAILURE_STATUSES:
                continue

            for f in d.field_results:
                category_map.setdefault(f.category, []).append(f)
                category_map.setdefault("all", []).append(f)
            for e in d.evidence_results:
                all_evidence.append(e)

        field_stats: Dict[str, FieldMetricStats] = {}
        if not category_map:
            field_stats["all"] = FieldMetricStats(category="all")
        for cat, results in category_map.items():
            total = len(results)
            exact_matches = sum(1 for r in results if r.exact_match)
            norm_matches = sum(1 for r in results if r.normalized_match)

            tp = sum(1 for r in results if r.ground_truth_present and r.extracted_present and r.normalized_match)
            fp = sum(
                1 for r in results
                if (not r.ground_truth_present and r.extracted_present)
                or (r.ground_truth_present and r.extracted_present and not r.normalized_match)
            )
            tn = sum(1 for r in results if not r.ground_truth_present and not r.extracted_present)
            fn = sum(1 for r in results if r.ground_truth_present and not r.extracted_present)
            uncertain = sum(1 for r in results if r.notes and "UNCERTAIN" in r.notes)

            precision = (tp / (tp + fp)) if (tp + fp) > 0 else (1.0 if fn == 0 else 0.0)
            recall = (tp / (tp + fn)) if (tp + fn) > 0 else 1.0
            f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

            field_stats[cat] = FieldMetricStats(
                category=cat,
                total_evaluated=total,
                exact_matches=exact_matches,
                normalized_matches=norm_matches,
                true_positives=tp,
                false_positives=fp,
                true_negatives=tn,
                false_negatives=fn,
                uncertain_extractions=uncertain,
                precision=round(precision, 4),
                recall=round(recall, 4),
                f1_score=round(f1, 4),
                exact_match_accuracy=round((exact_matches / total) if total > 0 else 0.0, 4),
                normalized_accuracy=round((norm_matches / total) if total > 0 else 0.0, 4),
            )

        # Aggregate evidence stats
        tot_ev = len(all_evidence)
        page_correct = sum(1 for e in all_evidence if e.page_correct)
        found_on_page = sum(1 for e in all_evidence if e.quote_found_on_page)
        unsupported = sum(1 for e in all_evidence if not e.quote_found_on_page or not e.value_supported or not e.category_aligned)
        hallucinated = sum(1 for e in all_evidence if e.is_hallucinated)
        cross_page = sum(1 for e in all_evidence if e.is_cross_page_attribution)
        val_supported = sum(1 for e in all_evidence if e.value_supported)
        cat_aligned = sum(1 for e in all_evidence if e.category_aligned)
        digital_citations = sum(1 for e in all_evidence if not e.is_ocr_derived)
        ocr_citations = sum(1 for e in all_evidence if e.is_ocr_derived)

        evidence_stats = EvidenceMetricStats(
            total_citations=tot_ev,
            page_correct_count=page_correct,
            page_accuracy=round((page_correct / tot_ev) if tot_ev > 0 else 1.0, 4),
            found_on_page_count=found_on_page,
            unsupported_quote_count=unsupported,
            unsupported_rate=round((unsupported / tot_ev) if tot_ev > 0 else 0.0, 4),
            hallucinated_quote_count=hallucinated,
            hallucination_rate=round((hallucinated / tot_ev) if tot_ev > 0 else 0.0, 4),
            cross_page_count=cross_page,
            cross_page_rate=round((cross_page / tot_ev) if tot_ev > 0 else 0.0, 4),
            value_supported_count=val_supported,
            value_supported_rate=round((val_supported / tot_ev) if tot_ev > 0 else 1.0, 4),
            category_aligned_count=cat_aligned,
            category_aligned_rate=round((cat_aligned / tot_ev) if tot_ev > 0 else 1.0, 4),
            digital_citations_count=digital_citations,
            ocr_citations_count=ocr_citations,
        )

        return field_stats, evidence_stats
