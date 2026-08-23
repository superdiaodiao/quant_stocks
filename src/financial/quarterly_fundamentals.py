"""Point-in-time quarterly profit and sales growth from SEC Company Facts."""

from __future__ import annotations

from pathlib import Path
import weakref

import numpy as np
import pandas as pd

from src.conf import POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
from src.io.security_identity import normalize_point_in_time_tickers

QUARTERLY_METRICS = ("net_income", "revenue")
DIRECT_GROWTH_METRICS = (
    "net_income_ttm",
    "net_income_growth",
    "revenue_ttm",
    "revenue_growth",
)
GROWTH_SNAPSHOT_COLUMNS = [
    "fiscal_end",
    "growth_available_date",
    "financial_age_days",
    "net_income_ttm",
    "net_income_growth",
    "revenue_ttm",
    "revenue_growth",
]
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


def quarterly_profit_ttm_snapshot(
    fundamentals: pd.DataFrame,
    as_of: pd.Timestamp,
    maximum_age_days: int = 200,
) -> pd.DataFrame:
    """Return recent four-quarter profit TTM without revenue or prior growth."""
    cache = _snapshot_cache(fundamentals)
    cache_key = ("profit_ttm", pd.Timestamp(as_of), int(maximum_age_days))
    if cache_key in cache:
        return cache[cache_key].copy()
    direct = fundamentals.loc[
        (fundamentals["available_date"] <= as_of)
        & fundamentals["metric"].eq("net_income_ttm")
    ].copy()
    direct_result = pd.DataFrame()
    if not direct.empty:
        direct_result = (
            direct.sort_values("available_date")
            .drop_duplicates(["ticker", "fiscal_end"], keep="last")
            .sort_values(["ticker", "fiscal_end", "available_date"])
            .groupby("ticker", sort=False)
            .tail(1)
            .set_index("ticker")
            .rename(columns={
                "value": "net_income_ttm",
                "available_date": "financial_available_date",
            })
        )
        direct_result["financial_age_days"] = (
            as_of - direct_result["financial_available_date"]
        ).dt.days
        direct_result = direct_result.loc[
            direct_result["financial_age_days"].between(0, maximum_age_days),
            [
                "fiscal_end", "financial_available_date",
                "financial_age_days", "net_income_ttm",
            ],
        ]

    known = fundamentals.loc[
        (fundamentals["available_date"] <= as_of)
        & fundamentals["metric"].eq("net_income")
    ].copy()
    quarterly_result = pd.DataFrame()
    if not known.empty:
        values = known.sort_values("available_date").drop_duplicates(
            ["ticker", "fiscal_end"], keep="last"
        ).rename(columns={
            "value": "net_income",
            "available_date": "financial_available_date",
        })
        values = values.sort_values(["ticker", "fiscal_end"])
        grouped = values.groupby("ticker", sort=False)
        values["quarter_gap_days"] = grouped["fiscal_end"].diff().dt.days
        values["four_quarter_span_days"] = (
            values["fiscal_end"] - grouped["fiscal_end"].shift(3)
        ).dt.days
        values["net_income_ttm"] = grouped["net_income"].transform(
            lambda series: series.rolling(4, min_periods=4).sum()
        )
        gap_valid = values["quarter_gap_days"].between(70, 110)
        values["four_quarter_gaps_valid"] = (
            gap_valid.groupby(values["ticker"], sort=False)
            .transform(lambda series: series.rolling(3, min_periods=3).sum())
            .eq(3)
        )
        latest_ticker = values.loc[
            values["four_quarter_gaps_valid"]
            & values["four_quarter_span_days"].between(250, 310)
        ].groupby("ticker", sort=False).tail(1).set_index("ticker")
        if not latest_ticker.empty:
            latest_ticker["financial_age_days"] = (
                as_of - latest_ticker["financial_available_date"]
            ).dt.days
            quarterly_result = latest_ticker.loc[
                latest_ticker["financial_age_days"].between(
                    0, maximum_age_days
                ),
                [
                    "fiscal_end", "financial_available_date",
                    "financial_age_days", "net_income_ttm",
                ],
            ]
    combined = pd.concat([quarterly_result, direct_result])
    if combined.empty:
        result = combined
    else:
        result = (
            combined.reset_index()
            .sort_values([
                "ticker", "fiscal_end", "financial_available_date"
            ])
            .groupby("ticker", sort=False)
            .tail(1)
            .set_index("ticker")
        )
    cache[cache_key] = result
    return result.copy()


def quarterly_growth_snapshot(
    fundamentals: pd.DataFrame,
    as_of: pd.Timestamp,
    maximum_age_days: int = 200,
) -> pd.DataFrame:
    """Return TTM profit and sales growth using only facts known by ``as_of``."""
    cache = _snapshot_cache(fundamentals)
    cache_key = ("growth", pd.Timestamp(as_of), int(maximum_age_days))
    if cache_key in cache:
        return cache[cache_key].copy()
    known = fundamentals.loc[
        (fundamentals["available_date"] <= as_of)
        & fundamentals["metric"].isin(QUARTERLY_METRICS)
    ].copy()
    quarterly_result = pd.DataFrame(columns=GROWTH_SNAPSHOT_COLUMNS)
    if not known.empty:
        latest = known.sort_values("available_date").drop_duplicates(
            ["ticker", "fiscal_end", "metric"], keep="last"
        )
        values = latest.pivot_table(
            index=["ticker", "fiscal_end"],
            columns="metric",
            values="value",
            aggfunc="last",
        )
        # One supported metric without the other is a valid missing-data
        # observation.  It cannot form a quarterly growth history.
        if set(QUARTERLY_METRICS).issubset(values.columns):
            values = values.dropna(
                subset=list(QUARTERLY_METRICS)
            ).reset_index()
            availability = latest.groupby(
                ["ticker", "fiscal_end"]
            )["available_date"].max()
            key = pd.MultiIndex.from_frame(values[["ticker", "fiscal_end"]])
            values["growth_available_date"] = availability.reindex(
                key
            ).to_numpy()
            values = values.sort_values(["ticker", "fiscal_end"])
            grouped = values.groupby("ticker", sort=False)
            for metric in QUARTERLY_METRICS:
                values[f"{metric}_ttm"] = grouped[metric].transform(
                    lambda series: series.rolling(4, min_periods=4).sum()
                )
                values[f"prior_{metric}_ttm"] = values.groupby(
                    "ticker", sort=False
                )[f"{metric}_ttm"].shift(4)
                denominator = values[f"prior_{metric}_ttm"].abs().replace(
                    0, np.nan
                )
                values[f"{metric}_growth"] = (
                    values[f"{metric}_ttm"]
                    - values[f"prior_{metric}_ttm"]
                ) / denominator
            values["prior_fiscal_end"] = grouped["fiscal_end"].shift(4)
            year_gap = (
                values["fiscal_end"] - values["prior_fiscal_end"]
            ).dt.days
            latest_ticker = values.loc[
                year_gap.between(330, 400)
            ].groupby("ticker", sort=False).tail(1).set_index("ticker")
            if not latest_ticker.empty:
                latest_ticker["financial_age_days"] = (
                    as_of - latest_ticker["growth_available_date"]
                ).dt.days
                latest_ticker = latest_ticker.loc[
                    latest_ticker["financial_age_days"].between(
                        0, maximum_age_days
                    )
                ]
                quarterly_result = latest_ticker[
                    GROWTH_SNAPSHOT_COLUMNS
                ].replace([np.inf, -np.inf], np.nan).dropna()

    direct = fundamentals.loc[
        (fundamentals["available_date"] <= as_of)
        & fundamentals["metric"].isin(DIRECT_GROWTH_METRICS)
    ].copy()
    direct_result = pd.DataFrame(columns=GROWTH_SNAPSHOT_COLUMNS)
    if not direct.empty:
        bundle_columns = ["ticker", "fiscal_end", "available_date"]
        if "accession" in direct.columns:
            bundle_columns.append("accession")
        latest_direct = direct.sort_values("available_date").drop_duplicates(
            bundle_columns + ["metric"], keep="last"
        )
        direct_values = latest_direct.pivot_table(
            index=bundle_columns,
            columns="metric",
            values="value",
            aggfunc="last",
        )
        if set(DIRECT_GROWTH_METRICS).issubset(direct_values.columns):
            direct_values = direct_values.dropna(
                subset=list(DIRECT_GROWTH_METRICS)
            ).reset_index()
            direct_values["growth_available_date"] = direct_values[
                "available_date"
            ]
            direct_values["financial_age_days"] = (
                as_of - direct_values["growth_available_date"]
            ).dt.days
            direct_values = direct_values.loc[
                direct_values["financial_age_days"].between(
                    0, maximum_age_days
                )
            ]
            direct_values = direct_values.replace(
                [np.inf, -np.inf], np.nan
            ).dropna(subset=list(DIRECT_GROWTH_METRICS))
            direct_result = (
                direct_values.sort_values([
                    "ticker", "fiscal_end", "growth_available_date"
                ])
                .groupby("ticker", sort=False)
                .tail(1)
                .set_index("ticker")[GROWTH_SNAPSHOT_COLUMNS]
            )

    candidates = []
    if not quarterly_result.empty:
        quarterly_result = quarterly_result.copy()
        quarterly_result["_direct_priority"] = 0
        candidates.append(quarterly_result)
    if not direct_result.empty:
        direct_result = direct_result.copy()
        direct_result["_direct_priority"] = 1
        candidates.append(direct_result)
    combined = (
        pd.concat(candidates)
        if candidates
        else pd.DataFrame(columns=GROWTH_SNAPSHOT_COLUMNS)
    )
    if combined.empty:
        result = combined.drop(columns="_direct_priority", errors="ignore")
    else:
        result = (
            combined.reset_index()
            .sort_values([
                "ticker",
                "fiscal_end",
                "growth_available_date",
                "_direct_priority",
            ])
            .groupby("ticker", sort=False)
            .tail(1)
            .set_index("ticker")
            .drop(columns="_direct_priority")
        )

    # Only an explicitly supplied direct TTM state invalidates an older
    # growth package.  A newer standalone quarterly-profit calculation must
    # not suppress the latest complete paired revenue/profit growth window.
    # This keeps the ordinary eight-quarter path unchanged while allowing a
    # source-locked cumulative H1/9M TTM loss to supersede older annual growth.
    direct_profit = fundamentals.loc[
        (fundamentals["available_date"] <= as_of)
        & fundamentals["metric"].eq("net_income_ttm")
    ].copy()
    if not direct_profit.empty:
        direct_profit = (
            direct_profit.sort_values("available_date")
            .drop_duplicates(["ticker", "fiscal_end"], keep="last")
        )
        direct_profit["financial_age_days"] = (
            as_of - direct_profit["available_date"]
        ).dt.days
        direct_profit = (
            direct_profit.loc[
                direct_profit["financial_age_days"].between(
                    0, maximum_age_days
                )
            ]
            .sort_values(["ticker", "fiscal_end", "available_date"])
            .groupby("ticker", sort=False)
            .tail(1)
            .set_index("ticker")
            .rename(columns={
                "available_date": "financial_available_date",
            })
        )
    if not result.empty and not direct_profit.empty:
        aligned_profit = direct_profit.reindex(result.index)
        newer_profit = aligned_profit["fiscal_end"].gt(
            result["fiscal_end"]
        ) | (
            aligned_profit["fiscal_end"].eq(result["fiscal_end"])
            & aligned_profit["financial_available_date"].gt(
                result["growth_available_date"]
            )
        )
        result = result.loc[~newer_profit.fillna(False)]
    cache[cache_key] = result.copy()
    return result
