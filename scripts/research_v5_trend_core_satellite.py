#!/usr/bin/env python3
"""Evaluate a tradable relative-strength trend-core v5 research candidate.

At each completed month end, compare the trailing 63-session return of frozen
v4 Candidate 15 with QQQ.  If v4 leads, the next month remains 100% v4.  If v4
lags and QQQ is above its 200-session moving average, the next month is 50% v4
and 50% QQQ.  If v4 lags while QQQ is below trend, the other 50% stays in cash.
This is historical research, not independent forward evidence or an order
generator.
"""

from __future__ import annotations

import argparse
from datetime import date
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from src.io.nasdaq_update import fetch_history


MODEL_VERSION = "can-slim-v5-qqq-relative-trend-core-research"
DEFAULT_V4_DAILY = Path(
    "output/research_v4_cost_robust_top10_proven_only_bank_v3_daily.csv"
)
DEFAULT_V4_SUMMARY = Path("output/research_v4_cost_robust_top10_shadow_summary.json")
DEFAULT_CORE_PRICE = Path("output/research_only/qqq_nasdaq_history.csv")
DEFAULT_OUTPUT_PREFIX = Path("output/research_v5_qqq_relative_trend_core")
SATELLITE_WEIGHT = 0.5
CORE_WEIGHT = 0.5
TREND_WINDOW = 200
RELATIVE_STRENGTH_WINDOW = 63
EVALUATION_START = pd.Timestamp("2022-01-01")
NASDAQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _nasdaq_dividend_history(
) -> tuple[bytes, pd.DataFrame]:
    url = (
        "https://api.nasdaq.com/api/quote/QQQ/dividends"
        "?assetclass=etf&limit=500"
    )
    with urlopen(Request(url, headers=NASDAQ_HEADERS), timeout=30) as response:
        payload = response.read()
    data = json.loads(payload.decode("utf-8")).get("data") or {}
    rows = ((data.get("dividends") or {}).get("rows") or [])
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise ValueError("Nasdaq returned no QQQ dividend history")
    frame = pd.DataFrame({
        "date": pd.to_datetime(frame["exOrEffDate"], errors="coerce"),
        "cash_dividend": pd.to_numeric(
            frame["amount"].astype(str).str.replace("$", "", regex=False),
            errors="coerce",
        ),
    }).dropna().drop_duplicates("date")
    if frame.empty or frame["cash_dividend"].le(0).any():
        raise ValueError("Nasdaq returned invalid QQQ cash dividends")
    return payload, frame.sort_values("date")


def refresh_core_price(path: str | Path) -> Path:
    output = Path(path)
    frame = fetch_history(
        "QQQ", date(2018, 1, 1), date.today(), asset_class="etf", retries=2
    )
    required = ["date", "open", "high", "low", "close", "volume"]
    frame = frame[required].sort_values("date").drop_duplicates("date")
    if frame.empty or frame["close"].le(0).any():
        raise ValueError("QQQ history has no valid positive closes")
    dividend_payload, dividends = _nasdaq_dividend_history()
    frame = frame.merge(dividends, on="date", how="left", validate="one_to_one")
    frame["cash_dividend"] = frame["cash_dividend"].fillna(0.0)
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output, index=False)
    provenance = {
        "schema_version": 1,
        "ticker": "QQQ",
        "nasdaq_source": "Nasdaq public historical API",
        "dividend_source": "Nasdaq public dividend API",
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "dividend_payload_sha256": hashlib.sha256(dividend_payload).hexdigest(),
        "dividend_rows": int(len(dividends)),
        "minimum_dividend_date": dividends["date"].min().strftime("%Y-%m-%d"),
        "maximum_dividend_date": dividends["date"].max().strftime("%Y-%m-%d"),
        "return_series": "Nasdaq close plus cash dividend on ex-date",
        "signal_series": "Nasdaq unadjusted close",
    }
    output.with_suffix(".provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    return output


def _prior_month_allocation(
    v4_return: pd.Series,
    core_close: pd.Series,
    index: pd.DatetimeIndex,
) -> pd.DataFrame:
    close = core_close.sort_index().reindex(index).ffill()
    moving_average = close.rolling(TREND_WINDOW, min_periods=TREND_WINDOW).mean()
    v4_trailing = (
        (1.0 + v4_return).rolling(RELATIVE_STRENGTH_WINDOW).apply(
            np.prod, raw=True
        )
        - 1.0
    )
    core_trailing = close.div(close.shift(RELATIVE_STRENGTH_WINDOW)).sub(1.0)
    signals = pd.DataFrame({
        "v4_leads": v4_trailing.ge(core_trailing),
        "core_trend_on": close.gt(moving_average),
    }).groupby([close.index.year, close.index.month]).last()
    ordered = list(signals.index)
    prior = {}
    for position, key in enumerate(ordered):
        signal = (
            signals.iloc[position - 1]
            if position
            else pd.Series({"v4_leads": False, "core_trend_on": False})
        )
        v4_leads = bool(signal["v4_leads"])
        trend_on = bool(signal["core_trend_on"])
        prior[key] = {
            "satellite_weight": 1.0 if v4_leads else SATELLITE_WEIGHT,
            "core_weight": CORE_WEIGHT if (not v4_leads and trend_on) else 0.0,
        }
    return pd.DataFrame(
        [prior[(stamp.year, stamp.month)] for stamp in index], index=index
    )


def simulate_candidate(
    v4_daily: pd.DataFrame,
    core_close: pd.Series,
    *,
    transaction_cost_bps: float,
    core_dividend: pd.Series | None = None,
) -> pd.DataFrame:
    if transaction_cost_bps < 10 or not np.isfinite(transaction_cost_bps):
        raise ValueError("transaction_cost_bps must be finite and at least 10")
    frame = v4_daily.copy().sort_index()
    core_close = core_close.reindex(frame.index)
    if core_close.isna().any():
        missing = core_close.index[core_close.isna()]
        zero_return_rows = frame.loc[missing, ["strategy", "benchmark", "turnover"]]
        if not zero_return_rows.fillna(0.0).eq(0.0).all(axis=None):
            raise ValueError("QQQ history is missing a non-zero v4 session")
        # The v4 panel retains a zero-return row for some full-market holidays.
        # Carrying the prior QQQ close across those rows produces exactly zero
        # core return and is allowed only under the explicit check above.
        core_close = core_close.ffill()
    dividends = (
        pd.Series(0.0, index=frame.index)
        if core_dividend is None
        else core_dividend.reindex(frame.index).fillna(0.0)
    )
    frame["core_return"] = core_close.add(dividends).div(
        core_close.shift(1)
    ).sub(1.0).fillna(0.0)
    allocation = _prior_month_allocation(
        frame["strategy"], core_close, frame.index
    )
    frame = frame.join(allocation)
    # The v4 daily stream already includes 10 bps at its own rebalance.  Apply
    # only the incremental stress cost to its reported turnover here.
    incremental_cost = (
        frame["turnover"].fillna(0.0)
        * (transaction_cost_bps - 10.0)
        / 10_000.0
    )
    frame["satellite_return_stressed"] = frame["strategy"] - incremental_cost

    satellite = core = cash = 0.0
    nav = 1.0
    previous_month = None
    rows = []
    for stamp, row in frame.iterrows():
        current_month = (stamp.year, stamp.month)
        sleeve_turnover = 0.0
        sleeve_cost = 0.0
        # Existing sleeves first earn the current close-to-close return.  A
        # new monthly allocation is then executed at this session's close and
        # becomes exposed from the following session onward.
        if previous_month is not None:
            satellite *= 1.0 + float(row["satellite_return_stressed"])
            core *= 1.0 + float(row["core_return"])
            nav = satellite + core + cash
        if previous_month != current_month:
            target_satellite = nav * float(row["satellite_weight"])
            target_core = nav * float(row["core_weight"])
            sleeve_turnover = (
                abs(target_satellite - satellite)
                + abs(target_core - core)
            )
            sleeve_cost = sleeve_turnover * transaction_cost_bps / 10_000.0
            nav -= sleeve_cost
            satellite = nav * float(row["satellite_weight"])
            core = nav * float(row["core_weight"])
            cash = nav - satellite - core
        nav = satellite + core + cash
        rows.append({
            "date": stamp,
            "return": np.nan,
            "nav": nav,
            "satellite_weight": float(row["satellite_weight"]),
            "core_weight": float(row["core_weight"]),
            "satellite_value": satellite,
            "core_value": core,
            "cash": cash,
            "sleeve_turnover": sleeve_turnover,
            "sleeve_transaction_cost": sleeve_cost,
        })
        previous_month = current_month
    result = pd.DataFrame(rows).set_index("date")
    result["return"] = result["nav"].pct_change().fillna(result["nav"].iloc[0] - 1)
    result["benchmark_return"] = frame["benchmark"]
    result["drawdown"] = result["nav"].div(result["nav"].cummax()).sub(1.0)
    return result


def summarize(result: pd.DataFrame, transaction_cost_bps: float) -> dict:
    evaluation = result.loc[result.index >= EVALUATION_START].copy()
    annual = evaluation.groupby(evaluation.index.year).apply(
        lambda group: pd.Series({
            "strategy": float((1.0 + group["return"]).prod() - 1.0),
            "nasdaq": float((1.0 + group["benchmark_return"]).prod() - 1.0),
        }),
        include_groups=False,
    )
    annual["excess_vs_nasdaq"] = annual["strategy"] - annual["nasdaq"]
    evaluation_drawdown = evaluation["nav"].div(
        evaluation["nav"].cummax()
    ).sub(1.0)
    return {
        "transaction_cost_bps": transaction_cost_bps,
        "years": int(len(annual)),
        "wins_vs_nasdaq": int(annual["excess_vs_nasdaq"].gt(0).sum()),
        "median_excess_vs_nasdaq": float(annual["excess_vs_nasdaq"].median()),
        "minimum_excess_vs_nasdaq": float(annual["excess_vs_nasdaq"].min()),
        "maximum_drawdown": float(evaluation_drawdown.min()),
        "time_underwater_fraction": float(evaluation_drawdown.lt(0).mean()),
        "annual": annual.reset_index(names="year").to_dict(orient="records"),
    }


def run(
    *,
    v4_daily_path: str | Path,
    core_price_path: str | Path,
    output_prefix: str | Path,
    v4_summary_path: str | Path = DEFAULT_V4_SUMMARY,
) -> dict:
    v4_daily_path = Path(v4_daily_path)
    v4_summary_path = Path(v4_summary_path)
    core_price_path = Path(core_price_path)
    output_prefix = Path(output_prefix)
    v4 = pd.read_csv(v4_daily_path, parse_dates=["date"]).set_index("date")
    core_frame = pd.read_csv(core_price_path, parse_dates=["date"]).set_index("date")
    core = core_frame["close"]
    core_dividend = core_frame.get(
        "cash_dividend", pd.Series(0.0, index=core_frame.index)
    )
    v4_summary = json.loads(v4_summary_path.read_text(encoding="utf-8"))
    missing_core_dates = core.reindex(v4.index).loc[lambda values: values.isna()].index
    summaries = {}
    baseline_result = None
    for cost in (10.0, 30.0, 50.0):
        result = simulate_candidate(
            v4,
            core,
            transaction_cost_bps=cost,
            core_dividend=core_dividend,
        )
        summaries[str(int(cost))] = summarize(result, cost)
        if cost == 30.0:
            baseline_result = result
    assert baseline_result is not None
    daily_path = output_prefix.with_name(output_prefix.name + "_30bps_daily.csv")
    summary_path = output_prefix.with_name(output_prefix.name + "_summary.json")
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_result.reset_index().to_csv(daily_path, index=False)
    baseline = summaries["30"]
    v4_historical = v4_summary["historical_diagnostic"]
    v4_risk = v4_summary["risk_diagnostic"]
    provenance_path = core_price_path.with_suffix(".provenance.json")
    payload = {
        "format_version": 1,
        "research_only": True,
        "historical_selection_contaminated": True,
        "model_version": MODEL_VERSION,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "configuration": {
            "satellite_model": "can-slim-v4-cost-robust-top10-shadow",
            "satellite_weight": SATELLITE_WEIGHT,
            "core_ticker": "QQQ",
            "relative_strength_window_sessions": RELATIVE_STRENGTH_WINDOW,
            "allocation_when_v4_leads": {"v4": 1.0, "qqq": 0.0, "cash": 0.0},
            "allocation_when_v4_lags_and_qqq_trend_on": {"v4": 0.5, "qqq": 0.5, "cash": 0.0},
            "allocation_when_v4_lags_and_qqq_trend_off": {"v4": 0.5, "qqq": 0.0, "cash": 0.5},
            "core_trend_window_sessions": TREND_WINDOW,
            "signals": "prior_completed_month_end_relative_strength_and_close_above_sma",
            "rebalance_frequency": "monthly",
        },
        "evaluation_start": EVALUATION_START.strftime("%Y-%m-%d"),
        "cost_stress": summaries,
        "comparison_vs_v4_at_30bps": {
            "wins_delta": baseline["wins_vs_nasdaq"]
            - int(v4_historical["cost_stress_wins"]["30"]),
            "median_excess_delta": baseline["median_excess_vs_nasdaq"]
            - float(v4_historical["median_excess_vs_nasdaq"]),
            "minimum_excess_delta": baseline["minimum_excess_vs_nasdaq"]
            - float(v4_historical["minimum_excess_vs_nasdaq"]),
            "maximum_drawdown_delta": baseline["maximum_drawdown"]
            - float(v4_risk["maximum_drawdown"]),
            "time_underwater_fraction_delta": baseline[
                "time_underwater_fraction"
            ] - float(v4_risk["time_underwater_fraction"]),
        },
        "inputs": {
            "v4_daily": {"path": str(v4_daily_path.resolve()), "sha256": _sha256(v4_daily_path)},
            "v4_frozen_summary": {"path": str(v4_summary_path.resolve()), "sha256": _sha256(v4_summary_path)},
            "qqq_price": {
                "path": str(core_price_path.resolve()),
                "sha256": _sha256(core_price_path),
                "source": "Nasdaq public historical API",
                "return_series": "close_plus_cash_dividend_on_ex_date",
                "rows": int(len(core)),
                "minimum_date": core.index.min().strftime("%Y-%m-%d"),
                "maximum_date": core.index.max().strftime("%Y-%m-%d"),
                "missing_v4_sessions": int(core.reindex(v4.index).isna().sum()),
                "carried_zero_return_market_holidays": [
                    stamp.strftime("%Y-%m-%d") for stamp in missing_core_dates
                ],
                "provenance_path": (
                    str(provenance_path.resolve()) if provenance_path.is_file() else None
                ),
                "provenance_sha256": (
                    _sha256(provenance_path) if provenance_path.is_file() else None
                ),
            },
        },
        "daily_artifact": {"path": str(daily_path.resolve()), "sha256": _sha256(daily_path)},
        "interpretation": (
            "Historical mechanism research only. QQQ signals and closes use Nasdaq; "
            "cash dividends are included on their Nasdaq ex-dates."
        ),
    }
    summary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {**payload, "summary_path": str(summary_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v4-daily", type=Path, default=DEFAULT_V4_DAILY)
    parser.add_argument("--v4-summary", type=Path, default=DEFAULT_V4_SUMMARY)
    parser.add_argument("--core-price", type=Path, default=DEFAULT_CORE_PRICE)
    parser.add_argument("--output-prefix", type=Path, default=DEFAULT_OUTPUT_PREFIX)
    parser.add_argument("--refresh-core-price", action="store_true")
    args = parser.parse_args()
    if args.refresh_core_price or not args.core_price.is_file():
        refresh_core_price(args.core_price)
    result = run(
        v4_daily_path=args.v4_daily,
        v4_summary_path=args.v4_summary,
        core_price_path=args.core_price,
        output_prefix=args.output_prefix,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
