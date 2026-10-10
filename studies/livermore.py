"""Systematic Jesse Livermore-style trend strategy: development grid (2017-2022) and frozen one-shot tests.

Rules (each switchable in ``Config``):

- Leading groups: each Friday, eligible names (Nasdaq common stocks, 50-day median dollar volume rank <= 300, raw
  close >= $10, a close on the Friday) are grouped by Fama-French 49 industry. A group's strength is the median
  total return of its members over ``rs_lb`` sessions (groups with fewer than ``min_members`` members are skipped);
  the top ``n_groups`` groups lead.
- Leading stocks: members of a leading group whose relative strength (same return) is in the top ``rs_top`` of the
  whole eligible universe (or, with ``per_group``, the best ``per_group`` names of each leading group).
- Pivotal point (entry signal at a session's close, on split-adjusted closes):
  ``high252`` close above the highest close of the prior 252 sessions (a new 52-week closing high);
  ``baseL_tT`` close above the highest close of the prior L sessions while those L closes stayed inside a range of
  at most T (max / min - 1 <= T), i.e. a breakout from a tight base. Optional volume confirmation: the session's
  volume >= ``vol_mult`` x the average of the prior 50 sessions.
- Pyramiding: the first purchase is ``tranches[0]`` of a full position (full = NAV / K at the first entry); tranche
  j (j >= 1) is added only after the close is at least j x ``add_step`` above the first entry price (never on a
  loss, never averaging down).
- Losses are cut: close at or below (1 - ``stop``) x first entry price (total-return basis).
- Winners run until the trailing exit: ``smaN`` close below its N-session average; ``lowN`` close below the lowest
  close of the prior N sessions; ``pctX`` close X% or more below the highest close since entry.
- Market: ``sma200`` / ``sma50`` QQQ close above its 200 / 50-session average; ``m_exit`` sells everything when the
  market turns off (otherwise it only blocks new entries); ``none`` no filter.
- At most K names. Idle money: ``cash`` (no interest: IBKR pays none on the first $10,000) or ``qqq_on`` (QQQ while
  the market filter is on, cash while it is off).

Timing: every decision uses closes up to session d and is executed at the close of session d+1 (market-on-close
orders a person can place by hand). Costs: IBKR Pro Tiered for the actual order size from
quant.backtest.costs (commission, pass-through, SEC/FINRA on sells, half-spread by dv rank).

HARD DATE GUARD: ``load_window(name)`` truncates every price / return / universe frame to the window's
[price_start, perf_end] and asserts it. The development grid only ever loads ``dev`` (2017-01-01 .. 2022-12-31;
prices from 2015-10 are used for signals only). The one-shot windows can only be run with ``--oneshot`` and the frozen
rule file written before the run; each runs once (it refuses when its output already exists).

Rules, engine and metrics are in quant.strategies.livermore, the windows in quant.data.panel; the grid, the frozen
rule and the one-shots stay here.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study livermore [--oneshot test1|test2 | --evaluate]
        (or scripts/research_livermore.py)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, replace

import numpy as np
import pandas as pd

from quant.data import version as dv
from quant.data.panel import TERMINAL_D5_STRESS
from quant.evaluation.criteria import bonferroni_t, weekly_deflated_sharpe
from quant.paths import ROOT
from quant.strategies import canslim as cs  # noqa: F401  (lv.cs: research_stops, tests)
from quant.strategies.livermore import (  # noqa: F401  (lv.* names read by other scripts and tests)
    CACHE, INPUTS, Config, DateGuardError, RULE_FIELDS, Runner, WINDOWS, WinData, breakout_frame, eligible_universe,
    load_window, market_state, parse_pivot, perf_metrics, simulate, sub_metrics, trade_stats, trail_frame,
    weekly_leaders,
)

OUT_V1 = ROOT / "output/research_only/livermore"
OUT = dv.versioned(OUT_V1)
LEDGER = ROOT / "docs/research_ledger_livermore.md"
FROZEN = OUT_V1 / "frozen_rule.json"   # frozen rules are never versioned


# ======================================================================== development grid

def build_grid() -> list[tuple[str, Config]]:
    base = Config()
    grid, seen = [], set()

    def add(tag, cfg):
        if cfg.name not in seen:
            seen.add(cfg.name)
            grid.append((tag, cfg))

    add("baseline", base)
    # A: portfolio / market / exit factorial on the baseline entry rule
    for k in (3, 5):
        for idle in ("cash", "qqq_on"):
            for market, m_exit in (("sma200", True), ("sma200", False), ("sma50", True), ("none", True)):
                for trail in ("sma50", "low20", "pct20"):
                    add("A_portfolio_market_exit", replace(base, k=k, idle=idle, market=market, m_exit=m_exit,
                                                           trail=trail))
    # B: entry rule (groups, leaders, pivot, volume), one change at a time, idle cash and idle QQQ-when-on
    for idle in ("cash", "qqq_on"):
        b = replace(base, idle=idle)
        for ch in (dict(pivot="base25_t15"), dict(pivot="base50_t20"), dict(pivot="base35_t12"),
                   dict(vol_mult=1.5), dict(rs_lb=252), dict(n_groups=4), dict(n_groups=12), dict(n_groups=None),
                   dict(rs_top=0.10), dict(rs_top=0.30), dict(rs_top=None, per_group=2), dict(n_universe=150)):
            add("B_entry_one_at_a_time", replace(b, **ch))
    # C: pyramiding, stops, trails, number of names, one change at a time
    for idle in ("cash", "qqq_on"):
        b = replace(base, idle=idle)
        for ch in (dict(tranches=(1.0,)), dict(tranches=(1 / 3, 1 / 3, 1 / 3)), dict(add_step=0.10),
                   dict(tranches=(1 / 3, 1 / 3, 1 / 3), add_step=0.10), dict(stop=0.05), dict(stop=0.10),
                   dict(trail="sma20"), dict(trail="low50"), dict(trail="pct15"), dict(trail="pct25"),
                   dict(k=4), dict(k=6)):
            add("C_pyramid_stop_one_at_a_time", replace(b, **ch))
    # D: small factorial of the core Livermore choices
    for pivot in ("high252", "base50_t20"):
        for vm in (None, 1.5):
            for tranches in ((1.0,), (0.5, 0.5)):
                for trail in ("sma50", "pct20"):
                    for idle in ("cash", "qqq_on"):
                        add("D_core_factorial", replace(base, pivot=pivot, vol_mult=vm, tranches=tranches,
                                                        trail=trail, idle=idle))
    return grid


def neighbours(cfg: Config, rows: list) -> list:
    out = []
    for r in rows:
        diff = [f for f in RULE_FIELDS if str(r["cfg"].__getattribute__(f)) != str(getattr(cfg, f))]
        if len(diff) == 1 or (len(diff) == 2 and set(diff) == {"tranches", "add_step"}):
            out.append(r)
    return out


def plateau_pick(rows: list, min_buys: int = 30) -> tuple[dict, pd.DataFrame]:
    """Pre-registered freeze rule: highest median net IR over (variant + its one-change neighbours), among variants
    with >= ``min_buys`` first entries and >= 3 neighbours."""
    scored = []
    for r in rows:
        nb = neighbours(r["cfg"], rows)
        if r["m"]["n_buy"] < min_buys or len(nb) < 3:
            continue
        irs = [r["m"]["ir"]] + [x["m"]["ir"] for x in nb]
        scored.append({"variant": r["variant"], "config": r["cfg"].name, "plateau_ir": float(np.median(irs)),
                       "own_ir": r["m"]["ir"], "n_neighbours": len(nb),
                       "neighbours_beating_qqq": sum(x["m"]["excess_cagr"] > 0 for x in nb)})
    tab = pd.DataFrame(scored).sort_values(["plateau_ir", "own_ir"], ascending=False)
    best = next(r for r in rows if r["variant"] == int(tab.iloc[0]["variant"]))
    return best, tab


def fmt(v):
    if isinstance(v, (np.floating, np.integer)) and not isinstance(v, bool):
        return float(v)
    return v


def run_dev(args) -> dict:
    out = OUT / "dev_2017_2022"
    out.mkdir(parents=True, exist_ok=True)
    data = load_window("dev")
    assert data.spec["effective_end"] <= "2022-12-31" and data.guard["max_session_loaded"] <= "2022-12-31"
    print(data.guard["assertion"])
    for name, g in data.guard["frames"].items():
        print(f"  guard {name}: kept {g['rows_kept']} rows [{g['min_date']} .. {g['max_date']}], dropped "
              f"{g['rows_dropped_after_end']} after the end and {g['rows_dropped_before_start']} before the start")
    runner = Runner(data)
    stress = Runner(load_window("dev", terminal_awaiting=TERMINAL_D5_STRESS))
    grid = build_grid()
    rows = []
    for i, (tag, cfg) in enumerate(grid, 1):
        m, res, bench = runner.run(cfg)
        m2, *_ = runner.run(replace(cfg, spread_mult=2.0))
        rows.append({"variant": i, "tag": tag, "cfg": cfg, "m": m, "excess_spread2x": m2["excess_cagr"],
                     "ir_spread2x": m2["ir"]})
        print(f"[{i}/{len(grid)}] {cfg.name}: CAGR {m['cagr']:+.1%} ex {m['excess_cagr']:+.1%} IR {m['ir']:.2f} "
              f"t {m['t_excess_monthly']:.2f} DD {m['max_dd']:.0%} buys {m['n_buy']} adds {m['n_add']} "
              f"cost {m['cost_drag_per_year']:.2%}", flush=True)
    flat = []
    for r in rows:
        m = {k: fmt(v) for k, v in r["m"].items() if k not in ("by_year", "_monthly_active")}
        d = asdict(r["cfg"])
        d["tranches"] = "/".join(f"{x:.3g}" for x in r["cfg"].tranches)
        flat.append({"variant": r["variant"], "tag": r["tag"], "config": r["cfg"].name, **d, **m,
                     "excess_cagr_spread2x": r["excess_spread2x"], "ir_spread2x": r["ir_spread2x"],
                     "by_year_excess": json.dumps({y: v["excess"] for y, v in r["m"]["by_year"].items()})})
    df = pd.DataFrame(flat)
    df.to_csv(out / "grid.csv", index=False)
    n = len(df)
    trials = df["ir_weekly_per_period"].to_numpy()

    def robust(r, label):
        cfg, m = r["cfg"], r["m"]
        mres = runner.run(cfg)
        _, res, bench = mres
        ms, *_ = stress.run(cfg)
        nb = neighbours(cfg, rows)
        dsr = weekly_deflated_sharpe(float(m["ir_weekly_per_period"]), int(m["weeks"]), float(m["weekly_active_skew"]),
                                  float(m["weekly_active_kurt"]), trials)
        res["nav"].to_frame("nav").assign(qqq=bench, exposure=res["exposure"]).to_csv(out / f"{label}_daily_nav.csv")
        res["trades"].to_csv(out / f"{label}_trades.csv", index=False)
        res["orders"].to_csv(out / f"{label}_orders.csv", index=False)
        nbt = pd.DataFrame([{"variant": x["variant"], "config": x["cfg"].name,
                             "changed": [f for f in RULE_FIELDS if str(getattr(x["cfg"], f)) != str(getattr(cfg, f))],
                             "excess_cagr": x["m"]["excess_cagr"], "ir": x["m"]["ir"], "max_dd": x["m"]["max_dd"],
                             "cagr": x["m"]["cagr"]} for x in nb])
        nbt.to_csv(out / f"{label}_neighbours.csv", index=False)
        return {"variant": r["variant"], "config": cfg.name, "cfg": asdict(cfg),
                "metrics": {k: fmt(v) for k, v in m.items() if k != "_monthly_active"},
                "excess_spread2x": r["excess_spread2x"], "terminal_minus100_excess_cagr": ms["excess_cagr"],
                "neighbours": nbt.to_dict("records"),
                "neighbours_beating_qqq": int((nbt["excess_cagr"] > 0).sum()) if len(nbt) else 0,
                "deflated_sharpe_active_weekly": dsr}

    by_ir = max(rows, key=lambda r: r["m"]["ir"])
    pick, plateau = plateau_pick(rows)
    plateau.to_csv(out / "plateau_scores.csv", index=False)
    base_row = rows[0]
    summary = {
        "date_guard": data.guard, "variants_tried": n, "variants_by_tag": df["tag"].value_counts().to_dict(),
        "variants_beating_qqq": int((df["excess_cagr"] > 0).sum()),
        "variants_beating_qqq_spread2x": int((df["excess_cagr_spread2x"] > 0).sum()),
        "variants_dd_10pts_shallower_and_cagr_within_3": int(((df["max_dd"] - df["qqq_max_dd"] >= 0.10) &
                                                              (df["excess_cagr"] >= -0.03)).sum()),
        "median_excess_cagr": float(df["excess_cagr"].median()),
        "bonferroni_t_one_sided_5pct": float(bonferroni_t(n)),
        "baseline": robust(base_row, "baseline"),
        "top_by_ir": robust(by_ir, "top_by_ir"),
        "plateau_pick": robust(pick, "plateau_pick"),
        "qqq": {"cagr": float(base_row["m"]["qqq_cagr"]), "max_dd": float(base_row["m"]["qqq_max_dd"]),
                "sharpe": float(base_row["m"]["qqq_sharpe"])},
        "carried_universe_weeks": int(runner.u.loc[runner.u["carried"], "week_end"].nunique()),
        "terminal_events": data.terminal_events["status"].value_counts().to_dict() if len(data.terminal_events) else {},
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print("variants:", n, "beating QQQ:", summary["variants_beating_qqq"], "Bonferroni t:",
          round(summary["bonferroni_t_one_sided_5pct"], 2))
    return summary


def run_dev_drop_best_name() -> dict:
    """Post-hoc robustness (development period only): rerun every variant without the single name that made the
    largest dollar profit for the plateau pick, and the three reported variants without their own best name."""
    out = OUT / "dev_2017_2022"
    data = load_window("dev")
    assert data.guard["max_session_loaded"] <= "2022-12-31"
    full = Runner(data)
    grid = build_grid()
    summ = json.loads((out / "summary.json").read_text())

    def best_name(cfg):
        _, res, _ = full.run(cfg)
        o = res["orders"]
        t = res["trades"]
        # dollar P&L per name = sells + terminal values - buys - adds (open positions valued at the end)
        pnl = {}
        for r in o.itertuples():
            pnl[r.sid] = pnl.get(r.sid, 0.0) + (r.value if r.side == "sell" else -r.value)
        return max(pnl, key=pnl.get), pnl

    reported = {}
    for key in ("baseline", "top_by_ir", "plateau_pick"):
        cfg = grid[summ[key]["variant"] - 1][1]
        sid, pnl = best_name(cfg)
        m, *_ = Runner(data, frozenset([sid])).run(cfg)
        reported[key] = {"variant": summ[key]["variant"], "dropped": sid, "dropped_pnl_usd": pnl[sid],
                         "excess_cagr": m["excess_cagr"], "ir": m["ir"], "cagr": m["cagr"], "max_dd": m["max_dd"],
                         "t_excess_monthly": m["t_excess_monthly"]}
        print(key, reported[key], flush=True)
    drop = reported["plateau_pick"]["dropped"]
    ex = Runner(data, frozenset([drop]))
    rows = []
    for i, (tag, cfg) in enumerate(grid, 1):
        m, *_ = ex.run(cfg)
        rows.append({"variant": i, "config": cfg.name, "excess_cagr_ex": m["excess_cagr"], "ir_ex": m["ir"],
                     "max_dd_ex": m["max_dd"], "t_monthly_ex": m["t_excess_monthly"]})
    df = pd.DataFrame(rows)
    df.to_csv(out / f"grid_without_{drop}.csv", index=False)
    res = {"dropped_for_grid": drop, "variants_beating_qqq_without_it": int((df["excess_cagr_ex"] > 0).sum()),
           "median_excess_without_it": float(df["excess_cagr_ex"].median()), "reported": reported}
    (out / "drop_best_name.json").write_text(json.dumps(res, indent=2, default=str))
    print(json.dumps(res, indent=2, default=str))
    return res


# ======================================================================== one-shot tests

def frozen_config() -> tuple[Config, dict]:
    if not FROZEN.is_file():
        raise SystemExit(f"no frozen rule at {FROZEN}: freeze the rule in the ledger first")
    fr = json.loads(FROZEN.read_text())
    c = dict(fr["config"])
    c["tranches"] = tuple(c["tranches"])
    cfg = Config(**c)
    if cfg.name != fr["config_name"]:
        raise SystemExit("frozen rule file is inconsistent")
    text = LEDGER.read_text()
    a, b = text.index("<!-- FREEZE-BEGIN -->"), text.index("<!-- FREEZE-END -->")
    h = hashlib.sha256(text[a:b].encode()).hexdigest()
    if h != fr["ledger_freeze_sha256"]:
        raise SystemExit("the ledger freeze block changed after the rule was frozen")
    return cfg, fr


def write_frozen(cfg: Config, criterion: str) -> None:
    text = LEDGER.read_text()
    a, b = text.index("<!-- FREEZE-BEGIN -->"), text.index("<!-- FREEZE-END -->")
    d = asdict(cfg)
    d["tranches"] = list(cfg.tranches)
    FROZEN.write_text(json.dumps({"config": d, "config_name": cfg.name, "criterion_applied": criterion,
                                  "frozen_at": pd.Timestamp.now("UTC").isoformat(),
                                  "ledger_freeze_sha256": hashlib.sha256(text[a:b].encode()).hexdigest()}, indent=2))


def run_oneshot(name: str) -> dict:
    assert name in ("test1", "test2")
    cfg, fr = frozen_config()
    out = OUT / f"oneshot_{name}"
    if (out / "result.json").exists():
        raise SystemExit(f"{name} has already been run once ({out / 'result.json'}); a one-shot test is not re-run")
    out.mkdir(parents=True, exist_ok=True)
    data = load_window(name)
    print(data.guard["assertion"])
    runner = Runner(data)
    m, res, bench = runner.run(cfg)
    m2, *_ = runner.run(replace(cfg, spread_mult=2.0))
    ms, *_ = Runner(load_window(name, terminal_awaiting=TERMINAL_D5_STRESS)).run(cfg)
    judged = sub_metrics(res["nav"], bench, data.spec["judged_from"], res["cost"], res["exposure"])
    res["nav"].to_frame("nav").assign(qqq=bench, exposure=res["exposure"]).to_csv(out / "daily_nav.csv")
    res["trades"].to_csv(out / "trades.csv", index=False)
    res["orders"].to_csv(out / "orders.csv", index=False)
    judged["_monthly_active"].to_csv(out / "judged_monthly_active.csv", header=["active"])
    result = {"window": name, "config": cfg.name, "frozen_at": fr["frozen_at"], "date_guard": data.guard,
              "full_window": {k: fmt(v) for k, v in m.items() if k != "_monthly_active"},
              "judged_window_from": data.spec["judged_from"],
              "judged": {k: fmt(v) for k, v in judged.items() if k != "_monthly_active"},
              "excess_spread2x": m2["excess_cagr"], "terminal_minus100_excess_cagr": ms["excess_cagr"],
              "carried_universe_weeks": int(runner.u.loc[runner.u["carried"], "week_end"].nunique()),
              "terminal_events": data.terminal_events["status"].value_counts().to_dict()
              if len(data.terminal_events) else {}, "open_positions_at_end": res["open"]}
    (out / "result.json").write_text(json.dumps(result, indent=2, default=str))
    j = judged
    print(f"{name} judged {j['start']}..{j['end']}: CAGR {j['cagr']:+.1%} QQQ {j['qqq_cagr']:+.1%} ex "
          f"{j['excess_cagr']:+.1%} DD {j['max_dd']:.0%} (QQQ {j['qqq_max_dd']:.0%}) t_m {j['t_excess_monthly']:.2f}")
    return result


def recompute_oneshot_metrics(name: str) -> dict:
    """Metrics recomputed from a one-shot's saved daily NAV (no strategy re-run), with the first-month fix."""
    out = OUT / f"oneshot_{name}"
    d = pd.read_csv(out / "daily_nav.csv", index_col=0, parse_dates=True)
    cost = pd.Series(0.0, index=d.index)
    spec = WINDOWS[name]
    full = perf_metrics(d["nav"], d["qqq"], 10_000.0, cost, 0.0, d["exposure"])
    judged = sub_metrics(d["nav"], d["qqq"], spec["judged_from"], cost, d["exposure"])
    judged["_monthly_active"].to_csv(out / "judged_monthly_active_recomputed.csv", header=["active"])
    res = {"note": "recomputed from daily_nav.csv; cost fields are zero here, see result.json for costs",
           "full_window": {k: fmt(v) for k, v in full.items() if k != "_monthly_active"},
           "judged": {k: fmt(v) for k, v in judged.items() if k != "_monthly_active"}}
    (out / "metrics_recomputed.json").write_text(json.dumps(res, indent=2, default=str))
    return res


def evaluate_tests() -> dict:
    """Both pre-registered criteria on the two judged test windows (written after both one-shots ran)."""
    r = {n: recompute_oneshot_metrics(n) for n in ("test1", "test2")}
    ma = pd.concat([pd.read_csv(OUT / f"oneshot_{n}/judged_monthly_active_recomputed.csv", index_col=0)["active"]
                    for n in ("test1", "test2")])
    t = float(ma.mean() / ma.std(ddof=1) * math.sqrt(len(ma)))
    j = {n: r[n]["judged"] for n in r}
    crit_a = {"excess_positive_each": {n: j[n]["excess_cagr"] > 0 for n in j}, "combined_months": len(ma),
              "combined_t_monthly": t}
    crit_a["pass"] = all(crit_a["excess_positive_each"].values()) and t >= 2
    crit_b = {n: {"dd_shallower_pts": j[n]["max_dd"] - j[n]["qqq_max_dd"],
                  "cagr_gap_pts": j[n]["excess_cagr"],
                  "pass": (j[n]["max_dd"] - j[n]["qqq_max_dd"] >= 0.10) and (j[n]["excess_cagr"] >= -0.03)} for n in j}
    crit_b["pass"] = all(v["pass"] for v in crit_b.values() if isinstance(v, dict))
    fr = json.loads(FROZEN.read_text())
    out = {"criterion_applied": fr["criterion_applied"], "A_excess_and_t": crit_a, "B_drawdown": crit_b}
    out["verdict_on_applied"] = crit_a["pass"] if fr["criterion_applied"] == "A" else crit_b["pass"]
    (OUT / "oneshot_evaluation.json").write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps(out, indent=2, default=str))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--oneshot", choices=["test1", "test2"], help="run one frozen one-shot test (once only)")
    p.add_argument("--dev-drop-best-name", action="store_true", help="post-hoc dev robustness: drop the best name")
    p.add_argument("--evaluate", action="store_true", help="apply the pre-registered criteria to both one-shots")
    p.add_argument("--freeze-variant", type=int, help="write the frozen-rule file for this dev grid variant")
    p.add_argument("--criterion", choices=["A", "B"], help="pass criterion that applies (with --freeze-variant)")
    args = p.parse_args(argv)
    if args.freeze_variant:
        if args.criterion is None:
            raise SystemExit("--criterion is required with --freeze-variant")
        cfg = build_grid()[args.freeze_variant - 1][1]
        write_frozen(cfg, args.criterion)
        print("frozen:", cfg.name)
    elif args.dev_drop_best_name:
        run_dev_drop_best_name()
    elif args.oneshot:
        run_oneshot(args.oneshot)
    elif args.evaluate:
        evaluate_tests()
    else:
        run_dev(args)


if __name__ == "__main__":
    main()
