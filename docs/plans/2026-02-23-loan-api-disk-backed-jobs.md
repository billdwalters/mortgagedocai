# Loan API — Disk-Backed Job System Migration

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the in-memory JOBS/JOBS_LOCK/JOB_KEY_INDEX registry in `scripts/loan_api.py` with the disk-backed `loan_service` implementation so jobs survive API restarts and are the single source of truth.

**Architecture:** `loan_api.py` becomes a thin FastAPI entrypoint that constructs `JobService` from disk adapters, loads persisted jobs on startup, and spawns daemon threads for each enqueued/recovered job. All job state read/write goes through `JobService`. The streaming stdout behavior (PHASE lines visible while job runs) is preserved by adding an `on_stdout_line` callback to `SubprocessRunner`.

**Tech Stack:** FastAPI, Pydantic, `loan_service` (DiskJobStore, JobKeyIndexImpl, LoanLockImpl, SubprocessRunner, JobService), threading, uvicorn.

---

## Pre-flight: Understand the known syntax bug

`scripts/loan_api.py` line 916 currently has a syntax error:
```python
# BROKEN (extra " before ])
step12_cmd += ["--llm-model", body.llm_model"]
# CORRECT
step12_cmd += ["--llm-model", body.llm_model]
```
This is fixed in Task 4 along with the other `query_run` changes.

---

## Task 1: Add streaming + `on_stdout_line` callback to `SubprocessRunner`

**Why:** The current `SubprocessRunner.run()` uses `subprocess.run()` (blocking, no output until process ends). The WebUI polls `/jobs/{job_id}` every few seconds and reads `stdout` for `PHASE:` lines to drive its progress stepper. If stdout only appears at the end (after 10–30 min), the stepper is frozen. We preserve the streaming behavior by changing to `Popen` + reader threads + an optional `on_stdout_line` callback.

**Files:**
- Modify: `scripts/loan_service/adapters_subprocess.py`

**Step 1: Add `import threading` at the top (currently missing)**

In `adapters_subprocess.py`, the imports block (lines 1–8) needs `threading`. Add it:

```python
import os
import subprocess
import sys
import threading   # ADD THIS LINE
import time
from pathlib import Path
from typing import Any
```

**Step 2: Replace `SubprocessRunner.run()` with a streaming Popen version**

Replace the entire `SubprocessRunner` class body (lines 108–166) with:

```python
class SubprocessRunner:
    """Runs scripts/run_loan_job.py; streams stdout so callers see PHASE lines in real time."""

    def run(
        self,
        req: dict[str, Any],
        tenant_id: str,
        loan_id: str,
        env: dict[str, str],
        timeout: int,
        on_stdout_line: Any = None,  # Optional[Callable[[str], None]]
    ) -> tuple[int, str, str]:
        if "question" in req and "profile" in req:
            # Query jobs are fast; streaming not needed.
            return _run_query_job(req, tenant_id, loan_id, env, timeout)
        run_id = req.get("run_id")
        cmd = [
            sys.executable,
            str(SCRIPTS_DIR / "run_loan_job.py"),
            "--tenant-id", tenant_id,
            "--loan-id", loan_id,
        ]
        if run_id:
            cmd += ["--run-id", run_id]
        if req.get("skip_intake"):
            cmd += ["--skip-intake"]
        if req.get("skip_process"):
            cmd += ["--skip-process"]
        if req.get("source_path"):
            cmd += ["--source-path", req["source_path"]]
        if req.get("smoke_debug"):
            cmd += ["--debug"]
        run_llm_val = req.get("run_llm")
        if run_llm_val is not None:
            if run_llm_val in (True, 1, "1", "true", "True"):
                cmd += ["--run-llm"]
            else:
                cmd += ["--no-run-llm"]
        exp_val = req.get("expect_rp_hash_stable")
        if exp_val in (True, 1, "1", "true", "True"):
            cmd += ["--expect-rp-hash-stable"]
        if req.get("max_dropped_chunks") is not None:
            cmd += ["--max-dropped-chunks", str(int(req["max_dropped_chunks"]))]
        if req.get("offline_embeddings"):
            cmd += ["--offline-embeddings"]
        if req.get("top_k") is not None:
            cmd += ["--top-k", str(int(req["top_k"]))]
        if req.get("max_per_file") is not None:
            cmd += ["--max-per-file", str(int(req["max_per_file"]))]

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            def _read_stdout() -> None:
                for line in proc.stdout or []:
                    line = line if line.endswith("\n") else line + "\n"
                    stdout_parts.append(line)
                    if on_stdout_line is not None:
                        try:
                            on_stdout_line(line)
                        except Exception:
                            pass

            def _read_stderr() -> None:
                for line in proc.stderr or []:
                    stderr_parts.append(line)

            t_out = threading.Thread(target=_read_stdout, daemon=True)
            t_err = threading.Thread(target=_read_stderr, daemon=True)
            t_out.start()
            t_err.start()
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                returncode = -1
                stderr_parts.append(f"Job timed out after {timeout}s\n")
            t_out.join(timeout=2.0)
            t_err.join(timeout=2.0)
        except Exception as e:
            returncode = -1
            stderr_parts.append(str(e) + "\n")

        stdout = _truncate("".join(stdout_parts), STDOUT_TRUNCATE)
        stderr = _truncate("".join(stderr_parts), STDERR_TRUNCATE)
        return returncode, stdout, stderr
```

**Step 3: Compile-check**

```bash
python3 -m py_compile scripts/loan_service/adapters_subprocess.py
```
Expected: no output (clean).

**Step 4: Commit**

```bash
git add scripts/loan_service/adapters_subprocess.py
git commit -m "feat(loan_service): stream SubprocessRunner stdout via on_stdout_line callback

Replace subprocess.run() with Popen+threads so PHASE lines are
visible in real time. Backward-compatible: callback is optional.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Wire `on_stdout_line` callback in `JobService._run_worker`

**Why:** `service.py` calls `self._runner.run(...)` and only writes `stdout` to the job dict after the subprocess finishes. We add a closure that appends each line to the in-memory job's stdout as it arrives, so `/jobs/{job_id}` returns live PHASE progress.

**Files:**
- Modify: `scripts/loan_service/service.py`

**Step 1: Add `_on_line` closure and pass it to `self._runner.run()`**

Locate `_run_worker` in `service.py` (line 114). Find the existing call:

```python
        try:
            returncode, stdout, stderr = self._runner.run(
                request, tenant_id, loan_id, env, timeout
            )
```

Replace it with:

```python
        def _on_line(line: str) -> None:
            with self._lock:
                if job_id in self._jobs:
                    current = self._jobs[job_id].get("stdout") or ""
                    self._jobs[job_id]["stdout"] = _truncate(
                        current + line, STDOUT_TRUNCATE
                    )

        try:
            returncode, stdout, stderr = self._runner.run(
                request, tenant_id, loan_id, env, timeout, on_stdout_line=_on_line
            )
```

> NOTE: `_on_line` goes **inside** `_run_worker`, just before the `try:` block that calls `self._runner.run()`. It closes over `job_id`.

**Step 2: Compile-check**

```bash
python3 -m py_compile scripts/loan_service/service.py
```
Expected: no output (clean).

**Step 3: Commit**

```bash
git add scripts/loan_service/service.py
git commit -m "feat(loan_service): stream stdout into job record during pipeline run

Pass on_stdout_line callback so PHASE lines appear in GET /jobs/{id}
while the subprocess is running, not only after completion.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Rewrite `loan_api.py` — remove in-memory state, wire `JobService`

**Why:** This is the primary migration. We remove every reference to `JOBS`, `JOBS_LOCK`, `JOB_KEY_INDEX`, and `_run_job_worker`, and replace them with calls to `_service` (a `JobService` instance backed by `DiskJobStore`).

**Files:**
- Modify: `scripts/loan_api.py`

### Sub-task 3a — Remove in-memory globals and their helpers

**Remove these lines entirely** (they are replaced by the service):

| Lines | Content to remove |
|-------|------------------|
| 74–78 | `JOBS: dict...`, `JOBS_LOCK = threading.Lock()`, blank, `JOB_KEY_INDEX: dict...` |
| 85–89 | `_compute_job_key()` function |
| 92–94 | `_utc_now_z()` function (service has its own; no longer used here) |
| 105–108 | `_truncate()` function |
| 111–117 | `_phase()` function |
| 120–125 | `_parse_run_id_from_stdout()` function |
| 128–135 | `_quiet_env()` function |
| 138–145 | `_job_env_from_request()` function |
| 148–157 | `_load_manifest_if_present()` function |
| 389–508 | entire `_run_job_worker()` function |

> After removing `_utc_now_z`, also remove the redundant `import datetime` inside it at line 93 (the top-level `from datetime import datetime, timezone` at line 27 covers everything still needed).

### Sub-task 3b — Add loan_service imports and construct `_service`

After the `_get_base_path()` function definition (currently line 81–82), insert:

```python
# ---------------------------------------------------------------------------
# Disk-backed job service — single source of truth (replaces JOBS/JOBS_LOCK)
# ---------------------------------------------------------------------------
from loan_service.adapters_disk import DiskJobStore, JobKeyIndexImpl, LoanLockImpl
from loan_service.adapters_subprocess import SubprocessRunner
from loan_service.service import JobService

_store = DiskJobStore(_get_base_path)
_key_index = JobKeyIndexImpl()
_loan_lock = LoanLockImpl(_get_base_path)
_runner = SubprocessRunner()
_service = JobService(
    store=_store,
    key_index=_key_index,
    loan_lock=_loan_lock,
    runner=_runner,
    get_base_path=_get_base_path,
)
```

> These are module-level imports and singletons. They are constructed at import time (before routes are called). `_get_base_path` is a callable that returns `NAS_ANALYZE`, matching the port signature expected by `DiskJobStore` and `LoanLockImpl`.

### Sub-task 3c — Add startup event handler

After `app.add_middleware(_SecurityMiddleware)` (currently around line 590), insert:

```python
@app.on_event("startup")
async def _startup() -> None:
    """Load persisted jobs on startup (restart recovery) then resume any PENDING jobs."""
    _service.load_all_from_disk()
    # After load_all_from_disk, RUNNING jobs are recovered to SUCCESS or FAIL.
    # Spawn worker threads for any PENDING jobs (e.g. queued before an API restart).
    with _service._lock:
        pending_ids = [
            jid for jid, j in _service._jobs.items()
            if j.get("status") == "PENDING"
        ]
    for job_id in pending_ids:
        t = threading.Thread(target=_service._run_worker, args=(job_id,), daemon=True)
        t.start()
```

### Sub-task 3d — Replace `start_run_job` endpoint (POST /runs/start)

This endpoint is the main "Process Loan" trigger used by the WebUI. Replace the old implementation (lines 810–878) with:

```python
@app.post("/tenants/{tenant_id}/loans/{loan_id}/runs/start", status_code=202)
def start_run_job(tenant_id: str, loan_id: str, body: StartRunRequest) -> dict[str, Any]:
    source_path = (body.source_path or "").strip()
    if not source_path:
        raise HTTPException(status_code=422, detail="source_path is required and must be non-empty")
    if body.run_id is not None and body.run_id != "":
        if not _RUN_ID_PATTERN.match(body.run_id):
            raise HTTPException(
                status_code=422,
                detail="run_id must match format YYYY-MM-DDTHHMMSSZ (e.g. 2026-02-19T231500Z)",
            )
        run_id = body.run_id
    else:
        run_id = _utc_run_id()
    request = {
        "run_id": run_id,
        "skip_intake": False,
        "skip_process": False,
        "source_path": source_path,
        "run_llm": body.run_llm,
        "offline_embeddings": body.offline_embeddings,
        "top_k": body.top_k,
        "max_per_file": body.max_per_file,
        "max_dropped_chunks": body.max_dropped_chunks,
        "expect_rp_hash_stable": body.expect_rp_hash_stable,
        "smoke_debug": body.smoke_debug,
    }
    result = _service.enqueue_job(tenant_id, loan_id, request)
    if result.get("status") == "PENDING":
        t = threading.Thread(
            target=_service._run_worker, args=(result["job_id"],), daemon=True
        )
        t.start()
    return {
        "job_id": result["job_id"],
        "run_id": run_id,
        "status": result["status"],
        "status_url": result["status_url"],
    }
```

> The `run_id` key is added back explicitly because `enqueue_job()` returns `{job_id, status, status_url}` but the existing WebUI client reads `run_id` from this response.

### Sub-task 3e — Add `submit_query_job` endpoint (POST /runs/{run_id}/query_jobs)

Insert after `start_run_job` (before `query_run`):

```python
@app.post("/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/query_jobs", status_code=202)
def submit_query_job(
    tenant_id: str, loan_id: str, run_id: str, body: QueryBody
) -> dict[str, Any]:
    """Async background query job (Step13 + Step12). Returns job_id to poll via /jobs/{job_id}."""
    valid_profiles = ("default", "uw_conditions", "income_analysis", "uw_decision")
    if body.profile not in valid_profiles:
        raise HTTPException(
            status_code=422, detail=f"profile must be one of {valid_profiles}"
        )
    req = {"run_id": run_id, **body.model_dump()}
    result = _service.enqueue_job(tenant_id, loan_id, req)
    if result.get("status") == "PENDING":
        t = threading.Thread(
            target=_service._run_worker, args=(result["job_id"],), daemon=True
        )
        t.start()
    return result
```

### Sub-task 3f — Replace `submit_job` endpoint (POST /loans/{loan_id}/jobs)

Replace the old `submit_job` (lines 936–988) with:

```python
@app.post("/tenants/{tenant_id}/loans/{loan_id}/jobs", status_code=202)
def submit_job(tenant_id: str, loan_id: str, body: SubmitJobBody) -> dict[str, Any]:
    if not body.skip_intake and not body.source_path:
        raise HTTPException(
            status_code=422,
            detail="source_path is required when skip_intake is False (run_loan_job.py contract)",
        )
    if body.skip_process and not body.run_id:
        raise HTTPException(
            status_code=422,
            detail="run_id is required when skip_process is True (run_loan_job.py contract)",
        )
    request = body.model_dump()
    result = _service.enqueue_job(tenant_id, loan_id, request)
    if result.get("status") == "PENDING":
        t = threading.Thread(
            target=_service._run_worker, args=(result["job_id"],), daemon=True
        )
        t.start()
    return result
```

### Sub-task 3g — Replace `get_job_status` and `list_jobs`

Replace the old `get_job_status` (lines 990–996) with:

```python
@app.get("/jobs/{job_id}")
def get_job_status(job_id: str) -> dict[str, Any]:
    job = _service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job
```

Replace the old `list_jobs` (lines 999–1006) with:

```python
@app.get("/jobs")
def list_jobs(limit: int = 50, status: str | None = None) -> dict[str, list[dict[str, Any]]]:
    return _service.list_jobs(limit=limit, status=status)
```

---

## Task 4: Fix existing syntax error in `query_run`

**Files:**
- Modify: `scripts/loan_api.py` (same file, different location)

**Step 1: Fix line 916**

The current line 916 in `query_run`:
```python
# BROKEN — trailing " causes SyntaxError: unterminated string literal
        step12_cmd += ["--llm-model", body.llm_model"]
```

Fix to:
```python
        step12_cmd += ["--llm-model", body.llm_model]
```

Also remove the redundant `--ollama-timeout 600 --evidence-max-chars 6000 --llm-max-tokens 400` comment line if it was meant as a continuation (check the surrounding context in the file). The clean version matches `api_router.py` lines 461–462.

---

## Task 5: Compile-check all modified files (Verify A)

Run all three compile checks:

```bash
python3 -m py_compile scripts/loan_api.py && echo "loan_api OK"
python3 -m py_compile scripts/loan_service/service.py && echo "service OK"
python3 -m py_compile scripts/loan_service/adapters_subprocess.py && echo "adapters_subprocess OK"
```

Expected output:
```
loan_api OK
service OK
adapters_subprocess OK
```

If any fail: read the error, fix the offending line, re-run.

---

## Task 6: Commit loan_api.py changes

```bash
git add scripts/loan_api.py
git commit -m "feat(loan_api): replace in-memory JOBS with disk-backed JobService

- Remove JOBS/JOBS_LOCK/JOB_KEY_INDEX and all helper functions
- Construct DiskJobStore + JobService at module level
- On startup: load_all_from_disk() + resume PENDING jobs in daemon threads
- POST /runs/start, POST /jobs, GET /jobs, GET /jobs/{id} all delegate to JobService
- Add POST /runs/{run_id}/query_jobs (async query, backed by service)
- Jobs now persist across API restarts; RUNNING→FAIL/SUCCESS recovery on reload
- Fix syntax error in query_run (unterminated string literal line 916)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 7: Runtime smoke test (Verify B)

On the AI server (`/opt/mortgagedocai`), with API running:

```bash
# Start the API if not running
/opt/mortgagedocai/venv/bin/python3 scripts/loan_api.py --host 0.0.0.0 --port 8000 &

# Health check
curl -s -H "X-API-Key: 123456789" http://127.0.0.1:8000/health
# Expected: {"status":"ok"}

# List loans
curl -s -H "X-API-Key: 123456789" http://127.0.0.1:8000/tenants/peak/loans
# Expected: {"loan_ids":[...]} or {"detail":"...not found"} if path doesn't exist yet

# List jobs (should return empty or loaded-from-disk jobs)
curl -s -H "X-API-Key: 123456789" http://127.0.0.1:8000/jobs
# Expected: {"jobs":[...]}
```

---

## Task 8: Job persistence test (Verify C)

```bash
# 1. Submit a job
JOB=$(curl -s -X POST -H "X-API-Key: 123456789" \
  -H "Content-Type: application/json" \
  -d '{"source_path":"/mnt/source_loans/5-Borrowers TBD/[Loan 16271681] FolderName","offline_embeddings":true}' \
  http://127.0.0.1:8000/tenants/peak/loans/16271681/runs/start)
echo "$JOB"
JOB_ID=$(echo "$JOB" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

# 2. Poll status
curl -s -H "X-API-Key: 123456789" http://127.0.0.1:8000/jobs/$JOB_ID | python3 -m json.tool

# 3. While status=RUNNING, restart the API
#    (kill + restart the uvicorn process or: systemctl restart mortgagedocai-api)

# 4. After restart, poll the same job_id — must NOT return 404
curl -s -H "X-API-Key: 123456789" http://127.0.0.1:8000/jobs/$JOB_ID | python3 -m json.tool
# Expected: {"status":"SUCCESS"} or {"status":"FAIL"} — never 404
```

---

## Task 9: Idempotency test (Verify D)

```bash
# Submit the same request twice — must get same job_id
REQ='{"source_path":"/mnt/source_loans/5-Borrowers TBD/[Loan 16271681] FolderName","offline_embeddings":true}'

JOB1=$(curl -s -X POST -H "X-API-Key: 123456789" -H "Content-Type: application/json" \
  -d "$REQ" http://127.0.0.1:8000/tenants/peak/loans/16271681/runs/start)
JOB2=$(curl -s -X POST -H "X-API-Key: 123456789" -H "Content-Type: application/json" \
  -d "$REQ" http://127.0.0.1:8000/tenants/peak/loans/16271681/runs/start)

echo "JOB1: $JOB1"
echo "JOB2: $JOB2"
# Expected: job_id fields match between JOB1 and JOB2 (idempotent)
```

---

## Summary of all changed files

| File | Change |
|------|--------|
| `scripts/loan_service/adapters_subprocess.py` | Add `import threading`; replace `SubprocessRunner.run()` with Popen streaming + `on_stdout_line` callback |
| `scripts/loan_service/service.py` | Add `_on_line` closure in `_run_worker`; pass it to `self._runner.run()` |
| `scripts/loan_api.py` | Remove JOBS/JOBS_LOCK/JOB_KEY_INDEX and 10 helper functions; add loan_service imports + `_service` singleton; add startup handler; rewrite 4 job endpoints; add `submit_query_job`; fix syntax error |

**No changes to:** `lib.py`, `step10–13`, `run_loan_job.py`, `loan_service/domain.py`, `loan_service/adapters_disk.py`, `loan_service/ports.py`, `loan_service/api_router.py`, `webui/*`

---

## Key behavioural invariants preserved

1. `/jobs/{job_id}` — same JSON shape, `job_key` excluded from response
2. `/jobs` — same shape `{"jobs":[...]}`
3. `POST /runs/start` — same response `{job_id, run_id, status, status_url}`
4. `POST /jobs` — same response `{job_id, status, status_url}`
5. Security middleware — unchanged; `/ui` exempt; 401 for bad key; 404 for unlisted tenant
6. PHASE lines in stdout — streamed in real time via `on_stdout_line` callback
7. All artifact, retrieval_pack, job_manifest, query, source_loan endpoints — **untouched**
8. WebUI static file serving — **untouched**
9. ExecStart command — **unchanged**: `python3 scripts/loan_api.py --host 0.0.0.0 --port 8000`
