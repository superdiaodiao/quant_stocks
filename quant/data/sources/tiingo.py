"""Tiingo end-of-day prices: the API address, the URL form of a ticker and the answer's parser (moved unchanged
from scripts/reversal_data_tiingo.py, phase 3).

The key goes in the Authorization header, never in a URL (``read_env_key`` reads it from ``.env.tiingo``; it is never
printed). The free tier allows 500 unique symbols and 1 GB a month: the fetch loop stays with the caller
(``pipelines/reversal_data/tiingo.py``), which also owns the cache folder and the request ledger.

The 500 symbols are the account's, shared by every caller, while each caller logs to its own ledger (since 2026-10-11:
the pipeline's version ledgers, the mega-cap studies' ledgers, the forward observation's). So the monthly count is
taken across all of them (``month_symbols``, ``check_budget``) and every Tiingo caller asks it before a request:

- ``pipelines/reversal_data/tiingo.py`` (the reversal data fetcher; its budget is this count plus ``--already-used``);
- ``quant/data/sources/megacap_oos2.py`` ``tiingo_daily`` and ``quant/data/megacap_lookback.py`` ``tiingo_pre`` (only
  symbols already counted this month, i.e. no new symbol);
- ``quant/observation/runner.py`` ``fetch`` (the forward observation, B1 / B2, from 2026-11; on GitHub Actions only its
  own ledger is visible, so the local callers stop at 480 and leave its 20 symbols free).
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

import pandas as pd

from quant.data.sources.http import redact

API = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"


QUOTA_WORDS = re.compile(r"limit|allocation|exceed|run over|upgrade|too many|quota", re.IGNORECASE)


PRICE_FIELDS = ["date", "open", "high", "low", "close", "volume", "adjOpen", "adjHigh", "adjLow", "adjClose",
                "adjVolume", "divCash", "splitFactor"]


def url_ticker(ticker: str) -> str:
    """Tiingo's URL form: lower case, share-class dots as dashes (BRK.B -> brk-b)."""
    return ticker.strip().lower().replace(".", "-").replace("/", "-")


def parse_body(data: bytes) -> tuple[list | None, str]:
    """(price rows, "") for a list answer; (None, error text) for an error object or bad JSON."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "not JSON: " + redact(data[:200].decode("utf-8", "replace"))
    if isinstance(payload, list):
        return payload, ""
    if isinstance(payload, dict):
        text = str(payload.get("detail") or payload.get("message") or payload.get("error") or payload)
        return None, redact(text[:300])
    return None, f"unexpected JSON type {type(payload).__name__}"


def is_quota_text(text: str) -> bool:
    return bool(QUOTA_WORDS.search(text or ""))


def to_frame(prices: list) -> pd.DataFrame:
    """Tiingo rows as a frame: ``close``/``volume`` as traded, ``adj*`` adjusted, ``divCash`` as paid,
    ``splitFactor`` new shares per old share on the ex-date."""
    frame = pd.DataFrame(prices)
    if not len(frame):
        return pd.DataFrame(columns=PRICE_FIELDS)
    frame = frame.reindex(columns=PRICE_FIELDS)
    frame["date"] = pd.to_datetime(frame["date"].astype(str).str[:10])
    for column in PRICE_FIELDS[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


# ======================================================================== the account's monthly symbol budget

MONTHLY_SYMBOLS = 500     # the free tier's unique symbols a month, for the whole account
SOURCE = "tiingo"


def account_ledgers() -> list[Path]:
    """Every request ledger that can hold Tiingo requests: ``research_cache/*/quota_ledger.csv`` (the pipeline's
    version caches, its fill and probe caches, the mega-cap studies, the forward observation) and the forward
    observation's ledger under ``$FORWARD_OBS_CACHE`` (GitHub Actions). Missing files are left out."""
    from quant.data import version as dv   # late: keeps this module importable without the data version set up

    paths = sorted(dv.CACHE_ROOT.glob("*/quota_ledger.csv"))
    if os.environ.get("FORWARD_OBS_CACHE"):
        paths.append(Path(os.environ["FORWARD_OBS_CACHE"]) / "quota_ledger.csv")
    seen, out = set(), []
    for p in paths:
        key = os.path.realpath(p)
        if key not in seen and p.is_file():
            seen.add(key)
            out.append(p)
    return out


def ledger_symbols(path: str | Path, month: str, source: str = SOURCE) -> set[str]:
    """Symbols (upper case) a quota ledger (``fetched_utc,source,month,symbol,status``) logged for ``source`` in
    ``month``; every logged request counts, whatever its status (Tiingo counts the symbol when it is asked)."""
    path = Path(path)
    if not path.is_file():
        return set()
    out = set()
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("source") == source and (row.get("month") or (row.get("fetched_utc") or "")[:7]) == month:
                symbol = (row.get("symbol") or "").strip().upper()
                if symbol:
                    out.add(symbol)
    return out


def this_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def month_symbols(month: str | None = None, ledgers: list | None = None, extra=()) -> set[str]:
    """Unique Tiingo symbols this account asked in ``month`` (default: this UTC month), across ``ledgers``
    (default ``account_ledgers()``) plus ``extra`` (a caller's own ledger, if it is somewhere else)."""
    month = month or this_month()
    paths = list(account_ledgers() if ledgers is None else ledgers) + [Path(p) for p in extra]
    seen, out = set(), set()
    for p in paths:
        key = os.path.realpath(p)
        if key in seen:
            continue
        seen.add(key)
        out |= ledger_symbols(p, month)
    return out


def check_budget(symbols, month: str | None = None, stop: int = MONTHLY_SYMBOLS, already_used: int = 0,
                 ledgers: list | None = None, extra=()) -> tuple[list[str], list[str]]:
    """(allowed, refused): the ``symbols`` that may be asked now without the account's count of unique symbols this
    month (all ledgers + ``already_used``, the symbols the account page shows that no ledger has) going over ``stop``
    (at most ``MONTHLY_SYMBOLS``). A symbol already counted this month costs nothing; new ones are allowed in order
    while there is room."""
    if stop > MONTHLY_SYMBOLS:
        raise ValueError(f"stop {stop} is above the free tier's {MONTHLY_SYMBOLS} symbols a month")
    counted = month_symbols(month, ledgers, extra)
    room = stop - len(counted) - int(already_used or 0)
    allowed, refused = [], []
    for s in symbols:
        u = str(s).strip().upper()
        if u in counted:
            allowed.append(s)
        elif room > 0:
            allowed.append(s)
            counted.add(u)
            room -= 1
        else:
            refused.append(s)
    return allowed, refused
