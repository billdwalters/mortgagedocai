"""Tests for run history & comparison API helpers (punch list #16)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers to build a fake NAS_ANALYZE tree
# ---------------------------------------------------------------------------

def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _make_run(
    base: Path,
    tenant: str,
    loan: str,
    run_id: str,
    *,
    status: str = "SUCCESS",
    profiles: dict[str, dict[str, dict]] | None = None,
    write_manifest: bool = True,
) -> Path:
    """Create a fake run directory under base/tenants/{tenant}/loans/{loan}/{run_id}."""
    run_dir = base / "tenants" / tenant / "loans" / loan / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if write_manifest:
        _write_json(run_dir / "job_manifest.json", {"status": status, "run_id": run_id})
    if profiles:
        for prof_name, files in profiles.items():
            prof_dir = run_dir / "outputs" / "profiles" / prof_name
            prof_dir.mkdir(parents=True, exist_ok=True)
            for fname, content in files.items():
                _write_json(prof_dir / fname, content)
    return run_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def nas(tmp_path, monkeypatch):
    """Patch NAS_ANALYZE and return the tmp_path used."""
    import loan_api
    monkeypatch.setattr(loan_api, "NAS_ANALYZE", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# _build_run_summary
# ---------------------------------------------------------------------------

class TestBuildRunSummary:
    def test_success_with_all_profiles(self, nas):
        from loan_api import _build_run_summary
        _make_run(
            nas, "peak", "123", "2026-03-10T143022Z",
            profiles={
                "uw_decision": {
                    "decision.json": {
                        "decision_primary": {"status": "PASS"},
                        "confidence": 0.85,
                        "ruleset": {"program": "FHA"},
                        "decision_version": "v0.8-policy",
                    },
                },
                "income_analysis": {
                    "dti.json": {
                        "front_end_dti": 0.265,
                        "back_end_dti": 0.391,
                        "housing_payment_used": 1850.0,
                        "monthly_debt_total": 450.0,
                        "monthly_income_total": 5875.0,
                    },
                    "income_analysis.json": {
                        "monthly_income_total": 5875.0,
                        "monthly_liabilities_total": 450.0,
                        "income_items": [{"a": 1}, {"b": 2}, {"c": 3}],
                        "borrowers": [{"name": "John"}],
                    },
                },
                "uw_conditions": {
                    "conditions.json": {
                        "conditions": [
                            {"timing": "Prior to Docs", "category": "Income"},
                            {"timing": "Prior to Docs", "category": "Income"},
                            {"timing": "Prior to Closing", "category": "Credit"},
                        ],
                    },
                },
                "default": {"answer.json": {"answer": "ok"}},
            },
        )
        result = _build_run_summary("peak", "123", "2026-03-10T143022Z")
        assert result["run_id"] == "2026-03-10T143022Z"
        assert result["status"] == "SUCCESS"
        assert set(result["profiles_available"]) == {"uw_decision", "income_analysis", "uw_conditions", "default"}
        # Decision
        assert result["decision"]["status"] == "PASS"
        assert result["decision"]["program"] == "FHA"
        assert result["decision"]["confidence"] == 0.85
        # DTI
        assert result["dti"]["front_end_dti"] == 0.265
        assert result["dti"]["back_end_dti"] == 0.391
        assert result["dti"]["monthly_income_total"] == 5875.0
        # Income summary
        assert result["income_summary"]["income_item_count"] == 3
        assert result["income_summary"]["borrower_count"] == 1
        # Conditions summary
        assert result["conditions_summary"]["total_count"] == 3
        assert result["conditions_summary"]["by_timing"]["Prior to Docs"] == 2
        assert result["conditions_summary"]["by_timing"]["Prior to Closing"] == 1

    def test_missing_profiles_returns_nulls(self, nas):
        """Older run with only default profile — decision/dti/conditions should be null."""
        from loan_api import _build_run_summary
        _make_run(
            nas, "peak", "123", "2026-03-08T091500Z",
            profiles={"default": {"answer.json": {"answer": "hi"}}},
        )
        result = _build_run_summary("peak", "123", "2026-03-08T091500Z")
        assert result["run_id"] == "2026-03-08T091500Z"
        assert result["status"] == "SUCCESS"
        assert result["profiles_available"] == ["default"]
        assert result["decision"] is None
        assert result["dti"] is None
        assert result["income_summary"] is None
        assert result["conditions_summary"] is None

    def test_missing_manifest_still_works(self, nas):
        """Run dir exists with outputs but no manifest."""
        from loan_api import _build_run_summary
        _make_run(
            nas, "peak", "123", "2026-03-07T120000Z",
            write_manifest=False,
            profiles={"default": {"answer.json": {"answer": "x"}}},
        )
        result = _build_run_summary("peak", "123", "2026-03-07T120000Z")
        assert result["status"] is None
        assert result["profiles_available"] == ["default"]

    def test_nonexistent_run_raises(self, nas):
        """Requesting a run that doesn't exist should raise FileNotFoundError."""
        from loan_api import _build_run_summary
        with pytest.raises(FileNotFoundError):
            _build_run_summary("peak", "123", "2026-01-01T000000Z")


# ---------------------------------------------------------------------------
# _build_comparison_diff
# ---------------------------------------------------------------------------

class TestBuildComparisonDiff:
    def _make_summary(self, **overrides):
        base = {
            "run_id": "2026-03-10T143022Z",
            "status": "SUCCESS",
            "profiles_available": ["default"],
            "decision": None,
            "dti": None,
            "income_summary": None,
            "conditions_summary": None,
        }
        base.update(overrides)
        return base

    def test_decision_changed(self):
        from loan_api import _build_comparison_diff
        a = self._make_summary(
            run_id="2026-03-08T091500Z",
            decision={"status": "FAIL", "program": "FHA", "confidence": 0.6},
        )
        b = self._make_summary(
            run_id="2026-03-10T143022Z",
            decision={"status": "PASS", "program": "FHA", "confidence": 0.85},
        )
        diff = _build_comparison_diff(a, b)
        assert diff["decision_status"]["changed"] is True
        assert diff["decision_status"]["a"] == "FAIL"
        assert diff["decision_status"]["b"] == "PASS"

    def test_dti_delta(self):
        from loan_api import _build_comparison_diff
        a = self._make_summary(
            dti={"front_end_dti": 0.265, "back_end_dti": 0.481, "monthly_income_total": 4800.0},
        )
        b = self._make_summary(
            dti={"front_end_dti": 0.265, "back_end_dti": 0.391, "monthly_income_total": 5875.0},
        )
        diff = _build_comparison_diff(a, b)
        assert diff["front_end_dti"]["changed"] is False
        assert diff["back_end_dti"]["changed"] is True
        assert abs(diff["back_end_dti"]["delta"] - (-0.09)) < 0.001
        assert diff["monthly_income_total"]["changed"] is True
        assert abs(diff["monthly_income_total"]["delta"] - 1075.0) < 0.01

    def test_no_changes(self):
        from loan_api import _build_comparison_diff
        dec = {"status": "PASS", "program": "FHA", "confidence": 0.8}
        dti = {"front_end_dti": 0.25, "back_end_dti": 0.40, "monthly_income_total": 5000.0}
        a = self._make_summary(decision=dec, dti=dti)
        b = self._make_summary(decision=dec, dti=dti)
        diff = _build_comparison_diff(a, b)
        for key, entry in diff.items():
            assert entry["changed"] is False, f"{key} should not be changed"

    def test_missing_fields_on_one_side(self):
        """One run has DTI, the other doesn't — should show changed with nulls."""
        from loan_api import _build_comparison_diff
        a = self._make_summary(dti=None)
        b = self._make_summary(
            dti={"front_end_dti": 0.25, "back_end_dti": 0.40, "monthly_income_total": 5000.0},
        )
        diff = _build_comparison_diff(a, b)
        assert diff["back_end_dti"]["changed"] is True
        assert diff["back_end_dti"]["a"] is None
        assert diff["back_end_dti"]["b"] == 0.40

    def test_conditions_count_diff(self):
        from loan_api import _build_comparison_diff
        a = self._make_summary(
            conditions_summary={"total_count": 15, "by_timing": {}, "by_category": {}},
        )
        b = self._make_summary(
            conditions_summary={"total_count": 12, "by_timing": {}, "by_category": {}},
        )
        diff = _build_comparison_diff(a, b)
        assert diff["conditions_count"]["changed"] is True
        assert diff["conditions_count"]["delta"] == -3


# ---------------------------------------------------------------------------
# Enhanced list_runs
# ---------------------------------------------------------------------------

class TestListRunsEnriched:
    def test_returns_runs_with_metadata(self, nas):
        from loan_api import _build_enriched_run_list
        _make_run(nas, "peak", "123", "2026-03-10T143022Z",
                  profiles={"default": {"answer.json": {}}})
        _make_run(nas, "peak", "123", "2026-03-08T091500Z",
                  profiles={"default": {"answer.json": {}}, "uw_decision": {"decision.json": {}}})
        result = _build_enriched_run_list("peak", "123")
        assert len(result) == 2
        # Should be sorted newest-first
        assert result[0]["run_id"] == "2026-03-10T143022Z"
        assert result[1]["run_id"] == "2026-03-08T091500Z"
        assert "uw_decision" in result[1]["profiles_available"]
        assert result[0]["status"] == "SUCCESS"

    def test_limit_parameter(self, nas):
        from loan_api import _build_enriched_run_list
        for i in range(5):
            rid = f"2026-03-{10+i:02d}T120000Z"
            _make_run(nas, "peak", "123", rid, profiles={"default": {"answer.json": {}}})
        result = _build_enriched_run_list("peak", "123", limit=3)
        assert len(result) == 3
        # Newest first
        assert result[0]["run_id"] == "2026-03-14T120000Z"

    def test_empty_loan_returns_empty(self, nas):
        from loan_api import _build_enriched_run_list
        loan_dir = nas / "tenants" / "peak" / "loans" / "123"
        loan_dir.mkdir(parents=True)
        result = _build_enriched_run_list("peak", "123")
        assert result == []
