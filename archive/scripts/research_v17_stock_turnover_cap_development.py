#!/usr/bin/env python3
"""Development-only test of capped stock-to-stock v16 rebalances.

This post-v16 hypothesis preserves every frozen v16 target and executes cash,
QQQ-core, and risk-regime transitions in full.  Only a scheduled stock target
following another stock target may be moved toward gradually under a turnover
cap.  Candidate selection is bounded to 2022-2024 and cannot create a clean
historical holdout because the later interval was already exposed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v15_benchmark_core_development as v15
from scripts import research_v16_trend_confirmed_qqq_development as v16
from src.conf import NASDAQ_INDEX_FILE
from src.research.data_quality import (
    back_adjust_common_splits,
    stock_returns_with_delisting_penalty,
)
from src.research.panel_data import load_panel


START = v15.START
DEVELOPMENT_END = v15.DEVELOPMENT_END
CORE_TICKER = v15.CORE_TICKER
TURNOVER_CAP_CANDIDATES = (0.50, 1.00, 1.50)
MINIMUM_TURNOVER_REDUCTION_FRACTION = 0.10
OUTPUT_DIR = Path("output/research_only/v17/stock_turnover_cap_development")

V16_DEVELOPMENT = {
    "path": Path(
        "output/research_only/v16/trend_confirmed_qqq_development/manifest.json"
    ),
    "sha256": (
        "42e59bc3070003e9efddb6fdab4a0a45e10d09e52f3ff21efc05ee49f212245c"
    ),
}
V16_TARGETS = {
    "path": Path(
        "output/research_only/v16/trend_confirmed_qqq_development/"
        "targets_sma_50.csv"
    ),
    "sha256": (
        "da271e14b128c89b2358b135df6f1b2559f0c7d5872d929ce8f083a35b2cab91"
    ),
}
V16_BASELINE_DAILY = {
    10: {
        "path": Path(
            "output/research_only/v16/trend_confirmed_qqq_development/"
            "daily_sma_50_10bps.csv"
        ),
        "sha256": (
            "5d7ecf8062dcd2dca1a27fcd81aed444c295095bd74cda90cffc2e69c8572ca1"
        ),
    },
    30: {
        "path": Path(
            "output/research_only/v16/trend_confirmed_qqq_development/"
            "daily_sma_50_30bps.csv"
        ),
        "sha256": (
            "98800d98bc8abfcca6f14fc88c76656bcf48d2d730d258e02dd198719dfaefd1"
        ),
    },
    50: {
        "path": Path(
            "output/research_only/v16/trend_confirmed_qqq_development/"
            "daily_sma_50_50bps.csv"
        ),
        "sha256": (
            "c2f311c77f28b7eef190f11adcedc0aff6134b918eaa86379fb519fbf344e8b7"
        ),
    },
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_binding(name: str, binding: dict) -> dict:
    path = Path(binding["path"])
    actual = _sha256(path)
    if actual != binding["sha256"]:
        raise RuntimeError(f"{name} binding changed: {actual}")
    return {"path": str(path), "sha256": actual}


def _target_state(group: pd.DataFrame) -> str:
    tickers = set(group["ticker"].astype(str))
    if tickers == {"__CASH__"}:
        return "cash"
    if tickers == {CORE_TICKER}:
        return "core"
    if "__CASH__" in tickers or CORE_TICKER in tickers:
        raise ValueError("target event mixes stocks and synthetic tickers")
    return "stocks"


def _solve_trade(
    position_values: pd.Series,
    pre_trade_nav: float,
    target_weights: pd.Series,
    cost_rate: float,
) -> tuple[pd.Series, float, float, float]:
    post_trade_nav = float(pre_trade_nav)
    for _ in range(50):
        desired = target_weights * post_trade_nav
        traded = float((desired - position_values).abs().sum())
        updated = float(pre_trade_nav - traded * cost_rate)
        if abs(updated - post_trade_nav) < 1e-13:
            post_trade_nav = updated
            break
        post_trade_nav = updated
    desired = target_weights * post_trade_nav
    traded = float((desired - position_values).abs().sum())
    turnover = traded / pre_trade_nav if pre_trade_nav else 0.0
    cost = traded * cost_rate
    return desired, turnover, cost, post_trade_nav


def _capped_trade(
    position_values: pd.Series,
    pre_trade_nav: float,
    target_weights: pd.Series,
    cost_rate: float,
    turnover_cap: float,
) -> tuple[pd.Series, float, float, float]:
    full = _solve_trade(
        position_values, pre_trade_nav, target_weights, cost_rate
    )
    if full[1] <= turnover_cap + 1e-12:
        return full
    current_weights = (
        position_values / pre_trade_nav
        if pre_trade_nav
        else pd.Series(0.0, index=position_values.index)
    )
    low = 0.0
    high = 1.0
    accepted = _solve_trade(
        position_values, pre_trade_nav, current_weights, cost_rate
    )
    for _ in range(80):
        midpoint = (low + high) / 2.0
        blended = current_weights + midpoint * (
            target_weights - current_weights
        )
        candidate = _solve_trade(
            position_values, pre_trade_nav, blended, cost_rate
        )
        if candidate[1] <= turnover_cap:
            low = midpoint
            accepted = candidate
        else:
            high = midpoint
    if accepted[1] > turnover_cap + 1e-10:
        raise RuntimeError("capped trade exceeded its turnover limit")
    return accepted


def replay_stock_turnover_cap(
    close: pd.DataFrame,
    index_close: pd.Series,
    target_schedule: pd.DataFrame,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    stock_to_stock_turnover_cap: float,
    excluded_tickers: tuple[str, ...] = (),
    adjust_splits: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Replay frozen targets, capping only consecutive stock target changes."""
    if stock_to_stock_turnover_cap <= 0.0:
        raise ValueError("stock-to-stock turnover cap must be positive")
    required = {
        "effective_date",
        "ticker",
        "target_weight",
        "base_transaction_cost_bps",
    }
    missing = required - set(target_schedule.columns)
    if missing:
        raise ValueError(f"target schedule columns missing: {sorted(missing)}")
    prices = (
        back_adjust_common_splits(close) if adjust_splits else close.copy()
    ).sort_index()
    index_close = index_close.reindex(prices.index).ffill()
    schedule = target_schedule.copy()
    schedule["effective_date"] = pd.to_datetime(
        schedule["effective_date"], errors="raise"
    ).dt.normalize()
    schedule["ticker"] = schedule["ticker"].astype(str)
    excluded = {str(ticker) for ticker in excluded_tickers}
    unknown = set(schedule["ticker"]) - set(prices.columns.astype(str)) - {
        "__CASH__"
    }
    if unknown:
        raise ValueError(f"target schedule has unknown tickers: {sorted(unknown)}")
    events = {}
    for date, group in schedule.groupby("effective_date", sort=True):
        costs = group["base_transaction_cost_bps"].astype(float).unique()
        if len(costs) != 1:
            raise ValueError(f"multiple cost rates on {date.date()}")
        active = group.loc[
            ~group["ticker"].isin(excluded | {"__CASH__"})
        ]
        if active["ticker"].duplicated().any():
            raise ValueError(f"duplicate target ticker on {date.date()}")
        target = pd.Series(0.0, index=prices.columns)
        if len(active):
            target.loc[active["ticker"]] = active["target_weight"].astype(
                float
            ).to_numpy()
        if target.lt(0.0).any() or float(target.sum()) > 1.0 + 1e-9:
            raise ValueError(f"invalid target weights on {date.date()}")
        events[pd.Timestamp(date)] = (
            target,
            float(costs[0]),
            _target_state(group),
        )

    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()
    if start > end:
        raise ValueError("target replay start must not follow end")
    stock_returns = stock_returns_with_delisting_penalty(prices).fillna(0.0)
    position_values = pd.Series(0.0, index=prices.columns)
    cash = 1.0
    nav = 1.0
    previous_target_state: str | None = None
    rows = []
    gross_contribution = pd.Series(0.0, index=prices.columns)
    cost_contribution = pd.Series(0.0, index=prices.columns)
    for current_date, returns in stock_returns.iterrows():
        previous_nav = nav
        attribution_active = start <= current_date <= end
        if attribution_active and previous_nav:
            gross_contribution = gross_contribution.add(
                position_values.mul(returns).div(previous_nav), fill_value=0.0
            )
        position_values = position_values.mul(1.0 + returns)
        pre_trade_nav = float(cash + position_values.sum())
        event = events.get(current_date)
        turnover = 0.0
        if event is not None:
            target, cost_bps, target_state = event
            cost_rate = cost_bps / 10_000.0
            if previous_target_state == "stocks" and target_state == "stocks":
                desired, turnover, cost, _ = _capped_trade(
                    position_values,
                    pre_trade_nav,
                    target,
                    cost_rate,
                    stock_to_stock_turnover_cap,
                )
            else:
                desired, turnover, cost, _ = _solve_trade(
                    position_values, pre_trade_nav, target, cost_rate
                )
            trade_costs = (desired - position_values).abs() * cost_rate
            if attribution_active and previous_nav:
                cost_contribution = cost_contribution.add(
                    trade_costs.div(previous_nav), fill_value=0.0
                )
            cash = float(pre_trade_nav - desired.sum() - cost)
            position_values = desired
            nav = float(cash + position_values.sum())
            previous_target_state = target_state
        else:
            nav = pre_trade_nav
        rows.append((
            nav / previous_nav - 1.0 if previous_nav else 0.0,
            float(index_close.pct_change(fill_method=None).loc[current_date])
            if current_date != prices.index[0]
            else 0.0,
            float(position_values.sum() / nav) if nav else 0.0,
            turnover,
            int((position_values > 1e-8).sum()),
        ))
    result = pd.DataFrame(rows, index=prices.index, columns=[
        "strategy", "benchmark", "invested", "turnover", "holdings"
    ]).loc[start:end]
    contributions = pd.DataFrame({
        "ticker": prices.columns.astype(str),
        "gross_return_contribution": gross_contribution.to_numpy(),
        "transaction_cost_contribution": cost_contribution.to_numpy(),
    })
    contributions["net_return_contribution"] = (
        contributions["gross_return_contribution"]
        - contributions["transaction_cost_contribution"]
    )
    contributions = contributions.loc[
        contributions["gross_return_contribution"].ne(0.0)
        | contributions["transaction_cost_contribution"].ne(0.0)
    ].sort_values(
        ["net_return_contribution", "ticker"],
        ascending=[False, True],
    ).reset_index(drop=True)
    return result, contributions


def _candidate_score(summary: dict) -> tuple[float, float, float]:
    annual = summary["performance"]["costs"]["10"]["annual"]
    minimum_excess = min(
        float(row["excess_vs_nasdaq"]) for row in annual
    )
    compounded_excess = float(
        summary["performance"]["costs"]["10"]["compounded_excess"]
    )
    turnover_reduction = float(summary["turnover_reduction_fraction_10bps"])
    return minimum_excess, compounded_excess, turnover_reduction


def select_candidate(candidate_summaries: dict[float, dict]) -> float | None:
    eligible = [
        cap
        for cap, summary in candidate_summaries.items()
        if summary["all_development_gates_passed"]
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda cap: (*_candidate_score(candidate_summaries[cap]), -cap),
    )


def run(output_dir: Path = OUTPUT_DIR) -> dict:
    bindings = {
        "v14_protocol": v15._verify_binding("v14_protocol", v15.V14_PROTOCOL),
        "v14_targets": v15._verify_binding("v14_targets", v15.V14_TARGETS),
        "v14_daily": v15._verify_binding("v14_daily", v15.V14_DAILY),
        "qqq_history": v15._verify_binding("qqq_history", v15.QQQ_HISTORY),
        "qqq_provenance": v15._verify_binding(
            "qqq_provenance", v15.QQQ_PROVENANCE
        ),
        "v16_development": _verify_binding(
            "v16_development", V16_DEVELOPMENT
        ),
        "v16_targets": _verify_binding("v16_targets", V16_TARGETS),
        "v16_baseline_daily": {
            str(cost): _verify_binding(
                f"v16_baseline_{cost}bps", binding
            )
            for cost, binding in V16_BASELINE_DAILY.items()
        },
    }
    v16_manifest = json.loads(
        V16_DEVELOPMENT["path"].read_text(encoding="utf-8")
    )
    if v16_manifest["selected_lookback"] != 50:
        raise RuntimeError("frozen v16 development selection changed")
    if v16_manifest["confirmation_period_computed"]:
        raise RuntimeError("v16 development crossed its boundary")
    v14_protocol = json.loads(
        v15.V14_PROTOCOL["path"].read_text(encoding="utf-8")
    )
    price_binding = v14_protocol["input_bindings"]["price_directory"]
    close, _ = load_panel(price_binding["path"], "2017-01-01", DEVELOPMENT_END)
    prices = back_adjust_common_splits(close)
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    qqq = pd.read_csv(
        v15.QQQ_HISTORY["path"], index_col="date", parse_dates=True
    )
    prices[CORE_TICKER] = v15.qqq_total_return_index(
        qqq,
        prices.index,
        allowed_market_closed=nasdaq.reindex(prices.index).isna(),
    )
    targets = pd.read_csv(
        V16_TARGETS["path"], parse_dates=["effective_date"]
    )
    v14_daily = pd.read_csv(
        v15.V14_DAILY["path"], index_col="date", parse_dates=True
    )
    baseline_daily = {
        cost: pd.read_csv(
            binding["path"], index_col="date", parse_dates=True
        )
        for cost, binding in V16_BASELINE_DAILY.items()
    }
    baseline_turnover_10bps = float(baseline_daily[10]["turnover"].sum())

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_paths = {}
    candidate_summaries = {}
    for cap in TURNOVER_CAP_CANDIDATES:
        cost_results = {}
        for cost in (10, 30, 50):
            stressed = targets.copy()
            stressed["base_transaction_cost_bps"] = float(cost)
            result, _ = replay_stock_turnover_cap(
                prices,
                nasdaq,
                stressed,
                START,
                DEVELOPMENT_END,
                stock_to_stock_turnover_cap=cap,
                adjust_splits=False,
            )
            cost_results[cost] = result
            path = output_dir / f"daily_cap_{cap:.2f}_{cost}bps.csv"
            result.to_csv(path, index_label="date")
            output_paths[f"daily_cap_{cap:.2f}_{cost}bps"] = path
        performance = v15.summarize_development(cost_results, v14_daily)
        turnover_10bps = float(cost_results[10]["turnover"].sum())
        reduction = 1.0 - turnover_10bps / baseline_turnover_10bps
        passed_turnover = reduction >= MINIMUM_TURNOVER_REDUCTION_FRACTION
        candidate_summaries[cap] = {
            "performance": performance,
            "baseline_turnover_10bps": baseline_turnover_10bps,
            "candidate_turnover_10bps": turnover_10bps,
            "turnover_reduction_fraction_10bps": reduction,
            "required_turnover_reduction_fraction": (
                MINIMUM_TURNOVER_REDUCTION_FRACTION
            ),
            "turnover_reduction_gate_passed": passed_turnover,
            "all_development_gates_passed": bool(
                performance["all_development_gates_passed"]
                and passed_turnover
            ),
        }
    selected = select_candidate(candidate_summaries)
    report = {
        "schema_version": 1,
        "research_only": True,
        "hypothesis": "V17_STOCK_TO_STOCK_TURNOVER_CAP",
        "stage": "DEVELOPMENT_ONLY",
        "post_v16_confirmation_hypothesis": True,
        "historical_selection_contaminated": True,
        "development_period": {"start": START, "end": DEVELOPMENT_END},
        "post_development_period_computed": False,
        "post_development_results_inspected": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "formal_universe_modified": False,
        "signal_policy": "preserve the frozen v16 target schedule exactly",
        "execution_policy": (
            "execute initial, cash, QQQ-core, and regime transitions in full; "
            "cap only consecutive stock-to-stock target turnover"
        ),
        "turnover_cap_candidates": list(TURNOVER_CAP_CANDIDATES),
        "minimum_turnover_reduction_fraction": (
            MINIMUM_TURNOVER_REDUCTION_FRACTION
        ),
        "candidate_selection_rule": (
            "all v16 development performance gates and turnover reduction "
            "must pass; then maximize minimum 10bps annual excess, compounded "
            "10bps excess, turnover reduction, and prefer the smaller cap"
        ),
        "candidate_results": {
            f"{cap:.2f}": {
                "score": list(_candidate_score(summary)),
                **summary,
            }
            for cap, summary in candidate_summaries.items()
        },
        "selected_turnover_cap": selected,
        "all_development_gates_passed": selected is not None,
        "input_bindings": {**bindings, "price_directory": price_binding},
        "outputs": {
            name: {"path": str(path), "sha256": _sha256(path)}
            for name, path in output_paths.items()
        },
        "interpretation_guardrail": (
            "This is post-confirmation exploratory development. It cannot "
            "repair v16, create an untouched holdout, or authorize release."
        ),
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
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    report = run(args.output_dir)
    print(json.dumps({
        "hypothesis": report["hypothesis"],
        "stage": report["stage"],
        "selected_turnover_cap": report["selected_turnover_cap"],
        "all_development_gates_passed": report[
            "all_development_gates_passed"
        ],
        "post_development_period_computed": report[
            "post_development_period_computed"
        ],
        "release_status": report["release_status"],
        "manifest": report["manifest"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
