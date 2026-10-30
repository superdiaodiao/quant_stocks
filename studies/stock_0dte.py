"""Single-stock 0DTE option grid: analysis of the QuantConnect exports (pre-registered in
docs/research_ledger_stock_0dte.md, 0.2-0.11).

Inputs (read only):
- output/research_only/stock_0dte/raw/stock_0dte_<first>_<last>.json.gz: the chart stream of qc/stock_0dte/main.py, one
  file per registered chunk, saved by qc/stock_0dte/fetch.js as {"runs": [{"vals", "bad", "meta"}]} with meta =
  {"bt", "book_line", "mech_line", "hashes": {"main", "sim", "simx", "optmath"}, "rs": the runtime statistics};
  decoded by quant.strategies.stock_0dte;
- SEC EDGAR submissions and filing headers of the runs' CIKs and their predecessors (8-K Item 2.02; the earnings
  pipeline's header cache read-only, the rest cached by quant.data.sources.sec_cache);
- Yahoo daily charts of every exported ticker (dividends; cached by quant.data.sources.sec_cache.yahoo_chart);
- Yahoo daily charts in research_cache/sector_lev/raw (ONEQ, QQQ: benchmark returns and ONEQ's 200-day average);
- FRED DTB3 (cash, the BIL stand-in); the risk ladder's monthly returns (QQQ + 10% bitcoin).
Outputs: output/research_only/stock_0dte/ (statistics only, no vendor price levels).

Steps (ledger section): name-day filters, pools and earnings days (0.2, 0.4); per-position conventions and the 1-lot
fees (0.5); the ALL program's pool-average series and the K10 program's $10k book (0.4, 0.5); grid statistics and A
(0.6.1-0.6.3); the yearly walk-forward (0.6.4); two-fold, PBO, bootstrap max-t / Romano-Wolf, deflated Sharpe, the
minimum detectable effect and the power simulation (0.6.5); the named comparators with the per-name, third-Friday and
2026 Monday / Wednesday reports (0.6.6); the fill report (0.6.7); the $10k integer replay (0.6.8); the equal-risk
overlay (0.6.9); the scorecard (0.6.10); the GO table (0.10). Readings the ledger leaves open are pinned in 0.6.11 and
coded here.

Usage:  PYTHONPATH=. .venv/bin/python -m studies.stock_0dte [--quick] [--allow-partial] [--files ...]
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from quant.data.rates import load_dtb3
from quant.data.sources.yahoo import parse_ohlc
from quant.evaluation.criteria import deflated_sharpe_n
from quant.evaluation.scorecard import scorecard
from quant.evaluation.stats import stationary_bootstrap_index
from quant.paths import CACHE_ROOT, ROOT, output_dir
from quant.reporting import write_json
from quant.strategies import stock_0dte as Q
from studies.zero_dte import alpha_t, ann, bootstrap_family, ir_of, leader_clusters, mdd, nw_t, nw_var, row_hash, smoothed

OUT = output_dir("stock_0dte")
RAW = OUT / "raw"
YAHOO = CACHE_ROOT / "sector_lev" / "raw"
LADDER = output_dir("risk_ladder") / "monthly_returns_full.csv"
START, END = "2012-02-03", "2026-07-29"
ERAS = {"E1": ("2012-02-03", "2019-12-31"), "E2": ("2020-01-01", "2022-12-31"), "E3": ("2023-01-01", END)}
OOS0 = "2016-01-01"
POOLS = (10, 20)
FILTERS = ("ALL", "EARN", "NOEARN", "GAP", "MOM", "RICH", "UP", "V20")
F_ON = ("ALL", "EARN", "NOEARN", "UP", "V20")              # the overnight arm cannot see the expiry day's open
F_ORB = ("ALL", "EARN", "NOEARN", "GAP", "UP", "V20")      # the breakout may enter before 09:45
MGMT = Q.MGMT
FROZEN = {"main": "99f83eb10301fc9f", "sim": "6801359bd95232b7", "simx": "a986b874edb3e9b3",
          "optmath": "2dd5ecc677960596"}     # the engine at commit 06959a8 (main.py before its BOOK / MECH lines)
CHUNKS = [("2012-02-01", "2013-02-28"), ("2013-03-01", "2014-03-31"), ("2014-04-01", "2015-04-30"),
          ("2015-05-01", "2016-05-31"), ("2016-06-01", "2017-05-31"), ("2017-06-01", "2018-05-31"),
          ("2018-06-01", "2019-05-31"), ("2019-06-01", "2020-05-31"), ("2020-06-01", "2021-04-30"),
          ("2021-05-01", "2022-03-31"), ("2022-04-01", "2023-02-28"), ("2023-03-01", "2024-01-31"),
          ("2024-02-01", "2024-12-31"), ("2025-01-01", "2025-11-30"), ("2025-12-01", "2026-04-30"),
          ("2026-05-01", "2026-07-29")]       # registered book chunks (ledger 0.7)
N_TRIALS = 29_162
N_CUM = 69_900                              # the project's cumulative option trials (ledger 0.6.3)
T_OOS = 2.24
SLEEVE_SD = 0.005
CAPS = {0: 0.05, 6: 0.05, 2: 0.02, 5: 0.025}   # sleeve scale caps of unit-return series by kind (ledger 0.5)
K10_LOSS = 300.0                            # $ max loss (or debit) per session in the K10 book
NAV0 = 10_000.0
STUCK_COST = 0.25                           # 'no free exit': dollars per share per stuck leg
EXDIV_MIN = 0.003
STOCK_COST = 0.0011                         # unwinding delivered shares at the next open
NW_LAGS = 5
SEED = 20261030
LAD, ON = Q.NE, Q.NE + 1                    # entry levels: 0..4 the times, 5 LAD, 6 ON (panel block Q.NE)
FAM = Q.FAM
REPORT_ONLY = {"SP", "SC", "SG", "SD", "RR", "RRB", "SDH"}
ORB_SHAPE = Q.SHAPES.index((FAM.index("ORB"), 0.50, 0.0))
SELECTABLE = [s for s in range(Q.NS) if FAM[Q.SHAPES[s][0]] not in REPORT_ONLY and s != ORB_SHAPE]
K10_FAMS = {"PCS", "CCS", "IC", "IF", "IFP", "IFM", "PRF", "LC", "LP", "LG", "LD", "DVC", "DVP", "MPD", "ORB"}
BULL, BEAR = {"PCS", "LC", "DVC"}, {"CCS", "LP", "DVP"}
DIRECTED = {"MPD", "IFP", "IFM"}            # direction exported per position (ORB has its own)
BUYERS = {"LC", "LP", "LG", "LD", "DVC", "DVP", "MPD", "ORB"}
LEGQ = {"PCS": (1, 1), "CCS": (1, 1), "IC": (1, 1, 1, 1), "IF": (1, 1, 1, 1), "SP": (1,), "SC": (1,), "SG": (1, 1),
        "SD": (1, 1), "LD": (1, 1), "RR": (1, 1), "RRB": (1, 1), "SDH": (1, 1), "LC": (1,), "LP": (1,), "LG": (1, 1),
        "DVC": (1, 1), "DVP": (1, 1), "IFP": (1, 1, 1, 1), "IFM": (1, 1, 1, 1), "MPD": (1, 1), "ORB": (1,),
        "PRF": (1, 3, 2)}                   # contracts per leg (series) of one position
ENTRY_MIN = [Q.SIM.mindex(hh, mm) for hh, mm in Q.ENTRY] + [-1]   # book order: the overnight arm first
# a company that changed its SEC filer reports its earlier releases under the predecessor's CIK (ledger 0.6.11)
PRED_CIKS = {"1652044": ("1288776",),               # Alphabet <- Google Inc. (2015-10)
             "1730168": ("1649338", "1441634"),     # Broadcom Inc. <- Broadcom Ltd. (2018-04) <- Avago (2016-02)
             "1744489": ("1001039",)}               # The Walt Disney Company <- TWDC Enterprises 18 (2019-03)


def bonferroni_t(n: int) -> float:
    """Two-sided 5% Bonferroni threshold for n trials (ledger 0.4)."""
    return NormalDist().inv_cdf(1 - 0.025 / n)


T_GRID, T_CUM = bonferroni_t(N_TRIALS), bonferroni_t(N_CUM)


# ---------------------------------------------------------------------------------------------------- structures
def composites() -> list[dict]:
    """Per-side-stop structures from the single-side books at M2 (ledger 0.4): IC-BE, IF-BE selectable; SG-BE, SD-BE
    report-only."""
    idx = {sh: i for i, sh in enumerate(Q.SHAPES)}
    out = []
    for d in Q.SIM.DELTAS:
        for w in Q.SIM.WINGS:
            p, c = idx[(0, d, w)], idx[(1, d, w)]
            name = (f"IC-BE{d:.2f}" if d < 0.5 else "IF-BE") + ("-W1" if w == 0 else f"-W{w * 100:g}%")
            out.append({"name": name, "put": p, "call": c, "kind": 5, "fam": "IC-BE" if d < 0.5 else "IF-BE",
                        "d": d, "w": w, "selectable": True})
    for d in (0.10, 0.16, 0.25, 0.50):
        p, c = idx[(4, d, 0.0)], idx[(5, d, 0.0)]
        out.append({"name": f"SG-BE{d:.2f}" if d < 0.5 else "SD-BE", "put": p, "call": c, "kind": 1,
                    "fam": "SG-BE" if d < 0.5 else "SD-BE", "d": d, "w": 0.0, "selectable": False})
    return out


COMP = composites()
COMP_SEL = [Q.NS + j for j, c in enumerate(COMP) if c["selectable"]]


def base_name(b: int) -> str:
    return Q.shape_name(b) if b < Q.NS else COMP[b - Q.NS]["name"]


def base_fam(b: int) -> str:
    return FAM[Q.SHAPES[b][0]] if b < Q.NS else COMP[b - Q.NS]["fam"]


def base_dw(b: int) -> tuple[float, float]:
    return (Q.SHAPES[b][1], Q.SHAPES[b][2]) if b < Q.NS else (COMP[b - Q.NS]["d"], COMP[b - Q.NS]["w"])


def base_kind(b: int) -> int:
    return Q.KIND[Q.SHAPES[b][0]] if b < Q.NS else COMP[b - Q.NS]["kind"]


def shape(fam: str, d: float = 0.5, w: float = 0.0) -> int:
    return Q.SHAPES.index((FAM.index(fam), d, w))


def entry_name(b: int, e: int) -> str:
    return "ORB" if b == ORB_SHAPE else ("LAD" if e == LAD else ("ON" if e == ON else "%02d:%02d" % Q.ENTRY[e]))


class Bases:
    """Per-position arrays of each base structure b (the 81 shapes, then the composites; ledger 0.4, 0.5), computed
    on demand over (block, slot, name-day):

    conv     P&L conventions in dollars per share [NB, 4, NK] (0 where not opened): primary (F = 0.25 fills),
             touch (entry and take-profits at the bid / ask), cons (touch + 0.30 per contract per fill), free
             (primary less 0.25 per stuck leg in the slots that reach the 15:50 flatten, the charge limited by the
             structure's range in primary terms, plus the conservative adjustment: never above cons); report-only
             settlements: hold (held to the close at intrinsic), flat15 (slot H only: closed at 15:00 when it would
             deliver shares; M slots = primary), asn (hold; shares delivered at the close unwound at the next open
             less 0.11%, plus the next session's ex-dividend)
    den      unit-return denominator per share [NB, NK]
    ml       K10 max loss per contract in dollars [NB, NK] (NaN where undefined); k10: the position may enter a book
    sur      the 1-lot $1-minimum surcharge per share [NB, 4, NK], one minimum per order (combo reading)
    sur_leg  the same with one minimum per leg (the stricter reading, check (d))
    fees     engine fees of the entry and the slot's closing per share [NB, 4, NK]; prem: gross premium per share
    x        signed fill-to-close stock return [NB, NK] (the alpha regressor)
    nsh      shares per contract-share left by the 15:50 flatten [NB, 4, NK] (slots that reach it)
    dt       day trades used [NB, 4, NK]: contract series closed the same day (the overnight block uses none)
    nser     contract series of the structure (the day trades it may use, counted before entry)
    """

    def __init__(self, P: Q.Panel, div_next: np.ndarray | None = None):
        self.P = P
        self.cache: dict[int, dict] = {}
        nm = P.names
        sc = nm["S_close"].to_numpy(float)
        gap = nm["next_open"].to_numpy(float) - sc
        ok = np.isfinite(gap) & (sc > 0) & (nm["split_next"].to_numpy() == 0)
        self.gap = np.where(ok, gap, 0.0)                               # next open - close (0 when unknown or split)
        self.cost = np.where(ok, STOCK_COST * sc, 0.0)
        # the dividend of a next session that is the ex-date: delivered shares held to that open earn (pay) it
        self.div = np.where(ok, np.nan_to_num(div_next) if div_next is not None else 0.0, 0.0)

    def get(self, b: int) -> dict:
        if b not in self.cache:
            if len(self.cache) >= 6:
                self.cache.pop(next(iter(self.cache)))
            self.cache[b] = self._shape(b) if b < Q.NS else self._comp(b)
        return self.cache[b]

    @staticmethod
    def bounds(f: int, kind: int, width, net, den, fee_x):
        """'No free exit' range of the P&L per share (ledger 0.5): defined-risk credit [-(width - net) - exit fees,
        net]; debit verticals [net - exit fees, width + net]; other long premium >= net - exit fees; the ratio fly
        >= -den - exit fees; naked structures unbounded."""
        w, n = width[:, None, :], net[:, None, :]
        inf = np.full(fee_x.shape, np.inf)
        if kind == 0:
            return -(w - n) - fee_x, n + 0 * fee_x
        if f in Q.SIM.VERT_DEBIT:
            return n - fee_x, w + n + 0 * fee_x
        if kind == 2:
            return n - fee_x, inf
        if kind == 6:
            return -den[:, None, :] - fee_x, inf
        return -inf, inf

    def _shape(self, b: int) -> dict:
        P = self.P
        f = Q.SHAPES[b][0]
        fam, kind = FAM[f], Q.KIND[f]
        o = P.opened[b]
        o4 = o[:, None, :]
        width, net = np.nan_to_num(P.width[b]), np.nan_to_num(P.net[b])
        den = np.where(o, P.den[b], np.nan)
        reached = np.concatenate([np.ones_like(o4), P.xmin[b] == 0], axis=1)
        if fam == "ORB":                                  # slot H is closed by the breakout's own order, if any
            reached[:, 0] = P.xmin[b][:, 0] == 0
        if kind == 0:
            ml = (width - net) * 100.0
        elif kind == 6:
            ml = np.nan_to_num(den) * 100.0
        elif kind == 2:
            ml = -net * 100.0
        else:
            ml = np.full(o.shape, np.nan)
        ml = np.where(o, ml, np.nan)
        k10 = o & (fam in K10_FAMS) & (np.nan_to_num(ml, nan=np.inf) <= K10_LOSS + 1e-9)
        pnl = P.pnl[b]
        cons = pnl + P.cons[b]
        stuck = P.stuck[b][:, None, :] * reached
        lo, hi = self.bounds(f, kind, width, net, np.nan_to_num(den), P.fee_exit[b])
        nsh_h = P.nsh_hold[b][:, None, :]
        f15 = pnl.copy()
        f15[:, 0] = pnl[:, 0] + P.d15[b]                  # FLAT15 is registered for the H slot only (ledger 0.3)
        # 'no free exit': the stuck-leg cost, limited by the structure's range in primary terms (a loss already
        # beyond the range is not lifted), then the conservative adjustment: never above the conservative series
        conv = {"primary": pnl, "touch": pnl + P.touch[b], "cons": cons,
                "free": np.minimum(np.clip(pnl - STUCK_COST * stuck, lo, hi), pnl) + P.cons[b],
                "hold": P.hold[b], "flat15": f15,
                "asn": P.hold[b] + reached * (nsh_h * (self.gap + self.div) - np.abs(nsh_h) * self.cost)}
        conv = {k: np.where(o4, v, 0.0) for k, v in conv.items()}
        q = np.array(LEGQ[fam], float)
        n_en = P.n_legs[b]
        comm_e = P.fee_entry[b] * 100.0 - Q.SIM.OTHER_FEE * n_en        # IBKR tier of the entry order, $ per position
        sur_e = np.maximum(0.0, 1.0 - comm_e)
        sur_e_leg = np.maximum(0.0, 1.0 - (comm_e / np.maximum(n_en, 1))[..., None] * q).sum(axis=-1)
        nx, fx = P.n_exit[b], P.fee_exit[b]
        comm_x = fx * 100.0 - Q.SIM.OTHER_FEE * nx
        nl = np.minimum(len(q), np.maximum(nx, 1))                      # legs in a partial closing (approximation)
        sur_x = np.where(reached, P.flat_sur[b][:, None, :] * 100.0, np.where(nx > 0, np.maximum(0.0, 1.0 - comm_x), 0.0))
        leg_full = np.maximum(0.0, 1.0 - (comm_x / np.maximum(nx, 1))[..., None] * q).sum(axis=-1)
        sur_x_leg = np.where(nx > 0, np.where(nx == n_en[:, None, :], leg_full, nl * np.maximum(0.0, 1.0 - comm_x / nl)),
                             0.0)                                       # every contract closed: the legs' own counts
        x = P.xpos[b].copy()
        if fam in DIRECTED:
            x = np.where(P.direction[b] == 2, -x, x)
        if fam == "ORB":
            x[0] = np.where(P.orb_dir == 2, -x[0], x[0])
        dt = np.where(o4, np.minimum(len(q), nx), 0)
        dt[Q.NE] = 0
        return {"opened": o, "k10": k10, "ml": ml, "den": den, "conv": conv, "kind": kind, "fam": fam,
                "sur": np.where(o4, (sur_e[:, None, :] + sur_x) / 100.0, 0.0),
                "sur_leg": np.where(o4, (sur_e_leg[:, None, :] + sur_x_leg) / 100.0, 0.0),
                "fees": np.where(o4, P.fee_entry[b][:, None, :] + fx, 0.0),
                "prem": np.where(o, np.abs(net + P.fee_entry[b]), 0.0), "x": np.where(o, x, 0.0),
                "nsh": np.where(o4 & reached, P.nsh_flat[b][:, None, :], 0), "dt": dt, "nser": len(q)}

    def _comp(self, b: int) -> dict:
        c = COMP[b - Q.NS]
        P = self.P
        A, B_ = self.get(c["put"]), self.get(c["call"])
        o = A["opened"] & B_["opened"]
        o4 = o[:, None, :]
        rep = lambda v: np.repeat(v[:, None, :], 4, axis=1)            # the composite lives in slot M2; every slot sees it
        if c["kind"] == 5:
            wmax = np.fmax(P.width[c["put"]], P.width[c["call"]])
            den = np.maximum(wmax - (P.net[c["put"]] + P.net[c["call"]]), Q.DEN_FLOOR * wmax)
            ml = A["ml"] + B_["ml"]                                     # both sides can be stopped
        else:
            den = P.s0.astype(float)
            ml = np.full(o.shape, np.nan)
        ml = np.where(o, ml, np.nan)
        out = {"opened": o, "den": np.where(o, den, np.nan), "ml": ml,
               "k10": o & c["selectable"] & (np.nan_to_num(ml, nan=np.inf) <= K10_LOSS + 1e-9),
               "conv": {k: np.where(o4, rep(A["conv"][k][:, 2] + B_["conv"][k][:, 2]), 0.0) for k in A["conv"]},
               "x": np.where(o, 0.5 * (A["x"] + B_["x"]), 0.0), "prem": np.where(o, A["prem"] + B_["prem"], 0.0),
               "kind": c["kind"], "fam": c["fam"], "nser": A["nser"] + B_["nser"]}
        for k in ("sur", "sur_leg", "fees", "nsh", "dt"):
            out[k] = np.where(o4, rep(A[k][:, 2] + B_[k][:, 2]), 0)
        return out


# ---------------------------------------------------------------------------------------------------- data
def yahoo_frame(name: str) -> pd.DataFrame:
    df = parse_ohlc(YAHOO / f"chart_{name}.json", END).set_index("date")
    df.index = pd.DatetimeIndex(df.index)
    return df


def calendar() -> pd.DatetimeIndex:
    """Exchange sessions (QQQ's daily chart) from 2011 through END."""
    d = yahoo_frame("QQQ").index
    return d[d >= "2011-01-01"]


def week_ends(days: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """The last session of each calendar week: a Friday, or the Thursday before a Friday holiday (ledger 0.5)."""
    s = pd.Series(days, index=days)
    iso = days.isocalendar()
    we = pd.DatetimeIndex(s.groupby([iso.year.to_numpy(), iso.week.to_numpy()]).max().sort_values().to_numpy())
    return we[we.weekday >= 3]          # a calendar cut mid-week (END is Wednesday 2026-07-29) has no week end there


def third_friday(d: pd.DatetimeIndex) -> np.ndarray:
    """Sessions in the week of the month's third Friday (the monthly expiry)."""
    fri = d + pd.to_timedelta(4 - d.weekday, unit="D")
    return np.asarray((fri.day >= 15) & (fri.day <= 21))


def benchmarks(days: pd.DatetimeIndex) -> pd.DataFrame:
    """Benchmark day returns, cash and ONEQ's 200-day trend state at the previous close; raises on a missing day."""
    out = pd.DataFrame(index=days)
    for t in ("ONEQ", "QQQ"):
        out[t.lower()] = yahoo_frame(t)["adjclose"].pct_change().reindex(days)
    if out[["oneq", "qqq"]].isna().any().any():
        raise ValueError(f"missing benchmark days: {out.index[out[['oneq', 'qqq']].isna().any(axis=1)][:5].tolist()}")
    rf = load_dtb3(END)
    rf.index = pd.DatetimeIndex(rf.index)
    out["rf"] = (rf.reindex(days, method="ffill") / 252.0).fillna(0.0)
    one = yahoo_frame("ONEQ")
    out["oneq_close"] = one["close"].reindex(days)                      # its traded price level (the $10k replay)
    o = one["adjclose"]
    up = (o > o.rolling(200).mean()).astype(bool)                       # state at each close
    out["up_prev"] = up.shift(1).reindex(days).fillna(False).astype(bool)    # known before day d's open
    out["up_prev2"] = up.shift(2).reindex(days).fillna(False).astype(bool)   # before the previous session's open
    return out


def ticker_ciks(metas: list[dict]) -> dict[str, set[str]]:
    """Per exported ticker, every SEC CIK the runs' name dictionaries gave it plus their predecessors (PRED_CIKS)."""
    out: dict[str, set[str]] = {}
    for m in metas:
        for t, c in Q.name_ciks(m).items():
            if c:
                c = str(int(c))
                out.setdefault(t, set()).update((c,) + PRED_CIKS.get(c, ()))
    return out


def earnings_table(ciks, days: pd.DatetimeIndex) -> pd.DataFrame:
    """SEC 8-K Item 2.02 releases of the given CIKs with New York times (filing headers) and reaction days (ledger
    0.2): accepted before 16:00 ET on a session -> that session, else the next session. An 8-K/A within 30 days
    after the CIK's previous release corrects it and is not a new release."""
    rel = Q.earnings_releases(ciks)
    cols = ["cik", "form", "t_et", "t_source", "d0"]
    if rel.empty:
        return pd.DataFrame(columns=cols)
    rel = rel.sort_values(["cik", "t_et"]).reset_index(drop=True)
    keep, last = [], {}
    for i, (cik, form, t) in enumerate(zip(rel["cik"], rel["form"], rel["t_et"])):
        if form == "8-K/A" and cik in last and t - last[cik] <= pd.Timedelta(days=30):
            continue
        keep.append(i)
        last[cik] = t
    rel = rel.loc[keep].reset_index(drop=True)
    s = pd.DatetimeIndex(days).sort_values()
    day = rel["t_et"].dt.normalize()
    late = ((rel["t_et"].dt.hour >= 16) | ~day.isin(s)).to_numpy()
    pos = np.where(late, s.searchsorted(day, side="right"), s.searchsorted(day, side="left"))
    ok = pos < len(s)
    rel = rel[ok].copy()
    rel["d0"] = s[pos[ok]]
    return rel[cols].reset_index(drop=True)


def earnings_flags(P: Q.Panel, rel: pd.DataFrame, ciks: dict, days: pd.DatetimeIndex) -> dict:
    """Per name-day (ledger 0.4), over every CIK of the ticker (ticker_ciks): ``known`` (the company files 8-K
    Item 2.02 releases: one within 200 days either side), ``react`` (the day is a reaction day), ``earn`` [NB, NK]:
    for a same-day entry, released before the entry minute and the day is the reaction day; for the overnight block,
    released after the previous session's 15:46 fill and before the expiry day's flatten (15:50, 12:50 on a half
    day); ``earn_orb``: the same-day rule at the breakout's own decision bar."""
    nm = P.names
    nk = len(nm)
    known, react, earn_orb = np.zeros(nk, bool), np.zeros(nk, bool), np.zeros(nk, bool)
    earn = np.zeros((Q.NB, nk), bool)
    by = {str(c): (g["t_et"].to_numpy(dtype="datetime64[ns]"), g["d0"].to_numpy(dtype="datetime64[ns]"))
          for c, g in rel.groupby("cik")} if len(rel) else {}
    prev = pd.Series(days[:-1], index=days[1:])
    dates = pd.DatetimeIndex(nm["date"])
    half = nm["half"].to_numpy()
    for k, (t, d) in enumerate(zip(nm["name"], dates)):
        cs = ciks.get(t) or ()
        got = [by[str(int(c))] for c in ((cs,) if isinstance(cs, str) else cs) if str(int(c)) in by]
        if not got:
            continue
        ts, d0 = np.concatenate([g[0] for g in got]), np.concatenate([g[1] for g in got])
        dn = np.datetime64(d, "ns")
        known[k] = bool((np.abs(ts - dn) <= np.timedelta64(200, "D")).any())
        if not known[k]:
            continue
        on_d = d0 == dn
        react[k] = bool(on_d.any())
        if react[k]:
            first = ts[on_d].min()
            for e, mS in enumerate(ENTRY_MIN[:Q.NE]):
                earn[e, k] = first < dn + np.timedelta64(9 * 60 + 30 + mS + 1, "m")
            if P.orb_min[k] > 0:                          # orb_min = the decision bar + 2 (the fill minute + 1)
                earn_orb[k] = first < dn + np.timedelta64(9 * 60 + 30 + int(P.orb_min[k]) - 1, "m")
        p = prev.get(d)
        if p is not None:
            lo = np.datetime64(p, "ns") + np.timedelta64(15 * 60 + 46, "m")
            hi = dn + np.timedelta64((12 if half[k] else 15) * 60 + 50, "m")
            earn[Q.NE, k] = bool(((ts > lo) & (ts <= hi)).any())
    return {"earn": earn, "react": react, "known": known, "earn_orb": earn_orb}


def yahoo_daily(tickers) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """From Yahoo's daily chart of every exported ticker (cached by quant.data.sources.sec_cache.yahoo_chart): the
    cash dividends by ex-date, scaled back by later splits to the day's own share count (to compare with the raw
    stock price), and the daily total returns of the adjusted close through END (one column per ticker).
    ``attrs['missing']`` lists the tickers Yahoo has no chart for (their dividends and returns are unknown). The
    third value holds, per ticker and day, the next session's open over this close (``gap``; split-adjusted, the
    same shares) and the open of the session after that over this close (``gap2``)."""
    from quant.data.sources.sec_cache import yahoo_chart
    def ny(ts):                                       # unix seconds (one or many) -> New York dates
        d = pd.DatetimeIndex(pd.to_datetime(np.atleast_1d(np.asarray(ts, dtype="int64")), unit="s", utc=True))
        d = d.tz_convert("America/New_York").tz_localize(None).normalize()
        return d if np.ndim(ts) else d[0]
    rows, rets, gaps, gaps2, missing = [], {}, {}, {}, []
    for t in sorted(tickers):
        ch = yahoo_chart(t.replace(".", "-"))
        if not ch or not ch.get("timestamp"):
            missing.append(t)
            continue
        adj = ((ch.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
        if len(adj) == len(ch["timestamp"]):
            px = pd.Series(adj, index=ny(ch["timestamp"]), dtype=float).groupby(level=0).last().dropna()
            rets[t] = px[px.index <= END].pct_change()
            q = ((ch.get("indicators") or {}).get("quote") or [{}])[0]
            if len(q.get("open") or []) == len(ch["timestamp"]):
                oc = pd.DataFrame({"o": q["open"], "c": q["close"]}, index=ny(ch["timestamp"]), dtype=float)
                oc = oc.groupby(level=0).last().dropna()
                gaps[t] = oc["o"].shift(-1) / oc["c"]
                gaps2[t] = oc["o"].shift(-2) / oc["c"]
        ev = ch.get("events") or {}
        spl = [(ny(v["date"]), float(v["numerator"]) / float(v["denominator"])) for v in (ev.get("splits") or {}).values()]
        for v in (ev.get("dividends") or {}).values():
            d = ny(v["date"])
            rows.append((t, d, float(v["amount"]) * math.prod(r for sd, r in spl if sd > d)))
    divs = pd.DataFrame(rows, columns=["ticker", "ex_date", "cash"])
    divs.attrs["missing"] = missing
    return divs, pd.DataFrame(rets), {"gap": pd.DataFrame(gaps), "gap2": pd.DataFrame(gaps2)}


def fix_next_opens(names: pd.DataFrame, gap: pd.DataFrame | None, gap2: pd.DataFrame | None) -> tuple[pd.DataFrame, dict]:
    """The engine's next-open record is written at the first minute bar of the next day whose first data event
    comes at or after 09:31; on some days an earlier event (a daily bar) made it the open of the session after the
    next, and in 2026 such a Friday's record was lost when Monday's own expiry replaced it (ledger 0.7 run record).
    The next session's open is rebuilt as the day's close (official, else the last bar) times Yahoo's overnight gap
    for every name Yahoo covers; a name without Yahoo data keeps the engine's value on days where the names with
    data confirm it (at least 60% within 0.3% of the next session) and is unknown otherwise. A rebuilt record has no
    split flag (the gap is split-adjusted)."""
    nm = names.copy()
    if gap is None or gap.empty:
        return nm, {"rebuilt": 0}
    d = pd.DatetimeIndex(nm["date"])
    look = lambda tab: np.array([tab[t].get(x, np.nan) if t in tab else np.nan for t, x in zip(nm["name"], d)], float)
    sane = lambda g: np.where(np.isfinite(g) & (g > 0.3) & (g < 3.0), g, np.nan)      # a bad Yahoo open is no gap
    g1, g2 = sane(look(gap)), sane(look(gap2) if gap2 is not None else np.full(len(nm), np.nan))
    base = nm["close_official"].to_numpy(float)
    base = np.where(np.isfinite(base) & (base > 0), base, nm["S_close"].to_numpy(float))
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = nm["next_open"].to_numpy(float) / base
        e1, e2 = np.abs(ratio / g1 - 1), np.abs(ratio / g2 - 1)
    chk = pd.DataFrame({"d": d, "ok": e1 < 0.003, "late": e2 < 0.003, "has": np.isfinite(e1)})[lambda v: v["has"]]
    day_ok = chk.groupby("d")["ok"].mean() >= 0.6
    keep = pd.Series(d).map(day_ok).fillna(False).to_numpy(bool)
    rebuilt = np.isfinite(g1) & (base > 0)
    new = np.where(rebuilt, base * g1, np.where(keep, nm["next_open"].to_numpy(float), np.nan))
    nm["next_open"] = new
    nm["split_next"] = np.where(rebuilt, 0, np.where(keep, nm["split_next"].to_numpy(), -1))
    late_days = chk.groupby("d")["late"].mean() > 0.6
    return nm, {"rebuilt": int(rebuilt.sum()), "engine_kept": int((~rebuilt & keep).sum()),
                "unknown": int((~rebuilt & ~keep).sum()), "days_confirmed": int(day_ok.sum()),
                "days_late_by_one_session": int((late_days & ~day_ok.reindex(late_days.index).fillna(False)).sum()),
                "days_checked": int(len(day_ok))}


def dividend_on(names: pd.DataFrame, divs: pd.DataFrame | None, when: pd.DatetimeIndex) -> np.ndarray:
    """Per name-day: the name's cash dividend with ex-date ``when`` (0 when none or unknown)."""
    if divs is None or divs.empty:
        return np.zeros(len(names))
    cash = divs.groupby(["ticker", "ex_date"])["cash"].sum()
    return np.array([cash.get((t, d), 0.0) for t, d in zip(names["name"], when)], float)


def trailing_mean(names: pd.DataFrame, v: np.ndarray, n: int = 20) -> np.ndarray:
    """Per row of v [rows, NK]: the mean of the name's previous n finite values (by date), NaN with fewer."""
    out = np.full(v.shape, np.nan)
    key = names["name"].to_numpy()
    order = np.lexsort((names["date"].to_numpy(), key))
    ks = key[order]
    cut = np.r_[0, np.flatnonzero(ks[1:] != ks[:-1]) + 1, len(ks)]
    for a, b in zip(cut[:-1], cut[1:]):
        idx = order[a:b]
        for e in range(v.shape[0]):
            x = v[e, idx]
            fin = np.isfinite(x)
            cs = np.r_[0.0, np.cumsum(x[fin])]
            cnt = np.cumsum(fin) - fin                                  # finite values strictly before each row
            ok = cnt >= n
            r = np.full(len(x), np.nan)
            r[ok] = (cs[cnt[ok]] - cs[cnt[ok] - n]) / n
            out[e, idx] = r
    return out


def filter_masks(P: Q.Panel, bench: pd.DataFrame, ef: dict) -> dict:
    """Name-day masks [NB, NK] of the entry filters (ledger 0.4); MOM and RICH per side. GAP, MOM and RICH are not
    defined for the overnight block."""
    nm = P.names
    nk = len(nm)
    pos = lambda c: np.where(nm[c].to_numpy(float) > 0, nm[c].to_numpy(float), np.nan)
    sig = nm["sig"].to_numpy(float)
    prev, s_open, s_close = pos("prev_close"), pos("S_open"), pos("S_close")
    S = np.vstack([np.vstack([pos(f"S_e{e}") for e in range(Q.NE)]), np.full((1, nk), np.nan)])
    strad = np.vstack([np.vstack([nm[f"strad_e{e}"].to_numpy(float) for e in range(Q.NE)]), np.full((1, nk), np.nan)])
    with np.errstate(divide="ignore", invalid="ignore"):
        gap = np.abs(s_open / prev - 1)
        r = S / prev[None, :] - 1
        move = np.abs(s_close[None, :] / S - 1)
        rich = np.where(strad > 0, strad / S, np.nan)
    valid = np.isin(nm["status"].to_numpy(), (0, 3)) & (nm["half"].to_numpy() == 0)
    past = trailing_mean(nm, np.where(valid[None, :], move, np.nan), 20)
    d = pd.DatetimeIndex(nm["date"])
    up_same = bench["up_prev"].reindex(d).fillna(False).to_numpy(bool)
    up_on = bench["up_prev2"].reindex(d).fillna(False).to_numpy(bool)
    v_same = np.nan_to_num(nm["vix_prev"].to_numpy(float), nan=0.0) > 20
    v_on = np.nan_to_num(nm["vix_on"].to_numpy(float), nan=0.0) > 20
    half_sig = np.where(np.isfinite(sig), 0.5 * np.nan_to_num(sig), -1.0)
    m = {"ALL": np.ones((Q.NB, nk), bool),
         "EARN": ef["earn"] & ef["known"][None, :],
         "NOEARN": np.vstack([np.broadcast_to(ef["known"] & ~ef["react"], (Q.NE, nk)),
                              (ef["known"] & ~ef["earn"][Q.NE])[None, :]]),
         "GAP": np.broadcast_to(np.isfinite(gap) & (np.nan_to_num(gap, nan=9.0) <= half_sig), (Q.NB, nk)).copy(),
         "UP": np.vstack([np.broadcast_to(up_same, (Q.NE, nk)), up_on[None, :]]),
         "V20": np.vstack([np.broadcast_to(v_same, (Q.NE, nk)), v_on[None, :]])}
    ok = np.isfinite(past) & np.isfinite(rich)
    m["RICH_sell"] = ok & (np.nan_to_num(rich) >= np.nan_to_num(past))
    m["RICH_buy"] = ok & (np.nan_to_num(rich) < np.nan_to_num(past))
    m["MOM_bull"] = np.nan_to_num(r, nan=0.0) > 0
    m["MOM_bear"] = np.nan_to_num(r, nan=0.0) < 0
    m["MOM_neutral"] = np.isfinite(r) & (np.abs(np.nan_to_num(r)) <= half_sig[None, :])
    for k in ("GAP", "RICH_sell", "RICH_buy", "MOM_bull", "MOM_bear", "MOM_neutral"):
        m[k][Q.NE] = False
    m["EARN_ORB"] = np.zeros((Q.NB, nk), bool)
    m["EARN_ORB"][0] = ef["earn_orb"] & ef["known"]
    return m


def mask_for(masks: dict, f: str, b: int, P: Q.Panel) -> np.ndarray:
    """Filter mask [NB, NK] of filter f for base structure b (MOM and RICH by the structure's side)."""
    fam = base_fam(b)
    if f == "EARN" and b == ORB_SHAPE:
        return masks["EARN_ORB"]
    if f == "RICH":
        return masks["RICH_buy" if fam in BUYERS else "RICH_sell"]
    if f == "MOM":
        if fam in DIRECTED:                                        # by the exported direction of each position
            return np.where(P.direction[b] == 2, masks["MOM_bear"], masks["MOM_bull"])
        return masks["MOM_bull" if fam in BULL else ("MOM_bear" if fam in BEAR else "MOM_neutral")]
    return masks[f]


# ---------------------------------------------------------------------------------------------------- configurations
def config_list() -> pd.DataFrame:
    rows = []
    for pool in POOLS:
        for b in SELECTABLE:
            for e in range(Q.NE + 2):
                for mi in range(4):
                    for fi, f in enumerate(FILTERS):
                        if e != ON or f in F_ON:
                            rows.append((pool, b, e, mi, fi))
        for b in COMP_SEL:
            for e in range(Q.NE + 2):
                for fi, f in enumerate(FILTERS):
                    if e != ON or f in F_ON:
                        rows.append((pool, b, e, 2, fi))
        for fi, f in enumerate(FILTERS):
            if f in F_ORB:
                rows.append((pool, ORB_SHAPE, 0, 0, fi))
    cf = pd.DataFrame(rows, columns=["pool", "b", "e", "m", "f"])
    if len(cf) != N_TRIALS:
        raise ValueError(f"{len(cf)} configurations, registered {N_TRIALS}")
    cf["name"] = [f"P{p}|{base_name(b)}|{entry_name(b, e)}|{'BE' if b >= Q.NS else MGMT[m]}|{FILTERS[f]}"
                  for p, b, e, m, f in cf[["pool", "b", "e", "m", "f"]].to_numpy()]
    cf["fam"] = [base_fam(b) for b in cf["b"]]
    cf["kind"] = [base_kind(b) for b in cf["b"]]
    return cf


def cfg_keys(cf: pd.DataFrame) -> dict:
    return {k: cf[k].to_numpy() for k in ("pool", "b", "e", "m", "f", "kind")}


def book_take(s: np.ndarray, ml: np.ndarray) -> np.ndarray:
    """K10 book (ledger 0.4): positions in session, entry-minute, rank order, one contract each, taken while the
    session's summed max loss including this one stays <= $300 (the first that would exceed it ends the session)."""
    if not len(s):
        return np.zeros(0, bool)
    cs = np.cumsum(ml)
    first = np.r_[True, s[1:] != s[:-1]]
    base = (cs - ml)[first][np.cumsum(first) - 1]
    return cs - base <= K10_LOSS + 1e-9


BOOK_SERIES = {"cons": ("cons", "sur"), "primary": ("primary", "sur"), "leg": ("cons", "sur_leg"),
               "free": ("free", "sur")}


class Grid:
    """The two series of a configuration on a set of sessions: the ALL program's pool average of unit returns (equal
    weight over the pool's name-days that open the position, 0 when none; LAD averages its five blocks) and the K10
    book (dollars / $10k; LAD trades one entry a day, rotating by ISO week)."""

    def __init__(self, P: Q.Panel, sess: pd.DatetimeIndex, masks: dict):
        self.P, self.masks, self.sess = P, masks, sess
        nm = P.names
        d = pd.DatetimeIndex(nm["date"])
        si = pd.Series(np.arange(len(sess)), index=sess).reindex(d).to_numpy()
        self.valid = np.isfinite(si)
        self.si = np.where(self.valid, si, 0).astype(int)
        self.ns = len(sess)
        self.rank, self.rank_on = nm["rank"].to_numpy(), nm["rank_on"].to_numpy()
        self.lad = d.isocalendar().week.to_numpy().astype(int) % Q.NE

    def inpool(self, pool: int, blk: int) -> np.ndarray:
        r = self.rank_on if blk == Q.NE else self.rank
        return self.valid & (r >= 1) & (r <= pool)

    @staticmethod
    def blocks(b: int, e: int) -> list[int]:
        if b == ORB_SHAPE:
            return [0]
        if e == LAD:
            return list(range(Q.NE))
        return [Q.NE] if e == ON else [e]

    def run(self, cfg, base: dict, mk: np.ndarray | None = None, pool_avg: bool = True, book: bool = True,
            positions: bool = False, convs: dict | None = None) -> dict:
        pool, b, e, m, f = cfg
        convs = BOOK_SERIES if convs is None else convs
        if mk is None:
            mk = mask_for(self.masks, FILTERS[f], b, self.P)
        bl = self.blocks(b, e)
        ns = self.ns
        out = {}
        if pool_avg:
            rp, rc, rp1, rc1, x = np.zeros(ns), np.zeros(ns), np.zeros(ns), np.zeros(ns), np.zeros(ns)
            t, o = np.zeros(ns, bool), np.zeros(ns, bool)
            for blk in bl:
                op = base["opened"][blk] & self.inpool(pool, blk)
                w = op & mk[blk]
                s = self.si[w]
                cnt = np.maximum(np.bincount(s, minlength=ns), 1) * len(bl)
                den = base["den"][blk][w]
                rp += np.bincount(s, weights=base["conv"]["primary"][blk, m][w] / den, minlength=ns) / cnt
                rc += np.bincount(s, weights=base["conv"]["cons"][blk, m][w] / den, minlength=ns) / cnt
                sur = base["sur"][blk, m][w]
                rp1 += np.bincount(s, weights=(base["conv"]["primary"][blk, m][w] - sur) / den, minlength=ns) / cnt
                rc1 += np.bincount(s, weights=(base["conv"]["cons"][blk, m][w] - sur) / den, minlength=ns) / cnt
                x += np.bincount(s, weights=base["x"][blk][w], minlength=ns) / cnt
                t |= np.bincount(s, minlength=ns) > 0
                o |= np.bincount(self.si[op], minlength=ns) > 0
            out["pool"] = {"primary": rp, "cons": rc, "primary_1lot": rp1, "cons_1lot": rc1, "traded": t, "x": x,
                           "open": o}
        if book:
            parts = []
            ok_any = np.zeros(ns, bool)
            for blk in bl:
                kk = base["k10"][blk] & self.inpool(pool, blk)
                ok_any |= np.bincount(self.si[kk], minlength=ns) > 0
                inc = kk & mk[blk]
                if e == LAD:
                    inc &= self.lad == blk
                idx = np.flatnonzero(inc)
                if len(idx):
                    mn = self.P.orb_min[idx] if b == ORB_SHAPE else np.full(len(idx), ENTRY_MIN[blk])
                    rk = (self.rank_on if blk == Q.NE else self.rank)[idx]
                    parts.append((np.full(len(idx), blk), idx, self.si[idx], mn, rk))
            res = {k: np.zeros(ns) for k in convs}
            n, x = np.zeros(ns), np.zeros(ns)
            pos = (np.zeros(0, int),) * 3
            if parts:
                blk_a, idx, s, mn, rk = (np.concatenate(z) for z in zip(*parts))
                order = np.lexsort((rk, mn, s))
                blk_a, idx, s = blk_a[order], idx[order], s[order]
                take = book_take(s, base["ml"][blk_a, idx])
                blk_a, idx, s = blk_a[take], idx[take], s[take]
                for k, (c, sr) in convs.items():
                    v = 100.0 * (base["conv"][c][blk_a, m, idx] - (base[sr][blk_a, m, idx] if sr else 0.0))
                    res[k] = np.bincount(s, weights=v, minlength=ns) / NAV0
                n = np.bincount(s, minlength=ns).astype(float)
                x = np.bincount(s, weights=base["x"][blk_a, idx], minlength=ns) / np.maximum(n, 1)
                pos = (s, blk_a, idx)
            out["book"] = {**res, "traded": n > 0, "n": n, "x": x, "open": ok_any}
            if positions:
                out["positions"] = pos
        return out


def matrices(G: Grid, B: Bases, cf: pd.DataFrame) -> dict:
    """Series of every configuration [n_cfg, n_sessions]: all / all_cons / all_x / all_t / all_o (ALL program) and
    k10 (conservative + 1-lot fees, the decision series) / k10_primary / k10_leg / k10_free / k10_x / k10_t / k10_o."""
    n = len(cf)
    M = {k: np.zeros((n, G.ns), np.float32) for k in ("all", "all_cons", "all_x", "k10", "k10_primary", "k10_leg",
                                                       "k10_free", "k10_x")}
    M.update({k: np.zeros((n, G.ns), bool) for k in ("all_t", "all_o", "k10_t", "k10_o")})
    for b, grp in cf.groupby("b", sort=True):
        base = B.get(int(b))
        for i, cfg in zip(grp.index, grp[["pool", "b", "e", "m", "f"]].to_numpy()):
            r = G.run(tuple(int(v) for v in cfg), base)
            p, k = r["pool"], r["book"]
            M["all"][i], M["all_cons"][i], M["all_x"][i] = p["primary"], p["cons"], p["x"]
            M["all_t"][i], M["all_o"][i] = p["traded"], p["open"]
            M["k10"][i], M["k10_primary"][i], M["k10_leg"][i], M["k10_free"][i] = k["cons"], k["primary"], k["leg"], k["free"]
            M["k10_x"][i], M["k10_t"][i], M["k10_o"][i] = k["x"], k["traded"], k["open"]
    return M


# ---------------------------------------------------------------------------------------------------- statistics
def nw_t1(r: np.ndarray, lags: int = NW_LAGS) -> float:
    r = np.asarray(r, np.float64)
    return float(nw_t(r[None, :], lags)[0]) if len(r) > lags + 1 else 0.0


def alpha_years(R: np.ndarray, X: np.ndarray, yrs, lags: int = NW_LAGS) -> tuple[np.ndarray, np.ndarray]:
    """Per row of R [rows, T]: the intercept a of R = a + sum_y b_y 1{year y} X + u (one slope per calendar year,
    ledger 0.6.2) and its Newey-West (Bartlett, ``lags``) t. A year where X is all zero gets no slope."""
    R = np.atleast_2d(np.asarray(R, np.float64))
    X = np.atleast_2d(np.asarray(X, np.float64))
    nr, n = R.shape
    if n == 0:
        return np.zeros(nr), np.zeros(nr)
    _, yi = np.unique(np.asarray(yrs), return_inverse=True)
    H = np.zeros((n, yi.max() + 1))
    H[np.arange(n), yi] = 1.0
    a_out, t_out = np.zeros(nr), np.zeros(nr)
    with np.errstate(all="ignore"):
        for lo in range(0, nr, 2048):
            r, x = R[lo:lo + 2048], X[lo:lo + 2048]
            sx, sxx, sxr = x @ H, (x * x) @ H, (x * r) @ H
            inv = np.where(sxx > 1e-18, 1.0 / np.where(sxx > 1e-18, sxx, 1.0), 0.0)
            den = n - (sx * sx * inv).sum(axis=1)
            den = np.where(den > 1e-9, den, np.nan)
            a = (r.sum(axis=1) - (sxr * sx * inv).sum(axis=1)) / den
            u = r - a[:, None] - ((sxr - a[:, None] * sx) * inv)[:, yi] * x
            h = (1.0 - (sx * inv)[:, yi] * x) / den[:, None] * u          # (X'X)^-1 first row times the scores
            v = (h * h).sum(axis=1)
            for k in range(1, lags + 1):
                v += 2 * (1 - k / (lags + 1)) * (h[:, k:] * h[:, :-k]).sum(axis=1)
            ok = np.isfinite(a) & np.isfinite(v) & (v > 0)
            a_out[lo:lo + 2048] = np.where(np.isfinite(a), a, 0.0)
            t_out[lo:lo + 2048] = np.where(ok, a / np.sqrt(np.where(ok, v, 1.0)), 0.0)
    return a_out, t_out


def grid_stats(R, T, X, sess: pd.DatetimeIndex) -> pd.DataFrame:
    mu = R.mean(axis=1, dtype=np.float64)
    sd = R.std(axis=1, ddof=1, dtype=np.float64)
    a, at = alpha_years(R, X, sess.year)
    out = pd.DataFrame({"mean_bp": mu * 1e4, "ir": np.where(sd > 0, mu / np.where(sd > 0, sd, 1), 0.0), "t": nw_t(R),
                        "alpha_bp": a * 1e4, "alpha_t": at, "trades": T.sum(axis=1)})
    for era, (a0, a1) in {"E1": ERAS["E1"], "E23": (ERAS["E2"][0], END), "E3": ERAS["E3"]}.items():
        sl = (sess >= a0) & (sess <= a1)
        out[f"mean_{era}_bp"] = R[:, sl].mean(axis=1, dtype=np.float64) * 1e4 if sl.any() else 0.0
        out[f"trades_{era}"] = T[:, sl].sum(axis=1)
    out["t_E3"] = nw_t(R[:, (sess >= ERAS["E3"][0])])
    out["dup"] = pd.Series(row_hash(R)).duplicated().to_numpy()
    ev1, ev2 = out["trades_E1"] >= 100, out["trades_E23"] >= 100
    eras_ok = (~ev1 | (out["mean_E1_bp"] > 0)) & (~ev2 | (out["mean_E23_bp"] > 0))
    out["A"] = (out["t"] >= T_GRID) & (out["alpha_t"] >= T_GRID) & eras_ok
    out["A_cum"] = (out["t"] >= T_CUM) & (out["alpha_t"] >= T_CUM) & eras_ok
    return out


def neighbours(cf: pd.DataFrame) -> np.ndarray:
    """Each configuration and its neighbours [n_cfg, 7] (-1 padded): the adjacent delta in the family's own delta
    list, the adjacent wing, the adjacent entry time; same pool, family, management and filter (ledger 0.6.4)."""
    key = {tuple(v): i for i, v in enumerate(cf[["pool", "b", "e", "m", "f"]].to_numpy())}
    bs = sorted(set(cf["b"]))
    fam_of = {b: base_fam(b) for b in bs}
    dw = {b: base_dw(b) for b in bs}
    by_fd = {(fam_of[b], dw[b]): b for b in bs}
    fam_d, fam_w = {}, {}
    for b in bs:
        fam_d.setdefault(fam_of[b], set()).add(dw[b][0])
        fam_w.setdefault(fam_of[b], set()).add(dw[b][1])
    out = np.full((len(cf), 7), -1, int)
    for i, (p, b, e, m, f) in enumerate(cf[["pool", "b", "e", "m", "f"]].to_numpy()):
        d, w = dw[b]
        ds, ws = sorted(fam_d[fam_of[b]]), sorted(fam_w[fam_of[b]])
        nb = [i]
        for step in (-1, 1):
            j = ds.index(d) + step
            if 0 <= j < len(ds):
                b2 = by_fd.get((fam_of[b], (ds[j], w)))
                if b2 is not None and (p, b2, e, m, f) in key:
                    nb.append(key[(p, b2, e, m, f)])
            j = ws.index(w) + step
            if 0 <= j < len(ws):
                b2 = by_fd.get((fam_of[b], (d, ws[j])))
                if b2 is not None and (p, b2, e, m, f) in key:
                    nb.append(key[(p, b2, e, m, f)])
            if e < Q.NE and b != ORB_SHAPE and 0 <= e + step < Q.NE and (p, b, e + step, m, f) in key:
                nb.append(key[(p, b, e + step, m, f)])
        out[i, :len(nb)] = nb[:7]
    return out


def ranked(score: np.ndarray, elig: np.ndarray, keys: dict) -> np.ndarray:
    """Eligible configurations by descending score; ties (to 1e-12): ALL filter, then H, then P10, then the lowest
    index (ledger 0.6.4)."""
    s = np.where(elig & np.isfinite(score), score, -np.inf)
    cand = np.flatnonzero(np.isfinite(s))
    if not len(cand):
        return cand
    return cand[np.lexsort((cand, keys["pool"][cand] != POOLS[0], keys["m"][cand] != 0, keys["f"][cand] != 0,
                            -np.round(s[cand], 12)))]


def select(R, T, X, Of, keys, sess, nb, sel, recent, score_R=None) -> tuple[int, np.ndarray, np.ndarray]:
    """The registered selection on the sessions in sel (ledger 0.6.4): openable on >= 50% of sel and of recent;
    traded on >= max(100, 25% of sel); not a duplicate series; mean > 0; alpha t > 0 (checked in score order);
    score = the IR of R (or score_R) averaged with the neighbours'."""
    nz, nr = int(sel.sum()), int(recent.sum())
    feas = Of[:, sel].sum(axis=1) >= 0.5 * nz
    if nr:
        feas &= Of[:, recent].sum(axis=1) >= 0.5 * nr
    trades = T[:, sel].sum(axis=1) >= max(100, 0.25 * nz)
    Rs = R[:, sel]
    dup = pd.Series(row_hash(Rs)).duplicated().to_numpy()
    el = feas & trades & ~dup & (Rs.mean(axis=1, dtype=np.float64) > 0)
    sc = smoothed(ir_of(Rs if score_R is None else score_R[:, sel]), nb)
    yrs = sess.year[sel]
    for i in ranked(sc, el, keys):
        if alpha_years(R[i, sel], X[i, sel], yrs)[1][0] > 0:
            return int(i), sc, el
    return -1, sc, el


def scale_of(u_traded: np.ndarray, p_expected: float, cap: float) -> float:
    """Ex-ante sleeve scale (ledger 0.5): daily sd over all trading days = SLEEVE_SD, capped."""
    if len(u_traded) < 2 or p_expected <= 0:
        return 0.0
    sd = float(np.std(u_traded, ddof=1)) * math.sqrt(p_expected)
    return min(SLEEVE_SD / sd, cap) if sd > 0 else 0.0


def boot_p(r: np.ndarray, n_boot: int = 2000, block: float = 10.0) -> float:
    """One-sided stationary-bootstrap p-value of a positive mean (centred draws, mean block 10)."""
    r = np.asarray(r, np.float64)
    if len(r) < 20 or r.std() == 0:
        return 1.0
    rng = np.random.default_rng(SEED)
    z = r - r.mean()
    m = np.array([z[stationary_bootstrap_index(len(r), block, rng)].mean() for _ in range(n_boot)])
    return float((m >= r.mean()).mean())


def summary(r, x, sess, oos, e3) -> dict:
    r, x = np.asarray(r, np.float64), np.asarray(x, np.float64)
    a, at = alpha_years(r[oos], x[oos], sess.year[oos])
    _, _, at1 = alpha_t(r[oos][None, :], x[oos][None, :])
    return {"t": nw_t1(r[oos]), "mean_bp": float(r[oos].mean() * 1e4), "sd_bp": float(r[oos].std() * 1e4),
            "e3_mean_bp": float(r[e3].mean() * 1e4), "e3_t": nw_t1(r[e3]), "alpha_bp": float(a[0] * 1e4),
            "alpha_t": float(at[0]), "alpha_t_one_beta": float(at1[0]), "t_nw15": nw_t1(r[oos], 15)}


def walk_forward(R, T, X, Of, cf, keys, sess, nb, share, cap_mult, extra=None, score_R=None,
                 bench_sess=None) -> tuple[dict, dict]:
    """Yearly walk-forward (ledger 0.6.4) on the decision series R. extra: name -> matrix aligned with R whose
    out-of-sample series under the same picks and scales are reported."""
    extra = extra or {}
    ns = len(sess)
    sleeve, xs, scale, bsel = np.zeros(ns), np.zeros(ns), np.zeros(ns), np.zeros(ns)
    pick = np.full(ns, -1)
    other = {k: np.zeros(ns) for k in extra}
    picks = []
    for y in range(int(OOS0[:4]), int(END[:4]) + 1):
        sel = sess < pd.Timestamp(f"{y}-01-01")
        recent = sel & (sess >= pd.Timestamp(f"{y - 2}-01-01"))
        test = sess.year == y
        i, sc, el = select(R, T, X, Of, keys, sess, nb, sel, recent, score_R)
        if i < 0:
            picks.append({"year": y, "config": None, "n_eligible": int(el.sum())})
            continue
        tr = T[i, sel]
        cap = CAPS[int(keys["kind"][i])] * cap_mult
        s = scale_of(R[i, sel][tr].astype(np.float64), float(tr.mean()) * share[y], cap)
        sleeve[test], xs[test], scale[test], pick[test] = s * R[i, test], s * X[i, test], s, i
        for k, M_ in extra.items():
            other[k][test] = s * M_[i, test]
        xw = X[i, sel].astype(np.float64)
        bsel[test] = np.polyfit(xw, R[i, sel].astype(np.float64), 1)[0] if xw.std() > 0 else 0.0
        picks.append({"year": y, "config": cf.loc[i, "name"], "i": int(i), "score": float(sc[i]), "scale": s,
                      "n_eligible": int(el.sum()), "test_mean_bp": float(sleeve[test].mean() * 1e4),
                      "test_trades": int(T[i, test].sum())})
    oos = sess >= pd.Timestamp(OOS0)
    e3 = sess >= pd.Timestamp(ERAS["E3"][0])
    res = {"picks": picks, **summary(sleeve, xs, sess, oos, e3), "boot_p": boot_p(sleeve[oos]),
           "alpha_t_selection_beta": nw_t1((sleeve - bsel * xs)[oos])}   # the selection window's beta, out of sample
    if bench_sess is not None and np.std(bench_sess[oos]) > 0:
        res["beta_oneq"] = float(np.polyfit(bench_sess[oos], sleeve[oos], 1)[0])
    for k, v in other.items():
        res[k] = summary(v, xs, sess, oos, e3)
    return res, {"sleeve": sleeve, "x": xs, "pick": pick, "scale": scale, **other}


def oos_checks(res: dict) -> dict:
    return {"a_t": bool(res["t"] >= T_OOS), "b_e3_positive": bool(res["e3_mean_bp"] > 0),
            "c_alpha_t": bool(res["alpha_t"] >= T_OOS)}


def two_fold(R, T, X, Of, cf, keys, sess, nb) -> dict:
    """Interleaved quarters (ledger 0.6.5): select on Q1 + Q3 sessions, test on Q2 + Q4, and the reverse."""
    q = sess.quarter
    folds = {"X": np.isin(q, (1, 3)), "Y": np.isin(q, (2, 4))}
    out, pooled = {}, []
    for a, b in (("X", "Y"), ("Y", "X")):
        sel = folds[a]
        recent = sel & (sess > sess[sel].max() - pd.Timedelta(days=730))
        i, _, _ = select(R, T, X, Of, keys, sess, nb, sel, recent)
        if i < 0:
            out[f"{a}->{b}"] = None
            continue
        rt = R[i, folds[b]].astype(np.float64)
        out[f"{a}->{b}"] = {"config": cf.loc[i, "name"], "test_t": nw_t1(rt), "test_mean_bp": float(rt.mean() * 1e4),
                            "test_trades": int(T[i, folds[b]].sum())}
        pooled.append(rt / max(float(np.std(R[i, sel], ddof=1)), 1e-12))
    if len(pooled) == 2:
        out["pooled_t"] = nw_t1(np.concatenate(pooled))
    return out


def pbo(R, T, X, Of, keys, sess, nb, n_blocks: int = 16) -> dict:
    """Combinatorially symmetric cross-validation (ledger 0.6.5): 16 equal blocks of decision sessions (the earliest
    n mod 16 dropped), every 8/8 split, the walk-forward's eligibility (the recent window = in-sample sessions within
    24 months of the last in-sample one), score and tie-break in the in-sample half; the pick's out-of-sample IR rank
    among all configurations."""
    n_cfg, nd = R.shape
    drop = nd % n_blocks
    size = (nd - drop) // n_blocks
    blk = np.repeat(np.arange(n_blocks), size)
    Rr, Xr, Tr, Ofr = R[:, drop:], X[:, drop:], T[:, drop:], Of[:, drop:]
    dates, yrs = sess[drop:], sess.year[drop:]
    R64 = Rr.astype(np.float64)
    S1 = np.stack([R64[:, blk == k].sum(axis=1) for k in range(n_blocks)], 1)
    S2 = np.stack([(R64[:, blk == k] ** 2).sum(axis=1) for k in range(n_blocks)], 1)
    del R64
    TR = np.stack([Tr[:, blk == k].sum(axis=1) for k in range(n_blocks)], 1)
    OF = np.stack([Ofr[:, blk == k].sum(axis=1) for k in range(n_blocks)], 1)
    cum = np.zeros((n_cfg, nd - drop + 1), np.int32)
    cum[:, 1:] = np.cumsum(Ofr, axis=1)
    H = np.stack([row_hash(Rr[:, blk == k]) for k in range(n_blocks)], 1).astype(np.uint64)
    mult = np.random.default_rng(SEED).integers(1, 2**62, n_blocks, dtype=np.int64).astype(np.uint64)

    def irs(mask):
        n = size * int(mask.sum())
        s1, s2 = S1[:, mask].sum(axis=1), S2[:, mask].sum(axis=1)
        var = (s2 - s1 * s1 / n) / (n - 1)
        return np.where(var > 1e-30, (s1 / n) / np.sqrt(np.maximum(var, 1e-300)), 0.0), s1 / n

    lam, picks = [], {}
    nz = size * (n_blocks // 2)
    for comb in itertools.combinations(range(n_blocks), n_blocks // 2):
        ins = np.zeros(n_blocks, bool)
        ins[list(comb)] = True
        insd = ins[blk]
        w_idx = np.flatnonzero(insd & (dates > dates[insd].max() - pd.Timedelta(days=730)))
        runs = np.split(w_idx, np.flatnonzero(np.diff(w_idx) > 1) + 1)
        rec = sum(cum[:, r[-1] + 1] - cum[:, r[0]] for r in runs if len(r))
        ir_in, mu_in = irs(ins)
        hk = (H[:, ins] * mult[ins]).sum(axis=1)
        el = ((OF[:, ins].sum(axis=1) >= 0.5 * nz) & (rec >= 0.5 * len(w_idx))
              & (TR[:, ins].sum(axis=1) >= max(100, 0.25 * nz)) & ~pd.Series(hk).duplicated().to_numpy() & (mu_in > 0))
        j = -1
        for i in ranked(smoothed(ir_in, nb), el, keys):
            if alpha_years(Rr[i, insd], Xr[i, insd], yrs[insd])[1][0] > 0:
                j = int(i)
                break
        if j < 0:
            continue
        picks[j] = picks.get(j, 0) + 1
        o_all = irs(~ins)[0]
        o = o_all[j]
        rank = (o_all < o).sum() + 1 + 0.5 * ((o_all == o).sum() - 1)
        w = rank / (n_cfg + 1)
        lam.append(math.log(w / (1 - w)))
    lam = np.array(lam)
    return {"splits": int(len(lam)), "dropped_sessions": int(drop),
            "pbo": float((lam <= 0).mean()) if len(lam) else None,
            "lambda_median": float(np.median(lam)) if len(lam) else None,
            "lambda_q": {str(q): float(np.quantile(lam, q)) for q in (0.1, 0.25, 0.75, 0.9)} if len(lam) else {},
            "top_picks": {str(k): v for k, v in sorted(picks.items(), key=lambda kv: -kv[1])[:5]}}


def sharpe_shift(r: np.ndarray, traded: np.ndarray, k: float) -> np.ndarray:
    """The constant on the traded sessions that gives the series a per-session Sharpe of exactly k (0 elsewhere):
    with traded share p and traded-session variance s2, a traded-session mean a solves p a / sqrt(p s2 + p (1 - p)
    a^2) = k, i.e. the series mean is k sqrt(p s2 / q) with q = 1 - k^2 (1 - p) / p. Zeros when undefined."""
    p = float(traded.mean())
    s2 = float(r[traded].astype(np.float64).var()) if p > 0 else 0.0
    q = 1.0 - k * k * (1.0 - p) / p if p > 0 else 0.0
    if p <= 0 or s2 <= 0 or q <= 0:
        return np.zeros(len(r), np.float32)
    return np.where(traded, (k * math.sqrt(p * s2 / q) - float(r.mean(dtype=np.float64))) / p, 0.0).astype(np.float32)


def power_sim(R, RL, T, X, Of, cf, keys, sess, nb, share, cap_mult, targets=(0.4, 0.7, 1.0)) -> dict:
    """Ledger 0.6.5: give every configuration of one K10 family an annual Sharpe equal to the target over the
    decision sessions (a constant added on its traded sessions, sized by the closed form for a series that is 0 on
    the other sessions; the same shift in the per-leg-minimum series RL), rerun the walk-forward and record whether
    (a)-(d) pass, whether every pick then came from that family, and both; each family with K10 trades in turn."""
    per_year = len(sess) / max((sess[-1] - sess[0]).days / 365.25, 1e-9)
    fam_c = cf["fam"].to_numpy()
    fams = sorted(f for f in set(fam_c) if f in K10_FAMS | {"IC-BE", "IF-BE"} and T[fam_c == f].any())
    out = {}
    for tgt in targets:
        hits, own, both = {}, {}, {}
        for fam in fams:
            rows = np.flatnonzero(fam_c == fam)
            orig, orig_l = R[rows].copy(), RL[rows].copy()
            for i in rows:
                sh = sharpe_shift(R[i], T[i], tgt / math.sqrt(per_year))
                R[i] += sh
                RL[i] += sh
            try:
                res, _ = walk_forward(R, T, X, Of, cf, keys, sess, nb, share, cap_mult, extra={"leg_min": RL})
            finally:
                R[rows], RL[rows] = orig, orig_l
            ok = all(oos_checks(res).values()) and res["leg_min"]["mean_bp"] > 0 and res["leg_min"]["t"] >= 1.96
            mine = all(fam_c[p["i"]] == fam for p in res["picks"] if p.get("config"))
            hits[fam], own[fam], both[fam] = bool(ok), bool(mine), bool(ok and mine)
        mean = lambda d: float(np.mean(list(d.values()))) if d else None
        out[str(tgt)] = {"p_pass": mean(hits), "p_own_family_picked": mean(own), "p_pass_and_own": mean(both),
                         "by_family": hits}
    return out


def dsr_report(R, st: pd.DataFrame, cf: pd.DataFrame) -> dict:
    ev = np.flatnonzero(((st["trades"] >= 100) & ~st["dup"]).to_numpy())
    if not len(ev):
        return {"evaluable": 0}
    top = int(ev[np.argmax(st["ir"].to_numpy()[ev])])
    r = R[top].astype(np.float64)
    ir_ev = st["ir"].iloc[ev]
    famdm = ir_ev - ir_ev.groupby(cf["fam"].iloc[ev]).transform("mean")
    samp = np.random.default_rng(SEED).choice(ev, size=min(len(ev), 3000), replace=False)
    n_eff = int(round(leader_clusters(R[samp]) * len(ev) / len(samp)))
    sk, ku = float(pd.Series(r).skew()), float(pd.Series(r).kurt()) + 3
    ir = float(st["ir"].iloc[top])
    return {"config": cf.loc[top, "name"], "ir": ir, "evaluable": int(len(ev)), "n_eff": n_eff,
            "N": deflated_sharpe_n(ir, len(r), sk, ku, N_TRIALS, float(famdm.var())),
            "N_cumulative": deflated_sharpe_n(ir, len(r), sk, ku, N_CUM, float(famdm.var()))}


def axis_tables(cf: pd.DataFrame, st: pd.DataFrame, prefix: str, write: bool = True) -> dict:
    """In-grid summaries (ledger 0.6.3), for all configurations and separately for the same-day and overnight ones:
    A counts (raw, distinct, at the cumulative threshold), the family medians of t and the mean t over pairs of axes
    (CSV)."""
    t = pd.concat([cf, st], axis=1)
    t["entry"] = [entry_name(b, e) for b, e in zip(t["b"], t["e"])]
    t["filter"] = t["f"].map(lambda f: FILTERS[f])
    t["mgmt"] = np.where(t["b"] >= Q.NS, "BE", t["m"].map(lambda m: MGMT[m]))
    on = (t["e"] == ON).to_numpy()
    out = {}
    for grp, sel in (("all", np.ones(len(t), bool)), ("same_day", ~on), ("overnight", on)):
        g = t[sel]
        if write:
            OUT.mkdir(parents=True, exist_ok=True)
            for a, b in (("fam", "entry"), ("fam", "filter"), ("fam", "mgmt"), ("pool", "fam")):
                g.pivot_table(index=a, columns=b, values="t", aggfunc="mean").to_csv(
                    OUT / f"axes_{prefix}_{grp}_{a}_{b}.csv", float_format="%.3g")
        out[grp] = {"configs": int(len(g)), "A": int(g["A"].sum()), "A_distinct": int((g["A"] & ~g["dup"]).sum()),
                    "A_cum": int(g["A_cum"].sum()), "A_cum_distinct": int((g["A_cum"] & ~g["dup"]).sum()),
                    "family_median_t": {k: float(v) for k, v in g.groupby("fam")["t"].median().items()}}
    out["top"] = t.sort_values("t", ascending=False).head(10)[["name", "t", "alpha_t", "mean_bp", "trades"]].to_dict("records")
    return out


# ---------------------------------------------------------------------------------------------------- comparators
def comparator_specs() -> list[dict]:
    """The named comparators of ledger 0.6.6: (structure, entry, management, filter) at P10 unless stated."""
    S = shape
    C = lambda name: Q.NS + [c["name"] for c in COMP].index(name)
    spec = [(1, "lottery calls, Friday 15:00", [(S("LC", 0.05), 4, 0, "ALL")]),
            (2, "lottery puts, Friday 15:00", [(S("LP", 0.05), 4, 0, "ALL")]),
            (3, "OTM call at 09:45, sold at 3x", [(S("LC", 0.10), 0, 3, "ALL")]),
            (4, "tastylive 16-delta put credit spread, keep 1/4", [(S("PCS", 0.16), 0, 3, "ALL")]),
            (5, "tastylive 16-delta iron condor, keep 1/4", [(S("IC", 0.16), 0, 3, "ALL")]),
            (6, "Option Alpha ATM iron fly at 12:00, keep 1/4", [(S("IF"), 2, 3, "ALL")]),
            (7, "Chambless 5-delta spreads", [(S("PCS", 0.05), 0, 2, "ALL"), (S("CCS", 0.05), 0, 2, "ALL")]),
            (8, "break-even iron condor", [(C("IC-BE0.10-W2%"), LAD, 2, "ALL")]),
            (9, "Augen pin ratio fly", [(S("PRF"), 2, 3, "ALL")]),
            (10, "pin iron fly", [(S("IFP", 0.5, 0.02), 2, 0, "ALL")]),
            (11, "max-pain iron fly", [(S("IFM", 0.5, 0.02), 2, 0, "ALL")]),
            (12, "debit vertical toward max pain", [(S("MPD"), 2, 0, "ALL")]),
            (13, "opening-range breakout", [(ORB_SHAPE, 0, 0, "ALL")]),
            (14, "post-earnings ATM straddle (short report-only)", [(S("SD"), 0, 0, "EARN"), (S("LD"), 0, 0, "EARN")]),
            (15, "post-earnings iron fly (K10)", [(S("IF"), 0, 0, "EARN")]),
            (16, "0DTE covered call, stocks <= $100, P20", [(S("SC", 0.25), 0, 0, "ALL"), (S("SC", 0.10), 0, 0, "ALL")]),
            (17, "0DTE cash-secured put, stocks <= $100 (report-only)", [(S("SP", 0.16), 0, 0, "ALL")]),
            (18, "momentum calls", [(S("LC", 0.25), 1, 3, "MOM")]),
            (19, "Thursday-close spread / condor held into expiry", [(S("PCS", 0.10), ON, 3, "NOEARN"),
                                                                     (S("IC", 0.10), ON, 3, "NOEARN")]),
            (20, "the same, out at 09:45 (overnight only)", [(S("PCS", 0.10), ON, 1, "NOEARN"),
                                                            (S("IC", 0.10), ON, 1, "NOEARN")]),
            (21, "post-earnings directional ATM (report-only)", [(S("LC", 0.50), 0, 1, "EARN&MOM"),
                                                                 (S("LP", 0.50), 0, 1, "EARN&MOM")]),
            (22, "drift control RRB (report-only)", [(S("RRB"), e, 0, "ALL") for e in range(Q.NE)])]
    return [{"no": n, "what": w, "legs": legs, "pool": 20 if n == 16 else 10, "cheap": n in (16, 17)}
            for n, w, legs in spec]


def spec_mask(masks, fname: str, b: int, P: Q.Panel, cheap: bool) -> np.ndarray:
    if fname == "EARN&MOM":
        mk = mask_for(masks, "EARN", b, P) & mask_for(masks, "MOM", b, P)
    else:
        mk = mask_for(masks, fname, b, P)
    if cheap:
        s0 = P.names["S_e0"].to_numpy(float)
        mk = mk & (np.nan_to_num(s0, nan=1e9) <= 100.0)[None, :]
    return mk


def sstats(r, traded, sess: pd.DatetimeIndex) -> dict:
    r = np.asarray(r, np.float64)
    e3 = np.asarray(sess >= ERAS["E3"][0])
    tf = third_friday(sess)
    mean = lambda v: float(v.mean() * 1e4) if len(v) else None
    return {"mean_bp": mean(r), "t": nw_t1(r), "traded": int(np.asarray(traded).sum()), "e3_mean_bp": mean(r[e3]),
            "e3_t": nw_t1(r[e3]), "third_friday_mean_bp": mean(r[tf]), "other_friday_mean_bp": mean(r[~tf])}


def comparators(G: Grid, B: Bases, masks: dict, P: Q.Panel, Gmw: Grid | None, bench: pd.DataFrame) -> list[dict]:
    """Each comparator's K10 book (primary and conservative fills, 1-lot fees) and pool average (unit returns,
    primary and conservative, with and without the 1-lot fees) on the decision sessions; no 16 (covered call) also
    as stock + short call against the stock alone and ONEQ; the 2026 Monday / Wednesday expiries separately."""
    out = []
    for sp in comparator_specs():
        rows = []
        for b, e, m, fname in sp["legs"]:
            base = B.get(b)
            mk = spec_mask(masks, fname, b, P, sp["cheap"])
            cfg = (sp["pool"], b, e, m, 0)
            r = G.run(cfg, base, mk=mk)
            p, k = r["pool"], r["book"]
            row = {"config": f"P{sp['pool']}|{base_name(b)}|{entry_name(b, e)}|{'BE' if b >= Q.NS else MGMT[m]}|{fname}",
                   "pool_primary": sstats(p["primary"], p["traded"], G.sess),
                   "pool_cons": sstats(p["cons"], p["traded"], G.sess),
                   "pool_primary_1lot": sstats(p["primary_1lot"], p["traded"], G.sess),
                   "pool_cons_1lot": sstats(p["cons_1lot"], p["traded"], G.sess),
                   "entry_before_noon": "mixed" if e == LAD else bool(e in (0, 1) or b == ORB_SHAPE)}
            if sp["no"] == 16:                            # holding the stock all day plus the short call
                w = base["opened"][e] & G.inpool(sp["pool"], e) & mk[e]
                sc_, pc_ = P.names["S_close"].to_numpy(float), P.names["prev_close"].to_numpy(float)
                with np.errstate(divide="ignore", invalid="ignore"):
                    day = np.where((sc_ > 0) & (pc_ > 0), sc_ / pc_ - 1, 0.0)
                cnt = np.maximum(np.bincount(G.si[w], minlength=G.ns), 1)
                stock = np.bincount(G.si[w], weights=day[w], minlength=G.ns) / cnt
                tr = p["traded"]
                one = bench["oneq"].reindex(G.sess).to_numpy()
                bp = lambda v: float(np.mean(v[tr]) * 1e4) if tr.any() else None
                row["covered_call"] = {"sessions": int(tr.sum()), "stock_mean_bp": bp(stock),
                                       "covered_primary_1lot_mean_bp": bp(stock + p["primary_1lot"]),
                                       "covered_cons_1lot_mean_bp": bp(stock + p["cons_1lot"]),
                                       "oneq_mean_bp": bp(one),
                                       "covered_minus_oneq_t": nw_t1((stock + p["cons_1lot"] - one)[tr])}
            if base["k10"].any():
                row["k10_primary"] = sstats(k["primary"], k["traded"], G.sess)
                row["k10_cons"] = sstats(k["cons"], k["traded"], G.sess)
                row["k10_alpha_t"] = float(alpha_years(k["cons"], k["x"], G.sess.year)[1][0])
            if Gmw is not None and Gmw.ns:
                rm = Gmw.run(cfg, base, mk=mk)
                row["mon_wed_2026"] = {"pool_primary_mean_bp": float(rm["pool"]["primary"].mean() * 1e4),
                                       "traded": int(rm["pool"]["traded"].sum()),
                                       "k10_cons_mean_bp": float(rm["book"]["cons"].mean() * 1e4)}
            rows.append(row)
        out.append({"no": sp["no"], "what": sp["what"], "configs": rows})
    return out


def per_name(G: Grid, B: Bases, masks: dict, P: Q.Panel) -> dict:
    """Comparators 1, 2, 4, 9, 10, 11 on TSLA, NVDA and AAPL alone, unit returns at primary fills (descriptive; 9 and
    10 also with VIX <= 20)."""
    specs = {sp["no"]: sp for sp in comparator_specs()}
    names = P.names["name"].to_numpy()
    vix_ok = (np.nan_to_num(P.names["vix_prev"].to_numpy(float), nan=99.0) <= 20)[None, :]
    out = {}
    for t in ("TSLA", "NVDA", "AAPL"):
        one = (names == t)[None, :]
        for no in (1, 2, 4, 9, 10, 11):
            b, e, m, fname = specs[no]["legs"][0]
            base = B.get(b)
            for tag, extra in ((("", one), ("_vix_le_20", one & vix_ok)) if no in (9, 10) else (("", one),)):
                mk = spec_mask(masks, fname, b, P, False) & extra
                p = G.run((20, b, e, m, 0), base, mk=mk, book=False)["pool"]
                out[f"{t}_{no}{tag}"] = sstats(p["primary"], p["traded"], G.sess)
    return out


# ---------------------------------------------------------------------------------------------------- other reports
PICK_CONVS = {"touch_fills": ("touch", "sur"), "no_1lot_minimum": ("cons", None), "hold_to_close": ("hold", "sur"),
              "flat15": ("flat15", "sur"), "assignment": ("asn", "sur")}


def pick_conventions(G: Grid, B: Bases, picks: list[dict], cf: pd.DataFrame) -> dict:
    """The K10 out-of-sample sleeve of the walk-forward picks (same scales) under the other fill, fee and settlement
    conventions (report-only, ledger 0.5)."""
    ser = {k: np.zeros(G.ns) for k in PICK_CONVS}
    for p in picks:
        if not p.get("config"):
            continue
        cfg = tuple(int(v) for v in cf.loc[p["i"], ["pool", "b", "e", "m", "f"]])
        r = G.run(cfg, B.get(cfg[1]), pool_avg=False, convs=PICK_CONVS)["book"]
        t = G.sess.year == p["year"]
        for k in PICK_CONVS:
            ser[k][t] = p["scale"] * r[k][t]
    oos, e3 = G.sess >= pd.Timestamp(OOS0), G.sess >= pd.Timestamp(ERAS["E3"][0])
    out = {k: {"t": nw_t1(v[oos]), "mean_bp": float(v[oos].mean() * 1e4), "e3_mean_bp": float(v[e3].mean() * 1e4)}
           for k, v in ser.items()}
    if any(int(cf.loc[p["i"], "m"]) != 0 for p in picks if p.get("config")):
        out["flat15"] = None                              # registered for the H slot only
    return out


def account_needed(G: Grid, B: Bases, picks: list[dict], cf: pd.DataFrame) -> dict:
    """For the ALL program's picks: the account whose 3% daily max loss covers one contract of every position the
    pool average holds (summed max loss / 0.03) over the out-of-sample sessions with positions (ledger 0.10)."""
    need = []
    for p in picks:
        if not p.get("config"):
            continue
        pool, b, e, m, f = (int(v) for v in cf.loc[p["i"], ["pool", "b", "e", "m", "f"]])
        base = B.get(b)
        mk = mask_for(G.masks, FILTERS[f], b, G.P)
        tot = np.zeros(G.ns)
        for blk in G.blocks(b, e):
            w = base["opened"][blk] & G.inpool(pool, blk) & mk[blk]
            tot += np.bincount(G.si[w], weights=np.nan_to_num(base["ml"][blk][w]), minlength=G.ns)
        t = (G.sess.year == p["year"]) & (tot > 0)
        need.extend((tot[t] / (K10_LOSS / NAV0)).tolist())
    if not need:
        return {}
    return {"sessions": len(need), "median_usd": float(np.median(need)), "p90_usd": float(np.quantile(need, 0.9))}


def fill_report(rows: pd.DataFrame) -> dict:
    """Entry fills relative to the quotes (ledger 0.6.7): the share of the summed spread the F = 0.25 fill saved."""
    sel = rows[(rows["code"] == 0) & rows["shape"].isin(SELECTABLE) & (rows["spr_entry"] > 0)]
    if sel.empty:
        return {}
    nser = np.array([len(LEGQ[FAM[Q.SHAPES[x][0]]]) for x in sel["shape"]])
    share = (sel["imp_entry"] / (sel["spr_entry"] * sel["n_entry"].to_numpy() / nser)).clip(0, 1)   # PRF: 6 contracts
    return {"positions": int(len(sel)), "mean_share_of_spread_saved": float(share.mean()),
            "median": float(share.median())}


def close_report(P: Q.Panel) -> dict:
    """Last-bar close against the official close (ledger 0.8), all expiry days and index-rebalance Fridays."""
    nm = P.names
    d = pd.DatetimeIndex(nm["date"])
    with np.errstate(divide="ignore", invalid="ignore"):
        g = (nm["close_official"] / nm["S_close"] - 1).to_numpy(float) * 1e4
    ok = np.isfinite(g) & (nm["status"].to_numpy() != 2)        # status 2: no minute bar, no last-bar close
    reb = third_friday(d) & np.isin(d.month, (3, 6, 9, 12))
    f = lambda m: {"n": int(m.sum()), "mean_abs_bp": float(np.abs(g[m]).mean()) if m.any() else None,
                   "share_over_10bp": float((np.abs(g[m]) > 10).mean()) if m.any() else None}
    return {"all": f(ok), "rebalance_fridays": f(ok & reb)}


def stuck_report(P: Q.Panel, B: Bases) -> dict:
    """Stuck legs ('no free exit', ledger 0.5): among opened positions of the selectable shapes in a pool (the H slot;
    the overnight block by its entry-day rank), the share with a stuck leg and their P&L (primary and 'no free
    exit', dollars per contract), by name, year, pool and entry."""
    sh = np.array(SELECTABLE + [ORB_SHAPE])
    nm = P.names
    rk = np.vstack([np.tile(nm["rank"].to_numpy(), (Q.NE, 1)), nm["rank_on"].to_numpy()[None, :]])      # [NB, NK]
    o = P.opened[sh] & (rk >= 1)[None]
    st = (P.stuck[sh] > 0) & o
    pnl = 100.0 * np.where(st, P.pnl[sh][:, :, 0, :], 0.0).sum(axis=0)
    free = 100.0 * sum(np.where(st[j], B.get(int(b))["conv"]["free"][:, 0, :], 0.0) for j, b in enumerate(sh))
    tab = pd.DataFrame({"name": np.tile(nm["name"].to_numpy(), Q.NB),
                        "year": np.tile(pd.DatetimeIndex(nm["date"]).year.to_numpy(), Q.NB),
                        "pool": np.where(rk <= 10, "P10", "P11-20").ravel(),
                        "entry": np.repeat(["ON" if e == Q.NE else "%02d:%02d" % Q.ENTRY[e] for e in range(Q.NB)], len(nm)),
                        "opened": o.sum(axis=0).ravel(), "stuck": st.sum(axis=0).ravel(),
                        "pnl": pnl.ravel(), "free": free.ravel()})
    by = lambda c: {str(k): {"share": float(v["stuck"].sum() / max(v["opened"].sum(), 1)),
                             "usd_primary": float(v["pnl"].sum() / max(v["stuck"].sum(), 1)),
                             "usd_no_free_exit": float(v["free"].sum() / max(v["stuck"].sum(), 1))}
                    for k, v in tab.groupby(c)}
    worst = tab.groupby("name")[["opened", "stuck"]].sum()
    worst = worst[worst["opened"] >= 200].assign(share=lambda v: v["stuck"] / v["opened"]).sort_values("share").tail(15)
    return {"share": float(tab["stuck"].sum() / max(tab["opened"].sum(), 1)),
            "usd_primary_per_stuck": float(tab["pnl"].sum() / max(tab["stuck"].sum(), 1)),
            "usd_no_free_exit_per_stuck": float(tab["free"].sum() / max(tab["stuck"].sum(), 1)),
            "by_year": by("year"), "by_pool": by("pool"), "by_entry": by("entry"), "by_name": by("name"),
            "worst_names": {k: float(v) for k, v in worst["share"].items()}}


def exdiv_flags(P: Q.Panel, divs: pd.DataFrame | None) -> np.ndarray:
    """Overnight positions whose expiry day is the name's ex-dividend day with dividend / stock >= 0.3% (ledger 0.8)."""
    nm = P.names
    amt = dividend_on(nm, divs, pd.DatetimeIndex(nm["date"]))
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.nan_to_num(amt / nm["S_on"].to_numpy(float)) >= EXDIV_MIN


def overlay_report(sleeve_days: np.ndarray, bench: pd.DataFrame, days: pd.DatetimeIndex, sl: np.ndarray,
                   pools: dict | None = None) -> dict:
    one = bench["oneq"].to_numpy()[sl]
    lev = 1.3 * one - 0.3 * (bench["rf"].to_numpy()[sl] + 0.015 / 252)
    ov = one + sleeve_days[sl]
    mon = lambda r: pd.Series(r, index=days[sl]).groupby(days[sl].to_period("M")).apply(lambda x: np.prod(1 + x) - 1)
    alone = bench["rf"].to_numpy()[sl] + sleeve_days[sl]                  # BIL + the option sleeve (report-only)
    out = {"days": int(sl.sum()), "overlay_cagr": ann(ov), "oneq_cagr": ann(one), "oneq13_cagr": ann(lev),
           "standalone_cagr": ann(alone), "standalone_mdd": mdd(alone),
           "overlay_mdd": mdd(ov), "oneq_mdd": mdd(one), "oneq13_mdd": mdd(lev),
           "overlay_worst_month": float(mon(ov).min()), "oneq_worst_month": float(mon(one).min()),
           "oneq13_worst_month": float(mon(lev).min()), "sleeve_sd_bp": float(np.std(sleeve_days[sl]) * 1e4)}
    if LADDER.exists():
        lad = pd.read_csv(LADDER)
        lad.index = pd.PeriodIndex(lad["date"], freq="M")
        mo = mon(ov)
        common = mo.index.intersection(lad.index)
        if len(common) >= 12 and "BTC10" in lad:
            out["months_vs_ladder"] = int(len(common))
            out["overlay_cagr_monthly"] = float(np.prod(1 + mo[common]) ** (12 / len(common)) - 1)
            out["qqq_btc10_cagr"] = float(np.prod(1 + lad.loc[common, "BTC10"]) ** (12 / len(common)) - 1)
            out["qqq_btc10_worst_month"] = float(lad.loc[common, "BTC10"].min())
    for k in POOLS:                                       # the pool's stocks held equally (ledger 0.2)
        if pools and f"P{k}" in pools:
            r = pools[f"P{k}"][sl]
            out[f"pool{k}_stocks_cagr"], out[f"pool{k}_stocks_mdd"] = ann(r), mdd(r)
            out[f"pool{k}_stocks_worst_month"] = float(mon(r).min())
    return out


def pool_stocks(P: Q.Panel, stock_ret: pd.DataFrame | None, days: pd.DatetimeIndex) -> dict:
    """Ledger 0.2's benchmark 'the pool's stocks held in equal weight, no options': each month's members (rank 1..N on
    the month's expiry days) held every trading day of the month, the day's total return averaged over the members
    with a return (P10, P20); also how many member-months had no Yahoo returns."""
    if stock_ret is None or stock_ret.empty:
        return {}
    nm = P.names
    month = pd.DatetimeIndex(nm["date"]).to_period("M")
    dm = days.to_period("M")
    ret = stock_ret.reindex(days)
    out = {}
    for pool in POOLS:
        r, absent = np.zeros(len(days)), 0
        sel = (nm["rank"] >= 1) & (nm["rank"] <= pool)
        for mo, names in nm[sel].groupby(month[sel.to_numpy()])["name"]:
            cols = sorted(set(names) & set(ret.columns))
            absent += len(set(names)) - len(cols)
            rows = np.flatnonzero(dm == mo)
            if cols and len(rows):
                r[rows] = np.nan_to_num(ret.iloc[rows][cols].mean(axis=1, skipna=True).to_numpy())
        out[f"P{pool}"] = r
        out[f"P{pool}_member_months_without_returns"] = absent
    return out


def replay_10k(G: Grid, B: Bases, plan: dict, conv: str, bench: pd.DataFrame, days: pd.DatetimeIndex, sl: np.ndarray,
               pdt: bool) -> dict:
    """$10k integer replay (ledger 0.6.8): ONEQ in whole shares (rebalanced when cash drifts beyond 2% of NAV); on
    each decision session the K10 book of plan[year] (one contract per position, entry minute then rank, summed max
    loss <= $300, 1-lot fees); the old day-trade rule counted before entry per contract series (positions already
    opened that day count with all their series, the overnight block never counts; a blocked position's budget is
    not reused); shares a flatten left behind unwound at the next open (0.11% cost; a split at that open is flagged
    and left out)."""
    P = G.P
    nm = P.names
    gap = (nm["next_open"] - nm["S_close"]).to_numpy(float)
    split = nm["split_next"].to_numpy() != 0
    s_close = nm["S_close"].to_numpy(float)
    gap = np.where(s_close > 0, gap, np.nan)
    ret = bench["oneq"].to_numpy()
    px = np.cumprod(1 + ret)
    rf = bench["rf"].to_numpy()
    idx = np.flatnonzero(sl)
    if "oneq_close" in bench:                       # whole ONEQ shares at its traded price (total-return path kept)
        i0 = max(idx[0] - 1, 0)
        lvl = float(bench["oneq_close"].iloc[i0])
        if np.isfinite(lvl) and lvl > 0:
            px = px * (lvl / px[i0])
    p0 = px[idx[0]] / (1 + ret[idx[0]])
    sh = math.floor(NAV0 / p0)
    cash = NAV0 - sh * p0
    nav = NAV0
    spos = {d: i for i, d in enumerate(G.sess)}
    cache = {}
    navs, used = [], []                                    # used: (day index, day trades used that day)
    st = {"taken": 0, "skipped_pdt": 0, "fees": 0.0, "premium": 0.0, "split_flags": 0, "worst_day": 0.0}
    for di in idx:
        d = days[di]
        cash *= 1 + (rf[di] if cash > 0 else rf[di] + 0.015 / 252)
        pnl_day = 0.0
        cfg = plan.get(d.year)
        si = spos.get(d)
        if cfg is not None and si is not None:
            if cfg not in cache:
                s, blk, k = G.run(cfg, B.get(cfg[1]), pool_avg=False, positions=True)["positions"]
                grp = {}
                for a, bb, kk in zip(s, blk, k):
                    grp.setdefault(int(a), []).append((int(bb), int(kk)))
                cache[cfg] = grp
            base = B.get(cfg[1])
            m = cfg[3]
            past = sum(u for j, u in used if j >= di - 4)
            pot, used_today = 0, 0
            for blk, k in cache[cfg].get(si, []):
                ser = base["nser"] if blk != Q.NE else 0
                if pdt and past + pot + ser > 3:
                    st["skipped_pdt"] += 1
                    continue
                pot += ser
                used_today += int(base["dt"][blk, m, k])
                v = 100.0 * (base["conv"][conv][blk, m, k] - base["sur"][blk, m, k])
                ns_ = base["nsh"][blk, m, k]
                if ns_:
                    if split[k] or not np.isfinite(gap[k]):
                        st["split_flags"] += 1
                    else:
                        v += 100.0 * ns_ * gap[k] - 100.0 * STOCK_COST * abs(ns_) * s_close[k]
                pnl_day += v
                st["taken"] += 1
                st["fees"] += 100.0 * (base["fees"][blk, m, k] + base["sur"][blk, m, k])
                st["premium"] += 100.0 * base["prem"][blk, k]
            used.append((di, used_today))
        cash += pnl_day
        nav_new = cash + sh * px[di]
        st["worst_day"] = min(st["worst_day"], nav_new / nav - 1)
        if abs(cash) > 0.02 * nav_new:
            tgt = math.floor(nav_new / px[di])
            cash -= (tgt - sh) * px[di]
            sh = tgt
        nav = nav_new
        navs.append(nav)
    navs = np.array(navs)
    r = np.r_[navs[0] / NAV0 - 1, navs[1:] / navs[:-1] - 1]
    one = ret[sl]
    return {"days": int(len(idx)), "conv": conv, "old_pdt_rule": pdt, "cagr": ann(r), "oneq_cagr": ann(one),
            "mdd": mdd(r), "oneq_mdd": mdd(one), **st,
            "skipped_share": st["skipped_pdt"] / max(st["taken"] + st["skipped_pdt"], 1),
            "fees_over_premium": st["fees"] / max(st["premium"], 1e-9), "beats_oneq": bool(ann(r) > ann(one))}


def index_increment(st: pd.DataFrame, R: np.ndarray, cf: pd.DataFrame, sess: pd.DatetimeIndex) -> dict:
    """For any A passer: its increment over the QQQ / SPXW 0DTE series of the same family, delta, entry and
    management (ALL filter, W1 wing) from the zero_dte study's exports, as the intercept t of R on that series
    (ledger 0.6.3). Report-only; needs output/research_only/zero_dte/raw."""
    passers = np.flatnonzero(st["A"].to_numpy())
    if not len(passers):
        return {"passers": 0}
    out = {"passers": int(len(passers))}
    try:
        import studies.zero_dte as ZD
        for under in ("QQQ", "SPXW"):
            days, rows, _ = ZD.Z.read_exports(ZD.files_for(under), under=under)
            Pz = ZD.Z.build_panel(days, rows, under)
            Uz, _, _ = ZD.base_units(Pz, "primary")
            dz = Pz.days.index
            for i in passers[:20]:
                b, e, m = int(cf.loc[i, "b"]), int(cf.loc[i, "e"]), int(cf.loc[i, "m"])
                fam, (d, _) = base_fam(b), base_dw(b)
                key = (ZD.Z.FAM.index(fam), d, 0.0) if fam in ZD.Z.FAM else None
                zs = ZD.Z.SHAPES.index(key) if key in ZD.Z.SHAPES else None
                ze = list(ZD.Z.ENTRY).index(Q.ENTRY[e]) if e < Q.NE and Q.ENTRY[e] in list(ZD.Z.ENTRY) else None
                if b >= Q.NS or zs is None or ze is None:
                    out[f"{under}:{cf.loc[i, 'name']}"] = "no counterpart"
                    continue
                q = pd.Series(Uz[zs, ze, m], index=dz).reindex(sess).fillna(0.0).to_numpy()
                _, a, t = alpha_t(R[i][None, :], q[None, :])
                out[f"{under}:{cf.loc[i, 'name']}"] = {"increment_bp": float(a[0] * 1e4), "t": float(t[0])}
    except Exception as ex:                                    # report-only: the reason goes into the results
        out["error"] = str(ex)[:300]
    return out


# ---------------------------------------------------------------------------------------------------- inputs
def code_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def main_hash(book_line: str, mech_line: str) -> str:
    """The hash a formal run's main.py must have: the frozen main.py with its BOOK and MECH lines replaced."""
    lines = (ROOT / "qc" / "stock_0dte" / "main.py").read_text().split("\n")
    lines = [book_line if ln.startswith("BOOK = ") else (mech_line if ln.startswith("MECH = ") else ln) for ln in lines]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def chunk_problems(seen: list[tuple[str, str]]) -> list[str]:
    """Ledger 0.7: each registered chunk covered exactly once, by its own run or by consecutive reruns split at month
    ends (a chunk whose export overflowed the channel is rerun in halves); no run outside the registered chunks."""
    out = []
    rep_ = sorted({c for c in seen if seen.count(c) > 1})
    if rep_:
        out.append(f"repeated runs {rep_}")
    day = lambda x: pd.Timestamp(x)
    used = set()
    for a, b in CHUNKS:
        inside = sorted({r for r in seen if r[0] >= a and r[1] <= b})
        used.update(inside)
        ok = (bool(inside) and inside[0][0] == a and inside[-1][1] == b
              and all(day(x[1]) + pd.Timedelta(days=1) == day(y[0]) and day(y[0]).day == 1
                      for x, y in zip(inside, inside[1:])))
        if not ok:
            out.append(f"chunk {a}..{b} not covered exactly by {inside}")
    extra = sorted({r for r in seen if r not in used})
    if extra:
        out.append(f"runs outside the registered chunks {extra}")
    return out


def check_inputs(metas: list[dict], days_rec: pd.DataFrame, cal_days: pd.DatetimeIndex, allow_partial: bool) -> dict:
    """Ledger 0.7: every run formal (mech0), complete (left 0, final 1), error-free, on the frozen code; the
    registered chunks present once each; one day record for every week-end session of the book."""
    probs, seen = [], []
    if FROZEN.get("main") and code_hash(ROOT / "qc" / "stock_0dte" / "main.py") != FROZEN["main"]:
        probs.append("qc/stock_0dte/main.py is not the frozen file")
    for m in metas:
        rs = m.get("rs", {})
        run = " " + rs.get("RUN", "") + " "
        tag = run.split()[1] if run.split()[:1] == ["book"] else "?"
        seen.append(tuple(tag.split("..")))
        if " mech0 " not in run or " left 0 " not in run or " final 1 " not in run or rs.get("ERR") != "none":
            probs.append(f"{tag}: not a clean formal run ({run.strip()[-70:]} | ERR {rs.get('ERR')})")
        h = m.get("hashes", {})
        for k in ("sim", "simx", "optmath"):
            if FROZEN.get(k) and h.get(k) != FROZEN[k]:
                probs.append(f"{tag}: {k} {h.get(k)} is not the frozen {FROZEN[k]}")
        if FROZEN.get("main") and h.get("main") != main_hash(m.get("book_line", ""), m.get("mech_line", "")):
            probs.append(f"{tag}: main.py differs from the frozen file beyond the BOOK / MECH lines")
    probs += chunk_problems(seen)
    want = week_ends(cal_days)
    want = want[(want >= START) & (want <= END)]
    got = pd.DatetimeIndex(days_rec["date"])
    miss = want[~want.isin(got)]
    if len(miss):
        probs.append(f"{len(miss)} week-end sessions without a day record (e.g. {[str(x.date()) for x in miss[:3]]})")
    if got.duplicated().any():
        probs.append(f"{int(got.duplicated().sum())} repeated day records")
    if probs and not allow_partial:
        raise ValueError("; ".join(probs))
    return {"problems": probs, "runs": len(metas)}


def files() -> list[Path]:
    return sorted(RAW.glob("stock_0dte_*.json.gz"))


# ---------------------------------------------------------------------------------------------------- main
def analyse_frames(names: pd.DataFrame, rows: pd.DataFrame, days_rec: pd.DataFrame, metas: list[dict], *,
                   cal_days: pd.DatetimeIndex, bench: pd.DataFrame, rel: pd.DataFrame, ciks: dict[str, str],
                   divs: pd.DataFrame | None, stock_ret: pd.DataFrame | None = None, gaps: dict | None = None,
                   quick: bool = False,
                   allow_partial: bool = False, inputs_note: dict | None = None, write: bool = True) -> tuple[dict, dict]:
    """The whole registered analysis on decoded frames; returns (results, series)."""
    names, opens_report = fix_next_opens(names, (gaps or {}).get("gap"), (gaps or {}).get("gap2"))
    keep = ((names["date"] >= START) & (names["date"] <= END)).to_numpy()
    kmap = np.full(len(names), -1)
    kmap[keep] = np.arange(int(keep.sum()))
    names = names[keep].reset_index(drop=True)
    rows = rows[rows["k"].isin(np.flatnonzero(keep))].copy()
    rows["k"] = kmap[rows["k"].to_numpy()]
    days_rec = days_rec[(days_rec["date"] >= START) & (days_rec["date"] <= END)]
    inputs = check_inputs(metas, days_rec, cal_days, allow_partial)
    P = Q.build_panel(names, rows)
    nxt = pd.Series(cal_days[1:], index=cal_days[:-1]).reindex(pd.DatetimeIndex(P.names["date"]))
    B = Bases(P, dividend_on(P.names, divs, pd.DatetimeIndex(nxt.to_numpy())))
    rec_days = pd.DatetimeIndex(sorted(set(pd.DatetimeIndex(days_rec["date"]))))
    we = week_ends(cal_days)
    sess = we[(we >= START) & (we <= END) & we.isin(rec_days)]
    mw = rec_days[(rec_days.year == 2026) & ~rec_days.isin(we)]                  # the 2026 Monday / Wednesday expiries
    ef = earnings_flags(P, rel, ciks, cal_days)
    masks = filter_masks(P, bench, ef)
    G = Grid(P, sess, masks)
    Gmw = Grid(P, mw, masks)
    cf = config_list()
    keys = cfg_keys(cf)
    M = matrices(G, B, cf)
    sta = grid_stats(M["all"], M["all_t"], M["all_x"], sess)
    stk = grid_stats(M["k10"], M["k10_t"], M["k10_x"], sess)
    table = pd.concat([cf, sta.add_prefix("all_"), stk.add_prefix("k10_"),
                       pd.DataFrame({"all_t_cons": nw_t(M["all_cons"]), "k10_t_primary": nw_t(M["k10_primary"]),
                                     "k10_t_leg": nw_t(M["k10_leg"]), "k10_t_free": nw_t(M["k10_free"])})], axis=1)
    nb = neighbours(cf)
    cal = cal_days[(cal_days >= START) & (cal_days <= END)]
    share = {int(y): float(np.isin(cal[cal.year == y], we).mean()) for y in sorted(set(cal.year))}
    bsess = bench["oneq"].reindex(sess).to_numpy()
    res = {"inputs": {**inputs, **(inputs_note or {}), "next_opens": opens_report},
           "sessions": int(len(sess)), "mon_wed_2026_days": int(len(mw)),
           "name_days": int(len(names)), "configs": int(len(cf)), "t_grid": T_GRID, "t_cumulative": T_CUM,
           "sessions_by_era": {k: int(((sess >= a) & (sess <= b)).sum()) for k, (a, b) in ERAS.items()},
           "earnings": {"releases": int(len(rel)), "names_with_cik": int(sum(1 for v in ciks.values() if v)),
                        "header_times": float((rel["t_source"] == "header").mean()) if "t_source" in rel and len(rel) else None,
                        "known_name_days": float(ef["known"].mean()) if len(ef["known"]) else None,
                        "reaction_name_days": int(ef["react"].sum()),
                        "table_sha256": hashlib.sha256(rel.to_csv(index=False).encode()).hexdigest()},
           "grid_ALL": axis_tables(cf, sta, "all", write), "grid_K10": axis_tables(cf, stk, "k10", write)}
    k10_mult = NAV0 / K10_LOSS                     # K10 series are in NAV units with a $300 (3%) daily max loss
    wf_k, sk = walk_forward(M["k10"], M["k10_t"], M["k10_x"], M["k10_o"], cf, keys, sess, nb, share, k10_mult,
                            extra={"primary": M["k10_primary"], "leg_min": M["k10_leg"], "no_free_exit": M["k10_free"]},
                            bench_sess=bsess)
    wf_k["checks"] = {**oos_checks(wf_k),
                      "d_leg_min": bool(wf_k["leg_min"]["mean_bp"] > 0 and wf_k["leg_min"]["t"] >= 1.96)}
    wf_k["pass"] = bool(all(wf_k["checks"].values()))
    wf_k["no_free_exit_checks"] = oos_checks(wf_k["no_free_exit"])
    wf_a, sa = walk_forward(M["all"], M["all_t"], M["all_x"], M["all_o"], cf, keys, sess, nb, share, 1.0,
                            extra={"cons": M["all_cons"]}, bench_sess=bsess)
    wf_a["checks"] = oos_checks(wf_a)
    wf_a["pass"] = bool(all(wf_a["checks"].values()))
    wf_kp, _ = walk_forward(M["k10"], M["k10_t"], M["k10_x"], M["k10_o"], cf, keys, sess, nb, share, k10_mult,
                            score_R=M["k10_primary"])
    res["walk_forward"] = {"K10": wf_k, "ALL": wf_a,
                           "K10_scored_on_primary": {k: wf_kp[k] for k in ("t", "mean_bp", "e3_mean_bp", "alpha_t")},
                           "K10_other_conventions": pick_conventions(G, B, wf_k["picks"], cf),
                           "ALL_account_needed": account_needed(G, B, wf_a["picks"], cf)}
    oos_s = sess >= pd.Timestamp(OOS0)
    on_years = sorted({p["year"] for p in wf_k["picks"] if p.get("config") and int(cf.loc[p["i"], "e"]) == ON})
    exd = exdiv_flags(P, divs)
    res["exdiv_overnight"] = {"name_days": int(exd.sum()), "years_with_overnight_picks": on_years}
    if on_years:                                   # the overnight picks without ex-dividend expiry days (sensitivity)
        alt = sk["sleeve"].copy()
        for p in wf_k["picks"]:
            if p["year"] in on_years:
                pool, b, e, m, f = (int(v) for v in cf.loc[p["i"], ["pool", "b", "e", "m", "f"]])
                mk = mask_for(masks, FILTERS[f], b, P) & ~exd[None, :]
                r = G.run((pool, b, e, m, f), B.get(b), mk=mk, pool_avg=False)["book"]["cons"]
                t = sess.year == p["year"]
                alt[t] = p["scale"] * r[t]
        res["exdiv_overnight"]["k10_t_without"] = nw_t1(alt[oos_s])
    days = cal
    to_days = lambda v: pd.Series(v, index=sess).reindex(days).fillna(0.0).to_numpy()
    bd = bench.reindex(days)
    oos_d, e3_d = np.asarray(days >= pd.Timestamp(OOS0)), np.asarray(days >= pd.Timestamp(ERAS["E3"][0]))
    pools = pool_stocks(P, stock_ret, days)
    res["pool_stocks_coverage"] = {k: v for k, v in pools.items() if k.endswith("without_returns")}
    res["equal_risk"] = {f"{w}_{c}": overlay_report(to_days(v), bd, days, sl, pools)
                         for c, v in (("cons", sk["sleeve"]), ("primary", sk["primary"]))
                         for w, sl in (("oos", oos_d), ("E3", e3_d))}
    res["two_fold"] = {"K10": two_fold(M["k10"], M["k10_t"], M["k10_x"], M["k10_o"], cf, keys, sess, nb),
                       "ALL": two_fold(M["all"], M["all_t"], M["all_x"], M["all_o"], cf, keys, sess, nb)}
    ev = ((stk["trades"] >= 100) & ~stk["dup"]).to_numpy()
    lr = np.sqrt(np.maximum(nw_var(M["k10"][ev].astype(np.float64)
                                   - M["k10"][ev].mean(axis=1, dtype=np.float64)[:, None]), 0))
    so = sk["sleeve"][oos_s]
    fam_ev = cf["fam"].to_numpy()[ev]
    res["mde"] = {"oos_sleeve_bp": float(math.sqrt(max(nw_var((so - so.mean())[None, :])[0], 0)) * (T_OOS + 0.84)
                                         / math.sqrt(max(len(so), 1)) * 1e4),
                  "k10_family_median_bp": {f: float(np.median(lr[fam_ev == f]) * (T_GRID + 0.84) / math.sqrt(len(sess)) * 1e4)
                                           for f in sorted(set(fam_ev))}}
    if not quick:
        res["pbo_K10"] = pbo(M["k10"], M["k10_t"], M["k10_x"], M["k10_o"], keys, sess, nb)
        res["bootstrap_K10"] = bootstrap_family(M["k10"], M["k10_t"], stk["dup"].to_numpy())
        res["bootstrap_ALL"] = bootstrap_family(M["all"], M["all_t"], sta["dup"].to_numpy())
        res["dsr_K10"] = dsr_report(M["k10"], stk, cf)
        res["dsr_ALL"] = dsr_report(M["all"], sta, cf)
        res["power"] = power_sim(M["k10"], M["k10_leg"], M["k10_t"], M["k10_x"], M["k10_o"], cf, keys, sess, nb, share,
                                 k10_mult)
        res["index_increment_ALL"] = index_increment(sta, M["all"], cf, sess)
        res["index_increment_K10"] = index_increment(stk, M["k10"], cf, sess)
    res["comparators"] = comparators(G, B, masks, P, Gmw, bench)
    res["per_name"] = per_name(G, B, masks, P)
    res["fills"] = fill_report(rows)
    res["official_close"] = close_report(P)
    res["stuck"] = stuck_report(P, B)
    # the $10k integer replay over E3: the K10 picks, then the defined-risk comparators (ledger 0.6.8)
    plan = {p["year"]: tuple(int(v) for v in cf.loc[p["i"], ["pool", "b", "e", "m", "f"]])
            for p in wf_k["picks"] if p.get("config") and p["year"] >= int(ERAS["E3"][0][:4])}
    res["replay_10k"] = {f"K10_{c}_{'old_pdt' if pdt else 'no_pdt'}": replay_10k(G, B, plan, c, bd, days, e3_d, pdt)
                         for c in ("cons", "primary") for pdt in (True, False)}
    specs = {sp["no"]: sp for sp in comparator_specs()}
    for no in (4, 5, 6, 7, 8, 9, 10, 11, 15, 19, 20):            # the defined-risk comparators
        legs = specs[no]["legs"]
        for b, e, m, fname in legs:
            cfg = (specs[no]["pool"], b, e, m, FILTERS.index(fname))
            tag = f"comparator_{no}" + (f"_{base_name(b)}" if len(legs) > 1 else "")
            for c in ("cons", "primary"):
                for pdt in (True, False):
                    res["replay_10k"][f"{tag}_{c}_{'old_pdt' if pdt else 'no_pdt'}"] = replay_10k(
                        G, B, {y: cfg for y in range(2023, 2027)}, c, bd, days, e3_d, pdt)
    if write:
        try:
            ov = pd.Series(bd["oneq"].to_numpy()[oos_d] + to_days(sk["sleeve"])[oos_d], index=days[oos_d])
            card = scorecard(ov, freq="D", oneq=bd["oneq"][oos_d], qqq=bd["qqq"][oos_d], rf=bd["rf"][oos_d],
                             meta={"n_trials": N_TRIALS, "oos_status": "passed" if wf_k["pass"] else "failed"})
            card.to_csv(OUT / "scorecard_k10.csv", index=False)
            res["scorecard_rows"] = int(len(card))
        except Exception as ex:
            res["scorecard_error"] = str(ex)[:200]
    er, rp = res["equal_risk"], res["replay_10k"]
    res["go"] = {"1_k10_oos_pass": bool(wf_k["pass"]),
                 "2_pbo_below_half": bool(res.get("pbo_K10", {}).get("pbo") is not None and res["pbo_K10"]["pbo"] < 0.5),
                 "3_replay_beats_oneq_E3": bool(rp["K10_cons_old_pdt"]["beats_oneq"] and rp["K10_primary_old_pdt"]["beats_oneq"]),
                 "4_beats_oneq13": bool(all(v["overlay_cagr"] > v["oneq13_cagr"] for v in er.values())),
                 "6_no_free_exit": bool(all(wf_k["no_free_exit_checks"].values()))}
    res["go"]["GO"] = bool(all(res["go"].values()))
    res["go"]["science_only"] = bool(wf_a["pass"] and not wf_k["pass"])
    series = {"table": table, "sess": sess, "sleeve_k10": sk["sleeve"], "sleeve_k10_primary": sk["primary"],
              "sleeve_all": sa["sleeve"], "pick_k10": sk["pick"], "M": M, "P": P, "masks": masks, "cf": cf}
    return res, series


def analyse(paths: list[Path], quick: bool = False, allow_partial: bool = False) -> dict:
    names, rows, days_rec, metas = Q.read_exports(paths)
    cal_days = calendar()
    bench = benchmarks(cal_days)
    ciks = ticker_ciks(metas)
    rel = earnings_table({c for cs in ciks.values() for c in cs}, cal_days)
    divs, stock_ret, gaps = yahoo_daily(set(names["name"]))
    note = {"files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
            "dividends": {"rows": int(len(divs)), "missing_tickers": divs.attrs.get("missing", []),
                          "sha256": hashlib.sha256(divs.to_csv(index=False).encode()).hexdigest()},
            "analysis_code": {"studies/stock_0dte.py": code_hash(Path(__file__)),
                              "quant/strategies/stock_0dte.py": code_hash(Path(Q.__file__))}}
    res, series = analyse_frames(names, rows, days_rec, metas, cal_days=cal_days, bench=bench, rel=rel, ciks=ciks,
                                 divs=divs, stock_ret=stock_ret, gaps=gaps, quick=quick, allow_partial=allow_partial,
                                 inputs_note=note)
    OUT.mkdir(parents=True, exist_ok=True)
    series["table"].drop(columns=["b"]).to_csv(OUT / "configs.csv.gz", index=False, float_format="%.5g")
    pd.DataFrame({"date": series["sess"], "sleeve_k10": series["sleeve_k10"],
                  "sleeve_k10_primary": series["sleeve_k10_primary"], "sleeve_all": series["sleeve_all"],
                  "pick_k10": series["pick_k10"]}).to_csv(OUT / "walkforward.csv", index=False)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--quick", action="store_true", help="skip PBO, the bootstraps, the deflated Sharpe and the power run")
    ap.add_argument("--allow-partial", action="store_true", help="mechanism smoke tests only")
    a = ap.parse_args(argv)
    paths = [Path(f) for f in a.files] if a.files else files()
    if not paths:
        raise SystemExit(f"no exports under {RAW}")
    res = analyse(paths, a.quick, a.allow_partial)
    write_json(OUT / "results.json", res)
    print(json.dumps({k: res.get(k) for k in ("sessions", "name_days", "configs", "go")}, indent=1, default=str))


if __name__ == "__main__":
    main()
