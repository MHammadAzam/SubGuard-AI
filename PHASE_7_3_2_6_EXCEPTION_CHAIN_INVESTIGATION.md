# Phase 7.3.2.6 — Offline HTTP 400 Exception-Chain Investigation & Remediation Report

**Phase:** 7.3.2.6  
**Status:** COMPLETE (Strictly Offline Investigation & Remediation)  
**Date:** 2026-11-05  
**Local Project:** `D:\Documents\DOCUMENTS\SubGuard-AI`  
**Execution Context:** Offline Analysis, Architectural Audit, Exception-Chain Refactoring, and Adversarial Unit Testing (Zero Live API Requests, Zero Credit Consumption)

---

## 1. Executive Summary

During live diagnostic run 3 (`live_diagnostic_scen03.md`), the extraction pipeline failed with:
`OpenRouter invalid request/model error (HTTP 400): Error code: 400`
and the message was duplicated across scenario documents in the generated evaluation report:
`AI extraction provider 'openrouter' failed: ...; AI extraction provider 'openrouter' failed: ...`

Phase 7.3.2.6 performed a strictly offline, forensic investigation into the entire exception lifecycle from `openai._base_client` through `openrouter.py`, `pipeline.py`, and `scenario_runner.py`.

### Key Root-Cause Discoveries:
1. **`Error code: 400` String Construction in OpenAI SDK v3.42.1:**
   In the OpenAI SDK (`openai._base_client.py:BaseClient._make_status_error_from_response`), when the HTTP response body is empty (`b""` or `""`), JSON decoding raises an exception and `body` is set to `None` or `""`. The SDK initializes the exception with `err_msg = f"Error code: {response.status_code}"`. Thus, `str(exc)` literally becomes `"Error code: 400"`.
2. **Defensive Body Inspection Fallback in Phase 7.3.2.5:**
   In Phase 7.3.2.5, `extract_http_error_details()` inspected `exc.body` assuming it would be a dictionary containing an `"error"` key. When `exc.body` was `None`, empty, or a string, or when `response.text` was inaccessible (e.g. streaming responses raising `httpx.ResponseNotRead`), `extracted_msg` fell back to `str(exc)`. This propagated `"Error code: 400"` as if it were the server's error message, when in fact it was the client-side SDK's fallback string for an empty response body.
3. **Duplicate Error Prefixing:**
   The error message appeared twice in the benchmark report due to two compounding factors:
   - **Provider/Pipeline Layer:** In `pipeline.py`, `f"AI extraction provider '{prov}' failed: {clean_msg}"` prepended a provider failure prefix, even if `clean_msg` already contained a provider error prefix.
   - **Scenario Evaluation Layer:** In multi-document scenarios like `SCEN-01` (comprising a Certificate of Insurance and an Endorsement), both documents failed against the provider with identical errors. `scenario_runner.py` joined all document errors with `"; "`, repeating the full error message for each failed document in the scenario.

All defects have been remediated with defensive parsing, request ID header extraction (`cf-ray`, `x-openrouter-request-id`, `x-request-id`), exception chaining via `raise ... from exc`, prefix deduplication, document-level error deduplication, and credential redaction. All 303 backend tests pass cleanly offline.

---

## 2. Complete Exception Lifecycle Trace

The execution path from network socket to final benchmark report follows this exact chain:

```
[OpenRouter API / Cloudflare Edge Gateway]
     │
     │ HTTP 400 Bad Request (0-byte payload or empty body, headers: cf-ray, content-length: 0)
     ▼
[httpx.Response]
     │ status_code = 400, content = b"", text = ""
     ▼
[openai._base_client.BaseClient._make_status_error_from_response]
     │ tries: json.loads(response.text) -> fails or returns empty
     │ err_text = response.text.strip() -> ""
     │ err_msg = f"Error code: {response.status_code}" -> "Error code: 400"
     │ raises openai.BadRequestError(message="Error code: 400", response=response, body=None)
     ▼
[OpenRouterExtractionProvider.extract_insurance_data]
     │ catches openai.BadRequestError as exc
     │ calls extract_http_error_details(exc)
     │   - extracts status_code = 400
     │   - extracts request_id from response.headers ('cf-ray', 'x-openrouter-request-id', 'x-request-id')
     │   - checks exc.body: None
     │   - checks exc.response.text / .content: empty
     │   - checks str(exc): "Error code: 400"
     │ raises ExtractionInvalidRequestError(msg, provider_name="openrouter", status_code=400, ...) from exc
     ▼
[ExtractionPipelineService.process_inspection]
     │ catches ExtractionInvalidRequestError as exc
     │ inspects clean_msg: checks if already prefixed with provider name
     │ creates ExtractionPipelineResult(status=FAILED, errors=[err_line], error_type="ExtractionInvalidRequestError", ...)
     ▼
[ScenarioRunner.run_single_scenario]
     │ iterates scenario documents (doc1: COI, doc2: Endorsement)
     │ collects pipeline_errors from each failed document
     │ deduplicates identical errors: unique_errors = list(dict.fromkeys(pipeline_errors))
     │ creates ScenarioEvaluationResult(processing_status="error", error_message="; ".join(unique_errors))
     ▼
[ScenarioBenchmarkReport & Markdown Exporter]
     │ serializes clean, un-duplicated error details into JSON and Markdown reports
```

---

## 3. Root Cause of `Error code: 400` Persistence

Why did the third diagnostic run still produce `Error code: 400` despite Phase 7.3.2.5 error observability improvements?

1. **Upstream Empty Body (0 Bytes):**
   When Cloudflare or the OpenRouter routing layer rejects an incoming HTTP POST request at the edge (for example, due to a gateway rule, invalid parameter, or header validation failure), it can return an HTTP 400 with an empty body (`Content-Length: 0`).
2. **OpenAI SDK Exception Factory:**
   The OpenAI Python client (`openai._base_client.py`) parses the upstream response:
   ```python
   try:
       body = response.json()
   except Exception:
       body = None

   if isinstance(body, dict) and "error" in body:
       err_msg = body["error"].get("message")
   elif response.text.strip():
       err_msg = response.text.strip()
   else:
       err_msg = f"Error code: {response.status_code}"
   ```
   When `body` is `None` and `response.text` is `""`, `err_msg` is literally `"Error code: 400"`.
3. **Previous Tier 4 Fallback:**
   In Phase 7.3.2.5, `extract_http_error_details()` had:
   ```python
   if not extracted_msg:
       extracted_msg = str(exc)
   ```
   Because `str(exc)` was `"Error code: 400"`, `extracted_msg` was assigned `"Error code: 400"`.
   Then, `openrouter.py` formatted:
   `msg = f"OpenRouter invalid request/model error (HTTP {status_code}): {details['message']}"`
   resulting in:
   `OpenRouter invalid request/model error (HTTP 400): Error code: 400`.

---

## 4. Explanation of Duplicate Error Prefix in Reports

The generated report `live_diagnostic_scen03.md` displayed:
`AI extraction provider 'openrouter' failed: OpenRouter invalid request/model error (HTTP 400): Error code: 400; AI extraction provider 'openrouter' failed: OpenRouter invalid request/model error (HTTP 400): Error code: 400`

This duplication occurred at two distinct tiers:
1. **Pipeline Prefix Duplication:**
   In `backend/app/extraction/pipeline.py`:
   `err_line = f"AI extraction provider '{prov_name}' failed: {clean_msg}"`
   Because `clean_msg` already began with `OpenRouter invalid request/model error`, prefixing it again resulted in redundant terminology: `AI extraction provider 'openrouter' failed: OpenRouter...`.
2. **Scenario Document Duplication:**
   In `backend/evaluation/runners/scenario_runner.py`:
   Scenario `SCEN-01` contains two documents:
   - `SCEN-01_coi.pdf`
   - `SCEN-01_endorsement.pdf`
   Both documents were submitted to the OpenRouter provider sequentially. Both failed with the exact same error.
   `pipeline_errors` collected both failures (`[err_doc1, err_doc2]`) and joined them with `"; ".join(pipeline_errors)`:
   `err_doc1; err_doc2`.
   This produced the duplicated error string in `ScenarioEvaluationResult.error_message`.

---

## 5. Changes Made to Repository Code

### A. `backend/app/extraction/providers/openrouter.py`
1. **Defensive Response Reading & Multi-Header Request ID Extraction:**
   - Checks `response.headers` across multiple common identifiers: `cf-ray`, `x-openrouter-request-id`, `x-request-id`, and `request-id`.
   - Safely triggers `.read()` on unconsumed streaming responses if `is_stream_consumed` is False.
   - Inspects raw binary `content` with utf-8 decoding fallback if `text` is empty or raises `ResponseNotRead`.
2. **Comprehensive Body Extraction:**
   - Handles dictionaries with non-standard keys (`msg`, `err`, `reason`, `error_description`) or serializes entire dictionary payloads as JSON.
   - Handles plain text bodies.
   - In string parsing, extracts messages after `" - "` when SDK formats `"Error code: 400 - Actual message"`.
   - Explicitly sanitizes all string fields including `request_id` to prevent credential leakage.
3. **Exception Chaining (`__cause__`):**
   - Updated `extract_insurance_data` to ensure all raised domain exceptions (`ExtractionInvalidRequestError`, `ExtractionRateLimitError`, `ExtractionProviderError`, `ExtractionTimeoutError`) preserve the underlying SDK exception via `raise ... from exc`.
   - Prevented double provider prefixing when `server_msg` already begins with `OpenRouter invalid request/model error`.

### B. `backend/app/extraction/pipeline.py`
1. **Intelligent Prefix Deduplication:**
   - Before prefixing `clean_msg` with `AI extraction provider '{prov_name}' failed: `, inspects whether `clean_msg` already starts with the provider name or already contains `AI extraction provider`.
   - Eliminates redundant wrapping at the pipeline layer.

### C. `backend/evaluation/runners/scenario_runner.py`
1. **Multi-Document Error Deduplication:**
   - In `run_single_scenario()`, deduplicates identical document error messages using `unique_errors = list(dict.fromkeys(pipeline_errors))` before joining.
   - Prevents duplicate identical failure messages from cluttering benchmark reports for multi-document scenarios.

---

## 6. Outgoing Request Audit

To determine why OpenRouter rejected the request with HTTP 400, the prepared HTTP request from `openai.OpenAI` was intercepted and audited offline:

```json
{
  "request_url": "https://openrouter.ai/api/v1/chat/completions",
  "method": "POST",
  "headers": {
    "host": "openrouter.ai",
    "content-type": "application/json",
    "accept": "application/json",
    "http-referer": "https://github.com/subguard-ai",
    "x-title": "SubGuard AI Insurance Extraction",
    "user-agent": "OpenAI/Python 3.42.1"
  },
  "body": {
    "model": "nvidia/nemotron-3.5-super-vl:free",
    "messages": [
      {
        "role": "system",
        "content": "<system prompt defining RawInsuranceExtraction schema>"
      },
      {
        "role": "user",
        "content": "<user prompt with document text>"
      }
    ],
    "response_format": {
      "type": "json_object"
    },
    "temperature": 0.0
  }
}
```

### Observations & Suspect Parameter:
- `timeout` is passed via the client configuration (45.0s httpx client timeout) and is NOT in the JSON body.
- `model`: `"nvidia/nemotron-3.5-super-vl:free"`.
- `temperature`: `0.0`.
- **`response_format: {"type": "json_object"}`:**
  OpenRouter model documentation indicates that not all models support OpenAI's structured `response_format: {"type": "json_object"}`. For models that lack native JSON mode support, OpenRouter's proxy or the upstream inference provider may reject the request with HTTP 400 Bad Request.
  *(Note: In accordance with Phase 7.3.2.6 strict offline constraints, `response_format` was not removed or altered during this phase; this is documented for future evaluation).*

---

## 7. Offline Unit Test Results

A comprehensive regression suite (`Phase7326ExceptionChainTests`) was added to `backend/tests/test_openrouter_extraction_provider.py`. All tests run completely offline with mocked SDK objects.

| Test Name | Verified Assertion | Status |
|:---|:---|:---:|
| `test_1_populated_body_error` | Standard `body["error"]` with `message`, `type`, `code`, `param` extracted | PASS |
| `test_2_plain_text_response_body` | Plain text error body preserved without crash | PASS |
| `test_3_raw_json_in_response_text_or_content` | Raw JSON string or raw bytes in `response.text`/`.content` parsed | PASS |
| `test_4_unconsumed_streaming_response_read` | Unconsumed streaming response calls `.read()` safely | PASS |
| `test_5_genuinely_empty_response_body` | Empty body (`""` / `b""`) falls back cleanly with status code | PASS |
| `test_6_request_id_extraction_headers` | Request ID extracted from `cf-ray`, `x-openrouter-request-id`, `x-request-id` | PASS |
| `test_7_exception_chaining_cause` | Upstream exception preserved via `__cause__` (`raise ... from exc`) | PASS |
| `test_8_duplicate_wrapping_prevention` | Duplicate prefixing prevented in provider and pipeline | PASS |
| `test_9_document_error_deduplication` | Scenario runner deduplicates identical errors across documents | PASS |
| `test_10_credential_redaction_headers_and_body` | API keys and Bearer tokens redacted across headers, body, request_id | PASS |
| `test_11_message_truncation_limit` | Error messages capped at 1,000 characters + `... [truncated]` | PASS |

### Full Test Suite Summary:
- **Total Tests Discovered & Executed:** 303
- **Failures:** 0
- **Errors:** 0
- **Execution Time:** ~7.6 seconds
- **Pass Rate:** 100%

---

## 8. Risk Assessment for Live Smoke-Test Execution

| Risk Area | Likelihood | Impact | Mitigation / Status |
|:---|:---:|:---:|:---|
| Credential Leakage | Very Low | Critical | Regex redaction active across body, message, headers, and request_id fields. |
| Automatic Model Fallback / Paid Routing | Zero | High | Hardcoded model check enforces `:free` suffix; automatic fallback disabled. |
| Duplicate Exception Wrapping | Zero | Low | Deduplication implemented in `openrouter.py`, `pipeline.py`, and `scenario_runner.py`. |
| Exception Context Loss | Zero | Medium | All provider exceptions raised with `from exc`, preserving full `__cause__`. |
| Upstream HTTP 400 Due to JSON Mode | Moderate | Medium | If `nvidia/nemotron-3.5-super-vl:free` does not support `response_format: {"type": "json_object"}`, live request may continue to fail until JSON mode handling is adjusted. |

---

## 9. Recommended Next Steps

1. **Verify JSON Mode Compatibility:**
   When authorized for live requests, test whether `nvidia/nemotron-3.5-super-vl:free` accepts or rejects `response_format={"type": "json_object"}`. If unsupported, allow prompt-directed JSON output with schema enforcement.
2. **Conduct Controlled Live Diagnostic:**
   Execute a strictly single-scenario smoke test (`SCEN-01`, `--max-retries 1`) to confirm whether the improved observability captures Cloudflare / OpenRouter error headers and details.
3. **Operator Approval Gate:**
   Await explicit operator sign-off before making any outbound network requests or consuming API credits.

---

## 10. Final Sign-Off Block

- **Lead Engineer:** Antigravity Agent
- **Audited Components:** `backend/app/extraction/providers/openrouter.py`, `backend/app/extraction/pipeline.py`, `backend/evaluation/runners/scenario_runner.py`, `backend/tests/test_openrouter_extraction_provider.py`
- **Network Requests Made:** 0
- **API Credits Consumed:** 0.00
- **Test Status:** 303/303 PASSED
- **Repository State:** Clean, verified, ready for operator review
