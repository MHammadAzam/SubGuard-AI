"""Dataset loader and serializer for SubGuard AI evaluation benchmarks.

Provides utilities for:
1. Building the standard synthetic benchmark dataset.
2. Saving datasets to JSON with complete ground truth annotations.
3. Loading datasets from JSON files.
4. Validating dataset schema contracts.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

from app.schemas.insurance import CoverageType, DocumentType, EndorsementType
from evaluation.schemas.ground_truth import (
    DocumentGroundTruth,
    DocumentSourceType,
    EvaluationDataset,
    ExpectedCoverage,
    ExpectedDate,
    ExpectedEndorsement,
    ExpectedEntity,
    ExpectedEvidence,
    ExpectedMoney,
)


def build_default_synthetic_dataset() -> EvaluationDataset:
    """Build the official SubGuard AI synthetic evaluation benchmark dataset."""
    docs = [
        # 1. Standard ACORD 25 COI
        DocumentGroundTruth(
            document_id="SYNTH-001",
            filename="synthetic_standard_acord25.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            document_type=DocumentType.COI,
            page_count=1,
            description="Standard single-page ACORD 25 with GL, Auto, and Workers Comp",
            requires_ocr=False,
            named_insured=ExpectedEntity(
                name="Apex Electrical Contractors LLC",
                address="123 Industrial Pkwy, Austin, TX 78701",
                evidence=ExpectedEvidence(
                    page_number=1,
                    source_text="INSURED: Apex Electrical Contractors LLC",
                ),
            ),
            certificate_holder=ExpectedEntity(
                name="General Contractor Corp",
                address="500 Builder Way, Dallas, TX 75201",
                evidence=ExpectedEvidence(
                    page_number=1,
                    source_text="CERTIFICATE HOLDER: General Contractor Corp",
                ),
            ),
            coverages=[
                ExpectedCoverage(
                    coverage_type=CoverageType.GENERAL_LIABILITY,
                    policy_number="GL-98765432",
                    insurer_name="Travelers Indemnity Co",
                    each_occurrence=ExpectedMoney(amount_cents=100_000_000, raw_text="$1,000,000"),
                    general_aggregate=ExpectedMoney(amount_cents=200_000_000, raw_text="$2,000,000"),
                    products_completed_ops=ExpectedMoney(amount_cents=200_000_000, raw_text="$2,000,000"),
                    effective_date=ExpectedDate(date=date(2025, 1, 1), raw_text="01/01/2025"),
                    expiration_date=ExpectedDate(date=date(2026, 1, 1), raw_text="01/01/2026"),
                    evidence=ExpectedEvidence(page_number=1, source_text="POLICY NUMBER: GL-98765432"),
                ),
                ExpectedCoverage(
                    coverage_type=CoverageType.AUTO_LIABILITY,
                    policy_number="AL-12345678",
                    combined_single_limit=ExpectedMoney(amount_cents=100_000_000, raw_text="$1,000,000"),
                    effective_date=ExpectedDate(date=date(2025, 1, 1), raw_text="01/01/2025"),
                    expiration_date=ExpectedDate(date=date(2026, 1, 1), raw_text="01/01/2026"),
                    evidence=ExpectedEvidence(page_number=1, source_text="POLICY NUMBER: AL-12345678"),
                ),
                ExpectedCoverage(
                    coverage_type=CoverageType.WORKERS_COMP,
                    policy_number="WC-55554444",
                    insurer_name="Texas Mutual Insurance",
                    per_accident=ExpectedMoney(amount_cents=100_000_000, raw_text="$1,000,000"),
                    disease_each_employee=ExpectedMoney(amount_cents=100_000_000, raw_text="$1,000,000"),
                    disease_policy_limit=ExpectedMoney(amount_cents=100_000_000, raw_text="$1,000,000"),
                    effective_date=ExpectedDate(date=date(2025, 1, 1), raw_text="01/01/2025"),
                    expiration_date=ExpectedDate(date=date(2026, 1, 1), raw_text="01/01/2026"),
                    evidence=ExpectedEvidence(page_number=1, source_text="POLICY NUMBER: WC-55554444"),
                ),
            ],
            endorsements=[
                ExpectedEndorsement(
                    endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                    is_blanket=False,
                    is_coi_checkbox_only=False,
                    named_entities=["General Contractor Corp"],
                    evidence=ExpectedEvidence(
                        page_number=1,
                        source_text="General Contractor Corp is included as Additional Insured on General Liability.",
                    ),
                )
            ],
            absent_coverages=[CoverageType.UMBRELLA_EXCESS, CoverageType.PROFESSIONAL_LIABILITY],
            absent_endorsements=[EndorsementType.WAIVER_OF_SUBROGATION, EndorsementType.PRIMARY_NONCONTRIBUTORY],
        ),

        # 2. Multi-page COI + Endorsement (CG 20 10 on page 2)
        DocumentGroundTruth(
            document_id="SYNTH-002",
            filename="synthetic_multipage_coi_endorsement.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            document_type=DocumentType.COI,
            page_count=2,
            description="2-page document with COI on page 1 and CG 20 10 on page 2",
            requires_ocr=False,
            named_insured=ExpectedEntity(
                name="Apex Electrical Contractors LLC",
                evidence=ExpectedEvidence(page_number=1, source_text="INSURED: Apex Electrical Contractors LLC"),
            ),
            certificate_holder=ExpectedEntity(
                name="General Contractor Corp",
                evidence=ExpectedEvidence(page_number=1, source_text="CERTIFICATE HOLDER: General Contractor Corp"),
            ),
            coverages=[
                ExpectedCoverage(
                    coverage_type=CoverageType.GENERAL_LIABILITY,
                    policy_number="GL-98765432",
                    each_occurrence=ExpectedMoney(amount_cents=100_000_000, raw_text="$1,000,000"),
                    general_aggregate=ExpectedMoney(amount_cents=200_000_000, raw_text="$2,000,000"),
                    effective_date=ExpectedDate(date=date(2025, 1, 1), raw_text="01/01/2025"),
                    expiration_date=ExpectedDate(date=date(2026, 1, 1), raw_text="01/01/2026"),
                    evidence=ExpectedEvidence(page_number=1, source_text="COMMERCIAL GENERAL LIABILITY - POLICY NUMBER: GL-98765432"),
                )
            ],
            endorsements=[
                ExpectedEndorsement(
                    endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                    form_number="CG 20 10 04 13",
                    is_blanket=False,
                    named_entities=["General Contractor Corp"],
                    evidence=ExpectedEvidence(
                        page_number=2,
                        source_text="COMMERCIAL GENERAL LIABILITY CG 20 10 04 13",
                    ),
                )
            ],
            absent_coverages=[CoverageType.WORKERS_COMP, CoverageType.UMBRELLA_EXCESS],
            absent_endorsements=[EndorsementType.WAIVER_OF_SUBROGATION],
        ),

        # 3. Missing Named Insured and Missing Workers' Comp
        DocumentGroundTruth(
            document_id="SYNTH-003",
            filename="synthetic_missing_sections.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            document_type=DocumentType.COI,
            page_count=1,
            description="COI with missing named insured and missing Workers Comp",
            requires_ocr=False,
            named_insured=ExpectedEntity(name=None, is_absent=True),
            certificate_holder=ExpectedEntity(
                name="General Contractor Corp",
                evidence=ExpectedEvidence(page_number=1, source_text="CERTIFICATE HOLDER: General Contractor Corp"),
            ),
            coverages=[
                ExpectedCoverage(
                    coverage_type=CoverageType.GENERAL_LIABILITY,
                    policy_number="GL-11223344",
                    each_occurrence=ExpectedMoney(amount_cents=100_000_000, raw_text="$1,000,000"),
                    general_aggregate=ExpectedMoney(amount_cents=200_000_000, raw_text="$2,000,000"),
                    effective_date=ExpectedDate(date=date(2025, 1, 1), raw_text="01/01/2025"),
                    expiration_date=ExpectedDate(date=date(2026, 1, 1), raw_text="01/01/2026"),
                    evidence=ExpectedEvidence(page_number=1, source_text="POLICY NUMBER: GL-11223344"),
                )
            ],
            endorsements=[],
            absent_coverages=[CoverageType.WORKERS_COMP, CoverageType.AUTO_LIABILITY],
            absent_endorsements=[EndorsementType.ADDITIONAL_INSURED, EndorsementType.WAIVER_OF_SUBROGATION],
        ),

        # 4. Umbrella Excess Liability
        DocumentGroundTruth(
            document_id="SYNTH-004",
            filename="synthetic_split_limits_and_umbrella.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            document_type=DocumentType.COI,
            page_count=1,
            description="COI with Umbrella excess liability of $5,000,000",
            requires_ocr=False,
            named_insured=ExpectedEntity(
                name="Apex Electrical Contractors LLC",
                evidence=ExpectedEvidence(page_number=1, source_text="INSURED: Apex Electrical Contractors LLC"),
            ),
            certificate_holder=ExpectedEntity(
                name="General Contractor Corp",
                evidence=ExpectedEvidence(page_number=1, source_text="CERTIFICATE HOLDER: General Contractor Corp"),
            ),
            coverages=[
                ExpectedCoverage(
                    coverage_type=CoverageType.GENERAL_LIABILITY,
                    policy_number="GL-98765432",
                    each_occurrence=ExpectedMoney(amount_cents=100_000_000, raw_text="$1,000,000"),
                    general_aggregate=ExpectedMoney(amount_cents=200_000_000, raw_text="$2,000,000"),
                    effective_date=ExpectedDate(date=date(2025, 1, 1), raw_text="01/01/2025"),
                    expiration_date=ExpectedDate(date=date(2026, 1, 1), raw_text="01/01/2026"),
                ),
                ExpectedCoverage(
                    coverage_type=CoverageType.UMBRELLA_EXCESS,
                    policy_number="UMB-55667788",
                    each_occurrence=ExpectedMoney(amount_cents=500_000_000, raw_text="$5,000,000"),
                    general_aggregate=ExpectedMoney(amount_cents=500_000_000, raw_text="$5,000,000"),
                    effective_date=ExpectedDate(date=date(2025, 1, 1), raw_text="01/01/2025"),
                    expiration_date=ExpectedDate(date=date(2026, 1, 1), raw_text="01/01/2026"),
                    evidence=ExpectedEvidence(page_number=1, source_text="POLICY NUMBER: UMB-55667788"),
                ),
            ],
            absent_coverages=[CoverageType.WORKERS_COMP],
            absent_endorsements=[],
        ),

        # 5. Expired GL Dates and Inverted Auto Dates
        DocumentGroundTruth(
            document_id="SYNTH-005",
            filename="synthetic_expired_and_inverted_dates.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            document_type=DocumentType.COI,
            page_count=1,
            description="COI with expired General Liability dates and inverted Auto dates",
            requires_ocr=False,
            named_insured=ExpectedEntity(
                name="Apex Electrical Contractors LLC",
                evidence=ExpectedEvidence(page_number=1, source_text="INSURED: Apex Electrical Contractors LLC"),
            ),
            certificate_holder=ExpectedEntity(
                name="General Contractor Corp",
                evidence=ExpectedEvidence(page_number=1, source_text="CERTIFICATE HOLDER: General Contractor Corp"),
            ),
            coverages=[
                ExpectedCoverage(
                    coverage_type=CoverageType.GENERAL_LIABILITY,
                    policy_number="GL-98765432",
                    each_occurrence=ExpectedMoney(amount_cents=100_000_000, raw_text="$1,000,000"),
                    effective_date=ExpectedDate(date=date(2023, 1, 1), raw_text="01/01/2023"),
                    expiration_date=ExpectedDate(date=date(2024, 1, 1), raw_text="01/01/2024"),
                ),
                ExpectedCoverage(
                    coverage_type=CoverageType.AUTO_LIABILITY,
                    policy_number="AL-99887766",
                    combined_single_limit=ExpectedMoney(amount_cents=100_000_000, raw_text="$1,000,000"),
                    effective_date=ExpectedDate(date=date(2026, 1, 1), raw_text="01/01/2026"),
                    expiration_date=ExpectedDate(date=date(2025, 1, 1), raw_text="01/01/2025"),
                ),
            ],
            absent_coverages=[CoverageType.WORKERS_COMP, CoverageType.UMBRELLA_EXCESS],
            absent_endorsements=[],
        ),

        # 6. Scanned image-only PDF
        DocumentGroundTruth(
            document_id="SYNTH-006",
            filename="synthetic_scanned_ocr.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            document_type=DocumentType.UNKNOWN,
            page_count=1,
            description="Scanned image-only PDF requiring OCR processing",
            requires_ocr=True,
            absent_coverages=[],
            absent_endorsements=[],
        ),

        # 7. Corporate Suffix Variation ('Apex Electrical Inc')
        DocumentGroundTruth(
            document_id="SYNTH-007",
            filename="synthetic_corporate_suffix_variation.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            document_type=DocumentType.COI,
            page_count=1,
            description="COI naming 'Apex Electrical Inc' rather than 'Apex Electrical Contractors LLC'",
            requires_ocr=False,
            named_insured=ExpectedEntity(
                name="Apex Electrical Inc",
                evidence=ExpectedEvidence(page_number=1, source_text="INSURED: Apex Electrical Inc"),
            ),
            certificate_holder=ExpectedEntity(
                name="General Contractor Corp",
                evidence=ExpectedEvidence(page_number=1, source_text="CERTIFICATE HOLDER: General Contractor Corp"),
            ),
            coverages=[
                ExpectedCoverage(
                    coverage_type=CoverageType.GENERAL_LIABILITY,
                    policy_number="GL-98765432",
                    each_occurrence=ExpectedMoney(amount_cents=100_000_000, raw_text="$1,000,000"),
                    general_aggregate=ExpectedMoney(amount_cents=200_000_000, raw_text="$2,000,000"),
                    effective_date=ExpectedDate(date=date(2025, 1, 1), raw_text="01/01/2025"),
                    expiration_date=ExpectedDate(date=date(2026, 1, 1), raw_text="01/01/2026"),
                )
            ],
            absent_coverages=[CoverageType.WORKERS_COMP],
            absent_endorsements=[],
        ),

        # 8. Blanket Endorsement Wording
        DocumentGroundTruth(
            document_id="SYNTH-008",
            filename="synthetic_blanket_endorsement.pdf",
            source_type=DocumentSourceType.SYNTHETIC,
            document_type=DocumentType.ENDORSEMENT,
            page_count=1,
            description="Endorsement document with blanket automatic additional insured wording",
            requires_ocr=False,
            endorsements=[
                ExpectedEndorsement(
                    endorsement_type=EndorsementType.ADDITIONAL_INSURED,
                    is_blanket=True,
                    is_coi_checkbox_only=False,
                    evidence=ExpectedEvidence(
                        page_number=1,
                        source_text="organization whom you have agreed by written contract to add as additional insured",
                    ),
                )
            ],
            absent_coverages=[CoverageType.GENERAL_LIABILITY, CoverageType.WORKERS_COMP],
            absent_endorsements=[EndorsementType.WAIVER_OF_SUBROGATION],
        ),
    ]

    return EvaluationDataset(
        dataset_id="subguard-synthetic-v1",
        name="SubGuard AI Standard Synthetic Benchmark",
        version="1.0.0",
        description="Comprehensive synthetic suite evaluating entities, limits, dates, endorsements, and evidence grounding.",
        documents=docs,
    )


def save_dataset_to_json(dataset: EvaluationDataset, output_path: Path) -> None:
    """Serialize dataset to JSON file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(dataset.model_dump_json(indent=2))


def load_dataset_from_json(input_path: Path) -> EvaluationDataset:
    """Deserialize dataset from JSON file."""
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return EvaluationDataset.model_validate(data)
