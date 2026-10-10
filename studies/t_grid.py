"""A broad "T" grid (做T 大网格) around a held base with honest out-of-sample selection, pre-registered in
docs/research_ledger_t_grid.md (section 0). Not QuantConnect.

Grid per base (61,440 configurations): direction (S sell-high / B buy-dip / C both) x trigger measure (D10, D20, D50,
RSI2, RSI14, R1, R5, Z1) x trigger level (expanding own-history quantiles 80/90/95/98, mirrored 20/10/5/2) x exit
(SMA, TARGET 2 sigma, TRAIL 2 sigma, OPP = measure back to its median, TIME) x max hold (5/10/20/40) x fraction of the
base (1/5, 1/3, 1/2, 1) x cash reserve (0 = margin account, 10/25/50% cash account) x regime (ALL / UP = close >
SMA200). Bases: QQQ, U18 (18 large caps, Yahoo OHLC from the selective_t cache), T10 (monthly point-in-time top-10 by
market cap from the megacap v2 study, v2 panel closes, close-only execution).

The simulator is vectorised over configurations and accounts (numpy arrays of shape (accounts, configs)); one Python
loop over sessions. Account / execution / cost rules are those of quant.strategies.selective_t (tested equal on
the S1-like special case). The grid run accumulates per calendar year the sufficient statistics of each
configuration's daily basket returns (sum R, R^2, R*M, log(1+R), exposure, trips ...), from which every window
statistic used for selection is computed. Selected configurations are re-run exactly (fresh $10k accounts on the
test window, per-account matched exposure).

Usage: PYTHONPATH=. .venv/bin/python scripts/research_t_grid.py [--workers 12] [--smoke]
Outputs (returns / statistics only, no prices): output/research_only/t_grid/. Local derived caches (not in Git):
research_cache/t_grid/. The engine is quant.strategies.t_grid; the grid run, selection and analysis stay here.
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
import time
from concurrent.futures import ProcessPoolExecutor
from statistics import NormalDist

import numpy as np
import pandas as pd

from quant.evaluation.criteria import bh_reject, bonferroni_t, deflated_sharpe
from quant.evaluation.metrics import TRADING_DAYS, cagr_of, max_drawdown, monthly, relative_metrics, t_and_ir
from quant.strategies import selective_t as st
from quant.paths import ROOT
from quant.strategies.t_grid import (  # noqa: F401  (tg.* names read by tests and the observation)
    ACC_KEYS, AXES, BUFFER, CostModel, DIRS, DIR_NONE, GLOB_KEYS, MARGIN_SPREAD, Rows, SHAPE, START_EQUITY,
    T10_END, T10_START, U18_START, base_rows, cfg_arrays, cost_vec, exact_run, expanding_thresholds, grid,
    hbase_of, hmatch_series, label, load_all, make_inst, oneq_for, simulate_grid, t10_spells,
)

MAIN = st.MAIN
END = st.END
CACHE = MAIN / "research_cache/t_grid"
OUT = ROOT / "output/research_only/t_grid"

WF_MIN_SESSIONS = 750

ORDINAL = ("level", "hold", "frac", "reserve")
N_GRID = int(np.prod(SHAPE))
BASES = ("QQQ", "U18", "T10")
HALF_SPLIT_YEAR = {"QQQ": 2013, "U18": 2019, "T10": 2019}   # half2 starts on Jan 1 of this year
COSTS = {"base": (True, 1.0), "zero": (False, 1.0), "x2": (True, 2.0)}
NORM = NormalDist()


# ======================================================================== grid


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
    return int(bh_reject(p, q).sum())


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
