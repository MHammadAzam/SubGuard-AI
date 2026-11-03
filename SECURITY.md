# SubGuard AI — Security Design

> **Version:** 0.1.0 (Draft)  
> **Last Updated:** 2026-10-30  
> **Status:** Planning Phase

> [!CAUTION]
> This document identifies security requirements and strategies. It does **NOT** constitute a security audit, penetration test, or compliance certification. A professional security review is recommended before handling production customer data.

---

## 1. Security Overview

### Philosophy

- **Defense in depth** — Multiple layers of protection; no single point of failure
- **Least privilege** — Every component gets only the access it needs
- **Assume breach** — Design so that a single compromised component limits blast radius
- **Fail closed** — On error, deny access rather than grant it
- **Transparency** — Log everything important; make security auditable

---

## 2. Threat Model (STRIDE)

### 2.1 Spoofing (Identity)

| Threat | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Credential stuffing (leaked passwords from other sites) | High | High | Rate limiting on login, bcrypt/argon2 hashing, account lockout |
| Session hijacking (stolen JWT) | Medium | High | Short-lived access tokens (15-30 min), HTTP-only refresh token cookies, token rotation |
| Forged magic link tokens | Low | High | Cryptographically signed tokens, single-use, 15-minute expiry |

### 2.2 Tampering

| Threat | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Modifying uploaded documents after processing | Low | High | Content hash (SHA-256) stored on upload; verify hash before processing |
| API parameter tampering (changing org_id in requests) | Medium | Critical | Never trust client-provided org_id; derive from authenticated session |
| Modifying compliance findings | Low | High | Audit log all changes; require human_override fields |

### 2.3 Repudiation

| Threat | Likelihood | Impact | Mitigation |
|---|---|---|---|
| User denies performing an action | Medium | Medium | Comprehensive audit logging with user_id, timestamp, IP address |
| No record of compliance decisions | Medium | High | Immutable audit_logs table; findings preserve both AI and human values |

### 2.4 Information Disclosure

| Threat | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Cross-tenant data leakage | Low (if coded correctly) | Critical | org_id filtering on every query; RLS as safety net; integration tests |
| Unauthorized document access | Medium | High | Presigned URLs with 15-min expiry; authorization check before URL generation |
| API error messages leaking internals | Medium | Medium | Generic error messages in production; detailed errors only in logs |
| Secrets in source code or frontend bundle | Medium | Critical | .env files, .gitignore, no secrets in frontend code |

### 2.5 Denial of Service

| Threat | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Upload flooding (large/many files) | Medium | Medium | File size limits (25 MB), per-user rate limits, queue depth limits |
| API abuse (excessive requests) | Medium | Medium | Per-IP and per-user rate limiting |
| Processing queue saturation | Low | Medium | Queue depth monitoring, max concurrent jobs, daily processing limits |

### 2.6 Elevation of Privilege

| Threat | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Viewer role accessing admin functions | Medium | High | RBAC enforcement on every endpoint; role checks in middleware |
| Member escalating to owner | Low | Critical | Role changes restricted to owner/admin; audit logged |
| Accessing another org's resources | Low | Critical | org_id derived from session, never from request parameters |

---

## 3. Authentication

### 3.1 Password Authentication

- **Hashing:** bcrypt with cost factor 12 (or argon2id if performance allows)
- **Password requirements:** Minimum 8 characters, no maximum limit, check against common password list (top 10,000)
- **Email verification:** Required before accessing any org resources
- **Login rate limiting:** Max 5 attempts per email per 15 minutes; progressive lockout

### 3.2 Token Management

| Token Type | Storage | Lifetime | Rotation |
|---|---|---|---|
| Access token (JWT) | Frontend memory (never localStorage) | 15-30 minutes | On refresh |
| Refresh token | HTTP-only, Secure, SameSite=Strict cookie | 7 days | On each use (rotation) |
| Email verification token | Database | 24 hours | Single-use |
| Password reset token | Database | 1 hour | Single-use |
| Magic link token | Database | 15 minutes | Single-use |

### 3.3 JWT Claims

```json
{
  "sub": "user_uuid",
  "org_id": "current_org_uuid",
  "role": "admin",
  "iat": 1700000000,
  "exp": 1700001800
}
```

### 3.4 Account Lockout

- After 5 failed login attempts: 15-minute lockout
- After 15 failed attempts: 1-hour lockout
- Notify user via email after 5 failed attempts
- Lockout is per-account, not per-IP (prevents IP-based bypass)

### 3.5 Future: Multi-Factor Authentication

Not in MVP. Plan for TOTP-based MFA (Google Authenticator, Authy) as a Phase 2 feature.

---

## 4. Authorization (RBAC)

### 4.1 Role Definitions

| Role | Description |
|---|---|
| **Owner** | Full access including billing, member management, org deletion |
| **Admin** | Full access except billing and org deletion |
| **Member** | CRUD on projects, subs, documents, reviews; cannot manage members or settings |
| **Viewer** | Read-only access to all data; cannot create, modify, or delete anything |

### 4.2 Permission Matrix

| Resource | Owner | Admin | Member | Viewer |
|---|---|---|---|---|
| View dashboard | ✅ | ✅ | ✅ | ✅ |
| Manage org settings | ✅ | ✅ | ❌ | ❌ |
| Manage members | ✅ | ✅ | ❌ | ❌ |
| Manage billing | ✅ | ❌ | ❌ | ❌ |
| Delete organization | ✅ | ❌ | ❌ | ❌ |
| Create/edit projects | ✅ | ✅ | ✅ | ❌ |
| Create/edit subcontractors | ✅ | ✅ | ✅ | ❌ |
| Upload documents | ✅ | ✅ | ✅ | ❌ |
| View documents | ✅ | ✅ | ✅ | ✅ |
| Review compliance findings | ✅ | ✅ | ✅ | ✅ |
| Override findings (human review) | ✅ | ✅ | ✅ | ❌ |
| Send correction emails | ✅ | ✅ | ✅ | ❌ |
| Manage requirements | ✅ | ✅ | ✅ | ❌ |
| View reports | ✅ | ✅ | ✅ | ✅ |
| Export data | ✅ | ✅ | ✅ | ❌ |

### 4.3 Authorization Enforcement

```python
# FastAPI dependency for role-based authorization
async def require_role(
    minimum_role: Role,
    current_user: User = Depends(get_current_user),
    org_id: UUID = Depends(get_current_org_id),
) -> OrganizationMember:
    member = await get_membership(current_user.id, org_id)
    if not member or not member.has_role(minimum_role):
        raise HTTPException(403, "Insufficient permissions")
    return member
```

---

## 5. Multi-Tenant Isolation

### 5.1 Enforcement Layers

1. **Application layer (primary):** Every DB query includes `org_id` filter
2. **Database layer (secondary):** RLS policies enforce org_id matching
3. **API layer:** org_id derived from JWT, never from request body/params for authorization
4. **Storage layer:** R2 objects organized under `{org_id}/` prefix; presigned URLs scoped by path

### 5.2 Testing Strategy

- **Unit tests:** Every repository method tested with two org_ids; verify data from org_A is invisible when querying as org_B
- **Integration tests:** Full API calls with two authenticated users from different orgs; verify complete isolation
- **Penetration test scenario:** Manually attempt to access documents/data from another org by manipulating request parameters

---

## 6. Document Security

### 6.1 Upload Validation

| Check | Implementation | Limit |
|---|---|---|
| File extension | Whitelist: `.pdf` only | — |
| MIME type | Check `Content-Type` header | `application/pdf` |
| Magic bytes | Read first 4 bytes, verify `%PDF` | — |
| File size | Reject before upload completes | Max 25 MB (configurable) |
| Page count | After upload, limit excessive pages | Max 100 pages |

### 6.2 Malware Scanning

**Strategy:** Scan uploaded files before AI processing.

**Options (evaluate during implementation):**
- **ClamAV** — Open-source, self-hosted. Run as a sidecar service or on the worker.
- **Cloud scanning API** — VirusTotal API (rate limits on free tier), Cloudflare (if available for R2)

**Workflow:** Upload → scan → if clean, proceed to processing. If malware detected, quarantine file, mark document as rejected, notify user.

**MVP pragmatic approach:** At minimum, validate magic bytes and file structure (PyMuPDF will fail on corrupt/malicious PDFs). Full antivirus scanning can be added post-MVP.

### 6.3 PDF-Specific Risks

- **JavaScript in PDFs:** PyMuPDF does not execute JS; PDF.js has JS execution disabled by default. Ensure `disableAutoFetch` and sandboxing are enabled.
- **Embedded files:** Log if PDF contains embedded files; do not extract or execute them
- **Large pages/images:** Set rendering limits to prevent memory exhaustion

### 6.4 Storage Security

- R2 bucket: **no public access**
- Server-side encryption at rest (R2 default)
- All access through presigned URLs (15-minute expiry)
- Presigned URL generation requires prior authorization check
- Upload URLs include content-type and content-length conditions

---

## 7. API Security

### 7.1 Transport

- HTTPS only (enforced by hosting provider and HSTS header)
- TLS 1.2 minimum

### 7.2 CORS

```python
# Allow only the frontend origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app.subguard.ai"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=True,  # For refresh token cookie
)
```

### 7.3 Rate Limiting

| Endpoint Category | Limit | Window |
|---|---|---|
| Login/Register | 5 requests | 15 minutes per IP |
| Password reset | 3 requests | 1 hour per email |
| Document upload | 20 uploads | 1 hour per user |
| AI extraction trigger | 10 requests | 1 hour per user |
| General API | 100 requests | 1 minute per user |

### 7.4 Input Validation

- All API inputs validated through Pydantic models (enforced by FastAPI)
- String length limits on all text fields
- UUID format validation for all ID parameters
- No raw SQL queries — all queries through SQLAlchemy ORM

### 7.5 Security Headers

```python
# Add via middleware
headers = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "0",  # Deprecated; rely on CSP
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
```

---

## 8. Secret Management

### Rules

1. **Never** commit secrets to source control
2. **Never** include secrets in frontend bundles
3. **Never** log secrets (mask in error messages)
4. `.env` files in `.gitignore`
5. Separate secrets per environment (dev, staging, production)

### Secrets Inventory

| Secret | Used By | Rotation Frequency |
|---|---|---|
| Database URL | Backend, Worker | On compromise |
| Redis URL | Backend, Worker | On compromise |
| JWT signing key | Backend | Every 90 days (support multiple active keys) |
| R2 access key / secret | Backend, Worker | Every 90 days |
| AI provider API key | Worker | Every 90 days |
| Email provider API key | Worker | Every 90 days |
| Payment provider key | Backend | On compromise |

### Production Secret Management

Use the deployment platform's built-in secret management (Railway secrets, Render environment variables, or a dedicated solution like Doppler/Infisical if needed).

---

## 9. Data Privacy

### 9.1 Data Classification

| Data Type | Sensitivity | Examples |
|---|---|---|
| **High** | Sensitive business documents | COIs, endorsements, policy details |
| **High** | Authentication credentials | Password hashes, tokens |
| **Medium** | Business information | Subcontractor names, contact details, project info |
| **Medium** | Compliance findings | Review results, deficiency details |
| **Low** | Application metadata | Timestamps, status fields, audit events |

### 9.2 Encryption

| Layer | Method |
|---|---|
| In transit | TLS 1.2+ (HTTPS) |
| At rest (database) | Managed provider's encryption (AES-256) |
| At rest (object storage) | R2 server-side encryption (AES-256) |
| Application-level | Not required for MVP (managed encryption is sufficient) |

### 9.3 Data Retention

| Data Type | Retention | Deletion Method |
|---|---|---|
| Active business data | While org is active | Soft delete (mark inactive) |
| Uploaded documents | While org is active or per policy | Hard delete from R2 after retention period |
| Audit logs | 3 years minimum | Archive after retention, never delete |
| Processed extraction data | Tied to document lifecycle | Delete with document |
| User accounts | While active | Anonymize on deletion request |

### 9.4 Right to Deletion

If a user or organization requests data deletion:
1. Soft-delete all organization data (mark inactive)
2. Schedule hard deletion of documents from R2 (30-day grace period)
3. Anonymize user personal data
4. Preserve anonymized audit logs (legal retention)
5. Confirm deletion to the requester

> [!WARNING]
> **Regulatory considerations:** Handling business insurance documents may trigger state-level regulatory obligations (varies by US state). GDPR may apply if any EU data subjects are involved. SOC 2, CCPA, and industry-specific requirements need evaluation by a qualified attorney. Do NOT claim compliance with any regulatory framework without professional legal review.

---

## 10. Audit Logging

### What to Log

| Event Category | Examples |
|---|---|
| Authentication | Login success/failure, logout, password change, token refresh |
| Authorization | Permission denied events |
| Document access | Upload, download, view, delete |
| Compliance actions | Review triggered, finding created, human override |
| Admin actions | Member invited/removed, role changed, settings modified |
| Data modifications | Create, update, delete of business entities |

### What NOT to Log

- Passwords (even hashed)
- Full document contents
- Raw AI responses (store in extraction_jobs, not audit_logs)
- PII in log messages beyond what's needed for audit

### Log Format

```json
{
  "timestamp": "2025-01-15T10:30:00Z",
  "level": "INFO",
  "action": "document.downloaded",
  "org_id": "uuid",
  "user_id": "uuid",
  "entity_type": "document",
  "entity_id": "uuid",
  "ip_address": "192.168.1.1",
  "request_id": "req_abc123"
}
```

---

## 11. Incident Response (Lightweight)

### What Constitutes a Security Incident

- Confirmed or suspected unauthorized access to customer data
- Cross-tenant data exposure
- Credential or secret compromise
- Malware detection in uploaded documents
- Significant service disruption from attack

### Basic Response Steps

1. **Detect** — Monitoring, alerting, user reports
2. **Contain** — Revoke compromised credentials, block suspicious IPs, disable affected accounts
3. **Assess** — Determine scope (which orgs/users affected, what data exposed)
4. **Notify** — Inform affected customers (legal obligations vary by jurisdiction)
5. **Remediate** — Fix the vulnerability, rotate secrets, deploy patches
6. **Review** — Post-incident analysis, document lessons learned

> [!IMPORTANT]
> This incident response plan is preliminary. Before handling production customer data, consult with legal counsel about notification obligations (which vary by US state) and consider cyber liability insurance.

---

## 12. Security Checklist

| Control | Status | Priority | Notes |
|---|---|---|---|
| HTTPS enforced | Planned | P0 | Hosting provider handles SSL |
| Password hashing (bcrypt/argon2) | Planned | P0 | |
| JWT with short expiry | Planned | P0 | |
| org_id filtering on all queries | Planned | P0 | Critical for tenant isolation |
| Input validation (Pydantic) | Planned | P0 | FastAPI provides this |
| File upload validation | Planned | P0 | Type, size, magic bytes |
| Presigned URLs for documents | Planned | P0 | 15-minute expiry |
| CORS configuration | Planned | P0 | |
| Rate limiting | Planned | P0 | Auth endpoints first |
| .env in .gitignore | Planned | P0 | |
| Security headers | Planned | P1 | HSTS, CSP, X-Frame-Options |
| Audit logging | Planned | P1 | |
| RLS as defense-in-depth | Planned | P1 | If using Supabase |
| Account lockout | Planned | P1 | |
| Malware scanning | Planned | P2 | Post-MVP |
| MFA | Planned | P2 | Post-MVP |
| Professional security audit | Needs Review | P1 | Before production launch |
| Privacy policy (legal review) | Needs Review | P1 | |
| Regulatory compliance assessment | Needs Review | P1 | |

---

## 13. Known Risks and Honest Limitations

| Risk | Current Status | Mitigation |
|---|---|---|
| Solo founder cannot cover all security aspects | Accepted for MVP | Use managed services, follow OWASP guidelines, plan for professional review before launch |
| No penetration testing yet | Planned for pre-launch | Budget for a basic pen test or use automated scanning tools (OWASP ZAP) |
| AI provider sees customer documents | Inherent to the architecture | Review AI provider's data processing terms; evaluate providers with data privacy commitments |
| Document metadata may leak information | Low risk | Strip metadata from rendered page images before sending to AI |
| Insider threat (founder has full access) | Accepted for solo founder | Implement audit logging; transition to role-based access as team grows |

---

*This document will be updated as security controls are implemented and reviewed.*
