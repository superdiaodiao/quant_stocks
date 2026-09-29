#!/usr/bin/env python3
"""Run the sue-lt-v1 rules once on 2020-2026 (docs/sue_lt_2020_2026_plan.md).

Selection is ``sue_live.select_targets`` (the forward observation's own
function, one share class per issuer); trading and costs are the
``research_sue_low_turnover`` replay that reproduces the forward book.
Only the inputs are new: Nasdaq listing status from the post-2020 listing
snapshots, and prices, SEC quarters and terminal returns for companies
delisted in 2020-2024.

    PYTHONPATH=. python scripts/research_sue_lt_2020_2026.py report   # data only, no returns
    PYTHONPATH=. python scripts/research_sue_lt_2020_2026.py run
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import subprocess

import numpy as np
import pandas as pd

from scripts import research_holdout_2011_2019 as holdout
from scripts import research_sue_low_turnover as replay
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.io import fundamentals_update
from src.io.security_universe import investable_common_equities
from src.io.terminal_returns import observed_terminal_return_map
from src.research import sue_live as live
from src.research.panel_data import load_panel

INPUTS = Path("output/research_only/sue_lt_2020_2026/inputs")
CACHE = Path("research_cache/sue_lt_2020_2026")
OUTPUT = Path("output/research_only/sue_lt_2020_2026/results")
SNAPSHOTS = Path("stocks_list_dir/nasdaq/snapshots")
QUARTERLY = Path("cleaned_stocks_data/financial/quarterly_fundamentals_point_in_time.csv")
QQQ = Path("output/research_only/qqq_nasdaq_history.csv")
LOAD_START = "2018-06-01"
FIRST_SIGNAL = {"monthly": pd.Timestamp("2019-12-31"), "weekly": pd.Timestamp("2019-12-27")}
LAST_SIGNAL = {"monthly": pd.Timestamp("2026-06-30"), "weekly": pd.Timestamp("2026-07-24")}
FREQUENCIES = ("monthly", "weekly")  # monthly decides (plan 3-4); weekly is reported only (plan 3.1)
START = "2020-01-01"
END = "2026-07-31"
SUPPLEMENT_DIRS = (holdout.CACHE / "price_supplement", CACHE / "price_supplement")


# ------------------------------------------------------------------ prices


def _supplements() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    close, dollar_volume, nominal = {}, {}, {}
    for folder in SUPPLEMENT_DIRS:
        for path in sorted(folder.glob("*.csv")) if folder.exists() else []:
            frame = pd.read_csv(path, parse_dates=["date"]).set_index("date")
            ticker = path.stem.upper()
            close[ticker] = frame["close"]
            dollar_volume[ticker] = frame["close"] * frame["volume"]
            nominal[ticker] = frame["raw_close"]
    return pd.DataFrame(close), pd.DataFrame(dollar_volume), pd.DataFrame(nominal)


def load_prices() -> dict:
    raw_close, dollar_volume = load_panel(holdout.CLEANED_PRICE_DATA_DIR, LOAD_START, END)
    extra_close, extra_volume, extra_nominal = _supplements()
    extra_close = extra_close.loc[LOAD_START:END]
    raw_close = raw_close.drop(columns=raw_close.columns.intersection(extra_close.columns))
    dollar_volume = dollar_volume.drop(columns=dollar_volume.columns.intersection(extra_close.columns))
    index = raw_close.index.union(extra_close.index)
    raw_close = pd.concat([raw_close.reindex(index), extra_close.reindex(index)], axis=1).sort_index()
    dollar_volume = pd.concat([dollar_volume.reindex(index), extra_volume.loc[LOAD_START:END].reindex(index)],
                              axis=1).reindex_like(raw_close)
    nominal = pd.concat([holdout._yahoo_nominal(), extra_nominal], axis=1)
    nominal = nominal.loc[:, ~nominal.columns.duplicated(keep="last")]
    eligibility = nominal.reindex(index=raw_close.index, columns=raw_close.columns).combine_first(raw_close)
    return {"close": raw_close, "dollar_volume": dollar_volume, "eligibility": eligibility}


# ------------------------------------------------------------------ universe


def build_universe(close: pd.DataFrame):
    snapshots = {}
    for path in sorted(SNAPSHOTS.glob("nasdaq_listed_*.csv")):
        stamp = path.stem.removeprefix("nasdaq_listed_")
        if stamp >= "2019-06-01":
            with path.open(newline="", encoding="utf-8") as handle:
                snapshots[stamp] = {row["Symbol"]: row for row in csv.DictReader(handle)}
    dates = sorted(snapshots)
    history = {}
    with (INPUTS / "nasdaq_symbol_history_2020.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            history[row["ticker"]] = row["historical_symbol"]
    supplied = {p.stem.upper() for folder in SUPPLEMENT_DIRS if folder.exists() for p in folder.glob("*.csv")}
    first_price = close.apply(lambda column: column.first_valid_index())
    names = {}
    for stamp in dates:
        names.update(snapshots[stamp])

    def common_equity(ticker: str) -> bool:
        row = names.get(ticker) or names.get(history.get(ticker, ""))
        if row is None:
            return True  # supplied delisted names are common stock (checked when supplied)
        return not investable_common_equities(pd.DataFrame([row])).empty

    equity = {ticker: common_equity(ticker) for ticker in close.columns}

    def present(ticker: str, stamp: str) -> bool:
        members = snapshots[stamp]
        return ticker in members or history.get(ticker, "\0") in members

    def listed(ticker: str, day: pd.Timestamp) -> bool:
        text = day.strftime("%Y-%m-%d")
        before = [s for s in dates if s <= text]
        after = [s for s in dates if s > text]
        if before and present(ticker, before[-1]):
            return True
        # One incomplete snapshot (e.g. 2022-06-24): present on both sides counts.
        if len(before) >= 2 and after and present(ticker, before[-2]) and present(ticker, after[0]):
            return True
        if ticker in supplied and before:
            # Delisted names are absent from later snapshots; listed while any
            # snapshot up to this date still carries them.
            return any(present(ticker, s) for s in before[-3:])
        started = first_price.get(ticker)
        return bool(after and present(ticker, after[0]) and started is not None
                    and before and started.strftime("%Y-%m-%d") > before[-1])

    cache: dict[pd.Timestamp, set[str]] = {}

    def universe(day) -> set[str]:
        day = pd.Timestamp(day)
        if day not in cache:
            traded = close.loc[day].dropna().index
            cache[day] = {t for t in traded if equity[t] and listed(t, day)} - set(live_forbidden())
        return cache[day]

    return universe


def live_forbidden() -> set[str]:
    from scripts.sue_lt_v1 import FORBIDDEN_ETFS
    return set(FORBIDDEN_ETFS)


# ------------------------------------------------------------------ fundamentals, issuers, terminal


def load_quarterly() -> pd.DataFrame:
    frames = [load_quarterly_fundamentals(QUARTERLY),
              load_quarterly_fundamentals(holdout.INPUTS / "quarterly_supplement.csv")]
    extra = INPUTS / "quarterly_supplement_2020.csv"
    if extra.exists():
        frames.append(load_quarterly_fundamentals(extra))
    return pd.concat(frames, ignore_index=True)


def issuer_map() -> dict[str, int]:
    mapping = {str(t).upper(): int(c) for t, c in fundamentals_update.fetch_sec_ticker_map().items()}
    for path in (holdout.INPUTS / "sec_cik_map.csv", INPUTS / "sec_cik_map_2020.csv"):
        if path.exists():
            for row in pd.read_csv(path).itertuples():
                mapping.setdefault(str(row.ticker).upper(), int(row.source_cik))
    return mapping


def terminal_map() -> dict:
    merged = observed_terminal_return_map()
    merged.update(observed_terminal_return_map(holdout.INPUTS / "terminal_returns_supplement.csv"))
    extra = INPUTS / "terminal_returns_2020.csv"
    if extra.exists():
        merged.update(observed_terminal_return_map(extra))
    return merged


def complete_terminal(close: pd.DataFrame, terminal: dict) -> tuple[dict, list[dict]]:
    """Series that end before END without a sourced terminal end at their last
    close (plan section 5.4), and are listed; the replay's own default is -100%."""
    merged, defaulted = dict(terminal), []
    last_session = close.index.max()
    for ticker, ended in close.apply(lambda column: column.last_valid_index()).items():
        if pd.isna(ended) or ended >= last_session:
            continue
        key = (str(ticker).upper(), pd.Timestamp(ended).normalize())
        if key not in merged:
            merged[key] = 0.0
            defaulted.append({"ticker": key[0], "last_price_date": key[1].strftime("%Y-%m-%d")})
    return merged, defaulted


def qqq_returns() -> pd.Series:
    frame = pd.read_csv(QQQ, parse_dates=["date"]).set_index("date").sort_index()
    return (frame["close"] + frame["cash_dividend"]) / frame["close"].shift() - 1.0


# ------------------------------------------------------------------ targets


def signals(index: pd.DatetimeIndex, frequency: str = "monthly") -> list[pd.Timestamp]:
    """Last session of each calendar month, or of each Monday-Friday week."""
    period = index.to_period("M" if frequency == "monthly" else "W-FRI")
    last = index.to_series().groupby(period).max()
    return [d for d in last if FIRST_SIGNAL[frequency] <= d <= LAST_SIGNAL[frequency]]


def build_targets(prices: dict, quarterly: pd.DataFrame, issuer: dict,
                  frequency: str = "monthly") -> tuple[pd.DataFrame, list[dict]]:
    close, eligibility = prices["close"], prices["eligibility"]
    universe = build_universe(close)
    rows, coverage = [], []
    for signal in signals(close.index, frequency):
        symbols = universe(signal)
        chosen = live.select_targets(eligibility, prices["dollar_volume"], symbols, quarterly, signal, issuer=issuer)
        effective = close.index[close.index.get_loc(signal) + 1]
        coverage.append({"signal_date": signal.strftime("%Y-%m-%d"), "universe": len(symbols),
                         "pool": len(chosen["pool"]), "with_sue": len(chosen["sue"])})
        for ticker in chosen["targets"]:
            rows.append({"effective_date": effective, "ticker": ticker, "signal_date": signal,
                         "sue": chosen["sue"][ticker]})
    return pd.DataFrame(rows), coverage


# ------------------------------------------------------------------ commands


def report() -> dict:
    """Data coverage only; no returns are computed."""
    prices = load_prices()
    targets, coverage = build_targets(prices, load_quarterly(), issuer_map())
    frame = pd.DataFrame(coverage)
    frame["sue_share"] = frame["with_sue"] / frame["pool"]
    return {
        "signals": int(len(frame)),
        "universe_median": int(frame["universe"].median()),
        "pool_min": int(frame["pool"].min()),
        "sue_share_median": float(frame["sue_share"].median()),
        "sue_share_min": float(frame["sue_share"].min()),
        "months_below_80pct": frame.loc[frame["sue_share"] < 0.8, "signal_date"].tolist(),
        "distinct_holdings": int(targets["ticker"].nunique()),
        "ever_selected_with_series_ending_early_and_no_sourced_terminal": sorted(
            set(targets["ticker"]) & {d["ticker"] for d in complete_terminal(prices["close"], terminal_map())[1]}),
    }


def run() -> dict:
    if OUTPUT.exists() and any(OUTPUT.iterdir()):
        raise RuntimeError(f"results will not be overwritten: {OUTPUT}")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    prices = load_prices()
    quarterly, issuer = load_quarterly(), issuer_map()
    terminal, defaulted = complete_terminal(prices["close"], terminal_map())
    qqq = qqq_returns()
    nasdaq = pd.read_csv(holdout.NASDAQ_INDEX_FILE, index_col="date", parse_dates=True)["close"].sort_index()
    results, books, schedules, coverage = {}, {}, {}, {}
    for frequency in FREQUENCIES:
        targets, coverage[frequency] = build_targets(prices, quarterly, issuer, frequency)
        schedules[frequency] = targets
        for model in ("ibkr_tiered", "stress"):
            book = replay.simulate(prices["close"], prices["eligibility"], targets, terminal, END, model)
            books[(frequency, model)] = book
            m = holdout.metrics(replay.as_daily(book, nasdaq), qqq, START, END)
            m["orders_per_year"] = book.attrs["orders"] / (m["sessions"] / 252.0)
            m["total_costs_usd"] = book.attrs["costs"]
            m["final_nav_usd"] = float(book["nav"].iloc[-1])
            results[f"{frequency}_{model}"] = m
    primary = results["monthly_ibkr_tiered"]["annualized_excess_vs_qqq"]
    selected = set().union(*(set(t["ticker"]) for t in schedules.values()))
    summary = {
        "plan": "docs/sue_lt_2020_2026_plan.md",
        "code_commit": commit,
        "verdict": "REJECTED" if primary <= 0.0 else "NOT_REJECTED",
        "verdict_basis": "monthly, IBKR Tiered costs, vs QQQ total return",
        "coverage": coverage,
        "terminal_defaulted_to_last_close": defaulted,
        "held_names_with_defaulted_terminal": sorted(selected & {d["ticker"] for d in defaulted}),
        "results": results,
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for frequency, targets in schedules.items():
        targets.to_csv(OUTPUT / f"targets_{frequency}.csv", index=False)
    for (frequency, model), book in books.items():
        book.to_csv(OUTPUT / f"book_{frequency}_{model}.csv")
    (OUTPUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("report", "run"))
    args = parser.parse_args()
    result = report() if args.command == "report" else {"verdict": run()["verdict"]}
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
