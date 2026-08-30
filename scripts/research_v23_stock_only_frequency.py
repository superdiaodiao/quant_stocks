#!/usr/bin/env python3
"""Select a true stock-only CAN SLIM frequency without holding index ETFs.

``freeze`` records the monthly/weekly candidate grid and QQQ-relative gates
before any candidate return is calculated. ``develop`` then uses only
2019-2025 data. Risk-off capital remains cash; every positive target must be an
investable common equity. 2026 is reserved for a separate one-shot observer.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

from scripts import research_v15_benchmark_core_development as v15
from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
)
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.research.can_slim import (
    CanSlimConfig,
    calculate_can_slim_scheduled_returns,
    replay_can_slim_target_schedule,
)
from src.research.panel_data import load_panel
from src.research.universe_history import (
    load_universe_snapshots,
    snapshot_directory,
    universe_as_of,
)


DEVELOPMENT_START = "2019-01-01"
DEVELOPMENT_END = "2025-12-31"
DEVELOPMENT_YEARS = tuple(range(2019, 2026))
OBSERVATION_START = "2026-01-01"
COSTS = (10, 30, 50)
FREQUENCIES = ("monthly", "weekly")
TOP_NS = (3, 5, 10)
LIQUIDITIES = (2_000_000.0, 10_000_000.0)
MAXIMUM_SNAPSHOT_AGE_DAYS = 40
MAXIMUM_FINANCIAL_AGE_DAYS = 550
MAXIMUM_DRAWDOWN_LAG = 0.10
MINIMUM_WIN_RATE = 0.70
FORBIDDEN_ETFS = frozenset({
    "ONEQ", "PSQ", "QLD", "QQQ", "QQQM", "SQQQ", "SPY", "TQQQ",
})
OUTPUT_DIR = Path(
    "output/research_only/v23/stock_only_frequency_20260830"
)
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


def _candidate_key(frequency: str, top_n: int, liquidity: float) -> str:
    return f"{frequency}_top{top_n}_liq{int(liquidity / 1_000_000)}m"


def candidate_specs() -> list[dict]:
    return [
        {
            "key": _candidate_key(frequency, top_n, liquidity),
            "signal_frequency": frequency,
            "top_n": top_n,
            "minimum_median_dollar_volume": liquidity,
        }
        for frequency in FREQUENCIES
        for top_n in TOP_NS
        for liquidity in LIQUIDITIES
    ]


def _candidate_config(spec: dict, cost_bps: float = 10.0) -> CanSlimConfig:
    return CanSlimConfig(
        start=DEVELOPMENT_START,
        end=DEVELOPMENT_END,
        top_n=int(spec["top_n"]),
        maximum_position_weight=1.0 / int(spec["top_n"]),
        minimum_median_dollar_volume=float(
            spec["minimum_median_dollar_volume"]
        ),
        maximum_financial_age_days=MAXIMUM_FINANCIAL_AGE_DAYS,
        signal_frequency=str(spec["signal_frequency"]),
        use_quarterly_fundamentals=True,
        transaction_cost_bps=float(cost_bps),
    )


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict:
    path = Path(path)
    if path.exists():
        raise RuntimeError(f"v23 protocol will not be overwritten: {path}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V23_STOCK_ONLY_FREQUENCY_PRECOMMITMENT",
        "status": "FROZEN_NOT_DEVELOPED",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "objective": (
            "Select individual common equities only; use cash when risk-off; "
            "never hold QQQ, ONEQ, SPY, or another index ETF."
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
            "market_ma_days": 200,
            "maximum_financial_age_days": MAXIMUM_FINANCIAL_AGE_DAYS,
            "use_quarterly_fundamentals": True,
            "risk_off_asset": "CASH",
            "forbidden_etfs": sorted(FORBIDDEN_ETFS),
            "signal_execution": "completed period close to next trading close",
        },
        "cost_bps": list(COSTS),
        "primary_benchmark": "QQQ_TOTAL_RETURN",
        "secondary_benchmark": "NASDAQ_COMPOSITE",
        "eligibility_gates": {
            "positive_compounded_excess_vs_qqq_at_30_and_50bps": True,
            "minimum_annual_win_rate_vs_qqq_at_50bps": MINIMUM_WIN_RATE,
            "maximum_drawdown_lag_vs_qqq_percentage_points": (
                MAXIMUM_DRAWDOWN_LAG * 100.0
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
            "can_slim": _file_binding(Path("src/research/can_slim.py")),
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
            "eps": _file_binding(Path(POINT_IN_TIME_EPS_FILE)),
            "quarterly_fundamentals": _file_binding(
                Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE)
            ),
        },
        "parameters_frozen_before_development": True,
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
    return {
        **protocol,
        "protocol": {"path": str(path), "sha256": _sha256(path)},
    }


def _validated_protocol(path: Path) -> tuple[dict, str]:
    path = Path(path)
    protocol_sha = _sha256(path)
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if protocol["status"] != "FROZEN_NOT_DEVELOPED":
        raise RuntimeError("v23 protocol status changed")
    if protocol["candidate_grid"] != candidate_specs():
        raise RuntimeError("v23 candidate grid changed")
    if protocol["release_status"] != "BLOCKED" or protocol["promotion_eligible"]:
        raise RuntimeError("v23 release boundary changed")
    for name, binding in protocol["input_bindings"].items():
        if "content_manifest_sha256" in binding:
            actual = _directory_binding(Path(binding["path"]), binding["pattern"])
            if actual != binding:
                raise RuntimeError(f"v23 directory binding changed for {name}")
        elif _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"v23 file binding changed for {name}")
    return protocol, protocol_sha


def _load_inputs() -> dict:
    load_start = (
        pd.Timestamp(DEVELOPMENT_START) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")
    close, dollar_volume = load_panel(
        CLEANED_PRICE_DATA_DIR, load_start, DEVELOPMENT_END
    )
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"].sort_index().loc[:DEVELOPMENT_END]
    qqq = pd.read_csv(
        v15.QQQ_HISTORY["path"], index_col="date", parse_dates=True
    ).sort_index().loc[:DEVELOPMENT_END]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    quarterly = load_quarterly_fundamentals(
        POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
    )
    snapshots = load_universe_snapshots()

    def universe(signal_date):
        symbols = universe_as_of(
            snapshots,
            signal_date,
            maximum_age_days=MAXIMUM_SNAPSHOT_AGE_DAYS,
        )
        return None if symbols is None else set(symbols) - FORBIDDEN_ETFS

    return {
        "close": close,
        "dollar_volume": dollar_volume,
        "nasdaq": nasdaq,
        "qqq": qqq,
        "eps": eps,
        "quarterly": quarterly,
        "universe": universe,
    }


def _canonicalize_result(
    result: pd.DataFrame,
    nasdaq: pd.Series,
    qqq: pd.DataFrame,
) -> pd.DataFrame:
    dates = nasdaq.loc[DEVELOPMENT_START:DEVELOPMENT_END].dropna().index
    missing = dates.difference(result.index)
    if len(missing):
        raise RuntimeError(f"strategy is missing Nasdaq sessions: {list(missing[:5])}")
    result = result.reindex(dates).copy()
    qqq_dates = qqq.loc[:DEVELOPMENT_END].dropna(subset=["close"]).index
    qqq_index = v15.qqq_total_return_index(
        qqq,
        qqq_dates,
        allowed_market_closed=pd.Series(False, index=qqq_dates),
    )
    qqq_returns = qqq_index.pct_change(fill_method=None)
    missing_qqq = dates.difference(qqq_returns.dropna().index)
    if len(missing_qqq):
        raise RuntimeError(
            f"QQQ is missing Nasdaq sessions: {list(missing_qqq[:5])}"
        )
    result["qqq"] = qqq_returns.reindex(dates)
    return result


def _generate_candidate(
    spec: dict,
    inputs: dict,
) -> tuple[dict[int, pd.DataFrame], pd.DataFrame]:
    config = _candidate_config(spec)
    _, targets = calculate_can_slim_scheduled_returns(
        inputs["close"],
        inputs["dollar_volume"],
        inputs["nasdaq"],
        inputs["eps"],
        DEVELOPMENT_START,
        DEVELOPMENT_END,
        config_as_of=lambda _date: config,
        universe_as_of=inputs["universe"],
        signal_frequency=config.signal_frequency,
        quarterly_fundamentals=inputs["quarterly"],
        return_targets=True,
    )
    tickers = set(targets["ticker"].astype(str)) - {"__CASH__"}
    forbidden = sorted(tickers & FORBIDDEN_ETFS)
    if forbidden:
        raise RuntimeError(f"candidate selected forbidden ETFs: {forbidden}")
    results = {}
    for cost in COSTS:
        stressed = targets.copy()
        stressed["base_transaction_cost_bps"] = float(cost)
        daily, _ = replay_can_slim_target_schedule(
            inputs["close"],
            inputs["nasdaq"],
            stressed,
            DEVELOPMENT_START,
            DEVELOPMENT_END,
        )
        results[cost] = _canonicalize_result(
            daily, inputs["nasdaq"], inputs["qqq"]
        )
    return results, targets


def _maximum_drawdown(series: pd.Series) -> float:
    nav = (1.0 + series.astype(float)).cumprod()
    return float(nav.div(nav.cummax()).sub(1.0).min())


def _period_key(years: tuple[int, ...]) -> str:
    if not years:
        raise ValueError("selection years must not be empty")
    expected = tuple(range(years[0], years[-1] + 1))
    if years != expected or years[0] != DEVELOPMENT_YEARS[0]:
        raise ValueError("selection years must be a contiguous prefix from 2019")
    return f"{years[0]}-{years[-1]}"


def _period_metrics(result: pd.DataFrame, years: tuple[int, ...]) -> dict:
    selected = result.loc[result.index.year.isin(years)]
    observed_years = tuple(sorted(set(selected.index.year.astype(int))))
    if observed_years != years:
        raise RuntimeError(
            f"result years {observed_years} do not match requested years {years}"
        )
    strategy = float((1.0 + selected["strategy"]).prod() - 1.0)
    qqq = float((1.0 + selected["qqq"]).prod() - 1.0)
    nasdaq = float((1.0 + selected["benchmark"]).prod() - 1.0)
    strategy_drawdown = _maximum_drawdown(selected["strategy"])
    qqq_drawdown = _maximum_drawdown(selected["qqq"])
    return {
        "compounded_strategy": strategy,
        "compounded_qqq": qqq,
        "compounded_nasdaq": nasdaq,
        "compounded_excess_vs_qqq": strategy - qqq,
        "compounded_excess_vs_nasdaq": strategy - nasdaq,
        "strategy_maximum_drawdown": strategy_drawdown,
        "qqq_maximum_drawdown": qqq_drawdown,
        "drawdown_lag_vs_qqq": max(0.0, qqq_drawdown - strategy_drawdown),
        "turnover": float(selected["turnover"].sum()),
    }


def _summary(results: dict[int, pd.DataFrame]) -> dict:
    costs = {}
    for cost, result in results.items():
        annual = (
            (1.0 + result[["strategy", "benchmark", "qqq"]])
            .groupby(result.index.year)
            .prod()
            - 1.0
        )
        annual["excess_vs_qqq"] = annual["strategy"] - annual["qqq"]
        annual["excess_vs_nasdaq"] = annual["strategy"] - annual["benchmark"]
        periods = {
            _period_key(DEVELOPMENT_YEARS[:end]): _period_metrics(
                result, DEVELOPMENT_YEARS[:end]
            )
            for end in range(1, len(DEVELOPMENT_YEARS) + 1)
        }
        full_period = periods[_period_key(DEVELOPMENT_YEARS)]
        costs[str(cost)] = {
            "annual": [
                {"year": int(year), **values}
                for year, values in annual.to_dict(orient="index").items()
            ],
            "annual_wins_vs_qqq": int(annual["excess_vs_qqq"].gt(0.0).sum()),
            "worst_annual_excess_vs_qqq": float(
                annual["excess_vs_qqq"].min()
            ),
            **full_period,
            "periods": periods,
        }
    return {"costs": costs}


def _selection_row(key: str, summary: dict, years: tuple[int, ...]) -> dict:
    period_key = _period_key(years)
    required_wins = math.ceil(MINIMUM_WIN_RATE * len(years))
    selected_costs = {}
    for cost in COSTS:
        rows = [
            row for row in summary["costs"][str(cost)]["annual"]
            if int(row["year"]) in years
        ]
        observed_years = tuple(sorted(int(row["year"]) for row in rows))
        if observed_years != years:
            raise RuntimeError(
                f"{key} cost {cost} years {observed_years} != {years}"
            )
        period = summary["costs"][str(cost)]["periods"][period_key]
        selected_costs[cost] = {
            "wins": sum(float(row["excess_vs_qqq"]) > 0.0 for row in rows),
            "worst": min(float(row["excess_vs_qqq"]) for row in rows),
            "compounded_excess": period["compounded_excess_vs_qqq"],
            "drawdown_lag": period["drawdown_lag_vs_qqq"],
            "turnover": period["turnover"],
        }
    eligible = bool(
        selected_costs[30]["compounded_excess"] > 0.0
        and selected_costs[50]["compounded_excess"] > 0.0
        and selected_costs[50]["wins"] >= required_wins
        and selected_costs[50]["drawdown_lag"] <= MAXIMUM_DRAWDOWN_LAG
    )
    return {
        "candidate": key,
        "eligible": eligible,
        "required_wins": required_wins,
        "wins_vs_qqq_50bps": selected_costs[50]["wins"],
        "worst_annual_excess_vs_qqq_50bps": selected_costs[50]["worst"],
        "compounded_excess_vs_qqq_30bps": selected_costs[30][
            "compounded_excess"
        ],
        "compounded_excess_vs_qqq_50bps": selected_costs[50][
            "compounded_excess"
        ],
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
        -row["wins_vs_qqq_50bps"],
        -row["worst_annual_excess_vs_qqq_50bps"],
        -row["compounded_excess_vs_qqq_50bps"],
        row["turnover_50bps"],
        row["candidate"],
    ))
    for index, row in enumerate(ranking, start=1):
        row["rank"] = index
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
        raise RuntimeError(f"v23 development output will not be overwritten: {output_dir}")
    inputs = _load_inputs()
    results_by_candidate = {}
    targets_by_candidate = {}
    summaries = {}
    for spec in protocol["candidate_grid"]:
        key = spec["key"]
        results, targets = _generate_candidate(spec, inputs)
        results_by_candidate[key] = results
        targets_by_candidate[key] = targets
        summaries[key] = _summary(results)

    selected, ranking = select_candidate(summaries)
    folds = []
    for fold in protocol["walk_forward_folds"]:
        years = tuple(int(year) for year in fold["selection_years"])
        fold_selected, _ = select_candidate(summaries, years)
        test_year = int(fold["test_year"])
        if fold_selected is None:
            folds.append({
                **fold,
                "selected_candidate": None,
                "test_status": "NOT_RUN_NO_ELIGIBLE_CANDIDATE",
            })
            continue
        test = next(
            row for row in summaries[fold_selected]["costs"]["50"]["annual"]
            if int(row["year"]) == test_year
        )
        folds.append({
            **fold,
            "selected_candidate": fold_selected,
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
        "stage": "V23_STOCK_ONLY_FREQUENCY_DEVELOPMENT_RESULT",
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "development_status": "PASS" if selected is not None else "BLOCKED",
        "selected_candidate": selected,
        "selected_specification": selected_spec,
        "selected_configuration": (
            asdict(_candidate_config(selected_spec)) if selected_spec else None
        ),
        "selected_summary": selected_summary,
        "walk_forward_folds": folds,
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
    return {
        **report,
        "manifest": {"path": str(manifest_path), "sha256": _sha256(manifest_path)},
    }


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
    if args.command == "freeze":
        report = freeze_protocol(args.protocol)
        summary = {
            "status": report["status"],
            "candidate_count": report["candidate_count"],
            "reserved_observation_start": report["reserved_observation_start"],
            "protocol": report["protocol"],
        }
    else:
        report = develop(args.protocol, args.output_dir)
        summary = {
            "development_status": report["development_status"],
            "selected_candidate": report["selected_candidate"],
            "all_walk_forward_folds_passed": report[
                "all_walk_forward_folds_passed"
            ],
            "release_status": report["release_status"],
            "manifest": report["manifest"],
        }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
