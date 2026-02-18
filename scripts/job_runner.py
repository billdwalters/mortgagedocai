#!/usr/bin/env python3
"""
MortgageDocAI — background job runner (registry + daemon thread + run_loan_job subprocess).

Used by loan_api.py for POST/GET /jobs. No FastAPI dependency.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = _SCRIPT_DIR.parent
SCRIPTS_DIR = _SCRIPT_DIR
NAS_ANALYZE = Path("/mnt/nas_apps/nas_analyze")

# ---------------------------------------------------------------------------
# Registry and limits
# ---------------------------------------------------------------------------
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
# job_key (sha256 of stable request) -> job_id; rebuilt on reload; used for idempotency
JOB_KEY_INDEX: dict[str, str] = {}

LOCK_RETRY_SEC = 2

JOB_TIMEOUT_DEFAULT = 3600
STDOUT_TRUNCATE = 50_000
STDERR_TRUNCATE = 50_000
ERROR_TRUNCATE = 4_000

JOB_RELOAD_LIMIT = int(os.environ.get("JOB_RELOAD_LIMIT", "500"))
JOB_RETENTION_DAYS = int(os.environ.get("JOB_RETENTION_DAYS", "30"))

_RUN_ID_LINE_RE = re.compile(r"run_id\s*=\s*(\S+)")


def _job_file_path(tenant_id: str, loan_id: str, job_id: str) -> Path:
    """Deterministic path for a job file under tenant/loan _meta/jobs."""
    return NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id / "_meta" / "jobs" / f"{job_id}.json"


def _loan_lock_path(tenant_id: str, loan_id: str) -> Path:
    """Per-loan lock file path."""
    return NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id / "_meta" / "locks" / "loan.lock"


def _compute_job_key(tenant_id: str, loan_id: str, req: dict[str, Any]) -> str:
    """Stable sha256 of tenant_id + loan_id + request for idempotency."""
    payload = {"tenant_id": tenant_id, "loan_id": loan_id, "request": req}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _result_from_manifest(tenant_id: str, loan_id: str, run_id: str) -> dict[str, Any] | None:
    """Build result summary from manifest if present and SUCCESS; else None."""
    manifest = _load_manifest_if_present(tenant_id, loan_id, run_id)
    if not manifest or manifest.get("status") != "SUCCESS":
        return None
    mp = NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id / run_id / "job_manifest.json"
    return {
        "manifest_path": str(mp),
        "status": manifest.get("status"),
        "rp_sha256": manifest.get("retrieval_pack_sha256"),
        "outputs_base": str(mp.parent) if mp.parent else None,
    }


def _persist_job(job: dict[str, Any]) -> None:
    """Write job dict to disk (atomic). On failure log to stderr and continue."""
    try:
        tenant_id = job.get("tenant_id")
        loan_id = job.get("loan_id")
        job_id = job.get("job_id")
        if not tenant_id or not loan_id or not job_id:
            return
        path = _job_file_path(tenant_id, loan_id, job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w") as f:
            json.dump(job, f, indent=None)
        os.replace(tmp, path)
    except OSError as e:
        print(f"[job_runner] persist failed: {e}", file=sys.stderr)


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _truncate(s: str, max_len: int) -> str:
    if not s or len(s) <= max_len:
        return s or ""
    return s[:max_len] + "\n... (truncated)"


def _parse_run_id_from_stdout(stdout: str) -> str | None:
    for line in stdout.splitlines():
        m = _RUN_ID_LINE_RE.search(line)
        if m:
            return m.group(1).strip()
    return None


def _load_manifest_if_present(tenant_id: str, loan_id: str, run_id: str) -> dict[str, Any] | None:
    mp = NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id / run_id / "job_manifest.json"
    if not mp.exists():
        return None
    try:
        with mp.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _quiet_env() -> dict[str, str]:
    """Env vars to reduce HF/transformers/tqdm noise in subprocesses."""
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    env["TRANSFORMERS_VERBOSITY"] = "error"
    env["TQDM_MININTERVAL"] = "999999"
    env["PYTHONPATH"] = str(SCRIPTS_DIR)
    return env


def _job_env(request: dict[str, Any]) -> dict[str, str]:
    env = _quiet_env()
    env["SMOKE_DEBUG"] = "1" if request.get("smoke_debug") else "0"
    if "expect_rp_hash_stable" in request:
        env["EXPECT_RP_HASH_STABLE"] = "1" if request["expect_rp_hash_stable"] else "0"
    if request.get("max_dropped_chunks") is not None:
        env["MAX_DROPPED_CHUNKS"] = str(request["max_dropped_chunks"])
    if request.get("run_llm") is not None:
        env["RUN_LLM"] = str(request["run_llm"])
    return env


def _run_job_worker(job_id: str) -> None:
    """Daemon thread: acquire per-loan lock, run run_loan_job.py, update JOBS[job_id] with result."""
    with JOBS_LOCK:
        if job_id not in JOBS:
            return
        job = JOBS[job_id]
        tenant_id = job["tenant_id"]
        loan_id = job["loan_id"]
        request = job["request"]
    lock_path = _loan_lock_path(tenant_id, loan_id)
    lock_fd = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                time.sleep(LOCK_RETRY_SEC)
            except OSError as e:
                with JOBS_LOCK:
                    if job_id in JOBS:
                        JOBS[job_id]["status"] = "FAIL"
                        JOBS[job_id]["finished_at_utc"] = _utc_now_z()
                        JOBS[job_id]["error"] = _truncate(f"Could not acquire loan lock: {e}", ERROR_TRUNCATE)
                        job_copy = dict(JOBS[job_id])
                _persist_job(job_copy)
                return
        os.write(lock_fd, json.dumps({"job_id": job_id, "created_at_utc": _utc_now_z()}).encode())
        os.fsync(lock_fd)
    except OSError as e:
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "FAIL"
                JOBS[job_id]["finished_at_utc"] = _utc_now_z()
                JOBS[job_id]["error"] = _truncate(str(e), ERROR_TRUNCATE)
                job_copy = dict(JOBS[job_id])
            else:
                job_copy = None
        if job_copy:
            _persist_job(job_copy)
        return
    try:
        with JOBS_LOCK:
            if job_id not in JOBS:
                return
            JOBS[job_id]["status"] = "RUNNING"
            JOBS[job_id]["started_at_utc"] = _utc_now_z()
            job_copy = dict(JOBS[job_id])
        _persist_job(job_copy)
    except Exception:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            try:
                if lock_path.exists():
                    lock_path.unlink()
            except OSError:
                pass
        raise

    try:
        run_id = request.get("run_id")
        cmd = [
            sys.executable,
            str(SCRIPTS_DIR / "run_loan_job.py"),
            "--tenant-id", tenant_id,
            "--loan-id", loan_id,
        ]
        if run_id:
            cmd += ["--run-id", run_id]
        if request.get("skip_intake"):
            cmd += ["--skip-intake"]
        if request.get("skip_process"):
            cmd += ["--skip-process"]
        if request.get("source_path"):
            cmd += ["--source-path", request["source_path"]]
        if request.get("smoke_debug"):
            cmd += ["--debug"]
        if request.get("run_llm") is not None:
            cmd += ["--run-llm", str(request["run_llm"])]
        if request.get("max_dropped_chunks") is not None:
            cmd += ["--max-dropped-chunks", str(request["max_dropped_chunks"])]
        if request.get("expect_rp_hash_stable") is not None:
            cmd += ["--expect-rp-hash-stable", "1" if request["expect_rp_hash_stable"] else "0"]

        timeout = request.get("timeout", JOB_TIMEOUT_DEFAULT)
        env = _job_env(request)

        result = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdout = _truncate(result.stdout or "", STDOUT_TRUNCATE)
        stderr = _truncate(result.stderr or "", STDERR_TRUNCATE)

        resolved_run_id = run_id or _parse_run_id_from_stdout(stdout)
        result_summary: dict[str, Any] = {}
        if resolved_run_id:
            mp = NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id / resolved_run_id / "job_manifest.json"
            manifest = _load_manifest_if_present(tenant_id, loan_id, resolved_run_id)
            if manifest:
                result_summary["manifest_path"] = str(mp)
                result_summary["status"] = manifest.get("status")
                result_summary["rp_sha256"] = manifest.get("retrieval_pack_sha256")
                result_summary["outputs_base"] = str(mp.parent) if mp.parent else None

        with JOBS_LOCK:
            if job_id not in JOBS:
                return
            JOBS[job_id]["finished_at_utc"] = _utc_now_z()
            JOBS[job_id]["stdout"] = stdout
            JOBS[job_id]["stderr"] = stderr
            JOBS[job_id]["run_id"] = resolved_run_id
            if result.returncode == 0 and result_summary.get("status") == "SUCCESS":
                JOBS[job_id]["status"] = "SUCCESS"
                JOBS[job_id]["result"] = result_summary
            else:
                JOBS[job_id]["status"] = "FAIL"
                err = result.stderr or result.stdout or f"Exit code {result.returncode}"
                JOBS[job_id]["error"] = _truncate(err, ERROR_TRUNCATE)
                if result_summary:
                    JOBS[job_id]["result"] = result_summary
            job_copy = dict(JOBS[job_id])
        _persist_job(job_copy)
    except subprocess.TimeoutExpired:
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "FAIL"
                JOBS[job_id]["finished_at_utc"] = _utc_now_z()
                JOBS[job_id]["error"] = _truncate(f"Job timed out after {timeout}s", ERROR_TRUNCATE)
                job_copy = dict(JOBS[job_id])
            else:
                job_copy = None
        if job_copy:
            _persist_job(job_copy)
    except Exception as e:
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "FAIL"
                JOBS[job_id]["finished_at_utc"] = _utc_now_z()
                JOBS[job_id]["error"] = _truncate(str(e), ERROR_TRUNCATE)
                job_copy = dict(JOBS[job_id])
            else:
                job_copy = None
        if job_copy:
            _persist_job(job_copy)
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            try:
                if lock_path.exists():
                    lock_path.unlink()
            except OSError:
                pass


def enqueue_job(tenant_id: str, loan_id: str, req: dict[str, Any]) -> dict[str, Any]:
    """Create job record, start worker thread, return {job_id, status, status_url}."""
    job_key = _compute_job_key(tenant_id, loan_id, req)
    with JOBS_LOCK:
        if job_key in JOB_KEY_INDEX:
            existing_id = JOB_KEY_INDEX[job_key]
            existing = JOBS.get(existing_id)
            if existing and existing.get("status") in ("PENDING", "RUNNING", "SUCCESS"):
                return {
                    "job_id": existing_id,
                    "status": existing["status"],
                    "status_url": f"/jobs/{existing_id}",
                }
    run_id = req.get("run_id")
    if run_id:
        result_summary = _result_from_manifest(tenant_id, loan_id, run_id)
        if result_summary is not None:
            job_id = str(uuid.uuid4())
            with JOBS_LOCK:
                JOBS[job_id] = {
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "loan_id": loan_id,
                    "run_id": run_id,
                    "status": "SUCCESS",
                    "created_at_utc": _utc_now_z(),
                    "started_at_utc": _utc_now_z(),
                    "finished_at_utc": _utc_now_z(),
                    "request": dict(req),
                    "result": result_summary,
                    "error": None,
                    "stdout": None,
                    "stderr": None,
                    "job_key": job_key,
                }
                JOB_KEY_INDEX[job_key] = job_id
                job_copy = dict(JOBS[job_id])
            _persist_job(job_copy)
            return {
                "job_id": job_id,
                "status": "SUCCESS",
                "status_url": f"/jobs/{job_id}",
            }
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "tenant_id": tenant_id,
            "loan_id": loan_id,
            "run_id": req.get("run_id"),
            "status": "PENDING",
            "created_at_utc": _utc_now_z(),
            "started_at_utc": None,
            "finished_at_utc": None,
            "request": dict(req),
            "result": None,
            "error": None,
            "stdout": None,
            "stderr": None,
            "job_key": job_key,
        }
        JOB_KEY_INDEX[job_key] = job_id
        job_copy = dict(JOBS[job_id])
    _persist_job(job_copy)
    t = threading.Thread(target=_run_job_worker, args=(job_id,), daemon=True)
    t.start()
    return {
        "job_id": job_id,
        "status": "PENDING",
        "status_url": f"/jobs/{job_id}",
    }


def load_jobs_from_disk() -> None:
    """Load persisted jobs into JOBS at startup. Bounded by JOB_RELOAD_LIMIT; optional retention cleanup."""
    tenants_dir = NAS_ANALYZE / "tenants"
    if not tenants_dir.is_dir():
        return
    collected: list[tuple[Path, float]] = []
    try:
        for tenant_id in tenants_dir.iterdir():
            if not tenant_id.is_dir():
                continue
            loans_dir = tenant_id / "loans"
            if not loans_dir.is_dir():
                continue
            for loan_id in loans_dir.iterdir():
                if not loan_id.is_dir():
                    continue
                jobs_dir = loan_id / "_meta" / "jobs"
                if not jobs_dir.is_dir():
                    continue
                for p in jobs_dir.iterdir():
                    if p.is_file() and p.suffix == ".json":
                        try:
                            mtime = p.stat().st_mtime
                        except OSError:
                            continue
                        collected.append((p, mtime))
    except OSError:
        return
    collected.sort(key=lambda x: x[1], reverse=True)
    to_load = collected[:JOB_RELOAD_LIMIT]
    now_ts = time.time()
    retention_sec = JOB_RETENTION_DAYS * 24 * 3600
    with JOBS_LOCK:
        for path, mtime in to_load:
            try:
                with path.open() as f:
                    job = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                print(f"[job_runner] load skip {path}: {e}", file=sys.stderr)
                continue
            if not isinstance(job, dict) or "job_id" not in job or "status" not in job:
                print(f"[job_runner] load skip {path}: missing job_id or status", file=sys.stderr)
                continue
            job_id = job.get("job_id")
            if job_id in JOBS:
                continue
            if not job.get("created_at_utc"):
                job["created_at_utc"] = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            JOBS[job_id] = job
        for j in JOBS.values():
            if j.get("job_key"):
                JOB_KEY_INDEX[j["job_key"]] = j["job_id"]
        runnings = [dict(j) for j in JOBS.values() if j.get("status") == "RUNNING"]
    for job in runnings:
        job_id = job.get("job_id")
        tenant_id = job.get("tenant_id")
        loan_id = job.get("loan_id")
        run_id = job.get("run_id")
        if not tenant_id or not loan_id:
            continue
        result_summary = _result_from_manifest(tenant_id, loan_id, run_id) if run_id else None
        lock_path = _loan_lock_path(tenant_id, loan_id)
        try:
            if lock_path.exists():
                lock_path.unlink()
        except OSError:
            pass
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if j is None or j.get("status") != "RUNNING":
                continue
            if result_summary is not None:
                j["status"] = "SUCCESS"
                j["finished_at_utc"] = _utc_now_z()
                j["result"] = result_summary
                j["error"] = None
            else:
                j["status"] = "FAIL"
                j["finished_at_utc"] = _utc_now_z()
                j["error"] = _truncate("Recovered after restart: job was RUNNING but no active worker", ERROR_TRUNCATE)
            _persist_job(dict(j))
    for path, mtime in collected:
        if now_ts - mtime > retention_sec:
            try:
                path.unlink()
            except OSError as e:
                print(f"[job_runner] retention delete failed {path}: {e}", file=sys.stderr)


def get_job(job_id: str) -> dict[str, Any] | None:
    """Return job record or None if not found. Excludes internal job_key from response."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return None
    return {k: v for k, v in job.items() if v is not None and k != "job_key"}


def list_jobs(limit: int = 50, status: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Return {jobs: [...]} newest first, optional status filter."""
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    if status:
        jobs = [j for j in jobs if j.get("status") == status]
    jobs = sorted(jobs, key=lambda j: j.get("created_at_utc") or "", reverse=True)[:limit]
    return {"jobs": [{k: v for k, v in j.items() if v is not None and k != "job_key"} for j in jobs]}
