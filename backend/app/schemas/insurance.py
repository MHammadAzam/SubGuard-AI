"""Core Pydantic data contracts for SubGuard AI.

These models define:
1. Extracted insurance data (from COIs, endorsements, declaration pages)
2. Configurable insurance requirements
3. Deterministic compliance evaluation findings and summaries

All models adhere to the core philosophy:
'AI extracts information. Deterministic Python rules evaluate it. Humans resolve uncertainty.'
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ============================================================================
# Enums
# ============================================================================

class DocumentType(str, Enum):
    """Recognized document categories."""
    COI = "coi"                          # Certificate of Insurance (e.g. ACORD 25)
    ENDORSEMENT = "endorsement"          # Endorsement document (e.g. CG 20 10)
    POLICY_DEC_PAGE = "policy_dec_page"  # Policy declaration page
    OTHER = "other"
    UNKNOWN = "unknown"


class CoverageType(str, Enum):
    """Standard insurance coverage types."""
    GENERAL_LIABILITY = "general_liability"
    AUTO_LIABILITY = "auto_liability"
    WORKERS_COMP = "workers_comp"
    UMBRELLA_EXCESS = "umbrella_excess"
    PROFESSIONAL_LIABILITY = "professional_liability"
    OTHER = "other"


class EndorsementType(str, Enum):
    """Recognized endorsement types."""
    ADDITIONAL_INSURED = "additional_insured"
    WAIVER_OF_SUBROGATION = "waiver_of_subrogation"
    PRIMARY_NONCONTRIBUTORY = "primary_noncontributory"
    OTHER = "other"


class ExtractionStatus(str, Enum):
    """Reliability state of an extracted field."""
    EXTRACTED = "extracted"     # Value was clearly found, parsed, and semantically supported
    NOT_FOUND = "not_found"     # Field was inspected but absent / blank
    UNCERTAIN = "uncertain"     # Text exists but parse is ambiguous / low-confidence
    QUOTE_PRESENT_UNVERIFIED_SUPPORT = "quote_present_unverified_support" # Quote verified on page text layer, but specific semantic support for the extracted field value is unverified
    PARTIAL = "partial"         # Only part of expected composite data found
    ERROR = "error"             # Extraction failed due to OCR / provider error


class ComplianceStatus(str, Enum):
    """Deterministic evaluation outcome for a rule or aggregate review."""
    COMPLIANT = "compliant"
    NON_COMPLIANT = "non_compliant"
    MISSING_EVIDENCE = "missing_evidence"
    UNABLE_TO_DETERMINE = "unable_to_determine"
    REQUIRES_HUMAN_REVIEW = "requires_human_review"


class Severity(str, Enum):
    """Severity classification for compliance findings."""
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


class RequirementType(str, Enum):
    """Categories of insurance rules supported by the evaluator."""
    COVERAGE_LIMIT = "coverage_limit"
    POLICY_EXPIRATION = "policy_expiration"
    POLICY_EFFECTIVE_DATE = "policy_effective_date"
    NAMED_INSURED = "named_insured"
    ENDORSEMENT_PRESENCE = "endorsement_presence"
    COMPLETE_DOCUMENT_SET = "complete_document_set"
    CONTRACTUAL_DURATION = "contractual_duration"


class LimitField(str, Enum):
    """Specific coverage sub-limits."""
    EACH_OCCURRENCE = "each_occurrence"
    GENERAL_AGGREGATE = "general_aggregate"
    PRODUCTS_COMPLETED_OPS = "products_completed_ops"
    PERSONAL_ADV_INJURY = "personal_adv_injury"
    COMBINED_SINGLE_LIMIT = "combined_single_limit"
    PER_ACCIDENT = "per_accident"
    DISEASE_EACH_EMPLOYEE = "disease_each_employee"
    DISEASE_POLICY_LIMIT = "disease_policy_limit"


# ============================================================================
# Extracted Evidence & Primitives
# ============================================================================

class BoundingBox(BaseModel):
    """Normalized coordinates [0.0, 1.0] relative to page dimensions."""
    model_config = ConfigDict(frozen=True)

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(ge=0.0, le=1.0)
    height: float = Field(ge=0.0, le=1.0)


class EvidenceReference(BaseModel):
    """Traceable pointer to the source document location."""
    model_config = ConfigDict(frozen=True)

    document_id: str
    document_type: DocumentType = DocumentType.COI
    page_number: int = Field(ge=1, description="1-indexed source page")
    source_text: str = Field(description="Exact verbatim text excerpt from document")
    bounding_box: Optional[BoundingBox] = None
    ocr_provider: Optional[str] = Field(default=None, description="OCR provider name if evidence was OCR-transcribed")
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Extraction or OCR confidence score")
    processing_status: Optional[str] = Field(default=None, description="Processing state: native_digital, ocr_extracted, etc.")


class MoneyValue(BaseModel):
    """Monetary limit represented in integer cents to prevent floating point drift."""
    amount_cents: int = Field(ge=0, description="Amount in whole cents ($1,000,000 = 100,000,000 cents)")
    currency: str = Field(default="USD", max_length=3)
    raw_text: str = Field(description="Raw string as extracted, e.g. '$1,000,000'")
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    evidence: Optional[EvidenceReference] = Field(
        default=None,
        description="Field-level traceable evidence supporting this specific monetary value",
    )


class DateValue(BaseModel):
    """Extracted date with supporting text."""
    date: date
    raw_text: str = Field(description="Raw date string, e.g. '06/01/2025'")
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    evidence: Optional[EvidenceReference] = Field(
        default=None,
        description="Field-level traceable evidence supporting this specific date",
    )


class EntityInfo(BaseModel):
    """Named party extracted from documents (Insured, Certificate Holder, Producer)."""
    name: Optional[str] = None
    address: Optional[str] = None
    evidence: Optional[EvidenceReference] = None
    extraction_status: ExtractionStatus = ExtractionStatus.EXTRACTED
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class CoverageExtraction(BaseModel):
    """Extracted limits and policy dates for a specific coverage section."""
    coverage_type: CoverageType
    policy_number: Optional[str] = None
    insurer_name: Optional[str] = None

    # Coverage limits in cents
    each_occurrence: Optional[MoneyValue] = None
    general_aggregate: Optional[MoneyValue] = None
    products_completed_ops: Optional[MoneyValue] = None
    personal_adv_injury: Optional[MoneyValue] = None
    combined_single_limit: Optional[MoneyValue] = None
    per_accident: Optional[MoneyValue] = None
    disease_each_employee: Optional[MoneyValue] = None
    disease_policy_limit: Optional[MoneyValue] = None

    # Coverage dates
    effective_date: Optional[DateValue] = None
    expiration_date: Optional[DateValue] = None

    # Tracking & evidence
    evidence: Optional[EvidenceReference] = None
    date_evidence: Tuple[EvidenceReference, ...] = Field(
        default_factory=tuple,
        description="Evidence references supporting policy effective/expiration dates",
    )
    extraction_status: ExtractionStatus = ExtractionStatus.EXTRACTED
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class EndorsementExtraction(BaseModel):
    """Extracted endorsement details from COI remarks or standalone endorsement pages."""
    endorsement_type: EndorsementType
    document_type: DocumentType = DocumentType.ENDORSEMENT
    form_number: Optional[str] = Field(default=None, description="e.g. 'CG 20 10 04 13'")
    applicable_policy: Optional[str] = None
    named_entities: List[str] = Field(default_factory=list, description="Entities explicitly named in endorsement")
    key_terms: List[str] = Field(default_factory=list, description="Important phrases extracted, e.g. 'ongoing operations'")
    is_blanket: bool = Field(default=False, description="True if endorsement specifies blanket / automatic coverage by contract")
    is_coi_checkbox_only: bool = Field(
        default=False,
        description="True if indication came ONLY from a checked box on COI without separate endorsement text/wording"
    )
    is_standard_form: Optional[bool] = Field(
        default=None,
        description="True if standard ISO/industry form, False for non-standard/manuscript, None if unverified/unknown"
    )
    manuscript_clause_detected: Optional[bool] = Field(
        default=None,
        description="True if restrictive manuscript modifications or exclusionary conditions were detected, False if verified absent, None if unverified/unknown"
    )
    manuscript_status: Optional[str] = Field(
        default=None,
        description="'DETECTED', 'NOT_DETECTED', or 'UNCERTAIN'"
    )
    manuscript_verification_basis: Optional[str] = Field(
        default=None,
        description="Provider-asserted verification basis for negative manuscript determination: e.g. 'unmodified_standard_form', 'full_text_inspection', or None if unverified"
    )
    independent_integrity_verified: bool = Field(
        default=False,
        description="True ONLY if an independent, trusted document-integrity verification mechanism has verified that the physical document is an authentic, unmodified standard form"
    )
    effective_date: Optional[date] = Field(
        default=None,
        description="Endorsement effective date if explicitly stated"
    )
    expiration_date: Optional[date] = Field(
        default=None,
        description="Endorsement expiration date if explicitly stated"
    )
    restriction_description: Optional[str] = Field(
        default=None,
        description="Description of detected manuscript restriction or exclusion"
    )
    restriction_reason_code: Optional[str] = Field(
        default=None,
        description="Specific reason code for detected limitation or restriction"
    )
    evidence: Optional[EvidenceReference] = None
    date_evidence: Tuple[EvidenceReference, ...] = Field(
        default_factory=tuple,
        description="Evidence references supporting endorsement dates"
    )
    entity_evidence: Tuple[EvidenceReference, ...] = Field(
        default_factory=tuple,
        description="Source-verified evidence references supporting extracted named entities"
    )
    extraction_status: ExtractionStatus = ExtractionStatus.EXTRACTED
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class ExtractedDocumentSet(BaseModel):
    """Full set of extracted data available for a subcontractor's evaluation."""
    subcontractor_id: str
    documents_submitted: List[DocumentType] = Field(default_factory=list)
    is_document_set_complete: bool = Field(
        default=True,
        description="False if known supporting endorsement pages were referenced on COI but missing from upload"
    )
    named_insured: Optional[EntityInfo] = None
    additional_named_insureds: List[EntityInfo] = Field(
        default_factory=list,
        description="Secondary Named Insured extractions preserved from additional packet documents for entity reconciliation",
    )
    certificate_holder: Optional[EntityInfo] = None
    coverages: List[CoverageExtraction] = Field(default_factory=list)
    endorsements: List[EndorsementExtraction] = Field(default_factory=list)
    raw_contradictions_detected: List[str] = Field(default_factory=list)
    reconciliation_result: Optional[Any] = Field(
        default=None,
        description="Immutable cross-document reconciliation result (CrossDocumentReconciliationResult)"
    )


# ============================================================================
# Insurance Requirements (Configurable Inputs)
# ============================================================================

class InsuranceRequirement(BaseModel):
    """Configurable rule against which extracted documents are evaluated."""
    requirement_id: str = Field(description="Unique stable ID, e.g. 'REQ_GL_EACH_OCC'")
    rule_type: RequirementType
    description: str = Field(description="Human-readable rule intent")
    severity: Severity = Severity.CRITICAL
    coverage_type: Optional[CoverageType] = None
    target_policy_number: Optional[str] = Field(
        default=None,
        description="Optional explicit policy number binding this requirement to a specific policy",
    )
    limit_field: Optional[LimitField] = None
    min_amount_cents: Optional[int] = Field(default=None, ge=0)
    required_endorsement_type: Optional[EndorsementType] = None
    required_entity_name: Optional[str] = None
    require_exact_name_match: bool = False
    require_separate_endorsement_document: bool = True
    mandatory_evidence: bool = True
    # Contractual duration fields (WP3)
    required_coverage_end_date: Optional[date] = Field(
        default=None,
        description="Explicit contractual deadline / end date through which coverage or endorsement must remain active",
    )
    requires_coverage_through_policy_expiration: bool = Field(
        default=False,
        description="Whether coverage or endorsement must remain active through the underlying policy expiration date",
    )
    is_duration_requirement_validated: bool = Field(
        default=True,
        description="Whether the contractual duration requirement has been legally and contractually validated",
    )

    @model_validator(mode="after")
    def validate_requirement_fields(self) -> InsuranceRequirement:
        if not self.requirement_id or not self.requirement_id.strip():
            raise ValueError("requirement_id cannot be empty")
        if self.rule_type == RequirementType.COVERAGE_LIMIT:
            if not self.coverage_type or not self.limit_field or self.min_amount_cents is None:
                raise ValueError("COVERAGE_LIMIT requires coverage_type, limit_field, and min_amount_cents")
        elif self.rule_type in (RequirementType.POLICY_EXPIRATION, RequirementType.POLICY_EFFECTIVE_DATE):
            if not self.coverage_type:
                raise ValueError(f"{self.rule_type.value} requires coverage_type")
        elif self.rule_type == RequirementType.NAMED_INSURED:
            if not self.required_entity_name or not self.required_entity_name.strip():
                raise ValueError("NAMED_INSURED requires required_entity_name")
        elif self.rule_type == RequirementType.ENDORSEMENT_PRESENCE:
            if not self.required_endorsement_type:
                raise ValueError("ENDORSEMENT_PRESENCE requires required_endorsement_type")
        elif self.rule_type == RequirementType.CONTRACTUAL_DURATION:
            if not self.coverage_type and not self.required_endorsement_type:
                raise ValueError("CONTRACTUAL_DURATION requires coverage_type or required_endorsement_type")
            if self.required_coverage_end_date is None and not self.requires_coverage_through_policy_expiration:
                raise ValueError("CONTRACTUAL_DURATION requires required_coverage_end_date or requires_coverage_through_policy_expiration")
        return self


# ============================================================================
# Compliance Evaluation Results
# ============================================================================

class ComplianceFinding(BaseModel):
    """Deterministic evaluation outcome for a single requirement."""
    finding_id: str = Field(default_factory=lambda: str(uuid4()))
    requirement_id: str
    rule_type: RequirementType
    status: ComplianceStatus
    severity: Severity
    reason_code: str = Field(description="Machine-readable code, e.g. 'LIMIT_BELOW_MINIMUM'")
    explanation: str = Field(description="Detailed human-readable reasoning")
    requires_human_review: bool
    required_value: Optional[Any] = None
    found_value: Optional[Any] = None
    evidence: List[EvidenceReference] = Field(default_factory=list)


class ComplianceReviewResult(BaseModel):
    """Aggregated compliance review across all configured requirements."""
    subcontractor_id: str
    evaluation_date: date
    overall_status: ComplianceStatus
    requires_human_review: bool
    findings: List[ComplianceFinding]
    reconciliation_summary: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Structured summary of cross-document reconciliation checks and findings",
    )
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


SUPPORTED_EVALUATION_RULE_TYPES = {
    RequirementType.COVERAGE_LIMIT,
    RequirementType.POLICY_EXPIRATION,
    RequirementType.POLICY_EFFECTIVE_DATE,
    RequirementType.NAMED_INSURED,
    RequirementType.ENDORSEMENT_PRESENCE,
    RequirementType.CONTRACTUAL_DURATION,
}


def validate_requirement_profile(
    requirements: List[InsuranceRequirement],
    enforce_effective_date_baseline: bool = True,
) -> List[InsuranceRequirement]:
    """Validate and normalize an insurance requirement profile.

    Enforces:
    - Non-empty requirement list (explicit empty profiles must be rejected, not defaulted)
    - Unique, non-empty requirement_id values
    - Supported, structurally complete requirement definitions
    - Baseline POLICY_EFFECTIVE_DATE paired with POLICY_EXPIRATION when enabled
    """
    if not requirements:
        raise ValueError("Requirement profile must contain at least one requirement.")

    seen_ids: set[str] = set()
    validated: List[InsuranceRequirement] = []
    for req in requirements:
        req_id = (req.requirement_id or "").strip()
        if not req_id:
            raise ValueError("Each requirement must define a non-empty requirement_id.")
        if req_id in seen_ids:
            raise ValueError(f"Duplicate requirement_id '{req_id}' in requirement profile.")
        if req.rule_type not in SUPPORTED_EVALUATION_RULE_TYPES:
            raise ValueError(f"Unsupported requirement rule_type '{req.rule_type.value}' in requirement '{req_id}'.")
        req.validate_requirement_fields()
        seen_ids.add(req_id)
        validated.append(req)

    if not enforce_effective_date_baseline:
        return validated

    expiration_targets = {
        (r.coverage_type, (r.target_policy_number or "").strip() or None)
        for r in validated
        if r.rule_type == RequirementType.POLICY_EXPIRATION and r.coverage_type
    }
    effective_targets = {
        (r.coverage_type, (r.target_policy_number or "").strip() or None)
        for r in validated
        if r.rule_type == RequirementType.POLICY_EFFECTIVE_DATE and r.coverage_type
    }

    missing_targets = sorted(
        [
            (cov_type, pol)
            for cov_type, pol in expiration_targets
            if (cov_type, pol) not in effective_targets and (cov_type, None) not in effective_targets
        ],
        key=lambda item: (item[0].value, item[1] or ""),
    )

    for idx, (cov_type, target_pol) in enumerate(missing_targets):
        base_id = f"REQ_{cov_type.value.upper()}_EFFECTIVE_DATE"
        candidate_id = base_id if base_id not in seen_ids else f"{base_id}_{idx + 1}"
        while candidate_id in seen_ids:
            candidate_id = f"{candidate_id}_BASELINE"
        seen_ids.add(candidate_id)
        validated.append(
            InsuranceRequirement(
                requirement_id=candidate_id,
                rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
                description=f"{cov_type.value} policy must be effective on or before evaluation date",
                coverage_type=cov_type,
                target_policy_number=target_pol,
                severity=Severity.CRITICAL,
            )
        )
    return validated

