"""Forward observation runner for the frozen candidates of docs/forward_observation_checklist.md section B
(owner decisions 2026-10-09 and 2026-10-10). Not QuantConnect.

- B1: stock basket S3-Yb (18 names, one $10k cash account each), simulated by scripts/research_selective_t.py
  (``run_one`` with config "S3-Yb", asset class "stock", variant "net").
- B2: QQQ RSI2-dip, two separate lines SEL-A (config 29876) and SEL-P (config 29916), simulated by
  scripts/research_t_grid.py (``make_inst`` / ``exact_run`` / ``hmatch_series`` / ``hbase_of`` / ``oneq_for``).
- B3: S-MISP short overlay, configuration S-MISP N=10 k=20% MN, simulated by scripts/research_short_overlay.py
  (``schedule`` / ``simulate`` / ``basket_diagnostics`` / ``month_returns``) on a monthly U300 signal built from
  fresh data by scripts/forward_smisp.py (checklist section B3). First signal at the 2026-10-30 close, executed at
  the next close (2026-11-02).

No simulator logic is reimplemented here: this file only fetches and loads data the way the research scripts do,
chooses the forward window and turns the research functions' outputs into a monthly table. The frozen parameters of
the lines are declared once, in the block "the frozen lines" below (B3's in scripts/forward_smisp.py).

Forward window: the accounts open at the 2026-10-12 close (base bought at that close, as the research ``simulate``
functions buy the base at the close of s-1), so the first forward return session is the next session. Indicators
use the full history (QQQ from 1999 for RSI2's expanding percentiles, stocks from 2024 for the SMA20); everything
is truncated at the as-of date right after parsing and asserted (no row after the as-of date survives).

H_match, incremental and fixed now (2026-10-09, before any forward data): for month m, each account's w_m is the
mean daily exposure that the frozen simulator reports for that account run from the forward start through the last
forward session of month m (for the latest month: through the as-of date). Month m's H_match daily return is
w_m x the instrument's daily total return (research ``match_returns`` / ``hmatch_series`` on that window; B1 basket =
equal-weight mean of the 18 accounts). Only data through month m enter month m's H_match, so completed rows never
change when later data arrive. At the end of the research window this is the research definition (one w over the
whole window); here each month uses the exposure realised to date.

Cumulative excess vs H_match = (chained strategy return) - (chained H_match return), in percentage points, both
chained from the forward start (same convention as section A).

Data (B1 / B2): Tiingo daily prices from 2026-11 (key TIINGO_API_KEY), Yahoo v8 chart as fallback (one request per
2 s, stop at the first 401/403/429); both are written in the Yahoo chart format (scripts/forward_prices.py). Only a
recent tail is fetched: the history through the 2026-10-09 decision close is rebuilt from the committed returns-only
state state/forward_observation/price_history_returns.csv.gz (no price levels) and checked on the overlapping days.
B3 prices: Alpaca first, Yahoo fallback (scripts/forward_smisp.py). Raw bodies only in the local cache
(``forward_smisp.cache_root()``: research_cache/forward_observation, or .cache/forward_observation /
$FORWARD_OBS_CACHE on GitHub Actions; never in Git). The committed log docs/forward_observation_log.md holds returns
in percent, dates and tickers only, never vendor price levels.

A month's row is final once the data hold a session of a later month (the frozen simulators treat the window's
last session specially); the month holding the last session is "provisional" and is replaced by the next run.

Usage (monthly, after 18:00 New York time on the first trading day of the new month or later, so that the month
just ended is final; see the checklist section B):
  PYTHONPATH=. .venv/bin/python scripts/forward_observation.py --fetch --fetch-smisp
  PYTHONPATH=. .venv/bin/python scripts/forward_observation.py --as-of 2026-11-30 --dry-run   # testing
"""
from __future__ import annotations

import os

os.environ.setdefault("REVERSAL_DATA_VERSION", "v2")   # B3 reads the frozen v2 inputs (B1 / B2 do not depend on it)

import argparse  # noqa: E402
import csv  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.error import HTTPError  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from scripts import forward_prices as fp  # noqa: E402
from scripts import forward_smisp as fs  # noqa: E402
from scripts import research_intraday_t as it  # noqa: E402
from scripts import research_selective_t as st  # noqa: E402
from scripts import research_t_grid as tg  # noqa: E402
from scripts.research_calendar import t_and_ir  # noqa: E402
from scripts.research_qqq_timing import assert_dev_dates, monthly  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CACHE = fs.cache_root()             # $FORWARD_OBS_CACHE, else research_cache/forward_observation (local only)
RAW = CACHE / "raw"
FETCH_LOG = CACHE / "fetch_log.csv"
RUNS = CACHE / "runs"
LOG = ROOT / "docs/forward_observation_log.md"
NY = ZoneInfo("America/New_York")
COMPLETE_HOUR_NY = 18               # a session counts as complete from 18:00 New York time

# ======================================================================== the frozen lines (checklist section B)

START_CLOSE = "2026-10-12"          # B1 / B2 accounts open at this close (in-sample data end at the 2026-10-09 close)
STOCKS = st.STOCKS                  # B1: the 18 names
SYMBOLS = ("QQQ", "ONEQ") + STOCKS
FETCH_START = {"QQQ": "1999-01-01", "ONEQ": "2003-01-01"}   # stocks: st.FETCH_START (without the history state)
B1_CONFIG = "S3-Yb"                 # research_selective_t.run_one(config, "stock", "net")
B2 = {"SEL-A": (29876, "RSI2 p2 B OPP H10 1/2 res25 ALL"),   # research_t_grid config id, label
      "SEL-P": (29916, "RSI2 p2 B OPP H20 1 res25 ALL")}
B2_IDS = {k: v[0] for k, v in B2.items()}
B2_LABELS = {k: v[1] for k, v in B2.items()}
B2_RESERVE_CODE = 2                 # res25 -> H_base_res25
B3_TITLE = "B3 S-MISP short overlay (N=10, k=20%, MN)"      # fs.CFG, first signal fs.FIRST_SIGNAL
TITLES = {"B1": "B1 S3-Yb (U18 basket)", "SEL-A": "B2 SEL-A (QQQ, config 29876)",
          "SEL-P": "B2 SEL-P (QQQ, config 29916)", "B3": B3_TITLE}
RULES = {"B1": f"config `{B1_CONFIG}`, stock parameters (`scripts/research_selective_t.py`)",
         **{k: f"config {i} `{lab}` (`scripts/research_t_grid.py`)" for k, (i, lab) in B2.items()}}
VERDICT_MONTHS = (24, 36)
SMISP_DIR = fs.BASE                 # B3 raw bodies (local cache)
SMISP_STATE = fs.STATE              # B3 frozen monthly signals (committed, no price levels)


# ======================================================================== dates

def last_complete_session_date(now: datetime | None = None) -> str:
    """Today in New York if it is past 18:00 there, else yesterday (weekends / holidays have no rows anyway)."""
    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    d = now.date() if now.hour >= COMPLETE_HOUR_NY else now.date() - timedelta(days=1)
    return d.isoformat()


# ======================================================================== fetch B1 / B2 prices (Tiingo, Yahoo fallback)

def raw_path(sym: str, raw_dir: Path = RAW) -> Path:
    return raw_dir / f"{sym}.json"


def b_source(now: datetime | None = None) -> str:
    """Tiingo from 2026-11 when a key is available (the free plan's October quota is used up), else Yahoo."""
    month = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
    return "tiingo" if month >= fp.TIINGO_FROM_MONTH and fp.tiingo_key() else "yahoo"


def fetch(symbols=SYMBOLS, raw_dir: Path = RAW, log: Path = FETCH_LOG, getter=None, sleep=time.sleep,
          history: Path | None = fp.HISTORY_STATE, source: str | None = None, tiingo_getter=None,
          source_log: Path | None = None) -> dict:
    """Re-download every symbol (the forward data grow each month). Source: Tiingo (from 2026-11, ``b_source``),
    Yahoo as fallback (one request per 2 s, stop at 401/403/429). With the committed history state the vendor is
    asked only for a recent tail; the history through the decision close is rebuilt from the state's ratios and
    the overlapping days are checked (``forward_prices.merge_history``). Bodies are stored as plain JSON (the QQQ /
    ONEQ loaders of the research code read plain JSON)."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    period2 = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    state = fp.load_history_state(history) if history is not None else None
    source = source or b_source()
    source_log = source_log or log.with_name("fetch_sources.csv")
    fp.log_line(log, "fetched_utc,symbol,http_status,bytes")
    fp.log_line(source_log, "fetched_utc,symbol,source,history_check")
    out, stop_yahoo = {}, False
    for k, sym in enumerate(symbols):
        start = FETCH_START.get(sym, st.FETCH_START)
        if state is not None and (state["sym"] == sym).any():
            last = state.loc[state["sym"] == sym, "date"].max()
            start = (pd.Timestamp(last) - pd.Timedelta(days=fp.TAIL_DAYS)).strftime("%Y-%m-%d")
        now = fp.utc_stamp()
        payload, used = None, None
        if source == "tiingo":
            try:
                payload, used = fp.tiingo_chart(sym, start, period2, getter=tiingo_getter), "tiingo"
            except Exception as exc:  # noqa: BLE001 - any Tiingo failure falls back to Yahoo
                print(f"fetch: Tiingo failed for {sym} ({type(exc).__name__}); Yahoo fallback")
        if payload is None:
            if stop_yahoo:
                out[sym] = "skipped (Yahoo stopped)"
                continue
            if k:
                sleep(st.SECONDS_PER_REQUEST)
            try:
                data = fp.yahoo_get(sym, start, period2, getter)
            except HTTPError as exc:
                fp.log_line(log, "", f"{now},{sym},{exc.code},0")
                out[sym] = f"HTTP {exc.code}"
                if exc.code in st.STOP_CODES:
                    print(f"fetch: HTTP {exc.code} on {sym}; stopping Yahoo (the cached files are left as they were)")
                    stop_yahoo = True
                continue
            fp.log_line(log, "", f"{now},{sym},200,{len(data)}")
            payload, used = json.loads(data), "yahoo"
        if not fp.is_daily_chart(payload):
            out[sym] = "not daily / empty"
            continue
        check = {}
        if state is not None:
            try:
                payload, check = fp.merge_history(sym, state, payload)
            except ValueError as exc:
                out[sym] = f"history merge failed: {exc}"
                print("fetch:", out[sym])
                continue
            if not check.get("overlap_ok", True):
                print(f"fetch: WARNING {sym}: the {used} tail disagrees with the history state on overlapping days "
                      f"({check})")
        fp.save_payload(payload, raw_path(sym, raw_dir), compress=False)
        with source_log.open("a", newline="") as fh:
            csv.writer(fh).writerow([now, sym, used, json.dumps(check)])
        out[sym] = f"ok ({used})"
        print(f"fetch: {sym} {out[sym]}")
    return out


# ======================================================================== load (as research_selective_t, end = as-of)

def load_market(sym: str, end: str, raw_dir: Path = RAW) -> st.Market:
    """QQQ / ONEQ as ``st.load_qqq_like``, stocks as ``st.load_stock``, truncated at ``end`` and asserted."""
    path = raw_path(sym, raw_dir)
    if sym in ("QQQ", "ONEQ"):
        df, splits = it.parse_ohlc(path, end), it.split_events(path, end)
    else:
        df, splits = st.ohlc_frame(json.loads(path.read_text()), end)
    assert_dev_dates(df["date"], end)
    return st.market_from_frame(sym, df, splits)


@dataclass
class Data:
    end: str
    qqq: st.Market
    oneq: st.Market
    stocks: dict
    checks: dict = field(default_factory=dict)

    def markets(self) -> list:
        return [("QQQ", self.qqq), ("ONEQ", self.oneq), *self.stocks.items()]


CHECK_KEYS = ("first", "last", "rows", "ohlc_missing_or_nonpositive", "open_or_close_outside_high_low",
              "ohlc_filled_from_close")


def load_all(end: str, raw_dir: Path = RAW, stocks=STOCKS) -> Data:
    missing = [s for s in ("QQQ", "ONEQ", *stocks) if not raw_path(s, raw_dir).exists()]
    if missing:
        raise SystemExit(f"missing raw data for {missing} in {raw_dir}; run with --fetch")
    qqq, oneq = load_market("QQQ", end, raw_dir), load_market("ONEQ", end, raw_dir)
    out = Data(end, qqq, oneq, {s: load_market(s, end, raw_dir) for s in stocks})
    for sym, m in out.markets():
        out.checks[sym] = {k: m.s.checks[k] for k in CHECK_KEYS}
        if sym in out.stocks:
            extra = pd.DatetimeIndex(m.s.sessions).difference(qqq.s.sessions)
            if len(extra):
                raise ValueError(f"{sym} sessions not in the QQQ calendar: {list(extra[:5])}")
    return out


def overlap_check(d: Data, ref_end: str = st.END) -> dict:
    """Fresh split-adjusted closes vs the research caches on common dates through ``ref_end`` (console only)."""
    out = {}
    for sym, m in d.markets():
        try:
            ref = st.load_qqq_like(sym) if sym in ("QQQ", "ONEQ") else (
                st.load_stock(sym) if st.raw_path(sym).exists() else None)
        except Exception as exc:  # noqa: BLE001 - a reference cache problem must not stop the run
            out[sym] = f"no reference ({type(exc).__name__})"
            continue
        if ref is None:
            out[sym] = "no reference"
            continue
        a = pd.Series(m.s.close, index=m.s.sessions)
        b = pd.Series(ref.s.close, index=ref.s.sessions)
        j = pd.concat([a, b], axis=1, join="inner").dropna()
        j = j[j.index <= pd.Timestamp(ref_end)]
        rel = (j.iloc[:, 0] / j.iloc[:, 1] - 1).abs()
        out[sym] = {"common_days": int(len(j)), "median_abs_rel_diff": float(rel.median()) if len(j) else None,
                    "max_abs_rel_diff": float(rel.max()) if len(j) else None}
    return out


# ======================================================================== forward window, H_match to date

@dataclass
class Window:
    started: bool
    s: int = -1               # first forward return session (index in the instrument's own sessions)
    e: int = -1               # last session <= as-of
    sessions: pd.DatetimeIndex | None = None


def window_of(m: st.Market, end: str, start_close: str | None = None) -> Window:
    start_close = start_close or START_CLOSE
    ses = m.s.sessions
    i0 = int(ses.searchsorted(pd.Timestamp(start_close)))
    e = int(ses.searchsorted(pd.Timestamp(end), side="right")) - 1
    if i0 >= len(ses) or ses[i0] != pd.Timestamp(start_close):
        if pd.Timestamp(end) <= pd.Timestamp(start_close) or i0 >= len(ses):
            return Window(False)
        raise ValueError(f"{m.sym}: no session on the start close {start_close}")
    s = i0 + 1
    if e < s:
        return Window(False)
    return Window(True, s, e, ses[s: e + 1])


def month_ends(sessions: pd.DatetimeIndex) -> list:
    """Index (within ``sessions``) of the last session of each month."""
    per = sessions.to_period("M")
    return [i for i in range(len(sessions)) if i == len(sessions) - 1 or per[i + 1] != per[i]]


def hmatch_to_date(sessions: pd.DatetimeIndex, run_at, hmatch, exposure) -> tuple[pd.Series, dict]:
    """The H_match rule: month m's H_match days and its w come from the simulator run truncated at m's last forward
    session (``run_at(k)``: the run through ``sessions[k]``; ``hmatch(run)`` its daily H_match, ``exposure(run)``
    its mean exposure). Returns (H_match daily returns, {month: w})."""
    parts, wm = [], {}
    for k in month_ends(sessions):
        run = run_at(k)
        mon = sessions[k].to_period("M")
        hm = hmatch(run)
        parts.append(hm[hm.index.to_period("M") == mon])
        wm[str(mon)] = exposure(run)
    return pd.concat(parts), wm


def zero_rf(index) -> pd.Series:
    """The T-bill only enters margin accounts (H100) in the research code; cash accounts and w <= 1 ignore it."""
    return pd.Series(0.0, index=index)


# ======================================================================== B1: S3-Yb (research_selective_t)

def run_b1(d: Data) -> dict:
    rf = zero_rf(d.qqq.s.sessions)
    per, ref_cache = {}, {}
    for sym, m in d.stocks.items():
        w = window_of(m, d.end)
        if not w.started:
            continue

        def run_to(e, m=m, w=w):
            return st.run_one(m, w.s, e, B1_CONFIG, "stock", "net", rf, ref_cache)
        o = run_to(w.e)
        hm, wm = hmatch_to_date(w.sessions, lambda k: o if w.s + k == w.e else run_to(w.s + k),
                                lambda r: r["hmatch"], lambda r: r["sim"]["exposure"])
        per[sym] = {"ret": o["ret"], "hbase": o["hbase"], "hmatch": hm, "w": wm,
                    "trips": o["sim"]["trips"].assign(symbol=sym), "hmatch_full_window": o["hmatch"]}
    if not per:
        return {"started": False}
    ret = st.basket_mean([p["ret"] for p in per.values()])
    out = {"started": True, "ret": ret,
           **{k: st.basket_mean([p[k] for p in per.values()]) for k in ("hbase", "hmatch", "hmatch_full_window")},
           "w": {mo: float(np.mean([p["w"][mo] for p in per.values() if mo in p["w"]]))
                 for mo in sorted({k for p in per.values() for k in p["w"]})},
           "oneq": it.buy_hold_oneq(d.oneq.s, d.qqq.s.sessions, ret.index[0], ret.index[-1], True),
           "n_accounts": len(per)}
    trips = pd.concat([p["trips"] for p in per.values()], ignore_index=True)
    out["trades"] = [{"symbol": r.symbol, "entry": r.entry, "exit": r.exit, "open": r.reason == "end",
                      "reason": r.reason, "days": int(r.days),
                      "net_pct": float((r.gross - r.cost) / r.notional * 100)} for r in trips.itertuples()]
    return out


# ======================================================================== B2: SEL-A / SEL-P (research_t_grid)

def b2_data(d: Data) -> dict:
    """The QQQ part of ``tg.load_all`` (master calendar = QQQ sessions, ONEQ total return from 2003-10-02)."""
    qqq, oneq = d.qqq, d.oneq
    master = qqq.s.sessions
    oneq_tr = oneq.s.tr.reindex(master)
    oneq_tr[oneq_tr.index < pd.Timestamp(st.ONEQ_FIRST_RETURN)] = np.nan
    s = qqq.s
    ins = tg.make_inst(qqq.sym, st.HALF_SPREAD_BPS["QQQ"], s.sessions, master, s.open, s.high, s.low, s.close,
                       s.split, s.div, s.tr.to_numpy(float), qqq.factor)
    return {"master": master, "insts": {"QQQ": ins}, "rf": zero_rf(master), "oneq_tr": oneq_tr, "oneq": oneq}


def b2_trades(ex: dict, j: int) -> list:
    """The trades of configuration column ``j`` as the research simulator records them (``exact_run(...,
    record_trades=True)``); a trade still open at the last session is closed there ('end') and shown as open."""
    ses = ex["sessions"]
    return [{"symbol": "QQQ", "entry": ses[t["t0"]], "exit": ses[t["t1"]], "open": t["reason"] == "end",
             "reason": t["reason"], "days": None if t["reason"] == "end" else t["days"],
             "net_pct": t["net_bp"] / 100} for t in ex["trades"] if t["cfg"] == j]


def run_b2(d: Data) -> dict:
    w = window_of(d.qqq, d.end)
    if not w.started:
        return {name: {"started": False} for name in B2}
    data = b2_data(d)
    ids = list(B2_IDS.values())
    g = tg.grid()
    for name, (i, lab) in B2.items():
        assert tg.label(g.loc[i]) == lab, (name, tg.label(g.loc[i]))
        assert int(g.loc[i, "reserve"]) == B2_RESERVE_CODE
    start, last = str(w.sessions[0].date()), len(w.sessions) - 1

    def exact(k: int, trades: bool = False) -> dict:
        return tg.exact_run(data, "QQQ", ids, start, str(w.sessions[k].date()), tg.CostModel(), record_trades=trades)
    full = exact(last, trades=True)
    runs = {k: full if k == last else exact(k) for k in month_ends(w.sessions)}   # truncated at each month end
    out = {}
    for j, (name, i) in enumerate(B2_IDS.items()):
        r = full["R"][i]
        hm, wm = hmatch_to_date(w.sessions, runs.__getitem__, lambda ex: tg.hmatch_series(ex, j),
                                lambda ex: float((ex["sim"]["rec"]["x"][:, :, j] * ex["act"]).sum() / ex["act"].sum()))
        oneq = tg.oneq_for(data, r.index)
        if oneq is None:   # a one-session window: ONEQ bought at the previous close, one return
            oneq = it.buy_hold_oneq(d.oneq.s, data["master"], r.index[0], r.index[-1], True)
        out[name] = {"started": True, "config_id": i, "label": B2_LABELS[name], "ret": r,
                     "hbase": tg.hbase_of(full, B2_RESERVE_CODE), "hmatch": hm, "w": wm,
                     "hmatch_full_window": tg.hmatch_series(full, j), "oneq": oneq, "trades": b2_trades(full, j)}
    return out


# ======================================================================== B3: S-MISP short overlay (research_short_overlay)

def run_b3(d: Data, smisp_dir: Path | None = None, borrow_dir: Path | None = None,
           smisp_state: Path | None = None) -> dict:
    """The S-MISP line through the as-of date from the frozen monthly signal files (scripts/forward_smisp.py).
    Signals are the last sessions of each month from 2026-10-30 on; each is executed at the next close. A missing
    signal file stops the simulation at that signal's date (the line is then 'pending' from there)."""
    base = Path(smisp_dir or SMISP_DIR)
    state = Path(smisp_state) if smisp_state else (base / "state" if smisp_dir else SMISP_STATE)
    sigs = fs.signal_dates(d.qqq.s.sessions)
    if not sigs:
        return {"started": False, "reason": "before_first_signal"}
    signals = {}
    for s in sigs:
        p = fs.signal_path(s, state)
        if not p.exists():
            break
        signals[s] = fs.load_signal(p)
    if not signals:
        return {"started": False, "reason": f"signal {sigs[0].date()} not computed yet (run --fetch-smisp)"}
    pending = sigs[len(signals)] if len(signals) < len(sigs) else None
    end = str(pending.date()) if pending is not None else d.end
    effr_path = base / "EFFR_nyfed.csv"
    if not effr_path.exists():
        return {"started": False, "reason": "no EFFR file (run --fetch-smisp)"}
    snaps = fs.BorrowSnapshots(Path(borrow_dir or fs.BORROW_DIR))
    line = fs.run_line(signals, d.qqq, d.oneq, end, base / "held", fs.ro.load_effr(effr_path), snaps)
    if not line["started"]:
        return {"started": False, "reason": "no executable signal yet"}
    line["pending"] = pending
    line["n_snapshots"] = len(snaps.index)
    return line


# ======================================================================== monthly tables

def pct(x: float, nd: int = 2) -> str:
    return "" if x is None or not np.isfinite(x) else f"{x * 100:+.{nd}f}%"


def pp(x: float) -> str:
    return "" if x is None or not np.isfinite(x) else f"{x * 100:+.2f}"


def tstr(t: float) -> str:
    return "" if not np.isfinite(t) else f"{t:+.2f}"


def trade_cell(trades: list, mon: pd.Period, show_symbol: bool) -> str:
    """Trades that closed in the month, and trades still open at the month's last forward session."""
    parts = []
    for tr in sorted(trades, key=lambda x: (x["entry"], x.get("symbol", ""))):
        ent, ex = tr["entry"], tr["exit"]
        if ent.to_period("M") > mon:
            continue
        closed_in = (not tr["open"]) and ex.to_period("M") == mon
        open_at_end = tr["open"] or ex.to_period("M") > mon
        if not (closed_in or open_at_end):
            continue
        who = f"{tr['symbol']} " if show_symbol else ""
        if closed_in:
            why = {"target": "tgt", "timeout": "time"}.get(tr["reason"], tr["reason"])
            parts.append(f"{who}{ent:%m-%d}→{ex:%m-%d} {tr['net_pct']:+.2f}% {why}")
        else:
            parts.append(f"{who}{ent:%m-%d}→open")
    return "; ".join(parts) if parts else "none"


def month_rows(line: dict, show_symbol: bool) -> list:
    """One row per forward month (B1 / B2). A month is final once the data hold a later session: the frozen
    simulators treat the window's last session specially (open trades closed at its close, no month-end reset, no
    new signal), so the month that contains the last session of the data is provisional and is recomputed by the
    next run."""
    r, hb, hm, oq = line["ret"], line["hbase"], line["hmatch"], line["oneq"]
    mr, mb, mh, mo = monthly(r), monthly(hb), monthly(hm.reindex(r.index)), monthly(oq.reindex(r.index))
    cum_r, cum_h = (1 + mr).cumprod() - 1, (1 + mh).cumprod() - 1
    last_month = r.index[-1].to_period("M")
    rows = []
    for n, mon in enumerate(mr.index, start=1):
        days = r.index[r.index.to_period("M") == mon]
        ex_m = mr[:mon] - mh[:mon]
        rows.append({"month": str(mon), "n": n, "trades": trade_cell(line["trades"], mon, show_symbol),
                     "ret": mr[mon], "hbase": mb[mon], "hmatch": mh[mon], "w": line["w"].get(str(mon), float("nan")),
                     "oneq": mo[mon], "excess": mr[mon] - mh[mon], "cum_excess": cum_r[mon] - cum_h[mon],
                     "t": t_and_ir(ex_m)[0] if len(ex_m) >= 3 else float("nan"),
                     "sessions": f"{days[0]:%m-%d}..{days[-1]:%m-%d} ({len(days)})",
                     "status": "final" if mon < last_month else "provisional"})
    return rows


HEAD = ("| month | n | T-trades | strategy | H_base | H_match | w | ONEQ | excess vs H_match (pp) "
        "| cum. excess vs H_match (pp) | t | sessions | status |")
B3_HEAD = ("| month | n | shorts (after the month's rebalance) | strategy | QQQ | ONEQ | excess vs ONEQ (pp) "
           "| short leg (holding period) | short leg − QQQ (pp) | IBKR fee mean / max (%/yr) "
           "| unborrowable / not in IBKR file | strategy at IBKR fees | cum. excess vs ONEQ (pp) | t | status |")


def row_line(x: dict) -> str:
    return (f"| {x['month']} | {x['n']} | {x['trades']} | {pct(x['ret'])} | {pct(x['hbase'])} | {pct(x['hmatch'])} | "
            f"{x['w']:.3f} | {pct(x['oneq'])} | {pp(x['excess'])} | {pp(x['cum_excess'])} | {tstr(x['t'])} | "
            f"{x['sessions']} | {x['status']} |")


def b3_row_line(x: dict) -> str:
    leg = f"{pct(x['leg'])} ({x['leg_period']})" if np.isfinite(x["leg"]) else ""
    lq = pp(x["leg"] - x["leg_qqq"]) if np.isfinite(x["leg"]) else ""
    if x["fee_days"] == 0:
        fee = "no snapshot"
    elif np.isfinite(x["fee_mean"]):
        fee = f"{x['fee_mean'] * 100:.2f} / {x['fee_max'] * 100:.2f}"
    else:
        fee = "n/a"
    flags = [f"{s} (AVAILABLE 0)" for s in x["unborrowable"]] + [f"{s} (not in file)" for s in x["not_in_file"]]
    return (f"| {x['month']} | {x['n']} | {x['shorts']} | {pct(x['ret'])} | {pct(x['qqq'])} | {pct(x['oneq'])} | "
            f"{pp(x['excess'])} | {leg} | {lq} | {fee} | {'; '.join(flags) if flags else 'none'} | "
            f"{pct(x['ret_ibkr'])} | {pp(x['cum_excess'])} | {tstr(x['t'])} | {x['status']} |")


# ======================================================================== log file

LOG_HEADER = """# Forward observation log: B1 S3-Yb, B2 SEL-A / SEL-P, B3 S-MISP

Written by `scripts/forward_observation.py` (frozen rules: `docs/forward_observation_checklist.md` section B; owner
decisions 2026-10-09 for B1 / B2 and 2026-10-10 for B3). Do not edit the tables by hand: each run recomputes every forward month from the start and
replaces the rows of the months it computes (one row per month, never duplicated). Returns in percent and dates
only; no vendor price levels.

**Forward window.** In-sample data end at the 2026-10-09 close. The accounts open at the **2026-10-12 close** (base
bought at that close, $10k per account); the first forward return session is the next session. Under the frozen
simulators a signal is read at a close and acted on at the next open, so the earliest T-entry is the open after the
first forward close.

**Columns.**
- *month*: calendar month (New York sessions); *n*: month count since the start (2026-10 = 1, a partial month).
- *T-trades*: trades that closed in the month (`entry→exit` as MM-DD, net return of the traded shares after costs,
  `tgt` = limit / OPP exit, `time` = time stop) and trades still open at the month's last session (`MM-DD→open`).
  B1 lists the symbol. A trade's net return = its P&L after both orders' costs / the entry value of the traded
  shares (as the research trip statistics).
- *strategy*: the month's return of the account (B1: equal-weight mean of the 18 accounts' daily returns, chained).
- *H_base*: the same account (same base, reserve, month-end resets, costs) with no T-trading. Report only.
- *H_match*: same average exposure (the main comparison). Month m uses w = the mean daily exposure the frozen
  simulator reports for the account run from the start through the last forward session of month m (exposure
  realised to date; no later data). H_match's daily return = w × the instrument's daily total return (B1: per
  account, basket mean). *w* is that exposure (B1: mean over the 18 accounts).
- *ONEQ*: buy and hold, bought at the 2026-10-12 close with one order (IBKR Tiered + 2 bp). Report only.
- *excess vs H_match*: strategy − H_match for the month, in percentage points.
- *cum. excess vs H_match*: (strategy chained from the start) − (H_match chained from the start), percentage points.
- *t*: t of the monthly excess vs H_match from the start through the month (mean / sd × √n), shown from n = 3.
- *sessions*: first..last forward session of the month in the data (count).
- *status*: `final` once the data hold a session of a later month; otherwise `provisional`. The frozen simulators
  treat the window's last session specially (an open T-trade is closed at that close with its costs, no month-end
  reset, no new signal), so the month holding the last session of the data is recomputed by the next run. A
  provisional row is replaced by the next run; a final row should never change (the runner warns if it does).

**Evaluation (fixed 2026-10-09).** Main comparison H_match. No verdict before 24 months. At 36 months the verdict is
positive only if the cumulative excess over H_match is > 0 and the monthly-excess t is ≥ 2. No early abandon rule;
observed, not traded.

The B3 table (S-MISP short overlay) has its own columns and evaluation; they are described above that table.
"""

B3_INTRO = """Rule: configuration S-MISP N=10 k=20% MN of `docs/research_ledger_short_overlay.md` section 0 (frozen code
`scripts/research_short_overlay.py`; fresh monthly signal from `scripts/forward_smisp.py`; checklist section B3).
Observed, not traded.

**Forward window.** In-sample data end at the 2026-10-09 close. Signal at the last session of each month, executed at
the next session's close: the first signal is the 2026-10-30 close, the account ($10k) opens at the 2026-11-02 close,
so month 1 is the partial 2026-11 measured from that close (month 24 = 2028-10, month 36 = 2029-10).

**Columns.** *shorts*: the names held after the rebalance at the close of the month's first session (whole shares,
the research simulator). *strategy*: the month's return of the simulated account (registered tiered borrow fee;
research `month_returns`, the first month from the first trade's close). *QQQ*, *ONEQ*: total return over the same
days (ONEQ is the judged benchmark). *excess vs ONEQ*: strategy − ONEQ. *short leg*: the research ideal equal-weight
loser basket (no rounding, no costs) from the month's trade close to the next trade close (dates shown), and
*short leg − QQQ* over the same days (negative is good for a short). *IBKR fee*: report only; each short's FEERATE
from the latest IBKR snapshot on or before each calendar day of that holding period: mean over the names of each
name's mean, and the maximum (% a year); names with AVAILABLE = 0 or missing from the file are listed.
*strategy at IBKR fees*: report-only sensitivity, the strategy with the IBKR fee instead of the registered tier
(each short's value approximated as an equal share of the short book). *cum. excess vs ONEQ*: (strategy chained
from the start) − (ONEQ chained from the start), percentage points. *t*: monthly-excess t vs ONEQ (shown from
n = 3). *status*: `final` once the data hold the next month's trade session; else `provisional`.

**Evaluation (fixed 2026-10-10).** No verdict before 24 months. At 36 months positive only if the cumulative excess
over ONEQ is > 0 and the monthly-excess t vs ONEQ is ≥ 2. QQQ and the short leg vs QQQ are reported alongside.
The registered tiered borrow fee is the rule; the IBKR fees are report only."""


def progress(title: str, rows: list, vs: str, tail: str = "") -> str:
    """The status line of a started line: months so far, cumulative excess, t, verdict months."""
    last = rows[-1]
    t = "" if not np.isfinite(last["t"]) else f", monthly-excess t {last['t']:+.2f}"
    return (f"- {title}: {last['n']} month(s) through {last['month']}; cumulative excess vs {vs} "
            f"{pp(last['cum_excess'])} pp{t}; no verdict before month {VERDICT_MONTHS[0]}, verdict at month "
            f"{VERDICT_MONTHS[1]}.{tail}")


def b3_status(v: dict) -> str:
    if not v.get("started"):
        why = v.get("reason", "")
        extra = "" if why in ("", "before_first_signal") else f" Pending: {why}."
        return (f"- {B3_TITLE}: **observation starts at the {fs.FIRST_SIGNAL} month-end signal** (the last session "
                f"of October 2026), executed at the next close; no forward month yet.{extra}")
    pend = f" Pending: signal {v['pending'].date()} not computed (run --fetch-smisp)." if v.get("pending") else ""
    return progress(B3_TITLE, v["rows"], "ONEQ", pend)


def render_status(as_of: str, data_end: str | None, lines: dict) -> str:
    out = ["## Status", "", f"- As-of date of the last run: {as_of}; data through: {data_end or 'n/a'}."]
    if not any(lines.get(k, {}).get("started") for k in ("B1", *B2)):
        out.append(f"- **observation starts {START_CLOSE}** for B1 / B2 (accounts open at the {START_CLOSE} close). "
                   "No forward sessions yet, so no trades and no returns.")
    else:
        for key in ("B1", *B2):
            v = lines.get(key, {})
            out.append(progress(TITLES[key], v["rows"], "H_match") if v.get("started")
                       else f"- {TITLES[key]}: no forward sessions yet.")
    out.append(b3_status(lines.get("B3", {"started": False})))
    return "\n".join(out) + "\n"


TABLE_RE = re.compile(r"<!-- table:(?P<key>[\w-]+):start -->\n(?P<body>.*?)<!-- table:(?P=key):end -->", re.S)


def parse_rows(text: str) -> dict:
    """{line key: {month: row line}} from an existing log."""
    out = {}
    for m in TABLE_RE.finditer(text):
        rows = {}
        for ln in m.group("body").splitlines():
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if ln.startswith("|") and re.fullmatch(r"\d{4}-\d{2}", cells[0] or ""):
                rows[cells[0]] = ln
        out[m.group("key")] = rows
    return out


def last_data_end(text: str) -> str | None:
    m = re.search(r"data through: (\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else None


def section(key: str, new_rows: list, old_rows: dict, warnings: list) -> str:
    """One line's section of the log: the rows of the existing log, replaced by the rows computed now (a final row
    that changes is reported in ``warnings``)."""
    head, fmt, intro = (B3_HEAD, b3_row_line, B3_INTRO) if key == "B3" else (HEAD, row_line, f"Rule: {RULES[key]}.")
    rows = dict(old_rows)
    for x in new_rows:
        new = fmt(x)
        prev = rows.get(x["month"])
        if prev is not None and prev != new and prev.rstrip().endswith("| final |"):
            warnings.append(f"{key} {x['month']}: a final row changed (vendor data revision?)\n  old {prev}\n"
                            f"  new {new}")
        rows[x["month"]] = new
    sep = "|" + "---|" * (head.count("|") - 1)
    return (f"## {TITLES[key]}\n\n{intro}\n\n<!-- table:{key}:start -->\n{head}\n{sep}\n"
            + "".join(rows[mo] + "\n" for mo in sorted(rows)) + f"<!-- table:{key}:end -->\n")


def render_log(as_of: str, data_end: str | None, lines: dict, old_text: str = "") -> tuple[str, list]:
    old = parse_rows(old_text)
    warnings = []
    parts = [LOG_HEADER, render_status(as_of, data_end, lines)]
    parts += [section(key, lines.get(key, {}).get("rows", []), old.get(key, {}), warnings) for key in TITLES]
    return "\n".join(parts), warnings


# ======================================================================== run

def compute(as_of: str, raw_dir: Path = RAW, stocks=STOCKS, smisp_dir: Path | None = None,
            borrow_dir: Path | None = None, smisp_state: Path | None = None) -> dict:
    """Everything for one as-of date (no file writes)."""
    d = load_all(as_of, raw_dir, stocks)
    lines = {"B1": run_b1(d), **run_b2(d), "B3": run_b3(d, smisp_dir, borrow_dir, smisp_state)}
    for k, v in lines.items():
        if v["started"]:
            v["rows"] = fs.month_rows(v) if k == "B3" else month_rows(v, show_symbol=k == "B1")
    return {"as_of": as_of, "data_end": str(d.qqq.s.sessions[-1].date()), "lines": lines, "data": d}


def run(as_of: str, raw_dir: Path = RAW, log_path: Path = LOG, dry_run: bool = False, stocks=STOCKS,
        force: bool = False, smisp_dir: Path | None = None, borrow_dir: Path | None = None,
        smisp_state: Path | None = None) -> dict:
    res = compute(as_of, raw_dir, stocks, smisp_dir, borrow_dir, smisp_state)
    old = log_path.read_text() if log_path.exists() else ""
    prev_end = last_data_end(old)
    if prev_end and pd.Timestamp(res["data_end"]) < pd.Timestamp(prev_end) and not (dry_run or force):
        raise SystemExit(f"the log already has data through {prev_end}, later than this run's {res['data_end']}; "
                         "use --dry-run (or --force) for an earlier as-of date")
    text, warnings = render_log(as_of, res["data_end"], res["lines"], old)
    res["text"], res["warnings"] = text, warnings
    for w in warnings:
        print("WARNING:", w)
    if dry_run:
        print(text)
    else:
        log_path.write_text(text)
        print(f"wrote {log_path}")
    return res


def by_date(s: pd.Series) -> dict:
    return {str(i.date()): float(x) for i, x in s.items()}


def run_details(res: dict) -> dict:
    """Daily returns / exposures and trades per line (returns only, no price levels)."""
    out = {"as_of": res["as_of"], "data_end": res["data_end"], "lines": {}}
    for k, v in res["lines"].items():
        if not v.get("started"):
            continue
        if k == "B3":
            out["lines"][k] = {s: by_date(v[s]) for s in ("nav", "nav_ibkr", "qqq", "oneq")}
            out["lines"][k]["held"] = [[str(v["mk"].sessions[t].date()), names] for t, names in v["res"]["held"]]
            out["lines"][k]["acc"] = v["res"]["acc"]
            continue
        out["lines"][k] = {s: by_date(v[s]) for s in ("ret", "hbase", "hmatch", "hmatch_full_window", "oneq")}
        out["lines"][k]["w"] = v["w"]
        out["lines"][k]["trades"] = [{kk: (str(vv.date()) if isinstance(vv, pd.Timestamp) else vv)
                                      for kk, vv in t.items()} for t in v["trades"]]
    return out


def save_run_details(res: dict, runs_dir: Path = RUNS) -> Path:
    """``run_details`` for audit, local only (the cache, not Git)."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    p = runs_dir / f"asof_{res['as_of']}.json"
    p.write_text(json.dumps(run_details(res), indent=1, default=str))
    return p


def smisp_fetch(as_of: str, raw_dir: Path = RAW, smisp_dir: Path | None = None) -> dict:
    """B3 inputs for every due signal (checklist B3.3): Nasdaq listing snapshot, SEC tickers / submissions /
    companyfacts, charts of the universe base (Alpaca first, Yahoo fallback; resumable), then the charts of the
    held names and the EFFR."""
    base = Path(smisp_dir or SMISP_DIR)
    qqq = load_market("QQQ", as_of, raw_dir)
    signals = {}
    for s in fs.signal_dates(qqq.s.sessions):
        try:
            sc = fs.prepare_signal(s, qqq, base, fetch=True, state=SMISP_STATE)
        except (RuntimeError, OSError) as exc:      # network / SEC errors: nothing is frozen; re-run resumes
            print(f"S-MISP signal {s.date()}: {exc}")
            sc = None
        if sc is None:
            break
        signals[s] = sc
    if signals:
        print("S-MISP held charts:", fs.fetch_held(signals, base / "held"))
        fs.fetch_effr(base / "EFFR_nyfed.csv",
                      start=str((pd.Timestamp(fs.FIRST_SIGNAL) - pd.Timedelta(days=40)).date()))
    else:
        print(f"S-MISP: no signal due through {as_of} (first signal {fs.FIRST_SIGNAL}, known once a later session "
              "exists)")
    return signals


def chained(r: pd.Series) -> float:
    return (1 + r).prod() - 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--as-of", help="last date to use (YYYY-MM-DD); default: the last complete New York session")
    ap.add_argument("--fetch", action="store_true",
                    help="B1 / B2: refresh the 20 price series (QQQ, ONEQ, 18 stocks): Tiingo from 2026-11, Yahoo "
                         "fallback (one request per 2 s); only a recent tail, the history comes from the committed "
                         "returns-only state")
    ap.add_argument("--fetch-smisp", action="store_true",
                    help="B3: compute any due S-MISP signal from fresh data (Nasdaq list, SEC, ~2,400 charts: Alpaca "
                         "first, Yahoo fallback at one request per 2 s; resumable) and refresh the held names' charts "
                         "and the EFFR")
    ap.add_argument("--dry-run", action="store_true", help="print the log instead of writing it")
    ap.add_argument("--force", action="store_true", help="allow writing the log for an earlier as-of date")
    ap.add_argument("--no-overlap-check", action="store_true",
                    help="skip the console comparison with the local research caches")
    a = ap.parse_args(argv)
    latest = last_complete_session_date()
    as_of = a.as_of or latest
    pd.Timestamp(as_of)  # validates the format
    if pd.Timestamp(as_of) > pd.Timestamp(latest):
        print(f"as-of {as_of} is after the last complete session date {latest}; using {latest}")
        as_of = latest
    if a.fetch:
        print("FETCH:", fetch())
    if a.fetch_smisp:
        smisp_fetch(as_of)
    res = run(as_of, dry_run=a.dry_run, force=a.force)
    checks = res["data"].checks
    print("DATA:", json.dumps({k: (v["first"], v["last"], v["rows"]) for k, v in checks.items()}))
    bad = {k: v for k, v in checks.items() if v["ohlc_missing_or_nonpositive"] or v["open_or_close_outside_high_low"]
           or v["ohlc_filled_from_close"]}
    if bad:
        print("DATA FLAGS:", json.dumps(bad))
    if not a.no_overlap_check:
        print("OVERLAP vs research caches (split-adjusted closes through", st.END + "):",
              json.dumps(overlap_check(res["data"]), default=str))
    for k, v in res["lines"].items():
        if k == "B3" and v.get("started"):
            print(f"B3: chained strategy {pct(v['nav'].iloc[-1] / v['nav'].iloc[0] - 1)}, QQQ "
                  f"{pct(v['qqq'].iloc[-1] - 1)}, ONEQ {pct(v['oneq'].iloc[-1] - 1)}; costs {v['res']['acc']}; "
                  f"IBKR borrow snapshots available: {v['n_snapshots']}")
        elif k == "B3":
            print("B3:", v.get("reason"))
        elif v.get("started"):
            idx = v["ret"].index
            print(f"{k}: chained strategy {pct(chained(v['ret']))}, H_match (to-date w per month) "
                  f"{pct(chained(v['hmatch'].reindex(idx)))}, H_match (one w over the whole window, research "
                  f"definition; console only) {pct(chained(v['hmatch_full_window'].reindex(idx)))}")
    if any(v.get("started") for v in res["lines"].values()):
        print("run details (local only):", save_run_details(res))
    else:
        print(f"observation starts {START_CLOSE}: no forward sessions through {res['data_end']}")


if __name__ == "__main__":
    main()
