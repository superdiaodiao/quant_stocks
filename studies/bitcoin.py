"""Bitcoin: context, stand-alone BTC rules and QQQ + BTC sleeves vs ONEQ (pre-registered in
docs/research_ledger_bitcoin.md, section 0).

Rules (quant.strategies.bitcoin): BT1a/b 200-day SMA / 50-200 cross, BT2a/b 3- / 12-month time-series momentum,
BT3 40% vol target (stand-alone, 7-day UTC calendar, BTC or cash); BT4a-d QQQ + 5/10/20% BTC sleeve (monthly or
5/25 band), BT5a-c the sleeve with the BT1a filter on the BTC part (US sessions). BT6: weekend diagnostic.
Execution: decided at the close of t, traded at the next row's close. Costs: IBKR spot crypto (0.18%, min $1.75
capped at 1%, 5 bp half-spread, Paxos $0.15 a month), QQQ / ONEQ by ``etf_order_cost``; cash earns the T-bill rate.
Data (local only): research_cache/bitcoin/raw (Bitstamp primary; Coinbase, Yahoo, Kraken as checks; IBIT/FBTC),
QQQ / ONEQ / sessions / T-bill from research_cache/sector_lev/raw (quant.data.sector_etfs). Every frame truncated at
``END``. Outputs: output/research_only/bitcoin/ (returns and metrics only, never vendor price levels).

Usage:  PYTHONPATH=. .venv/bin/python -m quant study bitcoin [--fetch] [--data-only]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from quant.backtest.costs import PAXOS_MONTHLY_FEE, etf_order_cost, ibkr_crypto_cost
from quant.data import bitcoin as bd
from quant.data import sector_etfs as etfs
from quant.data.guards import assert_dev_dates
from quant.data.sources import crypto
from quant.evaluation.criteria import A_T_MIN, ab_verdict, bonferroni_t
from quant.evaluation.equity import period_metrics
from quant.evaluation.metrics import max_drawdown, monthly, yearly
from quant.paths import ROOT, output_dir
from quant.reporting import Preregistration, RunLog, write_json, write_period_tables
from quant.signals.technical import sma_state
from quant.strategies import bitcoin as bt

END = "2026-09-30"
ENTRY = "2014-09-30"                       # entry close (US session and UTC day); targets decided 2014-09-29
FOLDS = {"H1": ("2014-10-01", "2019-12-31"), "H2": ("2020-01-01", END)}
P = bt.Params()
BTC_HS, QQQ_HS, ONEQ_HS, ETF_BTC_HS = 5e-4, 1e-4, 2e-4, 2e-4
SENS_BTC_HS = 25e-4
IBIT_FROM = "2024-01-12"                   # IBIT's first daily return
N_TRIALS = len(bt.SPECS) + len(bt.DIAGNOSTICS)
DD_OK_PP = 5.0                             # sleeve "acceptable drawdown": no more than 5 points deeper than QQQ
OUT = output_dir("bitcoin")
LEDGER = ROOT / "docs/research_ledger_bitcoin.md"


# ======================================================================== data

def fetch() -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    crypto.fetch_bitstamp(bd.CACHE, END)
    crypto.fetch_coinbase(bd.CACHE, END)
    crypto.fetch_kraken(bd.CACHE, today)
    crypto.fetch_yahoo(bd.CACHE, "BTC-USD", "2014-09-01", "2026-10-02")
    for s in bd.ETFS:
        crypto.fetch_yahoo(bd.CACHE, s, "2024-01-01", "2026-10-02")


def data_report(src: bd.Sources, log: RunLog) -> tuple[pd.Series, dict]:
    cov = bd.coverage(src)
    pairs, by_year = bd.agreement(src.close)
    p, info = bd.primary_close(src)
    OUT.mkdir(parents=True, exist_ok=True)
    pairs.to_csv(OUT / "source_agreement.csv", index=False, float_format="%.6f")
    by_year.to_csv(OUT / "source_agreement_by_year.csv", float_format="%.4f")
    for k, v in cov.items():
        log.say(f"{k:9s} {v['first']}..{v['last']} rows {v['rows']} missing {v['missing_calendar_days']} "
                f"zero-volume {v['zero_volume_days']}")
    for _, r in pairs.iterrows():
        log.say(f"{r['pair']:18s} from {r['first']} days {r['days']}: daily within 0.5pt {r['share_daily_within_0p5pct']:.1%}"
                f" | monthly within 1pt {r['share_monthly_within_1pct']:.1%} | max {r['max_abs_daily_diff']:.2%} on {r['max_day']}")
    log.say("primary", info)
    return p, {"coverage": cov, "agreement": pairs.to_dict("records"), "primary": info}


# ======================================================================== evaluation helpers

def window(r: pd.Series, a: str, b: str) -> pd.Series:
    return r.loc[a:b]


def rekey(m: dict, tag: str) -> dict:
    return {k.replace("oneq", tag): v for k, v in m.items()}


def judge(r: pd.Series, bench: pd.Series, q: pd.Series, rf: pd.Series) -> dict:
    """period_metrics on full / H1 / H2 and the A / B verdict against ``bench`` (keys say 'oneq')."""
    parts = {"full": (ENTRY_RET, END)} | FOLDS
    m = {k: period_metrics(window(r, a, b), window(bench, a, b), window(q, a, b), rf) for k, (a, b) in parts.items()}
    v = ab_verdict(m["full"], m["H1"], m["H2"])
    hurdle = bonferroni_t(N_TRIALS)
    a_bonf = all(m[k]["cagr"] > m[k]["oneq_cagr"] for k in m) and m["full"]["t_monthly_excess_vs_oneq"] >= hurdle
    v["A_bonferroni"] = bool(a_bonf)
    v["pass_bonferroni"] = bool(a_bonf or v["B"])
    return {"metrics": m, "verdict": v}


ENTRY_RET = "2014-10-01"


def short(m: dict) -> dict:
    keys = ("cagr", "max_dd", "cagr_minus_oneq", "t_monthly_excess_vs_oneq")
    return {k: m["metrics"]["full"][k] for k in keys} | {
        f"{h}_cagr_minus_oneq": m["metrics"][h]["cagr_minus_oneq"] for h in FOLDS} | {"pass": m["verdict"]["pass"]}


# ======================================================================== engines bound to the study's data

def legs_calendar(p: pd.Series, rf: pd.Series, cash_zero: bool = False):
    days = p.index
    rets = pd.DataFrame({"BTC": p.pct_change(fill_method=None),
                         "CASH": 0.0 if cash_zero else bd.calendar_rf(rf, days)}, index=days)
    prices = pd.DataFrame({"BTC": p, "CASH": np.nan}, index=days)
    return rets, prices


def legs_sessions(p: pd.Series, d: etfs.Data, sessions: pd.DatetimeIndex, cash_zero: bool = False,
                  etf_btc: pd.Series | None = None):
    btc = bd.on_sessions(p, sessions).pct_change(fill_method=None)
    if etf_btc is not None:
        btc = btc.where(btc.index < pd.Timestamp(IBIT_FROM), etf_btc.reindex(sessions))
    rets = pd.DataFrame({"QQQ": d.rets["QQQ"].reindex(sessions), "BTC": btc,
                         "CASH": 0.0 if cash_zero else d.rf.reindex(sessions),
                         "ONEQ": d.rets["ONEQ"].reindex(sessions)}, index=sessions)
    prices = pd.DataFrame({"QQQ": d.close["QQQ"].reindex(sessions), "ONEQ": d.close["ONEQ"].reindex(sessions),
                           "BTC": np.nan, "CASH": np.nan}, index=sessions)
    return rets, prices


def cost_fns(btc_hs: float = BTC_HS, etf_close: pd.Series | None = None) -> dict:
    def btc(v, px, sell, ts):
        if etf_close is not None and ts >= pd.Timestamp(IBIT_FROM):
            return etf_order_cost(v, float(etf_close.loc[ts]), sell, ETF_BTC_HS)
        return ibkr_crypto_cost(v, btc_hs)
    return {"BTC": btc, "QQQ": lambda v, px, sell, ts: etf_order_cost(v, px, sell, QQQ_HS),
            "ONEQ": lambda v, px, sell, ts: etf_order_cost(v, px, sell, ONEQ_HS)}


def run_standalone(name: str, p: pd.Series, rf: pd.Series, sessions, *, btc_hs=BTC_HS, costs=True, cash_zero=False):
    rets, prices = legs_calendar(p, rf, cash_zero)
    w, flag = bt.standalone_targets(name, p, P) if name != "BTC" else (
        pd.DataFrame({"BTC": 1.0, "CASH": 0.0}, index=p.index), pd.Series(False, index=p.index))
    s_i = p.index.get_loc(pd.Timestamp(ENTRY))
    sim = bt.simulate(w, flag, rets, prices, s_i, cost_fns(btc_hs), monthly_fee=PAXOS_MONTHLY_FEE, costs=costs)
    sim["ret_sessions"] = bt.to_sessions(sim["ret"], sessions)
    return sim


def run_sleeve(spec, p: pd.Series, d: etfs.Data, sessions, *, btc_hs=BTC_HS, costs=True, cash_zero=False,
               etf=None):
    rets, prices = legs_sessions(p, d, sessions, cash_zero, None if etf is None else etf["ret"])
    trend = sma_state(p, P.sma_long).reindex(sessions) if spec.name.startswith("BT5") else None
    w, flag = bt.sleeve_targets(spec, sessions, trend)
    band = {"BTC": bt.band_width(P, spec.sleeve)} if spec.name == "BT4d" else None
    s_i = sessions.get_loc(pd.Timestamp(ENTRY))
    fns = cost_fns(btc_hs, None if etf is None else etf["close"])
    sim = bt.simulate(w, flag, rets, prices, s_i, fns, band=band, monthly_fee=PAXOS_MONTHLY_FEE, costs=costs)
    sim["ret_sessions"] = sim["ret"]
    sim["btc_ret"] = rets["BTC"]
    return sim


def etf_series(symbol: str, sessions) -> dict:
    df = bd.load_etf(symbol, END)
    ix = pd.DatetimeIndex(df["date"])
    adj = pd.Series(df["adjclose"].values, index=ix).reindex(sessions)
    close = pd.Series(df["close"].values, index=ix).reindex(sessions)
    return {"ret": adj.pct_change(fill_method=None), "close": close, "first": str(ix[0].date())}


# ======================================================================== main

def run(args=None) -> dict:
    log = RunLog()
    if getattr(args, "fetch", False):
        fetch()
    src = bd.load_sources(END)
    p, data_info = data_report(src, log)
    if getattr(args, "data_only", False):
        log.write(OUT / "run_log_data.txt")
        return data_info
    pre = Preregistration(LEDGER, OUT / "prereg_frozen.json", OUT)
    if not (OUT / "prereg_frozen.json").exists():
        pre.register()
    frozen = json.loads((OUT / "prereg_frozen.json").read_text())
    if frozen["sha256"] != pre.hash():
        raise SystemExit("section 0 of the ledger changed after registration")
    d = etfs.load_data(END)
    sessions = d.sessions[d.sessions >= pd.Timestamp("2013-01-01")]
    assert_dev_dates(sessions, END)
    assert pd.Timestamp(ENTRY) in sessions and pd.Timestamp(ENTRY) in p.index
    rf = d.rf
    res = {"end": END, "entry_close": ENTRY, "folds": FOLDS, "n_trials": N_TRIALS,
           "bonferroni_t_one_sided_5pct": bonferroni_t(N_TRIALS), "prereg_sha256": frozen["sha256"],
           "data": data_info, "rules": {}}

    # ---- benchmarks (bought at the ENTRY close, with costs)
    rets_s, prices_s = legs_sessions(p, d, sessions)
    s_i = sessions.get_loc(pd.Timestamp(ENTRY))
    oneq = bt.buy_hold("ONEQ", rets_s, prices_s, s_i, cost_fns())["ret"]
    qqq = bt.buy_hold("QQQ", rets_s, prices_s, s_i, cost_fns())["ret"]
    btc_bh = run_standalone("BTC", p, rf, sessions)["ret_sessions"]
    for s in (oneq, qqq, btc_bh):
        assert s.index[0] == pd.Timestamp(ENTRY_RET) and s.index[-1] == pd.Timestamp(END)
    assert btc_bh.index.equals(oneq.index)

    # ---- context (descriptive)
    ctx = {}
    for tag, r in (("BTC", btc_bh), ("QQQ", qqq), ("ONEQ", oneq)):
        j = judge(r, oneq, qqq, rf)["metrics"]
        ctx[tag] = {k: {f: j[k][f] for f in ("start", "end", "cagr", "vol", "max_dd", "sharpe", "calmar",
                                             "worst_year", "worst_year_ret", "worst_month", "worst_month_ret")}
                    for k in j}
        y = yearly(r)
        ctx[tag]["best_year"] = [int(y.idxmax()), float(y.max())]
    btc_cal = p.pct_change(fill_method=None)
    ev = btc_cal.loc[ENTRY_RET:]
    ctx["BTC"]["vol_7day_calendar_x_sqrt365"] = float(ev.std() * np.sqrt(365))
    long = btc_cal.loc["2011-08-19":]
    ctx["BTC_long_report_only"] = {"start": "2011-08-18", "cagr_calendar": float((p.iloc[-1] / p.iloc[0]) ** (365.25 / (
        (p.index[-1] - p.index[0]).days)) - 1), "max_dd": max_drawdown(p), "vol": float(long.std() * np.sqrt(365))}
    ctx["BTC_drawdowns_over_50pct"] = bd.drawdown_episodes(p.loc["2011-08-18":], -0.5)
    btc_s = bd.on_sessions(p, sessions).pct_change(fill_method=None).loc[ENTRY_RET:]
    rc = bt.rolling_corr(btc_s, d.rets["QQQ"].reindex(btc_s.index), 252).dropna()
    wk_btc = (1 + btc_s).groupby(btc_s.index.to_period("W-FRI")).prod() - 1
    q_s = d.rets["QQQ"].reindex(btc_s.index)
    wk_q = (1 + q_s).groupby(q_s.index.to_period("W-FRI")).prod() - 1
    corr_year = pd.DataFrame({
        "daily_corr": btc_s.groupby(btc_s.index.year).apply(lambda x: x.corr(q_s.loc[x.index])),
        "weekly_corr": pd.Series({y: wk_btc[wk_btc.index.year == y].corr(wk_q[wk_q.index.year == y])
                                  for y in sorted(set(wk_btc.index.year))}),
        "rolling252_at_year_end": rc.groupby(rc.index.year).last()})
    ctx["corr_btc_qqq"] = {"daily_full": float(btc_s.corr(q_s)), "weekly_full": float(wk_btc.corr(wk_q)),
                           "monthly_full": float(monthly(btc_s).corr(monthly(q_s))),
                           "rolling252_median": float(rc.median()), "rolling252_min": float(rc.min()),
                           "rolling252_max": float(rc.max()), "rolling252_last": float(rc.iloc[-1])}
    res["context"] = ctx
    halv = pd.DataFrame(bt.halving_table(p, bd.HALVINGS))
    res["halvings"] = halv.to_dict("records")
    res["BT6_weekend"] = {k: bt.weekend_test(btc_cal.loc[a:b]) for k, (a, b) in ({"full": (ENTRY_RET, END)} | FOLDS).items()}
    etf_track = {}
    etf = {}
    for sym in bd.ETFS:
        e = etf_series(sym, sessions)
        etf[sym] = e
        a = pd.concat([e["ret"], bd.on_sessions(p, sessions).pct_change(fill_method=None)], axis=1).loc[IBIT_FROM:].dropna()
        a.columns = ["etf", "btc"]
        wa = (1 + a).groupby(a.index.to_period("W-FRI")).prod() - 1
        etf_track[sym] = {"first_close": e["first"], "window": [str(a.index[0].date()), str(a.index[-1].date())],
                          "cagr_etf": float((1 + a["etf"]).prod() ** (252 / len(a)) - 1),
                          "cagr_btc_primary": float((1 + a["btc"]).prod() ** (252 / len(a)) - 1),
                          "daily_corr": float(a["etf"].corr(a["btc"])), "weekly_corr": float(wa["etf"].corr(wa["btc"])),
                          "tracking_error_daily_ann": float((a["etf"] - a["btc"]).std() * np.sqrt(252)),
                          "tracking_error_weekly_ann": float((wa["etf"] - wa["btc"]).std() * np.sqrt(52))}
    res["etf_tracking"] = etf_track
    log.say("context", json.dumps({k: {h: round(v[h]["cagr"], 4) for h in ("full", "H1", "H2")}
                                   for k, v in ctx.items() if k in ("BTC", "QQQ", "ONEQ")}))

    # ---- rules
    alt = {"coinbase": bd.source_close(src, "coinbase", p), "yahoo": bd.source_close(src, "yahoo", p)}
    rows, sens_rows, by_year, mon = [], [], {}, {}
    for spec in bt.SPECS:
        stand = spec.family == "standalone"
        run_one = (lambda series, **kw: run_standalone(spec.name, series, rf, sessions, **kw)) if stand else \
            (lambda series, **kw: run_sleeve(spec, series, d, sessions, **kw))
        sim = run_one(p)
        r = sim["ret_sessions"]
        assert r.index.equals(oneq.index), spec.name
        main = judge(r, oneq, qqq, rf)
        second_bench, tag = (btc_bh, "btcbh") if stand else (qqq, "qqq")
        sec = judge(r, second_bench, qqq, rf)
        sec = {"metrics": {k: rekey(v, tag) for k, v in sec["metrics"].items()}, "verdict": sec["verdict"]}
        years = (r.index[-1] - r.index[0]).days / 365.25
        held = sim["held"]["BTC"]
        info = {"description": spec.description, "family": spec.family, "vs_oneq": main, f"vs_{tag}": sec,
                "trades": sim["trades"], "orders": sim["orders"], "cost_dollars": sim["cost_dollars"],
                "paxos_fee_dollars": sim["fee_dollars"], "final_value": float(sim["value"].iloc[-1]),
                "cost_drag_per_year_approx": sim["cost_dollars"] / float(sim["value"].mean()) / years,
                "trades_per_year": sim["trades"] / years, "avg_btc_weight": float(held.mean()),
                "share_rows_btc_held": float((held > 1e-9).mean())}
        if not stand:
            port = r
            info["risk_contribution_btc"] = {
                k: bt.risk_contribution(sim["btc_ret"].loc[a:b], port.loc[a:b], held.loc[a:b])
                for k, (a, b) in ({"full": (ENTRY_RET, END)} | FOLDS).items()}
            info["dd_ok_vs_qqq"] = all(main["metrics"][h]["max_dd"] >= main["metrics"][h]["qqq_max_dd"] - DD_OK_PP / 100
                                       for h in FOLDS)
        # report-only sensitivities
        sens = {"no_costs": run_one(p, costs=False), "btc_half_spread_25bp": run_one(p, btc_hs=SENS_BTC_HS),
                "cash_zero_rate": run_one(p, cash_zero=True),
                "data_coinbase": run_one(alt["coinbase"]), "data_yahoo": run_one(alt["yahoo"])}
        if not stand:
            sens["ibit_from_2024"] = run_one(p, etf=etf["IBIT"])
        info["sensitivities"] = {}
        for k, s in sens.items():
            j = short(judge(s["ret_sessions"].reindex(oneq.index), oneq, qqq, rf))
            info["sensitivities"][k] = j
            sens_rows.append({"rule": spec.name, "sensitivity": k, **j})
        res["rules"][spec.name] = info
        for h in ("full", "H1", "H2"):
            m = main["metrics"][h]
            rows.append({"rule": spec.name, "period": h, "start": m["start"], "end": m["end"], "cagr": m["cagr"],
                         "oneq_cagr": m["oneq_cagr"], "qqq_cagr": m["qqq_cagr"], "cagr_minus_oneq": m["cagr_minus_oneq"],
                         "cagr_minus_qqq": m["cagr_minus_qqq"], "vol": m["vol"], "max_dd": m["max_dd"],
                         "oneq_max_dd": m["oneq_max_dd"], "qqq_max_dd": m["qqq_max_dd"],
                         "dd_shallower_than_oneq_pp": m["dd_shallower_than_oneq_pp"], "sharpe": m["sharpe"],
                         "t_vs_oneq": m["t_monthly_excess_vs_oneq"], "t_vs_qqq": m["t_monthly_excess_vs_qqq"],
                         "second_bench": tag, "cagr_minus_second": sec["metrics"][h][f"cagr_minus_{tag}"],
                         "max_dd_second": sec["metrics"][h][f"{tag}_max_dd"],
                         "t_vs_second": sec["metrics"][h][f"t_monthly_excess_vs_{tag}"],
                         "pass_vs_oneq": main["verdict"]["pass"] if h == "full" else None,
                         "pass_vs_oneq_bonferroni": main["verdict"]["pass_bonferroni"] if h == "full" else None,
                         "pass_vs_second": sec["verdict"]["pass"] if h == "full" else None})
        by_year[spec.name], mon[spec.name] = yearly(r), monthly(r)
        f = main["metrics"]["full"]
        log.say(f"{spec.name:5s} CAGR {f['cagr']:+.2%} (ONEQ {f['oneq_cagr']:+.2%} QQQ {f['qqq_cagr']:+.2%}) MDD {f['max_dd']:.1%}"
                f" t {f['t_monthly_excess_vs_oneq']:+.2f} | H1 {main['metrics']['H1']['cagr_minus_oneq']:+.2%}"
                f" H2 {main['metrics']['H2']['cagr_minus_oneq']:+.2%} | pass {main['verdict']['pass']}"
                f" (bonf {main['verdict']['pass_bonferroni']}) | vs {tag}: {sec['verdict']['pass']}"
                f" ({sec['metrics']['full'][f'cagr_minus_{tag}']:+.2%}, t {sec['metrics']['full'][f't_monthly_excess_vs_{tag}']:+.2f})"
                f" | trades {sim['trades']}")
    for tag, r in (("BTC_buyhold", btc_bh), ("QQQ_buyhold", qqq), ("ONEQ_buyhold", oneq)):
        by_year[tag], mon[tag] = yearly(r), monthly(r)
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "results.json", res, default=float)
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    pd.DataFrame(sens_rows).to_csv(OUT / "sensitivities.csv", index=False, float_format="%.6f")
    halv.to_csv(OUT / "halvings.csv", index=False, float_format="%.4f")
    corr_year.to_csv(OUT / "corr_btc_qqq_by_year.csv", float_format="%.4f")
    write_period_tables(OUT, by_year, mon)
    log.write(OUT / "run_log.txt")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fetch", action="store_true", help="download missing raw files first (cached, logged)")
    ap.add_argument("--data-only", action="store_true", help="only the source coverage / agreement report")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
