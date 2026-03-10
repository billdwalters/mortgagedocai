#!/usr/bin/env python3
"""Tests for income_analysis normalization improvements in step12_analyze.py.

Covers:
  - Borrower extraction and normalization
  - Borrower-linked income items (borrower_name, employer fields)
  - Biweekly frequency conversion in _compute_dti
  - Program-specific DTI thresholds in _build_uw_decision
  - Front-end DTI enforcement in _build_uw_decision
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step12_analyze import (
    _normalize_income_analysis,
    _compute_dti,
    _build_uw_decision,
    _INCOME_FREQUENCIES_CANONICAL,
    _PROGRAM_THRESHOLDS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALLOWED = {"c1", "c2", "c3", "c4", "c5"}


def _cit(cid="c1", quote="some text"):
    return {"chunk_id": cid, "quote": quote}


def _make_llm_output(**overrides):
    """Minimal valid LLM output for income_analysis."""
    base = {
        "borrowers": [
            {
                "name": "Jane Doe",
                "role": "primary",
                "employer": "Acme Corp",
                "employment_type": "W2",
                "citations": [_cit("c1")],
            }
        ],
        "income_items": [
            {
                "description": "Base salary",
                "amount": 6000,
                "frequency": "monthly",
                "borrower_name": "Jane Doe",
                "employer": "Acme Corp",
                "citations": [_cit("c1")],
            }
        ],
        "liability_items": [
            {
                "description": "Auto loan",
                "payment_monthly": 400,
                "citations": [_cit("c2")],
            }
        ],
        "proposed_pitia": {
            "value": 1800,
            "citations": [_cit("c3")],
        },
        "confidence": 0.7,
    }
    base.update(overrides)
    return base


def _make_policy(program="Conventional", max_back=0.45, max_front=None):
    return {
        "program": program,
        "thresholds": {
            "max_back_end_dti": max_back,
            "max_front_end_dti": max_front,
        },
        "policy_version": None,
        "policy_source": "default",
        "policy_path": None,
    }


# ---------------------------------------------------------------------------
# Tests: Borrower extraction
# ---------------------------------------------------------------------------

def test_borrowers_extracted():
    """Borrowers array should appear in normalized output."""
    llm = _make_llm_output()
    result = _normalize_income_analysis(llm, ALLOWED)
    assert "borrowers" in result
    assert len(result["borrowers"]) == 1
    b = result["borrowers"][0]
    assert b["name"] == "Jane Doe"
    assert b["role"] == "primary"
    assert b["employer"] == "Acme Corp"
    assert b["employment_type"] == "W2"
    assert len(b["citations"]) >= 1


def test_borrowers_co_borrower():
    """Co-borrower role should be normalized."""
    llm = _make_llm_output(borrowers=[
        {"name": "Jane Doe", "role": "primary", "employer": "Acme Corp",
         "employment_type": "W2", "citations": [_cit("c1")]},
        {"name": "John Doe", "role": "co-borrower", "employer": "Beta Inc",
         "employment_type": "W2", "citations": [_cit("c2")]},
    ])
    result = _normalize_income_analysis(llm, ALLOWED)
    assert len(result["borrowers"]) == 2
    roles = {b["role"] for b in result["borrowers"]}
    assert roles == {"primary", "co-borrower"}


def test_borrowers_missing_returns_empty():
    """If LLM returns no borrowers, output should have empty list."""
    llm = _make_llm_output(borrowers=[])
    result = _normalize_income_analysis(llm, ALLOWED)
    assert result["borrowers"] == []


def test_borrowers_no_key_returns_empty():
    """If LLM omits borrowers key entirely, output should have empty list."""
    llm = _make_llm_output()
    del llm["borrowers"]
    result = _normalize_income_analysis(llm, ALLOWED)
    assert result["borrowers"] == []


def test_borrower_invalid_citations_dropped():
    """Borrowers with citations pointing to unknown chunks should still be kept (citations filtered)."""
    llm = _make_llm_output(borrowers=[
        {"name": "Jane Doe", "role": "primary", "employer": "Acme Corp",
         "employment_type": "W2", "citations": [_cit("bad_chunk")]},
    ])
    result = _normalize_income_analysis(llm, ALLOWED)
    assert len(result["borrowers"]) == 1
    assert result["borrowers"][0]["citations"] == []


def test_borrower_role_normalization():
    """Various role strings should normalize to primary or co-borrower."""
    llm = _make_llm_output(borrowers=[
        {"name": "A", "role": "Co-Borrower", "citations": [_cit("c1")]},
        {"name": "B", "role": "coborrower", "citations": [_cit("c2")]},
        {"name": "C", "role": "PRIMARY", "citations": [_cit("c3")]},
    ])
    result = _normalize_income_analysis(llm, ALLOWED)
    roles = [b["role"] for b in result["borrowers"]]
    assert roles == ["co-borrower", "co-borrower", "primary"]


def test_borrower_missing_name_dropped():
    """Borrowers without a name should be dropped."""
    llm = _make_llm_output(borrowers=[
        {"name": "", "role": "primary", "citations": [_cit("c1")]},
        {"name": "Jane Doe", "role": "primary", "citations": [_cit("c2")]},
    ])
    result = _normalize_income_analysis(llm, ALLOWED)
    assert len(result["borrowers"]) == 1
    assert result["borrowers"][0]["name"] == "Jane Doe"


# ---------------------------------------------------------------------------
# Tests: Income items with borrower_name and employer
# ---------------------------------------------------------------------------

def test_income_item_borrower_name_preserved():
    """Income items should preserve borrower_name if provided."""
    llm = _make_llm_output()
    result = _normalize_income_analysis(llm, ALLOWED)
    item = result["income_items"][0]
    assert item.get("borrower_name") == "Jane Doe"
    assert item.get("employer") == "Acme Corp"


def test_income_item_borrower_name_null_when_missing():
    """Income items without borrower_name should have null."""
    llm = _make_llm_output(income_items=[
        {"description": "Rental income", "amount": 1000, "frequency": "monthly",
         "citations": [_cit("c1")]},
    ])
    result = _normalize_income_analysis(llm, ALLOWED)
    item = result["income_items"][0]
    assert item.get("borrower_name") is None
    assert item.get("employer") is None


# ---------------------------------------------------------------------------
# Tests: Biweekly frequency support
# ---------------------------------------------------------------------------

def test_biweekly_is_canonical():
    """biweekly should be in the canonical frequency set."""
    assert "biweekly" in _INCOME_FREQUENCIES_CANONICAL


def test_biweekly_frequency_preserved():
    """LLM output with biweekly frequency should be preserved, not mapped to unknown."""
    llm = _make_llm_output(income_items=[
        {"description": "Biweekly salary", "amount": 3000, "frequency": "biweekly",
         "citations": [_cit("c1")]},
    ])
    result = _normalize_income_analysis(llm, ALLOWED)
    assert result["income_items"][0]["frequency"] == "biweekly"


def test_dti_biweekly_conversion():
    """Biweekly income should convert to monthly: amount * 26 / 12."""
    normalized = {
        "income_items": [
            {"description": "Biweekly salary", "amount": 3000.0, "frequency": "biweekly"},
        ],
        "liability_items": [],
        "proposed_pitia": {"value": 2000.0, "citations": []},
    }
    dti = _compute_dti(normalized)
    expected_monthly = round(3000.0 * 26 / 12, 2)  # 6500.00
    assert dti["monthly_income_total"] == expected_monthly
    detail = dti["inputs_snapshot"]["income_details"][0]
    assert detail["monthly_equivalent"] == expected_monthly
    assert detail["stated_frequency"] == "biweekly"


def test_dti_weekly_conversion():
    """Weekly income should convert to monthly: amount * 52 / 12."""
    normalized = {
        "income_items": [
            {"description": "Weekly wages", "amount": 1500.0, "frequency": "weekly"},
        ],
        "liability_items": [],
        "proposed_pitia": {"value": 2000.0, "citations": []},
    }
    dti = _compute_dti(normalized)
    expected_monthly = round(1500.0 * 52 / 12, 2)  # 6500.00
    assert dti["monthly_income_total"] == expected_monthly


# ---------------------------------------------------------------------------
# Tests: Program-specific thresholds
# ---------------------------------------------------------------------------

def test_program_thresholds_exist():
    """_PROGRAM_THRESHOLDS should have entries for standard programs."""
    assert "Conventional" in _PROGRAM_THRESHOLDS
    assert "FHA" in _PROGRAM_THRESHOLDS
    assert "VA" in _PROGRAM_THRESHOLDS
    assert "USDA" in _PROGRAM_THRESHOLDS


def test_program_thresholds_have_required_keys():
    """Each program threshold should have max_front_end_dti and max_back_end_dti."""
    for program, thresholds in _PROGRAM_THRESHOLDS.items():
        assert "max_back_end_dti" in thresholds, f"{program} missing max_back_end_dti"
        assert "max_front_end_dti" in thresholds, f"{program} missing max_front_end_dti"


# ---------------------------------------------------------------------------
# Tests: Front-end DTI enforcement
# ---------------------------------------------------------------------------

def test_uw_decision_front_end_fail():
    """Decision should FAIL if front-end DTI exceeds threshold."""
    dti = {
        "front_end_dti": 0.35,
        "back_end_dti": 0.40,
        "missing_inputs": [],
    }
    policy = _make_policy(program="FHA", max_back=0.43, max_front=0.31)
    decision = _build_uw_decision({}, dti, "t1", "L1", "R1", policy)
    assert decision["decision_primary"]["status"] == "FAIL"
    # Should have a front-end reason
    rules = [r["rule"] for r in decision["decision_primary"]["reasons"]]
    assert "DTI_FRONT_END_MAX" in rules


def test_uw_decision_front_end_pass():
    """Decision should PASS when both front-end and back-end are within limits."""
    dti = {
        "front_end_dti": 0.25,
        "back_end_dti": 0.40,
        "missing_inputs": [],
    }
    policy = _make_policy(program="Conventional", max_back=0.45, max_front=0.28)
    decision = _build_uw_decision({}, dti, "t1", "L1", "R1", policy)
    assert decision["decision_primary"]["status"] == "PASS"


def test_uw_decision_front_end_none_skips_check():
    """If max_front_end_dti is None, front-end check should be skipped."""
    dti = {
        "front_end_dti": 0.50,  # would fail any front-end check
        "back_end_dti": 0.40,
        "missing_inputs": [],
    }
    policy = _make_policy(program="VA", max_back=0.41, max_front=None)
    decision = _build_uw_decision({}, dti, "t1", "L1", "R1", policy)
    assert decision["decision_primary"]["status"] == "PASS"
    rules = [r["rule"] for r in decision["decision_primary"]["reasons"]]
    assert "DTI_FRONT_END_MAX" not in rules


def test_uw_decision_back_end_fail_front_end_pass():
    """If back-end fails but front-end passes, overall should be FAIL."""
    dti = {
        "front_end_dti": 0.25,
        "back_end_dti": 0.50,
        "missing_inputs": [],
    }
    policy = _make_policy(max_back=0.45, max_front=0.28)
    decision = _build_uw_decision({}, dti, "t1", "L1", "R1", policy)
    assert decision["decision_primary"]["status"] == "FAIL"
    rules = [r["rule"] for r in decision["decision_primary"]["reasons"]]
    assert "DTI_BACK_END_MAX" in rules
    # Front-end passed, so should show as PASS reason
    front_reason = [r for r in decision["decision_primary"]["reasons"] if r["rule"] == "DTI_FRONT_END_MAX"]
    assert len(front_reason) == 1
    assert front_reason[0]["status"] == "PASS"


def test_uw_decision_both_fail():
    """Both front-end and back-end exceed thresholds."""
    dti = {
        "front_end_dti": 0.35,
        "back_end_dti": 0.50,
        "missing_inputs": [],
    }
    policy = _make_policy(max_back=0.45, max_front=0.31)
    decision = _build_uw_decision({}, dti, "t1", "L1", "R1", policy)
    assert decision["decision_primary"]["status"] == "FAIL"
    rules = [r["rule"] for r in decision["decision_primary"]["reasons"]]
    assert "DTI_FRONT_END_MAX" in rules
    assert "DTI_BACK_END_MAX" in rules


def test_uw_decision_version_bump():
    """Decision ruleset version should be v0.8 after hardening."""
    dti = {"back_end_dti": 0.40, "missing_inputs": []}
    policy = _make_policy()
    decision = _build_uw_decision({}, dti, "t1", "L1", "R1", policy)
    assert decision["ruleset"]["version"] == "v0.8-policy"


# ---------------------------------------------------------------------------
# Manual runner
# ---------------------------------------------------------------------------

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
