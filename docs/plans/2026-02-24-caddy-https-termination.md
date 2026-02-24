# Caddy HTTPS Termination Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Terminate HTTPS at Caddy and reverse-proxy to FastAPI on loopback, closing port 8000 from the LAN.

**Architecture:** Caddy (installed via official apt repo) listens on :443, terminates TLS using its internal CA (self-signed for `mortgagedocai.local`), and reverse-proxies to `127.0.0.1:8000`. FastAPI is rebound from `0.0.0.0` to `127.0.0.1` so port 8000 is invisible to the LAN. The Caddyfile is version-controlled in `infra/Caddyfile` and deployed to `/etc/caddy/Caddyfile`.

**Tech Stack:** Caddy v2 (apt, caddy/stable), systemd, Ubuntu 22.04/24.04

**Server:** `10.10.10.190` — run all server-side commands there via SSH or on the machine directly.
**Repo root (server):** `/opt/mortgagedocai`
**Repo root (Windows worktree):** `M:/mortgagedocai`

---

## Task 1: Add `infra/Caddyfile` to the repo

**Files:**
- Create: `infra/Caddyfile`

**Step 1: Create the file**

```
infra/Caddyfile
```

Content (exact):

```
{
    auto_https disable_redirects
}

mortgagedocai.local {
    reverse_proxy 127.0.0.1:8000
}
```

`auto_https disable_redirects` tells Caddy to serve TLS on `:443` using its internal CA
(self-signed cert for `.local`) without issuing HTTP→HTTPS redirects. Let's Encrypt is
not attempted — `.local` is not publicly routable.

**Step 2: Verify content**

```bash
cat M:/mortgagedocai/infra/Caddyfile
```

Expected output — exactly the 7 lines above, no trailing whitespace.

**Step 3: Commit**

```bash
cd M:/mortgagedocai
git add infra/Caddyfile
git commit -m "feat(infra): add Caddyfile for HTTPS termination (mortgagedocai.local)"
```

---

## Task 2: Rebind FastAPI from `0.0.0.0` to `127.0.0.1`

**Files:**
- Modify: `mortgagedocai-api.service` line 26

**Step 1: Make the edit**

In `mortgagedocai-api.service`, change line 26:

```diff
-ExecStart=/opt/mortgagedocai/venv/bin/python3 scripts/loan_api.py --host 0.0.0.0 --port 8000
+ExecStart=/opt/mortgagedocai/venv/bin/python3 scripts/loan_api.py --host 127.0.0.1 --port 8000
```

**Step 2: Verify the diff**

```bash
cd M:/mortgagedocai && git diff mortgagedocai-api.service
```

Expected: exactly one line changed — `0.0.0.0` → `127.0.0.1`. Nothing else.

**Step 3: Compile-check (no Python changes — verify service file parses)**

```bash
# On the server:
systemd-analyze verify /opt/mortgagedocai/mortgagedocai-api.service
```

Expected: no output (no warnings/errors).
If `systemd-analyze` isn't available, skip — the unit syntax is unchanged.

**Step 4: Commit**

```bash
cd M:/mortgagedocai
git add mortgagedocai-api.service
git commit -m "feat(infra): bind FastAPI to 127.0.0.1 — port 8000 loopback-only"
```

---

## Task 3: Install Caddy on the server

All commands in this task run **on the server** (`10.10.10.190`).

**Step 1: Add Caddy's official apt repository (idempotent)**

```bash
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update
```

Expected: no errors. If the keyring file already exists, `gpg --dearmor` will overwrite it — that is fine.

**Step 2: Install Caddy**

```bash
sudo apt-get install -y caddy
```

Expected: caddy installs or "already at newest version". Caddy's package creates `/etc/caddy/Caddyfile` (a placeholder) and registers `caddy.service`.

**Step 3: Verify binary**

```bash
caddy version
```

Expected: `v2.x.x ...`

---

## Task 4: Deploy Caddyfile and start Caddy

All commands in this task run **on the server**.

**Step 1: Deploy Caddyfile from repo**

```bash
sudo cp /opt/mortgagedocai/infra/Caddyfile /etc/caddy/Caddyfile
```

**Step 2: Validate Caddyfile syntax**

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
```

Expected: `Valid configuration`

**Step 3: Enable and start Caddy**

```bash
sudo systemctl enable caddy
sudo systemctl restart caddy
```

**Step 4: Verify Caddy is running and owns :443**

```bash
sudo systemctl status caddy --no-pager
ss -tlnp | grep caddy
```

Expected:
- `systemctl status` shows `active (running)`
- `ss` shows caddy listening on `*:443` (and `*:80`)

---

## Task 5: Reload FastAPI with loopback binding

All commands in this task run **on the server**.

**Step 1: Pull the updated service file**

```bash
cd /opt/mortgagedocai && git pull
```

Expected: fast-forward, shows `mortgagedocai-api.service` changed.

**Step 2: Install the updated unit**

```bash
sudo cp /opt/mortgagedocai/mortgagedocai-api.service /etc/systemd/system/mortgagedocai-api.service
sudo systemctl daemon-reload
sudo systemctl restart mortgagedocai-api
```

**Step 3: Confirm FastAPI is on loopback only**

```bash
ss -tlnp | grep 8000
```

Expected: address is `127.0.0.1:8000`, NOT `0.0.0.0:8000`.

---

## Task 6: Full verification

All commands run **on the server** unless noted.

**Step 1: Summary port check**

```bash
ss -tlnp | grep -E ':(443|80|8000)'
```

Expected output (approximately):
```
LISTEN 0 ... 0.0.0.0:443   ... caddy
LISTEN 0 ... 0.0.0.0:80    ... caddy
LISTEN 0 ... 127.0.0.1:8000 ... python3
```

Port 8000 MUST show `127.0.0.1`, not `0.0.0.0`.

**Step 2: HTTPS through Caddy**

```bash
curl -sk https://mortgagedocai.local/health | jq .
```

Expected: `{"status": "ok"}` (or similar FastAPI health response).
`-s` suppresses progress, `-k` skips cert validation (self-signed is fine for now).

If DNS doesn't resolve `mortgagedocai.local`, use the IP with a Host header:

```bash
curl -sk --resolve mortgagedocai.local:443:127.0.0.1 https://mortgagedocai.local/health | jq .
```

**Step 3: Direct loopback still works (server-local tooling)**

```bash
curl -s http://127.0.0.1:8000/health | jq .
```

Expected: same JSON response.

**Step 4: Confirm port 8000 is NOT reachable from LAN**

Run from a **different machine** on the LAN (e.g. Windows dev machine):

```bash
curl --max-time 3 http://10.10.10.190:8000/health
```

Expected: connection refused or timeout — NOT a valid API response.

**Step 5: Check Caddy logs for errors**

```bash
journalctl -u caddy -n 50 --no-pager | grep -iE "error|warn|fail" || echo "no errors"
```

Expected: `no errors` or only informational TLS cert generation messages.

**Step 6: Check FastAPI logs**

```bash
journalctl -u mortgagedocai-api -n 20 --no-pager
```

Expected: normal startup messages, no bind errors.

---

## Rollback

If anything goes wrong:

```bash
# Stop Caddy (FastAPI on loopback still serves locally)
sudo systemctl stop caddy

# Revert service binding
sudo sed -i 's/--host 127.0.0.1/--host 0.0.0.0/' /etc/systemd/system/mortgagedocai-api.service
sudo systemctl daemon-reload && sudo systemctl restart mortgagedocai-api
```

---

## Notes

- The Caddyfile hostname (`mortgagedocai.local`) is a placeholder. When a final domain is
  confirmed, update `infra/Caddyfile`, redeploy with `sudo cp` + `sudo systemctl reload caddy`.
- Caddy's internal CA root cert lives at:
  `/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`
  Add it to your browser/OS trust store to eliminate the self-signed warning.
- Let's Encrypt integration is deferred — update the Caddyfile global block and remove
  `auto_https disable_redirects` when a public domain is ready.
