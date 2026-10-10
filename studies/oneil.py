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

Usage:  PYTHONPATH=. .venv/bin/python -m quant study oneil --calibrate fold1   (or scripts/research_oneil.py)
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

from quant.backtest.costs import rank_half_spread
from quant.data import eps as pit_eps
from quant.data import panel
from quant.data import version as dv
from quant.data.panel import TERMINAL_D5, TERMINAL_D5_STRESS
from quant.evaluation.metrics import max_drawdown_of_returns
from quant.paths import CACHE_ROOT, ROOT
from quant.strategies import canslim as cs  # (EPS states, as-of merge, order cost, QQQ benchmark)
from quant.strategies import indicators as ind
from quant.strategies import livermore as lv

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


# ======================================================================== pattern detection (closes only)

@dataclass(frozen=True)
class BaseParams:
    handle_min: int = 5          # sessions after the right peak (>= 1 week)
    handle_max: int = 25         # right peak searched in the last 25 sessions
    handle_dd: float = 0.12      # handle decline <= 12% (closes)
    cup_min: int = 35            # 7 weeks
    cup_max: int = 325           # 65 weeks
    depth_min: float = 0.12
    depth_max: float = 0.33
    depth_bear: float = 0.40     # when the Composite itself fell >= bear_drop during the base
    bear_drop: float = 0.20
    right_lo: float = 0.90       # right peak relative to the left peak
    right_hi: float = 1.05
    side_min: int = 15           # each side of the cup >= 3 weeks (not a V)
    bottom_days: int = 10        # >= 2 weeks of closes in the lowest quarter of the cup (rounded bottom)
    prior_lb: int = 130          # prior-uptrend look-back before the left peak / base start
    prior_up_cup: float = 0.30
    flat_min: int = 25           # 5 weeks
    flat_depth: float = 0.15
    prior_up_flat: float = 0.20
    db_seg: int = 10
    db_mid_rise: float = 0.10
    db_tail: int = 5
    vol_mult: float = 1.4        # breakout volume vs the prior 50-session average
    buy_within: float = 0.05     # buy only within 5% of the pivot


BP = BaseParams()


def _tick(h: float, tick_abs: float) -> float:
    return max(0.001 * h, tick_abs)


def _depth_ok(depth: float, ix: np.ndarray | None, p: BaseParams) -> bool:
    lim = p.depth_max
    if ix is not None and len(ix) and np.isfinite(ix).all():
        dd = 1 - np.min(ix / np.maximum.accumulate(ix))
        if dd >= p.bear_drop:
            lim = p.depth_bear
    return p.depth_min <= depth <= lim


def _prior_up(c: np.ndarray, at: int, peak: float, need: float, p: BaseParams) -> bool:
    p0 = at - p.prior_lb
    if p0 < 0:
        return False
    lo = np.min(c[p0:at + 1])
    return bool(np.isfinite(lo) and lo > 0 and peak >= (1 + need) * lo)


def cup_with_handle(c: np.ndarray, v: np.ndarray, ix: np.ndarray | None = None, tick_abs: float = 0.0,
                    p: BaseParams = BP) -> dict | None:
    """Cup with handle completed at the last element of ``c`` (the session before a possible breakout)."""
    n = len(c)
    if n < p.cup_min + p.handle_min + 2:
        return None
    w0 = n - p.handle_max
    if w0 < 0:
        return None
    seg = c[w0:n]
    if not np.isfinite(seg).all():
        return None
    r = w0 + int(np.argmax(seg))
    h = n - 1 - r
    if h < p.handle_min:
        return None
    handle = c[r + 1:n]
    hmin = float(handle.min())
    hdd = 1 - hmin / c[r]
    if hdd > p.handle_dd:
        return None
    lo, hi = r - p.cup_max, r - p.cup_min
    if hi < 0:
        return None
    lo = max(lo, 0)
    win = c[lo:hi + 1]
    if not np.isfinite(win).any():
        return None
    l_ = lo + int(np.nanargmax(win))
    cup = c[l_:r + 1]
    if not np.isfinite(cup).all():
        return None
    left = float(c[l_])
    if len(cup) > 2 and cup[1:-1].max() > max(left, c[r]):
        return None
    if not (p.right_lo * left <= c[r] <= p.right_hi * left):
        return None
    b = l_ + int(np.argmin(cup))
    bottom = float(c[b])
    depth = 1 - bottom / left
    if not _depth_ok(depth, None if ix is None else ix[l_:r + 1], p):
        return None
    if b - l_ < p.side_min or r - b < p.side_min:
        return None
    if int((cup <= bottom + 0.25 * (left - bottom)).sum()) < p.bottom_days:
        return None
    if hmin < bottom + 0.5 * (left - bottom):
        return None
    hs = c[r:n]
    if np.polyfit(np.arange(len(hs)), hs, 1)[0] > 0:
        return None
    pre = v[max(0, r - 50):r]
    if not (np.isfinite(v[r + 1:n]).all() and np.isfinite(pre).any()) or np.mean(v[r + 1:n]) >= np.nanmean(pre):
        return None
    if not _prior_up(c, l_, left, p.prior_up_cup, p):
        return None
    return {"type": "cup_handle", "left": l_, "bottom": b, "right": r, "depth": depth, "handle_depth": hdd,
            "handle_days": h, "base_days": n - l_, "pivot": float(c[r]) + _tick(float(c[r]), tick_abs)}


def flat_base(c: np.ndarray, v: np.ndarray, ix: np.ndarray | None = None, tick_abs: float = 0.0,
              p: BaseParams = BP) -> dict | None:
    """Flat base: the longest run of closes ending at the last element with depth <= 15%, >= 5 weeks."""
    n = len(c)
    mx, mn, length = -np.inf, np.inf, 0
    for k in range(1, min(p.cup_max, n) + 1):
        x = c[n - k]
        if not np.isfinite(x):
            break
        mx2, mn2 = max(mx, x), min(mn, x)
        if 1 - mn2 / mx2 > p.flat_depth:
            break
        mx, mn, length = mx2, mn2, k
    if length < p.flat_min:
        return None
    start = n - length
    if not _prior_up(c, start, mx, p.prior_up_flat, p):
        return None
    return {"type": "flat_base", "left": start, "bottom": start + int(np.argmin(c[start:n])), "right": n - 1,
            "depth": 1 - mn / mx, "handle_depth": np.nan, "handle_days": 0, "base_days": length,
            "pivot": float(mx) + _tick(float(mx), tick_abs)}


def double_bottom(c: np.ndarray, v: np.ndarray, ix: np.ndarray | None = None, tick_abs: float = 0.0,
                  p: BaseParams = BP) -> dict | None:
    """W: left peak, first low b1, middle peak m, second low b2 undercutting b1; pivot = middle peak."""
    n = len(c)
    lo, hi = n - 1 - p.cup_max, n - 1 - p.cup_min
    if hi < 0:
        return None
    lo = max(lo, 0)
    win = c[lo:hi + 1]
    if not np.isfinite(win).any():
        return None
    l_ = lo + int(np.nanargmax(win))
    after = c[l_ + 1:n]
    if not np.isfinite(after).all() or after.max() > c[l_]:
        return None
    b2 = l_ + 1 + int(np.argmin(after))
    if n - 1 - b2 < p.db_tail or b2 - l_ < 3 * p.db_seg:
        return None
    first = c[l_ + 1:b2 - p.db_seg + 1]
    b1 = l_ + 1 + int(np.argmin(first))
    if b1 - l_ < p.db_seg or b2 - b1 < p.db_seg:
        return None
    m = b1 + 1 + int(np.argmax(c[b1 + 1:b2]))
    left, mid = float(c[l_]), float(c[m])
    if not (c[b2] < c[b1]) or mid < (1 + p.db_mid_rise) * c[b1] or mid >= left:
        return None
    if m + 1 < n and c[m + 1:n].max() > mid:
        return None
    depth = 1 - c[b2] / left
    if not _depth_ok(depth, None if ix is None else ix[l_:n], p):
        return None
    if not _prior_up(c, l_, left, p.prior_up_cup, p):
        return None
    return {"type": "double_bottom", "left": l_, "bottom": b2, "right": m, "depth": depth, "handle_depth": np.nan,
            "handle_days": 0, "base_days": n - l_, "pivot": mid + _tick(mid, tick_abs), "first_low": b1}


DETECTORS = (cup_with_handle, double_bottom, flat_base)     # priority order


def find_base(c_hist: np.ndarray, v_hist: np.ndarray, ix_hist: np.ndarray | None = None, tick_abs: float = 0.0,
              p: BaseParams = BP) -> dict | None:
    """The first base (in priority order) completed at the end of the history (up to session d-1)."""
    for f in DETECTORS:
        out = f(c_hist, v_hist, ix_hist, tick_abs, p)
        if out is not None:
            return out
    return None


def is_breakout(c: np.ndarray, v: np.ndarray, d: int, base: dict | None, p: BaseParams = BP) -> bool:
    if base is None or d < 50:
        return False
    avg = np.nanmean(v[d - 50:d])
    return bool(c[d] > base["pivot"] and np.isfinite(v[d]) and avg > 0 and v[d] >= p.vol_mult * avg)


def breakout_events(close_adj: pd.DataFrame, vol_adj: pd.DataFrame, close_raw: pd.DataFrame, ix_close: pd.Series,
                    elig: pd.DataFrame, first_day: pd.Timestamp, p: BaseParams = BP) -> pd.DataFrame:
    """All breakouts (session d, security) with d >= first_day, for eligible names; each uses data up to d only."""
    C = close_adj.to_numpy(float)
    V = vol_adj.to_numpy(float)
    R = close_raw.to_numpy(float)
    IX = ix_close.reindex(close_adj.index).to_numpy(float)
    prior_max = close_adj.shift(1).rolling(p.handle_max, min_periods=p.handle_max).max().to_numpy()
    avg50 = vol_adj.shift(1).rolling(50, min_periods=50).mean().to_numpy()
    cand = (C > prior_max * 1.001) & (V >= p.vol_mult * avg50) & elig.reindex_like(close_adj).fillna(False).to_numpy()
    cand[close_adj.index < first_day, :] = False
    rows = []
    sessions = close_adj.index
    for d, j in zip(*np.nonzero(cand)):
        c, v = C[:d, j], V[:d, j]
        ok = np.isfinite(c)
        if ok.sum() < p.cup_min + 10:
            continue
        tick_abs = 0.10 * (C[d - 1, j] / R[d - 1, j]) if np.isfinite(R[d - 1, j]) and R[d - 1, j] > 0 else 0.0
        base = find_base(c, v, IX[:d], tick_abs, p)
        if not is_breakout(C[:, j], V[:, j], d, base, p):
            continue
        rows.append({"session": sessions[d], "security_id": close_adj.columns[j], "type": base["type"],
                     "pivot": base["pivot"], "close": C[d, j], "depth": base["depth"],
                     "handle_depth": base["handle_depth"], "handle_days": base["handle_days"],
                     "base_weeks": base["base_days"] / 5, "left_date": sessions[base["left"]],
                     "bottom_date": sessions[base["bottom"]], "right_date": sessions[base["right"]],
                     "vol_ratio": V[d, j] / avg50[d, j]})
    return pd.DataFrame(rows)


# ======================================================================== M: distribution and follow-through days

def market_state(o, h, l, c, v, dd_limit: int = 5, ftd_gain: float = 0.0125, dd_drop: float = 0.002,
                 dd_window: int = 25, dd_expire_gain: float = 0.05, ftd_day: int = 4) -> pd.DataFrame:
    """O'Neil / IBD market direction from index OHLCV. Returns per session: uptrend (bool), dd_count, ftd, dd_day.
    State at session t uses data up to t only."""
    idx = pd.Series(c).index
    o, h, l, c, v = (np.asarray(x, float) for x in (o, h, l, c, v))
    n = len(c)
    up = np.zeros(n, bool)
    cnt = np.zeros(n, int)
    ftd = np.zeros(n, bool)
    ddd = np.zeros(n, bool)
    state = "correction"
    dds: list = []           # (session index, close)
    cur_low = l[0] if n else np.nan
    day1, att_low, rally_low = None, np.nan, np.nan
    for t in range(n):
        if t == 0:
            cnt[t] = 0
            continue
        chg = c[t] / c[t - 1] - 1
        vol_up = v[t] > v[t - 1]
        if state == "uptrend":
            if chg <= -dd_drop and vol_up:
                dds.append((t, c[t]))
                ddd[t] = True
            dds = [(i, x) for i, x in dds if t - i < dd_window and c[t] < x * (1 + dd_expire_gain)]
            if len(dds) >= dd_limit or c[t] < rally_low:
                state, dds = "correction", []
                cur_low, day1 = l[t], None
        else:
            upper_half = h[t] > l[t] and (c[t] - l[t]) / (h[t] - l[t]) >= 0.5
            if day1 is not None and l[t] < att_low:
                day1 = None
                cur_low = l[t]
            if day1 is None:
                cur_low = min(cur_low, l[t])
                if chg > 0 or upper_half:
                    day1, att_low = t, cur_low
            elif t - day1 + 1 >= ftd_day and chg >= ftd_gain and vol_up:
                state, dds, rally_low = "uptrend", [], att_low
                ftd[t] = True
        up[t] = state == "uptrend"
        cnt[t] = len(dds)
    return pd.DataFrame({"uptrend": up, "dd_count": cnt, "ftd": ftd, "dd_day": ddd}, index=idx)


# ======================================================================== sell rules

@dataclass(frozen=True)
class Config:
    dd_limit: int = 5
    ftd_gain: float = 0.0125
    rs_line: bool = False
    k: int = 5
    stop: float = 0.08
    profit: float = 0.20
    fast_gain: float = 0.20
    fast_days: int = 15          # sessions after the breakout day (3 weeks)
    hold_days: int = 40          # sessions after the breakout day (8 weeks)
    rs_min: float = 0.80
    group_top: float = 0.20
    c_min: float = 0.25
    a_cagr: float = 0.25
    spread_mult: float = 1.0
    account: float = 10_000.0

    @property
    def name(self) -> str:
        return f"DD{self.dd_limit}_FTD{self.ftd_gain * 100:.2f}_RSL{'on' if self.rs_line else 'off'}"


CAL_FIELDS = ("dd_limit", "ftd_gain", "rs_line")


def position_exit(pos: dict, i: int, gain: float, below50_heavy: bool, correction: bool, cfg: Config) -> str | None:
    """Exit reason decided at the close of session i (filled at i+1), or None. ``pos['entry_i']`` is the buy session
    (the breakout was the session before). Mutates the 8-week-hold state of ``pos``."""
    if gain <= -cfg.stop:
        return "stop"
    since_breakout = i - (pos["entry_i"] - 1)
    if pos.get("hold_until") is None and not pos.get("big") and since_breakout <= cfg.fast_days \
            and gain >= cfg.fast_gain:
        pos["hold_until"] = pos["entry_i"] - 1 + cfg.hold_days
    if pos.get("hold_until") is not None:
        if i < pos["hold_until"]:
            return None
        pos["hold_until"], pos["big"] = None, True
    if pos.get("big"):
        if below50_heavy:
            return "sma50_heavy"
        if gain <= 0:
            return "breakeven"
        return None
    if gain >= cfg.profit:
        return "profit"
    if below50_heavy:
        return "sma50_heavy"
    if correction and gain <= 0:
        return "market_correction"
    return None


# ======================================================================== data

def parse_ohlcv(path: Path, start: str, end: str) -> pd.DataFrame:
    j = json.loads(Path(path).read_text())["chart"]["result"][0]
    ts = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    q = j["indicators"]["quote"][0]
    df = pd.DataFrame({"date": ts.strftime("%Y-%m-%d"), **{k: q[k] for k in ("open", "high", "low", "close", "volume")}})
    df = df.dropna(subset=["close"])
    df = cs.truncate(df, "date", start, end).drop_duplicates("date", keep="last")
    df.index = pd.DatetimeIndex(df.pop("date"))
    for k in ("open", "high", "low"):
        df[k] = df[k].fillna(df["close"])
    return df


def build_eps_states(ciks) -> pd.DataFrame:
    """Point-in-time EPS states for every CIK with a local companyfacts file (CAN SLIM cache reused)."""
    if EPS_CACHE.is_file():
        st = pd.read_csv(EPS_CACHE, dtype={"filed": str, "avail": str, "q_end": str, "q_ya_end": str, "fy0_end": str})
    else:
        base = pd.read_csv(pit_eps.EPS_CACHE / "eps_states_filed.csv.gz",
                           dtype={"filed": str, "avail": str, "q_end": str, "q_ya_end": str, "fy0_end": str})
        have = set(base["cik"].astype(int))
        parts = [base]
        for c in sorted(set(int(x) for x in ciks) - have):
            if pit_eps._cf_path(c) is None:
                continue
            s = pit_eps.eps_states(pit_eps.extract_eps_facts(c))
            if len(s):
                parts.append(s)
        st = pd.concat(parts, ignore_index=True)
        EPS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        st.to_csv(EPS_CACHE, index=False)
    return st


@dataclass
class Period:
    name: str
    spec: dict
    win: lv.WinData
    ixic: pd.DataFrame
    oneq_lvl: pd.Series
    oneq_px: pd.Series
    eps: pd.DataFrame
    guard: dict


def load_period(name: str, terminal_awaiting: float = TERMINAL_D5) -> Period:
    spec = PERIODS[name]
    win = panel.load_window(f"oneil_{name}", dict(PERIODS[name]), terminal_awaiting)
    end = win.spec["effective_end"]
    ps = spec["price_start"]
    guard = dict(win.guard)
    ix = parse_ohlcv(RAW / "chart_%5EIXIC.json", ps, end)
    missing = win.sessions.difference(ix.index)
    if len(missing) > 5:
        raise ValueError(f"^IXIC misses {len(missing)} stock sessions")
    ix = ix.reindex(win.sessions).ffill()
    cs.assert_window(ix.index, ps, end, "^IXIC")
    lvl, px = ind.oneq_on_sessions(win.sessions, ps, end)
    cs.assert_window(lvl.dropna().index, ps, end, "ONEQ")
    ciks = pd.to_numeric(win.universe["cik"], errors="coerce").dropna().astype(int).unique()
    eps = build_eps_states(ciks)
    n0 = len(eps)
    eps = eps[eps["filed"].astype(str) <= end].copy()
    assert (eps["avail"].astype(str) <= end).all()
    guard["eps_states"] = {"rows_kept": int(len(eps)), "rows_dropped_filed_after_end": int(n0 - len(eps)),
                           "max_filed": str(eps["filed"].max())}
    guard["ixic"] = {"first": str(ix.index.min().date()), "last": str(ix.index.max().date()),
                     "missing_sessions_ffilled": int(len(missing))}
    guard["assertion"] += f"; ^IXIC/ONEQ in [{ps}, {end}]; EPS states filed <= {end}"
    return Period(name=name, spec={**spec, "effective_end": end}, win=win, ixic=ix, oneq_lvl=lvl, oneq_px=px,
                  eps=eps, guard=guard)


# ======================================================================== weekly screen (C, A, L, groups)

def rs_score(idx: pd.DataFrame) -> pd.DataFrame:
    r = lambda n: idx / idx.shift(n) - 1  # noqa: E731
    return 2 * r(63) + r(126) + r(189) + r(252)


def weekly_screen(per: Period, cfg: Config) -> pd.DataFrame:
    w = per.win
    u = lv.eligible_universe(w)
    cik = (w.universe.dropna(subset=["cik"]).drop_duplicates("security_id", keep="last")
           .set_index("security_id")["cik"])
    u["cik"] = u["security_id"].map(cik)
    sess = w.sessions
    pos = sess.searchsorted(u["week_end"], side="right") - 1
    u = u[pos >= 0].copy()
    pos = pos[pos >= 0]
    u["session"] = sess[pos]
    col = {s: i for i, s in enumerate(w.sig_idx.columns)}
    rs = rs_score(w.sig_idx).to_numpy()
    u["rs_score"] = rs[pos, u["security_id"].map(col).to_numpy()]
    u = u[np.isfinite(u["rs_score"])].copy()
    u["rs_pct"] = u.groupby("week_end")["rs_score"].rank(pct=True)
    grp = u.groupby(["week_end", "ff49"]).agg(g=("rs_score", "median"), n=("rs_score", "size")).reset_index()
    grp = grp[grp["n"] >= 3].copy()
    grp["group_rank"] = grp.groupby("week_end")["g"].rank(ascending=False, method="first")
    grp["n_groups"] = grp.groupby("week_end")["g"].transform("size")
    u = u.merge(grp[["week_end", "ff49", "group_rank", "n_groups"]], on=["week_end", "ff49"], how="left")
    u = pit_eps.attach_eps(u, per.eps)
    stale_q = (u["week_end"] - pd.to_datetime(u["q_end"])).dt.days
    stale_y = (u["week_end"] - pd.to_datetime(u["fy0_end"])).dt.days
    u["pass_C"] = (u["c_growth"] >= cfg.c_min) & (stale_q <= 200)
    u["pass_A"] = (u["a_cagr3"] >= cfg.a_cagr) & (stale_y <= 550)
    u["pass_L"] = u["rs_pct"] >= cfg.rs_min
    u["pass_G"] = u["group_rank"] <= np.ceil(cfg.group_top * u["n_groups"])
    u["pass_all"] = u["pass_C"] & u["pass_A"] & u["pass_L"] & u["pass_G"].fillna(False)
    assert (pd.to_datetime(u["avail"].dropna()) < u.loc[u["avail"].notna(), "week_end"]).all(), "EPS look-ahead"
    return u


# ======================================================================== engine

@dataclass
class SimInputs:
    perf: pd.DatetimeIndex            # simulated sessions
    I: pd.DataFrame                   # total-return index (perf sessions, NaN before a name starts)
    C: pd.DataFrame                   # split-adjusted close (signals, pivot check)
    P: pd.DataFrame                   # raw close, ffilled (order sizing)
    below50: pd.DataFrame             # close < SMA50 on volume >= 1.4 x avg50 (bool)
    last_row: pd.Series               # last session with a price row per name
    events: dict                      # session -> list of (sid, pivot, rs_score) passing the screen
    uptrend: pd.Series                # market state at each session's close
    rank_of: dict                     # week_end -> {sid: dv50 rank}
    weeks: list


def simulate(cfg: Config, s: SimInputs, account: float | None = None) -> dict:
    account = cfg.account if account is None else account
    perf = s.perf
    cols = list(s.I.columns)
    col = {x: i for i, x in enumerate(cols)}
    I = s.I.reindex(index=perf, columns=cols).to_numpy()
    C = s.C.reindex(index=perf, columns=cols).to_numpy()
    P = s.P.reindex(index=perf, columns=cols).to_numpy()
    B50 = s.below50.reindex(index=perf, columns=cols).fillna(False).to_numpy().astype(bool)
    UP = s.uptrend.reindex(perf).fillna(False).to_numpy().astype(bool)
    lr = s.last_row.reindex(cols)
    wk = np.searchsorted(np.array(s.weeks, dtype="datetime64[ns]"), perf.values, side="right") - 1
    cash = account
    pos: dict = {}
    pend_sell: dict = {}
    pend_buy: list = []
    navs, expo, costs = np.zeros(len(perf)), np.zeros(len(perf)), np.zeros(len(perf))
    traded = 0.0
    counts = {"buy": 0, "sell": 0, "skipped_extended": 0, "delisted": 0}
    reasons: dict = {}
    trades, orders = [], []

    def hs(sid, price, i):
        week = s.weeks[wk[i]] if wk[i] >= 0 else None
        r = s.rank_of.get(week, {}).get(sid, np.nan)
        return rank_half_spread(r, price, cfg.spread_mult)

    for i, d in enumerate(perf):
        day_cost = 0.0
        for sid in [x for x in pos if pd.notna(lr[x]) and d > lr[x]]:
            p = pos.pop(sid)
            val = p["units"] * I[i, col[sid]]
            cash += val
            counts["delisted"] += 1
            trades.append({"sid": sid, "entry": perf[p["entry_i"]], "exit": d, "reason": "delisted",
                           "ret": val / p["cost_basis"] - 1, "days": i - p["entry_i"], "type": p["type"]})
            pend_sell.pop(sid, None)
        # 1. sells decided at yesterday's close
        for sid, reason in sorted(pend_sell.items()):
            if sid not in pos:
                continue
            p = pos.pop(sid)
            c = col[sid]
            val = p["units"] * I[i, c]
            k = cs.order_cost(val, P[i, c], True, hs(sid, P[i, c], i))
            cash += val - k
            day_cost += k
            traded += val
            counts["sell"] += 1
            reasons[reason] = reasons.get(reason, 0) + 1
            trades.append({"sid": sid, "entry": perf[p["entry_i"]], "exit": d, "reason": reason,
                           "ret": (val - k) / p["cost_basis"] - 1, "days": i - p["entry_i"], "type": p["type"],
                           "held_8w": bool(p.get("big") or p.get("hold_until") is not None)})
            orders.append({"date": d, "sid": sid, "side": "sell", "value": val, "reason": reason})
        pend_sell = {}
        # 2. buys decided at yesterday's close: only within 5% of the pivot at today's close
        nav_now = cash + sum(p["units"] * I[i, col[x]] for x, p in pos.items())
        for sid, pivot, typ in pend_buy:
            if len(pos) >= cfg.k or sid in pos:
                continue
            c = col[sid]
            if not (np.isfinite(I[i, c]) and np.isfinite(C[i, c])) or (pd.notna(lr[sid]) and lr[sid] < d):
                continue
            if C[i, c] > pivot * (1 + BP.buy_within):
                counts["skipped_extended"] += 1
                continue
            amt = min(nav_now / cfg.k, cash)
            if amt < 100:
                break
            k = cs.order_cost(amt, P[i, c], False, hs(sid, P[i, c], i))
            pos[sid] = {"units": (amt - k) / I[i, c], "entry_i": i, "entry_idx": I[i, c], "cost_basis": amt,
                        "hold_until": None, "big": False, "type": typ, "pivot": pivot}
            cash -= amt
            day_cost += k
            traded += amt
            counts["buy"] += 1
            orders.append({"date": d, "sid": sid, "side": "buy", "value": amt, "reason": typ, "pivot": pivot,
                           "fill_vs_pivot": C[i, c] / pivot - 1})
        pend_buy = []
        # 3. decisions at today's close
        if i < len(perf) - 1:
            for sid, p in pos.items():
                if p["entry_i"] == i:
                    continue
                c = col[sid]
                gain = I[i, c] / p["entry_idx"] - 1
                why = position_exit(p, i, gain, bool(B50[i, c]), not UP[i], cfg)
                if why is not None:
                    pend_sell[sid] = why
            slots = cfg.k - (len(pos) - len(pend_sell))
            if UP[i] and slots > 0:
                for sid, pivot, rs, typ in s.events.get(d, []):
                    if sid in pos:
                        continue
                    pend_buy.append((sid, pivot, typ))
                    if len(pend_buy) >= slots:
                        break
        stock_val = sum(p["units"] * I[i, col[x]] for x, p in pos.items())
        navs[i] = cash + stock_val
        expo[i] = stock_val / navs[i] if navs[i] > 0 else 0.0
        costs[i] = day_cost
    counts.update({f"exit_{k}": v for k, v in reasons.items()})
    return {"dates": perf, "nav": pd.Series(navs, index=perf), "exposure": pd.Series(expo, index=perf),
            "cost": pd.Series(costs, index=perf), "traded": traded, "counts": counts,
            "trades": pd.DataFrame(trades), "orders": pd.DataFrame(orders), "open": sorted(pos)}


# ======================================================================== runner

class Runner:
    def __init__(self, per: Period):
        self.per = per
        w = per.win
        self.base_cfg = Config()
        self.screen = weekly_screen(per, self.base_cfg)
        u = lv.eligible_universe(w)
        self.rank_of = {t: dict(zip(g["security_id"], g["dv50_rank"])) for t, g in u.groupby("week_end")}
        weeks = sorted(u["week_end"].unique())
        sess = w.sessions
        # daily eligibility = membership of the latest Friday on or before the session
        memb = u.assign(v=True).pivot_table(index="week_end", columns="security_id", values="v", aggfunc="any")
        memb = memb.reindex(columns=w.close_adj.columns).fillna(False).astype(bool)
        wk = np.searchsorted(np.array(memb.index, dtype="datetime64[ns]"), sess.values, side="right") - 1
        el = np.zeros((len(sess), memb.shape[1]), bool)
        el[wk >= 0] = memb.to_numpy()[wk[wk >= 0]]
        self.elig = pd.DataFrame(el, index=sess, columns=memb.columns)
        perf_first = sess[sess >= pd.Timestamp(per.spec["perf_start"])][0]
        first_signal = sess[sess < perf_first][-1]
        self.events = breakout_events(w.close_adj, w.vol_adj, w.close.where(w.close_adj.notna()),
                                      per.ixic["close"], self.elig, first_signal)
        cs.assert_window(self.events["session"], per.spec["price_start"], per.spec["effective_end"], "events")
        # RS line = stock close / Composite close; new high vs the prior 251 sessions
        rsl = w.close_adj.div(per.ixic["close"], axis=0)
        self.rsl_high = (rsl >= rsl.shift(1).rolling(251, min_periods=200).max()).fillna(False)
        sma50 = w.close_adj.rolling(50, min_periods=50).mean()
        avg50 = w.vol_adj.shift(1).rolling(50, min_periods=50).mean()
        self.below50 = (w.close_adj < sma50) & (w.vol_adj >= BP.vol_mult * avg50)
        self.perf = sess[(sess >= perf_first) & (sess <= pd.Timestamp(per.spec["effective_end"]))]
        cs.assert_window(self.perf, per.spec["perf_start"], per.spec["effective_end"], "simulation sessions")
        self.weeks = weeks
        self._m = {}
        # screen lookup: latest Friday on or before the session
        sc = self.screen[self.screen["pass_all"]]
        self.pass_by_week = {t: dict(zip(g["security_id"], g["rs_score"])) for t, g in sc.groupby("week_end")}
        self.screen_weeks = sorted(self.screen["week_end"].unique())
        ev = self.events.copy()
        sw = np.array(self.screen_weeks, dtype="datetime64[ns]")
        ev["week"] = [self.screen_weeks[k] if k >= 0 else pd.NaT
                      for k in np.searchsorted(sw, ev["session"].values, side="right") - 1]
        ev["rs_score"] = [self.pass_by_week.get(wk_, {}).get(sid, np.nan) for wk_, sid in zip(ev["week"], ev["security_id"])]
        ev["pass_screen"] = ev["rs_score"].notna()
        rh = self.rsl_high
        ev["rs_line_high"] = [bool(rh.at[d, sid]) for d, sid in zip(ev["session"], ev["security_id"])]
        tick = w.universe.drop_duplicates("security_id").set_index("security_id")["ticker"]
        ev["ticker"] = ev["security_id"].map(tick)
        self.events = ev

    def m(self, cfg: Config) -> pd.DataFrame:
        key = (cfg.dd_limit, cfg.ftd_gain)
        if key not in self._m:
            x = self.per.ixic
            self._m[key] = market_state(x["open"], x["high"], x["low"], x["close"], x["volume"], cfg.dd_limit,
                                        cfg.ftd_gain)
        return self._m[key]

    def inputs(self, cfg: Config) -> SimInputs:
        w = self.per.win
        ev = self.events[self.events["pass_screen"]]
        if cfg.rs_line:
            ev = ev[ev["rs_line_high"]]
        ev = ev.sort_values(["session", "rs_score"], ascending=[True, False])
        evd = {d: list(zip(g["security_id"], g["pivot"], g["rs_score"], g["type"])) for d, g in ev.groupby("session")}
        return SimInputs(perf=self.perf, I=w.perf_idx, C=w.close_adj, P=w.close, below50=self.below50,
                         last_row=w.last_row, events=evd, uptrend=self.m(cfg)["uptrend"], rank_of=self.rank_of,
                         weeks=self.weeks)

    def benchmarks(self, dates, account):
        q = self.per.oneq_lvl.reindex(dates)
        kc = cs.order_cost(account, float(self.per.oneq_px.reindex(dates).iloc[0]), False, ind.HALF_SPREAD["COMP"])
        oneq = (account - kc) * q / q.iloc[0]
        qqq = cs.qqq_benchmark(self.per.win.qqq_perf_idx, self.per.win.qqq_close, dates, account)
        return oneq, qqq

    def run(self, cfg: Config):
        res = simulate(cfg, self.inputs(cfg))
        oneq, qqq = self.benchmarks(res["dates"], cfg.account)
        m = ind.stock_metrics(res["nav"], oneq, qqq, cfg.account, res["cost"], res["traded"], res["exposure"])
        m.update({f"n_{x}": v for x, v in res["counts"].items()})
        tr = res["trades"]
        m.update(lv.trade_stats(tr.assign(tranches=1)) if len(tr) else {"closed_trades": 0})
        m["uptrend_share"] = float(self.m(cfg)["uptrend"].reindex(res["dates"]).mean())
        crit = ind.criteria(m["cagr"], m["max_dd"], m["bench_cagr"], m["bench_max_dd"], m["t_excess_monthly"])
        m["crit_A"], m["crit_B"], m["crit_pass"] = crit["A"], crit["B"], crit["pass"]
        return m, res, oneq, qqq


# ======================================================================== calibration / freeze / test

def calibration_grid() -> list[Config]:
    return [Config(dd_limit=a, ftd_gain=b, rs_line=c) for a in (4, 5, 6) for b in (0.0125, 0.017) for c in (False, True)]


def _neighbours(cfg: Config, cfgs: list[Config]) -> list[int]:
    return [i for i, x in enumerate(cfgs) if sum(getattr(x, f) != getattr(cfg, f) for f in CAL_FIELDS) == 1]


def plateau_pick(rows: list[dict], cfgs: list[Config], min_buys: int = 15) -> tuple[int, pd.DataFrame, str]:
    tab = []
    for i, cfg in enumerate(cfgs):
        nb = _neighbours(cfg, cfgs)
        irs = [rows[i]["ir"]] + [rows[j]["ir"] for j in nb]
        tab.append({"variant": i + 1, "config": cfg.name, "plateau_ir": float(np.nanmedian(irs)),
                    "own_ir": rows[i]["ir"], "n_buy": rows[i]["n_buy"], "n_neighbours": len(nb)})
    t = pd.DataFrame(tab)
    ok = t[t["n_buy"] >= min_buys]
    note = "among variants with >= 15 buys"
    if ok.empty:
        ok, note = t, "no variant had >= 15 buys; picked among all 12"
    best = ok.sort_values(["plateau_ir", "own_ir"], ascending=False).iloc[0]
    return int(best["variant"]), t, note


def _fmt(v):
    if isinstance(v, (np.floating, np.integer)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, np.bool_):
        return bool(v)
    return v


def _clean(m: dict) -> dict:
    return {k: _fmt(v) for k, v in m.items() if not k.startswith("_")}


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


def _freeze_block(fold: str) -> str:
    text = LEDGER.read_text()
    a = text.index(f"<!-- FREEZE-{fold.upper()}-BEGIN -->")
    b = text.index(f"<!-- FREEZE-{fold.upper()}-END -->")
    return text[a:b]


def write_frozen(fold: str, variant: int) -> None:
    cfg = calibration_grid()[variant - 1]
    p = OUT / f"frozen_{fold}.json"
    p.write_text(json.dumps({"fold": fold, "variant": variant, "config": asdict(cfg), "config_name": cfg.name,
                             "frozen_at": pd.Timestamp.now("UTC").isoformat(),
                             "ledger_freeze_sha256": hashlib.sha256(_freeze_block(fold).encode()).hexdigest()},
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
    if hashlib.sha256(_freeze_block(fold).encode()).hexdigest() != fr["ledger_freeze_sha256"]:
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
