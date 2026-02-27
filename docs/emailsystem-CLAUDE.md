# Knowledge Server (Tier-2) — Claude Context

**Project:** Email Tracking Knowledge Server — on-prem appliance, Tier-2 service
**Role in system:** Tier-2 handles document indexing + semantic search only. Tier-1 (the email agent) calls this service to retrieve relevant client context before drafting replies.
**This file lives in:** `emailsystem/CLAUDE.md` (copy here when repo is created)

---

## What This Project Is

A FastAPI service that:
1. Scans a Synology-mounted folder tree (`/mnt/synology/<SharedFolder>/<RealtorFolder>/<ClientFolder>/`)
2. Extracts text from PDFs, DOCX, TXT/MD
3. Chunks + embeds documents via **OpenAI API** (not local GPU)
4. Stores everything in **PostgreSQL + pgvector** (not Qdrant)
5. Exposes `/v1/knowledge/search` for semantic retrieval by Tier-1

It does **NOT** draft emails, call LLMs for answers, or modify source files. It is purely an index + search layer.

---

## Relationship to the Mortgage Project

**The mortgage project (`mortgagedocai`) is the direct ancestor of this codebase.** Many patterns, algorithms, and hard-won lessons are shared. Do not reinvent what is already solved.

### What to reuse / port directly

| Mortgage component | Where it lives | What to reuse |
|--------------------|---------------|---------------|
| `sha256_file()` | `lib.py` | Identical — file fingerprinting for change detection |
| `normalize_chunk_text()` | `lib.py` | Identical — copy into `core/hashing.py` |
| `chunk_text_hash()` | `lib.py` | Identical concept — used for `chunk_sha256` |
| PDF extraction (pypdf + fallback) | `step11_process.py` | Same pypdf first / pdfminer fallback pattern |
| DOCX extraction (python-docx) | `step11_process.py` | Identical |
| XLSX extraction (openpyxl) | `step11_process.py` | Identical |
| Two-pass chunking algorithm | `step11_process.py` | Same logic, different params (3500/400 vs 4500/800) |
| FastAPI X-API-Key auth | `loan_api.py` | Same env-var-driven key check, different header name |
| Job status pattern (queued/running/done/failed) | `loan_service/domain.py` | Maps to `ingestion_jobs` table |
| Fail-loud / raise on contract violation | `lib.py:ContractError` | Same discipline — raise, never silently skip |
| Folder scanner pattern | `step10_intake.py` | Same concept — walk folder tree, fingerprint files |

### What is fundamentally different

| Concern | Mortgage | Email System |
|---------|----------|-----------------|
| Vector storage | Qdrant (local, file-based) | PostgreSQL + pgvector |
| Embedding model | E5-large-v2 (local, no API) | OpenAI `text-embedding-3-small` (cloud) |
| Embed dimensions | 1024 | 1536 |
| ORM / migrations | Raw dicts + JSONL files | SQLAlchemy 2.x + Alembic |
| Identity model | `tenant_id` + `loan_id` | `tenant_id` + `workspace_id` + `client_id` |
| Incremental indexing | Full reprocess each run | `mtime` + `content_sha256` change detection |
| Deployment | Bare Python scripts + systemd | Docker Compose |
| Config | argparse + hardcoded paths | Pydantic v2 Settings + `.env` |
| Job persistence | Disk JSON files | `ingestion_jobs` table in Postgres |
| LLM answering | Ollama (local) | None — Tier-2 is search only |
| Chunking params | target=4500 / overlap=800 | target=3500 / overlap=400 |

---

## Why Both Projects Share the Same Synology Hardware

The mortgage project reads loan documents from:
```
/mnt/source_loans/5-Borrowers TBD/        ← Synology mount, read-only
```

This knowledge server reads realtor/client documents from:
```
/mnt/synology/<SharedFolder>/<RealtorFolder>/<ClientFolder>/   ← same Synology, different path, read-only
```

**They coexist safely because:**
- Both treat the Synology as **read-only** — neither modifies source files
- They use completely different mount paths and folder structures
- They write to different output destinations (mortgage → NAS TrueNAS; knowledge server → Postgres)
- Different tenants, different Qdrant collections vs pgvector tables
- The same AI server hardware runs both workloads

**Why share hardware instead of separate boxes:**
- One NAS investment serves both use cases
- The AI server already has the network path to Synology established
- Docker Compose isolates the knowledge server cleanly alongside the mortgage pipeline
- Future integration (e.g. realtor checking mortgage conditions) becomes straightforward

---

## Identity Model

```
tenant_id    → the brokerage / company (top-level isolation)
workspace_id → the realtor (one workspace per realtor folder)
client_id    → the client (one client per subfolder under realtor)
```

Folder mapping:
```
/mnt/synology/<SharedFolder>/              ← tenant root (SYNOLOGY_ROOT env var)
  <RealtorFolder>/                         → workspace (name + root_path stored in workspaces table)
    <ClientFolder>/                        → client (folder_name + folder_path in clients table)
      **/<docs>                            → documents (all files recursively)
```

**Every query MUST filter by all three:** `tenant_id + workspace_id + client_id`. Non-negotiable — no exceptions.

---

## Tech Stack

```
Python 3.11+
FastAPI
SQLAlchemy 2.x (sync for MVP)
Alembic (all schema changes via migrations only — no manual DDL in prod)
Pydantic v2 (Settings class for all config)
PostgreSQL 15+ with pgvector extension
pgvector/pgvector Docker image (or install extension manually)
OpenAI Python SDK (embeddings only — no chat/completion calls in Tier-2)
pypdf + pdfminer.six (PDF extraction)
python-docx (DOCX extraction)
Docker Compose (deployment unit)
```

---

## Folder Structure

```
emailsystem/
  CLAUDE.md                     ← this file
  README.md
  .env.example
  docker-compose.yml
  services/
    emailsystem-api/
      Dockerfile
      pyproject.toml
      alembic.ini
      alembic/
        env.py
        versions/
      src/app/
        main.py                  ← FastAPI app factory
        config.py                ← Pydantic v2 Settings
        api/v1/
          knowledge.py           ← POST /v1/knowledge/search
          ingest.py              ← POST /v1/ingest/scan + /process_pending
        db/
          session.py             ← SQLAlchemy session dependency
          models.py              ← ORM models (workspaces, clients, documents, chunks, embeddings, ingestion_jobs)
        core/
          scanner.py             ← folder tree walker (port of step10_intake.py concept)
          parser.py              ← PDF/DOCX/TXT extraction (port of step11_process.py)
          chunker.py             ← two-pass chunker (port of step11_process.py)
          embedder.py            ← OpenAI batch embedding
          retrieval.py           ← pgvector cosine search
          hashing.py             ← sha256_file, normalize_chunk_text, chunk_sha256
          paths.py               ← realtor/client folder mapping
          jobs.py                ← ingestion job lifecycle
        util/
          logging.py
          time.py
      tests/
        test_chunker.py
        test_path_mapping.py
        test_retrieval_filters.py
```

---

## Database Schema (key points)

All changes via Alembic migrations. Never alter tables manually in production.

```
workspaces    tenant_id + workspace_id (realtor)
clients       tenant_id + workspace_id + client_id (client folder)
documents     file fingerprint (mtime + content_sha256); parse_status: pending/parsed/failed
chunks        document_id + chunk_index; stores text + char/page offsets + chunk_sha256
embeddings    chunk_id → vector(1536); ivfflat cosine index (lists=100)
ingestion_jobs  job lifecycle: queued → running → done/failed; stats_json JSONB
```

**Incremental indexing rule:** When scanning, compare `mtime` first (fast). If mtime changed, recompute `content_sha256`. Only mark `parse_status = pending` if sha256 actually differs. Unchanged files → skip entirely.

---

## Environment Variables

```bash
DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/knowledge
OPENAI_API_KEY=sk-...
OPENAI_EMBED_MODEL=text-embedding-3-small
EMBED_DIM=1536
SYNOLOGY_ROOT=/mnt/synology/SharedFolder
SERVER_API_KEY=...                    # X-API-Key header for Tier-1 → Tier-2 auth
LOG_LEVEL=INFO
```

**Never hardcode any of these.** All secrets via environment only.

---

## API Endpoints

```
GET  /health                      → {"ok": true}
POST /v1/ingest/scan              → fingerprint all files, enqueue pending (auth required)
POST /v1/ingest/process_pending   → parse + chunk + embed pending docs (auth required)
POST /v1/knowledge/search         → semantic search with tenant/workspace/client filter (auth required)
```

Auth: `X-API-Key` header must match `SERVER_API_KEY` env var. FastAPI dependency injection pattern (not middleware — cleaner than mortgage's BaseHTTPMiddleware approach).

---

## Non-Negotiables

- **Tenant isolation is absolute:** every DB query filters `tenant_id + workspace_id + client_id` — no exceptions
- **Synology mount is read-only:** never write to source files
- **OpenAI API key never hardcoded** — env var only
- **All schema changes via Alembic migrations** — no manual DDL in production
- **Incremental indexing must be correct** — do not reprocess files whose sha256 hasn't changed
- **Docker Compose is the deployment unit** — no bare-script deployment for this project

---

## What's Next (Build Order)

1. **Scaffold:** Docker Compose (Postgres + pgvector + emailsystem-api), Alembic migrations, db models
2. **Scanner + fingerprinting:** port `sha256_file()` + folder walker from mortgage `step10_intake.py`
3. **Parser + chunker:** port from mortgage `step11_process.py` (pypdf, python-docx, two-pass chunker)
4. **Embedder:** OpenAI batch embedding (replace E5-large-v2 calls)
5. **Ingest endpoints:** `/v1/ingest/scan` + `/v1/ingest/process_pending`
6. **Retrieval endpoint:** `/v1/knowledge/search` with pgvector cosine similarity
7. **Tests:** chunker determinism, path mapping, retrieval filter isolation

---

## Development Workflow

Same TDD discipline as the mortgage project:

1. **Plan first** — research, pre-flight audit, flag any spec inconsistencies
2. **Red** — write failing tests
3. **Green** — implement until tests pass
4. **Regression** — run full suite
5. **Commit each phase** with semantic messages

**ChatGPT is the System Architect.** Claude is the Implementation Assistant. Verify specs against this CLAUDE.md before implementing — flag hallucinations (e.g. wrong embed dimensions, wrong folder paths).

---

## Common Gotchas

- `EMBED_DIM=1536` for `text-embedding-3-small` (NOT 1024 — that's the mortgage project's E5 model)
- ivfflat index needs ~100K vectors to beat exact scan; for small datasets, exact cosine search is fine and more accurate — don't force the index prematurely
- SQLAlchemy 2.x uses `session.execute(select(...))` not `session.query(...)` — different API
- OpenAI embedding API has rate limits — batch requests (max 2048 inputs per call) and add retry logic
- Tier-2 does NOT call OpenAI chat/completions — search only. LLM answering is Tier-1's job.
- `pgvector/pgvector` Docker image already has the extension; no need to install manually
