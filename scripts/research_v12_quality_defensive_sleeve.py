#!/usr/bin/env python3
"""Point-in-time quality/low-volatility sleeve and v8 diversification test."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE
from src.io.security_identity import normalize_point_in_time_tickers
from src.research.data_quality import stock_returns_with_delisting_penalty
from src.research.panel_data import load_panel
from src.research.universe_history import load_universe_snapshots, universe_as_of
from src.strategy.common import scheduled_signal_dates, next_trading_date
from src.research.data_quality import back_adjust_common_splits


ANNUAL_PATH = Path(
    "output/data_provenance/companyfacts_proven_only_manifest-"
    "6c8a87fcc71cfcd5-recipe-6f0998be-q1-fp-guard-bank-duration-v3/annual.csv"
)
PRICE_DIR = Path(CLEANED_PRICE_DATA_DIR)
NASDAQ_PATH = Path(NASDAQ_INDEX_FILE)
QQQ_PATH = Path("output/research_only/qqq_nasdaq_history.csv")
V8_PATH = Path("output/research_v8_monthly_risk_budget_blend_50bps_daily.csv")
PREFIX = Path("output/research_v12_quality_defensive_sleeve")
START = "2022-01-01"
END = "2026-07-17"
TOP_N = 20
MAXIMUM_FINANCIAL_AGE_DAYS = 550
MINIMUM_PRICE = 10.0
MINIMUM_DOLLAR_VOLUME = 10_000_000.0
TRANSACTION_COST_BPS = 50.0


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


V7 = _load("research_v7_v12", "scripts/research_v7_qqq_targeted_core_satellite.py")
V8 = _load("research_v8_v12", "scripts/research_v8_monthly_risk_budget_blend.py")
ROBUST = _load("research_v8_robust_v12", "scripts/research_v8_short_horizon_robustness.py")


def load_annual(path: Path = ANNUAL_PATH) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame["ticker"] = frame["ticker"].astype(str).str.upper()
    frame = frame.rename(columns={"fiscal_end": "period_end"})
    frame = normalize_point_in_time_tickers(frame).rename(columns={"period_end": "fiscal_end"})
    frame["fiscal_end"] = pd.to_datetime(frame["fiscal_end"], errors="coerce")
    frame["available_date"] = pd.to_datetime(frame["available_date"], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    return frame.dropna(subset=["ticker", "fiscal_end", "available_date", "metric", "value"])


def quality_snapshot(
    annual: pd.DataFrame,
    as_of: pd.Timestamp,
    *,
    maximum_age_days: int = MAXIMUM_FINANCIAL_AGE_DAYS,
) -> pd.DataFrame:
    known = annual.loc[annual["available_date"].le(as_of)].copy()
    latest_facts = known.sort_values("available_date").drop_duplicates(
        ["ticker", "fiscal_end", "metric"], keep="last"
    )
    wide = latest_facts.pivot_table(
        index=["ticker", "fiscal_end"], columns="metric", values="value", aggfunc="last"
    ).reset_index()
    required = {"net_income", "operating_cash_flow", "assets", "equity"}
    if not required.issubset(wide.columns):
        return pd.DataFrame()
    availability = latest_facts.groupby(["ticker", "fiscal_end"])["available_date"].max()
    key = pd.MultiIndex.from_frame(wide[["ticker", "fiscal_end"]])
    wide["available_date"] = availability.reindex(key).to_numpy()
    latest = wide.sort_values(["ticker", "fiscal_end"]).groupby("ticker").tail(1).set_index("ticker")
    latest["financial_age_days"] = (as_of - latest["available_date"]).dt.days
    latest = latest.loc[latest["financial_age_days"].between(0, maximum_age_days)]
    positive_assets = latest["assets"].where(latest["assets"].gt(0))
    latest["roa"] = latest["net_income"] / positive_assets
    latest["cash_return_on_assets"] = latest["operating_cash_flow"] / positive_assets
    latest["equity_to_assets"] = latest["equity"] / positive_assets
    latest["cash_earnings_spread"] = (
        latest["operating_cash_flow"] - latest["net_income"]
    ) / positive_assets
    if {"gross_profit", "revenue"}.issubset(latest.columns):
        latest["gross_margin"] = latest["gross_profit"] / latest["revenue"].where(latest["revenue"].gt(0))
    else:
        latest["gross_margin"] = np.nan
    return latest.replace([np.inf, -np.inf], np.nan)


def select_quality_portfolio(
    snapshot: pd.DataFrame,
    close: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    signal_date: pd.Timestamp,
    symbols: set[str],
    *,
    top_n: int = TOP_N,
) -> pd.DataFrame:
    eligible = snapshot.index.intersection(close.columns).intersection(pd.Index(symbols))
    if len(eligible) == 0:
        return pd.DataFrame()
    prices = close.loc[:signal_date, eligible]
    current = prices.iloc[-1]
    liquidity = dollar_volume.loc[:signal_date, eligible].tail(50).median()
    volatility = prices.pct_change(fill_method=None).tail(126).std().mul(np.sqrt(252))
    momentum_126d = prices.iloc[-1].div(prices.iloc[-127]).sub(1.0) if len(prices) >= 127 else pd.Series(index=eligible, dtype=float)
    frame = snapshot.reindex(eligible).copy()
    frame["price"] = current
    frame["median_dollar_volume_50d"] = liquidity
    frame["volatility_126d"] = volatility
    frame["momentum_126d"] = momentum_126d
    frame = frame.loc[
        frame["price"].ge(MINIMUM_PRICE)
        & frame["median_dollar_volume_50d"].ge(MINIMUM_DOLLAR_VOLUME)
        & frame["volatility_126d"].notna()
        & frame["momentum_126d"].notna()
        & frame["roa"].notna()
        & frame["cash_return_on_assets"].notna()
        & frame["equity_to_assets"].gt(0)
        & frame["cash_earnings_spread"].notna()
    ]
    if frame.empty:
        return frame
    factors = pd.DataFrame(index=frame.index)
    for column in ("roa", "cash_return_on_assets", "equity_to_assets", "cash_earnings_spread"):
        factors[column] = frame[column].rank(pct=True)
    gross = frame["gross_margin"]
    factors["gross_margin"] = gross.rank(pct=True).fillna(0.5)
    factors["low_volatility"] = (-frame["volatility_126d"]).rank(pct=True)
    factors["momentum_126d"] = frame["momentum_126d"].rank(pct=True)
    frame["quality_score"] = factors.mean(axis=1)
    selected = frame.sort_values(
        ["quality_score", "median_dollar_volume_50d"], ascending=False
    ).head(top_n).copy()
    selected["target_weight"] = 1.0 / len(selected)
    return selected


def simulate_quality_sleeve(
    close: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    nasdaq: pd.Series,
    annual: pd.DataFrame,
    *,
    start: str = START,
    end: str = END,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    prices = back_adjust_common_splits(close).sort_index()
    returns = stock_returns_with_delisting_penalty(prices).fillna(0.0)
    snapshots = load_universe_snapshots()
    targets = {}
    target_rows = []
    for signal_date in scheduled_signal_dates(prices.index, pd.Timestamp(start) - pd.Timedelta(days=62), end, "monthly"):
        effective = next_trading_date(prices.index, signal_date)
        if effective is None or effective < pd.Timestamp(start) or effective > pd.Timestamp(end):
            continue
        universe = universe_as_of(snapshots, signal_date)
        if universe is None:
            continue
        selected = select_quality_portfolio(
            quality_snapshot(annual, signal_date), close, dollar_volume,
            signal_date, universe,
        )
        target = pd.Series(0.0, index=prices.columns)
        if not selected.empty:
            target.loc[selected.index] = selected["target_weight"]
            for ticker, row in selected.iterrows():
                target_rows.append({
                    "signal_date": signal_date,
                    "effective_date": effective,
                    "ticker": ticker,
                    "target_weight": row["target_weight"],
                    "quality_score": row["quality_score"],
                    "financial_age_days": row["financial_age_days"],
                })
        targets[effective] = target
    position = pd.Series(0.0, index=prices.columns)
    cash = nav = 1.0
    rows = []
    for stamp, daily in returns.iterrows():
        previous = nav
        position = position.mul(1.0 + daily)
        pre_trade = float(cash + position.sum())
        turnover = 0.0
        if stamp in targets:
            target = targets[stamp]
            cost_rate = TRANSACTION_COST_BPS / 10_000.0
            post_trade = pre_trade
            for _ in range(20):
                desired = target * post_trade
                traded = float((desired - position).abs().sum())
                updated = pre_trade - traded * cost_rate
                if abs(updated - post_trade) < 1e-12:
                    break
                post_trade = updated
            desired = target * post_trade
            turnover = float((desired - position).abs().sum() / pre_trade)
            cost = float((desired - position).abs().sum() * cost_rate)
            cash = pre_trade - float(desired.sum()) - cost
            position = desired
        nav = float(cash + position.sum())
        rows.append({
            "date": stamp, "return": nav / previous - 1.0,
            "benchmark_return": float(nasdaq.reindex(prices.index).ffill().pct_change(fill_method=None).loc[stamp]) if stamp != prices.index[0] else 0.0,
            "turnover": turnover, "nav": nav,
        })
    result = pd.DataFrame(rows).set_index("date").loc[start:end]
    return result, pd.DataFrame(target_rows)


def run() -> dict:
    close, dollar_volume = load_panel(PRICE_DIR, "2018-01-01", END)
    nasdaq = pd.read_csv(NASDAQ_PATH, parse_dates=["date"]).set_index("date")["close"]
    quality, targets = simulate_quality_sleeve(close, dollar_volume, nasdaq, load_annual())
    qqq = pd.read_csv(QQQ_PATH, parse_dates=["date"]).set_index("date")
    dividends = qqq.get("cash_dividend", pd.Series(0.0, index=qqq.index))
    quality["qqq_return"] = qqq["close"].add(dividends).div(qqq["close"].shift(1)).sub(1.0).reindex(quality.index).fillna(0.0)
    quality["drawdown"] = quality["nav"].div(quality["nav"].cummax()).sub(1.0)
    v8 = pd.read_csv(V8_PATH, parse_dates=["date"]).set_index("date")
    variants = {}
    primary = None
    for weight in (0.10, 0.20, 0.30):
        combined = V8.combine_monthly_sleeves(
            v8, quality, v7_weight=weight, transfer_cost_bps=50.0
        )
        relative = ROBUST.relative_returns(combined["return"], combined["qqq_return"])
        summary = V7.summarize(combined)
        summary["13_week_positive_fraction"] = ROBUST.rolling_summary(relative, 65)["positive_fraction"]
        summary["26_week_positive_fraction"] = ROBUST.rolling_summary(relative, 130)["positive_fraction"]
        variants[f"quality_weight_{weight:.2f}"] = summary
        if weight == 0.20:
            primary = combined
    assert primary is not None
    quality.to_csv(PREFIX.with_name(PREFIX.name + "_sleeve_50bps_daily.csv"), index_label="date")
    targets.to_csv(PREFIX.with_name(PREFIX.name + "_targets.csv"), index=False)
    primary.to_csv(PREFIX.with_name(PREFIX.name + "_20pct_50bps_daily.csv"), index_label="date")
    payload = {
        "model_version": "quality-defensive-v12-research",
        "research_only": True,
        "historical_selection_contaminated": True,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "configuration": {
            "top_n": TOP_N,
            "maximum_financial_age_days": MAXIMUM_FINANCIAL_AGE_DAYS,
            "minimum_price": MINIMUM_PRICE,
            "minimum_median_dollar_volume_50d": MINIMUM_DOLLAR_VOLUME,
            "transaction_cost_bps": TRANSACTION_COST_BPS,
            "signal_frequency": "monthly",
            "factors": ["roa", "cash_return_on_assets", "equity_to_assets", "cash_earnings_spread", "gross_margin", "low_volatility_126d", "momentum_126d"],
        },
        "quality_sleeve": V7.summarize(quality),
        "primary": variants["quality_weight_0.20"],
        "blend_sensitivity": variants,
        "target_panel": {
            "rows": len(targets),
            "periods": int(targets["effective_date"].nunique()),
            "tickers": int(targets["ticker"].nunique()),
        },
        "selection_warning": "The quality sleeve was designed after inspecting v8 weaknesses.",
    }
    PREFIX.with_name(PREFIX.name + "_summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
