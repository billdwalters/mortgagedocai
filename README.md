MortgageDocAI Pipeline (repo + pipeline/ package)

Install:
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -U pip
  pip install -e .

Run:
  pipeline --help

Qdrant (localhost only) via docker compose:
  docker compose up -d

Qdrant snapshots to TrueNAS:
  qdrant-snapshot snapshot --tenant-id peak --nas-root /mnt/nas_apps/nas_chunk/_meta/qdrant_snapshots
