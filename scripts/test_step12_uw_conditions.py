#!/usr/bin/env python3
"""Tests for uw_conditions deduplication helpers in step12_analyze.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step12_analyze import (
    _make_dedupe_key,
    _token_jaccard,
    _dedup_conditions,
)

# ---------------------------------------------------------------------------
# _make_dedupe_key
# ---------------------------------------------------------------------------

def test_make_dedupe_key_lowercase():
    assert _make_dedupe_key("Provide Bank Statements") == "bank statements"

def test_make_dedupe_key_strips_boilerplate():
    assert _make_dedupe_key("Please provide bank statements.") == "bank statements"

def test_make_dedupe_key_punctuation():
    assert _make_dedupe_key("Submit W-2 forms (last 2 years)") == "w 2 forms last 2 years"

def test_make_dedupe_key_collapse_whitespace():
    assert _make_dedupe_key("provide   bank   statements") == "bank statements"

# ---------------------------------------------------------------------------
# _token_jaccard
# ---------------------------------------------------------------------------

def test_token_jaccard_identical():
    assert _token_jaccard("bank statements required", "bank statements required") == 1.0

def test_token_jaccard_disjoint():
    assert _token_jaccard("apple orange", "banana grape") == 0.0

def test_token_jaccard_partial():
    # {bank,statements,required} vs {bank,tax,returns,required}: intersect=2, union=5 = 0.4
    assert abs(_token_jaccard("bank statements required", "bank tax returns required") - 0.4) < 1e-9

def test_token_jaccard_empty():
    assert _token_jaccard("", "") == 0.0

# ---------------------------------------------------------------------------
# _dedup_conditions helpers
# ---------------------------------------------------------------------------

def _make_cond(desc, category="Other", timing="Unknown", citations=None, source=None):
    return {
        "description": desc,
        "category": category,
        "timing": timing,
        "citations": citations or [{"chunk_id": "c1", "quote": "some text"}],
        "source": source or {"documents": []},
    }

# ---------------------------------------------------------------------------
# _dedup_conditions
# ---------------------------------------------------------------------------

def test_dedup_exact_duplicate():
    conds = [_make_cond("Provide bank statements"), _make_cond("Provide bank statements")]
    result, stats = _dedup_conditions(conds)
    assert len(result) == 1
    assert stats["raw_count"] == 2
    assert stats["deduped_count"] == 1
    assert stats["removed_count"] == 1

def test_dedup_near_duplicate_jaccard():
    # 12-token vs 13-token string: jaccard = 12/13 ≈ 0.923 >= 0.92
    a = "aa bb cc dd ee ff gg hh ii jj kk ll"
    b = "aa bb cc dd ee ff gg hh ii jj kk ll mm"
    result, stats = _dedup_conditions([_make_cond(a), _make_cond(b)])
    assert len(result) == 1
    assert stats["removed_count"] == 1

def test_dedup_no_duplicates():
    conds = [_make_cond("bank statements"), _make_cond("tax returns"), _make_cond("pay stubs")]
    result, stats = _dedup_conditions(conds)
    assert len(result) == 3 and stats["removed_count"] == 0

def test_dedup_merge_citations():
    conds = [
        _make_cond("bank statements", citations=[{"chunk_id": "c1", "quote": "q1"}]),
        _make_cond("bank statements", citations=[{"chunk_id": "c2", "quote": "q2"}]),
    ]
    result, _ = _dedup_conditions(conds)
    assert len(result) == 1
    assert {c["chunk_id"] for c in result[0]["citations"]} == {"c1", "c2"}

def test_dedup_sort_order():
    conds = [
        _make_cond("zzz doc", category="Income", timing="Prior to Closing"),
        _make_cond("aaa doc", category="Income", timing="Prior to Closing"),
        _make_cond("mmm doc", category="Assets", timing="Unknown"),
    ]
    result, _ = _dedup_conditions(conds)
    assert result[0]["category"] == "Assets"
    assert result[1]["description"] == "aaa doc"
    assert result[2]["description"] == "zzz doc"

def test_dedup_deterministic():
    import random
    conds = [
        _make_cond("bank statements", category="Assets", timing="Prior to Closing"),
        _make_cond("tax returns", category="Income", timing="Prior to Closing"),
        _make_cond("pay stubs", category="Income", timing="Unknown"),
        _make_cond("insurance policy", category="Insurance", timing="Prior to Docs"),
    ]
    result1, _ = _dedup_conditions(list(conds))
    shuffled = list(conds)
    random.seed(42)
    random.shuffle(shuffled)
    result2, _ = _dedup_conditions(shuffled)
    assert [r["description"] for r in result1] == [r["description"] for r in result2]

def test_dedup_stats():
    conds = [_make_cond("bank statements"), _make_cond("bank statements"), _make_cond("tax returns")]
    _, stats = _dedup_conditions(conds)
    assert stats["raw_count"] == 3
    assert stats["deduped_count"] == 2
    assert stats["removed_count"] == 1
    assert "top_dup_keys" in stats

def test_dedup_confidence_calibration():
    # 4 raw, 2 removed = 50% > 30% -> confidence decremented by 0.1
    conds = [_make_cond("bank statements"), _make_cond("bank statements"),
             _make_cond("tax returns"), _make_cond("tax returns")]
    _, stats = _dedup_conditions(conds)
    confidence = 0.75
    if stats["raw_count"] > 0 and stats["removed_count"] / stats["raw_count"] > 0.30:
        confidence = max(0.3, confidence - 0.1)
    assert confidence == 0.65

def test_dedup_confidence_no_calibration():
    # 4 raw, 1 removed = 25% <= 30% -> confidence unchanged
    conds = [_make_cond("bank statements"), _make_cond("bank statements"),
             _make_cond("tax returns"), _make_cond("pay stubs")]
    _, stats = _dedup_conditions(conds)
    confidence = 0.75
    if stats["raw_count"] > 0 and stats["removed_count"] / stats["raw_count"] > 0.30:
        confidence = max(0.3, confidence - 0.1)
    assert confidence == 0.75


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
