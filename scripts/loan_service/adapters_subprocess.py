"""Subprocess runner for run_loan_job.py (same args, env, timeout, truncation)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .adapters_disk import STDOUT_TRUNCATE, STDERR_TRUNCATE, _parse_run_id_from_stdout, _truncate

JOB_TIMEOUT_DEFAULT = 3600
_SCRIPT_DIR = Path(__file__).resolve().parent.parent  # scripts/
REPO_ROOT = _SCRIPT_DIR.parent  # repo root
SCRIPTS_DIR = _SCRIPT_DIR


def _quiet_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    env["TRANSFORMERS_VERBOSITY"] = "error"
    env["TQDM_MININTERVAL"] = "999999"
    env["PYTHONPATH"] = str(SCRIPTS_DIR)
    return env


def get_job_env(request: dict[str, Any]) -> dict[str, str]:
    """Build env for run_loan_job subprocess (quiet env + request-driven vars)."""
    env = _quiet_env()
    env["SMOKE_DEBUG"] = "1" if request.get("smoke_debug") else "0"
    if "expect_rp_hash_stable" in request:
        env["EXPECT_RP_HASH_STABLE"] = "1" if request["expect_rp_hash_stable"] else "0"
    if request.get("max_dropped_chunks") is not None:
        env["MAX_DROPPED_CHUNKS"] = str(request["max_dropped_chunks"])
    if request.get("run_llm") is not None:
        env["RUN_LLM"] = str(request["run_llm"])
    return env


def _run_query_job(
    req: dict[str, Any],
    tenant_id: str,
    loan_id: str,
    env: dict[str, str],
    timeout: int,
) -> tuple[int, str, str]:
    """Run Step13 then Step12 for a background query job; return (step12 returncode, combined stdout, combined stderr)."""
    step13 = str(SCRIPTS_DIR / "step13_build_retrieval_pack.py")
    step12 = str(SCRIPTS_DIR / "step12_analyze.py")
    question = req.get("question", "")
    profile = req.get("profile", "default")
    top_k = req.get("top_k") or 80
    max_per_file = req.get("max_per_file") or 12
    run_id = req.get("run_id") or ""
    step13_cmd = [
        sys.executable, step13,
        "--tenant-id", tenant_id,
        "--loan-id", loan_id,
        "--run-id", run_id,
        "--query", question,
        "--out-run-id", run_id,
        "--top-k", str(top_k),
        "--max-per-file", str(max_per_file),
    ]
    if req.get("offline_embeddings", True):
        step13_cmd.append("--offline-embeddings")
    if req.get("smoke_debug"):
        step13_cmd.append("--debug")
    step12_cmd = [
        sys.executable, step12,
        "--tenant-id", tenant_id,
        "--loan-id", loan_id,
        "--run-id", run_id,
        "--query", question,
        "--analysis-profile", profile,
        "--no-auto-retrieve",
    ]
    if req.get("llm_model") and profile != "uw_decision":
        step12_cmd += ["--llm-model", str(req["llm_model"])]
    if req.get("smoke_debug"):
        step12_cmd.append("--debug")
    out_parts: list[str] = []
    err_parts: list[str] = []
    deadline = time.monotonic() + timeout
    rem = max(1, int(deadline - time.monotonic()))
    r13 = subprocess.run(step13_cmd, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=rem, check=False)
    out_parts.append(r13.stdout or "")
    err_parts.append(r13.stderr or "")
    if r13.returncode != 0:
        return (
            r13.returncode,
            _truncate("\n".join(out_parts), STDOUT_TRUNCATE),
            _truncate("\n".join(err_parts), STDERR_TRUNCATE),
        )
    rem = max(1, int(deadline - time.monotonic()))
    r12 = subprocess.run(step12_cmd, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=rem, check=False)
    out_parts.append(r12.stdout or "")
    err_parts.append(r12.stderr or "")
    return (
        r12.returncode,
        _truncate("\n".join(out_parts), STDOUT_TRUNCATE),
        _truncate("\n".join(err_parts), STDERR_TRUNCATE),
    )


class SubprocessRunner:
    """Runs scripts/run_loan_job.py with same args and env as job_runner."""

    def run(
        self,
        req: dict[str, Any],
        tenant_id: str,
        loan_id: str,
        env: dict[str, str],
        timeout: int,
    ) -> tuple[int, str, str]:
        if "question" in req and "profile" in req:
            return _run_query_job(req, tenant_id, loan_id, env, timeout)
        run_id = req.get("run_id")
        cmd = [
            sys.executable,
            str(SCRIPTS_DIR / "run_loan_job.py"),
            "--tenant-id", tenant_id,
            "--loan-id", loan_id,
        ]
        if run_id:
            cmd += ["--run-id", run_id]
        if req.get("skip_intake"):
            cmd += ["--skip-intake"]
        if req.get("skip_process"):
            cmd += ["--skip-process"]
        if req.get("source_path"):
            cmd += ["--source-path", req["source_path"]]
        if req.get("smoke_debug"):
            cmd += ["--debug"]
        run_llm_val = req.get("run_llm")
        if run_llm_val is not None:
            if run_llm_val in (True, 1, "1", "true", "True"):
                cmd += ["--run-llm"]
            else:
                cmd += ["--no-run-llm"]
        exp_val = req.get("expect_rp_hash_stable")
        if exp_val in (True, 1, "1", "true", "True"):
            cmd += ["--expect-rp-hash-stable"]
        if req.get("max_dropped_chunks") is not None:
            cmd += ["--max-dropped-chunks", str(int(req["max_dropped_chunks"]))]
        if req.get("offline_embeddings"):
            cmd += ["--offline-embeddings"]
        if req.get("top_k") is not None:
            cmd += ["--top-k", str(int(req["top_k"]))]
        if req.get("max_per_file") is not None:
            cmd += ["--max-per-file", str(int(req["max_per_file"]))]
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
        return result.returncode, stdout, stderr
