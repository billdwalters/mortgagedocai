#!/usr/bin/env python3
"""Tests for _ingest_jsonl_file and _load_chunk_text_index in step13_build_retrieval_pack.py."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# qdrant_client is only used inside main(); mock it so step13 imports cleanly
# on dev machines that don't have the full production dependency stack.
for _mod in (
    "qdrant_client",
    "qdrant_client.http",
    "qdrant_client.http.models",
):
    sys.modules.setdefault(_mod, MagicMock())

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step13_build_retrieval_pack import _ingest_jsonl_file, _load_chunk_text_index
import step13_build_retrieval_pack as _m
