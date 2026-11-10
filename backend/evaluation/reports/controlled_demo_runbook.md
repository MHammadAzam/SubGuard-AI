# SubGuard AI — Controlled Demonstration Runbook

**Document Purpose:** Repeatable operational guide and presenter script for an **8–10 minute controlled technical demonstration** of SubGuard AI.  
**Execution Mode:** Offline **Mock Provider** (`HonestApplicationMockProvider` / `provider="mock"`) on **localhost only**.  
**Last Verified Against Repository:** `2026-11-10` (read-only code & test-verified).

> [!IMPORTANT]
> This demonstration exercises the verified **offline mock extraction path** on isolated temporary storage. It does **not** invoke Gemini, OpenRouter, or any live/paid AI model, and it must **not** be presented as live AI extraction parity or unsupervised production readiness.

---

## 1. Demonstration Objective & Time Budget

**Objective:** Demonstrate how SubGuard AI separates extraction from compliance judgment using its core architecture—*"AI extracts information. Deterministic Python rules evaluate it. Humans resolve uncertainty."*—while preserving field-level evidence traceability, human-review guardrails, and SQLite persistence.

| Segment | Topic | Target Time | Cumulative |
| :--- | :--- | :---: | :---: |
| **Part A** | Business Problem & Contractual Risk | 1:00 | 1:00 |
| **Part B** | System Architecture & Trust Boundaries | 1:00 | 2:00 |
| **Part C** | Verified Scenario A Submission & Findings | 2:00 | 4:00 |
| **Part D** | Field-Level Evidence Traceability | 2:00 | 6:00 |
| **Part E** | Human Review Queue & Guardrails | 1:30 | 7:30 |
| **Part F** | SQLite Persistence & History Retrieval | 1:00 | 8:30 |
| **Part G** | Mandatory Limitations & Readiness Disclosure | 1:00 | 9:30 |

---

## 2. Prerequisites & Verified Baseline

### Environment Prerequisites
- **OS & Shell:** Windows (`PowerShell 5.1` verified; `Windows 10/11` built-in `curl.exe` available).
- **Repository Root:** `d:\Documents\DOCUMENTS\Project-Antigravity`
- **Virtual Environment:** `d:\Documents\DOCUMENTS\Project-Antigravity\backend\.venv\Scripts\python.exe`
- **Scenario A Synthetic PDFs (already present in repo):**
  - `backend/evaluation/cohorts/fixtures/SCEN-01/SCEN-01_acord25.pdf`
  - `backend/evaluation/cohorts/fixtures/SCEN-01/SCEN-01_endorsement_cg2010.pdf`
- **Optional OCR Engine (if demonstrating scanned PDFs later):** Tesseract OCR (`TesseractOCRProvider`); Scenario A uses native-digital PDFs and does not require Tesseract.

### Latest Verified Quality Baseline (Mock / Offline Scope)
The presenter may cite these exact repository verification counts:
- **Hybrid OCR Suite (`tests/test_hybrid_page_ocr.py`):** `50 passed`
- **Browser Acceptance Suite (`tests/test_browser_workflow_acceptance.py`):** `6 passed` (`6/6`)
- **Backend Regression Suite (21 test files):** `578 passed, 9 subtests passed`
- **40-Scenario Cohort Benchmark (`post_fix_cohort_v1_mock`):** `40/40` scenarios completed (`0` failures), `0.00%` False Compliance Rate (`0/10`), `0.00%` Unsafe Compliance Rate (`0/30`), `50.00%` overall status accuracy (`20/40`, Macro F1 `0.4521`), and `70.00%` human-review routing rate (`28/40`).
- **Cohort v1 Manifest Integrity:** `Manifest valid: True`, `Errors: 0`.

---

## 3. Pre-Demo Isolation & Server Startup (Terminal 1)

SubGuard AI defaults to `backend/data/subguard.db` and `backend/data/uploads/` when environment variables are unset. **Never use the default database or upload directories for a demonstration.**

### Step 3.1 — Confirm Port `8000` Is Free & Create Isolated Temp Storage

Run in **PowerShell (Terminal 1)**:

```powershell
Set-Location d:\Documents\DOCUMENTS\Project-Antigravity\backend

# 1. Verify nothing is already listening on port 8000 (avoid colliding with stale servers)
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue

# 2. Create an isolated demonstration folder in $env:TEMP
$demoRoot = Join-Path $env:TEMP ("subguard_demo_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
New-Item -ItemType Directory -Force -Path (Join-Path $demoRoot "uploads") | Out-Null

$env:SUBGUARD_DB_PATH = Join-Path $demoRoot "subguard_demo.db"
$env:SUBGUARD_UPLOAD_DIR = Join-Path $demoRoot "uploads"

# 3. Ensure EXTRACTION_PROVIDER is unset (factory defaults to 'mock')
Remove-Item Env:EXTRACTION_PROVIDER -ErrorAction SilentlyContinue

Write-Host "Demo DB:      $env:SUBGUARD_DB_PATH"
Write-Host "Demo Uploads: $env:SUBGUARD_UPLOAD_DIR"
```

*(If port `8000` is occupied by another process, substitute `--port 8010` below and in all URLs.)*

### Step 3.2 — Start the FastAPI + Jinja2 Server (Localhost Only)

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Wait for:
`INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)`

### Step 3.3 — Verify Health & Mock-Mode Configuration (Terminal 2)

Open a second PowerShell window (**Terminal 2**) before attendees arrive:

```powershell
Set-Location d:\Documents\DOCUMENTS\Project-Antigravity\backend

# Verify server health
Invoke-RestMethod -Uri "http://127.0.0.1:8000/health"
# Expected: status = ok, service = compliance-evaluator

# Verify EXTRACTION_PROVIDER is unset and confirm empty isolated dashboard stats
Test-Path Env:EXTRACTION_PROVIDER
# Expected: False

Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/dashboard/stats"
# Expected: total_submissions = 0
```

> [!CAUTION]
> **Security Boundary:** Bind strictly to `127.0.0.1` (never `0.0.0.0`). The application currently implements **no authentication or role-based access control (RBAC)**.

---

## 4. Verified Scenario A Submission Reference (API Payload)

> [!WARNING]
> **Do NOT use the stale preset buttons (`⚡ Bundled Demo Scenarios`: `SCEN-01`, `SCEN-02`, `SCEN-13`, `SCEN-17`, `SCEN-19`) on `/submissions/new`.** Those buttons hit `/api/v1/compliance/demo-run/{scenario_id}` and use hard-coded legacy demo assumptions. Instead, submit Scenario A directly via `POST /api/v1/compliance/review-documents` with explicit custom requirements (or upload through the standard form).

### Endpoint Contract (Verified in `backend/app/main.py`)
- **Method & URL:** `POST http://127.0.0.1:8000/api/v1/compliance/review-documents`
- **Content-Type:** `multipart/form-data`
- **Multipart File Fields:**
  - `coi_file` *(required)*: `evaluation/cohorts/fixtures/SCEN-01/SCEN-01_acord25.pdf`
  - `endorsement_files` *(optional, repeatable)*: `evaluation/cohorts/fixtures/SCEN-01/SCEN-01_endorsement_cg2010.pdf`
- **Multipart Form Fields:**
  - `submission_name`: `"Scenario A - Compliant Policy Packet (Demo)"`
  - `evaluation_date`: `"2025-06-15"` *(ISO `YYYY-MM-DD`; falls inside `01/01/2025`–`01/01/2026`)*
  - `provider`: `"mock"` *(explicitly selects `HonestApplicationMockProvider` in `DocumentReviewService`)*
  - `requirements`: JSON array string specifying all 6 Scenario A checks (Named Insured, GL Each Occurrence >= `$1,000,000`, GL General Aggregate >= `$2,000,000`, GL Effective Date, GL Expiration Date, and Additional Insured endorsement).

### Ready-to-Run Scenario A Submission Command (PowerShell 5.1 Safe)

Because Windows PowerShell `5.1` lacks `Invoke-RestMethod -Form`, run the submission from **Terminal 2** using the repository's `.venv` `httpx` client (guaranteed `multipart/form-data` encoding with zero quoting issues):

```powershell
Set-Location d:\Documents\DOCUMENTS\Project-Antigravity\backend

@'
import json
from pathlib import Path
import httpx

requirements = [
    {
        "requirement_id": "REQ_NAMED_INSURED",
        "rule_type": "named_insured",
        "description": "Named Insured must match 'Apex Electrical Contractors LLC'",
        "severity": "critical",
        "required_entity_name": "Apex Electrical Contractors LLC",
        "require_exact_name_match": False,
    },
    {
        "requirement_id": "REQ_GL_EACH_OCC",
        "rule_type": "coverage_limit",
        "description": "Commercial General Liability Each Occurrence >= $1,000,000",
        "severity": "critical",
        "coverage_type": "general_liability",
        "limit_field": "each_occurrence",
        "min_amount_cents": 100_000_000,
    },
    {
        "requirement_id": "REQ_GL_GEN_AGG",
        "rule_type": "coverage_limit",
        "description": "Commercial General Liability General Aggregate >= $2,000,000",
        "severity": "major",
        "coverage_type": "general_liability",
        "limit_field": "general_aggregate",
        "min_amount_cents": 200_000_000,
    },
    {
        "requirement_id": "REQ_GL_EFFECTIVE_DATE",
        "rule_type": "policy_effective_date",
        "description": "General Liability policy must be effective on or before evaluation date",
        "severity": "critical",
        "coverage_type": "general_liability",
    },
    {
        "requirement_id": "REQ_GL_EXPIRATION",
        "rule_type": "policy_expiration",
        "description": "Commercial General Liability policy must be currently active (not expired)",
        "severity": "critical",
        "coverage_type": "general_liability",
    },
    {
        "requirement_id": "REQ_ADDL_INSURED",
        "rule_type": "endorsement_presence",
        "description": "Additional Insured endorsement naming 'General Contractor Corp' or blanket status",
        "severity": "critical",
        "required_endorsement_type": "additional_insured",
        "required_entity_name": "General Contractor Corp",
    },
]

scen_dir = Path("evaluation/cohorts/fixtures/SCEN-01")
with (
    (scen_dir / "SCEN-01_acord25.pdf").open("rb") as coi_fh,
    (scen_dir / "SCEN-01_endorsement_cg2010.pdf").open("rb") as end_fh,
):
    resp = httpx.post(
        "http://127.0.0.1:8000/api/v1/compliance/review-documents",
        data={
            "submission_name": "Scenario A - Compliant Policy Packet (Demo)",
            "evaluation_date": "2025-06-15",
            "provider": "mock",
            "requirements": json.dumps(requirements),
        },
        files=[
            ("coi_file", ("SCEN-01_acord25.pdf", coi_fh, "application/pdf")),
            ("endorsement_files", ("SCEN-01_endorsement_cg2010.pdf", end_fh, "application/pdf")),
        ],
        timeout=30.0,
    )

resp.raise_for_status()
data = resp.json()
print("HTTP:", resp.status_code)
print("submission_id:", data["submission_id"])
print("overall_status:", data["overall_status"])
print("requires_human_review:", data["requires_human_review"])
print("provider:", data["provider"])
print("subcontractor_name:", data["subcontractor_name"])
print("Inspect in browser:", f"http://127.0.0.1:8000/submissions/{data['submission_id']}")
for f in data["review_result"]["findings"]:
    ev = (f.get("evidence") or [{}])[0]
    print(f"  - {f['requirement_id']}: {f['status']} ({f['reason_code']}) | quote={ev.get('source_text')!r}")
'@ | .\.venv\Scripts\python.exe -
```

### Equivalent `curl.exe` Command (Alternative)
If presenting with `curl.exe` (first save the `requirements` JSON array above to `$env:TEMP\scen_a_reqs.json`):

```powershell
curl.exe -s -X POST "http://127.0.0.1:8000/api/v1/compliance/review-documents" `
  -F "submission_name=Scenario A - Compliant Policy Packet (Demo)" `
  -F "evaluation_date=2025-06-15" `
  -F "provider=mock" `
  -F "requirements=<$env:TEMP\scen_a_reqs.json" `
  -F "coi_file=@evaluation/cohorts/fixtures/SCEN-01/SCEN-01_acord25.pdf;type=application/pdf" `
  -F "endorsement_files=@evaluation/cohorts/fixtures/SCEN-01/SCEN-01_endorsement_cg2010.pdf;type=application/pdf"
```

### Expected API Output & Status Values
- **Top-level fields:**
  - `"submission_id"`: `"sub_<12 hex chars>"`
  - `"overall_status"`: `"requires_human_review"`
  - `"requires_human_review"`: `true`
  - `"provider"`: `"mock"`
  - `"evaluation_date"`: `"2025-06-15"`
  - `"subcontractor_name"`: `"Apex Electrical Contractors LLC"`
  - `"pipeline_errors"`: `[]`
- **Finding breakdown (`review_result.findings`):**

| Requirement ID | Status | Reason Code | Key Extracted / Attributed Value |
| :--- | :--- | :--- | :--- |
| `REQ_NAMED_INSURED` | `compliant` | `NAMED_INSURED_EXACT_MATCH` | `Apex Electrical Contractors LLC` |
| `REQ_GL_EACH_OCC` | `compliant` | `LIMIT_MEETS_OR_EXCEEDS_MINIMUM` | `$1,000,000` (`100000000` cents) |
| `REQ_GL_GEN_AGG` | `compliant` | `LIMIT_MEETS_OR_EXCEEDS_MINIMUM` | `$2,000,000` (`200000000` cents) |
| `REQ_GL_EFFECTIVE_DATE` | `compliant` | `EFFECTIVE_DATE_VALID` | `2025-01-01` |
| `REQ_GL_EXPIRATION` | `compliant` | `POLICY_ACTIVE_AND_CURRENT` | `2026-01-01` |
| `REQ_ADDL_INSURED` | `requires_human_review` | `MANUSCRIPT_VERIFICATION_UNSUPPORTED` | `CG 20 10` / `CG 20 10 04 13` (escalated) |

---

## 5. Complete Browser Navigation & Route Map (Verified)

| Route | Nav Label / Trigger | Purpose |
| :--- | :--- | :--- |
| `GET /dashboard` | **Dashboard** (`/` redirects here via `302`) | Aggregate counts (`stats-grid`) & recent submissions table |
| `GET /submissions/new` | **+ New Review** | Multipart PDF upload UI (`provider="mock"` hidden field) — *avoid preset chips* |
| `GET /submissions/{id}` | Submission link / **Inspect** / **Reopen Result** | Uploaded PDFs, extracted entities & limits, deterministic findings |
| `GET /submissions/{id}/evidence` | **🔍 Evidence Trace** button | Per-field grounded citations, coverage provenance, bounding boxes |
| `GET /submissions/{id}/review` | **✍️ Human Review Decision** button | Review items, decision form, guardrail error banner, audit submission |
| `GET /submissions` | **History** | Search by `name` / `subcontractor_name` (`q`) and filter by `status` |

**How to Verify Mock Provider Mode in the Browser:**
1. Top-right navigation pill in `base.html`: **`Provider: Mock Mode (Offline)`**.
2. Footer notice in `base.html`: **`Local evaluation environment • Zero external network requests • Strict deterministic verification`**.
3. Submission Detail header (`/submissions/{id}`): **`Provider: mock`**.

---

## 6. Timed 8–10 Minute Presentation Sequence

### Part A — Introduction (0:00 – 1:00)

- **Action:** Show `http://127.0.0.1:8000/dashboard` in the browser.
- **Narration:**
  > "General contractors have to verify subcontractor Certificates of Insurance and policy endorsements before work starts on site. Doing that manually—reading multi-page ACORD 25 forms, matching entity suffixes like LLC versus Inc, comparing limits against contract thresholds, and checking endorsement wording—is slow and error-prone. On the other hand, letting an LLM issue a bare 'pass/fail' judgment creates serious contractual risk if it hallucinates a limit or misses an exclusion. SubGuard AI solves this with a strict separation of duties."
- **Checkpoint:** Dashboard loads cleanly with isolated stats and `Provider: Mock Mode (Offline)` visible in the header.

---

### Part B — Architecture (1:00 – 2:00)

- **Action:** Point to the footer tagline (*"AI extracts. Deterministic code evaluates. Humans resolve uncertainty."*) and briefly click **+ New Review** (`/submissions/new`) to show the upload profile (**do not** click the `⚡ Bundled Demo Scenarios` preset buttons).
- **Narration:**
  > "Our architecture enforces five deterministic stages. First, PyMuPDF and optional Tesseract OCR extract page text and coordinates. Second, an extraction provider pulls structured fields—today we're running strictly offline with our local mock provider, not a live AI model. Third, Python normalizes amounts into integer cents, checks dates, and verifies that every quoted citation actually appears as a substring on the cited page. Fourth, a deterministic Python compliance evaluator and cross-document reconciliation engine evaluate contract rules. Finally, anything ambiguous or unverified is routed to a qualified human reviewer instead of auto-passing."
- **Checkpoint:** Viewer understands that extraction, evidence verification, deterministic rules, reconciliation, and human review are decoupled layers.

---

### Part C — Verified Scenario A Submission & Deterministic Findings (2:00 – 4:00)

- **Action:** Run the **Section 4 Python `httpx` command** in Terminal 2 (or paste the `submission_id` URL printed by it) and open `http://127.0.0.1:8000/submissions/{submission_id}` in the browser (also refresh `/dashboard` afterward if desired).
- **Narration:**
  > "We just submitted Scenario A—two bundled synthetic PDFs: `SCEN-01_acord25.pdf` and `SCEN-01_endorsement_cg2010.pdf`—against a 6-rule requirement profile evaluated on `2025-06-15` using `provider='mock'`. Look at the Submission Detail page: under Extracted Policy Information and Coverage Limits, SubGuard extracted Named Insured `Apex Electrical Contractors LLC`, General Liability `GL-98765432` with `$1,000,000` Each Occurrence, `$2,000,000` General Aggregate, and policy term `2025-01-01` to `2026-01-01`. Under Deterministic Compliance Findings, all five of those requirements evaluated to `compliant`: `NAMED_INSURED_EXACT_MATCH`, `LIMIT_MEETS_OR_EXCEEDS_MINIMUM`, `EFFECTIVE_DATE_VALID`, and `POLICY_ACTIVE_AND_CURRENT`."
  >
  > "Now look at the overall status banner: **`requires human review`**. Even though all five insured, limit, and date checks passed, `REQ_ADDL_INSURED` returned `MANUSCRIPT_VERIFICATION_UNSUPPORTED`. The system refuses to issue an automated clean pass because the offline mock pipeline does not claim cryptographic or semantic verification of manuscript additional-insured endorsement scope."
- **Checkpoint:** Browser shows `overall_status = requires human review`, `Provider: mock`, 2 uploaded PDFs (`processed`), and 5 `compliant` findings + 1 `requires_human_review` finding.

---

### Part D — Field-Level Evidence Traceability (4:00 – 6:00)

- **Action:** Click **🔍 Evidence Trace** (`GET /submissions/{submission_id}/evidence`). Scroll to the **General Liability** rows inside *Extracted Fields & Grounded Citations*.
- **Narration:**
  > "Earlier in testing, a coverage-level citation could be reused across multiple findings—for instance, showing an occurrence quote under General Aggregate or Policy Expiration. Each monetary limit and date field now carries its own verified citation. Here on the Evidence Trace page, **Each Occurrence** (`$1000000`) cites `"EACH OCCURRENCE: $1,000,000"` on Page 1; **General Aggregate** (`$2000000`) cites `"GENERAL AGGREGATE: $2,000,000"` on Page 1; and **Effective Date** (`2025-01-01`) and **Expiration Date** (`2026-01-01`) both cite `"POLICY EFF: 01/01/2025   POLICY EXP: 01/01/2026"` on Page 1. Below those field rows, coverage-level provenance is displayed separately as `General Liability (Policy / Section)` so policy metadata never masquerades as proof for an individual limit."
  >
  > "Notice also what metadata this document exposes: each verified quote includes `BBox: [...]` normalized page coordinates extracted from the native-digital PDF text layer. Because `SCEN-01_acord25.pdf` and `SCEN-01_endorsement_cg2010.pdf` are native-digital PDFs rather than scanned images, you do **not** see an OCR badge here—those `OCR (...)` badges appear only when Tesseract processes a scanned page (`ocr_extracted`)."
- **Checkpoint:** Four distinct field rows (`Each Occurrence`, `General Aggregate`, `Effective Date`, `Expiration Date`) display exact field-specific quotes (`✓ Grounded`) plus `BBox` coordinates; no OCR badge is claimed on native-digital Scenario A.

---

### Part E — Human Review Queue & Guardrails (6:00 – 7:30)

- **Action:**
  1. Click **History** (`/submissions`), select **Requires Review** in the status dropdown, and click **Filter** (`/submissions?status=requires_human_review`) to show the review queue.
  2. Click back into the Scenario A submission -> **✍️ Human Review Decision** (`/submissions/{submission_id}/review`).
  3. Inspect the left column (*Review Items & Reason Codes*: `REQ_ADDL_INSURED` / `MANUSCRIPT_VERIFICATION_UNSUPPORTED`) and the right form (*Record Compliance Decision*).
  4. **Safe Live Guardrail Demo (Zero DB Mutation — Recommended):**
     - Leave *Decision Action* as **Waive Requirement** (`waived`), leave *Target Requirement* as `-- Apply to entire submission / flags --` (blank `rule_id`), enter `"Demo guardrail check"` in *Audit Notes*, leave *Update Overall Submission Status* as `Keep current status`, and click **Save Decision to Audit Trail**.
     - Observe `#reviewErrorBox`: **`⚠️ Waiver action requires specifying a target rule_id or finding_id.`** (`HTTP 422`).
     *(Alternative if showing a persisted audit entry inside the isolated temp DB: select **Issue Deficiency Notice to Subcontractor** (`deficiency_notice_required`), enter `"Request verified CG 20 10 endorsement review copy"`, keep `Update Overall Submission Status = Keep current status (requires_human_review)`, submit, and show the new entry in **Recorded Human Review Actions**. Explain that `review_records` are append-only in SQLite with no delete endpoint, and remain confined to the temp demo DB.)*
- **Narration:**
  > "Humans resolve what deterministic rules flag—here, `REQ_ADDL_INSURED` with `MANUSCRIPT_VERIFICATION_UNSUPPORTED`. Notice the guardrails enforced by `/api/v1/submissions/{id}/review`: audit notes and reviewer identity are mandatory; deterministic hard failures (`non_compliant`) are blocked from blanket `accepted_exception` approval with HTTP 422; waivers require a specific target rule and forbid waiving non-waivable rules like expiration dates or named insured identity; and when we try to submit an untargeted waiver, the backend rejects it cleanly without mutating the record. We never approve or force a green status just to make a demo look clean—original automated findings stay permanently preserved in `result_json`."
- **Checkpoint:** Review queue (`?status=requires_human_review`), `MANUSCRIPT_VERIFICATION_UNSUPPORTED` reason code, and deterministic guardrail rejection are demonstrated without improperly approving the submission.

---

### Part F — SQLite Persistence & History Retrieval (7:30 – 8:30)

- **Action:**
  1. Click **Dashboard** (`/dashboard`) in the top navigation bar to leave the submission view (`Total Submissions: 1`, `Requires Review: 1`).
  2. Click **History** (`/submissions`).
  3. Type **`Apex Electrical`** (or **`Scenario A`**) in the search box (`name="q"` — note that History searches submission `name` and `subcontractor_name`, **not** `submission_id`) and click **Filter**.
  4. Locate the `sub_<id>` row and click **Reopen Result** to return to `/submissions/{submission_id}` and verify uploaded PDFs, extracted limits, and findings reload intact from SQLite.
- **Narration:**
  > "Navigating back to the Dashboard and into Submission History, all records live in our isolated SQLite database. Searching History by subcontractor name—`Apex Electrical`—retrieves the exact `sub_...` submission, and clicking `Reopen Result` restores all uploaded documents, extracted values, and deterministic findings."
- **Checkpoint:** Search by subcontractor/submission name retrieves the record and reloads `/submissions/{submission_id}` with identical findings and evidence.

---

### Part G — Mandatory Limitations & Readiness Disclosure (8:30 – 9:30)

- **Action:** Remain on `/submissions/{id}` or return to `/dashboard` for the closing summary.
- **Narration (must cover all 6 disclosures verbatim in substance):**
  > "To be transparent about current system maturity and boundaries:
  > 1. Today's demonstration ran strictly on the **offline mock extraction provider** (`provider='mock'`).
  > 2. **Live AI-provider parity** (Gemini or OpenRouter multimodal extraction against real-world packets) has **not** been established.
  > 3. **Manuscript and blanket additional-insured endorsements** are intentionally escalated to human review rather than auto-approved.
  > 4. On our 40-scenario synthetic cohort benchmark with the mock runner, overall classification accuracy is **50.00% (`20/40`)** with a **70.00% (`28/40`) human-review routing rate**—preserving a `0.00%` False Compliance Rate at the cost of high human escalation.
  > 5. Because of these mock-extraction and endorsement limits, the system is **not ready for unsupervised production deployment**.
  > 6. Finally, this build has **no authentication or role-based access control (RBAC)** and must remain **localhost-only** during demonstrations."

---

## 7. Exact Cleanup & Safe Shutdown

### Step 7.1 — Stop the Server (Terminal 1)
1. Focus **Terminal 1** running Uvicorn.
2. Press **`Ctrl+C`**.
3. Wait for `INFO:     Shutting down` and `INFO:     Finished server process`.

### Step 7.2 — Remove Temporary Demo Database & Clear Session Environment (Terminal 1)

```powershell
# Remove the temporary demo database and uploaded PDFs
if ($demoRoot -and (Test-Path $demoRoot)) {
    Remove-Item -Recurse -Force $demoRoot
}

# Clear session-scoped environment variables
Remove-Item Env:SUBGUARD_DB_PATH -ErrorAction SilentlyContinue
Remove-Item Env:SUBGUARD_UPLOAD_DIR -ErrorAction SilentlyContinue
Remove-Item Env:EXTRACTION_PROVIDER -ErrorAction SilentlyContinue
```

*Repository `backend/data/subguard.db` and `backend/data/uploads/` remain untouched.*

---

## 8. Fallback Plan

| Failure Mode | Symptom | Immediate Action |
| :--- | :--- | :--- |
| **Port `8000` already in use** | `OSError: [Errno 10048]` on startup or `Get-NetTCPConnection` finds a listener | Restart Uvicorn on `--port 8010` and change `127.0.0.1:8000` -> `127.0.0.1:8010` in the Terminal 2 Python command and browser tabs. **Never** run against an unknown existing server pointing to another DB. |
| **API command fails (`422` / `400` / connection refused)** | Terminal 2 `httpx.post` errors | Verify Terminal 1 Uvicorn process is running and `GET /health` returns `ok`. Confirm current working directory is `d:\Documents\DOCUMENTS\Project-Antigravity\backend` so relative PDF paths resolve. |
| **Stale preset chip clicked accidentally** | URL navigates to a `demo_scen-01_*` submission instead of Scenario A custom upload | Ignore that row; re-run the Section 4 `httpx.post` script and open the returned `submission_id` URL, or start a fresh `$demoRoot` folder. |
| **Web UI upload fallback (if API script fails live)** | Terminal 2 Python snippet unavailable | Open `/submissions/new`, fill *Submission Reference* = `"Scenario A Fallback"`, *Evaluation Date* = `2025-06-15`, select `SCEN-01_acord25.pdf` (`#coi_file`) and `SCEN-01_endorsement_cg2010.pdf` (`#endorsement_files`), and click **Evaluate Compliance**. *(Note: uses the 5 default rules, omitting `REQ_GL_EFFECTIVE_DATE` as a separate finding card while still displaying `2025-01-01 -> 2026-01-01` in Coverage Limits.)* |
| **History search returns empty** | Presenter typed `sub_...` ID into `/submissions` search bar | `list_submissions` queries `name` and `subcontractor_name` only—clear the box (`Clear` link) or search `"Apex Electrical"` / `"Scenario A"`. |
| **Browser UI fails to load assets** | Broken layout or `HTTP 500` | Check Terminal 1 traceback; confirm `Env:SUBGUARD_DB_PATH` points to a writable folder in `$env:TEMP` and restart Uvicorn cleanly. |

---

## 9. Claims the Presenter Must NOT Make

Do **not** state or imply any of the following during the demonstration or Q&A:
1. ❌ *"The demo is calling Gemini / OpenRouter / a live LLM right now."* (It uses offline `HonestApplicationMockProvider`.)
2. ❌ *"Live AI extraction parity has been verified."* (It has not.)
3. ❌ *"SubGuard AI automatically approves standard policies end-to-end without humans."* (Benchmark automated pass rate is `0.00%`; `70.00%` route to review; `SCEN-01` routes to review.)
4. ❌ *"SubGuard AI automatically verifies manuscript or custom endorsement wording."* (`MANUSCRIPT_VERIFICATION_UNSUPPORTED` escalates to humans.)
5. ❌ *"The system is production-ready or multi-tenant secure."* (Not ready for unsupervised production; zero authentication/RBAC.)
6. ❌ *"Scenario A shows live Tesseract OCR badges."* (`SCEN-01` PDFs are native-digital and show PyMuPDF bounding boxes, not OCR badges.)

---

## 10. Concise Technical Q&A

- **Q1: Why did Scenario A return `requires_human_review` when all limits, dates, and the named insured were `compliant`?**  
  **A:** Under our fail-closed compliance rules, a submission is `COMPLIANT` only when **every** requirement passes. `REQ_ADDL_INSURED` returned `MANUSCRIPT_VERIFICATION_UNSUPPORTED` because verifying whether an additional-insured endorsement truly covers the certificate holder without hidden restrictions cannot be rubber-stamped by regex/mock extraction.
- **Q2: If you're using `provider="mock"`, is anything real happening with the PDF?**  
  **A:** Yes. The application stores and hashes the uploaded bytes, inspects the PDF pages with PyMuPDF (`PDFProcessor`), runs `HonestApplicationMockProvider` over the extracted page text, verifies exact substring quotes and page bounding boxes (`EvidenceReference`), executes the deterministic `ComplianceEvaluator` and `reconcile_extracted_document_set` in Python, and persists the full record to SQLite. Only the LLM call is mocked.
- **Q3: How do you prevent an AI extractor from attaching occurrence text as proof for general aggregate or expiration dates?**  
  **A:** `RawCoverageSection`, `MoneyValue`, and `DateValue` carry dedicated field-level evidence pointers (`each_occurrence_evidence`, `general_aggregate_evidence`, `effective_date_evidence`, `expiration_date_evidence`), and `ComplianceEvaluator` refuses to fall back to generic occurrence or policy evidence when a field's own citation is missing under `mandatory_evidence=True`.
- **Q4: Why is the mock cohort benchmark accuracy only 50%?**  
  **A:** Two reasons: (1) our evaluation safety policy routes missing-evidence and unverified-endorsement cases to human review rather than guessing, and (2) `HonestApplicationMockProvider` / `ScenarioBenchmarkMockProvider` conservatively flags uncertain packets (`70.00%` human-review routing rate), achieving `0.00%` False Compliance and `0.00%` Unsafe Compliance at the expense of automation rate.
- **Q5: Can a reviewer override a failed limit check (`LIMIT_BELOW_MINIMUM`) by just clicking "Approve"?**  
  **A:** No. `POST /api/v1/submissions/{id}/review` inspects the persisted findings and returns `HTTP 422` if `review_status="accepted_exception"` or `update_submission_status="compliant"` is attempted while any `non_compliant` finding exists. Non-waivable rules (`POLICY_EXPIRATION`, `POLICY_EFFECTIVE_DATE`, `NAMED_INSURED`) also reject waiver attempts.
- **Q6: Can SubGuard AI be deployed on a shared internal network today?**  
  **A:** Not as-is. It has no user authentication, session management, or RBAC, and must remain bound to `127.0.0.1` until auth and security hardening are added.

---

## 11. One-Page Presenter Quick-Reference Checklist

| Order | Step | Command / URL / Control | Pass Checkpoint |
| :---: | :--- | :--- | :--- |
| **1** | **Check Port 8000** | `Get-NetTCPConnection -LocalPort 8000 -State Listen -EA SilentlyContinue` | Empty output (no port conflict) |
| **2** | **Isolate Env & Start** | Set `$env:SUBGUARD_DB_PATH`, `$env:SUBGUARD_UPLOAD_DIR`; unset `EXTRACTION_PROVIDER`; run `uvicorn app.main:app --host 127.0.0.1 --port 8000` | `Uvicorn running on http://127.0.0.1:8000` |
| **3** | **Verify Health & Mode** | `Invoke-RestMethod http://127.0.0.1:8000/health` & `/api/v1/dashboard/stats` | `status=ok`, `total_submissions=0` |
| **4** | **Open Dashboard (Part A)** | Browser -> `http://127.0.0.1:8000/dashboard` | Header badge shows `Provider: Mock Mode (Offline)` |
| **5** | **Show Upload UI (Part B)** | Click `+ New Review` (`/submissions/new`) — **do NOT click preset chips** | Footer confirms `Zero external network requests` |
| **6** | **Submit Scenario A (Part C)** | Run Section 4 `httpx` script in Terminal 2; open `/submissions/{id}` | `overall_status = requires_human_review`, 5 `compliant`, `REQ_ADDL_INSURED` flagged |
| **7** | **Verify Field Evidence (Part D)** | Click `🔍 Evidence Trace` (`/submissions/{id}/evidence`) | Separate quotes for Each Occurrence, General Aggregate, Effective & Expiration Dates + `BBox` *(no OCR badge on SCEN-01)* |
| **8** | **Review Guardrail (Part E)** | Open `/submissions?status=requires_human_review` -> `/submissions/{id}/review` -> submit `waived` with empty `rule_id` | Red banner rejects with `Waiver action requires specifying a target rule_id` (`HTTP 422`) |
| **9** | **Verify Persistence (Part F)** | Click `Dashboard` -> `History` -> search `Apex Electrical` -> `Reopen Result` | Record & all findings reload from isolated SQLite DB |
| **10** | **Disclosures (Part G)** | State all 6 limitations (mock only, no live AI parity, manuscript escalation, 50% acc / 70% review rate, non-prod, no auth/RBAC) | Zero overclaims |
| **11** | **Shutdown & Clean** | `Ctrl+C` in Terminal 1; `Remove-Item -Recurse -Force $demoRoot`; clear env vars | Default `backend/data/` untouched |
