"""Regime-switching allocation across long-only ETFs (pre-registered in docs/research_ledger_regime.md, section 0).

Instead of in / out timing, each rule switches WHAT it holds according to a textbook market-regime signal:

- R1  trend:        QQQ > SMA200(QQQ) -> QQQ, else IEF           (R1b: else USMV)
- R2  dual momentum: month end, QQQ 12m return > T-bill 12m -> QQQ, else IEF
                     (R2b: the stronger of QQQ / SPY over 12m if it beats T-bills, else IEF)
- R3  volatility:   QQQ 20d realised vol < its own 252d median -> QQQ, else USMV  (R3b: else 50% QQQ + 50% IEF)
- R4  factor:       SPY > SMA200(SPY) -> MTUM, else USMV
- R5  risk parity lite: month end, weights proportional to 1 / (63d vol) across QQQ, IEF, GLD

Execution: the target decided at the close of t (data up to and including t) is traded at the close of t+1, so the
new weights earn from t+2. Trades happen only when the target changes; weights drift in between.
Benchmark: ONEQ buy-and-hold (total return); QQQ buy-and-hold is reported. Costs: IBKR Pro Tiered, $10,000 start.

Every vendor frame is truncated at ``END`` (latest complete month end) immediately after parsing and asserted
(reusing the guards of scripts/research_qqq_timing.py). Raw data local only: research_cache/regime/raw/.
Outputs: output/research_only/regime/ (returns and metrics only, no vendor price levels).
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.research_qqq_timing import (
    COMMISSION_MAX_FRAC, COMMISSION_MIN, COMMISSION_PER_SHARE, FEES_PER_SHARE, SELL_REG_FRAC, START_EQUITY,
    TRADING_DAYS, assert_dev_dates, bonferroni_t, cagr_of, fill_rf, load_dtb3, load_kf_rf, max_drawdown, monthly,
    parse_chart, yearly,
)

ROOT = Path(__file__).resolve().parents[1]
END = "2026-09-30"
RAW = Path("/Users/bytedance/code/quant_stocks/research_cache/regime/raw")
PRIOR_QQQ = Path("/Users/bytedance/code/quant_stocks/research_cache/qqq_timing/raw/chart_QQQ.json")
OUT = ROOT / "output/research_only/regime"

TICKERS = ("QQQ", "ONEQ", "SPY", "IEF", "TLT", "SHY", "BIL", "GLD", "MTUM", "USMV", "QUAL", "VLUE")
HALF_SPREAD = {"QQQ": 1e-4, "SPY": 1e-4, "IEF": 1e-4, "GLD": 1e-4, "ONEQ": 2e-4, "MTUM": 2e-4, "USMV": 2e-4}
BENCH = "ONEQ"
SMA_LEN = 200
MOM_LEN = 252
VOL_LEN = 20
VOL_MED_LEN = 252
RP_LEN = 63


# ======================================================================== data

@dataclass
class Data:
    sessions: pd.DatetimeIndex
    adj: pd.DataFrame        # total-return (adjusted) closes, NaN before listing
    close: pd.DataFrame      # split-adjusted closes, for share counts in the cost model
    rets: pd.DataFrame       # daily total returns, NaN on/before the first close
    rf: pd.Series            # daily T-bill return on the sessions
    raw: dict
    guard: dict


def load_data(end: str = END, raw_dir: Path = RAW) -> Data:
    raw = {t: parse_chart(raw_dir / f"chart_{t}.json", end) for t in TICKERS}
    sessions = pd.DatetimeIndex(raw["QQQ"]["date"])       # QQQ (1999-03-10 ..) defines the trading calendar
    adj, close = {}, {}
    for t, df in raw.items():
        df = df[pd.to_datetime(df["date"]) >= sessions[0]]      # SPY's 1993-1999 rows are not needed
        ix = pd.DatetimeIndex(df["date"])
        extra = ix.difference(sessions)
        if len(extra):
            raise ValueError(f"{t} has sessions not in the QQQ calendar: {list(extra[:5])}")
        adj[t] = pd.Series(df["adjclose"].values, index=ix).reindex(sessions)
        close[t] = pd.Series(df["close"].values, index=ix).reindex(sessions)
    adj, close = pd.DataFrame(adj), pd.DataFrame(close)
    rets = adj.pct_change(fill_method=None)
    rf = fill_rf(load_kf_rf(end), load_dtb3(end), sessions)
    guard = {"end": end}
    for t, df in raw.items():
        assert_dev_dates(df["date"], end)
        guard[t] = {"first": df["date"].iloc[0], "last": df["date"].iloc[-1], "rows": int(len(df))}
    assert_dev_dates(sessions, end)
    return Data(sessions=sessions, adj=adj, close=close, rets=rets, rf=rf, raw=raw, guard=guard)


def data_checks(d: Data) -> dict:
    out = {}
    for t, df in d.raw.items():
        a = df["adjclose"].pct_change()
        c = (df["close"] + df["dividend"]) / df["close"].shift(1) - 1
        diff = (a - c).dropna()
        first = pd.Timestamp(df["date"].iloc[0])
        listed = d.sessions[d.sessions >= first]
        out[t] = {"first": df["date"].iloc[0], "missing_sessions_after_listing": int(d.adj[t].reindex(listed).isna().sum()),
                  "max_abs_adj_vs_close_div": float(diff.abs().max()), "dividends": int((df["dividend"] > 0).sum()),
                  "max_abs_daily_return": float(a.abs().max())}
    bil = d.rets["BIL"].dropna()
    rf = d.rf.reindex(bil.index)
    out["BIL_vs_KF_rf_annualised"] = {"bil": cagr_of(bil), "kf_rf": cagr_of(rf), "window": [str(bil.index[0].date()),
                                                                                          str(bil.index[-1].date())]}
    if PRIOR_QQQ.exists():
        p = parse_chart(PRIOR_QQQ, END)
        pr = pd.Series(p["adjclose"].values, index=pd.DatetimeIndex(p["date"])).pct_change()
        j = pd.concat([pr, d.rets["QQQ"]], axis=1, join="inner").dropna()
        out["QQQ_vs_prior_cache"] = {"sessions": int(len(j)), "max_abs_daily_diff": float((j.iloc[:, 0] - j.iloc[:, 1]).abs().max())}
    return out


# ======================================================================== signals (data up to and including t)

def month_end_mask(idx: pd.DatetimeIndex) -> np.ndarray:
    """True on the last session of each month (uses only the calendar date of the next session).

    The last session in the data is never treated as a month end (the next session is unknown)."""
    per = idx.to_period("M")
    m = np.zeros(len(idx), dtype=bool)
    m[:-1] = per[:-1] != per[1:]
    return m


def sma_state(p: pd.Series, length: int = SMA_LEN) -> pd.Series:
    """1.0 if P_t > SMA(length)_t, 0.0 if not, NaN before the average exists (or where P is missing)."""
    ma = p.rolling(length, min_periods=length).mean()
    s = (p > ma).astype(float)
    return s.where(ma.notna() & p.notna())


def trailing_return(p: pd.Series, n: int = MOM_LEN) -> pd.Series:
    return p / p.shift(n) - 1


def trailing_rf(rf: pd.Series, n: int = MOM_LEN) -> pd.Series:
    """Compounded T-bill return over the last n sessions ending at t (returns t-n+1 .. t)."""
    return np.exp(np.log1p(rf).rolling(n, min_periods=n).sum()) - 1


def vol_state(r: pd.Series, window: int = VOL_LEN, med_len: int = VOL_MED_LEN) -> pd.Series:
    """1.0 = calm (20d vol below its own trailing 252d median, both including t), 0.0 = turbulent."""
    v = r.rolling(window, min_periods=window).std() * math.sqrt(TRADING_DAYS)
    med = v.rolling(med_len, min_periods=med_len).median()
    s = (v < med).astype(float)
    return s.where(med.notna())


def inverse_vol_weights(rets: pd.DataFrame, length: int = RP_LEN) -> pd.DataFrame:
    sig = rets.rolling(length, min_periods=length).std()
    inv = 1.0 / sig
    w = inv.div(inv.sum(axis=1), axis=0)
    return w.where(sig.notna().all(axis=1), np.nan)


def two_state(state: pd.Series, on: dict, off: dict, columns) -> pd.DataFrame:
    w = pd.DataFrame(np.nan, index=state.index, columns=list(columns))
    for k in columns:
        w.loc[state == 1.0, k] = on.get(k, 0.0)
        w.loc[state == 0.0, k] = off.get(k, 0.0)
    return w


def monthly_only(w: pd.DataFrame) -> pd.DataFrame:
    """Keep the month-end decisions and carry them until the next month end."""
    me = month_end_mask(w.index)
    keep = np.repeat(me[:, None], w.shape[1], axis=1)
    return w.where(keep, np.nan).ffill()


@dataclass(frozen=True)
class RuleSpec:
    name: str
    assets: tuple          # every ETF the rule can hold
    monthly: bool
    description: str


RULES = (
    RuleSpec("R1", ("QQQ", "IEF"), False, "QQQ > SMA200 -> QQQ, else IEF"),
    RuleSpec("R1b", ("QQQ", "USMV"), False, "QQQ > SMA200 -> QQQ, else USMV"),
    RuleSpec("R2", ("QQQ", "IEF"), True, "month end: QQQ 12m > T-bill 12m -> QQQ, else IEF"),
    RuleSpec("R2b", ("QQQ", "SPY", "IEF"), True, "month end: stronger of QQQ/SPY 12m, if > T-bill -> it, else IEF"),
    RuleSpec("R3", ("QQQ", "USMV"), False, "QQQ 20d vol < 252d median -> QQQ, else USMV"),
    RuleSpec("R3b", ("QQQ", "IEF"), False, "QQQ 20d vol < 252d median -> QQQ, else 50% QQQ + 50% IEF"),
    RuleSpec("R4", ("MTUM", "USMV"), False, "SPY > SMA200 -> MTUM, else USMV"),
    RuleSpec("R5", ("QQQ", "IEF", "GLD"), True, "month end: inverse 63d-vol weights across QQQ, IEF, GLD"),
)
SPEC = {r.name: r for r in RULES}


def target_weights(name: str, d: Data) -> pd.DataFrame:
    """Target weights decided at each close t (NaN rows = no decision possible yet)."""
    a = d.adj
    cols = SPEC[name].assets
    if name in ("R1", "R1b"):
        alt = "IEF" if name == "R1" else "USMV"
        return two_state(sma_state(a["QQQ"]), {"QQQ": 1.0}, {alt: 1.0}, cols)
    if name == "R2":
        q, tb = trailing_return(a["QQQ"]), trailing_rf(d.rf)
        st = (q > tb).astype(float).where(q.notna() & tb.notna())
        return monthly_only(two_state(st, {"QQQ": 1.0}, {"IEF": 1.0}, cols))
    if name == "R2b":
        q, s, tb = trailing_return(a["QQQ"]), trailing_return(a["SPY"]), trailing_rf(d.rf)
        ok = q.notna() & s.notna() & tb.notna()
        w = pd.DataFrame(np.nan, index=a.index, columns=list(cols))
        best_q = q >= s
        best_ret = q.where(best_q, s)
        w.loc[ok, :] = 0.0
        w.loc[ok & best_q & (best_ret > tb), "QQQ"] = 1.0
        w.loc[ok & ~best_q & (best_ret > tb), "SPY"] = 1.0
        w.loc[ok & ~(best_ret > tb), "IEF"] = 1.0
        return monthly_only(w)
    if name in ("R3", "R3b"):
        st = vol_state(d.rets["QQQ"])
        off = {"USMV": 1.0} if name == "R3" else {"QQQ": 0.5, "IEF": 0.5}
        return two_state(st, {"QQQ": 1.0}, off, cols)
    if name == "R4":
        return two_state(sma_state(a["SPY"]), {"MTUM": 1.0}, {"USMV": 1.0}, cols)
    if name == "R5":
        return monthly_only(inverse_vol_weights(d.rets[["QQQ", "IEF", "GLD"]]))
    raise KeyError(name)


def entry_index(w: pd.DataFrame, d: Data, assets, monthly_rule: bool) -> int:
    """First session S where every used ETF and the benchmark have a close, the target decided at S-1 exists,
    and (monthly rules) S-1 is a month end."""
    have = d.adj[list(assets) + [BENCH]].notna().all(axis=1).values
    defined = w.notna().all(axis=1).values
    me = month_end_mask(w.index)
    for i in range(1, len(w)):
        if have[i] and defined[i - 1] and (me[i - 1] or not monthly_rule):
            return i
    raise ValueError("no entry possible")


# ======================================================================== simulation

def order_cost(value: float, price: float, sell: bool, half_spread: float) -> float:
    if value <= 0:
        return 0.0
    shares = value / price
    comm = min(max(COMMISSION_MIN, COMMISSION_PER_SHARE * shares), COMMISSION_MAX_FRAC * value)
    return comm + FEES_PER_SHARE * shares + half_spread * value + (SELL_REG_FRAC * value if sell else 0.0)


def simulate(w: pd.DataFrame, rets: pd.DataFrame, close: pd.DataFrame, start_i: int, end_i: int | None = None,
             lag: int = 1, costs: bool = True) -> dict:
    """Portfolio that starts as $10,000 cash at the close of session ``start_i`` and trades there to the target
    decided ``lag`` sessions earlier; afterwards it trades at the close of i whenever the target decided at i-lag
    differs from the last executed target. Holdings drift with returns between trades."""
    cols = list(w.columns)
    W = w.values
    R = rets[cols].values
    P = close[cols].values
    end_i = len(w) - 1 if end_i is None else end_i
    h = np.zeros(len(cols))
    cash = START_EQUITY
    last = None
    values, held, trades, orders, cost_frac = [], [], 0, 0, 0.0
    for i in range(start_i, end_i + 1):
        if i > start_i:
            r = R[i]
            if np.any(np.isnan(r[h > 0])):
                raise ValueError(f"missing return for a held asset on {w.index[i].date()}")
            h = h * (1 + np.nan_to_num(r))
        total = h.sum() + cash
        tgt = W[i - lag]
        if np.any(np.isnan(tgt)):
            raise ValueError(f"no target on {w.index[i - lag].date()}")
        if last is None or not np.allclose(tgt, last, atol=1e-12, rtol=0):
            new = tgt * total
            delta = new - h
            c = 0.0
            for k in range(len(cols)):
                if abs(delta[k]) > 1.0:
                    if costs:
                        c += order_cost(abs(delta[k]), P[i, k], delta[k] < 0, HALF_SPREAD[cols[k]])
                    orders += 1
            if last is not None:
                trades += 1
            h = new * (1 - c / total)
            cash = 0.0
            cost_frac += c / total
            last = tgt.copy()
        values.append(h.sum() + cash)
        held.append(h / h.sum() if h.sum() > 0 else h)
    idx = w.index[start_i:end_i + 1]
    v = pd.Series(values, index=idx)
    hw = pd.DataFrame(held, index=idx, columns=cols).shift(1).iloc[1:]    # weights earning each day's return
    return {"value": v, "ret": v.pct_change().iloc[1:], "held": hw, "trades": trades, "orders": orders,
            "cost_frac": cost_frac}


def buy_hold(asset: str, d: Data, start_i: int) -> dict:
    w = pd.DataFrame({asset: 1.0}, index=d.sessions)
    return simulate(w, d.rets, d.close, start_i)


# ======================================================================== metrics and criteria

def period_metrics(r: pd.Series, b: pd.Series, q: pd.Series, rf: pd.Series) -> dict:
    v = (1 + r).cumprod()
    vb, vq = (1 + b).cumprod(), (1 + q).cumprod()
    v0 = pd.concat([pd.Series([1.0]), v.reset_index(drop=True)])
    vb0 = pd.concat([pd.Series([1.0]), vb.reset_index(drop=True)])
    vq0 = pd.concat([pd.Series([1.0]), vq.reset_index(drop=True)])
    ex = r - rf.reindex(r.index)
    mx = monthly(r) - monthly(b)
    cagr, bc, qc = cagr_of(r), cagr_of(b), cagr_of(q)
    dd, bdd, qdd = max_drawdown(v0), max_drawdown(vb0), max_drawdown(vq0)
    return {"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "sessions": int(len(r)),
            "cagr": cagr, "oneq_cagr": bc, "qqq_cagr": qc, "cagr_minus_oneq": cagr - bc, "cagr_minus_qqq": cagr - qc,
            "max_dd": dd, "oneq_max_dd": bdd, "qqq_max_dd": qdd,
            "dd_shallower_than_oneq_pp": (abs(bdd) - abs(dd)) * 100, "dd_shallower_than_qqq_pp": (abs(qdd) - abs(dd)) * 100,
            "vol": float(r.std() * math.sqrt(TRADING_DAYS)),
            "sharpe": float(ex.mean() / ex.std() * math.sqrt(TRADING_DAYS)),
            "calmar": cagr / abs(dd) if dd < 0 else float("nan"),
            "months": int(len(mx)), "t_monthly_excess_vs_oneq": float(mx.mean() / mx.std() * math.sqrt(len(mx)))}


def halves(index: pd.DatetimeIndex) -> tuple:
    n = len(index)
    return index[: n // 2], index[n // 2:]


def evaluate(full: dict, h1: dict, h2: dict) -> dict:
    """Pre-registered pass criteria (section 0.6): A or B."""
    parts = (full, h1, h2)
    a = all(p["cagr"] > p["oneq_cagr"] for p in parts) and full["t_monthly_excess_vs_oneq"] >= 2.0
    b = all(p["dd_shallower_than_oneq_pp"] >= 10.0 and p["cagr"] >= p["oneq_cagr"] - 0.03 for p in parts)
    return {"A_higher_cagr_all_three_and_t_ge_2": bool(a), "B_dd_10pp_shallower_and_cagr_within_3pp_all_three": bool(b),
            "pass": bool(a or b)}


# ======================================================================== main

def run(args=None) -> dict:
    d = load_data(END)
    print("DATE GUARD: every frame truncated at", END, "and asserted;", {k: v["last"] for k, v in d.guard.items()
                                                                       if isinstance(v, dict)})
    checks = data_checks(d)
    res = {"guard": d.guard, "data_checks": checks, "n_trials": len(RULES),
           "bonferroni_t_one_sided_5pct": bonferroni_t(len(RULES)), "rules": {}}
    rows, by_year, mon = [], {}, {}
    for spec in RULES:
        w = target_weights(spec.name, d)
        s_i = entry_index(w, d, spec.assets, spec.monthly)
        sim = simulate(w, d.rets, d.close, s_i)
        bq = buy_hold(BENCH, d, s_i)
        qq = buy_hold("QQQ", d, s_i)
        r, b, q = sim["ret"], bq["ret"], qq["ret"]
        assert r.index.equals(b.index) and r.index.equals(q.index)
        assert_dev_dates(r.index, END)
        i1, i2 = halves(r.index)
        full = period_metrics(r, b, q, d.rf)
        h1 = period_metrics(r.loc[i1], b.loc[i1], q.loc[i1], d.rf)
        h2 = period_metrics(r.loc[i2], b.loc[i2], q.loc[i2], d.rf)
        crit = evaluate(full, h1, h2)
        years = len(r) / TRADING_DAYS
        held = sim["held"]
        info = {"description": spec.description, "entry_close": str(d.sessions[s_i].date()),
                "signal_at_entry": {k: float(v) for k, v in w.iloc[s_i - 1].items()},
                "full": full, "half1": h1, "half2": h2, "criteria": crit,
                "trades": sim["trades"], "trades_per_year": sim["trades"] / years, "orders": sim["orders"],
                "cost_drag_per_year": sim["cost_frac"] / years,
                "avg_weight": {k: float(v) for k, v in held.mean().items()},
                "time_mostly_in": {k: float((held[k] > 0.5).mean()) for k in held.columns}}
        # report-only sensitivities: trade at the close of t; no costs
        for tag, kw in [("same_close", dict(lag=0)), ("no_costs", dict(costs=False))]:
            s2 = simulate(w, d.rets, d.close, s_i, **kw)
            info[f"sens_{tag}"] = {"cagr": cagr_of(s2["ret"]), "max_dd": max_drawdown(s2["value"]),
                                   "cagr_minus_oneq": cagr_of(s2["ret"]) - full["oneq_cagr"]}
        res["rules"][spec.name] = info
        for tag, p in [("full", full), ("half1", h1), ("half2", h2)]:
            rows.append({"rule": spec.name, "period": tag, **p, "pass": crit["pass"] if tag == "full" else None})
        by_year[spec.name] = yearly(r)
        mon[spec.name] = monthly(r)
        print(f"{spec.name:4s} {full['start']}..{full['end']} CAGR {full['cagr']:+.2%} (ONEQ {full['oneq_cagr']:+.2%}, "
              f"QQQ {full['qqq_cagr']:+.2%}) MDD {full['max_dd']:.1%} (ONEQ {full['oneq_max_dd']:.1%}) "
              f"t {full['t_monthly_excess_vs_oneq']:+.2f} pass={crit['pass']}")
    s0 = min(entry_index(target_weights(n, d), d, SPEC[n].assets, SPEC[n].monthly) for n in SPEC)
    for a in (BENCH, "QQQ"):
        rr = buy_hold(a, d, s0)["ret"]
        by_year[f"{a}_buyhold"] = yearly(rr)
        mon[f"{a}_buyhold"] = monthly(rr)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(res, indent=2, default=float))
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", float_format="%.6f")
    mk = pd.DataFrame(mon)
    mk.index = mk.index.astype(str)
    mk.to_csv(OUT / "monthly_returns.csv", float_format="%.6f")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
