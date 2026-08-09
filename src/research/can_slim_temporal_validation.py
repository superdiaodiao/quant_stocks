"""Time-cutoff challenger validation starting from the usable 2020 history."""

from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path

import pandas as pd

from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
)
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.research.can_slim import (
    calculate_can_slim_returns,
    calculate_can_slim_scheduled_returns,
)
from src.research.can_slim_validation import fixed_top3_config
from src.research.can_slim_walk_forward import (
    annual_parameter_snapshot_periods,
    candidate_configs,
    configs_from_snapshots,
    core_fallback_ids,
    rank_weighted_configs,
    select_stable_ensemble,
)
from src.research.panel_data import load_panel
from src.research.universe_history import load_universe_snapshots, universe_as_of


MODEL_VERSION = "can-slim-temporal-2020-v1"
TRAINING_START = "2020-01-01"
CANDIDATE_REPLAY_START = "2019-12-31"
CHALLENGER_START = "2023-01-01"
RESEARCH_END = "2026-07-17"
MAXIMUM_SNAPSHOT_AGE_DAYS = 40
PARTIAL_2019_SEGMENTS = (
    ("2019-01", "2019-01-31", "2019-02-28"),
    ("2019-06", "2019-06-28", "2019-07-31"),
    ("2019-11_12", "2019-11-29", "2019-12-31"),
)


def _annual(result: pd.DataFrame) -> pd.DataFrame:
    annual = (
        (1 + result[["strategy", "benchmark"]])
        .groupby(result.index.year)
        .prod()
        - 1
    )
    annual["excess_vs_nasdaq"] = annual["strategy"] - annual["benchmark"]
    return annual


def _comparison_rows(
    challenger: pd.DataFrame, fixed: pd.DataFrame, cost_bps: float
) -> list[dict]:
    challenger_annual = _annual(challenger)
    fixed_annual = _annual(fixed)
    rows = []
    for year in challenger_annual.index:
        challenger_row = challenger_annual.loc[year]
        fixed_row = fixed_annual.loc[year]
        rows.append({
            "cost_bps": cost_bps,
            "year": int(year),
            "period": "partial_through_2026-07-17" if year == 2026 else "full_year",
            "challenger": float(challenger_row["strategy"]),
            "fixed_top3": float(fixed_row["strategy"]),
            "nasdaq": float(challenger_row["benchmark"]),
            "challenger_excess": float(challenger_row["excess_vs_nasdaq"]),
            "fixed_top3_excess": float(fixed_row["excess_vs_nasdaq"]),
            "challenger_minus_fixed": float(
                challenger_row["strategy"] - fixed_row["strategy"]
            ),
        })
    return rows


def _partial_2019_diagnostics(
    close: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    nasdaq: pd.Series,
    eps: pd.DataFrame,
    quarterly: pd.DataFrame,
    configs: list,
    universe,
) -> pd.DataFrame:
    rows = []
    for config_id, config in enumerate(configs):
        for segment, start, end in PARTIAL_2019_SEGMENTS:
            result = calculate_can_slim_returns(
                close,
                dollar_volume,
                nasdaq,
                eps,
                replace(config, start=start, end=end),
                universe,
                quarterly,
            )
            strategy = float((1 + result["strategy"]).prod() - 1)
            benchmark = float((1 + result["benchmark"]).prod() - 1)
            rows.append({
                "config_id": config_id,
                "segment": segment,
                "start": start,
                "end": end,
                "strategy": strategy,
                "nasdaq": benchmark,
                "excess_vs_nasdaq": strategy - benchmark,
            })
    return pd.DataFrame(rows)


def run_temporal_validation() -> tuple[
    pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, dict
]:
    """Run the predeclared 2020-origin challenger without changing live policy."""
    configs = [
        replace(config, start=CANDIDATE_REPLAY_START)
        for config in candidate_configs(
            signal_frequency="monthly",
            use_quarterly_fundamentals=True,
            adaptive_channel=False,
            end=RESEARCH_END,
        )
    ]
    groups = {
        config_id: (
            f"top{config.top_n}-liq"
            f"{int(config.minimum_median_dollar_volume)}"
        )
        for config_id, config in enumerate(configs)
    }
    close, dollar_volume = load_panel(
        CLEANED_PRICE_DATA_DIR, "2017-11-28", RESEARCH_END
    )
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    quarterly = load_quarterly_fundamentals(
        POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
    )
    snapshots = load_universe_snapshots()
    universe = lambda date: universe_as_of(
        snapshots, date, maximum_age_days=MAXIMUM_SNAPSHOT_AGE_DAYS
    )

    candidate_results = {
        config_id: calculate_can_slim_returns(
            close,
            dollar_volume,
            nasdaq,
            eps,
            config,
            universe,
            quarterly,
        )
        for config_id, config in enumerate(configs)
    }

    model_snapshots = []
    ranking_rows = []
    for effective_start, effective_end, train_end in annual_parameter_snapshot_periods(
        CHALLENGER_START, RESEARCH_END
    ):
        selected_ids, ranking = select_stable_ensemble(
            candidate_results,
            candidate_groups=groups,
            train_end=train_end,
            expanding_start=TRAINING_START,
            no_evidence_fallback_ids=core_fallback_ids(configs),
        )
        ranking.insert(0, "effective_start", effective_start)
        ranking.insert(1, "training_end", train_end)
        ranking_rows.append(ranking)
        model_snapshots.append({
            "effective_start": effective_start,
            "effective_end": effective_end,
            "training_end": train_end,
            "config_ids": selected_ids,
            "configs": rank_weighted_configs(configs, selected_ids),
        })

    comparison_rows = []
    challenger_base = None
    fixed_base = None
    for cost_bps in (10.0, 30.0, 50.0):
        challenger = calculate_can_slim_scheduled_returns(
            close,
            dollar_volume,
            nasdaq,
            eps,
            CHALLENGER_START,
            RESEARCH_END,
            lambda date, bps=cost_bps: [
                replace(config, transaction_cost_bps=bps)
                for config in (configs_from_snapshots(model_snapshots, date) or [])
            ] or None,
            universe,
            "monthly",
            quarterly,
        )
        fixed_config = replace(
            fixed_top3_config(cost_bps), start=CHALLENGER_START, end=RESEARCH_END
        )
        fixed = calculate_can_slim_scheduled_returns(
            close,
            dollar_volume,
            nasdaq,
            eps,
            CHALLENGER_START,
            RESEARCH_END,
            lambda _date, config=fixed_config: [config],
            universe,
            "monthly",
            quarterly,
        )
        comparison_rows.extend(_comparison_rows(challenger, fixed, cost_bps))
        if cost_bps == 10.0:
            challenger_base = challenger
            fixed_base = fixed

    comparison = pd.DataFrame(comparison_rows)
    partial_2019 = _partial_2019_diagnostics(
        close, dollar_volume, nasdaq, eps, quarterly, configs, universe
    )
    rankings = pd.concat(ranking_rows, ignore_index=True)
    snapshots_frame = pd.DataFrame([
        {
            "effective_start": snapshot["effective_start"],
            "effective_end": snapshot["effective_end"],
            "training_end": snapshot["training_end"],
            "config_ids": json.dumps(snapshot["config_ids"]),
        }
        for snapshot in model_snapshots
    ])
    assert challenger_base is not None and fixed_base is not None
    base = comparison.loc[comparison["cost_bps"].eq(10.0)]
    summary = {
        "model_version": MODEL_VERSION,
        "status": "RESEARCH_CHALLENGER_ONLY",
        "training_start": TRAINING_START,
        "first_simulated_freeze": CHALLENGER_START,
        "research_end": RESEARCH_END,
        "maximum_snapshot_age_days": MAXIMUM_SNAPSHOT_AGE_DAYS,
        "candidate_count": len(configs),
        "parameter_update_frequency": "annual",
        "comparison_period": f"{CHALLENGER_START} through {RESEARCH_END}",
        "challenger_wins_vs_nasdaq": int(base["challenger_excess"].gt(0).sum()),
        "fixed_top3_wins_vs_nasdaq": int(base["fixed_top3_excess"].gt(0).sum()),
        "challenger_wins_vs_fixed_top3": int(
            base["challenger_minus_fixed"].gt(0).sum()
        ),
        "comparison_periods": int(len(base)),
        "challenger_compounded_return": float(
            (1 + challenger_base["strategy"]).prod() - 1
        ),
        "fixed_top3_compounded_return": float(
            (1 + fixed_base["strategy"]).prod() - 1
        ),
        "nasdaq_compounded_return": float(
            (1 + challenger_base["benchmark"]).prod() - 1
        ),
        "partial_2019_role": (
            "diagnostic_only; independent capital reset per usable segment; "
            "excluded from continuous training and parameter ranking"
        ),
        "model_snapshots": [
            {
                "effective_start": snapshot["effective_start"].strftime("%Y-%m-%d"),
                "effective_end": snapshot["effective_end"].strftime("%Y-%m-%d"),
                "training_end": snapshot["training_end"].strftime("%Y-%m-%d"),
                "config_ids": snapshot["config_ids"],
                "configs": [asdict(config) for config in snapshot["configs"]],
            }
            for snapshot in model_snapshots
        ],
    }
    return comparison, rankings, snapshots_frame, partial_2019, summary


def main() -> None:
    comparison, rankings, snapshots, partial_2019, summary = (
        run_temporal_validation()
    )
    output = Path("output")
    comparison.to_csv(output / "can_slim_temporal_2020_comparison.csv", index=False)
    rankings.to_csv(output / "can_slim_temporal_2020_rankings.csv", index=False)
    snapshots.to_csv(output / "can_slim_temporal_2020_snapshots.csv", index=False)
    partial_2019.to_csv(
        output / "can_slim_temporal_2019_diagnostics.csv", index=False
    )
    (output / "can_slim_temporal_2020_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(comparison.to_string(index=False))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
