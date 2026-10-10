"""Repeated intraday "T" trades (做T) on QQQ / QLD (pre-registered in docs/research_ledger_intraday_t.md, section 0).

Base: 75% of a $10,000 cash account in integer shares, 25% settled cash as the T reserve (= 1/3 of the base value);
month-end rebalance back to 75% when the base weight leaves 70-80%. Each session, with P = the previous real close:

- T1 (sell high, buy back at the close): limit sell q shares at L = ceil_cent(P * (1 + k)). Fills at the open when
  open >= L, else at L when high >= L + max($0.01, 0.05% * L). Filled shares are bought back at the close (MOC).
- T2 (buy the dip, sell at the close): limit buy q shares with the reserve at L = floor_cent(P * (1 - k)). Fills at
  the open when open <= L, else at L when low <= L - max($0.01, 0.05% * L). Sold at the close.
- T3: sell q shares at the open (MOO), buy them back at the close (MOC), every session.

k = 1% or 0.5 * ATR20% (mean true range of the 20 sessions up to t-1 over P). q = min(floor(B / 3), floor((cash - $5)
/ cap)) with cap = 1.10 * P for T1 / T3 and L for T2. These rules need only the day's open / high / low / close, never
the order in which the high and low happened.

Reference H75: the same base / reserve / rebalance with no T trades (the key comparison). H100: all-in integer shares,
buy and hold. Benchmark ONEQ: buy and hold. Costs: scripts/research_reversal_dev.order_cost (IBKR Tiered) with a
half-spread (QQQ 1 bp, QLD / ONEQ 2 bp, at least half a cent) on every order, limit fills and auctions included.

Yahoo OHLC are split-adjusted; real traded prices are rebuilt by multiplying back later splits, so share counts and
per-share commissions are real. Every vendor frame is truncated at ``END`` and asserted.
Raw data local only (research_cache/calendar/raw, research_cache/qqq_timing/raw). Outputs (returns, counts and
statistics only, no price levels): output/research_only/intraday_t/. The real-price OHLC, the limit fills, the
cost model and the metrics are in quant (quant.data.ohlc, quant.backtest, quant.evaluation).

Usage:  PYTHONPATH=. .venv/bin/python -m quant study intraday_t   (or scripts/research_intraday_t.py)
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant.backtest.costs import ibkr_order_cost as order_cost, tick_half_spread
from quant.backtest.execution import (  # noqa: F401  (it.* names read by tests)
    THROUGH_FRAC, TICK, buy_hold_returns, ceil_cent, fill_buy_limit as fill_t2, fill_sell_limit as fill_t1, floor_cent,
)
from quant.data.guards import assert_dev_dates
from quant.data.ohlc import OHLCSeries as Series_, build_real_ohlc as build, real_factor  # noqa: F401
from quant.data.rates import daily_rf
from quant.data.sources.yahoo import parse_ohlc as _parse_ohlc, split_events
from quant.evaluation.criteria import bonferroni_t
from quant.evaluation.metrics import (
    TRADING_DAYS, cagr_of, core_metrics, max_drawdown, monthly, relative_metrics, t_and_ir, yearly,
)
from quant.paths import CACHE_ROOT, output_dir

END = "2026-09-30"
CAL_RAW = CACHE_ROOT / "calendar/raw"
QT_RAW = CACHE_ROOT / "qqq_timing/raw"
SOURCES = {"QQQ": CAL_RAW / "chart_QQQ.json", "QLD": QT_RAW / "chart_QLD.json", "ONEQ": CAL_RAW / "chart_ONEQ.json"}
OUT = output_dir("intraday_t")

START_EQUITY = 10_000.0
BASE_FRAC = 0.75
BAND = (0.70, 0.80)
T_FRACTION = 3                     # q = floor(B / 3)
CASH_BUFFER = 5.0
CAP_MULT = 1.10
K_FIXED = 0.01
ATR_MULT = 0.5
ATR_N = 20
HALF_SPREAD_BPS = {"QQQ": 1.0, "QLD": 2.0, "ONEQ": 2.0}
BASES = ("QQQ", "QLD")
RULES = ("T1", "T2", "T3")
CONFIGS = (("T1", "k1"), ("T1", "atr"), ("T2", "k1"), ("T2", "atr"), ("T3", "none"))
ONEQ_FIRST_RETURN = "2003-10-02"


def hs_of(sym: str, price: float) -> float:
    return tick_half_spread(HALF_SPREAD_BPS[sym], price)


def cost(shares: float, price: float, sell: bool, sym: str, on: bool = True) -> float:
    if not on or shares <= 0:
        return 0.0
    return order_cost(shares, price, sell, hs_of(sym, price))["total"]



# ======================================================================== data (quant.data.ohlc: real-price OHLC)

def parse_ohlc(path, end: str = END) -> pd.DataFrame:
    return _parse_ohlc(path, end)


def load(sym: str, end: str = END) -> Series_:
    df = parse_ohlc(SOURCES[sym], end)
    for k in ("open", "high", "low"):
        if df[k].isna().any():
            raise ValueError(f"{sym} {k} missing on {df.loc[df[k].isna(), 'date'].head().tolist()}")
    assert_dev_dates(df["date"], end)
    return build(sym, df, split_events(SOURCES[sym], end))


# ======================================================================== simulation

@dataclass
class Spec:
    rule: str | None          # "T1" / "T2" / "T3" / None (hold)
    k_mode: str = "none"      # "k1" / "atr" / "none"
    base_frac: float = BASE_FRAC
    reserve: bool = True      # cash-account model: q limited by settled cash
    rebalance: bool = True
    costs: bool = True
    through: bool = True


def simulate(d: Series_, s: int, e: int, spec: Spec) -> dict:
    """$10,000 at the close of s-1; sessions s..e. Returns daily values, trip records and counters."""
    sym, on = d.sym, spec.costs
    p0 = d.close[s - 1]
    b = int(math.floor(spec.base_frac * (START_EQUITY - 5.0) / (p0 * (1 + hs_of(sym, p0)))))
    cash = START_EQUITY - b * p0 - cost(b, p0, False, sym, on)
    cost_total = cost(b, p0, False, sym, on)
    cost_frac = cost_total / START_EQUITY        # sum of each cost over the account value before it
    values, trips, orders, gfv_days, day_trade = [START_EQUITY], [], 1, 0, []
    month = d.sessions.to_period("M")
    for t in range(s, e + 1):
        if d.split[t] != 1.0:
            b = int(round(b * d.split[t]))
        cash += b * d.div[t]
        p, o, h, lo, c = d.prev_close[t], d.open[t], d.high[t], d.low[t], d.close[t]
        traded = False
        if spec.rule is not None:
            k = K_FIXED if spec.k_mode == "k1" else ATR_MULT * d.atr_pct[t] if spec.k_mode == "atr" else 0.0
            if spec.rule in ("T1", "T3"):
                q = b // T_FRACTION
                if spec.reserve:
                    q = min(q, int(max(0.0, math.floor((cash - CASH_BUFFER) / (CAP_MULT * p)))))
                px = None
                if q > 0 and np.isfinite(k):
                    px = o if spec.rule == "T3" else fill_t1(o, h, ceil_cent(p * (1 + k)), spec.through)
                if px is not None:
                    settled = cash
                    c_s, c_b = cost(q, px, True, sym, on), cost(q, c, False, sym, on)
                    cash += q * px - c_s - q * c - c_b
                    if spec.reserve and settled < q * c + c_b:
                        gfv_days += 1
                    trips.append((d.sessions[t], q, q * (px - c), c_s + c_b, q * px))
                    cost_total += c_s + c_b
                    cost_frac += (c_s + c_b) / values[-1]
                    orders += 2
                    traded = True
            elif spec.rule == "T2":
                lim = floor_cent(p * (1 - k)) if np.isfinite(k) else float("nan")
                q = b // T_FRACTION
                if spec.reserve and np.isfinite(lim):
                    q = min(q, int(max(0.0, math.floor((cash - CASH_BUFFER) / lim))))
                px = fill_t2(o, lo, lim, spec.through) if q > 0 and np.isfinite(lim) else None
                if px is not None:
                    c_b, c_s = cost(q, px, False, sym, on), cost(q, c, True, sym, on)
                    cash += q * c - c_s - q * px - c_b
                    trips.append((d.sessions[t], q, q * (c - px), c_s + c_b, q * px))
                    cost_total += c_s + c_b
                    cost_frac += (c_s + c_b) / values[-1]
                    orders += 2
                    traded = True
        day_trade.append(traded)
        v = b * c + cash
        if spec.rebalance and t < e and month[t + 1] != month[t]:
            w = b * c / v
            if not (BAND[0] <= w <= BAND[1]):
                nb = int(math.floor(spec.base_frac * (v - 5.0) / (c * (1 + hs_of(sym, c)))))
                dq = nb - b
                cc = cost(abs(dq), c, dq < 0, sym, on)
                cash -= dq * c + cc
                cost_total += cc
                cost_frac += cc / v
                orders += 1
                b = nb
                v = b * c + cash
        values.append(v)
    val = pd.Series(values, index=d.sessions[s - 1: e + 1])
    tdf = pd.DataFrame(trips, columns=["date", "q", "gross", "cost", "notional"])
    dt_ = pd.Series(day_trade, index=d.sessions[s: e + 1])
    pdt = (dt_.astype(int).rolling(5, min_periods=1).sum() > 3)
    return {"value": val, "ret": val.pct_change().iloc[1:], "trips": tdf, "orders": orders, "cost_total": cost_total, "cost_frac": cost_frac,
            "gfv_days": gfv_days, "pdt_breach_share": float(pdt.mean()), "final_base": b}


# ======================================================================== periods, metrics

def period_bounds(d: Series_, end: str = END) -> dict:
    s = int(np.argmax(np.isfinite(d.atr_pct)))
    e = int(d.sessions.searchsorted(pd.Timestamp(end), side="right")) - 1
    mid = s + (e - s + 1) // 2
    return {"full": (s, e), "half1": (s, mid - 1), "half2": (mid, e)}


def trip_stats(tr: pd.DataFrame, years: float) -> dict:
    if tr.empty:
        return {"trips": 0, "trips_per_year": 0.0, "gross_per_trip_usd": 0.0, "net_per_trip_usd": 0.0,
                "cost_per_trip_usd": 0.0, "gross_per_trip_bp": 0.0, "net_per_trip_bp": 0.0, "hit_rate_net": 0.0,
                "total_gross_usd": 0.0, "total_net_usd": 0.0}
    net = tr["gross"] - tr["cost"]
    return {"trips": int(len(tr)), "trips_per_year": len(tr) / years,
            "gross_per_trip_usd": float(tr["gross"].mean()), "net_per_trip_usd": float(net.mean()),
            "cost_per_trip_usd": float(tr["cost"].mean()),
            "gross_per_trip_bp": float((tr["gross"] / tr["notional"]).mean() * 1e4),
            "net_per_trip_bp": float((net / tr["notional"]).mean() * 1e4),
            "hit_rate_net": float((net > 0).mean()),
            "total_gross_usd": float(tr["gross"].sum()), "total_net_usd": float(net.sum())}


def evaluate(r: pd.Series, h75: pd.Series, h100: pd.Series, oneq: pd.Series, rf: pd.Series) -> dict:
    m = {"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "sessions": int(len(r)),
         **core_metrics(r, rf)}
    for tag, ref in (("h75", h75), ("h100", h100)):
        m[f"{tag}_cagr"] = cagr_of(ref)
        m[f"excess_vs_{tag}"] = m["cagr"] - cagr_of(ref)
        m[f"{tag}_max_dd"] = max_drawdown(pd.concat([pd.Series([1.0]), (1 + ref).cumprod()]))
        t, ir = t_and_ir(monthly(r) - monthly(ref))
        m[f"t_monthly_excess_vs_{tag}"], m[f"ir_vs_{tag}"] = t, ir
    m.update(relative_metrics(r, oneq, rf, "oneq"))
    return m


def judge(per: dict) -> dict:
    h1, h2, f = per["half1"], per["half2"], per["full"]
    adds = h1["excess_vs_h75"] > 0 and h2["excess_vs_h75"] > 0 and f["t_monthly_excess_vs_h75"] >= 2.0
    a = h1["excess_vs_oneq"] > 0 and h2["excess_vs_oneq"] > 0 and f["t_monthly_excess_vs_oneq"] >= 2.0
    b = all(x["dd_shallower_than_oneq_pp"] >= 10.0 and x["excess_vs_oneq"] >= -0.03 for x in (h1, h2))
    return {"adds_value_vs_h75": bool(adds), "A": bool(a), "B": bool(b), "pass_vs_oneq": bool(a or b)}


def name_of(rule: str, k_mode: str) -> str:
    return rule if k_mode == "none" else f"{rule}-{'1%' if k_mode == 'k1' else 'ATR'}"


# ======================================================================== main

def run(args=None) -> dict:
    data = {s: load(s) for s in ("QQQ", "QLD", "ONEQ")}
    for s in BASES:
        oq = data["ONEQ"].sessions
        extra = oq[oq >= data[s].sessions[0]].difference(data[s].sessions)
        if len(extra):
            raise ValueError(f"ONEQ sessions not in the {s} calendar: {list(extra[:5])}")
    print("DATA CHECKS:", {k: v.checks for k, v in data.items()})
    res = {"end": END, "data_checks": {k: v.checks for k, v in data.items()}, "n_trials": len(CONFIGS) * len(BASES),
           "bonferroni_t_one_sided_5pct": bonferroni_t(len(CONFIGS) * len(BASES)), "bases": {}}
    rows, by_year, trips_year, mon = [], {}, [], {}
    for base in BASES:
        d = data[base]
        rf = daily_rf(d.sessions, END)
        bounds = period_bounds(d)
        binfo = {"periods": {k: [str(d.sessions[s].date()), str(d.sessions[e].date())] for k, (s, e) in bounds.items()},
                 "configs": {}, "references": {}}
        refs = {}
        for pk, (s, e) in bounds.items():
            for costs in (True, False):
                h75 = simulate(d, s, e, Spec(None, costs=costs))
                h100 = simulate(d, s, e, Spec(None, base_frac=1.0, reserve=False, rebalance=False, costs=costs))
                so = max(d.sessions[s], pd.Timestamp(ONEQ_FIRST_RETURN))
                oq = buy_hold_returns(data["ONEQ"], d.sessions, so, d.sessions[e], HALF_SPREAD_BPS["ONEQ"], costs)
                refs[(pk, costs)] = (h75, h100, oq)
            h75, h100, oq = refs[(pk, True)]
            binfo["references"][pk] = {
                "H75": evaluate(h75["ret"], h75["ret"], h100["ret"], oq, rf),
                "H100": evaluate(h100["ret"], h75["ret"], h100["ret"], oq, rf),
                "ONEQ": {"start": str(oq.index[0].date()), **core_metrics(oq, rf)}}
            if pk == "full":
                by_year[f"{base}_H75"] = yearly(h75["ret"])
                by_year[f"{base}_H100"] = yearly(h100["ret"])
                by_year[f"{base}_ONEQ"] = yearly(oq)
                mon[f"{base}_H75"], mon[f"{base}_H100"] = monthly(h75["ret"]), monthly(h100["ret"])
        for pk in bounds:
            ref = binfo["references"][pk]
            rows.append({"base": base, "config": "H75", "variant": "net", "period": pk, **ref["H75"]})
            rows.append({"base": base, "config": "H100", "variant": "net", "period": pk, **ref["H100"]})
        for rule, km in CONFIGS:
            nm = name_of(rule, km)
            info = {"periods": {}}
            for pk, (s, e) in bounds.items():
                years = (e - s + 1) / TRADING_DAYS
                pr = {}
                variants = {
                    "net": (Spec(rule, km), True),
                    "gross": (Spec(rule, km, costs=False), False),
                    "touch_fill": (Spec(rule, km, through=False), True),
                    "margin_100": (Spec(rule, km, base_frac=1.0, reserve=False, rebalance=False), True),
                }
                for vn, (spec, costs) in variants.items():
                    sim = simulate(d, s, e, spec)
                    h75, h100, oq = refs[(pk, costs)]
                    m = evaluate(sim["ret"], h75["ret"], h100["ret"], oq, rf)
                    m.update(trip_stats(sim["trips"], years))
                    m.update({"orders_per_year": sim["orders"] / years,
                              "cost_drag_per_year": sim["cost_frac"] / years,
                              "gfv_risk_days": sim["gfv_days"], "pdt_breach_share": sim["pdt_breach_share"]})
                    pr[vn] = m
                    rows.append({"base": base, "config": nm, "variant": vn, "period": pk, **m})
                    if pk == "full" and vn == "net":
                        by_year[f"{base}_{nm}"] = yearly(sim["ret"])
                        mon[f"{base}_{nm}"] = monthly(sim["ret"])
                        tr = sim["trips"].copy()
                        if not tr.empty:
                            tr["net"] = tr["gross"] - tr["cost"]
                            g = tr.groupby(tr["date"].dt.year)
                            ty = pd.DataFrame({"trips": g.size(), "gross_usd": g["gross"].sum(),
                                               "cost_usd": g["cost"].sum(), "net_usd": g["net"].sum(),
                                               "net_bp_mean": g.apply(lambda x: (x["net"] / x["notional"]).mean() * 1e4)})
                            ty.insert(0, "config", nm)
                            ty.insert(0, "base", base)
                            trips_year.append(ty.reset_index().rename(columns={"date": "year"}))
                info["periods"][pk] = pr
            info["judgement"] = judge({pk: info["periods"][pk]["net"] for pk in bounds})
            info["judgement_margin_100_report_only"] = {
                "adds_value_vs_h100": bool(info["periods"]["half1"]["margin_100"]["excess_vs_h100"] > 0
                                           and info["periods"]["half2"]["margin_100"]["excess_vs_h100"] > 0
                                           and info["periods"]["full"]["margin_100"]["t_monthly_excess_vs_h100"] >= 2)}
            binfo["configs"][nm] = info
            f, h1, h2 = (info["periods"][k]["net"] for k in ("full", "half1", "half2"))
            print(f"{base} {nm}: trips/yr {f['trips_per_year']:.0f}, gross/net per trip {f['gross_per_trip_bp']:+.1f}/"
                  f"{f['net_per_trip_bp']:+.1f} bp; vs H75 half1 {h1['excess_vs_h75']:+.2%} half2 "
                  f"{h2['excess_vs_h75']:+.2%} t {f['t_monthly_excess_vs_h75']:+.2f}; CAGR {f['cagr']:+.2%} "
                  f"MDD {f['max_dd']:.1%}; {info['judgement']}")
        res["bases"][base] = binfo
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(res, indent=2, default=float))
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", float_format="%.6f")
    if trips_year:
        pd.concat(trips_year).to_csv(OUT / "trips_by_year.csv", index=False, float_format="%.4f")
    mk = pd.DataFrame(mon)
    mk.index = mk.index.astype(str)
    mk.to_csv(OUT / "monthly_returns.csv", float_format="%.6f")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
