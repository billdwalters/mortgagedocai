"""Pure data models for the loan job system. No pydantic."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

JobStatus = Literal["PENDING", "RUNNING", "SUCCESS", "FAIL"]


def _utc_now_z() -> str:
    """UTC ISO8601 string ending with Z (same as job_runner)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class JobRequest:
    """Request payload for enqueue (fields that map to run_loan_job.py)."""
    run_id: str | None = None
    skip_intake: bool = False
    skip_process: bool = False
    source_path: str | None = None
    smoke_debug: bool = False
    run_llm: int | None = None
    max_dropped_chunks: int | None = None
    expect_rp_hash_stable: bool | None = None
    timeout: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> JobRequest:
        return cls(
            run_id=d.get("run_id"),
            skip_intake=d.get("skip_intake", False),
            skip_process=d.get("skip_process", False),
            source_path=d.get("source_path"),
            smoke_debug=d.get("smoke_debug", False),
            run_llm=d.get("run_llm"),
            max_dropped_chunks=d.get("max_dropped_chunks"),
            expect_rp_hash_stable=d.get("expect_rp_hash_stable"),
            timeout=d.get("timeout"),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "skip_intake": self.skip_intake,
            "skip_process": self.skip_process,
            "smoke_debug": self.smoke_debug,
        }
        if self.run_id is not None:
            out["run_id"] = self.run_id
        if self.source_path is not None:
            out["source_path"] = self.source_path
        if self.run_llm is not None:
            out["run_llm"] = self.run_llm
        if self.max_dropped_chunks is not None:
            out["max_dropped_chunks"] = self.max_dropped_chunks
        if self.expect_rp_hash_stable is not None:
            out["expect_rp_hash_stable"] = self.expect_rp_hash_stable
        if self.timeout is not None:
            out["timeout"] = self.timeout
        return out


@dataclass
class JobResult:
    """Manifest-derived result fields."""
    manifest_path: str
    status: str | None
    rp_sha256: str | None
    outputs_base: str | None


@dataclass
class JobRecord:
    """Full job record (dict-like for persistence; job_key is internal-only)."""
    job_id: str
    tenant_id: str
    loan_id: str
    status: JobStatus
    created_at_utc: str
    started_at_utc: str | None
    finished_at_utc: str | None
    request: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    stdout: str | None
    stderr: str | None
    run_id: str | None = None
    job_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "job_id": self.job_id,
            "tenant_id": self.tenant_id,
            "loan_id": self.loan_id,
            "status": self.status,
            "created_at_utc": self.created_at_utc,
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
            "request": self.request,
            "result": self.result,
            "error": self.error,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }
        if self.run_id is not None:
            d["run_id"] = self.run_id
        if self.job_key is not None:
            d["job_key"] = self.job_key
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> JobRecord:
        return cls(
            job_id=d["job_id"],
            tenant_id=d["tenant_id"],
            loan_id=d["loan_id"],
            status=d["status"],
            created_at_utc=d.get("created_at_utc") or "",
            started_at_utc=d.get("started_at_utc"),
            finished_at_utc=d.get("finished_at_utc"),
            request=d.get("request") or {},
            result=d.get("result"),
            error=d.get("error"),
            stdout=d.get("stdout"),
            stderr=d.get("stderr"),
            run_id=d.get("run_id"),
            job_key=d.get("job_key"),
        )

    def to_api_dict(self) -> dict[str, Any]:
        """Same as to_dict but omit job_key (internal only)."""
        d = self.to_dict()
        d.pop("job_key", None)
        return {k: v for k, v in d.items() if v is not None}
