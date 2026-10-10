"""Spin-off effect, 2012-2026 (docs/research_ledger_spinoffs.md; rules registered in its section 0).

Stages (each reads what the one before wrote):
  events      SEC event list (+ hand_review.csv, SEC shares outstanding) -> output/research_only/spinoffs/events.csv
              (quant.data.spinoff_events; ``finalize`` re-applies the hand review to the cached raw table)
  prices      price series per spun-off company / parent / benchmark ETF (data v2 panel, Yahoo, WIKI)
              -> research_cache/spinoffs/series/ (local only); coverage -> price_coverage.csv
  archive     archive.org Yahoo history pages for the series still missing -> price_coverage_archive.csv
  crosscheck  the event list against archived CSD ETF holdings -> csd_crosscheck.csv
  run         SP1-SP4 + SP1_d1 on H1 and H2, Nasdaq subset, BHAR diagnostics -> output/research_only/spinoffs/

Usage::

    PYTHONPATH=. python scripts/research_spinoffs.py events
    PYTHONPATH=. python scripts/research_spinoffs.py prices
    PYTHONPATH=. python scripts/research_spinoffs.py archive
    PYTHONPATH=. python scripts/research_spinoffs.py crosscheck
    PYTHONPATH=. python scripts/research_spinoffs.py run

Vendor prices never leave research_cache/; outputs carry returns, dates and SEC facts only.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from quant.backtest.costs import ibkr_order_total as order_cost, volume_tier_half_spread as half_spread  # noqa: F401
from quant.data.sources import sec_cache as sec
from quant.data.sources.daily_series import series_wiki, series_yahoo
from quant.paths import ROOT

OUT = ROOT / "output/research_only/spinoffs"
EVENTS = OUT / "events.csv"
SERIES = sec.CACHE / "series"
MAIN = Path("/Users/bytedance/code/quant_stocks")   # the main checkout (research_cache)
V2_PRICES = MAIN / "research_cache/reversal_2012_2026_v2/prices"
V2_INPUTS = ROOT / "output/research_only/reversal_2012_2026/inputs_v2"
BENCH_DIR = MAIN / "research_cache/benchmarks"
WIKI_END = "2018-03-27"

ACCOUNT = 10_000.0
SLOTS = 10
BAND = 0.25                 # monthly rebalance band (fraction of target)
D5_RETURN = -0.55           # delisting with no terminal value (data plan section 0, D5)
BONFERRONI_T = 2.50         # two-sided 5% over 4 rules, reported only
HALVES = {"H1": ("2012-01-01", "2018-12-31"), "H2": ("2019-01-01", "2026-09-30")}
SIZE_ETF = ((2e9, "IWM"), (1e10, "IJH"), (float("inf"), "SPY"))
ONEQ_HALF_SPREAD = 2e-4


@dataclass(frozen=True)
class Config:
    code: str
    who: str = "spinco"       # spinco | parent
    entry_day: int = 5        # n-th regular-way session (1 = first)
    months: int = 12
    min_mcap: float = 0.0
    judged: bool = True


CONFIGS = [Config("SP1"), Config("SP2", months=24), Config("SP3", who="parent"), Config("SP4", min_mcap=1e9),
           Config("SP1_d1", entry_day=1)]
RULES = ("SP1", "SP2", "SP3", "SP4")


# ======================================================================== costs


# ======================================================================== price series


def series_v2(cik: int) -> pd.DataFrame | None:
    p = V2_PRICES / f"{int(cik)}.csv"
    if not p.exists():
        return None
    v = pd.read_csv(p)
    df = pd.DataFrame({"date": pd.to_datetime(v["date"]), "close_raw": v["close_raw"], "volume_raw": v["volume_raw"],
                       "tr": v["tr"]})
    df["source"] = "v2:" + str(int(cik))
    return df.dropna(subset=["close_raw"]).sort_values("date")


def choose_series(cands: list[pd.DataFrame], start: pd.Timestamp, window_days: int = 760,
                  max_lead_days: int = 30, max_lag_days: int = 30, must_start_near: bool = True):
    """The candidate with the most rows in [start, start + window_days], whose first row is near ``start``
    (an earlier start means another company under the same ticker, unless the rows start at most
    ``max_lead_days`` before: when-issued trading). Precedence v2 > yahoo > wiki on ties (list order)."""
    best, best_n, why = None, 0, []
    end = start + pd.Timedelta(days=window_days)
    for c in cands:
        if c is None or c.empty:
            continue
        first = c["date"].iloc[0]
        if must_start_near and (first < start - pd.Timedelta(days=max_lead_days) or
                                first > start + pd.Timedelta(days=max_lag_days)):
            why.append(f"{c['source'].iloc[0]} first row {first.date()}")
            continue
        n = int(((c["date"] >= start) & (c["date"] <= end)).sum())
        if n > best_n:
            best, best_n = c, n
    return best, why


def build_series(events: pd.DataFrame, offline: bool = False) -> pd.DataFrame:
    """Price series for every spun-off company, parent and benchmark ETF -> SERIES/{key}.csv.gz; returns a
    status table (no prices)."""
    SERIES.mkdir(parents=True, exist_ok=True)
    status = []
    for etf in ("IWM", "IJH", "SPY", "QQQ", "ONEQ"):
        s = series_yahoo(etf, offline=offline)
        if s is not None:
            s.to_csv(SERIES / f"etf_{etf}.csv.gz", index=False)
    for e in events.itertuples():
        if e.classification != "spin_off" or not e.first_regular_way_date:
            continue
        start = pd.Timestamp(e.first_regular_way_date)
        dist = pd.Timestamp(e.distribution_date)
        for who in ("spinco", "parent"):
            cik = e.spinco_cik if who == "spinco" else e.parent_cik
            if not cik or cik != cik:
                status.append({"event_id": e.event_id, "who": who, "status": "no_cik"})
                continue
            syms = [x for x in str(e.spinco_tickers_try if who == "spinco" else e.parent_tickers_try).split("|")
                    if x and x != "nan"]
            cands = [series_v2(int(cik))]
            cands += [series_yahoo(s, offline=offline) for s in syms]
            cands += [series_wiki(s) for s in syms]
            if who == "spinco":
                best, why = choose_series(cands, dist)
            else:   # a parent has years of history: it only needs rows around the spin
                best, why = choose_series(cands, dist, must_start_near=False)
                if best is not None and best["date"].iloc[0] > dist - pd.Timedelta(days=10):
                    best, why = None, why + ["parent series starts after the spin"]
            key = f"{who}_{int(cik)}"
            if best is None:
                status.append({"event_id": e.event_id, "who": who, "key": key, "status": "no_prices",
                               "tried": "|".join(syms), "why": "; ".join(why)})
                continue
            b = best[best["date"] > dist].copy() if who == "spinco" else best.copy()
            b.to_csv(SERIES / f"{key}.csv.gz", index=False)
            status.append({"event_id": e.event_id, "who": who, "key": key, "status": "ok",
                           "source": b["source"].iloc[0], "first": b["date"].iloc[0].date().isoformat(),
                           "last": b["date"].iloc[-1].date().isoformat(), "rows": len(b),
                           "first_raw_lag_days": int((b["date"].iloc[0] - start).days),
                           "tried": "|".join(syms), "why": "; ".join(why)})
    st = pd.DataFrame(status)
    st.to_csv(OUT / "price_coverage.csv", index=False)
    return st


# ======================================================================== archive.org fill (owner approved)

WAYBACK_RPM = sec.Limiter(0.25)   # at most 15 requests a minute (archive.org refuses at about 30)
FILL_BULK = MAIN / "research_cache/reversal_2012_2026_v2_fill/bulk_captures.csv.gz"


def _wayback(url: str, sub: str, name: str) -> bytes | None:
    try:
        return sec.cached_get(url, sub, headers={"User-Agent": "Mozilla/5.0"}, limiter=WAYBACK_RPM, name=name,
                              retries=2)
    except Exception as exc:  # noqa: BLE001  (archive.org timeouts: reported, not fatal)
        print(f"  wayback {name}: {type(exc).__name__}", flush=True)
        return None


def archive_captures(ticker: str, bulk: pd.DataFrame) -> list[tuple[str, str, str, pd.Timestamp, pd.Timestamp]]:
    """(kind, stamp, original, covers_from, covers_to) of archived Yahoo history pages for ``ticker``:
    quote/T/history pages (2016 on, about a year of rows), and the old q/hp pages and table.csv files
    from data version 2's bulk index."""
    out = []
    url = (f"https://web.archive.org/cdx/search/cdx?url=finance.yahoo.com/quote/{ticker}/history&matchType=prefix"
           "&filter=statuscode:200&fl=timestamp,original,length&limit=5000")
    body = _wayback(url, "wayback_cdx", f"qh_{ticker}")
    for line in (body or b"").decode("utf-8", "ignore").splitlines():
        parts = line.split(" ")
        if len(parts) == 3 and parts[2].isdigit() and int(parts[2]) > 5000 and \
                f"/quote/{ticker.lower()}/history" in parts[1].lower():
            ts = pd.Timestamp(parts[0][:8])
            out.append(("qh", parts[0], parts[1], ts - pd.Timedelta(days=365), ts))
    for r in bulk[bulk.ticker == ticker].itertuples():
        ts = pd.Timestamp(r.stamp[:8])
        if r.kind == "csv":
            out.append(("csv", r.stamp, r.original, pd.Timestamp("1990-01-01"), ts))
        elif r.kind == "hp":
            out.append(("hp", r.stamp, r.original, ts - pd.Timedelta(days=95), ts))
    return out


def archive_series(ticker: str, need_from: pd.Timestamp, need_to: pd.Timestamp, bulk: pd.DataFrame,
                   max_pages: int = 6) -> pd.DataFrame | None:
    """Daily rows for [need_from, need_to] stitched from archived Yahoo pages (greedy cover, latest page first
    for each day; returns use Adj Close within one page)."""
    from scripts import reversal_data_v2_archive as arc
    caps = [c for c in archive_captures(ticker, bulk) if c[4] >= need_from and c[3] <= need_to + pd.Timedelta(days=30)]
    if not caps:
        return None
    need = pd.bdate_range(need_from, need_to)
    covered = pd.Series(False, index=need)
    chosen = []
    for _ in range(max_pages):
        best, gain = None, 0
        for c in caps:
            if c in chosen:
                continue
            g = int((~covered & (need >= c[3]) & (need <= c[4])).sum())
            if g > gain:
                best, gain = c, g
        if best is None or gain < 10:
            break
        chosen.append(best)
        covered |= (need >= best[3]) & (need <= best[4])
    pages = []
    for kind, stamp, orig, _, _ in chosen:
        data = _wayback(f"https://web.archive.org/web/{stamp}id_/{orig.replace('&amp;', '&')}", "wayback_pages",
                        f"{kind}_{ticker}_{stamp}")
        if not data:
            continue
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        rows, events = arc.parse_body(kind, data.decode("utf-8", "ignore"))
        if rows.empty:
            continue
        raw = arc.raw_from_capture(rows, events)
        raw["stamp"] = stamp
        pages.append(raw)
    if not pages:
        return None
    allp = pd.concat(pages)
    allp = allp[(allp.date >= need_from - pd.Timedelta(days=10)) & (allp.date <= need_to + pd.Timedelta(days=5))]
    out = []
    for d, g in allp.sort_values("stamp").groupby("date"):
        out.append(g.iloc[-1])
    df = pd.DataFrame(out).sort_values("date").reset_index(drop=True)
    # total return of day t from the newest page that holds both t-1 and t
    adj = {s: g.set_index("date")["adj"] for s, g in allp.groupby("stamp")}
    tr = [np.nan]
    for i in range(1, len(df)):
        d0, d1 = df.date.iloc[i - 1], df.date.iloc[i]
        r = np.nan
        for s in sorted(adj, reverse=True):
            a = adj[s]
            if d0 in a.index and d1 in a.index and a[d0] > 0:
                r = float(a[d1] / a[d0] - 1)
                break
        if r != r:   # no page holds both days: price return on raw closes
            r = float(df.close_raw.iloc[i] / df.close_raw.iloc[i - 1] - 1)
        tr.append(r)
    return pd.DataFrame({"date": pd.to_datetime(df.date), "close_raw": df.close_raw.astype(float),
                         "volume_raw": df.volume_raw.astype(float), "tr": tr, "source": "wayback:" + ticker})


def fill_from_archive(events: pd.DataFrame, limit: int = 0) -> pd.DataFrame:
    """Archive.org pages for every spun-off company / parent that build_series left without prices."""
    cov = pd.read_csv(OUT / "price_coverage.csv", dtype=str).fillna("")
    bulk = pd.read_csv(FILL_BULK, dtype=str) if FILL_BULK.exists() else pd.DataFrame(columns=["kind", "ticker"])
    ev = events.set_index("event_id")
    miss = cov[cov.status == "no_prices"]
    if limit:
        miss = miss.head(limit)
    rows = []
    for r in miss.itertuples():
        e = ev.loc[r.event_id]
        dist = pd.Timestamp(e.distribution_date)
        if r.who == "spinco":
            end = pd.Timestamp(e.delist_form25_date) if e.delist_form25_date else dist + pd.DateOffset(months=26)
            need_from = dist + pd.Timedelta(days=1)
        else:
            end = pd.Timestamp(e.parent_delist_form25_date) if e.parent_delist_form25_date else \
                dist + pd.DateOffset(months=14)
            need_from = dist - pd.Timedelta(days=40)
        need_to = min(end, dist + pd.DateOffset(months=26), pd.Timestamp(HALVES["H2"][1]))
        best = None
        for tk in [x for x in r.tried.split("|") if x]:
            s = archive_series(tk, need_from, need_to, bulk)
            if s is not None and (best is None or len(s) > len(best)):
                best = s
        sessions_needed = len(pd.bdate_range(need_from, need_to))
        if best is None or len(best) < 5:
            rows.append({"event_id": r.event_id, "who": r.who, "status": "no_archive", "need_sessions": sessions_needed})
            continue
        if r.who == "spinco":
            best = best[best.date > dist]
        best.to_csv(SERIES / f"{r.key}.csv.gz", index=False)
        rows.append({"event_id": r.event_id, "who": r.who, "status": "archive", "rows": len(best),
                     "need_sessions": sessions_needed, "first": best.date.iloc[0].date().isoformat(),
                     "last": best.date.iloc[-1].date().isoformat()})
        print(r.event_id, r.who, len(best), "/", sessions_needed, flush=True)
    out = pd.DataFrame(rows)
    out.to_csv(OUT / "price_coverage_archive.csv", index=False)
    return out


# ======================================================================== panel on sessions

@dataclass
class Asset:
    """One security on the segment's sessions: raw close (NaN where no row), daily total return (0 where no row),
    the last row's session index, the terminal return booked on the session after it (None: not delisted in data),
    and the median raw dollar volume of the regular-way rows so far (max 20)."""
    key: str
    px: np.ndarray
    ret: np.ndarray
    last_i: int
    terminal: float | None
    dv: np.ndarray


def load_series(key: str) -> pd.DataFrame | None:
    p = SERIES / f"{key}.csv.gz"
    if not p.exists():
        return None
    return pd.read_csv(p, parse_dates=["date"])


def make_asset(key: str, df: pd.DataFrame, sessions: pd.DatetimeIndex, terminal: float | None,
               data_end: pd.Timestamp, delist_date: pd.Timestamp | None = None) -> Asset:
    """``terminal`` applies only when the series ends before ``data_end`` and, where the Form 25 date is known,
    within 15 days of it (a real delisting). A series that stops earlier is a data gap: the position leaves at
    the last close (0%), counted as ``data_end``."""
    s = df.set_index("date")
    s = s[~s.index.duplicated(keep="last")]
    px = s["close_raw"].reindex(sessions).to_numpy(float)
    tr = s["tr"].reindex(sessions).fillna(0.0).to_numpy(float)
    dvol = (s["close_raw"] * s["volume_raw"]).rolling(20, min_periods=1).median().reindex(sessions).ffill()
    rows = np.where(~np.isnan(px))[0]
    last_i = int(rows[-1]) if len(rows) else -1
    ended = len(rows) and s.index[-1] < data_end - pd.Timedelta(days=7)
    if ended and delist_date is not None and s.index[-1] < delist_date - pd.Timedelta(days=15):
        terminal = None   # the data stop before the delisting: a gap, not the delisting
    return Asset(key, px, tr, last_i, terminal if ended else None, dvol.to_numpy(float))


# ======================================================================== engine

def add_months(d: pd.Timestamp, m: int) -> pd.Timestamp:
    return d + pd.DateOffset(months=m)


def simulate(plan: list[dict], assets: dict[str, Asset], sessions: pd.DatetimeIndex, oneq_ret: np.ndarray,
             oneq_px: np.ndarray, months: int, spread_mult: float = 1.0, slots: int = SLOTS) -> dict:
    """Daily engine at the close. ``plan``: candidates with ``key``, ``entry_i`` (session index), ``order``
    (FIFO tie-break), ``event_id``. Registered rules (ledger 0.4): up to ``slots`` positions at NAV/slots, first
    come first served, monthly rebalance with a 25% band, exits after ``months`` calendar months, idle money in
    ONEQ, cash above 2% of NAV swept into ONEQ every close."""
    n = len(sessions)
    by_day: dict[int, list[dict]] = {}
    for c in plan:
        by_day.setdefault(c["entry_i"], []).append(c)
    month_end = np.r_[sessions[1:].month != sessions[:-1].month, True]
    pos: dict[str, dict] = {}
    cash, oneq_sh, oneq_val = ACCOUNT, 0.0, 0.0
    navs, costs, expo, npos = np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n, int)
    trades, skipped_full, skipped_price, traded = [], 0, 0, 0.0

    def oneq_trade(dollars: float, i: int) -> float:
        """Buy (dollars > 0) or sell ONEQ whole shares; returns the cost."""
        nonlocal cash, oneq_sh, oneq_val
        p = oneq_px[i]
        sh = math.floor(dollars / p) if dollars > 0 else -math.ceil(-dollars / p)
        if dollars < 0:
            sh = max(sh, -math.floor(oneq_sh + 1e-9))
        if sh == 0:
            return 0.0
        c = order_cost(abs(sh), p, sh < 0, max(ONEQ_HALF_SPREAD * spread_mult, 0.005 / p * spread_mult))
        cash -= sh * p + c
        oneq_sh += sh
        oneq_val = oneq_sh * p
        return c

    def raise_cash(need: float, i: int) -> float:
        if cash >= need:
            return 0.0
        return oneq_trade(-(need - cash) * 1.01, i)

    for i in range(n):
        day_cost = 0.0
        # 1. returns of the day
        if i > 0 and oneq_sh:   # ONEQ marked at its close; its distributions (total return - price return) to cash
            gross = oneq_sh * oneq_px[i - 1] * (1.0 + oneq_ret[i])
            oneq_val = oneq_sh * oneq_px[i]
            cash += gross - oneq_val
        for k, p in list(pos.items()):
            a = assets[k]
            if i > p["entry_i"]:
                p["val"] *= 1.0 + a.ret[i]
            # 2. delisted in the data: terminal value on the session after the last row
            if i > a.last_i >= p["entry_i"]:
                term = a.terminal if a.terminal is not None else 0.0
                cash += p["val"] * (1.0 + term)
                trades.append({**p["info"], "exit": sessions[i].date().isoformat(), "exit_kind":
                               "delisted" if a.terminal is not None else "data_end",
                               "ret": (p["wd"] + p["val"] * (1 + term)) / p["inv"] - 1, "days": i - p["entry_i"]})
                del pos[k]
        # 3. planned exits
        for k, p in list(pos.items()):
            if i >= p["exit_i"]:
                a = assets[k]
                price = a.px[i] if a.px[i] == a.px[i] else a.px[a.last_i]
                sh = p["val"] / price
                c = order_cost(sh, price, True, half_spread(a.dv[i], price, spread_mult))
                cash += p["val"] - c
                day_cost += c
                traded += p["val"]
                trades.append({**p["info"], "exit": sessions[i].date().isoformat(), "exit_kind": "planned",
                               "ret": (p["wd"] + p["val"] - c) / p["inv"] - 1, "days": i - p["entry_i"]})
                del pos[k]
        nav = cash + oneq_val + sum(p["val"] for p in pos.values())
        # 4. entries
        for c_ in sorted(by_day.get(i, []), key=lambda c: c["order"]):
            k = c_["key"]
            a = assets.get(k)
            if k in pos:
                continue
            if len(pos) >= slots:
                skipped_full += 1
                continue
            if a is None or not a.px[i] == a.px[i]:
                skipped_price += 1
                continue
            price = a.px[i]
            sh = math.floor(nav / slots / price)
            if sh < 1:
                skipped_price += 1
                continue
            hs = half_spread(a.dv[i], price, spread_mult)
            c = order_cost(sh, price, False, hs)
            day_cost += raise_cash(sh * price + c, i)
            cash -= sh * price + c
            day_cost += c
            traded += sh * price
            exit_d = add_months(sessions[i], months)
            exit_i = int(sessions.searchsorted(exit_d, side="left"))
            pos[k] = {"val": sh * price, "entry_i": i, "exit_i": exit_i, "inv": sh * price + c, "wd": 0.0,
                      "info": {"event_id": c_["event_id"], "key": k, "entry": sessions[i].date().isoformat()}}
        # 5. monthly rebalance (sells first)
        if month_end[i] and pos:
            nav = cash + oneq_val + sum(p["val"] for p in pos.values())
            tgt = nav / slots
            orders = []
            for k, p in pos.items():
                a = assets[k]
                if not a.px[i] == a.px[i] or abs(p["val"] - tgt) <= BAND * tgt:
                    continue
                sh = math.floor(abs(tgt - p["val"]) / a.px[i]) * (1 if tgt > p["val"] else -1)
                if sh:
                    orders.append((sh, k))
            for sh, k in sorted(orders):
                a, p = assets[k], pos[k]
                price = a.px[i]
                c = order_cost(abs(sh), price, sh < 0, half_spread(a.dv[i], price, spread_mult))
                if sh > 0:
                    day_cost += raise_cash(sh * price + c, i)
                cash -= sh * price + c
                p["val"] += sh * price
                if sh > 0:
                    p["inv"] += sh * price + c
                else:
                    p["wd"] += -sh * price - c
                day_cost += c
                traded += abs(sh) * price
        # 6. sweep cash into ONEQ
        nav = cash + oneq_val + sum(p["val"] for p in pos.values())
        if cash > 0.02 * nav:
            day_cost += oneq_trade(cash - 0.005 * nav, i)
        nav = cash + oneq_val + sum(p["val"] for p in pos.values())
        navs[i], costs[i] = nav, day_cost
        expo[i] = sum(p["val"] for p in pos.values()) / nav if nav > 0 else 0.0
        npos[i] = len(pos)
    for k, p in pos.items():
        trades.append({**p["info"], "exit": "", "exit_kind": "open", "ret": (p["wd"] + p["val"]) / p["inv"] - 1,
                       "days": n - 1 - p["entry_i"]})
    return {"nav": pd.Series(navs, index=sessions), "cost": pd.Series(costs, index=sessions),
            "exposure": pd.Series(expo, index=sessions), "npos": pd.Series(npos, index=sessions),
            "trades": pd.DataFrame(trades), "traded": traded, "skipped_full": skipped_full,
            "skipped_price": skipped_price}


# ======================================================================== run

def load_events() -> pd.DataFrame:
    ev = pd.read_csv(EVENTS, dtype=str).fillna("")
    for c in ("spinco_cik", "parent_cik"):
        ev[c] = pd.to_numeric(ev[c], errors="coerce")
    return ev


def attach_mcap(ev: pd.DataFrame) -> pd.DataFrame:
    """Market cap at the spin (ledger 0.3): SEC shares outstanding x the raw close of the first regular-way
    session in the price series (the first row on or after it). Local only: it rests on a vendor price."""
    ev = ev.copy()
    caps = []
    for r in ev.itertuples():
        cap = ""
        if r.classification == "spin_off" and r.shares_outstanding and r.spinco_cik == r.spinco_cik:
            s = load_series(f"spinco_{int(r.spinco_cik)}")
            if s is not None and r.first_regular_way_date:
                s = s[s.date >= pd.Timestamp(r.first_regular_way_date)]
                if len(s) and (s.date.iloc[0] - pd.Timestamp(r.first_regular_way_date)).days <= 10:
                    cap = float(s.close_raw.iloc[0]) * float(r.shares_outstanding)
        caps.append(cap)
    ev["mcap_usd"] = caps
    return ev


def v2_terminals() -> dict[int, float]:
    """CIK -> terminal return from data version 2 (Nasdaq names), where it has one."""
    t = pd.read_csv(V2_INPUTS / "terminal_returns_2012_2026.csv", dtype=str)
    t = t[pd.to_numeric(t["terminal_return"], errors="coerce").notna()]
    out = {}
    for r in t.itertuples():
        try:
            out[int(str(r.security_id).split(".")[0])] = float(r.terminal_return)
        except ValueError:
            pass
    return out


def terminal_for(e, who: str, v2t: dict, d5: float) -> float | None:
    """Registered terminal rule (ledger 0.3) for the security's data end; None = no delisting on file."""
    if who == "parent":
        reason, cik = e.parent_delist_reason, e.parent_cik
    else:
        reason, cik = e.delist_reason, e.spinco_cik
    if cik == cik and int(cik) in v2t:
        return v2t[int(cik)]
    if reason == "merger":
        return 0.0
    if reason in ("bankruptcy", "other"):
        return d5
    return None


def oneq_and_bench(sessions: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    out = {}
    for etf in ("ONEQ", "QQQ", "IWM", "IJH", "SPY"):
        df = load_series(f"etf_{etf}")
        s = df.set_index("date")
        out[etf] = pd.DataFrame({"px": s["close_raw"].reindex(sessions).ffill(),
                                 "ret": s["tr"].reindex(sessions).fillna(0.0)})
    return out


def regular_way_index(sessions: pd.DatetimeIndex, first_rw: str, n: int) -> int | None:
    """Index of the n-th regular-way session; None when the first regular-way day is outside ``sessions``
    (an event before a segment must not be moved to the segment's first days)."""
    d = pd.Timestamp(first_rw)
    if d < sessions[0] or d > sessions[-1]:
        return None
    i = int(sessions.searchsorted(d, side="left"))
    j = i + n - 1
    return j if j < len(sessions) else None


def build_plan(ev: pd.DataFrame, cfg: Config, sessions: pd.DatetimeIndex, start: str, end: str,
               nasdaq_only: bool = False) -> list[dict]:
    plan, parents_seen = [], set()
    e = ev[(ev.classification == "spin_off") & (ev.first_regular_way_date != "")]
    if nasdaq_only:
        e = e[e.nasdaq_listed == "Y"]
    for r in e.sort_values(["first_regular_way_date", "event_id"]).itertuples():
        i = regular_way_index(sessions, r.first_regular_way_date, cfg.entry_day)
        if i is None or not (pd.Timestamp(start) <= sessions[i] <= pd.Timestamp(end)):
            continue
        if cfg.min_mcap and not (r.mcap_usd and float(r.mcap_usd) >= cfg.min_mcap):
            continue
        if cfg.who == "parent":
            if r.parent_cik != r.parent_cik:
                continue
            key = f"parent_{int(r.parent_cik)}"
            if (key, r.distribution_date) in parents_seen:
                continue
            parents_seen.add((key, r.distribution_date))
        else:
            key = f"spinco_{int(r.spinco_cik)}"
        mcap = float(r.mcap_usd) if r.mcap_usd else 0.0
        plan.append({"key": key, "entry_i": i, "event_id": r.event_id,
                     "order": (r.distribution_date, -mcap, r.event_id)})
    return plan


def segment_assets(ev: pd.DataFrame, sessions: pd.DatetimeIndex, d5: float, data_end: pd.Timestamp) -> dict:
    v2t = v2_terminals()
    assets = {}
    for r in ev[ev.classification == "spin_off"].itertuples():
        for who, cik in (("spinco", r.spinco_cik), ("parent", r.parent_cik)):
            if cik != cik:
                continue
            key = f"{who}_{int(cik)}"
            if key in assets:
                continue
            df = load_series(key)
            if df is None:
                continue
            dd = r.delist_form25_date if who == "spinco" else r.parent_delist_form25_date
            assets[key] = make_asset(key, df, sessions, terminal_for(r, who, v2t, d5), data_end,
                                     pd.Timestamp(dd) if dd else None)
    return assets


def metrics(res: dict, bench: dict, start: str, end: str) -> dict:
    from quant.strategies import indicators as ind
    nav = res["nav"]
    oneq = (1 + bench["ONEQ"]["ret"]).cumprod()
    oneq = oneq / oneq.iloc[0] * ACCOUNT
    qqq = (1 + bench["QQQ"]["ret"]).cumprod()
    qqq = qqq / qqq.iloc[0] * ACCOUNT
    m = ind.stock_metrics(nav, oneq, qqq, ACCOUNT, res["cost"], res["traded"], res["exposure"])
    crit = ind.criteria(m["cagr"], m["max_dd"], m["bench_cagr"], m["bench_max_dd"], m["t_excess_monthly"])
    m["crit_A"], m["crit_B"], m["crit_pass"] = crit["A"], crit["B"], crit["pass"]
    m["t_ge_bonferroni"] = bool(m["t_excess_monthly"] >= BONFERRONI_T)
    tr = res["trades"]
    m["entries"] = int(len(tr))
    m["skipped_full"], m["skipped_no_price"] = res["skipped_full"], res["skipped_price"]
    m["avg_positions"] = float(res["npos"].mean())
    if len(tr):
        m["trade_mean_ret"] = float(tr["ret"].mean())
        m["trade_median_ret"] = float(tr["ret"].median())
        m["trade_win_rate"] = float((tr["ret"] > 0).mean())
        m["exits_delisted"] = int((tr["exit_kind"] == "delisted").sum())
        m["exits_data_end"] = int((tr["exit_kind"] == "data_end").sum())
    return m


def _clean(m: dict) -> dict:
    return {k: v for k, v in m.items() if not k.startswith("_")}


def bhar_table(ev: pd.DataFrame, sessions: pd.DatetimeIndex, bench: dict, assets: dict) -> pd.DataFrame:
    """Event-time buy-and-hold returns (ledger 0.7): stock vs ONEQ and vs the size ETF, 6/12/24 months,
    from the close of regular-way day 5 (and day 1). After a delisting the money sits in the benchmark."""
    rows = []
    lv = {k: (1 + v["ret"]).cumprod().to_numpy() for k, v in bench.items()}
    for r in ev[(ev.classification == "spin_off") & (ev.first_regular_way_date != "")].itertuples():
        mcap = float(r.mcap_usd) if r.mcap_usd else float("nan")
        size_etf = "IWM" if mcap != mcap else next(e for cap, e in SIZE_ETF if mcap < cap)
        for who in ("spinco", "parent"):
            cik = r.spinco_cik if who == "spinco" else r.parent_cik
            if cik != cik or f"{who}_{int(cik)}" not in assets:
                continue
            a = assets[f"{who}_{int(cik)}"]
            for day in (1, 5):
                i0 = regular_way_index(sessions, r.first_regular_way_date, day)
                if i0 is None or not a.px[i0] == a.px[i0]:
                    continue
                for h in (6, 12, 24):
                    i1 = int(sessions.searchsorted(add_months(sessions[i0], h), side="left"))
                    if i1 >= len(sessions):
                        continue
                    seg = a.ret[i0 + 1: i1 + 1].copy()
                    out = {}
                    for b in ("ONEQ", size_etf):
                        bl = lv[b]
                        if a.last_i < i1:   # delisted (or data ends) inside the window
                            j = max(a.last_i, i0)
                            term = a.terminal if a.terminal is not None else 0.0
                            stock = np.prod(1 + a.ret[i0 + 1: j + 1]) * (1 + term) * bl[i1] / bl[j] - 1
                        else:
                            stock = float(np.prod(1 + seg)) - 1
                        out[b] = (stock, bl[i1] / bl[i0] - 1)
                    rows.append({"event_id": r.event_id, "who": who, "entry_day": day, "months": h,
                                 "half": "H1" if sessions[i0] <= pd.Timestamp(HALVES["H1"][1]) else "H2",
                                 "nasdaq": r.nasdaq_listed, "size_etf": size_etf,
                                 "ret": out["ONEQ"][0], "oneq": out["ONEQ"][1], "size_bench": out[size_etf][1],
                                 "ret_sizepath": out[size_etf][0], "delisted_in_window": bool(a.last_i < i1)})
    t = pd.DataFrame(rows)
    if len(t):
        t["bhar_oneq"] = t["ret"] - t["oneq"]
        t["bhar_size"] = t["ret_sizepath"] - t["size_bench"]
    return t


def bhar_summary(t: pd.DataFrame) -> pd.DataFrame:
    rows = []
    groups = [("all", t)] + [(f"half={h}", g) for h, g in t.groupby("half")] + \
             [(f"nasdaq={n}", g) for n, g in t.groupby("nasdaq")]
    for name, g in groups:
        for (who, day, h), x in g.groupby(["who", "entry_day", "months"]):
            for col in ("bhar_oneq", "bhar_size"):
                v = x[col].dropna()
                if len(v) < 2:
                    continue
                rows.append({"group": name, "who": who, "entry_day": day, "months": h, "vs": col, "n": len(v),
                             "mean": v.mean(), "median": v.median(),
                             "t_naive": v.mean() / v.std(ddof=1) * math.sqrt(len(v)) if v.std() > 0 else np.nan,
                             "share_positive": float((v > 0).mean()), "mean_raw_ret": x["ret"].mean()})
    return pd.DataFrame(rows)


def run() -> dict:
    ev = attach_mcap(load_events())
    summary = {"halves": HALVES, "configs": [c.__dict__ for c in CONFIGS], "results": {}}
    sp = ev[ev.classification == "spin_off"]
    has = lambda who, c: c == c and (SERIES / f"{who}_{int(c)}.csv.gz").exists()  # noqa: E731
    sp_prices = sp.spinco_cik.apply(lambda c: has("spinco", c))
    summary["data"] = {
        "events": int(len(sp)), "events_by_half": {h: int(((sp.first_regular_way_date >= a) &
                                                          (sp.first_regular_way_date <= b)).sum())
                                                   for h, (a, b) in HALVES.items()},
        "nasdaq_events": int((sp.nasdaq_listed == "Y").sum()),
        "spinco_with_prices": int(sp_prices.sum()),
        "spinco_without_prices": sp[~sp_prices].event_id.tolist(),
        "spinco_without_prices_delist_reason": sp[~sp_prices].delist_reason.replace("", "none").value_counts().to_dict(),
        "parent_cik_known": int(sp.parent_cik.notna().sum()),
        "parent_with_prices": int(sp.parent_cik.apply(lambda c: has("parent", c)).sum()),
        "mcap_known": int((sp.mcap_usd != "").sum()),
        "mcap_ge_1b": int(sp.mcap_usd.apply(lambda x: x != "" and float(x) >= 1e9).sum()),
    }
    print(json.dumps(summary["data"], default=str)[:600], flush=True)
    all_sessions = pd.DatetimeIndex(load_series("etf_ONEQ")["date"])
    rows, by_year = [], []
    (OUT / "daily_nav").mkdir(parents=True, exist_ok=True)
    (OUT / "trades").mkdir(parents=True, exist_ok=True)
    for half, (start, end) in HALVES.items():
        sessions = all_sessions[(all_sessions >= pd.Timestamp(start)) & (all_sessions <= pd.Timestamp(end))]
        bench = oneq_and_bench(sessions)
        print(f"DATE GUARD {half}: sessions {sessions[0].date()}..{sessions[-1].date()}", flush=True)
        for d5, tag in ((D5_RETURN, ""), (-1.0, "_delist100")):
            assets = segment_assets(ev, sessions, d5, pd.Timestamp(HALVES["H2"][1]))
            for cfg in CONFIGS:
                for mult, stag in ((1.0, ""), (2.0, "_spread2")):
                    if tag and stag:
                        continue
                    plan = build_plan(ev, cfg, sessions, start, end)
                    res = simulate(plan, assets, sessions, bench["ONEQ"]["ret"].to_numpy(),
                                   bench["ONEQ"]["px"].to_numpy(), cfg.months, spread_mult=mult)
                    m = metrics(res, bench, start, end)
                    name = cfg.code + tag + stag
                    rows.append({"config": name, "half": half, **{k: v for k, v in _clean(m).items()
                                                                    if not isinstance(v, dict)}})
                    if not tag and not stag:
                        for y, v in m["by_year"].items():
                            by_year.append({"config": name, "half": half, "year": y, **v})
                        res["nav"].to_frame("nav").to_csv(OUT / "daily_nav" / f"{name}_{half}.csv")
                        res["trades"].to_csv(OUT / "trades" / f"{name}_{half}.csv", index=False)
            if not tag:   # Nasdaq-only SP1 (report only)
                plan = build_plan(ev, CONFIGS[0], sessions, start, end, nasdaq_only=True)
                res = simulate(plan, assets, sessions, bench["ONEQ"]["ret"].to_numpy(), bench["ONEQ"]["px"].to_numpy(), 12)
                m = metrics(res, bench, start, end)
                rows.append({"config": "SP1_nasdaq_only", "half": half,
                             **{k: v for k, v in _clean(m).items() if not isinstance(v, dict)}})
    res_df = pd.DataFrame(rows)
    res_df.to_csv(OUT / "results.csv", index=False)
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", index=False)
    # verdicts
    verdict = {}
    for code in RULES:
        v = {}
        for half in HALVES:
            r = res_df[(res_df.config == code) & (res_df.half == half)].iloc[0]
            v[half] = {"pass": bool(r.crit_pass), "A": bool(r.crit_A), "B": bool(r.crit_B), "cagr": r.cagr,
                       "oneq_cagr": r.bench_cagr, "t": r.t_excess_monthly, "max_dd": r.max_dd,
                       "oneq_max_dd": r.bench_max_dd}
        v["rule"] = ("pass" if all(v[h]["pass"] for h in HALVES) else
                     "unstable" if any(v[h]["pass"] for h in HALVES) else "fail")
        verdict[code] = v
    summary["verdict"] = verdict
    # diagnostics on the full window
    sessions = all_sessions[(all_sessions >= pd.Timestamp(HALVES["H1"][0])) &
                            (all_sessions <= pd.Timestamp(HALVES["H2"][1]))]
    bench = oneq_and_bench(sessions)
    assets = segment_assets(ev, sessions, D5_RETURN, pd.Timestamp(HALVES["H2"][1]))
    bt = bhar_table(ev, sessions, bench, assets)
    bt.to_csv(OUT / "bhar_events.csv", index=False)
    bs = bhar_summary(bt)
    bs.to_csv(OUT / "bhar_summary.csv", index=False)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    return summary


# ======================================================================== cross-check

WAYBACK_CSD = {"20240412151605": "2024-04-10", "20250327042752": "2025-03-26"}
CSD_URL = ("https://www.invesco.com/us/financial-products/etfs/holdings/main/holdings/0?audienceType=Investor"
           "&action=download&ticker=CSD")


def csd_crosscheck(ev: pd.DataFrame) -> pd.DataFrame:
    """Holdings of the Invesco S&P Spin-Off ETF (CSD) in the archive.org captures (owner approved) against the
    event list: each holding is matched by ticker to a spun-off company (any window) or marked missing."""
    rows = []
    for ts, asof in WAYBACK_CSD.items():
        url = f"https://web.archive.org/web/{ts}id_/{CSD_URL}"
        data = sec.cached_get(url, "wayback", headers={"User-Agent": "Mozilla/5.0"}, limiter=sec.WAYBACK_LIMIT,
                              name=f"csd_{ts}")
        if not data:
            continue
        import io
        h = pd.read_csv(io.StringIO(data.decode("utf-8", "ignore")))
        for r in h.itertuples():
            tk = str(r[3]).strip()
            m = ev[ev.spinco_tickers_try.fillna("").str.split("|").apply(lambda xs: tk in xs) |
                   (ev.spinco_symbol == tk)]
            rows.append({"asof": asof, "holding_ticker": tk, "holding_name": r.Name,
                         "matched_event_id": "|".join(m.event_id) if len(m) else "",
                         "matched_classification": "|".join(m.classification) if len(m) else ""})
    out = pd.DataFrame(rows)
    out.to_csv(OUT / "csd_crosscheck.csv", index=False)
    return out


# ======================================================================== CLI

def stage_events(rebuild_raw: bool = True) -> pd.DataFrame:
    from quant.data import spinoff_events as evm
    raw_path = sec.CACHE / "events_raw.csv"
    raw = evm.build_events() if rebuild_raw or not raw_path.exists() else pd.read_csv(raw_path, dtype=str)
    raw.to_csv(raw_path, index=False)
    ev = evm.finalize(raw)
    OUT.mkdir(parents=True, exist_ok=True)
    ev.to_csv(EVENTS, index=False)
    print(ev.classification.value_counts().to_string())
    return ev


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["events", "finalize", "prices", "archive", "run", "crosscheck"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offline", action="store_true", help="prices: cache only")
    a = ap.parse_args(argv)
    if a.stage == "events":
        stage_events(True)
    elif a.stage == "finalize":
        stage_events(False)
    elif a.stage == "archive":
        f = fill_from_archive(load_events(), limit=a.limit)
        print(f.groupby(["who", "status"]).size().to_string() if len(f) else "nothing to fill")
    elif a.stage == "crosscheck":
        c = csd_crosscheck(load_events())
        print(c.to_string())
    elif a.stage == "prices":
        st = build_series(load_events(), offline=a.offline)
        print(st.groupby(["who", "status"]).size().to_string())
    else:
        s = run()
        print(json.dumps(s["verdict"], indent=1, default=str))


if __name__ == "__main__":
    main()
