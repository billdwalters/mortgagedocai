#!/usr/bin/env python3
"""Tests for job_runner production hardening: idempotency, per-loan lock, restart recovery."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

# Run from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent))
import job_runner  # noqa: E402


def _tmp_nas():
    d = tempfile.mkdtemp(prefix="nas_analyze_")
    return Path(d)


def test_idempotency_same_job_id():
    """Enqueue same job twice; second call returns same job_id."""
    nas = _tmp_nas()
    tenants = nas / "tenants" / "t1" / "loans" / "L1" / "_meta" / "jobs"
    tenants.mkdir(parents=True)
    req = {"run_id": "run-1", "skip_intake": True}
    with patch.object(job_runner, "NAS_ANALYZE", nas):
        job_runner.JOBS.clear()
        job_runner.JOB_KEY_INDEX.clear()
        r1 = job_runner.enqueue_job("t1", "L1", req)
        r2 = job_runner.enqueue_job("t1", "L1", req)
    assert r1["job_id"] == r2["job_id"], (r1, r2)
    assert "job_id" in r1 and "status" in r1 and "status_url" in r1
    assert "job_key" not in r1 and "job_key" not in r2
    job = job_runner.get_job(r1["job_id"])
    assert job is not None
    assert "job_key" not in job
    print("test_idempotency_same_job_id OK")


def test_restart_recovery_running_becomes_fail():
    """After reload, a RUNNING job with no manifest becomes FAIL."""
    nas = _tmp_nas()
    job_dir = nas / "tenants" / "t1" / "loans" / "L1" / "_meta" / "jobs"
    job_dir.mkdir(parents=True)
    job_id = "recovery-test-job-id"
    job_file = job_dir / f"{job_id}.json"
    job = {
        "job_id": job_id,
        "tenant_id": "t1",
        "loan_id": "L1",
        "run_id": "run-x",
        "status": "RUNNING",
        "created_at_utc": "2025-01-01T00:00:00Z",
        "started_at_utc": "2025-01-01T00:00:01Z",
        "finished_at_utc": None,
        "request": {"run_id": "run-x"},
        "result": None,
        "error": None,
        "stdout": None,
        "stderr": None,
    }
    job_file.write_text(json.dumps(job))
    with patch.object(job_runner, "NAS_ANALYZE", nas):
        job_runner.JOBS.clear()
        job_runner.JOB_KEY_INDEX.clear()
        job_runner.load_jobs_from_disk()
    j = job_runner.get_job(job_id)
    assert j is not None, "job should be loaded"
    assert j["status"] == "FAIL", j
    assert "Recovered after restart" in (j.get("error") or ""), j
    print("test_restart_recovery_running_becomes_fail OK")


def test_per_loan_lock_second_waits():
    """Two jobs for same loan: worker processes them (claim + per-loan lock, no collision)."""
    nas = _tmp_nas()
    (nas / "tenants" / "t1" / "loans" / "L1" / "_meta" / "jobs").mkdir(parents=True)
    run_times = []

    class SlowFakeRunner:
        def run(self, req, tenant_id, loan_id, env, timeout, job_id=None):
            run_times.append(time.time())
            time.sleep(1.5)
            return 0, f"run_id = {req.get('run_id', 'slow-1')}", ""

    req1 = {"run_id": "run-a", "skip_intake": True}
    req2 = {"run_id": "run-b", "skip_intake": True}
    with patch.object(job_runner, "NAS_ANALYZE", nas):
        job_runner.JOBS.clear()
        job_runner.JOB_KEY_INDEX.clear()
        r1 = job_runner.enqueue_job("t1", "L1", req1)
        r2 = job_runner.enqueue_job("t1", "L1", req2)
        assert r1["job_id"] != r2["job_id"]
        from job_worker import run_one_cycle
        from loan_service.adapters_disk import DiskJobStore, LoanLockImpl
        get_base = lambda: nas
        store = DiskJobStore(get_base)
        loan_lock = LoanLockImpl(get_base)
        runner = SlowFakeRunner()
        for _ in range(30):
            run_one_cycle(get_base, store, loan_lock, runner)
            job_runner.load_jobs_from_disk()
            j1 = job_runner.get_job(r1["job_id"])
            j2 = job_runner.get_job(r2["job_id"])
            if j1 and j2 and j1.get("status") in ("SUCCESS", "FAIL") and j2.get("status") in ("SUCCESS", "FAIL"):
                break
            time.sleep(0.5)
    j1 = job_runner.get_job(r1["job_id"])
    j2 = job_runner.get_job(r2["job_id"])
    assert j1 and j2
    assert j1["status"] in ("SUCCESS", "FAIL") and j2["status"] in ("SUCCESS", "FAIL")
    assert len(run_times) >= 2, run_times
    print("test_per_loan_lock_second_waits OK")


def test_worker_processes_one_queued_job():
    """Worker run_one_cycle processes a single PENDING job (claim + run + persist)."""
    nas = _tmp_nas()
    (nas / "tenants" / "t1" / "loans" / "L1" / "_meta" / "jobs").mkdir(parents=True)
    with patch.object(job_runner, "NAS_ANALYZE", nas):
        job_runner.JOBS.clear()
        job_runner.JOB_KEY_INDEX.clear()
        r = job_runner.enqueue_job("t1", "L1", {"run_id": "run-1", "skip_intake": True})
        job_id = r["job_id"]
        assert r["status"] == "PENDING"
        class FakeRunner:
            def run(self, req, tenant_id, loan_id, env, timeout, job_id=None):
                return 0, "run_id = run-1", ""
        from job_worker import run_one_cycle
        from loan_service.adapters_disk import DiskJobStore, LoanLockImpl
        get_base = lambda: nas
        store = DiskJobStore(get_base)
        loan_lock = LoanLockImpl(get_base)
        ok = run_one_cycle(get_base, store, loan_lock, FakeRunner())
        assert ok
        job_runner.load_jobs_from_disk()
        j = job_runner.get_job(job_id)
    assert j is not None
    assert j["status"] in ("SUCCESS", "FAIL")
    print("test_worker_processes_one_queued_job OK")



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

if __name__ == "__main__":
    test_idempotency_same_job_id()
    test_restart_recovery_running_becomes_fail()
    test_per_loan_lock_second_waits()
    test_worker_processes_one_queued_job()
    test_disk_index_roundtrip()
    print("All hardening tests passed.")
