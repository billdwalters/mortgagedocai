# EmailSystem — Claude Context

**Project:** EmailSystem — on-prem AI email assistant, turnkey appliance
**This file lives in:** `emailsystem/CLAUDE.md` (copy here when repo is created on server)
**Server:** `/opt/emailsystem` on 10.10.10.12 (Ubuntu mini-PC)

---

## System Context (read this first)

EmailSystem is a **turnkey AI email assistant** sold as a product to small businesses
(Realtors, Salespersons, Lawyers, etc.). It monitors a Gmail inbox, uses AI to understand
and draft intelligent replies, routes emails to the right person, and allows approvals
via Telegram (including voice). An optional paid add-on indexes the client's document
folders for additional context.

### Two services — one repo

```
emailsystem/
  docker-compose.yml       ← runs everything: postgres + emailsystem-api + knowledge-api + n8n
  services/
    emailsystem-api/       ← Tier-1: PRIMARY (port 8000) — always deployed
    knowledge-api/         ← Tier-2: OPTIONAL ADD-ON (port 9000) — paid upgrade only
```

| | Tier-1 `emailsystem-api` | Tier-2 `knowledge-api` |
|---|---|---|
| **Purpose** | Email intake, AI classification, AI drafting, routing, Telegram, Calendar | Document folder indexing + semantic search |
| **Required** | Always | Only if customer pays for add-on |
| **OpenAI usage** | GPT for classification + drafting; embeddings for search | Embeddings only |
| **Database** | Postgres (tenants, mailboxes, threads, messages, audits) | Postgres + pgvector (workspaces, documents, chunks, embeddings) |
| **Port** | 8000 | 9000 |
| **n8n** | Yes — thin trigger + Telegram relay | No |

**Tier-1 works without Tier-2.** When Tier-2 is present, Tier-1 calls
`POST /v1/knowledge/search` before drafting to include relevant client document context.

---

## Non-Negotiables (never break these)

- **Drafting MUST use LLM (OpenAI GPT)** — never keyword templates or hardcoded strings.
  The goal is context-aware replies, NOT "Thank you for reaching out."
- **Thread context MUST be passed to the LLM** — full thread history included when drafting.
  A reply to a follow-up must reference the prior conversation.
- **n8n is thin** — Gmail trigger, Telegram relay only. Zero business logic in n8n.
  All logic lives in `emailsystem-api` so n8n can be removed later with minimal changes.
- **Multi-tenant isolation** — every DB query filters by `tenant_id`. No exceptions.
- **Secrets in env vars only** — never hardcode API keys, OAuth tokens, or passwords.
- **Human approval before sending** — default action is `DRAFT_FOR_APPROVAL`.
  `AUTO_SEND` only when tenant policy explicitly allows it AND confidence is high.
- **Admin GUI is required** — not just API endpoints. Customers need a web UI to manage
  users, mailboxes, routing rules, and settings without touching config files.
- **All schema changes via Alembic migrations** — no manual DDL in production.
- **Fail-loud** — raise errors clearly, never silently degrade or swallow exceptions.

---

## Tier-1: EmailSystem Core (Primary)

### What it does

1. n8n detects new Gmail message → calls `POST /v1/ingest/gmail/message_ref`
2. emailsystem-api fetches full message via Gmail API
3. LLM classifies intent (schedule, pricing, support, spam, general)
4. LLM drafts a context-aware reply using full thread history
   - If Tier-2 is available: retrieves relevant client document context first
5. Policy engine decides action: `DRAFT_FOR_APPROVAL`, `AUTO_SEND`, `ROUTE_ONLY`, `IGNORE`
6. Telegram notification sent to realtor/salesperson with draft
7. User approves, edits, or rejects via Telegram (text or voice)
8. On approval: emailsystem-api sends Gmail reply in-thread with correct headers

### Tech stack

```
Python 3.11+
FastAPI
SQLAlchemy 2.x (sync for MVP)
Alembic (all schema changes via migrations only)
Pydantic v2 Settings
PostgreSQL 15+
OpenAI Python SDK (GPT for drafting + classification; embeddings for thread search)
Google API Python Client (Gmail + Calendar)
python-telegram-bot (Telegram notifications + voice approval)
Docker Compose
```

### Folder structure

```
services/emailsystem-api/
  Dockerfile
  pyproject.toml
  alembic.ini
  alembic/
    env.py
    versions/
  src/app/
    main.py                  ← FastAPI app factory
    config.py                ← Pydantic v2 Settings (all config from env)
    security.py              ← X-API-Key dependency for n8n→API auth
    api/v1/
      ingest.py              ← POST /v1/ingest/gmail/message_ref
      approval.py            ← POST /v1/approval/decision
      gmail_oauth.py         ← POST /v1/admin/mailboxes/upsert + GET
      admin.py               ← admin CRUD (users, routing rules, policies)
      telegram.py            ← POST /v1/telegram/webhook
      calendar.py            ← GET /v1/calendar/availability, POST /schedule
    core/
      gmail_client.py        ← Gmail API wrapper (fetch, send, thread fetch)
      threading.py           ← assemble thread context for LLM prompt
      classifier.py          ← LLM-based intent classification (NOT keyword matching)
      policy.py              ← action decision engine (DRAFT/AUTO_SEND/ROUTE/IGNORE)
      router.py              ← route to user/department by intent + rules
      drafting.py            ← LLM draft generation (GPT, full thread context)
      telegram_client.py     ← send notifications, receive approvals, voice→text
      calendar_client.py     ← Google Calendar availability + event creation
      knowledge_client.py    ← optional HTTP call to Tier-2 /v1/knowledge/search
    db/
      session.py             ← SQLAlchemy session dependency
      models.py              ← all ORM models
    util/
      logging.py
  frontend/                  ← Admin GUI (served by FastAPI at /admin)
  tests/
    test_threading.py
    test_classifier.py
    test_drafting.py
    test_policy.py
```

### Database schema

```
tenants        id, tenant_key (unique), name, industry, created_at
users          id, tenant_id, email, display_name, role,
               telegram_user_id (nullable), calendar_id (nullable)
mailboxes      id, tenant_id, gmail_user (unique per tenant), oauth_json, created_at
threads        id, tenant_id, mailbox_id, provider_thread_id (indexed),
               stage, owner_user_id (nullable), last_message_at
messages       id, tenant_id, thread_id, direction (inbound/outbound),
               provider_message_id, rfc_message_id, headers_json,
               from_email, to_emails, cc_emails, subject, body_text,
               received_at, sent_at
audits         id, tenant_id, thread_id, inbound_message_id,
               intent, confidence, action, draft_subject, draft_body_text,
               approved_at, sent_message_id (nullable), created_at
routing_rules  id, tenant_id, priority, match_json, route_queue,
               assign_user_id (nullable), auto_send_allowed (bool)
policies       id, tenant_id, name, json
```

### Environment variables

```bash
DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/emailsystem
OPENAI_API_KEY=sk-...
OPENAI_CHAT_MODEL=gpt-4o-mini          # GPT model for drafting + classification
OPENAI_EMBED_MODEL=text-embedding-3-small
SERVER_API_KEY=...                      # X-API-Key: n8n → emailsystem-api
TELEGRAM_BOT_TOKEN=...
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
KNOWLEDGE_API_URL=http://knowledge-api:9000   # leave blank to disable Tier-2
LOG_LEVEL=INFO
```

### API endpoints

```
GET  /health
POST /v1/ingest/gmail/message_ref      ← n8n calls this on new Gmail message
POST /v1/approval/decision             ← n8n calls this after Telegram approval
POST /v1/telegram/webhook              ← Telegram calls this for voice/text replies
GET  /v1/calendar/availability         ← check available slots
POST /v1/calendar/schedule             ← create calendar event
POST /v1/admin/mailboxes/upsert        ← store Gmail OAuth token
GET  /v1/admin/mailboxes               ← list mailboxes
GET/POST /v1/admin/users               ← manage users
GET/POST /v1/admin/routing_rules       ← manage routing rules
GET  /admin                            ← Admin GUI (web UI)
```

### Drafting rules (CRITICAL — read carefully)

- **Always use LLM.** `drafting.py` calls OpenAI GPT. No templates, no f-strings as replies.
- **Always pass thread context.** `threading.py` assembles prior messages into the prompt.
- **If Tier-2 is configured:** `knowledge_client.py` retrieves relevant client document
  chunks BEFORE calling the LLM. Include them in the system prompt as context.
- **Prompt must instruct the LLM:**
  - Tone: professional, concise, specific to the thread (not generic)
  - For scheduling: propose specific times (from Calendar API) — do not ask for theirs
  - For support: reference the specific problem from the thread
  - Never start with "Thank you for reaching out" or similar filler openers
- **Classification also uses LLM** — not keyword matching. The classifier prompt asks the
  LLM to return structured JSON: `{intent, confidence, reasoning}`.

---

## Tier-2: Knowledge API (Optional Add-On)

### What it does

Indexes a Synology-mounted folder tree of client documents and exposes a semantic
search endpoint that Tier-1 calls before drafting. Deployed only when the customer
purchases the document context add-on.

### Tech stack

```
Python 3.11+ + FastAPI + SQLAlchemy 2.x + Alembic + Pydantic v2
PostgreSQL 15+ with pgvector extension
OpenAI text-embedding-3-small (EMBED_DIM=1536)
pypdf + pdfminer.six + python-docx
Docker Compose (same compose file as Tier-1)
```

### Folder structure

```
services/knowledge-api/
  Dockerfile
  pyproject.toml
  alembic.ini
  alembic/versions/
  src/app/
    main.py
    config.py
    api/v1/
      knowledge.py           ← POST /v1/knowledge/search
      ingest.py              ← POST /v1/ingest/scan + /process_pending
    db/
      session.py
      models.py              ← workspaces, clients, documents, chunks, embeddings, ingestion_jobs
    core/
      scanner.py             ← Synology folder walker
      parser.py              ← PDF/DOCX extraction
      chunker.py             ← two-pass chunker (target=3500, overlap=400)
      embedder.py            ← OpenAI batch embedding
      retrieval.py           ← pgvector cosine search
      hashing.py             ← sha256_file, normalize_chunk_text
      paths.py               ← realtor/client folder mapping
      jobs.py                ← ingestion job lifecycle
    util/logging.py
  tests/
```

### Identity model

```
tenant_id    → the business (top-level isolation)
workspace_id → the realtor/salesperson (one per folder under Synology root)
client_id    → the client (one per subfolder under workspace)
```

Every search MUST filter by all three. No exceptions.

### Synology folder mapping

```
/mnt/synology/<SharedFolder>/              ← SYNOLOGY_ROOT env var
  <RealtorFolder>/                         → workspace
    <ClientFolder>/                        → client
      **/<docs>                            → documents (recursive)
```

### Environment variables

```bash
DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/knowledge
OPENAI_API_KEY=sk-...
OPENAI_EMBED_MODEL=text-embedding-3-small
EMBED_DIM=1536
SYNOLOGY_ROOT=/mnt/synology/SharedFolder
SERVER_API_KEY=...                         # same key as Tier-1 for simplicity
LOG_LEVEL=INFO
```

### API endpoints

```
GET  /health
POST /v1/ingest/scan              → fingerprint all files, enqueue pending
POST /v1/ingest/process_pending   → parse + chunk + embed pending docs
POST /v1/knowledge/search         → semantic search filtered by tenant/workspace/client
```

---

## Combined Docker Compose

```yaml
# emailsystem/docker-compose.yml
services:
  postgres:          # shared by both services
  emailsystem-api:   # Tier-1, port 8000, always on
  knowledge-api:     # Tier-2, port 9000, comment out if not purchased
  n8n:               # port 5678, thin orchestrator
```

Tier-2 can be disabled by commenting out the `knowledge-api` service block.
Set `KNOWLEDGE_API_URL=` (blank) in Tier-1's env to disable the integration.

---

## Industry Template Design

The system is designed to be resold across industries. `tenant.industry` field
drives industry-specific behaviour. When adding industry support:

- Routing rules are tenant-configurable via admin UI (not hardcoded)
- Draft tone/style prompt is configurable per tenant via `policies` table
- Folder structure for Tier-2 maps to whatever the industry uses for client docs
- Do NOT hardcode "Realtor" or "real estate" terminology into core logic

Current target industries: Realtors, Salespersons, Lawyers (future).

---

## Build Phases

### Phase 1 — Core email loop (build first)
Gmail OAuth → message ingestion → LLM classification → LLM draft →
approval workflow → Gmail reply send → Telegram notification

### Phase 2 — Telegram + Calendar
Voice approval via Telegram (speech-to-text → confirm → send)
Google Calendar availability check + appointment scheduling

### Phase 3 — Admin GUI
Web UI at `/admin` for managing users, mailboxes, routing rules, policies.
FastAPI serves static files or a lightweight frontend.

### Phase 4 — Tier-2 Knowledge Add-On
Synology document indexing, pgvector search, Tier-1 integration.
Only build when Tier-1 is stable and a customer wants the add-on.

### Phase 5 — Hardening + multi-industry
Regression tests, audit trail, industry template abstraction.

---

## Reference Codebase (Mortgage Project)

The mortgage project (`mortgagedocai`) is a hardened Python/FastAPI system on the
same hardware. Its defensive patterns are battle-tested and should be inherited.

**Mount:** `/mnt/mortgagedocai` (NFS read-only from 10.10.10.190:/opt/mortgagedocai)
**Do not modify anything under this path.**

| Building this... | Read this first | What to inherit |
|---|---|---|
| Any error handling | `/mnt/mortgagedocai/scripts/lib.py` | `ContractError` — fail-loud, never silently degrade |
| Any DB/file write | `/mnt/mortgagedocai/scripts/lib.py` | `atomic_write_json()` — stage then rename, never write in place |
| `knowledge-api/core/hashing.py` | `/mnt/mortgagedocai/scripts/lib.py` | `sha256_file()`, `normalize_chunk_text()` |
| `knowledge-api/core/parser.py` | `/mnt/mortgagedocai/scripts/step11_process.py` | pypdf → pdfminer fallback, encrypted PDF safe-skip |
| `knowledge-api/core/chunker.py` | `/mnt/mortgagedocai/scripts/step11_process.py` | Two-pass chunking, overlap edge cases |
| `knowledge-api/core/scanner.py` | `/mnt/mortgagedocai/scripts/step10_intake.py` | Folder walk, fingerprint-first, fail-loud on bad paths |
| `knowledge-api/core/jobs.py` | `/mnt/mortgagedocai/scripts/loan_service/domain.py` | Job status model, atomic state transitions |
| NAS folder scanning | `/mnt/mortgagedocai/scripts/step13_build_retrieval_pack.py` | Use `glob("*/pattern")` on NAS — never `iterdir()+is_dir()` |

**Instruction for Claude:** Read the referenced file before implementing each component.
Port defensive patterns (atomic writes, fail-loud, encrypted-PDF safe-skip, first-wins
dedup, NAS-reliable globbing). Do NOT copy mortgage-specific logic (Qdrant, run_id,
PHASE markers, UW conditions, Ollama).

---

## Development Workflow

Same TDD discipline as the mortgage project:

1. **Plan first** — research, audit spec against this CLAUDE.md, flag inconsistencies
2. **Red** — write failing tests
3. **Green** — implement until tests pass
4. **Regression** — run full suite
5. **Commit each phase:** `test(area): ...`, `fix(area): ...`, `feat(area): ...`

**ChatGPT is the System Architect.** Claude is the Implementation Assistant.
Before implementing any spec from ChatGPT, verify it against this CLAUDE.md.

### Known ChatGPT spec gaps to watch for

- ChatGPT's Tier-1 spec uses **keyword/template drafting** — this is WRONG.
  Drafting MUST use OpenAI GPT. Flag and fix before implementing `drafting.py`.
- ChatGPT's Tier-1 spec has NO `OPENAI_API_KEY` in env vars — add it.
- ChatGPT deferred Google Calendar — it is in scope (Phase 2).
- ChatGPT deferred Telegram voice — it is in scope (Phase 2).
- ChatGPT called the Tier-2 service `emailsystem-api` — wrong name. It is `knowledge-api`.
- `EMBED_DIM=1536` for `text-embedding-3-small` — NOT 1024 (that's the mortgage project).
- SQLAlchemy 2.x: use `session.execute(select(...))` — NOT `session.query(...)`.

---

## Common Gotchas

- Gmail send requires `threadId` + correct `In-Reply-To` + `References` headers —
  without these, Gmail creates a new thread instead of replying in-thread.
- Telegram voice messages are `.ogg` files — must be transcribed via OpenAI Whisper
  before passing to the approval workflow.
- Google Calendar OAuth is per-user (not per-tenant) — each user authenticates separately.
- OpenAI rate limits: batch embedding requests (max 2048 inputs), add retry with backoff.
- pgvector ivfflat index needs ~100K vectors to beat exact scan — use exact cosine for MVP.
- n8n workflows must contain ZERO business logic — if you find yourself adding conditions
  or transformations in n8n, move that logic to emailsystem-api instead.
- Admin GUI: serve from FastAPI using `StaticFiles` mount — no separate frontend server needed.
