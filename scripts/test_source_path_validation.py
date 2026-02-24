"""Tests for A1: POST /tenants/{tenant_id}/loans/{loan_id}/source_path/validate
   and A2: GET /tenants/{tenant_id}/loans/source_folders"""
import os
import pytest
from pathlib import Path
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def tmp_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("source_loans")
    cat = root / "5-Borrowers TBD"
    cat.mkdir()
    loan_dir = cat / "Walters, Bill [Loan 16271681]"
    loan_dir.mkdir()
    return root


@pytest.fixture(scope="module")
def client(tmp_root):
    os.environ["MORTGAGEDOCAI_SOURCE_LOANS_ROOT"] = str(tmp_root)
    os.environ["MORTGAGEDOCAI_SOURCE_LOANS_CATEGORIES"] = "5-Borrowers TBD"
    os.environ.pop("MORTGAGEDOCAI_API_KEY", None)
    os.environ.pop("MORTGAGEDOCAI_ALLOWED_TENANTS", None)
    import importlib
    import sys
    # ensure fresh import from scripts/
    scripts_dir = str(Path(__file__).parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import loan_api as la
    importlib.reload(la)
    return TestClient(la.app)


def test_validate_valid_path(client, tmp_root):
    path = str(tmp_root / "5-Borrowers TBD" / "Walters, Bill [Loan 16271681]")
    r = client.post(
        "/tenants/peak/loans/16271681/source_path/validate",
        json={"source_path": path},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["exists"] is True
    assert d["is_dir"] is True
    assert d["within_root"] is True
    assert d["normalized"] == path


def test_validate_nonexistent_path(client, tmp_root):
    path = str(tmp_root / "5-Borrowers TBD" / "Does Not Exist [Loan 99999]")
    r = client.post(
        "/tenants/peak/loans/99999/source_path/validate",
        json={"source_path": path},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False
    assert d["exists"] is False


def test_validate_outside_root(client):
    r = client.post(
        "/tenants/peak/loans/16271681/source_path/validate",
        json={"source_path": "/tmp"},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False
    assert d["within_root"] is False


def test_validate_empty_path(client):
    r = client.post(
        "/tenants/peak/loans/16271681/source_path/validate",
        json={"source_path": ""},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False
    assert "required" in d["message"].lower()


def test_validate_null_path(client):
    r = client.post(
        "/tenants/peak/loans/16271681/source_path/validate",
        json={},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False
