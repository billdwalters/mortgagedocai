# Backup & Off-Site Topology Plan

**Status:** Design / pre-migration
**Last updated:** 2026-04-06
**Scope:** Off-site backup architecture for the MortgageDocAI stack, to be implemented during/after migration to the new Dell R750 AI server.

---

## Goals

- Local-only, no cloud APIs (matches project contract).
- One-way, pull-based backups — a compromise of any working box cannot reach into the backup vault.
- Off-site (physically different location) for fire/theft/flood resilience.
- Defense in depth: ZFS snapshots on the backup target as ransomware/fat-finger insurance.
- Sized for retention growth, not just current footprint.

---

## The cast (4 boxes)

| # | Box | Role | OS | Location |
|---|---|---|---|---|
| 1 | **OrigSyno** | Customer's existing Synology — source of truth for loan docs | DSM | Customer site |
| 2 | **NewSyno** | Working document store; serves files to AI server | DSM | Your site |
| 3 | **AI-R750** | Dell PowerEdge R750, dual CPU, 512 GB RAM, 1 TB boot + 2× 1 TB SSD mirror; runs MortgageDocAI, Qdrant, Ollama | Ubuntu | Your site |
| 4 | **BackupNAS** | TrueNAS SCALE — paranoid backup vault | TrueNAS | **Off-site (different building)** |

All four meshed via **Tailscale**. No public ports anywhere.

---

## Data flow diagram

```
                    ┌─────────────────┐
                    │    OrigSyno     │  (customer, source of truth)
                    └────────▲────────┘
                             │  ① NewSyno PULLS via Snapshot Replication
                             │     (read-only on OrigSyno side)
                    ┌────────┴────────┐
                    │     NewSyno     │  (working store)
                    └────────▲────────┘
                             │  ② AI-R750 mounts NFS export, READ-ONLY
                             │     for /mnt/source_loans
                    ┌────────┴────────┐
                    │    AI-R750      │  (compute: Qdrant, Ollama, pipeline)
                    └────────▲────────┘
                             │
                             │  ③ BackupNAS PULLS from AI-R750
                             │     (rsync over SSH, restricted via rrsync)
                             │
                             │  ④ BackupNAS PULLS from NewSyno
                             │     (Synology native rsync module, read-only)
                    ┌────────┴───────┐
                    │   BackupNAS    │  (off-site, ZFS snapshots)
                    └────────────────┘
```

**Critical property:** arrows point *up*. Every backup is initiated by the destination, not the source. A compromise of any box below cannot reach the box above to delete backups.

---

## Flow-by-flow detail

### ① OrigSyno → NewSyno (customer source → your working store)
- **Direction:** NewSyno **pulls** from OrigSyno using Synology **Snapshot Replication**.
- **Why pull:** OrigSyno is the customer's box; it should not have credentials *into* your environment.
- **Credentials:** Dedicated DSM user on OrigSyno, read-only on the source share, used only by NewSyno's replication task.
- **Frequency:** Hourly is reasonable for active loans.
- **Gotcha:** Folders you create on NewSyno will *not* exist on OrigSyno and so are *not* protected by this flow. Flow ④ catches them.

### ② NewSyno → AI-R750 (working store → compute)
- **Direction:** AI-R750 **mounts** NewSyno's share (this is access, not backup — no copy at rest).
- **Protocol:** **NFSv4** preferred over SMB for Linux clients.
- **Mount mode:** **read-only** for `/mnt/source_loans` (matches existing project contract).
- **Credentials:** NFS export restricted to AI-R750's Tailscale IP, prefer `root_squash`.
- **Failure mode:** If NewSyno is down, AI server can't ingest *new* loans, but already-ingested data in `/mnt/nas_apps` is unaffected.

### ③ BackupNAS → AI-R750 (off-site vault pulls from compute)
- **Direction:** **BackupNAS initiates.** This is the most important inversion.
- **How:** **rsync pull + ZFS snapshots** on the BackupNAS side. Restic is *not* used here because the target is ZFS — ZFS snapshots are simpler and faster than restic when the target supports them natively.
- **Sketch (runs on BackupNAS):**
  ```bash
  rsync -aAXH --delete --numeric-ids \
        backup@ai-r750:/etc /root /var/lib/qdrant/snapshots /opt/mortgagedocai \
        /mnt/backup-pool/ai-r750/
  zfs snapshot backup-pool/ai-r750@$(date +%Y%m%d-%H%M)
  ```
- **Retention (TrueNAS UI):** hourly×24, daily×30, weekly×12, monthly×12.
- **Credentials:** Dedicated `backup` user on AI-R750, SSH key from BackupNAS, `authorized_keys` line restricted with `command="rrsync -ro /"` so the key can only do **read-only rsync**, nothing else.
- **AI-R750 has zero credentials pointing at BackupNAS.**

### ④ BackupNAS → NewSyno (off-site vault pulls from working store)
- **Direction:** BackupNAS **pulls** from NewSyno.
- **How:** Synology's native rsync server (Control Panel → File Services → rsync). Read-only module, IP-restricted to BackupNAS Tailscale IP.
- **Sketch (runs on BackupNAS):**
  ```bash
  rsync -aAXH --delete \
        rsync://backup@newsyno/loans/ \
        /mnt/backup-pool/newsyno-loans/
  zfs snapshot backup-pool/newsyno-loans@$(date +%Y%m%d-%H%M)
  ```
- **Why this matters:** catches the "new folders on NewSyno that don't exist on OrigSyno" gap.
- **Frequency:** Daily.

---

## Credentials matrix

| From → To | Protocol | Credential lives on | Permission | Trust direction |
|---|---|---|---|---|
| NewSyno → OrigSyno | Snapshot Replication / rsync | NewSyno | RO on source shares | NewSyno trusts OrigSyno |
| AI-R750 → NewSyno | NFSv4 | (IP-based, no creds) | RO mount | AI-R750 trusts NewSyno |
| BackupNAS → AI-R750 | SSH + rrsync | BackupNAS | RO via `command=` restriction | BackupNAS trusts AI-R750 |
| BackupNAS → NewSyno | rsync (Synology native) | BackupNAS | RO rsync module | BackupNAS trusts NewSyno |
| **Anything → BackupNAS** | **NONE** | — | — | **BackupNAS trusts no one** |

The last row is the entire point. Nothing initiates a connection *into* BackupNAS except the admin laptop over Tailscale SSH.

---

## Tailscale ACL sketch

```jsonc
{
  "tagOwners": {
    "tag:origsyno": ["you@example.com"],
    "tag:newsyno":  ["you@example.com"],
    "tag:ai":       ["you@example.com"],
    "tag:backup":   ["you@example.com"],
    "tag:admin":    ["you@example.com"]
  },
  "acls": [
    { "action": "accept", "src": ["tag:newsyno"], "dst": ["tag:origsyno:22,873"] },
    { "action": "accept", "src": ["tag:ai"],      "dst": ["tag:newsyno:2049,111"] },
    { "action": "accept", "src": ["tag:backup"],  "dst": ["tag:ai:22"] },
    { "action": "accept", "src": ["tag:backup"],  "dst": ["tag:newsyno:873"] },
    { "action": "accept", "src": ["tag:admin"],   "dst": ["*:*"] }
  ]
}
```

Note what's missing: no `tag:ai → tag:backup` rule. The AI server literally cannot open a connection to the backup vault.

---

## Where does `nas_chunk` / `nas_analyze` live in the new topology?

Currently those live on the existing TrueNAS at `/mnt/nas_apps/{nas_chunk,nas_analyze}`. In the new topology:

**Decision: put them on NewSyno**, alongside loan documents.

Rationale:
- Keeps AI-R750 stateless-ish (only Qdrant + Ollama + OS + code on local SSD).
- Backup story is uniform: NewSyno is the working store, BackupNAS pulls from it via flow ④.
- Clean future migration: replace the AI compute box, mount NewSyno, pull repo, running.

Do **not** put them on BackupNAS — never make the backup target also be live storage.

---

## Qdrant snapshot strategy

### The core problem
Qdrant data lives in `/var/lib/qdrant/storage/`. Rsyncing it while Qdrant is running will *probably* work *most* of the time — and will eventually produce a corrupt restore. Same class of bug as backing up a running Postgres data dir. You need a **consistent snapshot**, not a file copy.

### What Qdrant gives you
| Type | Scope | Endpoint |
|---|---|---|
| Collection snapshot | One collection | `POST /collections/{name}/snapshots` |
| Full storage snapshot | All collections | `POST /snapshots` |

Snapshots land in `/var/lib/qdrant/snapshots/`, are internally consistent (Qdrant handles locking), and the resulting file is safe to copy while Qdrant keeps running.

### Decision: snapshot it
The "rebuild from `chunks.jsonl`" path is always available as fallback (Step 11 re-embed), so this is defense in depth. But snapshot-and-restore is minutes vs. hours of re-embedding, and the script is small. Worth it.

**Record in CLAUDE.md:** "Qdrant is backed up via nightly snapshot; fallback recovery is re-embedding from `chunks.jsonl`."

### Two-stage design
1. **Stage 1 (on AI-R750):** Qdrant takes a consistent snapshot to its local snapshot dir at 02:00.
2. **Stage 2 (on BackupNAS):** dumb file copy of an already-consistent file at 03:00 (part of flow ③).

Decoupled in time so a slow/failed rsync doesn't break the snapshot itself.

### Snapshot script (on AI-R750)

`/usr/local/sbin/qdrant-snapshot`:
```bash
#!/bin/bash
set -euo pipefail

QDRANT_URL="http://localhost:6333"
COLLECTION="peak_e5largev2_1024_cosine_v1"
SNAPSHOT_DIR="/var/lib/qdrant/snapshots"
KEEP_DAYS=7

log() { echo "[$(date -Iseconds)] $*"; }

# 1. Health check — refuse to snapshot an unhealthy Qdrant
if ! curl -fsS "$QDRANT_URL/healthz" >/dev/null; then
    log "FAIL: Qdrant not healthy at $QDRANT_URL"
    exit 1
fi

# 2. Verify collection exists (fail loud, per project contract)
if ! curl -fsS "$QDRANT_URL/collections/$COLLECTION" >/dev/null; then
    log "FAIL: collection $COLLECTION not found"
    exit 1
fi

# 3. Trigger snapshot
log "Creating snapshot of $COLLECTION..."
RESPONSE=$(curl -fsS -X POST "$QDRANT_URL/collections/$COLLECTION/snapshots")
SNAPSHOT_NAME=$(echo "$RESPONSE" | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["name"])')

if [ -z "$SNAPSHOT_NAME" ]; then
    log "FAIL: snapshot API returned no name. Response: $RESPONSE"
    exit 1
fi
log "Snapshot created: $SNAPSHOT_NAME"

# 4. Verify file landed
SNAPSHOT_PATH="$SNAPSHOT_DIR/$COLLECTION/$SNAPSHOT_NAME"
if [ ! -f "$SNAPSHOT_PATH" ]; then
    log "FAIL: snapshot file missing at $SNAPSHOT_PATH"
    exit 1
fi

SIZE=$(stat -c %s "$SNAPSHOT_PATH")
log "Snapshot size: $((SIZE / 1024 / 1024)) MB"

# 5. Sanity floor — catches "empty collection" disasters
MIN_SIZE=$((10 * 1024 * 1024))   # 10 MB; tune to real baseline
if [ "$SIZE" -lt "$MIN_SIZE" ]; then
    log "FAIL: snapshot suspiciously small ($SIZE bytes)"
    exit 1
fi

# 6. Prune old local snapshots (BackupNAS holds long-term history)
find "$SNAPSHOT_DIR/$COLLECTION" -name "*.snapshot" -mtime +$KEEP_DAYS -print -delete

log "OK: snapshot complete"
```

### Why each guard exists
- **Health check first:** snapshotting an unhealthy Qdrant produces a "snapshot" of the corrupt state.
- **Collection existence:** matches the project's "fail loud" rule.
- **File-landed check:** the API returns success when queued, not always when flushed.
- **Size floor:** catches the "Qdrant up but collection empty after fat-fingered delete" case. Without this, you back up nothing for a week and only notice on restore.
- **Short local retention (7 days):** long-term retention is BackupNAS's job via ZFS snapshots.

### Systemd wiring

`/etc/systemd/system/qdrant-snapshot.service`:
```ini
[Unit]
Description=Qdrant collection snapshot
After=qdrant.service
Requires=qdrant.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/qdrant-snapshot
StandardOutput=journal
StandardError=journal
```

`/etc/systemd/system/qdrant-snapshot.timer`:
```ini
[Unit]
Description=Nightly Qdrant snapshot

[Timer]
OnCalendar=*-*-* 02:00:00
RandomizedDelaySec=10m
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now qdrant-snapshot.timer
sudo systemctl start qdrant-snapshot.service   # test run
journalctl -u qdrant-snapshot.service -n 50
```

### Restore drill (quarterly, calendar reminder)
```bash
# 1. From BackupNAS: copy snapshot back
rsync -av /mnt/backup-pool/ai-r750/qdrant-snapshots/peak_e5largev2_1024_cosine_v1/peak-2026-04-05-0200.snapshot \
      ai-r750:/tmp/restore.snapshot

# 2. On AI-R750: ask Qdrant to restore
curl -X PUT "http://localhost:6333/collections/peak_e5largev2_1024_cosine_v1/snapshots/recover" \
  -H "Content-Type: application/json" \
  -d '{"location": "file:///tmp/restore.snapshot"}'

# 3. Verify points_count + sample query
curl -fsS http://localhost:6333/collections/peak_e5largev2_1024_cosine_v1 | python3 -m json.tool
```

**Restore to a *test* Qdrant instance (different port or container), not prod.**

### Things that bite people
1. **Snapshot dir grows fast** without pruning — each snapshot ≈ collection size. The script handles it; double-check free space if you raise `KEEP_DAYS`.
2. **Per-collection scope:** if you add another collection later, either loop the script over collections or switch to the full `POST /snapshots` endpoint. Update CLAUDE.md when this changes.
3. **Snapshot ≠ replication:** point-in-time. Anything ingested between snapshot and disaster must be re-run via Step 11.

---

## Sizing notes for BackupNAS

- Rule of thumb: **working_set × ~4** to give snapshot retention room.
- ZFS compression helps a lot on PDFs/text, less on already-compressed images.
- Don't undersize. Running out of space is how backups silently stop working.
- Prefer mirrors or RAIDZ2 for the backup pool — single-disk ZFS gives you checksums but no recovery.
- Enable scheduled scrubs (TrueNAS default is fine).

---

## Pre-flight checklist (before declaring backup story done)

- [ ] BackupNAS at a **physically different location** from everything else.
- [ ] BackupNAS **initiates** all backup jobs. Nothing pushes *to* it.
- [ ] BackupNAS has **ZFS snapshots with retention** on every backup dataset.
- [ ] AI-R750's `backup` SSH user restricted via `command="rrsync -ro /"`.
- [ ] NewSyno's rsync module is read-only and IP-restricted to BackupNAS.
- [ ] BackupNAS sized for working_set × ~4.
- [ ] Documented answer: "Qdrant — backed up or regenerated?" → backed up via snapshot, fallback re-embed.
- [ ] Documented answer: "What lives only on NewSyno and not OrigSyno?" → covered by flow ④.
- [ ] `nas_chunk` / `nas_analyze` location decided (recommendation: NewSyno).
- [ ] `qdrant-snapshot` script + systemd timer deployed and tested on AI-R750.
- [ ] `MIN_SIZE` floor tuned to real collection baseline.
- [ ] Restore drill performed end-to-end at least once.
- [ ] Quarterly restore drill on the calendar.
- [ ] Tailscale ACLs enforce the trust direction (no `ai → backup` rule).

---

## Open items / next steps

1. **Migration in progress:** linked + installed on new R750. GPU install causing crashes — possibly reseating issue per other Claude session. Resolve before backup wiring.
2. **Write rsync + ZFS snapshot scripts for flow ③** (BackupNAS pulling from AI-R750). Pending.
3. **Write rsync flow ④** (BackupNAS pulling from NewSyno). Pending.
4. **Tune `MIN_SIZE`** in `qdrant-snapshot` after first real snapshot on the new server.
5. **Decide hourly vs daily** for OrigSyno → NewSyno replication based on customer SLA.

---

## Related project context
- See `CLAUDE.md` for pipeline architecture and non-negotiables.
- See `MortgageDocAI_CONTRACT.md` for authoritative folder/path contracts.
- See memory: `project_server_topology_plan.md` for broader server migration plan.
