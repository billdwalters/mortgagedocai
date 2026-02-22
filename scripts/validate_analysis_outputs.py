#!/usr/bin/env python3
"""
validate_analysis_outputs.py — Local-only QA validator for Step12 outputs.

Asserts:
  1. outputs/answer.json exists and is valid JSON
  2. outputs/answer.json contains retrieval_pack_source
  3. Every chunk_id in outputs/citations.jsonl exists in the retrieval pack's
     retrieved_chunks list (citations ⊆ evidence)
  4. Per-profile outputs exist when multiple profiles are declared

Usage:
  python3 scripts/validate_analysis_outputs.py \
    --analyze-root /mnt/nas_apps/nas_analyze/tenants/peak/loans/LOAN001/<run_id>

  Or point directly at a retrieval pack for cross-check:
  python3 scripts/validate_analysis_outputs.py \
    --analyze-root /mnt/nas_apps/nas_analyze/tenants/peak/loans/LOAN001/<run_id> \
    --retrieval-pack /mnt/nas_apps/nas_analyze/tenants/peak/loans/LOAN001/retrieve/<run_id>/retrieval_pack.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Set


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl_chunk_ids(path: Path) -> list[str]:
    ids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        cid = obj.get("chunk_id", "")
        if cid:
            ids.append(cid)
    return ids


def _retrieval_pack_chunk_ids(pack: dict) -> Set[str]:
    ids: Set[str] = set()
    for ch in pack.get("retrieved_chunks", []):
        cid = ch.get("chunk_id") or ch.get("payload", {}).get("chunk_id") or ""
        if cid:
            ids.add(cid)
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate Step12 analysis outputs")
    ap.add_argument("--analyze-root", required=True, help="Path to nas_analyze/<run_id> directory")
    ap.add_argument("--retrieval-pack", default=None, help="Path to retrieval_pack.json (auto-detected from answer.json if omitted)")
    args = ap.parse_args()

    root = Path(args.analyze_root)
    out_dir = root / "outputs"
    meta_dir = root / "_meta"
    errors: list[str] = []
    warnings: list[str] = []

    # --- 1. answer.json exists and is valid ---
    answer_json_path = out_dir / "answer.json"
    if not answer_json_path.exists():
        errors.append(f"MISSING: {answer_json_path}")
    else:
        try:
            answer = _load_json(answer_json_path)
        except Exception as e:
            errors.append(f"INVALID JSON: {answer_json_path}: {e}")
            answer = None

        if answer:
            # --- 2. retrieval_pack_source present ---
            if "retrieval_pack_source" not in answer:
                errors.append(f"MISSING KEY 'retrieval_pack_source' in {answer_json_path}")
            else:
                rps = answer["retrieval_pack_source"]
                if rps not in ("explicit", "run_id", "latest", "step13", "unset"):
                    warnings.append(f"UNEXPECTED retrieval_pack_source value: {rps!r}")

    # --- 3. citations.jsonl chunk_ids ⊆ retrieval pack ---
    citations_path = out_dir / "citations.jsonl"
    if not citations_path.exists():
        warnings.append(f"MISSING (may be empty run): {citations_path}")
    else:
        cited_ids = _load_jsonl_chunk_ids(citations_path)

        # Resolve retrieval pack path
        rp_path = None
        if args.retrieval_pack:
            rp_path = Path(args.retrieval_pack)
        elif answer and answer.get("retrieval_pack"):
            rp_path = Path(answer["retrieval_pack"])

        if rp_path and rp_path.exists():
            pack = _load_json(rp_path)
            allowed = _retrieval_pack_chunk_ids(pack)
            for cid in cited_ids:
                if cid not in allowed:
                    errors.append(f"HALLUCINATED CITATION: chunk_id={cid} not in retrieval pack")
            if cited_ids and not errors:
                print(f"  ✓ All {len(cited_ids)} citation(s) verified against retrieval pack")
        elif cited_ids:
            warnings.append("Cannot verify citations: retrieval pack not found or not specified")

    # --- 4. analysis_run.json ---
    run_json_path = meta_dir / "analysis_run.json"
    if not run_json_path.exists():
        errors.append(f"MISSING: {run_json_path}")
    else:
        try:
            run_meta = _load_json(run_json_path)
        except Exception as e:
            errors.append(f"INVALID JSON: {run_json_path}: {e}")
            run_meta = None

        if run_meta:
            if "auto_retrieve" not in run_meta:
                warnings.append(f"MISSING KEY 'auto_retrieve' in {run_json_path}")
            for i, prof in enumerate(run_meta.get("profiles", [])):
                if "retrieval_pack_source" not in prof:
                    errors.append(f"MISSING KEY 'retrieval_pack_source' in profiles[{i}] of {run_json_path}")

    # --- 5. Per-profile directories ---
    profiles_dir = out_dir / "profiles"
    if profiles_dir.exists():
        for prof_dir in sorted(profiles_dir.iterdir()):
            if not prof_dir.is_dir():
                continue
            for fname in ("answer.md", "answer.json", "citations.jsonl"):
                if not (prof_dir / fname).exists():
                    errors.append(f"MISSING: {prof_dir / fname}")
            # Verify per-profile answer.json has retrieval_pack_source
            paj = prof_dir / "answer.json"
            if paj.exists():
                try:
                    pdata = _load_json(paj)
                    if "retrieval_pack_source" not in pdata:
                        errors.append(f"MISSING KEY 'retrieval_pack_source' in {paj}")
                except Exception as e:
                    errors.append(f"INVALID JSON: {paj}: {e}")

    # --- Report ---
    print()
    if warnings:
        for w in warnings:
            print(f"  ⚠ {w}")
    if errors:
        for e in errors:
            print(f"  ✗ {e}")
        print(f"\n  FAILED: {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    else:
        print(f"  ✓ PASSED: 0 errors, {len(warnings)} warning(s)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
