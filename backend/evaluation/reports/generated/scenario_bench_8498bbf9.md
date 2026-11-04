# SubGuard AI Multi-Document Benchmark Report

- **Benchmark Run ID:** `scenario_bench_8498bbf9`
- **Timestamp (UTC):** `2026-11-04T18:44:29.784862+00:00`
- **Cohort Manifest:** `cohort_v1.0.0` (`5e33540a3260...`)
- **Provider:** `mock` (`ScenarioBenchmarkMockProvider`)
- **Total Scenarios:** 1 (1 success, 0 failed)
- **Execution Time:** 0.08 seconds

---

## 1. High-Stakes Safety Metrics

| Safety Metric | Rate | Numerator | Denominator | Violations | Status |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **False Compliance Rate (FCR)** | **N/A** | 0 | 0 | 0 | ✅ PASS |
| **Unsafe Compliance Rate (UCR)** | **N/A** | 0 | 0 | 0 | ✅ PASS |

**Definitions:**
- **FCR:** False `COMPLIANT` decisions strictly on ground-truth `NON_COMPLIANT` cases.
- **UCR:** False `COMPLIANT` decisions across ALL unsafe ground-truth categories (`NON_COMPLIANT`, `REQUIRES_HUMAN_REVIEW`, `UNABLE_TO_DETERMINE`).

---

## 2. Submission-Level Evaluation Metrics

- **Overall Status Accuracy:** 100.00% (1/1)
- **Macro F1 Score:** 1.0000
- **Weighted F1 Score:** 1.0000

### Four-Class Confusion Matrix (Standard Projection)

*Note: In the standard 4-class projection, `MISSING_EVIDENCE` predictions are mapped into `UNABLE_TO_DETERMINE`.*

| Ground Truth \ Predicted | COMPLIANT | NON_COMPLIANT | REQUIRES_HUMAN_REVIEW | UNABLE_TO_DETERMINE |
| :--- | :---: | :---: | :---: | :---: |
| **COMPLIANT** | 1 | 0 | 0 | 0 |
| **NON_COMPLIANT** | 0 | 0 | 0 | 0 |
| **REQUIRES_HUMAN_REVIEW** | 0 | 0 | 0 | 0 |
| **UNABLE_TO_DETERMINE** | 0 | 0 | 0 | 0 |

### Five-Class Confusion Matrix (Exact Raw Statuses)

| Ground Truth \ Predicted | COMPLIANT | NON_COMPLIANT | REQUIRES_HUMAN_REVIEW | UNABLE_TO_DETERMINE | MISSING_EVIDENCE |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **COMPLIANT** | 1 | 0 | 0 | 0 | 0 |
| **NON_COMPLIANT** | 0 | 0 | 0 | 0 | 0 |
| **REQUIRES_HUMAN_REVIEW** | 0 | 0 | 0 | 0 | 0 |
| **UNABLE_TO_DETERMINE** | 0 | 0 | 0 | 0 | 0 |

---

## 3. Finding-Level Compliance Metrics

| Metric | Score | Matches / Total |
| :--- | :---: | :---: |
| **Exact Status Accuracy** | 100.00% | 7/7 |
| **Reason Code Accuracy** | 85.71% | 6/7 |
| **Human Review Accuracy** | 100.00% | 7/7 |
| **Compliance Precision** | 1.0000 | - |
| **Compliance Recall** | 1.0000 | - |
| **Compliance F1 Score** | 1.0000 | - |

---

## 4. Human-Review & Uncertainty Metrics (Phase 7.2.1 Audit)

| Metric | Score | Numerator / Denominator | Definition / Scope |
| :--- | :---: | :---: | :--- |
| **Unsafe Non-Pass Rate** | 100.00% | 0 / 0 | All unsafe GT cases predicted anything other than COMPLIANT (100% - UCR) |
| **Uncertainty Recall (Routing)** | 100.00% | 0 / 0 | GT in {Review, Unable} predicted as Review or Unable |
| **Human-Review Recall** | 100.00% | 0 / 0 | GT Review predicted strictly as REQUIRES_HUMAN_REVIEW |
| **Unable-to-Determine Recall** | 100.00% | 0 / 0 | GT Unable predicted strictly as UNABLE_TO_DETERMINE |
| **Review Precision** | 100.00% | - | Predicted Review cases that were ground-truth Review |
| **Unnecessary Review Rate** | 0.00% | 0 / 10 | Clean compliant submissions routed to review |
| **Missed Review Rate** | 0.00% | 0 / 0 | GT Review cases not predicted as Review |

---

## 5. Evidence Grounding & Support Metrics

| Metric | Score | Count / Total |
| :--- | :---: | :---: |
| **Quotation Exact Match Rate** | 100.00% | 7/7 |
| **Document Attribution Accuracy** | 100.00% | - |
| **Page Attribution Accuracy** | 100.00% | - |
| **Claim-to-Evidence Support Rate (CESR)** | 100.00% | 7/7 |
| **Unsupported Claim Rate (UCR)** | 0.00% | 0/7 |
| **Missing Evidence Rate** | 0.00% | 0/7 |

---

## 6. Per-Scenario Evaluation Summary

| Scenario ID | Expected Status | Predicted Status | Status Match | HR Match | Unsafe Pass | Runtime |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `SCEN-01` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.01s |
