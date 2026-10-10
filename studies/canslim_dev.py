"""Development-period research (2017-01-01 .. 2022-12-31 only) for a systematic CAN SLIM (William O'Neil) strategy.

Rules studied (each letter is a switchable screen; see ``Config``):

- C: latest quarterly EPS (as known at the signal date) vs the same quarter a year earlier >= ``c_min``, with a
  positive year-ago base. Quarterly EPS comes from SEC XBRL companyfacts (diluted, then basic-and-diluted, then
  basic). A missing fiscal Q4 is derived as fiscal-year EPS minus the nine-month year-to-date EPS (or minus the
  three reported quarters).
- A: annual EPS up in each of the last 3 fiscal years with a positive base (``up3``), or a 3-year CAGR >= 25%
  (``cagr25``), or off.
- N: split-adjusted close within ``n_within`` of its 252-session high (new-high proxy).
- S: optional breakout volume: in the 5 sessions to the signal date, an up day with volume >= ``s_min`` x the
  prior 50-session average volume (split-adjusted).
- L: relative strength (total return over ``rs_lookback`` sessions skipping the last ``rs_skip``) in the top
  ``rs_top`` of the eligible universe; buys are ranked by this percentile.
- I: no institutional-ownership data; not used (top-300 dollar volume is a weak proxy for sponsorship).
- M: QQQ above its 50-day and/or 200-day simple moving average at the signal date; otherwise the sleeve is sold
  and the money sits in cash (``idle=cash``, IBKR pays no interest on the first $10,000) or in QQQ (``idle=qqq``).
  ``core`` > 0 adds a buy-and-hold QQQ core beside the CAN SLIM sleeve.

Universe: each week the top ``n_universe`` (<= 300) Nasdaq common stocks by 50-day median dollar volume with a raw
close >= $10 (``weekly_universe_top300.csv.gz``; ranks already require close >= $10), a close on the week end, not a
foreign filer (they have no 10-Q EPS and fail C anyway).

Portfolio: up to K names, each bought at sleeve NAV / K at the close of the session after the signal week end; no
rebalancing of continuing names. Sells: stop loss (close-to-close total return since entry <= -stop, sold at the next
session's close), optional profit target (same timing), fail-the-screen at a rebalance date, and M turning off.
Costs: IBKR Pro Tiered for the actual dollar size (commission $0.0035/share, min $0.35, max 1%, pass-through, SEC and
FINRA TAF on sells, half-spread by dv rank) (quant.backtest.costs).

HARD RULE: no return, price outcome or performance number dated before 2017-01-01 or after 2022-12-31 is computed.
``load_dev_data`` truncates every price/return frame to [PRICE_START, DEV_END] and asserts it; signal-only inputs
(52-week highs, relative strength, moving averages) may use 2015-10..2016 prices; the performance index uses returns
dated on or after PERF_START only (earlier returns are masked and asserted); EPS facts are used only when filed
strictly before the signal date.

The loader, screen, engine and metrics are in quant.strategies.canslim (point-in-time EPS: quant.data.eps); the
grid and the development run stay here.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study canslim_dev [--fetch]   (or scripts/research_canslim_dev.py)
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace

import numpy as np
import pandas as pd

from quant.data import version as dv
from quant.data.eps import (  # noqa: F401  (cs.* names read by other scripts and tests)
    EPS_CACHE, attach_eps, build_eps_states, eps_asof, eps_states, fetch_companyfacts,
)
from quant.data.panel import TERMINAL_D5_STRESS
from quant.evaluation.criteria import bonferroni_t, weekly_deflated_sharpe
from quant.paths import ROOT
from quant.strategies.canslim import (  # noqa: F401  (cs.* names read by other scripts and tests)
    DEV_END, DEV_START, FIRST_SIGNAL, INPUTS, PRICE_START, UNIVERSE_START, Config, DateGuardError, DevData,
    assert_window, build_features, load_dev_data, market_filter, order_cost, perf_metrics, price_features,
    qqq_benchmark, screen, signal_schedule, simulate, truncate, weekly,
)

OUT = dv.versioned(ROOT / "output/research_only/canslim_dev_2017_2022")


# ======================================================================== grid

def build_grid() -> list[tuple[str, Config]]:
    base = Config()
    grid: list[tuple[str, Config]] = []
    seen = set()

    def add(tag, cfg):
        if cfg.name not in seen:
            seen.add(cfg.name)
            grid.append((tag, cfg))

    add("baseline", base)
    # stage A: portfolio x market factorial on the baseline screen
    for k in (5, 8, 10):
        for reb in ("weekly", "monthly"):
            for m in ("none", "sma50", "sma200"):
                for idle in ("cash", "qqq"):
                    for stop in (None, 0.08):
                        for xf in (True, False):
                            add("A_portfolio", replace(base, k=k, rebalance=reb, m_rule=m, idle=idle, stop=stop,
                                                       exit_on_fail=xf))
    # stage B: one-at-a-time screen and rule changes, on four portfolio settings (weekly / monthly x idle cash / QQQ)
    for reb, idle in (("weekly", "cash"), ("monthly", "cash"), ("weekly", "qqq"), ("monthly", "qqq")):
        b = replace(base, rebalance=reb, idle=idle)
        for ch in (dict(c_min=0.20), dict(c_min=0.30), dict(c_min=None),
                   dict(a_rule="cagr25"), dict(a_rule="off"),
                   dict(n_within=0.10), dict(n_within=0.25), dict(n_within=None),
                   dict(rs_lookback=126), dict(rs_skip=0), dict(rs_top=0.10), dict(rs_top=0.30), dict(rs_top=None),
                   dict(s_min=1.4), dict(s_min=1.5),
                   dict(stop=0.07), dict(profit=0.20), dict(profit=0.25),
                   dict(m_rule="sma50_200"), dict(core=0.5), dict(core=0.5, idle="qqq"),
                   dict(eps_timing="d0"), dict(n_universe=150)):
            add("B_one_at_a_time", replace(b, **ch))
    return grid


def run(args) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    data = load_dev_data()
    print(data.guard["assertion"])
    for name, g in data.guard["frames"].items():
        print(f"  guard {name}: kept {g['rows_kept']} rows [{g['min_date']} .. {g['max_date']}], dropped "
              f"{g['rows_dropped_after_end']} after {DEV_END} and {g['rows_dropped_before_start']} before start")
    ciks = data.universe["cik"].dropna().unique()
    if args.fetch:
        log = fetch_companyfacts(ciks)
        print("companyfacts fetch:", {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in log.items()})
    states = {"filed": build_eps_states(ciks, "filed"), "d0": build_eps_states(ciks, "d0")}
    feat = build_features(data, states)
    assert_window(feat["week_end"], FIRST_SIGNAL, DEV_END, "signal weeks")
    # point-in-time check on the built table: no EPS state used on or before its availability date
    used = feat.dropna(subset=["avail"])
    assert (pd.to_datetime(used["avail"]) < used["week_end"]).all(), "EPS look-ahead"
    used = feat.dropna(subset=["d0_avail"])
    assert (pd.to_datetime(used["d0_avail"]) < used["week_end"]).all(), "EPS look-ahead (d0)"
    print(f"point-in-time EPS check: {len(used)} (week, name) rows, every state available before its week: PASS")
    mkt = market_filter(data.qqq_close)
    m_series = {"none": pd.Series(True, index=data.sessions), "sma50": mkt["sma50"], "sma200": mkt["sma200"],
                "sma50_200": mkt["sma50"] & mkt["sma200"]}
    rank_of = {t: dict(zip(g["security_id"], g["dv50_rank"])) for t, g in data.universe.groupby("week_end")}
    weeks = sorted(feat["week_end"].unique())

    def run_cfg(cfg: Config, d: DevData = data):
        sel = screen(feat, cfg)
        picks = {t: list(g["security_id"]) for t, g in sel.groupby("week_end")}
        sched = signal_schedule(weeks, d.sessions, m_series[cfg.m_rule], cfg.rebalance)
        sleeve_acct = cfg.account * (1 - cfg.core)
        res = simulate(cfg, d.sessions, d.perf_idx, d.close, d.last_row, d.qqq_perf_idx, d.qqq_close, sched,
                       picks, rank_of, sleeve_acct)
        nav = res["nav"]
        if cfg.core > 0:
            nav = nav + qqq_benchmark(d.qqq_perf_idx, d.qqq_close, res["dates"], cfg.account * cfg.core)
        bench = qqq_benchmark(d.qqq_perf_idx, d.qqq_close, res["dates"], cfg.account)
        expo = res["exposure"] * res["nav"] / nav
        m = perf_metrics(nav, bench, cfg.account, res["cost"], res["traded"], expo)
        n_pass = sel.groupby("week_end").size().reindex(weeks).fillna(0)
        m.update({k: res[k] for k in ("n_buy", "n_sell", "n_stop", "n_profit", "n_fail", "n_market_off",
                                      "n_qqq_orders")})
        m["avg_names_passing"] = float(n_pass.mean())
        m["weeks_with_zero_passing"] = int((n_pass == 0).sum())
        tr_ = res["trades"]
        if len(tr_):
            m["win_rate_closed"] = float((tr_["ret"] > 0).mean())
            m["avg_trade_ret"] = float(tr_["ret"].mean())
        return m, res, nav, bench

    stress = load_dev_data(terminal_awaiting=TERMINAL_D5_STRESS)
    grid = build_grid()
    rows, keep = [], {}
    for i, (tag, cfg) in enumerate(grid, 1):
        m, res, nav, bench = run_cfg(cfg)
        m2, *_ = run_cfg(replace(cfg, spread_mult=2.0))
        row = {"variant": i, "config": cfg.name, "tag": tag, **asdict(cfg),
               **{k: v for k, v in m.items() if k != "by_year"},
               "excess_cagr_spread2x": m2["excess_cagr"], "ir_spread2x": m2["ir"],
               "by_year_excess": json.dumps({y: v["excess"] for y, v in m["by_year"].items()}),
               "by_year_strategy": json.dumps({y: v["strategy"] for y, v in m["by_year"].items()})}
        rows.append(row)
        keep[cfg.name] = (cfg, m, nav, bench, res)
        print(f"[{i}/{len(grid)}] {cfg.name}: CAGR {m['cagr']:+.1%} ex {m['excess_cagr']:+.1%} IR {m['ir']:.2f} "
              f"t {m['t_excess_weekly']:.2f} DD {m['max_dd']:.0%} buys {m['n_buy']} cost {m['cost_drag_per_year']:.2%}",
              flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "grid.csv", index=False)

    n = len(df)
    trials = df["ir_weekly_per_period"].to_numpy()
    fields = [f for f in Config.__dataclass_fields__ if f not in ("spread_mult", "account")]

    def robustness(row, label):
        cfg, m, nav, bench, res = keep[row["config"]]
        m_stress, *_ = run_cfg(cfg, stress)
        dsr = weekly_deflated_sharpe(float(row["ir_weekly_per_period"]), int(row["weeks"]),
                                  float(row["weekly_active_skew"]), float(row["weekly_active_kurt"]), trials)
        neigh = []
        for r in rows:       # parameter neighbours: variants differing in exactly one rule field
            diff = [f for f in fields if r[f] != getattr(cfg, f) and not (pd.isna(r[f]) and getattr(cfg, f) is None)]
            if len(diff) == 1:
                neigh.append({"changed": diff[0], "value": r[diff[0]], "excess_cagr": r["excess_cagr"],
                              "ir": r["ir"], "variant": r["variant"], "config": r["config"]})
        nav.to_frame("nav").assign(qqq=bench, exposure=res["exposure"]).to_csv(OUT / f"{label}_daily_nav.csv")
        res["trades"].to_csv(OUT / f"{label}_trades.csv", index=False)
        pd.DataFrame(neigh).to_csv(OUT / f"{label}_neighbours.csv", index=False)
        return {"row": {k: fmt(v) for k, v in row.items()}, "by_year": m["by_year"],
                "terminal_minus100_excess_cagr": m_stress["excess_cagr"], "neighbours": neigh,
                "neighbours_beating_qqq": sum(x["excess_cagr"] > 0 for x in neigh),
                "deflated_sharpe_active_weekly": dsr}

    fmt = lambda v: float(v) if isinstance(v, (np.floating, np.integer, float, int)) and not isinstance(v, bool) else v  # noqa: E731
    best = df.sort_values("ir", ascending=False).iloc[0]
    # post-hoc label (written after seeing the grid): a variant that buys at least 4 names a year on average is
    # "actively trading"; the pre-registered pick can be a one-time purchase held for six years
    active = df[df["n_buy"] >= 24].sort_values("ir", ascending=False).iloc[0]
    rob_best = robustness(best, "best")
    rob_active = robustness(active, "best_active")
    base_row = df[df["tag"] == "baseline"].iloc[0]
    summary = {
        "date_guard": data.guard,
        "periods": {"development": [DEV_START, DEV_END], "one_shot_check_1": ["2023-01-01", "2026-07-31"],
                    "one_shot_check_2": ["2012-01-01", "2016-12-31"],
                    "signal_warmup_prices_from": PRICE_START, "first_signal_week": FIRST_SIGNAL},
        "variants_tried": n,
        "variants_by_tag": df["tag"].value_counts().to_dict(),
        "variants_beating_qqq": int((df["excess_cagr"] > 0).sum()),
        "variants_beating_qqq_spread2x": int((df["excess_cagr_spread2x"] > 0).sum()),
        "bonferroni_t_one_sided_5pct": float(bonferroni_t(n)),
        "selection_rule": "highest net information ratio vs QQQ total return over 2017-2022 (set before running)",
        "best_by_ir_preregistered": rob_best,
        "best_actively_trading_post_hoc": rob_active,
        "baseline": {k: fmt(v) for k, v in base_row.items()},
        "qqq": {"cagr": float(best["qqq_cagr"]), "max_dd": float(best["qqq_max_dd"]), "sharpe": float(best["qqq_sharpe"])},
        "terminal_events_in_window": data.terminal_events["status"].value_counts().to_dict()
        if len(data.terminal_events) else {},
        "eps_states": {k: int(len(v)) for k, v in states.items()},
        "eps_coverage_weeks_with_c_value": float(feat["c_growth"].notna().mean()),
        "cost_model": "IBKR Pro Tiered via scripts/research_reversal_dev.py order_cost/half_spread; cash earns 0",
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    feat.to_csv(EPS_CACHE / "features.csv.gz", index=False)
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--fetch", action="store_true", help="fetch missing SEC companyfacts first (<= 3 req/s)")
    p.add_argument("--fetch-only", action="store_true")
    args = p.parse_args(argv)
    if args.fetch_only:
        uni = pd.read_csv(INPUTS / "weekly_universe_top300.csv.gz", dtype=str, usecols=["week_end", "cik"])
        uni = truncate(uni, "week_end", UNIVERSE_START)
        log = fetch_companyfacts(uni["cik"].dropna().unique())
        print(json.dumps({k: (v if not isinstance(v, (list, dict)) else len(v)) for k, v in log.items()}))
        print("errors:", log["error"])
        return
    s = run(args)
    print("variants:", s["variants_tried"], "Bonferroni t:", round(s["bonferroni_t_one_sided_5pct"], 2))


if __name__ == "__main__":
    main()
