# Copy-paste to Claude: Implement income_analysis profile + deterministic DTI

**Instructions for you (the implementer):**  
Implement the Step12 `income_analysis` profile and deterministic DTI computation in the MortgageDocAI repo exactly as specified below. You will be given access to the repository. Follow the existing patterns (uw_conditions), respect the non-negotiables, and add the optional smoke-test block. When done, the existing regression smoke test (without RUN_INCOME_ANALYSIS) must still pass, and with RUN_INCOME_ANALYSIS=1 the new assertions and citation-integrity check must pass.

---

## PROJECT & AUTHORITY (brief)

- **Project:** MortgageDocAI — on-prem, deterministic mortgage document analysis pipeline. Step10 (intake) → Step11 (chunk/embed) → Step13 (retrieval pack) → Step12 (analyze via local Ollama). All processing is local; no cloud APIs.
- **Authority:** If anything below conflicts with `MortgageDocAI_CONTRACT.md`, the CONTRACT wins. Key rules: no renaming scripts/folders; folder contracts `nas_chunk/`, `nas_analyze/`, `outputs/`; run_id required; atomic publish via _staging then rename.
- **Canonical files you will edit:** `scripts/step12_analyze.py`, `scripts/run_regression_smoke.sh`. Do not change step10, step11, step13, or lib.py except where explicitly required.

---

## NON-NEGOTIABLES

1. **run_id determinism** — Do not introduce any code path that bypasses or ignores run_id.
2. **Citation integrity** — Every citation (chunk_id) in any LLM output must be validated against the retrieval pack’s `allowed_chunk_ids`. Drop any extracted item (income item, liability item) that has zero valid citations. Same rule as uw_conditions.
3. **Financial math in Python only** — The LLM must never compute DTI, sums, or annualization. Only extract structured line items (amount, frequency, payment); Python does all arithmetic.
4. **Regression smoke test** — The existing smoke test (Step13, default profile phi3/mistral, citation integrity, optional RUN_UW_CONDITIONS=1) must still pass after your changes. New code must not break it.
5. **Folder contract** — Outputs go under `nas_analyze/tenants/<tenant>/loans/<loan>/<run_id>/outputs/` and `outputs/profiles/income_analysis/`. No new top-level folders.

---

## WHAT TO IMPLEMENT

### 1. Step12: `income_analysis` profile

- **Profile name:** `income_analysis` (string equality check, e.g. `profile == "income_analysis"`).
- **Flow:** Same as `uw_conditions`: when this profile is selected, use a dedicated prompt that asks the LLM to extract **only** income line items and liability line items (with amounts and frequency), plus optional housing payment. Each item must include `citations` with `chunk_id` and `quote`. The prompt must state: do not compute totals, DTI, or annualize; only list what is stated in the evidence.
- **LLM output schema (enforce in prompt):**

```json
{
  "income_items": [
    {
      "description": "e.g. Base salary",
      "amount": number,
      "frequency": "weekly|biweekly|monthly|annual",
      "citations": [{"chunk_id": "string", "quote": "short quote"}]
    }
  ],
  "liability_items": [
    {
      "description": "e.g. Auto loan",
      "payment_monthly": number,
      "balance_optional": number,
      "citations": [{"chunk_id": "string", "quote": "short quote"}]
    }
  ],
  "housing_payment_monthly_optional": number,
  "confidence": number
}
```

- **Normalizer:** Implement `_normalize_income_analysis(llm_obj, allowed_chunk_ids)` that:
  - Takes the parsed LLM dict and the set of allowed chunk_ids from the retrieval pack.
  - Filters `income_items`: keep only items that have at least one citation with `chunk_id` in `allowed_chunk_ids`; strip invalid citations from each item.
  - Filters `liability_items` the same way.
  - Normalizes types (e.g. ensure amount/payment_monthly are numbers; frequency string). If frequency is missing or invalid, treat as "monthly".
  - Returns a dict with keys: `income_items`, `liability_items`, `housing_payment_monthly_optional` (float or None), `confidence`. Cap confidence (e.g. 0.3) if no valid income/liability items remain.
- **DTI computation (Python only):** Implement `_compute_dti(normalized_extraction)` that:
  - Converts each income item to monthly: weekly → amount * 52/12, biweekly → amount * 26/12, monthly → amount, annual → amount/12. Sum → `monthly_income_total`.
  - Sums `payment_monthly` from each liability item; adds `housing_payment_monthly_optional` if present → `monthly_debt_total`.
  - Back-end DTI = monthly_debt_total / monthly_income_total (if monthly_income_total > 0, else null or 0 with a note).
  - Front-end DTI = housing_payment_monthly_optional / monthly_income_total (if both present).
  - Returns a dict for `dti.json`: e.g. `monthly_income_total`, `monthly_debt_total`, `housing_payment_used`, `front_end_dti`, `back_end_dti`, optional `formula_note` or `inputs_snapshot` for audit.
- **Main loop changes:** In the Step12 main loop (where `is_uw = (profile == "uw_conditions")`):
  - Add `is_income = (profile == "income_analysis")`.
  - When `is_income`, use `_income_analysis_prompt(question, evidence_block)` instead of the default or uw_conditions prompt.
  - After parsing LLM response with `_parse_llm_json()`, if `is_income`: call `_normalize_income_analysis(llm_obj, allowed_chunk_ids)`, then `_compute_dti(normalized_result)`. Build `answer` and `citations` from the normalized result (e.g. synthesize answer text from income/liability summary + DTI result). Apply same citation-integrity rule: only citations whose chunk_id is in allowed_chunk_ids.
  - Write per-profile outputs under `profiles_dir / "income_analysis"`: `answer.md`, `answer.json`, `citations.jsonl`, `financial_extraction.json` (normalized LLM output after filtering), `dti.json` (output of _compute_dti).
- **Parsing:** Reuse `_parse_llm_json()` and `_repair_truncated_json()`. In `_repair_truncated_json()`, extend the condition that accepts repaired dicts to also accept objects that contain `"income_items"` (e.g. `("answer" in obj or "conditions" in obj or "income_items" in obj)`). Add any closers needed for truncated income_analysis JSON (nested income_items/liability_items with citations) to the `closers_sequences` list if necessary.

### 2. Output files (income_analysis profile)

- **outputs/profiles/income_analysis/answer.md** — Human-readable: question + summary of income/liabilities + DTI result.
- **outputs/profiles/income_analysis/answer.json** — Same shape as other profiles: answer, citations, confidence, question, profile, retrieval_pack, retrieval_pack_source.
- **outputs/profiles/income_analysis/citations.jsonl** — One JSON line per citation (chunk_id, quote).
- **outputs/profiles/income_analysis/financial_extraction.json** — Normalized extraction after citation filtering: income_items, liability_items, housing_payment_monthly_optional, confidence.
- **outputs/profiles/income_analysis/dti.json** — Python-only DTI output: monthly_income_total, monthly_debt_total, housing_payment_used, front_end_dti, back_end_dti, and any audit fields.

When income_analysis is the first profile (idx==0), also write the primary outputs (answer.md, answer.json, citations.jsonl) to `outputs/` as with other profiles. Do not remove existing stub behavior (e.g. conditions.json, extracted_fields.json) for other profiles.

### 3. Regression smoke test: RUN_INCOME_ANALYSIS=1

- Add env var `RUN_INCOME_ANALYSIS` (default `0`). Add `INCOME_QUERY` (default e.g. "Extract all income and liabilities for DTI calculation."), `INCOME_MAX_TOKENS`, `INCOME_EVIDENCE_MAX_CHARS` if you want them overridable (optional).
- Add a new section (after the uw_conditions block, before Summary): when `RUN_INCOME_ANALYSIS=1`:
  - Run Step12 with `--tenant-id`, `--loan-id`, `--run-id` same as rest of smoke test, `--query "$INCOME_QUERY"`, `--analysis-profile income_analysis`, `--ollama-url`, `--llm-model` (e.g. mistral), `--llm-max-tokens`, `--evidence-max-chars`, `--ollama-timeout`, and optionally `--save-llm-raw`.
  - **Assert:** `outputs/profiles/income_analysis/financial_extraction.json` exists and has valid structure (e.g. `income_items` and `liability_items` are arrays).
  - **Assert:** `outputs/profiles/income_analysis/dti.json` exists and contains `back_end_dti` (and optionally `front_end_dti`). If no income was extracted, DTI may be null/zero but file must exist.
  - **Integrity:** Run a small Python inline that loads the retrieval pack and financial_extraction.json; collect every chunk_id cited in income_items and liability_items citations; verify each is in the set of chunk_ids from the retrieval pack. If any cited chunk_id is not in the pack, fail with a clear message (same style as the uw_conditions integrity block).
- In the final Summary block, when RUN_INCOME_ANALYSIS=1, print a line like "income_analysis: financial_extraction + dti validated".

---

## CODE PATTERNS TO FOLLOW (from existing step12_analyze.py)

**Profile branch (conceptual):**
```text
is_uw = (profile == "uw_conditions")
is_income = (profile == "income_analysis")
...
if not evidence_block.strip():
    llm_obj = { ... }  # Not found
else:
    if is_uw:
        prompt = _uw_conditions_prompt(question, evidence_block)
    elif is_income:
        prompt = _income_analysis_prompt(question, evidence_block)
    else:
        prompt = _evidence_only_prompt(question, evidence_block)
    llm_raw = _ollama_generate(...)
    llm_obj = _parse_llm_json(llm_raw)
# ... citation integrity for answer/citations ...
if is_uw:
    uw_result = _normalize_uw_conditions(llm_obj, allowed_chunk_ids, chunk_meta=chunk_meta)
    confidence = uw_result["confidence"]
if is_income:
    income_result = _normalize_income_analysis(llm_obj, allowed_chunk_ids)
    dti_result = _compute_dti(income_result)
    confidence = income_result["confidence"]
# ... write prof_out files ...
if uw_result is not None:
    atomic_write_json(prof_out / "conditions.json", { ... })
if income_result is not None:  # or dti_result
    atomic_write_json(prof_out / "financial_extraction.json", { ... })
    atomic_write_json(prof_out / "dti.json", dti_result)
```

**uw_conditions prompt (pattern for your income_analysis prompt):**  
The prompt is a multi-line f-string: "You are MortgageDocAI. You must answer ONLY using the EVIDENCE provided. ... Rules: ... Extract only ... Every item must cite chunk_id from evidence. Do not use outside knowledge. Output must be VALID JSON ONLY." Then "Question:", "Evidence:", "Return JSON in this schema:" and the JSON schema. No math instructions in the prompt.

**Normalizer pattern (uw_conditions):**  
Iterate over list from LLM; for each item validate citations against allowed_chunk_ids; if item has zero valid citations, skip it; otherwise normalize enum/fields and append to filtered list. Return dict with list + confidence.

**Smoke test (RUN_UW_CONDITIONS):**  
See run_regression_smoke.sh: env RUN_UW_CONDITIONS=1, run Step12 with uw_conditions profile and UW_QUERY, then assert conditions.json exists, then run Python inline to validate conditions list and citation integrity (all cited chunk_ids in retrieval_pack). Mirror that for RUN_INCOME_ANALYSIS with financial_extraction.json and dti.json.

---

## DELIVERABLE CHECKLIST

- [ ] `_income_analysis_prompt(question, evidence_block)` added; prompt contains no DTI/math instructions.
- [ ] `_normalize_income_analysis(llm_obj, allowed_chunk_ids)` filters by citation and returns normalized extraction.
- [ ] `_compute_dti(normalized_extraction)` implements monthly conversion and DTI in Python only; returns dti.json-shaped dict.
- [ ] Main loop: income_analysis branch uses income prompt, normalizer, and _compute_dti; writes answer.md, answer.json, citations.jsonl, financial_extraction.json, dti.json under profiles/income_analysis.
- [ ] _repair_truncated_json accepts dicts with "income_items"; closers updated if needed.
- [ ] run_regression_smoke.sh: RUN_INCOME_ANALYSIS (default 0), new section when 1: run Step12 income_analysis, assert financial_extraction.json and dti.json, assert citation integrity for income_analysis.
- [ ] Existing smoke test (without RUN_INCOME_ANALYSIS and without RUN_UW_CONDITIONS) still passes.
- [ ] With RUN_INCOME_ANALYSIS=1, smoke test passes.

---

## FILES TO EDIT

- **scripts/step12_analyze.py** — Add prompt, normalizer, _compute_dti, main-loop branch, and profile-specific writes.
- **scripts/run_regression_smoke.sh** — Add RUN_INCOME_ANALYSIS (and optional INCOME_* vars), new block with Step12 run + assertions + integrity check, and summary line.

Do not change step10_intake.py, step11_process.py, step13_build_retrieval_pack.py, or lib.py unless you have a concrete reason (e.g. a shared constant). Preserve all existing behavior for default and uw_conditions profiles.

End of instructions.
