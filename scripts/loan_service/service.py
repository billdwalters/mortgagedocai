"""Job service orchestrator: enqueue, get, list with same semantics as job_runner."""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from .adapters_disk import (
    ERROR_TRUNCATE,
    STDOUT_TRUNCATE,
    compute_job_key,
    load_manifest_if_present,
    result_from_manifest,
)
from .adapters_disk import _parse_run_id_from_stdout as parse_run_id_from_stdout
from .adapters_disk import _truncate
from .adapters_subprocess import JOB_TIMEOUT_DEFAULT, get_job_env
from .domain import _utc_now_z


class JobService:
    def __init__(
        self,
        store: Any,
        key_index: Any,
        loan_lock: Any,
        runner: Any,
        get_base_path: Any,
    ) -> None:
        self._store = store
        self._key_index = key_index
        self._loan_lock = loan_lock
        self._runner = runner
        self._get_base = get_base_path
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def load_all_from_disk(self) -> None:
        """Load persisted jobs (with restart recovery) and rebuild key index."""
        jobs = self._store.load_all()
        with self._lock:
            self._jobs.update(jobs)
            self._key_index.rebuild(self._jobs)

    def enqueue_job(self, tenant_id: str, loan_id: str, req: dict[str, Any]) -> dict[str, Any]:
        job_key = compute_job_key(tenant_id, loan_id, req)
        with self._lock:
            existing_id = self._key_index.get(job_key)
            if existing_id:
                existing = self._jobs.get(existing_id)
                if existing and existing.get("status") in ("PENDING", "RUNNING", "SUCCESS"):
                    return {
                        "job_id": existing_id,
                        "status": existing["status"],
                        "status_url": f"/jobs/{existing_id}",
                    }
        run_id = req.get("run_id")
        if run_id and "question" not in req:
            result_summary = result_from_manifest(self._get_base, tenant_id, loan_id, run_id)
            if result_summary is not None:
                job_id = str(uuid.uuid4())
                job = {
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
                with self._lock:
                    self._jobs[job_id] = job
                    self._key_index.set(job_key, job_id)
        job["stdout"] = f"PHASE:DONE {_utc_now_z()}\n"
        self._store.save(job)
        return {"job_id": job_id, "status": "SUCCESS", "status_url": f"/jobs/{job_id}"}
        job_id = str(uuid.uuid4())
        job = {
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
        with self._lock:
            self._jobs[job_id] = job
            self._key_index.set(job_key, job_id)
        self._store.save(job)
        return {"job_id": job_id, "status": "PENDING", "status_url": f"/jobs/{job_id}"}

    def _append_phase(self, job: dict[str, Any], name: str) -> None:
        """Append one phase marker line to job stdout (caller holds self._lock)."""
        current = job.get("stdout") or ""
        line = f"PHASE:{name} {_utc_now_z()}\n"
        job["stdout"] = _truncate(current + line, STDOUT_TRUNCATE)

    def _run_worker(self, job_id: str) -> None:
        import subprocess

        with self._lock:
            if job_id not in self._jobs:
                return
            job = self._jobs[job_id]
            tenant_id = job["tenant_id"]
            loan_id = job["loan_id"]
            request = job["request"]
        lock_held = False
        try:
            self._loan_lock.acquire(tenant_id, loan_id, job_id, _utc_now_z())
            lock_held = True
        except Exception as e:
            with self._lock:
                if job_id in self._jobs:
                    self._jobs[job_id]["status"] = "FAIL"
                    self._jobs[job_id]["finished_at_utc"] = _utc_now_z()
                    self._jobs[job_id]["error"] = _truncate(str(e), ERROR_TRUNCATE)
                    self._append_phase(self._jobs[job_id], "FAIL")
                    self._store.save(dict(self._jobs[job_id]))
            return
        try:
            with self._lock:
                if job_id not in self._jobs:
                    return
                self._jobs[job_id]["status"] = "RUNNING"
                self._jobs[job_id]["started_at_utc"] = _utc_now_z()
                self._store.save(dict(self._jobs[job_id]))
        except Exception:
            if lock_held:
                self._loan_lock.release(tenant_id, loan_id)
            raise
        timeout = request.get("timeout", JOB_TIMEOUT_DEFAULT)
        env = get_job_env(request)
        try:
            returncode, stdout, stderr = self._runner.run(
                request, tenant_id, loan_id, env, timeout
            )
        except subprocess.TimeoutExpired:
            with self._lock:
                if job_id in self._jobs:
                    self._jobs[job_id]["status"] = "FAIL"
                    self._jobs[job_id]["finished_at_utc"] = _utc_now_z()
                    self._jobs[job_id]["error"] = _truncate(f"Job timed out after {timeout}s", ERROR_TRUNCATE)
                    self._append_phase(self._jobs[job_id], "FAIL")
                    self._store.save(dict(self._jobs[job_id]))
            return
        except Exception as e:
            with self._lock:
                if job_id in self._jobs:
                    self._jobs[job_id]["status"] = "FAIL"
                    self._jobs[job_id]["finished_at_utc"] = _utc_now_z()
                    self._jobs[job_id]["error"] = _truncate(str(e), ERROR_TRUNCATE)
                    self._append_phase(self._jobs[job_id], "FAIL")
                    self._store.save(dict(self._jobs[job_id]))
            return
        finally:
            if lock_held:
                self._loan_lock.release(tenant_id, loan_id)
        resolved_run_id = request.get("run_id") or parse_run_id_from_stdout(stdout)
        result_summary: dict[str, Any] = {}
        if "question" in request:
            base = self._get_base()
            run_dir = base / "tenants" / tenant_id / "loans" / loan_id / resolved_run_id if resolved_run_id else None
            result_summary["outputs_base"] = str(run_dir) if run_dir else None
            result_summary["status"] = "SUCCESS" if returncode == 0 else "FAIL"
        elif resolved_run_id:
            manifest = load_manifest_if_present(self._get_base, tenant_id, loan_id, resolved_run_id)
            if manifest:
                base = self._get_base()
                mp = base / "tenants" / tenant_id / "loans" / loan_id / resolved_run_id / "job_manifest.json"
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

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        return {k: v for k, v in job.items() if v is not None and k != "job_key"}

    def list_jobs(self, limit: int = 50, status: str | None = None) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            jobs = list(self._jobs.values())
        if status:
            jobs = [j for j in jobs if j.get("status") == status]
        jobs = sorted(jobs, key=lambda j: j.get("created_at_utc") or "", reverse=True)[:limit]
        return {"jobs": [{k: v for k, v in j.items() if v is not None and k != "job_key"} for j in jobs]}

    def get_jobs_raw(self) -> dict[str, dict[str, Any]]:
        """For job_runner facade: return internal jobs dict."""
        with self._lock:
            return dict(self._jobs)

    def get_jobs_mutable(self) -> dict[str, dict[str, Any]]:
        """For job_runner facade: return the actual in-memory jobs dict (so tests can .clear())."""
        return self._jobs

    def get_key_index_mutable(self) -> dict[str, str]:
        """For job_runner facade: return the actual key index dict (so tests can .clear())."""
        return self._key_index.mutable_dict()

    def clear_jobs(self) -> None:
        """For tests: clear in-memory state."""
        with self._lock:
            self._jobs.clear()
        self._key_index.rebuild({})
