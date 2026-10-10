"""Readers of the Open Source Asset Pricing files (Chen & Zimmermann, release Oct 2025) and the Ken French monthly
market factors: the signal documentation, the predictor portfolio files and their long / short / long-short legs.

Moved unchanged from studies/osap_screen.py (``load_doc``, ``load_ports``, ``load_market``, ``legs``,
``check_ls_identity``). The raw folder and the data end date are parameters (studies/osap_screen.py passes
research_cache/osap and 2024-12-31).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_doc(raw: Path) -> pd.DataFrame:
    """SignalDoc.csv: the predictors (Cat.Signal == Predictor), indexed by acronym, years as nullable integers."""
    d = pd.read_csv(raw / "SignalDoc.csv")
    d = d[d["Cat.Signal"] == "Predictor"].copy()
    for c in ("Year", "SampleStartYear", "SampleEndYear"):
        d[c] = pd.to_numeric(d[c], errors="coerce").astype("Int64")
    return d.set_index("Acronym")


def load_ports(path: Path, data_end: str) -> pd.DataFrame:
    """A predictor portfolio file (signalname, port, month-end date, ret in %); asserts no month after ``data_end``."""
    p = pd.read_csv(path, dtype={"port": str, "signalname": str}, usecols=["signalname", "port", "date", "ret"])
    p["date"] = pd.to_datetime(p["date"]) + pd.offsets.MonthEnd(0)
    if p["date"].max() > pd.Timestamp(data_end):
        raise AssertionError(f"{path.name}: data after {data_end}")
    return p


def load_market(raw: Path) -> pd.Series:
    """Monthly total market return (%) = Mkt-RF + RF from the Ken French monthly file."""
    lines = (raw / "F-F_Research_Data_Factors.csv").read_text().splitlines()
    rows = []
    for ln in lines:
        parts = [x.strip() for x in ln.split(",")]
        if len(parts) == 5 and len(parts[0]) == 6 and parts[0].isdigit():
            rows.append((parts[0], float(parts[1]) + float(parts[4])))
        elif rows and len(parts[0]) != 6:
            break          # the annual block follows the monthly block
    s = pd.Series([v for _, v in rows], index=pd.to_datetime([k for k, _ in rows], format="%Y%m") + pd.offsets.MonthEnd(0))
    return s.rename("mkt")


def legs(ports: pd.DataFrame) -> dict:
    """signal -> DataFrame(date x {long, short, ls}) in %."""
    out = {}
    for s, g in ports.groupby("signalname"):
        w = g.pivot(index="date", columns="port", values="ret").sort_index()
        num = sorted(c for c in w.columns if c != "LS")
        f = pd.DataFrame({"long": w[num[-1]], "short": w[num[0]], "allavg": w[num].mean(axis=1, skipna=False)})
        f["ls"] = w["LS"] if "LS" in w else f["long"] - f["short"]
        out[s] = f
    return out


def check_ls_identity(leg: dict, tol: float = 1e-6) -> list:
    """Signals where LS != top port - port 01 (should be none)."""
    bad = []
    for s, f in leg.items():
        d = f.dropna()
        if len(d) and (d["ls"] - (d["long"] - d["short"])).abs().max() > tol:
            bad.append(s)
    return bad
