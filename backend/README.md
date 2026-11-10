# SubGuard AI — Backend & Compliance Evaluator

## Overview

This directory contains the production-grade core engine of **SubGuard AI**:
- **PDF Ingestion & Processing:** PyMuPDF-based text and bounding-box extraction with raster image rendering and memory safeguards.
- **AI Extraction Pipeline:** Swappable provider abstraction (Gemini multimodal & Mock) with deterministic value normalization and evidence verification.
- **Compliance Evaluator:** Pure, deterministic rule-based compliance evaluator operating over extracted data contracts.
- **FastAPI Endpoint:** Minimal stateless API boundary.

## Core Philosophy

> **AI extracts information. Deterministic Python rules evaluate it. Humans resolve uncertainty.**

- **AI is never the judge:** Compliance status is computed via pure, deterministic Python functions.
- **Uncertainty is first-class:** Missing values, ambiguous entity matches, contradictory limits, low extraction confidence, and COI checkbox-only representations always surface explicit uncertainty states (`unable_to_determine`, `requires_human_review`, or `missing_evidence`) rather than false passes.
- **Amounts in Cents:** Monetary limits are stored and evaluated as integer cents (e.g. `$1,000,000` = `100,000,000` cents) to eliminate floating-point rounding errors.

---

## Directory Structure

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py                     # Minimal FastAPI endpoint
│   ├── compliance/
│   │   ├── __init__.py
│   │   └── evaluator.py            # Deterministic ComplianceEvaluator & entity matching
│   ├── documents/
│   │   ├── __init__.py
│   │   └── processor.py            # PyMuPDF parser, security checks & classification
│   ├── extraction/
│   │   ├── __init__.py
│   │   ├── normalizer.py           # Currency (to cents), date, & coverage normalizers
│   │   ├── ocr.py                  # OCR provider interface & mock
│   │   ├── pipeline.py             # ExtractionPipelineService linking PDF -> AI -> Schemas
│   │   └── providers/
│   │       ├── __init__.py         # Provider factory (get_extraction_provider)
│   │       ├── base.py             # BaseExtractionProvider interface
│   │       ├── mock.py             # MockExtractionProvider with error simulation
│   │       ├── gemini.py           # Google Gemini multimodal extraction provider
│   │       └── openrouter.py       # OpenRouter multimodal extraction provider (OpenAI-compatible)
│   └── schemas/
│       ├── __init__.py
│       ├── insurance.py            # Evaluator data contracts (Extracted, Requirement, Findings)
│       ├── pdf.py                  # PDF inspection, span, and document processing models
│       └── raw_extraction.py       # Raw schemas returned by AI extraction models
├── evaluation/
│   ├── __init__.py
│   ├── schemas/                    # Ground-truth and benchmark report data models
│   ├── metrics/                    # Field accuracy, precision/recall/F1, and evidence grounding evaluator
│   ├── datasets/                   # Dataset loader and serialization utilities
│   ├── annotations/                # Persisted ground-truth annotations (JSON)
│   ├── fixtures/                   # Programmatic synthetic PDF generator
│   ├── runners/                    # CLI benchmark runner
│   └── reports/                    # Generated JSON and Markdown benchmark reports
├── tests/
│   ├── __init__.py
│   ├── test_compliance_evaluator.py # 38 unit tests for all compliance edge cases & legal entity matching
│   ├── test_pdf_processor.py        # 16 unit tests for PDF security, rotation, bounding boxes
│   ├── test_extraction_pipeline.py  # 13 unit tests for extraction, normalization, OCR & evidence
│   ├── test_evaluation_framework.py # 20 unit tests for ground truth, metrics & evidence grounding
│   ├── test_gemini_extraction_provider.py # 8 unit tests for Gemini SDK integration, error handling & retries
│   ├── test_openrouter_extraction_provider.py # 24 unit tests for OpenRouter SDK integration, error handling, diagnostics & retries
│   ├── test_api.py                  # 3 integration tests for FastAPI endpoint
│   └── fixtures/
│       ├── __init__.py
│       └── sample_data.py           # Reusable synthetic test fixtures
├── pyproject.toml
└── README.md
```

---

## Running Extraction Benchmarks

SubGuard AI provides a ground-truth evaluation and benchmarking framework to measure extraction accuracy, true negative rates, and evidence grounding against actual PDF text layers.

### Run Benchmark via CLI

To execute benchmarks against the built-in synthetic suite using the mock provider:

```bash
cd backend
python -m evaluation.runners.benchmark --provider mock --format both
```

To run against live Gemini (requires `GEMINI_API_KEY`):

```bash
cd backend
export GEMINI_API_KEY="your-api-key"
python -m evaluation.runners.benchmark --provider gemini --model gemini-2.0-flash --max-documents 5 --format both
```

### OpenRouter Provider Setup (Free Models)

SubGuard AI supports OpenRouter's OpenAI-compatible API (`https://openrouter.ai/api/v1`) for running benchmarks with zero API cost using free models (default: `nvidia/nemotron-3.5-super-vl:free`).

#### 1. Environment Variable Setup

Set the `OPENROUTER_API_KEY` environment variable:

- **Windows PowerShell:**
  ```powershell
  $env:OPENROUTER_API_KEY = "your-openrouter-key"
  ```
- **Linux/macOS:**
  ```bash
  export OPENROUTER_API_KEY="your-openrouter-key"
  ```

#### 2. Running the Benchmark

```bash
cd backend
python -m evaluation.runners.benchmark --provider openrouter --model nvidia/nemotron-3.5-super-vl:free --max-documents 1 --format both
```

#### 3. Strict Cost & Privacy Safeguards

> [!CAUTION]
> **Data Privacy Warning:** Never submit real, confidential, or proprietary subcontractor insurance documents to free public models. Free-tier endpoints may log prompts or use submitted data for model evaluation. Restrict free-model benchmarking strictly to the provided synthetic test fixtures.

- **Free-Only Model Enforcement:** OpenRouter is strictly restricted to free models. Any requested model must explicitly end with `:free` (default: `nvidia/nemotron-3.5-super-vl:free`). Paid models and automatic/random routers (`openrouter/auto`) are rejected immediately with `ExtractionConfigurationError`.
- **Zero-Cost Protection:** The provider will **never** automatically fall back to paid models (e.g. GPT-4o) or paid providers (e.g. Gemini). If the specified free model fails or is unavailable, the pipeline terminates cleanly with a structured diagnostic error.
- **Credential & Privacy Protection:** API keys, Bearer tokens, and Authorization headers are automatically redacted in console logs and generated benchmark reports (`[REDACTED_API_KEY]`, `[REDACTED_TOKEN]`). Raw completion text snippets and submitted input values are stripped from error messages.
- **Rate-Limit & Request Troubleshooting:** Free models on OpenRouter may experience queuing delays, strict rate limits (HTTP 429), or temporary downtime (HTTP 503). The provider automatically performs bounded exponential backoff retries for transient errors. If a model rejects `response_format={"type": "json_object"}` with HTTP 400, the provider automatically falls back to requesting without that parameter while enforcing full JSON parsing and Pydantic validation.

---

## Running Tests

Install dev dependencies (`pytest`, `httpx`, `playwright`) cleanly, then run tests against isolated temp databases:

```bash
cd backend
pytest tests --ignore=tests/test_browser_workflow_acceptance.py -q
```

---

## Pilot Configuration, Security & Operations

### 1. Clean Installation & Environment Configuration

Requires Python `3.12+` and local **Tesseract OCR** (`TESSERACT_CMD` env var or standard install path).

```bash
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e . -c requirements.lock
# Optional dev/test tools: pip install -e ".[dev]" -c requirements.lock
# Optional external extraction SDKs: pip install -e ".[providers]" -c requirements.lock
```

Copy `.env.example` and configure before running in production (`SUBGUARD_ENV=production`):
- `SUBGUARD_SESSION_SECRET`: Mandatory `>= 32` random non-placeholder characters in production.
- `SUBGUARD_DB_PATH` & `SUBGUARD_UPLOAD_DIR`: Explicit persistent filesystem locations.
- `SUBGUARD_AUDIT_LOG_PATH`: Optional path for `RotatingFileHandler` `INFO` JSON security logs (`subguard.security`).
- `SUBGUARD_TRUSTED_PROXIES` / `SUBGUARD_CORS_ORIGINS`: Set only explicit proxy IPs and allowed origins.

### 2. Administrator Bootstrap & Session Hygiene

There are no default passwords or public self-registration. Provision accounts explicitly:

```bash
cd backend
python -m app.security.bootstrap create-admin --username pilot_admin --display-name "Pilot Admin"
# Or purge expired/revoked sessions:
python -m app.security.bootstrap purge-sessions
```

### 3. Provider Safety Allowlist (`SUBGUARD_ALLOWED_PROVIDERS`)

- Server defaults strictly to `SUBGUARD_ALLOWED_PROVIDERS=mock`.
- Upload requests naming disabled providers (`gemini`, `openrouter`) are rejected with `HTTP 403` before files are read or processed.
- Never silently falls back to paid providers.

### 4. Resource Limits & Single-Worker Requirement

Defaults sized conservatively for an 8 GB host:
- `SUBGUARD_MAX_FILE_BYTES`: `15 MiB` per PDF (`MAX_PDF_SIZE_BYTES`)
- `SUBGUARD_MAX_ENDORSEMENT_FILES`: `10` per submission
- `SUBGUARD_MAX_CUMULATIVE_UPLOAD_BYTES`: `50 MiB` per submission
- `SUBGUARD_MAX_CONCURRENT_REVIEWS`: `2` simultaneous document processing pipelines (offloaded off asyncio event loop)
- `SUBGUARD_MAX_CONCURRENT_SCRYPT`: `4` concurrent `scrypt` (`N=131072, r=8, p=1`) verifications
- `SUBGUARD_MAX_PROVIDER_IMAGES`: `12` PNG pages / `SUBGUARD_MAX_PROVIDER_PAYLOAD_BYTES`: `12 MiB`

Rate limiters and concurrency gates use process-local memory; run a single Uvicorn worker (`--workers 1`) behind a TLS-terminating reverse proxy.

### 5. Online SQLite Backup, Integrity Check, and Staged Restore

Never copy running `-wal` SQLite `.db` files directly. Use the operational backup CLI:

```bash
cd backend
# Consistent online snapshot via sqlite3.Connection.backup() + upload SHA-256 manifest
python -m app.persistence.backup backup --output-dir D:\Backups\SubGuard

# Verify DB integrity, foreign keys, document rows, and manifest checksums
python -m app.persistence.backup verify --manifest D:\Backups\SubGuard\<backup_name>\manifest.json

# Restore cleanly to an explicit empty target (STOP the running server before swapping live data!)
python -m app.persistence.backup restore --backup-dir D:\Backups\SubGuard\<backup_name> --target-db-path D:\Restore\subguard.db --target-upload-dir D:\Restore\uploads
```

### 6. Running the Server

```bash
cd backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Public `/health` exposes minimal liveness only; authenticated `admin` accounts can inspect `GET /api/v1/admin/system-health` for SQLite connectivity and upload-storage writability. Interactive OpenAPI `/docs` and `/openapi.json` are disabled (`404`) when `SUBGUARD_ENV=production`.
