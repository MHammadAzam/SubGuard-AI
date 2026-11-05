# SubGuard AI — Phase 7.3.2.2: OpenRouter Request Compatibility Remediation Report

**Date:** November 5, 2026  
**Phase:** 7.3.2.2 — OpenRouter Request Compatibility Remediation  
**Status:** PASS — OFFLINE REMEDIATION COMPLETE  
**Execution Environment:** Windows, Python 3.12, Offline Verification  
**Repository:** `D:\Documents\DOCUMENTS\SubGuard-AI`  

---

## 1. Executive Summary

During Phase 7.3.2.1, diagnosis of the first live smoke-test failure on `nvidia/nemotron-3.5-super-vl:free` revealed two critical implementation deficiencies:
1. **Opaque Error Observability:** OpenAI Python SDK `BadRequestError` exception strings yielded only `'Error code: 400'`, discarding the structured error response payload in `exc.body`.
2. **Strict Request Formatting Incompatibility:** In text-only mode (`render_images=False`), `OpenRouterExtractionProvider` passed a multimodal list of content blocks `[{"type": "text", "text": "..."}]`. Certain upstream provider engines (notably NVIDIA NIM endpoints via OpenRouter) reject list-based user content for text-only inference, requiring a plain string.
3. **Uncontrolled Fallback Risk:** The provider implementation lacked explicit gating to prevent silent fallback or hidden secondary API calls during evaluation benchmarks.

In Phase 7.3.2.2, all identified weaknesses were remediated and verified **strictly offline** with zero live network calls, zero API credit consumption, and zero credential exposure. The full test suite of 282 tests passed cleanly.

---

## 2. Confirmed Defects & Root Cause Analysis

| Defect ID | Component | Root Cause | Remediation |
|---|---|---|---|
| **DEF-01** | `openrouter.py:extract_insurance_data` | `str(exc)` on `openai.BadRequestError` outputs only `"Error code: 400"`. The actionable error message, error type, and param returned by OpenRouter were encapsulated inside `exc.body`. | Implemented `extract_http_error_details(exc)` to extract and sanitize `message`, `type`, `code`, and `param` from `exc.body` (handling `dict`, `str`, and unexpected structures). |
| **DEF-02** | `openrouter.py:_build_user_content` | User content was always formatted as `List[Dict[str, Any]]` even when `len(images) == 0`. NVIDIA NIM models on OpenRouter reject array content for non-multimodal queries with HTTP 400. | Updated `_build_user_content()`: returns a single concatenated `str` when `image_count == 0`, and preserves the structured content list when `image_count > 0`. |
| **DEF-03** | `openrouter.py:is_response_format_error` | Prior detection was naive string matching on top-level exception string. It also risked silent fallback retries during benchmarks. | Enhanced `is_response_format_error()` to inspect extracted `error_param`, `error_code`, and `message`, rejecting unrelated 400 errors. Added `allow_response_format_fallback=False` (default) to prohibit silent automatic fallback requests. |

---

## 3. Implementation Details

### 3.1 HTTP Error Observability (`extract_http_error_details`)
Implemented standalone extraction function:
```python
def extract_http_error_details(exc: Exception) -> Dict[str, Any]:
    # Extracts status_code, request_id, server error message, type, code, and param
    # Redacts credentials and API keys via sanitize_error_message()
    # Safely handles dict body, string body, missing fields, and non-HTTP exceptions
```
When `openai.BadRequestError` is caught, the error is unpacked:
```text
ExtractionInvalidRequestError: OpenRouter request rejected (HTTP 400): [server message from exc.body] (type=invalid_request_error, code=..., param=...)
```

### 3.2 Text-Only vs. Multimodal Request Content
In `_build_user_content()`:
- **`image_count == 0` (Text-Only):** Returns `"\n\n".join(text_parts)` as a Python `str`.
- **`image_count > 0` (Multimodal):** Returns `List[Dict[str, Any]]` containing text objects and base64 image objects:
  `[{"type": "text", "text": ...}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}]`.

### 3.3 Response-Format Error Classification & Fallback Gating
`OpenRouterExtractionProvider.is_response_format_error()` evaluates:
- `error_param in ("response_format", "response_format.type")`
- `error_code in ("unsupported_response_format", "unsupported_json_mode")`
- Specific error message phrases indicating structured output / JSON mode rejection.
- Explicitly rejects unrelated 400 errors (e.g. invalid base64, token limits, unknown parameters).
- **Gating Parameter:** `allow_response_format_fallback: bool = False` (default). When `False`, encountering an unsupported response format immediately raises `ExtractionInvalidRequestError(..., error_code="unsupported_response_format")` with zero retry/fallback calls.

---

## 4. Verification & Testing

### 4.1 Targeted Unit Tests Added
File: `backend/tests/test_openrouter_extraction_provider.py`
9 new offline unit tests were added:
1. `test_task1_extract_http_error_details_reproduces_openai_sdk_behavior`: Confirms extraction from `openai.BadRequestError.body`.
2. `test_task1_extract_http_error_details_fallbacks_and_safe_types`: Verifies string bodies, empty bodies, and non-HTTP exceptions.
3. `test_task1_extract_http_error_details_redacts_credentials`: Verifies API key redaction in error body attributes.
4. `test_task1_extract_insurance_data_unpacks_error_body_into_exception`: Verifies end-to-end exception message enrichment.
5. `test_task2_text_only_request_sends_plain_string`: Verifies `isinstance(content, str)` when `image_count == 0`.
6. `test_task2_multimodal_request_preserves_content_array_with_images`: Verifies list format when `image_count > 0`.
7. `test_task3_is_response_format_error_positives`: Verifies detection across params, codes, and messages.
8. `test_task3_is_response_format_error_negatives`: Verifies unrelated 400 errors return `False`.
9. `test_task3_response_format_error_does_not_silently_retry`: Verifies single request attempt (`call_count == 1`) when `allow_response_format_fallback=False`.

### 4.2 Full Regression Test Suite Results
```bash
python -m unittest discover -s tests -p "test_*.py"
Ran 282 tests in 6.686s
OK
```
Zero failures, zero errors across all evaluator, manuscript boundary, schema, pipeline, and provider tests.

---

## 5. Scope & Safety Attestations

1. **Zero External Calls:** Absolutely no network requests were made to OpenRouter or external endpoints during this phase.
2. **Zero Credit Consumption:** 0 API credits were consumed.
3. **No Credential Exposure:** `OPENROUTER_API_KEY` was never printed, logged, or recorded.
4. **Deterministic Compliance Intact:** Deterministic evaluation rules, reason codes, and manuscript trust boundaries remain completely unmodified.
5. **Original Reports Preserved:**
   - `backend/evaluation/reports/generated/live_smoke_test_scen01.json`
   - `backend/evaluation/reports/generated/live_smoke_test_scen01.md`
   Both original failed smoke-test reports remain intact and unmodified.

---

## 6. Git Worktree Status

- **Branch:** `main`
- **Files Modified:**
  - `backend/app/extraction/providers/__init__.py`
  - `backend/app/extraction/providers/openrouter.py`
  - `backend/evaluation/runners/scenario_runner.py`
  - `backend/tests/test_openrouter_extraction_provider.py`
- **Untracked Phase Reports:**
  - `PHASE_7_3_2_1_LIVE_SMOKE_FAILURE_DIAGNOSIS.md`
  - `PHASE_7_3_2_2_OPENROUTER_COMPATIBILITY_REMEDIATION.md`
- **Baseline Cohort / Test Artifacts:** All tracked historical benchmark outputs preserved.

---

## 7. Final Verdict

**PASS — OFFLINE REMEDIATION COMPLETE**

The provider request formatting and error observability remediation is complete, verified by offline unit and regression tests. The codebase is in a safe, verified state ready for operator review before any further live tests.
