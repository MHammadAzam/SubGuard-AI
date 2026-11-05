# SUBGUARD AI — PHASE 7.2.3.6 FINAL CLOSURE REPORT
## Manuscript Trust-Boundary Closure, Adversarial Verification & Baseline Sign-Off

**Date:** November 5, 2026  
**Phase:** 7.2.3.6 — Final Manuscript Trust-Boundary Closure  
**Mode:** Controlled remediation, adversarial testing, offline benchmark verification  
**Live API Calls:** 0 (Mock provider only)  
**Overall Verdict:** **PASS**

---

## 1. Executive Summary

### Final Verdict: PASS

Phase 7.2.3.6 has successfully eliminated the remaining provider-trust boundary vulnerability in negative manuscript verification, fully restoring SubGuard AI's core architectural principle:
> **AI extracts information. Deterministic Python rules evaluate it. Humans resolve uncertainty.**

### Core Problem Addressed
In Phase 7.2.3.5, provider assertions of `full_text_inspection` were rejected as a valid verification basis. However, Check 8b of the evaluator still permitted `manuscript_verification_basis == "unmodified_standard_form"` to authorize compliance for recognized standard form numbers. Because `manuscript_verification_basis` originates directly from the AI extraction provider (`RawEndorsement`), an untrusted AI model could simply assert that a recognized form (e.g. `CG 20 10 04 13`) was unmodified without proving it. A recognized form number proves only the form heading; it does not prove that manuscript deletions, alterations, or custom exclusionary clauses were absent from the underlying document text.

### Solution Implemented
1. **Schema Trust Separation:** Added `independent_integrity_verified: bool = Field(default=False)` to `EndorsementExtraction`. This attribute cannot be asserted by extraction providers. It can only be asserted by an independent, trusted document-integrity mechanism (e.g., standard form template diffing or authoritative hash verification) or an authenticated human operator.
2. **Untrusted Provider Neutralization:** `ExtractionPipelineService.process_inspection()` strictly initializes `independent_integrity_verified=False` for all provider extractions.
3. **Strict Conservative Fallback:** Check 8b in `ComplianceEvaluator` enforces that any endorsement claiming `manuscript_status == "NOT_DETECTED"` must possess `independent_integrity_verified is True` AND a recognized standard form number. In the absence of independent integrity verification, the system conservatively routes to `REQUIRES_HUMAN_REVIEW` with reason code `MANUSCRIPT_VERIFICATION_UNSUPPORTED`.
4. **Adversarial Verification:** Added 10 adversarial regression tests (plus positive control) in `backend/tests/test_phase_7_2_3_safeguards.py`.
5. **Full Suite Execution:** All 251 tests passed (0 failures, 0 errors) in 6.104s.
6. **Benchmark Integrity:** The 40-scenario cohort executed with 100% completion (40/40), maintaining **0.00% False Compliance Rate (FCR: 0/10)** and **0.00% Unsafe Compliance Rate (UCR: 0/30)**.

---

## 2. Git Traceability

| Attribute | Value |
|---|---|
| **Branch** | `main` |
| **Initial HEAD Commit** | `b983d1bfede1ee80cca5f9b47de29b6cc27712a5` |
| **Phase 7.2.3.6 Commit** | `3e41420ee054b0932140ba9e10a7ccc728a3a22c` |
| **Commit Message** | `fix(compliance): enforce strict manuscript trust boundary and independent verification requirement (Phase 7.2.3.6)` |
| **Working Directory Status** | Clean (tracked repository files clean; uncommitted test artifacts discarded) |

### Modified Files Diff Summary

```
 backend/app/compliance/evaluator.py                |   12 +-
 backend/app/extraction/pipeline.py                 |    1 +
 backend/app/schemas/insurance.py                   |    6 +-
 backend/tests/test_compliance_evaluator.py         |    1 +
 backend/tests/test_phase6_hardening.py             |    4 +
 backend/tests/test_phase_7_2_3_safeguards.py       |  393 +++++++++++++++++++++
 backend/tests/test_pipeline_compliance_e2e.py      |   21 +-
 backend/evaluation/reports/generated/phase_7_2_3_6_cohort_run.json | 5105 ++++++++++++++++++++
 backend/evaluation/reports/generated/phase_7_2_3_6_cohort_run.md  |  136 +
 9 files changed, 5670 insertions(+), 9 deletions(-)
```

---

## 3. Vulnerability Analysis & Technical Resolution

### The Vulnerability
Under Phase 7.2.3.5, `evaluator.py` evaluated negative manuscript absence via:
```python
# Phase 7.2.3.5 logic
unsupported_negative_ends = [
    e for e in valid_endorsements
    if e.manuscript_status == "NOT_DETECTED"
    and not (
        e.manuscript_verification_basis == "unmodified_standard_form"
        and is_recognized_standard_form(e.form_number)
    )
]
```
Because `e.manuscript_verification_basis` was populated by the extraction provider (`RawEndorsement.manuscript_verification_basis`), the evaluator relied on the untrusted AI provider's claim that a form was unmodified. In practice, adversarial or hallucinated extraction could claim `unmodified_standard_form` on a modified ISO form, allowing custom restrictive endorsements to pass compliance unchecked.

### Technical Resolution
The trust boundary was restored by moving the integrity verification requirement completely outside the reach of the extraction provider:

1. **`backend/app/schemas/insurance.py`**:
   ```python
   class EndorsementExtraction(BaseModel):
       # ...
       manuscript_verification_basis: Optional[str] = Field(
           default=None,
           description="Provider-claimed verification basis (untrusted metadata for auditability)",
       )
       independent_integrity_verified: bool = Field(
           default=False,
           description="True ONLY if an independent, trusted document-integrity verification mechanism has verified that the physical document is an authentic, unmodified standard form",
       )
   ```

2. **`backend/app/extraction/pipeline.py`**:
   In `process_inspection()`, all normalized endorsements explicitly set:
   ```python
   independent_integrity_verified=False
   ```
   AI extraction providers are strictly untrusted for negative verification of absence.

3. **`backend/app/compliance/evaluator.py`**:
   Check 8b was hardened:
   ```python
   # 8b. Multi-endorsement check: Negative manuscript classification ('NOT_DETECTED') requires independent verification
   # Provider assertions (e.g. 'unmodified_standard_form' or 'full_text_inspection') cannot independently prove absence.
   # In the absence of an independent document-integrity verification mechanism, conservative fallback is REQUIRES_HUMAN_REVIEW.
   unsupported_negative_ends = [
       e for e in valid_endorsements
       if e.manuscript_status == "NOT_DETECTED"
       and not (
           getattr(e, "independent_integrity_verified", False) is True
           and is_recognized_standard_form(e.form_number)
       )
   ]
   if unsupported_negative_ends:
       primary = unsupported_negative_ends[0]
       return ComplianceFinding(
           requirement_id=req.requirement_id,
           rule_type=req.rule_type,
           status=ComplianceStatus.REQUIRES_HUMAN_REVIEW,
           severity=Severity.MAJOR,
           reason_code="MANUSCRIPT_VERIFICATION_UNSUPPORTED",
           explanation=(
               f"{req.required_endorsement_type.value} endorsement form "
               f"({primary.form_number or 'unspecified'}) has a negative manuscript claim ('NOT_DETECTED') "
               "that lacks a defensible verification basis or verified standard form provenance. Human review required."
           ),
           requires_human_review=True,
           required_value={"required_endorsement": req.required_endorsement_type.value},
           found_value={
               "form_numbers": [e.form_number for e in valid_endorsements if e.form_number],
               "manuscript_verification_basis": getattr(primary, "manuscript_verification_basis", None),
           },
           evidence=evidence_list,
       )
   ```

---

## 4. Independent Document-Integrity Architecture

### Trust Boundary Principle
- **What the AI provider does:** Extracts text, extracts form numbers, extracts named entities, quotes source text, and flags potential manuscript language if detected.
- **What the AI provider cannot do:** Certify that a physical document has not been altered, edited, or modified relative to an authoritative standard form.
- **Provider Claims as Untrusted Metadata:** Fields such as `manuscript_verification_basis` provided by the AI provider are retained strictly for audit logging and diagnostic observation, but cannot satisfy compliance rules.

### Conservative Fallback Behavior
In the current system, where an automated independent pixel/text diffing engine against ISO master forms is not yet deployed:
- Any negative manuscript claim (`NOT_DETECTED`) from the provider routes to `REQUIRES_HUMAN_REVIEW` with reason code `MANUSCRIPT_VERIFICATION_UNSUPPORTED`.
- A human compliance officer or an authoritative document diffing service must verify authenticity before `independent_integrity_verified` can be asserted.

### Blueprint for Future Independent Verification Engine
When implemented in a future phase, a valid independent verification mechanism must satisfy:
1. **Authoritative Standard Library:** Cryptographic hashes or canonical token trees of unmodified ISO standard forms (`CG 20 10 04 13`, `CG 20 37 04 13`, etc.).
2. **Template Differ:** Deterministic, pixel-level or token-level alignment between the submitted document and the official canonical template.
3. **Tamper Detection:** Explicit detection of text insertions, whiteouts, deletions, or margin modifications.
4. **Isolated Execution:** Runs completely independent of the LLM extraction provider.

---

## 5. Comprehensive Test Results

### Full Regression Suite
- **Execution Command:** `python -m unittest discover -s tests -p "test_*.py"` (executed inside `backend`)
- **Total Tests:** 251
- **Passed:** 251
- **Failures:** 0
- **Errors:** 0
- **Execution Time:** 6.104 seconds

### 10 Adversarial Regression Test Cases (`Phase7236TrustBoundaryRemediationTests`)

| Case | Scenario Tested | Input Conditions | Expected Status | Reason Code | Result |
|---|---|---|---|---|---|
| **Adv-01** | Provider claims unmodified standard form | Recognized `CG 20 10 04 13`, provider `NOT_DETECTED`, `unmodified_standard_form`, unverified integrity | `REQUIRES_HUMAN_REVIEW` | `MANUSCRIPT_VERIFICATION_UNSUPPORTED` | **PASS** |
| **Adv-02** | Provider claims full text inspection | Recognized `CG 20 10 04 13`, provider `NOT_DETECTED`, `full_text_inspection`, unverified integrity | `REQUIRES_HUMAN_REVIEW` | `MANUSCRIPT_VERIFICATION_UNSUPPORTED` | **PASS** |
| **Adv-03** | Provider claims no verification basis | Recognized `CG 20 10 04 13`, provider `NOT_DETECTED`, `basis=None`, unverified integrity | `REQUIRES_HUMAN_REVIEW` | `MANUSCRIPT_VERIFICATION_UNSUPPORTED` | **PASS** |
| **Adv-04** | Document text contains restriction | Recognized `CG 20 10 04 13`, provider claims clean, doc text has residential exclusion | `REQUIRES_HUMAN_REVIEW` | `MANUSCRIPT_RESTRICTION_DETECTED` | **PASS** |
| **Adv-05** | Fabricated edition / form variation | `CG 20 10 99 99` (fabricated edition date), unverified integrity | `REQUIRES_HUMAN_REVIEW` | `MANUSCRIPT_VERIFICATION_UNSUPPORTED` | **PASS** |
| **Adv-06** | Non-standard proprietary form | `MANUSCRIPT-PROPRIETARY-2026`, provider claims `NOT_DETECTED` | `REQUIRES_HUMAN_REVIEW` | `NON_STANDARD_ENDORSEMENT_FORM` / `MANUSCRIPT_VERIFICATION_UNSUPPORTED` | **PASS** |
| **Adv-07** | Missing form number | `form_number=None`, provider claims `NOT_DETECTED` | `REQUIRES_HUMAN_REVIEW` | `MANUSCRIPT_VERIFICATION_UNSUPPORTED` / `ENDORSEMENT_FORM_UNCERTAIN` | **PASS** |
| **Adv-08** | Uncertain manuscript classification | `manuscript_status="UNCERTAIN"` | `REQUIRES_HUMAN_REVIEW` | `ENDORSEMENT_FORM_UNCERTAIN` | **PASS** |
| **Adv-09** | Restrictive clause outside provider quote | Multipage doc, standard quote on page 1, residential exclusion on page 2 | `REQUIRES_HUMAN_REVIEW` | `MANUSCRIPT_RESTRICTION_DETECTED` | **PASS** |
| **Adv-10** | Clean form without independent integrity | Clean `CG 20 10 04 13`, `confidence=0.99`, valid quote, `independent_integrity_verified=False` | `REQUIRES_HUMAN_REVIEW` | `MANUSCRIPT_VERIFICATION_UNSUPPORTED` | **PASS** |
| **Adv-11** | Clean form WITH independent integrity | Clean `CG 20 10 04 13`, `confidence=0.99`, valid quote, `independent_integrity_verified=True` | `COMPLIANT` | `ENDORSEMENT_VERIFIED` | **PASS** |

---

## 6. 40-Scenario Cohort Benchmark Analysis

The 40-scenario frozen synthetic cohort was evaluated using the offline mock provider (`scenario_runner.py`).

### Key Metrics Summary

| Metric | Phase 7.2.3.5 | Phase 7.2.3.6 | Delta / Analysis |
|---|---|---|---|
| **Total Scenarios** | 40 | 40 | Identical (frozen cohort) |
| **Scenarios Successful** | 40 (100%) | 40 (100%) | 0 failures |
| **Submission Status Accuracy** | 77.5% (31/40) | 52.5% (21/40) | -25.0% (Explained below) |
| **Macro F1** | 0.7770 | 0.4636 | Safe shift to human review |
| **Weighted F1** | 0.7570 | 0.4563 | Safe shift to human review |
| **Finding-Level Accuracy** | 90.4% (253/280) | 88.2% (247/280) | 247 exact status matches |
| **Reason Code Accuracy** | 77.5% (217/280) | 75.4% (211/280) | 211 reason code matches |
| **False Compliance Rate (FCR)** | **0.00% (0/10)** | **0.00% (0/10)** | **Zero false-positive compliance** |
| **Unsafe Compliance Rate (UCR)** | **0.00% (0/30)** | **0.00% (0/30)** | **Zero unsafe compliance decisions** |

### Root Cause of Accuracy Shift (SCEN-01..10)
In the benchmark cohort, scenarios SCEN-01 through SCEN-10 represent submissions whose ground-truth label in the synthetic cohort is `compliant`.
- In Phase 7.2.3.5, the mock provider returned `manuscript_status="NOT_DETECTED"` and `manuscript_verification_basis="unmodified_standard_form"`. The evaluator accepted this AI provider claim, allowing endorsement presence to evaluate to `COMPLIANT`.
- In Phase 7.2.3.6, with the manuscript trust boundary closed, the AI provider's claim cannot authorize compliance. In the absence of an independent document-integrity mechanism, the endorsement finding safely falls back to `REQUIRES_HUMAN_REVIEW` (`MANUSCRIPT_VERIFICATION_UNSUPPORTED`).
- Consequently, SCEN-01..10 evaluate to `requires_human_review` instead of `compliant`.

**Why this is safe and desirable:**
1. SubGuard AI's primary safety directive is that **AI extraction providers must never independently grant compliance without verification**.
2. **FCR is 0.00% (0/10)** and **UCR is 0.00% (0/30)**. Not a single non-compliant, deficient, or restrictive submission received a false pass.
3. Every scenario requiring human review or unable to determine was prevented from receiving an automated pass.

### Confusion Matrices

#### 4-Class Submission Status Confusion Matrix
```
                          Predicted
Actual         COMPLIANT  NON_COMPLIANT  REQUIRES_REVIEW  UNABLE_TO_DET
COMPLIANT              0              0               10              0
NON_COMPLIANT          0              7                0              3
REQUIRES_REVIEW        0              2                9              4
UNABLE_TO_DET          0              0                0              5
```

#### Per-Class Metrics
- **Non-Compliant:** Precision: 0.7778, Recall: 0.7000, F1: 0.7368 (Support: 10)
- **Requires Human Review:** Precision: 0.4737, Recall: 0.6000, F1: 0.5294 (Support: 15)
- **Unable to Determine:** Precision: 0.4167, Recall: 1.0000, F1: 0.5882 (Support: 5)
- **Compliant:** Precision: 0.0000, Recall: 0.0000, F1: 0.0000 (Support: 10)

---

## 7. Verification Against Architectural Principles

| Architectural Principle | Implementation Evidence | Status |
|---|---|---|
| **AI extracts information** | AI provider populates `RawInsuranceExtraction` and metadata. Provider cannot set `independent_integrity_verified`. | **VERIFIED** |
| **Deterministic Python rules evaluate it** | `ComplianceEvaluator` enforces deterministic checks in pure Python with zero probabilistic inference. Check 8b strictly gates negative manuscript claims. | **VERIFIED** |
| **Humans resolve uncertainty** | Any unverified negative manuscript claim routes to `REQUIRES_HUMAN_REVIEW` (`MANUSCRIPT_VERIFICATION_UNSUPPORTED`). | **VERIFIED** |
| **Zero False Compliance** | FCR = 0.00% (0/10) and UCR = 0.00% (0/30). | **VERIFIED** |
| **Zero Live API Calls** | Execution performed 100% offline using synthetic PDFs and `MockExtractionProvider`. | **VERIFIED** |

---

## 8. Phase 7.3 Readiness Assessment

SubGuard AI is **READY** to proceed to Phase 7.3 (Live Extraction Providers).

### Preconditions Established
1. **Strict Defense Against Untrusted LLM Outputs:** Live LLMs (Gemini, Claude, GPT-4, etc.) can hallucinate, omit, or misclassify manuscript clauses. Because provider assertions can no longer grant compliance for endorsements without independent integrity verification, live provider hallucinations cannot cause false compliance passes.
2. **Stable Deterministic Evaluator:** 251 unit and adversarial tests verify all 18 compliance categories, edge cases, date arithmetic, limit comparisons, and entity matching.
3. **Frozen Benchmark Cohort Intact:** The 40-scenario synthetic cohort manifest (`5e33540a32609019d8fdef1c07f88c27ca811da9edbc56f8927f46136b378a60`) and all PDFs remain unaltered and verified.

### Recommendations for Phase 7.3
1. Connect live providers via `OpenRouterExtractionProvider` and `GeminiExtractionProvider` within controlled test harnesses.
2. Maintain `independent_integrity_verified=False` as the default for all live LLM provider responses.
3. Validate prompt engineering for live providers to maximize verbatim quote fidelity and minimize latency while relying entirely on Python evaluator rules for safety.

---

## 9. Audit Signature & Date

- **Project:** SubGuard AI  
- **Phase:** 7.2.3.6 — Final Manuscript Trust-Boundary Closure  
- **Date:** November 5, 2026  
- **Auditor:** SubGuard AI Engineering Team  
- **Head Commit:** `3e41420ee054b0932140ba9e10a7ccc728a3a22c`  
- **Final Verdict:** **PASS (READY FOR PHASE 7.3)**
