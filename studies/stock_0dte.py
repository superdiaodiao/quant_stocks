"""Single-stock 0DTE option grid: analysis of the QuantConnect exports (pre-registered in
docs/research_ledger_stock_0dte.md, 0.2-0.10).

Inputs (read only): output/research_only/stock_0dte/raw/stock_0dte_<first>_<last>.json.gz (decoded chart streams of
qc/stock_0dte/main.py, one file per registered chunk, decoded by quant.strategies.stock_0dte); the SEC 8-K earnings
events and the weekly ticker table of data version 2.1 (research_cache/reversal_2012_2026_v2_1); Yahoo daily charts in
research_cache (ONEQ, QQQ: benchmark returns and ONEQ's 200-day average); FRED DTB3 (cash); the risk ladder's monthly
returns (ONEQ x1.3 and QQQ + 10% BTC). Outputs: output/research_only/stock_0dte/ (statistics only).

Steps (ledger section): name-day filters and pools (0.2, 0.4), unit returns under the three settlement conventions and
the conservative execution (0.3, 0.5), pool-day series of every configuration and its $10k-feasible (K10) version
(0.4, 0.5), per-configuration statistics and in-grid A (0.6.1-0.6.3), the walk-forward selection that decides (0.6.4),
two-fold, PBO, bootstrap max-t / Romano-Wolf, deflated Sharpe, minimum detectable effect (0.6.5), named comparators
(0.6.6), the $10k integer replay (0.6.8), the equal-risk comparison (0.6.9), the scorecard (0.6.10) and the GO table
(0.10).

Usage:  PYTHONPATH=. .venv/bin/python -m studies.stock_0dte [--quick] [--allow-partial] [--files ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data.rates import load_dtb3
from quant.data.sources.yahoo import parse_ohlc
from quant.evaluation.criteria import deflated_sharpe_n
from quant.evaluation.scorecard import scorecard
from quant.paths import CACHE_ROOT, output_dir
from quant.reporting import write_json
from quant.strategies import stock_0dte as Q
from studies.zero_dte import (alpha_t, ann, bootstrap_family, ir_of, leader_clusters, mdd, nw_t, nw_var, pick,
                              row_hash, smoothed)

OUT = output_dir("stock_0dte")
RAW = OUT / "raw"
V21 = CACHE_ROOT / "reversal_2012_2026_v2_1"
EARN_FILE = V21 / "earnings" / "earnings_events.csv"
LISTED = V21 / "universe" / "weekly_listed.csv.gz"
YAHOO = CACHE_ROOT / "sector_lev" / "raw"
LADDER = output_dir("risk_ladder") / "monthly_returns_full.csv"
START, END = "2012-02-01", "2026-07-29"
ERAS = {"E1": ("2012-02-01", "2019-12-31"), "E2": ("2020-01-01", "2022-12-31"), "E3": ("2023-01-01", END)}
POOLS = (10, 20)
FILTERS = ("ALL", "EARN", "NOEARN", "GAP", "MOM", "RICH", "UP", "V20")
MGMT = ("H", "M1", "M2", "M3")
SIM_HASH = None                             # the frozen qc/stock_0dte/sim.py (first 16 hex of SHA-256), set at freeze
CHUNKS: list[tuple[str, str]] = []          # registered book chunks, set at freeze (ledger 0.7)
N_TRIALS = 31_392
T_OOS = 2.24
SLEEVE_SD = 0.005
CAPS = {0: 0.05, 1: 3.0, 2: 0.02, 3: 3.0, 4: 3.0, 5: 0.025}  # kind 5 = per-side-stop composites of verticals
K10_LOSS = 300.0                            # $ max loss (defined risk) or premium (debit) per contract
K10_CASH = 10_000.0                         # $ notional of one cash-secured put
SEED = 20261030
LAD = Q.NE
FAM = Q.FAM
SELECTABLE = [s for s in range(Q.NS) if FAM[Q.SHAPES[s][0]] not in ("RRB", "SDH")]
BULL = {"PCS", "SP", "RR", "RRB", "LC", "DVC"}
BEAR = {"CCS", "SC", "LP", "DVP"}
BUYERS = {"LD", "LC", "LP", "LG", "DVC", "DVP", "MPD"}
K10_FAMS = {"PCS", "CCS", "IC", "IF", "IFP", "IFM", "IC-BE", "IF-BE", "LC", "LP", "LG", "LD", "DVC", "DVP", "MPD", "SP"}


def bonferroni_t(n: int) -> float:
    from statistics import NormalDist
    return NormalDist().inv_cdf(1 - 0.025 / n)


T_GRID = bonferroni_t(N_TRIALS)


# ---------------------------------------------------------------------------------------------------- structures
def composites() -> list[dict]:
    """Per-side-stop structures assembled from single-side books at SL2 (ledger 0.4): IC-BE, IF-BE, SG-BE, SD-BE."""
    idx = {sh: i for i, sh in enumerate(Q.SHAPES)}
    out = []
    for d in Q.SIM.DELTAS:
        for w in Q.SIM.WINGS:
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
NB = Q.NS + len(COMP)


def base_name(b: int) -> str:
    return Q.shape_name(b) if b < Q.NS else COMP[b - Q.NS]["name"]


def base_fam(b: int) -> str:
    return FAM[Q.SHAPES[b][0]] if b < Q.NS else COMP[b - Q.NS]["fam"]


def base_dw(b: int) -> tuple[float, float]:
    if b < Q.NS:
        return Q.SHAPES[b][1], Q.SHAPES[b][2]
    return COMP[b - Q.NS]["d"], COMP[b - Q.NS]["w"]


def base_kind(b: int) -> int:
    return Q.KIND[Q.SHAPES[b][0]] if b < Q.NS else COMP[b - Q.NS]["kind"]


def base_units(P: Q.Panel, conv: str = "flat"):
    """Unit returns [NB, NE, 4, NK] (0 where not opened), the opened mask [NB, NE, NK] and the $10k feasibility mask
    [NB, NE, NK] (ledger 0.4 K10). conv: flat (primary) | hold | asn | cons (flat with conservative execution)."""
    if conv == "hold":
        pnl = P.hold
    elif conv == "asn":
        nm = P.names
        gap = (nm["next_open"] - nm["S_close"]).to_numpy()
        ok = np.isfinite(gap) & (nm["split_next"].to_numpy() == 0)
        live = np.concatenate([np.ones(P.xmin.shape[:2] + (1, P.xmin.shape[-1]), bool), P.xmin == 0], axis=2)
        pnl = P.hold + live * (P.nsh_hold[:, :, None, :] * np.where(ok, gap, 0.0)[None, None, None, :])
    else:
        pnl = P.pnl + (P.cons if conv == "cons" else 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        u = np.where(P.opened[:, :, None, :], pnl / P.den[:, :, None, :], 0.0)
    u = np.nan_to_num(u)
    nk = u.shape[-1]
    U = np.zeros((NB, Q.NE, 4, nk), np.float32)
    O = np.zeros((NB, Q.NE, nk), bool)
    F = np.zeros((NB, Q.NE, nk), bool)
    U[:Q.NS], O[:Q.NS] = u, P.opened
    with np.errstate(invalid="ignore"):
        for s in range(Q.NS):
            fam, kind = FAM[Q.SHAPES[s][0]], Q.KIND[Q.SHAPES[s][0]]
            if fam not in K10_FAMS:
                continue
            if kind == 0:
                F[s] = (P.width[s] - P.net[s]) * 100.0 <= K10_LOSS
            elif kind == 2:
                F[s] = -P.net[s] * 100.0 <= K10_LOSS
            elif fam == "SP":
                F[s] = P.s0 * 100.0 <= K10_CASH
    F &= O
    for j, c in enumerate(COMP):
        p, q = c["put"], c["call"]
        both = P.opened[p] & P.opened[q]
        pn = pnl[p, :, 2, :] + pnl[q, :, 2, :]
        if c["kind"] == 5:
            wmax = np.fmax(P.width[p], P.width[q])
            den = np.maximum(wmax - (P.net[p] + P.net[q]), Q.DEN_FLOOR * wmax)
            with np.errstate(invalid="ignore"):
                F[Q.NS + j] = both & ((wmax - P.net[p] - P.net[q]) * 100.0 <= K10_LOSS)
        else:
            den = P.s0
        with np.errstate(divide="ignore", invalid="ignore"):
            uc = np.where(both, pn / den, 0.0)
        U[Q.NS + j] = np.nan_to_num(uc)[:, None, :]          # the same series under every management index
        O[Q.NS + j] = both
    return U, O, F


# ---------------------------------------------------------------------------------------------------- data
def yahoo_frame(name: str) -> pd.DataFrame:
    df = parse_ohlc(YAHOO / f"chart_{name}.json", END).set_index("date")
    df.index = pd.DatetimeIndex(df.index)
    return df


def sessions() -> pd.DatetimeIndex:
    df = yahoo_frame("QQQ")
    return df.index[(df.index >= START) & (df.index <= END)]


def benchmarks(index: pd.DatetimeIndex) -> pd.DataFrame:
    """Benchmark day returns and ONEQ's 200-day trend on the sessions; raises on any missing ONEQ / QQQ day."""
    out = pd.DataFrame(index=index)
    for t in ("ONEQ", "QQQ"):
        df = yahoo_frame(t)
        out[t.lower()] = df["adjclose"].pct_change().reindex(index)
    if out[["oneq", "qqq"]].isna().any().any():
        raise ValueError(f"missing benchmark days: {out.index[out[['oneq', 'qqq']].isna().any(axis=1)][:5].tolist()}")
    rf = load_dtb3(END)
    rf.index = pd.DatetimeIndex(rf.index)
    out["rf"] = (rf.reindex(index, method="ffill") / 252.0).fillna(0.0)
    o = yahoo_frame("ONEQ")
    out["up"] = (o["adjclose"].shift(1) > o["adjclose"].rolling(200).mean().shift(1)).reindex(index).fillna(False)
    return out


def earnings_days(names: pd.DataFrame) -> np.ndarray:
    """Per name-day: 1 = first session after an earnings release (8-K Item 2.02, first results release of the fiscal
    quarter), 0 = not, -1 = unknown (the ticker has no SEC security that week, e.g. foreign filers)."""
    ev = pd.read_csv(EARN_FILE, dtype=str, usecols=["security_id", "event_kind", "first_in_fiscal_quarter", "d0_session"])
    ev = ev[(ev["event_kind"] == "results_release") & (ev["first_in_fiscal_quarter"] == "Y")]
    d0 = set(zip(ev["security_id"], pd.to_datetime(ev["d0_session"])))
    lst = pd.read_csv(LISTED, dtype=str, usecols=["week_end", "security_id", "ticker"])
    lst["week_end"] = pd.to_datetime(lst["week_end"])
    key = {(r.ticker, r.week_end): r.security_id for r in lst.itertuples()}
    out = np.full(len(names), -1, int)
    for i, (d, t) in enumerate(zip(pd.to_datetime(names["date"]), names["name"])):
        we = d + pd.Timedelta(days=(4 - d.weekday()) % 7)
        sid = key.get((t.replace(".", "-"), we)) or key.get((t, we)) or key.get((t.replace(".", ""), we))
        if sid is not None:
            out[i] = 1 if (sid, d) in d0 else 0
    return out


def trailing_by_name(names: pd.DataFrame, x: np.ndarray, n: int) -> np.ndarray:
    """Per entry row and name-day: mean of the name's last n valid observations before that day (NaN = invalid)."""
    out = np.full(x.shape, np.nan)
    order = names.sort_values(["name", "date"]).index.to_numpy()
    nm = names["name"].to_numpy()[order]
    for e in range(x.shape[0]):
        s = pd.Series(x[e, order])
        g = s.groupby(nm)
        r = g.transform(lambda v: v.dropna().rolling(n, min_periods=n).mean().reindex(v.index).ffill().shift(1))
        out[e, order] = r.to_numpy()
    return out


def filter_masks(P: Q.Panel, bench: pd.DataFrame) -> dict:
    """Boolean masks [NE, NK] per filter (MOM / RICH per side), on the name-days (ledger 0.4)."""
    nm = P.names
    nk = len(nm)
    one = np.ones((Q.NE, nk), bool)
    sig = nm["sig"].to_numpy()
    prev = nm["prev_close"].to_numpy().astype(float)
    prev = np.where(prev > 0, prev, np.nan)
    so = nm["S_open"].to_numpy().astype(float)
    so = np.where(so > 0, so, np.nan)
    gap = np.abs(so / prev - 1)
    S = np.where(P.s0 > 0, P.s0, np.nan)
    Sc = nm["S_close"].to_numpy().astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = S / prev[None, :] - 1
        move = np.abs(Sc[None, :] / S - 1)
        strad = nm[[f"strad_e{e}" for e in range(Q.NE)]].to_numpy().T
        rich_px = np.where(strad > 0, strad / S, np.nan)
    bad = (nm["half"].to_numpy() == 1) | (nm["status"].to_numpy() != 0)
    past = trailing_by_name(nm, np.where(bad[None, :], np.nan, move), 20)
    earn = earnings_days(nm)
    d = pd.DatetimeIndex(nm["date"])
    m = {"ALL": one, "EARN": np.broadcast_to(earn == 1, (Q.NE, nk)).copy(),
         "NOEARN": np.broadcast_to(earn != 1, (Q.NE, nk)).copy(),
         "GAP": np.broadcast_to(np.nan_to_num(gap, nan=1.0) <= 0.5 * np.nan_to_num(sig, nan=0.0), (Q.NE, nk)).copy(),
         "UP": np.broadcast_to(bench["up"].reindex(d).fillna(False).to_numpy(bool), (Q.NE, nk)).copy(),
         "V20": np.broadcast_to(np.nan_to_num(nm["vix_prev"].to_numpy(), nan=0.0) > 20, (Q.NE, nk)).copy()}
    ok = ~np.isnan(past) & ~np.isnan(rich_px)
    m["RICH_sell"] = ok & (np.nan_to_num(rich_px) >= np.nan_to_num(past))
    m["RICH_buy"] = ok & (np.nan_to_num(rich_px) < np.nan_to_num(past))
    m["MOM_bull"] = np.nan_to_num(r, nan=0) > 0
    m["MOM_bear"] = np.nan_to_num(r, nan=0) < 0
    m["MOM_neutral"] = ~np.isnan(r) & (np.abs(np.nan_to_num(r)) <= 0.5 * np.nan_to_num(sig, nan=0.0)[None, :])
    m["_earn_known"] = earn
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
    for pool in POOLS:
        for b in SELECTABLE:
            for e in range(Q.NE + 1):
                for mi in range(4):
                    for fi in range(len(FILTERS)):
                        rows.append((pool, b, e, mi, fi))
        for j in range(len(COMP)):
            for e in range(Q.NE + 1):
                for fi in range(len(FILTERS)):
                    rows.append((pool, Q.NS + j, e, 2, fi))
    cf = pd.DataFrame(rows, columns=["pool", "b", "e", "m", "f"])
    if len(cf) != N_TRIALS:
        raise ValueError(f"{len(cf)} configurations, registered {N_TRIALS}")
    cf["name"] = [f"P{p}|{base_name(b)}|{'LAD' if e == LAD else '%02d:%02d' % Q.ENTRY[e]}|"
                  f"{'BE' if b >= Q.NS else MGMT[m]}|{FILTERS[f]}" for p, b, e, m, f in cf[["pool", "b", "e", "m", "f"]].to_numpy()]
    cf["fam"] = [base_fam(b) for b in cf["b"]]
    cf["k10"] = cf["fam"].isin(K10_FAMS)
    cf["kind"] = [base_kind(b) for b in cf["b"]]
    return cf


class Grid:
    """Pool-day series of every configuration on the 0DTE sessions: the equal-weight mean of the unit returns of the
    pool's name-days that open the position that day (0 when none), for the full and the K10 version."""

    def __init__(self, P: Q.Panel, sess: pd.DatetimeIndex, masks: dict):
        self.P, self.sess, self.masks = P, sess, masks
        nm = P.names
        pos = pd.Series(np.arange(len(sess)), index=sess)
        self.si = pos.reindex(pd.DatetimeIndex(nm["date"])).to_numpy()
        if np.isnan(self.si.astype(float)).any():
            raise ValueError("name-days outside the session list")
        self.si = self.si.astype(int)
        self.rank = nm["rank"].to_numpy()

    def agg(self, u: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """u, w [NK] -> (weighted mean per session, traded flag)."""
        ns = len(self.sess)
        num = np.bincount(self.si, weights=u * w, minlength=ns)
        den = np.bincount(self.si, weights=w.astype(float), minlength=ns)
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.where(den > 0, num / np.where(den > 0, den, 1), 0.0)
        return r, den > 0

    def series(self, U, O, F, cfg, k10: bool, X=None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        pool, b, e, m, f = cfg
        mk = mask_for(self.masks, FILTERS[f], b)
        inpool = self.rank <= pool
        ok = F if k10 else O
        ents = range(Q.NE) if e == LAD else [e]
        r = np.zeros(len(self.sess))
        x = np.zeros(len(self.sess))
        t = np.zeros(len(self.sess), bool)
        for ee in ents:
            w = (ok[b, ee] & mk[ee] & inpool).astype(float)
            ri, ti = self.agg(U[b, ee, m].astype(np.float64), w)
            r += ri / len(ents)
            t |= ti
            if X is not None:
                xi, _ = self.agg(np.nan_to_num(X[ee]), w)
                x += xi / len(ents)
        return r, t, x

    def openable(self, O, cfg) -> np.ndarray:
        pool, b, e, m, f = cfg
        inpool = self.rank <= pool
        ents = range(Q.NE) if e == LAD else [e]
        o = np.zeros(len(self.sess), bool)
        for ee in ents:
            o |= self.agg(np.zeros(len(self.rank)), (O[b, ee] & inpool).astype(float))[1]
        return o


def matrices(G: Grid, U, Uc, O, F, cf, k10: bool, X):
    n, ns = len(cf), len(G.sess)
    R = np.zeros((n, ns), np.float32)
    Rc = np.zeros((n, ns), np.float32)
    T = np.zeros((n, ns), bool)
    Xm = np.zeros((n, ns), np.float32)
    Of = np.zeros((n, ns), bool)
    for i, cfg in enumerate(cf[["pool", "b", "e", "m", "f"]].to_numpy()):
        r, t, x = G.series(U, O, F, cfg, k10, X)
        R[i], T[i], Xm[i] = r, t, x
        Rc[i] = G.series(Uc, O, F, cfg, k10)[0]
        Of[i] = G.openable(F if k10 else O, cfg)
    return R, Rc, T, Xm, Of


# ---------------------------------------------------------------------------------------------------- statistics
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


def neighbours(cf: pd.DataFrame) -> np.ndarray:
    """Index array [n_cfg, 7] of each configuration and its neighbours (adjacent delta, wing, entry; same pool,
    family, management and filter), -1 padded. LAD has no entry neighbours."""
    key = {(p, b, e, m, f): i for i, (p, b, e, m, f) in enumerate(cf[["pool", "b", "e", "m", "f"]].to_numpy())}
    bs = sorted(set(cf["b"]))
    fam_of = {b: base_fam(b) for b in bs}
    dw = {b: base_dw(b) for b in bs}
    by_fd = {(fam_of[b], dw[b]): b for b in bs}
    deltas = sorted({x[0] for x in dw.values()})
    wings = sorted({x[1] for x in dw.values()})
    out = np.full((len(cf), 7), -1, int)
    for i, (p, b, e, m, f) in enumerate(cf[["pool", "b", "e", "m", "f"]].to_numpy()):
        d, w = dw[b]
        nb = [i]
        for dd in (-1, 1):
            j = deltas.index(d) + dd
            if 0 <= j < len(deltas):
                b2 = by_fd.get((fam_of[b], (deltas[j], w)))
                if b2 is not None and (p, b2, e, m, f) in key:
                    nb.append(key[(p, b2, e, m, f)])
            j = wings.index(w) + dd
            if 0 <= j < len(wings):
                b2 = by_fd.get((fam_of[b], (d, wings[j])))
                if b2 is not None and (p, b2, e, m, f) in key:
                    nb.append(key[(p, b2, e, m, f)])
            if e != LAD and 0 <= e + dd < Q.NE and (p, b, e + dd, m, f) in key:
                nb.append(key[(p, b, e + dd, m, f)])
        out[i, :len(nb)] = nb[:7]
    return out


def pick_cfg(score, elig, cf) -> int:
    """Highest score; ties: ALL filter, then H, then P10, then the lowest index (ledger 0.6.4)."""
    s = np.where(elig & np.isfinite(score), score, -np.inf)
    if not np.isfinite(s).any():
        return -1
    tied = np.flatnonzero(s >= np.max(s) - 1e-12)
    if len(tied) == 1:
        return int(tied[0])
    f, m, p = cf["f"].to_numpy()[tied], cf["m"].to_numpy()[tied], cf["pool"].to_numpy()[tied]
    order = np.lexsort((tied, (p != POOLS[0]).astype(int), (m != 0).astype(int), (f != 0).astype(int)))
    return int(tied[order[0]])


def eligible(R, T, Of, sl, zd, hashes=None) -> np.ndarray:
    nz = int(zd[sl].sum())
    feas = Of[:, sl].sum(axis=1) >= 0.5 * max(nz, 1)
    trades = T[:, sl].sum(axis=1) >= max(100, 0.25 * nz)
    h = row_hash(R[:, sl]) if hashes is None else hashes
    dup = pd.Series(h).duplicated().to_numpy()
    return feas & trades & ~dup


def scale_of(u_traded: np.ndarray, p_expected: float, kind: int) -> float:
    """Ex-ante sleeve scale (ledger 0.5): the per-traded-session sd of the pool series in the selection window,
    times sqrt(expected share of all trading days traded in the coming year), set to SLEEVE_SD, capped by kind."""
    if len(u_traded) < 2 or p_expected <= 0:
        return 0.0
    sd = float(np.std(u_traded, ddof=1)) * math.sqrt(p_expected)
    return min(SLEEVE_SD / sd, CAPS[kind]) if sd > 0 else 0.0


def walk_forward(R, Rc, T, X, Of, cf, sess, zd, share, nb) -> tuple[dict, dict]:
    """Yearly walk-forward on the 0DTE-session series (ledger 0.6.4). share[y] = 0DTE sessions / all sessions in y."""
    years = range(2016, int(END[:4]) + 1)
    ns = len(sess)
    sleeve, sleeve_c, xsel = np.zeros(ns), np.zeros(ns), np.zeros(ns)
    pick_day = np.full(ns, -1)
    picks = []
    kinds = cf["kind"].to_numpy()
    for y in years:
        sel = sess < pd.Timestamp(f"{y}-01-01")
        test = (sess >= pd.Timestamp(f"{y}-01-01")) & (sess <= pd.Timestamp(f"{y}-12-31"))
        el = eligible(R, T, Of, sel, zd)
        sc = smoothed(ir_of(R[:, sel]), nb)
        i = pick_cfg(sc, el, cf)
        if i < 0:
            picks.append({"year": y, "config": None})
            continue
        tr = T[i, sel]
        nz = max(int(zd[sel].sum()), 1)
        p_exp = (tr.sum() / nz) * share.get(y, 0.2)
        s = scale_of(R[i, sel][tr].astype(np.float64), p_exp, int(kinds[i]))
        sleeve[test] = s * R[i, test]
        sleeve_c[test] = s * Rc[i, test]
        xsel[test] = s * X[i, test]
        pick_day[test] = i
        picks.append({"year": y, "config": cf.loc[i, "name"], "i": int(i), "score": float(sc[i]), "scale": s,
                      "n_eligible": int(el.sum()), "test_mean_bp": float(sleeve[test].mean() * 1e4),
                      "test_sd_bp": float(sleeve[test].std() * 1e4), "test_trades": int(T[i, test].sum())})
    oos = sess >= pd.Timestamp("2016-01-01")
    e3 = sess >= pd.Timestamp(ERAS["E3"][0])
    r, rc, x = sleeve[oos], sleeve_c[oos], xsel[oos]
    _, a, at = alpha_t(r[None, :], x[None, :])
    res = {"picks": picks, "t": float(nw_t(r[None, :])[0]), "mean_bp": float(r.mean() * 1e4),
           "sd_bp": float(r.std() * 1e4), "e3_mean_bp": float(sleeve[e3].mean() * 1e4),
           "e3_t": float(nw_t(sleeve[e3][None, :])[0]), "alpha_bp": float(a[0] * 1e4), "alpha_t": float(at[0]),
           "cons_mean_bp": float(rc.mean() * 1e4), "cons_t": float(nw_t(rc[None, :])[0])}
    res["checks"] = {"a_t": res["t"] >= T_OOS, "b_e3_positive": res["e3_mean_bp"] > 0,
                     "c_alpha_t": res["alpha_t"] >= T_OOS,
                     "d_conservative": res["cons_mean_bp"] > 0 and res["cons_t"] >= 1.96}
    res["pass"] = bool(all(res["checks"].values()))
    return res, {"sleeve": sleeve, "sleeve_c": sleeve_c, "pick_day": pick_day}


# ---------------------------------------------------------------------------------------------------- reports
def to_days(x_sess: np.ndarray, sess: pd.DatetimeIndex, days: pd.DatetimeIndex) -> np.ndarray:
    """A 0DTE-session series on all trading days (0 on the others)."""
    return pd.Series(x_sess, index=sess).reindex(days).fillna(0.0).to_numpy()


def overlay_report(sleeve_days: np.ndarray, bench: pd.DataFrame, days: pd.DatetimeIndex, sl: np.ndarray) -> dict:
    one = bench["oneq"].to_numpy()[sl]
    lev = 1.3 * one - 0.3 * (bench["rf"].to_numpy()[sl] + 0.015 / 252)
    ov = one + sleeve_days[sl]
    mon = lambda r: pd.Series(r, index=days[sl]).groupby(days[sl].to_period("M")).apply(lambda x: np.prod(1 + x) - 1)
    out = {"days": int(sl.sum()), "overlay_cagr": ann(ov), "oneq_cagr": ann(one), "oneq13_cagr": ann(lev),
           "overlay_mdd": mdd(ov), "oneq_mdd": mdd(one), "oneq13_mdd": mdd(lev),
           "overlay_worst_month": float(mon(ov).min()), "oneq_worst_month": float(mon(one).min()),
           "oneq13_worst_month": float(mon(lev).min()),
           "sleeve_sd_bp": float(np.std(sleeve_days[sl]) * 1e4), "extra_risk_13_sd_bp": float(np.std(0.3 * one) * 1e4)}
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
    return out


def replay_10k(P: Q.Panel, G: Grid, plan: dict, masks: dict, bench: pd.DataFrame, days: pd.DatetimeIndex,
               sl: np.ndarray, pdt: bool) -> dict:
    """$10k with integer contracts over the trading days in sl (ledger 0.6.8). plan[session] = (pool, b, e, m, f) or
    absent. Per session the pool's names are taken by rank, one contract each, while the day's total max loss stays
    <= 3% of NAV; with pdt, at most 3 positions closed the same day in any 5 sessions (every position is counted)."""
    px = np.cumprod(1 + bench["oneq"].to_numpy())
    rf = bench["rf"].to_numpy()
    idx = np.flatnonzero(sl)
    d0 = idx[0]
    p0 = px[d0] / (1 + bench["oneq"].to_numpy()[d0])
    sh = math.floor(10_000.0 / p0)
    cash = 10_000.0 - sh * p0
    nav = 10_000.0
    navs, trades, skipped, fees_paid, premium, worst = [], 0, 0, 0.0, 0.0, 0.0
    recent: list[int] = []
    nm = P.names
    by_sess: dict[int, list[int]] = {}
    for k, s in enumerate(G.si):
        by_sess.setdefault(int(s), []).append(k)
    spos = {d: i for i, d in enumerate(G.sess)}
    for di in idx:
        d = days[di]
        cash *= 1 + (rf[di] if cash > 0 else rf[di] + 0.015 / 252)
        pnl_day = 0.0
        si = spos.get(d)
        cfg = plan.get(si) if si is not None else None
        recent = [x for x in recent if x > di - 5]
        if cfg is not None:
            pool, b, e, m, f = cfg
            mk = mask_for(masks, FILTERS[f], b)
            sides, mi = ([COMP[b - Q.NS]["put"], COMP[b - Q.NS]["call"]], 2) if b >= Q.NS else ([b], m)
            ents = range(Q.NE) if e == LAD else [e]
            used = 0.0
            ks = sorted(by_sess.get(si, []), key=lambda k: nm.loc[k, "rank"])
            for k in ks:
                if nm.loc[k, "rank"] > pool:
                    continue
                for ee in ents:
                    if not mk[ee, k] or not all(P.opened[s, ee, k] for s in sides):
                        continue
                    if pdt and len(recent) >= 3:
                        skipped += 1
                        continue
                    kind = base_kind(b)
                    if kind in (0, 5):
                        w = max(P.width[s, ee, k] for s in sides)
                        maxloss = (w - sum(P.net[s, ee, k] for s in sides)) * 100.0
                    elif kind == 2:
                        maxloss = -P.net[sides[0], ee, k] * 100.0
                    else:
                        maxloss = P.s0[ee, k] * 100.0
                    if not np.isfinite(maxloss) or maxloss <= 0 or used + maxloss > 0.03 * nav:
                        skipped += 1
                        continue
                    used += maxloss
                    for s in sides:
                        legs = P.n_legs[s, ee, k]
                        tier_e = P.fee_entry[s, ee, k] * 100 - legs * Q.SIM.OTHER_FEE
                        tier_x = P.fee_flat[s, ee, k] * 100
                        extra = max(0.0, 1.0 - tier_e) + (max(0.0, 1.0 - tier_x) if tier_x > 0 else 0.0)
                        pnl_day += 100 * P.pnl[s, ee, mi, k] - extra
                        fees_paid += (P.fee_entry[s, ee, k] + P.fee_flat[s, ee, k]) * 100 + extra
                        premium += 100 * abs(P.net[s, ee, k])
                    trades += 1
                    recent.append(di)
        cash += pnl_day
        nav_new = cash + sh * px[di]
        worst = min(worst, pnl_day / max(nav, 1))
        if abs(cash) > 0.02 * nav_new:
            tgt = math.floor(nav_new / px[di])
            cash -= (tgt - sh) * px[di]
            sh = tgt
        nav = nav_new
        navs.append(nav)
    navs = np.array(navs)
    r = np.r_[navs[0] / 10_000.0 - 1, navs[1:] / navs[:-1] - 1]
    one = bench["oneq"].to_numpy()[sl]
    return {"days": int(len(idx)), "cagr": ann(r), "oneq_cagr": ann(one), "mdd": mdd(r), "oneq_mdd": mdd(one),
            "trades": trades, "skipped": skipped, "worst_day": worst, "pdt": pdt,
            "fees_over_premium": fees_paid / max(premium, 1e-9), "beats_oneq": bool(ann(r) > ann(one))}


# ---------------------------------------------------------------------------------------------------- main
def files() -> list[Path]:
    return sorted(RAW.glob("stock_0dte_*.json.gz"))


def check_inputs(metas: list[dict], days_rec: pd.DataFrame, names: pd.DataFrame, allow_partial: bool) -> dict:
    """The exports must be the registered runs (ledger 0.7) and have a day record on every last session of a week."""
    probs = []
    for m in metas:
        rt = m.get("runtime", {})
        if SIM_HASH and m.get("sim") != SIM_HASH:
            probs.append(f"{m.get('name')}: sim {m.get('sim')} is not the frozen {SIM_HASH}")
        run = rt.get("RUN", "")
        if "mech0" not in run or "left 0" not in run or "final 1" not in run or rt.get("ERR") != "none":
            probs.append(f"{m.get('name')}: RUN/ERR not clean ({run[-60:]} | {rt.get('ERR')})")
    have = {tuple(m.get("name", "").split(" ")[-1].split("..")) for m in metas}
    missing = [c for c in CHUNKS if c not in have]
    if missing:
        probs.append(f"missing chunks {missing}")
    s = sessions()
    nxt = pd.Series(s[1:], index=s[:-1])
    low = [d for d in s if d.weekday() == 4 or (d.weekday() == 3 and (d not in nxt.index or (nxt[d] - d).days > 1))]
    got = set(pd.DatetimeIndex(days_rec["date"]))
    miss = [d for d in low if d not in got]
    if miss:
        probs.append(f"{len(miss)} week-end sessions without a day record (e.g. {miss[:3]})")
    if days_rec["date"].duplicated().any():
        probs.append("duplicate day records")
    if probs and not allow_partial:
        raise ValueError("; ".join(probs))
    st = names["status"].to_numpy()
    return {"problems": probs, "name_days": int(len(names)), "status_counts": {int(k): int(v) for k, v in
                                                                               pd.Series(st).value_counts().items()},
            "penny_share": float(names["penny"].mean()) if len(names) else None,
            "next_open_missing": int(names["next_open"].isna().sum()) if "next_open" in names else None}


def analyse(paths: list[Path], quick: bool = False, allow_partial: bool = False) -> dict:
    names, rows, days_rec, metas = Q.read_exports(paths)
    keep = (names["date"] >= START) & (names["date"] <= END)
    kmap = pd.Series(-1, index=names.index)
    kmap[keep] = np.arange(int(keep.sum()))
    names = names[keep].reset_index(drop=True)
    rows = rows[rows["k"].isin(np.flatnonzero(keep.to_numpy()))].copy()
    rows["k"] = kmap.reindex(rows["k"]).to_numpy()
    days_rec = days_rec[(days_rec["date"] >= START) & (days_rec["date"] <= END)]
    inputs = check_inputs(metas, days_rec, names, allow_partial)
    P = Q.build_panel(names, rows)
    days = sessions()
    sess = pd.DatetimeIndex(sorted(set(pd.DatetimeIndex(days_rec["date"])) | set(pd.DatetimeIndex(names["date"]))))
    zd = np.isin(sess, pd.DatetimeIndex(names.loc[names["status"] == 0, "date"]))
    bench = benchmarks(days)
    masks = filter_masks(P, bench)
    U, O, F = base_units(P, "flat")
    Uc, _, _ = base_units(P, "cons")
    Uh, _, _ = base_units(P, "hold")
    Ua, _, _ = base_units(P, "asn")
    G = Grid(P, sess, masks)
    cf = config_list()
    X = P.xret
    R, Rc, T, Xm, Of = matrices(G, U, Uc, O, F, cf, False, X)
    k10i = np.flatnonzero(cf["k10"].to_numpy())
    cfk = cf.iloc[k10i].reset_index(drop=True)
    Rk, Rkc, Tk, Xk, Ofk = matrices(G, U, Uc, O, F, cfk, True, X)
    st = grid_stats(R, T, Xm, sess)
    Rh = np.vstack([G.series(Uh, O, F, cfg, False)[0] for cfg in cf[["pool", "b", "e", "m", "f"]].to_numpy()])
    Ra = np.vstack([G.series(Ua, O, F, cfg, False)[0] for cfg in cf[["pool", "b", "e", "m", "f"]].to_numpy()])
    table = pd.concat([cf, st, pd.DataFrame({"t_cons": nw_t(Rc), "mean_cons_bp": Rc.mean(axis=1, dtype=np.float64) * 1e4,
                                             "t_hold": nw_t(Rh), "mean_hold_bp": Rh.mean(axis=1, dtype=np.float64) * 1e4,
                                             "t_asn": nw_t(Ra), "mean_asn_bp": Ra.mean(axis=1, dtype=np.float64) * 1e4})],
                      axis=1)
    stk = grid_stats(Rk, Tk, Xk, sess)
    tablek = pd.concat([cfk, stk, pd.DataFrame({"t_cons": nw_t(Rkc)})], axis=1)
    nb = neighbours(cf)
    nbk = neighbours(cfk)
    share = {int(y): float(np.isin(days[days.year == y], sess).mean()) for y in range(2012, int(END[:4]) + 1)}
    res = {"files": [p.name for p in paths], "file_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
           "inputs": inputs, "sessions": int(len(sess)), "zdte_sessions": int(zd.sum()), "name_days": int(len(names)),
           "eras_sessions": {k: int(zd[(sess >= a) & (sess <= b)].sum()) for k, (a, b) in ERAS.items()},
           "configs": int(len(cf)), "configs_k10": int(len(cfk)), "t_grid": T_GRID,
           "A": int(table["A"].sum()), "A_distinct": int((table["A"] & ~table["dup"]).sum()),
           "A_k10": int(tablek["A"].sum()), "earn_known_share": float((masks["_earn_known"] >= 0).mean())}
    wf_k10, sk = walk_forward(Rk, Rkc, Tk, Xk, Ofk, cfk, sess, zd, share, nbk)
    wf_all, sa = walk_forward(R, Rc, T, Xm, Of, cf, sess, zd, share, nb)
    res["walk_forward"] = {"K10": wf_k10, "ALL": wf_all}
    sk_days = to_days(sk["sleeve"], sess, days)
    skc_days = to_days(sk["sleeve_c"], sess, days)
    oos = days >= pd.Timestamp("2016-01-01")
    e3 = days >= pd.Timestamp(ERAS["E3"][0])
    res["equal_risk"] = {"oos": overlay_report(sk_days, bench, days, oos), "E3": overlay_report(sk_days, bench, days, e3),
                         "oos_cons": overlay_report(skc_days, bench, days, oos)}
    n = len(sess)
    lr = np.sqrt(np.maximum(nw_var(R.astype(np.float64) - R.mean(axis=1, dtype=np.float64)[:, None]), 0))
    evaluable = (table["trades"] >= 100) & ~table["dup"]
    oos_s = sess >= pd.Timestamp("2016-01-01")
    res["mde"] = {"per_family_unit_bp": {f: float(np.median(lr[(table["fam"] == f) & evaluable]) *
                                                  (T_GRID + 0.84) / math.sqrt(n) * 1e4)
                                         for f in sorted(table["fam"].unique()) if ((table["fam"] == f) & evaluable).any()},
                  "oos_sleeve_bp": float(math.sqrt(max(nw_var((sk["sleeve"][oos_s] - sk["sleeve"][oos_s].mean())[None, :])[0], 0))
                                         * (T_OOS + 0.84) / math.sqrt(int(oos_s.sum())) * 1e4)}
    if not quick:
        res["bootstrap"] = bootstrap_family(R, T, table["dup"].to_numpy())
        ev = np.flatnonzero(evaluable.to_numpy())
        if len(ev):
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
    # $10k replay over E3 with each year's K10 pick, with and without the pattern-day-trader limit
    plan = {}
    for s_i in np.flatnonzero(sess >= pd.Timestamp(ERAS["E3"][0])):
        i = sk["pick_day"][s_i]
        if i >= 0:
            plan[int(s_i)] = tuple(int(x) for x in cfk.loc[i, ["pool", "b", "e", "m", "f"]])
    res["replay_10k"] = {"K10_walk_forward_pdt": replay_10k(P, G, plan, masks, bench, days, e3, True),
                         "K10_walk_forward": replay_10k(P, G, plan, masks, bench, days, e3, False)}
    try:
        ov = pd.Series(bench["oneq"].to_numpy()[oos] + sk_days[oos], index=days[oos])
        card = scorecard(ov, freq="D", oneq=bench["oneq"][oos], qqq=bench["qqq"][oos], rf=bench["rf"][oos],
                         meta={"n_trials": N_TRIALS, "oos_status": "passed" if wf_k10["pass"] else "failed"})
        card.to_csv(OUT / "scorecard_k10.csv", index=False)
        keys = {"excess_cagr_vs_oneq", "max_drawdown", "worst_month", "ce_crra_2"}
        res["scorecard_selected"] = {r_["key"]: r_["value"] for _, r_ in card.iterrows()
                                     if any(k in str(r_["key"]) for k in keys)}
    except Exception as ex:
        res["scorecard_selected"] = {"error": str(ex)[:200]}
    er = res["equal_risk"]
    res["go"] = {"1_k10_oos_pass": bool(wf_k10["pass"]),
                 "3_replay_beats_oneq_E3": bool(res["replay_10k"]["K10_walk_forward_pdt"]["beats_oneq"]),
                 "4_beats_oneq13_oos_and_E3": bool(er["oos"]["overlay_cagr"] > er["oos"]["oneq13_cagr"]
                                                   and er["E3"]["overlay_cagr"] > er["E3"]["oneq13_cagr"]),
                 "5_conservative_passes": bool(wf_k10["checks"]["d_conservative"])}
    OUT.mkdir(parents=True, exist_ok=True)
    table.drop(columns=["b"]).to_csv(OUT / "configs.csv.gz", index=False, float_format="%.5g")
    tablek.drop(columns=["b"]).to_csv(OUT / "configs_k10.csv.gz", index=False, float_format="%.5g")
    pd.DataFrame({"date": sess, "sleeve_k10": sk["sleeve"], "sleeve_k10_cons": sk["sleeve_c"],
                  "sleeve_all": sa["sleeve"]}).to_csv(OUT / "walkforward.csv", index=False)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--quick", action="store_true", help="skip the bootstrap and the deflated Sharpe")
    ap.add_argument("--allow-partial", action="store_true", help="mechanism smoke tests only")
    a = ap.parse_args(argv)
    paths = [Path(f) for f in a.files] if a.files else files()
    if not paths:
        raise SystemExit(f"no exports under {RAW}")
    res = analyse(paths, a.quick, a.allow_partial)
    write_json(OUT / "results.json", res)
    print(json.dumps({k: res[k] for k in ("sessions", "zdte_sessions", "name_days", "configs", "A", "go")}, indent=1,
                     default=str))


if __name__ == "__main__":
    main()
