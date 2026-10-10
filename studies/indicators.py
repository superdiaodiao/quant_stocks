"""Owner's own technical-indicator strategies and textbook MACD / RSI / channel rules: development grids and one-shot tests.

Two applications (pre-registered in docs/research_ledger_indicators.md, section 0, before the first run):

(a) Index timing: in = hold the index ETF, out = a T-bill ETF.  Two instruments:
    ``COMP``: signals on ^IXIC OHLC, P&L on the Nasdaq Composite total return (ONEQ adjusted close from 2003-10-02;
    ^IXIC price return before ONEQ existed);  ``QQQ``: signals on ^NDX OHLC, P&L on QQQ total return.
    Development 1999-03-10 .. 2014-12-31 (every frame truncated at 2014-12-31 and asserted); one-shot 2015-01-02 ..
    2026-09-30 for the single frozen rule.
(b) Individual stocks: Nasdaq common stocks in the weekly dollar-volume top 300 with close >= $10 (frozen data version
    1 panel via ``quant.data.panel.load_window``), at most 5 names, NAV/5 per entry, idle cash at 0%.
    Development 2017-2022; one-shots 2023-01..2026-08-31 and 2012-2016 (2012-2013 reported, not judged).

Benchmark (decided before any result): Nasdaq Composite total return = ONEQ adjusted close; QQQ reported alongside.
Timing: signals from closes up to t, executed at the close of t+1.  Costs: IBKR Pro Tiered (see the ledger).
The stock panel has closes only, so stock indicators use closes for highs / lows (documented in the ledger).

Indicators: quant.signals.indicators; rules, data, engines and metrics: quant.strategies.indicators. The grids, the
freeze and the one-shots stay here.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study indicators --dev both   (or scripts/research_indicators.py)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math

import pandas as pd

from quant.data import version as dv
from quant.evaluation.criteria import weekly_deflated_sharpe
from quant.evaluation.metrics import cagr_of, max_drawdown, monthly, yearly
from quant.paths import ROOT
from quant.signals.indicators import (  # noqa: F401  (ind.* names read by other scripts and tests)
    atr, bollinger, donchian, ema, kdj, macd, rsi, sma, true_range,
)
from quant.strategies import livermore as lv
from quant.strategies.indicators import (  # noqa: F401  (ind.* names read by other scripts and tests)
    FutureDataError, INDEX_DEV_END, INDEX_DEV_START, INDEX_TEST_END, INDEX_TEST_ENTRY, INDEX_TEST_FIRST, RULES,
    RULE_BY_CODE, StockRunner, criteria, index_metrics, index_sim, index_targets, load_index_data, parse_ohlc,
    r_donchian, state_machine, stock_signals, stock_sim, stock_sub_metrics,
)

OUT_V1 = ROOT / "output/research_only/indicators"
OUT = dv.versioned(OUT_V1)
LEDGER = ROOT / "docs/research_ledger_indicators.md"
FROZEN = OUT_V1 / "frozen_rules.json"   # frozen rules are never versioned


# ======================================================================== development runs

def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def run_index_dev() -> pd.DataFrame:
    data = load_index_data(INDEX_DEV_END)
    print("DATE GUARD (index dev): all frames truncated at", INDEX_DEV_END, {k: v["last"] for k, v in data.guard.items()
                                                                             if isinstance(v, dict)})
    rows, monthly_keep = [], {}
    for inst in ("COMP", "QQQ"):
        sess = data.r1[inst].index
        const = pd.Series(1.0, index=sess)
        bench_own = index_sim(const, data, inst, INDEX_DEV_START, INDEX_DEV_END)
        bench_comp = index_sim(pd.Series(1.0, index=data.r1["COMP"].index), data, "COMP", INDEX_DEV_START, INDEX_DEV_END)
        targets = index_targets(data, inst)
        monthly_keep[f"{inst}_buyhold"] = monthly(bench_own["ret"])
        for r in RULES:
            tgt = targets[r.code]
            sim = index_sim(tgt, data, inst, INDEX_DEV_START, INDEX_DEV_END)
            if inst == "COMP":
                m = index_metrics(sim, bench_own, data.rf)
            else:
                # judged against the Composite (ONEQ) on common dates; own-instrument buy-and-hold for robustness
                m = index_metrics(sim, _align(bench_comp, sim), data.rf)
            mo = index_metrics(sim, bench_own, data.rf)
            row = {"instrument": inst, "code": r.code, "rule": r.name, "owner": r.owner, "counted": True,
                   **{k: m[k] for k in ("cagr", "bench_cagr", "excess_cagr", "max_dd", "bench_max_dd", "sharpe",
                                        "calmar", "t_monthly_excess", "ir_monthly", "skew_mx", "kurt_mx", "months",
                                        "time_1x", "trades_per_year", "cost_drag_per_year", "worst_year",
                                        "worst_year_which")},
                   "own_bh_cagr": mo["bench_cagr"], "own_bh_max_dd": mo["bench_max_dd"],
                   "excess_vs_own_bh": mo["excess_cagr"], "t_vs_own_bh": mo["t_monthly_excess"]}
            c = criteria(m["cagr"], m["max_dd"], m["bench_cagr"], m["bench_max_dd"], m["t_monthly_excess"])
            co = criteria(mo["cagr"], mo["max_dd"], mo["bench_cagr"], mo["bench_max_dd"], mo["t_monthly_excess"])
            row.update({"crit_A": c["A"], "crit_B": c["B"], "own_crit_A": co["A"], "own_crit_B": co["B"]})
            for tag, kw in (("sameday", dict(lag=0)), ("cash0", dict(cash_is_etf=False))):
                s2 = index_sim(tgt, data, inst, INDEX_DEV_START, INDEX_DEV_END, **kw)
                row[f"{tag}_cagr"] = cagr_of(s2["ret"])
            # halves of the development period
            for h, (a, b) in (("h1", (INDEX_DEV_START, "2006-12-29")), ("h2", ("2006-12-29", INDEX_DEV_END))):
                sh = index_sim(tgt, data, inst, a, b)
                bh = index_sim(const, data, inst, a, b)
                row[f"{h}_excess_vs_own_bh"] = cagr_of(sh["ret"]) - cagr_of(bh["ret"])
            rows.append(row)
            monthly_keep[f"{inst}_{r.code}"] = monthly(sim["ret"])
    grid = pd.DataFrame(rows)
    n = len(grid)
    grid["dsr"] = [weekly_deflated_sharpe(x.ir_monthly, int(x.months), x.skew_mx, x.kurt_mx,
                                       grid["ir_monthly"].to_numpy())["dsr"] for x in grid.itertuples()]
    out = OUT / "index_dev_1999_2014"
    out.mkdir(parents=True, exist_ok=True)
    grid.to_csv(out / "grid.csv", index=False, float_format="%.6f")
    mk = pd.DataFrame(monthly_keep)
    mk.index = mk.index.astype(str)
    mk.to_csv(out / "monthly_returns.csv", float_format="%.6f")
    (out / "guard.json").write_text(json.dumps({"guard": data.guard, "n_variants": n}, indent=2))
    return grid


def _align(bench: dict, sim: dict) -> dict:
    """Composite benchmark restricted to the dates of ``sim`` (both start at the same entry close)."""
    v = bench["value"].reindex(sim["value"].index).ffill()
    return {"value": v, "ret": v.pct_change().iloc[1:].fillna(0.0)}


def run_stock_dev() -> pd.DataFrame:
    data = lv.load_window("dev")
    print(data.guard["assertion"])
    runner = StockRunner(data)
    rows = []
    out = OUT / "stock_dev_2017_2022"
    out.mkdir(parents=True, exist_ok=True)
    mon = {}
    for r in [x for x in RULES if x.stock_ok]:
        m, res, oneq, qqq = runner.run(r)
        h1 = stock_sub_metrics(res["nav"].loc[:"2019-12-31"], oneq.loc[:"2019-12-31"], qqq.loc[:"2019-12-31"],
                               "2017-01-01", res["cost"].loc[:"2019-12-31"], res["exposure"].loc[:"2019-12-31"])
        h2 = stock_sub_metrics(res["nav"], oneq, qqq, "2020-01-01", res["cost"], res["exposure"])
        c = criteria(m["cagr"], m["max_dd"], m["bench_cagr"], m["bench_max_dd"], m["t_excess_monthly"])
        row = {"code": r.code, "rule": r.name, "owner": r.owner, "counted": True,
               **{k: m[k] for k in ("cagr", "bench_cagr", "excess_cagr", "qqq_cagr", "max_dd", "bench_max_dd",
                                    "qqq_max_dd", "sharpe", "t_excess_monthly", "t_excess_weekly",
                                    "ir_weekly_per_period", "weekly_active_skew", "weekly_active_kurt", "weeks",
                                    "turnover_one_way_per_year", "cost_drag_per_year", "time_in_stocks",
                                    "avg_exposure", "years_beating_bench", "n_buy", "n_sell", "n_take_profit",
                                    "n_delisted", "closed_trades", "win_rate", "avg_trade_ret", "median_days")
                  if k in m},
               "h1_2017_2019_excess": h1["excess_cagr"], "h2_2020_2022_excess": h2["excess_cagr"],
               "crit_A": c["A"], "crit_B": c["B"]}
        rows.append(row)
        mon[r.code] = m["_monthly_active"]
        print(f"{r.code:4s} {r.name:24s} CAGR {m['cagr']:+.1%} ONEQ {m['bench_cagr']:+.1%} DD {m['max_dd']:.0%} "
              f"t {m['t_excess_monthly']:+.2f} buys {m['n_buy']}")
    grid = pd.DataFrame(rows)
    grid["dsr_weekly"] = [weekly_deflated_sharpe(x.ir_weekly_per_period, int(x.weeks), x.weekly_active_skew,
                                              x.weekly_active_kurt, grid["ir_weekly_per_period"].to_numpy())["dsr"]
                          for x in grid.itertuples()]
    grid.to_csv(out / "grid.csv", index=False, float_format="%.6f")
    pd.DataFrame(mon).to_csv(out / "monthly_active_vs_oneq.csv", float_format="%.6f")
    (out / "guard.json").write_text(json.dumps(data.guard, indent=2, default=str))
    return grid


def pick(grid: pd.DataFrame, t_col: str, robust_col: str | None = None) -> tuple[pd.Series, str]:
    """Pre-registered freeze rule (ledger 0.5)."""
    def choose(g):
        a = g[g["crit_A"]]
        if len(a):
            return a.sort_values(t_col, ascending=False).iloc[0], "A"
        b = g[g["crit_B"]]
        if len(b):
            return b.sort_values("excess_cagr", ascending=False).iloc[0], "B"
        return g.sort_values(t_col, ascending=False).iloc[0], "none_highest_t"
    if robust_col is not None:
        g = grid[(grid["crit_A"] & grid[f"{robust_col}_A"]) | (~grid["crit_A"] & grid["crit_B"] & grid[f"{robust_col}_B"])]
        if len(g):
            return choose(g)
    return choose(grid)


# ======================================================================== freeze and one-shots

def _freeze_section() -> str:
    text = LEDGER.read_text()
    a, b = text.index("<!-- FREEZE-BEGIN -->"), text.index("<!-- FREEZE-END -->")
    return text[a:b]


def write_frozen(index_code: str, stock_code: str) -> None:
    FROZEN.write_text(json.dumps({"index_rule": index_code, "index_instrument": "COMP", "stock_rule": stock_code,
                                  "frozen_at": pd.Timestamp.now("UTC").isoformat(),
                                  "ledger_freeze_sha256": hashlib.sha256(_freeze_section().encode()).hexdigest()},
                                 indent=2))


def frozen() -> dict:
    if not FROZEN.exists():
        raise SystemExit("no frozen_rules.json: freeze the rules (ledger section 4) before any one-shot test")
    fr = json.loads(FROZEN.read_text())
    if hashlib.sha256(_freeze_section().encode()).hexdigest() != fr["ledger_freeze_sha256"]:
        raise SystemExit("the ledger freeze section changed after freezing; refusing to run")
    return fr


def run_oneshot_index() -> dict:
    fr = frozen()
    out = OUT / "oneshot_index_2015_2026"
    if (out / "result.json").exists():
        raise SystemExit(f"already run once ({out / 'result.json'}); a one-shot test is not re-run")
    out.mkdir(parents=True, exist_ok=True)
    data = load_index_data(INDEX_TEST_END)
    print("ONE-SHOT index: frames truncated at", INDEX_TEST_END, {k: v["last"] for k, v in data.guard.items()
                                                                   if isinstance(v, dict)})
    rule = RULE_BY_CODE[fr["index_rule"]]
    res = {"rule": rule.code, "name": rule.name, "guard": data.guard}
    bench = index_sim(pd.Series(1.0, index=data.r1["COMP"].index), data, "COMP", INDEX_TEST_ENTRY, INDEX_TEST_END)
    assert str(bench["ret"].index[0].date()) == INDEX_TEST_FIRST
    qqq_bh = index_sim(pd.Series(1.0, index=data.r1["QQQ"].index), data, "QQQ", INDEX_TEST_ENTRY, INDEX_TEST_END)
    res["ONEQ_buyhold"] = {"cagr": cagr_of(bench["ret"]), "max_dd": max_drawdown(bench["value"])}
    res["QQQ_buyhold"] = {"cagr": cagr_of(qqq_bh["ret"]), "max_dd": max_drawdown(qqq_bh["value"])}
    by_year = {"ONEQ_buyhold": yearly(bench["ret"]), "QQQ_buyhold": yearly(qqq_bh["ret"])}
    for inst in ("COMP", "QQQ"):
        bars = data.bars[inst]
        buy, sell = rule.fn(bars)
        tgt = state_machine(buy, sell, bars["close"], rule.take_profit, rule.buy_wins)
        sim = index_sim(tgt, data, inst, INDEX_TEST_ENTRY, INDEX_TEST_END)
        m = index_metrics(sim, bench if inst == "COMP" else _align(bench, sim), data.rf)
        m["state_at_entry_close"] = float(tgt.loc[INDEX_TEST_ENTRY])
        m["sameday_cagr"] = cagr_of(index_sim(tgt, data, inst, INDEX_TEST_ENTRY, INDEX_TEST_END, lag=0)["ret"])
        m["cash0_cagr"] = cagr_of(index_sim(tgt, data, inst, INDEX_TEST_ENTRY, INDEX_TEST_END,
                                               cash_is_etf=False)["ret"])
        m["criteria"] = criteria(m["cagr"], m["max_dd"], m["bench_cagr"], m["bench_max_dd"], m["t_monthly_excess"])
        res[inst] = m
        by_year[f"{rule.code}_{inst}"] = yearly(sim["ret"])
    res["verdict_judged_on"] = "COMP (ONEQ instrument vs ONEQ buy-and-hold)"
    res["pass"] = res["COMP"]["criteria"]["pass"]
    (out / "result.json").write_text(json.dumps(res, indent=2, default=float))
    pd.DataFrame(by_year).to_csv(out / "by_year.csv", float_format="%.6f")
    print(json.dumps({k: res[k] for k in ("ONEQ_buyhold", "QQQ_buyhold")}, indent=1, default=float))
    for inst in ("COMP", "QQQ"):
        m = res[inst]
        print(f"{rule.code} on {inst}: CAGR {m['cagr']:+.2%} vs ONEQ {m['bench_cagr']:+.2%}, DD {m['max_dd']:.1%} "
              f"(ONEQ {m['bench_max_dd']:.1%}), t {m['t_monthly_excess']:.2f}, criteria {m['criteria']}")
    return res


def run_oneshot_stock(name: str) -> dict:
    assert name in ("test1", "test2")
    fr = frozen()
    out = OUT / f"oneshot_stock_{name}"
    if (out / "result.json").exists():
        raise SystemExit(f"{name} already run once ({out / 'result.json'}); a one-shot test is not re-run")
    out.mkdir(parents=True, exist_ok=True)
    data = lv.load_window(name)
    print(data.guard["assertion"])
    rule = RULE_BY_CODE[fr["stock_rule"]]
    runner = StockRunner(data)
    m, res, oneq, qqq = runner.run(rule)
    judged = stock_sub_metrics(res["nav"], oneq, qqq, data.spec["judged_from"], res["cost"], res["exposure"])
    res["nav"].to_frame("nav").assign(oneq=oneq, qqq=qqq, exposure=res["exposure"]).to_csv(out / "daily_nav.csv")
    res["trades"].to_csv(out / "trades.csv", index=False)
    judged["_monthly_active"].to_csv(out / "judged_monthly_active.csv", header=["active"])
    result = {"window": name, "rule": rule.code, "name": rule.name, "date_guard": data.guard,
              "full_window": _clean(m), "judged_from": data.spec["judged_from"], "judged": _clean(judged),
              "carried_universe_weeks": runner.carried_weeks, "open_positions_at_end": res["open"]}
    (out / "result.json").write_text(json.dumps(result, indent=2, default=str))
    j = judged
    print(f"{name} judged {j['start']}..{j['end']}: CAGR {j['cagr']:+.1%} ONEQ {j['bench_cagr']:+.1%} QQQ "
          f"{j['qqq_cagr']:+.1%} DD {j['max_dd']:.0%} (ONEQ {j['bench_max_dd']:.0%}) t_m {j['t_excess_monthly']:.2f}")
    return result


def evaluate_stock() -> dict:
    r = {n: json.loads((OUT / f"oneshot_stock_{n}/result.json").read_text()) for n in ("test1", "test2")}
    ma = pd.concat([pd.read_csv(OUT / f"oneshot_stock_{n}/judged_monthly_active.csv", index_col=0)["active"]
                    for n in ("test1", "test2")])
    t = float(ma.mean() / ma.std(ddof=1) * math.sqrt(len(ma)))
    j = {n: r[n]["judged"] for n in r}
    a = {"cagr_above_oneq_each": {n: j[n]["cagr"] > j[n]["bench_cagr"] for n in j}, "combined_months": len(ma),
         "combined_t_monthly": t}
    a["pass"] = all(a["cagr_above_oneq_each"].values()) and t >= 2
    b = {n: {"dd_shallower_pts": abs(j[n]["bench_max_dd"]) - abs(j[n]["max_dd"]),
             "cagr_gap_pts": j[n]["cagr"] - j[n]["bench_cagr"]} for n in j}
    for n in j:
        b[n]["pass"] = b[n]["dd_shallower_pts"] >= 0.10 and b[n]["cagr_gap_pts"] >= -0.03
    b["pass"] = all(v["pass"] for v in b.values() if isinstance(v, dict))
    res = {"A": a, "B": b, "pass": a["pass"] or b["pass"]}
    (OUT / "oneshot_stock_evaluation.json").write_text(json.dumps(res, indent=2, default=str))
    print(json.dumps(res, indent=2, default=str))
    return res


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dev", choices=["index", "stock", "both"], help="development grids (behind the date guards)")
    p.add_argument("--freeze", nargs=2, metavar=("INDEX_CODE", "STOCK_CODE"), help="write frozen_rules.json")
    p.add_argument("--oneshot", choices=["index", "stock_test1", "stock_test2"], help="run one frozen one-shot (once)")
    p.add_argument("--evaluate-stock", action="store_true", help="apply the stock pass criteria to both one-shots")
    a = p.parse_args(argv)
    if a.dev in ("index", "both"):
        g = run_index_dev()
        print(g[["instrument", "code", "rule", "cagr", "bench_cagr", "max_dd", "bench_max_dd", "t_monthly_excess",
                 "crit_A", "crit_B", "own_crit_A", "own_crit_B", "trades_per_year"]].to_string())
        sel, why = pick(g[g["instrument"] == "COMP"].merge(
            g[g["instrument"] == "QQQ"][["code", "own_crit_A", "own_crit_B"]].rename(
                columns={"own_crit_A": "qqq_A", "own_crit_B": "qqq_B"}), on="code"), "t_monthly_excess", "qqq")
        print("index freeze candidate by the pre-registered rule:", sel["code"], sel["rule"], why)
    if a.dev in ("stock", "both"):
        g = run_stock_dev()
        sel, why = pick(g[g["n_buy"] >= 30], "t_excess_monthly")
        print("stock freeze candidate by the pre-registered rule:", sel["code"], sel["rule"], why)
    if a.freeze:
        write_frozen(*a.freeze)
        print("frozen:", FROZEN.read_text())
    if a.oneshot == "index":
        run_oneshot_index()
    elif a.oneshot:
        run_oneshot_stock(a.oneshot.split("_")[1])
    if a.evaluate_stock:
        evaluate_stock()


if __name__ == "__main__":
    main()
