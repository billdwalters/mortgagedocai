#!/usr/bin/env python3
"""Tests for _build_version_blob and _SCHEMA_VERSIONS in step12_analyze.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step12_analyze import (
    _build_version_blob,
    _SCHEMA_VERSIONS,
)

import types
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCHEMAS = {"uw_decision": "v0.7", "uw_conditions": "v1", "income_analysis": "v1", "default": "v1"}


def _make_args(**kwargs):
    defaults = dict(
        tenant_id="t1", loan_id="L1",
        llm_model="llama3", llm_temperature=0.0, llm_max_tokens=2048,
        evidence_max_chars=4000, ollama_url="http://localhost:11434",
    )
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


def _make_run_result(stdout="abc1234def\n", returncode=0):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    return m


def _call_blob(args=None, ctx_run_id="R1", schemas=None,
               rp_path=None, rp_sha256=None, rp_source="unset"):
    args = args or _make_args()
    schemas = schemas or _SCHEMAS
    with patch("step12_analyze.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _make_run_result("abc1234def\n"),  # rev-parse HEAD
            _make_run_result(""),              # status --porcelain (clean)
        ]
        return _build_version_blob(args, ctx_run_id, schemas, rp_path, rp_sha256, rp_source)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_blob_has_required_keys():
    blob = _call_blob()
    for key in ("generated_at_utc", "git", "run", "options", "retrieval_pack", "schemas"):
        assert key in blob, f"missing top-level key: {key}"


def test_blob_run_populated():
    blob = _call_blob(args=_make_args(tenant_id="acme", loan_id="L99"), ctx_run_id="R42")
    assert blob["run"]["tenant_id"] == "acme"
    assert blob["run"]["loan_id"] == "L99"
    assert blob["run"]["run_id"] == "R42"


def test_blob_options_populated_no_offline_embeddings():
    blob = _call_blob(args=_make_args(llm_model="gpt4", llm_temperature=0.5))
    assert blob["options"]["llm_model"] == "gpt4"
    assert blob["options"]["llm_temperature"] == 0.5
    assert "offline_embeddings" not in blob["options"], \
        "offline_embeddings is a step13 arg — must NOT appear in step12 version.json"


def test_blob_retrieval_pack_with_path():
    p = Path("/data/retrieval_pack.json")
    blob = _call_blob(rp_path=p, rp_sha256="deadbeef", rp_source="explicit")
    assert blob["retrieval_pack"]["path"] == str(p)
    assert blob["retrieval_pack"]["sha256"] == "deadbeef"
    assert blob["retrieval_pack"]["source"] == "explicit"


def test_blob_retrieval_pack_missing():
    blob = _call_blob(rp_path=None, rp_sha256=None, rp_source="unset")
    assert blob["retrieval_pack"]["path"] is None
    assert blob["retrieval_pack"]["sha256"] is None
    assert blob["retrieval_pack"]["source"] == "unset"


def test_blob_schemas_passthrough():
    custom = {"uw_decision": "v0.9", "uw_conditions": "v2"}
    blob = _call_blob(schemas=custom)
    assert blob["schemas"] == custom


def test_blob_git_failure_graceful():
    args = _make_args()
    with patch("step12_analyze.subprocess.run", side_effect=OSError("no git")):
        blob = _build_version_blob(args, "R1", _SCHEMAS, None, None, "unset")
    assert blob["git"]["commit"] is None
    assert blob["git"]["dirty"] is None


def test_schema_versions_constant():
    for profile in ("uw_decision", "uw_conditions", "income_analysis", "default"):
        assert profile in _SCHEMA_VERSIONS, f"_SCHEMA_VERSIONS missing profile: {profile}"


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
