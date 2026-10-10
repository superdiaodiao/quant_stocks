#!/usr/bin/env python3
"""Audit a low-beta defensive overlay on the time-frozen walk-forward model.

This is retrospective research.  The overlay family and the reported 42/45
session ensemble were inspected on the full history, so no result from this
script is independent forward evidence or authorization to trade.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_BASE = Path(
    "output/can_slim_walk_forward_daily_quarterly_financials_"
    "financial_age_150_365_550_proven_only_bank_v3_13d77de9.csv"
)
DEFAULT_QQQ = Path("output/research_only/qqq_nasdaq_history.csv")
DEFAULT_PREFIX = Path("output/research_v6_walkforward_defensive_ensemble")
LOOKBACKS = (42, 45)
TREND_WINDOW = 100
STOCK_WEIGHT = 0.25


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prior_month_allocations(
    stock_return: pd.Series,
    qqq_close: pd.Series,
    *,
    relative_strength_window: int,
    trend_window: int,
    stock_weight: float,
    cadence: str = "monthly",
    confirmation_periods: int = 1,
) -> pd.DataFrame:
    if relative_strength_window <= 1 or trend_window <= 1:
        raise ValueError("signal windows must exceed one session")
    if not 0 < stock_weight < 1:
        raise ValueError("stock_weight must be in (0, 1)")
    close = qqq_close.reindex(stock_return.index).ffill()
    stock_trailing = (
        (1.0 + stock_return).rolling(relative_strength_window).apply(
            np.prod, raw=True
        )
        - 1.0
    )
    qqq_trailing = close.div(close.shift(relative_strength_window)).sub(1.0)
    qqq_trend_on = close.gt(
        close.rolling(trend_window, min_periods=trend_window).mean()
    )
    if cadence == "monthly":
        period_keys = pd.Series(
            stock_return.index.to_period("M").astype(str), index=stock_return.index
        )
    elif cadence == "weekly":
        period_keys = pd.Series(
            stock_return.index.to_period("W-FRI").astype(str),
            index=stock_return.index,
        )
    elif cadence == "biweekly":
        anchor = pd.Timestamp("2021-01-04")
        period_keys = pd.Series(
            ((stock_return.index.normalize() - anchor).days // 14).astype(str),
            index=stock_return.index,
        )
    else:
        raise ValueError("cadence must be monthly, biweekly, or weekly")
    if confirmation_periods < 1:
        raise ValueError("confirmation_periods must be positive")
    signals = pd.DataFrame({
        "stock_leads": stock_trailing.ge(qqq_trailing),
        "qqq_trend_on": qqq_trend_on,
        "period_key": period_keys,
    }).groupby("period_key", sort=False).last()
    keys = list(signals.index)
    allocation: dict[str, dict[str, float | bool | str]] = {}
    active_risk_on = False
    pending_risk_on = False
    pending_count = 0
    for position, key in enumerate(keys):
        prior = (
            signals.iloc[position - 1]
            if position
            else pd.Series({"stock_leads": False, "qqq_trend_on": False})
        )
        desired_risk_on = bool(prior["stock_leads"] or prior["qqq_trend_on"])
        if desired_risk_on == active_risk_on:
            pending_risk_on = desired_risk_on
            pending_count = 0
        else:
            if desired_risk_on == pending_risk_on:
                pending_count += 1
            else:
                pending_risk_on = desired_risk_on
                pending_count = 1
            if pending_count >= confirmation_periods:
                active_risk_on = desired_risk_on
                pending_count = 0
        risk_on = active_risk_on
        allocation[key] = {
            "stock_weight": stock_weight,
            "qqq_weight": 1.0 - stock_weight if risk_on else 0.0,
            "risk_on": risk_on,
            "rebalance_key": key,
        }
    return pd.DataFrame(
        [allocation[key] for key in period_keys],
        index=stock_return.index,
    )


def simulate_sleeve(
    base: pd.DataFrame,
    qqq_close: pd.Series,
    qqq_dividend: pd.Series,
    *,
    relative_strength_window: int,
    trend_window: int = TREND_WINDOW,
    stock_weight: float = STOCK_WEIGHT,
    transaction_cost_bps: float = 30.0,
    cadence: str = "monthly",
    confirmation_periods: int = 1,
) -> pd.DataFrame:
    if transaction_cost_bps < 10 or not np.isfinite(transaction_cost_bps):
        raise ValueError("transaction_cost_bps must be finite and at least 10")
    frame = base.sort_index().copy()
    close = qqq_close.reindex(frame.index)
    missing = close.isna()
    if missing.any():
        allowed = frame.loc[missing, ["strategy", "benchmark", "turnover"]]
        if not allowed.fillna(0.0).eq(0.0).all(axis=None):
            raise ValueError("QQQ is missing on a non-zero market session")
        close = close.ffill()
    dividend = qqq_dividend.reindex(frame.index).fillna(0.0)
    qqq_return = close.add(dividend).div(close.shift(1)).sub(1.0).fillna(0.0)
    allocation = prior_month_allocations(
        frame["strategy"], close,
        relative_strength_window=relative_strength_window,
        trend_window=trend_window,
        stock_weight=stock_weight,
        cadence=cadence,
        confirmation_periods=confirmation_periods,
    )
    incremental_base_cost = (
        frame["turnover"].fillna(0.0)
        * (transaction_cost_bps - 10.0) / 10_000.0
    )
    stock_return = frame["strategy"] - incremental_base_cost

    stock = qqq = cash = 0.0
    nav = 1.0
    previous_period = None
    rows: list[dict] = []
    for stamp in frame.index:
        period = str(allocation.loc[stamp, "rebalance_key"])
        sleeve_turnover = sleeve_cost = 0.0
        if previous_period is not None:
            stock *= 1.0 + float(stock_return.loc[stamp])
            qqq *= 1.0 + float(qqq_return.loc[stamp])
            nav = stock + qqq + cash
        if period != previous_period:
            target_stock = nav * float(allocation.loc[stamp, "stock_weight"])
            target_qqq = nav * float(allocation.loc[stamp, "qqq_weight"])
            sleeve_turnover = abs(target_stock - stock) + abs(target_qqq - qqq)
            sleeve_cost = sleeve_turnover * transaction_cost_bps / 10_000.0
            nav -= sleeve_cost
            stock = nav * float(allocation.loc[stamp, "stock_weight"])
            qqq = nav * float(allocation.loc[stamp, "qqq_weight"])
            cash = nav - stock - qqq
        nav = stock + qqq + cash
        rows.append({
            "date": stamp,
            "nav": nav,
            "risk_on": bool(allocation.loc[stamp, "risk_on"]),
            "stock_weight": float(allocation.loc[stamp, "stock_weight"]),
            "qqq_weight": float(allocation.loc[stamp, "qqq_weight"]),
            "sleeve_turnover": sleeve_turnover,
            "sleeve_transaction_cost": sleeve_cost,
        })
        previous_period = period
    result = pd.DataFrame(rows).set_index("date")
    result["return"] = result["nav"].pct_change().fillna(result["nav"].iloc[0] - 1.0)
    result["benchmark_return"] = frame["benchmark"]
    result["qqq_return"] = qqq_return
    result["drawdown"] = result["nav"].div(result["nav"].cummax()).sub(1.0)
    return result


def combine_sleeves(sleeves: dict[int, pd.DataFrame]) -> pd.DataFrame:
    if not sleeves:
        raise ValueError("at least one sleeve is required")
    returns = pd.concat(
        {window: frame["return"] for window, frame in sleeves.items()}, axis=1
    )
    benchmark = next(iter(sleeves.values()))["benchmark_return"]
    qqq_return = next(iter(sleeves.values()))["qqq_return"]
    result = pd.DataFrame({
        "return": returns.mean(axis=1),
        "benchmark_return": benchmark,
        "qqq_return": qqq_return,
        "risk_on_sleeves": pd.concat(
            {window: frame["risk_on"].astype(int) for window, frame in sleeves.items()},
            axis=1,
        ).sum(axis=1),
    })
    result["nav"] = (1.0 + result["return"]).cumprod()
    result["drawdown"] = result["nav"].div(result["nav"].cummax()).sub(1.0)
    return result


def summarize(result: pd.DataFrame) -> dict:
    annual = (1.0 + result[["return", "benchmark_return", "qqq_return"]]).groupby(
        result.index.year
    ).prod() - 1.0
    excess = annual["return"] - annual["benchmark_return"]
    excess_qqq = annual["return"] - annual["qqq_return"]
    neutralized = result["return"].copy()
    neutralized.loc[neutralized.idxmax()] = 0.0
    neutralized_annual = (1.0 + neutralized).groupby(neutralized.index.year).prod() - 1.0
    neutralized_excess = neutralized_annual - annual["benchmark_return"]
    years = (result.index[-1] - result.index[0]).days / 365.25
    performance = {}
    for name, column in {
        "strategy": "return",
        "qqq": "qqq_return",
        "nasdaq_composite": "benchmark_return",
    }.items():
        series = result[column]
        nav = (1.0 + series).cumprod()
        maximum_drawdown = float(nav.div(nav.cummax()).sub(1.0).min())
        cagr = float(nav.iloc[-1] ** (1.0 / years) - 1.0)
        annualized_volatility = float(series.std() * np.sqrt(252.0))
        performance[name] = {
            "cagr": cagr,
            "annualized_volatility": annualized_volatility,
            "zero_rate_sharpe": float(
                series.mean() / series.std() * np.sqrt(252.0)
            ),
            "maximum_drawdown": maximum_drawdown,
            "calmar": cagr / -maximum_drawdown,
        }
    return {
        "wins_vs_nasdaq": int(excess.gt(0.0).sum()),
        "years": int(len(excess)),
        "median_excess_vs_nasdaq": float(excess.median()),
        "minimum_excess_vs_nasdaq": float(excess.min()),
        "wins_vs_qqq": int(excess_qqq.gt(0.0).sum()),
        "median_excess_vs_qqq": float(excess_qqq.median()),
        "minimum_excess_vs_qqq": float(excess_qqq.min()),
        "maximum_drawdown": float(result["drawdown"].min()),
        "largest_daily_return": float(result["return"].max()),
        "largest_daily_return_date": result["return"].idxmax().strftime("%Y-%m-%d"),
        "wins_after_largest_day_neutralized": int(neutralized_excess.gt(0.0).sum()),
        "minimum_excess_after_largest_day_neutralized": float(
            neutralized_excess.min()
        ),
        "annual_excess_vs_nasdaq": {
            str(int(year)): float(value) for year, value in excess.items()
        },
        "annual_excess_vs_qqq": {
            str(int(year)): float(value) for year, value in excess_qqq.items()
        },
        "full_period_performance": performance,
    }


def run(base_path: Path, qqq_path: Path, prefix: Path) -> dict:
    base = pd.read_csv(base_path, parse_dates=["date"]).set_index("date")
    qqq = pd.read_csv(qqq_path, parse_dates=["date"]).set_index("date")
    summaries = {}
    primary = None
    for cost in (30.0, 50.0):
        sleeves = {
            window: simulate_sleeve(
                base, qqq["close"], qqq.get("cash_dividend", pd.Series(dtype=float)),
                relative_strength_window=window,
                transaction_cost_bps=cost,
            )
            for window in LOOKBACKS
        }
        combined = combine_sleeves(sleeves)
        combined.to_csv(prefix.with_name(f"{prefix.name}_{int(cost)}bps_daily.csv"))
        summaries[str(int(cost))] = summarize(combined)
        if cost == 30.0:
            primary = combined
    assert primary is not None
    sensitivity = []
    for left, right in ((40, 42), (42, 45), (45, 50), (40, 45), (42, 50)):
        combined = combine_sleeves({
            window: simulate_sleeve(
                base, qqq["close"], qqq.get("cash_dividend", pd.Series(dtype=float)),
                relative_strength_window=window,
                transaction_cost_bps=50.0,
            )
            for window in (left, right)
        })
        sensitivity.append({"lookbacks": [left, right], **summarize(combined)})
    cadence_results = {}
    for label, cadence, confirmation_periods in (
        ("monthly", "monthly", 1),
        ("biweekly", "biweekly", 1),
        ("weekly", "weekly", 1),
        ("weekly_confirm_2", "weekly", 2),
        ("weekly_confirm_3", "weekly", 3),
    ):
        combined = combine_sleeves({
            window: simulate_sleeve(
                base, qqq["close"], qqq.get("cash_dividend", pd.Series(dtype=float)),
                relative_strength_window=window,
                transaction_cost_bps=50.0,
                cadence=cadence,
                confirmation_periods=confirmation_periods,
            )
            for window in LOOKBACKS
        })
        cadence_results[label] = summarize(combined)
    payload = {
        "model_version": "can-slim-v6-walkforward-defensive-ensemble-research",
        "research_only": True,
        "historical_selection_contaminated": True,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "configuration": {
            "base": "annual time-frozen CAN SLIM walk-forward ensemble",
            "relative_strength_windows": list(LOOKBACKS),
            "qqq_trend_window": TREND_WINDOW,
            "stock_weight_per_sleeve": STOCK_WEIGHT,
            "qqq_weight_when_risk_on": 1.0 - STOCK_WEIGHT,
            "decision": "prior completed month end",
            "execution": "next monthly close boundary",
            "sleeve_weights": [0.5, 0.5],
        },
        "cost_results": summaries,
        "neighbor_pair_sensitivity_at_50bps": sensitivity,
        "cadence_sensitivity_at_50bps": cadence_results,
        "selection_warning": (
            "The overlay family and 42/45 pair were chosen after inspecting the "
            "same 2022-2026 history. Historical results are diagnostic only."
        ),
        "inputs": {
            "base_daily": {"path": str(base_path), "sha256": _sha256(base_path)},
            "qqq": {"path": str(qqq_path), "sha256": _sha256(qqq_path)},
        },
    }
    prefix.with_name(prefix.name + "_summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--qqq", type=Path, default=DEFAULT_QQQ)
    parser.add_argument("--prefix", type=Path, default=DEFAULT_PREFIX)
    args = parser.parse_args()
    print(json.dumps(run(args.base, args.qqq, args.prefix), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
