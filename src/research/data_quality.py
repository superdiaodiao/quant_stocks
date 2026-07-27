"""Price-series quality helpers used by stock-level backtests."""

from __future__ import annotations

import pandas as pd

from src.io.terminal_returns import observed_terminal_return_map


def back_adjust_common_splits(close: pd.DataFrame, tolerance: float = 0.025) -> pd.DataFrame:
    """Back-adjust obvious 2:1 through 20:1 forward/reverse split jumps.

    Nasdaq's public historical endpoint is not consistently split-adjusted
    across all symbols. A ratio must be within ``tolerance`` of a common whole
    split factor; ordinary large price moves are otherwise left untouched.
    """
    adjusted = close.copy()
    factors = [1 / n for n in range(2, 21)] + list(range(2, 21))
    for ticker in close.columns:
        series = close[ticker].dropna()
        ratios = series / series.shift(1)
        for split_date, ratio in ratios.dropna().items():
            factor = min(factors, key=lambda candidate: abs(ratio / candidate - 1))
            if abs(ratio / factor - 1) <= tolerance:
                adjusted.loc[adjusted.index < split_date, ticker] *= factor
    return adjusted


def stock_returns_with_delisting_penalty(
    close: pd.DataFrame,
    delisting_return: float = -1.0,
    terminal_returns: dict[tuple[str, pd.Timestamp], float] | None = None,
) -> pd.DataFrame:
    """Calculate returns without treating an ended price history as free cash.

    Interior gaps are treated as suspended sessions: the last price is carried
    forward and the full move is recognized when trading resumes. If a series
    ends before the panel does, the first following panel session receives the
    observed terminal return exactly once when one is available. ``-1`` is
    retained only as an explicit incomplete-data stress fallback.
    Pre-listing rows remain missing and are never penalized.
    """
    if not -1.0 <= delisting_return <= 0.0:
        raise ValueError("delisting_return must be between -1 and 0")
    if terminal_returns is None:
        terminal_returns = observed_terminal_return_map()
    returns = close.ffill().pct_change(fill_method=None)
    for ticker in close.columns:
        last_valid = close[ticker].last_valid_index()
        if last_valid is None:
            continue
        location = close.index.get_indexer([last_valid])[0]
        if location + 1 < len(close.index):
            observed = terminal_returns.get(
                (str(ticker).upper(), pd.Timestamp(last_valid).normalize())
            )
            returns.iloc[location + 1, returns.columns.get_loc(ticker)] = (
                delisting_return if observed is None else observed
            )
    return returns
