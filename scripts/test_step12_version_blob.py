#!/usr/bin/env python3
"""Tests for _build_version_blob and _SCHEMA_VERSIONS in step12_analyze.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step12_analyze import (
    _build_version_blob,
    _SCHEMA_VERSIONS,
)
