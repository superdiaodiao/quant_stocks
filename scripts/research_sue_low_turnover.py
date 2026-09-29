#!/usr/bin/env python3
"""Low-turnover SUE test for a $10k IBKR Pro Tiered account (docs/earnings_surprise_low_turnover_plan.md).

Selection is ledger item 5's (``research_earnings_surprise.build_targets``,
top 10). This replay holds shares, trades only on changes (sell names that
leave the list, split all cash across names that join it) and charges each
order per docs/cost_model_ibkr_tiered.md.

    PYTHONPATH=. python scripts/research_sue_low_turnover.py validate
    PYTHONPATH=. python scripts/research_sue_low_turnover.py run
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import research_earnings_surprise as sue
from scripts import research_holdout_2011_2019 as holdout
from src.io.terminal_returns import observed_terminal_return_map
from src.research import prospective_replay

OUTPUT = Path("output/research_only/sue_low_turnover")
START_CASH = 10_000.0
HOLDINGS = 10

# docs/cost_model_ibkr_tiered.md
COMMISSION_PER_SHARE = 0.0035
COMMISSION_MINIMUM = 0.35
COMMISSION_CAP = 0.01
EXCHANGE_CLEARING_PER_SHARE = 0.0005
REGULATORY_SELL = 0.00003
SPREAD = 0.0002
STRESS = 0.005


def order_cost(notional: float, price: float, sell: bool, model: str) -> float:
    if notional <= 0:
        return 0.0
    if model == "zero":
        return 0.0
    if model == "stress":
        return STRESS * notional
    shares = notional / price
    commission = min(max(COMMISSION_PER_SHARE * shares, COMMISSION_MINIMUM), COMMISSION_CAP * notional)
    other = EXCHANGE_CLEARING_PER_SHARE * shares + SPREAD * notional + (REGULATORY_SELL * notional if sell else 0.0)
    return commission + other


def _buy_notional(budget: float, price: float, model: str) -> float:
    """Largest order whose notional plus cost fits the budget."""
    notional = budget
    for _ in range(30):
        notional = budget - order_cost(notional, price, False, model)
    return max(notional, 0.0)


def simulate(close: pd.DataFrame, nominal: pd.DataFrame, targets: pd.DataFrame, terminal: dict,
             end: str, model: str, rebalance: bool = False) -> pd.DataFrame:
    """Daily book for a target schedule; ``rebalance`` restores equal weights each month (validation)."""
    schedule = {day: list(group["ticker"]) for day, group in targets.groupby("effective_date")}
    dates = close.loc[targets["effective_date"].min():end].index
    last_valid = close.apply(lambda column: column.last_valid_index())
    carried = close.ffill()
    cash, units = START_CASH, {}
    rows, orders, costs = [], 0, 0.0
    previous_nav = START_CASH
    for day in dates:
        # Series that ended before today leave at their terminal return.
        for ticker in list(units):
            end_day = last_valid.get(ticker)
            if end_day is not None and end_day < day:
                last = float(close.at[end_day, ticker])
                ratio = terminal.get((ticker, pd.Timestamp(end_day).normalize()), -1.0)
                cash += units.pop(ticker) * last * (1.0 + ratio)
        price_now = carried.loc[day]
        turnover = 0.0
        if day in schedule:
            wanted = [t for t in schedule[day] if t != "__CASH__"]
            tradable = lambda t: t in close.columns and pd.notna(close.at[day, t])
            def nominal_price(t):
                value = nominal.at[day, t] if t in nominal.columns else np.nan
                return float(value) if pd.notna(value) and value > 0 else float(close.at[day, t])
            nav = cash + sum(u * float(price_now[t]) for t, u in units.items())
            if rebalance:
                goal = {t: nav / HOLDINGS for t in wanted if tradable(t)}
                for t in list(units):
                    if not tradable(t):
                        continue
                    value = units[t] * float(close.at[day, t])
                    target_value = goal.get(t, 0.0)
                    if value > target_value:
                        sold = value - target_value
                        fee = order_cost(sold, nominal_price(t), True, model)
                        cash += sold - fee; costs += fee; orders += 1; turnover += sold
                        units[t] = target_value / float(close.at[day, t])
                        if units[t] <= 1e-12:
                            units.pop(t)
                for t, target_value in goal.items():
                    value = units.get(t, 0.0) * float(close.at[day, t])
                    if target_value > value + 1e-9:
                        need = min(target_value - value, cash)
                        fee = order_cost(need, nominal_price(t), False, model)
                        bought = need - fee
                        cash -= need; costs += fee; orders += 1; turnover += bought
                        units[t] = units.get(t, 0.0) + bought / float(close.at[day, t])
            else:
                for t in [t for t in units if t not in wanted]:
                    if not tradable(t):
                        continue  # suspended: sell when it trades again
                    value = units[t] * float(close.at[day, t])
                    fee = order_cost(value, nominal_price(t), True, model)
                    cash += value - fee; costs += fee; orders += 1; turnover += value
                    units.pop(t)
                new = [t for t in wanted if t not in units and tradable(t)]
                if new and cash > 0:
                    budget = cash / len(new)
                    for t in new:
                        notional = _buy_notional(budget, nominal_price(t), model)
                        fee = budget - notional
                        cash -= budget; costs += fee; orders += 1; turnover += notional
                        units[t] = notional / float(close.at[day, t])
        nav = cash + sum(u * float(price_now[t]) for t, u in units.items())
        rows.append({"date": day, "nav": nav, "cash": cash, "holdings": len(units),
                     "turnover": turnover / previous_nav if previous_nav else 0.0})
        previous_nav = nav
    book = pd.DataFrame(rows).set_index("date")
    book.attrs["orders"], book.attrs["costs"] = orders, costs
    return book


def as_daily(book: pd.DataFrame, nasdaq: pd.Series) -> pd.DataFrame:
    """Shape a book like replay_live's output so holdout.metrics can read it."""
    daily = pd.DataFrame(index=book.index)
    daily["strategy"] = book["nav"].pct_change().fillna(book["nav"].iloc[0] / START_CASH - 1.0)
    daily["benchmark"] = nasdaq.reindex(book.index).ffill().pct_change().fillna(0.0)
    daily["invested"] = 1.0 - book["cash"] / book["nav"]
    daily["turnover"] = book["turnover"]
    daily["stock_stop_exits"] = 0
    daily["portfolio_stop_exits"] = 0
    return daily


def _terminal() -> dict:
    merged = observed_terminal_return_map()
    merged.update(observed_terminal_return_map(holdout.INPUTS / "terminal_returns_supplement.csv"))
    return merged


def validate() -> dict:
    """Rebalanced, zero-cost mode against the frozen replay_live on the same targets."""
    inputs = holdout.load_inputs()
    targets = sue.build_targets(inputs)[0][HOLDINGS]
    book = simulate(inputs["close"], inputs["eligibility_close"], targets, _terminal(), holdout.END, "zero", rebalance=True)
    mine = book["nav"].pct_change().dropna()
    with holdout.terminal_returns():
        frozen = prospective_replay.replay_live(
            inputs["raw_close"], inputs["nasdaq"], targets, targets["effective_date"].min(), holdout.END,
            validation=holdout._with_loud_market_moves(inputs),
            entry_loss_fraction=holdout.NO_STOP, portfolio_stop_fraction=holdout.NO_STOP, transaction_cost_bps=0.0,
        )["strategy"]
    common = mine.index.intersection(frozen.index)
    difference = (mine.loc[common] - frozen.loc[common]).abs()
    return {
        "sessions": int(len(common)),
        "median_abs_daily_difference": float(difference.median()),
        "p99_abs_daily_difference": float(difference.quantile(0.99)),
        "growth_ratio_new_over_frozen": float((1 + mine.loc[common]).prod() / (1 + frozen.loc[common]).prod()),
    }


def run() -> dict:
    if OUTPUT.exists() and any(OUTPUT.iterdir()):
        raise RuntimeError(f"results will not be overwritten: {OUTPUT}")
    checks = holdout.check()
    inputs = holdout.load_inputs()
    targets = sue.build_targets(inputs)[0][HOLDINGS]
    qqq = holdout.qqq_returns()
    terminal = _terminal()
    results, books = {}, {}
    for model in ("ibkr_tiered", "stress"):
        book = simulate(inputs["close"], inputs["eligibility_close"], targets, terminal, holdout.END, model)
        books[model] = book
        m = holdout.metrics(as_daily(book, inputs["nasdaq"]), qqq, "2012-01-01", holdout.END)
        years = m["sessions"] / 252.0
        m["orders_per_year"] = book.attrs["orders"] / years
        m["total_costs_usd"] = book.attrs["costs"]
        m["final_nav_usd"] = float(book["nav"].iloc[-1])
        results[model] = m
    primary = results["ibkr_tiered"]["annualized_excess_vs_qqq"]
    summary = {
        "plan": "docs/earnings_surprise_low_turnover_plan.md",
        "checks": checks,
        "verdict": "REJECTED" if primary <= 0.0 else "NOT_REJECTED",
        "results": results,
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for model, book in books.items():
        book.to_csv(OUTPUT / f"book_{model}.csv")
    (OUTPUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("validate", "run"))
    args = parser.parse_args()
    if args.command == "validate":
        print(json.dumps(validate(), indent=2))
    else:
        summary = run()
        print(json.dumps({"verdict": summary["verdict"]}, indent=2))


if __name__ == "__main__":
    main()
