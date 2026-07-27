"""Evaluate recorded monthly recommendation portfolios without backdating evidence."""

from __future__ import annotations

import argparse
import json
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE
from src.research.data_quality import stock_returns_with_delisting_penalty


NEW_YORK = ZoneInfo("America/New_York")


def execution_close_utc(execution_date: pd.Timestamp) -> pd.Timestamp:
    """Return the regular Nasdaq close for an execution date in UTC."""
    local_date = pd.Timestamp(execution_date).date()
    local_close = pd.Timestamp.combine(local_date, time(16, 0)).tz_localize(NEW_YORK)
    return local_close.tz_convert("UTC")


def evaluate_recorded_portfolio(
    records: pd.DataFrame,
    close: pd.DataFrame,
    benchmark_close: pd.Series,
    transaction_cost_bps: float = 10.0,
    evaluation_end: pd.Timestamp | None = None,
) -> dict:
    if records.empty:
        raise ValueError("No recommendation records")
    signal_date = pd.Timestamp(records["signal_date"].iloc[0])
    execution_raw = records["execution_date"].iloc[0]
    if pd.isna(execution_raw) or str(execution_raw).strip() == "":
        return {
            "signal_date": signal_date.strftime("%Y-%m-%d"),
            "execution_date": None,
            "status": "PENDING_EXECUTION",
            "forward_eligible": True,
            "forward_sessions": 0,
        }
    execution_date = pd.Timestamp(execution_raw)
    portfolio_time_column = (
        "portfolio_generated_at"
        if "portfolio_generated_at" in records.columns
        else "generated_at"
    )
    generated_at = pd.to_datetime(records[portfolio_time_column], utc=True).min()
    close_cutoff = execution_close_utc(execution_date)
    # A recommendation first recorded at or after the execution close could not
    # have been acted on at that close and is retrospective evidence.
    forward_eligible = generated_at < close_cutoff
    tickers = [
        ticker for ticker in records["ticker"].astype(str).str.upper()
        if ticker != "CASH"
    ]
    weights = records.set_index(records["ticker"].astype(str).str.upper())["target_weight"].astype(float)
    available_tickers = [ticker for ticker in tickers if ticker in close.columns]
    if set(available_tickers) != set(tickers):
        missing = sorted(set(tickers) - set(available_tickers))
        raise ValueError(f"Missing shadow prices: {missing}")
    common_index = close.index.intersection(benchmark_close.index).sort_values()
    evaluation_index = common_index[common_index >= execution_date]
    if evaluation_end is not None:
        evaluation_index = evaluation_index[evaluation_index <= evaluation_end]
    if not len(evaluation_index) or evaluation_index[0] != execution_date:
        raise ValueError(f"Execution close {execution_date.date()} is unavailable")
    if tickers:
        panel = close.loc[evaluation_index, tickers]
        returns = stock_returns_with_delisting_penalty(panel).iloc[1:].fillna(0.0)
        portfolio_returns = returns.mul(
            weights.reindex(tickers).to_numpy(), axis=1
        ).sum(axis=1)
    else:
        portfolio_returns = pd.Series(0.0, index=evaluation_index[1:])
    cost = weights.abs().sum() * transaction_cost_bps / 10_000
    strategy_return = (1 - cost) * (1 + portfolio_returns).prod() - 1
    benchmark_window = benchmark_close.loc[evaluation_index]
    benchmark_return = benchmark_window.iloc[-1] / benchmark_window.iloc[0] - 1
    sessions = len(portfolio_returns)
    return {
        "signal_date": signal_date.strftime("%Y-%m-%d"),
        "execution_date": execution_date.strftime("%Y-%m-%d"),
        "evaluation_end": evaluation_index[-1].strftime("%Y-%m-%d"),
        "generated_at": generated_at.isoformat(),
        "portfolio_generated_at": generated_at.isoformat(),
        "execution_close_utc": close_cutoff.isoformat(),
        "status": "FORWARD" if forward_eligible else "RETROSPECTIVE_SEED",
        "forward_eligible": forward_eligible,
        "forward_sessions": sessions if forward_eligible else 0,
        "observed_sessions": sessions,
        "strategy_return": float(strategy_return),
        "benchmark_return": float(benchmark_return),
        "excess_return": float(strategy_return - benchmark_return),
        "target_exposure": float(weights.sum()),
        "transaction_cost_bps": transaction_cost_bps,
    }


def evaluate_history(
    history_file: str | Path,
    output_file: str | Path,
    transaction_cost_bps: float = 10.0,
) -> dict:
    history_path = Path(history_file)
    if not history_path.exists() or history_path.stat().st_size == 0:
        result = {
            "status": "NO_RECORDED_POSITIONS",
            "recorded_periods": 0,
            "forward_periods": 0,
            "forward_sessions": 0,
            "forward_strategy_return": 0.0,
            "forward_benchmark_return": 0.0,
        }
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        Path(output_file).write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        return result
    history = pd.read_csv(history_path)
    required = {"signal_date", "execution_date", "generated_at", "ticker", "target_weight"}
    if not required.issubset(history.columns):
        missing = sorted(required - set(history.columns))
        raise ValueError(f"History lacks forward-audit columns: {missing}")
    history = history.loc[history["signal_date"].notna()].copy()
    history["signal_date"] = pd.to_datetime(history["signal_date"])
    signal_dates = sorted(history["signal_date"].unique())
    if not signal_dates:
        raise ValueError("History has no recorded signal date")
    tickers = [
        ticker
        for ticker in history["ticker"].astype(str).str.lower().unique()
        if ticker != "cash"
    ]
    benchmark = pd.read_csv(NASDAQ_INDEX_FILE, index_col="date", parse_dates=True)["close"].sort_index()
    close = pd.DataFrame({
        ticker.upper(): pd.read_csv(
            Path(CLEANED_PRICE_DATA_DIR) / f"{ticker}.csv",
            index_col="date",
            parse_dates=True,
        )["close"]
        for ticker in tickers
    }).sort_index()
    if not tickers:
        close = pd.DataFrame(index=benchmark.index)
    periods = []
    for index, signal_date in enumerate(signal_dates):
        all_records = history.loc[history["signal_date"] == signal_date].copy()
        generated = pd.to_datetime(all_records["generated_at"], utc=True)
        first_generation = generated.min()
        records = all_records.loc[generated == first_generation].copy()
        known_executions = pd.to_datetime(
            all_records["execution_date"], errors="coerce"
        ).dropna()
        if len(known_executions):
            records["execution_date"] = known_executions.min().strftime("%Y-%m-%d")
        period_end = None
        if index + 1 < len(signal_dates):
            next_records = history.loc[history["signal_date"] == signal_dates[index + 1]]
            next_executions = pd.to_datetime(
                next_records["execution_date"], errors="coerce"
            ).dropna()
            if len(next_executions):
                candidates = benchmark.index[benchmark.index < next_executions.min()]
                period_end = candidates[-1] if len(candidates) else None
        periods.append(
            evaluate_recorded_portfolio(
                records, close, benchmark, transaction_cost_bps, period_end
            )
        )
    model_version = str(history["model_version"].iloc[0])
    forward_periods = [period for period in periods if period.get("forward_eligible")]
    result = {
        "model_version": model_version,
        "recorded_periods": len(periods),
        "forward_periods": len(forward_periods),
        "forward_sessions": sum(period.get("forward_sessions", 0) for period in forward_periods),
        "forward_strategy_return": (
            float(pd.Series([1 + period["strategy_return"] for period in forward_periods]).prod() - 1)
            if forward_periods else None
        ),
        "forward_benchmark_return": (
            float(pd.Series([1 + period["benchmark_return"] for period in forward_periods]).prod() - 1)
            if forward_periods else None
        ),
        "periods": periods,
    }
    output = Path(output_file)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    args = parser.parse_args()
    print(json.dumps(evaluate_history(args.history, args.output, args.cost_bps), indent=2))


if __name__ == "__main__":
    main()
