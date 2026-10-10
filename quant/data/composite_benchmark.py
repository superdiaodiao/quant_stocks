"""Nasdaq Composite benchmarks from Yahoo charts: ONEQ (total return net of its fee), an estimated Composite total
return (^IXIC close + a dividend yield estimated from ONEQ's distributions) and QQQ, as studies/benchmark_composite.py
re-scores saved results against them.

Extracted unchanged from studies/benchmark_composite.py in the slimming pass (its own chart parser, here
``parse_chart_with_dividends``, and ``load_benchmarks``); the raw folder and ONEQ's expense ratio are arguments.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def parse_chart_with_dividends(path: Path) -> tuple[pd.DataFrame, pd.Series]:
    """(close / adjusted close by date, cash dividends by ex-date) of a Yahoo chart JSON; no date truncation."""
    r = json.loads(path.read_text())["chart"]["result"][0]
    ts = pd.to_datetime(r["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    q = r["indicators"]["quote"][0]
    adj = r["indicators"].get("adjclose", [{}])[0].get("adjclose") or q["close"]
    df = pd.DataFrame({"close": q["close"], "adj": adj}, index=pd.DatetimeIndex(ts.strftime("%Y-%m-%d")))
    divs = r.get("events", {}).get("dividends", {})
    d = pd.Series({pd.Timestamp(pd.to_datetime(v["date"], unit="s", utc=True).tz_convert("America/New_York")
                                .strftime("%Y-%m-%d")): v["amount"] for v in divs.values()}, dtype=float)
    return df.dropna(), d.sort_index()


def load_benchmarks(raw: Path, oneq_er: float) -> tuple[pd.DataFrame, pd.Series]:
    """Levels of ONEQ (adjusted), the estimated Nasdaq Composite total return (^IXIC + yield), ^IXIC and QQQ
    (adjusted) from the charts in ``raw``, and the estimated yield per calendar year (ONEQ distributions / mean
    ONEQ close + ``oneq_er``)."""
    oneq, oneq_div = parse_chart_with_dividends(raw / "chart_ONEQ.json")
    ixic, _ = parse_chart_with_dividends(raw / "chart_%5EIXIC.json")
    qqq, _ = parse_chart_with_dividends(raw / "chart_QQQ.json")
    # estimated Composite dividend yield per calendar year (from ONEQ distributions, fee added back)
    yrs = sorted(set(oneq.index.year))
    yld = {}
    for y in yrs:
        px = oneq.loc[oneq.index.year == y, "close"].mean()
        yld[y] = float(oneq_div[oneq_div.index.year == y].sum() / px + oneq_er)
    yld = pd.Series(yld)
    idx = ixic.index
    n_per_year = pd.Series(idx.year, index=idx).map(pd.Series(idx.year).value_counts())
    daily_y = pd.Series(idx.year, index=idx).map(yld) / n_per_year
    ixic_tr_ret = ixic["close"].pct_change() + daily_y
    level = pd.DataFrame({
        "ONEQ": oneq["adj"],
        "IXIC_TR_est": (1 + ixic_tr_ret.fillna(0)).cumprod(),
        "IXIC_price": ixic["close"],
        "QQQ": qqq["adj"],
    })
    return level, yld
