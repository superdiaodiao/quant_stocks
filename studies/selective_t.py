"""Selective / swing "T" trades (有条件、可跨天的做T) around a held base, pre-registered in
docs/research_ledger_selective_t.md (section 0). Not QuantConnect.

Base: 75% of a $10,000 cash account in integer shares, 25% settled cash as the reserve (~1/3 of the base).
A T cycle sells (S1, S2) or adds (S3) q = floor(B / 3) shares and is closed by a limit order or at the max hold:

- S1 (overextended, sell high / buy back lower): close_t >= (1 + X) * SMA20_t, or RSI14_t > 75 -> sell q at the
  open of t+1; buy back with a limit at the SMA20 up to the previous close; else at the close of holding day 20.
- S2 (gap up): open_t >= (1 + G) * close_{t-1} -> sell q just after the open at open * (1 - slip); buy back with a
  limit at close_{t-1} (gap fill) on the gap day and the 5 sessions after; else at the close of the 5th.
- S3 (big dip, add / sell on recovery): close_t / close_{t-1} - 1 <= -D, or close_t <= (1 - Y) * SMA20_t -> buy q
  at the open of t+1 from settled cash; sell with a limit at min(SMA20, entry * (1 + Z)) (entry * (1 + Z) when the
  SMA20 is not above the entry); else at the close of holding day 20.
- S4 = an S1 cycle and an S3 cycle running independently.

Cash account: every buy needs settled cash; sale proceeds settle the next session (T+1). Margin variant (report
only): 100% base, no reserve, negative cash charged T-bill + 1.5%, day-trade (PDT) counts. Limits need a trade
through of max($0.01, 0.05%). Costs: quant.backtest.costs.ibkr_order_cost (IBKR Tiered) plus a half-spread
(QQQ 1 bp, stocks / ONEQ 2 bp, at least half a cent) on every order.

Data: QQQ / ONEQ Yahoo charts of the calendar study; 18 large caps fetched here from Yahoo v8 (daily OHLC, splits,
dividends; one request per 2 s; raw bodies local only under research_cache/selective_t/raw). Every vendor frame is
truncated at ``END`` and asserted. Outputs (returns, counts, statistics; no price levels):
output/research_only/selective_t/.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study selective_t [--fetch]   (or scripts/research_selective_t.py)
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from quant.backtest import execution
from quant.data.rates import daily_rf
from quant.evaluation.criteria import NORM, ab_criteria, bh_reject, bonferroni_t  # noqa: F401  (st.bh_reject)
from quant.evaluation.metrics import (
    TRADING_DAYS, cagr_of, core_metrics, max_drawdown, monthly, relative_metrics, t_and_ir, yearly,
)
from quant.paths import ROOT
from quant.strategies.selective_t import (  # noqa: F401  (st.* names read by tests and the observation)
    BASE_FRAC, BUFFER, CAP_MULT, CONFIGS, Cycle, END, Fam, HALF_SPREAD_BPS, Market, ONEQ_FIRST_RETURN, S2_SLIP,
    SECONDS_PER_REQUEST, START_EQUITY, STOCKS, Spec, basket_mean, cost, exit_limit, fams_of, fetch, hs_bps, hs_of,
    load_qqq_like, load_stock, match_returns, ohlc_frame, panel_check, raw_path, ref_specs, rsi_wilder, run_one,
    signal, simulate, variant_spec,
)

OUT = ROOT / "output/research_only/selective_t"


STOCK_PERIODS = {"half1": ("2012-01-03", "2018-12-31"), "half2": ("2019-01-02", END), "full": ("2012-01-03", END)}

ASSETS = ("QQQ", "U18")


# ======================================================================== periods, metrics

def first_valid(m: Market) -> int:
    return int(np.argmax(np.isfinite(m.sma_prev) & np.isfinite(m.rsi)))


def qqq_bounds(m: Market) -> dict:
    s = max(first_valid(m), 1)
    e = int(m.s.sessions.searchsorted(pd.Timestamp(END), side="right")) - 1
    mid = s + (e - s + 1) // 2
    return {"full": (s, e), "half1": (s, mid - 1), "half2": (mid, e)}


def stock_bounds(m: Market) -> dict:
    out = {}
    fv = max(first_valid(m), 1)
    for k, (a, b_) in STOCK_PERIODS.items():
        s = max(int(m.s.sessions.searchsorted(pd.Timestamp(a))), fv)
        e = int(m.s.sessions.searchsorted(pd.Timestamp(b_), side="right")) - 1
        if e - s > 60:
            out[k] = (s, e)
    return out


def trip_stats(tr: pd.DataFrame, stock_years: float) -> dict:
    if tr.empty:
        return {"trips": 0, "trips_per_year": 0.0, "win_rate_net": float("nan"), "gross_per_trip_bp": float("nan"),
                "net_per_trip_bp": float("nan"), "gross_per_trip_usd": float("nan"), "net_per_trip_usd": float("nan"),
                "cost_per_trip_bp": float("nan"), "share_target": float("nan"), "share_timeout": float("nan"),
                "share_end": float("nan"), "mean_days": float("nan"), "same_day_share": float("nan")}
    net = tr["gross"] - tr["cost"]
    rn = tr["reason"].value_counts(normalize=True)
    return {"trips": int(len(tr)), "trips_per_year": len(tr) / stock_years,
            "win_rate_net": float((net > 0).mean()),
            "gross_per_trip_bp": float((tr["gross"] / tr["notional"]).mean() * 1e4),
            "net_per_trip_bp": float((net / tr["notional"]).mean() * 1e4),
            "gross_per_trip_usd": float(tr["gross"].mean()), "net_per_trip_usd": float(net.mean()),
            "cost_per_trip_bp": float((tr["cost"] / tr["notional"]).mean() * 1e4),
            "share_target": float(rn.get("target", 0.0)), "share_timeout": float(rn.get("timeout", 0.0)),
            "share_end": float(rn.get("end", 0.0)), "mean_days": float(tr["days"].mean()),
            "same_day_share": float(tr["same_day"].astype(bool).mean())}


def evaluate(r: pd.Series, refs: dict, oneq: pd.Series, rf: pd.Series) -> dict:
    m = {"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "sessions": int(len(r)),
         **core_metrics(r, rf)}
    for tag, ref in refs.items():
        ref = ref.reindex(r.index)
        m[f"{tag}_cagr"] = cagr_of(ref)
        m[f"excess_vs_{tag}"] = m["cagr"] - cagr_of(ref)
        m[f"{tag}_max_dd"] = max_drawdown(pd.concat([pd.Series([1.0]), (1 + ref).cumprod()]))
        t, ir = t_and_ir(monthly(r) - monthly(ref))
        m[f"t_monthly_excess_vs_{tag}"], m[f"ir_vs_{tag}"] = t, ir
    m.update(relative_metrics(r, oneq, rf, "oneq"))
    return m


def judge(per: dict) -> dict:
    h1, h2, f = per["half1"], per["half2"], per["full"]
    adds = (h1["excess_vs_hbase"] > 0 and h2["excess_vs_hbase"] > 0 and f["t_monthly_excess_vs_hbase"] >= 2.0
            and h1["excess_vs_hmatch"] > 0 and h2["excess_vs_hmatch"] > 0)
    a, b = ab_criteria((h1, h2), f, cagr="excess_vs_oneq", bench=None)
    return {"adds_value_vs_hbase": bool(adds), "A": bool(a), "B": bool(b), "pass_vs_oneq": bool(a or b),
            "worth_forward_watch": bool(adds and (a or b))}


# ======================================================================== runs per asset

VARIANTS = ("net", "gross", "touch_fill", "margin")


def run(args=None) -> dict:
    if args is not None and getattr(args, "fetch", False):
        print("FETCH:", fetch())
    qqq, oneq = load_qqq_like("QQQ"), load_qqq_like("ONEQ")
    stocks = {}
    for sym in STOCKS:
        if raw_path(sym).exists():
            stocks[sym] = load_stock(sym)
    missing = [x for x in STOCKS if x not in stocks]
    if missing:
        raise SystemExit(f"missing raw data for {missing}; run with --fetch")
    rf = daily_rf(qqq.s.sessions, END)
    checks = {"QQQ": qqq.s.checks, "ONEQ": oneq.s.checks}
    pc_rows = []
    for sym, m in stocks.items():
        pc = panel_check(m)
        checks[sym] = {**m.s.checks, **pc}
        pc_rows.append({"symbol": sym, **{k: v for k, v in m.s.checks.items() if k != "splits"},
                        "splits": json.dumps(m.s.checks["splits"]), **{k: (json.dumps(v) if isinstance(v, list) else v)
                                                                        for k, v in pc.items()}})
        extra = pd.DatetimeIndex(m.s.sessions).difference(qqq.s.sessions)
        if len(extra):
            raise ValueError(f"{sym} sessions not in the QQQ calendar: {list(extra[:5])}")
    OUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(pc_rows).to_csv(OUT / "data_checks.csv", index=False, float_format="%.6g")
    print("DATA CHECKS:", json.dumps({k: {kk: vv for kk, vv in v.items() if kk in (
        "first", "last", "ohlc_missing_or_nonpositive", "open_or_close_outside_high_low", "ohlc_filled_from_close",
        "median_abs_rel_diff", "share_within_0p5pct", "max_abs_rel_diff", "split_dates_agree")} for k, v in checks.items()},
        default=str))

    n_trials = len(CONFIGS) * len(ASSETS)
    res = {"end": END, "n_trials": n_trials, "bonferroni_t_one_sided_5pct": bonferroni_t(n_trials),
           "data_checks": checks, "assets": {}}
    rows, by_year, trips_year, per_stock, mon = [], {}, [], [], {}
    ref_cache: dict = {}

    def oneq_for(index: pd.DatetimeIndex) -> pd.Series:
        so = max(index[0], pd.Timestamp(ONEQ_FIRST_RETURN))
        return execution.buy_hold_returns(oneq.s, qqq.s.sessions, so, index[-1], hs_bps("ONEQ"), True)

    # ---------------- QQQ
    qb = qqq_bounds(qqq)
    res["assets"]["QQQ"] = {"periods": {k: [str(qqq.s.sessions[s].date()), str(qqq.s.sessions[e].date())]
                                        for k, (s, e) in qb.items()}, "configs": {}, "references": {}}
    for pk, (s, e) in qb.items():
        hb = simulate(qqq, s, e, ref_specs("net")["hbase"], rf)["ret"]
        h1 = simulate(qqq, s, e, ref_specs("net")["h100"], rf)["ret"]
        oq = oneq_for(hb.index)
        for nm, rr in (("H_base", hb), ("H100", h1)):
            mm = evaluate(rr, {"hbase": hb, "h100": h1}, oq, rf)
            res["assets"]["QQQ"]["references"].setdefault(pk, {})[nm] = mm
            rows.append({"asset": "QQQ", "config": nm, "variant": "net", "period": pk, **mm})
        if pk == "full":
            by_year["QQQ_H_base"], by_year["QQQ_H100"], by_year["QQQ_ONEQ"] = yearly(hb), yearly(h1), yearly(oq)
    for cfg in CONFIGS:
        info = {"periods": {}}
        for pk, (s, e) in qb.items():
            years = (e - s + 1) / TRADING_DAYS
            pr = {}
            for vn in VARIANTS:
                o = run_one(qqq, s, e, cfg, "QQQ", vn, rf, ref_cache)
                refs = {"hbase": o["hbase"], "hmatch": o["hmatch"], "h100": o["h100"]}
                mm = evaluate(o["ret"], refs, oneq_for(o["ret"].index), rf)
                mm.update(trip_stats(o["sim"]["trips"], years))
                mm.update({"exposure": o["sim"]["exposure"], "cost_drag_per_year": o["sim"]["cost_frac"] / years,
                           "shortfall_days": o["sim"]["shortfall_days"],
                           "day_trades_per_year": o["sim"]["day_trades"] / years,
                           "pdt_breach_share": o["sim"]["pdt_breach_share"]})
                pr[vn] = mm
                rows.append({"asset": "QQQ", "config": cfg, "variant": vn, "period": pk, **mm})
                if pk == "full" and vn == "net":
                    by_year[f"QQQ_{cfg}"] = yearly(o["ret"])
                    mon[f"QQQ_{cfg}_minus_hbase"] = monthly(o["ret"]) - monthly(o["hbase"])
                    trips_year.append(trips_by_year(o["sim"]["trips"], "QQQ", cfg))
            info["periods"][pk] = pr
        info["judgement"] = judge({pk: info["periods"][pk]["net"] for pk in qb})
        res["assets"]["QQQ"]["configs"][cfg] = info
        report_line("QQQ", cfg, info)

    # ---------------- U18 basket (each stock its own $10k account; basket = equal-weight mean of daily returns)
    sb = {sym: stock_bounds(m) for sym, m in stocks.items()}
    res["assets"]["U18"] = {"periods": {k: list(v) for k, v in STOCK_PERIODS.items()}, "configs": {}, "references": {}}
    for pk in STOCK_PERIODS:
        hb = basket_mean([simulate(m, *sb[x][pk], ref_specs("net")["hbase"], rf)["ret"] for x, m in stocks.items()
                          if pk in sb[x]])
        h1 = basket_mean([simulate(m, *sb[x][pk], ref_specs("net")["h100"], rf)["ret"] for x, m in stocks.items()
                          if pk in sb[x]])
        oq = oneq_for(hb.index)
        for nm, rr in (("H_base", hb), ("H100", h1)):
            mm = evaluate(rr, {"hbase": hb, "h100": h1}, oq, rf)
            res["assets"]["U18"]["references"].setdefault(pk, {})[nm] = mm
            rows.append({"asset": "U18", "config": nm, "variant": "net", "period": pk, **mm})
        if pk == "full":
            by_year["U18_H_base"], by_year["U18_H100"], by_year["U18_ONEQ"] = yearly(hb), yearly(h1), yearly(oq)
    for cfg in CONFIGS:
        info = {"periods": {}}
        for pk in STOCK_PERIODS:
            pr = {}
            for vn in VARIANTS:
                outs = {x: run_one(m, *sb[x][pk], cfg, "stock", vn, rf, ref_cache) for x, m in stocks.items()
                        if pk in sb[x]}
                r = basket_mean([o["ret"] for o in outs.values()])
                refs = {k: basket_mean([o[k] for o in outs.values()]) for k in ("hbase", "hmatch", "h100")}
                mm = evaluate(r, refs, oneq_for(r.index), rf)
                stock_years = sum(len(o["ret"]) for o in outs.values()) / TRADING_DAYS
                trips = pd.concat([o["sim"]["trips"].assign(symbol=x) for x, o in outs.items()], ignore_index=True)
                mm.update(trip_stats(trips, stock_years))
                mm.update({"trips_per_year_basket_total": len(trips) / (len(r) / TRADING_DAYS),
                           "exposure": float(np.mean([o["sim"]["exposure"] for o in outs.values()])),
                           "cost_drag_per_year": float(np.mean([o["sim"]["cost_frac"] / (len(o["ret"]) / TRADING_DAYS)
                                                                for o in outs.values()])),
                           "shortfall_days": int(sum(o["sim"]["shortfall_days"] for o in outs.values())),
                           "day_trades_per_year": float(np.mean([o["sim"]["day_trades"] / (len(o["ret"]) / TRADING_DAYS)
                                                                 for o in outs.values()])),
                           "pdt_breach_share": float(np.mean([o["sim"]["pdt_breach_share"] for o in outs.values()])),
                           "n_stocks": len(outs)})
                pr[vn] = mm
                rows.append({"asset": "U18", "config": cfg, "variant": vn, "period": pk, **mm})
                if vn == "net":
                    for x, o in outs.items():
                        yrs = len(o["ret"]) / TRADING_DAYS
                        ts_ = trip_stats(o["sim"]["trips"], yrs)
                        per_stock.append({"config": cfg, "period": pk, "symbol": x,
                                          "cagr": cagr_of(o["ret"]), "hbase_cagr": cagr_of(o["hbase"]),
                                          "excess_vs_hbase": cagr_of(o["ret"]) - cagr_of(o["hbase"]),
                                          "excess_vs_hmatch": cagr_of(o["ret"]) - cagr_of(o["hmatch"]),
                                          "t_monthly_excess_vs_hbase": t_and_ir(monthly(o["ret"]) - monthly(o["hbase"]))[0],
                                          "trips_per_year": ts_["trips_per_year"], "win_rate_net": ts_["win_rate_net"],
                                          "net_per_trip_bp": ts_["net_per_trip_bp"], "share_target": ts_["share_target"]})
                    if pk == "full":
                        by_year[f"U18_{cfg}"] = yearly(r)
                        mon[f"U18_{cfg}_minus_hbase"] = monthly(r) - monthly(refs["hbase"])
                        trips_year.append(trips_by_year(trips, "U18", cfg))
            info["periods"][pk] = pr
        info["judgement"] = judge({pk: info["periods"][pk]["net"] for pk in STOCK_PERIODS})
        res["assets"]["U18"]["configs"][cfg] = info
        report_line("U18", cfg, info)

    # ---------------- multiple testing over the 18 trials
    names, tvals = [], []
    for a in ASSETS:
        for cfg in CONFIGS:
            names.append(f"{a} {cfg}")
            tvals.append(res["assets"][a]["configs"][cfg]["periods"]["full"]["net"]["t_monthly_excess_vs_hbase"])
    p = np.array([1 - NORM.cdf(t) for t in tvals])
    rej = bh_reject(p)
    res["multiple_testing"] = {n: {"t_vs_hbase": t, "p_one_sided": float(pp), "bh_reject_q05": bool(rj),
                                   "above_bonferroni": bool(t >= res["bonferroni_t_one_sided_5pct"])}
                               for n, t, pp, rj in zip(names, tvals, p, rej)}
    (OUT / "results.json").write_text(json.dumps(res, indent=2, default=str))
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", float_format="%.6f")
    pd.concat([x for x in trips_year if x is not None]).to_csv(OUT / "trips_by_year.csv", index=False,
                                                                float_format="%.4f")
    pd.DataFrame(per_stock).to_csv(OUT / "per_stock.csv", index=False, float_format="%.6f")
    mk = pd.DataFrame(mon)
    mk.index = mk.index.astype(str)
    mk.to_csv(OUT / "monthly_excess_vs_hbase.csv", float_format="%.6f")
    return res


def trips_by_year(tr: pd.DataFrame, asset: str, cfg: str) -> pd.DataFrame | None:
    if tr.empty:
        return None
    tr = tr.copy()
    tr["net"] = tr["gross"] - tr["cost"]
    tr["net_bp"] = tr["net"] / tr["notional"] * 1e4
    tr["gross_bp"] = tr["gross"] / tr["notional"] * 1e4
    g = tr.groupby([tr["exit"].dt.year, "family"])
    out = pd.DataFrame({"trips": g.size(), "win_rate_net": g["net"].apply(lambda x: (x > 0).mean()),
                        "gross_bp_mean": g["gross_bp"].mean(), "net_bp_mean": g["net_bp"].mean(),
                        "share_target": g["reason"].apply(lambda x: (x == "target").mean())}).reset_index()
    out = out.rename(columns={"exit": "year"})
    out.insert(0, "config", cfg)
    out.insert(0, "asset", asset)
    return out


def report_line(asset: str, cfg: str, info: dict) -> None:
    f, h1, h2 = (info["periods"][k]["net"] for k in ("full", "half1", "half2"))
    print(f"{asset} {cfg}: trips/yr {f['trips_per_year']:.1f}, win {f['win_rate_net']:.0%}, gross/net per trip "
          f"{f['gross_per_trip_bp']:+.0f}/{f['net_per_trip_bp']:+.0f} bp, target {f['share_target']:.0%}; vs H_base "
          f"h1 {h1['excess_vs_hbase']:+.2%} h2 {h2['excess_vs_hbase']:+.2%} t {f['t_monthly_excess_vs_hbase']:+.2f}; "
          f"vs H_match h1 {h1['excess_vs_hmatch']:+.2%} h2 {h2['excess_vs_hmatch']:+.2%}; {info['judgement']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fetch", action="store_true", help="fetch missing Yahoo charts first (one per 2 s)")
    ap.add_argument("--fetch-only", action="store_true")
    a = ap.parse_args(argv)
    if a.fetch_only:
        print(fetch())
        return
    run(a)


if __name__ == "__main__":
    main()
