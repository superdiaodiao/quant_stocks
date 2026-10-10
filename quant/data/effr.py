"""The effective federal funds rate (NY Fed EFFR), with the Fed funds target before the series starts (2000-07-03).

Extracted unchanged from studies/leverage_methods.py in the slimming pass (``FF_TARGET``, ``load_effr``); the raw
folder is an argument (no default). Used for the IBKR margin-loan rate (EFFR + spread).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant.data.guards import truncate_dev

# Fed funds target before the NY Fed EFFR series starts (2000-07-03): (effective date, rate)
FF_TARGET = (("1998-11-17", 4.75), ("1999-06-30", 5.00), ("1999-08-24", 5.25), ("1999-11-16", 5.50),
             ("2000-02-02", 5.75), ("2000-03-21", 6.00), ("2000-05-16", 6.50))


def load_effr(end: str, raw_dir: Path) -> pd.Series:
    """Daily EFFR (decimal) from the NY Fed CSV ``raw_dir``/EFFR_nyfed.csv, truncated at ``end``; the Fed funds
    target ``FF_TARGET`` filled in (calendar days) before the series starts."""
    df = pd.read_csv(raw_dir / "EFFR_nyfed.csv")
    df = df[df["Rate Type"] == "EFFR"]
    out = pd.DataFrame({"date": pd.to_datetime(df["Effective Date"], format="%m/%d/%Y").dt.strftime("%Y-%m-%d"),
                        "rate": pd.to_numeric(df["Rate (%)"], errors="coerce") / 100.0}).dropna()
    out = truncate_dev(out.sort_values("date"), "date", end)
    s = pd.Series(out["rate"].values, index=pd.DatetimeIndex(out["date"]))
    first = s.index[0]
    tgt = pd.Series([r / 100.0 for _, r in FF_TARGET], index=pd.DatetimeIndex([d for d, _ in FF_TARGET]))
    pre_idx = pd.date_range(tgt.index[0], first - pd.Timedelta(days=1), freq="D")
    pre = tgt.reindex(pre_idx.union(tgt.index)).ffill().reindex(pre_idx)
    return pd.concat([pre, s]).sort_index()
