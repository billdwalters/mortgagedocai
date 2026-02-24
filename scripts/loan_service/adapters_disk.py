"""Disk-backed job store, job-key index, loan lock; manifest/truncation helpers."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .domain import _utc_now_z

# Same caps and limits as job_runner
STDOUT_TRUNCATE = 50_000
STDERR_TRUNCATE = 50_000
ERROR_TRUNCATE = 4_000
JOB_RELOAD_LIMIT = int(os.environ.get("JOB_RELOAD_LIMIT", "500"))
JOB_RETENTION_DAYS = int(os.environ.get("JOB_RETENTION_DAYS", "30"))
LOCK_RETRY_SEC = 2

_RUN_ID_LINE_RE = re.compile(r"run_id\s*=\s*(\S+)")


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


def compute_job_key(tenant_id: str, loan_id: str, req: dict[str, Any]) -> str:
    """Stable sha256 of tenant_id + loan_id + request for idempotency."""
    payload = {"tenant_id": tenant_id, "loan_id": loan_id, "request": req}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def load_manifest_if_present(
    get_base: Callable[[], Path],
    tenant_id: str,
    loan_id: str,
    run_id: str,
) -> dict[str, Any] | None:
    base = get_base()
    mp = base / "tenants" / tenant_id / "loans" / loan_id / run_id / "job_manifest.json"
    if not mp.exists():
        return None
    try:
        with mp.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def result_from_manifest(
    get_base: Callable[[], Path],
    tenant_id: str,
    loan_id: str,
    run_id: str,
) -> dict[str, Any] | None:
    """Build result summary from manifest if present and SUCCESS; else None."""
    manifest = load_manifest_if_present(get_base, tenant_id, loan_id, run_id)
    if not manifest or manifest.get("status") != "SUCCESS":
        return None
    base = get_base()
    mp = base / "tenants" / tenant_id / "loans" / loan_id / run_id / "job_manifest.json"
    return {
        "manifest_path": str(mp),
        "status": manifest.get("status"),
        "rp_sha256": manifest.get("retrieval_pack_sha256"),
        "outputs_base": str(mp.parent) if mp.parent else None,
    }


class DiskJobStore:
    """JSON-on-disk job store; same paths and atomic write as job_runner."""

    def __init__(self, get_base_path: Callable[[], Path]):
        self._get_base = get_base_path

    def _job_file_path(self, tenant_id: str, loan_id: str, job_id: str) -> Path:
        base = self._get_base()
        return base / "tenants" / tenant_id / "loans" / loan_id / "_meta" / "jobs" / f"{job_id}.json"

    def _claim_file_path(self, tenant_id: str, loan_id: str, job_id: str) -> Path:
        base = self._get_base()
        return base / "tenants" / tenant_id / "loans" / loan_id / "_meta" / "jobs" / f"{job_id}.claim"

    def load_job(self, tenant_id: str, loan_id: str, job_id: str) -> dict[str, Any] | None:
        """Load a single job from disk. Returns None if missing or invalid."""
        path = self._job_file_path(tenant_id, loan_id, job_id)
        if not path.exists():
            return None
        try:
            with path.open() as f:
                job = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(job, dict) or job.get("job_id") != job_id:
            return None
        return job

    def list_pending_jobs(
        self,
        tenant_id: str | None = None,
        loan_id: str | None = None,
    ) -> list[tuple[str, str, str]]:
        """List (tenant_id, loan_id, job_id) for jobs with status PENDING, optionally filtered."""
        base = self._get_base()
        tenants_dir = base / "tenants"
        if not tenants_dir.is_dir():
            return []
        out: list[tuple[str, str, str]] = []
        try:
            for tdir in tenants_dir.iterdir():
                if not tdir.is_dir():
                    continue
                tid = tdir.name
                if tenant_id is not None and tid != tenant_id:
                    continue
                loans_dir = tdir / "loans"
                if not loans_dir.is_dir():
                    continue
                for ldir in loans_dir.iterdir():
                    if not ldir.is_dir():
                        continue
                    lid = ldir.name
                    if loan_id is not None and lid != loan_id:
                        continue
                    jobs_dir = ldir / "_meta" / "jobs"
                    if not jobs_dir.is_dir():
                        continue
                    for p in jobs_dir.iterdir():
                        if not p.is_file() or p.suffix != ".json":
                            continue
                        try:
                            with p.open() as f:
                                job = json.load(f)
                        except (json.JSONDecodeError, OSError):
                            continue
                        if not isinstance(job, dict) or job.get("status") != "PENDING":
                            continue
                        jid = job.get("job_id")
                        if jid:
                            out.append((tid, lid, jid))
        except OSError:
            pass
        return out

    def try_claim(self, tenant_id: str, loan_id: str, job_id: str) -> bool:
        """Atomically claim a job (create .claim file with O_CREAT|O_EXCL). Returns True if claimed."""
        path = self._claim_file_path(tenant_id, loan_id, job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, json.dumps({"claimed_at_utc": _utc_now_z()}).encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            return False
        except OSError:
            return False

    def release_claim(self, tenant_id: str, loan_id: str, job_id: str) -> None:
        """Remove claim file for a job."""
        path = self._claim_file_path(tenant_id, loan_id, job_id)
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    def clear_stale_claims(self, max_age_sec: int = 300) -> None:
        """Remove claim files for jobs that are still PENDING and claim is older than max_age_sec."""
        base = self._get_base()
        tenants_dir = base / "tenants"
        if not tenants_dir.is_dir():
            return
        now = time.time()
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
                        if not p.is_file() or p.suffix != ".claim":
                            continue
                        try:
                            if now - p.stat().st_mtime > max_age_sec:
                                job_id = p.stem
                                job_path = jobs_dir / f"{job_id}.json"
                                if job_path.exists():
                                    with job_path.open() as f:
                                        job = json.load(f)
                                    if isinstance(job, dict) and job.get("status") == "PENDING":
                                        p.unlink()
                        except (OSError, json.JSONDecodeError):
                            pass
        except OSError:
            pass

    def load_all(self) -> dict[str, dict[str, Any]]:
        """Load jobs from disk, apply restart recovery for RUNNING, run retention; return job dict."""
        base = self._get_base()
        tenants_dir = base / "tenants"
        if not tenants_dir.is_dir():
            return {}
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
            return {}
        collected.sort(key=lambda x: x[1], reverse=True)
        to_load = collected[:JOB_RELOAD_LIMIT]
        now_ts = time.time()
        retention_sec = JOB_RETENTION_DAYS * 24 * 3600
        jobs: dict[str, dict[str, Any]] = {}
        from datetime import datetime, timezone

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
            if job_id in jobs:
                continue
            if not job.get("created_at_utc"):
                job["created_at_utc"] = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            jobs[job_id] = job

        # Restart recovery: for each RUNNING, set SUCCESS from manifest or FAIL, clear lock, persist
        lock = LoanLockImpl(self._get_base)
        for job in list(jobs.values()):
            if job.get("status") != "RUNNING":
                continue
            job_id = job.get("job_id")
            tenant_id = job.get("tenant_id")
            loan_id = job.get("loan_id")
            run_id = job.get("run_id")
            if not tenant_id or not loan_id:
                continue
            result_summary = result_from_manifest(self._get_base, tenant_id, loan_id, run_id) if run_id else None
            lock.clear_if_stale(tenant_id, loan_id)
            if result_summary is not None:
                job["status"] = "SUCCESS"
                job["finished_at_utc"] = _utc_now_z()
                job["result"] = result_summary
                job["error"] = None
            else:
                job["status"] = "FAIL"
                job["finished_at_utc"] = _utc_now_z()
                job["error"] = _truncate("Recovered after restart: job was RUNNING but no active worker", ERROR_TRUNCATE)
            self.save(job)

        for path, mtime in collected:
            if now_ts - mtime > retention_sec:
                try:
                    path.unlink()
                except OSError as e:
                    print(f"[job_runner] retention delete failed {path}: {e}", file=sys.stderr)
        return jobs

    def save(self, job: dict[str, Any]) -> None:
        tenant_id = job.get("tenant_id")
        loan_id = job.get("loan_id")
        job_id = job.get("job_id")
        if not tenant_id or not loan_id or not job_id:
            return
        path = self._job_file_path(tenant_id, loan_id, job_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w") as f:
                json.dump(job, f, indent=None)
            os.replace(tmp, path)
        except OSError as e:
            print(f"[job_runner] persist failed: {e}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Job-ID index: _meta/job_index/{job_id}.json -> {tenant_id, loan_id}
    # Enables get_job(job_id) to load from disk without any in-memory cache.
    # ------------------------------------------------------------------

    def _job_index_dir(self) -> Path:
        return self._get_base() / "_meta" / "job_index"

    def save_index_entry(self, job_id: str, tenant_id: str, loan_id: str) -> None:
        """Atomically write _meta/job_index/{job_id}.json -> {tenant_id, loan_id}."""
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

    def load_index_entry(self, job_id: str) -> "tuple[str, str] | None":
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


class JobKeyIndexImpl:
    """In-memory job_key -> job_id index."""

    def __init__(self) -> None:
        self._index: dict[str, str] = {}

    def get(self, job_key: str) -> str | None:
        return self._index.get(job_key)

    def set(self, job_key: str, job_id: str) -> None:
        self._index[job_key] = job_id

    def rebuild(self, jobs: dict[str, dict[str, Any]]) -> None:
        self._index.clear()
        for j in jobs.values():
            if j.get("job_key"):
                self._index[j["job_key"]] = j["job_id"]

    def mutable_dict(self) -> dict[str, str]:
        """Return the underlying dict for facade/tests (e.g. .clear())."""
        return self._index


class LoanLockImpl:
    """Per-loan file lock under .../ _meta/locks/loan.lock."""

    def __init__(self, get_base_path: Callable[[], Path]):
        self._get_base = get_base_path

    def _lock_path(self, tenant_id: str, loan_id: str) -> Path:
        base = self._get_base()
        return base / "tenants" / tenant_id / "loans" / loan_id / "_meta" / "locks" / "loan.lock"

    def acquire(self, tenant_id: str, loan_id: str, job_id: str, created_at_utc: str) -> None:
        lock_path = self._lock_path(tenant_id, loan_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, json.dumps({"job_id": job_id, "created_at_utc": created_at_utc}).encode())
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return
            except FileExistsError:
                time.sleep(LOCK_RETRY_SEC)
            except OSError as e:
                raise RuntimeError(f"Could not acquire loan lock: {e}") from e

    def release(self, tenant_id: str, loan_id: str) -> None:
        lock_path = self._lock_path(tenant_id, loan_id)
        try:
            if lock_path.exists():
                lock_path.unlink()
        except OSError:
            pass

    def clear_if_stale(self, tenant_id: str, loan_id: str) -> None:
        self.release(tenant_id, loan_id)
