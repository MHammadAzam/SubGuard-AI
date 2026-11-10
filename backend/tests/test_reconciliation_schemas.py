"""Unit tests for Milestone 3.5A cross-document reconciliation schemas.

Validates:
1. Deep immutability and input shielding across all models and EvidenceReference.
2. Legal-entity identity separation from document-role compatibility.
3. Strict 5-tier policy match classification and safe normalization.
4. Confirmed relationship edge evidence requirements and orphan semantics.
5. Strict date algebra, nullable states, and contradiction rejection.
6. DocumentDiscrepancy contractual violation validation.
7. CrossDocumentReconciliationResult referential integrity and summary derivation.
8. Every negative-test scenario identified in the NEG-01 to NEG-12 matrix.
"""
from __future__ import annotations

import unittest
from datetime import date
from pydantic import ValidationError

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
    MissingEvidenceProvenance,
    PolicyMatchClassification,
    PolicyReconciliationRecord,
    is_role_compatible,
    normalize_policy_identifier,
    validate_evidence_reference,
)


class TestReconciliationSchemas(unittest.TestCase):
    """Test suite for Milestone 3.5A reconciliation models and invariants."""

    def setUp(self):
        self.doc1_manifest = DocumentManifestEntry(
            document_id="doc_coi",
            file_name="coi.pdf",
            sha256_hash="a" * 64,
            page_count=2,
            detected_document_type=DocumentType.COI,
        )
        self.doc2_manifest = DocumentManifestEntry(
            document_id="doc_end",
            file_name="end.pdf",
            sha256_hash="b" * 64,
            page_count=3,
            detected_document_type=DocumentType.ENDORSEMENT,
        )
        self.ref1 = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Apex Electrical LLC Policy GL-12345 Commercial Liability Coverage",
            bounding_box=BoundingBox(x=0.1, y=0.1, width=0.3, height=0.05),
        )
        self.ref2 = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Apex Electrical LLC Policy GL-12345 Endorsement Form Coverage",
            bounding_box=BoundingBox(x=0.1, y=0.1, width=0.3, height=0.05),
        )

    # =========================================================================
    # 1. Deep Immutability & Input Shielding Tests
    # =========================================================================

    def test_evidence_reference_immutability(self):
        """EvidenceReference and BoundingBox must be strictly immutable."""
        with self.assertRaises((ValidationError, TypeError)):
            self.ref1.document_id = "other_doc"  # type: ignore

        if self.ref1.bounding_box:
            with self.assertRaises((ValidationError, TypeError)):
                self.ref1.bounding_box.x = 0.5  # type: ignore

    def test_reconciliation_record_immutability(self):
        """Reconciliation models must be frozen and reject attribute reassignment."""
        record = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.EXACT,
            evidence_chain=(self.ref1, self.ref2),
            is_confirmed_match=True,
            requires_human_review=False,
        )
        with self.assertRaises((ValidationError, TypeError)):
            record.primary_entity_name = "Changed Name"  # type: ignore

        # Attempt to mutate tuple attribute
        with self.assertRaises(AttributeError):
            record.evidence_chain.append(self.ref1)  # type: ignore

    def test_input_shielding_against_external_list_mutation(self):
        """Mutating an external list passed to a model must not affect the model's internal tuple."""
        external_list = [self.ref1, self.ref2]
        record = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.EXACT,
            evidence_chain=external_list,  # type: ignore
            is_confirmed_match=True,
            requires_human_review=False,
        )
        # Ensure it is stored as a tuple
        self.assertIsInstance(record.evidence_chain, tuple)
        self.assertEqual(len(record.evidence_chain), 2)

        # Mutate the external caller-owned list
        external_list.append(self.ref1)
        self.assertEqual(len(external_list), 3)

        # Verify internal tuple in record was shielded and remains length 2
        self.assertEqual(len(record.evidence_chain), 2)

    # =========================================================================
    # 2. Role Compatibility & Entity Matching Invariants
    # =========================================================================

    def test_role_compatibility_definitions(self):
        """Verify role compatibility semantics."""
        # Compatible pairs
        self.assertTrue(is_role_compatible(EntityRole.NAMED_INSURED, EntityRole.NAMED_INSURED))
        self.assertTrue(is_role_compatible(EntityRole.CERTIFICATE_HOLDER, EntityRole.ADDITIONAL_INSURED))
        self.assertTrue(is_role_compatible(EntityRole.ADDITIONAL_INSURED, EntityRole.CERTIFICATE_HOLDER))

        # Incompatible pairs (must not be treated as interchangeable)
        self.assertFalse(is_role_compatible(EntityRole.NAMED_INSURED, EntityRole.CERTIFICATE_HOLDER))
        self.assertFalse(is_role_compatible(EntityRole.PRODUCER, EntityRole.NAMED_INSURED))
        self.assertFalse(is_role_compatible(EntityRole.PRODUCER, EntityRole.ADDITIONAL_INSURED))

    def test_identity_matching_independent_of_document_roles(self):
        """Legal identity matching is independent of document roles (C-01, C-02)."""
        record = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.CERTIFICATE_HOLDER,  # Different document roles!
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.EXACT,
            evidence_chain=(self.ref1, self.ref2),
            is_confirmed_match=True,
            requires_human_review=False,
        )
        self.assertEqual(record.classification, EntityMatchClassification.EXACT)
        self.assertTrue(record.is_confirmed_match)

    def test_confirmed_dba_requires_explicit_evidence(self):
        """NEG-05: CONFIRMED_DBA requires non-null dba_evidence included in evidence_chain."""
        # Missing dba_evidence
        with self.assertRaises(ValueError) as ctx:
            EntityReconciliationRecord(
                primary_entity_name="Apex Contractors Inc",
                primary_entity_role=EntityRole.NAMED_INSURED,
                primary_document_id="doc_coi",
                referenced_entity_name="Apex Contractors Inc DBA Apex Roofing",
                referenced_entity_role=EntityRole.NAMED_INSURED,
                referenced_document_id="doc_end",
                classification=EntityMatchClassification.CONFIRMED_DBA,
                dba_evidence=None,
                evidence_chain=(self.ref1, self.ref2),
                is_confirmed_match=True,
                requires_human_review=False,
            )
        self.assertIn("CONFIRMED_DBA requires explicit dba_evidence", str(ctx.exception))

        # Valid CONFIRMED_DBA
        dba_ref = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="DBA Apex Roofing",
        )
        rec = EntityReconciliationRecord(
            primary_entity_name="Apex Contractors Inc",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Contractors Inc DBA Apex Roofing",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.CONFIRMED_DBA,
            dba_evidence=dba_ref,
            evidence_chain=(self.ref1, self.ref2, dba_ref),
            is_confirmed_match=True,
            requires_human_review=False,
        )
        self.assertTrue(rec.is_confirmed_match)

    def test_legal_suffix_discrepancy_requires_review(self):
        """LEGAL_SUFFIX_DISCREPANCY must require review and cannot be marked confirmed."""
        rec = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Apex Electrical Inc",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.LEGAL_SUFFIX_DISCREPANCY,
            evidence_chain=(self.ref1, self.ref2),
            is_confirmed_match=False,
            requires_human_review=True,
            review_reason_code="CORPORATE_SUFFIX_DISCREPANCY",
        )
        self.assertTrue(rec.requires_human_review)
        self.assertFalse(rec.is_confirmed_match)

        # Trying to confirm it must raise ValueError
        with self.assertRaises(ValueError):
            EntityReconciliationRecord(
                primary_entity_name="Apex Electrical LLC",
                primary_entity_role=EntityRole.NAMED_INSURED,
                primary_document_id="doc_coi",
                referenced_entity_name="Apex Electrical Inc",
                referenced_entity_role=EntityRole.NAMED_INSURED,
                referenced_document_id="doc_end",
                classification=EntityMatchClassification.LEGAL_SUFFIX_DISCREPANCY,
                evidence_chain=(self.ref1, self.ref2),
                is_confirmed_match=True,  # Invalid!
                requires_human_review=False,
            )

    def test_confirmed_mismatch_invariants(self):
        """NEG-06: CONFIRMED_MISMATCH requires evidence, contractual requirement, and is_confirmed_match=False."""
        # Missing applicable_contract_requirement_id
        ref_lone = EvidenceReference(
            document_id="doc_end",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Lone Star Plumbing Corp Policy GL-12345 Endorsement Form Coverage",
        )
        with self.assertRaises(ValueError) as ctx:
            EntityReconciliationRecord(
                primary_entity_name="Apex Electrical LLC",
                primary_entity_role=EntityRole.NAMED_INSURED,
                primary_document_id="doc_coi",
                referenced_entity_name="Lone Star Plumbing Corp",
                referenced_entity_role=EntityRole.NAMED_INSURED,
                referenced_document_id="doc_end",
                classification=EntityMatchClassification.CONFIRMED_MISMATCH,
                applicable_contract_requirement_id=None,  # Missing!
                evidence_chain=(self.ref1, ref_lone),
                is_confirmed_match=False,
                requires_human_review=False,
            )
        self.assertIn("requires an applicable_contract_requirement_id", str(ctx.exception))

        # Valid CONFIRMED_MISMATCH
        rec = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_coi",
            referenced_entity_name="Lone Star Plumbing Corp",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.CONFIRMED_MISMATCH,
            applicable_contract_requirement_id="REQ_NAMED_INSURED",
            evidence_chain=(self.ref1, ref_lone),
            is_confirmed_match=False,
            requires_human_review=False,
            review_reason_code="ENDORSEMENT_ENTITY_MISMATCH",
        )
        self.assertFalse(rec.is_confirmed_match)
        self.assertEqual(rec.applicable_contract_requirement_id, "REQ_NAMED_INSURED")

    # =========================================================================
    # 3. Policy Identifier Reconciliation Invariants
    # =========================================================================

    def test_normalize_policy_identifier(self):
        """Algorithm must strip whitespace/delimiters and preserve alphanumeric renewal suffixes."""
        self.assertEqual(normalize_policy_identifier("GL-98765432"), "GL98765432")
        self.assertEqual(normalize_policy_identifier("GL 98765432"), "GL98765432")
        self.assertEqual(normalize_policy_identifier("GL/98765432-01"), "GL9876543201")
        self.assertEqual(normalize_policy_identifier(""), "")
        self.assertEqual(normalize_policy_identifier(None), "")

    def test_policy_exact_classification_loophole_rejection(self):
        """NEG-01: EXACT requires character-for-character identical strings."""
        with self.assertRaises(ValueError) as ctx:
            PolicyReconciliationRecord(
                coi_document_id="doc_coi",
                coi_policy_number="GL-100",
                coi_evidence=self.ref1,
                endorsement_document_id="doc_end",
                endorsement_policy_number="GL-200",  # Unrelated!
                classification=PolicyMatchClassification.EXACT,
                evidence_chain=(self.ref1, self.ref2),
                is_confirmed_match=True,
                requires_human_review=False,
            )
        self.assertIn("EXACT classification requires identical strings", str(ctx.exception))

    def test_policy_normalized_classification_renewal_suffix_rejection(self):
        """NEG-02: FORMATTING_NORMALIZED must not strip renewal suffixes (-01 vs -02)."""
        with self.assertRaises(ValueError) as ctx:
            PolicyReconciliationRecord(
                coi_document_id="doc_coi",
                coi_policy_number="GL-100-01",
                coi_evidence=self.ref1,
                endorsement_document_id="doc_end",
                endorsement_policy_number="GL-100-02",  # Renewal suffix differs!
                classification=PolicyMatchClassification.FORMATTING_NORMALIZED,
                evidence_chain=(self.ref1, self.ref2),
                is_confirmed_match=True,
                requires_human_review=False,
            )
        self.assertIn("FORMATTING_NORMALIZED requires equal normalized identifiers", str(ctx.exception))

    def test_policy_confirmed_match_requires_dual_citations(self):
        """Confirmed policy match requires distinct non-null evidence from both documents."""
        with self.assertRaises(ValueError) as ctx:
            PolicyReconciliationRecord(
                coi_document_id="doc_coi",
                coi_policy_number="GL-98765432",
                coi_evidence=self.ref1,
                endorsement_document_id="doc_end",
                endorsement_policy_number="GL-98765432",
                endorsement_evidence=None,  # Missing endorsement-side citation!
                classification=PolicyMatchClassification.EXACT,
                evidence_chain=(self.ref1,),
                is_confirmed_match=True,
                requires_human_review=False,
            )
        self.assertIn("Confirmed policy match requires separate non-null coi_evidence and endorsement_evidence", str(ctx.exception))

    # =========================================================================
    # 4. Relationship Edge Invariants & Orphan Semantics
    # =========================================================================

    def test_confirmed_edge_requires_dual_document_evidence(self):
        """NEG-03 & NEG-04: Confirmed edge requires evidence referencing both source and target documents."""
        # Empty evidence
        with self.assertRaises(ValueError):
            DocumentRelationshipEdge(
                edge_id="e1",
                source_document_id="doc_coi",
                target_document_id="doc_end",
                relationship_type=DocumentRelationshipType.SAME_POLICY,
                classification=EdgeClassification.CONFIRMED_EXACT,
                is_confirmed=True,
                requires_human_review=False,
                evidence_references=(),
            )

        # Single document evidence only
        with self.assertRaises(ValueError) as ctx:
            DocumentRelationshipEdge(
                edge_id="e1",
                source_document_id="doc_coi",
                target_document_id="doc_end",
                relationship_type=DocumentRelationshipType.SAME_POLICY,
                classification=EdgeClassification.CONFIRMED_EXACT,
                is_confirmed=True,
                requires_human_review=False,
                evidence_references=(self.ref1,),  # ref1 points only to doc_coi!
            )
        self.assertIn("requires evidence referencing both source_document_id 'doc_coi' and target_document_id 'doc_end'", str(ctx.exception))

        # Valid dual evidence
        edge = DocumentRelationshipEdge(
            edge_id="e1",
            source_document_id="doc_coi",
            target_document_id="doc_end",
            relationship_type=DocumentRelationshipType.SAME_POLICY,
            classification=EdgeClassification.CONFIRMED_EXACT,
            is_confirmed=True,
            requires_human_review=False,
            evidence_references=(self.ref1, self.ref2),
        )
        self.assertTrue(edge.is_confirmed)

    def test_orphan_document_edge_semantics(self):
        """Orphan document edge must have target_document_id=None and classification=UNLINKED_ORPHAN."""
        orphan_edge = DocumentRelationshipEdge(
            edge_id="e_orphan",
            source_document_id="doc_end",
            target_document_id=None,
            relationship_type=DocumentRelationshipType.ORPHAN_DOCUMENT,
            classification=EdgeClassification.UNLINKED_ORPHAN,
            is_confirmed=False,
            requires_human_review=True,
            evidence_references=(self.ref2,),
        )
        self.assertIsNone(orphan_edge.target_document_id)
        self.assertFalse(orphan_edge.is_confirmed)
        self.assertTrue(orphan_edge.requires_human_review)

        # Invalid orphan with target_document_id
        with self.assertRaises(ValueError):
            DocumentRelationshipEdge(
                edge_id="e_orphan_bad",
                source_document_id="doc_end",
                target_document_id="doc_coi",  # Cannot have target!
                relationship_type=DocumentRelationshipType.ORPHAN_DOCUMENT,
                classification=EdgeClassification.UNLINKED_ORPHAN,
                is_confirmed=False,
                requires_human_review=True,
            )

    # =========================================================================
    # 5. Date Algebra & Temporal Invariants
    # =========================================================================

    def test_date_active_state_requires_both_dates(self):
        """NEG-07: Active state True requires verified start and end dates."""
        with self.assertRaises(ValueError) as ctx:
            DateReconciliationRecord(
                policy_effective_date=date(2025, 1, 1),
                policy_expiration_date=None,  # Missing expiration!
                policy_date_evidence=(self.ref1,),
                evaluation_date=date(2025, 6, 15),
                is_policy_active_on_evaluation_date=True,
            )
        self.assertIn("cannot be True without verified start and end dates", str(ctx.exception))

    def test_date_inverted_range_rejection(self):
        """Effective date after expiration date must be rejected."""
        with self.assertRaises(ValueError) as ctx:
            DateReconciliationRecord(
                policy_effective_date=date(2025, 12, 31),
                policy_expiration_date=date(2025, 1, 1),  # Inverted!
                policy_date_evidence=(self.ref1,),
                evaluation_date=date(2025, 6, 15),
            )
        self.assertIn("Inverted policy dates", str(ctx.exception))

    def test_premature_expiration_invariants(self):
        """NEG-08: Premature expiration True requires endorsement expiration < policy expiration."""
        with self.assertRaises(ValueError) as ctx:
            DateReconciliationRecord(
                policy_effective_date=date(2025, 1, 1),
                policy_expiration_date=date(2025, 6, 1),
                policy_date_evidence=(self.ref1,),
                endorsement_effective_date=date(2025, 1, 1),
                endorsement_expiration_date=date(2025, 12, 31),  # Expires AFTER policy!
                endorsement_date_evidence=(self.ref2,),
                evaluation_date=date(2025, 3, 15),
                is_endorsement_active_on_evaluation_date=True,
                is_premature_expiration=True,  # Contradiction!
            )
        self.assertIn("cannot be True when endorsement expiration date >= policy expiration date", str(ctx.exception))

    # =========================================================================
    # 6. Discrepancy & Contractual Violation Invariants
    # =========================================================================

    def test_confirmed_contractual_violation_invariants(self):
        """CONFIRMED_CONTRACTUAL_VIOLATION requires requirement ID, entity/coverage context, and evidence."""
        # Missing applicable_requirement_id
        with self.assertRaises(ValueError) as ctx:
            DocumentDiscrepancy(
                discrepancy_id="d1",
                category=DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION,
                severity=Severity.CRITICAL,
                reason_code="LIMIT_BELOW_MINIMUM",
                description="General aggregate below required limit",
                source_document_id="doc_coi",
                applicable_requirement_id=None,  # Missing!
                applicable_coverage_type=CoverageType.GENERAL_LIABILITY,
                evidence_chain=(self.ref1,),
            )
        self.assertIn("strictly requires a non-empty applicable_requirement_id", str(ctx.exception))

        # Missing evidence_chain
        with self.assertRaises(ValueError) as ctx:
            DocumentDiscrepancy(
                discrepancy_id="d1",
                category=DiscrepancyCategory.CONFIRMED_CONTRACTUAL_VIOLATION,
                severity=Severity.CRITICAL,
                reason_code="LIMIT_BELOW_MINIMUM",
                description="General aggregate below required limit",
                source_document_id="doc_coi",
                applicable_requirement_id="REQ_GL_AGG",
                applicable_coverage_type=CoverageType.GENERAL_LIABILITY,
                evidence_chain=(),  # Empty!
            )
        self.assertIn("strictly requires non-empty supporting evidence_chain", str(ctx.exception))

    # =========================================================================
    # 7. Result Referential Integrity & Summary Derivation
    # =========================================================================

    def test_result_referential_integrity_unknown_doc_id(self):
        """NEG-09: Any evidence citation to a document not in the manifest must be rejected."""
        unknown_ref = EvidenceReference(
            document_id="doc_nonexistent",  # Not in manifest!
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Some text",
        )
        edge = DocumentRelationshipEdge(
            edge_id="e1",
            source_document_id="doc_coi",
            target_document_id="doc_end",
            relationship_type=DocumentRelationshipType.SAME_POLICY,
            classification=EdgeClassification.CONFIRMED_EXACT,
            is_confirmed=True,
            evidence_references=(self.ref1, self.ref2, unknown_ref),
        )
        with self.assertRaises(ValueError) as ctx:
            CrossDocumentReconciliationResult(
                document_packet_id="packet_1",
                packet_manifest=(self.doc1_manifest, self.doc2_manifest),
                graph_edges=(edge,),
            )
        self.assertIn("Edge evidence document_id 'doc_nonexistent' not in packet_manifest", str(ctx.exception))

    def test_result_referential_integrity_page_out_of_bounds(self):
        """NEG-10: Evidence page number exceeding document page count must be rejected."""
        oob_ref = EvidenceReference(
            document_id="doc_coi",  # page_count is 2
            document_type=DocumentType.COI,
            page_number=5,  # Out of bounds!
            source_text="Some text",
        )
        edge = DocumentRelationshipEdge(
            edge_id="e1",
            source_document_id="doc_coi",
            target_document_id="doc_end",
            relationship_type=DocumentRelationshipType.SAME_POLICY,
            classification=EdgeClassification.CONFIRMED_EXACT,
            is_confirmed=True,
            evidence_references=(self.ref1, self.ref2, oob_ref),
        )
        with self.assertRaises(ValueError) as ctx:
            CrossDocumentReconciliationResult(
                document_packet_id="packet_1",
                packet_manifest=(self.doc1_manifest, self.doc2_manifest),
                graph_edges=(edge,),
            )
        self.assertIn("out of bounds for doc 'doc_coi' (max: 2)", str(ctx.exception))

    def test_result_missing_evidence_assertion_parity(self):
        """NEG-11: assertion_evidence must match assertion_document_id and assertion_page_number."""
        miss = MissingEvidenceProvenance(
            assertion_document_id="doc_coi",
            assertion_document_type=DocumentType.COI,
            assertion_page_number=1,
            assertion_source_text="See attached CG 20 10",
            assertion_evidence=self.ref2,  # Points to doc_end instead of doc_coi!
            missing_requirement_id="REQ_AI_FORM",
            missing_form_pattern="CG 20 10",
            packet_manifest_snapshot=(self.doc1_manifest, self.doc2_manifest),
            inspected_total_pages=5,
        )
        with self.assertRaises(ValueError) as ctx:
            CrossDocumentReconciliationResult(
                document_packet_id="packet_1",
                packet_manifest=(self.doc1_manifest, self.doc2_manifest),
                missing_evidence_records=(miss,),
            )
        self.assertIn("assertion_evidence.document_id 'doc_end' != assertion_document_id 'doc_coi'", str(ctx.exception))

    def test_summary_derivation_and_compliance_relevance(self):
        """NEG-12: summary booleans must be derived from compliance-relevant records."""
        # Edge 1: confirmed compliance edge
        edge_comp = DocumentRelationshipEdge(
            edge_id="e1",
            source_document_id="doc_coi",
            target_document_id="doc_end",
            relationship_type=DocumentRelationshipType.SAME_POLICY,
            classification=EdgeClassification.CONFIRMED_EXACT,
            is_compliance_relevant=True,
            is_confirmed=True,
            requires_human_review=False,
            evidence_references=(self.ref1, self.ref2),
        )
        # Edge 2: unconfirmed informational duplicate edge (is_compliance_relevant=False)
        edge_info = DocumentRelationshipEdge(
            edge_id="e2",
            source_document_id="doc_end",
            target_document_id="doc_coi",
            relationship_type=DocumentRelationshipType.UNRELATED,
            classification=EdgeClassification.AMBIGUOUS,
            is_compliance_relevant=False,  # Informational!
            is_confirmed=False,
            requires_human_review=True,
        )

        result = CrossDocumentReconciliationResult(
            document_packet_id="packet_1",
            packet_manifest=(self.doc1_manifest, self.doc2_manifest),
            graph_edges=(edge_comp, edge_info),
        )

        # Informational edge should not prevent all_relationships_confirmed
        self.assertTrue(result.all_relationships_confirmed)
        self.assertFalse(result.has_unresolved_contradictions)
        self.assertFalse(result.has_missing_evidence)

        # Zero compliance edges must result in all_relationships_confirmed=False
        result_zero_comp = CrossDocumentReconciliationResult(
            document_packet_id="packet_2",
            packet_manifest=(self.doc1_manifest, self.doc2_manifest),
            graph_edges=(edge_info,),
        )
        self.assertFalse(result_zero_comp.all_relationships_confirmed)

    # =========================================================================
    # 8. Milestone 3.5B: Cross-Document Evidence & Referential Integrity Tests
    # =========================================================================

    def test_packet_manifest_cannot_be_empty(self):
        """CrossDocumentReconciliationResult must reject an empty packet_manifest."""
        with self.assertRaises(ValueError) as ctx:
            CrossDocumentReconciliationResult(
                document_packet_id="packet_1",
                packet_manifest=(),
            )
        self.assertIn("packet_manifest must contain at least one DocumentManifestEntry", str(ctx.exception))

    def test_packet_manifest_duplicate_document_id_rejection(self):
        """Duplicate document_id in packet_manifest must be rejected."""
        dup_entry = DocumentManifestEntry(
            document_id="doc_coi",  # Duplicate of self.doc1_manifest
            file_name="another_coi.pdf",
            sha256_hash="c" * 64,
            page_count=1,
            detected_document_type=DocumentType.COI,
        )
        with self.assertRaises(ValueError) as ctx:
            CrossDocumentReconciliationResult(
                document_packet_id="packet_1",
                packet_manifest=(self.doc1_manifest, dup_entry),
            )
        self.assertIn("Duplicate document_id 'doc_coi' in packet_manifest", str(ctx.exception))

    def test_primary_coi_id_validation(self):
        """primary_coi_id must resolve to a valid COI document entry in manifest."""
        # Case A: primary_coi_id empty string
        with self.assertRaises(ValueError) as ctx:
            CrossDocumentReconciliationResult(
                document_packet_id="packet_1",
                packet_manifest=(self.doc1_manifest, self.doc2_manifest),
                primary_coi_id="",
            )
        self.assertIn("primary_coi_id cannot be an empty string", str(ctx.exception))

        # Case B: primary_coi_id not in manifest
        with self.assertRaises(ValueError) as ctx:
            CrossDocumentReconciliationResult(
                document_packet_id="packet_1",
                packet_manifest=(self.doc1_manifest, self.doc2_manifest),
                primary_coi_id="doc_unknown",
            )
        self.assertIn("primary_coi_id 'doc_unknown' not found in packet_manifest", str(ctx.exception))

        # Case C: primary_coi_id points to ENDORSEMENT instead of COI
        with self.assertRaises(ValueError) as ctx:
            CrossDocumentReconciliationResult(
                document_packet_id="packet_1",
                packet_manifest=(self.doc1_manifest, self.doc2_manifest),
                primary_coi_id="doc_end",
            )
        self.assertIn("detected_document_type must be DocumentType.COI", str(ctx.exception))

    def test_evidence_reference_document_type_mismatch_against_manifest(self):
        """EvidenceReference document_type must match manifest detected_document_type."""
        mismatched_type_ref = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.ENDORSEMENT,  # Manifest says doc_coi is DocumentType.COI
            page_number=1,
            source_text="Apex Electrical LLC",
        )
        edge = DocumentRelationshipEdge(
            edge_id="e1",
            source_document_id="doc_coi",
            target_document_id="doc_end",
            relationship_type=DocumentRelationshipType.SAME_POLICY,
            classification=EdgeClassification.CONFIRMED_EXACT,
            is_confirmed=True,
            evidence_references=(self.ref1, self.ref2, mismatched_type_ref),
        )
        with self.assertRaises(ValueError) as ctx:
            CrossDocumentReconciliationResult(
                document_packet_id="packet_1",
                packet_manifest=(self.doc1_manifest, self.doc2_manifest),
                graph_edges=(edge,),
            )
        self.assertIn("does not match manifest detected_document_type", str(ctx.exception))

    def test_edge_cross_document_evidence_rejection(self):
        """Edge evidence referencing a document not associated with the edge must be rejected."""
        doc3_manifest = DocumentManifestEntry(
            document_id="doc_third",
            file_name="third.pdf",
            sha256_hash="d" * 64,
            page_count=2,
            detected_document_type=DocumentType.POLICY_DEC_PAGE,
        )
        third_ref = EvidenceReference(
            document_id="doc_third",
            document_type=DocumentType.POLICY_DEC_PAGE,
            page_number=1,
            source_text="Third party text",
        )
        edge = DocumentRelationshipEdge(
            edge_id="e1",
            source_document_id="doc_coi",
            target_document_id="doc_end",
            relationship_type=DocumentRelationshipType.SAME_POLICY,
            classification=EdgeClassification.CONFIRMED_EXACT,
            is_confirmed=True,
            evidence_references=(self.ref1, self.ref2, third_ref),
        )
        with self.assertRaises(ValueError) as ctx:
            CrossDocumentReconciliationResult(
                document_packet_id="packet_1",
                packet_manifest=(self.doc1_manifest, self.doc2_manifest, doc3_manifest),
                graph_edges=(edge,),
            )
        self.assertIn("Cross-document evidence reference rejected", str(ctx.exception))

    def test_entity_record_cross_document_evidence_rejection(self):
        """Entity record evidence chain or dba_evidence cannot reference unrelated documents."""
        doc3_manifest = DocumentManifestEntry(
            document_id="doc_third",
            file_name="third.pdf",
            sha256_hash="d" * 64,
            page_count=2,
            detected_document_type=DocumentType.POLICY_DEC_PAGE,
        )
        third_ref = EvidenceReference(
            document_id="doc_third",
            document_type=DocumentType.POLICY_DEC_PAGE,
            page_number=1,
            source_text="Third party text",
        )
        # Record level rejection on evidence_chain
        with self.assertRaises(ValueError) as ctx:
            EntityReconciliationRecord(
                primary_entity_name="Apex Electrical LLC",
                primary_entity_role=EntityRole.NAMED_INSURED,
                primary_document_id="doc_coi",
                referenced_entity_name="Apex Electrical LLC",
                referenced_entity_role=EntityRole.NAMED_INSURED,
                referenced_document_id="doc_end",
                classification=EntityMatchClassification.EXACT,
                is_confirmed_match=True,
                evidence_chain=(self.ref1, self.ref2, third_ref),
            )
        self.assertIn("neither primary 'doc_coi' nor referenced 'doc_end'", str(ctx.exception))

        # Record level rejection on dba_evidence
        with self.assertRaises(ValueError) as ctx:
            EntityReconciliationRecord(
                primary_entity_name="Apex Electrical LLC",
                primary_entity_role=EntityRole.NAMED_INSURED,
                primary_document_id="doc_coi",
                referenced_entity_name="Apex Electric",
                referenced_entity_role=EntityRole.NAMED_INSURED,
                referenced_document_id="doc_end",
                classification=EntityMatchClassification.CONFIRMED_DBA,
                is_confirmed_match=True,
                dba_evidence=third_ref,
                evidence_chain=(self.ref1, self.ref2, third_ref),
            )
        self.assertIn("neither primary 'doc_coi' nor referenced 'doc_end'", str(ctx.exception))

    def test_entity_record_document_not_in_manifest(self):
        """Entity record referencing document_id not in manifest must be rejected."""
        ent = EntityReconciliationRecord(
            primary_entity_name="Apex Electrical LLC",
            primary_entity_role=EntityRole.NAMED_INSURED,
            primary_document_id="doc_unknown",  # Not in manifest
            referenced_entity_name="Apex Electrical LLC",
            referenced_entity_role=EntityRole.NAMED_INSURED,
            referenced_document_id="doc_end",
            classification=EntityMatchClassification.AMBIGUOUS_IDENTITY,
            requires_human_review=True,
        )
        with self.assertRaises(ValueError) as ctx:
            CrossDocumentReconciliationResult(
                document_packet_id="packet_1",
                packet_manifest=(self.doc1_manifest, self.doc2_manifest),
                entity_records=(ent,),
            )
        self.assertIn("Entity primary_document_id 'doc_unknown' not in manifest", str(ctx.exception))

    def test_policy_record_evidence_document_matching(self):
        """Policy coi_evidence and endorsement_evidence must match their declared document_id."""
        # coi_evidence document_id mismatch at record level
        with self.assertRaises(ValueError) as ctx:
            PolicyReconciliationRecord(
                coi_document_id="doc_coi",
                coi_policy_number="GL-12345",
                coi_evidence=self.ref2,  # Points to doc_end instead of doc_coi!
                endorsement_document_id="doc_end",
                endorsement_policy_number="GL-12345",
                endorsement_evidence=self.ref2,
                classification=PolicyMatchClassification.EXACT,
                is_confirmed_match=True,
                evidence_chain=(self.ref2,),
            )
        self.assertIn("coi_evidence.document_id 'doc_end' must match coi_document_id 'doc_coi'", str(ctx.exception))

        # endorsement_evidence document_id mismatch at record level
        with self.assertRaises(ValueError) as ctx:
            PolicyReconciliationRecord(
                coi_document_id="doc_coi",
                coi_policy_number="GL-12345",
                coi_evidence=self.ref1,
                endorsement_document_id="doc_end",
                endorsement_policy_number="GL-12345",
                endorsement_evidence=self.ref1,  # Points to doc_coi instead of doc_end!
                classification=PolicyMatchClassification.EXACT,
                is_confirmed_match=True,
                evidence_chain=(self.ref1,),
            )
        self.assertIn("endorsement_evidence.document_id 'doc_coi' must match endorsement_document_id 'doc_end'", str(ctx.exception))

    def test_policy_record_cross_document_evidence_rejection(self):
        """Policy record evidence chain cannot reference third-party documents."""
        doc3_manifest = DocumentManifestEntry(
            document_id="doc_third",
            file_name="third.pdf",
            sha256_hash="d" * 64,
            page_count=2,
            detected_document_type=DocumentType.POLICY_DEC_PAGE,
        )
        third_ref = EvidenceReference(
            document_id="doc_third",
            document_type=DocumentType.POLICY_DEC_PAGE,
            page_number=1,
            source_text="Third party text",
        )
        with self.assertRaises(ValueError) as ctx:
            PolicyReconciliationRecord(
                coi_document_id="doc_coi",
                coi_policy_number="GL-12345",
                coi_evidence=self.ref1,
                endorsement_document_id="doc_end",
                endorsement_policy_number="GL-12345",
                endorsement_evidence=self.ref2,
                classification=PolicyMatchClassification.EXACT,
                is_confirmed_match=True,
                evidence_chain=(self.ref1, self.ref2, third_ref),
            )
        self.assertIn("neither coi 'doc_coi' nor endorsement 'doc_end'", str(ctx.exception))

    def test_date_record_cross_document_evidence_rejection(self):
        """DateReconciliationRecord enforces document-id matching on evidence citations."""
        # policy_date_evidence document mismatch against policy_document_id
        with self.assertRaises(ValueError) as ctx:
            DateReconciliationRecord(
                policy_document_id="doc_coi",
                policy_effective_date=date(2025, 1, 1),
                policy_expiration_date=date(2026, 1, 1),
                policy_date_evidence=(self.ref2,),  # Points to doc_end!
                evaluation_date=date(2025, 6, 1),
            )
        self.assertIn("policy_date_evidence document_id 'doc_end' does not match policy_document_id 'doc_coi'", str(ctx.exception))

        # endorsement_date_evidence document mismatch against endorsement_document_id
        with self.assertRaises(ValueError) as ctx:
            DateReconciliationRecord(
                endorsement_document_id="doc_end",
                endorsement_effective_date=date(2025, 1, 1),
                endorsement_expiration_date=date(2026, 1, 1),
                endorsement_date_evidence=(self.ref1,),  # Points to doc_coi!
                evaluation_date=date(2025, 6, 1),
            )
        self.assertIn("endorsement_date_evidence document_id 'doc_coi' does not match endorsement_document_id 'doc_end'", str(ctx.exception))

        # evidence_chain contains third-party document
        third_ref = EvidenceReference(
            document_id="doc_third",
            document_type=DocumentType.POLICY_DEC_PAGE,
            page_number=1,
            source_text="Third party text",
        )
        with self.assertRaises(ValueError) as ctx:
            DateReconciliationRecord(
                policy_document_id="doc_coi",
                policy_effective_date=date(2025, 1, 1),
                policy_expiration_date=date(2026, 1, 1),
                policy_date_evidence=(self.ref1,),
                endorsement_document_id="doc_end",
                endorsement_effective_date=date(2025, 1, 1),
                endorsement_expiration_date=date(2026, 1, 1),
                endorsement_date_evidence=(self.ref2,),
                evaluation_date=date(2025, 6, 1),
                evidence_chain=(self.ref1, self.ref2, third_ref),
            )
        self.assertIn("Cross-document evidence reference rejected", str(ctx.exception))

    def test_date_record_manifest_validation_in_result(self):
        """DateReconciliationRecord document IDs must resolve to manifest entries."""
        drec = DateReconciliationRecord(
            policy_document_id="doc_nonexistent",
            policy_effective_date=date(2025, 1, 1),
            policy_expiration_date=date(2026, 1, 1),
            policy_date_evidence=(EvidenceReference(
                document_id="doc_nonexistent",
                document_type=DocumentType.COI,
                page_number=1,
                source_text="text",
            ),),
            evaluation_date=date(2025, 6, 1),
        )
        with self.assertRaises(ValueError) as ctx:
            CrossDocumentReconciliationResult(
                document_packet_id="packet_1",
                packet_manifest=(self.doc1_manifest, self.doc2_manifest),
                date_records=(drec,),
            )
        self.assertIn("Date record policy_document_id 'doc_nonexistent' not in manifest", str(ctx.exception))

    def test_discrepancy_cross_document_evidence_rejection(self):
        """DocumentDiscrepancy evidence_chain cannot reference third-party documents."""
        third_ref = EvidenceReference(
            document_id="doc_third",
            document_type=DocumentType.POLICY_DEC_PAGE,
            page_number=1,
            source_text="Third party text",
        )
        # Record-level rejection
        with self.assertRaises(ValueError) as ctx:
            DocumentDiscrepancy(
                discrepancy_id="disc_1",
                category=DiscrepancyCategory.DOCUMENT_DISCREPANCY,
                severity=Severity.MAJOR,
                reason_code="ENTITY_NAME_MISMATCH",
                description="Names do not match between COI and Endorsement",
                source_document_id="doc_coi",
                conflicting_document_id="doc_end",
                evidence_chain=(self.ref1, third_ref),  # third_ref is not doc_coi or doc_end!
            )
        self.assertIn("neither source 'doc_coi' nor conflicting 'doc_end'", str(ctx.exception))

    def test_missing_evidence_page_out_of_bounds_and_type_mismatch(self):
        """MissingEvidenceProvenance assertion must match manifest page count and doc type."""
        # Page out of bounds
        miss_oob = MissingEvidenceProvenance(
            assertion_document_id="doc_coi",
            assertion_document_type=DocumentType.COI,
            assertion_page_number=99,  # Out of bounds! doc_coi has 2 pages
            assertion_source_text="See attached CG 20 10",
            assertion_evidence=EvidenceReference(
                document_id="doc_coi",
                document_type=DocumentType.COI,
                page_number=99,
                source_text="See attached CG 20 10",
            ),
            missing_requirement_id="REQ_AI_FORM",
            missing_form_pattern="CG 20 10",
            packet_manifest_snapshot=(self.doc1_manifest, self.doc2_manifest),
            inspected_total_pages=5,
        )
        with self.assertRaises(ValueError) as ctx:
            CrossDocumentReconciliationResult(
                document_packet_id="packet_1",
                packet_manifest=(self.doc1_manifest, self.doc2_manifest),
                missing_evidence_records=(miss_oob,),
            )
        self.assertIn("out of bounds for doc 'doc_coi'", str(ctx.exception))

        # Document type mismatch
        miss_type = MissingEvidenceProvenance(
            assertion_document_id="doc_coi",
            assertion_document_type=DocumentType.ENDORSEMENT,  # Mismatch: doc_coi is DocumentType.COI
            assertion_page_number=1,
            assertion_source_text="See attached CG 20 10",
            assertion_evidence=EvidenceReference(
                document_id="doc_coi",
                document_type=DocumentType.ENDORSEMENT,
                page_number=1,
                source_text="See attached CG 20 10",
            ),
            missing_requirement_id="REQ_AI_FORM",
            missing_form_pattern="CG 20 10",
            packet_manifest_snapshot=(self.doc1_manifest, self.doc2_manifest),
            inspected_total_pages=5,
        )
        with self.assertRaises(ValueError) as ctx:
            CrossDocumentReconciliationResult(
                document_packet_id="packet_1",
                packet_manifest=(self.doc1_manifest, self.doc2_manifest),
                missing_evidence_records=(miss_type,),
            )
        self.assertIn("does not match manifest detected_document_type", str(ctx.exception))

    def test_validate_evidence_reference_helper(self):
        """Top-level validate_evidence_reference helper verifies citations against manifest."""
        manifest_map = {self.doc1_manifest.document_id: self.doc1_manifest}

        # Valid reference passes
        validate_evidence_reference(self.ref1, manifest_map, "Test")

        # Unknown document
        unknown_ref = EvidenceReference(
            document_id="doc_ghost",
            document_type=DocumentType.COI,
            page_number=1,
            source_text="Ghost doc",
        )
        with self.assertRaises(ValueError) as ctx:
            validate_evidence_reference(unknown_ref, manifest_map, "Test")
        self.assertIn("Test evidence document_id 'doc_ghost' not in packet_manifest", str(ctx.exception))

        # Page out of bounds (doc1 has 2 pages)
        oob_ref = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.COI,
            page_number=3,
            source_text="Page 3",
        )
        with self.assertRaises(ValueError) as ctx:
            validate_evidence_reference(oob_ref, manifest_map, "Test")
        self.assertIn("out of bounds for doc 'doc_coi' (max: 2)", str(ctx.exception))

        # Document type mismatch
        mismatched_ref = EvidenceReference(
            document_id="doc_coi",
            document_type=DocumentType.ENDORSEMENT,
            page_number=1,
            source_text="Page 1",
        )
        with self.assertRaises(ValueError) as ctx:
            validate_evidence_reference(mismatched_ref, manifest_map, "Test")
        self.assertIn("does not match manifest detected_document_type", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
