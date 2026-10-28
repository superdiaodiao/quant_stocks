"""0DTE option grid: analysis of the QuantConnect exports (pre-registered in docs/research_ledger_zero_dte.md, 0.4-0.12).

Inputs (read only): output/research_only/zero_dte/raw/zero_dte_<UNDER>_<first>_<last>.json.gz (decoded chart
streams of qc/zero_dte/main.py, one file per registered chunk, decoded by quant.strategies.zero_dte),
state/zero_dte/event_days.csv (FOMC / CPI / NFP days), Yahoo daily charts in research_cache (ONEQ, QQQ, SPY: benchmark
returns, official opens, 200-day averages; live 0DTE funds for the calibration), FRED DTB3 (cash), the risk ladder's
monthly returns (ONEQ x1.3 and QQQ + 10% BTC). Outputs: output/research_only/zero_dte/ (statistics only).

Steps (ledger section): configurations (0.4), units and sizing (0.5, 0.12), per-configuration statistics and in-grid A
(0.6.1-0.6.3), the walk-forward selection that decides (0.6.4), two-fold, PBO, bootstrap max-t / Romano-Wolf, deflated
Sharpe, minimum detectable effect (0.6.5), named comparators (0.6.6), live-fund calibration (0.6.7), the $10k integer
replay (0.6.8), the equal-risk comparison (0.6.9), the scorecard (0.6.10) and the GO table (0.10).

Usage:  PYTHONPATH=. .venv/bin/python -m quant study zero_dte [--under QQQ|SPXW] [--quick] [--allow-partial]
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data.rates import load_dtb3
from quant.data.sources.yahoo import parse_chart, parse_ohlc
from quant.evaluation.criteria import deflated_sharpe_n
from quant.evaluation.scorecard import scorecard
from quant.evaluation.stats import stationary_bootstrap_index
from quant.paths import CACHE_ROOT, ROOT, output_dir
from quant.reporting import write_json
from quant.strategies import zero_dte as Z

OUT = output_dir("zero_dte")
RAW = OUT / "raw"
EVENTS = ROOT / "state" / "zero_dte" / "event_days.csv"
YAHOO = CACHE_ROOT / "sector_lev" / "raw"
FUNDS = CACHE_ROOT / "zero_dte" / "raw" / "funds"
LADDER = output_dir("risk_ladder") / "monthly_returns_full.csv"
START, END = "2012-02-01", "2026-07-29"
ERAS = {"E1": ("2012-02-01", "2021-04-30"), "E2": ("2021-05-01", "2022-11-11"), "E3": ("2022-11-14", END)}
CHUNKS = {"QQQ": [("2012-02-01", "2016-12-31"), ("2017-01-01", "2020-12-31"), ("2021-01-01", "2022-12-31"),
                  ("2023-01-01", "2024-12-31"), ("2025-01-01", "2026-07-29")],
          "SPXW": [("2012-02-01", "2016-12-31"), ("2017-01-01", "2019-12-31"), ("2020-01-01", "2021-12-31"),
                   ("2022-01-01", "2023-12-31"), ("2024-01-01", "2025-12-31"), ("2026-01-01", "2026-07-29")]}
SIM_HASH = "75230e4ad7396633"
N_TRIALS = 24_480
T_GRID = 4.75
T_OOS = 2.24
SLEEVE_SD = 0.005                          # target daily sd of the option sleeve over all trading days
CAPS = {0: 0.05, 1: 3.0, 2: 0.02, 3: 3.0, 4: 3.0, 5: 0.025}  # kind 5 = per-side-stop composites of verticals
NW_LAGS = 5
SEED = 20261028
FILTERS = ("ALL", "V20", "GAP", "UP", "NOEVT", "RICH", "MOM", "FRI")
MGMT = ("H", "M1", "M2", "M3")
LAD = Z.NE                                 # entry index of the equal-weight ladder
SELECTABLE = [s for s in range(Z.NS) if Z.FAM[Z.SHAPES[s][0]] not in ("RRB", "SDH")]
BULL, BEAR = {"PCS", "SP", "RR", "RRB"}, {"CCS", "SC"}
BUYERS = {"LD"}


# ---------------------------------------------------------------------------------------------------- structures
def composites() -> list[dict]:
    """Per-side-stop structures assembled from single-side books at SL2 (ledger 0.4): IC-BE, IF-BE, SG-BE, SD-BE."""
    idx = {sh: i for i, sh in enumerate(Z.SHAPES)}
    out = []
    for d in Z.SIM.DELTAS:
        for w in Z.SIM.WINGS:
            p, c = idx[(0, d, w)], idx[(1, d, w)]
            name = (f"IC-BE{d:.2f}" if d < 0.5 else "IF-BE") + ("-W1" if w == 0 else f"-W{w * 100:g}%")
            out.append({"name": name, "put": p, "call": c, "kind": 5, "fam": "IC-BE" if d < 0.5 else "IF-BE",
                        "d": d, "w": w})
    for d in (0.10, 0.16, 0.25, 0.50):
        p, c = idx[(4, d, 0.0)], idx[(5, d, 0.0)]
        out.append({"name": f"SG-BE{d:.2f}" if d < 0.5 else "SD-BE", "put": p, "call": c, "kind": 1,
                    "fam": "SG-BE" if d < 0.5 else "SD-BE", "d": d, "w": 0.0})
    return out


COMP = composites()                        # 19


def base_units(P: Z.Panel, conv: str = "primary"):
    """Unit returns [NS + 19, NE, 4, ND] (0 where not opened), the opened mask and the kind of each base structure.
    conv: primary | alt (other settlement) | cons (primary with conservative execution)."""
    pnl = P.pnl if conv in ("primary", "cons") else P.alt
    if conv == "cons":
        pnl = pnl + P.cons
    with np.errstate(divide="ignore", invalid="ignore"):
        u = np.where(P.opened[:, :, None, :], pnl / P.den[:, :, None, :], 0.0)
    u = np.nan_to_num(u)
    nb = Z.NS + len(COMP)
    U = np.zeros((nb, Z.NE, 4, u.shape[-1]))
    O = np.zeros((nb, Z.NE, u.shape[-1]), bool)
    U[:Z.NS], O[:Z.NS] = u, P.opened
    kinds = [Z.KIND[Z.SHAPES[s][0]] for s in range(Z.NS)]
    for j, c in enumerate(COMP):
        p, q = c["put"], c["call"]
        both = P.opened[p] & P.opened[q]
        pn = pnl[p, :, 2, :] + pnl[q, :, 2, :]
        if c["kind"] == 5:
            wmax = np.fmax(P.width[p], P.width[q])
            den = np.maximum(wmax - (P.net[p] + P.net[q]), Z.DEN_FLOOR * wmax)
        else:
            den = P.s0
        with np.errstate(divide="ignore", invalid="ignore"):
            uc = np.where(both, pn / den, 0.0)
        U[Z.NS + j, :, :, :] = np.nan_to_num(uc)[:, None, :]       # the same series under every management index
        O[Z.NS + j] = both
        kinds.append(c["kind"])
    return U, O, kinds


def base_name(b: int) -> str:
    return Z.shape_name(b) if b < Z.NS else COMP[b - Z.NS]["name"]


def base_fam(b: int) -> str:
    return Z.FAM[Z.SHAPES[b][0]] if b < Z.NS else COMP[b - Z.NS]["fam"]


def base_dw(b: int) -> tuple[float, float]:
    if b < Z.NS:
        return Z.SHAPES[b][1], Z.SHAPES[b][2]
    return COMP[b - Z.NS]["d"], COMP[b - Z.NS]["w"]


def k10(b: int) -> bool:
    fam = base_fam(b)
    return fam in ("PCS", "CCS", "IC", "IF", "IC-BE", "IF-BE") and base_dw(b)[1] in (0.0, 0.005)


# ---------------------------------------------------------------------------------------------------- data
def yahoo_frame(name: str) -> pd.DataFrame:
    df = parse_ohlc(YAHOO / f"chart_{name}.json", END).set_index("date")
    df.index = pd.DatetimeIndex(df.index)
    return df


def sessions(under: str) -> pd.DatetimeIndex:
    df = yahoo_frame("SPY" if under == "SPXW" else "QQQ")
    return df.index[(df.index >= START) & (df.index <= END)]


def benchmarks(index: pd.DatetimeIndex, under: str) -> pd.DataFrame:
    """Benchmark day returns and the inputs of GAP / UP on the panel days; raises on any missing ONEQ / QQQ day."""
    out = pd.DataFrame(index=index)
    for t in ("ONEQ", "QQQ", "SPY"):
        df = yahoo_frame(t)
        out[t.lower()] = df["adjclose"].pct_change().reindex(index)
    if out[["oneq", "qqq"]].isna().any().any():
        raise ValueError(f"missing benchmark days: {out.index[out[['oneq', 'qqq']].isna().any(axis=1)][:5].tolist()}")
    rf = load_dtb3(END)                                               # decimal annual rate
    rf.index = pd.DatetimeIndex(rf.index)
    out["rf"] = (rf.reindex(index, method="ffill") / 252.0).fillna(0.0)
    if under == "SPXW":
        spy = yahoo_frame("SPY")
        out["sma200_prev"] = spy["adjclose"].rolling(200).mean().shift(1).reindex(index)
        out["prev_trend"] = spy["adjclose"].shift(1).reindex(index)
        out["open"], out["prev_close"] = np.nan, np.nan                # SPXW gap comes from the panel (filter_masks)
    else:
        q = yahoo_frame("QQQ")
        out["sma200_prev"] = q["close"].rolling(200).mean().shift(1).reindex(index)
        out["prev_trend"] = q["close"].shift(1).reindex(index)
        out["open"] = q["open"].reindex(index)
        out["prev_close"] = q["close"].shift(1).reindex(index)
    return out


def trailing(x: np.ndarray, n: int, how: str, min_n: int) -> np.ndarray:
    """Per row (entry), the mean / median of the previous n valid observations (NaN = not valid), excluding today."""
    out = np.full(x.shape, np.nan)
    for e in range(x.shape[0]):
        s = pd.Series(x[e])
        v = s.dropna()
        r = v.rolling(n, min_periods=min_n)
        st = (r.mean() if how == "mean" else r.median()).shift(1)
        out[e] = st.reindex(s.index).ffill().to_numpy() if len(v) else np.nan
    return out


def filter_masks(P: Z.Panel, bench: pd.DataFrame, under: str) -> dict:
    """Boolean masks [NE, ND] per filter; RICH and MOM per side ("sell"/"buy", "bull"/"bear"/"neutral")."""
    d = P.days
    nd = len(d)
    idx = d.index
    ev = pd.read_csv(EVENTS, parse_dates=["date"])
    vix = d["vix_prev"].to_numpy()
    Sc = d["S_close"].to_numpy().astype(float)
    Sc = np.where(Sc > 0, Sc, np.nan)
    prev = np.r_[np.nan, Sc[:-1]]
    so = d["S_open"].to_numpy().astype(float)
    so = np.where(so > 0, so, np.nan)
    gap_panel = np.abs(so / prev - 1)                                  # 09:31 bar open vs previous close
    if under == "SPXW":
        gap_late = gap_panel
    else:
        gap_late = np.abs(bench["open"] / bench["prev_close"] - 1).to_numpy()   # official open (Yahoo)
    gap = np.vstack([gap_panel] + [gap_late] * (Z.NE - 1))            # 09:45 uses the 09:31 price (ledger 0.4)
    up = (bench["prev_trend"] > bench["sma200_prev"]).to_numpy()
    one = np.ones((Z.NE, nd), bool)
    m = {"ALL": one, "V20": np.broadcast_to(np.nan_to_num(vix) > 20, (Z.NE, nd)).copy(),
         "GAP": np.nan_to_num(gap, nan=1.0) <= 0.005,
         "UP": np.broadcast_to(up, (Z.NE, nd)).copy(),
         "NOEVT": np.broadcast_to(~idx.isin(set(ev["date"])), (Z.NE, nd)).copy()}
    wd = idx.weekday
    nxt_open = np.r_[(idx[1:] - idx[:-1]).days == 1, False]       # next calendar day is a session
    fri = (wd == 4) | ((wd == 3) & ~nxt_open)
    m["FRI"] = np.broadcast_to(fri, (Z.NE, nd)).copy()
    S = np.where(P.s0 > 0, P.s0, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        move = np.abs(Sc[None, :] / S - 1)                             # realised rest-of-day move at each entry
        strad = d[[f"strad_e{e}" for e in range(Z.NE)]].to_numpy().T
        rich_px = np.where(strad > 0, strad / S, np.nan)
        r = S / prev[None, :] - 1
    past = trailing(move, 20, "mean", 20)
    med = trailing(np.abs(r), 250, "median", 60)
    ok = ~np.isnan(past) & ~np.isnan(rich_px)
    m["RICH_sell"] = ok & (np.nan_to_num(rich_px) >= np.nan_to_num(past))
    m["RICH_buy"] = ok & (np.nan_to_num(rich_px) < np.nan_to_num(past))
    m["MOM_bull"] = np.nan_to_num(r, nan=0) > 0
    m["MOM_bear"] = np.nan_to_num(r, nan=0) < 0
    m["MOM_neutral"] = ~np.isnan(med) & ~np.isnan(r) & (np.abs(np.nan_to_num(r)) <= np.nan_to_num(med))
    return m


def mask_for(masks: dict, f: str, b: int) -> np.ndarray:
    fam = base_fam(b)
    if f == "RICH":
        return masks["RICH_buy" if fam in BUYERS else "RICH_sell"]
    if f == "MOM":
        return masks["MOM_bull" if fam in BULL else ("MOM_bear" if fam in BEAR else "MOM_neutral")]
    return masks[f]


# ---------------------------------------------------------------------------------------------------- configurations
def config_list() -> pd.DataFrame:
    rows = []
    for b in SELECTABLE:
        for e in range(Z.NE + 1):
            for mi in range(4):
                for fi in range(len(FILTERS)):
                    rows.append((b, e, mi, fi))
    for j in range(len(COMP)):
        for e in range(Z.NE + 1):
            for fi in range(len(FILTERS)):
                rows.append((Z.NS + j, e, 2, fi))
    cf = pd.DataFrame(rows, columns=["b", "e", "m", "f"])
    if len(cf) != N_TRIALS // 2:
        raise ValueError(f"{len(cf)} configurations, registered {N_TRIALS // 2}")
    cf["name"] = [f"{base_name(b)}|{'LAD' if e == LAD else '%02d:%02d' % Z.ENTRY[e]}|"
                  f"{'BE' if b >= Z.NS else MGMT[m]}|{FILTERS[f]}" for b, e, m, f in cf[["b", "e", "m", "f"]].to_numpy()]
    cf["fam"] = [base_fam(b) for b in cf["b"]]
    cf["k10"] = [k10(b) for b in cf["b"]]
    return cf


def series(U, O, masks, b, e, m, f) -> tuple[np.ndarray, np.ndarray]:
    """Daily unit return and traded flag of one configuration (LAD = equal-weight average of the five entries)."""
    mk = mask_for(masks, FILTERS[f], b)
    if e == LAD:
        return (U[b, :, m, :] * mk).mean(axis=0), (O[b] & mk).any(axis=0)
    return U[b, e, m, :] * mk[e], O[b, e] & mk[e]


def matrix(U, O, masks, cf) -> tuple[np.ndarray, np.ndarray]:
    R = np.zeros((len(cf), U.shape[-1]), np.float32)
    T = np.zeros((len(cf), U.shape[-1]), bool)
    for i, (b, e, m, f) in enumerate(cf[["b", "e", "m", "f"]].to_numpy()):
        R[i], T[i] = series(U, O, masks, b, e, m, f)
    return R, T


def xret_matrix(P, O, masks, cf) -> np.ndarray:
    """Underlying return from each configuration's entry to the close on its traded days (LAD: weighted 1/5 like
    the returns), 0 elsewhere."""
    X = np.zeros((len(cf), P.xret.shape[-1]), np.float32)
    xr = np.nan_to_num(P.xret)
    for i, (b, e, m, f) in enumerate(cf[["b", "e", "m", "f"]].to_numpy()):
        mk = mask_for(masks, FILTERS[f], b)
        X[i] = (xr * (O[b] & mk)).mean(axis=0) if e == LAD else xr[e] * (O[b, e] & mk[e])
    return X


# ---------------------------------------------------------------------------------------------------- statistics
def nw_var(Zc: np.ndarray, lags: int = NW_LAGS) -> np.ndarray:
    """Newey-West (Bartlett) long-run variance of each row of the demeaned matrix Zc [n_series, n_obs]."""
    n = Zc.shape[1]
    s = (Zc * Zc).sum(axis=1) / n
    for k in range(1, lags + 1):
        s = s + 2 * (1 - k / (lags + 1)) * (Zc[:, k:] * Zc[:, :-k]).sum(axis=1) / n
    return s


def nw_t(R: np.ndarray, lags: int = NW_LAGS) -> np.ndarray:
    """Newey-West t of the row means of R [n_series, n_obs]."""
    R = np.asarray(R, np.float64)
    n = R.shape[1]
    mu = R.mean(axis=1)
    s = nw_var(R - mu[:, None], lags)
    return np.where(s > 0, mu / np.sqrt(np.maximum(s, 1e-300) / n), 0.0)


def alpha_t(R: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per row: OLS of R on [1, X]: beta, daily alpha and the HAC (Newey-West, Bartlett 5) t of alpha."""
    R, X = np.asarray(R, np.float64), np.asarray(X, np.float64)
    n = R.shape[1]
    xm, rm = X.mean(axis=1), R.mean(axis=1)
    sxx = ((X - xm[:, None]) ** 2).mean(axis=1)
    beta = np.where(sxx > 0, ((X - xm[:, None]) * (R - rm[:, None])).mean(axis=1) / np.where(sxx > 0, sxx, 1), 0.0)
    a = rm - beta * xm
    u = R - a[:, None] - beta[:, None] * X
    # sandwich for the 2x2 case: Q = [[1, xm], [xm, x2]], S = LRV of [u, x u]; V = Q^-1 S Q^-1 / n
    x2 = (X * X).mean(axis=1)
    g0, g1 = u, X * u
    def lrv(p, q):
        s = (p * q).sum(axis=1) / n
        for k in range(1, NW_LAGS + 1):
            w = 1 - k / (NW_LAGS + 1)
            s = s + w * ((p[:, k:] * q[:, :-k]).sum(axis=1) + (q[:, k:] * p[:, :-k]).sum(axis=1)) / n
        return s
    s00, s01, s11 = lrv(g0, g0), lrv(g0, g1), lrv(g1, g1)
    det = x2 - xm * xm
    ok = det > 1e-18
    i00 = np.where(ok, x2 / np.where(ok, det, 1), 1.0)
    i01 = np.where(ok, -xm / np.where(ok, det, 1), 0.0)
    i11 = np.where(ok, 1 / np.where(ok, det, 1), 0.0)
    v00 = (i00 * i00 * s00 + 2 * i00 * i01 * s01 + i01 * i01 * s11) / n
    t = np.where(v00 > 0, a / np.sqrt(np.maximum(v00, 1e-300)), 0.0)
    return beta, a, t


def row_hash(R: np.ndarray) -> np.ndarray:
    return np.array([int(hashlib.md5(r.tobytes()).hexdigest()[:15], 16) for r in R], np.int64)


def grid_stats(R, T, X, days: pd.DatetimeIndex) -> pd.DataFrame:
    mu = R.mean(axis=1, dtype=np.float64)
    sd = R.std(axis=1, ddof=1, dtype=np.float64)
    out = pd.DataFrame({"mean_bp": mu * 1e4, "ir": np.where(sd > 0, mu / np.where(sd > 0, sd, 1), 0.0), "t": nw_t(R),
                        "trades": T.sum(axis=1)})
    beta, a, at = alpha_t(R, X)
    out["beta_intraday"], out["alpha_bp"], out["alpha_t"] = beta, a * 1e4, at
    for era, (a0, a1) in {"E1": ERAS["E1"], "E23": (ERAS["E2"][0], END)}.items():
        sl = (days >= a0) & (days <= a1)
        out[f"mean_{era}_bp"] = R[:, sl].mean(axis=1, dtype=np.float64) * 1e4
        out[f"trades_{era}"] = T[:, sl].sum(axis=1)
    sl = (days >= ERAS["E3"][0]) & (days <= END)
    out["mean_E3_bp"] = R[:, sl].mean(axis=1, dtype=np.float64) * 1e4
    out["t_E3"] = nw_t(R[:, sl])
    out["dup"] = pd.Series(row_hash(R)).duplicated().to_numpy()
    eval1, eval2 = out["trades_E1"] >= 100, out["trades_E23"] >= 100
    out["A"] = ((out["t"] >= T_GRID) & (out["alpha_t"] >= T_GRID)
                & (~eval1 | (out["mean_E1_bp"] > 0)) & (~eval2 | (out["mean_E23_bp"] > 0)) & (eval1 | eval2))
    return out


# ---------------------------------------------------------------------------------------------------- selection
def neighbours(cf: pd.DataFrame) -> np.ndarray:
    """Index array [n_cfg, 7] of each configuration and its neighbours (adjacent delta, wing, entry; same family,
    management and filter), -1 padded. LAD has no entry neighbours."""
    key = {(b, e, m, f): i for i, (b, e, m, f) in enumerate(cf[["b", "e", "m", "f"]].to_numpy())}
    bs = sorted(set(cf["b"]))
    fam_of = {b: base_fam(b) for b in bs}
    dw = {b: base_dw(b) for b in bs}
    by_fd = {(fam_of[b], dw[b]): b for b in bs}
    deltas = sorted({x[0] for x in dw.values()})
    wings = sorted({x[1] for x in dw.values()})
    out = np.full((len(cf), 7), -1, int)
    for i, (b, e, m, f) in enumerate(cf[["b", "e", "m", "f"]].to_numpy()):
        d, w = dw[b]
        nb = [i]
        for dd in (-1, 1):
            j = deltas.index(d) + dd
            if 0 <= j < len(deltas):
                b2 = by_fd.get((fam_of[b], (deltas[j], w)))
                if b2 is not None and (b2, e, m, f) in key:
                    nb.append(key[(b2, e, m, f)])
            j = wings.index(w) + dd
            if 0 <= j < len(wings):
                b2 = by_fd.get((fam_of[b], (d, wings[j])))
                if b2 is not None and (b2, e, m, f) in key:
                    nb.append(key[(b2, e, m, f)])
            if e != LAD and 0 <= e + dd < Z.NE and (b, e + dd, m, f) in key:
                nb.append(key[(b, e + dd, m, f)])
        out[i, :len(nb)] = nb[:7]
    return out


def smoothed(ir: np.ndarray, nb: np.ndarray) -> np.ndarray:
    v = np.where(nb >= 0, ir[np.maximum(nb, 0)], np.nan)
    with np.errstate(all="ignore"):
        return np.nanmean(v, axis=1)


def pick(score, elig, cf) -> int:
    """Highest score among eligible configurations; ties: ALL filter first, then H, then the lowest index."""
    s = np.where(elig & np.isfinite(score), score, -np.inf)
    if not np.isfinite(s).any():
        return -1
    tied = np.flatnonzero(s >= np.max(s) - 1e-12)
    if len(tied) == 1:
        return int(tied[0])
    f, m = cf["f"].to_numpy()[tied], cf["m"].to_numpy()[tied]
    order = np.lexsort((tied, (m != 0).astype(int), (f != 0).astype(int)))
    return int(tied[order[0]])


def eligible(R, T, Of, sl, zdte, hashes=None) -> np.ndarray:
    nz = int(zdte[sl].sum())
    feas = Of[:, sl].sum(axis=1) >= 0.5 * max(nz, 1)
    trades = T[:, sl].sum(axis=1) >= max(100, 0.25 * nz)
    h = row_hash(R[:, sl]) if hashes is None else hashes
    dup = pd.Series(h).duplicated().to_numpy()
    return feas & trades & ~dup


def ir_of(Rw: np.ndarray) -> np.ndarray:
    Rw = np.asarray(Rw, np.float64)
    sd = Rw.std(axis=1, ddof=1)
    return np.where(sd > 0, Rw.mean(axis=1) / np.where(sd > 0, sd, 1), 0.0)


def scale_of(u_traded: np.ndarray, p_expected: float, kind: int) -> float:
    """Ex-ante sleeve scale (ledger 0.12): the per-traded-day sd of the unit series in the selection window, scaled
    by the expected share of traded days in the coming year, set to SLEEVE_SD, capped by kind."""
    if len(u_traded) < 2 or p_expected <= 0:
        return 0.0
    sd = float(np.std(u_traded, ddof=1)) * math.sqrt(p_expected)
    return min(SLEEVE_SD / sd, CAPS[kind]) if sd > 0 else 0.0


def walk_forward(R, Rc, T, X, Of, cf, kinds_cfg, days, zdte, sched_share, universe, nb) -> tuple[dict, dict]:
    years = range(2016, int(END[:4]) + 1)
    nd = len(days)
    sleeve, sleeve_c, xsel = np.zeros(nd), np.zeros(nd), np.zeros(nd)
    pick_day = np.full(nd, -1)
    picks = []
    for y in years:
        sel = days < pd.Timestamp(f"{y}-01-01")
        test = (days >= pd.Timestamp(f"{y}-01-01")) & (days <= pd.Timestamp(f"{y}-12-31"))
        el = universe & eligible(R, T, Of, sel, zdte)
        sc = smoothed(ir_of(R[:, sel]), nb)
        i = pick(sc, el, cf)
        if i < 0:
            picks.append({"year": y, "config": None})
            continue
        tr = T[i, sel]
        nz = max(int(zdte[sel].sum()), 1)
        p_exp = (tr.sum() / nz) * sched_share[y]                     # traded share of 0DTE days x 0DTE share of y
        s = scale_of(R[i, sel][tr].astype(np.float64), p_exp, kinds_cfg[i])
        sleeve[test] = s * R[i, test]
        sleeve_c[test] = s * Rc[i, test]
        xsel[test] = s * X[i, test]
        pick_day[test] = i
        picks.append({"year": y, "config": cf.loc[i, "name"], "i": int(i), "score": float(sc[i]),
                      "scale": s, "n_eligible": int(el.sum()), "test_mean_bp": float(sleeve[test].mean() * 1e4),
                      "test_sd_bp": float(sleeve[test].std() * 1e4), "test_trades": int(T[i, test].sum())})
    oos = days >= pd.Timestamp("2016-01-01")
    e3 = days >= pd.Timestamp(ERAS["E3"][0])
    r, rc, x = sleeve[oos], sleeve_c[oos], xsel[oos]
    _, a, at = alpha_t(r[None, :], x[None, :])
    res = {"picks": picks, "t": float(nw_t(r[None, :])[0]), "mean_bp": float(r.mean() * 1e4),
           "sd_bp": float(r.std() * 1e4), "e3_mean_bp": float(sleeve[e3].mean() * 1e4),
           "e3_t": float(nw_t(sleeve[e3][None, :])[0]), "e3_sd_bp": float(sleeve[e3].std() * 1e4),
           "alpha_bp": float(a[0] * 1e4), "alpha_t": float(at[0]),
           "cons_mean_bp": float(rc.mean() * 1e4), "cons_t": float(nw_t(rc[None, :])[0])}
    res["checks"] = {"a_t": res["t"] >= T_OOS, "b_e3_positive": res["e3_mean_bp"] > 0,
                     "c_alpha_t": res["alpha_t"] >= T_OOS,
                     "d_conservative": res["cons_mean_bp"] > 0 and res["cons_t"] >= 1.96}
    res["pass"] = bool(all(res["checks"].values()))
    return res, {"sleeve": sleeve, "sleeve_c": sleeve_c, "pick_day": pick_day}


def two_fold(R, T, Of, cf, days, zdte, universe, nb, only: np.ndarray | None = None) -> dict:
    q = days.quarter
    base = np.ones(len(days), bool) if only is None else only
    folds = {"X": np.isin(q, (1, 3)) & base, "Y": np.isin(q, (2, 4)) & base}
    out, pooled = {}, []
    for a, b in (("X", "Y"), ("Y", "X")):
        el = universe & eligible(R, T, Of, folds[a], zdte)
        i = pick(smoothed(ir_of(R[:, folds[a]]), nb), el, cf)
        if i < 0:
            out[f"{a}->{b}"] = None
            continue
        rt = R[i, folds[b]].astype(np.float64)
        out[f"{a}->{b}"] = {"config": cf.loc[i, "name"], "test_t": float(nw_t(rt[None, :])[0]),
                            "test_mean_bp": float(rt.mean() * 1e4), "test_trades": int(T[i, folds[b]].sum())}
        pooled.append(rt / max(float(np.std(R[i, folds[a]], ddof=1)), 1e-12))
    if len(pooled) == 2:
        out["pooled_t"] = float(nw_t(np.concatenate(pooled)[None, :])[0])
    return out


def pbo(R, T, Of, cf, zdte, universe, nb, n_blocks: int = 16) -> dict:
    """Combinatorially symmetric cross-validation (ledger 0.6.5): 16 contiguous blocks with equal 0DTE-day counts,
    every 8/8 split, the registered selection rule (eligibility, smoothed IR over the full grid, tie-break) in the
    in-sample half, the pick's rank among the universe's out-of-sample IRs (0 for a series with no variance)."""
    n_cfg, nd = R.shape
    cz = np.cumsum(zdte)
    edges = np.searchsorted(cz, np.linspace(0, cz[-1], n_blocks + 1)[1:-1], side="right")
    blk = np.zeros(nd, int)
    for k, e in enumerate(edges):
        blk[e:] = k + 1
    R64 = R.astype(np.float64)
    S1 = np.stack([R64[:, blk == k].sum(axis=1) for k in range(n_blocks)], 1)
    S2 = np.stack([(R64[:, blk == k] ** 2).sum(axis=1) for k in range(n_blocks)], 1)
    N = np.array([(blk == k).sum() for k in range(n_blocks)])
    TR = np.stack([T[:, blk == k].sum(axis=1) for k in range(n_blocks)], 1)
    OF = np.stack([Of[:, blk == k].sum(axis=1) for k in range(n_blocks)], 1)
    NZ = np.array([zdte[blk == k].sum() for k in range(n_blocks)])
    H = np.stack([row_hash(R[:, blk == k]) for k in range(n_blocks)], 1).astype(np.uint64)
    mult = np.random.default_rng(SEED).integers(1, 2**62, n_blocks, dtype=np.int64).astype(np.uint64)
    uni = np.flatnonzero(universe)

    def irs(mask):
        n = N[mask].sum()
        s1, s2 = S1[:, mask].sum(axis=1), S2[:, mask].sum(axis=1)
        var = (s2 - s1 * s1 / n) / (n - 1)
        return np.where(var > 1e-30, (s1 / n) / np.sqrt(np.maximum(var, 1e-300)), 0.0)

    lam = []
    for comb in itertools.combinations(range(n_blocks), n_blocks // 2):
        ins = np.zeros(n_blocks, bool)
        ins[list(comb)] = True
        nz = NZ[ins].sum()
        hk = (H[:, ins] * mult[ins]).sum(axis=1)                       # combined in-sample hash (mod 2^64)
        dup = pd.Series(hk).duplicated().to_numpy()
        el = (OF[:, ins].sum(axis=1) >= 0.5 * max(nz, 1)) & (TR[:, ins].sum(axis=1) >= max(100, 0.25 * nz)) & ~dup
        j = pick(smoothed(irs(ins), nb), universe & el, cf)
        if j < 0:
            continue
        o_all = irs(~ins)[uni]
        o = irs(~ins)[j]
        n_u = len(uni)
        rank = (o_all < o).sum() + 1 + 0.5 * ((o_all == o).sum() - 1)
        w = rank / (n_u + 1)
        lam.append(math.log(w / (1 - w)))
    lam = np.array(lam)
    return {"splits": int(len(lam)), "pbo": float((lam <= 0).mean()) if len(lam) else None,
            "lambda_median": float(np.median(lam)) if len(lam) else None,
            "lambda_q": {str(q): float(np.quantile(lam, q)) for q in (0.1, 0.25, 0.75, 0.9)} if len(lam) else {}}


def bootstrap_family(R, T, dup, n_boot: int = 2000, block: float = 10.0) -> dict:
    """Stationary-bootstrap max-t under zero means with the full-sample Newey-West standard error (the t of the
    table), the same day draws for every configuration, over the evaluable family (>= 100 trades, not duplicate);
    Romano-Wolf step-down rejections at 5%."""
    keep = (T.sum(axis=1) >= 100) & ~dup
    if not keep.any():
        return {"family": 0}
    Rd = np.asarray(R[keep], np.float32)
    n = Rd.shape[1]
    mu = Rd.mean(axis=1, dtype=np.float64)
    se = np.sqrt(np.maximum(nw_var(Rd.astype(np.float64) - mu[:, None]), 1e-300) / n)
    t_obs = mu / se
    rng = np.random.default_rng(SEED)
    order = np.argsort(-t_obs)
    maxsuf = np.empty((n_boot, len(order)), np.float32)
    for bstart in range(0, n_boot, 100):
        nb = min(100, n_boot - bstart)
        W = np.zeros((n, nb), np.float32)
        for k in range(nb):
            W[:, k] = np.bincount(stationary_bootstrap_index(n, block, rng), minlength=n)
        m1 = (Rd @ W) / n
        tb = (m1 - mu[:, None].astype(np.float32)) / se[:, None].astype(np.float32)
        maxsuf[bstart:bstart + nb] = np.maximum.accumulate(tb[order].T[:, ::-1], axis=1)[:, ::-1]
    crit = np.quantile(maxsuf, 0.95, axis=0)
    rejected = 0
    for k, i in enumerate(order):
        if t_obs[i] >= crit[k]:
            rejected += 1
        else:
            break
    return {"family": int(keep.sum()), "max_t_95": float(crit[0]), "romano_wolf_rejections": rejected,
            "top_t": float(t_obs[order[0]])}


def leader_clusters(R, rho: float = 0.9) -> int:
    Zm = np.asarray(R, np.float64)
    Zm = Zm - Zm.mean(axis=1, keepdims=True)
    nrm = np.linalg.norm(Zm, axis=1)
    Zm = Zm[nrm > 0] / nrm[nrm > 0, None]
    leaders = np.zeros((len(Zm), Zm.shape[1]))
    k = 0
    for z in Zm:
        if k and (leaders[:k] @ z).max() >= rho:
            continue
        leaders[k] = z
        k += 1
    return int(k)


# ---------------------------------------------------------------------------------------------------- reports
def ann(r) -> float:
    r = np.asarray(r, float)
    return float(np.prod(1 + r) ** (252 / max(len(r), 1)) - 1)


def mdd(r) -> float:
    nav = np.cumprod(1 + np.asarray(r, float))
    return float((nav / np.maximum.accumulate(nav) - 1).min())


def overlay_report(sleeve: np.ndarray, bench: pd.DataFrame, days: pd.DatetimeIndex, sl: np.ndarray) -> dict:
    one = bench["oneq"].to_numpy()[sl]
    lev = 1.3 * one - 0.3 * (bench["rf"].to_numpy()[sl] + 0.015 / 252)
    ov = one + sleeve[sl]
    mon = lambda r: pd.Series(r, index=days[sl]).groupby(days[sl].to_period("M")).apply(lambda x: np.prod(1 + x) - 1)
    out = {"days": int(sl.sum()), "overlay_cagr": ann(ov), "oneq_cagr": ann(one), "oneq13_cagr": ann(lev),
           "overlay_mdd": mdd(ov), "oneq_mdd": mdd(one), "oneq13_mdd": mdd(lev),
           "overlay_worst_month": float(mon(ov).min()), "oneq_worst_month": float(mon(one).min()),
           "oneq13_worst_month": float(mon(lev).min()),
           "sleeve_sd_bp": float(np.std(sleeve[sl]) * 1e4), "extra_risk_13_sd_bp": float(np.std(0.3 * one) * 1e4)}
    if LADDER.exists():
        lad = pd.read_csv(LADDER)
        lad.index = pd.PeriodIndex(lad["date"], freq="M")
        mo = mon(ov)
        common = mo.index.intersection(lad.index)
        if len(common) >= 12 and "BTC10" in lad:
            out["months_vs_ladder"] = int(len(common))
            out["overlay_cagr_monthly"] = float(np.prod(1 + mo[common]) ** (12 / len(common)) - 1)
            out["qqq_btc10_cagr"] = float(np.prod(1 + lad.loc[common, "BTC10"]) ** (12 / len(common)) - 1)
            out["m13_cagr_ladder"] = float(np.prod(1 + lad.loc[common, "M1.3"]) ** (12 / len(common)) - 1)
            out["qqq_btc10_worst_month"] = float(lad.loc[common, "BTC10"].min())
    return out


def calibration(P: Z.Panel, U, bench, under: str) -> dict:
    """Replicas of live 0DTE funds (ledger 0.6.7): the first half picks the delta, the second half reports the gap."""
    def fund_tr(t, end):
        df = parse_chart(FUNDS / f"{t}.json", end).set_index("date")
        df.index = pd.DatetimeIndex(df.index)
        return ((df["close"] + df["dividend"]) / df["close"].shift(1) - 1).dropna()
    idx = P.days.index
    cases = {"QQQ": [("QDTE", "2024-03-08", END, 5, (0.16, 0.25), "qqq", 0.0095),
                     ("QQQY", "2023-09-14", "2025-12-16", 4, (0.50,), "rf", 0.0099)],
             "SPXW": [("XDTE", "2024-03-08", END, 5, (0.16, 0.25), "spy", 0.0095),
                      ("WDTE", "2023-09-19", "2025-12-16", 4, (0.50,), "rf", 0.0099)]}[under]
    out = {}
    for fund, a, b, fam, deltas, base, fee in cases:
        fr = fund_tr(fund, b)
        sl = (idx >= pd.Timestamp(a)) & (idx <= pd.Timestamp(b))
        days = idx[sl]
        br = bench[base].to_numpy()[sl]
        f_r = fr.reindex(days)
        ok = f_r.notna().to_numpy()
        f_r = f_r.fillna(0).to_numpy()
        half = len(days) // 2
        best = None
        for d in deltas:
            s = Z.SHAPES.index((fam, d, 0.0))
            rep = br + U[s, 0, 0, sl]                    # notional 1x, 09:45 entry, held to the flatten / close
            te = float(np.std((rep - f_r)[:half][ok[:half]]))
            if best is None or te < best[0]:
                best = (te, d, rep)
        te, d, rep = best
        g = (rep - f_r - fee / 252)[half:][ok[half:]]
        out[fund] = {"delta": d, "first_half_te": te, "second_half_gap_ann": float(g.mean() * 252),
                     "gap_t": float(nw_t(g[None, :])[0]),
                     "corr": float(np.corrcoef(rep[half:][ok[half:]], f_r[half:][ok[half:]])[0, 1]),
                     "days": int(len(days)), "missing_fund_days": int((~ok).sum())}
    out["optimistic_fills"] = bool(any(v["second_half_gap_ann"] > 0.03 for v in out.values() if isinstance(v, dict)))
    return out


def replay_10k(P: Z.Panel, plan: list[tuple], bench: pd.DataFrame, sl: np.ndarray) -> dict:
    """$10k with integer contracts over the days in sl (ledger 0.6.8, 0.12). plan: per day index d the configuration
    (b, e, m, mask) to trade, or None. Defined-risk structures only; a composite trades only when both sides opened
    and is sized as one unit; LAD gives each entry a fifth of the 2% budget."""
    days = np.flatnonzero(sl)
    px = np.cumprod(1 + bench["oneq"].to_numpy())          # ONEQ total-return index (day d close)
    rf = bench["rf"].to_numpy()
    d0 = days[0]
    p0 = px[d0] / (1 + bench["oneq"].to_numpy()[d0])        # previous close: day 1's ONEQ return is earned
    sh = math.floor(10_000.0 / p0)
    cash = 10_000.0 - sh * p0
    nav = 10_000.0
    navs, skip, trades, fees_paid, credit, worst = [], 0, 0, 0.0, 0.0, 0.0
    for d in days:
        cash *= 1 + (rf[d] if cash > 0 else rf[d] + 0.015 / 252)
        pnl_day = 0.0
        cfg = plan[d]
        if cfg is not None:
            b, e, m, mask = cfg
            if b >= Z.NS:
                sides, mi = [COMP[b - Z.NS]["put"], COMP[b - Z.NS]["call"]], 2
            else:
                sides, mi = [b], m
            entries = range(Z.NE) if e == LAD else [e]
            budget = 0.02 / len(entries)
            used = 0.0
            for ee in entries:
                if not mask[ee, d] or not all(P.opened[s, ee, d] for s in sides):
                    continue
                if len(sides) == 2:
                    maxloss = (max(P.width[sides[0], ee, d], P.width[sides[1], ee, d])
                               - P.net[sides[0], ee, d] - P.net[sides[1], ee, d]) * 100.0
                else:
                    maxloss = (P.width[sides[0], ee, d] - P.net[sides[0], ee, d]) * 100.0
                n = math.floor(budget * nav / max(maxloss, 1e-9))
                if n == 0:
                    if maxloss <= 0.03 * nav - used:
                        n = 1
                    else:
                        skip += 1
                        continue
                used += n * maxloss
                for s in sides:
                    legs = P.n_legs[s, ee, d]
                    tier_e = P.fee_entry[s, ee, d] * 100 - legs * Z.SIM.OTHER_FEE
                    tier_x = P.fee_flat[s, ee, d] * 100
                    extra = max(0.0, 1.0 - n * tier_e) + (max(0.0, 1.0 - n * tier_x) if tier_x > 0 else 0.0)
                    pnl_day += n * 100 * P.pnl[s, ee, mi, d] - extra
                    fees_paid += n * (P.fee_entry[s, ee, d] + P.fee_flat[s, ee, d]) * 100 + extra
                    credit += n * 100 * max(P.net[s, ee, d], 0)
                trades += 1
        cash += pnl_day
        nav_new = cash + sh * px[d]
        worst = min(worst, pnl_day / max(nav, 1))
        if abs(cash) > 0.02 * nav_new:
            tgt = math.floor(nav_new / px[d])
            cash -= (tgt - sh) * px[d]
            sh = tgt
        nav = nav_new
        navs.append(nav)
    navs = np.array(navs)
    r = np.r_[navs[0] / 10_000.0 - 1, navs[1:] / navs[:-1] - 1]
    one = bench["oneq"].to_numpy()[sl]
    return {"days": int(len(days)), "cagr": ann(r), "oneq_cagr": ann(one), "mdd": mdd(r), "oneq_mdd": mdd(one),
            "trades": trades, "skipped": skip, "worst_day": worst,
            "fees_over_credit": fees_paid / max(credit, 1e-9), "beats_oneq": bool(ann(r) > ann(one))}


# ---------------------------------------------------------------------------------------------------- main
def files_for(under: str) -> list[Path]:
    return sorted(RAW.glob(f"zero_dte_{under}_*.json.gz"))


def check_inputs(under: str, metas: list[dict], P: Z.Panel, allow_partial: bool) -> dict:
    """The exports must be the registered runs (ledger 0.7) and cover every session of the period."""
    probs = []
    for m in metas:
        rt = m.get("runtime", {})
        if m.get("under") != under:
            probs.append(f"run for {m.get('under')} in a {under} analysis")
        if m.get("sim") != SIM_HASH:
            probs.append(f"{m.get('name')}: sim {m.get('sim')} is not the frozen {SIM_HASH}")
        run = rt.get("RUN", "")
        if "mech0" not in run or "left 0" not in run or "final 1" not in run or rt.get("ERR") != "none":
            probs.append(f"{m.get('name')}: RUN/ERR not clean ({run[-60:]} | {rt.get('ERR')})")
    have = {tuple(m.get("name", "").split(" ")[-1].split("..")) for m in metas}
    missing = [c for c in CHUNKS[under] if c not in have]
    if missing:
        probs.append(f"missing chunks {missing}")
    want = sessions(under)
    got = P.days.index
    miss_days = want.difference(got)
    extra = got.difference(want)
    if len(miss_days) or len(extra):
        probs.append(f"{len(miss_days)} sessions missing (e.g. {list(miss_days[:3])}), {len(extra)} extra")
    if probs and not allow_partial:
        raise ValueError("; ".join(probs))
    z = P.days["zdte"].to_numpy() == 1
    st = P.days["status"].to_numpy()
    nrows = P.code.max(axis=(0, 1)) >= 0
    return {"problems": probs, "zdte_status_not_ok": int((z & (st != 0)).sum()),
            "zdte_without_rows": int((z & (st == 0) & ~nrows).sum()),
            "covmiss_days": int(((P.code == 3).any(axis=(0, 1))).sum())}


def qqq_calendar() -> pd.DatetimeIndex:
    """QQQ's same-day-expiry days (from the QQQ exports): the SPXW reference run trades only on these (ledger 0.2)."""
    d, _, _ = Z.read_exports(files_for("QQQ"))
    return pd.DatetimeIndex(d.loc[d["zdte"] == 1, "date"])


def analyse(under: str, files: list[Path], quick: bool = False, allow_partial: bool = False) -> dict:
    days, rows, metas = Z.read_exports(files, under=under)
    days = days[(days["date"] >= START) & (days["date"] <= END)]
    rows = rows[rows["date"].isin(days["date"])]
    if under == "SPXW":
        qd = qqq_calendar()
        rows = rows[rows["date"].isin(qd)]
        days = days.assign(zdte=np.where(days["date"].isin(qd), days["zdte"], 0))
    P = Z.build_panel(days, rows, under)
    inputs = check_inputs(under, metas, P, allow_partial)
    D = P.days.index
    zdte = P.days["zdte"].to_numpy().astype(bool) & (P.days["status"].to_numpy() == 0)
    bench = benchmarks(D, under)
    masks = filter_masks(P, bench, under)
    U, O, kinds = base_units(P, "primary")
    Uc, _, _ = base_units(P, "cons")
    Ua, _, _ = base_units(P, "alt")
    cf = config_list()
    kinds_cfg = np.array([kinds[b] for b in cf["b"]])
    R, T = matrix(U, O, masks, cf)
    Rc, _ = matrix(Uc, O, masks, cf)
    Ra, _ = matrix(Ua, O, masks, cf)
    X = xret_matrix(P, O, masks, cf)
    Of = np.zeros_like(T)
    for i, (b, e) in enumerate(cf[["b", "e"]].to_numpy()):
        Of[i] = O[b].any(axis=0) if e == LAD else O[b, e]
    st = grid_stats(R, T, X, D)
    table = pd.concat([cf, st, pd.DataFrame({"t_alt": nw_t(Ra), "mean_alt_bp": Ra.mean(axis=1, dtype=np.float64) * 1e4,
                                             "t_cons": nw_t(Rc), "mean_cons_bp": Rc.mean(axis=1, dtype=np.float64) * 1e4})],
                      axis=1)
    nb = neighbours(cf)
    # scheduled share of 0DTE days per year (the expiry calendar is published in advance)
    yrs = pd.Series(zdte, index=D).groupby(D.year).mean()
    sched = {int(y): float(v) for y, v in yrs.items()}
    res = {"under": under, "files": [f.name for f in files],
           "file_sha256": {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in files},
           "inputs": inputs, "days": int(len(D)), "zdte_days": int(zdte.sum()),
           "eras_zdte": {k: int(zdte[(D >= a) & (D <= b)].sum()) for k, (a, b) in ERAS.items()},
           "configs": int(len(cf)), "A": int(table["A"].sum()), "A_distinct": int((table["A"] & ~table["dup"]).sum())}
    uni_k10, uni_all = cf["k10"].to_numpy(), np.ones(len(cf), bool)
    wf_k10, sk = walk_forward(R, Rc, T, X, Of, cf, kinds_cfg, D, zdte, sched, uni_k10, nb)
    wf_all, sa = walk_forward(R, Rc, T, X, Of, cf, kinds_cfg, D, zdte, sched, uni_all, nb)
    res["walk_forward"] = {"K10": wf_k10, "ALL": wf_all}
    fri = masks["FRI"][0]
    res["two_fold"] = {"K10": two_fold(R, T, Of, cf, D, zdte, uni_k10, nb),
                       "ALL": two_fold(R, T, Of, cf, D, zdte, uni_all, nb),
                       "K10_fridays": two_fold(R, T, Of, cf, D, zdte, uni_k10, nb, only=fri)}
    oos = D >= pd.Timestamp("2016-01-01")
    e3 = D >= pd.Timestamp(ERAS["E3"][0])
    res["equal_risk"] = {"oos": overlay_report(sk["sleeve"], bench, D, oos),
                         "E3": overlay_report(sk["sleeve"], bench, D, e3),
                         "oos_cons": overlay_report(sk["sleeve_c"], bench, D, oos)}
    # minimum detectable effect (bp of the unit series per day, per family; and for the realised K10 sleeve)
    n = len(D)
    lr = np.sqrt(np.maximum(nw_var(R.astype(np.float64) - R.mean(axis=1, dtype=np.float64)[:, None]), 0))
    evaluable = (table["trades"] >= 100) & ~table["dup"]
    res["mde"] = {"per_family_unit_bp": {f: float(np.median(lr[(table["fam"] == f) & evaluable]) *
                                                  (T_GRID + 0.84) / math.sqrt(n) * 1e4)
                                         for f in sorted(table["fam"].unique()) if ((table["fam"] == f) & evaluable).any()},
                  "oos_sleeve_bp": float(math.sqrt(max(nw_var((sk["sleeve"][oos] - sk["sleeve"][oos].mean())[None, :])[0], 0))
                                         * (T_OOS + 0.84) / math.sqrt(int(oos.sum())) * 1e4)}
    if not quick:
        res["pbo_K10"] = pbo(R, T, Of, cf, zdte, uni_k10, nb)
        res["bootstrap"] = bootstrap_family(R, T, table["dup"].to_numpy())
        ev = np.flatnonzero(evaluable.to_numpy())
    if not quick and len(ev):
        top = int(ev[np.argmax(table["ir"].to_numpy()[ev])])
        r = R[top].astype(np.float64)
        sk_, ku = float(pd.Series(r).skew()), float(pd.Series(r).kurt()) + 3
        famdm = (table["ir"] - table.groupby("fam")["ir"].transform("mean"))[evaluable]
        samp = np.random.default_rng(SEED).choice(ev, size=min(len(ev), 3000), replace=False)
        n_eff = int(round(leader_clusters(R[samp]) * len(ev) / len(samp)))
        res["dsr"] = {"config": cf.loc[top, "name"], "ir": float(table.loc[top, "ir"]), "n_eff": n_eff,
                      "N": deflated_sharpe_n(float(table.loc[top, "ir"]), len(r), sk_, ku, N_TRIALS, float(famdm.var())),
                      "N_eff": deflated_sharpe_n(float(table.loc[top, "ir"]), len(r), sk_, ku, max(2, n_eff),
                                                 float(famdm.var()))}
    # named comparators (0.6.6)
    name_i = {nm: i for i, nm in enumerate(cf["name"])}
    named = {"breakeven_IC": "IC-BE0.10-W0.5%|LAD|BE|ALL", "tastylive_IC": "IC0.16-W0.5%|09:45|M3|ALL",
             "optionalpha_IF": "IF-W0.5%|12:00|M3|ALL"}
    res["named"] = {}
    for k, nm in named.items():
        i = name_i[nm]
        res["named"][k] = {"config": nm, **{c: float(table.loc[i, c]) for c in
                                            ("mean_bp", "t", "alpha_t", "mean_E3_bp", "t_E3", "mean_cons_bp", "trades")}}
    mwf = D.weekday.isin((0, 2, 4)) & (D >= pd.Timestamp(ERAS["E2"][0]))
    for side, nm in (("put", "PCS0.05-W1|09:45|M2|ALL"), ("call", "CCS0.05-W1|09:45|M2|ALL")):
        r = R[name_i[nm]][mwf].astype(np.float64)
        res["named"][f"chambless_{side}"] = {"config": nm + " (Mon/Wed/Fri, E2+E3)", "mean_bp": float(r.mean() * 1e4),
                                             "t": float(nw_t(r[None, :])[0])}
    sc025 = Z.SHAPES.index((5, 0.25, 0.0))
    sp050 = Z.SHAPES.index((4, 0.50, 0.0))
    qdte = bench["oneq"].to_numpy() + U[sc025, 0, 0]
    qqqy = bench["rf"].to_numpy() + U[sp050, 0, 0]
    res["named"]["QDTE_like"] = {"what": "ONEQ + SC0.25 09:45 H, notional 1x", "cagr": ann(qdte),
                                 "oneq_cagr": ann(bench["oneq"].to_numpy()), "cagr_E3": ann(qdte[e3]),
                                 "oneq_cagr_E3": ann(bench["oneq"].to_numpy()[e3])}
    res["named"]["QQQY_like"] = {"what": "cash + SP0.50 09:45 H, notional 1x", "cagr": ann(qqqy), "cagr_E3": ann(qqqy[e3]),
                                 "mdd": mdd(qqqy)}
    rrb = Z.SHAPES.index((10, 0.50, 0.0))
    sdh = Z.SHAPES.index((11, 0.50, 0.0))
    res["named"]["intraday_drift_RRB_bp"] = {"%02d:%02d" % Z.ENTRY[e]: float(U[rrb, e, 0].mean() * 1e4) for e in range(Z.NE)}
    res["named"]["delta_hedged_straddle_SDH"] = {"%02d:%02d" % Z.ENTRY[e]: {"mean_bp": float(U[sdh, e, 0].mean() * 1e4),
                                                                            "t": float(nw_t(U[sdh, e, 0][None, :])[0])}
                                                 for e in range(Z.NE)}
    try:
        res["calibration"] = calibration(P, U, bench, under)
    except Exception as ex:
        res["calibration"] = {"error": str(ex)[:200]}
    # $10k replay over E3: one continuous account, each year's K10 pick (the 2022 pick for 2022-11-14..12-31)
    plan = [None] * len(D)
    for d in np.flatnonzero(e3):
        i = sk["pick_day"][d]
        if i >= 0:
            b, e, m, f = cf.loc[i, ["b", "e", "m", "f"]]
            plan[d] = (int(b), int(e), int(m), mask_for(masks, FILTERS[int(f)], int(b)))
    res["replay_10k"] = {"K10_walk_forward": replay_10k(P, plan, bench, e3)}
    for k, nm in (("breakeven_IC", named["breakeven_IC"]), ("tastylive_IC", named["tastylive_IC"]),
                  ("optionalpha_IF", named["optionalpha_IF"])):
        b, e, m, f = cf.loc[name_i[nm], ["b", "e", "m", "f"]]
        plan = [(int(b), int(e), int(m), mask_for(masks, FILTERS[int(f)], int(b))) if x else None for x in e3]
        res["replay_10k"][k] = replay_10k(P, plan, bench, e3)
    # scorecard of the K10 out-of-sample overlay (0.6.10)
    try:
        ov = pd.Series(bench["oneq"].to_numpy()[oos] + sk["sleeve"][oos], index=D[oos])
        card = scorecard(ov, freq="D", oneq=bench["oneq"][oos], qqq=bench["qqq"][oos], rf=bench["rf"][oos],
                         meta={"n_trials": N_TRIALS, "oos_status": "passed" if wf_k10["pass"] else "failed"})
        card.to_csv(OUT / f"scorecard_k10_{under}.csv", index=False)
        keys = {"excess_cagr_vs_oneq", "max_drawdown", "worst_month", "ce_crra_2"}
        res["scorecard_selected"] = {r_["key"]: r_["value"] for _, r_ in card.iterrows()
                                     if any(k in str(r_["key"]) for k in keys)}
    except Exception as ex:
        res["scorecard_selected"] = {"error": str(ex)[:200]}
    # GO table (0.10); the calibration rule switches the decision basis to the conservative execution
    k = wf_k10
    cal_opt = bool(res["calibration"].get("optimistic_fills", False)) if isinstance(res["calibration"], dict) else False
    er = res["equal_risk"]
    res["go"] = {"1_k10_oos_pass": bool(k["pass"]),
                 "2_pbo_below_half": bool(res.get("pbo_K10", {}).get("pbo") is not None and res["pbo_K10"]["pbo"] < 0.5),
                 "3_replay_beats_oneq_E3": bool(res["replay_10k"]["K10_walk_forward"]["beats_oneq"]),
                 "4_beats_oneq13_oos_and_E3": bool(er["oos"]["overlay_cagr"] > er["oos"]["oneq13_cagr"]
                                                   and er["E3"]["overlay_cagr"] > er["E3"]["oneq13_cagr"]),
                 "5_calibration_ok_or_conservative_passes": bool((not cal_opt) or k["checks"]["d_conservative"])}
    res["go"]["GO"] = bool(all(res["go"].values()))
    OUT.mkdir(parents=True, exist_ok=True)
    table.drop(columns=["b"]).to_csv(OUT / f"configs_{under}.csv.gz", index=False, float_format="%.5g")
    pd.DataFrame({"date": D, "sleeve_k10": sk["sleeve"], "sleeve_k10_cons": sk["sleeve_c"], "sleeve_all": sa["sleeve"],
                  "oneq": bench["oneq"].to_numpy()}).to_csv(OUT / f"walkforward_{under}.csv", index=False)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--under", choices=("QQQ", "SPXW"), default="QQQ")
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--quick", action="store_true", help="skip PBO, bootstrap and the deflated Sharpe")
    ap.add_argument("--allow-partial", action="store_true", help="mechanism smoke tests only")
    a = ap.parse_args(argv)
    files = [Path(f) for f in a.files] if a.files else files_for(a.under)
    if not files:
        raise SystemExit(f"no exports for {a.under} under {RAW}")
    res = analyse(a.under, files, a.quick, a.allow_partial)
    write_json(OUT / f"results_{a.under}.json", res)
    print(json.dumps({k: res[k] for k in ("under", "days", "zdte_days", "configs", "A", "go")}, indent=1, default=str))


if __name__ == "__main__":
    main()
