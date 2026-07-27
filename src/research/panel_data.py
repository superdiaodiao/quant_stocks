"""Load aligned stock panels for research and recommendation workflows."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.research.universe_history import known_non_common_symbols


def load_panel(
    price_dir: str | Path, start: str, end: str | None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    close, dollar_volume, _, _ = load_ohlc_panel(
        price_dir, start, end, require_ohlc=False
    )
    return close, dollar_volume


def load_ohlc_panel(
    price_dir: str | Path,
    start: str,
    end: str | None,
    *,
    require_ohlc: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    closes, dollar_volumes, highs, lows = {}, {}, {}, {}
    excluded = known_non_common_symbols()
    warmup = pd.Timestamp(start) - pd.Timedelta(days=400)
    required = {"close", "volume", "high", "low"} if require_ohlc else {
        "close", "volume"
    }
    for path in Path(price_dir).glob("*.csv"):
        frame = pd.read_csv(path, index_col="date", parse_dates=True)
        if not required.issubset(frame.columns):
            continue
        frame = frame.loc[frame.index >= warmup]
        if end:
            frame = frame.loc[frame.index <= pd.Timestamp(end)]
        ticker = path.stem.upper()
        if len(frame) < 150 or ticker in excluded:
            continue
        closes[ticker] = frame["close"]
        dollar_volumes[ticker] = frame["close"] * frame["volume"]
        if {"high", "low"}.issubset(frame.columns):
            highs[ticker] = frame["high"]
            lows[ticker] = frame["low"]
    return (
        pd.DataFrame(closes).sort_index(),
        pd.DataFrame(dollar_volumes).sort_index(),
        pd.DataFrame(highs).sort_index(),
        pd.DataFrame(lows).sort_index(),
    )
