"""Data v1 vs v2 comparison of the stock-level strategy studies (docs/robustness_data_v2.md).

Reads only the saved outputs of each study: ``output/research_only/<study>/`` (v1) and ``<study>_v2/`` (v2, written
with REVERSAL_DATA_VERSION=v2). Computes nothing new except the report-only "v2-eligible years" readings from saved
daily NAVs. Writes output/research_only/robustness_data_v2/{comparison.csv, flips.csv, supplement.csv}.
Returns and metrics only; no vendor price level.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RES = ROOT / "output/research_only"
OUT = RES / "robustness_data_v2"
ROWS: list[dict] = []
SUPP: list[dict] = []


def _j(p: Path) -> dict:
    return json.loads(p.read_text())


VERDICT_HINTS = ("criteri", "count", "Full", "full", "WF ", "2018-01..2026-08", "fold1 test", "fold2 test")


def row(study, config, segment, bench, m1: dict, m2: dict, pass1, pass2, note="", level=None):
    """``level``: "verdict" when pass_v1/pass_v2 is the study's own pass/fail for this config, "component" when it is
    only one condition of it (e.g. one half above ONEQ). Only verdict rows can flip a conclusion (section 0.4)."""
    if level is None:
        level = "verdict" if any(h in segment for h in VERDICT_HINTS) else "component"
    r = {"study": study, "config": config, "segment": segment, "benchmark": bench, "level": level}
    for k in ("cagr", "bench_cagr", "t", "max_dd", "bench_max_dd"):
        r[f"{k}_v1"], r[f"{k}_v2"] = m1.get(k), m2.get(k)
    r["pass_v1"], r["pass_v2"] = pass1, pass2
    r["note"] = note
    ROWS.append(r)


def both(rel: str):
    a, b = RES / rel, RES / (rel.split("/")[0] + "_v2" + ("/" + "/".join(rel.split("/")[1:]) if "/" in rel else ""))
    return a, b


# ------------------------------------------------------------------ S1 reversal dev (vs QQQ overlay)
def s1():
    a, b = both("reversal_dev_2012_2016")
    g1, g2 = pd.read_csv(a / "grid.csv"), pd.read_csv(b / "grid.csv")
    s1_, s2_ = _j(a / "summary.json"), _j(b / "summary.json")
    bt = s1_["bonferroni_t_one_sided_5pct"]
    for cfg in ("dv50_N100_K3_monday_ov30", "dv50_N100_K3_monday_ov50"):
        x1, x2 = g1.set_index("config").loc[cfg], g2.set_index("config").loc[cfg]
        f = lambda x: {"cagr": x["excess_net_ann"], "t": x["t_stat"], "max_dd": x["max_dd_overlay"]}  # noqa: E731
        f3 = lambda x: {"cagr": x["excess_net_ann_2014_2016"], "t": x["t_stat_2014_2016"]}  # noqa: E731
        row("S1 reversal", cfg, "2012-2016 net overlay excess", "QQQ", f(x1), f(x2), bool(x1["t_stat"] >= bt),
            bool(x2["t_stat"] >= bt), "cagr = net overlay excess/yr; max_dd = overlay DD")
        row("S1 reversal", cfg, "judged 2014-2016", "QQQ", f3(x1), f3(x2), bool(x1["t_stat_2014_2016"] >= bt),
            bool(x2["t_stat_2014_2016"] >= bt))
    g1 = pd.concat([g1, pd.read_csv(a / "extra_tries.csv")], ignore_index=True)    # 128 grid + 20 extra = 148
    g2 = pd.concat([g2, pd.read_csv(b / "extra_tries.csv")], ignore_index=True)
    n1, n2 = int((g1["t_stat"] >= bt).sum()), int((g2["t_stat"] >= bt).sum())
    d1 = s1_.get("deflated_sharpe_best_monday", {})
    d2 = s2_.get("deflated_sharpe_best_monday", {})
    dsr = lambda d: d.get("dsr", d.get("deflated_sharpe")) if isinstance(d, dict) else d  # noqa: E731
    row("S1 reversal", f"grid ({len(g1)}/{len(g2)} configs)", "count t >= Bonferroni", "QQQ",
        {"t": n1}, {"t": n2}, n1 > 0, n2 > 0,
        f"best by IR v1 {s1_['best_monday_by_ir'].get('config')} / v2 {s2_['best_monday_by_ir'].get('config')}; "
        f"DSR v1 {dsr(d1):.2g} v2 {dsr(d2):.2g} (pass needs >= 0.95); max t v1 {g1['t_stat'].max():.2f} v2 {g2['t_stat'].max():.2f}")
    for y in ("2013",):
        for cfg in ("dv50_N100_K3_monday_ov30",):
            by = [json.loads(g.set_index("config").loc[cfg, "by_year_net"].replace("'", '"')) for g in (g1, g2)]
            SUPP.append({"study": "S1 reversal", "config": cfg, "reading": f"{y} net overlay excess (report-only)",
                         "v1": by[0].get(y), "v2": by[1].get(y)})


# ------------------------------------------------------------------ S2 CAN SLIM (vs QQQ)
def s2():
    a, b = both("canslim_dev_2017_2022")
    g1, g2 = pd.read_csv(a / "grid.csv"), pd.read_csv(b / "grid.csv")
    for v in (78, 215):
        x1, x2 = g1.set_index("variant").loc[v], g2.set_index("variant").loc[v]
        f = lambda x: {"cagr": x["cagr"], "bench_cagr": x["qqq_cagr"], "t": x["t_excess_weekly"],  # noqa: E731
                       "max_dd": x["max_dd"]}
        row("S2 CAN SLIM", f"#{v} {x1['config']}", "dev 2017-2022", "QQQ", f(x1), f(x2),
            bool(x1["excess_cagr"] > 0), bool(x2["excess_cagr"] > 0), "t = weekly excess t (study's own)")
    n1, n2 = int((g1["excess_cagr"] > 0).sum()), int((g2["excess_cagr"] > 0).sum())
    row("S2 CAN SLIM", f"grid ({len(g1)}/{len(g2)})", "count beating QQQ after costs", "QQQ", {"t": n1}, {"t": n2},
        n1 > 0, n2 > 0)


# ------------------------------------------------------------------ S3 O'Neil (vs ONEQ)
def s3():
    for fold, seg in (("fold1", "test_B"), ("fold2", "test_A")):
        a, b = both("oneil")
        r1, r2 = _j(a / f"{fold}_{seg}/result.json"), _j(b / f"{fold}_{seg}/result.json")
        f = lambda r: {"cagr": r["metrics"]["cagr"], "bench_cagr": r["metrics"]["bench_cagr"],  # noqa: E731
                       "t": r["metrics"]["t_excess_monthly"], "max_dd": r["metrics"]["max_dd"],
                       "bench_max_dd": r["metrics"]["bench_max_dd"]}
        row("S3 O'Neil", r1.get("config_name", r1.get("config", fold)) if isinstance(r1.get("config"), str)
            else fold, f"{fold} {seg}", "ONEQ", f(r1), f(r2), r1["criteria"]["pass"], r2["criteria"]["pass"])
    for fold, cal in (("fold1", "A"), ("fold2", "B")):
        p1, p2 = (RES / d / f"{fold}_calibrate_{cal}/summary.json" for d in ("oneil", "oneil_v2"))
        if p1.is_file() and p2.is_file():
            s1_, s2_ = _j(p1), _j(p2)
            pk = lambda s: (s.get("plateau_pick") or {}).get("variant") if isinstance(s.get("plateau_pick"), dict) \
                else s.get("plateau_pick")  # noqa: E731
            SUPP.append({"study": "S3 O'Neil", "config": f"{fold} calibration on {cal}",
                         "reading": "plateau pick (variant #)", "v1": pk(s1_), "v2": pk(s2_)})


# ------------------------------------------------------------------ S4 Livermore (vs QQQ)
def s4():
    a, b = both("livermore")
    for n in ("test1", "test2"):
        j1, j2 = (_j(d / f"oneshot_{n}/metrics_recomputed.json")["judged"] for d in (a, b))
        f = lambda j: {"cagr": j["cagr"], "bench_cagr": j["qqq_cagr"], "t": j["t_excess_monthly"],  # noqa: E731
                       "max_dd": j["max_dd"], "bench_max_dd": j["qqq_max_dd"]}
        row("S4 Livermore", "#20 frozen", f"{n} judged {j1['start']}..{j1['end']}", "QQQ", f(j1), f(j2),
            j1["excess_cagr"] > 0, j2["excess_cagr"] > 0, "per-window part of criterion A (excess > 0)")
    e1, e2 = _j(a / "oneshot_evaluation.json"), _j(b / "oneshot_evaluation.json")
    row("S4 Livermore", "#20 frozen", "criterion A (80 months)", "QQQ",
        {"t": e1["A_excess_and_t"]["combined_t_monthly"]}, {"t": e2["A_excess_and_t"]["combined_t_monthly"]},
        e1["verdict_on_applied"], e2["verdict_on_applied"])
    d1, d2 = (_j(d / "dev_2017_2022/summary.json") for d in (a, b))
    SUPP.append({"study": "S4 Livermore", "config": "dev grid (118)", "reading": "plateau pick / top by IR",
                 "v1": f"#{d1['plateau_pick']['variant']} / #{d1['top_by_ir']['variant']}",
                 "v2": f"#{d2['plateau_pick']['variant']} / #{d2['top_by_ir']['variant']}"})
    g1, g2 = (pd.read_csv(d / "dev_2017_2022/grid.csv").set_index("variant") for d in (a, b))
    SUPP.append({"study": "S4 Livermore", "config": "#20 dev 2017-2022", "reading": "CAGR / excess vs QQQ",
                 "v1": f"{g1.loc[20, 'cagr']:+.1%} / {g1.loc[20, 'excess_cagr']:+.1%}",
                 "v2": f"{g2.loc[20, 'cagr']:+.1%} / {g2.loc[20, 'excess_cagr']:+.1%}"})
    SUPP.append(eligible_from_nav("S4 Livermore", "#20 test2", a, b, "oneshot_test2/daily_nav.csv", "qqq"))


def eligible_from_nav(study, config, a, b, rel, bench_col, start="2013-01-01"):
    """Report-only: CAGR vs benchmark and monthly excess t from ``start`` (2013 is v2-eligible)."""
    out = {"study": study, "config": config, "reading": f"from {start} (v2-eligible years; report-only)"}
    for tag, d in (("v1", a), ("v2", b)):
        nav = pd.read_csv(d / rel, index_col=0, parse_dates=True)
        nav = nav[nav.index >= pd.Timestamp(start) - pd.Timedelta(days=7)]
        base = nav[nav.index < start].iloc[-1] if (nav.index < start).any() else nav.iloc[0]
        nav = nav[nav.index >= start]
        yrs = (nav.index[-1] - base.name).days / 365.25
        c = (nav["nav"].iloc[-1] / base["nav"]) ** (1 / yrs) - 1
        cb = (nav[bench_col].iloc[-1] / base[bench_col]) ** (1 / yrs) - 1
        mm = pd.concat([base.to_frame().T, nav])[["nav", bench_col]].resample("ME").last().pct_change().dropna()
        act = mm["nav"] - mm[bench_col]
        t = act.mean() / act.std(ddof=1) * math.sqrt(len(act))
        out[tag] = f"{c:+.1%} vs {cb:+.1%}, t {t:.2f}"
    return out


# ------------------------------------------------------------------ S5 indicators stock (vs ONEQ)
def s5():
    a, b = both("indicators")
    for n in ("test1", "test2"):
        j1, j2 = (_j(d / f"oneshot_stock_{n}/result.json")["judged"] for d in (a, b))
        f = lambda j: {"cagr": j["cagr"], "bench_cagr": j["bench_cagr"], "t": j["t_excess_monthly"],  # noqa: E731
                       "max_dd": j["max_dd"], "bench_max_dd": j["bench_max_dd"]}
        row("S5 indicators O10", "O10 frozen", f"{n} judged {j1['start']}..{j1['end']}", "ONEQ", f(j1), f(j2),
            j1["cagr"] > j1["bench_cagr"], j2["cagr"] > j2["bench_cagr"], "per-window part of criterion A")
    e1, e2 = (_j(d / "oneshot_stock_evaluation.json") for d in (a, b))
    row("S5 indicators O10", "O10 frozen", "criteria A/B (80 months)", "ONEQ", {"t": e1["A"]["combined_t_monthly"]},
        {"t": e2["A"]["combined_t_monthly"]}, e1["pass"], e2["pass"])
    try:
        SUPP.append(eligible_from_nav("S5 indicators O10", "O10 test2", a, b, "oneshot_stock_test2/daily_nav.csv",
                                      _bench_col(a / "oneshot_stock_test2/daily_nav.csv")))
    except Exception as exc:  # noqa: BLE001
        SUPP.append({"study": "S5 indicators O10", "config": "O10 test2", "reading": "from 2013", "v1": repr(exc)})


def _bench_col(p: Path) -> str:
    cols = pd.read_csv(p, nrows=1).columns
    return next(c for c in ("oneq", "ONEQ", "bench", "qqq") if c in cols)


# ------------------------------------------------------------------ S6 grid check (stock; post hoc)
def s6():
    p1, p2 = RES / "grid_check/picks_both.csv", RES / "grid_check_v2/picks_stock.csv"
    k1, k2 = pd.read_csv(p1), pd.read_csv(p2)
    k1 = k1[k1["setting"] == "stock"]
    keys = ["kind", "test_period", "picked_by"]
    m = k1.merge(k2, on=keys, suffixes=("_1", "_2"))
    for r in m.itertuples():
        row("S6 grid check (stock)", f"{r.kind} by {r.picked_by}: v1 {r.params_1} / v2 {r.params_2}",
            f"test {r.test_period}", "ONEQ",
            {"cagr": r.test_cagr_1, "bench_cagr": r.test_oneq_cagr_1, "t": r.test_t_monthly_1, "max_dd": r.test_max_dd_1},
            {"cagr": r.test_cagr_2, "bench_cagr": r.test_oneq_cagr_2, "t": r.test_t_monthly_2, "max_dd": r.test_max_dd_2},
            bool(r.test_excess_1 > 0), bool(r.test_excess_2 > 0),
            f"grid points beating ONEQ v1 {r.test_points_beating_oneq_1} / v2 {r.test_points_beating_oneq_2}",
            level="verdict")


# ------------------------------------------------------------------ S7 walk forward (stock)
def s7():
    r1, r2 = pd.read_csv(RES / "walk_forward/results_both.csv"), pd.read_csv(RES / "walk_forward_v2/results_stock.csv")
    r1 = r1[(r1["setting"] == "stock") & r1["primary"]]
    r2 = r2[(r2["setting"] == "stock") & r2["primary"]]
    for k in r1["kind"]:
        x1, x2 = r1[r1.kind == k].iloc[0], r2[r2.kind == k].iloc[0]
        f = lambda x: {"cagr": x["cagr"], "bench_cagr": x["bench_cagr"], "t": x["t_monthly_excess"],  # noqa: E731
                       "max_dd": x["max_dd"], "bench_max_dd": x["bench_max_dd"]}
        row("S7 walk-forward (stock)", f"{k} L=3 sharpe", "2018-01..2026-08", "ONEQ", f(x1), f(x2),
            bool(x1["pass"]), bool(x2["pass"]),
            f"halves v1 {x1['h1_cagr']:+.1%}/{x1['h2_cagr']:+.1%} v2 {x2['h1_cagr']:+.1%}/{x2['h2_cagr']:+.1%} "
            f"(ONEQ {x1['h1_bench']:+.1%}/{x1['h2_bench']:+.1%})")


# ------------------------------------------------------------------ S8 stops
def s8():
    w1, w2 = pd.read_csv(RES / "stops/walk_forward.csv"), pd.read_csv(RES / "stops_v2/walk_forward.csv")
    for s in ("livermore", "o10", "canslim"):
        x1 = w1[(w1.strategy == s) & w1.primary].iloc[0]
        x2 = w2[(w2.strategy == s) & w2.primary].iloc[0]
        f = lambda x: {"cagr": x["cagr"], "bench_cagr": x["oneq_cagr"], "t": x["t_monthly_excess"],  # noqa: E731
                       "max_dd": x["max_dd"], "bench_max_dd": x["oneq_max_dd"]}
        row("S8 stops", f"{s} WF 3y sharpe", "2018-01..2026-08", "ONEQ", f(x1), f(x2), bool(x1["pass"]),
            bool(x2["pass"]), f"original stop ({x1['base_stop']}) CAGR v1 {x1['base_cagr']:+.1%} v2 {x2['base_cagr']:+.1%}")


# ------------------------------------------------------------------ S9 megacap
def s9():
    s1_, s2_ = pd.read_csv(RES / "megacap/summary.csv"), pd.read_csv(RES / "megacap_v2/summary.csv")
    v1, v2 = _j(RES / "megacap/results.json"), _j(RES / "megacap_v2/results.json")

    def verdict(res, rule):
        return res["results"][rule]["criteria"]["pass"]

    for rule in ("M1", "M2", "M3", "M4", "M5", "M6"):
        for period in ("H1 2014-2019", "H2 2020-2026-08", "Full 2014-2026-08"):
            x1 = s1_[(s1_.rule == rule) & (s1_.period == period)].iloc[0]
            x2 = s2_[(s2_.rule == rule) & (s2_.period == period)].iloc[0]
            f = lambda x: {"cagr": x["cagr"], "bench_cagr": x["oneq_cagr"],  # noqa: E731
                           "t": x["t_monthly_excess_vs_oneq"], "max_dd": x["max_dd"], "bench_max_dd": x["oneq_max_dd"]}
            last = period.startswith("Full")
            row("S9 megacap", rule, period, "ONEQ", f(x1), f(x2),
                verdict(v1, rule) if last else bool(x1["cagr"] > x1["oneq_cagr"]),
                verdict(v2, rule) if last else bool(x2["cagr"] > x2["oneq_cagr"]),
                "Full row: overall verdict (A or B)" if last else "half: CAGR > ONEQ")
    for rule in ("M1", "M2", "M3", "M6"):
        rr = {}
        for tag, d in (("v1", "megacap"), ("v2", "megacap_v2")):
            y = pd.read_csv(RES / d / "by_year.csv")
            x = y[(y["rule"] == rule) & (y["year"] == 2013)].iloc[0]
            rr[tag] = f"{x['strategy']:+.1%} vs ONEQ {x['oneq']:+.1%}"
        SUPP.append({"study": "S9 megacap", "config": rule, "reading": "2013 return (report-only)", **rr})


# ------------------------------------------------------------------ S10 fundamentals
def s10():
    r1, r2 = _j(RES / "fundamentals/results.json"), _j(RES / "fundamentals_v2/results.json")
    for k in ("U300:WF-F", "U300:WF-C"):
        p1 = r1["walk_forward"][k]["periods"]["WF 2017-2026-08"]
        p2 = r2["walk_forward"][k]["periods"]["WF 2017-2026-08"]
        f = lambda p: {"cagr": p["cagr"], "bench_cagr": p["oneq_cagr"], "t": p.get("t_monthly_excess_vs_oneq",  # noqa: E731
                       p.get("t_monthly_excess")), "max_dd": p["max_dd"], "bench_max_dd": p.get("oneq_max_dd")}
        row("S10 fundamentals", k, "WF 2017-2026-08", "ONEQ", f(p1), f(p2), r1["walk_forward"][k]["criteria"]["pass"],
            r2["walk_forward"][k]["criteria"]["pass"], level="verdict")
    t1, t2 = pd.read_csv(RES / "fundamentals/tests.csv"), pd.read_csv(RES / "fundamentals_v2/tests.csv")
    n1, n2 = int(t1["criterion_pass"].sum()), int(t2["criterion_pass"].sum())
    row("S10 fundamentals", f"96 single/composite tests", "count passing A or B", "ONEQ", {"t": n1}, {"t": n2},
        n1 > 0, n2 > 0, f"Bonferroni passes v1 {int(t1['bonferroni_pass'].sum())} v2 {int(t2['bonferroni_pass'].sum())}")


# ------------------------------------------------------------------ S11 OSAP, S12 sec_alt, S13 MR, S14 EE
def s11():
    c1, c2 = (_j(RES / d / "backtest/results.json")["candidates"] for d in ("osap", "osap_v2"))
    for c in ("P1", "P2"):
        for p in ("A", "B"):
            f = lambda x: {"cagr": x["cagr"], "bench_cagr": x["bench_cagr"], "t": x["t_excess_monthly"],  # noqa: E731
                           "max_dd": x["max_dd"], "bench_max_dd": x["bench_max_dd"]}
            row("S11 OSAP", c, f"period {p}", "ONEQ", f(c1[c][p]), f(c2[c][p]), c1[c][p]["pass"], c2[c][p]["pass"],
                level="component")
        row("S11 OSAP", c, "verdict (both periods)", "ONEQ", {}, {}, c1[c]["verdict"], c2[c]["verdict"],
            level="verdict")


def s12():
    m1, m2 = pd.read_csv(RES / "sec_alt/metrics.csv"), pd.read_csv(RES / "sec_alt_v2/metrics.csv")
    v1, v2 = _j(RES / "sec_alt/summary.json")["verdict"], _j(RES / "sec_alt_v2/summary.json")["verdict"]
    for c in ("I1", "I2", "I3", "F1", "F2"):
        for seg in ("2014-2019", "2020-2026-08", "full"):
            x1 = m1[(m1.config == c) & (m1.segment == seg)].iloc[0]
            x2 = m2[(m2.config == c) & (m2.segment == seg)].iloc[0]
            f = lambda x: {"cagr": x["cagr"], "bench_cagr": x["oneq_cagr"], "t": x["t_monthly_excess_vs_oneq"],  # noqa: E731
                           "max_dd": x["max_dd"], "bench_max_dd": x["oneq_max_dd"]}
            full = seg == "full"
            row("S12 sec_alt", c, seg, "ONEQ", f(x1), f(x2),
                v1[c]["pass"] if full else bool(x1["cagr"] > x1["oneq_cagr"]),
                v2[c]["pass"] if full else bool(x2["cagr"] > x2["oneq_cagr"]),
                "full row: overall verdict" if full else "half: CAGR > ONEQ")


def s13():
    d1, d2 = (_j(RES / d / "results.json")["stocks"] for d in ("mean_reversion", "mean_reversion_v2"))
    for k in ("R3_F1", "R3_F2", "R4_F1", "R4_F2"):
        f = lambda m: {"cagr": m["cagr"], "bench_cagr": m["bench_cagr"], "t": m["t_excess_monthly"],  # noqa: E731
                       "max_dd": m["max_dd"], "bench_max_dd": m["bench_max_dd"]}
        row("S13 mean reversion", k[:2], f"{k[3:]} {d1[k]['metrics']['start']}..{d1[k]['metrics']['end']}", "ONEQ",
            f(d1[k]["metrics"]), f(d2[k]["metrics"]), d1[k]["criteria"]["pass"], d2[k]["criteria"]["pass"],
            level="component")
    for r in ("R3", "R4"):
        row("S13 mean reversion", r, "verdict (both folds)", "ONEQ", {}, {}, d1[f"{r}_verdict"], d2[f"{r}_verdict"],
            level="verdict")


def s14():
    f1, f2 = (_j(RES / d / "summary.json")["folds"] for d in ("earnings_events", "earnings_events_v2"))
    for fold in ("fold1", "fold2"):
        for e in ("E1", "E2", "E3"):
            a, b = f1[fold][e], f2[fold][e]
            f = lambda x: {"cagr": x["test"]["cagr"], "bench_cagr": x["test"]["bench_cagr"],  # noqa: E731
                           "t": x["test"]["t_excess_monthly"], "max_dd": x["test"]["max_dd"],
                           "bench_max_dd": x["test"]["bench_max_dd"]}
            row("S14 earnings events", f"{e}: v1 {a['picked']} / v2 {b['picked']}", f"{fold} test {a['test_half']}",
                "ONEQ", f(a), f(b), a["test"]["crit_pass"], b["test"]["crit_pass"],
                "H1 judged 2014-2017" if a["test_half"] == "H1" else "H2 2019-2026-08")
    y1, y2 = pd.read_csv(RES / "earnings_events/by_year.csv"), pd.read_csv(RES / "earnings_events_v2/by_year.csv")
    for cfg in sorted(set(y1.get("config", pd.Series(dtype=str)))):
        for y in (2013, 2018):
            g = lambda t: t[(t["config"] == cfg) & (t["year"].astype(int) == y)]  # noqa: E731
            a, b = g(y1), g(y2)
            ec = next((c for c in y1.columns if "excess" in c), None)
            if len(a) and len(b) and ec:
                SUPP.append({"study": "S14 earnings events", "config": cfg,
                             "reading": f"{y} excess vs ONEQ (report-only)", "v1": round(float(a[ec].iloc[0]), 4),
                             "v2": round(float(b[ec].iloc[0]), 4)})


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for fn in (s1, s2, s3, s4, s5, s6, s7, s8, s9, s10, s11, s12, s13, s14):
        try:
            fn()
        except FileNotFoundError as exc:
            print(f"{fn.__name__}: missing output ({exc.filename})")
    df = pd.DataFrame(ROWS)
    df["changed"] = df["pass_v1"].astype(object) != df["pass_v2"].astype(object)
    df["flip"] = df["changed"] & (df["level"] == "verdict")
    df.to_csv(OUT / "comparison.csv", index=False, float_format="%.6f")
    df[df["changed"]].to_csv(OUT / "flips.csv", index=False, float_format="%.6f")
    pd.DataFrame(SUPP).to_csv(OUT / "supplement.csv", index=False)
    with pd.option_context("display.width", 250, "display.max_colwidth", 70, "display.max_rows", 500):
        print(df.drop(columns=["note"]).round(4).to_string())
        print(pd.DataFrame(SUPP).to_string())
    print("verdict flips:", int(df["flip"].sum()), "component changes:", int((df["changed"] & ~df["flip"]).sum()))


if __name__ == "__main__":
    main()
