# SubGuard AI — Engineering Case Study

**Project:** SubGuard AI (AI-Assisted Subcontractor Insurance Compliance Engine)
**Repository:** [github.com/MHammadAzam/SubGuard-AI](https://github.com/MHammadAzam/SubGuard-AI)
**Portfolio:** [hammadazam.me](https://hammadazam.me)
**Core Invariant:** *"AI extracts information. Deterministic Python rules evaluate it. Humans resolve uncertainty."*

---

## 1. Project Introduction

SubGuard AI is a local-first web application and deterministic compliance engine designed to evaluate subcontractor Certificates of Insurance (ACORD 25 COIs) and supporting policy endorsements (such as Additional Insured `CG 20 10` and Waiver of Subrogation `CG 24 04`) against contractual coverage requirements.

Rather than building an opaque "AI wrapper" that outputs a binary pass/fail answer from a prompt, this project treats document extraction, financial/date normalization, cross-document reconciliation, rule evaluation, and human auditability as distinct engineering layers with explicit contracts between them.

---

## 2. Problem and Motivation

Construction general contractors and compliance teams routinely review multi-PDF insurance packets submitted by subcontractors before allowing crews on-site. In practice, that review process breaks down in predictable ways:

1. **Multi-document fragmentation:** Limits may appear on an ACORD 25 certificate while qualifying exclusions, blanket clauses, or conflicting policy numbers live in separate endorsement PDFs.
2. **High-consequence edge cases:** A missing corporate suffix mismatch (`Apex Electrical LLC` vs. `Apex Electrical Inc`), an expired coverage line, a `$500,000` limit written informally as `$500K`, or an ACORD checkbox marked without an attached endorsement can invalidate risk transfer.
3. **Audit & accountability requirements:** Compliance teams cannot approve a subcontractor based on an ungrounded model summary; they need the exact document, page number, and quoted text supporting each requirement—and an immutable record showing whether an automated rule passed or a human reviewer made a judgment call.

---

## 3. Why a Pure-LLM Approach Was Insufficient

Early in designing SubGuard AI, delegating end-to-end compliance judgment to a Vision-Language Model (VLM) created unacceptable failure modes for a compliance workflow:

- **Arithmetic & format fragility:** Comparing `$1,000,000` vs. `1M` vs. `$500,000 / $1,000,000` split limits or evaluating policy inception/expiration boundaries against an evaluation date cannot rely on probabilistic token prediction or floating-point rounding.
- **Silent hallucination risk:** If an LLM misreads an illegible scan or invents a quote to justify an endorsement, a pure-LLM evaluator can produce a confident false approval (`COMPLIANT` on an uninsured contractor).
- **Non-determinism & legal defensibility:** Compliance reviews require reproducibility—running the same extracted evidence against the same requirement profile must yield the exact same finding, reason code, and escalation path every time.

SubGuard AI solves this by confining AI strictly to **structured candidate extraction with source citations**, while keeping **normalization, verification, reconciliation, and compliance status computation 100% deterministic in Python**.

---

## 4. Architecture and Data Flow

Every uploaded insurance packet flows through an explicit pipeline (`backend/app/`):

```text
PDF Upload (documents/review_service.py)
  -> Security checks (extension, %PDF- magic bytes, per-file/cumulative byte limits)
  -> PDFProcessor (documents/processor.py: PyMuPDF + pypdf text spans & coordinates)
  -> Scanned-page fallback (extraction/ocr.py: TesseractOCRProvider + numpy deskew)
  -> ExtractionPipelineService (extraction/pipeline.py)
       -> Swappable BaseExtractionProvider (mock default; optional gated gemini/openrouter)
       -> Deterministic normalizer (extraction/normalizer.py: integer cents, ISO dates, enums)
       -> Quote & semantic evidence grounding check (prevents ungrounded hallucinations)
  -> Cross-Document Reconciliation (compliance/reconciliation.py)
       -> Policy-number reconciliation, entity identity matching, discrepancy detection
  -> Deterministic ComplianceEvaluator (compliance/evaluator.py)
       -> Per-finding status + projected submission status + immutable result_json
  -> SQLite WAL + Confined Storage + Human Review (persistence/repository.py, main.py)
       -> Append-only review_records with server-derived reviewer attribution
```

*(Browser UI screenshots from Milestone 2.2 local synthetic verification can be inspected in [evaluation/reports/browser_screenshots/](../evaluation/reports/browser_screenshots/).)*

---

## 5. Technical Implementation

- **API & Server-Rendered UI (`app/main.py`, `app/templates/`):** FastAPI serves both JSON API routes (`/api/v1/...`) and Jinja2 templates with per-request CSP script nonces (`default-src 'self'`), avoiding client-side `localStorage` token storage.
- **Strict Pydantic Contracts (`app/schemas/`):** `RawInsuranceExtraction` separates untrusted provider output from validated domain schemas (`ExtractedDocumentSet`, `ComplianceReviewResult`, `ComplianceFinding`, `CrossDocumentReconciliationResult`).
- **PDF & OCR Layer (`app/documents/processor.py`, `app/extraction/ocr.py`):** Digital PDFs retain exact text spans and normalized bounding boxes from PyMuPDF; textless/rotated scans trigger local Tesseract OCR with `numpy`-based orientation/skew detection.
- **Persistence (`app/persistence/database.py`, `repository.py`):** SQLite configured with `PRAGMA foreign_keys = ON`, `PRAGMA journal_mode = WAL`, explicit `PRAGMA user_version` schema tracking (`CURRENT_SCHEMA_VERSION = 2`), and atomic transactional updates for review decisions.

---

## 6. Key Engineering Decisions and Trade-Offs

### 1. Separating AI Extraction from Deterministic Rule Evaluation
- **Decision:** Providers never compute `COMPLIANT` or `NON_COMPLIANT`. `ComplianceEvaluator` (`app/compliance/evaluator.py`) runs pure Python logic with zero network/DB side effects.
- **Trade-off:** Requires maintaining explicit schema contracts and normalizer rules, in exchange for complete testability (`pytest` unit tests run offline in milliseconds) and reproducible compliance verdicts.

### 2. Integer-Cent Financial Representation
- **Decision:** `normalize_currency_to_cents()` (`app/extraction/normalizer.py`) uses `Decimal` arithmetic to convert `$1,000,000` or `1M` into exact integer cents (`100_000_000`) before comparison, and immediately flags split/dual formats (`$1,000,000 / $2,000,000`), negative accounting values, or ambiguous separators (`AMBIGUOUS_NUMBER_SEPARATORS`) as uncertain instead of guessing.
- **Trade-off:** Rejects messy unparsed strings to human review rather than applying permissive regex heuristics, favoring safety over forced automation.

### 3. Strict Date and Entity Normalization
- **Decision:** `normalize_date()` parses explicit date formats into `datetime.date` objects without silently defaulting ambiguous numeric orders, while `_split_core_and_suffix()` + `compare_entity_names()` normalize punctuation/abbreviations (`L.L.C.` → `llc`) without conflating distinct legal forms (`LLC` ≠ `Inc`).
- **Trade-off:** Corporate suffix discrepancies route to `REQUIRES_HUMAN_REVIEW` rather than auto-passing fuzzy string similarity.

### 4. Evidence-Backed Field Grounding
- **Decision:** Every extracted value carries `EvidenceReference` citations verified against actual PDF text layers (`pipeline.py`). If a provider quotes text that does not appear on the cited page or fails semantic support for the claimed value, extraction status downgrades to `QUOTE_PRESENT_UNVERIFIED_SUPPORT` or `UNCERTAIN`.
- **Trade-off:** Slightly higher human-review rate when OCR text has minor spacing noise, eliminating unverified "black-box" approvals.

### 5. Policy-Scoped Cross-Document Reconciliation
- **Decision:** `app/compliance/reconciliation.py` builds a deterministic relationship and identity graph across uploaded COIs, endorsements, and declaration pages, using a 5-tier policy identifier classifier (`RENEWAL_SUFFIX_REGEX` distinguishes known renewal suffixes while treating arbitrary leading-zero or prefix differences as conflicting/ambiguous).
- **Trade-off:** Conservative matching prevents mistakenly linking unrelated policies across documents.

### 6. Human Review with Append-Only Audit History & Non-Waivable Critical Rules
- **Decision:** `submissions.result_json` is immutable after automated evaluation. Review actions insert rows into `review_records`, recalculate `overall_status` server-side by replaying finding state (`_replay_finding_states`), bind `reviewer_user_id` / `reviewer_name` exclusively to the server-authenticated user, and reject waivers (`HTTP 422`) on critical requirements (`POLICY_EXPIRATION`, `POLICY_EFFECTIVE_DATE`, `NAMED_INSURED`, `POLICY_EXPIRED`, `NAMED_INSURED_MISMATCH`, `COVERAGE_NOT_FOUND`).
- **Trade-off:** Reviewers must resolve or reject per finding cleanly and cannot silently override expired policies with a blanket submission approval.

### 7. Online SQLite Backup, Verification & Staged Restore
- **Decision:** `app/persistence/backup.py` avoids raw file copies of live `-wal` files by using `sqlite3.Connection.backup()` paired with SHA-256 upload manifests, strict path-confinement validation (`_extract_safe_doc_rel_parts`), and non-destructive staged restore (`refuses to overwrite existing target DB or non-empty upload dir`).
- **Trade-off:** Assumes single-node filesystem persistence and requires stopping the application before swapping restored target directories.

### 8. Local-Only Direct HTTPS Pilot Architecture
- **Decision:** In Phase 7A on Windows, running Uvicorn with `--workers 1`, `--no-proxy-headers`, empty `SUBGUARD_TRUSTED_PROXIES`, and direct TLS termination (`127.0.0.1:8443`) preserved production security headers (`HSTS`, `Secure` cookies, disabled `/docs`) while keeping in-process rate limiters accurate.
- **Trade-off:** Direct Uvicorn TLS on a single workstation requires local CA trust onboarding for warning-free browser access, whereas multi-worker/HA production deployments should terminate TLS at a dedicated proxy/load balancer with shared state stores.

---

## 7. Security and Compliance Design

Security hardening focused on closing practical web and pilot risks (`app/security/`):

- **Fail-Closed Production Startup (`config.py`):** When `SUBGUARD_ENV=production`, startup aborts if `SUBGUARD_SESSION_SECRET` is absent/weak (`<32` chars), `SUBGUARD_COOKIE_SECURE` or `SUBGUARD_ENFORCE_HTTPS` is disabled, non-HTTPS CORS origins are configured, or no active `admin` account exists.
- **Password & Token Hygiene (`passwords.py`, `service.py`):** Salted `scrypt` (`N=131072, r=8, p=1`) behind a bounded semaphore (`4` concurrent hashes); opaque session and Bearer tokens stored only as `SHA-256` hashes in SQLite.
- **RBAC & CSRF (`rbac.py`):** Four roles (`viewer`, `analyst`, `reviewer`, `admin`); every cookie-authenticated state change enforces synchronizer CSRF tokens (`X-CSRF-Token` or form field).
- **Provider Cost & Privacy Gate (`providers/__init__.py`, `config.py`):** Server defaults to `SUBGUARD_ALLOWED_PROVIDERS=mock`. Disabled provider requests fail with `HTTP 403` before uploaded bytes are read; OpenRouter enforces `:free`-suffixed models and scrubs API keys/tokens from error messages (`sanitize_error_message`).
- **Path-Confined Downloads (`main.py`):** Downloads verify DB submission association and resolve every directory segment under `SUBGUARD_UPLOAD_DIR/<submission_id>/`, rejecting symlinks and traversal inputs.

---

## 8. Evaluation and Testing Evidence

### Frozen Cohort v1 (`cohort_v1.0.0`, 40 Scenarios / 66 PDFs)
From `backend/evaluation/reports/wp5_cohort_run/post_fix_cohort_v1_mock.md` (offline `mock` baseline):

- **Processing Success:** `40/40` scenarios (`0` crashes)
- **Safety Metrics:** `0.00%` False Compliance Rate (`0/10`), `0.00%` Unsafe Compliance Rate (`0/30`), `100.00%` Missing Evidence Review Rate (`11/11`)
- **Classification Accuracy (4-class):** `50.00%` (`20/40`), `0.4521` Macro F1
- **Routing Breakdown:** `70.00%` (`28/40`) `REQUIRES_HUMAN_REVIEW`, `17.50%` (`7/40`) `NON_COMPLIANT`, `12.50%` (`5/40`) `UNABLE_TO_DETERMINE`, `0.00%` automated `COMPLIANT` pass
- **Evidence Grounding:** `100.00%` quotation exact match (`71/71`), `98.59%` Claim-to-Evidence Support Rate (`70/71`)

*Interpretation:* Under the review-first policy on synthetic fixtures, zero unsafe scenarios passed as `COMPLIANT`, while the mock baseline conservatively routed all `10` ground-truth `COMPLIANT` scenarios into `REQUIRES_HUMAN_REVIEW`. These figures verify deterministic pipeline behavior on synthetic documents, not real-world LLM accuracy.

### Phase 7A Live Direct-HTTPS Acceptance (`https://127.0.0.1:8443`)
Executed end-to-end against a running single-worker Uvicorn TLS instance in `production` + `mock` mode on an isolated disposable recovery-drill clone:

- **17/17 live HTTPS checks passed (`0` failed):** Strict TLSv1.3 verification against local CA + leaf SHA-256 fingerprint match, hostname mismatch & plaintext HTTP rejection, disabled `/docs` & demo routes, rejected `gemini`/`openrouter` providers (`403`), auth/session cookies (`Secure`, `HttpOnly`, `SameSite=Lax`), CSRF enforcement, logout/expired/revoked session rejection, 4-role RBAC enforcement, mock upload/extraction/evaluation/evidence workflow, invalid/oversized file rejection, non-waivable review enforcement & append-only audit immutability, submission-bound downloads & traversal rejection, readiness/health checks, and zero-secret audit log verification.
- **Browser UI over HTTPS:** Playwright browser suite was **not** run over live HTTPS because its test harness spawns an internal HTTP server and Windows CA trust stores were intentionally left unmodified during headless validation.

---

## 9. Challenges Encountered and How They Were Addressed

1. **Missing Evidence vs. Hard Non-Compliance Distinction:** Earlier evaluation logic risked auto-rejecting submissions simply because an endorsement document was missing or incomplete. Introducing `MISSING_EVIDENCE` internally and projecting it to `REQUIRES_HUMAN_REVIEW` under the Milestone 3.2B review-first policy eliminated false auto-rejections on incomplete uploads while preserving `0%` false approvals.
2. **Uvicorn Default `proxy_headers=True` in Direct-TLS Mode:** When pivoting from a reverse-proxy architecture to direct Uvicorn TLS on Windows, testing `uvicorn.Config` revealed that Uvicorn defaults `proxy_headers=True` unless `--no-proxy-headers` is passed explicitly. Adding `--no-proxy-headers` and clearing `SUBGUARD_TRUSTED_PROXIES` ensured untrusted `X-Forwarded-*` headers cannot spoof scheme or client IP.
3. **Windows Endpoint Controls During Local Deployment:** On the pilot workstation, Windows Smart App Control blocked execution of unsigned `caddy.exe`, and consumer antivirus HTTPS scanning (Avast Web/Mail Shield) intercepted loopback TLS sockets with a dynamically substituted root certificate until a narrow `https://127.0.0.1:8443` URL exception was applied. Rather than disabling OS or AV security controls, I switched the local architecture to direct Uvicorn TLS (`--ssl-certfile`/`--ssl-keyfile`), verified wire peer SHA-256 fingerprints strictly against `subguard_local_ca.crt`, and documented clean-host VM separation as a wider-deployment prerequisite.

---

## 10. Current Limitations

- **Controlled local pilot only:** Not cleared for commercial production, multi-tenant hosting, or public internet exposure.
- **Mock-only pilot verification:** Live `gemini` and `openrouter` adapters are gated and unvalidated for production SLA/accuracy.
- **Synthetic dataset coverage:** Cohort v1 metrics measure synthetic test packages rather than scanned field COIs across diverse insurance carriers.
- **Single-worker / SQLite scope:** Process-local rate limiters require `--workers 1`; SQLite WAL supports single-node persistence only.
- **Browser HTTPS walkthrough pending trust onboarding:** Automated HTTPS browser tests were not run, and desktop browsers require installing `subguard_local_ca.crt` into `Cert:\CurrentUser\Root` (or a CA-signed server cert) to suppress self-signed trust warnings.

---

## 11. Engineering Takeaways

1. **Reliability comes from constraining the AI boundary, not tuning prompts alone:** Separating structured extraction from deterministic Python arithmetic, date math, and state machines made compliance failures reproducible, testable, and auditable.
2. **Safety metrics require throughput context:** A `0%` False Compliance Rate on its own can hide a system that rejects or escalates everything (`0%` automated pass rate); measuring FCR alongside human-review routing rate and finding-level accuracy prevents misleading benchmarks.
3. **Operational readiness is part of the architecture:** Backup/restore semantics with SQLite WAL, single-worker constraints on in-memory rate limiters, proxy-header trust boundaries, and Windows endpoint TLS behavior all surfaced concrete engineering decisions that wouldn't appear in unit tests alone.
