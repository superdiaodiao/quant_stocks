#!/usr/bin/env python3
"""Run the frozen v50r3 rules once on 2011-2019 (docs/holdout_2011_2019_plan.md).

Only the inputs are new. Selection is the frozen corrected selector
(``corrected_stock_policy.large_liquid_ranking`` under v26's monthly schedule,
with r3's latest-four-quarter profitability rule bound the way r3 binds it at
runtime) and valuation is ``prospective_replay.replay_live``. Before anything
runs, every file of the r3 code closure must match the frozen protocol on
``live/v50r3`` byte for byte.

    PYTHONPATH=. python scripts/research_holdout_2011_2019.py check
    PYTHONPATH=. python scripts/research_holdout_2011_2019.py run

``run`` refuses to write over an existing result.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from functools import partial
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
import pandas as pd

from scripts import research_v24_stock_momentum_development as v24
from scripts import research_v26_large_liquid_stock_momentum as v26
from scripts import research_v30_2019_selection_path_adjudication as v30
from scripts import research_v50r3_corrected_v47 as r3
from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.io.security_universe import investable_common_equities
from src.io.terminal_returns import observed_terminal_return_map
from src.research import corrected_stock_policy as policy
from src.research import prospective_replay
from src.research.data_quality import stock_returns_with_delisting_penalty
from src.research.panel_data import load_panel

PROTOCOL = "output/research_only/v50/corrected_v47_20260924_r3/frozen_protocol.json"
LIVE_BRANCH = "origin/live/v50r3"
INPUTS = Path("output/research_only/holdout_2011_2019/inputs")
CACHE = Path("research_cache/holdout_2011_2019")
RESULTS = Path("output/research_only/holdout_2011_2019/results")
SNAPSHOTS = Path("stocks_list_dir/nasdaq/snapshots")
QUARTERLY = Path("cleaned_stocks_data/financial/quarterly_fundamentals_point_in_time.csv")

LOAD_START = "2010-01-01"
FIRST_SIGNAL = "2010-12-31"  # 2011 is reported for reference only
MAIN_FIRST_SIGNAL = pd.Timestamp("2011-12-30")
LAST_SIGNAL = pd.Timestamp("2019-11-29")
END = "2019-12-31"
COSTS = (10, 30, 50)
PRIMARY_COST = 50
SNAPSHOT_EPOCH = "2015-01-10"
SNAPSHOT_LAST = "2020-02-25"
DEVELOPMENT_TARGETS = Path("output/research_only/v50/corrected_v47_20260831_r1/development_results/corrected_targets.csv")


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def frozen_protocol() -> dict:
    text = subprocess.run(
        ["git", "show", f"{LIVE_BRANCH}:{PROTOCOL}"],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(text)


def verify_frozen_code() -> dict:
    closure = frozen_protocol()["code_closure"]
    changed = [
        path for path, digest in closure["files"].items()
        if _sha256(Path(path)) != digest
    ]
    if changed:
        raise RuntimeError("frozen r3 code changed: " + ", ".join(changed))
    return {"file_count": closure["file_count"], "sha256": closure["sha256"]}


def verify_inputs() -> dict:
    manifest = json.loads((INPUTS / "manifest.json").read_text(encoding="utf-8"))
    changed = [
        path for path, digest in manifest["sha256"].items()
        if _sha256(Path(path)) != digest
    ]
    if changed:
        raise RuntimeError("holdout inputs changed: " + ", ".join(changed))
    return {"files": len(manifest["sha256"])}


# ---------------------------------------------------------------- prices


def _supplement_prices() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    close, dollar_volume, nominal = {}, {}, {}
    for path in sorted((CACHE / "price_supplement").glob("*.csv")):
        frame = pd.read_csv(path, parse_dates=["date"]).set_index("date")
        ticker = path.stem.upper()
        close[ticker] = frame["close"]
        dollar_volume[ticker] = frame["close"] * frame["volume"]
        nominal[ticker] = frame["raw_close"]
    return pd.DataFrame(close), pd.DataFrame(dollar_volume), pd.DataFrame(nominal)


def _yahoo_nominal() -> pd.DataFrame:
    """Contemporaneous nominal closes: Yahoo's split-only close times later splits."""
    series = {}
    for path in sorted((CACHE / "yahoo_nominal").glob("*.json")):
        result = json.loads(path.read_text())["chart"]["result"][0]
        stamps = pd.to_datetime(result["timestamp"], unit="s").normalize()
        closes = pd.Series(result["indicators"]["quote"][0]["close"], index=stamps, dtype=float)
        splits = (result.get("events") or {}).get("splits") or {}
        factor = pd.Series(1.0, index=closes.index)
        for event in splits.values():
            day = pd.to_datetime(event["date"], unit="s").normalize()
            ratio = float(event["numerator"]) / float(event["denominator"])
            factor.loc[factor.index < day] *= ratio
        series[path.stem.upper()] = (closes * factor).groupby(level=0).last()
    return pd.DataFrame(series)


def load_prices() -> dict:
    raw_close, dollar_volume = load_panel(CLEANED_PRICE_DATA_DIR, LOAD_START, END)
    extra_close, extra_volume, extra_nominal = _supplement_prices()
    extra_close = extra_close.loc[LOAD_START:END]
    # A supplied history replaces a local column wholly, so sources never splice.
    raw_close = raw_close.drop(columns=raw_close.columns.intersection(extra_close.columns))
    dollar_volume = dollar_volume.drop(columns=dollar_volume.columns.intersection(extra_close.columns))
    index = raw_close.index.union(extra_close.index)
    raw_close = pd.concat([raw_close.reindex(index), extra_close.reindex(index)], axis=1).sort_index()
    dollar_volume = pd.concat(
        [dollar_volume.reindex(index), extra_volume.loc[LOAD_START:END].reindex(index)], axis=1
    ).reindex_like(raw_close)
    nominal = pd.concat([_yahoo_nominal(), extra_nominal], axis=1)
    nominal = nominal.loc[:, ~nominal.columns.duplicated(keep="last")]
    return {"raw_close": raw_close, "dollar_volume": dollar_volume, "nominal": nominal}


# ---------------------------------------------------------------- universe


def _snapshots() -> dict[str, dict[str, str]]:
    out = {}
    for path in sorted(SNAPSHOTS.glob("nasdaq_listed_*.csv")):
        stamp = path.stem.removeprefix("nasdaq_listed_")
        if stamp <= SNAPSHOT_LAST:
            with path.open(newline="", encoding="utf-8") as handle:
                out[stamp] = {row["Symbol"]: row for row in csv.DictReader(handle)}
    return out


def _read_csv_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def build_universe(raw_close: pd.DataFrame):
    snapshots = _snapshots()
    dates = sorted(snapshots)
    history = {row["ticker"]: row["historical_symbol"] for row in _read_csv_rows(INPUTS / "nasdaq_symbol_history.csv")}
    overrides = {row["ticker"]: row for row in _read_csv_rows(INPUTS / "nasdaq_listing_overrides.csv")}
    supplied = {path.stem.upper() for path in (CACHE / "price_supplement").glob("*.csv")}
    first_price = raw_close.apply(lambda column: column.first_valid_index())

    names = {}
    for stamp in dates:
        for symbol, row in snapshots[stamp].items():
            names[symbol] = row
    tiingo_names = {}
    for path in (Path("research_cache") / "tiingo_delisted").glob("*.json") if (Path("research_cache") / "tiingo_delisted").exists() else []:
        tiingo_names[path.stem.upper()] = json.loads(path.read_text())["meta"].get("name", "")

    def common_equity(ticker: str) -> bool:
        row = names.get(ticker) or names.get(history.get(ticker, ""))
        if row is None:
            row = {"Symbol": ticker, "Name": tiingo_names.get(ticker, ticker), "ETF": "N"}
        return not investable_common_equities(pd.DataFrame([row])).empty

    equity = {ticker: common_equity(ticker) for ticker in raw_close.columns}

    def in_snapshot(ticker: str, stamp: str) -> bool:
        members = snapshots[stamp]
        return ticker in members or history.get(ticker, "\0") in members

    def listed(ticker: str, day: pd.Timestamp) -> bool:
        text = day.strftime("%Y-%m-%d")
        rule = overrides.get(ticker)
        if rule is not None:
            if rule["nasdaq_from"] and text < rule["nasdaq_from"]:
                return False
            if rule["nasdaq_until"] and text > rule["nasdaq_until"]:
                return False
            return True
        if ticker in supplied:
            return True
        if text < SNAPSHOT_EPOCH:
            return in_snapshot(ticker, SNAPSHOT_EPOCH)
        before = [stamp for stamp in dates if stamp <= text][-1]
        if in_snapshot(ticker, before):
            return True
        after = [stamp for stamp in dates if stamp > text]
        started = first_price.get(ticker)
        # Listed at its first trade on Nasdaq: absent only because no
        # snapshot was taken between its first trade and this date.
        return bool(
            after and in_snapshot(ticker, after[0])
            and started is not None and started.strftime("%Y-%m-%d") > before
        )

    cache: dict[pd.Timestamp, set[str]] = {}

    def universe(signal_date) -> set[str]:
        day = pd.Timestamp(signal_date).normalize()
        if day not in cache:
            traded = raw_close.loc[day].dropna().index
            cache[day] = {
                ticker for ticker in traded
                if equity[ticker] and listed(ticker, day)
            } - v24.FORBIDDEN_ETFS
        return cache[day]

    return universe


# ---------------------------------------------------------------- inputs


def load_inputs() -> dict:
    prices = load_prices()
    validation = policy.load_corporate_action_validation()
    continuous, eligibility = policy.corrected_price_views(prices["raw_close"], validation)
    # Minimum price in the units of the day (plan section 2): restore nominal
    # closes where later splits (or dividends) moved the adjusted series.
    nominal = prices["nominal"].reindex(index=eligibility.index, columns=eligibility.columns)
    eligibility = nominal.combine_first(eligibility)
    quarterly = pd.concat(
        [
            load_quarterly_fundamentals(QUARTERLY),
            load_quarterly_fundamentals(INPUTS / "quarterly_supplement.csv"),
        ],
        ignore_index=True,
    )
    nasdaq = pd.read_csv(NASDAQ_INDEX_FILE, index_col="date", parse_dates=True)["close"].sort_index().loc[:END]
    return {
        "raw_close": prices["raw_close"],
        "close": continuous,
        "eligibility_close": eligibility,
        "dollar_volume": prices["dollar_volume"].reindex_like(continuous),
        "nasdaq": nasdaq,
        "quarterly": quarterly,
        "universe": build_universe(prices["raw_close"]),
        "corporate_action_validation": validation,
        "technical_cache": {},
        "quality_cache": {},
        "large_liquid_cache": {},
    }


@contextmanager
def frozen_selector():
    """Bind the selector exactly as r1 and r3 bind it for a live signal."""
    original_ranking = v26._large_liquid_ranking
    original_profit = v24._profitable_symbols
    original_window = (v26.DEVELOPMENT_START, v26.DEVELOPMENT_END)
    try:
        v26._large_liquid_ranking = policy.large_liquid_ranking
        v24._profitable_symbols = r3._live_profitable_symbols
        # v26's schedule reads its window from these module constants.
        v26.DEVELOPMENT_START, v26.DEVELOPMENT_END = (
            (pd.Timestamp(FIRST_SIGNAL) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            END,
        )
        yield
    finally:
        v26._large_liquid_ranking = original_ranking
        v24._profitable_symbols = original_profit
        v26.DEVELOPMENT_START, v26.DEVELOPMENT_END = original_window


@contextmanager
def terminal_returns():
    merged = observed_terminal_return_map()
    merged.update(observed_terminal_return_map(INPUTS / "terminal_returns_supplement.csv"))
    original = policy.stock_returns_with_delisting_penalty
    try:
        policy.stock_returns_with_delisting_penalty = partial(
            stock_returns_with_delisting_penalty, terminal_returns=merged
        )
        yield len(merged)
    finally:
        policy.stock_returns_with_delisting_penalty = original


# ---------------------------------------------------------------- metrics


def qqq_returns() -> pd.Series:
    frame = pd.read_csv(CACHE / "qqq_tiingo_2010_2020.csv", parse_dates=["date"]).set_index("date")
    return frame["adjClose"].pct_change()


def _annualized(daily: pd.Series) -> float:
    growth = float((1.0 + daily).prod())
    return growth ** (252.0 / len(daily)) - 1.0 if len(daily) else float("nan")


def _max_drawdown(daily: pd.Series) -> float:
    wealth = (1.0 + daily).cumprod()
    return float((wealth / wealth.cummax() - 1.0).min())


def metrics(daily: pd.DataFrame, qqq: pd.Series, start: str, end: str) -> dict:
    window = daily.loc[start:end]
    strategy = window["strategy"]
    reference = qqq.reindex(window.index).fillna(0.0)
    nasdaq = window["benchmark"]
    monthly_strategy = (1.0 + strategy).resample("ME").prod() - 1.0
    monthly_qqq = (1.0 + reference).resample("ME").prod() - 1.0
    excess = monthly_strategy - monthly_qqq
    yearly = []
    for year, frame in window.groupby(window.index.year):
        s = float((1.0 + frame["strategy"]).prod() - 1.0)
        q = float((1.0 + reference.loc[frame.index]).prod() - 1.0)
        n = float((1.0 + frame["benchmark"]).prod() - 1.0)
        yearly.append({"year": int(year), "strategy": s, "qqq_total_return": q,
                       "excess_vs_qqq": s - q, "nasdaq_price_return": n, "excess_vs_nasdaq": s - n})
    top = excess.sort_values(ascending=False).head(3)
    total_excess = float(excess.sum())
    return {
        "start": start, "end": end, "sessions": int(len(window)),
        "annualized_strategy": _annualized(strategy),
        "annualized_qqq_total_return": _annualized(reference),
        "annualized_nasdaq_price_return": _annualized(nasdaq),
        "annualized_excess_vs_qqq": _annualized(strategy) - _annualized(reference),
        "annualized_excess_vs_nasdaq_price": _annualized(strategy) - _annualized(nasdaq),
        "monthly_excess_vs_qqq_mean": float(excess.mean()),
        "monthly_excess_vs_qqq_t": float(excess.mean() / excess.std(ddof=1) * np.sqrt(len(excess))),
        "months": int(len(excess)),
        "years_beating_qqq": int(sum(row["excess_vs_qqq"] > 0 for row in yearly)),
        "max_drawdown_strategy": _max_drawdown(strategy),
        "max_drawdown_qqq": _max_drawdown(reference),
        "average_invested": float(window["invested"].mean()),
        "turnover_per_year": float(window["turnover"].sum() / (len(window) / 252.0)),
        "stock_stop_exits": int(window["stock_stop_exits"].sum()),
        "portfolio_stop_exits": int(window["portfolio_stop_exits"].sum()),
        "top3_months_share_of_summed_excess": (float(top.sum()) / total_excess) if total_excess else None,
        "top3_months": {k.strftime("%Y-%m"): float(v) for k, v in top.items()},
        "yearly": yearly,
    }


def verdict(main: dict, sub: dict) -> str:
    if main["annualized_excess_vs_qqq"] <= 0.0 or sub["annualized_excess_vs_qqq"] <= -0.02:
        return "REJECTED"
    if main["monthly_excess_vs_qqq_t"] >= 1.5 and main["years_beating_qqq"] >= 5:
        return "SUPPORTED"
    return "NOT_REJECTED"


# ---------------------------------------------------------------- commands


def validate() -> dict:
    """Plumbing test on 2020-2025, a period already seen: compare targets with
    the frozen development replay. Differences are expected where r3's
    profitability rule or this loader's universe differ from r1's."""
    global LOAD_START, FIRST_SIGNAL, END, SNAPSHOT_LAST
    saved = (LOAD_START, FIRST_SIGNAL, END, SNAPSHOT_LAST)
    LOAD_START, FIRST_SIGNAL, END, SNAPSHOT_LAST = "2018-06-01", "2019-12-31", "2025-12-31", "2099-12-31"
    try:
        inputs = load_inputs()
        with frozen_selector():
            targets = v26.generate_target_schedule(v30.selected_specification(), inputs)
    finally:
        LOAD_START, FIRST_SIGNAL, END, SNAPSHOT_LAST = saved
    development = pd.read_csv(DEVELOPMENT_TARGETS, parse_dates=["effective_date"])
    development = development.loc[development["effective_date"].between("2020-01-01", "2025-12-31")]
    targets["effective_date"] = pd.to_datetime(targets["effective_date"])
    mine = targets.groupby("effective_date")["ticker"].apply(set)
    theirs = development.groupby("effective_date")["ticker"].apply(set)
    months = mine.index.intersection(theirs.index)
    same = sum(mine[m] == theirs[m] for m in months)
    overlap = sum(len(mine[m] & theirs[m]) for m in months)
    size = sum(len(theirs[m]) for m in months)
    return {
        "months_compared": int(len(months)),
        "months_identical": int(same),
        "holdings_overlap": f"{overlap}/{size}",
        "differences": {
            m.strftime("%Y-%m-%d"): {"loader_only": sorted(mine[m] - theirs[m]), "development_only": sorted(theirs[m] - mine[m])}
            for m in months if mine[m] != theirs[m]
        },
    }


NEIGHBORHOOD = Path("output/research_only/holdout_2011_2019/neighborhood")
IN_SAMPLE = {
    "v26": "output/research_only/v29/recovered_2019_stock_momentum_20260830/development_results/candidate_summaries.json",
    "v31": "output/research_only/v31/recovery_speed_stock_momentum_20260830/development_results/candidate_summaries.json",
    "v33": "output/research_only/v33/portfolio_stop_development_20260830/development_results/candidate_summaries.json",
    "v46": "output/research_only/v46/entry_loss_stop_20260830/development_results/candidate_summaries.json",
}
NO_STOP = 0.999  # the replays reject 1.0; a 99.9% loss threshold never binds here


def _in_sample_excess(family: str, key: str) -> float | None:
    """2020-2025 annualized excess over the Nasdaq Composite at 50 bps, as the research saw it."""
    summaries = json.loads(Path(IN_SAMPLE[family]).read_text(encoding="utf-8"))
    entry = summaries.get(key)
    if entry is None:
        return None
    cost = entry["costs"]["50"]
    annual = cost.get("annual") or cost.get("annual_training_diagnostics")
    rows = [row for row in annual if 2020 <= int(row["year"]) <= 2025]
    strategy = np.prod([1.0 + row["strategy"] for row in rows])
    benchmark = np.prod([1.0 + row["benchmark"] for row in rows])
    years = len(rows)
    return float(strategy ** (1 / years) - benchmark ** (1 / years)) if years else None


def _main_window(targets: pd.DataFrame, index: pd.DatetimeIndex) -> pd.DataFrame:
    targets = targets.copy()
    targets["effective_date"] = pd.to_datetime(targets["effective_date"])
    signal_of = {
        v26.next_trading_date(index, signal): signal
        for signal in v26.scheduled_signal_dates(index, FIRST_SIGNAL, END, "monthly")
    }
    targets["signal_date"] = targets["effective_date"].map(signal_of)
    return targets.loc[targets["signal_date"].between(MAIN_FIRST_SIGNAL, LAST_SIGNAL)]


@contextmanager
def _v31_window(v31):
    original = (v31.DEVELOPMENT_START, v31.DEVELOPMENT_END)
    try:
        v31.DEVELOPMENT_START, v31.DEVELOPMENT_END = v26.DEVELOPMENT_START, v26.DEVELOPMENT_END
        yield
    finally:
        v31.DEVELOPMENT_START, v31.DEVELOPMENT_END = original


def _with_loud_market_moves(inputs: dict) -> pd.DataFrame:
    """Add r3's volume rule to the validation the replay checks targets against.

    r3 treats a split-sized move traded at more than QUIET_VOLUME_MULTIPLE
    times its recent median dollar volume as a real move
    (``prospective_marks.split_like_moves``); the development replay predates
    that rule. Quiet split-sized moves stay unresolved, and a candidate that
    holds one fails closed. These rows feed only that check, not prices.
    """
    from src.research import prospective_marks as marks

    raw = inputs["raw_close"].loc[:END]
    every = marks.split_like_moves(raw)
    quiet = marks.split_like_moves(raw, dollar_volume=inputs["dollar_volume"].loc[:END])
    every = every.loc[every["reason"].eq("COMMON_SPLIT_RATIO")]
    loud = every.merge(quiet[["ticker", "split_date"]], how="left", indicator=True)
    loud = loud.loc[loud["_merge"].eq("left_only")]
    validation = inputs["corporate_action_validation"]
    extra = pd.DataFrame({
        "ticker": loud["ticker"].values,
        "split_date": pd.to_datetime(loud["split_date"]).values,
        "validation_status": "CONFIRMED_MARKET_MOVE",
        "confirmed_adjustment_factor": np.nan,
        "confirmed_action_type": "MARKET_MOVE_NO_ADJUSTMENT",
        "confirmed_action_date": pd.to_datetime(loud["split_date"]).values,
    })
    return pd.concat([validation, extra.reindex(columns=validation.columns)], ignore_index=True)


def neighborhood() -> dict:
    from scripts import research_v31_recovery_speed_stock_momentum as v31

    if NEIGHBORHOOD.exists() and any(NEIGHBORHOOD.iterdir()):
        raise RuntimeError(f"neighborhood results will not be overwritten: {NEIGHBORHOOD}")
    checks = check()
    inputs = load_inputs()
    index = inputs["close"].index
    chosen = v30.selected_specification()
    schedules = []
    with frozen_selector():
        for spec in v26.candidate_specs():
            schedules.append(("v26", spec, v26.generate_target_schedule(spec, inputs)))
        with _v31_window(v31):
            for spec in v31.candidate_specs():
                if (int(spec["lookback_sessions"]), int(spec["market_ma_days"]), int(spec["top_n"])) == (63, 200, 5):
                    continue  # the v26 pick, run above
                schedules.append(("v31", spec, v31.generate_target_schedule(spec, inputs)))
    runs = []  # (family, key, overlay, targets, entry, portfolio, in_sample_key)
    for family, spec, targets in schedules:
        main = _main_window(targets, index)
        runs.append((family, spec["key"], "no_stop", main, NO_STOP, NO_STOP, spec["key"]))
        runs.append((family, spec["key"], "stop_20_25", main, 0.20, 0.25, None))
        if spec["key"] == chosen["key"]:
            chosen_targets = main
    for fraction in (0.10, 0.15, 0.20, 0.25):
        pct = int(round(fraction * 100))
        runs.append(("v33", f"portfolio_trailing_stop_{pct}pct", "portfolio_stop", chosen_targets, NO_STOP, fraction, f"portfolio_trailing_stop_{pct}pct"))
        runs.append(("v46", f"monthly_entry_loss_stop_{pct}pct", "entry_stop", chosen_targets, fraction, NO_STOP, f"monthly_entry_loss_stop_{pct}pct"))
    qqq = qqq_returns()
    validation = _with_loud_market_moves(inputs)
    rows = []
    with terminal_returns():
        for number, (family, key, overlay, targets, entry, portfolio, in_key) in enumerate(runs, 1):
            row = {"family": family, "key": key, "overlay": overlay,
                   "is_frozen_model": family == "v26" and key == chosen["key"] and overlay == "stop_20_25",
                   "blocked": ""}
            try:
                dailies = {
                    cost: prospective_replay.replay_live(
                        inputs["raw_close"], inputs["nasdaq"], targets, targets["effective_date"].min(), END,
                        validation=validation,
                        entry_loss_fraction=entry, portfolio_stop_fraction=portfolio,
                        transaction_cost_bps=float(cost),
                    )
                    for cost in (50, 10)
                }
            except RuntimeError as error:
                if "Unresolved corporate action" not in str(error):
                    raise
                row["blocked"] = str(error).split(": ", 1)[-1]
                rows.append(row)
                print(f"[{number}/{len(runs)}] {family} {key} {overlay} BLOCKED {row['blocked']}", flush=True)
                continue
            for cost, daily in dailies.items():
                m = metrics(daily, qqq, "2012-01-01", END)
                row[f"excess_vs_qqq_{cost}bps"] = m["annualized_excess_vs_qqq"]
                row[f"excess_vs_nasdaq_price_{cost}bps"] = m["annualized_excess_vs_nasdaq_price"]
                row[f"annualized_{cost}bps"] = m["annualized_strategy"]
                row[f"years_beating_qqq_{cost}bps"] = m["years_beating_qqq"]
            source = {"v26": "v26", "v31": "v31", "v33": "v33", "v46": "v46"}[family]
            row["in_sample_2020_2025_excess_vs_nasdaq_50bps"] = _in_sample_excess(source, in_key) if in_key else None
            rows.append(row)
            print(f"[{number}/{len(runs)}] {family} {key} {overlay}", flush=True)
    table = pd.DataFrame(rows)
    NEIGHBORHOOD.mkdir(parents=True, exist_ok=True)
    table.to_csv(NEIGHBORHOOD / "candidates.csv", index=False)
    blocked = table.loc[table["blocked"].astype(str).ne("")]
    table = table.loc[table["blocked"].astype(str).eq("")]
    paired = table.dropna(subset=["in_sample_2020_2025_excess_vs_nasdaq_50bps"])
    summary = {
        "checks": checks,
        "candidates": int(len(table)),
        "blocked_fail_closed": int(len(blocked)),
        "beating_qqq_50bps": int((table["excess_vs_qqq_50bps"] > 0).sum()),
        "beating_qqq_10bps": int((table["excess_vs_qqq_10bps"] > 0).sum()),
        "beating_nasdaq_price_50bps": int((table["excess_vs_nasdaq_price_50bps"] > 0).sum()),
        "median_excess_vs_qqq_50bps": float(table["excess_vs_qqq_50bps"].median()),
        "frozen_model_rank_50bps": int(table["excess_vs_qqq_50bps"].rank(ascending=False)[table["is_frozen_model"]].iloc[0]),
        "in_sample_vs_holdout_spearman": float(
            paired["in_sample_2020_2025_excess_vs_nasdaq_50bps"].corr(paired["excess_vs_nasdaq_price_50bps"], method="spearman")
        ) if len(paired) > 2 else None,
        "paired_candidates": int(len(paired)),
    }
    (NEIGHBORHOOD / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def check() -> dict:
    return {"frozen_code": verify_frozen_code(), "inputs": verify_inputs()}


def run() -> dict:
    if RESULTS.exists() and any(RESULTS.iterdir()):
        raise RuntimeError(f"holdout results will not be overwritten: {RESULTS}")
    checks = check()
    inputs = load_inputs()
    spec = v30.selected_specification()
    with frozen_selector():
        targets = v26.generate_target_schedule(spec, inputs)
    targets["effective_date"] = pd.to_datetime(targets["effective_date"])
    signal_of = {
        effective: signal for signal, effective in (
            (s, v26.next_trading_date(inputs["close"].index, s))
            for s in v26.scheduled_signal_dates(inputs["close"].index, FIRST_SIGNAL, END, "monthly")
        )
    }
    targets["signal_date"] = targets["effective_date"].map(signal_of)
    targets = targets.loc[targets["signal_date"].le(LAST_SIGNAL)]
    main_targets = targets.loc[targets["signal_date"].ge(MAIN_FIRST_SIGNAL)]
    main_start = main_targets["effective_date"].min()
    qqq = qqq_returns()
    validation = inputs["corporate_action_validation"]
    results, daily_frames = {}, {}
    with terminal_returns() as terminal_count:
        for label, schedule, start in (
            ("main_2012_2019", main_targets, main_start),
            ("reference_2011_2019", targets, targets["effective_date"].min()),
        ):
            for cost in COSTS:
                daily = prospective_replay.replay_live(
                    inputs["raw_close"], inputs["nasdaq"], schedule, start, END,
                    validation=validation,
                    entry_loss_fraction=0.20, portfolio_stop_fraction=0.25,
                    transaction_cost_bps=float(cost),
                )
                daily_frames[(label, cost)] = daily
    for (label, cost), daily in daily_frames.items():
        if label == "main_2012_2019":
            results[f"{cost}bps"] = {
                "main_2012_2019": metrics(daily, qqq, "2012-01-01", END),
                "sub_2014_2019": metrics(daily, qqq, "2014-01-01", END),
                "sub_2018_2019": metrics(daily, qqq, "2018-01-01", END),
            }
        else:
            results[f"{cost}bps"]["reference_2011_2019"] = metrics(daily, qqq, "2011-01-01", END)
    primary = results[f"{PRIMARY_COST}bps"]
    summary = {
        "plan": "docs/holdout_2011_2019_plan.md",
        "data_report": "docs/holdout_2011_2019_data_report.md",
        "checks": checks,
        "specification": spec,
        "terminal_returns_available": terminal_count,
        "signals": int(targets["signal_date"].nunique()),
        "cash_signals": int(targets.groupby("signal_date")["ticker"].apply(lambda s: set(s) == {"__CASH__"}).sum()),
        "verdict": verdict(primary["main_2012_2019"], primary["sub_2014_2019"]),
        "results": results,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    targets.to_csv(RESULTS / "targets.csv", index=False)
    for (label, cost), daily in daily_frames.items():
        daily.to_csv(RESULTS / f"daily_{label}_{cost}bps.csv", index_label="date")
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("check", "validate", "run", "neighborhood"))
    args = parser.parse_args()
    result = {"check": check, "validate": validate, "run": run, "neighborhood": neighborhood}[args.command]()
    if args.command == "run":
        result = {k: result[k] for k in ("verdict", "signals", "cash_signals", "checks")}
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
