"""Where code, outputs and the large local caches live.

``ROOT`` is the checkout this code runs from (a worktree has its own). Raw vendor data and derived caches are big and
not in git, so every checkout reads them from the main checkout's ``research_cache`` (the same absolute paths the
study scripts have always used).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_CHECKOUT = Path("/Users/bytedance/code/quant_stocks")
CACHE_ROOT = MAIN_CHECKOUT / "research_cache"
OUTPUT_ROOT = ROOT / "output/research_only"


def output_dir(name: str) -> Path:
    """``output/research_only/<name>`` in this checkout."""
    return OUTPUT_ROOT / name
