#!/usr/bin/env python3
"""
MortgageDocAI — minimal LOCAL-ONLY FastAPI service wrapping run_loan_job.py and step12/step13.

Usage:
  Install deps (if needed): pip install fastapi uvicorn
  Run: python3 scripts/loan_api.py --host 127.0.0.1 --port 8000

  To allow access from another machine (e.g. Windows client to server 10.10.10.190):
  On the server: python3 scripts/loan_api.py --host 0.0.0.0 --port 8000
  From client:   http://10.10.10.190:8000 (use the server's IP, not 127.0.0.1)

  If "address already in use" when starting: another instance is on 8000. Find it with
  ss -tlnp | grep 8000  then  kill <PID> . Or use  --port 8001  to use a different port.

  If connection refused from another machine:
  - On server: confirm the process is running and you see "Starting Loan API on 0.0.0.0:8000".
  - On server: curl http://127.0.0.1:8000/health then curl http://10.10.10.190:8000/health; if only the first works, the app is bound to localhost only (restart with --host 0.0.0.0).
  - On server: open port 8000 in firewall if needed, e.g. sudo ufw allow 8000/tcp && sudo ufw status (Linux).
  - From client: Test-NetConnection -ComputerName 10.10.10.190 -Port 8000 (PowerShell) or telnet 10.10.10.190 8000 to see if the port is reachable.

  From repo root with venv:
    cd /opt/mortgagedocai && source venv/bin/activate
    python3 scripts/loan_api.py --host 127.0.0.1 --port 8000

Example curl commands:
  curl -s http://127.0.0.1:8000/health
  curl -s http://127.0.0.1:8000/tenants/peak/loans
  curl -s -X POST http://127.0.0.1:8000/tenants/peak/loans/16271681/runs -H "Content-Type: application/json" -d '{"skip_intake":true,"skip_process":true,"run_id":"2026-02-13T073441Z"}'
  curl -s http://127.0.0.1:8000/tenants/peak/loans/16271681/runs
  curl -s http://127.0.0.1:8000/tenants/peak/loans/16271681/runs/2026-02-13T073441Z
  curl -s -X POST http://127.0.0.1:8000/tenants/peak/loans/16271681/runs/2026-02-13T073441Z/query -H "Content-Type: application/json" -d '{"question":"What are the conditions of approval?","profile":"default"}'

  Jobs (background): POST .../jobs to submit, GET /jobs to list, GET /jobs/{job_id} to poll (replace {job_id} with the UUID returned from POST).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure sibling scripts (loan_service) are importable when run from repo root or scripts/
_scripts_dir = Path(__file__).resolve().parent
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from fastapi import FastAPI

from loan_service.adapters_disk import DiskJobStore, JobKeyIndexImpl, LoanLockImpl
from loan_service.adapters_subprocess import SubprocessRunner
from loan_service.api_router import create_router
from loan_service.service import JobService

# ---------------------------------------------------------------------------
# Paths (local-only; no cloud)
# ---------------------------------------------------------------------------
REPO_ROOT = _scripts_dir.parent
SCRIPTS_DIR = _scripts_dir
NAS_ANALYZE = Path("/mnt/nas_apps/nas_analyze")


def _get_base_path() -> Path:
    return NAS_ANALYZE


# ---------------------------------------------------------------------------
# Service and router
# ---------------------------------------------------------------------------
_store = DiskJobStore(_get_base_path)
_key_index = JobKeyIndexImpl()
_loan_lock = LoanLockImpl(_get_base_path)
_runner = SubprocessRunner()
_job_service = JobService(_store, _key_index, _loan_lock, _runner, _get_base_path)
_job_service.load_all_from_disk()

app = FastAPI(title="MortgageDocAI Loan API", description="Local-only loan analysis API")
app.include_router(create_router(_job_service, NAS_ANALYZE, SCRIPTS_DIR, REPO_ROOT))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="MortgageDocAI Loan API (local-only)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1). Use 0.0.0.0 to accept connections from other machines.")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default 8000)")
    args = parser.parse_args()
    print(f"Starting Loan API on {args.host}:{args.port} (0.0.0.0 = listen on all interfaces)", flush=True)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
