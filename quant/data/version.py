"""Which stock-panel data version a study reads (docs/robustness_data_v2.md).

Same contract as scripts/study_data_version.py (from which this was extracted): ``REVERSAL_DATA_VERSION`` selects the
inputs and the price cache. It defaults to ``v1`` so every v1 reproduction is unchanged. With ``v2`` a study reads
the version-2 copies and writes to ``<study>_v2`` output folders and ``<cache>_v2`` derived caches, so a v2 run never
overwrites a v1 result or reuses a cache built from v1 data. Rules and frozen-rule files are never versioned.

Version 2.1 is being built by the data pipeline (docs/reversal_2012_2026_data_plan.md); it is added here, in one
place, when that build is accepted.
"""
from __future__ import annotations

import os
from pathlib import Path

from quant.paths import CACHE_ROOT, ROOT

DATA_VERSION = os.environ.get("REVERSAL_DATA_VERSION", "v1").strip().lower() or "v1"
if DATA_VERSION not in ("v1", "v2"):
    raise ValueError(f"REVERSAL_DATA_VERSION must be v1 or v2, not {DATA_VERSION!r}")
IS_V2 = DATA_VERSION == "v2"

INPUTS = ROOT / ("output/research_only/reversal_2012_2026/" + ("inputs_v2" if IS_V2 else "inputs"))
CACHE = CACHE_ROOT / ("reversal_2012_2026_v2" if IS_V2 else "reversal_2012_2026")


def versioned(path: str | Path) -> Path:
    """``path`` under v1; ``<path>_v2`` (same parent, suffix kept on files) under v2."""
    path = Path(path)
    if not IS_V2:
        return path
    suffixes = "".join(path.suffixes)
    stem = path.name[: len(path.name) - len(suffixes)] if suffixes else path.name
    return path.with_name(f"{stem}_v2{suffixes}")
