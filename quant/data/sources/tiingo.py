"""Tiingo end-of-day prices: the API address, the URL form of a ticker and the answer's parser (moved unchanged
from scripts/reversal_data_tiingo.py, phase 3).

The key goes in the Authorization header, never in a URL (``read_env_key`` reads it from ``.env.tiingo``; it is never
printed). The free tier allows 500 unique symbols and 1 GB a month: the budget and the fetch loop stay with the caller
(``pipelines/reversal_data/tiingo.py``), which also owns the cache folder and the request ledger.
"""
from __future__ import annotations

import json
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
