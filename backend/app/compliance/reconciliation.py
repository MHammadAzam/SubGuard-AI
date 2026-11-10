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

import re
from datetime import date
from typing import Dict, List, Optional, Set, Tuple

from app.compliance.evaluator import _clean_entity_name, _split_core_and_suffix, compare_entity_names
from app.schemas.insurance import (
    CoverageType,
    DocumentType,
    EvidenceReference,
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


def _evidence_supports_date(
    target_date: Optional[date],
    evidence_refs: Tuple[EvidenceReference, ...],
    date_field: str,  # "effective" or "expiration"
    doc_id: Optional[str] = None,
) -> bool:
    """Verify that at least one evidence reference semantically supports target_date for date_field.
    
    Semantic validation rules:
    - Associate the extracted date with its semantic field or label.
    - Distinguish effective dates from expiration dates.
    - Reject dates that occur only in unrelated contexts (invoice, payment, prior policy, billed, quote).
    - Ensure evidence references belong to expected document (doc_id).
    - If date cannot be confidently associated with its field, returns False (requires human review).
    - Disallows ambiguous numeric date transposition (cannot assume day-first without unambiguous text).
    - Rejects ambiguous numeric dates (including 2-digit years) unless document evidence establishes convention.
    """
    if target_date is None:
        return True
    if not evidence_refs:
        return False

    year = target_date.year
    yy = str(year)[2:]
    month = target_date.month
    day = target_date.day

    month_names_full = [
        "", "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
    ]
    month_names_abbr = [
        "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
    ]
    full_m = month_names_full[month]
    abbr_m = month_names_abbr[month]
    m_group = rf"(?:{full_m}|{abbr_m}\.?|sept\.)"

    # 1. Unambiguous ISO date patterns: YYYY-MM-DD or YYYY/MM/DD (4-digit year first)
    iso_patterns = [
        rf"\b{year}[-/]0?{month}[-/]0?{day}\b",
    ]
    # 2. Unambiguous textual month patterns (month written as name/abbreviation)
    textual_patterns = [
        rf"\b{m_group}\s+0?{day}(?:st|nd|rd|th)?,?\s+(?:{year}|{yy})\b",
        rf"\b0?{day}(?:st|nd|rd|th)?[\s/-]+{m_group}[\s/,-]+(?:{year}|{yy})\b",
        rf"\b{year}[\s/-]+{m_group}[\s/-]+0?{day}(?:st|nd|rd|th)?\b",
    ]

    for ref in evidence_refs:
        if doc_id is not None and ref.document_id != doc_id:
            continue
        text = (ref.source_text or "").strip()
        if not text:
            continue

        convention = _detect_date_convention(text)
        numeric_patterns = []

        if convention == "MM/DD":
            # MM/DD convention established by document: accept 4-digit and 2-digit years
            numeric_patterns.append(rf"\b0?{month}[/.-]0?{day}[/.-](?:{year}|{yy})\b")
        elif convention == "DD/MM":
            # DD/MM convention established by document
            numeric_patterns.append(rf"\b0?{day}[/.-]0?{month}[/.-](?:{year}|{yy})\b")
        else:
            # When date convention is not explicitly established:
            # - Two-digit year numeric dates (e.g. 01/02/25) are rejected as ambiguous
            # - Standard 4-digit year dates in US MM/DD/YYYY format are accepted
            # - Day-first transposition (DD/MM/YYYY) is NOT accepted without explicit DD/MM convention
            numeric_patterns.append(rf"\b0?{month}[/.-]0?{day}[/.-]{year}\b")

        active_patterns = iso_patterns + textual_patterns + numeric_patterns
        if not active_patterns:
            continue

        date_regex = re.compile("|".join(f"(?:{p})" for p in active_patterns), re.IGNORECASE)

        for m in date_regex.finditer(text):
            m_start, m_end = m.start(), m.end()
            prefix = text[max(0, m_start - 80):m_start]
            suffix = text[m_end:min(len(text), m_end + 80)]

            # 1. Unrelated context check
            unrelated_match = None
            for um in UNRELATED_DATE_CONTEXT_REGEX.finditer(prefix):
                unrelated_match = um
            if unrelated_match:
                # Check if there is a closer policy/effective/expiration label
                eff_m = list(EFFECTIVE_LABEL_REGEX.finditer(prefix))
                exp_m = list(EXPIRATION_LABEL_REGEX.finditer(prefix))
                last_eff_pos = eff_m[-1].start() if eff_m else -1
                last_exp_pos = exp_m[-1].start() if exp_m else -1
                last_field_pos = max(last_eff_pos, last_exp_pos)
                if unrelated_match.start() > last_field_pos:
                    # Unrelated label is closer to the date than any field label
                    continue

            # 2. Date Range association:
            # Check if this date is part of "Date_1 to Date_2" or "Date_1 - Date_2"
            is_start_of_range = bool(re.match(r"^\s*(?:to|through|until|\u2013|\u2014|-)\s*(?:[A-Za-z0-9/.-]+)", suffix, re.IGNORECASE))
            is_end_of_range = bool(re.search(r"(?:to|through|until|\u2013|\u2014|-)\s*$", prefix, re.IGNORECASE))

            if is_start_of_range:
                # Start of range corresponds to effective date
                if date_field == "effective":
                    return True
                else:
                    # Not expiration date
                    continue

            if is_end_of_range:
                # End of range corresponds to expiration date
                if date_field == "expiration":
                    return True
                else:
                    # Not effective date
                    continue

            # 3. Explicit Label association
            eff_matches = list(EFFECTIVE_LABEL_REGEX.finditer(prefix))
            exp_matches = list(EXPIRATION_LABEL_REGEX.finditer(prefix))
            last_eff_end = eff_matches[-1].end() if eff_matches else -1
            last_exp_end = exp_matches[-1].end() if exp_matches else -1

            if date_field == "effective":
                if last_eff_end > last_exp_end and last_eff_end != -1:
                    return True
            elif date_field == "expiration":
                if last_exp_end > last_eff_end and last_exp_end != -1:
                    return True

    return False


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


__all__ = [
    "reconcile_policy_identifiers",
    "reconcile_entity_identities",
    "check_contractual_role_satisfaction",
    "reconcile_dates",
    "classify_document_relationship",
    "DeterministicReconciliationEngine",
]
