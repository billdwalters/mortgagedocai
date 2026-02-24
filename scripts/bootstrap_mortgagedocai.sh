#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="/opt/mortgagedocai"
SCRIPTS_DIR="${REPO_ROOT}/scripts"
VENV_DIR="${REPO_ROOT}/venv"

QDRANT_DATA="/scratch/qdrant/data"
QDRANT_SNAPSHOTS="/scratch/qdrant/snapshots"

SOURCE_DIR="${SOURCE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# One level up from scripts/ — repo root where *.service and infra/ live
REPO_ROOT_SVC="$(cd "${SOURCE_DIR}/.." && pwd)"

say(){ echo -e "\n==> $*\n"; }
warn(){ echo -e "\n[WARN] $*\n" >&2; }
die(){ echo -e "\n[ERROR] $*\n" >&2; exit 1; }

need_cmd(){ command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"; }

need_cmd sudo
need_cmd python3
need_cmd findmnt
need_cmd mountpoint

# ── OS prerequisites ──────────────────────────────────────────────────────────
say "Installing OS prerequisites"
sudo apt-get update -y
sudo apt-get install -y \
    python3-venv python3-pip nfs-common jq \
    curl debian-keyring debian-archive-keyring apt-transport-https \
    libnss3-tools

# ── Directories ───────────────────────────────────────────────────────────────
say "Creating repo directories"
sudo mkdir -p "${SCRIPTS_DIR}"
sudo chown -R "${USER}:${USER}" "${REPO_ROOT}"

say "Creating Qdrant local SSD dirs"
sudo mkdir -p "${QDRANT_DATA}" "${QDRANT_SNAPSHOTS}"
sudo chown -R "${USER}:${USER}" /scratch || true
sudo chown -R "${USER}:${USER}" "${QDRANT_DATA}" "${QDRANT_SNAPSHOTS}"

# ── Mount validation (does NOT configure mounts) ──────────────────────────────
say "Validating mounts (does NOT configure mounts)"
for p in /mnt/source_loans /mnt/nas_apps/nas_ingest /mnt/nas_apps/nas_chunk /mnt/nas_apps/nas_analyze; do
  if ! mountpoint -q "$p"; then warn "Mount not present: $p"; fi
done

say "Ensuring atomic publish staging dirs exist"
sudo mkdir -p /mnt/nas_apps/nas_chunk/_staging
sudo mkdir -p /mnt/nas_apps/nas_analyze/_staging

# ── Scripts ───────────────────────────────────────────────────────────────────
say "Copying scripts from SOURCE_DIR=${SOURCE_DIR}"
cp -f "${SOURCE_DIR}"/*.py "${SCRIPTS_DIR}/"
chmod +x "${SCRIPTS_DIR}"/*.py || true
cp -f "${SOURCE_DIR}/requirements.txt" "${SCRIPTS_DIR}/requirements.txt"

# Copy webui (served by loan_api.py at /ui/static)
if [[ -d "${SOURCE_DIR}/webui" ]]; then
    cp -rf "${SOURCE_DIR}/webui" "${SCRIPTS_DIR}/webui"
fi

# ── Python venv ───────────────────────────────────────────────────────────────
say "Creating venv at ${VENV_DIR}"
python3 -m venv "${VENV_DIR}"
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
python -m pip install -U pip
pip install -r "${SCRIPTS_DIR}/requirements.txt"

# ── Caddy reverse proxy ───────────────────────────────────────────────────────
say "Installing Caddy (stable) from official apt repository"
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
    say "Caddy already installed: $(caddy version)"
fi

say "Deploying Caddyfile"
CADDYFILE_SRC="${REPO_ROOT_SVC}/infra/Caddyfile"
if [[ -f "${CADDYFILE_SRC}" ]]; then
    sudo cp "${CADDYFILE_SRC}" /etc/caddy/Caddyfile
    sudo caddy validate --config /etc/caddy/Caddyfile
    sudo caddy fmt --overwrite /etc/caddy/Caddyfile
else
    warn "infra/Caddyfile not found at ${CADDYFILE_SRC} — skipping Caddy config"
fi

say "Enabling and starting Caddy"
sudo systemctl enable caddy
sudo systemctl restart caddy

say "Installing Caddy internal CA root certificate (for browser trust)"
sudo caddy trust || warn "caddy trust failed — you may need to import the CA manually"

# ── systemd service units ─────────────────────────────────────────────────────
say "Installing systemd service units"
for svc in mortgagedocai-api.service mortgagedocai-job-worker.service; do
    SVC_SRC="${REPO_ROOT_SVC}/${svc}"
    if [[ -f "${SVC_SRC}" ]]; then
        sudo cp "${SVC_SRC}" "/etc/systemd/system/${svc}"
        say "  installed ${svc}"
    else
        warn "${svc} not found at ${SVC_SRC} — skipping"
    fi
done

sudo systemctl daemon-reload
sudo systemctl enable mortgagedocai-api.service mortgagedocai-job-worker.service
sudo systemctl start mortgagedocai-api.service mortgagedocai-job-worker.service || \
    warn "Service start failed — check 'journalctl -u mortgagedocai-api' for details"

# ── Tailscale ─────────────────────────────────────────────────────────────────
say "Installing Tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
    warn "Tailscale installed but NOT connected."
    warn "Run: sudo tailscale up"
    warn "Then authenticate via the URL shown."
else
    say "Tailscale already installed: $(tailscale version 2>/dev/null || echo 'version unknown')"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
say "Bootstrap complete"
echo "Repo:       ${REPO_ROOT}"
echo "Scripts:    ${SCRIPTS_DIR}"
echo "Venv:       ${VENV_DIR}"
echo ""
echo "Service status:"
systemctl is-active caddy mortgagedocai-api mortgagedocai-job-worker 2>/dev/null || true
echo ""
echo "Next steps (if not already done):"
echo "  1. Verify NFS mounts: /mnt/source_loans, /mnt/nas_apps/nas_*"
echo "  2. Start Qdrant:  docker compose up -d  (from ${REPO_ROOT})"
echo "  3. Start Ollama:  sudo systemctl start ollama"
echo "  4. Connect Tailscale: sudo tailscale up"
echo "  5. Verify HTTPS:  curl -sk --resolve mortgagedocai.local:443:127.0.0.1 https://mortgagedocai.local/health"
