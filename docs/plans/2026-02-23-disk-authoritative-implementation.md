# Disk-Authoritative Job Reads + Remove API Daemon Threads

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** `GET /jobs/{job_id}` and `GET /jobs` always reflect current on-disk state from `job_worker.py`; API never runs jobs in daemon threads.

**Architecture:** Add a per-job disk index (`_meta/job_index/{job_id}.json`) so `get_job()` can load directly from disk without touching the in-memory cache. Add `scan_all_raw()` to `DiskJobStore` for cacheless `list_jobs()`. Remove the three daemon-thread spawn blocks from `loan_api.py` and the startup PENDING-resumption step.

**Tech Stack:** Python 3.11, FastAPI, pathlib, json, os.replace (atomic writes)

**Branch:** `rc/v0.8-loan-service-refactor`
**Repo root:** `M:/mortgagedocai` (git add/commit from here)
**Compile check command:** `cd M:/mortgagedocai && python -c "import sys; sys.path.insert(0,'scripts'); from loan_service import adapters_disk, service; import loan_api; print('ok')"`
**Test command:** `cd M:/mortgagedocai && python scripts/test_job_hardening.py`

---

## Task 1: Add `save_index_entry()` and `load_index_entry()` to `DiskJobStore`

**Files:**
- Modify: `scripts/loan_service/adapters_disk.py` — add two methods inside `DiskJobStore`, after `save()` (line 307)
- Test: `scripts/test_job_hardening.py` — add `test_disk_index_roundtrip()`

**Step 1: Add the test to `test_job_hardening.py`**

Find `if __name__ == "__main__":` at line 147. Insert this new test function **immediately before** that block:

```python
def test_disk_index_roundtrip():
    """save_index_entry writes; load_index_entry reads back correctly (no in-memory cache)."""
    import uuid as _uuid
    from loan_service.adapters_disk import DiskJobStore
    nas = _tmp_nas()
    store = DiskJobStore(lambda: nas)
    job_id = str(_uuid.uuid4())
    # Write and read back
    store.save_index_entry(job_id, "t1", "L1")
    result = store.load_index_entry(job_id)
    assert result == ("t1", "L1"), f"expected ('t1','L1'), got {result}"
    # Unknown job_id returns None
    assert store.load_index_entry("no-such-id") is None
    print("test_disk_index_roundtrip OK")
```

Also add it to the `if __name__ == "__main__":` block (before the final `print`):

```python
if __name__ == "__main__":
    test_idempotency_same_job_id()
    test_restart_recovery_running_becomes_fail()
    test_per_loan_lock_second_waits()
    test_worker_processes_one_queued_job()
    test_disk_index_roundtrip()          # ← add this line
    print("All hardening tests passed.")
```

**Step 2: Run test to verify it FAILS**

```bash
cd M:/mortgagedocai && python -c "
import sys; sys.path.insert(0,'scripts')
from test_job_hardening import test_disk_index_roundtrip
test_disk_index_roundtrip()
"
```

Expected: `AttributeError: 'DiskJobStore' object has no attribute 'save_index_entry'`

**Step 3: Add two methods to `DiskJobStore` in `adapters_disk.py`**

In `scripts/loan_service/adapters_disk.py`, after the `save()` method (after line 321, before `class JobKeyIndexImpl`), insert:

```python
    # ------------------------------------------------------------------
    # Job-ID index: _meta/job_index/{job_id}.json → {tenant_id, loan_id}
    # Enables get_job(job_id) to load from disk without any in-memory cache.
    # ------------------------------------------------------------------

    def _job_index_dir(self) -> Path:
        return self._get_base() / "_meta" / "job_index"

    def save_index_entry(self, job_id: str, tenant_id: str, loan_id: str) -> None:
        """Atomically write _meta/job_index/{job_id}.json → {tenant_id, loan_id}."""
        try:
            idx_dir = self._job_index_dir()
            idx_dir.mkdir(parents=True, exist_ok=True)
            path = idx_dir / f"{job_id}.json"
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w") as f:
                json.dump({"tenant_id": tenant_id, "loan_id": loan_id}, f)
            os.replace(tmp, path)
        except OSError as e:
            print(f"[job_runner] index write failed {job_id}: {e}", file=sys.stderr)

    def load_index_entry(self, job_id: str) -> tuple[str, str] | None:
        """Return (tenant_id, loan_id) from index, or None if not found."""
        path = self._job_index_dir() / f"{job_id}.json"
        try:
            with path.open() as f:
                data = json.load(f)
            tid = data.get("tenant_id")
            lid = data.get("loan_id")
            if tid and lid:
                return (tid, lid)
        except (OSError, json.JSONDecodeError, KeyError):
            pass
        return None
```

**Step 4: Run test to verify it PASSES**

```bash
cd M:/mortgagedocai && python -c "
import sys; sys.path.insert(0,'scripts')
from test_job_hardening import test_disk_index_roundtrip
test_disk_index_roundtrip()
"
```

Expected: `test_disk_index_roundtrip OK`

**Step 5: Compile check**

```bash
cd M:/mortgagedocai && python -c "import sys; sys.path.insert(0,'scripts'); from loan_service import adapters_disk; print('ok')"
```

Expected: `ok`

**Step 6: Commit**

```bash
git -C //10.10.10.190/opt/mortgagedocai add scripts/loan_service/adapters_disk.py scripts/test_job_hardening.py
git -C //10.10.10.190/opt/mortgagedocai commit -m "feat(disk): add save_index_entry/load_index_entry to DiskJobStore"
```

---

## Task 2: Add `scan_all_raw()` to `DiskJobStore`

**Files:**
- Modify: `scripts/loan_service/adapters_disk.py` — add `scan_all_raw()` inside `DiskJobStore` after `load_index_entry()`
- Test: `scripts/test_job_hardening.py` — add `test_scan_all_raw_no_recovery()`

**Step 1: Add the test**

Insert before `if __name__ == "__main__":`:

```python
def test_scan_all_raw_no_recovery():
    """scan_all_raw returns jobs as-is: RUNNING stays RUNNING (no manifest recovery)."""
    import json as _json, uuid as _uuid
    from loan_service.adapters_disk import DiskJobStore
    nas = _tmp_nas()
    job_dir = nas / "tenants" / "t1" / "loans" / "L1" / "_meta" / "jobs"
    job_dir.mkdir(parents=True)
    job_id = str(_uuid.uuid4())
    job = {
        "job_id": job_id, "tenant_id": "t1", "loan_id": "L1", "run_id": None,
        "status": "RUNNING",   # should NOT be recovered to FAIL
        "created_at_utc": "2026-01-01T00:00:00Z",
        "started_at_utc": "2026-01-01T00:00:01Z",
        "finished_at_utc": None, "request": {}, "result": None, "error": None,
        "stdout": None, "stderr": None,
    }
    (job_dir / f"{job_id}.json").write_text(_json.dumps(job))
    store = DiskJobStore(lambda: nas)
    raw = store.scan_all_raw()
    found = [j for j in raw if j.get("job_id") == job_id]
    assert len(found) == 1, f"expected 1 result, got {len(found)}"
    assert found[0]["status"] == "RUNNING", f"expected RUNNING, got {found[0]['status']}"
    print("test_scan_all_raw_no_recovery OK")
```

Add `test_scan_all_raw_no_recovery()` to `if __name__ == "__main__":`.

**Step 2: Run test to verify it FAILS**

```bash
cd M:/mortgagedocai && python -c "
import sys; sys.path.insert(0,'scripts')
from test_job_hardening import test_scan_all_raw_no_recovery
test_scan_all_raw_no_recovery()
"
```

Expected: `AttributeError: 'DiskJobStore' object has no attribute 'scan_all_raw'`

**Step 3: Add `scan_all_raw()` to `DiskJobStore` in `adapters_disk.py`**

Insert after `load_index_entry()` (after the index methods added in Task 1):

```python
    def scan_all_raw(self, limit: int = JOB_RELOAD_LIMIT) -> list[dict[str, Any]]:
        """Scan all job JSON files; return as-is with NO recovery logic applied.

        Uses the same mtime-based pre-sort and JOB_RELOAD_LIMIT cap as load_all(),
        but skips restart recovery so RUNNING stays RUNNING. Used by list_jobs()
        to serve live disk state without writing anything.
        """
        base = self._get_base()
        tenants_dir = base / "tenants"
        if not tenants_dir.is_dir():
            return []
        collected: list[tuple[Path, float]] = []
        try:
            for tdir in tenants_dir.iterdir():
                if not tdir.is_dir():
                    continue
                loans_dir = tdir / "loans"
                if not loans_dir.is_dir():
                    continue
                for ldir in loans_dir.iterdir():
                    if not ldir.is_dir():
                        continue
                    jobs_dir = ldir / "_meta" / "jobs"
                    if not jobs_dir.is_dir():
                        continue
                    for p in jobs_dir.iterdir():
                        if p.is_file() and p.suffix == ".json":
                            try:
                                collected.append((p, p.stat().st_mtime))
                            except OSError:
                                continue
        except OSError:
            return []
        collected.sort(key=lambda x: x[1], reverse=True)
        to_scan = collected[:limit]
        results: list[dict[str, Any]] = []
        for path, _ in to_scan:
            try:
                with path.open() as f:
                    job = json.load(f)
                if isinstance(job, dict) and job.get("job_id"):
                    results.append(job)
            except (json.JSONDecodeError, OSError):
                continue
        return results
```

**Step 4: Run test to verify it PASSES**

```bash
cd M:/mortgagedocai && python -c "
import sys; sys.path.insert(0,'scripts')
from test_job_hardening import test_scan_all_raw_no_recovery
test_scan_all_raw_no_recovery()
"
```

Expected: `test_scan_all_raw_no_recovery OK`

**Step 5: Commit**

```bash
git -C //10.10.10.190/opt/mortgagedocai add scripts/loan_service/adapters_disk.py scripts/test_job_hardening.py
git -C //10.10.10.190/opt/mortgagedocai commit -m "feat(disk): add scan_all_raw() to DiskJobStore for cacheless list_jobs"
```

---

## Task 3: Rebuild missing index entries in `load_all()`

**Files:**
- Modify: `scripts/loan_service/adapters_disk.py` — in `DiskJobStore.load_all()`, write index entry for each loaded job if missing
- Test: `scripts/test_job_hardening.py` — add `test_load_all_rebuilds_missing_index()`

**Step 1: Add the test**

Insert before `if __name__ == "__main__":`:

```python
def test_load_all_rebuilds_missing_index():
    """load_all_from_disk() writes index entries for pre-existing jobs (migration path)."""
    import json as _json, uuid as _uuid
    from loan_service.adapters_disk import DiskJobStore, JobKeyIndexImpl, LoanLockImpl
    from loan_service.service import JobService
    nas = _tmp_nas()
    job_dir = nas / "tenants" / "t1" / "loans" / "L1" / "_meta" / "jobs"
    job_dir.mkdir(parents=True)
    job_id = str(_uuid.uuid4())
    # Write a job file directly (bypassing enqueue — no index entry exists yet)
    job = {
        "job_id": job_id, "tenant_id": "t1", "loan_id": "L1", "run_id": "run-x",
        "status": "SUCCESS",
        "created_at_utc": "2026-01-01T00:00:00Z",
        "started_at_utc": "2026-01-01T00:00:01Z",
        "finished_at_utc": "2026-01-01T00:01:00Z",
        "request": {"run_id": "run-x"}, "result": None, "error": None,
        "stdout": None, "stderr": None,
    }
    (job_dir / f"{job_id}.json").write_text(_json.dumps(job))
    store = DiskJobStore(lambda: nas)
    # Confirm no index entry yet
    assert store.load_index_entry(job_id) is None, "index should not exist before load_all"
    # load_all_from_disk must create it
    svc = JobService(
        store=store,
        key_index=JobKeyIndexImpl(),
        loan_lock=LoanLockImpl(lambda: nas),
        runner=None,
        get_base_path=lambda: nas,
    )
    svc.load_all_from_disk()
    entry = store.load_index_entry(job_id)
    assert entry == ("t1", "L1"), f"expected ('t1','L1'), got {entry}"
    print("test_load_all_rebuilds_missing_index OK")
```

Add to `if __name__ == "__main__":`.

**Step 2: Run test to verify it FAILS**

```bash
cd M:/mortgagedocai && python -c "
import sys; sys.path.insert(0,'scripts')
from test_job_hardening import test_load_all_rebuilds_missing_index
test_load_all_rebuilds_missing_index()
"
```

Expected: `AssertionError: index should not exist before load_all` — wait, actually it should get past that and fail at `assert entry == ("t1", "L1")`. Expected fail: `AssertionError: expected ('t1','L1'), got None`

**Step 3: Modify `DiskJobStore.load_all()` to write missing index entries**

In `adapters_disk.py`, find `DiskJobStore.load_all()`. Inside the main collection loop, after line 273 (`jobs[job_id] = job`), add an index write:

```python
            jobs[job_id] = job
            # Write index entry if absent (migration: jobs created before this index existed).
            _jid = job.get("job_id")
            _tid = job.get("tenant_id")
            _lid = job.get("loan_id")
            if _jid and _tid and _lid:
                idx_path = self._job_index_dir() / f"{_jid}.json"
                if not idx_path.exists():
                    self.save_index_entry(_jid, _tid, _lid)
```

The exact insertion point is after `jobs[job_id] = job` at line 273. The block to modify looks like:

```python
# BEFORE (lines 268-273):
            job_id = job.get("job_id")
            if job_id in jobs:
                continue
            if not job.get("created_at_utc"):
                job["created_at_utc"] = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            jobs[job_id] = job

# AFTER:
            job_id = job.get("job_id")
            if job_id in jobs:
                continue
            if not job.get("created_at_utc"):
                job["created_at_utc"] = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            jobs[job_id] = job
            # Write index entry if absent (migration path for pre-index jobs).
            _tid = job.get("tenant_id")
            _lid = job.get("loan_id")
            if job_id and _tid and _lid:
                idx_path = self._job_index_dir() / f"{job_id}.json"
                if not idx_path.exists():
                    self.save_index_entry(job_id, _tid, _lid)
```

**Step 4: Run test to verify it PASSES**

```bash
cd M:/mortgagedocai && python -c "
import sys; sys.path.insert(0,'scripts')
from test_job_hardening import test_load_all_rebuilds_missing_index
test_load_all_rebuilds_missing_index()
"
```

Expected: `test_load_all_rebuilds_missing_index OK`

**Step 5: Run all hardening tests to confirm no regressions**

```bash
cd M:/mortgagedocai && python scripts/test_job_hardening.py
```

Expected: all tests (including the 4 original) pass.

**Step 6: Commit**

```bash
git -C //10.10.10.190/opt/mortgagedocai add scripts/loan_service/adapters_disk.py scripts/test_job_hardening.py
git -C //10.10.10.190/opt/mortgagedocai commit -m "feat(disk): load_all() writes missing index entries (migration path)"
```

---

## Task 4: Write index entry in `enqueue_job()`

**Files:**
- Modify: `scripts/loan_service/service.py` — two spots in `enqueue_job()`, both after `self._store.save(job)`
- Test: `scripts/test_job_hardening.py` — add `test_enqueue_writes_index()`

**Step 1: Add the test**

Insert before `if __name__ == "__main__":`:

```python
def test_enqueue_writes_index():
    """enqueue_job() creates a disk index entry so get_job can find the file path."""
    from loan_service.adapters_disk import DiskJobStore, JobKeyIndexImpl, LoanLockImpl
    from loan_service.service import JobService
    nas = _tmp_nas()
    (nas / "tenants" / "t1" / "loans" / "L1" / "_meta" / "jobs").mkdir(parents=True)
    store = DiskJobStore(lambda: nas)
    svc = JobService(
        store=store, key_index=JobKeyIndexImpl(),
        loan_lock=LoanLockImpl(lambda: nas), runner=None, get_base_path=lambda: nas,
    )
    result = svc.enqueue_job("t1", "L1", {"run_id": "run-idx", "skip_intake": True})
    job_id = result["job_id"]
    entry = store.load_index_entry(job_id)
    assert entry == ("t1", "L1"), f"expected ('t1','L1'), got {entry}"
    print("test_enqueue_writes_index OK")
```

Add to `if __name__ == "__main__":`.

**Step 2: Run test to verify it FAILS**

```bash
cd M:/mortgagedocai && python -c "
import sys; sys.path.insert(0,'scripts')
from test_job_hardening import test_enqueue_writes_index
test_enqueue_writes_index()
"
```

Expected: `AssertionError: expected ('t1','L1'), got None`

**Step 3: Add `save_index_entry()` calls in `service.py` `enqueue_job()`**

In `scripts/loan_service/service.py`, there are two code paths that call `self._store.save(job)` inside `enqueue_job()`:

**Path A — SUCCESS from manifest** (around line 83):
```python
# BEFORE:
                self._store.save(job)
                return {"job_id": job_id, "status": "SUCCESS", "status_url": f"/jobs/{job_id}"}

# AFTER:
                self._store.save(job)
                self._store.save_index_entry(job_id, tenant_id, loan_id)
                return {"job_id": job_id, "status": "SUCCESS", "status_url": f"/jobs/{job_id}"}
```

**Path B — new PENDING job** (around line 105):
```python
# BEFORE:
        self._store.save(job)
        return {"job_id": job_id, "status": "PENDING", "status_url": f"/jobs/{job_id}"}

# AFTER:
        self._store.save(job)
        self._store.save_index_entry(job_id, tenant_id, loan_id)
        return {"job_id": job_id, "status": "PENDING", "status_url": f"/jobs/{job_id}"}
```

**Step 4: Run test to verify it PASSES**

```bash
cd M:/mortgagedocai && python -c "
import sys; sys.path.insert(0,'scripts')
from test_job_hardening import test_enqueue_writes_index
test_enqueue_writes_index()
"
```

Expected: `test_enqueue_writes_index OK`

**Step 5: Run all hardening tests**

```bash
cd M:/mortgagedocai && python scripts/test_job_hardening.py
```

Expected: all tests pass.

**Step 6: Commit**

```bash
git -C //10.10.10.190/opt/mortgagedocai add scripts/loan_service/service.py scripts/test_job_hardening.py
git -C //10.10.10.190/opt/mortgagedocai commit -m "feat(service): enqueue_job() writes disk index entry for get_job() lookup"
```

---

## Task 5: Rewrite `get_job()` to be fully disk-authoritative

**Files:**
- Modify: `scripts/loan_service/service.py` — replace `get_job()` (lines 248-253)
- Test: `scripts/test_job_hardening.py` — add `test_get_job_reads_live_disk_state()`

**Step 1: Add the test**

This test simulates `job_worker.py` updating the disk directly (bypassing the service's in-memory dict) and verifies `get_job()` returns the updated state.

Insert before `if __name__ == "__main__":`:

```python
def test_get_job_reads_live_disk_state():
    """get_job returns current disk state even if in-memory cache has stale PENDING."""
    import json as _json
    from loan_service.adapters_disk import DiskJobStore, JobKeyIndexImpl, LoanLockImpl
    from loan_service.service import JobService
    nas = _tmp_nas()
    (nas / "tenants" / "t1" / "loans" / "L1" / "_meta" / "jobs").mkdir(parents=True)
    store = DiskJobStore(lambda: nas)
    svc = JobService(
        store=store, key_index=JobKeyIndexImpl(),
        loan_lock=LoanLockImpl(lambda: nas), runner=None, get_base_path=lambda: nas,
    )
    # Enqueue: PENDING in memory + disk
    result = svc.enqueue_job("t1", "L1", {"run_id": "run-live", "skip_intake": True})
    job_id = result["job_id"]
    assert result["status"] == "PENDING"

    # Simulate job_worker.py: write RUNNING directly to disk (not via svc)
    job_path = nas / "tenants" / "t1" / "loans" / "L1" / "_meta" / "jobs" / f"{job_id}.json"
    raw = _json.loads(job_path.read_text())
    raw["status"] = "RUNNING"
    raw["started_at_utc"] = "2026-01-01T00:00:01Z"
    job_path.write_text(_json.dumps(raw))

    # get_job must return RUNNING, not the stale PENDING from in-memory cache
    fetched = svc.get_job(job_id)
    assert fetched is not None
    assert fetched["status"] == "RUNNING", f"expected RUNNING, got {fetched['status']}"
    assert "job_key" not in fetched, "job_key must be filtered out"
    print("test_get_job_reads_live_disk_state OK")
```

Add to `if __name__ == "__main__":`.

**Step 2: Run test to verify it FAILS**

```bash
cd M:/mortgagedocai && python -c "
import sys; sys.path.insert(0,'scripts')
from test_job_hardening import test_get_job_reads_live_disk_state
test_get_job_reads_live_disk_state()
"
```

Expected: `AssertionError: expected RUNNING, got PENDING`

**Step 3: Replace `get_job()` in `service.py`**

Find and replace the entire `get_job()` method (lines 248-253):

```python
# BEFORE (lines 248-253):
    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        return {k: v for k, v in job.items() if v is not None and k != "job_key"}

# AFTER:
    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Load job from disk; uses index file for tenant/loan path lookup.
        No reliance on in-memory cache for job state or path correctness.
        """
        entry = self._store.load_index_entry(job_id)
        if entry is None:
            return None
        tenant_id, loan_id = entry
        job = self._store.load_job(tenant_id, loan_id, job_id)
        if job is None:
            return None
        return {k: v for k, v in job.items() if v is not None and k != "job_key"}
```

**Step 4: Run test to verify it PASSES**

```bash
cd M:/mortgagedocai && python -c "
import sys; sys.path.insert(0,'scripts')
from test_job_hardening import test_get_job_reads_live_disk_state
test_get_job_reads_live_disk_state()
"
```

Expected: `test_get_job_reads_live_disk_state OK`

**Step 5: Run all hardening tests**

```bash
cd M:/mortgagedocai && python scripts/test_job_hardening.py
```

Expected: all tests pass.

**Step 6: Commit**

```bash
git -C //10.10.10.190/opt/mortgagedocai add scripts/loan_service/service.py scripts/test_job_hardening.py
git -C //10.10.10.190/opt/mortgagedocai commit -m "feat(service): get_job() loads from disk via index — fully restart-safe"
```

---

## Task 6: Rewrite `list_jobs()` to be disk-authoritative

**Files:**
- Modify: `scripts/loan_service/service.py` — replace `list_jobs()` (lines 255-261)
- Test: `scripts/test_job_hardening.py` — add `test_list_jobs_reads_live_disk_state()`

**Step 1: Add the test**

Insert before `if __name__ == "__main__":`:

```python
def test_list_jobs_reads_live_disk_state():
    """list_jobs returns current disk state even if in-memory cache is stale."""
    import json as _json
    from loan_service.adapters_disk import DiskJobStore, JobKeyIndexImpl, LoanLockImpl
    from loan_service.service import JobService
    nas = _tmp_nas()
    (nas / "tenants" / "t1" / "loans" / "L1" / "_meta" / "jobs").mkdir(parents=True)
    store = DiskJobStore(lambda: nas)
    svc = JobService(
        store=store, key_index=JobKeyIndexImpl(),
        loan_lock=LoanLockImpl(lambda: nas), runner=None, get_base_path=lambda: nas,
    )
    result = svc.enqueue_job("t1", "L1", {"run_id": "run-list-1", "skip_intake": True})
    job_id = result["job_id"]

    # Simulate worker: write SUCCESS directly to disk
    job_path = nas / "tenants" / "t1" / "loans" / "L1" / "_meta" / "jobs" / f"{job_id}.json"
    raw = _json.loads(job_path.read_text())
    raw["status"] = "SUCCESS"
    raw["finished_at_utc"] = "2026-01-01T00:02:00Z"
    job_path.write_text(_json.dumps(raw))

    jobs_list = svc.list_jobs()["jobs"]
    matched = [j for j in jobs_list if j["job_id"] == job_id]
    assert len(matched) == 1, f"expected 1 match, got {len(matched)}"
    assert matched[0]["status"] == "SUCCESS", f"expected SUCCESS, got {matched[0]['status']}"
    assert "job_key" not in matched[0], "job_key must be filtered out"
    print("test_list_jobs_reads_live_disk_state OK")
```

Add to `if __name__ == "__main__":`.

**Step 2: Run test to verify it FAILS**

```bash
cd M:/mortgagedocai && python -c "
import sys; sys.path.insert(0,'scripts')
from test_job_hardening import test_list_jobs_reads_live_disk_state
test_list_jobs_reads_live_disk_state()
"
```

Expected: `AssertionError: expected SUCCESS, got PENDING`

**Step 3: Replace `list_jobs()` in `service.py`**

Find and replace `list_jobs()` (lines 255-261):

```python
# BEFORE (lines 255-261):
    def list_jobs(self, limit: int = 50, status: str | None = None) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            jobs = list(self._jobs.values())
        if status:
            jobs = [j for j in jobs if j.get("status") == status]
        jobs = sorted(jobs, key=lambda j: j.get("created_at_utc") or "", reverse=True)[:limit]
        return {"jobs": [{k: v for k, v in j.items() if v is not None and k != "job_key"} for j in jobs]}

# AFTER:
    def list_jobs(self, limit: int = 50, status: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """Scan disk for current job state. No in-memory cache used."""
        raw = self._store.scan_all_raw()
        if status:
            raw = [j for j in raw if j.get("status") == status]
        raw = sorted(raw, key=lambda j: j.get("created_at_utc") or "", reverse=True)[:limit]
        return {"jobs": [{k: v for k, v in j.items() if v is not None and k != "job_key"} for j in raw]}
```

**Step 4: Run test to verify it PASSES**

```bash
cd M:/mortgagedocai && python -c "
import sys; sys.path.insert(0,'scripts')
from test_job_hardening import test_list_jobs_reads_live_disk_state
test_list_jobs_reads_live_disk_state()
"
```

Expected: `test_list_jobs_reads_live_disk_state OK`

**Step 5: Run all hardening tests**

```bash
cd M:/mortgagedocai && python scripts/test_job_hardening.py
```

Expected: all tests pass.

**Step 6: Commit**

```bash
git -C //10.10.10.190/opt/mortgagedocai add scripts/loan_service/service.py scripts/test_job_hardening.py
git -C //10.10.10.190/opt/mortgagedocai commit -m "feat(service): list_jobs() scans disk via scan_all_raw — no stale in-memory state"
```

---

## Task 7: Remove daemon threads and add heartbeat warning in `loan_api.py`

**Files:**
- Modify: `scripts/loan_api.py` — remove 3 daemon thread spawn blocks, remove `_startup()` step 4, add `_warn_if_no_recent_worker_heartbeat()`

No new unit tests — the absence of daemon threads is verified by grep and compile check.

**Step 1: Remove daemon thread spawn from `start_run_job`**

Find this block in `start_run_job` (around line 827):

```python
# REMOVE THIS BLOCK (lines ~827-831):
    if result.get("status") == "PENDING":
        t = threading.Thread(
            target=_service._run_worker, args=(result["job_id"],), daemon=True
        )
        t.start()
```

**Step 2: Add heartbeat warning after `start_run_job`'s enqueue call**

The `start_run_job` function's `return` statement is:
```python
    return {
        "job_id": result["job_id"],
        "run_id": run_id,
        "status": result["status"],
        "status_url": result["status_url"],
    }
```

Add one line before the `return`:
```python
    _warn_if_no_recent_worker_heartbeat()
    return {
        "job_id": result["job_id"],
        "run_id": run_id,
        "status": result["status"],
        "status_url": result["status_url"],
    }
```

**Step 3: Remove daemon thread spawn from `submit_query_job` and add warning**

Find and remove (around line 851):
```python
# REMOVE:
    if result.get("status") == "PENDING":
        t = threading.Thread(
            target=_service._run_worker, args=(result["job_id"],), daemon=True
        )
        t.start()
    return result
```

Replace with:
```python
    _warn_if_no_recent_worker_heartbeat()
    return result
```

**Step 4: Remove daemon thread spawn from `submit_job` and add warning**

Find and remove (around line 929):
```python
# REMOVE:
    if result.get("status") == "PENDING":
        t = threading.Thread(
            target=_service._run_worker, args=(result["job_id"],), daemon=True
        )
        t.start()
    return result
```

Replace with:
```python
    _warn_if_no_recent_worker_heartbeat()
    return result
```

**Step 5: Remove step 4 from `_startup()`**

In `_startup()`, find and remove this entire block (lines ~571-579):

```python
# REMOVE:
    # 4. Resume PENDING jobs (e.g. queued before this restart).
    with _service._lock:
        pending_ids = [
            jid for jid, j in _service._jobs.items()
            if j.get("status") == "PENDING"
        ]
    for job_id in pending_ids:
        t = threading.Thread(target=_service._run_worker, args=(job_id,), daemon=True)
        t.start()
```

**Step 6: Add `_warn_if_no_recent_worker_heartbeat()` function**

The heartbeat file path used by `job_worker.py` is `{NAS_ANALYZE}/_meta/worker_heartbeat.json` (confirmed in `job_worker.py:43`).

Add this constant and function immediately before the `@app.on_event("startup")` decorator (around line 425, after `app.add_middleware(_SecurityMiddleware)`):

```python
WORKER_HEARTBEAT_MAX_AGE_SEC = 300  # 5 minutes


def _warn_if_no_recent_worker_heartbeat() -> None:
    """Log-only warning when job_worker.py heartbeat is absent or stale.
    No response schema change. Path matches job_worker._write_heartbeat().
    """
    import sys as _sys
    hb = _get_base_path() / "_meta" / "worker_heartbeat.json"
    if not hb.exists():
        print(
            "[loan_api] WARNING: no worker heartbeat at _meta/worker_heartbeat.json; "
            "job queued but job_worker.py may not be running",
            file=_sys.stderr,
        )
        return
    try:
        age = time.time() - hb.stat().st_mtime
        if age > WORKER_HEARTBEAT_MAX_AGE_SEC:
            print(
                f"[loan_api] WARNING: worker heartbeat is {age:.0f}s old (>{WORKER_HEARTBEAT_MAX_AGE_SEC}s); "
                "job_worker.py may be down",
                file=_sys.stderr,
            )
    except OSError:
        pass
```

Note: `sys` is already imported at the top of `loan_api.py`, so the `import sys as _sys` inside the function is redundant — use the module-level `sys` instead:

```python
def _warn_if_no_recent_worker_heartbeat() -> None:
    """Log-only warning when job_worker.py heartbeat is absent or stale.
    No response schema change. Path matches job_worker._write_heartbeat().
    """
    hb = _get_base_path() / "_meta" / "worker_heartbeat.json"
    if not hb.exists():
        print(
            "[loan_api] WARNING: no worker heartbeat at _meta/worker_heartbeat.json; "
            "job queued but job_worker.py may not be running",
            file=sys.stderr,
        )
        return
    try:
        age = time.time() - hb.stat().st_mtime
        if age > WORKER_HEARTBEAT_MAX_AGE_SEC:
            print(
                f"[loan_api] WARNING: worker heartbeat is {age:.0f}s old (>{WORKER_HEARTBEAT_MAX_AGE_SEC}s); "
                "job_worker.py may be down",
                file=sys.stderr,
            )
    except OSError:
        pass
```

**Step 7: Verify daemon threads are gone**

```bash
grep -n "threading.Thread.*_run_worker" M:/mortgagedocai/scripts/loan_api.py
```

Expected: **no output** (empty result)

**Step 8: Compile check**

```bash
cd M:/mortgagedocai && python -c "import sys; sys.path.insert(0,'scripts'); import loan_api; print('ok')"
```

Expected: `ok`

**Step 9: Run all hardening tests**

```bash
cd M:/mortgagedocai && python scripts/test_job_hardening.py
```

Expected: all tests pass.

**Step 10: Commit**

```bash
git -C //10.10.10.190/opt/mortgagedocai add scripts/loan_api.py
git -C //10.10.10.190/opt/mortgagedocai commit -m "feat(loan_api): remove daemon thread execution; API enqueues only, job_worker runs jobs"
```

---

## Task 8: Full verification

**Step 1: Compile all modified modules**

```bash
cd M:/mortgagedocai && python -c "
import sys; sys.path.insert(0, 'scripts')
from loan_service import adapters_disk, service
import loan_api
print('all imports ok')
"
```

Expected: `all imports ok`

**Step 2: py_compile check (acceptance test from spec)**

```bash
cd M:/mortgagedocai && python3 -m py_compile scripts/loan_api.py scripts/loan_service/*.py && echo "py_compile ok"
```

Expected: `py_compile ok`

**Step 3: Run complete hardening test suite**

```bash
cd M:/mortgagedocai && python scripts/test_job_hardening.py
```

Expected:
```
test_idempotency_same_job_id OK
test_restart_recovery_running_becomes_fail OK
test_per_loan_lock_second_waits OK
test_worker_processes_one_queued_job OK
test_disk_index_roundtrip OK
test_scan_all_raw_no_recovery OK
test_load_all_rebuilds_missing_index OK
test_enqueue_writes_index OK
test_get_job_reads_live_disk_state OK
test_list_jobs_reads_live_disk_state OK
All hardening tests passed.
```

**Step 4: Confirm daemon threads are absent**

```bash
grep -n "threading.Thread.*_run_worker" M:/mortgagedocai/scripts/loan_api.py
```

Expected: **no output**

**Step 5: Verify git log shows 7 new commits**

```bash
git -C //10.10.10.190/opt/mortgagedocai log --oneline -10
```

Expected top 7 (newest first):
```
<hash> feat(loan_api): remove daemon thread execution; API enqueues only, job_worker runs jobs
<hash> feat(service): list_jobs() scans disk via scan_all_raw — no stale in-memory state
<hash> feat(service): get_job() loads from disk via index — fully restart-safe
<hash> feat(service): enqueue_job() writes disk index entry for get_job() lookup
<hash> feat(disk): load_all() writes missing index entries (migration path)
<hash> feat(disk): add scan_all_raw() to DiskJobStore for cacheless list_jobs
<hash> feat(disk): add save_index_entry/load_index_entry to DiskJobStore
```

**Step 6: Verify OpenAPI contains all required artifact endpoints**

```bash
cd M:/mortgagedocai && python -c "
import sys; sys.path.insert(0,'scripts'); import loan_api
routes = [r.path for r in loan_api.app.routes]
required = [
    '/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/artifacts',
    '/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/artifacts/{profile}/{filename}',
    '/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/retrieval_pack',
    '/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/job_manifest',
]
missing = [p for p in required if p not in routes]
print('MISSING:', missing) if missing else print('All artifact paths present ✓')
"
```

Expected: `All artifact paths present ✓`
