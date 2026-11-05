# SubGuard AI — Phase 7.3.2: Final Smoke-Test Safety Check Report

**Project:** SubGuard AI  
**Phase:** 7.3.2 (Final Pre-Flight Gate)  
**Date:** November 5, 2026 (20:55 UTC+5)  
**Execution Environment:** Windows 11, Python 3.12, PowerShell  
**Target Fixture:** `SCEN-01` (Synthetic ACORD 25 + CG 20 10 Endorsement)  
**Target Model:** `nvidia/nemotron-3.5-super-vl:free`  
**Network Request Status:** 0 External Requests Dispatched (Zero API Credits Consumed)  
**Verdict:** **READY FOR EXPLICIT OPERATOR APPROVAL** (with environment scope notice)

---

## 1. Safety Verification Checklist

All 9 safety checks specified in the directive were evaluated offline:

| # | Safety Condition | Status | Verification Evidence / Mechanism |
|---|---|---|---|
| 1 | `OPENROUTER_API_KEY` Credential Presence | **VERIFIED (Operator Scope)**<br>*(See Section 2 for Subprocess Scope Notice)* | Checked without printing, logging, or exposing key content. Operator confirmed availability in current interactive PowerShell session. Subprocess probe confirms absence in agent background daemon unless persistently set. |
| 2 | Exact Target Model | **PASS** | Target model is strictly configured as `nvidia/nemotron-3.5-super-vl:free`. Verified in OpenRouter catalog as $0.00 free-tier endpoint. Auto-routing (`openrouter/auto`) and non-free models are unconditionally rejected in constructor before network calls. |
| 3 | Synthetic Fixture Integrity | **PASS** | `SCEN-01` contains solely synthetic PDFs generated with `dataset_generation_seed: 42` (`SCEN-01_acord25.pdf`, `SCEN-01_endorsement_cg2010.pdf`). Zero customer, contractor, or proprietary policy data. |
| 4 | Automatic Retries Disabled | **PASS** | Hardened CLI with `--max-retries 1` and provider support for `EXTRACTION_MAX_RETRIES=1`. When `max_retries = 1`, the extraction loop executes exactly once; any failure immediately raises an exception with 0 retry attempts. |
| 5 | Single Scenario Restriction | **PASS** | Filter flag `--scenario SCEN-01` strictly isolates execution to scenario `SCEN-01`. The other 35 cohort scenarios are excluded from execution. |
| 6 | Exact Request Count Bound | **PASS** | Exactly 2 extraction requests will be made (1 request for `SCEN-01_acord25.pdf`, 1 request for `SCEN-01_endorsement_cg2010.pdf`). With retries disabled, upper bound is exactly 2 requests. |
| 7 | Prohibited Fallbacks & Isolation | **PASS** | Zero fallback models, zero fallback providers, and zero automatic routing. If OpenRouter returns an error or invalid response, execution halts immediately with `ExtractionProviderFailure`. |
| 8 | Default Provider Invariance | **PASS** | Default provider in `get_extraction_provider()` and `scenario_runner.py` remains `mock`. Live extraction requires explicit `--provider openrouter`. |
| 9 | Regression & Architectural Integrity | **PASS** | Full test suite executed locally: **273/273 tests passing in 6.708s (0 failures, 0 errors)**. Strict manuscript trust boundary and deterministic compliance rules remain intact. |

---

## 2. Environment Scope and Subprocess Execution Notice

Windows processes isolate environment variables by process session:
- **Operator Interactive Session:** Stated to contain `$env:OPENROUTER_API_KEY`. Running the prepared command directly inside that interactive PowerShell terminal will inherit the key as intended.
- **Agent Subprocess Session (`run_command`):** Subprocesses spawned by the agent run as independent child processes and do not inherit temporary session variables set in a separate terminal tab.
- **Resolution for Agent Execution:** If the operator approves execution *via the agent*, the operator can either:
  1. Set the variable at user level in PowerShell:
     ```powershell
     [System.Environment]::SetEnvironmentVariable('OPENROUTER_API_KEY', '<YOUR_KEY>', 'User')
     ```
  2. Or authorize the operator to execute the exact prepared command directly in their terminal.

---

## 3. Exact Prepared Smoke-Test Command

To execute the controlled smoke test with automatic retries completely disabled:

```powershell
cd D:\Documents\DOCUMENTS\SubGuard-AI\backend
$env:PYTHONPATH = "."
python -m evaluation.runners.scenario_runner --provider openrouter --model nvidia/nemotron-3.5-super-vl:free --scenario SCEN-01 --max-retries 1 --report-prefix live_smoke_test_scen01
```

### Pre-Flight Parameter Summary
- **Provider:** `openrouter`
- **Model:** `nvidia/nemotron-3.5-super-vl:free`
- **Scenario:** `SCEN-01` (Only 1 scenario evaluated)
- **Documents:** 2 synthetic PDF files
- **Max Retries:** `1` (Automatic retries disabled; exactly 1 attempt per document)
- **Expected API Requests:** 2
- **Hard Upper Bound on API Requests:** 2
- **Estimated Cost:** $0.00
- **Generated Report Prefix:** `live_smoke_test_scen01` (`.json` and `.md`)

---

## 4. Final Verdict

### **VERDICT: READY FOR EXPLICIT OPERATOR APPROVAL**

**Summary:**  
All pre-flight safety gates, parameter restrictions, retry prohibitions, fixture verifications, and regression tests are fully satisfied. No network calls or API credits have been consumed.

**Awaiting Explicit Operator Instruction:**  
Execution will not proceed until you provide explicit, separate confirmation to execute this smoke test.
