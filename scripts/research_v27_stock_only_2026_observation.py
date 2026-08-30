#!/usr/bin/env python3
"""Freeze and run the v26 stock-only model on January-July 2026 once.

The v26 candidate was selected using no 2026 observations. This runner binds
that exact selection and precommits seven-month QQQ-relative gates before
calculating 2026 performance. Holdings remain individual common equities or
cash; QQQ is only the primary benchmark.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v15_benchmark_core_development as v15
from scripts import research_v23_stock_only_frequency as v23
from scripts import research_v24_stock_momentum_development as v24
from scripts import research_v26_large_liquid_stock_momentum as v26
from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
)
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
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


OBSERVATION_START = "2026-01-01"
OBSERVATION_END = "2026-07-31"
OBSERVATION_MONTHS = tuple(f"2026-{month:02d}" for month in range(1, 8))
COSTS = v23.COSTS
FORBIDDEN_ETFS = v23.FORBIDDEN_ETFS
SELECTED_CANDIDATE = "mom63_skip0_liquid25_top5_profitable_monthly"
MINIMUM_MONTHLY_WINS = 4
MAXIMUM_DRAWDOWN_LAG = 0.10
OUTPUT_DIR = Path(
    "output/research_only/v27/stock_only_model_isolated_2026_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
RESULT_OUTPUT_DIR = OUTPUT_DIR / "observation_results"
V26_MANIFEST = v26.DEVELOPMENT_OUTPUT_DIR / "manifest.json"
V26_TARGETS = v26.DEVELOPMENT_OUTPUT_DIR / "selected_targets.csv"
V26_RANKING = v26.DEVELOPMENT_OUTPUT_DIR / "candidate_ranking.csv"


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


def _selected_specification() -> dict:
    manifest = json.loads(V26_MANIFEST.read_text(encoding="utf-8"))
    if manifest["development_status"] != "PASS":
        raise RuntimeError("v26 development status changed")
    if manifest["selected_candidate"] != SELECTED_CANDIDATE:
        raise RuntimeError("v26 selected candidate changed")
    if manifest["2026_used_for_development_or_selection"]:
        raise RuntimeError("v26 unexpectedly used 2026")
    if manifest["contains_index_etf_holdings"]:
        raise RuntimeError("v26 unexpectedly contains index ETF holdings")
    specification = manifest["selected_specification"]
    expected = next(
        spec for spec in v26.candidate_specs() if spec["key"] == SELECTED_CANDIDATE
    )
    if specification != expected:
        raise RuntimeError("v26 selected specification changed")
    return specification


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict:
    path = Path(path)
    if path.exists():
        raise RuntimeError(f"v27 protocol will not be overwritten: {path}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    specification = _selected_specification()
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V27_STOCK_ONLY_MODEL_ISOLATED_2026_PRECOMMITMENT",
        "status": "FROZEN_NOT_OBSERVED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "selected_candidate": SELECTED_CANDIDATE,
        "selected_specification": specification,
        "model_selection_window": {
            "start": v23.DEVELOPMENT_START,
            "end": v23.DEVELOPMENT_END,
            "2026_used": False,
        },
        "historical_coverage_adjudication": {
            "2019_monthly_universe_signals_available": 4,
            "2019_monthly_universe_signals_expected": 12,
            "2019_classification": "PARTIAL_PIT_UNIVERSE_COVERAGE",
            "2019_missing_signal_dates": [
                "2019-02-28",
                "2019-03-29",
                "2019-04-30",
                "2019-05-31",
                "2019-07-31",
                "2019-08-30",
                "2019-09-30",
                "2019-10-31",
            ],
            "2020_2025_monthly_universe_signals_available": 72,
            "2020_2025_monthly_universe_signals_expected": 72,
            "missing_universe_policy": "CASH_NO_BACKFILL_NO_STALE_EXTENSION",
        },
        "observation_window": {
            "start": OBSERVATION_START,
            "end": OBSERVATION_END,
            "required_months": list(OBSERVATION_MONTHS),
            "performance_calculated_during_freeze": False,
        },
        "primary_benchmark": "QQQ_TOTAL_RETURN",
        "secondary_benchmark": "NASDAQ_COMPOSITE",
        "cost_bps": list(COSTS),
        "acceptance_gates": {
            "positive_compounded_excess_vs_qqq_at_30_and_50bps": True,
            "minimum_monthly_wins_vs_qqq_at_50bps": MINIMUM_MONTHLY_WINS,
            "maximum_drawdown_lag_vs_qqq_percentage_points": (
                MAXIMUM_DRAWDOWN_LAG * 100.0
            ),
            "all_seven_observation_months_required": True,
            "positive_targets_must_exclude_all_forbidden_etfs": True,
        },
        "precommitted_decision_policy": {
            "if_all_gates_pass": {
                "model_isolated_historical_observation": "PASS",
                "additional_performance_observation_months_required": 0,
                "minimum_future_operational_dry_run_cycles_required": 1,
            },
            "if_any_gate_fails": {
                "model_isolated_historical_observation": "BLOCKED",
                "candidate_rejected_for_live_progression": True,
            },
        },
        "exposure_classification": (
            "MODEL_EXCLUDED_RESEARCHER_EXPOSED; valid for model-isolated "
            "evaluation, not a claim of researcher-blind forward evidence"
        ),
        "common_parameters": {
            "risk_off_asset": "CASH",
            "residual_weight_asset": "CASH",
            "forbidden_etfs": sorted(FORBIDDEN_ETFS),
            "signal_execution": "completed month close to next trading close",
        },
        "input_bindings": {
            "runner": _file_binding(runner),
            "v23_evaluation_helpers": _file_binding(
                Path("scripts/research_v23_stock_only_frequency.py")
            ),
            "v24_signal_helpers": _file_binding(
                Path("scripts/research_v24_stock_momentum_development.py")
            ),
            "v26_selector": _file_binding(
                Path("scripts/research_v26_large_liquid_stock_momentum.py")
            ),
            "v26_manifest": _file_binding(V26_MANIFEST),
            "v26_targets": _file_binding(V26_TARGETS),
            "v26_ranking": _file_binding(V26_RANKING),
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
        "parameters_frozen_before_observation": True,
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
    if protocol["status"] != "FROZEN_NOT_OBSERVED":
        raise RuntimeError("v27 protocol status changed")
    if protocol["selected_specification"] != _selected_specification():
        raise RuntimeError("v27 selected specification changed")
    if protocol["release_status"] != "BLOCKED" or protocol["promotion_eligible"]:
        raise RuntimeError("v27 release boundary changed")
    for name, binding in protocol["input_bindings"].items():
        if "content_manifest_sha256" in binding:
            actual = _directory_binding(Path(binding["path"]), binding["pattern"])
            if actual != binding:
                raise RuntimeError(f"v27 directory binding changed for {name}")
        elif _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"v27 file binding changed for {name}")
    return protocol, protocol_sha


def _load_inputs() -> dict:
    load_start = (
        pd.Timestamp(OBSERVATION_START) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")
    raw_close, dollar_volume = load_panel(
        CLEANED_PRICE_DATA_DIR, load_start, OBSERVATION_END
    )
    close = back_adjust_common_splits(raw_close).sort_index()
    dollar_volume = dollar_volume.reindex_like(close)
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"].sort_index().loc[:OBSERVATION_END]
    qqq = pd.read_csv(
        v15.QQQ_HISTORY["path"], index_col="date", parse_dates=True
    ).sort_index().loc[:OBSERVATION_END]
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
                maximum_age_days=v24.MAXIMUM_SNAPSHOT_AGE_DAYS,
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
        "large_liquid_cache": {},
    }


def generate_observation_targets(spec: dict, inputs: dict) -> pd.DataFrame:
    close = inputs["close"]
    index_close = inputs["nasdaq"].reindex(close.index).ffill()
    replay_start = pd.Timestamp(OBSERVATION_START) - pd.Timedelta(days=62)
    signals = scheduled_signal_dates(
        close.index, replay_start, OBSERVATION_END, "monthly"
    )
    rows = []
    top_n = int(spec["top_n"])
    for signal_date in signals:
        effective = next_trading_date(close.index, signal_date)
        if (
            effective is None
            or effective < pd.Timestamp(OBSERVATION_START)
            or effective > pd.Timestamp(OBSERVATION_END)
        ):
            continue
        if market_regime_is_on(signal_date, index_close, v24.MARKET_MA_DAYS):
            selected = v26._large_liquid_ranking(
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
        raise RuntimeError(f"observation selected forbidden ETFs: {forbidden}")
    return targets


def _canonicalize_result(
    result: pd.DataFrame,
    nasdaq: pd.Series,
    qqq: pd.DataFrame,
) -> pd.DataFrame:
    dates = nasdaq.loc[OBSERVATION_START:OBSERVATION_END].dropna().index
    missing = dates.difference(result.index)
    if len(missing):
        raise RuntimeError(f"strategy is missing Nasdaq sessions: {list(missing[:5])}")
    result = result.reindex(dates).copy()
    qqq_dates = qqq.loc[:OBSERVATION_END].dropna(subset=["close"]).index
    qqq_index = v15.qqq_total_return_index(
        qqq,
        qqq_dates,
        allowed_market_closed=pd.Series(False, index=qqq_dates),
    )
    qqq_returns = qqq_index.pct_change(fill_method=None)
    missing_qqq = dates.difference(qqq_returns.dropna().index)
    if len(missing_qqq):
        raise RuntimeError(f"QQQ is missing Nasdaq sessions: {list(missing_qqq[:5])}")
    result["qqq"] = qqq_returns.reindex(dates)
    return result


def evaluate_observation(results: dict[int, pd.DataFrame], targets: pd.DataFrame) -> dict:
    costs = {}
    for cost, result in results.items():
        monthly = (
            (1.0 + result[["strategy", "benchmark", "qqq"]])
            .groupby(result.index.to_period("M"))
            .prod()
            - 1.0
        )
        monthly["excess_vs_qqq"] = monthly["strategy"] - monthly["qqq"]
        strategy = float((1.0 + result["strategy"]).prod() - 1.0)
        qqq = float((1.0 + result["qqq"]).prod() - 1.0)
        nasdaq = float((1.0 + result["benchmark"]).prod() - 1.0)
        strategy_drawdown = v23._maximum_drawdown(result["strategy"])
        qqq_drawdown = v23._maximum_drawdown(result["qqq"])
        costs[str(cost)] = {
            "monthly": [
                {"month": str(month), **values}
                for month, values in monthly.to_dict(orient="index").items()
            ],
            "monthly_wins_vs_qqq": int(monthly["excess_vs_qqq"].gt(0.0).sum()),
            "compounded_strategy": strategy,
            "compounded_qqq": qqq,
            "compounded_nasdaq": nasdaq,
            "compounded_excess_vs_qqq": strategy - qqq,
            "compounded_excess_vs_nasdaq": strategy - nasdaq,
            "strategy_maximum_drawdown": strategy_drawdown,
            "qqq_maximum_drawdown": qqq_drawdown,
            "drawdown_lag_vs_qqq": max(
                0.0, qqq_drawdown - strategy_drawdown
            ),
            "turnover": float(result["turnover"].sum()),
        }
    observed_months = sorted({
        pd.Timestamp(date).to_period("M").strftime("%Y-%m")
        for date in results[50].index
    })
    decision_months = sorted({
        pd.Timestamp(date).to_period("M").strftime("%Y-%m")
        for date in pd.to_datetime(targets["effective_date"])
    })
    tickers = set(targets["ticker"].astype(str)) - {"__CASH__"}
    gates = {
        "all_observation_months_present": observed_months == list(OBSERVATION_MONTHS),
        "all_decision_months_present": decision_months == list(OBSERVATION_MONTHS),
        "no_forbidden_etf_targets": not bool(tickers & FORBIDDEN_ETFS),
        "positive_excess_30bps": (
            costs["30"]["compounded_excess_vs_qqq"] > 0.0
        ),
        "positive_excess_50bps": (
            costs["50"]["compounded_excess_vs_qqq"] > 0.0
        ),
        "monthly_wins_50bps": (
            costs["50"]["monthly_wins_vs_qqq"] >= MINIMUM_MONTHLY_WINS
        ),
        "drawdown_50bps": (
            costs["50"]["drawdown_lag_vs_qqq"] <= MAXIMUM_DRAWDOWN_LAG
        ),
    }
    return {
        "observed_months": observed_months,
        "decision_months": decision_months,
        "costs": costs,
        "gates": gates,
        "all_precommitted_gates_passed": all(gates.values()),
    }


def observe(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = RESULT_OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v27 observation output will not be overwritten: {output_dir}")
    inputs = _load_inputs()
    targets = generate_observation_targets(
        protocol["selected_specification"], inputs
    )
    results = {}
    for cost in COSTS:
        stressed = targets.copy()
        stressed["base_transaction_cost_bps"] = float(cost)
        daily, _ = replay_can_slim_target_schedule(
            inputs["raw_close"],
            inputs["nasdaq"],
            stressed,
            OBSERVATION_START,
            OBSERVATION_END,
        )
        results[cost] = _canonicalize_result(
            daily, inputs["nasdaq"], inputs["qqq"]
        )
    evaluation = evaluate_observation(results, targets)

    output_dir.mkdir(parents=True, exist_ok=True)
    targets_path = output_dir / "observed_targets.csv"
    targets.to_csv(targets_path, index=False)
    outputs = {"observed_targets": _file_binding(targets_path)}
    for cost in COSTS:
        path = output_dir / f"observed_daily_{cost}bps.csv"
        results[cost].to_csv(path, index_label="date")
        outputs[f"observed_daily_{cost}bps"] = _file_binding(path)
    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V27_STOCK_ONLY_MODEL_ISOLATED_2026_RESULT",
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "selected_candidate": SELECTED_CANDIDATE,
        "observation_status": (
            "PASS" if evaluation["all_precommitted_gates_passed"] else "BLOCKED"
        ),
        "evaluation": evaluation,
        "contains_index_etf_holdings": False,
        "risk_off_asset": "CASH",
        "additional_performance_observation_months_required": (
            0 if evaluation["all_precommitted_gates_passed"] else None
        ),
        "minimum_future_operational_dry_run_cycles_required": (
            1 if evaluation["all_precommitted_gates_passed"] else None
        ),
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
    observe_parser = subparsers.add_parser("observe")
    observe_parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    observe_parser.add_argument("--output-dir", type=Path, default=RESULT_OUTPUT_DIR)
    args = parser.parse_args()
    report = (
        freeze_protocol(args.protocol)
        if args.command == "freeze"
        else observe(args.protocol, args.output_dir)
    )
    fields = (
        ("status", "selected_candidate", "observation_window", "protocol")
        if args.command == "freeze"
        else (
            "observation_status",
            "selected_candidate",
            "additional_performance_observation_months_required",
            "release_status",
            "manifest",
        )
    )
    print(json.dumps({field: report[field] for field in fields}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
