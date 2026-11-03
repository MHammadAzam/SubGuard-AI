# SubGuard AI Multi-Document Benchmark Report

- **Benchmark Run ID:** `scenario_bench_d901e725`
- **Timestamp (UTC):** `2026-11-03T18:17:49.263000+00:00`
- **Cohort Manifest:** `cohort_v1.0.0` (`adf3195e1001...`)
- **Provider:** `mock` (`ScenarioBenchmarkMockProvider`)
- **Total Scenarios:** 40 (0 success, 40 failed)
- **Execution Time:** 0.30 seconds

---

## 1. High-Stakes Safety Metrics

| Safety Metric | Rate | Numerator | Denominator | Violations | Status |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **False Compliance Rate (FCR)** | **0.00%** | 0 | 10 | 0 | ✅ PASS |
| **Unsafe Compliance Rate (UCR)** | **0.00%** | 0 | 30 | 0 | ✅ PASS |

**Definitions:**
- **FCR:** False `COMPLIANT` decisions strictly on ground-truth `NON_COMPLIANT` cases.
- **UCR:** False `COMPLIANT` decisions across ALL unsafe ground-truth categories (`NON_COMPLIANT`, `REQUIRES_HUMAN_REVIEW`, `UNABLE_TO_DETERMINE`).

---

## 2. Submission-Level Evaluation Metrics

- **Overall Status Accuracy:** 0.00% (0/40)
- **Macro F1 Score:** 0.0556
- **Weighted F1 Score:** 0.0278

### Four-Class Confusion Matrix

| Ground Truth \ Predicted | COMPLIANT | NON_COMPLIANT | REQUIRES_HUMAN_REVIEW | UNABLE_TO_DETERMINE |
| :--- | :---: | :---: | :---: | :---: |
| **COMPLIANT** | 0 | 0 | 0 | 10 |
| **NON_COMPLIANT** | 0 | 0 | 0 | 10 |
| **REQUIRES_HUMAN_REVIEW** | 0 | 0 | 0 | 15 |
| **UNABLE_TO_DETERMINE** | 0 | 0 | 0 | 5 |

---

## 3. Finding-Level Compliance Metrics

| Metric | Score | Matches / Total |
| :--- | :---: | :---: |
| **Exact Status Accuracy** | 0.00% | 0/0 |
| **Reason Code Accuracy** | 0.00% | 0/0 |
| **Human Review Accuracy** | 0.00% | 0/0 |
| **Compliance Precision** | 0.0000 | - |
| **Compliance Recall** | 0.0000 | - |
| **Compliance F1 Score** | 0.0000 | - |

---

## 4. Human-Review & Uncertainty Metrics

| Metric | Score | Details |
| :--- | :---: | :--- |
| **Uncertainty Recall** | 0.00% | Ground-truth uncertain submissions successfully escalated |
| **Review Precision** | 100.00% | Precision of human review escalation trigger |
| **Unnecessary Review Rate** | 0.00% | Clean submissions unnecessarily routed to review |
| **Missed Review Rate** | 100.00% | Uncertain submissions missed by review filter |

---

## 5. Evidence Grounding & Support Metrics

| Metric | Score | Count / Total |
| :--- | :---: | :---: |
| **Quotation Exact Match Rate** | 0.00% | 0/0 |
| **Document Attribution Accuracy** | 0.00% | - |
| **Page Attribution Accuracy** | 0.00% | - |
| **Claim-to-Evidence Support Rate (CESR)** | 0.00% | 0/0 |
| **Unsupported Claim Rate (UCR)** | 0.00% | 0/0 |
| **Missing Evidence Rate** | 0.00% | 0/0 |

---

## 6. Per-Scenario Evaluation Summary

| Scenario ID | Expected Status | Predicted Status | Status Match | HR Match | Unsafe Pass | Runtime |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `SCEN-01` | `compliant` | `None` | ❌ | ❌ | OK | 0.02s |
| `SCEN-02` | `compliant` | `None` | ❌ | ❌ | OK | 0.00s |
| `SCEN-03` | `compliant` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-04` | `compliant` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-05` | `compliant` | `None` | ❌ | ❌ | OK | 0.00s |
| `SCEN-06` | `compliant` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-07` | `compliant` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-08` | `compliant` | `None` | ❌ | ❌ | OK | 0.00s |
| `SCEN-09` | `compliant` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-10` | `compliant` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-11` | `non_compliant` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-12` | `non_compliant` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-13` | `non_compliant` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-14` | `non_compliant` | `None` | ❌ | ❌ | OK | 0.00s |
| `SCEN-15` | `non_compliant` | `None` | ❌ | ❌ | OK | 0.00s |
| `SCEN-16` | `non_compliant` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-17` | `non_compliant` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-18` | `non_compliant` | `None` | ❌ | ❌ | OK | 0.00s |
| `SCEN-19` | `non_compliant` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-20` | `non_compliant` | `None` | ❌ | ❌ | OK | 0.00s |
| `SCEN-21` | `requires_human_review` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-22` | `requires_human_review` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-23` | `requires_human_review` | `None` | ❌ | ❌ | OK | 0.00s |
| `SCEN-24` | `requires_human_review` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-25` | `requires_human_review` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-26` | `requires_human_review` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-27` | `requires_human_review` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-28` | `requires_human_review` | `None` | ❌ | ❌ | OK | 0.00s |
| `SCEN-29` | `unable_to_determine` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-30` | `unable_to_determine` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-31` | `unable_to_determine` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-32` | `unable_to_determine` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-33` | `unable_to_determine` | `None` | ❌ | ❌ | OK | 0.00s |
| `SCEN-34` | `requires_human_review` | `None` | ❌ | ❌ | OK | 0.00s |
| `SCEN-35` | `requires_human_review` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-36` | `requires_human_review` | `None` | ❌ | ❌ | OK | 0.00s |
| `SCEN-37` | `requires_human_review` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-38` | `requires_human_review` | `None` | ❌ | ❌ | OK | 0.01s |
| `SCEN-39` | `requires_human_review` | `None` | ❌ | ❌ | OK | 0.00s |
| `SCEN-40` | `requires_human_review` | `None` | ❌ | ❌ | OK | 0.01s |
