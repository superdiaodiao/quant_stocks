#!/usr/bin/env python3
"""Develop monthly momentum inside a point-in-time large-liquid stock pool.

v24 showed profitable momentum was materially stronger than unrestricted
technical momentum, while its holdings audit revealed repeated concentration
in extreme smaller names. This frozen grid first takes the 25/50/100 most
liquid profitable common equities known at each signal date, then selects the
Top-3 or Top-5 momentum leaders. Risk-off and residual capital stay in cash.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v23_stock_only_frequency as v23
from scripts import research_v24_stock_momentum_development as v24
from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
)
from src.research.can_slim import replay_can_slim_target_schedule
from src.research.universe_history import snapshot_directory
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
LIQUID_POOL_SIZES = (25, 50, 100)
TOP_NS = (3, 5)
OUTPUT_DIR = Path(
    "output/research_only/v26/large_liquid_stock_momentum_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
DEVELOPMENT_OUTPUT_DIR = OUTPUT_DIR / "development_results"
V24_SUMMARIES = Path(
    "output/research_only/v24/stock_momentum_20260830_retry2/"
    "development_results/candidate_summaries.json"
)
V24_MANIFEST = Path(
    "output/research_only/v24/stock_momentum_20260830_retry2/"
    "development_results/manifest.json"
)


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
    return [
        {
            "key": (
                f"mom{lookback}_skip{skip}_liquid{pool_size}_top{top_n}_"
                "profitable_monthly"
            ),
            "lookback_sessions": lookback,
            "skip_recent_sessions": skip,
            "liquid_pool_size": pool_size,
            "top_n": top_n,
            "signal_frequency": "monthly",
            "rank_buffer_multiple": 1,
            "quality_mode": "profitable",
        }
        for lookback, skip in LOOKBACKS
        for pool_size in LIQUID_POOL_SIZES
        for top_n in TOP_NS
    ]


def _validate_v24_source() -> dict:
    summaries = json.loads(V24_SUMMARIES.read_text(encoding="utf-8"))
    key = "mom252_skip21_monthly_top5_buffer1_profitable"
    source = summaries[key]["costs"]["50"]
    if source["annual_wins_vs_qqq"] != 4:
        raise RuntimeError("v24 source annual-win count changed")
    if source["compounded_excess_vs_qqq"] <= 0.0:
        raise RuntimeError("v24 source compounded excess changed")
    return {
        "candidate": key,
        "annual_wins_vs_qqq_50bps": source["annual_wins_vs_qqq"],
        "compounded_excess_vs_qqq_50bps": source[
            "compounded_excess_vs_qqq"
        ],
        "drawdown_lag_vs_qqq_50bps": source["drawdown_lag_vs_qqq"],
        "turnover_50bps": source["turnover"],
    }


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict:
    path = Path(path)
    if path.exists():
        raise RuntimeError(f"v26 protocol will not be overwritten: {path}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V26_LARGE_LIQUID_STOCK_MOMENTUM_PRECOMMITMENT",
        "status": "FROZEN_NOT_DEVELOPED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "objective": (
            "Select momentum leaders only from the most liquid profitable "
            "point-in-time common equities; hold cash when risk-off."
        ),
        "source_diagnosis": _validate_v24_source(),
        "development_window": {
            "start": DEVELOPMENT_START,
            "end": DEVELOPMENT_END,
            "years": list(DEVELOPMENT_YEARS),
        },
        "reserved_observation_start": OBSERVATION_START,
        "candidate_grid": candidate_specs(),
        "candidate_count": len(candidate_specs()),
        "common_parameters": {
            "signal_frequency": "monthly",
            "quality_mode": "positive point-in-time TTM net income",
            "minimum_price": v24.MINIMUM_PRICE,
            "minimum_median_dollar_volume": (
                v24.MINIMUM_MEDIAN_DOLLAR_VOLUME
            ),
            "market_ma_days": v24.MARKET_MA_DAYS,
            "stock_ma_days": v24.STOCK_MA_DAYS,
            "relative_momentum_gate": "stock momentum > Nasdaq momentum",
            "liquid_pool_rule": (
                "rank eligible profitable common equities by trailing 50-session "
                "median dollar volume, keep the configured pool size, then rank "
                "momentum inside that pool"
            ),
            "risk_off_asset": "CASH",
            "residual_weight_asset": "CASH",
            "forbidden_etfs": sorted(FORBIDDEN_ETFS),
            "signal_execution": "completed month close to next trading close",
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
            "v24_signal_helpers": _file_binding(
                Path("scripts/research_v24_stock_momentum_development.py")
            ),
            "v24_candidate_summaries": _file_binding(V24_SUMMARIES),
            "v24_manifest": _file_binding(V24_MANIFEST),
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
            "qqq_history": _file_binding(Path(v24.v15.QQQ_HISTORY["path"])),
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
        raise RuntimeError("v26 protocol status changed")
    if protocol["candidate_grid"] != candidate_specs():
        raise RuntimeError("v26 candidate grid changed")
    if protocol["source_diagnosis"] != _validate_v24_source():
        raise RuntimeError("v26 source diagnosis changed")
    if protocol["release_status"] != "BLOCKED" or protocol["promotion_eligible"]:
        raise RuntimeError("v26 release boundary changed")
    for name, binding in protocol["input_bindings"].items():
        if "content_manifest_sha256" in binding:
            actual = _directory_binding(Path(binding["path"]), binding["pattern"])
            if actual != binding:
                raise RuntimeError(f"v26 directory binding changed for {name}")
        elif _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"v26 file binding changed for {name}")
    return protocol, protocol_sha


def _load_inputs() -> dict:
    inputs = v24._load_inputs()
    inputs["large_liquid_cache"] = {}
    return inputs


def _large_liquid_ranking(
    signal_date: pd.Timestamp,
    spec: dict,
    inputs: dict,
) -> pd.DataFrame:
    key = (
        pd.Timestamp(signal_date).normalize(),
        int(spec["lookback_sessions"]),
        int(spec["skip_recent_sessions"]),
        int(spec["liquid_pool_size"]),
    )
    cached = inputs["large_liquid_cache"].get(key)
    if cached is not None:
        return cached.copy()
    ranking = v24._technical_ranking(signal_date, spec, inputs)
    if not ranking.empty:
        ranking = ranking.loc[
            ranking.index.isin(v24._profitable_symbols(signal_date, inputs))
        ]
        ranking = ranking.nlargest(
            int(spec["liquid_pool_size"]),
            "median_dollar_volume_50d",
            keep="first",
        ).sort_values(
            ["momentum_excess_vs_nasdaq", "median_dollar_volume_50d"],
            ascending=[False, False],
        )
    inputs["large_liquid_cache"][key] = ranking.copy()
    return ranking


def generate_target_schedule(spec: dict, inputs: dict) -> pd.DataFrame:
    close = inputs["close"]
    index_close = inputs["nasdaq"].reindex(close.index).ffill()
    replay_start = pd.Timestamp(DEVELOPMENT_START) - pd.Timedelta(days=62)
    signals = scheduled_signal_dates(
        close.index, replay_start, DEVELOPMENT_END, "monthly"
    )
    rows = []
    top_n = int(spec["top_n"])
    for signal_date in signals:
        effective = next_trading_date(close.index, signal_date)
        if (
            effective is None
            or effective < pd.Timestamp(DEVELOPMENT_START)
            or effective > pd.Timestamp(DEVELOPMENT_END)
        ):
            continue
        if market_regime_is_on(signal_date, index_close, v24.MARKET_MA_DAYS):
            selected = _large_liquid_ranking(
                signal_date, spec, inputs
            ).head(top_n).index.astype(str).tolist()
        else:
            selected = []
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
        raise RuntimeError(f"v26 development output will not be overwritten: {output_dir}")
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
        "stage": "V26_LARGE_LIQUID_STOCK_MOMENTUM_DEVELOPMENT_RESULT",
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
