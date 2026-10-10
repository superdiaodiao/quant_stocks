"""Price series of the spin-off study's companies (docs/research_ledger_spinoffs.md section 0.3): per spun-off company
and parent the best of the data v2 panel, Yahoo and WIKI series, and for the names still missing the archive.org
copies of Yahoo history pages (owner approved).

Moved unchanged from studies/spinoffs.py in the slimming pass (stages ``prices`` and ``archive``). Every folder (the
series cache, the v2 panel, the study output for the coverage tables, the v2 bulk-capture index) and the last
evaluated day are arguments declared by the study. Bodies are cached by quant.data.sources.sec_cache.
"""
from __future__ import annotations

import gzip
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data.sources import sec_cache as sec
from quant.data.sources.daily_series import series_v2, series_wiki, series_yahoo


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


def build_series(events: pd.DataFrame, series_dir: Path, v2_prices: Path, out_dir: Path,
                 offline: bool = False) -> pd.DataFrame:
    """Price series for every spun-off company, parent and benchmark ETF -> ``series_dir``/{key}.csv.gz; returns a
    status table (no prices), also written to ``out_dir``/price_coverage.csv."""
    series_dir.mkdir(parents=True, exist_ok=True)
    status = []
    for etf in ("IWM", "IJH", "SPY", "QQQ", "ONEQ"):
        s = series_yahoo(etf, offline=offline)
        if s is not None:
            s.to_csv(series_dir / f"etf_{etf}.csv.gz", index=False)
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
            cands = [series_v2(v2_prices, str(int(cik)))]
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
            b.to_csv(series_dir / f"{key}.csv.gz", index=False)
            status.append({"event_id": e.event_id, "who": who, "key": key, "status": "ok",
                           "source": b["source"].iloc[0], "first": b["date"].iloc[0].date().isoformat(),
                           "last": b["date"].iloc[-1].date().isoformat(), "rows": len(b),
                           "first_raw_lag_days": int((b["date"].iloc[0] - start).days),
                           "tried": "|".join(syms), "why": "; ".join(why)})
    st = pd.DataFrame(status)
    st.to_csv(out_dir / "price_coverage.csv", index=False)
    return st


# ======================================================================== archive.org fill (owner approved)

WAYBACK_RPM = sec.Limiter(0.25)   # at most 15 requests a minute (archive.org refuses at about 30)


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
    from quant.data.sources import archive_org as arc
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
        data = _wayback(arc.capture_url(stamp, orig), "wayback_pages",
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


def fill_from_archive(events: pd.DataFrame, series_dir: Path, out_dir: Path, fill_bulk: Path, last_day: str,
                      limit: int = 0) -> pd.DataFrame:
    """Archive.org pages for every spun-off company / parent that build_series left without prices (coverage in
    ``out_dir``; ``fill_bulk``: data version 2's bulk capture index; rows needed up to ``last_day``)."""
    cov = pd.read_csv(out_dir / "price_coverage.csv", dtype=str).fillna("")
    bulk = pd.read_csv(fill_bulk, dtype=str) if fill_bulk.exists() else pd.DataFrame(columns=["kind", "ticker"])
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
        need_to = min(end, dist + pd.DateOffset(months=26), pd.Timestamp(last_day))
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
        best.to_csv(series_dir / f"{r.key}.csv.gz", index=False)
        rows.append({"event_id": r.event_id, "who": r.who, "status": "archive", "rows": len(best),
                     "need_sessions": sessions_needed, "first": best.date.iloc[0].date().isoformat(),
                     "last": best.date.iloc[-1].date().isoformat()})
        print(r.event_id, r.who, len(best), "/", sessions_needed, flush=True)
    out = pd.DataFrame(rows)
    out.to_csv(out_dir / "price_coverage_archive.csv", index=False)
    return out
