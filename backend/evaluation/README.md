# SubGuard AI — Evaluation & Multi-Document Benchmarking Framework

## 1. Overview & Architecture

SubGuard AI evaluates insurance documents according to the immutable principle:
$$\textbf{AI extracts information} \longrightarrow \textbf{Deterministic Python rules evaluate it} \longrightarrow \textbf{Humans resolve uncertainty}$$

The evaluation and benchmarking framework provides rigorous, reproducible, and transparent performance measurement across:
1. **Field extraction accuracy and normalization** (integers cents, ISO dates, entity suffixes).
2. **Evidence grounding and quote verifiability** (verbatim text presence on cited page and semantic support verification).
3. **End-to-end compliance classification** (4-class classification across configured insurance rules).
4. **Safety metrics: False Compliance Rate (FCR) and Unsafe Compliance Rate (UCR)**.
5. **Human-review escalation efficacy** (uncertainty recall vs unnecessary review rate).

```
backend/evaluation/
├── annotations/
│   ├── synthetic_dataset.json            # 8-document single-document baseline
│   └── cohort_scenarios_v1.json          # 40-scenario multi-document ground truth
├── cohorts/
│   ├── generator.py                      # Programmatic 40-scenario PDF and manifest generator
│   ├── manifest.py                       # Manifest verification & SHA-256 integrity checker
│   ├── manifests/
│   │   └── cohort_v1_manifest.json       # Frozen manifest with document hashes & seed
│   └── fixtures/                         # Multi-document PDF packages per scenario (SCEN-01 to SCEN-40)
│       ├── SCEN-01/
│       ├── SCEN-02/
│       └── ...
├── metrics/
│   ├── evaluator.py                      # Single-document field extraction evaluator
│   └── compliance_metrics.py             # Compliance, safety (FCR/UCR), and confusion matrix metrics
├── runners/
│   ├── benchmark.py                      # Single-document CLI benchmark runner
│   └── scenario_runner.py                # Multi-document scenario CLI benchmark runner
├── schemas/
│   ├── ground_truth.py                   # Ground truth Pydantic models (ScenarioGroundTruth, ExpectedFinding)
│   └── reports.py                        # Benchmark report schemas (ScenarioBenchmarkReport, SafetyMetricResult)
└── reports/
    └── generated/                        # Output directory for JSON and Markdown benchmark reports
```

---

## 2. The 40-Scenario Cohort Composition

The standard benchmark cohort (`cohort_v1.0.0`) contains exactly 40 multi-document subcontractor scenarios partitioned into six balanced categories:

1. **Group 1: Clearly Compliant (`SCEN-01` to `SCEN-10`)**
   - Expected status: `COMPLIANT`.
   - Complete multi-document packages (ACORD 25 + CG 20 10, CG 24 04, umbrella, or scheduled endorsements).
   - Limits meet or exceed minimums; policy dates active; legal entities match.

2. **Group 2: Definitive Non-Compliance (`SCEN-11` to `SCEN-20`)**
   - Expected status: `NON_COMPLIANT`.
   - Limits below minimum ($500k vs $1M); expired policies; missing required coverage lines on complete uploads; missing required endorsements; named insured mismatch.

3. **Group 3: Human Review & Ambiguity (`SCEN-21` to `SCEN-28`)**
   - Expected status: `REQUIRES_HUMAN_REVIEW`.
   - Blanket endorsements requiring contract check; corporate suffix discrepancies (`LLC` vs `Inc`); COI checkbox marked without separate endorsement; DBA trade names.

4. **Group 4: Incomplete Document Sets (`SCEN-29` to `SCEN-33`)**
   - Expected status: `UNABLE_TO_DETERMINE` (for unresolved missing requirements).
   - Submissions with missing pages (`is_document_set_complete=False`); positive verified fields remain evaluable while missing required lines escalate cleanly.

5. **Group 5: Cross-Document Contradictions (`SCEN-34` to `SCEN-37`)**
   - Expected status: `REQUIRES_HUMAN_REVIEW`.
   - Contradictory policy numbers between COI and endorsement; conflicting limits between dec page and COI; conflicting endorsement schedules.

6. **Group 6: Adversarial Evidence Grounding (`SCEN-38` to `SCEN-40`)**
   - Expected status: `REQUIRES_HUMAN_REVIEW` or `MISSING_EVIDENCE`.
   - Fabricated quote sharing generic bigrams; citation of named insured header as proof of an endorsement; cross-page attribution mismatch.

---

## 3. How to Run the Benchmark

### Run the Full 40-Scenario Cohort with Mock Provider
From the `backend/` directory:
```bash
python -m evaluation.runners.scenario_runner
```

### Run a Single Scenario
```bash
python -m evaluation.runners.scenario_runner --scenario SCEN-01
```

### Custom Manifest and Output Directory
```bash
python -m evaluation.runners.scenario_runner \
  --manifest evaluation/cohorts/manifests/cohort_v1_manifest.json \
  --annotations evaluation/annotations/cohort_scenarios_v1.json \
  --output-dir evaluation/reports/generated
```

Reports are written to `evaluation/reports/generated/` in both JSON and Markdown formats.

---

## 4. How to Regenerate Fixtures & Freeze Manifest

To regenerate all 40 scenario directories, synthetic PDF fixtures, ground truth annotations, and update the frozen manifest with SHA-256 hashes:
```bash
python -m evaluation.cohorts.generator
```

### Manifest Verification
To independently verify the SHA-256 hashes of all generated PDFs against the manifest:
```bash
python -c "from evaluation.cohorts.manifest import verify_manifest; from pathlib import Path; ok, err = verify_manifest(Path('evaluation/cohorts/manifests/cohort_v1_manifest.json')); print('Manifest valid:', ok, 'Errors:', len(err))"
```

---

## 5. Interpreting High-Stakes Safety Metrics

### False Compliance Rate (FCR)
$$\mathbf{FCR} = \frac{\text{False COMPLIANT predictions on definitely NON\_COMPLIANT cases}}{\text{Total NON\_COMPLIANT cases}}$$
- **Meaning:** Measures catastrophic failures where an unqualified subcontractor with missing coverage, expired insurance, or inadequate limits is approved.
- **Production Target:** $0.00\%$.

### Unsafe Compliance Rate (UCR)
$$\mathbf{UCR} = \frac{\text{COMPLIANT predictions across unsafe cases}}{\text{Total unsafe cases (\text{NON\_COMPLIANT} + \text{REQUIRES\_HUMAN\_REVIEW} + \text{UNABLE\_TO\_DETERMINE})}}$$
- **Meaning:** Measures any compliance approval granted without human resolution on ambiguous, incomplete, or failing submissions.
- **Reporting:** If denominator is 0, reported as `N/A`.

---

## 6. Interpreting the Four-Class Confusion Matrix

The benchmark reports a $4 \times 4$ confusion matrix:
- Rows: Ground Truth (`COMPLIANT`, `NON_COMPLIANT`, `REQUIRES_HUMAN_REVIEW`, `UNABLE_TO_DETERMINE`).
- Columns: Predicted Status (`COMPLIANT`, `NON_COMPLIANT`, `REQUIRES_HUMAN_REVIEW`, `UNABLE_TO_DETERMINE`).

**Key Principles:**
- Uncertainty is never collapsed into non-compliance for the primary confusion matrix.
- `UNABLE_TO_DETERMINE` represents incomplete document sets or missing evidence.
- `REQUIRES_HUMAN_REVIEW` represents contradictions, blanket clauses, or fuzzy entity discrepancies.

---

## 7. Evidence Grounding Measurement

Evidence citations must satisfy two independent levels of verification:
1. **Text Layer Verification:** The verbatim excerpt must match a contiguous sequence in the PDF page text layer (short quotes must match exactly; longer quotes must contain a 4-gram contiguous sequence).
2. **Semantic Support Verification:** The verified text must specifically support the claimed insurance concept. Citing `"INSURED: Apex Electrical Contractors LLC"` as evidence for an Additional Insured endorsement fails semantic support (`ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT`).

---

## 8. Failure Isolation & Reporting

If any scenario encounters a processing error, OCR failure, or malformed PDF:
- The error is isolated using try-catch blocks.
- The failure is explicitly recorded in `report.execution_failures` with `scenario_id`, `error_type`, and `error_message`.
- Execution continues immediately for subsequent scenarios.
- The benchmark never silently drops a scenario or crashes prematurely.

---

## 9. Limitations & Boundary

This benchmark evaluates deterministic compliance decision logic and synthetic document extraction:
- **Synthetic Vector PDFs:** Does not simulate physical scanner degradations, skew, creases, or coffee stains.
- **Sample Power:** A 40-scenario pilot cohort provides qualitative defect discovery and invariant testing, but does not provide tight statistical confidence intervals ($99.9\%$) for production sign-off.
- **Production Invariant:** Live models must be evaluated against frozen manifests with identical Pydantic extraction contracts.
