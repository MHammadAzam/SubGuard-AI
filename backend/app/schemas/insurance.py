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
from typing import Any, List, Optional
from uuid import UUID, uuid4
from pydantic import BaseModel, Field, field_validator, model_validator


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
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(ge=0.0, le=1.0)
    height: float = Field(ge=0.0, le=1.0)


class EvidenceReference(BaseModel):
    """Traceable pointer to the source document location."""
    document_id: str
    document_type: DocumentType = DocumentType.COI
    page_number: int = Field(ge=1, description="1-indexed source page")
    source_text: str = Field(description="Exact verbatim text excerpt from document")
    bounding_box: Optional[BoundingBox] = None


class MoneyValue(BaseModel):
    """Monetary limit represented in integer cents to prevent floating point drift."""
    amount_cents: int = Field(ge=0, description="Amount in whole cents ($1,000,000 = 100,000,000 cents)")
    currency: str = Field(default="USD", max_length=3)
    raw_text: str = Field(description="Raw string as extracted, e.g. '$1,000,000'")
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class DateValue(BaseModel):
    """Extracted date with supporting text."""
    date: date
    raw_text: str = Field(description="Raw date string, e.g. '06/01/2025'")
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


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
        description="Defensible verification basis for negative manuscript determination: e.g. 'unmodified_standard_form', 'full_text_inspection', or None if unverified"
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
    certificate_holder: Optional[EntityInfo] = None
    coverages: List[CoverageExtraction] = Field(default_factory=list)
    endorsements: List[EndorsementExtraction] = Field(default_factory=list)
    raw_contradictions_detected: List[str] = Field(default_factory=list)


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
    limit_field: Optional[LimitField] = None
    min_amount_cents: Optional[int] = Field(default=None, ge=0)
    required_endorsement_type: Optional[EndorsementType] = None
    required_entity_name: Optional[str] = None
    require_exact_name_match: bool = False
    require_separate_endorsement_document: bool = True
    mandatory_evidence: bool = True

    @model_validator(mode="after")
    def validate_requirement_fields(self) -> InsuranceRequirement:
        if self.rule_type == RequirementType.COVERAGE_LIMIT:
            if not self.coverage_type or not self.limit_field or self.min_amount_cents is None:
                raise ValueError("COVERAGE_LIMIT requires coverage_type, limit_field, and min_amount_cents")
        elif self.rule_type == RequirementType.NAMED_INSURED:
            if not self.required_entity_name:
                raise ValueError("NAMED_INSURED requires required_entity_name")
        elif self.rule_type == RequirementType.ENDORSEMENT_PRESENCE:
            if not self.required_endorsement_type:
                raise ValueError("ENDORSEMENT_PRESENCE requires required_endorsement_type")
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
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def validate_requirement_profile(
    requirements: List[InsuranceRequirement],
    enforce_effective_date_baseline: bool = True,
) -> List[InsuranceRequirement]:
    """Validate and normalize an insurance requirement profile.
    
    If enforce_effective_date_baseline is True, verifies that any coverage line evaluating
    POLICY_EXPIRATION also includes a corresponding POLICY_EFFECTIVE_DATE requirement.
    If omitted, a corresponding baseline POLICY_EFFECTIVE_DATE requirement is automatically injected
    to guarantee full policy active-term protection.
    """
    validated = list(requirements)
    if not enforce_effective_date_baseline:
        return validated

    expiration_cov_types = {
        r.coverage_type for r in requirements if r.rule_type == RequirementType.POLICY_EXPIRATION and r.coverage_type
    }
    effective_cov_types = {
        r.coverage_type for r in requirements if r.rule_type == RequirementType.POLICY_EFFECTIVE_DATE and r.coverage_type
    }

    missing_cov_types = expiration_cov_types - effective_cov_types
    for cov_type in sorted(missing_cov_types, key=lambda c: c.value):
        validated.append(
            InsuranceRequirement(
                requirement_id=f"REQ_{cov_type.value.upper()}_EFFECTIVE_DATE",
                rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
                description=f"{cov_type.value} policy must be effective on or before evaluation date",
                coverage_type=cov_type,
                severity=Severity.CRITICAL,
            )
        )
    return validated

