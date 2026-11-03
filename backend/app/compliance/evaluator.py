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
from typing import List, Optional, Tuple

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

    if core_ext and core_ext == core_req:
        if suffix_ext == suffix_req and suffix_ext is not None:
            return True, "FORMAT_NORMALIZED_MATCH"
        if suffix_ext != suffix_req:
            # Different corporate suffixes (e.g. Inc vs LLC) or one missing suffix
            if suffix_ext is not None and suffix_req is not None:
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


# ============================================================================
# Rule Evaluators
# ============================================================================

def evaluate_coverage_limit(
    req: InsuranceRequirement,
    doc_set: ExtractedDocumentSet,
) -> ComplianceFinding:
    """Evaluate whether an extracted coverage limit meets or exceeds the required minimum."""
    matching_coverages = [c for c in doc_set.coverages if c.coverage_type == req.coverage_type]

    if not matching_coverages:
        if not doc_set.is_document_set_complete:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.UNABLE_TO_DETERMINE,
                severity=req.severity,
                reason_code="INCOMPLETE_DOCUMENT_SET_COVERAGE_UNKNOWN",
                explanation=(
                    f"Required {req.coverage_type.value} coverage was not found, "
                    "but the submitted document set is flagged as incomplete. "
                    "Coverage pages or declaration sheets may be missing."
                ),
                requires_human_review=True,
                required_value={"min_amount_cents": req.min_amount_cents, "limit_field": req.limit_field.value},
                found_value=None,
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
            required_value={"min_amount_cents": req.min_amount_cents, "limit_field": req.limit_field.value},
            found_value=None,
            evidence=[],
        )

    # Check for contradictions across multiple policies of the same coverage type
    extracted_limits = []
    evidences = []
    for cov in matching_coverages:
        limit_val = getattr(cov, req.limit_field.value, None)
        if limit_val is not None:
            extracted_limits.append((cov, limit_val))
            if cov.evidence:
                evidences.append(cov.evidence)

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
            evidence=evidences,
        )

    cov, limit_obj = extracted_limits[0]

    # Check extraction status: PARTIAL
    if cov.extraction_status == ExtractionStatus.PARTIAL:
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
            found_value={"amount_cents": limit_obj.amount_cents, "raw_text": limit_obj.raw_text},
            evidence=evidences,
        )

    # Check extraction confidence / status
    if cov.extraction_status == ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT:
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
            severity=req.severity,
            reason_code="EVIDENCE_SUPPORT_UNVERIFIED",
            explanation=(
                f"Extracted {req.coverage_type.value} {req.limit_field.value} limit of "
                f"${limit_obj.amount_cents/100:,.2f} has cited document text whose semantic support "
                "for this limit value is unverified. Human review required."
            ),
            requires_human_review=True,
            required_value={"min_amount_cents": req.min_amount_cents},
            found_value={"amount_cents": limit_obj.amount_cents, "raw_text": limit_obj.raw_text},
            evidence=[cov.evidence] if cov.evidence else [],
        )

    if cov.extraction_status == ExtractionStatus.UNCERTAIN or (
        limit_obj.confidence is not None and limit_obj.confidence < CONFIDENCE_THRESHOLD
    ):
        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.UNABLE_TO_DETERMINE,
            severity=req.severity,
            reason_code="EXTRACTION_CONFIDENCE_LOW",
            explanation=(
                f"Extracted {req.coverage_type.value} {req.limit_field.value} limit of "
                f"${limit_obj.amount_cents/100:,.2f} has low extraction confidence "
                f"({limit_obj.confidence or 'uncertain'}). Human verification required."
            ),
            requires_human_review=True,
            required_value={"min_amount_cents": req.min_amount_cents},
            found_value={"amount_cents": limit_obj.amount_cents, "raw_text": limit_obj.raw_text},
            evidence=evidences,
        )

    # If mandatory_evidence is True, require supporting evidence before passing
    if req.mandatory_evidence and not evidences:
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


def evaluate_policy_expiration(
    req: InsuranceRequirement,
    doc_set: ExtractedDocumentSet,
    evaluation_date: date,
) -> ComplianceFinding:
    """Evaluate whether the policy has expired relative to evaluation_date."""
    matching_coverages = [c for c in doc_set.coverages if c.coverage_type == req.coverage_type]

    if not matching_coverages:
        if not doc_set.is_document_set_complete:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.UNABLE_TO_DETERMINE,
                severity=req.severity,
                reason_code="INCOMPLETE_DOCUMENT_SET_COVERAGE_UNKNOWN",
                explanation=f"Cannot verify expiration: no {req.coverage_type.value} coverage found and document set is incomplete.",
                requires_human_review=True,
                required_value={"evaluation_date": evaluation_date.isoformat()},
                found_value=None,
                evidence=[],
            )

        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.MISSING_EVIDENCE,
            severity=req.severity,
            reason_code="COVERAGE_NOT_FOUND",
            explanation=f"Cannot verify expiration: no {req.coverage_type.value} coverage found.",
            requires_human_review=True,
            required_value={"evaluation_date": evaluation_date.isoformat()},
            found_value=None,
            evidence=[],
        )

    cov = matching_coverages[0]
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
    evidence_list = [cov.evidence] if cov.evidence else []

    # Check extraction status: PARTIAL
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

    # Check extraction confidence / status
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

    # Check if mandatory evidence is required and present
    if req.mandatory_evidence and not evidence_list:
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

    # Date comparison:
    # If expiration_date < evaluation_date -> Expired (NON_COMPLIANT)
    # If expiration_date == evaluation_date -> Expiring today (NON_COMPLIANT / Requires renewal)
    # If expiration_date > evaluation_date -> Active (COMPLIANT)
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
    elif exp_date_obj.date == evaluation_date:
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
    else:
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


def evaluate_policy_effective_date(
    req: InsuranceRequirement,
    doc_set: ExtractedDocumentSet,
    evaluation_date: date,
) -> ComplianceFinding:
    """Evaluate whether the policy effective date is valid (not post-dated to the future)."""
    matching_coverages = [c for c in doc_set.coverages if c.coverage_type == req.coverage_type]

    if not matching_coverages:
        if not doc_set.is_document_set_complete:
            return ComplianceFinding(
                requirement_id=req.requirement_id,
                rule_type=req.rule_type,
                status=ComplianceStatus.UNABLE_TO_DETERMINE,
                severity=req.severity,
                reason_code="INCOMPLETE_DOCUMENT_SET_COVERAGE_UNKNOWN",
                explanation=f"Cannot verify effective date: no {req.coverage_type.value} coverage found and document set is incomplete.",
                requires_human_review=True,
                required_value={"evaluation_date": evaluation_date.isoformat()},
                found_value=None,
                evidence=[],
            )

        return ComplianceFinding(
            requirement_id=req.requirement_id,
            rule_type=req.rule_type,
            status=ComplianceStatus.MISSING_EVIDENCE,
            severity=req.severity,
            reason_code="COVERAGE_NOT_FOUND",
            explanation=f"Cannot verify effective date: no {req.coverage_type.value} coverage found.",
            requires_human_review=True,
            required_value={"evaluation_date": evaluation_date.isoformat()},
            found_value=None,
            evidence=[],
        )

    cov = matching_coverages[0]
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
    evidence_list = [cov.evidence] if cov.evidence else []

    # Check extraction status: PARTIAL
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

    # Check extraction confidence / status
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

    # Check if mandatory evidence is required and present
    if req.mandatory_evidence and not evidence_list:
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

    # Inverted date range check (effective date after expiration date)
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

    # Check future effective date
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

    is_match, reason = compare_entity_names(
        extracted_name=ni.name,
        required_name=req.required_entity_name,
        require_exact=req.require_exact_name_match,
    )

    # Check mandatory evidence before marking compliant
    if is_match and req.mandatory_evidence and not evidence_list:
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


def evaluate_endorsement_presence(
    req: InsuranceRequirement,
    doc_set: ExtractedDocumentSet,
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

    # Mandatory evidence check across all valid endorsements
    if req.mandatory_evidence and not evidence_list:
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

    # 6. All endorsements verified with valid supporting evidence
    all_form_numbers = [e.form_number for e in valid_endorsements if e.form_number]
    all_named_entities = []
    for e in valid_endorsements:
        all_named_entities.extend(e.named_entities)

    primary_form = all_form_numbers[0] if all_form_numbers else "Standard"

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
                finding = evaluate_endorsement_presence(req, document_set)
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

        overall_status, requires_review = _aggregate_overall_status(findings)

        # If there are unresolved document contradictions, overall review cannot be compliant
        if contradictions and overall_status == ComplianceStatus.COMPLIANT:
            overall_status = ComplianceStatus.REQUIRES_HUMAN_REVIEW
            requires_review = True

        return ComplianceReviewResult(
            subcontractor_id=document_set.subcontractor_id,
            evaluation_date=evaluation_date,
            overall_status=overall_status,
            requires_human_review=requires_review,
            findings=findings,
        )
