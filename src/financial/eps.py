"""Point-in-time EPS history and derived EPS signals only."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from src.conf import POINT_IN_TIME_EPS_FILE
from src.io.security_identity import normalize_point_in_time_tickers


def load_eps_history(path: str | Path = POINT_IN_TIME_EPS_FILE) -> pd.DataFrame:
    eps = pd.read_csv(path)
    eps["ticker"] = eps["ticker"].astype(str).str.upper()
    eps = normalize_point_in_time_tickers(eps)
    eps["period_end"] = pd.to_datetime(eps["period_end"], errors="coerce")
    eps["available_date"] = pd.to_datetime(eps["available_date"], errors="coerce")
    eps["quarterly_eps"] = pd.to_numeric(eps["quarterly_eps"], errors="coerce")
    eps = eps.dropna(subset=["ticker", "period_end", "available_date", "quarterly_eps"])
    eps = eps.sort_values(["ticker", "period_end", "available_date"])
    eps["trailing_eps"] = eps.groupby("ticker")["quarterly_eps"].transform(
        lambda values: values.rolling(4, min_periods=4).sum()
    )
    eps["prior_trailing_eps"] = eps.groupby("ticker")["trailing_eps"].shift(4)
    denominator = eps["prior_trailing_eps"].abs().replace(0, np.nan)
    eps["eps_growth"] = (
        (eps["trailing_eps"] - eps["prior_trailing_eps"]) / denominator
    ).clip(-2, 2)
    return eps


def eps_snapshot(
    eps: pd.DataFrame, as_of: pd.Timestamp, maximum_age_days: int
) -> pd.DataFrame:
    available = eps.loc[eps["available_date"] <= as_of].copy()
    if available.empty:
        return pd.DataFrame(
            columns=["trailing_eps", "eps_growth", "available_date", "financial_age_days"]
        )
    latest = (
        available.sort_values(["ticker", "available_date", "period_end"])
        .groupby("ticker", sort=False)
        .tail(1)
        .set_index("ticker")
    )
    latest["financial_age_days"] = (as_of - latest["available_date"]).dt.days
    latest = latest.loc[latest["financial_age_days"] <= maximum_age_days]
    return latest[
        ["trailing_eps", "eps_growth", "available_date", "financial_age_days", "source"]
    ]
