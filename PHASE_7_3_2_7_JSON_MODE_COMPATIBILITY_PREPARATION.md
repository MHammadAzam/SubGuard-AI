# Phase 7.3.2.7 — Explicit JSON Mode Configuration & Offline Compatibility Preparation Report

**Phase:** 7.3.2.7  
**Status:** COMPLETE (Strictly Offline Verification & Compatibility Preparation)  
**Date:** 2026-11-05  
**Local Project:** `D:\Documents\DOCUMENTS\SubGuard-AI`  
**Execution Context:** Forensic SDK Inspection, Dual-Mode Configuration Engineering, Multi-Document Attribution Refactoring, and Adversarial Unit Testing (Zero Live API Requests, Zero Credit Consumption)

---

## 1. SDK Version Verification (Task 1)

Forensic inspection of the installed Python package metadata was conducted using `importlib.metadata.version("openai")`:

```python
import importlib.metadata, openai

version = importlib.metadata.version("openai")
# Result: '3.42.1'
```

### Verified Findings:
- **Installed Distribution Version:** `3.42.1`
- **Reported `openai.__version__`:** `3.42.1`
- **Confirmation:** The installed version **matches** the previous report's claim of `3.42.1` exactly.
- **Exception Hierarchy:**
  `openai.BadRequestError` -> `openai.APIStatusError` -> `openai.APIError` -> `openai.OpenAIError` -> `Exception` -> `BaseException`.
- **Response & Error Attributes on `openai.APIStatusError`:**
  - `status_code`: HTTP status code integer (`400`, `401`, `429`, `500`, etc.)
  - `response`: `httpx.Response` object (carrying `.status_code`, `.headers`, `.text`, `.content`, `.read()`)
  - `request`: `httpx.Request` object (carrying `.method`, `.url`, `.headers`)
  - `body`: Parsed JSON dictionary or raw payload string
  - `message`: Formatted SDK error message (`f"Error code: {response.status_code} - {server_msg}"` or fallback `f"Error code: {response.status_code}"`)
  - `code`: Upstream error code string if extracted from `body["error"]["code"]`
  - `param`: Upstream error parameter string if extracted from `body["error"]["param"]`
  - `type`: Upstream error type string if extracted from `body["error"]["type"]`
  - `request_id`: Upstream request ID string extracted from response headers

---

## 2. Configuration Design (Task 2)

An explicit, non-leaky configuration option for JSON mode was implemented across the provider, factory, and CLI layers:

### A. Provider Layer (`OpenRouterExtractionProvider`)
- Added `json_mode: Union[bool, str] = True` to `OpenRouterExtractionProvider.__init__`.
- Supports boolean (`True`/`False`), string (`"enabled"`/`"disabled"`, `"true"`/`"false"`, `"1"`/`"0"`, `"on"`/`"off"`), and environment variable `EXTRACTION_JSON_MODE`.
- **Default:** `True` (preserves backward compatibility as default).
- When `json_mode` is `False` / `"disabled"`:
  - `response_format` is **completely omitted** from `client.chat.completions.create(**kwargs)` (not passed as `None` or `"null"`).
- **Invariants Maintained:**
  - JSON mode is never silently disabled after an error.
  - Automatic fallback is strictly prohibited.
  - Retries are disabled on invalid request / response_format errors.

### B. Factory Layer (`get_extraction_provider`)
- Extended `get_extraction_provider("openrouter", ..., json_mode=...)` in `backend/app/extraction/providers/__init__.py`.
- Preserves full signature compatibility with default `json_mode=True`.

### C. CLI Runner Layer (`scenario_runner.py` & `benchmark.py`)
- Added explicit CLI argument `--json-mode` with choices `["enabled", "disabled"]` defaulting to `"enabled"`:
  ```powershell
  python evaluation/runners/scenario_runner.py --scenario SCEN-01 --provider openrouter --json-mode enabled
  python evaluation/runners/scenario_runner.py --scenario SCEN-01 --provider openrouter --json-mode disabled
  ```

---

## 3. Request Comparison (Task 2 & Task 5)

An audit of the outgoing request payload constructed by `OpenRouterExtractionProvider` confirms that all parameters other than `response_format` remain 100% identical:

| Request Parameter | JSON Mode Enabled (`--json-mode enabled`) | JSON Mode Disabled (`--json-mode disabled`) | Identical? |
|:---|:---|:---|:---:|
| `model` | `nvidia/nemotron-3.5-super-vl:free` | `nvidia/nemotron-3.5-super-vl:free` | ✅ YES |
| `temperature` | `0.0` | `0.0` | ✅ YES |
| `timeout` | `timeout_seconds` (e.g. `30.0` or `45.0`) | `timeout_seconds` (e.g. `30.0` or `45.0`) | ✅ YES |
| `messages[0]` (system) | `OPENROUTER_SYSTEM_PROMPT` | `OPENROUTER_SYSTEM_PROMPT` | ✅ YES |
| `messages[1]` (user) | Extracted page text & raster images | Extracted page text & raster images | ✅ YES |
| `response_format` | `{"type": "json_object"}` | **OMITTED ENTIRELY** | **Divergence Point** |

---

## 4. Downstream Validation Invariants (Task 3)

Regardless of whether JSON mode is enabled or disabled, both paths route through the exact same downstream validation and normalization pipeline:

1. **Text Extraction:** `raw_text = response.choices[0].message.content or ""`
2. **Code Fence Stripping:** `cleaned_json = self._clean_json_text(raw_text)` (handles raw JSON or markdown fences ````json ... ````)
3. **JSON Decoding:** `data = json.loads(cleaned_json)` (raises `ExtractionSchemaValidationError` if invalid)
4. **Root Object Check:** Ensures `isinstance(data, dict)` (raises `ExtractionInvalidModelResponseError` if not a dict)
5. **Schema Validation:** `RawInsuranceExtraction.model_validate(data)` (raises `ExtractionSchemaValidationError` if schema violated)
6. **Evidence Resolution:** `_resolve_evidence()` verifies page numbers and quote substrings.
7. **Semantic Support Verification:** `_verify_entity_semantic_support()` checks entity names against cited quotes.
8. **Normalization & Compliance Evaluation:** Deterministic rule evaluation via `ComplianceEvaluator`.

**Safety Invariant:** A malformed, incomplete, or rejected response from either mode results in `DocumentProcessingStatus.FAILED` and is **never** interpreted as `COMPLIANT`, `is_false_compliant=False`, and `is_unsafe_pass=False`.

---

## 5. Diagnostic Attribution & Multi-Document Deduplication (Task 4)

Phase 7.3.2.7 reviewed the error deduplication mechanism to ensure that summary-level error collapsing does not obscure individual document failures:

### Implementation:
1. **Per-Document Failure Collection:**
   In `run_single_scenario()`, every document in the scenario that fails extraction is recorded in `document_failures`:
   ```python
   document_failures.append({
       "document_id": doc_gt.document_id,
       "filename": doc_gt.filename,
       "document_type": doc_gt.document_type.value,
       "error_type": pipeline_res.error_type,
       "error_message": doc_err_msg,
       "status_code": pipeline_res.status_code,
       "error_code": pipeline_res.error_code,
       "request_id": pipeline_res.request_id,
   })
   ```
2. **Schema Support:**
   `ScenarioEvaluationResult` and `ScenarioBenchmarkReport.execution_failures` now carry `document_failures: List[Dict[str, Any]]`.
3. **Summary Collapsing Without Hiding:**
   - `error_message` continues to collapse identical duplicate error strings into a clean single string for the scenario summary table.
   - When multiple documents fail in a scenario, Section 7 of the Markdown report renders detailed sub-rows itemizing each failed document:
     ```markdown
     | `SCEN-01` | `ExtractionInvalidRequestError` | 400 | `http_400` | `-` | OpenRouter invalid request... |
     | └─ **Documents (2)** | *Per-Document Attribution* | | | | |
     |     • `DOC1` (`SCEN-01_coi.pdf`) | `ExtractionInvalidRequestError` | 400 | - | `cf-ray-123` | Error msg... |
     |     • `DOC2` (`SCEN-01_endorsement.pdf`) | `ExtractionInvalidRequestError` | 400 | - | `cf-ray-456` | Error msg... |
     ```
4. **Integrity of Request IDs:** Missing request IDs remain `None` / `"-"`, never fabricated.

---

## 6. Offline Regression Test Results (Tasks 5 & 6)

A dedicated test suite `Phase7327JsonModeCompatibilityTests` was added to `backend/tests/test_openrouter_extraction_provider.py` covering all 12 specified test cases offline:

| # | Test Name | Target Requirement | Status |
|:---:|:---|:---|:---:|
| 1 | `test_1_default_json_mode_remains_enabled` | Default JSON mode remains enabled without explicit configuration | PASS |
| 2 | `test_2_explicit_json_mode_enabled` | Explicit JSON mode enabled via argument or string (`"enabled"`, `True`) | PASS |
| 3 | `test_3_explicit_json_mode_disabled` | Explicit JSON mode disabled via argument or string (`"disabled"`, `False`) | PASS |
| 4 | `test_4_disabled_mode_completely_omits_response_format` | Disabled mode completely omits `response_format` from kwargs | PASS |
| 5 | `test_5_all_other_request_parameters_remain_identical` | Model, messages, temperature, timeout remain 100% identical | PASS |
| 6 | `test_6_both_modes_use_identical_downstream_validation` | Both modes validate through identical JSON parsing & Pydantic pipeline | PASS |
| 7 | `test_7_invalid_json_fails_safely` | Invalid JSON raises `ExtractionSchemaValidationError` in both modes | PASS |
| 8 | `test_8_pydantic_validation_failures_fail_safely` | Schema mismatch raises `ExtractionSchemaValidationError` in both modes | PASS |
| 9 | `test_9_no_automatic_fallback_occurs` | No silent fallback from enabled to disabled mode upon error | PASS |
| 10 | `test_10_no_automatic_retry_occurs` | No retry occurs upon unsupported `response_format` error (call count = 1) | PASS |
| 11 | `test_11_document_level_error_attribution_survives_summary_deduplication` | Multi-doc errors collapse at summary level while retaining per-doc records | PASS |
| 12 | `test_12_existing_configuration_remains_backward_compatible` | Existing factories and runners default safely without breakage | PASS |

### Test Suite Execution Summary:
- **Total Tests Discovered & Executed:** 315
- **Passed:** 315
- **Failed:** 0
- **Errors:** 0
- **Live Network Requests:** 0
- **API Credits Consumed:** 0.00
- **Pass Rate:** 100%

---

## 7. Git & Working Tree Status

- **Current Branch:** `main`
- **Head Commit Before Phase 7.3.2.7:** `b018117`
- **Modified Files Staged for Commit:**
  - `backend/app/extraction/providers/openrouter.py`
  - `backend/app/extraction/providers/__init__.py`
  - `backend/evaluation/runners/scenario_runner.py`
  - `backend/evaluation/runners/benchmark.py`
  - `backend/evaluation/schemas/reports.py`
  - `backend/tests/test_openrouter_extraction_provider.py`
  - `PHASE_7_3_2_7_JSON_MODE_COMPATIBILITY_PREPARATION.md`

---

## 8. Remaining Uncertainties

1. **Upstream Gateway Support for `nvidia/nemotron-3.5-super-vl:free`:**
   Whether OpenRouter or the host inference endpoint accepts or rejects `response_format: {"type": "json_object"}` remains unverified against live endpoints because all work was conducted strictly offline.
2. **Behavior Without `response_format`:**
   In `--json-mode disabled`, `OPENROUTER_SYSTEM_PROMPT` explicitly instructs the model to return a single JSON object. While synthetic tests demonstrate that markdown-fenced and raw JSON are both cleanly parsed and validated by SubGuard AI, live model compliance with the prompt without `response_format` will only be observed during the next authorized controlled live test.

---

## 9. Sign-Off Block

- **Lead Engineer:** Antigravity Agent
- **Audited Components:** `openrouter.py`, `pipeline.py`, `scenario_runner.py`, `benchmark.py`, `reports.py`
- **Network Requests Made:** 0
- **API Credits Consumed:** 0.00
- **Test Status:** 315/315 PASSED
- **Phase Completion:** READY FOR OPERATOR REVIEW
