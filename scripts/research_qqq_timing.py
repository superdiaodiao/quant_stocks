"""Development-period research (1999-03 .. 2014-12 only) for QQQ exposure timing: cash / 1x QQQ / 2x (QLD-like).

Rules studied (pre-registered in docs/research_ledger_qqq_timing.md, section 0, before the first run):

- T01 / T02 / T12: trend filter on P = QQQ total-return index (NDX price before 1999-03-10). The state turns "on"
  when P > MA(L) * (1 + b) and "off" when P < MA(L) * (1 - b); inside the band the previous state is kept.
  T01: on = 1x QQQ, off = cash.  T02: on = 2x, off = cash.  T12: on = 2x, off = 1x.
- VT: volatility target, exposure = min(2, target / realised vol over W sessions), traded only when the target
  is at least 0.25 away from the current exposure.
- VTT: VT while the MA(L) trend state (band 2%) is on, cash while it is off.
- M: month-end check (signal looked at only on the last session of each month), T01 / T02, band 0.

Signals use data up to and including the decision close t. Main execution: the close of the next session t+1
(the new position earns returns from session t+2). Variant: the close of t itself (not really doable by hand).

Exposure between 0 and 2 is held as sleeves: e <= 1 -> e in QQQ, 1 - e in cash (a T-bill ETF); e > 1 ->
(2 - e) in QQQ and (e - 1) in the 2x fund.  The 2x fund is synthetic: 2 * r_QQQ - (RF + spread) - 0.95%/yr,
with the spread calibrated on the real QLD 2006-06-22 .. 2014-12-31 only.

HARD RULE: no return, price or outcome dated after 2014-12-31 is used. Every vendor frame is read through
``load_dev_data``, which truncates at ``DEV_END`` immediately after parsing and asserts that no later row survives;
the guard result is printed on every run.

Raw data (local only, never committed): /Users/bytedance/code/quant_stocks/research_cache/qqq_timing/raw/
Outputs: output/research_only/qqq_timing_dev/ (returns and metrics only, no vendor price levels).
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

NORM = NormalDist()
ROOT = Path(__file__).resolve().parents[1]

DEV_END = "2014-12-31"
DEV_START = "1999-03-10"          # QQQ's first close; strategies and the benchmark enter at this close
SECOND_START = "1986-10-01"       # second reference period on NDX price (after a 250-session warm-up)
SECOND_END = "1999-03-09"
QLD_CAL_START = "2006-06-22"      # first QLD close is 2006-06-21; first daily return 2006-06-22

CACHE = Path("/Users/bytedance/code/quant_stocks/research_cache/qqq_timing/raw")
KF_ZIP = Path("/Users/bytedance/code/quant_stocks/research_cache/reversal_2012_2026/raw/kf/"
              "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip")
TIINGO_QQQ = Path("/Users/bytedance/code/quant_stocks/research_cache/holdout_2011_2019/qqq_tiingo_2010_2020.csv")
OUT = ROOT / "output/research_only/qqq_timing_dev"

# ---------------------------------------------------------------- costs (IBKR Pro Tiered, $10,000 start)
START_EQUITY = 10_000.0
COMMISSION_PER_SHARE = 0.0035
COMMISSION_MIN = 0.35
COMMISSION_MAX_FRAC = 0.01
FEES_PER_SHARE = 0.0005           # exchange + clearing pass-through, order of magnitude
SELL_REG_FRAC = 0.3e-4            # SEC + FINRA TAF on sells, order of magnitude
HALF_SPREAD = 1e-4                # 1 bp for QQQ, QLD and the T-bill ETF
NOMINAL_PRICE = 50.0              # share price assumed where no real close is used (QLD, T-bill ETF, NDX era)
CASH_ETF_FEE = 0.0010             # BIL / SGOV-like expense ratio, per year
LEV_ER = 0.0095                   # QLD expense ratio, per year
VT_BAND = 0.25
TRADING_DAYS = 252
EXPOSURE_LEVELS = (0.0, 1.0, 2.0)


# ======================================================================== the date guard

class FutureDataError(AssertionError):
    pass


def assert_dev_dates(dates, end: str = DEV_END) -> None:
    """Raise when any date (YYYY-MM-DD strings or Timestamps) is after the development end."""
    values = pd.to_datetime(pd.Index(list(dates)) if not isinstance(dates, (pd.Series, pd.Index)) else dates)
    if len(values) and values.max() > pd.Timestamp(end):
        raise FutureDataError(f"date {values.max().date()} is after the development end {end}")


def truncate_dev(frame: pd.DataFrame, column: str = "date", end: str = DEV_END) -> pd.DataFrame:
    out = frame[pd.to_datetime(frame[column]) <= pd.Timestamp(end)].copy()
    assert_dev_dates(out[column], end)
    return out


# ======================================================================== data

def parse_chart(path: Path, end: str = DEV_END) -> pd.DataFrame:
    """Yahoo chart JSON -> date, close (split-adjusted), adjclose, dividend; truncated at ``end`` at once."""
    j = json.loads(Path(path).read_text())["chart"]["result"][0]
    ts = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    q = j["indicators"]["quote"][0]
    adj = j["indicators"].get("adjclose", [{}])[0].get("adjclose") or q["close"]
    df = pd.DataFrame({"date": ts.strftime("%Y-%m-%d"), "close": q["close"], "adjclose": adj})
    divs = j.get("events", {}).get("dividends", {})
    dmap = {}
    for v in divs.values():
        d = pd.to_datetime(v["date"], unit="s", utc=True).tz_convert("America/New_York").strftime("%Y-%m-%d")
        dmap[d] = dmap.get(d, 0.0) + float(v["amount"])
    df["dividend"] = df["date"].map(dmap).fillna(0.0)
    df = truncate_dev(df.dropna(subset=["close"]), "date", end)
    if df["date"].duplicated().any():
        raise ValueError(f"duplicate dates in {path}")
    return df.reset_index(drop=True)


def load_kf_rf(end: str = DEV_END) -> pd.Series:
    with zipfile.ZipFile(KF_ZIP) as z:
        text = z.read(z.namelist()[0]).decode("latin-1")
    rows = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 7 and parts[0].isdigit() and len(parts[0]) == 8:
            rows.append((pd.Timestamp(parts[0]).strftime("%Y-%m-%d"), float(parts[6]) / 100.0))
    df = truncate_dev(pd.DataFrame(rows, columns=["date", "rf"]), "date", end)
    return pd.Series(df["rf"].values, index=pd.DatetimeIndex(df["date"]))


def load_dtb3(end: str = DEV_END) -> pd.Series:
    df = pd.read_csv(CACHE / "DTB3.csv")
    df.columns = ["date", "dtb3"]
    df["dtb3"] = pd.to_numeric(df["dtb3"], errors="coerce")
    df = truncate_dev(df.dropna(), "date", end)
    return pd.Series(df["dtb3"].values / 100.0, index=pd.DatetimeIndex(df["date"]))


@dataclass
class DevData:
    sessions: pd.DatetimeIndex          # NDX sessions before 1999-03-10, QQQ sessions from then on
    r1: pd.Series                       # 1x daily return (NDX price before 1999-03-11, QQQ total return after)
    price: pd.Series                    # QQQ split-adjusted close for share counts (NaN before 1999-03-10)
    rf: pd.Series                       # Ken French daily RF on the sessions
    qld: pd.Series                      # real QLD daily total return (2006-06-22 ..)
    qqq_raw: pd.DataFrame               # QQQ close / adjclose / dividend rows (for the data checks)
    dtb3: pd.Series
    guard: dict = field(default_factory=dict)


def load_dev_data(end: str = DEV_END) -> DevData:
    qqq = parse_chart(CACHE / "chart_QQQ.json", end)
    qld = parse_chart(CACHE / "chart_QLD.json", end)
    ndx = parse_chart(CACHE / "chart_%5ENDX.json", end)
    rf = load_kf_rf(end)
    dtb3 = load_dtb3(end)

    ndx = ndx[ndx["date"] < qqq["date"].iloc[0]]
    idx_ndx = pd.DatetimeIndex(ndx["date"])
    idx_qqq = pd.DatetimeIndex(qqq["date"])
    sessions = idx_ndx.append(idx_qqq)
    level_ndx = pd.Series(ndx["close"].values, index=idx_ndx)
    level_qqq = pd.Series(qqq["adjclose"].values, index=idx_qqq)
    r_ndx = level_ndx.pct_change()
    r_qqq = level_qqq.pct_change()
    # 1999-03-10 (QQQ's first day): NDX return from 1999-03-09 to 1999-03-10 is the best available
    first = idx_qqq[0]
    ndx_all = parse_chart(CACHE / "chart_%5ENDX.json", end).set_index("date")["close"]
    r_qqq.iloc[0] = ndx_all.loc[first.strftime("%Y-%m-%d")] / ndx_all.loc[idx_ndx[-1].strftime("%Y-%m-%d")] - 1
    r1 = pd.concat([r_ndx, r_qqq]).fillna(0.0)
    price = pd.Series(np.nan, index=sessions)
    price.loc[idx_qqq] = qqq["close"].values
    rf_s = rf.reindex(sessions).ffill().fillna(0.0)
    qld_level = pd.Series(qld["adjclose"].values, index=pd.DatetimeIndex(qld["date"]))
    qld_r = qld_level.pct_change().dropna()

    guard = {"dev_end": end}
    for name, ix in [("sessions", sessions), ("qqq", idx_qqq), ("qld", qld_r.index), ("ndx", idx_ndx),
                     ("kf_rf", rf.index), ("dtb3", dtb3.index)]:
        assert_dev_dates(ix, end)
        guard[name] = {"first": str(ix.min().date()), "last": str(ix.max().date()), "rows": int(len(ix))}
    return DevData(sessions=sessions, r1=r1, price=price, rf=rf_s, qld=qld_r, qqq_raw=qqq, dtb3=dtb3, guard=guard)


# ======================================================================== synthetic 2x

def synthetic_2x(r1: pd.Series, rf: pd.Series, spread: float, er: float = LEV_ER) -> pd.Series:
    """Daily-rebalanced 2x: 2 * r - (rf_daily + spread / 252) - er / 252 (one unit of notional is borrowed)."""
    return 2.0 * r1 - (rf + spread / TRADING_DAYS) - er / TRADING_DAYS


def cagr_of(r: pd.Series, periods: float = TRADING_DAYS) -> float:
    r = pd.Series(r).dropna()
    return float(np.prod(1 + r.values) ** (periods / len(r)) - 1) if len(r) else float("nan")


def calibrate_spread(data: DevData, start: str = QLD_CAL_START, end: str = DEV_END) -> dict:
    real = data.qld.loc[start:end]
    assert_dev_dates(real.index, DEV_END)
    r1 = data.r1.reindex(real.index)
    rf = data.rf.reindex(real.index)
    target = cagr_of(real)
    lo, hi = -0.05, 0.10
    for _ in range(80):
        mid = (lo + hi) / 2
        if cagr_of(synthetic_2x(r1, rf, mid)) > target:
            lo = mid
        else:
            hi = mid
    s = (lo + hi) / 2
    out = {"window": [str(real.index[0].date()), str(real.index[-1].date())], "sessions": int(len(real)),
           "spread_calibrated": s}
    for tag, sp in [("spread_0", 0.0), ("calibrated", s)]:
        syn = synthetic_2x(r1, rf, sp)
        d = syn - real
        out[tag] = {"cagr_gap_syn_minus_real": cagr_of(syn) - target,
                    "tracking_error_daily_ann": float(d.std() * math.sqrt(TRADING_DAYS)),
                    "tracking_error_monthly_ann": float((monthly(syn) - monthly(real)).std() * math.sqrt(12)),
                    "corr": float(np.corrcoef(syn, real)[0, 1]),
                    "mean_daily_diff_bp": float(d.mean() * 1e4), "max_abs_daily_diff": float(d.abs().max())}
    # tracking by calendar year (cagr gaps only; both legs are dev-period data)
    yrs = {}
    syn = synthetic_2x(r1, rf, s)
    for y in sorted(set(real.index.year)):
        m = real.index.year == y
        yrs[int(y)] = {"gap": float(np.prod(1 + syn[m]) - np.prod(1 + real[m])),
                       "te": float((syn[m] - real[m]).std() * math.sqrt(TRADING_DAYS))}
    out["by_year"] = yrs
    return out


def data_checks(data: DevData) -> dict:
    q = data.qqq_raw
    adj_r = q["adjclose"].pct_change()
    cd_r = (q["close"] + q["dividend"]) / q["close"].shift(1) - 1
    d = (adj_r - cd_r).dropna()
    out = {"qqq_adj_vs_close_plus_div": {"max_abs_daily_diff": float(d.abs().max()),
                                         "cagr_adj": cagr_of(adj_r.dropna()), "cagr_close_div": cagr_of(cd_r.dropna()),
                                         "dividends_counted": int((q["dividend"] > 0).sum())}}
    if TIINGO_QQQ.exists():
        t = pd.read_csv(TIINGO_QQQ)
        t = truncate_dev(t, "date")
        tr = pd.Series(t["adjClose"].values, index=pd.DatetimeIndex(t["date"])).pct_change().dropna()
        yr = pd.Series(q["adjclose"].values, index=pd.DatetimeIndex(q["date"])).pct_change()
        j = pd.concat([tr, yr], axis=1, join="inner").dropna()
        out["qqq_yahoo_vs_tiingo_2010_2014"] = {"sessions": int(len(j)),
                                                "max_abs_daily_diff": float((j.iloc[:, 0] - j.iloc[:, 1]).abs().max()),
                                                "cagr_gap": cagr_of(j.iloc[:, 1]) - cagr_of(j.iloc[:, 0])}
    ndx = data.r1.loc[:"1999-03-09"]
    per_year = ndx.groupby(ndx.index.year).size()
    out["ndx_pre1999"] = {"sessions_per_year_min": int(per_year.iloc[1:-1].min()),
                          "sessions_per_year_max": int(per_year.iloc[1:-1].max()),
                          "abs_daily_moves_over_10pct": int((ndx.abs() > 0.10).sum()),
                          "zero_return_days": int((ndx == 0).sum())}
    rf = data.rf.loc[DEV_START:DEV_END]
    dt = data.dtb3.loc[DEV_START:DEV_END]
    out["rf_check"] = {"kf_rf_mean_annualised": float(rf.mean() * TRADING_DAYS), "dtb3_mean": float(dt.mean())}
    return out


# ======================================================================== signals (data up to t only)

def trend_state(p: pd.Series, length: int, band: float) -> pd.Series:
    """1.0 = on, 0.0 = off, NaN before the MA exists. Uses P and MA up to and including t."""
    ma = p.rolling(length, min_periods=length).mean()
    up = (p > ma * (1 + band)).values
    dn = (p < ma * (1 - band)).values
    valid = ma.notna().values
    pv, mv = p.values, ma.values
    out = np.full(len(p), np.nan)
    state = np.nan
    for i in range(len(p)):
        if not valid[i]:
            continue
        if np.isnan(state):
            state = 1.0 if pv[i] > mv[i] else 0.0
        if up[i]:
            state = 1.0
        elif dn[i]:
            state = 0.0
        out[i] = state
    return pd.Series(out, index=p.index)


def realised_vol(r: pd.Series, window: int) -> pd.Series:
    return r.rolling(window, min_periods=window).std() * math.sqrt(TRADING_DAYS)


def month_end_only(sig: pd.Series) -> pd.Series:
    """Keep the value of each month's last session and carry it forward until the next month end."""
    idx = sig.index
    is_me = pd.Series(idx.to_period("M"), index=idx) != pd.Series(idx.to_period("M"), index=idx).shift(-1)
    is_me.iloc[-1] = False   # the last session of the data is not known to be a month end
    return sig.where(is_me.values).ffill()


@dataclass(frozen=True)
class Rule:
    family: str            # T01 T02 T12 VT VTT M01 M02
    length: int = 0
    band: float = 0.0
    target: float = 0.0
    window: int = 0

    @property
    def name(self) -> str:
        if self.family in ("T01", "T02", "T12", "M01", "M02"):
            return f"{self.family}_L{self.length}_b{int(round(self.band * 100))}"
        if self.family == "VT":
            return f"VT_t{int(self.target * 100)}_w{self.window}"
        return f"VTT_t{int(self.target * 100)}_w{self.window}_L{self.length}"

    @property
    def continuous(self) -> bool:
        return self.family in ("VT", "VTT")


def target_exposure(rule: Rule, p: pd.Series, r1: pd.Series) -> pd.Series:
    """Desired exposure decided at each close t (only data up to t)."""
    if rule.family in ("T01", "T02", "T12", "M01", "M02"):
        st = trend_state(p, rule.length, rule.band)
        if rule.family.startswith("M"):
            st = month_end_only(st)
        on, off = {"T01": (1, 0), "T02": (2, 0), "T12": (2, 1), "M01": (1, 0), "M02": (2, 0)}[rule.family]
        return st.map({1.0: float(on), 0.0: float(off)})
    vt = (rule.target / realised_vol(r1, rule.window)).clip(upper=2.0)
    if rule.family == "VT":
        return vt
    st = trend_state(p, rule.length, 0.02)
    return vt.where(st != 0.0, 0.0).where(st.notna())


def rule_grid() -> list[Rule]:
    rules = []
    for fam in ("T01", "T02", "T12"):
        for L in (75, 100, 112, 125, 150, 188, 200, 250):
            for b in (0.0, 0.01, 0.02, 0.03, 0.05):
                rules.append(Rule(fam, L, b))
    for t in (0.15, 0.20, 0.25):
        for w in (20, 60):
            rules.append(Rule("VT", target=t, window=w))
    for t in (0.15, 0.20, 0.25):
        for w in (20, 60):
            for L in (150, 200):
                rules.append(Rule("VTT", L, 0.02, t, w))
    for fam in ("M01", "M02"):
        for L in (150, 200):
            rules.append(Rule(fam, L, 0.0))
    return rules


# ======================================================================== simulation

def weights_for(e: float) -> np.ndarray:
    """Exposure -> sleeve weights (QQQ, 2x fund, cash)."""
    e = float(min(max(e, 0.0), 2.0))
    if e <= 1.0:
        return np.array([e, 0.0, 1.0 - e])
    return np.array([2.0 - e, e - 1.0, 0.0])


def order_cost(value: float, price: float, sell: bool) -> float:
    if value <= 0:
        return 0.0
    shares = value / price
    comm = min(max(COMMISSION_MIN, COMMISSION_PER_SHARE * shares), COMMISSION_MAX_FRAC * value)
    return comm + FEES_PER_SHARE * shares + HALF_SPREAD * value + (SELL_REG_FRAC * value if sell else 0.0)


def simulate(target: pd.Series, r1: pd.Series, r2: pd.Series, rc: pd.Series, price: pd.Series,
             start: str, end: str, lag: int = 1, continuous: bool = False, cash_is_etf: bool = True) -> dict:
    """Run the sleeve portfolio.

    ``target`` is the exposure decided at each close t. With ``lag`` = 1 the trade happens at the close of t+1
    (so the new exposure earns from t+2); with ``lag`` = 0 at the close of t. The portfolio starts as cash at the
    close of ``start`` and trades there to the target decided ``lag`` sessions earlier.
    """
    idx = r1.index
    i0 = int(idx.searchsorted(pd.Timestamp(start)))
    i1 = int(idx.searchsorted(pd.Timestamp(end), side="right")) - 1
    tv = target.reindex(idx).values
    a1, a2, ac, pv = r1.values, r2.values, rc.values, price.values
    sleeves = np.array([0.0, 0.0, START_EQUITY])
    cur_state = None
    values, expo, orders, rebal, costs, cost_frac = [], [], 0, 0, 0.0, 0.0
    for i in range(i0, i1 + 1):
        if i > i0:
            sleeves = sleeves * (1 + np.array([a1[i], a2[i], ac[i]]))
        total = sleeves.sum()
        held_e = (sleeves[0] + 2 * sleeves[1]) / total
        desired = tv[i - lag] if i - lag >= 0 else np.nan
        if np.isnan(desired):
            desired = 0.0 if cur_state is None else cur_state
        if continuous:
            go = cur_state is None or abs(desired - held_e) >= VT_BAND - 1e-12 or (desired == 0.0 and held_e > 0)
        else:
            go = cur_state is None or desired != cur_state
        if go:
            new = weights_for(desired) * total
            delta = new - sleeves
            px = [pv[i] if not np.isnan(pv[i]) else NOMINAL_PRICE, NOMINAL_PRICE, NOMINAL_PRICE]
            c = 0.0
            for k in range(3):
                if k == 2 and not cash_is_etf:
                    continue
                if abs(delta[k]) > 1.0:
                    c += order_cost(abs(delta[k]), px[k], delta[k] < 0)
                    orders += 1
            if cur_state is not None:
                rebal += 1
            sleeves = new * (1 - c / total)
            costs += c
            cost_frac += c / total
            cur_state = desired
            held_e = desired
        values.append(sleeves.sum())
        expo.append(held_e)     # exposure held into the next session
    v = pd.Series(values, index=idx[i0:i1 + 1])
    e = pd.Series(expo, index=idx[i0:i1 + 1])
    return {"value": v, "ret": v.pct_change().iloc[1:], "exposure_held": e.shift(1).iloc[1:],
            "orders": orders, "rebalances": rebal, "costs": costs, "cost_frac": cost_frac}


# ======================================================================== metrics

def max_drawdown(value: pd.Series) -> float:
    return float((value / value.cummax() - 1).min())


def monthly(r: pd.Series) -> pd.Series:
    return (1 + r).groupby(r.index.to_period("M")).prod() - 1


def yearly(r: pd.Series) -> pd.Series:
    return (1 + r).groupby(r.index.year).prod() - 1


def cagr_months(m: pd.Series) -> float:
    return float(np.prod(1 + m.values) ** (12 / len(m)) - 1)


def metrics(sim: dict, bench: dict, rf: pd.Series) -> dict:
    r, b, v = sim["ret"], bench["ret"], sim["value"]
    years = len(r) / TRADING_DAYS
    cagr = cagr_of(r)
    bcagr = cagr_of(b)
    ex = r - rf.reindex(r.index)
    mdd = max_drawdown(v)
    e = sim["exposure_held"]
    m, mb = monthly(r), monthly(b)
    mx = m - mb
    out = {
        "cagr": cagr, "excess_cagr_vs_qqq": cagr - bcagr, "vol": float(r.std() * math.sqrt(TRADING_DAYS)),
        "max_dd": mdd, "sharpe": float(ex.mean() / ex.std() * math.sqrt(TRADING_DAYS)),
        "calmar": cagr / abs(mdd) if mdd < 0 else float("nan"),
        "time_cash": float((e < 0.05).mean()), "time_between_0_1": float(((e >= 0.05) & (e < 0.95)).mean()),
        "time_1x": float(((e >= 0.95) & (e <= 1.05)).mean()), "time_between_1_2": float(((e > 1.05) & (e < 1.95)).mean()),
        "time_2x": float((e >= 1.95).mean()), "avg_exposure": float(e.mean()),
        "switches_per_year": sim["rebalances"] / years, "orders_per_year": sim["orders"] / years,
        "cost_drag_per_year": sim["cost_frac"] / years,
        "worst_year": float(yearly(r).min()), "worst_year_which": int(yearly(r).idxmin()),
        "months": int(len(mx)), "mean_monthly_excess": float(mx.mean()),
        "t_monthly_excess": float(mx.mean() / mx.std() * math.sqrt(len(mx))) if mx.std() > 0 else 0.0,
        "ir_monthly": float(mx.mean() / mx.std()) if mx.std() > 0 else 0.0,
        "skew_mx": float(mx.skew()), "kurt_mx": float(mx.kurt() + 3.0),
    }
    # drop-best-months: strategy's own best months removed, and the biggest-excess months replaced by QQQ's
    for k in (3, 6):
        keep = m.drop(m.nlargest(k).index)
        out[f"cagr_drop_best{k}"] = cagr_months(keep)
        swapped = m.copy()
        top = mx.nlargest(k).index
        swapped.loc[top] = mb.loc[top]
        out[f"excess_drop_best{k}_excess_months"] = cagr_months(swapped) - cagr_months(mb)
    return out


def deflated_sharpe(sr: float, n_obs: int, skew: float, kurt: float, sr_trials: np.ndarray) -> dict:
    """Bailey and Lopez de Prado (2014), per-period Sharpe ratios (here: monthly IR of excess over QQQ)."""
    n = len(sr_trials)
    var = float(np.var(sr_trials, ddof=1))
    g = 0.5772156649
    sr0 = math.sqrt(var) * ((1 - g) * NORM.inv_cdf(1 - 1 / n) + g * NORM.inv_cdf(1 - 1 / (n * math.e)))
    denom = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr ** 2, 1e-12))
    z = (sr - sr0) * math.sqrt(n_obs - 1) / denom
    return {"n_trials": n, "sr0_monthly": sr0, "dsr": float(NORM.cdf(z)), "z": z}


def bonferroni_t(n: int, alpha: float = 0.05) -> float:
    return NORM.inv_cdf(1 - alpha / n)


# ======================================================================== main

def run(args) -> dict:
    data = load_dev_data(DEV_END)
    print("DATE GUARD: every vendor frame truncated at", DEV_END, "and asserted;",
          {k: v["last"] for k, v in data.guard.items() if isinstance(v, dict)})
    assert_dev_dates(data.sessions)
    checks = data_checks(data)
    cal = calibrate_spread(data)
    spread = cal["spread_calibrated"]
    print(f"synthetic 2x: calibrated spread {spread:.4%}/yr on {cal['window']}, "
          f"TE {cal['calibrated']['tracking_error_daily_ann']:.2%}, corr {cal['calibrated']['corr']:.4f}")

    p = (1 + data.r1).cumprod()
    r1 = data.r1
    rc_etf = data.rf - CASH_ETF_FEE / TRADING_DAYS
    rc_zero = pd.Series(0.0, index=data.sessions)
    r2 = synthetic_2x(r1, data.rf, spread)
    r2_dear = synthetic_2x(r1, data.rf, spread + 0.01)

    periods = {"dev": (DEV_START, DEV_END), "second": (SECOND_START, SECOND_END)}
    const1 = pd.Series(1.0, index=data.sessions)
    const2 = pd.Series(2.0, index=data.sessions)
    bench = {k: simulate(const1, r1, r2, rc_etf, data.price, *v) for k, v in periods.items()}
    refs = {"QQQ_buyhold": const1, "2x_buyhold": const2, "1.5x_buyhold": pd.Series(1.5, index=data.sessions)}

    rows, monthly_keep, yearly_tab = [], {}, {}
    rules = rule_grid()
    assert len(rules) == 142, len(rules)
    for name, tgt in list(refs.items()) + [(r.name, r) for r in rules]:
        is_rule = isinstance(tgt, Rule)
        rule = tgt if is_rule else None
        target = target_exposure(rule, p, r1) if is_rule else tgt
        cont = rule.continuous if is_rule else (name == "1.5x_buyhold")
        row = {"config": name, "family": rule.family if is_rule else "REF", "counted": bool(is_rule),
               "length": rule.length if is_rule else None, "band": rule.band if is_rule else None,
               "vol_target": rule.target if is_rule else None, "vol_window": rule.window if is_rule else None}
        sim = simulate(target, r1, r2, rc_etf, data.price, *periods["dev"], continuous=cont)
        assert_dev_dates(sim["ret"].index)
        row.update(metrics(sim, bench["dev"], data.rf))
        sim2 = simulate(target, r1, r2, rc_etf, data.price, *periods["second"], continuous=cont)
        m2 = metrics(sim2, bench["second"], data.rf)
        for k in ("cagr", "excess_cagr_vs_qqq", "max_dd", "sharpe", "calmar", "switches_per_year", "t_monthly_excess"):
            row[f"second_{k}"] = m2[k]
        for tag, kw in [("sameday", dict(lag=0)), ("cash0", dict(cash_is_etf=False)), ("dear2x", {})]:
            rc = rc_zero if tag == "cash0" else rc_etf
            rr2 = r2_dear if tag == "dear2x" else r2
            s = simulate(target, r1, rr2, rc, data.price, *periods["dev"], continuous=cont, **kw)
            bm = bench["dev"] if tag != "cash0" else simulate(const1, r1, r2, rc_zero, data.price, *periods["dev"])
            mm = metrics(s, bm, data.rf)
            row[f"{tag}_cagr"], row[f"{tag}_excess"], row[f"{tag}_max_dd"] = mm["cagr"], mm["excess_cagr_vs_qqq"], mm["max_dd"]
        rows.append(row)
        monthly_keep[name] = monthly(sim["ret"])
        yearly_tab[name] = yearly(sim["ret"])
    grid = pd.DataFrame(rows)

    counted = grid[grid["counted"]]
    n = len(counted)
    hurdle = bonferroni_t(n)
    best = counted.sort_values("t_monthly_excess", ascending=False).iloc[0]
    dsr_best = deflated_sharpe(best["ir_monthly"], int(best["months"]), best["skew_mx"], best["kurt_mx"],
                               counted["ir_monthly"].values)
    grid["dsr"] = [deflated_sharpe(r.ir_monthly, int(r.months), r.skew_mx, r.kurt_mx, counted["ir_monthly"].values)["dsr"]
                   if r.counted else np.nan for r in grid.itertuples()]

    OUT.mkdir(parents=True, exist_ok=True)
    grid.to_csv(OUT / "grid.csv", index=False, float_format="%.6f")
    pd.DataFrame(yearly_tab).to_csv(OUT / "by_year.csv", float_format="%.6f")
    mk = pd.DataFrame(monthly_keep)
    mk.index = mk.index.astype(str)
    mk.to_csv(OUT / "monthly_returns.csv", float_format="%.6f")
    summary = {"guard": data.guard, "data_checks": checks, "synthetic_2x_calibration": cal,
               "n_counted_configs": n, "bonferroni_t_one_sided_5pct": hurdle,
               "best_by_t": {"config": best["config"], "t": best["t_monthly_excess"], "dsr": dsr_best},
               "note": "development period only (1999-03-10 .. 2014-12-31); second period 1986-10 .. 1999-03 on NDX price"}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=float))
    print(f"{n} counted configs, Bonferroni one-sided 5% t = {hurdle:.2f}; best t {best['config']} "
          f"{best['t_monthly_excess']:.2f}, DSR {dsr_best['dsr']:.3f}")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
