# MortgageDocAI — Claude Context

**Purpose:** Local-only AI pipeline for mortgage document analysis. Ingests scanned loan docs, chunks + embeds them, retrieves evidence, and answers structured questions via local Ollama LLM. No cloud APIs. Ever.

---

## Authority Order (when in doubt, these win)

1. `MortgageDocAI_CONTRACT.md` — FINAL (wins over all code and docs)
2. `ARCHITECTURE_AUTHORITY.md` — Roles & precedence
3. `PROJECT_STATUS.md` — Detailed step-by-step history
4. `.cursor/project_context.md` — Durable AI phase context

**Rule:** If code and CONTRACT disagree, CONTRACT wins. Ask before refactoring. Prefer fail-loud over silent degradation.

---

## Current Phase: Structured Intelligence v0.5 + Productization

- LLM **extracts structured data only** (conditions, financial inputs)
- **Python computes all financial math** — LLM must NEVER compute DTI or underwriting decisions
- Profiles active: `default`, `uw_conditions`, `income_analysis`, `uw_decision`
- **Form Fill** feature live: pre-fills Excel worksheets from pipeline data (3 templates, extensible)
- Next phase (v0.6): Deterministic underwriting decision engine hardening, more form templates

---

## Non-Negotiables (never break these)

- No cloud APIs
- Do NOT rename scripts or folders
- Preserve folder contracts: `nas_chunk/`, `nas_analyze/`, `outputs/`
- Maintain `run_id` determinism — never break it
- Preserve citation integrity filtering — never weaken it
- **Regression smoke test must always pass:** `scripts/run_regression_smoke.sh`
- Financial calculations: deterministic Python only; LLM never computes DTI
- No background daemons; no schema drift without updating CONTRACT

---

## Architecture (Option B — Final)

```
Source (RO):  /mnt/source_loans/5-Borrowers TBD/       ← Synology
TrueNAS (RW): /mnt/nas_apps/nas_ingest                  ← Step10 writes here
              /mnt/nas_apps/nas_chunk                   ← Step11 writes here
              /mnt/nas_apps/nas_analyze                 ← Step12/13 write here
Qdrant:       localhost:6333                            ← Local to AI server
Ollama:       localhost:11434                           ← Local LLM inference
```

**TrueNAS = authoritative system of record. AI server = on-demand processing only.**

---

## Pipeline

| Step | Script | What it does |
|------|--------|-------------|
| Step 10 | `step10_intake.py` | Copy docs from source → `nas_ingest/tenants/<t>/loans/<l>/`; writes `intake_manifest.json` |
| Step 11 | `step11_process.py` | Extract+OCR PDFs, DOCX, XLSX → chunk → embed (E5-large-v2) → upsert Qdrant; writes `chunks/<doc_id>/chunks.jsonl`, `chunk_map.json`, `_meta/processing_run.json` |
| Step 13 | `step13_build_retrieval_pack.py` | Embed query → Qdrant search → reconstruct text from `chunks.jsonl` → write `retrieve/<run_id>/retrieval_pack.json` |
| Step 12 | `step12_analyze.py` | Load retrieval pack → build evidence prompt → call Ollama → write `outputs/profiles/<profile>/answer.json` + `conditions.json` + `version.json` |

**Run order:** Step10 → Step11 → Step13 → Step12 (Step12 auto-triggers Step13 if pack missing)

**Orchestrators:**
- `run_loan_pipeline.py` — CLI single-run orchestrator
- `run_loan_job.py` — Production subprocess entry point (emits `PHASE:` markers)
- `job_worker.py` — Durable background worker (polls disk, claims PENDING jobs)
- `loan_api.py` — Local-only FastAPI service (no external access)

---

## Key Output Paths

```
nas_chunk/tenants/<tenant>/loans/<loan>/<run_id>/
  chunks/<document_id>/chunks.jsonl          ← authoritative chunk text
  chunks/<document_id>/chunk_map.json
  _meta/processing_run.json

nas_analyze/tenants/<tenant>/loans/<loan>/retrieve/<run_id>/
  retrieval_pack.json

nas_analyze/tenants/<tenant>/loans/<loan>/<run_id>/
  outputs/profiles/<profile>/
    answer.json
    conditions.json          ← uw_conditions profile
    version.json             ← audit trail (all profiles)
  outputs/formfill/
    {template_id}.xlsx       ← pre-filled Excel forms
  outputs/_meta/
    analysis_run.json
    version.json
```

---

## Qdrant

- Collection: `{tenant_id}_e5largev2_1024_cosine_v1`
- Dimensions: 1024, Distance: cosine
- Point IDs: deterministic UUIDv5 from `chunk_id`
- Payload includes: `tenant_id`, `loan_id`, `run_id`, `chunk_id`, `document_id`, `file_relpath`
- **`run_id` in payload** = cross-run isolation (never mix vectors from different runs)

---

## Chunk Identity

```
document_id  = SHA256(file bytes)
chunk_id     = SHA256(normalized_chunk_text)   — set-based, no ML
```

---

## PHASE Markers (contract-stable, do not rename)

`run_loan_job.py` MUST emit to stdout:
```
PHASE:INTAKE               ← if --skip-intake not set
PHASE:PROCESS              ← if --skip-process not set
PHASE:STEP13_GENERAL       ← always when retrieval pack built
PHASE:STEP12_UW_CONDITIONS ← always (uw_conditions profile)
PHASE:STEP13_INCOME        ← if income retrieval pack built
PHASE:STEP12_INCOME_ANALYSIS ← if income_analysis profile runs
PHASE:STEP12_UW_DECISION   ← if uw_decision profile runs
PHASE:DONE                 ← on success
PHASE:FAIL                 ← on failure
```
Format: `PHASE:<NAME> YYYY-MM-DDTHH:MM:SSZ` — Web UI parses these for progress display.

---

## Scripts Directory Map

**Core pipeline:**
- `lib.py` — Shared constants, helpers, `ContractError`, `atomic_write_json`, `normalize_chunk_text`, mount paths
- `step10_intake.py`, `step11_process.py`, `step12_analyze.py`, `step13_build_retrieval_pack.py`

**Job service (`loan_service/`):**
- `domain.py` — Pure data models (`JobStatus`, `JobRecord`, `JobRequest`, `JobResult`)
- `ports.py` — Protocol interfaces (`JobStore`, `LoanLock`, `PipelineRunner`)
- `service.py` — `JobService` (enqueue, get, list; atomic writes)
- `adapters_disk.py` — `DiskJobStore`, `JobKeyIndexImpl`, `LoanLockImpl`
- `adapters_subprocess.py` — `SubprocessRunner`

**Form Fill:**
- `formfill.py` — Form registry (`FORM_TEMPLATES`), `FieldMapping`/`FormTemplate` dataclasses, `fill_form()` filler logic (openpyxl)

**Tests (81 passing as of 2026-03-05):**
- `test_formfill.py` — Form registry, JSON path resolution, filler logic (19 tests)
- `test_job_hardening.py` — Job workflow resilience
- `test_source_path_validation.py` — Source path validation
- `test_step12_uw_conditions.py` — UW conditions extraction (17 tests)
- `test_step12_postprocess_conditions.py` — Condition postprocessing/dedup (13 tests)
- `test_step12_version_blob.py` — Unified version.json audit trail (8 tests)
- `test_step13_chunk_index.py` — Chunk index loading (9 tests)

**Note:** `test_step13_chunk_index.py` mocks `qdrant_client` at import time — safe to run on Windows dev machine without full production deps.

---

## Recently Completed Work (as of 2026-03-06)

All TDD (red → green → regression). 81 tests passing (+ 10 cleanup_orphans tests).

### Punch List #9: Database Housekeeping UI (2026-03-06)
| Component | What was done |
|-----------|--------------|
| `loan_api.py` | Added `GET /tenants/{t}/housekeeping/orphans` — scans for orphaned loans (source removed, NAS/Qdrant data remains), returns sizes + Qdrant vector counts |
| `loan_api.py` | Added `POST /tenants/{t}/housekeeping/orphans/purge` — deletes selected orphans; re-verifies orphan status, skips active jobs, caps 20/request |
| `loan_api.py` | Imports from `cleanup_orphans.py`: `find_orphaned_loans`, `delete_orphan_nas`, `delete_orphan_qdrant`, `_dir_size_bytes` |
| `webui/index.html` | Added "Housekeeping" button in sidebar + `housekeeping-panel` section with summary, checkbox table, purge/select-all buttons |
| `webui/app.js` | Added `initHousekeeping()` IIFE: scan → render table → select all → purge with confirm dialog → inline results → auto-rescan |

### Punch List #2, #4, #5, #6: View Artifacts Bug Fix + Dashboard Audit (2026-03-06)
| Component | What was done |
|-----------|--------------|
| `webui/app.js` | Fixed double-base-URL bug in View Artifacts: `data-url` stored full URL (`base + "/tenants/..."`), but `apiFetch()` also prepends base → 404 on every artifact click. Changed to store path only (`"/tenants/..."`) |
| `webui/app.js` | Added `r.ok` check in artifact click handler — HTTP errors now display cleanly instead of raw 404 body |
| `webui/index.html` | Added cache-buster query string (`?v=20260306a`) to `app.js` script tag |
| `punch_list.md` | Marked #2 (Summary dashboard), #4 (Income & DTI panel), #5 (Decision explanation), #6 (Markdown rendering) as DONE — all already implemented |

### Punch List #3: Conditions Checklist View (2026-03-05)
| Component | What was done |
|-----------|--------------|
| `loan_api.py` | Added `conditions.json` to `PROFILE_FILE_NAMES` — API was returning 404 |
| `run_loan_job.py` | Wired `uw_conditions` profile into pipeline — was never called in production runs |
| `run_loan_job.py` | Added `UW_CONDITIONS_QUERY`, `STEP12_UW_CONDITIONS` phase, `conditions_json` to `_output_paths` |
| `webui/app.js` | Added `STEP12_UW_CONDITIONS` to stepper labels and order |
| Pipeline order | Step13 general → **Step12 uw_conditions** → Step13 income → Step12 income_analysis → Step12 uw_decision |

### Form Fill Feature (2026-03-04)
| Component | What was built |
|-----------|---------------|
| `scripts/formfill.py` | `FormTemplate`/`FieldMapping` dataclasses, `FORM_TEMPLATES` registry (3 templates), `fill_form()` filler with openpyxl (preserves formulas), `_resolve_json_path()`, `_load_source_data()`, audit dict return |
| `scripts/test_formfill.py` | 19 tests: registry validation, JSON path resolution, source data loading, fill_form (audit, dir creation, formula preservation, invalid template, missing values, numeric types) |
| `webui/forms/*.xlsx` | 3 Excel templates: `income_calc_w2.xlsx`, `fha_max_mortgage_calc.xlsx`, `va_irrrl_recoupment_calc.xlsx` |
| `loan_api.py` | `GET /formfill/templates` (list by category), `POST .../formfill/{template_id}` (generate + download), `.xlsx` media type |
| `webui/` | Dropdown + Generate button in `main-actions`; `initFormFill` IIFE (static fallback + API refresh, blob download, inline feedback) |
| Output path | `nas_analyze/.../outputs/formfill/{template_id}.xlsx` |

### Web UI: Stall Detection Fix (2026-03-04)
| Bug | Fix |
|-----|-----|
| Stall detection stopped polling during long Step11 | Now shows informational warning but keeps polling; stepper updates naturally when job finishes |

### Web UI: Punch List #8, #11, #15 (2026-03-03)
| Item | What was done |
|------|--------------|
| #8 inline feedback | Replaced 4 `alert()` calls with `showInlineMsg()`/`clearInlineMsg()` helpers; auto-clear 6s; new `<p id="inline-msg">` element |
| #11 button disable | View Artifacts + Chat Send disable during async ops; `.btn-secondary:disabled` + `.chat-send-row button:disabled` CSS |
| #15 timestamps | `formatTimestamp()` parses run_id + ISO formats → locale string; applied at 6 locations |

### Step12: `uw_conditions` profile hardening
| Commit area | What was built |
|-------------|---------------|
| `_dedup_conditions` | Union-Find dedup with token Jaccard (threshold 0.92); `_make_dedupe_key` strips boilerplate; 17 tests |
| `_postprocess_conditions` | v2 replace: fixed `_CATEGORY_ORDER`/`_TIMING_ORDER` sort; `source.documents` union; debug logging; 13 tests |
| `_UW_DEDUPE_BOILERPLATE` | Extended with "obtain", "verify", "furnish" |

### Step12: Unified version.json audit trail
| What | Detail |
|------|--------|
| `_SCHEMA_VERSIONS` constant | Dict of profile → schema version string |
| `_build_version_blob()` | Unified audit blob: git commit, dirty flag, run options, retrieval pack provenance, schemas |
| All profiles now get `version.json` | uw_conditions, income_analysis, default, uw_decision — previously only uw_decision had it |
| `offline_embeddings` excluded | It's a step13-only arg; explicitly NOT in step12 version snapshot |

### Step13: `_load_chunk_text_index` strict-mode fix
| Bug | Fix |
|-----|-----|
| `iterdir()+is_dir()` unreliable on SMB/NAS | Replaced with `glob("*/chunks.jsonl")` |
| Duplicate chunk_id kept last (overwrote) | Changed to keep FIRST occurrence |
| `--strict` was registered but never passed | Wired `strict=args.strict` to call site |
| No debug visibility | Added: discovered file count, per-file add/dupe stats, total indexed |
| No self-test | Added `_self_test()` + `--self-test` CLI hook (uses tempfile, no prod deps) |

---

## What's Next (Priority Order)

1. **More form templates** — Add remaining 7 worksheets to `FORM_TEMPLATES` registry as extraction profiles improve.
2. **`income_analysis` profile improvements** — Richer structured financial extraction (borrower names, employer, pay frequency).
3. **Deterministic DTI engine hardening** — Edge cases, co-borrower logic, program-specific thresholds.
4. **Underwriting decision simulation (v0.6)** — Rule-based PASS/FAIL/UNKNOWN; hardcoded thresholds; LLM used only for explanation layer.
5. **Audit trail hardening** — Reproducible runs, exportable JSON artifacts, version tagging (version.json already in place for all profiles).

---

## Running / Testing

```bash
# Activate venv (Windows PowerShell)
cd m:\mortgagedocai
.\venv\bin\Activate.ps1

# Syntax check all scripts
python -m py_compile scripts/step12_analyze.py
python -m py_compile scripts/step13_build_retrieval_pack.py

# Run full test suite (81 tests)
python -m pytest scripts/test_formfill.py \
                 scripts/test_step13_chunk_index.py \
                 scripts/test_step12_version_blob.py \
                 scripts/test_step12_uw_conditions.py \
                 scripts/test_step12_postprocess_conditions.py \
                 scripts/test_job_hardening.py \
                 scripts/test_source_path_validation.py -q

# Step13 self-test (Linux AI server, requires qdrant_client)
python scripts/step13_build_retrieval_pack.py --self-test

# Regression smoke test (Linux AI server)
bash scripts/run_regression_smoke.sh

# Full pipeline run (Linux AI server)
python3 scripts/run_loan_pipeline.py \
  --tenant-id peak --loan-id 16271681 \
  --source-path "/mnt/source_loans/5-Borrowers TBD/16271681" \
  --query "List all underwriting conditions" \
  --llm-model phi3 --ollama-url http://localhost:11434
```

---

## Development Workflow

This project uses strict TDD. Always follow this pattern:

1. **Plan first** (`writing-plans` skill) — research, audit hallucinations, pre-flight table
2. **Red** — create test skeleton, write all tests failing
3. **Green** — implement until all tests pass
4. **Regression** — run full suite, confirm no regressions
5. **Commit each phase** with semantic messages: `test(area): ...`, `fix(area): ...`, `feat(area): ...`, `chore(area): final regression`

**ChatGPT is the System Architect** (spec + requirements). Claude is the Implementation Assistant. When ChatGPT specs conflict with the codebase, verify against CONTRACT.md and flag hallucinations before implementing.

**Common ChatGPT hallucinations to watch for:**
- `offline_embeddings` is a step13 arg only (NOT step12)
- Do not drop existing boilerplate entries — extend additively only
- Do not rename output paths or schema fields
