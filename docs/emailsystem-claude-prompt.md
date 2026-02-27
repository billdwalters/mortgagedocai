# EmailSystem — Claude Implementation Prompt
# Give this entire file contents to Claude on the new server as the starting prompt.
# Located at: /mnt/mortgagedocai/docs/emailsystem-claude-prompt.md

---

You are generating a production-grade monorepo named:
emailsystem/

Before writing any code, read the following files in full:
1. /opt/emailsystem/CLAUDE.md  (project context and non-negotiables)
2. /mnt/mortgagedocai/scripts/lib.py  (inherit: ContractError, fail-loud, atomic writes)
3. /mnt/mortgagedocai/scripts/loan_service/domain.py  (inherit: job status model pattern)
4. /mnt/mortgagedocai/scripts/step11_process.py  (inherit: PDF/DOCX parsing, chunking)

This repository contains TWO FastAPI services deployed together in ONE docker-compose.yml.

═══════════════════════════════════════
NON-NEGOTIABLE NAMING
═══════════════════════════════════════
Repo root: emailsystem/
Tier-1 service: services/emailsystem-api  (container name: emailsystem-api, port 8000)
Tier-2 service: services/knowledge-api    (container name: knowledge-api, port 9000)
Deployment folder on server: /opt/emailsystem
Both services must be started in ONE docker-compose.yml at repo root.

═══════════════════════════════════════
SCOPE OF THIS BUILD
═══════════════════════════════════════
Build ONLY what is specified below.
Do NOT create stub files, placeholder endpoints, or empty modules for:
- Telegram integration (Phase 2 — not yet)
- Google Calendar integration (Phase 2 — not yet)
- Admin GUI / frontend (Phase 3 — not yet)
If a feature is not in this spec, do not create a file for it at all.
Better to have no file than an empty stub.

═══════════════════════════════════════
ARCHITECTURE OVERVIEW
═══════════════════════════════════════
Tier-1: emailsystem-api (PRIMARY PRODUCT)
- Gmail OAuth ingestion
- Thread tracking
- Message storage
- Audit log
- Intent classification via OpenAI GPT (structured for future upgrade)
- Routing
- OpenAI GPT drafting (MANDATORY — never templates)
- Knowledge integration (always wired, gracefully no-ops if unconfigured)
- n8n orchestration integration

Tier-2: knowledge-api (OPTIONAL UPGRADE)
- Synology scan (read-only)
- Document parsing (PDF, DOCX, TXT)
- Chunking
- Embeddings via OpenAI
- pgvector similarity search
- Strict tenant/workspace/client filtering
- Ingestion job tracking

Tier-2 is ALWAYS deployed but becomes "inactive" if SYNOLOGY_ROOT is blank.
Tier-1 ALWAYS calls knowledge_client before drafting.
If KNOWLEDGE_API_URL is blank, knowledge_client returns [] silently — no error.

═══════════════════════════════════════
ENVIRONMENT VARIABLES (top-level .env.example)
═══════════════════════════════════════
# Shared
SERVER_API_KEY=
LOG_LEVEL=INFO

# Tier-1
CORE_DATABASE_URL=postgresql+psycopg2://user:pass@postgres:5432/emailsystem_core
OPENAI_API_KEY=
OPENAI_CHAT_MODEL=gpt-4o-mini
KNOWLEDGE_API_URL=http://knowledge-api:9000
KNOWLEDGE_API_KEY=
GMAIL_FETCH_FORMAT=full

# Tier-2
KNOWLEDGE_DATABASE_URL=postgresql+psycopg2://user:pass@postgres:5432/emailsystem_knowledge
SYNOLOGY_ROOT=
OPENAI_EMBED_MODEL=text-embedding-3-small
EMBED_DIM=1536

═══════════════════════════════════════
TIER-1 (emailsystem-api) REQUIREMENTS
═══════════════════════════════════════

══════════════════
Database Schema (Alembic)
══════════════════
tables:

tenants(
  id UUID PK,
  tenant_key TEXT UNIQUE NOT NULL,
  name TEXT,
  industry TEXT DEFAULT 'general',
  created_at TIMESTAMPTZ DEFAULT now()
)

users(
  id UUID PK,
  tenant_id UUID FK tenants(id),
  email TEXT,
  display_name TEXT,
  role TEXT,
  telegram_user_id TEXT NULL,
  calendar_id TEXT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
)

mailboxes(
  id UUID PK,
  tenant_id UUID FK tenants(id),
  gmail_user TEXT,
  oauth_json JSONB,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(tenant_id, gmail_user)
)

threads(
  id UUID PK,
  tenant_id UUID FK tenants(id),
  mailbox_id UUID FK mailboxes(id),
  provider TEXT DEFAULT 'gmail',
  provider_thread_id TEXT,
  stage TEXT DEFAULT 'new',
  owner_user_id UUID NULL,
  last_message_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now()
)
INDEX on threads(provider_thread_id)
INDEX on threads(tenant_id, provider_thread_id)

messages(
  id UUID PK,
  tenant_id UUID FK tenants(id),
  thread_id UUID FK threads(id),
  direction TEXT CHECK(direction IN ('inbound','outbound')),
  provider_message_id TEXT,
  rfc_message_id TEXT,
  headers_json JSONB,
  from_email TEXT,
  to_emails TEXT,
  cc_emails TEXT,
  subject TEXT,
  body_text TEXT,
  received_at TIMESTAMPTZ,
  sent_at TIMESTAMPTZ
)

audits(
  id UUID PK,
  tenant_id UUID FK tenants(id),
  thread_id UUID FK threads(id),
  inbound_message_id UUID FK messages(id),
  intent TEXT,
  confidence FLOAT,
  action TEXT,
  draft_subject TEXT,
  draft_body_text TEXT,
  knowledge_used_json JSONB,
  approved_at TIMESTAMPTZ NULL,
  sent_message_id TEXT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
)

routing_rules(
  id UUID PK,
  tenant_id UUID FK tenants(id),
  priority INT NOT NULL DEFAULT 0,
  match_intent TEXT NOT NULL,
  route_queue TEXT NOT NULL,
  assign_user_id UUID NULL FK users(id),
  auto_send_allowed BOOL DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT now()
)
INDEX on routing_rules(tenant_id, priority)

policies(
  id UUID PK,
  tenant_id UUID FK tenants(id),
  name TEXT NOT NULL,
  value_json JSONB NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(tenant_id, name)
)

══════════════════
Endpoints (Tier-1)
══════════════════
GET /health
  -> {"ok": true}

POST /v1/admin/mailboxes/upsert
  Auth: X-API-Key
  Body: { tenant_key, gmail_user, oauth_json }
  - Find or create tenant by tenant_key
  - Upsert mailbox row

GET /v1/admin/mailboxes
  Auth: X-API-Key
  - List all mailboxes (id, tenant_id, gmail_user, created_at)

POST /v1/ingest/gmail/message_ref
  Auth: X-API-Key must match SERVER_API_KEY
  Body:
    {
      "tenant_key": "acme",
      "gmail_user": "agent@domain.com",
      "gmail_message_id": "<gmail message id>",
      "gmail_thread_id": "<gmail thread id>"
    }
  Behavior:
    1. Find tenant by tenant_key (create if missing for MVP)
    2. Load mailbox OAuth for gmail_user — error if not present
    3. Fetch full message via Gmail API users.messages.get(format=full)
    4. Extract headers: From, To, Cc, Subject, Message-Id, References, In-Reply-To
    5. Extract plain-text body (best effort: prefer text/plain, fallback decode text/html)
    6. Upsert thread by provider_thread_id
    7. Insert inbound message row
    8. Classify intent (see classifier.py rules below)
    9. Determine action via policy (default: DRAFT_FOR_APPROVAL)
    10. ALWAYS call knowledge_client.search() BEFORE drafting
    11. Assemble full thread history oldest → newest
    12. Build GPT prompt and call drafting.py
    13. Store draft in audits row
    14. Return Decision JSON:
        {
          "action": "DRAFT_FOR_APPROVAL|AUTO_SEND|ROUTE_ONLY|IGNORE",
          "audit_id": "uuid",
          "intent": "schedule_request",
          "confidence": 0.9,
          "draft": {"subject": "Re: ...", "body_text": "..."}
        }

POST /v1/approval/decision
  Auth: X-API-Key
  Body:
    {
      "audit_id": "uuid",
      "decision": "APPROVE|REJECT|EDIT",
      "edited_subject": null,
      "edited_body_text": null
    }
  Behavior:
    - Load audit + thread + inbound message
    - If EDIT: replace draft_subject + draft_body_text in audit
    - If APPROVE:
        - Send Gmail reply using Gmail API
        - MUST set threadId
        - MUST include In-Reply-To and References headers from original message
        - Subject: "Re: <original>" if missing
        - Insert outbound message row
        - Update audit: approved_at, sent_message_id
    - If REJECT: update audit action = 'REJECTED'
    - Return {"status": "ok"}

══════════════════
CLASSIFIER (classifier.py)
══════════════════
Structure classifier.py so the classification logic lives in ONE function:

    def classify(subject: str, body: str) -> dict:
        ...
        return {"intent": str, "confidence": float, "reasoning": str}

This function MUST be self-contained and replaceable with an LLM call in a future
phase without changing any calling code.

For this MVP, implement rule-based classification inside classify():
- "schedule" intent if subject/body contains: schedule, call, meet, meeting,
  appointment, availability, available, book, when are you free
- "pricing" intent if contains: price, quote, estimate, cost, how much, budget
- "support" intent if contains: problem, issue, error, not working, help, broken
- "spam" intent if confidence >= 0.85 and common spam signals detected
- "other" for everything else

Confidence scoring: count keyword matches, normalize to 0.0–1.0.
Return the highest-confidence match.

NOTE FOR FUTURE UPGRADE: To switch to LLM classification, replace only the body
of classify() with an openai.chat.completions.create() call that returns the same
{intent, confidence, reasoning} JSON structure. No other code changes required.

══════════════════
MANDATORY GPT DRAFTING RULES (drafting.py)
══════════════════
drafting.py MUST call:
    openai.chat.completions.create()
Model: settings.OPENAI_CHAT_MODEL (from env, default gpt-4o-mini)

The drafting function signature:
    def draft_reply(
        thread_messages: list[dict],
        knowledge_chunks: list[dict],
        intent: str,
        tenant: Tenant,
    ) -> dict:  # returns {"subject": str, "body_text": str}

System message MUST include:
    - You are a professional email assistant for a {tenant.industry} business.
    - You MUST reference specific facts from the email thread.
    - You MUST NOT use generic openers like "Thanks for reaching out" or
      "I hope this email finds you well."
    - You MUST flag any missing required information (e.g. missing PO number,
      missing address, missing date).
    - You MUST stay concise and action-oriented.
    - If knowledge context is provided, reference relevant details naturally.
    - Draft reply only. No commentary, no meta-text.

User message MUST include:
    - Full thread history formatted clearly (oldest first, each message labeled
      with direction, sender, date, subject, body)
    - If knowledge_chunks is not empty: a "Client Context" section with the
      retrieved chunks, each labeled with filename and relevance score
    - Instruction: "Draft a reply to the most recent inbound message above."

Store draft in audits.draft_body_text and audits.draft_subject.

══════════════════
KNOWLEDGE CLIENT (knowledge_client.py)
══════════════════
Called on EVERY draft attempt, before calling GPT.

    def search(
        tenant_id: str,
        workspace_id: str | None,
        client_id: str | None,
        query: str,
        top_k: int = 5,
    ) -> list[dict]:

Behavior:
- If settings.KNOWLEDGE_API_URL is blank or None: return [] immediately, no error
- POST to {KNOWLEDGE_API_URL}/v1/knowledge/search with X-API-Key header
- On HTTP error or connection error: log warning, return [] (never raise)
- On success: return list of chunk dicts with text, file_name, rel_path, score

═══════════════════════════════════════
TIER-2 (knowledge-api) REQUIREMENTS
═══════════════════════════════════════

Uses pgvector extension.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

Tables:

workspaces(
  id UUID PK,
  tenant_id UUID NOT NULL,
  name TEXT NOT NULL,
  root_path TEXT NOT NULL,
  is_active BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT now()
)

clients(
  id UUID PK,
  tenant_id UUID NOT NULL,
  workspace_id UUID FK workspaces(id),
  folder_name TEXT NOT NULL,
  folder_path TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
)

documents(
  id UUID PK,
  tenant_id UUID NOT NULL,
  workspace_id UUID NOT NULL,
  client_id UUID NOT NULL,
  abs_path TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  file_name TEXT NOT NULL,
  file_ext TEXT NOT NULL,
  file_size BIGINT NOT NULL,
  mtime TIMESTAMPTZ NOT NULL,
  content_sha256 TEXT NOT NULL,
  parse_status TEXT DEFAULT 'pending',
  parse_error TEXT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(tenant_id, abs_path)
)

chunks(
  id UUID PK,
  tenant_id UUID NOT NULL,
  workspace_id UUID NOT NULL,
  client_id UUID NOT NULL,
  document_id UUID FK documents(id) ON DELETE CASCADE,
  chunk_index INT NOT NULL,
  text TEXT NOT NULL,
  char_start INT NULL,
  char_end INT NULL,
  chunk_sha256 TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(document_id, chunk_index)
)

embeddings(
  chunk_id UUID PK FK chunks(id) ON DELETE CASCADE,
  tenant_id UUID NOT NULL,
  workspace_id UUID NOT NULL,
  client_id UUID NOT NULL,
  embedding vector(1536) NOT NULL,
  model TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
)
INDEX: ivfflat cosine index on embeddings(embedding vector_cosine_ops) WITH (lists=100)
INDEX: on embeddings(tenant_id, workspace_id, client_id)

ingestion_jobs(
  id UUID PK,
  tenant_id UUID NOT NULL,
  workspace_id UUID NULL,
  client_id UUID NULL,
  job_type TEXT NOT NULL,
  status TEXT DEFAULT 'queued',
  started_at TIMESTAMPTZ NULL,
  finished_at TIMESTAMPTZ NULL,
  stats_json JSONB NULL,
  error TEXT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
)

══════════════════
Tier-2 Endpoints
══════════════════
GET /health
  -> {"ok": true}

POST /v1/ingest/scan
  Auth: X-API-Key
  Body: { tenant_id, workspace_name, root_path }
  - If SYNOLOGY_ROOT is blank:
      return {"scanned": 0, "reason": "SYNOLOGY_ROOT not configured"}
  - Walk folder tree: workspace root → client folders → documents
  - Upsert workspaces and clients
  - For each file: check mtime first (fast), then content_sha256 if mtime changed
  - Only mark parse_status='pending' if sha256 actually differs (skip unchanged files)
  - Create ingestion_job row (job_type='scan')
  - Return job stats

POST /v1/ingest/process_pending
  Auth: X-API-Key
  - Find documents with parse_status='pending'
  - For each: parse → chunk → embed → update parse_status
  - Create ingestion_job row (job_type='process')
  - Parsing: PDF (pypdf first, pdfminer fallback), DOCX (python-docx), TXT/MD (read direct)
  - Encrypted PDFs: log warning, set parse_status='failed', parse_error='encrypted', continue
  - Chunking: two-pass (target_chars=3500, overlap=400)
  - Embedding: OpenAI batch (max 100 chunks per API call), retry on rate limit
  - First-wins dedup: if chunk_sha256 already exists for this document, skip

POST /v1/knowledge/search
  Auth: X-API-Key
  Body:
    {
      "tenant_id": "uuid",
      "workspace_id": "uuid",
      "client_id": "uuid",
      "query": "text",
      "top_k": 5,
      "min_score": 0.0
    }
  Behavior:
    - Embed query via OpenAI text-embedding-3-small
    - Vector search filtered by tenant_id + workspace_id + client_id (ALL THREE required)
    - Return top_k chunks ordered by cosine similarity
    - Response: list of {chunk_id, text, file_name, rel_path, score}

═══════════════════════════════════════
Docker Compose
═══════════════════════════════════════
Single docker-compose.yml at repo root.

services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: emailuser
      POSTGRES_PASSWORD: emailpass
      POSTGRES_MULTIPLE_DATABASES: emailsystem_core,emailsystem_knowledge
    volumes:
      - postgres_data:/var/lib/postgresql/data
    ports:
      - "5432:5432"

  emailsystem-api:
    build: services/emailsystem-api
    ports:
      - "8000:8000"
    env_file: .env
    depends_on: [postgres]

  knowledge-api:
    build: services/knowledge-api
    ports:
      - "9000:9000"
    env_file: .env
    depends_on: [postgres]
    volumes:
      - ${SYNOLOGY_ROOT:-/tmp/synology-placeholder}:/mnt/synology:ro

  n8n:
    image: n8nio/n8n
    ports:
      - "5678:5678"
    volumes:
      - n8n_data:/home/node/.n8n
    environment:
      - N8N_BASIC_AUTH_ACTIVE=true
      - N8N_BASIC_AUTH_USER=admin
      - N8N_BASIC_AUTH_PASSWORD=changeme

volumes:
  postgres_data:
  n8n_data:

Note: Use a custom postgres init script or entrypoint to create both databases
(emailsystem_core and emailsystem_knowledge) on first startup.

═══════════════════════════════════════
Output Requirements
═══════════════════════════════════════
- Provide full directory tree
- Provide full content of EVERY file
- Include Alembic migrations for both services (initial migration with all tables)
- Include Dockerfiles for both services
- Include docker-compose.yml
- Include minimal n8n workflow JSON skeleton (Gmail trigger → POST message_ref)
- Include .env.example at repo root
- Include README.md with setup steps, docker compose up --build, example curl commands

Must run with: docker compose up --build

Do NOT skip implementation details.
Do NOT leave stubs unimplemented.
Do NOT create files for Telegram, Calendar, or Admin GUI.
All logic must be real and runnable.
