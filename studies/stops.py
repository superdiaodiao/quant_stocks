"""Do stop-loss settings help? Static grid (post-hoc) and walk-forward stop selection (judged) on three frozen rules.

Pre-registered in docs/research_ledger_stops.md BEFORE any result was computed.
Base strategies (rules unchanged, only the stop slot is replaced by quant.backtest.stop_rules.stop_grid()):
  livermore  Livermore #20 (quant.strategies.livermore.simulate), original stop fixed 8%
  o10        indicators stock rule O10, Donchian 20/20 close version (quant.strategies.indicators.stock_sim), no stop
  canslim    CAN SLIM #215 (quant.strategies.canslim.simulate), original stop fixed 8%
One continuous data window (frozen data version 1): prices from 2013-06-01 (warm-up), universe from 2014-01-01,
performance from 2015-01-02 to 2026-08-31. Static view: every setting from cash at 2015-01-02 and at the 2017-12-29
close. Walk-forward: each January (2018..2026) the setting with the best trailing-3-year Sharpe (secondary: Calmar,
CAGR) of the stand-alone runs, stitched into one continuous run from the 2017-12-29 close. Judged vs ONEQ.

Strategies, picking and checks: quant.strategies.stops; metrics: quant.evaluation.nav (``oneq_nav_metrics``,
``oneq_nav_sub_window``); criteria: quant.evaluation.criteria.ab_oneq. Settings, runs and reports stay here.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study stops [--skip-dev-check]   (or scripts/research_stops.py)
"""
from __future__ import annotations

import argparse
import json
import time

import numpy as np
import pandas as pd

from quant.backtest import stop_rules as sr
from quant.data import panel
from quant.data import version as dv
from quant.evaluation.criteria import ab_oneq
from quant.evaluation.nav import oneq_nav_sub_window, oneq_nav_vs_base
from quant.paths import CACHE_ROOT, ROOT
from quant.strategies import canslim as cs
from quant.strategies import indicators as ri
from quant.strategies import stops as stp
from quant.strategies.stops import Strategies, pick, stop_schedule

OUT = dv.versioned(ROOT / "output/research_only/stops")
EPS_ALL = dv.versioned(CACHE_ROOT / "stops/eps_states_filed_all.csv.gz")
RAW = CACHE_ROOT / "indicators/raw"     # ^VIX chart of the indicators study (O10 signal bars)
ACCOUNT = 10_000.0
WINDOW = {"perf_start": "2015-01-01", "perf_end": "2026-08-31", "price_start": "2013-06-01",
          "universe_start": "2014-01-01", "judged_from": "2018-01-01"}
WF_START = "2017-12-29"                 # last close of 2017: walk-forward and comparison runs start here from cash
WF_YEARS = range(2018, 2027)
LOOKBACK = 3
PICK_METRICS = ("sharpe", "calmar", "cagr")
PRIMARY = "sharpe"
HALVES = (("2018-01-01", "2021-12-31"), ("2022-01-01", "2026-08-31"))
BONFERRONI_3 = 2.1280                   # one-sided 5% / 3 primary tests (reported only)
STRATEGIES = ("livermore", "o10", "canslim")
BASE_STOP = {"livermore": sr.StopSpec("fixed", 0.08), "o10": sr.NONE, "canslim": sr.StopSpec("fixed", 0.08)}
FutureDataError = ri.FutureDataError


# ======================================================================== data window and checks (ledger 0.9)

def load(window_name: str = "stops", perf_end: str | None = None):
    data = panel.load_window(window_name, {**WINDOW, **({"perf_end": perf_end} if perf_end else {})})
    assert data.spec["effective_end"] <= "2026-08-31"
    return data


def check_causality(st_full: Strategies, eps: pd.DataFrame, cut: str = "2020-12-31") -> dict:
    """Reload the window truncated at ``cut`` and re-run: NAV up to the cut must equal the full-window NAV."""
    data = load("stops_cut", perf_end=cut)
    assert data.spec["effective_end"] <= cut
    st_cut = Strategies(data, RAW, eps, canslim_first_signal=str(st_full.cweeks[0].date()))
    return stp.check_causality(st_full, st_cut, STRATEGIES)


# ======================================================================== main study

ROW_KEYS = ["start", "end", "cagr", "oneq_cagr", "qqq_cagr", "excess_oneq", "excess_qqq", "vol", "max_dd",
            "oneq_max_dd", "qqq_max_dd", "longest_dd_days", "longest_dd_unrecovered", "ulcer", "sharpe", "sortino",
            "calmar", "oneq_sharpe", "ir_oneq", "t_monthly_excess", "months", "beta_oneq", "alpha_oneq",
            "share_years_beating_oneq", "worst_year", "worst_year_which", "worst_month", "buys", "closed_trades",
            "stop_exits", "win_rate", "avg_win", "avg_loss", "win_loss_ratio", "profit_factor", "avg_hold_days",
            "time_in_market", "avg_exposure", "turnover_one_way_per_year", "cost_drag_per_year", "cost_usd_total"]


def judge(m: dict) -> dict:
    return ab_oneq(m["cagr"], m["max_dd"], m["oneq_cagr"], m["oneq_max_dd"], m["t_monthly_excess"])


def run_study(only=None) -> dict:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    data = load()
    print(data.guard["assertion"])
    ciks = data.universe["cik"].dropna().unique()
    eps = stp.eps_states_all(pd.read_csv(cs.INPUTS / "weekly_universe_top300.csv.gz", dtype=str,
                                         usecols=["cik"])["cik"].dropna().unique(), EPS_ALL)
    cover = float(pd.Series(pd.to_numeric(ciks, errors="coerce")).dropna().astype(int).isin(
        set(eps["cik"].astype(int))).mean())
    st = Strategies(data, RAW, eps, account=ACCOUNT)
    print(f"setup {time.time() - t0:.0f}s; EPS states {len(eps)} rows / {eps['cik'].nunique()} CIKs; "
          f"universe CIK coverage {cover:.1%}; point-in-time EPS rows checked {st.eps_check_rows}")
    meta = {"guard": data.guard["assertion"], "window": WINDOW, "wf_start": WF_START,
            "eps_states_rows": int(len(eps)), "eps_ciks": int(eps["cik"].nunique()), "universe_cik_coverage": cover,
            "canslim_avg_names_passing": float(st.c_n_pass.mean()),
            "canslim_weeks_zero_passing": int((st.c_n_pass == 0).sum())}
    meta["check_equivalence_full_window"] = stp.check_equivalence(st, BASE_STOP)
    print("equivalence:", meta["check_equivalence_full_window"])

    grid = sr.stop_grid()
    strategies = only or STRATEGIES
    static_rows, wf_rows, pick_rows, yearly = [], [], [], {}
    for name in strategies:
        rets, full_m, wfw_m = {}, {}, {}
        for spec in grid:
            res = st.run(name, spec)
            m, _, _ = st.evaluate(res)
            rets[spec.label] = stp.nav_returns(res["nav"], ACCOUNT)
            full_m[spec.label] = m
            res2 = st.run(name, spec, start=WF_START)
            m2, oneq2, qqq2 = st.evaluate(res2)
            wfw_m[spec.label] = m2
            for win, mm in (("2015-2026", m), ("2018-2026", m2)):
                static_rows.append({"strategy": name, "window": win, "stop": spec.label, "stop_kind": spec.kind,
                                    "is_base": spec == BASE_STOP[name], **{k: mm[k] for k in ROW_KEYS}})
            print(f"[{time.time() - t0:5.0f}s] {name:9s} {spec.label:8s} full CAGR {m['cagr']:+.3f} SR {m['sharpe']:.2f} "
                  f"| 2018- CAGR {m2['cagr']:+.3f} vs ONEQ {m2['oneq_cagr']:+.3f} DD {m2['max_dd']:.2f}", flush=True)
        # walk-forward
        base_m = wfw_m[BASE_STOP[name].label]
        for metric in PICK_METRICS:
            picks = {y: pick(rets, y, metric, LOOKBACK) for y in WF_YEARS}
            for y, p in picks.items():
                pick_rows.append({"strategy": name, "metric": metric, "year": y, "stop": p["key"],
                                  "trailing_value": p["value"], "window_last": p["window_last"]})
            sess = st.data.sessions
            perf = sess[(sess >= pd.Timestamp(WF_START)) & (sess <= pd.Timestamp(st.end))]
            sched = stop_schedule(perf, picks)
            res = st.run(name, sched, start=WF_START)
            m, oneq, qqq = st.evaluate(res)
            j = judge(m)
            halves = [oneq_nav_sub_window(res, oneq, qqq, a, b, ACCOUNT) for a, b in HALVES]
            vb = oneq_nav_vs_base(m, base_m)
            n_switch = sum(picks[y]["key"] != picks[y - 1]["key"] for y in list(WF_YEARS)[1:])
            fixed_cagrs = {k: v["cagr"] for k, v in wfw_m.items()}
            rank = int(sum(v > m["cagr"] for v in fixed_cagrs.values()) + 1)
            wf_rows.append({"strategy": name, "metric": metric, "primary": metric == PRIMARY,
                            **{k: m[k] for k in ROW_KEYS}, **j,
                            "h1_cagr": halves[0]["cagr"], "h1_oneq": halves[0]["oneq_cagr"],
                            "h2_cagr": halves[1]["cagr"], "h2_oneq": halves[1]["oneq_cagr"],
                            "switches": n_switch, "rank_among_28_fixed_cagr": rank,
                            **{f"vs_base_{k}": v for k, v in vb.items()},
                            "base_stop": BASE_STOP[name].label, "base_cagr": base_m["cagr"],
                            "base_sharpe": base_m["sharpe"], "base_max_dd": base_m["max_dd"]})
            yearly[f"{name}_wf_{metric}"] = m["by_year"]
            if metric == PRIMARY:
                res["nav"].to_frame("nav").assign(oneq=oneq, qqq=qqq, exposure=res["exposure"]).to_csv(
                    OUT / f"wf_{name}_daily_nav.csv")
                res["trades"].to_csv(OUT / f"wf_{name}_trades.csv", index=False)
            print(f"WF {name} {metric}: CAGR {m['cagr']:+.3f} ONEQ {m['oneq_cagr']:+.3f} t {m['t_monthly_excess']:+.2f} "
                  f"DD {m['max_dd']:.2f}/{m['oneq_max_dd']:.2f} pass={j['pass']} | base CAGR {base_m['cagr']:+.3f}",
                  flush=True)
        yearly[f"{name}_base"] = base_m["by_year"]
    meta["runtime_s"] = time.time() - t0
    return {"static": pd.DataFrame(static_rows), "wf": pd.DataFrame(wf_rows), "picks": pd.DataFrame(pick_rows),
            "yearly": yearly, "meta": meta, "eps": eps, "st": st}


def heat_tables(static: pd.DataFrame) -> None:
    hd = OUT / "heat"
    hd.mkdir(exist_ok=True)
    order = [g.label for g in sr.stop_grid()]
    for win in static["window"].unique():
        s = static[static["window"] == win]
        for col in ("cagr", "excess_oneq", "sharpe", "calmar", "max_dd", "ulcer", "t_monthly_excess",
                    "cost_drag_per_year", "closed_trades"):
            pv = s.pivot(index="stop", columns="strategy", values=col).reindex(order)
            pv.to_csv(hd / f"{win}_{col}.csv", float_format="%.4f")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        cols = ["cagr", "excess_oneq", "sharpe", "calmar", "max_dd", "ulcer"]
        for win in static["window"].unique():
            fig, axes = plt.subplots(1, 3, figsize=(18, 9))
            for ax, name in zip(axes, STRATEGIES):
                s = static[(static["window"] == win) & (static["strategy"] == name)].set_index("stop").reindex(order)
                z = s[cols].astype(float)
                zn = (z - z.mean()) / z.std().replace(0, 1)
                zn["max_dd"], zn["ulcer"] = zn["max_dd"], -zn["ulcer"]
                ax.imshow(zn.to_numpy(), aspect="auto", cmap="RdYlGn")
                for (i, j), v in np.ndenumerate(z.to_numpy()):
                    ax.text(j, i, f"{v:.2f}" if cols[j] in ("sharpe", "calmar", "ulcer") else f"{v:+.0%}",
                            ha="center", va="center", fontsize=7)
                ax.set_xticks(range(len(cols)), cols, rotation=30)
                ax.set_yticks(range(len(order)), order, fontsize=7)
                ax.set_title(f"{name} {win}")
            fig.tight_layout()
            fig.savefig(hd / f"heat_{win}.png", dpi=110)
            plt.close(fig)
    except Exception as exc:  # noqa: BLE001
        print("heatmap png skipped:", exc)


def main(argv=None):
    p = argparse.ArgumentParser(description="stop-loss study (pre-registered in docs/research_ledger_stops.md)")
    p.add_argument("--checks-only", action="store_true", help="dev reproduction + causality checks only")
    p.add_argument("--skip-dev-check", action="store_true")
    a = p.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.checks_only:
        chk = stp.check_dev_reproduction(RAW, EPS_ALL, ACCOUNT)
        (OUT / "check_dev_reproduction.json").write_text(json.dumps(chk, indent=1, default=str))
        print(json.dumps(chk, indent=1, default=str))
        return
    out = run_study()
    meta = out["meta"]
    meta["check_causality_cut_2020_12_31"] = check_causality(out["st"], out["eps"])
    print("causality:", meta["check_causality_cut_2020_12_31"])
    if not a.skip_dev_check:
        meta["check_dev_reproduction"] = stp.check_dev_reproduction(RAW, EPS_ALL, ACCOUNT)
        print("dev reproduction:", meta["check_dev_reproduction"])
    out["static"].to_csv(OUT / "static_all.csv", index=False, float_format="%.6f")
    out["wf"].to_csv(OUT / "walk_forward.csv", index=False, float_format="%.6f")
    out["picks"].to_csv(OUT / "walk_forward_picks.csv", index=False, float_format="%.6f")
    heat_tables(out["static"])
    meta["bonferroni_t_3_primary_one_sided_5pct"] = BONFERRONI_3
    meta["trial_count"] = {"static_post_hoc": int(len(out["static"]) // 2), "walk_forward_configs": int(len(out["wf"])),
                           "primary_tests": len(STRATEGIES)}
    (OUT / "summary.json").write_text(json.dumps({"meta": meta, "yearly": out["yearly"]}, indent=1, default=str))
    pd.set_option("display.width", 250)
    cols = ["strategy", "metric", "cagr", "oneq_cagr", "qqq_cagr", "max_dd", "oneq_max_dd", "sharpe",
            "t_monthly_excess", "A", "B", "pass", "base_cagr", "vs_base_cagr_diff", "vs_base_sharpe_diff",
            "vs_base_beats_base", "switches"]
    print(out["wf"][cols].to_string(float_format=lambda v: f"{v:+.3f}"))
    print(out["picks"][out["picks"]["metric"] == PRIMARY].pivot(index="year", columns="strategy",
                                                                 values="stop").to_string())


if __name__ == "__main__":
    main()
