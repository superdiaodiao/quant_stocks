"""Plan step 7 (docs/reversal_2012_2026_data_plan.md, sections 2, 3.2, 4.2, 4.4): Yahoo v8 charts.

Data only. Nothing here computes returns, signals, return rankings or spreads. The checks
are single-stock data flags: price levels against other sources, dollar volume against the
stored files, split restores, first trade dates and names.

Requests: one v8 chart per symbol for every ``candidate_fetch_list.csv`` row with
``planned_source == yahoo`` plus the V verification sample (which Tiingo also gets), from
2011-06-01 to the fetch day (``events=div,splits``, ``includeAdjustedClose=true``). The end is
the fetch day, not 2026-08-31, because Yahoo's ``close`` is adjusted for every split up to
today: a split after the window must be in the response for the raw restore to be right.
One request per 2 seconds; the first 429 (or 401/403) stops the run, and a re-run fetches only
what is missing (raw bodies are cached under ``CACHE/raw/yahoo/{SYMBOL}__{fetched_utc}.json.gz``,
404s as ``.404`` markers).

Round 6: a candidate ticker whose answer is a 404, has no daily rows or covers under half of the
need is asked again under the security's first SEC current ticker (``sec_alternates``; the last
snapshot's LIXT, EVTV, ATLN ... are NMAD, AZIO, CIRC ... now), and the better answer is used
(``resolve_alternates``). A relisting day the candidate list names (``junction_date``) is a junction
in the series when it also holds rows before it (Oasis/Chord), and any other single-day move of more
than 10x on a day without a split inside the need sends the series to review (``level_jumps``). The
summary's ``requests`` block counts every Yahoo request of the quota ledger, this step's and others'.

Only bodies of daily bars are used: Yahoo can answer with other bars (CRNX's range=max request
came back as 1h bars), so a body whose ``meta.dataGranularity`` is not ``1d`` (or, without that
field, with two bars on one New York date) is skipped.

Conversion to the canonical record (plan 4.2). Yahoo's ``close``, ``volume`` and dividend
``amount`` are split-adjusted to the fetch day, so with F_t = product of the split ratios
(numerator/denominator) whose ex-date is after t, and V_t the same product over the events
whose volume Yahoo scaled (below):
    close_raw = close x F_t,  volume_raw = volume / V_t,  div_cash = amount x F_t,
and ``split_factor`` S is the ratio on its ex-date (new shares per old). ``adjclose`` is kept as
served but not used (it is multiplicative; plan 4.2). A whole-number split that Yahoo lists but
has not yet applied to ``close`` (the served close moves by about 1/ratio on the ex-date; OPTT
1:30 on 2026-09-14) is left out of F and V and marked ``applied_by_yahoo = N``.

Odd ratios (not n:1, 1:n or n:m with n, m <= 10) are distributions or stock dividends that Yahoo
serves as splits (PENN 4.423 in 2013, HSIC 1275:1000, LGND 1603:1000). Each one is treated as
follows, and events.csv says which way per event:
- close and dividends: restored with the ratio (F); this matches WIKI raw closes and dividends;
- volume: for the 2013-2014 distributions WIKI can test (PENN, LBTYA, SLM, INVA, ENSG, WBD,
  ADP) Yahoo scaled close but served raw volume; CBSH's stock dividends and every tested event
  from 2015-07 on have both scaled. So the restored volume over the 60 sessions before the
  ex-date is compared with the security's WIKI raw volume (20+ common days): a median ratio
  near 1/ratio means Yahoo left volume as served, and the event is left out of V
  (``volume_restored = N``, flag
  ``volume_not_scaled_by_yahoo``); near 1 it stays in V (``Y``). Without WIKI in those 60
  sessions the event stays in V and is flagged ``volume_restore_unverified`` (named in the
  entity report's reasons, without changing the verdict: every WIKI-tested event from 2015-07
  on was scaled), unless its ratio is below 0.8, which only a reverse split gives (volume is
  scaled like a share split's). ``volume_evidence`` holds n days and the two median ratios;
- ``split_factor`` keeps the ratio, but it is not a share split: flag ``odd_ratio``, and step 9
  needs an SEC document before using it.

Rows of the Yahoo symbol that another security claims are cut (plan 4.4 R9): the other
securities fetched with the same symbol, the master's predecessors and successor, the other
classes of the security's multi-class group, and other holders of the ticker in step 6's
listing spans. On a side where such a claimant exists, rows outside the security's own listing
span and need are dropped (GOOGL before Alphabet's span is Google Inc; LMCK's FWONK rows after
2017-01 are 1560385.T-FWONK's). A side with no claimant keeps every row (a move from or to
another exchange). The first row after such a cut, like the first row of a later segment, is a
junction: events on it are flagged (``trim_junction`` / ``segment_junction``) and named in the
verdict reasons, because their S and D come from the history before it. A series with no row in
its need gets the verdict ``no_rows`` and goes to rejected/.

Entity check, per (security, symbol), against the security master and the files we hold:
- Yahoo ``firstTradeDate`` against the need and the first listing; first and last rows;
- the Yahoo name against the master name, former names and the snapshot names;
- raw close against the Wayback company lists' LastSale (2011-2019, 2% / 5% band as in step 6);
- raw close against the security's WIKI file (2011-06..2018-03-27);
- raw close x raw volume against the stored repo file's close x volume (median ratio near 1 for
  the same company). The stored files are older Yahoo downloads: their volume is Yahoo's served
  volume, so this tests the entity, not the volume restore (PENN's 2013 rows agree at 1.0000
  with both sides wrong by 4.423);
- instrument type, currency.
Level ratios are split into runs: a few steady runs off 1.0 are an adjustment Yahoo made without
listing it (IART's 2015 SeaSpine spin-off, CBSH's stock dividends), sent to review, and so is a
steady run more than 1% off LastSale even inside the 2% agreement band (FWONK and LMCK at
0.9833); unsteady runs are another share class or company (wrong_entity). A reused ticker that
fails is re-checked on the security's own listing span only (plan 4.4 R9). SYMBOL_OVERRIDES and
SEGMENTS record the routing corrections found by these checks (each documented where it is
defined).

Outputs (local only; vendor values never go to INPUTS):
  CACHE/yahoo/{security_id}.csv.gz  date, close_raw, volume_raw, split_factor, div_cash,
                                     close_yahoo, volume_yahoo, adjclose_yahoo, split_cum_after (F),
                                     volume_cum_after (V), symbol, junction (Y on the first row of
                                     a later segment or after a cut of claimed rows); factors are
                                     written to 10 significant digits, volumes as integers
                                     (verdict ok / partial / review; other series go to
                                     CACHE/yahoo/rejected/ for review only)
  CACHE/yahoo/weekly_coverage_after_yahoo.csv  per week: dv50 ranks 1-300 with vendor raw before / after
  CACHE/yahoo/events.csv            split and dividend events (as served and restored), how the
                                     close and volume restore treats each, flags
  CACHE/yahoo/fetch_status.csv      per symbol: status, http, raw file, rows
  CACHE/yahoo/entity_report.csv     per (security, symbol): checks and verdict
  CACHE/yahoo/summary.json          counts and coverage
  CACHE/yahoo/not_requested/        series of securities the candidate list no longer asks for
                                     (moved there by the build, not deleted)

Usage::

    PYTHONPATH=. python scripts/reversal_data_yahoo.py --fetch     # fetch what is missing, then build
    PYTHONPATH=. python scripts/reversal_data_yahoo.py             # build from the cache only
    PYTHONPATH=. python scripts/reversal_data_yahoo.py --fetch --limit 5
    PYTHONPATH=. python scripts/reversal_data_yahoo.py --retry CRNX,APGE   # ask truncated answers again
    PYTHONPATH=. python scripts/reversal_data_yahoo.py --retry CRNX --period1 2018-07-18   # daily bars over the need
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode

import numpy as np
import pandas as pd

from scripts import reversal_data_common as common

MAIN = common.MAIN_CHECKOUT
INPUTS = common.INPUTS
CANDIDATES = INPUTS / "candidate_fetch_list.csv"
MASTER = INPUTS / "security_master.csv"
INTERVALS = INPUTS / "ticker_intervals.csv"
PREFILTER = common.CACHE / "prefilter"
LISTS = PREFILTER / "lists.csv.gz"
FILES = PREFILTER / "files.csv.gz"
SPANS = PREFILTER / "spans.csv.gz"
WEEKLY_METRICS = PREFILTER / "weekly_metrics.pkl"
WIKI_DIR = common.CACHE / "wiki" / "by_ticker"
STORED_DIR = MAIN / "cleaned_stocks_data" / "price"
RAW_DIR = common.RAW / "yahoo"
OUT = common.CACHE / "yahoo"

SOURCE = "yahoo"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
LIMITER = common.SlidingWindowLimiter({2: 1})  # one request per 2 seconds
STOP_CODES = (401, 403, 429)
PERIOD1 = "2011-06-01"
WINDOW_START, WINDOW_END = "2011-06-01", "2026-08-31"
WIKI_END = "2018-03-27"
NY = "America/New_York"
ACCEPTED = ("ok", "partial", "review")  # verdicts whose series count as vendor raw


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


# ------------------------------------------------------------------ requests

# security_id -> (Yahoo symbol, why): where the candidate list's ticker is not this security's
# Yahoo symbol (found by the entity check of the first run; each costs one extra request).
SYMBOL_OVERRIDES: dict[str, tuple[str, str]] = {
    "1643953": ("PRPL", "V row carried the SPAC ticker GPAC, which Yahoo now gives to General Purpose "
                        "Acquisition Corp (first trade 2026-01-23); Purple Innovation trades as PRPL"),
    "1495222": ("OXLC", "V row carried OXLCN, one of Oxford Lane's notes; the common stock is OXLC"),
    "1570585.T-LILA": ("LILA", "LBTYA is Liberty Global class A (LastSale 4/56 agree); the LiLAC tracking "
                               "stock traded as LILA and was split off 1:1 into Liberty Latin America"),
    "1570585.T-LILAK": ("LILAK", "as LILA, class C: LILAK"),
    "1560385.T-LMCB": ("FWONB", "FWONA is series A (LastSale 8/44 agree); series B LMCB was renamed FWONB"),
    "1560385.T-LMCK": ("FWONK", "FWONA is series A; series C LMCK was renamed FWONK (Yahoo FWONK first trade "
                                "2014-07-08, the LMCK issue)"),
    "1437107.B": ("DISCB", "WBD carries Discovery series A history (LastSale 52/100 agree, unsteady); "
                           "series B traded as DISCB until the 2022-04 merger"),
}


# security_id -> [(Yahoo symbol, first day, last day, why)]: a security whose history Yahoo keeps
# under two symbols ('' is open-ended). Old IAC (CIK 891103) became Match Group at the 2020-06-30
# separation: Yahoo files old IAC's 2011-2020 history under the new IAC (now People Inc, PPLI;
# LastSale 100/100 and WIKI 100% agree) and MTCH before 2020-07-01 is the old Match Group.
SEGMENTS: dict[str, list[tuple[str, str, str, str]]] = {
    "891103": [("PPLI", "", "2020-06-30", "old IAC history is under the new IAC, now PPLI"),
               ("MTCH", "2020-07-01", "", "Match Group (this CIK) from the 2020-07-01 separation; "
                                          "MTCH before that is the old Match Group, CIK 1575189")],
}
REQUEST_COLUMNS = ["security_id", "symbol", "candidate_symbol", "segment_start", "segment_end", "needed_start",
                   "needed_end", "reasons", "active", "successor_routed", "note", "alt_symbols", "junction_date"]


def sec_alternates(master: pd.DataFrame) -> dict[str, list[str]]:
    """security -> its SEC current tickers on Nasdaq, NYSE or CBOE (warrants, units and rights dropped):
    the symbols asked when the candidate ticker's answer is poor (round 6: the last Nasdaq snapshot's
    LIXT, EVTV, ATLN, ALBT, LMFA, GREE, TBH, WGRX came back 404 while SEC lists NMAD, AZIO, CIRC, CHGA,
    PWCM, VIP, HODO, MEDS). Only the first one, which SEC lists for the common stock (GREE's CIK also
    has the notes GREEL). None for a tracking stock or a class of a multi-class company, whose CIK's
    tickers can name another class."""
    from scripts.reversal_data_prefilter import sec_current_tickers

    out = {}
    for row in master.fillna("").itertuples(index=False):
        if ".T-" in row.security_id or str(getattr(row, "multi_class_group", "") or ""):
            continue
        tickers = sec_current_tickers(row)
        if tickers:
            out[row.security_id] = tickers[:1]
    return out


def request_rows(candidates: pd.DataFrame, overrides: dict | None = None, segments: dict | None = None,
                 alternates: dict | None = None) -> pd.DataFrame:
    """One row per (security, symbol) to fetch: the Yahoo rows and the V sample. Columns:
    REQUEST_COLUMNS. ``overrides`` (default SYMBOL_OVERRIDES) replace the symbol; ``segments``
    (default SEGMENTS) split a security over several symbols, each with its own date window;
    ``alternates`` (security -> SEC current tickers, ``sec_alternates``) give ``alt_symbols``, asked
    and used only when the candidate ticker's answer is poor (``resolve_alternates``) and the
    candidate ticker is not one of them. ``junction_date``: a relisting day the candidate list
    names (the Yahoo series joins two listings there)."""
    overrides = SYMBOL_OVERRIDES if overrides is None else overrides
    segments = SEGMENTS if segments is None else segments
    alternates = alternates or {}
    frame = candidates.fillna("")
    if "junction_date" not in frame:
        frame["junction_date"] = ""
    wanted = frame[(frame["planned_source"] == "yahoo") | (frame["reason"] == "V_verify_sample")]
    rows = []
    for (sid, symbol), group in wanted.groupby(["security_id", "ticker_for_source"], sort=True):
        symbol = symbol.strip().upper()
        override, why = overrides.get(sid, (symbol, ""))
        notes = sorted(set(n for n in group["note"] if n)) + ([f"symbol {symbol} -> {override}: {why}"] if why else [])
        alt = [t for t in alternates.get(sid, []) if t != override] if (
            sid not in overrides and sid not in segments and override not in alternates.get(sid, [])) else []
        base = {"security_id": sid, "candidate_symbol": symbol,
                "needed_start": group["needed_start"].min(), "needed_end": group["needed_end"].max(),
                "reasons": " ".join(sorted(set(group["reason"]))), "active": group["active"].iloc[0],
                "successor_routed": "Y" if (group["active"] == "successor").any() else "",
                "alt_symbols": " ".join(alt), "junction_date": max(group["junction_date"])}
        if sid not in segments:
            rows.append({**base, "symbol": override, "segment_start": "", "segment_end": "", "note": " | ".join(notes)})
            continue
        for seg_symbol, first, last, reason in segments[sid]:
            start = max(base["needed_start"], first) if first else base["needed_start"]
            end = min(base["needed_end"], last) if last else base["needed_end"]
            if start > end:
                continue
            rows.append({**base, "symbol": seg_symbol, "segment_start": first, "segment_end": last,
                         "needed_start": start, "needed_end": end,
                         "note": " | ".join(notes + [f"segment {seg_symbol} {first or '..'}..{last or '..'}: {reason}"])})
    return pd.DataFrame(rows, columns=REQUEST_COLUMNS)


def _epoch(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def chart_url(symbol: str, period1: str, period2: str) -> str:
    """The v8 chart URL for [period1, period2) (dates, UTC midnight); no key involved."""
    params = urlencode({"period1": _epoch(period1), "period2": _epoch(period2), "interval": "1d",
                        "events": "div,splits", "includeAdjustedClose": "true"}, safe=",")
    return CHART.format(symbol=symbol) + "?" + params


def cached_raw(symbol: str, raw_dir: Path = RAW_DIR) -> tuple[Path | None, str]:
    """(newest cached body, 'ok') or (marker, 'not_found') or (None, '') for a symbol."""
    bodies = sorted(raw_dir.glob(f"{symbol}__*.json.gz"))
    if bodies:
        return bodies[-1], "ok"
    markers = sorted(raw_dir.glob(f"{symbol}__*.json.gz.404"))
    if markers:
        return markers[-1], "not_found"
    return None, ""


def bar_granularity(payload: dict) -> str:
    """The bar size of a v8 body: ``meta.dataGranularity`` ('1d', '1h', ...; every cached body has
    it). Without that field, 'intraday' when two bars other than the last (which can be the live
    one, stamped later) fall on the same New York date, else '1d'."""
    result = ((payload.get("chart") or {}).get("result") or [None])[0] or {}
    granularity = (result.get("meta") or {}).get("dataGranularity")
    if granularity:
        return str(granularity)
    stamps = (result.get("timestamp") or [])[:-1]
    if stamps and _ny_dates(stamps).duplicated().any():
        return "intraday"
    return "1d"


def best_raw(symbol: str, raw_dir: Path = RAW_DIR) -> Path | None:
    """Of several cached bodies (a retry of a truncated answer), a body of daily bars before any
    other (CRNX's range=max body has more stamps, but they are hourly bars), then the one with the
    most rows; the newest on a tie."""
    bodies = sorted(raw_dir.glob(f"{symbol}__*.json.gz"))
    if len(bodies) <= 1:
        return bodies[0] if bodies else None
    scored = []
    for k, path in enumerate(bodies):
        payload = read_raw(path)
        result = ((payload.get("chart") or {}).get("result") or [None])[0] or {}
        scored.append((bar_granularity(payload) == "1d", len(result.get("timestamp") or []), k, path))
    return max(scored)[3]


def fetch_symbols(symbols: list[str], *, period1: str = PERIOD1, period2: str | None = None,
                  limit: int | None = None, raw_dir: Path = RAW_DIR, getter=common.cached_get,
                  retry: bool = False) -> dict:
    """Fetch every symbol without a cached body or 404 marker, one request per 2 seconds.

    Stops at the first 401/403/429 and leaves the rest for the next run. Network or server
    errors (after cached_get's retries) are recorded and retried on the next run. With
    ``retry``, the symbols are asked again from the query2 host even when cached (for answers
    that came back truncated), daily bars from ``period1`` (a need start, say) to the fetch day;
    the build keeps whichever daily body has more rows.
    Returns {symbol: {status, http_status, message}} for the symbols asked in this run.
    """
    period2 = period2 or (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    todo = list(symbols) if retry else [s for s in symbols if not cached_raw(s, raw_dir)[1]]
    if limit is not None:
        todo = todo[:limit]
    log(f"yahoo: {len(symbols)} symbols, {len(symbols) - len(todo)} cached or 404, {len(todo)} to fetch "
        f"(about {len(todo) * 2.3 / 60:.0f} min)")
    outcome, started = {}, time.monotonic()
    for k, symbol in enumerate(todo, 1):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = raw_dir / f"{symbol}__{stamp}.json.gz"
        url = chart_url(symbol, period1, period2)
        if retry:
            url = url.replace("//query1.", "//query2.", 1)
        try:
            data = getter(url, path, source=SOURCE, headers=HEADERS, limiter=LIMITER, symbol=symbol, timeout=30)
            outcome[symbol] = {"status": "ok", "http_status": 200, "message": f"{len(data)} bytes"}
        except FileNotFoundError:
            outcome[symbol] = {"status": "not_found", "http_status": 404, "message": "404"}
        except HTTPError as exc:
            outcome[symbol] = {"status": "stopped", "http_status": exc.code, "message": f"HTTP {exc.code}"}
            log(f"yahoo: HTTP {exc.code} on {symbol} after {k - 1} requests this run; stopping (re-run resumes)")
            break
        except Exception as exc:  # recorded; the next run tries again
            outcome[symbol] = {"status": "error", "http_status": "", "message": common.redact(str(exc))[:200]}
        if k % 25 == 0 or k == len(todo):
            elapsed = time.monotonic() - started
            counts = pd.Series([o["status"] for o in outcome.values()]).value_counts().to_dict()
            log(f"yahoo: {k}/{len(todo)} fetched in {elapsed / 60:.1f} min, "
                f"about {(len(todo) - k) * elapsed / k / 60:.1f} min left; {counts}")
    return outcome


MIN_PARTIAL_COVERAGE = 0.5  # as the prefilter: a poorer answer is asked again under an SEC current ticker


def cached_coverage(symbol: str, need_start: str, need_end: str, sessions: pd.DatetimeIndex,
                    raw_dir: Path = RAW_DIR) -> tuple[str, float]:
    """(state, share of the need's sessions with a daily row) of a symbol's cached answer: state ok,
    empty (no daily rows), 404 or not_asked (coverage NaN)."""
    path, state = cached_raw(symbol, raw_dir)
    if state == "not_found":
        return "404", 0.0
    if state != "ok":
        return "not_asked", np.nan
    _, daily, _ = parse_chart(read_raw(best_raw(symbol, raw_dir)))
    need = sessions[(sessions >= pd.Timestamp(need_start)) & (sessions <= pd.Timestamp(min(need_end, WINDOW_END)))]
    if not len(daily):
        return "empty", 0.0
    if not len(need):
        return "ok", 1.0
    have = set(daily["date"])
    return "ok", round(sum(d in have for d in need) / len(need), 4)


def poor_answers(requests: pd.DataFrame, sessions: pd.DatetimeIndex, raw_dir: Path = RAW_DIR) -> dict:
    """index -> (state, coverage) of the requests with ``alt_symbols`` whose candidate symbol's cached answer
    is a 404, has no daily rows, or covers less than MIN_PARTIAL_COVERAGE of the need."""
    out = {}
    for k, row in requests.iterrows():
        if not str(row.get("alt_symbols", "") or ""):
            continue
        state, coverage = cached_coverage(row["symbol"], row["needed_start"], row["needed_end"], sessions, raw_dir)
        if state != "not_asked" and coverage < MIN_PARTIAL_COVERAGE:
            out[k] = (state, coverage)
    return out


def resolve_alternates(requests: pd.DataFrame, sessions: pd.DatetimeIndex,
                       raw_dir: Path = RAW_DIR) -> tuple[pd.DataFrame, dict]:
    """``requests`` with the symbol of each poor answer (``poor_answers``) replaced by the SEC current ticker
    whose cached answer covers most of the need, when it covers more. Returns (requests, {replaced symbol:
    why}) for the status table."""
    requests = requests.copy()
    replaced = {}
    for k, (state, coverage) in poor_answers(requests, sessions, raw_dir).items():
        row = requests.loc[k]
        tried = [(a, *cached_coverage(a, row["needed_start"], row["needed_end"], sessions, raw_dir))
                 for a in str(row["alt_symbols"]).split()]
        usable = [t for t in tried if t[1] == "ok"]
        if not usable:
            continue
        symbol, _, best = max(usable, key=lambda t: t[2])
        if not best > coverage:
            continue
        old = row["symbol"]
        what = "404" if state == "404" else f"{state}, {coverage:.1%} of the need"
        note = f"symbol {old} answered {what}; the SEC current ticker {symbol} covers {best:.1%} and is used"
        requests.loc[k, "symbol"] = symbol
        requests.loc[k, "note"] = " | ".join(filter(None, [str(row["note"] or ""), note]))
        replaced[old] = f"replaced by {symbol} (SEC current ticker) for {row['security_id']}: {what}"
    return requests, replaced


# ------------------------------------------------------------------ parsing

def _ny_dates(stamps) -> pd.DatetimeIndex:
    """Exchange-local session dates of Yahoo's epoch stamps (they mark the 09:30 open)."""
    return pd.to_datetime(pd.Series(stamps, dtype="float64"), unit="s", utc=True).dt.tz_convert(NY) \
        .dt.tz_localize(None).dt.normalize().pipe(pd.DatetimeIndex)


def split_cum_after(dates: pd.DatetimeIndex, splits: pd.DataFrame) -> np.ndarray:
    """F_t: product of the split ratios whose ex-date is after each date."""
    factor = np.ones(len(dates))
    for event in splits.itertuples(index=False):
        factor[dates < event.ex_date] *= event.ratio
    return factor


PARSED_EVENT_COLUMNS = ["ex_date", "event_type", "numerator", "denominator", "ratio", "amount_yahoo", "div_cash_raw",
                        "split_cum_after", "on_session", "applied_by_yahoo", "volume_restored", "volume_evidence"]


def parse_chart(payload: dict, not_applied: set | None = None, volume_as_served: set | None = None,
                volume_evidence: dict | None = None) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """(meta, daily frame, events) from one v8 chart body; empty frames when there is no result
    or the bars are not daily (``meta['error']`` says why).

    ``not_applied``: ex-dates of listed splits Yahoo has not applied (left out of F and V).
    ``volume_as_served``: ex-dates of odd-ratio events whose volume Yahoo did not scale (left out
    of V only; see ``volume_scaling``), with ``volume_evidence`` {ex_date: text} for the table.

    Daily columns: DAILY_COLUMNS. Events: ex_date, event_type (split / reverse_split / dividend),
    numerator, denominator, ratio, amount_yahoo, div_cash_raw, split_cum_after, on_session,
    applied_by_yahoo, volume_restored (Y: volume divided by the ratio before the ex-date; N: left as
    served), volume_evidence (share_split, not_applied, reverse_split, none, or wiki:n:chosen:other).
    """
    chart = payload.get("chart") or {}
    result = (chart.get("result") or [None])[0]
    empty_events = pd.DataFrame(columns=PARSED_EVENT_COLUMNS)
    if not result:
        return {"error": json.dumps(chart.get("error"))[:200]}, pd.DataFrame(columns=DAILY_COLUMNS), empty_events
    meta = result.get("meta") or {}
    granularity = bar_granularity(payload)
    if granularity != "1d":
        return ({**meta, "error": f"not daily bars ({granularity})"}, pd.DataFrame(columns=DAILY_COLUMNS),
                empty_events)
    stamps = result.get("timestamp") or []
    events = result.get("events") or {}
    splits = pd.DataFrame([
        {"ex_date": _ny_dates([e["date"]])[0], "numerator": float(e["numerator"]),
         "denominator": float(e["denominator"])}
        for e in (events.get("splits") or {}).values()
        if float(e.get("numerator") or 0) > 0 and float(e.get("denominator") or 0) > 0],
        columns=["ex_date", "numerator", "denominator"])
    splits["ratio"] = splits["numerator"] / splits["denominator"]
    splits = splits.sort_values("ex_date").reset_index(drop=True)
    dividends = pd.DataFrame([{"ex_date": _ny_dates([e["date"]])[0], "amount_yahoo": float(e["amount"])}
                              for e in (events.get("dividends") or {}).values() if e.get("amount") is not None],
                             columns=["ex_date", "amount_yahoo"]).sort_values("ex_date").reset_index(drop=True)
    if not stamps:
        return meta, pd.DataFrame(columns=DAILY_COLUMNS), empty_events
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    adjusted = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or [None] * len(stamps)
    daily = pd.DataFrame({
        "date": _ny_dates(stamps),
        "close_yahoo": pd.to_numeric(pd.Series(quote.get("close") or [None] * len(stamps)), errors="coerce"),
        "volume_yahoo": pd.to_numeric(pd.Series(quote.get("volume") or [None] * len(stamps)), errors="coerce"),
        "adjclose_yahoo": pd.to_numeric(pd.Series(adjusted), errors="coerce"),
    })
    # The live last row can repeat a date; a halted day comes back with nulls.
    daily = daily.drop_duplicates("date", keep="last")
    daily = daily[daily["close_yahoo"].notna() & (daily["close_yahoo"] > 0)].sort_values("date").reset_index(drop=True)
    dates = pd.DatetimeIndex(daily["date"])
    splits["applied"] = [event.ex_date not in (not_applied or set())
                         and split_applied(event.ex_date, event.numerator, event.denominator, dates,
                                           daily["close_yahoo"].values) for event in splits.itertuples(index=False)]
    applied = splits[splits["applied"]]
    factor = split_cum_after(dates, applied)
    daily["split_cum_after"] = factor
    daily["close_raw"] = daily["close_yahoo"] * factor
    daily["split_factor"] = 1.0
    daily["div_cash"] = 0.0
    rows = []
    for event in splits.itertuples(index=False):
        position = dates.searchsorted(event.ex_date)  # first session on or after the ex-date
        if position < len(daily):
            daily.loc[position, "split_factor"] *= event.ratio
        rows.append({"ex_date": event.ex_date, "event_type": "split" if event.ratio >= 1 else "reverse_split",
                     "numerator": event.numerator, "denominator": event.denominator, "ratio": event.ratio,
                     "amount_yahoo": np.nan, "div_cash_raw": np.nan,
                     "split_cum_after": float(split_cum_after(pd.DatetimeIndex([event.ex_date]), applied)[0]),
                     "on_session": "Y" if position < len(daily) and dates[position] == event.ex_date else "N",
                     "applied_by_yahoo": "Y" if event.applied else "N"})
    for event in dividends.itertuples(index=False):
        # Yahoo scales dividend amounts by every split it lists, even one whose prices it has not
        # adjusted yet (NFE: $0.10 served as $5.00 after the 1:50 of 2026-09-14).
        cum = float(split_cum_after(pd.DatetimeIndex([event.ex_date]), splits)[0])
        position = dates.searchsorted(event.ex_date)
        if position < len(daily):
            daily.loc[position, "div_cash"] += event.amount_yahoo * cum
        rows.append({"ex_date": event.ex_date, "event_type": "dividend", "numerator": np.nan, "denominator": np.nan,
                     "ratio": np.nan, "amount_yahoo": event.amount_yahoo, "div_cash_raw": event.amount_yahoo * cum,
                     "split_cum_after": cum,
                     "on_session": "Y" if position < len(daily) and dates[position] == event.ex_date else "N",
                     "applied_by_yahoo": ""})
    table = pd.DataFrame(rows, columns=empty_events.columns) if rows else empty_events
    daily, table = restore_volume(daily, table.sort_values(["ex_date", "event_type"]).reset_index(drop=True),
                                  volume_as_served, volume_evidence)
    return meta, daily, table


def restore_volume(daily: pd.DataFrame, events: pd.DataFrame, as_served: set | None = None,
                   evidence: dict | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """V (``volume_cum_after``) and ``volume_raw = volume_yahoo / V`` for a parsed series, and the
    events' volume_restored / volume_evidence columns. V holds every split applied to close except
    the ex-dates in ``as_served``: odd-ratio events whose volume Yahoo did not scale (found by
    ``volume_scaling``; ``evidence`` {ex_date: text} from it). Returns new frames."""
    as_served = set(as_served or ())
    daily, events = daily.copy(), events.copy()
    in_v = events[(events["event_type"] != "dividend") & (events["applied_by_yahoo"] == "Y")
                  & ~events["ex_date"].isin(as_served)]
    factor = split_cum_after(pd.DatetimeIndex(daily["date"]), in_v)
    daily["volume_cum_after"] = factor
    daily["volume_raw"] = (daily["volume_yahoo"] / factor).round()
    restored, text = [], []
    for event in events.itertuples(index=False):
        if event.event_type == "dividend":
            restored.append(""), text.append("")
        elif event.applied_by_yahoo != "Y":
            restored.append("N"), text.append("not_applied")
        else:
            restored.append("N" if event.ex_date in as_served else "Y")
            text.append("share_split" if ordinary_ratio(event.ratio) else (evidence or {}).get(event.ex_date, "none"))
    events["volume_restored"], events["volume_evidence"] = restored, text
    return daily[DAILY_COLUMNS], events


def split_applied(ex_date, numerator: float, denominator: float, dates: pd.DatetimeIndex,
                  closes: np.ndarray) -> bool:
    """Whether Yahoo's ``close`` is adjusted for a listed split. A recent split is sometimes listed
    before the history is adjusted: then the served close moves by about 1/ratio on the ex-date
    (the raw move) instead of about 1. Only share splits (``ordinary_ratio``) of 25% or more are
    judged. Smaller ones (stock dividends) and distributions
    served as odd ratios (1423988:1000000) are assumed applied: a spin-off can move the adjusted
    close by about the ratio too."""
    ratio = numerator / denominator
    if not ordinary_ratio(ratio) or abs(np.log(ratio)) < np.log(1.25):
        return True
    position = dates.searchsorted(ex_date)
    if position == 0 or position >= len(dates) or dates[position] != ex_date:
        return True
    move = np.log(closes[position] / closes[position - 1])
    raw_move = -np.log(ratio)
    return not (abs(move - raw_move) < 0.5 * abs(raw_move) and abs(move - raw_move) < abs(move))


def unsessioned_splits_not_applied(daily: pd.DataFrame, events: pd.DataFrame, stored: pd.DataFrame) -> set:
    """Ex-dates of whole-number splits with no Yahoo row on the ex-date (a split dated after the
    last trade, NFE 1:50 on 2026-09-14) that Yahoo has not applied: there the served close shows no
    jump to judge by, so it is compared with the stored file (in the units before the split) over
    the last 60 common days before the ex-date. A served close within 10% of the stored close is
    in pre-split units: not applied."""
    out = set()
    if daily.empty or events.empty or stored.empty:
        return out
    candidates = events[(events["event_type"] != "dividend") & (events["on_session"] == "N")]
    for event in candidates.itertuples(index=False):
        if abs(np.log(event.ratio)) < np.log(1.25):
            continue
        before = daily[daily["date"] < event.ex_date][["date", "close_yahoo"]]
        joined = before.merge(stored[["date", "close"]], on="date").tail(60)
        joined = joined[joined["close"] > 0]
        if len(joined) >= 20 and abs(float((joined["close_yahoo"] / joined["close"]).median()) - 1) <= 0.10:
            out.add(event.ex_date)
    return out


VOLUME_TEST_SESSIONS = 60  # Yahoo sessions before an odd-ratio ex-date compared with WIKI volume
VOLUME_TEST_MIN_DAYS = 20
VOLUME_TEST_CLEAR = 0.02   # the chosen median ratio within 2% of 1, else volume_check_unclear
REVERSE_SPLIT_BELOW = 0.8  # an odd ratio below this is a reverse split (1:150, 2:25), not a distribution


def volume_scaling(daily: pd.DataFrame, events: pd.DataFrame, wiki: pd.DataFrame) -> tuple[set, dict]:
    """Whether Yahoo scaled its served volume for each applied odd-ratio event (a distribution or a
    stock dividend served as a split). Yahoo scales ``close`` for all of them, but for the 2013-2014
    ones it left ``volume`` raw (PENN 2013-11-04 4.423: the restore divided raw volume by 4.423).

    Per event, latest first (so later choices are already in V): over the WIKI days among the
    60 Yahoo sessions before the ex-date (20+ needed), the median of restored volume / WIKI raw
    volume with the event in V (q) and without it (q x ratio). The one nearer 1 (in log) wins.
    Without WIKI there the event stays in V: evidence 'none' (flagged volume_restore_unverified), or
    'reverse_split' for a ratio below 0.8, which only a reverse split gives.
    Returns (ex-dates to leave out of V, {ex_date: 'wiki:n:chosen:other', 'none' or 'reverse_split'})."""
    as_served, evidence = set(), {}
    if daily.empty or events.empty:
        return as_served, evidence
    splits = events[(events["event_type"] != "dividend") & (events["applied_by_yahoo"] == "Y")]
    odd = splits[np.array([not ordinary_ratio(r) for r in splits["ratio"]], dtype=bool)]
    if odd.empty:
        return as_served, evidence
    wiki = wiki[pd.to_numeric(wiki["volume"], errors="coerce") > 0] if len(wiki) else wiki
    dates = pd.DatetimeIndex(daily["date"])
    for event in odd.sort_values("ex_date", ascending=False).itertuples(index=False):
        position = dates.searchsorted(event.ex_date)
        before = daily.iloc[max(0, position - VOLUME_TEST_SESSIONS):position][["date", "volume_yahoo"]]
        joined = before.merge(wiki[["date", "volume"]], on="date") if len(wiki) else before.iloc[0:0]
        joined = joined[joined["volume_yahoo"] > 0]
        if len(joined) < VOLUME_TEST_MIN_DAYS:
            # A ratio below 0.8 needs a reverse split (a distribution or stock dividend gives a ratio
            # above 1): its volume is scaled like any share split's.
            evidence[event.ex_date] = "reverse_split" if event.ratio < REVERSE_SPLIT_BELOW else "none"
            continue
        kept = splits[~splits["ex_date"].isin(as_served)]
        restored = joined["volume_yahoo"].values / split_cum_after(pd.DatetimeIndex(joined["date"]), kept)
        with_event = float(np.median(restored / joined["volume"].values.astype(float)))
        without = with_event * event.ratio
        if abs(np.log(without)) < abs(np.log(with_event)):
            as_served.add(event.ex_date)
            chosen, other = without, with_event
        else:
            chosen, other = with_event, without
        evidence[event.ex_date] = f"wiki:{len(joined)}:{chosen:.4f}:{other:.4f}"
    return as_served, evidence


DAILY_COLUMNS = ["date", "close_raw", "volume_raw", "split_factor", "div_cash", "close_yahoo", "volume_yahoo",
                 "adjclose_yahoo", "split_cum_after", "volume_cum_after"]


def read_raw(path: Path) -> dict:
    return json.loads(gzip.decompress(path.read_bytes()))


# ------------------------------------------------------------------ entity checks

NAME_STOP = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COS", "LTD", "LIMITED", "PLC", "LLC", "LP", "L",
    "P", "SA", "S", "A", "NV", "N", "V", "AG", "SE", "AB", "ASA", "THE", "OF", "AND", "HOLDINGS", "HOLDING",
    "HLDGS", "GROUP", "GRP", "CLASS", "SERIES", "COMMON", "STOCK", "SHARES", "ORDINARY", "NEW", "DE", "DEL",
    "B", "C", "K", "ADR", "ADS", "DEPOSITARY", "REPRESENTING", "EACH", "ONE", "PAR", "VALUE",
}
NAME_ALIASES = {"INTL": "INTERNATIONAL", "TECH": "TECHNOLOGY", "TECHNOLOGIES": "TECHNOLOGY", "SYS": "SYSTEMS",
                "COMMS": "COMMUNICATIONS", "PHARMA": "PHARMACEUTICALS", "BANCORPORATION": "BANCORP",
                "FINL": "FINANCIAL", "MGMT": "MANAGEMENT", "SVCS": "SERVICES", "ENTMT": "ENTERTAINMENT"}
GENERIC_FIRST = {"AMERICAN", "FIRST", "UNITED", "NATIONAL", "GLOBAL", "INTERNATIONAL", "US", "NORTH", "SOUTHERN",
                 "WESTERN", "EASTERN", "PACIFIC", "GENERAL", "NEW", "CENTRAL", "CAPITAL", "BANK"}


def name_tokens(name: str) -> list[str]:
    """Upper-case word tokens of a company name without legal-form words, share-class text
    (after ' - ' in the Nasdaq files), state tags (/DE/, \\PA\\) or punctuation."""
    text = str(name or "").upper().split(" - ")[0]
    text = re.sub(r"[/\\][A-Z]{2,3}[/\\]?", " ", text)
    text = text.replace("&", " AND ").replace(".COM", " COM").replace("'", "")
    tokens = [NAME_ALIASES.get(t, t) for t in re.split(r"[^A-Z0-9]+", text) if t]
    return [t for t in tokens if t not in NAME_STOP]


def _same_token(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)))


def names_match(left: str, right: str) -> bool:
    """Two company names name the same company: the same non-generic first token, or at least
    half of the shorter name's tokens found in the other (prefixes of 4+ letters count)."""
    a, b = name_tokens(left), name_tokens(right)
    if not a or not b:
        return False
    if _same_token(a[0], b[0]) and (a[0] not in GENERIC_FIRST or (len(a) > 1 and len(b) > 1 and _same_token(a[1], b[1]))):
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    found = sum(any(_same_token(t, u) for u in long_) for t in short)
    return found >= max(1, (len(short) + 1) // 2) and found >= min(2, len(short))


def former_name_list(text: str) -> list[str]:
    """Names from the master's ``former_names`` ('NAME (from..to) | NAME (from..to)')."""
    return [re.sub(r"\s*\([^()]*\)\s*$", "", part).strip() for part in str(text or "").split("|") if part.strip()]


def yahoo_first_trade(meta: dict) -> str:
    stamp = meta.get("firstTradeDate")
    if stamp in (None, ""):
        return ""
    return _ny_dates([stamp])[0].strftime("%Y-%m-%d")


LASTSALE_BAND = {"confirmed": 0.02}  # otherwise 5%: the as-of session of the list is unverified
LEVEL_OFFSET_REPORT = 0.01  # a steady LastSale run further than this from 1.0 is reported, even inside the band
WIKI_BAND = 0.01
DV_RATIO_BAND = (0.67, 1.5)


def _runs_text(runs: list[dict]) -> str:
    return " ".join(f"{r['start']:%Y-%m-%d}..{r['end']:%Y-%m-%d}@{r['level']:.4f}" for r in runs)


def lastsale_check(daily: pd.DataFrame, quotes: pd.DataFrame) -> dict:
    """Raw close on a company list's as-of session against its LastSale (step 6's rule: 2%, or 5%
    where the as-of session is unverified; 2+ comparisons with fewer than half agreeing fail), and
    the runs of a steady level ratio (see ``level_segments``; 2% tolerance for list quotes).
    ``lastsale_off_runs``: runs of 3+ more than 2% off (what the entity rule weighs);
    ``lastsale_offset_runs``: steady runs of 3+ (80% within 2% of their level) more than 1% off,
    which may sit inside the agreement band (FWONK and LMCK at 0.9833) and are reported."""
    empty = {"lastsale_n": 0, "lastsale_agree": 0, "lastsale_fail": False, "lastsale_median_ratio": np.nan,
             "lastsale_runs": 0, "lastsale_piecewise_stable": False, "lastsale_off_share": 0.0, "lastsale_off_runs": "",
             "lastsale_offset_runs": ""}
    if daily.empty or quotes.empty:
        return empty
    quotes = quotes.assign(date=pd.to_datetime(quotes["as_of_session"]))
    joined = daily[["date", "close_raw"]].merge(quotes[["date", "last_sale", "as_of_check"]], on="date")
    if joined.empty:
        return empty
    band = joined["as_of_check"].map(LASTSALE_BAND).fillna(0.05)
    ratio = joined["close_raw"] / joined["last_sale"]
    agree = int(((ratio - 1).abs() <= band).sum())
    runs = level_segments(joined["date"], ratio, jump=0.01, min_run=3, tolerance=0.02)
    stable = len(runs) <= 4 and sum(r["stable"] * r["n"] for r in runs) / len(joined) >= 0.8
    off = [r for r in runs if r["n"] >= 3 and abs(r["level"] - 1) > 0.02]
    steady = [r for r in runs if r["n"] >= 3 and r["stable"] >= 0.8 and abs(r["level"] - 1) > LEVEL_OFFSET_REPORT]
    return {"lastsale_n": int(len(joined)), "lastsale_agree": agree,
            "lastsale_fail": bool(len(joined) >= 2 and agree < 0.5 * len(joined)),
            "lastsale_median_ratio": round(float(ratio.median()), 4), "lastsale_runs": len(runs),
            "lastsale_piecewise_stable": bool(stable), "lastsale_off_share": round(sum(r["n"] for r in off) / len(joined), 4),
            "lastsale_off_runs": _runs_text(off), "lastsale_offset_runs": _runs_text(steady)}


def level_segments(dates: pd.Series, ratio: pd.Series, jump: float = 0.004, min_run: int = 5,
                   tolerance: float = 0.01) -> list[dict]:
    """Runs of a steady price-level ratio (Yahoo raw close / another source's raw close), date
    ordered. A run ends where the 5-day centred median of log(ratio) moves by more than ``jump``;
    runs shorter than ``min_run`` days are merged into their neighbour (single bad prints)."""
    order = np.argsort(dates.values)
    days, logs = pd.DatetimeIndex(dates.values[order]), np.log(ratio.values[order])
    smooth = pd.Series(logs).rolling(5, center=True, min_periods=1).median().values
    cuts = [0] + [i for i in range(1, len(smooth)) if abs(smooth[i] - smooth[i - 1]) > jump] + [len(smooth)]
    runs = [(a, b) for a, b in zip(cuts[:-1], cuts[1:]) if b > a]
    merged: list[list[int]] = []
    for a, b in runs:
        if merged and (b - a < min_run or merged[-1][1] - merged[-1][0] < min_run):
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [{"start": days[a], "end": days[b - 1], "n": b - a, "level": float(np.exp(np.median(logs[a:b]))),
             "stable": float((np.abs(logs[a:b] - np.median(logs[a:b])) <= tolerance).mean())} for a, b in merged]


def wiki_check(daily: pd.DataFrame, wiki: pd.DataFrame) -> dict:
    """Raw close against the security's WIKI raw close on common days (WIKI is raw as traded):
    share within 1% (20+ common days with fewer than half agreeing fail), and the runs of a steady
    level ratio. A few steady runs, one off 1.0, are an adjustment Yahoo made without listing it
    (a spin-off folded into ``close``, or a missing stock dividend), not another company."""
    empty = {"wiki_n": 0, "wiki_share_1pct": np.nan, "wiki_fail": False, "wiki_median_ratio": np.nan,
             "wiki_runs": 0, "wiki_piecewise_stable": False, "wiki_off_share": 0.0, "wiki_off_runs": ""}
    if daily.empty or wiki.empty:
        return empty
    joined = daily[["date", "close_raw"]].merge(wiki[["date", "close"]], on="date")
    joined = joined[joined["close"] > 0]
    if joined.empty:
        return empty
    ratio = joined["close_raw"] / joined["close"]
    share = float(((ratio - 1).abs() <= WIKI_BAND).mean())
    runs = level_segments(joined["date"], ratio)
    stable = len(runs) <= 4 and sum(r["stable"] * r["n"] for r in runs) / len(joined) >= 0.9
    off = [r for r in runs if r["n"] >= 20 and abs(r["level"] - 1) > WIKI_BAND]
    return {"wiki_n": int(len(joined)), "wiki_share_1pct": round(share, 4),
            "wiki_fail": bool(len(joined) >= 20 and share < 0.5), "wiki_median_ratio": round(float(ratio.median()), 4),
            "wiki_runs": len(runs), "wiki_piecewise_stable": bool(stable),
            "wiki_off_share": round(sum(r["n"] for r in off) / len(joined), 4),
            "wiki_off_runs": _runs_text(off)}


def stored_dv_check(daily: pd.DataFrame, stored: pd.DataFrame) -> dict:
    """Raw close x raw volume against the stored file's close x volume on common days: the median
    ratio and the share of days inside [0.67, 1.5].

    Not an independent check of the volume restore: the stored files are older Yahoo downloads
    (split-adjusted close, volume as Yahoo served it), so their dollar volume is raw only where
    Yahoo scaled close and volume alike. Before PENN's 2013 distribution both sides were 4.423 too
    low and agreed at 1.0000. It tells another company (another dollar volume) from the same one."""
    if daily.empty or stored.empty:
        return {"stored_n": 0, "stored_dv_median": np.nan, "stored_dv_share": np.nan, "stored_dv_off": False}
    joined = daily[["date", "close_raw", "volume_raw"]].merge(stored[["date", "close", "volume"]], on="date")
    joined = joined[(joined["volume_raw"] > 0) & (joined["volume"] > 0) & (joined["close"] > 0)]
    if joined.empty:
        return {"stored_n": 0, "stored_dv_median": np.nan, "stored_dv_share": np.nan, "stored_dv_off": False}
    ratio = (joined["close_raw"] * joined["volume_raw"]) / (joined["close"] * joined["volume"])
    median = float(ratio.median())
    share = float(ratio.between(*DV_RATIO_BAND).mean())
    return {"stored_n": int(len(joined)), "stored_dv_median": round(median, 4), "stored_dv_share": round(share, 4),
            "stored_dv_off": bool(len(joined) >= 60 and not (0.8 <= median <= 1.25))}


def split_restore_flags(daily: pd.DataFrame, events: pd.DataFrame, verified_until=None) -> list[str]:
    """Split events where Yahoo's split-adjusted close still moves by more than 25% and more than
    half the split's size on the ex-date: either Yahoo did not adjust the history before the split
    (the raw restore would then apply it twice) or the event is a distribution recorded as a split.
    (The raw close itself is meant to jump by 1/ratio.) Events on or before ``verified_until``
    (the last day WIKI raw closes agree with the restore) are not flagged."""
    flags = []
    if daily.empty or events.empty:
        return flags
    closes = daily.set_index("date")["close_yahoo"]
    for event in events[events["event_type"].isin(["split", "reverse_split"])].itertuples(index=False):
        if event.ratio == 1 or event.ex_date not in closes.index or event.applied_by_yahoo == "N":
            continue
        if verified_until is not None and event.ex_date <= verified_until:
            continue
        position = closes.index.get_loc(event.ex_date)
        if position == 0:
            continue
        move = abs(np.log(closes.iloc[position] / closes.iloc[position - 1]))
        if move > 0.5 * abs(np.log(event.ratio)) and move > np.log(1.25):
            flags.append(f"split_day_move:{event.ex_date:%Y-%m-%d}:{event.numerator:g}:{event.denominator:g}")
    return flags


class References:
    """The local files the entity check reads: security master, ticker-interval names, the
    company-list quotes and the step-6 file map (WIKI and stored files per security)."""

    def __init__(self, master: pd.DataFrame, intervals: pd.DataFrame, lists: pd.DataFrame, files: pd.DataFrame,
                 wiki_dir: Path = WIKI_DIR, stored_dir: Path = STORED_DIR, spans: pd.DataFrame | None = None):
        self.master = master.fillna("").set_index("security_id")
        self.spans = (spans if spans is not None else pd.DataFrame(
            columns=["security_id", "ticker", "list_start", "list_end", "obs_end"])).fillna("")
        self.snapshot_names = intervals.fillna("").groupby("security_id")["name_in_source"].apply(
            lambda s: sorted(set(n for n in s if n)))
        lists = lists.copy()
        lists["last_sale"] = pd.to_numeric(lists["last_sale"], errors="coerce")
        self.quotes = {sid: g for sid, g in lists[lists["last_sale"] > 0].groupby("security_id")}
        self.files = {sid: g for sid, g in files.fillna("").groupby("security_id")}
        self.wiki_dir, self.stored_dir = wiki_dir, stored_dir

    @classmethod
    def load(cls) -> "References":
        read = dict(dtype=str, keep_default_na=False)
        return cls(pd.read_csv(MASTER, **read), pd.read_csv(INTERVALS, **read), pd.read_csv(LISTS, **read),
                   pd.read_csv(FILES, **read), spans=pd.read_csv(SPANS, **read))

    def own_ticker_window(self, sid: str, ticker: str) -> tuple[str, str, str] | None:
        """(start, end, note) of the rows of ``ticker`` that belong to ``sid`` when another security
        held the ticker before it (step 6's listing spans): from the later of its own span start and
        the day after the earlier holder's last observation, to its own span end. None otherwise."""
        rows = self.spans[self.spans["ticker"] == ticker]
        own, others = rows[rows["security_id"] == sid], rows[rows["security_id"] != sid]
        if own.empty or others.empty:
            return None
        start, end = own["list_start"].min(), own["list_end"].max()
        before = others[others["list_start"] < start]
        if before.empty:
            return None
        holder = before.sort_values("list_start").iloc[-1]
        last_seen = holder["obs_end"] or holder["list_end"]
        cut = (pd.Timestamp(last_seen) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        return max(start, cut), end, f"ticker {ticker} held by {holder['security_id']} until {last_seen}"

    def _master_value(self, sid: str, column: str) -> str:
        if sid not in self.master.index or column not in self.master.columns:
            return ""
        return str(self.master.at[sid, column] or "")

    def claimants(self, sid: str, symbol: str, same_symbol=()) -> list[tuple[str, str, str, str]]:
        """(how, security_id, first day, last day) of the listing spans of the securities that can
        own rows of Yahoo ``symbol`` besides ``sid``: the other securities fetched with the same
        symbol, the master's predecessors (successor_security_id = sid) and successor, the other
        classes of sid's multi-class group (all their spans), and other holders of the ticker
        (their spans on it)."""
        linked = {y: "same_symbol" for y in same_symbol if y != sid}
        if "successor_security_id" in self.master.columns:
            for y in self.master.index[self.master["successor_security_id"] == sid]:
                linked.setdefault(y, "predecessor")
        successor = self._master_value(sid, "successor_security_id")
        if successor and successor != sid:
            linked.setdefault(successor, "successor")
        group = self._master_value(sid, "multi_class_group")
        if group:
            for y in self.master.index[self.master["multi_class_group"] == group]:
                if y != sid:
                    linked.setdefault(y, "sibling")
        spans = self.spans
        out = [(how, y, s.list_start, s.list_end) for y, how in linked.items()
               for s in spans[spans["security_id"] == y].itertuples(index=False)]
        out += [("ticker", s.security_id, s.list_start, s.list_end)
                for s in spans[(spans["ticker"] == symbol) & (spans["security_id"] != sid)].itertuples(index=False)]
        return out

    def claimed_cut(self, sid: str, symbol: str, need_start: str, need_end: str, same_symbol=()) -> dict:
        """Where to cut the rows of Yahoo ``symbol`` for ``sid`` because another security claims
        them (plan 4.4 R9): on a side where a claimant's span reaches beyond sid's own listing
        span, rows outside [min(span start, need start), max(span end, need end)] are dropped.
        A side with no claimant is kept whole. Returns {'first': day or '', 'last': day or '',
        'before': claimant ids, 'after': claimant ids}; no span for sid means no cut."""
        out = {"first": "", "last": "", "before": "", "after": ""}
        own = self.spans[self.spans["security_id"] == sid]
        if own.empty:
            return out
        own_start, own_end = own["list_start"].min(), own["list_end"].max()
        claims = self.claimants(sid, symbol, same_symbol)
        before = sorted({f"{how}:{y}" for how, y, first, _ in claims if first < own_start})
        after = sorted({f"{how}:{y}" for how, y, _, last in claims if last > own_end})
        if before:
            out["first"], out["before"] = min(own_start, need_start), " ".join(before)
        if after:
            out["last"], out["after"] = max(own_end, need_end), " ".join(after)
        return out

    def names(self, sid: str) -> list[str]:
        if sid not in self.master.index:
            return []
        row = self.master.loc[sid]
        return [row["name"]] + former_name_list(row["former_names"]) + list(self.snapshot_names.get(sid, []))

    def _series(self, sid: str, src: str) -> pd.DataFrame:
        frames = []
        for row in self.files.get(sid, pd.DataFrame(columns=["src"])).itertuples(index=False):
            if row.src != src:
                continue
            if str(getattr(row, "entity_check_failed", "")) == "True":
                continue  # step 6 dropped this file as another company's (22701's SUNE.csv.gz is SunEdison)
            path = (self.wiki_dir if src == "wiki" else self.stored_dir) / row.file
            if not path.exists():
                continue
            data = pd.read_csv(path, usecols=["date", "close", "volume"], dtype={"date": str})
            data = data[(data["date"] >= row.first) & (data["date"] <= row.last)]
            frames.append(data.assign(date=pd.to_datetime(data["date"])))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["date", "close", "volume"])

    def wiki(self, sid: str) -> pd.DataFrame:
        return self._series(sid, "wiki")

    def stored(self, sid: str) -> pd.DataFrame:
        return self._series(sid, "stored")

    def lastsale(self, sid: str) -> pd.DataFrame:
        return self.quotes.get(sid, pd.DataFrame(columns=["as_of_session", "last_sale", "as_of_check"]))


SNAPSHOT_FLOOR = "2011-01-01"  # first_listed on or before this is the first snapshot, not a listing date
FIRST_TRADE_SLACK_DAYS = 30
COVER_SLACK_DAYS = 7


def _days(a: str, b: str) -> int:
    return (pd.Timestamp(a) - pd.Timestamp(b)).days


def check_entity(request: dict, meta: dict, daily: pd.DataFrame, refs: References,
                 sessions: pd.DatetimeIndex, fetch_day: str) -> dict:
    """Checks and a verdict for one (security, symbol): ok / partial / review / wrong_entity.

    wrong_entity: not a USD equity; the raw close fails the LastSale or WIKI level check (unless
    the name matches and the miss is one steady ratio, which is a split restore problem); or the
    name matches none of the security's names and Yahoo's first trade comes after the security
    was already listed (a newer company on the ticker) or the dollar volume is off.
    no_rows: no row at all, or none inside the need.
    partial: the same company, but the rows do not reach the start or end of the need (a need
    that starts at most 30 days before Yahoo's first trade of an IPO counts as covered), or need
    sessions are missing between the first and last row.
    review: a steady level offset, a name mismatch or a dollar-volume mismatch.
    """
    sid = request["security_id"]
    row = refs.master.loc[sid] if sid in refs.master.index else pd.Series(dtype=str)
    names = refs.names(sid)
    heir = str(row.get("successor_security_id", "")) if request.get("successor_routed") == "Y" else ""
    if heir:
        names += refs.names(heir)
    yahoo_names = [n for n in (meta.get("longName"), meta.get("shortName")) if n]
    name_ok = any(names_match(y, n) for y in yahoo_names for n in names)
    first_trade = yahoo_first_trade(meta)
    first_listed = str(row.get("first_listed", ""))
    need_start, need_end = request["needed_start"], min(request["needed_end"], WINDOW_END, fetch_day)
    out = {"yahoo_symbol": meta.get("symbol", ""), "yahoo_long_name": meta.get("longName", ""),
           "yahoo_short_name": meta.get("shortName", ""), "yahoo_first_trade": first_trade,
           "yahoo_exchange": meta.get("exchangeName", ""), "yahoo_instrument": meta.get("instrumentType", ""),
           "yahoo_currency": meta.get("currency", ""), "master_name": row.get("name", ""),
           "first_listed": first_listed, "delist_date": row.get("delist_date", ""), "name_match": name_ok}
    out["first_row"] = daily["date"].min().strftime("%Y-%m-%d") if len(daily) else ""
    out["last_row"] = daily["date"].max().strftime("%Y-%m-%d") if len(daily) else ""
    out["rows"] = int(len(daily))
    need = sessions[(sessions >= pd.Timestamp(need_start)) & (sessions <= pd.Timestamp(need_end))]
    have = set(daily["date"]) if len(daily) else set()
    need_rows = int(sum(d in have for d in need))
    out["need_sessions"] = int(len(need))
    out["need_coverage"] = round(need_rows / len(need), 4) if len(need) else np.nan
    late_vs_listing = bool(first_trade and first_listed > SNAPSHOT_FLOOR
                           and _days(first_trade, first_listed) > FIRST_TRADE_SLACK_DAYS) or bool(
        first_trade and first_listed and first_listed <= SNAPSHOT_FLOOR and first_trade > "2011-02-01")
    out["first_trade_after_listing"] = late_vs_listing
    out["first_trade_after_need"] = bool(first_trade and _days(first_trade, need_start) > COVER_SLACK_DAYS)
    out.update(lastsale_check(daily, refs.lastsale(sid)))
    out.update(wiki_check(daily, refs.wiki(sid)))
    out.update(stored_dv_check(daily, refs.stored(sid)))
    reasons, offsets = [], []
    # A level miss by one steady ratio on every compared day is the same company with a split that
    # Yahoo applied but did not list (or listed but did not apply): a restore problem, not an entity.
    ls_offset = out["lastsale_piecewise_stable"] and bool(out["lastsale_off_runs"])
    wiki_offset = out["wiki_piecewise_stable"] and bool(out["wiki_off_runs"])
    if out["yahoo_instrument"] != "EQUITY" or out["yahoo_currency"] != "USD":
        reasons.append(f"not_usd_equity:{out['yahoo_instrument']}/{out['yahoo_currency']}")
    # Levels that wander off in several unsteady runs (another share class, another company) fail
    # even when about half the comparisons happen to agree.
    ls_bad = out["lastsale_fail"] or (out["lastsale_n"] >= 5 and not out["lastsale_piecewise_stable"]
                                      and out["lastsale_off_share"] >= 0.2)
    wiki_bad = out["wiki_fail"] or (out["wiki_n"] >= 20 and not out["wiki_piecewise_stable"]
                                    and out["wiki_off_share"] >= 0.1)
    ls_strong = out["lastsale_n"] >= 10 and out["lastsale_agree"] >= 0.9 * out["lastsale_n"]
    # Steady runs 1-2% off sit inside the agreement band but are still an unlisted adjustment.
    ls_report = sorted(set((out["lastsale_off_runs"].split() if ls_offset else []) + out["lastsale_offset_runs"].split()))
    if ls_bad and not (ls_offset and name_ok):
        reasons.append(f"lastsale_level:{out['lastsale_agree']}/{out['lastsale_n']}")
    elif ls_report:
        offsets.append(f"level_offset_lastsale:{' '.join(ls_report)}")
    if wiki_bad and not (wiki_offset and name_ok):
        if ls_strong and out["wiki_n"] < 120:
            # A short WIKI file against a long run of agreeing list prices: WIKI is the doubtful one.
            offsets.append(f"wiki_short_disagrees:{out['wiki_n']}d@{out['wiki_median_ratio']:.4f}")
        else:
            reasons.append(f"wiki_level:{out['wiki_share_1pct']:.0%}")
    elif out["wiki_off_runs"]:
        offsets.append(f"level_offset_wiki:{out['wiki_off_runs']}")
    if not name_ok and (late_vs_listing or out["stored_dv_off"]):
        reasons.append("name_and_" + ("first_trade" if late_vs_listing else "dollar_volume"))
    level_agrees = out["lastsale_agree"] > 0 or (out["wiki_n"] >= 20 and not out["wiki_fail"])
    out["missing_inside"] = 0
    if reasons:
        verdict = "wrong_entity"
    elif len(daily) == 0:
        verdict, reasons = "no_rows", ["no rows in the window"]
    elif len(need) and need_rows == 0:
        # Rows, but none in the need (another security's history under the symbol): not coverage.
        verdict, reasons = "no_rows", [f"no rows in the need {need_start}..{need_end}: {out['rows']} rows "
                                       f"{out['first_row']}..{out['last_row']}"]
    else:
        first_gap = not len(need) or _days(out["first_row"], need[0].strftime("%Y-%m-%d")) > COVER_SLACK_DAYS
        last_gap = len(need) and _days(need[-1].strftime("%Y-%m-%d"), out["last_row"]) > COVER_SLACK_DAYS
        notes = []
        if (first_gap and len(need) and out["first_row"] == first_trade and first_listed > SNAPSHOT_FLOOR
                and _days(first_trade, first_listed) <= FIRST_TRADE_SLACK_DAYS):
            # An IPO: the need was opened from a snapshot before the first trade, and Yahoo has every
            # row from its first trade on.
            first_gap = False
            notes.append(f"need_starts_before_first_trade:{int((need < pd.Timestamp(first_trade)).sum())}")
        inside = need[(need >= pd.Timestamp(out["first_row"])) & (need <= pd.Timestamp(out["last_row"]))]
        out["missing_inside"] = int(sum(d not in have for d in inside))
        if first_gap or last_gap or out["missing_inside"]:
            verdict = "partial"
            if first_gap:
                missed = int((need < pd.Timestamp(out["first_row"])).sum())
                at_first_trade = " (Yahoo's first trade)" if out["first_row"] == first_trade else ""
                reasons.append(f"starts {out['first_row']}{at_first_trade}: {missed} need sessions before")
            if last_gap:
                missed = int((need > pd.Timestamp(out["last_row"])).sum())
                reasons.append(f"ends {out['last_row']}: {missed} need sessions after")
            if out["missing_inside"]:
                reasons.append(f"missing_inside:{out['missing_inside']} need sessions between the first and last row")
        else:
            verdict = "ok"
        reasons += notes
        review = list(offsets)
        if not name_ok:
            review.append("name_mismatch" + ("" if level_agrees else "_no_level_check"))
        if out["stored_dv_off"]:
            review.append(f"stored_dv_ratio:{out['stored_dv_median']:.2f}")
        if late_vs_listing and verdict == "ok":
            review.append(f"first_trade_{first_trade}_after_listing_{first_listed}")
        if review and verdict == "ok":
            verdict = "review"
        reasons += review
    out["verdict"], out["verdict_reasons"] = verdict, "; ".join(reasons)
    return out


# ------------------------------------------------------------------ build

def ordinary_ratio(value: float, tolerance: float = 0.001) -> bool:
    """A share split: n:1 or 1:n with n up to 100, or n:m with both up to 10 (3:2, 5:4). Anything
    else (1275:1000, 1603:1000, 21:20 stock dividends) is 'odd'."""
    if value <= 0:
        return False
    for whole in (value, 1 / value):
        if 1 <= round(whole) <= 100 and abs(round(whole) / whole - 1) <= tolerance:
            return True
    for denominator in range(1, 11):
        numerator = round(value * denominator)
        if 1 <= numerator <= 10 and abs(numerator / denominator / value - 1) <= tolerance:
            return True
    return False


def event_flags(events: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """Adds prior_close_raw, pct_of_prior and flags (odd_ratio, not_applied_by_yahoo,
    volume_not_scaled_by_yahoo, volume_restore_unverified, volume_check_unclear, split_after_2023,
    special_dividend_gt10pct, not_on_session) to an events table."""
    events = events.copy()
    closes = daily.set_index("date")["close_raw"] if len(daily) else pd.Series(dtype=float)
    prior, flags = [], []
    for event in events.itertuples(index=False):
        before = closes[closes.index < event.ex_date]
        value = float(before.iloc[-1]) if len(before) else np.nan
        prior.append(value)
        row = []
        if event.event_type != "dividend":
            if not ordinary_ratio(event.ratio):
                row.append("odd_ratio")
            if getattr(event, "applied_by_yahoo", "Y") == "N":
                row.append("not_applied_by_yahoo")
            evidence = str(getattr(event, "volume_evidence", "") or "")
            if evidence == "none":
                row.append("volume_restore_unverified")
            elif evidence.startswith("wiki:"):
                if getattr(event, "volume_restored", "Y") == "N":
                    row.append("volume_not_scaled_by_yahoo")
                if abs(np.log(float(evidence.split(":")[2]))) > VOLUME_TEST_CLEAR:
                    row.append("volume_check_unclear")
            if event.ex_date >= pd.Timestamp("2024-01-01"):
                row.append("split_after_2023")
        elif value > 0 and event.div_cash_raw > 0.10 * value:
            row.append("special_dividend_gt10pct")
        if event.on_session != "Y":
            row.append("not_on_session")
        flags.append(" ".join(row))
    events["prior_close_raw"] = prior
    events["pct_of_prior"] = np.where(events["event_type"] == "dividend",
                                      events["div_cash_raw"] / events["prior_close_raw"], np.nan)
    events["flags"] = flags
    return events


EVENT_COLUMNS = ["security_id", "symbol", "verdict", "ex_date", "event_type", "numerator", "denominator", "ratio",
                 "amount_yahoo", "div_cash_raw", "split_cum_after", "applied_by_yahoo", "volume_restored",
                 "volume_evidence", "prior_close_raw", "pct_of_prior", "on_session", "in_window", "flags"]
FLOAT_FORMAT = "%.10g"  # 10 significant digits: a factor of 5e-7 (KUST) no longer prints as 0.000000


def series_csv(frame: pd.DataFrame) -> bytes:
    """A security's series as CSV bytes: volumes as integers, other numbers to 10 significant digits
    (so close_raw = close_yahoo x split_cum_after can be checked from the file)."""
    out = frame.copy()
    for column in ("volume_raw", "volume_yahoo"):
        out[column] = pd.to_numeric(out[column], errors="coerce").round().astype("Int64")
    return out.to_csv(index=False, float_format=FLOAT_FORMAT).encode("utf-8")
STATUS_COLUMNS = ["symbol", "status", "http_status", "raw_file", "fetched_utc", "rows_returned", "message"]


def _fetched_utc(path: Path) -> str:
    stamp = path.name.split("__", 1)[1].split(".", 1)[0]
    return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat(timespec="seconds")


LEVEL_JUMP = 10.0  # a single-day close ratio beyond this (either way) on a day without a split is reported
SPLIT_NEAR_DAYS = 3  # a split listed within this many days of the move explains it


def level_jumps(window: pd.DataFrame, events: pd.DataFrame) -> list[tuple[pd.Timestamp, float]]:
    """(date, close_raw / previous close_raw) of the rows whose raw close moves by more than LEVEL_JUMP
    times from the previous row, on a day with no split (``split_factor`` 1 and no split event within
    SPLIT_NEAR_DAYS): another equity joined on, or a split Yahoo did not list."""
    if len(window) < 2:
        return []
    rows = window.sort_values("date")
    ratio = (rows["close_raw"] / rows["close_raw"].shift(1)).to_numpy()
    dates = pd.DatetimeIndex(rows["date"])
    splits = pd.DatetimeIndex(events.loc[events["event_type"] != "dividend", "ex_date"]) if len(events) else pd.DatetimeIndex([])
    out = []
    for k in np.flatnonzero(np.abs(np.log(np.where(ratio > 0, ratio, 1.0))) > np.log(LEVEL_JUMP)):
        day = dates[k]
        if rows["split_factor"].iloc[k] != 1 or (len(splits) and np.min(np.abs((splits - day).days)) <= SPLIT_NEAR_DAYS):
            continue
        out.append((day, float(ratio[k])))
    return out


def relist_junction(window: pd.DataFrame, day: str) -> tuple[pd.Timestamp | None, str]:
    """(first row on or after the relisting ``day``, review text) when the series also holds rows before
    it; (None, '') otherwise (a series that starts with the new listing needs no junction: WW, OPI)."""
    if not day or not len(window):
        return None, ""
    after = window[window["date"] >= pd.Timestamp(day)]
    before = window[window["date"] < pd.Timestamp(day)]
    if not len(after) or not len(before):
        return None, ""
    first, last = after.iloc[0], before.iloc[-1]
    ratio = float(first["close_raw"] / last["close_raw"]) if last["close_raw"] > 0 else np.nan
    return first["date"], (f"relist_junction:{first['date']:%Y-%m-%d} (listed again from {day}; raw close x{ratio:.4g} "
                           f"from {last['date']:%Y-%m-%d}: no return across it)")


def build(requests: pd.DataFrame, outcome: dict | None = None, refs: References | None = None,
          raw_dir: Path = RAW_DIR, out_dir: Path = OUT, sessions: pd.DatetimeIndex | None = None,
          metrics_path: Path = WEEKLY_METRICS) -> dict:
    """Parse every cached body, check each (security, symbol) and write the outputs."""
    from scripts.reversal_data_prefilter import xnas_sessions

    outcome = outcome or {}
    refs = refs or References.load()
    sessions = sessions if sessions is not None else xnas_sessions(WINDOW_START, WINDOW_END)
    out_dir.mkdir(parents=True, exist_ok=True)
    requests = requests.copy()
    for column in ("alt_symbols", "junction_date", "candidate_symbol"):
        if column not in requests:
            requests[column] = "" if column != "candidate_symbol" else requests["symbol"]
    requests, replaced = resolve_alternates(requests, sessions, raw_dir)
    if replaced:
        log(f"build: poor answers replaced by an SEC current ticker: {replaced}")
    parsed, status_rows = {}, []
    for symbol in sorted(set(requests["symbol"]) | set(replaced)):
        path, state = cached_raw(symbol, raw_dir)
        if state == "ok":
            path = best_raw(symbol, raw_dir)
        info = {"symbol": symbol, "status": state or outcome.get(symbol, {}).get("status", "pending"),
                "http_status": 200 if state == "ok" else 404 if state == "not_found" else
                outcome.get(symbol, {}).get("http_status", ""), "raw_file": path.name if path else "",
                "fetched_utc": _fetched_utc(path) if path else "", "rows_returned": 0,
                "message": "" if state else outcome.get(symbol, {}).get("message", "")}
        if symbol in replaced:
            info["message"] = "; ".join(filter(None, [info["message"], replaced[symbol]]))
            if symbol not in set(requests["symbol"]):
                status_rows.append(info)
                continue
        if state == "ok":
            meta, daily, events = parse_chart(read_raw(path))
            parsed[symbol] = (meta, daily, events, info["fetched_utc"][:10])
            info["rows_returned"] = int(len(daily))
            if not len(daily):
                info["status"], info["message"] = "empty", meta.get("error", "no rows")
        status_rows.append(info)
    status = pd.DataFrame(status_rows, columns=STATUS_COLUMNS)
    report_rows, event_frames, yahoo_weeks, accepted, rejected = [], [], {}, {}, {}
    same_symbol = requests.groupby("symbol")["security_id"].apply(set).to_dict()
    for k, request in enumerate(requests.to_dict("records"), 1):
        symbol, sid = request["symbol"], request["security_id"]
        base = {**request}
        if symbol not in parsed:
            row = status.loc[status["symbol"] == symbol].iloc[0]
            report_rows.append({**base, "verdict": "failed", "verdict_reasons": f"{row.status} {row.message}".strip()})
            continue
        meta, daily, events, fetch_day = parsed[symbol]
        late = unsessioned_splits_not_applied(daily, events, refs.stored(sid))
        if late:
            meta, daily, events = parse_chart(read_raw(best_raw(symbol, raw_dir)), not_applied=late)
        # Odd-ratio events whose volume Yahoo did not scale, judged against this security's WIKI volume.
        as_served, evidence = volume_scaling(daily, events, refs.wiki(sid))
        daily, events = restore_volume(daily, events, as_served, evidence)
        window = daily[(daily["date"] >= pd.Timestamp(WINDOW_START)) & (daily["date"] <= pd.Timestamp(WINDOW_END))]
        if request.get("segment_start"):
            window = window[window["date"] >= pd.Timestamp(request["segment_start"])]
        if request.get("segment_end"):
            window = window[window["date"] <= pd.Timestamp(request["segment_end"])]
        if request.get("successor_routed") == "Y":
            # The successor's ticker carries the predecessor's history: keep the predecessor's need only.
            window = window[window["date"] <= pd.Timestamp(request["needed_end"])]
        # Plan 4.4 R9: rows another security claims (a predecessor, successor, sibling class, another
        # security fetched with this symbol, another holder of the ticker) are cut on that side.
        cut = refs.claimed_cut(sid, symbol, request["needed_start"], request["needed_end"],
                               same_symbol.get(symbol, ()))
        cut_notes, cut_before = [], False
        if cut["first"]:
            dropped = int((window["date"] < pd.Timestamp(cut["first"])).sum())
            if dropped:
                cut_before = True
                window = window[window["date"] >= pd.Timestamp(cut["first"])]
                cut_notes.append(f"claimed_rows_cut:{dropped} before {cut['first']} ({cut['before']})")
        if cut["last"]:
            dropped = int((window["date"] > pd.Timestamp(cut["last"])).sum())
            if dropped:
                window = window[window["date"] <= pd.Timestamp(cut["last"])]
                cut_notes.append(f"claimed_rows_cut:{dropped} after {cut['last']} ({cut['after']})")
        checks = check_entity(request, meta, window, refs, sessions, fetch_day)
        segmented = bool(request.get("segment_start") or request.get("segment_end"))
        own = refs.own_ticker_window(sid, symbol) if checks["verdict"] == "wrong_entity" and not segmented else None
        if own:
            # Plan 4.4 R9: a reused ticker's series is kept only inside this security's own span.
            trimmed = window[(window["date"] >= pd.Timestamp(own[0])) & (window["date"] <= pd.Timestamp(own[1]))]
            again = check_entity(request, meta, trimmed, refs, sessions, fetch_day)
            if again["verdict"] != "wrong_entity" and len(trimmed):
                note = f"trimmed to {own[0]}..{own[1]} ({own[2]}); untrimmed: {checks['verdict_reasons']}"
                checks, window = again, trimmed
                checks["verdict_reasons"] = "; ".join(filter(None, [checks["verdict_reasons"], note]))
        verified = None
        if checks.get("wiki_n", 0) >= 20 and not checks.get("wiki_fail") and not checks.get("wiki_off_runs"):
            verified = min(pd.Timestamp(WIKI_END), window["date"].max())
        checks["split_flags"] = " ".join(split_restore_flags(daily, events, verified))
        notes, review = list(cut_notes), []
        if checks["split_flags"]:
            review.append(checks["split_flags"])
        skipped = events[events["applied_by_yahoo"] == "N"]
        if len(skipped):
            notes.append("split_not_applied_by_yahoo:" + ",".join(
                f"{e.ex_date:%Y-%m-%d}:{e.numerator:g}:{e.denominator:g}" for e in skipped.itertuples(index=False)))
        # Events of this security only: inside its segment, and not on rows cut as another's.
        flagged = event_flags(events, daily)
        start = max(filter(None, [request.get("segment_start", ""), cut["first"]]), default="")
        end = min(filter(None, [request.get("segment_end", ""), cut["last"]]), default="")
        if start:
            flagged = flagged[flagged["ex_date"] >= pd.Timestamp(start)]
        if end:
            flagged = flagged[flagged["ex_date"] <= pd.Timestamp(end)]
        flagged["in_window"] = np.where(flagged["ex_date"] <= pd.Timestamp(WINDOW_END), "Y", "N")
        junction = ("segment_junction" if request.get("segment_start") else "trim_junction" if cut_before else "")
        if junction and len(window):
            # The first row after another symbol's or security's rows: S and D there come from the
            # history before it, and the move into it is a corporate action (steps 9 and 11), not a return.
            on_junction = flagged["ex_date"] <= window["date"].iloc[0]
            flagged.loc[on_junction, "flags"] = [" ".join(filter(None, [f, junction]))
                                                 for f in flagged.loc[on_junction, "flags"]]
            for e in flagged[on_junction].itertuples(index=False):
                value = f"{e.ratio:.6g}" if e.event_type != "dividend" else f"{e.div_cash_raw:.6g}"
                review.append(f"{junction}_event:{e.ex_date:%Y-%m-%d}:{e.event_type}:{value}")
        # A relisting the candidate list names (round 6: Oasis/Chord's new equity of 2020-11-20 after the
        # Form 25): when the series holds rows of the listing before it, its first row from that day on is a
        # junction; the move into it is not a return.
        relist_day, relist_text = relist_junction(window, str(request.get("junction_date", "") or ""))
        if relist_day is not None:
            on_relist = flagged["ex_date"] == relist_day
            flagged.loc[on_relist, "flags"] = [" ".join(filter(None, [f, "relist_junction"]))
                                               for f in flagged.loc[on_relist, "flags"]]
            review.append(relist_text)
        # Any other single-day level change of more than 10x on a day without a split (round 6: CHRD's
        # $0.12 -> $34 joined two equities with no flag), inside the need, is sent to review.
        jumps = level_jumps(window, flagged)
        checks["level_jumps"] = " ".join(f"{d:%Y-%m-%d}:x{r:.4g}" for d, r in jumps)
        in_need = [(d, r) for d, r in jumps if pd.Timestamp(request["needed_start"]) <= d <= pd.Timestamp(request["needed_end"])
                   and d != relist_day]
        if in_need:
            review.append("level_jump_not_a_split:" + ",".join(f"{d:%Y-%m-%d}:x{r:.4g}" for d, r in in_need))
        # Odd-ratio events after the first row set the volume of the rows before them.
        affecting = flagged[(flagged["event_type"] != "dividend")
                            & (flagged["ex_date"] > (window["date"].iloc[0] if len(window) else pd.Timestamp.max))]
        unverified = affecting[affecting["flags"].astype(str).str.contains("volume_restore_unverified")]
        if len(unverified):
            # Reported, but not a verdict change: every WIKI-tested event from 2015-07 on was scaled.
            notes.append("volume_restore_unverified:" + ",".join(
                f"{e.ex_date:%Y-%m-%d}:{e.ratio:.6g}" for e in unverified.itertuples(index=False)))
        as_served_rows = affecting[affecting["flags"].astype(str).str.contains("volume_not_scaled_by_yahoo")]
        if len(as_served_rows):
            notes.append("volume_not_scaled_by_yahoo:" + ",".join(
                f"{e.ex_date:%Y-%m-%d}:{e.ratio:.6g}" for e in as_served_rows.itertuples(index=False)))
        if checks["verdict"] not in ACCEPTED:
            review = []
        elif review and checks["verdict"] == "ok":
            checks["verdict"] = "review"
        checks["verdict_reasons"] = "; ".join(filter(None, [checks["verdict_reasons"], *review, *notes]))
        report_rows.append({**base, **checks, "claimed_cut_first": cut["first"], "claimed_cut_last": cut["last"]})
        if checks["verdict"] in ACCEPTED and len(window):
            yahoo_weeks.setdefault(sid, set()).update(window["date"].dt.to_period("W-SUN"))
        event_frames.append(flagged.assign(security_id=sid, symbol=symbol, verdict=checks["verdict"]))
        # (an empty body, AGEND's ECNQUOTE answer, has an untyped date column)
        frame = window.assign(date=pd.to_datetime(window["date"]).dt.strftime("%Y-%m-%d"), symbol=symbol, junction="")
        if junction and len(frame):
            frame.iloc[0, frame.columns.get_loc("junction")] = "Y"
        if relist_day is not None:
            frame.loc[frame["date"] == relist_day.strftime("%Y-%m-%d"), "junction"] = "Y"
        (accepted if checks["verdict"] in ACCEPTED else rejected).setdefault(sid, []).append(frame)
        if k % 100 == 0:
            log(f"build: {k}/{len(requests)} securities")
    # One file per security (segments joined in date order); a series of another entity is kept
    # for review under rejected/, never next to the accepted ones.
    for sid in sorted(set(accepted) | set(rejected)):
        for frames, target in ((accepted.get(sid), out_dir / f"{sid}.csv.gz"),
                               (rejected.get(sid), out_dir / "rejected" / f"{sid}.csv.gz")):
            if not frames:
                target.unlink(missing_ok=True)
                continue
            joined = pd.concat(frames, ignore_index=True).sort_values("date")
            common.atomic_write(target, gzip.compress(series_csv(joined), mtime=0))
    # Series of securities the candidate list no longer asks for are moved aside (not_requested/),
    # so that every file next to the reports belongs to this build.
    wanted, moved = set(requests["security_id"]), []
    for folder, aside in ((out_dir, out_dir / "not_requested"), (out_dir / "rejected", out_dir / "not_requested" / "rejected")):
        for path in sorted(folder.glob("*.csv.gz")) if folder.exists() else []:
            if path.name[:-len(".csv.gz")] not in wanted:
                aside.mkdir(parents=True, exist_ok=True)
                path.replace(aside / path.name)
                moved.append(path.name[:-len(".csv.gz")])
    if moved:
        log(f"build: {len(moved)} series of securities no longer requested moved to not_requested/: {' '.join(moved)}")
    report = pd.DataFrame(report_rows)
    events = pd.concat(event_frames, ignore_index=True) if event_frames else pd.DataFrame(columns=EVENT_COLUMNS)
    events["ex_date"] = pd.to_datetime(events["ex_date"]).dt.strftime("%Y-%m-%d")
    events = events.reindex(columns=EVENT_COLUMNS)
    common.atomic_write(out_dir / "fetch_status.csv", status.to_csv(index=False).encode("utf-8"))
    common.atomic_write(out_dir / "entity_report.csv", report.to_csv(index=False).encode("utf-8"))
    common.atomic_write(out_dir / "events.csv", events.to_csv(index=False, float_format=FLOAT_FORMAT).encode("utf-8"))
    weekly = weekly_coverage(yahoo_weeks, metrics_path)
    if not weekly.empty:
        common.atomic_write(out_dir / "weekly_coverage_after_yahoo.csv",
                            weekly.assign(week_end=weekly["week_end"].dt.strftime("%Y-%m-%d"))
                            .to_csv(index=False).encode("utf-8"))
    summary = summarize(requests, status, report, events, outcome)
    summary["moved_to_not_requested"] = moved
    summary["top300_vendor_coverage_by_year"] = coverage_by_year(weekly)
    common.atomic_write(out_dir / "summary.json", (json.dumps(summary, indent=2, default=str) + "\n").encode("utf-8"))
    log(f"build: {summary['verdicts']}; fetch {summary['fetch_status']}")
    return summary


def weekly_coverage(yahoo_weeks: dict[str, set], metrics_path: Path = WEEKLY_METRICS) -> pd.DataFrame:
    """Per week: of the universe names at dv50 rank <= 300 (step 6's ranks; dollar volume only),
    how many had a vendor raw series before this step (step 6's ``vendor_ok``) and how many have
    one now that the accepted Yahoo series are added (a Yahoo row in that calendar week)."""
    if not metrics_path.exists():
        return pd.DataFrame()
    weekly = pd.read_pickle(metrics_path)
    top = weekly[weekly["universe"] & (weekly["dv50_rank"] <= 300)][["security_id", "week_end", "vendor_ok"]].copy()
    keys = top["week_end"].dt.to_period("W-SUN")
    top["yahoo"] = [k in yahoo_weeks.get(s, ()) for s, k in zip(top["security_id"], keys)]
    top["after"] = top["vendor_ok"] | top["yahoo"]
    out = top.groupby("week_end").agg(top300=("security_id", "size"), vendor_before=("vendor_ok", "sum"),
                                      yahoo_rows=("yahoo", "sum"), vendor_after=("after", "sum")).reset_index()
    out["stored_only_after"] = out["top300"] - out["vendor_after"]
    return out


def coverage_by_year(weekly: pd.DataFrame) -> dict:
    if weekly.empty:
        return {}
    years = weekly.groupby(weekly["week_end"].dt.year)
    return {int(y): {"before_median": int(g["vendor_before"].median()), "after_median": int(g["vendor_after"].median()),
                     "after_min": int(g["vendor_after"].min()), "after_max": int(g["vendor_after"].max()),
                     "stored_only_after_max": int(g["stored_only_after"].max())} for y, g in years}


def summarize(requests: pd.DataFrame, status: pd.DataFrame, report: pd.DataFrame, events: pd.DataFrame,
              outcome: dict | None = None) -> dict:
    flags = events["flags"].fillna("").astype(str)
    flagged = flags[flags != ""]
    reasons = report.get("verdict_reasons", pd.Series(dtype=str)).fillna("").astype(str)
    cuts = reasons.str.findall(r"claimed_rows_cut:(\d+)").map(lambda found: sum(int(x) for x in found))
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pairs": int(len(requests)), "symbols": int(requests["symbol"].nunique()),
        "fetch_status": status["status"].value_counts().to_dict(),
        "verdicts": report["verdict"].value_counts().to_dict(),
        "verdicts_by_reason": {r: g["verdict"].value_counts().to_dict()
                               for r, g in report.assign(r=report["reasons"].str.split().str[0]).groupby("r")},
        "need_coverage_median": float(pd.to_numeric(report.get("need_coverage"), errors="coerce").median()),
        "events": {"splits": int((events["event_type"] != "dividend").sum()),
                   "dividends": int((events["event_type"] == "dividend").sum()),
                   "flag_counts": flagged.str.split().explode().value_counts().to_dict() if len(flagged) else {}},
        "odd_ratio_volume_restore": odd_ratio_volume_summary(events),
        "claimed_rows_cut": {"series": int(cuts.astype(bool).sum()), "rows": int(cuts.sum())},
        "level_jumps_not_a_split": {
            "series": int(report.get("level_jumps", pd.Series(dtype=str)).fillna("").astype(str).ne("").sum()),
            "in_need_sent_to_review": int(reasons.str.contains("level_jump_not_a_split").sum()),
            "pairs": {f"{r.security_id}:{r.symbol}": r.level_jumps for r in report.itertuples(index=False)
                      if str(getattr(r, "level_jumps", "") or "") not in ("", "nan")}},
        "relist_junctions": [m for m in reasons.str.findall(r"relist_junction:\d{4}-\d{2}-\d{2}").sum()]
                            if len(reasons) else [],
        "symbols_replaced_by_sec_ticker": {r.symbol: r.message for r in status.itertuples(index=False)
                                           if "replaced by" in str(r.message)},
        "stored_dv_check_note": "the stored files hold Yahoo's served volume, so stored_dv tests the entity, "
                                "not the volume restore",
        "requests_logged": common.quota_used(SOURCE),
        "requests": request_ledger(set(status["symbol"])),
        "this_run": {"asked": int(len(outcome or {})),
                     "by_status": dict(Counter(o["status"] for o in (outcome or {}).values()))},
    }


def request_ledger(requested: set[str], raw_dir: Path = RAW_DIR, ledger_path: Path = common.QUOTA_LEDGER,
                   index_path: Path = common.RAW_INDEX) -> dict:
    """Every Yahoo request the quota ledger holds (the count of record: one row per request, plain CSV),
    split into this step's (a v8 chart whose answer is cached here, or whose 404 marker is) and other
    steps' (step 11's terminal prices), with the raw index beside it (gzip, written at the same moment:
    rows it lacks are listed) and the files on disk (round 6: the summary named "1,599 new tickers"
    while the run had asked 1,600, AGEND and four shells left out later among them)."""
    if not Path(ledger_path).exists():
        return {"ledger": "missing"}
    ledger = pd.read_csv(ledger_path, dtype=str, keep_default_na=False)
    ledger = ledger[ledger["source"] == SOURCE].copy()
    index = pd.read_csv(index_path, dtype=str, keep_default_na=False) if Path(index_path).exists() else pd.DataFrame(
        columns=["fetched_utc", "source", "url_redacted", "http_status", "cache_path"])
    index = index[index["source"] == SOURCE].copy()
    index["symbol"] = index["url_redacted"].str.extract(r"/chart/([^?]+)\?", expand=False).fillna("")
    raw_dir = Path(raw_dir)
    bodies = {p.name.split("__")[0] for p in raw_dir.glob("*__*.json.gz")} if raw_dir.exists() else set()
    markers = {p.name.split("__")[0] for p in raw_dir.glob("*__*.json.gz.404")} if raw_dir.exists() else set()
    elsewhere = set(index.loc[index["cache_path"].ne("") & ~index["cache_path"].str.startswith(str(raw_dir)), "symbol"])
    here_paths = index.loc[index["cache_path"].str.startswith(str(raw_dir)), ["fetched_utc", "symbol"]]
    here_keys = set(zip(here_paths["fetched_utc"], here_paths["symbol"]))
    other_keys = set(zip(index.loc[index["cache_path"].ne("") & ~index["cache_path"].str.startswith(str(raw_dir)),
                                   "fetched_utc"],
                         index.loc[index["cache_path"].ne("") & ~index["cache_path"].str.startswith(str(raw_dir)),
                                   "symbol"]))
    keys = list(zip(ledger["fetched_utc"], ledger["symbol"]))
    mine = []
    for (stamp, symbol), status in zip(keys, ledger["status"]):
        if (stamp, symbol) in here_keys:
            mine.append(True)
        elif (stamp, symbol) in other_keys:
            mine.append(False)
        else:  # not in the index (a 404 or an error has no path; 31 rows of 2026-10-01 were lost from it)
            mine.append(symbol in bodies or symbol in markers or symbol not in elsewhere)
    ledger["this_step"] = mine
    step = ledger[ledger["this_step"]]
    counts = step["symbol"].value_counts()
    first_asked = step.groupby("symbol")["fetched_utc"].min()
    not_requested = sorted(set(step["symbol"]) - set(requested))
    indexed = set(zip(index["fetched_utc"], index["symbol"]))
    missing_index = [f"{s}@{t}" for t, s in keys if (t, s) not in indexed]
    return {
        "ledger_requests": int(len(ledger)), "ledger_by_month": dict(Counter(ledger["month"])),
        "ledger_by_status": dict(Counter(ledger["status"])), "ledger_unique_symbols": int(ledger["symbol"].nunique()),
        "this_step": {"requests": int(len(step)), "by_status": dict(Counter(step["status"])),
                      "by_day_utc": dict(Counter(step["fetched_utc"].str[:10])),
                      "unique_symbols": int(step["symbol"].nunique()),
                      "symbols_asked_more_than_once": {s: int(n) for s, n in counts[counts > 1].items()},
                      "symbols_asked_not_in_this_build": {s: first_asked[s] for s in not_requested},
                      "raw_bodies_on_disk": len(bodies), "raw_404_markers_on_disk": len(markers),
                      "symbols_in_this_build_never_asked": sorted(set(requested) - set(step["symbol"]))},
        "other_steps": {"requests": int((~ledger["this_step"]).sum()),
                        "by_cache_dir": dict(Counter(Path(p).parent.name for p in index.loc[
                            index["cache_path"].ne("") & ~index["cache_path"].str.startswith(str(raw_dir)), "cache_path"])),
                        "symbols": sorted(set(ledger.loc[~ledger["this_step"], "symbol"]))},
        "raw_index_requests": int(len(index)),
        "raw_index_rows_missing": {"count": len(missing_index), "rows": missing_index[:50]},
        "checks": {"ledger_this_step_plus_other_equals_ledger": int(len(step)) + int((~ledger["this_step"]).sum()) == len(ledger),
                   "raw_index_complete": not missing_index},
    }


def odd_ratio_volume_summary(events: pd.DataFrame) -> dict:
    """Odd-ratio events of the accepted series in the window, by how their volume is restored."""
    flags = events["flags"].fillna("").astype(str)
    odd = events[flags.str.contains("odd_ratio") & events["verdict"].isin(ACCEPTED) & (events["in_window"] == "Y")]
    odd_flags = flags[odd.index]
    return {"events": int(len(odd)),
            "volume_restored_wiki_checked": int((odd["volume_evidence"].astype(str).str.startswith("wiki:")
                                                 & (odd["volume_restored"] == "Y")).sum()),
            "volume_as_served_wiki_checked": int(odd_flags.str.contains("volume_not_scaled_by_yahoo").sum()),
            "volume_restored_unverified": int(odd_flags.str.contains("volume_restore_unverified").sum()),
            "volume_check_unclear": int(odd_flags.str.contains("volume_check_unclear").sum()),
            "not_applied_by_yahoo": int(odd_flags.str.contains("not_applied_by_yahoo").sum())}


# ------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fetch", action="store_true", help="fetch the symbols not yet cached, then build")
    parser.add_argument("--fetch-only", action="store_true", help="fetch, do not build")
    parser.add_argument("--limit", type=int, default=None, help="fetch at most this many symbols")
    parser.add_argument("--symbols", default="", help="comma-separated subset of symbols")
    parser.add_argument("--retry", default="", help="comma-separated symbols to ask again (query2 host)")
    parser.add_argument("--period1", default=PERIOD1, help="first day asked (default 2011-06-01; a need start "
                                                           "for --retry of a truncated answer)")
    args = parser.parse_args(argv)
    candidates = pd.read_csv(CANDIDATES, dtype=str, keep_default_na=False)
    requests = request_rows(candidates, alternates=sec_alternates(pd.read_csv(MASTER, dtype=str, keep_default_na=False)))
    symbols = sorted(set(requests["symbol"]))
    chosen = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
    if chosen:
        symbols = [s for s in symbols if s in chosen]
    log(f"requests: {len(requests)} (security, symbol) pairs, {len(set(requests['symbol']))} symbols, "
        f"{int((requests['alt_symbols'] != '').sum())} with an SEC current ticker to ask if their answer is poor")
    outcome = {}
    if args.retry:
        again = sorted({x.strip().upper() for x in args.retry.split(",") if x.strip()} & set(requests["symbol"]))
        outcome.update(fetch_symbols(again, retry=True, period1=args.period1))
    if args.fetch or args.fetch_only:
        outcome.update(fetch_symbols(symbols, limit=args.limit, period1=args.period1))
        stopped = [s for s, o in outcome.items() if o["status"] == "stopped"]
        if not stopped:
            # Second pass: the SEC current tickers of the requests whose answer is a 404, empty or short.
            from scripts.reversal_data_prefilter import xnas_sessions

            poor = poor_answers(requests, xnas_sessions(WINDOW_START, WINDOW_END))
            alternates = sorted({a for k in poor for a in str(requests.at[k, "alt_symbols"]).split()}
                                - set(symbols) - set(outcome))
            if chosen:
                alternates = [a for a in alternates if a in chosen or requests.loc[
                    requests["alt_symbols"].str.split().apply(lambda x: a in x), "symbol"].isin(chosen).any()]
            log(f"yahoo: {len(poor)} poor answers with an SEC current ticker; {len(alternates)} alternates to ask")
            limit = None if args.limit is None else max(0, args.limit - len([o for o in outcome.values()]))
            outcome.update(fetch_symbols(alternates, limit=limit, period1=args.period1))
            stopped = [s for s, o in outcome.items() if o["status"] == "stopped"]
        if stopped:
            log(f"stopped by HTTP {outcome[stopped[0]]['http_status']} at {stopped[0]}; re-run to resume")
    if args.fetch_only:
        return 0
    build(requests, outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
