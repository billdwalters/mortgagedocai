# EmailSystem — n8n → Gmail → Tier-1 Wiring Guide

**Purpose:** Step-by-step setup to connect Gmail → n8n → emailsystem-api so real emails trigger classify → draft → Telegram approval.

---

## Current State (as of 2026-02-27)

All services running on 10.10.10.12:
- `emailsystem-api` port 8000 — UP, 9 tables, /health OK
- `knowledge-api` port 9000 — UP, 6 tables, /health OK
- `postgres` port 5432 — Healthy, both DBs, all 17 tables
- `n8n` port 5678 — UP

Verified: Admin GUI working, tenant "acme" + mailbox created, auth gating working.

---

## Prerequisites check

```bash
docker ps
curl http://localhost:8000/health
curl http://localhost:9000/health
grep -E "SERVER_API_KEY|OPENAI_API_KEY|TELEGRAM_BOT_TOKEN|TELEGRAM_WEBHOOK_URL" /opt/emailsystem/.env
```

---

## Step 1 — Google Cloud: enable Gmail API + create OAuth credentials

Do this once per Gmail account being monitored.

1. Go to https://console.cloud.google.com
2. Create or select project: **emailsystem**
3. Enable **Gmail API**: APIs & Services → Library → search "Gmail API" → Enable
4. Create OAuth 2.0 credentials:
   - APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: **Desktop app**
   - Name: `emailsystem-local`
   - Download the `client_secret_*.json` file
5. OAuth consent screen → Test users → add the Gmail address being monitored

---

## Step 2 — Get Gmail refresh_token for emailsystem-api

emailsystem-api fetches and sends Gmail messages using its OWN stored tokens (not n8n's).

```bash
# On email server or your local machine (needs a browser)
pip install google-auth-oauthlib google-auth-httplib2

python3 - <<'EOF'
from google_auth_oauthlib.flow import InstalledAppFlow
import json

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify"
]

flow = InstalledAppFlow.from_client_secrets_file("client_secret_XXXX.json", SCOPES)
creds = flow.run_local_server(port=0)

print(json.dumps({
    "token":         creds.token,
    "refresh_token": creds.refresh_token,
    "client_id":     creds.client_id,
    "client_secret": creds.client_secret,
    "token_uri":     creds.token_uri,
}, indent=2))
EOF
```

Save the output — you need `client_id`, `client_secret`, `refresh_token` for Step 3.

> **Headless server tip:** If the email server has no browser, run this script on your local machine and copy the output back.

---

## Step 3 — Store Gmail credentials in mailbox row

**Option A — via Admin GUI** (check if OAuth fields are present):
1. Browse to `http://10.10.10.12:8000/admin`
2. Log in with ADMIN_USERNAME / ADMIN_PASSWORD from `.env`
3. Find mailbox → edit → paste `gmail_client_id`, `gmail_client_secret`, `gmail_refresh_token`

**Option B — via API upsert:**
```bash
curl -X POST http://localhost:8000/v1/admin/mailboxes/upsert \
  -H "X-API-Key: $SERVER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_key": "acme",
    "email_address": "you@gmail.com",
    "gmail_client_id": "YOUR_CLIENT_ID",
    "gmail_client_secret": "YOUR_CLIENT_SECRET",
    "gmail_refresh_token": "YOUR_REFRESH_TOKEN"
  }'
```

**Verify credentials are working:**
```bash
curl -X POST http://localhost:8000/v1/ingest/gmail/message_ref \
  -H "X-API-Key: $SERVER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tenant_key":"acme","gmail_user":"you@gmail.com","gmail_message_id":"TEST","gmail_thread_id":"TEST"}'
# Expect: 404 (message not found) — NOT 500 (credentials error)
```

---

## Step 4 — Expose Telegram webhook via Tailscale Funnel

Telegram needs a public HTTPS URL to call `POST /v1/telegram/webhook`.

```bash
# Install Tailscale if not present
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Enable Funnel on port 8000
sudo tailscale funnel 8000
# Output: https://HOSTNAME.TAILNET.ts.net → forwards to localhost:8000
```

Update `.env`:
```
TELEGRAM_WEBHOOK_URL=https://HOSTNAME.TAILNET.ts.net/v1/telegram/webhook
```

Restart emailsystem-api to pick up the new URL:
```bash
docker compose restart emailsystem-api
```

Verify Telegram webhook registered:
```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
# Should show url: https://HOSTNAME.TAILNET.ts.net/v1/telegram/webhook
```

---

## Step 5 — Configure Gmail credentials in n8n

1. Open n8n at `http://10.10.10.12:5678`
2. Settings → Credentials → New → **Gmail OAuth2 API**
3. Paste `client_id` and `client_secret` from Step 1
4. Click **Connect** → complete Google OAuth in browser
5. Select the Gmail account being monitored
6. Save as: `Gmail - emailsystem`

---

## Step 6 — Create n8n workflow

In n8n: New Workflow → add two nodes:

### Node 1: Gmail Trigger
| Setting | Value |
|---------|-------|
| Type | Gmail Trigger |
| Credentials | `Gmail - emailsystem` |
| Event | Message Received |
| Mailbox | INBOX |
| Filters | Label: UNREAD (optional) |
| Poll interval | Every 1 minute |

### Node 2: HTTP Request
| Setting | Value |
|---------|-------|
| Type | HTTP Request |
| Method | POST |
| URL | `http://emailsystem-api:8000/v1/ingest/gmail/message_ref` |
| Auth | Generic → Header Auth |
| Header name | `X-API-Key` |
| Header value | (paste SERVER_API_KEY value) |
| Body | JSON (see below) |

**Body JSON:**
```json
{
  "tenant_key": "acme",
  "gmail_user": "you@gmail.com",
  "gmail_message_id": "{{ $json.id }}",
  "gmail_thread_id": "{{ $json.threadId }}"
}
```

> **Important:** Use `http://emailsystem-api:8000` (Docker service name), not `localhost`.

Connect: Gmail Trigger → HTTP Request → Save → **Activate**.

---

## Step 7 — End-to-end test

1. Send a test email to the monitored Gmail address
2. Wait up to 60 seconds (n8n poll interval) OR trigger manually in n8n
3. In n8n Executions: verify one run completed successfully
4. HTTP Request response should show: `action=DRAFT_FOR_APPROVAL`, `audit_id`, `draft.body_text`
5. Check Telegram — approval message should arrive:
   ```
   📧 New email from: sender@example.com
   Subject: Test subject
   ---
   Draft reply:
   [GPT-drafted reply text]
   ---
   Reply: approve / reject / or type edited reply
   ```
6. Reply `approve` in Telegram
7. Check Gmail Sent folder — reply should appear in the thread

---

## Debugging

**emailsystem-api logs:**
```bash
docker logs emailsystem-api --tail 50 -f
```

**Telegram webhook not receiving:**
```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
# Check: url field, last_error_message, pending_update_count
```

**n8n HTTP Request failing (connection refused):**
- Use `http://emailsystem-api:8000` not `http://localhost:8000` in n8n
- Verify both containers are on the same Docker network: `docker network ls`

**GPT draft empty or error:**
```bash
grep OPENAI_API_KEY /opt/emailsystem/.env
# Make sure it's set and valid
```

---

## Verification checklist

- [ ] All 4 services UP (`docker ps`)
- [ ] `.env` has SERVER_API_KEY, OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_URL
- [ ] Mailbox row has Gmail OAuth credentials (test ingest returns 404 not 500)
- [ ] `getWebhookInfo` shows webhook registered at Tailscale HTTPS URL
- [ ] n8n workflow active, Gmail Trigger + HTTP Request configured
- [ ] Test email → n8n fires → Telegram approval message received
- [ ] `approve` in Telegram → Gmail reply sent in-thread
