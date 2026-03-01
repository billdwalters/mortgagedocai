#!/usr/bin/env python3
"""
MortgageDocAI — facade for job_runner API (delegates to loan_service).
Used by loan_api.py and by test_job_hardening.py (unchanged).
"""
from __future__ import annotations

from pathlib import Path

# Resolve scripts dir for imports
_scripts_dir = Path(__file__).resolve().parent
import sys
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from loan_service.adapters_disk import DiskJobStore, JobKeyIndexImpl, LoanLockImpl
from loan_service.adapters_subprocess import SubprocessRunner
from loan_service.service import JobService

NAS_ANALYZE = Path("/mnt/nas_apps/nas_analyze")


def _get_base_path() -> Path:
    return NAS_ANALYZE


_store = DiskJobStore(_get_base_path)
_key_index = JobKeyIndexImpl()
_loan_lock = LoanLockImpl(_get_base_path)
_runner = SubprocessRunner()
_service = JobService(_store, _key_index, _loan_lock, _runner, _get_base_path)
_service.load_all_from_disk()

JOBS = _service.get_jobs_mutable()
JOB_KEY_INDEX = _service.get_key_index_mutable()

enqueue_job = _service.enqueue_job
get_job = _service.get_job
list_jobs = _service.list_jobs
load_jobs_from_disk = _service.load_all_from_disk

from loan_service.adapters_subprocess import _quiet_env
