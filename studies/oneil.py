"""William O'Neil's method ("How to Make Money in Stocks"), coded as faithfully as closes + volume allow.

Two-fold cross-check (pre-registered in docs/research_ledger_oneil.md before any run):
  period A = 2014-01-01 .. 2019-12-31, period B = 2020-01-01 .. 2026-08-31.
  fold 1: calibrate (12 variants) on A, freeze, test once on B; fold 2: calibrate on B, freeze, test once on A.

Elements:
- C, A, L and leading industry groups on Fridays (point-in-time SEC EPS, IBD-style weighted RS rank >= 80, FF49 group
  strength in the top 20%); optional RS-line new high.
- N: cup with handle, flat base and double bottom detected on split-adjusted closes; breakout = close above the pivot
  with volume >= 1.4 x the prior 50-session average; buy at the next close only within 5% of the pivot.
- M by O'Neil's own rules on ^IXIC OHLCV: distribution days and follow-through days -> confirmed uptrend / correction.
- Sells: 8% stop, 20% profit target, the 8-week hold for stocks up 20% within 3 weeks, close below the 50-day line on
  heavy volume, and selling non-profitable positions while the market is in correction. At most 5 names; idle cash.

Every decision uses data up to session d's close and is filled at session d+1's close. Costs: IBKR Pro Tiered for the
actual order (quant.backtest.costs). Judged benchmark: ONEQ total return (Nasdaq Composite); QQQ reported.

HARD DATE GUARD: ``load_period(name)`` truncates every frame (stock panel, universe, QQQ, ONEQ, ^IXIC, terminal values,
EPS states by filing date) to [warm-up start, period end] and asserts it.

Patterns, market direction, sell rules, screen, engine and the plateau freeze rule: quant.strategies.oneil. The
periods, the calibration grid, the freeze files, the one-shot tests and the context reports stay here.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study oneil --calibrate fold1   (or scripts/research_oneil.py)
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace

import pandas as pd

from quant.data import version as dv
from quant.data.panel import TERMINAL_D5, TERMINAL_D5_STRESS
from quant.evaluation.metrics import max_drawdown_of_returns
from quant.paths import CACHE_ROOT, ROOT
from quant.reporting import freeze_sha256, public, scalar
from quant.strategies import canslim as cs
from quant.strategies import oneil as ons
from quant.strategies.oneil import CAL_FIELDS, Config, Period, Runner, plateau_pick

OUT_V1 = ROOT / "output/research_only/oneil"
OUT = dv.versioned(OUT_V1)
LEDGER = ROOT / "docs/research_ledger_oneil.md"
RAW = CACHE_ROOT / "oneil/raw"
EPS_CACHE = dv.versioned(CACHE_ROOT / "oneil/eps_states_filed.csv.gz")
DateGuardError = cs.DateGuardError

PERIODS = {
    "A": {"perf_start": "2014-01-01", "perf_end": "2019-12-31", "price_start": "2012-06-01",
          "universe_start": "2013-01-01", "judged_from": "2014-01-01"},
    "B": {"perf_start": "2020-01-01", "perf_end": "2026-08-31", "price_start": "2018-06-01",
          "universe_start": "2019-01-01", "judged_from": "2020-01-01"},
}
FOLDS = {"fold1": {"calibrate": "A", "test": "B"}, "fold2": {"calibrate": "B", "test": "A"}}


def load_period(name: str, terminal_awaiting: float = TERMINAL_D5) -> Period:
    return ons.load_period(name, PERIODS[name], RAW, EPS_CACHE, terminal_awaiting)


# ======================================================================== calibration / freeze / test

def calibration_grid() -> list[Config]:
    return [Config(dd_limit=a, ftd_gain=b, rs_line=c) for a in (4, 5, 6) for b in (0.0125, 0.017) for c in (False, True)]


def _fmt(v):
    return scalar(v)


def _clean(m: dict) -> dict:
    return public(m, convert=True)


def print_guard(per: Period):
    print(per.guard["assertion"])
    for name, g in per.guard["frames"].items():
        print(f"  guard {name}: kept {g['rows_kept']} rows [{g['min_date']} .. {g['max_date']}], dropped "
              f"{g['rows_dropped_after_end']} after the end and {g['rows_dropped_before_start']} before the start")
    print("  guard eps_states:", per.guard["eps_states"], " ^IXIC:", per.guard["ixic"])


def run_calibration(fold: str) -> dict:
    pname = FOLDS[fold]["calibrate"]
    out = OUT / f"{fold}_calibrate_{pname}"
    out.mkdir(parents=True, exist_ok=True)
    per = load_period(pname)
    print_guard(per)
    assert per.guard["max_session_loaded"] <= per.spec["perf_end"]
    runner = Runner(per)
    ev = runner.events
    ev.to_csv(out / "breakout_events.csv", index=False)
    cfgs = calibration_grid()
    rows = []
    for i, cfg in enumerate(cfgs, 1):
        m, res, oneq, qqq = runner.run(cfg)
        m2, *_ = runner.run(replace(cfg, spread_mult=2.0))
        row = {"variant": i, "config": cfg.name, **{f: getattr(cfg, f) for f in CAL_FIELDS},
               **{k: _fmt(v) for k, v in m.items() if k not in ("by_year", "_monthly_active")},
               "excess_cagr_spread2x": m2["excess_cagr"],
               "by_year_excess": json.dumps({y: v["excess"] for y, v in m["by_year"].items()})}
        rows.append(row)
        print(f"[{i}/12] {cfg.name}: CAGR {m['cagr']:+.1%} ONEQ {m['bench_cagr']:+.1%} ex {m['excess_cagr']:+.1%} "
              f"IR {m['ir']:.2f} t_m {m['t_excess_monthly']:.2f} DD {m['max_dd']:.0%} (ONEQ {m['bench_max_dd']:.0%}) "
              f"buys {m['n_buy']} expo {m['avg_exposure']:.0%} up {m['uptrend_share']:.0%}", flush=True)
    grid = pd.DataFrame(rows)
    grid.to_csv(out / "grid.csv", index=False)
    pick, plateau, note = plateau_pick(rows, cfgs)
    plateau.to_csv(out / "plateau_scores.csv", index=False)
    by_ir = int(grid.sort_values("ir", ascending=False).iloc[0]["variant"])
    for label, v in (("plateau_pick", pick), ("top_by_ir", by_ir)):
        m, res, oneq, qqq = runner.run(cfgs[v - 1])
        res["nav"].to_frame("nav").assign(oneq=oneq, qqq=qqq, exposure=res["exposure"]).to_csv(out / f"{label}_daily_nav.csv")
        res["trades"].to_csv(out / f"{label}_trades.csv", index=False)
        res["orders"].to_csv(out / f"{label}_orders.csv", index=False)
    sc = runner.screen
    summary = {"fold": fold, "period": pname, "date_guard": per.guard, "variants": len(cfgs),
               "plateau_pick": pick, "plateau_note": note, "top_by_ir": by_ir,
               "pick_config": asdict(cfgs[pick - 1]),
               "events_total": int(len(ev)), "events_by_type": ev["type"].value_counts().to_dict(),
               "events_passing_screen": int(ev["pass_screen"].sum()),
               "events_passing_screen_by_type": ev.loc[ev["pass_screen"], "type"].value_counts().to_dict(),
               "screen_weeks": int(sc["week_end"].nunique()),
               "screen_pass_per_week_mean": float(sc.groupby("week_end")["pass_all"].sum().mean()),
               "screen_pass_share": {k: float(sc[k].mean()) for k in ("pass_C", "pass_A", "pass_L", "pass_G", "pass_all")},
               "eps_coverage_rows": float(sc["q_end"].notna().mean()),
               "variants_passing": int(grid["crit_pass"].sum()),
               "terminal_events": per.win.terminal_events["status"].value_counts().to_dict()
               if len(per.win.terminal_events) else {}}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps({k: summary[k] for k in ("plateau_pick", "plateau_note", "top_by_ir", "events_total",
                                               "events_passing_screen", "screen_pass_per_week_mean",
                                               "variants_passing")}, default=str))
    return summary


def write_frozen(fold: str, variant: int) -> None:
    cfg = calibration_grid()[variant - 1]
    p = OUT / f"frozen_{fold}.json"
    p.write_text(json.dumps({"fold": fold, "variant": variant, "config": asdict(cfg), "config_name": cfg.name,
                             "frozen_at": pd.Timestamp.now("UTC").isoformat(),
                             "ledger_freeze_sha256": freeze_sha256(LEDGER, f"FREEZE-{fold.upper()}")},
                            indent=2))
    print("frozen", fold, cfg.name)


def frozen_config(fold: str) -> tuple[Config, dict]:
    p = (OUT_V1 if dv.IS_V2 else OUT) / f"frozen_{fold}.json"   # v2 reads the v1 frozen rule (never versioned)
    if not p.is_file():
        raise SystemExit(f"no frozen rule at {p}: freeze the rule in the ledger first")
    fr = json.loads(p.read_text())
    cfg = Config(**fr["config"])
    if cfg.name != fr["config_name"]:
        raise SystemExit("frozen rule file is inconsistent")
    if freeze_sha256(LEDGER, f"FREEZE-{fold.upper()}") != fr["ledger_freeze_sha256"]:
        raise SystemExit("the ledger freeze block changed after the rule was frozen")
    return cfg, fr


def run_test(fold: str) -> dict:
    cfg, fr = frozen_config(fold)
    pname = FOLDS[fold]["test"]
    out = OUT / f"{fold}_test_{pname}"
    if (out / "result.json").exists():
        raise SystemExit(f"{fold} has already been tested once ({out / 'result.json'}); a one-shot test is not re-run")
    out.mkdir(parents=True, exist_ok=True)
    per = load_period(pname)
    print_guard(per)
    runner = Runner(per)
    m, res, oneq, qqq = runner.run(cfg)
    m2, *_ = runner.run(replace(cfg, spread_mult=2.0))
    stress = Runner(load_period(pname, terminal_awaiting=TERMINAL_D5_STRESS))
    ms, *_ = stress.run(cfg)
    res["nav"].to_frame("nav").assign(oneq=oneq, qqq=qqq, exposure=res["exposure"]).to_csv(out / "daily_nav.csv")
    res["trades"].to_csv(out / "trades.csv", index=False)
    res["orders"].to_csv(out / "orders.csv", index=False)
    runner.events.to_csv(out / "breakout_events.csv", index=False)
    m["_monthly_active"].to_csv(out / "monthly_active_vs_oneq.csv", header=["active"])
    result = {"fold": fold, "period": pname, "config": cfg.name, "frozen_at": fr["frozen_at"], "date_guard": per.guard,
              "metrics": _clean(m), "excess_spread2x": m2["excess_cagr"], "pass_spread2x": m2["crit_pass"],
              "terminal_minus100_excess": ms["excess_cagr"],
              "criteria": {"A_excess_and_t": bool(m["crit_A"]), "B_drawdown": bool(m["crit_B"]),
                           "pass": bool(m["crit_pass"])},
              "events_passing_screen": int(runner.events["pass_screen"].sum()),
              "open_positions_at_end": res["open"]}
    (out / "result.json").write_text(json.dumps(result, indent=2, default=str))
    print(f"{fold} test on {pname}: CAGR {m['cagr']:+.1%} ONEQ {m['bench_cagr']:+.1%} QQQ {m['qqq_cagr']:+.1%} "
          f"t_m {m['t_excess_monthly']:.2f} DD {m['max_dd']:.0%} (ONEQ {m['bench_max_dd']:.0%}) "
          f"buys {m['n_buy']} -> pass {m['crit_pass']}")
    return result


# ======================================================================== context: IBD 50 ETF

def ffty_context() -> dict:
    out = {}
    lv_ = {}
    for t in ("FFTY", "QQQ", "ONEQ"):
        j = json.loads((RAW / f"chart_{t}.json").read_text())["chart"]["result"][0]
        ts = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("America/New_York").strftime("%Y-%m-%d")
        adj = pd.Series(j["indicators"]["adjclose"][0]["adjclose"], index=pd.DatetimeIndex(ts)).dropna()
        lv_[t] = adj[~adj.index.duplicated(keep="last")]
    start = lv_["FFTY"].index[0]
    end = min(s.index[-1] for s in lv_.values())
    for t, s in lv_.items():
        s = s[(s.index >= start) & (s.index <= end)]
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        r = s.pct_change().dropna()
        by = s.groupby(s.index.year).last()
        prev = by.shift(1)
        prev.iloc[0] = s.iloc[0]
        out[t] = {"start": str(s.index[0].date()), "end": str(s.index[-1].date()),
                  "total_return": float(s.iloc[-1] / s.iloc[0] - 1), "cagr": float((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1),
                  "max_dd": max_drawdown_of_returns(r), "by_year": {int(y): round(float(v), 4) for y, v in (by / prev - 1).items()}}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ffty_context.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({t: {k: v for k, v in d.items() if k != "by_year"} for t, d in out.items()}, indent=2))
    return out


EXAMPLES = [("NFLX", "2014-07-01"), ("AMZN", "2016-04-29"), ("INTC", "2016-09-16"), ("GOOGL", "2020-07-02"),
            ("FB", "2021-04-29"), ("AMD", "2021-07-28"), ("AVGO", "2023-03-03"), ("COST", "2025-05-30")]


def cup_examples() -> pd.DataFrame:
    """All detected cup-with-handle breakouts of both periods (from the saved event files); well-known names flagged."""
    parts = [pd.read_csv(OUT / f) for f in ("fold1_calibrate_A/breakout_events.csv", "fold1_test_B/breakout_events.csv")]
    e = pd.concat(parts, ignore_index=True)
    c = e[e["type"] == "cup_handle"].copy()
    c["example"] = [(t, s[:10]) in EXAMPLES for t, s in zip(c["ticker"], c["session"].astype(str))]
    c["close_vs_pivot"] = c["close"] / c["pivot"] - 1
    c.to_csv(OUT / "cup_handle_detections.csv", index=False)
    summary = {"cup_handle_detected": int(len(c)), "passing_screen": int(c["pass_screen"].sum()),
               "breakouts_by_type": e["type"].value_counts().to_dict(),
               "passing_screen_by_type": e.loc[e["pass_screen"], "type"].value_counts().to_dict()}
    (OUT / "pattern_summary.json").write_text(json.dumps(summary, indent=2))
    print(c[c["example"]][["session", "ticker", "left_date", "bottom_date", "right_date", "depth", "handle_depth",
                           "handle_days", "base_weeks", "pivot", "close_vs_pivot", "pass_screen"]].to_string())
    print(summary)
    return c


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--examples", action="store_true")
    p.add_argument("--calibrate", choices=list(FOLDS))
    p.add_argument("--freeze", choices=list(FOLDS))
    p.add_argument("--variant", type=int)
    p.add_argument("--test", choices=list(FOLDS))
    p.add_argument("--ffty", action="store_true")
    a = p.parse_args(argv)
    if a.calibrate:
        run_calibration(a.calibrate)
    elif a.freeze:
        if not a.variant:
            raise SystemExit("--variant is required")
        write_frozen(a.freeze, a.variant)
    elif a.test:
        run_test(a.test)
    elif a.ffty:
        ffty_context()
    elif a.examples:
        cup_examples()
    else:
        p.print_help()


if __name__ == "__main__":
    main()
