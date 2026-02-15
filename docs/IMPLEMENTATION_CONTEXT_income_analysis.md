# Implementation context: income_analysis profile + deterministic DTI

**Purpose:** Provide enough context for an implementer (e.g. Claude) to add the `income_analysis` Step12 profile and deterministic DTI computation. After implementation, another reviewer (e.g. Cursor/Claude) will verify the code and test outcomes.

**Authority:** This spec follows `MortgageDocAI_CONTRACT.md`, `.cursor/project_context.md`, and existing patterns in `scripts/step12_analyze.py` (especially `uw_conditions`). If anything here conflicts with the CONTRACT, CONTRACT wins.

---

## 1. Goal

- Add a Step12 **analysis profile** named `income_analysis`.
- **LLM:** Extract structured financial *inputs* only (income line items, liability line items, optional housing payment / loan amount). Every item must cite `chunk_id`s from the retrieval pack. **No math in the LLM** — no DTI, no sums, no annualization.
- **Python:** Consume the extracted inputs, normalize amounts (e.g. annualize income by frequency), sum to monthly totals, and compute **DTI deterministically**. Write results to `dti.json` and related artifacts.
- Preserve **citation integrity**: only chunk_ids present in the retrieval pack are allowed; drop any extracted item with zero valid citations; cap confidence if nothing valid remains.
- **Regression:** Add optional `RUN_INCOME_ANALYSIS=1` to `scripts/run_regression_smoke.sh` that runs the profile and validates outputs + citation integrity.

---

## 2. Non-negotiables (reminder)

- Do not break **run_id** determinism.
- Do not weaken **citation integrity** (filter all citations against retrieval pack `allowed_chunk_ids`).
- **Financial calculations must be implemented in Python only;** LLM must never compute DTI or any underwriting math.
- **Regression smoke test must still pass** for existing behavior (Step13, default profile phi3/mistral, citation integrity, optional uw_conditions). New code must not break those checks.
- Preserve folder contracts: outputs under `nas_analyze/tenants/<tenant>/loans/<loan>/<run_id>/outputs/` and `outputs/profiles/income_analysis/`.

---

## 3. Existing patterns to follow (step12_analyze.py)

- **Profile dispatch:** Same pattern as `uw_conditions`: when `profile == "uw_conditions"` we use `_uw_conditions_prompt()` and `_normalize_uw_conditions()`. Add `profile == "income_analysis"` with a dedicated prompt and normalizer.
- **Evidence block:** Reuse `_build_evidence_block(retrieved_chunks, max_chars)`; use same `allowed_chunk_ids` from retrieval pack.
- **Citation filtering:** For any LLM-produced list (income items, liability items), each item must have at least one citation whose `chunk_id` is in `allowed_chunk_ids`; otherwise drop the item. Same as uw_conditions “drop conditions with zero valid citations.”
- **Output layout:** Per-profile artifacts go under `profiles_dir / profile` (e.g. `outputs/profiles/income_analysis/`). Write `answer.md`, `answer.json`, `citations.jsonl`, and profile-specific JSON(s).
- **chunk_meta:** Build from retrieval pack (chunk_id → document_id, file_relpath, page_start, page_end) for source metadata on extracted items if desired (optional for v1).
- **Parsing:** Reuse `_parse_llm_json()`, `_repair_truncated_json()`, and existing citation-integrity logic. Extend repair closers if the new JSON schema has different nesting (so truncated income_analysis JSON can be salvaged).

---

## 4. LLM schema (income_analysis) — extraction only, no math

The LLM must return **valid JSON only** (no markdown). Suggested schema:

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

- **Rules for the prompt:** Extract only what is stated in the evidence. Use exact chunk_id values from the evidence blocks. Do not compute totals, DTI, or annualize; only list line items with amounts and frequency. If a value is missing or unclear, omit the field or use null; do not infer.

---

## 5. Python responsibilities (deterministic)

- **Normalize income to monthly:** From each `income_items` entry: if `frequency` is weekly → monthly = amount * 52/12; biweekly → amount * 26/12; monthly → amount; annual → amount/12. Sum → `monthly_income_total`. If frequency is missing or invalid, treat as monthly or drop the item (define a clear rule and document it).
- **Liabilities:** Sum `payment_monthly` from each liability item (and any other monthly debt from evidence). Add `housing_payment_monthly_optional` if present → `monthly_debt_total`.
- **DTI:**
  - Back-end DTI = `monthly_debt_total / monthly_income_total` (if monthly_income_total > 0, else null or 0 and note in output).
  - Front-end DTI = `housing_payment_monthly_optional / monthly_income_total` (if housing and income present).
- **Output:** Write a **dti.json** with only Python-derived numbers, e.g.:
  - `monthly_income_total`, `monthly_debt_total`, `housing_payment_used`
  - `front_end_dti`, `back_end_dti`
  - Optional: `formula_note` or `inputs_snapshot` for audit (e.g. list of normalized income amounts used).

All arithmetic in one place (e.g. a small function `_compute_dti(extracted)` that returns a dict); no floating-point in the LLM path.

---

## 6. Output files and paths

- **Under `outputs/profiles/income_analysis/`:**
  - `answer.md` — human-readable summary (e.g. question + summary of income/liabilities + DTI result).
  - `answer.json` — same structure as other profiles (answer, citations, confidence, question, profile, retrieval_pack, retrieval_pack_source).
  - `citations.jsonl` — one JSON line per citation (chunk_id, quote).
  - **financial_extraction.json** (or **income_analysis.json**) — normalized LLM output after citation filtering: income_items, liability_items, housing_payment_monthly_optional, confidence. Optionally include source/document metadata per item.
  - **dti.json** — Python-only: monthly_income_total, monthly_debt_total, front_end_dti, back_end_dti, and any audit fields (formula_note, inputs_snapshot).

- **Primary outputs (when income_analysis is the first profile, idx==0):** Same as today: answer.md, answer.json, citations.jsonl in `outputs/`. The stub `extracted_fields.json` can remain a stub or be updated; do not remove existing behavior for other profiles.

---

## 7. Regression smoke test (RUN_INCOME_ANALYSIS=1)

- **Env var:** `RUN_INCOME_ANALYSIS` (default `0`). When `1`:
  - Run Step12 with `--analysis-profile income_analysis` and a financial query (e.g. “Extract all income and liabilities for DTI calculation.”). Use same RUN_ID, tenant, loan as the rest of the smoke test.
  - **Assert:** `outputs/profiles/income_analysis/financial_extraction.json` (or chosen name) exists and has valid structure (e.g. income_items array, liability_items array).
  - **Assert:** `outputs/profiles/income_analysis/dti.json` exists and contains `back_end_dti` (and optionally `front_end_dti`). If no income extracted, DTI can be null/zero but file must exist.
  - **Integrity:** Every `chunk_id` cited in `financial_extraction.json` (in any income or liability item citations) must be in the retrieval pack. Same style as the uw_conditions integrity block (Python one-liner loading RP and extraction JSON, collect cited chunk_ids, check ⊆ rp_ids).

- Existing sections (Step13, phi3, mistral, default citation integrity, optional uw_conditions) must remain and still pass.

---

## 8. Suggested order of implementation

1. Add `_income_analysis_prompt(question, evidence_block)` and LLM schema to the prompt text.
2. Add `_normalize_income_analysis(llm_obj, allowed_chunk_ids)` — filter income_items and liability_items to only those with at least one valid citation; normalize frequency/amount types; return dict suitable for Python DTI and for writing financial_extraction.json.
3. Add `_compute_dti(normalized_extraction)` — pure function: inputs → monthly totals → front_end_dti, back_end_dti; return dict for dti.json.
4. In main loop: when `profile == "income_analysis"`, call the income prompt, parse LLM response, run normalizer, run _compute_dti, write answer.md/answer.json/citations.jsonl + financial_extraction.json + dti.json under `profiles_dir / "income_analysis"`.
5. Extend `_repair_truncated_json` closers if the new schema needs it (nested income_items/liability_items with citations).
6. Add RUN_INCOME_ANALYSIS block to `run_regression_smoke.sh` with assertions and citation-integrity check.
7. Run full smoke test without RUN_INCOME_ANALYSIS first (must pass); then with RUN_INCOME_ANALYSIS=1 and fix until it passes.

---

## 9. Verification checklist (for reviewer after implementation)

- [ ] No financial math (no DTI, no annualization) in the LLM prompt or in any string sent to Ollama.
- [ ] DTI and monthly totals computed only in Python.
- [ ] Citation integrity: all cited chunk_ids in income_analysis outputs are in the retrieval pack; items with no valid citations are dropped.
- [ ] run_id determinism unchanged (no new code path that bypasses run_id).
- [ ] Existing regression smoke test (without RUN_INCOME_ANALYSIS) still passes.
- [ ] With RUN_INCOME_ANALYSIS=1, smoke test passes: financial_extraction.json and dti.json exist; income_analysis citation integrity check passes.
- [ ] Outputs written under correct paths (profiles/income_analysis/, no new top-level folders that violate contract).

---

## 10. References

- **Contract:** `MortgageDocAI_CONTRACT.md`
- **Project context:** `.cursor/project_context.md`
- **Step12 implementation:** `scripts/step12_analyze.py` (uw_conditions: prompt, _normalize_uw_conditions, profile branch, conditions.json write).
- **Smoke test:** `scripts/run_regression_smoke.sh` (RUN_UW_CONDITIONS block and citation integrity as template for RUN_INCOME_ANALYSIS).

End of implementation context.
