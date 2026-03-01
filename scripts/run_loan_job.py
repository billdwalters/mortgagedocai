#!/usr/bin/env python3
"""MortgageDocAI production entry point.

Executes the full pipeline for a single loan via subprocess calls
and writes a job_manifest.json summarizing the run.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from lib import (
    DEFAULT_TENANT,
    NAS_ANALYZE,
    atomic_write_json,
    ensure_dir,
    preflight_mount_contract,
    sha256_file,
    utc_run_id,
    validate_source_path,
)

# ---------------------------------------------------------------------------
# Constants — query defaults (match smoke harness)
# ---------------------------------------------------------------------------
QUERY_RETRIEVE = (
    "conditions of approval underwriting conditions prior to closing "
    "PTC suspense approval conditions"
)

INCOME_RETRIEVE_QUERY = (
    "Estimated Total Monthly Payment PITIA Proposed housing payment "
    "Principal & Interest Escrow Amount can increase over time "
    "Loan Estimate Closing Disclosure HOA dues Property Taxes "
    "Homeowners Insurance credit report liabilities monthly payment "
    "Total Monthly Payments monthly debt obligations "
    "Uniform Residential Loan Application assets and liabilities "
    "Gross Monthly Income Base Employment Income qualifying income "
    "Total Monthly Income Desktop Underwriter DU Findings "
    "Profit and Loss Net Income Total Income"
)

INCOME_QUERY = (
    "Extract all income sources, liabilities, and proposed housing "
    "payment (PITIA) for DTI calculation."
)

UW_DECISION_QUERY = (
    "Deterministic underwriting decision based on DTI thresholds."
)

SCRIPT_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _step(name: str) -> str:
    """Return absolute path to a sibling step script."""
    return str(SCRIPT_DIR / name)


def _run(cmd: list, label: str, env: Optional[Dict[str, str]] = None) -> None:
    """Run a subprocess; raise on non-zero exit. If env is set, merge with os.environ."""
    print(f"=== {label} ===", flush=True)
    run_env = {**os.environ, **env} if env else None
    subprocess.run(cmd, check=True, env=run_env)


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _phase(name: str) -> None:
    """Emit one phase marker line to stdout for desktop progress (format: PHASE:<NAME> <UTC_ISO_Z>)."""
    print(f"PHASE:{name} {_utc_now_iso()}", flush=True)


# ---------------------------------------------------------------------------
# Derived paths
# ---------------------------------------------------------------------------
def _output_paths(
    tenant_id: str,
    loan_id: str,
    run_id: str,
    ran_income: bool,
    ran_uw_decision: bool,
) -> Dict[str, Optional[str]]:
    """Build dict of absolute output file paths (null if step was skipped)."""
    base = NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id
    rp = base / "retrieve" / run_id / "retrieval_pack.json"
    profiles = base / run_id / "outputs" / "profiles"
    return {
        "decision_json": (
            str(profiles / "uw_decision" / "decision.json")
            if ran_uw_decision else None
        ),
        "dti_json": (
            str(profiles / "income_analysis" / "dti.json")
            if ran_income else None
        ),
        "retrieval_pack_json": str(rp),
        "version_json": (
            str(profiles / "uw_decision" / "version.json")
            if ran_uw_decision else None
        ),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="MortgageDocAI production job runner (Steps 10-13 + manifest)",
    )
    p.add_argument("--tenant-id", default=DEFAULT_TENANT)
    p.add_argument("--loan-id", required=True)
    p.add_argument("--source-path", default=None,
                   help="Source document directory (required unless --skip-intake)")
    p.add_argument(
        "--run-income-analysis",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run income_analysis profile (default: true)",
    )
    p.add_argument(
        "--run-uw-decision",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run uw_decision profile (default: true; requires income_analysis)",
    )
    p.add_argument("--skip-intake", action="store_true",
                   help="Skip Step10 intake (loan must already be ingested)")
    p.add_argument("--skip-process", action="store_true",
                   help="Skip Step11 process (loan must already be chunked/embedded)")
    p.add_argument("--ollama-url", default="http://localhost:11434")
    p.add_argument("--run-id", default=None,
                   help="Override run_id (default: auto-generate; required with --skip-process)")
    p.add_argument("--run-llm", dest="run_llm", action="store_true", default=True,
                   help="Run LLM profiles (default: true)")
    p.add_argument("--no-run-llm", dest="run_llm", action="store_false",
                   help="Deterministic-only: do not call Ollama")
    p.add_argument("--expect-rp-hash-stable", action="store_true", default=False,
                   help="Rerun general Step13 and fail if retrieval_pack hash differs")
    p.add_argument("--max-dropped-chunks", type=int, default=None,
                   help="Fail if income-focused Step13 reports dropped_chunk_ids_count > this")
    p.add_argument("--debug", action="store_true", default=False,
                   help="Pass debug to steps and emit extra logs (smoke_debug)")
    p.add_argument("--offline-embeddings", action="store_true", default=False,
                   help="Pass --offline-embeddings to Step13 (use only cached model files)")
    p.add_argument("--top-k", type=int, default=None,
                   help="Override Step13 top-k for both general and income runs (default: 80 / 120)")
    p.add_argument("--max-per-file", type=int, default=None,
                   help="Override Step13 max-per-file for income run (default: 12)")
    args = p.parse_args(argv)
    if not args.skip_intake and not args.source_path:
        p.error("--source-path is required unless --skip-intake is set")
    if args.skip_process and not args.run_id:
        p.error("--run-id is required when --skip-process is set")
    return args


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    args = parse_args(argv)
    tenant_id = args.tenant_id
    loan_id = args.loan_id
    source_path = args.source_path
    ollama_url = args.ollama_url
    run_id = args.run_id or utc_run_id()
    ran_income = args.run_income_analysis
    ran_uw_decision = args.run_uw_decision

    # uw_decision requires income_analysis
    if ran_uw_decision and not ran_income:
        print("ERROR: --run-uw-decision requires --run-income-analysis", file=sys.stderr)
        return 2

    # Manifest path (may not exist yet — ensure_dir later)
    manifest_dir = (
        NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id / run_id
    )
    manifest_path = manifest_dir / "job_manifest.json"

    # Short-circuit: if run_id was supplied and manifest already SUCCESS, skip run
    if args.run_id and manifest_path.exists():
        try:
            with manifest_path.open() as f:
                existing = json.load(f)
            if existing.get("status") == "SUCCESS":
                print(f"run_id = {run_id} (short-circuit: manifest already SUCCESS)", flush=True)
                _phase("DONE")
                return 0
        except (json.JSONDecodeError, OSError):
            pass

    rp_path = (
        NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id
        / "retrieve" / run_id / "retrieval_pack.json"
    )

    error_msg: Optional[str] = None
    failed_step: Optional[str] = None

    try:
        preflight_mount_contract()

        print(f"run_id = {run_id}", flush=True)

        # --- Step 10: Intake ---
        if not args.skip_intake:
            _phase("INTAKE")
            validate_source_path(source_path)
            _run([
                "python3", _step("step10_intake.py"),
                "--tenant-id", tenant_id,
                "--loan-id", loan_id,
                "--source-path", source_path,
            ], "Step10: intake")

        # --- Step 11: Process / Chunk / Embed ---
        if not args.skip_process:
            _phase("PROCESS")
            _run([
                "python3", _step("step11_process.py"),
                "--tenant-id", tenant_id,
                "--loan-id", loan_id,
                "--run-id", run_id,
            ], "Step11: process + embed")

        # --- Step 13: General retrieval pack ---
        _phase("STEP13_GENERAL")
        top_k_general = args.top_k if args.top_k is not None else 80
        step13_general_cmd = [
            "python3", _step("step13_build_retrieval_pack.py"),
            "--tenant-id", tenant_id,
            "--loan-id", loan_id,
            "--run-id", run_id,
            "--query", QUERY_RETRIEVE,
            "--out-run-id", run_id,
            "--top-k", str(top_k_general),
        ]
        if args.debug:
            step13_general_cmd.append("--debug")
        if args.offline_embeddings:
            step13_general_cmd.append("--offline-embeddings")
        _run(step13_general_cmd, "Step13: general retrieval pack")
        if args.expect_rp_hash_stable:
            if not rp_path.exists():
                raise RuntimeError("expect_rp_hash_stable: retrieval_pack.json missing after first Step13")
            hash1 = sha256_file(rp_path)
            _run(step13_general_cmd, "Step13: general retrieval pack (rerun for hash stability)")
            if not rp_path.exists():
                raise RuntimeError("expect_rp_hash_stable: retrieval_pack.json missing after rerun")
            hash2 = sha256_file(rp_path)
            if hash1 != hash2:
                raise RuntimeError(
                    f"expect_rp_hash_stable: retrieval_pack hash changed (first={hash1!r}, second={hash2!r})"
                )
            if args.debug:
                print(f"[debug] expect_rp_hash_stable: hashes match {hash1!r}", flush=True)

        step12_env = None if args.run_llm else {"RUN_LLM": "0"}
        # --- Income analysis ---
        if ran_income:
            _phase("STEP13_INCOME")
            # Step 13: income-focused retrieval (overwrites RP)
            top_k_income = args.top_k if args.top_k is not None else 120
            max_per_file = args.max_per_file if args.max_per_file is not None else 12
            step13_income_cmd = [
                "python3", _step("step13_build_retrieval_pack.py"),
                "--tenant-id", tenant_id,
                "--loan-id", loan_id,
                "--run-id", run_id,
                "--query", INCOME_RETRIEVE_QUERY,
                "--out-run-id", run_id,
                "--top-k", str(top_k_income),
                "--max-per-file", str(max_per_file),
                "--required-keywords", "Total Monthly Payments",
                "--required-keywords", "Profit and Loss",
            ]
            if args.debug:
                step13_income_cmd.append("--debug")
            if args.offline_embeddings:
                step13_income_cmd.append("--offline-embeddings")
            _run(step13_income_cmd, "Step13: income-focused retrieval pack")
            if args.max_dropped_chunks is not None and rp_path.exists():
                with rp_path.open() as f:
                    rp_data = json.load(f)
                meta = rp_data.get("retrieval_pack_meta") or {}
                dropped_count = meta.get("dropped_chunk_ids_count", 0)
                if dropped_count > args.max_dropped_chunks:
                    raise RuntimeError(
                        f"max_dropped_chunks={args.max_dropped_chunks} but income Step13 reported "
                        f"dropped_chunk_ids_count={dropped_count}"
                    )
                if args.debug:
                    print(f"[debug] max_dropped_chunks check: dropped_chunk_ids_count={dropped_count}", flush=True)

            _phase("STEP12_INCOME_ANALYSIS")
            # Step 12: income_analysis (env RUN_LLM=0 when --no-run-llm)
            step12_income_cmd = [
                "python3", _step("step12_analyze.py"),
                "--tenant-id", tenant_id,
                "--loan-id", loan_id,
                "--run-id", run_id,
                "--query", INCOME_QUERY,
                "--analysis-profile", "income_analysis",
                "--ollama-url", ollama_url,
                "--llm-model", "mistral",
                "--llm-temperature", "0",
                "--llm-max-tokens", "650",
                "--evidence-max-chars", "4500",
                "--ollama-timeout", "900",
                "--save-llm-raw",
            ]
            if args.debug:
                step12_income_cmd.append("--debug")
            _run(step12_income_cmd, "Step12: income_analysis", env=step12_env)

        # --- UW Decision ---
        if ran_uw_decision:
            _phase("STEP12_UW_DECISION")
            step12_uw_cmd = [
                "python3", _step("step12_analyze.py"),
                "--tenant-id", tenant_id,
                "--loan-id", loan_id,
                "--run-id", run_id,
                "--query", UW_DECISION_QUERY,
                "--analysis-profile", "uw_decision",
                "--ollama-url", ollama_url,
                "--llm-model", "mistral",
                "--llm-temperature", "0",
                "--llm-max-tokens", "1",
                "--ollama-timeout", "10",
                "--no-auto-retrieve",
            ]
            if args.debug:
                step12_uw_cmd.append("--debug")
            _run(step12_uw_cmd, "Step12: uw_decision", env=step12_env)

    except subprocess.CalledProcessError as exc:
        error_msg = f"{exc.cmd[1] if len(exc.cmd) > 1 else exc.cmd[0]} exited {exc.returncode}"
        failed_step = exc.cmd[1] if len(exc.cmd) > 1 else str(exc.cmd)
    except Exception as exc:
        error_msg = str(exc)

    # --- Compute RP hash ---
    rp_sha256: Optional[str] = None
    if rp_path.exists():
        try:
            rp_sha256 = sha256_file(rp_path)
        except OSError:
            pass

    # --- Write manifest ---
    status = "SUCCESS" if error_msg is None else "FAIL"
    outputs = _output_paths(tenant_id, loan_id, run_id, ran_income, ran_uw_decision)

    manifest: Dict[str, Any] = {
        "generated_at_utc": _utc_now_iso(),
        "loan_id": loan_id,
        "outputs": outputs,
        "retrieval_pack_sha256": rp_sha256,
        "run_id": run_id,
        "status": status,
        "tenant_id": tenant_id,
        "options": {
            "run_llm": args.run_llm,
            "expect_rp_hash_stable": args.expect_rp_hash_stable,
            "max_dropped_chunks": args.max_dropped_chunks,
            "smoke_debug": args.debug,
            "offline_embeddings": args.offline_embeddings,
            "top_k": args.top_k,
            "max_per_file": args.max_per_file,
        },
    }
    if error_msg is not None:
        manifest["error"] = error_msg
        if failed_step:
            manifest["failed_step"] = os.path.basename(failed_step)

    ensure_dir(manifest_dir)
    atomic_write_json(manifest_path, manifest)
    print(f"\n{'✓' if status == 'SUCCESS' else '✗'} job_manifest: {manifest_path}", flush=True)
    print(f"  status={status}  run_id={run_id}  rp_sha256={rp_sha256 or 'null'}", flush=True)

    if status == "SUCCESS":
        _phase("DONE")
    else:
        _phase("FAIL")
    return 0 if status == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
