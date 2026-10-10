"""Short-loser overlay on QQQ (pre-registered in docs/research_ledger_short_overlay.md, section 0).

A margin account holds QQQ (100% of equity, or 100% + k for the market-neutral variant) and shorts the N worst names
of a monthly "loser" ranking in the data-v2 U300 universe, total short notional k of equity. Four loser signals
(GBRT walk-forward scores of research_ml_cross_section, the profitability composite, a Stambaugh-Yu-Yuan style
mispricing composite, 12-1 momentum). Daily simulation with whole-share shorts, IBKR Tiered commissions and fees,
half-spreads, tiered borrow fees, IBKR margin-loan and credit-interest rules, Reg T initial / maintenance margin with
forced liquidation, and terminal values for delisted shorts. Judged against ONEQ total return on two folds.

Usage:
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_short_overlay.py --check
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_short_overlay.py --register
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_short_overlay.py --run
"""
from __future__ import annotations

import os

os.environ.setdefault("REVERSAL_DATA_VERSION", "v2")      # the study is defined on frozen data v2 only

import argparse  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
from dataclasses import replace  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant.evaluation.metrics import max_drawdown  # noqa: E402
from quant.evaluation.stats import t_sf  # noqa: E402
from quant.prereg import Preregistration  # noqa: E402
from quant.paths import ROOT  # noqa: E402
from quant.strategies.short_overlay import (  # noqa: E402,F401  (so.* names read by tests)
    ACCOUNT, ALPHA_ONE_SIDED, BAND, BIG_RANK, BORROW_BIG, BORROW_HTB, BORROW_REST, BUFFER_MULT, CREDIT_FREE,
    CREDIT_SPREAD, Cfg, DAYCOUNT, EFFR_CSV, FOLDS, FULL_RATE_NAV, HTB_VOL_Q, INIT_CAP, KS, LIQ_SLIP, MAINT_LONG,
    MARGIN_SPREAD, MISP_MIN, MISP_PARTS, MODES, Market, NS, N_TRIALS, QQQ_BAND, QQQ_HALF_SPREAD, REGT_INIT,
    SIGNALS, basket_diagnostics, bonferroni_t, borrow_rates, build_market, daily_interest, fold_bounds, folds_for,
    leg_summary, load_effr, loser_orders, misp_score, month_returns, ols_alpha, pct_rank, schedule, select_shorts,
    short_maint, signal_scores, simulate, terminal_variant, tstat,
)

OUT = ROOT / "output/research_only/short_overlay"
LEDGER = ROOT / "docs/research_ledger_short_overlay.md"
FROZEN = OUT / "frozen_prereg.json"
PREREG = Preregistration(LEDGER, FROZEN, OUT)
prereg_block, prereg_hash, register = PREREG.block, PREREG.hash, PREREG.register
PRED = ROOT / "output/research_only/ml_cross_section/predictions.csv.gz"


SENSITIVITIES = {
    "borrow_x2": {"borrow_mult": 2.0},
    "d5_minus100": {"idx": "d5_minus100"},
    "delist_0pct": {"idx": "delist_0pct"},
    "spread_x2": {"spread_mult": 2.0},
    "account_100k": {"account": 100_000.0},
    "account_1m": {"account": 1_000_000.0},
    "ideal": {"ideal": True},
}


# ======================================================================== metrics


def fold_criteria(m: dict) -> dict:
    a = (m["cagr"] > m["oneq_cagr"]) and (m["t_ex_oneq"] >= 2.0)
    b = (m["dd_shallower_pp"] >= 10.0) and (m["cagr"] - m["oneq_cagr"] >= -0.03)
    return {"A": bool(a), "B": bool(b), "pass": bool(a or b)}


def fold_metrics(res: dict, oneq: pd.Series, qqq_lvl: pd.Series, account: float) -> dict:
    nav = res["nav"]["nav"]
    v = nav.where(nav > 0)
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    o = oneq.reindex(nav.index)
    q = qqq_lvl.reindex(nav.index)
    cagr = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1 if nav.iloc[-1] > 0 else -1.0
    oc = (o.iloc[-1] / o.iloc[0]) ** (1 / years) - 1
    qc = (q.iloc[-1] / q.iloc[0]) ** (1 / years) - 1
    mr, mo, mq = month_returns(nav), month_returns(o), month_returns(q)
    ex, exq = mr - mo, mr - mq
    dd, odd = max_drawdown(v.ffill().fillna(0)), max_drawdown(o)
    r, rb = nav.pct_change().iloc[1:], o.pct_change().iloc[1:]
    beta = float(np.cov(r, rb, ddof=1)[0, 1] / rb.var(ddof=1))
    acc = res["acc"]
    avg_nav = float(nav.mean())
    short = res["nav"]["short"]
    m = {"start": str(nav.index[0].date()), "end": str(nav.index[-1].date()), "years": round(years, 2),
         "cagr": cagr, "oneq_cagr": oc, "qqq_cagr": qc, "excess_vs_oneq": cagr - oc, "excess_vs_qqq": cagr - qc,
         "t_ex_oneq": tstat(ex), "t_ex_qqq": tstat(exq), "ex_qqq_ann": float(exq.mean() * 12),
         "max_dd": dd, "oneq_max_dd": odd, "qqq_max_dd": max_drawdown(q), "dd_shallower_pp": (abs(odd) - abs(dd)) * 100,
         "beta_vs_oneq": beta, "months": int(len(ex)),
         "borrow_pct_yr": acc["borrow"] / avg_nav / years, "margin_int_pct_yr": acc["margin_int"] / avg_nav / years,
         "credit_int_pct_yr": acc["credit_int"] / avg_nav / years,
         "trade_cost_pct_yr": (acc["commission"] + acc["fees"] + acc["spread"]) / avg_nav / years,
         "short_turnover_yr": (acc["short_traded"] / 2) / max(float(short.mean()), 1e-9) / years,
         "short_exposure": float((short / nav.where(nav > 0)).mean()),
         "avg_n_short": float(res["nav"]["n_short"].mean()),
         "orders_per_month": acc["orders"] / max(len(ex), 1),
         "liquidations": acc["liquidations"], "zero_share_skips": acc["zero_share_skips"],
         "delisted_covered": acc["delisted_covered"], "scaled_rebalances": acc["scaled_rebalances"],
         "dead": acc["dead"], "final_nav": float(nav.iloc[-1]), "start_nav": account}
    m.update(fold_criteria(m))
    return m, ex


# ======================================================================== data loading

def load():
    from quant.strategies import ml_features as ml
    from quant.data import version as dv
    if not dv.IS_V2:
        raise SystemExit("set REVERSAL_DATA_VERSION=v2")
    data, sigs, panel, oneq_lvl, _ = ml.build_panel()
    pred = pd.read_csv(PRED, dtype={"security_id": str}, parse_dates=["s"])
    panel = panel.copy()
    panel["security_id"] = panel["security_id"].astype(str)
    end = data.spec["effective_end"]
    qtr = ml.qqq_total_returns(end).reindex(data.sessions).fillna(0.0)
    qqq_lvl = (1 + qtr).cumprod()
    effr = load_effr()
    effr = effr[effr.index <= pd.Timestamp(end)]
    scores = signal_scores(panel, pred)
    mk = build_market(data, list(scores["security_id"].unique()), effr, qtr)
    for name, s in (("sessions", pd.Series(data.sessions)), ("effr", pd.Series(effr.index))):
        if pd.Series(s).max() > pd.Timestamp(end):
            raise RuntimeError(f"{name} beyond {end}")
    return data, sigs, panel, scores, mk, oneq_lvl, qqq_lvl


def coverage(scores: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for y, g in scores.groupby(scores["s"].dt.year):
        r = {"year": int(y), "avg_u300": round(len(g) / g["s"].nunique(), 1)}
        for c in SIGNALS:
            r[c] = round(float(g[c].notna().mean()), 3)
        for v, lab in ((BORROW_BIG, "borrow_0.5"), (BORROW_REST, "borrow_2"), (BORROW_HTB, "borrow_10")):
            r[lab] = round(float((g["borrow"] == v).mean()), 3)
        rows.append(r)
    return pd.DataFrame(rows)


# ======================================================================== registration


def check(args):
    OUT.mkdir(parents=True, exist_ok=True)
    data, sigs, panel, scores, mk, oneq, qqq = load()
    cov = coverage(scores)
    cov.to_csv(OUT / "check_coverage.csv", index=False)
    pd.set_option("display.width", 250)
    print(cov.to_string())
    for sig in SIGNALS:
        sched = schedule(mk, scores, sig, 20)
        print(sig, "signals", len(sched), "first trade", mk.sessions[sched[0][0]].date(),
              "last trade", mk.sessions[sched[-1][0]].date())
    # where do the worst names sit: average borrow tier of the worst 20 (no returns)
    for sig in SIGNALS:
        w = scores.dropna(subset=[sig]).sort_values(["s", sig]).groupby("s").head(20)
        print(sig, "worst-20 borrow tier mix", w["borrow"].value_counts(normalize=True).round(3).to_dict(),
              "median dv50 rank", float(w["dv50_rank"].median()))
    print("EFFR range", float(mk.effr.min()), float(mk.effr.max()), "terminal events in market", len(mk.booked))


# ======================================================================== run

def run(args):
    if not FROZEN.exists():
        raise SystemExit("not registered")
    if json.loads(FROZEN.read_text())["sha256"] != prereg_hash():
        raise SystemExit("pre-registration text changed since --register")
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    data, sigs, panel, scores, mk, oneq, qqq = load()
    log = []

    def say(*a):
        msg = " ".join(str(x) for x in a)
        print(msg, flush=True)
        log.append(msg)

    say("DATE GUARD:", data.guard["assertion"])
    rows, months_ex, nav_out = [], {}, []
    scheds = {(sig, n): schedule(mk, scores, sig, n) for sig in SIGNALS for n in NS}
    variants = [("main", {})] + list(SENSITIVITIES.items())
    for sig in SIGNALS:
        for n in NS:
            sched = scheds[(sig, n)]
            for k in KS:
                for mode in MODES:
                    for vname, mods in variants:
                        cfg = replace(Cfg(signal=sig, n=n, k=k, mode=mode), **mods)
                        for fold, (a, b) in folds_for(sig).items():
                            start, end, entries = fold_bounds(mk, sched, a, b)
                            res = simulate(mk, entries, cfg, start, end)
                            m, ex = fold_metrics(res, oneq, qqq, cfg.account)
                            rows.append({"config": cfg.name, "signal": sig, "n": n, "k": k, "mode": mode,
                                         "variant": vname, "fold": fold, **m})
                            if vname == "main":
                                months_ex[(cfg.name, fold)] = ex
                                nv = res["nav"][["nav", "short", "n_short"]].copy()
                                nv["config"], nv["fold"] = cfg.name, fold
                                nav_out.append(nv.reset_index())
                say(f"{sig} N{n}: done ({time.time() - t0:.0f}s)")
    summ = pd.DataFrame(rows)
    summ.to_csv(OUT / "summary.csv", index=False)
    pd.concat(nav_out, ignore_index=True).to_csv(OUT / "nav_daily.csv.gz", index=False)

    # ---- judgement
    main = summ[summ["variant"] == "main"]
    verdict = []
    for cfgname, g in main.groupby("config", sort=False):
        ex = pd.concat([months_ex[(cfgname, f)] for f in ("H1", "H2")])
        t_all = tstat(ex)
        hurdle = bonferroni_t(len(ex) - 1)
        h1, h2 = g[g["fold"] == "H1"].iloc[0], g[g["fold"] == "H2"].iloc[0]
        both = bool(h1["pass"] and h2["pass"])
        verdict.append({"config": cfgname, "H1_pass": bool(h1["pass"]), "H2_pass": bool(h2["pass"]),
                        "H1_A": bool(h1["A"]), "H1_B": bool(h1["B"]), "H2_A": bool(h2["A"]), "H2_B": bool(h2["B"]),
                        "t_pooled_vs_oneq": t_all, "p_one_sided": t_sf(t_all, len(ex) - 1),
                        "bonferroni_t": hurdle, "nominal_pass": both, "pass": bool(both and t_all >= hurdle)})
    verdict = pd.DataFrame(verdict)
    verdict.to_csv(OUT / "verdict.csv", index=False)

    # ---- diagnostics: the loser basket alone
    legs, squeeze, names, feas = [], [], [], []
    tick = scores.drop_duplicates(["s", "security_id"]).set_index(["s", "security_id"])["ticker"]
    for sig in SIGNALS:
        for n in NS:
            sched = scheds[(sig, n)]
            parts = []
            for fold, (a, b) in folds_for(sig).items():
                start, end, _ = fold_bounds(mk, sched, a, b)
                bd = basket_diagnostics(mk, sched, n, start, end)
                bd["fold"] = fold
                parts.append(bd)
                legs.append({"signal": sig, "n": n, "fold": fold, **leg_summary(bd)})
            allb = pd.concat(parts, ignore_index=True)
            legs.append({"signal": sig, "n": n, "fold": "ALL", **leg_summary(allb)})
            if n == 20:
                d = allb.assign(rel=allb["basket"] - allb["qqq"]).sort_values("rel", ascending=False).head(5)
                for r in d.itertuples():
                    top = sorted(r.names.items(), key=lambda kv: -kv[1] if np.isfinite(kv[1]) else 0)[:3]
                    squeeze.append({"signal": sig, "month_start": str(r.t0.date()), "month_end": str(r.t1.date()),
                                    "basket": r.basket, "qqq": r.qqq, "basket_minus_qqq": r.rel,
                                    "top_names": "; ".join(f"{tick.get((r.signal, s_), s_)} {v:+.0%}" for s_, v in top)})
                for r in allb.itertuples():
                    for s_, v in r.names.items():
                        names.append({"signal": sig, "t0": str(r.t0.date()), "t1": str(r.t1.date()),
                                      "security_id": s_, "ret": v, "s": r.signal})
            pr = np.concatenate([np.asarray(p, float) for p in allb["prices"]]) if len(allb) else np.array([])
            p50, p90 = float(np.nanmedian(pr)), float(np.nanpercentile(pr, 90))
            for k in KS:
                per_name = k * ACCOUNT / n
                need = max(5 * p90, 350.0) * n / k
                x = per_name / pr
                rnd = np.round(x)
                err = np.abs(rnd - x)[x >= 0.5] / x[x >= 0.5]
                feas.append({"signal": sig, "n": n, "k": k, "target_per_short_at_10k": per_name,
                             "median_short_price": p50, "p90_short_price": p90,
                             "share_rounding_to_zero_at_10k": float(np.mean(x < 0.5)),
                             "mean_abs_rounding_error_at_10k": float(np.mean(err)) if len(err) else np.nan,
                             "min_commission_bp_at_10k": 0.35 / per_name * 1e4, "min_account_needed": need})
    legs = pd.DataFrame(legs)
    legs.to_csv(OUT / "short_leg.csv", index=False)
    pd.DataFrame(squeeze).to_csv(OUT / "worst_squeeze_months.csv", index=False)
    nm = pd.DataFrame(names)
    if len(nm):
        nm["ticker"] = [tick.get((s_, i), i) for s_, i in zip(nm["s"], nm["security_id"])]
        nm = nm.drop(columns=["s"]).sort_values("ret", ascending=False)
        nm.drop_duplicates(["t0", "security_id"]).head(30).to_csv(OUT / "worst_single_name_squeezes.csv", index=False)
    pd.DataFrame(feas).to_csv(OUT / "feasibility.csv", index=False)

    # ---- benchmarks per fold
    bench = []
    for key, fs in FOLDS.items():
        for fold, (a, b) in fs.items():
            o = oneq[(oneq.index >= pd.Timestamp(a)) & (oneq.index <= pd.Timestamp(b))]
            q = qqq[(qqq.index >= pd.Timestamp(a)) & (qqq.index <= pd.Timestamp(b))]
            yrs = (o.index[-1] - o.index[0]).days / 365.25
            bench.append({"folds": key, "fold": fold, "oneq_cagr": (o.iloc[-1] / o.iloc[0]) ** (1 / yrs) - 1,
                          "qqq_cagr": (q.iloc[-1] / q.iloc[0]) ** (1 / yrs) - 1,
                          "oneq_max_dd": max_drawdown(o), "qqq_max_dd": max_drawdown(q)})
    pd.DataFrame(bench).to_csv(OUT / "benchmarks.csv", index=False)
    res = {"prereg_sha256": prereg_hash(), "n_trials": N_TRIALS, "verdict": "PASS" if verdict["pass"].any() else "FAIL",
           "n_pass": int(verdict["pass"].sum()), "n_nominal_pass": int(verdict["nominal_pass"].sum()),
           "bonferroni_t_typical": float(verdict["bonferroni_t"].median()),
           "guard": data.guard["assertion"], "runtime_s": round(time.time() - t0, 1)}
    (OUT / "results.json").write_text(json.dumps(res, indent=1, default=str) + "\n")
    say(json.dumps(res, default=str))
    (OUT / "run_log.txt").write_text("\n".join(log) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--register", action="store_true")
    g.add_argument("--run", action="store_true")
    args = ap.parse_args(argv)
    if args.check:
        check(args)
    elif args.register:
        register()
    else:
        run(args)


if __name__ == "__main__":
    main()
