"""Re-score already-saved research results against a Nasdaq Composite total-return benchmark.

No rule is changed and no strategy is re-run: every rule return series is read from the files the original
research scripts saved under output/research_only/. Only the benchmark changes.

Benchmarks (raw Yahoo chart JSON, kept local under research_cache/benchmarks/):
  * ONEQ adjusted close  -- Fidelity Nasdaq Composite Index ETF, total return net of its ~0.21% fee (primary).
  * ^IXIC close + yield  -- Composite price index plus an estimated dividend yield (cross-check). The yield for
                            calendar year y = ONEQ cash dividends paid in y / mean ONEQ (split-adjusted) close in y,
                            plus the 0.21% expense ratio added back (fund distributions are net of fees), spread
                            evenly over the sessions of that year.
  * QQQ adjusted close   -- the benchmark the research originally used, for reference.

Usage: PYTHONPATH=. .venv/bin/python scripts/research_benchmark_composite.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "output" / "research_only"
OUT = RES / "benchmark_composite"
RAW = Path("/Users/bytedance/code/quant_stocks/research_cache/benchmarks")
ONEQ_ER = 0.0021


def parse_chart(path: Path) -> tuple[pd.DataFrame, pd.Series]:
    r = json.loads(path.read_text())["chart"]["result"][0]
    ts = pd.to_datetime(r["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    q = r["indicators"]["quote"][0]
    adj = r["indicators"].get("adjclose", [{}])[0].get("adjclose") or q["close"]
    df = pd.DataFrame({"close": q["close"], "adj": adj}, index=pd.DatetimeIndex(ts.strftime("%Y-%m-%d")))
    divs = r.get("events", {}).get("dividends", {})
    d = pd.Series({pd.Timestamp(pd.to_datetime(v["date"], unit="s", utc=True).tz_convert("America/New_York")
                                .strftime("%Y-%m-%d")): v["amount"] for v in divs.values()}, dtype=float)
    return df.dropna(), d.sort_index()


def load_benchmarks() -> tuple[pd.DataFrame, pd.Series]:
    oneq, oneq_div = parse_chart(RAW / "chart_ONEQ.json")
    ixic, _ = parse_chart(RAW / "chart_%5EIXIC.json")
    qqq, _ = parse_chart(RAW / "chart_QQQ.json")
    # estimated Composite dividend yield per calendar year (from ONEQ distributions, fee added back)
    yrs = sorted(set(oneq.index.year))
    yld = {}
    for y in yrs:
        px = oneq.loc[oneq.index.year == y, "close"].mean()
        yld[y] = float(oneq_div[oneq_div.index.year == y].sum() / px + ONEQ_ER)
    yld = pd.Series(yld)
    idx = ixic.index
    n_per_year = pd.Series(idx.year, index=idx).map(pd.Series(idx.year).value_counts())
    daily_y = pd.Series(idx.year, index=idx).map(yld) / n_per_year
    ixic_tr_ret = ixic["close"].pct_change() + daily_y
    level = pd.DataFrame({
        "ONEQ": oneq["adj"],
        "IXIC_TR_est": (1 + ixic_tr_ret.fillna(0)).cumprod(),
        "IXIC_price": ixic["close"],
        "QQQ": qqq["adj"],
    })
    return level, yld


# ----------------------------------------------------------------------------- metrics

def cagr(growth: float, start: pd.Timestamp, end: pd.Timestamp) -> float:
    yrs = (end - start).days / 365.25
    return float(growth ** (1 / yrs) - 1)


def max_dd(nav: pd.Series) -> float:
    return float((nav / nav.cummax() - 1).min())


def tstat(x: pd.Series) -> float:
    x = x.dropna()
    return float(x.mean() / x.std(ddof=1) * math.sqrt(len(x))) if x.std(ddof=1) > 0 else float("nan")


def bench_window(level: pd.DataFrame, base: pd.Timestamp, end: pd.Timestamp, dates=None) -> pd.DataFrame:
    """Benchmark NAVs (base = 1 at the close of ``base``) on ``dates`` (default: all sessions in the window)."""
    lv = level.loc[(level.index >= base) & (level.index <= end), ["ONEQ", "IXIC_TR_est", "QQQ"]]
    if lv.index[0] != base or lv.index[-1] != end:
        raise ValueError(f"benchmark sessions do not match the rule window {base.date()}..{end.date()}")
    nav = lv / lv.iloc[0]
    if dates is not None:
        missing = pd.DatetimeIndex(dates).difference(nav.index)
        if len(missing):
            raise ValueError(f"benchmark missing rule dates: {list(missing[:5])}")
        nav = nav.reindex(dates)
    return nav


def summarise(name: str, label: str, rule_nav: pd.Series, bnav: pd.DataFrame, rule_mdd: float | None,
              bench_mdd: dict, monthly: pd.DataFrame, yearly: pd.DataFrame, extra: dict | None = None) -> dict:
    start, end = rule_nav.index[0], rule_nav.index[-1]
    out = {"name": name, "label": label, "base_close": str(start.date()), "end": str(end.date()),
           "years": round((end - start).days / 365.25, 3)}
    out["cagr"] = {"rule": cagr(rule_nav.iloc[-1] / rule_nav.iloc[0], start, end)}
    for b in ("ONEQ", "IXIC_TR_est", "QQQ"):
        out["cagr"][b] = cagr(bnav[b].iloc[-1] / bnav[b].iloc[0], start, end)
    out["excess_cagr"] = {f"vs_{b}": out["cagr"]["rule"] - out["cagr"][b] for b in ("ONEQ", "IXIC_TR_est", "QQQ")}
    out["max_dd"] = {"rule": rule_mdd, **bench_mdd}
    mx_c = monthly["rule"] - monthly["ONEQ"]
    mx_q = monthly["rule"] - monthly["QQQ"]
    mx_i = monthly["rule"] - monthly["IXIC_TR_est"]
    out["months"] = int(len(monthly))
    out["monthly_excess_vs_ONEQ"] = {"mean": float(mx_c.mean()), "std": float(mx_c.std(ddof=1)), "t": tstat(mx_c),
                                     "ir_ann": float(mx_c.mean() / mx_c.std(ddof=1) * math.sqrt(12))}
    out["monthly_excess_vs_IXIC_TR_est"] = {"mean": float(mx_i.mean()), "t": tstat(mx_i)}
    out["monthly_excess_vs_QQQ"] = {"mean": float(mx_q.mean()), "t": tstat(mx_q)}
    oneq_minus_qqq = monthly["ONEQ"] - monthly["QQQ"]
    out["ONEQ_minus_QQQ_monthly_t"] = tstat(oneq_minus_qqq)
    out["by_year"] = {str(y): {k: round(float(v), 6) for k, v in row.items()} for y, row in yearly.iterrows()}
    if extra:
        out.update(extra)
    return out


def tables(rule: pd.Series, bnav: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Monthly and by-year returns from NAV series (first point is the base)."""
    nav = pd.concat([rule.rename("rule"), bnav], axis=1)
    def per(group):
        last = nav.groupby(group).last()
        prev = last.shift(1)
        prev.iloc[0] = nav.iloc[0]
        return last / prev - 1
    m = per(nav.index.to_period("M"))
    y = per(nav.index.year)
    # drop the base point if it is the only observation in its period
    if (nav.index.to_period("M") == nav.index[0].to_period("M")).sum() == 1:
        m = m.iloc[1:]
    if (nav.index.year == nav.index[0].year).sum() == 1:
        y = y.iloc[1:]
    for t in (m, y):
        t["excess_vs_ONEQ"] = t["rule"] - t["ONEQ"]
        t["excess_vs_QQQ"] = t["rule"] - t["QQQ"]
    return m, y


def daily_nav_case(name, label, path, level, rebase=None, end=None, rule_col="nav"):
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    nav = df[rule_col]
    if rebase is not None:
        nav = nav[nav.index >= pd.Timestamp(rebase)]
    if end is not None:
        nav = nav[nav.index <= pd.Timestamp(end)]
    nav = nav / nav.iloc[0] * 1.0
    bnav = bench_window(level, nav.index[0], nav.index[-1], nav.index)
    full = bench_window(level, nav.index[0], nav.index[-1])
    bmdd = {b: max_dd(full[b]) for b in full.columns}
    # cross-check our QQQ against the QQQ series the research script saved
    saved_q = df["qqq"].reindex(nav.index)
    saved_q = saved_q / saved_q.iloc[0]
    q_gap = float(cagr(saved_q.iloc[-1], nav.index[0], nav.index[-1]) - cagr(bnav["QQQ"].iloc[-1], nav.index[0], nav.index[-1]))
    m, y = tables(nav, bnav)
    s = summarise(name, label, nav, bnav, max_dd(nav), bmdd, m, y,
                  {"check_saved_qqq_cagr_minus_yahoo_qqq_cagr": q_gap, "rule_source": str(path.relative_to(ROOT))})
    return s, m, y


def monthly_case(name, label, folder, col, level, base, end):
    mon = pd.read_csv(folder / "monthly_returns.csv")
    res = json.loads((folder / "results.json").read_text())
    base, end = pd.Timestamp(base), pd.Timestamp(end)
    full = bench_window(level, base, end)
    me = full.iloc[1:].groupby(full.index[1:].to_period("M")).tail(1)
    bm = me / pd.concat([full.iloc[[0]], me.iloc[:-1]]).values - 1
    bm.index = bm.index.to_period("M").astype(str)
    mon = mon.set_index("date")
    if list(mon.index) != list(bm.index):
        raise ValueError("month labels differ")
    # rule NAV at month ends (base = 1 at the entry close)
    rnav = pd.concat([pd.Series([1.0], index=[base]),
                      pd.Series((1 + mon[col]).cumprod().values, index=me.index)])
    bnav = pd.concat([full.iloc[[0]], me])
    m = pd.DataFrame({"rule": mon[col].values}, index=me.index.to_period("M"))
    for b in bm.columns:
        m[b] = bm[b].values
    yy = (1 + m).groupby(m.index.year).prod() - 1
    for t in (m, yy):
        t["excess_vs_ONEQ"] = t["rule"] - t["ONEQ"]
        t["excess_vs_QQQ"] = t["rule"] - t["QQQ"]
    bmdd = {b: max_dd(full[b]) for b in full.columns}
    q_saved = mon["QQQ_buyhold"]
    s = summarise(name, label, rnav, bnav, float(res[col]["max_dd"]), bmdd, m, yy,
                  {"rule_max_dd_note": "daily max drawdown as saved in results.json by the original one-shot run",
                   "check_saved_qqq_monthly_max_abs_diff": float((q_saved.values - bm["QQQ"].values).__abs__().max()),
                   "saved_cagr_daily_252": float(res[col]["cagr"]),
                   "rule_source": str((folder / "monthly_returns.csv").relative_to(ROOT))})
    return s, m, yy


def reversal_case(level):
    path = RES / "reversal_dev_2012_2016" / "weekly" / "dv50_N100_K3_monday_ov30.csv"
    w = pd.read_csv(path, parse_dates=["entry", "exit"])
    if not (w["exit"].iloc[:-1].values == w["entry"].iloc[1:].values).all():
        raise ValueError("weekly holding periods are not contiguous")
    r = w["qqq"] + w["excess_net"]
    chk = (w["nav"].shift(-1) / w["nav"] - 1).iloc[:-1]
    if float((chk - r.iloc[:-1]).abs().max()) > 1e-9:
        raise ValueError("weekly return does not reproduce the saved NAV")
    dates = pd.DatetimeIndex([w["entry"].iloc[0]] + list(w["exit"]))
    nav = pd.Series(np.r_[1.0, (1 + r).cumprod().values], index=dates)
    bnav = bench_window(level, dates[0], dates[-1], dates)
    full = bench_window(level, dates[0], dates[-1])
    bmdd = {b: max_dd(full[b]) for b in full.columns}
    saved_q = pd.Series(np.r_[1.0, (1 + w["qqq"]).cumprod().values], index=dates)
    q_gap = float(cagr(saved_q.iloc[-1], dates[0], dates[-1]) - cagr(bnav["QQQ"].iloc[-1], dates[0], dates[-1]))
    m, y = tables(nav, bnav)
    s = summarise("reversal_rev_ind_v1", "DEV ONLY (in-sample, never tested): rev-ind-v1 = dv50_N100_K3_monday_ov30, "
                  "100% QQQ + 30/30 overlay, 2012-2016", nav, bnav, max_dd(nav), bmdd, m, y,
                  {"rule_max_dd_note": "on weekly (Monday-close) NAV; benchmark drawdowns are daily",
                   "rule_max_dd_weekly_benchmarks": {b: max_dd(bnav[b]) for b in bnav.columns},
                   "overlay_excess_cagr_vs_qqq_from_ledger": 0.101,
                   "check_saved_qqq_cagr_minus_yahoo_qqq_cagr": q_gap,
                   "month_assignment": "each weekly holding period is assigned to the month of its exit date",
                   "rule_source": str(path.relative_to(ROOT))})
    return s, m, y


def criteria_recheck(c: dict) -> dict:
    """The pre-registered pass rules, re-applied with ONEQ in place of QQQ. Post hoc: information only."""
    out = {"note": "post hoc: benchmark changed after the results were seen; does not replace the original verdicts"}
    for k in ("qqq200_T01_L200_b2", "dow_R_p5_D15"):
        s = c[k]
        calmar_r = s["cagr"]["rule"] / abs(s["max_dd"]["rule"])
        calmar_b = s["cagr"]["ONEQ"] / abs(s["max_dd"]["ONEQ"])
        dd_pts = s["max_dd"]["rule"] - s["max_dd"]["ONEQ"]
        gap = s["cagr"]["rule"] - s["cagr"]["ONEQ"]
        r = {"c1_maxdd_shallower_pts": dd_pts, "c1_pass_ge_10pp": dd_pts >= 0.10,
             "c2_calmar_rule": calmar_r, "c2_calmar_ONEQ": calmar_b, "c2_pass": calmar_r > calmar_b,
             "c3_cagr_gap": gap, "c3_pass_shortfall_le_3pp": gap >= -0.03}
        r["pass_all_three"] = bool(r["c1_pass_ge_10pp"] and r["c2_pass"] and r["c3_pass_shortfall_le_3pp"])
        out[k] = r
    t1, t2 = c["livermore20_test1"], c["livermore20_test2_judged"]
    mx = pd.concat([pd.read_csv(OUT / f"{n}_monthly.csv", index_col=0).eval("rule - ONEQ")
                    for n in ("livermore20_test1", "livermore20_test2_judged")])
    a = {"A1_test1_excess": t1["excess_cagr"]["vs_ONEQ"], "A2_test2_excess": t2["excess_cagr"]["vs_ONEQ"],
         "A3_combined_months": int(len(mx)), "A3_combined_t": tstat(mx)}
    a["pass_A"] = bool(a["A1_test1_excess"] > 0 and a["A2_test2_excess"] > 0 and a["A3_combined_t"] >= 2)
    for n, s in (("test1", t1), ("test2", t2)):
        a[f"B_{n}_dd_shallower_pts"] = s["max_dd"]["rule"] - s["max_dd"]["ONEQ"]
    out["livermore20"] = a
    return out


def main():
    level, yld = load_benchmarks()
    OUT.mkdir(parents=True, exist_ok=True)
    cases = [
        monthly_case("qqq200_T01_L200_b2", "ONE-SHOT TEST: QQQ 200-day rule T01_L200_b2, 2015-01-02..2026-09-30",
                     RES / "qqq_timing_oneshot", "T01_L200_b2", level, "2014-12-31", "2026-09-30"),
        monthly_case("dow_R_p5_D15", "ONE-SHOT TEST: Dow theory R_p5_D15, 2015-01-02..2026-09-30",
                     RES / "dow_theory" / "oneshot", "R_p5_D15", level, "2014-12-31", "2026-09-30"),
        daily_nav_case("livermore20_test1", "ONE-SHOT TEST 1: Livermore #20, 2023-01-03..2026-08-31",
                       RES / "livermore" / "oneshot_test1" / "daily_nav.csv", level),
        daily_nav_case("livermore20_test2_judged", "ONE-SHOT TEST 2 (judged part): Livermore #20, rebased at the "
                       "2013-12-31 close, 2014-01-02..2016-12-30",
                       RES / "livermore" / "oneshot_test2" / "daily_nav.csv", level, rebase="2013-12-31"),
        daily_nav_case("canslim215_dev", "DEV ONLY (in-sample, never tested): CAN SLIM #215, 2017-2022",
                       RES / "canslim_dev_2017_2022" / "best_active_daily_nav.csv", level),
        reversal_case(level),
    ]
    summary = {"benchmarks": {
        "ONEQ": "Yahoo adjusted close of ONEQ (dividends reinvested, net of ~0.21%/yr fee) -- primary",
        "IXIC_TR_est": "^IXIC close + estimated dividend yield (ONEQ distributions / mean ONEQ close per calendar "
                       "year + 0.21% fee added back), spread evenly over that year's sessions -- cross-check",
        "QQQ": "Yahoo adjusted close of QQQ -- the original benchmark",
        "raw_files": str(RAW),
        "estimated_composite_yield_by_year": {str(k): round(v, 5) for k, v in yld.items()}},
        "cases": {}}
    for s, m, y in cases:
        summary["cases"][s["name"]] = s
        mm = m.copy()
        mm.index = mm.index.astype(str)
        mm.to_csv(OUT / f"{s['name']}_monthly.csv", float_format="%.6f")
        mm.index.name, y.index.name = "month", "year"
        y.to_csv(OUT / f"{s['name']}_by_year.csv", float_format="%.6f")
    summary["criteria_recheck_post_hoc"] = criteria_recheck(summary["cases"])
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=float))
    print(json.dumps(summary["criteria_recheck_post_hoc"], indent=1, default=float))
    rows = []
    for s in summary["cases"].values():
        rows.append({"case": s["name"], "start_base": s["base_close"], "end": s["end"],
                     "cagr_rule": s["cagr"]["rule"], "cagr_ONEQ": s["cagr"]["ONEQ"],
                     "cagr_IXIC_TR_est": s["cagr"]["IXIC_TR_est"], "cagr_QQQ": s["cagr"]["QQQ"],
                     "excess_vs_ONEQ": s["excess_cagr"]["vs_ONEQ"], "excess_vs_IXIC_TR_est": s["excess_cagr"]["vs_IXIC_TR_est"],
                     "excess_vs_QQQ": s["excess_cagr"]["vs_QQQ"],
                     "mdd_rule": s["max_dd"]["rule"], "mdd_ONEQ": s["max_dd"]["ONEQ"], "mdd_QQQ": s["max_dd"]["QQQ"],
                     "months": s["months"], "t_monthly_vs_ONEQ": s["monthly_excess_vs_ONEQ"]["t"],
                     "t_monthly_vs_IXIC_TR_est": s["monthly_excess_vs_IXIC_TR_est"]["t"],
                     "t_monthly_vs_QQQ": s["monthly_excess_vs_QQQ"]["t"]})
    tab = pd.DataFrame(rows)
    tab.to_csv(OUT / "comparison.csv", index=False, float_format="%.6f")
    pd.set_option("display.width", 250)
    print(tab.round(4).to_string())
    for s in summary["cases"].values():
        print(s["name"], {k: v for k, v in s.items() if k.startswith("check")})


if __name__ == "__main__":
    main()
