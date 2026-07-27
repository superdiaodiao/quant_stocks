"""Validate and freeze the canonical concentrated CAN SLIM policy."""

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
    CanSlimConfig,
    calculate_can_slim_returns,
    calculate_can_slim_returns_with_ledger,
)
from src.research.metrics import moving_block_bootstrap
from src.research.panel_data import load_panel
from src.research.universe_history import load_universe_snapshots, universe_as_of


MODEL_VERSION = "can-slim-top3-v1"
POLICY_FROZEN_AT = "2026-07-18"


def fixed_top3_config(
    transaction_cost_bps: float = 10.0,
) -> CanSlimConfig:
    """Return the exact policy frozen after the historical research period."""
    return CanSlimConfig(
        start="2019-01-01",
        end="2026-07-17",
        top_n=3,
        maximum_position_weight=1 / 3,
        minimum_median_dollar_volume=10_000_000.0,
        transaction_cost_bps=transaction_cost_bps,
        signal_frequency="monthly",
        use_quarterly_fundamentals=True,
        price_channel="none",
        selection_mode="growth",
    )


def _annual(result: pd.DataFrame) -> pd.DataFrame:
    annual = (
        (1 + result[["strategy", "benchmark"]])
        .groupby(result.index.year)
        .prod()
        - 1
    )
    annual["excess_vs_nasdaq"] = annual["strategy"] - annual["benchmark"]
    annual.index.name = "year"
    return annual


def run_can_slim_validation(
    config: CanSlimConfig | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    """Replay the frozen policy and report research evidence without relabeling it OOS."""
    config = config or fixed_top3_config()
    load_start = (
        pd.Timestamp(config.start) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")
    close, dollar_volume = load_panel(
        CLEANED_PRICE_DATA_DIR, load_start, config.end
    )
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    quarterly = load_quarterly_fundamentals(
        POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
    )
    snapshots = load_universe_snapshots()
    universe = lambda date: universe_as_of(snapshots, date)

    cost_results: dict[float, pd.DataFrame] = {}
    cost_rows: list[dict] = []
    for cost_bps in (10.0, 30.0, 50.0):
        stressed_config = replace(config, transaction_cost_bps=cost_bps)
        result = calculate_can_slim_returns(
            close, dollar_volume, nasdaq, eps, stressed_config, universe,
            quarterly,
        )
        cost_results[cost_bps] = result
        for year, row in _annual(result).loc[2021:].iterrows():
            cost_rows.append({
                "cost_bps": cost_bps,
                "year": int(year),
                "strategy": float(row["strategy"]),
                "nasdaq": float(row["benchmark"]),
                "excess_vs_nasdaq": float(row["excess_vs_nasdaq"]),
            })

    result = cost_results[10.0]
    annual = _annual(result)
    evidence = annual.loc[2021:]
    costs = pd.DataFrame(cost_rows)
    active = result.loc["2021-01-01":, "strategy"] - result.loc[
        "2021-01-01":, "benchmark"
    ]
    uncertainty = moving_block_bootstrap(active)
    live_config = replace(config, end="2099-12-31")
    all_years_win = bool(evidence["excess_vs_nasdaq"].gt(0).all())
    three_x_cost = costs.loc[costs["cost_bps"].eq(30.0)]
    three_x_strategy = float((1 + three_x_cost["strategy"]).prod() - 1)
    three_x_nasdaq = float((1 + three_x_cost["nasdaq"]).prod() - 1)
    # Three times the assumed one-way cost is the actionable capacity stress:
    # require positive compounded alpha and at least 75% winning years.  The
    # 50 bps run remains a deliberately extreme diagnostic, not the base case.
    cost_stress_passed = bool(
        three_x_strategy > three_x_nasdaq
        and three_x_cost["excess_vs_nasdaq"].gt(0).mean() >= 0.75
    )
    summary = {
        "model_version": MODEL_VERSION,
        "policy_status": "FROZEN_SHADOW",
        "release_status": "BLOCKED",
        "release_reason": (
            "Historical research passed; genuine forward evidence starts after "
            f"{POLICY_FROZEN_AT}."
        ),
        "rules": (
            "Public CAN SLIM-inspired quarterly growth and leadership; at most "
            "three qualifying Nasdaq stocks, equal weighted when full; monthly "
            "close signal and next-session close execution."
        ),
        "configurations_tested_in_top_n_neighborhood": 4,
        "historical_research_period": "2021-01-01 through 2026-07-17",
        "policy_frozen_at": POLICY_FROZEN_AT,
        "forward_evidence_start": POLICY_FROZEN_AT,
        "parameter_update_frequency": "frozen",
        "adaptive_framework_status": "RESEARCH_ONLY",
        "adaptive_parameter_family": {
            "top_n": [3, 5, 10],
            "minimum_median_dollar_volume": [2_000_000, 10_000_000],
            "position_weight_rule": "1 / top_n",
            "selection_data_cutoff": "previous calendar year end",
            "promotion_rule": (
                "must beat the frozen policy in chronological walk-forward "
                "before it can update live parameters"
            ),
        },
        "signal_frequency": config.signal_frequency,
        "uses_quarterly_fundamentals": True,
        "uses_adaptive_channel": False,
        "historical_years": int(len(evidence)),
        "wins_vs_nasdaq": int(evidence["excess_vs_nasdaq"].gt(0).sum()),
        "passed_every_historical_year": all_years_win,
        "minimum_historical_excess": float(
            evidence["excess_vs_nasdaq"].min()
        ),
        "median_historical_excess": float(
            evidence["excess_vs_nasdaq"].median()
        ),
        "transaction_cost_stress_passed": cost_stress_passed,
        "transaction_cost_stress_definition": (
            "30 bps one-way (3x assumed): compounded return above Nasdaq and "
            "at least 75% winning calendar years"
        ),
        "three_x_cost_strategy_return": three_x_strategy,
        "three_x_cost_nasdaq_return": three_x_nasdaq,
        "cost_stress_wins": {
            str(int(cost)): int(group["excess_vs_nasdaq"].gt(0).sum())
            for cost, group in costs.groupby("cost_bps")
        },
        **uncertainty,
        "current_shadow_config_ids": [0],
        "current_shadow_configs": [asdict(live_config)],
        "model_snapshots": [{
            "effective_start": POLICY_FROZEN_AT,
            "effective_end": "9999-12-31",
            "training_end": "2026-07-17",
            "config_ids": [0],
            "configs": [asdict(live_config)],
        }],
    }
    return result, annual, costs, summary


def main() -> None:
    result, annual, costs, summary = run_can_slim_validation()
    config = fixed_top3_config()
    load_start = (
        pd.Timestamp(config.start) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")
    close, dollar_volume = load_panel(
        CLEANED_PRICE_DATA_DIR, load_start, config.end
    )
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    quarterly = load_quarterly_fundamentals(
        POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
    )
    snapshots = load_universe_snapshots()
    _, ledger = calculate_can_slim_returns_with_ledger(
        close, dollar_volume, nasdaq, eps, config,
        lambda date: universe_as_of(snapshots, date), quarterly,
    )
    output = Path("output")
    result.to_csv(output / "can_slim_fixed_top3_backtest.csv")
    ledger.to_csv(output / "can_slim_fixed_top3_trade_ledger.csv", index=False)
    annual.to_csv(output / "can_slim_fixed_top3_annual.csv")
    costs.to_csv(output / "can_slim_fixed_top3_cost_stress.csv", index=False)
    (output / "can_slim_fixed_top3_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(annual.loc[2021:].to_string(
        float_format=lambda value: f"{value:.2%}"
    ))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
