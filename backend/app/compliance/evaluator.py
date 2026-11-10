"""Deterministic Insurance Compliance Evaluator.

This module contains pure, deterministic evaluation functions that accept:
1. Validated extracted document sets (ExtractedDocumentSet)
2. Configured insurance requirements (List[InsuranceRequirement])
3. An explicit evaluation date (date)

No external APIs, databases, or AI models are called.
Missing values never pass.
Ambiguities and contradictions result in uncertain / human-review states.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional, Set, Tuple

from app.schemas.insurance import (
    ComplianceFinding,
    ComplianceReviewResult,
    ComplianceStatus,
    CoverageExtraction,
    DocumentType,
    EndorsementExtraction,
    EvidenceReference,
    ExtractedDocumentSet,
    ExtractionStatus,
    InsuranceRequirement,
    RequirementType,
    Severity,
)


# Confidence threshold: extractions below this are deemed too uncertain for automated verification
CONFIDENCE_THRESHOLD = 0.70


# ============================================================================
# Helper Functions & Entity Matching Logic
# ============================================================================

# Suffix formatting map for canonical formatting normalization (does NOT equate distinct legal forms)
SUFFIX_FORMAT_MAP = {
    "llc": "llc",
    "l.l.c.": "llc",
    "l.l.c": "llc",
    "inc": "inc",
    "inc.": "inc",
    "incorporated": "inc",
    "corp": "corp",
    "corp.": "corp",
    "corporation": "corp",
    "co": "co",
    "co.": "co",
    "company": "co",
    "ltd": "ltd",
    "ltd.": "ltd",
    "limited": "ltd",
    "pllc": "pllc",
    "p.l.l.c.": "pllc",
    "lp": "lp",
    "l.p.": "lp",
}


def _split_core_and_suffix(name: str) -> Tuple[str, Optional[str]]:
    """Split an entity name into its core words and recognized legal suffix type."""
    cleaned = re.sub(r"[,\'\"]", "", name.strip()).lower()
    tokens = cleaned.split()
    if not tokens:
        return "", None
    last = tokens[-1]
    if last in SUFFIX_FORMAT_MAP:
        return " ".join(tokens[:-1]), SUFFIX_FORMAT_MAP[last]
    return " ".join(tokens), None


def _clean_entity_name(name: str) -> str:
    """Strip common punctuation, corporate suffix periods, and standardize whitespace.
    
    Preserves meaningful differences like Inc vs LLC vs Corp, but handles trailing periods,
    commas, and extra spaces.
    """
    cleaned = name.strip()
    # Replace multiple whitespaces/tabs with single space
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Remove surrounding quotes or commas
    cleaned = cleaned.strip(",'\"")
    return cleaned.strip()


def compare_entity_names(
    extracted_name: Optional[str],
    required_name: str,
    require_exact: bool = False,
) -> Tuple[bool, str]:
    """Compare an extracted business name against a required legal entity name.
    
    Conservative matching rules:
    - Preserves original legal entity names.
    - Never equates Inc, LLC, Corp, Ltd, or other legal suffixes.
    - Normalizes formatting punctuation (e.g. 'Apex, LLC.' vs 'Apex LLC').
    - Discrepancies in corporate designation trigger CORPORATE_SUFFIX_DISCREPANCY (requiring human review).
    
    Returns:
        (is_match, reason)
    """
    if not extracted_name or not extracted_name.strip():
        return False, "NO_NAME_EXTRACTED"

    cleaned_ext = _clean_entity_name(extracted_name)
    cleaned_req = _clean_entity_name(required_name)

    if not cleaned_ext or not cleaned_req:
        return False, "NO_NAME_EXTRACTED"

    # 1. Exact case-sensitive match
    if cleaned_ext == cleaned_req:
        return True, "EXACT_MATCH"

    # 2. Case-insensitive exact match
    if cleaned_ext.lower() == cleaned_req.lower():
        if require_exact:
            return False, "CASE_MISMATCH_EXACT_REQUIRED"
        return True, "CASE_INSENSITIVE_MATCH"

    if require_exact:
        return False, "NAME_MISMATCH"

    # 3. Formatting-normalized suffix match (e.g. 'Apex Contractors, LLC.' vs 'Apex Contractors LLC')
    core_ext, suffix_ext = _split_core_and_suffix(cleaned_ext)
    core_req, suffix_req = _split_core_and_suffix(cleaned_req)

    if core_ext and core_req:
        if core_ext == core_req:
            if suffix_ext == suffix_req and suffix_ext is not None:
                return True, "FORMAT_NORMALIZED_MATCH"
            if suffix_ext != suffix_req:
                # Different corporate suffixes (e.g. Inc vs LLC) or one missing suffix
                if suffix_ext is not None and suffix_req is not None:
                    return False, "CORPORATE_SUFFIX_DISCREPANCY"
                return False, "PARTIAL_OR_SUBSTRING_MISMATCH"

        # Check core substring match or trade name variation (e.g. 'Apex Electrical Inc' vs 'Apex Electrical Contractors LLC')
        if (core_ext in core_req or core_req in core_ext) or ("dba" in cleaned_ext.lower() or "dba" in cleaned_req.lower()):
            if suffix_ext != suffix_req and suffix_ext is not None and suffix_req is not None:
                return False, "CORPORATE_SUFFIX_DISCREPANCY"
            return False, "PARTIAL_OR_SUBSTRING_MISMATCH"

    # 4. Substring / partial checks
    ext_lower = cleaned_ext.lower()
    req_lower = cleaned_req.lower()
    if ext_lower and req_lower and (ext_lower in req_lower or req_lower in ext_lower):
        return False, "PARTIAL_OR_SUBSTRING_MISMATCH"

    return False, "NAME_MISMATCH"


def _aggregate_overall_status(findings: List[ComplianceFinding]) -> Tuple[ComplianceStatus, bool]:
    """Aggregate individual rule findings into an overall review status.
    
    Priority ordering:
    1. If ANY finding is NON_COMPLIANT -> NON_COMPLIANT
    2. If ANY finding is MISSING_EVIDENCE -> MISSING_EVIDENCE
    3. If ANY finding is UNABLE_TO_DETERMINE -> UNABLE_TO_DETERMINE
    4. If ANY finding is REQUIRES_HUMAN_REVIEW -> REQUIRES_HUMAN_REVIEW
    5. Only if ALL findings are COMPLIANT -> COMPLIANT
    """
    if not findings:
        return ComplianceStatus.UNABLE_TO_DETERMINE, True

    has_non_compliant = any(f.status == ComplianceStatus.NON_COMPLIANT for f in findings)
    if has_non_compliant:
        return ComplianceStatus.NON_COMPLIANT, True

    has_missing_evidence = any(f.status == ComplianceStatus.MISSING_EVIDENCE for f in findings)
    if has_missing_evidence:
        return ComplianceStatus.MISSING_EVIDENCE, True

    has_unable = any(f.status == ComplianceStatus.UNABLE_TO_DETERMINE for f in findings)
    if has_unable:
        return ComplianceStatus.UNABLE_TO_DETERMINE, True

    has_review = any(f.status == ComplianceStatus.REQUIRES_HUMAN_REVIEW for f in findings)
    if has_review:
        return ComplianceStatus.REQUIRES_HUMAN_REVIEW, True

    all_compliant = all(f.status == ComplianceStatus.COMPLIANT for f in findings)
    if all_compliant:
        return ComplianceStatus.COMPLIANT, False

    return ComplianceStatus.UNABLE_TO_DETERMINE, True


def _get_limit_field_evidence(
    cov: CoverageExtraction,
    limit_field: Optional[LimitField],
    limit_val: Any,
) -> Optional[EvidenceReference]:
    """Return field-specific evidence supporting limit_val without reusing unrelated limit quotes."""
    if limit_field is None or limit_val is None:
        return None
    from app.extraction.pipeline import _verify_money_field_support

    field_ev = getattr(limit_val, "evidence", None)
    if field_ev is not None and _verify_money_field_support(
        limit_field.value, limit_val.amount_cents, limit_val.raw_text, field_ev.source_text
    ):
        return field_ev

    if cov.evidence is not None and _verify_money_field_support(
        limit_field.value, limit_val.amount_cents, limit_val.raw_text, cov.evidence.source_text
    ):
        return cov.evidence

    return None


def _get_date_field_evidence(
    cov: CoverageExtraction,
    date_field: str,
    date_obj: Any,
) -> List[EvidenceReference]:
    """Return field-specific date evidence without reusing unrelated coverage limit quotes."""
    if date_obj is None:
        return []
    from app.extraction.pipeline import _verify_date_field_support

    field_ev = getattr(date_obj, "evidence", None)
    if field_ev is not None and _verify_date_field_support(
        date_field, date_obj.date, date_obj.raw_text, field_ev.source_text
    ):
        return [field_ev]

    if getattr(cov, "date_evidence", None):
        matched = [
            ref for ref in cov.date_evidence
            if _verify_date_field_support(date_field, date_obj.date, date_obj.raw_text, ref.source_text)
        ]
        if matched:
            return matched

    if cov.evidence is not None and _verify_date_field_support(
        date_field, date_obj.date, date_obj.raw_text, cov.evidence.source_text
    ):
        return [cov.evidence]

    return []


# ============================================================================
# Rule Evaluators
# ============================================================================

def _normalize_policy_token(policy_number: Optional[str]) -> Optional[str]:
    """Normalize delimiters and case on policy numbers while preserving digits and suffixes."""
    if not policy_number or not policy_number.strip():
        return None
    cleaned = re.sub(r"[\s\-/.]", "", policy_number.strip().upper())
    return cleaned or None


def _select_target_coverages(
    req: InsuranceRequirement,
    doc_set: ExtractedDocumentSet,
) -> Tuple[List[CoverageExtraction], bool]:
    """Select coverages matching req.coverage_type and optional req.target_policy_number.

    Returns:
        (matching_coverages, other_type_coverages_exist)
    """
    by_type = [c for c in doc_set.coverages if c.coverage_type == req.coverage_type]
    target_pol = _normalize_policy_token(getattr(req, "target_policy_number", None))
    if not target_pol:
        return by_type, False
    linked = [c for c in by_type if _normalize_policy_token(c.policy_number) == target_pol]
    return linked, bool(by_type and not linked)


def _deduplicate_evidence(refs: List[EvidenceReference]) -> List[EvidenceReference]:
    seen = set()
    unique: List[EvidenceReference] = []
    for ref in refs:
        key = (ref.document_id, ref.page_number, ref.source_text)
        if key not in seen:
            seen.add(key)
            unique.append(ref)
    return unique


def _missing_coverage_finding(
    req: InsuranceRequirement,
    doc_set: ExtractedDocumentSet,
    required_value: Dict[str, Any],
    *,
    target_policy_missing: bool = False,
) -> ComplianceFinding:
    target_pol = getattr(req, "target_policy_number", None)
    if not doc_set.is_document_set_complete:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.UNABLE_TO_DETERMINE,
            severity=req.severity,
            reason_code="INCOMPLETE_DOCUMENT_SET_COVERAGE_UNKNOWN",
            explanation=(
                f"Required {req.coverage_type.value} coverage"
                + (f" for policy '{target_pol}'" if target_pol else "")
                + " was not found, and the submitted document set is flagged as incomplete."
            ),
            requires_human_review=True,
            required_value=required_value,
            found_value={"target_policy_number": target_pol} if target_pol else None,
            evidence=[],
        )

    if target_policy_missing and target_pol:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.MISSING_EVIDENCE,
            severity=req.severity,
            reason_code="TARGET_POLICY_NOT_FOUND",
            explanation=(
                f"Target policy '{target_pol}' for {req.coverage_type.value} was not found; "
                "unrelated policies of the same coverage type cannot satisfy this requirement."
            ),
            requires_human_review=True,
            required_value=required_value,
            found_value={"target_policy_number": target_pol},
            evidence=[],
        )

    return ComplianceFinding(
        requirement_id=req.requirement_id,
        rule_type=req.rule_type,
        status=ComplianceStatus.MISSING_EVIDENCE,
        severity=req.severity,
        reason_code="COVERAGE_NOT_FOUND",
        explanation=f"No {req.coverage_type.value} coverage section found in uploaded documents.",
        requires_human_review=True,
        required_value=required_value,
        found_value=None,
        evidence=[],
    )


def evaluate_coverage_limit(
    req: InsuranceRequirement,
    doc_set: ExtractedDocumentSet,
) -> ComplianceFinding:
    """Evaluate whether an extracted coverage limit meets or exceeds the required minimum."""
    matching_coverages, target_missing = _select_target_coverages(req, doc_set)

    if not matching_coverages:
        return _missing_coverage_finding(
            req,
            doc_set,
            {"min_amount_cents": req.min_amount_cents, "limit_field": req.limit_field.value},
            target_policy_missing=target_missing,
        )

    # Check for contradictions across all matching policies of this coverage type
    extracted_limits = []
    missing_limit_covs = []
    evidences = []
    for cov in matching_coverages:
        limit_val = getattr(cov, req.limit_field.value, None)
        if limit_val is not None:
            extracted_limits.append((cov, limit_val))
            field_ev = _get_limit_field_evidence(cov, req.limit_field, limit_val)
            if field_ev:
                evidences.append(field_ev)
        else:
            missing_limit_covs.append(cov)

    if not extracted_limits:
        if not doc_set.is_document_set_complete:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.UNABLE_TO_DETERMINE,
                severity=req.severity,
                reason_code="INCOMPLETE_DOCUMENT_SET_COVERAGE_UNKNOWN",
                explanation=(
                    f"{req.coverage_type.value} section exists, but limit field '{req.limit_field.value}' "
                    "was not found and the document set is flagged as incomplete."
                ),
                requires_human_review=True,
                required_value={"min_amount_cents": req.min_amount_cents, "limit_field": req.limit_field.value},
                found_value=None,
                evidence=[cov.evidence for cov in matching_coverages if cov.evidence],
            )

        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.MISSING_EVIDENCE,
            severity=req.severity,
            reason_code="LIMIT_FIELD_NOT_EXTRACTED",
            explanation=(
                f"{req.coverage_type.value} section exists, but limit field "
                f"'{req.limit_field.value}' was not found or is blank."
            ),
            requires_human_review=True,
            required_value={"min_amount_cents": req.min_amount_cents, "limit_field": req.limit_field.value},
            found_value=None,
            evidence=[cov.evidence for cov in matching_coverages if cov.evidence],
        )

    # Check if multiple contradictory limits exist
    distinct_amounts = {lim.amount_cents for _, lim in extracted_limits}
    if len(distinct_amounts) > 1:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.UNABLE_TO_DETERMINE,
            severity=req.severity,
            reason_code="CONTRADICTORY_LIMITS_FOUND",
            explanation=(
                f"Contradictory limits found for {req.coverage_type.value} {req.limit_field.value}: "
                f"{[f'${amt/100:,.2f}' for amt in distinct_amounts]}"
            ),
            requires_human_review=True,
            required_value={"min_amount_cents": req.min_amount_cents},
            found_value={"amounts_found_cents": list(distinct_amounts)},
            evidence=_deduplicate_evidence(evidences),
        )

    # If multiple matching coverages exist without target policy linkage, ensure we don't hide
    # unlinked or distinct policies missing the limit field (same-policy empty endorsement stubs are exempt).
    if len(matching_coverages) > 1 and not getattr(req, "target_policy_number", None):
        distinct_pols = {_normalize_policy_token(c.policy_number) for c in matching_coverages}
        unlinked_or_multi_pol = len(distinct_pols) > 1 or None in distinct_pols
        non_endorsement_missing = [
            c for c in missing_limit_covs
            if not (c.evidence and c.evidence.document_type == DocumentType.ENDORSEMENT)
        ]
        if unlinked_or_multi_pol or non_endorsement_missing:
            all_cov_evs = _deduplicate_evidence(
                evidences + [c.evidence for c in matching_coverages if c.evidence]
            )
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                severity=Severity.MAJOR,
                reason_code="AMBIGUOUS_APPLICABLE_POLICY",
                explanation=(
                    f"Multiple {req.coverage_type.value} coverage sections exist without unique target policy linkage. "
                    "Human review required to confirm the applicable policy limit."
                ),
                requires_human_review=True,
                required_value={"min_amount_cents": req.min_amount_cents},
                found_value={"policy_numbers": [c.policy_number for c in matching_coverages]},
                evidence=all_cov_evs,
            )

    cov, limit_obj = extracted_limits[0]
    field_ev = _get_limit_field_evidence(cov, req.limit_field, limit_obj)
    evidences = [field_ev] if field_ev else []

    # Check extraction status across all extracted_limits entries (never ignore later duplicate entries)
    for chk_cov, chk_lim in extracted_limits:
        if chk_cov.extraction_status == ExtractionStatus.PARTIAL:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                severity=req.severity,
                reason_code="COVERAGE_PARTIAL_EXTRACTION",
                explanation=(
                    f"Extracted {req.coverage_type.value} {req.limit_field.value} limit has PARTIAL extraction status. "
                    "Incomplete extraction records require human review."
                ),
                requires_human_review=True,
                required_value={"min_amount_cents": req.min_amount_cents},
                found_value={"amount_cents": chk_lim.amount_cents, "raw_text": chk_lim.raw_text},
                evidence=evidences,
            )

        if chk_cov.extraction_status == ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                severity=req.severity,
                reason_code="EVIDENCE_SUPPORT_UNVERIFIED",
                explanation=(
                    f"Extracted {req.coverage_type.value} {req.limit_field.value} limit of "
                    f"${chk_lim.amount_cents/100:,.2f} has cited document text whose semantic support "
                    "for this limit value is unverified. Human review required."
                ),
                requires_human_review=True,
                required_value={"min_amount_cents": req.min_amount_cents},
                found_value={"amount_cents": chk_lim.amount_cents, "raw_text": chk_lim.raw_text},
                evidence=evidences,
            )

        if chk_cov.extraction_status == ExtractionStatus.UNCERTAIN or (
            chk_lim.confidence is not None and chk_lim.confidence < CONFIDENCE_THRESHOLD
        ):
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.UNABLE_TO_DETERMINE,
                severity=req.severity,
                reason_code="EXTRACTION_CONFIDENCE_LOW",
                explanation=(
                    f"Extracted {req.coverage_type.value} {req.limit_field.value} limit of "
                    f"${chk_lim.amount_cents/100:,.2f} has low extraction confidence "
                    f"({chk_lim.confidence or 'uncertain'}). Human verification required."
                ),
                requires_human_review=True,
                required_value={"min_amount_cents": req.min_amount_cents},
                found_value={"amount_cents": chk_lim.amount_cents, "raw_text": chk_lim.raw_text},
                evidence=evidences,
            )

    # Core coverage limit rules always require verifiable field evidence before returning COMPLIANT
    if not evidences:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.MISSING_EVIDENCE,
            severity=req.severity,
            reason_code="LIMIT_EVIDENCE_MISSING",
            explanation=(
                f"{req.coverage_type.value} {req.limit_field.value} limit of "
                f"${limit_obj.amount_cents/100:,.2f} lacks verifiable document evidence references."
            ),
            requires_human_review=True,
            required_value={"min_amount_cents": req.min_amount_cents, "mandatory_evidence": True},
            found_value={"amount_cents": limit_obj.amount_cents, "raw_text": limit_obj.raw_text},
            evidence=[],
        )

    # Evaluate limit comparison
    if limit_obj.amount_cents >= req.min_amount_cents:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.COMPLIANT,
            severity=Severity.INFO,
            reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
            explanation=(
                f"{req.coverage_type.value} {req.limit_field.value} of ${limit_obj.amount_cents/100:,.2f} "
                f"meets or exceeds required minimum of ${req.min_amount_cents/100:,.2f}."
            ),
            requires_human_review=False,
            required_value={"min_amount_cents": req.min_amount_cents},
            found_value={"amount_cents": limit_obj.amount_cents, "raw_text": limit_obj.raw_text},
            evidence=evidences,
        )
    else:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.NON_COMPLIANT,
            severity=req.severity,
            reason_code="LIMIT_BELOW_MINIMUM",
            explanation=(
                f"{req.coverage_type.value} {req.limit_field.value} of ${limit_obj.amount_cents/100:,.2f} "
                f"is below required minimum of ${req.min_amount_cents/100:,.2f}."
            ),
            requires_human_review=True,
            required_value={"min_amount_cents": req.min_amount_cents},
            found_value={"amount_cents": limit_obj.amount_cents, "raw_text": limit_obj.raw_text},
            evidence=evidences,
        )


def _evaluate_single_policy_expiration(
    req: InsuranceRequirement,
    cov: CoverageExtraction,
    doc_set: ExtractedDocumentSet,
    evaluation_date: date,
) -> ComplianceFinding:
    """Evaluate policy expiration for a single coverage record without borrowing from sibling coverages."""
    if not cov.expiration_date:
        if not doc_set.is_document_set_complete:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.UNABLE_TO_DETERMINE,
                severity=req.severity,
                reason_code="INCOMPLETE_DOCUMENT_SET_COVERAGE_UNKNOWN",
                explanation=f"No policy expiration date extracted for {req.coverage_type.value} and document set is incomplete.",
                requires_human_review=True,
                required_value={"evaluation_date": evaluation_date.isoformat()},
                found_value=None,
                evidence=[cov.evidence] if cov.evidence else [],
            )

        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.MISSING_EVIDENCE,
            severity=req.severity,
            reason_code="EXPIRATION_DATE_MISSING",
            explanation=f"No policy expiration date extracted for {req.coverage_type.value}.",
            requires_human_review=True,
            required_value={"evaluation_date": evaluation_date.isoformat()},
            found_value=None,
            evidence=[cov.evidence] if cov.evidence else [],
        )

    exp_date_obj = cov.expiration_date
    evidence_list = _get_date_field_evidence(cov, "expiration", exp_date_obj)

    if cov.extraction_status == ExtractionStatus.PARTIAL:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=req.severity,
            reason_code="POLICY_EXPIRATION_PARTIAL_EXTRACTION",
            explanation=(
                f"Policy expiration date for {req.coverage_type.value} has PARTIAL extraction status. "
                "Human review required."
            ),
            requires_human_review=True,
            required_value={"evaluation_date": evaluation_date.isoformat()},
            found_value={"date": exp_date_obj.date.isoformat(), "raw_text": exp_date_obj.raw_text},
            evidence=evidence_list,
        )

    if cov.extraction_status == ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=req.severity,
            reason_code="EXPIRATION_DATE_EVIDENCE_SUPPORT_UNVERIFIED",
            explanation=(
                f"Extracted {req.coverage_type.value} expiration date has cited document text "
                "whose semantic support for this date is unverified. Human review required."
            ),
            requires_human_review=True,
            required_value={"evaluation_date": evaluation_date.isoformat()},
            found_value={"date": exp_date_obj.date.isoformat(), "raw_text": exp_date_obj.raw_text},
            evidence=evidence_list,
        )

    if cov.extraction_status == ExtractionStatus.UNCERTAIN or (
        exp_date_obj.confidence is not None and exp_date_obj.confidence < CONFIDENCE_THRESHOLD
    ):
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.UNABLE_TO_DETERMINE,
            severity=req.severity,
            reason_code="EXPIRATION_DATE_CONFIDENCE_LOW",
            explanation=(
                f"Extracted expiration date {exp_date_obj.date.isoformat()} has low confidence "
                f"({exp_date_obj.confidence or 'uncertain'}). Human review required."
            ),
            requires_human_review=True,
            required_value={"evaluation_date": evaluation_date.isoformat()},
            found_value={"date": exp_date_obj.date.isoformat(), "raw_text": exp_date_obj.raw_text},
            evidence=evidence_list,
        )

    # Core date rules always require verifiable date evidence before passing as COMPLIANT
    if not evidence_list:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.MISSING_EVIDENCE,
            severity=req.severity,
            reason_code="EXPIRATION_DATE_EVIDENCE_MISSING",
            explanation=(
                f"{req.coverage_type.value} expiration date lacks verifiable document evidence references."
            ),
            requires_human_review=True,
            required_value={"evaluation_date": evaluation_date.isoformat(), "mandatory_evidence": True},
            found_value={"expiration_date": exp_date_obj.date.isoformat()},
            evidence=[],
        )

    if exp_date_obj.date < evaluation_date:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.NON_COMPLIANT,
            severity=Severity.CRITICAL,
            reason_code="POLICY_EXPIRED",
            explanation=(
                f"{req.coverage_type.value} policy expired on {exp_date_obj.date.isoformat()} "
                f"(evaluation date: {evaluation_date.isoformat()})."
            ),
            requires_human_review=True,
            required_value={"min_valid_date": evaluation_date.isoformat()},
            found_value={"expiration_date": exp_date_obj.date.isoformat()},
            evidence=evidence_list,
        )
    if exp_date_obj.date == evaluation_date:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.NON_COMPLIANT,
            severity=Severity.CRITICAL,
            reason_code="POLICY_EXPIRES_TODAY",
            explanation=(
                f"{req.coverage_type.value} policy expires today ({exp_date_obj.date.isoformat()}). "
                "Immediate renewal required."
            ),
            requires_human_review=True,
            required_value={"min_valid_date": evaluation_date.isoformat()},
            found_value={"expiration_date": exp_date_obj.date.isoformat()},
            evidence=evidence_list,
        )
    if req.required_coverage_end_date and exp_date_obj.date < req.required_coverage_end_date:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.NON_COMPLIANT,
            severity=req.severity,
            reason_code="POLICY_PREMATURE_EXPIRATION",
            explanation=(
                f"{req.coverage_type.value} policy expires on {exp_date_obj.date.isoformat()}, "
                f"which precedes the contractual requirement deadline ({req.required_coverage_end_date.isoformat()})."
            ),
            requires_human_review=True,
            required_value={"required_coverage_end_date": req.required_coverage_end_date.isoformat()},
            found_value={"expiration_date": exp_date_obj.date.isoformat()},
            evidence=evidence_list,
        )
    return ComplianceFinding(
        requirement_id=req.requirement_id,
        rule_type=req.rule_type,
        status=ComplianceStatus.COMPLIANT,
        severity=Severity.INFO,
        reason_code="POLICY_ACTIVE_AND_CURRENT",
        explanation=(
            f"{req.coverage_type.value} policy is active through {exp_date_obj.date.isoformat()} "
            f"(current as of {evaluation_date.isoformat()})."
        ),
        requires_human_review=False,
        required_value={"evaluation_date": evaluation_date.isoformat()},
        found_value={"expiration_date": exp_date_obj.date.isoformat()},
        evidence=evidence_list,
    )


def _aggregate_multi_coverage_date_findings(
    req: InsuranceRequirement,
    matching_coverages: List[CoverageExtraction],
    per_cov_findings: List[ComplianceFinding],
    date_attr: str,
    evaluation_date: date,
) -> ComplianceFinding:
    """Aggregate individual coverage date findings without blindly picking index 0 or blending policies."""
    if len(per_cov_findings) == 1:
        return per_cov_findings[0]

    pol_tokens = {_normalize_policy_token(c.policy_number) for c in matching_coverages}
    has_unlinked = None in pol_tokens

    # If every coverage belongs to the SAME linked policy number, filter out empty endorsement
    # policy-reference stubs (no effective_date and no expiration_date from an endorsement doc),
    # while never filtering split date sections where one date is set and the other is missing.
    if len(pol_tokens) == 1 and not has_unlinked:
        substantive_pairs = [
            (cov, f)
            for cov, f in zip(matching_coverages, per_cov_findings)
            if not (
                cov.effective_date is None
                and cov.expiration_date is None
                and cov.evidence is not None
                and cov.evidence.document_type == DocumentType.ENDORSEMENT
            )
        ]
        if substantive_pairs:
            matching_coverages = [cov for cov, _ in substantive_pairs]
            per_cov_findings = [f for _, f in substantive_pairs]
            if len(per_cov_findings) == 1:
                return per_cov_findings[0]

    all_evidence = _deduplicate_evidence(
        [ev for f in per_cov_findings for ev in f.evidence]
        + [c.evidence for c in matching_coverages if c.evidence]
    )
    dates_found = sorted({
        getattr(c, date_attr).date.isoformat()
        for c in matching_coverages
        if getattr(c, date_attr) is not None and getattr(c, date_attr).date is not None
    })
    distinct_statuses = {f.status for f in per_cov_findings}
    distinct_reasons = {f.reason_code for f in per_cov_findings}

    # Conflicting dates on coverages sharing same policy identity or unlinked sections -> CONTRADICTORY_POLICY_DATES
    if len(dates_found) > 1 and (len(pol_tokens) == 1 or has_unlinked):
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.UNABLE_TO_DETERMINE,
            severity=Severity.CRITICAL,
            reason_code="CONTRADICTORY_POLICY_DATES",
            explanation=(
                f"Conflicting {date_attr.replace('_', ' ')}s extracted for {req.coverage_type.value}: "
                f"{dates_found}. Human verification required."
            ),
            requires_human_review=True,
            required_value={"evaluation_date": evaluation_date.isoformat()},
            found_value={
                "dates_found": dates_found,
                "policy_numbers": [c.policy_number for c in matching_coverages],
            },
            evidence=all_evidence,
        )

    # Multiple distinct policies or divergent per-policy outcomes without explicit target_policy_number
    if not getattr(req, "target_policy_number", None) and (len(pol_tokens) > 1 or has_unlinked):
        # If every policy definitively failed for the exact same reason, preserve NON_COMPLIANT;
        # if mixed (e.g. one expired, one active) or differing active dates -> AMBIGUOUS_APPLICABLE_POLICY.
        if distinct_statuses == {ComplianceStatus.NON_COMPLIANT} and len(distinct_reasons) == 1:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.NON_COMPLIANT,
                severity=per_cov_findings[0].severity,
                reason_code=per_cov_findings[0].reason_code,
                explanation=(
                    f"All {len(matching_coverages)} matching {req.coverage_type.value} coverages failed "
                    f"({per_cov_findings[0].reason_code})."
                ),
                requires_human_review=True,
                required_value=per_cov_findings[0].required_value,
                found_value={
                    "dates_found": dates_found,
                    "policy_numbers": [c.policy_number for c in matching_coverages],
                },
                evidence=all_evidence,
            )
        if distinct_statuses == {ComplianceStatus.MISSING_EVIDENCE}:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.MISSING_EVIDENCE,
                severity=per_cov_findings[0].severity,
                reason_code=per_cov_findings[0].reason_code,
                explanation=(
                    f"All {len(matching_coverages)} matching {req.coverage_type.value} coverages lack "
                    f"verified {date_attr.replace('_', ' ')} evidence."
                ),
                requires_human_review=True,
                required_value=per_cov_findings[0].required_value,
                found_value={"policy_numbers": [c.policy_number for c in matching_coverages]},
                evidence=all_evidence,
            )
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=Severity.MAJOR,
            reason_code="AMBIGUOUS_APPLICABLE_POLICY",
            explanation=(
                f"Multiple {req.coverage_type.value} coverages ({[c.policy_number or 'unlinked' for c in matching_coverages]}) "
                f"found with {date_attr.replace('_', ' ')}s {dates_found} and no target_policy_number linkage. "
                "Human review required."
            ),
            requires_human_review=True,
            required_value={"evaluation_date": evaluation_date.isoformat()},
            found_value={
                "dates_found": dates_found,
                "policy_numbers": [c.policy_number for c in matching_coverages],
                "per_policy_statuses": [f.status.value for f in per_cov_findings],
            },
            evidence=all_evidence,
        )

    # Same linked/target policy across multiple records: fail closed to worst status if any non-compliant/uncertain
    for priority_status in (
        ComplianceStatus.NON_COMPLIANT,
        ComplianceStatus.UNABLE_TO_DETERMINE,
        ComplianceStatus.MISSING_EVIDENCE,
        ComplianceStatus.REQUIRES_HUMAN_REVIEW,
    ):
        for f in per_cov_findings:
            if f.status == priority_status:
                return f.model_copy(update={"evidence": all_evidence if f.evidence else []})

    return per_cov_findings[0].model_copy(update={"evidence": all_evidence})


def evaluate_policy_expiration(
    req: InsuranceRequirement,
    doc_set: ExtractedDocumentSet,
    evaluation_date: date,
) -> ComplianceFinding:
    """Evaluate policy expiration across all matching coverages for req."""
    matching_coverages, target_missing = _select_target_coverages(req, doc_set)

    if not matching_coverages:
        return _missing_coverage_finding(
            req,
            doc_set,
            {"evaluation_date": evaluation_date.isoformat()},
            target_policy_missing=target_missing,
        )

    per_cov_findings = [
        _evaluate_single_policy_expiration(req, cov, doc_set, evaluation_date)
        for cov in matching_coverages
    ]
    return _aggregate_multi_coverage_date_findings(
        req, matching_coverages, per_cov_findings, "expiration_date", evaluation_date
    )


def _evaluate_single_policy_effective_date(
    req: InsuranceRequirement,
    cov: CoverageExtraction,
    doc_set: ExtractedDocumentSet,
    evaluation_date: date,
) -> ComplianceFinding:
    """Evaluate policy effective date for a single coverage record without borrowing from sibling coverages."""
    if not cov.effective_date:
        if not doc_set.is_document_set_complete:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.UNABLE_TO_DETERMINE,
                severity=req.severity,
                reason_code="INCOMPLETE_DOCUMENT_SET_COVERAGE_UNKNOWN",
                explanation=f"No policy effective date extracted for {req.coverage_type.value} and document set is incomplete.",
                requires_human_review=True,
                required_value={"evaluation_date": evaluation_date.isoformat()},
                found_value=None,
                evidence=[cov.evidence] if cov.evidence else [],
            )

        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.MISSING_EVIDENCE,
            severity=req.severity,
            reason_code="EFFECTIVE_DATE_MISSING",
            explanation=f"No policy effective date extracted for {req.coverage_type.value}.",
            requires_human_review=True,
            required_value={"evaluation_date": evaluation_date.isoformat()},
            found_value=None,
            evidence=[cov.evidence] if cov.evidence else [],
        )

    eff_date_obj = cov.effective_date
    evidence_list = _get_date_field_evidence(cov, "effective", eff_date_obj)

    if cov.extraction_status == ExtractionStatus.PARTIAL:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=req.severity,
            reason_code="POLICY_EFFECTIVE_DATE_PARTIAL_EXTRACTION",
            explanation=(
                f"Policy effective date for {req.coverage_type.value} has PARTIAL extraction status. "
                "Human review required."
            ),
            requires_human_review=True,
            required_value={"evaluation_date": evaluation_date.isoformat()},
            found_value={"date": eff_date_obj.date.isoformat(), "raw_text": eff_date_obj.raw_text},
            evidence=evidence_list,
        )

    if cov.extraction_status == ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=req.severity,
            reason_code="EFFECTIVE_DATE_EVIDENCE_SUPPORT_UNVERIFIED",
            explanation=(
                f"Extracted {req.coverage_type.value} effective date has cited document text "
                "whose semantic support for this date is unverified. Human review required."
            ),
            requires_human_review=True,
            required_value={"evaluation_date": evaluation_date.isoformat()},
            found_value={"date": eff_date_obj.date.isoformat(), "raw_text": eff_date_obj.raw_text},
            evidence=evidence_list,
        )

    if cov.extraction_status == ExtractionStatus.UNCERTAIN or (
        eff_date_obj.confidence is not None and eff_date_obj.confidence < CONFIDENCE_THRESHOLD
    ):
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.UNABLE_TO_DETERMINE,
            severity=req.severity,
            reason_code="EFFECTIVE_DATE_CONFIDENCE_LOW",
            explanation=(
                f"Extracted effective date {eff_date_obj.date.isoformat()} has low confidence "
                f"({eff_date_obj.confidence or 'uncertain'}). Human review required."
            ),
            requires_human_review=True,
            required_value={"evaluation_date": evaluation_date.isoformat()},
            found_value={"date": eff_date_obj.date.isoformat(), "raw_text": eff_date_obj.raw_text},
            evidence=evidence_list,
        )

    # Core date rules always require verifiable date evidence before passing as COMPLIANT
    if not evidence_list:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.MISSING_EVIDENCE,
            severity=req.severity,
            reason_code="EFFECTIVE_DATE_EVIDENCE_MISSING",
            explanation=(
                f"{req.coverage_type.value} effective date lacks verifiable document evidence references."
            ),
            requires_human_review=True,
            required_value={"evaluation_date": evaluation_date.isoformat(), "mandatory_evidence": True},
            found_value={"effective_date": eff_date_obj.date.isoformat()},
            evidence=[],
        )

    if cov.expiration_date and eff_date_obj.date > cov.expiration_date.date:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.NON_COMPLIANT,
            severity=Severity.CRITICAL,
            reason_code="INVALID_DATE_RANGE_INVERTED",
            explanation=(
                f"Policy effective date ({eff_date_obj.date.isoformat()}) is after its "
                f"expiration date ({cov.expiration_date.date.isoformat()})."
            ),
            requires_human_review=True,
            required_value={"effective_before": cov.expiration_date.date.isoformat()},
            found_value={"effective_date": eff_date_obj.date.isoformat()},
            evidence=evidence_list,
        )

    if eff_date_obj.date > evaluation_date:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.NON_COMPLIANT,
            severity=Severity.MAJOR,
            reason_code="POLICY_FUTURE_INCEPTION",
            explanation=(
                f"{req.coverage_type.value} policy inception date ({eff_date_obj.date.isoformat()}) "
                f"is in the future relative to evaluation date ({evaluation_date.isoformat()}). Policy is not yet in force."
            ),
            requires_human_review=True,
            required_value={"must_be_effective_on_or_before": evaluation_date.isoformat()},
            found_value={"effective_date": eff_date_obj.date.isoformat()},
            evidence=evidence_list,
        )

    return ComplianceFinding(
        requirement_id=req.requirement_id,
        rule_type=req.rule_type,
        status=ComplianceStatus.COMPLIANT,
        severity=Severity.INFO,
        reason_code="EFFECTIVE_DATE_VALID",
        explanation=(
            f"{req.coverage_type.value} policy effective date {eff_date_obj.date.isoformat()} "
            f"is active and in force as of {evaluation_date.isoformat()}."
        ),
        requires_human_review=False,
        required_value={"evaluation_date": evaluation_date.isoformat()},
        found_value={"effective_date": eff_date_obj.date.isoformat()},
        evidence=evidence_list,
    )


def evaluate_policy_effective_date(
    req: InsuranceRequirement,
    doc_set: ExtractedDocumentSet,
    evaluation_date: date,
) -> ComplianceFinding:
    """Evaluate policy effective date across all matching coverages for req."""
    matching_coverages, target_missing = _select_target_coverages(req, doc_set)

    if not matching_coverages:
        return _missing_coverage_finding(
            req,
            doc_set,
            {"evaluation_date": evaluation_date.isoformat()},
            target_policy_missing=target_missing,
        )

    per_cov_findings = [
        _evaluate_single_policy_effective_date(req, cov, doc_set, evaluation_date)
        for cov in matching_coverages
    ]
    return _aggregate_multi_coverage_date_findings(
        req, matching_coverages, per_cov_findings, "effective_date", evaluation_date
    )


def evaluate_named_insured(
    req: InsuranceRequirement,
    doc_set: ExtractedDocumentSet,
) -> ComplianceFinding:
    """Evaluate whether the named insured on the certificate matches the required entity name."""
    if not doc_set.named_insured or not doc_set.named_insured.name or not doc_set.named_insured.name.strip():
        evidence_list = [doc_set.named_insured.evidence] if (doc_set.named_insured and doc_set.named_insured.evidence) else []
        if not doc_set.is_document_set_complete:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.UNABLE_TO_DETERMINE,
                severity=req.severity,
                reason_code="INCOMPLETE_DOCUMENT_SET_NAMED_INSURED_UNKNOWN",
                explanation="No named insured entity name found and the document set is flagged as incomplete.",
                requires_human_review=True,
                required_value={"required_entity_name": req.required_entity_name},
                found_value=None,
                evidence=evidence_list,
            )

        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.MISSING_EVIDENCE,
            severity=req.severity,
            reason_code="NAMED_INSURED_NOT_FOUND",
            explanation="No named insured entity name found in extracted documents.",
            requires_human_review=True,
            required_value={"required_entity_name": req.required_entity_name},
            found_value=None,
            evidence=evidence_list,
        )

    ni = doc_set.named_insured
    evidence_list = [ni.evidence] if ni.evidence else []

    # Check extraction status: PARTIAL
    if ni.extraction_status == ExtractionStatus.PARTIAL:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=req.severity,
            reason_code="NAMED_INSURED_PARTIAL_EXTRACTION",
            explanation=(
                f"Named insured '{ni.name or ''}' has PARTIAL extraction status. "
                "Entity information may be truncated. Human review required."
            ),
            requires_human_review=True,
            required_value={"required_entity_name": req.required_entity_name},
            found_value={"extracted_name": ni.name},
            evidence=evidence_list,
        )

    if ni.extraction_status == ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=req.severity,
            reason_code="NAMED_INSURED_EVIDENCE_SUPPORT_UNVERIFIED",
            explanation=(
                f"Named insured '{ni.name or ''}' has source quote whose semantic support "
                "for this entity name is unverified. Human review required."
            ),
            requires_human_review=True,
            required_value={"required_entity_name": req.required_entity_name},
            found_value={"extracted_name": ni.name},
            evidence=evidence_list,
        )

    if ni.extraction_status == ExtractionStatus.UNCERTAIN or (
        ni.confidence is not None and ni.confidence < CONFIDENCE_THRESHOLD
    ):
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.UNABLE_TO_DETERMINE,
            severity=req.severity,
            reason_code="NAMED_INSURED_UNCERTAIN",
            explanation=(
                f"Named insured '{ni.name or ''}' was extracted with low confidence "
                f"({ni.confidence or 'uncertain'}). Human review required."
            ),
            requires_human_review=True,
            required_value={"required_entity_name": req.required_entity_name},
            found_value={"extracted_name": ni.name},
            evidence=evidence_list,
        )

    # Verify evidence text semantically supports extracted named insured (defense-in-depth)
    from app.extraction.pipeline import _verify_entity_semantic_support

    if ni.evidence is not None and not _verify_entity_semantic_support(ni.name, ni.evidence.source_text):
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=req.severity,
            reason_code="NAMED_INSURED_EVIDENCE_SUPPORT_UNVERIFIED",
            explanation=(
                f"Named insured '{ni.name}' is not verified by the cited source quote "
                f"('{ni.evidence.source_text}'). Human review required."
            ),
            requires_human_review=True,
            required_value={"required_entity_name": req.required_entity_name},
            found_value={"extracted_name": ni.name},
            evidence=evidence_list,
        )

    is_match, reason = compare_entity_names(
        extracted_name=ni.name,
        required_name=req.required_entity_name,
        require_exact=req.require_exact_name_match,
    )

    # Core named insured rules always require verifiable evidence before returning COMPLIANT
    if is_match and not evidence_list:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.MISSING_EVIDENCE,
            severity=req.severity,
            reason_code="NAMED_INSURED_EVIDENCE_MISSING",
            explanation=(
                f"Named insured '{ni.name}' matched required entity '{req.required_entity_name}', "
                "but lacks supporting document evidence references."
            ),
            requires_human_review=True,
            required_value={"required_entity_name": req.required_entity_name, "mandatory_evidence": True},
            found_value={"extracted_name": ni.name},
            evidence=[],
        )

    if is_match:
        # Check additional_named_insureds across secondary packet documents for conflicting entities
        for extra_ni in getattr(doc_set, "additional_named_insureds", []) or []:
            if not extra_ni or not extra_ni.name or not extra_ni.name.strip():
                continue
            extra_match, _ = compare_entity_names(
                extracted_name=extra_ni.name,
                required_name=req.required_entity_name,
                require_exact=req.require_exact_name_match,
            )
            if not extra_match:
                combined_ev = _deduplicate_evidence(
                    evidence_list + ([extra_ni.evidence] if extra_ni.evidence else [])
                )
                return ComplianceFinding(
                    requirement_id=req.requirement_id,
                    rule_type=req.rule_type,
                    status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                    severity=Severity.MAJOR,
                    reason_code="CONFLICTING_NAMED_INSURED_ENTITIES",
                    explanation=(
                        f"Primary named insured '{ni.name}' matched '{req.required_entity_name}', "
                        f"but another packet document named '{extra_ni.name}'. Human review required."
                    ),
                    requires_human_review=True,
                    required_value={"required_entity_name": req.required_entity_name},
                    found_value={"primary_name": ni.name, "conflicting_name": extra_ni.name},
                    evidence=combined_ev,
                )

        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.COMPLIANT,
            severity=Severity.INFO,
            reason_code=f"NAMED_INSURED_{reason}",
            explanation=f"Named insured '{ni.name}' matches required entity '{req.required_entity_name}'.",
            requires_human_review=False,
            required_value={"required_entity_name": req.required_entity_name},
            found_value={"extracted_name": ni.name},
            evidence=evidence_list,
        )
    elif reason == "NO_NAME_EXTRACTED":
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.MISSING_EVIDENCE,
            severity=req.severity,
            reason_code="NAMED_INSURED_NOT_FOUND",
            explanation="Named insured entity name is missing, empty, or blank in extracted documents.",
            requires_human_review=True,
            required_value={"required_entity_name": req.required_entity_name},
            found_value=None,
            evidence=evidence_list,
        )
    elif reason in ("PARTIAL_OR_SUBSTRING_MISMATCH", "CORPORATE_SUFFIX_DISCREPANCY"):
        code = "NAMED_INSURED_PARTIAL_MATCH" if reason == "PARTIAL_OR_SUBSTRING_MISMATCH" else f"NAMED_INSURED_{reason}"
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=Severity.MAJOR,
            reason_code=code,
            explanation=(
                f"Extracted named insured '{ni.name}' partially resembles required entity "
                f"'{req.required_entity_name}' but differs in structure or corporate designation (e.g. Inc vs LLC). "
                "Human review required to verify legal entity."
            ),
            requires_human_review=True,
            required_value={"required_entity_name": req.required_entity_name},
            found_value={"extracted_name": ni.name},
            evidence=evidence_list,
        )
    else:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.NON_COMPLIANT,
            severity=req.severity,
            reason_code="NAMED_INSURED_MISMATCH",
            explanation=(
                f"Extracted named insured '{ni.name}' does not match required entity "
                f"'{req.required_entity_name}'."
            ),
            requires_human_review=True,
            required_value={"required_entity_name": req.required_entity_name},
            found_value={"extracted_name": ni.name},
            evidence=evidence_list,
        )


def is_recognized_standard_form(form_number: Optional[str]) -> bool:
    """Check if form number corresponds to a recognized standard ISO or bureau endorsement."""
    if not form_number:
        return False
    norm = form_number.upper().strip()
    standard_prefixes = (
        "CG 20", "CG 21", "CG 24", "CG 25", "CG 00",
        "CA 20", "CA 00",
        "WC 00", "WC 04",
        "IL 00", "IL 12",
        "BP 04", "CP 00",
    )
    return any(norm.startswith(p) for p in standard_prefixes)


def evaluate_endorsement_presence(
    req: InsuranceRequirement,
    doc_set: ExtractedDocumentSet,
    evaluation_date: Optional[date] = None,
) -> ComplianceFinding:
    """Evaluate whether a required endorsement is supported by extracted document evidence."""
    matching_endorsements = [
        e for e in doc_set.endorsements if e.endorsement_type == req.required_endorsement_type
    ]

    # Case 1: No endorsement mentioned at all
    if not matching_endorsements:
        # If the document set itself is incomplete, report uncertainty rather than definitive absence
        if not doc_set.is_document_set_complete:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.UNABLE_TO_DETERMINE,
                severity=req.severity,
                reason_code="INCOMPLETE_DOCUMENT_SET_ENDORSEMENT_UNKNOWN",
                explanation=(
                    f"Required {req.required_endorsement_type.value} endorsement was not found, "
                    "but the submitted document set is flagged as incomplete. Endorsement pages may be missing."
                ),
                requires_human_review=True,
                required_value={"required_endorsement": req.required_endorsement_type.value},
                found_value=None,
                evidence=[],
            )

        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.MISSING_EVIDENCE,
            severity=req.severity,
            reason_code="ENDORSEMENT_NOT_FOUND",
            explanation=f"Required {req.required_endorsement_type.value} endorsement was not found in uploaded documents.",
            requires_human_review=True,
            required_value={"required_endorsement": req.required_endorsement_type.value},
            found_value=None,
            evidence=[],
        )

    # Inspect matching endorsements
    # Case 2: Only a COI checkbox exists without separate endorsement page
    checkbox_only = all(e.is_coi_checkbox_only for e in matching_endorsements)
    if checkbox_only and req.require_separate_endorsement_document:
        evidences = [e.evidence for e in matching_endorsements if e.evidence]
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=req.severity,
            reason_code="COI_CHECKBOX_WITHOUT_ENDORSEMENT_DOC",
            explanation=(
                f"A checkbox for {req.required_endorsement_type.value} is marked on the ACORD 25 COI, "
                "but no supporting endorsement form or policy wording document was submitted. "
                "COI checkboxes alone do not grant coverage; separate endorsement document is required."
            ),
            requires_human_review=True,
            required_value={"require_separate_endorsement_document": True},
            found_value={"coi_checkbox_checked": True, "endorsement_form_present": False},
            evidence=evidences,
        )

    # Case 3: Endorsement document exists
    valid_endorsements = [e for e in matching_endorsements if not e.is_coi_checkbox_only]
    if not valid_endorsements:
        valid_endorsements = matching_endorsements

    evidence_list = [e.evidence for e in valid_endorsements if e.evidence]

    # Core endorsement presence rules always require verifiable evidence before passing
    if not evidence_list:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.MISSING_EVIDENCE,
            severity=req.severity,
            reason_code="ENDORSEMENT_EVIDENCE_TEXT_MISSING",
            explanation=(
                f"{req.required_endorsement_type.value} endorsement record exists but has no "
                "supporting text evidence from the document."
            ),
            requires_human_review=True,
            required_value={"mandatory_evidence": True},
            found_value={"form_numbers": [e.form_number for e in valid_endorsements if e.form_number]},
            evidence=[],
        )

    # 1. Multi-endorsement check: Unverified semantic support in ANY matching endorsement
    unverified_ends = [
        e for e in valid_endorsements if e.extraction_status == ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT
    ]
    if unverified_ends:
        primary = unverified_ends[0]
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=req.severity,
            reason_code="ENDORSEMENT_EVIDENCE_SUPPORT_UNVERIFIED",
            explanation=(
                f"{req.required_endorsement_type.value} endorsement form "
                f"({primary.form_number or 'unspecified'}) has cited text on the page, "
                "but specific semantic support for this endorsement is unverified. Human review required."
            ),
            requires_human_review=True,
            required_value={"required_endorsement": req.required_endorsement_type.value},
            found_value={"form_numbers": [e.form_number for e in valid_endorsements if e.form_number]},
            evidence=evidence_list,
        )

    # 2. Multi-endorsement check: PARTIAL extraction status in ANY matching endorsement
    partial_ends = [
        e for e in valid_endorsements if e.extraction_status == ExtractionStatus.PARTIAL
    ]
    if partial_ends:
        primary = partial_ends[0]
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=req.severity,
            reason_code="ENDORSEMENT_PARTIAL_EXTRACTION",
            explanation=(
                f"Extracted {req.required_endorsement_type.value} endorsement form "
                f"({primary.form_number or 'unspecified'}) has PARTIAL extraction status. "
                "Incomplete endorsement details require human review."
            ),
            requires_human_review=True,
            required_value={"required_endorsement": req.required_endorsement_type.value},
            found_value={"form_numbers": [e.form_number for e in valid_endorsements if e.form_number]},
            evidence=evidence_list,
        )

    # 3. Multi-endorsement check: UNCERTAIN extraction status or low confidence in ANY matching endorsement
    uncertain_ends = [
        e for e in valid_endorsements
        if e.extraction_status == ExtractionStatus.UNCERTAIN
        or (e.confidence is not None and e.confidence < CONFIDENCE_THRESHOLD)
    ]
    if uncertain_ends:
        primary = uncertain_ends[0]
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.UNABLE_TO_DETERMINE,
            severity=req.severity,
            reason_code="ENDORSEMENT_CONFIDENCE_LOW",
            explanation=(
                f"Extracted {req.required_endorsement_type.value} endorsement form "
                f"({primary.form_number or 'unspecified'}) has low extraction confidence "
                f"({primary.confidence or 'uncertain'}). Human verification required."
            ),
            requires_human_review=True,
            required_value={"required_endorsement": req.required_endorsement_type.value},
            found_value={"form_numbers": [e.form_number for e in valid_endorsements if e.form_number]},
            evidence=evidence_list,
        )

    # 4. Multi-endorsement check: Blanket wording in ANY matching endorsement requires contract check
    blanket_ends = [e for e in valid_endorsements if e.is_blanket]
    if blanket_ends:
        primary = blanket_ends[0]
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=Severity.MAJOR,
            reason_code="BLANKET_ENDORSEMENT_REQUIRES_CONTRACT_CHECK",
            explanation=(
                f"{req.required_endorsement_type.value} endorsement form "
                f"({primary.form_number or 'blanket'}) provides automatic coverage 'where required by written contract'. "
                "Human review is required to verify an executed written contract exists."
            ),
            requires_human_review=True,
            required_value={"required_endorsement": req.required_endorsement_type.value},
            found_value={
                "form_numbers": [e.form_number for e in valid_endorsements if e.form_number],
                "is_blanket": True,
            },
            evidence=evidence_list,
        )

    # 5. Multi-endorsement check: Contradictory policies across multiple endorsements
    distinct_policies = {e.applicable_policy for e in valid_endorsements if e.applicable_policy}
    if len(distinct_policies) > 1:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=Severity.MAJOR,
            reason_code="CONTRADICTORY_ENDORSEMENTS_FOUND",
            explanation=(
                f"Multiple conflicting endorsements found for {req.required_endorsement_type.value} "
                f"linked to different policies: {list(distinct_policies)}. Human review required."
            ),
            requires_human_review=True,
            required_value={"required_endorsement": req.required_endorsement_type.value},
            found_value={"conflicting_policies": list(distinct_policies)},
            evidence=evidence_list,
        )

    # 5b. Multi-endorsement check: Conflicting entity schedules across multiple endorsements
    if len(valid_endorsements) > 1:
        schedules = [
            tuple(sorted(e.named_entities))
            for e in valid_endorsements
            if e.named_entities and not e.is_blanket
        ]
        if len(set(schedules)) > 1:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                severity=Severity.MAJOR,
                reason_code="CONTRADICTORY_ENDORSEMENTS_FOUND",
                explanation=(
                    f"Multiple endorsements for {req.required_endorsement_type.value} have conflicting schedules: "
                    f"{list(set(schedules))}. Human review required."
                ),
                requires_human_review=True,
                required_value={"required_endorsement": req.required_endorsement_type.value},
                found_value={"conflicting_schedules": [list(s) for s in set(schedules)]},
                evidence=evidence_list,
            )

    # 6. Multi-endorsement check: Detected manuscript restriction or custom exclusion wording
    manuscript_ends = [
        e for e in valid_endorsements
        if e.manuscript_clause_detected is True or e.manuscript_status == "DETECTED"
    ]
    if manuscript_ends:
        primary = manuscript_ends[0]
        code = primary.restriction_reason_code or "MANUSCRIPT_RESTRICTION_DETECTED"
        desc = primary.restriction_description or "non-standard manuscript limitation"
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=Severity.MAJOR,
            reason_code=code,
            explanation=(
                f"{req.required_endorsement_type.value} endorsement form "
                f"({primary.form_number or 'manuscript'}) contains restrictive wording or custom exclusions: {desc}. "
                "Human review required to verify whether coverage is legally and contractually sufficient."
            ),
            requires_human_review=True,
            required_value={"required_endorsement": req.required_endorsement_type.value},
            found_value={
                "form_numbers": [e.form_number for e in valid_endorsements if e.form_number],
                "restriction_description": desc,
            },
            evidence=evidence_list,
        )

    # 7. Multi-endorsement check: Non-standard / proprietary endorsement forms without explicit approval
    non_standard_ends = [e for e in valid_endorsements if e.is_standard_form is False]
    if non_standard_ends:
        primary = non_standard_ends[0]
        code = primary.restriction_reason_code or "NON_STANDARD_ENDORSEMENT_FORM"
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=Severity.MAJOR,
            reason_code=code,
            explanation=(
                f"Extracted {req.required_endorsement_type.value} endorsement form "
                f"({primary.form_number or 'non-standard'}) is a non-standard or proprietary form without pre-approval. "
                "Human review required to verify endorsement terms."
            ),
            requires_human_review=True,
            required_value={"required_endorsement": req.required_endorsement_type.value},
            found_value={"form_numbers": [e.form_number for e in valid_endorsements if e.form_number]},
            evidence=evidence_list,
        )

    # 8. Multi-endorsement check: Uncertain or unknown form / manuscript classification
    # Unknown extraction values cannot silently evaluate to COMPLIANT
    uncertain_form_ends = [
        e for e in valid_endorsements
        if e.is_standard_form is not True
        or e.manuscript_clause_detected is not False
        or e.manuscript_status != "NOT_DETECTED"
    ]
    if uncertain_form_ends:
        primary = uncertain_form_ends[0]
        code = primary.restriction_reason_code or "ENDORSEMENT_FORM_UNCERTAIN"
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=Severity.MAJOR,
            reason_code=code,
            explanation=(
                f"{req.required_endorsement_type.value} endorsement form "
                f"({primary.form_number or 'unspecified'}) has uncertain standard form or manuscript classification. "
                "Human review required."
            ),
            requires_human_review=True,
            required_value={"required_endorsement": req.required_endorsement_type.value},
            found_value={"form_numbers": [e.form_number for e in valid_endorsements if e.form_number]},
            evidence=evidence_list,
        )

    # 8b. Multi-endorsement check: Negative manuscript classification ('NOT_DETECTED') requires independent verification
    # Provider assertions (e.g. 'unmodified_standard_form' or 'full_text_inspection') cannot independently prove absence.
    # In the absence of an independent document-integrity verification mechanism, conservative fallback is REQUIRES_HUMAN_REVIEW.
    unsupported_negative_ends = [
        e for e in valid_endorsements
        if e.manuscript_status == "NOT_DETECTED"
        and not (
            getattr(e, "independent_integrity_verified", False) is True
            and is_recognized_standard_form(e.form_number)
        )
    ]
    if unsupported_negative_ends:
        primary = unsupported_negative_ends[0]
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=Severity.MAJOR,
            reason_code="MANUSCRIPT_VERIFICATION_UNSUPPORTED",
            explanation=(
                f"{req.required_endorsement_type.value} endorsement form "
                f"({primary.form_number or 'unspecified'}) has a negative manuscript claim ('NOT_DETECTED') "
                "that lacks a defensible verification basis or verified standard form provenance. Human review required."
            ),
            requires_human_review=True,
            required_value={"required_endorsement": req.required_endorsement_type.value},
            found_value={
                "form_numbers": [e.form_number for e in valid_endorsements if e.form_number],
                "manuscript_verification_basis": getattr(primary, "manuscript_verification_basis", None),
            },
            evidence=evidence_list,
        )

    # 9. All endorsements verified with valid supporting evidence
    all_form_numbers = [e.form_number for e in valid_endorsements if e.form_number]
    all_named_entities = []
    for e in valid_endorsements:
        all_named_entities.extend(e.named_entities)

    # Check contract-specific required entity on endorsement with source-evidence verification
    if req.required_entity_name:
        from app.extraction.pipeline import _verify_entity_semantic_support

        matched_verified_entity = False
        matched_unverified_entity = False
        for e in valid_endorsements:
            ent_refs = [
                ref
                for ref in [*getattr(e, "entity_evidence", ()), *([e.evidence] if e.evidence else [])]
                if ref is not None and ref.source_text
            ]
            for ent in e.named_entities:
                is_match, _ = compare_entity_names(
                    extracted_name=ent,
                    required_name=req.required_entity_name,
                    require_exact=req.require_exact_name_match,
                )
                if not is_match:
                    continue
                if any(_verify_entity_semantic_support(ent, ref.source_text) for ref in ent_refs):
                    matched_verified_entity = True
                    break
                matched_unverified_entity = True
            if matched_verified_entity:
                break

        if not matched_verified_entity and matched_unverified_entity:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                severity=Severity.MAJOR,
                reason_code="ENDORSEMENT_ENTITY_EVIDENCE_UNVERIFIED",
                explanation=(
                    f"Endorsement claims entity '{req.required_entity_name}', "
                    "but cited source document evidence does not verify this entity. Human review required."
                ),
                requires_human_review=True,
                required_value={"required_entity_name": req.required_entity_name},
                found_value={"scheduled_entities": list(set(all_named_entities))},
                evidence=evidence_list,
            )

        if not matched_verified_entity:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.NON_COMPLIANT,
                severity=req.severity,
                reason_code="ENDORSEMENT_ENTITY_MISMATCH",
                explanation=(
                    f"Required entity '{req.required_entity_name}' was not found in endorsement schedule "
                    f"{list(set(all_named_entities))}."
                ),
                requires_human_review=True,
                required_value={"required_entity_name": req.required_entity_name},
                found_value={"scheduled_entities": list(set(all_named_entities))},
                evidence=evidence_list,
            )

    primary_form = all_form_numbers[0] if all_form_numbers else "Standard"

    # 10. Contractual Duration & Endorsement Date Alignment (WP3)
    if req.required_coverage_end_date is not None or req.requires_coverage_through_policy_expiration:
        if not req.is_duration_requirement_validated:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                severity=Severity.MAJOR,
                reason_code="UNVALIDATED_DURATION_REQUIREMENT",
                explanation="Contractual duration requirement has not been validated. Human review required.",
                requires_human_review=True,
                required_value={"is_duration_requirement_validated": True},
                found_value=None,
                evidence=evidence_list,
            )

        target_end_date = req.required_coverage_end_date
        underlying_policy_cov = None

        if req.requires_coverage_through_policy_expiration:
            candidate_coverages: List[CoverageExtraction] = []
            if req.coverage_type is not None:
                candidate_coverages = [
                    c for c in doc_set.coverages if c.coverage_type == req.coverage_type
                ]
            else:
                end_policies = {
                    e.applicable_policy.strip().upper()
                    for e in valid_endorsements
                    if e.applicable_policy and e.applicable_policy.strip()
                }
                if len(end_policies) == 1:
                    target_pol = next(iter(end_policies))
                    candidate_coverages = [
                        c for c in doc_set.coverages
                        if c.policy_number and c.policy_number.strip().upper() == target_pol
                    ]
                elif not end_policies:
                    candidate_coverages = list(doc_set.coverages)

            if not candidate_coverages:
                return ComplianceFinding(
                    requirement_id=req.requirement_id,
                    rule_type=req.rule_type,
                    status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                    severity=req.severity,
                    reason_code="POLICY_EXPIRATION_UNKNOWN_FOR_DURATION",
                    explanation=(
                        "Coverage through policy expiration was required, but no applicable underlying policy "
                        "was found in the document set."
                    ),
                    requires_human_review=True,
                    required_value={
                        "requires_coverage_through_policy_expiration": True,
                        "coverage_type": req.coverage_type.value if req.coverage_type else None,
                    },
                    found_value={"available_coverage_types": [c.coverage_type.value for c in doc_set.coverages]},
                    evidence=evidence_list,
                )

            if len(candidate_coverages) > 1:
                # Multiple policies (even if sharing an expiration date) cannot be assumed interchangeable
                # when coverage_type is omitted and no unique applicable policy is established.
                distinct_cov_types = sorted({c.coverage_type.value for c in candidate_coverages})
                distinct_exp_dates = sorted({
                    c.expiration_date.date.isoformat()
                    for c in candidate_coverages
                    if c.expiration_date and c.expiration_date.date
                })
                return ComplianceFinding(
                    requirement_id=req.requirement_id,
                    rule_type=req.rule_type,
                    status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                    severity=Severity.MAJOR,
                    reason_code="AMBIGUOUS_APPLICABLE_POLICY",
                    explanation=(
                        "Coverage through policy expiration was required without an unambiguous applicable policy: "
                        f"multiple candidate policies found across coverage types {distinct_cov_types} "
                        f"(expiration dates: {distinct_exp_dates}). Human review required."
                    ),
                    requires_human_review=True,
                    required_value={
                        "requires_coverage_through_policy_expiration": True,
                        "coverage_type": req.coverage_type.value if req.coverage_type else None,
                    },
                    found_value={
                        "candidate_coverage_types": distinct_cov_types,
                        "candidate_expiration_dates": distinct_exp_dates,
                    },
                    evidence=evidence_list,
                )

            underlying_policy_cov = candidate_coverages[0]
            if underlying_policy_cov.expiration_date and underlying_policy_cov.expiration_date.date:
                policy_exp = underlying_policy_cov.expiration_date.date
                target_end_date = max(target_end_date, policy_exp) if target_end_date else policy_exp
            else:
                return ComplianceFinding(
                    requirement_id=req.requirement_id,
                    rule_type=req.rule_type,
                    status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                    severity=req.severity,
                    reason_code="POLICY_EXPIRATION_UNKNOWN_FOR_DURATION",
                    explanation=(
                        f"Coverage through {underlying_policy_cov.coverage_type.value} policy expiration was required, "
                        "but the underlying policy expiration date is missing."
                    ),
                    requires_human_review=True,
                    required_value={"requires_coverage_through_policy_expiration": True},
                    found_value=None,
                    evidence=evidence_list,
                )

        dated_endorsements = [
            e for e in valid_endorsements
            if e.effective_date is not None and e.expiration_date is not None
        ]
        if not dated_endorsements:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.MISSING_EVIDENCE,
                severity=req.severity,
                reason_code="ENDORSEMENT_DATES_MISSING",
                explanation=(
                    f"Contract requires {req.required_endorsement_type.value} endorsement coverage through "
                    f"{target_end_date.isoformat() if target_end_date else 'required period'}, but the endorsement "
                    "document lacks verified effective or expiration dates. Policy dates cannot be inferred to establish endorsement duration."
                ),
                requires_human_review=True,
                required_value={"required_coverage_end_date": target_end_date.isoformat() if target_end_date else None},
                found_value={"endorsement_dates": None},
                evidence=evidence_list,
            )

        distinct_date_ranges = {
            (e.effective_date, e.expiration_date) for e in dated_endorsements
        }
        if len(distinct_date_ranges) > 1:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                severity=Severity.MAJOR,
                reason_code="CONFLICTING_ENDORSEMENT_DATES",
                explanation=(
                    f"Multiple {req.required_endorsement_type.value} endorsements have conflicting date periods: "
                    f"{[(d[0].isoformat(), d[1].isoformat()) for d in sorted(distinct_date_ranges)]}. Human review required."
                ),
                requires_human_review=True,
                required_value={"required_coverage_end_date": target_end_date.isoformat() if target_end_date else None},
                found_value={
                    "conflicting_periods": [
                        {"effective_date": d[0].isoformat(), "expiration_date": d[1].isoformat()}
                        for d in sorted(distinct_date_ranges)
                    ]
                },
                evidence=evidence_list,
            )

        primary_end = dated_endorsements[0]
        from app.compliance.reconciliation import _evidence_has_conflicting_dates, _evidence_supports_date

        end_evidence: List[EvidenceReference] = []
        if getattr(primary_end, "date_evidence", None):
            end_evidence.extend(primary_end.date_evidence)
        if primary_end.evidence:
            end_evidence.append(primary_end.evidence)

        # Only evidence references from the matching endorsement document type (and consistent document_id)
        # can establish endorsement duration; unrelated document types (e.g. COI or OTHER) are rejected.
        valid_date_evidence = [
            ref for ref in end_evidence
            if ref.document_type == primary_end.document_type
        ]
        distinct_ev_docs = {ref.document_id for ref in valid_date_evidence}
        if len(distinct_ev_docs) > 1:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                severity=req.severity,
                reason_code="ENDORSEMENT_DATE_EVIDENCE_UNSUPPORTED",
                explanation=(
                    f"{req.required_endorsement_type.value} endorsement date evidence spans multiple distinct "
                    f"documents ({sorted(distinct_ev_docs)}). Human review required."
                ),
                requires_human_review=True,
                required_value={"required_coverage_end_date": target_end_date.isoformat() if target_end_date else None},
                found_value={"evidence_document_ids": sorted(distinct_ev_docs)},
                evidence=end_evidence,
            )

        end_evidence_tuple = tuple(valid_date_evidence)
        end_doc_id = valid_date_evidence[0].document_id if valid_date_evidence else None

        eff_conflicting = _evidence_has_conflicting_dates(end_evidence_tuple, date_field="effective", doc_id=end_doc_id)
        exp_conflicting = _evidence_has_conflicting_dates(end_evidence_tuple, date_field="expiration", doc_id=end_doc_id)

        if eff_conflicting or exp_conflicting:
            conflict_field = "effective and expiration dates" if (eff_conflicting and exp_conflicting) else ("effective date" if eff_conflicting else "expiration date")
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                severity=Severity.MAJOR,
                reason_code="CONFLICTING_DATE_EVIDENCE",
                explanation=(
                    f"{req.required_endorsement_type.value} endorsement evidence contains multiple conflicting "
                    f"values for {conflict_field}. Human review required."
                ),
                requires_human_review=True,
                required_value={"required_coverage_end_date": target_end_date.isoformat() if target_end_date else None},
                found_value={
                    "effective_date": primary_end.effective_date.isoformat() if primary_end.effective_date else None,
                    "expiration_date": primary_end.expiration_date.isoformat() if primary_end.expiration_date else None,
                },
                evidence=end_evidence,
            )

        eff_supported = _evidence_supports_date(primary_end.effective_date, end_evidence_tuple, date_field="effective", doc_id=end_doc_id)
        exp_supported = _evidence_supports_date(primary_end.expiration_date, end_evidence_tuple, date_field="expiration", doc_id=end_doc_id)

        if not eff_supported or not exp_supported:
            unsupported_field = "effective and expiration dates" if (not eff_supported and not exp_supported) else ("effective date" if not eff_supported else "expiration date")
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                severity=req.severity,
                reason_code="ENDORSEMENT_DATE_EVIDENCE_UNSUPPORTED",
                explanation=(
                    f"{req.required_endorsement_type.value} endorsement {unsupported_field} is not semantically supported "
                    "by cited document evidence. Human review required."
                ),
                requires_human_review=True,
                required_value={"required_coverage_end_date": target_end_date.isoformat() if target_end_date else None},
                found_value={
                    "effective_date": primary_end.effective_date.isoformat() if primary_end.effective_date else None,
                    "expiration_date": primary_end.expiration_date.isoformat() if primary_end.expiration_date else None,
                },
                evidence=end_evidence,
            )

        if primary_end.effective_date > primary_end.expiration_date:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.NON_COMPLIANT,
                severity=Severity.CRITICAL,
                reason_code="INVALID_DATE_RANGE_INVERTED",
                explanation=f"{req.required_endorsement_type.value} effective date ({primary_end.effective_date}) is after expiration date ({primary_end.expiration_date}).",
                requires_human_review=True,
                required_value=None,
                found_value={"effective_date": primary_end.effective_date.isoformat(), "expiration_date": primary_end.expiration_date.isoformat()},
                evidence=end_evidence,
            )

        if evaluation_date is not None:
            if primary_end.effective_date > evaluation_date:
                return ComplianceFinding(
                    requirement_id=req.requirement_id,
                    rule_type=req.rule_type,
                    status=ComplianceStatus.NON_COMPLIANT,
                    severity=Severity.MAJOR,
                    reason_code="ENDORSEMENT_FUTURE_INCEPTION",
                    explanation=f"{req.required_endorsement_type.value} endorsement inception date ({primary_end.effective_date.isoformat()}) is in the future relative to evaluation date ({evaluation_date.isoformat()}).",
                    requires_human_review=True,
                    required_value={"evaluation_date": evaluation_date.isoformat()},
                    found_value={"effective_date": primary_end.effective_date.isoformat()},
                    evidence=end_evidence,
                )
            if primary_end.expiration_date < evaluation_date:
                return ComplianceFinding(
                    requirement_id=req.requirement_id,
                    rule_type=req.rule_type,
                    status=ComplianceStatus.NON_COMPLIANT,
                    severity=Severity.CRITICAL,
                    reason_code="ENDORSEMENT_EXPIRED",
                    explanation=f"{req.required_endorsement_type.value} endorsement expired on {primary_end.expiration_date.isoformat()} (evaluation date: {evaluation_date.isoformat()}).",
                    requires_human_review=True,
                    required_value={"evaluation_date": evaluation_date.isoformat()},
                    found_value={"expiration_date": primary_end.expiration_date.isoformat()},
                    evidence=end_evidence,
                )

        if target_end_date is not None and primary_end.expiration_date < target_end_date:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.NON_COMPLIANT,
                severity=req.severity,
                reason_code="ENDORSEMENT_PREMATURE_EXPIRATION",
                explanation=(
                    f"{req.required_endorsement_type.value} endorsement expires on {primary_end.expiration_date.isoformat()}, "
                    f"which precedes the contractual requirement deadline ({target_end_date.isoformat()})."
                ),
                requires_human_review=True,
                required_value={"required_coverage_end_date": target_end_date.isoformat()},
                found_value={"endorsement_expiration_date": primary_end.expiration_date.isoformat()},
                evidence=end_evidence,
            )

        if req.requires_coverage_through_policy_expiration and underlying_policy_cov and underlying_policy_cov.expiration_date:
            if primary_end.expiration_date < underlying_policy_cov.expiration_date.date:
                return ComplianceFinding(
                    requirement_id=req.requirement_id,
                    rule_type=req.rule_type,
                    status=ComplianceStatus.NON_COMPLIANT,
                    severity=req.severity,
                    reason_code="ENDORSEMENT_PREMATURE_EXPIRATION",
                    explanation=(
                        f"{req.required_endorsement_type.value} endorsement expires on {primary_end.expiration_date.isoformat()}, "
                        f"which precedes the underlying policy expiration date ({underlying_policy_cov.expiration_date.date.isoformat()})."
                    ),
                    requires_human_review=True,
                    required_value={"policy_expiration_date": underlying_policy_cov.expiration_date.date.isoformat()},
                    found_value={"endorsement_expiration_date": primary_end.expiration_date.isoformat()},
                    evidence=end_evidence,
                )

    return ComplianceFinding(
        requirement_id=req.requirement_id,
        rule_type=req.rule_type,
        status=ComplianceStatus.COMPLIANT,
        severity=Severity.INFO,
        reason_code="ENDORSEMENT_VERIFIED",
        explanation=(
            f"{req.required_endorsement_type.value} endorsement form "
            f"({primary_form}) verified with document evidence."
        ),
        requires_human_review=False,
        required_value={"required_endorsement": req.required_endorsement_type.value},
        found_value={
            "form_number": primary_form,
            "all_form_numbers": all_form_numbers,
            "named_entities": list(set(all_named_entities)),
        },
        evidence=evidence_list,
    )


def evaluate_contractual_duration(
    req: InsuranceRequirement,
    doc_set: ExtractedDocumentSet,
    evaluation_date: Optional[date] = None,
) -> ComplianceFinding:
    """Evaluate contractual duration requirement for coverage or endorsement."""
    if req.required_endorsement_type:
        return evaluate_endorsement_presence(req, doc_set, evaluation_date)
    elif req.coverage_type:
        return evaluate_policy_expiration(req, doc_set, evaluation_date or date.today())
    else:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.UNABLE_TO_DETERMINE,
            severity=req.severity,
            reason_code="INCOMPLETE_DURATION_REQUIREMENT_TERMS",
            explanation="Contractual duration requirement must specify either coverage_type or required_endorsement_type.",
            requires_human_review=True,
            required_value=None,
            found_value=None,
            evidence=[],
        )


def _propagate_reconciliation_findings(
    findings: List[ComplianceFinding],
    reconciliation_result: Any,
    requirements: List[InsuranceRequirement],
) -> Dict[str, Any]:
    """Propagate structured reconciliation findings into ComplianceFinding list without duplication."""
    from app.schemas.reconciliation import (
        CrossDocumentReconciliationResult,
        DiscrepancyCategory,
        EntityMatchClassification,
        EntityRole,
    )

    if isinstance(reconciliation_result, dict):
        rec_res = CrossDocumentReconciliationResult.model_validate(reconciliation_result)
    elif isinstance(reconciliation_result, CrossDocumentReconciliationResult):
        rec_res = reconciliation_result
    else:
        return {}

    req_rule_map = {r.requirement_id: r.rule_type for r in requirements}
    date_equiv_codes = {
        "ENDORSEMENT_PREMATURE_EXPIRATION",
        "PREMATURE_EXPIRATION_VIOLATION",
        "CONFLICTING_DATE_EVIDENCE",
        "ENDORSEMENT_DATE_EVIDENCE_UNSUPPORTED",
        "UNSUPPORTED_DATE_EVIDENCE",
        "ENDORSEMENT_DATES_MISSING",
        "MISSING_ENDORSEMENT_DATES",
        "ENDORSEMENT_NOT_FOUND",
        "MISSING_REQUIRED_ENDORSEMENT_DOCUMENT",
        "ENDORSEMENT_EXPIRED",
        "ENDORSEMENT_FUTURE_INCEPTION",
        "ENDORSEMENT_INACTIVE_ON_EVALUATION_DATE",
        "UNVALIDATED_DURATION_REQUIREMENT",
        "INCOMPLETE_DURATION_REQUIREMENT_TERMS",
        "POLICY_EXPIRED",
        "POLICY_EXPIRES_TODAY",
        "POLICY_PREMATURE_EXPIRATION",
        "POLICY_FUTURE_INCEPTION",
        "POLICY_INACTIVE_ON_EVALUATION_DATE",
    }

    existing_failed_reqs: Dict[str, ComplianceStatus] = {
        f.requirement_id: f.status
        for f in findings
        if f.status != ComplianceStatus.COMPLIANT
    }
    existing_date_failed_reqs: Set[str] = {
        f.requirement_id
        for f in findings
        if f.status != ComplianceStatus.COMPLIANT and f.reason_code in date_equiv_codes
    }
    emitted_keys: Set[Tuple[str, frozenset]] = set()
    emitted_req_reasons: Set[Tuple[str, str]] = {(f.requirement_id, f.reason_code) for f in findings}

    def _map_cat_to_status(cat: Optional[DiscrepancyCategory], reason: Optional[str]) -> ComplianceStatus:
        if cat == DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION:
            return ComplianceStatus.NON_COMPLIANT
        if cat == DiscrepancyCategory.MISSING_EVIDENCE:
            if reason in ("UNSUPPORTED_DATE_EVIDENCE", "CONFLICTING_DATE_EVIDENCE"):
                return ComplianceStatus.REQUIRES_HUMAN_REVIEW
            return ComplianceStatus.MISSING_EVIDENCE
        return ComplianceStatus.REQUIRES_HUMAN_REVIEW

    # 1. Propagate structured DocumentDiscrepancy items first
    for d in rec_res.discrepancies:
        req_id = d.applicable_requirement_id or d.discrepancy_id
        mapped_status = _map_cat_to_status(d.category, d.reason_code)
        doc_key = frozenset(doc_id for doc_id in (d.source_document_id, d.conflicting_document_id) if doc_id)
        dedup_key = (d.reason_code, doc_key)

        if (req_id, d.reason_code) in emitted_req_reasons or dedup_key in emitted_keys:
            continue
        if d.applicable_requirement_id and d.applicable_requirement_id in existing_failed_reqs:
            existing_status = existing_failed_reqs[d.applicable_requirement_id]
            # Preserve non-duplication when primary evaluator already caught the equivalent requirement issue
            if mapped_status != ComplianceStatus.NON_COMPLIANT or existing_status == ComplianceStatus.NON_COMPLIANT:
                if d.reason_code in date_equiv_codes and d.applicable_requirement_id in existing_date_failed_reqs:
                    emitted_keys.add(dedup_key)
                    continue
                if d.category == DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION and existing_status == ComplianceStatus.NON_COMPLIANT:
                    emitted_keys.add(dedup_key)
                    continue

        rule_type = req_rule_map.get(req_id)
        if rule_type is None:
            if d.applicable_entity_role is not None:
                rule_type = RequirementType.NAMED_INSURED
            elif d.reason_code in date_equiv_codes:
                rule_type = RequirementType.CONTRACTUAL_DURATION
            else:
                rule_type = RequirementType.COMPLETE_DOCUMENT_SET

        findings.append(
            ComplianceFinding(
                finding_id=f"rec_{d.discrepancy_id}",
                requirement_id=req_id,
                rule_type=rule_type,
                status=mapped_status,
                severity=d.severity,
                reason_code=d.reason_code,
                explanation=d.description,
                requires_human_review=True,
                required_value={"requirement_id": d.applicable_requirement_id} if d.applicable_requirement_id else None,
                found_value={
                    "discrepancy_category": d.category.value,
                    "source_document_id": d.source_document_id,
                    "conflicting_document_id": d.conflicting_document_id,
                },
                evidence=list(d.evidence_chain),
            )
        )
        emitted_keys.add(dedup_key)
        emitted_req_reasons.add((req_id, d.reason_code))

    # 2. Propagate any uncovered DateReconciliationRecord review states
    for idx, drec in enumerate(rec_res.date_records):
        if not drec.requires_human_review:
            continue
        reason = drec.review_reason_code or "DATE_RECONCILIATION_REVIEW_REQUIRED"
        doc_key = frozenset(doc_id for doc_id in (drec.policy_document_id, drec.endorsement_document_id) if doc_id)
        if (reason, doc_key) in emitted_keys:
            continue
        # Check if any existing finding already captures an equivalent date failure for these docs
        if any(
            (r_code in date_equiv_codes and drec.discrepancy_category != DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION)
            or (r_code == reason)
            for (_, r_code) in emitted_req_reasons
            if any(
                f.status != ComplianceStatus.COMPLIANT and f.reason_code == r_code and (
                    not doc_key or any(ev.document_id in doc_key for ev in f.evidence)
                )
                for f in findings
            )
        ):
            continue
        if drec.discrepancy_category == DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION and any(
            f.status == ComplianceStatus.NON_COMPLIANT and f.reason_code in date_equiv_codes
            and any(ev.document_id in doc_key for ev in f.evidence)
            for f in findings
        ):
            continue

        mapped_status = _map_cat_to_status(drec.discrepancy_category, reason)
        ev_list = list(drec.evidence_chain or drec.endorsement_date_evidence or drec.policy_date_evidence)
        findings.append(
            ComplianceFinding(
                finding_id=f"rec_date_{idx}_{reason}",
                requirement_id=f"RECON_DATE_{idx}_{reason}",
                rule_type=RequirementType.CONTRACTUAL_DURATION,
                status=mapped_status,
                severity=Severity.CRITICAL if mapped_status == ComplianceStatus.NON_COMPLIANT else Severity.MAJOR,
                reason_code=reason,
                explanation=(
                    f"Temporal date reconciliation identified '{reason}' across documents "
                    f"{sorted(doc_key) if doc_key else ['packet']}."
                ),
                requires_human_review=True,
                found_value={
                    "discrepancy_category": drec.discrepancy_category.value if drec.discrepancy_category else None,
                    "policy_document_id": drec.policy_document_id,
                    "endorsement_document_id": drec.endorsement_document_id,
                },
                evidence=ev_list,
            )
        )
        emitted_keys.add((reason, doc_key))
        emitted_req_reasons.add((f"RECON_DATE_{idx}_{reason}", reason))

    # 3. Propagate any uncovered EntityReconciliationRecord conflicts/ambiguities
    for idx, erec in enumerate(rec_res.entity_records):
        if not erec.requires_human_review and erec.classification != EntityMatchClassification.CONFIRMED_MISMATCH:
            continue
        reason = erec.review_reason_code or (
            "CONFIRMED_ENTITY_MISMATCH"
            if erec.classification == EntityMatchClassification.CONFIRMED_MISMATCH
            else "AMBIGUOUS_ENTITY_IDENTITY"
        )
        doc_key = frozenset(doc_id for doc_id in (erec.primary_document_id, erec.referenced_document_id) if doc_id)
        if (reason, doc_key) in emitted_keys:
            continue
        mapped_status = (
            ComplianceStatus.NON_COMPLIANT
            if erec.classification == EntityMatchClassification.CONFIRMED_MISMATCH
            else (
                ComplianceStatus.MISSING_EVIDENCE
                if erec.classification == EntityMatchClassification.NO_EVIDENCE
                else ComplianceStatus.REQUIRES_HUMAN_REVIEW
            )
        )
        req_id = erec.applicable_contract_requirement_id or f"RECON_ENTITY_{idx}_{reason}"
        ev_list = list(erec.evidence_chain)
        for ev in (erec.primary_role_evidence, erec.referenced_role_evidence, erec.dba_evidence):
            if ev is not None and ev not in ev_list:
                ev_list.append(ev)
        findings.append(
            ComplianceFinding(
                finding_id=f"rec_entity_{idx}_{reason}",
                requirement_id=req_id,
                rule_type=RequirementType.NAMED_INSURED,
                status=mapped_status,
                severity=Severity.CRITICAL if mapped_status == ComplianceStatus.NON_COMPLIANT else Severity.MAJOR,
                reason_code=reason,
                explanation=(
                    f"Entity identity reconciliation between '{erec.primary_entity_name}' ({erec.primary_document_id}) "
                    f"and '{erec.referenced_entity_name}' ({erec.referenced_document_id}) yielded {erec.classification.value}."
                ),
                requires_human_review=True,
                found_value={
                    "classification": erec.classification.value,
                    "primary_document_id": erec.primary_document_id,
                    "referenced_document_id": erec.referenced_document_id,
                },
                evidence=ev_list,
            )
        )
        emitted_keys.add((reason, doc_key))

    # 4. Propagate any uncovered PolicyReconciliationRecord conflicts/ambiguities
    for idx, prec in enumerate(rec_res.policy_records):
        if not prec.requires_human_review:
            continue
        reason = prec.review_reason_code or "CONFLICTING_POLICY_IDENTIFIERS"
        doc_key = frozenset(doc_id for doc_id in (prec.coi_document_id, prec.endorsement_document_id) if doc_id)
        if (reason, doc_key) in emitted_keys:
            continue
        mapped_status = (
            ComplianceStatus.MISSING_EVIDENCE
            if reason == "POLICY_NUMBER_MISSING_EVIDENCE"
            else ComplianceStatus.REQUIRES_HUMAN_REVIEW
        )
        ev_list = list(prec.evidence_chain)
        for ev in (prec.coi_evidence, prec.endorsement_evidence):
            if ev is not None and ev not in ev_list:
                ev_list.append(ev)
        findings.append(
            ComplianceFinding(
                finding_id=f"rec_pol_{idx}_{reason}",
                requirement_id=f"RECON_POLICY_{idx}_{reason}",
                rule_type=RequirementType.COMPLETE_DOCUMENT_SET,
                status=mapped_status,
                severity=Severity.MAJOR,
                reason_code=reason,
                explanation=(
                    f"Policy number reconciliation between '{prec.coi_policy_number}' ({prec.coi_document_id}) "
                    f"and '{prec.endorsement_policy_number}' ({prec.endorsement_document_id}) requires review."
                ),
                requires_human_review=True,
                found_value={
                    "classification": prec.classification.value,
                    "coi_document_id": prec.coi_document_id,
                    "endorsement_document_id": prec.endorsement_document_id,
                },
                evidence=ev_list,
            )
        )
        emitted_keys.add((reason, doc_key))

    # 5. Propagate any uncovered DocumentRelationshipEdge ambiguities/conflicts/orphans
    for edge in rec_res.graph_edges:
        if not edge.is_compliance_relevant or not edge.requires_human_review:
            continue
        reason = edge.review_reason_code or "AMBIGUOUS_DOCUMENT_RELATIONSHIP"
        doc_key = frozenset(doc_id for doc_id in (edge.source_document_id, edge.target_document_id) if doc_id)
        if (reason, doc_key) in emitted_keys:
            continue
        findings.append(
            ComplianceFinding(
                finding_id=f"rec_edge_{edge.edge_id}",
                requirement_id=f"RECON_REL_{edge.edge_id}",
                rule_type=RequirementType.COMPLETE_DOCUMENT_SET,
                status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                severity=Severity.MAJOR,
                reason_code=reason,
                explanation=(
                    f"Document relationship '{edge.relationship_type.value}' from '{edge.source_document_id}' "
                    f"to '{edge.target_document_id or 'MISSING'}' requires human review ({reason})."
                ),
                requires_human_review=True,
                found_value={
                    "classification": edge.classification.value,
                    "relationship_type": edge.relationship_type.value,
                    "source_document_id": edge.source_document_id,
                    "target_document_id": edge.target_document_id,
                },
                evidence=list(edge.evidence_references),
            )
        )
        emitted_keys.add((reason, doc_key))

    # 6. Propagate MissingEvidenceProvenance assertions
    for idx, miss in enumerate(rec_res.missing_evidence_records):
        findings.append(
            ComplianceFinding(
                finding_id=f"rec_miss_{idx}_{miss.assertion_document_id}",
                requirement_id=f"RECON_MISSING_{idx}_{miss.assertion_document_id}",
                rule_type=RequirementType.COMPLETE_DOCUMENT_SET,
                status=ComplianceStatus.MISSING_EVIDENCE,
                severity=Severity.MAJOR,
                reason_code="MISSING_REFERENCED_DOCUMENT_EVIDENCE",
                explanation=f"Document '{miss.assertion_document_id}' asserts missing supporting document evidence.",
                requires_human_review=True,
                found_value={
                    "assertion_document_id": miss.assertion_document_id,
                    "assertion_page_number": miss.assertion_page_number,
                },
                evidence=[miss.assertion_evidence],
            )
        )

    reason_codes = sorted({
        *(d.reason_code for d in rec_res.discrepancies),
        *(r.review_reason_code for r in rec_res.date_records if r.requires_human_review and r.review_reason_code),
        *(
            (r.review_reason_code or ("CONFIRMED_ENTITY_MISMATCH" if r.classification == EntityMatchClassification.CONFIRMED_MISMATCH else "AMBIGUOUS_ENTITY_IDENTITY"))
            for r in rec_res.entity_records
            if r.requires_human_review or r.classification == EntityMatchClassification.CONFIRMED_MISMATCH
        ),
        *(r.review_reason_code for r in rec_res.policy_records if r.requires_human_review and r.review_reason_code),
        *(e.review_reason_code for e in rec_res.graph_edges if e.is_compliance_relevant and e.requires_human_review and e.review_reason_code),
    })

    return {
        "document_packet_id": rec_res.document_packet_id,
        "graph_evaluation_status": rec_res.graph_evaluation_status.value,
        "has_unresolved_contradictions": rec_res.has_unresolved_contradictions,
        "has_missing_evidence": rec_res.has_missing_evidence,
        "all_relationships_confirmed": rec_res.all_relationships_confirmed,
        "date_records_count": len(rec_res.date_records),
        "entity_records_count": len(rec_res.entity_records),
        "policy_records_count": len(rec_res.policy_records),
        "graph_edges_count": len(rec_res.graph_edges),
        "discrepancies_count": len(rec_res.discrepancies),
        "reason_codes": reason_codes,
    }


# ============================================================================
# Main Evaluator Entry Point
# ============================================================================

class ComplianceEvaluator:
    """Pure, deterministic evaluator engine for insurance document sets."""

    def evaluate(
        self,
        document_set: ExtractedDocumentSet,
        requirements: List[InsuranceRequirement],
        evaluation_date: date,
    ) -> ComplianceReviewResult:
        """Evaluate a subcontractor's document set against configured requirements deterministically.
        
        Args:
            document_set: Validated extracted insurance data
            requirements: List of configured insurance requirements
            evaluation_date: Explicit date used as "today" for policy date checks
            
        Returns:
            ComplianceReviewResult containing individual findings and overall status
        """
        if not requirements:
            raise ValueError("Requirement profile must contain at least one requirement.")

        findings: List[ComplianceFinding] = []

        # If document set contains known raw contradictions, note that in evaluation
        contradictions = document_set.raw_contradictions_detected

        for req in requirements:
            if req.rule_type == RequirementType.COVERAGE_LIMIT:
                finding = evaluate_coverage_limit(req, document_set)
            elif req.rule_type == RequirementType.POLICY_EXPIRATION:
                finding = evaluate_policy_expiration(req, document_set, evaluation_date)
            elif req.rule_type == RequirementType.POLICY_EFFECTIVE_DATE:
                finding = evaluate_policy_effective_date(req, document_set, evaluation_date)
            elif req.rule_type == RequirementType.NAMED_INSURED:
                finding = evaluate_named_insured(req, document_set)
            elif req.rule_type == RequirementType.ENDORSEMENT_PRESENCE:
                finding = evaluate_endorsement_presence(req, document_set, evaluation_date)
            elif req.rule_type == RequirementType.CONTRACTUAL_DURATION:
                finding = evaluate_contractual_duration(req, document_set, evaluation_date)
            else:
                finding = ComplianceFinding(
                    requirement_id=req.requirement_id,
                    rule_type=req.rule_type,
                    status=ComplianceStatus.UNABLE_TO_DETERMINE,
                    severity=req.severity,
                    reason_code="UNSUPPORTED_RULE_TYPE",
                    explanation=f"Unsupported requirement type '{req.rule_type}'.",
                    requires_human_review=True,
                    required_value=None,
                    found_value=None,
                    evidence=[],
                )
            findings.append(finding)

        # Defensive cross-document reconciliation:
        # Detect material inconsistencies across documents if not already performed
        try:
            from app.extraction.pipeline import reconcile_cross_document_set
            reconcile_cross_document_set(document_set)
        except Exception:
            pass

        # Structured cross-document reconciliation integration (WP4):
        # Reuse pre-computed CrossDocumentReconciliationResult if attached (no duplicate execution),
        # otherwise invoke reconcile_extracted_document_set() once for applicable inputs.
        reconciliation_summary: Optional[Dict[str, Any]] = None
        if document_set.reconciliation_result is not None:
            reconciliation_obj = document_set.reconciliation_result
        else:
            from app.compliance.reconciliation import reconcile_extracted_document_set
            reconciliation_obj = reconcile_extracted_document_set(
                document_set=document_set,
                requirements=requirements,
                evaluation_date=evaluation_date,
            )

        if reconciliation_obj is not None:
            reconciliation_summary = _propagate_reconciliation_findings(
                findings=findings,
                reconciliation_result=reconciliation_obj,
                requirements=requirements,
            )

        overall_status, requires_review = _aggregate_overall_status(findings)

        # Defensive active-term enforcement:
        # A reliably extracted future policy effective date must never produce an overall COMPLIANT result,
        # even if the requirement profile omits POLICY_EFFECTIVE_DATE.
        has_future_effective_date = False
        for cov in document_set.coverages:
            if cov.effective_date and cov.effective_date.date > evaluation_date:
                if cov.extraction_status not in (ExtractionStatus.UNCERTAIN, ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT) and (
                    cov.effective_date.confidence is None or cov.effective_date.confidence >= CONFIDENCE_THRESHOLD
                ):
                    has_future_effective_date = True
                    break

        if has_future_effective_date and overall_status == ComplianceStatus.COMPLIANT:
            overall_status = ComplianceStatus.NON_COMPLIANT
            requires_review = True

        # If there are unresolved document contradictions, overall review cannot be compliant
        if (contradictions or document_set.raw_contradictions_detected) and overall_status == ComplianceStatus.COMPLIANT:
            overall_status = ComplianceStatus.REQUIRES_HUMAN_REVIEW
            requires_review = True

        if reconciliation_summary and overall_status == ComplianceStatus.COMPLIANT:
            if reconciliation_summary.get("has_unresolved_contradictions"):
                overall_status = ComplianceStatus.REQUIRES_HUMAN_REVIEW
                requires_review = True
            elif reconciliation_summary.get("has_missing_evidence"):
                overall_status = ComplianceStatus.MISSING_EVIDENCE
                requires_review = True

        return ComplianceReviewResult(
            subcontractor_id=document_set.subcontractor_id,
            evaluation_date=evaluation_date,
            overall_status=overall_status,
            requires_human_review=requires_review,
            findings=findings,
            reconciliation_summary=reconciliation_summary,
        )
