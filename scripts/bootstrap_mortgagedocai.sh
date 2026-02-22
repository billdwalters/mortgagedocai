#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="/opt/mortgagedocai"
SCRIPTS_DIR="${REPO_ROOT}/scripts"
VENV_DIR="${REPO_ROOT}/venv"

QDRANT_DATA="/scratch/qdrant/data"
QDRANT_SNAPSHOTS="/scratch/qdrant/snapshots"

SOURCE_DIR="${SOURCE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

say(){ echo -e "\n==> $*\n"; }
warn(){ echo -e "\n[WARN] $*\n" >&2; }
die(){ echo -e "\n[ERROR] $*\n" >&2; exit 1; }

need_cmd(){ command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"; }

need_cmd sudo
need_cmd python3
need_cmd findmnt
need_cmd mountpoint

say "Installing OS prerequisites"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip nfs-common jq

say "Creating repo directories"
sudo mkdir -p "${SCRIPTS_DIR}"
sudo chown -R "${USER}:${USER}" "${REPO_ROOT}"

say "Creating Qdrant local SSD dirs"
sudo mkdir -p "${QDRANT_DATA}" "${QDRANT_SNAPSHOTS}"
sudo chown -R "${USER}:${USER}" /scratch || true
sudo chown -R "${USER}:${USER}" "${QDRANT_DATA}" "${QDRANT_SNAPSHOTS}"

say "Validating mounts (does NOT configure mounts)"
for p in /mnt/source_loans /mnt/nas_apps/nas_ingest /mnt/nas_apps/nas_chunk /mnt/nas_apps/nas_analyze; do
  if ! mountpoint -q "$p"; then warn "Mount not present: $p"; fi
done

say "Ensuring atomic publish staging dirs exist"
sudo mkdir -p /mnt/nas_apps/nas_chunk/_staging
sudo mkdir -p /mnt/nas_apps/nas_analyze/_staging

say "Copying canonical scripts from SOURCE_DIR=${SOURCE_DIR}"
cp -f "${SOURCE_DIR}/lib.py" "${SCRIPTS_DIR}/lib.py"
cp -f "${SOURCE_DIR}/run_loan_pipeline.py" "${SCRIPTS_DIR}/run_loan_pipeline.py"
cp -f "${SOURCE_DIR}/step10_intake.py" "${SCRIPTS_DIR}/step10_intake.py"
cp -f "${SOURCE_DIR}/step11_process.py" "${SCRIPTS_DIR}/step11_process.py"
cp -f "${SOURCE_DIR}/step12_analyze.py" "${SCRIPTS_DIR}/step12_analyze.py"
cp -f "${SOURCE_DIR}/step13_build_retrieval_pack.py" "${SCRIPTS_DIR}/step13_build_retrieval_pack.py"
cp -f "${SOURCE_DIR}/requirements.txt" "${SCRIPTS_DIR}/requirements.txt"
chmod +x "${SCRIPTS_DIR}"/*.py || true

say "Creating venv at ${VENV_DIR}"
python3 -m venv "${VENV_DIR}"
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
python -m pip install -U pip
pip install -r "${SCRIPTS_DIR}/requirements.txt"

say "Bootstrap complete"
echo "Repo:   ${REPO_ROOT}"
echo "Scripts:${SCRIPTS_DIR}"
echo "Venv:   ${VENV_DIR}"
