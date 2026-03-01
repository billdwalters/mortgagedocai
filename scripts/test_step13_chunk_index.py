#!/usr/bin/env python3
"""Tests for _ingest_jsonl_file and _load_chunk_text_index in step13_build_retrieval_pack.py."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# qdrant_client is only used inside main(); mock it so step13 imports cleanly
# on dev machines that don't have the full production dependency stack.
for _mod in (
    "qdrant_client",
    "qdrant_client.http",
    "qdrant_client.http.models",
):
    sys.modules.setdefault(_mod, MagicMock())

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step13_build_retrieval_pack import _ingest_jsonl_file, _load_chunk_text_index
import step13_build_retrieval_pack as _m

import json
import tempfile
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, chunks: list) -> None:
    path.write_text("\n".join(json.dumps(c) for c in chunks) + "\n", encoding="utf-8")


def _chunk(cid, text="hello", doc_id="D1", fpath="a.pdf", page_start=1, page_end=2, idx=0):
    return {
        "chunk_id": cid, "text": text,
        "document_id": doc_id, "file_relpath": fpath,
        "page_start": page_start, "page_end": page_end, "chunk_index": idx,
    }


# ---------------------------------------------------------------------------
# _ingest_jsonl_file tests
# ---------------------------------------------------------------------------

def test_ingest_keeps_first_duplicate():
    """Same chunk_id appearing twice: first occurrence text must win."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "chunks.jsonl"
        _write_jsonl(f, [_chunk("c1", text="FIRST"), _chunk("c1", text="SECOND")])
        idx = {}
        added, dupes = _ingest_jsonl_file(f, idx)
        assert idx["c1"]["text"] == "FIRST", "First occurrence must win"
        assert added == 1
        assert dupes == 1


def test_ingest_returns_added_dupes_tuple():
    """Return value is (added, dupes) tuple, not a bare int."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "chunks.jsonl"
        _write_jsonl(f, [_chunk("c1"), _chunk("c2")])
        idx = {}
        result = _ingest_jsonl_file(f, idx)
        assert isinstance(result, tuple) and len(result) == 2
        assert result == (2, 0)


def test_ingest_skips_invalid_json():
    """Invalid JSON lines are silently skipped; valid lines still indexed."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "chunks.jsonl"
        f.write_text(
            '{"chunk_id":"c1","text":"ok"}\nNOT JSON\n{"chunk_id":"c2","text":"ok2"}\n',
            encoding="utf-8",
        )
        idx = {}
        added, dupes = _ingest_jsonl_file(f, idx)
        assert added == 2
        assert "c1" in idx and "c2" in idx


# ---------------------------------------------------------------------------
# _load_chunk_text_index tests
# ---------------------------------------------------------------------------

def test_load_canonical_layout():
    """Strategy 1 (glob */chunks.jsonl) indexes all doc dirs end-to-end."""
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        for doc, cid, text in [("doc1", "c1", "hello"), ("doc2", "c2", "world")]:
            d = run_dir / "chunks" / doc
            d.mkdir(parents=True)
            _write_jsonl(d / "chunks.jsonl", [_chunk(cid, text=text, doc_id=doc)])
        idx = _load_chunk_text_index(run_dir)
        assert len(idx) == 2
        assert idx["c1"]["text"] == "hello"
        assert idx["c2"]["text"] == "world"


def test_load_first_wins_across_files():
    """Duplicate chunk_id across two sorted doc dirs: first (sorted) wins."""
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        for doc, text in [("aaa", "from_aaa"), ("bbb", "from_bbb")]:
            d = run_dir / "chunks" / doc
            d.mkdir(parents=True)
            _write_jsonl(d / "chunks.jsonl", [_chunk("c1", text=text, doc_id=doc)])
        idx = _load_chunk_text_index(run_dir)
        assert idx["c1"]["text"] == "from_aaa"  # aaa < bbb alphabetically


def test_load_strict_raises_on_unreadable():
    """strict=True: ContractError when _ingest_jsonl_file raises OSError."""
    from lib import ContractError
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        d = run_dir / "chunks" / "doc1"
        d.mkdir(parents=True)
        (d / "chunks.jsonl").write_text('{"chunk_id":"c1","text":"ok"}\n', encoding="utf-8")
        with patch("step13_build_retrieval_pack._ingest_jsonl_file",
                   side_effect=OSError("permission denied (mocked)")):
            try:
                _load_chunk_text_index(run_dir, strict=True)
                assert False, "Expected ContractError"
            except ContractError:
                pass


def test_load_strict_false_skips_unreadable():
    """strict=False: unreadable file skipped; other files still indexed."""
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        for doc, cid, text in [("aaa", "c1", "good"), ("bbb", "c2", "also_good")]:
            d = run_dir / "chunks" / doc
            d.mkdir(parents=True)
            _write_jsonl(d / "chunks.jsonl", [_chunk(cid, text=text, doc_id=doc)])
        orig = _m._ingest_jsonl_file
        def _selective(path, idx):
            if "bbb" in str(path):
                raise OSError("permission denied (mocked)")
            return orig(path, idx)
        with patch("step13_build_retrieval_pack._ingest_jsonl_file", side_effect=_selective):
            idx = _load_chunk_text_index(run_dir, strict=False)
        assert "c1" in idx
        assert "c2" not in idx  # bbb was skipped


def test_load_debug_logs_discovered_count(capsys):
    """_DEBUG=True: prints 'discovered N chunks.jsonl files'."""
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        d = run_dir / "chunks" / "doc1"
        d.mkdir(parents=True)
        _write_jsonl(d / "chunks.jsonl", [_chunk("c1")])
        orig = _m._DEBUG
        try:
            _m._DEBUG = True
            _load_chunk_text_index(run_dir)
            captured = capsys.readouterr()
            assert "discovered" in captured.out
            assert "1" in captured.out
        finally:
            _m._DEBUG = orig


def test_self_test_function():
    """_self_test() runs without raising."""
    _m._self_test()


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t(); print(f"  PASS  {t.__name__}"); passed += 1
        except Exception:
            print(f"  FAIL  {t.__name__}"); traceback.print_exc(); failed += 1
    print(f"\n{passed} passed, {failed} failed.")
