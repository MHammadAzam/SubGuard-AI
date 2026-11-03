"""Ground truth schemas for SubGuard AI evaluation and benchmarking.

Supports:
- Document metadata with strict source classification (synthetic vs public sample vs licensed real).
- Granular field-level expected values (amounts in cents, ISO dates, entities, coverages, endorsements).
- Expected evidence references with verbatim quotes and page numbers.
- Missing-field and ambiguity annotations to evaluate true negative detection and uncertainty handling.
"""

from __future__ import annotations

from datetime import date as dt_date
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.insurance import (
    BoundingBox,
    ComplianceStatus,
    CoverageType,
    DocumentType,
    EndorsementType,
    ExtractionStatus,
    InsuranceRequirement,
    RequirementType,
)


class FieldPresenceStatus(str, Enum):
    """Ground-truth presence and verifiability state of an insurance field."""
    PRESENT_VERIFIABLE = "present_verifiable"
    ABSENT_VERIFIED = "absent_verified"
    ABSENT_TRUNCATED = "absent_truncated"
    PRESENT_UNREADABLE = "present_unreadable"
    CONTRADICTORY = "contradictory"


class DocumentSourceType(str, Enum):
    """Origin and licensing status of benchmark documents.
    
    Protects against leaking proprietary/PII contractor documents.
    """
    SYNTHETIC = "synthetic"          # Programmatically generated or fully redacted test fixture
    PUBLIC_SAMPLE = "public_sample"  # Open public sample with no sensitive contractor PII
    LICENSED_REAL = "licensed_real"  # Real-world licensed document (never committed to open source)


class ExpectedEvidence(BaseModel):
    """Expected evidence citation for a ground-truth field."""
    page_number: int = Field(ge=1, description="Expected 1-indexed page number")
    source_text: str = Field(description="Exact verbatim excerpt that must appear on the page")
    bounding_box: Optional[BoundingBox] = None


class ExpectedMoney(BaseModel):
    """Expected monetary value in integer cents with raw text representation."""
    amount_cents: int = Field(ge=0, description="Amount in whole cents, e.g. 100_000_000 for $1,000,000")
    currency: str = Field(default="USD", max_length=3)
    raw_text: Optional[str] = None
    is_absent: bool = False
    presence_status: FieldPresenceStatus = FieldPresenceStatus.PRESENT_VERIFIABLE


class ExpectedDate(BaseModel):
    """Expected policy or signature date."""
    date: Optional[dt_date] = None
    raw_text: Optional[str] = None
    is_absent: bool = False
    is_ambiguous: bool = False
    presence_status: FieldPresenceStatus = FieldPresenceStatus.PRESENT_VERIFIABLE


class ExpectedEntity(BaseModel):
    """Expected party details (named insured, certificate holder, producer)."""
    name: Optional[str] = None
    address: Optional[str] = None
    is_absent: bool = False
    evidence: Optional[ExpectedEvidence] = None
    presence_status: FieldPresenceStatus = FieldPresenceStatus.PRESENT_VERIFIABLE


class ExpectedCoverage(BaseModel):
    """Expected insurance coverage line item."""
    coverage_type: CoverageType
    policy_number: Optional[str] = None
    insurer_name: Optional[str] = None

    # Sub-limits in cents
    each_occurrence: Optional[ExpectedMoney] = None
    general_aggregate: Optional[ExpectedMoney] = None
    products_completed_ops: Optional[ExpectedMoney] = None
    personal_adv_injury: Optional[ExpectedMoney] = None
    combined_single_limit: Optional[ExpectedMoney] = None
    per_accident: Optional[ExpectedMoney] = None
    disease_each_employee: Optional[ExpectedMoney] = None
    disease_policy_limit: Optional[ExpectedMoney] = None

    # Dates
    effective_date: Optional[ExpectedDate] = None
    expiration_date: Optional[ExpectedDate] = None

    is_absent: bool = False
    evidence: Optional[ExpectedEvidence] = None
    presence_status: FieldPresenceStatus = FieldPresenceStatus.PRESENT_VERIFIABLE


class ExpectedEndorsement(BaseModel):
    """Expected endorsement on COI remarks or endorsement form."""
    endorsement_type: EndorsementType
    form_number: Optional[str] = None
    is_blanket: bool = False
    is_coi_checkbox_only: bool = False
    named_entities: List[str] = Field(default_factory=list)
    is_absent: bool = False
    evidence: Optional[ExpectedEvidence] = None
    presence_status: FieldPresenceStatus = FieldPresenceStatus.PRESENT_VERIFIABLE


class DocumentGroundTruth(BaseModel):
    """Complete ground-truth annotation for a single benchmark document."""
    document_id: str = Field(description="Unique identifier for the document")
    filename: str = Field(description="PDF filename within the dataset directory")
    source_type: DocumentSourceType = Field(default=DocumentSourceType.SYNTHETIC)
    document_type: DocumentType = Field(default=DocumentType.COI)
    page_count: int = Field(ge=1, default=1)
    description: Optional[str] = None
    requires_ocr: bool = Field(default=False, description="True if document lacks text layer and must trigger OCR")

    # Expected entities
    named_insured: Optional[ExpectedEntity] = None
    certificate_holder: Optional[ExpectedEntity] = None

    # Expected coverages and endorsements
    coverages: List[ExpectedCoverage] = Field(default_factory=list)
    endorsements: List[ExpectedEndorsement] = Field(default_factory=list)

    # Explicit negative assertions (fields known to be missing from the document)
    absent_coverages: List[CoverageType] = Field(
        default_factory=list,
        description="Coverage types that are explicitly absent and must NOT be extracted"
    )
    absent_endorsements: List[EndorsementType] = Field(
        default_factory=list,
        description="Endorsement types that are explicitly absent and must NOT be extracted"
    )


class EvaluationDataset(BaseModel):
    """Collection of ground-truth annotated documents forming a benchmark suite."""
    dataset_id: str
    name: str
    version: str = "1.0.0"
    description: str = ""
    documents: List[DocumentGroundTruth] = Field(default_factory=list)


class ExpectedFinding(BaseModel):
    """Expected compliance finding for an individual insurance requirement."""
    requirement_id: str = Field(description="Matches an InsuranceRequirement.requirement_id")
    rule_type: RequirementType
    expected_status: ComplianceStatus
    expected_reason_code: str
    requires_human_review: bool
    supporting_document_id: Optional[str] = None
    supporting_page: Optional[int] = Field(default=None, ge=1)
    expected_verbatim_quote: Optional[str] = None
    expected_value: Optional[Any] = None
    expected_uncertainty_classification: Optional[str] = None


class ScenarioGroundTruth(BaseModel):
    """Ground-truth definition for a multi-document subcontractor submission scenario."""
    scenario_id: str = Field(description="Unique stable scenario identifier, e.g. SCEN-01")
    title: str
    description: str
    subcontractor_id: str
    evaluation_date: dt_date
    document_ids: List[str] = Field(description="List of document IDs belonging to this scenario")
    documents: List[DocumentGroundTruth] = Field(default_factory=list, description="Field-level ground truth per document")
    requirements: List[InsuranceRequirement] = Field(default_factory=list, description="Configured requirements")
    expected_findings: List[ExpectedFinding] = Field(default_factory=list, description="Ground truth findings")
    expected_overall_status: ComplianceStatus
    expected_requires_human_review: bool
    is_document_set_complete: bool = True
    annotation_notes: Optional[str] = None
    scenario_classification_tags: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_scenario(self) -> ScenarioGroundTruth:
        if not self.scenario_id or not self.scenario_id.strip():
            raise ValueError("scenario_id cannot be empty")

        doc_map = {d.document_id: d for d in self.documents}
        for doc_id in self.document_ids:
            if doc_id not in doc_map:
                raise ValueError(f"document_id '{doc_id}' listed in document_ids but not found in documents")

        for finding in self.expected_findings:
            if finding.supporting_document_id:
                if finding.supporting_document_id not in doc_map:
                    raise ValueError(
                        f"Finding '{finding.requirement_id}' references unknown supporting_document_id '{finding.supporting_document_id}'"
                    )
                doc = doc_map[finding.supporting_document_id]
                if finding.supporting_page is not None and finding.supporting_page > doc.page_count:
                    raise ValueError(
                        f"Finding '{finding.requirement_id}' references page {finding.supporting_page} "
                        f"exceeding document page count {doc.page_count}"
                    )

        if self.expected_overall_status == ComplianceStatus.COMPLIANT and self.expected_requires_human_review:
            raise ValueError("Scenario cannot have expected_overall_status=COMPLIANT and expected_requires_human_review=True")

        has_non_compliant_finding = any(f.expected_status == ComplianceStatus.NON_COMPLIANT for f in self.expected_findings)
        if has_non_compliant_finding and self.expected_overall_status == ComplianceStatus.COMPLIANT:
            raise ValueError("Scenario cannot be COMPLIANT when it has NON_COMPLIANT findings")

        has_review_finding = any(f.requires_human_review for f in self.expected_findings)
        if has_review_finding and self.expected_overall_status == ComplianceStatus.COMPLIANT:
            raise ValueError("Scenario cannot be COMPLIANT when findings require human review")

        return self


class ScenarioEvaluationDataset(BaseModel):
    """Collection of ground-truth annotated multi-document scenarios."""
    dataset_id: str
    name: str
    version: str = "1.0.0"
    description: str = ""
    scenarios: List[ScenarioGroundTruth] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_scenario_ids(self) -> ScenarioEvaluationDataset:
        seen = set()
        for sc in self.scenarios:
            if sc.scenario_id in seen:
                raise ValueError(f"Duplicate scenario_id '{sc.scenario_id}' found in dataset")
            seen.add(sc.scenario_id)
        return self

