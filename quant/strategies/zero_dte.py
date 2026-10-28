"""Decoder for the 0DTE grid's QuantConnect export (docs/research_ledger_zero_dte.md, sections 0.3 and 0.7).

qc/zero_dte/main.py queues, per trading day, a 17-value header followed by one row per (entry time, structure)
when the day has same-day-expiry contracts; qc/zero_dte/fetch.js rebuilds that queue from the chart points and
saves it as gzipped JSON under output/research_only/zero_dte/raw/. ``read_export`` turns one file into
``Export(days, rows)``: ``days`` is one row per trading day, ``rows`` one row per (day, entry, shape) with the
unpacked position fields (NaN where the position was not opened; ``code`` says why).

The structure catalogue (``SHAPES``) is imported from qc/zero_dte/sim.py so the decoder can never drift from the
engine.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import math
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from quant.paths import ROOT

SIM_PATH = ROOT / "qc" / "zero_dte" / "sim.py"
HEADER = 17
OFF = 5_000_000
CODES = {1: "nosel", 2: "wingmiss", 3: "covmiss", 4: "stale", 5: "nocredit", 9: "too_late"}
MGMT = ("H", "M1", "M2", "M3")


def load_sim():
    """qc/zero_dte/sim.py as a module (scipy.special stubbed when the local venv lacks scipy)."""
    try:
        import scipy.special  # noqa: F401
    except ImportError:
        sp, sps = types.ModuleType("scipy"), types.ModuleType("scipy.special")
        erf = np.vectorize(math.erf, otypes=[float])
        sps.ndtr = lambda x: 0.5 * (1.0 + erf(np.asarray(x, float) / math.sqrt(2.0)))
        sp.special = sps
        sys.modules.setdefault("scipy", sp)
        sys.modules["scipy.special"] = sps
    spec = importlib.util.spec_from_file_location("zero_dte_sim", SIM_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SIM = load_sim()
SHAPES = SIM.SHAPES
FAM = SIM.FAM
KIND = SIM.KIND
ENTRY = SIM.ENTRY
NS, NE = len(SHAPES), len(ENTRY)


def shape_name(si: int) -> str:
    f, d, w = SHAPES[si]
    fam = FAM[f]
    if fam in ("PCS", "CCS", "IC", "IF"):
        wl = "W1" if w == 0.0 else f"W{w * 100:g}%"
        return f"{fam}{d:.2f}-{wl}" if fam != "IF" else f"IF-{wl}"
    if fam in ("SD", "LD", "SDH", "RRB"):
        return fam
    return f"{fam}{d:.2f}"


def scale(under: str) -> float:
    return 1e3 if under == "SPXW" else 1e4


@dataclass
class Export:
    under: str
    meta: dict
    days: pd.DataFrame
    rows: pd.DataFrame


def _two(x: int) -> tuple[int, int]:
    return x // 10**7, x % 10**7


def parse_stream(vals: list, under: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    U = scale(under)
    days, recs = [], []
    i, n = 0, len(vals)
    while i < n:
        v = vals[i]
        if v is None:
            raise ValueError(f"missing value at {i}")
        if v == 920_000_000_000_000:
            i += 1
            continue
        if not (900_000_000_000_000 <= v < 910_000_000_000_000):
            raise ValueError(f"expected a day marker at {i}, got {v}")
        ymd = int(v - 900_000_000_000_000)
        h = [int(x) for x in vals[i:i + HEADER]]
        flags = h[1]
        z, half, status = flags // 10**13, (flags // 10**12) % 10, (flags // 10**11) % 10
        nchain, nload, sig = (flags // 10**6) % 10**4, (flags // 10**2) % 10**4, flags % 10
        nreal, nrows = h[15], h[16]
        day = pd.Timestamp(year=ymd // 10_000, month=(ymd // 100) % 100, day=ymd % 100)
        days.append({"date": day, "zdte": z, "half": half, "status": status, "nchain": nchain, "nload": nload, "nreal": nreal,
                     "S_open": h[2] / U, "S_close": h[3] / U, "vix_prev": h[4] / 100 if sig else np.nan,
                     **{f"S_e{e}": h[5 + e] / U for e in range(NE)}, **{f"strad_e{e}": h[10 + e] / U for e in range(NE)}})
        i += HEADER
        if nrows and nrows != NS * NE:
            raise ValueError(f"{day.date()}: {nrows} rows, expected {NS * NE}")
        for r in range(nrows):
            e, si = divmod(r, NS)
            x1 = vals[i]
            if x1 < 0:
                recs.append((day, e, si, int(-x1)))
                i += 1
                continue
            x = [int(t) for t in vals[i:i + 8]]
            i += 8
            c, den = _two(x[0])
            ph, p1 = _two(x[1])
            p2, p3 = _two(x[2])
            im0, dc = _two(x[3])
            fe0, fef = _two(x[7])
            recs.append((day, e, si, 0, (c - OFF) / U, den / U, (ph - OFF) / U, (p1 - OFF) / U, (p2 - OFF) / U,
                         (p3 - OFF) / U, im0 / U, (dc - OFF) / U,
                         x[4] // 10**10, (x[4] // 10**7) % 1000, (x[4] // 10**4) % 1000, (x[4] % 10**4) / 100,
                         (x[5] // 10**8) / 100, ((x[5] // 10**4) % 10**4) / 100, (x[5] % 10**4) / 100,
                         (x[6] // 10**9) / 1000, ((x[6] // 10**5) % 10**4) / 1e4, (x[6] // 10**4) % 10,
                         (x[6] // 10**3) % 10, (x[6] // 100) % 10, (x[6] // 10) % 10, x[6] % 10, fe0 / U, fef / U))
    cols = ["date", "entry", "shape", "code", "credit", "den", "pnl_H", "pnl_M1", "pnl_M2", "pnl_M3", "imp_entry",
            "d_close", "x_M1", "x_M2", "x_M3", "spr_entry", "imp_M1", "imp_M2", "imp_M3", "delta", "width_frac",
            "n_entry", "n_flat", "n_M1", "n_M2", "n_M3", "fee_entry", "fee_flat"]
    rows = pd.DataFrame([r + (np.nan,) * (len(cols) - len(r)) for r in recs], columns=cols)
    return pd.DataFrame(days), rows


def read_export(path: Path) -> Export:
    with gzip.open(path, "rt") as fh:
        obj = json.load(fh)
    if obj.get("bad"):
        raise ValueError(f"{path.name}: {obj['bad']} bad slots in the chart stream")
    under = obj["meta"]["under"]
    days, rows = parse_stream(obj["vals"], under)
    return Export(under, obj["meta"], days, rows)
