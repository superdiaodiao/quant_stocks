"""Research-only robustness checks for the frozen fixed Top 3 policy."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import numpy as np
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
    calculate_can_slim_returns_with_ledger,
    select_can_slim_portfolio,
)
from src.research.can_slim_validation import fixed_top3_config
from src.research.data_quality import (
    apply_confirmed_price_adjustments,
    back_adjust_common_splits,
    detect_common_split_events,
    restore_contemporaneous_prices,
)
from src.research.data_fingerprint import can_slim_input_fingerprints
from src.research.panel_data import load_panel
from src.strategy.common import (
    market_regime_is_on,
    scheduled_signal_dates,
)
from src.research.universe_history import load_universe_snapshots, universe_as_of


MODEL_VERSION = "can-slim-top3-v1"
RESEARCH_STATUS = "RESEARCH_ONLY"
EVIDENCE_START = "2021-01-01"
SCENARIOS = (
    {
        "scenario": "baseline",
        "maximum_snapshot_age_days": None,
        "maximum_financial_age_days": 550,
        "adjust_splits": True,
    },
    {
        "scenario": "snapshot_max_40d",
        "maximum_snapshot_age_days": 40,
        "maximum_financial_age_days": 550,
        "adjust_splits": True,
    },
    {
        "scenario": "snapshot_max_30d",
        "maximum_snapshot_age_days": 30,
        "maximum_financial_age_days": 550,
        "adjust_splits": True,
    },
    {
        "scenario": "financial_max_200d",
        "maximum_snapshot_age_days": None,
        "maximum_financial_age_days": 200,
        "adjust_splits": True,
    },
    {
        "scenario": "financial_max_150d",
        "maximum_snapshot_age_days": None,
        "maximum_financial_age_days": 150,
        "adjust_splits": True,
    },
    {
        "scenario": "financial_max_120d",
        "maximum_snapshot_age_days": None,
        "maximum_financial_age_days": 120,
        "adjust_splits": True,
    },
    {
        "scenario": "no_split_adjustment_stress",
        "maximum_snapshot_age_days": None,
        "maximum_financial_age_days": 550,
        "adjust_splits": False,
    },
    {
        "scenario": "confirmed_selected_actions_only",
        "maximum_snapshot_age_days": None,
        "maximum_financial_age_days": 550,
        "adjust_splits": False,
        "confirmed_actions": True,
        "confirmed_actions_scope": "selected",
    },
    {
        "scenario": "confirmed_all_actions_only",
        "maximum_snapshot_age_days": None,
        "maximum_financial_age_days": 550,
        "adjust_splits": False,
        "confirmed_actions": True,
        "confirmed_actions_scope": "all",
    },
    {
        "scenario": "confirmed_all_with_contemporaneous_price_filter",
        "maximum_snapshot_age_days": None,
        "maximum_financial_age_days": 550,
        "adjust_splits": False,
        "confirmed_actions": True,
        "confirmed_actions_scope": "all",
        "contemporaneous_price_filter": True,
    },
    {
        "scenario": "confirmed_all_plus_failed_heuristic_stress",
        "maximum_snapshot_age_days": None,
        "maximum_financial_age_days": 550,
        "adjust_splits": False,
        "confirmed_actions": True,
        "confirmed_actions_scope": "all",
        "include_failed_heuristic": True,
    },
    {
        "scenario": "combined_40d_snapshot_150d_financial",
        "maximum_snapshot_age_days": 40,
        "maximum_financial_age_days": 150,
        "adjust_splits": True,
    },
)


def annual_scenario_metrics(result: pd.DataFrame) -> pd.DataFrame:
    """Return annual performance and risk metrics for one replay."""
    rows = []
    for year, frame in result.loc[EVIDENCE_START:].groupby(
        result.loc[EVIDENCE_START:].index.year
    ):
        strategy = frame["strategy"].astype(float)
        benchmark = frame["benchmark"].astype(float)
        nav = (1 + strategy).cumprod()
        annual_return = float(nav.iloc[-1] - 1)
        benchmark_return = float((1 + benchmark).prod() - 1)
        rows.append({
            "year": int(year),
            "strategy": annual_return,
            "nasdaq": benchmark_return,
            "excess_vs_nasdaq": annual_return - benchmark_return,
            "maximum_drawdown": float((nav / nav.cummax() - 1).min()),
            "annualized_volatility": float(strategy.std() * np.sqrt(252)),
            "average_invested": float(frame["invested"].mean()),
            "average_holdings": float(frame["holdings"].mean()),
            "annual_turnover": float(frame["turnover"].sum()),
        })
    return pd.DataFrame(rows)


def tail_dependency_diagnostics(
    result: pd.DataFrame,
) -> tuple[pd.DataFrame, dict]:
    """Measure whether annual outperformance depends on one tail period.

    This is a mechanical exclusion stress, not a tradable counterfactual:
    the strategy's best day/month and the benchmark's same dates are removed.
    """
    frame = (
        result.loc[EVIDENCE_START:, ["strategy", "benchmark"]]
        .dropna()
        .rename(columns={"benchmark": "nasdaq"})
    )
    rows = []
    for year, annual in frame.groupby(frame.index.year):
        if annual.empty:
            continue
        best_day = annual["strategy"].idxmax()
        without_day = annual.drop(index=best_day)
        monthly = (
            (1 + annual)
            .groupby(annual.index.to_period("M"))
            .prod()
            - 1
        )
        best_month = monthly["strategy"].idxmax()
        without_month = annual.loc[
            annual.index.to_period("M") != best_month
        ]

        def compounded(data: pd.DataFrame, column: str) -> float:
            return float((1 + data[column]).prod() - 1)

        original_strategy = compounded(annual, "strategy")
        original_nasdaq = compounded(annual, "nasdaq")
        no_day_strategy = compounded(without_day, "strategy")
        no_day_nasdaq = compounded(without_day, "nasdaq")
        no_month_strategy = compounded(without_month, "strategy")
        no_month_nasdaq = compounded(without_month, "nasdaq")
        original_excess = original_strategy - original_nasdaq
        no_day_excess = no_day_strategy - no_day_nasdaq
        no_month_excess = no_month_strategy - no_month_nasdaq
        rows.append({
            "year": int(year),
            "original_strategy_return": original_strategy,
            "original_nasdaq_return": original_nasdaq,
            "original_excess": original_excess,
            "best_strategy_day": best_day.strftime("%Y-%m-%d"),
            "best_strategy_day_return": float(
                annual.loc[best_day, "strategy"]
            ),
            "strategy_return_without_best_day": no_day_strategy,
            "nasdaq_return_without_same_day": no_day_nasdaq,
            "excess_without_best_day": no_day_excess,
            "outperformance_depends_on_best_day": bool(
                original_excess > 0 and no_day_excess <= 0
            ),
            "best_strategy_month": str(best_month),
            "best_strategy_month_return": float(
                monthly.loc[best_month, "strategy"]
            ),
            "strategy_return_without_best_month": no_month_strategy,
            "nasdaq_return_without_same_month": no_month_nasdaq,
            "excess_without_best_month": no_month_excess,
            "outperformance_depends_on_best_month": bool(
                original_excess > 0 and no_month_excess <= 0
            ),
        })
    details = pd.DataFrame(rows)
    summary = {
        "method": (
            "Mechanical exclusion stress: remove each year's best strategy "
            "day/month and remove the benchmark's same dates. This is not a "
            "tradable counterfactual or a parameter-selection criterion."
        ),
        "years": int(len(details)),
        "original_wins_vs_nasdaq": int(
            details["original_excess"].gt(0).sum()
        ),
        "wins_without_each_year_best_day": int(
            details["excess_without_best_day"].gt(0).sum()
        ),
        "wins_without_each_year_best_month": int(
            details["excess_without_best_month"].gt(0).sum()
        ),
        "years_dependent_on_best_day": details.loc[
            details["outperformance_depends_on_best_day"], "year"
        ].astype(int).tolist(),
        "years_dependent_on_best_month": details.loc[
            details["outperformance_depends_on_best_month"], "year"
        ].astype(int).tolist(),
    }
    return details, summary


def financial_freshness_summary(details: pd.DataFrame) -> dict:
    ages = pd.to_numeric(
        details["baseline_quarterly_financial_ages"]
        .fillna("")
        .str.split("|")
        .explode(),
        errors="coerce",
    ).dropna()
    return {
        "method": (
            "Compare the frozen 550-day policy with a 120-day financial-age "
            "stress on the same signal dates. This is diagnostic only and "
            "must not be used to retune the frozen policy after seeing returns."
        ),
        "signal_count": int(len(details)),
        "selected_position_observations": int(len(ages)),
        "median_selected_quarterly_financial_age_days": (
            float(ages.median()) if not ages.empty else None
        ),
        "p90_selected_quarterly_financial_age_days": (
            float(ages.quantile(0.9)) if not ages.empty else None
        ),
        "maximum_selected_quarterly_financial_age_days": (
            int(ages.max()) if not ages.empty else None
        ),
        "selected_positions_older_than_120d": int(ages.gt(120).sum()),
        "selected_positions_older_than_150d": int(ages.gt(150).sum()),
        "selected_positions_older_than_200d": int(ages.gt(200).sum()),
        "raw_top3_changed_signals": int(details["raw_top3_changed"].sum()),
        "executed_top3_changed_signals": int(
            details["executed_top3_changed"].sum()
        ),
        "raw_changed_signal_dates": details.loc[
            details["raw_top3_changed"], "signal_date"
        ].astype(str).tolist(),
        "executed_changed_signal_dates": details.loc[
            details["executed_top3_changed"], "signal_date"
        ].astype(str).tolist(),
    }


def financial_freshness_selection_impact(
    close: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    index_close: pd.Series,
    eps: pd.DataFrame,
    quarterly_fundamentals: pd.DataFrame,
    eligibility_close: pd.DataFrame,
) -> tuple[pd.DataFrame, dict]:
    baseline = fixed_top3_config()
    strict = replace(baseline, maximum_financial_age_days=120)
    rows = []
    snapshots = load_universe_snapshots()
    for signal_date in scheduled_signal_dates(
        close.index,
        baseline.start,
        baseline.end,
        baseline.signal_frequency,
    ):
        if signal_date < pd.Timestamp(EVIDENCE_START):
            continue
        symbols = universe_as_of(snapshots, signal_date)
        if symbols is None:
            continue
        baseline_selected = select_can_slim_portfolio(
            signal_date,
            close,
            dollar_volume,
            index_close,
            eps,
            baseline,
            symbols,
            quarterly_fundamentals,
            eligibility_close=eligibility_close,
        )
        strict_selected = select_can_slim_portfolio(
            signal_date,
            close,
            dollar_volume,
            index_close,
            eps,
            strict,
            symbols,
            quarterly_fundamentals,
            eligibility_close=eligibility_close,
        )
        baseline_tickers = tuple(sorted(baseline_selected.index.astype(str)))
        strict_tickers = tuple(sorted(strict_selected.index.astype(str)))
        risk_on = market_regime_is_on(
            signal_date, index_close, baseline.market_ma_days
        )
        ages = pd.to_numeric(
            baseline_selected.get(
                "quarterly_financial_age_days",
                pd.Series(dtype=float),
            ),
            errors="coerce",
        ).dropna()
        rows.append({
            "signal_date": signal_date.strftime("%Y-%m-%d"),
            "market_regime_on": bool(risk_on),
            "baseline_top3": "|".join(baseline_tickers),
            "strict_120d_top3": "|".join(strict_tickers),
            "baseline_quarterly_financial_ages": "|".join(
                str(int(age)) for age in ages
            ),
            "baseline_max_quarterly_financial_age_days": (
                int(ages.max()) if not ages.empty else None
            ),
            "raw_top3_changed": baseline_tickers != strict_tickers,
            "executed_top3_changed": (
                risk_on and baseline_tickers != strict_tickers
            ),
        })
    details = pd.DataFrame(rows)
    return details, financial_freshness_summary(details)


def realized_concentration(ledger: pd.DataFrame) -> pd.DataFrame:
    """Summarize concentration in realized PnL without relabeling it total return."""
    if ledger.empty:
        return pd.DataFrame()
    frame = ledger.copy()
    frame["execution_date"] = pd.to_datetime(frame["execution_date"])
    rows = []
    for year, annual in frame.loc[
        frame["execution_date"].dt.year >= 2021
    ].groupby(frame["execution_date"].dt.year):
        pnl = annual.groupby("ticker")["realized_pnl"].sum().fillna(0).sort_values(
            ascending=False
        )
        total = float(pnl.sum())
        rows.append({
            "year": int(year),
            "tickers_traded": int(annual["ticker"].nunique()),
            "net_realized_pnl": total,
            "top_ticker": str(pnl.index[0]) if len(pnl) else None,
            "top1_share_of_net_realized_pnl": (
                float(pnl.iloc[:1].sum() / total) if total > 0 else np.nan
            ),
            "top2_share_of_net_realized_pnl": (
                float(pnl.iloc[:2].sum() / total) if total > 0 else np.nan
            ),
            "top4_share_of_net_realized_pnl": (
                float(pnl.iloc[:4].sum() / total) if total > 0 else np.nan
            ),
        })
    return pd.DataFrame(rows)


def run_robustness_checks() -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    dict,
]:
    """Run fixed-policy sensitivities without changing the production model."""
    base_config = fixed_top3_config()
    load_start = (
        pd.Timestamp(base_config.start) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")
    close, dollar_volume = load_panel(
        CLEANED_PRICE_DATA_DIR, load_start, base_config.end
    )
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    quarterly = load_quarterly_fundamentals(
        POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
    )
    snapshots = load_universe_snapshots()
    confirmed_close = None

    scenario_rows = []
    scenario_summaries = []
    baseline_ledger = pd.DataFrame()
    baseline_result = pd.DataFrame()
    for scenario in SCENARIOS:
        scenario_close = close
        eligibility_close = None
        if scenario.get("confirmed_actions"):
            action_scope = scenario.get("confirmed_actions_scope", "selected")
            validation_path = Path(
                f"output/can_slim_{action_scope}_corporate_action_validation.csv"
            )
            if not validation_path.exists():
                raise FileNotFoundError(
                    "Run corporate_action_validation before confirmed-action "
                    "robustness checks"
                )
            validation = pd.read_csv(validation_path)
            confirmed = validation.loc[
                validation["validation_status"].eq("CONFIRMED")
            ].rename(columns={
                "split_date": "effective_date",
                "confirmed_adjustment_factor": "adjustment_factor",
            })
            if scenario.get("include_failed_heuristic"):
                failed = validation.loc[
                    validation["validation_status"].eq("SOURCE_FETCH_FAILED")
                ].rename(columns={
                    "split_date": "effective_date",
                    "matched_factor": "adjustment_factor",
                })
                confirmed = pd.concat(
                    [confirmed, failed], ignore_index=True
                )
            confirmed_close = apply_confirmed_price_adjustments(
                close,
                confirmed[[
                    "ticker", "effective_date", "adjustment_factor"
                ]],
            )
            scenario_close = confirmed_close
            if scenario.get("contemporaneous_price_filter"):
                eligibility_close = restore_contemporaneous_prices(
                    close, validation
                )
        maximum_snapshot_age = scenario["maximum_snapshot_age_days"]
        universe = lambda date, age=maximum_snapshot_age: universe_as_of(
            snapshots, date, maximum_age_days=age
        )
        config = replace(
            base_config,
            maximum_financial_age_days=scenario["maximum_financial_age_days"],
        )
        if scenario["scenario"] == "baseline":
            baseline_result, baseline_ledger = (
                calculate_can_slim_returns_with_ledger(
                close,
                dollar_volume,
                nasdaq,
                eps,
                config,
                universe,
                quarterly,
                adjust_splits=scenario["adjust_splits"],
                eligibility_close=eligibility_close,
            )
            )
            result = baseline_result
        else:
            result = calculate_can_slim_returns(
                scenario_close,
                dollar_volume,
                nasdaq,
                eps,
                config,
                universe,
                quarterly,
                adjust_splits=scenario["adjust_splits"],
                eligibility_close=eligibility_close,
            )
        annual = annual_scenario_metrics(result)
        annual.insert(0, "scenario", scenario["scenario"])
        annual["maximum_snapshot_age_days"] = maximum_snapshot_age
        annual["maximum_financial_age_days"] = scenario[
            "maximum_financial_age_days"
        ]
        annual["adjust_splits"] = scenario["adjust_splits"]
        annual["price_adjustment_method"] = (
            (
                "confirmed_all_actions_plus_failed_heuristic"
                if scenario.get("include_failed_heuristic")
                else (
                    "confirmed_all_actions_with_contemporaneous_price_filter"
                    if scenario.get("contemporaneous_price_filter")
                    else
                    f"confirmed_"
                    f"{scenario.get('confirmed_actions_scope', 'selected')}_actions"
                )
            )
            if scenario.get("confirmed_actions")
            else ("integer_ratio_heuristic" if scenario["adjust_splits"] else "none")
        )
        scenario_rows.append(annual)
        scenario_summaries.append({
            "scenario": scenario["scenario"],
            "historical_periods": int(len(annual)),
            "wins_vs_nasdaq": int(annual["excess_vs_nasdaq"].gt(0).sum()),
            "minimum_annual_excess": float(annual["excess_vs_nasdaq"].min()),
            "compounded_strategy_return": float(
                (1 + annual["strategy"]).prod() - 1
            ),
            "compounded_nasdaq_return": float(
                (1 + annual["nasdaq"]).prod() - 1
            ),
            "worst_annual_drawdown": float(annual["maximum_drawdown"].min()),
        })

    scenarios = pd.concat(scenario_rows, ignore_index=True)
    selected_symbols = set(
        baseline_ledger["ticker"].astype(str)
    ) if len(baseline_ledger) else set()
    split_events = detect_common_split_events(close)
    split_events["selected_by_fixed_top3"] = split_events["ticker"].isin(
        selected_symbols
    )
    split_events["requires_external_confirmation"] = split_events[
        "selected_by_fixed_top3"
    ]
    concentration = realized_concentration(baseline_ledger)
    tail_dependency, tail_dependency_summary = tail_dependency_diagnostics(
        baseline_result
    )
    freshness, freshness_summary = financial_freshness_selection_impact(
        back_adjust_common_splits(close),
        dollar_volume,
        nasdaq,
        eps,
        quarterly,
        close,
    )
    action_validation_path = Path(
        "output/can_slim_all_corporate_action_validation_summary.json"
    )
    action_validation = (
        json.loads(action_validation_path.read_text(encoding="utf-8"))
        if action_validation_path.exists() else None
    )
    summary = {
        "model_version": MODEL_VERSION,
        "status": RESEARCH_STATUS,
        "formal_model_changed": False,
        "scenario_count": len(SCENARIOS),
        "scenario_summaries": scenario_summaries,
        "heuristic_split_events": int(len(split_events)),
        "heuristic_split_events_in_selected_symbols": int(
            split_events["selected_by_fixed_top3"].sum()
        ),
        "traded_symbols": sorted(selected_symbols),
        "split_warning": (
            "The no-adjustment scenario is a stress bound, not a valid "
            "corporate-action replacement. Every inferred event affecting a "
            "selected symbol still requires external confirmation."
        ),
        "corporate_action_validation": action_validation,
        "concentration_metric": (
            "Realized-PnL concentration from the trade ledger; it is not an "
            "exact decomposition of annual total return."
        ),
        "tail_dependency": tail_dependency_summary,
        "financial_freshness_selection_impact": freshness_summary,
        "input_fingerprints": can_slim_input_fingerprints(),
    }
    return (
        scenarios,
        split_events,
        concentration,
        tail_dependency,
        freshness,
        summary,
    )


def main() -> None:
    (
        scenarios,
        split_events,
        concentration,
        tail_dependency,
        freshness,
        summary,
    ) = run_robustness_checks()
    output = Path("output")
    scenarios.to_csv(
        output / "can_slim_fixed_top3_robustness_scenarios.csv", index=False
    )
    split_events.to_csv(
        output / "can_slim_fixed_top3_split_events.csv", index=False
    )
    concentration.to_csv(
        output / "can_slim_fixed_top3_concentration.csv", index=False
    )
    tail_dependency.to_csv(
        output / "can_slim_fixed_top3_tail_dependency.csv", index=False
    )
    freshness.to_csv(
        output / "can_slim_fixed_top3_financial_freshness_impact.csv",
        index=False,
    )
    (output / "can_slim_fixed_top3_robustness_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(scenarios.to_string(index=False))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
