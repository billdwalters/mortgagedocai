#!/usr/bin/env python3
"""
MortgageDocAI — minimal LOCAL-ONLY FastAPI service wrapping run_loan_job.py and step12/step13.

Usage:
  Install deps (if needed): pip install fastapi uvicorn
  Run: python3 scripts/loan_api.py --host 127.0.0.1 --port 8000

  To allow access from another machine (e.g. Windows client to server 10.10.10.190):
  On the server: python3 scripts/loan_api.py --host 0.0.0.0 --port 8000
  From client:   http://10.10.10.190:8000 (use the server's IP, not 127.0.0.1)

  If "address already in use" when starting: another instance is on 8000. Find it with
  ss -tlnp | grep 8000  then  kill <PID> . Or use  --port 8001  to use a different port.

  If connection refused from another machine:
  - On server: confirm the process is running and you see "Starting Loan API on 0.0.0.0:8000".
  - On server: curl http://127.0.0.1:8000/health then curl http://10.10.10.190:8000/health; if only the first works, the app is bound to localhost only (restart with --host 0.0.0.0).
  - On server: open port 8000 in firewall if needed, e.g. sudo ufw allow 8000/tcp && sudo ufw status (Linux).
  - From client: Test-NetConnection -ComputerName 10.10.10.190 -Port 8000 (PowerShell) or telnet 10.10.10.190 8000 to see if the port is reachable.

  From repo root with venv:
    cd /opt/mortgagedocai && source venv/bin/activate
    python3 scripts/loan_api.py --host 127.0.0.1 --port 8000

Example curl commands:
  curl -s http://127.0.0.1:8000/health
  curl -s http://127.0.0.1:8000/tenants/peak/loans
  curl -s -X POST http://127.0.0.1:8000/tenants/peak/loans/16271681/runs -H "Content-Type: application/json" -d '{"skip_intake":true,"skip_process":true,"run_id":"2026-02-13T073441Z"}'
  curl -s http://127.0.0.1:8000/tenants/peak/loans/16271681/runs
  curl -s http://127.0.0.1:8000/tenants/peak/loans/16271681/runs/2026-02-13T073441Z
  curl -s -X POST http://127.0.0.1:8000/tenants/peak/loans/16271681/runs/2026-02-13T073441Z/query -H "Content-Type: application/json" -d '{"question":"What are the conditions of approval?","profile":"default"}'

  Jobs (background): POST .../jobs to submit, GET /jobs to list, GET /jobs/{job_id} to poll (replace {job_id} with the UUID returned from POST).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# FastAPI / Pydantic / uvicorn — install with: pip install fastapi uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Ensure sibling scripts (job_runner, lib) are importable when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_runner import _quiet_env, enqueue_job, get_job, list_jobs as list_jobs_impl, load_jobs_from_disk

# Load persisted jobs into registry at startup (survives API restart)
load_jobs_from_disk()

# ---------------------------------------------------------------------------
# Paths (local-only; no cloud)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
NAS_ANALYZE = Path("/mnt/nas_apps/nas_analyze")


# Loan IDs: prefer directory names that look like IDs (digits or alphanumeric)
def _is_loan_dir(name: str) -> bool:
    if not name or name.startswith("."):
        return False
    return name.replace("-", "").replace("_", "").isalnum() or name.isdigit()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="MortgageDocAI Loan API", description="Local-only loan analysis API")


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------
class StartRunBody(BaseModel):
    run_id: Optional[str] = None
    skip_intake: bool = False
    skip_process: bool = False
    source_path: Optional[str] = Field(None, description="Required when skip_intake is False")


class StartRunResponse(BaseModel):
    tenant_id: str
    loan_id: str
    run_id: Optional[str] = None
    status: str = "STARTED"


class QueryBody(BaseModel):
    question: str
    profile: str = Field("default", description="default | uw_conditions | income_analysis | uw_decision")
    llm_model: Optional[str] = Field(None, description="e.g. phi3, mistral, llama3")
    offline_embeddings: bool = True
    top_k: Optional[int] = None
    max_per_file: Optional[int] = None


class SubmitJobBody(BaseModel):
    run_id: Optional[str] = None
    skip_intake: bool = False
    skip_process: bool = False
    source_path: Optional[str] = None
    offline_embeddings: bool = True
    top_k: Optional[int] = 80
    max_per_file: Optional[int] = 12
    max_dropped_chunks: Optional[int] = 5
    expect_rp_hash_stable: Optional[bool] = None
    smoke_debug: bool = False
    llm_model: Optional[str] = None
    run_llm: Optional[int] = None
    timeout: Optional[int] = Field(None, description="Subprocess timeout in seconds (default 3600)")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "service": "MortgageDocAI Loan API",
        "docs": "/docs",
        "health": "/health",
        "tenants": "/tenants/{tenant_id}/loans",
        "jobs": "/tenants/{tenant_id}/loans/{loan_id}/jobs",
    }


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/tenants/{tenant_id}/loans")
def list_loans(tenant_id: str) -> Dict[str, List[str]]:
    loans_dir = NAS_ANALYZE / "tenants" / tenant_id / "loans"
    if not loans_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Tenant loans path not found: {loans_dir}",
        )
    loan_ids = [
        d.name for d in loans_dir.iterdir()
        if d.is_dir() and _is_loan_dir(d.name)
    ]
    return {"loan_ids": sorted(loan_ids)}


@app.post("/tenants/{tenant_id}/loans/{loan_id}/runs", status_code=202)
def start_run(tenant_id: str, loan_id: str, body: StartRunBody) -> StartRunResponse:
    if not body.skip_intake and not body.source_path:
        raise HTTPException(
            status_code=422,
            detail="source_path is required when skip_intake is False",
        )
    if body.skip_process and not body.run_id:
        raise HTTPException(
            status_code=422,
            detail="run_id is required when skip_process is True (run_loan_job.py contract)",
        )
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
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start job: {e}. Paths: cwd={REPO_ROOT}, cmd={cmd}",
        )
    return StartRunResponse(
        tenant_id=tenant_id,
        loan_id=loan_id,
        run_id=body.run_id,
        status="STARTED",
    )


@app.get("/tenants/{tenant_id}/loans/{loan_id}/runs")
def list_runs(tenant_id: str, loan_id: str) -> Dict[str, List[str]]:
    loan_dir = NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id
    if not loan_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Loan path not found: {loan_dir}",
        )
    run_ids = []
    for d in loan_dir.iterdir():
        if not d.is_dir():
            continue
        if (d / "job_manifest.json").exists() or (d / "outputs").is_dir():
            run_ids.append(d.name)
    return {"run_ids": sorted(run_ids)}


@app.get("/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}")
def get_run_status(tenant_id: str, loan_id: str, run_id: str) -> Dict[str, Any]:
    manifest_path = (
        NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id / run_id / "job_manifest.json"
    )
    if not manifest_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"job_manifest.json not found at {manifest_path}. Run may not exist or job not finished.",
        )
    try:
        with manifest_path.open() as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Invalid JSON in {manifest_path}: {e}",
        )


# ---------------------------------------------------------------------------
# Background jobs (submit + status polling) — delegate to job_runner
# ---------------------------------------------------------------------------
@app.post("/tenants/{tenant_id}/loans/{loan_id}/jobs", status_code=202)
def submit_job(tenant_id: str, loan_id: str, body: SubmitJobBody) -> Dict[str, Any]:
    if not body.skip_intake and not body.source_path:
        raise HTTPException(
            status_code=422,
            detail="source_path is required when skip_intake is False",
        )
    if body.skip_process and not body.run_id:
        raise HTTPException(
            status_code=422,
            detail="run_id is required when skip_process is True",
        )
    return enqueue_job(tenant_id, loan_id, body.model_dump())


@app.get("/jobs/{job_id}")
def get_job_status(job_id: str) -> Dict[str, Any]:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/jobs")
def list_jobs(
    limit: int = 50,
    status: Optional[str] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    return list_jobs_impl(limit=limit, status=status)


@app.post("/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/query")
def query_run(
    tenant_id: str,
    loan_id: str,
    run_id: str,
    body: QueryBody,
) -> Dict[str, Any]:
    valid_profiles = ("default", "uw_conditions", "income_analysis", "uw_decision")
    if body.profile not in valid_profiles:
        raise HTTPException(
            status_code=422,
            detail=f"profile must be one of {valid_profiles}",
        )

    env = _quiet_env()
    step13 = str(SCRIPTS_DIR / "step13_build_retrieval_pack.py")
    step12 = str(SCRIPTS_DIR / "step12_analyze.py")

    # Step13: build retrieval pack for this run_id and question
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

    result13 = subprocess.run(
        step13_cmd,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if result13.returncode != 0:
        stderr_tail = (result13.stderr or result13.stdout or "")[-2000:]
        raise HTTPException(
            status_code=500,
            detail=f"Step13 failed (exit {result13.returncode}). stderr tail: {stderr_tail}",
        )

    # Step12: analyze with profile
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

    result12 = subprocess.run(
        step12_cmd,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if result12.returncode != 0:
        stderr_tail = (result12.stderr or result12.stdout or "")[-2000:]
        raise HTTPException(
            status_code=500,
            detail=f"Step12 failed (exit {result12.returncode}). stderr tail: {stderr_tail}",
        )

    answer_path = (
        NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id / run_id
        / "outputs" / "profiles" / body.profile / "answer.json"
    )
    if not answer_path.exists():
        stderr_tail = (result12.stderr or result12.stdout or "")[-2000:]
        raise HTTPException(
            status_code=500,
            detail=f"Profile output missing: {answer_path}. Step12 stderr tail: {stderr_tail}",
        )
    try:
        with answer_path.open() as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Invalid JSON in {answer_path}: {e}",
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="MortgageDocAI Loan API (local-only)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1). Use 0.0.0.0 to accept connections from other machines.")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default 8000)")
    args = parser.parse_args()
    print(f"Starting Loan API on {args.host}:{args.port} (0.0.0.0 = listen on all interfaces)", flush=True)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
