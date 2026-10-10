"""Point-in-time fundamental factor zoo (pre-registered in docs/research_ledger_fundamentals.md).

40 financial-statement factors (value, profitability, growth, quality/accruals, investment, leverage/liquidity,
efficiency, size) from SEC XBRL companyfacts annual facts (10-K, usable only strictly after the filing date), plus
7 family composites and an all-family composite. Each is held as a monthly, long-only, equal-weight top-10 portfolio
in two universes (U300: the weekly top-300 dollar-volume list; UW: every eligible panel name with a point-in-time
market cap >= $300M, survivorship-biased, report only), $10k IBKR Tiered costs, signal at the last session of a month,
trade at the next session's close. Judged test: walk-forward selection (each January the factor / composite with the
best trailing-36-month Sharpe is held for the year), 2017-01..2026-08 vs ONEQ total return.
Factors, panels, targets, walk-forward and IC: quant.strategies.fundamentals.

Usage:
  PYTHONPATH=. .venv/bin/python scripts/research_fundamentals.py --check      # data pipeline checks (no returns)
  PYTHONPATH=. .venv/bin/python scripts/research_fundamentals.py --register   # freeze the pre-registration hash
  PYTHONPATH=. .venv/bin/python scripts/research_fundamentals.py --run        # run every configuration once
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

from quant.data import version as dv
from quant.evaluation.criteria import HALVES_AB_LABELS, ab_verdict_on, bh_reject_rank_share, bonferroni_t
from quant.evaluation.metrics import yearly
from quant.evaluation.periods import nav_window_metrics
from quant.evaluation.stats import one_sided_p_normal, t_sf
from quant.paths import CACHE_ROOT, ROOT
from quant.reporting import Preregistration, json_rounded
from quant.strategies import fundamentals as fs
from quant.strategies import megacap as mc
from quant.strategies.fundamentals import COMPOSITES, FACTORS, family_of

# ======================================================================== frozen configuration

OUT_V1 = ROOT / "output/research_only/fundamentals"
OUT = dv.versioned(OUT_V1)
LEDGER = ROOT / "docs/research_ledger_fundamentals.md"
FROZEN = OUT_V1 / "frozen_prereg.json"   # frozen rules are never versioned
PREREG = Preregistration(LEDGER, FROZEN, OUT)
prereg_block, prereg_hash, register = PREREG.block, PREREG.hash, PREREG.register
LISTED = dv.CACHE / "universe/weekly_listed.csv.gz"
FCACHE = dv.versioned(CACHE_ROOT / "fundamentals")      # derived caches of this study

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

# factor signs (section 1.4): +1 = higher is better
SIGN = {**fs.SIGN, **{f: -1 for f in ("accruals", "sloan", "earn_var", "noa", "asset_g", "capex_assets", "share_iss",
                                      "de", "nd_ebitda", "size")}}
RULES = FACTORS + COMPOSITES
UNIVERSES = ("U300", "UW")
assert len(FACTORS) == 40 and len(COMPOSITES) == 8
N_TESTS = len(RULES) * len(UNIVERSES)
N_WF = 4
MARKET_FACTORS = ("ep", "bm", "sp", "cfp", "ebit_ev", "fcf_yield", "payout_yield", "altman_z", "rd_mcap", "size")


def criteria(per: dict) -> dict:
    return ab_verdict_on([per[p] for p in JUDGED], per[FULL], HALVES_AB_LABELS)


def wf_criteria(m: dict) -> dict:
    a = m["cagr"] > m["oneq_cagr"] and m["t_monthly_excess_vs_oneq"] >= 2.0
    b = m["dd_shallower_than_oneq_pp"] >= 10.0 and m["cagr"] >= m["oneq_cagr"] - 0.03
    return {"A_cagr_above_oneq_and_t_ge_2": bool(a), "B_dd_10pp_shallower_and_cagr_within_3pp": bool(b),
            "pass": bool(a or b)}


def prepare():
    return fs.prepare(FIRST_SIGNAL, LISTED, FCACHE, SIGN)


# ======================================================================== commands

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


def simulate(data, tg, oneq_lvl, oneq_px):
    sim = mc.simulate(tg, data.sessions, data.perf_idx, data.close, data.last_row, data.qqq_perf_idx, data.qqq_close)
    dates = sim["nav"].index
    return sim, mc.buy_hold(oneq_lvl, oneq_px, dates, mc.ONEQ_HS), mc.buy_hold(data.qqq_perf_idx, data.qqq_close,
                                                                               dates, mc.QQQ_HS)


def year_rows(by_year: list, u: str, rule: str, nav, oneq, qqq) -> None:
    r = nav.pct_change().dropna()
    yo, yq = yearly(oneq.pct_change().dropna()), yearly(qqq.pct_change().dropna())
    for y, v in yearly(r).items():
        by_year.append({"universe": u, "rule": rule, "year": y, "strategy": v, "oneq": yo.get(y), "qqq": yq.get(y)})


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
            tg = fs.top_targets(p, rule, N_HOLD, SIGN)
            targets_all[(u, rule)] = tg
            sim, oneq, qqq = simulate(data, tg, oneq_lvl, oneq_px)
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
            year_rows(by_year, u, rule, sim["nav"], oneq, qqq)
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
    res["bh_pass"] = bh_reject_rank_share(res["p_one_sided"].fillna(1).to_numpy(), 0.05)
    res3 = res[res["universe"] == "U300"].copy()
    res3["bonferroni_pass_u300"] = res3["p_one_sided"] < 0.05 / len(RULES)
    res3["bh_pass_u300"] = bh_reject_rank_share(res3["p_one_sided"].fillna(1).to_numpy(), 0.05)
    res = res.merge(res3[["universe", "rule", "bonferroni_pass_u300", "bh_pass_u300"]], how="left",
                    on=["universe", "rule"])
    res["criterion_pass"] = res["criteria"].map(lambda c: c["pass"])
    mt = {"n_tests": N_TESTS, "bonferroni_t_one_sided_5pct_approx": bonferroni_t(N_TESTS),
          "nominal_t_ge_2": int((res["t_full"] >= 2).sum()),
          "expected_by_chance_t_ge_2": round(N_TESTS * one_sided_p_normal(2.0), 1),
          "bonferroni_pass": int(res["bonferroni_pass"].sum()), "bh_fdr5_pass": int(res["bh_pass"].sum()),
          "criterion_pass": int(res["criterion_pass"].sum()),
          "u300": {"n_tests": len(RULES), "nominal_t_ge_2": int((res3["t_full"] >= 2).sum()),
                   "bonferroni_pass": int(res3["bonferroni_pass_u300"].sum()),
                   "bh_fdr5_pass": int(res3["bh_pass_u300"].sum()),
                   "criterion_pass": int(res[(res["universe"] == "U300")]["criterion_pass"].sum())}}
    # ---- information coefficients (diagnostic)
    ic_df = pd.DataFrame(fs.ic_rows(panels, sigs, data.sig_idx, RULES, PERIODS, SIGN))
    # ---- walk-forward (judged)
    wf_res, wf_picks = {}, []
    for u in UNIVERSES:
        for sel, pool in (("WF-F", FACTORS), ("WF-C", COMPOSITES)):
            cand_navs = {r: navs[(u, r)] for r in pool}
            tg, picks = fs.walk_forward(cand_navs, {r: targets_all[(u, r)] for r in pool}, sigs, WF_FIRST_YEAR,
                                        WF_LOOKBACK_MONTHS, ACCOUNT)
            sim, oneq, qqq = simulate(data, tg, oneq_lvl, oneq_px)
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
            year_rows(by_year, u, sel, sim["nav"], oneq, qqq)
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
           "wf_bonferroni_t_2_trials": bonferroni_t(2),
           "qqq_buyhold_reference_criteria": qqq_ref, "first_signal": str(sigs[0].date()),
           "last_signal": str(sigs[-1].date()), "n_signals": len(sigs),
           "terminal_events": int(len(data.terminal_events))}
    (OUT / "results.json").write_text(json.dumps(out, indent=1, default=json_rounded) + "\n")
    print(json.dumps(mt, indent=1))
    print("VERDICT (U300 walk-forward):", "PASS" if verdict else "FAIL")
    return out


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
