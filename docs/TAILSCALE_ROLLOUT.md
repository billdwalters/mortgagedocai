# Tailscale Rollout — MortgageDocAI

## Purpose & Scope

MortgageDocAI processes mortgage documents that contain **PII and sensitive financial data**. The API must never be reachable from the public internet. This document describes how to roll out Tailscale as the sole remote-access path for office and remote users.

**Security posture:**
- The API binds to `127.0.0.1:8000` — no LAN exposure, no WAN exposure.
- Caddy terminates HTTPS on `:443` for `mortgagedocai.local` (LAN only, internal CA).
- Remote access (off-site or across office VLANs) is via Tailscale tailnet only.
- Port 8000 is never port-forwarded. Port 443 is never exposed to the internet.

---

## 1. Server Setup

### Install Tailscale on the AI server (`10.10.10.190`)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# Follow the auth URL printed — log in with your Tailscale account
```

### Verify

```bash
tailscale status          # should show server node as "online"
tailscale ip -4           # note the 100.x.x.x tailnet IP
```

### (Optional) Tailscale Serve — expose API on tailnet HTTPS

If you want clients to reach the API at `https://<hostname>.ts.net` without needing Caddy or a hosts file entry:

```bash
sudo tailscale serve --bg https+insecure://127.0.0.1:8000
# Clients can now curl https://<hostname>.ts.net/health
```

To remove Serve:
```bash
sudo tailscale serve off
```

---

## 2. Client Setup (office + remote users)

### Install Tailscale

| Platform | Method |
|---|---|
| Windows | [tailscale.com/download](https://tailscale.com/download) → installer |
| macOS | App Store or `brew install tailscale` |
| Linux | `curl -fsSL https://tailscale.com/install.sh \| sh` |
| iOS / Android | App Store / Play Store |

### Connect to tailnet

1. Open Tailscale → **Log in** with the shared organization account (or invite link).
2. Approve the device in the [Tailscale admin console](https://login.tailscale.com/admin) if device approval is enabled.
3. Confirm connectivity:

```bash
# Ping the server's tailnet IP (replace with actual 100.x.x.x)
ping 100.x.x.x

# Or use the MagicDNS hostname if enabled
ping aiserver
```

### Access the API

```bash
# Direct tailnet IP
curl -H "X-API-Key: <key>" http://100.x.x.x:8000/health

# Via Tailscale Serve (if configured on server)
curl -H "X-API-Key: <key>" https://aiserver.ts.net/health

# Web UI
http://100.x.x.x:8000/ui
```

> **Note:** Port 8000 is only reachable via tailnet because the server's firewall blocks LAN access to 8000. If Tailscale Serve is configured, prefer `https://aiserver.ts.net`.

---

## 3. Verification Steps

Run these after connecting each new client:

```bash
# 1. Tailscale is connected
tailscale status | grep aiserver

# 2. Can reach the server tailnet IP
ping -c 3 100.x.x.x

# 3. API responds through tailnet
curl -s -o /dev/null -w "%{http_code}" \
    -H "X-API-Key: <key>" http://100.x.x.x:8000/health
# expect: 200

# 4. Port 8000 is NOT reachable from LAN (should time out / refuse)
curl --connect-timeout 3 http://10.10.10.190:8000/health
# expect: connection refused or timeout
```

---

## 4. Recommended Security Posture

### Tailscale ACLs

In the [Tailscale admin console](https://login.tailscale.com/admin/acls), restrict which devices can reach the AI server on port 8000:

```json
{
  "acls": [
    {
      "action": "accept",
      "src": ["tag:mortgagedocai-users"],
      "dst": ["tag:mortgagedocai-server:8000"]
    }
  ],
  "tagOwners": {
    "tag:mortgagedocai-server": ["autogroup:admin"],
    "tag:mortgagedocai-users": ["autogroup:admin"]
  }
}
```

Tag the server node as `mortgagedocai-server` and approved user devices as `mortgagedocai-users` in the admin console.

### Key-Auth on the API

Set `MORTGAGEDOCAI_API_KEY` in `/etc/systemd/system/mortgagedocai-api.service`:

```ini
Environment=MORTGAGEDOCAI_API_KEY=<strong-random-key>
```

Then: `sudo systemctl daemon-reload && sudo systemctl restart mortgagedocai-api`

All requests must include `X-API-Key: <key>`; missing or wrong key returns `401 Unauthorized`.

### What NOT to do

- ❌ Do not port-forward 8000 or 443 on the router to the internet.
- ❌ Do not re-bind the API to `0.0.0.0` — defeats the loopback-only posture.
- ❌ Do not share the Tailscale auth key publicly.
- ❌ Do not disable key auth while Tailscale Serve is active.

---

## 5. Optional: Tailscale Serve Details

Tailscale Serve proxies a local service to the tailnet with a valid HTTPS cert (no self-signed warnings):

```bash
# Front the FastAPI at https://aiserver.ts.net
sudo tailscale serve --bg https+insecure://127.0.0.1:8000

# Check current serve config
tailscale serve status

# Remove serve
sudo tailscale serve off
```

Clients access `https://aiserver.ts.net/health` — Tailscale issues the cert from its own CA, trusted by all tailnet devices automatically.

---

## 6. Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| `ping 100.x.x.x` fails | `tailscale status` on both ends | Re-authenticate: `sudo tailscale up` |
| Connection refused on port 8000 | `ss -tlnp \| grep 8000` on server | API must show `127.0.0.1:8000`; access only via tailnet IP |
| `401 Unauthorized` on all requests | API key set? | Pass `-H "X-API-Key: <key>"` header |
| `502 Bad Gateway` via Tailscale Serve | Is FastAPI running? | `sudo systemctl status mortgagedocai-api` |
| Can reach 10.10.10.190:8000 from LAN | Firewall not blocking 8000 | Add `ufw deny 8000` or equivalent; confirm API bind is `127.0.0.1` |
| Device not in tailnet | Not approved | Approve in [admin console](https://login.tailscale.com/admin/machines) |
| MagicDNS hostname not resolving | MagicDNS disabled | Enable in admin console → DNS → MagicDNS |
| Tailscale Serve not working after reboot | `--bg` flag wasn't used | Re-run with `--bg` or add to startup |
