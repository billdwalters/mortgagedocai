#!/usr/bin/env python3
"""
MortgageDocAI — durable worker process: polls disk job store, claims PENDING jobs,
runs run_loan_job.py, persists results. Safe to run multiple workers (claim is atomic).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

# Ensure scripts/ is on path
_scripts_dir = Path(__file__).resolve().parent
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from loan_service.adapters_disk import (
    ERROR_TRUNCATE,
    load_manifest_if_present,
    result_from_manifest,
)
from loan_service.adapters_disk import _parse_run_id_from_stdout as parse_run_id_from_stdout
from loan_service.adapters_disk import _truncate
from loan_service.adapters_subprocess import JOB_TIMEOUT_DEFAULT, get_job_env
from loan_service.domain import _utc_now_z

DEFAULT_POLL_INTERVAL = 2
CLAIM_STALE_SEC = 300


def _write_heartbeat(get_base_path: Callable[[], Path]) -> None:
    """Write worker heartbeat (optional; does not affect API schema)."""
    try:
        base = get_base_path()
        meta = base / "_meta"
        meta.mkdir(parents=True, exist_ok=True)
        path = meta / "worker_heartbeat.json"
        payload = {"heartbeat_utc": _utc_now_z()}
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except OSError:
        pass


def run_one_cycle(
    get_base_path: Callable[[], Path],
    store: Any,
    loan_lock: Any,
    runner: Any,
    claim_max_age_sec: int = CLAIM_STALE_SEC,
    tenant_id: str | None = None,
    loan_id: str | None = None,
) -> bool:
    """
    Find one PENDING job, claim it, run pipeline, persist result. Returns True if a job was processed.
    Used by the CLI loop and by tests (with optional mock runner).
    """
    store.clear_stale_claims(max_age_sec=claim_max_age_sec)
    pending = store.list_pending_jobs(tenant_id=tenant_id, loan_id=loan_id)
    for tid, lid, jid in pending:
        if not store.try_claim(tid, lid, jid):
            continue
        job = store.load_job(tid, lid, jid)
        if job is None or job.get("status") != "PENDING":
            store.release_claim(tid, lid, jid)
            continue
        request = job.get("request") or {}
        lock_held = False
        try:
            loan_lock.acquire(tid, lid, jid, _utc_now_z())
            lock_held = True
        except Exception as e:
            job["status"] = "FAIL"
            job["finished_at_utc"] = _utc_now_z()
            job["error"] = _truncate(str(e), ERROR_TRUNCATE)
            store.save(job)
            store.release_claim(tid, lid, jid)
            return True
        try:
            job["status"] = "RUNNING"
            job["started_at_utc"] = _utc_now_z()
            store.save(job)
        except Exception:
            loan_lock.release(tid, lid)
            store.release_claim(tid, lid, jid)
            raise
        timeout = request.get("timeout", JOB_TIMEOUT_DEFAULT)
        env = get_job_env(request)
        try:
            returncode, stdout, stderr = runner.run(
                request, tid, lid, env, timeout
            )
        except subprocess.TimeoutExpired:
            job["status"] = "FAIL"
            job["finished_at_utc"] = _utc_now_z()
            job["error"] = _truncate(f"Job timed out after {timeout}s", ERROR_TRUNCATE)
            store.save(job)
            return True
        except Exception as e:
            job["status"] = "FAIL"
            job["finished_at_utc"] = _utc_now_z()
            job["error"] = _truncate(str(e), ERROR_TRUNCATE)
            store.save(job)
            return True
        finally:
            if lock_held:
                loan_lock.release(tid, lid)
        resolved_run_id = request.get("run_id") or parse_run_id_from_stdout(stdout)
        result_summary: dict[str, Any] = {}
        if resolved_run_id:
            manifest = load_manifest_if_present(get_base_path, tid, lid, resolved_run_id)
            if manifest:
                base = get_base_path()
                mp = base / "tenants" / tid / "loans" / lid / resolved_run_id / "job_manifest.json"
                result_summary["manifest_path"] = str(mp)
                result_summary["status"] = manifest.get("status")
                result_summary["rp_sha256"] = manifest.get("retrieval_pack_sha256")
                result_summary["outputs_base"] = str(mp.parent) if mp.parent else None
        job["finished_at_utc"] = _utc_now_z()
        job["stdout"] = stdout
        job["stderr"] = stderr
        job["run_id"] = resolved_run_id
        if returncode == 0 and result_summary.get("status") == "SUCCESS":
            job["status"] = "SUCCESS"
            job["result"] = result_summary
        else:
            job["status"] = "FAIL"
            err = stderr or stdout or f"Exit code {returncode}"
            job["error"] = _truncate(err, ERROR_TRUNCATE)
            if result_summary:
                job["result"] = result_summary
        store.save(job)
        store.release_claim(tid, lid, jid)
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="MortgageDocAI job worker (disk queue)")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL, help="Seconds between polls")
    parser.add_argument("--once", action="store_true", help="Process at most one job then exit")
    parser.add_argument("--tenant-id", default=None, help="Only process jobs for this tenant")
    parser.add_argument("--loan-id", default=None, help="Only process jobs for this loan")
    args = parser.parse_args()
    base_path = os.environ.get("NAS_ANALYZE", "/mnt/nas_apps/nas_analyze")
    get_base_path: Callable[[], Path] = lambda: Path(base_path)
    from loan_service.adapters_disk import DiskJobStore, LoanLockImpl
    from loan_service.adapters_subprocess import SubprocessRunner
    store = DiskJobStore(get_base_path)
    loan_lock = LoanLockImpl(get_base_path)
    runner = SubprocessRunner()
    if args.once:
        _write_heartbeat(get_base_path)
        if run_one_cycle(get_base_path, store, loan_lock, runner, tenant_id=args.tenant_id, loan_id=args.loan_id):
            return 0
        return 0
    while True:
        _write_heartbeat(get_base_path)
        run_one_cycle(get_base_path, store, loan_lock, runner, tenant_id=args.tenant_id, loan_id=args.loan_id)
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    sys.exit(main())
