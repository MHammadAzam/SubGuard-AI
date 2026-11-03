"""Synthetic test fixtures for compliance engine evaluation."""

from datetime import date
from app.schemas.insurance import (
    BoundingBox,
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
    InsuranceRequirement,
    LimitField,
    MoneyValue,
    RequirementType,
    Severity,
)

# Standard evaluation date used across tests
EVAL_DATE = date(2025, 6, 15)


def make_evidence(
    doc_id: str = "doc_acord_25_001",
    doc_type: DocumentType = DocumentType.COI,
    page: int = 1,
    text: str = "$1,000,000 EACH OCCURRENCE",
) -> EvidenceReference:
    return EvidenceReference(
        document_id=doc_id,
        document_type=doc_type,
        page_number=page,
        source_text=text,
        bounding_box=BoundingBox(x=0.1, y=0.2, width=0.3, height=0.05),
    )


def sample_gl_coverage(
    occ_cents: int = 100_000_000,
    agg_cents: int = 200_000_000,
    eff: date = date(2025, 1, 1),
    exp: date = date(2026, 1, 1),
    confidence: float = 0.95,
    status: ExtractionStatus = ExtractionStatus.EXTRACTED,
) -> CoverageExtraction:
    return CoverageExtraction(
        coverage_type=CoverageType.GENERAL_LIABILITY,
        policy_number="GL-987654-01",
        insurer_name="Travelers Indemnity Co",
        each_occurrence=MoneyValue(
            amount_cents=occ_cents,
            raw_text=f"${occ_cents/100:,.0f}",
            confidence=confidence,
        ),
        general_aggregate=MoneyValue(
            amount_cents=agg_cents,
            raw_text=f"${agg_cents/100:,.0f}",
            confidence=confidence,
        ),
        effective_date=DateValue(
            date=eff,
            raw_text=eff.strftime("%m/%d/%Y"),
            confidence=confidence,
        ),
        expiration_date=DateValue(
            date=exp,
            raw_text=exp.strftime("%m/%d/%Y"),
            confidence=confidence,
        ),
        evidence=make_evidence(text=f"GL EACH OCCURRENCE ${occ_cents/100:,.0f}"),
        extraction_status=status,
    )


def sample_subcontractor_document_set(
    sub_id: str = "sub_apex_electric",
    named_insured_name: str = "Apex Electrical Contractors LLC",
    ni_confidence: float = 0.98,
    gl_coverage: CoverageExtraction = None,
    endorsements: list = None,
    is_complete: bool = True,
    contradictions: list = None,
) -> ExtractedDocumentSet:
    if gl_coverage is None:
        gl_coverage = sample_gl_coverage()

    return ExtractedDocumentSet(
        subcontractor_id=sub_id,
        documents_submitted=[DocumentType.COI, DocumentType.ENDORSEMENT],
        is_document_set_complete=is_complete,
        named_insured=EntityInfo(
            name=named_insured_name,
            address="123 Industrial Pkwy, Austin, TX 78701",
            confidence=ni_confidence,
            evidence=make_evidence(text=f"NAMED INSURED: {named_insured_name}"),
        ),
        coverages=[gl_coverage],
        endorsements=endorsements or [],
        raw_contradictions_detected=contradictions or [],
    )


def sample_gl_requirement(
    req_id: str = "REQ_GL_EACH_OCC_1M",
    min_cents: int = 100_000_000,
    field: LimitField = LimitField.EACH_OCCURRENCE,
) -> InsuranceRequirement:
    return InsuranceRequirement(
        requirement_id=req_id,
        rule_type=RequirementType.COVERAGE_LIMIT,
        description=f"General Liability {field.value} must be at least ${min_cents/100:,.0f}",
        severity=Severity.CRITICAL,
        coverage_type=CoverageType.GENERAL_LIABILITY,
        limit_field=field,
        min_amount_cents=min_cents,
    )
