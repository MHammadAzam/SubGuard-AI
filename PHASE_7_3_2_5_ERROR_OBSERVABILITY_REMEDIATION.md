# SubGuard AI — Phase 7.3.2.5: Provider Error Observability & Metadata Preservation Report

**Date:** November 5, 2026  
**Phase:** 7.3.2.5 — Provider Error Observability and Metadata Preservation  
**Status:** PASS — ERROR OBSERVABILITY RESTORED  
**Execution Environment:** Windows, Python 3.12, Offline Verification  
**Repository:** `D:\Documents\DOCUMENTS\SubGuard-AI`  
**Git Branch:** `main`  
**Git Baseline HEAD:** `12c4c413d54e24d40168392e8b3867c2c17d8ccf`  

---

## 1. Executive Summary

In Phase 7.3.2.4 diagnosis, two systemic defects were identified that caused the live smoke-test reports to obscure upstream HTTP errors with generic `"Error code: 400"` text and hardcoded `"ExtractionProviderFailure"` labels:
1. `extract_http_error_details()` in `openrouter.py` only inspected `body.get("error")` and `body.get("message")`. When an endpoint or proxy returns an empty body (`""`), non-standard JSON (such as `{"detail": ...}` or `{"description": ...}`), or raw text, the function fell back to `str(exc)` (`"Error code: 400"`) and failed to inspect `exc.response.text`.
2. `scenario_runner.py` discarded structured metadata from `ExtractionPipelineResult` (`error_type`, `status_code`, `error_code`, `request_id`), hardcoding `error_type="ExtractionProviderFailure"`.

In Phase 7.3.2.5, both defects were remediated and verified **strictly offline** (zero network calls, zero API credit consumption, zero credential exposure). The test suite expanded from 282 to 292 tests, all passing with zero failures.

---

## 2. Defects Remediated & Code Changes

### 2.1 Error Extraction Precedence (`openrouter.py`)
`extract_http_error_details()` was redesigned with a defensive four-tier extraction hierarchy:
1. **Tier 1 — Standard Structured Body (OpenAI-compatible):**
   - `body["error"]["message"]`
   - `body["error"]["type"]`
   - `body["error"]["code"]`
   - `body["error"]["param"]`
2. **Tier 2 — Alternative Structured Fields:**
   - `body["message"]`
   - `body["detail"]` (handles plain strings and FastAPI/Pydantic validation lists of dicts, extracting field location and message)
   - `body["description"]`
3. **Tier 3 — Raw HTTP Response Text (`exc.response.text`):**
   - Safely accessed inside `try...except` to prevent exceptions if streams were closed or unread.
   - If JSON-parsable, attempts to extract nested error/detail/description objects.
   - Otherwise, extracts raw non-JSON text (e.g. gateway error pages, Cloudflare notices).
4. **Tier 4 — Final Fallback:**
   - `str(exc)`, safely wrapped to avoid raising.

### 2.2 Robustness & Credential Redaction
- **Exception Safety:** The extraction function is fully defensive; no malformed input, missing attribute, or closed stream can cause it to raise an exception.
- **Message Truncation:** Long error strings (e.g. large HTML pages) are truncated at 1,000 characters with `... [truncated]` to protect report readability.
- **Credential Sanitization:** All extracted string fields (`message`, `error_type`, `error_code`, `error_param`) are passed through `sanitize_error_message()`, redacting OpenAI/OpenRouter keys (`sk-or-v1-...`, `sk-...`), Google keys (`AIza...`), Bearer tokens, and Authorization headers.

### 2.3 Metadata Preservation (`scenario_runner.py` & `reports.py`)
- **`ScenarioEvaluationResult` Schema:** Added optional fields `status_code: Optional[int] = None`, `error_code: Optional[str] = None`, and `request_id: Optional[str] = None` with default `None` for full backward compatibility.
- **`run_single_scenario`:** When extraction fails, the primary `error_type` is dynamically preserved from `pipeline_res.error_type` (e.g. `ExtractionInvalidRequestError`, `ExtractionAuthenticationError`, `ExtractionTimeoutError`) rather than hardcoding `ExtractionProviderFailure`. Status code, error code, and request ID are preserved.
- **Execution Failures & Markdown Reporting:** `report.execution_failures` preserves `status_code`, `error_code`, and `request_id`. `generate_markdown_report()` renders a dedicated Section 7 table: `Execution Failures & Diagnostic Details`.

---

## 3. Response-Format Compatibility Investigation (Task 3)

| Question | Finding | Architectural Classification |
|---|---|---|
| **Is JSON mode mandatory for the current pipeline?** | **No.** The SubGuard AI pipeline takes the completion text, strips markdown fences via `_clean_json_text()`, and parses it using standard Python `json.loads()`. JSON mode (`response_format={"type": "json_object"}`) is an API-level generation hint, not an ingestion requirement. | **Confirmed Fact** (from `openrouter.py:445-463`) |
| **Can Pydantic safely validate a plain text JSON response?** | **Yes.** `RawInsuranceExtraction.model_validate(data)` strictly verifies types, evidence structures, limits, and endorsements deterministically regardless of whether the provider used JSON mode or prompt-directed generation. | **Confirmed Fact** (from `pipeline.py:472-479`) |
| **Does the current provider support explicit structured outputs?** | **Partially.** OpenRouter supports OpenAI-compatible structured outputs (`response_format={"type": "json_schema", ...}`) only on select models. Many open-source, vision, or free-tier models (including `nvidia/nemotron-3.5-super-vl:free`) do not support `response_format` and reject requests with HTTP 400 Bad Request. | **Confirmed Fact** (from OpenRouter documentation & smoke tests) |
| **Would removing `response_format` compromise reliability or schema enforcement?** | **No.** Deterministic Python schema enforcement remains 100% active via Pydantic. If an upstream model rejects `response_format`, removing that parameter is the only way to allow the model to respond; prompt instructions in `OPENROUTER_SYSTEM_PROMPT` guide JSON structuring. | **Strongly Supported Inference** |

*Note: Per Phase 7.3.2.5 constraints, `response_format` was NOT removed and no fallback was enabled in this phase.*

---

## 4. Offline Verification & Regression Testing

File: `backend/tests/test_openrouter_extraction_provider.py`  
A new test class `Phase7325ErrorObservabilityTests` containing 10 targeted tests was implemented:
1. `test_1_standard_openai_error_body`: Validates extraction of standard OpenAI error body attributes.
2. `test_2_alternative_detail_error_body_string_and_list`: Validates extraction from string `detail` and FastAPI list of dicts.
3. `test_3_alternative_description_error_body`: Validates extraction of `description` field.
4. `test_4_raw_response_text_fallback`: Validates extraction from `exc.response.text` (both raw JSON and plain gateway error text).
5. `test_5_missing_or_malformed_response_body`: Validates safety across `None`, integers, empty dicts, and stream-closed exceptions.
6. `test_6_credential_redaction`: Verifies API key, Bearer token, and authorization header redaction across all error fields.
7. `test_7_error_message_truncation`: Verifies clean truncation at 1,000 characters for oversized error bodies.
8. `test_8_structured_metadata_propagation_into_scenario_reports`: Verifies JSON serialization and Markdown Section 7 diagnostic table rendering.
9. `test_9_missing_optional_metadata_handled_gracefully`: Verifies missing metadata produces safe `-` placeholders without crashing.
10. `test_10_provider_failure_never_produces_compliant`: Verifies extraction failures never produce `COMPLIANT`, `is_false_compliant=False`, and `is_unsafe_pass=False`.

### Full Test Suite Run:
```powershell
cd backend; python -m unittest discover -s tests -p "test_*.py"
Ran 292 tests in 7.768s
OK
```
Zero failures, zero errors across all 292 tests.

---

## 5. Scope & Safety Attestations

1. **Zero External Requests:** Absolutely zero network calls, HTTP requests, or external provider contacts were made.
2. **Zero API Credits Consumed:** 0 credits used.
3. **No Credential Exposure:** `OPENROUTER_API_KEY` was never printed, logged, or recorded.
4. **Deterministic Compliance Intact:** Compliance rules, reason codes, and manuscript trust boundaries remain completely unmodified.
5. **Historical Reports Preserved:** Both `live_smoke_test_scen01.*` and `live_smoke_test_scen02.*` remain preserved and unmodified.

---

## 6. Git Worktree Status

- **Branch:** `main`
- **Tracked Files Modified:**
  - `backend/app/extraction/providers/openrouter.py`
  - `backend/evaluation/runners/scenario_runner.py`
  - `backend/evaluation/schemas/reports.py`
  - `backend/tests/test_openrouter_extraction_provider.py`
- **Untracked Reports Present:**
  - `PHASE_7_3_2_4_SECOND_SMOKE_FAILURE_DIAGNOSIS.md`
  - `PHASE_7_3_2_5_ERROR_OBSERVABILITY_REMEDIATION.md`
  - `backend/evaluation/reports/generated/live_smoke_test_scen02.json`
  - `backend/evaluation/reports/generated/live_smoke_test_scen02.md`

---

## 7. Final Verdict

**`PASS — ERROR OBSERVABILITY RESTORED`**

Provider HTTP error extraction now defensively recovers standard bodies, alternative `detail`/`description` payloads, and raw response text. The scenario benchmark runner preserves structured error types, status codes, error codes, and request IDs into JSON and Markdown reports. All 292 unit and regression tests pass offline.
