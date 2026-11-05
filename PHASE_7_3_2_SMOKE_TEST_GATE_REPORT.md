# SubGuard AI — Phase 7.3.2: Live Smoke-Test Gate and Controlled Execution Report

**Project:** SubGuard AI  
**Phase:** 7.3.2  
**Date:** November 5, 2026 (19:35 UTC+5)  
**Execution Environment:** Windows 11, Python 3.12, PowerShell  
**Target Repository:** `D:\Documents\DOCUMENTS\SubGuard-AI`  
**Execution Gate Mode:** Offline Verification & Gate Audit  
**External Model Calls Made:** 0 (Zero)  
**API Credits Consumed:** $0.00  
**Gate Verdict:** **BLOCKED: key unavailable**

---

## 1. Final Git State Verification

A comprehensive inspection of the Git baseline was conducted:

| Parameter | Observed Value | Verification Status |
|---|---|---|
| **Current Branch** | `main` | Verified (`git branch --show-current`) |
| **HEAD Commit** | `1539b8dcc8fd84ec5bf9d54095b9cd7b947f45b3` | Verified (`git rev-parse HEAD`) |
| **Commit Subject** | `feat(evaluation): classify missing-key runs as configuration errors and finalize Phase 7.3.1 audit` | Verified |
| **Commit Ancestry** | `1539b8d` is confirmed to be an ancestor of HEAD | Verified (`git merge-base --is-ancestor 1539b8d HEAD` returned 0) |
| **Changes in `1539b8d`** | 2 files changed, 225 insertions (+)<br>- `PHASE_7_3_1_CLOSURE_AND_SMOKE_TEST_READINESS.md` (201 lines)<br>- `backend/evaluation/runners/scenario_runner.py` (24 lines) | Verified |
| **Phase 7.3.1 Report** | `D:\Documents\DOCUMENTS\SubGuard-AI\PHASE_7_3_1_CLOSURE_AND_SMOKE_TEST_READINESS.md` exists and accurately describes repository state | Verified |
| **Working Tree Status** | Clean tracked working tree. Pre-existing untracked files preserved: `phase_7_2_3_5_cohort_run.json`, `phase_7_2_3_5_cohort_run.md` | Verified (`git status --short`) |

No files were reset, amended, force-pushed, deleted, or overwritten.

---

## 2. Model Pricing Evidence and Live Catalog Verification

In accordance with Step 2, the current OpenRouter public model catalog was inspected directly without sending an extraction request:

- **Verification Endpoint:** `https://openrouter.ai/api/v1/models` (Public API query, zero credentials, zero credits consumed)
- **Check Timestamp:** `2026-11-05T19:35:31+05:00` (`2026-11-05T14:35:31Z`)
- **Target Model ID:** `nvidia/nemotron-3.5-super-vl:free`
- **Canonical Slug:** `nvidia/nemotron-3.5-super-vl-20261015`
- **Display Name:** `NVIDIA: Nemotron 3.5 Super VL (free)`
- **Architecture / Modalities:** `text+image+video->text` (multimodal vision-language model)
- **Context Length:** 1,000,000 tokens
- **Official OpenRouter Pricing Data:**
  - `pricing.prompt`: `"0"` ($0.00 / token)
  - `pricing.completion`: `"0"` ($0.00 / token)
- **Supported Parameters:** `response_format`, `structured_outputs`, `temperature`, `max_tokens`, `tools`

**Pricing Verification Conclusion:** The target model `nvidia/nemotron-3.5-super-vl:free` is currently verified as an active, $0.00 free-tier endpoint in OpenRouter's live catalog.

---

## 3. Credential Availability Check

In strict adherence to Step 3, the local execution environment was inspected without logging, printing, or disclosing any token value:

- **Inspected Variable:** `OPENROUTER_API_KEY`
- **Result:** **ABSENT** (`None` / empty)
- **Safety Action Taken:** No network requests were dispatched. The smoke test execution is strictly halted.
- **Privacy Compliance:** In accordance with instructions, the operator is NOT asked to paste any API key into the chat session.

---

## 4. Synthetic Fixture Confirmation (`SCEN-01`)

The fixture documents for `SCEN-01` were verified in the frozen cohort manifest (`backend/evaluation/cohorts/manifests/cohort_v1_manifest.json`) and annotations (`backend/evaluation/annotations/cohort_scenarios_v1.json`):

- **Scenario Identifier:** `SCEN-01`
- **Title:** `Standard Clean ACORD 25 with Attached CG 20 10 and CG 24 04`
- **Dataset Generation Seed:** `42`
- **Document 1:**
  - **Filename:** `SCEN-01_acord25.pdf`
  - **Relative Path:** `backend/evaluation/fixtures/SCEN-01/SCEN-01_acord25.pdf`
  - **Source Type:** `synthetic` (programmatically generated ACORD 25 certificate)
  - **SHA-256:** `a30f962532b6b025fc11765249c47651e93a946002fa775fa7f74968ea08dd11`
  - **Page Count:** 1 page
- **Document 2:**
  - **Filename:** `SCEN-01_endorsement_cg2010.pdf`
  - **Relative Path:** `backend/evaluation/fixtures/SCEN-01/SCEN-01_endorsement_cg2010.pdf`
  - **Source Type:** `synthetic` (programmatically generated ISO CG 20 10 04 13 endorsement)
  - **SHA-256:** `1a1a424ebe91e34c6afa0e76693faffdf605aae3b2a829ae4744d2ed85c09778`
  - **Page Count:** 1 page

**Synthetic Integrity:** Both documents are 100% synthetic test fixtures. They contain zero customer, contractor, or proprietary insurance policy information.

---

## 5. Prepared Smoke-Test Execution Specification & Limits

### 5.1 Exact Prepared Command
```powershell
cd D:\Documents\DOCUMENTS\SubGuard-AI\backend
$env:PYTHONPATH = "."
$env:OPENROUTER_API_KEY = "<OPERATOR_INJECTED_KEY>"
python evaluation/runners/scenario_runner.py --provider openrouter --model nvidia/nemotron-3.5-super-vl:free --scenario SCEN-01 --report-prefix live_smoke_test_scen01
```

### 5.2 Call-Limit and Execution Boundary Analysis
- **Target Scenario Count:** Exactly 1 (`SCEN-01`)
- **Document Count in Scenario:** 2 documents
- **Expected Provider Extraction Calls:** 2 model calls (1 per document)
- **Configured Timeout:** `timeout_seconds = 30.0` per request
- **Retry Policy:**
  - `max_retries`: 3 attempts maximum per document
  - Backoff: Exponential starting at 1.0s (doubling to 2.0s)
  - Retryable Conditions: Transient HTTP 429 (rate limit), 408 (timeout), 5xx server errors, or `response_format` JSON mode re-negotiation.
  - Non-retryable Conditions: HTTP 401 (invalid auth) and Schema Validation failure abort immediately without retry.
- **Maximum Possible Network Calls:** 6 calls (2 documents × 3 retries maximum in worst-case transient error scenarios)
- **Monetary Cost:** **$0.00** (Verified free endpoint)

---

## 6. Gate Status and Recommendation

### **Gate Verdict: BLOCKED: key unavailable**

### Status Summary:
1. **Git Baseline:** Clean and verified at HEAD `1539b8d`.
2. **Model Pricing:** Verified as $0.00 free tier in OpenRouter catalog at `2026-11-05T19:35:31+05:00`.
3. **Fixture Safety:** Verified 100% synthetic fixtures for `SCEN-01`.
4. **Credential Gate:** `OPENROUTER_API_KEY` is absent. Execution is blocked safely. Zero network calls were made.

### Next Step:
Execution is on hold awaiting:
1. Operator setting the `OPENROUTER_API_KEY` environment variable in the execution shell.
2. Operator's separate, explicit instruction approving the execution of the smoke test.
