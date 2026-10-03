"""Dow Theory market timing: primary trend from swing highs / lows of the Dow Jones Industrial (^DJI) and
Transportation (^DJT) averages, with mutual confirmation. Bull = 100% QQQ, bear = 100% T-bill ETF.

Pre-registered in docs/research_ledger_dow_theory.md (section 0) before the first run.

Swing points (each average separately, closes only, data up to the decision close t):
- In an up-leg the highest close H is tracked. H is *confirmed* as a swing high on the first day t on which the
  reaction from H satisfies the family's condition; the leg then turns down, starting from the lowest close after H.
  Down-legs are symmetric. Confirmation is dated t (never back-filled to the day of H).
- Condition: decline from H to the lowest close since H >= pct; family R also requires t - index(H) >= min_days
  sessions and a retracement of at least ``retrace`` (1/3) of the preceding leg, which starts at the latest swing
  low before H (version 2: own pivots or pivots of a pct + min_days tracker without the retracement test; version 1,
  family R1, used only the tracker's own pivots and locked up in long trends -- see the ledger).
Break direction of one average: close above its last confirmed swing high -> up; below its last confirmed swing
low -> down; otherwise unchanged. Family P ("strict pattern") counts an up break only when the last swing low is
higher than the one before (higher low), a down break only after a lower high.
Primary trend: bull when both averages' break directions are up, bear when both are down, otherwise unchanged
(non-confirmation keeps the trend). Before the first signal the state is bull.

Execution, costs, metrics, the 2014-12-31 date guard and the one-shot pass criteria are reused from
scripts/research_qqq_timing.py. Raw data stays local in research_cache/qqq_timing/raw/.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import research_qqq_timing as qt

ROOT = Path(__file__).resolve().parents[1]
DJ_FILES = {"DJI": qt.CACHE / "chart_%5EDJI.json", "DJT": qt.CACHE / "chart_%5EDJT.json"}
SECOND_START = "1993-01-04"        # 1992 is the swing warm-up (Yahoo ^DJI / ^DJT start 1992-01-02)
SECOND_END = qt.SECOND_END
OUT = ROOT / "output/research_only/dow_theory"
FROZEN = "R_p5_D15"                # chosen before the development run (ledger section 0)
ONESHOT_ENTRY_CLOSE = "2014-12-31"
ONESHOT_FIRST_RETURN = "2015-01-02"


# ======================================================================== swing detection

def _ref_point(events: list, kind: str, t: int, before: int, own: tuple) -> float:
    """Most recent pivot of ``kind`` ('L' or 'H') confirmed by t whose extreme lies before index ``before``;
    ``own`` = (extreme index, value) of the tracker's own last pivot of that kind. The later of the two wins."""
    best_i, best_v = own
    for ct, k, ei, v in reversed(events):
        if ct > t or k != kind or ei >= before:
            continue
        if np.isnan(best_v) or ei > best_i:
            best_i, best_v = ei, v
        break
    return best_v


def swing_levels(close, pct: float, min_days: int = 0, retrace: float = 0.0, ref: str = "intermediate") -> pd.DataFrame:
    """Causal swing points. Returns, for every session t, the swing levels known at the close of t:
    last_high, prev_high, last_low, prev_low (confirmed points) and a 'confirm' column ('H', 'L' or '').

    With ``retrace`` > 0 a reaction must also retrace that fraction of the preceding leg. ``ref`` sets where the
    preceding leg starts: "intermediate" (version 2, the frozen definition) = the latest pivot before the current
    extreme among this tracker's own pivots and the pivots of a pct + min_days tracker without the retracement test;
    "leg" (version 1, kept for the record) = this tracker's own last pivot only, which can lock up in long trends.
    ``df.attrs['events']`` lists (confirm index, kind, extreme index, value)."""
    s = pd.Series(close, dtype=float)
    c = s.values
    n = len(c)
    inter = []
    if retrace > 0 and ref == "intermediate":
        inter = swing_levels(c, pct, min_days, 0.0).attrs["events"]
    out = np.full((n, 4), np.nan)
    conf = np.array([""] * n, dtype=object)
    events = []
    last_h = prev_h = last_l = prev_l = np.nan
    i_last_h = i_last_l = -1
    mode = 0                         # 0 = no swing yet, +1 = up-leg (tracking a high), -1 = down-leg
    hi = lo = c[0] if n else np.nan
    ihi = ilo = 0
    for t in range(n):
        x = c[t]
        if mode == 0:
            if x > hi:
                hi, ihi = x, t
            if x < lo:
                lo, ilo = x, t
            if (hi - x) / hi >= pct and t - ihi >= min_days:
                last_h, i_last_h, mode = hi, ihi, -1
                events.append((t, "H", ihi, hi))
                seg = c[ihi:t + 1]
                ilo = ihi + int(np.argmin(seg))
                lo = c[ilo]
                conf[t] = "H"
            elif (x - lo) / lo >= pct and t - ilo >= min_days:
                last_l, i_last_l, mode = lo, ilo, +1
                events.append((t, "L", ilo, lo))
                seg = c[ilo:t + 1]
                ihi = ilo + int(np.argmax(seg))
                hi = c[ihi]
                conf[t] = "L"
        elif mode == +1:
            if x > hi:
                hi, ihi = x, t
            seg = c[ihi:t + 1]
            j = int(np.argmin(seg))
            rmin = seg[j]
            ok = (hi - rmin) / hi >= pct and t - ihi >= min_days
            if ok and retrace > 0:
                base = _ref_point(inter, "L", t, ihi, (i_last_l, last_l))
                ok = np.isnan(base) or (hi - rmin) >= retrace * (hi - base)
            if ok:
                prev_h, last_h, i_last_h = last_h, hi, ihi
                events.append((t, "H", ihi, hi))
                mode, lo, ilo = -1, rmin, ihi + j
                conf[t] = "H"
        else:
            if x < lo:
                lo, ilo = x, t
            seg = c[ilo:t + 1]
            j = int(np.argmax(seg))
            rmax = seg[j]
            ok = (rmax - lo) / lo >= pct and t - ilo >= min_days
            if ok and retrace > 0:
                base = _ref_point(inter, "H", t, ilo, (i_last_h, last_h))
                ok = np.isnan(base) or (rmax - lo) >= retrace * (base - lo)
            if ok:
                prev_l, last_l, i_last_l = last_l, lo, ilo
                events.append((t, "L", ilo, lo))
                mode, hi, ihi = +1, rmax, ilo + j
                conf[t] = "L"
        out[t] = (last_h, prev_h, last_l, prev_l)
    df = pd.DataFrame(out, index=s.index, columns=["last_high", "prev_high", "last_low", "prev_low"])
    df["confirm"] = conf
    df.attrs["events"] = events
    return df


def break_direction(close, levels: pd.DataFrame, strict: bool = False) -> pd.Series:
    """+1 after a close above the last confirmed swing high, -1 after a close below the last swing low, else
    the previous value (0 before any break)."""
    c = pd.Series(close, dtype=float).values
    lh, ph, ll, pl = (levels[k].values for k in ("last_high", "prev_high", "last_low", "prev_low"))
    d, out = 0, np.zeros(len(c), dtype=int)
    for t in range(len(c)):
        up = not np.isnan(lh[t]) and c[t] > lh[t] and (not strict or ll[t] > pl[t])
        dn = not np.isnan(ll[t]) and c[t] < ll[t] and (not strict or lh[t] < ph[t])
        if up:
            d = 1
        elif dn:
            d = -1
        out[t] = d
    return pd.Series(out, index=levels.index)


def confirm_trend(dirs: list[pd.Series], initial: int = 1) -> pd.Series:
    """Primary trend: +1 when every average's direction is +1, -1 when every one is -1, else unchanged."""
    m = pd.concat(dirs, axis=1).values
    s, out = initial, np.zeros(len(m), dtype=int)
    for t in range(len(m)):
        if (m[t] == 1).all():
            s = 1
        elif (m[t] == -1).all():
            s = -1
        out[t] = s
    return pd.Series(out, index=dirs[0].index)


# ======================================================================== rules

@dataclass(frozen=True)
class DowRule:
    family: str                       # Z, R (version 2), R1 (version 1, first run), P, N
    pct: float
    min_days: int = 0
    retrace: float = 0.0
    averages: tuple = ("DJI", "DJT")

    @property
    def strict(self) -> bool:
        return self.family == "P"

    @property
    def name(self) -> str:
        base = f"{self.family}_p{int(round(self.pct * 100))}"
        if self.family in ("R", "R1"):
            base += f"_D{self.min_days}"
        if self.family == "N":
            base += "_" + "".join(self.averages)
        return base


def rule_grid() -> list[DowRule]:
    rules = [DowRule("Z", p) for p in (0.03, 0.05, 0.08)]
    rules += [DowRule("R", p, 15, 1 / 3) for p in (0.03, 0.05, 0.08)]
    rules += [DowRule("R", 0.05, d, 1 / 3) for d in (10, 20)]
    rules += [DowRule("P", p) for p in (0.03, 0.05, 0.08)]
    # version 1 of family R (first run, definition error logged in the ledger); counted as trials
    rules += [DowRule("R1", p, 15, 1 / 3) for p in (0.03, 0.05, 0.08)]
    rules += [DowRule("R1", 0.05, d, 1 / 3) for d in (10, 20)]
    rules += [DowRule("N", 0.05, averages=("DJI",)), DowRule("N", 0.05, averages=("DJT",))]
    return rules


def dow_state(rule: DowRule, closes: pd.DataFrame) -> pd.Series:
    """+1 bull / -1 bear at each close of ``closes`` (columns DJI, DJT), using data up to that close only."""
    dirs = []
    for a in rule.averages:
        c = closes[a].dropna()
        lv = swing_levels(c, rule.pct, rule.min_days, rule.retrace, "leg" if rule.family == "R1" else "intermediate")
        dirs.append(break_direction(c, lv, rule.strict).reindex(closes.index).ffill().fillna(0).astype(int))
    if len(dirs) == 1:
        return dirs[0].replace(0, 1)
    return confirm_trend(dirs)


def target_on_sessions(state: pd.Series, sessions: pd.DatetimeIndex) -> pd.Series:
    """Exposure (1 bull, 0 bear) decided at each trading session's close; Dow dates carried forward only."""
    e = (state > 0).astype(float)
    full = e.reindex(e.index.union(sessions)).ffill()
    return full.reindex(sessions).fillna(1.0)


# ======================================================================== data

def load_dow(end: str = qt.DEV_END) -> tuple[pd.DataFrame, dict]:
    cols, guard = {}, {}
    for k, path in DJ_FILES.items():
        df = qt.parse_chart(path, end)          # truncated at ``end`` immediately and asserted
        s = pd.Series(df["close"].values, index=pd.DatetimeIndex(df["date"]))
        qt.assert_dev_dates(s.index, end)
        cols[k] = s
        guard[k] = {"first": str(s.index.min().date()), "last": str(s.index.max().date()), "rows": int(len(s))}
    return pd.DataFrame(cols), guard


def dow_checks(closes: pd.DataFrame, sessions: pd.DatetimeIndex, start: str, end: str) -> dict:
    out = {}
    for k in closes:
        s = closes[k].dropna()
        r = s.pct_change().dropna()
        gaps = pd.Series(s.index[1:] - s.index[:-1]).dt.days
        py = s.groupby(s.index.year).size()
        out[k] = {"rows": int(len(s)), "sessions_per_year_min": int(py.iloc[:-1].min()) if len(py) > 1 else int(py.min()),
                  "max_calendar_gap_days": int(gaps.max()),
                  "abs_daily_moves_over_7pct": [f"{d.date()} {v:+.1%}" for d, v in r[r.abs() > 0.07].items()],
                  "zero_return_days": int((r == 0).sum())}
    tr = sessions[(sessions >= pd.Timestamp(start)) & (sessions <= pd.Timestamp(end))]
    out["trading_sessions_missing_in_dow"] = int(len(tr.difference(closes.dropna().index)))
    out["dow_dates_not_trading_sessions"] = int(len(closes.loc[start:end].dropna().index.difference(tr)))
    return out


# ======================================================================== runs

def signal_list(state: pd.Series, start: str, end: str) -> list[str]:
    s = state.loc[:end]
    ch = s[s != s.shift(1)].iloc[1:]
    ch = ch.loc[start:]
    return [f"{d.date()} {'bull' if v > 0 else 'bear'}" for d, v in ch.items()]


def run_dev(args) -> dict:
    data = qt.load_dev_data(qt.DEV_END)
    closes, g = load_dow(qt.DEV_END)
    guard = {**{k: v["last"] for k, v in data.guard.items() if isinstance(v, dict)}, **{k: v["last"] for k, v in g.items()}}
    print("DATE GUARD: every frame truncated at", qt.DEV_END, "and asserted;", guard)
    checks = dow_checks(closes, data.sessions, "1992-01-02", qt.DEV_END)
    r1 = data.r1
    rc_etf = data.rf - qt.CASH_ETF_FEE / qt.TRADING_DAYS
    rc_zero = pd.Series(0.0, index=data.sessions)
    r2 = pd.Series(0.0, index=data.sessions)      # no 2x sleeve in this study
    periods = {"dev": (qt.DEV_START, qt.DEV_END), "second": (SECOND_START, SECOND_END)}
    const1 = pd.Series(1.0, index=data.sessions)
    bench = {k: qt.simulate(const1, r1, r2, rc_etf, data.price, *v) for k, v in periods.items()}
    bench_cash0 = qt.simulate(const1, r1, r2, rc_zero, data.price, *periods["dev"])
    m_q = qt.metrics(bench["dev"], bench["dev"], data.rf)
    m_q2 = qt.metrics(bench["second"], bench["second"], data.rf)

    rows, yearly_tab, monthly_keep, signals = [], {"QQQ_buyhold": qt.yearly(bench["dev"]["ret"])}, {}, {}
    rules = rule_grid()
    assert len(rules) == 18, len(rules)
    for rule in rules:
        state = dow_state(rule, closes)
        qt.assert_dev_dates(state.index)
        tgt = target_on_sessions(state, data.sessions)
        row = {"config": rule.name, "family": rule.family, "pct": rule.pct, "min_days": rule.min_days,
               "retrace": rule.retrace, "averages": "+".join(rule.averages)}
        sim = qt.simulate(tgt, r1, r2, rc_etf, data.price, *periods["dev"])
        qt.assert_dev_dates(sim["ret"].index)
        m = qt.metrics(sim, bench["dev"], data.rf)
        row.update(m)
        crit = qt.evaluate_criteria(m, m_q)
        row.update({f"dev_{k}": v for k, v in crit.items()})
        sim2 = qt.simulate(tgt, r1, r2, rc_etf, data.price, *periods["second"])
        m2 = qt.metrics(sim2, bench["second"], data.rf)
        for k in ("cagr", "excess_cagr_vs_qqq", "max_dd", "calmar", "switches_per_year", "time_cash"):
            row[f"second_{k}"] = m2[k]
        s0 = qt.simulate(tgt, r1, r2, rc_etf, data.price, *periods["dev"], lag=0)
        row["sameday_excess"] = qt.metrics(s0, bench["dev"], data.rf)["excess_cagr_vs_qqq"]
        sc = qt.simulate(tgt, r1, r2, rc_zero, data.price, *periods["dev"], cash_is_etf=False)
        row["cash0_excess"] = qt.metrics(sc, bench_cash0, data.rf)["excess_cagr_vs_qqq"]
        rows.append(row)
        yearly_tab[rule.name] = qt.yearly(sim["ret"])
        monthly_keep[rule.name] = qt.monthly(sim["ret"])
        signals[rule.name] = signal_list(state, SECOND_START, qt.DEV_END)
    grid = pd.DataFrame(rows)
    n = len(grid)
    hurdle = qt.bonferroni_t(n)
    grid["dsr"] = [qt.deflated_sharpe(r.ir_monthly, int(r.months), r.skew_mx, r.kurt_mx, grid["ir_monthly"].values)["dsr"]
                   for r in grid.itertuples()]
    out = OUT / "dev"
    out.mkdir(parents=True, exist_ok=True)
    grid.to_csv(out / "grid.csv", index=False, float_format="%.6f")
    pd.DataFrame(yearly_tab).to_csv(out / "by_year.csv", float_format="%.6f")
    mk = pd.DataFrame(monthly_keep)
    mk.index = mk.index.astype(str)
    mk.to_csv(out / "monthly_returns.csv", float_format="%.6f")
    summary = {"guard": {**data.guard, "dow": g}, "dow_checks": checks, "n_configs": n,
               "bonferroni_t_one_sided_5pct": hurdle, "QQQ_buyhold_dev": m_q, "NDX_buyhold_second": m_q2,
               "signals_1993_2014": signals, "frozen_pre_registered": FROZEN}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=float))
    print(f"{n} configs, Bonferroni one-sided 5% t = {hurdle:.2f}")
    cols = ["config", "cagr", "excess_cagr_vs_qqq", "max_dd", "calmar", "time_cash", "switches_per_year",
            "t_monthly_excess", "dsr", "second_excess_cagr_vs_qqq", "second_max_dd"]
    print(f"QQQ dev: cagr {m_q['cagr']:.4f} mdd {m_q['max_dd']:.4f} calmar {m_q['calmar']:.3f}; "
          f"NDX second: cagr {m_q2['cagr']:.4f} mdd {m_q2['max_dd']:.4f}")
    print(grid[cols].to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    return summary


def run_oneshot(args) -> dict:
    end_s = args.test_end
    if end_s != "2026-09-30":
        raise ValueError("the pre-registered one-shot window ends 2026-09-30")
    data = qt.load_dev_data(end_s)
    closes, g = load_dow(end_s)
    last = data.sessions.max()
    if last > pd.Timestamp(end_s) or (pd.Timestamp(end_s) - last).days > 4:
        raise ValueError(f"last session {last.date()} does not close the month ending {end_s}")
    print("ONE-SHOT TEST: frames truncated at", end_s, {k: v["last"] for k, v in g.items()},
          "kf_rf last", data.guard["kf_rf"]["last"])
    rule = {r.name: r for r in rule_grid()}[FROZEN]
    r1 = data.r1
    rc = data.rf - qt.CASH_ETF_FEE / qt.TRADING_DAYS
    r2 = pd.Series(0.0, index=data.sessions)
    const1 = pd.Series(1.0, index=data.sessions)
    bench = qt.simulate(const1, r1, r2, rc, data.price, ONESHOT_ENTRY_CLOSE, end_s)
    assert str(bench["ret"].index[0].date()) == ONESHOT_FIRST_RETURN
    state = dow_state(rule, closes)
    tgt = target_on_sessions(state, data.sessions)
    sim = qt.simulate(tgt, r1, r2, rc, data.price, ONESHOT_ENTRY_CLOSE, end_s)
    m_q = qt.metrics(bench, bench, data.rf)
    m = qt.metrics(sim, bench, data.rf)
    e = sim["exposure_held"]
    m["state_at_entry_close"] = float(tgt.loc[ONESHOT_ENTRY_CLOSE])
    m["switches"] = int(sim["rebalances"])
    chg = np.flatnonzero(e.values[1:] != e.values[:-1]) + 1
    m["switch_dates_effective"] = [f"{e.index[i].date()} {'QQQ' if e.values[i] > 0.5 else 'T-bill'}" for i in chg]
    m["signals"] = signal_list(state, "2014-12-01", end_s)
    res = {"rule": FROZEN, "window": [ONESHOT_FIRST_RETURN, str(bench["ret"].index[-1].date())],
           "sessions": int(len(bench["ret"])), "dow_guard": g, "kf_rf_last": data.guard["kf_rf"]["last"],
           "QQQ_buyhold": m_q, FROZEN: m, "criteria": qt.evaluate_criteria(m, m_q)}
    out = OUT / "oneshot"
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(res, indent=2, default=float))
    pd.DataFrame({"QQQ_buyhold": qt.yearly(bench["ret"]), FROZEN: qt.yearly(sim["ret"])}).to_csv(
        out / "by_year.csv", float_format="%.6f")
    mk = pd.DataFrame({"QQQ_buyhold": qt.monthly(bench["ret"]), FROZEN: qt.monthly(sim["ret"])})
    mk.index = mk.index.astype(str)
    mk.to_csv(out / "monthly_returns.csv", float_format="%.6f")
    print(json.dumps({"criteria": res["criteria"],
                      **{k: {x: res[k][x] for x in ("cagr", "max_dd", "calmar", "vol", "sharpe", "time_cash",
                                                    "t_monthly_excess")} for k in ("QQQ_buyhold", FROZEN)}},
                     indent=1, default=float))
    print("switches:", m["switches"], m["switch_dates_effective"])
    print("signals:", m["signals"])
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=["dev", "oneshot"], default="dev")
    ap.add_argument("--test-end", default=None, help="oneshot only: 2026-09-30")
    args = ap.parse_args(argv)
    run_oneshot(args) if args.mode == "oneshot" else run_dev(args)


if __name__ == "__main__":
    main()
