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

## Current Phase: Structured Intelligence v0.5 (in progress)

- LLM **extracts structured data only** (conditions, financial inputs)
- **Python computes all financial math** — LLM must NEVER compute DTI or underwriting decisions
- Profiles active: `default`, `uw_conditions`
- Profile planned next: `income_analysis` (structured financial extraction + deterministic DTI)
- Next phase (v0.6): Deterministic underwriting decision engine (rule-based PASS/FAIL, LLM for explanation only)

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

**Tests (62 passing as of 2026-02-26):**
- `test_job_hardening.py` — Job workflow resilience
- `test_source_path_validation.py` — Source path validation
- `test_step12_uw_conditions.py` — UW conditions extraction (17 tests)
- `test_step12_postprocess_conditions.py` — Condition postprocessing/dedup (13 tests)
- `test_step12_version_blob.py` — Unified version.json audit trail (8 tests)
- `test_step13_chunk_index.py` — Chunk index loading (9 tests)

**Note:** `test_step13_chunk_index.py` mocks `qdrant_client` at import time — safe to run on Windows dev machine without full production deps.

---

## Recently Completed Work (as of 2026-02-26)

All TDD (red → green → regression). 62 tests passing.

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

1. **`income_analysis` profile** — Structured financial extraction: income sources, liabilities, borrower info. LLM extracts only; Python validates.
2. **Deterministic DTI engine** — Python computes DTI from extracted values; writes `dti.json`; LLM never touches the math.
3. **Underwriting decision simulation (v0.6)** — Rule-based PASS/FAIL/UNKNOWN; hardcoded thresholds; LLM used only for explanation layer.
4. **Audit trail hardening** — Reproducible runs, exportable JSON artifacts, version tagging (version.json already in place for all profiles).

---

## Running / Testing

```bash
# Activate venv (Windows PowerShell)
cd m:\mortgagedocai
.\venv\bin\Activate.ps1

# Syntax check all scripts
python -m py_compile scripts/step12_analyze.py
python -m py_compile scripts/step13_build_retrieval_pack.py

# Run full test suite (Windows dev machine, 62 tests)
python -m pytest scripts/test_step13_chunk_index.py \
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
