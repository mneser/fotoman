"""Isolate Fotoman library paths before the package computes DATA_DIR."""

from __future__ import annotations

import os
import tempfile

_data = tempfile.mkdtemp(prefix="fotoman-test-data-")
_cache = tempfile.mkdtemp(prefix="fotoman-test-cache-")
os.environ["FOTOMAN_DATA"] = _data
os.environ["FOTOMAN_CACHE"] = _cache
