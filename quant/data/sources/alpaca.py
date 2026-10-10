"""Alpaca market data (SIP daily bars and corporate actions): the API address and the body parsers (moved unchanged
from scripts/reversal_data_v2_1_alpaca.py, phase 3).

The key pair is read from ``.env.alpaca`` inside Python and sent in headers, never printed. Paging, the request
budget (``PER_MINUTE``), the cache layout and the entity rules A1-A7 stay with the caller
(``pipelines/reversal_data/v2_1_alpaca.py``).
"""
from __future__ import annotations

import json

import pandas as pd

BASE = "https://data.alpaca.markets"


ADJUSTMENTS = ("raw", "split", "all")


CA_TYPES = "forward_split,reverse_split,unit_split,stock_dividend,cash_dividend,spin_off,name_change"


def bars_frame(body: bytes | None, symbol: str) -> pd.DataFrame:
    """date, close, volume from one /v2/stocks/bars body."""
    if not body:
        return pd.DataFrame(columns=["date", "close", "volume"])
    data = json.loads(body)
    rows = (data.get("bars") or {}).get(symbol) or []
    if not rows:
        return pd.DataFrame(columns=["date", "close", "volume"])
    f = pd.DataFrame({"date": pd.to_datetime([r["t"][:10] for r in rows]),
                      "close": [float(r["c"]) for r in rows], "volume": [float(r.get("v") or 0) for r in rows]})
    return f.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def corporate_actions(body: bytes | None) -> dict[str, list[dict]]:
    if not body:
        return {}
    return (json.loads(body).get("corporate_actions") or {})
