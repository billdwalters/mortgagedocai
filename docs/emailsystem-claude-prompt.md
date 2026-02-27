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
SCOPE OF THIS BUILD — BUILD ALL OF THIS
═══════════════════════════════════════
This is the complete MVP. Build ALL of the following:
- Gmail ingestion + thread tracking + message storage
- OpenAI GPT drafting (mandatory — never templates)
- Telegram notifications + approval (text and voice)
- Google Calendar availability check + appointment scheduling
- Admin GUI (web UI for managing users, mailboxes, routing rules)
- Knowledge API (Tier-2) with Synology document indexing + search

Do NOT leave stubs. Do NOT skip implementation. Every file must be real and runnable.

═══════════════════════════════════════
ARCHITECTURE OVERVIEW
═══════════════════════════════════════
Tier-1: emailsystem-api (PRIMARY PRODUCT)
- Gmail OAuth ingestion
- Thread tracking + message storage
- Audit log
- Intent classification (rule-based MVP, structured for LLM upgrade)
- Routing to user/department
- OpenAI GPT drafting (MANDATORY — never keyword templates)
- Knowledge integration (always wired, gracefully no-ops if unconfigured)
- Telegram notifications + voice/text approval workflow
- Google Calendar availability + scheduling
- Admin GUI (web UI served at /admin)
- n8n orchestration integration

Tier-2: knowledge-api (OPTIONAL UPGRADE — always deployed, activates via config)
- Synology scan (read-only)
- Document parsing (PDF, DOCX, TXT)
- Chunking + OpenAI embeddings
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

# Tier-1 — Database
CORE_DATABASE_URL=postgresql+psycopg2://emailuser:emailpass@postgres:5432/emailsystem_core

# Tier-1 — OpenAI
OPENAI_API_KEY=
OPENAI_CHAT_MODEL=gpt-4o-mini

# Tier-1 — Knowledge integration
KNOWLEDGE_API_URL=http://knowledge-api:9000
KNOWLEDGE_API_KEY=

# Tier-1 — Gmail
GMAIL_FETCH_FORMAT=full

# Tier-1 — Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_URL=https://your-server/v1/telegram/webhook

# Tier-1 — Admin GUI
ADMIN_USERNAME=admin
ADMIN_PASSWORD=changeme

# Tier-2 — Database
KNOWLEDGE_DATABASE_URL=postgresql+psycopg2://emailuser:emailpass@postgres:5432/emailsystem_knowledge

# Tier-2 — Synology
SYNOLOGY_ROOT=

# Tier-2 — Embeddings
OPENAI_EMBED_MODEL=text-embedding-3-small
EMBED_DIM=1536

═══════════════════════════════════════
TIER-1 (emailsystem-api) REQUIREMENTS
═══════════════════════════════════════

══════════════════
Folder Structure
══════════════════
services/emailsystem-api/
  Dockerfile
  pyproject.toml
  alembic.ini
  alembic/
    env.py
    versions/
  src/app/
    main.py
    config.py
    security.py
    api/v1/
      ingest.py          ← Gmail message ingestion
      approval.py        ← human approval workflow
      gmail_oauth.py     ← store/list mailbox OAuth tokens
      telegram.py        ← Telegram webhook receiver
      calendar.py        ← availability check + scheduling
      admin.py           ← admin API routes (users, routing rules, policies)
    core/
      gmail_client.py    ← Gmail API: fetch messages, send replies
      threading.py       ← assemble full thread history for LLM
      classifier.py      ← intent classification (rule-based, swappable)
      policy.py          ← action decision (DRAFT/AUTO_SEND/ROUTE/IGNORE)
      router.py          ← route to user/queue by intent + rules
      drafting.py        ← GPT draft generation
      knowledge_client.py ← calls Tier-2, returns [] if unconfigured
      telegram_client.py ← send notifications, receive approvals, transcribe voice
      calendar_client.py ← Google Calendar free/busy + event creation
    db/
      session.py
      models.py
    frontend/
      templates/
        base.html        ← shared layout with nav
        dashboard.html   ← recent audits + pending approvals
        users.html       ← manage users (add/edit/delete, set Telegram ID)
        mailboxes.html   ← manage Gmail accounts + OAuth status
        routing.html     ← manage routing rules
    util/
      logging.py
  tests/
    test_threading.py
    test_classifier.py
    test_drafting.py
    test_policy.py

══════════════════
Database Schema (Alembic)
══════════════════

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
  role TEXT DEFAULT 'agent',
  telegram_user_id TEXT NULL,
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

calendar_tokens(
  id UUID PK,
  user_id UUID FK users(id) ON DELETE CASCADE,
  token_json JSONB NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT now()
)

threads(
  id UUID PK,
  tenant_id UUID FK tenants(id),
  mailbox_id UUID FK mailboxes(id),
  provider TEXT DEFAULT 'gmail',
  provider_thread_id TEXT,
  stage TEXT DEFAULT 'new',
  owner_user_id UUID NULL FK users(id),
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
  telegram_message_id TEXT NULL,
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
  - List all mailboxes

POST /v1/ingest/gmail/message_ref
  Auth: X-API-Key must match SERVER_API_KEY
  Body: { tenant_key, gmail_user, gmail_message_id, gmail_thread_id }
  Behavior:
    1. Find tenant by tenant_key (create if missing for MVP)
    2. Load mailbox OAuth for gmail_user — error if not present
    3. Fetch full message via Gmail API users.messages.get(format=full)
    4. Extract headers: From, To, Cc, Subject, Message-Id, References, In-Reply-To
    5. Extract plain-text body (prefer text/plain, fallback decode text/html)
    6. Upsert thread by provider_thread_id
    7. Insert inbound message row
    8. Classify intent (see classifier.py below)
    9. Determine action via policy (default: DRAFT_FOR_APPROVAL)
    10. ALWAYS call knowledge_client.search() BEFORE drafting
    11. Assemble full thread history oldest → newest
    12. Call drafting.py → store draft in audits row
    13. If action == DRAFT_FOR_APPROVAL and thread.owner_user_id has telegram_user_id:
        - Call telegram_client.send_approval_request()
        - Store telegram_message_id in audit
    14. If action == AUTO_SEND: send immediately via gmail_client, insert outbound message
    15. Return Decision JSON:
        {
          "action": "DRAFT_FOR_APPROVAL|AUTO_SEND|ROUTE_ONLY|IGNORE",
          "audit_id": "uuid",
          "intent": "schedule_request",
          "confidence": 0.9,
          "draft": {"subject": "Re: ...", "body_text": "..."}
        }

POST /v1/approval/decision
  Auth: X-API-Key
  Body: { audit_id, decision: "APPROVE|REJECT|EDIT", edited_subject, edited_body_text }
  Behavior:
    - Load audit + thread + inbound message
    - If EDIT: replace draft_subject + draft_body_text in audit
    - If APPROVE:
        - Send Gmail reply using Gmail API
        - MUST set threadId
        - MUST include correct In-Reply-To and References headers
        - Subject: "Re: <original>" if missing
        - Insert outbound message row
        - Update audit: approved_at, sent_message_id
    - If REJECT: update audit action = 'REJECTED'
    - Return {"status": "ok"}

POST /v1/telegram/webhook
  No auth (Telegram calls this directly — validate via bot token secret)
  Behavior:
    - Parse incoming Telegram update
    - If message is voice: download .ogg file, transcribe via OpenAI Whisper API,
      treat transcript as text command
    - If message text is "approve" (case-insensitive):
        - Find most recent DRAFT_FOR_APPROVAL audit for this telegram_user_id
        - Call approval/decision with APPROVE
        - Reply to user: "Reply sent ✓"
    - If message text is "reject":
        - Find audit, mark REJECTED
        - Reply to user: "Reply discarded"
    - If message text starts with "edit:" or is anything else:
        - Treat full message as edited reply body
        - Call approval/decision with EDIT + the new text, then APPROVE
        - Reply to user: "Edited reply sent ✓"

GET /v1/calendar/availability
  Auth: X-API-Key
  Query params: user_id, date (YYYY-MM-DD), duration_minutes (default 30)
  Behavior:
    - Load calendar_tokens for user_id
    - Call Google Calendar freebusy API for that date
    - Return list of available time slots (not busy, within 9am-5pm)

POST /v1/calendar/schedule
  Auth: X-API-Key
  Body: { user_id, summary, attendee_email, start_time, end_time, description }
  Behavior:
    - Load calendar_tokens for user_id
    - Create Google Calendar event with attendee
    - Return { event_id, html_link }

GET /admin
  No API auth (uses session cookie — see ADMIN_USERNAME / ADMIN_PASSWORD env vars)
  Serves Admin GUI dashboard (Jinja2 HTML)

GET /admin/users
POST /admin/users/create
POST /admin/users/{id}/edit
POST /admin/users/{id}/delete
  - Manage users: name, email, role, telegram_user_id

GET /admin/mailboxes
POST /admin/mailboxes/add
  - Add Gmail accounts (link to OAuth flow or paste oauth_json)

GET /admin/routing
POST /admin/routing/create
POST /admin/routing/{id}/delete
  - Manage routing rules: intent → user assignment + auto_send flag

══════════════════
CLASSIFIER (classifier.py)
══════════════════
Structure classifier.py so the classification logic lives in ONE function:

    def classify(subject: str, body: str) -> dict:
        return {"intent": str, "confidence": float, "reasoning": str}

This function MUST be self-contained so the body can be replaced with an LLM call
in a future phase without changing any calling code.

MVP rule-based implementation inside classify():
- "schedule" if contains: schedule, call, meet, meeting, appointment,
  availability, available, book, when are you free
- "pricing" if contains: price, quote, estimate, cost, how much, budget
- "support" if contains: problem, issue, error, not working, help, broken
- "spam" if confidence >= 0.85 and common spam signals detected
- "other" for everything else

Confidence: count keyword matches, normalize to 0.0–1.0.

══════════════════
MANDATORY GPT DRAFTING RULES (drafting.py)
══════════════════
MUST call: openai.chat.completions.create()
Model: settings.OPENAI_CHAT_MODEL

Function signature:
    def draft_reply(
        thread_messages: list[dict],
        knowledge_chunks: list[dict],
        intent: str,
        tenant: Tenant,
        available_slots: list[dict] | None = None,
    ) -> dict:  # returns {"subject": str, "body_text": str}

System message MUST instruct:
    - You are a professional email assistant for a {tenant.industry} business.
    - Reference specific facts from the email thread — never respond generically.
    - NEVER use openers like "Thanks for reaching out" or "I hope this finds you well."
    - Flag any missing required info (missing PO number, missing address, etc.)
    - Stay concise and action-oriented.
    - If calendar slots are provided, propose specific times — do not ask for theirs.
    - If knowledge context is provided, reference client details naturally.
    - Draft reply only. No commentary.

User message MUST include:
    - Full thread history (oldest first, labeled with direction/sender/date/body)
    - If knowledge_chunks not empty: "Client Context" section with filename + score
    - If available_slots not empty: "Available Times" section listing specific slots
    - Instruction: "Draft a reply to the most recent inbound message."

══════════════════
TELEGRAM CLIENT (telegram_client.py)
══════════════════
Uses python-telegram-bot library.

Functions:
    def send_approval_request(telegram_user_id: str, audit: Audit,
                               thread: Thread, draft: dict) -> str:
        """Send draft to user via Telegram for approval. Returns telegram message_id."""
        # Message format:
        # 📧 New email from: {from_email}
        # Subject: {subject}
        # ---
        # Draft reply:
        # {draft.body_text[:500]}... (truncate if long)
        # ---
        # Reply: approve / reject / or type edited reply
        # Voice messages also accepted.

    def transcribe_voice(file_id: str) -> str:
        """Download .ogg from Telegram, transcribe via OpenAI Whisper API."""
        # Download file from Telegram API using TELEGRAM_BOT_TOKEN
        # POST to openai.audio.transcriptions.create(model="whisper-1")
        # Return transcript text

    def send_message(telegram_user_id: str, text: str) -> None:
        """Send simple text message to user."""

Setup: register webhook via Telegram setWebhook API pointing to TELEGRAM_WEBHOOK_URL.
Include a startup check in main.py that registers the webhook on app startup.

══════════════════
CALENDAR CLIENT (calendar_client.py)
══════════════════
Uses google-api-python-client + google-auth.

Functions:
    def get_available_slots(token_json: dict, date: str,
                            duration_minutes: int = 30) -> list[dict]:
        """Return list of available slots on given date (9am-5pm).
        Each slot: {"start": ISO datetime, "end": ISO datetime, "label": "10:00 AM"}"""
        # Build credentials from token_json
        # Call calendar.freebusy().query() for the date range
        # Calculate gaps between busy blocks that fit duration_minutes
        # Return slots within business hours only

    def create_event(token_json: dict, summary: str, attendee_email: str,
                     start_time: str, end_time: str, description: str = "") -> dict:
        """Create calendar event. Returns {event_id, html_link}."""

Token storage: calendar_tokens table (token_json JSONB per user).
Include token refresh logic — google-auth handles this automatically via credentials.

══════════════════
ADMIN GUI (frontend/templates/)
══════════════════
Serve at /admin using FastAPI + Jinja2Templates.
Use simple HTML + minimal CSS (no JavaScript framework required).
Use session cookie for admin login (ADMIN_USERNAME + ADMIN_PASSWORD from env).

Pages required:
1. GET /admin/login — login form
2. GET /admin — dashboard: last 20 audits with status, pending approvals count
3. GET /admin/users — table of users with edit/delete buttons
   POST /admin/users/create — form: display_name, email, role, telegram_user_id
   POST /admin/users/{id}/edit
   POST /admin/users/{id}/delete
4. GET /admin/mailboxes — table of Gmail accounts with status
   POST /admin/mailboxes/add — form: tenant_key, gmail_user, paste oauth_json
5. GET /admin/routing — table of routing rules
   POST /admin/routing/create — form: intent, route_queue, assign_user, auto_send
   POST /admin/routing/{id}/delete

base.html must include navigation: Dashboard | Users | Mailboxes | Routing Rules

══════════════════
KNOWLEDGE CLIENT (knowledge_client.py)
══════════════════
Called on EVERY draft attempt, before calling GPT.

    def search(tenant_id, workspace_id, client_id, query, top_k=5) -> list[dict]:
        - If KNOWLEDGE_API_URL blank: return [] immediately, no error, no log
        - POST to {KNOWLEDGE_API_URL}/v1/knowledge/search with X-API-Key
        - On any error: log warning, return []
        - On success: return list of {text, file_name, rel_path, score}

═══════════════════════════════════════
TIER-2 (knowledge-api) REQUIREMENTS
═══════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

Tables:

workspaces(
  id UUID PK, tenant_id UUID NOT NULL, name TEXT NOT NULL,
  root_path TEXT NOT NULL, is_active BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT now()
)

clients(
  id UUID PK, tenant_id UUID NOT NULL,
  workspace_id UUID FK workspaces(id),
  folder_name TEXT NOT NULL, folder_path TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
)

documents(
  id UUID PK, tenant_id UUID NOT NULL, workspace_id UUID NOT NULL,
  client_id UUID NOT NULL, abs_path TEXT NOT NULL, rel_path TEXT NOT NULL,
  file_name TEXT NOT NULL, file_ext TEXT NOT NULL, file_size BIGINT NOT NULL,
  mtime TIMESTAMPTZ NOT NULL, content_sha256 TEXT NOT NULL,
  parse_status TEXT DEFAULT 'pending', parse_error TEXT NULL,
  created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(tenant_id, abs_path)
)

chunks(
  id UUID PK, tenant_id UUID NOT NULL, workspace_id UUID NOT NULL,
  client_id UUID NOT NULL, document_id UUID FK documents(id) ON DELETE CASCADE,
  chunk_index INT NOT NULL, text TEXT NOT NULL,
  char_start INT NULL, char_end INT NULL, chunk_sha256 TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(document_id, chunk_index)
)

embeddings(
  chunk_id UUID PK FK chunks(id) ON DELETE CASCADE,
  tenant_id UUID NOT NULL, workspace_id UUID NOT NULL, client_id UUID NOT NULL,
  embedding vector(1536) NOT NULL, model TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
)
INDEX: ivfflat cosine on embeddings(embedding vector_cosine_ops) WITH (lists=100)
INDEX: on embeddings(tenant_id, workspace_id, client_id)

ingestion_jobs(
  id UUID PK, tenant_id UUID NOT NULL,
  workspace_id UUID NULL, client_id UUID NULL,
  job_type TEXT NOT NULL, status TEXT DEFAULT 'queued',
  started_at TIMESTAMPTZ NULL, finished_at TIMESTAMPTZ NULL,
  stats_json JSONB NULL, error TEXT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
)

══════════════════
Tier-2 Endpoints
══════════════════

GET /health -> {"ok": true}

POST /v1/ingest/scan
  - If SYNOLOGY_ROOT blank: return {scanned:0, reason:"SYNOLOGY_ROOT not configured"}
  - Walk folder tree, upsert workspaces/clients/documents
  - mtime-first change detection, sha256 only if mtime changed
  - Create ingestion_job row (job_type='scan')

POST /v1/ingest/process_pending
  - Parse pending docs (pypdf→pdfminer fallback, python-docx, TXT direct)
  - Encrypted PDFs: log warning, set parse_status='failed', continue
  - Two-pass chunking (target_chars=3500, overlap=400)
  - OpenAI batch embedding (max 100 per call, retry on rate limit)
  - First-wins dedup on chunk_sha256
  - Create ingestion_job row (job_type='process')

POST /v1/knowledge/search
  Auth: X-API-Key
  Body: {tenant_id, workspace_id, client_id, query, top_k, min_score}
  - Embed query via OpenAI
  - pgvector cosine search filtered by ALL THREE IDs
  - Return [{chunk_id, text, file_name, rel_path, score}]

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
    volumes: [postgres_data:/var/lib/postgresql/data]
    ports: ["5432:5432"]

  emailsystem-api:
    build: services/emailsystem-api
    ports: ["8000:8000"]
    env_file: .env
    depends_on: [postgres]

  knowledge-api:
    build: services/knowledge-api
    ports: ["9000:9000"]
    env_file: .env
    depends_on: [postgres]
    volumes:
      - ${SYNOLOGY_ROOT:-/tmp/synology-placeholder}:/mnt/synology:ro

  n8n:
    image: n8nio/n8n
    ports: ["5678:5678"]
    volumes: [n8n_data:/home/node/.n8n]
    environment:
      - N8N_BASIC_AUTH_ACTIVE=true
      - N8N_BASIC_AUTH_USER=admin
      - N8N_BASIC_AUTH_PASSWORD=changeme

volumes:
  postgres_data:
  n8n_data:

Include a postgres init script to create both databases on first startup.

═══════════════════════════════════════
Output Requirements
═══════════════════════════════════════
- Full directory tree
- Full content of EVERY file
- Alembic migrations for both services (all tables in initial migration)
- Dockerfiles for both services
- docker-compose.yml
- .env.example
- n8n workflow JSON skeleton (Gmail trigger → POST message_ref)
- README.md (setup, docker compose up --build, example curl commands)

Must run with: docker compose up --build
Do NOT skip implementation details.
Do NOT leave stubs unimplemented.
All logic must be real and runnable.
