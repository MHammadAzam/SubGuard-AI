# SubGuard AI Multi-Document Benchmark Report

- **Benchmark Run ID:** `scenario_bench_f24f023c`
- **Timestamp (UTC):** `2026-11-04T14:36:54.810782+00:00`
- **Cohort Manifest:** `cohort_v1.0.0` (`adf3195e1001...`)
- **Provider:** `mock` (`ScenarioBenchmarkMockProvider`)
- **Total Scenarios:** 40 (40 success, 0 failed)
- **Execution Time:** 0.41 seconds

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

- **Overall Status Accuracy:** 77.50% (31/40)
- **Macro F1 Score:** 0.7688
- **Weighted F1 Score:** 0.7890

### Four-Class Confusion Matrix (Standard Projection)

*Note: In the standard 4-class projection, `MISSING_EVIDENCE` predictions are mapped into `UNABLE_TO_DETERMINE`.*

| Ground Truth \ Predicted | COMPLIANT | NON_COMPLIANT | REQUIRES_HUMAN_REVIEW | UNABLE_TO_DETERMINE |
| :--- | :---: | :---: | :---: | :---: |
| **COMPLIANT** | 10 | 0 | 0 | 0 |
| **NON_COMPLIANT** | 0 | 7 | 0 | 3 |
| **REQUIRES_HUMAN_REVIEW** | 0 | 2 | 9 | 4 |
| **UNABLE_TO_DETERMINE** | 0 | 0 | 0 | 5 |

### Five-Class Confusion Matrix (Exact Raw Statuses)

| Ground Truth \ Predicted | COMPLIANT | NON_COMPLIANT | REQUIRES_HUMAN_REVIEW | UNABLE_TO_DETERMINE | MISSING_EVIDENCE |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **COMPLIANT** | 10 | 0 | 0 | 0 | 0 |
| **NON_COMPLIANT** | 0 | 7 | 0 | 0 | 3 |
| **REQUIRES_HUMAN_REVIEW** | 0 | 2 | 9 | 0 | 4 |
| **UNABLE_TO_DETERMINE** | 0 | 0 | 0 | 5 | 0 |

---

## 3. Finding-Level Compliance Metrics

| Metric | Score | Matches / Total |
| :--- | :---: | :---: |
| **Exact Status Accuracy** | 93.93% | 263/280 |
| **Reason Code Accuracy** | 82.14% | 230/280 |
| **Human Review Accuracy** | 97.50% | 273/280 |
| **Compliance Precision** | 0.9917 | - |
| **Compliance Recall** | 0.9794 | - |
| **Compliance F1 Score** | 0.9855 | - |

---

## 4. Human-Review & Uncertainty Metrics (Phase 7.2.1 Audit)

| Metric | Score | Numerator / Denominator | Definition / Scope |
| :--- | :---: | :---: | :--- |
| **Unsafe Non-Pass Rate** | 100.00% | 30 / 30 | All unsafe GT cases predicted anything other than COMPLIANT (100% - UCR) |
| **Uncertainty Recall (Routing)** | 70.00% | 14 / 20 | GT in {Review, Unable} predicted as Review or Unable |
| **Human-Review Recall** | 60.00% | 9 / 15 | GT Review predicted strictly as REQUIRES_HUMAN_REVIEW |
| **Unable-to-Determine Recall** | 100.00% | 5 / 5 | GT Unable predicted strictly as UNABLE_TO_DETERMINE |
| **Review Precision** | 100.00% | - | Predicted Review cases that were ground-truth Review |
| **Unnecessary Review Rate** | 0.00% | 0 / 10 | Clean compliant submissions routed to review |
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
| `SCEN-01` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.02s |
| `SCEN-02` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.02s |
| `SCEN-03` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.02s |
| `SCEN-04` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.00s |
| `SCEN-05` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-06` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.02s |
| `SCEN-07` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.00s |
| `SCEN-08` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.02s |
| `SCEN-09` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-10` | `compliant` | `compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-11` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-12` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.02s |
| `SCEN-13` | `non_compliant` | `missing_evidence` | ❌ | ✅ | OK | 0.00s |
| `SCEN-14` | `non_compliant` | `missing_evidence` | ❌ | ✅ | OK | 0.02s |
| `SCEN-15` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.00s |
| `SCEN-16` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.01s |
| `SCEN-17` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.00s |
| `SCEN-18` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.02s |
| `SCEN-19` | `non_compliant` | `missing_evidence` | ❌ | ✅ | OK | 0.01s |
| `SCEN-20` | `non_compliant` | `non_compliant` | ✅ | ✅ | OK | 0.00s |
| `SCEN-21` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.02s |
| `SCEN-22` | `requires_human_review` | `non_compliant` | ❌ | ✅ | OK | 0.00s |
| `SCEN-23` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.00s |
| `SCEN-24` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.01s |
| `SCEN-25` | `requires_human_review` | `missing_evidence` | ❌ | ✅ | OK | 0.00s |
| `SCEN-26` | `requires_human_review` | `non_compliant` | ❌ | ✅ | OK | 0.00s |
| `SCEN-27` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.02s |
| `SCEN-28` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.00s |
| `SCEN-29` | `unable_to_determine` | `unable_to_determine` | ✅ | ✅ | OK | 0.02s |
| `SCEN-30` | `unable_to_determine` | `unable_to_determine` | ✅ | ✅ | OK | 0.00s |
| `SCEN-31` | `unable_to_determine` | `unable_to_determine` | ✅ | ✅ | OK | 0.00s |
| `SCEN-32` | `unable_to_determine` | `unable_to_determine` | ✅ | ✅ | OK | 0.02s |
| `SCEN-33` | `unable_to_determine` | `unable_to_determine` | ✅ | ✅ | OK | 0.01s |
| `SCEN-34` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.01s |
| `SCEN-35` | `requires_human_review` | `missing_evidence` | ❌ | ✅ | OK | 0.01s |
| `SCEN-36` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.02s |
| `SCEN-37` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.00s |
| `SCEN-38` | `requires_human_review` | `requires_human_review` | ✅ | ✅ | OK | 0.00s |
| `SCEN-39` | `requires_human_review` | `missing_evidence` | ❌ | ✅ | OK | 0.02s |
| `SCEN-40` | `requires_human_review` | `missing_evidence` | ❌ | ✅ | OK | 0.00s |
