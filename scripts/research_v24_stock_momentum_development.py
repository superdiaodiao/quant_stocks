#!/usr/bin/env python3
"""Develop a stock-only momentum selector after the v23 CAN SLIM grid failed.

The candidate grid is frozen before returns are calculated. Development uses
2019-2025 only. Positive targets are individual common equities, and risk-off
or unused weight remains cash. QQQ is a benchmark only and is never a holding.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import research_v15_benchmark_core_development as v15
from scripts import research_v23_stock_only_frequency as v23
from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
)
from src.financial.quarterly_fundamentals import (
    load_quarterly_fundamentals,
    quarterly_growth_snapshot,
)
from src.research.can_slim import replay_can_slim_target_schedule
from src.research.data_quality import back_adjust_common_splits
from src.research.panel_data import load_panel
from src.research.universe_history import (
    load_universe_snapshots,
    snapshot_directory,
    universe_as_of,
)
from src.strategy.common import (
    market_regime_is_on,
    next_trading_date,
    scheduled_signal_dates,
)


DEVELOPMENT_START = v23.DEVELOPMENT_START
DEVELOPMENT_END = v23.DEVELOPMENT_END
DEVELOPMENT_YEARS = v23.DEVELOPMENT_YEARS
OBSERVATION_START = v23.OBSERVATION_START
COSTS = v23.COSTS
FORBIDDEN_ETFS = v23.FORBIDDEN_ETFS
LOOKBACKS = ((63, 0), (126, 0), (252, 21))
FREQUENCY_POLICIES = (("monthly", 1), ("weekly", 2))
TOP_NS = (3, 5)
QUALITY_MODES = ("technical", "profitable")
MINIMUM_PRICE = 10.0
MINIMUM_MEDIAN_DOLLAR_VOLUME = 10_000_000.0
MARKET_MA_DAYS = 200
STOCK_MA_DAYS = 200
MAXIMUM_FINANCIAL_AGE_DAYS = 550
MAXIMUM_SNAPSHOT_AGE_DAYS = 40
OUTPUT_DIR = Path("output/research_only/v24/stock_momentum_20260830_retry2")
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
DEVELOPMENT_OUTPUT_DIR = OUTPUT_DIR / "development_results"


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_binding(path: Path) -> dict:
    path = Path(path)
    return {"path": str(path), "sha256": _sha256(path)}


def _directory_binding(path: Path, pattern: str) -> dict:
    path = Path(path)
    digest = hashlib.sha256()
    files = sorted(path.glob(pattern))
    for item in files:
        digest.update(item.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(item).encode("ascii"))
        digest.update(b"\n")
    return {
        "path": str(path),
        "pattern": pattern,
        "file_count": len(files),
        "content_manifest_sha256": digest.hexdigest(),
    }


def candidate_specs() -> list[dict]:
    specs = []
    for lookback, skip in LOOKBACKS:
        for frequency, rank_buffer in FREQUENCY_POLICIES:
            for top_n in TOP_NS:
                for quality_mode in QUALITY_MODES:
                    specs.append({
                        "key": (
                            f"mom{lookback}_skip{skip}_{frequency}_top{top_n}_"
                            f"buffer{rank_buffer}_{quality_mode}"
                        ),
                        "lookback_sessions": lookback,
                        "skip_recent_sessions": skip,
                        "signal_frequency": frequency,
                        "top_n": top_n,
                        "rank_buffer_multiple": rank_buffer,
                        "quality_mode": quality_mode,
                    })
    return specs


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict:
    path = Path(path)
    if path.exists():
        raise RuntimeError(f"v24 protocol will not be overwritten: {path}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V24_STOCK_MOMENTUM_PRECOMMITMENT",
        "status": "FROZEN_NOT_DEVELOPED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "reason": (
            "v23 showed monthly concentration had positive compounded alpha "
            "but insufficient annual consistency, while unbuffered weekly "
            "turnover destroyed 50bps performance."
        ),
        "objective": (
            "Select liquid individual common-equity momentum leaders; keep "
            "risk-off and residual weight in cash; never hold an index ETF."
        ),
        "development_window": {
            "start": DEVELOPMENT_START,
            "end": DEVELOPMENT_END,
            "years": list(DEVELOPMENT_YEARS),
        },
        "reserved_observation_start": OBSERVATION_START,
        "candidate_grid": candidate_specs(),
        "candidate_count": len(candidate_specs()),
        "common_parameters": {
            "minimum_price": MINIMUM_PRICE,
            "minimum_median_dollar_volume": MINIMUM_MEDIAN_DOLLAR_VOLUME,
            "market_ma_days": MARKET_MA_DAYS,
            "stock_ma_days": STOCK_MA_DAYS,
            "maximum_financial_age_days": MAXIMUM_FINANCIAL_AGE_DAYS,
            "relative_momentum_gate": "stock momentum > Nasdaq momentum",
            "risk_off_asset": "CASH",
            "residual_weight_asset": "CASH",
            "forbidden_etfs": sorted(FORBIDDEN_ETFS),
            "signal_execution": "completed period close to next trading close",
        },
        "cost_bps": list(COSTS),
        "primary_benchmark": "QQQ_TOTAL_RETURN",
        "secondary_benchmark": "NASDAQ_COMPOSITE",
        "eligibility_gates": {
            "positive_compounded_excess_vs_qqq_at_30_and_50bps": True,
            "minimum_annual_win_rate_vs_qqq_at_50bps": v23.MINIMUM_WIN_RATE,
            "maximum_drawdown_lag_vs_qqq_percentage_points": (
                v23.MAXIMUM_DRAWDOWN_LAG * 100.0
            ),
            "positive_targets_must_exclude_all_forbidden_etfs": True,
        },
        "selection_order": [
            "eligible first",
            "annual wins versus QQQ at 50bps descending",
            "worst annual excess versus QQQ at 50bps descending",
            "compounded excess versus QQQ at 50bps descending",
            "turnover at 50bps ascending",
            "candidate key ascending",
        ],
        "walk_forward_folds": [
            {"selection_years": list(range(2019, year)), "test_year": year}
            for year in range(2022, 2026)
        ],
        "input_bindings": {
            "runner": _file_binding(runner),
            "v23_evaluation_helpers": _file_binding(
                Path("scripts/research_v23_stock_only_frequency.py")
            ),
            "can_slim_replay": _file_binding(Path("src/research/can_slim.py")),
            "data_quality": _file_binding(Path("src/research/data_quality.py")),
            "universe_history": _file_binding(
                Path("src/research/universe_history.py")
            ),
            "price_directory": _directory_binding(
                Path(CLEANED_PRICE_DATA_DIR), "*.csv"
            ),
            "universe_snapshots": _directory_binding(
                snapshot_directory(), "nasdaq_listed_*.csv"
            ),
            "nasdaq_index": _file_binding(Path(NASDAQ_INDEX_FILE)),
            "qqq_history": _file_binding(Path(v15.QQQ_HISTORY["path"])),
            "quarterly_fundamentals": _file_binding(
                Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE)
            ),
        },
        "parameters_frozen_before_development": True,
        "2026_used_for_development_or_selection": False,
        "brokerage_or_trading_authorized": False,
        "broker_connection_used": False,
        "order_created": False,
        "capital_allocated": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(protocol, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**protocol, "protocol": _file_binding(path)}


def _validated_protocol(path: Path) -> tuple[dict, str]:
    path = Path(path)
    protocol_sha = _sha256(path)
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if protocol["status"] != "FROZEN_NOT_DEVELOPED":
        raise RuntimeError("v24 protocol status changed")
    if protocol["candidate_grid"] != candidate_specs():
        raise RuntimeError("v24 candidate grid changed")
    if protocol["release_status"] != "BLOCKED" or protocol["promotion_eligible"]:
        raise RuntimeError("v24 release boundary changed")
    for name, binding in protocol["input_bindings"].items():
        if "content_manifest_sha256" in binding:
            actual = _directory_binding(Path(binding["path"]), binding["pattern"])
            if actual != binding:
                raise RuntimeError(f"v24 directory binding changed for {name}")
        elif _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"v24 file binding changed for {name}")
    return protocol, protocol_sha


def _load_inputs() -> dict:
    load_start = (
        pd.Timestamp(DEVELOPMENT_START) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")
    raw_close, dollar_volume = load_panel(
        CLEANED_PRICE_DATA_DIR, load_start, DEVELOPMENT_END
    )
    close = back_adjust_common_splits(raw_close).sort_index()
    dollar_volume = dollar_volume.reindex_like(close)
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"].sort_index().loc[:DEVELOPMENT_END]
    qqq = pd.read_csv(
        v15.QQQ_HISTORY["path"], index_col="date", parse_dates=True
    ).sort_index().loc[:DEVELOPMENT_END]
    quarterly = load_quarterly_fundamentals(
        POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
    )
    snapshots = load_universe_snapshots()
    universe_cache = {}

    def universe(signal_date):
        stamp = pd.Timestamp(signal_date).normalize()
        if stamp not in universe_cache:
            symbols = universe_as_of(
                snapshots,
                stamp,
                maximum_age_days=MAXIMUM_SNAPSHOT_AGE_DAYS,
            )
            universe_cache[stamp] = (
                None if symbols is None else set(symbols) - FORBIDDEN_ETFS
            )
        return universe_cache[stamp]

    return {
        "raw_close": raw_close,
        "close": close,
        "dollar_volume": dollar_volume,
        "nasdaq": nasdaq,
        "qqq": qqq,
        "quarterly": quarterly,
        "universe": universe,
        "technical_cache": {},
        "quality_cache": {},
    }


def _technical_ranking(signal_date: pd.Timestamp, spec: dict, inputs: dict) -> pd.DataFrame:
    lookback = int(spec["lookback_sessions"])
    skip = int(spec["skip_recent_sessions"])
    cache_key = (pd.Timestamp(signal_date), lookback, skip)
    cached = inputs["technical_cache"].get(cache_key)
    if cached is not None:
        return cached.copy()
    close = inputs["close"]
    position = int(close.index.get_loc(signal_date))
    required = max(lookback, STOCK_MA_DAYS - 1, 49)
    if position < required or position - skip < 0:
        return pd.DataFrame()
    current = close.iloc[position]
    momentum_end = close.iloc[position - skip]
    momentum_start = close.iloc[position - lookback]
    momentum = momentum_end.div(momentum_start).sub(1.0)
    stock_ma = close.iloc[position - STOCK_MA_DAYS + 1:position + 1].mean()
    liquidity = inputs["dollar_volume"].iloc[position - 49:position + 1].median()
    index_history = inputs["nasdaq"].reindex(close.index).ffill()
    index_momentum = (
        float(index_history.iloc[position - skip])
        / float(index_history.iloc[position - lookback])
        - 1.0
    )
    frame = pd.DataFrame({
        "price": current,
        "median_dollar_volume_50d": liquidity,
        "momentum": momentum,
        "stock_ma": stock_ma,
    }).replace([np.inf, -np.inf], np.nan)
    symbols = inputs["universe"](signal_date)
    if symbols is None:
        return pd.DataFrame()
    frame = frame.loc[frame.index.intersection(sorted(symbols))]
    frame = frame.loc[
        frame["price"].ge(MINIMUM_PRICE)
        & frame["median_dollar_volume_50d"].ge(
            MINIMUM_MEDIAN_DOLLAR_VOLUME
        )
        & frame["price"].gt(frame["stock_ma"])
        & frame["momentum"].gt(index_momentum)
    ].dropna()
    frame["momentum_excess_vs_nasdaq"] = frame["momentum"] - index_momentum
    frame = frame.sort_values(
        ["momentum_excess_vs_nasdaq", "median_dollar_volume_50d"],
        ascending=[False, False],
    )
    inputs["technical_cache"][cache_key] = frame.copy()
    return frame


def _profitable_symbols(signal_date: pd.Timestamp, inputs: dict) -> set[str]:
    stamp = pd.Timestamp(signal_date).normalize()
    cached = inputs["quality_cache"].get(stamp)
    if cached is not None:
        return cached
    snapshot = quarterly_growth_snapshot(
        inputs["quarterly"], stamp, MAXIMUM_FINANCIAL_AGE_DAYS
    )
    profitable = set(
        snapshot.index[snapshot["net_income_ttm"].astype(float).gt(0.0)].astype(str)
    )
    inputs["quality_cache"][stamp] = profitable
    return profitable


def generate_target_schedule(spec: dict, inputs: dict) -> pd.DataFrame:
    close = inputs["close"]
    index_close = inputs["nasdaq"].reindex(close.index).ffill()
    replay_start = pd.Timestamp(DEVELOPMENT_START) - pd.Timedelta(days=62)
    signals = scheduled_signal_dates(
        close.index,
        replay_start,
        DEVELOPMENT_END,
        str(spec["signal_frequency"]),
    )
    top_n = int(spec["top_n"])
    buffer_count = top_n * int(spec["rank_buffer_multiple"])
    previous: list[str] = []
    rows = []
    for signal_date in signals:
        effective = next_trading_date(close.index, signal_date)
        if (
            effective is None
            or effective < pd.Timestamp(DEVELOPMENT_START)
            or effective > pd.Timestamp(DEVELOPMENT_END)
        ):
            continue
        if not market_regime_is_on(signal_date, index_close, MARKET_MA_DAYS):
            selected = []
        else:
            ranking = _technical_ranking(signal_date, spec, inputs)
            if spec["quality_mode"] == "profitable" and not ranking.empty:
                ranking = ranking.loc[
                    ranking.index.isin(_profitable_symbols(signal_date, inputs))
                ]
            ranked = ranking.index.astype(str).tolist()
            buffered = set(ranked[:buffer_count])
            selected = [ticker for ticker in previous if ticker in buffered][:top_n]
            selected.extend(
                ticker for ticker in ranked
                if ticker not in selected and len(selected) < top_n
            )
            selected = selected[:top_n]
        previous = selected
        if not selected:
            rows.append({
                "effective_date": effective,
                "ticker": "__CASH__",
                "target_weight": 0.0,
                "base_transaction_cost_bps": 10.0,
            })
            continue
        for ticker in selected:
            rows.append({
                "effective_date": effective,
                "ticker": ticker,
                "target_weight": 1.0 / top_n,
                "base_transaction_cost_bps": 10.0,
            })
    targets = pd.DataFrame(rows)
    tickers = set(targets["ticker"].astype(str)) - {"__CASH__"}
    forbidden = sorted(tickers & FORBIDDEN_ETFS)
    if forbidden:
        raise RuntimeError(f"candidate selected forbidden ETFs: {forbidden}")
    return targets


def _generate_candidate(spec: dict, inputs: dict) -> tuple[dict[int, pd.DataFrame], pd.DataFrame]:
    targets = generate_target_schedule(spec, inputs)
    results = {}
    for cost in COSTS:
        stressed = targets.copy()
        stressed["base_transaction_cost_bps"] = float(cost)
        daily, _ = replay_can_slim_target_schedule(
            inputs["raw_close"],
            inputs["nasdaq"],
            stressed,
            DEVELOPMENT_START,
            DEVELOPMENT_END,
        )
        results[cost] = v23._canonicalize_result(
            daily, inputs["nasdaq"], inputs["qqq"]
        )
    return results, targets


def develop(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = DEVELOPMENT_OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v24 development output will not be overwritten: {output_dir}")
    inputs = _load_inputs()
    results_by_candidate = {}
    targets_by_candidate = {}
    summaries = {}
    for spec in protocol["candidate_grid"]:
        key = spec["key"]
        results, targets = _generate_candidate(spec, inputs)
        results_by_candidate[key] = results
        targets_by_candidate[key] = targets
        summaries[key] = v23._summary(results)

    selected, ranking = v23.select_candidate(summaries)
    folds = []
    for fold in protocol["walk_forward_folds"]:
        years = tuple(int(year) for year in fold["selection_years"])
        fold_selected, fold_ranking = v23.select_candidate(summaries, years)
        test_year = int(fold["test_year"])
        ranked_candidate = fold_selected or fold_ranking[0]["candidate"]
        test = next(
            row for row in summaries[ranked_candidate]["costs"]["50"]["annual"]
            if int(row["year"]) == test_year
        )
        folds.append({
            **fold,
            "selected_candidate": ranked_candidate,
            "training_gates_passed": fold_selected is not None,
            "test_excess_vs_qqq_50bps": float(test["excess_vs_qqq"]),
            "test_status": (
                "PASS" if float(test["excess_vs_qqq"]) > 0.0 else "BLOCKED"
            ),
        })

    output_dir.mkdir(parents=True, exist_ok=True)
    ranking_path = output_dir / "candidate_ranking.csv"
    pd.DataFrame(ranking).to_csv(ranking_path, index=False)
    summary_path = output_dir / "candidate_summaries.json"
    summary_path.write_text(
        json.dumps(summaries, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    folds_path = output_dir / "walk_forward_folds.csv"
    pd.DataFrame(folds).to_csv(folds_path, index=False)
    outputs = {
        "candidate_ranking": _file_binding(ranking_path),
        "candidate_summaries": _file_binding(summary_path),
        "walk_forward_folds": _file_binding(folds_path),
    }
    selected_spec = None
    selected_summary = None
    if selected is not None:
        selected_spec = next(
            spec for spec in protocol["candidate_grid"] if spec["key"] == selected
        )
        selected_summary = summaries[selected]
        targets_path = output_dir / "selected_targets.csv"
        targets_by_candidate[selected].to_csv(targets_path, index=False)
        outputs["selected_targets"] = _file_binding(targets_path)
        for cost in COSTS:
            path = output_dir / f"selected_daily_{cost}bps.csv"
            results_by_candidate[selected][cost].to_csv(path, index_label="date")
            outputs[f"selected_daily_{cost}bps"] = _file_binding(path)

    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V24_STOCK_MOMENTUM_DEVELOPMENT_RESULT",
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "development_status": "PASS" if selected is not None else "BLOCKED",
        "selected_candidate": selected,
        "selected_specification": selected_spec,
        "selected_summary": selected_summary,
        "walk_forward_folds": folds,
        "walk_forward_pass_count": sum(
            fold["test_status"] == "PASS" for fold in folds
        ),
        "all_walk_forward_folds_passed": bool(
            folds and all(fold["test_status"] == "PASS" for fold in folds)
        ),
        "contains_index_etf_holdings": False,
        "risk_off_asset": "CASH",
        "2026_used_for_development_or_selection": False,
        "outputs": outputs,
        "brokerage_or_trading_authorized": False,
        "broker_connection_used": False,
        "order_created": False,
        "capital_allocated": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**report, "manifest": _file_binding(manifest_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze_parser = subparsers.add_parser("freeze")
    freeze_parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    develop_parser = subparsers.add_parser("develop")
    develop_parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    develop_parser.add_argument(
        "--output-dir", type=Path, default=DEVELOPMENT_OUTPUT_DIR
    )
    args = parser.parse_args()
    report = (
        freeze_protocol(args.protocol)
        if args.command == "freeze"
        else develop(args.protocol, args.output_dir)
    )
    fields = (
        ("status", "candidate_count", "reserved_observation_start", "protocol")
        if args.command == "freeze"
        else (
            "development_status",
            "selected_candidate",
            "walk_forward_pass_count",
            "release_status",
            "manifest",
        )
    )
    print(json.dumps({field: report[field] for field in fields}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
