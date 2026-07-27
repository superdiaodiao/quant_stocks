"""Hard data-readiness checks for backtests and daily recommendations."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd

from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_300M_STOCK_LIST_FILE,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_EPS_FILE,
    PROJECT_PATH,
)
from src.io.financial_update import audit_financial_coverage, investable_common_equities


def audit_project_data(
    as_of: date,
    minimum_price_coverage: float = 0.95,
    minimum_financial_coverage: float = 0.90,
    maximum_market_age_days: int = 5,
) -> dict:
    universe = investable_common_equities(pd.read_csv(NASDAQ_300M_STOCK_LIST_FILE))
    symbols = universe["Symbol"].dropna().astype(str).str.upper().tolist()
    index = pd.read_csv(NASDAQ_INDEX_FILE, usecols=["date"], parse_dates=["date"])
    benchmark_date = index["date"].max()
    price_dates, missing_price_files, future_price_rows = {}, [], []
    for ticker in symbols:
        path = Path(CLEANED_PRICE_DATA_DIR) / f"{ticker.lower()}.csv"
        if not path.exists():
            missing_price_files.append(ticker)
            continue
        dates = pd.read_csv(path, usecols=["date"], parse_dates=["date"])["date"]
        if dates.empty:
            missing_price_files.append(ticker)
            continue
        price_dates[ticker] = dates.max()
        if dates.max() > pd.Timestamp(as_of):
            future_price_rows.append(ticker)
    current_prices = {
        ticker for ticker, latest in price_dates.items() if latest >= benchmark_date
    }
    price_coverage = len(current_prices) / max(len(symbols), 1)

    eps = pd.read_csv(
        POINT_IN_TIME_EPS_FILE,
        parse_dates=["period_end", "available_date", "fetched_at"],
    )
    financial = audit_financial_coverage(eps, symbols, as_of)
    benchmark_age_days = (pd.Timestamp(as_of) - benchmark_date).days
    checks = {
        "benchmark_not_future": bool(benchmark_date <= pd.Timestamp(as_of)),
        "benchmark_fresh": 0 <= benchmark_age_days <= maximum_market_age_days,
        "price_coverage": price_coverage >= minimum_price_coverage,
        "financial_coverage": financial["fresh_coverage"] >= minimum_financial_coverage,
        "no_future_price_rows": not future_price_rows,
        "point_in_time_columns_present": {
            "period_end", "available_date", "quarterly_eps", "source"
        }.issubset(eps.columns),
    }
    report = {
        "as_of": as_of.isoformat(),
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "common_equity_universe": len(symbols),
        "benchmark_latest_date": benchmark_date.strftime("%Y-%m-%d"),
        "benchmark_age_days": benchmark_age_days,
        "maximum_market_age_days": maximum_market_age_days,
        "current_price_files": len(current_prices),
        "price_coverage": price_coverage,
        "minimum_price_coverage": minimum_price_coverage,
        "missing_price_files": missing_price_files,
        "future_price_rows": future_price_rows,
        "fresh_financial_tickers": financial["fresh_tickers"],
        "financial_coverage": financial["fresh_coverage"],
        "minimum_financial_coverage": minimum_financial_coverage,
        "missing_or_stale_financials": financial["missing_or_stale"],
    }
    output = Path(PROJECT_PATH) / "output/data_audit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def require_project_data(as_of: date) -> dict:
    report = audit_project_data(as_of)
    if report["status"] != "PASS":
        failed = [name for name, passed in report["checks"].items() if not passed]
        raise RuntimeError(f"Data readiness failed: {', '.join(failed)}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=date.today().isoformat())
    args = parser.parse_args()
    result = audit_project_data(date.fromisoformat(args.as_of))
    compact = {key: value for key, value in result.items() if not isinstance(value, list)}
    compact["missing_price_files_count"] = len(result["missing_price_files"])
    compact["missing_or_stale_financials_count"] = len(result["missing_or_stale_financials"])
    print(json.dumps(compact, indent=2))
    if result["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
