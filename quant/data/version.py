"""Which stock-panel data version is read and built (docs/robustness_data_v2.md, docs/reversal_2012_2026_data_plan.md
section 0). The one place the versions are declared: the studies read it, and so does the data pipeline
(``pipelines/reversal_data/common.py``).

``REVERSAL_DATA_VERSION`` selects the inputs and the price cache. It defaults to ``v1`` so every v1 reproduction is
unchanged.

- ``v1``: ``inputs/`` and ``research_cache/reversal_2012_2026`` (frozen, read only).
- ``v2`` (2026-10-05): its own copies, ``inputs_v2/`` and ``reversal_2012_2026_v2``; a study writes to ``<study>_v2``
  output folders and ``<cache>_v2`` derived caches, so a v2 run never overwrites a v1 result or reuses a cache built
  from v1 data.
- ``v2.1`` (2026-10-10): v2 plus the Alpaca SIP fill, again its own copies (``inputs_v2_1/``,
  ``reversal_2012_2026_v2_1``) and ``_v2_1`` study outputs. Studies pre-registered on frozen v2 check ``IS_V2`` and
  refuse it.

Rules and frozen-rule files are never versioned. ``REVERSAL_DATA_MAIN_CHECKOUT`` (testing only) points the versioned
caches at another folder laid out like the main checkout, so an offline rebuild can be checked in a scratch copy;
unset, every path is the main checkout's, as before.
"""
from __future__ import annotations

import os
from pathlib import Path

from quant.paths import MAIN_CHECKOUT, ROOT

VERSIONS = ("v1", "v2", "v2.1")
DATA_VERSION = os.environ.get("REVERSAL_DATA_VERSION", "v1").strip().lower() or "v1"
if DATA_VERSION not in VERSIONS:
    raise ValueError(f"REVERSAL_DATA_VERSION must be v1, v2 or v2.1, not {DATA_VERSION!r}")
IS_V2 = DATA_VERSION == "v2"            # exactly v2 (the frozen robustness copy)
IS_V2_1 = DATA_VERSION == "v2.1"
V2_PLUS = DATA_VERSION in ("v2", "v2.1")  # the v2 fixes, archive fills and third votes apply

CACHE_NAME = {"v1": "reversal_2012_2026", "v2": "reversal_2012_2026_v2", "v2.1": "reversal_2012_2026_v2_1"}
INPUTS_NAME = {"v1": "inputs", "v2": "inputs_v2", "v2.1": "inputs_v2_1"}

DATA_MAIN = Path(os.environ.get("REVERSAL_DATA_MAIN_CHECKOUT") or MAIN_CHECKOUT)
CACHE_ROOT = DATA_MAIN / "research_cache"
INPUTS_PARENT = Path("output/research_only/reversal_2012_2026")   # relative to a checkout
INPUTS = ROOT / INPUTS_PARENT / INPUTS_NAME[DATA_VERSION]
CACHE = CACHE_ROOT / CACHE_NAME[DATA_VERSION]
# the request ledger of the versioned cache (quant.data.sources.http.Ledger)
RAW_INDEX = CACHE / "raw_index.csv.gz"
QUOTA_LEDGER = CACHE / "quota_ledger.csv"


def versioned(path: str | Path) -> Path:
    """``path`` under v1; ``<path>_v2`` under v2 and ``<path>_v2_1`` under v2.1 (same parent, suffix kept on files)."""
    path = Path(path)
    if IS_V2_1:
        tag = "_v2_1"
    elif IS_V2:
        tag = "_v2"
    else:
        return path
    suffixes = "".join(path.suffixes)
    stem = path.name[: len(path.name) - len(suffixes)] if suffixes else path.name
    return path.with_name(f"{stem}{tag}{suffixes}")
