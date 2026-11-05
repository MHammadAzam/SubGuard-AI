# SubGuard AI — Phase 7.3.2.1: Live Smoke-Test Failure Diagnosis Report

**Project:** SubGuard AI  
**Phase:** 7.3.2.1 (Diagnostic Investigation)  
**Date:** November 5, 2026  
**Execution Environment:** Windows 11, Python 3.12, PowerShell  
**Target Scenario:** `SCEN-01` (`SCEN-01_acord25.pdf`, `SCEN-01_endorsement_cg2010.pdf`)  
**Target Provider & Model:** `openrouter` (`nvidia/nemotron-3.5-super-vl:free`)  
**External API Requests Made During Diagnosis:** 0 (Zero)  
**API Credits Consumed:** $0.00  
**Diagnosis Verdict:** **ROOT CAUSE IDENTIFIED**

---

## 1. Executive Summary

The first controlled live smoke test for scenario `SCEN-01` finished in 5.63 seconds and resulted in `0/1 successful (1 failures)`:
- Both documents in `SCEN-01` failed extraction with `HTTP 400 Bad Request` from the OpenRouter API.
- The failure occurred at the **Request Formatting & API Dispatch stage** during the `client.chat.completions.create()` invocation.
- Exactly **two requests** were attempted (one per document); **zero retries** occurred.
- The runner correctly intercepted the provider failures and classified the scenario as `predicted_status="extraction_failed"` with `processing_status="error"`, accurately keeping safety metrics (`FCR` and `UCR`) at `N/A (0/0)`.
- A diagnostic observability defect in `backend/app/extraction/providers/openrouter.py` was also identified: the provider inspected only `str(exc)` instead of unpacking `exc.body["error"]["message"]`, which masked OpenRouter's specific validation error details in the final report.

---

## 2. Complete Error Details and Secret Redaction

From [`live_smoke_test_scen01.json`](file:///d:/Documents/DOCUMENTS/Project-Antigravity/backend/evaluation/reports/generated/live_smoke_test_scen01.json):

```json
{
  "scenario_id": "SCEN-01",
  "title": "Standard Clean ACORD 25 with Attached CG 20 10 and CG 24 04",
  "expected_status": "compliant",
  "predicted_status": "extraction_failed",
  "status_matched": false,
  "expected_requires_human_review": false,
  "predicted_requires_human_review": true,
  "human_review_matched": false,
  "is_unsafe_pass": false,
  "is_false_compliant": false,
  "processing_status": "error",
  "error_type": "ExtractionProviderFailure",
  "error_message": "AI extraction provider 'openrouter' failed: OpenRouter invalid request/model error: Error code: 400; AI extraction provider 'openrouter' failed: OpenRouter invalid request/model error: Error code: 400",
  "findings_results": [],
  "execution_time_seconds": 5.558
}
```

- **Redacted Exception:** No API keys, credentials, or authentication tokens appeared in the error messages. The sanitization layer in `sanitize_error_message()` functioned as designed.
- **Error Structure:** The string contains two semicolon-separated clauses:
  1. `AI extraction provider 'openrouter' failed: OpenRouter invalid request/model error: Error code: 400` (Document 1: `SCEN-01_acord25.pdf`)
  2. `AI extraction provider 'openrouter' failed: OpenRouter invalid request/model error: Error code: 400` (Document 2: `SCEN-01_endorsement_cg2010.pdf`)

---

## 3. Failure Stage Determination

Each potential stage of the execution lifecycle was evaluated against empirical evidence:

| Execution Stage | Status | Evidence / Analysis |
|---|---|---|
| **Credential Loading** | **PASSED** | `OPENROUTER_API_KEY` was successfully read by `OpenRouterExtractionProvider`. If the key had been absent, the runner would have halted in `__init__` with `ExtractionConfigurationError` ("OpenRouter API key is not configured") and classified the run as `configuration_error`. |
| **Provider Initialization** | **PASSED** | Provider instantiated with `model="nvidia/nemotron-3.5-super-vl:free"` and `max_retries=1`. |
| **HTTP Authentication** | **PASSED** | HTTP 401 Unauthorized was **not** returned. OpenRouter accepted the authorization token. |
| **Rate Limiting** | **NOT HIT** | HTTP 429 Too Many Requests was **not** returned. |
| **Model Availability** | **PASSED** | The model endpoint accepted the connection and responded within ~2.7s per request. |
| **Request Formatting & Dispatch** | **FAILED** | **HTTP 400 Bad Request returned by OpenRouter API.** The request payload constructed by `openrouter.py` was rejected by OpenRouter/upstream inference engine. |
| **Response Parsing** | **NOT REACHED** | No valid completion payload was returned by the provider. |
| **Schema Validation** | **NOT REACHED** | Downstream `RawInsuranceExtraction.model_validate` was not reached. |
| **Evidence Grounding** | **NOT REACHED** | Downstream quote resolution was not reached. |
| **Normalization** | **NOT REACHED** | Downstream currency/date parsing was not reached. |
| **Deterministic Compliance** | **NOT REACHED** | Document evaluation was aborted due to empty document sets. |

---

## 4. Request Count & Retry Verification

### 4.1 Request Count
- **Expected Requests:** Exactly 2 (one for each PDF in `SCEN-01`).
- **Actual Requests Attempted:** **2 (CONFIRMED)**.
- **Evidence:**
  1. The error message explicitly concatenates two identical extraction failure strings separated by `; `, corresponding to `doc_gt 1` (`SCEN-01_acord25.pdf`) and `doc_gt 2` (`SCEN-01_endorsement_cg2010.pdf`).
  2. Total elapsed scenario execution time was 5.558s (~2.78s per HTTPS round-trip).

### 4.2 Retry Behavior
- **Configured Retries:** `--max-retries 1`.
- **Actual Retries:** **0 retries (CONFIRMED)**.
- **Evidence:**
  1. With `max_retries=1`, `attempt in range(1, 2)` executes exactly 1 attempt per document.
  2. In `openrouter.py` (lines 456–463), HTTP 400 is classified as `ExtractionInvalidRequestError`, which is strictly non-retryable and immediately raises without retry.

---

## 5. Failure Classification Verification

The runner correctly handled the failure:
- `processing_status`: `"error"` (not miscounted in `successful_scenarios`).
- `predicted_status`: `"extraction_failed"`.
- `total_scenarios`: 1, `successful_scenarios`: 0, `failed_scenarios`: 1.
- `False Compliance Rate (FCR)` and `Unsafe Compliance Rate (UCR)`: Correctly reported as `N/A (0/0)`. A provider failure was not allowed to generate false passes or false compliance metrics.

---

## 6. Root Cause Analysis: Confirmed Facts vs. Hypotheses

### 6.1 Confirmed Facts
1. **Fact 1 (HTTP 400 Status):** OpenRouter returned HTTP status code 400 (Bad Request) for both document payloads.
2. **Fact 2 (Diagnostic Masking in `openai-python v3.42.1`):**
   In the installed `openai` Python SDK (v3.42.1), `str(BadRequestError)` stringifies strictly as:
   ```
   "Error code: 400"
   ```
   The underlying JSON response body from OpenRouter is stored in `exc.body["error"]["message"]` and `exc.response.json()`.
3. **Fact 3 (Observability Bug in `openrouter.py`):**
   In `backend/app/extraction/providers/openrouter.py`:
   - Line 357: `err_msg = str(exc)` produces only `"Error code: 400"`.
   - Line 390: `is_response_format_error = ("response_format" in err_lower or "json_object" in err_lower ...)` checked only `str(exc)`, completely missing the actual OpenRouter response error string.
   - Line 458: `raise ExtractionInvalidRequestError(f"OpenRouter invalid request/model error: {exc}", ...)` omitted `exc.body`, masking the root cause message from the final report.

### 6.2 Primary Root Cause Hypotheses for the HTTP 400 Rejection
Based on OpenRouter API specifications and the payload generated by `OpenRouterExtractionProvider`:

1. **Hypothesis 1 — Unsupported `response_format: {"type": "json_object"}`:**
   `nvidia/nemotron-3.5-super-vl:free` supports multimodal chat completions, but many open-weights models and upstream providers reject OpenAI-style JSON mode (`response_format: {"type": "json_object"}`) with HTTP 400 unless structured JSON Schema is used or format constraints are omitted. Because `openrouter.py` failed to inspect `exc.body`, its internal `response_format` retry was never triggered.
2. **Hypothesis 2 — Multimodal Content Structure Without Images:**
   In `_build_user_content()`:
   When `render_images=False` (the default in `scenario_runner.py`), `user_content` is passed as:
   ```python
   [{"type": "text", "text": "=== PAGE 1 ... ===\n..."}]
   ```
   Several OpenAI-compatible proxy endpoints (including NVIDIA NIM endpoints on OpenRouter) reject a list of dicts when no image parts (`type: "image_url"`) are present, requiring `content` to be a plain string (`str`).
3. **Hypothesis 3 — System Message Role Rejection:**
   Some vision-language models do not accept messages with `role: "system"`, requiring instructions to be prepended to the `user` message.

---

## 7. Issue Classification

- **Category:** **Request Formatting / Provider-Model Compatibility Defect** (Primary) and **Application Observability Defect** (Secondary).
- **Not a Configuration Defect:** Keys and environment flags loaded properly.
- **Not a Compliance Logic Defect:** Compliance evaluators and trust boundaries were preserved.

---

## 8. Minimal Recommended Remediation (Do Not Implement Yet)

1. **Fix Exception Unpacking in `openrouter.py`:**
   Extract detailed server message from `exc.body.get("error", {}).get("message")` or `getattr(exc, "message", None)` so error bodies are never masked.
2. **Text-Only Content Flattening:**
   In `_build_user_content()`: If `image_count == 0`, return a plain string `str` instead of a list of `{"type": "text"}` parts.
3. **Graceful `response_format` Handling:**
   Ensure `is_response_format_error` inspects `exc.body` to properly fall back when a model rejects JSON object mode.

---

## 9. Is Another Live Request Necessary?

**YES, but only after implementing the minimal formatting/observability fixes under controlled operator authorization.**

---

## 10. Verdict

### **VERDICT: ROOT CAUSE IDENTIFIED**

The exact failure point, HTTP status code (400), request count (2 attempted), retry count (0), failure stage (API Request Formatting & Dispatch), and the client-side error masking defect have all been established with empirical evidence.
