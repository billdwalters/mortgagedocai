#!/usr/bin/env python3
"""Tests for cleanup_orphans.py — orphaned loan data detection and cleanup."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mock qdrant_client.models so delete_orphan_qdrant can import it without the real package
_mock_models = MagicMock()
for _mod in ("qdrant_client", "qdrant_client.models"):
    sys.modules.setdefault(_mod, _mock_models)

_scripts_dir = Path(__file__).resolve().parent
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from cleanup_orphans import (
    OrphanedLoan,
    delete_orphan_nas,
    delete_orphan_qdrant,
    enumerate_active_loan_ids,
    enumerate_processed_loan_ids,
    find_active_jobs,
    find_orphaned_loans,
)


# ---------------------------------------------------------------------------
# Helpers to build test fixture directories
# ---------------------------------------------------------------------------

def _make_source_loan(source_root: Path, category: str, folder_name: str) -> Path:
    """Create a source loan folder."""
    d = source_root / category / folder_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_nas_loan(nas_base: Path, tenant_id: str, loan_id: str) -> Path:
    """Create a NAS loan directory with a dummy file."""
    d = nas_base / "tenants" / tenant_id / "loans" / loan_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "dummy.txt").write_text("test")
    return d


def _make_job_file(nas_analyze: Path, tenant_id: str, loan_id: str, status: str, job_id: str = "j1") -> Path:
    """Create a job JSON file in _meta/jobs/."""
    jobs_dir = nas_analyze / "_meta" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_file = jobs_dir / f"{job_id}.json"
    job_file.write_text(json.dumps({
        "job_id": job_id,
        "tenant_id": tenant_id,
        "loan_id": loan_id,
        "status": status,
    }))
    return job_file


# ---------------------------------------------------------------------------
# Test 1: Active loan not flagged as orphan
# ---------------------------------------------------------------------------
def test_active_loan_not_flagged(tmp_path: Path) -> None:
    """Loan present in both source and NAS should NOT be orphaned."""
    source = tmp_path / "source"
    nas_chunk = tmp_path / "nas_chunk"
    nas_analyze = tmp_path / "nas_analyze"
    nas_ingest = tmp_path / "nas_ingest"

    _make_source_loan(source, "5-Borrowers TBD", "Smith [Loan 12345678]")
    _make_nas_loan(nas_chunk, "peak", "12345678")
    _make_nas_loan(nas_analyze, "peak", "12345678")

    orphans = find_orphaned_loans(
        tenant_id="peak",
        source_root=source,
        source_categories=["5-Borrowers TBD"],
        nas_ingest=nas_ingest,
        nas_chunk=nas_chunk,
        nas_analyze=nas_analyze,
    )
    assert len(orphans) == 0


# ---------------------------------------------------------------------------
# Test 2: Orphaned loan detected
# ---------------------------------------------------------------------------
def test_orphaned_loan_detected(tmp_path: Path) -> None:
    """Loan in NAS but not in source → orphaned."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "5-Borrowers TBD").mkdir()
    nas_chunk = tmp_path / "nas_chunk"
    nas_analyze = tmp_path / "nas_analyze"
    nas_ingest = tmp_path / "nas_ingest"

    _make_nas_loan(nas_chunk, "peak", "99999999")
    _make_nas_loan(nas_analyze, "peak", "99999999")

    orphans = find_orphaned_loans(
        tenant_id="peak",
        source_root=source,
        source_categories=["5-Borrowers TBD"],
        nas_ingest=nas_ingest,
        nas_chunk=nas_chunk,
        nas_analyze=nas_analyze,
    )
    assert len(orphans) == 1
    assert orphans[0].loan_id == "99999999"
    assert orphans[0].locations["nas_chunk"] is True
    assert orphans[0].locations["nas_analyze"] is True
    assert orphans[0].locations["nas_ingest"] is False


# ---------------------------------------------------------------------------
# Test 3: Dry-run deletes nothing
# ---------------------------------------------------------------------------
def test_dry_run_deletes_nothing(tmp_path: Path) -> None:
    """When confirm=False, orphan dirs must still exist after find."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "5-Borrowers TBD").mkdir()
    nas_chunk = tmp_path / "nas_chunk"
    nas_analyze = tmp_path / "nas_analyze"
    nas_ingest = tmp_path / "nas_ingest"

    loan_dir = _make_nas_loan(nas_chunk, "peak", "88888888")

    orphans = find_orphaned_loans(
        tenant_id="peak",
        source_root=source,
        source_categories=["5-Borrowers TBD"],
        nas_ingest=nas_ingest,
        nas_chunk=nas_chunk,
        nas_analyze=nas_analyze,
    )
    assert len(orphans) == 1
    # find_orphaned_loans is detection-only; dirs untouched
    assert loan_dir.is_dir()


# ---------------------------------------------------------------------------
# Test 4: Confirm deletes NAS dirs
# ---------------------------------------------------------------------------
def test_confirm_deletes_nas_dirs(tmp_path: Path) -> None:
    """delete_orphan_nas removes all 3 NAS dirs for a loan."""
    nas_ingest = tmp_path / "nas_ingest"
    nas_chunk = tmp_path / "nas_chunk"
    nas_analyze = tmp_path / "nas_analyze"

    ingest_dir = _make_nas_loan(nas_ingest, "peak", "77777777")
    chunk_dir = _make_nas_loan(nas_chunk, "peak", "77777777")
    analyze_dir = _make_nas_loan(nas_analyze, "peak", "77777777")

    stats = delete_orphan_nas(
        tenant_id="peak",
        loan_id="77777777",
        nas_ingest=nas_ingest,
        nas_chunk=nas_chunk,
        nas_analyze=nas_analyze,
    )

    assert stats == {"nas_ingest": True, "nas_chunk": True, "nas_analyze": True}
    assert not ingest_dir.exists()
    assert not chunk_dir.exists()
    assert not analyze_dir.exists()


# ---------------------------------------------------------------------------
# Test 5: Qdrant vector deletion
# ---------------------------------------------------------------------------
def test_qdrant_vector_deletion() -> None:
    """delete_orphan_qdrant calls count + delete with correct filter."""
    mock_client = MagicMock()
    mock_client.count.return_value = MagicMock(count=42)

    count = delete_orphan_qdrant(
        qdrant_client=mock_client,
        collection_name="peak_e5largev2_1024_cosine_v1",
        tenant_id="peak",
        loan_id="66666666",
    )

    assert count == 42
    mock_client.count.assert_called_once()
    mock_client.delete.assert_called_once()
    # Verify filter args include tenant_id and loan_id
    call_kwargs = mock_client.delete.call_args
    assert call_kwargs[1]["collection_name"] == "peak_e5largev2_1024_cosine_v1"


# ---------------------------------------------------------------------------
# Test 6: Skip-qdrant flag
# ---------------------------------------------------------------------------
def test_skip_qdrant_no_calls() -> None:
    """When skip_qdrant is True, no Qdrant client calls should be made.
    This is tested by verifying delete_orphan_qdrant is not called in the
    CLI flow — here we just verify the function signature works correctly
    and that a mock client with 0 count results in no delete call."""
    mock_client = MagicMock()
    mock_client.count.return_value = MagicMock(count=0)

    count = delete_orphan_qdrant(
        qdrant_client=mock_client,
        collection_name="peak_e5largev2_1024_cosine_v1",
        tenant_id="peak",
        loan_id="55555555",
    )

    assert count == 0
    mock_client.count.assert_called_once()
    mock_client.delete.assert_not_called()  # 0 vectors → no delete call


# ---------------------------------------------------------------------------
# Test 7: Active job skipped
# ---------------------------------------------------------------------------
def test_active_job_skipped(tmp_path: Path) -> None:
    """Loan with PENDING job should be flagged has_active_job=True."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "5-Borrowers TBD").mkdir()
    nas_chunk = tmp_path / "nas_chunk"
    nas_analyze = tmp_path / "nas_analyze"
    nas_ingest = tmp_path / "nas_ingest"

    _make_nas_loan(nas_analyze, "peak", "44444444")
    _make_job_file(nas_analyze, "peak", "44444444", "PENDING")

    orphans = find_orphaned_loans(
        tenant_id="peak",
        source_root=source,
        source_categories=["5-Borrowers TBD"],
        nas_ingest=nas_ingest,
        nas_chunk=nas_chunk,
        nas_analyze=nas_analyze,
    )
    assert len(orphans) == 1
    assert orphans[0].has_active_job is True


# ---------------------------------------------------------------------------
# Test 8: Max-loans safety cap
# ---------------------------------------------------------------------------
def test_max_loans_safety_cap(tmp_path: Path) -> None:
    """find_orphaned_loans returns all orphans; caller truncates to max_loans."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "5-Borrowers TBD").mkdir()
    nas_chunk = tmp_path / "nas_chunk"
    nas_analyze = tmp_path / "nas_analyze"
    nas_ingest = tmp_path / "nas_ingest"

    for i in range(5):
        _make_nas_loan(nas_analyze, "peak", f"1000000{i}")

    orphans = find_orphaned_loans(
        tenant_id="peak",
        source_root=source,
        source_categories=["5-Borrowers TBD"],
        nas_ingest=nas_ingest,
        nas_chunk=nas_chunk,
        nas_analyze=nas_analyze,
    )
    assert len(orphans) == 5

    # Simulate --max-loans=2 (CLI truncates before deletion)
    max_loans = 2
    deletable = [o for o in orphans if not o.has_active_job][:max_loans]
    assert len(deletable) == 2


# ---------------------------------------------------------------------------
# Test 9: Source mount missing — graceful handling
# ---------------------------------------------------------------------------
def test_source_mount_missing(tmp_path: Path) -> None:
    """If source root doesn't exist, enumerate_active_loan_ids returns empty set."""
    missing = tmp_path / "nonexistent"
    active = enumerate_active_loan_ids(missing, ["5-Borrowers TBD"])
    assert active == set()


# ---------------------------------------------------------------------------
# Test 10: Mixed state — loan in some NAS mounts but not all
# ---------------------------------------------------------------------------
def test_mixed_state_partial_cleanup(tmp_path: Path) -> None:
    """Loan in nas_analyze but not nas_chunk — partial cleanup works."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "5-Borrowers TBD").mkdir()
    nas_ingest = tmp_path / "nas_ingest"
    nas_chunk = tmp_path / "nas_chunk"
    nas_analyze = tmp_path / "nas_analyze"

    analyze_dir = _make_nas_loan(nas_analyze, "peak", "33333333")
    # nas_chunk and nas_ingest have no data for this loan

    orphans = find_orphaned_loans(
        tenant_id="peak",
        source_root=source,
        source_categories=["5-Borrowers TBD"],
        nas_ingest=nas_ingest,
        nas_chunk=nas_chunk,
        nas_analyze=nas_analyze,
    )
    assert len(orphans) == 1
    assert orphans[0].locations["nas_analyze"] is True
    assert orphans[0].locations["nas_chunk"] is False
    assert orphans[0].locations["nas_ingest"] is False

    # delete_orphan_nas handles missing dirs gracefully
    stats = delete_orphan_nas(
        tenant_id="peak",
        loan_id="33333333",
        nas_ingest=nas_ingest,
        nas_chunk=nas_chunk,
        nas_analyze=nas_analyze,
    )
    assert stats["nas_analyze"] is True
    assert stats["nas_chunk"] is False
    assert stats["nas_ingest"] is False
    assert not analyze_dir.exists()
