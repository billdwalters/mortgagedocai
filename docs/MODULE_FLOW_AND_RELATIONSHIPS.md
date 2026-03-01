# MortgageDocAI — Module Flow and Relationships

This document describes what each major module does, the order in which they are called, and how they relate to each other. Use it to understand the end-to-end process and data flow.

---

## 1. Overview

MortgageDocAI is a **local-only** pipeline that:

1. **Ingests** loan documents from a read-only source folder.
2. **Processes** them (extract text, chunk, embed) and stores chunks + vectors in Qdrant.
3. **Builds retrieval packs** by querying Qdrant and reconstructing chunk text.
4. **Analyzes** with an LLM (Ollama) using that evidence to produce answers, income analysis, and underwriting decisions.

The **Web UI** and **Loan API** let you run the pipeline (“Process Loan”) and ask questions (“Ask a question”) against a chosen run.

---

## 2. Shared Foundation: `lib.py`

**Path:** `scripts/lib.py`

**Role:** Central contract and utilities used by almost every script. Defines:

- **Paths:** `SOURCE_MOUNT`, `NAS_INGEST`, `NAS_CHUNK`, `NAS_ANALYZE` (and related).
- **Preflight:** `preflight_mount_contract()` — checks that source and NAS mounts exist and are correct (e.g. source read-only). Can skip source check for query-only runs.
- **Validation:** `validate_source_path()` — ensures a path is under the source mount.
- **Chunking/Qdrant:** `normalize_chunk_text()`, `chunk_id()`, `chunk_text_hash()`, `qdrant_collection_name()`, `build_run_context()`.
- **Helpers:** `ensure_dir()`, `atomic_write_json()`, `sha256_file()`, `utc_run_id()`, etc.

**Used by:** Step10, Step11, Step12, Step13, run_loan_job.py, run_loan_pipeline.py.

---

## 3. Pipeline Orchestrator: `run_loan_job.py`

**Path:** `scripts/run_loan_job.py`

**Role:** Production entry point for a **full pipeline run** for one loan. It:

- Takes `--tenant-id`, `--loan-id`, `--source-path`, optional `--run-id`, and flags for LLM, top_k, etc.
- Can short-circuit: if `--run-id` is provided and `job_manifest.json` already exists with status SUCCESS, it exits without re-running.
- Runs the steps below in order (some optional). Emits **PHASE:** lines to stdout so the UI can show progress (INTAKE → PROCESS → STEP13_GENERAL → … → DONE or FAIL).
- Writes **job_manifest.json** under `nas_analyze/tenants/<tenant>/loans/<loan>/<run_id>/`.

**Call order (simplified):**

1. Preflight (mounts).
2. **Step10** (intake) — unless `--skip-intake`.
3. **Step11** (process) — unless `--skip-process`.
4. **Step13** (general retrieval pack).
5. Optionally: **Step13** again (income-focused retrieval), then **Step12** (income_analysis).
6. Optionally: **Step12** (uw_decision).
7. Write manifest; emit DONE or FAIL.

**Invoked by:** Loan API when you click “Process Loan” (API starts a background thread that runs `run_loan_job.py`).

---

## 4. Step 10 — Intake: `step10_intake.py`

**Path:** `scripts/step10_intake.py`

**Role:** Copy loan documents from the **read-only source path** into the ingest area.

- **Input:** `--tenant-id`, `--loan-id`, `--source-path` (must be under source mount).
- **Output:**  
  - Files copied to: `nas_ingest/tenants/<tenant>/loans/<loan>/synology_stage/<timestamp>/...`  
  - `_meta/intake_manifest.json` (file list, document_id, paths, sha256).  
  - `_meta/source_system.json`.

**Called by:** `run_loan_job.py` (first step when not `--skip-intake`).

**Depends on:** `lib` (paths, preflight, validate_source_path, ensure_dir, sha256_file).

---

## 5. Step 11 — Process: `step11_process.py`

**Path:** `scripts/step11_process.py`

**Role:** Extract text from staged files (PDF, DOCX, XLSX), **chunk** it, **embed** with E5-large-v2, and **upsert** into **Qdrant**. Writes chunk artifacts under `nas_chunk` so Step13 can reconstruct text later.

- **Input:** `--tenant-id`, `--loan-id`, `--run-id`. Reads from `nas_ingest` (intake_manifest.json and staged files).
- **Output:**  
  - `nas_chunk/tenants/<tenant>/loans/<loan>/<run_id>/` — chunks (e.g. chunks.jsonl, chunk_map.json), `_meta/processing_run.json`.  
  - Qdrant: vectors in collection `{tenant}_e5largev2_1024_cosine_v1` with payload including `run_id`.

**Called by:** `run_loan_job.py` (after Step10, when not `--skip-process`).

**Depends on:** `lib`, PyPDF/python-docx/openpyxl, sentence_transformers, qdrant_client.

---

## 6. Step 13 — Retrieval Pack Builder: `step13_build_retrieval_pack.py`

**Path:** `scripts/step13_build_retrieval_pack.py`

**Role:** Turn a **query** into a **retrieval pack**: embed the query, search Qdrant (filtered by tenant, loan, run_id), then reconstruct chunk text from `nas_chunk` and write a single JSON pack.

- **Input:** `--tenant-id`, `--loan-id`, `--run-id`, `--query`, `--top-k`, optional `--max-per-file`, `--required-keywords`, `--out-run-id`, etc.
- **Output:** `nas_analyze/tenants/<tenant>/loans/<loan>/retrieve/<run_id>/retrieval_pack.json` (or path from `--out-dir` / `--out-run-id`).

**Called by:**

- **run_loan_job.py** — twice in the full pipeline: once for a “general” query, and (if enabled) once for an income-focused query before Step12 income_analysis.
- **Loan API** — when you “Ask a question”: API runs Step13 with your question, then Step12.

**Depends on:** `lib`, Qdrant client, sentence_transformers (for query embedding).

---

## 7. Step 12 — Analyze: `step12_analyze.py`

**Path:** `scripts/step12_analyze.py`

**Role:** Use the **retrieval pack** and an **LLM (Ollama)** to produce answers. Can run multiple profiles (default, income_analysis, uw_conditions, uw_decision). Writes profile outputs under the run directory.

- **Input:** `--tenant-id`, `--loan-id`, `--run-id`, `--query`, `--analysis-profile`, Ollama URL/model/timeout, evidence/max-tokens options. May auto-run Step13 if no retrieval pack exists (unless `--no-auto-retrieve`).
- **Output:** Under `nas_analyze/tenants/<tenant>/loans/<loan>/<run_id>/outputs/profiles/<profile>/`: e.g. `answer.json`, `answer.md`, `citations.jsonl`, and profile-specific files (income_analysis.json, dti.json, decision.json, etc.).

**Called by:**

- **run_loan_job.py** — for income_analysis and uw_decision (after the corresponding Step13).
- **Loan API** — for “Ask a question”: after Step13, the API runs Step12 with your question and selected profile/model.

**Depends on:** `lib`, requests (Ollama), Step13 (optional, for auto-retrieve).

---

## 8. Loan API: `loan_api.py`

**Path:** `scripts/loan_api.py`

**Role:** **FastAPI** service that:

- Exposes **HTTP endpoints** for health, source-of-truth loan list (`/tenants/.../source_loans`), browse, jobs, runs, artifacts, and **query** (sync and async).
- **Process Loan:** Starts a background thread that runs **run_loan_job.py**; streams stdout so the UI can show phase progress; stores job state in memory (no DB).
- **Ask a question:** For the sync path, runs **Step13** (build retrieval pack for the question) then **Step12** (analyze with chosen profile/LLM). Returns the answer JSON.
- Optional API key and tenant allowlist (middleware).

**Invokes:** run_loan_job.py (subprocess), step13_build_retrieval_pack.py, step12_analyze.py (subprocesses). Uses `lib` only indirectly via those scripts.

---

## 9. Web UI: `scripts/webui/`

**Path:** `scripts/webui/index.html`, `app.js`, `styles.css`

**Role:** Static UI served at `/ui`. Lets you:

- **Refresh Loans** — calls `GET /tenants/<tenant>/source_loans` (source-of-truth list with needs_reprocess).
- Select a loan (sets source path and last-processed run from the list).
- **Process Loan** — calls `POST .../runs/start` → starts run_loan_job; polls `GET /jobs/<job_id>` and shows phase progress (Intake → Process → … → Done).
- **Ask a question** — sends question + profile + LLM model to the API; API runs Step13 then Step12; UI shows “Processing…” and then the answer or error.

**Calls:** Loan API only (REST). No direct calls to run_loan_job or steps.

---

## 10. Call Order Summary

### Full pipeline (“Process Loan”)

```
User clicks Process Loan
  → loan_api.py (POST .../runs/start)
    → Starts background thread running run_loan_job.py
      → preflight_mount_contract()
      → Step10 (intake)        ← step10_intake.py
      → Step11 (process)       ← step11_process.py
      → Step13 (general RP)    ← step13_build_retrieval_pack.py
      → [optional] Step13 (income RP) then Step12 (income_analysis)  ← step12_analyze.py
      → [optional] Step12 (uw_decision)
      → Write job_manifest.json
      → Emit PHASE:DONE or PHASE:FAIL
```

### Ask a question (sync)

```
User asks question in UI
  → loan_api.py (POST .../runs/<run_id>/query)  [or sync path when query_jobs 404]
    → Step13 (retrieval pack for that question)  ← step13_build_retrieval_pack.py
    → Step12 (analyze with profile + LLM)        ← step12_analyze.py
    → Return answer JSON to UI
```

---

## 11. Data Flow (Where Things Live)

| Data / artifact        | Location / producer |
|------------------------|----------------------|
| Original loan docs     | Read-only source (e.g. `/mnt/source_loans/...`) |
| Staged files + manifest| `nas_ingest/tenants/<tenant>/loans/<loan>/` (Step10) |
| Chunks + embeddings    | `nas_chunk/.../<run_id>/` (Step11); vectors in Qdrant |
| Retrieval pack         | `nas_analyze/.../retrieve/<run_id>/retrieval_pack.json` (Step13) |
| Analysis outputs       | `nas_analyze/.../<run_id>/outputs/profiles/<profile>/` (Step12) |
| Job manifest           | `nas_analyze/.../<run_id>/job_manifest.json` (run_loan_job.py) |

---

## 12. Other Scripts (Brief)

- **run_loan_pipeline.py** — Alternative CLI entry point for a single pipeline run (similar to what run_loan_job.py does); can be used for one-off or smoke tests.
- **run_regression_smoke.sh** — Runs a full pipeline + Step12 profiles and checks outputs (retrieval pack, citations, DTI, etc.).
- **loan_service/** — Optional service layer (JobService, adapters); if used, it still invokes run_loan_job.py or Step13+Step12 the same way.

---

*Document generated for MortgageDocAI. Path contracts and authority: see MortgageDocAI_CONTRACT.md and ARCHITECTURE_AUTHORITY.md.*
