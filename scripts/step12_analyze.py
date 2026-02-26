#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from lib import (
    DEFAULT_TENANT,
    ContractError,
    NAS_ANALYZE,
    preflight_mount_contract,
    build_run_context,
    atomic_write_json,
    atomic_write_text,
    atomic_rename_dir,
    ensure_dir,
    sha256_file,
)

_DEBUG = False  # set by main() from --debug flag

def _dprint(msg: str) -> None:
    """Print diagnostic message only when --debug is active."""
    if _DEBUG:
        print(msg)

STEP13_CANDIDATES = [
    "/opt/mortgagedocai/scripts/step13_build_retrieval_pack.py",
    "step13_build_retrieval_pack.py",
]

def _find_step13() -> Optional[str]:
    for p in STEP13_CANDIDATES:
        if os.path.exists(p):
            return p
    return None

def _find_retrieval_pack_for_run(tenant_id: str, loan_id: str, run_id: str) -> Optional[Path]:
    rp = (
        NAS_ANALYZE
        / "tenants" / tenant_id / "loans" / loan_id
        / "retrieve" / run_id
        / "retrieval_pack.json"
    )
    return rp if rp.exists() else None

def _find_latest_retrieval_pack(tenant_id: str, loan_id: str) -> Optional[Path]:
    base = NAS_ANALYZE / "tenants" / tenant_id / "loans" / loan_id / "retrieve"
    if not base.exists():
        return None
    run_dirs = [d for d in base.iterdir() if d.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}T\d{6}Z$", d.name)]
    if not run_dirs:
        return None
    latest = sorted(run_dirs, key=lambda p: p.name)[-1]
    rp = latest / "retrieval_pack.json"
    return rp if rp.exists() else None

def _run_step13(tenant_id: str, loan_id: str, run_id: str, query: str) -> Path:
    step13 = _find_step13()
    if not step13:
        raise ContractError("Step13 script not found; cannot auto-retrieve")

    cmd = [
        "python3", step13,
        "--tenant-id", tenant_id,
        "--loan-id", loan_id,
        "--run-id", run_id,
        "--query", query,
        "--out-run-id", run_id,
    ]
    subprocess.run(cmd, check=True)

    rp = _find_retrieval_pack_for_run(tenant_id, loan_id, run_id)
    if not rp:
        raise ContractError("Step13 did not produce retrieval_pack.json at expected run_id path")
    return rp

def _build_evidence_block(retrieved_chunks: List[Dict[str, Any]], max_chars_total: int = 12000) -> str:
    """
    Evidence-only context for Ollama.
    Format: [chunk_id=<sha256>] <text>
    Bounded to avoid huge prompts.
    """
    parts: List[str] = []
    used = 0
    for ch in retrieved_chunks:
        chunk_id = ch.get("chunk_id") or ch.get("payload", {}).get("chunk_id") or ""
        text = (ch.get("text") or "").strip()
        if not chunk_id or not text:
            continue
        entry = f"[chunk_id={chunk_id}] {text}\n"
        if used + len(entry) > max_chars_total:
            break
        parts.append(entry)
        used += len(entry)
    return "\n".join(parts).strip()

def _ollama_generate(
    ollama_url: str,
    model: str,
    prompt: str,
    temperature: float = 0.0,
    max_tokens: int = 800,
    timeout: int = 600,
) -> str:
    url = ollama_url.rstrip("/") + "/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": float(temperature),
            "num_predict": int(max_tokens),
        },
    }
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data.get("response", "")

def _evidence_only_prompt(question: str, evidence_block: str) -> str:
    return f"""You are MortgageDocAI.

You must answer ONLY using the EVIDENCE provided.
If the answer is not explicitly supported by the evidence, say: "Not found in provided documents."

Rules:
- Every factual statement must cite one or more chunk_id values from the evidence.
- Do not use outside knowledge.
- Output must be VALID JSON ONLY (no markdown, no extra text).

Question:
{question}

Evidence:
{evidence_block}

Return JSON in this schema:
{{
  "answer": "string",
  "citations": [{{"chunk_id":"string","quote":"string"}}],
  "confidence": number
}}
"""

_UW_CATEGORIES = {"Verification", "Assets", "Income", "Credit", "Property", "Title", "Insurance", "Compliance", "Other"}
_UW_TIMINGS = {"Prior to Closing", "Prior to Docs", "Post Closing", "Unknown"}

def _uw_conditions_prompt(question: str, evidence_block: str) -> str:
    return f"""You are MortgageDocAI.

You must answer ONLY using the EVIDENCE provided.
If no underwriting conditions are found, return an empty conditions list.

Rules:
- Extract each distinct underwriting condition as a separate item.
- Every condition must cite one or more chunk_id values from the evidence.
- Do not use outside knowledge.
- Output must be VALID JSON ONLY (no markdown, no extra text).

Question:
{question}

Evidence:
{evidence_block}

Return JSON in this schema:
{{
  "conditions": [
    {{
      "description": "short actionable condition text",
      "category": "Verification|Assets|Income|Credit|Property|Title|Insurance|Compliance|Other",
      "timing": "Prior to Closing|Prior to Docs|Post Closing|Unknown",
      "citations": [{{"chunk_id":"string","quote":"short quote from chunk"}}]
    }}
  ],
  "confidence": number
}}
"""


def _normalize_uw_conditions(llm_obj: Dict[str, Any], allowed_chunk_ids: set,
                             chunk_meta: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Normalize and filter the LLM output for uw_conditions profile.
    Returns a dict with 'conditions', 'confidence', and optionally 'parse_warning'.
    chunk_meta: mapping of chunk_id -> {document_id, file_relpath, page_start, page_end}
    """
    conditions_raw = llm_obj.get("conditions", [])

    # If conditions arrived as a JSON string, try to decode it
    if isinstance(conditions_raw, str):
        try:
            parsed = json.loads(conditions_raw)
            if isinstance(parsed, list):
                conditions_raw = parsed
            else:
                conditions_raw = []
        except Exception:
            conditions_raw = []
            llm_obj["_parse_warning_conditions"] = "conditions was a non-parseable string"

    if not isinstance(conditions_raw, list):
        conditions_raw = []

    # Filter and normalize each condition
    filtered: List[Dict[str, Any]] = []
    for cond in conditions_raw:
        if not isinstance(cond, dict):
            continue
        desc = str(cond.get("description", "") or "").strip()
        if not desc:
            continue

        # Normalize category and timing
        cat = str(cond.get("category", "Other") or "Other").strip()
        if cat not in _UW_CATEGORIES:
            cat = "Other"
        timing = str(cond.get("timing", "Unknown") or "Unknown").strip()
        if timing not in _UW_TIMINGS:
            timing = "Unknown"

        # Filter citations: keep only those with allowed chunk_ids
        cits_raw = cond.get("citations", []) or []
        if isinstance(cits_raw, str):
            try:
                cits_raw = json.loads(cits_raw)
            except Exception:
                cits_raw = []
        if not isinstance(cits_raw, list):
            cits_raw = []

        cits = []
        for c in cits_raw:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("chunk_id", "") or "").strip()
            if cid in allowed_chunk_ids:
                quote = str(c.get("quote", "") or "").strip()[:200]
                cits.append({"chunk_id": cid, "quote": quote})

        # Drop conditions with zero valid citations (non-negotiable)
        if not cits:
            continue

        # Build source document metadata from chunk_meta
        source_docs: List[Dict[str, Any]] = []
        if chunk_meta:
            # Group by (document_id, file_relpath)
            doc_groups: Dict[str, Dict[str, Any]] = {}
            for c_item in cits:
                cid = c_item["chunk_id"]
                meta = chunk_meta.get(cid)
                if not meta:
                    continue
                doc_id = meta.get("document_id") or ""
                fpath = meta.get("file_relpath") or ""
                key = f"{doc_id}|{fpath}"
                if key not in doc_groups:
                    doc_groups[key] = {
                        "document_id": doc_id,
                        "file_relpath": fpath,
                        "page_start": meta.get("page_start"),
                        "page_end": meta.get("page_end"),
                        "chunk_ids": [],
                    }
                doc_groups[key]["chunk_ids"].append(cid)
                # Widen page range
                ps = meta.get("page_start")
                pe = meta.get("page_end")
                if ps is not None:
                    cur_ps = doc_groups[key]["page_start"]
                    if cur_ps is None or ps < cur_ps:
                        doc_groups[key]["page_start"] = ps
                if pe is not None:
                    cur_pe = doc_groups[key]["page_end"]
                    if cur_pe is None or pe > cur_pe:
                        doc_groups[key]["page_end"] = pe
            source_docs = list(doc_groups.values())

        filtered.append({
            "description": desc,
            "category": cat,
            "timing": timing,
            "citations": cits,
            "source": {"documents": source_docs},
        })

    # Confidence
    default_conf = 0.5 if llm_obj.get("_truncation_repaired") else 0.3
    confidence = float(llm_obj.get("confidence", default_conf) or default_conf)
    if not filtered:
        confidence = min(confidence, 0.3)

    return {
        "conditions": filtered,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# uw_conditions deduplication helpers
# ---------------------------------------------------------------------------

# Longer phrases before shorter prefixes so they match first.
_UW_DEDUPE_BOILERPLATE = [
    "please provide", "please submit", "please upload", "please send",
    "provide", "submit", "upload", "send",
    "obtain", "verify", "furnish",
]


def _make_dedupe_key(description: str) -> str:
    """Normalise a condition description to a stable deduplication key."""
    key = description.lower()
    key = re.sub(r"[^\w\s]", " ", key)       # punctuation -> space
    key = re.sub(r"\s+", " ", key).strip()    # collapse whitespace
    for verb in _UW_DEDUPE_BOILERPLATE:
        if key.startswith(verb + " "):
            key = key[len(verb) + 1:]
            break
    key = key.rstrip(".")                     # trailing period safety net
    return key.strip()


def _token_jaccard(a: str, b: str) -> float:
    """Token-level Jaccard similarity. Returns 0.0 for empty inputs."""
    set_a = set(a.split())
    set_b = set(b.split())
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def _dedup_conditions(conditions: List[Dict]) -> "tuple[List[Dict], Dict]":
    """Deduplicate uw_conditions deterministically via Union-Find + token Jaccard.

    Groups conditions with identical dedupe_key OR _token_jaccard >= 0.92.
    Union-Find root is always the lowest original index (first encountered).
    Output sorted by (category, timing, description).
    Returns (deduped_list, stats_dict).
    """
    n = len(conditions)
    if n == 0:
        return [], {"raw_count": 0, "deduped_count": 0, "removed_count": 0, "top_dup_keys": []}

    parent = list(range(n))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]    # path halving -- deterministic
            x = parent[x]
        return x

    def _union(x: int, y: int) -> None:
        rx, ry = _find(x), _find(y)
        if rx != ry:
            if rx < ry:
                parent[ry] = rx             # lower index always becomes root
            else:
                parent[rx] = ry

    keys = [_make_dedupe_key(c.get("description", "")) for c in conditions]

    for i in range(n):
        for j in range(i + 1, n):
            if _find(i) == _find(j):
                continue
            if keys[i] == keys[j] or _token_jaccard(keys[i], keys[j]) >= 0.92:
                _union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        root = _find(i)
        groups.setdefault(root, []).append(i)

    merged: List[Dict] = []

    for root in sorted(groups.keys()):
        members = groups[root]

        if len(members) == 1:
            merged.append(conditions[members[0]])
            continue

        # Winner: longest description after boilerplate strip; first-index tie-break
        winner_idx = members[0]
        for idx in members[1:]:
            if len(_make_dedupe_key(conditions[idx].get("description", ""))) > \
               len(_make_dedupe_key(conditions[winner_idx].get("description", ""))):
                winner_idx = idx
        winner = conditions[winner_idx]

        # Citations: union by chunk_id; prefer non-empty quote
        merged_cits: Dict[str, str] = {}
        for idx in members:
            for cit in conditions[idx].get("citations", []) or []:
                cid = cit.get("chunk_id", "")
                if not cid:
                    continue
                quote = cit.get("quote", "") or ""
                if cid not in merged_cits:
                    merged_cits[cid] = quote
                elif not merged_cits[cid] and quote:
                    merged_cits[cid] = quote
        citations_out = [{"chunk_id": cid, "quote": q} for cid, q in merged_cits.items()]

        # Category: most frequent; first-encountered tie-break
        cat_order: List[str] = []
        cat_counter: Counter = Counter()
        for idx in members:
            cat = conditions[idx].get("category", "Other")
            cat_counter[cat] += 1
            if cat not in cat_order:
                cat_order.append(cat)
        best = max(cat_counter.values())
        category = next(c for c in cat_order if cat_counter[c] == best)

        # Timing: most frequent; first-encountered tie-break
        tim_order: List[str] = []
        tim_counter: Counter = Counter()
        for idx in members:
            tim = conditions[idx].get("timing", "Unknown")
            tim_counter[tim] += 1
            if tim not in tim_order:
                tim_order.append(tim)
        best_t = max(tim_counter.values())
        timing = next(t for t in tim_order if tim_counter[t] == best_t)

        merged.append({
            "description": winner.get("description", ""),
            "category": category,
            "timing": timing,
            "citations": citations_out,
            "source": winner.get("source", {"documents": []}),
        })

    merged.sort(key=lambda c: (
        c.get("category", ""), c.get("timing", ""), c.get("description", "")
    ))

    raw_count = n
    deduped_count = len(merged)
    removed_count = raw_count - deduped_count
    dup_groups = sorted(
        [(len(m), keys[r]) for r, m in groups.items() if len(m) >= 2],
        key=lambda x: -x[0]
    )
    return merged, {
        "raw_count": raw_count,
        "deduped_count": deduped_count,
        "removed_count": removed_count,
        "top_dup_keys": [dk for _, dk in dup_groups[:5]],
    }


# ---------------------------------------------------------------------------
# _postprocess_conditions — v2 dedup with fixed sort order + source merge
# ---------------------------------------------------------------------------

# Fixed priority for output ordering (lower index = earlier in output).
_CATEGORY_ORDER: Dict[str, int] = {
    "Verification": 0, "Assets": 1, "Income": 2, "Credit": 3,
    "Property": 4, "Title": 5, "Insurance": 6, "Compliance": 7, "Other": 8,
}

_TIMING_ORDER: Dict[str, int] = {
    "Prior to Docs": 0, "Prior to Closing": 1, "Post Closing": 2, "Unknown": 3,
}


def _postprocess_conditions(
    conditions: List[Dict],
    debug: bool = False,
) -> List[Dict]:
    """Deterministic dedup + normalisation of uw_conditions (replaces _dedup_conditions at call site).

    Enhancements over _dedup_conditions:
      - Fixed category/timing sort order (not alphabetical).
      - source.documents merged across group members by (document_id, file_relpath, page_start, page_end).
      - debug parameter: emits a single structured diagnostic line via _dprint when True.
    Caller is responsible for confidence calibration using len(input) - len(output).
    """
    n = len(conditions)
    if n == 0:
        if debug:
            _dprint("UW_COND_DEDUPE raw=0 merged=0 removed=0")
        return []

    parent = list(range(n))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]    # path halving — deterministic
            x = parent[x]
        return x

    def _union(x: int, y: int) -> None:
        rx, ry = _find(x), _find(y)
        if rx != ry:
            if rx < ry:
                parent[ry] = rx
            else:
                parent[rx] = ry

    keys = [_make_dedupe_key(c.get("description", "")) for c in conditions]

    for i in range(n):
        for j in range(i + 1, n):
            if _find(i) == _find(j):
                continue
            if keys[i] == keys[j] or _token_jaccard(keys[i], keys[j]) >= 0.92:
                _union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        root = _find(i)
        groups.setdefault(root, []).append(i)

    merged: List[Dict] = []

    for root in sorted(groups.keys()):
        members = groups[root]

        if len(members) == 1:
            merged.append(conditions[members[0]])
            continue

        # Description: longest dedupe key; first-index tie-break
        winner_idx = members[0]
        for idx in members[1:]:
            if len(keys[idx]) > len(keys[winner_idx]):
                winner_idx = idx

        # Citations: union by chunk_id; prefer non-empty quote; stable by first appearance
        merged_cits: Dict[str, str] = {}
        for idx in members:
            for cit in (conditions[idx].get("citations") or []):
                cid = cit.get("chunk_id", "")
                if not cid:
                    continue
                quote = cit.get("quote", "") or ""
                if cid not in merged_cits:
                    merged_cits[cid] = quote
                elif not merged_cits[cid] and quote:
                    merged_cits[cid] = quote
        citations_out = [{"chunk_id": cid, "quote": q} for cid, q in merged_cits.items()]

        # Category: majority vote; first-encountered tie-break
        cat_order: List[str] = []
        cat_counter: Counter = Counter()
        for idx in members:
            cat = conditions[idx].get("category", "Other")
            cat_counter[cat] += 1
            if cat not in cat_order:
                cat_order.append(cat)
        best_cat = max(cat_counter.values())
        category = next(c for c in cat_order if cat_counter[c] == best_cat)

        # Timing: majority vote; first-encountered tie-break
        tim_order: List[str] = []
        tim_counter: Counter = Counter()
        for idx in members:
            tim = conditions[idx].get("timing", "Unknown")
            tim_counter[tim] += 1
            if tim not in tim_order:
                tim_order.append(tim)
        best_tim = max(tim_counter.values())
        timing = next(t for t in tim_order if tim_counter[t] == best_tim)

        # source.documents: union by (document_id, file_relpath, page_start, page_end);
        # stable ordering by first appearance across members.
        doc_seen: Dict[tuple, Dict] = {}
        for idx in members:
            src = conditions[idx].get("source") or {}
            for doc in (src.get("documents") or []):
                key_tuple = (
                    doc.get("document_id") or "",
                    doc.get("file_relpath") or "",
                    doc.get("page_start"),
                    doc.get("page_end"),
                )
                if key_tuple not in doc_seen:
                    doc_seen[key_tuple] = doc
        docs_out = list(doc_seen.values())

        merged.append({
            "description": conditions[winner_idx].get("description", ""),
            "category": category,
            "timing": timing,
            "citations": citations_out,
            "source": {"documents": docs_out},
        })

    # Fixed sort: category priority, timing priority, description casefold
    merged.sort(key=lambda c: (
        _CATEGORY_ORDER.get(c.get("category", "Other"), 8),
        _TIMING_ORDER.get(c.get("timing", "Unknown"), 3),
        (c.get("description") or "").casefold(),
    ))

    if debug:
        _dprint(f"UW_COND_DEDUPE raw={n} merged={len(merged)} removed={n - len(merged)}")

    return merged


_INCOME_FREQUENCIES_CANONICAL = {"monthly", "annual", "one-time", "unknown"}

# ---------------------------------------------------------------------------
# Deterministic PITIA extraction from evidence text (regex-based, 3-tier)
# ---------------------------------------------------------------------------
# Primary: "Estimated Total Monthly Payment" then $ amount on same line or
# separated by minimal whitespace only (no other $ amounts in between).
PITIA_PATTERN_PRIMARY = re.compile(
    r"""
    Estimated\s+Total\s+Monthly\s+Payment
    [\s\S]{0,40}
    \$\s*
    (?P<amount>
        \d{1,3}(?:,\d{3})*(?:\.\d{2})?
        |
        \d+(?:\.\d{2})?
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
# Multiline: label may wrap across a line break, but amount must be the
# very next token after the label (allow only whitespace/newline, no other
# text or dollar signs in between).
PITIA_PATTERN_MULTILINE = re.compile(
    r"""
    Estimated\s+Total\s*
    \n?\s*
    Monthly\s+Payment
    [\s\S]{0,40}
    \$\s*
    (?P<amount>
        \d{1,3}(?:,\d{3})*(?:\.\d{2})?
        |
        \d+(?:\.\d{2})?
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
# No-dollar: amount immediately follows label without $ sign.
PITIA_PATTERN_NO_DOLLAR = re.compile(
    r"""
    Estimated\s+Total\s+Monthly\s+Payment
    \s{0,20}
    (?P<amount>
        \d{1,3}(?:,\d{3})*\.\d{2}
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PITIA_PATTERNS = [PITIA_PATTERN_PRIMARY, PITIA_PATTERN_MULTILINE, PITIA_PATTERN_NO_DOLLAR]

# Pattern to detect Principal & Interest amounts for deprioritization.
_PI_AMOUNT_RE = re.compile(
    r"(?:Monthly\s+)?Principal\s*&\s*Interest\s*\$?\s*"
    r"(?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)",
    re.IGNORECASE,
)

# PITI-total patterns: prefer this over "Estimated Total Monthly Payment" for DTI
# (housing = P&I + taxes + insurance; full total may include HOA/assessments).
# Single amount after "Principal, Interest, Taxes (, and|&) Insurance" or "Total PITI".
PITIA_PITI_LINE = re.compile(
    r"""
    (?:Total\s+)?
    (?:PITI|Principal\s*,?\s*Interest\s*,?\s*Taxes\s*,?\s*(?:,\s*)?(?:and|&)\s*Insurance)
    \s{0,20}
    \$?\s*
    (?P<amount>
        \d{1,3}(?:,\d{3})*(?:\.\d{2})?
        |
        \d+(?:\.\d{2})?
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
PITIA_PITI_NO_DOLLAR = re.compile(
    r"""
    (?:Total\s+)?
    (?:PITI|Principal\s*,?\s*Interest\s*,?\s*Taxes\s*,?\s*(?:,\s*)?(?:and|&)\s*Insurance)
    \s{0,20}
    (?P<amount>\d{1,3}(?:,\d{3})*\.\d{2})
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PITIA_PITI_PATTERNS = [PITIA_PITI_LINE, PITIA_PITI_NO_DOLLAR]

# ---------------------------------------------------------------------------
# File-relpath doc-type scoring, loan ID coherence, internal consistency
# ---------------------------------------------------------------------------
_CD_RELPATH_RE = re.compile(
    r"(?:Closing[\s_-]*Disclosure|\bCD)\.pdf",
    re.IGNORECASE,
)
_LE_RELPATH_RE = re.compile(
    r"(?:Loan[\s_-]*Estimate|\bLE)\.pdf",
    re.IGNORECASE,
)
_LOAN_ID_RE = re.compile(r"\b(\d{6}R\d+)\b")
_ESCROW_RE = re.compile(
    r"(?:Estimated\s+)?(?:Escrow|Taxes\s*(?:,?\s*(?:and|&)\s*)?Insurance)"
    r"\s{0,20}\$?\s*"
    r"(?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Deterministic liabilities-total extraction (regex-based)
# ---------------------------------------------------------------------------
# Primary: "Total Monthly Payments $X" (common on 1003 assets/liabilities section)
LIAB_PATTERN_TOTAL_MONTHLY_PAYMENTS = re.compile(
    r"""
    Total\s+Monthly\s+Payments
    [^\$0-9]{0,50}?
    \$\s*
    (?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)
    """,
    re.IGNORECASE | re.VERBOSE,
)
# Generic: "Total Monthly Debt/Debts/Obligations" or "Monthly Debt/Obligations Total"
LIAB_PATTERN_MONTHLY_DEBT_GENERIC = re.compile(
    r"""
    (?:Total\s+Monthly\s+(?:Debt|Debts|Obligations)
     |
     Monthly\s+(?:Debt|Debts|Obligations)\s+Total
     |
     Total\s+Debt\s+Payment)
    [^\$0-9]{0,50}?
    \$\s*
    (?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)
    """,
    re.IGNORECASE | re.VERBOSE,
)
# Credit report style: "Total Monthly Payment $X" (only used if chunk is credit-related)
LIAB_PATTERN_CREDIT_REPORT_TOTAL = re.compile(
    r"""
    Total\s+Monthly\s+Payment
    [^\$0-9]{0,50}?
    \$\s*
    (?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_LIAB_EXCLUSION_PHRASES = frozenset({
    "cash to close", "closing costs", "total closing costs",
    "total of payments", "finance charge", "amount financed",
    "estimated cash to close",
})
_LIAB_CD_PENALTY_PHRASES = frozenset({"loan costs", "prepaids", "escrow"})

# 1003-specific: individual liability item in OCR table layout
# Matches: payment_amount \n months_or_R \n balance_amount
# e.g.: "566.00\n52\n28,904.00" → payment=566.00, months=52, balance=28904.00
# Also handles revolving credit: "25.00\nR\n1,029.00" (R = revolving)
_1003_LIABILITY_ITEM_RE = re.compile(
    r'(?P<payment>\d{1,3}(?:,\d{3})*\.\d{2})\s*\n\s*(?P<months>\d{1,3}|R)\s*\n\s*(?P<balance>\d{1,3}(?:,\d{3})*\.\d{2})',
    re.MULTILINE,
)

# Junk-folder penalty: chunks from Junk/ paths are superseded documents
_JUNK_RELPATH_PENALTY = -10

# ---------------------------------------------------------------------------
# Deterministic income-total extraction (regex-based)
# ---------------------------------------------------------------------------

# AUS / DU Findings: "Total Monthly Income $X" or "Qualifying Income $X"
INCOME_PATTERN_AUS_TOTAL = re.compile(
    r"""
    (?:Total\s+Monthly\s+Income|Total\s+Qualifying\s+Income)
    [^\$0-9]{0,50}?
    \$\s*
    (?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)
    """,
    re.IGNORECASE | re.VERBOSE,
)
INCOME_PATTERN_QUALIFYING = re.compile(
    r"""
    (?:Qualifying\s+Income|Income\s+Used\s+to\s+Qualify)
    [^\$0-9]{0,50}?
    \$\s*
    (?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# 1003 form: "Gross Monthly Income $X" or "Monthly Income $X"
INCOME_PATTERN_GROSS_MONTHLY = re.compile(
    r"""
    (?:(?:Total\s+)?Gross\s+Monthly\s+Income|Monthly\s+Income)
    [^\$0-9]{0,50}?
    \$\s*
    (?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# P&L / business income: "Net Income  NNNN.NN" or "Total Income  NNNN.NN"
# P&L from XLSX extraction has tab-separated format: "Net Income\t79863.5"
# Note: \d[\d,]* captures the full integer part including numbers >999 without commas
INCOME_PATTERN_PL_NET = re.compile(
    r"""
    Net\s+Income
    \s+
    (?P<amount>\d[\d,]*(?:\.\d+)?)
    """,
    re.IGNORECASE | re.VERBOSE,
)
INCOME_PATTERN_PL_TOTAL = re.compile(
    r"""
    Total\s+Income
    \s+
    (?P<amount>\d[\d,]*(?:\.\d+)?)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Exclusion phrases for income (avoid capturing housing/closing-related amounts)
_INCOME_EXCLUSION_PHRASES = frozenset({
    "estimated total monthly payment", "principal & interest",
    "escrow", "closing cost", "cash to close",
})

# Income sanity bounds
_INCOME_MIN = 100      # $100/month minimum
_INCOME_MAX = 500000   # $500,000/month maximum (annual P&L can be up to $6M)


def _score_doc_type_from_relpath(file_relpath: str) -> tuple:
    """Return (relpath_score, doc_type_label) from payload.file_relpath."""
    if not file_relpath:
        return (0, "other")
    if _CD_RELPATH_RE.search(file_relpath):
        return (20, "Closing Disclosure")
    if _LE_RELPATH_RE.search(file_relpath):
        return (10, "Loan Estimate")
    return (0, "other")


def _extract_loan_id_tokens(text: str) -> set:
    """Extract all loan ID tokens matching NNNNNNRN+ from text."""
    return set(_LOAN_ID_RE.findall(text))


def _check_internal_consistency(
    candidate_value: float,
    pi_amounts: set,
    chunk_text: str,
    tolerance: float = 1.00,
) -> bool:
    """Return True if candidate_value ≈ pi_amount + escrow_amount within tolerance."""
    escrow_amounts: set = set()
    for m in _ESCROW_RE.finditer(chunk_text):
        try:
            escrow_amounts.add(float(m.group("amount").replace(",", "")))
        except ValueError:
            pass
    if not pi_amounts or not escrow_amounts:
        return False
    for pi in pi_amounts:
        for esc in escrow_amounts:
            if abs(candidate_value - (pi + esc)) <= tolerance:
                return True
    return False


def _extract_monthly_liabilities_total_from_retrieval_pack(
    retrieval_pack: dict,
    allowed_chunk_ids: set,
) -> Dict[str, Any]:
    """
    Deterministic extraction of total monthly liabilities from retrieval pack.

    Scans retrieved_chunks for "Total Monthly Payments", "Total Monthly Debt/Obligations",
    or credit-report-style "Total Monthly Payment" patterns. Avoids closing-cost totals
    and other non-liability totals via exclusion heuristics.

    Returns {"value": float|None, "citations": [{"chunk_id": str, "quote": str}]}.
    """
    _PATTERNS_ALWAYS = [
        ("TOTAL_MONTHLY_PAYMENTS", LIAB_PATTERN_TOTAL_MONTHLY_PAYMENTS),
        ("MONTHLY_DEBT_GENERIC", LIAB_PATTERN_MONTHLY_DEBT_GENERIC),
    ]
    _PATTERN_CREDIT = ("CREDIT_REPORT_TOTAL", LIAB_PATTERN_CREDIT_REPORT_TOTAL)

    candidates: List[Dict[str, Any]] = []
    chunks = retrieval_pack.get("retrieved_chunks", []) or []

    for ch in chunks:
        cid = ch.get("chunk_id") or ch.get("payload", {}).get("chunk_id") or ""
        if not cid or cid not in allowed_chunk_ids:
            continue
        text = (ch.get("text") or "").strip()
        if not text:
            continue

        file_relpath = (ch.get("payload") or {}).get("file_relpath", "")
        text_lower = text.lower()
        file_lower = file_relpath.lower()

        # Credit-related heuristic
        is_credit_related = (
            "credit report" in text_lower
            or "liabilities" in text_lower
            or "credit" in file_lower
            or "report" in file_lower
        )

        # Determine which patterns to try
        patterns_to_try = list(_PATTERNS_ALWAYS)
        if is_credit_related:
            patterns_to_try.append(_PATTERN_CREDIT)

        for pat_name, pat in patterns_to_try:
            for m in pat.finditer(text):
                amt_str = m.group("amount")
                try:
                    value = float(amt_str.replace(",", ""))
                except ValueError:
                    continue
                if value <= 0:
                    continue

                # Exclusion check: ±200 chars around match
                exc_start = max(0, m.start() - 200)
                exc_end = min(len(text), m.end() + 200)
                context_lower = text[exc_start:exc_end].lower()
                excluded = False
                for phrase in _LIAB_EXCLUSION_PHRASES:
                    if phrase in context_lower:
                        excluded = True
                        break
                if excluded:
                    continue

                # Scoring
                score = 0
                if "uniform residential loan application" in text_lower or "form 1003" in text_lower:
                    score += 3
                if "credit report" in file_lower or "1003" in file_lower:
                    score += 2
                if pat_name == "TOTAL_MONTHLY_PAYMENTS":
                    score += 1
                # CD penalty: if chunk has many closing-disclosure terms
                cd_term_count = sum(1 for p in _LIAB_CD_PENALTY_PHRASES if p in text_lower)
                if cd_term_count >= 2:
                    score -= 5
                # Junk-folder penalty
                if "/Junk/" in file_relpath or file_relpath.startswith("Junk/"):
                    score += _JUNK_RELPATH_PENALTY

                has_decimal = "." in amt_str

                # Quote: ±100 chars, truncate to 200
                quote_start = max(0, m.start() - 100)
                quote_end = min(len(text), m.end() + 100)
                quote = text[quote_start:quote_end].strip()
                if len(quote) > 200:
                    quote = quote[:200].strip()

                candidates.append({
                    "value": value,
                    "chunk_id": cid,
                    "quote": quote,
                    "score": score,
                    "has_decimal": has_decimal,
                    "pattern": pat_name,
                    "file_relpath": file_relpath,
                })

    # Stage B: 1003-specific individual liability summation fallback.
    # Activates only when Stage A regex found no candidates.
    # 1003 OCR tables have "Total Monthly Payments $" as a column header
    # without an adjacent dollar value — the amounts are in separate rows.
    if not candidates:
        for ch in chunks:
            cid = ch.get("chunk_id") or ch.get("payload", {}).get("chunk_id") or ""
            if not cid or cid not in allowed_chunk_ids:
                continue
            text = (ch.get("text") or "").strip()
            if not text:
                continue
            text_lower = text.lower()
            file_relpath = (ch.get("payload") or {}).get("file_relpath", "")

            # Must be a 1003 form chunk with the liabilities page
            is_1003 = ("form 1003" in text_lower
                       or "uniform residential loan application" in text_lower)
            has_liab_section = "total monthly payments" in text_lower
            if not is_1003 or not has_liab_section:
                continue

            # Extract individual liability items: payment/months/balance triplets
            items: List[Dict[str, Any]] = []
            for m in _1003_LIABILITY_ITEM_RE.finditer(text):
                try:
                    pmt = float(m.group("payment").replace(",", ""))
                    months_raw = m.group("months")
                    # "R" = revolving credit, treat as ongoing (months=999)
                    months = 999 if months_raw.upper() == "R" else int(months_raw)
                    bal = float(m.group("balance").replace(",", ""))
                except (ValueError, TypeError):
                    continue
                # Sanity: payment <= balance, months > 0, payment reasonable
                if pmt > 0 and months > 0 and pmt <= bal:
                    items.append({"payment": pmt, "months": months, "balance": bal})

            if items:
                total = round(sum(it["payment"] for it in items), 2)
                # Build quote
                quote_parts = ["1003 liabilities: " + str(len(items)) + " items summed"]
                for it in items:
                    quote_parts.append(
                        f"  ${it['payment']:,.2f}/mo ({it['months']}mo, bal=${it['balance']:,.2f})")
                quote = "; ".join(quote_parts)
                if len(quote) > 200:
                    quote = quote[:200].strip()

                score = 3  # 1003 form bonus
                # Junk-folder penalty
                if "/Junk/" in file_relpath or file_relpath.startswith("Junk/"):
                    score += _JUNK_RELPATH_PENALTY
                candidates.append({
                    "value": total,
                    "chunk_id": cid,
                    "quote": quote,
                    "score": score,
                    "has_decimal": True,
                    "pattern": "1003_ITEM_SUM",
                    "file_relpath": file_relpath,
                })
                _dprint(f"[DEBUG] 1003 liability item summation: ${total:,.2f} "
                        f"from {len(items)} items in chunk {cid[:16]}...")

    if not candidates:
        _dprint("[DEBUG] deterministic liabilities total: no candidates found")
        return {"value": None, "citations": []}

    # Pick best: highest score, then first encountered (stable sort)
    candidates.sort(key=lambda c: -c["score"])
    best = candidates[0]
    _dprint(f"[DEBUG] deterministic liabilities total: ${best['value']:,.2f} "
            f"(score={best['score']}, pattern={best['pattern']}, "
            f"file_relpath={best['file_relpath']}, "
            f"chunk={best['chunk_id'][:16]}..., "
            f"{len(candidates)} candidates)")
    for i, c in enumerate(candidates):
        _dprint(f"[DEBUG]   liab_candidate[{i}]: ${c['value']:,.2f} "
                f"score={c['score']} pattern={c['pattern']} "
                f"file={c['file_relpath']!r} "
                f"decimal={c['has_decimal']}")
    return {
        "value": best["value"],
        "citations": [{"chunk_id": best["chunk_id"], "quote": best["quote"]}],
    }


def _extract_proposed_pitia_from_retrieval_pack(
    retrieval_pack: dict,
    allowed_chunk_ids: set,
) -> Dict[str, Any]:
    """
    Deterministic PITIA extraction from retrieval pack evidence text.

    Scans retrieved_chunks for PITIA-total patterns:
    - "Estimated Total Monthly Payment" $X
    - "Principal, Interest, Taxes and Insurance" / "Total PITI" $X

    Selection gating:
      If ANY candidates have is_piti_total=True (matched by PITIA/ETMP patterns),
      select ONLY among those candidates. Non-PITIA candidates are discarded.

    Additive scoring (applied after gating):
      - File-relpath doc-type: Closing Disclosure (+20) > Loan Estimate (+10)
      - Text-based doc-type: "closing disclosure" (+3) > "loan estimate" (+2)
        > "projected payments" (+1)
      - Internal consistency: P&I + Escrow ≈ Total (+30)
      - Loan ID coherence: matching dominant loan ID (+5)
      - Decimal precision (+1)

    Tiebreakers:
      1. Highest additive score
      2. Non-P&I preferred over P&I (total > P&I-only)
      3. First encountered (Qdrant relevance order)

    Returns {"value": float|None, "citations": [{"chunk_id": str, "quote": str}]}.
    """
    candidates: List[Dict[str, Any]] = []
    chunks = retrieval_pack.get("retrieved_chunks", []) or []

    for ch in chunks:
        cid = ch.get("chunk_id") or ch.get("payload", {}).get("chunk_id") or ""
        if not cid or cid not in allowed_chunk_ids:
            continue
        text = (ch.get("text") or "").strip()
        if not text:
            continue

        # Detect P&I amounts in this chunk for deprioritization
        pi_amounts: set = set()
        for pi_m in _PI_AMOUNT_RE.finditer(text):
            try:
                pi_amounts.add(float(pi_m.group("amount").replace(",", "")))
            except ValueError:
                pass

        # --- File-relpath-based scoring (dominant signal) ---
        file_relpath = (ch.get("payload") or {}).get("file_relpath", "")
        relpath_score, relpath_doc_type = _score_doc_type_from_relpath(file_relpath)

        # --- Text-based scoring (secondary signal) ---
        text_lower = text.lower()
        text_score = 0
        text_doc_type = "other"
        if "closing disclosure" in text_lower:
            text_score += 3
            text_doc_type = "Closing Disclosure"
        if "loan estimate" in text_lower:
            text_score += 2
            if text_doc_type == "other":
                text_doc_type = "Loan Estimate"
        if "projected payments" in text_lower:
            text_score += 1

        score = relpath_score + text_score
        doc_type = relpath_doc_type if relpath_doc_type != "other" else text_doc_type

        # --- Loan ID tokens for coherence ---
        loan_ids = _extract_loan_id_tokens(text)

        def add_candidates(matches: List[Any], is_piti_total: bool) -> None:
            for m in matches:
                amt_str = m.group("amount")
                try:
                    value = float(amt_str.replace(",", ""))
                except ValueError:
                    continue
                if value <= 0:
                    continue
                is_pi = value in pi_amounts
                has_decimal = "." in amt_str
                is_consistent = _check_internal_consistency(
                    value, pi_amounts, text,
                )
                quote_start = max(0, m.start() - 100)
                quote_end = min(len(text), m.end() + 100)
                quote = text[quote_start:quote_end].strip()
                if len(quote) > 200:
                    quote = quote[:200].strip()
                candidates.append({
                    "value": value,
                    "chunk_id": cid,
                    "quote": quote,
                    "score": score,
                    "has_decimal": has_decimal,
                    "is_pi": is_pi,
                    "doc_type": doc_type,
                    "is_piti_total": is_piti_total,
                    "loan_ids": loan_ids,
                    "file_relpath": file_relpath,
                    "is_consistent": is_consistent,
                })

        # PITI-total patterns (Principal, Interest, Taxes and Insurance)
        piti_matches = []
        for pat in _PITIA_PITI_PATTERNS:
            for m in pat.finditer(text):
                piti_matches.append(m)
        add_candidates(piti_matches, is_piti_total=True)

        # Estimated Total Monthly Payment patterns — also PITIA total
        etmp_matches = []
        for pat in _PITIA_PATTERNS:
            for m in pat.finditer(text):
                etmp_matches.append(m)
        add_candidates(etmp_matches, is_piti_total=True)

    if not candidates:
        return {"value": None, "citations": []}

    # --- Selection gating: prefer PITIA-total candidates ---
    pitia_total_candidates = [c for c in candidates if c["is_piti_total"]]
    if pitia_total_candidates:
        _dprint(f"[DEBUG] PITIA gating: {len(pitia_total_candidates)} PITIA-total "
                f"candidates out of {len(candidates)} total — selecting from PITIA only")
        candidates = pitia_total_candidates
    else:
        _dprint(f"[DEBUG] PITIA gating: no PITIA-total candidates, "
                f"using all {len(candidates)} candidates")

    # --- Loan ID coherence: find the dominant loan ID ---
    all_loan_ids: Counter = Counter()
    cd_loan_ids: set = set()
    for c in candidates:
        c_ids = c.get("loan_ids", set())
        for lid in c_ids:
            all_loan_ids[lid] += 1
        if c["doc_type"] == "Closing Disclosure" and c_ids:
            cd_loan_ids.update(c_ids)

    dominant_loan_id: Optional[str] = None
    if cd_loan_ids:
        if len(cd_loan_ids) == 1:
            dominant_loan_id = next(iter(cd_loan_ids))
        else:
            dominant_loan_id = max(
                cd_loan_ids, key=lambda lid: all_loan_ids.get(lid, 0),
            )
    elif all_loan_ids:
        dominant_loan_id = all_loan_ids.most_common(1)[0][0]

    # --- Additive scoring (applied after gating) ---
    for c in candidates:
        # Loan ID match bonus
        c_ids = c.get("loan_ids", set())
        if dominant_loan_id and c_ids:
            c["loan_id_match"] = dominant_loan_id in c_ids
        else:
            c["loan_id_match"] = True  # no loan ID info -> neutral
        if c["loan_id_match"]:
            c["score"] += 5

        # Consistency bonus: P&I + Escrow ≈ Total
        if c["is_consistent"]:
            c["score"] += 30

        # Decimal bonus
        if c["has_decimal"]:
            c["score"] += 1

        # Junk-folder penalty
        frp = c.get("file_relpath", "")
        if "/Junk/" in frp or frp.startswith("Junk/"):
            c["score"] += _JUNK_RELPATH_PENALTY

    # Pick best candidate: highest score, then non-P&I, then stable order
    candidates.sort(key=lambda c: (
        -c["score"],                # 1. Additive score (file +20/+10, consistency +30, lid +5, decimal +1)
        c["is_pi"],                 # 2. Non-P&I preferred (total > P&I-only)
        # 3. Stable sort → first encountered (Qdrant relevance order)
    ))
    best = candidates[0]
    _dprint(f"[DEBUG] deterministic PITIA: ${best['value']:,.2f} "
            f"(score={best['score']}, doc_type={best['doc_type']}, "
            f"file_relpath={best.get('file_relpath', '?')}, "
            f"is_piti_total={best['is_piti_total']}, is_pi={best['is_pi']}, "
            f"is_consistent={best.get('is_consistent', '?')}, "
            f"loan_id_match={best.get('loan_id_match', '?')}, "
            f"chunk={best['chunk_id'][:16]}..., "
            f"{len(candidates)} candidates)")
    for i, c in enumerate(candidates):
        _dprint(f"[DEBUG]   candidate[{i}]: ${c['value']:,.2f} "
                f"score={c['score']} doc_type={c['doc_type']} "
                f"file={c.get('file_relpath', '?')!r} "
                f"consistent={c.get('is_consistent', '?')} "
                f"lid_match={c.get('loan_id_match', '?')} "
                f"piti_total={c['is_piti_total']} pi={c['is_pi']} "
                f"decimal={c['has_decimal']}")
    return {
        "value": best["value"],
        "citations": [{"chunk_id": best["chunk_id"], "quote": best["quote"]}],
    }


# ---------------------------------------------------------------------------
# Deterministic income total extractor
# ---------------------------------------------------------------------------

def _score_income_source(text_lower: str, file_relpath: str) -> tuple:
    """Return (score, source_label) for income document authority.

    source_label: "AUS", "1003", "P&L", or "other".
    """
    score = 0
    source = "other"
    file_lower = file_relpath.lower()

    # AUS / DU Findings indicators (highest authority)
    if "c-aus" in file_lower or "desktop underwriter" in file_lower:
        score += 50
        source = "AUS"
    elif ("desktop underwriter" in text_lower
          or "du findings" in text_lower
          or "automated underwriting" in text_lower):
        score += 10
        source = "AUS"

    # 1003 form indicators
    if ("uniform residential loan application" in text_lower
            or "form 1003" in text_lower):
        if source == "other":
            source = "1003"
        score += 5

    # P&L / business income indicators
    if "profit and loss" in text_lower or "p&l" in file_lower:
        if source == "other":
            source = "P&L"
        score += 3

    return (score, source)


def _extract_monthly_income_total_from_retrieval_pack(
    retrieval_pack: dict,
    allowed_chunk_ids: set,
) -> Dict[str, Any]:
    """
    Deterministic extraction of total monthly income from retrieval pack.

    Strategy: AUS-first, 1003 second, P&L fallback.

    Stage A: Scan for label-adjacent patterns:
      - "Total Monthly Income $X" / "Qualifying Income $X" (AUS / DU)
      - "Gross Monthly Income $X" / "Monthly Income $X" (1003 form)

    Stage B: P&L business income (self-employed borrowers):
      - "Net Income NNNN.NN" from Profit & Loss statements
      - "Total Income NNNN.NN" as fallback
      - P&L values are period totals, not monthly — divide by period months.
      - Default period: 12 months (annual). 9-month YTD detected by keywords.

    Returns {"value": float|None, "citations": [...], "source": "AUS"|"1003"|"P&L"|None}.
    """
    _STAGE_A_PATTERNS = [
        ("AUS_TOTAL", INCOME_PATTERN_AUS_TOTAL, 5),
        ("QUALIFYING", INCOME_PATTERN_QUALIFYING, 4),
        ("GROSS_MONTHLY", INCOME_PATTERN_GROSS_MONTHLY, 2),
    ]
    # Stage B P&L patterns: lower priority, scored separately
    _STAGE_B_PATTERNS = [
        ("PL_NET_INCOME", INCOME_PATTERN_PL_NET, 1),
        ("PL_TOTAL_INCOME", INCOME_PATTERN_PL_TOTAL, 0),
    ]

    candidates: List[Dict[str, Any]] = []
    chunks = retrieval_pack.get("retrieved_chunks", []) or []

    for ch in chunks:
        cid = ch.get("chunk_id") or ch.get("payload", {}).get("chunk_id") or ""
        if not cid or cid not in allowed_chunk_ids:
            continue
        text = (ch.get("text") or "").strip()
        if not text:
            continue

        file_relpath = (ch.get("payload") or {}).get("file_relpath", "")
        text_lower = text.lower()

        doc_score, doc_source = _score_income_source(text_lower, file_relpath)

        # --- Stage A: AUS / 1003 monthly income patterns ---
        for pat_name, pat, pat_bonus in _STAGE_A_PATTERNS:
            for m in pat.finditer(text):
                amt_str = m.group("amount")
                try:
                    value = float(amt_str.replace(",", ""))
                except ValueError:
                    continue
                if value < _INCOME_MIN or value > _INCOME_MAX:
                    continue

                # Exclusion check: ±200 chars
                exc_start = max(0, m.start() - 200)
                exc_end = min(len(text), m.end() + 200)
                context_lower = text[exc_start:exc_end].lower()
                excluded = any(p in context_lower for p in _INCOME_EXCLUSION_PHRASES)
                if excluded:
                    continue

                score = doc_score + pat_bonus
                has_decimal = "." in amt_str
                if has_decimal:
                    score += 1
                # Junk penalty
                if "/Junk/" in file_relpath or file_relpath.startswith("Junk/"):
                    score += _JUNK_RELPATH_PENALTY

                # Quote
                quote_start = max(0, m.start() - 100)
                quote_end = min(len(text), m.end() + 100)
                quote = text[quote_start:quote_end].strip()
                if len(quote) > 200:
                    quote = quote[:200].strip()

                candidates.append({
                    "value": value,
                    "chunk_id": cid,
                    "quote": quote,
                    "score": score,
                    "has_decimal": has_decimal,
                    "pattern": pat_name,
                    "file_relpath": file_relpath,
                    "source": doc_source if doc_source != "other" else (
                        "AUS" if "AUS" in pat_name else "1003"),
                    "is_monthly": True,
                })

        # --- Stage B: P&L business income (period total → monthly) ---
        is_pl = ("profit and loss" in text_lower or "p&l" in file_relpath.lower())
        if is_pl:
            # Detect period length from text
            # Common: "January 1 - September 7, 2020" → ~8.2 months
            # Default assumption: 12 months (annual P&L)
            period_months = 12.0
            period_match = re.search(
                r'(January|February|March|April|May|June|July|August|September|October|November|December)'
                r'\s+\d{1,2}\s*[-–]\s*'
                r'(January|February|March|April|May|June|July|August|September|October|November|December)'
                r'\s+\d{1,2}',
                text, re.IGNORECASE,
            )
            if period_match:
                _MONTH_NUM = {
                    "january": 1, "february": 2, "march": 3, "april": 4,
                    "may": 5, "june": 6, "july": 7, "august": 8,
                    "september": 9, "october": 10, "november": 11, "december": 12,
                }
                start_month = _MONTH_NUM.get(period_match.group(1).lower(), 1)
                end_month = _MONTH_NUM.get(period_match.group(2).lower(), 12)
                if end_month >= start_month:
                    period_months = max(1.0, float(end_month - start_month + 1))
                else:
                    period_months = max(1.0, float(12 - start_month + end_month + 1))
                _dprint(f"[DEBUG] P&L period detected: {period_match.group(1)}-"
                        f"{period_match.group(2)} → {period_months} months")

            for pat_name, pat, pat_bonus in _STAGE_B_PATTERNS:
                for m in pat.finditer(text):
                    amt_str = m.group("amount")
                    try:
                        period_value = float(amt_str.replace(",", ""))
                    except ValueError:
                        continue
                    if period_value <= 0:
                        continue
                    # Convert period total to monthly
                    monthly_value = round(period_value / period_months, 2)
                    if monthly_value < _INCOME_MIN or monthly_value > _INCOME_MAX:
                        continue

                    score = doc_score + pat_bonus
                    has_decimal = "." in amt_str
                    if has_decimal:
                        score += 1
                    # Junk penalty
                    if "/Junk/" in file_relpath or file_relpath.startswith("Junk/"):
                        score += _JUNK_RELPATH_PENALTY

                    # Quote
                    quote_start = max(0, m.start() - 100)
                    quote_end = min(len(text), m.end() + 100)
                    quote = text[quote_start:quote_end].strip()
                    if len(quote) > 200:
                        quote = quote[:200].strip()

                    candidates.append({
                        "value": monthly_value,
                        "chunk_id": cid,
                        "quote": quote,
                        "score": score,
                        "has_decimal": has_decimal,
                        "pattern": pat_name,
                        "file_relpath": file_relpath,
                        "source": "P&L",
                        "is_monthly": False,
                        "period_months": period_months,
                        "period_total": period_value,
                    })

    if not candidates:
        _dprint("[DEBUG] deterministic income total: no candidates found")
        return {"value": None, "citations": [], "source": None,
                "combined_value": None, "combined_citations": [],
                "combined_source": None, "components": []}

    # Pick best: highest score, then first encountered
    candidates.sort(key=lambda c: -c["score"])
    best = candidates[0]
    _dprint(f"[DEBUG] deterministic income total: ${best['value']:,.2f} "
            f"(score={best['score']}, pattern={best['pattern']}, "
            f"source={best['source']}, "
            f"file_relpath={best['file_relpath']}, "
            f"chunk={best['chunk_id'][:16]}..., "
            f"{len(candidates)} candidates)")
    for i, c in enumerate(candidates):
        extra = ""
        if not c.get("is_monthly", True):
            extra = (f" period_total=${c.get('period_total', 0):,.2f}"
                     f" period_months={c.get('period_months', 0)}")
        _dprint(f"[DEBUG]   income_candidate[{i}]: ${c['value']:,.2f} "
                f"score={c['score']} pattern={c['pattern']} "
                f"source={c['source']} "
                f"file={c['file_relpath']!r} "
                f"decimal={c['has_decimal']}{extra}")

    # --- Combined self-employed income ---
    # Eligible: PL_NET_INCOME pattern, distinct file_relpath,
    # same period_months as primary (within 0.1).
    primary_period = best.get("period_months")
    seen_files: set = set()
    components: List[Dict[str, Any]] = []
    combined_value = 0.0
    combined_citations: List[Dict[str, Any]] = []

    for c in candidates:
        if c["pattern"] != "PL_NET_INCOME":
            continue
        frp = c["file_relpath"]
        if frp in seen_files:
            continue
        pm = c.get("period_months")
        if primary_period is not None and pm is not None:
            if abs(pm - primary_period) > 0.1:
                continue
        seen_files.add(frp)
        combined_value += c["value"]
        combined_citations.append({"chunk_id": c["chunk_id"], "quote": c["quote"]})
        components.append({
            "file_relpath": frp,
            "period_total": c.get("period_total"),
            "period_months": pm,
            "monthly_equivalent": c["value"],
            "chunk_id": c["chunk_id"],
        })

    combined_value = round(combined_value, 2)
    _dprint(f"[DEBUG] income combined: ${combined_value:,.2f} "
            f"from {len(components)} component(s)")

    return {
        "value": best["value"],
        "citations": [{"chunk_id": best["chunk_id"], "quote": best["quote"]}],
        "source": best["source"],
        "combined_value": combined_value,
        "combined_citations": combined_citations,
        "combined_source": "P&L" if len(components) > 1 else best["source"],
        "components": components,
    }


def _income_analysis_prompt(question: str, evidence_block: str) -> str:
    return f"""You are MortgageDocAI.

You must answer ONLY using the EVIDENCE provided.
If no income or liability items are found, return empty lists.

Rules:
- Extract each distinct income source as a separate item in income_items.
- Extract each distinct liability / recurring debt as a separate item in liability_items.
- Include only recurring monthly debts in liability_items (e.g. mortgage, auto loan, credit card minimum). Do NOT include one-time amounts such as Cash to Close or closing costs.
- For each item include the dollar amount and frequency (weekly, biweekly, monthly, annual).
- If a proposed total housing payment (PITI + HOA / PITIA) is stated, set proposed_pitia with its monthly value and citations.
- Every item must cite one or more chunk_id values from the evidence.
- Do NOT compute totals, DTI ratios, or annualize amounts. Only list what is stated.
- Do not use outside knowledge.
- Output must be VALID JSON ONLY (no markdown, no extra text).

Question:
{question}

Evidence:
{evidence_block}

Return JSON in this schema:
{{
  "income_items": [
    {{
      "description": "e.g. Base salary",
      "amount": number,
      "frequency": "weekly|biweekly|monthly|annual",
      "citations": [{{"chunk_id":"string","quote":"short quote from chunk"}}]
    }}
  ],
  "liability_items": [
    {{
      "description": "e.g. Auto loan",
      "payment_monthly": number,
      "balance_optional": number,
      "citations": [{{"chunk_id":"string","quote":"short quote from chunk"}}]
    }}
  ],
  "proposed_pitia": {{
    "value": number,
    "citations": [{{"chunk_id":"string","quote":"short quote from chunk"}}]
  }},
  "confidence": number
}}
"""


def _normalize_income_analysis(llm_obj: Dict[str, Any], allowed_chunk_ids: set,
                               chunk_text_map: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Normalize and filter the LLM output for income_analysis profile.
    Returns a dict with 'income_items', 'liability_items',
    'proposed_pitia', and 'confidence'.
    Drops any item that has zero valid citations.
    chunk_text_map: mapping of chunk_id -> chunk text (for quote backfill).
    """
    if chunk_text_map is None:
        chunk_text_map = {}

    def _backfill_quote(cid: str, desc: str) -> str:
        """Fill empty quotes from the chunk text for auditability."""
        text = chunk_text_map.get(cid, "")
        if not text:
            return f"[chunk {cid[:12]}...]"
        # Try to find a window around the description term
        lower_text = text.lower()
        lower_desc = desc.lower().split()[0] if desc else ""
        pos = lower_text.find(lower_desc) if lower_desc else -1
        if pos >= 0:
            start = max(0, pos - 20)
            return text[start:start + 200].strip()
        return text[:200].strip()

    def _filter_citations(cits_raw_val: Any, desc: str) -> List[Dict[str, Any]]:
        """Shared citation filtering: validate chunk_ids, backfill empty quotes."""
        cits_raw = cits_raw_val
        if isinstance(cits_raw, str):
            try:
                cits_raw = json.loads(cits_raw)
            except Exception:
                cits_raw = []
        if not isinstance(cits_raw, list):
            cits_raw = []
        cits: List[Dict[str, Any]] = []
        for c in cits_raw:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("chunk_id", "") or "").strip()
            if cid not in allowed_chunk_ids:
                continue
            quote = str(c.get("quote", "") or "").strip()[:200]
            if not quote:
                quote = _backfill_quote(cid, desc)
            cits.append({"chunk_id": cid, "quote": quote})
        return cits

    # --- income_items ---
    income_raw = llm_obj.get("income_items", [])
    if isinstance(income_raw, str):
        try:
            parsed = json.loads(income_raw)
            if isinstance(parsed, list):
                income_raw = parsed
            else:
                income_raw = []
        except Exception:
            income_raw = []
    if not isinstance(income_raw, list):
        income_raw = []

    filtered_income: List[Dict[str, Any]] = []
    seen_income_keys: set = set()
    for item in income_raw:
        if not isinstance(item, dict):
            continue
        desc = str(item.get("description", "") or "").strip()
        if not desc:
            continue

        # Normalize amount
        try:
            amount = float(item.get("amount", 0) or 0)
        except (TypeError, ValueError):
            amount = 0.0

        # Normalize frequency — preserve model-provided value, canonical set only
        freq = str(item.get("frequency", "") or "").strip().lower().replace("-", "-")
        if freq in ("one_time", "onetime", "once", "lump sum", "lump_sum"):
            freq = "one-time"
        if freq not in _INCOME_FREQUENCIES_CANONICAL:
            freq = "unknown"

        # Filter citations
        cits = _filter_citations(item.get("citations", []) or [], desc)
        if not cits:
            continue

        # Deduplicate: (description, amount, first chunk_id)
        first_cid = cits[0]["chunk_id"] if cits else ""
        dedup_key = (desc.lower(), amount, first_cid)
        if dedup_key in seen_income_keys:
            continue
        seen_income_keys.add(dedup_key)

        filtered_income.append({
            "description": desc,
            "amount": amount,
            "frequency": freq,
            "citations": cits,
        })

    # --- liability_items ---
    liab_raw = llm_obj.get("liability_items", [])
    if isinstance(liab_raw, str):
        try:
            parsed = json.loads(liab_raw)
            if isinstance(parsed, list):
                liab_raw = parsed
            else:
                liab_raw = []
        except Exception:
            liab_raw = []
    if not isinstance(liab_raw, list):
        liab_raw = []

    filtered_liab: List[Dict[str, Any]] = []
    seen_liab_keys: set = set()
    for item in liab_raw:
        if not isinstance(item, dict):
            continue
        desc = str(item.get("description", "") or "").strip()
        if not desc:
            continue

        payment_monthly = None
        pmt_val = item.get("payment_monthly")
        if pmt_val is not None:
            try:
                payment_monthly = float(pmt_val)
            except (TypeError, ValueError):
                payment_monthly = None

        balance_optional = None
        bal_val = item.get("balance_optional")
        if bal_val is not None:
            try:
                balance_optional = float(bal_val)
            except (TypeError, ValueError):
                balance_optional = None

        # Filter citations
        cits = _filter_citations(item.get("citations", []) or [], desc)
        if not cits:
            continue

        # Deduplicate
        first_cid = cits[0]["chunk_id"] if cits else ""
        dedup_key = (desc.lower(), payment_monthly, first_cid)
        if dedup_key in seen_liab_keys:
            continue
        seen_liab_keys.add(dedup_key)

        entry: Dict[str, Any] = {
            "description": desc,
            "payment_monthly": payment_monthly,
            "citations": cits,
        }
        if balance_optional is not None:
            entry["balance_optional"] = balance_optional
        filtered_liab.append(entry)

    # --- proposed_pitia (structured, with citations) ---
    proposed_pitia: Optional[Dict[str, Any]] = None
    pitia_raw = llm_obj.get("proposed_pitia")
    if isinstance(pitia_raw, dict):
        pitia_val = pitia_raw.get("value")
        pitia_amount: Optional[float] = None
        if pitia_val is not None:
            try:
                pitia_amount = float(pitia_val)
            except (TypeError, ValueError):
                pitia_amount = None
        if pitia_amount is not None and pitia_amount > 0:
            pitia_cits = _filter_citations(pitia_raw.get("citations", []) or [], "proposed PITIA")
            proposed_pitia = {
                "value": pitia_amount,
                "citations": pitia_cits,
            }

    # --- fallback: housing_payment_monthly_optional (backward compat) ---
    # If proposed_pitia was not extracted, try the legacy field
    if proposed_pitia is None:
        hval = llm_obj.get("housing_payment_monthly_optional")
        if hval is not None:
            try:
                housing_fallback = float(hval)
                if housing_fallback > 0:
                    proposed_pitia = {
                        "value": housing_fallback,
                        "citations": [],  # no citations from legacy field
                    }
            except (TypeError, ValueError):
                pass

    # --- confidence calibration ---
    default_conf = 0.5 if llm_obj.get("_truncation_repaired") else 0.3
    confidence = float(llm_obj.get("confidence", default_conf) or default_conf)
    has_income = bool(filtered_income)
    has_pitia = proposed_pitia is not None
    if not has_income and not has_pitia:
        confidence = min(confidence, 0.3)
    elif not has_income or not has_pitia:
        confidence = min(confidence, 0.5)

    return {
        "income_items": filtered_income,
        "liability_items": filtered_liab,
        "proposed_pitia": proposed_pitia,
        "confidence": confidence,
    }


def _compute_dti(normalized: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic DTI computation — Python only, no LLM math.

    Only monthly and annual frequencies are convertible to monthly equivalents.
    one-time and unknown → monthly_equivalent = null (excluded from totals).
    Missing inputs produce null DTI (not 0.0).
    """
    missing_inputs: List[str] = []
    notes: List[str] = []

    # Monthly income — prefer deterministic monthly_income_total if available,
    # then fall back to summing individual income_items (LLM-extracted).
    monthly_income_total: Optional[float] = None
    income_details: List[Dict[str, Any]] = []

    det_income_obj = normalized.get("monthly_income_total")
    if isinstance(det_income_obj, dict) and det_income_obj.get("value") is not None:
        try:
            det_val = float(det_income_obj["value"])
            if det_val > 0:
                monthly_income_total = round(det_val, 2)
                notes.append(f"monthly_income_total from deterministic extractor: "
                             f"${det_val:,.2f} (source={det_income_obj.get('source', '?')})")
        except (TypeError, ValueError):
            pass

    if monthly_income_total is None:
        # Fallback: sum individual income_items (LLM-extracted)
        income_sum = 0.0
        convertible_count = 0
        for item in normalized.get("income_items", []):
            amount = item.get("amount", 0.0)
            freq = item.get("frequency", "unknown")
            monthly: Optional[float] = None
            if freq == "monthly":
                monthly = amount
            elif freq == "annual":
                monthly = round(amount / 12, 2)
            else:
                notes.append(f"income '{item['description']}' freq={freq} excluded from monthly total")

            if monthly is not None:
                monthly = round(monthly, 2)
                income_sum += monthly
                convertible_count += 1

            income_details.append({
                "description": item["description"],
                "stated_amount": amount,
                "stated_frequency": freq,
                "monthly_equivalent": monthly,
            })

        if convertible_count > 0:
            monthly_income_total = round(income_sum, 2)

    # Combined income (self-employed multi-business) — from deterministic extractor
    monthly_income_combined: Optional[float] = None
    if isinstance(det_income_obj, dict) and det_income_obj.get("combined_value") is not None:
        try:
            comb_val = float(det_income_obj["combined_value"])
            if comb_val > 0:
                monthly_income_combined = round(comb_val, 2)
                notes.append(f"monthly_income_combined from deterministic extractor: "
                             f"${comb_val:,.2f}")
        except (TypeError, ValueError):
            pass

    if monthly_income_total is None:
        missing_inputs.append("monthly_income_total")
        notes.append("no income: deterministic extractor returned null "
                     "and no LLM income_items with convertible frequency")

    # Monthly debt — prefer deterministic monthly_liabilities_total if available,
    # then fall back to summing individual liability_items.
    monthly_debt_total: Optional[float] = None
    liab_details: List[Dict[str, Any]] = []

    det_liab_obj = normalized.get("monthly_liabilities_total")
    if isinstance(det_liab_obj, dict) and det_liab_obj.get("value") is not None:
        try:
            det_val = float(det_liab_obj["value"])
            if det_val >= 0:
                monthly_debt_total = round(det_val, 2)
                notes.append(f"monthly_debt_total from deterministic extractor: ${det_val:,.2f}")
        except (TypeError, ValueError):
            pass

    if monthly_debt_total is None:
        # Fallback: sum individual liability_items
        debt_sum = 0.0
        debt_count = 0
        for item in normalized.get("liability_items", []):
            pmt = item.get("payment_monthly")
            liab_details.append({
                "description": item["description"],
                "payment_monthly": pmt,
            })
            if pmt is not None and pmt > 0:
                debt_sum += pmt
                debt_count += 1
        if debt_count > 0:
            monthly_debt_total = round(debt_sum, 2)

    if monthly_debt_total is None:
        missing_inputs.append("monthly_liabilities_total")

    # Housing / proposed PITIA (structured dict with value + citations)
    pitia_obj = normalized.get("proposed_pitia")
    housing_used: Optional[float] = None
    if isinstance(pitia_obj, dict) and pitia_obj.get("value") is not None:
        try:
            pval = float(pitia_obj["value"])
            if pval > 0:
                housing_used = pval
        except (TypeError, ValueError):
            pass
    if housing_used is None:
        missing_inputs.append("proposed_pitia")

    # DTI ratios — null when required inputs missing
    front_end_dti = None
    back_end_dti = None

    if monthly_income_total is not None and monthly_income_total > 0:
        # Front-end: proposed_pitia / income
        if housing_used is not None:
            front_end_dti = round(housing_used / monthly_income_total, 4)

        # Back-end: (proposed_pitia + liabilities) / income
        # Both housing_used and monthly_debt_total must be present
        if housing_used is not None and monthly_debt_total is not None:
            back_end_dti = round((housing_used + monthly_debt_total) / monthly_income_total, 4)

    # Combined DTI ratios (when multiple self-employed businesses)
    front_end_dti_combined = None
    back_end_dti_combined = None

    if monthly_income_combined is not None and monthly_income_combined > 0:
        if housing_used is not None:
            front_end_dti_combined = round(housing_used / monthly_income_combined, 4)
        if housing_used is not None and monthly_debt_total is not None:
            back_end_dti_combined = round(
                (housing_used + monthly_debt_total) / monthly_income_combined, 4)

    return {
        "monthly_income_total": monthly_income_total,
        "monthly_income_combined": monthly_income_combined,
        "monthly_debt_total": monthly_debt_total,
        "housing_payment_used": housing_used,
        "front_end_dti": front_end_dti,
        "back_end_dti": back_end_dti,
        "front_end_dti_combined": front_end_dti_combined,
        "back_end_dti_combined": back_end_dti_combined,
        "missing_inputs": missing_inputs,
        "notes": notes,
        "inputs_snapshot": {
            "income_details": income_details,
            "liability_details": liab_details,
        },
    }


# ---------------------------------------------------------------------------
# uw_decision profile helpers (deterministic, no LLM)
# ---------------------------------------------------------------------------

def _resolve_uw_decision_inputs(
    profiles_dir: Path,
) -> Dict[str, Any]:
    """Locate income_analysis.json + dti.json for uw_decision.

    Searches profiles_dir / "income_analysis" (either from same invocation
    or preserved from a previous run via profile-aware rerun).
    Raises ContractError if not found.
    """
    base = profiles_dir / "income_analysis"
    ia_path = base / "income_analysis.json"
    dti_path = base / "dti.json"

    if ia_path.exists() and dti_path.exists():
        income_analysis = json.loads(ia_path.read_text(encoding="utf-8"))
        dti = json.loads(dti_path.read_text(encoding="utf-8"))
        _dprint(f"[DEBUG] uw_decision: loaded inputs from {base}")
        return {"income_analysis": income_analysis, "dti": dti}

    raise ContractError(
        "uw_decision requires income_analysis profile to have run first. "
        f"Expected income_analysis.json + dti.json in {base}"
    )


_UW_MAX_BACK_END_DTI = 0.45


def _load_uw_policy(tenant_id: str) -> Dict[str, Any]:
    """Load tenant-specific UW policy thresholds, falling back to defaults.

    Looks for:
        NAS_ANALYZE / "tenants" / {tenant_id} / "policy" / "uw_thresholds.json"
    """
    policy_path = NAS_ANALYZE / "tenants" / tenant_id / "policy" / "uw_thresholds.json"
    defaults: Dict[str, Any] = {
        "program": "Conventional",
        "thresholds": {
            "max_back_end_dti": _UW_MAX_BACK_END_DTI,
            "max_front_end_dti": None,
        },
        "policy_version": None,
        "policy_source": "default",
        "policy_path": None,
    }
    try:
        if policy_path.exists():
            raw = json.loads(policy_path.read_text(encoding="utf-8"))
            threshold = float(raw["thresholds"]["max_back_end_dti"])
            if not (0 < threshold < 1):
                raise ValueError(f"max_back_end_dti={threshold} outside (0,1)")
            _dprint(f"[DEBUG] uw_policy: loaded from {policy_path}")
            return {
                "program": raw.get("program", "Conventional"),
                "thresholds": {
                    "max_back_end_dti": threshold,
                    "max_front_end_dti": raw["thresholds"].get("max_front_end_dti"),
                },
                "policy_version": raw.get("policy_version"),
                "policy_source": "file",
                "policy_path": str(policy_path),
            }
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        _dprint(f"[DEBUG] uw_policy: failed to load {policy_path}: {exc}; using defaults")
    return defaults


def _build_version_info(policy: Dict[str, Any]) -> Dict[str, Any]:
    """Build version.json metadata. Git operations fail gracefully."""
    git_commit = None
    git_dirty = None
    try:
        r = subprocess.run(
            ["git", "-C", "/opt/mortgagedocai", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            git_commit = r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        r = subprocess.run(
            ["git", "-C", "/opt/mortgagedocai", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            git_dirty = bool(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git": {"commit": git_commit, "dirty": git_dirty},
        "schemas": {"uw_decision": "v0.7"},
        "policy": {
            "policy_version": policy.get("policy_version"),
            "policy_source": policy["policy_source"],
            "path": policy.get("policy_path"),
        },
    }


# ---------------------------------------------------------------------------
# Step12 unified version blob (all profiles)
# ---------------------------------------------------------------------------

# Schema version — bump when the output JSON shape for that profile changes.
_SCHEMA_VERSIONS: Dict[str, str] = {
    "uw_decision":    "v0.7",
    "uw_conditions":  "v1",
    "income_analysis": "v1",
    "default":        "v1",
}


def _build_version_blob(
    args: "argparse.Namespace",
    ctx_run_id: str,
    schemas: Dict[str, str],
    rp_path: Optional[Path],
    rp_sha256: Optional[str],
    rp_source: str,
) -> Dict[str, Any]:
    """Build the unified version/audit blob written to every Step12 profile output.

    Mirrors _build_version_info git pattern; fails gracefully on git errors.
    Note: offline_embeddings is a step13 argument and is intentionally absent.
    """
    git_commit = None
    git_dirty = None
    try:
        r = subprocess.run(
            ["git", "-C", "/opt/mortgagedocai", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            git_commit = r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        r = subprocess.run(
            ["git", "-C", "/opt/mortgagedocai", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            git_dirty = bool(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "git": {"commit": git_commit, "dirty": git_dirty},
        "run": {
            "tenant_id": getattr(args, "tenant_id", None),
            "loan_id": getattr(args, "loan_id", None),
            "run_id": ctx_run_id,
        },
        "options": {
            "llm_model": getattr(args, "llm_model", None),
            "llm_temperature": getattr(args, "llm_temperature", None),
            "llm_max_tokens": getattr(args, "llm_max_tokens", None),
            "evidence_max_chars": getattr(args, "evidence_max_chars", None),
            "ollama_url": getattr(args, "ollama_url", None),
        },
        "retrieval_pack": {
            "path": str(rp_path) if rp_path else None,
            "sha256": rp_sha256,
            "source": rp_source,
        },
        "schemas": schemas,
    }


def _build_uw_decision(
    income_analysis: Dict[str, Any],
    dti: Dict[str, Any],
    tenant_id: str,
    loan_id: str,
    run_id: str,
    policy: Dict[str, Any],
) -> Dict[str, Any]:
    """Build deterministic underwriting decision from income_analysis outputs.

    Rules (v0.7 policy-driven):
      back_end_dti is None                                -> UNKNOWN
      back_end_dti <= policy.thresholds.max_back_end_dti  -> PASS
      back_end_dti >  policy.thresholds.max_back_end_dti  -> FAIL
    Same rules applied independently for combined scenario.
    """
    threshold = policy["thresholds"]["max_back_end_dti"]
    back_end_dti = dti.get("back_end_dti")
    front_end_dti = dti.get("front_end_dti")
    back_end_dti_combined = dti.get("back_end_dti_combined")
    front_end_dti_combined = dti.get("front_end_dti_combined")
    missing_inputs = dti.get("missing_inputs", [])

    # --- Primary decision ---
    primary_reasons: List[Dict[str, Any]] = []
    primary_missing: List[str] = []
    if back_end_dti is None:
        primary_status = "UNKNOWN"
        primary_missing = list(missing_inputs)
        primary_reasons.append({
            "rule": "DTI_BACK_END_MAX",
            "status": "UNKNOWN",
            "value": None,
            "threshold": threshold,
        })
    elif back_end_dti <= threshold:
        primary_status = "PASS"
        primary_reasons.append({
            "rule": "DTI_BACK_END_MAX",
            "status": "PASS",
            "value": back_end_dti,
            "threshold": threshold,
        })
    else:
        primary_status = "FAIL"
        primary_reasons.append({
            "rule": "DTI_BACK_END_MAX",
            "status": "FAIL",
            "value": back_end_dti,
            "threshold": threshold,
        })

    # --- Combined decision ---
    decision_combined = None
    if back_end_dti_combined is not None:
        combined_reasons: List[Dict[str, Any]] = []
        if back_end_dti_combined <= threshold:
            combined_status = "PASS"
        else:
            combined_status = "FAIL"
        combined_reasons.append({
            "rule": "DTI_BACK_END_MAX",
            "status": combined_status,
            "value": back_end_dti_combined,
            "threshold": threshold,
        })
        decision_combined = {
            "status": combined_status,
            "reasons": combined_reasons,
            "missing_inputs": [],
        }

    # --- Collect citations from income_analysis ---
    cit_pitia: List[Dict[str, Any]] = []
    pitia_obj = income_analysis.get("proposed_pitia")
    if isinstance(pitia_obj, dict):
        cit_pitia = pitia_obj.get("citations", [])

    cit_liab: List[Dict[str, Any]] = []
    mlt_obj = income_analysis.get("monthly_liabilities_total")
    if isinstance(mlt_obj, dict):
        cit_liab = mlt_obj.get("citations", [])

    cit_income_primary: List[Dict[str, Any]] = []
    mit_obj = income_analysis.get("monthly_income_total")
    if isinstance(mit_obj, dict):
        cit_income_primary = mit_obj.get("citations", [])

    cit_income_combined: List[Dict[str, Any]] = []
    mic_obj = income_analysis.get("monthly_income_total_combined")
    if isinstance(mic_obj, dict):
        cit_income_combined = mic_obj.get("citations", [])

    # Build inputs snapshot
    pitia_val = None
    if isinstance(pitia_obj, dict) and pitia_obj.get("value") is not None:
        pitia_val = pitia_obj["value"]
    liab_val = None
    if isinstance(mlt_obj, dict) and mlt_obj.get("value") is not None:
        liab_val = mlt_obj["value"]
    income_primary_val = None
    if isinstance(mit_obj, dict) and mit_obj.get("value") is not None:
        income_primary_val = mit_obj["value"]
    income_combined_val = None
    if isinstance(mic_obj, dict) and mic_obj.get("value") is not None:
        income_combined_val = mic_obj["value"]

    dti_primary_snapshot = None
    if front_end_dti is not None or back_end_dti is not None:
        dti_primary_snapshot = {"front_end": front_end_dti, "back_end": back_end_dti}
    dti_combined_snapshot = None
    if front_end_dti_combined is not None or back_end_dti_combined is not None:
        dti_combined_snapshot = {
            "front_end": front_end_dti_combined,
            "back_end": back_end_dti_combined,
        }

    # Flatten citations for answer-level list (deduped)
    all_cits: List[Dict[str, Any]] = []
    seen_cids: set = set()
    for source_cits in [cit_pitia, cit_liab, cit_income_primary, cit_income_combined]:
        for c in source_cits:
            cid = c.get("chunk_id", "")
            if cid and cid not in seen_cids:
                seen_cids.add(cid)
                all_cits.append(c)

    confidence = 0.9 if primary_status != "UNKNOWN" else 0.3

    return {
        "profile": "uw_decision",
        "tenant_id": tenant_id,
        "loan_id": loan_id,
        "run_id": run_id,
        "ruleset": {
            "program": policy["program"],
            "version": "v0.7-policy",
            "thresholds": {
                "max_back_end_dti": threshold,
                "max_front_end_dti": policy["thresholds"].get("max_front_end_dti"),
            },
            "policy_source": policy["policy_source"],
            "policy_version": policy.get("policy_version"),
        },
        "inputs": {
            "pitia": pitia_val,
            "liabilities_monthly": liab_val,
            "income_monthly_primary": income_primary_val,
            "income_monthly_combined": income_combined_val,
            "dti_primary": dti_primary_snapshot,
            "dti_combined": dti_combined_snapshot,
        },
        "decision_primary": {
            "status": primary_status,
            "reasons": primary_reasons,
            "missing_inputs": primary_missing,
        },
        "decision_combined": decision_combined,
        "citations": {
            "pitia": cit_pitia,
            "liabilities": cit_liab,
            "income_primary": cit_income_primary,
            "income_combined": cit_income_combined,
        },
        "confidence": confidence,
        "_citations_flat": all_cits,
    }


def _format_uw_decision_md(decision: Dict[str, Any]) -> str:
    """Format uw_decision result as human-readable markdown."""
    ruleset = decision["ruleset"]
    inp = decision["inputs"]
    primary = decision["decision_primary"]
    combined = decision.get("decision_combined")

    lines = [
        "# Underwriting Decision (Deterministic)",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Tenant | {decision['tenant_id']} |",
        f"| Loan | {decision['loan_id']} |",
        f"| Run | {decision['run_id']} |",
        f"| Generated | {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} |",
        "",
        f"**Program:** {ruleset['program']}",
        f"**Ruleset version:** {ruleset['version']}",
        f"**Policy source:** {ruleset.get('policy_source', 'default')}",
        f"**Threshold:** back-end DTI <= {ruleset['thresholds']['max_back_end_dti']:.0%}",
        "",
        "## Primary Decision",
        "",
        f"**Status: {primary['status']}**",
        "",
    ]
    for r in primary.get("reasons", []):
        val_str = f"{r['value']:.4f} ({r['value'] * 100:.2f}%)" if r["value"] is not None else "null"
        lines.append(f"- Rule `{r['rule']}`: {r['status']} "
                     f"(value={val_str}, threshold={r['threshold']:.0%})")
    if primary.get("missing_inputs"):
        lines.append(f"- Missing inputs: {', '.join(primary['missing_inputs'])}")
    lines.append("")

    if combined is not None:
        lines.extend([
            "## Combined Decision (Multi-Business Self-Employed)",
            "",
            f"**Status: {combined['status']}**",
            "",
        ])
        for r in combined.get("reasons", []):
            val_str = (f"{r['value']:.4f} ({r['value'] * 100:.2f}%)"
                       if r["value"] is not None else "null")
            lines.append(f"- Rule `{r['rule']}`: {r['status']} "
                         f"(value={val_str}, threshold={r['threshold']:.0%})")
        lines.append("")

    pitia_str = f"${inp['pitia']:,.2f}" if inp.get("pitia") is not None else "UNKNOWN"
    liab_str = f"${inp['liabilities_monthly']:,.2f}" if inp.get("liabilities_monthly") is not None else "UNKNOWN"
    inc_str = f"${inp['income_monthly_primary']:,.2f}" if inp.get("income_monthly_primary") is not None else "UNKNOWN"
    lines.extend([
        "## Inputs",
        "",
        f"- PITIA: {pitia_str}",
        f"- Liabilities (monthly): {liab_str}",
        f"- Income (primary): {inc_str}",
    ])
    if inp.get("income_monthly_combined") is not None:
        lines.append(f"- Income (combined): ${inp['income_monthly_combined']:,.2f}")
    dti_p = inp.get("dti_primary")
    if dti_p:
        if dti_p.get("front_end") is not None:
            lines.append(f"- Front-end DTI (primary): {dti_p['front_end']:.4f} "
                         f"({dti_p['front_end'] * 100:.2f}%)")
        if dti_p.get("back_end") is not None:
            lines.append(f"- Back-end DTI (primary): {dti_p['back_end']:.4f} "
                         f"({dti_p['back_end'] * 100:.2f}%)")
    dti_c = inp.get("dti_combined")
    if dti_c:
        if dti_c.get("front_end") is not None:
            lines.append(f"- Front-end DTI (combined): {dti_c['front_end']:.4f} "
                         f"({dti_c['front_end'] * 100:.2f}%)")
        if dti_c.get("back_end") is not None:
            lines.append(f"- Back-end DTI (combined): {dti_c['back_end']:.4f} "
                         f"({dti_c['back_end'] * 100:.2f}%)")
    lines.append("")

    # --- Evidence / Citations ---
    cit_dict = decision.get("citations", {})
    has_any_cit = any(cit_dict.get(k) for k in
                      ("pitia", "liabilities", "income_primary", "income_combined"))
    if has_any_cit:
        lines.extend(["## Evidence / Citations", ""])
        for section_key, section_label in [
            ("pitia", "PITIA"),
            ("liabilities", "Liabilities"),
            ("income_primary", "Income (Primary)"),
            ("income_combined", "Income (Combined)"),
        ]:
            section_cits = cit_dict.get(section_key, [])
            if section_cits:
                lines.append(f"### {section_label}")
                lines.append("")
                for c in section_cits:
                    cid = c.get("chunk_id", "unknown")
                    quote = (c.get("quote", "") or "").strip()[:200]
                    if quote:
                        lines.append(f"- **{cid}**: {quote}")
                    else:
                        lines.append(f"- **{cid}**")
                lines.append("")

    return "\n".join(lines) + "\n"


def _synthesize_uw_decision_answer(decision: Dict[str, Any]) -> str:
    """Synthesize answer text from uw_decision for answer.json."""
    primary = decision["decision_primary"]
    parts = [
        f"Underwriting Decision ({decision['ruleset']['version']}): "
        f"{primary['status']}"
    ]
    for r in primary.get("reasons", []):
        val_str = (f"{r['value'] * 100:.2f}%" if r["value"] is not None else "null")
        parts.append(f"  {r['rule']}: {r['status']} "
                     f"(value={val_str}, threshold={r['threshold']:.0%})")
    combined = decision.get("decision_combined")
    if combined is not None:
        parts.append(f"Combined: {combined['status']}")
        for r in combined.get("reasons", []):
            val_str = (f"{r['value'] * 100:.2f}%" if r["value"] is not None else "null")
            parts.append(f"  {r['rule']}: {r['status']} "
                         f"(value={val_str}, threshold={r['threshold']:.0%})")
    if primary.get("missing_inputs"):
        parts.append(f"Missing inputs: {', '.join(primary['missing_inputs'])}")
    return "\n".join(parts)


def _unwrap_nested_json(obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Post-parse normalization:
    - If obj["answer"] is a list of strings, join into a single string.
    - If obj["answer"] is itself a JSON-encoded string containing a dict
      with "answer", unwrap it one level.
    - If "citations" arrives as a JSON string rather than a list, decode it.
    """
    answer_val = obj.get("answer")

    # --- answer as list (e.g. mistral: ["1. ...", "2. ..."]) ---
    if isinstance(answer_val, list):
        obj["answer"] = "\n".join(str(item) for item in answer_val)
        answer_val = obj["answer"]  # update for nested-string check below

    if isinstance(answer_val, str) and answer_val.strip().startswith("{"):
        try:
            inner = json.loads(answer_val)
            if isinstance(inner, dict) and "answer" in inner:
                # Promote inner fields, preserving outer fields as fallback
                obj["answer"] = inner.get("answer", answer_val)
                if "citations" in inner and inner["citations"]:
                    obj["citations"] = inner["citations"]
                if "confidence" in inner:
                    obj["confidence"] = inner["confidence"]
                obj["_unwrapped_nested"] = True
        except Exception:
            pass

    citations_val = obj.get("citations")
    if isinstance(citations_val, str):
        try:
            parsed = json.loads(citations_val)
            if isinstance(parsed, list):
                obj["citations"] = parsed
        except Exception:
            pass

    return obj


def _repair_truncated_json(text: str) -> Optional[Dict[str, Any]]:
    """
    Attempt to salvage a truncated JSON object (e.g. model hit max_tokens).

    Strategy: find the first '{', then iteratively close any open strings,
    arrays, and objects until the JSON parses.  Only mutates the *tail* of
    the string; never touches content that already parsed successfully.
    Returns parsed dict on success, None on failure.
    """
    start = text.find("{")
    if start == -1:
        return None

    fragment = text[start:]

    # Closers to try appending — order matters: close string first, then
    # array, then object.  We try increasingly aggressive repairs.
    closers_sequences = [
        '"}]}]}',    # truncated inside quote→citation→citations[]→condition→conditions[]→root (uw_conditions)
        '"}]}}',     # truncated inside quote→citation→citations[]→proposed_pitia→root (income_analysis)
        '"}]}',      # truncated inside a string inside an array inside the object
        '"]}',       # truncated inside a string at end of array
        '"]',        # truncated inside a string value
        '"}',        # truncated inside a string key/value
        '"',         # just an unclosed string
        '}]}]}',     # unclosed obj→array→obj→array→root (uw_conditions, no open string)
        '}]}}',      # citation obj→citations[]→proposed_pitia→root (income_analysis)
        ']}]}',      # deeply nested
        ']}',        # unclosed array + object
        ']',         # unclosed array
        '}',         # unclosed object
        '"}}',       # string + nested objects
    ]

    for closers in closers_sequences:
        candidate = fragment + closers
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and ("answer" in obj or "conditions" in obj or "income_items" in obj):
                obj["_truncation_repaired"] = True
                return obj
        except Exception:
            continue

    return None


def _remove_trailing_commas_json(text: str) -> str:
    """Remove trailing commas before ] or } so strict JSON becomes parseable."""
    return re.sub(r",(\s*[\]}])", r"\1", text)


def _rescue_income_json(raw: str) -> Optional[Dict[str, Any]]:
    """
    Last-resort extraction for income_analysis payloads.

    When _parse_llm_json falls to the generic fallback (wrapping raw text as
    {"answer": ...}), this function tries harder to find a dict containing
    "income_items" — including truncation repair with progressive tail
    trimming.  Returns the rescued dict or None.
    """
    text = raw.strip()

    # Try direct parse (and with trailing commas removed — LLMs often emit them)
    for candidate in (text, _remove_trailing_commas_json(text)):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and "income_items" in obj:
                return obj
        except Exception:
            pass

    # Try extracting { ... } from prose
    obj = _extract_json_object(text)
    if obj is not None and "income_items" in obj:
        return obj

    # --- Progressive truncation repair ---
    # The income_analysis JSON can be truncated at many nesting depths.
    # Strategy: find the opening '{', then try closing with increasingly
    # aggressive closer sequences.  If none work on the full fragment,
    # trim backwards to the last comma or brace and retry — this discards
    # the incomplete trailing element and closes the parent containers.
    start = text.find("{")
    if start == -1:
        return None
    fragment = text[start:]

    closers = [
        '"}]}]}',    # quote→citation→citations[]→item→items[]→root
        '"}]}}',     # quote→citation→citations[]→proposed_pitia→root
        '"}]}',      # quote→citation→citations[]→root
        '"]}',       # string inside array
        '"]',        # string at end of array
        '"}',        # string inside object
        '"',         # unclosed string
        '}]}]}',     # no open string, deeply nested
        '}]}}',      # citation obj→citations[]→proposed_pitia→root
        ']}]}',      # array→object→array→root
        '}]}',       # object→array→root
        ']}',        # array + object
        ']',         # array
        '}',         # object
    ]

    # Phase 1: try closers on the full fragment
    for c in closers:
        candidate = fragment + c
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and "income_items" in obj:
                obj["_truncation_repaired"] = True
                return obj
        except Exception:
            continue

    # Phase 2: progressively trim the fragment tail back to the last
    # comma / closing-brace / closing-bracket, then retry closers.
    # This discards the incomplete trailing element.
    trim_chars = {",", "}", "]", "\n"}
    frag = fragment
    for _ in range(80):  # max 80 trim attempts
        # Find the last safe trim point
        idx = max(frag.rfind(ch) for ch in trim_chars)
        if idx <= 0:
            break
        frag = frag[:idx]
        for c in closers:
            candidate = frag + c
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict) and "income_items" in obj:
                    obj["_truncation_repaired"] = True
                    return obj
            except Exception:
                continue

    return None


def _parse_llm_json(raw: str) -> Dict[str, Any]:
    """
    Robust JSON parsing for LLM output.

    Handles common failure modes:
    1. Raw JSON (ideal case)
    2. Markdown-fenced JSON (```json ... ``` or ``` ... ```)
    3. JSON embedded in prose (leading/trailing text around {...})
    4. Nested JSON-in-string (answer value is itself a JSON string)
    5. Control characters / trailing whitespace that break json.loads
    6. Truncated JSON (model hit max_tokens before closing)
    7. Fallback: wrap raw text as low-confidence answer
    """
    text = raw.strip()

    # --- Attempt 1: direct parse ---
    try:
        return _unwrap_nested_json(json.loads(text))
    except Exception:
        pass

    # --- Attempt 2: strip markdown fences ---
    # Handles: ```json\n{...}\n```  or  ```\n{...}\n```
    fenced = re.sub(r"^```[a-zA-Z]*\s*\n?", "", text)
    fenced = re.sub(r"\n?```\s*$", "", fenced).strip()
    if fenced != text:
        try:
            return _unwrap_nested_json(json.loads(fenced))
        except Exception:
            pass

    # --- Attempt 3: extract outermost { ... } using brace-depth matching ---
    obj = _extract_json_object(fenced) or _extract_json_object(text)
    if obj is not None:
        return _unwrap_nested_json(obj)

    # --- Attempt 4: greedy regex fallback (first { to last }) ---
    m = re.search(r"\{.*\}", text, flags=re.S)
    if m:
        try:
            return _unwrap_nested_json(json.loads(m.group(0)))
        except Exception:
            pass

    # --- Attempt 5: truncated JSON repair (model hit max_tokens mid-output) ---
    repaired = _repair_truncated_json(text)
    if repaired is not None:
        return _unwrap_nested_json(repaired)

    # --- Fallback: wrap raw text ---
    return {
        "answer": raw[:4000],
        "citations": [],
        "confidence": 0.3,
        "parse_warning": "Model did not return valid JSON",
    }


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """
    Find the first balanced { ... } in text using brace-depth counting.
    Respects quoted strings so braces inside strings are ignored.
    Returns parsed dict if successful, None otherwise.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            if in_string:
                escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        return obj
                except Exception:
                    pass
                return None
    return None

def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Step 12 — Analyze (local Ollama, evidence-only, multiquery)")
    ap.add_argument("--tenant-id", default=DEFAULT_TENANT)
    ap.add_argument("--loan-id", required=True)
    ap.add_argument("--run-id", required=True)

    ap.add_argument("--query", action="append", dest="queries", default=None)
    ap.add_argument("--analysis-profile", action="append", dest="profiles", default=None)
    ap.add_argument("--retrieval-pack", action="append", dest="retrieval_packs", default=None)
    # Auto-retrieve is ON by default (contract-friendly), but operators can disable it.
    # NOTE: previous implementation used store_true with default=True, which made it impossible
    # to disable from CLI. This flag fixes that.
    ap.add_argument("--no-auto-retrieve", action="store_true", default=False,
                    help="Disable Step13 auto-retrieval when retrieval_pack.json is missing")

    # Ollama controls
    ap.add_argument("--ollama-url", default="http://localhost:11434")
    ap.add_argument("--llm-model", default="llama3")
    ap.add_argument("--llm-temperature", type=float, default=0.0)
    ap.add_argument("--llm-max-tokens", type=int, default=800)
    ap.add_argument("--evidence-max-chars", type=int, default=12000)
    ap.add_argument("--ollama-timeout", type=int, default=600,
                    help="Timeout in seconds for Ollama HTTP request (default: 600)")
    ap.add_argument("--debug", action="store_true", default=False,
                    help="Enable diagnostic debug output")
    ap.add_argument("--save-llm-raw", action="store_true", default=False,
                    help="Write raw LLM response to disk for inspection (overwritten per rerun)")

    return ap.parse_args(argv)

def main(argv=None) -> None:
    global _DEBUG
    args = parse_args(argv)
    _DEBUG = args.debug
    # Query-only runs (e.g. API "Ask a question") read from nas_analyze only; skip SOURCE_MOUNT check to avoid autofs not materialized
    preflight_mount_contract(skip_source_check=bool(args.queries))
    ctx = build_run_context(args.tenant_id, args.loan_id, run_id=args.run_id)

    queries = args.queries or ["Summarize the key underwriting conditions."]
    profiles = args.profiles or ["default"]
    if len(profiles) == 1 and len(queries) > 1:
        profiles = profiles * len(queries)
    if len(profiles) != len(queries):
        raise ContractError("Number of profiles must equal number of queries (or provide one profile for all)")

    packs = args.retrieval_packs or []
    if packs and len(packs) not in (1, len(queries)):
        raise ContractError("--retrieval-pack must be provided once or once per query")
    if len(packs) == 1 and len(queries) > 1:
        packs = packs + [None] * (len(queries) - 1)
    if not packs:
        packs = [None] * len(queries)

    staging = ctx.analyze_staging_run_root
    final = ctx.analyze_final_run_root

    # Create staging structure first (needed for profile-aware preservation)
    ensure_dir(staging)
    meta_dir = staging / "_meta"
    out_dir = staging / "outputs"
    profiles_dir = out_dir / "profiles"
    ensure_dir(meta_dir)
    ensure_dir(out_dir)
    ensure_dir(profiles_dir)

    # Profile-aware rerun: preserve profiles NOT being overwritten in this invocation.
    # This allows uw_decision to read income_analysis outputs from a prior run.
    import shutil
    current_profiles = set(profiles)
    _prev_run_meta = None
    if final.exists():
        existing_profiles_dir = final / "outputs" / "profiles"
        if existing_profiles_dir.exists():
            for existing_prof in existing_profiles_dir.iterdir():
                if existing_prof.is_dir() and existing_prof.name not in current_profiles:
                    dest = profiles_dir / existing_prof.name
                    if not dest.exists():
                        shutil.copytree(str(existing_prof), str(dest))
                        _dprint(f"[DEBUG] preserved profile from previous run: "
                                f"{existing_prof.name}")
        existing_meta = final / "_meta" / "analysis_run.json"
        if existing_meta.exists():
            _prev_run_meta = json.loads(existing_meta.read_text(encoding="utf-8"))
        shutil.rmtree(final)

    run_meta: List[Dict[str, Any]] = []

    for idx, (question, profile, rp_arg) in enumerate(zip(queries, profiles, packs)):
        rp_path: Optional[Path] = Path(rp_arg) if rp_arg else None
        rp_source: str = "explicit" if rp_arg else "unset"

        # If pack explicitly provided, use it; else prefer retrieve/<run_id>/; else optionally auto Step13.
        if rp_path and not rp_path.exists():
            raise ContractError(f"retrieval pack not found: {rp_path}")

        if rp_path is None:
            rp_path = _find_retrieval_pack_for_run(args.tenant_id, args.loan_id, ctx.run_id)
            if rp_path is None:
                # fallback to latest if present
                rp_path = _find_latest_retrieval_pack(args.tenant_id, args.loan_id)
                if rp_path is not None:
                    rp_source = "latest"
            else:
                rp_source = "run_id"

        if rp_path is None and not args.no_auto_retrieve:
            rp_path = _run_step13(args.tenant_id, args.loan_id, ctx.run_id, question)
            rp_source = "step13"

        retrieved: List[Dict[str, Any]] = []
        pack: Dict[str, Any] = {}
        if rp_path and rp_path.exists():
            pack = json.loads(rp_path.read_text(encoding="utf-8"))
            retrieved = pack.get("retrieved_chunks", []) or []

        evidence_block = _build_evidence_block(retrieved, max_chars_total=args.evidence_max_chars)
        allowed_chunk_ids = {
            (ch.get("chunk_id") or ch.get("payload", {}).get("chunk_id") or "")
            for ch in retrieved
        }
        allowed_chunk_ids.discard("")

        # Retrieval provenance — hash from bytes on disk
        _rp_sha256: Optional[str] = None
        _rp_abspath: Optional[str] = None
        if rp_path and rp_path.exists():
            try:
                _rp_sha256 = sha256_file(rp_path)
                _rp_abspath = str(rp_path.resolve())
            except OSError:
                import sys as _sys
                print(f"WARNING: could not hash retrieval_pack: {rp_path}", file=_sys.stderr)
        _rp_provenance = {
            "retrieval_pack_sha256": _rp_sha256,
            "retrieval_pack_path": _rp_abspath,
            "retrieval_pack_run_id": ctx.run_id,
        }

        is_uw = (profile == "uw_conditions")
        is_income = (profile == "income_analysis")
        is_uw_decision = (profile == "uw_decision")

        # ── Per-profile version.json + run-level outputs/_meta/version.json ──────
        # Written for EVERY profile (uw_conditions, income_analysis, uw_decision,
        # default). The old per-profile writes inside the if-blocks below are removed.
        _ver_blob = _build_version_blob(
            args, ctx.run_id, _SCHEMA_VERSIONS, rp_path, _rp_sha256, rp_source
        )
        _ver_prof_out = profiles_dir / profile
        ensure_dir(_ver_prof_out)
        atomic_write_json(_ver_prof_out / "version.json", _ver_blob)
        _ver_meta_dir = out_dir / "_meta"
        ensure_dir(_ver_meta_dir)
        atomic_write_json(_ver_meta_dir / "version.json", _ver_blob)

        # --- uw_decision: purely deterministic, no LLM needed ---
        if is_uw_decision:
            policy = _load_uw_policy(args.tenant_id)
            _dprint(f"[DEBUG] uw_decision: policy_source={policy['policy_source']}")
            decision_data = _resolve_uw_decision_inputs(profiles_dir)
            decision_result = _build_uw_decision(
                income_analysis=decision_data["income_analysis"],
                dti=decision_data["dti"],
                tenant_id=args.tenant_id,
                loan_id=args.loan_id,
                run_id=ctx.run_id,
                policy=policy,
            )
            decision_md_text = _format_uw_decision_md(decision_result)
            answer_text = _synthesize_uw_decision_answer(decision_result)
            flat_cits = decision_result.pop("_citations_flat", [])
            confidence = decision_result["confidence"]

            _dprint(f"[DEBUG] uw_decision: primary={decision_result['decision_primary']['status']} "
                    f"combined={decision_result['decision_combined']['status'] if decision_result.get('decision_combined') else 'N/A'}")

            # Write profile artifacts
            prof_out = profiles_dir / profile
            ensure_dir(prof_out)
            decision_result.update(_rp_provenance)
            atomic_write_json(prof_out / "decision.json", decision_result)
            atomic_write_text(prof_out / "decision.md", decision_md_text)

            # Standard profile framework files
            answer_md_lines = [
                f"# Answer — profile: {profile}",
                "",
                f"**Question:** {question}",
                "",
                answer_text,
                "",
                "## Citations",
            ]
            citations_lines: List[str] = []
            for c in flat_cits:
                cid = c.get("chunk_id", "")
                quote = (c.get("quote", "") or "").strip()
                answer_md_lines.append(f"- {cid}: {quote[:200]}")
                citations_lines.append(
                    json.dumps({"chunk_id": cid, "quote": quote}, ensure_ascii=False))

            atomic_write_text(prof_out / "answer.md",
                              "\n".join(answer_md_lines) + "\n")
            atomic_write_json(prof_out / "answer.json", {
                "answer": answer_text,
                "citations": flat_cits,
                "confidence": confidence,
                "question": question,
                "profile": profile,
                "retrieval_pack": None,
                "retrieval_pack_source": "none (deterministic)",
            })
            atomic_write_text(prof_out / "citations.jsonl",
                              "\n".join(citations_lines) + ("\n" if citations_lines else ""))

            run_meta.append({
                "profile": profile,
                "question": question,
                "retrieval_pack": None,
                "retrieval_pack_source": "none (deterministic)",
                "evidence_chars": 0,
                "ollama_model": "none (deterministic)",
                "confidence": confidence,
            })
            continue  # Skip LLM call, evidence loading, etc.

        # If RUN_LLM=0 (deterministic-only) or no evidence, skip Ollama.
        if os.environ.get("RUN_LLM", "1") == "0" or not evidence_block.strip():
            llm_obj = {"answer": "Not found in provided documents.", "citations": [], "confidence": 0.1}
            if is_uw:
                llm_obj["conditions"] = []
            if is_income:
                llm_obj["income_items"] = []
                llm_obj["liability_items"] = []
                llm_obj["proposed_pitia"] = None
            llm_raw = ""
        else:
            if is_uw:
                prompt = _uw_conditions_prompt(question, evidence_block)
            elif is_income:
                prompt = _income_analysis_prompt(question, evidence_block)
            else:
                prompt = _evidence_only_prompt(question, evidence_block)
            llm_raw = _ollama_generate(
                ollama_url=args.ollama_url,
                model=args.llm_model,
                prompt=prompt,
                temperature=args.llm_temperature,
                max_tokens=args.llm_max_tokens,
                timeout=args.ollama_timeout,
            )
            llm_obj = _parse_llm_json(llm_raw)

            # For income_analysis: if parser fell to fallback but raw contains
            # income_items JSON, rescue by re-extracting the income payload.
            if is_income and "income_items" not in llm_obj:
                rescued = _rescue_income_json(llm_raw)
                if rescued is not None:
                    llm_obj = rescued
                    _dprint("[DEBUG] income_analysis: rescued income_items from raw LLM output")

        _dprint(f"[DEBUG] llm_raw (first 500 chars):\n{llm_raw[:500]}")
        _dprint(f"[DEBUG] llm_obj keys={sorted(llm_obj.keys())} "
                f"answer_type={type(llm_obj.get('answer')).__name__} "
                f"citations_len={len(llm_obj.get('citations', []) or [])} "
                f"confidence={llm_obj.get('confidence')!r} "
                f"truncation_repaired={llm_obj.get('_truncation_repaired', False)}")

        # --- --save-llm-raw: persist raw LLM response to disk ---
        if args.save_llm_raw:
            # Determine target directory: profile dir if profiles are used, else outputs/
            raw_dir = profiles_dir / profile if profile else out_dir
            ensure_dir(raw_dir)
            # Filename: llm_raw.txt for single query, llm_raw_q<NN>.txt for multi
            if len(queries) == 1:
                raw_filename = "llm_raw.txt"
            else:
                raw_filename = f"llm_raw_q{idx + 1:02d}.txt"
            raw_path = raw_dir / raw_filename
            header = (
                f"# timestamp: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
                f"# tenant_id={args.tenant_id} loan_id={args.loan_id} run_id={ctx.run_id}"
                f" model={args.llm_model} profile={profile} query={question}\n"
            )
            raw_path.write_text(header + llm_raw, encoding="utf-8")
            _dprint(f"[DEBUG] saved llm_raw -> {raw_path} ({raw_path.stat().st_size} bytes)")

        # For uw_conditions: synthesize answer text from conditions if model didn't return "answer"
        if is_uw and not llm_obj.get("answer") and llm_obj.get("conditions"):
            conds = llm_obj.get("conditions", [])
            if isinstance(conds, list) and conds:
                lines = []
                for i, c in enumerate(conds, 1):
                    if isinstance(c, dict):
                        lines.append(f"{i}. {c.get('description', '')}")
                llm_obj["answer"] = "\n".join(lines)

        # For income_analysis: synthesize answer text from items if model didn't return "answer"
        if is_income and not llm_obj.get("answer"):
            synth_lines: List[str] = []
            inc_items = llm_obj.get("income_items", [])
            if isinstance(inc_items, list) and inc_items:
                synth_lines.append("Income items:")
                for i, it in enumerate(inc_items, 1):
                    if isinstance(it, dict):
                        synth_lines.append(f"  {i}. {it.get('description', '')} — "
                                           f"${it.get('amount', 0)} {it.get('frequency', 'monthly')}")
            liab_items = llm_obj.get("liability_items", [])
            if isinstance(liab_items, list) and liab_items:
                synth_lines.append("Liability items:")
                for i, it in enumerate(liab_items, 1):
                    if isinstance(it, dict):
                        synth_lines.append(f"  {i}. {it.get('description', '')} — "
                                           f"${it.get('payment_monthly', 0)}/mo")
            if synth_lines:
                llm_obj["answer"] = "\n".join(synth_lines)

        # Ensure required keys exist
        answer = str(llm_obj.get("answer", "")).strip()
        citations_raw = llm_obj.get("citations", []) or []
        # Enforce citation integrity: keep only citations pointing to retrieved evidence.
        citations = [c for c in citations_raw if (c or {}).get("chunk_id") in allowed_chunk_ids]
        _dprint(f"[DEBUG] citations: {len(citations_raw)} raw -> {len(citations)} after integrity filter "
                f"(allowed_chunk_ids={len(allowed_chunk_ids)})")
        default_conf = 0.5 if llm_obj.get("_truncation_repaired") else 0.3
        confidence = float(llm_obj.get("confidence", default_conf) or default_conf)
        if citations_raw and not citations:
            confidence = min(confidence, 0.2)

        # --- uw_conditions profile: normalize conditions and override confidence ---
        uw_result = None
        if is_uw:
            # Build chunk_id -> metadata index from retrieval pack for source enrichment
            chunk_meta: Dict[str, Dict[str, Any]] = {}
            for ch in retrieved:
                cid = ch.get("chunk_id") or ch.get("payload", {}).get("chunk_id") or ""
                if not cid:
                    continue
                p = ch.get("payload") or {}
                chunk_meta[cid] = {
                    "document_id": p.get("document_id") or ch.get("document_id"),
                    "file_relpath": p.get("file_relpath") or ch.get("file_relpath"),
                    "page_start": p.get("page_start") or ch.get("page_start"),
                    "page_end": p.get("page_end") or ch.get("page_end"),
                }
            uw_result = _normalize_uw_conditions(llm_obj, allowed_chunk_ids, chunk_meta=chunk_meta)
            # -- Deduplication + confidence calibration ---------------------------
            _deduped_conds, _dedup_stats = _dedup_conditions(uw_result["conditions"])
            _raw = _dedup_stats["raw_count"]
            _removed = _dedup_stats["removed_count"]
            if _raw > 0 and _removed / _raw > 0.30:
                uw_result["confidence"] = max(0.3, uw_result["confidence"] - 0.1)
            uw_result["conditions"] = _deduped_conds
            confidence = uw_result["confidence"]
            _dprint(
                f"[DEBUG] uw_conditions: raw={_dedup_stats['raw_count']} "
                f"deduped={_dedup_stats['deduped_count']} "
                f"removed={_dedup_stats['removed_count']} "
                f"top_dup_keys={_dedup_stats['top_dup_keys']} "
                f"confidence={confidence}"
            )

        # --- income_analysis profile: normalize extraction + compute DTI ---
        income_result = None
        dti_result = None
        if is_income:
            # Build chunk_id -> text map for quote backfill
            chunk_text_map: Dict[str, str] = {}
            for ch in retrieved:
                cid = ch.get("chunk_id") or ch.get("payload", {}).get("chunk_id") or ""
                if cid:
                    chunk_text_map[cid] = (ch.get("text") or "").strip()
            income_result = _normalize_income_analysis(llm_obj, allowed_chunk_ids,
                                                       chunk_text_map=chunk_text_map)

            # Deterministic PITIA extraction — regex over evidence text.
            # Takes priority over LLM-extracted proposed_pitia.
            det_pitia = _extract_proposed_pitia_from_retrieval_pack(pack, allowed_chunk_ids)
            if det_pitia["value"] is not None:
                income_result["proposed_pitia"] = det_pitia
                _dprint(f"[DEBUG] using deterministic PITIA: ${det_pitia['value']:,.2f}")
            elif income_result.get("proposed_pitia") is not None:
                _dprint("[DEBUG] using LLM-extracted PITIA (no deterministic match)")
            else:
                _dprint("[DEBUG] no PITIA found (deterministic or LLM)")

            # Deterministic liabilities total extraction — regex over evidence text.
            det_liab = _extract_monthly_liabilities_total_from_retrieval_pack(pack, allowed_chunk_ids)
            if det_liab["value"] is not None:
                income_result["monthly_liabilities_total"] = det_liab
                _dprint(f"[DEBUG] using deterministic liabilities total: ${det_liab['value']:,.2f}")
            else:
                _dprint("[DEBUG] no deterministic liabilities total found")

            # Deterministic income total extraction — regex over evidence text.
            # Takes priority over LLM-extracted income_items summation.
            det_income = _extract_monthly_income_total_from_retrieval_pack(pack, allowed_chunk_ids)
            if det_income["value"] is not None:
                income_result["monthly_income_total"] = det_income
                _dprint(f"[DEBUG] using deterministic income total: ${det_income['value']:,.2f} "
                        f"(source={det_income.get('source')})")
                # Combined self-employed income (multiple P&L businesses)
                if det_income.get("combined_value") is not None:
                    income_result["monthly_income_total_combined"] = {
                        "value": det_income["combined_value"],
                        "citations": det_income.get("combined_citations", []),
                        "source": det_income.get("combined_source"),
                        "components": det_income.get("components", []),
                    }
                    _dprint(f"[DEBUG] using combined income total: "
                            f"${det_income['combined_value']:,.2f} "
                            f"({len(det_income.get('components', []))} businesses)")
            else:
                _dprint("[DEBUG] no deterministic income total found")

            # Re-calibrate confidence if deterministic extractors filled gaps
            has_det_income = det_income["value"] is not None
            has_det_pitia = det_pitia["value"] is not None
            if has_det_income and has_det_pitia:
                income_result["confidence"] = max(income_result["confidence"], 0.7)
            elif has_det_income or has_det_pitia:
                income_result["confidence"] = max(income_result["confidence"], 0.5)

            dti_result = _compute_dti(income_result)
            confidence = income_result["confidence"]
            # Collect all citations from income + liability + proposed_pitia for answer-level citation list
            all_inc_cits: List[Dict[str, Any]] = []
            for it in income_result.get("income_items", []):
                all_inc_cits.extend(it.get("citations", []))
            for it in income_result.get("liability_items", []):
                all_inc_cits.extend(it.get("citations", []))
            pitia_obj = income_result.get("proposed_pitia")
            if isinstance(pitia_obj, dict):
                all_inc_cits.extend(pitia_obj.get("citations", []))
            liab_total_obj = income_result.get("monthly_liabilities_total")
            if isinstance(liab_total_obj, dict):
                all_inc_cits.extend(liab_total_obj.get("citations", []))
            income_total_obj = income_result.get("monthly_income_total")
            if isinstance(income_total_obj, dict):
                all_inc_cits.extend(income_total_obj.get("citations", []))
            income_combined_obj = income_result.get("monthly_income_total_combined")
            if isinstance(income_combined_obj, dict):
                all_inc_cits.extend(income_combined_obj.get("citations", []))
            # Deduplicate by chunk_id (preserve first quote)
            seen_cids: set = set()
            deduped_cits: List[Dict[str, Any]] = []
            for c in all_inc_cits:
                cid = c.get("chunk_id", "")
                if cid and cid not in seen_cids:
                    seen_cids.add(cid)
                    deduped_cits.append(c)
            citations = deduped_cits
            citations_lines = []
            for c in citations:
                cid = c.get("chunk_id", "")
                quote = (c.get("quote", "") or "").strip()
                citations_lines.append(json.dumps({"chunk_id": cid, "quote": quote}, ensure_ascii=False))
            # Enrich answer text with DTI summary
            dti_summary_parts: List[str] = []
            if dti_result:
                dti_summary_parts.append(f"\nDTI Summary (Python-computed):")
                mit = dti_result.get("monthly_income_total")
                mdt = dti_result.get("monthly_debt_total")
                dti_summary_parts.append(f"  Monthly income total: "
                                         f"{'$' + f'{mit:,.2f}' if mit is not None else 'UNKNOWN'}")
                dti_summary_parts.append(f"  Monthly debt total: "
                                         f"{'$' + f'{mdt:,.2f}' if mdt is not None else 'UNKNOWN'}")
                if dti_result.get("back_end_dti") is not None:
                    dti_summary_parts.append(f"  Back-end DTI: {dti_result['back_end_dti']:.4f} "
                                             f"({dti_result['back_end_dti'] * 100:.2f}%)")
                if dti_result.get("front_end_dti") is not None:
                    dti_summary_parts.append(f"  Front-end DTI: {dti_result['front_end_dti']:.4f} "
                                             f"({dti_result['front_end_dti'] * 100:.2f}%)")
                if dti_result.get("monthly_income_combined") is not None:
                    mic = dti_result["monthly_income_combined"]
                    dti_summary_parts.append(f"  Monthly income combined: ${mic:,.2f}")
                if dti_result.get("front_end_dti_combined") is not None:
                    dti_summary_parts.append(f"  Front-end DTI (combined): "
                                             f"{dti_result['front_end_dti_combined']:.4f} "
                                             f"({dti_result['front_end_dti_combined'] * 100:.2f}%)")
                if dti_result.get("back_end_dti_combined") is not None:
                    dti_summary_parts.append(f"  Back-end DTI (combined): "
                                             f"{dti_result['back_end_dti_combined']:.4f} "
                                             f"({dti_result['back_end_dti_combined'] * 100:.2f}%)")
                if dti_result.get("missing_inputs"):
                    dti_summary_parts.append(f"  Missing inputs: {', '.join(dti_result['missing_inputs'])}")
                for note in (dti_result.get("notes") or []):
                    dti_summary_parts.append(f"  Note: {note}")
            answer = (answer + "\n".join(dti_summary_parts)) if dti_summary_parts else answer
            _dprint(f"[DEBUG] income_analysis: {len(income_result['income_items'])} income items, "
                    f"{len(income_result['liability_items'])} liability items, "
                    f"back_end_dti={dti_result.get('back_end_dti')}, confidence={confidence}")

        # Write per-profile artifacts
        prof_out = profiles_dir / profile
        ensure_dir(prof_out)

        answer_md = [
            f"# Answer — profile: {profile}",
            "",
            f"**Question:** {question}",
            "",
            answer or "Not found in provided documents.",
            "",
            "## Citations",
        ]
        citations_lines: List[str] = []
        for c in citations:
            cid = c.get("chunk_id", "")
            quote = (c.get("quote", "") or "").strip()
            answer_md.append(f"- {cid}: {quote[:200]}")
            citations_lines.append(json.dumps({"chunk_id": cid, "quote": quote}, ensure_ascii=False))

        atomic_write_text(prof_out / "answer.md", "\n".join(answer_md) + "\n")
        _answer_extra = _rp_provenance if is_income else {}
        atomic_write_json(prof_out / "answer.json", {
            "answer": answer,
            "citations": citations,
            "confidence": confidence,
            "question": question,
            "profile": profile,
            "retrieval_pack": str(rp_path) if rp_path else None,
            "retrieval_pack_source": rp_source,
            **_answer_extra,
        })
        atomic_write_text(prof_out / "citations.jsonl", "\n".join(citations_lines) + ("\n" if citations_lines else ""))

        if uw_result is not None:
            atomic_write_json(prof_out / "conditions.json", {
                "profile": profile,
                "question": question,
                "conditions": uw_result["conditions"],
                "confidence": uw_result["confidence"],
            })

        if income_result is not None:
            atomic_write_json(prof_out / "income_analysis.json", {
                "profile": profile,
                "question": question,
                "income_items": income_result["income_items"],
                "liability_items": income_result["liability_items"],
                "proposed_pitia": income_result["proposed_pitia"],
                "monthly_liabilities_total": income_result.get("monthly_liabilities_total"),
                "monthly_income_total": income_result.get("monthly_income_total"),
                "monthly_income_total_combined": income_result.get("monthly_income_total_combined"),
                "confidence": income_result["confidence"],
                **_rp_provenance,
            })
        if dti_result is not None:
            dti_result.update(_rp_provenance)
            atomic_write_json(prof_out / "dti.json", dti_result)

        # Primary outputs (first query/profile) go to contracted filenames
        if idx == 0:
            atomic_write_text(out_dir / "answer.md", "\n".join(answer_md) + "\n")
            atomic_write_json(out_dir / "answer.json", {
                "answer": answer,
                "citations": citations,
                "confidence": confidence,
                "question": question,
                "profile": profile,
                "retrieval_pack": str(rp_path) if rp_path else None,
                "retrieval_pack_source": rp_source,
            })
            atomic_write_text(out_dir / "citations.jsonl", "\n".join(citations_lines) + ("\n" if citations_lines else ""))

            # Keep existing stub files for compatibility if you want:
            atomic_write_text(out_dir / "summary.md", "\n".join(answer_md) + "\n")
            atomic_write_json(out_dir / "conditions.json", {"conditions": [], "stub": True, "profile": profile})
            atomic_write_json(out_dir / "extracted_fields.json", {"fields": {}, "stub": True, "profile": profile})

        run_meta.append({
            "profile": profile,
            "question": question,
            "retrieval_pack": str(rp_path) if rp_path else None,
            "retrieval_pack_source": rp_source,
            "evidence_chars": len(evidence_block),
            "ollama_model": args.llm_model,
            "confidence": confidence,
        })

    # Merge preserved profile metadata from previous run
    if _prev_run_meta and _prev_run_meta.get("profiles"):
        current_profile_names = {m["profile"] for m in run_meta}
        for prev_entry in _prev_run_meta["profiles"]:
            if prev_entry.get("profile") not in current_profile_names:
                run_meta.append(prev_entry)

    atomic_write_json(meta_dir / "analysis_run.json", {
        "tenant_id": args.tenant_id,
        "loan_id": args.loan_id,
        "run_id": ctx.run_id,
        "llm_backend": "ollama",
        "llm_model": args.llm_model,
        "profiles": run_meta,
        "auto_retrieve": (not args.no_auto_retrieve),
    })

    ensure_dir(final.parent)
    atomic_rename_dir(staging, final)
    print(f"✓ Step12 complete (ollama): {final}")

if __name__ == "__main__":
    main()
