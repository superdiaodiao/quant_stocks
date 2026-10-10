"""Mega-cap momentum variants M3, M4, M5 out of sample 1999-2013 on the OOS.2 point-in-time Nasdaq ranking.

Pre-registered in docs/research_ledger_megacap.md, section OOS.3. Rules unchanged from section 0.4:
  M3 among the top 20 by market cap, the 5 with the best 6-month (126-session) total return, equal weight, monthly
  M4 among the top 20, the 10 with the best 12-1 momentum (t-252 .. t-21), equal weight
  M5 M3, but 100% QQQ when QQQ closes below its 200-session SMA on the signal day
Data, execution, costs, benchmark and criteria as OOS.2 (scripts/research_megacap_oos2.py), with two registered
additions: (1) M4's 252-session look-back is served by total-return history before 1998-06-01 from each name's own
OOS.2 source (used for momentum only); (2) M5's SMA warm-up uses the Nasdaq-100 index (^NDX) scaled to QQQ before
QQQ's first close on 1999-03-10; a QQQ signal before QQQ trades is held as cash. Judged vs QQQ 1999-03-10..2013-12-31:
A = CAGR above QQQ and monthly excess t >= 2.39 (Bonferroni for 6 tests on this sample), or B = max drawdown >= 10 pp
shallower and CAGR within 3 pp. ONEQ (2003-10..2013) is reported only.

Vendor price levels stay under research_cache/megacap_oos3/ (local only); git gets returns, ranks, IDs and flags.

Usage:  PYTHONPATH=. .venv/bin/python scripts/research_megacap_oos3.py [--check-data]
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from quant.data import megacap_history as o2     # the OOS.2 data build, ranking and period statistics
from quant.data import version as dv
from quant.data.sources import http
from quant.data.sources import megacap_oos2 as md      # the OOS.2 sources (cached_get on the pipeline ledger)
from quant.data.sources.yahoo import parse_chart
from quant.evaluation.metrics import yearly
from quant.paths import CACHE_ROOT, output_dir
from quant.signals.technical import momentum_frames
from quant.strategies import megacap as mc

OUT = output_dir("megacap_oos3")
LOCAL = CACHE_ROOT / "megacap_oos3"
NDX_CHART = CACHE_ROOT / "qqq_timing/raw/chart_%5ENDX.json"
RULES = tuple(r for r in mc.RULES if r.name in ("M3", "M4", "M5"))
T_THRESHOLD = 2.39            # one-sided 5% Bonferroni, 6 tests on 1999-2013 (OOS.2's 3 + these 3)
T_BATCH = 2.13                # this batch of 3 alone (reported tier, not the rule)
PRE_FROM, PRE_TO = "1997-09-01", "1998-07-31"
EXTEND_UNTIL_SIGNAL = "1999-05-28"     # the last signal whose 252-session look-back reaches before 1998-06-01
QQQ_FIRST = pd.Timestamp(o2.JUDGE_FROM)


# ======================================================================== M4 look-back: history before 1998-06-01

def _yahoo_pre(symbol: str) -> pd.Series | None:
    p1, p2 = int(pd.Timestamp(PRE_FROM).timestamp()), int(pd.Timestamp("1998-08-01").timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={p1}&period2={p2}"
           "&interval=1d&events=div%2Csplits&includeAdjustedClose=true")
    try:
        body = md.cached_get(url, LOCAL / "raw/yahoo" / f"{symbol}__{PRE_FROM}_{PRE_TO}.json.gz", source="yahoo",
                                 headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                                 limiter=md.YAHOO_LIMITER, symbol=symbol, timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"  yahoo pre {symbol}: {exc}")
        return None
    res = json.loads(body).get("chart", {}).get("result")
    if not res or not res[0].get("timestamp"):
        return None
    r = res[0]
    ts = pd.to_datetime(r["timestamp"], unit="s", utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
    df = pd.DataFrame({"date": ts, "c": r["indicators"]["quote"][0]["close"]}).dropna().drop_duplicates("date", keep="last")
    divs = {}
    for v in r.get("events", {}).get("dividends", {}).values():
        d = pd.Timestamp(v["date"], unit="s", tz="UTC").tz_convert("America/New_York").normalize().tz_localize(None)
        divs[d] = divs.get(d, 0.0) + float(v["amount"])
    df["dv"] = df["date"].map(divs).fillna(0.0)
    ret = (df["c"] + df["dv"]) / df["c"].shift(1) - 1          # split-adjusted close + dividend (as md.yahoo_daily)
    return pd.Series((1 + ret.fillna(0)).cumprod().to_numpy(), index=pd.DatetimeIndex(df["date"]))


def _tiingo_pre(symbol: str) -> pd.Series | None:
    if symbol.upper() not in md.tiingo_month_symbols():
        print(f"  tiingo pre {symbol}: not counted this month -> not asked")
        return None
    url = f"https://api.tiingo.com/tiingo/daily/{symbol.lower()}/prices?startDate={PRE_FROM}&endDate={PRE_TO}"
    key = http.read_env_key(dv.DATA_MAIN / ".env.tiingo", "TIINGO_API_KEY")
    try:
        body = md.cached_get(url, LOCAL / "raw/tiingo" / f"{symbol}__{PRE_FROM}_{PRE_TO}.json.gz", source="tiingo",
                                 symbol=symbol.upper(), limiter=md.TIINGO_LIMITER, timeout=60,
                                 headers={"Authorization": f"Token {key}", "Content-Type": "application/json"})
    except Exception as exc:  # noqa: BLE001
        print(f"  tiingo pre {symbol}: {type(exc).__name__}")
        return None
    t = md._tiingo_frame(json.loads(body))
    if t is None:
        return None
    return pd.Series(o2._tiingo_tr(t).to_numpy(), index=pd.DatetimeIndex(t["date"]))


def pre_history(row, panel_source: str) -> tuple[pd.Series | None, str]:
    """Total-return level before (and overlapping) 1998-06-01 from the source this name uses on 1998-06-01 in OOS.2."""
    src, _, sym = panel_source.partition(":")
    if src == "yahoo":
        return _yahoo_pre(sym), panel_source
    if src in ("tiingo", "tiingo_cache"):
        return _tiingo_pre(sym), f"tiingo:{sym}"
    if src == "wayback":
        a_need, _ = md.need_window(row)
        seg = o2.wayback_segment(sym, max(a_need, pd.Timestamp(o2.START)), pd.Timestamp(o2.END))   # OOS.2's capture
        if seg is None:
            return None, panel_source
        return pd.Series(seg["tr"].to_numpy(), index=pd.DatetimeIndex(seg["date"])), panel_source
    if src == "cmc":
        m = md.cmc_series(sym)["marketcap"]
        return (m / m.iloc[0]) if len(m) else None, panel_source
    return None, panel_source


def chain_pre(pre: pd.Series, own: pd.Series, cal_pre: pd.DatetimeIndex) -> tuple[pd.Series, dict]:
    """Prepend ``pre`` to ``own`` (the OOS.2 total-return level) at own's first date; own is not changed.
    Returns the levels on cal_pre (dates before own's first date) and an overlap check of daily returns."""
    anchor = own.first_valid_index()
    pre = pre[~pre.index.duplicated()].sort_index()
    if anchor not in pre.index or not np.isfinite(pre.loc[anchor]) or pre.loc[anchor] <= 0:
        return pd.Series(dtype=float), {"anchor": str(anchor.date()), "ok": False}
    scaled = pre * (own.loc[anchor] / pre.loc[anchor])
    ov = pd.DataFrame({"a": scaled, "b": own}).dropna()
    ov = ov[(ov.index >= anchor) & (ov.index <= pd.Timestamp(PRE_TO))].pct_change().dropna()
    diff = float((ov["a"] - ov["b"]).abs().max()) if len(ov) else np.nan
    out = scaled[scaled.index < anchor].reindex(cal_pre).ffill()
    first = scaled.index.min()
    out[out.index < first] = np.nan
    return out, {"anchor": str(anchor.date()), "ok": True, "overlap_days": int(len(ov)), "overlap_max_abs_ret_diff": diff,
                 "pre_first": str(first.date())}


def extended_index(idx: pd.DataFrame, extend: dict, cal_pre: pd.DatetimeIndex) -> pd.DataFrame:
    """idx on cal_pre + idx.index; columns in ``extend`` get their prepended history, others NaN before."""
    top = pd.DataFrame(np.nan, index=cal_pre, columns=idx.columns)
    for k, s in extend.items():
        top[k] = s.reindex(cal_pre).to_numpy()
    return pd.concat([top, idx]).sort_index()


# ======================================================================== M5 filter with the ^NDX warm-up

def trend_series(qqq_close: pd.Series, ndx_close: pd.Series, first: pd.Timestamp = QQQ_FIRST) -> pd.DataFrame:
    """X = QQQ close from ``first``; before it ^NDX close x (QQQ / NDX on ``first``). sma = mean of the last 200 X."""
    q = qqq_close.dropna()
    q = q[q.index >= first]
    n = ndx_close.dropna()
    k = float(q.loc[first] / n.loc[first])
    x = pd.concat([n[n.index < first] * k, q]).sort_index()
    src = pd.Series(np.where(x.index < first, "ndx", "qqq"), index=x.index)
    return pd.DataFrame({"x": x, "sma": x.rolling(200, min_periods=200).mean(), "src": src})


def apply_trend(m3_targets: dict, tr: pd.DataFrame, first_qqq_exec: pd.Timestamp, sessions: pd.DatetimeIndex):
    """M5 targets from M3's: below the SMA -> QQQ, or cash ([]) when the trade date is before QQQ's first close."""
    out, log = {}, []
    for s, lst in m3_targets.items():
        row = tr.loc[:s].iloc[-1]
        later = sessions[sessions > s]
        exec_d = later[0] if len(later) else pd.NaT
        if not np.isfinite(row["sma"]):
            continue
        above = bool(row["x"] >= row["sma"])
        if above:
            out[s], act = lst, "M3 stocks"
        elif pd.notna(exec_d) and exec_d < first_qqq_exec:
            out[s], act = [], "cash (QQQ not yet trading)"
        else:
            out[s], act = [("QQQ", 1.0, np.nan, np.nan, "qqq")], "QQQ"
        log.append({"signal": s.date().isoformat(), "sma_from": row["src"] if tr.loc[:s].tail(200)["src"].eq(row["src"]).all()
                    else "ndx+qqq", "qqq_at_or_above_sma200": above, "action": act})
    return out, pd.DataFrame(log)


# ======================================================================== criteria and checks

def criteria(full: dict) -> dict:
    up = full["cagr"] > full["bench_cagr"]
    t = full["t_monthly_excess"]
    a = up and t >= T_THRESHOLD
    b = (abs(full["bench_max_dd"]) - abs(full["max_dd"])) * 100 >= 10 and full["cagr"] >= full["bench_cagr"] - 0.03
    tier = ("A" if a else "batch-only (2.13<=t<2.39)" if up and t >= T_BATCH else
            "nominal (2<=t<2.13)" if up and t >= 2.0 else "no")
    return {"A": bool(a), "A_tier": tier, "B": bool(b), "pass": bool(a or b)}


def vector_gross(targets: dict, sessions: pd.DatetimeIndex, idx: pd.DataFrame, qqq: pd.Series) -> float:
    """Gross growth when each target is bought at the next close and held (weights drift) to the next trade."""
    execs = []
    for s in sorted(targets):
        later = sessions[sessions > s]
        if len(later):
            execs.append((later[0], targets[s]))
    g = 1.0
    for (d0, tgt), (d1, _) in zip(execs, execs[1:] + [(sessions[-1], None)]):
        if not tgt:
            continue
        r = 0.0
        for sid, w, *_ in tgt:
            lv = qqq if sid == "QQQ" else idx[sid]
            r += w * (lv.loc[d1] / lv.loc[d0])
        g *= r + (1 - sum(w for _, w, *_ in tgt))
    return g


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
    msft_pre = _yahoo_pre("MSFT")
    cal_pre = pd.DatetimeIndex(msft_pre.index[msft_pre.index < sessions[0]])
    early = sorted(set(ranks.loc[ranks["s"] <= EXTEND_UNTIL_SIGNAL, "key"]))
    crow = {r.key: r for r in cand.itertuples()}
    extend, ext_log = {}, []
    for k in early:
        own = idx[k]
        first_src = str(panel[k]["source"].iloc[0])
        if own.first_valid_index() > sessions[0]:
            ext_log.append({"key": k, "source": first_src, "status": "series starts after 1998-06-01 (IPO): not extended",
                            "own_first": str(own.first_valid_index().date())})
            continue
        pre, label = pre_history(crow[k], first_src)
        if pre is None or pre.empty:
            ext_log.append({"key": k, "source": label, "status": "no pre-1998-06 history"})
            continue
        s_, chk = chain_pre(pre, own, cal_pre)
        if not chk["ok"] or s_.notna().sum() == 0:
            ext_log.append({"key": k, "source": label, "status": "could not chain", **chk})
            continue
        extend[k] = s_
        ext_log.append({"key": k, "source": label, "status": "extended", **chk,
                        "pre_sessions": int(s_.notna().sum())})
    ext_log = pd.DataFrame(ext_log)
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
    trs = trend_series(qqq["close"], ndx)
    first_qqq_exec = qqq.index.min()

    results, by_year, holds, navs, filt = {}, [], [], {}, None
    targets = {}
    for rule in RULES:
        if rule.name == "M5":
            base = mc.build_targets(next(r for r in RULES if r.name == "M3"), ranked, mom, qqq_close)
            tg, filt = apply_trend(base, trs, first_qqq_exec, sessions)
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
        crash = {}
        for nm, s_ in (("strategy", nav), ("qqq", bq)):
            v = s_[(s_.index >= pd.Timestamp("2000-03-10")) & (s_.index <= pd.Timestamp("2002-10-09"))]
            crash[nm] = float(v.iloc[-1] / v.iloc[0] - 1)
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
        gross = vector_gross(tg, sessions, idx, qqq_lvl.ffill())
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
