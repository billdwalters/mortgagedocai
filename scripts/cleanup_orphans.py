#!/usr/bin/env python3
"""
MortgageDocAI — Orphaned Loan Data Cleanup

Identifies loans whose source folders have been removed (e.g. closed loans)
but whose processed data still lingers in NAS mounts and Qdrant.

Dry-run by default. Pass --confirm --yes to actually delete.

Usage:
  python scripts/cleanup_orphans.py --tenant-id peak               # dry-run
  python scripts/cleanup_orphans.py --tenant-id peak --confirm --yes  # delete
  python scripts/cleanup_orphans.py --tenant-id peak --skip-qdrant  # skip Qdrant
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_scripts_dir = Path(__file__).resolve().parent
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from lib import (
    DEFAULT_QDRANT_URL,
    DEFAULT_TENANT,
    NAS_ANALYZE,
    NAS_CHUNK,
    NAS_INGEST,
    SOURCE_MOUNT,
    SOURCE_SUBDIR,
    qdrant_collection_name,
)

# ---------------------------------------------------------------------------
# Source loan folder detection (inlined from loan_api.py to avoid FastAPI dep)
# ---------------------------------------------------------------------------
_LOAN_FOLDER_RE = re.compile(r"\[Loan\s+(\d+)\]")
_LOAN_ID_FALLBACK_RE = re.compile(r"\d{6,}")

_SOURCE_CATEGORIES_RAW = os.environ.get("MORTGAGEDOCAI_SOURCE_LOANS_CATEGORIES", SOURCE_SUBDIR).strip()
SOURCE_LOANS_CATEGORIES = [s.strip() for s in _SOURCE_CATEGORIES_RAW.split(",") if s.strip()]


def _is_source_loan_folder(name: str) -> bool:
    if not name or name.startswith("."):
        return False
    if "[Loan" in name and "]" in name:
        return True
    return "Loan" in name and any(c.isdigit() for c in name)


def _extract_loan_id_from_folder_name(name: str) -> str | None:
    m = _LOAN_FOLDER_RE.search(name)
    if m:
        return m.group(1)
    m = _LOAN_ID_FALLBACK_RE.search(name)
    if m:
        return m.group(0)
    return None


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class OrphanedLoan:
    loan_id: str
    locations: dict[str, bool] = field(default_factory=dict)  # {nas_name: present}
    has_active_job: bool = False


# ---------------------------------------------------------------------------
# Core detection (testable, no side effects)
# ---------------------------------------------------------------------------
def enumerate_active_loan_ids(
    source_root: Path,
    source_categories: list[str],
) -> set[str]:
    """Return set of loan IDs found under source mount category folders."""
    active: set[str] = set()
    if not source_root.is_dir():
        return active
    for cat_name in source_categories:
        if not cat_name or cat_name.startswith("."):
            continue
        cat = source_root / cat_name
        try:
            if not cat.is_dir():
                continue
            for d in cat.iterdir():
                try:
                    if not d.is_dir() or not _is_source_loan_folder(d.name):
                        continue
                    loan_id = _extract_loan_id_from_folder_name(d.name)
                    if loan_id:
                        active.add(loan_id)
                except OSError:
                    continue
        except OSError:
            continue
    return active


def enumerate_processed_loan_ids(
    tenant_id: str,
    nas_ingest: Path = NAS_INGEST,
    nas_chunk: Path = NAS_CHUNK,
    nas_analyze: Path = NAS_ANALYZE,
) -> dict[str, set[str]]:
    """Return {nas_name: {loan_ids}} for each NAS mount."""
    result: dict[str, set[str]] = {}
    for label, base in [("nas_ingest", nas_ingest), ("nas_chunk", nas_chunk), ("nas_analyze", nas_analyze)]:
        loans_dir = base / "tenants" / tenant_id / "loans"
        ids: set[str] = set()
        if loans_dir.is_dir():
            try:
                for d in loans_dir.iterdir():
                    if d.is_dir() and not d.name.startswith("."):
                        ids.add(d.name)
            except OSError:
                pass
        result[label] = ids
    return result


def find_active_jobs(
    tenant_id: str,
    nas_analyze: Path = NAS_ANALYZE,
) -> set[str]:
    """Return loan_ids that have PENDING or RUNNING jobs."""
    active: set[str] = set()
    # Jobs stored at: nas_analyze/_meta/jobs/*.json (global) or per-loan
    # Check global job store
    jobs_dir = nas_analyze / "_meta" / "jobs"
    if not jobs_dir.is_dir():
        return active
    try:
        for f in jobs_dir.iterdir():
            if not f.name.endswith(".json"):
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                status = data.get("status", "")
                loan_id = data.get("loan_id", "")
                t_id = data.get("tenant_id", "")
                if t_id == tenant_id and status in ("PENDING", "RUNNING") and loan_id:
                    active.add(loan_id)
            except (json.JSONDecodeError, OSError):
                continue
    except OSError:
        pass
    return active


def find_orphaned_loans(
    tenant_id: str,
    source_root: Path,
    source_categories: list[str],
    nas_ingest: Path = NAS_INGEST,
    nas_chunk: Path = NAS_CHUNK,
    nas_analyze: Path = NAS_ANALYZE,
) -> list[OrphanedLoan]:
    """Return list of OrphanedLoan for loans in NAS but not in source."""
    active_ids = enumerate_active_loan_ids(source_root, source_categories)
    processed = enumerate_processed_loan_ids(tenant_id, nas_ingest, nas_chunk, nas_analyze)
    active_jobs = find_active_jobs(tenant_id, nas_analyze)

    # Union of all processed loan IDs
    all_processed: set[str] = set()
    for ids in processed.values():
        all_processed |= ids

    orphaned_ids = sorted(all_processed - active_ids)
    orphans: list[OrphanedLoan] = []
    for loan_id in orphaned_ids:
        locations = {
            label: loan_id in ids
            for label, ids in processed.items()
        }
        orphan = OrphanedLoan(
            loan_id=loan_id,
            locations=locations,
            has_active_job=loan_id in active_jobs,
        )
        orphans.append(orphan)
    return orphans


# ---------------------------------------------------------------------------
# Reporting (no side effects)
# ---------------------------------------------------------------------------
def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _format_size(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes} B"
    elif nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f} KB"
    elif nbytes < 1024 * 1024 * 1024:
        return f"{nbytes / (1024 * 1024):.1f} MB"
    else:
        return f"{nbytes / (1024 * 1024 * 1024):.1f} GB"


def print_orphan_report(
    orphans: list[OrphanedLoan],
    tenant_id: str,
    skip_qdrant: bool = False,
    qdrant_client: Any = None,
    collection_name: str = "",
    nas_ingest: Path = NAS_INGEST,
    nas_chunk: Path = NAS_CHUNK,
    nas_analyze: Path = NAS_ANALYZE,
) -> None:
    """Print a human-readable report of orphaned loans."""
    if not orphans:
        print(f"No orphaned loans found for tenant '{tenant_id}'.")
        return

    deletable = [o for o in orphans if not o.has_active_job]
    skipped = [o for o in orphans if o.has_active_job]

    print(f"\nOrphaned loans for tenant '{tenant_id}': {len(orphans)} found")
    if skipped:
        print(f"  ({len(skipped)} skipped due to active jobs)")
    print()

    for orphan in orphans:
        tag = " [ACTIVE JOB - SKIP]" if orphan.has_active_job else ""
        print(f"  Loan {orphan.loan_id}{tag}")

        for label, base in [("nas_ingest", nas_ingest), ("nas_chunk", nas_chunk), ("nas_analyze", nas_analyze)]:
            if orphan.locations.get(label):
                loan_dir = base / "tenants" / tenant_id / "loans" / orphan.loan_id
                size = _dir_size_bytes(loan_dir)
                print(f"    {label:14s} {_format_size(size)}")

        if not skip_qdrant and qdrant_client and collection_name:
            try:
                from qdrant_client.models import FieldCondition, Filter, MatchValue
                count_result = qdrant_client.count(
                    collection_name=collection_name,
                    count_filter=Filter(must=[
                        FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
                        FieldCondition(key="loan_id", match=MatchValue(value=orphan.loan_id)),
                    ]),
                    exact=True,
                )
                print(f"    {'qdrant':14s} {count_result.count} vectors")
            except Exception as e:
                print(f"    {'qdrant':14s} error: {e}")

        print()


# ---------------------------------------------------------------------------
# Deletion (side effects, only when --confirm)
# ---------------------------------------------------------------------------
def delete_orphan_nas(
    tenant_id: str,
    loan_id: str,
    nas_ingest: Path = NAS_INGEST,
    nas_chunk: Path = NAS_CHUNK,
    nas_analyze: Path = NAS_ANALYZE,
) -> dict[str, bool]:
    """Delete NAS dirs for a loan. Returns {label: deleted}."""
    stats: dict[str, bool] = {}
    for label, base in [("nas_ingest", nas_ingest), ("nas_chunk", nas_chunk), ("nas_analyze", nas_analyze)]:
        loan_dir = base / "tenants" / tenant_id / "loans" / loan_id
        if loan_dir.is_dir():
            shutil.rmtree(loan_dir)
            stats[label] = True
        else:
            stats[label] = False
    return stats


def delete_orphan_qdrant(
    qdrant_client: Any,
    collection_name: str,
    tenant_id: str,
    loan_id: str,
) -> int:
    """Delete Qdrant vectors for a loan. Returns count of vectors deleted."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    filt = Filter(must=[
        FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
        FieldCondition(key="loan_id", match=MatchValue(value=loan_id)),
    ])
    count_result = qdrant_client.count(
        collection_name=collection_name,
        count_filter=filt,
        exact=True,
    )
    count = count_result.count
    if count > 0:
        qdrant_client.delete(
            collection_name=collection_name,
            points_selector=filt,
        )
    return count


# ---------------------------------------------------------------------------
# CLI main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Identify and clean up orphaned loan data (source folder removed but NAS/Qdrant data remains).",
    )
    parser.add_argument("--tenant-id", default=DEFAULT_TENANT, help=f"Tenant ID (default: {DEFAULT_TENANT})")
    parser.add_argument("--dry-run", action="store_true", default=True, help="List orphans without deleting (default)")
    parser.add_argument("--confirm", action="store_true", help="Actually delete orphaned data")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation prompt")
    parser.add_argument("--skip-qdrant", action="store_true", help="Skip Qdrant vector cleanup")
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL, help=f"Qdrant URL (default: {DEFAULT_QDRANT_URL})")
    parser.add_argument("--max-loans", type=int, default=20, help="Max loans to delete per run (default: 20)")
    args = parser.parse_args(argv)

    source_root = SOURCE_MOUNT
    tenant_id = args.tenant_id

    if not source_root.is_dir():
        print(f"ERROR: Source mount not accessible: {source_root}", file=sys.stderr)
        sys.exit(1)

    orphans = find_orphaned_loans(
        tenant_id=tenant_id,
        source_root=source_root,
        source_categories=SOURCE_LOANS_CATEGORIES,
    )

    # Connect to Qdrant if needed
    qdrant = None
    collection = ""
    if not args.skip_qdrant:
        try:
            from qdrant_client import QdrantClient
            qdrant = QdrantClient(url=args.qdrant_url, timeout=10)
            collection = qdrant_collection_name(tenant_id)
        except Exception as e:
            print(f"WARNING: Cannot connect to Qdrant ({e}). Use --skip-qdrant to skip.", file=sys.stderr)
            qdrant = None

    print_orphan_report(
        orphans=orphans,
        tenant_id=tenant_id,
        skip_qdrant=args.skip_qdrant,
        qdrant_client=qdrant,
        collection_name=collection,
    )

    if not args.confirm:
        if orphans:
            print("Run with --confirm to delete.")
        return

    # Filter to deletable orphans (no active jobs)
    deletable = [o for o in orphans if not o.has_active_job]
    if not deletable:
        print("No orphans eligible for deletion (all have active jobs).")
        return

    if len(deletable) > args.max_loans:
        print(f"Safety cap: {len(deletable)} orphans exceed --max-loans={args.max_loans}.")
        print(f"Only the first {args.max_loans} will be processed.")
        deletable = deletable[:args.max_loans]

    # Interactive confirmation
    if not args.yes:
        answer = input(f"\nDelete data for {len(deletable)} orphaned loan(s)? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return

    # Delete
    for orphan in deletable:
        print(f"\nDeleting loan {orphan.loan_id}...")

        if not args.skip_qdrant and qdrant and collection:
            try:
                count = delete_orphan_qdrant(qdrant, collection, tenant_id, orphan.loan_id)
                print(f"  qdrant: {count} vectors deleted")
            except Exception as e:
                print(f"  qdrant: error — {e}")

        stats = delete_orphan_nas(tenant_id, orphan.loan_id)
        for label, deleted in stats.items():
            if deleted:
                print(f"  {label}: deleted")

    print(f"\nDone. {len(deletable)} loan(s) cleaned up.")


if __name__ == "__main__":
    main()
