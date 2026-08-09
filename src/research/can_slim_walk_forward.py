"""Expanding walk-forward parameter selection for the canonical CAN SLIM selector."""

from __future__ import annotations

from dataclasses import asdict, replace
import argparse
import json
from pathlib import Path

import pandas as pd

from src.conf import (
    CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE, POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
)
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.research.can_slim import (
    CanSlimConfig,
    calculate_can_slim_returns,
    calculate_can_slim_scheduled_returns,
)
from src.research.panel_data import load_ohlc_panel, load_panel
from src.research.universe_history import load_universe_snapshots, universe_as_of


def candidate_configs(
    signal_frequency: str = "monthly",
    use_quarterly_fundamentals: bool = False,
    adaptive_channel: bool = False,
    end: str | pd.Timestamp = "2026-07-17",
    maximum_financial_age_days: tuple[int, ...] = (550,),
) -> list[CanSlimConfig]:
    """Small, predeclared grid; broad enough to test robustness without curve fitting."""
    if not maximum_financial_age_days or any(
        int(days) <= 0 for days in maximum_financial_age_days
    ):
        raise ValueError("maximum financial ages must be positive")
    end = pd.Timestamp(end).normalize().strftime("%Y-%m-%d")
    base = CanSlimConfig(
        start="2019-01-01", end=end, signal_frequency=signal_frequency,
        use_quarterly_fundamentals=use_quarterly_fundamentals,
    )
    variants = (
        (("none", "growth"), ("keltner", "recovery"))
        if adaptive_channel else (("none", "growth"),)
    )
    # Keep three economically distinct breadth choices without changing the
    # grid size: concentrated, balanced, and diversified.  Tying the position
    # cap to breadth avoids silently comparing a fully invested Top 5 model
    # with a Top 3 model that is forced to hold 40% cash.
    return [
        replace(
            base,
            top_n=top_n,
            maximum_position_weight=1 / top_n,
            minimum_median_dollar_volume=liquidity,
            maximum_financial_age_days=int(financial_age_days),
            price_channel=channel,
            selection_mode=selection_mode,
        )
        for top_n in (3, 5, 10)
        for liquidity in (2_000_000.0, 10_000_000.0)
        for financial_age_days in maximum_financial_age_days
        for channel, selection_mode in variants
    ]


def core_fallback_ids(configs: list[CanSlimConfig]) -> list[int]:
    """Return the predeclared liquid, concentrated CAN SLIM growth core."""
    fallback_age = max(config.maximum_financial_age_days for config in configs)
    return [
        config_id
        for config_id, config in enumerate(configs)
        if (
            config.selection_mode == "growth"
            and config.top_n == 5
            and config.minimum_median_dollar_volume == 10_000_000.0
            and config.maximum_financial_age_days == fallback_age
        )
    ]


def _annual(result: pd.DataFrame) -> pd.DataFrame:
    annual = (1 + result[["strategy", "benchmark"]]).groupby(result.index.year).prod() - 1
    annual["excess_vs_nasdaq"] = annual["strategy"] - annual["benchmark"]
    return annual


def _training_score(annual: pd.DataFrame, years: list[int]) -> tuple[float, float]:
    excess = annual.reindex(years)["excess_vs_nasdaq"].dropna()
    if len(excess) != len(years):
        return (-float("inf"), -float("inf"))
    # Prefer a configuration whose weakest year held up, then its typical year.
    return float(excess.min()), float(excess.median())


def _monthly(result: pd.DataFrame) -> pd.DataFrame:
    monthly = (1 + result[["strategy", "benchmark"]]).resample("ME").prod() - 1
    monthly["excess_vs_nasdaq"] = monthly["strategy"] - monthly["benchmark"]
    return monthly


def rank_weighted_configs(
    configs: list[CanSlimConfig], selected_ids: list[int]
) -> list[CanSlimConfig]:
    """Freeze deterministic linear ensemble weights from training rank order."""
    count = len(selected_ids)
    return [
        replace(configs[config_id], ensemble_weight=float(count - rank))
        for rank, config_id in enumerate(selected_ids)
    ]


def select_stable_ensemble(
    candidate_results: dict[int, pd.DataFrame],
    test_year: int | None = None,
    rolling_years: int = 3,
    ensemble_size: int = 3,
    candidate_groups: dict[int, str] | None = None,
    train_end: str | pd.Timestamp | None = None,
    expanding_start: str | pd.Timestamp = "2019-01-01",
    no_evidence_fallback_ids: list[int] | None = None,
) -> tuple[list[int], pd.DataFrame]:
    """Choose stable recent leaders, or a fixed core when none shows evidence."""
    if train_end is None:
        if test_year is None:
            raise ValueError("Either test_year or train_end is required")
        train_end = pd.Timestamp(test_year - 1, 12, 31)
    else:
        train_end = pd.Timestamp(train_end).normalize()
    rolling_months = rolling_years * 12
    rolling_start = (train_end.to_period("M") - (rolling_months - 1)).start_time
    expanding_start = pd.Timestamp(expanding_start).normalize()
    rows = []
    for config_id, result in candidate_results.items():
        monthly = _monthly(result.loc[expanding_start:train_end])
        rolling = monthly.loc[rolling_start:train_end, "excess_vs_nasdaq"]
        expanding = monthly["excess_vs_nasdaq"]
        if len(rolling) < rolling_months or expanding.empty:
            continue
        rolling_downside = float(rolling.clip(upper=0).pow(2).mean() ** 0.5)
        expanding_downside = float(expanding.clip(upper=0).pow(2).mean() ** 0.5)
        rows.append({
            "config_id": config_id,
            "rolling_months": len(rolling),
            "rolling_quality": float(rolling.mean()) - 0.5 * rolling_downside,
            "expanding_quality": float(expanding.mean()) - 0.5 * expanding_downside,
            "rolling_worst_annual_excess": float(
                _annual(result.loc[rolling_start:train_end])["excess_vs_nasdaq"].min()
            ),
        })
    ranking = pd.DataFrame(rows)
    if ranking.empty:
        raise ValueError(f"No complete rolling training window through {train_end.date()}")
    ranking["rolling_rank"] = ranking["rolling_quality"].rank(
        method="min", ascending=False
    )
    ranking["expanding_rank"] = ranking["expanding_quality"].rank(
        method="min", ascending=False
    )
    ranking["combined_rank"] = ranking["rolling_rank"] + ranking["expanding_rank"]
    if candidate_groups:
        ranking["candidate_group"] = ranking["config_id"].map(candidate_groups)
        ranking["variant_selected"] = False
        ranking["variant_rolling_rank"] = ranking.groupby(
            "candidate_group"
        )["rolling_quality"].rank(method="min", ascending=False)
        ranking["variant_expanding_rank"] = ranking.groupby(
            "candidate_group"
        )["expanding_quality"].rank(method="min", ascending=False)
        ranking["variant_combined_rank"] = (
            ranking["variant_rolling_rank"]
            + ranking["variant_expanding_rank"]
        )
        variant_ids = (
            ranking.sort_values(
                [
                    "variant_combined_rank",
                    "rolling_worst_annual_excess",
                    "rolling_quality",
                ],
                ascending=[True, False, False],
            )
            .groupby("candidate_group", sort=False)
            .head(1)["config_id"]
        )
        ranking.loc[ranking["config_id"].isin(variant_ids), "variant_selected"] = True
        eligible_ranking = ranking.loc[ranking["variant_selected"]].copy()
        eligible_ranking["rolling_rank"] = eligible_ranking["rolling_quality"].rank(
            method="min", ascending=False
        )
        eligible_ranking["expanding_rank"] = eligible_ranking[
            "expanding_quality"
        ].rank(method="min", ascending=False)
        eligible_ranking["combined_rank"] = (
            eligible_ranking["rolling_rank"] + eligible_ranking["expanding_rank"]
        )
        ranking.loc[eligible_ranking.index, [
            "rolling_rank", "expanding_rank", "combined_rank"
        ]] = eligible_ranking[["rolling_rank", "expanding_rank", "combined_rank"]]
    else:
        eligible_ranking = ranking
    cutoff = (len(eligible_ranking) + 1) // 2
    stable = eligible_ranking.loc[
        (eligible_ranking["rolling_rank"] <= cutoff)
        & (eligible_ranking["expanding_rank"] <= cutoff)
    ].sort_values(
        ["combined_rank", "rolling_worst_annual_excess", "rolling_quality"],
        ascending=[True, False, False],
    )
    if len(stable) < 2:
        stable = eligible_ranking.sort_values(
            ["combined_rank", "rolling_worst_annual_excess", "rolling_quality"],
            ascending=[True, False, False],
        )
    if (
        no_evidence_fallback_ids
        and float(ranking["rolling_quality"].max()) <= 0.0
    ):
        missing = set(no_evidence_fallback_ids) - set(ranking["config_id"])
        if missing:
            raise ValueError(
                f"Fallback configs missing from ranking: {sorted(missing)}"
            )
        selected = list(no_evidence_fallback_ids)
        ranking["selection_reason"] = "no_positive_rolling_evidence"
    else:
        selected = stable.head(
            min(ensemble_size, len(stable))
        )["config_id"].astype(int).tolist()
        ranking["selection_reason"] = "adaptive_stability_rank"
    ranking["selected"] = ranking["config_id"].isin(selected)
    return selected, ranking.sort_values("combined_rank")


def annual_parameter_snapshot_periods(
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """Return annual frozen periods and their strictly prior training cutoffs."""
    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()
    effective_starts = pd.date_range(
        pd.Timestamp(start.year, 1, 1), end, freq="YS"
    )
    effective_starts = [date for date in effective_starts if date >= start]
    periods = []
    for index, effective_start in enumerate(effective_starts):
        next_start = (
            effective_starts[index + 1]
            if index + 1 < len(effective_starts)
            else end + pd.Timedelta(days=1)
        )
        effective_end = min(end, next_start - pd.Timedelta(days=1))
        train_end = effective_start - pd.Timedelta(days=1)
        periods.append((effective_start, effective_end, train_end))
    return periods


def configs_from_snapshots(
    snapshots: list[dict], decision_date: str | pd.Timestamp
) -> list[CanSlimConfig] | None:
    """Resolve the latest already-effective frozen configuration."""
    decision_date = pd.Timestamp(decision_date).normalize()
    eligible = [
        snapshot for snapshot in snapshots
        if snapshot["effective_start"] <= decision_date
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda item: item["effective_start"])["configs"]


def fit_annual_parameter_snapshot(
    effective_year: int,
    signal_frequency: str = "monthly",
    use_quarterly_fundamentals: bool = True,
    adaptive_channel: bool = True,
) -> tuple[dict, pd.DataFrame]:
    """Fit one annual snapshot using only information before its effective date."""
    effective_start = pd.Timestamp(effective_year, 1, 1)
    effective_end = pd.Timestamp(effective_year, 12, 31)
    train_end = effective_start - pd.Timedelta(days=1)
    configs = candidate_configs(
        signal_frequency,
        use_quarterly_fundamentals,
        adaptive_channel,
        end=train_end,
    )
    candidate_groups = {
        config_id: f"top{config.top_n}-liq{int(config.minimum_median_dollar_volume)}"
        for config_id, config in enumerate(configs)
    }
    load_start = "2017-11-28"
    if adaptive_channel:
        close, dollar_volume, high, low = load_ohlc_panel(
            CLEANED_PRICE_DATA_DIR, load_start, train_end
        )
    else:
        close, dollar_volume = load_panel(
            CLEANED_PRICE_DATA_DIR, load_start, train_end
        )
        high = low = None
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"].loc[:train_end]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    quarterly = (
        load_quarterly_fundamentals(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE)
        if use_quarterly_fundamentals else None
    )
    universe_snapshots = load_universe_snapshots()
    universe = lambda date: universe_as_of(universe_snapshots, date)
    candidate_results = {
        config_id: calculate_can_slim_returns(
            close, dollar_volume, nasdaq, eps, config, universe,
            quarterly, high, low,
        )
        for config_id, config in enumerate(configs)
    }
    selected_ids, ranking = select_stable_ensemble(
        candidate_results,
        candidate_groups=candidate_groups,
        train_end=train_end,
        no_evidence_fallback_ids=core_fallback_ids(configs),
    )
    ranking.insert(0, "effective_start", effective_start)
    ranking.insert(1, "training_end", train_end)
    snapshot = {
        "effective_start": effective_start.strftime("%Y-%m-%d"),
        "effective_end": effective_end.strftime("%Y-%m-%d"),
        "training_end": train_end.strftime("%Y-%m-%d"),
        "config_ids": selected_ids,
        "configs": [
            asdict(config)
            for config in rank_weighted_configs(configs, selected_ids)
        ],
    }
    return snapshot, ranking


def run_walk_forward(
    signal_frequency: str = "monthly",
    artifact_suffix: str = "",
    use_quarterly_fundamentals: bool = False,
    adaptive_channel: bool = False,
    maximum_financial_age_days: tuple[int, ...] = (550,),
    quarterly_path: str | Path = POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    configs = candidate_configs(
        signal_frequency,
        use_quarterly_fundamentals,
        adaptive_channel,
        maximum_financial_age_days=maximum_financial_age_days,
    )
    candidate_groups = {
        config_id: f"top{config.top_n}-liq{int(config.minimum_median_dollar_volume)}"
        for config_id, config in enumerate(configs)
    }
    load_start = "2017-11-28"
    if adaptive_channel:
        close, dollar_volume, high, low = load_ohlc_panel(
            CLEANED_PRICE_DATA_DIR, load_start, "2026-07-17"
        )
    else:
        close, dollar_volume = load_panel(
            CLEANED_PRICE_DATA_DIR, load_start, "2026-07-17"
        )
        high = low = None
    nasdaq = pd.read_csv(NASDAQ_INDEX_FILE, index_col="date", parse_dates=True)["close"]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    quarterly = (
        load_quarterly_fundamentals(quarterly_path)
        if use_quarterly_fundamentals else None
    )
    universe_snapshots = load_universe_snapshots()
    universe = lambda date: universe_as_of(universe_snapshots, date)

    candidate_rows = []
    candidate_annual: dict[int, pd.DataFrame] = {}
    candidate_results: dict[int, pd.DataFrame] = {}
    for config_id, config in enumerate(configs):
        result = calculate_can_slim_returns(
            close, dollar_volume, nasdaq, eps, config, universe, quarterly, high, low
        )
        annual = _annual(result)
        candidate_results[config_id] = result
        candidate_annual[config_id] = annual
        for year, row in annual.iterrows():
            candidate_rows.append({
                "config_id": config_id, "year": int(year), **asdict(config),
                "strategy": row.strategy, "nasdaq": row.benchmark,
                "excess_vs_nasdaq": row.excess_vs_nasdaq,
            })

    model_snapshots = []
    ranking_rows = []
    for effective_start, effective_end, train_end in annual_parameter_snapshot_periods(
        "2022-01-01", "2026-07-17"
    ):
        selected_ids, ranking = select_stable_ensemble(
            candidate_results,
            candidate_groups=candidate_groups,
            train_end=train_end,
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

    continuous = calculate_can_slim_scheduled_returns(
        close, dollar_volume, nasdaq, eps, "2022-01-01", "2026-07-17",
        lambda date: configs_from_snapshots(model_snapshots, date),
        universe, signal_frequency, quarterly,
        high, low,
    )
    continuous_annual = _annual(continuous)
    walk_rows = []
    for test_year in range(2022, 2027):
        test_annual = continuous_annual.loc[test_year]
        year_daily = continuous.loc[continuous.index.year == test_year]
        year_snapshots = [
            snapshot for snapshot in model_snapshots
            if snapshot["effective_start"].year == test_year
        ]
        walk_rows.append({
            "test_year": test_year,
            "training_years": "expanding from 2019 through each prior cutoff",
            "rolling_training_years": "36 months through each prior cutoff",
            "config_ids": json.dumps({
                snapshot["effective_start"].strftime("%Y-%m-%d"):
                    snapshot["config_ids"]
                for snapshot in year_snapshots
            }),
            "portfolio_state": "continuous",
            "strategy": test_annual.strategy,
            "nasdaq": test_annual.benchmark,
            "excess_vs_nasdaq": test_annual.excess_vs_nasdaq,
            "average_invested": float(year_daily["invested"].mean()),
            "invested_days_pct": float(year_daily["invested"].gt(0).mean()),
            "average_holdings": float(year_daily["holdings"].mean()),
            "annual_turnover": float(year_daily["turnover"].sum()),
        })

    candidates = pd.DataFrame(candidate_rows)
    walk = pd.DataFrame(walk_rows)
    current_snapshot = model_snapshots[-1]
    current_ids = current_snapshot["config_ids"]
    cost_rows = []
    for cost_bps in (10.0, 30.0, 50.0):
        stressed = continuous if cost_bps == 10.0 else calculate_can_slim_scheduled_returns(
            close, dollar_volume, nasdaq, eps, "2022-01-01", "2026-07-17",
            lambda date, bps=cost_bps: [
                replace(config, transaction_cost_bps=bps)
                for config in (configs_from_snapshots(model_snapshots, date) or [])
            ] or None,
            universe, signal_frequency, quarterly,
            high, low,
        )
        for year, row in _annual(stressed).iterrows():
            cost_rows.append({
                "cost_bps": cost_bps, "test_year": int(year),
                "strategy": row.strategy, "nasdaq": row.benchmark,
                "excess_vs_nasdaq": row.excess_vs_nasdaq,
            })
    cost_stress = pd.DataFrame(cost_rows)
    summary = {
        "model_version": (
            "can-slim-v4-adaptive-channel"
            if adaptive_channel
            else ("can-slim-v3-quarterly" if use_quarterly_fundamentals else "can-slim-v2")
        ),
        "method": (
            "36-month rolling selection with expanding-history stability ranks; "
            "fixed CAN SLIM core fallback when no candidate has positive rolling "
            "evidence; parameters updated annually and frozen between updates"
        ),
        "signal_frequency": signal_frequency,
        "parameter_update_frequency": "annual",
        "uses_quarterly_fundamentals": use_quarterly_fundamentals,
        "uses_adaptive_channel": adaptive_channel,
        "maximum_financial_age_days_grid": list(maximum_financial_age_days),
        "candidate_count": len(configs),
        "ensemble_size": len(current_ids),
        "out_of_sample_years": len(walk),
        "wins_vs_nasdaq": int(walk["excess_vs_nasdaq"].gt(0).sum()),
        "passed_every_out_of_sample_year": bool(walk["excess_vs_nasdaq"].gt(0).all()),
        "release_status": "PASS" if walk["excess_vs_nasdaq"].gt(0).all() else "BLOCKED",
        "current_shadow_config_ids": current_ids,
        "current_shadow_configs": [
            asdict(config) for config in current_snapshot["configs"]
        ],
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
        "cost_stress_wins": {
            str(int(cost)): int(group["excess_vs_nasdaq"].gt(0).sum())
            for cost, group in cost_stress.groupby("cost_bps")
        },
    }
    pd.concat(ranking_rows, ignore_index=True).to_csv(
        f"output/can_slim_walk_forward_rankings{artifact_suffix}.csv", index=False
    )
    cost_stress.to_csv(
        f"output/can_slim_walk_forward_cost_stress{artifact_suffix}.csv", index=False
    )
    return candidates, walk, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--signal-frequency", choices=("monthly", "weekly", "daily"), default="monthly"
    )
    parser.add_argument("--use-quarterly-fundamentals", action="store_true")
    parser.add_argument("--adaptive-channel", action="store_true")
    parser.add_argument(
        "--maximum-financial-age-days",
        default="550",
        help="Comma-separated predeclared freshness grid, for example 150,365,550",
    )
    parser.add_argument(
        "--quarterly-input",
        type=Path,
        default=Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE),
    )
    parser.add_argument(
        "--artifact-tag",
        help="Append a caller-supplied provenance tag to research outputs.",
    )
    args = parser.parse_args()
    financial_age_days = tuple(
        int(value.strip())
        for value in args.maximum_financial_age_days.split(",")
        if value.strip()
    )
    suffix_parts = [] if args.signal_frequency == "monthly" else [args.signal_frequency]
    if args.use_quarterly_fundamentals:
        suffix_parts.append("quarterly_financials")
    if args.adaptive_channel:
        suffix_parts.append("adaptive_channel")
    if financial_age_days != (550,):
        suffix_parts.append(
            "financial_age_" + "_".join(str(value) for value in financial_age_days)
        )
    if args.artifact_tag:
        suffix_parts.append(args.artifact_tag)
    suffix = f"_{'_'.join(suffix_parts)}" if suffix_parts else ""
    candidates, walk, summary = run_walk_forward(
        args.signal_frequency, suffix, args.use_quarterly_fundamentals,
        args.adaptive_channel, financial_age_days, args.quarterly_input,
    )
    output = Path("output")
    candidates.to_csv(output / f"can_slim_walk_forward_candidates{suffix}.csv", index=False)
    walk.to_csv(output / f"can_slim_walk_forward{suffix}.csv", index=False)
    (output / f"can_slim_walk_forward_summary{suffix}.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(walk.to_string(index=False))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
