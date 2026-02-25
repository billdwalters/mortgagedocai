#!/usr/bin/env bash
# ==============================================================================
# MortgageDocAI — Bootstrap & Verify
#
# Usage:
#   ./bootstrap_mortgagedocai.sh --install   # Full idempotent install / re-deploy
#   ./bootstrap_mortgagedocai.sh --verify    # System health check (read-only)
#   ./bootstrap_mortgagedocai.sh --help      # Show usage
#
# Design:
#   --install is fully idempotent; safe to re-run after Tailscale auth, updates, etc.
#   --verify  makes no changes; exits non-zero if any check FAILs.
#   CIFS mounts are validated but NOT configured here (managed via /etc/fstab).
#   Docker and Ollama must be pre-installed (see pre-bootstrap checklist below).
#
# Pre-bootstrap checklist (fresh server — required BEFORE running --install):
#   1. Install Docker + compose plugin:
#        https://docs.docker.com/engine/install/ubuntu/
#        sudo apt-get install -y docker-compose-plugin
#   2. Install Ollama:
#        curl -fsSL https://ollama.ai/install.sh | sh
#   3. Provision /scratch as local SSD (ext4 mount or symlink for Qdrant data)
#   4. Configure CIFS fstab entries (see repo root fstab reference), then:
#        sudo mount -a
#   5. Create SMB credential files (chmod 600):
#        /etc/smb_ai_svc                       (TrueNAS: nas_ingest/chunk/analyze)
#        /root/.smbcredentials-peakcapital     (Synology: source_loans, RO)
#
# Post-install steps (after --install):
#   a. sudo tailscale up --advertise-tags=tag:mortgagedocai   (interactive auth)
#   b. ./bootstrap_mortgagedocai.sh --install                 (re-run to patch Tailscale IP)
#   c. cd /opt/mortgagedocai && docker compose up -d
#   d. sudo systemctl start ollama
#   e. ./bootstrap_mortgagedocai.sh --verify
# ==============================================================================
set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────────
REPO_ROOT="/opt/mortgagedocai"
SCRIPTS_DIR="${REPO_ROOT}/scripts"
VENV_DIR="${REPO_ROOT}/venv"
QDRANT_DATA="/scratch/qdrant/data"
QDRANT_SNAPSHOTS="/scratch/qdrant/snapshots"
API_PORT=8000
QDRANT_PORT=6333
OLLAMA_PORT=11434

# Script source dir: defaults to the directory this script lives in.
_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${SOURCE_DIR:-${_SELF_DIR}}"

# Repo service root (where *.service files and infra/ live):
# If running from scripts/, go one level up; otherwise SOURCE_DIR is already repo root.
if [[ "$(basename "${SOURCE_DIR}")" == "scripts" ]]; then
  REPO_ROOT_SVC="$(cd "${SOURCE_DIR}/.." && pwd)"
else
  REPO_ROOT_SVC="${SOURCE_DIR}"
fi

REQUIRED_MOUNTS=(
  /mnt/source_loans
  /mnt/nas_apps/nas_ingest
  /mnt/nas_apps/nas_chunk
  /mnt/nas_apps/nas_analyze
)

# ── Terminal helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

_PASS=0; _FAIL=0; _WARN=0

say()  { echo -e "\n${BOLD}==> $*${NC}\n"; }
ok()   { echo -e "  ${GREEN}[OK]${NC}  $*";   _PASS=$((_PASS+1)); }
fail() { echo -e "  ${RED}[FAIL]${NC} $*"; _FAIL=$((_FAIL+1)); }
warn() { echo -e "  ${YELLOW}[WARN]${NC} $*" >&2; _WARN=$((_WARN+1)); }
info() { echo -e "  ${CYAN}[INFO]${NC} $*"; }
die()  { echo -e "\n${RED}[ERROR]${NC} $*\n" >&2; exit 1; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"; }

# ── Usage ──────────────────────────────────────────────────────────────────────
usage() {
  cat <<EOF

${BOLD}MortgageDocAI Bootstrap & Verify${NC}

Usage:
  $(basename "${BASH_SOURCE[0]}") --install   Full idempotent install / re-deploy
  $(basename "${BASH_SOURCE[0]}") --verify    System health check (read-only, exits 1 on failures)
  $(basename "${BASH_SOURCE[0]}") --help      Show this help

Environment overrides:
  SOURCE_DIR=/path          Override source directory (default: directory containing this script)
  INSTALL_CADDY=true        Install Caddy reverse proxy (default: false — Tailscale handles transport)
  MORTGAGEDOCAI_API_KEY=…   API key for --verify health check (auto-read from service file if unset)

Pre-bootstrap checklist (fresh server only):
  ✗ Install Docker + docker-compose-plugin    (not managed by this script)
  ✗ Install Ollama                            (curl -fsSL https://ollama.ai/install.sh | sh)
  ✗ Provision /scratch local SSD             (ext4 mount or symlink for Qdrant data)
  ✗ Add CIFS entries to /etc/fstab; sudo mount -a
  ✗ Create SMB credential files (chmod 600):
      /etc/smb_ai_svc
      /root/.smbcredentials-peakcapital

Post-install sequence:
  sudo tailscale up --advertise-tags=tag:mortgagedocai   (browser auth — interactive)
  $(basename "${BASH_SOURCE[0]}") --install               (re-run to patch Tailscale bind address)
  cd /opt/mortgagedocai && docker compose up -d
  sudo systemctl start ollama
  $(basename "${BASH_SOURCE[0]}") --verify

EOF
}

# ══════════════════════════════════════════════════════════════════════════════
# --verify
# ══════════════════════════════════════════════════════════════════════════════
cmd_verify() {
  echo ""
  echo -e "${BOLD}══════════════════════════════════════════${NC}"
  echo -e "${BOLD}  MortgageDocAI — System Verify           ${NC}"
  echo -e "${BOLD}══════════════════════════════════════════${NC}"

  # ── 1. Systemd services ──────────────────────────────────────────────────────
  say "Services  (systemctl is-active)"

  for svc in mortgagedocai-api mortgagedocai-job-worker ollama docker; do
    status="$(systemctl is-active "${svc}" 2>/dev/null || echo "not-found")"
    if [[ "${status}" == "active" ]]; then
      ok "${svc}: ${status}"
    else
      fail "${svc}: ${status}"
    fi
  done

  # Qdrant runs via Docker, not a native systemd unit
  if command -v docker >/dev/null 2>&1 && \
     docker ps --format '{{.Names}}' 2>/dev/null | grep -qi qdrant; then
    ok "qdrant container: running"
  else
    fail "qdrant container: not running  →  cd ${REPO_ROOT} && docker compose up -d"
  fi

  # ── 2. Port 8000 ─────────────────────────────────────────────────────────────
  say "Port ${API_PORT}  (ss -ltnp)"
  ss_out="$(ss -ltnp 2>/dev/null | grep ":${API_PORT} " || true)"
  if [[ -n "${ss_out}" ]]; then
    ok "Port ${API_PORT}: LISTENING"
    info "${ss_out}"
  else
    fail "Port ${API_PORT}: NOT listening"
  fi

  # ── 3. API health check ───────────────────────────────────────────────────────
  say "API health  (curl /health with API key)"
  TS_IP="$(tailscale ip -4 2>/dev/null || true)"
  if [[ -z "${TS_IP}" ]]; then
    warn "Tailscale not connected — using 127.0.0.1 for health check"
    CHECK_HOST="127.0.0.1"
  else
    CHECK_HOST="${TS_IP}"
  fi

  # Read API key: env override first, then read from /etc/mortgagedocai/env
  # (key is NOT in the service file — it lives in EnvironmentFile=/etc/mortgagedocai/env)
  API_KEY="${MORTGAGEDOCAI_API_KEY:-}"
  if [[ -z "${API_KEY}" ]]; then
    API_KEY="$(sudo grep -oP '(?<=MORTGAGEDOCAI_API_KEY=)\S+' \
        /etc/mortgagedocai/env 2>/dev/null || true)"
  fi
  # Skip health check if key is still the bootstrap placeholder
  if [[ "${API_KEY:-}" == "CHANGE_ME" ]]; then
    warn "API key is CHANGE_ME in /etc/mortgagedocai/env — skipping authenticated health check"
    API_KEY=""
  fi

  HEALTH_URL="http://${CHECK_HOST}:${API_PORT}/health"
  CURL_ARGS=(-s --max-time 5)
  [[ -n "${API_KEY}" ]] && CURL_ARGS+=(-H "X-API-Key: ${API_KEY}")

  HTTP_CODE="$(curl "${CURL_ARGS[@]}" -o /dev/null -w '%{http_code}' \
      "${HEALTH_URL}" 2>/dev/null || echo "ERR")"
  BODY="$(curl "${CURL_ARGS[@]}" "${HEALTH_URL}" 2>/dev/null || echo "(no response)")"

  if [[ "${HTTP_CODE}" == "200" ]]; then
    ok "GET ${HEALTH_URL}  →  HTTP ${HTTP_CODE}"
    info "${BODY}"
  else
    fail "GET ${HEALTH_URL}  →  HTTP ${HTTP_CODE}"
    [[ -n "${BODY}" ]] && info "${BODY}"
  fi

  # ── 4. CIFS / NFS mounts ─────────────────────────────────────────────────────
  say "CIFS mounts  (findmnt)"
  for p in "${REQUIRED_MOUNTS[@]}"; do
    if findmnt -n "${p}" >/dev/null 2>&1; then
      opts="$(findmnt -no OPTIONS "${p}" 2>/dev/null | cut -c1-72)"
      ok "${p}  [${opts}]"
    else
      fail "${p}: NOT mounted  →  sudo mount ${p}"
    fi
  done

  # source_loans contract: must be read-only
  if findmnt -n /mnt/source_loans >/dev/null 2>&1; then
    SOPTS="$(findmnt -no OPTIONS /mnt/source_loans 2>/dev/null || true)"
    if echo "${SOPTS}" | tr ',' '\n' | grep -qx "ro"; then
      ok "/mnt/source_loans: read-only  ✓  (contract requirement met)"
    else
      warn "/mnt/source_loans: NOT mounted read-only — OPTIONS=${SOPTS}"
    fi
  fi

  # ── 5. Qdrant ────────────────────────────────────────────────────────────────
  say "Qdrant  (localhost:${QDRANT_PORT})"
  QDR_CODE="$(curl -s --max-time 3 -o /dev/null -w '%{http_code}' \
      "http://127.0.0.1:${QDRANT_PORT}/readyz" 2>/dev/null || echo "ERR")"
  if [[ "${QDR_CODE}" == "200" ]]; then
    ok "GET /readyz  →  HTTP ${QDR_CODE}"
  else
    fail "GET /readyz  →  HTTP ${QDR_CODE}  →  cd ${REPO_ROOT} && docker compose up -d"
  fi

  # ── 6. Ollama ────────────────────────────────────────────────────────────────
  say "Ollama  (localhost:${OLLAMA_PORT})"
  OLL_CODE="$(curl -s --max-time 3 -o /dev/null -w '%{http_code}' \
      "http://127.0.0.1:${OLLAMA_PORT}/api/tags" 2>/dev/null || echo "ERR")"
  if [[ "${OLL_CODE}" == "200" ]]; then
    MODEL_COUNT="$(curl -s --max-time 3 "http://127.0.0.1:${OLLAMA_PORT}/api/tags" \
        2>/dev/null | jq -r '.models | length' 2>/dev/null || echo "?")"
    ok "GET /api/tags  →  HTTP ${OLL_CODE}  (${MODEL_COUNT} model(s) loaded)"
  else
    fail "GET /api/tags  →  HTTP ${OLL_CODE}  →  sudo systemctl start ollama"
  fi

  # ── Summary ──────────────────────────────────────────────────────────────────
  echo ""
  echo -e "${BOLD}══════════════════════════════════════════${NC}"
  printf  "  ${GREEN}PASS: %d${NC}   ${YELLOW}WARN: %d${NC}   ${RED}FAIL: %d${NC}\n" \
      "${_PASS}" "${_WARN}" "${_FAIL}"
  echo -e "${BOLD}══════════════════════════════════════════${NC}"
  echo ""
  [[ "${_FAIL}" -gt 0 ]] && return 1 || return 0
}

# ══════════════════════════════════════════════════════════════════════════════
# --install
# ══════════════════════════════════════════════════════════════════════════════
cmd_install() {
  say "MortgageDocAI — Install  (idempotent)"

  need_cmd sudo
  need_cmd python3
  need_cmd findmnt
  need_cmd mountpoint

  # ── 1. OS packages ───────────────────────────────────────────────────────────
  say "OS packages"
  sudo apt-get update -y
  # cifs-utils: required for CIFS/SMB mounts (source_loans, nas_*)
  sudo apt-get install -y \
      python3-venv python3-pip \
      cifs-utils \
      jq curl \
      debian-keyring debian-archive-keyring apt-transport-https \
      libnss3-tools

  # ── 2. Directories ───────────────────────────────────────────────────────────
  say "Repo directories"
  sudo mkdir -p "${SCRIPTS_DIR}"
  sudo chown -R "${USER}:${USER}" "${REPO_ROOT}"

  say "Qdrant local SSD dirs  (${QDRANT_DATA})"
  sudo mkdir -p "${QDRANT_DATA}" "${QDRANT_SNAPSHOTS}"
  if sudo chown -R "${USER}:${USER}" /scratch 2>/dev/null; then
    echo "  [OK] /scratch owned by ${USER}"
  else
    warn "/scratch not accessible — provision local SSD mount and re-run"
  fi
  sudo chown -R "${USER}:${USER}" "${QDRANT_DATA}" "${QDRANT_SNAPSHOTS}" 2>/dev/null || true

  # ── 3. Mount validation (does NOT configure mounts) ──────────────────────────
  say "CIFS mount validation"
  for p in "${REQUIRED_MOUNTS[@]}"; do
    if mountpoint -q "${p}"; then
      echo "  [OK] ${p}"
    else
      warn "Not mounted: ${p}  →  check /etc/fstab and run: sudo mount -a"
    fi
  done

  # source_loans contract: must be RO
  if mountpoint -q /mnt/source_loans; then
    SRC_OPTS="$(findmnt -no OPTIONS /mnt/source_loans 2>/dev/null || true)"
    if ! echo "${SRC_OPTS}" | tr ',' '\n' | grep -qx "ro"; then
      warn "source_loans mounted but NOT read-only — contract requires RO"
    fi
  fi

  say "Atomic staging dirs"
  sudo mkdir -p /mnt/nas_apps/nas_chunk/_staging
  sudo mkdir -p /mnt/nas_apps/nas_analyze/_staging

  # ── 4. Deploy scripts and modules ───────────────────────────────────────────
  say "Scripts  (SOURCE_DIR=${SOURCE_DIR})"

  # Python scripts (top-level *.py)
  py_count=0
  for f in "${SOURCE_DIR}"/*.py; do
    [[ -f "${f}" ]] || continue
    cp -f "${f}" "${SCRIPTS_DIR}/"
    chmod +x "${SCRIPTS_DIR}/$(basename "${f}")" || true
    py_count=$((py_count+1))
  done
  echo "  [OK] ${py_count} Python scripts deployed"

  # loan_service module — required for job orchestration (job_worker, adapters_disk, etc.)
  if [[ -d "${SOURCE_DIR}/loan_service" ]]; then
    cp -rf "${SOURCE_DIR}/loan_service" "${SCRIPTS_DIR}/loan_service"
    # a+rX: ensure all files are readable; capital X sets +x on dirs (for traversal)
    # and on files already executable — does NOT chmod +x regular Python modules
    chmod -R a+rX "${SCRIPTS_DIR}/loan_service"
    echo "  [OK] loan_service module deployed"
  else
    warn "loan_service/ not found at ${SOURCE_DIR}/loan_service — job orchestration will fail"
  fi

  # requirements.txt
  if [[ -f "${SOURCE_DIR}/requirements.txt" ]]; then
    cp -f "${SOURCE_DIR}/requirements.txt" "${SCRIPTS_DIR}/requirements.txt"
    echo "  [OK] requirements.txt deployed"
  else
    warn "requirements.txt not found at ${SOURCE_DIR}/requirements.txt"
  fi

  # webui (served at /ui/static by loan_api.py)
  if [[ -d "${SOURCE_DIR}/webui" ]]; then
    cp -rf "${SOURCE_DIR}/webui" "${SCRIPTS_DIR}/webui"
    echo "  [OK] webui deployed"
  else
    warn "webui/ not found at ${SOURCE_DIR}/webui — UI will be unavailable"
  fi

  # ── 5. Python venv ───────────────────────────────────────────────────────────
  say "Python venv  (${VENV_DIR})"
  if [[ ! -f "${VENV_DIR}/bin/activate" ]]; then
    python3 -m venv "${VENV_DIR}"
    echo "  [OK] venv created"
  else
    echo "  [OK] venv already exists (${VENV_DIR}/bin/activate present)"
  fi
  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  python -m pip install -U pip --quiet
  if [[ -f "${SCRIPTS_DIR}/requirements.txt" ]]; then
    pip install -r "${SCRIPTS_DIR}/requirements.txt" --quiet
    echo "  [OK] Python dependencies installed"
  else
    warn "requirements.txt missing from ${SCRIPTS_DIR} — skipping pip install"
  fi

  # ── 6. Caddy (optional, disabled by default) ─────────────────────────────────
  INSTALL_CADDY="${INSTALL_CADDY:-false}"
  if [[ "${INSTALL_CADDY}" != "true" ]]; then
    say "Caddy: skipped  (INSTALL_CADDY=${INSTALL_CADDY})"
    echo "  Tailscale WireGuard provides encrypted transport — Caddy not required."
    echo "  Set INSTALL_CADDY=true to re-enable (e.g. for LAN HTTPS fallback)."
  else
    say "Caddy reverse proxy"
    CADDY_KEYRING=/usr/share/keyrings/caddy-stable-archive-keyring.gpg
    CADDY_SOURCES=/etc/apt/sources.list.d/caddy-stable.list
    if ! command -v caddy >/dev/null 2>&1; then
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
          | sudo gpg --dearmor -o "${CADDY_KEYRING}"
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
          | sudo tee "${CADDY_SOURCES}" >/dev/null
      sudo apt-get update -y
      sudo apt-get install -y caddy
    else
      echo "  [OK] Caddy already installed: $(caddy version)"
    fi
    CADDYFILE_SRC="${REPO_ROOT_SVC}/infra/Caddyfile"
    if [[ -f "${CADDYFILE_SRC}" ]]; then
      sudo cp "${CADDYFILE_SRC}" /etc/caddy/Caddyfile
      sudo caddy validate --config /etc/caddy/Caddyfile
      sudo caddy fmt --overwrite /etc/caddy/Caddyfile
      sudo systemctl enable caddy
      sudo systemctl restart caddy
      sudo caddy trust || warn "caddy trust failed — import CA manually if needed"
    else
      warn "infra/Caddyfile not found at ${CADDYFILE_SRC}"
    fi
  fi

  # ── 6b. Centralized environment files ────────────────────────────────────────
  say "Environment files  (/etc/mortgagedocai/)"
  sudo mkdir -p /etc/mortgagedocai

  # /etc/mortgagedocai/env: create with placeholders, NEVER overwrite if already exists
  if [[ ! -f /etc/mortgagedocai/env ]]; then
    sudo tee /etc/mortgagedocai/env > /dev/null <<'ENVEOF'
MORTGAGEDOCAI_API_KEY=CHANGE_ME
MORTGAGEDOCAI_ALLOWED_TENANTS=peak
MORTGAGEDOCAI_SOURCE_LOANS_ROOT=/mnt/source_loans
ENVEOF
    sudo chown root:root /etc/mortgagedocai/env
    sudo chmod 600 /etc/mortgagedocai/env
    echo "  [OK] /etc/mortgagedocai/env created — edit MORTGAGEDOCAI_API_KEY before production"
  else
    echo "  [OK] /etc/mortgagedocai/env already exists — not overwritten"
  fi

  # /etc/mortgagedocai/env.local: touch only (optional local overrides), NEVER overwrite
  if [[ ! -f /etc/mortgagedocai/env.local ]]; then
    sudo touch /etc/mortgagedocai/env.local
    sudo chown root:root /etc/mortgagedocai/env.local
    sudo chmod 600 /etc/mortgagedocai/env.local
    echo "  [OK] /etc/mortgagedocai/env.local created (empty, for local overrides)"
  else
    echo "  [OK] /etc/mortgagedocai/env.local already exists — not overwritten"
  fi

  # Warn if API key is still placeholder (do NOT print the actual key value)
  _api_key_val="$(sudo grep -oP '(?<=MORTGAGEDOCAI_API_KEY=)\S+' /etc/mortgagedocai/env 2>/dev/null || true)"
  if [[ "${_api_key_val}" == "CHANGE_ME" ]]; then
    warn "MORTGAGEDOCAI_API_KEY is still CHANGE_ME in /etc/mortgagedocai/env — update before production"
  fi

  # ── 7. Systemd service units ─────────────────────────────────────────────────
  say "Systemd service units"
  for svc in mortgagedocai-api.service mortgagedocai-job-worker.service; do
    SVC_SRC="${REPO_ROOT_SVC}/${svc}"
    if [[ -f "${SVC_SRC}" ]]; then
      sudo cp "${SVC_SRC}" "/etc/systemd/system/${svc}"
      echo "  [OK] ${svc} installed"
    else
      warn "${svc} not found at ${SVC_SRC} — skipping"
    fi
  done

  sudo systemctl daemon-reload

  # Tailscale IP patch: keep --host in ExecStart in sync with current Tailscale IPv4.
  # Uses a POSIX regex replacement (--host [^ ]*) — safe regardless of current host value.
  TS_IP="$(tailscale ip -4 2>/dev/null || true)"
  if [[ -n "${TS_IP}" ]]; then
    _svc=/etc/systemd/system/mortgagedocai-api.service
    CURRENT_HOST="$(grep -oP '(?<=--host )\S+' "${_svc}" 2>/dev/null | head -1 || true)"
    if [[ -z "${CURRENT_HOST}" ]]; then
      warn "No --host argument found in ExecStart of ${_svc} — skipping Tailscale bind patch"
    elif [[ "${CURRENT_HOST}" == "${TS_IP}" ]]; then
      echo "  [OK] API already bound to Tailscale IP: ${TS_IP}"
    else
      say "Patching API service bind address: ${CURRENT_HOST} → ${TS_IP}"
      sudo sed -i "s/--host [^ ]*/--host ${TS_IP}/" "${_svc}"
      sudo systemctl daemon-reload
      echo "  [OK] mortgagedocai-api.service will bind to ${TS_IP}:${API_PORT}"
    fi
  else
    warn "Tailscale not connected — API will bind to 127.0.0.1 (loopback fallback)"
    warn "After 'tailscale up', re-run: $(basename "${BASH_SOURCE[0]}") --install"
  fi

  sudo systemctl enable mortgagedocai-api.service mortgagedocai-job-worker.service
  sudo systemctl start  mortgagedocai-api.service mortgagedocai-job-worker.service || \
      warn "Service start failed — check: journalctl -u mortgagedocai-api -n 50"

  # ── 8. Tailscale ─────────────────────────────────────────────────────────────
  say "Tailscale"
  if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
    echo "  [OK] Tailscale installed"
  else
    echo "  [OK] $(tailscale version 2>/dev/null || echo 'Tailscale: version unknown')"
  fi

  sudo systemctl enable tailscaled || true
  sudo systemctl start  tailscaled || true

  if tailscale status --json 2>/dev/null | grep -q '"BackendState":"Running"'; then
    echo "  [OK] Tailscale authenticated: $(tailscale ip -4 2>/dev/null)"
  else
    warn "Tailscale daemon running but NOT authenticated to tailnet"
    warn "Run: sudo tailscale up --advertise-tags=tag:mortgagedocai"
    warn "Then re-run: $(basename "${BASH_SOURCE[0]}") --install  (to patch API bind address)"
  fi

  # ── Done ─────────────────────────────────────────────────────────────────────
  say "Install complete"
  echo "  Repo root:  ${REPO_ROOT}"
  echo "  Scripts:    ${SCRIPTS_DIR}"
  echo "  Venv:       ${VENV_DIR}"
  echo ""
  echo "  Service status:"
  systemctl is-active mortgagedocai-api mortgagedocai-job-worker 2>/dev/null || true
  echo ""
  echo -e "${YELLOW}Remaining steps (if not already done):${NC}"
  echo "  1. Verify CIFS mounts:    sudo mount -a && findmnt /mnt/source_loans"
  echo "  2. Start Qdrant:          cd ${REPO_ROOT} && docker compose up -d"
  echo "  3. Start Ollama:          sudo systemctl start ollama"
  echo "  4. Authenticate TS:       sudo tailscale up --advertise-tags=tag:mortgagedocai"
  echo "  5. Re-run after step 4:   $(basename "${BASH_SOURCE[0]}") --install"
  echo "  6. Verify everything:     $(basename "${BASH_SOURCE[0]}") --verify"
  echo ""
}

# ── Dispatch ───────────────────────────────────────────────────────────────────
case "${1:-}" in
  --install) cmd_install ;;
  --verify)  cmd_verify  ;;
  --help|-h) usage       ;;
  "")        usage; exit 1 ;;
  *)         echo -e "${RED}Unknown option: $1${NC}"; usage; exit 1 ;;
esac
