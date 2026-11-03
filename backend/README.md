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
│   ├── test_openrouter_extraction_provider.py # 19 unit tests for OpenRouter SDK integration, error handling, diagnostics & retries
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

- **Zero-Cost Protection:** The provider will **never** automatically fall back to paid models (e.g. GPT-4o) or paid providers (e.g. Gemini). If the specified free model fails or is unavailable, the pipeline terminates cleanly with an explicit error.
- **Credential Protection:** API keys and tokens are automatically redacted in console logs and generated benchmark reports (`[REDACTED]`).
- **Rate-Limit Troubleshooting:** Free models on OpenRouter may experience queuing delays, strict rate limits (HTTP 429), or temporary downtime (HTTP 503). The provider automatically performs bounded exponential backoff retries. If rate limits persist, reduce concurrency or benchmark document count (`--max-documents 1`).

---

## Running Tests

The test suite requires Python 3.12+, Pydantic v2, PyMuPDF, google-genai, and openai. Run all 117 automated tests using standard library `unittest`:

```bash
cd backend
python -m unittest discover -s tests -p "test_*.py" -v
```

---

## Running the API Server (Optional)

To start the minimal local development API:

```bash
cd backend
python -m uvicorn app.main:app --reload --port 8000
```
Interactive Swagger documentation: `http://localhost:8000/docs`.
