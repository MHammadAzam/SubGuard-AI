# SubGuard AI — Phase 7.3.1: Closure Verification and Live Smoke-Test Readiness Report

**Project:** SubGuard AI  
**Phase:** 7.3.1  
**Date:** November 5, 2026  
**Execution Environment:** Windows 11, Python 3.12, PowerShell  
**Repository Path:** `D:\Documents\DOCUMENTS\SubGuard-AI`  
**Evaluation Mode:** Offline / Safety Audit / Controlled Smoke-Test Readiness  
**External API Calls Made:** 0 (Zero)  
**Overall Verdict:** **PASS** (Phase 7.3.1 Closure & Guardrail Verification) / **READY FOR LIVE SMOKE TEST** (Pending API Key Provision)

---

## 1. Actual Repository and Git Baseline

An independent inspection of the local Git repository was conducted prior to executing verification steps:

- **Local Repository Root:** `D:\Documents\DOCUMENTS\SubGuard-AI`
- **Current Branch:** `main`
- **HEAD Commit Hash:** `54a953098e0835532ddc31e2d26170048ac3c1cf`
- **Commit Subject:** `feat(extraction): integrate live provider CLI runner, reliability handling and Phase 7.3 security tests`
- **Ancestor Verification:** `git merge-base --is-ancestor 54a9530 HEAD` returned exit code 0 (`True`). Commit `54a9530` is confirmed to be the current HEAD.
- **Phase 7.3 Report Location:** `D:\Documents\DOCUMENTS\SubGuard-AI\PHASE_7_3_LIVE_EXTRACTION_INTEGRATION_REPORT.md`
- **Phase 7.3 Benchmark Artifacts:**
  - `backend/evaluation/reports/generated/phase_7_3_mock_cohort_run.json`
  - `backend/evaluation/reports/generated/phase_7_3_mock_cohort_run.md`
- **Working Tree State:** Clean with respect to tracked files, except for the controlled fix applied during this audit to `backend/evaluation/runners/scenario_runner.py`. Two pre-existing untracked files from Phase 7.2.3.5 remain preserved (`phase_7_2_3_5_cohort_run.json`, `phase_7_2_3_5_cohort_run.md`).

---

## 2. Test-Suite Execution and Regression Verification

The full project regression test suite was executed in the backend virtual environment:

```powershell
cd backend
python -m unittest discover -s tests -p "test_*.py"
```

### Test Execution Summary
- **Total Tests Ran:** 273
- **Failures:** 0
- **Errors:** 0
- **Skipped:** 0
- **Execution Time:** 6.961 seconds
- **Result:** **OK**

### Key Subsystems Verified
1. **Manuscript Trust Boundary:** Strict verification requirements (`test_compliance_evaluator.py`, `test_phase_7_2_3_6_manuscript_trust.py`) enforce that unverified negative manuscript classifications or provider-asserted "unmodified" claims fail safe to `requires_human_review`.
2. **Effective-Date & Expiration Safeguards:** Non-standard dates, missing dates, and future policy inception dates are strictly evaluated against policy enforcement rules.
3. **OpenRouter Provider & Guardrails:** Constructor validation, model string checks, payload schema structuring, and error handling pass all unit tests without invoking network endpoints (`test_openrouter_provider.py`).
4. **Scenario Runner & Reporting:** Multi-document benchmark execution and metrics calculations remain fully functional (`test_scenario_runner.py`).

---

## 3. Missing-Key Classification Audit and Remediation

### 3.1 Initial Vulnerability Identified
In Phase 7.3, when `OpenRouterExtractionProvider` was invoked without `OPENROUTER_API_KEY` configured in the environment, the provider correctly threw a `ValueError("OpenRouter API key is not configured. Set the OPENROUTER_API_KEY environment variable.")`. The extraction pipeline caught this exception and recorded it in `pipeline_res.errors`, resulting in `pipeline_res.document_set = None`.

However, in `backend/evaluation/runners/scenario_runner.py`, the runner proceeded to pass an empty document set to `ComplianceEvaluator.evaluate_submission()`. The deterministic compliance evaluator evaluated the empty document set against contractual requirements, producing `unable_to_determine` for all rules and an overall `requires_human_review`. Because no unhandled Python exception was raised, `ScenarioEvaluationResult` was instantiated with `processing_status="success"`. This created a reporting flaw:
- The benchmark runner reported: `Scenarios: 1/1 successful (0 failures)`.
- It inaccurately implied that the scenario was successfully processed through the live AI extraction pipeline when in reality extraction had failed at the credential check.

### 3.2 Engineering Remediation Applied
In `backend/evaluation/runners/scenario_runner.py` (lines 520–547), explicit handling was introduced for scenarios where all documents fail extraction due to provider or pipeline errors:

```python
# If all documents in the scenario failed extraction due to pipeline/provider errors,
# fail the scenario explicitly rather than evaluating compliance on empty documents.
if not doc_sets and pipeline_errors:
    elapsed = round(time.time() - start_time, 3)
    return ScenarioEvaluationResult(
        scenario_id=scen_id,
        title=scenario.title,
        expected_status=scenario.expected_overall_status.value,
        predicted_status="configuration_error" if any("key" in e.lower() or "configuration" in e.lower() for e in pipeline_errors) else "extraction_failed",
        status_matched=False,
        expected_requires_human_review=scenario.expected_requires_human_review,
        predicted_requires_human_review=True,
        human_review_matched=False,
        is_unsafe_pass=False,
        is_false_compliant=False,
        processing_status="error",
        error_type="ExtractionProviderFailure",
        error_message="; ".join(pipeline_errors),
        execution_time_seconds=elapsed,
    )
```

### 3.3 Verification of Correct Classification
The scenario runner was re-tested against scenario `SCEN-01` with `OPENROUTER_API_KEY` absent:

```
Executing multi-document benchmark on 1 scenarios...

Benchmark completed in 0.13s:
  - Scenarios: 0/1 successful (1 failures)
  - Submission Status Accuracy: 0.0% (Macro F1: 0.0000)
  - Finding-Level Accuracy: 0.0% (Reason Code Accuracy: 0.0%)
  - False Compliance Rate (FCR): N/A (0/0)
  - Unsafe Compliance Rate (UCR): N/A (0/0)
```

The resulting benchmark JSON artifact confirmed:
- `total_scenarios`: 1
- `successful_scenarios`: 0
- `failed_scenarios`: 1
- `processing_status`: `"error"`
- `predicted_status`: `"configuration_error"`
- `error_type`: `"ExtractionProviderFailure"`
- `error_message`: `"AI extraction provider 'openrouter' failed: OpenRouter API key is not configured. Set the OPENROUTER_API_KEY environment variable."`

Zero successful scenarios are claimed when credentials are absent, guaranteeing full reporting integrity.

---

## 4. Free-Model Guard Audit and Technical Limitations

### 4.1 Guard Implementation Review
The guard logic in `backend/app/extraction/providers/openrouter.py` was audited:

```python
def is_free_model(model: Optional[str]) -> bool:
    """Return True if model explicitly targets an OpenRouter free endpoint."""
    if not model:
        return False
    return model.strip().lower().endswith(":free")
```

Constructor enforcement in `OpenRouterExtractionProvider.__init__()`:
```python
if self.require_free_tier and not is_free_model(self.model):
    raise ValueError(
        f"Model '{self.model}' is not a free-tier model. "
        "Only models ending in ':free' are permitted when require_free_tier=True. "
        "Set require_free_tier=False or choose an eligible free model."
    )
```

### 4.2 Guardrail Capabilities Verified
1. **Network-Free Rejection:** The model guard executes in `__init__` before any network session is opened, request prepared, or payload dispatched.
2. **Auto-Routing Prohibition:** Auto-routing identifiers such as `openrouter/auto`, `auto`, or `default` do not end in `:free` and are unconditionally rejected when `require_free_tier=True`.
3. **Explicit Opt-In Required for Paid Models:** Any model lacking the `:free` suffix causes an immediate `ValueError` unless the operator explicitly sets `require_free_tier=False`.

### 4.3 Technical Limitations and Operational Risk
- **Static Suffix Reliance:** The check relies strictly on string suffix convention (`:free`). It does not dynamically query the OpenRouter models catalog (`GET https://openrouter.ai/api/v1/models`) to inspect the actual real-time pricing fields (`pricing.prompt == 0` and `pricing.completion == 0`).
- **Upstream Catalog Changes:** If OpenRouter renames a free model, changes its pricing tier without removing the `:free` tag, or redirects a deprecated slug to a paid fallback, the static suffix check cannot detect the change offline.
- **Recommended Future Enhancement:** Before deploying unattended large-cohort live runs, implement an optional online catalog verification step in `OpenRouterExtractionProvider` that fetches model metadata and confirms `pricing.prompt == "0"` and `pricing.completion == "0"`.

---

## 5. Controlled Live Smoke-Test Preparation

### 5.1 Test Specification
- **Target Fixture:** `SCEN-01` (`backend/evaluation/fixtures/scenarios/SCEN-01`)
  - Synthetic documents: ACORD 25 Certificate of Insurance + Attached Endorsements (CG 20 10, CG 24 04).
- **Target Model:** `nvidia/nemotron-3.5-super-vl:free` (Primary) or `google/gemini-2.0-flash-exp:free` (Alternative).
- **Enforcement Mode:** `require_free_tier=True`.

### 5.2 Prepared Command
For execution by an authorized operator once `OPENROUTER_API_KEY` is provisioned:

```powershell
cd D:\Documents\DOCUMENTS\SubGuard-AI\backend
$env:PYTHONPATH = "."
$env:OPENROUTER_API_KEY = "<YOUR_OPENROUTER_API_KEY>"
python evaluation/runners/scenario_runner.py --provider openrouter --model nvidia/nemotron-3.5-super-vl:free --scenario SCEN-01 --report-prefix live_smoke_test_nemotron
```

### 5.3 Smoke-Test Execution Status
- **Execution Status:** **SKIPPED (SAFELY BLOCKED)**
- **Reason:** `OPENROUTER_API_KEY` is not set in the local environment.
- **Safety Policy Compliance:** In accordance with Phase 7.3.1 Section 5 instructions ("If OPENROUTER_API_KEY is not present, do not call the API... record that live execution was safely skipped"), zero external network calls were initiated. The system safely aborted live calls at the pre-flight check.

---

## 6. Blockers and Next Steps for Live Benchmarking

| Status | Item | Requirement / Remediation |
|---|---|---|
| **RESOLVED** | Missing-Key Classification | `scenario_runner.py` now classifies extraction credential failures as `configuration_error` with `processing_status="error"` and 0 successful scenarios. |
| **RESOLVED** | Free-Tier Guardrail | Confirmed rejection of paid models and auto-routing prior to network calls. |
| **RESOLVED** | Regression Suite | 273/273 tests passing in 6.961s with zero errors or failures. |
| **RESOLVED** | Manuscript Trust Boundary | Evaluator strictly enforces independent verification; AI assertions of "unmodified standard form" fail safe. |
| **PENDING** | API Key Provisioning | Operator must set valid `OPENROUTER_API_KEY` in environment prior to executing live smoke test. |
| **RECOMMENDED** | Dynamic Pricing Verification | Add pre-flight query to OpenRouter models endpoint to verify zero pricing dynamically before batch runs. |

---

## 7. Sign-Off Verdict

### **VERDICT: PASS**

All Phase 7.3.1 requirements have been satisfied:
1. Repository state and baseline commit `54a9530` verified.
2. Missing-key extraction behavior hardened so unauthenticated runs are never reported as completed benchmarks.
3. Free-model guards audited and verified to reject non-free models prior to network requests.
4. Full regression test suite passed cleanly (273 tests, 0 failures, 0 errors).
5. Live smoke test command prepared with strict free-tier parameters and synthetic document targets.
6. Live execution safely skipped due to absent API credentials, honoring zero-unauthorized-call security guarantees.
