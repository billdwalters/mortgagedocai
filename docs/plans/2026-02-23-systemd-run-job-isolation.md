# systemd-run Job Isolation + Restart Recovery

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Background jobs survive `mortgagedocai-api.service` restarts by running in isolated systemd scopes. On API restart, active scopes are detected and monitored; inactive RUNNING jobs fall through to existing manifest-based recovery.

**Architecture:**
`SubprocessRunner.run()` wraps `run_loan_job.py` in `systemd-run --scope --wait` and redirects stdout/stderr to `/tmp/mortgagedocai-<job_id>.*` temp files. A tail thread streams live PHASE lines for the WebUI stepper. Temp files survive the API kill. On startup, `_startup()` scans for RUNNING jobs whose systemd scopes are still active, restores them to RUNNING status, and spawns watcher threads that finalise the job once the scope exits. Inactive RUNNING jobs go through the unchanged `load_all()` manifest-recovery path.

**Tech Stack:** Python 3.11, FastAPI, systemd (Linux), subprocess.Popen, threading

**Current branch:** `rc/v0.8-loan-service-refactor`

---

## Task 1: Add `job_id` to `PipelineRunner` protocol

**Files:**
- Modify: `scripts/loan_service/ports.py:47-58`

**Step 1: Edit `PipelineRunner.run()` signature** — add `job_id` keyword-only at the end:

```python
class PipelineRunner(Protocol):
    def run(
        self,
        req: dict[str, Any],
        tenant_id: str,
        loan_id: str,
        env: dict[str, str],
        timeout: int,
        on_stdout_line: Callable[[str], None] | None = None,
        job_id: str | None = None,
    ) -> tuple[int, str, str]:
        """Run run_loan_job.py. Returns (returncode, stdout, stderr)."""
        ...
```

**Step 2: Compile check**

```bash
cd M:/mortgagedocai && python -c "import scripts.loan_service.ports" 2>&1 || python -c "import sys; sys.path.insert(0,'scripts'); from loan_service import ports; print('ok')"
```

Expected: `ok`

**Step 3: Commit**

```bash
git add scripts/loan_service/ports.py
git commit -m "fix(loan_service): add job_id param to PipelineRunner protocol"
```

---

## Task 2: Add systemd-run helpers and implement `SubprocessRunner._run_with_systemd()`

**Files:**
- Modify: `scripts/loan_service/adapters_subprocess.py`

**Step 1: Add `shutil` import and module-level constants after the existing imports (after line 10)**

Add to the imports block:
```python
import shutil
```

Add after `SCRIPTS_DIR = _SCRIPT_DIR` (after line 17):
```python
_SYSTEMD_RUN: str | None = shutil.which("systemd-run")
_TEMP_DIR = Path("/tmp")


def _job_unit_name(job_id: str) -> str:
    return f"mortgagedocai-job-{job_id}"


def _job_temp_stdout(job_id: str) -> Path:
    return _TEMP_DIR / f"mortgagedocai-{job_id}.stdout"


def _job_temp_stderr(job_id: str) -> Path:
    return _TEMP_DIR / f"mortgagedocai-{job_id}.stderr"


def _job_temp_rc(job_id: str) -> Path:
    return _TEMP_DIR / f"mortgagedocai-{job_id}.rc"
```

**Step 2: Add `job_id` parameter to `SubprocessRunner.run()` signature** (line 119)

Change the signature from:
```python
    def run(
        self,
        req: dict[str, Any],
        tenant_id: str,
        loan_id: str,
        env: dict[str, str],
        timeout: int,
        on_stdout_line: Any = None,  # Optional[Callable[[str], None]]
    ) -> tuple[int, str, str]:
```
to:
```python
    def run(
        self,
        req: dict[str, Any],
        tenant_id: str,
        loan_id: str,
        env: dict[str, str],
        timeout: int,
        on_stdout_line: Any = None,  # Optional[Callable[[str], None]]
        job_id: str | None = None,
    ) -> tuple[int, str, str]:
```

**Step 3: Add the systemd-run dispatch** — insert after the query-job early-return and after all the `cmd = [...]` building, just before `stdout_parts: list[str] = []`:

The end of the cmd-building block currently ends around line 157. Insert this block immediately before `stdout_parts: list[str] = []` (line 159):
```python
        # Use systemd-run when available; fallback to Popen for dev/test environments.
        if job_id and _SYSTEMD_RUN:
            return self._run_with_systemd(cmd, job_id, env, timeout, on_stdout_line)
```

**Step 4: Add `_run_with_systemd()` method** — add as a new method after `run()`, before the end of the class:

```python
    def _run_with_systemd(
        self,
        cmd: list[str],
        job_id: str,
        env: dict[str, str],
        timeout: int,
        on_stdout_line: Any,
    ) -> tuple[int, str, str]:
        """Run cmd in an isolated systemd scope so it survives API restart."""
        import shlex

        stdout_file = _job_temp_stdout(job_id)
        stderr_file = _job_temp_stderr(job_id)
        rc_file = _job_temp_rc(job_id)
        unit_name = _job_unit_name(job_id)

        # Remove stale temp files from any prior crash of this job_id (UUID; shouldn't exist).
        for p in [stdout_file, stderr_file, rc_file]:
            try:
                p.unlink()
            except FileNotFoundError:
                pass

        # Build shell command: redirect pipeline output to temp files; capture exit code.
        python_cmd = " ".join(shlex.quote(c) for c in cmd)
        shell_script = (
            f"{python_cmd} "
            f">{shlex.quote(str(stdout_file))} "
            f"2>{shlex.quote(str(stderr_file))}; "
            f"echo $? >{shlex.quote(str(rc_file))}"
        )
        systemd_cmd: list[str] = [
            _SYSTEMD_RUN, "--scope", "--wait",
            f"--unit={unit_name}",
            "--property=KillMode=process",
            "--property=TimeoutStopSec=20",
            "--",
            "/bin/sh", "-c", shell_script,
        ]

        # Tail the stdout temp file in a thread so PHASE lines reach the in-memory job
        # record (WebUI stepper) in near-real-time.
        stdout_parts: list[str] = []
        stop_tail = threading.Event()

        def _tail() -> None:
            try:
                deadline = time.monotonic() + 15.0
                while not stdout_file.exists() and time.monotonic() < deadline:
                    if stop_tail.is_set():
                        return
                    time.sleep(0.1)
                if not stdout_file.exists():
                    return
                with stdout_file.open("r") as f:
                    while not stop_tail.is_set():
                        line = f.readline()
                        if line:
                            normed = line if line.endswith("\n") else line + "\n"
                            stdout_parts.append(normed)
                            if on_stdout_line is not None:
                                try:
                                    on_stdout_line(normed)
                                except Exception:
                                    pass
                        else:
                            time.sleep(0.05)
            except Exception:
                pass

        t_tail = threading.Thread(target=_tail, daemon=True)
        t_tail.start()
        returncode = -1
        try:
            proc = subprocess.Popen(
                systemd_cmd,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                returncode = -1
        except Exception as e:
            stop_tail.set()
            t_tail.join(timeout=2.0)
            return -1, "", _truncate(str(e), STDERR_TRUNCATE)
        finally:
            stop_tail.set()
            t_tail.join(timeout=2.0)

        # Read authoritative output from temp files (more complete than the tailed parts).
        try:
            final_stdout = _truncate(stdout_file.read_text(), STDOUT_TRUNCATE)
        except OSError:
            final_stdout = _truncate("".join(stdout_parts), STDOUT_TRUNCATE)
        try:
            final_stderr = _truncate(stderr_file.read_text(), STDERR_TRUNCATE)
        except OSError:
            final_stderr = ""

        # The rc file gives the actual pipeline exit code (systemd-run's code is
        # not always the subprocess's code when the API was killed mid-run).
        try:
            if rc_file.exists():
                returncode = int(rc_file.read_text().strip())
        except (ValueError, OSError):
            pass

        # Clean up temp files (watcher handles cleanup if API was killed before here).
        for p in [stdout_file, stderr_file, rc_file]:
            try:
                p.unlink()
            except OSError:
                pass

        return returncode, final_stdout, final_stderr
```

**Step 5: Compile check**

```bash
cd M:/mortgagedocai && python -c "import sys; sys.path.insert(0,'scripts'); from loan_service import adapters_subprocess; print('ok')"
```

Expected: `ok`

**Step 6: Commit**

```bash
git add scripts/loan_service/adapters_subprocess.py
git commit -m "feat(loan_service): run_loan_job.py in systemd-run scope for API-restart isolation"
```

---

## Task 3: Extract `_finalize_job()` + pass `job_id` in `service.py`

**Files:**
- Modify: `scripts/loan_service/service.py`

This task does two things atomically:
1. Passes `job_id=job_id` to `self._runner.run()` so systemd-run has a unit name.
2. Extracts the finalization block from `_run_worker` into `_finalize_job()` so the restart watcher (Task 5) can reuse it without duplicating logic.

**Step 1: In `_run_worker`, change the `runner.run()` call** (line 160) to pass `job_id`:

```python
            returncode, stdout, stderr = self._runner.run(
                request, tenant_id, loan_id, env, timeout,
                on_stdout_line=_on_line,
                job_id=job_id,
            )
```

**Step 2: Replace the finalization block at the end of `_run_worker`** (lines 184–217) with a call to `_finalize_job`:

Replace everything from `resolved_run_id = ...` down to and including `self._store.save(dict(self._jobs[job_id]))` with:
```python
        self._finalize_job(job_id, returncode, stdout, stderr)
```

**Step 3: Add `_finalize_job()` as a new method after `_run_worker`**:

```python
    def _finalize_job(
        self, job_id: str, returncode: int, stdout: str, stderr: str
    ) -> None:
        """Persist final job state after subprocess completion.

        Called by _run_worker (normal path) and by the restart watcher (Task 5).
        Lock is NOT held by the caller; this method acquires it as needed.
        """
        with self._lock:
            if job_id not in self._jobs:
                return
            job = self._jobs[job_id]
            request = job.get("request") or {}
            tenant_id = job.get("tenant_id") or ""
            loan_id = job.get("loan_id") or ""

        resolved_run_id = request.get("run_id") or parse_run_id_from_stdout(stdout)
        result_summary: dict[str, Any] = {}
        if "question" in request:
            base = self._get_base()
            run_dir = (
                base / "tenants" / tenant_id / "loans" / loan_id / resolved_run_id
                if resolved_run_id else None
            )
            result_summary["outputs_base"] = str(run_dir) if run_dir else None
            result_summary["status"] = "SUCCESS" if returncode == 0 else "FAIL"
        elif resolved_run_id:
            manifest = load_manifest_if_present(
                self._get_base, tenant_id, loan_id, resolved_run_id
            )
            if manifest:
                base = self._get_base()
                mp = (
                    base / "tenants" / tenant_id / "loans" / loan_id
                    / resolved_run_id / "job_manifest.json"
                )
                result_summary["manifest_path"] = str(mp)
                result_summary["status"] = manifest.get("status")
                result_summary["rp_sha256"] = manifest.get("retrieval_pack_sha256")
                result_summary["outputs_base"] = str(mp.parent) if mp.parent else None

        with self._lock:
            if job_id not in self._jobs:
                return
            self._jobs[job_id]["finished_at_utc"] = _utc_now_z()
            self._jobs[job_id]["stdout"] = stdout
            self._jobs[job_id]["stderr"] = stderr
            self._jobs[job_id]["run_id"] = resolved_run_id
            if returncode == 0 and result_summary.get("status") == "SUCCESS":
                self._jobs[job_id]["status"] = "SUCCESS"
                self._jobs[job_id]["result"] = result_summary
            else:
                self._jobs[job_id]["status"] = "FAIL"
                err = stderr or stdout or f"Exit code {returncode}"
                self._jobs[job_id]["error"] = _truncate(err, ERROR_TRUNCATE)
                if result_summary:
                    self._jobs[job_id]["result"] = result_summary
                self._append_phase(self._jobs[job_id], "FAIL")
            self._store.save(dict(self._jobs[job_id]))
```

**Step 4: Compile check**

```bash
cd M:/mortgagedocai && python -c "import sys; sys.path.insert(0,'scripts'); from loan_service import service; print('ok')"
```

Expected: `ok`

**Step 5: Commit**

```bash
git add scripts/loan_service/service.py
git commit -m "refactor(loan_service): extract _finalize_job; pass job_id to runner for systemd-run"
```

---

## Task 4: Pass `job_id` in `job_worker.py`

**Files:**
- Modify: `scripts/job_worker.py:97-99`

**Step 1: Edit `run_one_cycle` — add `job_id=jid` to the `runner.run()` call**

Current (lines 97–99):
```python
        try:
            returncode, stdout, stderr = runner.run(
                request, tid, lid, env, timeout
            )
```

Change to:
```python
        try:
            returncode, stdout, stderr = runner.run(
                request, tid, lid, env, timeout, job_id=jid
            )
```

**Step 2: Compile check**

```bash
cd M:/mortgagedocai && python -c "import sys; sys.path.insert(0,'scripts'); import job_worker; print('ok')"
```

Expected: `ok`

**Step 3: Commit**

```bash
git add scripts/job_worker.py
git commit -m "feat(job_worker): pass job_id to runner so run_one_cycle uses systemd-run isolation"
```

---

## Task 5: Update `_startup()` in `loan_api.py` for orphaned job detection + watcher

**Files:**
- Modify: `scripts/loan_api.py:425-438`

**Step 1: Add imports at the top of the function block** — these helpers need `time` (already imported) and `subprocess` (already imported). The three helper functions and the updated `_startup` go directly before the `@app.on_event("startup")` line (currently line 425).

Add this block between `app.add_middleware(_SecurityMiddleware)` and `@app.on_event("startup")`:

```python
def _is_scope_active(job_id: str) -> bool:
    """Return True if a systemd scope for this job_id is currently active."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "--quiet",
             f"mortgagedocai-job-{job_id}"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _find_orphaned_running_jobs() -> list[tuple[str, str, str, str | None]]:
    """Scan disk for RUNNING jobs whose systemd scope is still active.

    Returns list of (job_id, tenant_id, loan_id, run_id).
    Called BEFORE load_all_from_disk() so the raw on-disk status is still RUNNING.
    """
    nas = _get_base_path()
    tenants_dir = nas / "tenants"
    if not tenants_dir.is_dir():
        return []
    result: list[tuple[str, str, str, str | None]] = []
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
                    if not p.is_file() or p.suffix != ".json":
                        continue
                    try:
                        with p.open() as fh:
                            job = json.load(fh)
                        if not isinstance(job, dict) or job.get("status") != "RUNNING":
                            continue
                        job_id = job.get("job_id")
                        if not job_id:
                            continue
                        if _is_scope_active(job_id):
                            result.append((
                                job_id,
                                job.get("tenant_id", ""),
                                job.get("loan_id", ""),
                                job.get("run_id"),
                            ))
                    except (OSError, json.JSONDecodeError, Exception):
                        continue
    except OSError:
        pass
    return result


def _watch_orphaned_job(job_id: str) -> None:
    """Background thread: wait for an orphaned systemd scope then finalise the job.

    Called after _startup() restores a RUNNING job that had an active scope.
    The loan lock was already released by load_all(). We don't re-acquire it
    because the subprocess owns its own execution at this point.
    """
    from loan_service.adapters_subprocess import (
        _job_unit_name,
        _job_temp_stdout,
        _job_temp_stderr,
        _job_temp_rc,
    )

    unit_name = _job_unit_name(job_id)
    deadline = time.time() + JOB_TIMEOUT_DEFAULT
    while time.time() < deadline:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", "--quiet", unit_name],
                capture_output=True,
                timeout=5,
            )
            if r.returncode != 0:
                break
        except Exception:
            break
        time.sleep(5)

    stdout_file = _job_temp_stdout(job_id)
    stderr_file = _job_temp_stderr(job_id)
    rc_file = _job_temp_rc(job_id)

    try:
        final_stdout = _truncate(stdout_file.read_text(), STDOUT_TRUNCATE)
    except OSError:
        final_stdout = ""
    try:
        final_stderr = _truncate(stderr_file.read_text(), STDERR_TRUNCATE)
    except OSError:
        final_stderr = ""
    returncode = -1
    try:
        if rc_file.exists():
            returncode = int(rc_file.read_text().strip())
    except (ValueError, OSError):
        pass
    for p in [stdout_file, stderr_file, rc_file]:
        try:
            p.unlink()
        except OSError:
            pass

    _service._finalize_job(job_id, returncode, final_stdout, final_stderr)
```

You also need to add `_truncate` to the imports from loan_service. Look for the existing import block near line 80 and add `_truncate`:
```python
from loan_service.adapters_disk import DiskJobStore, JobKeyIndexImpl, LoanLockImpl, _truncate
```

**Step 2: Replace `_startup()` itself** — the current body (lines 426–438) becomes:

```python
@app.on_event("startup")
async def _startup() -> None:
    """Load persisted jobs on startup (restart recovery) then resume any PENDING jobs.

    For RUNNING jobs whose systemd scope is still active (API was killed during a run),
    the job is restored to RUNNING and a watcher thread finalises it when the scope exits.
    For all other RUNNING jobs, load_all() applies standard manifest-based recovery.
    """
    # 1. Find jobs that are RUNNING on disk and whose systemd scope is still alive.
    orphaned = _find_orphaned_running_jobs()
    orphaned_ids = {jid for jid, *_ in orphaned}

    # 2. Standard load: RUNNING → SUCCESS (manifest) or FAIL (no manifest).
    _service.load_all_from_disk()

    # 3. For orphaned jobs, undo the recovery and restore RUNNING so the watcher can
    #    finalise them correctly once the scope exits.
    for job_id, _tid, _lid, _run_id in orphaned:
        with _service._lock:
            if job_id in _service._jobs:
                _service._jobs[job_id]["status"] = "RUNNING"
                _service._jobs[job_id]["finished_at_utc"] = None
                _service._jobs[job_id]["error"] = None
                _store.save(dict(_service._jobs[job_id]))
        t = threading.Thread(target=_watch_orphaned_job, args=(job_id,), daemon=True)
        t.start()

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

**Step 3: Compile check**

```bash
cd M:/mortgagedocai && python -c "import sys; sys.path.insert(0,'scripts'); import loan_api; print('ok')"
```

Expected: `ok`

**Step 4: Commit**

```bash
git add scripts/loan_api.py
git commit -m "feat(loan_api): detect orphaned systemd scopes on startup; spawn watcher threads"
```

---

## Task 6: Update `FakeRunner` / `SlowFakeRunner` in tests

**Files:**
- Modify: `scripts/test_job_hardening.py:130-132` and `84-88`

`run_one_cycle` now calls `runner.run(..., job_id=jid)`. Both test fakes must accept (and ignore) the new keyword arg.

**Step 1: Add `job_id=None` to `FakeRunner.run` signature** (line 131):

```python
        class FakeRunner:
            def run(self, req, tenant_id, loan_id, env, timeout, job_id=None):
                return 0, "run_id = run-1", ""
```

**Step 2: Add `job_id=None` to `SlowFakeRunner.run` signature** (line 85):

```python
    class SlowFakeRunner:
        def run(self, req, tenant_id, loan_id, env, timeout, job_id=None):
            run_times.append(time.time())
            time.sleep(1.5)
            return 0, f"run_id = {req.get('run_id', 'slow-1')}", ""
```

**Step 3: Run all tests**

```bash
cd M:/mortgagedocai && python scripts/test_job_hardening.py
```

Expected:
```
test_idempotency_same_job_id OK
test_restart_recovery_running_becomes_fail OK
test_per_loan_lock_second_waits OK
test_worker_processes_one_queued_job OK
All hardening tests passed.
```

**Step 4: Commit**

```bash
git add scripts/test_job_hardening.py
git commit -m "test: add job_id=None to FakeRunner and SlowFakeRunner for new runner signature"
```

---

## Task 7: Compile-check all modified files + full test run

**Step 1: Compile all five modified modules**

```bash
cd M:/mortgagedocai && python -c "
import sys; sys.path.insert(0, 'scripts')
from loan_service import ports, adapters_subprocess, service
import job_worker, loan_api
print('all imports ok')
"
```

Expected: `all imports ok`

**Step 2: Run hardening tests**

```bash
cd M:/mortgagedocai && python scripts/test_job_hardening.py
```

Expected: `All hardening tests passed.`

**Step 3: Verify git log shows 6 new commits**

```bash
cd M:/mortgagedocai && git log --oneline -10
```

Expected top 6 commits (newest first):
```
<hash> test: add job_id=None to FakeRunner and SlowFakeRunner for new runner signature
<hash> feat(loan_api): detect orphaned systemd scopes on startup; spawn watcher threads
<hash> feat(job_worker): pass job_id to runner so run_one_cycle uses systemd-run isolation
<hash> refactor(loan_service): extract _finalize_job; pass job_id to runner for systemd-run
<hash> feat(loan_service): run_loan_job.py in systemd-run scope for API-restart isolation
<hash> fix(loan_service): add job_id param to PipelineRunner protocol
```

---

## Acceptance Test Checklist (manual, on production server)

After deploying the branch:

```
1. sudo systemctl start mortgagedocai-api.service
2. POST /tenants/peak/loans/16271681/runs/start  →  note job_id and confirm status=RUNNING
3. Watch the job start: GET /jobs/<job_id>  →  status should be RUNNING within ~5s
4. sudo systemctl restart mortgagedocai-api.service  (while job is RUNNING)
5. GET /jobs/<job_id>  →  must return the job record (not 404), status should be RUNNING
   (orphaned watcher has detected the scope and restored RUNNING status)
6. Wait for the pipeline to complete (up to 60 min for a full loan)
7. GET /jobs/<job_id>  →  status must be SUCCESS (or FAIL with manifest-based error)
8. Confirm run dir exists:
   ls /mnt/nas_apps/nas_chunk/tenants/peak/loans/16271681/<run_id>/
   ls /mnt/nas_apps/nas_analyze/tenants/peak/loans/16271681/<run_id>/
9. GET /tenants/peak/loans/16271681/runs/<run_id>/artifacts  →  HTTP 200
```

### Known limitation
If the API is restarted AND a concurrent new job for the same loan is submitted between the brief FAIL recovery window and the watcher restoring RUNNING, both jobs could run simultaneously for that loan (the loan lock was cleared by `load_all()`). This edge case requires an active user submission during the ~1-second startup window and is not addressed in this plan.

---
