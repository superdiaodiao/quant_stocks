#!/usr/bin/env python3
"""Explain v8 short-window failures with information known at window start."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


V8_PATH = Path("output/research_v8_monthly_risk_budget_blend_50bps_daily.csv")
V6_PATH = Path("output/research_v6_walkforward_defensive_ensemble_50bps_daily.csv")
V7_STOCK_PATH = Path("output/research_v7_qqq_targeted_core_satellite_stock_sleeve_10bps_daily.csv")
QQQ_PATH = Path("output/research_only/qqq_nasdaq_history.csv")
OUTPUT_PATH = Path("output/research_v8_regime_diagnostic.json")


def future_compounded(relative: pd.Series, sessions: int) -> pd.Series:
    values = relative.to_numpy(dtype=float)
    result = np.full(len(values), np.nan)
    for start in range(len(values) - sessions):
        result[start] = np.prod(1.0 + values[start + 1 : start + sessions + 1]) - 1.0
    return pd.Series(result, index=relative.index)


def regime_summary(outcome: pd.Series, regime: pd.Series) -> dict:
    rows = {}
    for state in (False, True):
        values = outcome.loc[regime.eq(state)].dropna()
        rows[str(state).lower()] = {
            "windows": int(len(values)),
            "positive_fraction": float(values.gt(0).mean()),
            "median_relative_return": float(values.median()),
            "quantile_10": float(values.quantile(0.10)),
            "minimum_relative_return": float(values.min()),
        }
    return rows


def build_report(
    v8: pd.DataFrame,
    v6: pd.DataFrame,
    v7_stock: pd.DataFrame,
    qqq: pd.DataFrame,
) -> dict:
    index = v8.index
    dividend = qqq.get("cash_dividend", pd.Series(0.0, index=qqq.index))
    qqq_return = qqq["close"].add(dividend).div(qqq["close"].shift(1)).sub(1.0).reindex(index)
    qqq_close = qqq["close"].reindex(index).ffill()
    v8_relative = (1.0 + v8["return"]) / (1.0 + v8["qqq_return"]) - 1.0
    v6_relative = (1.0 + v6["return"].reindex(index)) / (1.0 + v6["qqq_return"].reindex(index)) - 1.0
    v7_relative = (1.0 + v7_stock["strategy"].reindex(index)) / (1.0 + qqq_return) - 1.0
    # Shift every feature by one session so the window-start close/return is
    # not used to predict the same session.
    features = pd.DataFrame(index=index)
    features["qqq_above_100d"] = qqq_close.gt(qqq_close.rolling(100).mean()).shift(1)
    features["qqq_above_200d"] = qqq_close.gt(qqq_close.rolling(200).mean()).shift(1)
    features["qqq_63d_return_positive"] = qqq_close.pct_change(63).gt(0).shift(1)
    features["qqq_126d_return_positive"] = qqq_close.pct_change(126).gt(0).shift(1)
    features["qqq_63d_vol_above_30pct"] = qqq_return.rolling(63).std().mul(np.sqrt(252)).gt(0.30).shift(1)
    features["v6_63d_relative_positive"] = (
        (1.0 + v6_relative).rolling(63).apply(np.prod, raw=True).sub(1.0).gt(0).shift(1)
    )
    features["v7_stock_63d_relative_positive"] = (
        (1.0 + v7_relative).rolling(63).apply(np.prod, raw=True).sub(1.0).gt(0).shift(1)
    )
    windows = {}
    for label, sessions in (("13_weeks", 65), ("26_weeks", 130)):
        outcome = future_compounded(v8_relative, sessions)
        windows[label] = {
            column: regime_summary(outcome, features[column].fillna(False).astype(bool))
            for column in features
        }
    return {
        "schema_version": 1,
        "research_only": True,
        "historical_selection_contaminated": True,
        "feature_timing": "all regime features shifted one session and known before outcome window",
        "windows": windows,
    }


def run() -> dict:
    read = lambda path: pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    payload = build_report(read(V8_PATH), read(V6_PATH), read(V7_STOCK_PATH), read(QQQ_PATH))
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
