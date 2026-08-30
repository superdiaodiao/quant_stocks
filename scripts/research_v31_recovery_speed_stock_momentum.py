#!/usr/bin/env python3
"""Search stock-only recovery speed after the complete 2019 path audit.

The v30 baseline loses 2019 mainly because the 200-session market filter misses
the January-February recovery and concentrated monthly selections lose in May
and July.  This predeclared grid varies only market recovery speed, short/medium
momentum horizon, and Top-5/Top-10 breadth.  Eligibility requires beating the
Nasdaq Composite in every 2019-2025 calendar year at 50 bps cost.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

from scripts import research_v23_stock_only_frequency as v23
from scripts import research_v26_large_liquid_stock_momentum as v26
from scripts import research_v29_recovered_2019_stock_momentum as v29
from scripts import research_v30_2019_selection_path_adjudication as v30
from src.research.can_slim import replay_can_slim_target_schedule
from src.research.universe_history import snapshot_directory, universe_as_of
from src.strategy.common import (
    market_regime_is_on,
    next_trading_date,
    scheduled_signal_dates,
)


DEVELOPMENT_START = v29.DEVELOPMENT_START
DEVELOPMENT_END = v29.DEVELOPMENT_END
DEVELOPMENT_YEARS = v29.DEVELOPMENT_YEARS
OBSERVATION_START = v29.OBSERVATION_START
COSTS = v29.COSTS
FORBIDDEN_ETFS = v29.FORBIDDEN_ETFS
LOOKBACKS = (21, 42, 63)
MARKET_MA_DAYS = (0, 50, 100, 150, 200)
TOP_NS = (5, 10)
LIQUID_POOL_SIZE = 25
MAXIMUM_DRAWDOWN_LAG = 0.10
GAP_SIGNAL_DATES = tuple(pd.to_datetime([
    "2018-12-31",
    "2019-04-30",
    "2019-05-31",
    "2019-08-30",
    "2019-09-30",
]))

OUTPUT_DIR = Path(
    "output/research_only/v31/recovery_speed_stock_momentum_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
DEVELOPMENT_OUTPUT_DIR = OUTPUT_DIR / "development_results"
V30_MANIFEST = v30.RESULT_OUTPUT_DIR / "manifest.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_binding(path: Path) -> dict:
    path = Path(path)
    return {"path": str(path), "sha256": _sha256(path)}


def _directory_binding(path: Path, pattern: str) -> dict:
    return v26._directory_binding(Path(path), pattern)


def candidate_specs() -> list[dict]:
    return [
        {
            "key": (
                f"mom{lookback}_marketma{market_ma}_liquid{LIQUID_POOL_SIZE}_"
                f"top{top_n}_profitable_monthly"
            ),
            "lookback_sessions": lookback,
            "skip_recent_sessions": 0,
            "liquid_pool_size": LIQUID_POOL_SIZE,
            "top_n": top_n,
            "signal_frequency": "monthly",
            "rank_buffer_multiple": 1,
            "quality_mode": "profitable",
            "market_ma_days": market_ma,
        }
        for lookback in LOOKBACKS
        for market_ma in MARKET_MA_DAYS
        for top_n in TOP_NS
    ]


def _validated_v30_source() -> dict:
    manifest = json.loads(V30_MANIFEST.read_text(encoding="utf-8"))
    audit = manifest["audit"]
    if not audit["strategy_selection_path_complete"]:
        raise RuntimeError("v30 selection path is no longer complete")
    if audit["full_exchange_membership_recovered"]:
        raise RuntimeError("v30 membership limitation unexpectedly changed")
    annual = manifest["selected_summary"]["costs"]["50"]["annual"]
    losses = [
        int(row["year"])
        for row in annual
        if float(row["excess_vs_nasdaq"]) <= 0.0
    ]
    if losses != [2019]:
        raise RuntimeError(f"v30 Nasdaq failure diagnosis changed: {losses}")
    return {
        "base_candidate": manifest["selected_candidate"],
        "nasdaq_failure_years_50bps": losses,
        "strategy_selection_path_complete": True,
        "full_exchange_membership_recovered": False,
    }


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict:
    path = Path(path)
    if path.exists():
        raise RuntimeError(f"v31 protocol will not be overwritten: {path}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V31_RECOVERY_SPEED_STOCK_MOMENTUM_PRECOMMITMENT",
        "status": "FROZEN_NOT_DEVELOPED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "objective": (
            "Beat Nasdaq in every 2019-2025 year with pure stocks at 50bps "
            "without using 2026 for parameter selection."
        ),
        "source_diagnosis": _validated_v30_source(),
        "development_window": {
            "start": DEVELOPMENT_START,
            "end": DEVELOPMENT_END,
            "years": list(DEVELOPMENT_YEARS),
        },
        "reserved_observation_start": OBSERVATION_START,
        "candidate_grid": candidate_specs(),
        "candidate_count": len(candidate_specs()),
        "controlled_dimensions": {
            "lookback_sessions": list(LOOKBACKS),
            "market_ma_days": list(MARKET_MA_DAYS),
            "top_n": list(TOP_NS),
            "liquid_pool_size": LIQUID_POOL_SIZE,
            "signal_frequency": "monthly",
        },
        "gap_universe_policy": {
            "signal_dates": [
                stamp.strftime("%Y-%m-%d") for stamp in GAP_SIGNAL_DATES
            ],
            "resolution": "BOUNDING_SNAPSHOT_INTERSECTION",
            "selected_winner_requires_post_selection_scenario_audit": True,
        },
        "cost_bps": list(COSTS),
        "primary_benchmark": "NASDAQ_COMPOSITE",
        "secondary_benchmark": "QQQ_TOTAL_RETURN",
        "eligibility_gates": {
            "annual_wins_vs_nasdaq_at_50bps": len(DEVELOPMENT_YEARS),
            "positive_compounded_excess_vs_nasdaq_at_30_and_50bps": True,
            "maximum_drawdown_lag_vs_qqq_percentage_points": (
                MAXIMUM_DRAWDOWN_LAG * 100.0
            ),
            "forbidden_etfs_absent": True,
        },
        "selection_order": [
            "eligible first",
            "annual wins versus Nasdaq at 50bps descending",
            "worst annual excess versus Nasdaq at 50bps descending",
            "compounded excess versus Nasdaq at 50bps descending",
            "drawdown lag versus QQQ ascending",
            "turnover ascending",
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
            "v26_candidate_helpers": _file_binding(
                Path("scripts/research_v26_large_liquid_stock_momentum.py")
            ),
            "v29_recovered_universe_helpers": _file_binding(
                Path("scripts/research_v29_recovered_2019_stock_momentum.py")
            ),
            "v30_selection_path_helpers": _file_binding(
                Path("scripts/research_v30_2019_selection_path_adjudication.py")
            ),
            "v30_manifest": _file_binding(V30_MANIFEST),
            "formal_universe_snapshots": _directory_binding(
                snapshot_directory(), "nasdaq_listed_*.csv"
            ),
            "price_directory": _directory_binding(
                Path(v26.CLEANED_PRICE_DATA_DIR), "*.csv"
            ),
            "nasdaq_index": _file_binding(Path(v26.NASDAQ_INDEX_FILE)),
            "qqq_history": _file_binding(Path(v26.v24.v15.QQQ_HISTORY["path"])),
            "quarterly_fundamentals": _file_binding(
                Path(v26.POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE)
            ),
        },
        "parameters_frozen_before_development": True,
        "2026_used_for_development_or_selection": False,
        "contains_index_etf_holdings": False,
        "brokerage_or_trading_authorized": False,
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
        raise RuntimeError("v31 protocol status changed")
    if protocol["candidate_grid"] != candidate_specs():
        raise RuntimeError("v31 candidate grid changed")
    if protocol["source_diagnosis"] != _validated_v30_source():
        raise RuntimeError("v31 source diagnosis changed")
    if protocol["release_status"] != "BLOCKED" or protocol["promotion_eligible"]:
        raise RuntimeError("v31 release boundary changed")
    for name, binding in protocol["input_bindings"].items():
        if "content_manifest_sha256" in binding:
            actual = _directory_binding(Path(binding["path"]), binding["pattern"])
            if actual != binding:
                raise RuntimeError(f"v31 directory binding changed for {name}")
        elif _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"v31 file binding changed for {name}")
    return protocol, protocol_sha


def load_gap_resolved_inputs() -> dict:
    inputs = v29._load_inputs()
    snapshots = v30.normalize_meta_identity(v29.load_repaired_universe_snapshots())
    gap_universes = {}
    for signal_date in GAP_SIGNAL_DATES:
        prior_date = max(stamp for stamp in snapshots if stamp <= signal_date)
        next_date = min(stamp for stamp in snapshots if stamp > signal_date)
        gap_universes[signal_date] = snapshots[prior_date] & snapshots[next_date]
    universe_cache = {}

    def universe(signal_date):
        stamp = pd.Timestamp(signal_date).normalize()
        if stamp in gap_universes:
            return gap_universes[stamp] - FORBIDDEN_ETFS
        if stamp not in universe_cache:
            symbols = universe_as_of(
                snapshots,
                stamp,
                maximum_age_days=v29.MAXIMUM_SNAPSHOT_AGE_DAYS,
            )
            universe_cache[stamp] = (
                None if symbols is None else set(symbols) - FORBIDDEN_ETFS
            )
        return universe_cache[stamp]

    inputs["universe"] = universe
    inputs["technical_cache"] = {}
    inputs["quality_cache"] = {}
    inputs["large_liquid_cache"] = {}
    inputs["gap_universes"] = gap_universes
    inputs["normalized_snapshots"] = snapshots
    return inputs


def _market_regime_on(
    signal_date: pd.Timestamp,
    index_close: pd.Series,
    market_ma_days: int,
) -> bool:
    if market_ma_days == 0:
        return True
    return market_regime_is_on(signal_date, index_close, market_ma_days)


def generate_target_schedule(spec: dict, inputs: dict) -> pd.DataFrame:
    close = inputs["close"]
    index_close = inputs["nasdaq"].reindex(close.index).ffill()
    replay_start = pd.Timestamp(DEVELOPMENT_START) - pd.Timedelta(days=62)
    signals = scheduled_signal_dates(
        close.index, replay_start, DEVELOPMENT_END, spec["signal_frequency"]
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
        if _market_regime_on(
            signal_date, index_close, int(spec["market_ma_days"])
        ):
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
    selected_tickers = set(targets["ticker"].astype(str)) - {"__CASH__"}
    forbidden = sorted(selected_tickers & FORBIDDEN_ETFS)
    if forbidden:
        raise RuntimeError(f"candidate selected forbidden ETFs: {forbidden}")
    return targets


def _generate_candidate(spec: dict, inputs: dict):
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


def _period_key(years: tuple[int, ...]) -> str:
    return f"{min(years)}-{max(years)}"


def _selection_row(key: str, summary: dict, years: tuple[int, ...]) -> dict:
    required_wins = len(years)
    selected_costs = {}
    for cost in COSTS:
        annual = [
            row for row in summary["costs"][str(cost)]["annual"]
            if int(row["year"]) in years
        ]
        if tuple(sorted(int(row["year"]) for row in annual)) != years:
            raise RuntimeError(f"{key} cost {cost} has incomplete annual rows")
        period = summary["costs"][str(cost)]["periods"][_period_key(years)]
        selected_costs[cost] = {
            "wins": sum(float(row["excess_vs_nasdaq"]) > 0.0 for row in annual),
            "worst": min(float(row["excess_vs_nasdaq"]) for row in annual),
            "compounded_excess": period["compounded_excess_vs_nasdaq"],
            "drawdown_lag": period["drawdown_lag_vs_qqq"],
            "turnover": period["turnover"],
        }
    eligible = bool(
        selected_costs[30]["compounded_excess"] > 0.0
        and selected_costs[50]["compounded_excess"] > 0.0
        and selected_costs[50]["wins"] == required_wins
        and selected_costs[50]["drawdown_lag"] <= MAXIMUM_DRAWDOWN_LAG
    )
    return {
        "candidate": key,
        "eligible": eligible,
        "required_wins": required_wins,
        "wins_vs_nasdaq_50bps": selected_costs[50]["wins"],
        "worst_annual_excess_vs_nasdaq_50bps": selected_costs[50]["worst"],
        "compounded_excess_vs_nasdaq_30bps": selected_costs[30]["compounded_excess"],
        "compounded_excess_vs_nasdaq_50bps": selected_costs[50]["compounded_excess"],
        "drawdown_lag_vs_qqq_50bps": selected_costs[50]["drawdown_lag"],
        "turnover_50bps": selected_costs[50]["turnover"],
    }


def select_candidate(
    summaries: dict[str, dict],
    years: tuple[int, ...] = DEVELOPMENT_YEARS,
) -> tuple[str | None, list[dict]]:
    ranking = [
        _selection_row(key, summary, years)
        for key, summary in summaries.items()
    ]
    ranking.sort(key=lambda row: (
        not row["eligible"],
        -row["wins_vs_nasdaq_50bps"],
        -row["worst_annual_excess_vs_nasdaq_50bps"],
        -row["compounded_excess_vs_nasdaq_50bps"],
        row["drawdown_lag_vs_qqq_50bps"],
        row["turnover_50bps"],
        row["candidate"],
    ))
    for rank, row in enumerate(ranking, start=1):
        row["rank"] = rank
    selected = next(
        (row["candidate"] for row in ranking if row["eligible"]), None
    )
    return selected, ranking


def develop(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = DEVELOPMENT_OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v31 output will not be overwritten: {output_dir}")
    inputs = load_gap_resolved_inputs()
    results_by_candidate = {}
    targets_by_candidate = {}
    summaries = {}
    for spec in protocol["candidate_grid"]:
        key = spec["key"]
        results, targets = _generate_candidate(spec, inputs)
        results_by_candidate[key] = results
        targets_by_candidate[key] = targets
        summaries[key] = v23._summary(results)
    selected, ranking = select_candidate(summaries)

    folds = []
    for fold in protocol["walk_forward_folds"]:
        years = tuple(int(year) for year in fold["selection_years"])
        fold_selected, fold_ranking = select_candidate(summaries, years)
        candidate = fold_selected or fold_ranking[0]["candidate"]
        test_year = int(fold["test_year"])
        test = next(
            row for row in summaries[candidate]["costs"]["50"]["annual"]
            if int(row["year"]) == test_year
        )
        folds.append({
            **fold,
            "selected_candidate": candidate,
            "training_gates_passed": fold_selected is not None,
            "test_excess_vs_nasdaq_50bps": float(test["excess_vs_nasdaq"]),
            "test_status": (
                "PASS" if float(test["excess_vs_nasdaq"]) > 0.0 else "BLOCKED"
            ),
        })

    output_dir.mkdir(parents=True, exist_ok=True)
    ranking_path = output_dir / "candidate_ranking.csv"
    pd.DataFrame(ranking).to_csv(ranking_path, index=False)
    summaries_path = output_dir / "candidate_summaries.json"
    summaries_path.write_text(
        json.dumps(summaries, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    folds_path = output_dir / "walk_forward_folds.csv"
    pd.DataFrame(folds).to_csv(folds_path, index=False)
    outputs = {
        "candidate_ranking": _file_binding(ranking_path),
        "candidate_summaries": _file_binding(summaries_path),
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
            result_path = output_dir / f"selected_daily_{cost}bps.csv"
            results_by_candidate[selected][cost].to_csv(result_path, index_label="date")
            outputs[f"selected_daily_{cost}bps"] = _file_binding(result_path)

    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V31_RECOVERY_SPEED_STOCK_MOMENTUM_RESULT",
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "development_status": "PASS" if selected is not None else "BLOCKED",
        "selected_candidate": selected,
        "selected_specification": selected_spec,
        "selected_summary": selected_summary,
        "best_ranked_candidate": ranking[0]["candidate"],
        "best_ranked_wins_vs_nasdaq_50bps": ranking[0]["wins_vs_nasdaq_50bps"],
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
    develop_parser.add_argument("--output-dir", type=Path, default=DEVELOPMENT_OUTPUT_DIR)
    args = parser.parse_args()
    report = (
        freeze_protocol(args.protocol)
        if args.command == "freeze"
        else develop(args.protocol, args.output_dir)
    )
    fields = (
        ("status", "candidate_count", "protocol")
        if args.command == "freeze"
        else (
            "development_status",
            "selected_candidate",
            "best_ranked_candidate",
            "best_ranked_wins_vs_nasdaq_50bps",
            "walk_forward_pass_count",
            "release_status",
            "manifest",
        )
    )
    print(json.dumps({field: report[field] for field in fields}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
