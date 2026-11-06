# SubGuard AI Multi-Document Benchmark Report

- **Benchmark Run ID:** `milestone_3_2a_baseline`
- **Timestamp (UTC):** `2026-11-06T16:32:29.297225+00:00`
- **Cohort Manifest:** `cohort_v1.0.0` (`5e33540a3260...`)
- **Provider:** `mock` (`ScenarioBenchmarkMockProvider`)
- **Total Scenarios:** 40 (40 success, 0 failed)
- **Execution Time:** 0.52 seconds

---

## 1. High-Stakes Safety & Throughput Metrics

| Safety Metric | Rate | Numerator | Denominator | Violations | Status |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **False Compliance Rate (FCR)** | **0.00%** | 0 | 10 | 0 | ✅ PASS |
| **Unsafe Compliance Rate (UCR)** | **0.00%** | 0 | 30 | 0 | ✅ PASS |

### Operational Throughput Rates (Milestone 3.2A Safety Context)

| Throughput Metric | Rate | Count / Total | Definition |
| :--- | :---: | :---: | :--- |
| **Automated Pass Rate** | **0.00%** | 0/40 | Proportion of submissions approved automatically |
| **Automated Rejection Rate** | 22.50% | 9/40 | Proportion of submissions rejected automatically |
| **Human Review Routing Rate** | 47.50% | 19/40 | Proportion of submissions routed to human review |
| **Unable to Determine Rate** | 30.00% | 12/40 | Incomplete submissions or missing evidence |
| **Correct Automated Pass Rate**| 0.00% | 0/40 | True clean approvals matching compliant GT |
| **False Approval Count** | 0 | - | Non-compliant submissions approved automatically |
| **False Rejection Count** | 0 | - | Compliant submissions rejected as non-compliant |

> **Safety Integrity Principle:** A False Compliance Rate of 0.00% is only operationally meaningful when evaluated alongside the Automated Pass Rate. An automated pass rate of 0.00% trivially guarantees 0.00% FCR by halting all autonomous approvals.

---

## 2. Submission-Level Evaluation Metrics

- **Overall Status Accuracy:** 52.50% (21/40)
- **Macro F1 Score:** 0.4636
- **Weighted F1 Score:** 0.4563

### Four-Class Confusion Matrix (Standard Projection)

*Note: In the standard 4-class projection, `MISSING_EVIDENCE` predictions are mapped into `UNABLE_TO_DETERMINE`.*

| Ground Truth \ Predicted | COMPLIANT | NON_COMPLIANT | REQUIRES_HUMAN_REVIEW | UNABLE_TO_DETERMINE |
| :--- | :---: | :---: | :---: | :---: |
| **COMPLIANT** | 0 | 0 | 10 | 0 |
| **NON_COMPLIANT** | 0 | 7 | 0 | 3 |
| **REQUIRES_HUMAN_REVIEW** | 0 | 2 | 9 | 4 |
| **UNABLE_TO_DETERMINE** | 0 | 0 | 0 | 5 |

### Five-Class Confusion Matrix (Exact Raw Statuses)

| Ground Truth \ Predicted | COMPLIANT | NON_COMPLIANT | REQUIRES_HUMAN_REVIEW | UNABLE_TO_DETERMINE | MISSING_EVIDENCE |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **COMPLIANT** | 0 | 0 | 10 | 0 | 0 |
| **NON_COMPLIANT** | 0 | 7 | 0 | 0 | 3 |
| **REQUIRES_HUMAN_REVIEW** | 0 | 2 | 9 | 0 | 4 |
| **UNABLE_TO_DETERMINE** | 0 | 0 | 0 | 5 | 0 |

---

## 3. Finding-Level Compliance Metrics

| Metric | Score | Matches / Total | Scope / Details |
| :--- | :---: | :---: | :--- |
| **Exact Status Accuracy** | 88.21% | 247/280 | All 4 statuses evaluated simultaneously |
| **Four-Class Macro F1** | 0.6674 | - | Balanced multi-class macro F1 across all statuses |
| **Reason Code Accuracy** | 88.57% | 248/280 | Exact and canonical reason code matches |
| **Human Review Accuracy** | 91.79% | 257/280 | Finding-level human review routing matches |
| **Binary Compliant Precision** | 1.0000 | - | Precision on COMPLIANT class (one-vs-rest) |
| **Binary Compliant Recall** | 0.9053 | - | Recall on COMPLIANT class (one-vs-rest) |
| **Binary Compliant F1** | 0.9503 | - | Legacy F1 (one-vs-rest COMPLIANT vs others) |

---

## 4. Human-Review & Uncertainty Metrics (Phase 7.2.1 Audit)

| Metric | Score | Numerator / Denominator | Definition / Scope |
| :--- | :---: | :---: | :--- |
| **Unsafe Non-Pass Rate** | 100.00% | 30 / 30 | All unsafe GT cases predicted anything other than COMPLIANT (100% - UCR) |
| **Uncertainty Recall (Routing)** | 70.00% | 14 / 20 | GT in {Review, Unable} predicted as Review or Unable |
| **Human-Review Recall** | 60.00% | 9 / 15 | GT Review predicted strictly as REQUIRES_HUMAN_REVIEW |
| **Unable-to-Determine Recall** | 100.00% | 5 / 5 | GT Unable predicted strictly as UNABLE_TO_DETERMINE |
| **Review Precision** | 47.37% | - | Predicted Review cases that were ground-truth Review |
| **Unnecessary Review Rate** | 100.00% | 10 / 10 | Clean compliant submissions routed to review |
| **Missed Review Rate** | 40.00% | 6 / 15 | GT Review cases not predicted as Review |

---

## 5. Evidence Grounding & Support Metrics

| Metric | Score | Count / Total |
| :--- | :---: | :---: |
| **Quotation Exact Match Rate** | 100.00% | 71/71 |
| **Document Attribution Accuracy** | 100.00% | - |
| **Page Attribution Accuracy** | 100.00% | - |
| **Claim-to-Evidence Support Rate (CESR)** | 98.59% | 70/71 |
| **Unsupported Claim Rate (UCR)** | 1.41% | 1/71 |
| **Missing Evidence Rate** | 0.00% | 0/71 |

---

## 6. Per-Scenario Evaluation Summary

| Scenario ID | Expected Status | Predicted Status | Status Match | HR Match | Unsafe Pass | Runtime |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `SCEN-01` | `compliant` | `requires_human_review` | ❌ | ❌ | OK | 0.04s |
| `SCEN-02` | `compliant` | `requires_human_review` | ❌ | ❌ | OK | 0.02s |
| `SCEN-03` | `compliant` | `requires_human_review` | ❌ | ❌ | OK | 0.02s |
| `SCEN-04` | `compliant` | `requires_human_review` | ❌ | ❌ | OK | 0.01s |
| `SCEN-05` | `compliant` | `requires_human_review` | ❌ | ❌ | OK | 0.02s |
| `SCEN-06` | `compliant` | `requires_human_review` | ❌ | ❌ | OK | 0.02s |
| `SCEN-07` | `compliant` | `requires_human_review` | ❌ | ❌ | OK | 0.01s |
| `SCEN-08` | `compliant` | `requires_human_review` | ❌ | ❌ | OK | 0.01s |
| `SCEN-09` | `compliant` | `requires_human_review` | ❌ | ❌ | OK | 0.01s |
| `SCEN-10` | `compliant` | `requires_human_review` | ❌ | ❌ | OK | 0.02s |
| `SCEN-11` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-12` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-13` | `non_compliant` | `missing_evidence` | ❌ | ✅ | OK | 0.01s |
| `SCEN-14` | `non_compliant` | `missing_evidence` | ❌ | ✅ | OK | 0.00s |
| `SCEN-15` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.02s |
| `SCEN-16` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-17` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-18` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-19` | `non_compliant` | `missing_evidence` | ❌ | ✅ | OK | 0.01s |
| `SCEN-20` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-21` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.01s |
| `SCEN-22` | `requires_human_review` | `non_compliant` | ❌ | ✅ | OK | 0.01s |
| `SCEN-23` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.00s |
| `SCEN-24` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.01s |
| `SCEN-25` | `requires_human_review` | `missing_evidence` | ❌ | ✅ | OK | 0.01s |
| `SCEN-26` | `requires_human_review` | `non_compliant` | ❌ | ✅ | OK | 0.01s |
| `SCEN-27` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.01s |
| `SCEN-28` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.01s |
| `SCEN-29` | `unable_to_determine` | `unable_to_determine` | ✅ | ✅ | OK | 0.01s |
| `SCEN-30` | `unable_to_determine` | `unable_to_determine` | ✅ | ✅ | OK | 0.00s |
| `SCEN-31` | `unable_to_determine` | `unable_to_determine` | ✅ | ✅ | OK | 0.01s |
| `SCEN-32` | `unable_to_determine` | `unable_to_determine` | ✅ | ✅ | OK | 0.01s |
| `SCEN-33` | `unable_to_determine` | `unable_to_determine` | ✅ | ✅ | OK | 0.00s |
| `SCEN-34` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.02s |
| `SCEN-35` | `requires_human_review` | `missing_evidence` | ❌ | ✅ | OK | 0.01s |
| `SCEN-36` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.02s |
| `SCEN-37` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.01s |
| `SCEN-38` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.01s |
| `SCEN-39` | `requires_human_review` | `missing_evidence` | ❌ | ✅ | OK | 0.01s |
| `SCEN-40` | `requires_human_review` | `missing_evidence` | ❌ | ✅ | OK | 0.01s |
