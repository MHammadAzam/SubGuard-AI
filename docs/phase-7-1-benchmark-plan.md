# SubGuard AI — Phase 7.1 Multi-Document Cohort Benchmarking & Evaluation Plan

**Document ID:** `DOC-P71-PLAN-20261103`  
**Phase:** 7.1 — Benchmarking & Evaluation Planning  
**Date:** November 3, 2026  
**Status:** Planning & Architectural Specification (READ-ONLY)  
**Author:** Principal Insurance Technology Architect & Systems Auditor  

---

## 1. Executive Summary

SubGuard AI operates on the immutable principle:
$$\textbf{AI extracts information} \longrightarrow \textbf{Deterministic Python rules evaluate it} \longrightarrow \textbf{Humans resolve uncertainty}$$

Phases 5 and 6 established pipeline reliability, hardened evidence grounding against hallucinated bigrams and generic tokens, enforced incomplete document-set symmetry across all rule types, and eliminated silent compliance on partial extraction states. The unit and integration test baseline stands independently verified at **185 passing tests (0 failures, 0 errors)**.

However, all existing tests and benchmarks rely on an 8-document single-page/two-page synthetic corpus. To establish true engineering confidence prior to production deployment, SubGuard AI requires a **multi-document cohort benchmarking framework**. This framework must evaluate end-to-end multi-document subcontractor submission scenarios (30–50 document sets), measuring not only raw field extraction accuracy and evidence text layer grounding, but also **end-to-end compliance classification accuracy**, **uncertainty escalation efficacy**, and the critical safety metric: **False-Compliant Rate**.

This document defines the complete architectural design, dataset composition, ground-truth annotation schema, statistical methodology, provider comparison protocol, and execution roadmap for Phase 7.1.

---

## 2. Repository Architecture & Document-Processing Flow Audit

### 2.1 Complete End-to-End Processing Flow

```
+----------------------------------------------------------------------------------------------------+
| 1. INGESTION & PARSING (backend/app/documents/processor.py)                                        |
|    PDF bytes -> PDFProcessor.process_pdf() -> PyMuPDF (fitz) text/span extraction                  |
|    Output: PDFDocumentInspection (pages: List[PDFPageInspection], is_scanned, classified_type)     |
+----------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+----------------------------------------------------------------------------------------------------+
| 2. EXTRACTION ORCHESTRATION (backend/app/extraction/pipeline.py)                                   |
|    ExtractionPipelineService.process_inspection()                                                  |
|    - If is_scanned and no text layer -> routes to BaseOCRProvider (app/extraction/ocr.py)          |
|    - Invokes BaseExtractionProvider.extract_insurance_data()                                       |
|      * GeminiExtractionProvider (app/extraction/providers/gemini.py)                               |
|      * OpenRouterExtractionProvider (app/extraction/providers/openrouter.py)                       |
|      * MockExtractionProvider (app/extraction/providers/mock.py)                                   |
|    Output: RawInsuranceExtraction (raw strings, bounding boxes, raw quotes)                         |
+----------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+----------------------------------------------------------------------------------------------------+
| 3. NORMALIZATION & EVIDENCE RESOLUTION (backend/app/extraction/)                                   |
|    - Currency -> integer cents (app/extraction/normalizer.py: normalize_currency_to_cents)         |
|    - Dates -> ISO datetime.date (app/extraction/normalizer.py: normalize_date)                     |
|    - Evidence Verification (app/extraction/pipeline.py: _resolve_evidence)                         |
|      * Normalized sequence matching, short-quote exact match, 4-gram contiguous sequence match      |
|    - Semantic Support Check (app/extraction/pipeline.py)                                           |
|      * _verify_coverage_semantic_support()                                                         |
|      * _verify_endorsement_semantic_support() (strictly typed; generic tokens eliminated)          |
|      * _verify_entity_semantic_support()                                                           |
|    Output: ExtractedDocumentSet (app/schemas/insurance.py)                                         |
+----------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+----------------------------------------------------------------------------------------------------+
| 4. DETERMINISTIC COMPLIANCE EVALUATION (backend/app/compliance/evaluator.py)                       |
|    ComplianceEvaluator.evaluate(doc_set, requirements, evaluation_date)                            |
|    - evaluate_coverage_limit()           -> Checks amounts in cents & is_document_set_complete     |
|    - evaluate_policy_expiration()        -> Checks dates vs evaluation_date & completeness          |
|    - evaluate_policy_effective_date()     -> Checks effective date validity & completeness          |
|    - evaluate_named_insured()            -> Entity suffix normalization & exact/fuzzy match        |
|    - evaluate_endorsement_presence()     -> Multi-endorsement aggregation & blanket contract check  |
|    - _aggregate_overall_status()         -> Pessimistic cascade (Contradictions force Review)      |
|    Output: ComplianceReviewResult (findings: List[ComplianceFinding], overall_status)              |
+----------------------------------------------------------------------------------------------------+
```

### 2.2 Component Mapping & Technical Inventory

| Architecture Function | File Path | Class / Function Name | Data Contract / Model |
| :--- | :--- | :--- | :--- |
| **Field Generation** | `backend/app/extraction/providers/` | `extract_insurance_data()` | `RawInsuranceExtraction` |
| **Evidence Resolution** | `backend/app/extraction/pipeline.py` | `_resolve_evidence()` | `EvidenceReference` |
| **Semantic Grounding** | `backend/app/extraction/pipeline.py` | `_verify_*_semantic_support()` | `ExtractionStatus` |
| **Confidence Model** | `backend/app/schemas/insurance.py` | Per-field `Optional[float]` | `CONFIDENCE_THRESHOLD = 0.70` |
| **Set Completeness** | `backend/app/schemas/insurance.py` | `ExtractedDocumentSet` | `is_document_set_complete: bool` |
| **Compliance Decision** | `backend/app/compliance/evaluator.py` | `ComplianceEvaluator.evaluate()` | `ComplianceReviewResult` |
| **Human Review Triggers** | `backend/app/compliance/evaluator.py` | `_aggregate_overall_status()` | `requires_human_review: bool` |
| **Stateless API Boundary**| `backend/app/main.py` | `evaluate_compliance()` | `POST /api/v1/compliance/evaluate` |

### 2.3 Independent Sub-Component Benchmarkability

1. **OCR Layer:** Can be benchmarked independently via character error rate (CER) and word error rate (WER) on synthetic image-only PDFs (`synthetic_scanned_ocr.pdf`).
2. **Extraction Providers:** Can be benchmarked in isolation for schema compliance, raw field recall, and JSON error rates without invoking the compliance evaluator.
3. **Evidence Grounding Engine:** Can be benchmarked directly by feeding known ground-truth text variations to `_resolve_evidence()` to measure true positive quote matches vs. hallucination rejection rates.
4. **Deterministic Compliance Evaluator:** Can be benchmarked with 100% determinism by feeding synthetic `ExtractedDocumentSet` fixtures directly to `ComplianceEvaluator.evaluate()` without calling any AI models.

---

## 3. Existing Benchmark & Evaluation Capabilities Audit

The existing benchmarking infrastructure in `backend/evaluation/` provides a solid foundational baseline, but has key gaps that Phase 7.1 must address:

### Existing Strengths:
- **`MetricsEvaluator` (`backend/evaluation/metrics/evaluator.py`):** 1,054 lines of code computing field-level exact match, precision, recall, F1, and evidence grounding against PDF text layers.
- **Ground Truth Schema (`backend/evaluation/schemas/ground_truth.py`):** Structured Pydantic contracts (`EvaluationDataset`, `DocumentGroundTruth`, `ExpectedCoverage`, `ExpectedEvidence`).
- **Benchmark Runner (`backend/evaluation/runners/benchmark.py`):** CLI runner supporting provider selection (`mock`, `gemini`, `openrouter`), document limits, structured JSON, and Markdown report generation.
- **Redaction & Privacy Engine:** Automatic scrubbing of API keys (`[REDACTED_API_KEY]`) and bearer tokens in logs and reports.

### Existing Gaps to be Resolved in Phase 7.1:
1. **Single-Document Paradigm:** The current runner processes one PDF file at a time (`DocumentGroundTruth`). It has no concept of a **multi-document scenario** (e.g. an ACORD 25 + 2 separate endorsement forms belonging to one subcontractor).
2. **Lack of End-to-End Compliance Benchmarking:** Existing benchmarks only evaluate extraction accuracy. They do not run the `ComplianceEvaluator` or measure compliance classification outcomes (`COMPLIANT`, `NON_COMPLIANT`, `REQUIRES_HUMAN_REVIEW`).
3. **Absence of False-Compliant Tracking:** The current report schemas (`backend/evaluation/schemas/reports.py`) track field accuracy and F1 score, but have no metric or alert for false-compliant classifications.
4. **Small Dataset Size:** The dataset (`synthetic_dataset.json`) contains only 8 synthetic documents.

---

## 4. Benchmark Dataset Design

### 4.1 Document Categories Covered
1. **ACORD 25 Certificates of Liability Insurance:** Standard ACORD 25 (2016/03, 2014/01, 2010/05 editions) representing commercial general liability, automobile liability, umbrella/excess liability, and workers' compensation.
2. **General Liability Policy Declarations:** 1–3 page dec sheets stating policy limits, deductibles, effective/expiration dates, and scheduled forms.
3. **Workers' Compensation & Employers' Liability Dec Sheets:** State-specific coverage listings, statutory limits, and split disease limits.
4. **Commercial Auto Declarations:** Symbol coverage listings (Symbol 1 Any Auto vs Symbol 7 Scheduled Autos), combined single limits.
5. **Umbrella / Excess Liability Declarations:** Underlying policy schedules, self-insured retentions (SIR), drop-down terms.
6. **Standard ISO Endorsement Forms:**
   - `CG 20 10` (Additional Insured — Owners, Lessees or Contractors — Scheduled Person)
   - `CG 20 37` (Additional Insured — Completed Operations)
   - `CG 20 26` (Additional Insured — Designated Person or Organization)
   - `CG 24 04` (Waiver of Transfer of Rights of Recovery Against Others)
   - `CA 20 48` (Designated Insured for Covered Autos)
   - `WC 00 03 13` (Waiver of Our Right to Recover From Others)
7. **Manuscript & Non-Standard Endorsement Forms:** Proprietary carrier wordings with custom automatic / blanket clauses and specific project limitation schedules.

### 4.2 Realistic Real-World Variations
- **Multi-Document Subcontractor Sets:** ACORD 25 referencing attached forms + 2 separate standalone endorsement PDFs.
- **Cross-Document Contradictions:** Policy number on COI differs from policy number on the endorsement page; dates on dec page differ from COI.
- **Incomplete Uploads:** COI remarks cite "See attached CG 20 10", but only Page 1 is uploaded (`is_document_set_complete=False`).
- **Entity Name Variations:** Legal name "Apex Electrical Contractors LLC" vs trade DBA "Apex Power Solutions" vs suffix variation "Apex Electrical Inc".
- **Degraded Quality & OCR Fallback:** Synthetic generation of rotation (90/180/270 deg), speckle noise, resolution downsampling (100–150 DPI), and simulated fax headers.

---

## 5. Document-Set Scenario Composition (40-Scenario Pilot Cohort)

A benchmark scenario represents a complete subcontractor submission evaluated against a standardized set of prime contractor insurance requirements (e.g. GL $1M/$2M, Auto $1M, WC $1M, Additional Insured, and Waiver of Subrogation).

The proposed 40-scenario cohort is partitioned across six balanced evaluation classes:

```
+----------------------------------------------------------------------------------------------------+
|                                    40-SCENARIO BENCHMARK COHORT                                    |
+----------------------------------------------------------------------------------------------------+
| 1. Clearly Compliant Cases             (10 scenarios / 25%)  -> Expected: COMPLIANT                |
| 2. Clearly Non-Compliant Cases         (10 scenarios / 25%)  -> Expected: NON_COMPLIANT            |
| 3. Human Review / Uncertainty Cases     (8 scenarios / 20%)  -> Expected: REQUIRES_HUMAN_REVIEW    |
| 4. Incomplete Document Sets             (5 scenarios / 12.5%) -> Expected: UNABLE_TO_DETERMINE     |
| 5. Cross-Document Contradictions        (4 scenarios / 10%)   -> Expected: REQUIRES_HUMAN_REVIEW    |
| 6. Adversarial Evidence Hallucinations  (3 scenarios / 7.5%)  -> Expected: UNABLE / REVIEW          |
+----------------------------------------------------------------------------------------------------+
```

### Detailed Scenario Breakdown

| Scenario Group | Scenario ID Range | Description & Target Edge Cases | Ground-Truth Expected Status | Key Reason Code |
| :--- | :--- | :--- | :--- | :--- |
| **Group 1: Clean Compliance** | `SCEN-01` to `SCEN-10` | Full ACORD 25 + matching CG 20 10 & CG 24 04 forms, active dates, clean entity match, limits $\ge$ requirements. | `COMPLIANT` | `ALL_REQUIREMENTS_SATISFIED` |
| **Group 2: Definitive Non-Compliance** | `SCEN-11` to `SCEN-20` | Insufficient occurrence limits ($500k vs $1M); expired policy dates; missing required coverage (no WC); completely missing endorsement on complete upload. | `NON_COMPLIANT` | `LIMIT_BELOW_MINIMUM`<br>`POLICY_EXPIRED`<br>`COVERAGE_NOT_FOUND`<br>`ENDORSEMENT_NOT_FOUND` |
| **Group 3: Human Review / Ambiguity** | `SCEN-21` to `SCEN-28` | Blanket endorsement requiring contract check; corporate suffix mismatch (LLC vs Inc); ACORD 25 checkbox without endorsement document. | `REQUIRES_HUMAN_REVIEW` | `BLANKET_ENDORSEMENT_REQUIRES_CONTRACT_CHECK`<br>`COI_CHECKBOX_WITHOUT_ENDORSEMENT_DOC`<br>`NAMED_INSURED_CORPORATE_SUFFIX_DISCREPANCY` |
| **Group 4: Incomplete Uploads** | `SCEN-29` to `SCEN-33` | Subcontractor upload truncated: Page 1 of multi-page COI provided; WC or required Additional Insured on un-uploaded pages (`is_document_set_complete=False`). | `UNABLE_TO_DETERMINE` | `INCOMPLETE_DOCUMENT_SET_COVERAGE_UNKNOWN`<br>`INCOMPLETE_DOCUMENT_SET_ENDORSEMENT_UNKNOWN` |
| **Group 5: Contradictory Records** | `SCEN-34` to `SCEN-37` | Multiple matching endorsements linked to conflicting policies; contradictory limits for same coverage type on dec page vs COI. | `REQUIRES_HUMAN_REVIEW` | `CONTRADICTORY_ENDORSEMENTS_FOUND`<br>`CONTRADICTORY_LIMITS_FOUND` |
| **Group 6: Adversarial Grounding** | `SCEN-38` to `SCEN-40` | Fabricated quote sharing only generic words (`"liability insurance"`); quote citing `"INSURED: [Name]"` as endorsement proof. | `REQUIRES_HUMAN_REVIEW` or `MISSING_EVIDENCE` | `ENDORSEMENT_EVIDENCE_SUPPORT_UNVERIFIED`<br>`ENDORSEMENT_EVIDENCE_TEXT_MISSING` |

---

## 6. Ground-Truth Annotation Schema Design

To evaluate multi-document scenarios and end-to-end compliance, the schema expands beyond `DocumentGroundTruth` into a top-level `ScenarioGroundTruth`:

```python
class ExpectedFinding(BaseModel):
    requirement_id: str
    rule_type: RequirementType
    expected_status: ComplianceStatus
    expected_reason_code: str
    requires_human_review: bool
    expected_supporting_document_id: Optional[str] = None
    expected_supporting_page: Optional[int] = None
    expected_verbatim_quote: Optional[str] = None

class ScenarioGroundTruth(BaseModel):
    scenario_id: str = Field(description="Unique stable scenario identifier, e.g. SCEN-01")
    title: str
    description: str
    subcontractor_id: str
    evaluation_date: dt_date
    is_document_set_complete: bool = True
    
    # Associated documents in this scenario submission
    document_ids: List[str] = Field(description="List of document IDs belonging to this scenario")
    documents: List[DocumentGroundTruth] = Field(description="Field-level ground truth per document")
    
    # Configured requirements for this benchmark scenario
    requirements: List[InsuranceRequirement]
    
    # Ground truth compliance expectations
    expected_overall_status: ComplianceStatus
    expected_requires_human_review: bool
    expected_findings: List[ExpectedFinding]
    
    # Explicit scenario classification flags
    is_incomplete_submission: bool = False
    is_contradictory_submission: bool = False
    is_adversarial_case: bool = False
    annotation_notes: Optional[str] = None
```

### Absence & Verifiability Classification Matrix
For every annotated claim, the ground truth must explicitly distinguish:
- **`ABSENT_VERIFIED`:** Confirmed that field/endorsement is not on the document.
- **`ABSENT_TRUNCATED`:** Field is not present because document set is incomplete (`is_document_set_complete=False`).
- **`PRESENT_VERIFIABLE`:** Field is present and supported by exact text layer quotes.
- **`PRESENT_UNREADABLE`:** Field is in scanned image but degraded beyond OCR threshold.
- **`CONTRADICTORY`:** Field is present in multiple places with conflicting values.

---

## 7. Evaluation Metrics & Mathematical Formulations

### A. Field Extraction Metrics
- **Exact Match Rate:** $\text{EM} = \frac{1}{N} \sum_{i=1}^N \mathbb{I}(\hat{y}_i = y_i^*)$
- **Currency Normalized Accuracy:** Evaluated in integer cents ($\hat{c} = c^*$).
- **Date Accuracy:** Strict ISO date equality ($\hat{d} = d^*$).
- **Entity Accuracy:** Suffix-normalized token match ($\text{Clean}(\hat{e}) = \text{Clean}(e^*)$).

### B. Evidence-Grounding Metrics
- **Evidence Quotation Exact-Match Rate:** Percentage of extracted quotes present verbatim on the specified page text layer.
- **Page Attribution Accuracy:** Percentage of quotes correctly attributed to the true 1-indexed page.
- **Claim-to-Evidence Support Rate (CESR):**
  $$\text{CESR} = \frac{\text{Extracted claims where evidence text specifically supports the field value}}{\text{Total extracted claims}}$$
- **Unsupported Claim Rate (UCR):** Percentage of claims with missing evidence or unverified semantic support (`ExtractionStatus.QUOTE_PRESENT_UNVERIFIED_SUPPORT` / `ExtractionStatus.UNCERTAIN`).

### C. Compliance Decision Metrics
- **Macro & Weighted Precision, Recall, F1:** Standard multi-class classification metrics across `COMPLIANT`, `NON_COMPLIANT`, `REQUIRES_HUMAN_REVIEW`, and `UNABLE_TO_DETERMINE`.
- **Confusion Matrix:** $4 \times 4$ contingency table tracking prediction shifts.

### D. Critical Safety Metric: False-Compliant Rate (FCR)
In insurance compliance, a false-compliant decision exposes prime contractors to uninsured third-party liabilities:

$$\mathbf{FCR} = \frac{\sum \mathbb{I}(\text{Prediction} = \text{COMPLIANT} \land \text{GroundTruth} \neq \text{COMPLIANT})}{\sum \mathbb{I}(\text{GroundTruth} \neq \text{COMPLIANT})}$$

- **Reporting Standard:** The benchmark report must explicitly state the numerator (count of false passes), the denominator (total non-compliant, review, and unable cases), and individual inspection links for any false-compliant scenario.
- **Zero-Tolerance Target:** In production qualification, target $\text{FCR} = 0.00\%$.

### E. Uncertainty Escalation Metrics
- **Uncertainty Recall (UR):** Fraction of ground-truth ambiguous, incomplete, or low-confidence submissions successfully routed to human review or unable to determine.
- **Unnecessary Review Rate (Over-Escalation):** Fraction of clearly compliant submissions unnecessarily routed to human review (measuring system operational overhead).

---

## 8. Evidence-Grounding Evaluation Methodology

Quotation verification must never conflate textual presence with semantic validity:

```
[Extracted Field & Quote]
         |
         v
+-------------------------------+       NO
| 1. Text Layer Verification    | -------------> [FAIL: Hallucinated Quote]
|    Does normalized quote      |
|    exist on cited page?       |
+-------------------------------+
         | YES
         v
+-------------------------------+       NO
| 2. Semantic Support Check     | -------------> [FAIL: Unsupported Claim]
|    Does the verified quote    |                Status: QUOTE_PRESENT_UNVERIFIED_SUPPORT
|    specifically support the   |                (e.g. Quoting 'INSURED' for an Endorsement)
|    claimed field/endorsement? |
+-------------------------------+
         | YES
         v
[PASS: Grounded & Semantically Verified]
```

### Adversarial Evaluation Protocol
1. Feed synthetic documents containing legitimate COI headers alongside injected adversarial prompts claiming endorsements.
2. Measure whether `_verify_endorsement_semantic_support()` flags the finding as unverified support.
3. Verify that `_resolve_evidence()` rejects quotes fabricated with partial 2-word bigrams.

---

## 9. Benchmark Runner Architecture

### Proposed Architecture & Directory Layout (To be implemented in Phase 7.2)

```
backend/evaluation/
├── annotations/
│   ├── synthetic_dataset.json            # Existing 8-document baseline
│   └── cohort_scenarios_v1.json          # Proposed 40-scenario multi-document ground truth
├── cohorts/
│   ├── manifests/
│   │   └── cohort_v1_manifest.json       # Frozen manifest of scenario files and requirements
│   └── fixtures/                         # Multi-document PDF packages per scenario
├── runners/
│   ├── benchmark.py                      # Existing single-document benchmark runner
│   └── scenario_runner.py                # Proposed multi-document scenario CLI runner
├── metrics/
│   ├── evaluator.py                      # Existing field extraction metrics evaluator
│   └── compliance_metrics.py             # Proposed compliance FCR and confusion matrix calculator
├── reports/
│   ├── schema.py                         # Updated benchmark report data models
│   └── generators/                       # Markdown, JSON, and ASCII summary generators
```

### Reproducibility Metadata Logged per Benchmark Run:
- Run ID (UUID4) and UTC timestamp.
- Benchmark cohort version (`cohort_v1.0.0`) and manifest SHA-256 hash.
- Git commit hash and branch.
- Python runtime, OS, and PyMuPDF/Pydantic package versions.
- Extraction provider (`gemini`, `openrouter`, `mock`) and exact model string.
- Provider parameters (temperature, max tokens, system prompt hash).
- Execution latency per scenario and total elapsed time.
- Redacted configuration (Zero credentials or API keys logged).

---

## 10. Provider Comparison Strategy

To ensure a fair, rigorous, and scientific comparison between AI extraction providers:

### Comparison Matrix
1. **Identical Test Inputs:** Exactly the same 40-scenario cohort PDFs and document text layers are submitted to each provider.
2. **Identical Prompts & Contracts:** System prompts enforce identical Pydantic extraction schemas (`RawInsuranceExtraction`).
3. **Identical Compliance Engine:** All extracted document sets are evaluated by the exact same deterministic `ComplianceEvaluator` with fixed evaluation dates.
4. **Decoupled Evaluation:**
   - **Provider Metric:** Field accuracy, JSON syntax errors, API timeouts, schema validation failures.
   - **Engine Metric:** Reason code accuracy, false-compliant rate, uncertainty escalation.

### Low-Cost & Safe Execution Policy:
- **Baseline Provider:** `BenchmarkMockProvider` (Zero cost, deterministic validation of pipeline logic).
- **Free Live Provider:** `OpenRouterExtractionProvider` with free-tier model (`nvidia/nemotron-3.5-super-vl:free`).
- **Commercial Live Provider:** `GeminiExtractionProvider` (`gemini-2.0-flash`), requiring explicit operator authorization and API key setup.
- **Provider Failure Isolation:** HTTP 429 rate limits, timeouts, and JSON decode errors are tallied in a separate `ProviderReliabilityStats` block, ensuring network issues do not distort compliance evaluator metrics.

---

## 11. Statistical Methodology & Confidence Assessment

### What a 40-Scenario Pilot Cohort Establishes:
- **Proof of Architectural Invariants:** Confirms that deterministic rules, confidence thresholds, and incomplete document-set escalations operate without regressions across complex scenarios.
- **Qualitative Edge Case Discovery:** Identifies prompt contract misunderstandings, layout parsing issues, and edge-case reason code mismatches.
- **Zero-Tolerance False Compliance Detection:** A single false-compliant finding in 40 scenarios immediately alerts engineering to an invalid evaluation path.

### Limitations of Small Sample Size:
- A cohort of $N=40$ scenarios is not sufficient to claim high-confidence statistical significance (e.g. $99.9\%$ confidence intervals) for production deployment.
- With $N=40$, a measured pass rate of $95\%$ has a binomial 95% Wilson score confidence interval of $[83.5\%, 98.6\%]$.
- **Methodology Recommendation:** Phase 7.1 establishes the frozen benchmark methodology, schemas, and 40-scenario synthetic pilot. Phase 7.3 will expand the cohort to $N \ge 250$ document sets (including authorized anonymized samples) to achieve tight production statistical bounds.

---

## 12. Cost & Execution Estimates

| Component | Provider / Configuration | Estimated Runtime | Estimated Cost |
| :--- | :--- | :---: | :---: |
| **Deterministic Validation** | `BenchmarkMockProvider` (In-memory) | $< 5\text{ seconds}$ | **$0.00** |
| **Free OpenRouter Benchmark** | `nvidia/nemotron-3.5-super-vl:free` (40 docs) | $20 - 30\text{ minutes}$ (rate-limited) | **$0.00** |
| **Google Gemini Benchmark** | `gemini-2.0-flash` (40 docs) | $2 - 4\text{ minutes}$ | $\approx \$0.15 - \$0.25$ |

---

## 13. Risks & Known Limitations

1. **Synthetic Bias:** Synthetic PDFs have crisp vector text. Model extraction success on synthetic PDFs does not guarantee equal performance on wrinkled, scanned, or low-resolution physical documents.
2. **Rate Limiting on Free OpenRouter Tiers:** Public free-tier models on OpenRouter enforce strict requests-per-minute limits. The benchmark runner must implement bounded exponential backoff to prevent run aborts.
3. **Data Privacy Protection:** Real contractor documents must **never** be used in open benchmarks or submitted to free public APIs. Only synthetic or legally sanitized, authorized public forms may be added to cohort manifests.

---

## 14. Proposed Implementation Sequence (Phased Roadmap)

```
[Phase 7.1: Planning & Specification]  <-- CURRENT PHASE (READ-ONLY)
         |
         v
[Phase 7.2: Cohort Infrastructure & Scenario Generation]
         | - Generate multi-document PDF scenario packages (SCEN-01 to SCEN-40)
         | - Create cohort_scenarios_v1.json ground truth annotations
         | - Build scenario_runner.py CLI and compliance evaluation metrics calculator
         v
[Phase 7.3: Benchmark Execution, Provider Comparison & Reporting]
         | - Execute mock baseline run (0 cost, deterministic)
         | - Execute OpenRouter free model benchmark
         | - Execute Gemini benchmark (if authorized)
         | - Generate comprehensive comparison report and FCR audit
```

---

## 15. Acceptance Criteria for Phase 7.2 Implementation

1. **Annotation Completeness:** 100% of the 40 benchmark scenarios possess valid, schema-validated ground-truth annotations covering coverages, limits, endorsements, and expected compliance findings.
2. **Multi-Document Execution:** The scenario runner processes multi-document packages and evaluates overall submission compliance deterministically.
3. **Zero Untracked Failures:** All provider exceptions (rate limits, timeouts, validation errors) are captured and categorized in the benchmark report without crashing the runner.
4. **False-Compliant Tracking:** The False-Compliant Rate is computed and surfaced as a standalone primary metric; any false-compliant scenario is flagged with an individual error diff.
5. **Baseline Preservation:** All 185 existing unit and integration tests continue to pass without regression.

---

## 16. Implementation File Scope

### Files that will be created or modified in Phase 7.2 (NOT modified now):
- `backend/evaluation/schemas/ground_truth.py` (Add `ScenarioGroundTruth`, `ExpectedFinding`)
- `backend/evaluation/schemas/reports.py` (Add `ScenarioBenchmarkReport`, `ComplianceMetricsStats`)
- `backend/evaluation/annotations/cohort_scenarios_v1.json` (New 40-scenario ground truth)
- `backend/evaluation/fixtures/synthetic_generator.py` (Add multi-document scenario generators)
- `backend/evaluation/runners/scenario_runner.py` (New multi-document CLI runner)
- `backend/evaluation/metrics/compliance_evaluator.py` (New compliance metrics calculator)

### Files actually created/modified during this Phase 7.1 planning phase:
- `docs/phase-7-1-benchmark-plan.md` (This planning document — created as explicitly authorized)
- **Zero application code, test code, or dependency files modified.**

---

## 17. Architecture & Review Sign-Off

This planning document establishes the complete engineering specifications for Phase 7.1. All technical conclusions have been verified against the current repository state. Phase 7.1 planning is complete.

**Implementation is halted.** No code changes, dataset modifications, or live API calls will be initiated without explicit authorization for Phase 7.2.
