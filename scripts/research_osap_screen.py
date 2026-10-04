"""Screen of the 212 published stock-return predictors in Open Source Asset Pricing (Chen & Zimmermann, Oct 2025).

Rules are pre-registered in docs/research_ledger_osap.md section 0 (written before any number was computed).

For every predictor and three periods -- (a) the original paper's sample, (b) post-publication (Jan of Year+1 ..
2024-12), (c) 2015-01 .. 2024-12 -- compute the monthly mean (%), t (mean / sd * sqrt(n)) and months of:
  ls_op      long-short following the original paper (PredictorPortsFull, port LS)
  lc_ls      long-short among stocks above the NYSE 20th size percentile (LiqScreen_ME_gt_NYSE20pct)
  lc_long_x  large/mid-cap long leg minus the market (Ken French Mkt-RF + RF)  <- the ranking measure
  vw_long_x  value-weighted long leg minus the market (LiqScreen_VWforce)
The long leg is the highest port (LS == top port - port 01 for all 212 signals; checked in ``check_ls_identity``).

Multiple-testing hurdle: post-2015 lc_long_x t >= 3.0 with >= 60 months; Bonferroni and BH FDR also reported.

Raw files: /Users/bytedance/code/quant_stocks/research_cache/osap/ (not committed). No strategy is tuned here.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = Path("/Users/bytedance/code/quant_stocks/research_cache/osap")
OUT = ROOT / "output/research_only/osap"
NORM = NormalDist()

DATA_END = "2024-12-31"
POST2015 = ("2015-01-01", DATA_END)
HURDLE_T = 3.0
TIER2_T = 2.0
MIN_MONTHS = 60
FILES = {"op": "PredictorPortsFull.csv",
         "lc": "PredictorAltPorts_LiqScreen_ME_gt_NYSE20pct.csv",
         "vw": "PredictorAltPorts_LiqScreen_VWforce.csv"}

# Data the signal needs that we do not have for free (SignalDoc Cat.Data / known inputs). Used in step 2 only.
UNAVAILABLE_DATA = {"Analyst", "Options", "13F", "ShortInterest", "Event", "Other", "Trading"}


# ======================================================================== loading

def load_doc(raw: Path = RAW) -> pd.DataFrame:
    d = pd.read_csv(raw / "SignalDoc.csv")
    d = d[d["Cat.Signal"] == "Predictor"].copy()
    for c in ("Year", "SampleStartYear", "SampleEndYear"):
        d[c] = pd.to_numeric(d[c], errors="coerce").astype("Int64")
    return d.set_index("Acronym")


def load_ports(path: Path) -> pd.DataFrame:
    p = pd.read_csv(path, dtype={"port": str, "signalname": str}, usecols=["signalname", "port", "date", "ret"])
    p["date"] = pd.to_datetime(p["date"]) + pd.offsets.MonthEnd(0)
    if p["date"].max() > pd.Timestamp(DATA_END):
        raise AssertionError(f"{path.name}: data after {DATA_END}")
    return p


def load_market(raw: Path = RAW) -> pd.Series:
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


# ======================================================================== the screen math

def tstat(x: pd.Series) -> tuple[float, float, int]:
    """(mean, t, n) of a monthly series; t = mean / sd(ddof=1) * sqrt(n)."""
    x = pd.Series(x).dropna()
    n = len(x)
    if n < 2:
        return (float(x.mean()) if n else np.nan, np.nan, n)
    sd = x.std(ddof=1)
    return float(x.mean()), float(x.mean() / sd * math.sqrt(n)) if sd > 0 else np.nan, n


def periods_for(row) -> dict:
    """The three periods of one signal as (start, end) month-end strings."""
    out = {"post2015": POST2015}
    if pd.notna(row["SampleStartYear"]) and pd.notna(row["SampleEndYear"]):
        out["is"] = (f"{int(row['SampleStartYear'])}-01-01", f"{int(row['SampleEndYear'])}-12-31")
    if pd.notna(row["Year"]):
        out["postpub"] = (f"{int(row['Year']) + 1}-01-01", DATA_END)
    return out


def window(x: pd.Series, start: str, end: str) -> pd.Series:
    return x[(x.index >= pd.Timestamp(start)) & (x.index <= pd.Timestamp(end))]


def bh_reject(pvals, q: float = 0.05) -> np.ndarray:
    """Benjamini-Hochberg: boolean array of rejections at FDR q (NaN p-values never rejected)."""
    p = np.asarray(pvals, float)
    ok = np.isfinite(p)
    m = ok.sum()
    rej = np.zeros(len(p), bool)
    if m == 0:
        return rej
    idx = np.where(ok)[0][np.argsort(p[ok])]
    ranked = p[idx]
    below = ranked <= q * np.arange(1, m + 1) / m
    if below.any():
        k = np.max(np.where(below)[0])
        rej[idx[:k + 1]] = True
    return rej


def one_sided_p(t: float) -> float:
    return 1 - NORM.cdf(t) if np.isfinite(t) else np.nan


def screen(doc: pd.DataFrame, op: dict, lc: dict, vw: dict, mkt: pd.Series) -> pd.DataFrame:
    rows = []
    for s, meta in doc.iterrows():
        per = periods_for(meta)
        series = {}
        if s in op:
            series["ls_op"] = op[s]["ls"]
        if s in lc:
            series["lc_ls"] = lc[s]["ls"]
            series["lc_long_x"] = (lc[s]["long"] - mkt.reindex(lc[s].index)).dropna()
            # post-hoc diagnostic (not part of the hurdle): long leg minus the average of all its ports, i.e. against
            # an equal-ish weighted large/mid-cap universe instead of the cap-weighted market
            series["lc_long_vs_ports"] = lc[s]["long"] - lc[s]["allavg"]
            series["lc_allports_x"] = (lc[s]["allavg"] - mkt.reindex(lc[s].index)).dropna()
        if s in vw:
            series["vw_long_x"] = (vw[s]["long"] - mkt.reindex(vw[s].index)).dropna()
        r = {"signal": s, "authors": meta["Authors"], "year": meta["Year"], "journal": meta["Journal"],
             "sample": f"{meta['SampleStartYear']}-{meta['SampleEndYear']}", "cat_data": meta["Cat.Data"],
             "cat_economic": meta["Cat.Economic"], "description": meta["LongDescription"],
             "op_weight": meta["Stock Weight"], "op_hold_months": meta["Portfolio Period"],
             "op_return_paper": meta["Return"], "op_t_paper": meta["T-Stat"],
             "pub_after_2014": bool(pd.notna(meta["Year"]) and int(meta["Year"]) >= 2015)}
        for name, x in series.items():
            for pk, (a, b) in per.items():
                m, t, n = tstat(window(x, a, b))
                r[f"{name}_{pk}_mean"], r[f"{name}_{pk}_t"], r[f"{name}_{pk}_n"] = m, t, n
        rows.append(r)
    df = pd.DataFrame(rows)
    key = "lc_long_x_post2015"
    df["p_primary"] = [one_sided_p(t) for t in df[f"{key}_t"]]
    eligible = df[f"{key}_n"].fillna(0) >= MIN_MONTHS
    df["pass_hurdle"] = eligible & (df[f"{key}_t"] >= HURDLE_T)
    bonf = NORM.inv_cdf(1 - 0.05 / len(df))
    df["pass_bonferroni"] = eligible & (df[f"{key}_t"] >= bonf)
    df["pass_bh5"] = bh_reject(np.where(eligible, df["p_primary"], np.nan), 0.05)
    df["still_works"] = df["pass_hurdle"] & (df["ls_op_postpub_mean"] > 0) & (df["vw_long_x_post2015_mean"] > 0)
    df["tier2"] = (~df["still_works"]) & eligible & (df[f"{key}_t"] >= TIER2_T) & \
        (df["ls_op_postpub_t"] >= TIER2_T) & (df["vw_long_x_post2015_mean"] > 0)
    return df.sort_values(f"{key}_t", ascending=False).reset_index(drop=True)


def summarize(df: pd.DataFrame) -> dict:
    n = len(df)
    k = "lc_long_x_post2015"
    elig = df[f"{k}_n"].fillna(0) >= MIN_MONTHS

    def frac_pos(col):
        x = df[col].dropna()
        return {"n": int(len(x)), "positive": int((x > 0).sum()), "median_mean_pct_month": float(x.median())}

    out = {
        "signals": n, "eligible_post2015_ge60m": int(elig.sum()),
        "hurdle": f"post-2015 large/mid-cap long-leg excess over the market, t >= {HURDLE_T}",
        "pass_hurdle_t3": int(df["pass_hurdle"].sum()),
        "bonferroni_t": NORM.inv_cdf(1 - 0.05 / n), "pass_bonferroni": int(df["pass_bonferroni"].sum()),
        "pass_bh_fdr5": int(df["pass_bh5"].sum()),
        "t_ge_2": int((elig & (df[f"{k}_t"] >= 2)).sum()),
        "expected_t_ge_2_by_luck": float(elig.sum() * (1 - NORM.cdf(2))),
        "t_le_minus2": int((elig & (df[f"{k}_t"] <= -2)).sum()),
        "still_works": df.loc[df["still_works"], "signal"].tolist(),
        "tier2": df.loc[df["tier2"], "signal"].tolist(),
        "decay": {
            "ls_op_is": frac_pos("ls_op_is_mean"), "ls_op_postpub": frac_pos("ls_op_postpub_mean"),
            "ls_op_post2015": frac_pos("ls_op_post2015_mean"), "lc_ls_post2015": frac_pos("lc_ls_post2015_mean"),
            "lc_long_x_post2015": frac_pos("lc_long_x_post2015_mean"),
            "vw_long_x_post2015": frac_pos("vw_long_x_post2015_mean"),
        },
        "ls_op_t_ge_2": {p: int((df[f"ls_op_{p}_t"] >= 2).sum()) for p in ("is", "postpub", "post2015")},
        "lc_ls_post2015_t_ge_2": int((df["lc_ls_post2015_t"] >= 2).sum()),
        "lc_ls_post2015_t_ge_3": int((df["lc_ls_post2015_t"] >= 3).sum()),
        "ls_op_post2015_t_ge_3": int((df["ls_op_post2015_t"] >= 3).sum()),
        "vw_long_x_post2015_t_ge_3": int((df["vw_long_x_post2015_t"] >= 3).sum()),
        "vw_long_x_post2015_t_ge_2": int((df["vw_long_x_post2015_t"] >= 2).sum()),
        "posthoc_lc_long_vs_ports_post2015_t_ge_2": int((df["lc_long_vs_ports_post2015_t"] >= 2).sum()),
        "posthoc_lc_long_vs_ports_post2015_t_ge_3": int((df["lc_long_vs_ports_post2015_t"] >= 3).sum()),
        "posthoc_median_lc_allports_minus_mkt_post2015": float(df["lc_allports_x_post2015_mean"].median()),
    }
    # average decay of the OP long-short, the standard McLean-Pontiff ratio (post-pub mean / in-sample mean)
    both = df[["ls_op_is_mean", "ls_op_postpub_mean", "ls_op_post2015_mean"]].dropna()
    out["mean_ls_op_pct_month"] = {c: float(both[c].mean()) for c in both}
    return out


def run(raw: Path = RAW, out: Path = OUT) -> tuple[pd.DataFrame, dict]:
    out.mkdir(parents=True, exist_ok=True)
    doc = load_doc(raw)
    mkt = load_market(raw)
    op, lc, vw = (legs(load_ports(raw / FILES[k])) for k in ("op", "lc", "vw"))
    bad = {k: check_ls_identity(v) for k, v in (("op", op), ("lc", lc), ("vw", vw))}
    df = screen(doc, op, lc, vw, mkt)
    summ = summarize(df)
    summ["ls_identity_violations"] = bad
    summ["data_end"] = {k: str(max(f.index.max() for f in v.values()).date()) for k, v in (("op", op), ("lc", lc), ("vw", vw))}
    summ["market_months"] = [str(mkt.index.min().date()), str(mkt.index.max().date())]
    df.to_csv(out / "screen_all.csv", index=False)
    (out / "screen_summary.json").write_text(json.dumps(summ, indent=2, default=str))
    return df, summ


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.parse_args(argv)
    df, s = run()
    cols = ["signal", "year", "cat_data", "lc_long_x_post2015_mean", "lc_long_x_post2015_t", "vw_long_x_post2015_t",
            "lc_ls_post2015_t", "ls_op_post2015_t", "ls_op_postpub_t", "ls_op_is_t", "still_works", "tier2"]
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(df[cols].head(30).round(2).to_string())
    print(json.dumps({k: v for k, v in s.items() if k not in ("decay",)}, indent=1, default=str))
    print(json.dumps(s["decay"], indent=1))


if __name__ == "__main__":
    sys.exit(main())
