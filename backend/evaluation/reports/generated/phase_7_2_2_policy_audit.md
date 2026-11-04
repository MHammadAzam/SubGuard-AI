# SubGuard AI — Phase 7.2.2 Policy Audit Report
## Deep Compliance Policy Audit, False-Pass Investigation & Regression-Test Design

**Phase:** 7.2.2  
**Date:** November 4, 2026  
**Status:** COMPLETE — AWAITING OWNER APPROVAL  
**Auditor:** Independent Senior Software Architect & Insurance Technology Specialist  
**Execution Mode:** Read-Only Audit & Proposal (Offline / Mock Provider Only)  
**API Invocations:** 0 (Strictly Prohibited & Enforced)  
**Production Code Changes:** 0 (Strictly Prohibited & Enforced)  

---

## A. Executive Summary

This audit delivers an evidence-based technical investigation into three false-pass compliance vulnerabilities identified during the Phase 7.2 benchmark execution and Phase 7.2.1 benchmark audit of the frozen Cohort v1.0.0 (50 multi-document synthetic scenarios).

### Core Architectural Principle
The SubGuard AI system strictly enforces:
> **AI extracts information → Deterministic Python rules evaluate it → Humans resolve uncertainty.**

A false-pass result (False Compliant Result, or FCR) represents the most critical failure mode in commercial insurance compliance. In construction risk management, allowing an unverified, future-dated, manuscript-restricted, or contradictory document set to receive a `COMPLIANT` verdict transfers severe bodily injury and property damage liabilities directly to the general contractor or property owner without primary, non-contributory insurance backing.

### Key Audit Findings

1. **Case A — Future Policy Effective Date (`SCEN-16`):**
   - **Status:** `[CONFIRMED]` Critical Policy Gap.
   - **Root Cause:** Standard requirement profiles omit `RequirementType.POLICY_EFFECTIVE_DATE`. The evaluator only inspects `POLICY_EXPIRATION` against the evaluation date. When an uncommenced policy (e.g., effective 2025-09-01 evaluated on 2025-06-01) has a future expiration date (2026-09-01), the expiration rule passes and no effective date check is performed, resulting in an unmerited `COMPLIANT` verdict.
   - **Severity:** P0 — Critical (Immediate bodily injury / property exposure during active operations).

2. **Case B — Manuscript Endorsements & Restrictive Wording (`SCEN-24`, `SCEN-27`):**
   - **Status:** `[CONFIRMED]` Architectural Distinction Gap (Presence vs. Sufficiency).
   - **Root Cause:** `evaluate_endorsement_presence()` verifies string presence and exact quote grounding for standard form headers (e.g., `CG 20 10`), but possesses no capability to detect manuscript restrictions, exclusionary endorsements, or custom operation limitations that nullify coverage for required subcontracted work.
   - **Severity:** P0 — Critical (Illusion of coverage; claim denial upon loss).

3. **Case C — Cross-Document Date Contradictions (`SCEN-36`):**
   - **Status:** `[CONFIRMED]` Incomplete Pipeline Aggregation.
   - **Root Cause:** While `ComplianceEvaluator._aggregate_overall_status()` correctly handles `raw_contradictions_detected`, the document aggregation layer in `ExtractionPipelineService` does not algorithmically compare dates across multi-document sets (e.g., COI expiration vs. Endorsement policy term). Discrepant dates are silently discarded or overwritten, leaving `raw_contradictions_detected` empty and returning `COMPLIANT`.
   - **Severity:** P1 — High (Lapsed underlying policy represented as active on a summary certificate).

### Baseline Test Health & Cohort Stability
- **Active Unit/Integration Tests:** 205 passed in 6.895s (0 failures, 0 errors) `[CONFIRMED]`.
- **Cohort Manifest Integrity:** 50/50 scenarios valid, SHA-256 manifest verified with 0 errors `[CONFIRMED]`.
- **Phase 7.2.1 Metric Decoupling:** Verified and confirmed. Unsafe Non-Pass Rate: 86.67% (26/30); Uncertainty Recall: 55.00% (11/20); Human-Review Recall: 40.00% (6/15); Unable-to-Determine Recall: 100.00% (5/5).

---

## B. Repository Integrity

### B.1 Git State & Commit Verification
- **Current Head Commit:** `2bb3744aad6bca8778f4867bd85107a54a68bfd2` `[CONFIRMED]`.
- **Staged Files:** 0 `[CONFIRMED]`.
- **Unstaged Working Directory Modifications (from Phase 7.2.1):**
  - `backend/evaluation/README.md`
  - `backend/evaluation/metrics/compliance_metrics.py`
  - `backend/evaluation/runners/scenario_runner.py`
  - `backend/evaluation/schemas/reports.py`
  - `backend/tests/test_benchmark_cohort_v1.py`
- **Production Code Status:** 0 modifications to `backend/app/` `[CONFIRMED]`.
- **Test Baseline:** Exactly 205 discovery tests pass without error `[CONFIRMED]`.

---

## C. Case A — Future Policy Effective Date Policy Audit (`SCEN-16`)

### C.1 Tracing the Evaluation Pipeline
In `backend/app/compliance/evaluator.py`:
- `evaluate_policy_effective_date()` exists (lines 142-181) and correctly asserts:
  ```python
  if doc_set.policy_effective_date > evaluation_date:
      return ComplianceFinding(
          status=ComplianceStatus.NON_COMPLIANT,
          reason_code=ReasonCode.POLICY_NOT_YET_EFFECTIVE,
          ...
      )
  ```
- However, in `ComplianceEvaluator.evaluate()`, requirements are evaluated dynamically:
  ```python
  for req in requirements:
      if req.requirement_type == RequirementType.COVERAGE_LIMIT:
          ...
      elif req.requirement_type == RequirementType.POLICY_EXPIRATION:
          findings.append(self.evaluate_policy_expiration(...))
      elif req.requirement_type == RequirementType.POLICY_EFFECTIVE_DATE:
          findings.append(self.evaluate_policy_effective_date(...))
  ```
- In `backend/evaluation/generators/generator.py` (`get_standard_requirements()`) and typical industry contract profiles, the standard profile only specifies:
  - `REQ_GL_OCCURRENCE`
  - `REQ_GL_AGGREGATE`
  - `REQ_GL_EXPIRATION`
  - `REQ_AI_ENDORSEMENT`
  - `REQ_WOS_ENDORSEMENT`
- `RequirementType.POLICY_EFFECTIVE_DATE` is completely absent.
- Furthermore, `evaluate_policy_expiration()` (lines 100-140) only evaluates:
  ```python
  if eval_date > exp_date:
      return NON_COMPLIANT, ReasonCode.POLICY_EXPIRED
  else:
      return COMPLIANT, ReasonCode.POLICY_ACTIVE
  ```
  It never inspects `policy_effective_date`.
- In `SCEN-16`:
  - `policy_effective_date = 2025-09-01`
  - `evaluation_date = 2025-06-01`
  - `policy_expiration_date = 2026-09-01`
  Because `POLICY_EFFECTIVE_DATE` is not in `requirements`, `evaluate_policy_effective_date()` is never called. `evaluate_policy_expiration()` evaluates `2025-06-01 <= 2026-09-01` and returns `COMPLIANT` (`POLICY_ACTIVE`). All other rules pass, returning an overall verdict of `COMPLIANT` `[CONFIRMED]`.

### C.2 Policy Options Analysis

#### Option A: Mandatory Baseline Effective-Date Enforcement in Profile Validator
- **Mechanism:** Enforce via `RequirementProfile` validation that any profile containing `RequirementType.POLICY_EXPIRATION` must also contain `RequirementType.POLICY_EFFECTIVE_DATE`. If omitted, the profile parser rejects or injects a default baseline rule.
- **Pros:** Preserves strict deterministic requirement-driven architecture; does not hardcode hidden logic in evaluator.
- **Cons:** Legacy profiles or external API callers omitting the field will fail schema validation.

#### Option B: Omitted Means Not Evaluated (Strict Scope Limitation)
- **Mechanism:** Maintain current behavior, but emit an explicit `UNREVIEWED_SCOPE` warning or metadata tag in `ComplianceReviewResult` indicating that effective date was present in the document but unreviewed.
- **Pros:** Completely transparent; adheres to contractual literalism.
- **Cons:** Fails commercial safety expectations. A general contractor using SubGuard AI assumes that a "Compliant" certificate means the policy is currently in force.

#### Option C: Defensive Active Term Boundary Assertion in Evaluator
- **Mechanism:** Redefine the meaning of `evaluate_policy_expiration()` to represent an "In-Force Policy Term Assertion". Whenever `policy_effective_date` is present on the document set, `evaluate_policy_expiration()` or the evaluator core asserts:
  `policy_effective_date <= evaluation_date <= policy_expiration_date`.
  If `policy_effective_date > evaluation_date`, it fails with `POLICY_NOT_YET_EFFECTIVE`.
- **Pros:** Zero configuration burden; eliminates false-pass at the engine level immediately.
- **Cons:** Overloads `POLICY_EXPIRATION` requirement semantics.

### C.3 Recommended Solution for Case A
`[REQUIRES OWNER APPROVAL]`
**Hybrid Recommendation (Option A + Option C Variant):**
1. Require standard profiles to explicitly include `RequirementType.POLICY_EFFECTIVE_DATE`.
2. Introduce a mandatory core policy assertion in `ComplianceEvaluator.evaluate()`: If `doc_set.policy_effective_date` is extracted and `doc_set.policy_effective_date > evaluation_date`, the evaluator must flag `POLICY_NOT_YET_EFFECTIVE` as an unprompted safety boundary violation (or route to `REQUIRES_HUMAN_REVIEW`), preventing an unmerited `COMPLIANT` overall verdict regardless of profile specification.

---

## D. Case B — Manuscript Endorsements and Restrictive Wording Policy Audit (`SCEN-24`, `SCEN-27`)

### D.1 Tracing the Evaluation Pipeline
In `backend/app/compliance/evaluator.py:evaluate_endorsement_presence()`:
- Evaluator tests:
  1. No endorsement mentioned -> `NON_COMPLIANT` (`ENDORSEMENT_NOT_FOUND`).
  2. Mentioned on COI but no document attached -> `REQUIRES_HUMAN_REVIEW` (`COI_CHECKBOX_WITHOUT_ENDORSEMENT_DOC`).
  3. Extraction status uncertain or unverified evidence support -> `REQUIRES_HUMAN_REVIEW` (`EVIDENCE_SUPPORT_UNVERIFIED`).
  4. Blanket endorsement -> `REQUIRES_HUMAN_REVIEW` (`BLANKET_ENDORSEMENT_REQUIRES_CONTRACT_CHECK`).
  5. Multi-endorsement policy contradiction -> `REQUIRES_HUMAN_REVIEW` (`CONTRADICTING_ENDORSEMENTS_DETECTED`).
  6. Otherwise -> `COMPLIANT` (`ENDORSEMENT_VERIFIED`).
- In `SCEN-24` and `SCEN-27`:
  - An endorsement document exists.
  - Form title / number matches keywords (`CG 20 10`).
  - Evidence quote grounding is verified.
  - However, the document contains manuscript modifications (e.g. "Coverage is restricted strictly to work performed in County X" or "Excluding all exterior work above 10 feet").
  - The evaluator has no parameter or schema field representing *sufficiency* or *restrictions*, so it returns `COMPLIANT` `[CONFIRMED]`.

### D.2 Presence vs. Sufficiency Formal Definition
- **Endorsement Presence:** The physical and lexical identification of an endorsement document attached to the policy, matching expected standard form identifiers (e.g. ISO `CG 20 10 04 13`, `CG 20 37 04 13`).
- **Endorsement Sufficiency:** The legal and contractual adequacy of the endorsement wording to fulfill the indemnification and additional insured obligations without manuscript exclusions, conditional limitations, unapproved geographical restrictions, or subcontractor warranty warranties.

### D.3 Policy Options Analysis

#### Option A: Conservative Human-Review Routing on Non-Standard Indicators
- **Mechanism:** Expand `EndorsementExtraction` to include `is_standard_form: bool` and `manuscript_indicators_detected: List[str]`. If an endorsement is non-ISO, manuscript, or contains restriction markers ("modified", "manuscript", "restricted", "exclusion schedule"), the evaluator automatically routes to `REQUIRES_HUMAN_REVIEW` with `ReasonCode.MANUSCRIPT_RESTRICTION_DETECTED`.
- **Pros:** Preserves the core principle: humans resolve semantic legal nuances; AI flags the deviation.
- **Cons:** Increases human review queue volume for non-standard but harmless forms.

#### Option B: Upstream Semantic Extraction of Endorsement Scope
- **Mechanism:** LLM provider extracts detailed coverage scopes (ongoing operations, completed operations, cross-liability, location restrictions). Deterministic evaluator matches these against requirement criteria.
- **Pros:** Highly automated.
- **Cons:** Violates "AI never makes compliance decisions"; places critical legal interpretation into non-deterministic prompt completions.

#### Option C: Status Quo (Keyword / Form Number Match Only)
- **Mechanism:** Maintain current presence-only verification.
- **Pros:** 0 engineering effort.
- **Cons:** Unacceptable liability; leaves general contractors completely unprotected against restrictive manuscript exclusions.

### D.4 Recommended Solution for Case B
`[REQUIRES OWNER APPROVAL]`
**Adopt Option A:**
1. Maintain deterministic rule evaluation.
2. In the extraction schema, extract `is_standard_iso_form: bool` and `manuscript_clause_present: bool`.
3. In `evaluate_endorsement_presence()`, if `manuscript_clause_present` is True or `is_standard_iso_form` is False, route to `ComplianceStatus.REQUIRES_HUMAN_REVIEW` under reason code `ReasonCode.MANUSCRIPT_ENDORSEMENT_REQUIRES_REVIEW`.

---

## E. Case C — Cross-Document Date Contradictions Policy Audit (`SCEN-36`)

### E.1 Tracing the Evaluation Pipeline
In `backend/app/compliance/evaluator.py`:
- `_aggregate_overall_status()` includes:
  ```python
  if doc_set.raw_contradictions_detected:
      return ComplianceStatus.REQUIRES_HUMAN_REVIEW, ReasonCode.CROSS_DOCUMENT_CONTRADICTION
  ```
- However, in `backend/app/extraction/pipeline.py`, when multi-document sets (e.g., COI + Policy Declarations + Endorsement) are processed:
  - Each document is parsed individually.
  - The aggregate `ExtractedDocumentSet` has a single `policy_effective_date` and `policy_expiration_date`.
  - There is NO algorithmic comparison across documents.
  - In `SCEN-36`, the COI states expiration is `2026-05-01`, while the attached policy document states expiration was `2025-05-01`.
  - The extraction pipeline took the first document's date or the COI's date and left `raw_contradictions_detected` empty.
  - The evaluator saw a valid future date, had an empty contradiction list, and issued a `COMPLIANT` verdict `[CONFIRMED]`.

### E.2 Policy Options Analysis

#### Option A: Deterministic Multi-Document Date Reconciliation in Aggregation
- **Mechanism:** During multi-document assembly in `ExtractionPipelineService`, collect all document dates into a normalized list:
  `[(doc_id, doc_type, eff_date, exp_date)]`.
  Compute unique non-null values. If distinct effective dates or expiration dates exist across documents within the same coverage line, automatically append a descriptive contradiction entry to `doc_set.raw_contradictions_detected`.
- **Pros:** Completely deterministic, zero reliance on LLM awareness, leverages existing `raw_contradictions_detected` evaluator handler.
- **Cons:** Requires clear policy on date discrepancies (e.g., endorsement term dates vs. master policy term dates).

#### Option B: Direct Document-Level Finding in ComplianceEvaluator
- **Mechanism:** Pass the list of individual document extractions to `ComplianceEvaluator`. Add an explicit `evaluate_document_consistency()` rule that generates individual compliance findings for date, limit, and insured name contradictions.
- **Pros:** Provides granular per-finding tracking in `ComplianceReviewResult.findings`.
- **Cons:** Modifies `ExtractedDocumentSet` schema and evaluator contract.

#### Option C: Status Quo (Rely on LLM Provider to Detect Contradictions)
- **Mechanism:** Prompt LLM to spot contradictions across pages.
- **Pros:** None.
- **Cons:** Unreliable, non-deterministic, already proven to fail in `SCEN-36`.

### E.3 Recommended Solution for Case C
`[REQUIRES OWNER APPROVAL]`
**Adopt Option A + Option B Hybrid:**
1. In the multi-document pipeline aggregator, deterministically compare extracted effective dates, expiration dates, named insured entities, and coverage limits across all documents in the bundle.
2. If conflicting values are found, populate `doc_set.raw_contradictions_detected`.
3. The evaluator automatically escalates to `REQUIRES_HUMAN_REVIEW` with `ReasonCode.CROSS_DOCUMENT_CONTRADICTION`.

---

## F. Cross-Case Risk Analysis (Compound Failure Modes)

When the three vulnerabilities interact, they create severe compounding blindspots:

### Composite Failure Scenario 1: Uncommenced Term + Restrictive Manuscript
- **Configuration:** Subcontractor submits a COI showing coverage starting 3 months in the future with an attached manuscript endorsement containing restrictive location exclusions.
- **Failure Mode:** Evaluator passes expiration because the future expiration is valid. Evaluator passes endorsement because `CG 20 10` keywords are present.
- **Result:** Overall status is falsely `COMPLIANT`. The subcontractor operates on site with neither active insurance nor valid additional insured coverage.

### Composite Failure Scenario 2: Contradictory Date + Uncommenced Term
- **Configuration:** COI reports active dates, but attached policy declarations document shows an in-advance renewal starting in the future, while the current policy has expired.
- **Failure Mode:** Lack of cross-document reconciliation accepts the COI dates; absence of effective date requirement ignores the future start date.
- **Result:** Overall status is falsely `COMPLIANT`.

### Composite Failure Scenario 3: Triple Compound Failure
- **Configuration:** Multi-page packet with contradictory policy dates between COI and policy jacket, a future inception date on the endorsement, and manuscript exclusions.
- **Result:** System issues a `COMPLIANT` badge, providing total false security to risk managers.

---

## G. Complete Regression-Test Matrix

To ensure that future implementations permanently prevent these vulnerabilities, a 4-level regression test matrix is designed:

| Test ID | Level | Vulnerability Case | Input Document & Extraction State | Evaluated Requirements | Expected Status | Expected Reason Code |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `TEST_UNIT_EFF_01` | L1 (Unit) | Case A | `eff=2025-09-01`, `exp=2026-09-01`, `eval=2025-06-01` | `POLICY_EXPIRATION`, `POLICY_EFFECTIVE_DATE` | `NON_COMPLIANT` | `POLICY_NOT_YET_EFFECTIVE` |
| `TEST_UNIT_EFF_02` | L1 (Unit) | Case A | `eff=2025-09-01`, `exp=2026-09-01`, `eval=2025-06-01` (Profile omits eff date, engine boundary active) | `POLICY_EXPIRATION` | `REQUIRES_HUMAN_REVIEW` | `POLICY_NOT_YET_EFFECTIVE` |
| `TEST_UNIT_EFF_03` | L1 (Unit) | Case A | `eff=2024-01-01`, `exp=2025-01-01`, `eval=2024-06-01` | `POLICY_EFFECTIVE_DATE` | `COMPLIANT` | `POLICY_EFFECTIVE_DATE_VALID` |
| `TEST_UNIT_MAN_01` | L1 (Unit) | Case B | Form `CG 20 10`, `manuscript_clause_present=True` | `REQ_AI_ENDORSEMENT` | `REQUIRES_HUMAN_REVIEW` | `MANUSCRIPT_RESTRICTION_DETECTED` |
| `TEST_UNIT_MAN_02` | L1 (Unit) | Case B | Form `MANUSCRIPT_FORM_01`, `is_standard_iso_form=False` | `REQ_AI_ENDORSEMENT` | `REQUIRES_HUMAN_REVIEW` | `NON_STANDARD_ENDORSEMENT_FORM` |
| `TEST_UNIT_MAN_03` | L1 (Unit) | Case B | Standard ISO `CG 20 10 04 13`, `manuscript=False` | `REQ_AI_ENDORSEMENT` | `COMPLIANT` | `ENDORSEMENT_VERIFIED` |
| `TEST_UNIT_XDOC_01`| L1 (Unit) | Case C | COI `exp=2026-05-01`, Policy Dec `exp=2025-05-01` | Multi-doc Aggregation | `REQUIRES_HUMAN_REVIEW` | `CROSS_DOCUMENT_CONTRADICTION` |
| `TEST_UNIT_XDOC_02`| L1 (Unit) | Case C | COI `limit=1M`, Policy Dec `limit=500K` | Multi-doc Aggregation | `REQUIRES_HUMAN_REVIEW` | `CROSS_DOCUMENT_CONTRADICTION` |
| `TEST_INT_E2E_01`  | L2 (Int)  | Case A | Synthetic PDF: Future effective date COI | Standard Contract Profile | `NON_COMPLIANT` | `POLICY_NOT_YET_EFFECTIVE` |
| `TEST_INT_E2E_02`  | L2 (Int)  | Case B | Synthetic PDF: Manuscript Endorsement attached | Standard Contract Profile | `REQUIRES_HUMAN_REVIEW` | `MANUSCRIPT_RESTRICTION_DETECTED` |
| `TEST_INT_E2E_03`  | L2 (Int)  | Case C | Synthetic PDF Bundle: Discrepant Exp Dates | Standard Contract Profile | `REQUIRES_HUMAN_REVIEW` | `CROSS_DOCUMENT_CONTRADICTION` |
| `TEST_COHORT_16`   | L4 (Cohort)| Case A | Cohort Scenario `SCEN-16` | Cohort Requirement Profile | `NON_COMPLIANT` / `REQUIRES_HUMAN_REVIEW` | Expected non-pass |
| `TEST_COHORT_24`   | L4 (Cohort)| Case B | Cohort Scenario `SCEN-24` | Cohort Requirement Profile | `REQUIRES_HUMAN_REVIEW` | Expected non-pass |
| `TEST_COHORT_36`   | L4 (Cohort)| Case C | Cohort Scenario `SCEN-36` | Cohort Requirement Profile | `REQUIRES_HUMAN_REVIEW` | Expected non-pass |

---

## H. Requirement-Profile Architecture Review

### H.1 Profile Validation & Defaults
Currently, `RequirementProfile` is a flat Pydantic model containing a list of `InsuranceRequirement`.
To eliminate Case A, the profile schema should support:
1. **Mandatory Baseline Rule Sets:** Define a minimum contractual baseline:
   - Coverage occurrence limit.
   - Coverage aggregate limit.
   - Policy effective date assertion.
   - Policy expiration date assertion.
2. **Profile Versioning:** Add `profile_version: str` (e.g. `v1.1.0`) to guarantee immutability and reproducibility.
3. **Pre-Evaluation Validator:** Ensure that if `POLICY_EXPIRATION` is defined, the system validates whether `POLICY_EFFECTIVE_DATE` is present or explicitly opted out with documented justification.

---

## I. Status and Reason-Code Reference Mapping

All 12 edge cases evaluated across Cases A, B, and C map cleanly to existing or minimal proposed enum values:

| # | Boundary Condition | Recommended Status | Recommended Reason Code | Existing vs Proposed |
| :- | :--- | :--- | :--- | :--- |
| 1 | Effective date in future relative to eval date | `NON_COMPLIANT` | `POLICY_NOT_YET_EFFECTIVE` | Existing |
| 2 | Effective date missing on doc set | `REQUIRES_HUMAN_REVIEW` | `EFFECTIVE_DATE_NOT_EXTRACTED` | Existing (`MISSING_REQUIRED_DOCUMENT` / `UNCERTAIN`) |
| 3 | Expiration date expired | `NON_COMPLIANT` | `POLICY_EXPIRED` | Existing |
| 4 | Effective date omitted from profile, but future on doc | `REQUIRES_HUMAN_REVIEW` | `POLICY_NOT_YET_EFFECTIVE` | Existing |
| 5 | Standard ISO endorsement with verified evidence | `COMPLIANT` | `ENDORSEMENT_VERIFIED` | Existing |
| 6 | Manuscript endorsement with restrictive clause | `REQUIRES_HUMAN_REVIEW` | `MANUSCRIPT_RESTRICTION_DETECTED` | Proposed Enum Value |
| 7 | Non-standard / unapproved endorsement form number | `REQUIRES_HUMAN_REVIEW` | `NON_STANDARD_ENDORSEMENT_FORM` | Proposed Enum Value |
| 8 | Blanket endorsement requiring contract check | `REQUIRES_HUMAN_REVIEW` | `BLANKET_ENDORSEMENT_REQUIRES_CONTRACT_CHECK` | Existing |
| 9 | COI checkbox only without endorsement doc | `REQUIRES_HUMAN_REVIEW` | `COI_CHECKBOX_WITHOUT_ENDORSEMENT_DOC` | Existing |
| 10 | Cross-document date mismatch (COI vs Policy Dec) | `REQUIRES_HUMAN_REVIEW` | `CROSS_DOCUMENT_CONTRADICTION` | Existing |
| 11 | Cross-document limit mismatch (COI vs Endorsement) | `REQUIRES_HUMAN_REVIEW` | `CROSS_DOCUMENT_CONTRADICTION` | Existing |
| 12 | Contradicting endorsements within same policy line | `REQUIRES_HUMAN_REVIEW` | `CONTRADICTING_ENDORSEMENTS_DETECTED` | Existing |

---

## J. Reporting Integrity Review

### J.1 Decoupled Metrics from Phase 7.2.1
The benchmark audit in Phase 7.2.1 decoupled the conflated metric from Phase 7.2.
- **Unsafe Non-Pass Rate:** `26 / 30 = 86.67%` (reflecting overall non-compliant + uncertain routing).
- **Uncertainty Recall:** `11 / 20 = 55.00%` (measuring scenarios whose ground truth specifically requires uncertainty routing).
- **Human-Review Recall:** `6 / 15 = 40.00%`.
- **Unable-to-Determine Recall:** `5 / 5 = 100.00%`.

### J.2 Production Isolation
Audit confirms that `backend/evaluation/metrics/compliance_metrics.py` and `backend/evaluation/runners/scenario_runner.py` are strictly quarantined within `backend/evaluation/`. They are neither imported by nor accessible to `backend/app/main.py` or production API routers `[CONFIRMED]`.

---

## K. Proposed Minimal Future Implementation Plan

The proposed implementation is decomposed into 4 modular, test-driven work packages:

### Work Package A: Requirement-Profile & Effective-Date Safeguards
- **Files:** `backend/app/schemas/insurance.py`, `backend/app/compliance/evaluator.py`.
- **Changes:**
  - Add default baseline validation to ensure `POLICY_EFFECTIVE_DATE` is evaluated whenever `POLICY_EXPIRATION` is present.
  - Add defensive term check in `evaluate_policy_expiration()` or evaluator core.
- **Complexity:** Low.

### Work Package B: Endorsement Sufficiency & Manuscript Safeguards
- **Files:** `backend/app/schemas/insurance.py`, `backend/app/compliance/evaluator.py`.
- **Changes:**
  - Extend `EndorsementExtraction` schema with `manuscript_clause_present` and `is_standard_iso_form`.
  - Add reason codes `MANUSCRIPT_RESTRICTION_DETECTED` and `NON_STANDARD_ENDORSEMENT_FORM`.
  - Update `evaluate_endorsement_presence()` to escalate when either flag is present.
- **Complexity:** Medium.

### Work Package C: Deterministic Cross-Document Reconciliation
- **Files:** `backend/app/extraction/pipeline.py`, `backend/app/compliance/evaluator.py`.
- **Changes:**
  - Add deterministic cross-document comparator in `ExtractionPipelineService._aggregate_document_set()`.
  - Automatically populate `doc_set.raw_contradictions_detected` when multi-document dates, limits, or insured names contradict.
- **Complexity:** Medium.

### Work Package D: Regression Suite & Cohort Verification
- **Files:** `backend/tests/test_compliance_evaluator.py`, `backend/tests/test_pipeline_compliance_e2e.py`.
- **Changes:**
  - Implement Level 1-3 test suite covering all 12 edge cases.
  - Re-run cohort evaluation to verify resolution of `SCEN-16`, `SCEN-24`, and `SCEN-36`.
- **Complexity:** Low.

---

## L. Unresolved Decisions & Open Policy Questions
`[REQUIRES OWNER APPROVAL]`

1. **Policy Decision 1 (Effective Date Omission):**
   Should an omitted `POLICY_EFFECTIVE_DATE` in a custom requirement profile cause a validation error at API ingress, or should the compliance evaluator silently execute a fallback in-force date check?
2. **Policy Decision 2 (Manuscript Routing Threshold):**
   Should any non-ISO endorsement form number immediately escalate to `REQUIRES_HUMAN_REVIEW`, or should approved carrier-specific manuscript forms (e.g., Travelers, Liberty Mutual proprietary endorsements) be whitelisted?
3. **Policy Decision 3 (Cross-Document Date Tolerance):**
   Should endorsements that expire before the master policy term automatically trigger `CROSS_DOCUMENT_CONTRADICTION`, or should they trigger a specialized `ENDORSEMENT_TERM_MISMATCH` finding?
4. **Policy Decision 4 (Reason Code Schema Evolution):**
   Are the proposed reason codes `MANUSCRIPT_RESTRICTION_DETECTED` and `NON_STANDARD_ENDORSEMENT_FORM` approved for inclusion in `ReasonCode` enum?

---

## M. Final Verdict

### **POLICY PROPOSAL COMPLETE — AWAITING OWNER APPROVAL**

The Phase 7.2.2 policy audit has thoroughly diagnosed the root causes of the three benchmark false-pass findings (`SCEN-16`, `SCEN-24`, `SCEN-36`). Concrete, deterministic, and safe remediation architectures have been formulated alongside comprehensive regression test designs.

No code modifications have been made to production or test files during this phase. All work is strictly quarantined and ready for owner review and Phase 7.3 implementation authorization.
