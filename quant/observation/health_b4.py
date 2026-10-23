"""Daily self-checks of the B4 lines (docs/forward_observation_checklist.md B4; added 2026-10-24).

Run by ``quant.observation.daily`` after the B1-B3 checks of ``quant.observation.health`` (which are unchanged), each
returning ok / warn / fail:

- ``b4_pipeline``: every B4 line was computed (a line that raised is named), the B4 part of the log was rendered,
  and the B4 fetches completed (a vendor that failed, a mega-cap signal computed without its real-close check: a
  warning; the files of the previous run are used). The B4 checks themselves never stop the B1-B3 record: an error in
  them is this check's failure (``daily``).
- ``b4_data_freshness``: QLD and IBIT end at the last completed NYSE session (the B1 / B2 rules of
  ``health.check_freshness``: a bar missing only for the session just closed is a warning on an early run or before
  20:00 New York); a held mega-cap name ends there too, judged with its second vendor: one session short on an early
  run is a warning; the second vendor has later bars: failure; 1-2 sessions short without a second vendor
  (``HELD_STALE_FAIL``): failure; both vendors stop, or more than 2 sessions short: a warning (delisted or halted:
  the frozen rule holds it as cash at its last value). BTC's last complete UTC day is the session (its candle closes
  at 00:00 UTC, so it is expected only once that has passed; one day short is a warning on an early run; a BTC load
  error is named). The T-bill file (FRED DTB3) at most 4 sessions behind (warning) or 8 (failure). Every started line
  ends at the data end unless it waits for a signal file or a held name's bar (named); on an early run a line one
  session short (BT4b: one BTC day) waits for a late bar: a warning.
- ``b4_data_integrity``: the same session / positive-price / day-move tests as ``health.integrity_items`` (bounds:
  QLD and IBIT |ln| 0.50, mega-cap stocks 0.50) on the forward window; QLD's day return vs 2 x QQQ's (a 2x daily
  fund: a warning above 1.5 points, a failure above 3; 2011-2026 max 2.1) and IBIT's session return vs BTC's UTC-close
  return of the same session (a failure above 15 points; 2024-2026 max 7.3), so a wrong bar fails even when a second
  vendor carries it too; BTC: no missing UTC day, positive closes, day moves within |ln| 0.60; Bitstamp zero-volume
  days filled from another source are named (warning). A reviewed item is cleared with ``--accept-data`` (its
  ``symbol date`` key).
- ``b4_vendor_agreement``: QLD, IBIT and the held mega-cap charts vs a second vendor (``health.check_vendors``:
  closes within 0.2%, day total returns within 1%); BTC Bitstamp vs Coinbase (else Kraken, Yahoo) on every UTC day of
  the window: closes within 1%, day returns within 1 point (warning from 0.5; ``--accept-data`` clears a reviewed
  ``BTC date``). A symbol without a second vendor is a warning; only when no symbol has one is it a failure on the
  evening's last run (as B1 / B2).
- ``b4_rules``: an independent recompute from the stored files with the registered values transcribed below (not
  imported): LEV-B3's SMA200 state with the 2% band (each decision from the opening close on), V3's
  e = min(1.5, sigma_LR / sigma_20) at each month-end decision, the held exposure of both against the target in force
  (within 1e-6 on the first day after a trade, ``POST_TRADE_TOL``; within 0.12 on the drift days between trades),
  BT4b's 10% BTC weight right after every rebalance, and the mega-cap top-N ranking re-derived from each frozen
  signal file (M1 top 10 equal, M2 top 5, M3 best 5 of the top 20 by 126-session momentum, M6 top 10 by market-cap
  ratio; no ticker or chart symbol twice) against the file's weights and the targets the runner simulated.
- ``b4_signal``: from the session after each month-end signal session the frozen mega-cap signal file
  ``state/forward_observation/megacap/m_<date>.csv`` and its meta exist, are well formed, and never change once
  recorded (hash of both in the replay state; the first recorded hash is kept, so a changed file fails until it is
  restored from Git).

The replay drift of the B4 numbers and the B4 rows of the log status are part of ``health.check_replay`` /
``check_log_status`` (fed by ``replay_inputs`` and ``b4.line_ends``). Nothing here changes a rule or a number of the
log. health.json holds statuses, dates, symbols and counts only.
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from quant.observation import health as hc

OK, WARN, FAIL = hc.OK, hc.WARN, hc.FAIL

# ======================================================================== frozen values (transcribed 2026-10-24)
# From docs/forward_observation_checklist.md B4 and the ledgers it cites; not imported from quant.strategies.

START_CLOSE = "2026-10-26"                 # LEV-B3 / V3 open at this close
FIRST_SIGNAL = "2026-10-30"                # mega-cap lines and BT4b: first month-end signal
LEV_ON, LEV_OFF, LEV_SMA, LEV_BAND = 1.5, 1.0, 200, 0.02
V3_CAP, V3_SHORT, V3_LR_MIN = 1.5, 20, 252
BT_SLEEVE = 0.10
M_RULES = {"M1": {"pool": 10, "hold": 10, "mom": None, "capw": False},
           "M2": {"pool": 5, "hold": 5, "mom": None, "capw": False},
           "M3": {"pool": 20, "hold": 5, "mom": "mom_m6", "capw": False},
           "M6": {"pool": 10, "hold": 10, "mom": None, "capw": True}}
TRADING_DAYS = 252

# check thresholds (fixed 2026-10-24)
DAY_MOVE_BOUND = {"QLD": 0.50, "IBIT": 0.50, "stock": 0.50, "btc": 0.60}
EXPOSURE_TOL = 0.12                        # held exposure vs the target in force (drift between trades)
POST_TRADE_TOL = 1e-6                      # held exposure on the first day after a trade (as SLEEVE_TOL)
QLD_TRACK_WARN, QLD_TRACK_FAIL = 0.015, 0.03   # |QLD day return - 2 x QQQ day return| (2011-2026: max 2.1 points)
IBIT_TRACK_FAIL = 0.15                     # |IBIT session return - BTC UTC-close return| (2024-2026: max 7.3 points)
HELD_STALE_FAIL = 2                        # a held mega-cap 1-2 sessions behind without a second vendor: fail
SLEEVE_TOL = 1e-6                          # BTC weight right after a rebalance (costs scale both legs alike)
E_TOL = 1e-9                               # V3 target exposure: the runner vs the recompute
WEIGHT_TOL = 1e-9
BTC_CLOSE_TOL, BTC_TR_WARN, BTC_TR_FAIL = 0.01, 0.005, 0.01
DTB3_WARN, DTB3_FAIL = 4, 8
BTC_FP_FROM = "2026-10-01"


def _d(x) -> str:
    return str(pd.Timestamp(x).date())


def _status(fails, warns) -> str:
    return FAIL if fails else WARN if warns else OK


# ======================================================================== pipeline


def check_pipeline(b4res: dict | None, notes=()) -> hc.Check:
    if not b4res:
        return hc.Check("b4_pipeline", FAIL, "the B4 lines were not computed (the runner did not complete)")
    errs = [f"{k}: {v['error'].splitlines()[0][:200]}" for k, v in b4res["lines"].items() if v.get("error")]
    if b4res.get("render_error"):
        errs.append(f"log: {b4res['render_error'][:200]}")
    if errs:
        return hc.Check("b4_pipeline", FAIL, f"{len(errs)} B4 line(s) or the B4 log part not computed",
                        errs + list(notes))
    if notes:
        return hc.Check("b4_pipeline", WARN, f"B4 lines computed; {len(notes)} fetch note(s)", list(notes))
    return hc.Check("b4_pipeline", OK, "B4 fetches and lines completed")


# ======================================================================== freshness


def btc_expected(target, now: datetime) -> pd.Timestamp:
    """The last UTC day whose BTC candle should be complete for this session's run."""
    target = pd.Timestamp(target)
    now = pd.Timestamp(now)
    now = now.tz_convert("UTC") if now.tzinfo else now.tz_localize("UTC")
    done = pd.Timestamp(now.date()) - pd.Timedelta(days=1)           # yesterday (UTC) is the last closed candle
    return min(target, done)


def check_freshness(frames: dict, target, calendar: pd.DatetimeIndex, btc_last, rf_last, now: datetime,
                    grace: bool = False, line_end: dict | None = None, data_end=None, waiting=(),
                    held: dict | None = None, held_other: dict | None = None, btc_note: str | None = None) -> hc.Check:
    """``frames`` {symbol: frame or None}: QLD and IBIT; ``held`` {ticker: frame or None}: the held mega-cap names,
    ``held_other`` {ticker: (last bar of a second vendor or None, every vendor asked has no data)}; ``btc_last`` the
    last complete UTC day of the BTC series (None: no BTC data; ``btc_note`` the load error, if any); ``rf_last`` the
    last DTB3 date; ``line_end`` {line: last session}; ``waiting`` the lines that stop for a known reason (a pending
    signal file or a held name's bar)."""
    base = hc.check_freshness(frames, target, calendar, None, None, None, grace) if frames else \
        hc.Check("data_freshness", OK, "")
    items = list(base.items)
    fails, warns = base.status == FAIL, base.status == WARN
    target = pd.Timestamp(target)
    exp = btc_expected(target, now)
    if btc_last is None:
        items.append(f"BTC: not loaded ({btc_note.splitlines()[0][:160]})" if btc_note else "BTC: no data")
        fails = True
    elif pd.Timestamp(btc_last) < exp:
        late = (exp - pd.Timestamp(btc_last)).days
        msg = f"BTC: last complete UTC day {_d(btc_last)}, expected {_d(exp)}"
        if grace and late == 1:
            items.append(msg + " (the evening's last run fails if still missing)")
            warns = True
        else:
            items.append(msg)
            fails = True
    if rf_last is None:
        items.append("T-bill (DTB3): no file")
        fails = True
    else:
        n = len(calendar[(calendar > pd.Timestamp(rf_last)) & (calendar <= target)])
        if n > DTB3_WARN:
            items.append(f"T-bill (DTB3) ends {_d(rf_last)}, {n} sessions behind")
            fails = fails or n > DTB3_FAIL
            warns = True
    prev = calendar[calendar < target]
    prev = prev[-1] if len(prev) else None
    for tick, df in (held or {}).items():
        last = None if df is None else df.index[df.index <= target].max()
        if last is None or pd.isna(last):
            items.append(f"mega-cap {tick}: no readable chart")
            fails = True
            continue
        if last == target:
            continue
        n = len(calendar[(calendar > last) & (calendar <= target)])
        other, gone = (held_other or {}).get(tick, (None, False))
        if grace and last == prev:
            items.append(f"mega-cap {tick}: last bar {_d(last)}, expected {_d(target)} (not published yet? the "
                         "evening's last run fails if still missing)")
            warns = True
        elif other is not None and pd.Timestamp(other) > last:
            items.append(f"mega-cap {tick}: last bar {_d(last)}, the second vendor has bars through {_d(other)}")
            fails = True
        elif other is not None or gone or n > HELD_STALE_FAIL:
            items.append(f"mega-cap {tick}: last bar {_d(last)}, {n} session(s) behind"
                         + (", and the second vendor " + ("has no data for it" if other is None else
                                                          f"also stops at {_d(other)}") if other is not None or gone
                            else "") + ": delisted or halted? (the frozen rule holds it as cash at its last value)")
            warns = True
        else:
            items.append(f"mega-cap {tick}: last bar {_d(last)}, expected {_d(target)} ({n} session(s) behind; no "
                         "second vendor to tell a vendor delay from a delisting)")
            fails = True
    end = pd.Timestamp(data_end) if data_end is not None else target
    before_end = calendar[calendar < end]
    for k, last in (line_end or {}).items():
        last = pd.Timestamp(last)
        if last >= end or k in waiting:
            continue
        if k == "BT4b" and btc_last is not None and last >= hc.session_on_or_before(exp, calendar):
            continue                                       # waits for the BTC candle of the session
        msg = f"{k}: line ends {_d(last)}, the data end at {_d(end)}"
        late_ok = (last >= hc.session_on_or_before(exp - pd.Timedelta(days=1), calendar) if k == "BT4b"
                   else len(before_end) and last >= before_end[-1])
        if grace and late_ok:                              # one session short on an early run: a late bar
            items.append(msg + " (waits for a late bar; the evening's last run fails if still missing)")
            warns = True
        else:
            items.append(msg)
            fails = True
    status = FAIL if fails else WARN if warns else OK
    n = len(frames) + len(held or {}) + 2
    msg = (f"{n} B4 series fresh for the session {_d(target)} (BTC through {_d(btc_last)})" if status == OK
           else f"{len(items)} problem(s) against the session {_d(target)}")
    return hc.Check("b4_data_freshness", status, msg, items)


# ======================================================================== integrity


def btc_items(btc, start, end) -> tuple[list, list]:
    """(failures, warnings) of the BTC primary series on [start, end]."""
    if btc is None:
        return [], []
    p = btc.close[(btc.close.index >= pd.Timestamp(start)) & (btc.close.index <= pd.Timestamp(end))]
    fails, warns = [], []
    if p.empty:
        return [f"BTC: no closes in {_d(start)}..{_d(end)}"], []
    full = pd.date_range(p.index[0], p.index[-1], freq="D")
    miss = full.difference(p.index)
    if len(miss):
        fails.append(f"BTC: {len(miss)} missing UTC day(s): {', '.join(_d(d) for d in miss[:5])}")
    if (p <= 0).any():
        fails.append(f"BTC: non-positive close on {', '.join(_d(d) for d in p.index[p <= 0][:5])}")
    with np.errstate(divide="ignore", invalid="ignore"):
        lm = np.log(p / p.shift(1)).dropna()
    for d, x in lm[lm.abs() > DAY_MOVE_BOUND["btc"]].items():
        fails.append(f"BTC {_d(d)}: day move {math.exp(x) - 1:+.2%} beyond the frozen bound")
    filled = {d: s for d, s in (btc.info.get("filled_days") or {}).items() if pd.Timestamp(d) >= pd.Timestamp(start)}
    warns += [f"BTC {d}: Bitstamp had no trades, close taken from {s} (the registered fill rule)"
              for d, s in sorted(filled.items())]
    return fails, warns


def track_items(frames: dict, qqq: pd.DataFrame | None, btc, start, target) -> tuple[list, list]:
    """QLD's day return vs 2 x QQQ's (a 2x daily fund; 2011-2026 max gap 2.1 points) and IBIT's session return vs the
    BTC UTC-close return of the same session (max 7.3 points): an unexplained jump in one file fails here even when a
    second vendor carries it too."""
    fails, warns = [], []
    a, b = pd.Timestamp(start), pd.Timestamp(target)
    q = qqq["adjclose"] if qqq is not None else None
    if q is not None and frames.get("QLD") is not None:
        lq = frames["QLD"]["adjclose"]
        j = pd.concat([lq / lq.shift(1) - 1, q / q.shift(1) - 1], axis=1, keys=["l", "q"]).dropna()
        j = j[(j.index >= a) & (j.index <= b)]
        for d, r in j.iterrows():
            gap = abs(r["l"] - 2 * r["q"])
            msg = (f"QLD {_d(d)}: day return {r['l']:+.2%} vs 2 x QQQ {2 * r['q']:+.2%}, {gap * 100:.2f} points apart")
            if gap > QLD_TRACK_FAIL:
                fails.append(msg + " (a 2x daily fund cannot do that: a wrong bar)")
            elif gap > QLD_TRACK_WARN:
                warns.append(msg)
    if btc is not None and frames.get("IBIT") is not None:
        ib = frames["IBIT"]["adjclose"]
        ses = ib.index[(ib.index >= btc.close.index[0]) & (ib.index <= btc.close.index[-1])]
        bs = btc.close.reindex(ses)
        j = pd.concat([ib.reindex(ses).pct_change(), bs.pct_change()], axis=1, keys=["i", "b"]).dropna()
        j = j[(j.index >= a) & (j.index <= b)]
        for d, r in j.iterrows():
            if abs(r["i"] - r["b"]) > IBIT_TRACK_FAIL:
                fails.append(f"IBIT {_d(d)}: day return {r['i']:+.2%} vs BTC {r['b']:+.2%} (UTC close), "
                             f"{abs(r['i'] - r['b']) * 100:.1f} points apart (a wrong bar)")
    return fails, warns


_DAY_KEY = re.compile(r"(\S+ \d{4}-\d{2}-\d{2}): (?:day move|close \+ dividend|day return)")


def check_integrity(frames: dict, target, calendar: pd.DatetimeIndex, btc=None, sources: dict | None = None,
                    start=None, accepted=(), held: dict | None = None, qqq: pd.DataFrame | None = None) -> hc.Check:
    """``frames``: QLD / IBIT from ``start`` (the integrity window of the B1 / B2 series); ``held``: the mega-cap
    names held now, from the first mega-cap signal (they enter the simulation only from there)."""
    start = pd.Timestamp(start) if start is not None else (pd.Timestamp(START_CLOSE)
                                                           - pd.Timedelta(days=hc.INTEGRITY_FROM_DAYS))
    fails, warns = [], []
    series = [(s, df, start) for s, df in frames.items()] + [(s, df, pd.Timestamp(FIRST_SIGNAL))
                                                             for s, df in (held or {}).items()]
    for sym, df, a in series:
        if df is None:
            continue                                       # reported by b4_data_freshness
        bound = DAY_MOVE_BOUND.get(sym, DAY_MOVE_BOUND["stock"])
        f, w = hc.integrity_items(sym, df, a, target, calendar, bound,
                                  self_tr=(sources or {}).get(sym) not in ("alpaca", None))
        fails += f
        warns += w
    f, w = btc_items(btc, start, target)
    fails += f
    warns += w
    f, w = track_items(frames, qqq, btc, start, target)
    fails += f
    warns += w
    acc = [x for x in fails if any(x.startswith(a + ":") for a in accepted)]
    fails = [x for x in fails if x not in acc]
    keys = sorted({m.group(1) for x in fails if (m := _DAY_KEY.match(x))})
    status = _status(fails, warns)
    msg = (f"{len(frames) + len(held or {}) + (btc is not None)} B4 series clean to {_d(target)} (QLD / IBIT from "
           f"{_d(start)}, held mega-caps from {FIRST_SIGNAL})" if status == OK
           else f"{len(fails)} failure(s), {len(warns)} warning(s)")
    return hc.Check("b4_data_integrity", status, msg, fails + warns + [f"{x} (accepted earlier)" for x in acc],
                    {"keys": keys})


# ======================================================================== vendor agreement


def btc_vendor_items(btc, start, end, strict: bool, accepted=()) -> tuple[list, list, str]:
    """Bitstamp (the stored primary) vs the first other source with data: closes and day returns."""
    if btc is None:
        return [], [], "no BTC data"
    close = btc.sources.close
    prim = btc.close[(btc.close.index >= pd.Timestamp(start)) & (btc.close.index <= pd.Timestamp(end))]
    for other in ("coinbase", "kraken", "yahoo"):
        if other in close and close[other].reindex(prim.index).notna().sum() >= min(5, len(prim)):
            break
    else:
        msg = "BTC: no second source to compare with"
        return ([msg], [], "none") if strict else ([], [msg], "none")
    o = close[other].reindex(prim.index)
    j = pd.concat([prim, o], axis=1, keys=["p", "o"]).dropna()
    fails, warns = [], []
    rel = (j["p"] / j["o"] - 1).abs()
    rd = ((j["p"] / j["p"].shift(1)) - (j["o"] / j["o"].shift(1))).abs()
    for d in j.index:
        if f"BTC {_d(d)}" in set(accepted or ()):
            continue                                       # reviewed with --accept-data
        if rel[d] > BTC_CLOSE_TOL:
            fails.append(f"BTC {_d(d)}: Bitstamp close differs from {other} by {rel[d]:.2%}")
        elif np.isfinite(rd[d]) and rd[d] > BTC_TR_FAIL:
            fails.append(f"BTC {_d(d)}: day return differs from {other} by {rd[d] * 100:.2f} points")
        elif np.isfinite(rd[d]) and rd[d] > BTC_TR_WARN:
            warns.append(f"BTC {_d(d)}: day return differs from {other} by {rd[d] * 100:.2f} points")
    return fails, warns, f"{other} ({len(j)} days)"


def check_vendors(primary: dict, others: dict, target, calendar: pd.DatetimeIndex, sources: dict, notes=(),
                  btc=None, strict: bool = True, accepted=(), start=None) -> hc.Check:
    start = pd.Timestamp(start) if start is not None else pd.Timestamp(START_CLOSE) - pd.Timedelta(
        days=hc.FINGERPRINT_FROM_DAYS)
    window = {s: list(calendar[(calendar >= start) & (calendar <= pd.Timestamp(target))]) for s in primary}
    base = hc.check_vendors(primary, others, target, calendar, sources, notes, extra_days=window,
                            confirm=hc.large_moves(primary, target, calendar), strict=strict, accepted=accepted,
                            must=tuple(primary)) if primary else hc.Check("vendor_agreement", OK, "")
    f, w, used = btc_vendor_items(btc, max(start, pd.Timestamp(BTC_FP_FROM) - pd.Timedelta(days=30)),
                                  target, strict, accepted)
    items = f + w + list(base.items)
    status = hc.worst([base.status, _status(f, w)])
    head = base.message.replace(f", B3 shorts {hc.VENDOR_TOL_B3:.1%}", "")   # (no B3 short among the B4 series)
    msg = f"{head}; BTC vs {used}" + (f"; {len(f)} BTC failure(s), {len(w)} BTC warning(s)" if f or w else "")
    keys = list(base.data.get("keys", [])) + sorted({x.split(":")[0] for x in f if x.startswith("BTC 2")})
    return hc.Check("b4_vendor_agreement", status, msg, items, {**base.data, "keys": keys})


# ======================================================================== independent rules


def sma_state(adj: pd.Series, n: int = LEV_SMA, band: float = LEV_BAND) -> tuple[pd.Series, pd.Series]:
    """(state 1 / 0 / NaN, distance to the nearer switch level as a fraction) from the adjusted closes: on above
    (1 + band) x SMA, off below (1 - band) x SMA, unchanged in between; the first state is close > SMA."""
    v = adj.to_numpy(float)
    out, near = np.full(len(v), np.nan), np.full(len(v), np.nan)
    state = np.nan
    run = []
    for i, x in enumerate(v):
        run.append(x)
        if len(run) > n:
            run.pop(0)
        if len(run) < n or not np.all(np.isfinite(run)):
            continue
        m = sum(run) / n
        if np.isnan(state):
            state = 1.0 if x > m else 0.0
        if x > m * (1 + band):
            state = 1.0
        elif x < m * (1 - band):
            state = 0.0
        out[i] = state
        near[i] = min(abs(x / m - 1 - band), abs(x / m - 1 + band))
    return pd.Series(out, index=adj.index), pd.Series(near, index=adj.index)


def v3_exposure(adj: pd.Series, days) -> dict:
    """{day: min(cap, sd(all returns since the first) / sd(last 20)) } (sample sd; >= 252 returns)."""
    r = (adj / adj.shift(1) - 1).dropna()
    out = {}
    for d in days:
        x = r[r.index <= pd.Timestamp(d)].to_numpy(float)
        if len(x) < V3_LR_MIN:
            out[pd.Timestamp(d)] = np.nan
            continue
        lr, sh = np.std(x, ddof=1), np.std(x[-V3_SHORT:], ddof=1)
        out[pd.Timestamp(d)] = min(V3_CAP, lr / sh)
    return out


def _month_end_days(ses: pd.DatetimeIndex) -> list:
    per = ses.to_period("M")
    return [ses[i] for i in range(len(ses) - 1) if per[i] != per[i + 1]]


def exposure_items(name: str, held_e: pd.Series, target_e: pd.Series, sessions: pd.DatetimeIndex,
                   trades=()) -> list:
    """The held exposure of each return day vs the target decided two sessions earlier (traded at the previous
    close): within POST_TRADE_TOL on the first day after a trade (decision days ``trades``), EXPOSURE_TOL (drift)
    on the other days."""
    out = []
    trades = set(pd.DatetimeIndex(list(trades)))
    pos = {d: i for i, d in enumerate(sessions)}
    for d, x in held_e.items():
        i = pos.get(d)
        if i is None or i < 2:
            continue
        want = target_e.get(sessions[i - 2])
        if want is None or not np.isfinite(want):
            continue
        tol = POST_TRADE_TOL if sessions[i - 2] in trades else EXPOSURE_TOL
        if abs(x - want) > tol:
            out.append(f"{name} {_d(d)}: held exposure {x:.6f}, the target in force {want:.6f}"
                       + (" (the day after a trade)" if tol == POST_TRADE_TOL else ""))
    return out[:5] + ([f"{name}: ... {len(out) - 5} more day(s)"] if len(out) > 5 else [])


def m_targets(sc: pd.DataFrame, rule: str) -> list:
    """[(security_id, weight)] of one mega-cap rule re-derived from a frozen signal file."""
    p = M_RULES[rule]
    g = sc[np.isfinite(pd.to_numeric(sc["mcap_rank"], errors="coerce"))].sort_values("mcap_rank").head(p["pool"])
    if len(g) < p["pool"]:
        return []
    if p["mom"]:
        m = pd.to_numeric(g[p["mom"]], errors="coerce")
        g = g[np.isfinite(m)].assign(_m=m[np.isfinite(m)])
        g = g.sort_values(["_m", "security_id"], ascending=[False, True]).head(p["hold"])
        if len(g) < p["hold"]:
            return []
    if p["capw"]:
        rel = pd.to_numeric(g["mcap_rel"], errors="coerce").to_numpy(float)
        return list(zip(g["security_id"], rel / rel.sum()))
    return [(sid, 1.0 / len(g)) for sid in g["security_id"]]


def signal_items(name: str, sc: pd.DataFrame) -> list:
    """Form of one frozen mega-cap signal file and its weights vs the re-derived ranking."""
    out = []
    need = {"s", "security_id", "ticker", "mcap_rank", "mcap_rel", "mom_m6", "company",
            *[f"w_{k}" for k in M_RULES]}
    if not need <= set(sc.columns):
        return [f"{name}: missing columns {sorted(need - set(sc.columns))}"]
    rk = pd.to_numeric(sc["mcap_rank"], errors="coerce")
    if list(rk.sort_values()) != list(range(1, len(sc) + 1)):
        out.append(f"{name}: market-cap ranks are not 1..{len(sc)}")
    if sc["security_id"].duplicated().any() or sc["company"].dropna().duplicated().any():
        out.append(f"{name}: a security or a company appears twice")
    if sc["yahoo"].dropna().duplicated().any() or sc["ticker"].dropna().duplicated().any():
        out.append(f"{name}: a ticker or chart symbol appears twice (two securities would share one chart)")
    rel = pd.to_numeric(sc.sort_values("mcap_rank")["mcap_rel"], errors="coerce").to_numpy(float)
    if len(rel) and (abs(rel[0] - 1) > 1e-9 or np.any(np.diff(rel) > 1e-12) or np.any(rel <= 0)):
        out.append(f"{name}: market-cap ratios do not fall from 1 with the rank")
    for rule in M_RULES:
        want = dict(m_targets(sc, rule))
        got = {r.security_id: float(getattr(r, f"w_{rule}")) for r in sc.itertuples() if getattr(r, f"w_{rule}") > 0}
        if set(want) != set(got) or any(abs(want[k] - got[k]) > WEIGHT_TOL for k in want):
            t = dict(zip(sc["security_id"], sc["ticker"]))
            out.append(f"{name} {rule}: file targets {sorted((t.get(k, k), round(x, 4)) for k, x in got.items())}, "
                       f"the ranking gives {sorted((t.get(k, k), round(x, 4)) for k, x in want.items())}")
    return out


def check_rules(b4res: dict | None, qqq: pd.DataFrame | None, signals: dict) -> tuple[hc.Check, list]:
    """``qqq``: the stored QQQ chart frame (own reader; adjclose); ``signals`` {file name: frozen frame}. Returns
    (check, next-session notes)."""
    fails, warns, nxt, done = [], [], [], []
    lines = (b4res or {}).get("lines", {})
    adj = qqq["adjclose"] if qqq is not None else None
    # LEV-B3
    v = lines.get("LEV-B3", {})
    if adj is not None:
        st, near = sma_state(adj)
        last = adj.index[-1]
        if np.isfinite(st.iloc[-1]):
            ma = adj.iloc[-LEV_SMA:].mean()
            nxt.append(f"LEV-B3: SMA200 state {'on' if st.iloc[-1] == 1 else 'off'} at the {last:%m-%d} close "
                       f"(QQQ {adj.iloc[-1] / ma - 1:+.1%} vs its SMA200; on above +{LEV_BAND:.0%}, off below "
                       f"-{LEV_BAND:.0%})")
        if v.get("started"):
            w = v["w"]
            ses = w.index
            dec = ses[v["s_i"] - 1: v["end_i"] + 1]
            mine = st.reindex(dec)
            theirs = (w["QLDX"].reindex(dec) > 0).astype(float)
            bad = [d for d in dec if mine[d] != theirs[d]]
            for d in bad[:5]:
                msg = (f"LEV-B3 {_d(d)}: runner state {'on' if theirs[d] == 1 else 'off'}, the SMA200 rule gives "
                       f"{'on' if mine[d] == 1 else 'off'}")
                (warns if near.get(d, 1.0) < hc.BORDERLINE else fails).append(
                    msg + (" (numerical tie)" if near.get(d, 1.0) < hc.BORDERLINE else ""))
            e_t = mine.map({1.0: LEV_ON, 0.0: LEV_OFF})
            ch = mine.ne(mine.shift(1)) & mine.shift(1).notna()
            trades = [d for d in dec if ch.get(d, False) or d == dec[0] or (
                ses.get_loc(d) + 1 < len(ses) and d.to_period("M") != ses[ses.get_loc(d) + 1].to_period("M"))]
            fails += exposure_items("LEV-B3", v["exposure"], st.reindex(ses).map({1.0: LEV_ON, 0.0: LEV_OFF}), ses,
                                    trades)
            done.append(f"LEV-B3 {len(dec)} decision(s), state {'on' if e_t.iloc[-1] == LEV_ON else 'off'}")
    # V3
    v = lines.get("V3", {})
    if adj is not None:
        now = v3_exposure(adj, [adj.index[-1]])[adj.index[-1]]
        if np.isfinite(now):
            nxt.append(f"V3: min(1.5, sigma_LR / sigma_20) at the {adj.index[-1]:%m-%d} close {now:.3f} "
                       "(decided at the month end)")
    if adj is not None and v.get("started"):
        ses = v["w"].index
        win = ses[: v["end_i"] + 1]
        decide = [d for d in _month_end_days(win) if d >= ses[v["s_i"] - 1] - pd.Timedelta(days=40)]
        mine = v3_exposure(adj, decide)
        for d in decide:
            got = float(v["e_t"].loc[d])
            if not (np.isfinite(mine[d]) and abs(got - mine[d]) <= E_TOL * max(1.0, mine[d])):
                fails.append(f"V3 {_d(d)}: runner target {got:.6f}, the rule gives {mine[d]:.6f}")
        tgt = pd.Series({d: mine[d] for d in decide}).reindex(ses).ffill()
        fails += exposure_items("V3", v["exposure"], tgt, ses, [d for d in decide if d >= ses[v["s_i"] - 1]]
                                + [ses[v["s_i"] - 1]])
        done.append(f"V3 {len(decide)} month-end decision(s)")
    # BT4b
    v = lines.get("BT4b", {})
    if v.get("started"):
        ses = v["sessions"]
        bw = v["btc_weight"]
        reb = [ses[v["s_i"]]] + [ses[i + 1] for i in range(v["s_i"], len(ses) - 1)
                                 if ses[i].to_period("M") != ses[i + 1].to_period("M")]
        n = 0
        for d in reb:
            i = ses.get_loc(d)
            if i + 1 >= len(ses):
                continue
            x = bw.get(ses[i + 1])
            n += 1
            if x is None or abs(x - BT_SLEEVE) > SLEEVE_TOL:
                fails.append(f"BT4b {_d(d)}: BTC weight after the rebalance {x if x is None else round(x, 6)}, "
                             f"registered {BT_SLEEVE:.0%}")
        nxt.append(f"BT4b: BTC weight {bw.iloc[-1]:.3f} at the {ses[-1]:%m-%d} close (rebalanced to "
                   f"{BT_SLEEVE:.0%} at the close after each month end)")
        done.append(f"BT4b {n} rebalance(s)")
    # mega-cap: the frozen files and the targets the runner simulated
    for name, sc in sorted(signals.items()):
        fails += signal_items(name, sc)
    for k in M_RULES:
        v = lines.get(k, {})
        if not v.get("started"):
            continue
        for s, tg in v["targets"].items():
            sc = signals.get(f"m_{_d(s)}.csv")
            if sc is None:
                fails.append(f"{k}: the runner used a signal of {_d(s)} without a frozen file")
                continue
            want = dict(m_targets(sc, k))
            got = {sid: w for sid, w, *_ in tg}
            if set(want) != set(got) or any(abs(want[x] - got[x]) > WEIGHT_TOL for x in want):
                fails.append(f"{k} {_d(s)}: the runner's targets differ from the ranking of the frozen file")
        for d, s in v["execs"].items():
            n_t = len(v["targets"][s])
            n_h = int(v["names"].get(d, -1))
            if n_h != n_t:
                fails.append(f"{k} {_d(d)}: {n_h} name(s) held after the rebalance, {n_t} target(s)")
        done.append(f"{k} {len(v['execs'])} rebalance(s)")
    if signals:
        nxt.append("mega-cap: next signal at the month's last session (computed by the next session's run)")
    status = _status(fails, warns)
    msg = ("B4 rules recomputed: " + ", ".join(done) if done else
           "before the B4 windows: nothing to compare") if status == OK else \
        f"{len(fails)} mismatch(es)" + (f", {len(warns)} at a numerical tie" if warns else "")
    return hc.Check("b4_rules", status, msg, fails + warns), nxt


# ======================================================================== mega-cap signal files


def due_signals(calendar: pd.DatetimeIndex, target) -> list:
    return [s for s in hc.month_end_sessions(calendar, FIRST_SIGNAL, target) if s < pd.Timestamp(target)]


def check_signals(state_dir: Path, target, calendar: pd.DatetimeIndex, known: dict | None = None,
                  grace: bool = False) -> tuple[hc.Check, dict, dict]:
    """(check, {file name: sha256}, {file name: frame}) of the due mega-cap signal files."""
    target = pd.Timestamp(target)
    due = due_signals(calendar, target)
    hashes, frames, fails, warns = {}, {}, [], []
    if not due:
        nxt = hc.month_end_sessions(calendar, FIRST_SIGNAL, pd.Timestamp(FIRST_SIGNAL) + pd.Timedelta(days=40))
        when = ("is today's close: computed by the next session's run" if nxt and nxt[0] == target
                else f"is the {_d(nxt[0]) if nxt else FIRST_SIGNAL} close")
        return hc.Check("b4_signal", OK, f"no mega-cap signal due yet (the first signal {when})"), hashes, frames
    for s in due:
        p = Path(state_dir) / f"m_{_d(s)}.csv"
        exe = hc.next_session(s, calendar)
        if not p.exists():
            msg = f"mega-cap signal {_d(s)} missing: due at the {_d(exe)} run; the M lines stop at {_d(s)}"
            (warns if grace and exe == target else fails).append(
                msg + (" (needs this session's bar; a run after 20:00 New York fails if still missing)"
                       if grace and exe == target else ""))
            continue
        try:
            sc = pd.read_csv(p, dtype={"security_id": str, "ticker": str, "yahoo": str, "company": str, "cik": str})
        except (OSError, ValueError) as exc:
            fails.append(f"{p.name}: unreadable ({type(exc).__name__})")
            continue
        if "s" not in sc or (pd.to_datetime(sc["s"]) != s).any():
            fails.append(f"{p.name}: rows with another signal date")
        if not p.with_name(p.stem + "_meta.json").exists():
            fails.append(f"{p.stem}_meta.json: missing")
        for q in (p, p.with_name(p.stem + "_meta.json")):
            if not q.exists():
                continue
            h, old = hc.file_sha256(q), (known or {}).get(q.name)
            if old and old != h:
                fails.append(f"{q.name}: changed after it was first recorded (frozen signal files are never "
                             "recomputed; restore it from Git)")
            hashes[q.name] = old or h                      # the first recorded hash stays until the file is restored
        frames[p.name] = sc
    status = _status(fails, warns)
    msg = (f"{len(frames)} mega-cap signal file(s) present ({', '.join(n[2:12] for n in sorted(frames))})"
           if status == OK else f"{len(fails)} failure(s), {len(warns)} warning(s)")
    return hc.Check("b4_signal", status, msg, fails + warns), hashes, frames


# ======================================================================== replay inputs


def btc_fingerprints(btc, start, end) -> dict:
    if btc is None:
        return {}
    p = btc.close[(btc.close.index >= pd.Timestamp(start)) & (btc.close.index <= pd.Timestamp(end))]
    r = (p / p.shift(1)).dropna()
    return {"BTC": {_d(d): f"{x:.12g}" for d, x in r.items()}}


def rf_fingerprints(rf: pd.Series | None, start, end) -> dict:
    if rf is None:
        return {}
    x = rf[(rf.index >= pd.Timestamp(start)) & (rf.index <= pd.Timestamp(end))]
    return {"RF": {_d(d): f"{v:.12g}" for d, v in x.items()}}


def held_names(b4res: dict | None) -> dict:
    """{ticker: yahoo symbol} of the names held after the latest executed rebalance of each mega-cap line."""
    out = {}
    for k in M_RULES:
        v = ((b4res or {}).get("lines") or {}).get(k, {})
        if not v.get("started") or not v["execs"]:
            continue
        s = v["execs"][max(v["execs"])]
        for sid, *_ in v["targets"][s]:
            out[v["tick"].get(sid, sid)] = v["yahoo"].get(sid, sid)
    return dict(sorted(out.items()))


FP_MARGIN = 3                              # sessions around each holding period (the fingerprints of a held name)


def megacap_fingerprints(b4res: dict | None, held_dir: Path, start, target, margin: int = FP_MARGIN) -> dict:
    """{ticker: {date: ratios}} of every mega-cap target name on the sessions it was held by any line (each executed
    rebalance through the next one, or through the line's last session) with ``margin`` sessions on each side, as
    the B3 shorts: a vendor revision outside a holding period changes no number, and the replay state stays small."""
    days, ymap = {}, {}
    for k in M_RULES:
        v = ((b4res or {}).get("lines") or {}).get(k, {})
        if not v.get("started"):
            continue
        ses = v["nav"].index
        ex = sorted(v["execs"].items())
        for j, (e, s) in enumerate(ex):
            e1 = ex[j + 1][0] if j + 1 < len(ex) else ses[-1]
            span = ses[max(0, ses.get_loc(e) - margin): ses.get_loc(e1) + margin + 1]
            for sid, *_ in v["targets"][s]:
                tick = v["tick"].get(sid, sid)
                days.setdefault(tick, set()).update(span)
                ymap[tick] = v["yahoo"].get(sid, sid)
    out = {}
    for tick, ds in sorted(days.items()):
        p = Path(held_dir) / f"{ymap[tick]}.json.gz"
        df = hc.chart_frame(p, target) if p.exists() else None
        if df is not None:
            out[tick] = hc.chart_fingerprints(df, start, target, sorted(ds))
    return out
