"""Intraday minute-bar rules on QQQ vs ONEQ (pre-registered in docs/research_ledger_intraday_minute.md, section 0).

Rules (quant.strategies.intraday): M1a/b 30-minute opening-range breakout (long / long-short), M1c 5-minute ORB
(Zarattini & Aziz), M2a/b/c last-half-hour intraday momentum, M3a/b VWAP reversion, M4 noise-area band (Zarattini,
Aziz & Barbon), M5 = M1a with a 200-day SMA regime filter. Each judged stand-alone (flat overnight, cash earns the
T-bill rate) and as a combo (long QQQ overnight + the rule intraday): 20 trials.
Execution: decisions on completed 1-minute bars, fills at the next bar's open (resting stops at the stop or a worse
open; exits "at the close" at the last regular-hours bar's close), slippage max(1 cent, 1 bp) per share (2x / 3x
sensitivities), IBKR Tiered ``ibkr_order_cost`` per order, integer shares, $10,000. Accounts: margin (primary),
margin with the PDT cap (3 day trades per 5 sessions under $25k), cash (settled cash only; T+2 / T+1; no shorts).
Data (local only): Alpaca SIP minute bars research_cache/intraday_minute/ (QQQ; SPY, TQQQ for reference),
validated against Yahoo daily OHLC (research_cache/sector_lev/raw). Outputs: output/research_only/intraday_minute/
(returns, metrics and agreement statistics only, never price levels).

Usage:  PYTHONPATH=. .venv/bin/python -m quant study intraday_minute [--fetch] [--data-only]
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from quant.backtest.costs import etf_order_cost
from quant.data import intraday_minute as im
from quant.data import sector_etfs as etfs
from quant.data.guards import assert_dev_dates
from quant.data.sources.yahoo import parse_ohlc
from quant.evaluation.criteria import ab_verdict, bonferroni_t
from quant.evaluation.equity import period_metrics
from quant.evaluation.metrics import monthly, yearly
from quant.paths import ROOT, output_dir
from quant.reporting import Preregistration, RunLog, write_json, write_period_tables
from quant.strategies import intraday as it

END = "2026-09-30"
FIRST_MONTH, LAST_MONTH = "2016-01", "2026-09"
SYMBOLS = ("QQQ", "SPY", "TQQQ")
EVAL_START = "2016-01-05"                  # 2016-01-04 is the warm-up day (combo buys its first overnight position)
FOLDS = {"H1": (EVAL_START, "2020-12-31"), "H2": ("2021-01-01", END)}
N_TRIALS = 2 * len(it.RULES)
SLIP_SENS = (2.0, 3.0)
ONEQ_HS, QQQ_HS = 2e-4, 1e-4
NOISE_LOOKBACK = 14
OUT = output_dir("intraday_minute")
LEDGER = ROOT / "docs/research_ledger_intraday_minute.md"
ACCOUNTS = {"margin": it.Account("margin"), "margin_pdt": it.Account("margin", pdt=True), "cash": it.Account("cash")}


# ======================================================================== data

def yahoo_daily(symbol: str) -> pd.DataFrame:
    df = parse_ohlc(etfs.RAW / f"chart_{symbol}.json", END)
    df = df.set_index("date")
    df.index = df.index.astype(str)
    return df


def validate(symbol: str, rth: pd.DataFrame, log: RunLog) -> dict:
    """Daily OHLC rebuilt from regular-hours minutes vs Yahoo daily bars (scaled to Yahoo's dividend-adjusted level,
    because the minute bars are requested with adjustment=all). Differences in basis points."""
    mins = im.daily_from_minutes(rth)
    ext = im.extended_daily(symbol, FIRST_MONTH, LAST_MONTH, END)
    out = {"sessions_in_minutes": int(len(mins)), "first": mins.index[0], "last": mins.index[-1]}
    exp_len = np.array([im.session_length(d) for d in mins.index])
    out["bars_complete_share"] = float((mins["bars"] == exp_len).mean())
    out["bars_missing_total"] = int((exp_len - mins["bars"]).sum())
    out["sessions_below_95pct_bars"] = int((mins["bars"] < 0.95 * exp_len).sum())
    out["mean_bars_present_share"] = float((mins["bars"] / exp_len).mean())
    share = (ext["late_volume"] / ext["rth_volume"]).reindex(mins.index)
    listed = share[share.index.isin(im.EARLY_CLOSE)]
    other = share[~share.index.isin(im.EARLY_CLOSE)]
    out["sessions_not_traded"] = sorted(mins.index[mins["bars"] < it.MIN_BARS_SHARE * exp_len])
    out["early_close"] = {"listed_in_data": int(len(listed)), "listed_max_13_16_volume_share": float(listed.max()),
                          "unlisted_min_13_16_volume_share": float(other.min()),
                          "unlisted_days_below_10pct": sorted(other.index[other < 0.10])}
    if symbol == "TQQQ":
        return out
    y = yahoo_daily(symbol)
    y = y.loc[y.index >= mins.index[0]]
    out["yahoo_sessions"] = int(len(y))
    out["yahoo_sessions_missing_in_minutes"] = sorted(set(y.index) - set(mins.index))
    out["minute_sessions_missing_in_yahoo"] = sorted(set(mins.index) - set(y.index))
    j = mins.join(y, rsuffix="_y", how="inner")
    k, level, period = minute_units(mins, y)
    rows = {}
    for col in ("open", "high", "low", "close"):
        diff = (j[col] / (j[f"{col}_y"] * k) - 1) * 1e4
        rows[col] = diff
    cb = ext["close_bar_open"].reindex(j.index)
    rows["close_bar_print"] = (cb / (j["close_y"] * k) - 1) * 1e4
    agree = {}
    for col, diff in rows.items():
        a = diff.abs().dropna()
        agree[col] = {"n": int(len(a)), "median_abs_bp": float(a.median()), "p95_abs_bp": float(a.quantile(0.95)),
                      "share_within_1bp": float((a <= 1).mean()), "share_within_5bp": float((a <= 5).mean()),
                      "share_within_10bp": float((a <= 10).mean()), "max_abs_bp": float(a.max()),
                      "max_day": str(a.idxmax())}
    r_min = j["close"].pct_change()
    r_y = j["adjclose"].pct_change()
    d = ((r_min - r_y) * 1e4).abs().dropna()
    agree["close_to_close_return"] = {"n": int(len(d)), "median_abs_bp": float(d.median()),
                                      "share_within_2bp": float((d <= 2).mean()), "share_within_10bp": float((d <= 10).mean()),
                                      "max_abs_bp": float(d.max()), "max_day": str(d.idxmax()),
                                      "corr": float(r_min.corr(r_y))}
    out["vs_yahoo"] = agree
    out["adjusted_level_ratio_alpaca_over_yahoo"] = {"min": float(level.min()), "max": float(level.max()),
                                                     "dividend_periods": int(period.nunique())}
    out["early_close"]["listed_max_abs_close_diff_bp"] = float(rows["close"][rows["close"].index.isin(im.EARLY_CLOSE)].abs().max())
    pd.DataFrame(rows).to_csv(OUT / f"validation_daily_diff_bp_{symbol}.csv", float_format="%.3f")
    for col, a in agree.items():
        log.say(f"{symbol} {col:22s} median |diff| {a['median_abs_bp']:.2f} bp; "
                + ", ".join(f"{k} {v:.1%}" for k, v in a.items() if k.startswith("share")) + f"; max {a['max_abs_bp']:.1f} bp on {a['max_day']}")
    return out


def minute_units(mins: pd.DataFrame, y: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Per day, minute-bar price units per Yahoo raw (split-adjusted) unit. Alpaca (adjustment=all) and Yahoo adjust
    for dividends with slightly different factors; the ratio of the two adjusted levels is constant between
    ex-dividend dates, so it is taken per dividend period (median ratio)."""
    j = mins.join(y, rsuffix="_y", how="inner")
    period = (j["dividend"] > 0).cumsum()
    level = j["close"] / j["adjclose"]
    scale = level.groupby(period).transform("median")
    return j["adjclose"] / j["close_y"] * scale, level, period


def build_days(rth: pd.DataFrame, y: pd.DataFrame | None) -> list[it.Day]:
    """Days in minute-bar (adjusted) units; ``factor`` = raw price per adjusted unit (Yahoo close / adjclose). A
    session with too few minutes is not traded by the rules; its open and close (for the combo's overnight leg) are
    Yahoo's daily open and close in minute units."""
    k = minute_units(im.daily_from_minutes(rth), y)[0] if y is not None else None
    days, prev = [], float("nan")
    for date, g in rth.groupby("date", sort=True):
        n = im.session_length(date)
        f = float(y.loc[date, "close"] / y.loc[date, "adjclose"]) if y is not None and date in y.index else 1.0
        d = it.make_day(date, g["minute"].values, g["open"].values, g["high"].values, g["low"].values,
                        g["close"].values, g["volume"].values, g["vwap"].values, n, prev, f)
        if not d.usable and k is not None and date in k.index:
            o, c = float(y.loc[date, "open"] * k[date]), float(y.loc[date, "close"] * k[date])
            d.o[:] = d.h[:] = d.l[:] = d.c[:] = d.vw[:] = c
            d.o[0] = o
        days.append(d)
        prev = d.c[-1]
    return days


def moves_matrix(days: list[it.Day]) -> np.ndarray:
    m = np.full((len(days), 390), np.nan)
    for t, d in enumerate(days):
        if d.usable:
            m[t, :d.n] = np.abs(d.c / d.o[0] - 1)
    return m


# ======================================================================== evaluation

def judge(r: pd.Series, oneq: pd.Series, qqq: pd.Series, rf: pd.Series) -> dict:
    parts = {"full": (EVAL_START, END)} | FOLDS
    m = {k: period_metrics(r.loc[a:b], oneq.loc[a:b], qqq.loc[a:b], rf) for k, (a, b) in parts.items()}
    v = ab_verdict(m["full"], m["H1"], m["H2"])
    hurdle = bonferroni_t(N_TRIALS)
    a_b = all(m[k]["cagr"] > m[k]["oneq_cagr"] for k in m) and m["full"]["t_monthly_excess_vs_oneq"] >= hurdle
    v["A_bonferroni"] = bool(a_b)
    v["pass_bonferroni"] = bool(a_b or v["B"])
    return {"metrics": m, "verdict": v}


def trade_stats(sim: it.SimResult, years: float) -> dict:
    t, st = sim.trades, sim.stats
    mean_eq = float(sim.equity.mean())
    out = {"trades": int(len(t)), "trades_per_year": len(t) / years,
           "time_in_market": st["minutes_in_market"] / max(st["minutes_total"], 1),
           "cost_dollars": st["cost_dollars"], "slippage_dollars": st["slippage_dollars"],
           "cost_drag_per_year": (st["cost_dollars"] + st["slippage_dollars"]) / mean_eq / years,
           "orders": st["orders"], "day_trades": st["day_trades"], "skipped_pdt": st["skipped_pdt"],
           "pdt_binding_days": st["pdt_binding_days"], "skipped_short_cash": st["skipped_short_cash"],
           "skipped_no_cash": st["skipped_no_cash"], "overnight_not_bought": st["overnight_not_bought"],
           "final_equity": st["final_equity"]}
    if len(t):
        out.update({"win_rate_net": float((t["net_ret"] > 0).mean()), "win_rate_gross": float((t["gross_ret"] > 0).mean()),
                    "avg_gross_bp": float(t["gross_ret"].mean() * 1e4), "avg_net_bp": float(t["net_ret"].mean() * 1e4),
                    "t_avg_gross": float(t["gross_ret"].mean() / t["gross_ret"].std() * np.sqrt(len(t))) if len(t) > 2 else float("nan"),
                    "share_long": float((t["side"] > 0).mean()),
                    "exit_kinds": t["kind"].value_counts().to_dict()})
    return out


def short(j: dict) -> dict:
    f = j["metrics"]["full"]
    return {"cagr": f["cagr"], "max_dd": f["max_dd"], "cagr_minus_oneq": f["cagr_minus_oneq"],
            "t_vs_oneq": f["t_monthly_excess_vs_oneq"], "H1_cagr_minus_oneq": j["metrics"]["H1"]["cagr_minus_oneq"],
            "H2_cagr_minus_oneq": j["metrics"]["H2"]["cagr_minus_oneq"], "pass": j["verdict"]["pass"],
            "pass_bonferroni": j["verdict"]["pass_bonferroni"]}


# ======================================================================== main

def run(args=None) -> dict:
    log = RunLog()
    OUT.mkdir(parents=True, exist_ok=True)
    if getattr(args, "fetch", False):
        im.fetch(SYMBOLS, FIRST_MONTH, LAST_MONTH, log=log.say)
    rth = {s: im.load_rth(s, FIRST_MONTH, LAST_MONTH, END) for s in SYMBOLS}
    for s, f in rth.items():
        assert_dev_dates(pd.to_datetime(f["date"].drop_duplicates()), END)
    data_info = {s: validate(s, rth[s], log) for s in SYMBOLS}
    write_json(OUT / "data_quality.json", data_info, default=float)
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
    days = build_days(rth["QQQ"], yahoo_daily("QQQ"))      # QQQ had no split in 2016-2026
    dates = pd.DatetimeIndex([x.date for x in days])
    start = int(dates.get_loc(pd.Timestamp(EVAL_START)))
    assert start == 1, "2016-01-04 must be the only warm-up session"
    rf = d.rf.reindex(dates).fillna(0.0).values
    sigma = it.noise_sigma(moves_matrix(days), NOISE_LOOKBACK)
    regime = it.regime_flags(d.adj["QQQ"].dropna()).reindex(dates).fillna(False)
    trades = {}
    for spec in it.RULES + it.CONTEXT:
        trades[spec.name] = [it.rule_trades(spec, x, sigma[t], bool(regime.iloc[t])) for t, x in enumerate(days)]

    # benchmarks: bought at the 2016-01-04 close, with costs
    ev = dates[start:]
    def bh(sym, hs):
        r = d.rets[sym].reindex(ev).copy()
        r.iloc[0] = (1 + r.iloc[0]) * (1 - etf_order_cost(10_000, float(d.close[sym].loc[dates[0]]), False, hs) / 10_000) - 1
        return r
    oneq, qqq = bh("ONEQ", ONEQ_HS), bh("QQQ", QQQ_HS)
    rf_s = d.rf.reindex(ev)
    years = (ev[-1] - ev[0]).days / 365.25
    res = {"end": END, "eval_start": EVAL_START, "folds": FOLDS, "n_trials": N_TRIALS,
           "bonferroni_t_one_sided_5pct": bonferroni_t(N_TRIALS), "prereg_sha256": frozen["sha256"],
           "data": {s: {k: v for k, v in data_info[s].items() if k != "vs_yahoo"} for s in SYMBOLS},
           "benchmarks": {"ONEQ": short(judge(oneq, oneq, qqq, rf_s)), "QQQ": short(judge(qqq, oneq, qqq, rf_s))},
           "rules": {}}
    rows, sens_rows, by_year, mon = [], [], {}, {}

    def sim(name, wrapper, acct="margin", slip=1.0, rfa=rf, costs=True):
        return it.simulate(days, trades[name], rfa, account=ACCOUNTS[acct], combo=(wrapper == "combo"),
                           slip_mult=slip, start=start, costs=costs)

    for spec in it.RULES + it.CONTEXT:
        info = {"description": spec.description, "context_only": spec in it.CONTEXT}
        for wrapper in ("standalone", "combo"):
            key = f"{spec.name}_{wrapper}"
            runs = {acct: sim(spec.name, wrapper, acct) for acct in ACCOUNTS}
            main = judge(runs["margin"].ret, oneq, qqq, rf_s)
            w = {"vs_oneq": main, "trade_stats": {a: trade_stats(s, years) for a, s in runs.items()},
                 "accounts": {a: short(judge(s.ret, oneq, qqq, rf_s)) for a, s in runs.items()}}
            sens = {f"slip_{m:g}x": sim(spec.name, wrapper, "margin", m) for m in SLIP_SENS}
            sens["gross_no_costs"] = sim(spec.name, wrapper, "margin", 0.0, costs=False)
            if wrapper == "standalone":
                sens["cash_zero_interest"] = sim(spec.name, wrapper, "margin", 1.0, np.zeros(len(days)))
            w["sensitivities"] = {k: short(judge(s.ret, oneq, qqq, rf_s)) for k, s in sens.items()}
            g = w["sensitivities"]["gross_no_costs"]["cagr"]
            w["cagr_cost_drag"] = g - main["metrics"]["full"]["cagr"]
            info[wrapper] = w
            for k, s in w["sensitivities"].items():
                sens_rows.append({"rule": spec.name, "wrapper": wrapper, "variant": k, **s})
            for a, s in w["accounts"].items():
                sens_rows.append({"rule": spec.name, "wrapper": wrapper, "variant": f"account_{a}", **s})
            for h in ("full", "H1", "H2"):
                m = main["metrics"][h]
                ts = w["trade_stats"]["margin"]
                rows.append({"rule": spec.name, "wrapper": wrapper, "period": h, "start": m["start"], "end": m["end"],
                             "cagr": m["cagr"], "oneq_cagr": m["oneq_cagr"], "qqq_cagr": m["qqq_cagr"],
                             "cagr_minus_oneq": m["cagr_minus_oneq"], "vol": m["vol"], "max_dd": m["max_dd"],
                             "oneq_max_dd": m["oneq_max_dd"], "sharpe": m["sharpe"],
                             "t_vs_oneq": m["t_monthly_excess_vs_oneq"],
                             "pass": main["verdict"]["pass"] if h == "full" else None,
                             "pass_bonferroni": main["verdict"]["pass_bonferroni"] if h == "full" else None,
                             "trades_per_year": ts["trades_per_year"] if h == "full" else None,
                             "win_rate_net": ts.get("win_rate_net") if h == "full" else None,
                             "avg_gross_bp": ts.get("avg_gross_bp") if h == "full" else None,
                             "avg_net_bp": ts.get("avg_net_bp") if h == "full" else None,
                             "time_in_market": ts["time_in_market"] if h == "full" else None,
                             "cost_drag_per_year": ts["cost_drag_per_year"] if h == "full" else None,
                             "context_only": spec in it.CONTEXT})
            by_year[key], mon[key] = yearly(runs["margin"].ret), monthly(runs["margin"].ret)
            f = main["metrics"]["full"]
            ts = w["trade_stats"]
            log.say(f"{key:28s} CAGR {f['cagr']:+.2%} (ONEQ {f['oneq_cagr']:+.2%}) MDD {f['max_dd']:.1%} t {f['t_monthly_excess_vs_oneq']:+.2f}"
                    f" H1 {main['metrics']['H1']['cagr_minus_oneq']:+.2%} H2 {main['metrics']['H2']['cagr_minus_oneq']:+.2%}"
                    f" pass {main['verdict']['pass']} | tr/yr {ts['margin']['trades_per_year']:.0f}"
                    f" avg gross {ts['margin'].get('avg_gross_bp', float('nan')):+.1f}bp net {ts['margin'].get('avg_net_bp', float('nan')):+.1f}bp"
                    f" win {ts['margin'].get('win_rate_net', float('nan')):.0%} | gross CAGR {g:+.2%}"
                    f" | PDT {w['accounts']['margin_pdt']['cagr']:+.2%} cash {w['accounts']['cash']['cagr']:+.2%}")
        res["rules"][spec.name] = info

    # report-only: the same rules on SPY and TQQQ, stand-alone, gross per-trade statistics and net CAGR (margin)
    ref = {}
    for sym in ("SPY", "TQQQ"):
        sd = build_days(rth[sym], yahoo_daily("SPY") if sym == "SPY" else None)
        sg = it.noise_sigma(moves_matrix(sd), NOISE_LOOKBACK)
        sdates = pd.DatetimeIndex([x.date for x in sd])
        s0 = int(sdates.get_loc(pd.Timestamp(EVAL_START)))
        reg = it.regime_flags(d.adj["QQQ"].dropna()).reindex(sdates).fillna(False)
        ref[sym] = {}
        for spec in it.RULES:
            tr = [it.rule_trades(spec, x, sg[t], bool(reg.iloc[t])) for t, x in enumerate(sd)]
            sres = it.simulate(sd, tr, d.rf.reindex(sdates).fillna(0.0).values, start=s0)
            gres = it.simulate(sd, tr, np.zeros(len(sd)), start=s0, slip_mult=0.0, costs=False)
            st = trade_stats(sres, years)
            ref[sym][spec.name] = {"cagr_net": float((1 + sres.ret).prod() ** (1 / years) - 1),
                                   "cagr_gross_no_rf": float((1 + gres.ret).prod() ** (1 / years) - 1),
                                   **{k: st.get(k) for k in ("trades_per_year", "win_rate_gross", "avg_gross_bp", "avg_net_bp", "t_avg_gross")}}
            log.say(f"ref {sym:4s} {spec.name:4s} net CAGR {ref[sym][spec.name]['cagr_net']:+.2%} gross {ref[sym][spec.name]['cagr_gross_no_rf']:+.2%}"
                    f" avg gross {st.get('avg_gross_bp', float('nan')):+.1f}bp (t {st.get('t_avg_gross', float('nan')):+.2f})")
    res["reference_symbols"] = ref
    for tag, r in (("ONEQ_buyhold", oneq), ("QQQ_buyhold", qqq)):
        by_year[tag], mon[tag] = yearly(r), monthly(r)
    write_json(OUT / "results.json", res, default=float)
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    pd.DataFrame(sens_rows).to_csv(OUT / "sensitivities.csv", index=False, float_format="%.6f")
    write_period_tables(OUT, by_year, mon)
    log.write(OUT / "run_log.txt")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fetch", action="store_true", help="download missing minute bars first (cached, logged)")
    ap.add_argument("--data-only", action="store_true", help="only the data-quality report")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
