"""Deterministic Cross-Document Reconciliation Logic.

Architectural Invariant:
'AI extracts information. Deterministic Python rules evaluate it. Humans resolve uncertainty.'

This module implements the deterministic reconciliation stage between document extraction
and compliance evaluation:
1. Entity identity reconciliation (independent of document roles)
2. Contractual role compatibility and contractual-role satisfaction
3. Policy identifier reconciliation (strict 5-tier classification)
4. Temporal date reconciliation with nullable active states
5. Multi-edge document relationship graph classification with relationship-specific evidence
6. Cross-document evidence and referential integrity validation
7. Deterministic summary derivation
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Dict, List, Optional, Set, Tuple

from app.compliance.evaluator import _clean_entity_name, _split_core_and_suffix, compare_entity_names
from app.schemas.insurance import (
    CoverageExtraction,
    CoverageType,
    DocumentType,
    EndorsementExtraction,
    EvidenceReference,
    ExtractedDocumentSet,
    InsuranceRequirement,
    RequirementType,
    Severity,
)
from app.schemas.reconciliation import (
    AMENDS_EVIDENCE_PATTERN,
    DECLARATION_EVIDENCE_PATTERN,
    DUPLICATE_EVIDENCE_PATTERN,
    ENDORSEMENT_EVIDENCE_PATTERN,
    POLICY_COVERAGE_EVIDENCE_PATTERN,
    REPLACES_EVIDENCE_PATTERN,
    CrossDocumentReconciliationResult,
    DateReconciliationRecord,
    DiscrepancyCategory,
    DocumentDiscrepancy,
    DocumentManifestEntry,
    DocumentRelationshipEdge,
    DocumentRelationshipType,
    EdgeClassification,
    EntityMatchClassification,
    EntityReconciliationRecord,
    EntityRole,
    GraphEvaluationStatus,
    MissingEvidenceProvenance,
    PolicyMatchClassification,
    PolicyReconciliationRecord,
    is_role_compatible,
    normalize_policy_identifier,
    validate_evidence_reference,
    _evidence_supports_entity,
    _validate_target_linkage,
)


# =============================================================================
# 1. Policy Number Reconciliation
# =============================================================================

RENEWAL_SUFFIX_REGEX = re.compile(
    r"[-_/](0[1-9]|[1-9][0-9]?|20[2-9][0-9]|R[0-9]+|RN[0-9]+|TERM[0-9]+)$",
    re.IGNORECASE,
)

def _is_possible_policy_family(raw_a: str, raw_b: str) -> bool:
    """Detect if two policy identifiers share a common base with a documented renewal pattern.
    
    Conservative policy-family classification (Correction 3):
    - Leading-zero differences alone do NOT infer family (they classify as CONFLICTING/AMBIGUOUS).
    - Arbitrary shared prefixes do NOT infer family.
    - Only documented renewal patterns (e.g. -01 vs -02, /2024 vs /2025, -R1 vs -R2, -TERM1 vs -TERM2)
      on an identical base qualify as POSSIBLE_FAMILY.
    - Arbitrary alphanumeric suffix differences without renewal evidence are rejected.
    """
    if not raw_a or not raw_b:
        return False

    a = raw_a.strip().upper()
    b = raw_b.strip().upper()

    if a == b:
        return False

    m_a = RENEWAL_SUFFIX_REGEX.search(a)
    m_b = RENEWAL_SUFFIX_REGEX.search(b)

    # Both have documented renewal suffixes sharing the identical base
    if m_a and m_b:
        base_a = a[: m_a.start()]
        base_b = b[: m_b.start()]
        if base_a == base_b and len(base_a) >= 3:
            return True

    # One has a renewal suffix, the other is the exact common base
    if m_a and not m_b:
        base_a = a[: m_a.start()]
        if base_a == b and len(base_a) >= 3:
            return True

    if m_b and not m_a:
        base_b = b[: m_b.start()]
        if base_b == a and len(base_b) >= 3:
            return True

    return False


def reconcile_policy_identifiers(
    coi_document_id: str,
    coi_policy_number: Optional[str],
    coi_evidence: Optional[EvidenceReference],
    endorsement_document_id: str,
    endorsement_policy_number: Optional[str],
    endorsement_evidence: Optional[EvidenceReference],
    evidence_chain: Optional[Tuple[EvidenceReference, ...]] = None,
) -> PolicyReconciliationRecord:
    r"""Deterministically reconcile policy numbers between a COI and an Endorsement.
    
    Enforces the strict 5-tier classification:
    - EXACT: character-for-character identical strings
    - FORMATTING_NORMALIZED: identical after permitted delimiter stripping, differing raw
    - POSSIBLE_FAMILY: common base with differing renewal/term suffix or leading zeros (requires review)
    - AMBIGUOUS: missing or unparseable policy numbers (requires review)
    - CONFLICTING: distinct, incompatible policy numbers (requires review)
    
    Normalization Rules:
    - Strips permitted delimiter characters: whitespace, hyphens, slashes, periods: [\\s\\-\\/\.]
    - Converts to uppercase.
    - Preserves all alphanumeric characters, leading zeros, sequence numbers,
      and renewal/term suffixes.
    - Does NOT remove leading zeros (e.g. '0012345' is NOT converted to '12345').
    - Does NOT remove renewal suffixes (e.g. '-01', '-02' are preserved).
    """
    coi_raw = (coi_policy_number or "").strip()
    end_raw = (endorsement_policy_number or "").strip()

    # Determine default evidence chain if omitted
    if evidence_chain is None:
        chain_refs: List[EvidenceReference] = []
        if coi_evidence is not None:
            chain_refs.append(coi_evidence)
        if endorsement_evidence is not None:
            chain_refs.append(endorsement_evidence)
        evidence_chain = tuple(chain_refs)

    # 1. Missing or unparseable policy numbers
    if not coi_raw or not end_raw:
        return PolicyReconciliationRecord(
            coi_document_id=coi_document_id,
            coi_policy_number=coi_policy_number,
            coi_evidence=coi_evidence,
            endorsement_document_id=endorsement_document_id,
            endorsement_policy_number=endorsement_policy_number,
            endorsement_evidence=endorsement_evidence,
            classification=PolicyMatchClassification.AMBIGUOUS,
            evidence_chain=evidence_chain,
            is_confirmed_match=False,
            requires_human_review=True,
            review_reason_code="MISSING_OR_UNPARSEABLE_POLICY_NUMBER",
        )

    # 2. EXACT character-for-character match
    if coi_raw == end_raw:
        has_citations = (coi_evidence is not None and endorsement_evidence is not None)
        if has_citations:
            return PolicyReconciliationRecord(
                coi_document_id=coi_document_id,
                coi_policy_number=coi_policy_number,
                coi_evidence=coi_evidence,
                endorsement_document_id=endorsement_document_id,
                endorsement_policy_number=endorsement_policy_number,
                endorsement_evidence=endorsement_evidence,
                classification=PolicyMatchClassification.EXACT,
                evidence_chain=evidence_chain,
                is_confirmed_match=True,
                requires_human_review=False,
                review_reason_code=None,
            )
        else:
            return PolicyReconciliationRecord(
                coi_document_id=coi_document_id,
                coi_policy_number=coi_policy_number,
                coi_evidence=coi_evidence,
                endorsement_document_id=endorsement_document_id,
                endorsement_policy_number=endorsement_policy_number,
                endorsement_evidence=endorsement_evidence,
                classification=PolicyMatchClassification.AMBIGUOUS,
                evidence_chain=evidence_chain,
                is_confirmed_match=False,
                requires_human_review=True,
                review_reason_code="POLICY_NUMBER_MISSING_EVIDENCE",
            )

    # 3. Delimiter-normalized match
    norm_coi = normalize_policy_identifier(coi_raw)
    norm_end = normalize_policy_identifier(end_raw)

    if norm_coi == norm_end:
        has_citations = (coi_evidence is not None and endorsement_evidence is not None)
        if has_citations:
            return PolicyReconciliationRecord(
                coi_document_id=coi_document_id,
                coi_policy_number=coi_policy_number,
                coi_evidence=coi_evidence,
                endorsement_document_id=endorsement_document_id,
                endorsement_policy_number=endorsement_policy_number,
                endorsement_evidence=endorsement_evidence,
                classification=PolicyMatchClassification.FORMATTING_NORMALIZED,
                evidence_chain=evidence_chain,
                is_confirmed_match=True,
                requires_human_review=False,
                review_reason_code=None,
            )
        else:
            return PolicyReconciliationRecord(
                coi_document_id=coi_document_id,
                coi_policy_number=coi_policy_number,
                coi_evidence=coi_evidence,
                endorsement_document_id=endorsement_document_id,
                endorsement_policy_number=endorsement_policy_number,
                endorsement_evidence=endorsement_evidence,
                classification=PolicyMatchClassification.AMBIGUOUS,
                evidence_chain=evidence_chain,
                is_confirmed_match=False,
                requires_human_review=True,
                review_reason_code="POLICY_NUMBER_MISSING_EVIDENCE",
            )

    # 4. Possible Family Match (documented renewal suffix pattern on identical base)
    if _is_possible_policy_family(coi_raw, end_raw):
        return PolicyReconciliationRecord(
            coi_document_id=coi_document_id,
            coi_policy_number=coi_policy_number,
            coi_evidence=coi_evidence,
            endorsement_document_id=endorsement_document_id,
            endorsement_policy_number=endorsement_policy_number,
            endorsement_evidence=endorsement_evidence,
            classification=PolicyMatchClassification.POSSIBLE_FAMILY,
            evidence_chain=evidence_chain,
            is_confirmed_match=False,
            requires_human_review=True,
            review_reason_code="POTENTIAL_POLICY_FAMILY_RENEWAL_MISMATCH",
        )

    # 5. Distinct / Conflicting policy numbers
    return PolicyReconciliationRecord(
        coi_document_id=coi_document_id,
        coi_policy_number=coi_policy_number,
        coi_evidence=coi_evidence,
        endorsement_document_id=endorsement_document_id,
        endorsement_policy_number=endorsement_policy_number,
        endorsement_evidence=endorsement_evidence,
        classification=PolicyMatchClassification.CONFLICTING,
        evidence_chain=evidence_chain,
        is_confirmed_match=False,
        requires_human_review=True,
        review_reason_code="CONFLICTING_POLICY_IDENTIFIERS",
    )


# =============================================================================
# 2. Entity Identity Reconciliation (Independent of Document Roles)
# =============================================================================

def reconcile_entity_identities(
    primary_entity_name: Optional[str],
    primary_entity_role: EntityRole,
    primary_document_id: str,
    referenced_entity_name: Optional[str],
    referenced_entity_role: EntityRole,
    referenced_document_id: str,
    applicable_contract_requirement_id: Optional[str] = None,
    dba_evidence: Optional[EvidenceReference] = None,
    evidence_chain: Tuple[EvidenceReference, ...] = (),
    primary_role_evidence: Optional[EvidenceReference] = None,
    referenced_role_evidence: Optional[EvidenceReference] = None,
) -> EntityReconciliationRecord:
    """Deterministically reconcile legal identity between two entities.
    
    Architectural Invariant:
    Legal identity and document roles are evaluated INDEPENDENTLY.
    - Two records representing the same legal entity (e.g. 'Apex Electrical LLC')
      may appear as NAMED_INSURED on a COI and ADDITIONAL_INSURED on an endorsement.
      Their identity is EXACT or FORMATTING_NORMALIZED.
    - Never classify two entities as DISTINCT_LEGAL_ENTITY solely because their
      document roles differ.
    - Contractual role satisfaction is evaluated separately.
    - A confirmed mismatch strictly requires affirmative dual-document evidence.
    """
    p_raw = (primary_entity_name or "").strip()
    r_raw = (referenced_entity_name or "").strip()

    # Missing entity names
    if not p_raw or not r_raw:
        return EntityReconciliationRecord(
            primary_entity_name=p_raw,
            primary_entity_role=primary_entity_role,
            primary_document_id=primary_document_id,
            referenced_entity_name=r_raw,
            referenced_entity_role=referenced_entity_role,
            referenced_document_id=referenced_document_id,
            classification=EntityMatchClassification.AMBIGUOUS_IDENTITY,
            applicable_contract_requirement_id=applicable_contract_requirement_id,
            primary_role_evidence=primary_role_evidence,
            referenced_role_evidence=referenced_role_evidence,
            evidence_chain=evidence_chain,
            is_confirmed_match=False,
            requires_human_review=True,
            review_reason_code="MISSING_ENTITY_NAME",
        )

    # Evidence coverage check (both documents present in chain and semantically relevant)
    doc_ids_in_evidence = {ref.document_id for ref in evidence_chain}
    has_doc_ids = (
        primary_document_id in doc_ids_in_evidence and referenced_document_id in doc_ids_in_evidence
    )
    has_primary_evidence = _evidence_supports_entity(p_raw, evidence_chain, doc_id=primary_document_id)
    has_referenced_evidence = _evidence_supports_entity(r_raw, evidence_chain, doc_id=referenced_document_id)
    has_dual_evidence = has_doc_ids and has_primary_evidence and has_referenced_evidence

    cleaned_p = _clean_entity_name(p_raw)
    cleaned_r = _clean_entity_name(r_raw)

    # 1. Exact string match (character-for-character, case-sensitive)
    if cleaned_p == cleaned_r:
        if has_dual_evidence:
            return EntityReconciliationRecord(
                primary_entity_name=p_raw,
                primary_entity_role=primary_entity_role,
                primary_document_id=primary_document_id,
                referenced_entity_name=r_raw,
                referenced_entity_role=referenced_entity_role,
                referenced_document_id=referenced_document_id,
                classification=EntityMatchClassification.EXACT,
                applicable_contract_requirement_id=applicable_contract_requirement_id,
                primary_role_evidence=primary_role_evidence,
                referenced_role_evidence=referenced_role_evidence,
                evidence_chain=evidence_chain,
                is_confirmed_match=True,
                requires_human_review=False,
                review_reason_code=None,
            )
        else:
            return EntityReconciliationRecord(
                primary_entity_name=p_raw,
                primary_entity_role=primary_entity_role,
                primary_document_id=primary_document_id,
                referenced_entity_name=r_raw,
                referenced_entity_role=referenced_entity_role,
                referenced_document_id=referenced_document_id,
                classification=EntityMatchClassification.AMBIGUOUS_IDENTITY,
                applicable_contract_requirement_id=applicable_contract_requirement_id,
                primary_role_evidence=primary_role_evidence,
                referenced_role_evidence=referenced_role_evidence,
                evidence_chain=evidence_chain,
                is_confirmed_match=False,
                requires_human_review=True,
                review_reason_code="INSUFFICIENT_EVIDENCE_FOR_CONFIRMATION",
            )

    # 2. Case-insensitive exact match
    if cleaned_p.lower() == cleaned_r.lower():
        if has_dual_evidence:
            return EntityReconciliationRecord(
                primary_entity_name=p_raw,
                primary_entity_role=primary_entity_role,
                primary_document_id=primary_document_id,
                referenced_entity_name=r_raw,
                referenced_entity_role=referenced_entity_role,
                referenced_document_id=referenced_document_id,
                classification=EntityMatchClassification.FORMATTING_NORMALIZED,
                applicable_contract_requirement_id=applicable_contract_requirement_id,
                primary_role_evidence=primary_role_evidence,
                referenced_role_evidence=referenced_role_evidence,
                evidence_chain=evidence_chain,
                is_confirmed_match=True,
                requires_human_review=False,
                review_reason_code=None,
            )
        else:
            return EntityReconciliationRecord(
                primary_entity_name=p_raw,
                primary_entity_role=primary_entity_role,
                primary_document_id=primary_document_id,
                referenced_entity_name=r_raw,
                referenced_entity_role=referenced_entity_role,
                referenced_document_id=referenced_document_id,
                classification=EntityMatchClassification.AMBIGUOUS_IDENTITY,
                applicable_contract_requirement_id=applicable_contract_requirement_id,
                primary_role_evidence=primary_role_evidence,
                referenced_role_evidence=referenced_role_evidence,
                evidence_chain=evidence_chain,
                is_confirmed_match=False,
                requires_human_review=True,
                review_reason_code="INSUFFICIENT_EVIDENCE_FOR_CONFIRMATION",
            )

    # 3. Formatting-normalized suffix check (e.g. 'Apex Electrical, LLC.' vs 'Apex Electrical LLC')
    core_p, suff_p = _split_core_and_suffix(cleaned_p)
    core_r, suff_r = _split_core_and_suffix(cleaned_r)

    if core_p and core_r and core_p == core_r:
        if suff_p == suff_r and suff_p is not None:
            if has_dual_evidence:
                return EntityReconciliationRecord(
                    primary_entity_name=p_raw,
                    primary_entity_role=primary_entity_role,
                    primary_document_id=primary_document_id,
                    referenced_entity_name=r_raw,
                    referenced_entity_role=referenced_entity_role,
                    referenced_document_id=referenced_document_id,
                    classification=EntityMatchClassification.FORMATTING_NORMALIZED,
                    applicable_contract_requirement_id=applicable_contract_requirement_id,
                    primary_role_evidence=primary_role_evidence,
                    referenced_role_evidence=referenced_role_evidence,
                    evidence_chain=evidence_chain,
                    is_confirmed_match=True,
                    requires_human_review=False,
                    review_reason_code=None,
                )
            else:
                return EntityReconciliationRecord(
                    primary_entity_name=p_raw,
                    primary_entity_role=primary_entity_role,
                    primary_document_id=primary_document_id,
                    referenced_entity_name=r_raw,
                    referenced_entity_role=referenced_entity_role,
                    referenced_document_id=referenced_document_id,
                    classification=EntityMatchClassification.AMBIGUOUS_IDENTITY,
                    applicable_contract_requirement_id=applicable_contract_requirement_id,
                    primary_role_evidence=primary_role_evidence,
                    referenced_role_evidence=referenced_role_evidence,
                    evidence_chain=evidence_chain,
                    is_confirmed_match=False,
                    requires_human_review=True,
                    review_reason_code="INSUFFICIENT_EVIDENCE_FOR_CONFIRMATION",
                )
        if suff_p != suff_r and suff_p is not None and suff_r is not None:
            # Different corporate legal forms (e.g. Inc vs LLC)
            return EntityReconciliationRecord(
                primary_entity_name=p_raw,
                primary_entity_role=primary_entity_role,
                primary_document_id=primary_document_id,
                referenced_entity_name=r_raw,
                referenced_entity_role=referenced_entity_role,
                referenced_document_id=referenced_document_id,
                classification=EntityMatchClassification.LEGAL_SUFFIX_DISCREPANCY,
                applicable_contract_requirement_id=applicable_contract_requirement_id,
                primary_role_evidence=primary_role_evidence,
                referenced_role_evidence=referenced_role_evidence,
                evidence_chain=evidence_chain,
                is_confirmed_match=False,
                requires_human_review=True,
                review_reason_code="CORPORATE_SUFFIX_DISCREPANCY",
            )

    # 4. Confirmed DBA Relationship with affirmative evidence
    if dba_evidence is not None:
        chain_with_dba = evidence_chain
        if dba_evidence not in chain_with_dba:
            chain_with_dba = chain_with_dba + (dba_evidence,)
        doc_ids_dba = {ref.document_id for ref in chain_with_dba}
        if primary_document_id in doc_ids_dba and referenced_document_id in doc_ids_dba:
            return EntityReconciliationRecord(
                primary_entity_name=p_raw,
                primary_entity_role=primary_entity_role,
                primary_document_id=primary_document_id,
                referenced_entity_name=r_raw,
                referenced_entity_role=referenced_entity_role,
                referenced_document_id=referenced_document_id,
                classification=EntityMatchClassification.CONFIRMED_DBA,
                applicable_contract_requirement_id=applicable_contract_requirement_id,
                dba_evidence=dba_evidence,
                primary_role_evidence=primary_role_evidence,
                referenced_role_evidence=referenced_role_evidence,
                evidence_chain=chain_with_dba,
                is_confirmed_match=True,
                requires_human_review=False,
                review_reason_code=None,
            )

    # 5. Ambiguous Trade / Partial Names
    tokens_p = set(re.findall(r"\b\w+\b", (core_p or cleaned_p).lower()))
    tokens_r = set(re.findall(r"\b\w+\b", (core_r or cleaned_r).lower()))
    tokens_p.discard("the")
    tokens_p.discard("a")
    tokens_p.discard("an")
    tokens_r.discard("the")
    tokens_r.discard("a")
    tokens_r.discard("an")

    is_partial_token_subset = bool(
        tokens_p and tokens_r and (tokens_p.issubset(tokens_r) or tokens_r.issubset(tokens_p))
    )
    dba_word_regex = re.compile(
        r"\b(d/?b/?a|doing\s+business\s+as|t/a|trading\s+as|trade\s+name|operating\s+as|division\s+of|subsidiary\s+of)\b",
        re.IGNORECASE,
    )
    has_dba_in_evidence = any(
        bool(dba_word_regex.search(ref.source_text or ""))
        for ref in evidence_chain
    )
    has_dba_keyword = bool(
        dba_word_regex.search(p_raw)
        or dba_word_regex.search(r_raw)
        or has_dba_in_evidence
    )

    is_ambiguous = is_partial_token_subset or has_dba_keyword

    if is_ambiguous:
        return EntityReconciliationRecord(
            primary_entity_name=p_raw,
            primary_entity_role=primary_entity_role,
            primary_document_id=primary_document_id,
            referenced_entity_name=r_raw,
            referenced_entity_role=referenced_entity_role,
            referenced_document_id=referenced_document_id,
            classification=EntityMatchClassification.AMBIGUOUS_IDENTITY,
            applicable_contract_requirement_id=applicable_contract_requirement_id,
            primary_role_evidence=primary_role_evidence,
            referenced_role_evidence=referenced_role_evidence,
            evidence_chain=evidence_chain,
            is_confirmed_match=False,
            requires_human_review=True,
            review_reason_code=(
                "POTENTIAL_DBA_OR_TRADE_NAME_RELATIONSHIP"
                if (has_dba_keyword and not is_partial_token_subset)
                else "AMBIGUOUS_TRADE_OR_PARTIAL_NAME"
            ),
        )

    # 6. Genuinely Different Legal Entities (Correction D)
    if applicable_contract_requirement_id:
        # Both entities must establish distinct legal identities (must have distinct corporate legal suffixes)
        has_established_legal_identities = (suff_p is not None and suff_r is not None)
        if has_dual_evidence and has_established_legal_identities:
            return EntityReconciliationRecord(
                primary_entity_name=p_raw,
                primary_entity_role=primary_entity_role,
                primary_document_id=primary_document_id,
                referenced_entity_name=r_raw,
                referenced_entity_role=referenced_entity_role,
                referenced_document_id=referenced_document_id,
                classification=EntityMatchClassification.CONFIRMED_MISMATCH,
                applicable_contract_requirement_id=applicable_contract_requirement_id,
                primary_role_evidence=primary_role_evidence,
                referenced_role_evidence=referenced_role_evidence,
                evidence_chain=evidence_chain,
                is_confirmed_match=False,
                requires_human_review=False,
                review_reason_code="AFFIRMATIVE_CONTRACTUAL_ENTITY_MISMATCH",
            )
        elif not has_established_legal_identities:
            return EntityReconciliationRecord(
                primary_entity_name=p_raw,
                primary_entity_role=primary_entity_role,
                primary_document_id=primary_document_id,
                referenced_entity_name=r_raw,
                referenced_entity_role=referenced_entity_role,
                referenced_document_id=referenced_document_id,
                classification=EntityMatchClassification.AMBIGUOUS_IDENTITY,
                applicable_contract_requirement_id=applicable_contract_requirement_id,
                primary_role_evidence=primary_role_evidence,
                referenced_role_evidence=referenced_role_evidence,
                evidence_chain=evidence_chain,
                is_confirmed_match=False,
                requires_human_review=True,
                review_reason_code="UNESTABLISHED_LEGAL_IDENTITY",
            )
        else:
            return EntityReconciliationRecord(
                primary_entity_name=p_raw,
                primary_entity_role=primary_entity_role,
                primary_document_id=primary_document_id,
                referenced_entity_name=r_raw,
                referenced_entity_role=referenced_entity_role,
                referenced_document_id=referenced_document_id,
                classification=EntityMatchClassification.AMBIGUOUS_IDENTITY,
                applicable_contract_requirement_id=applicable_contract_requirement_id,
                primary_role_evidence=primary_role_evidence,
                referenced_role_evidence=referenced_role_evidence,
                evidence_chain=evidence_chain,
                is_confirmed_match=False,
                requires_human_review=True,
                review_reason_code="INSUFFICIENT_MISMATCH_EVIDENCE",
            )

    return EntityReconciliationRecord(
        primary_entity_name=p_raw,
        primary_entity_role=primary_entity_role,
        primary_document_id=primary_document_id,
        referenced_entity_name=r_raw,
        referenced_entity_role=referenced_entity_role,
        referenced_document_id=referenced_document_id,
        classification=EntityMatchClassification.DISTINCT_LEGAL_ENTITY,
        applicable_contract_requirement_id=applicable_contract_requirement_id,
        primary_role_evidence=primary_role_evidence,
        referenced_role_evidence=referenced_role_evidence,
        evidence_chain=evidence_chain,
        is_confirmed_match=False,
        requires_human_review=True,
        review_reason_code="DISTINCT_LEGAL_ENTITIES",
    )


# =============================================================================
# 3. Contractual Role Satisfaction Verification
# =============================================================================

ROLE_EVIDENCE_PATTERNS = {
    EntityRole.ADDITIONAL_INSURED: re.compile(
        r"\b(additional\s+insured|included\s+as\s+(an\s+)?additional\s+insured|who\s+is\s+an\s+additional\s+insured|cg\s*20\s*(10|26|37|38|01))\b",
        re.IGNORECASE,
    ),
    EntityRole.NAMED_INSURED: re.compile(
        r"\b(named\s+insured|insured\s*:|insured\s+name|name\s+of\s+insured)\b",
        re.IGNORECASE,
    ),
    EntityRole.CERTIFICATE_HOLDER: re.compile(
        r"\b(certificate\s+holder|holder\s*:)\b",
        re.IGNORECASE,
    ),
}

CONFLICTING_ROLE_PATTERNS = {
    EntityRole.ADDITIONAL_INSURED: re.compile(
        r"\b(named\s+insured|insured\s*:|insured\s+name|certificate\s+holder|holder\s*:)\b",
        re.IGNORECASE,
    ),
    EntityRole.NAMED_INSURED: re.compile(
        r"\b(additional\s+insured|certificate\s+holder|holder\s*:)\b",
        re.IGNORECASE,
    ),
    EntityRole.CERTIFICATE_HOLDER: re.compile(
        r"\b(named\s+insured|insured\s*:|additional\s+insured)\b",
        re.IGNORECASE,
    ),
}

ROLE_LINKING_PHRASES = re.compile(
    r"\b(is|are|shall\s+be|was|as|including|designated\s+as|included\s+as|named\s+as)\b",
    re.IGNORECASE,
)

OTHER_CORP_ENTITY_REGEX = re.compile(
    r"\b[A-Za-z0-9&',.\s]{2,40}\b(?:LLC|Inc|Corp|Co|Ltd|PLLC|LP|Corporation|Incorporated|Company|Limited)\b",
    re.IGNORECASE,
)
CONJOINED_ENTITY_PRE_REGEX = re.compile(
    r"\b(?:LLC|Inc|Corp|Co|Ltd|PLLC|LP|Corporation|Incorporated|Company|Limited)\b[,\s]+(?:and|or|&)\s*$",
    re.IGNORECASE,
)
CONJOINED_ENTITY_POST_REGEX = re.compile(
    r"^\s*(?:and|or|&)\s+[A-Za-z0-9&',.\s]{2,40}\b(?:LLC|Inc|Corp|Co|Ltd|PLLC|LP|Corporation|Incorporated|Company|Limited)\b",
    re.IGNORECASE,
)


def _has_role_evidence(
    role: EntityRole,
    doc_id: str,
    record: EntityReconciliationRecord,
    role_ref: Optional[EvidenceReference],
) -> bool:
    """Verify that affirmative role evidence exists for the specified role and document,
    connecting the role wording directly to the specific evaluated entity."""
    pattern = ROLE_EVIDENCE_PATTERNS.get(role)
    if not pattern:
        return False

    # Determine which entity is associated with doc_id
    if doc_id == record.primary_document_id:
        target_entity = record.primary_entity_name
    elif doc_id == record.referenced_document_id:
        target_entity = record.referenced_entity_name
    else:
        return False

    candidates: List[EvidenceReference] = []
    if role_ref is not None and role_ref.document_id == doc_id:
        candidates.append(role_ref)
    for ref in record.evidence_chain:
        if ref.document_id == doc_id and ref not in candidates:
            candidates.append(ref)

    cleaned_target = _clean_entity_name(target_entity)
    core_t, suff_t = _split_core_and_suffix(cleaned_target)
    target_core = core_t if core_t else cleaned_target
    tokens_t = [w for w in re.findall(r"[A-Za-z0-9]+", target_core.lower()) if w not in ("the", "a", "an")]
    if not tokens_t:
        tokens_t = [w for w in re.findall(r"[A-Za-z0-9]+", target_core.lower())]
    if not tokens_t:
        return False

    pattern_ent = re.compile(
        r"\b" + r"(?:[\s,\.\-]+|\s*&\s*)".join(re.escape(t) for t in tokens_t) + r"\b",
        re.IGNORECASE,
    )
    conflicting_pat = CONFLICTING_ROLE_PATTERNS.get(role)

    for ref in candidates:
        text = ref.source_text or ""
        if not text:
            continue
        if not _evidence_supports_entity(target_entity, (ref,), doc_id=doc_id):
            continue

        ent_spans = []
        for m in pattern_ent.finditer(text):
            e_start, e_end = m.start(), m.end()
            post = text[e_end:e_end + 30]
            m_suff = re.match(
                r"^([,\s]+(?:llc|inc|corp|co|ltd|pllc|lp|incorporated|corporation|company|limited)\.?)",
                post,
                re.IGNORECASE,
            )
            if m_suff:
                e_end += len(m_suff.group(1))
            ent_spans.append((e_start, e_end))

        role_spans = [(m.start(), m.end()) for m in pattern.finditer(text)]
        if not ent_spans or not role_spans:
            continue

        for (e_start, e_end) in ent_spans:
            # Check immediate negative attribution: is target entity directly preceded by a conflicting role?
            pre_window = text[max(0, e_start - 35):e_start]
            if conflicting_pat and conflicting_pat.search(pre_window):
                continue

            for (r_start, r_end) in role_spans:
                # Role before Entity (e.g. "Additional Insured: Apex Electrical LLC")
                if r_end <= e_start:
                    between = text[r_end:e_start]
                    if len(between) <= 120:
                        # Header or prepositional assignment
                        if "." in between or ";" in between or "\n\n" in between:
                            continue
                        if conflicting_pat and conflicting_pat.search(between):
                            continue
                        # If another corporate entity intervened between role and target entity, reject
                        if OTHER_CORP_ENTITY_REGEX.search(between):
                            continue
                        # If target entity is conjoined with another entity following it under this role, reject
                        if CONJOINED_ENTITY_POST_REGEX.search(text[e_end:]):
                            continue
                        # Valid role-before-entity syntax: colon, dash, whitespace, or "status granted/provided to"
                        if re.match(
                            r"^[:\s\-]*(?:status\s+(?:is\s+)?(?:granted|provided|extended|applicable|added)\s+to\s+)?$",
                            between,
                            re.IGNORECASE,
                        ):
                            return True

                # Entity before Role (e.g. "Apex Electrical LLC is included as an Additional Insured")
                elif e_end <= r_start:
                    between = text[e_end:r_start]
                    if len(between) <= 120:
                        if "." in between or ";" in between or "\n\n" in between:
                            continue
                        if conflicting_pat and conflicting_pat.search(between):
                            continue
                        # If another corporate entity intervened between target entity and role, reject
                        if OTHER_CORP_ENTITY_REGEX.search(between):
                            continue
                        # If target entity is conjoined with a preceding entity in the subject position, reject
                        if CONJOINED_ENTITY_PRE_REGEX.search(text[max(0, e_start - 60):e_start]):
                            continue
                        # Valid entity-before-role syntax: linking phrase
                        clean_between = between.strip(" ,")
                        if ROLE_LINKING_PHRASES.search(clean_between) or not clean_between:
                            return True

    return False


def check_contractual_role_satisfaction(
    entity_record: EntityReconciliationRecord,
    required_entity_name: Optional[str],
    required_role: Optional[EntityRole],
    contract_requirement_id: Optional[str],
    coverage_type: Optional[CoverageType] = None,
    is_requirement_validated: bool = True,
) -> Tuple[bool, Optional[DocumentDiscrepancy]]:
    """Verify whether an entity satisfies a specific contractual role requirement.
    
    Guarantees (Correction C):
    - A general entity-name citation must not automatically prove a contractual role.
    - Require evidence supporting the actual role asserted for the relevant entity and document.
    - Preserve the distinction between:
      * Legal entity identity.
      * Document role.
      * Contractual role requirement.
      * Evidence proving that role.
    - A confirmed identity match must not automatically satisfy an additional-insured requirement.
    - Missing role evidence must produce an unresolved/missing-evidence result, not a confirmed violation.
    - An entity mismatch without affirmative evidence produces an unresolved discrepancy,
      not a confirmed violation.
    
    Returns:
        (is_satisfied, optional_discrepancy)
    """
    # 1. Require validated contractual requirement
    if not contract_requirement_id or not contract_requirement_id.strip():
        # No contractual requirement validated -> cannot establish a confirmed contractual violation!
        return False, None

    if not is_requirement_validated:
        # Requirement ID exists but requirement is not validated or applicable -> no confirmed violation
        return False, None

    if not required_entity_name or not required_entity_name.strip() or required_role is None:
        # Incomplete requirement parameters -> cannot determine violation
        return False, None

    # 2. Check entity identity against required_entity_name
    p_name = entity_record.primary_entity_name or ""
    r_name = entity_record.referenced_entity_name or ""
    is_p_match, _ = compare_entity_names(p_name, required_entity_name, require_exact=False)
    is_r_match, _ = compare_entity_names(r_name, required_entity_name, require_exact=False)

    entity_matches_requirement = (
        is_p_match
        or is_r_match
        or (entity_record.is_confirmed_match and (is_p_match or is_r_match))
    )

    # If neither entity matches the contractual requirement
    if not entity_matches_requirement:
        if entity_record.classification == EntityMatchClassification.CONFIRMED_MISMATCH and entity_record.evidence_chain:
            disc = DocumentDiscrepancy(
                discrepancy_id=f"disc_mismatch_{contract_requirement_id}",
                category=DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION,
                severity=Severity.CRITICAL,
                reason_code="CONFIRMED_NAMED_INSURED_VIOLATION",
                description=(
                    f"Confirmed entity mismatch: entity '{entity_record.primary_entity_name}' does not match "
                    f"required entity '{required_entity_name}' for requirement '{contract_requirement_id}'."
                ),
                source_document_id=entity_record.primary_document_id,
                conflicting_document_id=entity_record.referenced_document_id,
                applicable_requirement_id=contract_requirement_id,
                applicable_entity_role=required_role,
                applicable_coverage_type=coverage_type,
                evidence_chain=entity_record.evidence_chain,
                requires_human_review=True,
            )
            return False, disc

        # Entity does not match contractual requirement -> unresolved discrepancy, not a role-only violation
        disc = DocumentDiscrepancy(
            discrepancy_id=f"disc_unmatched_entity_{contract_requirement_id}",
            category=DiscrepancyCategory.DOCUMENT_DISCREPANCY,
            severity=Severity.MAJOR,
            reason_code="ENTITY_NAME_MISMATCH",
            description=(
                f"Entity '{entity_record.primary_entity_name}' does not match required entity "
                f"'{required_entity_name}' for requirement '{contract_requirement_id}'."
            ),
            source_document_id=entity_record.primary_document_id,
            conflicting_document_id=entity_record.referenced_document_id,
            applicable_requirement_id=contract_requirement_id,
            applicable_entity_role=required_role,
            applicable_coverage_type=coverage_type,
            evidence_chain=entity_record.evidence_chain,
            requires_human_review=True,
        )
        return False, disc

    # 3. Evidence adequacy check
    if not entity_record.evidence_chain and entity_record.primary_role_evidence is None and entity_record.referenced_role_evidence is None:
        disc = DocumentDiscrepancy(
            discrepancy_id=f"disc_missing_evidence_{contract_requirement_id}",
            category=DiscrepancyCategory.MISSING_EVIDENCE,
            severity=Severity.MAJOR,
            reason_code="INSUFFICIENT_ROLE_EVIDENCE",
            description=(
                f"Contractual requirement '{contract_requirement_id}' requires entity '{required_entity_name}' "
                f"in role '{required_role.value}', but evidence is missing or insufficient."
            ),
            source_document_id=entity_record.primary_document_id,
            conflicting_document_id=entity_record.referenced_document_id,
            applicable_requirement_id=contract_requirement_id,
            applicable_entity_role=required_role,
            applicable_coverage_type=coverage_type,
            evidence_chain=(),
            requires_human_review=True,
        )
        return False, disc

    # 4. Check if role is satisfied across BOTH primary and referenced documents WITH affirmative role evidence
    primary_has_role = (
        is_p_match
        and entity_record.primary_entity_role == required_role
    )
    primary_role_evidenced = (
        primary_has_role
        and _has_role_evidence(required_role, entity_record.primary_document_id, entity_record, entity_record.primary_role_evidence)
    )

    referenced_has_role = (
        (is_r_match or entity_record.is_confirmed_match)
        and entity_record.referenced_entity_role == required_role
    )
    referenced_role_evidenced = (
        referenced_has_role
        and _has_role_evidence(required_role, entity_record.referenced_document_id, entity_record, entity_record.referenced_role_evidence)
    )

    if primary_role_evidenced or referenced_role_evidenced:
        return True, None

    # If the role was claimed/asserted on either document, but affirmative role evidence is missing
    if primary_has_role or referenced_has_role:
        disc = DocumentDiscrepancy(
            discrepancy_id=f"disc_missing_role_evidence_{contract_requirement_id}",
            category=DiscrepancyCategory.MISSING_EVIDENCE,
            severity=Severity.MAJOR,
            reason_code="MISSING_ROLE_EVIDENCE",
            description=(
                f"Entity '{entity_record.primary_entity_name}' identity is matched, but affirmative evidence "
                f"proving the required contractual role '{required_role.value}' is missing."
            ),
            source_document_id=entity_record.primary_document_id,
            conflicting_document_id=entity_record.referenced_document_id,
            applicable_requirement_id=contract_requirement_id,
            applicable_entity_role=required_role,
            applicable_coverage_type=coverage_type,
            evidence_chain=(),
            requires_human_review=True,
        )
        return False, disc

    # 5. Role is NOT satisfied in either document
    # Same entity with no evidence of the required role -> unresolved (MISSING_EVIDENCE), not confirmed violation
    if entity_record.is_confirmed_match:
        disc = DocumentDiscrepancy(
            discrepancy_id=f"disc_missing_role_{contract_requirement_id}",
            category=DiscrepancyCategory.MISSING_EVIDENCE,
            severity=Severity.MAJOR,
            reason_code="INSUFFICIENT_ROLE_EVIDENCE",
            description=(
                f"Entity '{entity_record.primary_entity_name}' is confirmed, but neither document "
                f"evidences required contractual role '{required_role.value}' for {contract_requirement_id}."
            ),
            source_document_id=entity_record.primary_document_id,
            conflicting_document_id=entity_record.referenced_document_id,
            applicable_requirement_id=contract_requirement_id,
            applicable_entity_role=required_role,
            applicable_coverage_type=coverage_type,
            evidence_chain=(),
            requires_human_review=True,
        )
        return False, disc

    # Corporate suffix discrepancy -> Document Discrepancy
    if entity_record.classification == EntityMatchClassification.LEGAL_SUFFIX_DISCREPANCY:
        disc = DocumentDiscrepancy(
            discrepancy_id=f"disc_suffix_{contract_requirement_id}",
            category=DiscrepancyCategory.DOCUMENT_DISCREPANCY,
            severity=Severity.MAJOR,
            reason_code="CORPORATE_SUFFIX_DISCREPANCY",
            description=(
                f"Corporate suffix discrepancy between '{entity_record.primary_entity_name}' and "
                f"required entity '{required_entity_name}' for requirement '{contract_requirement_id}'."
            ),
            source_document_id=entity_record.primary_document_id,
            conflicting_document_id=entity_record.referenced_document_id,
            applicable_requirement_id=contract_requirement_id,
            applicable_entity_role=required_role,
            applicable_coverage_type=coverage_type,
            evidence_chain=entity_record.evidence_chain,
            requires_human_review=True,
        )
        return False, disc

    # Unresolved entity relationship -> Ambiguous relationship
    disc = DocumentDiscrepancy(
        discrepancy_id=f"disc_ambiguous_{contract_requirement_id}",
        category=DiscrepancyCategory.AMBIGUOUS_RELATIONSHIP,
        severity=Severity.MAJOR,
        reason_code=entity_record.review_reason_code or "AMBIGUOUS_ENTITY_RELATIONSHIP",
        description=(
            f"Unresolved entity relationship ({entity_record.classification.value}) for "
            f"'{entity_record.primary_entity_name}' against required '{required_entity_name}'."
        ),
        source_document_id=entity_record.primary_document_id,
        conflicting_document_id=entity_record.referenced_document_id,
        applicable_requirement_id=contract_requirement_id,
        applicable_entity_role=required_role,
        applicable_coverage_type=coverage_type,
        evidence_chain=entity_record.evidence_chain,
        requires_human_review=True,
    )
    return False, disc


# =============================================================================
# 4. Temporal Date Reconciliation
# =============================================================================

EFFECTIVE_LABEL_REGEX = re.compile(
    r"\b(effective|eff|inception|commencing|commence|start(?:ing)?|begins?|policy\s+period\s+from|period\s+from|term\s+start|from)\b",
    re.IGNORECASE,
)

EXPIRATION_LABEL_REGEX = re.compile(
    r"\b(expir(?:ation|es|y)|exp|ending|ends?|term\s+end|until|through|policy\s+period\s+to|period\s+to|to)\b",
    re.IGNORECASE,
)

UNRELATED_DATE_CONTEXT_REGEX = re.compile(
    r"\b(prior\s+policy|prior\s+term|previous\s+policy|previous\s+term|"
    r"invoice|payment|paid|billed|bill\s+date|quote|printed|signature|signed|notarized|"
    r"date\s+of\s+loss|loss\s+date|claim\s+date)\b",
    re.IGNORECASE,
)


def _detect_date_convention(text: str) -> Optional[str]:
    """Detect whether document text establishes a date convention: 'MM/DD' or 'DD/MM'."""
    # 1. Explicit convention labels / header formats in document
    if re.search(r"\b(?:date\s+format[:\s]+)?(?:mm/dd/yyyy|mm/dd/yy|mm-dd-yyyy|mm-dd-yy|\(mm/dd/yyyy\)|\(mm/dd/yy\))\b", text, re.IGNORECASE):
        return "MM/DD"
    if re.search(r"\b(?:date\s+format[:\s]+)?(?:dd/mm/yyyy|dd/mm/yy|dd-mm-yyyy|dd-mm-yy|\(dd/mm/yyyy\)|\(dd/mm/yy\))\b", text, re.IGNORECASE):
        return "DD/MM"
    # 2. Contextual dates in text where one token is > 12 (unambiguous day)
    for m in re.finditer(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b", text):
        t1, t2 = int(m.group(1)), int(m.group(2))
        if 1 <= t1 <= 12 and 13 <= t2 <= 31:
            return "MM/DD"
        if 13 <= t1 <= 31 and 1 <= t2 <= 12:
            return "DD/MM"
    return None


_MONTH_NAME_MAP: Dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_MONTH_ALT = (
    r"January|Jan\.?|February|Feb\.?|March|Mar\.?|April|Apr\.?|May|June|Jun\.?|"
    r"July|Jul\.?|August|Aug\.?|September|Sept\.?|Sep\.?|October|Oct\.?|November|Nov\.?|December|Dec\.?"
)

_DATE_CANDIDATE_REGEX = re.compile(
    rf"\b(?P<iso_y>\d{{4}})[-/](?P<iso_m>\d{{1,2}})[-/](?P<iso_d>\d{{1,2}})\b"
    rf"|\b(?P<ymd_y>\d{{4}})[\s/-]+(?P<ymd_m>{_MONTH_ALT})[\s/-]+(?P<ymd_d>\d{{1,2}})(?:st|nd|rd|th)?\b"
    rf"|\b(?P<mdy_m>{_MONTH_ALT})\s+(?P<mdy_d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<mdy_y>\d{{4}}|\d{{2}})\b"
    rf"|\b(?P<dmy_d>\d{{1,2}})(?:st|nd|rd|th)?[\s/-]+(?P<dmy_m>{_MONTH_ALT})[\s/,-]+(?P<dmy_y>\d{{4}}|\d{{2}})\b"
    rf"|\b(?P<num_1>\d{{1,2}})[/.-](?P<num_2>\d{{1,2}})[/.-](?P<num_y>\d{{4}}|\d{{2}})\b",
    re.IGNORECASE,
)

_PRIOR_POLICY_CONTEXT_REGEX = re.compile(
    r"\b(?:prior\s+policy|prior\s+term|previous\s+policy|previous\s+term)\b",
    re.IGNORECASE,
)
_CURRENT_POLICY_OVERRIDE_REGEX = re.compile(
    r"\b(?:current\s+policy|current\s+term|this\s+policy|this\s+endorsement|new\s+policy|policy\s+period)\b",
    re.IGNORECASE,
)


def _parse_date_candidate(m: re.Match[str], convention: Optional[str]) -> Optional[date]:
    """Parse a matched date candidate using ISO, textual month, or established numeric convention."""
    try:
        if m.group("iso_y"):
            return date(int(m.group("iso_y")), int(m.group("iso_m")), int(m.group("iso_d")))
        if m.group("ymd_y"):
            month_num = _MONTH_NAME_MAP[m.group("ymd_m").rstrip(".").lower()]
            return date(int(m.group("ymd_y")), month_num, int(m.group("ymd_d")))
        if m.group("mdy_m"):
            y_str = m.group("mdy_y")
            year_val = int(y_str) if len(y_str) == 4 else 2000 + int(y_str)
            month_num = _MONTH_NAME_MAP[m.group("mdy_m").rstrip(".").lower()]
            return date(year_val, month_num, int(m.group("mdy_d")))
        if m.group("dmy_m"):
            y_str = m.group("dmy_y")
            year_val = int(y_str) if len(y_str) == 4 else 2000 + int(y_str)
            month_num = _MONTH_NAME_MAP[m.group("dmy_m").rstrip(".").lower()]
            return date(year_val, month_num, int(m.group("dmy_d")))
        if m.group("num_y"):
            y_str = m.group("num_y")
            t1, t2 = int(m.group("num_1")), int(m.group("num_2"))
            if convention == "MM/DD":
                year_val = int(y_str) if len(y_str) == 4 else 2000 + int(y_str)
                return date(year_val, t1, t2)
            if convention == "DD/MM":
                year_val = int(y_str) if len(y_str) == 4 else 2000 + int(y_str)
                return date(year_val, t2, t1)
            # Without explicit convention: reject 2-digit years; accept unambiguous 4-digit US MM/DD/YYYY
            if len(y_str) != 4:
                return None
            return date(int(y_str), t1, t2)
    except (ValueError, KeyError):
        return None
    return None


def _find_explicit_dates_for_field(text: str, date_field: str) -> Set[date]:
    """Extract all dates in text explicitly associated with date_field ('effective' or 'expiration').

    Returns a set of distinct dates bound to date_field, excluding unrelated context
    (e.g. invoices, billing, or prior policy sentences). A set of length > 1 indicates
    conflicting dates for the same semantic field within the excerpt.
    """
    associated_dates: Set[date] = set()
    convention = _detect_date_convention(text)

    for m in _DATE_CANDIDATE_REGEX.finditer(text):
        parsed = _parse_date_candidate(m, convention)
        if parsed is None:
            continue

        m_start, m_end = m.start(), m.end()
        prefix = text[max(0, m_start - 80):m_start]
        suffix = text[m_end:min(len(text), m_end + 80)]

        # Isolate the current clause/sentence so prior sentences do not bleed across boundaries
        clause_prefix = re.split(r"(?<!\d)\.(?!\d)|[;\n]", prefix)[-1]

        # 1. Unrelated context check
        prior_matches = list(_PRIOR_POLICY_CONTEXT_REGEX.finditer(clause_prefix))
        if prior_matches:
            override_matches = list(_CURRENT_POLICY_OVERRIDE_REGEX.finditer(clause_prefix))
            last_prior_pos = prior_matches[-1].start()
            last_override_pos = override_matches[-1].start() if override_matches else -1
            if last_prior_pos > last_override_pos:
                continue

        unrelated_match = None
        for um in UNRELATED_DATE_CONTEXT_REGEX.finditer(clause_prefix):
            unrelated_match = um
        if unrelated_match:
            eff_m = list(EFFECTIVE_LABEL_REGEX.finditer(clause_prefix))
            exp_m = list(EXPIRATION_LABEL_REGEX.finditer(clause_prefix))
            last_eff_pos = eff_m[-1].start() if eff_m else -1
            last_exp_pos = exp_m[-1].start() if exp_m else -1
            last_field_pos = max(last_eff_pos, last_exp_pos)
            if unrelated_match.start() > last_field_pos:
                continue

        # 2. Date Range association ("Date_1 to/through/- Date_2")
        is_start_of_range = bool(
            re.match(
                r"^\s*(?:to|through|until|\u2013|\u2014|-)\s*(?:\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
                suffix,
                re.IGNORECASE,
            )
        )
        is_end_of_range = bool(re.search(r"(?:to|through|until|\u2013|\u2014|-)\s*$", clause_prefix, re.IGNORECASE))

        matched_field: Optional[str] = None
        if is_start_of_range and not is_end_of_range:
            matched_field = "effective"
        elif is_end_of_range:
            matched_field = "expiration"
        else:
            # 3. Explicit Label association
            eff_matches = list(EFFECTIVE_LABEL_REGEX.finditer(clause_prefix))
            exp_matches = list(EXPIRATION_LABEL_REGEX.finditer(clause_prefix))
            last_eff_end = eff_matches[-1].end() if eff_matches else -1
            last_exp_end = exp_matches[-1].end() if exp_matches else -1

            if last_eff_end > last_exp_end and last_eff_end != -1:
                matched_field = "effective"
            elif last_exp_end > last_eff_end and last_exp_end != -1:
                matched_field = "expiration"

        if matched_field == date_field:
            associated_dates.add(parsed)

    return associated_dates


def _evidence_has_conflicting_dates(
    evidence_refs: Tuple[EvidenceReference, ...],
    date_field: str,
    doc_id: Optional[str] = None,
) -> bool:
    """Return True if evidence_refs contain multiple distinct dates for the same semantic field."""
    all_dates: Set[date] = set()
    for ref in evidence_refs:
        if doc_id is not None and ref.document_id != doc_id:
            continue
        text = (ref.source_text or "").strip()
        if not text:
            continue
        snippet_dates = _find_explicit_dates_for_field(text, date_field)
        if len(snippet_dates) > 1:
            return True
        all_dates.update(snippet_dates)
        if len(all_dates) > 1:
            return True
    return False


def _evidence_supports_date(
    target_date: Optional[date],
    evidence_refs: Tuple[EvidenceReference, ...],
    date_field: str,  # "effective" or "expiration"
    doc_id: Optional[str] = None,
) -> bool:
    """Verify that evidence references semantically and unambiguously support target_date for date_field.
    
    Semantic validation rules:
    - Associate the extracted date with its semantic field or label.
    - Distinguish effective dates from expiration dates.
    - Reject dates that occur only in unrelated contexts (invoice, payment, prior policy, billed, quote).
    - Reject excerpts containing multiple distinct dates associated with the same semantic field.
    - Ensure evidence references belong to expected document (doc_id).
    - If date cannot be confidently associated with its field, returns False (requires human review).
    - Disallows ambiguous numeric date transposition (cannot assume day-first without unambiguous text).
    - Rejects ambiguous numeric dates (including 2-digit years) unless document evidence establishes convention.
    """
    if target_date is None:
        return True
    if not evidence_refs:
        return False

    all_matched_dates: Set[date] = set()
    for ref in evidence_refs:
        if doc_id is not None and ref.document_id != doc_id:
            continue
        text = (ref.source_text or "").strip()
        if not text:
            continue

        snippet_dates = _find_explicit_dates_for_field(text, date_field)
        # Multiple distinct dates bound to the same field within one snippet is conflicting/ambiguous
        if len(snippet_dates) > 1:
            return False
        all_matched_dates.update(snippet_dates)
        if len(all_matched_dates) > 1:
            return False

    return target_date in all_matched_dates


def reconcile_dates(
    policy_effective_date: Optional[date],
    policy_expiration_date: Optional[date],
    policy_date_evidence: Tuple[EvidenceReference, ...],
    endorsement_effective_date: Optional[date],
    endorsement_expiration_date: Optional[date],
    endorsement_date_evidence: Tuple[EvidenceReference, ...],
    evaluation_date: Optional[date] = None,
    policy_document_id: Optional[str] = None,
    endorsement_document_id: Optional[str] = None,
    evidence_chain: Tuple[EvidenceReference, ...] = (),
    contractual_duration_requirement_id: Optional[str] = None,
    required_coverage_end_date: Optional[date] = None,
    requires_coverage_through_policy_expiration: bool = False,
    is_duration_requirement_validated: bool = False,
) -> DateReconciliationRecord:
    """Deterministically reconcile policy and endorsement validity periods.
    
    Guarantees (Correction A):
    - Missing material date information must not silently produce requires_human_review=False.
    - Explicit unresolved handling for:
      * Missing policy effective or expiration dates when policy activity must be evaluated.
      * Missing endorsement effective or expiration dates when endorsement activity must be evaluated.
      * Missing evaluation date when an active/inactive conclusion is required.
      * Missing or inadequate date evidence for populated dates.
      * Incomplete validated contractual duration requirements.
    - Do not classify missing information as a confirmed contractual violation.
    - Do not infer noncompliance merely because an endorsement expires before the underlying policy
      when it remains active on the evaluation date and no validated contractual duration requirement
      establishes that longer coverage is required.
    - Inverted dates trigger immediate ValueError.
    """
    # Track whether policy or endorsement evaluation is required
    evaluating_policy = bool(
        policy_document_id is not None
        or policy_effective_date is not None
        or policy_expiration_date is not None
        or policy_date_evidence
    )
    evaluating_endorsement = bool(
        endorsement_document_id is not None
        or endorsement_effective_date is not None
        or endorsement_expiration_date is not None
        or endorsement_date_evidence
    )

    is_policy_active: Optional[bool] = None
    is_endorsement_active: Optional[bool] = None

    missing_eval_date = (evaluation_date is None) and (evaluating_policy or evaluating_endorsement)
    missing_policy_dates = evaluating_policy and (policy_effective_date is None or policy_expiration_date is None)
    missing_endorsement_dates = evaluating_endorsement and (endorsement_effective_date is None or endorsement_expiration_date is None)

    # Date evidence semantic validation (WP1)
    policy_dates_supported = True
    if policy_effective_date is not None and not _evidence_supports_date(policy_effective_date, policy_date_evidence, date_field="effective", doc_id=policy_document_id):
        policy_dates_supported = False
    if policy_expiration_date is not None and not _evidence_supports_date(policy_expiration_date, policy_date_evidence, date_field="expiration", doc_id=policy_document_id):
        policy_dates_supported = False

    endorsement_dates_supported = True
    if endorsement_effective_date is not None and not _evidence_supports_date(endorsement_effective_date, endorsement_date_evidence, date_field="effective", doc_id=endorsement_document_id):
        endorsement_dates_supported = False
    if endorsement_expiration_date is not None and not _evidence_supports_date(endorsement_expiration_date, endorsement_date_evidence, date_field="expiration", doc_id=endorsement_document_id):
        endorsement_dates_supported = False

    unsupported_dates = (evaluating_policy and not policy_dates_supported) or (evaluating_endorsement and not endorsement_dates_supported)

    if evaluation_date is not None:
        if policy_effective_date is not None and policy_expiration_date is not None and policy_dates_supported:
            if policy_effective_date <= evaluation_date <= policy_expiration_date:
                is_policy_active = True
            else:
                is_policy_active = False

        if endorsement_effective_date is not None and endorsement_expiration_date is not None and endorsement_dates_supported:
            if endorsement_effective_date <= evaluation_date <= endorsement_expiration_date:
                is_endorsement_active = True
            else:
                is_endorsement_active = False

    # Midterm addition
    is_midterm: Optional[bool] = None
    if endorsement_effective_date is not None and policy_effective_date is not None and policy_dates_supported and endorsement_dates_supported:
        is_midterm = endorsement_effective_date > policy_effective_date

    # Premature expiration relative to underlying policy
    is_premature: Optional[bool] = None
    if endorsement_expiration_date is not None and policy_expiration_date is not None and policy_dates_supported and endorsement_dates_supported:
        if endorsement_expiration_date < policy_expiration_date:
            if is_endorsement_active is not None:
                is_premature = True
        else:
            is_premature = False

    # Review & Discrepancy Derivation (Correction A & WP1):
    requires_review = False
    discrepancy_cat: Optional[DiscrepancyCategory] = None
    reason_code: Optional[str] = None

    conflicting_date_evidence = (
        (evaluating_policy and (
            _evidence_has_conflicting_dates(policy_date_evidence, "effective", doc_id=policy_document_id)
            or _evidence_has_conflicting_dates(policy_date_evidence, "expiration", doc_id=policy_document_id)
        ))
        or (evaluating_endorsement and (
            _evidence_has_conflicting_dates(endorsement_date_evidence, "effective", doc_id=endorsement_document_id)
            or _evidence_has_conflicting_dates(endorsement_date_evidence, "expiration", doc_id=endorsement_document_id)
        ))
    )

    if missing_eval_date:
        requires_review = True
        discrepancy_cat = DiscrepancyCategory.MISSING_EVIDENCE
        reason_code = "MISSING_EVALUATION_DATE"
    elif missing_policy_dates:
        requires_review = True
        discrepancy_cat = DiscrepancyCategory.MISSING_EVIDENCE
        reason_code = "MISSING_POLICY_DATES"
    elif missing_endorsement_dates:
        requires_review = True
        discrepancy_cat = DiscrepancyCategory.MISSING_EVIDENCE
        reason_code = "MISSING_ENDORSEMENT_DATES"
    elif conflicting_date_evidence:
        requires_review = True
        discrepancy_cat = DiscrepancyCategory.DOCUMENT_DISCREPANCY
        reason_code = "CONFLICTING_DATE_EVIDENCE"
    elif unsupported_dates:
        requires_review = True
        discrepancy_cat = DiscrepancyCategory.MISSING_EVIDENCE
        reason_code = "UNSUPPORTED_DATE_EVIDENCE"
    elif is_policy_active is False:
        requires_review = True
        discrepancy_cat = DiscrepancyCategory.DOCUMENT_DISCREPANCY
        reason_code = "POLICY_INACTIVE_ON_EVALUATION_DATE"
    elif is_endorsement_active is False:
        requires_review = True
        discrepancy_cat = DiscrepancyCategory.DOCUMENT_DISCREPANCY
        reason_code = "ENDORSEMENT_INACTIVE_ON_EVALUATION_DATE"
    elif contractual_duration_requirement_id is not None:
        if not is_duration_requirement_validated:
            requires_review = True
            discrepancy_cat = DiscrepancyCategory.DOCUMENT_DISCREPANCY
            reason_code = "UNVALIDATED_DURATION_REQUIREMENT"
        elif required_coverage_end_date is None and not requires_coverage_through_policy_expiration:
            # Incomplete validated contractual duration requirements
            requires_review = True
            discrepancy_cat = DiscrepancyCategory.DOCUMENT_DISCREPANCY
            reason_code = "INCOMPLETE_DURATION_REQUIREMENT_TERMS"
        elif endorsement_document_id is None:
            # Required endorsement document is missing when contractual duration requirement is evaluated
            requires_review = True
            discrepancy_cat = DiscrepancyCategory.MISSING_EVIDENCE
            reason_code = "MISSING_REQUIRED_ENDORSEMENT_DOCUMENT"
        elif endorsement_effective_date is None or endorsement_expiration_date is None:
            # Required endorsement dates are missing
            requires_review = True
            discrepancy_cat = DiscrepancyCategory.MISSING_EVIDENCE
            reason_code = "MISSING_ENDORSEMENT_DATES"
        else:
            violation = False
            if requires_coverage_through_policy_expiration and is_premature is True:
                violation = True
            elif required_coverage_end_date is not None:
                if endorsement_expiration_date is not None and endorsement_expiration_date < required_coverage_end_date:
                    violation = True
                elif policy_expiration_date is not None and policy_expiration_date < required_coverage_end_date:
                    violation = True

            if violation:
                requires_review = True
                discrepancy_cat = DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION
                reason_code = "PREMATURE_EXPIRATION_VIOLATION"
            else:
                requires_review = False
                discrepancy_cat = None
                reason_code = None
    else:
        # Earlier endorsement expiration alone does not create a confirmed violation
        requires_review = False
        discrepancy_cat = None
        reason_code = None

    return DateReconciliationRecord(
        policy_document_id=policy_document_id,
        policy_effective_date=policy_effective_date,
        policy_expiration_date=policy_expiration_date,
        policy_date_evidence=policy_date_evidence,
        endorsement_document_id=endorsement_document_id,
        endorsement_effective_date=endorsement_effective_date,
        endorsement_expiration_date=endorsement_expiration_date,
        endorsement_date_evidence=endorsement_date_evidence,
        evaluation_date=evaluation_date,
        is_midterm_addition=is_midterm,
        is_policy_active_on_evaluation_date=is_policy_active,
        is_endorsement_active_on_evaluation_date=is_endorsement_active,
        is_premature_expiration=is_premature,
        requires_human_review=requires_review,
        discrepancy_category=discrepancy_cat,
        review_reason_code=reason_code,
        evidence_chain=evidence_chain,
    )


# =============================================================================
# 5. Relationship Classification with Relationship-Specific Evidence
# =============================================================================

def classify_document_relationship(
    edge_id: str,
    source_document_id: str,
    target_document_id: Optional[str],
    relationship_type: DocumentRelationshipType,
    classification: EdgeClassification,
    is_compliance_relevant: bool = True,
    evidence_references: Tuple[EvidenceReference, ...] = (),
    associated_policy_id: Optional[str] = None,
    coverage_type: Optional[CoverageType] = None,
    has_relationship_evidence: bool = True,
    requires_human_review: Optional[bool] = None,
    review_reason_code: Optional[str] = None,
    target_policy_number: Optional[str] = None,
    target_document_number: Optional[str] = None,
    target_form_number: Optional[str] = None,
    candidate_target_document_ids: Optional[Tuple[str, ...]] = None,
) -> DocumentRelationshipEdge:
    """Build a validated directional relationship edge between documents.
    
    Guarantees (WP2):
    - Identify source document and referenced target document.
    - Establish that source text actually refers to that target.
    - Validate available target identifiers against the actual target document.
    - Do not treat generic wording such as 'this endorsement amends the policy' as sufficient proof.
    - If target is missing, ambiguous, conflicting, or insufficiently identified, return
      unresolved/ambiguous/conflicting classification and require human review.
    - Preserve backward compatibility for valid existing document relationships.
    """
    if relationship_type == DocumentRelationshipType.ORPHAN_DOCUMENT:
        return DocumentRelationshipEdge(
            edge_id=edge_id,
            source_document_id=source_document_id,
            target_document_id=None,
            relationship_type=DocumentRelationshipType.ORPHAN_DOCUMENT,
            classification=EdgeClassification.UNLINKED_ORPHAN,
            is_compliance_relevant=is_compliance_relevant,
            evidence_references=evidence_references,
            is_confirmed=False,
            requires_human_review=True,
            review_reason_code=review_reason_code or "ORPHAN_DOCUMENT_NO_POLICY_LINK",
        )

    # Missing target document check
    if not target_document_id or not target_document_id.strip():
        return DocumentRelationshipEdge(
            edge_id=edge_id,
            source_document_id=source_document_id,
            target_document_id=None,
            relationship_type=DocumentRelationshipType.ORPHAN_DOCUMENT,
            classification=EdgeClassification.UNLINKED_ORPHAN,
            is_compliance_relevant=is_compliance_relevant,
            evidence_references=evidence_references,
            is_confirmed=False,
            requires_human_review=True,
            review_reason_code=review_reason_code or "MISSING_TARGET_DOCUMENT",
        )

    # For REPLACES_DOCUMENT and AMENDS_DOCUMENT, execute target linkage validation (WP2)
    if relationship_type in (
        DocumentRelationshipType.REPLACES_DOCUMENT,
        DocumentRelationshipType.AMENDS_DOCUMENT,
    ):
        is_linked, reason, target_class = _validate_target_linkage(
            relationship_type=relationship_type,
            source_document_id=source_document_id,
            target_document_id=target_document_id,
            evidence_references=evidence_references,
            associated_policy_id=associated_policy_id,
            target_policy_number=target_policy_number,
            target_document_number=target_document_number,
            target_form_number=target_form_number,
            candidate_target_document_ids=candidate_target_document_ids,
        )
        if not is_linked or not has_relationship_evidence:
            eff_reason = reason or ("UNSUPPORTED_RELATIONSHIP_ASSERTION" if not has_relationship_evidence else "INSUFFICIENT_RELATIONSHIP_EVIDENCE")
            eff_class = target_class if (target_class and target_class != EdgeClassification.CONFIRMED_EXACT) else EdgeClassification.AMBIGUOUS
            return DocumentRelationshipEdge(
                edge_id=edge_id,
                source_document_id=source_document_id,
                target_document_id=target_document_id,
                relationship_type=relationship_type,
                associated_policy_id=associated_policy_id,
                coverage_type=coverage_type,
                classification=eff_class,
                is_compliance_relevant=is_compliance_relevant,
                evidence_references=evidence_references,
                is_confirmed=False,
                requires_human_review=True,
                review_reason_code=review_reason_code or eff_reason,
            )

        return DocumentRelationshipEdge(
            edge_id=edge_id,
            source_document_id=source_document_id,
            target_document_id=target_document_id,
            relationship_type=relationship_type,
            associated_policy_id=associated_policy_id,
            coverage_type=coverage_type,
            classification=classification,
            is_compliance_relevant=is_compliance_relevant,
            evidence_references=evidence_references,
            is_confirmed=True,
            requires_human_review=False,
            review_reason_code=review_reason_code,
        )

    # Validate relationship-specific affirmative evidence across both documents (other relationship types)
    doc_ids = {ref.document_id for ref in evidence_references}
    has_both_docs = bool(source_document_id in doc_ids and target_document_id is not None and target_document_id in doc_ids)

    src_text = " ".join((ref.source_text or "") for ref in evidence_references if ref.document_id == source_document_id)
    tgt_text = " ".join((ref.source_text or "") for ref in evidence_references if ref.document_id == target_document_id)

    has_specific_evidence = False

    if has_both_docs and has_relationship_evidence:
        if relationship_type == DocumentRelationshipType.DUPLICATE_OF:
            has_specific_evidence = bool(DUPLICATE_EVIDENCE_PATTERN.search(f"{src_text} {tgt_text}"))

        elif relationship_type == DocumentRelationshipType.DECLARATION_FOR_POLICY:
            has_specific_evidence = bool(
                DECLARATION_EVIDENCE_PATTERN.search(src_text)
                and POLICY_COVERAGE_EVIDENCE_PATTERN.search(tgt_text)
            )

        elif relationship_type == DocumentRelationshipType.ENDORSEMENT_TO_POLICY:
            has_specific_evidence = bool(
                ENDORSEMENT_EVIDENCE_PATTERN.search(src_text)
                and POLICY_COVERAGE_EVIDENCE_PATTERN.search(tgt_text)
            )

        elif relationship_type == DocumentRelationshipType.SAME_POLICY:
            has_specific_evidence = bool(
                POLICY_COVERAGE_EVIDENCE_PATTERN.search(src_text)
                and POLICY_COVERAGE_EVIDENCE_PATTERN.search(tgt_text)
            )
        else:
            has_specific_evidence = True

    # If classification is confirmed but relationship-specific evidence is missing, downgrade to AMBIGUOUS
    wants_confirmed = classification in (
        EdgeClassification.CONFIRMED_EXACT,
        EdgeClassification.CONFIRMED_NORMALIZED,
        EdgeClassification.CONFIRMED_DBA,
    )

    if wants_confirmed and not has_specific_evidence:
        default_reason = "UNSUPPORTED_RELATIONSHIP_ASSERTION" if not has_relationship_evidence else "INSUFFICIENT_RELATIONSHIP_EVIDENCE"
        return DocumentRelationshipEdge(
            edge_id=edge_id,
            source_document_id=source_document_id,
            target_document_id=target_document_id,
            relationship_type=relationship_type,
            associated_policy_id=associated_policy_id,
            coverage_type=coverage_type,
            classification=EdgeClassification.AMBIGUOUS,
            is_compliance_relevant=is_compliance_relevant,
            evidence_references=evidence_references,
            is_confirmed=False,
            requires_human_review=True,
            review_reason_code=review_reason_code or default_reason,
        )

    is_confirmed = wants_confirmed and has_specific_evidence
    if requires_human_review is None:
        requires_human_review = not is_confirmed

    return DocumentRelationshipEdge(
        edge_id=edge_id,
        source_document_id=source_document_id,
        target_document_id=target_document_id,
        relationship_type=relationship_type,
        associated_policy_id=associated_policy_id,
        coverage_type=coverage_type,
        classification=classification,
        is_compliance_relevant=is_compliance_relevant,
        evidence_references=evidence_references,
        is_confirmed=is_confirmed,
        requires_human_review=requires_human_review,
        review_reason_code=review_reason_code,
    )


# =============================================================================
# 6. Deterministic Reconciliation Engine
# =============================================================================

class DeterministicReconciliationEngine:
    """Deterministic, rule-based cross-document reconciliation engine.
    
    Operates without AI in the decision path: takes extracted documents,
    packet manifest, and contractual requirements to build a fully validated,
    immutable CrossDocumentReconciliationResult.
    """

    def reconcile_packet(
        self,
        document_packet_id: str,
        packet_manifest: Tuple[DocumentManifestEntry, ...],
        primary_coi_id: Optional[str] = None,
        graph_edges: Tuple[DocumentRelationshipEdge, ...] = (),
        entity_records: Tuple[EntityReconciliationRecord, ...] = (),
        policy_records: Tuple[PolicyReconciliationRecord, ...] = (),
        date_records: Tuple[DateReconciliationRecord, ...] = (),
        missing_evidence_records: Tuple[MissingEvidenceProvenance, ...] = (),
        discrepancies: Tuple[DocumentDiscrepancy, ...] = (),
    ) -> CrossDocumentReconciliationResult:
        """Construct and validate complete immutable reconciliation result."""
        return CrossDocumentReconciliationResult(
            document_packet_id=document_packet_id,
            primary_coi_id=primary_coi_id,
            packet_manifest=packet_manifest,
            graph_edges=graph_edges,
            entity_records=entity_records,
            policy_records=policy_records,
            date_records=date_records,
            missing_evidence_records=missing_evidence_records,
            discrepancies=discrepancies,
        )


def _evidence_has_date_content(refs: Tuple[EvidenceReference, ...]) -> bool:
    """Return True if any evidence snippet contains recognizable date expressions."""
    return any(_DATE_CANDIDATE_REGEX.search(ref.source_text or "") for ref in refs if ref.source_text)


def reconcile_extracted_document_set(
    document_set: ExtractedDocumentSet,
    requirements: Optional[List[InsuranceRequirement]] = None,
    evaluation_date: Optional[date] = None,
    packet_id: Optional[str] = None,
    document_hashes: Optional[Dict[str, str]] = None,
) -> Optional[CrossDocumentReconciliationResult]:
    """Execute deterministic cross-document reconciliation across an ExtractedDocumentSet.

    Wires WP1, WP2, and WP3 reconciliation primitives into the production evaluation pipeline:
    - Entity identity & contractual role reconciliation across packet documents
    - Policy identifier reconciliation between COI coverages and endorsements
    - Document relationship target-linkage classification for REPLACES/AMENDS wording
    - Date & contractual-duration reconciliation where applicable inputs exist

    Returns a validated CrossDocumentReconciliationResult via DeterministicReconciliationEngine,
    or None if the document set contains no document evidence manifest entries.
    """
    reqs = requirements or []

    # 1. Build deterministic packet manifest from all EvidenceReferences on document_set
    all_refs: List[EvidenceReference] = []
    if document_set.named_insured and document_set.named_insured.evidence:
        all_refs.append(document_set.named_insured.evidence)
    for sec_ni in document_set.additional_named_insureds:
        if sec_ni.evidence:
            all_refs.append(sec_ni.evidence)
    if document_set.certificate_holder and document_set.certificate_holder.evidence:
        all_refs.append(document_set.certificate_holder.evidence)
    for cov in document_set.coverages:
        all_refs.extend(cov.date_evidence)
        if cov.evidence:
            all_refs.append(cov.evidence)
    for end in document_set.endorsements:
        all_refs.extend(end.date_evidence)
        if end.evidence:
            all_refs.append(end.evidence)

    if not all_refs:
        return None

    doc_types: Dict[str, DocumentType] = {}
    doc_max_page: Dict[str, int] = {}
    for ref in all_refs:
        if ref.document_id not in doc_types:
            doc_types[ref.document_id] = ref.document_type
        doc_max_page[ref.document_id] = max(doc_max_page.get(ref.document_id, 1), ref.page_number)

    doc_hash_map = document_hashes or {}
    manifest_entries = tuple(
        DocumentManifestEntry(
            document_id=doc_id,
            sha256_hash=doc_hash_map.get(doc_id) or hashlib.sha256(f"{doc_id}:{doc_types[doc_id].value}".encode("utf-8")).hexdigest(),
            detected_document_type=doc_types[doc_id],
            page_count=doc_max_page[doc_id],
        )
        for doc_id in doc_types
    )
    manifest_map = {m.document_id: m for m in manifest_entries}
    primary_coi_id = next(
        (m.document_id for m in manifest_entries if m.detected_document_type == DocumentType.COI),
        None,
    )

    def _ev_compatible(ref: Optional[EvidenceReference]) -> bool:
        return bool(
            ref is not None
            and ref.document_id in manifest_map
            and ref.document_type == manifest_map[ref.document_id].detected_document_type
        )

    graph_edges: List[DocumentRelationshipEdge] = []
    entity_records: List[EntityReconciliationRecord] = []
    policy_records: List[PolicyReconciliationRecord] = []
    date_records: List[DateReconciliationRecord] = []
    discrepancies: List[DocumentDiscrepancy] = []
    seen_disc_ids: Set[str] = set()

    def _add_discrepancy(disc: DocumentDiscrepancy) -> None:
        if disc.discrepancy_id not in seen_disc_ids:
            seen_disc_ids.add(disc.discrepancy_id)
            discrepancies.append(disc)

    # 2. Cross-document Entity Identity & Role Reconciliation
    ni_req = next((r for r in reqs if r.rule_type == RequirementType.NAMED_INSURED), None)
    prim_ni = document_set.named_insured
    if prim_ni and prim_ni.name and _ev_compatible(prim_ni.evidence):
        assert prim_ni.evidence is not None
        for idx, sec_ni in enumerate(document_set.additional_named_insureds):
            if not _ev_compatible(sec_ni.evidence) or sec_ni.evidence is None:
                continue
            if sec_ni.evidence.document_id == prim_ni.evidence.document_id:
                continue
            erec = reconcile_entity_identities(
                primary_entity_name=prim_ni.name,
                primary_entity_role=EntityRole.NAMED_INSURED,
                primary_document_id=prim_ni.evidence.document_id,
                referenced_entity_name=sec_ni.name,
                referenced_entity_role=EntityRole.NAMED_INSURED,
                referenced_document_id=sec_ni.evidence.document_id,
                applicable_contract_requirement_id=ni_req.requirement_id if ni_req else None,
                evidence_chain=(prim_ni.evidence, sec_ni.evidence),
                primary_role_evidence=prim_ni.evidence,
                referenced_role_evidence=sec_ni.evidence,
            )
            entity_records.append(erec)

            if erec.classification == EntityMatchClassification.CONFIRMED_MISMATCH:
                req_id = ni_req.requirement_id if ni_req else None
                cat = (
                    DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION
                    if req_id
                    else DiscrepancyCategory.DOCUMENT_DISCREPANCY
                )
                _add_discrepancy(
                    DocumentDiscrepancy(
                        discrepancy_id=f"disc_entity_{idx}_{sec_ni.evidence.document_id}",
                        category=cat,
                        severity=Severity.CRITICAL,
                        reason_code=erec.review_reason_code or "CONFIRMED_ENTITY_MISMATCH",
                        description=(
                            f"Cross-document entity identity mismatch: '{prim_ni.name}' ({prim_ni.evidence.document_id}) "
                            f"conflicts with '{sec_ni.name}' ({sec_ni.evidence.document_id})."
                        ),
                        source_document_id=prim_ni.evidence.document_id,
                        conflicting_document_id=sec_ni.evidence.document_id,
                        applicable_requirement_id=req_id,
                        applicable_entity_role=EntityRole.NAMED_INSURED,
                        evidence_chain=(prim_ni.evidence, sec_ni.evidence),
                        requires_human_review=True,
                    )
                )
            elif erec.requires_human_review:
                cat = (
                    DiscrepancyCategory.DOCUMENT_DISCREPANCY
                    if erec.classification == EntityMatchClassification.LEGAL_SUFFIX_DISCREPANCY
                    else DiscrepancyCategory.AMBIGUOUS_RELATIONSHIP
                )
                _add_discrepancy(
                    DocumentDiscrepancy(
                        discrepancy_id=f"disc_entity_{idx}_{sec_ni.evidence.document_id}",
                        category=cat,
                        severity=Severity.MAJOR,
                        reason_code=erec.review_reason_code or "AMBIGUOUS_ENTITY_IDENTITY",
                        description=(
                            f"Cross-document entity identity requires review: '{prim_ni.name}' "
                            f"vs '{sec_ni.name}' ({erec.classification.value})."
                        ),
                        source_document_id=prim_ni.evidence.document_id,
                        conflicting_document_id=sec_ni.evidence.document_id,
                        applicable_requirement_id=ni_req.requirement_id if ni_req else None,
                        applicable_entity_role=EntityRole.NAMED_INSURED,
                        evidence_chain=(prim_ni.evidence, sec_ni.evidence),
                        requires_human_review=True,
                    )
                )

    # 3. Cross-document Policy Identifier Reconciliation
    covs_with_pol = [
        c for c in document_set.coverages
        if c.policy_number and c.policy_number.strip() and _ev_compatible(c.evidence)
    ]
    for idx, end in enumerate(document_set.endorsements):
        if not (end.applicable_policy and end.applicable_policy.strip() and _ev_compatible(end.evidence)):
            continue
        assert end.evidence is not None
        end_doc_id = end.evidence.document_id
        other_covs = [c for c in covs_with_pol if c.evidence and c.evidence.document_id != end_doc_id]
        if not other_covs:
            continue

        norm_end_pol = normalize_policy_identifier(end.applicable_policy)
        matched_cov = next(
            (c for c in other_covs if normalize_policy_identifier(c.policy_number) == norm_end_pol),
            None,
        )
        target_cov = matched_cov or next(
            (c for c in other_covs if c.coverage_type == CoverageType.GENERAL_LIABILITY),
            other_covs[0],
        )
        assert target_cov.evidence is not None
        pol_rec = reconcile_policy_identifiers(
            coi_document_id=target_cov.evidence.document_id,
            coi_policy_number=target_cov.policy_number,
            coi_evidence=target_cov.evidence,
            endorsement_document_id=end_doc_id,
            endorsement_policy_number=end.applicable_policy,
            endorsement_evidence=end.evidence,
            evidence_chain=(target_cov.evidence, end.evidence),
        )
        policy_records.append(pol_rec)
        if pol_rec.requires_human_review:
            _add_discrepancy(
                DocumentDiscrepancy(
                    discrepancy_id=f"disc_pol_{idx}_{end_doc_id}",
                    category=DiscrepancyCategory.DOCUMENT_DISCREPANCY,
                    severity=Severity.MAJOR,
                    reason_code=pol_rec.review_reason_code or "CONFLICTING_POLICY_IDENTIFIERS",
                    description=(
                        f"Policy identifier mismatch between COI '{target_cov.policy_number}' and "
                        f"endorsement applicable policy '{end.applicable_policy}'."
                    ),
                    source_document_id=target_cov.evidence.document_id,
                    conflicting_document_id=end_doc_id,
                    applicable_coverage_type=target_cov.coverage_type,
                    evidence_chain=(target_cov.evidence, end.evidence),
                    requires_human_review=True,
                )
            )

    # 4. Document Relationship Target Linkage (REPLACES / AMENDS)
    covs_with_ev = [c for c in document_set.coverages if _ev_compatible(c.evidence)]
    for idx, end in enumerate(document_set.endorsements):
        if not _ev_compatible(end.evidence) or end.evidence is None:
            continue
        end_doc_id = end.evidence.document_id
        src_evs = [r for r in ([end.evidence] + list(end.date_evidence)) if _ev_compatible(r) and r.document_id == end_doc_id]
        src_text = " ".join(r.source_text or "" for r in src_evs)

        is_replaces = bool(REPLACES_EVIDENCE_PATTERN.search(src_text))
        is_amends = bool(AMENDS_EVIDENCE_PATTERN.search(src_text))
        if not (is_replaces or is_amends):
            continue

        rel_type = (
            DocumentRelationshipType.REPLACES_DOCUMENT
            if is_replaces
            else DocumentRelationshipType.AMENDS_DOCUMENT
        )
        target_covs = [c for c in covs_with_ev if c.evidence and c.evidence.document_id != end_doc_id]
        candidate_doc_ids = tuple(dict.fromkeys(c.evidence.document_id for c in target_covs if c.evidence))

        if not candidate_doc_ids:
            edge = classify_document_relationship(
                edge_id=f"edge_{idx}_{end_doc_id}",
                source_document_id=end_doc_id,
                target_document_id=None,
                relationship_type=rel_type,
                classification=EdgeClassification.CONFIRMED_EXACT,
                evidence_references=tuple(src_evs),
                candidate_target_document_ids=(),
            )
        else:
            primary_tgt = None
            if end.applicable_policy and end.applicable_policy.strip():
                norm_ap = normalize_policy_identifier(end.applicable_policy)
                primary_tgt = next(
                    (c for c in target_covs if c.policy_number and normalize_policy_identifier(c.policy_number) == norm_ap),
                    None,
                )
            if primary_tgt is None:
                primary_tgt = target_covs[0]
            assert primary_tgt.evidence is not None
            tgt_doc_id = primary_tgt.evidence.document_id
            tgt_evs = [c.evidence for c in target_covs if c.evidence and c.evidence.document_id == tgt_doc_id]

            edge = classify_document_relationship(
                edge_id=f"edge_{idx}_{end_doc_id}",
                source_document_id=end_doc_id,
                target_document_id=tgt_doc_id,
                relationship_type=rel_type,
                classification=EdgeClassification.CONFIRMED_EXACT,
                evidence_references=tuple(src_evs + tgt_evs),
                associated_policy_id=primary_tgt.policy_number,
                target_policy_number=primary_tgt.policy_number,
                candidate_target_document_ids=candidate_doc_ids,
            )

        graph_edges.append(edge)
        if edge.requires_human_review:
            disc_cat = (
                DiscrepancyCategory.DOCUMENT_DISCREPANCY
                if edge.classification == EdgeClassification.CONFLICTING
                else DiscrepancyCategory.AMBIGUOUS_RELATIONSHIP
            )
            _add_discrepancy(
                DocumentDiscrepancy(
                    discrepancy_id=f"disc_rel_{idx}_{end_doc_id}",
                    category=disc_cat,
                    severity=Severity.MAJOR,
                    reason_code=edge.review_reason_code or "AMBIGUOUS_DOCUMENT_RELATIONSHIP",
                    description=(
                        f"Document relationship '{rel_type.value}' from '{end_doc_id}' could not be uniquely confirmed "
                        f"({edge.review_reason_code or edge.classification.value})."
                    ),
                    source_document_id=end_doc_id,
                    conflicting_document_id=edge.target_document_id,
                    evidence_chain=edge.evidence_references,
                    requires_human_review=True,
                )
            )

    # 5. Temporal Date & Contractual Duration Reconciliation
    reconciled_end_keys: Set[Tuple[Optional[str], Optional[str]]] = set()
    reconciled_pol_docs: Set[str] = set()

    def _get_compatible_cov_date_evidence(cov: CoverageExtraction) -> Tuple[Optional[str], Tuple[EvidenceReference, ...]]:
        ev_list: List[EvidenceReference] = []
        if cov.date_evidence:
            ev_list = [r for r in cov.date_evidence if _ev_compatible(r)]
        elif cov.evidence and _ev_compatible(cov.evidence) and _evidence_has_date_content((cov.evidence,)):
            ev_list = [cov.evidence]
        if not ev_list:
            return None, ()
        doc_id = ev_list[0].document_id
        same_doc_ev = tuple(r for r in ev_list if r.document_id == doc_id)
        return doc_id, same_doc_ev

    # 5a. Duration requirements (endorsement or policy)
    dur_reqs = [
        r for r in reqs
        if r.required_coverage_end_date is not None
        or r.requires_coverage_through_policy_expiration
        or r.rule_type == RequirementType.CONTRACTUAL_DURATION
    ]
    for r_idx, req in enumerate(dur_reqs):
        # Resolve unambiguous coverage if applicable
        cand_covs: List[CoverageExtraction] = []
        if req.coverage_type is not None:
            cand_covs = [c for c in document_set.coverages if c.coverage_type == req.coverage_type]
        elif req.required_endorsement_type is not None:
            matching_ends = [
                e for e in document_set.endorsements
                if e.endorsement_type == req.required_endorsement_type and not e.is_coi_checkbox_only
            ]
            end_pols = {e.applicable_policy.strip().upper() for e in matching_ends if e.applicable_policy and e.applicable_policy.strip()}
            if len(end_pols) == 1:
                tpol = next(iter(end_pols))
                cand_covs = [c for c in document_set.coverages if c.policy_number and c.policy_number.strip().upper() == tpol]
            elif not end_pols:
                cand_covs = list(document_set.coverages)

        sel_cov = cand_covs[0] if len(cand_covs) == 1 else None
        pol_doc_id: Optional[str] = None
        pol_eff: Optional[date] = None
        pol_exp: Optional[date] = None
        pol_ev: Tuple[EvidenceReference, ...] = ()
        if sel_cov is not None:
            pol_doc_id, pol_ev = _get_compatible_cov_date_evidence(sel_cov)
            if pol_doc_id and pol_ev:
                pol_eff = sel_cov.effective_date.date if sel_cov.effective_date else None
                pol_exp = sel_cov.expiration_date.date if sel_cov.expiration_date else None
                reconciled_pol_docs.add(pol_doc_id)

        if req.required_endorsement_type is not None:
            # Skip reconcile_dates when policy selection itself is ambiguous under coverage_type=None (handled by evaluator)
            if req.requires_coverage_through_policy_expiration and len(cand_covs) != 1 and req.required_coverage_end_date is None:
                continue

            matching_ends = [
                e for e in document_set.endorsements
                if e.endorsement_type == req.required_endorsement_type and not e.is_coi_checkbox_only
            ]
            if matching_ends:
                end = matching_ends[0]
                raw_end_ev = [
                    r for r in (list(end.date_evidence) + ([end.evidence] if end.evidence else []))
                    if _ev_compatible(r) and r.document_type == end.document_type
                ]
                end_ev_docs = {r.document_id for r in raw_end_ev}
                if len(end_ev_docs) > 1:
                    continue
                end_doc_id = next(iter(end_ev_docs)) if end_ev_docs else None
                end_ev_tuple = tuple(raw_end_ev)
                if end_doc_id is None:
                    # Wrong-doc evidence or no evidence: handled by evaluator without violating manifest
                    continue

                eff_end_date = req.required_coverage_end_date
                use_pol_span = req.requires_coverage_through_policy_expiration
                if use_pol_span and pol_doc_id is None and sel_cov and sel_cov.expiration_date:
                    eff_end_date = max(eff_end_date, sel_cov.expiration_date.date) if eff_end_date else sel_cov.expiration_date.date
                    use_pol_span = False

                try:
                    drec = reconcile_dates(
                        policy_document_id=pol_doc_id,
                        policy_effective_date=pol_eff,
                        policy_expiration_date=pol_exp,
                        policy_date_evidence=pol_ev,
                        endorsement_document_id=end_doc_id,
                        endorsement_effective_date=end.effective_date,
                        endorsement_expiration_date=end.expiration_date,
                        endorsement_date_evidence=end_ev_tuple,
                        evaluation_date=evaluation_date,
                        evidence_chain=pol_ev + end_ev_tuple,
                        contractual_duration_requirement_id=req.requirement_id,
                        required_coverage_end_date=eff_end_date,
                        requires_coverage_through_policy_expiration=use_pol_span,
                        is_duration_requirement_validated=req.is_duration_requirement_validated,
                    )
                    date_records.append(drec)
                    reconciled_end_keys.add((end_doc_id, req.requirement_id))
                    if drec.requires_human_review and drec.review_reason_code:
                        cat = drec.discrepancy_category or DiscrepancyCategory.DOCUMENT_DISCREPANCY
                        sev = Severity.CRITICAL if cat == DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION else req.severity
                        _add_discrepancy(
                            DocumentDiscrepancy(
                                discrepancy_id=f"disc_dur_{req.requirement_id}_{end_doc_id}",
                                category=cat,
                                severity=sev,
                                reason_code=drec.review_reason_code,
                                description=(
                                    f"Duration date reconciliation for '{req.requirement_id}' requires review "
                                    f"({drec.review_reason_code})."
                                ),
                                source_document_id=end_doc_id,
                                conflicting_document_id=pol_doc_id if (pol_doc_id and pol_doc_id != end_doc_id) else None,
                                applicable_requirement_id=req.requirement_id,
                                applicable_coverage_type=req.coverage_type or (sel_cov.coverage_type if sel_cov else CoverageType.GENERAL_LIABILITY),
                                evidence_chain=pol_ev + end_ev_tuple,
                                requires_human_review=True,
                            )
                        )
                except ValueError:
                    pass
            elif pol_doc_id is not None:
                # Missing required endorsement while COI policy dates exist
                try:
                    drec = reconcile_dates(
                        policy_document_id=pol_doc_id,
                        policy_effective_date=pol_eff,
                        policy_expiration_date=pol_exp,
                        policy_date_evidence=pol_ev,
                        endorsement_document_id=None,
                        endorsement_effective_date=None,
                        endorsement_expiration_date=None,
                        endorsement_date_evidence=(),
                        evaluation_date=evaluation_date,
                        evidence_chain=pol_ev,
                        contractual_duration_requirement_id=req.requirement_id,
                        required_coverage_end_date=req.required_coverage_end_date,
                        requires_coverage_through_policy_expiration=req.requires_coverage_through_policy_expiration,
                        is_duration_requirement_validated=req.is_duration_requirement_validated,
                    )
                    date_records.append(drec)
                    if drec.requires_human_review and drec.review_reason_code:
                        _add_discrepancy(
                            DocumentDiscrepancy(
                                discrepancy_id=f"disc_dur_missing_{req.requirement_id}_{pol_doc_id}",
                                category=drec.discrepancy_category or DiscrepancyCategory.MISSING_EVIDENCE,
                                severity=req.severity,
                                reason_code=drec.review_reason_code,
                                description=(
                                    f"Required endorsement document missing for duration requirement '{req.requirement_id}'."
                                ),
                                source_document_id=pol_doc_id,
                                applicable_requirement_id=req.requirement_id,
                                applicable_coverage_type=req.coverage_type or (sel_cov.coverage_type if sel_cov else CoverageType.GENERAL_LIABILITY),
                                evidence_chain=pol_ev,
                                requires_human_review=True,
                            )
                        )
                except ValueError:
                    pass

    # 5b. Policy date-evidence validation when POLICY_EXPIRATION / POLICY_EFFECTIVE_DATE has date-bearing evidence
    date_reqs = [
        r for r in reqs
        if r.rule_type in (RequirementType.POLICY_EXPIRATION, RequirementType.POLICY_EFFECTIVE_DATE, RequirementType.CONTRACTUAL_DURATION)
        and r.coverage_type is not None
        and r.required_endorsement_type is None
    ]
    for req in date_reqs:
        cov = next((c for c in document_set.coverages if c.coverage_type == req.coverage_type), None)
        if cov is None:
            continue
        pol_doc_id, pol_ev = _get_compatible_cov_date_evidence(cov)
        if not pol_doc_id or pol_doc_id in reconciled_pol_docs:
            continue
        pol_eff = cov.effective_date.date if cov.effective_date else None
        pol_exp = cov.expiration_date.date if cov.expiration_date else None
        try:
            drec = reconcile_dates(
                policy_document_id=pol_doc_id,
                policy_effective_date=pol_eff,
                policy_expiration_date=pol_exp,
                policy_date_evidence=pol_ev,
                endorsement_document_id=None,
                endorsement_effective_date=None,
                endorsement_expiration_date=None,
                endorsement_date_evidence=(),
                evaluation_date=evaluation_date,
                evidence_chain=pol_ev,
                contractual_duration_requirement_id=None,
            )
            date_records.append(drec)
            reconciled_pol_docs.add(pol_doc_id)
            if drec.requires_human_review and drec.review_reason_code:
                cat = drec.discrepancy_category or DiscrepancyCategory.DOCUMENT_DISCREPANCY
                if cat == DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION:
                    cat = DiscrepancyCategory.DOCUMENT_DISCREPANCY
                _add_discrepancy(
                    DocumentDiscrepancy(
                        discrepancy_id=f"disc_poldate_{req.requirement_id}_{pol_doc_id}",
                        category=cat,
                        severity=req.severity,
                        reason_code=drec.review_reason_code,
                        description=(
                            f"Policy date reconciliation on '{pol_doc_id}' requires review "
                            f"({drec.review_reason_code})."
                        ),
                        source_document_id=pol_doc_id,
                        applicable_requirement_id=req.requirement_id,
                        applicable_coverage_type=cov.coverage_type,
                        evidence_chain=pol_ev,
                        requires_human_review=True,
                    )
                )
        except ValueError:
            pass

    # 5c. Standalone endorsement dates (when endorsement carries dates outside duration requirements)
    for end in document_set.endorsements:
        if end.is_coi_checkbox_only or (end.effective_date is None and end.expiration_date is None):
            continue
        raw_end_ev = [
            r for r in (list(end.date_evidence) + ([end.evidence] if end.evidence else []))
            if _ev_compatible(r) and r.document_type == end.document_type
        ]
        end_ev_docs = {r.document_id for r in raw_end_ev}
        if len(end_ev_docs) != 1:
            continue
        end_doc_id = next(iter(end_ev_docs))
        if any(k[0] == end_doc_id for k in reconciled_end_keys):
            continue
        end_ev_tuple = tuple(raw_end_ev)
        try:
            drec = reconcile_dates(
                policy_document_id=None,
                policy_effective_date=None,
                policy_expiration_date=None,
                policy_date_evidence=(),
                endorsement_document_id=end_doc_id,
                endorsement_effective_date=end.effective_date,
                endorsement_expiration_date=end.expiration_date,
                endorsement_date_evidence=end_ev_tuple,
                evaluation_date=evaluation_date,
                evidence_chain=end_ev_tuple,
                contractual_duration_requirement_id=None,
            )
            date_records.append(drec)
            reconciled_end_keys.add((end_doc_id, None))
            if drec.requires_human_review and drec.review_reason_code:
                cat = drec.discrepancy_category or DiscrepancyCategory.DOCUMENT_DISCREPANCY
                if cat == DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION:
                    cat = DiscrepancyCategory.DOCUMENT_DISCREPANCY
                _add_discrepancy(
                    DocumentDiscrepancy(
                        discrepancy_id=f"disc_enddate_{end_doc_id}",
                        category=cat,
                        severity=Severity.MAJOR,
                        reason_code=drec.review_reason_code,
                        description=(
                            f"Endorsement date reconciliation on '{end_doc_id}' requires review "
                            f"({drec.review_reason_code})."
                        ),
                        source_document_id=end_doc_id,
                        applicable_coverage_type=CoverageType.GENERAL_LIABILITY,
                        evidence_chain=end_ev_tuple,
                        requires_human_review=True,
                    )
                )
        except ValueError:
            pass

    if not (graph_edges or entity_records or policy_records or date_records or discrepancies):
        return None

    engine = DeterministicReconciliationEngine()
    pkt_id = packet_id or f"pkt_{document_set.subcontractor_id}"
    return engine.reconcile_packet(
        document_packet_id=pkt_id,
        packet_manifest=manifest_entries,
        primary_coi_id=primary_coi_id,
        graph_edges=tuple(graph_edges),
        entity_records=tuple(entity_records),
        policy_records=tuple(policy_records),
        date_records=tuple(date_records),
        missing_evidence_records=(),
        discrepancies=tuple(discrepancies),
    )


__all__ = [
    "reconcile_policy_identifiers",
    "reconcile_entity_identities",
    "check_contractual_role_satisfaction",
    "reconcile_dates",
    "classify_document_relationship",
    "DeterministicReconciliationEngine",
    "reconcile_extracted_document_set",
]
