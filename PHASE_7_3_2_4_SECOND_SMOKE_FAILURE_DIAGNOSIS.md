# SubGuard AI — Phase 7.3.2.4: Second Live Smoke-Test Failure Diagnosis

**Date:** November 5, 2026  
**Phase:** 7.3.2.4 — Second Live Smoke-Test Failure Diagnosis  
**Status:** COMPLETE (READ-ONLY DIAGNOSIS)  
**Final Verdict:** `ROOT CAUSE PARTIALLY IDENTIFIED`  
**Repository:** `D:\Documents\DOCUMENTS\SubGuard-AI`  
**Git HEAD:** `12c4c413d54e24d40168392e8b3867c2c17d8ccf` (`main`)  

---

## 1. Executive Summary

Following the Phase 7.3.2.2 remediation (commit `12c4c41`), a second strictly controlled live smoke test (`live_smoke_test_scen02`) was executed against OpenRouter using the free vision model `nvidia/nemotron-3.5-super-vl:free` with `--max-retries 1`.

The run failed again in 3.03s:
- **Scenario:** `SCEN-01` (2 documents: `SCEN-01_acord25.pdf` and `SCEN-01_endorsement_cg2010.pdf`)
- **Outcome:** 0/1 scenarios successful (0/2 documents extracted)
- **Error:** `OpenRouter invalid request/model error (HTTP 400): Error code: 400` on both documents.

This diagnostic investigation was conducted **strictly offline** (zero network calls, zero API calls, zero credit consumption). It establishes:
1. **Remediation Verification:** Phase 7.3.2.2 successfully changed the text payload to a plain Python `str` (when `render_images=False`) and updated the error prefix path. However, sending text as a string **did not resolve the underlying HTTP 400 error**.
2. **Error Observability Failure:** The server error message was STILL reported as `"Error code: 400"`. Forensic code tracing revealed two systemic causes:
   - In `openrouter.py`, `extract_http_error_details()` inspects `body.get("error")` and `body.get("message")`. If OpenRouter returns an empty body (`""`), non-standard JSON (such as `{"detail": ...}` common in FastAPI/proxy gateways), or unparsed text, `details["message"]` falls back to `str(exc)`. Crucially, `extract_http_error_details()` failed to inspect `exc.response.text`.
   - In `scenario_runner.py`, lines 525–546 completely discard `pipeline_res.error_type`, `pipeline_res.status_code`, `pipeline_res.error_code`, and `pipeline_res.request_id`, hardcoding `error_type="ExtractionProviderFailure"` and preserving only `error_message="; ".join(pipeline_errors)`.
3. **HTTP 400 Suspects:** Because the raw response body was not captured, the exact upstream rejection reason remains between:
   - Model rejection of `response_format={"type": "json_object"}` (unsupported JSON mode on `nemotron-3.5-super-vl:free`).
   - Upstream parameter or header validation failure on OpenRouter.

---

## 2. Second-Run Inspection (`live_smoke_test_scen02`)

Data extracted directly from `live_smoke_test_scen02.json` and `live_smoke_test_scen02.md`:

| Attribute | Observed Value | Evidence Source |
|---|---|---|
| **Benchmark Run ID** | `live_smoke_test_scen02` | `live_smoke_test_scen02.json:2` |
| **Timestamp (UTC)** | `2026-11-05T16:40:12.192115+00:00` | `live_smoke_test_scen02.json:3` |
| **Manifest Version & Hash** | `cohort_v1.0.0` (`5e33540a32609019d8fdef1c07f88c27ca811da9edbc56f8927f46136b378a60`) | `live_smoke_test_scen02.json:4-5` |
| **Provider & Model** | `openrouter` / `nvidia/nemotron-3.5-super-vl:free` | `live_smoke_test_scen02.json:6-7` |
| **Git Commit in Report** | `12c4c413d54e24d40168392e8b3867c2c17d8ccf` | `live_smoke_test_scen02.json:203` |
| **Reported `error_type`** | `ExtractionProviderFailure` | `live_smoke_test_scen02.json:186, 195` |
| **Actual Raised Exception Class** | `ExtractionInvalidRequestError` | Instantiated in `openrouter.py:569` |
| **HTTP Status** | `400` (Bad Request) | Explicit in error string `(HTTP 400)` |
| **Error Message** | `AI extraction provider 'openrouter' failed: OpenRouter invalid request/model error (HTTP 400): Error code: 400; AI extraction provider 'openrouter' failed: OpenRouter invalid request/model error (HTTP 400): Error code: 400` | `live_smoke_test_scen02.json:187` |
| **Failure Stage** | Document extraction (Stage 1 of pipeline; before compliance evaluation) | `live_smoke_test_scen02.json:178` |
| **Documents Extracted** | 0 of 2 (both `SCEN-01_acord25.pdf` and `SCEN-01_endorsement_cg2010.pdf` failed) | Code tracing `scenario_runner.py:528` |
| **Requests Made** | Exactly 2 requests (1 per document) | Confirmed by runtime and 2 joined error messages |
| **Retry Count** | 0 retries (`--max-retries 1` halted on attempt 1) | `live_smoke_test_scen02.json:189` (2.96s total scenario time) |

---

## 3. Comparison: First Run (`scen01`) vs. Second Run (`scen02`)

| Dimension | First Smoke Test (`scen01`) | Second Smoke Test (`scen02`) | Change Assessment |
|---|---|---|---|
| **Git Commit** | `1539b8dcc8fd84ec5bf9d54095b9cd7b947f45b3` | `12c4c413d54e24d40168392e8b3867c2c17d8ccf` | Includes Phase 7.3.2.2 remediation |
| **Error String** | `...invalid request/model error: Error code: 400` | `...invalid request/model error (HTTP 400): Error code: 400` | **CONFIRMED CHANGE:** Line 567 formatting reached |
| **User Content Format** | `List[Dict[str, Any]]` array | Plain string `str` (`\n\n.join(...)`) | **CONFIRMED CHANGE:** Text-only string sent |
| **Response-Format Fallback** | Disabled (default) | Disabled (`allow_response_format_fallback=False`) | **CONFIRMED UNCHANGED:** No silent retry |
| **Detailed Body Observed** | No (`"Error code: 400"`) | No (`"Error code: 400"`) | **DEFECT PERSISTS:** Error body not unpacked |
| **Failure Stage** | Extraction (0/2 docs) | Extraction (0/2 docs) | **UNCHANGED:** Both documents rejected at HTTP layer |
| **Scenario Execution Time** | 5.56s | 2.96s | Faster due to `--max-retries 1` vs default |

---

## 4. Implementation Analysis: Error Discard & Serialization Path

Tracing the error from the OpenAI Python SDK to the benchmark report reveals two distinct breakage points:

### 4.1 Breakage Point 1: `extract_http_error_details` in `openrouter.py`
In OpenAI Python SDK (`openai` v3.42.1), `BaseClient._make_status_error_from_response` executes:
```python
err_msg = f"Error code: {response.status_code}"
...
try:
    body = json.loads(err_text)
except Exception:
    pass
else:
    error = body.get("error", body) if is_mapping(body) else None
    message = error.get("message") if is_mapping(error) else None
    if isinstance(message, str) and message:
        err_msg += f" - {message}"
return self._make_status_error(err_msg, body=body, response=response)
```
Forensic simulation demonstrates that:
1. If the response is valid JSON with `body["error"]["message"]` or `body["message"]`, `str(exc)` becomes `Error code: 400 - <message>`.
2. The fact that `str(exc)` is literally `"Error code: 400"` proves that `body` was:
   - An empty body `""` (e.g., HTTP 400 with no content).
   - A JSON structure without an `"error"` or `"message"` key (e.g. `{"detail": ...}` from FastAPI/Starlette proxy, or `{"description": ...}`).
   - An empty JSON dict `{}`.
   - Or `body` was None.
3. In `openrouter.py`, `extract_http_error_details()` did NOT check:
   - `body.get("detail")` or other dictionary values.
   - `getattr(exc, "response", None).text` (the raw HTTP response text).
   - Consequently, `details["message"]` became None, and fell back to `str(exc)` (`"Error code: 400"`).

### 4.2 Breakage Point 2: Discard of Structured Attributes in `scenario_runner.py`
In `backend/app/extraction/pipeline.py` (lines 435–446), the pipeline constructs a structured `ExtractionPipelineResult`:
```python
ExtractionPipelineResult(
    errors=[f"AI extraction provider '{...}' failed: {clean_msg}"],
    error_category="invalid_request",
    error_type="ExtractionInvalidRequestError",
    error_message=clean_msg,
    error_code="http_400",
    status_code=400,
    request_id=request_id,
)
```
However, in `backend/evaluation/runners/scenario_runner.py` (lines 525–546):
```python
if not doc_sets and pipeline_errors:
    return ScenarioEvaluationResult(
        scenario_id=scen_id,
        processing_status="error",
        error_type="ExtractionProviderFailure",  # <-- Discards pipeline_res.error_type!
        error_message="; ".join(pipeline_errors), # <-- Discards status_code, error_code, request_id!
    )
```
The scenario runner completely erases `error_type`, `error_code`, `status_code`, and `request_id`, leaving only the concatenated error message strings.

---

## 5. Separation of Facts, Inferences, and Hypotheses

### Confirmed Facts (Report & Code)
1. Both `SCEN-01` documents failed during extraction with HTTP status 400.
2. In Run 2, `openrouter.py` reached line 567 (`OpenRouter invalid request/model error (HTTP 400)`), confirming Phase 7.3.2.2 code was active.
3. Text-only content was sent as a plain Python `str` (not an array) in Run 2.
4. Sending user content as a string did not resolve the HTTP 400 error.
5. In Run 2, `is_response_format_error()` returned `False` because `details["message"]` and `details["error_param"]` were empty/None.
6. In Run 2, `allow_response_format_fallback=False` prevented any silent secondary fallback requests.
7. `scenario_runner.py` discards structured pipeline exception attributes (`error_type`, `status_code`, `error_code`, `request_id`).

### Strongly Supported Inferences
1. OpenRouter did NOT return a JSON body with `error.message`. If it had, OpenAI SDK's built-in error handler would have set `str(exc)` to `Error code: 400 - <message>`.
2. The response body was either:
   - An empty body `""` (common when proxies drop invalid requests),
   - A non-standard JSON payload (e.g. `{"detail": ...}` or `{"error": {}}`),
   - Or an unread stream.
3. Because `extract_http_error_details()` did not fall back to `getattr(exc.response, "text", None)` or inspect `body.get("detail")`, the underlying server payload was lost.

### Unverified Hypotheses
1. **Hypothesis A:** The model `nvidia/nemotron-3.5-super-vl:free` rejects `response_format={"type": "json_object"}` with HTTP 400. Many vision models and free endpoints on OpenRouter do not support JSON mode.
2. **Hypothesis B:** OpenRouter returned a `detail` or custom validation error object describing an invalid parameter (such as `response_format`, `temperature`, or an unexpected header).
3. **Hypothesis C:** The free tier model endpoint is deprecated, overloaded, or requires a different payload structure not documented in standard chat completions.

---

## 6. Remaining Defects & Smallest Safe Remediation

### Defect 1: Error Body Extraction Misses Raw Response Text and `detail`
- **Location:** `backend/app/extraction/providers/openrouter.py:extract_http_error_details`
- **Fix:** If `body` is a dict, check `body.get("detail")`, `body.get("error")`, `body.get("message")`. If still None, inspect `getattr(exc.response, "text", None)` before falling back to `str(exc)`. Always sanitize.

### Defect 2: Scenario Runner Erases Pipeline Error Attributes
- **Location:** `backend/evaluation/runners/scenario_runner.py:530-547`
- **Fix:** In `ScenarioEvaluationResult`, preserve `pipeline_res.error_type` (e.g. `ExtractionInvalidRequestError`), and include `status_code` and `error_code` if available, rather than hardcoding `ExtractionProviderFailure`.

### Defect 3: OpenRouter Model Incompatibility with `response_format`
- **Location:** Model capability / configuration.
- **Analysis:** `nvidia/nemotron-3.5-super-vl:free` may fundamentally reject `response_format={"type": "json_object"}`. If confirmed once raw error text is visible, the options are:
  - Configure the model without `response_format` (enforcing JSON extraction purely through the prompt and Pydantic validation), OR
  - Select an alternative free/low-cost model verified to support structured JSON mode on OpenRouter.

### Is Another Live Request Eventually Necessary?
**Yes.** Once the error observability is fixed (capturing `exc.response.text` and `detail`), a third controlled smoke test of `SCEN-01` with `--max-retries 1` will be necessary to see the actual provider error text or test JSON extraction without `response_format`.

---

## 7. Git State

- **Branch:** `main`
- **Commit:** `12c4c413d54e24d40168392e8b3867c2c17d8ccf`
- **Worktree:** Clean (only untracked test reports and diagnosis documents present).
- **No fixes implemented in this phase.**

---

## 8. Final Verdict

**`ROOT CAUSE PARTIALLY IDENTIFIED`**

- **What is identified:** Text-only string conversion alone does not fix the HTTP 400. Error observability failed because `extract_http_error_details()` omitted `exc.response.text` and `body.get("detail")`, and `scenario_runner.py` discarded structured pipeline metadata.
- **What remains to be established:** The exact upstream server error string returned by OpenRouter for `nvidia/nemotron-3.5-super-vl:free` (pending raw response text capture).
