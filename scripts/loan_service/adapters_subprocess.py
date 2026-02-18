"""Subprocess runner for run_loan_job.py (same args, env, timeout, truncation)."""
from __future__ import annotations

import os
import subprocess
import sys
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
        if req.get("run_llm") is not None:
            cmd += ["--run-llm", str(req["run_llm"])]
        if req.get("max_dropped_chunks") is not None:
            cmd += ["--max-dropped-chunks", str(req["max_dropped_chunks"])]
        if req.get("expect_rp_hash_stable") is not None:
            cmd += ["--expect-rp-hash-stable", "1" if req["expect_rp_hash_stable"] else "0"]
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
