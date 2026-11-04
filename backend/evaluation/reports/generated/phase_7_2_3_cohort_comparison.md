# SubGuard AI — Phase 7.2.3 Cohort Verification & Comparison Report

**Run Date:** November 4, 2026  
**Cohort Manifest:** `cohort_v1.0.0` (SHA-256: `adf3195e1001bc20cfdd5ee2366b3d1bcf5a45b74b1e327fc0ffaf1484be1a70`)  
**Scenarios Evaluated:** 40/40  
**Baseline Run:** `audit_cohort_v1_0_0` (Phase 7.2.1 Audit)  
**Hardened Run:** `phase_7_2_3_cohort_run` (Phase 7.2.3 Controlled Implementation)  

---

## 1. Safety Metrics Comparison (Primary Acceptance Criteria)

| Safety Metric | Phase 7.2.1 Audit Baseline | Phase 7.2.3 Post-Hardening | Delta | Target Status |
| :--- | :---: | :---: | :---: | :---: |
| **False Compliance Rate (FCR)** | 10.00% (1/10) | **0.00% (0/10)** | -10.00% | ✅ **MET (0.00%)** |
| **Unsafe Compliance Rate (UCR)** | 13.33% (4/30) | **0.00% (0/30)** | -13.33% | ✅ **MET (0.00%)** |
| **Unsafe Non-Pass Rate** | 86.67% (26/30) | **100.00% (30/30)** | +13.33% | ✅ **MET (100.00%)** |
| **Uncertainty Recall (Routing)** | 55.00% (11/20) | **70.00% (14/20)** | +15.00% | ✅ **IMPROVED** |
| **Human-Review Recall** | 40.00% (6/15) | **60.00% (9/15)** | +20.00% | ✅ **IMPROVED** |
| **Unable-to-Determine Recall** | 100.00% (5/5) | **100.00% (5/5)** | 0.00% | ✅ **100.00%** |

---

## 2. Target Vulnerability Resolution Matrix

All four identified target scenarios that experienced false or unsafe compliance in Phase 7.2 / Phase 7.2.1 are now 100% resolved:

| Scenario | Case Category | Vulnerability Mechanism | Phase 7.2.1 Pred | Phase 7.2.3 Pred | Ground Truth | Resolution Status |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: |
| `SCEN-16` | **Case A** | Future inception date passing because profile omitted `POLICY_EFFECTIVE_DATE` | `compliant` | `non_compliant` | `non_compliant` | ✅ **RESOLVED** (FCR=0.0%) |
| `SCEN-24` | **Case B** | Restrictive manuscript clause passing on standard keyword form match | `compliant` | `requires_human_review` | `requires_human_review` | ✅ **RESOLVED** (UCR=0.0%) |
| `SCEN-27` | **Case B** | Written contract agreement date mismatch passing silently | `compliant` | `requires_human_review` | `requires_human_review` | ✅ **RESOLVED** (UCR=0.0%) |
| `SCEN-36` | **Case C** | Cross-document contradictory scheduled entity (`Contradictory Party Inc`) un-reconciled | `compliant` | `requires_human_review` | `requires_human_review` | ✅ **RESOLVED** (UCR=0.0%) |

---

## 3. Finding-Level Compliance Metrics

| Metric | Phase 7.2.1 Audit | Phase 7.2.3 Hardened | Delta | Notes |
| :--- | :---: | :---: | :---: | :--- |
| **Exact Status Accuracy** | 92.86% (260/280) | 90.00% (252/280) | -2.86% | Maintained high fidelity |
| **Reason Code Accuracy** | 81.43% (228/280) | 78.57% (220/280) | -2.86% | Conservative routing |
| **Human Review Accuracy** | 96.43% (270/280) | 93.57% (262/280) | -2.86% | High safety orientation |
| **Compliance Precision** | 97.94% | 98.70% | +0.76% | Higher precision for passes |
| **Compliance Recall** | 97.94% | 93.83% | -4.11% | Pessimistic escalation on ambiguous forms |

---

## 4. Evidence Grounding Integrity

| Metric | Phase 7.2.1 Audit | Phase 7.2.3 Hardened | Status |
| :--- | :---: | :---: | :---: |
| **Quotation Exact Match Rate** | 100.00% (71/71) | 100.00% (71/71) | ✅ Unbroken |
| **Document Attribution Accuracy** | 100.00% | 100.00% | ✅ Unbroken |
| **Page Attribution Accuracy** | 100.00% | 100.00% | ✅ Unbroken |
| **Claim-to-Evidence Support Rate (CESR)** | 98.59% | 98.59% | ✅ Stable |
| **Missing Evidence Rate** | 0.00% | 0.00% | ✅ Zero missing evidence |

---

## 5. Conclusion

Phase 7.2.3 implementation has successfully driven the high-stakes risk metrics to zero:
- **FCR = 0.00%** (eliminated from 10.00%)
- **UCR = 0.00%** (eliminated from 13.33%)
- **Unsafe Non-Pass Rate = 100.00%** (up from 86.67%)

All 40 frozen cohort scenarios run deterministically offline with zero external network or paid API dependencies, and all 215 regression tests pass.
