"""The daily risk-free rate: Ken French daily RF, extended with FRED DTB3 / 252 after the last French row.

Extracted unchanged from scripts/research_qqq_timing.py.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd

from quant.data.guards import truncate_dev
from quant.paths import CACHE_ROOT

TRADING_DAYS = 252
KF_ZIP = CACHE_ROOT / "reversal_2012_2026/raw/kf/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
DTB3_CSV = CACHE_ROOT / "qqq_timing/raw/DTB3.csv"


def load_kf_rf(end: str, path: Path = KF_ZIP) -> pd.Series:
    """Ken French daily RF (decimal), truncated at ``end``."""
    with zipfile.ZipFile(path) as z:
        text = z.read(z.namelist()[0]).decode("latin-1")
    rows = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 7 and parts[0].isdigit() and len(parts[0]) == 8:
            rows.append((pd.Timestamp(parts[0]).strftime("%Y-%m-%d"), float(parts[6]) / 100.0))
    df = truncate_dev(pd.DataFrame(rows, columns=["date", "rf"]), "date", end)
    return pd.Series(df["rf"].values, index=pd.DatetimeIndex(df["date"]))


def load_dtb3(end: str, path: Path = DTB3_CSV) -> pd.Series:
    """FRED 3-month T-bill rate (decimal, annual), truncated at ``end``."""
    df = pd.read_csv(path)
    df.columns = ["date", "dtb3"]
    df["dtb3"] = pd.to_numeric(df["dtb3"], errors="coerce")
    df = truncate_dev(df.dropna(), "date", end)
    return pd.Series(df["dtb3"].values / 100.0, index=pd.DatetimeIndex(df["date"]))


def fill_rf(rf: pd.Series, dtb3: pd.Series, sessions: pd.DatetimeIndex) -> pd.Series:
    """KF daily RF on the sessions; sessions after the last KF row use DTB3 / 252 (the KF file lags ~1 month)."""
    out = rf.reindex(sessions)
    after = sessions > rf.index.max()
    if after.any():
        out[after] = (dtb3.reindex(sessions).ffill() / TRADING_DAYS)[after]
    return out.ffill().fillna(0.0)


def daily_rf(sessions: pd.DatetimeIndex, end: str) -> pd.Series:
    """``fill_rf(load_kf_rf(end), load_dtb3(end), sessions)``: the usual one-liner."""
    return fill_rf(load_kf_rf(end), load_dtb3(end), sessions)
