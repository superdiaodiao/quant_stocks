#!/usr/bin/env python3
"""Development-only test of a trend-confirmed QQQ residual-cash sleeve.

This hypothesis is separate from the failed unconditional v15 cash fill.  It
preserves every frozen v14 stock target.  When v14 is wholly in cash, QQQ is
held only after its prior-session close is above a predeclared moving average.
All candidate selection is bounded to 2022-2024; this script must not compute
or inspect confirmation-period returns for 2025-2026.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v15_benchmark_core_development as v15
from src.conf import NASDAQ_INDEX_FILE
from src.research.can_slim import replay_can_slim_target_schedule
from src.research.data_quality import back_adjust_common_splits
from src.research.panel_data import load_panel


START = v15.START
DEVELOPMENT_END = v15.DEVELOPMENT_END
CORE_TICKER = v15.CORE_TICKER
LOOKBACK_CANDIDATES = (20, 50, 100, 200)
OUTPUT_DIR = Path("output/research_only/v16/trend_confirmed_qqq_development")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prior_session_trend_signal(
    qqq_close: pd.Series,
    session_index: pd.DatetimeIndex,
    *,
    lookback: int,
) -> pd.Series:
    """Map a QQQ trend signal to execution sessions without lookahead."""
    if lookback <= 1:
        raise ValueError("lookback must exceed one session")
    close = qqq_close.astype(float).copy()
    close.index = pd.to_datetime(close.index, errors="raise").normalize()
    close = close.sort_index()
    if close.index.duplicated().any():
        raise ValueError("QQQ close index contains duplicate dates")
    observed = close.gt(close.rolling(lookback, min_periods=lookback).mean())
    prior_session = observed.shift(1).reindex(session_index).ffill()
    return prior_session.fillna(False).astype(bool)


def trend_confirmed_target_schedule(
    targets: pd.DataFrame,
    qqq_close: pd.Series,
    session_index: pd.DatetimeIndex,
    *,
    lookback: int,
    end: str | pd.Timestamp = DEVELOPMENT_END,
) -> pd.DataFrame:
    """Keep v14 stocks and activate QQQ only inside v14 cash intervals."""
    required = {
        "effective_date",
        "ticker",
        "target_weight",
        "base_transaction_cost_bps",
    }
    missing = required - set(targets.columns)
    if missing:
        raise ValueError(f"v14 target columns missing: {sorted(missing)}")
    sessions = pd.DatetimeIndex(session_index).normalize().sort_values().unique()
    sessions = sessions[sessions <= pd.Timestamp(end)]
    frame = targets.copy()
    frame["effective_date"] = pd.to_datetime(
        frame["effective_date"], errors="raise"
    ).dt.normalize()
    frame = frame.loc[frame["effective_date"].le(pd.Timestamp(end))]
    effective_dates = pd.DatetimeIndex(
        sorted(frame["effective_date"].unique())
    )
    if effective_dates.empty:
        raise ValueError("v14 target schedule is empty in development")
    missing_sessions = effective_dates.difference(sessions)
    if len(missing_sessions):
        raise ValueError(
            "v14 effective dates are not replay sessions: "
            f"{missing_sessions.strftime('%Y-%m-%d').tolist()[:5]}"
        )
    signal = _prior_session_trend_signal(
        qqq_close, sessions, lookback=lookback
    )

    records: list[dict] = []
    last_target: dict[str, float] | None = None

    def emit(
        effective_date: pd.Timestamp,
        target: dict[str, float],
        cost_bps: float,
    ) -> None:
        nonlocal last_target
        normalized = {
            str(ticker): float(weight)
            for ticker, weight in sorted(target.items())
            if float(weight) > 1e-12
        }
        if normalized == last_target:
            return
        if normalized:
            records.extend({
                "effective_date": effective_date,
                "ticker": ticker,
                "target_weight": weight,
                "base_transaction_cost_bps": float(cost_bps),
            } for ticker, weight in normalized.items())
        else:
            records.append({
                "effective_date": effective_date,
                "ticker": "__CASH__",
                "target_weight": 0.0,
                "base_transaction_cost_bps": float(cost_bps),
            })
        last_target = normalized

    grouped = {
        pd.Timestamp(date): group
        for date, group in frame.groupby("effective_date", sort=True)
    }
    for offset, effective_date in enumerate(effective_dates):
        group = grouped[pd.Timestamp(effective_date)]
        costs = group["base_transaction_cost_bps"].astype(float).unique()
        if len(costs) != 1:
            raise ValueError(
                f"v14 target costs changed on {effective_date.date()}"
            )
        stocks = group.loc[~group["ticker"].eq("__CASH__")].copy()
        stock_target = dict(zip(
            stocks["ticker"].astype(str),
            stocks["target_weight"].astype(float),
        ))
        if sum(stock_target.values()) > 1.0 + 1e-9:
            raise ValueError(
                f"v14 stock target weight invalid on {effective_date.date()}"
            )
        if stock_target:
            emit(effective_date, stock_target, float(costs[0]))
            continue

        next_date = (
            effective_dates[offset + 1]
            if offset + 1 < len(effective_dates)
            else pd.Timestamp(end) + pd.Timedelta(days=1)
        )
        interval = sessions[
            (sessions >= effective_date) & (sessions < next_date)
        ]
        previous_state: bool | None = None
        for date in interval:
            state = bool(signal.loc[date])
            if state == previous_state:
                continue
            emit(
                pd.Timestamp(date),
                {CORE_TICKER: 1.0} if state else {},
                float(costs[0]),
            )
            previous_state = state

    result = pd.DataFrame(records, columns=[
        "effective_date",
        "ticker",
        "target_weight",
        "base_transaction_cost_bps",
    ])
    if result["effective_date"].max() > pd.Timestamp(end):
        raise RuntimeError("trend target crossed development boundary")
    return result


def _candidate_score(summary: dict) -> tuple[float, float]:
    annual = summary["costs"]["10"]["annual"]
    minimum_annual_excess = min(
        float(row["excess_vs_nasdaq"]) for row in annual
    )
    compounded_excess = float(summary["costs"]["10"]["compounded_excess"])
    return minimum_annual_excess, compounded_excess


def select_candidate(candidate_summaries: dict[int, dict]) -> int | None:
    """Select only among full-gate passers using a fixed maximin rule."""
    eligible = [
        lookback
        for lookback, summary in candidate_summaries.items()
        if summary["all_development_gates_passed"]
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda lookback: (
            *_candidate_score(candidate_summaries[lookback]),
            lookback,
        ),
    )


def run(output_dir: Path = OUTPUT_DIR) -> dict:
    bindings = {
        "v14_protocol": v15._verify_binding("v14_protocol", v15.V14_PROTOCOL),
        "v14_result": v15._verify_binding("v14_result", v15.V14_RESULT),
        "v14_targets": v15._verify_binding("v14_targets", v15.V14_TARGETS),
        "v14_daily": v15._verify_binding("v14_daily", v15.V14_DAILY),
        "qqq_history": v15._verify_binding("qqq_history", v15.QQQ_HISTORY),
        "qqq_provenance": v15._verify_binding(
            "qqq_provenance", v15.QQQ_PROVENANCE
        ),
    }
    protocol = json.loads(
        v15.V14_PROTOCOL["path"].read_text(encoding="utf-8")
    )
    if protocol["data_split"]["development_validation"]["end"] != (
        DEVELOPMENT_END
    ):
        raise RuntimeError("v14 development boundary changed")
    price_binding = protocol["input_bindings"]["price_directory"]
    targets = pd.read_csv(
        v15.V14_TARGETS["path"], parse_dates=["effective_date"]
    )
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
    v14_daily = pd.read_csv(
        v15.V14_DAILY["path"], index_col="date", parse_dates=True
    )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_paths: dict[str, Path] = {}
    candidate_summaries: dict[int, dict] = {}
    for lookback in LOOKBACK_CANDIDATES:
        schedule = trend_confirmed_target_schedule(
            targets,
            qqq["close"],
            prices.index,
            lookback=lookback,
        )
        target_path = output_dir / f"targets_sma_{lookback}.csv"
        schedule.to_csv(target_path, index=False)
        output_paths[f"targets_sma_{lookback}"] = target_path
        cost_results = {}
        for cost in (10, 30, 50):
            stressed = schedule.copy()
            stressed["base_transaction_cost_bps"] = float(cost)
            result, _ = replay_can_slim_target_schedule(
                prices,
                nasdaq,
                stressed,
                START,
                DEVELOPMENT_END,
                adjust_splits=False,
            )
            cost_results[cost] = result
            daily_path = output_dir / f"daily_sma_{lookback}_{cost}bps.csv"
            result.to_csv(daily_path, index_label="date")
            output_paths[f"daily_sma_{lookback}_{cost}bps"] = daily_path
        candidate_summaries[lookback] = v15.summarize_development(
            cost_results, v14_daily
        )

    selected = select_candidate(candidate_summaries)
    all_passed = selected is not None
    report = {
        "schema_version": 1,
        "research_only": True,
        "hypothesis": "V16_TREND_CONFIRMED_QQQ_CASH_FILL",
        "stage": "DEVELOPMENT_ONLY",
        "development_period": {"start": START, "end": DEVELOPMENT_END},
        "confirmation_period_computed": False,
        "confirmation_results_inspected": False,
        "historical_selection_contaminated": True,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "formal_universe_modified": False,
        "stock_target_policy": "preserve every frozen v14 stock target",
        "core_policy": (
            "only while v14 is in cash, hold QQQ when the prior-session "
            "close is above its simple moving average"
        ),
        "lookback_candidates": list(LOOKBACK_CANDIDATES),
        "candidate_selection_rule": (
            "full development gates must pass; then maximize the minimum "
            "10bps annual excess, compounded excess, and lookback in order"
        ),
        "development_gates": v15.DEVELOPMENT_GATES,
        "candidate_results": {
            str(lookback): {
                "score": list(_candidate_score(summary)),
                "summary": summary,
            }
            for lookback, summary in candidate_summaries.items()
        },
        "selected_lookback": selected,
        "all_development_gates_passed": all_passed,
        "input_bindings": {**bindings, "price_directory": price_binding},
        "outputs": {
            name: {"path": str(path), "sha256": _sha256(path)}
            for name, path in output_paths.items()
        },
        "interpretation_guardrail": (
            "This development run cannot repair v14 or v15 and cannot justify "
            "release. It does not compute v16 returns after 2024-12-31."
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
        "selected_lookback": report["selected_lookback"],
        "all_development_gates_passed": report[
            "all_development_gates_passed"
        ],
        "confirmation_period_computed": report[
            "confirmation_period_computed"
        ],
        "release_status": report["release_status"],
        "manifest": report["manifest"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
