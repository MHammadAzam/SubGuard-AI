# SubGuard AI

**Contractor Insurance Compliance Evaluation and Field-Level Evidence Traceability System.**

SubGuard AI automates the review of subcontractor Certificates of Insurance (ACORD 25 COIs) and supporting policy endorsements against contractual insurance requirements.

> **Core Architectural Principle:**  
> **AI extracts information. Deterministic Python rules evaluate it. Humans resolve uncertainty.**

---

## Implemented Architecture (Single-Tenant Pilot)

- **Backend & Web UI:** Python 3.12+ · FastAPI · Jinja2 templates · vanilla JavaScript (CSP nonced, zero `localStorage` tokens)
- **Persistence:** Local SQLite (`WAL` mode, foreign keys enforced, `PRAGMA user_version` schema tracking) + confined local PDF storage (`SUBGUARD_UPLOAD_DIR`)
- **PDF & OCR Pipeline:** PyMuPDF digital text/span extraction + local Tesseract OCR (`TesseractOCRProvider`) for scanned/skewed pages with normalized bounding boxes
- **Extraction Providers:** Offline deterministic `HonestApplicationMockProvider` (`mock` default), plus optional gated `openrouter` (`:free` models only) and `gemini` providers disabled unless explicitly enabled in `SUBGUARD_ALLOWED_PROVIDERS`
- **Deterministic Compliance Engine:** Integer-cents currency normalization, exact/alias legal-entity verification, cross-document reconciliation, explicit uncertainty surfacing (`COMPLIANT`, `NON_COMPLIANT`, `REQUIRES_HUMAN_REVIEW`, `UNABLE_TO_DETERMINE`)
- **Security Controls:** `hashlib.scrypt` (`N=131072, r=8, p=1`, bounded concurrency + rate limiting), `HttpOnly`/`SameSite=Lax`/`Secure` cookie sessions + synchronizer CSRF, revocable SHA-256 hashed Bearer API tokens, RBAC (`viewer`, `analyst`, `reviewer`, `admin`), server-derived reviewer attribution, atomic append-only audit trail (`review_records`)

---

## Clean Setup & Single-Worker Pilot Deployment

1. **Prerequisites:** Python `3.12+` and Tesseract OCR installed locally (`tesseract` on `PATH`, or `C:\Program Files\Tesseract-OCR\tesseract.exe`, or `TESSERACT_CMD` configured).
2. **Install Backend Environment:**
   ```bash
   cd backend
   python -m venv .venv
   # Activate .venv (Windows PowerShell: .\.venv\Scripts\Activate.ps1)
   pip install -e . -c requirements.lock
   # Optional if using Playwright browser tests or external LLM SDKs:
   # pip install -e ".[dev,providers]" -c requirements.lock
   ```
3. **Configure Environment:** Copy `backend/.env.example` and configure explicit storage and security values (`SUBGUARD_ENV`, `SUBGUARD_SESSION_SECRET`, `SUBGUARD_DB_PATH`, `SUBGUARD_UPLOAD_DIR`, `SUBGUARD_AUDIT_LOG_PATH`).
4. **Provision Initial Administrator (No Default Credentials Exist):**
   ```bash
   cd backend
   python -m app.security.bootstrap create-admin --username pilot_admin --display-name "Pilot Admin"
   ```
5. **Run Single-Worker Uvicorn Behind TLS Reverse Proxy:**
   - Single-process deployment is required for in-memory rate limiting (`SlidingWindowRateLimiter`, `_BoundedScryptGate`, `_BoundedProcessingGate`). Run with `--workers 1` (default) behind nginx/Caddy TLS termination with `SUBGUARD_TRUSTED_PROXIES` configured.
   ```bash
   cd backend
   python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

See [backend/README.md](./backend/README.md) for backup/restore commands, provider configuration, resource limits, and test execution.

---

## Pilot Limitations

- Single-tenant pilot instance (`viewer`+ accounts share visibility across submissions within the instance; per-organization ACLs are not implemented).
- Single-worker local SQLite persistence and in-process rate/concurrency limiters (not multi-worker or multi-node HA).
- Self-service password reset and MFA are outside current pilot scope.

---

## Disclaimer

SubGuard AI is an administrative document-review automation tool. It does not provide legal advice or replace qualified risk/insurance review on flagged findings (`REQUIRES_HUMAN_REVIEW`, `UNABLE_TO_DETERMINE`).
