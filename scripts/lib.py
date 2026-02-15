#!/usr/bin/env python3
"""
MortgageDocAI v1 - Canonical lib.py

This module is the stable contract surface for all runtime scripts in /scripts.
Authoritative contract: MortgageDocAI_CONTRACT.md

Exports expected by:
  - run_loan_pipeline.py
  - step10_intake.py
  - step11_process.py
  - step12_analyze.py
  - step13_build_retrieval_pack.py
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import shlex
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

# -----------------------------
# Locked constants / mounts
# -----------------------------
DEFAULT_TENANT = "peak"



SOURCE_MOUNT = Path(os.environ.get("SOURCE_MOUNT", "/mnt/source_loans"))
SOURCE_SUBDIR = "5-Borrowers TBD"

NAS_INGEST = Path("/mnt/nas_apps/nas_ingest")
NAS_CHUNK = Path("/mnt/nas_apps/nas_chunk")
NAS_ANALYZE = Path("/mnt/nas_apps/nas_analyze")

# Qdrant defaults (scripts may override via CLI)
DEFAULT_QDRANT_URL = "http://localhost:6333"

# Embedding defaults (locked v1)
EMBED_MODEL_NAME = "intfloat/e5-large-v2"
EMBED_DIM = 1024

class ContractError(RuntimeError):
    """Raised when MortgageDocAI contract or preflight checks are violated."""
    pass

# -----------------------------
# Time helpers
# -----------------------------
def utc_run_id() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H%M%SZ")

def utc_timestamp_compact() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%d_%H%M%S")

# -----------------------------
# Hashing helpers
# -----------------------------
def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(chunk_size), b""):
            h.update(b)
    return h.hexdigest()

# -----------------------------
# Chunk normalization (LOCKED order)
# -----------------------------
_blank_lines_re = re.compile(r"\n{3,}")

def normalize_chunk_text(text: str) -> str:
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = "\n".join([ln.rstrip() for ln in t.split("\n")])
    t = _blank_lines_re.sub("\n\n", t)
    return t.strip()

def chunk_text_hash(text: str) -> str:
    return hashlib.sha256(normalize_chunk_text(text).encode("utf-8")).hexdigest()

def chunk_id(document_id: str, page_start: int, page_end: int, chunk_index: int, chunk_text: str) -> str:
    cth = chunk_text_hash(chunk_text)
    s = f"{document_id}:{page_start}-{page_end}:{chunk_index}:{cth}"
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

# -----------------------------
# Qdrant naming (LOCKED)
# -----------------------------
def qdrant_collection_name(tenant_id: str) -> str:
    return f"{tenant_id}_e5largev2_1024_cosine_v1"

# -----------------------------
# FS helpers
# -----------------------------
def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)

def safe_mkdir(path: Path) -> None:
    ensure_dir(path)

def atomic_write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))

def atomic_write_json(path: Path, obj: Any) -> None:
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(str(tmp), str(path))

def atomic_rename_dir(staging_dir: Path, final_dir: Path) -> None:
    ensure_dir(final_dir.parent)
    os.replace(str(staging_dir), str(final_dir))

# -----------------------------
# Mount contract preflight
# -----------------------------
def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise ContractError(f"Command failed: {' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout.strip()

def _is_mountpoint(p: Path) -> bool:
    return subprocess.run(["mountpoint", "-q", str(p)]).returncode == 0

def _findmnt_source(p: Path) -> str:
    return _run(["findmnt", "-no", "SOURCE", str(p)])

def _findmnt_options(p: Path) -> str:
    """Return mount options for *p*, preferring backing (non-autofs) row when multiple exist."""
    out = _run(["findmnt", "-R", "-no", "FSTYPE,OPTIONS", str(p)])
    fallback = ""
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)  # "cifs ro,relatime,..." or "autofs rw,..."
        if len(parts) == 2:
            fstype, opts = parts[0], parts[1]
        elif len(parts) == 1:
            # Shouldn't happen with FSTYPE,OPTIONS, but be safe
            fstype, opts = parts[0], ""
        else:
            continue
        if not fallback:
            fallback = opts
        if fstype != "autofs":
            return opts.strip()
    return fallback.strip()

def _mount_entries_for_target(target: Path) -> list[tuple[str, set[str]]]:
    """
    Return list of (fstype, options_set) for mount entries whose TARGET == target.
    This is used to correctly handle autofs/systemd.automount wrappers where findmnt
    may report autofs OPTIONS (rw) even though the backing filesystem (e.g., cifs) is ro.
    """
    out = _run(["mount"])
    entries: list[tuple[str, set[str]]] = []
    needle = f" on {str(target)} type "
    for line in out.splitlines():
        if needle not in line:
            continue
        # Example:
        # //server/share on /mnt/source_loans type cifs (ro,...)
        # systemd-1 on /mnt/source_loans type autofs (rw,...)
        try:
            # split at " type "
            left, right = line.split(" type ", 1)
            # right begins with fstype + " (" + opts + ")"
            fstype = right.split(" ", 1)[0].strip()
            # extract "(...)" options
            m = re.search(r"\(([^)]*)\)", line)
            opts = set()
            if m:
                opts = set([o.strip() for o in m.group(1).split(",") if o.strip()])
            entries.append((fstype, opts))
        except Exception:
            # If parsing fails, ignore this line rather than breaking preflight.
            continue
    return entries

def _findmnt_fstype(p: Path) -> str:
    """Return fstype for *p*, preferring backing (non-autofs) row when multiple exist."""
    out = _run(["findmnt", "-R", "-no", "FSTYPE", str(p)])
    fallback = ""
    for line in out.splitlines():
        fs = line.strip()
        if not fs:
            continue
        if not fallback:
            fallback = fs
        if fs != "autofs":
            return fs
    return fallback

def _opts_has_ro(opts) -> bool:
    """Check for 'ro' whether *opts* is a set, list, or comma-separated string."""
    if isinstance(opts, (set, list, tuple)):
        return "ro" in opts
    return "ro" in str(opts).split(",")

def _source_mount_is_effectively_ro() -> bool:
    """
    Determine whether SOURCE_MOUNT is effectively read-only.
    Returns True/False only — never raises.
    Handles systemd automount (autofs) layered over a backing mount (e.g., cifs ro).

    Logic:
    1) If fstype != autofs -> standard check: "ro" in findmnt OPTIONS.
    2) If fstype == autofs:
       a) Force materialization so the backing mount appears.
       b) Re-read mount entries for SOURCE_MOUNT.
       c) Filter out autofs entries.
       d) If no backing entries -> return False.
       e) If any backing entry has "ro" -> return True, else False.
    """
    fstype = _findmnt_fstype(SOURCE_MOUNT)

    # --- Non-autofs: standard RO check ---
    if fstype != "autofs":
        opts = _findmnt_options(SOURCE_MOUNT)
        return _opts_has_ro(opts)

    # --- Autofs path: force materialization, then inspect backing mount ---
    try:
        os.listdir(str(SOURCE_MOUNT))
    except OSError:
        pass  # best-effort touch; backing mount may still appear

    entries = _mount_entries_for_target(SOURCE_MOUNT)
    backing = [(fst, opts) for (fst, opts) in entries if fst != "autofs"]

    if not backing:
        return False

    return any(_opts_has_ro(opts) for (_, opts) in backing)

def preflight_mount_contract() -> None:
    # Source mount exists and is RO
    if not _is_mountpoint(SOURCE_MOUNT):
        raise ContractError(f"SOURCE_MOUNT not mounted: {SOURCE_MOUNT}")

    if not _source_mount_is_effectively_ro():
        fstype = _findmnt_fstype(SOURCE_MOUNT)
        if fstype == "autofs":
            # Re-read backing entries for diagnostic message
            entries = _mount_entries_for_target(SOURCE_MOUNT)
            backing = [(fst, opts) for (fst, opts) in entries if fst != "autofs"]
            if not backing:
                raise ContractError(
                    f"SOURCE_MOUNT autofs present but backing mount not materialized: {SOURCE_MOUNT}"
                )
            backing_info = ", ".join(
                f"{fst}({','.join(sorted(opts))})" for fst, opts in backing
            )
            raise ContractError(
                f"SOURCE_MOUNT backing mount must be ro. backing=[{backing_info}]"
            )
        else:
            opts = _findmnt_options(SOURCE_MOUNT)
            raise ContractError(f"SOURCE_MOUNT must be ro. findmnt_OPTIONS={opts}")

    # NAS mounts exist, writable, and not rootfs
    root_src = _findmnt_source(Path("/"))
    for p in (NAS_INGEST, NAS_CHUNK, NAS_ANALYZE):
        if not _is_mountpoint(p):
            raise ContractError(f"{p} is not a mountpoint")
        if not os.access(p, os.W_OK):
            raise ContractError(f"{p} is not writable")
        if _findmnt_source(p) == root_src:
            raise ContractError(f"{p} resolves to root filesystem SOURCE={root_src}")

    # Ensure staging dirs exist (atomic publish contract)
    ensure_dir(NAS_CHUNK / "_staging")
    ensure_dir(NAS_ANALYZE / "_staging")
    # NOTE: some findmnt builds don't report SOURCE for subdirectories; do not query subdir.
    _ = _findmnt_source(NAS_CHUNK)
    _ = _findmnt_source(NAS_ANALYZE)

# -----------------------------
# Source path validation (LOCKED)
# -----------------------------
def validate_source_path(source_path: str) -> Path:
    p = Path(source_path)
    if not p.exists():
        raise ContractError(f"Source path does not exist: {p}")
    subroot = SOURCE_MOUNT / SOURCE_SUBDIR
    try:
        p.resolve().relative_to(subroot.resolve())
    except Exception:
        raise ContractError(f"source-path must be under {subroot}: {p}")
    return p

# -----------------------------
# Run context (contracted)
# -----------------------------
@dataclasses.dataclass(frozen=True)
class RunContext:
    tenant_id: str
    loan_id: str
    run_id: str

    @property
    def ingest_loan_root(self) -> Path:
        return NAS_INGEST / "tenants" / self.tenant_id / "loans" / self.loan_id

    @property
    def chunk_staging_run_root(self) -> Path:
        return NAS_CHUNK / "_staging" / "tenants" / self.tenant_id / "loans" / self.loan_id / self.run_id

    @property
    def chunk_final_run_root(self) -> Path:
        return NAS_CHUNK / "tenants" / self.tenant_id / "loans" / self.loan_id / self.run_id

    @property
    def analyze_staging_run_root(self) -> Path:
        return NAS_ANALYZE / "_staging" / "tenants" / self.tenant_id / "loans" / self.loan_id / self.run_id

    @property
    def analyze_final_run_root(self) -> Path:
        return NAS_ANALYZE / "tenants" / self.tenant_id / "loans" / self.loan_id / self.run_id

def build_run_context(tenant_id: str, loan_id: str, run_id: Optional[str] = None) -> RunContext:
    return RunContext(tenant_id=tenant_id, loan_id=loan_id, run_id=(run_id or utc_run_id()))
