"""Forward observation runner for the two frozen candidates of docs/forward_observation_checklist.md section B
(owner decision 2026-10-09). Not QuantConnect.

- B1: stock basket S3-Yb (18 names, one $10k cash account each), simulated by scripts/research_selective_t.py
  (``run_one`` with config "S3-Yb", asset class "stock", variant "net").
- B2: QQQ RSI2-dip, two separate lines SEL-A (config 29876) and SEL-P (config 29916), simulated by
  scripts/research_t_grid.py (``make_inst`` / ``exact_run`` / ``hmatch_series`` / ``hbase_of`` / ``oneq_for``).

No simulator logic is reimplemented here: this file only fetches and loads data the way the research scripts do,
chooses the forward window and turns the research functions' outputs into a monthly table.

Forward window: the accounts open at the 2026-10-12 close (base bought at that close, as the research ``simulate``
functions buy the base at the close of s-1), so the first forward return session is the next session. Indicators
use the full history (QQQ from 1999 for RSI2's expanding percentiles, stocks from 2011 for the SMA20); everything
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

Data: Yahoo v8 chart (daily OHLC, splits, dividends; owner accepted the Yahoo ToS risk), fetched with
``research_selective_t.chart_url`` / headers / stop codes, one request per 2 s, stop at the first 401/403/429.
Raw bodies only under research_cache/forward_observation/raw (never in Git). The committed log
docs/forward_observation_log.md holds returns in percent and dates only, never vendor price levels.

A month's row is final once the data hold a session of a later month (the frozen simulators treat the window's
last session specially); the month holding the last session is "provisional" and is replaced by the next run.

Usage (monthly, after 18:00 New York time on the first trading day of the new month or later, so that the month
just ended is final; see the checklist section B):
  PYTHONPATH=. .venv/bin/python scripts/forward_observation.py --fetch
  PYTHONPATH=. .venv/bin/python scripts/forward_observation.py --as-of 2026-11-30 --dry-run   # testing
"""
from __future__ import annotations

import argparse
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from scripts import research_intraday_t as it
from scripts import research_selective_t as st
from scripts import research_t_grid as tg
from scripts.research_calendar import t_and_ir
from scripts.research_qqq_timing import assert_dev_dates, monthly

ROOT = Path(__file__).resolve().parents[1]
CACHE = st.MAIN / "research_cache/forward_observation"
RAW = CACHE / "raw"
FETCH_LOG = CACHE / "fetch_log.csv"
RUNS = CACHE / "runs"
LOG = ROOT / "docs/forward_observation_log.md"

DECISION_CLOSE = "2026-10-09"       # last in-sample close (owner decision)
START_CLOSE = "2026-10-12"          # accounts open at this close
NY = ZoneInfo("America/New_York")
COMPLETE_HOUR_NY = 18               # a session counts as complete from 18:00 New York time

STOCKS = st.STOCKS
SYMBOLS = ("QQQ", "ONEQ") + STOCKS
FETCH_START = {"QQQ": "1999-01-01", "ONEQ": "2003-01-01"}   # stocks: st.FETCH_START (2011-01-01)

B1_CONFIG = "S3-Yb"
B2_IDS = {"SEL-A": 29876, "SEL-P": 29916}
B2_LABELS = {"SEL-A": "RSI2 p2 B OPP H10 1/2 res25 ALL", "SEL-P": "RSI2 p2 B OPP H20 1 res25 ALL"}
B2_RESERVE_CODE = 2                 # res25 -> H_base_res25
LINES = (("B1", "B1 S3-Yb (U18 basket)"), ("SEL-A", "B2 SEL-A (QQQ, config 29876)"),
         ("SEL-P", "B2 SEL-P (QQQ, config 29916)"))
VERDICT_MONTHS = (24, 36)


# ======================================================================== dates

def last_complete_session_date(now: datetime | None = None) -> str:
    """Today in New York if it is past 18:00 there, else yesterday (weekends / holidays have no rows anyway)."""
    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    d = now.date() if now.hour >= COMPLETE_HOUR_NY else now.date() - timedelta(days=1)
    return d.isoformat()


# ======================================================================== fetch (Yahoo v8, as research_selective_t)

def raw_path(sym: str, raw_dir: Path = RAW) -> Path:
    return raw_dir / f"{sym}.json"


def fetch(symbols=SYMBOLS, raw_dir: Path = RAW, log: Path = FETCH_LOG, getter=None, sleep=time.sleep) -> dict:
    """Re-download every symbol (the forward data grow each month); one request per 2 s; stop at 401/403/429.
    Bodies are stored as plain JSON (the QQQ / ONEQ loaders of the research code read plain JSON)."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    period2 = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    if not log.exists():
        log.write_text("fetched_utc,symbol,http_status,bytes\n")
    out = {}
    for k, sym in enumerate(symbols):
        if k:
            sleep(st.SECONDS_PER_REQUEST)
        url = st.chart_url(sym, FETCH_START.get(sym, st.FETCH_START), period2)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            if getter is not None:
                data, status = getter(url), 200
            else:
                with urlopen(Request(url, headers=st.HEADERS), timeout=30) as resp:
                    data, status = resp.read(), resp.status
        except HTTPError as exc:
            with log.open("a") as fh:
                fh.write(f"{now},{sym},{exc.code},0\n")
            out[sym] = f"HTTP {exc.code}"
            if exc.code in st.STOP_CODES:
                print(f"fetch: HTTP {exc.code} on {sym}; stopping (the cached files are left as they were)")
                break
            continue
        payload = json.loads(data)
        res = (payload.get("chart") or {}).get("result") or [None]
        if not res[0] or (res[0].get("meta") or {}).get("dataGranularity") != "1d":
            out[sym] = "not daily / empty"
        else:
            tmp = raw_path(sym, raw_dir).with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.rename(raw_path(sym, raw_dir))
            out[sym] = "ok"
        with log.open("a") as fh:
            fh.write(f"{now},{sym},{status},{len(data)}\n")
        print(f"fetch: {sym} {out[sym]} ({len(data)} bytes)")
    return out


# ======================================================================== load (as research_selective_t, end = as-of)

def load_market(sym: str, end: str, raw_dir: Path = RAW) -> st.Market:
    """QQQ / ONEQ as ``st.load_qqq_like``, stocks as ``st.load_stock``, truncated at ``end`` and asserted."""
    path = raw_path(sym, raw_dir)
    if sym in ("QQQ", "ONEQ"):
        df = it.parse_ohlc(path, end)
        assert_dev_dates(df["date"], end)
        return st.market_from_frame(sym, df, it.split_events(path, end))
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


def load_all(end: str, raw_dir: Path = RAW, stocks=STOCKS) -> Data:
    missing = [s for s in ("QQQ", "ONEQ", *stocks) if not raw_path(s, raw_dir).exists()]
    if missing:
        raise SystemExit(f"missing raw data for {missing} in {raw_dir}; run with --fetch")
    qqq, oneq = load_market("QQQ", end, raw_dir), load_market("ONEQ", end, raw_dir)
    out = Data(end, qqq, oneq, {s: load_market(s, end, raw_dir) for s in stocks})
    for sym, m in [("QQQ", qqq), ("ONEQ", oneq), *out.stocks.items()]:
        out.checks[sym] = {k: m.s.checks[k] for k in ("first", "last", "rows", "ohlc_missing_or_nonpositive",
                                                      "open_or_close_outside_high_low", "ohlc_filled_from_close")}
        if sym in out.stocks:
            extra = pd.DatetimeIndex(m.s.sessions).difference(qqq.s.sessions)
            if len(extra):
                raise ValueError(f"{sym} sessions not in the QQQ calendar: {list(extra[:5])}")
    return out


def overlap_check(d: Data, ref_end: str = st.END) -> dict:
    """Fresh split-adjusted closes vs the research caches on common dates through ``ref_end`` (console only)."""
    out = {}
    for sym, m in [("QQQ", d.qqq), ("ONEQ", d.oneq), *d.stocks.items()]:
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


# ======================================================================== forward window

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


# ======================================================================== B1: S3-Yb (research_selective_t)

def zero_rf(index) -> pd.Series:
    """The T-bill only enters margin accounts (H100) in the research code; cash accounts and w <= 1 ignore it."""
    return pd.Series(0.0, index=index)


def run_b1(d: Data) -> dict:
    rf = zero_rf(d.qqq.s.sessions)
    per, ref_cache = {}, {}
    for sym, m in d.stocks.items():
        w = window_of(m, d.end)
        if not w.started:
            continue
        o = st.run_one(m, w.s, w.e, B1_CONFIG, "stock", "net", rf, ref_cache)
        hm_parts, wm = [], {}
        for k in month_ends(w.sessions):
            em = w.s + k
            om = o if em == w.e else st.run_one(m, w.s, em, B1_CONFIG, "stock", "net", rf, ref_cache)
            mon = w.sessions[k].to_period("M")
            hm = om["hmatch"]
            hm_parts.append(hm[hm.index.to_period("M") == mon])
            wm[str(mon)] = om["sim"]["exposure"]
        tr = o["sim"]["trips"].assign(symbol=sym)
        per[sym] = {"ret": o["ret"], "hbase": o["hbase"], "hmatch": pd.concat(hm_parts), "w": wm, "trips": tr,
                    "exposure": o["sim"]["exposure"], "hmatch_full_window": o["hmatch"]}
    if not per:
        return {"started": False}
    ret = st.basket_mean([p["ret"] for p in per.values()])
    out = {"started": True, "ret": ret, "hbase": st.basket_mean([p["hbase"] for p in per.values()]),
           "hmatch": st.basket_mean([p["hmatch"] for p in per.values()]),
           "hmatch_full_window": st.basket_mean([p["hmatch_full_window"] for p in per.values()]),
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


def _acc(ex: dict, k: int) -> dict:
    return {key: float(v[:, k].sum()) for key, v in ex["sim"]["acc"].items()}


def run_b2(d: Data) -> dict:
    w = window_of(d.qqq, d.end)
    if not w.started:
        return {name: {"started": False} for name in B2_IDS}
    data = b2_data(d)
    ids = list(B2_IDS.values())
    g = tg.grid()
    for name, i in B2_IDS.items():
        assert tg.label(g.loc[i]) == B2_LABELS[name], (name, tg.label(g.loc[i]))
        assert int(g.loc[i, "reserve"]) == B2_RESERVE_CODE
    start = str(w.sessions[0].date())
    full = tg.exact_run(data, "QQQ", ids, start, str(w.sessions[-1].date()), tg.CostModel())
    # one truncated exact run per forward session: the research accumulators (trips closed by target / time stop,
    # and the 'end' close of a trade still open at that session's close) give the trade dates and net returns
    daily = []
    for k in range(len(w.sessions)):
        ex = full if k == len(w.sessions) - 1 else tg.exact_run(data, "QQQ", ids, start, str(w.sessions[k].date()),
                                                                tg.CostModel())
        daily.append(ex)
    me = set(month_ends(w.sessions))
    out = {}
    for j, (name, i) in enumerate(B2_IDS.items()):
        r = full["R"][i]
        hm_parts, wm = [], {}
        for k in sorted(me):
            ex = daily[k]
            mon = w.sessions[k].to_period("M")
            hm = tg.hmatch_series(ex, j)
            hm_parts.append(hm[hm.index.to_period("M") == mon])
            x = ex["sim"]["rec"]["x"][:, :, j]
            wm[str(mon)] = float((x * ex["act"]).sum() / ex["act"].sum())
        out[name] = {"started": True, "config_id": i, "label": B2_LABELS[name], "ret": r,
                     "hbase": tg.hbase_of(full, B2_RESERVE_CODE), "hmatch": pd.concat(hm_parts), "w": wm,
                     "hmatch_full_window": tg.hmatch_series(full, j),
                     "oneq": tg.oneq_for(data, r.index), "trades": b2_trades(daily, w.sessions, j),
                     "n_trips_full": int(_acc(full, j)["nT"])}
        if out[name]["oneq"] is None:   # a one-session window: ONEQ bought at the previous close, one return
            out[name]["oneq"] = it.buy_hold_oneq(d.oneq.s, data["master"], r.index[0], r.index[-1], True)
    return out


def b2_trades(daily: list, sessions: pd.DatetimeIndex, j: int) -> list:
    """Trades from the research accumulators of the truncated runs (run k ends at session k)."""
    trades, cur, closed_net, prev = [], None, 0.0, {"nT": 0, "nTarget": 0, "nTimeout": 0, "sDays": 0.0}
    for k, ex in enumerate(daily):
        a = _acc(ex, j)
        done = a["nTarget"] + a["nTimeout"]
        n_open = a["nT"] - done
        if done > prev["nTarget"] + prev["nTimeout"]:          # a trade closed today by OPP or the time stop
            if cur is None:
                raise ValueError(f"trade closed on {sessions[k].date()} without an observed entry")
            cur.update({"exit": sessions[k], "open": False,
                        "reason": "target" if a["nTarget"] > prev["nTarget"] else "timeout",
                        "days": int(round(a["sDays"] - prev["sDays"])), "net_pct": (a["sNet"] - closed_net) / 100})
            trades.append(cur)
            closed_net, cur = a["sNet"], None
            prev = {kk: a[kk] for kk in prev}
        if n_open > 1:
            raise ValueError("more than one open trade")
        if n_open == 1:
            if cur is None:
                cur = {"symbol": "QQQ", "entry": sessions[k]}
            cur.update({"exit": sessions[k], "open": True, "reason": "end", "days": None,
                        "net_pct": (a["sNet"] - closed_net) / 100})
    if cur is not None:
        trades.append(cur)
    return trades


# ======================================================================== monthly table

def pct(x: float, nd: int = 2) -> str:
    return "" if x is None or not np.isfinite(x) else f"{x * 100:+.{nd}f}%"


def pp(x: float) -> str:
    return "" if x is None or not np.isfinite(x) else f"{x * 100:+.2f}"


def trade_cell(trades: list, mon: pd.Period, show_symbol: bool) -> str:
    """Trades that closed in the month, and trades still open at the month's last forward session."""
    parts = []
    for tr in sorted(trades, key=lambda x: (x["entry"], x.get("symbol", ""))):
        ent, ex = tr["entry"], tr["exit"]
        if ent.to_period("M") > mon:
            continue
        closed_in = (not tr["open"]) and ex.to_period("M") == mon
        open_at_end = ent.to_period("M") <= mon and (tr["open"] or ex.to_period("M") > mon)
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
    """One row per forward month. A month is final once the data hold a later session: the frozen simulators treat
    the window's last session specially (open trades closed at its close, no month-end reset, no new signal), so
    the month that contains the last session of the data is provisional and is recomputed by the next run."""
    r, hb, hm, oq = line["ret"], line["hbase"], line["hmatch"], line["oneq"]
    mr, mb, mh, mo = monthly(r), monthly(hb), monthly(hm.reindex(r.index)), monthly(oq.reindex(r.index))
    cum_r, cum_h = (1 + mr).cumprod() - 1, (1 + mh).cumprod() - 1
    last_month = r.index[-1].to_period("M")
    rows = []
    for n, mon in enumerate(mr.index, start=1):
        days = r.index[r.index.to_period("M") == mon]
        status = "final" if mon < last_month else "provisional"
        ex_m = mr[:mon] - mh[:mon]
        t = t_and_ir(ex_m)[0] if len(ex_m) >= 3 else float("nan")
        rows.append({"month": str(mon), "n": n, "trades": trade_cell(line["trades"], mon, show_symbol),
                     "ret": mr[mon], "hbase": mb[mon], "hmatch": mh[mon], "w": line["w"].get(str(mon), float("nan")),
                     "oneq": mo[mon], "excess": mr[mon] - mh[mon], "cum_excess": cum_r[mon] - cum_h[mon], "t": t,
                     "sessions": f"{days[0]:%m-%d}..{days[-1]:%m-%d} ({len(days)})", "status": status})
    return rows


HEAD = ("| month | n | T-trades | strategy | H_base | H_match | w | ONEQ | excess vs H_match (pp) "
        "| cum. excess vs H_match (pp) | t | sessions | status |")
SEP = "|" + "---|" * 13


def row_line(x: dict) -> str:
    t = "" if not np.isfinite(x["t"]) else f"{x['t']:+.2f}"
    return (f"| {x['month']} | {x['n']} | {x['trades']} | {pct(x['ret'])} | {pct(x['hbase'])} | {pct(x['hmatch'])} | "
            f"{x['w']:.3f} | {pct(x['oneq'])} | {pp(x['excess'])} | {pp(x['cum_excess'])} | {t} | {x['sessions']} | "
            f"{x['status']} |")


# ======================================================================== log file

LOG_HEADER = """# Forward observation log: B1 S3-Yb, B2 SEL-A / SEL-P

Written by `scripts/forward_observation.py` (frozen rules: `docs/forward_observation_checklist.md` section B; owner
decision 2026-10-09). Do not edit the tables by hand: each run recomputes every forward month from the start and
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
"""


def render_status(as_of: str, data_end: str | None, lines: dict) -> str:
    out = ["## Status", "", f"- As-of date of the last run: {as_of}; data through: {data_end or 'n/a'}."]
    if not any(v.get("started") for v in lines.values()):
        out.append(f"- **observation starts {START_CLOSE}** (accounts open at the {START_CLOSE} close). "
                   "No forward sessions yet, so no trades and no returns.")
        return "\n".join(out) + "\n"
    for key, title in LINES:
        v = lines.get(key, {})
        if not v.get("started"):
            out.append(f"- {title}: no forward sessions yet.")
            continue
        rows = v["rows"]
        last = rows[-1]
        t = "" if not np.isfinite(last["t"]) else f", monthly-excess t {last['t']:+.2f}"
        out.append(f"- {title}: {last['n']} month(s) through {last['month']}; cumulative excess vs H_match "
                   f"{pp(last['cum_excess'])} pp{t}; no verdict before month {VERDICT_MONTHS[0]}, verdict at month "
                   f"{VERDICT_MONTHS[1]}.")
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


def render_log(as_of: str, data_end: str | None, lines: dict, old_text: str = "") -> tuple[str, list]:
    old = parse_rows(old_text)
    warnings = []
    parts = [LOG_HEADER, render_status(as_of, data_end, lines)]
    for key, title in LINES:
        rows = dict(old.get(key, {}))
        v = lines.get(key, {})
        for x in v.get("rows", []):
            new = row_line(x)
            prev = rows.get(x["month"])
            if prev is not None and prev != new and prev.rstrip().endswith("| final |"):
                warnings.append(f"{key} {x['month']}: a final row changed (vendor data revision?)\n  old {prev}\n"
                                f"  new {new}")
            rows[x["month"]] = new
        rule = (f"config `{B1_CONFIG}`, stock parameters (`scripts/research_selective_t.py`)" if key == "B1" else
                f"config {B2_IDS[key]} `{B2_LABELS[key]}` (`scripts/research_t_grid.py`)")
        parts.append(f"## {title}\n\nRule: {rule}.\n\n<!-- table:{key}:start -->\n{HEAD}\n{SEP}\n"
                     + "".join(rows[mo] + "\n" for mo in sorted(rows)) + f"<!-- table:{key}:end -->\n")
    text = "\n".join(parts)
    return text, warnings


# ======================================================================== run

def compute(as_of: str, raw_dir: Path = RAW, stocks=STOCKS) -> dict:
    """Everything for one as-of date (no file writes)."""
    d = load_all(as_of, raw_dir, stocks)
    data_end = str(d.qqq.s.sessions[-1].date())
    lines = {}
    b1 = run_b1(d)
    if b1["started"]:
        b1["rows"] = month_rows(b1, show_symbol=True)
    lines["B1"] = b1
    for name, v in run_b2(d).items():
        if v["started"]:
            v["rows"] = month_rows(v, show_symbol=False)
        lines[name] = v
    return {"as_of": as_of, "data_end": data_end, "lines": lines, "data": d}


def run(as_of: str, raw_dir: Path = RAW, log_path: Path = LOG, dry_run: bool = False, stocks=STOCKS,
        force: bool = False) -> dict:
    res = compute(as_of, raw_dir, stocks)
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


def save_run_details(res: dict, runs_dir: Path = RUNS) -> Path:
    """Daily returns / exposures per line (returns only) for audit, local only (research_cache, not Git)."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    out = {"as_of": res["as_of"], "data_end": res["data_end"], "lines": {}}
    for k, v in res["lines"].items():
        if not v.get("started"):
            continue
        out["lines"][k] = {s: {str(i.date()): float(x) for i, x in v[s].items()}
                           for s in ("ret", "hbase", "hmatch", "hmatch_full_window", "oneq")}
        out["lines"][k]["w"] = v["w"]
        out["lines"][k]["trades"] = [{kk: (str(vv.date()) if isinstance(vv, pd.Timestamp) else vv)
                                      for kk, vv in t.items()} for t in v["trades"]]
    p = runs_dir / f"asof_{res['as_of']}.json"
    p.write_text(json.dumps(out, indent=1, default=str))
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--as-of", help="last date to use (YYYY-MM-DD); default: the last complete New York session")
    ap.add_argument("--fetch", action="store_true", help="re-download the 20 Yahoo charts first (one per 2 s)")
    ap.add_argument("--dry-run", action="store_true", help="print the log instead of writing it")
    ap.add_argument("--force", action="store_true", help="allow writing the log for an earlier as-of date")
    ap.add_argument("--no-overlap-check", action="store_true")
    a = ap.parse_args(argv)
    latest = last_complete_session_date()
    as_of = a.as_of or latest
    pd.Timestamp(as_of)  # validates the format
    if pd.Timestamp(as_of) > pd.Timestamp(latest):
        print(f"as-of {as_of} is after the last complete session date {latest}; using {latest}")
        as_of = latest
    if a.fetch:
        print("FETCH:", fetch())
    res = run(as_of, dry_run=a.dry_run, force=a.force)
    d = res["data"]
    print("DATA:", json.dumps({k: (v["first"], v["last"], v["rows"]) for k, v in d.checks.items()}))
    bad = {k: v for k, v in d.checks.items() if v["ohlc_missing_or_nonpositive"] or v["open_or_close_outside_high_low"]
           or v["ohlc_filled_from_close"]}
    if bad:
        print("DATA FLAGS:", json.dumps(bad))
    if not a.no_overlap_check:
        print("OVERLAP vs research caches (split-adjusted closes through", st.END + "):",
              json.dumps(overlap_check(d), default=str))
    for k, v in res["lines"].items():
        if v.get("started"):
            hf = v["hmatch_full_window"].reindex(v["ret"].index)
            print(f"{k}: chained strategy {pct((1 + v['ret']).prod() - 1)}, H_match (to-date w per month) "
                  f"{pct((1 + v['hmatch'].reindex(v['ret'].index)).prod() - 1)}, H_match (one w over the whole window,"
                  f" research definition; console only) {pct((1 + hf).prod() - 1)}")
    if any(v.get("started") for v in res["lines"].values()):
        print("run details (local only):", save_run_details(res))
    else:
        print(f"observation starts {START_CLOSE}: no forward sessions through {res['data_end']}")


if __name__ == "__main__":
    main()
