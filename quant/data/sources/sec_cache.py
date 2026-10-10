"""SEC / Yahoo / Wayback helpers of the spin-off and Nasdaq-100 studies: cached, rate-limited GETs (moved unchanged
from scripts/research_spinoffs_sec.py, phase 2; docs/research_ledger_spinoffs.md).

Every SEC request sends the contact from ``src/io/sec_contact.py`` (never printed) and waits on a
limiter of 5 requests a second (SEC's fair-access ceiling is 10). Bodies are cached gzip-compressed
under ``research_cache/spinoffs/raw/`` and never fetched twice; a 404 leaves a ``.404`` marker.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import threading
import time
import zlib
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from quant.paths import CACHE_ROOT
from src.io.sec_contact import sec_user_agent

CACHE = CACHE_ROOT / "spinoffs"
RAW = CACHE / "raw"


class Limiter:
    def __init__(self, per_second: float):
        self.gap = 1.0 / per_second
        self.last = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            delay = self.last + self.gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()


SEC_LIMIT = Limiter(5)
YAHOO_LIMIT = Limiter(0.5)   # one request per 2 seconds, as scripts/reversal_data_yahoo.py
WAYBACK_LIMIT = Limiter(1)


def _key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:20]


def cached_get(url: str, sub: str, *, headers: dict, limiter: Limiter, name: str | None = None,
               max_bytes: int | None = None, retries: int = 4, offline: bool = False) -> bytes | None:
    """Body of ``url`` (cached under RAW/sub); None on a 404. ``max_bytes`` keeps only the start of a long
    document: the transfer stops after that many (compressed) bytes and the truncated body is cached."""
    path = RAW / sub / ((name or _key(url)) + ".gz")
    marker = path.with_name(path.name + ".404")
    if path.exists():
        return gzip.decompress(path.read_bytes())
    if marker.exists():
        return None
    if offline:
        raise FileNotFoundError(f"not cached: {url}")
    h = dict(headers)
    err = None
    for attempt in range(retries):
        limiter.wait()
        try:
            with urlopen(Request(url, headers=h), timeout=60) as r:
                data = r.read(max_bytes) if max_bytes else r.read()
                gz = r.headers.get("Content-Encoding") == "gzip"
            if gz:
                data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(gzip.compress(data, mtime=0))
            os.replace(tmp, path)
            return data
        except HTTPError as e:
            if e.code == 404:
                path.parent.mkdir(parents=True, exist_ok=True)
                marker.write_bytes(b"")
                return None
            if e.code in (401, 403) and "sec.gov" not in url:
                raise
            err = e
            time.sleep(5 * (attempt + 1) * (3 if e.code in (403, 429) else 1))
        except Exception as e:  # network trouble
            err = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{url}: {err}")


def sec_headers() -> dict:
    return {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip"}


def sec_get(url: str, sub: str = "sec", **kw) -> bytes | None:
    return cached_get(url, sub, headers=sec_headers(), limiter=SEC_LIMIT, **kw)


def sec_json(url: str, sub: str = "sec", **kw):
    data = sec_get(url, sub, **kw)
    return None if data is None else json.loads(data)


def efts(params: dict, **kw) -> dict:
    url = "https://efts.sec.gov/LATEST/search-index?" + urlencode(params)
    return sec_json(url, "efts", **kw)


def submissions(cik: int, **kw) -> dict | None:
    """The full submissions record (recent + older pages merged into ``filings``)."""
    base = sec_json(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", "submissions", **kw)
    if base is None:
        return None
    rec = base["filings"]["recent"]
    cols = list(rec)
    out = {c: list(rec[c]) for c in cols}
    for f in base["filings"].get("files", []):
        page = sec_json("https://data.sec.gov/submissions/" + f["name"], "submissions", **kw)
        if page:
            for c in cols:
                out[c] += list(page.get(c, [None] * len(page["accessionNumber"])))
    base["all_filings"] = out
    return base


# ------------------------------------------------------------------ Yahoo (owner accepted the terms risk, 2026-10-02)

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_PERIOD1 = 1293840000   # 2011-01-01


def yahoo_chart(symbol: str, period2: int = 1791072000, offline: bool = False) -> dict | None:
    """Daily v8 chart (splits, dividends, adjusted close) from 2011-01-01; cached as
    RAW/yahoo/{symbol}.gz. One request per 2 seconds; a 404 (no data / delisted) is cached as a marker.
    ``period2`` defaults to 2026-10-04 so a re-run asks the same window."""
    url = (YAHOO_CHART.format(symbol=symbol) + "?" +
           urlencode({"period1": YAHOO_PERIOD1, "period2": period2, "interval": "1d",
                      "events": "div,splits", "includeAdjustedClose": "true"}))
    data = cached_get(url, "yahoo", headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                      limiter=YAHOO_LIMIT, name=symbol.replace("/", "_"), offline=offline)
    if data is None:
        return None
    j = json.loads(data)
    res = (j.get("chart") or {}).get("result")
    return res[0] if res else None
