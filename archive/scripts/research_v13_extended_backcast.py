#!/usr/bin/env python3
"""Extend v8 to 2019 with explicitly labelled pre-training fallback years."""

from __future__ import annotations

from dataclasses import asdict
import importlib.util
import json
from pathlib import Path

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE, POINT_IN_TIME_EPS_FILE
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.research.can_slim import calculate_can_slim_returns, calculate_can_slim_scheduled_returns
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


START = "2019-01-01"
END = "2026-07-17"
QQQ_PATH = Path("output/research_only/qqq_nasdaq_history.csv")
QUARTERLY_PATH = Path(
    "output/data_provenance/companyfacts_proven_only_manifest-"
    "6c8a87fcc71cfcd5-recipe-6f0998be-q1-fp-guard-bank-duration-v3/quarterly.csv"
)
PREFIX = Path("output/research_v13_extended_backcast")


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


V6 = _load("research_v6_v13", "scripts/research_v6_walkforward_defensive_ensemble.py")
V7 = _load("research_v7_v13", "scripts/research_v7_qqq_targeted_core_satellite.py")
V8 = _load("research_v8_v13", "scripts/research_v8_monthly_risk_budget_blend.py")
ROBUST = _load("research_v8_robust_v13", "scripts/research_v8_short_horizon_robustness.py")


def _snapshot(start, end, train_end, ids, configs, reason):
    return {
        "effective_start": start,
        "effective_end": end,
        "training_end": train_end,
        "config_ids": ids,
        "configs": rank_weighted_configs(configs, ids),
        "selection_reason": reason,
    }


def run() -> dict:
    close, dollar_volume = load_panel(CLEANED_PRICE_DATA_DIR, "2017-11-28", END)
    nasdaq = pd.read_csv(NASDAQ_INDEX_FILE, index_col="date", parse_dates=True)["close"]
    qqq = pd.read_csv(QQQ_PATH, index_col="date", parse_dates=True)
    qqq_return = V7.qqq_total_return(
        qqq, close.index, allowed_market_closed=nasdaq.reindex(close.index).isna()
    )
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    quarterly = load_quarterly_fundamentals(QUARTERLY_PATH)
    universe_snapshots = load_universe_snapshots()
    universe = lambda date: universe_as_of(universe_snapshots, date)

    v6_configs = candidate_configs(
        signal_frequency="monthly", use_quarterly_fundamentals=True,
        adaptive_channel=False, end=END,
        maximum_financial_age_days=(150, 365, 550),
    )
    v6_candidates = {
        config_id: calculate_can_slim_returns(
            close, dollar_volume, nasdaq, eps, config, universe, quarterly
        )
        for config_id, config in enumerate(v6_configs)
    }
    v6_groups = {
        config_id: f"top{config.top_n}-liq{int(config.minimum_median_dollar_volume)}"
        for config_id, config in enumerate(v6_configs)
    }
    v6_fallback = core_fallback_ids(v6_configs)
    v6_snapshots = []
    for effective_start, effective_end, train_end in annual_parameter_snapshot_periods(START, END):
        if effective_start.year < 2022:
            ids, reason = v6_fallback, "pre_training_fixed_core_fallback"
        else:
            ids, _ = select_stable_ensemble(
                v6_candidates, candidate_groups=v6_groups, train_end=train_end,
                no_evidence_fallback_ids=v6_fallback,
            )
            reason = "original_36_month_walk_forward"
        v6_snapshots.append(_snapshot(
            effective_start, effective_end, train_end, ids, v6_configs, reason
        ))
    base = calculate_can_slim_scheduled_returns(
        close, dollar_volume, nasdaq, eps, START, END,
        lambda date: configs_from_snapshots(v6_snapshots, date), universe,
        "monthly", quarterly,
    )
    v6_extended = V6.combine_sleeves({
        window: V6.simulate_sleeve(
            base, qqq["close"], qqq.get("cash_dividend", pd.Series(dtype=float)),
            relative_strength_window=window, transaction_cost_bps=50.0,
        )
        for window in V6.LOOKBACKS
    })

    v7_configs = candidate_configs(
        signal_frequency="monthly", use_quarterly_fundamentals=True,
        adaptive_channel=False, end=END,
        maximum_financial_age_days=(150, 365),
    )
    v7_candidates = {
        config_id: calculate_can_slim_returns(
            close, dollar_volume, nasdaq, eps, config, universe, quarterly
        )
        for config_id, config in enumerate(v7_configs)
    }
    v7_fallback = core_fallback_ids(v7_configs)
    v7_snapshots = []
    for effective_start, effective_end, train_end in annual_parameter_snapshot_periods(START, END):
        if effective_start.year < 2021:
            ids, reason = v7_fallback, "pre_two_year_fixed_core_fallback"
        else:
            ids, _ = V7.select_qqq_stable_ensemble(
                v7_candidates, qqq_return, train_end,
                training_years=4, ensemble_size=2,
            )
            reason = "original_completed_year_qqq_walk_forward"
        v7_snapshots.append(_snapshot(
            effective_start, effective_end, train_end, ids, v7_configs, reason
        ))
    v7_stock = calculate_can_slim_scheduled_returns(
        close, dollar_volume, nasdaq, eps, START, END,
        lambda date: configs_from_snapshots(v7_snapshots, date), universe,
        "monthly", quarterly,
    )
    v7_extended = V7.simulate_monthly_core_satellite(
        v7_stock, qqq_return.reindex(v7_stock.index), stock_weight=0.40,
        qqq_weight=0.60, transaction_cost_bps=50.0,
    )
    combined = V8.combine_monthly_sleeves(
        v6_extended, v7_extended, v7_weight=0.75, transfer_cost_bps=50.0
    )
    relative = ROBUST.relative_returns(combined["return"], combined["qqq_return"])
    summary = V7.summarize(combined)
    summary["13_week_positive_fraction"] = ROBUST.rolling_summary(relative, 65)["positive_fraction"]
    summary["26_week_positive_fraction"] = ROBUST.rolling_summary(relative, 130)["positive_fraction"]
    annual = (1.0 + combined[["return", "benchmark_return", "qqq_return"]]).groupby(combined.index.year).prod() - 1.0
    annual["excess_vs_qqq"] = annual["return"] - annual["qqq_return"]
    annual["excess_vs_nasdaq"] = annual["return"] - annual["benchmark_return"]
    combined.to_csv(PREFIX.with_name(PREFIX.name + "_50bps_daily.csv"), index_label="date")
    annual.to_csv(PREFIX.with_name(PREFIX.name + "_annual.csv"), index_label="year")
    payload = {
        "model_version": "can-slim-v13-extended-v8-backcast-research",
        "research_only": True,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "historical_selection_contaminated": True,
        "evidence_classes": {
            "2019-2020": "STRUCTURAL_BACKCAST_BOTH_COMPONENTS_USE_FIXED_FALLBACK",
            "2021": "PARTIAL_BACKCAST_V6_FIXED_FALLBACK_V7_WALK_FORWARD",
            "2022-2026": "ORIGINAL_WALK_FORWARD_DIAGNOSTIC",
        },
        "configuration": {
            "v8_weights_unchanged": True,
            "v7_stock_weight": 0.40,
            "v7_capital_weight": 0.75,
            "transaction_cost_bps": 50.0,
            "sleeve_transfer_cost_bps": 50.0,
        },
        "summary": summary,
        "annual": {
            str(year): {key: float(value) for key, value in row.items()}
            for year, row in annual.iterrows()
        },
        "v6_snapshots": [{
            **{key: value.strftime("%Y-%m-%d") if isinstance(value, pd.Timestamp) else value
               for key, value in snapshot.items() if key != "configs"},
            "configs": [asdict(config) for config in snapshot["configs"]],
        } for snapshot in v6_snapshots],
        "v7_snapshots": [{
            **{key: value.strftime("%Y-%m-%d") if isinstance(value, pd.Timestamp) else value
               for key, value in snapshot.items() if key != "configs"},
            "configs": [asdict(config) for config in snapshot["configs"]],
        } for snapshot in v7_snapshots],
        "interpretation_guardrail": (
            "Fallback years test portfolio structure only and are not equivalent "
            "to the original fully trained 2022-2026 walk-forward evidence."
        ),
    }
    PREFIX.with_name(PREFIX.name + "_summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
