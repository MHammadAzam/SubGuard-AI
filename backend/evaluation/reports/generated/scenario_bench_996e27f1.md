# SubGuard AI Multi-Document Benchmark Report

- **Benchmark Run ID:** `scenario_bench_996e27f1`
- **Timestamp (UTC):** `2026-11-03T18:20:34.547755+00:00`
- **Cohort Manifest:** `cohort_v1.0.0` (`adf3195e1001...`)
- **Provider:** `mock` (`ScenarioBenchmarkMockProvider`)
- **Total Scenarios:** 40 (40 success, 0 failed)
- **Execution Time:** 0.40 seconds

---

## 1. High-Stakes Safety Metrics

| Safety Metric | Rate | Numerator | Denominator | Violations | Status |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **False Compliance Rate (FCR)** | **10.00%** | 1 | 10 | 1 | ❌ CRITICAL RISK |
| **Unsafe Compliance Rate (UCR)** | **13.33%** | 4 | 30 | 4 | ❌ CRITICAL RISK |

**Definitions:**
- **FCR:** False `COMPLIANT` decisions strictly on ground-truth `NON_COMPLIANT` cases.
- **UCR:** False `COMPLIANT` decisions across ALL unsafe ground-truth categories (`NON_COMPLIANT`, `REQUIRES_HUMAN_REVIEW`, `UNABLE_TO_DETERMINE`).

---

## 2. Submission-Level Evaluation Metrics

- **Overall Status Accuracy:** 67.50% (27/40)
- **Macro F1 Score:** 0.6649
- **Weighted F1 Score:** 0.6628

### Four-Class Confusion Matrix

| Ground Truth \ Predicted | COMPLIANT | NON_COMPLIANT | REQUIRES_HUMAN_REVIEW | UNABLE_TO_DETERMINE |
| :--- | :---: | :---: | :---: | :---: |
| **COMPLIANT** | 10 | 0 | 0 | 0 |
| **NON_COMPLIANT** | 1 | 6 | 0 | 3 |
| **REQUIRES_HUMAN_REVIEW** | 3 | 2 | 6 | 4 |
| **UNABLE_TO_DETERMINE** | 0 | 0 | 0 | 5 |

---

## 3. Finding-Level Compliance Metrics

| Metric | Score | Matches / Total |
| :--- | :---: | :---: |
| **Exact Status Accuracy** | 92.86% | 260/280 |
| **Reason Code Accuracy** | 81.43% | 228/280 |
| **Human Review Accuracy** | 96.43% | 270/280 |
| **Compliance Precision** | 0.9794 | - |
| **Compliance Recall** | 0.9794 | - |
| **Compliance F1 Score** | 0.9794 | - |

---

## 4. Human-Review & Uncertainty Metrics

| Metric | Score | Details |
| :--- | :---: | :--- |
| **Uncertainty Recall** | 86.67% | Ground-truth uncertain submissions successfully escalated |
| **Review Precision** | 100.00% | Precision of human review escalation trigger |
| **Unnecessary Review Rate** | 0.00% | Clean submissions unnecessarily routed to review |
| **Missed Review Rate** | 13.33% | Uncertain submissions missed by review filter |

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
| `SCEN-01` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.03s |
| `SCEN-02` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-03` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-04` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-05` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-06` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-07` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-08` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.02s |
| `SCEN-09` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-10` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-11` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.00s |
| `SCEN-12` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.02s |
| `SCEN-13` | `non_compliant` | `missing_evidence` | ❌ | ✅ | OK | 0.01s |
| `SCEN-14` | `non_compliant` | `missing_evidence` | ❌ | ✅ | OK | 0.00s |
| `SCEN-15` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-16` | `non_compliant` | `compliant` | ❌ | ❌ | ⚠️ UNCHECKED | 0.01s |
| `SCEN-17` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-18` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-19` | `non_compliant` | `missing_evidence` | ❌ | ✅ | OK | 0.01s |
| `SCEN-20` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-21` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.01s |
| `SCEN-22` | `requires_human_review` | `non_compliant` | ❌ | ✅ | OK | 0.00s |
| `SCEN-23` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.01s |
| `SCEN-24` | `requires_human_review` | `compliant` | ❌ | ❌ | ⚠️ UNCHECKED | 0.01s |
| `SCEN-25` | `requires_human_review` | `missing_evidence` | ❌ | ✅ | OK | 0.01s |
| `SCEN-26` | `requires_human_review` | `non_compliant` | ❌ | ✅ | OK | 0.00s |
| `SCEN-27` | `requires_human_review` | `compliant` | ❌ | ❌ | ⚠️ UNCHECKED | 0.01s |
| `SCEN-28` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.01s |
| `SCEN-29` | `unable_to_determine` | `unable_to_determine` | ✅ | ✅ | OK | 0.01s |
| `SCEN-30` | `unable_to_determine` | `unable_to_determine` | ✅ | ✅ | OK | 0.01s |
| `SCEN-31` | `unable_to_determine` | `unable_to_determine` | ✅ | ✅ | OK | 0.00s |
| `SCEN-32` | `unable_to_determine` | `unable_to_determine` | ✅ | ✅ | OK | 0.01s |
| `SCEN-33` | `unable_to_determine` | `unable_to_determine` | ✅ | ✅ | OK | 0.00s |
| `SCEN-34` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.01s |
| `SCEN-35` | `requires_human_review` | `missing_evidence` | ❌ | ✅ | OK | 0.01s |
| `SCEN-36` | `requires_human_review` | `compliant` | ❌ | ❌ | ⚠️ UNCHECKED | 0.01s |
| `SCEN-37` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.01s |
| `SCEN-38` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.01s |
| `SCEN-39` | `requires_human_review` | `missing_evidence` | ❌ | ✅ | OK | 0.00s |
| `SCEN-40` | `requires_human_review` | `missing_evidence` | ❌ | ✅ | OK | 0.01s |
