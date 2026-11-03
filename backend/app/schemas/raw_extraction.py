"""Raw extraction schemas returned by AI extraction providers.

These models define the expected structured JSON format from LLMs/multimodal models
before deterministic validation, normalization, and evidence resolution occur.
"""

from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field


class RawEvidenceQuote(BaseModel):
    """Evidence excerpt returned directly by an extraction model."""
    page_number: int = Field(ge=1, description="1-indexed source page")
    source_text: str = Field(description="Exact verbatim excerpt from the document page")
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class RawEntityExtraction(BaseModel):
    """Extracted legal entity or party."""
    name: Optional[str] = None
    address: Optional[str] = None
    evidence: Optional[RawEvidenceQuote] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class RawCoverageSection(BaseModel):
    """Raw coverage section extracted from a COI or declaration page."""
    coverage_type: str = Field(description="e.g. general_liability, auto_liability, workers_comp, umbrella_excess")
    policy_number: Optional[str] = None
    insurer_name: Optional[str] = None

    # Raw string limit representations (e.g. '$1,000,000', '1,000,000', '$2M')
    each_occurrence: Optional[str] = None
    general_aggregate: Optional[str] = None
    products_completed_ops: Optional[str] = None
    personal_adv_injury: Optional[str] = None
    combined_single_limit: Optional[str] = None
    per_accident: Optional[str] = None
    disease_each_employee: Optional[str] = None
    disease_policy_limit: Optional[str] = None

    # Additional standard limits
    damage_to_rented_premises: Optional[str] = None
    medical_expense: Optional[str] = None

    # Raw date representations (e.g. '01/01/2025', 'Jan 1, 2025')
    effective_date: Optional[str] = None
    expiration_date: Optional[str] = None

    evidence: Optional[RawEvidenceQuote] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class RawEndorsement(BaseModel):
    """Raw endorsement information extracted from an endorsement document or COI remarks."""
    endorsement_type: str = Field(
        description="e.g. additional_insured, waiver_of_subrogation, primary_noncontributory, other"
    )
    form_number: Optional[str] = Field(default=None, description="e.g. 'CG 20 10 04 13'")
    title: Optional[str] = Field(default=None, description="Endorsement form title")
    wording: Optional[str] = Field(default=None, description="Key policy wording excerpt")
    applicable_policy: Optional[str] = None
    named_entities: List[str] = Field(default_factory=list)
    key_terms: List[str] = Field(default_factory=list)
    is_blanket: bool = False
    is_coi_checkbox_only: bool = False
    evidence: Optional[RawEvidenceQuote] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class RawInsuranceExtraction(BaseModel):
    """Top-level structured extraction returned by AI extraction provider."""
    document_type: str = Field(default="coi", description="coi, endorsement, policy_dec_page, other, unknown")
    named_insured: Optional[RawEntityExtraction] = None
    certificate_holder: Optional[RawEntityExtraction] = None
    producer: Optional[RawEntityExtraction] = None
    insurers: List[str] = Field(default_factory=list, description="Insurance company names referenced on document")
    coverages: List[RawCoverageSection] = Field(default_factory=list)
    endorsements: List[RawEndorsement] = Field(default_factory=list)
    description_of_operations: Optional[str] = None
    raw_contradictions: List[str] = Field(default_factory=list)
    is_document_set_complete: bool = True
