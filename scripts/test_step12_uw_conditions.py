#!/usr/bin/env python3
"""Tests for uw_conditions deduplication helpers in step12_analyze.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step12_analyze import (
    _make_dedupe_key,
    _token_jaccard,
    _dedup_conditions,
)
