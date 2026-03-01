#!/usr/bin/env python3
"""
Step 10 — Intake / staging (canonical v1)
- Uses explicit --source-path under /mnt/source_loans/5-Borrowers TBD
- Copies into nas_ingest/tenants/<tenant>/loans/<loan>/synology_stage/<timestamp>/...
- Writes _meta/intake_manifest.json and _meta/source_system.json
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List

from lib import (
    DEFAULT_TENANT, ContractError,
    SOURCE_MOUNT, NAS_INGEST,
    preflight_mount_contract, validate_source_path,
    utc_timestamp_compact, sha256_file,
    atomic_write_json, ensure_dir,
)

def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Step 10 — Intake / staging (v1)")
    ap.add_argument("--tenant-id", default=DEFAULT_TENANT)
    ap.add_argument("--loan-id", required=True)
    ap.add_argument("--source-path", required=True)
    ap.add_argument("--intake-bucket", default="synology_stage")
    ap.add_argument("--force", action="store_true")
    return ap.parse_args(argv)

def _iter_files(root: Path) -> List[Path]:
    return [p for p in root.rglob("*") if p.is_file()]

def main(argv=None) -> None:
    args = parse_args(argv)
    preflight_mount_contract()
    source_root = validate_source_path(args.source_path)
    if not source_root.is_dir():
        raise ContractError(f"--source-path must be a directory: {source_root}")

    loan_root = NAS_INGEST / "tenants" / args.tenant_id / "loans" / args.loan_id
    meta_dir = loan_root / "_meta"
    ensure_dir(meta_dir)

    ts = utc_timestamp_compact()
    bucket_dir = loan_root / args.intake_bucket / ts
    ensure_dir(bucket_dir)

    staged_files: List[Dict[str, Any]] = []
    for src in sorted(_iter_files(source_root), key=lambda p: str(p).lower()):
        rel = src.relative_to(source_root)
        dst = bucket_dir / rel
        ensure_dir(dst.parent)
        if dst.exists() and not args.force:
            pass
        else:
            dst.write_bytes(src.read_bytes())
        h = sha256_file(dst)
        staged_files.append({
            "document_id": h,
            "original_source_path": str(src.resolve().relative_to(SOURCE_MOUNT.resolve())),
            "stored_relative_path": str(dst.resolve().relative_to(loan_root.resolve())),
            "size_bytes": dst.stat().st_size,
            "sha256": h,
        })

    atomic_write_json(meta_dir / "intake_manifest.json", {
        "tenant_id": args.tenant_id,
        "loan_id": args.loan_id,
        "intake_source": args.intake_bucket,
        "timestamp_utc": ts,
        "files": staged_files,
    })

    ss = meta_dir / "source_system.json"
    if not ss.exists():
        atomic_write_json(ss, {
            "source": "synology",
            "mount": str(SOURCE_MOUNT),
            "notes": "Read-only mount on AI server",
        })

    print(f"✓ Step10 complete: {len(staged_files)} files staged into {bucket_dir}")

if __name__ == "__main__":
    main()
