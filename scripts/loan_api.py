#!/usr/bin/env python3
"""
MortgageDocAI — minimal LOCAL-ONLY FastAPI service wrapping run_loan_job.py and step12/step13.

Background jobs: disk-backed JobService (DiskJobStore); job_worker.py polls disk and runs run_loan_job.py.
No cloud APIs. No Redis/DB/Celery. Jobs persist across restarts.

Usage:
  python3 scripts/loan_api.py --host 127.0.0.1 --port 8000
  For LAN access: python3 scripts/loan_api.py --host 0.0.0.0 --port 8000
  From client: http://10.10.10.190:8000

  Jobs: POST /tenants/{tenant_id}/loans/{loan_id}/jobs to submit, GET /jobs/{job_id} to poll, GET /jobs to list.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Paths (local-only; no cloud)
# ---------------------------------------------------------------------------
_scripts_dir = Path(__file__).resolve().parent
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

REPO_ROOT = _scripts_dir.parent
SCRIPTS_DIR = _scripts_dir
NAS_ANALYZE = Path("/mnt/nas_apps/nas_analyze")
# Source-of-truth root for original loan folders (env with fallback)
SOURCE_LOANS_ROOT = Path(os.environ.get("MORTGAGEDOCAI_SOURCE_LOANS_ROOT", "/mnt/source_loans").strip())
# Category folders under SOURCE_LOANS_ROOT to scan (avoids listing root, which can hit #recycle permission denied)
_SOURCE_CATEGORIES_RAW = os.environ.get("MORTGAGEDOCAI_SOURCE_LOANS_CATEGORIES", "5-Borrowers TBD").strip()
SOURCE_LOANS_CATEGORIES = [s.strip() for s in _SOURCE_CATEGORIES_RAW.split(",") if s.strip()]
DEFAULT_SOURCE_BASE = str(SOURCE_LOANS_ROOT / "5-Borrowers TBD")

STDOUT_TRUNCATE = 50_000
STDERR_TRUNCATE = 50_000
ERROR_TRUNCATE = 4_000
JOB_TIMEOUT_DEFAULT = 3600

_RUN_ID_LINE_RE = re.compile(r"run_id\s*=\s*(\S+)")

# Ollama URL for LLM model list (server-side only; used by GET /ollama/models)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").strip()

# API security (env at load time)
_API_KEY = os.environ.get("MORTGAGEDOCAI_API_KEY", "").strip()
_ALLOWED_TENANTS_RAW = os.environ.get("MORTGAGEDOCAI_ALLOWED_TENANTS", "").strip()
_ALLOWED_TENANTS: set[str] = (
    set(t.strip() for t in _ALLOWED_TENANTS_RAW.split(",") if t.strip())
    if _ALLOWED_TENANTS_RAW else set()
)

def _get_base_path() -> Path:
    return NAS_ANALYZE


# ---------------------------------------------------------------------------
# Disk-backed job service — single source of truth (replaces JOBS/JOBS_LOCK)
# ---------------------------------------------------------------------------
from loan_service.adapters_disk import DiskJobStore, JobKeyIndexImpl, LoanLockImpl, _truncate
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


def _quiet_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    env["TRANSFORMERS_VERBOSITY"] = "error"
    env["TQDM_MININTERVAL"] = "999999"
    env["PYTHONPATH"] = str(SCRIPTS_DIR)
    return env


_RUN_ID_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}Z$")


def _utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def _is_loan_dir(name: str) -> bool:
    if not name or name.startswith("."):
        return False
    return name.replace("-", "").replace("_", "").isalnum() or name.isdigit()


# Source-of-truth loan folder detection (under SOURCE_LOANS_ROOT, 2-level enumerate)
_LOAN_FOLDER_RE = re.compile(r"\[Loan\s+(\d+)\]")
_LOAN_ID_FALLBACK_RE = re.compile(r"\d{6,}")


def _is_source_loan_folder(name: str) -> bool:
    if not name or name.startswith("."):
        return False
    if "[Loan" in name and "]" in name:
        return True
    return "Loan" in name and any(c.isdigit() for c in name)


def _extract_loan_id_from_folder_name(name: str) -> str | None:
    m = _LOAN_FOLDER_RE.search(name)
    if m:
        return m.group(1)
    m = _LOAN_ID_FALLBACK_RE.search(name)
    if m:
        return m.group(0)
    return None


def _run_id_to_utc_iso(run_id: str) -> str:
    """Convert YYYY-MM-DDTHHMMSSZ to YYYY-MM-DDTHH:MM:SSZ."""
    if len(run_id) != 20 or run_id[10] != "T" or run_id[-1] != "Z":
        return run_id
    return run_id[:11] + run_id[11:13] + ":" + run_id[13:15] + ":" + run_id[15:17] + run_id[17:]


def _source_loan_last_modified_utc(path: Path) -> str:
    """Max mtime of path and its immediate children (one level), as UTC ISO Z."""
    try:
        best = path.stat().st_mtime if path.exists() else 0.0
        if path.is_dir():
            for c in path.iterdir():
                try:
                    best = max(best, c.stat().st_mtime)
                except OSError:
                    pass
        return datetime.fromtimestamp(best, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _last_processed_run_for_loan(tenant_id: str, loan_id: str) -> tuple[str | None, str | None]:
    """Return (last_processed_run_id, last_processed_utc) or (None, None). Run dirs must match YYYY-MM-DDTHHMMSSZ."""
    loans_dir = NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id
    if not loans_dir.is_dir():
        return (None, None)
    run_ids = [
        d.name for d in loans_dir.iterdir()
        if d.is_dir() and _RUN_ID_PATTERN.match(d.name)
    ]
    if not run_ids:
        return (None, None)
    run_ids.sort(reverse=True)
    run_id = run_ids[0]
    return (run_id, _run_id_to_utc_iso(run_id))


def _list_source_loan_items(tenant_id: str) -> list[dict[str, Any]]:
    """Enumerate loan folders under SOURCE_LOANS_ROOT (2 levels), attach last_processed and needs_reprocess.
    Only scans category folders named in SOURCE_LOANS_CATEGORIES to avoid listing root (e.g. #recycle permission denied)."""
    root = SOURCE_LOANS_ROOT.resolve()
    items: list[dict[str, Any]] = []
    for cat_name in SOURCE_LOANS_CATEGORIES:
        if not cat_name or cat_name.startswith("."):
            continue
        cat = root / cat_name
        try:
            if not cat.is_dir():
                continue
            sub_dirs = sorted(cat.iterdir())
        except OSError:
            continue  # skip inaccessible dirs
        for d in sub_dirs:
            try:
                if not d.is_dir() or not _is_source_loan_folder(d.name):
                    continue
                loan_id = _extract_loan_id_from_folder_name(d.name)
                if not loan_id:
                    continue
                source_path = str(d.resolve())
                source_last_modified_utc = _source_loan_last_modified_utc(d)
                last_processed_run_id, last_processed_utc = _last_processed_run_for_loan(tenant_id, loan_id)
                needs_reprocess = (
                    last_processed_run_id is None
                    or (source_last_modified_utc > last_processed_utc)
                )
                items.append({
                    "loan_id": loan_id,
                    "folder_name": d.name,
                    "source_path": source_path,
                    "source_last_modified_utc": source_last_modified_utc,
                    "last_processed_run_id": last_processed_run_id,
                    "last_processed_utc": last_processed_utc,
                    "needs_reprocess": needs_reprocess,
                })
            except OSError:
                continue  # skip inaccessible subdirs
    items.sort(key=lambda x: x["loan_id"])
    items.sort(key=lambda x: x["source_last_modified_utc"] or "", reverse=True)
    items.sort(key=lambda x: x["needs_reprocess"], reverse=True)
    return items


PROFILE_FILE_NAMES = (
    "answer.json",
    "answer.md",
    "citations.jsonl",
    "income_analysis.json",
    "dti.json",
    "decision.json",
    "decision.md",
    "version.json",
)


def _safe_single_component(name: str) -> bool:
    if not name or ".." in name or "/" in name or "\\" in name:
        return False
    return True


def _media_type_for_filename(filename: str) -> str:
    if filename.endswith(".json") and filename != ".json":
        return "application/json"
    if filename.endswith(".jsonl"):
        return "application/x-ndjson"
    if filename.endswith(".md"):
        return "text/markdown; charset=utf-8"
    return "application/octet-stream"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _build_artifacts_index(tenant_id: str, loan_id: str, run_id: str) -> dict[str, Any]:
    base = NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id / run_id
    if not base.is_dir():
        raise FileNotFoundError("Run not found")
    base_dir = str(base.resolve())
    retrieve_base = NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id / "retrieve" / run_id
    rp_path = retrieve_base / "retrieval_pack.json"
    rp_exists = rp_path.is_file()
    rp_sha256: str | None = None
    if rp_exists:
        try:
            rp_sha256 = _sha256_file(rp_path)
        except OSError:
            pass
    manifest_path = base / "job_manifest.json"
    manifest_exists = manifest_path.is_file()
    manifest_status: str | None = None
    if manifest_exists:
        try:
            with manifest_path.open() as f:
                manifest = json.load(f)
            manifest_status = manifest.get("status") if isinstance(manifest, dict) else None
        except (json.JSONDecodeError, OSError):
            pass
    retrieval_pack = {
        "path": str(rp_path.resolve()) if rp_path.exists() else None,
        "sha256": rp_sha256,
        "exists": rp_exists,
    }
    job_manifest = {
        "path": str(manifest_path.resolve()) if manifest_path.exists() else None,
        "exists": manifest_exists,
        "status": manifest_status,
    }
    profiles_dir = base / "outputs" / "profiles"
    profiles_list: List[dict[str, Any]] = []
    if profiles_dir.is_dir():
        profile_dirs = sorted([d for d in profiles_dir.iterdir() if d.is_dir()])
        for prof_dir in profile_dirs:
            name = prof_dir.name
            files_list: List[dict[str, Any]] = []
            for fname in PROFILE_FILE_NAMES:
                fpath = prof_dir / fname
                exists = fpath.is_file()
                entry: dict[str, Any] = {
                    "name": fname,
                    "path": str(fpath.resolve()),
                    "exists": exists,
                }
                if exists:
                    try:
                        st = fpath.stat()
                        entry["size_bytes"] = st.st_size
                        entry["mtime_utc"] = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                    except OSError:
                        entry["size_bytes"] = None
                        entry["mtime_utc"] = None
                else:
                    entry["size_bytes"] = None
                    entry["mtime_utc"] = None
                files_list.append(entry)
            profiles_list.append({
                "name": name,
                "dir": str(prof_dir.resolve()),
                "files": files_list,
            })
    return {
        "tenant_id": tenant_id,
        "loan_id": loan_id,
        "run_id": run_id,
        "base_dir": base_dir,
        "retrieval_pack": retrieval_pack,
        "job_manifest": job_manifest,
        "profiles": profiles_list,
    }


# ---------------------------------------------------------------------------
# Request body models
# ---------------------------------------------------------------------------
class StartRunBody(BaseModel):
    run_id: str | None = None
    skip_intake: bool = False
    skip_process: bool = False
    source_path: str | None = None


class StartRunRequest(BaseModel):
    """Body for POST .../runs/start — full pipeline from source_path."""
    source_path: str
    run_id: str | None = None
    run_llm: bool = False
    offline_embeddings: bool = True
    top_k: int = 80
    max_per_file: int = 12
    max_dropped_chunks: int = 5
    expect_rp_hash_stable: bool = True
    smoke_debug: bool = False


class SubmitJobBody(BaseModel):
    run_id: str | None = None
    skip_intake: bool = False
    skip_process: bool = False
    source_path: str | None = None
    offline_embeddings: bool = True
    top_k: int = 80
    max_per_file: int = 12
    max_dropped_chunks: int = 5
    expect_rp_hash_stable: bool = False
    smoke_debug: bool = False
    llm_model: str | None = None
    run_llm: bool | None = None
    timeout: int | None = Field(None, description="Subprocess timeout in seconds (default 3600)")


class QueryBody(BaseModel):
    question: str
    profile: str = Field("default", description="default | uw_conditions | income_analysis | uw_decision")
    llm_model: str | None = None
    offline_embeddings: bool = True
    top_k: int | None = None
    max_per_file: int | None = None


class ValidateSourcePathRequest(BaseModel):
    """Body for POST .../source_path/validate"""
    source_path: str | None = None


# ---------------------------------------------------------------------------
# App and routes
# ---------------------------------------------------------------------------
app = FastAPI(title="MortgageDocAI Loan API", description="Local-only loan analysis API")


class _SecurityMiddleware(BaseHTTPMiddleware):
    """Require X-API-Key when MORTGAGEDOCAI_API_KEY is set; 404 when tenant_id not in MORTGAGEDOCAI_ALLOWED_TENANTS.
    /ui and /ui/static are exempt so the UI can load; the page then sends the key on API calls."""
    _UI_PREFIX = "/ui"

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == self._UI_PREFIX or path.startswith(self._UI_PREFIX + "/"):
            return await call_next(request)
        if _API_KEY:
            key = request.headers.get("X-API-Key") or ""
            if key != _API_KEY:
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        if _ALLOWED_TENANTS:
            parts = path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "tenants":
                tenant_id = parts[1]
                if tenant_id not in _ALLOWED_TENANTS:
                    return JSONResponse(
                        status_code=404,
                        content={"detail": "Tenant not in allowed list", "tenant_id": tenant_id},
                    )
        return await call_next(request)


app.add_middleware(_SecurityMiddleware)


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


WORKER_HEARTBEAT_MAX_AGE_SEC = 300  # 5 minutes


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


@app.on_event("startup")
async def _startup() -> None:
    """Load persisted jobs on startup (restart recovery) then resume any PENDING jobs.

    For RUNNING jobs whose systemd scope is still active (API was killed during a run),
    the job is restored to RUNNING and a watcher thread finalises it when the scope exits.
    For all other RUNNING jobs, load_all() applies standard manifest-based recovery.
    """
    # 1. Find jobs that are RUNNING on disk and whose systemd scope is still alive.
    orphaned = _find_orphaned_running_jobs()

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



@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "MortgageDocAI Loan API",
        "docs": "/docs",
        "health": "/health",
        "tenants": "/tenants/{tenant_id}/loans",
        "jobs": "/tenants/{tenant_id}/loans/{loan_id}/jobs",
        "ui": "/ui",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ollama/models")
def ollama_models() -> dict[str, Any]:
    """Return list of model names from Ollama (GET /api/tags). Enables UI dropdown of installed LLMs."""
    url = OLLAMA_URL.rstrip("/") + "/api/tags"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        models = [m.get("name", m.get("model", "")) for m in data.get("models", []) if m.get("name") or m.get("model")]
        return {"models": models, "ollama_url": OLLAMA_URL}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Ollama unreachable: {e!s}")


@app.get("/browse/source")
def browse_source(base: str = DEFAULT_SOURCE_BASE) -> dict[str, Any]:
    """List direct subdirectories under an allowed source path. base must be under SOURCE_LOANS_ROOT."""
    base_path = Path(base.strip()).resolve()
    try:
        root_resolved = SOURCE_LOANS_ROOT.resolve()
        base_path.relative_to(root_resolved)
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail="base path must be under " + str(SOURCE_LOANS_ROOT))
    if not base_path.is_dir():
        raise HTTPException(status_code=404, detail="Path not found or not a directory")
    folders = [d.name for d in sorted(base_path.iterdir()) if d.is_dir() and not d.name.startswith(".")]
    return {"base": str(base_path), "folders": folders}


@app.get("/tenants/{tenant_id}/loans")
def list_loans(tenant_id: str) -> dict[str, list[str]]:
    loans_dir = NAS_ANALYZE / "tenants" / tenant_id / "loans"
    if not loans_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Tenant loans path not found: {loans_dir}")
    loan_ids = [
        d.name for d in loans_dir.iterdir()
        if d.is_dir() and _is_loan_dir(d.name)
    ]
    return {"loan_ids": sorted(loan_ids)}


def _ensure_source_loans_root_mounted() -> None:
    if not SOURCE_LOANS_ROOT.resolve().is_dir():
        raise HTTPException(status_code=500, detail="Source loans root not mounted")


@app.get("/tenants/{tenant_id}/source_loans")
def list_source_loans(tenant_id: str) -> dict[str, Any]:
    """List source-of-truth loan folders under SOURCE_LOANS_ROOT with last_processed and needs_reprocess."""
    _ensure_source_loans_root_mounted()
    items = _list_source_loan_items(tenant_id)
    return {"source_root": str(SOURCE_LOANS_ROOT.resolve()), "items": items}


@app.get("/tenants/{tenant_id}/source_loans/{loan_id}")
def get_source_loan(tenant_id: str, loan_id: str) -> dict[str, Any]:
    """Get source path and metadata for a single loan by loan_id."""
    _ensure_source_loans_root_mounted()
    items = _list_source_loan_items(tenant_id)
    for it in items:
        if it["loan_id"] == loan_id:
            return {
                "loan_id": it["loan_id"],
                "source_path": it["source_path"],
                "source_last_modified_utc": it["source_last_modified_utc"],
            }
    raise HTTPException(status_code=404, detail="Source loan not found")


@app.post("/tenants/{tenant_id}/loans/{loan_id}/runs", status_code=202)
def start_run(tenant_id: str, loan_id: str, body: StartRunBody) -> dict[str, Any]:
    if not body.skip_intake and not body.source_path:
        raise HTTPException(status_code=422, detail="source_path is required when skip_intake is False")
    if body.skip_process and not body.run_id:
        raise HTTPException(status_code=422, detail="run_id is required when skip_process is True (run_loan_job.py contract)")
    cmd = [
        sys.executable,
        str(SCRIPTS_DIR / "run_loan_job.py"),
        "--tenant-id", tenant_id,
        "--loan-id", loan_id,
    ]
    if body.run_id:
        cmd += ["--run-id", body.run_id]
    if body.skip_intake:
        cmd += ["--skip-intake"]
    if body.skip_process:
        cmd += ["--skip-process"]
    if body.source_path:
        cmd += ["--source-path", body.source_path]
    try:
        subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            env=_quiet_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start job: {e}. Paths: cwd={REPO_ROOT}, cmd={cmd}")
    return {
        "tenant_id": tenant_id,
        "loan_id": loan_id,
        "run_id": body.run_id,
        "status": "STARTED",
    }


@app.get("/tenants/{tenant_id}/loans/{loan_id}/runs")
def list_runs(tenant_id: str, loan_id: str) -> dict[str, list[str]]:
    loan_dir = NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id
    if not loan_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Loan path not found: {loan_dir}")
    run_ids = []
    for d in loan_dir.iterdir():
        if not d.is_dir():
            continue
        if (d / "job_manifest.json").exists() or (d / "outputs").is_dir():
            run_ids.append(d.name)
    return {"run_ids": sorted(run_ids)}


@app.get("/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}")
def get_run_status(tenant_id: str, loan_id: str, run_id: str) -> dict[str, Any]:
    manifest_path = NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id / run_id / "job_manifest.json"
    if not manifest_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"job_manifest.json not found at {manifest_path}. Run may not exist or job not finished.",
        )
    try:
        with manifest_path.open() as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Invalid JSON in {manifest_path}: {e}")


@app.get("/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/artifacts")
def get_run_artifacts(tenant_id: str, loan_id: str, run_id: str) -> dict[str, Any]:
    try:
        return _build_artifacts_index(tenant_id, loan_id, run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")


@app.get("/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/artifacts/{profile}/{filename}")
def get_profile_artifact(
    tenant_id: str, loan_id: str, run_id: str, profile: str, filename: str
) -> FileResponse:
    base = NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id / run_id
    if not base.is_dir():
        raise HTTPException(status_code=404, detail="Run not found")
    if not _safe_single_component(profile) or not _safe_single_component(filename):
        raise HTTPException(status_code=404, detail="Artifact not found")
    if filename not in PROFILE_FILE_NAMES:
        raise HTTPException(status_code=404, detail="Artifact not found")
    profiles_base = (base / "outputs" / "profiles").resolve()
    candidate = (profiles_base / profile / filename).resolve()
    try:
        candidate.relative_to(profiles_base)
    except ValueError:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(
        path=str(candidate),
        media_type=_media_type_for_filename(filename),
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@app.get("/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/retrieval_pack")
def get_retrieval_pack(tenant_id: str, loan_id: str, run_id: str) -> FileResponse:
    rp_path = (
        NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id
        / "retrieve" / run_id / "retrieval_pack.json"
    )
    if not rp_path.is_file():
        raise HTTPException(status_code=404, detail="Retrieval pack not found")
    return FileResponse(
        path=str(rp_path.resolve()),
        media_type="application/json",
        headers={"Content-Disposition": 'inline; filename="retrieval_pack.json"'},
    )


@app.get("/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/job_manifest")
def get_job_manifest(tenant_id: str, loan_id: str, run_id: str) -> FileResponse:
    manifest_path = (
        NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id / run_id
        / "job_manifest.json"
    )
    if not manifest_path.is_file():
        raise HTTPException(status_code=404, detail="Job manifest not found")
    return FileResponse(
        path=str(manifest_path.resolve()),
        media_type="application/json",
        headers={"Content-Disposition": 'inline; filename="job_manifest.json"'},
    )


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
    _warn_if_no_recent_worker_heartbeat()
    return {
        "job_id": result["job_id"],
        "run_id": run_id,
        "status": result["status"],
        "status_url": result["status_url"],
    }


@app.post("/tenants/{tenant_id}/loans/{loan_id}/source_path/validate")
def validate_source_path(tenant_id: str, loan_id: str, body: ValidateSourcePathRequest) -> dict:
    """Validate a proposed source_path: exists, is_dir, within SOURCE_LOANS_ROOT."""
    raw = (body.source_path or "").strip() if body.source_path is not None else ""
    if not raw:
        return {
            "ok": False, "exists": False, "is_dir": False,
            "within_root": False, "normalized": None,
            "message": "source_path required",
        }
    try:
        p = Path(raw).resolve()
        normalized = str(p)
    except Exception:
        return {
            "ok": False, "exists": False, "is_dir": False,
            "within_root": False, "normalized": None,
            "message": "source_path invalid",
        }
    exists = p.exists()
    is_dir = p.is_dir()
    try:
        root_resolved = SOURCE_LOANS_ROOT.resolve()
        p.relative_to(root_resolved)
        within_root = True
    except (ValueError, OSError):
        within_root = False
    if not within_root:
        return {
            "ok": False, "exists": exists, "is_dir": is_dir,
            "within_root": False, "normalized": normalized,
            "message": "source_path must be under SOURCE_LOANS_ROOT",
        }
    if not exists:
        return {
            "ok": False, "exists": False, "is_dir": False,
            "within_root": True, "normalized": normalized,
            "message": "source_path not found",
        }
    if not is_dir:
        return {
            "ok": False, "exists": True, "is_dir": False,
            "within_root": True, "normalized": normalized,
            "message": "source_path is not a directory",
        }
    return {
        "ok": True, "exists": True, "is_dir": True,
        "within_root": True, "normalized": normalized,
        "message": "ok",
    }


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
    _warn_if_no_recent_worker_heartbeat()
    return result


@app.post("/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/query")
def query_run(tenant_id: str, loan_id: str, run_id: str, body: QueryBody) -> dict[str, Any]:
    valid_profiles = ("default", "uw_conditions", "income_analysis", "uw_decision")
    if body.profile not in valid_profiles:
        raise HTTPException(status_code=422, detail=f"profile must be one of {valid_profiles}")
    env = _quiet_env()
    step13 = str(SCRIPTS_DIR / "step13_build_retrieval_pack.py")
    step12 = str(SCRIPTS_DIR / "step12_analyze.py")
    step13_cmd = [
        sys.executable, step13,
        "--tenant-id", tenant_id,
        "--loan-id", loan_id,
        "--run-id", run_id,
        "--query", body.question,
        "--out-run-id", run_id,
        "--top-k", str(body.top_k or 80),
        "--max-per-file", str(body.max_per_file or 12),
    ]
    if body.offline_embeddings:
        step13_cmd.append("--offline-embeddings")
    result13 = subprocess.run(step13_cmd, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True)
    if result13.returncode != 0:
        stderr_tail = (result13.stderr or result13.stdout or "")[-2000:]
        raise HTTPException(status_code=500, detail=f"Step13 failed (exit {result13.returncode}). stderr tail: {stderr_tail}")
    step12_cmd = [
        sys.executable, step12,
        "--tenant-id", tenant_id,
        "--loan-id", loan_id,
        "--run-id", run_id,
        "--query", body.question,
        "--analysis-profile", body.profile,
        "--no-auto-retrieve",
    ]
    if body.llm_model and body.profile != "uw_decision":
        step12_cmd += ["--llm-model", body.llm_model]
    # Lower evidence + tokens for API "Ask a question" so Ollama can complete on weak/sandbox servers
    step12_cmd += ["--ollama-timeout", "600", "--evidence-max-chars", "6000", "--llm-max-tokens", "400"]
    result12 = subprocess.run(step12_cmd, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True)
    if result12.returncode != 0:
        stderr_tail = (result12.stderr or result12.stdout or "")[-2000:]
        raise HTTPException(status_code=500, detail=f"Step12 failed (exit {result12.returncode}). stderr tail: {stderr_tail}")
    answer_path = (
        NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id / run_id
        / "outputs" / "profiles" / body.profile / "answer.json"
    )
    if not answer_path.exists():
        stderr_tail = (result12.stderr or result12.stdout or "")[-2000:]
        raise HTTPException(status_code=500, detail=f"Profile output missing: {answer_path}. Step12 stderr tail: {stderr_tail}")
    try:
        with answer_path.open() as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Invalid JSON in {answer_path}: {e}")


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
    _warn_if_no_recent_worker_heartbeat()
    return result


@app.get("/jobs/{job_id}")
def get_job_status(job_id: str) -> dict[str, Any]:
    job = _service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/jobs")
def list_jobs(limit: int = 50, status: str | None = None) -> dict[str, list[dict[str, Any]]]:
    return _service.list_jobs(limit=limit, status=status)


# ---------------------------------------------------------------------------
# Web UI (static single-page app)
# IMPORTANT: Access via http://<server>:8000/ui — file:// is not supported.
# ---------------------------------------------------------------------------
WEBUI_DIR = SCRIPTS_DIR / "webui"


@app.get("/ui")
def serve_ui() -> FileResponse:
    index_path = WEBUI_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=404, detail="UI not found")
    return FileResponse(path=str(index_path.resolve()), media_type="text/html")


app.mount("/ui/static", StaticFiles(directory=str(WEBUI_DIR)), name="webui_static")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="MortgageDocAI Loan API (local-only)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (0.0.0.0 for LAN)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    args = parser.parse_args()
    print(f"Starting Loan API on {args.host}:{args.port} (0.0.0.0 = listen on all interfaces)", flush=True)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
