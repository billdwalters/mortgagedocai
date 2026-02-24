# Disk-Authoritative Job Reads + Remove API Daemon Threads

**Date:** 2026-02-23
**Branch:** `rc/v0.8-loan-service-refactor`
**Status:** Approved

---

## Goal

`mortgagedocai-api.service` must restart without losing job visibility. `GET /jobs/{job_id}` and `GET /jobs` must always reflect the current on-disk state written by `job_worker.py`. No response schema changes.

---

## Problem Statement

### What already works
- `loan_api.py` constructs `JobService` + `DiskJobStore` (no `JOBS`/`JOB_KEY_INDEX` globals)
- `_startup()` calls `_service.load_all_from_disk()` — jobs survive restart ✅
- `job_worker.py` is the correct long-running job executor with heartbeat ✅

### What is broken
1. **Three endpoints still spawn daemon threads** — `start_run_job`, `submit_query_job`, `submit_job` each do `threading.Thread(target=_service._run_worker, ...)`. These threads die on API restart and race with `job_worker.py`.
2. **`_startup()` step 4 resumes PENDING jobs via daemon threads** — races with `job_worker.py`.
3. **`get_job(job_id)` reads `self._jobs` (in-memory)** — stale once `job_worker.py` takes over writing to disk. Clients see PENDING indefinitely.
4. **`list_jobs()` reads `self._jobs` (in-memory)** — same staleness problem.
5. **`get_job(job_id)` uses in-memory cache to look up `tenant_id`/`loan_id`** — not restart-safe if the cache is incomplete.

---

## Architecture After Fix

```
POST /runs/start  ──► enqueue_job() ──► write PENDING to disk ──► write index entry ──► return {job_id, status}
                                                                                                    │
                                                              job_worker.py polls disk ◄────────────┘
                                                              claims job, writes RUNNING
                                                              runs subprocess
                                                              writes SUCCESS/FAIL to disk
                                                              writes heartbeat every cycle

GET /jobs/{id}    ──► load_index_entry(job_id) ──► load_job(tenant, loan, job_id) ──► return disk state
GET /jobs         ──► scan_all_raw()            ──► filter/sort/limit             ──► return disk state
```

The API is **enqueue-only**. `job_worker.py` is the **sole executor**.

---

## Design Decisions

### D1: Disk-based job_id → (tenant_id, loan_id) index

`get_job(job_id)` receives only a `job_id`. To load the correct job file it needs `tenant_id` and `loan_id` (the file is at `tenants/{tid}/loans/{lid}/_meta/jobs/{job_id}.json`). We cannot rely on the in-memory `_jobs` dict (not restart-safe without prior load).

**Solution:** A per-job index file at `{NAS_ANALYZE}/_meta/job_index/{job_id}.json`:
```json
{"tenant_id": "peak", "loan_id": "16271681"}
```

Written at enqueue time (atomic). Rebuilt for existing jobs during `load_all_from_disk()`. This makes `get_job()` fully restart-safe with 2 sequential disk reads (index + job file) — no in-memory dependency.

### D2: scan_all_raw() for list_jobs()

`list_jobs()` needs a full disk scan without recovery logic (RUNNING jobs must stay RUNNING, not be marked FAIL). A new `DiskJobStore.scan_all_raw(limit)` method reads all `*.json` files under `tenants/*/loans/*/_meta/jobs/` and returns them as-is.

### D3: Keep orphaned systemd-scope watcher

`_startup()` steps 1–3 (orphaned systemd-scope detection + watcher threads) are kept. They guard against the transition period when old-style daemon threads may have been active during the last service run.

### D4: Heartbeat warning location

`job_worker.py` already writes `{NAS_ANALYZE}/_meta/worker_heartbeat.json` with `{"heartbeat_utc": "..."}`. Confirmed at `job_worker.py:43`. The API reads this same path.

---

## Files Changed

### 1. `scripts/loan_service/adapters_disk.py`

**Add three methods to `DiskJobStore`:**

**`_job_index_dir()`** — returns `{NAS_ANALYZE}/_meta/job_index/`

**`save_index_entry(job_id, tenant_id, loan_id)`**:
- Writes `_meta/job_index/{job_id}.json` → `{"tenant_id": "...", "loan_id": "..."}`
- Atomic: write to `.tmp`, then `os.replace()`
- Called by `enqueue_job()` and `load_all_from_disk()`

**`load_index_entry(job_id)`** → `tuple[str, str] | None`:
- Reads `_meta/job_index/{job_id}.json`
- Returns `(tenant_id, loan_id)` or `None` on any error

**`scan_all_raw(limit)`** → `list[dict]`:
- Walks `tenants/*/loans/*/_meta/jobs/*.json`
- Returns each job as-is from disk (NO recovery, RUNNING stays RUNNING)
- Sorted by `created_at_utc` descending, capped at `limit` (default: `JOB_RELOAD_LIMIT`)

**Modify `load_all()`**:
- After loading/recovering each job, call `save_index_entry(job_id, tenant_id, loan_id)` if index file is absent
- This is the migration path for jobs created before this change

### 2. `scripts/loan_service/service.py`

**`enqueue_job()`**: after `self._store.save(job)`, add:
```python
self._store.save_index_entry(job_id, tenant_id, loan_id)
```

**`get_job(job_id)`**: full replacement — disk-authoritative, no in-memory state for correctness:
```python
def get_job(self, job_id: str) -> dict[str, Any] | None:
    entry = self._store.load_index_entry(job_id)
    if entry is None:
        return None
    tenant_id, loan_id = entry
    job = self._store.load_job(tenant_id, loan_id, job_id)
    if job is None:
        return None
    return {k: v for k, v in job.items() if v is not None and k != "job_key"}
```

**`list_jobs(limit, status)`**: disk-authoritative scan:
```python
def list_jobs(self, limit=50, status=None):
    raw = self._store.scan_all_raw(limit=JOB_RELOAD_LIMIT)
    if status:
        raw = [j for j in raw if j.get("status") == status]
    raw = sorted(raw, key=lambda j: j.get("created_at_utc") or "", reverse=True)[:limit]
    return {"jobs": [{k: v for k, v in j.items() if v is not None and k != "job_key"} for j in raw]}
```

### 3. `scripts/loan_api.py`

**Remove** daemon thread spawns (same pattern in all 3 endpoints):
```python
# DELETE these blocks from start_run_job, submit_query_job, submit_job:
if result.get("status") == "PENDING":
    t = threading.Thread(
        target=_service._run_worker, args=(result["job_id"],), daemon=True
    )
    t.start()
```

**Remove** step 4 from `_startup()`:
```python
# DELETE:
# 4. Resume PENDING jobs (e.g. queued before this restart).
with _service._lock:
    pending_ids = [jid for jid, j in _service._jobs.items() if j.get("status") == "PENDING"]
for job_id in pending_ids:
    t = threading.Thread(target=_service._run_worker, args=(job_id,), daemon=True)
    t.start()
```

**Add** `_warn_if_no_recent_worker_heartbeat()` — define once, call after each enqueue:
```python
WORKER_HEARTBEAT_MAX_AGE_SEC = 300

def _warn_if_no_recent_worker_heartbeat() -> None:
    """Log-only; no response schema change."""
    hb = _get_base_path() / "_meta" / "worker_heartbeat.json"
    if not hb.exists():
        print("[loan_api] no worker heartbeat; job queued but job_worker.py may not be running",
              file=sys.stderr)
        return
    try:
        if time.time() - hb.stat().st_mtime > WORKER_HEARTBEAT_MAX_AGE_SEC:
            print("[loan_api] worker heartbeat stale (>5 min); job_worker.py may be down",
                  file=sys.stderr)
    except OSError:
        pass
```

### 4. `scripts/job_worker.py`

**No changes.** `_write_heartbeat()` already writes `{NAS_ANALYZE}/_meta/worker_heartbeat.json` on every poll cycle.

---

## Constraints Verified

| Constraint | Status |
|-----------|--------|
| No HTTP route path changes | ✅ all routes identical |
| No JobRecord JSON key changes | ✅ `get_job()` filters same keys |
| Idempotency unchanged | ✅ `enqueue_job()` logic untouched |
| PHASE lines unchanged | ✅ no stdout handling changed |
| Security middleware unchanged | ✅ `_SecurityMiddleware` untouched |
| Minimal diff | ✅ ~4 surgical changes, 3 files |

---

## Acceptance Tests

```bash
# 1. Compile check
python3 -m py_compile scripts/loan_api.py scripts/loan_service/*.py

# 2. Unit/hardening tests
cd M:/mortgagedocai && python scripts/test_job_hardening.py

# 3. Confirm daemon threads are gone
grep -n "threading.Thread.*_run_worker" scripts/loan_api.py
# must return nothing

# 4. OpenAPI contains artifact endpoints
curl -s http://localhost:8000/openapi.json | python3 -c "
import json, sys
paths = json.load(sys.stdin)['paths']
required = [
  '/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/artifacts',
  '/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/artifacts/{profile}/{filename}',
  '/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/retrieval_pack',
  '/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/job_manifest',
]
missing = [p for p in required if p not in paths]
print('MISSING:', missing) if missing else print('All artifact paths present ✓')
"

# 5. Restart persistence (manual, on production server)
# a. Start job
curl -s -X POST http://localhost:8000/tenants/peak/loans/16271681/runs/start \
  -H 'Content-Type: application/json' \
  -d '{"source_path":"/mnt/source_loans/5-Borrowers TBD/[Loan 16271681] ..."}' \
  | python3 -m json.tool
# note job_id; verify keys: job_id, run_id, status, status_url

# b. While RUNNING, restart the service
sudo systemctl restart mortgagedocai-api.service

# c. Confirm job record survives (not 404)
curl -s http://localhost:8000/jobs/{job_id} | python3 -m json.tool
# status must be RUNNING (or SUCCESS/FAIL if job completed during restart)
# must NOT be 404

# d. Confirm artifacts after completion
curl -s http://localhost:8000/tenants/peak/loans/16271681/runs/{run_id}/artifacts | python3 -m json.tool
# must return 200
```

---

## Non-Goals

- Not removing `_run_worker()` from `service.py` (kept for test compatibility and future use)
- Not changing `api_router.py` (it has no daemon threads; its `submit_*` routes already call `_warn_if_no_recent_worker_heartbeat`)
- Not adding new tests (existing `test_job_hardening.py` suite covers the changes)
