#!/usr/bin/env python3
"""
MortgageDocAI — minimal LOCAL-ONLY FastAPI service wrapping run_loan_job.py and step12/step13.

Background jobs: in-memory registry (JOBS), daemon thread runs run_loan_job.py.
No cloud APIs. No Redis/DB/Celery. Jobs lost on restart.

Usage:
  python3 scripts/loan_api.py --host 127.0.0.1 --port 8000
  For LAN access: python3 scripts/loan_api.py --host 0.0.0.0 --port 8000
  From client: http://10.10.10.190:8000

  Jobs: POST /tenants/{tenant_id}/loans/{loan_id}/jobs to submit, GET /jobs/{job_id} to poll, GET /jobs to list.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.requests import Request

# ---------------------------------------------------------------------------
# Paths (local-only; no cloud)
# ---------------------------------------------------------------------------
_scripts_dir = Path(__file__).resolve().parent
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

REPO_ROOT = _scripts_dir.parent
SCRIPTS_DIR = _scripts_dir
NAS_ANALYZE = Path("/mnt/nas_apps/nas_analyze")

STDOUT_TRUNCATE = 50_000
STDERR_TRUNCATE = 50_000
ERROR_TRUNCATE = 4_000
JOB_TIMEOUT_DEFAULT = 3600

_RUN_ID_LINE_RE = re.compile(r"run_id\s*=\s*(\S+)")

# API security (env at load time)
_API_KEY = os.environ.get("MORTGAGEDOCAI_API_KEY", "").strip()
_ALLOWED_TENANTS_RAW = os.environ.get("MORTGAGEDOCAI_ALLOWED_TENANTS", "").strip()
_ALLOWED_TENANTS: set[str] = (
    set(t.strip() for t in _ALLOWED_TENANTS_RAW.split(",") if t.strip())
    if _ALLOWED_TENANTS_RAW else set()
)

# In-memory job registry (minimal; jobs lost on restart)
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def _get_base_path() -> Path:
    return NAS_ANALYZE


def _utc_now_z() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


_RUN_ID_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}Z$")


def _utc_run_id() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def _truncate(s: str, max_len: int) -> str:
    if not s or len(s) <= max_len:
        return s or ""
    return s[:max_len] + "\n... (truncated)"


def _phase(job_id: str, name: str) -> None:
    """Append one phase marker line to JOBS[job_id][\"stdout\"]. Caller must hold JOBS_LOCK."""
    if job_id not in JOBS:
        return
    current = JOBS[job_id].get("stdout") or ""
    line = f"PHASE:{name} {_utc_now_z()}\n"
    JOBS[job_id]["stdout"] = _truncate(current + line, STDOUT_TRUNCATE)


def _parse_run_id_from_stdout(stdout: str) -> str | None:
    for line in stdout.splitlines():
        m = _RUN_ID_LINE_RE.search(line)
        if m:
            return m.group(1).strip()
    return None


def _quiet_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    env["TRANSFORMERS_VERBOSITY"] = "error"
    env["TQDM_MININTERVAL"] = "999999"
    env["PYTHONPATH"] = str(SCRIPTS_DIR)
    return env


def _job_env_from_request(request: dict[str, Any]) -> dict[str, str]:
    env = _quiet_env()
    env["SMOKE_DEBUG"] = "1" if request.get("smoke_debug") else "0"
    if "expect_rp_hash_stable" in request:
        env["EXPECT_RP_HASH_STABLE"] = "1" if request.get("expect_rp_hash_stable") else "0"
    if request.get("max_dropped_chunks") is not None:
        env["MAX_DROPPED_CHUNKS"] = str(request["max_dropped_chunks"])
    return env


def _load_manifest_if_present(tenant_id: str, loan_id: str, run_id: str) -> dict[str, Any] | None:
    base = _get_base_path()
    mp = base / "tenants" / tenant_id / "loans" / loan_id / run_id / "job_manifest.json"
    if not mp.exists():
        return None
    try:
        with mp.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _is_loan_dir(name: str) -> bool:
    if not name or name.startswith("."):
        return False
    return name.replace("-", "").replace("_", "").isalnum() or name.isdigit()


def _run_job_worker(job_id: str, tenant_id: str, loan_id: str, request: dict[str, Any]) -> None:
    """Daemon thread target: set RUNNING, run run_loan_job.py, then update job with result or error."""
    with JOBS_LOCK:
        if job_id not in JOBS:
            return
        JOBS[job_id]["status"] = "RUNNING"
        JOBS[job_id]["started_at_utc"] = _utc_now_z()

    timeout = int(request.get("timeout") or JOB_TIMEOUT_DEFAULT)
    env = _job_env_from_request(request)

    cmd = [
        sys.executable,
        str(SCRIPTS_DIR / "run_loan_job.py"),
        "--tenant-id", tenant_id,
        "--loan-id", loan_id,
    ]
    if request.get("run_id"):
        cmd += ["--run-id", str(request["run_id"])]
    if request.get("skip_intake"):
        cmd += ["--skip-intake"]
    if request.get("skip_process"):
        cmd += ["--skip-process"]
    if request.get("source_path"):
        cmd += ["--source-path", str(request["source_path"])]
    if request.get("smoke_debug"):
        cmd += ["--debug"]
    if request.get("expect_rp_hash_stable"):
        cmd += ["--expect-rp-hash-stable"]
    if request.get("max_dropped_chunks") is not None:
        cmd += ["--max-dropped-chunks", str(int(request["max_dropped_chunks"]))]
    if request.get("offline_embeddings"):
        cmd += ["--offline-embeddings"]
    if request.get("top_k") is not None:
        cmd += ["--top-k", str(int(request["top_k"]))]
    if request.get("max_per_file") is not None:
        cmd += ["--max-per-file", str(int(request["max_per_file"]))]
    if "run_llm" in request:
        if request.get("run_llm"):
            cmd += ["--run-llm"]
        else:
            cmd += ["--no-run-llm"]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        returncode = result.returncode
        stdout = _truncate(result.stdout or "", STDOUT_TRUNCATE)
        stderr = _truncate(result.stderr or "", STDERR_TRUNCATE)
    except subprocess.TimeoutExpired:
        returncode = -1
        stdout = ""
        stderr = _truncate(f"Job timed out after {timeout}s", STDERR_TRUNCATE)
    except Exception as e:
        returncode = -1
        stdout = ""
        stderr = _truncate(str(e), STDERR_TRUNCATE)

    resolved_run_id = request.get("run_id") or _parse_run_id_from_stdout(stdout)
    result_summary: dict[str, Any] = {}
    if resolved_run_id:
        manifest = _load_manifest_if_present(tenant_id, loan_id, resolved_run_id)
        if manifest:
            base = _get_base_path()
            mp = base / "tenants" / tenant_id / "loans" / loan_id / resolved_run_id / "job_manifest.json"
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
        if returncode == 0 and result_summary.get("status") == "SUCCESS":
            JOBS[job_id]["status"] = "SUCCESS"
            JOBS[job_id]["result"] = result_summary
            JOBS[job_id]["error"] = None
        else:
            JOBS[job_id]["status"] = "FAIL"
            err = stderr or stdout or f"Exit code {returncode}"
            JOBS[job_id]["error"] = _truncate(err, ERROR_TRUNCATE)
            if result_summary:
                JOBS[job_id]["result"] = result_summary
            _phase(job_id, "FAIL")


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
    timeout: int | None = Field(None, description="Subprocess timeout in seconds (default 3600)")


class QueryBody(BaseModel):
    question: str
    profile: str = Field("default", description="default | uw_conditions | income_analysis | uw_decision")
    llm_model: str | None = None
    offline_embeddings: bool = True
    top_k: int | None = None
    max_per_file: int | None = None


# ---------------------------------------------------------------------------
# App and routes
# ---------------------------------------------------------------------------
app = FastAPI(title="MortgageDocAI Loan API", description="Local-only loan analysis API")


class _SecurityMiddleware(BaseHTTPMiddleware):
    """Require X-API-Key when MORTGAGEDOCAI_API_KEY is set; 404 when tenant_id not in MORTGAGEDOCAI_ALLOWED_TENANTS."""

    async def dispatch(self, request: Request, call_next):
        if _API_KEY:
            key = request.headers.get("X-API-Key") or ""
            if key != _API_KEY:
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        if _ALLOWED_TENANTS:
            parts = request.url.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "tenants":
                tenant_id = parts[1]
                if tenant_id not in _ALLOWED_TENANTS:
                    return JSONResponse(status_code=404, content={"detail": "Not Found"})
        return await call_next(request)


app.add_middleware(_SecurityMiddleware)


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "MortgageDocAI Loan API",
        "docs": "/docs",
        "health": "/health",
        "tenants": "/tenants/{tenant_id}/loans",
        "jobs": "/tenants/{tenant_id}/loans/{loan_id}/jobs",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
    with JOBS_LOCK:
        for j in JOBS.values():
            if j.get("tenant_id") != tenant_id or j.get("loan_id") != loan_id:
                continue
            if j.get("status") not in ("PENDING", "RUNNING", "SUCCESS"):
                continue
            if j.get("request") == request:
                return {
                    "job_id": j["job_id"],
                    "run_id": j["request"]["run_id"],
                    "status": j["status"],
                    "status_url": f"/jobs/{j['job_id']}",
                }
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "tenant_id": tenant_id,
            "loan_id": loan_id,
            "run_id": run_id,
            "status": "PENDING",
            "created_at_utc": _utc_now_z(),
            "started_at_utc": None,
            "finished_at_utc": None,
            "request": request,
            "result": None,
            "error": None,
            "stdout": None,
            "stderr": None,
        }
    t = threading.Thread(
        target=_run_job_worker,
        args=(job_id, tenant_id, loan_id, request),
        daemon=True,
    )
    t.start()
    return {
        "job_id": job_id,
        "run_id": run_id,
        "status": "PENDING",
        "status_url": f"/jobs/{job_id}",
    }


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
    job_id = str(uuid.uuid4())
    request = body.model_dump()
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "tenant_id": tenant_id,
            "loan_id": loan_id,
            "run_id": body.run_id,
            "status": "PENDING",
            "created_at_utc": _utc_now_z(),
            "started_at_utc": None,
            "finished_at_utc": None,
            "request": request,
            "result": None,
            "error": None,
            "stdout": None,
            "stderr": None,
        }
    t = threading.Thread(
        target=_run_job_worker,
        args=(job_id, tenant_id, loan_id, request),
        daemon=True,
    )
    t.start()
    return {
        "job_id": job_id,
        "status": "PENDING",
        "status_url": f"/jobs/{job_id}",
    }


@app.get("/jobs/{job_id}")
def get_job_status(job_id: str) -> dict[str, Any]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {k: v for k, v in job.items() if v is not None}


@app.get("/jobs")
def list_jobs(limit: int = 50, status: str | None = None) -> dict[str, list[dict[str, Any]]]:
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    if status:
        jobs = [j for j in jobs if j.get("status") == status]
    jobs = sorted(jobs, key=lambda j: j.get("created_at_utc") or "", reverse=True)[:limit]
    return {"jobs": [{k: v for k, v in j.items() if v is not None} for j in jobs]}


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
