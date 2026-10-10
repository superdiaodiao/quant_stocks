"""Ways to hold leveraged Nasdaq exposure: a decision aid (pre-registered in docs/research_ledger_leverage_methods.md, section 0).

Methods at target leverage L in {1.0, 1.1, 1.2, 1.3, 1.5, 1.75, 2.0}:
- M   QQQ + IBKR margin loan (EFFR + 1.5%, actual/360), 25% maintenance, real-time forced liquidation at the day's low
- M1  ONEQ + IBKR margin loan (from ONEQ's first close, 2003-10-01)
- Q2  QQQ + QLD (synthetic 2x before 2006-06-22), no loan
- Q3  QQQ + TQQQ (synthetic 3x before 2010-02-12), reference
- T3  TQQQ + T-bill ETF, reference
- F   Nasdaq-100 futures, synthetic (NDX total return - T-bill - basis), fractional contracts (idealised), margin 7%

Rebalance policies: daily, monthly, band (|E/L - 1| > 10%), never. Periods: 1999-03-10..2026-09-30 and the two sub-periods
split at 2009-12-31, plus 211 rolling 10-year windows (every month end 1999-03..2016-09). Costs: IBKR Pro Tiered, $10,000.

Raw data stays local (research_cache/sector_lev/raw, research_cache/leverage_methods/raw); every frame is truncated at
``END`` and asserted. Outputs: output/research_only/leverage_methods/ (returns and metrics only, no price levels).
The market arrays, the engine, the metrics, Kelly and the validations are quant.strategies.leverage_methods; this file
holds the frozen parameters (``P``, the levels, policies, periods and methods) and the run.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study leverage_methods   (or scripts/research_leverage_methods.py)
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from quant.data import sector_etfs as etfs
from quant.data.effr import load_effr
from quant.data.guards import assert_dev_dates
from quant.data.sources.yahoo import parse_chart
from quant.paths import CACHE_ROOT, output_dir
from quant.reporting import write_json
from quant.strategies.leverage_methods import (
    Params, build_market, chart_ohlc, futures_feasibility, kelly, path_metrics, path_returns, path_values,
    rolling_windows, session_index, simulate_batch, single_paths, summarise_rolling, validate_futures, validate_legs,
    vs_bench, window_cagr,
)

END = "2026-09-30"                             # as research_sector_lev (same data, same end)
RAW = CACHE_ROOT / "leverage_methods/raw"
OUT = output_dir("leverage_methods")

LEVELS = (1.0, 1.1, 1.2, 1.3, 1.5, 1.75, 2.0)
POLICIES = ("daily", "monthly", "band", "never")

# ---------------------------------------------------------------- frozen parameters (section 0)
FUT_MAINT = 0.07               # maintenance margin, share of notional (approximate)
P = Params(
    ibkr_spread=0.015,         # IBKR Pro USD tier 0-100k: BM + 1.5% (checked 2026-10-08)
    daycount=360.0,
    spread_2x=etfs.SPREAD_2X,  # 0.70%, frozen QLD calibration (QQQ timing ledger); also used per borrowed unit in 3x
    qqq_er=0.0020,
    oneq_er=0.0021,
    #                   QQQ   ONEQ  QLDX  TQQQX TBILL BENCH
    maint=np.array([0.25, 0.25, 0.50, 0.75, 0.25, 0.25]),
    half_spread=np.array([1e-4, 2e-4, 2e-4, 2e-4, 1e-4, 2e-4]),
    fut_basis=0.0030,          # implied financing above T-bill
    fut_maint=FUT_MAINT,
    fut_init=FUT_MAINT * 1.1,
    fut_trade_cost=1e-4,       # of traded notional
    fut_roll_cost=1e-4,        # of notional, per quarterly roll
    roll_offset=5,             # sessions before expiry
    liq_slip=0.005,
    liq_cushion=1.10,
    min_order=1.0,
    band=0.10,
)
SEED = 20261008

ENTRY_FULL = "1999-03-10"
ONEQ_FIRST = "2003-10-01"
SPLIT_END = "2009-12-31"
ROLL_FIRST, ROLL_LAST, ROLL_MONTHS = "1999-03-31", "2016-09-30", 120


@dataclass(frozen=True)
class Method:
    name: str
    description: str
    not_before: str = ENTRY_FULL


METHODS = (
    Method("M", "QQQ + IBKR margin loan"),
    Method("M1", "ONEQ + IBKR margin loan", ONEQ_FIRST),
    Method("Q2", "QQQ + QLD (2x), no loan"),
    Method("Q3", "QQQ + TQQQ (3x), reference"),
    Method("T3", "TQQQ + T-bill ETF, reference"),
    Method("F", "Nasdaq-100 futures (synthetic, fractional contracts) + T-bill ETF"),
)
METHOD = {m.name: m for m in METHODS}


def paths(sessions: pd.DatetimeIndex, method: str) -> dict:
    """Full period (from the method's first date) and the two sub-periods split at ``SPLIT_END``."""
    return single_paths(sessions, METHOD[method].not_before if method in METHOD else ENTRY_FULL, SPLIT_END)


def run(args=None) -> dict:
    d = etfs.load_data(END)
    effr = load_effr(END, RAW)
    tqqq = parse_chart(RAW / "chart_TQQQ.json", END)
    nq = parse_chart(RAW / "chart_NQ%3DF.json", END)
    lows = {t: chart_ohlc(etfs.RAW / f"chart_{t}.json", END) for t in ("QQQ", "ONEQ")}
    for name, ix in (("effr", effr.index), ("tqqq", tqqq["date"]), ("nq", nq["date"])):
        assert_dev_dates(ix, END)
    mk = build_market(d, effr, tqqq, lows, P)
    s = mk.sessions
    print("DATE GUARD: every frame truncated at", END, mk.info)
    res = {"guard": {**d.guard, **mk.info, "effr_rows": int(len(effr))}, "params": {
        "levels": LEVELS, "policies": POLICIES, "band": P.band, "ibkr_spread": P.ibkr_spread, "spread_2x": P.spread_2x,
        "fut_basis": P.fut_basis, "fut_maint": P.fut_maint, "fut_init": P.fut_init, "roll_offset": P.roll_offset,
        "liq_slip": P.liq_slip, "liq_cushion": P.liq_cushion}}
    res["validation"] = {"legs": validate_legs(d, mk, P.spread_2x),
                         "futures": validate_futures(d, nq, d.raw["QQQ"], P.fut_basis)}
    res["validation"]["effr_vs_ibkr_page"] = {"effr_last": float(effr.iloc[-1]), "ibkr_bm_2026_10_08": 0.0388,
                                              "ibkr_tier1_rate": 0.0538}
    print("validation", json.dumps(res["validation"]["legs"], default=float))
    print("futures", json.dumps({k: v for k, v in res["validation"]["futures"].items() if k != "by_year_median"},
                                default=float))

    rs, re_ = rolling_windows(s, ROLL_FIRST, ROLL_LAST, ROLL_MONTHS)
    oneq_i = session_index(s, ONEQ_FIRST)
    rows, roll_rows = [], []
    bench_cache = {}

    def benches(starts, ends):
        key = (tuple(starts), tuple(ends))
        if key not in bench_cache:
            bench_cache[key] = (simulate_batch(mk, "BENCH_BH", 1.0, "never", starts, ends, P),
                                simulate_batch(mk, "QQQ_BH", 1.0, "never", starts, ends, P))
        return bench_cache[key]

    def one_config(method, L, policy, tag="main", **kw):
        sp = paths(s, method)
        if method == "M1":
            mask = rs >= oneq_i
            st, en = rs[mask], re_[mask]
        else:
            st, en = rs, re_
        labels = list(sp)
        starts = np.r_[[sp[k][0] for k in labels], st]
        ends = np.r_[[sp[k][1] for k in labels], en]
        r = simulate_batch(mk, method, L, policy, starts, ends, replace(P, **kw))
        bb, qq = benches(starts, ends)
        for bi, lab in enumerate(labels):
            v = path_values(r, bi, s)
            pm = path_metrics(v, r, bi)
            rr = path_returns(v)
            bret = path_returns(path_values(bb, bi, s))
            qret = path_returns(path_values(qq, bi, s))
            vb = vs_bench(rr, bret, qret, d.rf, SPLIT_END)
            ab = vb.pop("ab_record", None)
            rows.append({"variant": tag, "method": method, "L": L, "policy": policy, "period": lab, **pm, **vb,
                         "ab_A": ab["A"] if ab else None, "ab_B": ab["B"] if ab else None,
                         "ab_pass_record": ab["pass"] if ab else None})
        nl = len(labels)
        sub = {k: (v[nl:] if isinstance(v, np.ndarray) and v.ndim == 1 else v) for k, v in r.items() if k != "V"}
        sub["V"] = r["V"][:, nl:]
        bsub = {"V": bb["V"][:, nl:], "starts": bb["starts"][nl:], "ends": bb["ends"][nl:], "t0": bb["t0"]}
        qsub = {"V": qq["V"][:, nl:], "starts": qq["starts"][nl:], "ends": qq["ends"][nl:], "t0": qq["t0"]}
        c, mdd = window_cagr(sub, s)
        bc, _ = window_cagr(bsub, s)
        qc, _ = window_cagr(qsub, s)
        summ = summarise_rolling(c, bc, qc, sub["liq"], sub["dead"], mdd)
        oneq_only = sub["starts"] >= oneq_i
        so = summarise_rolling(c[oneq_only], bc[oneq_only], qc[oneq_only], sub["liq"][oneq_only],
                               sub["dead"][oneq_only], mdd[oneq_only])
        roll_rows.append({"variant": tag, "method": method, "L": L, "policy": policy, **summ,
                          "oneq_real_starts_n": so["n"], "oneq_real_p_beat_oneq": so["p_beat_oneq"],
                          "oneq_real_cagr_median": so["cagr_median"],
                          "oneq_real_median_minus_oneq": so["median_minus_oneq"]})
        full = [x for x in rows if x["method"] == method and x["L"] == L and x["policy"] == policy
                and x["period"] == "full" and x["variant"] == tag][-1]
        print(f"{tag:10s} {method:2s} L{L:.2f} {policy:7s} CAGR {full['cagr']:+.2%} MDD {full['max_dd']:.1%} "
              f"liq {full['liquidations']} wipe {full['wiped_out']} fin {full['financing_per_year']:.2%} "
              f"| 10y med {summ['cagr_median']:+.2%} p5 {summ['cagr_p5']:+.2%} beatONEQ {summ['p_beat_oneq']:.0%} "
              f"pLiq {summ['p_liquidation']:.0%}")

    for m in METHODS:
        for L in LEVELS:
            for pol in POLICIES:
                one_config(m.name, L, pol)
    # report-only futures sensitivities
    for tag, kw in (("basis_0", dict(fut_basis=0.0)), ("basis_1pct", dict(fut_basis=0.01)),
                    ("roll_1d", dict(roll_offset=1)), ("roll_10d", dict(roll_offset=10))):
        for L in LEVELS:
            for pol in ("monthly", "never"):
                one_config("F", L, pol, tag=tag, **kw)
    # references
    for nm, method in (("ONEQ_spliced_buyhold", "BENCH_BH"), ("QQQ_buyhold", "QQQ_BH")):
        sp = paths(s, "M")
        labels = list(sp)
        r = simulate_batch(mk, method, 1.0, "never", [sp[k][0] for k in labels], [sp[k][1] for k in labels], P)
        for bi, lab in enumerate(labels):
            v = path_values(r, bi, s)
            rows.append({"variant": "reference", "method": nm, "L": 1.0, "policy": "never", "period": lab,
                         **path_metrics(v, r, bi)})
    # Kelly
    last = len(s) - 1
    i0, isp = session_index(s, ENTRY_FULL), session_index(s, SPLIT_END)
    res["kelly"] = {"full": kelly(mk, i0, last, SEED), "p1": kelly(mk, i0, isp, SEED), "p2": kelly(mk, isp, last, SEED)}
    print("kelly", {k: (v["empirical_Lstar"], v["formula_Lstar"], v["bootstrap"]["p5"], v["bootstrap"]["p95"])
                    for k, v in res["kelly"].items()})
    res["futures_feasibility"] = futures_feasibility(d)

    OUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    pd.DataFrame(roll_rows).to_csv(OUT / "rolling_10y.csv", index=False, float_format="%.6f")
    pd.DataFrame(res["futures_feasibility"]).to_csv(OUT / "futures_feasibility.csv", index=False)
    write_json(OUT / "results.json", res, default=float)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
