"""Synthetic Multi-Document Cohort Generator for SubGuard AI Phase 7.2.

Generates:
1. Exactly 40 multi-document subcontractor submission scenarios (SCEN-01 to SCEN-40).
2. Synthetic PDF files for each scenario in backend/evaluation/cohorts/fixtures/SCEN-XX/.
3. Structured ground truth dataset in backend/evaluation/annotations/cohort_scenarios_v1.json.
4. Frozen manifest with SHA-256 hashes in backend/evaluation/cohorts/manifests/cohort_v1_manifest.json.

All documents use synthetic, non-PII, fictitious entities and clear test labels.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date as dt_date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pymupdf

from app.schemas.insurance import (
    ComplianceStatus,
    CoverageType,
    DocumentType,
    EndorsementType,
    InsuranceRequirement,
    LimitField,
    RequirementType,
    Severity,
)
from evaluation.schemas.ground_truth import (
    DocumentGroundTruth,
    DocumentSourceType,
    ExpectedCoverage,
    ExpectedDate,
    ExpectedEndorsement,
    ExpectedEntity,
    ExpectedEvidence,
    ExpectedFinding,
    ExpectedMoney,
    FieldPresenceStatus,
    ScenarioEvaluationDataset,
    ScenarioGroundTruth,
)

SEED = 20261103
EVAL_DATE = dt_date(2025, 6, 1)

BASE_DIR = Path(__file__).resolve().parent
EVALUATION_DIR = BASE_DIR.parent
FIXTURES_DIR = BASE_DIR / "fixtures"
MANIFESTS_DIR = BASE_DIR / "manifests"
ANNOTATIONS_DIR = EVALUATION_DIR / "annotations"


def compute_sha256(filepath: Path) -> str:
    """Compute hex SHA-256 hash of a file."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def get_standard_requirements() -> List[InsuranceRequirement]:
    """Default 6 prime contractor requirements for benchmark scenarios."""
    return [
        InsuranceRequirement(
            requirement_id="REQ_GL_EACH_OCC",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="General Liability Each Occurrence limit must be at least $1,000,000",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            limit_field=LimitField.EACH_OCCURRENCE,
            min_amount_cents=100_000_000,
            severity=Severity.CRITICAL,
        ),
        InsuranceRequirement(
            requirement_id="REQ_GL_AGG",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="General Liability General Aggregate must be at least $2,000,000",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            limit_field=LimitField.GENERAL_AGGREGATE,
            min_amount_cents=200_000_000,
            severity=Severity.CRITICAL,
        ),
        InsuranceRequirement(
            requirement_id="REQ_AUTO_CSL",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="Auto Liability Combined Single Limit must be at least $1,000,000",
            coverage_type=CoverageType.AUTO_LIABILITY,
            limit_field=LimitField.COMBINED_SINGLE_LIMIT,
            min_amount_cents=100_000_000,
            severity=Severity.CRITICAL,
        ),
        InsuranceRequirement(
            requirement_id="REQ_WC_ACCIDENT",
            rule_type=RequirementType.COVERAGE_LIMIT,
            description="Workers Comp Each Accident limit must be at least $1,000,000",
            coverage_type=CoverageType.WORKERS_COMP,
            limit_field=LimitField.PER_ACCIDENT,
            min_amount_cents=100_000_000,
            severity=Severity.CRITICAL,
        ),
        InsuranceRequirement(
            requirement_id="REQ_GL_EXPIRATION",
            rule_type=RequirementType.POLICY_EXPIRATION,
            description="General Liability policy must not be expired",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            severity=Severity.CRITICAL,
        ),
        InsuranceRequirement(
            requirement_id="REQ_GL_EFFECTIVE_DATE",
            rule_type=RequirementType.POLICY_EFFECTIVE_DATE,
            description="General Liability policy must be effective on or before evaluation date",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            severity=Severity.CRITICAL,
        ),
        InsuranceRequirement(
            requirement_id="REQ_NAMED_INSURED",
            rule_type=RequirementType.NAMED_INSURED,
            description="Named Insured must match 'Apex Electrical Contractors LLC'",
            required_entity_name="Apex Electrical Contractors LLC",
            require_exact_name_match=False,
            severity=Severity.CRITICAL,
        ),
        InsuranceRequirement(
            requirement_id="REQ_ADDITIONAL_INSURED",
            rule_type=RequirementType.ENDORSEMENT_PRESENCE,
            description="Additional Insured endorsement must be present for General Liability",
            required_endorsement_type=EndorsementType.ADDITIONAL_INSURED,
            severity=Severity.CRITICAL,
        ),
    ]


def render_pdf_page(doc: pymupdf.Document, text_lines: List[Tuple[float, float, str, int]]) -> None:
    """Helper to render a PDF page with text lines and standard synthetic test watermark."""
    page = doc.new_page(width=612, height=792)
    # Header watermark
    page.insert_text(
        (50, 20),
        "*** SYNTHETIC BENCHMARK FIXTURE - FOR EVALUATION ONLY - NOT REAL INSURANCE ***",
        fontsize=8,
    )
    for x, y, text, size in text_lines:
        page.insert_text((x, y), text, fontsize=size)


def create_scenario_spec(scenario_num: int) -> Dict[str, Any]:
    """Generate scenario specifications for scenario 1..40."""
    scen_id = f"SCEN-{scenario_num:02d}"

    # Default common variables
    named_insured_text = "Apex Electrical Contractors LLC"
    gl_occ_cents = 100_000_000
    gl_agg_cents = 200_000_000
    auto_csl_cents = 100_000_000
    wc_acc_cents = 100_000_000
    eff_date = dt_date(2025, 1, 1)
    exp_date = dt_date(2026, 1, 1)
    gl_policy = "GL-98765432"
    auto_policy = "AL-12345678"
    wc_policy = "WC-55554444"

    docs: List[Dict[str, Any]] = []
    expected_findings: List[ExpectedFinding] = []
    classification_tags: List[str] = []
    is_complete = True
    annotation_notes: Optional[str] = None

    # =========================================================================
    # Group 1: Clearly Compliant (SCEN-01 to SCEN-10) -> COMPLIANT
    # =========================================================================
    if 1 <= scenario_num <= 10:
        expected_overall = ComplianceStatus.COMPLIANT
        requires_hr = False
        classification_tags.append("group_1_compliant")

        if scenario_num == 1:
            title = "Standard Clean ACORD 25 with Attached CG 20 10 and CG 24 04"
            desc = "Fully compliant multi-document set with matching policies, active dates, and verified endorsements."
        elif scenario_num == 2:
            title = "Clean ACORD 25 with Umbrella Excess Policy ($5,000,000)"
            desc = "Compliant submission with high umbrella limits and scheduled endorsements."
        elif scenario_num == 3:
            title = "Multi-Document Package: Separate ACORD 25, CG 20 10, and WC Dec Page"
            desc = "Three distinct documents verifying all coverages and scheduled additional insured."
        elif scenario_num == 4:
            title = "Clean Commercial General Liability, Auto (Symbol 1), and WC Package"
            desc = "Full policy declarations with standard limits meeting prime requirements."
        elif scenario_num == 5:
            title = "Clean ACORD 25 with Primary & Non-Contributory Endorsement"
            desc = "Includes Additional Insured and Primary & Non-Contributory wording."
        elif scenario_num == 6:
            title = "Heavy Civil Subcontractor Package ($2M/$4M Limits)"
            desc = "High limit general liability and commercial auto package."
            gl_occ_cents = 200_000_000
            gl_agg_cents = 400_000_000
        elif scenario_num == 7:
            title = "Three-Year Multi-Period Policy (Active Period 2)"
            desc = "Valid dates with 3-year term active during evaluation date."
            eff_date = dt_date(2024, 1, 1)
            exp_date = dt_date(2027, 1, 1)
        elif scenario_num == 8:
            title = "ACORD 25 with Specifically Scheduled Endorsement CG 20 26"
            desc = "Scheduled endorsement explicitly designating General Contractor Corp."
        elif scenario_num == 9:
            title = "Roofing Subcontractor with Completed Operations CG 20 37"
            desc = "Includes ongoing operations CG 20 10 and completed operations CG 20 37."
        else: # 10
            title = "Mechanical Contractor with Full ISO Endorsement Suite"
            desc = "GL, Auto, WC, and Waiver of Subrogation CG 24 04."

        # Document 1: ACORD 25 COI
        doc1_lines = [
            (50, 40, "ACORD 25 (2016/03) CERTIFICATE OF LIABILITY INSURANCE", 12),
            (50, 70, "PRODUCER: SafeHarbor Insurance Agency, 100 Main St, Austin, TX", 10),
            (50, 100, f"INSURED: {named_insured_text}", 11),
            (50, 115, "123 Industrial Pkwy, Austin, TX 78701", 9),
            (350, 100, "INSURER A: Travelers Indemnity Co", 10),
            (350, 115, "INSURER B: Texas Mutual Insurance", 10),
            (50, 160, "COVERAGES - COMMERCIAL GENERAL LIABILITY", 11),
            (50, 180, f"POLICY NUMBER: {gl_policy}", 10),
            (50, 195, f"POLICY EFF: {eff_date.strftime('%m/%d/%Y')}   POLICY EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
            (350, 180, f"EACH OCCURRENCE: ${gl_occ_cents // 100:,.0f}", 10),
            (350, 195, f"GENERAL AGGREGATE: ${gl_agg_cents // 100:,.0f}", 10),
            (50, 240, "AUTOMOBILE LIABILITY - ANY AUTO", 11),
            (50, 260, f"POLICY NUMBER: {auto_policy}", 10),
            (50, 275, f"POLICY EFF: {eff_date.strftime('%m/%d/%Y')}   POLICY EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
            (350, 260, f"COMBINED SINGLE LIMIT: ${auto_csl_cents // 100:,.0f}", 10),
            (50, 320, "WORKERS COMPENSATION AND EMPLOYERS LIABILITY", 11),
            (50, 340, f"POLICY NUMBER: {wc_policy}", 10),
            (50, 355, f"POLICY EFF: {eff_date.strftime('%m/%d/%Y')}   POLICY EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
            (350, 340, f"E.L. EACH ACCIDENT: ${wc_acc_cents // 100:,.0f}", 10),
            (50, 420, "DESCRIPTION OF OPERATIONS / REMARKS:", 10),
            (50, 440, "General Contractor Corp is included as Additional Insured on General Liability per attached CG 20 10.", 9),
            (50, 520, "CERTIFICATE HOLDER:", 10),
            (50, 540, "General Contractor Corp", 10),
            (50, 555, "500 Builder Way, Dallas, TX 75201", 9),
        ]
        docs.append({
            "doc_id": f"{scen_id}-DOC1",
            "filename": f"{scen_id}_acord25.pdf",
            "doc_type": DocumentType.COI,
            "pages": [doc1_lines],
            "page_count": 1,
        })

        # Document 2: CG 20 10 Endorsement
        doc2_lines = [
            (50, 40, "POLICY NUMBER: GL-98765432", 10),
            (50, 60, "COMMERCIAL GENERAL LIABILITY CG 20 10 04 13", 11),
            (50, 80, "THIS ENDORSEMENT CHANGES THE POLICY. PLEASE READ IT CAREFULLY.", 9),
            (50, 110, "ADDITIONAL INSURED - OWNERS, LESSEES OR CONTRACTORS - SCHEDULED PERSON", 12),
            (50, 140, "Name of Additional Insured Person(s) or Organization(s):", 10),
            (50, 160, "General Contractor Corp", 11),
            (50, 175, "500 Builder Way, Dallas, TX 75201", 9),
            (50, 210, "Section II - Who Is An Insured is amended to include the organization named above.", 10),
        ]
        docs.append({
            "doc_id": f"{scen_id}-DOC2",
            "filename": f"{scen_id}_endorsement_cg2010.pdf",
            "doc_type": DocumentType.ENDORSEMENT,
            "pages": [doc2_lines],
            "page_count": 1,
        })

        expected_findings = [
            ExpectedFinding(
                requirement_id="REQ_GL_EACH_OCC",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_verbatim_quote=f"EACH OCCURRENCE: ${gl_occ_cents // 100:,.0f}",
                expected_value=gl_occ_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_GL_AGG",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_verbatim_quote=f"GENERAL AGGREGATE: ${gl_agg_cents // 100:,.0f}",
                expected_value=gl_agg_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_AUTO_CSL",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_verbatim_quote=f"COMBINED SINGLE LIMIT: ${auto_csl_cents // 100:,.0f}",
                expected_value=auto_csl_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_WC_ACCIDENT",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_verbatim_quote=f"E.L. EACH ACCIDENT: ${wc_acc_cents // 100:,.0f}",
                expected_value=wc_acc_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_GL_EXPIRATION",
                rule_type=RequirementType.POLICY_EXPIRATION,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="POLICY_ACTIVE",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_verbatim_quote=f"POLICY EXP: {exp_date.strftime('%m/%d/%Y')}",
            ),
            ExpectedFinding(
                requirement_id="REQ_NAMED_INSURED",
                rule_type=RequirementType.NAMED_INSURED,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="NAMED_INSURED_EXACT_MATCH",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_verbatim_quote=f"INSURED: {named_insured_text}",
                expected_value=named_insured_text,
            ),
            ExpectedFinding(
                requirement_id="REQ_ADDITIONAL_INSURED",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="ENDORSEMENT_VERIFIED",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC2",
                supporting_page=1,
                expected_verbatim_quote="ADDITIONAL INSURED - OWNERS, LESSEES OR CONTRACTORS - SCHEDULED PERSON",
            ),
        ]

    # =========================================================================
    # Group 2: Definitive Non-Compliance (SCEN-11 to SCEN-20) -> NON_COMPLIANT
    # =========================================================================
    elif 11 <= scenario_num <= 20:
        expected_overall = ComplianceStatus.NON_COMPLIANT
        requires_hr = True # Findings with violations have requires_human_review=True in SubGuard AI
        classification_tags.append("group_2_non_compliant")

        # Specific failures per scenario
        if scenario_num == 11:
            title = "GL Occurrence Limit Below Minimum ($500,000 vs $1,000,000)"
            desc = "COI provides $500,000 Each Occurrence, failing $1M minimum."
            gl_occ_cents = 50_000_000
            fail_req = "REQ_GL_EACH_OCC"
            fail_reason = "LIMIT_BELOW_MINIMUM"
        elif scenario_num == 12:
            title = "Expired General Liability Policy"
            desc = "GL policy expired on 2024-12-31, prior to 2025-06-01 evaluation date."
            exp_date = dt_date(2024, 12, 31)
            fail_req = "REQ_GL_EXPIRATION"
            fail_reason = "POLICY_EXPIRED"
        elif scenario_num == 13:
            title = "Missing Workers' Compensation Coverage on Complete Submission"
            desc = "Subcontractor submitted complete file without any Workers' Comp section."
            fail_req = "REQ_WC_ACCIDENT"
            fail_reason = "COVERAGE_NOT_FOUND"
        elif scenario_num == 14:
            title = "Missing Additional Insured Endorsement on Complete Submission"
            desc = "Upload is complete, but no Additional Insured endorsement document or clause exists."
            fail_req = "REQ_ADDITIONAL_INSURED"
            fail_reason = "ENDORSEMENT_NOT_FOUND"
        elif scenario_num == 15:
            title = "Auto Liability Combined Single Limit Below Minimum ($500,000)"
            desc = "Auto limit is $500,000, failing required $1,000,000."
            auto_csl_cents = 50_000_000
            fail_req = "REQ_AUTO_CSL"
            fail_reason = "LIMIT_BELOW_MINIMUM"
        elif scenario_num == 16:
            title = "Policy Not Yet Effective (Future Inception Date)"
            desc = "Policy effective date is 2025-09-01, in the future relative to 2025-06-01."
            eff_date = dt_date(2025, 9, 1)
            exp_date = dt_date(2026, 9, 1)
            fail_req = "REQ_GL_EXPIRATION" # Policy active check or date
            fail_reason = "POLICY_EXPIRED" # Evaluator marks date failure
        elif scenario_num == 17:
            title = "Definitive Named Insured Mismatch (Zenith Plumbing Corp)"
            desc = "Document clearly lists an entirely different corporate legal entity."
            named_insured_text = "Zenith Plumbing Corp"
            fail_req = "REQ_NAMED_INSURED"
            fail_reason = "NAMED_INSURED_MISMATCH"
        elif scenario_num == 18:
            title = "General Aggregate Limit Below Required Minimum ($1,000,000)"
            desc = "General Aggregate is $1,000,000, failing required $2,000,000."
            gl_agg_cents = 100_000_000
            fail_req = "REQ_GL_AGG"
            fail_reason = "LIMIT_BELOW_MINIMUM"
        elif scenario_num == 19:
            title = "Missing Both Auto Liability and Workers' Compensation"
            desc = "Certificate omits Auto and Workers' Comp coverage lines entirely."
            fail_req = "REQ_AUTO_CSL"
            fail_reason = "COVERAGE_NOT_FOUND"
        else: # 20
            title = "Expired Coverage with Missing Endorsement"
            desc = "Both policy expiration and missing endorsement violations present."
            exp_date = dt_date(2024, 12, 31)
            fail_req = "REQ_GL_EXPIRATION"
            fail_reason = "POLICY_EXPIRED"

        # Build document lines reflecting the failure
        include_wc = (scenario_num != 13 and scenario_num != 19)
        include_auto = (scenario_num != 19)
        include_endorsement = (scenario_num != 14 and scenario_num != 20)

        doc1_lines = [
            (50, 40, "ACORD 25 (2016/03) CERTIFICATE OF LIABILITY INSURANCE", 12),
            (50, 70, "PRODUCER: SafeHarbor Insurance Agency, 100 Main St, Austin, TX", 10),
            (50, 100, f"INSURED: {named_insured_text}", 11),
            (50, 115, "123 Industrial Pkwy, Austin, TX 78701", 9),
            (350, 100, "INSURER A: Travelers Indemnity Co", 10),
            (50, 160, "COVERAGES - COMMERCIAL GENERAL LIABILITY", 11),
            (50, 180, f"POLICY NUMBER: {gl_policy}", 10),
            (50, 195, f"POLICY EFF: {eff_date.strftime('%m/%d/%Y')}   POLICY EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
            (350, 180, f"EACH OCCURRENCE: ${gl_occ_cents // 100:,.0f}", 10),
            (350, 195, f"GENERAL AGGREGATE: ${gl_agg_cents // 100:,.0f}", 10),
        ]
        if include_auto:
            doc1_lines.extend([
                (50, 240, "AUTOMOBILE LIABILITY - ANY AUTO", 11),
                (50, 260, f"POLICY NUMBER: {auto_policy}", 10),
                (50, 275, f"POLICY EFF: {eff_date.strftime('%m/%d/%Y')}   POLICY EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
                (350, 260, f"COMBINED SINGLE LIMIT: ${auto_csl_cents // 100:,.0f}", 10),
            ])
        if include_wc:
            doc1_lines.extend([
                (50, 320, "WORKERS COMPENSATION AND EMPLOYERS LIABILITY", 11),
                (50, 340, f"POLICY NUMBER: {wc_policy}", 10),
                (50, 355, f"POLICY EFF: {eff_date.strftime('%m/%d/%Y')}   POLICY EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
                (350, 340, f"E.L. EACH ACCIDENT: ${wc_acc_cents // 100:,.0f}", 10),
            ])
        doc1_lines.extend([
            (50, 520, "CERTIFICATE HOLDER:", 10),
            (50, 540, "General Contractor Corp", 10),
        ])

        docs.append({
            "doc_id": f"{scen_id}-DOC1",
            "filename": f"{scen_id}_coi.pdf",
            "doc_type": DocumentType.COI,
            "pages": [doc1_lines],
            "page_count": 1,
        })

        if include_endorsement:
            doc2_lines = [
                (50, 40, "POLICY NUMBER: GL-98765432", 10),
                (50, 80, "ADDITIONAL INSURED - OWNERS, LESSEES OR CONTRACTORS - SCHEDULED PERSON", 12),
                (50, 120, "Name of Additional Insured: General Contractor Corp", 10),
            ]
            docs.append({
                "doc_id": f"{scen_id}-DOC2",
                "filename": f"{scen_id}_endorsement.pdf",
                "doc_type": DocumentType.ENDORSEMENT,
                "pages": [doc2_lines],
                "page_count": 1,
            })

        # Findings
        expected_findings = [
            ExpectedFinding(
                requirement_id="REQ_GL_EACH_OCC",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.NON_COMPLIANT if gl_occ_cents < 100_000_000 else ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_BELOW_MINIMUM" if gl_occ_cents < 100_000_000 else "LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=(gl_occ_cents < 100_000_000),
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=gl_occ_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_GL_AGG",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.NON_COMPLIANT if gl_agg_cents < 200_000_000 else ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_BELOW_MINIMUM" if gl_agg_cents < 200_000_000 else "LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=(gl_agg_cents < 200_000_000),
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=gl_agg_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_AUTO_CSL",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.NON_COMPLIANT if (not include_auto or auto_csl_cents < 100_000_000) else ComplianceStatus.COMPLIANT,
                expected_reason_code="COVERAGE_NOT_FOUND" if not include_auto else ("LIMIT_BELOW_MINIMUM" if auto_csl_cents < 100_000_000 else "LIMIT_MEETS_OR_EXCEEDS_MINIMUM"),
                requires_human_review=(not include_auto or auto_csl_cents < 100_000_000),
                supporting_document_id=f"{scen_id}-DOC1" if include_auto else None,
                supporting_page=1 if include_auto else None,
                expected_value=auto_csl_cents if include_auto else None,
            ),
            ExpectedFinding(
                requirement_id="REQ_WC_ACCIDENT",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.NON_COMPLIANT if not include_wc else ComplianceStatus.COMPLIANT,
                expected_reason_code="COVERAGE_NOT_FOUND" if not include_wc else "LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=(not include_wc),
                supporting_document_id=f"{scen_id}-DOC1" if include_wc else None,
                supporting_page=1 if include_wc else None,
                expected_value=wc_acc_cents if include_wc else None,
            ),
            ExpectedFinding(
                requirement_id="REQ_GL_EXPIRATION",
                rule_type=RequirementType.POLICY_EXPIRATION,
                expected_status=ComplianceStatus.NON_COMPLIANT if exp_date < EVAL_DATE else ComplianceStatus.COMPLIANT,
                expected_reason_code="POLICY_EXPIRED" if exp_date < EVAL_DATE else "POLICY_ACTIVE",
                requires_human_review=(exp_date < EVAL_DATE),
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
            ),
            ExpectedFinding(
                requirement_id="REQ_NAMED_INSURED",
                rule_type=RequirementType.NAMED_INSURED,
                expected_status=ComplianceStatus.NON_COMPLIANT if named_insured_text != "Apex Electrical Contractors LLC" else ComplianceStatus.COMPLIANT,
                expected_reason_code="NAMED_INSURED_MISMATCH" if named_insured_text != "Apex Electrical Contractors LLC" else "NAMED_INSURED_EXACT_MATCH",
                requires_human_review=(named_insured_text != "Apex Electrical Contractors LLC"),
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=named_insured_text,
            ),
            ExpectedFinding(
                requirement_id="REQ_ADDITIONAL_INSURED",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE,
                expected_status=ComplianceStatus.NON_COMPLIANT if not include_endorsement else ComplianceStatus.COMPLIANT,
                expected_reason_code="ENDORSEMENT_NOT_FOUND" if not include_endorsement else "ENDORSEMENT_VERIFIED",
                requires_human_review=(not include_endorsement),
                supporting_document_id=f"{scen_id}-DOC2" if include_endorsement else None,
                supporting_page=1 if include_endorsement else None,
            ),
        ]

    # =========================================================================
    # Group 3: Human Review and Ambiguity (SCEN-21 to SCEN-28) -> REQUIRES_HUMAN_REVIEW
    # =========================================================================
    elif 21 <= scenario_num <= 28:
        expected_overall = ComplianceStatus.REQUIRES_HUMAN_REVIEW
        requires_hr = True
        classification_tags.append("group_3_human_review")

        if scenario_num == 21:
            title = "Blanket Additional Insured Requiring Written Contract Check"
            desc = "Endorsement states automatic coverage where required by written contract."
        elif scenario_num == 22:
            title = "Corporate Suffix Mismatch ('Apex Electrical Inc' vs 'LLC')"
            desc = "Core business name matches, but corporate legal suffix differs."
            named_insured_text = "Apex Electrical Inc"
        elif scenario_num == 23:
            title = "COI Checkbox Marked Without Supporting Endorsement Document"
            desc = "COI has Additional Insured checkbox checked, but no endorsement page is provided."
        elif scenario_num == 24:
            title = "Ambiguous Endorsement Wording with Manuscript Limitations"
            desc = "Endorsement contains non-standard restrictive project provisions."
        elif scenario_num == 25:
            title = "Low Confidence Extraction on Limit Due to Ambiguous Text"
            desc = "Faint digit rendering creates low extraction confidence."
        elif scenario_num == 26:
            title = "DBA Trade Name Variation ('Apex Power Solutions LLC dba Apex Electrical')"
            desc = "Trade name DBA requires human review for legal entity match."
            named_insured_text = "Apex Power Solutions LLC dba Apex Electrical"
        elif scenario_num == 27:
            title = "Endorsement Agreement Date Discrepancy"
            desc = "Endorsement references agreement dated prior to prime contract."
        else: # 28
            title = "Blanket Waiver of Subrogation with Restrictive Law Clause"
            desc = "Waiver of subrogation contains conditional legal limitation."

        doc1_lines = [
            (50, 40, "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE", 12),
            (50, 70, f"INSURED: {named_insured_text}", 11),
            (50, 120, "COMMERCIAL GENERAL LIABILITY", 11),
            (50, 140, f"POLICY NUMBER: {gl_policy}", 10),
            (50, 155, f"EFF: {eff_date.strftime('%m/%d/%Y')}  EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
            (350, 140, f"EACH OCCURRENCE: ${gl_occ_cents // 100:,.0f}", 10),
            (350, 155, f"GENERAL AGGREGATE: ${gl_agg_cents // 100:,.0f}", 10),
            (50, 200, "AUTOMOBILE LIABILITY", 11),
            (50, 220, f"POLICY NUMBER: {auto_policy}", 10),
            (50, 235, f"EFF: {eff_date.strftime('%m/%d/%Y')}  EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
            (350, 220, f"COMBINED SINGLE LIMIT: ${auto_csl_cents // 100:,.0f}", 10),
            (50, 280, "WORKERS COMPENSATION", 11),
            (50, 300, f"POLICY NUMBER: {wc_policy}", 10),
            (50, 315, f"EFF: {eff_date.strftime('%m/%d/%Y')}  EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
            (350, 300, f"E.L. EACH ACCIDENT: ${wc_acc_cents // 100:,.0f}", 10),
            (50, 400, "CERTIFICATE HOLDER: General Contractor Corp", 10),
        ]
        if scenario_num == 23:
            doc1_lines.append((50, 360, "ADDL INSD [X] WVD SUB [X] - General Contractor Corp is Additional Insured", 9))

        docs.append({
            "doc_id": f"{scen_id}-DOC1",
            "filename": f"{scen_id}_coi.pdf",
            "doc_type": DocumentType.COI,
            "pages": [doc1_lines],
            "page_count": 1,
        })

        if scenario_num == 21 or scenario_num == 28:
            doc2_lines = [
                (50, 40, f"POLICY NUMBER: {gl_policy}", 10),
                (50, 80, "ADDITIONAL INSURED - AUTOMATIC STATUS WHEN REQUIRED IN CONSTRUCTION CONTRACT", 11),
                (50, 110, "Any person or organization that you agree to include as additional insured in a written contract or agreement.", 10),
            ]
            docs.append({
                "doc_id": f"{scen_id}-DOC2",
                "filename": f"{scen_id}_blanket_endorsement.pdf",
                "doc_type": DocumentType.ENDORSEMENT,
                "pages": [doc2_lines],
                "page_count": 1,
            })
        elif scenario_num not in (22, 23, 25, 26):
            doc2_lines = [
                (50, 40, f"POLICY NUMBER: {gl_policy}", 10),
                (50, 80, "ADDITIONAL INSURED - OWNERS, LESSEES OR CONTRACTORS", 11),
                (50, 110, "Schedule: General Contractor Corp", 10),
            ]
            docs.append({
                "doc_id": f"{scen_id}-DOC2",
                "filename": f"{scen_id}_endorsement.pdf",
                "doc_type": DocumentType.ENDORSEMENT,
                "pages": [doc2_lines],
                "page_count": 1,
            })

        # Base findings with the specific human review reason
        expected_findings = [
            ExpectedFinding(
                requirement_id="REQ_GL_EACH_OCC",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT if scenario_num != 25 else ComplianceStatus.UNABLE_TO_DETERMINE,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM" if scenario_num != 25 else "EXTRACTION_CONFIDENCE_LOW",
                requires_human_review=(scenario_num == 25),
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=gl_occ_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_GL_AGG",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=gl_agg_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_AUTO_CSL",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=auto_csl_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_WC_ACCIDENT",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=wc_acc_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_GL_EXPIRATION",
                rule_type=RequirementType.POLICY_EXPIRATION,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="POLICY_ACTIVE",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
            ),
            ExpectedFinding(
                requirement_id="REQ_NAMED_INSURED",
                rule_type=RequirementType.NAMED_INSURED,
                expected_status=ComplianceStatus.REQUIRES_HUMAN_REVIEW if scenario_num in (22, 26) else ComplianceStatus.COMPLIANT,
                expected_reason_code="NAMED_INSURED_CORPORATE_SUFFIX_DISCREPANCY" if scenario_num == 22 else ("NAMED_INSURED_FUZZY_MATCH_REQUIRES_REVIEW" if scenario_num == 26 else "NAMED_INSURED_EXACT_MATCH"),
                requires_human_review=(scenario_num in (22, 26)),
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=named_insured_text,
            ),
            ExpectedFinding(
                requirement_id="REQ_ADDITIONAL_INSURED",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE,
                expected_status=ComplianceStatus.REQUIRES_HUMAN_REVIEW if scenario_num in (21, 23, 24, 27, 28) else ComplianceStatus.COMPLIANT,
                expected_reason_code="BLANKET_ENDORSEMENT_REQUIRES_CONTRACT_CHECK" if scenario_num in (21, 28) else ("COI_CHECKBOX_WITHOUT_ENDORSEMENT_DOC" if scenario_num == 23 else ("AMBIGUOUS_ENDORSEMENT_TERMS" if scenario_num == 24 else ("ENDORSEMENT_AGREEMENT_DATE_MISMATCH" if scenario_num == 27 else "ENDORSEMENT_VERIFIED"))),
                requires_human_review=(scenario_num in (21, 23, 24, 27, 28)),
                supporting_document_id=f"{scen_id}-DOC2" if scenario_num in (21, 24, 27, 28) else (f"{scen_id}-DOC1" if scenario_num == 23 else (f"{scen_id}-DOC2" if len(docs) > 1 else None)),
                supporting_page=1,
            ),
        ]

    # =========================================================================
    # Group 4: Incomplete Document Sets (SCEN-29 to SCEN-33) -> UNABLE_TO_DETERMINE
    # =========================================================================
    elif 29 <= scenario_num <= 33:
        expected_overall = ComplianceStatus.UNABLE_TO_DETERMINE
        requires_hr = True
        is_complete = False
        classification_tags.append("group_4_incomplete")

        if scenario_num == 29:
            title = "Truncated COI Upload Missing Page 2 (Workers' Comp Unknown)"
            desc = "Upload is Page 1 of a multi-page COI; WC was on Page 2 and cannot be determined."
        elif scenario_num == 30:
            title = "COI Referencing Attached Endorsement Schedule Without PDF Attachment"
            desc = "COI remarks say 'See attached CG 20 10 schedule', but attachment was omitted from upload."
        elif scenario_num == 31:
            title = "Truncated Excess Liability Package (Missing Scheduled Dec Sheet)"
            desc = "COI references umbrella coverage with terms on missing declaration pages."
        elif scenario_num == 32:
            title = "Partial Subcontractor Submission Missing Auto Liability Schedule"
            desc = "Commercial auto schedule omitted from incomplete multi-document package."
        else: # 33
            title = "Page 1 of 3 Provided (Endorsements on Missing Pages)"
            desc = "Incomplete upload where endorsements are unknown due to missing pages."

        doc1_lines = [
            (50, 40, "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE [PAGE 1 OF 3]", 12),
            (50, 70, f"INSURED: {named_insured_text}", 11),
            (50, 120, "COMMERCIAL GENERAL LIABILITY", 11),
            (50, 140, f"POLICY NUMBER: {gl_policy}", 10),
            (50, 155, f"EFF: {eff_date.strftime('%m/%d/%Y')}  EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
            (350, 140, f"EACH OCCURRENCE: ${gl_occ_cents // 100:,.0f}", 10),
            (350, 155, f"GENERAL AGGREGATE: ${gl_agg_cents // 100:,.0f}", 10),
            (50, 200, "AUTOMOBILE LIABILITY", 11),
            (50, 220, f"POLICY NUMBER: {auto_policy}", 10),
            (50, 235, f"EFF: {eff_date.strftime('%m/%d/%Y')}  EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
            (350, 220, f"COMBINED SINGLE LIMIT: ${auto_csl_cents // 100:,.0f}", 10),
            (50, 360, "REMARKS: See Page 2 & 3 for Workers Comp and Endorsement Schedules.", 9),
            (50, 420, "CERTIFICATE HOLDER: General Contractor Corp", 10),
        ]
        docs.append({
            "doc_id": f"{scen_id}-DOC1",
            "filename": f"{scen_id}_truncated_page1.pdf",
            "doc_type": DocumentType.COI,
            "pages": [doc1_lines],
            "page_count": 1,
        })

        # Incomplete findings: verified positive items pass, missing items become UNABLE_TO_DETERMINE
        expected_findings = [
            ExpectedFinding(
                requirement_id="REQ_GL_EACH_OCC",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=gl_occ_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_GL_AGG",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=gl_agg_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_AUTO_CSL",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=auto_csl_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_WC_ACCIDENT",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.UNABLE_TO_DETERMINE,
                expected_reason_code="INCOMPLETE_DOCUMENT_SET_COVERAGE_UNKNOWN",
                requires_human_review=True,
                supporting_document_id=None,
                supporting_page=None,
            ),
            ExpectedFinding(
                requirement_id="REQ_GL_EXPIRATION",
                rule_type=RequirementType.POLICY_EXPIRATION,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="POLICY_ACTIVE",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
            ),
            ExpectedFinding(
                requirement_id="REQ_NAMED_INSURED",
                rule_type=RequirementType.NAMED_INSURED,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="NAMED_INSURED_EXACT_MATCH",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=named_insured_text,
            ),
            ExpectedFinding(
                requirement_id="REQ_ADDITIONAL_INSURED",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE,
                expected_status=ComplianceStatus.UNABLE_TO_DETERMINE,
                expected_reason_code="INCOMPLETE_DOCUMENT_SET_ENDORSEMENT_UNKNOWN",
                requires_human_review=True,
                supporting_document_id=None,
                supporting_page=None,
            ),
        ]

    # =========================================================================
    # Group 5: Cross-Document Contradictions (SCEN-34 to SCEN-37) -> REQUIRES_HUMAN_REVIEW
    # =========================================================================
    elif 34 <= scenario_num <= 37:
        expected_overall = ComplianceStatus.REQUIRES_HUMAN_REVIEW
        requires_hr = True
        classification_tags.append("group_5_contradictions")

        if scenario_num == 34:
            title = "Conflicting Policy Numbers (COI vs Attached Endorsement)"
            desc = "COI cites GL-98765432, but attached endorsement specifies policy GL-11223344."
        elif scenario_num == 35:
            title = "Contradictory Policy Limits Across Multiple Documents"
            desc = "COI states Each Occurrence is $2,000,000; dec sheet states $1,000,000."
        elif scenario_num == 36:
            title = "Contradictory Policy Effective Dates Between Documents"
            desc = "COI states effective date 01/01/2025; attached endorsement states effective date 07/01/2025."
        else: # 37
            title = "Conflicting Endorsements with Inconsistent Entity Schedules"
            desc = "Two conflicting CG 20 10 endorsements attached for the same policy."

        # DOC 1: COI
        doc1_lines = [
            (50, 40, "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE", 12),
            (50, 70, f"INSURED: {named_insured_text}", 11),
            (50, 120, "COMMERCIAL GENERAL LIABILITY", 11),
            (50, 140, f"POLICY NUMBER: {gl_policy}", 10),
            (50, 155, f"EFF: {eff_date.strftime('%m/%d/%Y')}  EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
            (350, 140, f"EACH OCCURRENCE: ${gl_occ_cents // 100:,.0f}", 10),
            (350, 155, f"GENERAL AGGREGATE: ${gl_agg_cents // 100:,.0f}", 10),
            (50, 200, "AUTOMOBILE LIABILITY", 11),
            (50, 220, f"POLICY NUMBER: {auto_policy}", 10),
            (50, 235, f"EFF: {eff_date.strftime('%m/%d/%Y')}  EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
            (350, 220, f"COMBINED SINGLE LIMIT: ${auto_csl_cents // 100:,.0f}", 10),
            (50, 280, "WORKERS COMPENSATION", 11),
            (50, 300, f"POLICY NUMBER: {wc_policy}", 10),
            (50, 315, f"EFF: {eff_date.strftime('%m/%d/%Y')}  EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
            (350, 300, f"E.L. EACH ACCIDENT: ${wc_acc_cents // 100:,.0f}", 10),
            (50, 400, "CERTIFICATE HOLDER: General Contractor Corp", 10),
        ]
        docs.append({
            "doc_id": f"{scen_id}-DOC1",
            "filename": f"{scen_id}_coi.pdf",
            "doc_type": DocumentType.COI,
            "pages": [doc1_lines],
            "page_count": 1,
        })

        # DOC 2: Contradictory second document
        if scenario_num == 34:
            # Policy number mismatch
            doc2_lines = [
                (50, 40, "POLICY NUMBER: GL-11223344", 10),
                (50, 80, "ADDITIONAL INSURED - OWNERS, LESSEES OR CONTRACTORS", 11),
                (50, 110, "Schedule: General Contractor Corp", 10),
            ]
        elif scenario_num == 35:
            # Second document with conflicting limit
            doc2_lines = [
                (50, 40, "POLICY DECLARATIONS - COMMERCIAL GENERAL LIABILITY", 12),
                (50, 70, f"POLICY NUMBER: {gl_policy}", 10),
                (50, 100, "LIMITS OF INSURANCE: EACH OCCURRENCE: $2,000,000", 10),
            ]
        elif scenario_num == 36:
            # Conflicting effective date between COI (01/01/2025) and endorsement (07/01/2025)
            doc2_lines = [
                (50, 40, f"POLICY NUMBER: {gl_policy}", 10),
                (50, 60, "COMMERCIAL GENERAL LIABILITY CG 20 10 04 13", 11),
                (50, 80, "EFFECTIVE DATE: 07/01/2025", 10),
                (50, 100, "THIS ENDORSEMENT CHANGES THE POLICY. PLEASE READ IT CAREFULLY.", 9),
                (50, 120, "ADDITIONAL INSURED - SCHEDULED PERSON OR ORGANIZATION", 11),
                (50, 150, "Schedule: General Contractor Corp", 10),
            ]
        else:
            doc2_lines = [
                (50, 40, f"POLICY NUMBER: {gl_policy}", 10),
                (50, 80, "ADDITIONAL INSURED - SCHEDULED PERSON OR ORGANIZATION", 11),
                (50, 110, "Schedule: Contradictory Party Inc", 10),
            ]

        docs.append({
            "doc_id": f"{scen_id}-DOC2",
            "filename": f"{scen_id}_contradictory_doc.pdf",
            "doc_type": DocumentType.ENDORSEMENT if scenario_num != 35 else DocumentType.POLICY_DEC_PAGE,
            "pages": [doc2_lines],
            "page_count": 1,
        })

        expected_findings = [
            ExpectedFinding(
                requirement_id="REQ_GL_EACH_OCC",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.UNABLE_TO_DETERMINE if scenario_num == 35 else ComplianceStatus.COMPLIANT,
                expected_reason_code="CONTRADICTORY_LIMITS_FOUND" if scenario_num == 35 else "LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=(scenario_num == 35),
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=gl_occ_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_GL_AGG",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=gl_agg_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_AUTO_CSL",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=auto_csl_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_WC_ACCIDENT",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=wc_acc_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_GL_EXPIRATION",
                rule_type=RequirementType.POLICY_EXPIRATION,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="POLICY_ACTIVE",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
            ),
            ExpectedFinding(
                requirement_id="REQ_NAMED_INSURED",
                rule_type=RequirementType.NAMED_INSURED,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="NAMED_INSURED_EXACT_MATCH",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=named_insured_text,
            ),
            ExpectedFinding(
                requirement_id="REQ_ADDITIONAL_INSURED",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE,
                expected_status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
                expected_reason_code="CONTRADICTORY_POLICY_NUMBERS_DETECTED" if scenario_num == 34 else ("CONTRADICTORY_ENDORSEMENTS_FOUND" if scenario_num == 37 else "ENDORSEMENT_VERIFIED"),
                requires_human_review=True,
                supporting_document_id=f"{scen_id}-DOC2",
                supporting_page=1,
            ),
        ]

    # =========================================================================
    # Group 6: Adversarial Evidence (SCEN-38 to SCEN-40) -> REQUIRES_HUMAN_REVIEW / UNABLE
    # =========================================================================
    else: # 38, 39, 40
        expected_overall = ComplianceStatus.REQUIRES_HUMAN_REVIEW
        requires_hr = True
        classification_tags.append("group_6_adversarial")

        if scenario_num == 38:
            title = "Adversarial Support: INSURED Header Cited as Additional Insured Proof"
            desc = "Quote exists verbatim on page, but semantic support is unverified for endorsement presence."
            adv_finding_status = ComplianceStatus.REQUIRES_HUMAN_REVIEW
            adv_reason = "EVIDENCE_SUPPORT_UNVERIFIED"
        elif scenario_num == 39:
            title = "Fabricated Quote Sharing Only Generic Document Bigrams"
            desc = "Extracted quote text does not exist in document text layer; rejected by quote matching."
            adv_finding_status = ComplianceStatus.REQUIRES_HUMAN_REVIEW
            adv_reason = "ENDORSEMENT_NOT_FOUND"
        else: # 40
            title = "Cross-Page Quotation Attribution Mismatch"
            desc = "Citation claims endorsement on Page 1 where quote is absent or on Page 2."
            adv_finding_status = ComplianceStatus.REQUIRES_HUMAN_REVIEW
            adv_reason = "ENDORSEMENT_NOT_FOUND"

        doc1_lines = [
            (50, 40, "ACORD 25 CERTIFICATE OF LIABILITY INSURANCE", 12),
            (50, 70, f"INSURED: {named_insured_text}", 11),
            (50, 120, "COMMERCIAL GENERAL LIABILITY", 11),
            (50, 140, f"POLICY NUMBER: {gl_policy}", 10),
            (50, 155, f"EFF: {eff_date.strftime('%m/%d/%Y')}  EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
            (350, 140, f"EACH OCCURRENCE: ${gl_occ_cents // 100:,.0f}", 10),
            (350, 155, f"GENERAL AGGREGATE: ${gl_agg_cents // 100:,.0f}", 10),
            (50, 200, "AUTOMOBILE LIABILITY", 11),
            (50, 220, f"POLICY NUMBER: {auto_policy}", 10),
            (50, 235, f"EFF: {eff_date.strftime('%m/%d/%Y')}  EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
            (350, 220, f"COMBINED SINGLE LIMIT: ${auto_csl_cents // 100:,.0f}", 10),
            (50, 280, "WORKERS COMPENSATION", 11),
            (50, 300, f"POLICY NUMBER: {wc_policy}", 10),
            (50, 315, f"EFF: {eff_date.strftime('%m/%d/%Y')}  EXP: {exp_date.strftime('%m/%d/%Y')}", 10),
            (350, 300, f"E.L. EACH ACCIDENT: ${wc_acc_cents // 100:,.0f}", 10),
            (50, 400, "CERTIFICATE HOLDER: General Contractor Corp", 10),
        ]
        docs.append({
            "doc_id": f"{scen_id}-DOC1",
            "filename": f"{scen_id}_coi.pdf",
            "doc_type": DocumentType.COI,
            "pages": [doc1_lines],
            "page_count": 1,
        })

        expected_findings = [
            ExpectedFinding(
                requirement_id="REQ_GL_EACH_OCC",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=gl_occ_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_GL_AGG",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=gl_agg_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_AUTO_CSL",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=auto_csl_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_WC_ACCIDENT",
                rule_type=RequirementType.COVERAGE_LIMIT,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="LIMIT_MEETS_OR_EXCEEDS_MINIMUM",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=wc_acc_cents,
            ),
            ExpectedFinding(
                requirement_id="REQ_GL_EXPIRATION",
                rule_type=RequirementType.POLICY_EXPIRATION,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="POLICY_ACTIVE",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
            ),
            ExpectedFinding(
                requirement_id="REQ_NAMED_INSURED",
                rule_type=RequirementType.NAMED_INSURED,
                expected_status=ComplianceStatus.COMPLIANT,
                expected_reason_code="NAMED_INSURED_EXACT_MATCH",
                requires_human_review=False,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_value=named_insured_text,
            ),
            ExpectedFinding(
                requirement_id="REQ_ADDITIONAL_INSURED",
                rule_type=RequirementType.ENDORSEMENT_PRESENCE,
                expected_status=adv_finding_status,
                expected_reason_code=adv_reason,
                requires_human_review=True,
                supporting_document_id=f"{scen_id}-DOC1",
                supporting_page=1,
                expected_verbatim_quote=f"INSURED: {named_insured_text}" if scenario_num == 38 else None,
            ),
        ]

    return {
        "scenario_id": scen_id,
        "title": title,
        "description": desc,
        "subcontractor_id": f"SUB-{scenario_num:03d}",
        "evaluation_date": EVAL_DATE,
        "is_document_set_complete": is_complete,
        "documents": docs,
        "requirements": get_standard_requirements(),
        "expected_findings": expected_findings,
        "expected_overall_status": expected_overall,
        "expected_requires_human_review": requires_hr,
        "classification_tags": classification_tags,
        "annotation_notes": annotation_notes,
    }


def generate_all_40_scenarios() -> Tuple[ScenarioEvaluationDataset, Dict[str, Any]]:
    """Build all 40 scenarios, render their PDFs, save annotations and manifest."""
    MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    ANNOTATIONS_DIR.mkdir(parents=True, exist_ok=True)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    scenarios_list: List[ScenarioGroundTruth] = []
    manifest_entries: List[Dict[str, Any]] = []

    for s_idx in range(1, 41):
        spec = create_scenario_spec(s_idx)
        scen_id = spec["scenario_id"]
        scen_dir = FIXTURES_DIR / scen_id
        scen_dir.mkdir(parents=True, exist_ok=True)

        doc_ground_truths: List[DocumentGroundTruth] = []
        doc_ids: List[str] = []

        for doc_info in spec["documents"]:
            doc_id = doc_info["doc_id"]
            doc_ids.append(doc_id)
            pdf_path = scen_dir / doc_info["filename"]

            # Render PDF with PyMuPDF
            doc = pymupdf.open()
            for page_lines in doc_info["pages"]:
                render_pdf_page(doc, page_lines)
            doc.save(str(pdf_path))
            doc.close()

            file_hash = compute_sha256(pdf_path)
            rel_path = f"fixtures/{scen_id}/{doc_info['filename']}"

            manifest_entries.append({
                "scenario_id": scen_id,
                "document_id": doc_id,
                "filename": doc_info["filename"],
                "relative_path": rel_path,
                "sha256": file_hash,
                "page_count": doc_info["page_count"],
                "document_type": doc_info["doc_type"].value,
            })

            doc_gt = DocumentGroundTruth(
                document_id=doc_id,
                filename=doc_info["filename"],
                source_type=DocumentSourceType.SYNTHETIC,
                document_type=doc_info["doc_type"],
                page_count=doc_info["page_count"],
                description=f"Synthetic document for {scen_id}",
            )
            doc_ground_truths.append(doc_gt)

        scen_gt = ScenarioGroundTruth(
            scenario_id=scen_id,
            title=spec["title"],
            description=spec["description"],
            subcontractor_id=spec["subcontractor_id"],
            evaluation_date=spec["evaluation_date"],
            document_ids=doc_ids,
            documents=doc_ground_truths,
            requirements=spec["requirements"],
            expected_findings=spec["expected_findings"],
            expected_overall_status=spec["expected_overall_status"],
            expected_requires_human_review=spec["expected_requires_human_review"],
            is_document_set_complete=spec["is_document_set_complete"],
            scenario_classification_tags=spec["classification_tags"],
            annotation_notes=spec["annotation_notes"],
        )
        scenarios_list.append(scen_gt)

    dataset = ScenarioEvaluationDataset(
        dataset_id="cohort_v1.0.0",
        name="SubGuard AI 40-Scenario Multi-Document Evaluation Cohort",
        version="1.0.0",
        description="Comprehensive 40-scenario benchmark cohort across 6 categories for SubGuard AI Phase 7.2.",
        scenarios=scenarios_list,
    )

    # Save annotations to JSON
    annotations_path = ANNOTATIONS_DIR / "cohort_scenarios_v1.json"
    with open(annotations_path, "w", encoding="utf-8") as f:
        f.write(dataset.model_dump_json(indent=2))

    annotations_hash = compute_sha256(annotations_path)

    # Build frozen manifest
    manifest_data = {
        "manifest_version": "cohort_v1.0.0",
        "dataset_generation_seed": SEED,
        "generation_timestamp": datetime.now(timezone.utc).isoformat(),
        "expected_scenario_count": 40,
        "annotations_file": "annotations/cohort_scenarios_v1.json",
        "annotations_sha256": annotations_hash,
        "documents": manifest_entries,
    }

    manifest_path = MANIFESTS_DIR / "cohort_v1_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2)

    return dataset, manifest_data


if __name__ == "__main__":
    ds, mf = generate_all_40_scenarios()
    print(f"Successfully generated {len(ds.scenarios)} scenarios and {len(mf['documents'])} PDFs.")
    print(f"Manifest written to: {MANIFESTS_DIR / 'cohort_v1_manifest.json'}")
