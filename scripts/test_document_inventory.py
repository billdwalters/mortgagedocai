"""Tests for _build_document_inventory() — document inventory helper in loan_api.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Import the function under test.  We monkeypatch NAS_INGEST / NAS_CHUNK at
# the module level so the helper reads from tmp_path fixtures.
# ---------------------------------------------------------------------------
import loan_api as _api


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def _make_intake_manifest(base: Path, tenant: str, loan: str, files: list[dict]) -> Path:
    p = base / "tenants" / tenant / "loans" / loan / "_meta" / "intake_manifest.json"
    _write_json(p, {
        "tenant_id": tenant,
        "loan_id": loan,
        "intake_source": "test",
        "timestamp_utc": "2026-03-06T12:00:00Z",
        "files": files,
    })
    return p


def _make_processing_run(base: Path, tenant: str, loan: str, run_id: str,
                         docs_processed: int = 3, total_chunks: int = 50,
                         skipped_encrypted: int = 0) -> Path:
    p = base / "tenants" / tenant / "loans" / loan / run_id / "_meta" / "processing_run.json"
    _write_json(p, {
        "tenant_id": tenant,
        "loan_id": loan,
        "run_id": run_id,
        "documents_processed": docs_processed,
        "total_chunks": total_chunks,
        "skipped_encrypted_count": skipped_encrypted,
    })
    return p


def _make_chunk_map(base: Path, tenant: str, loan: str, run_id: str,
                    document_id: str, chunks: dict) -> Path:
    """Write a chunk_map.json under nas_chunk/.../chunks/<document_id>/."""
    p = base / "tenants" / tenant / "loans" / loan / run_id / "chunks" / document_id / "chunk_map.json"
    _write_json(p, chunks)
    return p


SAMPLE_FILES = [
    {"document_id": "aaa111", "original_source_path": "loan/appraisal.pdf",
     "stored_relative_path": "synology_stage/ts/appraisal.pdf", "size_bytes": 102400, "sha256": "aaa111"},
    {"document_id": "bbb222", "original_source_path": "loan/paystub.xlsx",
     "stored_relative_path": "synology_stage/ts/paystub.xlsx", "size_bytes": 51200, "sha256": "bbb222"},
    {"document_id": "ccc333", "original_source_path": "loan/credit_report.docx",
     "stored_relative_path": "synology_stage/ts/credit_report.docx", "size_bytes": 25600, "sha256": "ccc333"},
]


@pytest.fixture()
def nas_paths(tmp_path, monkeypatch):
    """Set up isolated NAS_INGEST and NAS_CHUNK under tmp_path and monkeypatch loan_api."""
    ingest = tmp_path / "nas_ingest"
    chunk = tmp_path / "nas_chunk"
    ingest.mkdir()
    chunk.mkdir()
    monkeypatch.setattr(_api, "NAS_INGEST", ingest)
    monkeypatch.setattr(_api, "NAS_CHUNK", chunk)
    return {"ingest": ingest, "chunk": chunk}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

TENANT = "peak"
LOAN = "16271681"
RUN_ID = "2026-03-06T120000Z"


class TestCompleteData:
    """1. Complete data → correct merged output."""

    def test_complete_merge(self, nas_paths):
        ingest, chunk = nas_paths["ingest"], nas_paths["chunk"]

        _make_intake_manifest(ingest, TENANT, LOAN, SAMPLE_FILES)
        _make_processing_run(chunk, TENANT, LOAN, RUN_ID, docs_processed=3, total_chunks=50, skipped_encrypted=1)

        # chunk_map for first two docs
        _make_chunk_map(chunk, TENANT, LOAN, RUN_ID, "aaa111", {
            "c1": {"document_id": "aaa111", "file_relpath": "appraisal.pdf", "page_start": 1, "page_end": 5, "chunk_index": 0},
            "c2": {"document_id": "aaa111", "file_relpath": "appraisal.pdf", "page_start": 5, "page_end": 10, "chunk_index": 1},
        })
        _make_chunk_map(chunk, TENANT, LOAN, RUN_ID, "bbb222", {
            "c3": {"document_id": "bbb222", "file_relpath": "paystub.xlsx", "page_start": 1, "page_end": 1, "chunk_index": 0},
        })

        result = _api._build_document_inventory(TENANT, LOAN, RUN_ID)

        assert result["summary"]["documents_ingested"] == 3
        assert result["summary"]["documents_processed"] == 3
        assert result["summary"]["total_chunks"] == 50
        assert result["summary"]["skipped_encrypted_count"] == 1
        assert result["summary"]["total_size_bytes"] == 102400 + 51200 + 25600
        assert result["summary"]["intake_timestamp_utc"] == "2026-03-06T12:00:00Z"

        docs = result["documents"]
        assert len(docs) == 3

        # Check doc with chunk_map (aaa111)
        doc_a = [d for d in docs if d["document_id"] == "aaa111"][0]
        assert doc_a["filename"] == "appraisal.pdf"
        assert doc_a["file_type"] == "PDF"
        assert doc_a["size_bytes"] == 102400
        assert doc_a["page_count"] == 10
        assert doc_a["chunk_count"] == 2

        # Check doc with chunk_map (bbb222)
        doc_b = [d for d in docs if d["document_id"] == "bbb222"][0]
        assert doc_b["filename"] == "paystub.xlsx"
        assert doc_b["file_type"] == "XLSX"
        assert doc_b["page_count"] == 1
        assert doc_b["chunk_count"] == 1

        # Check doc without chunk_map (ccc333)
        doc_c = [d for d in docs if d["document_id"] == "ccc333"][0]
        assert doc_c["filename"] == "credit_report.docx"
        assert doc_c["file_type"] == "DOCX"
        assert doc_c["page_count"] is None
        assert doc_c["chunk_count"] is None

    def test_file_type_counts(self, nas_paths):
        ingest, chunk = nas_paths["ingest"], nas_paths["chunk"]
        _make_intake_manifest(ingest, TENANT, LOAN, SAMPLE_FILES)
        _make_processing_run(chunk, TENANT, LOAN, RUN_ID)

        result = _api._build_document_inventory(TENANT, LOAN, RUN_ID)
        counts = result["summary"]["file_type_counts"]
        assert counts == {"PDF": 1, "XLSX": 1, "DOCX": 1}


class TestMissingIntakeManifest:
    """2. Missing intake_manifest → empty documents, null timestamp."""

    def test_no_manifest(self, nas_paths):
        chunk = nas_paths["chunk"]
        _make_processing_run(chunk, TENANT, LOAN, RUN_ID)

        result = _api._build_document_inventory(TENANT, LOAN, RUN_ID)

        assert result["documents"] == []
        assert result["summary"]["documents_ingested"] == 0
        assert result["summary"]["intake_timestamp_utc"] is None
        # processing_run stats should still be available
        assert result["summary"]["documents_processed"] == 3
        assert result["summary"]["total_chunks"] == 50


class TestMissingProcessingRun:
    """3. Missing processing_run → null summary stats."""

    def test_no_processing_run(self, nas_paths):
        ingest = nas_paths["ingest"]
        _make_intake_manifest(ingest, TENANT, LOAN, SAMPLE_FILES)

        result = _api._build_document_inventory(TENANT, LOAN, RUN_ID)

        assert len(result["documents"]) == 3
        assert result["summary"]["documents_processed"] is None
        assert result["summary"]["total_chunks"] is None
        assert result["summary"]["skipped_encrypted_count"] is None
        # Intake stats should still be available
        assert result["summary"]["documents_ingested"] == 3
        assert result["summary"]["intake_timestamp_utc"] == "2026-03-06T12:00:00Z"


class TestMissingChunkMap:
    """4. Missing chunk_map.json → null page_count/chunk_count per doc."""

    def test_no_chunk_maps(self, nas_paths):
        ingest = nas_paths["ingest"]
        _make_intake_manifest(ingest, TENANT, LOAN, SAMPLE_FILES)
        # processing_run exists but no chunk_map files

        result = _api._build_document_inventory(TENANT, LOAN, RUN_ID)

        for doc in result["documents"]:
            assert doc["page_count"] is None
            assert doc["chunk_count"] is None


class TestFileTypeDetection:
    """5. File type detection (.pdf→PDF, .xlsx→XLSX, unknown→extension)."""

    def test_known_extensions(self, nas_paths):
        ingest = nas_paths["ingest"]
        files = [
            {"document_id": "d1", "original_source_path": "loan/file.pdf",
             "stored_relative_path": "synology_stage/ts/file.pdf", "size_bytes": 100, "sha256": "d1"},
            {"document_id": "d2", "original_source_path": "loan/file.xlsx",
             "stored_relative_path": "synology_stage/ts/file.xlsx", "size_bytes": 200, "sha256": "d2"},
            {"document_id": "d3", "original_source_path": "loan/file.docx",
             "stored_relative_path": "synology_stage/ts/file.docx", "size_bytes": 300, "sha256": "d3"},
            {"document_id": "d4", "original_source_path": "loan/file.jpg",
             "stored_relative_path": "synology_stage/ts/file.jpg", "size_bytes": 400, "sha256": "d4"},
            {"document_id": "d5", "original_source_path": "loan/file.unknown_ext",
             "stored_relative_path": "synology_stage/ts/file.unknown_ext", "size_bytes": 500, "sha256": "d5"},
        ]
        _make_intake_manifest(ingest, TENANT, LOAN, files)

        result = _api._build_document_inventory(TENANT, LOAN, RUN_ID)
        docs_by_id = {d["document_id"]: d for d in result["documents"]}

        assert docs_by_id["d1"]["file_type"] == "PDF"
        assert docs_by_id["d2"]["file_type"] == "XLSX"
        assert docs_by_id["d3"]["file_type"] == "DOCX"
        assert docs_by_id["d4"]["file_type"] == "JPG"
        assert docs_by_id["d5"]["file_type"] == ".unknown_ext"


class TestSummaryAggregation:
    """6. Summary aggregation (total_size_bytes, file_type_counts)."""

    def test_totals(self, nas_paths):
        ingest = nas_paths["ingest"]
        files = [
            {"document_id": "a", "original_source_path": "loan/a.pdf",
             "stored_relative_path": "synology_stage/ts/a.pdf", "size_bytes": 1000, "sha256": "a"},
            {"document_id": "b", "original_source_path": "loan/b.pdf",
             "stored_relative_path": "synology_stage/ts/b.pdf", "size_bytes": 2000, "sha256": "b"},
            {"document_id": "c", "original_source_path": "loan/c.xlsx",
             "stored_relative_path": "synology_stage/ts/c.xlsx", "size_bytes": 500, "sha256": "c"},
        ]
        _make_intake_manifest(ingest, TENANT, LOAN, files)

        result = _api._build_document_inventory(TENANT, LOAN, RUN_ID)

        assert result["summary"]["total_size_bytes"] == 3500
        assert result["summary"]["file_type_counts"] == {"PDF": 2, "XLSX": 1}
        assert result["summary"]["documents_ingested"] == 3


class TestMalformedJson:
    """7. Malformed JSON → graceful partial data."""

    def test_malformed_intake_manifest(self, nas_paths):
        ingest = nas_paths["ingest"]
        p = ingest / "tenants" / TENANT / "loans" / LOAN / "_meta" / "intake_manifest.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{bad json!!!")

        result = _api._build_document_inventory(TENANT, LOAN, RUN_ID)
        # Should degrade gracefully
        assert result["documents"] == []
        assert result["summary"]["documents_ingested"] == 0

    def test_malformed_processing_run(self, nas_paths):
        ingest, chunk = nas_paths["ingest"], nas_paths["chunk"]
        _make_intake_manifest(ingest, TENANT, LOAN, SAMPLE_FILES)

        p = chunk / "tenants" / TENANT / "loans" / LOAN / RUN_ID / "_meta" / "processing_run.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not json")

        result = _api._build_document_inventory(TENANT, LOAN, RUN_ID)

        assert len(result["documents"]) == 3
        assert result["summary"]["documents_processed"] is None
        assert result["summary"]["total_chunks"] is None

    def test_malformed_chunk_map(self, nas_paths):
        ingest, chunk = nas_paths["ingest"], nas_paths["chunk"]
        _make_intake_manifest(ingest, TENANT, LOAN, SAMPLE_FILES)

        # Write malformed chunk_map for first doc
        cm_dir = chunk / "tenants" / TENANT / "loans" / LOAN / RUN_ID / "chunks" / "aaa111"
        cm_dir.mkdir(parents=True, exist_ok=True)
        (cm_dir / "chunk_map.json").write_text("{corrupt}")

        result = _api._build_document_inventory(TENANT, LOAN, RUN_ID)

        doc_a = [d for d in result["documents"] if d["document_id"] == "aaa111"][0]
        assert doc_a["page_count"] is None
        assert doc_a["chunk_count"] is None
