"""Minute-bar intraday rules and the account-aware engine (intraday_minute study; docs/research_ledger_intraday_minute.md).

Conventions (pre-registered):
- A ``Day`` holds one regular-hours session of 1-minute bars (adjusted prices), bar ``i`` = minute 09:30 + i.
  Missing minutes are filled flat at the previous close with zero volume.
- A rule only looks at bars that are complete when it decides: a decision on bar ``i`` uses bars 0..i and fills
  at ``open[i + 1]``. Exceptions that are orders known in advance: a resting stop (fills at min(open, stop) for a
  long, max(open, stop) for a short, on the first bar whose low / high touches it) and the market-on-close exit
  (the close of the last bar).
- Rules return ``Trade`` records in adjusted prices, without slippage or costs. The engine (``simulate``) applies
  the raw-price factor, slippage, IBKR Tiered costs, integer shares, the account type (margin / margin with the
  PDT cap / cash with settlement) and the wrapper (stand-alone, flat overnight; or combo, long QQQ overnight).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np
import pandas as pd

from quant.backtest.costs import ibkr_order_cost

PDT_EQUITY = 25_000.0
PDT_MAX = 3            # day trades allowed in any 5 business days below PDT_EQUITY
PDT_WINDOW = 5
CASH_BUFFER = 2.0      # dollars kept back for commissions when sizing an order
MIN_BARS_SHARE = 0.95  # a session with fewer minutes present is not traded by the rules


@dataclass
class Day:
    date: str
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    vw: np.ndarray
    prev_close: float          # adjusted close of the previous session (nan on the first)
    factor: float = 1.0        # raw price = adjusted price x factor (share counts, cents, commissions)
    bars_present: int = 0
    usable: bool = True        # False: too many minutes missing (< MIN_BARS_SHARE); no rule trades that day

    @property
    def n(self) -> int:
        return len(self.c)


@dataclass
class Trade:
    side: int                  # +1 long, -1 short
    entry_i: int
    entry_px: float
    exit_i: int
    exit_px: float
    exit_kind: str             # 'close' (MOC), 'stop', 'signal', 'time'
    can_exit_intraday: bool    # ex ante: the rule may close it before the close (stop / signal / time)


def make_day(date: str, minute: np.ndarray, o, h, l, c, v, vw, n: int, prev_close: float, factor: float = 1.0) -> Day:
    """A full ``n``-bar grid from the bars present (missing minutes flat at the previous close, zero volume)."""
    cols = [np.full(n, np.nan) for _ in range(6)]
    m = np.asarray(minute)
    ok = (m >= 0) & (m < n)
    for col, src in zip(cols, (o, h, l, c, v, vw)):
        col[m[ok]] = np.asarray(src, dtype=float)[ok]
    oo, hh, ll, cc, vv, ww = cols
    last = cc[0] if not np.isnan(cc[0]) else (prev_close if prev_close == prev_close else np.nanmean(cc))
    for i in range(n):
        if np.isnan(cc[i]):
            oo[i] = hh[i] = ll[i] = cc[i] = ww[i] = last
            vv[i] = 0.0
        last = cc[i]
    return Day(date, oo, hh, ll, cc, vv, ww, float(prev_close), float(factor), int(ok.sum()),
               bool(ok.sum() >= MIN_BARS_SHARE * n))


# ======================================================================== rules

def _run_with_stop(d: Day, side: int, e: int, stop: float, kind_close: str = "close") -> tuple[int, float, str]:
    """Exit of a position entered at open[e] with a resting stop; else at the close of the last bar."""
    for j in range(e, d.n):
        if side > 0 and d.l[j] <= stop:
            return j, min(d.o[j], stop), "stop"
        if side < 0 and d.h[j] >= stop:
            return j, max(d.o[j], stop), "stop"
    return d.n - 1, d.c[-1], kind_close


def orb(d: Day, range_bars: int = 30, long_only: bool = True) -> list[Trade]:
    """M1a / M1b: opening range = first ``range_bars`` bars. First completed bar closing above the range high -> long
    at the next open (below the low -> short, long/short variant); stop at the opposite side; exit at the close.
    One trade a day."""
    n = d.n
    if n < range_bars + 3:
        return []
    hi, lo = d.h[:range_bars].max(), d.l[:range_bars].min()
    for i in range(range_bars, n - 2):
        side = 1 if d.c[i] > hi else (-1 if (d.c[i] < lo and not long_only) else 0)
        if side:
            e = i + 1
            j, px, kind = _run_with_stop(d, side, e, lo if side > 0 else hi)
            return [Trade(side, e, d.o[e], j, px, kind, True)]
    return []


def orb5_direction(d: Day) -> list[Trade]:
    """M1c (Zarattini & Aziz 2023, QQQ 5-minute ORB): the first 5-minute candle's direction (close of bar 4 vs open of
    bar 0) sets the side, entry at open[5], stop at the opposite extreme of the 5-minute range, exit at the close.
    Doji: no trade."""
    if d.n < 7:
        return []
    o0, c4 = d.o[0], d.c[4]
    if c4 == o0:
        return []
    side = 1 if c4 > o0 else -1
    stop = d.l[:5].min() if side > 0 else d.h[:5].max()
    j, px, kind = _run_with_stop(d, side, 5, stop)
    return [Trade(side, 5, d.o[5], j, px, kind, True)]


def last_half_hour(d: Day, signal: str = "rod", long_only: bool = True) -> list[Trade]:
    """M2 (intraday momentum): the sign of the return from the previous close to the signal bar's close sets the
    position held over the last 30 minutes (entry open[n-30], exit at the close). ``signal``: 'rod' = previous close
    to 15:30 (bar n-31; Baltussen et al. 2021 / the owner's spec), 'first' = previous close to 10:00 (bar 29; Gao,
    Han, Li & Zhou 2018)."""
    n = d.n
    if not d.prev_close == d.prev_close or n < 61:
        return []
    k = n - 31 if signal == "rod" else 29
    r = d.c[k] / d.prev_close - 1
    side = 1 if r > 0 else (-1 if r < 0 and not long_only else 0)
    if not side:
        return []
    e = n - 30
    # long-only on a 'rod' signal has no intraday exit; anything else may (a short is always closed the same day)
    return [Trade(side, e, d.o[e], n - 1, d.c[-1], "close", False)]


def vwap_bands(d: Day) -> tuple[np.ndarray, np.ndarray]:
    """Running VWAP from 09:30 (bar vwap x volume) and the volume-weighted standard deviation of price around it."""
    cv = np.cumsum(d.v)
    pv = np.cumsum(d.vw * d.v)
    p2v = np.cumsum(d.vw * d.vw * d.v)
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap = np.where(cv > 0, pv / cv, d.c)
        var = np.where(cv > 0, p2v / cv - vwap ** 2, 0.0)
    return vwap, np.sqrt(np.maximum(var, 0.0))


def vwap_reversion(d: Day, k: float = 2.0, long_only: bool = False, first: int = 30, last_entry_from_end: int = 61,
                   time_stop: int = 60) -> list[Trade]:
    """M3: decision bars ``first`` .. n - ``last_entry_from_end``. Close below VWAP - k sd -> long (above VWAP + k sd ->
    short, long/short variant) at the next open; exit at the next open after a close back at / through VWAP, or after
    ``time_stop`` bars, or at the close. Re-entry allowed after an exit."""
    n = d.n
    vwap, sd = vwap_bands(d)
    out, i = [], first
    while i <= n - last_entry_from_end:
        side = 0
        if sd[i] > 0:
            if d.c[i] < vwap[i] - k * sd[i]:
                side = 1
            elif d.c[i] > vwap[i] + k * sd[i] and not long_only:
                side = -1
        if not side:
            i += 1
            continue
        e = i + 1
        exit_i, px, kind = n - 1, d.c[-1], "close"
        for j in range(e, n - 1):
            back = d.c[j] >= vwap[j] if side > 0 else d.c[j] <= vwap[j]
            if back or j - e + 1 >= time_stop:
                exit_i, px, kind = j + 1, d.o[j + 1], ("signal" if back else "time")
                break
        out.append(Trade(side, e, d.o[e], exit_i, px, kind, True))
        i = exit_i if kind != "close" else n
        i = max(i, e + 1)
    return out


def noise_sigma(moves: np.ndarray, lookback: int = 14) -> np.ndarray:
    """M4: sigma[d, i] = mean over the previous ``lookback`` sessions of |close(d', i) / open(d', 0) - 1| (NaN where a
    prior session has no bar i, e.g. after an early close; NaN for the first ``lookback`` sessions)."""
    out = np.full_like(moves, np.nan)
    for t in range(lookback, len(moves)):
        with np.errstate(invalid="ignore"):
            out[t] = np.nanmean(moves[t - lookback:t], axis=0)
    return out


def noise_area(d: Day, sigma: np.ndarray, every: int = 30) -> list[Trade]:
    """M4 (Zarattini, Aziz & Barbon 2024, "Beat the Market"): bands UB = max(open, prev close) x (1 + sigma_i),
    LB = min(open, prev close) x (1 - sigma_i). Checked only on bars completing at HH:00 / HH:30 (10:00 .. 15:30):
    flat -> long above UB / short below LB; a long exits below max(UB, VWAP), a short above min(LB, VWAP) (and may
    reverse at once). Fills at the next open; flat at the close. 1x exposure (the paper's vol-target / leverage
    sizing is not used)."""
    n = d.n
    if not d.prev_close == d.prev_close or np.all(np.isnan(sigma[:n])):
        return []
    vwap, _ = vwap_bands(d)
    top, bot = max(d.o[0], d.prev_close), min(d.o[0], d.prev_close)
    out, pos, e = [], 0, -1
    for i in range(every - 1, n - 2, every):
        s = sigma[i]
        if not s == s:
            continue
        ub, lb = top * (1 + s), bot * (1 - s)
        c = d.c[i]
        new = pos
        if pos > 0 and c < max(ub, vwap[i]):
            new = 0
        elif pos < 0 and c > min(lb, vwap[i]):
            new = 0
        if new == 0:
            new = 1 if c > ub else (-1 if c < lb else 0)
        if new != pos:
            if pos:
                out.append(Trade(pos, e, d.o[e], i + 1, d.o[i + 1], "signal", True))
            if new:
                e = i + 1
            pos = new
    if pos:
        out.append(Trade(pos, e, d.o[e], n - 1, d.c[-1], "close", True))
    return out


def open_to_close(d: Day) -> list[Trade]:
    """Context only: long at the opening print (a market-on-open order), out at the close."""
    return [Trade(1, 0, d.o[0], d.n - 1, d.c[-1], "close", False)]


@dataclass(frozen=True)
class RuleSpec:
    name: str
    description: str
    fn: str
    kwargs: tuple = ()
    regime: bool = False       # M5: trade only when QQQ's previous close is above its 200-day SMA


RULES = (
    RuleSpec("M1a", "ORB 30 min, long only, stop at range low, exit at close", "orb", (("range_bars", 30), ("long_only", True))),
    RuleSpec("M1b", "ORB 30 min, long/short, stop at the opposite side, exit at close", "orb", (("range_bars", 30), ("long_only", False))),
    RuleSpec("M1c", "ORB 5 min (Zarattini-Aziz): first 5-min candle direction, stop at its other extreme", "orb5_direction"),
    RuleSpec("M2a", "last half hour, long if prev close->15:30 > 0, else flat", "last_half_hour", (("signal", "rod"), ("long_only", True))),
    RuleSpec("M2b", "last half hour, long/short on prev close->15:30", "last_half_hour", (("signal", "rod"), ("long_only", False))),
    RuleSpec("M2c", "last half hour, long/short on prev close->10:00 (Gao et al. 2018)", "last_half_hour", (("signal", "first"), ("long_only", False))),
    RuleSpec("M3a", "VWAP reversion k=2 sd, long/short, 60-min time stop", "vwap_reversion", (("k", 2.0), ("long_only", False))),
    RuleSpec("M3b", "VWAP reversion k=2 sd, long only, 60-min time stop", "vwap_reversion", (("k", 2.0), ("long_only", True))),
    RuleSpec("M4", "noise-area band (Zarattini-Aziz-Barbon), long/short, 30-min checks, VWAP trailing stop", "noise_area"),
    RuleSpec("M5", "M1a only when QQQ's previous close > its 200-day SMA", "orb", (("range_bars", 30), ("long_only", True)), True),
)
CONTEXT = (RuleSpec("CTX_open_to_close", "context: long open->close every day", "open_to_close"),
           RuleSpec("CTX_none", "context: no intraday trade (combo = QQQ overnight only)", "none"))


def rule_trades(spec: RuleSpec, d: Day, sigma: np.ndarray | None = None, regime_on: bool = True) -> list[Trade]:
    if spec.fn == "none" or (spec.regime and not regime_on) or not d.usable:
        return []
    kw = dict(spec.kwargs)
    if spec.fn == "noise_area":
        return noise_area(d, sigma)
    return globals()[spec.fn](d, **kw)


def regime_flags(adj_close: pd.Series, length: int = 200) -> pd.Series:
    """True on session t when the close of t-1 is above the SMA(length) of closes up to t-1."""
    ma = adj_close.rolling(length, min_periods=length).mean()
    return (adj_close > ma).where(ma.notna(), False).shift(1, fill_value=False).astype(bool)


# ======================================================================== engine

@dataclass(frozen=True)
class Account:
    kind: str = "margin"       # 'margin' or 'cash'
    pdt: bool = False          # margin: cap day trades at 3 per 5 sessions while equity < $25k


def settle_lag(date: str) -> int:
    """Sessions until a sale settles: T+2 before 2024-05-28, T+1 from then (US equities)."""
    return 1 if date >= "2024-05-28" else 2


def slippage(px_raw: float, mult: float) -> float:
    """Per-share slippage: the larger of 1 cent and 1 bp of the price, times ``mult``."""
    return mult * max(0.01, 1e-4 * px_raw)


def order_cost(shares: int, px_raw: float, sell: bool, on: bool = True) -> float:
    return ibkr_order_cost(shares, px_raw, sell, 0.0)["total"] if (shares and on) else 0.0


class Cash:
    """Cash in lots by settlement session; ``spend(amount, by)`` uses lots settled by session ``by`` first."""

    def __init__(self, amount: float):
        self.lots: list[list[float]] = [[-1, float(amount)]]

    def total(self) -> float:
        return float(sum(a for _, a in self.lots))

    def available(self, by: float) -> float:
        return float(sum(a for s, a in self.lots if s <= by))

    def add(self, amount: float, settle: float) -> None:
        self.lots.append([settle, float(amount)])

    def spend(self, amount: float, by: float) -> None:
        left = float(amount)
        for lot in sorted((x for x in self.lots if x[0] <= by), key=lambda x: x[0]):
            take = min(lot[1], left)
            lot[1] -= take
            left -= take
            if left <= 1e-9:
                break
        if left > 1e-6:          # costs beyond the eligible cash: charge the earliest lot (rounding only)
            self.lots[0][1] -= left
        self.lots = [x for x in self.lots if abs(x[1]) > 1e-9] or [[-1, 0.0]]

    def merge_settled(self, k: int) -> None:
        s = sum(a for t, a in self.lots if t <= k)
        self.lots = [[-1, s]] + [x for x in self.lots if x[0] > k]


@dataclass
class SimResult:
    ret: pd.Series
    equity: pd.Series
    trades: pd.DataFrame
    stats: dict = field(default_factory=dict)


def simulate(days: list[Day], trades: list[list[Trade]], rf: np.ndarray, *, account: Account = Account(),
             combo: bool = False, slip_mult: float = 1.0, start_equity: float = 10_000.0,
             start: int = 1, costs: bool = True) -> SimResult:
    """Run the trades of each day through the account. ``rf``: daily T-bill return per day (credited on the cash
    held over each night). ``combo``: hold QQQ (the same instrument) from each close to the next open: sold at the
    opening print, bought back at the close (MOC); a long rule trade that is still open at the close is simply kept.
    Returns are recorded from day ``start`` (day ``start - 1`` only buys the first overnight position in combo).
    ``costs=False``: no commissions or fees (pass ``slip_mult=0`` as well for a fully gross run)."""
    cash = Cash(start_equity)
    is_cash = account.kind == "cash"
    shares_on, on_px_adj, f_prev = 0, float("nan"), 1.0      # overnight position (combo), bought at adjusted close on_px_adj
    eq_prev = start_equity
    dt_hist: list[int] = []
    rows, rets, eqs = [], [], []
    st = {"skipped_short_cash": 0, "skipped_pdt": 0, "skipped_no_cash": 0, "day_trades": 0, "pdt_binding_days": 0,
          "overnight_not_bought": 0, "cost_dollars": 0.0, "slippage_dollars": 0.0, "minutes_in_market": 0,
          "minutes_total": 0, "orders": 0}
    lag = 0
    for k, (d, day_trades) in enumerate(zip(days, trades)):
        if k < start - 1:
            continue
        lag = settle_lag(d.date)
        f = d.factor
        cash.merge_settled(k)
        by_intraday = math.inf if not is_cash else k
        by_overnight = math.inf if not is_cash else k + 1
        st["minutes_total"] += d.n if k >= start else 0
        # ---- morning: sell the overnight position at the opening print
        if shares_on:
            gross = shares_on * f_prev * d.o[0]     # prev raw close x adjusted open / adjusted close: dividends included
            px_raw = d.o[0] * f
            s = slippage(px_raw, slip_mult)
            cst = order_cost(shares_on, px_raw, True, costs)
            cash.add(gross - shares_on * s - cst, k + lag)
            st["cost_dollars"] += cst
            st["slippage_dollars"] += shares_on * s
            st["orders"] += 1
            shares_on = 0
        dt_today = 0
        carry = 0                     # shares of a long rule trade carried into the overnight leg (combo)
        allow_trading = k >= start
        for t in (day_trades if allow_trading else []):
            if t.side < 0 and is_cash:
                st["skipped_short_cash"] += 1
                continue
            carried = combo and t.side > 0 and t.exit_kind == "close"
            # ex ante: could this become a day trade? Stand-alone: always. Combo: a short, or a long the rule may
            # close before the close (a long held to the close is carried overnight, not a day trade).
            maybe_dt = (not combo) or t.side < 0 or t.can_exit_intraday
            if account.pdt and eq_prev < PDT_EQUITY and maybe_dt:
                if sum(dt_hist[-(PDT_WINDOW - 1):]) + dt_today >= PDT_MAX:
                    st["skipped_pdt"] += 1
                    continue
            px_in = t.entry_px * f
            s_in = slippage(px_in, slip_mult)
            fill_in = px_in + s_in if t.side > 0 else px_in - s_in
            avail = cash.available(by_intraday) if t.side > 0 else cash.total()
            n_sh = int(max(avail - CASH_BUFFER, 0) // fill_in)
            if n_sh <= 0:
                st["skipped_no_cash"] += 1
                continue
            c_in = order_cost(n_sh, px_in, t.side < 0, costs)
            st["orders"] += 1
            if carried:
                cash.spend(n_sh * fill_in + c_in, by_intraday)
                carry = n_sh
                pnl_ex = 0.0
                c_out = 0.0
                s_out = 0.0
                fill_out = t.exit_px * f
            else:
                px_out = t.exit_px * f
                s_out = slippage(px_out, slip_mult)
                fill_out = px_out - s_out if t.side > 0 else px_out + s_out
                c_out = order_cost(n_sh, px_out, t.side > 0, costs)
                st["orders"] += 1
                if t.side > 0:
                    cash.spend(n_sh * fill_in + c_in, by_intraday)
                    cash.add(n_sh * fill_out - c_out, k + lag)
                else:                  # short (margin only): P&L settles like a sale
                    pnl = n_sh * (fill_in - fill_out) - c_in - c_out
                    cash.add(pnl, k + lag) if pnl >= 0 else cash.spend(-pnl, math.inf)
                dt_today += 1
                st["day_trades"] += 1
            st["cost_dollars"] += c_in + c_out
            st["slippage_dollars"] += n_sh * (s_in + s_out)
            held_to = d.n if t.exit_kind == "close" else t.exit_i
            st["minutes_in_market"] += max(held_to - t.entry_i, 1)
            notional = n_sh * fill_in
            gross = t.side * (t.exit_px / t.entry_px - 1)
            net = (t.side * n_sh * (fill_out - fill_in) - c_in - c_out) / notional
            rows.append({"date": d.date, "side": t.side, "entry_i": t.entry_i, "exit_i": t.exit_i, "kind": t.exit_kind,
                         "shares": n_sh, "notional": notional, "gross_ret": gross, "net_ret": net, "carried": carried})
        if account.pdt and eq_prev < PDT_EQUITY and allow_trading:
            if sum(dt_hist[-(PDT_WINDOW - 1):]) + dt_today >= PDT_MAX:
                st["pdt_binding_days"] += 1
        dt_hist.append(dt_today)
        # ---- close: buy the overnight position (combo)
        close_raw = d.c[-1] * f
        if combo:
            s = slippage(close_raw, slip_mult)
            avail = cash.available(by_overnight)
            target = carry + int(max(avail - CASH_BUFFER, 0) // (close_raw + s))
            add = target - carry
            if add > 0:
                cst = order_cost(add, close_raw, False, costs)
                cash.spend(add * (close_raw + s) + cst, by_overnight)
                st["cost_dollars"] += cst
                st["slippage_dollars"] += add * s
                st["orders"] += 1
            elif carry == 0:
                st["overnight_not_bought"] += 1
            shares_on = max(target, carry)
            on_px_adj = d.c[-1]
            f_prev = f
        # ---- interest on cash held overnight
        ctot = cash.total()
        if ctot > 0 and rf is not None:
            cash.add(ctot * float(rf[k]), -1)
        eq = cash.total() + shares_on * close_raw
        if k >= start:
            rets.append((d.date, eq / eq_prev - 1))
            eqs.append((d.date, eq))
        eq_prev = eq
    idx = pd.DatetimeIndex([a for a, _ in rets])
    st["final_equity"] = float(eq_prev)
    return SimResult(pd.Series([b for _, b in rets], index=idx), pd.Series([b for _, b in eqs], index=idx),
                     pd.DataFrame(rows), st)
