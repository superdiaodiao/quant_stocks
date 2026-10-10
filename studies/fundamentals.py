"""Point-in-time fundamental factor zoo (pre-registered in docs/research_ledger_fundamentals.md).

40 financial-statement factors (value, profitability, growth, quality/accruals, investment, leverage/liquidity,
efficiency, size) from SEC XBRL companyfacts annual facts (10-K, usable only strictly after the filing date), plus
7 family composites and an all-family composite. Each is held as a monthly, long-only, equal-weight top-10 portfolio
in two universes (U300: the weekly top-300 dollar-volume list; UW: every eligible panel name with a point-in-time
market cap >= $300M, survivorship-biased, report only), $10k IBKR Tiered costs, signal at the last session of a month,
trade at the next session's close. Judged test: walk-forward selection (each January the factor / composite with the
best trailing-36-month Sharpe is held for the year), 2017-01..2026-08 vs ONEQ total return.

Usage:
  PYTHONPATH=. .venv/bin/python scripts/research_fundamentals.py --check      # data pipeline checks (no returns)
  PYTHONPATH=. .venv/bin/python scripts/research_fundamentals.py --register   # freeze the pre-registration hash
  PYTHONPATH=. .venv/bin/python scripts/research_fundamentals.py --run        # run every configuration once
"""
from __future__ import annotations

import argparse
import json
import math
from statistics import NormalDist

import numpy as np
import pandas as pd

from quant.data import market_cap as mcap
from quant.data import panel
from quant.data import version as dv
from quant.data.calendar import last_session_of_each_month
from quant.evaluation.criteria import HALVES_AB_LABELS, ab_verdict_on
from quant.evaluation.metrics import yearly
from quant.evaluation.periods import nav_window_metrics
from quant.evaluation.stats import t_sf  # noqa: F401  (rf.t_sf: tests)
from quant.prereg import Preregistration
from quant.paths import ROOT
from quant.strategies import canslim as cs
from quant.strategies import indicators as ind
from quant.strategies import megacap as mc
from quant.strategies.fundamentals import (  # noqa: F401  (rf.* names read by tests)
    ANNUAL_COLS, COMPOSITES, END, FACTORS, FAMILIES, FCACHE, PRICE_START, INT_COV_CAP, SECFACTS, SIGN, STOCK,
    _json, accounting_factors, asof_states, build_states, company_states, composites, extract_all, load_all,
    market_factors, pick, piotroski, reject_small_mcaps, sic_map, universes, year_inputs, zscores,
)

OUT_V1 = ROOT / "output/research_only/fundamentals"
OUT = dv.versioned(OUT_V1)
LEDGER = ROOT / "docs/research_ledger_fundamentals.md"
FROZEN = OUT_V1 / "frozen_prereg.json"   # frozen rules are never versioned
PREREG = Preregistration(LEDGER, FROZEN, OUT)
prereg_block, prereg_hash, register = PREREG.block, PREREG.hash, PREREG.register
LISTED = dv.CACHE / "universe/weekly_listed.csv.gz"

FIRST_SIGNAL = "2013-12-31"
PERIODS = {"H1 2014-2019": ("2014-01-01", "2019-12-31"),
           "H2 2020-2026-08": ("2020-01-01", "2026-08-31"),
           "Full 2014-2026-08": ("2014-01-01", "2026-08-31")}
JUDGED, FULL = ("H1 2014-2019", "H2 2020-2026-08"), "Full 2014-2026-08"
WF_PERIODS = {"WF 2017-2026-08": ("2017-01-01", "2026-08-31"), "WF 2017-2019": ("2017-01-01", "2019-12-31"),
              "WF 2020-2026-08": ("2020-01-01", "2026-08-31")}
WF_JUDGED = "WF 2017-2026-08"
WF_FIRST_YEAR, WF_LOOKBACK_MONTHS = 2017, 36
N_HOLD = 10
ACCOUNT = 10_000.0

# ======================================================================== factors (section 1.4)

SIGN.update({f: -1 for f in ("accruals", "sloan", "earn_var", "noa", "asset_g", "capex_assets", "share_iss", "de",
                             "nd_ebitda", "size")})
RULES = FACTORS + COMPOSITES
UNIVERSES = ("U300", "UW")
assert len(FACTORS) == 40 and len(COMPOSITES) == 8
N_TESTS = len(RULES) * len(UNIVERSES)
N_WF = 4
MARKET_FACTORS = ("ep", "bm", "sp", "cfp", "ebit_ev", "fcf_yield", "payout_yield", "altman_z", "rd_mcap", "size")


# ======================================================================== data: prices for every panel security


def candidate_rows(data: panel.WinData, signals: list) -> pd.DataFrame:
    """Every eligible panel security at each signal (latest week <= s): U300 rows from the top-300 file, plus
    eligible listed names from weekly_listed. Columns s, security_id, ticker, cik, dv50_rank, in300, sic."""
    uni = data.universe
    wl = pd.read_csv(LISTED, dtype=str, usecols=["week_end", "security_id", "ticker", "eligible", "dv50_rank"])
    wl = cs.truncate(wl, "week_end", "2012-01-01", END)
    wl = wl[(wl["eligible"] == "True") & wl["security_id"].isin(data.close.columns)].copy()
    wl["week_end"] = pd.to_datetime(wl["week_end"])
    wl["dv50_rank"] = pd.to_numeric(wl["dv50_rank"], errors="coerce")
    facts = pd.read_csv(SECFACTS, dtype=str, usecols=["security_id", "cik"]).set_index("security_id")["cik"]
    u3 = uni[uni["dv50_rank"].notna() & uni["security_id"].isin(data.close.columns)].copy()
    weeks3 = np.array(sorted(u3["week_end"].unique()), dtype="datetime64[ns]")
    weeksl = np.array(sorted(wl["week_end"].unique()), dtype="datetime64[ns]")
    by3 = {w: g for w, g in u3.groupby("week_end")}
    byl = {w: g for w, g in wl.groupby("week_end")}
    rows = []
    for s in signals:
        k3 = weeks3.searchsorted(np.datetime64(s), side="right") - 1
        kl = weeksl.searchsorted(np.datetime64(s), side="right") - 1
        a = by3[pd.Timestamp(weeks3[k3])][["security_id", "ticker", "cik", "dv50_rank", "sic", "multi_class_group"]] \
            .assign(in300=True) if k3 >= 0 else pd.DataFrame()
        b = byl[pd.Timestamp(weeksl[kl])][["security_id", "ticker", "dv50_rank"]].assign(in300=False) \
            if kl >= 0 else pd.DataFrame()
        b = b[~b["security_id"].isin(a["security_id"])] if len(a) else b
        g = pd.concat([a, b], ignore_index=True)
        g["s"] = s
        g["universe_week"] = pd.Timestamp(weeks3[k3]) if k3 >= 0 else pd.NaT
        rows.append(g)
    c = pd.concat(rows, ignore_index=True)
    c["cik"] = c["cik"].fillna(c["security_id"].map(facts)).fillna(c["security_id"].str.split(".").str[0])
    c["cik"] = pd.to_numeric(c["cik"], errors="coerce").astype("Int64")
    px = mcap.value_at(data.close, c["security_id"], c["s"])
    lr = pd.to_datetime(c["security_id"].map(data.last_row))
    c["close_s"] = px
    c = c[np.isfinite(px) & (lr.isna() | (lr >= c["s"])).to_numpy()].copy()
    cs.assert_window(c["universe_week"].dropna(), "2012-01-01", END, "universe weeks used")
    c["sic"] = pd.to_numeric(c["sic"], errors="coerce")
    miss = c["sic"].isna() & c["cik"].notna()
    smap = sic_map(c.loc[miss, "cik"].dropna().astype(int).unique())
    c.loc[miss, "sic"] = c.loc[miss, "cik"].map(lambda x: smap.get(int(x), np.nan) if pd.notna(x) else np.nan)
    return c.reset_index(drop=True)


# ======================================================================== targets, walk-forward

def top_targets(panel: pd.DataFrame, col: str, n: int = N_HOLD) -> dict:
    """signal -> [(sid, 1/n, dv50_rank, mcap, src)] for the n names with the best signed value (ties: security id).
    Signals with fewer than n rankable names are skipped (no rebalance)."""
    sign = SIGN.get(col, 1)
    out = {}
    v = panel[["s", "security_id", "dv50_rank", "mcap", col]].dropna(subset=[col])
    v = v.assign(key=v[col] * sign)
    for s, g in v.groupby("s"):
        if len(g) < n:
            continue
        g = g.sort_values(["key", "security_id"], ascending=[False, True]).head(n)
        out[s] = [(r.security_id, 1.0 / n, float(r.dv50_rank) if pd.notna(r.dv50_rank) else np.nan,
                   float(r.mcap) if pd.notna(r.mcap) else np.nan, "") for r in g.itertuples()]
    return out


def monthly_returns(nav: pd.Series, base: float = ACCOUNT) -> pd.Series:
    """Month-end-to-month-end returns of a daily NAV (month label = calendar month); the first month is measured
    from the starting capital ``base``."""
    me = nav.groupby(nav.index.to_period("M")).last()
    prev = me.shift(1)
    prev.iloc[0] = base
    return me / prev - 1


def trailing_sharpe(nav: pd.Series, asof: pd.Timestamp, months: int = WF_LOOKBACK_MONTHS) -> float:
    """Sharpe (mean / sd x sqrt 12) of the ``months`` monthly returns ending with the month of ``asof``, using
    only NAV values dated on or before ``asof``; NaN without a full window."""
    v = nav[nav.index <= asof]
    if len(v) == 0:
        return np.nan
    r = monthly_returns(v)
    last = pd.Timestamp(asof).to_period("M")
    r = r[r.index <= last].tail(months)
    if len(r) < months or r.index[-1] != last or r.std(ddof=1) == 0:
        return np.nan
    return float(r.mean() / r.std(ddof=1) * math.sqrt(12))


def walk_forward(navs: dict, targets: dict, signals: list, first_year: int = WF_FIRST_YEAR) -> tuple[dict, list]:
    """Each January Y: at the last signal of December Y-1 pick the candidate with the best trailing Sharpe
    (ties: name) and use its targets for that signal and the next 11. Returns (targets, picks)."""
    sigs = sorted(signals)
    out, picks = {}, []
    dec = [s for s in sigs if s.month == 12 and s.year >= first_year - 1]
    for s0 in dec:
        sh = {k: trailing_sharpe(nav, s0) for k, nav in navs.items()}
        sh = {k: v for k, v in sh.items() if np.isfinite(v)}
        if not sh:
            continue
        best = sorted(sh.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        year_sigs = [s for s in sigs if s >= s0][:12]
        for s in year_sigs:
            if s in targets[best[0]]:
                out[s] = targets[best[0]][s]
        top3 = sorted(sh.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        picks.append({"year": s0.year + 1, "selected_at": str(s0.date()), "pick": best[0], "trailing_sharpe": best[1],
                      "runner_up": top3[1][0] if len(top3) > 1 else None,
                      "third": top3[2][0] if len(top3) > 2 else None, "n_candidates": len(sh)})
    return out, picks


# ======================================================================== statistics


def bh_reject(p: np.ndarray, q: float = 0.05) -> np.ndarray:
    """Benjamini-Hochberg step-up: boolean mask of rejected hypotheses at FDR q."""
    p = np.asarray(p, float)
    n = len(p)
    order = np.argsort(p)
    ranked = p[order]
    ok = ranked <= q * (np.arange(1, n + 1) / n)
    rej = np.zeros(n, bool)
    if ok.any():
        k = np.max(np.nonzero(ok)[0])
        rej[order[:k + 1]] = True
    return rej


def ic_series(panel: pd.DataFrame, col: str, fwd: pd.Series) -> pd.Series:
    """Monthly Spearman rank correlation of the signed value with the next-month total return."""
    v = panel[["s", col]].assign(r=fwd.to_numpy()).dropna()
    out = {}
    for s, g in v.groupby("s"):
        if len(g) < 20:
            continue
        a = (g[col] * SIGN.get(col, 1)).rank()
        b = g["r"].rank()
        out[s] = float(np.corrcoef(a, b)[0, 1])
    return pd.Series(out, dtype=float)


def criteria(per: dict) -> dict:
    return ab_verdict_on([per[p] for p in JUDGED], per[FULL], HALVES_AB_LABELS)


def wf_criteria(m: dict) -> dict:
    a = m["cagr"] > m["oneq_cagr"] and m["t_monthly_excess_vs_oneq"] >= 2.0
    b = m["dd_shallower_than_oneq_pp"] >= 10.0 and m["cagr"] >= m["oneq_cagr"] - 0.03
    return {"A_cagr_above_oneq_and_t_ge_2": bool(a), "B_dd_10pp_shallower_and_cagr_within_3pp": bool(b),
            "pass": bool(a or b)}


# ======================================================================== pre-registration hash


# ======================================================================== pipeline

def prepare():
    data = load_all()
    print("DATE GUARD:", data.guard["assertion"])
    end = data.spec["effective_end"]
    sigs = [s for s in last_session_of_each_month(data.sessions) if s >= pd.Timestamp(FIRST_SIGNAL)]
    cand = candidate_rows(data, sigs)
    ciks = sorted(cand["cik"].dropna().astype(int).unique())
    alias = mcap.successor_ciks(data.universe)
    sh = mcap.extract_share_facts(sorted(set(ciks) | set(alias)), cache=FCACHE / "sec_share_facts.csv.gz")
    sh = mcap.add_predecessor_facts(sh, alias)
    lists = mcap.load_company_lists()
    dv50 = (data.close_adj * data.vol_adj).rolling(50, min_periods=20).median()
    # data rule 1.1a: shares x price is not replaced by the public float here
    capped = mcap.market_caps(cand[["s", "security_id", "ticker", "cik", "dv50_rank", "universe_week"]]
                              .assign(dv50_rank=cand["dv50_rank"].where(cand["in300"])), sh, lists, data.close,
                              data.sig_idx, dv50, shares_vs_float=np.inf)
    cand["mcap"] = capped["mcap"].to_numpy()
    cand["mcap_src"] = capped["mcap_src"].to_numpy()
    bad = cand["mcap_src"].astype(str).str.startswith("dollar_volume")
    cand.loc[bad, "mcap"] = np.nan
    cand["dv50"] = mcap.value_at(dv50, cand["security_id"], cand["s"])
    cand = reject_small_mcaps(cand)
    annual, eps = extract_all(ciks)
    annual = annual[annual["filed"].astype(str) <= end]
    eps = eps[eps["avail"].astype(str) <= end]
    states = build_states(annual)
    states = states[states["filed"].astype(str) <= end]
    cs.assert_window(states["filed"], None, end, "fundamental filings used")
    unis = universes(cand)
    panels = {}
    for u, rows in unis.items():
        m = asof_states(rows, states, eps)
        m = market_factors(m)
        m = m.sort_values(["s", "security_id"]).reset_index(drop=True)
        comp = composites(m)
        panels[u] = pd.concat([m, comp], axis=1)
    oneq_lvl, oneq_px = ind.oneq_on_sessions(data.sessions, PRICE_START, end)
    return data, sigs, cand, panels, oneq_lvl, oneq_px


def coverage(panels: dict) -> pd.DataFrame:
    rows = []
    for u, p in panels.items():
        for y, g in p.groupby(p["s"].dt.year):
            r = {"universe": u, "year": int(y), "avg_names": round(len(g) / g["s"].nunique(), 1)}
            for f in RULES:
                r[f] = round(float(g[f].notna().mean()), 3)
            rows.append(r)
    return pd.DataFrame(rows)


def spot_check(panels: dict) -> pd.DataFrame:
    """A few well-known companies' factor values at December signals (eyeball against public statements)."""
    p = panels["U300"]
    keep = p[p["ticker"].isin(["AAPL", "MSFT", "INTC", "COST", "AMGN", "CSCO"]) & (p["s"].dt.month == 12)]
    cols = ["s", "ticker", "fy0_end", "filed", "mcap"] + list(FACTORS)
    return keep[cols].sort_values(["ticker", "s"])


def check(args):
    OUT.mkdir(parents=True, exist_ok=True)
    data, sigs, cand, panels, *_ = prepare()
    cov = coverage(panels)
    cov.to_csv(OUT / "coverage.csv", index=False)
    sc = spot_check(panels)
    sc.to_csv(OUT / "spot_check.csv", index=False)
    pd.set_option("display.width", 250)
    print(cov[["universe", "year", "avg_names", "ep", "bm", "gpa", "roic", "sales_g3", "q_eps_g", "piotroski",
               "altman_z", "sloan", "share_iss", "int_cov", "rd_sales", "size", "C_ALL"]].to_string())
    print(sc[sc["s"].dt.year.isin([2016, 2019, 2023])][["s", "ticker", "fy0_end", "roe", "roa", "gpa", "gross_margin",
                                                          "ep", "bm", "sales_g1", "eps_g1", "piotroski",
                                                          "altman_z", "de", "share_iss"]].round(3).to_string())


def run(args):
    if not FROZEN.exists():
        raise SystemExit("run --register first")
    if json.loads(FROZEN.read_text())["sha256"] != prereg_hash():
        raise SystemExit("pre-registration text changed since --register")
    OUT.mkdir(parents=True, exist_ok=True)
    data, sigs, cand, panels, oneq_lvl, oneq_px = prepare()
    coverage(panels).to_csv(OUT / "coverage.csv", index=False)
    summary, by_year, holds, results = [], [], [], {}
    navs, nav_out, targets_all = {}, {}, {}
    qqq_ref = None
    for u in UNIVERSES:
        p = panels[u]
        for rule in RULES:
            tg = top_targets(p, rule)
            targets_all[(u, rule)] = tg
            sim = mc.simulate(tg, data.sessions, data.perf_idx, data.close, data.last_row, data.qqq_perf_idx,
                              data.qqq_close)
            dates = sim["nav"].index
            oneq = mc.buy_hold(oneq_lvl, oneq_px, dates, mc.ONEQ_HS)
            qqq = mc.buy_hold(data.qqq_perf_idx, data.qqq_close, dates, mc.QQQ_HS)
            per = {}
            for pname, (a, b) in PERIODS.items():
                m = nav_window_metrics(sim["nav"], oneq, qqq, a, b, sim["cost"], sim["traded"], sim["orders"])
                per[pname] = m
                summary.append({"universe": u, "rule": rule, "family": family_of(rule), "period": pname, **m})
            crit = criteria(per)
            months = per[FULL]["months"]
            t = per[FULL]["t_monthly_excess_vs_oneq"]
            results[f"{u}:{rule}"] = {"universe": u, "rule": rule, "family": family_of(rule), "criteria": crit,
                                      "start": str(sim["start"].date()), "t_full": t, "months": months,
                                      "p_one_sided": t_sf(t, months - 1), "avg_names_held": float(sim["names"].mean()),
                                      "signals_skipped": int(len([s for s in sigs if s not in tg]))}
            navs[(u, rule)] = sim["nav"]
            nav_out[f"{u}:{rule}"] = sim["nav"]
            r = sim["nav"].pct_change().dropna()
            yo, yq = yearly(oneq.pct_change().dropna()), yearly(qqq.pct_change().dropna())
            for y, v in yearly(r).items():
                by_year.append({"universe": u, "rule": rule, "year": y, "strategy": v, "oneq": yo.get(y),
                                "qqq": yq.get(y)})
            for s, lst in tg.items():
                for k, (sid, w, rk, mcap_, _) in enumerate(lst, 1):
                    holds.append({"universe": u, "rule": rule, "signal": s.date().isoformat(), "slot": k,
                                  "security_id": sid})
            if qqq_ref is None:
                nav_out["ONEQ"], nav_out["QQQ"] = oneq, qqq
                qper = {pn: nav_window_metrics(qqq, oneq, qqq, a, b) for pn, (a, b) in PERIODS.items()}
                for pn, m in qper.items():
                    summary.append({"universe": "-", "rule": "QQQ buy-hold", "family": "benchmark", "period": pn, **m})
                    summary.append({"universe": "-", "rule": "ONEQ buy-hold", "family": "benchmark", "period": pn,
                                    **nav_window_metrics(oneq, oneq, qqq, *PERIODS[pn])})
                qqq_ref = criteria(qper)
            print(f"{u:4s} {rule:14s} H1 {per[JUDGED[0]]['cagr']:+.1%} H2 {per[JUDGED[1]]['cagr']:+.1%} "
                  f"(ONEQ {per[JUDGED[0]]['oneq_cagr']:+.1%} / {per[JUDGED[1]]['oneq_cagr']:+.1%}) t {t:+.2f} "
                  f"pass {crit['pass']}", flush=True)
    # ---- multiple testing over the 96 tests
    res = pd.DataFrame(results.values())
    res["bonferroni_pass"] = res["p_one_sided"] < 0.05 / N_TESTS
    res["bh_pass"] = bh_reject(res["p_one_sided"].fillna(1).to_numpy(), 0.05)
    res3 = res[res["universe"] == "U300"].copy()
    res3["bonferroni_pass_u300"] = res3["p_one_sided"] < 0.05 / len(RULES)
    res3["bh_pass_u300"] = bh_reject(res3["p_one_sided"].fillna(1).to_numpy(), 0.05)
    res = res.merge(res3[["universe", "rule", "bonferroni_pass_u300", "bh_pass_u300"]], how="left",
                    on=["universe", "rule"])
    res["criterion_pass"] = res["criteria"].map(lambda c: c["pass"])
    mt = {"n_tests": N_TESTS, "bonferroni_t_one_sided_5pct_approx": NormalDist().inv_cdf(1 - 0.05 / N_TESTS),
          "nominal_t_ge_2": int((res["t_full"] >= 2).sum()),
          "expected_by_chance_t_ge_2": round(N_TESTS * (1 - NormalDist().cdf(2.0)), 1),
          "bonferroni_pass": int(res["bonferroni_pass"].sum()), "bh_fdr5_pass": int(res["bh_pass"].sum()),
          "criterion_pass": int(res["criterion_pass"].sum()),
          "u300": {"n_tests": len(RULES), "nominal_t_ge_2": int((res3["t_full"] >= 2).sum()),
                   "bonferroni_pass": int(res3["bonferroni_pass_u300"].sum()),
                   "bh_fdr5_pass": int(res3["bh_pass_u300"].sum()),
                   "criterion_pass": int(res[(res["universe"] == "U300")]["criterion_pass"].sum())}}
    # ---- information coefficients (diagnostic)
    ic_rows = []
    for u in UNIVERSES:
        p = panels[u]
        nxt = {s: n for s, n in zip(sigs[:-1], sigs[1:])}
        s_next = p["s"].map(nxt)
        i0 = mcap.value_at(data.sig_idx, p["security_id"], p["s"])
        i1 = mcap.value_at(data.sig_idx, p["security_id"], s_next.fillna(p["s"]))
        fwd = pd.Series(np.where(s_next.notna(), i1 / i0 - 1, np.nan), index=p.index)
        for rule in RULES:
            ic = ic_series(p, rule, fwd)
            for pname, (a, b) in PERIODS.items():
                x = ic[(ic.index >= pd.Timestamp(a) - pd.Timedelta(days=5)) & (ic.index <= pd.Timestamp(b))]
                ic_rows.append({"universe": u, "rule": rule, "family": family_of(rule), "period": pname,
                                "months": len(x), "mean_ic": float(x.mean()) if len(x) else np.nan,
                                "t": float(x.mean() / x.std(ddof=1) * math.sqrt(len(x))) if len(x) > 2 else np.nan,
                                "share_positive": float((x > 0).mean()) if len(x) else np.nan})
    ic_df = pd.DataFrame(ic_rows)
    # ---- walk-forward (judged)
    wf_res, wf_picks = {}, []
    for u in UNIVERSES:
        for sel, pool in (("WF-F", FACTORS), ("WF-C", COMPOSITES)):
            cand_navs = {r: navs[(u, r)] for r in pool}
            tg, picks = walk_forward(cand_navs, {r: targets_all[(u, r)] for r in pool}, sigs)
            sim = mc.simulate(tg, data.sessions, data.perf_idx, data.close, data.last_row, data.qqq_perf_idx,
                              data.qqq_close)
            dates = sim["nav"].index
            oneq = mc.buy_hold(oneq_lvl, oneq_px, dates, mc.ONEQ_HS)
            qqq = mc.buy_hold(data.qqq_perf_idx, data.qqq_close, dates, mc.QQQ_HS)
            per = {pn: nav_window_metrics(sim["nav"], oneq, qqq, a, b, sim["cost"], sim["traded"], sim["orders"])
                   for pn, (a, b) in WF_PERIODS.items()}
            for pn, m in per.items():
                summary.append({"universe": u, "rule": sel, "family": "walk_forward", "period": pn, **m})
            crit = wf_criteria(per[WF_JUDGED])
            key = f"{u}:{sel}"
            wf_res[key] = {"universe": u, "selector": sel, "criteria": crit, "start": str(sim["start"].date()),
                           "periods": per, "judged": u == "U300",
                           "p_one_sided": t_sf(per[WF_JUDGED]["t_monthly_excess_vs_oneq"],
                                               per[WF_JUDGED]["months"] - 1)}
            for pk in picks:
                wf_picks.append({"universe": u, "selector": sel, **pk})
            nav_out[key] = sim["nav"]
            r = sim["nav"].pct_change().dropna()
            yo, yq = yearly(oneq.pct_change().dropna()), yearly(qqq.pct_change().dropna())
            for y, v in yearly(r).items():
                by_year.append({"universe": u, "rule": sel, "year": y, "strategy": v, "oneq": yo.get(y),
                                "qqq": yq.get(y)})
            m = per[WF_JUDGED]
            print(f"{key}: CAGR {m['cagr']:+.1%} ONEQ {m['oneq_cagr']:+.1%} t {m['t_monthly_excess_vs_oneq']:+.2f} "
                  f"DD {m['max_dd']:.0%} (ONEQ {m['oneq_max_dd']:.0%}) pass {crit['pass']}", flush=True)
    verdict = any(v["criteria"]["pass"] for v in wf_res.values() if v["judged"])
    # ---- write
    pd.DataFrame(summary).to_csv(OUT / "summary.csv", index=False)
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", index=False)
    res.drop(columns=["criteria"]).assign(**{
        "A": res["criteria"].map(lambda c: c["A_cagr_above_oneq_both_halves_and_full_t_ge_2"]),
        "B": res["criteria"].map(lambda c: c["B_dd_10pp_shallower_and_cagr_within_3pp_both_halves"])}) \
        .to_csv(OUT / "tests.csv", index=False)
    ic_df.to_csv(OUT / "ic.csv", index=False)
    pd.DataFrame(wf_picks).to_csv(OUT / "wf_picks.csv", index=False)
    pd.DataFrame(holds).to_csv(OUT / "holdings_by_signal.csv.gz", index=False, compression="gzip")
    pd.DataFrame(nav_out).to_csv(OUT / "nav_daily.csv.gz", compression="gzip")
    out = {"prereg_sha256": prereg_hash(), "guard": data.guard["assertion"], "n_tests": N_TESTS, "n_wf": N_WF,
           "multiple_testing": mt, "walk_forward": wf_res, "verdict_pass": verdict,
           "wf_bonferroni_t_2_trials": NormalDist().inv_cdf(1 - 0.05 / 2),
           "qqq_buyhold_reference_criteria": qqq_ref, "first_signal": str(sigs[0].date()),
           "last_signal": str(sigs[-1].date()), "n_signals": len(sigs),
           "terminal_events": int(len(data.terminal_events))}
    (OUT / "results.json").write_text(json.dumps(out, indent=1, default=_json) + "\n")
    print(json.dumps(mt, indent=1))
    print("VERDICT (U300 walk-forward):", "PASS" if verdict else "FAIL")
    return out


def family_of(rule: str) -> str:
    if rule.startswith("C_"):
        return "composite"
    return next(k for k, v in FAMILIES.items() if rule in v)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--register", action="store_true")
    g.add_argument("--run", action="store_true")
    a = ap.parse_args(argv)
    if a.register:
        register()
    elif a.check:
        check(a)
    else:
        run(a)


if __name__ == "__main__":
    main()
