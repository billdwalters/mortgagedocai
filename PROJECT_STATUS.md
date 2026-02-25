# MortgageDocAI — Project Status

**Last Updated:** 2026-02-24

## Current phase & AI context

**Durable context:** `.cursor/project_context.md` (phase, non-negotiables, authority, milestone — update there.)

**Phase:** Structured Intelligence / Productization (infrastructure stabilization complete.)

**Definition of “finish” (current):**
1. Production-ready structured outputs  
2. Stable regression harness  
3. Clean logs (debug gated)  
4. Reliable checklist extraction  
5. Ability to expand into deterministic underwriting rule engine  

**Priority order:**
1. Improve quality of `uw_conditions` extraction (deduplicate, normalize categories, prevent boilerplate inflation).  
2. Add structured financial extraction (income, liabilities, DTI) — deterministic math + rule-based evaluation.  
3. Harden audit trail (reproducible runs, exportable JSON artifacts, version tagging).  

**Non-negotiables:** No cloud APIs. Do not rename folders/files. Preserve folder contracts (`nas_chunk/`, `nas_analyze/`, `outputs/`). Maintain `run_id` determinism. Preserve citation-integrity filtering. No change that breaks the regression smoke test.  

**Authority order:** `MortgageDocAI_CONTRACT.md` → `ARCHITECTURE_AUTHORITY.md` → this file. When in doubt, ask before refactoring; prefer fail-loud over silent degradation.

---

## Current Milestone

**Local-only, end-to-end loan Q&A pipeline is operational with Ollama LLM answering.**

The system can:
- Ingest scanned loan documents
- Extract and OCR text
- Chunk and embed content locally
- Index embeddings in Qdrant
- Retrieve evidence for arbitrary questions
- Produce audit-safe retrieval packs
- Answer questions using local Ollama LLM with evidence-only citations

---

## Architecture Summary (v1)

Pipeline steps:

1. **Step10 — Intake**
   - Copies scanned loan documents from a read-only source folder
   - Writes immutable intake artifacts to:
     ```
     nas_ingest/tenants/<tenant>/loans/<loan>/
     ```
   - Generates `intake_manifest.json` and `source_system.json`

2. **Step11 — Process**
   - **Supported formats:** PDF, DOCX, XLSX. Text is extracted deterministically (DOCX: paragraphs; XLSX: sheets by name, rows tab-separated). Extracts text and performs OCR when needed for PDFs.
   - Chunks documents deterministically
   - Writes chunk artifacts to:
     ```
     nas_chunk/tenants/<tenant>/loans/<loan>/<run_id>/chunks/<document_id>/
     ```
     including:
     - `chunks.jsonl` — one JSON line per chunk (chunk_id, text, page, document_id, text_norm_sha256)
     - `chunk_map.json` — maps chunk_id to provenance (document_id, pages, chunk_index, file_relpath)
   - Writes `_meta/processing_run.json` (embedding model, chunker settings, counts)
   - Embeds chunks using **E5-large-v2**
   - Upserts embeddings into Qdrant using deterministic UUIDs derived from `chunk_id`
   - Qdrant payload includes `run_id` to prevent cross-run vector mixing
   - Contract assertions before publish: processing_run.json must exist, at least one chunks.jsonl required
   - Publishes artifacts atomically

3. **Step13 — Retrieval Pack Builder**
   - Embeds user questions locally
   - Queries Qdrant with tenant + loan filters
   - Reconstructs chunk text from `nas_chunk`
   - Writes retrieval pack to:
     ```
     nas_analyze/tenants/<tenant>/loans/<loan>/retrieve/<run_id>/retrieval_pack.json
     ```

4. **Step12 — Analyze (Ollama LLM v1)**
   - Ensures retrieval pack exists (runs Step13 if needed)
   - Calls local Ollama `/api/generate` with evidence-only prompt
   - Produces analysis outputs under:
     ```
     nas_analyze/tenants/<tenant>/loans/<loan>/<run_id>/
     ```
   - Supports multiple queries and profiles per run
   - Overwrites outputs safely on re-run for same `run_id`

---

## Pipeline Status

| Step | Name | Status |
|------|------|--------|
| Step 10 | Intake / staging | Working |
| Step 11 | Process (extract, OCR, chunk, embed, Qdrant) | **Repaired: full artifact write + run_id in Qdrant** |
| Step 13 | Retrieval Pack Builder | **run_id-scoped; latest fallback removed** |
| Step 12 | Analyze (Ollama LLM answering) | **Ollama v1 + hardened citations** |

---

## What Works Today (Verified)

- Step10 → Step11 → Step13 → Step12 run end-to-end
- Encrypted PDFs are safely skipped with warnings
- Qdrant collection: `{tenant}_e5largev2_1024_cosine_v1`
- Qdrant point IDs are deterministic UUIDs derived from sha256 chunk_id
- Qdrant payload includes `run_id` for cross-run isolation
- Step11 writes `chunks.jsonl`, `chunk_map.json`, and `processing_run.json` (required by Step13)
- Retrieval packs written under `retrieve/<pipeline_run_id>/retrieval_pack.json`
- Step12 auto-triggers Step13 if retrieval pack is missing
- Re-runs for same `run_id` overwrite cleanly
- **Loan API (FastAPI):** health, list tenants/loans/runs, **source-of-truth source_loans** (list + by loan_id), sync query, pipeline jobs, query jobs, artifacts index and artifact downloads; optional API key and tenant allowlist (see Loan API section below)
- **Web UI (/ui):** Refresh Loans from source-of-truth mount; loan list with Needs Processing / Up to date badges; Process Loan uses selected loan’s source_path; progress stepper shows live phase colors (streamed job stdout)

---

## Step 11 — Contract Repair (2026-02-10)

Step11 was previously only upserting to Qdrant and writing `collection_name.txt`. It did NOT write `chunks.jsonl`, `chunk_map.json`, or `processing_run.json`, which broke Step13 chunk text reconstruction.

### Changes

- **chunks.jsonl** now written per document under `chunks/<document_id>/chunks.jsonl`. Each line is JSON with: `chunk_id`, `document_id`, `file_relpath`, `page_start`, `page_end`, `chunk_index`, `text` (raw, without "passage: " prefix), `text_norm_sha256`.
- **chunk_map.json** now written per document. Maps chunk_id to provenance fields.
- **processing_run.json** now written under `_meta/`. Includes embedding model, dim, distance, device, chunker settings, total_chunks, upserts, skipped_encrypted_count, documents_processed, qdrant_collection.
- **run_id added to Qdrant payload** — prevents cross-run vector mixing when the same loan is processed multiple times.
- **Contract assertions** before atomic publish: `processing_run.json` must exist and at least one `chunks.jsonl` must be present. Raises `ContractError` on failure.

### Verification (on AI server)

```bash
# Run Step11 for a loan
python3 scripts/step11_process.py \
  --tenant-id peak --loan-id 16271681 --run-id $(date -u +%Y-%m-%dT%H%M%SZ)

# Confirm artifacts exist
find /mnt/nas_apps/nas_chunk/tenants/peak/loans/16271681/<run_id>/ -name "chunks.jsonl" | wc -l
cat /mnt/nas_apps/nas_chunk/tenants/peak/loans/16271681/<run_id>/_meta/processing_run.json | python3 -m json.tool

# Run Step13 against that run_id and confirm chunks_with_text > 0
python3 scripts/step13_build_retrieval_pack.py \
  --tenant-id peak --loan-id 16271681 --run-id <run_id> --query "What is the loan amount?"
```

---

## Step 11 — DOCX and XLSX support

Step11 now ingests **.docx** and **.xlsx** in addition to PDF. Staged files (from Step10) with these extensions are extracted to text, written to `<run_dir>/text/<document_id>.txt`, then chunked and embedded like PDFs.

- **DOCX:** `python-docx`; paragraphs concatenated with newlines (empty lines omitted). Deterministic.
- **XLSX:** `openpyxl` (read-only, data_only); each sheet emitted as `Sheet: <name>` then rows as tab-separated cell values. Sheets ordered by name for stability.
- **Dependencies:** `pip install python-docx openpyxl` (add to environment if not already installed).
- **Behavior:** Same chunking/embedding and Qdrant payload as PDF; document_id and file_relpath from intake manifest. Unreadable files are skipped with a warning.

---

## Step 12 — Ollama Integration (v1)

Step12 now calls a local Ollama instance (`/api/generate`) with an evidence-only prompt built from Step13 retrieval packs.

### CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--ollama-url` | `http://localhost:11434` | Ollama server URL |
| `--llm-model` | `llama3` | Model name for Ollama |
| `--llm-temperature` | `0` | Sampling temperature |
| `--llm-max-tokens` | `800` | Max tokens to generate |
| `--evidence-max-chars` | `12000` | Max chars of evidence sent to LLM |
| `--no-auto-retrieve` | off (auto-retrieve ON) | Disable Step13 auto-retrieval when retrieval pack is missing |

### Output Files

Step12 now writes under `outputs/`:

- `answer.md` — Human-readable answer with inline citations
- `answer.json` — Machine-readable JSON with answer, citations, confidence
- `citations.jsonl` — One JSON line per citation (`chunk_id`, `quote`)

Per-profile outputs are written under `outputs/profiles/<profile>/`.

### Run Commands

**Single query:**
```bash
python3 scripts/step12_analyze.py \
  --tenant-id peak \
  --loan-id LOAN001 \
  --run-id 2026-02-09T120000Z \
  --query "What are the key underwriting conditions?" \
  --analysis-profile default \
  --ollama-url http://localhost:11434 \
  --llm-model llama3
```

**Multiple queries/profiles:**
```bash
python3 scripts/step12_analyze.py \
  --tenant-id peak \
  --loan-id LOAN001 \
  --run-id 2026-02-09T120000Z \
  --query "What is the loan amount?" \
  --query "List all conditions of approval." \
  --analysis-profile underwriting \
  --analysis-profile conditions \
  --ollama-url http://localhost:11434 \
  --llm-model llama3
```

**Full pipeline with Ollama flags:**
```bash
python3 scripts/run_loan_pipeline.py \
  --tenant-id peak \
  --loan-id LOAN001 \
  --source-path /mnt/source_loans/5-Borrowers\ TBD/LOAN001 \
  --query "Summarize the key underwriting conditions." \
  --llm-model llama3 \
  --ollama-url http://localhost:11434
```

### Prerequisites

- Ollama running locally on port 11434
- Model pulled: `ollama pull llama3`
- `requests` Python package installed (`pip install requests`)

### Validation

```bash
# Compile check all scripts
python3 -m py_compile scripts/step12_analyze.py
python3 -m py_compile scripts/run_loan_pipeline.py
python3 -m py_compile scripts/lib.py
python3 -m py_compile scripts/step10_intake.py
python3 -m py_compile scripts/step11_process.py
python3 -m py_compile scripts/step13_build_retrieval_pack.py
```

### Hardening (2026-02-10)

- **Citation integrity**: LLM citations are filtered against `allowed_chunk_ids` from the retrieval pack. Only chunk_ids actually present in retrieved evidence are kept. If the LLM hallucinated all citations, confidence is capped at 0.2.
- **Retrieval pack provenance**: `answer.json` and `analysis_run.json` now include `retrieval_pack_source` field (`"explicit"`, `"run_id"`, `"latest"`, or `"step13"`) for auditability.
- **`--no-auto-retrieve` flag**: Replaces the broken `--auto-retrieve` (which was `store_true` with `default=True`, making it impossible to disable). Auto-retrieve remains ON by default; pass `--no-auto-retrieve` to disable for deterministic debugging.

### Notes

- LLM response is parsed with robust JSON extraction. If Ollama returns non-JSON, the raw text is wrapped as `answer` with empty citations and confidence `0.3`.
- The prompt instructs the model to use `[chunk_id=<sha256>]` citation format and return structured JSON.
- No cloud APIs are used. All processing is local-only.
- Bug fix: `_run_step13()` previously referenced undefined `args` variable; now accepts `run_id` as explicit parameter.

---

## Step 13 — run_id Scoping (2026-02-11)

Step13 retrieval is now deterministically scoped to `run_id`. The unsafe "latest run" fallback has been removed.

### Changes

- **`--run-id` is now required** (was optional with latest-fallback). Omitting it causes argparse to exit with an error.
- **Deleted `_find_latest_run_dir()`** — eliminated entirely; no code path falls back to "latest".
- **Qdrant filter includes `run_id`** — filter now enforces `tenant_id` + `loan_id` + `run_id` (was only tenant + loan).
- **Defense-in-depth post-filter** — after Qdrant returns hits, any hit whose `payload.run_id != requested run_id` is dropped with a warning.
- **0-hit fail-loud** — if no hits survive filtering, raises `ContractError` with a diagnostic message (no silent success, no fallback).
- **Step12 `_run_step13()` updated** — now passes `--run-id` to Step13 subprocess call (required since `--run-id` is no longer optional).

### Acceptance Tests

```bash
# A) Smoke test: existing run_id succeeds
python3 scripts/step13_build_retrieval_pack.py \
  --tenant-id peak \
  --loan-id 16271681 \
  --run-id 2026-02-11T054835Z \
  --query "List all underwriting conditions / conditions of approval." \
  --out-run-id 2026-02-11T054835Z \
  --top-k 25

# B) Negative test: invalid run_id must fail loudly
python3 scripts/step13_build_retrieval_pack.py \
  --tenant-id peak \
  --loan-id 16271681 \
  --run-id DOES-NOT-EXIST \
  --query "List all underwriting conditions / conditions of approval." \
  --out-run-id DOES-NOT-EXIST \
  --top-k 25

# C) Compile check
python3 -m py_compile scripts/lib.py
python3 -m py_compile scripts/step10_intake.py
python3 -m py_compile scripts/step11_process.py
python3 -m py_compile scripts/step12_analyze.py
python3 -m py_compile scripts/step13_build_retrieval_pack.py
python3 -m py_compile scripts/run_loan_pipeline.py
```

---

## Step 13 — Qdrant query_points Migration + Offline Embeddings (2026-02-11)

### Qdrant API Migration

Step13 now uses `qdrant.query_points()` instead of the deprecated `qdrant.search()`. This eliminates the `DeprecationWarning` from qdrant-client. Retrieval behavior is identical.

| Before | After |
|--------|-------|
| `qdrant.search(query_vector=qvec, ...)` | `qdrant.query_points(query=qvec, ...).points` |

### Offline Embedding Mode

New CLI flag: `--offline-embeddings` (default: off).

When set:
- Sets `TRANSFORMERS_OFFLINE=1`, `HF_HUB_OFFLINE=1`, and `HF_DATASETS_OFFLINE=1` **before** any HF/transformers code is imported (deferred imports)
- Uses `huggingface_hub.snapshot_download(local_files_only=True)` to resolve the model to its local cache path
- Loads `SentenceTransformer` from the resolved local path — zero network access
- If the model is not already cached locally, raises `ContractError` with a clear pre-cache instruction
- Eliminates the "unauthenticated requests to the HF Hub" warning entirely

When not set:
- Default behavior (may download model if not cached)
- No HF_TOKEN required

**Implementation note:** `sentence_transformers` and `torch` are imported inside `main()` (not at module top level) so that offline env vars are set before any HF initialization code runs.

**Pre-caching the model:** Run Step11 or Step13 once *without* `--offline-embeddings` on a network-connected provisioning machine, or run `python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('intfloat/e5-large-v2')"` to download the model into `~/.cache/huggingface/hub/`. After that, `--offline-embeddings` will work on any machine that has a copy of that cache directory.

### Example Commands

```bash
# Normal mode (may download model if not cached)
python3 scripts/step13_build_retrieval_pack.py \
  --tenant-id peak \
  --loan-id 16271681 \
  --run-id 2026-02-11T054835Z \
  --query "List all underwriting conditions / conditions of approval." \
  --out-run-id 2026-02-11T054835Z \
  --top-k 25

# Offline mode (no network, model must be pre-cached)
python3 scripts/step13_build_retrieval_pack.py \
  --tenant-id peak \
  --loan-id 16271681 \
  --run-id 2026-02-11T054835Z \
  --query "List all underwriting conditions / conditions of approval." \
  --out-run-id 2026-02-11T054835Z \
  --top-k 25 \
  --offline-embeddings
```

---

## Step 13 — Debug + Strict Mode (2026-02-12)

Step13 now supports `--debug` for verbose diagnostics and `--strict` to fail if any Qdrant hit cannot be mapped to local chunk text.

| Flag | Default | Description |
|------|---------|-------------|
| `--debug` | off | Print diagnostic output (chunk_index size, hit counts, missing chunk_id warnings) |
| `--strict` | off | Fail with `ContractError` if any hit's `chunk_id` is missing from `chunk_index` |

**Default behavior (no flags):** Only the success line is printed. Missing chunk_ids are silently dropped.

**`--debug`:** Prints all diagnostics plus a warning if any chunk_ids were dropped.

**`--strict`:** If any chunk_ids are missing, raises `ContractError` before writing `retrieval_pack.json`. Useful for QA gating.

```bash
# Verbose diagnostics
python3 scripts/step13_build_retrieval_pack.py \
  --tenant-id peak --loan-id 16271681 --run-id 2026-02-11T054835Z \
  --query "conditions of approval" --out-run-id 2026-02-11T054835Z \
  --top-k 80 --offline-embeddings --debug

# Strict QA mode (fail on any missing chunk text)
python3 scripts/step13_build_retrieval_pack.py \
  --tenant-id peak --loan-id 16271681 --run-id 2026-02-11T054835Z \
  --query "conditions of approval" --out-run-id 2026-02-11T054835Z \
  --top-k 80 --offline-embeddings --strict
```

---

## Step 13 — Retrieval diversity (--max-per-file)

To prevent one document from dominating the retrieval pack (e.g. many chunks from a single disclosure PDF), Step13 supports **`--max-per-file`**.

- **Meaning:** After Qdrant retrieval and run_id filtering, cap the number of chunks returned per unique `payload.file_relpath`. Chunks are grouped by file, sorted by score within each file, top N kept per file, then reassembled and sorted globally by score; final list is truncated to `top_k`.
- **Default:** `12`. Use `0` or a very large value for no cap (same behavior as before).
- **Why:** Improves diversity for income_analysis and other profiles so evidence is not dominated by a single file family.

| Flag | Default | Description |
|------|---------|-------------|
| `--max-per-file` | 12 | Max chunks per unique file_relpath; 0 or very large = no cap |

**With `--debug`:** Prints before/after top 10 file_relpath counts.

```bash
python3 scripts/step13_build_retrieval_pack.py \
  --tenant-id peak --loan-id 16271681 --run-id 2026-02-11T054835Z \
  --query "income paystub salary VOE liabilities credit report monthly payment PITIA" \
  --out-run-id 2026-02-11T054835Z \
  --top-k 120 --max-per-file 12 \
  --offline-embeddings --debug
```

---

## Validated Command Set (2026-02-11)

### Step13 — Retrieval Pack (offline recommended)

```bash
python3 scripts/step13_build_retrieval_pack.py \
  --tenant-id peak \
  --loan-id 16271681 \
  --run-id 2026-02-11T054835Z \
  --query "List all underwriting conditions / conditions of approval." \
  --out-run-id 2026-02-11T054835Z \
  --top-k 80 \
  --offline-embeddings
```

### Step12 — phi3 (known good)

```bash
python3 scripts/step12_analyze.py \
  --tenant-id peak \
  --loan-id 16271681 \
  --run-id 2026-02-11T054835Z \
  --query "List all underwriting conditions found in the provided documents." \
  --analysis-profile default \
  --ollama-url http://localhost:11434 \
  --llm-model phi3 \
  --llm-temperature 0 \
  --llm-max-tokens 900 \
  --evidence-max-chars 4500
```

### Step12 — mistral (slower, needs higher timeout or smaller budgets)

```bash
python3 scripts/step12_analyze.py \
  --tenant-id peak \
  --loan-id 16271681 \
  --run-id 2026-02-11T054835Z \
  --query "List all underwriting conditions found in the provided documents." \
  --analysis-profile default \
  --ollama-url http://localhost:11434 \
  --llm-model mistral \
  --llm-temperature 0 \
  --llm-max-tokens 450 \
  --evidence-max-chars 3500 \
  --ollama-timeout 900
```

### CLI Notes

| Flag | Default | Description |
|------|---------|-------------|
| `--ollama-timeout` | 300 | Timeout in seconds for Ollama HTTP request. Increase for slower models (mistral). |
| `--debug` | off | Enable parser diagnostics (llm_raw preview, parsed keys/types, citation filter counts). |

**Tip:** Mistral tends to produce truncated JSON at lower `--llm-max-tokens` values. The parser will repair truncated output and salvage the answer + any citations emitted before truncation. For best results, use `--llm-max-tokens 900` or higher if the model can handle it within the timeout.

---

## Regression Smoke Test

One-command health check that proves the end-to-end pipeline is working: Step13 retrieval pack build → Step12 analysis with phi3 → Step12 analysis with mistral, plus citation integrity assertions.

### Run

```bash
cd /opt/mortgagedocai && source venv/bin/activate && bash scripts/run_regression_smoke.sh
```

### Override defaults via env vars

```bash
RUN_ID=2026-02-12T100000Z bash scripts/run_regression_smoke.sh
```

### What it checks

1. `retrieval_pack.json` exists and `retrieved_chunks > 0`
2. Step12 outputs exist (`answer.json`, `citations.jsonl`) for both models
3. `citations.jsonl > 0` lines for both phi3 and mistral
4. Every cited `chunk_id` in `answer.json` and `citations.jsonl` exists in the retrieval pack (integrity)

### Configurable env vars

| Variable | Default | Description |
|----------|---------|-------------|
| `TENANT_ID` | `peak` | Tenant |
| `LOAN_ID` | `16271681` | Loan |
| `RUN_ID` | `2026-02-11T054835Z` | Pipeline run ID |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |
| `EMBED_OFFLINE` | `1` | Pass `--offline-embeddings` to Step13 |
| `TOP_K` | `80` | Retrieval top-k |
| `PHI3_MODEL` | `phi3` | Phi3 model name |
| `MISTRAL_MODEL` | `mistral` | Mistral model name |
| `PHI3_MAX_TOKENS` | `900` | Max tokens for phi3 |
| `MISTRAL_MAX_TOKENS` | `450` | Max tokens for mistral |
| `OLLAMA_TIMEOUT` | `900` | Ollama HTTP timeout (seconds) |
| `RUN_UW_CONDITIONS` | `0` | Set to `1` to also run and validate the `uw_conditions` profile |
| `UW_QUERY` | `Extract underwriting conditions...` | Query for uw_conditions profile |
| `UW_MAX_TOKENS` | `650` | Max tokens for uw_conditions LLM call |
| `UW_EVIDENCE_MAX_CHARS` | `4500` | Max evidence chars for uw_conditions |
| `RUN_INCOME_ANALYSIS` | `0` | Set to `1` to run income_analysis profile with targeted retrieval |
| `INCOME_QUERY` | `Extract all income sources...` | Query for income_analysis LLM call |
| `INCOME_RETRIEVE_QUERY` | `Estimated Total Monthly Payment PITIA...` | Query for income-focused retrieval pack (doc-type keywords) |
| `INCOME_TOP_K` | `120` | Retrieval top-k for income pack |
| `INCOME_MAX_PER_FILE` | `12` | Max chunks per file for income retrieval diversity |
| `INCOME_MAX_TOKENS` | `650` | Max tokens for income_analysis LLM call |
| `INCOME_EVIDENCE_MAX_CHARS` | `4500` | Max evidence chars for income_analysis |
| `EXPECT_DTI` | `0` | Set to `1` to assert at least one DTI ratio is non-null |
| `RUN_UW_DECISION` | `0` | Set to `1` to run and validate the `uw_decision` profile |
| `UW_DECISION_QUERY` | `Deterministic underwriting decision...` | Query for uw_decision (ignored by deterministic handler) |

### Optional: uw_conditions validation

```bash
RUN_UW_CONDITIONS=1 bash scripts/run_regression_smoke.sh
```

When enabled, the smoke test additionally:
5. Runs Step12 with `--analysis-profile uw_conditions` (mistral)
6. Asserts `conditions.json` exists with valid schema
7. Verifies every condition citation `chunk_id` exists in the retrieval pack

### Optional: uw_decision validation

```bash
RUN_UW_DECISION=1 bash scripts/run_regression_smoke.sh
```

When enabled, the smoke test additionally:
8. Runs Step12 with `--analysis-profile uw_decision` (no LLM — deterministic)
9. Asserts `decision.json` exists with valid schema and `decision_primary.status` ∈ {PASS, FAIL, UNKNOWN}
10. Validates `ruleset.version = v0.7-policy`, `policy_source` ∈ {file, default}, and citation structure
11. Asserts `answer.json`, `answer.md`, `decision.md`, and `outputs/_meta/version.json` exist

---

## Step12 Profile: uw_conditions (2026-02-12)

Structured underwriting conditions extraction. When `--analysis-profile uw_conditions` is used, Step12 sends a conditions-specific prompt to the LLM and writes an additional `conditions.json` alongside the standard `answer.md`, `answer.json`, and `citations.jsonl`.

### Output

Written to `outputs/profiles/uw_conditions/conditions.json`:

```json
{
  "profile": "uw_conditions",
  "question": "...",
  "conditions": [
    {
      "description": "short actionable condition text",
      "category": "Verification|Assets|Income|Credit|Property|Title|Insurance|Compliance|Other",
      "timing": "Prior to Closing|Prior to Docs|Post Closing|Unknown",
      "citations": [{"chunk_id": "...", "quote": "..."}],
      "source": {
        "documents": [
          {
            "document_id": "...",
            "file_relpath": "relative/path/to/file.pdf",
            "page_start": 1,
            "page_end": 1,
            "chunk_ids": ["chunk_id_1", "chunk_id_2"]
          }
        ]
      }
    }
  ],
  "confidence": 0.0-1.0
}
```

### Rules

- Every condition must have ≥1 citation with a valid `chunk_id` from the retrieval pack.
- Conditions with zero valid citations are dropped.
- If all conditions are dropped, confidence is capped at 0.3.
- Each condition includes `source.documents` metadata derived from the retrieval pack's chunk payload (`document_id`, `file_relpath`, `page_start`, `page_end`). Citations are grouped by document; page ranges are widened to cover all cited chunks.

### Example command (mistral)

```bash
python3 scripts/step12_analyze.py \
  --tenant-id peak --loan-id 16271681 --run-id 2026-02-11T054835Z \
  --query "Extract underwriting conditions / conditions of approval as a checklist." \
  --analysis-profile uw_conditions \
  --ollama-url http://localhost:11434 --llm-model mistral \
  --llm-temperature 0 --llm-max-tokens 650 --evidence-max-chars 4500 \
  --ollama-timeout 900
```

### Verification

```bash
python3 -c 'import json; p="/mnt/nas_apps/nas_analyze/tenants/peak/loans/16271681/2026-02-11T054835Z/outputs/profiles/uw_conditions/conditions.json"; j=json.load(open(p)); print("conditions:", len(j.get("conditions",[])), "confidence:", j.get("confidence"))'
```

---

## Step12 Profile: income_analysis (2026-02-13)

Structured income/liability extraction with deterministic DTI computation. When `--analysis-profile income_analysis` is used, Step12 sends an income-specific prompt to the LLM, normalizes the output, and computes DTI ratios in Python only (no LLM math).

### Output files

Written to `outputs/profiles/income_analysis/`:

- **income_analysis.json** — Normalized extraction: income_items, liability_items, proposed_pitia, confidence.
- **dti.json** — Python-computed DTI: monthly_income_total, monthly_debt_total, housing_payment_used, front_end_dti, back_end_dti, missing_inputs, notes, inputs_snapshot.
- **answer.md / answer.json / citations.jsonl** — Standard profile outputs with synthesized answer text and DTI summary.

### Proposed PITIA extraction (deterministic)

PITIA (PITI + HOA) is extracted **deterministically** from evidence text using regex, not LLM math. The LLM prompt also asks for `proposed_pitia` as a fallback, but the deterministic extractor takes priority.

- **Schema:** `"proposed_pitia": {"value": number, "citations": [{"chunk_id": "...", "quote": "..."}]}`
- **Algorithm (`_extract_proposed_pitia_from_retrieval_pack`):**
  1. Scan all retrieved_chunks text using three regex tiers in priority order:
     - `PITIA_PATTERN_PRIMARY` — same-line with `$` sign
     - `PITIA_PATTERN_MULTILINE` — tolerates line breaks between "Estimated Total" and "Monthly Payment"
     - `PITIA_PATTERN_NO_DOLLAR` — amount without `$` prefix
  2. Parse amounts robustly (`\d{1,3}(?:,\d{3})*(?:\.\d{2})?`)
  3. Additive scoring: +3 "Closing Disclosure", +2 "Loan Estimate", +1 "Projected Payments"
  4. Tiebreaker: prefer amounts with decimals (e.g. `3,203.31` over `3203`)
  5. Store quote (≤200 chars, ±100 around match)
- **Priority:** Deterministic PITIA > LLM-extracted PITIA > legacy `housing_payment_monthly_optional`
- **DTI:** `proposed_pitia.value` is used as the housing payment for both front-end and back-end DTI ratios.

### Targeted retrieval for income_analysis

When `RUN_INCOME_ANALYSIS=1` in the smoke test, a **separate** retrieval pack is built with doc-type keywords and `--max-per-file` to ensure document diversity and surface PITIA evidence:

```bash
python3 scripts/step13_build_retrieval_pack.py \
  --query "Estimated Total Monthly Payment PITIA Proposed housing payment Principal & Interest Escrow Loan Estimate Closing Disclosure HOA dues Property Taxes Homeowners Insurance credit report liabilities monthly payment" \
  --top-k 120 --max-per-file 12 --offline-embeddings
```

This prevents income evidence from being dominated by a single document family and ensures Closing Disclosure / Loan Estimate chunks are retrieved for deterministic PITIA extraction.

### Income frequency handling

Only `monthly` and `annual` frequencies are converted to monthly equivalents for DTI computation:

| Frequency | Monthly equivalent |
|-----------|-------------------|
| `monthly` | amount |
| `annual` | amount / 12 |
| `one-time` | null (excluded from total) |
| `unknown` | null (excluded from total) |

Items with non-convertible frequencies are preserved in `income_analysis.json` but excluded from `monthly_income_total` in `dti.json`. A note is added to `dti.json.notes` for each excluded item.

### DTI null semantics

DTI ratios are null (not 0.0) when required inputs are missing:

- `front_end_dti` requires: proposed PITIA + monthly_income_total
- `back_end_dti` requires: proposed PITIA + monthly_liabilities_total + monthly_income_total
- `dti.json.missing_inputs` lists which inputs are absent (e.g. `["proposed_pitia", "monthly_liabilities_total"]`)

### Citation quote backfill

If the LLM omits or truncates a citation quote, the normalizer backfills it from the retrieval pack chunk text (first 200 chars, or a window around the item description). Quotes are never empty in final outputs.

### Confidence calibration

- Both income and PITIA missing → cap ≤ 0.3
- Either income or PITIA missing → cap ≤ 0.5

### EXPECT_DTI smoke test toggle

Set `EXPECT_DTI=1` to assert that at least one DTI ratio (`front_end_dti` or `back_end_dti`) is non-null. Default: `0` (DTI may be null if no convertible income items are found).

The smoke test also asserts: if the retrieval pack contains "Total Monthly Payment" evidence, then `proposed_pitia` must be non-null (deterministic extraction should always succeed when evidence is present).

### Example command

```bash
python3 scripts/step12_analyze.py \
  --tenant-id peak --loan-id 16271681 --run-id 2026-02-13T073441Z \
  --query "Extract all income sources, liabilities, and proposed housing payment (PITIA) for DTI calculation." \
  --analysis-profile income_analysis \
  --ollama-url http://localhost:11434 --llm-model mistral \
  --llm-temperature 0 --llm-max-tokens 650 --evidence-max-chars 4500 \
  --ollama-timeout 900 --debug --save-llm-raw
```

### Smoke test

```bash
RUN_INCOME_ANALYSIS=1 bash scripts/run_regression_smoke.sh
# With DTI expectation:
RUN_INCOME_ANALYSIS=1 EXPECT_DTI=1 bash scripts/run_regression_smoke.sh
```

---

## Step12 — Deterministic PITIA Ranking Fix (2026-02-14)

Fixed incorrect PITIA candidate selection where P&I amount ($2,117.61 from D-CD.pdf) was chosen over the correct total ($3,203.31 from ESTIMATED CD.pdf).

### Root causes addressed

1. **`is_piti_total` classification was wrong**: ETMP pattern matches ("Estimated Total Monthly Payment") were marked `is_piti_total=False`. Fixed: all PITIA/ETMP matches are now `is_piti_total=True`.
2. **No selection gating**: P&I-only matches from high-scoring files could beat true PITIA totals from lower-scoring files. Fixed: if ANY `is_piti_total=True` candidates exist, ALL non-PITIA candidates are discarded before scoring.
3. **File-relpath scoring too dominant**: +100/+50 bonuses overwhelmed other signals. Rescaled to +20 CD / +10 LE, applied after gating.
4. **Regex gap too tight**: `\s{0,5}` widened to `[\s\S]{0,40}` (with mandatory `$` sign) for dollar-sign patterns and `\s{0,20}` for no-dollar pattern.
5. **CD filename regex too narrow**: `_CD_RELPATH_RE` didn't match `ESTIMATED CD.pdf`. Fixed with `\bCD` word boundary pattern.

### Selection gating

If any candidates have `is_piti_total=True`, select ONLY among those. This ensures a true "Estimated Total Monthly Payment" match always beats a "Monthly Principal & Interest" match regardless of file scoring.

### Additive scoring (applied after gating)

| Signal | Bonus |
|--------|-------|
| File-relpath: Closing Disclosure | +20 |
| File-relpath: Loan Estimate | +10 |
| Text: "closing disclosure" | +3 |
| Text: "loan estimate" | +2 |
| Text: "projected payments" | +1 |
| Internal consistency (P&I + Escrow ≈ Total) | +30 |
| Loan ID coherence (matches dominant loan ID) | +5 |
| Decimal precision (xx.xx) | +1 |

### Sort key (after gating)

1. Highest additive score
2. Non-P&I preferred (total > P&I-only)
3. First encountered (stable sort by Qdrant relevance)

---

## Step12 — Deterministic Liabilities Total Extractor (2026-02-14)

Added deterministic extraction of `monthly_liabilities_total` from retrieval pack evidence. Combined with existing deterministic PITIA extraction, this makes `back_end_dti` computable when income items are also present.

### Schema

`income_analysis.json` now includes:
```json
"monthly_liabilities_total": {"value": number|null, "citations": [{"chunk_id": "...", "quote": "..."}]}
```

### Regex patterns (3-tier)

1. **`LIAB_PATTERN_TOTAL_MONTHLY_PAYMENTS`** — "Total Monthly Payments $X" (common on 1003 form)
2. **`LIAB_PATTERN_MONTHLY_DEBT_GENERIC`** — "Total Monthly Debt/Debts/Obligations $X"
3. **`LIAB_PATTERN_CREDIT_REPORT_TOTAL`** — "Total Monthly Payment $X" (only used if chunk text or file_relpath indicates credit report context)

### Exclusion heuristics

Matches near these phrases (±200 chars) are discarded:
- Cash to Close, Closing Costs, Total Closing Costs
- Total of Payments, Finance Charge, Amount Financed

### Scoring

| Signal | Bonus |
|--------|-------|
| Text: "Uniform Residential Loan Application" or "Form 1003" | +3 |
| File-relpath: "Credit Report" or "1003" | +2 |
| Matched by TOTAL_MONTHLY_PAYMENTS pattern | +1 |
| Chunk has ≥2 CD terms (Loan Costs, Prepaids, Escrow) | -5 |

### 1003 form OCR table fallback (Stage B)

When the label-adjacent regex patterns (Stage A) find no candidates, a fallback activates for 1003 form chunks. The Fannie Mae Form 1003 "Assets and Liabilities" page has a table layout where "Total Monthly Payments $" appears as a column header without an adjacent dollar value — the amounts are in separate OCR rows.

**Stage B logic:**
1. Only activates when Stage A found zero candidates
2. Identifies 1003 chunks by "Form 1003" or "Uniform Residential Loan Application" + "Total Monthly Payments"
3. Uses `_1003_LIABILITY_ITEM_RE` to extract individual liability triplets: `payment / months_or_R / balance`
4. Handles revolving credit (`R` = revolving, treated as ongoing)
5. Sanity: `payment > 0`, `months > 0`, `payment <= balance`
6. Sums individual payments → `monthly_liabilities_total`
7. Score: +3 (1003 form), pattern: `1003_ITEM_SUM`

### Retrieval query

`INCOME_RETRIEVE_QUERY` (in `run_regression_smoke.sh`) enhanced with liabilities-focused keywords: `Total Monthly Payments`, `monthly debt obligations`, `Uniform Residential Loan Application`, `assets and liabilities` — ensures Step13 retrieves 1003 form and credit report chunks into the income-focused retrieval pack.

### DTI integration

- Deterministic `monthly_liabilities_total.value` takes priority over LLM-extracted `liability_items` summation
- `_compute_dti()` checks `monthly_liabilities_total` first; falls back to individual items if not available
- `missing_inputs` only includes `"monthly_liabilities_total"` if both paths produce nothing

## Step13 — Required-Keywords Injection (2026-02-14)

Added `--required-keywords` flag to `step13_build_retrieval_pack.py` that force-includes chunks containing specified keywords regardless of vector similarity score.

### Problem solved

1003 form page 4 (Assets & Liabilities) contains tabular OCR data with low semantic similarity to any query. Vector search consistently fails to retrieve these chunks even with liabilities-focused query terms. The `--required-keywords` flag bypasses this limitation.

### Usage

```bash
python3 scripts/step13_build_retrieval_pack.py \
    --required-keywords "Total Monthly Payments" \
    ...other args...
```

Comma-separated for multiple keywords (ALL must match, case-insensitive). Chunks matching all keywords are appended to the retrieval pack with `score=0.0` and `source.injection="required_keywords"`.

### Integration

`run_regression_smoke.sh` uses `INCOME_REQUIRED_KEYWORDS` env var (default: `Total Monthly Payments`) and `INCOME_REQUIRED_KEYWORDS_2` (default: `Profit and Loss`) to pass to the income-focused Step13 call. The flag now supports multiple groups via `action="append"` — each `--required-keywords` is an AND group, with OR across groups. This ensures both 1003 liabilities pages and P&L income chunks are available to the deterministic extractors.

---

## Step12 — Deterministic Income Total Extractor (2026-02-15)

Added deterministic extraction of `monthly_income_total` from retrieval pack evidence. Strategy: AUS-first, 1003 second, P&L fallback. Combined with existing PITIA and liabilities extractors, this completes the deterministic DTI pipeline.

### Schema

`income_analysis.json` now includes:
```json
"monthly_income_total": {"value": number|null, "citations": [...], "source": "AUS"|"1003"|"P&L"|null}
```

### AUS patterns (highest priority, +50/+10 bonus)
- "Total Monthly Income $X" / "Total Qualifying Income $X"
- "Qualifying Income $X" / "Income Used to Qualify $X"

### 1003 form patterns (medium priority, +5 bonus)
- "Gross Monthly Income $X" / "Monthly Income $X"

### P&L business income patterns (self-employed fallback, +3 bonus)
- "Net Income NNNN.NN" from Profit & Loss statements
- "Total Income NNNN.NN" as secondary
- P&L values are period totals → divided by detected period months
- Period detection: "January 1 - September 7, 2020" → 9 months
- Default period: 12 months if not detected

### Scoring

| Signal | Bonus |
|--------|-------|
| file_relpath: C-AUS or Desktop Underwriter | +50 |
| Text: Desktop Underwriter / DU Findings | +10 |
| Text: Form 1003 / Uniform Residential Loan Application | +5 |
| Text: Profit and Loss | +3 |
| Pattern: AUS Total / Qualifying | +4-5 |
| Pattern: Gross Monthly Income | +2 |
| Pattern: P&L Net Income | +1 |
| Pattern: P&L Total Income | +0 |
| Decimal precision | +1 |
| Junk folder | -10 |

### Exclusion phrases
- "Estimated Total Monthly Payment", "Principal & Interest", "Escrow"
- "Closing Cost", "Cash to Close"

### Sanity bounds
- Minimum: $100/month
- Maximum: $500,000/month

### Combined self-employed income (2026-02-15)

For self-employed borrowers with multiple businesses, the extractor also computes combined monthly income by summing all eligible P&L net-income candidates.

**Eligibility rules:**
- Pattern must be `PL_NET_INCOME` (net income only, not total income)
- Distinct `file_relpath` (no duplicate files)
- Same `period_months` as the primary winner (within 0.1 tolerance)

**Output fields in `income_analysis.json`:**
```json
"monthly_income_total_combined": {
  "value": 21398.21,
  "citations": [...],
  "source": "P&L",
  "components": [
    {"file_relpath": "...", "period_total": 112720.37, "period_months": 9.0, "monthly_equivalent": 12524.49, "chunk_id": "..."},
    {"file_relpath": "...", "period_total": 79863.50, "period_months": 9.0, "monthly_equivalent": 8873.72, "chunk_id": "..."}
  ]
}
```

**Output fields in `dti.json`:**
- `monthly_income_combined`: combined monthly income total
- `front_end_dti_combined`: housing / combined income
- `back_end_dti_combined`: (housing + debt) / combined income

All existing primary fields (`monthly_income_total`, `front_end_dti`, `back_end_dti`) remain unchanged for backward compatibility.

### DTI integration

- Deterministic `monthly_income_total.value` takes priority over LLM income_items summation
- `_compute_dti()` checks `monthly_income_total` first; falls back to individual items if not available
- `_compute_dti()` also computes combined DTI ratios from `monthly_income_combined` when available
- Confidence re-calibrated after deterministic extractors: both income+PITIA → 0.7, one of them → 0.5

### Retrieval support

- `INCOME_RETRIEVE_QUERY` enhanced with: `Gross Monthly Income`, `Base Employment Income`, `qualifying income`, `Total Monthly Income`, `Desktop Underwriter`, `DU Findings`, `Profit and Loss`, `Net Income`, `Total Income`
- `--required-keywords "Profit and Loss"` (second group) force-includes P&L chunks via Step13 multi-group injection

---

## Step12 Profile: uw_decision (2026-02-15, v0.7)

Deterministic underwriting decision simulation. When `--analysis-profile uw_decision` is used, Step12 reads the already-computed `income_analysis.json` + `dti.json` from the `income_analysis` profile and applies configurable DTI threshold rules. No LLM call is needed — purely deterministic.

### Prerequisites

The `income_analysis` profile must have run first for the same `run_id`. The uw_decision handler reads from `outputs/profiles/income_analysis/income_analysis.json` and `outputs/profiles/income_analysis/dti.json`.

### Profile-aware rerun preservation

When running `uw_decision` as a separate invocation (not in the same command as `income_analysis`), the rerun-wipe logic preserves profiles not being overwritten. Before wiping the final directory, non-current profiles are copied from `final` into `staging` via `shutil.copytree`. This ensures `income_analysis` outputs survive when only `uw_decision` runs.

### Configurable policy thresholds (v0.7)

Thresholds are loaded from a per-tenant JSON policy file. If the file is missing or invalid, hardcoded defaults are used.

**Policy file location:**
```
/mnt/nas_apps/nas_analyze/tenants/{tenant_id}/policy/uw_thresholds.json
```

**Schema (v0.1):**
```json
{
  "policy_version": "v0.1",
  "program": "Conventional",
  "thresholds": {
    "max_back_end_dti": 0.45,
    "max_front_end_dti": null
  }
}
```

**Defaults (when no file exists):** program="Conventional", max_back_end_dti=0.45, max_front_end_dti=null.

**Template:** `policy_templates/uw_thresholds.example.json` — copy to the tenant policy path to customize.

### Decision rules (v0.7-policy)

| Rule | Threshold | Outcome |
|------|-----------|---------|
| `back_end_dti <= policy.max_back_end_dti` | configurable (default 45%) | PASS |
| `back_end_dti > policy.max_back_end_dti` | configurable (default 45%) | FAIL |
| `back_end_dti` is null | — | UNKNOWN |

Applied independently for both primary and combined (if available) scenarios.

### Output files

Written to `outputs/profiles/uw_decision/`:

- **decision.json** — Full decision contract:
  ```json
  {
    "profile": "uw_decision",
    "tenant_id": "...", "loan_id": "...", "run_id": "...",
    "ruleset": {
      "program": "Conventional",
      "version": "v0.7-policy",
      "thresholds": {"max_back_end_dti": 0.45, "max_front_end_dti": null},
      "policy_source": "file|default",
      "policy_version": "v0.1"
    },
    "inputs": {
      "pitia": 3203.31, "liabilities_monthly": 582.00,
      "income_monthly_primary": 12524.49,
      "income_monthly_combined": 21398.21,
      "dti_primary": {"front_end": 0.2558, "back_end": 0.3022},
      "dti_combined": {"front_end": 0.1497, "back_end": 0.1769}
    },
    "decision_primary": {
      "status": "PASS",
      "reasons": [{"rule": "DTI_BACK_END_MAX", "status": "PASS", "value": 0.3022, "threshold": 0.45}],
      "missing_inputs": []
    },
    "decision_combined": { "status": "PASS", ... },
    "citations": {
      "pitia": [...], "liabilities": [...],
      "income_primary": [...], "income_combined": [...]
    },
    "confidence": 0.9
  }
  ```
- **decision.md** — Human-readable markdown with:
  - Header table (tenant, loan, run_id, generated timestamp)
  - Program, ruleset version, policy source, threshold
  - Primary/combined decisions with DTI values
  - Inputs snapshot (PITIA, liabilities, income, DTI ratios)
  - Evidence/citations section (chunk_id + quote for each input category)
- **answer.json / answer.md** — Standard profile outputs with synthesized answer text
- **citations.jsonl** — Flattened citation list from income_analysis sources

Written to `outputs/_meta/`:

- **version.json** — Audit metadata:
  ```json
  {
    "generated_at_utc": "2026-02-15T12:34:56Z",
    "git": {"commit": "<hash>", "dirty": false},
    "schemas": {"uw_decision": "v0.7"},
    "policy": {
      "policy_version": "v0.1",
      "policy_source": "file|default",
      "path": "/mnt/nas_apps/.../uw_thresholds.json"
    }
  }
  ```
  Git operations fail gracefully (commit=null, dirty=null if git unavailable).

### Confidence calibration

- Primary decision is PASS/FAIL (known status) → 0.9
- Primary decision is UNKNOWN → 0.3

### Smoke test

```bash
RUN_UW_DECISION=1 RUN_ID=2026-02-13T073441Z bash scripts/run_regression_smoke.sh

# Or with income_analysis + uw_decision together:
RUN_INCOME_ANALYSIS=1 RUN_UW_DECISION=1 EXPECT_DTI=1 RUN_ID=2026-02-13T073441Z bash scripts/run_regression_smoke.sh
```

### Configurable env vars

| Variable | Default | Description |
|----------|---------|-------------|
| `RUN_UW_DECISION` | `0` | Set to `1` to run uw_decision profile |
| `UW_DECISION_QUERY` | `Deterministic underwriting decision...` | Query text (ignored by deterministic handler) |

### Assertions

- `decision.json` exists with valid schema
- `decision_primary.status` ∈ {PASS, FAIL, UNKNOWN}
- `decision_combined.status` ∈ {PASS, FAIL, UNKNOWN} (if present)
- `ruleset.version` = `v0.7-policy`
- `ruleset.policy_source` ∈ {file, default}
- `citations` has `pitia`, `liabilities`, `income_primary` keys
- `answer.json`, `answer.md`, `decision.md` exist
- `outputs/_meta/version.json` exists

---

## Loan API (FastAPI) — 2026-02-17

Minimal local-only HTTP service for loan analysis: list tenants/loans/runs, sync query, background pipeline jobs, background query jobs, artifact index and downloads. Optional API key and tenant allowlist for security.

### Entry point

- **scripts/loan_api.py** — Standalone FastAPI app with in-memory job registry. No cloud; no Redis/DB/Celery. Jobs lost on restart.
- Run: `python3 scripts/loan_api.py --host 127.0.0.1 --port 8000` (use `--host 0.0.0.0` for LAN access, e.g. from Windows to 10.10.10.190:8000).

### Endpoints (unchanged paths and success response shapes)

| Method | Path | Description |
|--------|------|--------------|
| GET | `/` | Service info and links |
| GET | `/health` | `{"status":"ok"}` |
| GET | `/browse/source` | List subfolders under allowed source base (query `?base=`) |
| GET | `/tenants/{tenant_id}/loans` | List loan IDs (from nas_analyze) |
| GET | `/tenants/{tenant_id}/source_loans` | **Source-of-truth:** list loan folders with last_processed, needs_reprocess |
| GET | `/tenants/{tenant_id}/source_loans/{loan_id}` | **Source-of-truth:** get source_path + metadata for one loan |
| POST | `/tenants/{tenant_id}/loans/{loan_id}/runs` | Start run (fire-and-forget) |
| GET | `/tenants/{tenant_id}/loans/{loan_id}/runs` | List run IDs |
| GET | `/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}` | Run status (job_manifest.json) |
| POST | `/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/query` | **Sync** query (Step13 + Step12, returns answer JSON) |
| POST | `/tenants/{tenant_id}/loans/{loan_id}/jobs` | Submit **pipeline** job → 202 + job_id |
| POST | `/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/query_jobs` | Submit **query** job → 202 + job_id |
| GET | `/jobs/{job_id}` | Job status (PENDING/RUNNING/SUCCESS/FAIL) |
| GET | `/jobs` | List jobs (optional ?status=, ?limit=) |

### Artifacts index and downloads

- **GET** `/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/artifacts` — Deterministic JSON index of what exists on disk: `base_dir`, `retrieval_pack` (path, sha256, exists), `job_manifest` (path, exists, status), `profiles[]` (name, dir, files[] with name/path/exists/size_bytes/mtime_utc). Profiles and file lists are sorted for determinism.
- **GET** `/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/artifacts/{profile}/{filename}` — Download a profile artifact. `filename` must be in allow-list: `answer.json`, `answer.md`, `citations.jsonl`, `income_analysis.json`, `dti.json`, `decision.json`, `decision.md`, `version.json`. Returns 404 "Run not found" or "Artifact not found"; path traversal blocked.
- **GET** `/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/retrieval_pack` — Download `retrieval_pack.json`.
- **GET** `/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/job_manifest` — Download `job_manifest.json`.

### Background jobs

- **Pipeline jobs** (POST `.../jobs`): Body includes `run_id`, `skip_intake`, `skip_process`, `offline_embeddings`, `top_k`, `max_per_file`, `max_dropped_chunks`, `expect_rp_hash_stable`, `smoke_debug`, `llm_model`, `timeout`. A daemon thread runs `run_loan_job.py` with matching CLI flags. Idempotency: same (tenant_id, loan_id, request body) returns same job_id when status is PENDING/RUNNING/SUCCESS.

**Progress phases (2026-02-21):** Job `stdout` includes deterministic phase markers so a desktop client can show progress. Format: one line per transition, `PHASE:<NAME> <UTC_ISO_Z>`. Phase names (in order): `INTAKE`, `PROCESS`, `STEP13_GENERAL`, `STEP13_INCOME`, `STEP12_INCOME_ANALYSIS`, `STEP12_UW_DECISION`, `DONE` (success), `FAIL` (on failure). Emitted by `run_loan_job.py` during the pipeline; workers append `PHASE:FAIL` when the job fails from timeout or exception (no subprocess stdout). Manifest short-circuit emits `PHASE:DONE` only. No new API keys; progress is observable via existing `stdout` (and `stderr`/`error`/`result`).

**Streaming job stdout (2026-02-23):** Pipeline jobs now stream subprocess stdout into `JOBS[job_id]["stdout"]` as lines are produced, so the Web UI progress stepper can show live phase colors (pending / done / current / fail) during the run instead of only after completion.
- **Query jobs** (POST `.../runs/{run_id}/query_jobs`): Body matches sync query: `question`, `profile`, `llm_model`, `offline_embeddings`, `top_k`, `max_per_file`. Profile must be one of: `default`, `uw_conditions`, `income_analysis`, `uw_decision`. Runner executes Step13 then Step12 (one overall timeout split between steps). Success = Step12 returncode 0. No manifest short-circuit; result has `outputs_base` and `status` only. Fetch answer via artifact endpoint: `.../artifacts/{profile}/answer.json`.

### run_loan_job.py execution options (used by API/worker)

When the API or a job worker runs `run_loan_job.py`, the following flags are supported and propagated:

- `--run-llm` / `--no-run-llm` — Default: run LLM. With `--no-run-llm`, Step12 receives `RUN_LLM=0` (deterministic-only).
- `--expect-rp-hash-stable` — Rerun general Step13 and fail if retrieval_pack.json sha256 changes.
- `--max-dropped-chunks N` — Fail if income-focused Step13 reports `dropped_chunk_ids_count` > N.
- `--debug` — Pass debug to steps (smoke_debug).
- `--offline-embeddings` — Pass to Step13.
- `--top-k N`, `--max-per-file N` — Override Step13 defaults (80/120 and 12 for income).

Short-circuit: if `--run-id` is supplied and `job_manifest.json` already exists with status SUCCESS, the script exits 0 without running the pipeline. Manifest includes `options`: `run_llm`, `expect_rp_hash_stable`, `max_dropped_chunks`, `smoke_debug`, `offline_embeddings`, `top_k`, `max_per_file`.

### API security (optional)

Applied via middleware in `loan_api.py` (no new dependencies; uses Starlette).

- **API key:** Env `MORTGAGEDOCAI_API_KEY`. If **set and non-empty**, every request must include header `X-API-Key` with the exact value; otherwise **401** with body `{"detail":"Unauthorized"}`. If unset or empty, all requests are allowed (dev mode). Applies to all routes including `/docs` and `/openapi.json`.
- **Tenant allowlist:** Env `MORTGAGEDOCAI_ALLOWED_TENANTS` — comma-separated list (e.g. `peak,acme`). If **set and non-empty**, any path with `{tenant_id}` (e.g. `/tenants/peak/loans`) must have `tenant_id` in the list; otherwise **404** with body `{"detail":"Not Found"}` (not 403, to avoid leaking that the tenant is forbidden). If unset or empty, any tenant is allowed.

### Network Security Architecture (2026-02-25)

The API is **Tailscale-only**: it binds exclusively to the server's Tailscale IPv4 address and is unreachable from the LAN or WAN.

| Layer | Detail |
|---|---|
| **Bind address** | Tailscale IPv4 only (`--host <tailscale-ip>` e.g. `100.80.98.97`). Not `0.0.0.0`, not `127.0.0.1`, not any LAN interface. |
| **LAN exposure** | None. Port 8000 is not reachable on the local LAN. |
| **WAN exposure** | None. No port-forwarding, no public domain, no reverse-proxy to the internet. |
| **Encrypted transport** | Tailscale (WireGuard-based). All client↔server traffic is end-to-end encrypted by the tailnet. No additional TLS/HTTPS layer is needed or used. |
| **ACL enforcement** | Tailscale policy restricts port 8000 to devices with tag `tag:mortgagedocai`. Non-tagged tailnet peers cannot reach the API. |
| **API key auth** | `MORTGAGEDOCAI_API_KEY` env var (see §"API security" above). Provides a second authentication layer independent of Tailscale ACL. |
| **Caddy / reverse proxy** | **Not used.** Removed from the default deployment path. Caddy HTTPS-over-loopback was the prior model; it is superseded by Tailscale encrypted transport. |
| **UFW / nftables** | UFW disabled on the AI server. nftables managed by system defaults. Port 8000 is inaccessible from outside Tailscale regardless. |

**Do not expose port 8000 on WAN.** Do not bind to `0.0.0.0`. Tailscale provides the only network path to the API.

### Alternative: disk-backed job service (loan_service)

If the app is wired to use `loan_service` (JobService, DiskJobStore, SubprocessRunner) instead of the in-memory loan_api.py implementation, the same endpoint paths and response shapes apply. Query jobs and pipeline jobs are executed by `adapters_subprocess.SubprocessRunner` (query jobs: Step13 + Step12; pipeline jobs: `run_loan_job.py`). Manifest short-circuit and tenant/request idempotency are handled in `service.py`.

---

## Source-of-truth loan list and Web UI (2026-02-23)

The Web UI and API now use a **source-of-truth** view of loan folders on the read-only NAS mount, with **needs_reprocess** derived from source mtime vs last run.

### Config (env)

- **MORTGAGEDOCAI_SOURCE_LOANS_ROOT** — Root directory for original loan folders (default: `/mnt/source_loans`). If missing, new source_loans endpoints return 500 "Source loans root not mounted".
- **MORTGAGEDOCAI_SOURCE_LOANS_CATEGORIES** — Comma-separated subfolder names under the root to scan (default: `5-Borrowers TBD`). Avoids listing the root so Synology `#recycle` (permission denied) is never touched.

### API (additive)

- **GET /tenants/{tenant_id}/source_loans** — Enumerates loan folders under SOURCE_LOANS_ROOT (2 levels: category → loan folder). Loan folders detected by name pattern `[Loan 12345]` or Loan + digits; loan_id extracted by regex. Response: `source_root`, `items[]` with `loan_id`, `folder_name`, `source_path`, `source_last_modified_utc`, `last_processed_run_id`, `last_processed_utc`, `needs_reprocess`. Sorted: needs_reprocess desc, source_last_modified_utc desc, loan_id asc.
- **GET /tenants/{tenant_id}/source_loans/{loan_id}** — Single loan: `loan_id`, `source_path`, `source_last_modified_utc`. 404 "Source loan not found" if not in list.

Last processed comes from `nas_analyze/tenants/{tenant}/loans/{loan_id}/` run dirs matching `YYYY-MM-DDTHHMMSSZ`; newest run_id used. `needs_reprocess` = true when no run or `source_last_modified_utc` > `last_processed_utc`.

### Web UI (scripts/webui)

- **Refresh Loans** calls GET `/tenants/{tenant}/source_loans` (no longer `/tenants/{tenant}/loans`). List shows loan ID, folder name, source last modified, last processed (or "Never"), and badge **Needs Processing** or **Up to date**.
- Selecting a loan sets **source_path** from the item and shows last processed; **Process Loan** is disabled until a loan is selected.
- **Progress stepper:** Pending (gray), Done (green), Current (accent), Failed (red). Job **stdout** is streamed so PHASE lines appear during the run and the stepper updates live.

### Query path and SOURCE_MOUNT

- **preflight_mount_contract(skip_source_check=True)** — When step12 is run with `--query` (e.g. API "Ask a question"), the SOURCE_MOUNT check is skipped so autofs "backing mount not materialized" does not block query-only runs (which read only from nas_analyze). lib.py and step12_analyze.py updated accordingly.

---

## How to run production (v0.7)

### Preconditions

- Python venv activated: `source /opt/mortgagedocai/venv/bin/activate`
- Ollama running: `systemctl status ollama` (models: mistral, phi3)
- Qdrant running: `systemctl status qdrant` (http://localhost:6333)
- NAS source loans mounted read-only at `/mnt/source_loans`

### One-shot pipeline run

```bash
python3 scripts/run_loan_pipeline.py \
  --tenant-id peak \
  --loan-id 16271681 \
  --source-path "/mnt/source_loans/5-Borrowers TBD/Walters, Bill [Loan 16271681]" \
  --qdrant-url http://localhost:6333 \
  --embedding-device cpu
```

### Run income_analysis + uw_decision

```bash
RUN_INCOME_ANALYSIS=1 RUN_UW_DECISION=1 EXPECT_DTI=1 \
  RUN_ID=<RUN_ID> bash scripts/run_regression_smoke.sh
```

Replace `<RUN_ID>` with the run_id from the pipeline output (e.g. `2026-02-13T073441Z`).

### Enable per-tenant policy thresholds

```bash
mkdir -p /mnt/nas_apps/nas_analyze/tenants/<tenant>/policy
cp /opt/mortgagedocai/policy_templates/uw_thresholds.example.json \
   /mnt/nas_apps/nas_analyze/tenants/<tenant>/policy/uw_thresholds.json
# Edit thresholds as needed; policy_source will report "file" when loaded.
```

### Output locations

| Output | Path |
|--------|------|
| income_analysis | `outputs/profiles/income_analysis/` |
| uw_decision | `outputs/profiles/uw_decision/decision.json` + `decision.md` |
| version stamp | `outputs/_meta/version.json` |
| retrieval pack | `nas_analyze/.../retrieve/<run_id>/retrieval_pack.json` |
