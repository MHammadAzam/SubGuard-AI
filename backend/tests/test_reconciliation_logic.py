"""Unit tests for Milestone 3.5C Deterministic Cross-Document Reconciliation Logic.

Explicitly validates required scenarios C-01 through C-14:
- C-01: Same legal entity is NAMED_INSURED on COI and ADDITIONAL_INSURED on endorsement -> Identity remains EXACT; roles evaluated separately
- C-02: Same legal entity has different document roles -> Role difference does not establish different legal identity
- C-03: Same identity, but required contractual role is not evidenced -> Requirement remains unsatisfied or unresolved
- C-04: Role mismatch without a validated contractual requirement -> Must not create confirmed violation
- C-05: Two genuinely different legal entities -> Identity and roles evaluated independently
- C-06: Permitted formatting differences in policy identifiers -> FORMATTING_NORMALIZED
- C-07: Meaningful policy suffix differences -> Must not become EXACT
- C-08: Leading-zero differences -> Must follow explicitly documented normalization rules
- C-09: Endorsement expires earlier but remains active on evaluation date -> No automatic confirmed violation
- C-10: Endorsement expired before evaluation date with complete evidence -> Inactive conclusion supported by evidence
- C-11: Evaluation date missing -> No active/inactive conclusion
- C-12: Matching policy identifiers without relationship-specific evidence -> Relationship not confirmed
- C-13: Replacement asserted without supersession evidence -> Replacement not confirmed
- C-14: Zero compliance-relevant edges -> Must not establish compliance
"""
from __future__ import annotations

import unittest
from datetime import date

from app.compliance.reconciliation import (
    DeterministicReconciliationEngine,
    check_contractual_role_satisfaction,
    classify_document_relationship,
    reconcile_dates,
    reconcile_entity_identities,
    reconcile_policy_identifiers,
    _evidence_supports_date,
    _has_role_evidence,
)
from app.schemas.insurance import (
    BoundingBox,
    CoverageType,
    DocumentType,
    EvidenceReference,
    Severity,
)
from app.schemas.reconciliation import (
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
    PolicyMatchClassification,
    PolicyReconciliationRecord,
    normalize_policy_identifier,
)


class TestReconciliationLogic(unittest.TestCase):
    """Test suite for deterministic reconciliation algorithms and invariants."""

    def setUp(self):
        self.doc_coi_manifest = DocumentManifestEntry(
            document_id="doc_coi",
            file_name="coi.pdf",
            sha256_hash="a" * 64,
            page_count=2,
            detected_document_type=DocumentType.COI,
        )
        self.doc_end_manifest = DocumentManifestEntry(
            document_id="doc_end",
            file_name="endorsement.pdf",
            sha256_hash="b" * 64,
            page_count=3,
            detected_document_type=DocumentType.ENDORSEMENT,
        )
        self.doc_dec_manifest = DocumentManifestEntry(
            document_id="doc_dec",
            file_name="dec_page.pdf",
            sha256_hash="c" * 64,
            page_count=5,
            detected_document_type=DocumentType.POLICY_DEC_PAGE,
        )

        self.ref_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Apex Electrical LLC",
            bounding_box=BoundingBox(x=0.1, y=0.1, width=0.3, height=0.05),
        )
        self.ref_end = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Apex Electrical LLC",
            bounding_box=BoundingBox(x=0.1, y=0.1, width=0.3, height=0.05),
        )
        self.ref_dec = EvidenceReference(
            document_id="doc_dec",
            document_type=DocumentType.POLICY_DEC_PAGE,
            page_number=1,
            source_text="Apex Electrical LLC",
            bounding_box=BoundingBox(x=0.1, y=0.1, width=0.3, height=0.05),
        )

    # =========================================================================
    # C-01 & C-02: Separate Legal Identity from Document Roles
    # =========================================================================

    def test_c01_same_legal_entity_different_roles_exact_match(self):
        """C-01: Same legal entity is NAMED_INSURED on COI and ADDITIONAL_INSURED on endorsement.
        
        Identity remains EXACT; roles are evaluated separately.
        """
        rec = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            evidence_chain=(self.ref_coi, self.ref_end),
        )
        self.assertEqual(rec.classification, EntityMatchClassification.EXACT)
        self.assertTrue(rec.is_confirmed_match)
        self.assertFalse(rec.requires_human_review)
        self.assertEqual(rec.primary_entity_role, EntityRole.NAMED_INSURED)
        self.assertEqual(rec.referenced_entity_role, EntityRole.ADDITIONAL_INSURED)

    def test_c02_different_document_roles_does_not_establish_distinct_identity(self):
        """C-02: Role difference does not establish different legal identity."""
        ref_zenith_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Zenith Mechanical Inc",
        )
        ref_zenith_end = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Certificate Holder: Zenith Mechanical Inc",
        )
        rec = reconcile_entity_identities(
            primary_entity_name="Zenith Mechanical Inc",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Zenith Mechanical Inc",
            referenced_entity_role=EntityRole.CERTIFICATE_HOLDER,
            referenced_document_id="doc_end",
            evidence_chain=(ref_zenith_coi, ref_zenith_end),
        )
        # Identity is EXACT, never DISTINCT_LEGAL_ENTITY solely because roles differ
        self.assertEqual(rec.classification, EntityMatchClassification.EXACT)
        self.assertTrue(rec.is_confirmed_match)
        self.assertFalse(rec.requires_human_review)

    # =========================================================================
    # C-03 & C-04: Contractual Role Satisfaction & Violation Guards
    # =========================================================================

    def test_c03_same_identity_unsupported_role_evidence_unresolved(self):
        """C-03: Same identity, but required contractual role is not evidenced -> unresolved."""
        rec = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.AMBIGUOUS_IDENTITY,
            is_confirmed_match=False,
            requires_human_review=True,
            evidence_chain=(),  # Evidence missing!
        )
        satisfied, disc = check_contractual_role_satisfaction(
            entity_record=rec,
            required_entity_name="Apex Electrical LLC",
            required_role=EntityRole.NAMED_INSURED,
            contract_requirement_id="REQ_NAMED_INSURED",
        )
        self.assertFalse(satisfied)
        self.assertIsNotNone(disc)
        assert disc is not None
        # Must be MISSING_EVIDENCE requiring human review, NOT a confirmed violation!
        self.assertEqual(disc.category, DiscrepancyCategory.MISSING_EVIDENCE)
        self.assertEqual(disc.reason_code, "INSUFFICIENT_ROLE_EVIDENCE")
        self.assertTrue(disc.requires_human_review)

    def test_c04_role_mismatch_without_validated_requirement_creates_no_violation(self):
        """C-04: Role mismatch without a validated contractual requirement must not create confirmed violation."""
        rec = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.CERTIFICATE_HOLDER,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.EXACT,
            is_confirmed_match=True,
            requires_human_review=False,
            evidence_chain=(self.ref_coi, self.ref_end),
        )
        # Missing contract requirement ID
        satisfied, disc = check_contractual_role_satisfaction(
            entity_record=rec,
            required_entity_name="Apex Electrical LLC",
            required_role=EntityRole.NAMED_INSURED,
            contract_requirement_id=None,  # No validated requirement!
        )
        self.assertFalse(satisfied)
        # Must NOT create a confirmed contractual violation!
        self.assertIsNone(disc)

    def test_c05_genuinely_different_legal_entities(self):
        """C-05: Two genuinely different legal entities evaluated independently of roles."""
        rec = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Zenith Plumbing Inc",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            evidence_chain=(self.ref_coi, self.ref_end),
        )
        self.assertEqual(rec.classification, EntityMatchClassification.DISTINCT_LEGAL_ENTITY)
        self.assertFalse(rec.is_confirmed_match)
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.review_reason_code, "DISTINCT_LEGAL_ENTITIES")

    # =========================================================================
    # C-06, C-07, C-08: Policy Identifier Normalization Rules
    # =========================================================================

    def test_c06_permitted_formatting_differences_policy_identifiers(self):
        """C-06: Permitted formatting differences (spaces, hyphens, slashes, periods) -> FORMATTING_NORMALIZED."""
        # Spaces, hyphens, slashes, periods
        rec = reconcile_policy_identifiers(
            coi_document_id="doc_coi",
            coi_policy_number="GL-12345/A.01",
            coi_evidence=self.ref_coi,
            endorsement_document_id="doc_end",
            endorsement_policy_number="GL 12345-A 01",  # Same chars, different delimiters
            endorsement_evidence=self.ref_end,
        )
        self.assertEqual(rec.classification, PolicyMatchClassification.FORMATTING_NORMALIZED)
        self.assertTrue(rec.is_confirmed_match)
        self.assertFalse(rec.requires_human_review)

    def test_c07_meaningful_policy_suffix_differences_never_exact(self):
        """C-07: Meaningful policy renewal suffix differences must NOT become EXACT."""
        rec = reconcile_policy_identifiers(
            coi_document_id="doc_coi",
            coi_policy_number="GL-9876543-01",
            coi_evidence=self.ref_coi,
            endorsement_document_id="doc_end",
            endorsement_policy_number="GL-9876543-02",  # Renewal suffix -01 vs -02
            endorsement_evidence=self.ref_end,
        )
        self.assertNotEqual(rec.classification, PolicyMatchClassification.EXACT)
        self.assertNotEqual(rec.classification, PolicyMatchClassification.FORMATTING_NORMALIZED)
        self.assertEqual(rec.classification, PolicyMatchClassification.POSSIBLE_FAMILY)
        self.assertFalse(rec.is_confirmed_match)
        self.assertTrue(rec.requires_human_review)

    def test_c08_leading_zero_differences_in_policy_identifiers(self):
        """C-08: Leading zeros are preserved and must not be silently stripped to equate numbers."""
        norm_zero = normalize_policy_identifier("0012345")
        norm_no_zero = normalize_policy_identifier("12345")
        self.assertEqual(norm_zero, "0012345")
        self.assertEqual(norm_no_zero, "12345")
        self.assertNotEqual(norm_zero, norm_no_zero)

        rec = reconcile_policy_identifiers(
            coi_document_id="doc_coi",
            coi_policy_number="0012345",
            coi_evidence=self.ref_coi,
            endorsement_document_id="doc_end",
            endorsement_policy_number="12345",
            endorsement_evidence=self.ref_end,
        )
        # Leading zero difference without supporting family evidence classifies as CONFLICTING, NEVER EXACT or NORMALIZED
        self.assertNotEqual(rec.classification, PolicyMatchClassification.EXACT)
        self.assertNotEqual(rec.classification, PolicyMatchClassification.FORMATTING_NORMALIZED)
        self.assertEqual(rec.classification, PolicyMatchClassification.CONFLICTING)
        self.assertFalse(rec.is_confirmed_match)
        self.assertTrue(rec.requires_human_review)

    # =========================================================================
    # C-09, C-10, C-11: Date Reconciliation
    # =========================================================================

    def test_c09_endorsement_expires_earlier_but_active_on_eval_date(self):
        """C-09: Endorsement expiring earlier but remaining active on evaluation date is NOT noncompliant."""
        ref_coi_date = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy Period: Effective 01/01/2025 to Expiration 01/01/2026",
        )
        ref_end_date = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Endorsement Effective 01/01/2025 Expiration 10/01/2025",
        )
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(ref_coi_date,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2025, 10, 1),  # Expires before policy!
            endorsement_date_evidence=(ref_end_date,),
            evaluation_date=date(2025, 6, 1),  # But ACTIVE on evaluation date!
            evidence_chain=(ref_coi_date, ref_end_date),
        )
        self.assertTrue(rec.is_policy_active_on_evaluation_date)
        self.assertTrue(rec.is_endorsement_active_on_evaluation_date)
        self.assertTrue(rec.is_premature_expiration)
        # Must NOT automatically trigger review or noncompliance on the evaluation date!
        self.assertFalse(rec.requires_human_review)
        self.assertIsNone(rec.discrepancy_category)

    def test_c10_endorsement_expired_before_evaluation_date_with_evidence(self):
        """C-10: Endorsement expired before evaluation date with complete evidence -> inactive conclusion."""
        ref_coi_date = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy Period: Effective 01/01/2025 to Expiration 01/01/2026",
        )
        ref_end_date = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Endorsement Effective 01/01/2025 Expiration 05/01/2025",
        )
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(ref_coi_date,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2025, 5, 1),  # Expired before evaluation date!
            endorsement_date_evidence=(ref_end_date,),
            evaluation_date=date(2025, 6, 1),
            evidence_chain=(ref_coi_date, ref_end_date),
        )
        self.assertTrue(rec.is_policy_active_on_evaluation_date)
        self.assertFalse(rec.is_endorsement_active_on_evaluation_date)
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.discrepancy_category, DiscrepancyCategory.DOCUMENT_DISCREPANCY)
        self.assertEqual(rec.review_reason_code, "ENDORSEMENT_INACTIVE_ON_EVALUATION_DATE")

    def test_c11_evaluation_date_missing_no_active_inactive_conclusion(self):
        """C-11: Missing evaluation date leaves active/inactive conclusions as None."""
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(self.ref_coi,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2026, 1, 1),
            endorsement_date_evidence=(self.ref_end,),
            evaluation_date=None,  # Missing!
            evidence_chain=(self.ref_coi, self.ref_end),
        )
        self.assertIsNone(rec.evaluation_date)
        self.assertIsNone(rec.is_policy_active_on_evaluation_date)
        self.assertIsNone(rec.is_endorsement_active_on_evaluation_date)

    # =========================================================================
    # C-12, C-13: Document Relationship Evidence Invariants
    # =========================================================================

    def test_c12_matching_policy_identifiers_without_relationship_evidence(self):
        """C-12: Matching policy number without relationship evidence does not confirm edge."""
        edge = classify_document_relationship(
            edge_id="e_unsupported",
            source_document_id="doc_coi",
            target_document_id="doc_end",
            relationship_type=DocumentRelationshipType.SAME_POLICY,
            classification=EdgeClassification.AMBIGUOUS,
            evidence_references=(),  # No evidence!
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)

    def test_c13_replacement_asserted_without_supersession_evidence(self):
        """C-13: Replacement asserted without supersession evidence must NOT be confirmed."""
        edge = classify_document_relationship(
            edge_id="e_replace",
            source_document_id="doc_coi",
            target_document_id="doc_end",
            relationship_type=DocumentRelationshipType.REPLACES_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            has_relationship_evidence=False,  # No supersession evidence!
            evidence_references=(self.ref_coi, self.ref_end),
        )
        # Even with matching policy references, lack of replacement evidence prevents confirmation
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.AMBIGUOUS)
        self.assertEqual(edge.review_reason_code, "UNSUPPORTED_RELATIONSHIP_ASSERTION")

    # =========================================================================
    # C-14: Zero Compliance-Relevant Edges
    # =========================================================================

    def test_c14_zero_compliance_relevant_edges_never_confirms_compliance(self):
        """C-14: Zero compliance-relevant edges must explicitly result in all_relationships_confirmed=False."""
        engine = DeterministicReconciliationEngine()

        info_edge = classify_document_relationship(
            edge_id="e_info",
            source_document_id="doc_coi",
            target_document_id="doc_end",
            relationship_type=DocumentRelationshipType.UNRELATED,
            classification=EdgeClassification.AMBIGUOUS,
            is_compliance_relevant=False,  # Non-compliance relevant
            requires_human_review=True,
        )

        result = engine.reconcile_packet(
            document_packet_id="packet_empty_graph",
            packet_manifest=(self.doc_coi_manifest, self.doc_end_manifest),
            graph_edges=(info_edge,),
        )
        # Must not prove compliance
        self.assertFalse(result.all_relationships_confirmed)
        # And must not fabricate unresolved contradictions from informational edges
        self.assertFalse(result.has_unresolved_contradictions)

    # =========================================================================
    # Correction 1: Contractual Role Satisfaction (C-15 to C-19)
    # =========================================================================

    def test_c15_coi_named_insured_endorsement_additional_insured_satisfies_ai_requirement(self):
        """C-15: Primary role is NAMED_INSURED on COI, referenced role is ADDITIONAL_INSURED on endorsement -> satisfies AI requirement."""
        ref_ai = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Apex Electrical LLC is included as an Additional Insured",
        )
        ent_record = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            evidence_chain=(self.ref_coi, ref_ai),
            referenced_role_evidence=ref_ai,
        )
        self.assertTrue(ent_record.is_confirmed_match)

        is_satisfied, disc = check_contractual_role_satisfaction(
            entity_record=ent_record,
            required_entity_name="Apex Electrical LLC",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_AI_01",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            is_requirement_validated=True,
        )
        self.assertTrue(is_satisfied)
        self.assertIsNone(disc)

    def test_c16_same_entity_no_evidence_of_required_role_unresolved(self):
        """C-16: Same entity with no evidence of the required role -> unresolved (MISSING_EVIDENCE), not confirmed violation."""
        ent_record = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            evidence_chain=(self.ref_coi, self.ref_end),
        )
        self.assertTrue(ent_record.is_confirmed_match)

        is_satisfied, disc = check_contractual_role_satisfaction(
            entity_record=ent_record,
            required_entity_name="Apex Electrical LLC",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_AI_02",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            is_requirement_validated=True,
        )
        self.assertFalse(is_satisfied)
        self.assertIsNotNone(disc)
        self.assertEqual(disc.category, DiscrepancyCategory.MISSING_EVIDENCE)
        self.assertEqual(disc.reason_code, "INSUFFICIENT_ROLE_EVIDENCE")
        self.assertTrue(disc.requires_human_review)

    def test_c17_primary_role_mismatch_referenced_role_satisfies_requirement(self):
        """C-17: Primary role mismatch (COI is NAMED_INSURED) but referenced role satisfies requirement -> satisfied, no violation."""
        ref_ai = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Apex Electrical LLC is included as an Additional Insured",
        )
        ent_record = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.EXACT,
            evidence_chain=(self.ref_coi, ref_ai),
            referenced_role_evidence=ref_ai,
            is_confirmed_match=True,
            requires_human_review=False,
        )
        is_satisfied, disc = check_contractual_role_satisfaction(
            entity_record=ent_record,
            required_entity_name="Apex Electrical LLC",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_AI_03",
            is_requirement_validated=True,
        )
        self.assertTrue(is_satisfied)
        self.assertIsNone(disc)

    def test_c18_entity_does_not_match_contractual_requirement_no_role_only_violation(self):
        """C-18: Entity does not match contractual requirement -> no role-only violation without sufficient identity evidence."""
        ent_record = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.EXACT,
            evidence_chain=(self.ref_coi, self.ref_end),
            is_confirmed_match=True,
            requires_human_review=False,
        )
        is_satisfied, disc = check_contractual_role_satisfaction(
            entity_record=ent_record,
            required_entity_name="Acme Builders Inc",
            required_role=EntityRole.NAMED_INSURED,
            contract_requirement_id="REQ_NI_01",
            is_requirement_validated=True,
        )
        self.assertFalse(is_satisfied)
        self.assertIsNotNone(disc)
        self.assertNotEqual(disc.reason_code, "CONTRACTUAL_ROLE_MISMATCH")
        self.assertEqual(disc.category, DiscrepancyCategory.DOCUMENT_DISCREPANCY)
        self.assertEqual(disc.reason_code, "ENTITY_NAME_MISMATCH")

    def test_c19_requirement_id_exists_but_not_validated_creates_no_violation(self):
        """C-19: Requirement ID exists but requirement is not validated -> no confirmed violation."""
        ent_record = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.EXACT,
            evidence_chain=(self.ref_coi, self.ref_end),
            is_confirmed_match=True,
            requires_human_review=False,
        )
        is_satisfied, disc = check_contractual_role_satisfaction(
            entity_record=ent_record,
            required_entity_name="Apex Electrical LLC",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_UNVALIDATED",
            is_requirement_validated=False,
        )
        self.assertFalse(is_satisfied)
        self.assertIsNone(disc)

    # =========================================================================
    # Correction 2: Relationship-Specific Evidence (C-20 to C-27)
    # =========================================================================

    def test_c20_same_policy_with_only_policy_number_unconfirmed(self):
        """C-20: SAME_POLICY with matching identifiers but no relationship-specific evidence -> unconfirmed."""
        ref_coi_pol = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="GL-12345",
        )
        ref_dec_pol = EvidenceReference(
            document_id="doc_dec",
            document_type=DocumentType.POLICY_DEC_PAGE,
            page_number=1,
            source_text="GL-12345",
        )
        edge = classify_document_relationship(
            edge_id="e_same_pol_bare",
            source_document_id="doc_coi",
            target_document_id="doc_dec",
            relationship_type=DocumentRelationshipType.SAME_POLICY,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_coi_pol, ref_dec_pol),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.AMBIGUOUS)

    def test_c21_endorsement_to_policy_with_only_matching_policy_number_unconfirmed(self):
        """C-21: ENDORSEMENT_TO_POLICY with only matching policy identifiers -> unconfirmed."""
        ref_coi_pol = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="GL-12345",
        )
        ref_end_pol = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="GL-12345",
        )
        edge = classify_document_relationship(
            edge_id="e_end_pol_bare",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.ENDORSEMENT_TO_POLICY,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_end_pol, ref_coi_pol),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.AMBIGUOUS)

    def test_c22_declaration_for_policy_without_declaration_evidence_unconfirmed(self):
        """C-22: DECLARATION_FOR_POLICY without evidence supporting declaration relationship -> unconfirmed."""
        ref_coi_generic = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="GL-12345 Commercial",
        )
        ref_dec_generic = EvidenceReference(
            document_id="doc_dec",
            document_type=DocumentType.POLICY_DEC_PAGE,
            page_number=1,
            source_text="GL-12345 Commercial",
        )
        edge = classify_document_relationship(
            edge_id="e_dec_generic",
            source_document_id="doc_dec",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.DECLARATION_FOR_POLICY,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_dec_generic, ref_coi_generic),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.AMBIGUOUS)

    def test_c23_replaces_document_with_generic_citations_unconfirmed(self):
        """C-23: REPLACES_DOCUMENT with generic citations but no supersession evidence -> unconfirmed."""
        edge = classify_document_relationship(
            edge_id="e_rep_generic",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.REPLACES_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(self.ref_end, self.ref_coi),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.AMBIGUOUS)

    def test_c24_amends_document_with_generic_citations_unconfirmed(self):
        """C-24: AMENDS_DOCUMENT with generic citations but no amendment evidence -> unconfirmed."""
        edge = classify_document_relationship(
            edge_id="e_amend_generic",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.AMENDS_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(self.ref_end, self.ref_coi),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.AMBIGUOUS)

    def test_c25_duplicate_of_with_generic_citations_unconfirmed(self):
        """C-25: DUPLICATE_OF with generic citations but no duplicate evidence -> unconfirmed."""
        edge = classify_document_relationship(
            edge_id="e_dup_generic",
            source_document_id="doc_coi",
            target_document_id="doc_end",
            relationship_type=DocumentRelationshipType.DUPLICATE_OF,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(self.ref_coi, self.ref_end),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.AMBIGUOUS)

    def test_c26_caller_cannot_bypass_evidence_validation_with_boolean_flag(self):
        """C-26: Caller cannot bypass evidence validation by setting boolean flag to True."""
        edge = classify_document_relationship(
            edge_id="e_bypass",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.REPLACES_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            has_relationship_evidence=True,
            evidence_references=(self.ref_end, self.ref_coi),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.AMBIGUOUS)

        with self.assertRaises(ValueError) as ctx:
            DocumentRelationshipEdge(
                edge_id="e_direct_bypass",
                source_document_id="doc_end",
                target_document_id="doc_coi",
                relationship_type=DocumentRelationshipType.REPLACES_DOCUMENT,
                classification=EdgeClassification.CONFIRMED_EXACT,
                is_confirmed=True,
                evidence_references=(self.ref_end, self.ref_coi),
            )
        self.assertIn("requires affirmative evidence containing replacement or supersession text", str(ctx.exception))

    def test_c27_evidence_from_wrong_source_or_target_document_rejected(self):
        """C-27: Evidence from wrong source or target document is rejected in referential integrity validation."""
        ref_third = EvidenceReference(
            document_id="doc_dec",
            document_type=DocumentType.POLICY_DEC_PAGE,
            page_number=1,
            source_text="This endorsement replaces prior form CG 20 10",
        )
        ref_coi_sup = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="This endorsement replaces prior form CG 20 10",
        )
        edge = DocumentRelationshipEdge(
            edge_id="e_wrong_doc",
            source_document_id="doc_coi",
            target_document_id="doc_end",
            relationship_type=DocumentRelationshipType.REPLACES_DOCUMENT,
            classification=EdgeClassification.AMBIGUOUS,
            is_confirmed=False,
            requires_human_review=True,
            evidence_references=(ref_coi_sup, ref_third),
        )
        with self.assertRaises(ValueError) as ctx:
            CrossDocumentReconciliationResult(
                document_packet_id="pkt_c27",
                packet_manifest=(self.doc_coi_manifest, self.doc_end_manifest, self.doc_dec_manifest),
                graph_edges=(edge,),
            )
        self.assertIn("not among its associated documents", str(ctx.exception))

    # =========================================================================
    # Correction 3: Conservative Policy Family Classification (C-28 to C-31)
    # =========================================================================

    def test_c28_leading_zero_difference_without_family_evidence_conflicting(self):
        """C-28: Leading-zero difference without supporting family evidence is CONFLICTING (not POSSIBLE_FAMILY)."""
        rec = reconcile_policy_identifiers(
            coi_document_id="doc_coi",
            coi_policy_number="0012345",
            coi_evidence=self.ref_coi,
            endorsement_document_id="doc_end",
            endorsement_policy_number="12345",
            endorsement_evidence=self.ref_end,
        )
        self.assertEqual(rec.classification, PolicyMatchClassification.CONFLICTING)
        self.assertNotEqual(rec.classification, PolicyMatchClassification.POSSIBLE_FAMILY)
        self.assertFalse(rec.is_confirmed_match)
        self.assertTrue(rec.requires_human_review)

    def test_c29_arbitrary_shared_prefix_not_possible_family(self):
        """C-29: Arbitrary shared prefix with different remaining characters is CONFLICTING (not POSSIBLE_FAMILY)."""
        rec = reconcile_policy_identifiers(
            coi_document_id="doc_coi",
            coi_policy_number="COMM-1234",
            coi_evidence=self.ref_coi,
            endorsement_document_id="doc_end",
            endorsement_policy_number="COMM-9876",
            endorsement_evidence=self.ref_end,
        )
        self.assertEqual(rec.classification, PolicyMatchClassification.CONFLICTING)
        self.assertNotEqual(rec.classification, PolicyMatchClassification.POSSIBLE_FAMILY)
        self.assertFalse(rec.is_confirmed_match)
        self.assertTrue(rec.requires_human_review)

    def test_c30_documented_renewal_suffix_pattern_possible_family_never_confirmed(self):
        """C-30: Documented renewal suffix patterns classify as POSSIBLE_FAMILY and are never confirmed."""
        pairs = [
            ("GL-98765-01", "GL-98765-02"),
            ("PKG-1000/2024", "PKG-1000/2025"),
            ("POL-555-R1", "POL-555-R2"),
            ("GL-98765", "GL-98765-01"),
        ]
        for pol_a, pol_b in pairs:
            rec = reconcile_policy_identifiers(
                coi_document_id="doc_coi",
                coi_policy_number=pol_a,
                coi_evidence=self.ref_coi,
                endorsement_document_id="doc_end",
                endorsement_policy_number=pol_b,
                endorsement_evidence=self.ref_end,
            )
            self.assertEqual(rec.classification, PolicyMatchClassification.POSSIBLE_FAMILY, f"Failed for {pol_a} vs {pol_b}")
            self.assertFalse(rec.is_confirmed_match)
            self.assertTrue(rec.requires_human_review)

    def test_c31_meaningful_alphanumeric_suffix_difference_not_possible_family(self):
        """C-31: Meaningful alphanumeric suffix difference without renewal evidence is CONFLICTING."""
        rec = reconcile_policy_identifiers(
            coi_document_id="doc_coi",
            coi_policy_number="POL-12345-A",
            coi_evidence=self.ref_coi,
            endorsement_document_id="doc_end",
            endorsement_policy_number="POL-12345-B",
            endorsement_evidence=self.ref_end,
        )
        self.assertEqual(rec.classification, PolicyMatchClassification.CONFLICTING)
        self.assertNotEqual(rec.classification, PolicyMatchClassification.POSSIBLE_FAMILY)

    # =========================================================================
    # Correction 4: Contractual Duration Validation (C-32 to C-35)
    # =========================================================================

    def test_c32_earlier_endorsement_expiration_no_validated_duration_requirement_no_violation(self):
        """C-32: Earlier endorsement expiration with no validated duration requirement -> no confirmed violation."""
        ref_coi_date = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy Period: 01/01/2025 to 12/31/2025",
        )
        ref_end_date = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Endorsement Effective 01/01/2025 to 06/30/2025",
        )
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2025, 12, 31),
            policy_date_evidence=(ref_coi_date,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2025, 6, 30),
            endorsement_date_evidence=(ref_end_date,),
            evaluation_date=date(2025, 3, 1),
            contractual_duration_requirement_id=None,
        )
        self.assertTrue(rec.is_policy_active_on_evaluation_date)
        self.assertTrue(rec.is_endorsement_active_on_evaluation_date)
        self.assertTrue(rec.is_premature_expiration)
        self.assertFalse(rec.requires_human_review)
        self.assertIsNone(rec.discrepancy_category)

    def test_c33_earlier_endorsement_expiration_requirement_does_not_demand_policy_expiration_no_violation(self):
        """C-33: Earlier endorsement expiration with requirement that does not require coverage through policy expiration -> no confirmed violation."""
        ref_coi_date = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy Period: 01/01/2025 to 12/31/2025",
        )
        ref_end_date = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Endorsement Effective 01/01/2025 to 06/30/2025",
        )
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2025, 12, 31),
            policy_date_evidence=(ref_coi_date,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2025, 6, 30),
            endorsement_date_evidence=(ref_end_date,),
            evaluation_date=date(2025, 3, 1),
            contractual_duration_requirement_id="REQ_DUR_01",
            required_coverage_end_date=date(2025, 5, 1),
            requires_coverage_through_policy_expiration=False,
            is_duration_requirement_validated=True,
        )
        self.assertTrue(rec.is_endorsement_active_on_evaluation_date)
        self.assertFalse(rec.requires_human_review)
        self.assertIsNone(rec.discrepancy_category)

    def test_c34_explicit_validated_requirement_through_policy_expiration_confirmed_violation(self):
        """C-34: Explicit validated requirement requiring coverage through policy expiration, with earlier endorsement expiration -> confirmed violation."""
        ref_coi_date = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy Period: 01/01/2025 to 12/31/2025",
        )
        ref_end_date = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Endorsement Effective 01/01/2025 to 06/30/2025",
        )
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2025, 12, 31),
            policy_date_evidence=(ref_coi_date,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2025, 6, 30),
            endorsement_date_evidence=(ref_end_date,),
            evaluation_date=date(2025, 3, 1),
            contractual_duration_requirement_id="REQ_DUR_POLICY_SPAN",
            requires_coverage_through_policy_expiration=True,
            is_duration_requirement_validated=True,
        )
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.discrepancy_category, DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION)
        self.assertEqual(rec.review_reason_code, "PREMATURE_EXPIRATION_VIOLATION")

    def test_c35_requirement_id_exists_but_actual_duration_terms_missing_unresolved(self):
        """C-35: Requirement ID exists but actual duration terms missing -> unresolved."""
        ref_coi_date = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy Period: 01/01/2025 to 12/31/2025",
        )
        ref_end_date = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Endorsement Effective 01/01/2025 to 06/30/2025",
        )
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2025, 12, 31),
            policy_date_evidence=(ref_coi_date,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2025, 6, 30),
            endorsement_date_evidence=(ref_end_date,),
            evaluation_date=date(2025, 3, 1),
            contractual_duration_requirement_id="REQ_DUR_UNVALIDATED",
            is_duration_requirement_validated=False,
        )
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.discrepancy_category, DiscrepancyCategory.DOCUMENT_DISCREPANCY)
        self.assertEqual(rec.review_reason_code, "UNVALIDATED_DURATION_REQUIREMENT")

    # =========================================================================
    # Correction 5: Empty vs Incomplete Relationship Graphs (C-36 to C-38)
    # =========================================================================

    def test_c36_valid_empty_graph_explicitly_represented_as_evaluated_empty(self):
        """C-36: Valid empty graph explicitly represented as EVALUATED_EMPTY."""
        result = CrossDocumentReconciliationResult(
            document_packet_id="packet_empty",
            packet_manifest=(self.doc_coi_manifest, self.doc_end_manifest),
            graph_edges=(),
        )
        self.assertEqual(result.graph_evaluation_status, GraphEvaluationStatus.EVALUATED_EMPTY)
        self.assertFalse(result.all_relationships_confirmed)

    def test_c37_incomplete_or_unevaluated_graph_distinguishable_from_evaluated_empty(self):
        """C-37: Incomplete/unprocessed graph distinguishable from valid empty graph (UNEVALUATED / INCOMPLETE)."""
        result_uneval = CrossDocumentReconciliationResult(
            document_packet_id="packet_uneval",
            packet_manifest=(self.doc_coi_manifest, self.doc_end_manifest),
            graph_edges=(),
            graph_evaluation_status=GraphEvaluationStatus.UNEVALUATED,
        )
        self.assertEqual(result_uneval.graph_evaluation_status, GraphEvaluationStatus.UNEVALUATED)
        self.assertNotEqual(result_uneval.graph_evaluation_status, GraphEvaluationStatus.EVALUATED_EMPTY)

        result_incomp = CrossDocumentReconciliationResult(
            document_packet_id="packet_incomp",
            packet_manifest=(self.doc_coi_manifest, self.doc_end_manifest),
            graph_edges=(),
            graph_evaluation_status=GraphEvaluationStatus.INCOMPLETE,
        )
        self.assertEqual(result_incomp.graph_evaluation_status, GraphEvaluationStatus.INCOMPLETE)
        self.assertNotEqual(result_incomp.graph_evaluation_status, GraphEvaluationStatus.EVALUATED_EMPTY)

    def test_c38_neither_empty_nor_unevaluated_state_confirms_compliance(self):
        """C-38: Neither empty nor unevaluated state independently establishes compliance (all_relationships_confirmed = False)."""
        res_empty = CrossDocumentReconciliationResult(
            document_packet_id="packet_empty_2",
            packet_manifest=(self.doc_coi_manifest, self.doc_end_manifest),
            graph_edges=(),
            graph_evaluation_status=GraphEvaluationStatus.EVALUATED_EMPTY,
        )
        self.assertFalse(res_empty.all_relationships_confirmed)

        res_uneval = CrossDocumentReconciliationResult(
            document_packet_id="packet_uneval_2",
            packet_manifest=(self.doc_coi_manifest, self.doc_end_manifest),
            graph_edges=(),
            graph_evaluation_status=GraphEvaluationStatus.UNEVALUATED,
        )
        self.assertFalse(res_uneval.all_relationships_confirmed)

        res_incomp = CrossDocumentReconciliationResult(
            document_packet_id="packet_incomp_2",
            packet_manifest=(self.doc_coi_manifest, self.doc_end_manifest),
            graph_edges=(),
            graph_evaluation_status=GraphEvaluationStatus.INCOMPLETE,
        )
        self.assertFalse(res_incomp.all_relationships_confirmed)


class TestMilestone35CFinalAuditCorrections(unittest.TestCase):
    """Focused regression test suite covering the 22 mandatory audit scenarios for Corrections A, B, C, D."""

    def setUp(self):
        self.doc_coi_manifest = DocumentManifestEntry(
            document_id="doc_coi",
            file_name="coi.pdf",
            sha256_hash="1" * 64,
            page_count=2,
            detected_document_type=DocumentType.COI,
        )
        self.doc_end_manifest = DocumentManifestEntry(
            document_id="doc_end",
            file_name="endorsement.pdf",
            sha256_hash="2" * 64,
            page_count=3,
            detected_document_type=DocumentType.ENDORSEMENT,
        )
        self.doc_dec_manifest = DocumentManifestEntry(
            document_id="doc_dec",
            file_name="dec.pdf",
            sha256_hash="3" * 64,
            page_count=4,
            detected_document_type=DocumentType.POLICY_DEC_PAGE,
        )

        self.ref_coi_entity = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Apex Electrical LLC",
        )
        self.ref_end_entity = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Apex Electrical LLC",
        )
        self.ref_coi_policy = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy GL-12345 Commercial General Liability Coverage",
        )
        self.ref_end_policy = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Policy GL-12345 Commercial General Liability Coverage",
        )
        self.ref_coi_dates = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy Period: 01/01/2025 to 01/01/2026",
        )
        self.ref_end_dates = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Endorsement Effective: 01/01/2025 Expiration: 01/01/2026",
        )

    # -------------------------------------------------------------------------
    # Correction A: Fail-Closed Date Reconciliation
    # -------------------------------------------------------------------------

    def test_audit_a01_missing_endorsement_expiration_fails_closed(self):
        """1. Missing endorsement expiration date cannot silently produce requires_human_review=False."""
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(self.ref_coi_dates,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=None,  # Missing!
            endorsement_date_evidence=(self.ref_end_dates,),
            evaluation_date=date(2025, 6, 1),
        )
        self.assertIsNone(rec.is_endorsement_active_on_evaluation_date)
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.discrepancy_category, DiscrepancyCategory.MISSING_EVIDENCE)
        self.assertEqual(rec.review_reason_code, "MISSING_ENDORSEMENT_DATES")

    def test_audit_a02_missing_policy_effective_or_expiration_no_active_status(self):
        """2. Missing policy effective or expiration date cannot establish active status."""
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=None,  # Missing!
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(self.ref_coi_dates,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2026, 1, 1),
            endorsement_date_evidence=(self.ref_end_dates,),
            evaluation_date=date(2025, 6, 1),
        )
        self.assertIsNone(rec.is_policy_active_on_evaluation_date)
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.discrepancy_category, DiscrepancyCategory.MISSING_EVIDENCE)
        self.assertEqual(rec.review_reason_code, "MISSING_POLICY_DATES")

    def test_audit_a03_missing_evaluation_date_leaves_active_states_unknown(self):
        """3. Missing evaluation date leaves active states unknown."""
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(self.ref_coi_dates,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2026, 1, 1),
            endorsement_date_evidence=(self.ref_end_dates,),
            evaluation_date=None,  # Missing!
        )
        self.assertIsNone(rec.is_policy_active_on_evaluation_date)
        self.assertIsNone(rec.is_endorsement_active_on_evaluation_date)
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.discrepancy_category, DiscrepancyCategory.MISSING_EVIDENCE)
        self.assertEqual(rec.review_reason_code, "MISSING_EVALUATION_DATE")

    def test_audit_a04_complete_supported_dates_establish_active_status_without_review(self):
        """4. Complete supported dates establish active status without human review when no other discrepancy exists."""
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(self.ref_coi_dates,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2026, 1, 1),
            endorsement_date_evidence=(self.ref_end_dates,),
            evaluation_date=date(2025, 6, 1),
        )
        self.assertTrue(rec.is_policy_active_on_evaluation_date)
        self.assertTrue(rec.is_endorsement_active_on_evaluation_date)
        self.assertFalse(rec.requires_human_review)
        self.assertIsNone(rec.discrepancy_category)
        self.assertIsNone(rec.review_reason_code)

    def test_audit_a05_genuinely_expired_endorsement_produces_document_discrepancy(self):
        """5. Genuinely expired endorsement produces a supported DOCUMENT_DISCREPANCY, distinct from missing date information."""
        ref_end_expired = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Endorsement Effective: 01/01/2025 Expiration: 05/01/2025",
        )
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(self.ref_coi_dates,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2025, 5, 1),  # Expired before evaluation date
            endorsement_date_evidence=(ref_end_expired,),
            evaluation_date=date(2025, 6, 1),
        )
        self.assertTrue(rec.is_policy_active_on_evaluation_date)
        self.assertFalse(rec.is_endorsement_active_on_evaluation_date)
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.discrepancy_category, DiscrepancyCategory.DOCUMENT_DISCREPANCY)
        self.assertEqual(rec.review_reason_code, "ENDORSEMENT_INACTIVE_ON_EVALUATION_DATE")

    def test_audit_a06_earlier_endorsement_expiration_alone_not_confirmed_violation(self):
        """6. Earlier endorsement expiration alone does not create a confirmed violation."""
        ref_end_oct = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Endorsement Effective: 01/01/2025 Expiration: 10/01/2025",
        )
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(self.ref_coi_dates,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2025, 10, 1),  # Expired before policy, but active on eval date
            endorsement_date_evidence=(ref_end_oct,),
            evaluation_date=date(2025, 6, 1),
            requires_coverage_through_policy_expiration=False,
        )
        self.assertTrue(rec.is_policy_active_on_evaluation_date)
        self.assertTrue(rec.is_endorsement_active_on_evaluation_date)
        self.assertTrue(rec.is_premature_expiration)
        self.assertFalse(rec.requires_human_review)
        self.assertIsNone(rec.discrepancy_category)

    def test_audit_a07_explicit_validated_duration_requirement_violates_only_when_unmet(self):
        """7. An explicit validated duration requirement produces a confirmed violation only when supported required end date is not met."""
        ref_end_sep = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Endorsement Effective: 01/01/2025 Expiration: 09/01/2025",
        )
        # Unmet: endorsement expires before validated required end date
        rec_unmet = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(self.ref_coi_dates,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2025, 9, 1),
            endorsement_date_evidence=(ref_end_sep,),
            evaluation_date=date(2025, 6, 1),
            contractual_duration_requirement_id="REQ_DUR_PROJECT_SPAN",
            required_coverage_end_date=date(2025, 12, 31),
            is_duration_requirement_validated=True,
        )
        self.assertTrue(rec_unmet.requires_human_review)
        self.assertEqual(rec_unmet.discrepancy_category, DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION)
        self.assertEqual(rec_unmet.review_reason_code, "PREMATURE_EXPIRATION_VIOLATION")

        # Met: endorsement expires after required end date
        rec_met = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(self.ref_coi_dates,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2026, 1, 1),
            endorsement_date_evidence=(self.ref_end_dates,),
            evaluation_date=date(2025, 6, 1),
            contractual_duration_requirement_id="REQ_DUR_PROJECT_SPAN",
            required_coverage_end_date=date(2025, 12, 31),
            is_duration_requirement_validated=True,
        )
        self.assertFalse(rec_met.requires_human_review)
        self.assertIsNone(rec_met.discrepancy_category)

    # -------------------------------------------------------------------------
    # Correction B: Relationship-Specific Evidence
    # -------------------------------------------------------------------------

    def test_audit_b01_unrelated_prose_copy_does_not_confirm_duplicate_of(self):
        """8. The word 'copy' in unrelated prose ('Please retain a copy for your records') does not confirm DUPLICATE_OF."""
        ref_copy = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Please retain a copy for your records",
        )
        edge = classify_document_relationship(
            edge_id="e_copy",
            source_document_id="doc_coi",
            target_document_id="doc_end",
            relationship_type=DocumentRelationshipType.DUPLICATE_OF,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_copy, self.ref_end_policy),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.AMBIGUOUS)

    def test_audit_b02_unrelated_clause_adds_does_not_confirm_amends(self):
        """9. The word 'adds' in an unrelated clause ('Vendor adds a handling fee') does not confirm AMENDS_DOCUMENT."""
        ref_adds = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Vendor adds a handling fee for late payments",
        )
        edge = classify_document_relationship(
            edge_id="e_adds",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.AMENDS_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_adds, self.ref_coi_policy),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.AMBIGUOUS)

    def test_audit_b03_unrelated_replaces_air_filter_does_not_confirm_replaces(self):
        """10. 'Customer replaces air filter every month' does not confirm REPLACES_DOCUMENT."""
        ref_filter = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Customer replaces air filter every month per maintenance schedule",
        )
        edge = classify_document_relationship(
            edge_id="e_filter",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.REPLACES_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_filter, self.ref_coi_policy),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.AMBIGUOUS)

    def test_audit_b04_arbitrary_non_policy_text_does_not_confirm_same_policy(self):
        """11. Arbitrary non-policy text ('Apex Electrical LLC', 'Payment Terms') does not confirm SAME_POLICY or ENDORSEMENT_TO_POLICY."""
        ref_pay = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Payment Terms: Net 30 Days",
        )
        edge_same = classify_document_relationship(
            edge_id="e_non_pol_same",
            source_document_id="doc_coi",
            target_document_id="doc_end",
            relationship_type=DocumentRelationshipType.SAME_POLICY,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(self.ref_coi_entity, ref_pay),
        )
        self.assertFalse(edge_same.is_confirmed)
        self.assertTrue(edge_same.requires_human_review)

        edge_end = classify_document_relationship(
            edge_id="e_non_pol_end",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.ENDORSEMENT_TO_POLICY,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_pay, self.ref_coi_entity),
        )
        self.assertFalse(edge_end.is_confirmed)
        self.assertTrue(edge_end.requires_human_review)

    def test_audit_b05_relationship_keyword_in_one_doc_alone_does_not_confirm(self):
        """12. Relationship keyword appearing in one document alone does not establish relationship with another document."""
        ref_end_valid = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement attaches to and forms part of Policy GL-12345",
        )
        ref_coi_no_pol = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Invoice reference number only without details",
        )
        edge = classify_document_relationship(
            edge_id="e_one_sided",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.ENDORSEMENT_TO_POLICY,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_end_valid, ref_coi_no_pol),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)

    def test_audit_b06_positive_explicit_relationship_evidence_for_all_types(self):
        """13. Positive tests: explicit replacement, amendment, duplicate, endorsement-to-policy, declaration-for-policy, same-policy evidence."""
        # 1. REPLACES_DOCUMENT
        ref_rep = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement replaces prior form CG 20 10 11 85 for policy GL-12345",
        )
        edge_rep = classify_document_relationship(
            edge_id="e_pos_rep",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.REPLACES_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_rep, self.ref_coi_policy),
        )
        self.assertTrue(edge_rep.is_confirmed)
        self.assertFalse(edge_rep.requires_human_review)

        # 2. AMENDS_DOCUMENT
        ref_amend = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement modifies coverage provided under policy GL-12345",
        )
        edge_amend = classify_document_relationship(
            edge_id="e_pos_amend",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.AMENDS_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_amend, self.ref_coi_policy),
        )
        self.assertTrue(edge_amend.is_confirmed)
        self.assertFalse(edge_amend.requires_human_review)

        # 3. DUPLICATE_OF
        ref_dup1 = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Duplicate copy of certificate with identical sha256 checksum",
        )
        ref_dup2 = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Conformed copy matching original file exactly",
        )
        edge_dup = classify_document_relationship(
            edge_id="e_pos_dup",
            source_document_id="doc_coi",
            target_document_id="doc_end",
            relationship_type=DocumentRelationshipType.DUPLICATE_OF,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_dup1, ref_dup2),
        )
        self.assertTrue(edge_dup.is_confirmed)
        self.assertFalse(edge_dup.requires_human_review)

        # 4. ENDORSEMENT_TO_POLICY
        ref_end_att = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement forms part of Policy GL-12345",
        )
        edge_end_pol = classify_document_relationship(
            edge_id="e_pos_end_pol",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.ENDORSEMENT_TO_POLICY,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_end_att, self.ref_coi_policy),
        )
        self.assertTrue(edge_end_pol.is_confirmed)
        self.assertFalse(edge_end_pol.requires_human_review)

        # 5. DECLARATION_FOR_POLICY
        ref_dec = EvidenceReference(
            document_id="doc_dec",
            document_type=DocumentType.POLICY_DEC_PAGE,
            page_number=1,
            source_text="Common Policy Declarations Page Schedule",
        )
        edge_dec = classify_document_relationship(
            edge_id="e_pos_dec",
            source_document_id="doc_dec",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.DECLARATION_FOR_POLICY,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_dec, self.ref_coi_policy),
        )
        self.assertTrue(edge_dec.is_confirmed)
        self.assertFalse(edge_dec.requires_human_review)

        # 6. SAME_POLICY
        edge_same = classify_document_relationship(
            edge_id="e_pos_same",
            source_document_id="doc_coi",
            target_document_id="doc_end",
            relationship_type=DocumentRelationshipType.SAME_POLICY,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(self.ref_coi_policy, self.ref_end_policy),
        )
        self.assertTrue(edge_same.is_confirmed)
        self.assertFalse(edge_same.requires_human_review)

    # -------------------------------------------------------------------------
    # Correction C: Role-Specific Evidence
    # -------------------------------------------------------------------------

    def test_audit_c01_same_legal_name_different_roles_with_evidence_satisfies_requirement(self):
        """14. Entity with same legal name, verified as named insured on COI and additional insured on endorsement with explicit role evidence, satisfies requirement."""
        ref_ai = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Apex Electrical LLC is included as an Additional Insured",
        )
        ent_record = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            evidence_chain=(self.ref_coi_entity, ref_ai),
            referenced_role_evidence=ref_ai,
        )
        self.assertTrue(ent_record.is_confirmed_match)

        is_satisfied, disc = check_contractual_role_satisfaction(
            entity_record=ent_record,
            required_entity_name="Apex Electrical LLC",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_AI_AUDIT",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            is_requirement_validated=True,
        )
        self.assertTrue(is_satisfied)
        self.assertIsNone(disc)

    def test_audit_c02_same_entity_name_only_no_role_evidence_unresolved(self):
        """15. Same entity with only name evidence and no role evidence produces an unresolved/missing-evidence result."""
        ent_record = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            evidence_chain=(self.ref_coi_entity, self.ref_end_entity),  # Name citations only, no role keywords
        )
        self.assertTrue(ent_record.is_confirmed_match)

        is_satisfied, disc = check_contractual_role_satisfaction(
            entity_record=ent_record,
            required_entity_name="Apex Electrical LLC",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_AI_AUDIT_2",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            is_requirement_validated=True,
        )
        self.assertFalse(is_satisfied)
        self.assertIsNotNone(disc)
        self.assertEqual(disc.category, DiscrepancyCategory.MISSING_EVIDENCE)
        self.assertEqual(disc.reason_code, "MISSING_ROLE_EVIDENCE")
        self.assertTrue(disc.requires_human_review)

    def test_audit_c03_correct_entity_wrong_role_unresolved(self):
        """16. Correct entity name but wrong role produces unresolved/missing-evidence result."""
        ent_record = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.NAMED_INSURED,  # Both are NAMED_INSURED, none is ADDITIONAL_INSURED
            referenced_document_id="doc_end",
            evidence_chain=(self.ref_coi_entity, self.ref_end_entity),
        )
        is_satisfied, disc = check_contractual_role_satisfaction(
            entity_record=ent_record,
            required_entity_name="Apex Electrical LLC",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_AI_AUDIT_3",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            is_requirement_validated=True,
        )
        self.assertFalse(is_satisfied)
        self.assertIsNotNone(disc)
        self.assertEqual(disc.category, DiscrepancyCategory.MISSING_EVIDENCE)
        self.assertEqual(disc.reason_code, "INSUFFICIENT_ROLE_EVIDENCE")
        self.assertTrue(disc.requires_human_review)

    def test_audit_c04_unvalidated_requirement_creates_no_violation(self):
        """17. Missing or unvalidated contractual requirement creates no violation."""
        ref_ai = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Apex Electrical LLC is included as an Additional Insured",
        )
        ent_record = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            evidence_chain=(self.ref_coi_entity, ref_ai),
            referenced_role_evidence=ref_ai,
        )
        is_satisfied, disc = check_contractual_role_satisfaction(
            entity_record=ent_record,
            required_entity_name="Apex Electrical LLC",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_UNVALIDATED",
            is_requirement_validated=False,
        )
        self.assertFalse(is_satisfied)
        self.assertIsNone(disc)

    def test_audit_c05_different_entity_without_evidence_unresolved_not_violation(self):
        """18. Different entity with insufficient evidence to establish confirmed mismatch produces unresolved discrepancy."""
        ent_record = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Zenith Plumbing Inc",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            applicable_contract_requirement_id="REQ_ZENITH",
            evidence_chain=(self.ref_coi_entity,),  # Only 1 doc evidence -> AMBIGUOUS_IDENTITY
        )
        self.assertEqual(ent_record.classification, EntityMatchClassification.AMBIGUOUS_IDENTITY)
        self.assertTrue(ent_record.requires_human_review)

        is_satisfied, disc = check_contractual_role_satisfaction(
            entity_record=ent_record,
            required_entity_name="Contoso Construction",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_ZENITH",
            is_requirement_validated=True,
        )
        self.assertFalse(is_satisfied)
        self.assertIsNotNone(disc)
        self.assertEqual(disc.category, DiscrepancyCategory.DOCUMENT_DISCREPANCY)
        self.assertEqual(disc.reason_code, "ENTITY_NAME_MISMATCH")

    # -------------------------------------------------------------------------
    # Correction D: Confirmed Mismatch Evidence
    # -------------------------------------------------------------------------

    def test_audit_d01_mismatch_evidence_only_from_primary_doc_cannot_confirm(self):
        """19. Evidence only from the primary document cannot confirm mismatch."""
        rec = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Zenith Plumbing Inc",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            applicable_contract_requirement_id="REQ_ZENITH",
            evidence_chain=(self.ref_coi_entity,),  # Only primary document
        )
        self.assertEqual(rec.classification, EntityMatchClassification.AMBIGUOUS_IDENTITY)
        self.assertFalse(rec.is_confirmed_match)
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.review_reason_code, "INSUFFICIENT_MISMATCH_EVIDENCE")

    def test_audit_d02_mismatch_evidence_only_from_referenced_doc_cannot_confirm(self):
        """20. Evidence only from the referenced document cannot confirm mismatch."""
        rec = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Zenith Plumbing Inc",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            applicable_contract_requirement_id="REQ_ZENITH",
            evidence_chain=(self.ref_end_entity,),  # Only referenced document
        )
        self.assertEqual(rec.classification, EntityMatchClassification.AMBIGUOUS_IDENTITY)
        self.assertFalse(rec.is_confirmed_match)
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.review_reason_code, "INSUFFICIENT_MISMATCH_EVIDENCE")

    def test_audit_d03_mismatch_evidence_from_unrelated_doc_rejected(self):
        """21. Evidence from an unrelated document cannot support a confirmed mismatch."""
        ref_dec = EvidenceReference(
            document_id="doc_dec",
            document_type=DocumentType.POLICY_DEC_PAGE,
            page_number=1,
            source_text="Zenith Plumbing Inc",
        )
        # Attempting to directly construct CONFIRMED_MISMATCH with unrelated document reference raises ValueError
        with self.assertRaises(ValueError) as ctx:
            EntityReconciliationRecord(
                primary_entity_name="Apex Electrical LLC",
                primary_entity_role=EntityRole.NAMED_INSURED,
                primary_document_id="doc_coi",
                referenced_entity_name="Zenith Plumbing Inc",
                referenced_entity_role=EntityRole.NAMED_INSURED,
                referenced_document_id="doc_end",
                classification=EntityMatchClassification.CONFIRMED_MISMATCH,
                applicable_contract_requirement_id="REQ_ZENITH",
                evidence_chain=(self.ref_coi_entity, ref_dec),  # ref_dec is neither doc_coi nor doc_end!
                is_confirmed_match=False,
                requires_human_review=False,
            )
        self.assertIn("Cross-document evidence reference rejected", str(ctx.exception))

    def test_audit_d04_validated_mismatch_supported_by_both_docs_confirmed(self):
        """22. Validated mismatch supported by affirmative evidence from both documents confirms mismatch with requires_human_review=False."""
        ref_end_zenith = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Zenith Plumbing Inc",
        )
        rec = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Zenith Plumbing Inc",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            applicable_contract_requirement_id="REQ_ZENITH",
            evidence_chain=(self.ref_coi_entity, ref_end_zenith),
        )
        self.assertEqual(rec.classification, EntityMatchClassification.CONFIRMED_MISMATCH)
        self.assertFalse(rec.is_confirmed_match)
        self.assertFalse(rec.requires_human_review)
        self.assertEqual(rec.review_reason_code, "AFFIRMATIVE_CONTRACTUAL_ENTITY_MISMATCH")


# =============================================================================
# Milestone 3.5D WP1: Evidence Semantic Integrity Adversarial Tests
# =============================================================================


class TestMilestone35DEvidenceIntegrity(unittest.TestCase):
    """WP1 Adversarial regression test suite covering Date, Role, and Entity Mismatch evidence integrity."""

    def setUp(self):
        self.doc_coi_manifest = DocumentManifestEntry(
            document_id="doc_coi",
            file_name="coi.pdf",
            sha256_hash="1" * 64,
            page_count=2,
            detected_document_type=DocumentType.COI,
        )
        self.doc_end_manifest = DocumentManifestEntry(
            document_id="doc_end",
            file_name="endorsement.pdf",
            sha256_hash="2" * 64,
            page_count=3,
            detected_document_type=DocumentType.ENDORSEMENT,
        )
        self.ref_coi_default = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Apex Electrical LLC",
        )

    # =========================================================================
    # 1. Date Evidence Semantic Validation (11 tests)
    # =========================================================================

    def test_wp1_date_01_correct_effective_and_expiration_labels(self):
        """1. Correct effective and expiration date labels establish active status without review."""
        ref_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy Period: Effective 01/01/2025 Expiration 01/01/2026",
        )
        ref_end = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Endorsement Effective Date: 01/01/2025 Expiration Date: 01/01/2026",
        )
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(ref_coi,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2026, 1, 1),
            endorsement_date_evidence=(ref_end,),
            evaluation_date=date(2025, 6, 1),
        )
        self.assertTrue(rec.is_policy_active_on_evaluation_date)
        self.assertTrue(rec.is_endorsement_active_on_evaluation_date)
        self.assertFalse(rec.requires_human_review)
        self.assertIsNone(rec.discrepancy_category)

    def test_wp1_date_02_invoice_payment_clause_rejected(self):
        """2. Correct date appearing only in an invoice or payment clause must fail closed."""
        ref_coi_invoice = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Invoice #98234 paid on 01/01/2025 by wire transfer. Total billed: $1,250",
        )
        ref_end = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Endorsement Effective Date: 01/01/2025 Expiration Date: 01/01/2026",
        )
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(ref_coi_invoice,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2026, 1, 1),
            endorsement_date_evidence=(ref_end,),
            evaluation_date=date(2025, 6, 1),
        )
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.discrepancy_category, DiscrepancyCategory.MISSING_EVIDENCE)
        self.assertEqual(rec.review_reason_code, "UNSUPPORTED_DATE_EVIDENCE")
        self.assertIsNone(rec.is_policy_active_on_evaluation_date)

    def test_wp1_date_03_prior_policy_clause_rejected(self):
        """3. Correct date appearing only as a prior policy date must fail closed for current policy."""
        ref_coi_prior = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Prior policy expiration date was 01/01/2025 under prior carrier",
        )
        ref_end = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Endorsement Effective Date: 01/01/2025 Expiration Date: 01/01/2026",
        )
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(ref_coi_prior,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2026, 1, 1),
            endorsement_date_evidence=(ref_end,),
            evaluation_date=date(2025, 6, 1),
        )
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.discrepancy_category, DiscrepancyCategory.MISSING_EVIDENCE)
        self.assertEqual(rec.review_reason_code, "UNSUPPORTED_DATE_EVIDENCE")
        self.assertIsNone(rec.is_policy_active_on_evaluation_date)

    def test_wp1_date_04_reversed_labels_rejected(self):
        """4. Effective and expiration dates with reversed labels must fail closed."""
        ref_coi_reversed = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Effective Date: 01/01/2026 Expiration Date: 01/01/2025",
        )
        ref_end = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Endorsement Effective Date: 01/01/2025 Expiration Date: 01/01/2026",
        )
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(ref_coi_reversed,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2026, 1, 1),
            endorsement_date_evidence=(ref_end,),
            evaluation_date=date(2025, 6, 1),
        )
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.discrepancy_category, DiscrepancyCategory.MISSING_EVIDENCE)
        self.assertEqual(rec.review_reason_code, "UNSUPPORTED_DATE_EVIDENCE")

    def test_wp1_date_05_multiple_dates_with_correct_labels(self):
        """5. Multiple dates in a single excerpt with correct labels support current policy dates."""
        ref_coi_multi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text=(
                "Prior policy expired 12/31/2024. Current policy period: "
                "Inception 01/01/2025 Expiration 01/01/2026. Premium billed 02/01/2025."
            ),
        )
        ref_end = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Endorsement Effective Date: 01/01/2025 Expiration Date: 01/01/2026",
        )
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(ref_coi_multi,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 1, 1),
            endorsement_expiration_date=date(2026, 1, 1),
            endorsement_date_evidence=(ref_end,),
            evaluation_date=date(2025, 6, 1),
        )
        self.assertTrue(rec.is_policy_active_on_evaluation_date)
        self.assertTrue(rec.is_endorsement_active_on_evaluation_date)
        self.assertFalse(rec.requires_human_review)
        self.assertIsNone(rec.discrepancy_category)

    def test_wp1_date_06_wrong_document_reference_rejected(self):
        """6. Correct date in an evidence reference from the wrong document must fail closed."""
        ref_dec_wrong_doc = EvidenceReference(
            document_id="doc_dec",
            document_type=DocumentType.POLICY_DEC_PAGE,
            page_number=1,
            source_text="Policy Period: Effective 01/01/2025 Expiration 01/01/2026",
        )
        self.assertFalse(
            _evidence_supports_date(
                date(2025, 1, 1),
                (ref_dec_wrong_doc,),
                date_field="effective",
                doc_id="doc_coi",
            )
        )
        with self.assertRaises(ValueError):
            reconcile_dates(
                policy_document_id="doc_coi",
                policy_effective_date=date(2025, 1, 1),
                policy_expiration_date=date(2026, 1, 1),
                policy_date_evidence=(ref_dec_wrong_doc,),
                endorsement_document_id="doc_end",
                endorsement_effective_date=date(2025, 1, 1),
                endorsement_expiration_date=date(2026, 1, 1),
                endorsement_date_evidence=(),
                evaluation_date=date(2025, 6, 1),
            )

    def test_wp1_date_07_ambiguous_numeric_date_rejected(self):
        """7. Ambiguous numeric date interpretation where month and day could be transposed is rejected."""
        ref_coi_ambig = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Effective Date: 05/06/2025 Expiration Date: 01/01/2026",
        )
        ref_end = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Endorsement Effective Date: 06/05/2025 Expiration Date: 01/01/2026",
        )
        rec = reconcile_dates(
            policy_document_id="doc_coi",
            policy_effective_date=date(2025, 6, 5),  # June 5, 2025
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(ref_coi_ambig,),
            endorsement_document_id="doc_end",
            endorsement_effective_date=date(2025, 6, 5),
            endorsement_expiration_date=date(2026, 1, 1),
            endorsement_date_evidence=(ref_end,),
            evaluation_date=date(2025, 7, 1),
        )
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.discrepancy_category, DiscrepancyCategory.MISSING_EVIDENCE)
        self.assertEqual(rec.review_reason_code, "UNSUPPORTED_DATE_EVIDENCE")

    def test_wp1_date_08_two_digit_year_rejected_without_convention(self):
        """8. Two-digit year numeric date (e.g. 01/02/25) rejected without established date convention."""
        ref_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy Effective Date: 01/02/25 Expiration Date: 01/01/2026",
        )
        self.assertFalse(
            _evidence_supports_date(
                date(2025, 1, 2),
                (ref_coi,),
                date_field="effective",
                doc_id="doc_coi",
            )
        )

    def test_wp1_date_09_established_convention_accepts_two_digit_year(self):
        """9. Explicitly established date convention (MM/DD/YY) accepts two-digit year."""
        ref_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Date format: MM/DD/YY. Policy Effective Date: 01/02/25 Expiration Date: 01/01/26",
        )
        self.assertTrue(
            _evidence_supports_date(
                date(2025, 1, 2),
                (ref_coi,),
                date_field="effective",
                doc_id="doc_coi",
            )
        )

    def test_wp1_date_10_unambiguous_iso_and_textual_dates_preserved(self):
        """10. Unambiguous ISO and textual month dates are accepted without requiring explicit convention."""
        ref_iso = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy Effective: 2025-01-02 Expiration: 2026-01-01",
        )
        ref_textual = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy Effective: Jan 2, 2025 Expiration: Jan 1, 2026",
        )
        self.assertTrue(
            _evidence_supports_date(
                date(2025, 1, 2),
                (ref_iso,),
                date_field="effective",
                doc_id="doc_coi",
            )
        )
        self.assertTrue(
            _evidence_supports_date(
                date(2025, 1, 2),
                (ref_textual,),
                date_field="effective",
                doc_id="doc_coi",
            )
        )

    def test_wp1_date_11_date_ranges_with_to_hyphen_through(self):
        """11. Date range expressions using 'to', '-', and 'through' correctly attribute effective and expiration."""
        ref_to = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy Period: 01/01/2025 to 01/01/2026",
        )
        ref_hyphen = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Coverage Period: 01/01/2025 - 01/01/2026",
        )
        ref_through = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy Term: 01/01/2025 through 01/01/2026",
        )
        for ref in (ref_to, ref_hyphen, ref_through):
            self.assertTrue(_evidence_supports_date(date(2025, 1, 1), (ref,), date_field="effective", doc_id="doc_coi"))
            self.assertTrue(_evidence_supports_date(date(2026, 1, 1), (ref,), date_field="expiration", doc_id="doc_coi"))

    def test_wp1_date_12_established_mm_dd_yyyy_convention(self):
        """12. MM/DD/YYYY numeric dates match when MM/DD convention is established."""
        ref_mm_dd = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Date format: MM/DD/YYYY. Policy Effective: 03/05/2025 Expiration: 03/05/2026",
        )
        self.assertTrue(_evidence_supports_date(date(2025, 3, 5), (ref_mm_dd,), date_field="effective", doc_id="doc_coi"))
        self.assertTrue(_evidence_supports_date(date(2026, 3, 5), (ref_mm_dd,), date_field="expiration", doc_id="doc_coi"))

    def test_wp1_date_13_established_dd_mm_yyyy_convention(self):
        """13. DD/MM/YYYY numeric dates match when DD/MM convention is established."""
        ref_dd_mm = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Date format: DD/MM/YYYY. Policy Effective: 05/03/2025 Expiration: 05/03/2026",
        )
        self.assertTrue(_evidence_supports_date(date(2025, 3, 5), (ref_dd_mm,), date_field="effective", doc_id="doc_coi"))
        self.assertTrue(_evidence_supports_date(date(2026, 3, 5), (ref_dd_mm,), date_field="expiration", doc_id="doc_coi"))

    def test_wp1_date_14_established_two_digit_year_conventions(self):
        """14. Two-digit year numeric dates match with MM/DD/YY and DD/MM/YY conventions."""
        ref_mm_yy = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Date format: MM/DD/YY. Policy Effective: 04/07/25 Expiration: 04/07/26",
        )
        ref_dd_yy = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Date format: DD/MM/YY. Policy Effective: 07/04/25 Expiration: 07/04/26",
        )
        self.assertTrue(_evidence_supports_date(date(2025, 4, 7), (ref_mm_yy,), date_field="effective", doc_id="doc_coi"))
        self.assertTrue(_evidence_supports_date(date(2025, 4, 7), (ref_dd_yy,), date_field="effective", doc_id="doc_coi"))

    # =========================================================================
    # 2. Contractual-Role Evidence Semantic Validation (10 tests)
    # =========================================================================

    def test_wp1_role_01_correct_entity_explicitly_associated_ai(self):
        """1. Correct entity explicitly associated with Additional Insured satisfies requirement."""
        ref_ai = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Apex Electrical LLC is included as an Additional Insured under this policy",
        )
        ref_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Apex Electrical LLC",
        )
        ent_record = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            evidence_chain=(ref_coi, ref_ai),
            referenced_role_evidence=ref_ai,
        )
        is_sat, disc = check_contractual_role_satisfaction(
            entity_record=ent_record,
            required_entity_name="Apex Electrical LLC",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_AI_01",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            is_requirement_validated=True,
        )
        self.assertTrue(is_sat)
        self.assertIsNone(disc)

    def test_wp1_role_02_different_entity_associated_ai_evaluated_separate(self):
        """2. Different entity associated with Additional Insured, evaluated entity mentioned separately fails closed."""
        ref_diff = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Zenith Plumbing Inc is included as an Additional Insured. Apex Electrical LLC is the general contractor.",
        )
        ref_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Apex Electrical LLC",
        )
        ent_record = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            evidence_chain=(ref_coi, ref_diff),
            referenced_role_evidence=ref_diff,
        )
        is_sat, disc = check_contractual_role_satisfaction(
            entity_record=ent_record,
            required_entity_name="Apex Electrical LLC",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_AI_02",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            is_requirement_validated=True,
        )
        self.assertFalse(is_sat)

    def test_wp1_role_03_named_insured_not_additional_insured(self):
        """3. Correct entity mentioned as Named Insured but not Additional Insured fails closed for AI requirement."""
        ref_roles = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Additional Insured: Zenith Plumbing Inc. Named Insured: Apex Electrical LLC.",
        )
        ref_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Apex Electrical LLC",
        )
        ent_record = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            evidence_chain=(ref_coi, ref_roles),
            referenced_role_evidence=ref_roles,
        )
        is_sat, disc = check_contractual_role_satisfaction(
            entity_record=ent_record,
            required_entity_name="Apex Electrical LLC",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_AI_03",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            is_requirement_validated=True,
        )
        self.assertFalse(is_sat)

    def test_wp1_role_04_multiple_entities_different_roles(self):
        """4. Multiple entities with different roles in the same excerpt correctly isolates role assignments."""
        ref_multi_roles = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Named Insured: Acme Corp. Certificate Holder: City of Metropolis. Additional Insured: Apex Electrical LLC.",
        )
        ref_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Apex Electrical LLC",
        )
        # Apex Electrical LLC as Additional Insured -> Satisfied
        rec_apex = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            evidence_chain=(ref_coi, ref_multi_roles),
            referenced_role_evidence=ref_multi_roles,
        )
        is_sat_apex, _ = check_contractual_role_satisfaction(
            entity_record=rec_apex,
            required_entity_name="Apex Electrical LLC",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_AI_04A",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            is_requirement_validated=True,
        )
        self.assertTrue(is_sat_apex)

        # Acme Corp evaluated as Additional Insured -> Fails closed (is Named Insured)
        rec_acme = reconcile_entity_identities(
            primary_entity_name="Acme Corp",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Acme Corp",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            evidence_chain=(ref_multi_roles, self.ref_coi_default),
            referenced_role_evidence=ref_multi_roles,
        )
        is_sat_acme, _ = check_contractual_role_satisfaction(
            entity_record=rec_acme,
            required_entity_name="Acme Corp",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_AI_04B",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            is_requirement_validated=True,
        )
        self.assertFalse(is_sat_acme)

    def test_wp1_role_05_role_keyword_without_entity_association(self):
        """5. Role keyword present without an entity association fails closed."""
        ref_header_only = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Policy Endorsement Form CG 20 10: Additional Insured - Owners, Lessees or Contractors",
        )
        ref_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Apex Electrical LLC",
        )
        rec = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            evidence_chain=(ref_coi, ref_header_only),
            referenced_role_evidence=ref_header_only,
        )
        is_sat, disc = check_contractual_role_satisfaction(
            entity_record=rec,
            required_entity_name="Apex Electrical LLC",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_AI_05",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            is_requirement_validated=True,
        )
        self.assertFalse(is_sat)

    def test_wp1_role_06_legitimate_entity_name_variation(self):
        """6. Legitimate entity-name variation where role association remains clear satisfies requirement."""
        ref_variation = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Apex Electrical, Inc. is designated as an Additional Insured under this policy",
        )
        ref_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Apex Electrical LLC",
        )
        rec = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            evidence_chain=(ref_coi, ref_variation),
            referenced_role_evidence=ref_variation,
        )
        is_sat, disc = check_contractual_role_satisfaction(
            entity_record=rec,
            required_entity_name="Apex Electrical LLC",
            required_role=EntityRole.ADDITIONAL_INSURED,
            contract_requirement_id="REQ_AI_06",
            coverage_type=CoverageType.GENERAL_LIABILITY,
            is_requirement_validated=True,
        )
        self.assertTrue(is_sat)
        self.assertIsNone(disc)

    def test_wp1_role_07_role_attribution_colon_and_whitespace(self):
        """7. Role attribution functions correctly with both colon and ordinary whitespace separators."""
        ref_colon = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Additional Insured: Apex Electrical LLC",
        )
        ref_ws = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Additional Insured   Apex Electrical LLC",
        )
        rec_colon = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.EXACT,
            evidence_chain=(self.ref_coi_default, ref_colon),
            is_confirmed_match=True,
        )
        rec_ws = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.EXACT,
            evidence_chain=(self.ref_coi_default, ref_ws),
            is_confirmed_match=True,
        )
        self.assertTrue(_has_role_evidence(EntityRole.ADDITIONAL_INSURED, "doc_end", rec_colon, ref_colon))
        self.assertTrue(_has_role_evidence(EntityRole.ADDITIONAL_INSURED, "doc_end", rec_ws, ref_ws))

    def test_wp1_role_08_conjoined_entities_additional_insureds_ambiguous(self):
        """8. Two entities conjoined in attribution clause are not uniquely attributable and fail closed."""
        ref_conjoined = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Apex Electrical LLC and Zenith Plumbing Inc are Additional Insureds.",
        )
        rec_apex = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.EXACT,
            evidence_chain=(self.ref_coi_default, ref_conjoined),
            is_confirmed_match=True,
        )
        rec_zenith = EntityReconciliationRecord(
            primary_entity_name="Zenith Plumbing Inc",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Zenith Plumbing Inc",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.EXACT,
            evidence_chain=(self.ref_coi_default, ref_conjoined),
            is_confirmed_match=True,
        )
        self.assertFalse(_has_role_evidence(EntityRole.ADDITIONAL_INSURED, "doc_end", rec_apex, ref_conjoined))
        self.assertFalse(_has_role_evidence(EntityRole.ADDITIONAL_INSURED, "doc_end", rec_zenith, ref_conjoined))

    def test_wp1_role_09_two_entities_before_role_only_one_designated(self):
        """9. Two entities appear before role phrase, but only one is explicitly designated as receiving role."""
        ref_intro = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Regarding contractor Acme Corp, Apex Electrical LLC is designated as an Additional Insured.",
        )
        rec_acme = EntityReconciliationRecord(
            primary_entity_name="Acme Corp",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Acme Corp",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.EXACT,
            evidence_chain=(self.ref_coi_default, ref_intro),
            is_confirmed_match=True,
        )
        rec_apex = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.EXACT,
            evidence_chain=(self.ref_coi_default, ref_intro),
            is_confirmed_match=True,
        )
        self.assertFalse(_has_role_evidence(EntityRole.ADDITIONAL_INSURED, "doc_end", rec_acme, ref_intro))
        self.assertTrue(_has_role_evidence(EntityRole.ADDITIONAL_INSURED, "doc_end", rec_apex, ref_intro))

    def test_wp1_role_10_role_before_entity_with_conjoined_entities_rejected(self):
        """10. Role followed by conjoined multiple entities is rejected for automatic attribution."""
        ref_conjoined_header = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Additional Insured: Apex Electrical LLC and Zenith Plumbing Inc",
        )
        rec_apex = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.EXACT,
            evidence_chain=(self.ref_coi_default, ref_conjoined_header),
            is_confirmed_match=True,
        )
        rec_zenith = EntityReconciliationRecord(
            primary_entity_name="Zenith Plumbing Inc",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Zenith Plumbing Inc",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.EXACT,
            evidence_chain=(self.ref_coi_default, ref_conjoined_header),
            is_confirmed_match=True,
        )
        self.assertFalse(_has_role_evidence(EntityRole.ADDITIONAL_INSURED, "doc_end", rec_apex, ref_conjoined_header))
        self.assertFalse(_has_role_evidence(EntityRole.ADDITIONAL_INSURED, "doc_end", rec_zenith, ref_conjoined_header))

    # =========================================================================
    # 3. Entity Mismatch Semantic Validation (10 tests)
    # =========================================================================

    def test_wp1_mismatch_01_correct_complete_entity_evidence(self):
        """1. Correct complete entity evidence in both documents confirms mismatch without review."""
        ref_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Apex Electrical LLC, 100 Main Street",
        )
        ref_end = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Named Insured: Zenith Plumbing Inc, 200 Oak Avenue",
        )
        rec = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Zenith Plumbing Inc",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            applicable_contract_requirement_id="REQ_MISMATCH_01",
            evidence_chain=(ref_coi, ref_end),
        )
        self.assertEqual(rec.classification, EntityMatchClassification.CONFIRMED_MISMATCH)
        self.assertFalse(rec.is_confirmed_match)
        self.assertFalse(rec.requires_human_review)
        self.assertEqual(rec.review_reason_code, "AFFIRMATIVE_CONTRACTUAL_ENTITY_MISMATCH")

    def test_wp1_mismatch_02_same_first_token_different_entities(self):
        """2. Same first token but different entities are distinguished and not falsely equated."""
        ref_apex_elec = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Apex Electrical LLC",
        )
        ref_apex_plumb = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Named Insured: Apex Plumbing Corp",
        )
        rec = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Plumbing Corp",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            applicable_contract_requirement_id="REQ_MISMATCH_02",
            evidence_chain=(ref_apex_elec, ref_apex_plumb),
        )
        self.assertEqual(rec.classification, EntityMatchClassification.CONFIRMED_MISMATCH)
        self.assertFalse(rec.is_confirmed_match)
        self.assertFalse(rec.requires_human_review)

    def test_wp1_mismatch_03_shared_generic_words_different_entities(self):
        """3. Shared generic words but different legal entities are properly distinguished."""
        ref_nat_fire = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: National Fire Protection Inc",
        )
        ref_nat_water = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Named Insured: National Water Protection Inc",
        )
        rec = reconcile_entity_identities(
            primary_entity_name="National Fire Protection Inc",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="National Water Protection Inc",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            applicable_contract_requirement_id="REQ_MISMATCH_03",
            evidence_chain=(ref_nat_fire, ref_nat_water),
        )
        self.assertEqual(rec.classification, EntityMatchClassification.CONFIRMED_MISMATCH)
        self.assertFalse(rec.is_confirmed_match)
        self.assertFalse(rec.requires_human_review)

    def test_wp1_mismatch_04_punctuation_and_legal_suffix_variations(self):
        """4. Punctuation normalizes to FORMATTING_NORMALIZED while legal suffix mismatch requires review."""
        ref_coi_comma = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Apex Electrical, LLC",
        )
        ref_end_no_comma = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Apex Electrical LLC",
        )
        rec_norm = reconcile_entity_identities(
            primary_entity_name="Apex Electrical, LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            evidence_chain=(ref_coi_comma, ref_end_no_comma),
        )
        self.assertEqual(rec_norm.classification, EntityMatchClassification.FORMATTING_NORMALIZED)
        self.assertTrue(rec_norm.is_confirmed_match)
        self.assertFalse(rec_norm.requires_human_review)

        ref_end_inc = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Apex Electrical Inc",
        )
        rec_diff_suffix = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical Inc",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            evidence_chain=(ref_coi_comma, ref_end_inc),
        )
        self.assertEqual(rec_diff_suffix.classification, EntityMatchClassification.LEGAL_SUFFIX_DISCREPANCY)
        self.assertFalse(rec_diff_suffix.is_confirmed_match)
        self.assertTrue(rec_diff_suffix.requires_human_review)

    def test_wp1_mismatch_05_dba_relationship_explicit_evidence(self):
        """5. DBA relationship with explicit affirmative evidence confirms CONFIRMED_DBA without review."""
        ref_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Apex Electrical LLC",
        )
        ref_end = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Named Insured: Apex Power Services",
        )
        ref_dba = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Apex Electrical LLC dba Apex Power Services",
        )
        rec = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Power Services",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            dba_evidence=ref_dba,
            evidence_chain=(ref_coi, ref_end, ref_dba),
        )
        self.assertEqual(rec.classification, EntityMatchClassification.CONFIRMED_DBA)
        self.assertTrue(rec.is_confirmed_match)
        self.assertFalse(rec.requires_human_review)

    def test_wp1_mismatch_06_dba_substring_in_unrelated_name(self):
        """6. DBA substring appearing inside an unrelated entity name does not falsely trigger DBA ambiguity."""
        ref_grandbay = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Grandbay Electrical LLC",
        )
        ref_zenith = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Named Insured: Zenith Plumbing Inc",
        )
        rec = reconcile_entity_identities(
            primary_entity_name="Grandbay Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Zenith Plumbing Inc",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            applicable_contract_requirement_id="REQ_MISMATCH_06",
            evidence_chain=(ref_grandbay, ref_zenith),
        )
        self.assertNotEqual(rec.classification, EntityMatchClassification.AMBIGUOUS_IDENTITY)
        self.assertEqual(rec.classification, EntityMatchClassification.CONFIRMED_MISMATCH)

    def test_wp1_mismatch_07_irrelevant_evidence_both_documents(self):
        """7. Irrelevant evidence from both documents demotes to AMBIGUOUS_IDENTITY with human review."""
        ref_coi_irrel = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Payment terms are net 30 days from date of receipt",
        )
        ref_end_irrel = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Safety guidelines and worker protection procedures apply",
        )
        rec = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Zenith Plumbing Inc",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            applicable_contract_requirement_id="REQ_MISMATCH_07",
            evidence_chain=(ref_coi_irrel, ref_end_irrel),
        )
        self.assertEqual(rec.classification, EntityMatchClassification.AMBIGUOUS_IDENTITY)
        self.assertFalse(rec.is_confirmed_match)
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.review_reason_code, "INSUFFICIENT_MISMATCH_EVIDENCE")

    def test_wp1_mismatch_08_schema_validation_rejects_unsupported_mismatch(self):
        """8. Schema validation rejects CONFIRMED_MISMATCH record when evidence does not semantically support both entity names."""
        ref_coi_valid = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Apex Electrical LLC",
        )
        ref_end_wrong = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Coverage endorsement terms and conditions",
        )
        with self.assertRaises(ValueError) as ctx:
            EntityReconciliationRecord(
                primary_entity_name="Apex Electrical LLC",
                primary_entity_role=EntityRole.NAMED_INSURED,
                primary_document_id="doc_coi",
                referenced_entity_name="Zenith Plumbing Inc",
                referenced_entity_role=EntityRole.NAMED_INSURED,
                referenced_document_id="doc_end",
                classification=EntityMatchClassification.CONFIRMED_MISMATCH,
                applicable_contract_requirement_id="REQ_MISMATCH_08",
                evidence_chain=(ref_coi_valid, ref_end_wrong),
                is_confirmed_match=False,
                requires_human_review=False,
            )
        self.assertIn("CONFIRMED_MISMATCH requires affirmative evidence semantically supporting both entity names", str(ctx.exception))

    def test_wp1_mismatch_09_evidence_contains_dba_indicator_prevents_confirmed_mismatch(self):
        """9. Potential DBA or trade-name relationship in evidence prevents CONFIRMED_MISMATCH and requires human review."""
        ref_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Apex Electrical LLC doing business as Zenith Plumbing",
        )
        ref_end = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Named Insured: Zenith Plumbing Inc",
        )
        rec = reconcile_entity_identities(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Zenith Plumbing Inc",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            applicable_contract_requirement_id="REQ_MISMATCH_09",
            evidence_chain=(ref_coi, ref_end),
        )
        self.assertEqual(rec.classification, EntityMatchClassification.AMBIGUOUS_IDENTITY)
        self.assertFalse(rec.is_confirmed_match)
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.review_reason_code, "POTENTIAL_DBA_OR_TRADE_NAME_RELATIONSHIP")

    def test_wp1_mismatch_10_schema_validator_rejects_mismatch_with_dba_evidence(self):
        """10. Schema validator rejects CONFIRMED_MISMATCH when evidence contains DBA or trade-name indicators."""
        ref_coi_dba = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Apex Electrical LLC dba Zenith Plumbing",
        )
        ref_end = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Named Insured: Zenith Plumbing Inc",
        )
        with self.assertRaises(ValueError) as ctx:
            EntityReconciliationRecord(
                primary_entity_name="Apex Electrical LLC",
                primary_entity_role=EntityRole.NAMED_INSURED,
                primary_document_id="doc_coi",
                referenced_entity_name="Zenith Plumbing Inc",
                referenced_entity_role=EntityRole.NAMED_INSURED,
                referenced_document_id="doc_end",
                classification=EntityMatchClassification.CONFIRMED_MISMATCH,
                applicable_contract_requirement_id="REQ_MISMATCH_10",
                evidence_chain=(ref_coi_dba, ref_end),
                is_confirmed_match=False,
                requires_human_review=False,
            )
        self.assertIn("potential trade-name relationships require human review and cannot be confirmed mismatches", str(ctx.exception))

    def test_wp1_mismatch_11_unestablished_legal_identity_requires_review(self):
        """11. Different names without explicit DBA wording but lacking established legal identity require human review."""
        ref_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Apex Electrical Services",
        )
        ref_end = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Named Insured: Apex Power Solutions",
        )
        rec = reconcile_entity_identities(
            primary_entity_name="Apex Electrical Services",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Power Solutions",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            applicable_contract_requirement_id="REQ_MISMATCH_11",
            evidence_chain=(ref_coi, ref_end),
        )
        self.assertEqual(rec.classification, EntityMatchClassification.AMBIGUOUS_IDENTITY)
        self.assertFalse(rec.is_confirmed_match)
        self.assertTrue(rec.requires_human_review)
        self.assertEqual(rec.review_reason_code, "UNESTABLISHED_LEGAL_IDENTITY")

    def test_wp1_mismatch_12_schema_validator_rejects_mismatch_without_legal_suffixes(self):
        """12. Schema validator rejects CONFIRMED_MISMATCH when entity names lack recognized legal corporate suffixes."""
        ref_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Apex Electrical Services",
        )
        ref_end = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Named Insured: Apex Power Solutions",
        )
        with self.assertRaises(ValueError) as ctx:
            EntityReconciliationRecord(
                primary_entity_name="Apex Electrical Services",
                primary_entity_role=EntityRole.NAMED_INSURED,
                primary_document_id="doc_coi",
                referenced_entity_name="Apex Power Solutions",
                referenced_entity_role=EntityRole.NAMED_INSURED,
                referenced_document_id="doc_end",
                classification=EntityMatchClassification.CONFIRMED_MISMATCH,
                applicable_contract_requirement_id="REQ_MISMATCH_12",
                evidence_chain=(ref_coi, ref_end),
                is_confirmed_match=False,
                requires_human_review=False,
            )
        self.assertIn("both entities must have established legal entity suffixes", str(ctx.exception))

    def test_wp1_regex_behavior_proves_no_literal_asterisk_or_escaped_group_required(self):
        """13. Direct regex behavior test: verifies standard texts match without requiring literal asterisks or escaped groups."""
        # A. Numeric dates under established convention without literal '(' or '?:'
        ref_mm_conv = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Date format: MM/DD/YYYY. Policy Effective Date: 03/05/2025 Expiration Date: 03/05/2026",
        )
        # Would fail if pattern was \(?:{year}|{yy}) because text contains "03/05/2025", not "\(?:2025"
        self.assertTrue(_evidence_supports_date(date(2025, 3, 5), (ref_mm_conv,), date_field="effective", doc_id="doc_coi"))

        # B. Date ranges with 'to', 'through', and '-' without literal asterisks
        ref_to = EvidenceReference(document_id="doc_coi", page_number=1, source_text="Coverage: 01/01/2025 to 01/01/2026")
        ref_thru = EvidenceReference(document_id="doc_coi", page_number=1, source_text="Coverage: 01/01/2025 through 01/01/2026")
        ref_hyphen = EvidenceReference(document_id="doc_coi", page_number=1, source_text="Coverage: 01/01/2025 - 01/01/2026")
        # Would fail if pattern was \s\* because text contains spaces, not literal asterisks "*"
        for r in (ref_to, ref_thru, ref_hyphen):
            self.assertTrue(_evidence_supports_date(date(2025, 1, 1), (r,), date_field="effective", doc_id="doc_coi"))
            self.assertTrue(_evidence_supports_date(date(2026, 1, 1), (r,), date_field="expiration", doc_id="doc_coi"))

        # C. Role-before-entity with colon and whitespace without literal asterisks
        ref_colon = EvidenceReference(document_id="doc_end", page_number=1, source_text="Additional Insured: Apex Electrical LLC")
        ref_spaces = EvidenceReference(document_id="doc_end", page_number=1, source_text="Additional Insured    Apex Electrical LLC")
        rec = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.ADDITIONAL_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.EXACT,
            evidence_chain=(self.ref_coi_default, ref_colon),
            is_confirmed_match=True,
        )
        # Would fail if pattern was \s\* because text has ordinary whitespace/colon, not literal asterisks
        self.assertTrue(_has_role_evidence(EntityRole.ADDITIONAL_INSURED, "doc_end", rec, ref_colon))
        self.assertTrue(_has_role_evidence(EntityRole.ADDITIONAL_INSURED, "doc_end", rec, ref_spaces))


# =============================================================================
# Milestone 3.5D — WP2: Document Relationship Target Linkage Adversarial Suite
# =============================================================================

class TestMilestone35DDocumentTargetLinkage(unittest.TestCase):
    """Targeted adversarial suite for WP2: Document Relationship Target Linkage."""

    def setUp(self):
        self.doc_coi_manifest = DocumentManifestEntry(
            document_id="doc_coi",
            file_name="certificate.pdf",
            sha256_hash="a" * 64,
            page_count=2,
            detected_document_type=DocumentType.COI,
        )
        self.doc_end_manifest = DocumentManifestEntry(
            document_id="doc_end",
            file_name="endorsement.pdf",
            sha256_hash="b" * 64,
            page_count=1,
            detected_document_type=DocumentType.ENDORSEMENT,
        )

    def test_wp2_01_explicit_target_policy_number_identifies_target(self):
        """1. Explicit target policy number in source text correctly identifies target."""
        ref_src = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement modifies coverage under policy POL-12345",
        )
        ref_tgt = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy POL-12345 Commercial General Liability Coverage",
        )
        edge = classify_document_relationship(
            edge_id="e_wp2_01",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.AMENDS_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_src, ref_tgt),
        )
        self.assertTrue(edge.is_confirmed)
        self.assertFalse(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.CONFIRMED_EXACT)

    def test_wp2_02_explicit_target_document_form_identifier_identifies_target(self):
        """2. Explicit target document/form identifier correctly identifies the target."""
        ref_src = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement replaces prior form CG 20 10 11 85",
        )
        ref_tgt = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Attached form CG 20 10 11 85 Additional Insured Endorsement",
        )
        edge = classify_document_relationship(
            edge_id="e_wp2_02",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.REPLACES_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_src, ref_tgt),
        )
        self.assertTrue(edge.is_confirmed)
        self.assertFalse(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.CONFIRMED_EXACT)

    def test_wp2_03_generic_amends_policy_wording_without_target_identity_unconfirmed(self):
        """3. Generic 'amends the policy' wording without target identity does not confirm a target."""
        ref_src = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement modifies coverage provided under the policy",
        )
        ref_tgt = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy GL-99999 Commercial General Liability Coverage",
        )
        edge = classify_document_relationship(
            edge_id="e_wp2_03",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.AMENDS_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_src, ref_tgt),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.AMBIGUOUS)
        self.assertEqual(edge.review_reason_code, "GENERIC_RELATIONSHIP_WITHOUT_TARGET_IDENTITY")

    def test_wp2_04_source_referring_to_different_policy_number_conflicts(self):
        """4. A source document referring to a different policy number does not link to the available policy."""
        ref_src = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement modifies coverage under policy POL-99999",
        )
        ref_tgt = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy POL-12345 Commercial General Liability Coverage",
        )
        edge = classify_document_relationship(
            edge_id="e_wp2_04",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.AMENDS_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_src, ref_tgt),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.CONFLICTING)
        self.assertEqual(edge.review_reason_code, "TARGET_IDENTIFIER_MISMATCH")

    def test_wp2_05_multiple_possible_target_documents_produce_ambiguity(self):
        """5. Multiple possible target documents produce ambiguity when source does not resolve target uniquely."""
        ref_src = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement modifies policy POL-12345",
        )
        ref_tgt = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy POL-12345 Commercial General Liability Coverage",
        )
        edge = classify_document_relationship(
            edge_id="e_wp2_05",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.AMENDS_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_src, ref_tgt),
            candidate_target_document_ids=("doc_coi", "doc_coi_2"),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.AMBIGUOUS)
        self.assertEqual(edge.review_reason_code, "AMBIGUOUS_MULTIPLE_TARGET_DOCUMENTS")

    def test_wp2_06_missing_target_document_produces_unresolved_relationship(self):
        """6. Missing target document produces an unresolved relationship requiring human review."""
        ref_src = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement modifies coverage under policy POL-12345",
        )
        # Case A: target_document_id is None -> UNLINKED_ORPHAN
        edge_none = classify_document_relationship(
            edge_id="e_wp2_06_none",
            source_document_id="doc_end",
            target_document_id=None,
            relationship_type=DocumentRelationshipType.AMENDS_DOCUMENT,
            classification=EdgeClassification.AMBIGUOUS,
            evidence_references=(ref_src,),
        )
        self.assertFalse(edge_none.is_confirmed)
        self.assertTrue(edge_none.requires_human_review)
        self.assertEqual(edge_none.classification, EdgeClassification.UNLINKED_ORPHAN)
        self.assertEqual(edge_none.review_reason_code, "MISSING_TARGET_DOCUMENT")

        # Case B: target_document_id specified but missing from evidence -> AMBIGUOUS
        edge_missing = classify_document_relationship(
            edge_id="e_wp2_06_missing",
            source_document_id="doc_end",
            target_document_id="doc_coi_missing",
            relationship_type=DocumentRelationshipType.AMENDS_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_src,),  # target doc not in evidence
        )
        self.assertFalse(edge_missing.is_confirmed)
        self.assertTrue(edge_missing.requires_human_review)
        self.assertEqual(edge_missing.classification, EdgeClassification.AMBIGUOUS)
        self.assertEqual(edge_missing.review_reason_code, "MISSING_TARGET_DOCUMENT")

    def test_wp2_07_conflicting_target_identifiers_require_human_review(self):
        """7. Conflicting target identifiers (e.g. mismatched form numbers) require human review."""
        ref_src = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement replaces prior form CG 20 10 11 85",
        )
        ref_tgt = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Applicable form CG 20 37 04 13 Completed Operations",
        )
        edge = classify_document_relationship(
            edge_id="e_wp2_07",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.REPLACES_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_src, ref_tgt),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.CONFLICTING)
        self.assertEqual(edge.review_reason_code, "CONFLICTING_TARGET_IDENTIFIERS")

    def test_wp2_08_valid_replaces_relationship_remains_correctly_classified(self):
        """8. A valid REPLACES relationship with verified target identifier remains correctly confirmed."""
        ref_src = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement cancels and replaces prior form CG 20 10 11 85 under policy GL-55555",
        )
        ref_tgt = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy GL-55555 Form CG 20 10 11 85 Coverage Schedule",
        )
        edge = classify_document_relationship(
            edge_id="e_wp2_08",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.REPLACES_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_src, ref_tgt),
        )
        self.assertTrue(edge.is_confirmed)
        self.assertFalse(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.CONFIRMED_EXACT)

    def test_wp2_09_valid_amends_relationship_remains_correctly_classified(self):
        """9. A valid AMENDS relationship with verified target identifier remains correctly confirmed."""
        ref_src = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement amends the terms of Policy GL-55555",
        )
        ref_tgt = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy GL-55555 Commercial General Liability Coverage",
        )
        edge = classify_document_relationship(
            edge_id="e_wp2_09",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.AMENDS_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_src, ref_tgt),
        )
        self.assertTrue(edge.is_confirmed)
        self.assertFalse(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.CONFIRMED_EXACT)

    def test_wp2_10_evidence_from_unrelated_document_cannot_establish_target(self):
        """10. Evidence from an unrelated document cannot establish the target relationship."""
        ref_unrelated = EvidenceReference(
            document_id="doc_invoice",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Invoice for Policy GL-55555 replaces prior schedule",
        )
        ref_src = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement replaces prior form CG 20 10 11 85 for policy GL-55555",
        )
        ref_tgt = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy GL-55555 Commercial General Liability Coverage",
        )
        edge = classify_document_relationship(
            edge_id="e_wp2_10",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.REPLACES_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_src, ref_tgt, ref_unrelated),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.AMBIGUOUS)
        self.assertEqual(edge.review_reason_code, "UNSUPPORTED_RELATIONSHIP_EVIDENCE")

    def test_wp2_11_weak_name_date_similarity_alone_cannot_establish_target(self):
        """11. Weak name/date similarity alone without reliable document identifiers cannot establish unique target."""
        ref_src = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement amends the policy for Apex Electrical LLC effective 01/01/2025",
        )
        ref_tgt = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Named Insured: Apex Electrical LLC, Policy Period: 01/01/2025 to 01/01/2026",
        )
        edge = classify_document_relationship(
            edge_id="e_wp2_11",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.AMENDS_DOCUMENT,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_src, ref_tgt),
        )
        self.assertFalse(edge.is_confirmed)
        self.assertTrue(edge.requires_human_review)
        self.assertEqual(edge.classification, EdgeClassification.AMBIGUOUS)
        self.assertEqual(edge.review_reason_code, "GENERIC_RELATIONSHIP_WITHOUT_TARGET_IDENTITY")

    def test_wp2_12_existing_supported_relationships_backward_compatible(self):
        """12. Existing supported relationship behaviors (SAME_POLICY, ENDORSEMENT_TO_POLICY, etc.) remain backward compatible."""
        ref_end_att = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement forms part of Policy GL-12345",
        )
        ref_coi = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy GL-12345 Commercial General Liability Coverage",
        )
        edge_end = classify_document_relationship(
            edge_id="e_wp2_12_end",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.ENDORSEMENT_TO_POLICY,
            classification=EdgeClassification.CONFIRMED_EXACT,
            evidence_references=(ref_end_att, ref_coi),
        )
        self.assertTrue(edge_end.is_confirmed)
        self.assertFalse(edge_end.requires_human_review)

        edge_orphan = classify_document_relationship(
            edge_id="e_wp2_12_orphan",
            source_document_id="doc_end",
            target_document_id=None,
            relationship_type=DocumentRelationshipType.ORPHAN_DOCUMENT,
            classification=EdgeClassification.UNLINKED_ORPHAN,
        )
        self.assertFalse(edge_orphan.is_confirmed)
        self.assertTrue(edge_orphan.requires_human_review)
        self.assertEqual(edge_orphan.classification, EdgeClassification.UNLINKED_ORPHAN)

    def test_wp2_13_schema_validator_rejects_confirmed_edge_with_generic_wording(self):
        """13. Schema validator rejects confirmed REPLACES or AMENDS edge with purely generic wording."""
        ref_src_generic = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="This endorsement modifies coverage provided under the policy",
        )
        ref_tgt = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Policy GL-99999 Commercial General Liability Coverage",
        )
        with self.assertRaises(ValueError) as ctx:
            DocumentRelationshipEdge(
                edge_id="e_wp2_schema_generic",
                source_document_id="doc_end",
                target_document_id="doc_coi",
                relationship_type=DocumentRelationshipType.AMENDS_DOCUMENT,
                classification=EdgeClassification.CONFIRMED_EXACT,
                is_confirmed=True,
                requires_human_review=False,
                evidence_references=(ref_src_generic, ref_tgt),
            )
        self.assertIn("requires affirmative target identity linkage", str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
