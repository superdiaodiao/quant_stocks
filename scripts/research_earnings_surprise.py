#!/usr/bin/env python3
"""Run the earnings-surprise (SUE) test once on 2012-2019 (docs/earnings_surprise_plan.md).

Same universe, inputs and frozen replay (no stops) as the ROA test; only
the ranking differs: standardized unexpected earnings of the latest
quarter first reported within the last 63 sessions.

    PYTHONPATH=. python scripts/research_earnings_surprise.py run
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import research_holdout_2011_2019 as holdout
from scripts import research_quality_roa as roa
from scripts import research_v26_large_liquid_stock_momentum as v26
from src.research import prospective_replay

OUTPUT = Path("output/research_only/earnings_surprise")
ANNOUNCEMENT_WINDOW = 63
HISTORY_CHANGES = 8
MINIMUM_CHANGES = 4
HOLDINGS = (10, 30)
PRIMARY_HOLDINGS = 10


def first_reported_net_income(quarterly: pd.DataFrame) -> pd.DataFrame:
    """One row per (ticker, fiscal_end): the value and date it was first reported."""
    income = quarterly.loc[quarterly["metric"].eq("net_income")]
    income = income.sort_values(["ticker", "fiscal_end", "available_date"])
    first = income.groupby(["ticker", "fiscal_end"], as_index=False).first()
    return first[["ticker", "fiscal_end", "available_date", "value"]]


def _year_ago(quarters: pd.DataFrame, fiscal_end: pd.Timestamp) -> float | None:
    gap = (fiscal_end - quarters["fiscal_end"]).dt.days
    match = quarters.loc[gap.between(350, 380)]
    return None if match.empty else float(match["value"].iloc[-1])


def sue_at(income: pd.DataFrame, signal: pd.Timestamp, window_start: pd.Timestamp) -> pd.Series:
    known = income.loc[income["available_date"].le(signal)]
    scores = {}
    for ticker, quarters in known.groupby("ticker"):
        quarters = quarters.sort_values("fiscal_end")
        latest = quarters.iloc[-1]
        if latest["available_date"] < window_start:
            continue
        prior = _year_ago(quarters, latest["fiscal_end"])
        if prior is None:
            continue
        current = float(latest["value"]) - prior
        changes = []
        for _, row in quarters.iloc[-2::-1].iterrows():
            earlier = _year_ago(quarters, row["fiscal_end"])
            if earlier is not None:
                changes.append(float(row["value"]) - earlier)
            if len(changes) == HISTORY_CHANGES:
                break
        if len(changes) < MINIMUM_CHANGES:
            continue
        history = np.array(changes)
        deviation = history.std(ddof=1)
        if deviation > 0:
            scores[ticker] = current / deviation
    return pd.Series(scores, dtype=float)


def build_targets(inputs: dict) -> tuple[dict[int, pd.DataFrame], list[dict]]:
    close = inputs["close"]
    eligibility = inputs["eligibility_close"]
    dollar_volume = inputs["dollar_volume"]
    history = close.notna().cumsum()
    income = first_reported_net_income(inputs["quarterly"])
    signals = [
        s for s in v26.scheduled_signal_dates(close.index, "2011-12-01", "2019-12-31", "monthly")
        if roa.FIRST_SIGNAL <= s <= roa.LAST_SIGNAL
    ]
    rows = {n: [] for n in HOLDINGS}
    coverage = []
    for signal in signals:
        position = close.index.get_loc(signal)
        effective = v26.next_trading_date(close.index, signal)
        names = sorted(set(inputs["universe"](signal)) & set(close.columns))
        price = eligibility.loc[signal, names]
        ok = price.ge(roa.MINIMUM_PRICE) & history.loc[signal, names].ge(roa.MINIMUM_HISTORY)
        liquidity = dollar_volume.iloc[position - 49 : position + 1][names].median()
        pool = liquidity.loc[ok[ok].index].dropna().nlargest(roa.POOL).index
        window_start = close.index[max(0, position - ANNOUNCEMENT_WINDOW)]
        sue = sue_at(income.loc[income["ticker"].isin(pool)], signal, window_start).reindex(pool).dropna()
        coverage.append({"signal_date": signal.strftime("%Y-%m-%d"), "pool": int(len(pool)), "with_sue": int(len(sue))})
        ranked = sue.sort_values(ascending=False)
        for n in HOLDINGS:
            for ticker in ranked.index[:n]:
                rows[n].append({"effective_date": effective, "ticker": ticker,
                                "target_weight": 1.0 / n, "base_transaction_cost_bps": 10.0,
                                "signal_date": signal, "sue": float(ranked[ticker])})
    return {n: pd.DataFrame(r) for n, r in rows.items()}, coverage


def run() -> dict:
    if OUTPUT.exists() and any(OUTPUT.iterdir()):
        raise RuntimeError(f"SUE results will not be overwritten: {OUTPUT}")
    checks = holdout.check()
    inputs = holdout.load_inputs()
    targets, coverage = build_targets(inputs)
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
        "plan": "docs/earnings_surprise_plan.md",
        "checks": checks,
        "verdict": "REJECTED" if primary <= 0.0 else "NOT_REJECTED",
        "sue_coverage_of_pool": {
            "median": float((covered["with_sue"] / covered["pool"]).median()),
            "minimum": float((covered["with_sue"] / covered["pool"]).min()),
        },
        "results": results,
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for n, schedule in targets.items():
        schedule.to_csv(OUTPUT / f"targets_top{n}.csv", index=False)
    for (n, cost), daily in dailies.items():
        daily.to_csv(OUTPUT / f"daily_top{n}_{cost}bps.csv", index_label="date")
    covered.to_csv(OUTPUT / "sue_coverage.csv", index=False)
    (OUTPUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("run",))
    parser.parse_args()
    summary = run()
    print(json.dumps({k: summary[k] for k in ("verdict", "sue_coverage_of_pool")}, indent=2))


if __name__ == "__main__":
    main()
