"""Data contracts for cross-document reconciliation, relationship graphs, and provenance.

Architectural Invariant:
'AI extracts information. Deterministic Python rules evaluate it. Humans resolve uncertainty.'
"""
from __future__ import annotations

import re
from datetime import date
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.insurance import (
    CoverageType,
    DocumentType,
    EvidenceReference,
    Severity,
)

# =============================================================================
# Helper Algorithms
# =============================================================================

def normalize_policy_identifier(policy_num: Optional[str]) -> str:
    r"""Safely normalize policy identifier by stripping standard delimiter characters.
    
    Permitted normalization rule:
    - Strips whitespace, hyphens, slashes, and periods: [\s\-\/\.]
    - Converts to uppercase.
    - Preserves all alphanumeric characters, leading zeros, sequence numbers,
      and renewal/term suffixes.
    - Does NOT remove leading zeros (e.g. '0012345' is NOT converted to '12345').
    - Does NOT remove renewal suffixes (e.g. '-01', '-02' are preserved).
    """
    if not policy_num:
        return ""
    # Strip whitespace, hyphens, slashes, periods
    cleaned = re.sub(r"[\s\-\/\.]", "", policy_num.strip().upper())
    return cleaned


def validate_evidence_reference(
    ref: EvidenceReference,
    manifest_map: Dict[str, DocumentManifestEntry],
    record_context: str = "Evidence",
) -> None:
    """Validate referential integrity of an EvidenceReference against the packet manifest."""
    if ref.document_id not in manifest_map:
        raise ValueError(
            f"{record_context} evidence document_id '{ref.document_id}' not in packet_manifest."
        )
    doc_entry = manifest_map[ref.document_id]
    if not (1 <= ref.page_number <= doc_entry.page_count):
        raise ValueError(
            f"{record_context} evidence page {ref.page_number} out of bounds for doc '{ref.document_id}' (max: {doc_entry.page_count})."
        )
    if ref.document_type != doc_entry.detected_document_type:
        raise ValueError(
            f"{record_context} evidence document_type '{ref.document_type}' does not match "
            f"manifest detected_document_type '{doc_entry.detected_document_type}' for doc '{ref.document_id}'."
        )


SUFFIX_FORMAT_MAP = {
    "llc": "llc",
    "l.l.c.": "llc",
    "l.l.c": "llc",
    "inc": "inc",
    "inc.": "inc",
    "incorporated": "inc",
    "corp": "corp",
    "corp.": "corp",
    "corporation": "corp",
    "co": "co",
    "co.": "co",
    "company": "co",
    "ltd": "ltd",
    "ltd.": "ltd",
    "limited": "ltd",
    "pllc": "pllc",
    "p.l.l.c.": "pllc",
    "lp": "lp",
    "l.p.": "lp",
}


def _split_core_and_suffix(name: str) -> Tuple[str, Optional[str]]:
    """Split an entity name into its core words and recognized legal suffix type."""
    cleaned = re.sub(r"[,\'\"]", "", name.strip()).lower()
    tokens = cleaned.split()
    if not tokens:
        return "", None
    last = tokens[-1]
    if last in SUFFIX_FORMAT_MAP:
        return " ".join(tokens[:-1]), SUFFIX_FORMAT_MAP[last]
    return " ".join(tokens), None


def _clean_entity_name(name: str) -> str:
    """Strip common punctuation, corporate suffix periods, and standardize whitespace."""
    cleaned = name.strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip(",'\"")
    return cleaned.strip()


COMMON_LEGAL_SUFFIXES = {
    "llc", "inc", "corp", "co", "ltd", "pllc", "lp", "pc", "pa",
    "incorporated", "corporation", "company", "limited", "partnership",
}

COMMON_ROLE_TERMS = {
    "insured", "additional", "named", "holder", "certificate", "contractor",
    "subcontractor", "owner", "lessee", "vendor",
}

COMMON_LINKING_TERMS = {
    "is", "are", "shall", "be", "as", "an", "a", "the", "and", "or", "by", "for",
    "at", "on", "in", "to", "from", "with", "included", "named", "designated",
    "herein", "hereunder", "below", "above", "policy", "endorsement", "form",
}


def _evidence_supports_entity(
    entity_name: Optional[str],
    evidence_refs: Tuple[EvidenceReference, ...],
    doc_id: Optional[str] = None,
) -> bool:
    """Verify that at least one evidence reference semantically supports entity_name.

    Semantic validation rules:
    - Rejects citations where entity name tokens match only as disjoint/unordered tokens.
    - Requires meaningful contiguous phrase matching of core entity tokens.
    - Uses strict word boundaries to avoid partial substring matches (e.g. 'Ace' in 'Palace').
    - Distinguishes different entities sharing a common first token (e.g. 'Apex Electrical' vs 'Apex Plumbing').
    - Distinguishes different entities sharing generic words (e.g. 'National Fire Protection' vs 'National Water Protection').
    - Accepts legitimate legal-name formatting variations (punctuation, suffix formatting, optional suffix omission).
    - Rejects references that belong to an unexpected document ID when doc_id is provided.
    """
    if not entity_name or not entity_name.strip():
        return False
    if not evidence_refs:
        return False

    cleaned_name = _clean_entity_name(entity_name)
    core, suff = _split_core_and_suffix(cleaned_name)
    target_core = core if core else cleaned_name

    # Tokenize core into substantive words, ignoring non-distinguishing initial articles
    core_tokens = [w for w in re.findall(r"[A-Za-z0-9]+", target_core.lower()) if w not in ("the", "a", "an")]
    if not core_tokens:
        core_tokens = [w for w in re.findall(r"[A-Za-z0-9]+", target_core.lower())]
    if not core_tokens:
        return False

    token_patterns = [re.escape(t) for t in core_tokens]
    core_pattern_str = r"\b" + r"(?:[\s,\.\-]+|\s*&\s*)".join(token_patterns) + r"\b"
    core_regex = re.compile(core_pattern_str, re.IGNORECASE)

    for ref in evidence_refs:
        if doc_id is not None and ref.document_id != doc_id:
            continue
        text = ref.source_text or ""
        if not text.strip():
            continue

        for m in core_regex.finditer(text):
            # Check what immediately follows the match
            post_text = text[m.end():]
            post_words = re.findall(r"[A-Za-z0-9]+", post_text)
            
            # Check what immediately precedes the match
            pre_text = text[:m.start()]
            pre_words = re.findall(r"[A-Za-z0-9]+", pre_text)

            # Preceding word check: if preceded immediately by an unrecognized substantive noun
            if pre_words:
                last_pre = pre_words[-1].lower()
                dist_pre = m.start() - (text[:m.start()].rfind(pre_words[-1]) + len(pre_words[-1]))
                intervening_pre = text[m.start() - dist_pre:m.start()]
                if dist_pre <= 3:
                    if "," in intervening_pre or ";" in intervening_pre or ":" in intervening_pre:
                        pass
                    elif (
                        last_pre not in COMMON_ROLE_TERMS
                        and last_pre not in COMMON_LINKING_TERMS
                        and last_pre not in COMMON_LEGAL_SUFFIXES
                    ):
                        continue

            # Following word check:
            if post_words:
                first_post = post_words[0].lower()
                dist_post = text[m.end():].find(post_words[0])
                if dist_post <= 3:
                    if first_post.isdigit():
                        return True
                    if (
                        first_post in COMMON_LEGAL_SUFFIXES
                        or first_post in COMMON_ROLE_TERMS
                        or first_post in COMMON_LINKING_TERMS
                    ):
                        return True
                    else:
                        # Followed by a substantive distinguishing noun not in our entity name
                        continue

            return True

    return False


# =============================================================================
# Enums
# =============================================================================

class EntityRole(str, Enum):
    """Explicit functional role of an entity in insurance documentation."""
    NAMED_INSURED = "named_insured"
    ADDITIONAL_INSURED = "additional_insured"
    CERTIFICATE_HOLDER = "certificate_holder"
    GENERAL_CONTRACTOR = "general_contractor"
    PRODUCER = "producer"
    OTHER = "other"


def is_role_compatible(role_a: EntityRole, role_b: EntityRole) -> bool:
    """Determine whether two entity roles are legally and contextually comparable.
    
    Incompatible comparisons (e.g. comparing a Producer or Certificate Holder
    to a Named Insured as equivalent parties) are strictly rejected.
    """
    valid_document_role_pairs = {
        (EntityRole.NAMED_INSURED, EntityRole.NAMED_INSURED),
        (EntityRole.ADDITIONAL_INSURED, EntityRole.ADDITIONAL_INSURED),
        (EntityRole.CERTIFICATE_HOLDER, EntityRole.CERTIFICATE_HOLDER),
        (EntityRole.PRODUCER, EntityRole.PRODUCER),
        (EntityRole.GENERAL_CONTRACTOR, EntityRole.GENERAL_CONTRACTOR),
        (EntityRole.CERTIFICATE_HOLDER, EntityRole.ADDITIONAL_INSURED),
        (EntityRole.ADDITIONAL_INSURED, EntityRole.CERTIFICATE_HOLDER),
    }
    return (role_a, role_b) in valid_document_role_pairs


class PolicyMatchClassification(str, Enum):
    """Rigorous 5-tier classification for cross-document policy identifiers."""
    EXACT = "exact"                                  # Character-for-character identical
    FORMATTING_NORMALIZED = "formatting_normalized"  # Normalized whitespace/delimiters
    POSSIBLE_FAMILY = "possible_family"              # Common base identifier with differing renewal/term suffix
    AMBIGUOUS = "ambiguous"                          # Truncated, missing, or multiple conflicting policy numbers
    CONFLICTING = "conflicting"                      # Distinct, mutually exclusive policy numbers


class EntityMatchClassification(str, Enum):
    """Deterministic classification of entity relationship across documents."""
    EXACT = "exact"                                  # Character-for-character identical
    FORMATTING_NORMALIZED = "formatting_normalized"  # Legal suffix punctuation normalized
    CONFIRMED_DBA = "confirmed_dba"                  # Trade name explicitly substantiated by verbatim text evidence
    LEGAL_SUFFIX_DISCREPANCY = "legal_suffix_discrepancy" # Corporate suffix difference alone (LLC vs Inc); review required
    DISTINCT_LEGAL_ENTITY = "distinct_legal_entity"  # Affirmative evidence proves distinct legal entities
    AMBIGUOUS_IDENTITY = "ambiguous_identity"        # Spelling variation or unverified DBA relationship
    CONFIRMED_MISMATCH = "confirmed_mismatch"        # Unambiguously proven distinct entity failing contract requirement


class DocumentRelationshipType(str, Enum):
    """Categorization of edges in the Document Relationship Graph."""
    SAME_POLICY = "same_policy"                      # COI coverage line to Dec Page for same policy
    ENDORSEMENT_TO_POLICY = "endorsement_to_policy"  # Endorsement schedule explicitly referencing policy/coverage
    DECLARATION_FOR_POLICY = "declaration_for_policy"# Dec page detailing coverage terms for a policy
    REPLACES_DOCUMENT = "replaces_document"          # Directional: source replaces target
    AMENDS_DOCUMENT = "amends_document"              # Directional: source amends target
    DUPLICATE_OF = "duplicate_of"                    # Directional: source is duplicate of canonical target
    UNRELATED = "unrelated"                          # Distinct, unlinked document
    ORPHAN_DOCUMENT = "orphan_document"              # Standalone document in packet with no COI/policy linkage


REPLACES_EVIDENCE_PATTERN = re.compile(
    r"\b(replaces|supersedes|cancels\s+and\s+replaces)\s+(prior|previous|existing|form|endorsement|policy|schedule|document)\b",
    re.IGNORECASE,
)
AMENDS_EVIDENCE_PATTERN = re.compile(
    r"\b(amends|modifies|changes)\s+(the\s+)?(policy|endorsement|coverage|terms|insurance\s+provided|contract)\b",
    re.IGNORECASE,
)
DUPLICATE_EVIDENCE_PATTERN = re.compile(
    r"\b(duplicate\s+copy|duplicate\s+original|identical\s+(sha256\s+)?hash|conformed\s+copy|verbatim\s+duplicate|identical\s+copy)\b",
    re.IGNORECASE,
)
DECLARATION_EVIDENCE_PATTERN = re.compile(
    r"\b(declarations(\s+page)?|policy\s+declarations|common\s+policy\s+declarations|coverage\s+declarations|schedule\s+of\s+coverages|item\s+1\.?\s+named\s+insured)\b",
    re.IGNORECASE,
)
ENDORSEMENT_EVIDENCE_PATTERN = re.compile(
    r"\b(endorsement|forms\s+a\s+part\s+of|attached\s+to\s+and\s+forms\s+part\s+of|this\s+endorsement\s+(modifies|changes)|endorsement\s+schedule)\b",
    re.IGNORECASE,
)
POLICY_COVERAGE_EVIDENCE_PATTERN = re.compile(
    r"\b(policy\s*(number|no|#)?|coverage\s*(part|form)?|commercial\s+general\s+liability|general\s+liability|declarations)\b",
    re.IGNORECASE,
)

# Specific target identifier extraction patterns (WP2)
POLICY_NUMBER_PATTERN = re.compile(
    r"\b(?:policy\s*(?:no\.?|number|#)?\s*[:\s]*)([A-Za-z0-9\-]+)\b",
    re.IGNORECASE,
)
FORM_NUMBER_PATTERN = re.compile(
    r"\b((?:CG|BP|CA|IL|WC|CP|GL)\s*\d{2}\s*\d{2}(?:\s*\d{2}\s*\d{2})?)\b",
    re.IGNORECASE,
)
ENDORSEMENT_NUMBER_PATTERN = re.compile(
    r"\b(?:endorsement\s*(?:no\.?|number|#)?\s*[:\s]*)([A-Za-z0-9\-]+)\b",
    re.IGNORECASE,
)


def _extract_document_identifiers(text: str) -> Dict[str, Set[str]]:
    """Extract policy numbers, form numbers, endorsement numbers, and document IDs from text."""
    if not text:
        return {"policies": set(), "forms": set(), "endorsements": set()}

    policies: Set[str] = set()
    for m in POLICY_NUMBER_PATTERN.finditer(text):
        val = m.group(1).strip()
        if val.lower() not in (
            "period", "term", "effective", "expiration", "holder", "number", "no", "date",
            "provided", "clause", "for", "of", "under", "with", "in", "to", "at", "from",
            "by", "on", "coverage", "form", "the", "a", "an", "and", "or", "is", "are",
        ):
            policies.add(val.upper())

    forms: Set[str] = set()
    for m in FORM_NUMBER_PATTERN.finditer(text):
        forms.add(re.sub(r"\s+", " ", m.group(1).strip()).upper())

    form_phrase = re.findall(
        r"\bform\s*(?:no\.?|number|#)?\s*([A-Za-z0-9\s\-]+?)(?:\s+(?:under|for|dated|effective|edition)|\.|\n|,|$)",
        text,
        re.I,
    )
    for fp in form_phrase:
        cleaned_fp = re.sub(r"\s+", " ", fp.strip()).upper()
        if cleaned_fp and len(cleaned_fp) <= 25 and cleaned_fp.lower() not in ("period", "effective", "the policy", "prior form"):
            forms.add(cleaned_fp)

    endorsements: Set[str] = set()
    for m in ENDORSEMENT_NUMBER_PATTERN.finditer(text):
        val = m.group(1).strip()
        if val.lower() not in (
            "schedule", "period", "date", "effective", "form", "number", "no",
            "modifies", "replaces", "amends", "changes", "cancels", "forms",
            "attached", "dated", "is", "shall", "does", "coverage", "terms",
            "the", "a", "an", "hereby", "under", "for", "with", "to",
        ):
            endorsements.add(val.upper())

    return {
        "policies": policies,
        "forms": forms,
        "endorsements": endorsements,
    }


def _validate_target_linkage(
    relationship_type: DocumentRelationshipType,
    source_document_id: str,
    target_document_id: Optional[str],
    evidence_references: Tuple[EvidenceReference, ...],
    associated_policy_id: Optional[str] = None,
    target_policy_number: Optional[str] = None,
    target_document_number: Optional[str] = None,
    target_form_number: Optional[str] = None,
    candidate_target_document_ids: Optional[Tuple[str, ...]] = None,
) -> Tuple[bool, Optional[str], EdgeClassification]:
    """Validate that source document text actually refers to the target document.

    Guarantees (WP2):
    - Identify source and referenced target documents.
    - Establish that source text actually refers to that target.
    - Validate target identifiers (policy numbers, form numbers, endorsement IDs).
    - Reject generic wording (e.g. 'this endorsement amends the policy') as insufficient.
    - Reject source referencing different policy number than target (conflict).
    - If multiple candidate targets exist and cannot be uniquely resolved, return AMBIGUOUS.
    - If target document is missing, return AMBIGUOUS.
    - If conflicting target identifiers exist, return CONFLICTING.
    - Unrelated document evidence cannot establish target relationship.
    - Weak name/date similarity alone cannot establish unique target.
    """
    if not target_document_id or not target_document_id.strip():
        return False, "MISSING_TARGET_DOCUMENT", EdgeClassification.AMBIGUOUS

    doc_ids = {ref.document_id for ref in evidence_references}
    if target_document_id not in doc_ids:
        return False, "MISSING_TARGET_DOCUMENT", EdgeClassification.AMBIGUOUS
    if source_document_id not in doc_ids:
        return False, "INSUFFICIENT_RELATIONSHIP_EVIDENCE", EdgeClassification.AMBIGUOUS

    unrelated = [r for r in evidence_references if r.document_id not in (source_document_id, target_document_id)]
    if unrelated:
        return False, "UNSUPPORTED_RELATIONSHIP_EVIDENCE", EdgeClassification.AMBIGUOUS

    src_text = " ".join((ref.source_text or "") for ref in evidence_references if ref.document_id == source_document_id)
    tgt_text = " ".join((ref.source_text or "") for ref in evidence_references if ref.document_id == target_document_id)

    if relationship_type == DocumentRelationshipType.REPLACES_DOCUMENT:
        if not REPLACES_EVIDENCE_PATTERN.search(src_text):
            return False, "UNSUPPORTED_RELATIONSHIP_ASSERTION", EdgeClassification.AMBIGUOUS
    elif relationship_type == DocumentRelationshipType.AMENDS_DOCUMENT:
        if not AMENDS_EVIDENCE_PATTERN.search(src_text):
            return False, "UNSUPPORTED_RELATIONSHIP_ASSERTION", EdgeClassification.AMBIGUOUS

    src_ids = _extract_document_identifiers(src_text)
    tgt_ids = _extract_document_identifiers(tgt_text)

    if associated_policy_id:
        tgt_ids["policies"].add(associated_policy_id.strip().upper())
    if target_policy_number:
        tgt_ids["policies"].add(target_policy_number.strip().upper())
    if target_form_number:
        tgt_ids["forms"].add(target_form_number.strip().upper())
    if target_document_number:
        tgt_ids["forms"].add(target_document_number.strip().upper())

    direct_doc_id_ref = bool(re.search(rf"\b{re.escape(target_document_id)}\b", src_text, re.IGNORECASE))

    # Check for generic wording without target identity
    has_specific_target_id_in_src = bool(
        src_ids["policies"] or src_ids["forms"] or src_ids["endorsements"] or direct_doc_id_ref
    )
    if not has_specific_target_id_in_src:
        return False, "GENERIC_RELATIONSHIP_WITHOUT_TARGET_IDENTITY", EdgeClassification.AMBIGUOUS

    # Check for multiple candidate target documents
    if candidate_target_document_ids and len(candidate_target_document_ids) > 1:
        if not direct_doc_id_ref:
            return False, "AMBIGUOUS_MULTIPLE_TARGET_DOCUMENTS", EdgeClassification.AMBIGUOUS

    # Conflict check: source specifies policy, but target has different policy
    if src_ids["policies"] and tgt_ids["policies"]:
        policy_matches = any(
            normalize_policy_identifier(p_src) == normalize_policy_identifier(p_tgt)
            for p_src in src_ids["policies"]
            for p_tgt in tgt_ids["policies"]
        )
        if not policy_matches:
            return False, "TARGET_IDENTIFIER_MISMATCH", EdgeClassification.CONFLICTING

    # Conflict check: source specifies form, but target has different form
    if src_ids["forms"] and tgt_ids["forms"]:
        form_matches = any(
            f_src == f_tgt or f_src in f_tgt or f_tgt in f_src
            for f_src in src_ids["forms"]
            for f_tgt in tgt_ids["forms"]
        )
        if not form_matches:
            return False, "CONFLICTING_TARGET_IDENTIFIERS", EdgeClassification.CONFLICTING

    # Conflict check: source specifies endorsement ID, but target has different endorsement ID
    if src_ids["endorsements"] and tgt_ids["endorsements"]:
        if not (src_ids["endorsements"] & tgt_ids["endorsements"]):
            return False, "CONFLICTING_TARGET_IDENTIFIERS", EdgeClassification.CONFLICTING

    # Target linkage match confirmation
    policy_match = bool(
        src_ids["policies"] and tgt_ids["policies"] and
        any(
            normalize_policy_identifier(p_src) == normalize_policy_identifier(p_tgt)
            for p_src in src_ids["policies"]
            for p_tgt in tgt_ids["policies"]
        )
    )
    form_match = bool(
        src_ids["forms"] and tgt_ids["forms"] and
        any(
            f_src == f_tgt or f_src in f_tgt or f_tgt in f_src
            for f_src in src_ids["forms"]
            for f_tgt in tgt_ids["forms"]
        )
    )
    endorsement_match = bool(src_ids["endorsements"] and (src_ids["endorsements"] & tgt_ids["endorsements"]))

    if policy_match or form_match or endorsement_match or direct_doc_id_ref:
        return True, None, EdgeClassification.CONFIRMED_EXACT

    # Weak name/date similarity alone cannot establish unique target
    return False, "INSUFFICIENT_TARGET_IDENTITY_EVIDENCE", EdgeClassification.AMBIGUOUS


class EdgeClassification(str, Enum):
    """Explicit typed classification for graph edges."""
    CONFIRMED_EXACT = "confirmed_exact"
    CONFIRMED_NORMALIZED = "confirmed_normalized"
    CONFIRMED_DBA = "confirmed_dba"
    POSSIBLE_FAMILY = "possible_family"
    LEGAL_SUFFIX_DISCREPANCY = "legal_suffix_discrepancy"
    DISTINCT_LEGAL_ENTITY = "distinct_legal_entity"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"
    CONFIRMED_MISMATCH = "confirmed_mismatch"
    UNLINKED_ORPHAN = "unlinked_orphan"


class GraphEvaluationStatus(str, Enum):
    """Evaluation state of the document relationship graph."""
    EVALUATED_EMPTY = "evaluated_empty"              # Processed; correctly determined no cross-document edges exist
    EVALUATED_WITH_EDGES = "evaluated_with_edges"    # Processed with 1 or more evaluated edges
    UNEVALUATED = "unevaluated"                      # Graph evaluation has not been performed
    INCOMPLETE = "incomplete"                        # Graph evaluation incomplete or partially failed


class DiscrepancyCategory(str, Enum):
    """Taxonomy of cross-document findings."""
    CONFIRMED_CONTRACTUAL_VIOLATION = "confirmed_contractual_violation" # Proven failure against requirement
    DOCUMENT_DISCREPANCY = "document_discrepancy"                       # Inconsistent statements between documents
    AMBIGUOUS_RELATIONSHIP = "ambiguous_relationship"                   # Unconfirmed family match or entity link
    MISSING_EVIDENCE = "missing_evidence"                               # Required form/schedule absent from packet
    EXTRACTION_FAILURE = "extraction_failure"                           # Unreadable OCR or parse failure


# =============================================================================
# Manifest & Provenance Primitives
# =============================================================================

class DocumentManifestEntry(BaseModel):
    """SEC-10 audit snapshot of a document in the inspected packet."""
    model_config = ConfigDict(frozen=True)

    document_id: str
    file_name: Optional[str] = None
    sha256_hash: str = Field(description="64-character lowercase hexadecimal SHA-256 hash")
    page_count: int = Field(ge=1, description="Exact page count of this document")
    detected_document_type: DocumentType

    @field_validator("sha256_hash")
    @classmethod
    def validate_and_normalize_sha256(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if not re.match(r"^[a-f0-9]{64}$", cleaned):
            raise ValueError(f"sha256_hash must be exactly 64 hexadecimal characters, got '{v}'")
        return cleaned


class MissingEvidenceProvenance(BaseModel):
    """SEC-10 compliant provenance for missing documents."""
    model_config = ConfigDict(frozen=True)

    assertion_document_id: str
    assertion_document_type: DocumentType
    assertion_page_number: int = Field(ge=1)
    assertion_source_text: str
    assertion_evidence: EvidenceReference
    missing_requirement_id: str
    missing_form_pattern: str
    packet_manifest_snapshot: Tuple[DocumentManifestEntry, ...] = Field(default_factory=tuple)
    inspected_total_pages: int = Field(ge=1)


# =============================================================================
# Multi-Edge Graph & Detailed Records with Local Invariant Validation
# =============================================================================

class DocumentRelationshipEdge(BaseModel):
    """Explicit directional relationship edge between documents."""
    model_config = ConfigDict(frozen=True)

    edge_id: str
    source_document_id: str
    target_document_id: Optional[str] = None  # None ONLY when relationship_type == ORPHAN_DOCUMENT
    relationship_type: DocumentRelationshipType
    associated_policy_id: Optional[str] = None
    coverage_type: Optional[CoverageType] = None
    classification: EdgeClassification
    is_compliance_relevant: bool = True
    evidence_references: Tuple[EvidenceReference, ...] = Field(default_factory=tuple)
    is_confirmed: bool = False
    requires_human_review: bool = False
    review_reason_code: Optional[str] = None

    @model_validator(mode="after")
    def validate_edge_consistency(self) -> DocumentRelationshipEdge:
        if self.is_confirmed and self.requires_human_review:
            raise ValueError(f"Edge {self.edge_id} cannot be both confirmed and require human review.")

        # Orphan edge rules
        if self.relationship_type == DocumentRelationshipType.ORPHAN_DOCUMENT:
            if self.target_document_id is not None:
                raise ValueError("ORPHAN_DOCUMENT edge must have target_document_id = None.")
            if self.classification != EdgeClassification.UNLINKED_ORPHAN:
                raise ValueError("ORPHAN_DOCUMENT edge must have classification = UNLINKED_ORPHAN.")
            if self.is_confirmed or not self.requires_human_review:
                raise ValueError("ORPHAN_DOCUMENT edge must have is_confirmed=False and requires_human_review=True.")
            return self

        # Non-orphan edges must have a target
        if self.target_document_id is None:
            raise ValueError(f"Edge {self.edge_id} with relationship {self.relationship_type} requires target_document_id.")

        # RelationshipType to EdgeClassification compatibility matrix
        valid_type_classifications = {
            DocumentRelationshipType.SAME_POLICY: {
                EdgeClassification.CONFIRMED_EXACT, EdgeClassification.CONFIRMED_NORMALIZED,
                EdgeClassification.POSSIBLE_FAMILY, EdgeClassification.AMBIGUOUS, EdgeClassification.CONFLICTING
            },
            DocumentRelationshipType.ENDORSEMENT_TO_POLICY: {
                EdgeClassification.CONFIRMED_EXACT, EdgeClassification.CONFIRMED_NORMALIZED, EdgeClassification.CONFIRMED_DBA,
                EdgeClassification.POSSIBLE_FAMILY, EdgeClassification.LEGAL_SUFFIX_DISCREPANCY, EdgeClassification.DISTINCT_LEGAL_ENTITY,
                EdgeClassification.AMBIGUOUS, EdgeClassification.CONFLICTING, EdgeClassification.CONFIRMED_MISMATCH
            },
            DocumentRelationshipType.DECLARATION_FOR_POLICY: {
                EdgeClassification.CONFIRMED_EXACT, EdgeClassification.CONFIRMED_NORMALIZED,
                EdgeClassification.POSSIBLE_FAMILY, EdgeClassification.AMBIGUOUS, EdgeClassification.CONFLICTING
            },
            DocumentRelationshipType.REPLACES_DOCUMENT: {
                EdgeClassification.CONFIRMED_EXACT, EdgeClassification.AMBIGUOUS, EdgeClassification.CONFLICTING
            },
            DocumentRelationshipType.AMENDS_DOCUMENT: {
                EdgeClassification.CONFIRMED_EXACT, EdgeClassification.AMBIGUOUS, EdgeClassification.CONFLICTING
            },
            DocumentRelationshipType.DUPLICATE_OF: {
                EdgeClassification.CONFIRMED_EXACT, EdgeClassification.AMBIGUOUS
            },
            DocumentRelationshipType.UNRELATED: {
                EdgeClassification.DISTINCT_LEGAL_ENTITY, EdgeClassification.CONFIRMED_MISMATCH,
                EdgeClassification.CONFLICTING, EdgeClassification.AMBIGUOUS
            },
        }

        allowed_classes = valid_type_classifications.get(self.relationship_type, set())
        if self.classification not in allowed_classes:
            raise ValueError(f"Invalid classification '{self.classification}' for relationship '{self.relationship_type}'. Allowed: {allowed_classes}")

        # Confirmed cross-document edges require evidence from both source and target documents
        # and relationship-specific affirmative evidence tied to the respective documents
        if self.is_confirmed:
            if self.relationship_type in (
                DocumentRelationshipType.SAME_POLICY,
                DocumentRelationshipType.ENDORSEMENT_TO_POLICY,
                DocumentRelationshipType.DECLARATION_FOR_POLICY,
                DocumentRelationshipType.REPLACES_DOCUMENT,
                DocumentRelationshipType.AMENDS_DOCUMENT,
                DocumentRelationshipType.DUPLICATE_OF,
            ):
                doc_ids = {ref.document_id for ref in self.evidence_references}
                if self.source_document_id not in doc_ids or self.target_document_id not in doc_ids:
                    raise ValueError(
                        f"Confirmed edge {self.edge_id} requires evidence referencing both "
                        f"source_document_id '{self.source_document_id}' and target_document_id '{self.target_document_id}'."
                    )

                src_text = " ".join((ref.source_text or "") for ref in self.evidence_references if ref.document_id == self.source_document_id)
                tgt_text = " ".join((ref.source_text or "") for ref in self.evidence_references if ref.document_id == self.target_document_id)

                if self.relationship_type == DocumentRelationshipType.REPLACES_DOCUMENT:
                    if not REPLACES_EVIDENCE_PATTERN.search(src_text):
                        raise ValueError(
                            f"Confirmed REPLACES_DOCUMENT edge '{self.edge_id}' requires affirmative evidence containing replacement or supersession text in source document."
                        )
                    is_linked, reason, _ = _validate_target_linkage(
                        self.relationship_type,
                        self.source_document_id,
                        self.target_document_id,
                        self.evidence_references,
                        associated_policy_id=self.associated_policy_id,
                    )
                    if not is_linked:
                        raise ValueError(
                            f"Confirmed REPLACES_DOCUMENT edge '{self.edge_id}' requires affirmative target identity linkage ({reason})."
                        )

                elif self.relationship_type == DocumentRelationshipType.AMENDS_DOCUMENT:
                    if not AMENDS_EVIDENCE_PATTERN.search(src_text):
                        raise ValueError(
                            f"Confirmed AMENDS_DOCUMENT edge '{self.edge_id}' requires affirmative amendment evidence in source document."
                        )
                    is_linked, reason, _ = _validate_target_linkage(
                        self.relationship_type,
                        self.source_document_id,
                        self.target_document_id,
                        self.evidence_references,
                        associated_policy_id=self.associated_policy_id,
                    )
                    if not is_linked:
                        raise ValueError(
                            f"Confirmed AMENDS_DOCUMENT edge '{self.edge_id}' requires affirmative target identity linkage ({reason})."
                        )

                elif self.relationship_type == DocumentRelationshipType.DUPLICATE_OF:
                    combined_text = f"{src_text} {tgt_text}"
                    if not DUPLICATE_EVIDENCE_PATTERN.search(combined_text):
                        raise ValueError(
                            f"Confirmed DUPLICATE_OF edge '{self.edge_id}' requires affirmative duplicate evidence."
                        )

                elif self.relationship_type == DocumentRelationshipType.DECLARATION_FOR_POLICY:
                    if not DECLARATION_EVIDENCE_PATTERN.search(src_text):
                        raise ValueError(
                            f"Confirmed DECLARATION_FOR_POLICY edge '{self.edge_id}' requires affirmative declaration evidence in source document."
                        )
                    if not POLICY_COVERAGE_EVIDENCE_PATTERN.search(tgt_text):
                        raise ValueError(
                            f"Confirmed DECLARATION_FOR_POLICY edge '{self.edge_id}' requires policy or coverage evidence in target document."
                        )

                elif self.relationship_type == DocumentRelationshipType.ENDORSEMENT_TO_POLICY:
                    if not ENDORSEMENT_EVIDENCE_PATTERN.search(src_text):
                        raise ValueError(
                            f"Confirmed ENDORSEMENT_TO_POLICY edge '{self.edge_id}' requires affirmative endorsement schedule or modification evidence in source document."
                        )
                    if not POLICY_COVERAGE_EVIDENCE_PATTERN.search(tgt_text):
                        raise ValueError(
                            f"Confirmed ENDORSEMENT_TO_POLICY edge '{self.edge_id}' requires policy or coverage evidence in target document."
                        )

                elif self.relationship_type == DocumentRelationshipType.SAME_POLICY:
                    if not POLICY_COVERAGE_EVIDENCE_PATTERN.search(src_text) or not POLICY_COVERAGE_EVIDENCE_PATTERN.search(tgt_text):
                        raise ValueError(
                            f"Confirmed SAME_POLICY edge '{self.edge_id}' requires policy or coverage evidence in both source and target documents."
                        )

        # Classification-to-State Mapping
        if self.classification in (
            EdgeClassification.CONFIRMED_EXACT,
            EdgeClassification.CONFIRMED_NORMALIZED,
            EdgeClassification.CONFIRMED_DBA,
        ):
            if not self.is_confirmed or self.requires_human_review:
                raise ValueError(f"Edge classification {self.classification} requires is_confirmed=True and requires_human_review=False.")
            if not self.evidence_references:
                raise ValueError(f"Confirmed edge {self.edge_id} must carry supporting evidence references.")
        elif self.classification in (
            EdgeClassification.POSSIBLE_FAMILY,
            EdgeClassification.LEGAL_SUFFIX_DISCREPANCY,
            EdgeClassification.DISTINCT_LEGAL_ENTITY,
            EdgeClassification.AMBIGUOUS,
            EdgeClassification.CONFLICTING,
            EdgeClassification.UNLINKED_ORPHAN,
        ):
            if self.is_confirmed or not self.requires_human_review:
                raise ValueError(f"Edge classification {self.classification} requires is_confirmed=False and requires_human_review=True.")
        elif self.classification == EdgeClassification.CONFIRMED_MISMATCH:
            if self.is_confirmed:
                raise ValueError("Edge with CONFIRMED_MISMATCH cannot be marked confirmed.")
            if not self.evidence_references:
                raise ValueError("CONFIRMED_MISMATCH edge must carry supporting evidence of the mismatch.")

        return self


class EntityReconciliationRecord(BaseModel):
    """Record of role-aware cross-document entity comparison."""
    model_config = ConfigDict(frozen=True)

    primary_entity_name: str
    primary_entity_role: EntityRole
    primary_document_id: str
    referenced_entity_name: str
    referenced_entity_role: EntityRole
    referenced_document_id: str
    classification: EntityMatchClassification
    applicable_contract_requirement_id: Optional[str] = None
    dba_evidence: Optional[EvidenceReference] = None
    primary_role_evidence: Optional[EvidenceReference] = None
    referenced_role_evidence: Optional[EvidenceReference] = None
    evidence_chain: Tuple[EvidenceReference, ...] = Field(default_factory=tuple)
    is_confirmed_match: bool = False
    requires_human_review: bool = False
    review_reason_code: Optional[str] = None

    @model_validator(mode="after")
    def validate_entity_record_consistency(self) -> EntityReconciliationRecord:
        if self.is_confirmed_match and self.requires_human_review:
            raise ValueError("Entity match cannot be both confirmed and require human review.")

        if self.is_confirmed_match:
            doc_ids_in_evidence = {e.document_id for e in self.evidence_chain}
            if self.primary_document_id not in doc_ids_in_evidence or self.referenced_document_id not in doc_ids_in_evidence:
                raise ValueError("Confirmed entity match evidence chain must reference both primary and referenced documents.")

        allowed_ent_docs = {self.primary_document_id, self.referenced_document_id}

        if self.primary_role_evidence is not None:
            if self.primary_role_evidence.document_id != self.primary_document_id:
                raise ValueError(
                    f"primary_role_evidence document_id '{self.primary_role_evidence.document_id}' "
                    f"must match primary_document_id '{self.primary_document_id}'."
                )
        if self.referenced_role_evidence is not None:
            if self.referenced_role_evidence.document_id != self.referenced_document_id:
                raise ValueError(
                    f"referenced_role_evidence document_id '{self.referenced_role_evidence.document_id}' "
                    f"must match referenced_document_id '{self.referenced_document_id}'."
                )

        for ref in self.evidence_chain:
            if ref.document_id not in allowed_ent_docs:
                raise ValueError(
                    f"Cross-document evidence reference rejected: entity evidence references document '{ref.document_id}', "
                    f"which is neither primary '{self.primary_document_id}' nor referenced '{self.referenced_document_id}'."
                )
        if self.dba_evidence is not None and self.dba_evidence.document_id not in allowed_ent_docs:
            raise ValueError(
                f"Cross-document evidence reference rejected: dba_evidence references document '{self.dba_evidence.document_id}', "
                f"which is neither primary '{self.primary_document_id}' nor referenced '{self.referenced_document_id}'."
            )

        if self.classification == EntityMatchClassification.CONFIRMED_DBA:
            if self.dba_evidence is None:
                raise ValueError("CONFIRMED_DBA requires explicit dba_evidence.")
            if self.dba_evidence not in self.evidence_chain:
                raise ValueError("dba_evidence must be included within evidence_chain.")
            if not self.is_confirmed_match or self.requires_human_review:
                raise ValueError("CONFIRMED_DBA must be confirmed with requires_human_review=False.")

        if self.classification in (EntityMatchClassification.EXACT, EntityMatchClassification.FORMATTING_NORMALIZED):
            if not self.is_confirmed_match or self.requires_human_review:
                raise ValueError(f"{self.classification} must be confirmed with requires_human_review=False.")

        if self.classification in (
            EntityMatchClassification.LEGAL_SUFFIX_DISCREPANCY,
            EntityMatchClassification.DISTINCT_LEGAL_ENTITY,
            EntityMatchClassification.AMBIGUOUS_IDENTITY,
        ):
            if self.is_confirmed_match or not self.requires_human_review:
                raise ValueError(f"{self.classification} requires is_confirmed_match=False and requires_human_review=True.")

        if self.classification == EntityMatchClassification.CONFIRMED_MISMATCH:
            if self.is_confirmed_match:
                raise ValueError("CONFIRMED_MISMATCH cannot be a confirmed match.")
            if not self.evidence_chain:
                raise ValueError("CONFIRMED_MISMATCH requires affirmative evidence demonstrating the mismatch.")
            doc_ids_mismatch = {e.document_id for e in self.evidence_chain}
            if self.primary_document_id not in doc_ids_mismatch or self.referenced_document_id not in doc_ids_mismatch:
                raise ValueError("CONFIRMED_MISMATCH requires affirmative evidence from both primary and referenced documents.")
            if not self.applicable_contract_requirement_id:
                raise ValueError("CONFIRMED_MISMATCH requires an applicable_contract_requirement_id.")
            # Use identical semantic criteria as reconciliation logic
            if not _evidence_supports_entity(self.primary_entity_name, self.evidence_chain, doc_id=self.primary_document_id) or not _evidence_supports_entity(self.referenced_entity_name, self.evidence_chain, doc_id=self.referenced_document_id):
                raise ValueError("CONFIRMED_MISMATCH requires affirmative evidence semantically supporting both entity names.")
            # Both entities must have established corporate legal suffixes to confirm distinct legal entities
            _, suff_p = _split_core_and_suffix(self.primary_entity_name)
            _, suff_r = _split_core_and_suffix(self.referenced_entity_name)
            if suff_p is None or suff_r is None:
                raise ValueError(
                    "CONFIRMED_MISMATCH rejected: both entities must have established legal entity suffixes; "
                    "names without recognized legal corporate forms cannot establish distinct legal identity."
                )
            # Safeguard against potential DBA / trade-name relationships
            dba_word_regex = re.compile(
                r"\b(d/?b/?a|doing\s+business\s+as|t/a|trading\s+as|trade\s+name|operating\s+as|division\s+of|subsidiary\s+of)\b",
                re.IGNORECASE,
            )
            if dba_word_regex.search(self.primary_entity_name) or dba_word_regex.search(self.referenced_entity_name):
                raise ValueError("CONFIRMED_MISMATCH rejected: entity names contain DBA or trade-name indicators.")
            for ref in self.evidence_chain:
                if dba_word_regex.search(ref.source_text or ""):
                    raise ValueError(
                        "CONFIRMED_MISMATCH rejected: evidence contains trade-name or DBA indicators; "
                        "potential trade-name relationships require human review and cannot be confirmed mismatches."
                    )

        return self


class PolicyReconciliationRecord(BaseModel):
    """Record of policy number comparison. Handles null policy numbers safely."""
    model_config = ConfigDict(frozen=True)

    coi_document_id: str
    coi_policy_number: Optional[str] = None
    coi_evidence: Optional[EvidenceReference] = None
    endorsement_document_id: str
    endorsement_policy_number: Optional[str] = None
    endorsement_evidence: Optional[EvidenceReference] = None
    classification: PolicyMatchClassification
    evidence_chain: Tuple[EvidenceReference, ...] = Field(default_factory=tuple)
    is_confirmed_match: bool = False
    requires_human_review: bool = False
    review_reason_code: Optional[str] = None

    @model_validator(mode="after")
    def validate_policy_record_consistency(self) -> PolicyReconciliationRecord:
        has_both_numbers = bool(self.coi_policy_number and self.coi_policy_number.strip()) and bool(self.endorsement_policy_number and self.endorsement_policy_number.strip())

        if not has_both_numbers and self.is_confirmed_match:
            raise ValueError("Policy match cannot be confirmed if either policy number is missing.")

        # Exact and Normalized Policy Validation
        if self.classification == PolicyMatchClassification.EXACT:
            if has_both_numbers:
                if self.coi_policy_number != self.endorsement_policy_number:
                    raise ValueError(f"EXACT classification requires identical strings, got '{self.coi_policy_number}' vs '{self.endorsement_policy_number}'.")
                if not self.is_confirmed_match or self.requires_human_review:
                    raise ValueError("EXACT policy match must be confirmed with requires_human_review=False.")

        if self.classification == PolicyMatchClassification.FORMATTING_NORMALIZED:
            if has_both_numbers:
                if self.coi_policy_number == self.endorsement_policy_number:
                    raise ValueError("FORMATTING_NORMALIZED requires non-identical strings prior to normalization.")
                norm_coi = normalize_policy_identifier(self.coi_policy_number)
                norm_end = normalize_policy_identifier(self.endorsement_policy_number)
                if norm_coi != norm_end:
                    raise ValueError(f"FORMATTING_NORMALIZED requires equal normalized identifiers, got '{norm_coi}' vs '{norm_end}'.")
                if not self.is_confirmed_match or self.requires_human_review:
                    raise ValueError("FORMATTING_NORMALIZED policy match must be confirmed with requires_human_review=False.")

        # Ensure individual evidence references belong to their declared documents
        if self.coi_evidence is not None and self.coi_evidence.document_id != self.coi_document_id:
            raise ValueError(f"coi_evidence.document_id '{self.coi_evidence.document_id}' must match coi_document_id '{self.coi_document_id}'.")
        if self.endorsement_evidence is not None and self.endorsement_evidence.document_id != self.endorsement_document_id:
            raise ValueError(f"endorsement_evidence.document_id '{self.endorsement_evidence.document_id}' must match endorsement_document_id '{self.endorsement_document_id}'.")

        allowed_pol_docs = {self.coi_document_id, self.endorsement_document_id}
        for ref in self.evidence_chain:
            if ref.document_id not in allowed_pol_docs:
                raise ValueError(
                    f"Cross-document evidence reference rejected: policy evidence references document '{ref.document_id}', "
                    f"which is neither coi '{self.coi_document_id}' nor endorsement '{self.endorsement_document_id}'."
                )

        if self.is_confirmed_match:
            if not self.coi_evidence or not self.endorsement_evidence:
                raise ValueError("Confirmed policy match requires separate non-null coi_evidence and endorsement_evidence.")
            if self.coi_evidence not in self.evidence_chain or self.endorsement_evidence not in self.evidence_chain:
                raise ValueError("Both coi_evidence and endorsement_evidence must appear within evidence_chain.")

        if self.classification in (
            PolicyMatchClassification.POSSIBLE_FAMILY,
            PolicyMatchClassification.AMBIGUOUS,
            PolicyMatchClassification.CONFLICTING,
        ):
            if self.is_confirmed_match or not self.requires_human_review:
                raise ValueError(f"{self.classification} requires is_confirmed_match=False and requires_human_review=True.")

        return self


class DateReconciliationRecord(BaseModel):
    """Temporal validity evaluation with independent, nullable active states."""
    model_config = ConfigDict(frozen=True)

    policy_document_id: Optional[str] = None
    policy_effective_date: Optional[date] = None
    policy_expiration_date: Optional[date] = None
    policy_date_evidence: Tuple[EvidenceReference, ...] = Field(default_factory=tuple)
    endorsement_document_id: Optional[str] = None
    endorsement_effective_date: Optional[date] = None
    endorsement_expiration_date: Optional[date] = None
    endorsement_date_evidence: Tuple[EvidenceReference, ...] = Field(default_factory=tuple)
    evaluation_date: Optional[date] = None
    is_midterm_addition: Optional[bool] = Field(default=None)
    is_policy_active_on_evaluation_date: Optional[bool] = Field(default=None)
    is_endorsement_active_on_evaluation_date: Optional[bool] = Field(default=None)
    is_premature_expiration: Optional[bool] = Field(default=None)
    requires_human_review: bool = False
    discrepancy_category: Optional[DiscrepancyCategory] = None
    review_reason_code: Optional[str] = None
    evidence_chain: Tuple[EvidenceReference, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def validate_date_contradictions(self) -> DateReconciliationRecord:
        if self.policy_document_id is not None:
            for ref in self.policy_date_evidence:
                if ref.document_id != self.policy_document_id:
                    raise ValueError(
                        f"policy_date_evidence document_id '{ref.document_id}' does not match "
                        f"policy_document_id '{self.policy_document_id}'."
                    )
        if self.endorsement_document_id is not None:
            for ref in self.endorsement_date_evidence:
                if ref.document_id != self.endorsement_document_id:
                    raise ValueError(
                        f"endorsement_date_evidence document_id '{ref.document_id}' does not match "
                        f"endorsement_document_id '{self.endorsement_document_id}'."
                    )
        allowed_date_docs = {d for d in (self.policy_document_id, self.endorsement_document_id) if d is not None}
        if allowed_date_docs:
            for ref in self.evidence_chain:
                if ref.document_id not in allowed_date_docs:
                    raise ValueError(
                        f"Cross-document evidence reference rejected: date evidence references document '{ref.document_id}', "
                        f"which is neither policy '{self.policy_document_id}' nor endorsement '{self.endorsement_document_id}'."
                    )

        if (self.policy_effective_date or self.policy_expiration_date) and not self.policy_date_evidence:
            raise ValueError("Populated policy dates require non-empty policy_date_evidence.")
        if (self.endorsement_effective_date or self.endorsement_expiration_date) and not self.endorsement_date_evidence:
            raise ValueError("Populated endorsement dates require non-empty endorsement_date_evidence.")

        if self.policy_effective_date and self.policy_expiration_date:
            if self.policy_effective_date > self.policy_expiration_date:
                raise ValueError(f"Inverted policy dates: effective {self.policy_effective_date} > expiration {self.policy_expiration_date}")
        if self.endorsement_effective_date and self.endorsement_expiration_date:
            if self.endorsement_effective_date > self.endorsement_expiration_date:
                raise ValueError(f"Inverted endorsement dates: effective {self.endorsement_effective_date} > expiration {self.endorsement_expiration_date}")

        if self.evaluation_date is None:
            if self.is_policy_active_on_evaluation_date is not None or self.is_endorsement_active_on_evaluation_date is not None:
                raise ValueError("Active states on evaluation date cannot be determined when evaluation_date is None.")
            return self

        if self.is_policy_active_on_evaluation_date is True:
            if not (self.policy_effective_date and self.policy_expiration_date):
                raise ValueError("is_policy_active_on_evaluation_date cannot be True without verified start and end dates.")
            if self.policy_effective_date > self.evaluation_date:
                raise ValueError("Contradiction: policy marked active but effective date > evaluation date.")
            if self.policy_expiration_date < self.evaluation_date:
                raise ValueError("Contradiction: policy marked active but expiration date < evaluation date.")
        if self.is_policy_active_on_evaluation_date is False:
            if self.policy_effective_date and self.policy_expiration_date:
                if self.policy_effective_date <= self.evaluation_date <= self.policy_expiration_date:
                    raise ValueError("Contradiction: policy marked inactive but dates enclose evaluation date.")

        if self.is_endorsement_active_on_evaluation_date is True:
            if not (self.endorsement_effective_date and self.endorsement_expiration_date):
                raise ValueError("is_endorsement_active_on_evaluation_date cannot be True without verified start and end dates.")
            if self.endorsement_effective_date > self.evaluation_date:
                raise ValueError("Contradiction: endorsement marked active but effective date > evaluation date.")
            if self.endorsement_expiration_date < self.evaluation_date:
                raise ValueError("Contradiction: endorsement marked active but expiration date < evaluation date.")
        if self.is_endorsement_active_on_evaluation_date is False:
            if self.endorsement_effective_date and self.endorsement_expiration_date:
                if self.endorsement_effective_date <= self.evaluation_date <= self.endorsement_expiration_date:
                    raise ValueError("Contradiction: endorsement marked inactive but dates enclose evaluation date.")

        if self.is_midterm_addition is True:
            if not (self.endorsement_effective_date and self.policy_effective_date):
                raise ValueError("is_midterm_addition cannot be True without verified endorsement and policy effective dates.")
            if self.endorsement_effective_date <= self.policy_effective_date:
                raise ValueError("is_midterm_addition cannot be True when endorsement effective date <= policy effective date.")
        if self.is_midterm_addition is False:
            if self.endorsement_effective_date and self.policy_effective_date:
                if self.endorsement_effective_date > self.policy_effective_date:
                    raise ValueError("is_midterm_addition cannot be False when endorsement effective date > policy effective date.")

        if self.is_premature_expiration is True:
            if not (self.endorsement_expiration_date and self.policy_expiration_date):
                raise ValueError("is_premature_expiration cannot be True without verified expiration dates.")
            if self.endorsement_expiration_date >= self.policy_expiration_date:
                raise ValueError("is_premature_expiration cannot be True when endorsement expiration date >= policy expiration date.")
            if self.is_endorsement_active_on_evaluation_date is None:
                raise ValueError("is_premature_expiration requires an explicit is_endorsement_active_on_evaluation_date state.")
        if self.is_premature_expiration is False:
            if self.endorsement_expiration_date and self.policy_expiration_date:
                if self.endorsement_expiration_date < self.policy_expiration_date:
                    raise ValueError("is_premature_expiration cannot be False when endorsement expiration date < policy expiration date.")

        return self


class DocumentDiscrepancy(BaseModel):
    """Structured discrepancy payload consumed by ComplianceEvaluator."""
    model_config = ConfigDict(frozen=True)

    discrepancy_id: str
    category: DiscrepancyCategory
    severity: Severity
    reason_code: str
    description: str
    source_document_id: str
    conflicting_document_id: Optional[str] = None
    applicable_requirement_id: Optional[str] = None
    applicable_entity_role: Optional[EntityRole] = None
    applicable_coverage_type: Optional[CoverageType] = None
    evidence_chain: Tuple[EvidenceReference, ...] = Field(default_factory=tuple)
    requires_human_review: bool = True

    @model_validator(mode="after")
    def validate_contractual_violation_requirements(self) -> DocumentDiscrepancy:
        allowed_disc_docs = {self.source_document_id}
        if self.conflicting_document_id is not None:
            allowed_disc_docs.add(self.conflicting_document_id)
        for ref in self.evidence_chain:
            if ref.document_id not in allowed_disc_docs:
                raise ValueError(
                    f"Cross-document evidence reference rejected: discrepancy evidence references document '{ref.document_id}', "
                    f"which is neither source '{self.source_document_id}' nor conflicting '{self.conflicting_document_id}'."
                )

        if self.category == DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION:
            if not self.applicable_requirement_id or not self.applicable_requirement_id.strip():
                raise ValueError("CONFIRMED_CONTRACTUAL_VIOLATION strictly requires a non-empty applicable_requirement_id.")
            if not self.evidence_chain:
                raise ValueError("CONFIRMED_CONTRACTUAL_VIOLATION strictly requires non-empty supporting evidence_chain.")
            if not (self.applicable_coverage_type or self.applicable_entity_role):
                raise ValueError("CONFIRMED_CONTRACTUAL_VIOLATION strictly requires applicable_coverage_type or applicable_entity_role.")
        return self


class CrossDocumentReconciliationResult(BaseModel):
    """Complete immutable audit payload produced by Reconciliation Layer."""
    model_config = ConfigDict(frozen=True)

    document_packet_id: str
    primary_coi_id: Optional[str] = None
    packet_manifest: Tuple[DocumentManifestEntry, ...] = Field(default_factory=tuple)
    graph_edges: Tuple[DocumentRelationshipEdge, ...] = Field(default_factory=tuple)
    entity_records: Tuple[EntityReconciliationRecord, ...] = Field(default_factory=tuple)
    policy_records: Tuple[PolicyReconciliationRecord, ...] = Field(default_factory=tuple)
    date_records: Tuple[DateReconciliationRecord, ...] = Field(default_factory=tuple)
    missing_evidence_records: Tuple[MissingEvidenceProvenance, ...] = Field(default_factory=tuple)
    discrepancies: Tuple[DocumentDiscrepancy, ...] = Field(default_factory=tuple)
    has_unresolved_contradictions: bool = False
    has_missing_evidence: bool = False
    all_relationships_confirmed: bool = False
    graph_evaluation_status: GraphEvaluationStatus = GraphEvaluationStatus.EVALUATED_WITH_EDGES

    @model_validator(mode="after")
    def validate_cross_model_referential_integrity_and_derive_summaries(self) -> CrossDocumentReconciliationResult:
        if not self.packet_manifest:
            raise ValueError("packet_manifest must contain at least one DocumentManifestEntry.")

        manifest_map: Dict[str, DocumentManifestEntry] = {}
        for entry in self.packet_manifest:
            if entry.document_id in manifest_map:
                raise ValueError(f"Duplicate document_id '{entry.document_id}' in packet_manifest.")
            manifest_map[entry.document_id] = entry

        if self.primary_coi_id is not None:
            if not self.primary_coi_id.strip():
                raise ValueError("primary_coi_id cannot be an empty string.")
            if self.primary_coi_id not in manifest_map:
                raise ValueError(f"primary_coi_id '{self.primary_coi_id}' not found in packet_manifest.")
            if manifest_map[self.primary_coi_id].detected_document_type != DocumentType.COI:
                raise ValueError(
                    f"primary_coi_id '{self.primary_coi_id}' detected_document_type must be DocumentType.COI, "
                    f"got {manifest_map[self.primary_coi_id].detected_document_type}."
                )

        edge_ids: Set[str] = set()
        for edge in self.graph_edges:
            if edge.edge_id in edge_ids:
                raise ValueError(f"Duplicate edge_id '{edge.edge_id}' in graph_edges.")
            edge_ids.add(edge.edge_id)

            if edge.source_document_id not in manifest_map:
                raise ValueError(f"Edge source_document_id '{edge.source_document_id}' not in packet_manifest.")
            if edge.target_document_id is not None and edge.target_document_id not in manifest_map:
                raise ValueError(f"Edge target_document_id '{edge.target_document_id}' not in packet_manifest.")

            allowed_edge_docs = {edge.source_document_id}
            if edge.target_document_id is not None:
                allowed_edge_docs.add(edge.target_document_id)

            for ref in edge.evidence_references:
                validate_evidence_reference(ref, manifest_map, "Edge")
                if ref.document_id not in allowed_edge_docs:
                    raise ValueError(
                        f"Cross-document evidence reference rejected: edge '{edge.edge_id}' references document '{ref.document_id}', "
                        f"which is not among its associated documents {allowed_edge_docs}."
                    )

        for ent in self.entity_records:
            if ent.primary_document_id not in manifest_map:
                raise ValueError(f"Entity primary_document_id '{ent.primary_document_id}' not in manifest.")
            if ent.referenced_document_id not in manifest_map:
                raise ValueError(f"Entity referenced_document_id '{ent.referenced_document_id}' not in manifest.")

            allowed_ent_docs = {ent.primary_document_id, ent.referenced_document_id}

            if ent.dba_evidence is not None:
                validate_evidence_reference(ent.dba_evidence, manifest_map, "Entity dba_evidence")
                if ent.dba_evidence.document_id not in allowed_ent_docs:
                    raise ValueError(
                        f"Cross-document evidence reference rejected: entity dba_evidence references document '{ent.dba_evidence.document_id}', "
                        f"which is neither primary '{ent.primary_document_id}' nor referenced '{ent.referenced_document_id}'."
                    )

            if ent.primary_role_evidence is not None:
                validate_evidence_reference(ent.primary_role_evidence, manifest_map, "Entity primary_role_evidence")
                if ent.primary_role_evidence.document_id != ent.primary_document_id:
                    raise ValueError(
                        f"Entity primary_role_evidence document_id '{ent.primary_role_evidence.document_id}' "
                        f"does not match primary_document_id '{ent.primary_document_id}'."
                    )

            if ent.referenced_role_evidence is not None:
                validate_evidence_reference(ent.referenced_role_evidence, manifest_map, "Entity referenced_role_evidence")
                if ent.referenced_role_evidence.document_id != ent.referenced_document_id:
                    raise ValueError(
                        f"Entity referenced_role_evidence document_id '{ent.referenced_role_evidence.document_id}' "
                        f"does not match referenced_document_id '{ent.referenced_document_id}'."
                    )

            for ref in ent.evidence_chain:
                validate_evidence_reference(ref, manifest_map, "Entity")
                if ref.document_id not in allowed_ent_docs:
                    raise ValueError(
                        f"Cross-document evidence reference rejected: entity evidence references document '{ref.document_id}', "
                        f"which is neither primary '{ent.primary_document_id}' nor referenced '{ent.referenced_document_id}'."
                    )

        for pol in self.policy_records:
            if pol.coi_document_id not in manifest_map:
                raise ValueError(f"Policy coi_document_id '{pol.coi_document_id}' not in manifest.")
            if pol.endorsement_document_id not in manifest_map:
                raise ValueError(f"Policy endorsement_document_id '{pol.endorsement_document_id}' not in manifest.")

            allowed_pol_docs = {pol.coi_document_id, pol.endorsement_document_id}

            if pol.coi_evidence is not None:
                validate_evidence_reference(pol.coi_evidence, manifest_map, "Policy coi_evidence")
                if pol.coi_evidence.document_id != pol.coi_document_id:
                    raise ValueError(
                        f"Policy coi_evidence.document_id '{pol.coi_evidence.document_id}' != coi_document_id '{pol.coi_document_id}'."
                    )

            if pol.endorsement_evidence is not None:
                validate_evidence_reference(pol.endorsement_evidence, manifest_map, "Policy endorsement_evidence")
                if pol.endorsement_evidence.document_id != pol.endorsement_document_id:
                    raise ValueError(
                        f"Policy endorsement_evidence.document_id '{pol.endorsement_evidence.document_id}' != endorsement_document_id '{pol.endorsement_document_id}'."
                    )

            for ref in pol.evidence_chain:
                validate_evidence_reference(ref, manifest_map, "Policy")
                if ref.document_id not in allowed_pol_docs:
                    raise ValueError(
                        f"Cross-document evidence reference rejected: policy evidence references document '{ref.document_id}', "
                        f"which is neither coi '{pol.coi_document_id}' nor endorsement '{pol.endorsement_document_id}'."
                    )

        for drec in self.date_records:
            if drec.policy_document_id is not None and drec.policy_document_id not in manifest_map:
                raise ValueError(f"Date record policy_document_id '{drec.policy_document_id}' not in manifest.")
            if drec.endorsement_document_id is not None and drec.endorsement_document_id not in manifest_map:
                raise ValueError(f"Date record endorsement_document_id '{drec.endorsement_document_id}' not in manifest.")

            for ref in drec.policy_date_evidence:
                validate_evidence_reference(ref, manifest_map, "Date policy")
                if drec.policy_document_id is not None and ref.document_id != drec.policy_document_id:
                    raise ValueError(
                        f"Date policy evidence references document '{ref.document_id}', "
                        f"expected policy_document_id '{drec.policy_document_id}'."
                    )

            for ref in drec.endorsement_date_evidence:
                validate_evidence_reference(ref, manifest_map, "Date endorsement")
                if drec.endorsement_document_id is not None and ref.document_id != drec.endorsement_document_id:
                    raise ValueError(
                        f"Date endorsement evidence references document '{ref.document_id}', "
                        f"expected endorsement_document_id '{drec.endorsement_document_id}'."
                    )

            allowed_date_docs = {d for d in (drec.policy_document_id, drec.endorsement_document_id) if d is not None}
            for ref in drec.evidence_chain:
                validate_evidence_reference(ref, manifest_map, "Date chain")
                if allowed_date_docs and ref.document_id not in allowed_date_docs:
                    raise ValueError(
                        f"Cross-document evidence reference rejected: date evidence references document '{ref.document_id}', "
                        f"which is not among associated documents {allowed_date_docs}."
                    )

        for disc in self.discrepancies:
            if disc.source_document_id not in manifest_map:
                raise ValueError(f"Discrepancy source_document_id '{disc.source_document_id}' not in manifest.")
            if disc.conflicting_document_id is not None and disc.conflicting_document_id not in manifest_map:
                raise ValueError(f"Discrepancy conflicting_document_id '{disc.conflicting_document_id}' not in manifest.")

            allowed_disc_docs = {disc.source_document_id}
            if disc.conflicting_document_id is not None:
                allowed_disc_docs.add(disc.conflicting_document_id)

            for ref in disc.evidence_chain:
                validate_evidence_reference(ref, manifest_map, "Discrepancy")
                if ref.document_id not in allowed_disc_docs:
                    raise ValueError(
                        f"Cross-document evidence reference rejected: discrepancy evidence references document '{ref.document_id}', "
                        f"which is neither source '{disc.source_document_id}' nor conflicting '{disc.conflicting_document_id}'."
                    )

        for miss in self.missing_evidence_records:
            if miss.assertion_document_id not in manifest_map:
                raise ValueError(f"Missing evidence assertion doc '{miss.assertion_document_id}' not in manifest.")
            doc_entry = manifest_map[miss.assertion_document_id]
            if miss.assertion_document_type != doc_entry.detected_document_type:
                raise ValueError(
                    f"Missing evidence assertion_document_type '{miss.assertion_document_type}' does not match "
                    f"manifest detected_document_type '{doc_entry.detected_document_type}'."
                )
            if not (1 <= miss.assertion_page_number <= doc_entry.page_count):
                raise ValueError(f"Missing assertion page {miss.assertion_page_number} out of bounds for doc '{miss.assertion_document_id}'.")

            validate_evidence_reference(miss.assertion_evidence, manifest_map, "Missing assertion")
            if miss.assertion_evidence.document_id != miss.assertion_document_id:
                raise ValueError(f"assertion_evidence.document_id '{miss.assertion_evidence.document_id}' != assertion_document_id '{miss.assertion_document_id}'.")
            if miss.assertion_evidence.page_number != miss.assertion_page_number:
                raise ValueError(f"assertion_evidence.page_number {miss.assertion_evidence.page_number} != assertion_page_number {miss.assertion_page_number}.")
            if miss.assertion_evidence.document_type != miss.assertion_document_type:
                raise ValueError(f"assertion_evidence.document_type '{miss.assertion_evidence.document_type}' != assertion_document_type '{miss.assertion_document_type}'.")
            if miss.packet_manifest_snapshot != self.packet_manifest:
                raise ValueError("MissingEvidenceProvenance packet_manifest_snapshot does not match evaluated packet_manifest.")

        # Graph evaluation status determination:
        # Distinguish valid evaluated empty graphs from unevaluated or incomplete graphs
        derived_graph_status = self.graph_evaluation_status
        if len(self.graph_edges) == 0:
            if self.graph_evaluation_status not in (GraphEvaluationStatus.UNEVALUATED, GraphEvaluationStatus.INCOMPLETE):
                derived_graph_status = GraphEvaluationStatus.EVALUATED_EMPTY
        else:
            if self.graph_evaluation_status not in (GraphEvaluationStatus.UNEVALUATED, GraphEvaluationStatus.INCOMPLETE):
                derived_graph_status = GraphEvaluationStatus.EVALUATED_WITH_EDGES

        if self.graph_evaluation_status != derived_graph_status:
            object.__setattr__(self, "graph_evaluation_status", derived_graph_status)

        # Summary derivation considering is_compliance_relevant:
        compliance_edges = [e for e in self.graph_edges if e.is_compliance_relevant]

        derived_contradictions = any(
            d.category in (
                DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION,
                DiscrepancyCategory.DOCUMENT_DISCREPANCY,
                DiscrepancyCategory.AMBIGUOUS_RELATIONSHIP,
            )
            for d in self.discrepancies
        ) or any(e.requires_human_review for e in compliance_edges)

        derived_missing = len(self.missing_evidence_records) > 0 or any(
            d.category == DiscrepancyCategory.MISSING_EVIDENCE for d in self.discrepancies
        )

        if (
            len(compliance_edges) == 0
            or derived_graph_status != GraphEvaluationStatus.EVALUATED_WITH_EDGES
        ):
            derived_all_confirmed = False
        else:
            derived_all_confirmed = all(e.is_confirmed for e in compliance_edges) and not derived_contradictions and not derived_missing

        if self.has_unresolved_contradictions != derived_contradictions:
            object.__setattr__(self, "has_unresolved_contradictions", derived_contradictions)
        if self.has_missing_evidence != derived_missing:
            object.__setattr__(self, "has_missing_evidence", derived_missing)
        if self.all_relationships_confirmed != derived_all_confirmed:
            object.__setattr__(self, "all_relationships_confirmed", derived_all_confirmed)

        return self
