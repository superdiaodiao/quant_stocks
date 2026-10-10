"""QuantQuote's free S&P 500 daily pack (1998-01 .. 2013-08-09), the archive.org copy (phase 3).

One zip of ``table_<ticker>.csv`` files without a header: date (YYYYMMDD), time, open, high, low, close, volume. The
reader is the one the data-source probe and the v2 archive fill used (scripts/data_source_probe.py
``probe_quantquote``, scripts/reversal_data_v2_archive.py ``run_quantquote``); the zip itself stays under
``research_cache/data_source_probe/raw/quantquote/``.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd

ZIP_NAME = "quantquote_daily_sp500_83986.zip"
URL = "https://web.archive.org/web/20150602033648id_/http://quantquote.com/files/quantquote_daily_sp500_83986.zip"
COLUMNS = ["date", "time", "open", "high", "low", "close", "volume"]


def table_names(z: zipfile.ZipFile) -> dict[str, str]:
    """file name (lower case) -> member path, e.g. 'table_aapl.csv' -> 'daily/table_aapl.csv'."""
    return {Path(n).name.lower(): n for n in z.namelist()}


def read_table(z: zipfile.ZipFile, member: str) -> pd.DataFrame:
    """One ticker's rows with ``date`` parsed."""
    df = pd.read_csv(z.open(member), header=None, names=COLUMNS)
    df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")
    return df
