"""A broad "T" grid (做T 大网格) around a held base with honest out-of-sample selection, pre-registered in
docs/research_ledger_t_grid.md (section 0). Not QuantConnect.

Grid per base (61,440 configurations): direction (S sell-high / B buy-dip / C both) x trigger measure (D10, D20, D50,
RSI2, RSI14, R1, R5, Z1) x trigger level (expanding own-history quantiles 80/90/95/98, mirrored 20/10/5/2) x exit
(SMA, TARGET 2 sigma, TRAIL 2 sigma, OPP = measure back to its median, TIME) x max hold (5/10/20/40) x fraction of the
base (1/5, 1/3, 1/2, 1) x cash reserve (0 = margin account, 10/25/50% cash account) x regime (ALL / UP = close >
SMA200). Bases: QQQ, U18 (18 large caps, Yahoo OHLC from the selective_t cache), T10 (monthly point-in-time top-10 by
market cap from the megacap v2 study, v2 panel closes, close-only execution).

The simulator is vectorised over configurations and accounts (numpy arrays of shape (accounts, configs)); one Python
loop over sessions. Account / execution / cost rules are those of scripts/research_selective_t.py (tested equal on
the S1-like special case). The grid run accumulates per calendar year the sufficient statistics of each
configuration's daily basket returns (sum R, R^2, R*M, log(1+R), exposure, trips ...), from which every window
statistic used for selection is computed. Selected configurations are re-run exactly (fresh $10k accounts on the
test window, per-account matched exposure).

Usage: PYTHONPATH=. .venv/bin/python scripts/research_t_grid.py [--workers 12] [--smoke]
Outputs (returns / statistics only, no prices): output/research_only/t_grid/. Local derived caches (not in Git):
research_cache/t_grid/.
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from scripts import research_intraday_t as it
from scripts import research_selective_t as st
from scripts.research_calendar import relative_metrics, t_and_ir
from scripts.research_qqq_timing import (
    TRADING_DAYS, bonferroni_t, cagr_of, deflated_sharpe, fill_rf, load_dtb3, load_kf_rf, max_drawdown, monthly,
    yearly,
)
from scripts.research_reversal_dev import (
    CLEARING_FEE_PER_SHARE, EXCHANGE_FEE_PER_SHARE, FINRA_TAF_MAX, FINRA_TAF_PER_SHARE_SOLD,
    PASS_THROUGH_OF_COMMISSION, SEC_FEE_PER_DOLLAR_SOLD,
)
from src.research.ibkr_cost_calibration import (
    MAXIMUM_COMMISSION_FRACTION, TIERED_MINIMUM_PER_ORDER_USD, tiered_rate_per_share,
)

ROOT = Path(__file__).resolve().parents[1]
MAIN = st.MAIN
END = st.END
T10_END = "2026-08-31"
T10_START = "2012-07-02"
U18_START = "2012-01-03"
CACHE = MAIN / "research_cache/t_grid"
OUT = ROOT / "output/research_only/t_grid"
PANEL = MAIN / "research_cache/reversal_2012_2026_v2/prices"
TOP20 = ROOT / "output/research_only/megacap_v2/top20_by_month.csv"

START_EQUITY = 10_000.0
BUFFER = 5.0
CAP_MULT = 1.10
BAND_HALF = 0.05
MARGIN_SPREAD = 0.015
K_SIGMA = 2.0
MIN_OBS = 200
WF_MIN_SESSIONS = 750
RATE = tiered_rate_per_share(0.0)
TICK = 0.01
THROUGH_FRAC = 0.0005

MEASURES = ("D10", "D20", "D50", "RSI2", "RSI14", "R1", "R5", "Z1")
SMA_LENS = (10, 20, 50)
MEAS_SMA = np.array([0, 1, 2, 1, 1, 1, 1, 1])
Q_HI = (0.80, 0.90, 0.95, 0.98)
Q_LO = (0.20, 0.10, 0.05, 0.02)
DIRS = ("S", "B", "C")
EXITS = ("SMA", "TARGET", "TRAIL", "OPP", "TIME")
HOLDS = (5, 10, 20, 40)
FRACS = (1 / 5, 1 / 3, 1 / 2, 1.0)
RESERVES = (0.0, 0.10, 0.25, 0.50)
REGIMES = ("ALL", "UP")
AXES = (("measure", MEASURES), ("level", ("q1", "q2", "q3", "q4")), ("direction", DIRS), ("exit", EXITS),
        ("hold", HOLDS), ("frac", FRACS), ("reserve", RESERVES), ("regime", REGIMES))
SHAPE = tuple(len(v) for _, v in AXES)
ORDINAL = ("level", "hold", "frac", "reserve")
N_GRID = int(np.prod(SHAPE))
DIR_NONE = 3
BASES = ("QQQ", "U18", "T10")
HALF_SPLIT_YEAR = {"QQQ": 2013, "U18": 2019, "T10": 2019}   # half2 starts on Jan 1 of this year
COSTS = {"base": (True, 1.0), "zero": (False, 1.0), "x2": (True, 2.0)}
NORM = NormalDist()


# ======================================================================== grid

def grid() -> pd.DataFrame:
    """All configurations, id = C-order ravel index over SHAPE; integer codes per axis."""
    idx = np.indices(SHAPE).reshape(len(SHAPE), -1)
    return pd.DataFrame({name: idx[i] for i, (name, _) in enumerate(AXES)})


def cfg_arrays(codes: pd.DataFrame) -> dict:
    return {k: codes[k].to_numpy(int) for k, _ in AXES}


def label(codes: dict | pd.Series) -> str:
    parts = []
    for name, vals in AXES:
        v = vals[int(codes[name])] if int(codes[name]) < len(vals) else "none"
        if name == "level":
            d = int(codes["direction"])
            li = int(codes["level"])
            v = {0: f"p{Q_HI[li]*100:.0f}", 1: f"p{Q_LO[li]*100:.0f}"}.get(d, f"p{Q_HI[li]*100:.0f}/{Q_LO[li]*100:.0f}")
        elif name == "frac":
            v = {1 / 5: "1/5", 1 / 3: "1/3", 1 / 2: "1/2", 1.0: "1"}[v]
        elif name == "reserve":
            v = f"res{v*100:.0f}"
        elif name == "hold":
            v = f"H{v}"
        parts.append(str(v))
    return " ".join(parts)


def ref_codes() -> pd.DataFrame:
    """Four no-T references (H_base per reserve; reserve 0 = H100)."""
    return pd.DataFrame({"measure": 0, "level": 0, "direction": DIR_NONE, "exit": 4, "hold": 0, "frac": 0,
                         "reserve": [0, 1, 2, 3], "regime": 0})


def neighbour_median(score: np.ndarray) -> np.ndarray:
    """Median of a config's score and its +-1 neighbours on the ordinal axes (other axes fixed)."""
    a = score.reshape(SHAPE)
    stack = [a]
    for name in ORDINAL:
        ax = [n for n, _ in AXES].index(name)
        for sh in (1, -1):
            b = np.full(SHAPE, np.nan)
            src = [slice(None)] * len(SHAPE)
            dst = [slice(None)] * len(SHAPE)
            if sh == 1:
                src[ax], dst[ax] = slice(0, -1), slice(1, None)
            else:
                src[ax], dst[ax] = slice(1, None), slice(0, -1)
            b[tuple(dst)] = a[tuple(src)]
            stack.append(b)
    return np.nanmedian(np.stack(stack), axis=0).ravel()


# ======================================================================== costs (vectorised order_cost)

@dataclass(frozen=True)
class CostModel:
    on: bool = True
    spread_mult: float = 1.0


def cost_vec(q, px, sell: bool, hs_bps, cm: CostModel = CostModel()):
    """research_reversal_dev.order_cost(q, px, sell, max(hs_bps/1e4, 0.005/px) * mult)['total'], vectorised."""
    q = np.asarray(q, float)
    px = np.asarray(px, float)
    if not cm.on:
        return np.zeros(np.broadcast(q, px).shape)
    with np.errstate(invalid="ignore", divide="ignore"):
        value = q * px
        comm = np.minimum(np.maximum(q * RATE, TIERED_MINIMUM_PER_ORDER_USD), MAXIMUM_COMMISSION_FRACTION * value)
        fees = q * (EXCHANGE_FEE_PER_SHARE + CLEARING_FEE_PER_SHARE) + comm * PASS_THROUGH_OF_COMMISSION
        if sell:
            fees = fees + value * SEC_FEE_PER_DOLLAR_SOLD + np.minimum(q * FINRA_TAF_PER_SHARE_SOLD, FINRA_TAF_MAX)
        hs = np.maximum(np.asarray(hs_bps, float) / 1e4, 0.005 / px) * cm.spread_mult
        out = comm + fees + value * hs
    return np.where(q > 0, out, 0.0)


def floor_cent(x):
    return np.floor(np.round(x / TICK, 6)) * TICK


def ceil_cent(x):
    return np.ceil(np.round(x / TICK, 6)) * TICK


# ======================================================================== indicators

@dataclass
class Inst:
    """One instrument on the master calendar (missing sessions filled as no-trade days)."""
    sym: str
    hs_bps: float
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    prevc: np.ndarray
    split: np.ndarray
    div: np.ndarray
    tr: np.ndarray
    has: np.ndarray            # the instrument has a real row this session
    sig_hi: np.ndarray         # (M*4, T) bool
    sig_lo: np.ndarray
    opp_s: np.ndarray          # (M, T) bool: x_t <= expanding median
    opp_b: np.ndarray
    sma_prev: np.ndarray       # (3, T) real units of day t, SMA through t-1
    up: np.ndarray             # close_t > SMA200_t
    sigma: np.ndarray          # std of the 20 daily returns through t
    first_valid: int           # first master index with every threshold, SMA50 and SMA200 available
    checks: dict = field(default_factory=dict)


def measures(adj: np.ndarray) -> np.ndarray:
    s = pd.Series(adj)
    r1 = s.pct_change()
    sd_prev = r1.rolling(20, min_periods=20).std().shift(1)
    out = [s / s.rolling(n, min_periods=n).mean() - 1 for n in SMA_LENS]
    out += [pd.Series(st.rsi_wilder(adj, 2)), pd.Series(st.rsi_wilder(adj, 14)), r1, s.pct_change(5),
            r1 / sd_prev.where(sd_prev > 0)]
    return np.vstack([x.to_numpy(float) for x in out])


def expanding_thresholds(x: np.ndarray, qs, min_obs: int = MIN_OBS) -> np.ndarray:
    """(len(qs), T): quantile of x over its own history through t-1 (at least min_obs values, else NaN)."""
    s = pd.Series(x)
    ex = s.expanding(min_periods=min_obs)
    return np.vstack([ex.quantile(q).shift(1).to_numpy(float) for q in qs])


def indicator_block(adj: np.ndarray, factor: np.ndarray) -> dict:
    X = measures(adj)
    M = len(MEASURES)
    sig_hi = np.zeros((M * 4, len(adj)), bool)
    sig_lo = np.zeros((M * 4, len(adj)), bool)
    opp_s = np.zeros((M, len(adj)), bool)
    opp_b = np.zeros((M, len(adj)), bool)
    valid = np.ones(len(adj), bool)
    for m in range(M):
        th = expanding_thresholds(X[m], Q_HI + Q_LO + (0.5,))
        with np.errstate(invalid="ignore"):
            for li in range(4):
                sig_hi[m * 4 + li] = X[m] >= th[li]
                sig_lo[m * 4 + li] = X[m] <= th[4 + li]
            opp_s[m] = X[m] <= th[8]
            opp_b[m] = X[m] >= th[8]
        valid &= np.isfinite(th).all(0)
    s = pd.Series(adj)
    sma_prev = np.vstack([s.rolling(n, min_periods=n).mean().shift(1).to_numpy() * factor for n in SMA_LENS])
    sma200 = s.rolling(200, min_periods=200).mean().to_numpy()
    with np.errstate(invalid="ignore"):
        up = adj > sma200
    sigma = s.pct_change().rolling(20, min_periods=20).std().to_numpy()
    valid &= np.isfinite(sma200) & np.isfinite(sma_prev).all(0) & np.isfinite(sigma)
    return {"sig_hi": sig_hi, "sig_lo": sig_lo, "opp_s": opp_s, "opp_b": opp_b, "sma_prev": sma_prev, "up": up,
            "sigma": sigma, "valid": valid}


def make_inst(sym: str, hs_bps: float, sessions: pd.DatetimeIndex, master: pd.DatetimeIndex, o, h, lo, c, split,
              div, tr, factor) -> Inst:
    """Instrument arrays (real units, own calendar) -> master calendar; indicators on the own calendar.
    Master sessions without an own row are no-trade days: prices = the last close, no split / dividend / return,
    no signal; level indicators carried forward."""
    pos = master.get_indexer(sessions)
    if (pos < 0).any():
        raise ValueError(f"{sym}: {int((pos < 0).sum())} sessions not in the master calendar")
    c = np.asarray(c, float)
    split = np.asarray(split, float)
    ind = indicator_block(c / factor, factor)
    T = len(master)
    has = np.zeros(T, bool)
    has[pos] = True
    ar = np.arange(T)
    gap = (~has) & (ar > pos.min()) & (ar < pos.max())

    def ffill1(vals):
        out = np.full(T, np.nan)
        out[pos] = vals
        return pd.Series(out).ffill().to_numpy().copy()

    def ffill2(vals):
        out = np.full((vals.shape[0], T), np.nan)
        out[:, pos] = vals
        return pd.DataFrame(out.T).ffill().to_numpy().T.copy()

    def flag2(vals):
        out = np.zeros((vals.shape[0], T), bool)
        out[:, pos] = vals
        return out
    cc = ffill1(c)
    prevc = np.r_[np.nan, cc[:-1]]
    prevc[pos] = np.r_[np.nan, c[:-1]] / split
    oo, hh, ll = cc.copy(), cc.copy(), cc.copy()
    oo[pos], hh[pos], ll[pos] = o, h, lo
    sp = np.ones(T)
    sp[pos] = split
    dv = np.zeros(T)
    dv[pos] = div
    trr = np.zeros(T)
    trr[pos] = np.nan_to_num(np.asarray(tr, float))
    up = np.zeros(T, bool)
    up[pos] = ind["up"]
    up = pd.Series(np.where(has, up.astype(float), np.nan)).ffill().fillna(0.0).to_numpy() > 0.5
    valid = np.zeros(T, bool)
    valid[pos] = ind["valid"]
    fv = int(np.argmax(valid)) if valid.any() else T
    for a_ in (cc, oo, hh, ll, prevc):
        a_[~np.isfinite(a_)] = 1.0
    return Inst(sym, hs_bps, oo, hh, ll, cc, prevc, sp, dv, trr, has, flag2(ind["sig_hi"]), flag2(ind["sig_lo"]),
                flag2(ind["opp_s"]), flag2(ind["opp_b"]), ffill2(ind["sma_prev"]), up, ffill1(ind["sigma"]), fv,
                {"missing_sessions_inside": int(gap.sum())})


# ======================================================================== accounts ("rows") on a window

@dataclass
class Rows:
    name: str
    sessions: pd.DatetimeIndex
    syms: list
    hs_bps: np.ndarray        # (R,)
    o: np.ndarray             # (R, T)
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    prevc: np.ndarray
    split: np.ndarray
    div: np.ndarray
    tr: np.ndarray
    active: np.ndarray        # (R, T) bool
    init: np.ndarray          # (R, T) bool: an account opens (base bought at the previous close)
    last: np.ndarray          # (R, T) bool: an account's last session (open T trades closed at the close)
    sig_hi: np.ndarray        # (R, 32, T)
    sig_lo: np.ndarray
    opp_s: np.ndarray         # (R, 8, T)
    opp_b: np.ndarray
    sma_prev: np.ndarray      # (R, 3, T)
    up: np.ndarray            # (R, T)
    sigma: np.ndarray         # (R, T)
    rf: np.ndarray            # (T,)
    oneq: np.ndarray          # (T,) NaN before ONEQ


def build_rows(name: str, insts: dict, spells: list, master: pd.DatetimeIndex, start: str, end: str,
               rf: pd.Series, oneq: pd.Series) -> Rows:
    """spells: [(slot, sym, first_master_idx, last_master_idx)], clipped to [start, end]."""
    i0 = int(master.searchsorted(pd.Timestamp(start)))
    i1 = int(master.searchsorted(pd.Timestamp(end), side="right")) - 1
    T = i1 - i0 + 1
    slots = sorted({s[0] for s in spells})
    R = len(slots)
    sl = {s: k for k, s in enumerate(slots)}
    f2 = lambda: np.ones((R, T))  # noqa: E731
    rows = Rows(name, master[i0: i1 + 1], [None] * R, np.zeros(R), f2(), f2(), f2(), f2(), f2(), f2(),
                np.zeros((R, T)), np.zeros((R, T)), np.zeros((R, T), bool), np.zeros((R, T), bool),
                np.zeros((R, T), bool), np.zeros((R, 32, T), bool), np.zeros((R, 32, T), bool),
                np.zeros((R, 8, T), bool), np.zeros((R, 8, T), bool), np.full((R, 3, T), np.nan),
                np.zeros((R, T), bool), np.full((R, T), np.nan), rf.reindex(master[i0: i1 + 1]).fillna(0.0).to_numpy(),
                oneq.reindex(master[i0: i1 + 1]).to_numpy(float))
    for slot, sym, a, b in spells:
        a, b = max(a, i0), min(b, i1)
        if b < a:
            continue
        k = sl[slot]
        ins = insts[sym]
        src, dst = slice(a, b + 1), slice(a - i0, b - i0 + 1)
        rows.syms[k] = sym if rows.syms[k] is None else (rows.syms[k] if rows.syms[k] == sym else "multi")
        rows.hs_bps[k] = ins.hs_bps
        for attr in ("o", "h", "lo", "c", "prevc", "split", "div", "tr", "up", "sigma"):
            getattr(rows, attr)[k, dst] = getattr(ins, attr)[src]
        for attr in ("sig_hi", "sig_lo", "opp_s", "opp_b", "sma_prev"):
            getattr(rows, attr)[k, :, dst] = getattr(ins, attr)[:, src]
        rows.active[k, dst] = True
        rows.init[k, a - i0] = True
        rows.last[k, b - i0] = True
    rows.split[~rows.active] = 1.0
    rows.div[~rows.active] = 0.0
    rows.tr[~rows.active] = 0.0
    return rows


# ======================================================================== the vectorised simulator

class Leg:
    def __init__(self, shape):
        self.open = np.zeros(shape, bool)
        self.q = np.zeros(shape)
        self.px = np.zeros(shape)
        self.days = np.zeros(shape, np.int32)
        self.aux = np.zeros(shape)
        self.k = np.zeros(shape)
        self.cin = np.zeros(shape)
        self.t0 = np.full(shape, -1, np.int32)
        self.qx = np.zeros(shape, bool)      # exit queued for the next open
        self.qe = np.zeros(shape, bool)      # entry queued for the next open
        self.qe_q = np.zeros(shape)
        self.qe_k = np.zeros(shape)

    def reset_rows(self, rmask):
        for a in (self.open, self.qx, self.qe):
            a[rmask] = False
        for a in (self.q, self.px, self.aux, self.k, self.cin, self.qe_q, self.qe_k):
            a[rmask] = 0.0
        self.days[rmask] = 0
        self.t0[rmask] = -1


ACC_KEYS = ("sR", "sR2", "sRM", "sLog", "sX", "sRO", "sR_o", "sR2_o", "nT", "nW", "sG", "sNet", "sC", "nTarget",
            "nTimeout", "sDays", "nSame", "nShort", "nRuin")
GLOB_KEYS = ("n", "sM", "sM2", "sF", "nO", "sO", "sO2", "sMO", "acct_days")


def simulate_grid(rows: Rows, codes: dict, cm: CostModel = CostModel(), record: bool = False,
                  record_trades: bool = False) -> dict:
    """Run every configuration in ``codes`` on every account of ``rows``. Returns per-year accumulators (and, with
    ``record``, the daily per-account returns / exposures and basket returns; with ``record_trades``, the list of
    closed T-trades: account row, configuration column, leg, entry / exit session index, exit reason ('target',
    'timeout', or 'end' = closed at the account's last close), days held and net return in bp, the value the
    ``sNet`` accumulator adds). Neither option changes the simulation."""
    R, T = rows.c.shape
    N = len(codes["direction"])
    meas, dirc, exitc = codes["measure"], codes["direction"], codes["exit"]
    combo = meas * 4 + codes["level"]
    doS = (dirc == 0) | (dirc == 2)
    doB = (dirc == 1) | (dirc == 2)
    lim_ex = exitc <= 1
    is_sma = exitc == 0
    trail = exitc == 2
    oppx = exitc == 3
    hold = np.array(HOLDS)[codes["hold"]]
    frac = np.array(FRACS)[codes["frac"]]
    res = np.array(RESERVES)[codes["reserve"]]
    margin = res == 0.0
    cash = ~margin
    any_margin = bool(margin.any())
    bf = 1.0 - res
    up_req = codes["regime"] == 1
    smai = MEAS_SMA[meas]
    hsb = rows.hs_bps[:, None]
    shape = (R, N)
    base = np.zeros(shape)
    settled = np.zeros(shape)
    pending = np.zeros(shape)
    vprev = np.zeros(shape)
    dead = np.zeros(shape, bool)
    S, B = Leg(shape), Leg(shape)
    years = rows.sessions.year.to_numpy()
    uy = np.unique(years)
    yidx = np.searchsorted(uy, years)
    Y = len(uy)
    acc = {k: np.zeros((Y, N)) for k in ACC_KEYS}
    glob = {k: np.zeros(Y) for k in GLOB_KEYS}
    month = rows.sessions.to_period("M")
    month_end = np.r_[month[1:] != month[:-1], False]
    rec = {}
    if record:
        rec = {"r": np.zeros((T, R, N)), "x": np.zeros((T, R, N)), "R": np.full((T, N), np.nan),
               "M": np.full(T, np.nan), "act": rows.active.T.copy()}
    Mday = np.full(T, np.nan)
    trades = []

    def cost(q, px, sell, ri):
        return cost_vec(q, px, sell, rows.hs_bps[ri], cm)

    def bc(ni, w):
        return np.bincount(ni, weights=w, minlength=N)

    def close_leg(leg: Leg, sell_first: bool, idx, px, reason: str, t: int, y: int):
        ri, ni = idx
        q = leg.q[idx]
        if sell_first:   # buy back
            c_ex = cost(q, px, False, ri)
            amount = q * px + c_ex
            acc["nShort"][y] += bc(ni, (cash[ni] & (settled[idx] < amount)).astype(float))
            settled[idx] -= amount
            gross = q * (leg.px[idx] - px)
        else:            # sell the added shares
            c_ex = cost(q, px, True, ri)
            proceeds = q * px - c_ex
            pending[idx] += np.where(cash[ni], proceeds, 0.0)
            settled[idx] += np.where(cash[ni], 0.0, proceeds)
            gross = q * (px - leg.px[idx])
        notional = q * leg.px[idx]
        ct = leg.cin[idx] + c_ex
        net = gross - ct
        acc["nT"][y] += bc(ni, np.ones(len(ni)))
        acc["nW"][y] += bc(ni, (net > 0).astype(float))
        acc["sG"][y] += bc(ni, gross / notional * 1e4)
        acc["sNet"][y] += bc(ni, net / notional * 1e4)
        acc["sC"][y] += bc(ni, ct / notional * 1e4)
        acc["sDays"][y] += bc(ni, leg.days[idx].astype(float))
        acc["nSame"][y] += bc(ni, (leg.t0[idx] == t).astype(float))
        key = {"target": "nTarget", "timeout": "nTimeout"}.get(reason)
        if key:
            acc[key][y] += bc(ni, np.ones(len(ni)))
        if record_trades:
            for r_, n_, t0_, d_, nb_ in zip(ri, ni, leg.t0[idx], leg.days[idx], net / notional * 1e4):
                trades.append({"row": int(r_), "cfg": int(n_), "leg": "S" if sell_first else "B", "t0": int(t0_),
                               "t1": t, "reason": reason, "days": int(d_), "net_bp": float(nb_)})
        leg.open[idx] = False
        leg.qx[idx] = False
        leg.q[idx] = 0.0

    def open_leg(leg: Leg, sell_first: bool, idx, px, t: int, y: int):
        ri, ni = idx
        q = leg.qe_q[idx]
        if sell_first:
            c_in = cost(q, px, True, ri)
            proceeds = q * px - c_in
            pending[idx] += np.where(cash[ni], proceeds, 0.0)
            settled[idx] += np.where(cash[ni], 0.0, proceeds)
        else:
            c_in = cost(q, px, False, ri)
            amount = q * px + c_in
            acc["nShort"][y] += bc(ni, (cash[ni] & (settled[idx] < amount)).astype(float))
            settled[idx] -= amount
        leg.open[idx] = True
        leg.q[idx] = q
        leg.px[idx] = px
        leg.days[idx] = 0
        leg.aux[idx] = px
        leg.k[idx] = leg.qe_k[idx]
        leg.cin[idx] = c_in
        leg.t0[idx] = t
        leg.qe[idx] = False
        leg.qe_q[idx] = 0.0

    for t in range(T):
        act = rows.active[:, t]
        if not act.any():
            continue
        y = yidx[t]
        o_, h_, l_, c_ = rows.o[:, t], rows.h[:, t], rows.lo[:, t], rows.c[:, t]
        # 0. accounts opening today: base bought at the previous close
        ini = rows.init[:, t]
        if ini.any():
            p0 = rows.prevc[ini, t][:, None]
            hs0 = np.maximum(rows.hs_bps[ini][:, None] / 1e4, 0.005 / p0)
            b0 = np.floor(bf[None, :] * (START_EQUITY - BUFFER) / (p0 * (1 + hs0)))
            c0 = cost_vec(b0, np.broadcast_to(p0, b0.shape), False, rows.hs_bps[ini][:, None], cm)
            base[ini] = b0
            settled[ini] = START_EQUITY - b0 * p0 - c0
            pending[ini] = 0.0
            vprev[ini] = START_EQUITY
            dead[ini] = False
            S.reset_rows(ini)
            B.reset_rows(ini)
        # 1. splits
        r = rows.split[:, t]
        if (r != 1.0).any():
            rr = r[:, None]
            base[:] = np.round(base * rr)
            for leg in (S, B):
                leg.q[:] = np.round(leg.q * rr)
                leg.qe_q[:] = np.round(leg.qe_q * rr)
                leg.px[:] = leg.px / rr
                leg.aux[:] = leg.aux / rr
        # 2. T+1 settlement, dividends, margin interest
        settled += pending
        pending[:] = 0.0
        d = rows.div[:, t]
        if (d != 0).any():
            settled += (base - S.q + B.q) * d[:, None]
        if any_margin:
            neg = margin[None, :] & (settled < 0)
            if neg.any():
                settled[neg] -= -settled[neg] * (rows.rf[t] + MARGIN_SPREAD / TRADING_DAYS)
        # 3. the open: queued exits, then queued entries
        for leg, sf in ((S, True), (B, False)):
            if leg.qx.any():
                idx = np.nonzero(leg.qx)
                close_leg(leg, sf, idx, o_[idx[0]], "target", t, y)
        for leg, sf in ((S, True), (B, False)):
            if leg.qe.any():
                idx = np.nonzero(leg.qe)
                open_leg(leg, sf, idx, o_[idx[0]], t, y)
        S.days += S.open
        B.days += B.open
        # 4. resting limits (SMA / TARGET exits)
        for leg, sf in ((S, True), (B, False)):
            m = leg.open & lim_ex[None, :]
            if not m.any():
                continue
            idx = np.nonzero(m)
            ri, ni = idx
            if sf:
                L = np.where(is_sma[ni], floor_cent(rows.sma_prev[ri, smai[ni], t]), floor_cent(leg.px[idx] * (1 - leg.k[idx])))
            else:
                L = np.where(is_sma[ni], ceil_cent(rows.sma_prev[ri, smai[ni], t]), ceil_cent(leg.px[idx] * (1 + leg.k[idx])))
            ok = np.isfinite(L)
            Lz = np.where(ok, L, 1.0)
            thr = np.maximum(TICK, THROUGH_FRAC * Lz)
            q = leg.q[idx]
            if sf:
                afford = margin[ni] | (settled[idx] >= q * Lz + cost(q, Lz, False, ri) + BUFFER)
                at_open = o_[ri] <= Lz
                through = l_[ri] <= Lz - thr + 1e-9
            else:
                afford = np.ones(len(ri), bool)
                at_open = o_[ri] >= Lz
                through = h_[ri] >= Lz + thr - 1e-9
            fill = ok & afford & (at_open | through)
            if fill.any():
                px = np.where(at_open, o_[ri], Lz)[fill]
                close_leg(leg, sf, (ri[fill], ni[fill]), px, "target", t, y)
        # 5. time stop at the close, account end
        lastday = rows.last[:, t]
        for leg, sf in ((S, True), (B, False)):
            m = leg.open & (leg.days >= hold[None, :])
            if m.any():
                idx = np.nonzero(m)
                close_leg(leg, sf, idx, c_[idx[0]], "timeout", t, y)
            if lastday.any():
                m = leg.open & lastday[:, None]
                if m.any():
                    idx = np.nonzero(m)
                    close_leg(leg, sf, idx, c_[idx[0]], "end", t, y)
                leg.qe[lastday] = False
                leg.qx[lastday] = False
        # 6. close signals for the next open
        cont = act & ~lastday
        if cont.any():
            for leg, sf in ((S, True), (B, False)):
                m = leg.open & (trail | oppx)[None, :] & cont[:, None]
                if m.any():
                    idx = np.nonzero(m)
                    ri, ni = idx
                    cc = c_[ri]
                    if sf:
                        leg.aux[idx] = np.minimum(leg.aux[idx], cc)
                        sig_t = np.where(trail[ni], cc >= leg.aux[idx] * (1 + leg.k[idx]), rows.opp_s[ri, meas[ni], t])
                    else:
                        leg.aux[idx] = np.maximum(leg.aux[idx], cc)
                        sig_t = np.where(trail[ni], cc <= leg.aux[idx] * (1 - leg.k[idx]), rows.opp_b[ri, meas[ni], t])
                    if sig_t.any():
                        leg.qx[ri[sig_t], ni[sig_t]] = True
            reg_ok = (~up_req)[None, :] | rows.up[:, t][:, None]
            for leg, sf, do, sig in ((S, True, doS, rows.sig_hi), (B, False, doB, rows.sig_lo)):
                trig = np.take(sig[:, :, t], combo, axis=1)
                m = trig & do[None, :] & ~leg.open & ~leg.qe & reg_ok & cont[:, None]
                if not m.any():
                    continue
                idx = np.nonzero(m)
                ri, ni = idx
                q = np.floor(frac[ni] * base[idx])
                if not sf:
                    capq = np.floor(np.maximum(0.0, settled[idx] + pending[idx] - BUFFER) / (CAP_MULT * c_[ri]))
                    q = np.where(cash[ni], np.minimum(q, capq), q)
                go = q > 0
                gi = (ri[go], ni[go])
                leg.qe[gi] = True
                leg.qe_q[gi] = q[go]
                leg.qe_k[gi] = K_SIGMA * rows.sigma[ri[go], t]
        # 7. value, exposure
        held = base - S.q + B.q
        cc2 = c_[:, None]
        v = held * cc2 + settled + pending
        x = np.where(v > 0, held * cc2 / np.where(v > 0, v, 1.0), 0.0)
        # 8. month-end base rebalance (cash accounts, nothing open or queued)
        if month_end[t]:
            m = cash[None, :] & cont[:, None] & ~S.open & ~B.open & ~S.qe & ~B.qe & (v > 0)
            if m.any():
                w = base * cc2 / np.where(v > 0, v, 1.0)
                lo_, hi_ = bf - BAND_HALF, bf + BAND_HALF
                m &= ~((lo_[None, :] <= w) & (w <= hi_[None, :]))
            if m.any():
                idx = np.nonzero(m)
                ri, ni = idx
                cx = c_[ri]
                hsx = np.maximum(rows.hs_bps[ri] / 1e4, 0.005 / cx)
                nb = np.floor(bf[ni] * (v[idx] - BUFFER) / (cx * (1 + hsx)))
                dq = nb - base[idx]
                capb = np.floor(np.maximum(0.0, (settled[idx] - BUFFER) / (cx * (1 + hsx) + 0.01)))
                dq = np.where(dq > 0, np.minimum(dq, capb), dq)
                buy = dq > 0
                c_b = cost(np.abs(dq), cx, False, ri)
                c_s = cost(np.abs(dq), cx, True, ri)
                settled[idx] -= np.where(buy, dq * cx + c_b, 0.0)
                pending[idx] += np.where(dq < 0, -dq * cx - c_s, 0.0)
                base[idx] += dq
                v[idx] = (base[idx] - S.q[idx] + B.q[idx]) * cx + settled[idx] + pending[idx]
        # 9. ruin (margin accounts), returns, accumulators
        if any_margin:
            newdead = (v <= 0) & ~dead
            if newdead.any():
                acc["nRuin"][y] += newdead.sum(0)
                dead |= newdead
        ret = np.where(vprev > 0, np.where(dead, np.maximum(v, 0.0), v) / np.where(vprev > 0, vprev, 1.0) - 1, 0.0)
        ret = np.maximum(ret, -1.0)
        x = np.where(dead, 0.0, x)
        v = np.where(dead, 0.0, v)
        a = act.astype(float)[:, None]
        na = float(act.sum())
        Rb = (ret * a).sum(0) / na
        Xb = (x * a).sum(0) / na
        Mt = float(rows.tr[act, t].mean())
        Mday[t] = Mt
        ft = rows.rf[t] + MARGIN_SPREAD / TRADING_DAYS
        acc["sR"][y] += Rb
        acc["sR2"][y] += Rb * Rb
        acc["sRM"][y] += Rb * Mt
        acc["sLog"][y] += np.log(np.maximum(1 + Rb, 1e-12))
        acc["sX"][y] += Xb
        g = glob
        g["n"][y] += 1
        g["sM"][y] += Mt
        g["sM2"][y] += Mt * Mt
        g["sF"][y] += ft
        g["acct_days"][y] += na
        ot = rows.oneq[t]
        if np.isfinite(ot):
            acc["sRO"][y] += Rb * ot
            acc["sR_o"][y] += Rb
            acc["sR2_o"][y] += Rb * Rb
            g["nO"][y] += 1
            g["sO"][y] += ot
            g["sO2"][y] += ot * ot
            g["sMO"][y] += Mt * ot
        if record:
            rec["r"][t] = ret
            rec["x"][t] = x
            rec["R"][t] = Rb
            rec["M"][t] = Mt
        vprev = v
    out = {"years": uy, "acc": acc, "glob": glob, "M": Mday, "f": rows.rf + MARGIN_SPREAD / TRADING_DAYS,
           "oneq": rows.oneq, "sessions": rows.sessions}
    if record:
        out["rec"] = rec
    if record_trades:
        out["trades"] = trades
    return out


# ======================================================================== window statistics from accumulators

def wstats(sim: dict, ymask: np.ndarray) -> dict:
    """Per-configuration statistics over the years in ``ymask`` (daily excess vs H_match with the window w)."""
    A = {k: v[ymask].sum(0) for k, v in sim["acc"].items()}
    G = {k: v[ymask].sum() for k, v in sim["glob"].items()}
    n = G["n"]
    w = A["sX"] / n
    fin = np.maximum(w - 1, 0.0)
    d_mean = (A["sR"] - w * G["sM"]) / n
    mean_e = d_mean + fin * G["sF"] / n
    var_e = np.maximum((A["sR2"] - 2 * w * A["sRM"] + w * w * G["sM2"]) / n - d_mean ** 2, 1e-30)
    sd = np.sqrt(var_e)
    acct_years = G["acct_days"] / TRADING_DAYS
    nT = np.maximum(A["nT"], 1)
    out = {"n": n, "w": w, "mean_e": mean_e, "ann_excess_arith": mean_e * TRADING_DAYS,
           "ir": mean_e / sd * math.sqrt(TRADING_DAYS), "t": mean_e / sd * math.sqrt(n), "sr_daily": mean_e / sd,
           "cagr": np.exp(A["sLog"] * TRADING_DAYS / n) - 1,
           "trips_per_acct_year": A["nT"] / acct_years, "win_rate": np.where(A["nT"] > 0, A["nW"] / nT, np.nan),
           "gross_bp": np.where(A["nT"] > 0, A["sG"] / nT, np.nan), "net_bp": np.where(A["nT"] > 0, A["sNet"] / nT, np.nan),
           "cost_bp": np.where(A["nT"] > 0, A["sC"] / nT, np.nan),
           "share_target": np.where(A["nT"] > 0, A["nTarget"] / nT, np.nan),
           "mean_days": np.where(A["nT"] > 0, A["sDays"] / nT, np.nan),
           "same_day_per_acct_year": A["nSame"] / acct_years, "shortfall": A["nShort"], "ruined": A["nRuin"]}
    if G["nO"] > 0:   # arithmetic excess vs ONEQ on the ONEQ days
        mo = (A["sR_o"] - G["sO"]) / G["nO"]
        vo = np.maximum((A["sR2_o"] - 2 * A["sRO"] + G["sO2"]) / G["nO"] - mo ** 2, 1e-30)
        out["ann_excess_oneq_arith"] = mo * TRADING_DAYS
        out["t_oneq_daily"] = mo / np.sqrt(vo) * math.sqrt(G["nO"])
    return out


def geo_hmatch(sim: dict, ymask: np.ndarray, w: np.ndarray) -> np.ndarray:
    """CAGR of the basket-level H_match (w * M_t - max(w-1, 0) * f_t, daily) for each configuration's w."""
    years = sim["sessions"].year.to_numpy()
    sel = np.isin(years, sim["years"][ymask]) & np.isfinite(sim["M"])
    M, f = sim["M"][sel], sim["f"][sel]
    fin = np.maximum(w - 1, 0.0)
    s = np.zeros_like(w)
    for mt, ft in zip(M, f):
        s += np.log(np.maximum(1 + w * mt - fin * ft, 1e-12))
    return np.exp(s * TRADING_DAYS / len(M)) - 1


def series_cagr(r: pd.Series) -> float:
    return cagr_of(r.dropna())


# ======================================================================== data loading

def load_all(smoke: bool = False) -> dict:
    qqq = st.load_qqq_like("QQQ")
    oneq = st.load_qqq_like("ONEQ")
    master = qqq.s.sessions
    rf = fill_rf(load_kf_rf(END), load_dtb3(END), master)
    oneq_tr = oneq.s.tr.reindex(master)
    oneq_tr[oneq_tr.index < pd.Timestamp(st.ONEQ_FIRST_RETURN)] = np.nan
    checks = {"QQQ": qqq.s.checks, "ONEQ": oneq.s.checks}

    def from_market(mk: st.Market, hs: float) -> Inst:
        s = mk.s
        return make_inst(mk.sym, hs, s.sessions, master, s.open, s.high, s.low, s.close, s.split, s.div,
                         s.tr.to_numpy(float), mk.factor)
    insts = {"QQQ": from_market(qqq, st.HALF_SPREAD_BPS["QQQ"])}
    stocks = st.STOCKS[:3] if smoke else st.STOCKS
    pc_rows = []
    for sym in stocks:
        mk = st.load_stock(sym)
        pc = st.panel_check(mk)
        insts[sym] = from_market(mk, st.STOCK_HALF_SPREAD_BPS)
        checks[sym] = {**{k: v for k, v in mk.s.checks.items()}, **pc, **insts[sym].checks}
        pc_rows.append({"symbol": sym, **{k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                                          for k, v in checks[sym].items() if k != "first_real_close"}})
    # T10: point-in-time top-10 by market cap, v2 panel closes
    top = pd.read_csv(TOP20)
    top = top[top["rank"] <= 10].copy()
    top["hold_month"] = pd.PeriodIndex(pd.to_datetime(top["s"]), freq="M") + 1
    sids = sorted(top["security_id"].astype(str).unique())
    if smoke:
        sids = [x for x in sids if x in ("320193", "789019", "858877", "50863", "1018724", "1045810", "804328",
                                         "1341439", "1288776.A", "1652044.A", "77476")]
        top = top[top["security_id"].astype(str).isin(sids)]
    t10_master = master[master <= pd.Timestamp(T10_END)]
    t10_insts, t10_checks = {}, []
    for sid in sids:
        p = pd.read_csv(PANEL / f"{sid}.csv", usecols=["date", "close_raw", "split_factor", "div_cash", "tr"])
        p = p[p["close_raw"] > 0].drop_duplicates("date", keep="last")
        p = p[p["date"] <= T10_END].reset_index(drop=True)
        d = pd.DatetimeIndex(p["date"])
        extra = d.difference(t10_master)
        if len(extra):
            p = p[~d.isin(extra)].reset_index(drop=True)
            d = pd.DatetimeIndex(p["date"])
        c = p["close_raw"].to_numpy(float)
        split = p["split_factor"].fillna(1.0).to_numpy(float)
        split[split <= 0] = 1.0
        factor = np.r_[np.cumprod(split[::-1])[::-1][1:], 1.0]    # product of split ratios after t
        ins = make_inst(sid, st.STOCK_HALF_SPREAD_BPS, d, t10_master, c, c, c, c, split,
                        p["div_cash"].fillna(0.0).to_numpy(float), p["tr"].fillna(0.0).to_numpy(float), factor)
        t10_insts[sid] = ins
        t10_checks.append({"security_id": sid, "first": str(d[0].date()), "last": str(d[-1].date()), "rows": len(d),
                           "dropped_non_calendar": int(len(extra)), **ins.checks,
                           "first_valid": str(t10_master[ins.first_valid].date()) if ins.first_valid < len(t10_master) else ""})
    spells = t10_spells(top, t10_master, t10_insts)
    return {"master": master, "t10_master": t10_master, "rf": rf, "oneq_tr": oneq_tr, "oneq": oneq, "qqq": qqq,
            "insts": insts, "stocks": list(stocks), "t10_insts": t10_insts, "t10_spells": spells, "checks": checks,
            "pc_rows": pc_rows, "t10_checks": t10_checks}


def t10_spells(top: pd.DataFrame, master: pd.DatetimeIndex, insts: dict) -> list:
    """10 slots; a member keeps its slot (and its account) while it stays in the top 10. Spells are
    (slot, security_id, first master index, last master index); an account needs a real row on the day before it
    opens (the base is bought at that close) and ends at its last real session."""
    months = master.to_period("M")
    slots: list = [None] * 10
    cur: dict = {}
    spells = []
    for hm in pd.period_range(pd.Period(T10_START, "M"), months[-1], freq="M"):
        mem = sorted(top.loc[top["hold_month"] == hm, "security_id"].astype(str))
        days = np.nonzero(months == hm)[0]
        for k in range(10):
            if slots[k] is not None and slots[k] not in mem:
                if cur.get(k) is not None:
                    spells.append(cur[k])
                cur[k] = None
                slots[k] = None
        for sid in mem:
            if sid not in slots:
                k = slots.index(None)
                slots[k] = sid
                cur[k] = None
        for k in range(10):
            sid = slots[k]
            if sid is None or not len(days):
                continue
            ins = insts[sid]
            have = days[ins.has[days]]
            if cur.get(k) is None:
                can_open = have[(have > 0) & ins.has[np.maximum(have - 1, 0)]]
                if not len(can_open):
                    continue
                cur[k] = (k, sid, int(can_open[0]), int(have[-1]))
            elif len(have):
                cur[k] = (k, sid, cur[k][2], int(have[-1]))
    spells += [sp for sp in cur.values() if sp is not None]
    return sorted(spells, key=lambda x: (x[2], x[0]))


def base_rows(data: dict, base: str, start: str | None = None, end: str | None = None) -> Rows:
    rf, oneq = data["rf"], data["oneq_tr"]
    if base == "QQQ":
        ins = data["insts"]["QQQ"]
        s0 = start or str(data["master"][ins.first_valid].date())
        return build_rows("QQQ", {"QQQ": ins}, [(0, "QQQ", ins.first_valid, len(data["master"]) - 1)], data["master"],
                          s0, end or END, rf, oneq)
    if base == "U18":
        ins = {s: data["insts"][s] for s in data["stocks"]}
        spells = [(k, s, max(ins[s].first_valid, int(data["master"].searchsorted(pd.Timestamp(U18_START)))),
                   len(data["master"]) - 1) for k, s in enumerate(data["stocks"])]
        return build_rows("U18", ins, spells, data["master"], start or U18_START, end or END, rf, oneq)
    return build_rows("T10", data["t10_insts"], data["t10_spells"], data["t10_master"], start or T10_START,
                      end or T10_END, rf, oneq)


# ======================================================================== parallel grid runs

_ROWS: dict = {}


def _init_worker(paths: dict):
    for k, p in paths.items():
        with open(p, "rb") as fh:
            _ROWS[k] = pickle.load(fh)


def _job(args):
    base, cname, lo, hi = args
    codes = cfg_arrays(grid().iloc[lo:hi])
    on, mult = COSTS[cname]
    t0 = time.time()
    sim = simulate_grid(_ROWS[base], codes, CostModel(on, mult))
    return base, cname, lo, hi, sim["acc"], sim["glob"], sim["M"], time.time() - t0


def run_grid_all(data: dict, bases, costs, workers: int, chunk: int, n_configs: int) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    paths = {}
    rows_by = {}
    for b in bases:
        rows_by[b] = base_rows(data, b)
        paths[b] = CACHE / f"rows_{b}.pkl"
        with open(paths[b], "wb") as fh:
            pickle.dump(rows_by[b], fh, protocol=pickle.HIGHEST_PROTOCOL)
    jobs = [(b, c, lo, min(lo + chunk, n_configs)) for b in bases for c in costs for lo in range(0, n_configs, chunk)]
    # biggest jobs first
    jobs.sort(key=lambda j: -rows_by[j[0]].c.size)
    parts: dict = {}
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=({k: str(v) for k, v in paths.items()},)) as ex:
        for b, c, lo, hi, acc, glob, M, dt in ex.map(_job, jobs):
            parts.setdefault((b, c), []).append((lo, acc, glob, M))
            print(f"  grid {b} {c} [{lo}:{hi}] {dt:.0f}s (elapsed {time.time() - t0:.0f}s)", flush=True)
    sims = {}
    for (b, c), ps in parts.items():
        ps.sort(key=lambda p: p[0])
        rows = rows_by[b]
        years = np.unique(rows.sessions.year.to_numpy())
        sims[(b, c)] = {"years": years, "acc": {k: np.concatenate([p[1][k] for p in ps], axis=1) for k in ACC_KEYS},
                        "glob": ps[0][2], "M": ps[0][3], "f": rows.rf + MARGIN_SPREAD / TRADING_DAYS,
                        "oneq": rows.oneq, "sessions": rows.sessions}
        np.savez_compressed(CACHE / f"acc_{b}_{c}.npz", years=years, M=ps[0][3],
                            **{f"acc_{k}": v for k, v in sims[(b, c)]["acc"].items()},
                            **{f"glob_{k}": v for k, v in ps[0][2].items()})
    return sims


# ======================================================================== exact evaluation of chosen configurations

def exact_run(data: dict, base: str, ids: list, start: str | None, end: str | None, cm: CostModel,
              record_trades: bool = False) -> dict:
    """Fresh $10k accounts over [start, end]; configs ``ids`` plus the 4 references. Daily basket series. With
    ``record_trades``, ``out["trades"]`` lists every T-trade (``simulate_grid``; ``cfg`` k = ``ids[k]``, then the
    references)."""
    rows = base_rows(data, base, start, end)
    codes_df = pd.concat([grid().iloc[list(ids)], ref_codes()], ignore_index=True)
    sim = simulate_grid(rows, cfg_arrays(codes_df), cm, record=True, record_trades=record_trades)
    rec = sim["rec"]
    sess = rows.sessions
    act = rec["act"]                     # (T, R)
    acct = np.cumsum(rows.init.T, axis=0)  # account number per row
    tr = rows.tr.T                        # (T, R)
    f = sim["f"]
    out = {"rows": rows, "sim": sim, "ids": list(ids), "sessions": sess, "act": act, "acct": acct, "tr": tr, "f": f}
    out["R"] = {i: pd.Series(rec["R"][:, k], index=sess) for k, i in enumerate(list(ids))}
    nref = len(ids)
    for j, nm in enumerate(("H100", "H_base_res10", "H_base_res25", "H_base_res50")):
        out[nm] = pd.Series(rec["R"][:, nref + j], index=sess)
    out["oneq"] = pd.Series(rows.oneq, index=sess)
    if record_trades:
        out["trades"] = sim["trades"]
    return out


def hmatch_series(ex: dict, k: int, group_by_year: bool = False) -> pd.Series:
    """Per-account H_match (account's own mean exposure over the window, or per calendar year), basket mean."""
    rec = ex["sim"]["rec"]
    x = rec["x"][:, :, k]
    act = ex["act"]
    T, R = act.shape
    yr = ex["sessions"].year.to_numpy()
    key = ex["acct"] * 10_000 + (yr[:, None] if group_by_year else 0) + np.arange(R)[None, :] * 10_000_000
    kk = key[act]
    xs = x[act]
    uk, inv = np.unique(kk, return_inverse=True)
    wbar = np.bincount(inv, weights=xs) / np.bincount(inv)
    w = np.zeros((T, R))
    w[act] = wbar[inv]
    hm = w * ex["tr"] - np.maximum(w - 1, 0) * ex["f"][:, None]
    hm = np.where(act, hm, 0.0)
    na = act.sum(1)
    s = pd.Series(np.where(na > 0, hm.sum(1) / np.maximum(na, 1), np.nan), index=ex["sessions"])
    return s


def trip_summary(ex: dict, k: int) -> dict:
    A = {kk: v[:, k].sum() for kk, v in ex["sim"]["acc"].items()}
    G = {kk: v.sum() for kk, v in ex["sim"]["glob"].items()}
    nT = max(A["nT"], 1)
    return {"trips_per_acct_year": A["nT"] / (G["acct_days"] / TRADING_DAYS), "trips": int(A["nT"]),
            "win_rate_net": A["nW"] / nT if A["nT"] else float("nan"),
            "gross_per_trip_bp": A["sG"] / nT if A["nT"] else float("nan"),
            "net_per_trip_bp": A["sNet"] / nT if A["nT"] else float("nan"),
            "cost_per_trip_bp": A["sC"] / nT if A["nT"] else float("nan"),
            "share_target": A["nTarget"] / nT if A["nT"] else float("nan"),
            "share_timeout": A["nTimeout"] / nT if A["nT"] else float("nan"),
            "mean_days": A["sDays"] / nT if A["nT"] else float("nan"),
            "day_trades_per_acct_year": A["nSame"] / (G["acct_days"] / TRADING_DAYS),
            "shortfall": int(A["nShort"]), "ruined_accounts": int(A["nRuin"])}


def metrics_vs(r: pd.Series, refs: dict, oneq: pd.Series | None, rf: pd.Series, exposure: float | None = None) -> dict:
    r = r.dropna()
    v0 = pd.concat([pd.Series([1.0]), (1 + r).cumprod().reset_index(drop=True)])
    ex = r - rf.reindex(r.index).fillna(0)
    m = {"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "sessions": int(len(r)), "cagr": cagr_of(r),
         "vol": float(r.std() * math.sqrt(TRADING_DAYS)), "max_dd": max_drawdown(v0),
         "sharpe": float(ex.mean() / ex.std() * math.sqrt(TRADING_DAYS)) if ex.std() > 0 else float("nan")}
    if exposure is not None:
        m["exposure"] = exposure
    for tag, ref in refs.items():
        ref = ref.reindex(r.index)
        m[f"{tag}_cagr"] = cagr_of(ref)
        m[f"excess_vs_{tag}"] = m["cagr"] - cagr_of(ref)
        t, ir = t_and_ir(monthly(r) - monthly(ref))
        m[f"t_monthly_excess_vs_{tag}"], m[f"ir_vs_{tag}"] = t, ir
        d = r - ref
        m[f"t_daily_excess_vs_{tag}"] = float(d.mean() / d.std() * math.sqrt(len(d))) if d.std() > 0 else 0.0
    if oneq is not None and len(oneq) > 60:
        m.update(relative_metrics(r, oneq, rf, "oneq"))
    return m


def oneq_for(data: dict, index: pd.DatetimeIndex) -> pd.Series | None:
    """ONEQ buy and hold over the index (from 2003-10-02), one buy order at the previous close."""
    so = max(index[0], pd.Timestamp(st.ONEQ_FIRST_RETURN))
    if so >= index[-1]:
        return None
    return it.buy_hold_oneq(data["oneq"].s, data["master"], so, index[-1], True)


def hbase_of(ex: dict, reserve_code: int) -> pd.Series:
    return ex[("H100", "H_base_res10", "H_base_res25", "H_base_res50")[reserve_code]]


def eval_ids(data: dict, base: str, ids: list, start, end, cm: CostModel, rf: pd.Series) -> dict:
    """Exact metrics per configuration on [start, end] (fresh accounts)."""
    ex = exact_run(data, base, ids, start, end, cm)
    out = {}
    g = grid()
    for k, i in enumerate(ex["ids"]):
        r = ex["R"][i]
        hm = hmatch_series(ex, k)
        res_code = int(g.loc[i, "reserve"])
        refs = {"hmatch": hm, "hbase": hbase_of(ex, res_code), "h100": ex["H100"]}
        expo = float((ex["sim"]["rec"]["x"][:, :, k] * ex["act"]).sum() / ex["act"].sum())
        mm = metrics_vs(r, refs, oneq_for(data, r.dropna().index), rf, expo)
        mm.update(trip_summary(ex, k))
        mm["config_id"] = int(i)
        mm["config"] = label(g.loc[i])
        out[int(i)] = {"m": mm, "r": r, "hm": hm}
    out["_refs"] = {"H100": ex["H100"], "H_base_res25": ex["H_base_res25"]}
    return out


# ======================================================================== selection, walk-forward

def select(score: np.ndarray, mask: np.ndarray | None = None) -> dict:
    s = np.where(np.isfinite(score), score, -np.inf)
    if mask is not None:
        s = np.where(mask, s, -np.inf)
    nm = neighbour_median(np.where(np.isfinite(score), score, np.nan))
    nm = np.where(np.isfinite(nm), nm, -np.inf)
    if mask is not None:
        nm = np.where(mask, nm, -np.inf)
    return {"A": int(np.argmax(s)), "P": int(np.argmax(nm))}


def year_masks(sim: dict, base: str) -> dict:
    y = sim["years"]
    sp = HALF_SPLIT_YEAR[base]
    return {"half1": y < sp, "half2": y >= sp, "full": np.ones(len(y), bool)}


def half_dates(data: dict, base: str) -> dict:
    sp = HALF_SPLIT_YEAR[base]
    rows_start = {"QQQ": str(data["master"][data["insts"]["QQQ"].first_valid].date()), "U18": U18_START, "T10": T10_START}[base]
    end = T10_END if base == "T10" else END
    return {"half1": (rows_start, f"{sp - 1}-12-31"), "half2": (f"{sp}-01-01", end), "full": (rows_start, end)}


def walk_forward_picks(sim: dict) -> list:
    """[(year, {'A': id, 'P': id})] for each OOS year with >= WF_MIN_SESSIONS in-sample sessions."""
    y = sim["years"]
    n = sim["glob"]["n"]
    picks = []
    for j, yr in enumerate(y):
        ins = y < yr
        if n[ins].sum() < WF_MIN_SESSIONS:
            continue
        sc = wstats(sim, ins)["ir"]
        picks.append((int(yr), select(sc)))
    return picks


def stitch_wf(data: dict, base: str, picks: list, which: str, cm: CostModel, rf: pd.Series) -> dict:
    ids = sorted({p[1][which] for p in picks})
    dts = half_dates(data, base)["full"]
    ex = exact_run(data, base, ids, dts[0], dts[1], cm)
    k_of = {i: k for k, i in enumerate(ex["ids"])}
    hm_y = {i: hmatch_series(ex, k_of[i], group_by_year=True) for i in ids}
    rs, hs, h100, yrs = [], [], [], []
    for yr, sel in picks:
        i = sel[which]
        m = ex["sessions"].year == yr
        rs.append(ex["R"][i][m])
        hs.append(hm_y[i][m])
        h100.append(ex["H100"][m])
        yrs.append({"year": yr, "config_id": i, "config": label(grid().loc[i]),
                    "ret": float((1 + ex["R"][i][m]).prod() - 1), "hmatch": float((1 + hm_y[i][m]).prod() - 1),
                    "h100": float((1 + ex["H100"][m]).prod() - 1)})
    r, hm, hh = pd.concat(rs), pd.concat(hs), pd.concat(h100)
    mm = metrics_vs(r, {"hmatch": hm, "h100": hh}, oneq_for(data, r.index), rf)
    return {"m": mm, "years": yrs, "r": r, "hm": hm}


# ======================================================================== reporting helpers

def surface_table(sim: dict, base: str, extra: dict) -> pd.DataFrame:
    g = grid()
    df = g.copy()
    for nm, (k, vals) in zip([n for n, _ in AXES], AXES):
        df[nm + "_v"] = [vals[i] if i < len(vals) else None for i in g[nm]]
    for wn, ym in year_masks(sim, base).items():
        ws = wstats(sim, ym)
        hmc = geo_hmatch(sim, ym, ws["w"])
        df[f"{wn}_cagr"] = ws["cagr"]
        df[f"{wn}_excess_cagr_hmatch"] = ws["cagr"] - hmc
        df[f"{wn}_ann_excess_arith"] = ws["ann_excess_arith"]
        df[f"{wn}_ir"] = ws["ir"]
        df[f"{wn}_t"] = ws["t"]
        df[f"{wn}_exposure"] = ws["w"]
        for k in ("trips_per_acct_year", "win_rate", "gross_bp", "net_bp", "cost_bp", "share_target", "mean_days"):
            df[f"{wn}_{k}"] = ws[k]
        if "ann_excess_oneq_arith" in ws:
            df[f"{wn}_ann_excess_oneq_arith"] = ws["ann_excess_oneq_arith"]
        if wn == "full":
            df["full_sr_daily"] = ws["sr_daily"]
            df["full_shortfall"] = ws["shortfall"]
            df["full_ruined"] = ws["ruined"]
            df["full_same_day_per_acct_year"] = ws["same_day_per_acct_year"]
        for refn, refc in extra.get(wn, {}).items():
            if refn == "hbase":
                df[f"{wn}_excess_cagr_hbase"] = df[f"{wn}_cagr"] - np.array(refc)[g["reserve"].to_numpy()]
            else:
                df[f"{wn}_excess_cagr_{refn}"] = df[f"{wn}_cagr"] - refc
    df.insert(0, "config", [label(r) for _, r in g.iterrows()])
    return df


def heat_tables(df: pd.DataFrame, col: str) -> dict:
    out = {}
    pairs = (("measure_v", "exit_v"), ("level", "hold_v"), ("frac_v", "reserve_v"), ("measure_v", "level"),
             ("exit_v", "hold_v"), ("measure_v", "regime_v"))
    for d in DIRS:
        sub = df[df["direction_v"] == d]
        for a, b in pairs:
            out[f"{d}_{a}_x_{b}_median"] = sub.pivot_table(index=a, columns=b, values=col, aggfunc="median")
            out[f"{d}_{a}_x_{b}_share_pos"] = sub.pivot_table(index=a, columns=b, values=col,
                                                               aggfunc=lambda s: float((s > 0).mean()))
    return out


def marginals(df: pd.DataFrame, col: str) -> pd.DataFrame:
    rows = []
    for name, _ in AXES:
        key = name if name == "level" else name + "_v"
        for v, sub in df.groupby(key):
            rows.append({"axis": name, "value": str(v), "n": len(sub), "median": float(sub[col].median()),
                         "share_pos": float((sub[col] > 0).mean()), "p10": float(sub[col].quantile(0.1)),
                         "p90": float(sub[col].quantile(0.9))})
    return pd.DataFrame(rows)


def bh_count(p: np.ndarray, q: float = 0.05) -> int:
    return int(st.bh_reject(p, q).sum())


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    m = np.isfinite(a) & np.isfinite(b)
    ra = pd.Series(a[m]).rank().to_numpy()
    rb = pd.Series(b[m]).rank().to_numpy()
    return float(np.corrcoef(ra, rb)[0, 1])


# ======================================================================== main analysis

def analyse(data: dict, sims: dict, bases, costs, rf: pd.Series) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    g = grid()
    res = {"end": END, "n_grid_per_base": N_GRID, "n_trials_total": N_GRID * len(bases),
           "bonferroni_t_per_base": bonferroni_t(N_GRID), "bonferroni_t_total": bonferroni_t(N_GRID * len(bases)),
           "bonferroni_t_6_selector_runs": bonferroni_t(6), "bases": {}}
    pooled_p = []
    for base in bases:
        t0 = time.time()
        sim = sims[(base, "base")]
        hd = half_dates(data, base)
        masks = year_masks(sim, base)
        # references on the same continuous window (for surface excess vs H_base / H100 / ONEQ)
        ref_ex = exact_run(data, base, [], hd["full"][0], hd["full"][1], CostModel())
        extra = {}
        for wn, ym in masks.items():
            yrs = set(sim["years"][ym])
            sel = ref_ex["sessions"].year.isin(list(yrs))
            hb = [cagr_of(ref_ex[nm][sel]) for nm in ("H100", "H_base_res10", "H_base_res25", "H_base_res50")]
            extra[wn] = {"hbase": hb, "h100": hb[0]}
            on = ref_ex["oneq"][sel].dropna()
            if len(on) > 250:
                extra[wn]["oneq_overlap"] = cagr_of(on)
        df = surface_table(sim, base, extra)
        for cname in costs:
            if cname == "base" or (base, cname) not in sims:
                continue
            s2 = sims[(base, cname)]
            for wn, ym in year_masks(s2, base).items():
                ws = wstats(s2, ym)
                df[f"{wn}_ir_{cname}"] = ws["ir"]
                df[f"{wn}_excess_cagr_hmatch_{cname}"] = ws["cagr"] - geo_hmatch(s2, ym, ws["w"])
        df.to_csv(OUT / f"surface_{base}.csv.gz", index=False, float_format="%.6g", compression="gzip")
        col = "full_excess_cagr_hmatch"
        ht = heat_tables(df, col)
        with open(OUT / f"heat_{base}.txt", "w") as fh:
            fh.write(f"# {base}: median / share>0 of full-window CAGR excess vs H_match (basket-level w), by pairs of axes\n")
            for k, v in ht.items():
                fh.write(f"\n## {k}\n{(v * (100 if 'median' in k else 1)).round(3).to_string()}\n")
        marg = marginals(df, col)
        marg.to_csv(OUT / f"marginals_{base}.csv", index=False, float_format="%.6g")
        b = {"windows": {k: list(v) for k, v in hd.items()}}
        # ---------------- surface summary
        summ = {}
        for wn in ("half1", "half2", "full"):
            c_ = df[f"{wn}_excess_cagr_hmatch"]
            summ[wn] = {"median_excess_cagr_hmatch": float(c_.median()), "share_beating_hmatch": float((c_ > 0).mean()),
                        "p10": float(c_.quantile(0.1)), "p90": float(c_.quantile(0.9)), "max": float(c_.max()),
                        "median_ir": float(df[f"{wn}_ir"].median()),
                        "share_beating_h100": float((df[f"{wn}_excess_cagr_h100"] > 0).mean()),
                        "share_beating_hbase": float((df[f"{wn}_excess_cagr_hbase"] > 0).mean()),
                        "median_excess_cagr_hbase": float(df[f"{wn}_excess_cagr_hbase"].median()),
                        "median_excess_cagr_h100": float(df[f"{wn}_excess_cagr_h100"].median())}
            if "oneq_overlap" in extra[wn]:
                summ[wn]["share_ann_arith_beating_oneq"] = float((df[f"{wn}_ann_excess_oneq_arith"] > 0).mean()) \
                    if f"{wn}_ann_excess_oneq_arith" in df else None
            for cname in costs:
                if f"{wn}_excess_cagr_hmatch_{cname}" in df:
                    cc = df[f"{wn}_excess_cagr_hmatch_{cname}"]
                    summ[wn][f"share_beating_hmatch_{cname}"] = float((cc > 0).mean())
                    summ[wn][f"median_excess_cagr_hmatch_{cname}"] = float(cc.median())
        by_dir = {}
        for d in DIRS:
            sub = df[df["direction_v"] == d]
            by_dir[d] = {wn: {"median": float(sub[f"{wn}_excess_cagr_hmatch"].median()),
                              "share_pos": float((sub[f"{wn}_excess_cagr_hmatch"] > 0).mean()),
                              "median_gross_bp": float(sub[f"{wn}_gross_bp"].median()),
                              "median_net_bp": float(sub[f"{wn}_net_bp"].median())} for wn in ("half1", "half2", "full")}
        summ["by_direction"] = by_dir
        # median configuration (by full-window excess vs H_match)
        order = df[col].rank(method="first")
        med_i = int((order - (len(df) + 1) / 2).abs().idxmin())
        summ["median_config"] = {"config_id": med_i, "config": df.loc[med_i, "config"],
                                 **{k: float(df.loc[med_i, k]) for k in ("full_excess_cagr_hmatch", "full_ir",
                                                                          "full_trips_per_acct_year", "full_exposure")}}
        b["surface"] = summ
        # ---------------- stability
        i1, i2 = df["half1_ir"].to_numpy(), df["half2_ir"].to_numpy()
        top1 = np.argsort(-np.nan_to_num(i1, nan=-9))[: max(1, N_GRID // 100)]
        pct2 = pd.Series(i2).rank(pct=True).to_numpy()
        top50 = np.argsort(-np.nan_to_num(df["full_ir"].to_numpy(), nan=-9))[:50]
        nb_share = []
        full_ex = df[col].to_numpy().reshape(SHAPE)
        for i in top50:
            ix = np.unravel_index(i, SHAPE)
            vals = []
            for name in ORDINAL:
                ax = [n for n, _ in AXES].index(name)
                for sh in (-1, 1):
                    j = list(ix)
                    j[ax] += sh
                    if 0 <= j[ax] < SHAPE[ax]:
                        vals.append(full_ex[tuple(j)])
            nb_share.append(float(np.mean(np.array(vals) > 0)) if vals else float("nan"))
        b["stability"] = {"spearman_ir_half1_half2": spearman(i1, i2),
                          "spearman_excess_half1_half2": spearman(df["half1_excess_cagr_hmatch"].to_numpy(),
                                                                  df["half2_excess_cagr_hmatch"].to_numpy()),
                          "top1pct_half1_median_half2_pct_rank": float(np.median(pct2[top1])),
                          "top1pct_half1_share_pos_half2": float((df["half2_excess_cagr_hmatch"].to_numpy()[top1] > 0).mean()),
                          "top50_full_mean_share_neighbours_pos": float(np.nanmean(nb_share)),
                          "top50_full_configs": [df.loc[int(i), "config"] for i in top50[:10]]}
        for d in DIRS:
            m = (df["direction_v"] == d).to_numpy()
            b["stability"][f"spearman_ir_halves_{d}"] = spearman(i1[m], i2[m])
        # ---------------- multiple testing on the full window
        tfull = df["full_t"].to_numpy()
        p = np.array([1 - NORM.cdf(x) if np.isfinite(x) else 1.0 for x in tfull])
        pooled_p.append(p)
        best = int(np.nanargmax(df["full_ir"].to_numpy()))
        b["multiple_testing"] = {"bh_rejections_q05": bh_count(p), "n_t_above_bonferroni_base": int((tfull >= bonferroni_t(N_GRID)).sum()),
                                 "n_t_above_2": int((tfull >= 2).sum()), "share_t_above_2": float((tfull >= 2).mean()),
                                 "share_t_below_minus2": float((tfull <= -2).mean()),
                                 "best_full_ir_config": df.loc[best, "config"], "best_full_ir": float(df.loc[best, "full_ir"]),
                                 "best_full_t": float(df.loc[best, "full_t"])}
        # DSR of the full-window best (exact daily excess vs H_match)
        exb = eval_ids(data, base, [best], hd["full"][0], hd["full"][1], CostModel(), rf)
        dex = (exb[best]["r"] - exb[best]["hm"]).dropna()
        sr = float(dex.mean() / dex.std())
        z = (dex - dex.mean()) / dex.std()
        skew, kurt = float((z ** 3).mean()), float((z ** 4).mean())
        b["multiple_testing"]["dsr_best"] = deflated_sharpe(sr, len(dex), skew, kurt, df["full_sr_daily"].dropna().to_numpy())
        b["multiple_testing"]["best_exact_full"] = exb[best]["m"]
        # ---------------- two-fold selection
        folds = {}
        sel_info = {}
        for fold, (ins, oos) in {"fold1": ("half1", "half2"), "fold2": ("half2", "half1")}.items():
            sc = df[f"{ins}_ir"].to_numpy()
            pick = select(sc)
            for d_i, d in enumerate(DIRS):
                pick[f"A_{d}"] = select(sc, (g["direction"] == d_i).to_numpy())["A"]
            sel_info[fold] = {k: {"config_id": v, "config": df.loc[v, "config"], "in_sample_ir": float(df.loc[v, f"{ins}_ir"]),
                                  "in_sample_excess_cagr_hmatch": float(df.loc[v, f"{ins}_excess_cagr_hmatch"]),
                                  "in_sample_rank_pct": float(pd.Series(sc).rank(pct=True)[v]),
                                  "oos_surface_ir": float(df.loc[v, f"{oos}_ir"]),
                                  "oos_surface_excess_cagr_hmatch": float(df.loc[v, f"{oos}_excess_cagr_hmatch"]),
                                  "oos_surface_rank_pct": float(df[f"{oos}_ir"].rank(pct=True)[v])}
                              for k, v in pick.items()}
            ids = sorted(set(pick.values()))
            folds[fold] = {}
            for cname in costs:
                on, mult = COSTS[cname]
                ev = eval_ids(data, base, ids, hd[oos][0], hd[oos][1], CostModel(on, mult), rf)
                folds[fold][cname] = {k: ev[v]["m"] for k, v in pick.items()}
                if cname == "base":
                    folds[fold]["_series"] = {k: (ev[v]["r"], ev[v]["hm"]) for k, v in pick.items()}
        b["selection"] = sel_info
        b["two_fold"] = {f: {c: v for c, v in d.items() if not c.startswith("_")} for f, d in folds.items()}
        # ---------------- walk-forward
        picks = walk_forward_picks(sim)
        wf = {}
        for which in ("A", "P"):
            wf[which] = {}
            for cname in costs:
                on, mult = COSTS[cname]
                sres = stitch_wf(data, base, picks, which, CostModel(on, mult), rf)
                wf[which][cname] = {"m": sres["m"], "years": sres["years"]}
        b["walk_forward"] = wf
        pd.DataFrame([{"year": y["year"], "selector": w, **y} for w in ("A", "P") for y in wf[w]["base"]["years"]]).to_csv(
            OUT / f"walk_forward_{base}.csv", index=False, float_format="%.6f")
        # ---------------- verdicts
        verdict = {}
        for which in ("A", "P"):
            f1, f2 = folds["fold1"]["base"][which], folds["fold2"]["base"][which]
            x1, x2 = folds["fold1"]["x2"][which], folds["fold2"]["x2"][which]
            wm = wf[which]["base"]["m"]
            a_ = f1["excess_vs_hmatch"] > 0 and f2["excess_vs_hmatch"] > 0
            b_ = wm["excess_vs_hmatch"] > 0 and wm["t_monthly_excess_vs_hmatch"] >= 2.0
            c_ = x1["excess_vs_hmatch"] > 0 and x2["excess_vs_hmatch"] > 0
            v = {"a_two_fold": bool(a_), "b_walk_forward": bool(b_), "c_double_spread": bool(c_),
                 "survives": bool(a_ and b_ and c_)}
            if base == "QQQ":
                h1r = folds["fold2"]["_series"][which][0].dropna()
                h2r = folds["fold1"]["_series"][which][0].dropna()
                cat = pd.concat([h1r, h2r])
                oq = oneq_for(data, cat.index)
                rm_all = relative_metrics(cat, oq, rf, "oneq")
                m1 = relative_metrics(h1r, oq[oq.index <= h1r.index[-1]], rf, "oneq")
                m2 = relative_metrics(h2r, oq[oq.index >= h2r.index[0]], rf, "oneq")
                A = m1["excess_vs_oneq"] > 0 and m2["excess_vs_oneq"] > 0 and rm_all["t_monthly_excess_vs_oneq"] >= 2
                B = all(m["dd_shallower_than_oneq_pp"] >= 10 and m["excess_vs_oneq"] >= -0.03 for m in (m1, m2))
                v["oneq_cross_fitted"] = {
                    "half1": {k: m1[k] for k in ("excess_vs_oneq", "dd_shallower_than_oneq_pp", "oneq_window_start")},
                    "half2": {k: m2[k] for k in ("excess_vs_oneq", "dd_shallower_than_oneq_pp")},
                    "t_monthly_concat": rm_all["t_monthly_excess_vs_oneq"], "A": bool(A), "B": bool(B)}
                v["worth_forward_watch"] = bool(v["survives"] and (A or B))
            verdict[which] = v
        b["verdict"] = verdict
        res["bases"][base] = b
        print(f"{base}: analysed in {time.time() - t0:.0f}s; surface full share>H_match "
              f"{summ['full']['share_beating_hmatch']:.1%}, median {summ['full']['median_excess_cagr_hmatch']:+.2%}; "
              f"verdict {verdict}", flush=True)
        (OUT / "results.json").write_text(json.dumps(res, indent=2, default=_js))
    if pooled_p:
        allp = np.concatenate(pooled_p)
        res["pooled_bh_rejections_q05"] = bh_count(allp)
    (OUT / "results.json").write_text(json.dumps(res, indent=2, default=_js))
    return res


def _js(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)


def load_sims(bases, costs, data) -> dict:
    sims = {}
    for b in bases:
        rows = base_rows(data, b)
        for c in costs:
            p = CACHE / f"acc_{b}_{c}.npz"
            z = np.load(p)
            sims[(b, c)] = {"years": z["years"], "M": z["M"],
                            "acc": {k: z[f"acc_{k}"] for k in ACC_KEYS}, "glob": {k: z[f"glob_{k}"] for k in GLOB_KEYS},
                            "f": rows.rf + MARGIN_SPREAD / TRADING_DAYS, "oneq": rows.oneq, "sessions": rows.sessions}
    return sims


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--chunk", type=int, default=5120)
    ap.add_argument("--bases", default=",".join(BASES))
    ap.add_argument("--costs", default=",".join(COSTS))
    ap.add_argument("--smoke", action="store_true", help="tiny data / grid subset for timing (results not used)")
    ap.add_argument("--analyse-only", action="store_true", help="reuse the cached accumulators")
    a = ap.parse_args(argv)
    bases = a.bases.split(",")
    costs = a.costs.split(",")
    t0 = time.time()
    data = load_all(smoke=a.smoke)
    OUT.mkdir(parents=True, exist_ok=True)
    if not a.smoke:
        pd.DataFrame(data["pc_rows"]).to_csv(OUT / "data_checks_u18.csv", index=False, float_format="%.6g")
        pd.DataFrame(data["t10_checks"]).to_csv(OUT / "data_checks_t10.csv", index=False)
        sp = pd.DataFrame(data["t10_spells"], columns=["slot", "security_id", "first_i", "last_i"])
        sp["first"] = data["t10_master"][sp["first_i"]].strftime("%Y-%m-%d")
        sp["last"] = data["t10_master"][sp["last_i"]].strftime("%Y-%m-%d")
        sp.drop(columns=["first_i", "last_i"]).to_csv(OUT / "t10_spells.csv", index=False)
    print(f"data loaded in {time.time() - t0:.0f}s", flush=True)
    if a.smoke:
        n = 2048
        rows = base_rows(data, "U18")
        codes = cfg_arrays(grid().sample(n, random_state=0))
        t1 = time.time()
        simulate_grid(rows, codes)
        print(f"smoke: U18 {rows.c.shape} x {n} configs: {time.time() - t1:.1f}s")
        return
    if a.analyse_only:
        sims = load_sims(bases, costs, data)
    else:
        sims = run_grid_all(data, bases, costs, a.workers, a.chunk, N_GRID)
    print(f"grid done at {time.time() - t0:.0f}s", flush=True)
    analyse(data, sims, bases, costs, data["rf"])
    print(f"all done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
