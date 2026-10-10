"""Frozen reference copy from scripts/research_qqq_timing.py at master d91c71a (before phase 2): the original
functions tests/quant/test_quant_core.py compares the quant core with. Do not edit."""
from __future__ import annotations
import json
import math
import zipfile
from pathlib import Path
from statistics import NormalDist
import numpy as np
import pandas as pd


NORM = NormalDist()
DEV_END = "2014-12-31"
CACHE = Path("/Users/bytedance/code/quant_stocks/research_cache/qqq_timing/raw")
KF_ZIP = Path("/Users/bytedance/code/quant_stocks/research_cache/reversal_2012_2026/raw/kf/"
              "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip")
# ---------------------------------------------------------------- costs (IBKR Pro Tiered, $10,000 start)
START_EQUITY = 10_000.0
COMMISSION_PER_SHARE = 0.0035
COMMISSION_MIN = 0.35
COMMISSION_MAX_FRAC = 0.01
FEES_PER_SHARE = 0.0005           # exchange + clearing pass-through, order of magnitude
SELL_REG_FRAC = 0.3e-4            # SEC + FINRA TAF on sells, order of magnitude
HALF_SPREAD = 1e-4                # 1 bp for QQQ, QLD and the T-bill ETF
TRADING_DAYS = 252


class FutureDataError(AssertionError):
    pass


def assert_dev_dates(dates, end: str = DEV_END) -> None:
    """Raise when any date (YYYY-MM-DD strings or Timestamps) is after the development end."""
    values = pd.to_datetime(pd.Index(list(dates)) if not isinstance(dates, (pd.Series, pd.Index)) else dates)
    if len(values) and values.max() > pd.Timestamp(end):
        raise FutureDataError(f"date {values.max().date()} is after the development end {end}")


def truncate_dev(frame: pd.DataFrame, column: str = "date", end: str = DEV_END) -> pd.DataFrame:
    out = frame[pd.to_datetime(frame[column]) <= pd.Timestamp(end)].copy()
    assert_dev_dates(out[column], end)
    return out


def parse_chart(path: Path, end: str = DEV_END) -> pd.DataFrame:
    """Yahoo chart JSON -> date, close (split-adjusted), adjclose, dividend; truncated at ``end`` at once."""
    j = json.loads(Path(path).read_text())["chart"]["result"][0]
    ts = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    q = j["indicators"]["quote"][0]
    adj = j["indicators"].get("adjclose", [{}])[0].get("adjclose") or q["close"]
    df = pd.DataFrame({"date": ts.strftime("%Y-%m-%d"), "close": q["close"], "adjclose": adj})
    divs = j.get("events", {}).get("dividends", {})
    dmap = {}
    for v in divs.values():
        d = pd.to_datetime(v["date"], unit="s", utc=True).tz_convert("America/New_York").strftime("%Y-%m-%d")
        dmap[d] = dmap.get(d, 0.0) + float(v["amount"])
    df["dividend"] = df["date"].map(dmap).fillna(0.0)
    df = truncate_dev(df.dropna(subset=["close"]), "date", end)
    if df["date"].duplicated().any():
        raise ValueError(f"duplicate dates in {path}")
    return df.reset_index(drop=True)


def load_kf_rf(end: str = DEV_END) -> pd.Series:
    with zipfile.ZipFile(KF_ZIP) as z:
        text = z.read(z.namelist()[0]).decode("latin-1")
    rows = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 7 and parts[0].isdigit() and len(parts[0]) == 8:
            rows.append((pd.Timestamp(parts[0]).strftime("%Y-%m-%d"), float(parts[6]) / 100.0))
    df = truncate_dev(pd.DataFrame(rows, columns=["date", "rf"]), "date", end)
    return pd.Series(df["rf"].values, index=pd.DatetimeIndex(df["date"]))


def load_dtb3(end: str = DEV_END) -> pd.Series:
    df = pd.read_csv(CACHE / "DTB3.csv")
    df.columns = ["date", "dtb3"]
    df["dtb3"] = pd.to_numeric(df["dtb3"], errors="coerce")
    df = truncate_dev(df.dropna(), "date", end)
    return pd.Series(df["dtb3"].values / 100.0, index=pd.DatetimeIndex(df["date"]))


def fill_rf(rf: pd.Series, dtb3: pd.Series, sessions: pd.DatetimeIndex) -> pd.Series:
    """KF daily RF on the sessions; sessions after the last KF row use DTB3 / 252 (the KF file lags ~1 month).

    In development runs KF covers every session, so this is the plain KF series.
    """
    out = rf.reindex(sessions)
    after = sessions > rf.index.max()
    if after.any():
        out[after] = (dtb3.reindex(sessions).ffill() / TRADING_DAYS)[after]
    return out.ffill().fillna(0.0)


def cagr_of(r: pd.Series, periods: float = TRADING_DAYS) -> float:
    r = pd.Series(r).dropna()
    return float(np.prod(1 + r.values) ** (periods / len(r)) - 1) if len(r) else float("nan")


def order_cost(value: float, price: float, sell: bool) -> float:
    if value <= 0:
        return 0.0
    shares = value / price
    comm = min(max(COMMISSION_MIN, COMMISSION_PER_SHARE * shares), COMMISSION_MAX_FRAC * value)
    return comm + FEES_PER_SHARE * shares + HALF_SPREAD * value + (SELL_REG_FRAC * value if sell else 0.0)


def max_drawdown(value: pd.Series) -> float:
    return float((value / value.cummax() - 1).min())


def monthly(r: pd.Series) -> pd.Series:
    return (1 + r).groupby(r.index.to_period("M")).prod() - 1


def yearly(r: pd.Series) -> pd.Series:
    return (1 + r).groupby(r.index.year).prod() - 1


def cagr_months(m: pd.Series) -> float:
    return float(np.prod(1 + m.values) ** (12 / len(m)) - 1)


def deflated_sharpe(sr: float, n_obs: int, skew: float, kurt: float, sr_trials: np.ndarray) -> dict:
    """Bailey and Lopez de Prado (2014), per-period Sharpe ratios (here: monthly IR of excess over QQQ)."""
    n = len(sr_trials)
    var = float(np.var(sr_trials, ddof=1))
    g = 0.5772156649
    sr0 = math.sqrt(var) * ((1 - g) * NORM.inv_cdf(1 - 1 / n) + g * NORM.inv_cdf(1 - 1 / (n * math.e)))
    denom = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr ** 2, 1e-12))
    z = (sr - sr0) * math.sqrt(n_obs - 1) / denom
    return {"n_trials": n, "sr0_monthly": sr0, "dsr": float(NORM.cdf(z)), "z": z}


def bonferroni_t(n: int, alpha: float = 0.05) -> float:
    return NORM.inv_cdf(1 - alpha / n)
