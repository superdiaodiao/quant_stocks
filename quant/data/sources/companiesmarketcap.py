"""companiesmarketcap.com: which search result or guessed page belongs to a ticker (moved unchanged from
scripts/data_source_probe.py, phase 3).

A delisted name takes only a ``<T>.defunct.<year>`` entry within a year of its delisting (the bare ``<T>`` is today's
holder of a reused ticker); a guessed slug is built from the company name. The fetches (one request every 3 s) and
their caches stay with the callers (``pipelines/reversal_data/source_probe.py``, ``v2_archive.py``; the mega-cap
OOS.2 pages in ``quant.data.sources.megacap_oos2``).
"""
from __future__ import annotations

import re

BASE = "https://companiesmarketcap.com"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120 Safari/537.36"}

def cmc_pick(results: list[dict], ticker: str, delist_year: int | None) -> dict | None:
    """The search result for ``ticker``. A delisted name takes only '<T>.defunct.<year>' within a year of its
    delisting (the bare '<T>' is today's holder of a reused ticker); a listed name takes only the bare '<T>'."""
    best, score = None, None
    for x in results:
        ident = str(x.get("identifier", ""))
        m = re.fullmatch(rf"{re.escape(ticker)}(?:\.defunct\.(\d{{4}}))?", ident, re.I)
        if not m or x.get("type") != "stock":
            continue
        yr = int(m.group(1)) if m.group(1) else None
        if delist_year:
            if yr is None or abs(yr - delist_year) > 1:
                continue
            s = abs(yr - delist_year)
        else:
            if yr is not None:
                continue
            s = 0
        if score is None or s < score:
            best, score = x, s
    return best


CMC_SUFFIX = r"(?i)\b(inc|corp|corporation|ltd|limited|holdings?|co|company|plc|llc|group|the|n\.?v|s\.?a|de)\b\.?"


def cmc_slugs(name: str) -> list[str]:
    """Slug guesses from a company name: 'BMC SOFTWARE INC' -> ['bmc-software', 'bmc']."""
    base = re.sub(r"/[A-Z]{2}/?$", "", str(name))
    base = re.sub(CMC_SUFFIX, " ", base)
    words = [w for w in re.sub(r"[^a-z0-9]+", " ", base.lower().replace("&", " and ")).split() if w]
    out = []
    for k in (len(words), 2, 1):
        if 0 < k <= len(words):
            slug = "-".join(words[:k])
            if slug not in out:
                out.append(slug)
    return out
