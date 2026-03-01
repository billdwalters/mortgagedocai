"""Subprocess runner for run_loan_job.py (same args, env, timeout, truncation)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .adapters_disk import STDOUT_TRUNCATE, STDERR_TRUNCATE, _parse_run_id_from_stdout, _truncate

JOB_TIMEOUT_DEFAULT = 3600
_SCRIPT_DIR = Path(__file__).resolve().parent.parent  # scripts/
REPO_ROOT = _SCRIPT_DIR.parent  # repo root
SCRIPTS_DIR = _SCRIPT_DIR

_SYSTEMD_RUN: str | None = shutil.which("systemd-run")
_TEMP_DIR = Path("/tmp")


def _job_unit_name(job_id: str) -> str:
    return f"mortgagedocai-job-{job_id}"


def _job_temp_stdout(job_id: str) -> Path:
    return _TEMP_DIR / f"mortgagedocai-{job_id}.stdout"


def _job_temp_stderr(job_id: str) -> Path:
    return _TEMP_DIR / f"mortgagedocai-{job_id}.stderr"


def _job_temp_rc(job_id: str) -> Path:
    return _TEMP_DIR / f"mortgagedocai-{job_id}.rc"


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
    """Runs scripts/run_loan_job.py; streams stdout so callers see PHASE lines in real time."""

    def run(
        self,
        req: dict[str, Any],
        tenant_id: str,
        loan_id: str,
        env: dict[str, str],
        timeout: int,
        on_stdout_line: Any = None,  # Optional[Callable[[str], None]]
        job_id: str | None = None,
    ) -> tuple[int, str, str]:
        if "question" in req and "profile" in req:
            # Query jobs are fast; streaming not needed.
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

        # Use systemd-run when available; fallback to Popen for dev/test environments.
        if job_id and _SYSTEMD_RUN:
            return self._run_with_systemd(cmd, job_id, env, timeout, on_stdout_line)

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            def _read_stdout() -> None:
                for line in proc.stdout or []:
                    line = line if line.endswith("\n") else line + "\n"
                    stdout_parts.append(line)
                    if on_stdout_line is not None:
                        try:
                            on_stdout_line(line)
                        except Exception:
                            pass

            def _read_stderr() -> None:
                for line in proc.stderr or []:
                    stderr_parts.append(line)

            t_out = threading.Thread(target=_read_stdout, daemon=True)
            t_err = threading.Thread(target=_read_stderr, daemon=True)
            t_out.start()
            t_err.start()
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                returncode = -1
                stderr_parts.append(f"Job timed out after {timeout}s\n")
            t_out.join(timeout=2.0)
            t_err.join(timeout=2.0)
        except Exception as e:
            returncode = -1
            stderr_parts.append(str(e) + "\n")

        stdout = _truncate("".join(stdout_parts), STDOUT_TRUNCATE)
        stderr = _truncate("".join(stderr_parts), STDERR_TRUNCATE)
        return returncode, stdout, stderr

    def _run_with_systemd(
        self,
        cmd: list[str],
        job_id: str,
        env: dict[str, str],
        timeout: int,
        on_stdout_line: Any,
    ) -> tuple[int, str, str]:
        """Run cmd in an isolated systemd scope so it survives API restart."""
        import shlex

        stdout_file = _job_temp_stdout(job_id)
        stderr_file = _job_temp_stderr(job_id)
        rc_file = _job_temp_rc(job_id)
        unit_name = _job_unit_name(job_id)

        # Remove stale temp files from any prior crash of this job_id (UUID; shouldn't exist).
        for p in [stdout_file, stderr_file, rc_file]:
            try:
                p.unlink()
            except FileNotFoundError:
                pass

        # Build shell command: redirect pipeline output to temp files; capture exit code.
        python_cmd = " ".join(shlex.quote(c) for c in cmd)
        shell_script = (
            f"{python_cmd} "
            f">{shlex.quote(str(stdout_file))} "
            f"2>{shlex.quote(str(stderr_file))}; "
            f"echo $? >{shlex.quote(str(rc_file))}"
        )
        systemd_cmd: list[str] = [
            _SYSTEMD_RUN, "--scope", "--wait",
            f"--unit={unit_name}",
            "--property=KillMode=process",
            "--property=TimeoutStopSec=20",
            "--",
            "/bin/sh", "-c", shell_script,
        ]

        # Tail the stdout temp file in a thread so PHASE lines reach the in-memory job
        # record (WebUI stepper) in near-real-time.
        stdout_parts: list[str] = []
        stop_tail = threading.Event()

        def _tail() -> None:
            try:
                deadline = time.monotonic() + 15.0
                while not stdout_file.exists() and time.monotonic() < deadline:
                    if stop_tail.is_set():
                        return
                    time.sleep(0.1)
                if not stdout_file.exists():
                    return
                with stdout_file.open("r") as f:
                    while not stop_tail.is_set():
                        line = f.readline()
                        if line:
                            normed = line if line.endswith("\n") else line + "\n"
                            stdout_parts.append(normed)
                            if on_stdout_line is not None:
                                try:
                                    on_stdout_line(normed)
                                except Exception:
                                    pass
                        else:
                            time.sleep(0.05)
            except Exception:
                pass

        t_tail = threading.Thread(target=_tail, daemon=True)
        t_tail.start()
        returncode = -1
        try:
            proc = subprocess.Popen(
                systemd_cmd,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                returncode = -1
        except Exception as e:
            stop_tail.set()
            t_tail.join(timeout=2.0)
            return -1, "", _truncate(str(e), STDERR_TRUNCATE)
        finally:
            stop_tail.set()
            t_tail.join(timeout=2.0)

        # Read authoritative output from temp files (more complete than the tailed parts).
        try:
            final_stdout = _truncate(stdout_file.read_text(), STDOUT_TRUNCATE)
        except OSError:
            final_stdout = _truncate("".join(stdout_parts), STDOUT_TRUNCATE)
        try:
            final_stderr = _truncate(stderr_file.read_text(), STDERR_TRUNCATE)
        except OSError:
            final_stderr = ""

        # The rc file gives the actual pipeline exit code (systemd-run's code is
        # not always the subprocess's code when the API was killed mid-run).
        try:
            if rc_file.exists():
                returncode = int(rc_file.read_text().strip())
        except (ValueError, OSError):
            pass

        # Clean up temp files (watcher handles cleanup if API was killed before here).
        for p in [stdout_file, stderr_file, rc_file]:
            try:
                p.unlink()
            except OSError:
                pass

        return returncode, final_stdout, final_stderr
