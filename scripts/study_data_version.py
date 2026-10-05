"""Which data version the stock-level strategy studies read (docs/robustness_data_v2.md).

``REVERSAL_DATA_VERSION`` (the same variable as scripts/reversal_data_common.py) selects the inputs and the price
cache. It defaults to ``v1``, so every v1 reproduction is unchanged. With ``v2`` the studies read the version-2
copies and write to ``<study>_v2`` output folders and ``<cache>_v2`` derived caches, so a v2 run never overwrites a
v1 result or reuses a cache built from v1 data. Rules, parameters and frozen-rule files are not versioned: a v2 run
reads the frozen rules of the v1 study unchanged (data plan section 0: version 2 is a robustness check only).

Kept free of imports so any study can use it (reversal_data_common needs the SEC contact config at import).
"""
from __future__ import annotations

import os
from pathlib import Path

DATA_VERSION = os.environ.get("REVERSAL_DATA_VERSION", "v1").strip().lower() or "v1"
if DATA_VERSION not in ("v1", "v2"):
    raise ValueError(f"REVERSAL_DATA_VERSION must be v1 or v2, not {DATA_VERSION!r}")
IS_V2 = DATA_VERSION == "v2"

MAIN_CHECKOUT = Path("/Users/bytedance/code/quant_stocks")
ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / ("output/research_only/reversal_2012_2026/" + ("inputs_v2" if IS_V2 else "inputs"))
CACHE = MAIN_CHECKOUT / "research_cache" / ("reversal_2012_2026_v2" if IS_V2 else "reversal_2012_2026")


def versioned(path: str | Path) -> Path:
    """``path`` under v1; ``<path>_v2`` (same parent, suffix kept on files) under v2."""
    path = Path(path)
    if not IS_V2:
        return path
    suffixes = "".join(path.suffixes)
    stem = path.name[: len(path.name) - len(suffixes)] if suffixes else path.name
    return path.with_name(f"{stem}_v2{suffixes}")
