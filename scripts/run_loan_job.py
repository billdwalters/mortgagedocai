#!/usr/bin/env python3
"""MortgageDocAI production entry point.

Executes the full pipeline for a single loan via subprocess calls
and writes a job_manifest.json summarizing the run.
"""
from __future__ import annotations

import argparse
import datetime
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


def _run(cmd: list, label: str) -> None:
    """Run a subprocess; raise on non-zero exit."""
    print(f"=== {label} ===", flush=True)
    subprocess.run(cmd, check=True)


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


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
            validate_source_path(source_path)
            _run([
                "python3", _step("step10_intake.py"),
                "--tenant-id", tenant_id,
                "--loan-id", loan_id,
                "--source-path", source_path,
            ], "Step10: intake")

        # --- Step 11: Process / Chunk / Embed ---
        if not args.skip_process:
            _run([
                "python3", _step("step11_process.py"),
                "--tenant-id", tenant_id,
                "--loan-id", loan_id,
                "--run-id", run_id,
            ], "Step11: process + embed")

        # --- Step 13: General retrieval pack ---
        _run([
            "python3", _step("step13_build_retrieval_pack.py"),
            "--tenant-id", tenant_id,
            "--loan-id", loan_id,
            "--run-id", run_id,
            "--query", QUERY_RETRIEVE,
            "--out-run-id", run_id,
            "--top-k", "80",
        ], "Step13: general retrieval pack")

        # --- Income analysis ---
        if ran_income:
            # Step 13: income-focused retrieval (overwrites RP)
            _run([
                "python3", _step("step13_build_retrieval_pack.py"),
                "--tenant-id", tenant_id,
                "--loan-id", loan_id,
                "--run-id", run_id,
                "--query", INCOME_RETRIEVE_QUERY,
                "--out-run-id", run_id,
                "--top-k", "120",
                "--max-per-file", "12",
                "--required-keywords", "Total Monthly Payments",
                "--required-keywords", "Profit and Loss",
            ], "Step13: income-focused retrieval pack")

            # Step 12: income_analysis
            _run([
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
                "--debug",
            ], "Step12: income_analysis")

        # --- UW Decision ---
        if ran_uw_decision:
            _run([
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
                "--debug",
            ], "Step12: uw_decision")

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
    }
    if error_msg is not None:
        manifest["error"] = error_msg
        if failed_step:
            manifest["failed_step"] = os.path.basename(failed_step)

    ensure_dir(manifest_dir)
    atomic_write_json(manifest_path, manifest)
    print(f"\n{'✓' if status == 'SUCCESS' else '✗'} job_manifest: {manifest_path}", flush=True)
    print(f"  status={status}  run_id={run_id}  rp_sha256={rp_sha256 or 'null'}", flush=True)

    return 0 if status == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
