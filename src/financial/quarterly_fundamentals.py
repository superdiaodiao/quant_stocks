"""Point-in-time quarterly profit and sales growth from SEC Company Facts."""

from __future__ import annotations

from pathlib import Path
import weakref

import numpy as np
import pandas as pd

from src.conf import POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
from src.io.security_identity import normalize_point_in_time_tickers

QUARTERLY_METRICS = ("net_income", "revenue")
_SNAPSHOT_CACHES: dict[int, tuple[weakref.ReferenceType, dict]] = {}


def _snapshot_cache(frame: pd.DataFrame) -> dict:
    """Keep cache outside DataFrame.attrs so pandas copies stay lightweight."""
    identity = id(frame)
    cached = _SNAPSHOT_CACHES.get(identity)
    if cached is not None and cached[0]() is frame:
        return cached[1]
    cache: dict = {}
    reference = weakref.ref(
        frame, lambda _reference, key=identity: _SNAPSHOT_CACHES.pop(key, None)
    )
    _SNAPSHOT_CACHES[identity] = (reference, cache)
    return cache


def load_quarterly_fundamentals(
    path: str | Path = POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame["ticker"] = frame["ticker"].astype(str).str.upper()
    frame = frame.rename(columns={"fiscal_end": "period_end"})
    frame = normalize_point_in_time_tickers(frame)
    frame = frame.rename(columns={"period_end": "fiscal_end"})
    for column in ("fiscal_end", "available_date"):
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    return frame.dropna(
        subset=["ticker", "fiscal_end", "available_date", "metric", "value"]
    )


def quarterly_growth_snapshot(
    fundamentals: pd.DataFrame,
    as_of: pd.Timestamp,
    maximum_age_days: int = 200,
) -> pd.DataFrame:
    """Return TTM profit and sales growth using only facts known by ``as_of``."""
    cache = _snapshot_cache(fundamentals)
    cache_key = (pd.Timestamp(as_of), int(maximum_age_days))
    if cache_key in cache:
        return cache[cache_key].copy()
    known = fundamentals.loc[
        (fundamentals["available_date"] <= as_of)
        & fundamentals["metric"].isin(QUARTERLY_METRICS)
    ].copy()
    if known.empty:
        return pd.DataFrame()
    latest = known.sort_values("available_date").drop_duplicates(
        ["ticker", "fiscal_end", "metric"], keep="last"
    )
    values = latest.pivot_table(
        index=["ticker", "fiscal_end"], columns="metric", values="value", aggfunc="last"
    )
    # A Company Facts payload can contain one supported metric without the
    # other (for example, net income facts before an issuer reports an
    # operating-revenue concept).  That is a valid *missing-data* observation,
    # not a malformed frame.  Return an empty snapshot so historical audits
    # can classify it instead of failing with ``KeyError`` while selecting the
    # required metric columns below.
    if not set(QUARTERLY_METRICS).issubset(values.columns):
        return pd.DataFrame()
    values = values.dropna(subset=list(QUARTERLY_METRICS)).reset_index()
    availability = latest.groupby(["ticker", "fiscal_end"])["available_date"].max()
    key = pd.MultiIndex.from_frame(values[["ticker", "fiscal_end"]])
    values["growth_available_date"] = availability.reindex(key).to_numpy()
    values = values.sort_values(["ticker", "fiscal_end"])
    grouped = values.groupby("ticker", sort=False)
    for metric in QUARTERLY_METRICS:
        values[f"{metric}_ttm"] = grouped[metric].transform(
            lambda series: series.rolling(4, min_periods=4).sum()
        )
        values[f"prior_{metric}_ttm"] = values.groupby("ticker", sort=False)[
            f"{metric}_ttm"
        ].shift(4)
        denominator = values[f"prior_{metric}_ttm"].abs().replace(0, np.nan)
        values[f"{metric}_growth"] = (
            values[f"{metric}_ttm"] - values[f"prior_{metric}_ttm"]
        ) / denominator
    values["prior_fiscal_end"] = grouped["fiscal_end"].shift(4)
    year_gap = (values["fiscal_end"] - values["prior_fiscal_end"]).dt.days
    latest_ticker = values.loc[year_gap.between(330, 400)].groupby(
        "ticker", sort=False
    ).tail(1).set_index("ticker")
    if latest_ticker.empty:
        return latest_ticker
    latest_ticker["financial_age_days"] = (
        as_of - latest_ticker["growth_available_date"]
    ).dt.days
    latest_ticker = latest_ticker.loc[
        latest_ticker["financial_age_days"].between(0, maximum_age_days)
    ]
    columns = [
        "fiscal_end", "growth_available_date", "financial_age_days",
        "net_income_ttm", "net_income_growth", "revenue_ttm", "revenue_growth",
    ]
    result = latest_ticker[columns].replace([np.inf, -np.inf], np.nan).dropna()
    cache[cache_key] = result.copy()
    return result
