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
from pydantic import BaseModel, Field

from app.schemas.insurance import (
    BoundingBox,
    CoverageType,
    DocumentType,
    EndorsementType,
    ExtractionStatus,
)


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


class ExpectedDate(BaseModel):
    """Expected policy or signature date."""
    date: Optional[dt_date] = None
    raw_text: Optional[str] = None
    is_absent: bool = False
    is_ambiguous: bool = False


class ExpectedEntity(BaseModel):
    """Expected party details (named insured, certificate holder, producer)."""
    name: Optional[str] = None
    address: Optional[str] = None
    is_absent: bool = False
    evidence: Optional[ExpectedEvidence] = None


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


class ExpectedEndorsement(BaseModel):
    """Expected endorsement on COI remarks or endorsement form."""
    endorsement_type: EndorsementType
    form_number: Optional[str] = None
    is_blanket: bool = False
    is_coi_checkbox_only: bool = False
    named_entities: List[str] = Field(default_factory=list)
    is_absent: bool = False
    evidence: Optional[ExpectedEvidence] = None


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
