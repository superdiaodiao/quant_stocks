"""Mega-cap momentum variants M3, M4, M5 out of sample 1999-2013 on the OOS.2 point-in-time Nasdaq ranking.

Pre-registered in docs/research_ledger_megacap.md, section OOS.3. Rules unchanged from section 0.4:
  M3 among the top 20 by market cap, the 5 with the best 6-month (126-session) total return, equal weight, monthly
  M4 among the top 20, the 10 with the best 12-1 momentum (t-252 .. t-21), equal weight
  M5 M3, but 100% QQQ when QQQ closes below its 200-session SMA on the signal day
Data, execution, costs, benchmark and criteria as OOS.2 (scripts/research_megacap_oos2.py), with two registered
additions: (1) M4's 252-session look-back is served by total-return history before 1998-06-01 from each name's own
OOS.2 source (used for momentum only; quant.data.megacap_lookback); (2) M5's SMA warm-up uses the Nasdaq-100 index
(^NDX) scaled to QQQ before QQQ's first close on 1999-03-10; a QQQ signal before QQQ trades is held as cash
(quant.strategies.megacap.trend_series / apply_trend). Judged vs QQQ 1999-03-10..2013-12-31:
A = CAGR above QQQ and monthly excess t >= 2.39 (Bonferroni for 6 tests on this sample), or B = max drawdown >= 10 pp
shallower and CAGR within 3 pp. ONEQ (2003-10..2013) is reported only.

Vendor price levels stay under research_cache/megacap_oos3/ (local only); git gets returns, ranks, IDs and flags.

Usage:  PYTHONPATH=. .venv/bin/python scripts/research_megacap_oos3.py [--check-data]
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

from quant.data import megacap_history as o2     # the OOS.2 data build, ranking and period statistics
from quant.data.megacap_lookback import extended_index, lookback_extensions, yahoo_pre
from quant.data.sources.yahoo import parse_chart
from quant.evaluation.metrics import yearly
from quant.paths import CACHE_ROOT, output_dir
from quant.signals.technical import momentum_frames
from quant.strategies import megacap as mc

# ======================================================================== frozen configuration

OUT = output_dir("megacap_oos3")
LOCAL = CACHE_ROOT / "megacap_oos3"
NDX_CHART = CACHE_ROOT / "qqq_timing/raw/chart_%5ENDX.json"
RULES = tuple(r for r in mc.RULES if r.name in ("M3", "M4", "M5"))
T_THRESHOLD = 2.39            # one-sided 5% Bonferroni, 6 tests on 1999-2013 (OOS.2's 3 + these 3)
T_BATCH = 2.13                # this batch of 3 alone (reported tier, not the rule)
PRE_FROM, PRE_TO = "1997-09-01", "1998-07-31"
PRE_URL_TO = "1998-08-01"     # exclusive end of the Yahoo request
EXTEND_UNTIL_SIGNAL = "1999-05-28"     # the last signal whose 252-session look-back reaches before 1998-06-01
QQQ_FIRST = pd.Timestamp(o2.JUDGE_FROM)
CRASH = ("2000-03-10", "2002-10-09")


def criteria(full: dict) -> dict:
    up = full["cagr"] > full["bench_cagr"]
    t = full["t_monthly_excess"]
    a = up and t >= T_THRESHOLD
    b = (abs(full["bench_max_dd"]) - abs(full["max_dd"])) * 100 >= 10 and full["cagr"] >= full["bench_cagr"] - 0.03
    tier = ("A" if a else "batch-only (2.13<=t<2.39)" if up and t >= T_BATCH else
            "nominal (2<=t<2.13)" if up and t >= 2.0 else "no")
    return {"A": bool(a), "A_tier": tier, "B": bool(b), "pass": bool(a or b)}


# ======================================================================== run

def run(args) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    (LOCAL / "built").mkdir(parents=True, exist_ok=True)
    cand, sessions, panel, srcs, facts, metas = o2.build()
    signals = o2.signal_sessions(sessions)
    mcaps = o2.market_caps(cand, facts, panel, signals)
    ranks = o2.rank_table(mcaps, cand)
    ref = pd.read_csv(output_dir("megacap_oos2") / "ranks_top20_by_month.csv")
    same = len(ref) == len(ranks) and (ranks[["s", "rank", "key"]].to_numpy() == ref[["s", "rank", "key"]].to_numpy()).all()
    if not same:
        raise SystemExit("ranking differs from OOS.2 ranks_top20_by_month.csv: stop (registered check)")
    print("ranking identical to OOS.2:", same)
    idx, close, last_row, term = o2.engine_panels(cand, sessions, panel, facts)

    # ---- M4 look-back extension (registered): names in the top 20 at signals up to 1999-05-28
    msft_pre = yahoo_pre("MSFT", LOCAL, PRE_FROM, PRE_TO, PRE_URL_TO)
    cal_pre = pd.DatetimeIndex(msft_pre.index[msft_pre.index < sessions[0]])
    early = sorted(set(ranks.loc[ranks["s"] <= EXTEND_UNTIL_SIGNAL, "key"]))
    extend, ext_log = lookback_extensions(early, idx, panel, cand, sessions, cal_pre, LOCAL, PRE_FROM, PRE_TO,
                                          PRE_URL_TO)
    ext_log.to_csv(OUT / "m4_lookback_extension.csv", index=False)
    print(ext_log.to_string())
    if args.check_data:
        return {}
    idx_mom = extended_index(idx, extend, cal_pre)
    mom = momentum_frames(idx_mom.ffill())
    ranked = mcaps.rename(columns={"key": "security_id", "src": "mcap_src"}).assign(dv50_rank=1.0)

    # ---- benchmarks and the M5 filter
    qqq, oneq = o2.load_qqq(), o2.load_oneq()
    qqq_close = qqq["close"].reindex(sessions)
    qqq_lvl = qqq["adjclose"].reindex(sessions)
    ndx = parse_chart(NDX_CHART, o2.END)
    ndx = ndx.assign(date=pd.to_datetime(ndx["date"])).set_index("date")["close"]
    trs = mc.trend_series(qqq["close"], ndx, QQQ_FIRST)
    first_qqq_exec = qqq.index.min()

    results, by_year, holds, navs, filt = {}, [], [], {}, None
    targets = {}
    for rule in RULES:
        if rule.name == "M5":
            base = mc.build_targets(next(r for r in RULES if r.name == "M3"), ranked, mom, qqq_close)
            tg, filt = mc.apply_trend(base, trs, first_qqq_exec, sessions)
            filt.to_csv(OUT / "m5_filter_by_signal.csv", index=False)
        else:
            tg = mc.build_targets(rule, ranked, mom, qqq_close)
        targets[rule.name] = tg
        sim = mc.simulate(tg, sessions, idx, close, last_row, qqq_lvl.fillna(1.0), qqq_close.fillna(50.0))
        nav = sim["nav"]
        held_qqq_early = any(lst and lst[0][0] == "QQQ" and sessions[sessions > s][0] < first_qqq_exec
                             for s, lst in tg.items())
        assert not held_qqq_early, "QQQ leg before QQQ trades"
        jd = nav.index[nav.index >= QQQ_FIRST]
        bq = mc.buy_hold(qqq["adjclose"], qqq["close"], jd, mc.QQQ_HS)
        od = nav.index[nav.index >= oneq.index.min()]
        bo = mc.buy_hold(oneq["adjclose"], oneq["close"], od, mc.ONEQ_HS)
        per = {p: o2.period_stats(nav, bq, a, b) for p, (a, b) in o2.PERIODS.items()}
        per_oneq = o2.period_stats(nav, bo, o2.ONEQ_FROM, o2.END)
        crit = criteria(per[o2.FULL])
        r = nav.pct_change().dropna()
        yq, yo, ys = yearly(bq.pct_change().dropna()), yearly(bo.pct_change().dropna()), yearly(r)
        r_j = nav[nav.index >= QQQ_FIRST].pct_change().dropna()
        for y, v in ys.items():
            by_year.append({"rule": rule.name, "year": y, "strategy": v, "qqq": yq.get(y), "oneq": yo.get(y),
                            "strategy_from_1999_03_10": yearly(r_j).get(y) if y == 1999 else v})
        crash = mc.crash_returns({"strategy": nav, "qqq": bq}, *CRASH)
        vj = nav[nav.index >= QQQ_FIRST]
        peak = (vj / vj.cummax() - 1)
        trough = peak.idxmin()
        peak_d = vj[:trough].idxmax()
        rec = vj[trough:][vj[trough:] >= vj.loc[peak_d]]
        for s, lst in tg.items():
            for k, (sid, w, rk, mcap, srcx) in enumerate(lst, 1):
                holds.append({"rule": rule.name, "signal": s.date().isoformat(), "slot": k, "key": sid,
                              "weight": round(w, 4), "mcap_src": srcx})
            if not lst:
                holds.append({"rule": rule.name, "signal": s.date().isoformat(), "slot": 0, "key": "CASH",
                              "weight": 1.0, "mcap_src": ""})
        navs[rule.name] = nav
        gross = mc.vector_gross(tg, sessions, idx, qqq_lvl.ffill())
        results[rule.name] = {"start": str(sim["start"].date()), "periods": per, "vs_oneq_2003_10_2013": per_oneq,
                              "criteria_vs_qqq": crit, "crash_2000_03_10_to_2002_10_09": crash,
                              "max_dd_full": {"peak": str(peak_d.date()), "trough": str(trough.date()),
                                              "recovered": str(rec.index[0].date()) if len(rec) else "not by 2013-12-31"},
                              "avg_names_held": float(sim["names"].mean()), "orders": int(sim["orders"].sum()),
                              "cost_usd": float(sim["cost"].sum()),
                              "check_vector_gross_vs_engine": {"vector_gross_growth": gross,
                                                               "engine_net_growth": float(nav.iloc[-1] / mc.ACCOUNT)}}
        f = per[o2.FULL]
        print(f"{rule.name}: start {sim['start'].date()} full CAGR {f['cagr']:+.1%} vs QQQ {f['bench_cagr']:+.1%} | "
              f"MDD {f['max_dd']:.1%} vs {f['bench_max_dd']:.1%} | t {f['t_monthly_excess']:.2f} | A {crit['A_tier']} "
              f"B {crit['B']} pass {crit['pass']} | vector {gross:.2f}x engine {nav.iloc[-1] / mc.ACCOUNT:.2f}x")
    navs["QQQ_from_1999_03_10"] = bq
    navs["ONEQ_from_2003_10"] = bo
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", index=False)
    hd = pd.DataFrame(holds)
    hd.to_csv(OUT / "holdings_by_signal.csv", index=False)
    hd[hd["signal"].isin(o2.SNAPSHOT_SIGNALS)].to_csv(OUT / "holdings_snapshots.csv", index=False)
    pd.DataFrame(navs).to_csv(LOCAL / "built" / "nav_daily.csv")
    out = {"periods": o2.PERIODS, "t_threshold": T_THRESHOLD, "t_batch_only": T_BATCH, "results": results,
           "m4_extension": ext_log.to_dict("records"),
           "m5_filter_summary": filt["action"].value_counts().to_dict() if filt is not None else {}}
    (OUT / "results.json").write_text(json.dumps(out, indent=1, default=lambda x: round(x, 6) if isinstance(x, float)
                                                 else str(x)) + "\n")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check-data", action="store_true", help="only the ranking check and the M4 look-back extension")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
