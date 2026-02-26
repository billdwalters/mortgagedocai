#!/usr/bin/env python3
"""
Step 13 — Retrieval Pack Builder (canonical v1)

- Embeds query using E5 with "query: " prefix + L2 normalization
- Searches Qdrant collection: {tenant}_e5largev2_1024_cosine_v1
- Enforces tenant_id and loan_id filter
- Reconstructs chunk text from nas_chunk chunks.jsonl (authoritative)
- Writes retrieval_pack.json under:
    nas_analyze/tenants/<tenant>/loans/<loan>/retrieve/<run_id>/retrieval_pack.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from qdrant_client import QdrantClient
from qdrant_client.http.models import Filter, FieldCondition, MatchValue, SearchParams

# NOTE: sentence_transformers and torch are imported inside main() so that
# offline env vars (TRANSFORMERS_OFFLINE, HF_HUB_OFFLINE, HF_DATASETS_OFFLINE)
# are set BEFORE any HF/transformers code initializes.

from lib import (
    DEFAULT_TENANT, ContractError,
    NAS_CHUNK, NAS_ANALYZE,
    EMBED_DIM, EMBED_MODEL_NAME,
    qdrant_collection_name,
    normalize_chunk_text,
    atomic_write_json,
    utc_run_id,
    ensure_dir,

)

_DEBUG = False  # set by main() from --debug flag

def _dprint(msg: str) -> None:
    """Print diagnostic message only when --debug is active."""
    if _DEBUG:
        print(msg)

def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Step 13 — Build retrieval pack (v1)")
    ap.add_argument("--tenant-id", default=DEFAULT_TENANT)
    ap.add_argument("--loan-id", required=True)
    ap.add_argument("--query", required=True)
    ap.add_argument("--top-k", type=int, default=12)
    ap.add_argument("--qdrant-url", default="http://localhost:6333")
    ap.add_argument("--embedding-model", default=EMBED_MODEL_NAME)
    ap.add_argument("--embedding-dim", type=int, default=EMBED_DIM)
    ap.add_argument("--embedding-device", choices=["cpu","cuda"], default=None)
    ap.add_argument("--run-id", required=True, help="nas_chunk run_id (required; no latest-fallback)")
    ap.add_argument("--out-dir", default=None, help="override output dir (default: nas_analyze/.../retrieve/<run_id>)")
    ap.add_argument("--out-run-id", default=None, help="Use this run_id for output folder (pipeline run_id)")
    ap.add_argument("--offline-embeddings", action="store_true", default=False,
                    help="Disable HF Hub downloads; only use locally cached model files")
    ap.add_argument("--debug", action="store_true", default=False,
                    help="Enable diagnostic debug output")
    ap.add_argument("--strict", action="store_true", default=False,
                    help="Fail with ContractError if any Qdrant hit chunk_id is missing from chunk_index")
    ap.add_argument("--max-per-file", type=int, default=12,
                    help="Cap chunks per unique file_relpath for retrieval diversity (default: 12). Use 0 or very large value for no cap.")
    ap.add_argument("--required-keywords", action="append", default=None,
                    help="Keyword group (comma-separated, AND within group). "
                         "Can be specified multiple times (OR across groups). "
                         "Chunks matching ANY group are force-included.")

    return ap.parse_args(argv)

def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def _ingest_jsonl_file(path: Path, idx: Dict[str, Dict[str, Any]]) -> Tuple[int, int]:
    """Parse a chunks.jsonl file, add NEW entries to *idx*; return (added, dupes).

    First occurrence of each chunk_id wins; later duplicates are counted but skipped.
    """
    added = 0
    dupes = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        cid = obj.get("chunk_id")
        if not cid:
            continue
        if cid in idx:
            dupes += 1
            continue  # keep first occurrence; later duplicates are ignored
        txt = obj.get("text", "")
        idx[cid] = {
            "chunk_id": cid,
            "text": txt,
            "document_id": obj.get("document_id"),
            "file_relpath": obj.get("file_relpath"),
            "page_start": obj.get("page_start"),
            "page_end": obj.get("page_end"),
            "chunk_index": obj.get("chunk_index"),
            "text_norm_sha256": _sha256_hex(normalize_chunk_text(txt)),
        }
        added += 1
    return added, dupes


def _load_chunk_text_index(run_dir: Path, strict: bool = False) -> Dict[str, Dict[str, Any]]:
    chunks_root = run_dir / "chunks"
    if not chunks_root.exists():
        raise ContractError(f"Missing chunks directory: {chunks_root}")

    idx: Dict[str, Dict[str, Any]] = {}

    # Strategy 1: chunks/<document_id>/chunks.jsonl  (Step11 canonical layout)
    # Use glob("*/chunks.jsonl") — reliable on SMB/NAS mounts where is_dir() can misreport.
    jsonl_files = sorted(chunks_root.glob("*/chunks.jsonl"))
    _dprint(f"[DEBUG] Step13: discovered {len(jsonl_files)} chunks.jsonl files under {chunks_root}")
    for jsonl in jsonl_files:
        try:
            added, dupes = _ingest_jsonl_file(jsonl, idx)
            _dprint(f"[DEBUG] Step13: {jsonl.parent.name}/chunks.jsonl → added={added} dupes={dupes} total={len(idx)}")
        except OSError as exc:
            if strict:
                raise ContractError(f"Step13 strict: cannot read {jsonl}: {exc}") from exc
            _dprint(f"[WARN]  Step13: skipping unreadable {jsonl}: {exc}")

    if idx:
        _dprint(f"[DEBUG] Step13: chunk_index ready — {len(idx)} entries (Strategy 1)")
        return idx

    # Strategy 2: chunks.jsonl files directly under chunks/
    for f in sorted(chunks_root.glob("*.jsonl")):
        added, dupes = _ingest_jsonl_file(f, idx)
        _dprint(f"[DEBUG] Step13: {f.name} → added={added} dupes={dupes} total={len(idx)}")

    if idx:
        return idx

    # Strategy 3: individual .json files under chunks/ or chunks/<subdir>/
    for f in sorted(chunks_root.rglob("*.json")):
        try:
            obj = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if isinstance(obj, dict):
            cid = obj.get("chunk_id")
            if cid:
                txt = obj.get("text", "")
                idx[cid] = {
                    "chunk_id": cid,
                    "text": txt,
                    "document_id": obj.get("document_id"),
                    "page_start": obj.get("page_start"),
                    "page_end": obj.get("page_end"),
                    "chunk_index": obj.get("chunk_index"),
                    "text_norm_sha256": _sha256_hex(normalize_chunk_text(txt)),
                }
        elif isinstance(obj, list):
            for item in obj:
                cid = item.get("chunk_id") if isinstance(item, dict) else None
                if cid:
                    txt = item.get("text", "")
                    idx[cid] = {
                        "chunk_id": cid,
                        "text": txt,
                        "document_id": item.get("document_id"),
                        "page_start": item.get("page_start"),
                        "page_end": item.get("page_end"),
                        "chunk_index": item.get("chunk_index"),
                        "text_norm_sha256": _sha256_hex(normalize_chunk_text(txt)),
                    }

    if idx:
        return idx

    # Nothing found — list what's actually on disk to help diagnose
    contents = sorted([p.name for p in chunks_root.iterdir()][:20])
    raise ContractError(
        f"chunk_index is empty: no chunks.jsonl or .json files found under {chunks_root}. "
        f"Directory listing (first 20): {contents}"
    )

def _self_test() -> None:
    """Smoke test: canonical chunks layout, first-wins dedup, two doc dirs."""
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    try:
        # aaa/chunks.jsonl: c1="from_aaa"
        # bbb/chunks.jsonl: c1="from_bbb" (dup → must lose), c2="from_bbb_c2"
        for doc_name, entries in [
            ("aaa", [("c1", "from_aaa")]),
            ("bbb", [("c1", "from_bbb"), ("c2", "from_bbb_c2")]),
        ]:
            d = tmp / "chunks" / doc_name
            d.mkdir(parents=True)
            lines = "\n".join(
                json.dumps({
                    "chunk_id": cid, "text": text,
                    "document_id": doc_name, "file_relpath": f"{doc_name}.pdf",
                    "page_start": 1, "page_end": 2, "chunk_index": 0,
                })
                for cid, text in entries
            ) + "\n"
            (d / "chunks.jsonl").write_text(lines, encoding="utf-8")
        idx = _load_chunk_text_index(tmp)
        assert len(idx) == 2, \
            f"Expected 2 unique chunk_ids, got {len(idx)}: {list(idx.keys())}"
        assert idx["c1"]["text"] == "from_aaa", \
            f"First-wins failed: c1.text={idx['c1']['text']!r} (expected 'from_aaa')"
        assert idx["c2"]["text"] == "from_bbb_c2"
        print("✓ Step13 _self_test passed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None) -> None:
    global _DEBUG
    args = parse_args(argv)
    _DEBUG = args.debug
    if args.embedding_dim != EMBED_DIM:
        raise ContractError("v1 requires embedding_dim=1024")

    tenant_id = args.tenant_id
    loan_id = args.loan_id
    collection = qdrant_collection_name(tenant_id)

    run_id = args.run_id
    run_dir = NAS_CHUNK / "tenants" / tenant_id / "loans" / loan_id / run_id
    chunk_index = _load_chunk_text_index(run_dir, strict=args.strict)

    # --- Offline env vars MUST be set before importing sentence_transformers / torch ---
    if args.offline_embeddings:
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_DATASETS_OFFLINE"] = "1"

    import torch
    from sentence_transformers import SentenceTransformer

    device = args.embedding_device or ("cuda" if torch.cuda.is_available() else "cpu")

    if args.offline_embeddings:
        # Resolve model to a local cache path; fail immediately if not cached.
        try:
            from huggingface_hub import snapshot_download
            local_path = snapshot_download(args.embedding_model, local_files_only=True)
        except Exception as exc:
            raise ContractError(
                f"Embedding model not found in local cache while offline mode is enabled. "
                f"Pre-cache {args.embedding_model} first."
            ) from exc
        try:
            model = SentenceTransformer(local_path, device=device)
        except Exception as exc:
            raise ContractError(
                f"Embedding model not found in local cache while offline mode is enabled. "
                f"Pre-cache {args.embedding_model} first."
            ) from exc
    else:
        model = SentenceTransformer(args.embedding_model, device=device)
    qtext = "query: " + normalize_chunk_text(args.query)
    qvec = model.encode([qtext], normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)[0].tolist()

    qdrant = QdrantClient(url=args.qdrant_url)
    flt = Filter(must=[
        FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
        FieldCondition(key="loan_id", match=MatchValue(value=loan_id)),
        FieldCondition(key="run_id", match=MatchValue(value=run_id)),
    ])

    hits = qdrant.query_points(
        collection_name=collection,
        query=qvec,
        limit=args.top_k,
        with_payload=True,
        with_vectors=False,
        query_filter=flt,
        search_params=SearchParams(hnsw_ef=128, exact=False),
    ).points

    # --- Defense-in-depth: drop any hit whose payload run_id != requested run_id ---
    filtered_hits = []
    dropped = 0
    for h in hits:
        p = h.payload or {}
        if p.get("run_id") != run_id:
            dropped += 1
            continue
        filtered_hits.append(h)

    if dropped:
        _dprint(f"⚠ Step13: dropped {dropped}/{len(hits)} hits with mismatched payload run_id (defense-in-depth)")

    # --- Retrieval diversity: cap chunks per file_relpath (optional) ---
    max_per_file = args.max_per_file
    if max_per_file > 0:
        before_counts: Dict[str, int] = defaultdict(int)
        for h in filtered_hits:
            p = h.payload or {}
            fpath = p.get("file_relpath") or "<unknown>"
            before_counts[fpath] += 1
        if _DEBUG:
            sorted_before = sorted(before_counts.items(), key=lambda x: -x[1])[:10]
            _dprint(f"[DEBUG] Step13 before max-per-file cap: top 10 file_relpath counts = {dict(sorted_before)}")

        by_file: Dict[str, List[Any]] = defaultdict(list)
        for h in filtered_hits:
            p = h.payload or {}
            fpath = p.get("file_relpath") or "<unknown>"
            by_file[fpath].append(h)
        capped: List[Any] = []
        for fpath, group in by_file.items():
            group_sorted = sorted(group, key=lambda h: (-h.score, (h.payload or {}).get("chunk_id", "")))
            capped.extend(group_sorted[:max_per_file])
        capped.sort(key=lambda h: (-h.score, (h.payload or {}).get("chunk_id", "")))
        filtered_hits = capped[: args.top_k]

        if _DEBUG:
            after_counts: Dict[str, int] = defaultdict(int)
            for h in filtered_hits:
                p = h.payload or {}
                fpath = p.get("file_relpath") or "<unknown>"
                after_counts[fpath] += 1
            sorted_after = sorted(after_counts.items(), key=lambda x: -x[1])[:10]
            _dprint(f"[DEBUG] Step13 after max-per-file cap (top_k={args.top_k}): top 10 file_relpath counts = {dict(sorted_after)}")

    if not filtered_hits:
        raise ContractError(
            f"Step13: 0 Qdrant hits for tenant_id={tenant_id}, loan_id={loan_id}, "
            f"run_id={run_id} (top_k={args.top_k}). "
            f"Verify Step11 was run with this run_id and that Qdrant payloads include run_id."
        )

    items: List[Dict[str, Any]] = []
    missing_ids: List[str] = []
    for h in filtered_hits:
        payload = h.payload or {}

        point_id = str(h.id)  # Qdrant UUID
        chunk_id_val = payload.get("chunk_id", "")  # canonical chunk id (sha256 hex) stored by Step11
        chunk_entry = chunk_index.get(chunk_id_val)
        if chunk_entry is None:
            missing_ids.append(chunk_id_val)
        text = (chunk_entry or {}).get("text", "")

        items.append({
            "point_id": point_id,
            "chunk_id": chunk_id_val,
            "score": float(h.score),
            "text": text,
            "payload": payload,
            "source": {"nas_chunk_run_id": run_dir.name},
        })

    if missing_ids:
        examples = missing_ids[:5]
        if args.strict:
            raise ContractError(
                f"Step13 strict: {len(missing_ids)}/{len(filtered_hits)} chunk_ids missing "
                f"from chunk_index; examples={examples}"
            )
        # Non-strict: drop empty-text items and warn only under --debug
        items = [it for it in items if it.get("text")]
        _dprint(f"⚠ Step13: {len(missing_ids)}/{len(filtered_hits)} chunk_ids not found in chunk_index (dropped)")

    items_with_text = [it for it in items if it.get("text")]
    if filtered_hits and not items_with_text:
        raise ContractError(
            f"Qdrant returned {len(filtered_hits)} hits but no chunk texts were loaded/matched. "
            f"chunk_index has {len(chunk_index)} entries. Check chunks/ layout and chunk_id keys."
        )

    # --- Required-keywords injection: force-include chunks matching keywords ---
    # args.required_keywords is a list of groups (each group is a comma-separated string).
    # A chunk matches if ALL keywords in ANY group are present (AND within group, OR across groups).
    if args.required_keywords:
        kw_groups = []
        for group_str in args.required_keywords:
            kws = [kw.strip().lower() for kw in group_str.split(",") if kw.strip()]
            if kws:
                kw_groups.append(kws)
        if kw_groups:
            existing_cids = {it["chunk_id"] for it in items}
            injected = 0
            for cid, entry in chunk_index.items():
                if cid in existing_cids:
                    continue
                text = (entry.get("text") or "").strip()
                if not text:
                    continue
                text_lower = text.lower()
                if any(all(kw in text_lower for kw in grp) for grp in kw_groups):
                    items.append({
                        "point_id": "",
                        "chunk_id": cid,
                        "score": 0.0,
                        "text": text,
                        "payload": {
                            "chunk_id": cid,
                            "document_id": entry.get("document_id"),
                            "file_relpath": entry.get("file_relpath"),
                            "page_start": entry.get("page_start"),
                            "page_end": entry.get("page_end"),
                            "run_id": run_id,
                        },
                        "source": {"nas_chunk_run_id": run_dir.name,
                                   "injection": "required_keywords"},
                    })
                    injected += 1
            if injected:
                _dprint(f"[DEBUG] Step13: injected {injected} chunk(s) via "
                        f"--required-keywords {kw_groups}")

    items.sort(key=lambda d: (-d["score"], d.get("chunk_id","")))

    out_run = args.out_run_id or utc_run_id()
    if args.out_dir:
        out_root = Path(args.out_dir)
    else:
        out_root = NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id / "retrieve" / out_run
    ensure_dir(out_root)

    dropped_total = dropped + len(missing_ids)
    retrieval_pack_meta = {
        "dropped_chunk_ids_count": dropped_total,
        "dropped_chunk_ids": missing_ids[:50],
    }
    pack = {
        "schema_version": "retrieval_pack.v1",
        "tenant_id": tenant_id,
        "loan_id": loan_id,
        "run_id": out_run,
        "retrieval_pack_meta": retrieval_pack_meta,
        "qdrant": {"url": args.qdrant_url, "collection": collection, "distance": "cosine", "dim": EMBED_DIM},
        "embedding": {"model": args.embedding_model, "dim": EMBED_DIM, "normalize": True, "prefix": "query: ", "device": device},
        "query": {"text": args.query, "text_normalized": normalize_chunk_text(args.query), "text_norm_sha256": _sha256_hex(normalize_chunk_text(args.query))},
        "top_k": args.top_k,
        "retrieved_chunks": items,
    }
    atomic_write_json(out_root / "retrieval_pack.json", pack)
    print(f"✓ Step13 wrote retrieval pack: {out_root / 'retrieval_pack.json'}")

if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        _self_test()
    else:
        main()
