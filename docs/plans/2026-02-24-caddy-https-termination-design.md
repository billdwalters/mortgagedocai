# Caddy HTTPS Termination — Design

**Date:** 2026-02-24
**Status:** Approved
**Owner:** Bill Walters

---

## Goal

Add HTTPS termination in front of MortgageDocAI without modifying any Python application code.
FastAPI continues to run unchanged on localhost. Caddy terminates TLS and reverse-proxies.

---

## Traffic Flow

```
LAN client
    │
    ▼  HTTPS :443  (TLS via Caddy internal CA, self-signed for .local)
  Caddy
    │
    ▼  HTTP  127.0.0.1:8000
  FastAPI (mortgagedocai-api.service)
```

---

## Constraints

- Do NOT modify any Python files.
- Do NOT change any API routes or response shapes.
- Modify `mortgagedocai-api.service` only to bind FastAPI to `127.0.0.1` (loopback).
- Add only reverse-proxy infrastructure.
- Must be idempotent and safe on Ubuntu 22.04 / 24.04.
- Do NOT implement Let's Encrypt until domain decision is confirmed.

---

## Components

### 1. Caddy (new)

Installed from Caddy's official apt repository (`stable` channel, keyed).
Managed by the system-provided `caddy.service` systemd unit.

**`/etc/caddy/Caddyfile`:**

```
{
    auto_https disable_redirects
}

mortgagedocai.local {
    reverse_proxy 127.0.0.1:8000
}
```

- `auto_https disable_redirects` — Caddy serves TLS on `:443` using its internal CA
  (self-signed cert for `mortgagedocai.local`); HTTP→HTTPS redirects on `:80` are
  suppressed. No Let's Encrypt attempted (`.local` is not publicly routable).
- `mortgagedocai.local` — placeholder hostname; will be updated once final domain
  is decided. Clients must resolve this name to the server IP (e.g. via `/etc/hosts`
  or local DNS).

### 2. `mortgagedocai-api.service` (one-line change)

Bind FastAPI to loopback only, closing port 8000 from the LAN:

```diff
-ExecStart=…/python3 scripts/loan_api.py --host 0.0.0.0 --port 8000
+ExecStart=…/python3 scripts/loan_api.py --host 127.0.0.1 --port 8000
```

Port 8000 remains reachable locally (e.g. for `curl` from the server itself).

---

## Install Steps (idempotent)

1. Add Caddy's official apt repo (keyed, `stable` channel) if not present.
2. `apt-get install -y caddy`
3. Write `/etc/caddy/Caddyfile` (overwrite if exists).
4. `systemctl enable --now caddy && systemctl reload-or-restart caddy`
5. Edit `mortgagedocai-api.service`: `--host 0.0.0.0` → `--host 127.0.0.1`
6. `systemctl daemon-reload && systemctl restart mortgagedocai-api`

---

## Verification Commands

```bash
# Confirm Caddy owns :443 and FastAPI is loopback-only
ss -tlnp | grep -E ':(443|8000)'

# HTTPS through Caddy (skip cert verify for self-signed)
curl -sk https://mortgagedocai.local/health | jq .

# Direct loopback still works (for server-local tooling)
curl -s http://127.0.0.1:8000/health | jq .

# Confirm port 8000 is NOT reachable from LAN
# (run from a different machine — should time out or be refused)
curl --max-time 3 http://10.10.10.190:8000/health

# Caddy logs
journalctl -u caddy -n 50 --no-pager
```

---

## Out of Scope (deferred)

- Let's Encrypt / ACME — deferred until public domain is confirmed.
- Firewall rules (ufw/iptables) — separate concern; port 8000 closure is handled by
  binding FastAPI to `127.0.0.1`.
- Client trust for self-signed cert — browsers will warn; add Caddy's root CA
  (`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`) to the OS/browser
  trust store to silence warnings.
- HTTP/2 push, compression, rate limiting — future Caddyfile enhancements.

---

## Files Changed

| Path | Change |
|---|---|
| `/etc/caddy/Caddyfile` | **Create** (new infra file, not in repo) |
| `mortgagedocai-api.service` | **Update** `--host` flag |
| `bootstrap_mortgagedocai.sh` | **No change** |
| Any Python file | **No change** |
