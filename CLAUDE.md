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
- **Form Fill** feature live: pre-fills Excel worksheets from pipeline data (8 templates, 44 field mappings)
- Next phase (v0.6): Deterministic underwriting decision engine hardening, extraction accuracy tuning

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

**Tests (129 passing as of 2026-03-23: 119 main + 10 cleanup_orphans):**
- `test_formfill.py` — Form registry, JSON path resolution, filler logic (23 tests)
- `test_job_hardening.py` — Job workflow resilience (10 tests)
- `test_source_path_validation.py` — Source path validation (5 tests)
- `test_step12_uw_conditions.py` — UW conditions extraction (17 tests)
- `test_step12_postprocess_conditions.py` — Condition postprocessing/dedup (13 tests)
- `test_step12_version_blob.py` — Unified version.json audit trail (8 tests)
- `test_step12_income_analysis.py` — Borrowers, frequencies, DTI, UW decision (21 tests)
- `test_step13_chunk_index.py` — Chunk index loading (9 tests)
- `test_cleanup_orphans.py` — Orphaned loan detection and cleanup (10 tests)
- `test_run_history.py` — Run history summaries, comparison diffs, enriched run list (12 tests)

**Note:** `test_step13_chunk_index.py` mocks `qdrant_client` at import time — safe to run on Windows dev machine without full production deps.

---

## Recently Completed Work (as of 2026-03-23)

All TDD (red → green → regression). 133 tests passing (123 main + 10 cleanup_orphans).

### Web UI Nice-to-Have Items #16–#23 (2026-03-23)
| Item | What was built |
|------|---------------|
| #16 Run history & comparison | Enhanced `GET /runs` with per-run metadata; `GET /runs/{run_id}/summary` and `GET /runs/compare` endpoints; run selector dropdown, history table with comparison checkboxes, side-by-side comparison panel with change deltas. 12 new tests in `test_run_history.py` |
| #17 Batch processing | `POST /tenants/{t}/batch/process` accepts up to 50 loans, validates source paths, enqueues via JobService; checkboxes on loan list, batch action bar (Process Selected, Select Needing Processing, Clear) |
| #18 Search and filter loans | Client-side search input (loan ID / folder name), filter buttons (All / Needs Processing / Done), sort dropdown (ID asc/desc, newest/oldest) |
| #19 Export / report generation | `GET /runs/{run_id}/report` renders printable HTML via Jinja2 template (decision, DTI, income, conditions, inventory); print-optimized CSS; Export Report button opens in new tab |
| #20 Job queue depth indicator | Queue badge in header polls `GET /jobs` every 15s; shows PENDING/RUNNING/CLAIMED count; auto-hides when empty |
| #21 Mobile responsiveness | `@media (max-width: 700px)` styles: sidebar stacks above main, 44px touch targets, chat/actions stack vertically, summary/comparison single-column |
| #22 Keyboard navigation | `:focus-visible` outlines; loan list items focusable (tabindex=0); Arrow Up/Down, Enter to select, Space for batch checkbox |
| #23 Light mode toggle | `body.light` CSS variable overrides; toggle button in header (sun/moon); localStorage persistence |

### System-Wide Code Audit — Round 1 + Round 2 (2026-03-13)
| Severity | Count | Key Fixes |
|----------|-------|-----------|
| Critical (5) | `apiFetch→apiJson` in webui, narrow exception catch in step11 `_ensure_collection`, crash-safe rename-aside in step11+step12, PHASE:FAIL + release claim on worker timeout |
| High (9) | Path traversal middleware, LoanLock ownership verification, deferred Qdrant upserts, `find_active_jobs` per-loan scan, `keep_vba=True` for .xlsm, infinite loop guard in chunker, source_path validation |
| Medium (15) | Atomic writes (`tempfile.mkstemp`), `shutil.copy2` in step10, hash source not dest, subprocess timeouts on sync query endpoint, `sys.executable` instead of `python3`, null byte rejection in URLs, NAS rmtree error handling, `encodeURIComponent` on housekeeping calls, form fill error handling |
| Low (11) | `datetime.now(timezone.utc)`, CSP meta tag, `escapeHtml` for loan_ids, `javascript:` URL stripping, CSS fixes (`--border` var, `showHkMsg` class names), `wb.close()`, `STDOUT_TRUNCATE` constant, test hygiene (tautological assert, temp dir cleanup) |

### Income Analysis + DTI + UW Decision Hardening (2026-03-10)
| Component | What was done |
|-----------|--------------|
| `step12_analyze.py` | LLM prompt now requests `borrowers` array (name, role, employer, employment_type) and `borrower_name`/`employer` on income items |
| `step12_analyze.py` | `_normalize_income_analysis()` extracts + normalizes borrowers; income items carry `borrower_name` and `employer` (null if absent) |
| `step12_analyze.py` | `biweekly` and `weekly` added to `_INCOME_FREQUENCIES_CANONICAL` with alias normalization |
| `step12_analyze.py` | `_compute_dti()` converts biweekly (×26÷12) and weekly (×52÷12) to monthly equivalents |
| `step12_analyze.py` | `_PROGRAM_THRESHOLDS` dict: Conventional (28/45%), FHA (31/43%), VA (none/41%), USDA (29/41%) |
| `step12_analyze.py` | `_build_uw_decision()` enforces front-end DTI when `max_front_end_dti` is set; multiple reasons in output |
| `step12_analyze.py` | Schema versions bumped: `income_analysis` v1→v2, `uw_decision` v0.7→v0.8, decision version `v0.8-policy` |
| `test_step12_income_analysis.py` | 21 new tests: borrower extraction, role normalization, income linkage, biweekly/weekly conversion, program thresholds, front-end DTI enforcement |

### Punch List #12, #13, #14: Empty States, Profile Availability, Copy to Clipboard (2026-03-10)
| Component | What was done |
|-----------|--------------|
| `webui/app.js` | Artifacts panel: "No artifacts found for this run." when empty |
| `webui/index.html` | Chat: placeholder text "Select a loan and ask a question to get started." removed on first message |
| `webui/app.js` | `updateChatProfiles()` / `refreshChatProfileAvailability()`: fetches artifact index, disables profiles without outputs ("not run" label) |
| `webui/app.js` | Profile dropdown refreshed on loan select + job completion; previous selection preserved |
| `webui/app.js` | Copy button on artifact preview: copies JSON or markdown content to clipboard, "Copied!" feedback |
| `webui/index.html` | Added `#artifact-copy-btn` button next to Preview heading |
| `webui/styles.css` | Added `.btn-small` CSS class for compact inline buttons |

### Punch List #9 (per-step timing) + #10 (artifact metadata) (2026-03-08)
| Component | What was done |
|-----------|--------------|
| `webui/app.js` | `setJobFields()` now computes elapsed time (finished − started, or wall-clock if running) and displays in `#progress-elapsed` |
| `webui/app.js` | `renderStepper()` now diffs consecutive PHASE timestamps to show per-step duration inline, e.g. "Process (45.2s)" |
| `webui/app.js` | Artifact file list now shows `size_bytes` (via `formatBytes`) and `mtime_utc` (via `formatTimestamp`) next to each file link |
| `webui/app.js` | `formatDuration()` utility (already existed) now wired to elapsed + stepper |
| `webui/index.html` | Added `#progress-elapsed` span in progress times line |
| `webui/styles.css` | Added `.artifact-meta` spacing rule |
| `punch_list.md` | Marked #7 as DONE |

### Punch List #9 (housekeeping UI): Database Housekeeping UI (2026-03-06)
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
| `scripts/formfill.py` | `FormTemplate`/`FieldMapping` dataclasses, `FORM_TEMPLATES` registry (8 templates, 44 mappings), `fill_form()` filler with openpyxl (preserves formulas, merged cell protection), `_resolve_json_path()`, `_load_source_data()`, audit dict return |
| `scripts/test_formfill.py` | 23 tests: registry validation, JSON path resolution, source data loading, fill_form (audit, dir creation, formula preservation, invalid template, missing values, numeric types, borrower fill, multi-sheet fill, PITIA fill, 3-sheet FHA fill) |
| `webui/forms/*.xlsx` | 8 Excel templates: 3 `.xlsx` (income_calc_w2, fha_max_mortgage_calc, va_irrrl_recoupment_calc), 2 `.xlsx` (FHA Max Mtg Initial, VA-IRRRL Worksheet), 3 `.xlsm` (VA Max Mortgage, Income Calc UWM, Bank Stmt Loan Calc) |
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

1. ~~**More form templates**~~ — DONE (2026-04-02). All 8 templates registered with 44 field mappings. 2 legacy `.xls` files remain (need conversion or `xlrd`).
2. **Server migration** — Punch list #24 (bootstrap on new GPU server).
3. **Extraction accuracy tuning** — Punch list #25 (batch-process diverse loans, tune prompts post-GPU migration).

**Completed (2026-03-23):**
- ~~Web UI nice-to-have items~~ — All 8 items (#16–#23) complete: run history, batch, search, export, queue depth, mobile, keyboard, light mode
- ~~`income_analysis` improvements~~ — Borrowers, employer, biweekly/weekly frequencies (v2 schema)
- ~~DTI engine hardening~~ — Biweekly/weekly conversion, `_PROGRAM_THRESHOLDS` (FHA/VA/USDA/Conventional)
- ~~UW decision v0.8~~ — Front-end DTI enforcement, program-specific thresholds, multiple reasons
- ~~Audit trail~~ — Already complete (version.json on all profiles, 8 tests)

---

## Running / Testing

```bash
# Activate venv (Windows PowerShell)
cd m:\mortgagedocai
.\venv\bin\Activate.ps1

# Syntax check all scripts
python -m py_compile scripts/step12_analyze.py
python -m py_compile scripts/step13_build_retrieval_pack.py

# Run full test suite (133 tests)
python -m pytest scripts/test_formfill.py \
                 scripts/test_step13_chunk_index.py \
                 scripts/test_step12_version_blob.py \
                 scripts/test_step12_uw_conditions.py \
                 scripts/test_step12_postprocess_conditions.py \
                 scripts/test_job_hardening.py \
                 scripts/test_source_path_validation.py \
                 scripts/test_step12_income_analysis.py \
                 scripts/test_cleanup_orphans.py \
                 scripts/test_run_history.py -q

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
