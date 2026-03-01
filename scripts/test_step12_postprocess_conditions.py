#!/usr/bin/env python3
"""Tests for _postprocess_conditions in step12_analyze.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step12_analyze import _postprocess_conditions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cond(desc, category="Other", timing="Unknown", citations=None, source=None):
    return {
        "description": desc,
        "category": category,
        "timing": timing,
        "citations": citations or [{"chunk_id": "c1", "quote": "some text"}],
        "source": source or {"documents": []},
    }


def _doc(doc_id="D1", fpath="a.pdf", page_start=1, page_end=2, chunk_ids=None):
    return {
        "document_id": doc_id,
        "file_relpath": fpath,
        "page_start": page_start,
        "page_end": page_end,
        "chunk_ids": chunk_ids or ["c1"],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_postprocess_empty():
    assert _postprocess_conditions([]) == []


def test_postprocess_passthrough_single():
    cond = _make_cond("bank statements")
    result = _postprocess_conditions([cond])
    assert result == [cond]


def test_postprocess_new_boilerplate_obtain():
    # "obtain" is new vs _dedup_conditions — must be stripped to produce same key
    conds = [_make_cond("obtain bank statements"), _make_cond("bank statements")]
    result = _postprocess_conditions(conds)
    assert len(result) == 1


def test_postprocess_new_boilerplate_verify():
    conds = [_make_cond("verify employment income"), _make_cond("employment income")]
    result = _postprocess_conditions(conds)
    assert len(result) == 1


def test_postprocess_new_boilerplate_furnish():
    conds = [_make_cond("furnish tax returns"), _make_cond("tax returns")]
    result = _postprocess_conditions(conds)
    assert len(result) == 1


def test_postprocess_fixed_category_order():
    # Verification(0) < Assets(1) < Income(2) — regardless of alphabetical order
    conds = [
        _make_cond("zzz", category="Income"),
        _make_cond("aaa", category="Verification"),
        _make_cond("mmm", category="Assets"),
    ]
    result = _postprocess_conditions(conds)
    assert [c["category"] for c in result] == ["Verification", "Assets", "Income"]


def test_postprocess_fixed_timing_order():
    # Prior to Docs(0) < Prior to Closing(1) < Post Closing(2) < Unknown(3)
    conds = [
        _make_cond("aaa", timing="Unknown"),
        _make_cond("bbb", timing="Post Closing"),
        _make_cond("ccc", timing="Prior to Closing"),
        _make_cond("ddd", timing="Prior to Docs"),
    ]
    result = _postprocess_conditions(conds)
    assert [c["timing"] for c in result] == [
        "Prior to Docs", "Prior to Closing", "Post Closing", "Unknown"
    ]


def test_postprocess_description_casefold_sort():
    # Within the same category+timing bucket, sort by casefold description
    conds = [
        _make_cond("Zebra docs", category="Other"),
        _make_cond("alpha docs", category="Other"),
    ]
    result = _postprocess_conditions(conds)
    assert result[0]["description"] == "alpha docs"
    assert result[1]["description"] == "Zebra docs"


def test_postprocess_source_docs_merged():
    # Two conditions with same description but different source docs → docs are unioned
    d1 = _doc("D1", "a.pdf", 1, 2)
    d2 = _doc("D2", "b.pdf", 3, 4)
    conds = [
        _make_cond("bank statements", source={"documents": [d1]}),
        _make_cond("bank statements", source={"documents": [d2]}),
    ]
    result = _postprocess_conditions(conds)
    assert len(result) == 1
    docs = result[0]["source"]["documents"]
    assert len(docs) == 2
    assert {d["document_id"] for d in docs} == {"D1", "D2"}


def test_postprocess_source_docs_deduped():
    # Same document in both members → appears only once in output
    d = _doc("D1", "a.pdf", 1, 2)
    conds = [
        _make_cond("bank statements", source={"documents": [d]}),
        _make_cond("bank statements", source={"documents": [d]}),
    ]
    result = _postprocess_conditions(conds)
    assert len(result) == 1
    assert len(result[0]["source"]["documents"]) == 1


def test_postprocess_debug_prints(capsys):
    import step12_analyze as _m
    orig = _m._DEBUG
    try:
        _m._DEBUG = True
        _postprocess_conditions([_make_cond("bank statements"), _make_cond("bank statements")],
                                debug=True)
        captured = capsys.readouterr()
        assert "UW_COND_DEDUPE" in captured.out
        assert "raw=2" in captured.out
        assert "merged=1" in captured.out
        assert "removed=1" in captured.out
    finally:
        _m._DEBUG = orig


def test_postprocess_debug_silent_by_default(capsys):
    _postprocess_conditions([_make_cond("bank statements")])
    captured = capsys.readouterr()
    assert "UW_COND_DEDUPE" not in captured.out


def test_postprocess_deterministic():
    import random
    conds = [
        _make_cond("bank statements", category="Assets", timing="Prior to Closing"),
        _make_cond("tax returns", category="Income", timing="Prior to Closing"),
        _make_cond("pay stubs", category="Income", timing="Unknown"),
        _make_cond("insurance policy", category="Insurance", timing="Prior to Docs"),
    ]
    result1 = _postprocess_conditions(list(conds))
    shuffled = list(conds)
    random.seed(42)
    random.shuffle(shuffled)
    result2 = _postprocess_conditions(shuffled)
    assert [r["description"] for r in result1] == [r["description"] for r in result2]


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
