"""Protocol interfaces for job store, key index, loan lock, and pipeline runner."""
from __future__ import annotations

from typing import Any, Protocol

from .domain import JobRecord, JobRequest


class JobStore(Protocol):
    def load_all(self) -> dict[str, dict[str, Any]]:
        """Load all jobs from disk. Returns dict[job_id, job_dict]."""
        ...

    def save(self, job: dict[str, Any]) -> None:
        """Persist a single job (atomic write)."""
        ...


class JobKeyIndex(Protocol):
    def get(self, job_key: str) -> str | None:
        """Return job_id for job_key or None."""
        ...

    def set(self, job_key: str, job_id: str) -> None:
        """Register job_key -> job_id."""
        ...

    def rebuild(self, jobs: dict[str, dict[str, Any]]) -> None:
        """Rebuild index from job dicts (e.g. after load_all)."""
        ...


class LoanLock(Protocol):
    def acquire(self, tenant_id: str, loan_id: str, job_id: str, created_at_utc: str) -> None:
        """Acquire per-loan lock (blocking with retry). Raises on failure."""
        ...

    def release(self, tenant_id: str, loan_id: str) -> None:
        """Release per-loan lock."""
        ...

    def clear_if_stale(self, tenant_id: str, loan_id: str) -> None:
        """Remove lock file if present (e.g. after restart recovery)."""
        ...


class PipelineRunner(Protocol):
    def run(
        self,
        req: dict[str, Any],
        tenant_id: str,
        loan_id: str,
        env: dict[str, str],
        timeout: int,
    ) -> tuple[int, str, str]:
        """Run run_loan_job.py. Returns (returncode, stdout, stderr)."""
        ...
