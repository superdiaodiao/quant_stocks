"""Selection and analysis of the "T" grid (docs/research_ledger_t_grid.md section 0): the parallel grid run with its
per-year accumulators (cached), the neighbourhood-median selector, the judged windows, the two-fold selection, the
walk-forward picks and their stitched exact re-runs, and the surface tables (heat maps, marginals, summary,
stability, multiple testing).

Moved unchanged from studies/t_grid.py in the slimming pass; the study keeps its configuration (bases, cost
scenarios, output / cache folders), the verdicts and the reporting. The window statistics computed from the
accumulators are quant.evaluation.events ``grid_window_stats`` / ``grid_hmatch_cagr``.
"""
from __future__ import annotations

import pickle
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from quant.evaluation.criteria import NORM, bh_reject, bonferroni_t, deflated_sharpe
from quant.evaluation.events import basket_metrics_vs as metrics_vs, grid_hmatch_cagr as geo_hmatch
from quant.evaluation.events import grid_window_stats as wstats
from quant.evaluation.metrics import TRADING_DAYS, cagr_of
from quant.evaluation.stats import spearman_finite as spearman
from quant.strategies.t_grid import (
    ACC_KEYS, AXES, DIRS, END, GLOB_KEYS, MARGIN_SPREAD, SHAPE, T10_END, T10_START, U18_START, CostModel, base_rows,
    cfg_arrays, eval_ids, exact_run, grid, hmatch_series, label, oneq_for, simulate_grid,
)

WF_MIN_SESSIONS = 750
ORDINAL = ("level", "hold", "frac", "reserve")
N_GRID = int(np.prod(SHAPE))
HALF_SPLIT_YEAR = {"QQQ": 2013, "U18": 2019, "T10": 2019}   # half2 starts on Jan 1 of this year


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


# ======================================================================== parallel grid runs (cached accumulators)

_ROWS: dict = {}
_COSTS: dict = {}


def _init_worker(paths: dict, cost_table: dict):
    _COSTS.update(cost_table)
    for k, p in paths.items():
        with open(p, "rb") as fh:
            _ROWS[k] = pickle.load(fh)


def _job(args):
    base, cname, lo, hi = args
    codes = cfg_arrays(grid().iloc[lo:hi])
    on, mult = _COSTS[cname]
    t0 = time.time()
    sim = simulate_grid(_ROWS[base], codes, CostModel(on, mult))
    return base, cname, lo, hi, sim["acc"], sim["glob"], sim["M"], time.time() - t0


def run_grid_all(data: dict, bases, costs, workers: int, chunk: int, n_configs: int, cache, cost_table: dict) -> dict:
    """Every configuration of every base under each cost scenario of ``costs`` (``cost_table``: name -> (costs on,
    spread multiple)), in chunks over ``workers`` processes; accumulators saved to ``cache``."""
    cache.mkdir(parents=True, exist_ok=True)
    paths = {}
    rows_by = {}
    for b in bases:
        rows_by[b] = base_rows(data, b)
        paths[b] = cache / f"rows_{b}.pkl"
        with open(paths[b], "wb") as fh:
            pickle.dump(rows_by[b], fh, protocol=pickle.HIGHEST_PROTOCOL)
    jobs = [(b, c, lo, min(lo + chunk, n_configs)) for b in bases for c in costs for lo in range(0, n_configs, chunk)]
    # biggest jobs first
    jobs.sort(key=lambda j: -rows_by[j[0]].c.size)
    parts: dict = {}
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                             initargs=({k: str(v) for k, v in paths.items()}, cost_table)) as ex:
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
        np.savez_compressed(cache / f"acc_{b}_{c}.npz", years=years, M=ps[0][3],
                            **{f"acc_{k}": v for k, v in sims[(b, c)]["acc"].items()},
                            **{f"glob_{k}": v for k, v in ps[0][2].items()})
    return sims


def load_sims(bases, costs, data, cache) -> dict:
    """The accumulators saved by ``run_grid_all`` in ``cache``."""
    sims = {}
    for b in bases:
        rows = base_rows(data, b)
        for c in costs:
            p = cache / f"acc_{b}_{c}.npz"
            z = np.load(p)
            sims[(b, c)] = {"years": z["years"], "M": z["M"],
                            "acc": {k: z[f"acc_{k}"] for k in ACC_KEYS}, "glob": {k: z[f"glob_{k}"] for k in GLOB_KEYS},
                            "f": rows.rf + MARGIN_SPREAD / TRADING_DAYS, "oneq": rows.oneq, "sessions": rows.sessions}
    return sims


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


# ======================================================================== surface tables

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


def surface_frame(data: dict, sims: dict, base: str, costs) -> tuple[pd.DataFrame, dict]:
    """The surface table of ``base`` (base costs) with the other cost scenarios' IR / excess columns, and the
    reference CAGRs per window (H_base per reserve, H100, ONEQ overlap) from one exact run of the references."""
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
    return df, extra


def surface_summary(df: pd.DataFrame, extra: dict, costs, col: str = "full_excess_cagr_hmatch") -> dict:
    """Per window: distribution of the CAGR excess vs H_match and the references; by direction; the median
    configuration by ``col``."""
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
    return summ


def stability(df: pd.DataFrame, col: str = "full_excess_cagr_hmatch") -> dict:
    """Rank agreement of the two halves, the half-2 fate of the half-1 top 1%, and how many ordinal neighbours of
    the full-window top 50 also beat H_match."""
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
    out = {"spearman_ir_half1_half2": spearman(i1, i2),
                      "spearman_excess_half1_half2": spearman(df["half1_excess_cagr_hmatch"].to_numpy(),
                                                              df["half2_excess_cagr_hmatch"].to_numpy()),
                      "top1pct_half1_median_half2_pct_rank": float(np.median(pct2[top1])),
                      "top1pct_half1_share_pos_half2": float((df["half2_excess_cagr_hmatch"].to_numpy()[top1] > 0).mean()),
                      "top50_full_mean_share_neighbours_pos": float(np.nanmean(nb_share)),
                      "top50_full_configs": [df.loc[int(i), "config"] for i in top50[:10]]}
    for d in DIRS:
        m = (df["direction_v"] == d).to_numpy()
        out[f"spearman_ir_halves_{d}"] = spearman(i1[m], i2[m])
    return out


def multiple_testing(data: dict, base: str, df: pd.DataFrame, hd: dict, rf: pd.Series) -> tuple[dict, np.ndarray]:
    """Full-window one-sided p-values of the surface t (BH, Bonferroni counts) and the deflated Sharpe ratio of the
    full-window best by IR (exact daily excess vs H_match); returns (statistics, p-values)."""
    tfull = df["full_t"].to_numpy()
    p = np.array([1 - NORM.cdf(x) if np.isfinite(x) else 1.0 for x in tfull])
    best = int(np.nanargmax(df["full_ir"].to_numpy()))
    mt = {"bh_rejections_q05": bh_count(p), "n_t_above_bonferroni_base": int((tfull >= bonferroni_t(N_GRID)).sum()),
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
    mt["dsr_best"] = deflated_sharpe(sr, len(dex), skew, kurt, df["full_sr_daily"].dropna().to_numpy())
    mt["best_exact_full"] = exb[best]["m"]
    return mt, p


def two_fold(data: dict, base: str, df: pd.DataFrame, hd: dict, costs, cost_table: dict,
             rf: pd.Series) -> tuple[dict, dict]:
    """Select on one half (selectors A, P and A per direction), evaluate exactly on the other half under each cost
    scenario; returns (selection info, fold metrics incl. the base-cost series under ``_series``)."""
    g = grid()
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
            on, mult = cost_table[cname]
            ev = eval_ids(data, base, ids, hd[oos][0], hd[oos][1], CostModel(on, mult), rf)
            folds[fold][cname] = {k: ev[v]["m"] for k, v in pick.items()}
            if cname == "base":
                folds[fold]["_series"] = {k: (ev[v]["r"], ev[v]["hm"]) for k, v in pick.items()}
    return sel_info, folds


def walk_forward(data: dict, base: str, sim: dict, costs, cost_table: dict, rf: pd.Series) -> dict:
    """Yearly walk-forward picks (selectors A and P) stitched from exact re-runs under each cost scenario."""
    picks = walk_forward_picks(sim)
    wf = {}
    for which in ("A", "P"):
        wf[which] = {}
        for cname in costs:
            on, mult = cost_table[cname]
            sres = stitch_wf(data, base, picks, which, CostModel(on, mult), rf)
            wf[which][cname] = {"m": sres["m"], "years": sres["years"]}
    return wf
