"""FastAPI router: same paths and response shapes as loan_api.py."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .adapters_subprocess import _quiet_env

# Paths (injected by caller)
def _is_loan_dir(name: str) -> bool:
    if not name or name.startswith("."):
        return False
    return name.replace("-", "").replace("_", "").isalnum() or name.isdigit()


class StartRunBody(BaseModel):
    run_id: Optional[str] = None
    skip_intake: bool = False
    skip_process: bool = False
    source_path: Optional[str] = Field(None, description="Required when skip_intake is False")


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


def create_router(
    job_service: Any,
    nas_analyze: Path,
    scripts_dir: Path,
    repo_root: Path,
) -> APIRouter:
    router = APIRouter()

    @router.get("/")
    def root() -> Dict[str, Any]:
        return {
            "service": "MortgageDocAI Loan API",
            "docs": "/docs",
            "health": "/health",
            "tenants": "/tenants/{tenant_id}/loans",
            "jobs": "/tenants/{tenant_id}/loans/{loan_id}/jobs",
        }

    @router.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @router.get("/tenants/{tenant_id}/loans")
    def list_loans(tenant_id: str) -> Dict[str, List[str]]:
        loans_dir = nas_analyze / "tenants" / tenant_id / "loans"
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

    @router.post("/tenants/{tenant_id}/loans/{loan_id}/runs", status_code=202)
    def start_run(tenant_id: str, loan_id: str, body: StartRunBody) -> Dict[str, Any]:
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
            str(scripts_dir / "run_loan_job.py"),
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
                cwd=str(repo_root),
                env=_quiet_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to start job: {e}. Paths: cwd={repo_root}, cmd={cmd}",
            )
        return {
            "tenant_id": tenant_id,
            "loan_id": loan_id,
            "run_id": body.run_id,
            "status": "STARTED",
        }

    @router.get("/tenants/{tenant_id}/loans/{loan_id}/runs")
    def list_runs(tenant_id: str, loan_id: str) -> Dict[str, List[str]]:
        loan_dir = nas_analyze / "tenants" / tenant_id / "loans" / loan_id
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

    @router.get("/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}")
    def get_run_status(tenant_id: str, loan_id: str, run_id: str) -> Dict[str, Any]:
        manifest_path = (
            nas_analyze / "tenants" / tenant_id / "loans" / loan_id / run_id / "job_manifest.json"
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

    @router.post("/tenants/{tenant_id}/loans/{loan_id}/jobs", status_code=202)
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
        return job_service.enqueue_job(tenant_id, loan_id, body.model_dump())

    @router.get("/jobs/{job_id}")
    def get_job_status(job_id: str) -> Dict[str, Any]:
        job = job_service.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @router.get("/jobs")
    def list_jobs(
        limit: int = 50,
        status: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        return job_service.list_jobs(limit=limit, status=status)

    @router.post("/tenants/{tenant_id}/loans/{loan_id}/runs/{run_id}/query")
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
        step13 = str(scripts_dir / "step13_build_retrieval_pack.py")
        step12 = str(scripts_dir / "step12_analyze.py")
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
            cwd=str(repo_root),
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
            cwd=str(repo_root),
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
            nas_analyze / "tenants" / tenant_id / "loans" / loan_id / run_id
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

    return router
