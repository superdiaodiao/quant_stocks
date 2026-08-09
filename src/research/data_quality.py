"""Price-series quality helpers used by stock-level backtests."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.conf import PROJECT_PATH
from src.io.terminal_returns import observed_terminal_return_map


CONFIRMED_PRICE_ADJUSTMENTS_FILE = (
    Path(PROJECT_PATH)
    / "stocks_list_dir/nasdaq/confirmed_price_adjustments.csv"
)


def load_confirmed_price_adjustments(
    path: str | Path = CONFIRMED_PRICE_ADJUSTMENTS_FILE,
) -> pd.DataFrame:
    """Load sourced split/distribution factors used by live price adjustment."""
    path = Path(path)
    columns = [
        "ticker", "effective_date", "adjustment_factor",
        "action_type", "source_url", "verified_at",
    ]
    if not path.exists():
        return pd.DataFrame(columns=columns)
    frame = pd.read_csv(path)
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(
            "Confirmed price adjustments missing columns: "
            + ", ".join(sorted(missing))
        )
    if frame["source_url"].fillna("").str.strip().eq("").any():
        raise ValueError("Confirmed price adjustments require source URLs")
    return frame[columns]


def detect_common_split_events(
    close: pd.DataFrame, tolerance: float = 0.025
) -> pd.DataFrame:
    """Return price jumps that the heuristic would treat as common splits."""
    factors = [1 / n for n in range(2, 21)] + list(range(2, 21))
    rows = []
    for ticker in close.columns:
        ratios = close[ticker].dropna().pct_change(fill_method=None).add(1)
        for split_date, ratio in ratios.dropna().items():
            factor = min(
                factors, key=lambda candidate: abs(ratio / candidate - 1)
            )
            relative_error = abs(ratio / factor - 1)
            if relative_error <= tolerance:
                rows.append({
                    "ticker": str(ticker),
                    "split_date": pd.Timestamp(split_date),
                    "raw_price_ratio": float(ratio),
                    "matched_factor": float(factor),
                    "relative_error": float(relative_error),
                })
    return pd.DataFrame(rows, columns=[
        "ticker", "split_date", "raw_price_ratio", "matched_factor",
        "relative_error",
    ])


def back_adjust_common_splits(
    close: pd.DataFrame,
    tolerance: float = 0.025,
    confirmed_actions: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Back-adjust obvious 2:1 through 20:1 forward/reverse split jumps.

    Nasdaq's public historical endpoint is not consistently split-adjusted
    across all symbols. A ratio must be within ``tolerance`` of a common whole
    split factor; ordinary large price moves are otherwise left untouched.
    """
    adjusted = close.copy()
    sourced = (
        load_confirmed_price_adjustments()
        if confirmed_actions is None else confirmed_actions.copy()
    )
    sourced_keys: set[tuple[str, pd.Timestamp]] = set()
    if len(sourced) and len(close.index):
        sourced["ticker"] = sourced["ticker"].astype(str).str.upper()
        sourced["effective_date"] = pd.to_datetime(
            sourced["effective_date"], errors="raise"
        )
        known_columns = {str(column).upper() for column in close.columns}
        sourced = sourced.loc[
            sourced["ticker"].isin(known_columns)
            & sourced["effective_date"].le(close.index.max())
        ]
        if len(sourced):
            adjusted = apply_confirmed_price_adjustments(
                adjusted, sourced
            )
            sourced_keys = set(zip(
                sourced["ticker"],
                sourced["effective_date"].dt.normalize(),
            ))
    events = detect_common_split_events(close, tolerance)
    for event in events.itertuples(index=False):
        if (
            str(event.ticker).upper(),
            pd.Timestamp(event.split_date).normalize(),
        ) in sourced_keys:
            continue
        adjusted.loc[
            adjusted.index < event.split_date, event.ticker
        ] *= event.matched_factor
    return adjusted


def apply_confirmed_price_adjustments(
    close: pd.DataFrame, actions: pd.DataFrame
) -> pd.DataFrame:
    """Back-adjust prices using explicitly sourced corporate-action factors.

    ``actions`` must contain ``ticker``, ``effective_date``, and
    ``adjustment_factor``. The factor is applied only to observations before
    the effective date. This supports confirmed splits and special cash
    distributions without inferring either from the size of a price move.
    """
    required = {"ticker", "effective_date", "adjustment_factor"}
    missing = required - set(actions.columns)
    if missing:
        raise ValueError(
            "Corporate actions missing required columns: "
            + ", ".join(sorted(missing))
        )
    adjusted = close.copy()
    normalized = actions.copy()
    normalized["ticker"] = normalized["ticker"].astype(str).str.upper()
    normalized["effective_date"] = pd.to_datetime(
        normalized["effective_date"], errors="coerce"
    )
    normalized["adjustment_factor"] = pd.to_numeric(
        normalized["adjustment_factor"], errors="coerce"
    )
    invalid = (
        normalized["effective_date"].isna()
        | normalized["adjustment_factor"].isna()
        | normalized["adjustment_factor"].le(0)
    )
    if invalid.any():
        raise ValueError("Corporate actions contain invalid dates or factors")
    column_map = {str(column).upper(): column for column in close.columns}
    unknown = sorted(set(normalized["ticker"]) - set(column_map))
    if unknown:
        raise ValueError(
            "Corporate actions reference unknown tickers: " + ", ".join(unknown)
        )
    for action in normalized.sort_values(["effective_date", "ticker"]).itertuples(
        index=False
    ):
        ticker = column_map[action.ticker]
        adjusted.loc[
            adjusted.index < action.effective_date, ticker
        ] *= float(action.adjustment_factor)
    return adjusted


def restore_contemporaneous_prices(
    close: pd.DataFrame, validated_actions: pd.DataFrame
) -> pd.DataFrame:
    """Undo premature provider adjustments until the real split date.

    Some refreshed Nasdaq histories switch to post-split units before the
    corporate action becomes effective. For minimum-price eligibility, restore
    the nominal price between the provider boundary (``split_date``) and the
    sourced ``confirmed_action_date``. Return and momentum calculations should
    continue to use a separately back-adjusted continuous series.
    """
    required = {
        "ticker", "split_date", "confirmed_action_date",
        "confirmed_action_type", "confirmed_adjustment_factor",
    }
    missing = required - set(validated_actions.columns)
    if missing:
        raise ValueError(
            "Validated actions missing required columns: "
            + ", ".join(sorted(missing))
        )
    restored = close.copy()
    actions = validated_actions.loc[
        validated_actions["confirmed_action_type"].eq(
            "PROVIDER_ADJUSTMENT_DISCONTINUITY"
        )
    ].copy()
    actions["split_date"] = pd.to_datetime(
        actions["split_date"], errors="coerce"
    )
    actions["confirmed_action_date"] = pd.to_datetime(
        actions["confirmed_action_date"], errors="coerce"
    )
    actions["confirmed_adjustment_factor"] = pd.to_numeric(
        actions["confirmed_adjustment_factor"], errors="coerce"
    )
    invalid = (
        actions["split_date"].isna()
        | actions["confirmed_action_date"].isna()
        | actions["confirmed_adjustment_factor"].isna()
        | actions["confirmed_adjustment_factor"].le(0)
    )
    if invalid.any():
        raise ValueError("Validated provider discontinuities are invalid")
    column_map = {str(column).upper(): column for column in close.columns}
    for action in actions.itertuples(index=False):
        ticker = column_map.get(str(action.ticker).upper())
        if ticker is None:
            continue
        mask = (
            restored.index >= action.split_date
        ) & (
            restored.index < action.confirmed_action_date
        )
        restored.loc[mask, ticker] /= float(
            action.confirmed_adjustment_factor
        )
    return restored


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
