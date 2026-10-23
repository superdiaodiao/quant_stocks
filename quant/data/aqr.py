"""AQR's free monthly factor files (docs/research_ledger_buffett.md section 0.2) and FRED's 10-year Treasury yield.

- "Quality Minus Junk: Factors, Monthly" (QMJ, plus the same file's MKT, SMB, HML FF, UMD and RF sheets) and
  "Betting Against Beta: Equity Factors, Monthly" (BAB) from aqr.com. Every factor is a monthly self-financing
  excess return; the U.S. column (``USA``) is used; RF is the file's own risk-free rate. AQR rebuilds the whole
  history at every update, so numbers differ a little from the papers'.
- FRED DGS10 (10-year constant-maturity Treasury yield, percent) as a daily series in decimals.

Downloads go through ``quant.data.sources.http.cached_get`` (the caller's ``Ledger``); parsing needs openpyxl.
"""
from __future__ import annotations

import io
from pathlib import Path

import pandas as pd

from quant.data.sources.http import Ledger, cached_get

AQR_BASE = "https://www.aqr.com/-/media/AQR/Documents/Insights/Data-Sets/"
FILES = {"qmj": "Quality-Minus-Junk-Factors-Monthly.xlsx", "bab": "Betting-Against-Beta-Equity-Factors-Monthly.xlsx"}
HEADERS = {"User-Agent": "Mozilla/5.0"}
FRED_DGS10 = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"


def fetch(raw: Path, led: Ledger) -> None:
    for name in FILES.values():
        cached_get(AQR_BASE + name, Path(raw) / "aqr" / name, source="aqr", ledger=led, headers=HEADERS, timeout=120)
    cached_get(FRED_DGS10, Path(raw) / "fred" / "DGS10.csv", source="fred", ledger=led, headers=HEADERS, timeout=120)


def _sheet(path: Path, sheet: str, column: str = "USA") -> pd.Series:
    """One sheet of an AQR workbook as a month-end-indexed float series (the column under the 'DATE' header row)."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = list(wb[sheet].iter_rows(values_only=True))
    hi = next(i for i, r in enumerate(rows) if r and r[0] == "DATE")
    head = list(rows[hi])
    col = head.index(column) if column in head else 1
    out = {}
    for r in rows[hi + 1:]:
        if not r or r[0] is None:
            continue
        d = pd.Timestamp(r[0]) if not isinstance(r[0], str) else pd.to_datetime(r[0], format="%m/%d/%Y")
        v = r[col]
        if v is None or v == "":
            continue
        out[d.to_period("M")] = float(v)
    return pd.Series(out).sort_index()


def load_factors(raw: Path, last_month: str) -> pd.DataFrame:
    """Monthly U.S. MKT (excess), SMB, HML (Fama-French style), UMD, BAB, QMJ and RF; months up to ``last_month``
    (YYYY-MM) only."""
    q, b = Path(raw) / "aqr" / FILES["qmj"], Path(raw) / "aqr" / FILES["bab"]
    df = pd.DataFrame({"MKT": _sheet(q, "MKT"), "SMB": _sheet(q, "SMB"), "HML": _sheet(q, "HML FF"),
                       "UMD": _sheet(q, "UMD"), "QMJ": _sheet(q, "QMJ Factors"), "BAB": _sheet(b, "BAB Factors"),
                       "RF": _sheet(q, "RF", column="Risk Free Rate")})
    return df[df.index <= pd.Period(last_month, "M")]


def load_dgs10(raw: Path, end: str) -> pd.Series:
    """Daily 10-year Treasury yield (decimal) up to ``end``; FRED's missing days ('.') dropped."""
    df = pd.read_csv(io.BytesIO((Path(raw) / "fred" / "DGS10.csv").read_bytes()))
    df.columns = ["date", "dgs10"]
    df["dgs10"] = pd.to_numeric(df["dgs10"], errors="coerce")
    df = df.dropna()
    df = df[df["date"] <= end]
    return pd.Series(df["dgs10"].values / 100.0, index=pd.DatetimeIndex(df["date"]))
