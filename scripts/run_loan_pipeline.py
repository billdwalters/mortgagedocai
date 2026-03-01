#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys

from lib import (
    DEFAULT_TENANT,
    ContractError,
    preflight_mount_contract,
    validate_source_path,
    utc_run_id,
)
from step10_intake import main as step10_main
from step11_process import main as step11_main
from step12_analyze import main as step12_main

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MortgageDocAI v1 orchestrator (Steps 10–13)")
    p.add_argument("--tenant-id", default=DEFAULT_TENANT)
    p.add_argument("--loan-id", required=True)
    p.add_argument("--source-path", required=True)
    p.add_argument("--run-id", default=None)

    # Step 11 tuning knobs (accepted by step11_process.py)
    p.add_argument("--qdrant-url", default="http://localhost:6333")
    p.add_argument("--embedding-model", default="intfloat/e5-large-v2")
    p.add_argument("--embedding-dim", type=int, default=1024)
    p.add_argument("--embedding-device", choices=["cpu","cuda"], default=None)
    p.add_argument("--batch-size", type=int, default=64)

    p.add_argument("--chunk-target-chars", type=int, default=4500)
    p.add_argument("--chunk-max-chars", type=int, default=6000)
    p.add_argument("--chunk-overlap-chars", type=int, default=800)
    p.add_argument("--min-chunk-chars", type=int, default=900)
    p.add_argument("--dense-chunk-target-chars", type=int, default=2400)
    p.add_argument("--dense-chunk-max-chars", type=int, default=3400)
    p.add_argument("--dense-chunk-overlap-chars", type=int, default=350)
    p.add_argument("--ocr-threshold-chars", type=int, default=400)

    # Step 12 multiquery args
    p.add_argument("--query", action="append", dest="queries", default=None)
    p.add_argument("--analysis-profile", action="append", dest="profiles", default=None)
    p.add_argument("--retrieval-pack", action="append", dest="retrieval_packs", default=None)

    # Step 12 Ollama adapter flags
    p.add_argument("--ollama-url", default="http://localhost:11434")
    p.add_argument("--llm-model", default="llama3")
    p.add_argument("--llm-temperature", type=float, default=0)
    p.add_argument("--llm-max-tokens", type=int, default=800)
    return p.parse_args()

def main() -> int:
    args = parse_args()
    try:
        preflight_mount_contract()
        validate_source_path(args.source_path)
        run_id = args.run_id or utc_run_id()

        # Step 10
        step10_main([
            "--tenant-id", args.tenant_id,
            "--loan-id", args.loan_id,
            "--source-path", args.source_path,
            "--intake-bucket", "synology_stage",
        ])

        # Step 11
        step11_argv = [
            "--tenant-id", args.tenant_id,
            "--loan-id", args.loan_id,
            "--run-id", run_id,
            "--qdrant-url", args.qdrant_url,
            "--embedding-model", args.embedding_model,
            "--embedding-dim", str(args.embedding_dim),
            "--batch-size", str(args.batch_size),
            "--chunk-target-chars", str(args.chunk_target_chars),
            "--chunk-max-chars", str(args.chunk_max_chars),
            "--chunk-overlap-chars", str(args.chunk_overlap_chars),
            "--min-chunk-chars", str(args.min_chunk_chars),
            "--dense-chunk-target-chars", str(args.dense_chunk_target_chars),
            "--dense-chunk-max-chars", str(args.dense_chunk_max_chars),
            "--dense-chunk-overlap-chars", str(args.dense_chunk_overlap_chars),
            "--ocr-threshold-chars", str(args.ocr_threshold_chars),
        ]
        if args.embedding_device:
            step11_argv += ["--embedding-device", args.embedding_device]
        step11_main(step11_argv)

        # Step 12
        step12_argv = [
            "--tenant-id", args.tenant_id,
            "--loan-id", args.loan_id,
            "--run-id", run_id,
            "--ollama-url", args.ollama_url,
            "--llm-model", args.llm_model,
            "--llm-temperature", str(args.llm_temperature),
            "--llm-max-tokens", str(args.llm_max_tokens),
        ]
        if args.queries:
            for q in args.queries:
                step12_argv += ["--query", q]
        if args.profiles:
            for p in args.profiles:
                step12_argv += ["--analysis-profile", p]
        if args.retrieval_packs:
            for rp in args.retrieval_packs:
                step12_argv += ["--retrieval-pack", rp]
        step12_main(step12_argv)

        print("✓ Pipeline complete")
        return 0
    except ContractError as e:
        print(f"CONTRACT ERROR: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
