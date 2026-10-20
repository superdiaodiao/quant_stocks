"""Buy a dipping mega-cap with a 2x daily-reset long, take profit (pre-registered in docs/research_ledger_dip_2x.md).

Rules (section 0.3): each month's point-in-time Nasdaq top 10 by market cap; a name that dips at the close of t
(T1 one-day total return <= -5%; T2 <= 0.9 x SMA20; T3 RSI(2) <= 5 above the SMA200) is bought at the close of t+1
with 20% of equity, at most 5 at a time, and sold at the next close after its return reaches +5% / +10%, falls to
-15% (stop variant) or at the close of the 20th session. Instruments (0.4): I2 a synthetic single-stock 2x ETF
(2r - (T-bill + 5.6%)/252 - 1.0%/252, calibrated on 8 real funds), I1 the stock at 1x, IM 2x on Reg T margin
(EFFR + 1.5%). Idle money in T-bills or QQQ. 72 configurations (0.5), judged against ONEQ on two folds of the P10
sample (data v2, 2012-01-31 .. 2026-08-31) and against QQQ on the 1999-2013 OOS sample (0.6); U18 reported only.
Engine, samples and the synthetic check: quant.strategies.dip_2x; real 2x ETF data: quant.data.leveraged_etfs.
``--posthoc`` writes the report-only checks added after the two reviews of 2026-10-21 (posthoc_*.csv,
posthoc.json): stop-loss fills, the matched leverage twin, constant-exposure holds of the same lists, a
financing-fair matched-exposure hold, the IM margin cushion and stricter maintenance levels, dropping the best
names / trades, correlated-trial hurdles and the H_match pattern by trigger. None of them changes a verdict.

Usage:
  PYTHONPATH=. .venv/bin/python -m quant study dip_2x --check-data [--fetch]   (data only, no strategy results)
  PYTHONPATH=. .venv/bin/python -m quant study dip_2x --register
  PYTHONPATH=. .venv/bin/python -m quant study dip_2x --run
  PYTHONPATH=. .venv/bin/python -m quant study dip_2x --posthoc          (after --run; report only)
"""
from __future__ import annotations

import os

os.environ.setdefault("REVERSAL_DATA_VERSION", "v2")      # P10 is registered on frozen data v2

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
from dataclasses import replace  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant.data import leveraged_etfs as le  # noqa: E402
from quant.data import version as dv  # noqa: E402
from quant.data.calendar import last_session_of_each_month  # noqa: E402
from quant.data.rates import daily_rf  # noqa: E402
from quant.data.sources.http import Ledger  # noqa: E402
from quant.evaluation.criteria import (ab_criteria, bonferroni_t, bootstrap_max_t, effective_trials,  # noqa: E402
                                       sidak_t)
from quant.evaluation.equity import worst_window  # noqa: E402
from quant.evaluation.metrics import max_drawdown, monthly  # noqa: E402
from quant.evaluation.periods import longest_underwater_sessions, nav_window_metrics  # noqa: E402
from quant.paths import CACHE_ROOT, MAIN_CHECKOUT, ROOT  # noqa: E402
from quant.reporting import Preregistration, RunLog, json_rounded  # noqa: E402
from quant.strategies import dip_2x as dx  # noqa: E402
from quant.strategies import megacap as mc  # noqa: E402
from quant.strategies import registry  # noqa: E402
from quant.strategies import selective_t as st  # noqa: E402

# ======================================================================== frozen configuration

OUT = ROOT / "output/research_only/dip_2x"
LEDGER = ROOT / "docs/research_ledger_dip_2x.md"
FROZEN = OUT / "prereg_frozen.json"
PREREG = Preregistration(LEDGER, FROZEN, OUT)
LOCAL = CACHE_ROOT / "dip_2x"                       # vendor data (never committed)
RAW = LOCAL / "raw"
REQUESTS = Ledger(LOCAL / "raw_index.csv.gz", LOCAL / "quota_ledger.csv")
ENV_ALPACA = MAIN_CHECKOUT / ".env.alpaca"
TOP20 = ROOT / "output/research_only/megacap_v2/top20_by_month.csv"
TOP20_SHA256 = "c22c2ed9bb8b2f7d31546af0262e9e6cd2d4b428d529484adb19deb83fd18148"
OOS_RANKS = ROOT / "output/research_only/megacap_oos2/ranks_top20_by_month.csv"
EFFR_RAW = CACHE_ROOT / "leverage_methods/raw"
U18_RAW = CACHE_ROOT / "selective_t/raw"
U18_QQQ = CACHE_ROOT / "calendar/raw/chart_QQQ.json"
ETF_END = "2026-09-30"
SPREAD_SAMPLE = ("2024-06-01", "2026-09-30")        # month-end sessions for the closing-quote sample

WINDOW = {"perf_start": "2012-01-01", "perf_end": "2026-08-31", "price_start": "2011-06-01",
          "universe_start": "2012-01-01", "judged_from": "2012-02-01"}
P10_FOLDS = {"H1": ("2012-02-01", "2018-12-31"), "H2": ("2019-01-01", "2026-08-31"), "Full": ("2012-02-01", "2026-08-31")}
OOS_FIRST = "1999-03-10"
OOS_FOLDS = {"O1": ("1999-03-11", "2006-06-30"), "O2": ("2006-07-01", "2013-12-31"), "Full": ("1999-03-11", "2013-12-31")}
CRISES = {"P10": {"covid_2020-02-19_2020-03-23": ("2020-02-19", "2020-03-23"), "year_2022": ("2022-01-01", "2022-12-31")},
          "U18": {"covid_2020-02-19_2020-03-23": ("2020-02-19", "2020-03-23"), "year_2022": ("2022-01-01", "2022-12-31")},
          "OOS": {"crash_2000-03-10_2002-10-09": ("2000-03-10", "2002-10-09"),
                  "gfc_2007-10-31_2009-03-09": ("2007-10-31", "2009-03-09")}}

P = dx.Params(account=10_000.0, slot=0.20, k=5, max_hold=20, t1_drop=-0.05, t2_below=0.10, t3_rsi=5.0, sma_short=20,
              sma_long=200, rsi_n=2, etf_spread=0.056, etf_er=0.010, etf_price=50.0, margin_spread=0.015,
              maintenance=0.25, reg_t=2.0, cash_fee=0.0010, hs_stock_bps=2.0, hs_etf_bps=10.0, hs_qqq_bps=1.0)
ONEQ_HS, QQQ_HS = 2e-4, 1e-4
CONFIGS = tuple(dx.Config(t, i, z, s, idle) for t in dx.TRIGGERS for i in dx.INSTRUMENTS for z in (0.05, 0.10)
                for s in (None, -0.15) for idle in ("cash", "qqq"))
N_TRIALS = 72
assert len(CONFIGS) == N_TRIALS
T_BONF = bonferroni_t(N_TRIALS)                      # 3.20
VARIANTS = {"P10": {"base": {}, "spread_x2": {"spread_mult": 2.0}, "etf_spread_0.70": {"etf_spread": 0.007},
                    "zero_cash_interest": {"cash_interest": False}, "account_100k": {"account": 100_000.0}},
            "OOS": {"base": {}, "spread_x2": {"spread_mult": 2.0}, "etf_spread_0.70": {"etf_spread": 0.007}},
            "U18": {"base": {}}}

# post-hoc review checks (2026-10-21; report only, not trials, never a verdict)
OWNER = "T1-I2-Z5-noSL-cash"                         # the owner's own wording
POSTHOC_MAINTENANCE = (0.30, 0.35, 0.40)             # stricter IM maintenance requirements than the registered 25%
POSTHOC_BLOCKS = (1, 6, 12)                          # mean block lengths (months) of the stationary bootstrap
POSTHOC_BOOT, POSTHOC_SEED = 4000, 20261021
POSTHOC_DROP_K = (1, 2, 3)                           # best names / best trades removed
POSTHOC_WORST = (OWNER, "T1-I1-Z5-noSL-cash", "T2-I2-Z10-noSL-qqq", "T2-IM-Z10-noSL-qqq", "T3-IM-Z10-noSL-qqq",
                 "T3-IM-Z5-SL-cash")


# ======================================================================== data check (section 0.2 (d); no strategy results)

def synthetic_check(log) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    for etf, (under, issuer, first, two_x, er) in le.ETFS.items():
        s = le.load_chart(le.chart_path(RAW, etf), etf, ETF_END)
        m = st.load_stock(under, U18_RAW)
        r_e = pd.Series(s.tr.to_numpy(float), index=s.sessions)
        r_e = r_e[r_e.index > pd.Timestamp(two_x)].dropna()
        r_u = pd.Series(m.s.tr.to_numpy(float), index=m.s.sessions)
        rf = daily_rf(r_e.index, ETF_END)
        for spread in (P.etf_spread, 0.007):
            v = dx.validate_synthetic(r_e, r_u, rf, spread, er)
            rows.append({"etf": etf, "underlying": under, "issuer": issuer, "launched": first, "two_x_from": two_x,
                         "expense_ratio": er, "spread": spread, **v})
        log.say(f"{etf}: 2x days {rows[-1]['sessions']}, real {rows[-1]['ann_real']:+.1%}, implied spread over T-bill "
                f"{rows[-1]['implied_spread_over_tbill']:.1%}")
    val = pd.DataFrame(rows)
    q_path = OUT / "closing_spreads.csv"
    spreads = pd.read_csv(q_path) if q_path.exists() else pd.DataFrame()
    return val, spreads


def fetch_vendor(log) -> pd.DataFrame:
    """Yahoo charts of the 8 ETFs and Alpaca closing-quote samples (cached; resumable)."""
    le.fetch_charts(list(le.ETFS), RAW, REQUESTS, "2026-10-21", log=log.say)
    cal = st.load_stock("AAPL", U18_RAW).s.sessions
    days = [d.strftime("%Y-%m-%d") for d in last_session_of_each_month(
        cal[(cal >= pd.Timestamp(SPREAD_SAMPLE[0])) & (cal <= pd.Timestamp(SPREAD_SAMPLE[1]))])]
    syms = list(le.ETFS) + sorted({v[0] for v in le.ETFS.values()}) + ["QQQ"]
    q = le.closing_spreads(syms, days, RAW, REQUESTS, ENV_ALPACA, log=log.say)
    q.to_csv(OUT / "closing_spreads.csv", index=False, float_format="%.4f")
    return q


def sample_checks(p10, u18, oos) -> dict:
    out = {}
    for u in (p10, u18, oos):
        li = dx.list_index(u)
        sizes = pd.Series([len(x) for x in li])
        out[u.name] = {"simulation_first": str(u.dates[0].date()), "simulation_last": str(u.dates[-1].date()),
                       "sessions": int(len(u.dates)), "securities": int(u.X.shape[1]), "lists": len(u.lists),
                       "list_size_min": int(sizes.min()), "list_size_max": int(sizes.max()),
                       "names_ending_inside": int(u.last_row.notna().sum() - (u.last_row >= u.dates[-1]).sum())}
    return out


# ======================================================================== evaluation

def fold_metrics(res: dict, bench: pd.Series, qqq: pd.Series, folds: dict, r_ew: pd.Series, r_qqq: pd.Series,
                 rf: pd.Series, twin: dict | None) -> dict:
    nav = res["nav"]
    r = nav.pct_change().fillna(0.0)
    out = {}
    for f, (a, b) in folds.items():
        m = nav_window_metrics(nav, bench, qqq, a, b)
        if m is None:
            continue
        sel = (nav.index >= pd.Timestamp(a)) & (nav.index <= pd.Timestamp(b))
        e, q = float(res["exposure"][sel].mean()), float(res["qqq_weight"][sel].mean())
        hm = dx.h_match_returns(r_ew, r_qqq, rf.reindex(r_ew.index).fillna(0.0), e, q)
        hm_nav = (1 + hm).cumprod()
        m.update({"avg_stock_exposure": e, "avg_qqq_weight": q, "avg_positions": float(res["positions"][sel].mean()),
                  "share_days_5_positions": float((res["positions"][sel] >= P.k).mean()),
                  "h_match_cagr": dx.cagr_window(hm_nav, a, b)})
        m["excess_vs_h_match"] = m["cagr"] - m["h_match_cagr"]
        m["t_vs_h_match"] = dx.t_monthly(r[sel], hm[sel])
        mean_nav = float(nav[sel].mean())
        m["cost_drag_per_year"] = float(res["cost"][sel].sum() / mean_nav / m["years"]) if mean_nav > 0 else np.nan
        if twin is not None:
            tn = twin["nav"]
            m["twin_cagr"] = dx.cagr_window(tn, a, b)
            m["excess_vs_twin"] = m["cagr"] - m["twin_cagr"]
            m["t_vs_twin"] = dx.t_monthly(r[sel], tn.pct_change().fillna(0.0)[sel])
        tr = res["trades"]
        if len(tr):
            tr = tr[(pd.to_datetime(tr["exit"]) >= pd.Timestamp(a)) & (pd.to_datetime(tr["exit"]) <= pd.Timestamp(b))]
        m.update({f"tr_{k}": v for k, v in dx.trade_stats(tr, m["years"]).items()})
        out[f] = m
    return out


def benchmarks(sname: str, u, extra: dict) -> tuple:
    """(judging benchmark, QQQ, ONEQ) buy-and-hold values on ``u.dates``: OOS is judged against QQQ (ONEQ from
    2003-10, reported only), P10 / U18 against ONEQ."""
    if sname == "OOS":
        bench = mc.buy_hold(extra["qqq"]["adjclose"], extra["qqq"]["close"], u.dates, QQQ_HS)
        oneq = extra["oneq"]
        od = u.dates[u.dates >= oneq.index.min()]
        return bench, bench, mc.buy_hold(oneq["adjclose"], oneq["close"], od, ONEQ_HS)
    bench = mc.buy_hold(extra["oneq_level"], extra["oneq_price"], u.dates, ONEQ_HS)
    return bench, mc.buy_hold(u.qqq_level, u.qqq_price, u.dates, QQQ_HS), bench


def verdict(per: dict, fold_names: tuple) -> dict:
    parts = [per[f] for f in fold_names]
    full = per["Full"]
    a, b = ab_criteria(parts, full)
    up = all(x["cagr"] > x["oneq_cagr"] for x in parts)
    a_bonf = bool(up and full["t_monthly_excess_vs_oneq"] >= T_BONF)
    return {"A": a, "B": b, "pass": bool(a or b), "A_bonferroni": a_bonf, "pass_bonferroni": bool(a_bonf or b)}


SUMMARY_KEYS = ("start", "end", "years", "cagr", "oneq_cagr", "qqq_cagr", "excess_vs_oneq", "excess_vs_qqq", "vol",
                "max_dd", "oneq_max_dd", "dd_shallower_than_oneq_pp", "sharpe", "worst_month", "worst_month_label",
                "worst_year", "worst_year_label", "t_monthly_excess_vs_oneq", "t_monthly_excess_vs_qqq",
                "avg_stock_exposure", "avg_qqq_weight", "avg_positions", "share_days_5_positions", "h_match_cagr",
                "excess_vs_h_match", "t_vs_h_match", "twin_cagr", "excess_vs_twin", "t_vs_twin", "cost_drag_per_year")


# ======================================================================== run

def run(args) -> dict:
    t_start = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    log = RunLog()
    if not dv.IS_V2:
        raise SystemExit("dip_2x is registered on frozen data v2 (REVERSAL_DATA_VERSION=v2)")
    if args.register:
        PREREG.register()
        return {}
    if getattr(args, "posthoc", False):
        return posthoc(log)
    if args.fetch:
        fetch_vendor(log)
    val, spreads = synthetic_check(log)
    val.to_csv(OUT / "synthetic_validation.csv", index=False, float_format="%.6f")
    sha = hashlib.sha256(TOP20.read_bytes()).hexdigest()
    if sha != TOP20_SHA256:
        raise SystemExit(f"{TOP20} changed (sha256 {sha}); section 0.2 (a) names {TOP20_SHA256}")
    p10, p10x = dx.load_p10(TOP20, WINDOW, EFFR_RAW)
    log.say("P10", p10x["guard"])
    u18, u18x = dx.load_u18(registry.U18, U18_RAW, U18_QQQ, str(p10.dates[0].date()), str(p10.dates[-1].date()),
                            EFFR_RAW)
    oos, oosx = dx.load_oos(OOS_FIRST, OOS_RANKS, EFFR_RAW)
    checks = {"top20_sha256": sha, "samples": sample_checks(p10, u18, oos), "u18_yahoo": u18x["checks"],
              "oos_ranking_identical_to_oos2": oosx["ranking_identical_to_oos2"],
              "p10_terminal_events_in_window": int(len(p10x["terminal_events"])),
              "synthetic_median_implied_spread": float(val.loc[val["spread"] == P.etf_spread,
                                                               "implied_spread_over_tbill"].median()),
              "closing_spread_median_bps": (spreads.groupby("symbol")["median_spread_bps"].median().round(2).to_dict()
                                            if len(spreads) else {})}
    (OUT / "data_checks.json").write_text(json.dumps(checks, indent=1, default=json_rounded) + "\n")
    log.say("data checks:", json.dumps(checks["samples"]))
    if args.check_data:
        log.write(OUT / "run_log_check_data.txt")
        return checks
    frozen = json.loads(FROZEN.read_text())
    if frozen["sha256"] != PREREG.hash():
        raise SystemExit(f"section 0 changed since registration ({frozen['sha256'][:8]} vs {PREREG.hash()[:8]})")
    log.say("pre-registration", frozen["sha256"], "registered", frozen["registered_at"])

    samples = {"P10": (p10, P10_FOLDS, ("H1", "H2")), "OOS": (oos, OOS_FOLDS, ("O1", "O2")),
               "U18": (u18, P10_FOLDS, ("H1", "H2"))}
    summary, verdicts, tstats, years, crises, months, sens = [], {}, [], [], [], [], []
    trade_logs = {}
    for sname, (u, folds, judged) in samples.items():
        sig = dx.signals(u.X, P)
        r_ew = dx.ew_returns(u)
        r_q = u.qqq_level.reindex(u.dates).pct_change().fillna(0.0)
        rf = u.rf.reindex(u.dates).fillna(0.0)
        bench, qqq_nav, oneq_nav = benchmarks(sname, u, {"P10": p10x, "U18": u18x, "OOS": oosx}[sname])
        for f, (a, b) in folds.items():
            for nm, s_ in (("bench", bench), ("QQQ", qqq_nav)):
                m = nav_window_metrics(s_, bench, qqq_nav, a, b)
                summary.append({"sample": sname, "variant": "base", "config": f"{nm} buy-hold", "fold": f,
                                **{k: m.get(k) for k in SUMMARY_KEYS}})
        for y, v in dx.by_year(bench).items():
            years.append({"sample": sname, "config": "bench buy-hold", "year": y, "ret": v})
        for y, v in dx.by_year(qqq_nav).items():
            years.append({"sample": sname, "config": "QQQ buy-hold", "year": y, "ret": v})
        if sname == "OOS":
            for y, v in dx.by_year(oneq_nav).items():
                years.append({"sample": sname, "config": "ONEQ buy-hold (2003-10 on)", "year": y, "ret": v})
        for cname, (a, b) in CRISES[sname].items():
            for nm, s_ in (("bench buy-hold", bench), ("QQQ buy-hold", qqq_nav)):
                crises.append({"sample": sname, "config": nm, "window": cname, **dx.window_return(s_, a, b)})
        for vname, over in VARIANTS[sname].items():
            pv = replace(P, **over)
            runs = {}
            for cfg in CONFIGS:
                if vname == "etf_spread_0.70" and cfg.instrument != "I2":
                    continue
                runs[cfg.name] = dx.simulate(u, cfg, pv, sig)
            for cfg in CONFIGS:
                if cfg.name not in runs:
                    continue
                res = runs[cfg.name]
                acct_scale = pv.account / P.account
                b_ = bench * acct_scale
                q_ = qqq_nav * acct_scale
                twin = runs.get(cfg.twin().name) if cfg.instrument != "I1" else None
                per = fold_metrics(res, b_, q_, folds, r_ew, r_q, rf, twin)
                vd = verdict(per, judged)
                for f, m in per.items():
                    summary.append({"sample": sname, "variant": vname, "config": cfg.name, "fold": f,
                                    **{k: m.get(k) for k in SUMMARY_KEYS}, **{k: v for k, v in vd.items()}})
                if vname == "base":
                    verdicts.setdefault(cfg.name, {})[sname] = vd
                    for f, m in per.items():
                        tstats.append({"sample": sname, "config": cfg.name, "fold": f,
                                       **{k[3:]: v for k, v in m.items() if k.startswith("tr_")}})
                    for y, v in dx.by_year(res["nav"]).items():
                        years.append({"sample": sname, "config": cfg.name, "year": y, "ret": v})
                    for cname, (a, b) in CRISES[sname].items():
                        crises.append({"sample": sname, "config": cfg.name, "window": cname,
                                       **dx.window_return(res["nav"], a, b)})
                    mr = monthly(res["nav"].pct_change().dropna())
                    months += [{"sample": sname, "config": cfg.name, "month": str(k), "ret": v} for k, v in mr.items()]
                    t = res["trades"].assign(sample=sname, config=cfg.name)
                    trade_logs.setdefault(sname, []).append(t)
                    verdicts[cfg.name][f"{sname}_counts"] = res["counts"]
                    verdicts[cfg.name][f"{sname}_open_at_end"] = len(res["open_at_end"])
                else:
                    sens.append({"sample": sname, "variant": vname, "config": cfg.name, **vd,
                                 **{f"{f}_cagr": per[f]["cagr"] for f in per},
                                 **{f"{f}_bench_cagr": per[f]["oneq_cagr"] for f in per},
                                 "full_t": per["Full"]["t_monthly_excess_vs_oneq"], "full_max_dd": per["Full"]["max_dd"]})
            log.say(f"{sname} {vname}: {len(runs)} runs ({time.time() - t_start:.0f}s)")
    # ---- verdicts (section 0.6)
    vrows = []
    s2 = {(r["config"]): r for r in sens if r["sample"] == "P10" and r["variant"] == "spread_x2"}
    for cfg in CONFIGS:
        v = verdicts[cfg.name]
        row = {"config": cfg.name, "trigger": cfg.trigger, "instrument": cfg.instrument, "z": cfg.z,
               "stop": cfg.stop, "idle": cfg.idle}
        for sname in ("P10", "OOS", "U18"):
            row.update({f"{sname}_{k}": x for k, x in v[sname].items()})
        row["P10_pass_spread_x2"] = s2[cfg.name]["pass"]
        row["both_samples_pass"] = bool(row["P10_pass"] and row["OOS_pass"])
        row["candidate"] = bool(row["P10_pass_bonferroni"] and row["both_samples_pass"] and row["P10_pass_spread_x2"])
        vrows.append(row)
    vdf = pd.DataFrame(vrows)
    sm = pd.DataFrame(summary)
    sm.to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    vdf.to_csv(OUT / "verdicts.csv", index=False)
    pd.DataFrame(tstats).to_csv(OUT / "trade_stats.csv", index=False, float_format="%.6f")
    pd.DataFrame(years).to_csv(OUT / "by_year.csv", index=False, float_format="%.6f")
    pd.DataFrame(crises).to_csv(OUT / "crisis_windows.csv", index=False, float_format="%.6f")
    pd.DataFrame(sens).to_csv(OUT / "sensitivities.csv", index=False, float_format="%.6f")
    pd.DataFrame(months).to_csv(OUT / "monthly_returns.csv.gz", index=False, float_format="%.6f")
    for sname, parts in trade_logs.items():
        pd.concat(parts, ignore_index=True).to_csv(OUT / f"trades_{sname}.csv.gz", index=False, float_format="%.6f")
    out = {"prereg_sha256": frozen["sha256"], "n_trials": N_TRIALS, "bonferroni_t": T_BONF, "params": P.__dict__,
           "verdict_counts": {k: int(vdf[k].sum()) for k in ("P10_pass", "P10_pass_bonferroni", "OOS_pass",
                                                             "both_samples_pass", "candidate", "U18_pass")},
           "candidates": vdf.loc[vdf["candidate"], "config"].tolist(),
           "counts": {c: {s: verdicts[c].get(f"{s}_counts") for s in ("P10", "OOS", "U18")} for c in verdicts},
           "data_checks": checks, "runtime_s": round(time.time() - t_start, 1)}
    (OUT / "results.json").write_text(json.dumps(out, indent=1, default=json_rounded) + "\n")
    log.say("verdict counts:", json.dumps(out["verdict_counts"]), "candidates:", out["candidates"])
    log.write(OUT / "run_log.txt")
    return out


# ======================================================================== post-hoc review checks (report only)

def _window(nav: pd.Series, a: str, b: str) -> pd.Series:
    """Daily returns of ``nav`` on the sessions in [a, b] (the first one measured from the close before a)."""
    r = nav.pct_change().fillna(0.0)
    return r[(r.index >= pd.Timestamp(a)) & (r.index <= pd.Timestamp(b))]


def _short(per: dict, vd: dict, judged: tuple) -> dict:
    out = {f"{f}_cagr": per[f]["cagr"] for f in (*judged, "Full")}
    out.update({f"{f}_bench_cagr": per[f]["oneq_cagr"] for f in (*judged, "Full")})
    out.update({"full_t": per["Full"]["t_monthly_excess_vs_oneq"], "full_max_dd": per["Full"]["max_dd"],
                "A": vd["A"], "B": vd["B"], "pass": vd["pass"]})
    return out


def posthoc(log) -> dict:
    """The report-only checks of the two reviews (ledger, "审核后的修正与补充", 2026-10-21). Reads the registered
    verdicts; re-runs the base configurations (and checks they reproduce summary.csv); never changes a verdict."""
    t_start = time.time()
    reg = pd.read_csv(OUT / "summary.csv")
    reg = reg[reg["variant"] == "base"].set_index(["sample", "config", "fold"])
    verd = pd.read_csv(OUT / "verdicts.csv").set_index("config")
    p10, p10x = dx.load_p10(TOP20, WINDOW, EFFR_RAW)
    oos, oosx = dx.load_oos(OOS_FIRST, OOS_RANKS, EFFR_RAW)
    rows = {k: [] for k in ("stop_loss", "matched_twin", "hold_list", "h_match_fair", "margin", "concentration",
                            "by_trigger", "worst")}
    out = {"reproduces_registered": {}, "trials": {}, "trade_pnl": {}}
    for sname, u, x, folds, judged in (("P10", p10, p10x, P10_FOLDS, ("H1", "H2")),
                                       ("OOS", oos, oosx, OOS_FOLDS, ("O1", "O2"))):
        bench, qqq_nav, _ = benchmarks(sname, u, x)
        sig = dx.signals(u.X, P)
        r_ew = dx.ew_returns(u)
        r_q = u.qqq_level.reindex(u.dates).pct_change().fillna(0.0)
        rf = u.rf.reindex(u.dates).fillna(0.0)
        runs = {cfg.name: dx.simulate(u, cfg, P, sig) for cfg in CONFIGS}
        per = {n: fold_metrics(res, bench, qqq_nav, folds, r_ew, r_q, rf, None) for n, res in runs.items()}
        dev = max(max(abs(per[n][f][k] - reg.loc[(sname, n, f), k]) for k in ("cagr", "t_monthly_excess_vs_oneq"))
                  for n in runs for f in folds)
        out["reproduces_registered"][sname] = float(dev)
        log.say(f"posthoc {sname}: 72 base runs, largest deviation from summary.csv {dev:.2e} "
                f"({time.time() - t_start:.0f}s)")

        # (1) stop-loss fills (idle-cash variants; the idle-QQQ trade lists are nearly the same)
        for ins in dx.INSTRUMENTS:
            t = pd.concat([runs[c.name]["trades"] for c in CONFIGS
                           if c.instrument == ins and c.stop is not None and c.idle == "cash"], ignore_index=True)
            s = t[t["reason"] == "stop_loss"]
            w = s.loc[s["ret_net"].idxmin()]
            rows["stop_loss"].append({"sample": sname, "instrument": ins, "stop_exits": len(s),
                                      "median": s["ret_net"].median(), "mean": s["ret_net"].mean(),
                                      "p5": s["ret_net"].quantile(0.05), "share_below_-25%": (s["ret_net"] < -0.25).mean(),
                                      "share_below_-50%": (s["ret_net"] < -0.50).mean(), "worst": w["ret_net"],
                                      "worst_which": f"{w['ticker']} {w['entry']}"})

        # (2) the matched leverage twin (same stock move to take profit) next to the registered one (same thresholds)
        key = ["sid", "entry", "exit"]
        for cfg in CONFIGS:
            mt = cfg.matched_twin()
            if mt is None or mt.name not in runs:
                continue
            ta, tb = runs[cfg.name]["trades"], runs[mt.name]["trades"]
            diff = set(map(tuple, ta[key].to_numpy())) ^ set(map(tuple, tb[key].to_numpy()))
            row = {"sample": sname, "config": cfg.name, "matched_twin": mt.name, "registered_twin": cfg.twin().name,
                   "trades": len(ta), "twin_trades": len(tb), "trade_differences": len(diff)}
            for f, (a, b) in folds.items():
                ra, rb = _window(runs[cfg.name]["nav"], a, b), _window(runs[mt.name]["nav"], a, b)
                row[f"{f}_excess_vs_matched"] = per[cfg.name][f]["cagr"] - per[mt.name][f]["cagr"]
                row[f"{f}_t_vs_matched"] = dx.t_monthly(ra, rb)
                row[f"{f}_excess_vs_registered_twin"] = per[cfg.name][f]["cagr"] - per[cfg.twin().name][f]["cagr"]
            rows["matched_twin"].append(row)

        # (3) constant-exposure holds of the same lists (no timing, no costs)
        refs = dx.hold_list_returns(u, r_ew, P)
        full_cagrs = pd.Series({n: per[n]["Full"]["cagr"] for n in runs})
        for nm, r in refs.items():
            nav = P.account * (1 + r).cumprod()
            pm = {f: nav_window_metrics(nav, bench, qqq_nav, a, b) for f, (a, b) in folds.items()}
            vd = verdict(pm, judged)
            row = {"sample": sname, "hold": nm, **{f"{f}_{k}": pm[f][k] for f in folds
                                                   for k in ("cagr", "oneq_cagr", "max_dd")},
                   "full_t_vs_bench": pm["Full"]["t_monthly_excess_vs_oneq"],
                   "configs_with_higher_full_cagr": int((full_cagrs > pm["Full"]["cagr"]).sum()),
                   "A": vd["A"], "B": vd["B"], "A_bonferroni": vd["A_bonferroni"]}
            for cname, (a, b) in CRISES[sname].items():
                row[f"crisis_{cname}"] = dx.window_return(nav, a, b)["ret"]
            rows["hold_list"].append(row)

        # (4) a matched-exposure hold that pays the configuration's own financing (I2: half the exposure in the 2x
        # basket; IM: borrowing at EFFR + 1.5% where the registered H_match borrows at the T-bill)
        borrow = dx.margin_borrow_rate(u, u.dates, P)
        for cfg in CONFIGS:
            if cfg.instrument == "I1":
                continue
            res = runs[cfg.name]
            for f, (a, b) in folds.items():
                sel = (u.dates >= pd.Timestamp(a)) & (u.dates <= pd.Timestamp(b))
                e, q = float(res["exposure"][sel].mean()), float(res["qqq_weight"][sel].mean())
                if cfg.instrument == "I2":
                    hm = dx.h_match_financed_returns(refs["2x_etf"], r_q, rf, borrow, e / 2, q)
                else:
                    hm = dx.h_match_financed_returns(r_ew, r_q, rf, borrow, e, q)
                fair = dx.cagr_window((1 + hm).cumprod(), a, b)
                pm = per[cfg.name][f]
                rows["h_match_fair"].append({"sample": sname, "config": cfg.name, "instrument": cfg.instrument,
                                             "fold": f, "cagr": pm["cagr"], "h_match_cagr": pm["h_match_cagr"],
                                             "excess_vs_h_match": pm["excess_vs_h_match"],
                                             "t_vs_h_match": pm["t_vs_h_match"], "fair_cagr": fair,
                                             "excess_vs_fair": pm["cagr"] - fair,
                                             "t_vs_fair": dx.t_monthly(_window(res["nav"], a, b), hm[sel])})

        # (5) IM: the margin cushion and stricter maintenance requirements
        for cfg in CONFIGS:
            if cfg.instrument != "IM":
                continue
            row = {"sample": sname, "config": cfg.name, **dx.margin_cushion(runs[cfg.name], P.maintenance),
                   "calls_0.25": runs[cfg.name]["counts"]["margin_calls"], "pass_0.25": bool(verd.loc[cfg.name,
                                                                                                   f"{sname}_pass"])}
            for mreq in POSTHOC_MAINTENANCE:
                res = dx.simulate(u, cfg, replace(P, maintenance=mreq), sig)
                pm = fold_metrics(res, bench, qqq_nav, folds, r_ew, r_q, rf, None)
                tr = res["trades"]
                first = tr.loc[tr["reason"] == "margin_call", "exit"].min() if len(tr) else None
                row.update({f"calls_{mreq:.2f}": res["counts"]["margin_calls"],
                            f"first_call_{mreq:.2f}": first if isinstance(first, str) else "",
                            f"full_cagr_{mreq:.2f}": pm["Full"]["cagr"], f"max_dd_{mreq:.2f}": pm["Full"]["max_dd"],
                            f"pass_{mreq:.2f}": verdict(pm, judged)["pass"]})
            rows["margin"].append(row)
        log.say(f"posthoc {sname}: margin variants done ({time.time() - t_start:.0f}s)")

        # (6) concentration: drop the best 1-3 names (whole sample) and the best 1-3 trades (signal day .. exit)
        chosen = [c for c in CONFIGS if verd.loc[c.name, f"{sname}_pass"] or c.name == OWNER]
        for cfg in chosen:
            base = runs[cfg.name]
            tr = base["trades"].assign(contrib=dx.trade_contributions(base["trades"], base["nav"]))
            by_name = tr.groupby("sid")["contrib"].sum().sort_values(ascending=False)
            rows["concentration"].append({"sample": sname, "config": cfg.name, "drop": "none", "dropped": "",
                                          **_short(per[cfg.name], verdict(per[cfg.name], judged), judged)})
            best = tr.sort_values("contrib", ascending=False)
            for k in POSTHOC_DROP_K:
                names = list(by_name.index[:k])
                blocks = [(s, u.X.index[0], u.X.index[-1]) for s in names]
                trades = [(r.sid, u.dates[u.dates.get_loc(pd.Timestamp(r.entry)) - 1], r.exit)
                          for r in best.head(k).itertuples()]
                for kind, bl, label in (("names", blocks, ",".join(u.labels.get(s, s) for s in names)),
                                        ("trades", trades, ",".join(f"{u.labels.get(r.sid, r.sid)} {r.entry}"
                                                                    for r in best.head(k).itertuples()))):
                    res = dx.simulate(u, cfg, P, dx.mask_trigger(sig, cfg.trigger, bl))
                    pm = fold_metrics(res, bench, qqq_nav, folds, r_ew, r_q, rf, None)
                    rows["concentration"].append({"sample": sname, "config": cfg.name, "drop": f"best_{k}_{kind}",
                                                  "dropped": label, **_short(pm, verdict(pm, judged), judged)})
        tr = runs[OWNER]["trades"].sort_values("pnl")
        out["trade_pnl"][sname] = {"config": OWNER, "trades": int(len(tr)), "total_pnl": float(tr["pnl"].sum()),
                                   "best_10_pnl": float(tr["pnl"].tail(10).sum()),
                                   "worst_10_pnl": float(tr["pnl"].head(10).sum()), "account": P.account}
        log.say(f"posthoc {sname}: concentration done ({time.time() - t_start:.0f}s)")

        # (7) the H_match comparison by trigger (the 72-config average hides opposite signs)
        for trig in dx.TRIGGERS:
            xs = pd.Series({c.name: per[c.name]["Full"]["excess_vs_h_match"] for c in CONFIGS if c.trigger == trig})
            ts = pd.Series({c.name: per[c.name]["Full"]["t_vs_h_match"] for c in CONFIGS if c.trigger == trig})
            rows["by_trigger"].append({"sample": sname, "trigger": trig, "configs": len(xs),
                                       "positive": int((xs > 0).sum()), "mean_excess": xs.mean(), "min_excess": xs.min(),
                                       "max_excess": xs.max(), "mean_t": ts.mean(), "max_t": ts.max()})

        # (8) worst windows of a few configurations and the benchmarks
        for nm, nav in [("bench buy-hold", bench), ("QQQ buy-hold", qqq_nav)] + [(n, runs[n]["nav"]) for n in POSTHOC_WORST]:
            v = nav[nav.index >= u.dates[0]]
            rows["worst"].append({"sample": sname, "config": nm, "max_dd": max_drawdown(v),
                                  "worst_12m": worst_window(v, 252), "worst_36m": worst_window(v, 756),
                                  "longest_underwater_years": longest_underwater_sessions(v) / 252,
                                  "final_over_peak": float(v.iloc[-1] / v.max())})

        # (9) P10 only: correlated-trial hurdles for the A criterion's t (the registered rule is Bonferroni 3.20)
        if sname == "P10":
            a, b = folds["Full"]
            rb = _window(bench, a, b)
            ex = pd.DataFrame({n: monthly(_window(runs[n]["nav"], a, b)) - monthly(rb) for n in runs})
            t = ex.mean() / ex.std(ddof=1) * np.sqrt(len(ex))
            t_dev = float(max(abs(t[n] - per[n]["Full"]["t_monthly_excess_vs_oneq"]) for n in runs))
            eff = effective_trials(ex.to_numpy())
            hurdles = {"bonferroni_72": T_BONF, "sidak_nyholt": sidak_t(eff["nyholt"]),
                       "sidak_li_ji": sidak_t(eff["li_ji"])}
            for blk in POSTHOC_BLOCKS:
                hurdles[f"bootstrap_max_t_block_{blk}"] = bootstrap_max_t(ex.to_numpy(), blk, POSTHOC_BOOT,
                                                                          POSTHOC_SEED + blk)
            up = {n for n in runs if all(per[n][f]["cagr"] > per[n][f]["oneq_cagr"] for f in judged)}
            out["trials"] = {"months": int(len(ex)), "t_recomputed_max_deviation": t_dev, **eff,
                             "hurdles": hurdles,
                             "passing_A": {h: sorted(((n, round(float(t[n]), 3), bool(verd.loc[n, "OOS_pass"]))
                                                      for n in up if t[n] >= v), key=lambda z: -z[1])
                                           for h, v in hurdles.items()},
                             "top_t": {n: round(float(v), 3) for n, v in t.sort_values(ascending=False).head(6).items()}}
    for k, v in rows.items():
        pd.DataFrame(v).to_csv(OUT / f"posthoc_{k}.csv", index=False, float_format="%.6f")
    r18 = pd.read_csv(OUT / "summary.csv")
    r18 = r18[(r18["variant"] == "base") & (r18["fold"] == "Full") & r18["sample"].isin(["P10", "U18"])
              & ~r18["config"].str.contains("buy-hold")].pivot(index="config", columns="sample")
    out["u18_vs_p10"] = {"mean_cagr_gap": float((r18["cagr"]["U18"] - r18["cagr"]["P10"]).mean()),
                         "mean_exposure_P10": float(r18["avg_stock_exposure"]["P10"].mean()),
                         "mean_exposure_U18": float(r18["avg_stock_exposure"]["U18"].mean()),
                         "mean_positions_P10": float(r18["avg_positions"]["P10"].mean()),
                         "mean_positions_U18": float(r18["avg_positions"]["U18"].mean())}
    out["runtime_s"] = round(time.time() - t_start, 1)
    (OUT / "posthoc.json").write_text(json.dumps(out, indent=1, default=json_rounded) + "\n")
    log.say("posthoc trials:", json.dumps(out["trials"].get("hurdles", {}), default=json_rounded))
    log.write(OUT / "run_log_posthoc.txt")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check-data", action="store_true", help="data checks only (no strategy results)")
    g.add_argument("--register", action="store_true", help="write the section 0 hash once")
    g.add_argument("--run", action="store_true", help="the registered run")
    g.add_argument("--posthoc", action="store_true", help="report-only review checks (after --run)")
    ap.add_argument("--fetch", action="store_true", help="fetch / refresh the cached vendor data first")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
