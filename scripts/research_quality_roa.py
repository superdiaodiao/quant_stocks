#!/usr/bin/env python3
"""Run the ROA quality test once on 2012-2019 (docs/quality_roa_plan.md).

Reuses the 2011-2019 holdout's verified inputs (prices, nominal prices,
Nasdaq listing status, quarterly financials, terminal returns) and the
frozen ``replay_live`` without stops. Total assets come from annual 10-K
facts parsed from the local SEC companyfacts caches.

    PYTHONPATH=. python scripts/research_quality_roa.py run
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import research_holdout_2011_2019 as holdout
from scripts import research_v26_large_liquid_stock_momentum as v26
from src.financial.quarterly_fundamentals import latest_four_quarter_profit
from src.research import prospective_replay

ASSETS = holdout.CACHE / "annual_assets.csv"
OUTPUT = Path("output/research_only/quality_roa")
FIRST_SIGNAL = pd.Timestamp("2011-12-30")
LAST_SIGNAL = pd.Timestamp("2019-11-29")
MINIMUM_PRICE = 5.0
MINIMUM_HISTORY = 252
POOL = 100
MAXIMUM_AGE_DAYS = 550
HOLDINGS = (10, 30)
PRIMARY_HOLDINGS = 10


def load_assets() -> pd.DataFrame:
    frame = pd.read_csv(ASSETS, parse_dates=["fiscal_end", "available_date"])
    frame = frame.loc[frame["form"].astype(str).str.startswith("10-K") & frame["value"].gt(0)]
    frame["ticker"] = frame["ticker"].astype(str).str.upper()
    return frame.sort_values(["ticker", "available_date", "fiscal_end"])


def latest_assets(assets: pd.DataFrame, as_of: pd.Timestamp) -> pd.Series:
    known = assets.loc[assets["available_date"].le(as_of)]
    latest = known.sort_values(["fiscal_end", "available_date"]).groupby("ticker").tail(1)
    latest = latest.loc[(as_of - latest["available_date"]).dt.days.le(MAXIMUM_AGE_DAYS)]
    return latest.set_index("ticker")["value"]


def build_targets(inputs: dict, assets: pd.DataFrame) -> tuple[dict[int, pd.DataFrame], list[dict]]:
    close = inputs["close"]
    eligibility = inputs["eligibility_close"]
    dollar_volume = inputs["dollar_volume"]
    history = close.notna().cumsum()
    signals = [
        s for s in v26.scheduled_signal_dates(close.index, "2011-12-01", "2019-12-31", "monthly")
        if FIRST_SIGNAL <= s <= LAST_SIGNAL
    ]
    rows = {n: [] for n in HOLDINGS}
    coverage = []
    for signal in signals:
        position = close.index.get_loc(signal)
        effective = v26.next_trading_date(close.index, signal)
        names = sorted(set(inputs["universe"](signal)) & set(close.columns))
        price = eligibility.loc[signal, names]
        ok = price.ge(MINIMUM_PRICE) & history.loc[signal, names].ge(MINIMUM_HISTORY)
        liquidity = dollar_volume.iloc[position - 49 : position + 1][names].median()
        pool = liquidity.loc[ok[ok].index].dropna().nlargest(POOL).index
        income = latest_four_quarter_profit(inputs["quarterly"], signal, MAXIMUM_AGE_DAYS)["net_income_ttm"]
        roa = (income / latest_assets(assets, signal)).reindex(pool).dropna()
        coverage.append({"signal_date": signal.strftime("%Y-%m-%d"), "pool": int(len(pool)), "with_roa": int(len(roa))})
        ranked = roa.sort_values(ascending=False)
        for n in HOLDINGS:
            for ticker in ranked.index[:n]:
                rows[n].append({"effective_date": effective, "ticker": ticker,
                                "target_weight": 1.0 / n, "base_transaction_cost_bps": 10.0,
                                "signal_date": signal, "roa": float(ranked[ticker])})
    return {n: pd.DataFrame(r) for n, r in rows.items()}, coverage


def run() -> dict:
    if OUTPUT.exists() and any(OUTPUT.iterdir()):
        raise RuntimeError(f"ROA results will not be overwritten: {OUTPUT}")
    checks = holdout.check()
    inputs = holdout.load_inputs()
    targets, coverage = build_targets(inputs, load_assets())
    validation = holdout._with_loud_market_moves(inputs)
    qqq = holdout.qqq_returns()
    results, dailies = {}, {}
    with holdout.terminal_returns():
        for n, schedule in targets.items():
            for cost in (50, 10):
                daily = prospective_replay.replay_live(
                    inputs["raw_close"], inputs["nasdaq"], schedule, schedule["effective_date"].min(), holdout.END,
                    validation=validation,
                    entry_loss_fraction=holdout.NO_STOP, portfolio_stop_fraction=holdout.NO_STOP,
                    transaction_cost_bps=float(cost),
                )
                dailies[(n, cost)] = daily
                results[f"top{n}_{cost}bps"] = holdout.metrics(daily, qqq, "2012-01-01", holdout.END)
    primary = results[f"top{PRIMARY_HOLDINGS}_50bps"]["annualized_excess_vs_qqq"]
    covered = pd.DataFrame(coverage)
    summary = {
        "plan": "docs/quality_roa_plan.md",
        "checks": checks,
        "verdict": "REJECTED" if primary <= 0.0 else "NOT_REJECTED",
        "roa_coverage_of_pool": {
            "median": float((covered["with_roa"] / covered["pool"]).median()),
            "minimum": float((covered["with_roa"] / covered["pool"]).min()),
        },
        "results": results,
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for n, schedule in targets.items():
        schedule.to_csv(OUTPUT / f"targets_top{n}.csv", index=False)
    for (n, cost), daily in dailies.items():
        daily.to_csv(OUTPUT / f"daily_top{n}_{cost}bps.csv", index_label="date")
    covered.to_csv(OUTPUT / "roa_coverage.csv", index=False)
    (OUTPUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("run",))
    parser.parse_args()
    summary = run()
    print(json.dumps({k: summary[k] for k in ("verdict", "roa_coverage_of_pool")}, indent=2))


if __name__ == "__main__":
    main()
