# SubGuard AI — Product Specification

> **Version:** 0.1.0 (Draft)  
> **Last Updated:** 2026-10-30  
> **Status:** Planning Phase  
> **Author:** Founding Team

---

## 1. Product Vision

SubGuard AI is an AI-powered document compliance tool that helps general contractors verify whether their subcontractors carry adequate insurance coverage. It automates the tedious, error-prone process of reviewing Certificates of Insurance (COIs), endorsement documents, and related policy paperwork.

The product is **not** an insurance broker, a legal advisor, or a guarantee of coverage. It is an administrative review tool that reduces manual effort, surfaces deficiencies early, and maintains an auditable trail of compliance decisions. Our core design principle is: **AI extracts. Deterministic code evaluates. Humans resolve uncertainty.**

---

## 2. Target Users

### Primary Users

| Persona | Role | Pain Point |
|---------|------|------------|
| **Office Manager / Compliance Coordinator** | Reviews incoming COIs, tracks expirations, sends correction requests | Spends hours per week manually reading PDFs and comparing values against spreadsheet checklists |
| **Project Manager** | Needs to know if all subs on a project are insured before work begins | Delays projects when insurance gaps are discovered late |
| **General Contractor (Owner)** | Bears liability risk if a sub is underinsured | Relies on staff or memory to track compliance across many subs |

### Secondary Users

- **Subcontractors** — Receive correction request emails, upload updated documents
- **Insurance Brokers** — May receive forwarded deficiency reports to produce corrected certificates

### User Assumptions

- Users are familiar with standard ACORD 25 certificate forms
- Users understand basic insurance terminology (occurrence limits, aggregate limits, additional insured, waiver of subrogation)
- Users are not technical — the UI must be self-explanatory
- Most users work on desktop or laptop, though mobile access should be functional

---

## 3. Problem Statement

### Current Process

1. A general contractor hires a subcontractor
2. The subcontractor (or their broker) sends a Certificate of Insurance (COI), typically an ACORD 25 form, plus endorsement pages
3. Someone at the GC's office must manually:
   - Open the PDF
   - Locate each coverage type (general liability, auto, workers' comp, umbrella)
   - Read the dollar limits and compare them against the project's or company's requirements
   - Check that policy dates are current (not expired)
   - Look for Additional Insured endorsements naming the GC
   - Look for Waiver of Subrogation endorsements
   - Check for Primary & Non-Contributory language
   - Verify the named insured matches the subcontractor's legal name
4. If anything is missing or insufficient, draft an email to the sub or broker
5. Follow up until corrected documents are received
6. Track expiration dates and request renewals

### Pain Points

- **Time-consuming:** A single COI review can take 10-30 minutes; a GC with 50+ subs spends dozens of hours per month
- **Error-prone:** Easy to miss a deficiency, especially across multi-page endorsement documents
- **Inconsistent:** Different reviewers may interpret requirements differently
- **No audit trail:** Most GCs use spreadsheets, email, or filing cabinets — no structured record of what was checked
- **Expiration blind spots:** Policies expire mid-project; without tracking, the GC may not notice
- **Repetitive communication:** Writing correction-request emails is boilerplate work

---

## 4. Solution Overview

SubGuard AI provides a web-based workflow that automates this process:

```
Upload COI + Endorsements
    ↓
AI Extracts Structured Data (limits, dates, entities, endorsements)
    ↓
Deterministic Rules Engine Evaluates Against Requirements
    ↓
Compliance Findings with Evidence from Source Documents
    ↓
Deficiency Report → Correction Request Email
    ↓
Expiration Tracking → Renewal Reminders
```

### What the AI Does

- Reads PDF pages (text and images)
- Extracts structured fields: coverage types, dollar amounts, dates, entity names, endorsement types
- Returns source text, page numbers, and bounding-box coordinates as evidence
- Reports confidence levels and flags uncertainty

### What the AI Does NOT Do

- Does not decide whether a policy satisfies a requirement
- Does not interpret legal language
- Does not guarantee accuracy
- Does not replace human judgment for ambiguous cases

### What the Rules Engine Does

- Compares extracted values against configured requirements using deterministic Python logic
- Produces one of five statuses for each requirement:
  - **Compliant** — requirement met with verified evidence
  - **Non-Compliant** — requirement not met
  - **Missing Evidence** — required information was not found in documents
  - **Unable to Determine** — evidence exists but is ambiguous or contradictory
  - **Requires Human Review** — system cannot make a reliable determination

---

## 5. Core Workflows

### 5.1 Onboarding / Workspace Setup

1. User registers with email and password
2. User creates an organization (workspace)
3. User configures basic organization settings (company name, address)
4. User optionally invites team members
5. User creates their first insurance requirement template

### 5.2 Defining Insurance Requirements

1. User navigates to Requirements → Templates
2. User creates a new template (e.g., "Standard Subcontractor Requirements")
3. For each coverage type (GL, Auto, WC, Umbrella), user specifies:
   - Minimum per-occurrence limit
   - Minimum aggregate limit
   - Other applicable limits
4. User specifies endorsement requirements:
   - Additional Insured required? (yes/no)
   - Waiver of Subrogation required? (yes/no)
   - Primary & Non-Contributory required? (yes/no)
5. User saves the template
6. User can assign templates to specific projects or use as organization-wide defaults

### 5.3 Adding a Subcontractor

1. User navigates to Subcontractors → Add New
2. User enters: business name, contact name, email, phone, trade/specialty
3. User optionally associates the subcontractor with one or more projects
4. System creates the subcontractor record

### 5.4 Uploading and Processing Documents

1. User selects a subcontractor
2. User clicks "Upload Documents"
3. User selects one or more PDF files (COI, endorsements)
4. System validates files (PDF format, size limit, security checks)
5. System stores files in private object storage
6. Background job processes each document:
   a. Classify document type (COI, endorsement, declaration page, other)
   b. Extract structured data using AI
   c. Validate AI response
   d. Store extracted fields with evidence references
7. User sees processing status (pending → processing → complete/failed)
8. User can view extracted data and source evidence

### 5.5 Reviewing Compliance Results

1. System automatically evaluates extracted data against applicable requirements
2. User navigates to the compliance review for a subcontractor
3. User sees:
   - Overall compliance status
   - Per-requirement findings (compliant, non-compliant, missing, etc.)
   - For each finding: required value, found value, explanation, evidence link
4. User can click evidence links to view the original PDF with highlighted regions
5. User can override findings that require human review (confirm or reject)
6. User can add notes to findings

### 5.6 Managing Deficiencies

1. System generates a deficiency report listing all non-compliant and missing items
2. System drafts a correction-request email
3. User reviews and optionally edits the email draft
4. User approves and sends the email
5. System tracks the deficiency as "sent" and awaits corrected documents
6. When new documents are uploaded, system re-evaluates and updates findings

### 5.7 Handling Expirations

1. System records policy expiration dates from extracted data
2. System generates notifications at 30, 15, and 7 days before expiration
3. Notifications appear in the app and optionally via email
4. System prevents duplicate notifications for the same expiration
5. User can view all upcoming expirations in a dashboard or report

---

## 6. MVP Feature Matrix

| Feature Area | Priority | MVP Phase | Description |
|---|---|---|---|
| User registration and login | P0 | Phase 1 | Email/password auth with email verification |
| Organization creation | P0 | Phase 1 | Create workspace, basic settings |
| User roles (owner, admin, member, viewer) | P0 | Phase 1 | Role-based access within an org |
| Team member invitation | P1 | Phase 1 | Invite users to join an org |
| Subcontractor CRUD | P0 | Phase 2 | Add, edit, list, archive subcontractors |
| Project CRUD | P0 | Phase 2 | Add, edit, list, archive projects |
| Project-subcontractor association | P0 | Phase 2 | Assign subs to projects |
| Insurance requirement templates | P0 | Phase 2 | Create reusable requirement sets |
| Requirement configuration UI | P0 | Phase 2 | Configure coverage types, limits, endorsements |
| Assign requirements to projects | P0 | Phase 2 | Link templates or custom requirements to projects |
| PDF upload | P0 | Phase 3 | Upload COIs and endorsements |
| File validation and security | P0 | Phase 3 | Type, size, basic security checks |
| PDF viewer | P0 | Phase 3 | View uploaded documents in-browser |
| Background job infrastructure | P0 | Phase 3 | Redis + ARQ for async processing |
| Document classification | P0 | Phase 4 | Identify document type (COI, endorsement, etc.) |
| AI field extraction | P0 | Phase 4 | Extract structured data from documents |
| Evidence storage | P0 | Phase 4 | Source text, page numbers, bounding boxes |
| Extraction results UI | P0 | Phase 4 | View what was extracted from each document |
| Compliance rules engine | P0 | Phase 5 | Deterministic evaluation of extracted vs. required |
| Compliance review UI | P0 | Phase 5 | View compliance findings with evidence |
| Evidence viewer with highlighting | P1 | Phase 5 | Click-to-view PDF evidence for each finding |
| Deficiency report generation | P0 | Phase 6 | Summary of non-compliant and missing items |
| Email drafting and sending | P1 | Phase 6 | Correction request emails via Resend |
| Expiration tracking | P1 | Phase 6 | Record and alert on approaching expirations |
| Basic reporting (CSV export) | P2 | Phase 6 | Compliance overview, exportable data |
| Notification system | P1 | Phase 6 | In-app notifications for key events |

---

## 7. MVP Boundaries

### What IS in the MVP

- Single-organization workspaces (a user can belong to multiple orgs, but each org is independent)
- ACORD 25 certificate forms as the primary supported document type
- Common endorsement types: Additional Insured, Waiver of Subrogation, Primary & Non-Contributory
- Standard coverage types: General Liability, Automobile Liability, Workers' Compensation, Umbrella/Excess Liability
- English-language documents only
- US insurance market conventions only
- Web application (responsive, but not a native mobile app)
- Email-based correction requests (not real-time chat or portal)
- Manual document upload (not automated email ingestion)
- Single AI provider at launch (with abstraction layer for future providers)

### What IS NOT in the MVP

| Excluded Feature | Reason |
|---|---|
| Direct integration with insurance carrier systems | Complex, requires carrier partnerships |
| Automated policy binding or purchasing | Out of scope — we are a review tool, not a broker |
| Legal advice or compliance certification | We are not a law firm or compliance authority |
| Native mobile apps (iOS/Android) | Responsive web is sufficient for MVP |
| Real-time document collaboration | Not needed for initial use case |
| OCR for handwritten documents | Focus on standard typed/printed forms |
| Multi-language support | US market only initially |
| Integration with Procore, Buildertrend, or other PM tools | Future integration opportunity |
| Automated payment of insurance premiums | Completely out of scope |
| White-labeling | Enterprise feature, not MVP |
| Public API | Future phase after MVP validation |
| Automated email ingestion (monitor a mailbox for COIs) | Complex setup, manual upload is sufficient |
| Subcontractor self-service portal | Future feature |
| Batch processing of multiple subs at once | Single-sub workflow first |
| Historical compliance trending and analytics | Basic reporting only in MVP |
| Custom compliance rules per organization | Standard rule types only in MVP |
| Audit report generation for external parties | Internal use only |
| SSO/SAML integration | Enterprise feature |
| Offline access | Web-only |

---

## 8. Compliance Status Taxonomy

| Status | Icon | Definition | Example |
|---|---|---|---|
| **COMPLIANT** | ✅ | The requirement is met. Extracted evidence clearly satisfies the configured minimum or condition with high confidence. | GL each occurrence extracted as \$1,000,000; requirement is \$1,000,000 |
| **NON_COMPLIANT** | ❌ | The requirement is not met. Extracted evidence clearly falls below the configured minimum or a required condition is absent. | GL each occurrence extracted as \$500,000; requirement is \$1,000,000 |
| **MISSING_EVIDENCE** | ⚠️ | The required information was not found in any uploaded document. Either the field was not extracted or no document of the required type was uploaded. | Workers' compensation section was blank or no WC policy was listed on the COI |
| **UNABLE_TO_DETERMINE** | 🔶 | Evidence exists but is ambiguous, contradictory, or low-confidence. The system cannot reliably evaluate compliance. | Two different limits were found on different pages; extraction confidence is below threshold |
| **REQUIRES_HUMAN_REVIEW** | 👁️ | The system flagged this for manual review. Complex endorsement language, edge cases, or partial matches require a human decision. | Additional Insured endorsement found but uses "blanket" language without naming the GC specifically |

### Status Priority (for Overall Determination)

When aggregating individual findings into an overall subcontractor compliance status:

1. If ANY finding is **NON_COMPLIANT** → Overall is **NON_COMPLIANT**
2. If ANY finding is **MISSING_EVIDENCE** → Overall cannot be COMPLIANT
3. If ANY finding is **UNABLE_TO_DETERMINE** → Overall cannot be COMPLIANT
4. If ANY finding is **REQUIRES_HUMAN_REVIEW** → Overall cannot be COMPLIANT
5. Only if ALL findings are **COMPLIANT** → Overall is **COMPLIANT**

---

## 9. Non-Functional Requirements

| Requirement | Target | Notes |
|---|---|---|
| Page load time | < 2 seconds for dashboard and list views | Excludes PDF rendering |
| PDF rendering | < 5 seconds to display first page | Using PDF.js client-side |
| Document processing time | < 60 seconds per document (median) | AI extraction is the bottleneck; depends on provider |
| Concurrent users per org | Support 10+ simultaneous users | MVP scale |
| Document storage | Up to 10,000 documents per org | Sufficient for MVP validation |
| Uptime target | 99.5% (roughly 44 hours downtime/year) | Acceptable for early-stage SaaS |
| Data backup | Daily automated backups | Database and object storage |
| Browser support | Chrome, Firefox, Edge (latest 2 versions) | Safari support is desirable but not blocking |
| Accessibility | WCAG 2.1 Level AA (best effort) | Using shadcn/ui provides a reasonable baseline |
| Security | See SECURITY.md | Comprehensive security requirements documented separately |

---

## 10. Assumptions and Risks

### Business Assumptions

| Assumption | Risk if Wrong |
|---|---|
| GCs will pay for automated COI review | Product has no market; pivot or abandon |
| Manual COI review is painful enough to justify SaaS spending | GCs may tolerate current process; need strong ROI story |
| Office managers and PMs are the primary buyers/users | May need to target different decision-makers |
| US small-to-medium GCs are the right initial market | Market may be too small, too fragmented, or too price-sensitive |
| \$50-200/month price range is viable | Unvalidated — need customer discovery |

### Technical Assumptions

| Assumption | Risk if Wrong |
|---|---|
| Multimodal AI can extract COI data with >85% field accuracy | Extraction may be unreliable; need fallback strategies |
| ACORD 25 forms are standardized enough for reliable extraction | Formatting varies across brokers, insurance companies, and states |
| Endorsement documents follow patterns that AI can recognize | Endorsement wording varies significantly; may need human review for most |
| PyMuPDF can handle the variety of PDFs encountered | Some PDFs may be scanned images without text layers; may need OCR fallback |
| A single AI model call per page is sufficient | May need multiple passes or specialized prompts for different sections |

### Market Risks

| Risk | Mitigation |
|---|---|
| Established competitors (myCOI, Jones, TrustLayer, PINS) | Focus on simplicity, lower price point, solo-founder agility |
| GCs may not trust AI for compliance-related decisions | Our design principle explicitly puts humans in the loop; market as "AI-assisted" not "AI-decided" |
| Insurance industry is slow to adopt new technology | Target tech-forward GCs first; build case studies |
| Regulatory requirements may constrain what we can claim | Clear disclaimers; legal review before launch |

---

## 11. Open Questions Requiring Founder Decision

| # | Question | Options | Impact |
|---|---|---|---|
| 1 | Should we support subcontractor self-service upload in MVP? | Yes (more complex, better UX) / No (GC uploads on behalf of sub) | Scope and complexity |
| 2 | What is our pricing model? | Per-sub per-month / Per-document / Flat monthly | Revenue and market positioning |
| 3 | Do we need a free tier? | Yes (freemium, limited subs/docs) / No (free trial only) | Acquisition strategy |
| 4 | Should we build custom auth or use a managed service (Supabase Auth, Clerk)? | Custom (more control) / Managed (faster, more secure) | Development time vs. flexibility |
| 5 | How do we handle the payment provider question given your location in Pakistan? | Research needed — Stripe, Paddle, LemonSqueezy, others | Revenue collection is existential |
| 6 | What is our minimum viable extraction accuracy before launch? | 80%? 85%? 90%? | Quality bar vs. time to market |
| 7 | Should we require email verification before allowing document uploads? | Yes (security) / No (faster onboarding) | Security vs. friction |
| 8 | Do we want to support ACORD 28 (property) or ACORD 27 (property evidence) in MVP? | Yes (broader market) / No (focus on ACORD 25 first) | Scope |
| 9 | What is the maximum document file size we accept? | 10 MB? 25 MB? 50 MB? | Storage costs, processing time |
| 10 | Should correction-request emails come from our domain or the user's? | Our domain (simpler) / User's domain (better deliverability, more trust) | Email infrastructure complexity |

---

*This document will be updated as product decisions are made and validated through customer discovery.*
