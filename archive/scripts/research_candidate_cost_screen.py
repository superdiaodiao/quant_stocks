"""Cost-stress every predeclared research-v2 candidate on fixed data.

This is a retrospective diagnostic.  It can show whether the existing candidate
family contains a cost-robust fixed configuration, but it cannot turn a
historically selected configuration into out-of-sample or promotion evidence.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

import pandas as pd

from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
    PROJECT_PATH,
)
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.research.can_slim import calculate_can_slim_returns
from src.research.can_slim_walk_forward import candidate_configs
from src.research.data_fingerprint import (
    build_data_manifest,
    can_slim_input_fingerprints,
)
from src.research.panel_data import load_panel
from src.research.universe_history import load_universe_snapshots, universe_as_of


DEFAULT_OUTPUT = Path(PROJECT_PATH) / "output/research_candidate_cost_screen.csv"
DEFAULT_SUMMARY = (
    Path(PROJECT_PATH)
    / "output/data_provenance/research_candidate_cost_screen_2026-08-10.json"
)


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _annual(result: pd.DataFrame) -> pd.DataFrame:
    annual = (1 + result[["strategy", "benchmark"]]).groupby(
        result.index.year
    ).prod() - 1
    annual["excess_vs_nasdaq"] = annual["strategy"] - annual["benchmark"]
    return annual


def _atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        frame.to_csv(handle, index=False)
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def summarize_screen(
    rows: pd.DataFrame,
    *,
    test_years: tuple[int, ...] = (2022, 2023, 2024, 2025, 2026),
    minimum_wins: int = 4,
) -> list[dict]:
    expected = set(test_years)
    summaries: list[dict] = []
    for config_id, group in rows.groupby("config_id", sort=True):
        config_summary: dict = {"config_id": int(config_id), "costs": {}}
        all_costs_pass = True
        for cost_bps, cost_group in group.groupby("cost_bps", sort=True):
            years = set(cost_group["year"].astype(int))
            if years != expected:
                raise ValueError(
                    f"config {config_id} cost {cost_bps:g} has years "
                    f"{sorted(years)}, expected {sorted(expected)}"
                )
            excess = cost_group["excess_vs_nasdaq"].astype(float)
            wins = int(excess.gt(0).sum())
            passed = wins >= minimum_wins and float(excess.median()) > 0
            all_costs_pass = all_costs_pass and passed
            config_summary["costs"][str(int(cost_bps))] = {
                "wins": wins,
                "years": len(excess),
                "median_excess_vs_nasdaq": float(excess.median()),
                "minimum_excess_vs_nasdaq": float(excess.min()),
                "passed": passed,
            }
        config_summary["passed_all_costs"] = all_costs_pass
        summaries.append(config_summary)
    return summaries


def run(
    quarterly_path: Path,
    output_path: Path,
    summary_path: Path,
    cost_bps_values: tuple[float, ...] = (10.0, 30.0, 50.0),
) -> dict:
    configs = candidate_configs(
        use_quarterly_fundamentals=True,
        maximum_financial_age_days=(150, 365, 550),
    )
    close, dollar_volume = load_panel(
        CLEANED_PRICE_DATA_DIR, "2017-11-28", "2026-07-17"
    )
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    quarterly = load_quarterly_fundamentals(quarterly_path)
    snapshots = load_universe_snapshots()

    rows: list[dict] = []
    for config_id, base_config in enumerate(configs):
        for cost_bps in cost_bps_values:
            config = replace(base_config, transaction_cost_bps=float(cost_bps))
            result = calculate_can_slim_returns(
                close,
                dollar_volume,
                nasdaq,
                eps,
                config,
                lambda date: universe_as_of(snapshots, date),
                quarterly,
            )
            for year, annual_row in _annual(result).loc[2022:2026].iterrows():
                rows.append(
                    {
                        "config_id": config_id,
                        "cost_bps": float(cost_bps),
                        "year": int(year),
                        "strategy": float(annual_row.strategy),
                        "nasdaq": float(annual_row.benchmark),
                        "excess_vs_nasdaq": float(
                            annual_row.excess_vs_nasdaq
                        ),
                    }
                )
    frame = pd.DataFrame(rows)
    _atomic_csv(output_path, frame)

    summaries = summarize_screen(frame)
    fingerprints = can_slim_input_fingerprints()
    fingerprints["quarterly_fundamentals"] = {
        **fingerprints["quarterly_fundamentals"],
        "path": str(quarterly_path.resolve()),
        "sha256": _sha256(quarterly_path),
        "bytes": quarterly_path.stat().st_size,
    }
    robust_ids = [
        row["config_id"] for row in summaries if row["passed_all_costs"]
    ]
    payload = {
        "format_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "retrospective_fixed_candidate_cost_screen",
        "research_only": True,
        "diagnostic_only": True,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "selection_warning": (
            "Results use all test years and cannot select an out-of-sample "
            "winner; any chosen configuration requires a new forward record."
        ),
        "test_years": [2022, 2023, 2024, 2025, 2026],
        "cost_bps": [float(value) for value in cost_bps_values],
        "minimum_wins_per_cost": 4,
        "candidate_count": len(configs),
        "candidate_configs": [asdict(config) for config in configs],
        "candidate_summaries": summaries,
        "robust_candidate_ids": robust_ids,
        "robust_candidate_count": len(robust_ids),
        "quarterly_input": {
            "path": str(quarterly_path.resolve()),
            "sha256": _sha256(quarterly_path),
        },
        "data_manifest": build_data_manifest(fingerprints),
        "strategy_code_fingerprint": fingerprints["strategy_code"],
        "artifact": {
            "path": str(output_path.resolve()),
            "sha256": _sha256(output_path),
            "rows": len(frame),
        },
    }
    _atomic_json(summary_path, payload)
    return payload


def _parse_costs(value: str) -> tuple[float, ...]:
    costs = tuple(float(item.strip()) for item in value.split(",") if item.strip())
    if not costs or any(cost < 0 for cost in costs) or len(set(costs)) != len(costs):
        raise argparse.ArgumentTypeError("costs must be unique non-negative values")
    return costs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cost-stress every existing predeclared CAN SLIM candidate."
    )
    parser.add_argument(
        "--quarterly-input",
        type=Path,
        default=Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE),
    )
    parser.add_argument("--cost-bps", type=_parse_costs, default=(10.0, 30.0, 50.0))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    args = parser.parse_args()
    payload = run(args.quarterly_input, args.output, args.summary, args.cost_bps)
    print(
        json.dumps(
            {
                "candidate_count": payload["candidate_count"],
                "robust_candidate_ids": payload["robust_candidate_ids"],
                "release_status": payload["release_status"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
