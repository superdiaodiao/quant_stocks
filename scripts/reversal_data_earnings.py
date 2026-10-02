"""Plan step 10 (docs/reversal_2012_2026_data_plan.md, sections 5.1-5.2): earnings dates and point-in-time SIC.

Data only. Nothing here computes returns, signals, return rankings or spreads. It
records when each company's earnings 8-K reached EDGAR, the XNAS session that
first traded on it (D0), and the SIC code printed in each filing's header.

Scope: every CIK that can enter the weekly top-300 universe: one of its securities is in
``INPUTS/weekly_universe_top300.csv.gz`` (step 12) in a domestic week, or a row of
``candidate_fetch_list.csv`` (step 6) names it. Each CIK gets one event window, from its first
possible universe day (the Monday of its first top-300 week, or its earliest candidate
``needed_start``, which already carries the prefilter's 75-day dv50 warm-up) less WARMUP_DAYS
(120: the longest regular gap between quarterly releases, so the release before the first possible
week is in the table), not before 2011-10-01, to its last possible day (its last top-300 week or
latest ``needed_end``) plus HOLD_DAYS (35: the plan's hold of up to 4 weeks, plus D0+1), and at least
to the end of that day's calendar quarter (so every in-scope quarter is wholly in the window). Every
filing from 2011-10-01 is planned from the submissions (so fiscal quarters and event kinds see the
whole sequence), but only those filed inside the window get a header fetched and a row written.
Coverage is measured on the company-quarters with a domestic week in scope (a top-300 week, or a
listed week of ``CACHE/prefilter/weekly_metrics.pkl`` inside a candidate window) and listed the whole
quarter. Foreign filers are out (the owner excluded them): a CIK flagged Y in security_master is
foreign in every week, and a MIXED CIK in the weeks whose regime in force is foreign
(``regime_on`` over ``INPUTS/periodic_form_history.csv``, written by step 4). Those weeks
count nowhere here: not for the top-300 test, the listed spans or any coverage figure.
Event rows of a MIXED CIK are all kept; ``foreign_regime_on_d0`` = Y marks those whose
D0 falls in a foreign span (a real release, whose D0-1..D0+1 window can reach the first
domestic week). UNKNOWN filers stay in scope and are counted.

Steps:
1. Submissions JSON (cached in step 4) plus every older page whose ``filingTo`` is on
   or after 2011-10-01 (``CACHE/raw/sec/submissions/CIK##########-submissions-NNN.json.gz``).
2. Events: every 8-K and 8-K/A filed from 2011-10-01 whose ``items`` list holds 2.02. No
   event is dropped: the strategy excludes the days around any earnings release,
   preannouncements included.
3. Each event is assigned a fiscal quarter: the latest period end before its filing
   date, from the company's own 10-Q/10-K ``reportDate`` values (extended by 91-day
   steps past the last known period). ``event_kind`` then says what each event is
   (from filing dates, then checked against the 8-K's own Item 2.02 text, step 6b; the
   exhibit descriptions in the headers are almost all generic, e.g. 'EX-99.1'):
   - ``amendment``: an 8-K/A linked (``amends_accession``) to the Item 2.02 8-K it amends,
     by the same period of report, else the nearest Item 2.02 8-K filed up to 7 days before,
     else the latest Item 2.02 8-K of the same fiscal quarter;
   - ``results_release``: one per company-quarter. With one event it is that event. With
     several, it is the first event of the last run of original 8-Ks (consecutive filings at
     most 3 days apart) filed no later than the day after the quarter's 10-Q/10-K, since the
     full release comes just before the report; when every one comes later, or the quarter has
     no report, it is the first. In multi-event quarters this release lies within 7 days of the
     company's usual lag after the period end in 76% of quarters (the first event: 24%). Rule (b),
     step 6b, can then move it to an earlier 8-K;
   - ``preannouncement``: an event of the quarter before its results release (preliminary
     figures, delivery or sales updates, guidance);
   - ``other``: an event after the results release of its quarter (call materials,
     supplements, a later update) or with no fiscal quarter.
   ``event_kind_basis`` says which rule applied; ``n_item202_in_fiscal_quarter``,
   ``days_to_next_item202_in_quarter``, ``days_after_period_end`` and the quarter's
   ``periodic_report_accession``/``periodic_report_filing_date`` let the protocol use another
   choice (all events, first only, the one nearest the report). ``prior_quarter_report_pending``
   = Y marks an event filed before the previous period's late 10-Q/10-K while that period has
   no event: likely that period's release (the assignment is not changed).
   ``first_in_fiscal_quarter`` = Y on the first non-amendment event of each fiscal quarter (by
   D0, then acceptance), the plan's alternative to all events; Y on every fallback row.
4. Fallback: each fiscal quarter that has a 10-Q/10-K (filed from 2011-10-01) but no
   Item 2.02 event gets that report's acceptance (the first original filing for
   the period) in ``earnings_fallback_periodic.csv`` (``event_kind`` = periodic_report).
   ``catch_up_filing`` = Y marks a report filed past its latest due date (10-Q 45 + 5 days,
   10-K 90 + 15 days with the Rule 12b-25 extension, plus 3 days for weekends: more than
   53 or 108 days after the period end; ``catch_up_reason`` past_due) or on the same D0 as
   another period's fallback of the company (shared_d0, several periods filed together);
   ``item202_between`` lists the Item 2.02 8-Ks filed after the period end and up to the
   report (assigned to a later quarter: late filers, odd period ends). A row with either
   has ``usable_as_announcement`` = N and should not be used as an announcement date.
5. ``-index-headers.html`` for every event and every fallback filing inside the window
   (``CACHE/raw/sec/headers/{cik}/{accession}-index-headers.html.gz``), through this step's
   own SEC_LIMITER (at most 4 a second, ``--sec-rate``; SEC allows 10 for all processes) and
   sec_headers(). ``ACCEPTANCE-DATETIME`` there is Eastern wall-clock
   time and is the value used; the JSON ``acceptanceDateTime`` is kept and compared
   (it is labelled Z but is sometimes Eastern time).
6. D0 = the first XNAS session whose close (16:00 ET, 13:00 on early-close days) is
   strictly after the acceptance time: after the close, or on a non-session day, it is
   the next session (kept in ``d0_session_acceptance`` on every row).
6b. The 8-K's own text (``--fetch-evidence`` fetches the primary document of the rows below,
   ``CACHE/raw/sec/docs/{cik}/{accession}/{document}.gz``; ``item202_evidence`` reads its Item 2.02
   paragraph: the stated release date and what it furnishes). Read for every non-amendment row
   whose period of report lies 2+ sessions before its acceptance D0, and for the results release
   of every quarter where the last-run rule did not pick the first event (then also the earlier
   8-Ks of quarters whose pick furnishes no release). CACHE/earnings/release_evidence.csv lists
   what was read.
   - Rule (a), late-furnished releases (T2 Biosystems 0001193125-22-147547: released 2022-05-05,
     accepted 2022-05-11): the release date is the date the Item 2.02 text states, else the period
     of report when the 8-K reports only 2.02/7.01/8.01/9.01 (with other items it is the earliest
     event's date, which may come before the release). The time of day is not known, so D0 becomes
     the latest session that could first trade on it: the session after a session-day release date
     (a release after the close), else the first session after it; never later than the acceptance
     D0. If that is earlier, ``late_furnished`` = Y and ``d0_basis`` = release_date_latest. A release
     before the close of its own day would make D0-1 the first session: the D0-1..D0+1 window
     holds both. ``report_date_lag_days``/``_sessions`` and ``report_date_kind`` say how far the
     period of report lies before D0 and whether it can be a release date.
   - Rule (b), a later 8-K picked as the release (Interface 0000715787-18-000012, slides for
     investor meetings, picked over the 2018-04-25 release 0000715787-18-000010): when the pick's
     Item 2.02 text says it furnishes something else (slides, a transcript, a supplement, monthly
     statistics; a bare or boilerplate-only paragraph never moves a release) and
     an earlier original 8-K of the quarter states one, that 8-K becomes the release
     (``event_kind_basis`` item202_text, ``release_check`` moved_from:<accession>); without one the
     pick stays (``release_check`` pick_furnishes_no_release).
7. ``sic_history.csv``: one row per header read, the SIC of the block whose CENTRAL
   INDEX KEY is the company (a multi-filer 8-K lists several).
8. Completeness of the cached headers is checked by scanning the cache itself
   (``header_cache_scan``: each file present, readable gzip, the right accession, an
   acceptance time), not the shared request log, which lost lines on 2026-10-01.

``security_id`` holds every in-scope security of the CIK, space-joined, for a multi-class
company (one event row per filing): a join on security_id must split it first.

9. Hand sample (``--hand-sample``, plan section 6). Round 7 drew 20 in-scope company-quarters with
   a results release (numpy default_rng(20261002), whole scope); that sample found the T2 and
   Interface defects, so its 20 company-quarters (frozen in hand_sample_round7_keys.csv) are
   re-scored under the current rules, and a fresh 20 are drawn by default_rng(20261003) from the
   plan's population (results releases in company-quarters with a week in the top-300 file).
   Their filing index, 8-K and EX-99 exhibits are fetched (``CACHE/raw/sec/docs/{cik}/{accession}/``)
   and what they say is written to hand_sample_draw.csv for a person to read; that person's
   findings (hand_sample_verdicts.json, keyed by accession, judged by HAND_CRITERION) are joined
   into hand_sample.csv and INPUTS/earnings_hand_checks.csv (what validate reads). The ir_* columns
   come from the release itself (the EX-99 exhibit, or the 8-K body when there is none), never
   from the EDGAR acceptance time.

Outputs:
  INPUTS/earnings_events.csv, INPUTS/earnings_fallback_periodic.csv, sic_history.csv (``--sic-out``,
  default INPUTS/sic_history.csv)
  CACHE/earnings/earnings_summary.json   counts, header/JSON agreement, coverage on three populations (every
  in-scope week; the plan's universe, weeks in the top-300 file; validate's rank <= 250 weeks), rules (a)
  and (b) (and, with ``--before DIR``, an earlier build's coverage of the same company-quarters), cache scan
  CACHE/earnings/scope.csv (with each CIK's window), company_quarter_coverage.csv, company_year_coverage.csv,
  missing_company_quarters.csv (in-scope quarters without a usable date), no_event_companies.csv,
  foreign_scope_check.csv (each in-scope MIXED CIK: weeks and rows by regime), release_evidence.csv,
  hand_sample*.csv; INPUTS/earnings_hand_checks.csv (``--hand-sample``)

Usage::

    PYTHONPATH=. python scripts/reversal_data_earnings.py                 # fetch what is missing, then build
    PYTHONPATH=. python scripts/reversal_data_earnings.py --offline       # build from the cache only
    PYTHONPATH=. python scripts/reversal_data_earnings.py --limit-ciks 5  # a small trial
    PYTHONPATH=. python scripts/reversal_data_earnings.py --fetch-only --sec-rate 4   # fetch, write no table
    PYTHONPATH=. python scripts/reversal_data_earnings.py --fetch-evidence           # the 8-K texts of step 6b
    PYTHONPATH=. python scripts/reversal_data_earnings.py --hand-sample --sec-rate 1  # the 20-event hand check
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import gzip
import html
import json
from pathlib import Path
import re
import sys
import threading
import time
from urllib.error import HTTPError

import numpy as np
import pandas as pd

from scripts import reversal_data_common as common

INPUTS = common.INPUTS
SEC_RAW = common.RAW / "sec"
SUB_DIR = SEC_RAW / "submissions"
HEADER_DIR = SEC_RAW / "headers"
OUT = common.CACHE / "earnings"
PREFILTER = common.CACHE / "prefilter"
WEEKLY = PREFILTER / "weekly_metrics.pkl"
MASTER = INPUTS / "security_master.csv"
CANDIDATES = INPUTS / "candidate_fetch_list.csv"
UNIVERSE_TOP = INPUTS / "weekly_universe_top300.csv.gz"  # step 12: the weekly top-300 universe
EVENTS_OUT = INPUTS / "earnings_events.csv"
FALLBACK_OUT = INPUTS / "earnings_fallback_periodic.csv"
SIC_OUT = INPUTS / "sic_history.csv"
PERIODIC_HISTORY = INPUTS / "periodic_form_history.csv"  # step 4: each MIXED CIK's regime by filing

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
PAGE_URL = "https://data.sec.gov/submissions/{name}"
HEADER_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{accession}-index-headers.html"
HDR_SGML_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{accession}.hdr.sgml"
INDEX_HEADERS_FROM = "2014-06-01"

EVENTS_FROM = "2011-10-01"
WINDOW_OPEN_END = "2099-12-31"  # the window end of a CIK with no dated week or candidate window
EVENT_FORMS = {"8-K", "8-K/A"}
PERIODIC_FORMS = {"10-Q", "10-K", "10-QT", "10-KT", "10-K405", "10-QSB", "10-KSB"}
QUARTER_STEP_DAYS = 91
QUARTER_MAX_LAG_DAYS = 200  # an event this long after the last known period end is not assigned by extension
RELEASE_AFTER_REPORT_DAYS = 1  # a results release may be filed up to a day after the 10-Q/10-K
RELEASE_RUN_DAYS = 3  # events this close together form one run; the run's first is the release
AMEND_NEAREST_DAYS = 7  # an 8-K/A without a same-period 8-K amends the Item 2.02 8-K up to this far back
# Latest due date of a report, in days after the period end: any filer's deadline plus the Rule
# 12b-25 extension (10-Q 45 + 5, 10-K 90 + 15) plus 3 days for a weekend or holiday.
CATCH_UP_DAYS = {"10-Q": 53, "10-QT": 53, "10-QSB": 53, "10-K": 108, "10-KT": 108, "10-K405": 108, "10-KSB": 108}
WORKERS = 8
CHUNK = 400
# Each CIK's event window: its first possible universe day less WARMUP_DAYS (the longest regular gap
# between quarterly releases, 120 days, so the release before the first possible week is in the
# table) to its last possible day plus HOLD_DAYS (the plan's hold of up to 4 weeks, plus D0+1), and at
# least to the end of that day's calendar quarter.
WARMUP_DAYS = 120
HOLD_DAYS = 35
# SEC allows 10 requests a second for all processes together; several steps may run at once, so this
# step's share is at most 4 (orchestrator rule of 2026-10-02).
SEC_RATE_MAX = 4
SEC_LIMITER = common.SlidingWindowLimiter({1: SEC_RATE_MAX})


def set_sec_rate(rate: float) -> None:
    """Use a limiter of ``rate`` requests a second (at most SEC_RATE_MAX) for every SEC request."""
    global SEC_LIMITER
    if not 0 < rate <= SEC_RATE_MAX:
        raise ValueError(f"SEC rate must be in (0, {SEC_RATE_MAX}]")
    SEC_LIMITER = common.SlidingWindowLimiter({1: int(rate)} if rate >= 1 else {1 / rate: 1})


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def read_csv_text(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


# ------------------------------------------------------------------ scope

def _cik_int(value) -> int | None:
    text = str(value).strip()
    if not text or text.lower() in ("nan", "<na>", "none"):
        return None
    return int(float(text))


def cik_flags(master: pd.DataFrame) -> pd.Series:
    """CIK -> its securities' foreign_filer flags, space-joined (one flag for nearly every CIK)."""
    master = master.assign(_cik=master["cik"].map(_cik_int))
    return master.dropna(subset=["_cik"]).groupby("_cik")["foreign_filer"].agg(lambda s: " ".join(sorted(set(s))))


def load_history(path: Path = PERIODIC_HISTORY) -> pd.DataFrame | None:
    """``periodic_form_history.csv`` from step 4, or None when it has not been written."""
    return read_csv_text(path) if path.exists() else None


def regime_foreign(flags: pd.Series, history: pd.DataFrame | None, ciks, days) -> np.ndarray:
    """True where the CIK is a foreign filer on the day: flagged Y, or MIXED with the foreign regime
    in force (step 4's ``regime_on``: the regime set by its latest 10-K/10-Q/20-F/40-F/6-K family
    filing on or before the day). Without the history table a MIXED CIK counts as domestic."""
    ciks = pd.Series(np.asarray(ciks, dtype="int64"))
    flag = ciks.map(flags).fillna("")
    out = (flag == "Y").to_numpy().copy()
    mixed = flag.str.contains("MIXED").to_numpy()
    if history is None or not mixed.any():
        return out
    from scripts.reversal_data_security_master import regime_on

    days = pd.to_datetime(pd.Series(np.asarray(days)[mixed]), errors="coerce").dt.strftime("%Y-%m-%d").fillna("").to_numpy()
    rows = np.flatnonzero(mixed)
    for cik in sorted(set(ciks.to_numpy()[rows])):
        mine = np.flatnonzero(ciks.to_numpy()[rows] == cik)
        mine = mine[days[mine] != ""]
        out[rows[mine]] = [r == "F" for r in regime_on(history, cik, list(days[mine]))]
    return out


def domestic_weeks(weekly: pd.DataFrame, flags: pd.Series, history: pd.DataFrame | None) -> pd.DataFrame:
    """``weekly`` (rows with a CIK) without the weeks in which the CIK is a foreign filer."""
    weekly = weekly[weekly["cik"].notna()]
    foreign = regime_foreign(flags, history, weekly["cik"].astype(int), weekly["week_end"])
    return weekly[~foreign]


def load_universe_top(path: Path | None = None, ranks: bool = False) -> pd.DataFrame:
    """The weekly top-300 universe file (step 12): security_id, cik (Int64), week_end, one row per security-week
    (with ``ranks``, also rank: the better of dv50_rank and dv20_rank)."""
    columns = ["week_end", "security_id", "cik"] + (["dv50_rank", "dv20_rank"] if ranks else [])
    frame = pd.read_csv(path or UNIVERSE_TOP, dtype=str, keep_default_na=False, usecols=columns)
    frame = frame.assign(cik=pd.array([_cik_int(c) for c in frame["cik"]], dtype="Int64"),
                         week_end=pd.to_datetime(frame["week_end"]))
    if ranks:
        best = pd.concat([pd.to_numeric(frame[c], errors="coerce") for c in ("dv50_rank", "dv20_rank")], axis=1).min(axis=1)
        frame = frame.drop(columns=["dv50_rank", "dv20_rank"]).assign(rank=best)
    return frame


def candidate_windows(candidates: pd.DataFrame) -> pd.DataFrame:
    """cik, security_id, start, end for each candidate row with a CIK (start/end NaT without a needed window)."""
    def day(column):
        if column not in candidates:
            return pd.Series(pd.NaT, index=candidates.index)
        return pd.to_datetime(candidates[column].replace("", None), errors="coerce")

    out = pd.DataFrame({"cik": candidates["cik"].map(_cik_int), "security_id": candidates["security_id"].astype(str),
                        "start": day("needed_start"), "end": day("needed_end")})
    out = out[out["cik"].notna()]
    return out.assign(cik=out["cik"].astype(int)).reset_index(drop=True)


def _int_cik(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame[frame["cik"].notna()]
    return frame.assign(cik=frame["cik"].astype(int))


def scope_weeks(top: pd.DataFrame, weekly: pd.DataFrame, windows: pd.DataFrame, flags: pd.Series,
                history: pd.DataFrame | None = None) -> pd.DataFrame:
    """(cik, week_end), one row per domestic week in which the CIK can be in the universe: its weeks in
    the top-300 file, and its listed weeks (step 6 ``universe``) inside one of its candidate windows."""
    parts = [_int_cik(top)[["cik", "week_end"]]]
    listed = _int_cik(weekly[weekly["universe"]])[["cik", "week_end"]]
    dated = windows.dropna(subset=["start", "end"])
    if len(dated) and len(listed):
        merged = listed.merge(dated[["cik", "start", "end"]], on="cik")
        parts.append(merged.loc[(merged["week_end"] >= merged["start"]) & (merged["week_end"] <= merged["end"]),
                                ["cik", "week_end"]])
    weeks = pd.concat(parts, ignore_index=True).drop_duplicates()
    if weeks.empty:
        return weeks
    foreign = regime_foreign(flags, history, weeks["cik"], weeks["week_end"])
    return weeks[~foreign].sort_values(["cik", "week_end"]).reset_index(drop=True)


def build_scope(top: pd.DataFrame, weekly: pd.DataFrame, candidates: pd.DataFrame, master: pd.DataFrame,
                history: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per in-scope CIK: why it is in scope, its security_ids, its spans and its event window.

    A CIK is in scope when one of its securities is in the weekly top-300 file (``top``: security_id,
    cik, week_end) in a domestic week, or a candidate row names it. Weeks in which the CIK is a
    foreign filer (Y, or a MIXED CIK's foreign regime from ``history``) count for nothing:
    ``top300_weeks_foreign`` gives how many top-300 weeks that removed. A CIK whose every security
    is flagged foreign (Y) is marked ``excluded_foreign``. The listed span comes from ``weekly``
    (step 6: security_id, cik, week_end, universe).

    ``window_start``..``window_end`` is the span whose filings count: from its first possible universe
    day (the Monday of its first top-300 week, or its earliest candidate ``needed_start``) less
    WARMUP_DAYS, but not before EVENTS_FROM, to its last possible day (its last top-300 week, or its
    latest ``needed_end``) plus HOLD_DAYS, and at least to the end of that day's calendar quarter. One
    span per CIK, so the quarterly sequence has no holes; every calendar quarter holding a week in scope
    lies wholly inside it (WARMUP_DAYS exceeds a quarter at the start).
    """
    flags = cik_flags(master)
    top = _int_cik(top)
    top_domestic = top[~regime_foreign(flags, history, top["cik"], top["week_end"])] if len(top) else top
    removed = (top.groupby("cik").size().sub(top_domestic.groupby("cik").size(), fill_value=0).astype(int))
    windows = candidate_windows(candidates)
    listed = domestic_weeks(weekly, flags, history)
    listed = listed[listed["universe"]]
    rows: dict[int, dict] = {}

    def entry(cik: int) -> dict:
        return rows.setdefault(cik, {"cik": cik, "in_top300": "N", "in_candidates": "N", "security_ids": set()})

    for cik, sid in zip(top_domestic["cik"], top_domestic["security_id"].astype(str)):
        item = entry(int(cik))
        item["in_top300"] = "Y"
        item["security_ids"].add(sid)
    for cik, sid in zip(windows["cik"], windows["security_id"]):
        item = entry(int(cik))
        item["in_candidates"] = "Y"
        item["security_ids"].add(sid)
    master = master.assign(_cik=master["cik"].map(_cik_int))
    names = master.groupby("_cik")["name"].first()
    top_span = top_domestic.groupby("cik")["week_end"].agg(["min", "max", "size"])
    listed_span = listed.groupby(listed["cik"].astype(int))["week_end"].agg(["min", "max"])
    cand_span = windows.groupby("cik").agg(start=("start", "min"), end=("end", "max"))
    out = []
    for cik, item in sorted(rows.items()):
        flag = flags.get(cik, "")
        starts = [top_span["min"].get(cik, pd.NaT) - pd.Timedelta(days=6), cand_span["start"].get(cik, pd.NaT)]
        ends = [top_span["max"].get(cik, pd.NaT), cand_span["end"].get(cik, pd.NaT)]
        starts, ends = [d for d in starts if pd.notna(d)], [d for d in ends if pd.notna(d)]
        low = max(pd.Timestamp(EVENTS_FROM), min(starts) - pd.Timedelta(days=WARMUP_DAYS)) if starts else pd.Timestamp(EVENTS_FROM)
        # never before the end of the last day's calendar quarter, so each in-scope quarter (coverage
        # counts quarters) lies wholly inside the window
        high = (max(max(ends) + pd.Timedelta(days=HOLD_DAYS), max(ends).to_period("Q").end_time.normalize())
                if ends else pd.Timestamp(WINDOW_OPEN_END))
        out.append({
            "cik": cik, "name": names.get(cik, ""), "security_ids": " ".join(sorted(item["security_ids"])),
            "in_top300": item["in_top300"], "in_candidates": item["in_candidates"], "foreign_filer": flag,
            "excluded_foreign": "Y" if flag == "Y" else "N",
            "listed_first_week": _day(listed_span["min"].get(cik)), "listed_last_week": _day(listed_span["max"].get(cik)),
            "top300_first_week": _day(top_span["min"].get(cik)), "top300_last_week": _day(top_span["max"].get(cik)),
            "top300_weeks": int(top_span["size"].get(cik, 0)),
            "top300_weeks_foreign": int(removed.get(cik, 0)),
            "candidate_first_day": _day(cand_span["start"].get(cik)), "candidate_last_day": _day(cand_span["end"].get(cik)),
            "window_start": low.strftime("%Y-%m-%d"), "window_end": high.strftime("%Y-%m-%d"),
        })
    return pd.DataFrame(out)


def foreign_only_top300(top: pd.DataFrame, scope: pd.DataFrame) -> list[int]:
    """CIKs with a week in the top-300 file that are out of scope because every such week is foreign."""
    return sorted(set(_int_cik(top)["cik"]) - set(scope["cik"].astype(int)))


def _day(value) -> str:
    return "" if value is None or pd.isna(value) else pd.Timestamp(value).strftime("%Y-%m-%d")


def in_window(days, start: str, end: str) -> np.ndarray:
    """True where the filing date (YYYY-MM-DD) lies in the CIK's event window."""
    days = np.asarray(days, dtype=object).astype(str)
    return (days >= start) & (days <= end)


def load_scope() -> pd.DataFrame:
    weekly = pd.read_pickle(WEEKLY)[["security_id", "cik", "week_end", "universe"]]
    return build_scope(load_universe_top(), weekly, read_csv_text(CANDIDATES), read_csv_text(MASTER), load_history())


# ------------------------------------------------------------------ SEC fetches (cache first)

class StopFetching(Exception):
    """SEC refused a request (403/429): stop asking and leave the rest for a later run."""


_STOP = threading.Event()


def _sec_get(url: str, path: Path, source: str, symbol: str, offline: bool) -> bytes | None:
    """The body for ``url`` from cache, else one request through this step's SEC_LIMITER (at most
    SEC_RATE_MAX a second, not common.SEC_LIMITER); None when absent (404/offline)."""
    if path.exists():
        data = path.read_bytes()
        return gzip.decompress(data) if path.suffix == ".gz" else data
    if offline or path.with_name(path.name + ".404").exists():
        return None
    if _STOP.is_set():
        raise StopFetching("stopped after an earlier refusal")
    try:
        return common.cached_get(url, path, source=source, headers=common.sec_headers(),
                                 limiter=SEC_LIMITER, symbol=symbol)
    except FileNotFoundError:
        return None
    except HTTPError as exc:
        if exc.code in (403, 429):
            _STOP.set()
            raise StopFetching(f"HTTP {exc.code}") from None
        raise


def submissions_path(cik: int) -> Path:
    return SUB_DIR / f"CIK{int(cik):010d}.json.gz"


def load_submissions(cik: int, offline: bool = False) -> dict | None:
    data = _sec_get(SUBMISSIONS_URL.format(cik=int(cik)), submissions_path(cik), "sec_submissions",
                    f"CIK{int(cik)}", offline)
    return json.loads(data) if data else None


def pages_needed(payload: dict, since: str = EVENTS_FROM) -> list[str]:
    """Names of the older submissions pages that hold filings on or after ``since``."""
    files = (payload.get("filings") or {}).get("files") or []
    return [f["name"] for f in files if (f.get("filingTo") or "") >= since and f.get("name")]


def load_page(name: str, offline: bool = False) -> dict | None:
    data = _sec_get(PAGE_URL.format(name=name), SUB_DIR / f"{name}.gz", "sec_submissions",
                    name.split("-")[0], offline)
    return json.loads(data) if data else None


FILING_FIELDS = ["accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "form", "items",
                 "primaryDocument", "primaryDocDescription"]


def filing_table(payload: dict, pages: list[dict]) -> pd.DataFrame:
    """Every filing in the recent block and the given older pages, one row per accession."""
    blocks = [(payload.get("filings") or {}).get("recent") or {}] + list(pages)
    frames = []
    for block in blocks:
        n = len(block.get("accessionNumber") or [])
        if n:
            frames.append(pd.DataFrame({f: (block.get(f) or [""] * n) for f in FILING_FIELDS}))
    if not frames:
        return pd.DataFrame(columns=FILING_FIELDS)
    table = pd.concat(frames, ignore_index=True).fillna("")
    for column in FILING_FIELDS:
        table[column] = table[column].astype(str)
    return table.drop_duplicates("accessionNumber").reset_index(drop=True)


def company_filings(cik: int, offline: bool = False) -> dict:
    """Submissions JSON plus the needed older pages for ``cik``: the filing table and fetch facts."""
    payload = load_submissions(cik, offline)
    if payload is None:
        return {"cik": cik, "table": pd.DataFrame(columns=FILING_FIELDS), "pages_needed": 0, "pages_read": 0,
                "missing": "submissions", "fiscal_year_end": ""}
    names = pages_needed(payload)
    pages = [p for p in (load_page(name, offline) for name in names) if p is not None]
    return {"cik": cik, "table": filing_table(payload, pages), "pages_needed": len(names), "pages_read": len(pages),
            "missing": "" if len(pages) == len(names) else "pages", "fiscal_year_end": payload.get("fiscalYearEnd") or ""}


# ------------------------------------------------------------------ events, fiscal quarters, fallback

def has_item(items: str, item: str = "2.02") -> bool:
    return item in [part.strip() for part in str(items).split(",")]


def item202_filings(table: pd.DataFrame, since: str = EVENTS_FROM) -> pd.DataFrame:
    """8-K and 8-K/A filings from ``since`` whose items include 2.02."""
    mask = table["form"].isin(EVENT_FORMS) & (table["filingDate"] >= since) & table["items"].map(has_item)
    return table[mask].sort_values(["filingDate", "acceptanceDateTime", "accessionNumber"]).reset_index(drop=True)


def periodic_filings(table: pd.DataFrame) -> pd.DataFrame:
    """Original (not amended) 10-Q/10-K filings that carry a period of report."""
    mask = table["form"].isin(PERIODIC_FORMS) & (table["reportDate"] != "")
    return table[mask].sort_values(["filingDate", "acceptanceDateTime", "accessionNumber"]).reset_index(drop=True)


def period_ends(table: pd.DataFrame) -> list[str]:
    """Known fiscal period ends: the reportDate of every 10-Q/10-K, amendments included."""
    bases = table["form"].str.replace(r"/A$", "", regex=True)
    ends = table.loc[bases.isin(PERIODIC_FORMS) & (table["reportDate"] != ""), "reportDate"]
    return sorted(set(ends))


def assign_quarter(filing_date: str, ends: list[str]) -> tuple[str, str]:
    """(fiscal quarter end, how) for an event filed on ``filing_date``.

    The quarter is the latest known period end strictly before the filing date. When that
    end is more than a quarter back (a company that stopped filing 10-Qs, or skipped some)
    the grid is extended from it by 91-day steps; an event more than QUARTER_MAX_LAG_DAYS
    after its latest known end, or before the first one, is left unassigned.
    """
    before = [e for e in ends if e < filing_date]
    if not before:
        return "", "none"
    last = before[-1]
    lag = (pd.Timestamp(filing_date) - pd.Timestamp(last)).days
    if lag <= QUARTER_STEP_DAYS + 15:
        return last, "report_date"
    if lag > QUARTER_MAX_LAG_DAYS:
        return "", "none"
    steps = (lag - 1) // QUARTER_STEP_DAYS
    return (pd.Timestamp(last) + pd.Timedelta(days=QUARTER_STEP_DAYS * steps)).strftime("%Y-%m-%d"), "extended"


AMEND_LINKS = ("report_date", "nearest_item202", "same_quarter_item202")  # links to an Item 2.02 8-K: an amendment


def link_amendments(events: pd.DataFrame, table: pd.DataFrame) -> pd.DataFrame:
    """``amends_accession`` and ``amends_how`` for each 8-K/A event (blank for an 8-K).

    The amended filing is the latest 8-K filed on or before the 8-K/A with the same period of
    report (the date of the event both report): one with Item 2.02 first (report_date), else any
    (report_date_non202, e.g. an 8-K/A adding Item 2.02 to an acquisition 8-K). Without one it is
    the latest Item 2.02 8-K filed up to AMEND_NEAREST_DAYS before (nearest_item202), else the
    latest Item 2.02 8-K of the same fiscal quarter filed before it (same_quarter_item202, when
    ``events`` carries fiscal_quarter_end); else none.
    """
    out = pd.DataFrame({"amends_accession": "", "amends_how": ""}, index=events.index)
    originals = table[table["form"] == "8-K"].sort_values(["filingDate", "acceptanceDateTime", "accessionNumber"])
    is202 = originals["items"].map(has_item)
    for index, row in events[events["form"] == "8-K/A"].iterrows():
        before = originals["filingDate"] <= row["filingDate"]
        same = before & (originals["reportDate"] == row["reportDate"]) & (row["reportDate"] != "")
        near = (before & is202 & (pd.to_datetime(originals["filingDate"]) >= pd.Timestamp(row["filingDate"])
                                  - pd.Timedelta(days=AMEND_NEAREST_DAYS)))
        quarter = row.get("fiscal_quarter_end", "")
        same_quarter = (before & is202 & originals["accessionNumber"].isin(
            events.loc[events.get("fiscal_quarter_end", pd.Series("", index=events.index)) == quarter,
                       "accessionNumber"]) & (quarter != ""))
        for mask, how in ((same & is202, "report_date"), (same, "report_date_non202"), (near, "nearest_item202"),
                          (same_quarter, "same_quarter_item202")):
            if mask.any():
                out.loc[index] = [originals.loc[mask, "accessionNumber"].iloc[-1], how]
                break
        else:
            out.loc[index, "amends_how"] = "none"
    return out


def quarter_reports(table: pd.DataFrame) -> pd.DataFrame:
    """Per period end: the first original 10-Q/10-K for it (accession, form, filing date)."""
    first = periodic_filings(table).drop_duplicates("reportDate", keep="first")
    return pd.DataFrame({"fiscal_quarter_end": first["reportDate"].values,
                         "periodic_report_accession": first["accessionNumber"].values,
                         "periodic_report_form": first["form"].values,
                         "periodic_report_filing_date": first["filingDate"].values}).astype(str)


def prior_quarter_pending(events: pd.DataFrame, ends: list[str], reports: pd.DataFrame) -> list[str]:
    """Y for an event whose previous period's 10-Q/10-K was filed on or after it while that period has
    no Item 2.02 event of its own: a late filer's release for the previous period, assigned to the
    newer one. Flagged only; the assignment is kept (some such companies released on time and
    filed restated reports months later, so moving the event would be wrong as often as not)."""
    filed = dict(zip(reports["fiscal_quarter_end"], reports["periodic_report_filing_date"]))
    covered = set(events["fiscal_quarter_end"]) - {""}
    out = []
    for day, quarter, how in zip(events["filingDate"], events["fiscal_quarter_end"], events["fiscal_quarter_how"]):
        position = ends.index(quarter) if how == "report_date" and quarter in ends else 0
        previous = ends[position - 1] if position > 0 else ""
        late = bool(previous) and previous not in covered and filed.get(previous, "") >= day
        out.append("Y" if late else "N")
    return out


def _days(later: str, earlier: str) -> int:
    return (pd.Timestamp(later) - pd.Timestamp(earlier)).days


def classify_quarter(dates: list[str], report: str) -> tuple[int, str]:
    """(position of the results release, basis) among one company-quarter's events (filing dates,
    in acceptance order; amendments left out) given the quarter's 10-Q/10-K filing date ('' if none).

    The release is the first of the last run of events (each at most RELEASE_RUN_DAYS after the one
    before) filed no later than RELEASE_AFTER_REPORT_DAYS after the report; when every event is
    later, or there is no report, it is the first event."""
    if len(dates) == 1:
        return 0, "single"
    if not report:
        return 0, "first_no_report"
    by_report = [i for i, day in enumerate(dates) if _days(day, report) <= RELEASE_AFTER_REPORT_DAYS]
    if not by_report:
        return 0, "first_all_after_report"
    i = by_report[-1]
    while i > 0 and _days(dates[i], dates[i - 1]) <= RELEASE_RUN_DAYS:
        i -= 1
    return i, "last_run_by_report"


def classify_events(events: pd.DataFrame) -> pd.DataFrame:
    """event_kind, event_kind_basis, n_item202_in_fiscal_quarter, days_to_next_item202_in_quarter and
    pick_not_first (Y on a results release that is not its quarter's first non-amendment event) for
    ``events`` (cik, accession, form, filing_date, acceptance_sort, fiscal_quarter_end,
    periodic_report_filing_date, amends_how), indexed like ``events``. No row is dropped."""
    columns = ["event_kind", "event_kind_basis", "n_item202_in_fiscal_quarter", "days_to_next_item202_in_quarter",
               "pick_not_first"]
    out = pd.DataFrame("", index=events.index, columns=columns)
    if events.empty:
        return out
    order = events.sort_values(["cik", "fiscal_quarter_end", "acceptance_sort", "accession"])
    for (_, quarter), group in order.groupby(["cik", "fiscal_quarter_end"], sort=False):
        if quarter == "":
            out.loc[group.index, ["event_kind", "event_kind_basis"]] = ["other", "no_fiscal_quarter"]
            continue
        dates = group["filing_date"].tolist()
        out.loc[group.index, "n_item202_in_fiscal_quarter"] = str(len(group))
        out.loc[group.index, "days_to_next_item202_in_quarter"] = [
            str(_days(b, a)) for a, b in zip(dates, dates[1:])] + [""]
        amended = group["amends_how"].isin(AMEND_LINKS)
        out.loc[group.index[amended], ["event_kind", "event_kind_basis"]] = ["amendment", "amends_item202_8k"]
        members = group[~amended]
        if members.empty:
            continue
        # an 8-K/A not linked to an earnings 8-K (one adding Item 2.02 to another 8-K) is never chosen
        # over an original 8-K
        pool = members[members["form"] == "8-K"] if (members["form"] == "8-K").any() else members
        chosen, basis = classify_quarter(pool["filing_date"].tolist(), members["periodic_report_filing_date"].iloc[0])
        release = members.index.get_loc(pool.index[chosen])
        kinds = ["preannouncement"] * release + ["results_release"] + ["other"] * (len(members) - release - 1)
        out.loc[members.index, "event_kind"] = kinds
        out.loc[members.index, "event_kind_basis"] = basis
        out.loc[members.index, "pick_not_first"] = ["Y" if release > 0 and k == "results_release" else "N" for k in kinds]
    return out


def fallback_filings(table: pd.DataFrame, event_quarters: set[str], since: str = EVENTS_FROM) -> pd.DataFrame:
    """The first original 10-Q/10-K for each period that has no Item 2.02 event, filed from ``since``.

    ``other_8k_between`` lists ('date:items', ';'-joined) the company's 8-Ks with Item 7.01 or 8.01
    filed after the period end and up to the periodic filing: some companies (Urban Outfitters
    since 2017) put the earnings release under 8.01, so the release may predate the fallback.
    It is a flag for review, never used as the event. ``item202_between`` lists ('date:accession')
    the Item 2.02 8-Ks filed in the same span (assigned to a later quarter, so the release is
    there). ``days_after_period_end`` and ``past_due`` (filed later than CATCH_UP_DAYS) mark late
    reports; ``build_tables`` adds the shared-D0 test and ``usable_as_announcement`` (N when
    any of catch-up, an Item 2.02 in the span, or a 7.01/8.01 8-K in the span applies).
    """
    periodic = periodic_filings(table)
    periodic = periodic[periodic["filingDate"] >= since]
    first = periodic.drop_duplicates("reportDate", keep="first")
    out = first[~first["reportDate"].isin(event_quarters)].reset_index(drop=True)
    events = table[table["form"].isin(EVENT_FORMS)].sort_values(["filingDate", "accessionNumber"])
    others = events[events["items"].map(lambda items: has_item(items, "7.01") or has_item(items, "8.01"))]
    item202 = events[events["items"].map(has_item)]
    spans = list(zip(out["reportDate"], out["filingDate"]))
    out["other_8k_between"] = [
        ";".join(f"{d}:{i}" for d, i in zip(o["filingDate"], o["items"]))
        for o in (others[(others["filingDate"] > period) & (others["filingDate"] <= filed)] for period, filed in spans)]
    out["item202_between"] = [
        ";".join(f"{d}:{a}" for d, a in zip(o["filingDate"], o["accessionNumber"]))
        for o in (item202[(item202["filingDate"] > period) & (item202["filingDate"] <= filed)] for period, filed in spans)]
    out["days_after_period_end"] = [_days(filed, period) for period, filed in spans]
    out["past_due"] = [days > CATCH_UP_DAYS.get(form, CATCH_UP_DAYS["10-K"])
                       for days, form in zip(out["days_after_period_end"], out["form"])]
    return out


# ------------------------------------------------------------------ index headers

def header_path(cik: int, accession: str, kind: str = "index_headers") -> Path:
    name = f"{accession}-index-headers.html.gz" if kind == "index_headers" else f"{accession}.hdr.sgml.gz"
    return HEADER_DIR / str(int(cik)) / name


def header_url(cik: int, accession: str, kind: str = "index_headers") -> str:
    url = HEADER_URL if kind == "index_headers" else HDR_SGML_URL
    return url.format(cik=int(cik), folder=accession.replace("-", ""), accession=accession)


def header_kinds(filing_date: str) -> tuple[str, str]:
    """The header files to try, in order. EDGAR has no -index-headers.html for filings before
    about 2014-06 (all 61 such requests in the five-CIK trial were 404); the same SGML header
    is served as {accession}.hdr.sgml, so older filings ask for that first."""
    if filing_date and filing_date < INDEX_HEADERS_FROM:
        return ("hdr_sgml", "index_headers")
    return ("index_headers", "hdr_sgml")


def _absent(path: Path) -> bool:
    return path.with_name(path.name + ".404").exists()


def cached_header_kind(cik: int, accession: str) -> str:
    """Which header file is cached for the filing ('' when none)."""
    for kind in ("index_headers", "hdr_sgml"):
        if header_path(cik, accession, kind).exists():
            return kind
    return ""


def header_settled(cik: int, accession: str) -> bool:
    """A header is cached, or both header files are known to be absent."""
    return bool(cached_header_kind(cik, accession)) or all(
        _absent(header_path(cik, accession, kind)) for kind in ("index_headers", "hdr_sgml"))


def load_header(cik: int, accession: str, offline: bool = False, filing_date: str = "") -> tuple[str | None, str]:
    """(header text, kind) from the cache, else fetched: the preferred file, then the other on a 404."""
    cached = cached_header_kind(cik, accession)
    if cached:
        return gzip.decompress(header_path(cik, accession, cached).read_bytes()).decode("utf-8", "replace"), cached
    for kind in header_kinds(filing_date):
        data = _sec_get(header_url(cik, accession, kind), header_path(cik, accession, kind), "sec_headers",
                        f"CIK{int(cik)}", offline)
        if data:
            return data.decode("utf-8", errors="replace"), kind
    return None, ""


def header_cache_scan(jobs: list[tuple[int, str, str, str]]) -> dict:
    """Completeness of the cached headers from the cache itself (the shared request log lost lines
    on 2026-10-01, so it is not used): for every needed (cik, accession), is a header file cached,
    does it decompress, does its SEC header name that accession, and does it carry an acceptance time."""
    counts = Counter()
    problems: dict[str, list[str]] = {}
    for cik, accession, _, _ in jobs:
        kind = cached_header_kind(cik, accession)
        if not kind:
            status = "both_404" if header_settled(cik, accession) else "missing"
        else:
            try:
                text = gzip.decompress(header_path(cik, accession, kind).read_bytes()).decode("utf-8", "replace")
            except (OSError, EOFError, gzip.BadGzipFile):
                text = None
            if text is None:
                status = "unreadable_gzip"
            else:
                lines = _sgml_lines(text)
                named = any(line == f"<ACCESSION-NUMBER>{accession}" for line in lines) or \
                    any(line.startswith("<SEC-HEADER>") and accession in line for line in lines)
                timed = any(line.startswith("<ACCEPTANCE-DATETIME>") and len(line) >= 35 for line in lines)
                status = ("no_sec_header" if not lines else "accession_mismatch" if not named
                          else "ok" if timed else "ok_no_acceptance_time")
        counts[status] += 1
        if status != "ok":
            problems.setdefault(status, []).append(f"{cik}/{accession}")
    return {"method": "every needed header file read from CACHE/raw/sec/headers (not the request log)",
            "needed": len(jobs), "by_status": dict(counts),
            "complete": counts["ok"] + counts["ok_no_acceptance_time"] == len(jobs),
            "problems": {k: v[:50] for k, v in problems.items()}}


_TAG = re.compile(r"^<([A-Z0-9-]+)>(.*)$")
_SIC_TEXT = re.compile(r"STANDARD INDUSTRIAL CLASSIFICATION:\s*(.*?)\s*\[(\d{4})\]")


def _sgml_lines(text: str) -> list[str]:
    """The SGML header lines (the HTML comment block, or the escaped <PRE> copy)."""
    start = text.find("<SEC-HEADER>")
    if start < 0:
        text = html.unescape(text)
        start = text.find("<SEC-HEADER>")
    if start < 0:
        return []
    end = text.find("</SEC-HEADER>", start)
    return [line.strip() for line in text[start:end if end > 0 else None].splitlines()]


def parse_header(text: str, cik: int) -> dict:
    """Acceptance time (Eastern wall clock), form, period, items and the company's SIC from an index header.

    ``sic_match`` is ``cik`` when a filer block carries the company's CIK, ``first_filer``
    when none does (the first block is used) and ``none`` when the header has no filer block.
    """
    out = {"acceptance_header_et": "", "header_form": "", "header_period": "", "header_filing_date": "",
           "header_items": "", "header_sic": "", "header_sic_description": "", "header_name": "",
           "sic_match": "none", "n_filers": 0}
    blocks, current, section, items = [], None, "", []
    for line in _sgml_lines(text):
        match = _TAG.match(line)
        if not match:
            if line.startswith("</"):
                tag = line[2:].rstrip(">")
                if tag in ("FILER", "SUBJECT-COMPANY", "FILED-BY") and current is not None:
                    blocks.append(current)
                    current = None
                elif tag == "COMPANY-DATA":
                    section = ""
            continue
        tag, value = match.group(1), match.group(2).strip()
        if tag == "ACCEPTANCE-DATETIME" and len(value) >= 14:
            out["acceptance_header_et"] = f"{value[:4]}-{value[4:6]}-{value[6:8]} {value[8:10]}:{value[10:12]}:{value[12:14]}"
        elif tag == "TYPE" and not out["header_form"]:
            out["header_form"] = value
        elif tag == "PERIOD":
            out["header_period"] = f"{value[:4]}-{value[4:6]}-{value[6:8]}" if len(value) == 8 else value
        elif tag == "FILING-DATE":
            out["header_filing_date"] = f"{value[:4]}-{value[4:6]}-{value[6:8]}" if len(value) == 8 else value
        elif tag == "ITEMS":
            items.append(value)
        elif tag in ("FILER", "SUBJECT-COMPANY", "FILED-BY"):
            current = {"role": tag, "cik": None, "sic": "", "name": ""}
        elif tag == "COMPANY-DATA":
            section = "company"
        elif current is not None and section == "company":
            if tag == "CIK" and value.isdigit():
                current["cik"] = int(value)
            elif tag == "ASSIGNED-SIC":
                current["sic"] = value
            elif tag == "CONFORMED-NAME":
                current["name"] = value
    out["header_items"] = ",".join(items)
    filers = [b for b in blocks if b["role"] == "FILER"] or blocks
    out["n_filers"] = len(filers)
    chosen = next((b for b in filers if b["cik"] == int(cik)), None)
    if chosen is not None:
        out["sic_match"] = "cik"
    elif filers:
        chosen, out["sic_match"] = filers[0], "first_filer"
    if chosen is not None:
        out["header_sic"] = chosen["sic"].zfill(4) if chosen["sic"].isdigit() else chosen["sic"]
        out["header_name"] = chosen["name"]
        descriptions = {code: desc for desc, code in _SIC_TEXT.findall(html.unescape(text))}
        out["header_sic_description"] = descriptions.get(out["header_sic"], "")
    return out


# ------------------------------------------------------------------ times and D0

class XnasCloses:
    """XNAS sessions with their closes in Eastern wall-clock time (16:00, or 13:00 on early-close days)."""

    def __init__(self, start: str = "2011-06-01", end: str = "2027-09-30"):
        import exchange_calendars as xcals

        calendar = xcals.get_calendar("XNAS", start="2010-01-04", end="2027-10-01")
        sessions = calendar.sessions_in_range(start, end)
        closes = calendar.closes.loc[sessions]
        self.sessions = pd.DatetimeIndex(sessions).tz_localize(None) if sessions.tz is not None else pd.DatetimeIndex(sessions)
        self.closes = pd.DatetimeIndex(closes.dt.tz_convert("America/New_York").dt.tz_localize(None))
        self.opens = pd.DatetimeIndex(calendar.opens.loc[sessions].dt.tz_convert("America/New_York").dt.tz_localize(None))

    def d0(self, acceptance_et: str) -> tuple[str, str]:
        """(D0 session, timing) for an Eastern acceptance time 'YYYY-MM-DD HH:MM:SS'.

        D0 is the first session whose close is strictly after the acceptance; timing is
        pre_open / intraday / after_close (same calendar day as a session) or non_session.
        """
        if not acceptance_et:
            return "", ""
        stamp = pd.Timestamp(acceptance_et)
        index = int(self.closes.searchsorted(stamp, side="right"))
        if index >= len(self.closes):
            return "", ""
        session = self.sessions[index]
        day = stamp.normalize()
        position = self.sessions.searchsorted(day)
        if position < len(self.sessions) and self.sessions[position] == day:
            if stamp < self.opens[position]:
                timing = "pre_open"
            elif stamp < self.closes[position]:
                timing = "intraday"
            else:
                timing = "after_close"
        else:
            timing = "non_session"
        return session.strftime("%Y-%m-%d"), timing

    def index_on_or_after(self, day: str) -> int:
        """Position of the first session on or after the calendar day 'YYYY-MM-DD' (len(sessions) if none)."""
        return int(self.sessions.searchsorted(pd.Timestamp(day)))

    def is_session(self, day: str) -> bool:
        position = self.index_on_or_after(day)
        return position < len(self.sessions) and self.sessions[position] == pd.Timestamp(day)

    def session_at(self, position: int) -> str:
        return self.sessions[position].strftime("%Y-%m-%d") if 0 <= position < len(self.sessions) else ""

    def latest_d0_on_date(self, day: str) -> tuple[str, str]:
        """(latest D0, earliest D0) for a release made at an unknown time on the calendar day ``day``.

        On a session day the release may have come before that day's close (D0 = that session) or after
        it (D0 = the next session), so the latest D0 is the next session and the earliest is the day's
        own. On a non-session day both are the next session."""
        position = self.index_on_or_after(day)
        latest = position + 1 if self.is_session(day) else position
        return self.session_at(latest), self.session_at(position)


def json_naive(value: str) -> str:
    """'2016-04-26T20:31:09.000Z' -> '2016-04-26 20:31:09' (the label is not trusted)."""
    text = str(value).strip()
    if len(text) < 19:
        return ""
    return text[:10] + " " + text[11:19]


def utc_to_et(naive_utc: str) -> str:
    if not naive_utc:
        return ""
    stamp = pd.Timestamp(naive_utc).tz_localize("UTC").tz_convert("America/New_York").tz_localize(None)
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def json_label(json_raw: str, header_et: str) -> str:
    """How the JSON acceptanceDateTime relates to the header: utc, et_labelled_z, other or no_header."""
    naive = json_naive(json_raw)
    if not header_et:
        return "no_header"
    if not naive:
        return "no_json"
    if utc_to_et(naive) == header_et:
        return "utc"
    if naive == header_et:
        return "et_labelled_z"
    return "other"


def resolve_acceptance(json_raw: str, header_et: str, month_rule: str = "utc") -> tuple[str, str]:
    """(Eastern acceptance time, tz_resolution). The header wins; without one the JSON is read
    by ``month_rule`` (the majority label of headers in the same filing month)."""
    if header_et:
        return header_et, "header"
    naive = json_naive(json_raw)
    if not naive:
        return "", "none"
    if month_rule == "et_labelled_z":
        return naive, "json_et_rule"
    return utc_to_et(naive), "json_utc"


# ------------------------------------------------------------------ per-company plan

def plan_company(cik: int, offline: bool = False, window: tuple[str, str] = (EVENTS_FROM, WINDOW_OPEN_END)) -> dict:
    """Item 2.02 events (with fiscal quarters) and fallback periodic filings for one CIK, from its submissions.

    Every filing from EVENTS_FROM is planned, so fiscal quarters, event kinds and the fallback tests
    see the company's whole sequence; ``in_window`` marks those filed inside ``window`` (the CIK's
    event window), plus every event of a fiscal quarter that has one there, which alone get headers
    fetched and rows written."""
    facts = company_filings(cik, offline)
    table = facts.pop("table")
    ends = period_ends(table)
    events = item202_filings(table).copy()
    quarters = [assign_quarter(day, ends) for day in events["filingDate"]]
    events["fiscal_quarter_end"] = pd.Series([q for q, _ in quarters], index=events.index, dtype=object)
    events["fiscal_quarter_how"] = pd.Series([h for _, h in quarters], index=events.index, dtype=object)
    events = events.join(link_amendments(events, table))
    reports = quarter_reports(table)
    events = events.merge(reports, on="fiscal_quarter_end", how="left").fillna(
        {"periodic_report_accession": "", "periodic_report_form": "", "periodic_report_filing_date": ""})
    events["prior_quarter_report_pending"] = prior_quarter_pending(events, ends, reports)
    # a quarter whose only events amend another quarter's release has no release of its own
    covered = set(events.loc[~events["amends_how"].isin(AMEND_LINKS), "fiscal_quarter_end"]) - {""}
    fallback = fallback_filings(table, covered)
    periodic = periodic_filings(table)
    events["in_window"] = in_window(events["filingDate"], *window)
    # a fiscal quarter with one event inside the window comes whole (its release may lie just outside)
    quarters_in = set(events.loc[events["in_window"], "fiscal_quarter_end"]) - {""}
    events["in_window"] = events["in_window"] | events["fiscal_quarter_end"].isin(quarters_in)
    fallback["in_window"] = in_window(fallback["filingDate"], *window)
    return {**facts, "events": events, "fallback": fallback, "window": tuple(window), "period_ends": set(ends),
            "n_periodic_since": int((periodic["filingDate"] >= EVENTS_FROM).sum()),
            "first_filing": table["filingDate"].min() if len(table) else "",
            "last_filing": table["filingDate"].max() if len(table) else ""}


def foreign_scope_check(scope: pd.DataFrame, weekly: pd.DataFrame, flags: pd.Series, history: pd.DataFrame | None,
                        events: pd.DataFrame, fallback: pd.DataFrame, top: pd.DataFrame | None = None) -> pd.DataFrame:
    """Each in-scope MIXED CIK: its foreign spans (step 4) and how many of its top-300 weeks (``top``, the
    top-300 file) and listed weeks (``weekly``), events and fallback rows fall where the foreign regime is in force."""
    top_all = _int_cik(top) if top is not None else pd.DataFrame(columns=["cik", "week_end"])
    master_spans = {}
    if MASTER.exists():
        master = read_csv_text(MASTER)
        if "foreign_spans" in master.columns:
            master_spans = master.assign(_cik=master["cik"].map(_cik_int)).groupby("_cik")["foreign_spans"].first().to_dict()
    weekly = weekly[weekly["cik"].notna()]
    rows = []
    for record in scope[scope["foreign_filer"].str.contains("MIXED")].to_dict("records"):
        cik = int(record["cik"])
        mine = weekly[weekly["cik"].astype(int) == cik]
        top = top_all[top_all["cik"] == cik].drop_duplicates("week_end")
        listed = mine[mine["universe"]].drop_duplicates("week_end")

        def foreign(frame, column):
            return int(regime_foreign(flags, history, [cik] * len(frame), frame[column]).sum()) if len(frame) else 0

        own_events = events[events["cik"].astype(int) == cik] if len(events) else events
        own_fallback = fallback[fallback["cik"].astype(int) == cik] if len(fallback) else fallback
        rows.append({"cik": cik, "name": record["name"], "foreign_spans": master_spans.get(cik, ""),
                     "in_history": "Y" if history is not None and (history["cik"].astype(int) == cik).any() else "N",
                     "in_top300": record["in_top300"], "in_candidates": record["in_candidates"],
                     "top300_weeks": int(len(top)), "top300_weeks_foreign": foreign(top, "week_end"),
                     "listed_weeks": int(len(listed)), "listed_weeks_foreign": foreign(listed, "week_end"),
                     "events": int(len(own_events)),
                     "events_d0_foreign": int((own_events.get("foreign_regime_on_d0", pd.Series(dtype=str)) == "Y").sum()),
                     "fallback": int(len(own_fallback)),
                     "fallback_d0_foreign": int((own_fallback.get("foreign_regime_on_d0", pd.Series(dtype=str)) == "Y").sum())})
    return pd.DataFrame(rows)


def plan_all(ciks: list[int], offline: bool = False,
             windows: dict[int, tuple[str, str]] | None = None) -> tuple[dict[int, dict], dict[int, str]]:
    """(plans, failures): ``plan_company`` for every CIK (older pages fetched as needed), with progress lines;
    ``windows`` gives each CIK's event window (default: everything from EVENTS_FROM)."""
    windows = windows or {}
    plans: dict[int, dict] = {}
    failures: dict[int, str] = {}
    started = time.time()
    for start in range(0, len(ciks), CHUNK):
        chunk = ciks[start:start + CHUNK]
        results = common.parallel_map(
            lambda cik: plan_company(cik, offline, windows.get(cik, (EVENTS_FROM, WINDOW_OPEN_END))),
            chunk, WORKERS)
        for cik, result in zip(chunk, results):
            if isinstance(result, Exception):
                log(f"  CIK {cik}: {type(result).__name__}: {result}")
                failures[cik] = f"{type(result).__name__}: {result}"
                continue
            plans[cik] = result
        log(f"  submissions: {min(start + CHUNK, len(ciks))}/{len(ciks)} CIKs planned "
            f"({time.time() - started:.0f}s; pages read {sum(p['pages_read'] for p in plans.values())})")
        if _STOP.is_set():
            log("  SEC refused a request: stopping the submissions stage")
            break
    return plans, failures


def header_jobs(plans: dict[int, dict]) -> list[tuple[int, str, str, str]]:
    """(cik, accession, filing date, kind) for every event and fallback filing inside the CIK's window."""
    jobs = []
    for cik, plan in plans.items():
        for frame, kind in ((plan["events"], "item202"), (plan["fallback"], "periodic_fallback")):
            frame = frame[frame["in_window"]] if "in_window" in frame else frame
            jobs += [(cik, acc, day, kind) for acc, day in zip(frame["accessionNumber"], frame["filingDate"])]
    return jobs


def fetch_headers(jobs: list[tuple[int, str, str, str]], offline: bool = False) -> Counter:
    """Fetch every header not yet settled, in chunks, printing progress; stops on an SEC refusal."""
    todo = [(cik, acc, day) for cik, acc, day, _ in jobs if not header_settled(cik, acc)]
    counts = Counter(cached=len(jobs) - len(todo))
    log(f"headers: {len(jobs)} needed, {counts['cached']} cached, {len(todo)} to fetch")
    if offline or not todo:
        counts["not_fetched_offline"] = len(todo) if offline else 0
        return counts
    started = time.time()

    def one(item):
        cik, acc, day = item
        text, kind = load_header(cik, acc, filing_date=day)
        return kind if text is not None else "missing_both"

    for start in range(0, len(todo), CHUNK):
        chunk = todo[start:start + CHUNK]
        for result in common.parallel_map(one, chunk, WORKERS):
            counts[result if isinstance(result, str) else f"error_{type(result).__name__}"] += 1
        done = min(start + CHUNK, len(todo))
        elapsed = time.time() - started
        eta = elapsed / done * (len(todo) - done) if done else 0
        log(f"  headers: {done}/{len(todo)} ({elapsed / 60:.1f} min, eta {eta / 60:.1f} min) "
            + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        if _STOP.is_set():
            log("  SEC refused a request: stopping; re-run later to fetch the rest")
            break
    return counts


# ------------------------------------------------------------------ report dates and the 8-K's own Item 2.02 text

# An 8-K's period of report is the date of the earliest event it reports. For an earnings 8-K that is
# usually the release date, but the 8-K may be furnished days later (T2 Biosystems 0001193125-22-147547:
# released 2022-05-05, accepted 2022-05-11), and with other items (5.02, 1.01, 3.01, ...) it may be
# another event's date, before the release (Interface 0000715787-18-000010: period 2018-04-24, the
# 5.02 event; released 2018-04-25 after the close). The 8-K's own Item 2.02 paragraph says when the
# release was issued ("On April 25, 2018, Interface ... issued a press release reporting its financial
# results") and what is furnished (a release, slides, a call transcript), so it is read for the rows
# where that matters: those whose period of report lies LATE_MIN_SESSIONS or more sessions before the
# acceptance D0 (a D0-1..D0+1 window may then miss the release), and the results release of every
# quarter where the last-run rule did not pick the quarter's first event.
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{document}"
LATE_MIN_SESSIONS = 2
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"], 1)}
_DATE = re.compile(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
                   r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})\b"
                   r"|\b(\d{1,2})/(\d{1,2})/(\d{4})\b", re.I)
_ITEM = re.compile(r"\bitem\s*(\d{1,2})\s*\.\s*(\d{2})\b", re.I)
_HEADING = re.compile(r"^[\s.:\-\u2014\u2013]*(?:results\s+of\s+operations?\s+and\s+financial\s+conditions?|regulation\s+fd\s+disclosures?|other\s+events?)[\s.:\-\u2014\u2013]*", re.I)
_COVER = re.compile(r"securities\s+and\s+exchange\s+commission|date\s+of\s+report|table\s+of\s+contents|"
                    r"exact\s+name\s+of\s+registrant", re.I)
_MENTION = re.compile(r"(?:this|under|to|in|of|and|by|with|see|such)\s*$", re.I)  # "... this Item 2.02": not a heading


def report_date_timing(frame: pd.DataFrame, calendar: XnasCloses, ends: dict[int, set[str]]) -> pd.DataFrame:
    """report_date_lag_days (acceptance date, Eastern, less the period of report), report_date_lag_sessions
    (sessions from the first session on or after the period of report to the acceptance D0) and
    report_date_kind: event_date, or why the period of report cannot be the release date (blank,
    period_end: one of the company's 10-Q/10-K period ends or the event's fiscal quarter end;
    before_quarter_end: on or before the fiscal quarter end; month_end; after_acceptance)."""
    lags, sessions, kinds = [], [], []
    for cik, report, accepted, d0, quarter in zip(frame["cik"], frame["report_date"], frame["acceptance_et"],
                                                   frame["d0_session"], frame["fiscal_quarter_end"]):
        if not report or not accepted or not d0:
            lags.append(""), sessions.append(""), kinds.append("blank")
            continue
        day = accepted[:10]
        lags.append(str(_days(day, report)))
        sessions.append(str(calendar.index_on_or_after(d0) - calendar.index_on_or_after(report)))
        if report > day:
            kinds.append("after_acceptance")
        elif report in ends.get(int(cik), set()) or report == quarter:
            kinds.append("period_end")
        elif quarter and report <= quarter:
            kinds.append("before_quarter_end")
        elif pd.Timestamp(report).is_month_end:
            kinds.append("month_end")
        else:
            kinds.append("event_date")
    return pd.DataFrame({"report_date_lag_days": lags, "report_date_lag_sessions": sessions,
                         "report_date_kind": kinds}, index=frame.index)


def main_doc_path(cik: int, accession: str, document: str) -> Path:
    return DOCS_DIR / str(int(cik)) / accession / f"{document}.gz"


def doc_url(cik: int, accession: str, document: str) -> str:
    return DOC_URL.format(cik=int(cik), folder=accession.replace("-", ""), document=document)


def load_main_doc(cik: int, accession: str, document: str, offline: bool = False) -> str | None:
    """Plain text of the filing's primary document (the 8-K itself), cache first; None when absent."""
    if not document:
        return None
    data = _sec_get(doc_url(cik, accession, document), main_doc_path(cik, accession, document), "sec_docs",
                    f"CIK{int(cik)}", offline)
    return html_text(data) if data is not None else None


def parse_dates(text: str) -> list[tuple[str, int]]:
    """(YYYY-MM-DD, position) for every calendar date written in ``text`` ('May 5, 2022', 'Sept. 5 2022', '5/5/2022')."""
    out = []
    for match in _DATE.finditer(text):
        try:
            if match.group(1):
                stamp = pd.Timestamp(year=int(match.group(3)), month=_MONTHS[match.group(1)[:3].lower()],
                                     day=int(match.group(2)))
            else:
                stamp = pd.Timestamp(year=int(match.group(6)), month=int(match.group(4)), day=int(match.group(5)))
        except (ValueError, KeyError):
            continue
        out.append((stamp.strftime("%Y-%m-%d"), match.start()))
    return out


def item_section(text: str, item: tuple[str, str] = ("2", "02"), limit: int = 1500) -> str:
    """The text under the first ``Item 2.02`` heading of an 8-K, up to the next item heading (at most
    ``limit`` characters). A mention ('this Item 2.02', 'under Item 7.01') is not a heading. When the
    section is short (headings run together: 'Item 2.02 Results of Operations and Financial Condition.
    Item 7.01 Regulation FD Disclosure. ...') or refers to Item 7.01 or 8.01, the 7.01 and 8.01 sections
    that follow are added (the release is often described there)."""
    headings = [m for m in _ITEM.finditer(text) if not _MENTION.search(text[max(0, m.start() - 12):m.start()])]

    def body(n: int) -> str:
        match = headings[n]
        end = headings[n + 1].start() if n + 1 < len(headings) else len(text)
        return _HEADING.sub("", text[match.end():min(end, match.end() + limit)]).strip(" .:-—–")

    found = []
    for n, match in enumerate(headings):
        if (match.group(1), match.group(2)) != item:
            continue
        section = body(n)
        if len(section) < 60 or re.search(r"\bitems?\s*(?:7\.01|8\.01)", section, re.I):
            for later in range(n + 1, min(n + 4, len(headings))):
                if (headings[later].group(1), headings[later].group(2)) in (("7", "01"), ("8", "01")):
                    section = (section + " " + body(later)).strip()
                elif (headings[later].group(1), headings[later].group(2)) != item:
                    break
        found.append(section[:limit])
        # a heading in the cover page or a table of contents is not the item's own text
        if len(section) >= 40 and not _COVER.search(section[:400]):
            return section[:limit]
    return found[-1] if found else ""


# the first verb after a date: an issuing verb makes it a release (or call) date; 'filed' does not
_VERB_AFTER = re.compile(r"\b(?:(filed|files|filing)|issu(?:ed|es|ing)|releas(?:ed|es|ing)|announc(?:e|ed|es|ing)|report(?:ed|s|ing)|"
                         r"disclos(?:e|ed|es|ing)|publish(?:ed|es|ing)|post(?:ed|s|ing)|host(?:ed|s|ing)|held|conduct(?:ed|s|ing)|"
                         r"provid(?:ed|es|ing)|distribut(?:ed|es|ing)|disseminat(?:ed|es|ing)|made\s+available|present(?:ed|s|ing))\b", re.I)
_PERIOD_DATE = re.compile(r"\b(?:as\s+of|ended|ending|through|at|until|from|to|between)\s*$", re.I)
_CITES_EARLIER = re.compile(r"\bpreviously\b|\bfiled\b|\beffective\b", re.I)  # just before a date: an earlier filing or event
# a statement that results were released (not merely the heading 'Results of Operations and Financial Condition')
_RESULTS_STATED = re.compile(r"\b(?:announc|report|releas|issu|disclos|provid|publish|post)\w*\b[^.;]{0,200}?\b(?:financial\s+|operating\s+|"
                      r"quarterly\s+|annual\s+)?(?:results|earnings|performance)\b|\bearnings\s+(?:press\s+|news\s+)?release\b|"
                      r"\b(?:results|earnings)\s+(?:for|of)\s+(?:its|the|our)\s+(?:\w+\s+){0,4}(?:quarter|year|period|months|fiscal)|"
                      r"\b(?:financial|operating)\s+(?:information|results|data|statements)\s+(?:as\s+of\s+and\s+)?for\s+(?:its|the|our)\s+"
                      r"(?:\w+\s+){0,5}(?:quarters?|years?|months|periods?)\s+ended", re.I)
_PRELIMINARY = re.compile(r"\bprelim\w*|\bselected\s+(?:unaudited\s+)?(?:preliminary\s+)?(?:financial|operating)\s+(?:results|information|data)|"
                          r"\b(?:expected|estimated|anticipated)\s+(?:financial\s+)?(?:results|revenues?|sales|earnings)|\bpre-?announc\w*", re.I)
_PRESENTATION = re.compile(r"\bpresentation|\bslides?\b|\bslide\s+deck|\bpresent\s+to\s+investors|\binvestor\s+(?:day|meetings?|"
                           r"conferences?|materials)|\bmeet\s+(?:and\s+present\s+)?(?:to|with)\s+investors", re.I)
_TRANSCRIPT = re.compile(r"\btranscript|\bprepared\s+remarks|\bscript\b", re.I)
_STATISTICS = re.compile(r"\b(?:traffic|operating|monthly)\s+(?:statistics|data|results)|\bstatistics\s+for\b|"
                         r"\b(?:sales|revenues?|deliveries|production)\s+(?:for|through)\s+the\s+month|\bmonth(?:ly)?\s+(?:sales|revenues?)\b|"
                         r"\bmonth\s+ended\b|\bquarter[\s-]+to[\s-]+date\b", re.I)
# 'issued a press release, a copy of which is attached': a release, but the text does not say of what
_BARE_RELEASE = re.compile(r"\b(?:press|news|earnings)\s+release\b", re.I)
_SUPPLEMENT = re.compile(r"\bsupplement(?:al|ary)?\b|\bfact\s*book|\bhistorical\s+(?:financial\s+)?(?:data|information|results)|"
                         r"\brecast|\bas\s+adjusted\b", re.I)
EVIDENCE_HEAD = 1200  # characters of the Item 2.02 text read for the date and the kind


def item202_evidence(text: str | None, low: str, high: str) -> dict:
    """What the 8-K's Item 2.02 paragraph says: ``item202_date``, the first date in it after ``low`` (the
    fiscal quarter end) and on or before ``high`` (the acceptance date) that reads as the release date:
    an issuing verb (issued, announced, reported, held, conducted, ...; not 'filed') comes first after it
    or last before it in its sentence, no 'previously', 'filed' or 'effective' comes before it in that
    sentence (a date cited from an earlier filing or event), and 'On <date>' / 'dated <date>' is preferred;
    ``item202_kind``: no_document, no_item202_text, or what the paragraph furnishes: presentation,
    preliminary (preliminary, selected or estimated figures named in its first 600 characters),
    results_release (a statement that results were released), presentation, transcript, supplement or
    statistics (monthly or traffic figures; no such statement), release_unspecified (a press or news
    release named, nothing said of what it reports), other (none of these: guidance alone, an auditor
    change, boilerplate only); and the paragraph's opening."""
    if text is None:
        return {"item202_kind": "no_document", "item202_date": "", "item202_text": ""}
    section = item_section(text)
    if not section:
        return {"item202_kind": "no_item202_text", "item202_date": "", "item202_text": ""}
    head = section[:EVIDENCE_HEAD]
    issued, on = [], []
    for day, at in parse_dates(head):
        # the same sentence only ('U.S. Securities' does not end one)
        before = re.split(r"(?<![A-Z])\.\s+(?=[A-Z(])|;\s", head[max(0, at - 300):at])[-1]
        if _PERIOD_DATE.search(before[-30:]):   # 'as of December 31', 'quarter ended ...': a period, not an event
            continue
        after = _VERB_AFTER.search(head[at:at + 160])
        prior = [m for m in _VERB_AFTER.finditer(before)]
        issuing = (after is not None and not after.group(1)) or (bool(prior) and not prior[-1].group(1))
        if not low < day <= high or not issuing or _CITES_EARLIER.search(before):
            continue
        issued.append(day)
        if re.search(r"\b(?:on|dated)\s*$", head[max(0, at - 9):at], re.I):
            on.append(day)
    date = (on or issued or [""])[0]
    if _PRELIMINARY.search(head[:600]):  # read as preliminary first: that only ever blocks a move
        kind = "preliminary"
    elif _RESULTS_STATED.search(head):
        kind = "results_release"
    elif _PRESENTATION.search(head):
        kind = "presentation"
    elif _TRANSCRIPT.search(head):
        kind = "transcript"
    elif _SUPPLEMENT.search(head):
        kind = "supplement"
    elif _STATISTICS.search(head):
        kind = "statistics"
    elif _BARE_RELEASE.search(head):
        kind = "release_unspecified"
    else:
        kind = "other"
    return {"item202_kind": kind, "item202_date": date, "item202_text": head[:400]}


# The paragraph says it furnishes something other than a results release. 'release_unspecified' and
# 'other' (no statement either way: 'issued a press release, a copy of which is attached', boilerplate
# only) never move a release.
NOT_A_RELEASE = {"presentation", "transcript", "supplement", "statistics"}
# Rule (a) takes a date from the text only when the text is about a release, a call or furnished results
# ('other': an auditor change, a lawsuit, guidance alone, where the date is another event's).
LATE_TEXT_KINDS = {"results_release", "preliminary", "release_unspecified"} | NOT_A_RELEASE


def evidence_rows(events: pd.DataFrame, stage: int = 1) -> pd.DataFrame:
    """The event rows whose 8-K text is read, with ``evidence_reason``.

    Stage 1: every non-amendment row whose period of report lies LATE_MIN_SESSIONS or more sessions
    before its acceptance D0 (late_candidate), and the results release of each quarter where the
    last-run rule did not pick the first original 8-K (release_not_first), inside the CIK's window. Stage 2 (needs stage 1's
    ``item202_kind``): the earlier original 8-Ks of the quarters whose picked release furnishes no
    release (NOT_A_RELEASE), so another can be chosen."""
    events = events[events["in_window"].astype(bool)] if "in_window" in events else events
    lag = pd.to_numeric(events["report_date_lag_sessions"], errors="coerce")
    late = (lag >= LATE_MIN_SESSIONS) & (events["event_kind"] != "amendment")
    pick = events["pick_not_first"] == "Y"
    if stage == 1:
        reason = np.where(late & pick, "late_candidate;release_not_first",
                          np.where(late, "late_candidate", "release_not_first"))
        return events[late | pick].assign(evidence_reason=reason[late | pick])
    kinds = events.get("item202_kind", pd.Series("", index=events.index))
    flagged = events[pick & kinds.isin(NOT_A_RELEASE)]
    keys = set(zip(flagged["cik"], flagged["fiscal_quarter_end"]))
    picked = dict(zip(zip(flagged["cik"], flagged["fiscal_quarter_end"]), flagged["acceptance_sort"]))
    mask = [(c, q) in keys and form == "8-K" and kind != "amendment" and sort < picked[(c, q)]
            for c, q, form, kind, sort in zip(events["cik"], events["fiscal_quarter_end"], events["form"],
                                              events["event_kind"], events["acceptance_sort"])]
    return events[mask].assign(evidence_reason="earlier_than_a_non_release_pick")


def fetch_docs(rows: pd.DataFrame, offline: bool = False) -> Counter:
    """Fetch the primary document of each row not yet cached (or known absent), in chunks, printing
    progress, through this step's SEC limiter; stops on an SEC refusal."""
    todo = [(int(c), a, d) for c, a, d in zip(rows["cik"], rows["accession"], rows["primary_document"])
            if d and not main_doc_path(c, a, d).exists() and not _absent(main_doc_path(c, a, d))]
    counts = Counter(cached=len(rows) - len(todo), no_primary_document=int((rows["primary_document"] == "").sum()))
    log(f"8-K documents: {len(rows)} rows, {counts['cached']} cached or absent, {len(todo)} to fetch")
    if offline or not todo:
        counts["not_fetched_offline"] = len(todo) if offline else 0
        return counts
    started = time.time()

    def one(item):
        cik, accession, document = item
        return "fetched" if load_main_doc(cik, accession, document) is not None else "absent"

    for start in range(0, len(todo), CHUNK):
        for result in common.parallel_map(one, todo[start:start + CHUNK], WORKERS):
            counts[result if isinstance(result, str) else f"error_{type(result).__name__}"] += 1
        done = min(start + CHUNK, len(todo))
        elapsed = time.time() - started
        log(f"  documents: {done}/{len(todo)} ({elapsed / 60:.1f} min, eta {elapsed / done * (len(todo) - done) / 60:.1f} min) "
            + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        if _STOP.is_set():
            log("  SEC refused a request: stopping; re-run later to fetch the rest")
            break
    return counts


def attach_evidence(events: pd.DataFrame, rows: pd.DataFrame) -> pd.DataFrame:
    """``events`` with item202_kind / item202_date / item202_text / evidence_reason / evidence_url filled
    (from the cache only) for ``rows``; blank elsewhere."""
    events = events.copy()
    for column in ("item202_kind", "item202_date", "item202_text", "evidence_reason", "evidence_url"):
        if column not in events:
            events[column] = ""
    for index, record in rows.iterrows():
        text = load_main_doc(int(record["cik"]), record["accession"], record["primary_document"], offline=True)
        low = record["fiscal_quarter_end"] or (pd.Timestamp(record["filing_date"]) - pd.Timedelta(days=QUARTER_STEP_DAYS)).strftime("%Y-%m-%d")
        found = item202_evidence(text, low, (record["acceptance_et"] or record["filing_date"])[:10])
        reasons = ";".join(sorted(set(filter(None, events.at[index, "evidence_reason"].split(";")))
                                  | set(record["evidence_reason"].split(";"))))
        events.loc[index, ["item202_kind", "item202_date", "item202_text", "evidence_reason", "evidence_url"]] = [
            found["item202_kind"], found["item202_date"], found["item202_text"], reasons,
            doc_url(int(record["cik"]), record["accession"], record["primary_document"]) if record["primary_document"] else ""]
    return events


RELEASE_ITEMS = {"2.02", "7.01", "8.01", "9.01"}  # items that usually report the release itself
# the bare period of report is taken as the release day only when the text states a release (or is absent)
REPORT_DATE_KINDS = {"results_release", "preliminary", "release_unspecified", "no_document", "no_item202_text"}


def reclassify_by_text(events: pd.DataFrame, calendar: XnasCloses | None = None) -> pd.DataFrame:
    """Rule (b), for the Interface case: a quarter whose picked results release (last-run rule, not the
    quarter's first event) furnishes no release by its own Item 2.02 paragraph (NOT_A_RELEASE: slides,
    a call transcript, a supplement or recast, monthly or traffic statistics) takes as its release the latest earlier original 8-K whose
    paragraph reports results (item202_kind results_release). Events before it stay preannouncement,
    those after it become other, and the quarter's rows get event_kind_basis item202_text and
    release_check 'moved_from:<accession>'. Without such an earlier 8-K the pick stays, with
    release_check pick_furnishes_no_release (the protocol can use all events of such a quarter).
    Also (with ``calendar``): when the pick's own text dates the release (item202_date) to an earlier
    original 8-K of the quarter, one accepted on that day or later whose acceptance D0 is a session that
    could first trade on a release made that day, that 8-K becomes the release (release_check
    'moved_from_refurnished:...'; America's Car-Mart 0001171843-25-001354 dates its release to
    2025-03-06, and an 8-K accepted 2025-03-05 after the close is not it)."""
    events = events.copy()
    events["release_check"] = ""
    flagged = (events["pick_not_first"] == "Y") & (events["item202_kind"].isin(NOT_A_RELEASE) | (events["item202_date"] != ""))
    picks = events[flagged]
    if picks.empty:
        return events
    keys = set(zip(picks["cik"], picks["fiscal_quarter_end"]))
    pool = events[[k in keys for k in zip(events["cik"], events["fiscal_quarter_end"])]]
    for (cik, quarter), group in pool.groupby(["cik", "fiscal_quarter_end"], sort=False):
        members = group[group["event_kind"] != "amendment"].sort_values(["acceptance_sort", "accession"])
        pick = members.index[members["event_kind"] == "results_release"][0]
        before = members.iloc[:members.index.get_loc(pick)]
        before = before[before["form"] == "8-K"]
        dated, how = before.iloc[0:0], "moved_from"
        if events.at[pick, "item202_date"] and calendar is not None:
            # the pick's own text dates the release: an earlier 8-K accepted then is that release (Western
            # Digital 0001193125-13-336942 re-furnished its 2013-07-24 release on 2013-08-15)
            day = events.at[pick, "item202_date"]
            latest, earliest = calendar.latest_d0_on_date(day)
            # accepted on the release day or later, and early enough to be first traded on it
            dated = before[(before["acceptance_sort"].str[:10] >= day) & (before["d0_session_acceptance"] >= earliest)
                           & (before["d0_session_acceptance"] <= latest)]
        if len(dated):
            earlier, how = dated, "moved_from_refurnished"
        elif events.at[pick, "item202_kind"] in NOT_A_RELEASE:
            earlier = before[before["item202_kind"] == "results_release"]
            if earlier.empty:
                events.loc[pick, "release_check"] = "pick_furnishes_no_release"
                continue
        else:
            continue
        chosen = members.index.get_loc(earlier.index[-1])
        kinds = ["preannouncement"] * chosen + ["results_release"] + ["other"] * (len(members) - chosen - 1)
        events.loc[members.index, "event_kind"] = kinds
        events.loc[members.index, "event_kind_basis"] = "item202_text"
        events.loc[members.index, "pick_not_first"] = ["Y" if chosen > 0 and k == "results_release" else "N" for k in kinds]
        events.loc[members.index, "release_check"] = f"{how}:{events.at[pick, 'accession']}"
    return events


def late_furnished_d0(events: pd.DataFrame, calendar: XnasCloses) -> pd.DataFrame:
    """Rule (a), for the T2 case: a release furnished in an 8-K accepted days after it was issued.

    For every non-amendment row whose period of report lies LATE_MIN_SESSIONS or more sessions before
    its acceptance D0, the release date is the date the 8-K's own Item 2.02 paragraph gives when that
    paragraph is about a release, a call or furnished results (LATE_TEXT_KINDS; release_date_basis
    item202_text; not 'other', e.g. Quantum Computing 0001213900-24-051804, an auditor change whose
    date is the SEC's order), else the period of report when it can be an event date, the
    8-K reports nothing outside RELEASE_ITEMS and its text states a release or is absent
    (REPORT_DATE_KINDS; report_date), else none: unresolved_not_a_release when the text describes
    something else (statements of an acquired business, slides), unresolved_other_items when the
    8-K also reports other items (the period of report is then the earliest event's date, which may
    come before the release: AMD 0001193125-14-373863, period 2014-10-10 for Item 2.05, released
    2014-10-16), unresolved when the period of report is a period or month end; the acceptance D0
    stays for these. It also stays (release_date_basis release_has_own_8k) when another non-amendment
    8-K of the company, accepted on the release date or later, has its acceptance D0 on a session that
    could first trade on a release made that day: the release was furnished in time and this filing re-furnishes or supplements it
    (Texas Capital 0001193125-15-085666, 2015-03-10, on its 2015-01-21 release). The time of day of
    the release is not known, so D0 is made the latest session that could first trade on a release
    made that day: the next session when the release date is a session day (a release after its
    close), else the first session after it; it is never later than the acceptance D0. When that is
    earlier than the acceptance D0 the row is late_furnished = Y, d0_session takes it (d0_basis
    release_date_latest) and the acceptance D0 stays in d0_session_acceptance. The release may also
    have come before the close of its own day, making D0-1 the first session to trade on it: the
    protocol's D0-1..D0+1 window holds both, whatever the time of day."""
    events = events.copy()
    for column, value in (("late_furnished", "N"), ("release_date", ""), ("release_date_basis", ""),
                          ("d0_basis", "acceptance")):
        events[column] = value
    lag = pd.to_numeric(events["report_date_lag_sessions"], errors="coerce")
    candidates = events[(lag >= LATE_MIN_SESSIONS) & (events["event_kind"] != "amendment")]
    originals = events[events["event_kind"] != "amendment"]
    own_d0 = {cik: list(zip(g["accession"], g["acceptance_sort"].str[:10], g["d0_session_acceptance"]))
              for cik, g in originals.groupby("cik")}
    for index, row in candidates.iterrows():
        if row["item202_date"] and row["item202_kind"] in LATE_TEXT_KINDS:
            day, basis = row["item202_date"], "item202_text"
        elif row["report_date_kind"] == "event_date":
            items = {part.strip() for part in str(row["items"]).split(",")}
            if not items <= RELEASE_ITEMS:
                day, basis = "", "unresolved_other_items"
            elif row["item202_kind"] in REPORT_DATE_KINDS:
                day, basis = row["report_date"], "report_date"
            else:   # the text describes something other than a release (merger statements, slides, ...)
                day, basis = "", "unresolved_not_a_release"
        else:
            day, basis = "", "unresolved"
        if day:
            latest, earliest = calendar.latest_d0_on_date(day)
            if any(acc != row["accession"] and accepted >= day and earliest <= d0 <= latest
                   for acc, accepted, d0 in own_d0.get(row["cik"], [])):
                basis = "release_has_own_8k"   # a later re-furnishing: the release's own 8-K carries its D0
        events.loc[index, ["release_date", "release_date_basis"]] = [day, basis]
        if not day or basis == "release_has_own_8k":
            continue
        if latest and latest < row["d0_session_acceptance"]:
            events.loc[index, ["d0_session", "d0_basis", "late_furnished"]] = [latest, "release_date_latest", "Y"]
    return events


def first_in_fiscal_quarter(events: pd.DataFrame) -> pd.Series:
    """Y for the first non-amendment Item 2.02 event of each fiscal quarter, by D0 (then acceptance), else N."""
    out = pd.Series("N", index=events.index)
    rows = events[(events["event_kind"] != "amendment") & (events["fiscal_quarter_end"] != "")]
    first = rows.sort_values(["cik", "fiscal_quarter_end", "d0_session", "acceptance_sort", "accession"]).drop_duplicates(
        ["cik", "fiscal_quarter_end"]).index
    out[first] = "Y"
    return out


def apply_release_evidence(events: pd.DataFrame, calendar: XnasCloses) -> pd.DataFrame:
    """Read the cached 8-K texts (stage 1, then stage 2 of ``evidence_rows``), then rules (b) and (a)."""
    events = attach_evidence(events, evidence_rows(events, 1))
    events = attach_evidence(events, evidence_rows(events, 2))
    return late_furnished_d0(reclassify_by_text(events, calendar), calendar)


# ------------------------------------------------------------------ build the tables

BASE_COLUMNS = ["cik", "security_id", "accession", "form", "items", "acceptance_json_raw", "acceptance_header_et",
                "tz_resolution", "filing_date", "d0_session", "first_in_fiscal_quarter", "event_kind", "source"]
EVENT_COLUMNS = BASE_COLUMNS + [
    # extras
    "acceptance_et", "acceptance_timing", "json_label", "report_date", "fiscal_quarter_end",
    "fiscal_quarter_how", "event_kind_basis", "n_item202_in_fiscal_quarter", "days_after_period_end",
    "days_to_next_item202_in_quarter", "periodic_report_accession", "periodic_report_form",
    "periodic_report_filing_date", "prior_quarter_report_pending", "amends_accession", "amends_how", "foreign_filer",
    "foreign_regime_on_d0", "header_file",
    # rule (a): late-furnished releases; rule (b): the release chosen by the 8-K's own text
    "d0_session_acceptance", "d0_basis", "late_furnished", "release_date", "release_date_basis",
    "report_date_lag_days", "report_date_lag_sessions", "report_date_kind", "pick_not_first", "item202_kind",
    "item202_date", "release_check", "evidence_reason", "evidence_url", "primary_document"]
EVENT_KINDS = {"results_release", "preannouncement", "other", "amendment"}
EVIDENCE_COLUMNS = ["cik", "accession", "form", "items", "filing_date", "report_date", "fiscal_quarter_end",
                    "event_kind", "evidence_reason", "item202_kind", "item202_date", "item202_text", "evidence_url"]
FALLBACK_COLUMNS = BASE_COLUMNS + [
    # extras
    "acceptance_et", "acceptance_timing", "json_label", "report_date", "fiscal_quarter_end", "fiscal_quarter_how",
    "days_after_period_end", "foreign_filer", "foreign_regime_on_d0", "header_file", "other_8k_between",
    "item202_between", "catch_up_filing", "catch_up_reason", "usable_as_announcement"]
# The header's SIC goes to sic_history.csv; its form type matched the JSON form on every filing read.
SIC_COLUMNS = ["cik", "observed_date", "sic", "source_accession",
               # extras
               "form", "sic_description", "sic_match", "blank_check_6770", "sic_changed", "operating_sic_after_6770"]
PLAN_EXTRAS = ("amends_accession", "amends_how", "periodic_report_accession", "periodic_report_form",
               "periodic_report_filing_date", "prior_quarter_report_pending",
               "other_8k_between", "item202_between", "days_after_period_end", "past_due")


def _rows(cik: int, frame: pd.DataFrame, source: str, security_ids: str) -> list[dict]:
    """Table rows for ``frame``'s filings, with each cached header parsed (blank fields when none)."""
    rows = []
    for record in frame.to_dict("records"):
        acc = record["accessionNumber"]
        text, kind = load_header(cik, acc, offline=True)
        parsed = parse_header(text or "", cik)
        rows.append({
            "cik": cik, "security_id": security_ids, "accession": acc, "form": record["form"],
            "items": record["items"], "acceptance_json_raw": record["acceptanceDateTime"],
            "acceptance_header_et": parsed["acceptance_header_et"], "filing_date": record["filingDate"],
            "source": source, "report_date": record["reportDate"],
            "fiscal_quarter_end": record.get("fiscal_quarter_end", record["reportDate"]),
            "fiscal_quarter_how": record.get("fiscal_quarter_how", "report_date"),
            "header_form": parsed["header_form"], "header_sic": parsed["header_sic"],
            "header_sic_description": parsed["header_sic_description"], "sic_match": parsed["sic_match"],
            "primary_document": record["primaryDocument"], "header_file": kind,
            "json_label": json_label(record["acceptanceDateTime"], parsed["acceptance_header_et"]),
            **{key: record.get(key, "") for key in PLAN_EXTRAS}, "in_window": bool(record.get("in_window", True)),
        })
    return rows


def label_keys(row: dict) -> tuple[str, str, str]:
    """Keys for the JSON-label rule, most specific first: (company, filer agent and year, filing month).

    Which label a JSON ``acceptanceDateTime`` carries is a property of the company's submissions
    file, not of who filed: in the 2026-10-02 build every header-checked filing of 1,638 of 1,641
    CIKs carries one label (the other 3 switch on their newest filing, 2026-09-30), while the
    Eastern-labelled share is about the same for self-filed and agent-filed filings (2012 0.46
    vs 0.49, 2020 0.28 vs 0.34) and falls with the filing year for both (2026: under 0.01). The
    agent-year key holds the majority label for only 81% of headers. The agent-year and month
    keys serve only a company with no header-checked filing. The rule decides just the rows
    without a header time: Tesla 0001564590-18-023716 and Cal-Maine 0000016160-18-000093 (both
    2018-10-01; their companies are UTC-labelled, and either reading gives the same D0), plus the
    rows of a company whose headers are not cached yet (``earnings_summary.json`` headers)."""
    return (f"cik|{int(row['cik'])}", f"{row['accession'][:10]}|{row['filing_date'][:4]}", row["filing_date"][:7])


def label_rules(rows: list[dict]) -> dict[str, str]:
    """Majority JSON label (utc / et_labelled_z) among headers for each company, agent-year and month."""
    votes: dict[str, Counter] = {}
    for row in rows:
        if row["json_label"] in ("utc", "et_labelled_z"):
            for key in label_keys(row):
                votes.setdefault(key, Counter())[row["json_label"]] += 1
    return {key: counter.most_common(1)[0][0] for key, counter in votes.items()}


def finish_rows(rows: list[dict], calendar: XnasCloses, rules: dict[str, str]) -> pd.DataFrame:
    for row in rows:
        rule = next((rules[key] for key in label_keys(row) if key in rules), "utc")
        row["acceptance_et"], row["tz_resolution"] = resolve_acceptance(
            row["acceptance_json_raw"], row["acceptance_header_et"], rule)
        row["d0_session"], row["acceptance_timing"] = calendar.d0(row["acceptance_et"])
        row["acceptance_sort"] = row["acceptance_et"] or row["filing_date"]
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=list(dict.fromkeys(EVENT_COLUMNS + FALLBACK_COLUMNS))
                            + ["header_sic_description", "acceptance_sort", "past_due"])
    return frame


def mark_fallback(fallback: pd.DataFrame) -> pd.DataFrame:
    """catch_up_filing / catch_up_reason / usable_as_announcement for the fallback rows (``past_due``
    from ``fallback_filings``; shared_d0 when another period's fallback of the CIK has the same D0)."""
    fallback = fallback.copy()
    shared = fallback.duplicated(["cik", "d0_session"], keep=False) & (fallback["d0_session"] != "")
    past_due = fallback["past_due"].astype(bool)
    fallback["catch_up_reason"] = [";".join(r for r, on in (("past_due", p), ("shared_d0", s)) if on)
                                   for p, s in zip(past_due, shared)]
    fallback["catch_up_filing"] = np.where(fallback["catch_up_reason"] != "", "Y", "N")
    # A 7.01/8.01 8-K between the period end and the report may be the release itself
    # (Urban Outfitters since 2017), so the report date is not then the announcement date.
    usable = ((fallback["catch_up_filing"] == "N") & (fallback["item202_between"] == "")
              & (fallback["other_8k_between"] == ""))
    fallback["usable_as_announcement"] = np.where(usable, "Y", "N")
    fallback["event_kind"] = "periodic_report"
    return fallback


def build_tables(plans: dict[int, dict], scope: pd.DataFrame, calendar: XnasCloses,
                 history: pd.DataFrame | None = None, evidence: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(events, fallback) from the plans and the cached headers: the rows filed inside each CIK's window.

    Event kinds, quarter counts and the fallback's shared-D0 test are worked out over every planned
    filing first (a filing outside the window has no header and is timed from its JSON by the label
    rule), so a row near the window's edge is classified as it would be with the whole history. With
    ``evidence`` the cached 8-K texts then settle rules (b) and (a) (``apply_release_evidence``)."""
    ids = dict(zip(scope["cik"].astype(int), scope["security_ids"]))
    flags = pd.Series(dict(zip(scope["cik"].astype(int), scope["foreign_filer"])), dtype=object)
    event_rows, fallback_rows = [], []
    for n, (cik, plan) in enumerate(sorted(plans.items()), 1):
        event_rows += _rows(cik, plan["events"], "item202", ids.get(cik, ""))
        fallback_rows += _rows(cik, plan["fallback"], "periodic_fallback", ids.get(cik, ""))
        if n % 400 == 0:
            log(f"  parsed headers for {n}/{len(plans)} CIKs")
    rules = label_rules(event_rows + fallback_rows)
    events = finish_rows(event_rows, calendar, rules)
    fallback = finish_rows(fallback_rows, calendar, rules)
    if len(events):
        events = events.join(classify_events(events))
        events["days_after_period_end"] = [
            str(_days(day, quarter)) if quarter else "" for day, quarter in zip(events["filing_date"], events["fiscal_quarter_end"])]
        ends = {int(cik): plan.get("period_ends", set()) for cik, plan in plans.items()}
        events = events.join(report_date_timing(events, calendar, ends))
        events["d0_session_acceptance"] = events["d0_session"]
        if evidence:
            log("  release evidence: the cached 8-K texts of late and multi-event rows")
            events = apply_release_evidence(events, calendar)
        events["first_in_fiscal_quarter"] = first_in_fiscal_quarter(events)
    if len(fallback):
        fallback = mark_fallback(fallback)
        fallback["first_in_fiscal_quarter"] = "Y"
        fallback["d0_session_acceptance"] = fallback["d0_session"]
    for frame in (events, fallback):
        if len(frame):
            frame["foreign_filer"] = frame["cik"].astype(int).map(flags).fillna("")
            day = frame["d0_session"].where(frame["d0_session"] != "", frame["filing_date"])
            frame["foreign_regime_on_d0"] = np.where(regime_foreign(flags, history, frame["cik"], day), "Y", "N")
    if len(events):
        events = events[events["in_window"].astype(bool)]
    if len(fallback):
        fallback = fallback[fallback["in_window"].astype(bool)]
    order = ["cik", "acceptance_sort", "accession"]
    events = events.sort_values(order).reset_index(drop=True) if len(events) else events
    fallback = fallback.sort_values(order).reset_index(drop=True) if len(fallback) else fallback
    return events, fallback


def sic_history(events: pd.DataFrame, fallback: pd.DataFrame) -> pd.DataFrame:
    """One row per header with a SIC: the code observed on that acceptance date, change and 6770 flags."""
    frames = [f for f in (events, fallback) if len(f)]
    if not frames:
        return pd.DataFrame(columns=SIC_COLUMNS)
    both = pd.concat(frames, ignore_index=True)
    both = both[(both["header_sic"] != "") & (both["acceptance_header_et"] != "")].copy()
    both["observed_date"] = both["acceptance_header_et"].str[:10]
    both = both.sort_values(["cik", "acceptance_header_et", "accession"]).drop_duplicates(["cik", "accession"])
    out = pd.DataFrame({
        "cik": both["cik"].astype(int), "observed_date": both["observed_date"], "sic": both["header_sic"],
        "source_accession": both["accession"], "form": both["form"],
        "sic_description": both["header_sic_description"], "sic_match": both["sic_match"],
    }).reset_index(drop=True)
    out["blank_check_6770"] = np.where(out["sic"] == "6770", "Y", "N")
    previous = out.groupby("cik")["sic"].shift()
    out["sic_changed"] = np.where(previous.notna() & (previous != out["sic"]), "Y", "N")
    after = []
    for _, group in out.groupby("cik", sort=False):
        codes = group["sic"].tolist()
        later = [""] * len(codes)
        nxt = ""
        for i in range(len(codes) - 1, -1, -1):
            later[i] = nxt if codes[i] == "6770" else ""
            if codes[i] not in ("6770", "0000", ""):
                nxt = codes[i]
        after += later
    out["operating_sic_after_6770"] = after
    return out[SIC_COLUMNS]


# ------------------------------------------------------------------ coverage (counts only)

def presence(weeks: pd.DataFrame, weekly: pd.DataFrame, ciks: set[int],
             extra: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:
    """Per (cik, calendar quarter): weeks listed (step 6 ``universe``, domestic), weeks in scope
    (``weeks``: cik, week_end from ``scope_weeks``), the quarter's week count, and for each ``extra``
    name a count of that frame's (cik, week_end) weeks (e.g. top300_weeks: weeks in the top-300 file)."""
    listed = _int_cik(weekly[weekly["universe"]])[["cik", "week_end"]]
    total = (listed.drop_duplicates("week_end").assign(quarter=lambda f: f["week_end"].dt.to_period("Q").astype(str))
             .groupby("quarter").size().rename("quarter_weeks"))
    # one row per cik-week (a multi-class company counts once)
    listed = listed[listed["cik"].isin(ciks)].drop_duplicates()
    weeks = _int_cik(weeks)[["cik", "week_end"]]
    weeks = weeks[weeks["cik"].isin(ciks)].drop_duplicates()
    both = listed.merge(weeks, on=["cik", "week_end"], how="outer", indicator=True)
    both["quarter"] = both["week_end"].dt.to_period("Q").astype(str)
    both["is_listed"] = both["_merge"] != "right_only"
    both["in_scope"] = both["_merge"] != "left_only"
    out = both.groupby(["cik", "quarter"]).agg(listed_weeks=("is_listed", "sum"), scope_weeks=("in_scope", "sum")).reset_index()
    out = out.merge(total, left_on="quarter", right_index=True, how="left")
    out = out.assign(quarter_weeks=out["quarter_weeks"].fillna(0).astype(int))
    for name, frame in (extra or {}).items():
        frame = _int_cik(frame)[["cik", "week_end"]].drop_duplicates()
        counts = frame.assign(quarter=frame["week_end"].dt.to_period("Q").astype(str)).groupby(["cik", "quarter"]).size()
        out[name] = [int(counts.get((c, q), 0)) for c, q in zip(out["cik"], out["quarter"])]
    return out


def quarter_coverage(present: pd.DataFrame, events: pd.DataFrame, fallback: pd.DataFrame) -> pd.DataFrame:
    """``present`` plus event counts by D0 calendar quarter: all Item 2.02, those that are not amendments,
    results releases, fallback (all, and those usable as announcement dates)."""
    def counts(frame: pd.DataFrame, name: str, mask=None) -> pd.Series:
        if frame.empty:
            return pd.Series(dtype=int, name=name)
        frame = frame[frame["d0_session"] != ""]
        if mask is not None:
            frame = frame[mask(frame)]
        quarter = pd.PeriodIndex(pd.to_datetime(frame["d0_session"]), freq="Q").astype(str)
        result = frame.groupby([frame["cik"].astype(int).values, np.asarray(quarter)]).size().rename(name)
        result.index.names = ["cik", "quarter"]
        return result

    out = present.set_index(["cik", "quarter"])
    names = ("n_item202", "n_item202_original", "n_results_release", "n_fallback", "n_fallback_usable")
    for series in (counts(events, names[0]),
                   counts(events, names[1], lambda f: f["event_kind"] != "amendment"),
                   counts(events, names[2], lambda f: f["event_kind"] == "results_release"),
                   counts(fallback, names[3]),
                   counts(fallback, names[4], lambda f: f["usable_as_announcement"] == "Y")):
        out = out.join(series, how="left")
    out = out.fillna({name: 0 for name in names})
    for column in names:
        out[column] = out[column].astype(int)
    out["full_quarter"] = out["listed_weeks"] >= out["quarter_weeks"]
    out["usable_date"] = (out["n_item202_original"] > 0) | (out["n_fallback_usable"] > 0)
    return out.reset_index()


COVERAGE_DEFINITION = ("calendar quarters in which the CIK has at least one domestic week in scope (a week in the "
                       "weekly top-300 file, or a listed week inside one of its candidate windows) and is listed in "
                       "every week of the quarter; an event counts when its D0 falls in the quarter; a usable date is "
                       "an Item 2.02 event that is not an amendment, or a fallback with usable_as_announcement = Y")


# The populations coverage and gaps are measured on: every in-scope week (the default), the plan's
# population (weeks in the top-300 file: the universe), and validate's members (dv50 or dv20 rank <= 250
# in the window weeks 2012-01-06..2026-07-17).
POPULATIONS = {"scope": "scope_weeks", "top300_file": "top300_weeks", "rank250": "rank250_weeks"}
RANK250_WEEKS = ("2012-01-06", "2026-07-17")


def scope_quarters(quarters: pd.DataFrame, weeks_column: str = "scope_weeks") -> pd.DataFrame:
    """The company-quarters coverage is measured on (COVERAGE_DEFINITION; ``weeks_column`` picks the
    population: scope_weeks, top300_weeks or rank250_weeks)."""
    return quarters[(quarters[weeks_column] > 0) & quarters["full_quarter"]]


def coverage_summary(quarters: pd.DataFrame, weeks_column: str = "scope_weeks") -> dict:
    """Share of in-scope company-quarters (listed the whole quarter) with an event and with a usable date, by year."""
    q = scope_quarters(quarters, weeks_column).copy()
    q["year"] = q["quarter"].str[:4]
    q["any_item202"] = q["n_item202"] > 0
    q["any_event"] = (q["n_item202"] > 0) | (q["n_fallback"] > 0)
    q["any_usable"] = (q["n_item202"] > 0) | (q["n_fallback_usable"] > 0)
    by_year = q.groupby("year").agg(company_quarters=("cik", "size"), with_item202=("any_item202", "sum"),
                                    with_item202_or_fallback=("any_event", "sum"), with_usable_date=("usable_date", "sum"))
    for name, column in (("share_item202", "with_item202"), ("share_any", "with_item202_or_fallback"),
                         ("share_usable_date", "with_usable_date")):
        by_year[name] = (by_year[column] / by_year["company_quarters"]).round(4)
    return {"definition": COVERAGE_DEFINITION, "population_weeks": weeks_column,
            "company_quarters": int(len(q)), "with_item202": int(q["any_item202"].sum()),
            "with_item202_or_fallback": int(q["any_event"].sum()), "with_usable_date": int(q["usable_date"].sum()),
            "without_usable_date": int((~q["usable_date"]).sum()),
            "share_any": round(float(q["any_event"].mean()), 4) if len(q) else None,
            "share_item202_or_usable_fallback": round(float(q["any_usable"].mean()), 4) if len(q) else None,
            "share_usable_date": round(float(q["usable_date"].mean()), 4) if len(q) else None,
            "by_year": {year: {"company_quarters": int(row.company_quarters), "with_item202": int(row.with_item202),
                               "with_item202_or_fallback": int(row.with_item202_or_fallback),
                               "with_usable_date": int(row.with_usable_date),
                               "share_item202": float(row.share_item202), "share_any": float(row.share_any),
                               "share_usable_date": float(row.share_usable_date)}
                        for year, row in by_year.iterrows()}}


def company_year_table(quarters: pd.DataFrame) -> pd.DataFrame:
    """Per (cik, calendar year): listed and in-scope weeks, Item 2.02 / results-release / fallback counts."""
    q = quarters.assign(year=quarters["quarter"].str[:4])
    out = q.groupby(["cik", "year"]).agg(listed_weeks=("listed_weeks", "sum"), scope_weeks=("scope_weeks", "sum"),
                                         n_item202=("n_item202", "sum"), n_results_release=("n_results_release", "sum"),
                                         n_fallback=("n_fallback", "sum"),
                                         n_fallback_usable=("n_fallback_usable", "sum")).reset_index()
    year_weeks = (quarters.drop_duplicates("quarter").assign(year=lambda f: f["quarter"].str[:4])
                  .groupby("year")["quarter_weeks"].sum())
    out["year_weeks"] = out["year"].map(year_weeks).astype(int)
    out["full_year"] = np.where(out["listed_weeks"] >= out["year_weeks"], "Y", "N")
    out["n_quarterly_events"] = out["n_results_release"] + out["n_fallback"]
    return out


def company_year_summary(years: pd.DataFrame) -> dict:
    """Distribution of quarterly events (results releases + fallback) per in-scope company-year."""
    y = years[years["scope_weeks"] > 0]
    bucket = y["n_quarterly_events"].clip(upper=5).map(lambda n: "5+" if n >= 5 else str(int(n)))
    full = y["full_year"] == "Y"
    table = pd.crosstab(y.loc[full, "year"], bucket[full])
    return {"definition": "calendar years with at least one domestic week in scope (top-300 file or candidate window); "
                          "quarterly events = results_release Item 2.02 events plus fallback filings with D0 in the year",
            "company_years_in_scope": int(len(y)), "full_year_listed": int(full.sum()),
            "events_per_company_year": {str(k): int(v) for k, v in bucket.value_counts().sort_index().items()},
            "events_per_full_company_year": {str(k): int(v) for k, v in bucket[full].value_counts().sort_index().items()},
            "zero_item202_company_years": int((y["n_item202"] == 0).sum()),
            "zero_any_company_years": int((y["n_quarterly_events"] == 0).sum()),
            "full_years_by_year": {year: {str(k): int(v) for k, v in row.items()} for year, row in table.iterrows()}}


def gap_summary(events: pd.DataFrame, fallback: pd.DataFrame, scope: pd.DataFrame, usable_only: bool = False) -> dict:
    """Days between consecutive quarterly events (results releases and fallbacks, or only the fallbacks
    usable as announcement dates) inside the domestic listed span."""
    frames = []
    if len(events):
        frames.append(events.loc[events["event_kind"] == "results_release", ["cik", "d0_session"]])
    if len(fallback):
        keep = fallback["usable_as_announcement"] == "Y" if usable_only else slice(None)
        frames.append(fallback.loc[keep, ["cik", "d0_session"]])
    if not frames:
        return {}
    both = pd.concat(frames, ignore_index=True)
    both = both[both["d0_session"] != ""].copy()
    span = scope.set_index("cik")[["listed_first_week", "listed_last_week"]]
    both = both.join(span, on="cik")
    both = both[(both["d0_session"] >= both["listed_first_week"]) & (both["d0_session"] <= both["listed_last_week"])]
    both = both.sort_values(["cik", "d0_session"])
    days = pd.to_datetime(both["d0_session"]).groupby(both["cik"]).diff().dt.days.dropna()
    return {"gaps": int(len(days)), "share_60_120": round(float(((days >= 60) & (days <= 120)).mean()), 4),
            "under_60": int((days < 60).sum()), "over_120": int((days > 120).sum()),
            "median_days": float(days.median()) if len(days) else None}


def gap_summary_population(events: pd.DataFrame, fallback: pd.DataFrame, quarters: pd.DataFrame,
                           weeks_column: str, usable_only: bool = True) -> dict:
    """Days between consecutive quarterly events of a CIK (results releases plus fallbacks, only those
    usable as announcement dates by default) where the later event's D0 falls in one of the population's
    company-quarters (``scope_quarters(quarters, weeks_column)``): the plan's 60-120 day test on the same
    population as the coverage figure."""
    frames = []
    if len(events):
        frames.append(events.loc[events["event_kind"] == "results_release", ["cik", "d0_session"]])
    if len(fallback):
        keep = fallback["usable_as_announcement"] == "Y" if usable_only else slice(None)
        frames.append(fallback.loc[keep, ["cik", "d0_session"]])
    if not frames:
        return {}
    both = pd.concat(frames, ignore_index=True)
    both = both[both["d0_session"] != ""].assign(cik=lambda f: f["cik"].astype(int)).drop_duplicates()
    both = both.sort_values(["cik", "d0_session"])
    day = pd.to_datetime(both["d0_session"])
    both["gap"] = day.groupby(both["cik"]).diff().dt.days
    both["quarter"] = day.dt.to_period("Q").astype(str)
    keys = set(zip(scope_quarters(quarters, weeks_column)["cik"].astype(int), scope_quarters(quarters, weeks_column)["quarter"]))
    days = both.loc[[k in keys for k in zip(both["cik"], both["quarter"])], "gap"].dropna()
    return {"population_weeks": weeks_column, "usable_fallback_only": usable_only, "gaps": int(len(days)),
            "share_60_120": round(float(((days >= 60) & (days <= 120)).mean()), 4) if len(days) else None,
            "under_60": int((days < 60).sum()), "over_120": int((days > 120).sum()),
            "median_days": float(days.median()) if len(days) else None}


FF_MAPS = INPUTS / "ff_industry_maps.csv"


def ff49_lookup(path: Path = FF_MAPS) -> dict[str, str]:
    """SIC code (4 digits) -> FF49 short name, from Siccodes49 ranges."""
    maps = read_csv_text(path)
    maps = maps[maps["scheme"] == "FF49"]
    out = {}
    for lo, hi, name in zip(maps["sic_lo"].astype(int), maps["sic_hi"].astype(int), maps["short_name"]):
        for code in range(lo, hi + 1):
            out.setdefault(f"{code:04d}", name)
    return out


def sic_week_coverage(weeks: pd.DataFrame, sic: pd.DataFrame, ff49: dict[str, str], ciks: set[int]) -> dict:
    """For the name-weeks (``weeks``: cik, week_end, e.g. the top-300 file) of ``ciks``: is there a header
    SIC on or before the week, only a later one, or none; and does that SIC fall in an FF49 range (blank
    checks 6770 counted apart)."""
    top = _int_cik(weeks)[["cik", "week_end"]].drop_duplicates().sort_values("week_end")
    top = top[top["cik"].isin(ciks)]
    if sic.empty:
        return {"name_weeks": int(len(top)), "no_sic": int(len(top))}
    top = top.assign(week_end=top["week_end"].astype("datetime64[ns]"))
    obs = sic.assign(observed=pd.to_datetime(sic["observed_date"]).astype("datetime64[ns]"))
    obs = obs[["cik", "observed", "sic"]].sort_values("observed")
    merged = pd.merge_asof(top, obs, left_on="week_end", right_on="observed", by="cik", direction="backward")
    later = pd.merge_asof(top, obs, left_on="week_end", right_on="observed", by="cik", direction="forward")
    status = np.where(merged["sic"].notna(), "on_or_before", np.where(later["sic"].notna(), "earliest_after", "none"))
    code = merged["sic"].fillna(later["sic"]).fillna("")
    in_ff49 = code.map(lambda c: c in ff49)
    return {"name_weeks": int(len(top)), "sic_status": dict(Counter(status)),
            "with_sic_share": round(float((status != "none").mean()), 4),
            "ff49_range_match_share": round(float(in_ff49.mean()), 4),
            "blank_check_6770_name_weeks": int((code == "6770").sum()),
            "no_ff49_range_codes": dict(Counter(code[~in_ff49 & (code != "")]).most_common(15))}


def compact_sic_history(sic: pd.DataFrame) -> pd.DataFrame:
    """The first header of each company-year plus every header whose SIC differs from the one before.

    A point-in-time lookup (the latest row on or before t) gives the same SIC as the full list."""
    if sic.empty:
        return sic
    first_of_year = ~sic.assign(year=sic["observed_date"].str[:4]).duplicated(["cik", "year"])
    return sic[first_of_year | (sic["sic_changed"] == "Y")].reset_index(drop=True)


def no_event_companies(scope: pd.DataFrame, plans: dict[int, dict], events: pd.DataFrame,
                       fallback: pd.DataFrame) -> pd.DataFrame:
    """In-scope CIKs with no Item 2.02 event (and whether a fallback filing exists)."""
    n202 = events.groupby("cik").size() if len(events) else pd.Series(dtype=int)
    nfb = fallback.groupby("cik").size() if len(fallback) else pd.Series(dtype=int)
    rows = []
    for record in scope.to_dict("records"):
        cik = int(record["cik"])
        if n202.get(cik, 0) > 0:
            continue
        plan = plans.get(cik, {})
        rows.append({**{k: record[k] for k in ("cik", "name", "security_ids", "foreign_filer", "in_top300",
                                               "in_candidates", "listed_first_week", "listed_last_week",
                                               "top300_weeks")},
                     "n_fallback": int(nfb.get(cik, 0)), "n_periodic_since_2011_10": plan.get("n_periodic_since", 0),
                     "first_filing": plan.get("first_filing", ""), "last_filing": plan.get("last_filing", ""),
                     "submissions_missing": plan.get("missing", "no_plan")})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ hand sample (plan section 6: events checked by hand)

HAND_SAMPLE_SEED = 20261002        # round 7's draw: the whole scope of the 2026-10-02 14:15 build (it found T2 and Interface)
HAND_SAMPLE_SEED_FRESH = 20261003  # the fresh draw, its seed fixed before drawing (2026-10-02, after rules (a) and (b)), on
                                   # the plan's population: results releases in universe company-quarters (top-300 file weeks)
HAND_SAMPLE_N = 20
DOCS_DIR = SEC_RAW / "docs"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{accession}-index.htm"
QUARTERS_OUT = OUT / "company_quarter_coverage.csv"
HAND_DRAW = OUT / "hand_sample_draw.csv"
HAND_VERDICTS = OUT / "hand_sample_verdicts.json"  # the checker's findings, written by hand, keyed by accession
HAND_SAMPLE = OUT / "hand_sample.csv"
HAND_ROUND7_KEYS = OUT / "hand_sample_round7_keys.csv"  # frozen: round 7's 20 company-quarters and its verdicts
HAND_CHECKS = INPUTS / "earnings_hand_checks.csv"  # what validate's check_earnings_hand_sample reads
HAND_CRITERION = (
    "d0_matches = Y when the row is the quarter's results release (is_quarterly_results = Y) and every session that "
    "could be the first to trade on the release lies in D0-1..D0, so the protocol's D0-1..D0+1 window holds it and D0 "
    "is never before it. The possible first sessions come from the release date and time stated in the release "
    "exhibit or the 8-K, read on sec.gov (a release is never later than its 8-K's acceptance); with no stated time, "
    "every time of day on the stated date counts. match_type: exact when that leaves one session and it is D0, "
    "window when it leaves D0-1 and D0, none when d0_matches = N.")
HAND_VERDICT_FIELDS = ["release_date_stated", "release_time_stated", "ir_source", "ir_url", "ir_release_et",
                       "possible_first_sessions", "is_quarterly_results", "d0_matches", "match_type", "notes", "checked_at"]
HAND_CHECK_COLUMNS = ["sample", "seed", "draw_order", "accession", "security_id", "cik", "fiscal_quarter_end",
                      "d0_session", "d0_basis", "ir_url", "ir_source", "ir_release_et", "release_date_stated",
                      "release_time_stated", "possible_first_sessions", "is_quarterly_results", "d0_matches",
                      "match_type", "checked_at", "notes"]


def hand_sample_population(events: pd.DataFrame, quarters: pd.DataFrame, weeks_column: str = "scope_weeks") -> pd.DataFrame:
    """The results releases whose D0 falls in a company-quarter of the population (``scope_quarters(quarters,
    weeks_column)``), one per company-quarter (the earliest accepted when a calendar quarter holds two),
    sorted by CIK and quarter."""
    q = scope_quarters(quarters, weeks_column)[["cik", "quarter"]].assign(cik=lambda f: f["cik"].astype(int))
    rel = events[(events["event_kind"] == "results_release") & (events["d0_session"] != "")].copy()
    rel["quarter"] = pd.PeriodIndex(pd.to_datetime(rel["d0_session"]), freq="Q").astype(str)
    rel["cik"] = rel["cik"].astype(int)
    rel = rel.merge(q, on=["cik", "quarter"]).sort_values(["cik", "quarter", "acceptance_et", "accession"])
    return rel.drop_duplicates(["cik", "quarter"]).reset_index(drop=True)


def round7_rescored(events: pd.DataFrame, keys: pd.DataFrame) -> pd.DataFrame:
    """Round 7's 20 company-quarters (``keys``: cik, fiscal_quarter_end, draw_order, seed, population,
    accession_drawn) with the current table's results release for each (its accession may differ:
    rule (b) moved Interface's), so they are scored again under the current rules."""
    rel = events[events["event_kind"] == "results_release"].assign(cik=lambda f: f["cik"].astype(int))
    rel = rel.drop_duplicates(["cik", "fiscal_quarter_end"]).set_index(["cik", "fiscal_quarter_end"])
    rows = []
    for key in keys.to_dict("records"):
        index = (int(key["cik"]), key["fiscal_quarter_end"])
        if index not in rel.index:
            log(f"  round 7 draw {key['draw_order']}: no results release now for {index}")
            continue
        row = rel.loc[index].to_dict()
        row.update(cik=index[0], fiscal_quarter_end=index[1], draw_order=int(key["draw_order"]), seed=int(key["seed"]),
                   population=int(key["population"]), accession_drawn_round7=key["accession_drawn"],
                   quarter=str(pd.Period(row["d0_session"], "Q")) if row["d0_session"] else "")
        rows.append(row)
    return pd.DataFrame(rows)


def draw_hand_sample(population: pd.DataFrame, seed: int = HAND_SAMPLE_SEED, n: int = HAND_SAMPLE_N) -> pd.DataFrame:
    """``n`` company-quarters drawn without replacement by numpy default_rng(seed), in draw order."""
    picks = np.random.default_rng(seed).choice(len(population), size=min(n, len(population)), replace=False)
    return population.iloc[picks].assign(draw_order=range(1, len(picks) + 1), seed=seed,
                                         population=len(population)).reset_index(drop=True)


def _strip(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", fragment))).strip()


def parse_filing_index(text: str) -> dict:
    """Facts from an EDGAR filing index page: the info fields (Filing Date, Accepted, Period of Report,
    Items) and the document table (seq, description, document, type, url)."""
    info = {_strip(k): _strip(v.replace("<br>", "; ").replace("<br />", "; "))
            for k, v in re.findall(r'<div class="infoHead">(.*?)</div>\s*<div class="info">(.*?)</div>', text, re.S)}
    docs = []
    table = text.split('summary="Document Format Files"', 1)
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table[1].split("</table>", 1)[0] if len(table) > 1 else "", re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        href = re.search(r'href="([^"]+)"', cells[2]) if len(cells) >= 4 else None
        if href is None:
            continue
        link = href.group(1).replace("/ix?doc=", "")
        docs.append({"seq": _strip(cells[0]), "description": _strip(cells[1]), "document": _strip(cells[2]).split(" ")[0],
                     "type": _strip(cells[3]), "url": link if link.startswith("http") else "https://www.sec.gov" + link})
    return {"info": info, "documents": docs}


def html_text(data: bytes) -> str:
    """Plain text of an HTML (or text) filing document, whitespace collapsed."""
    text = data.decode("utf-8", "replace")
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    return _strip(text)


_SESSION = re.compile(r"(after (?:the )?(?:u\.s\. )?(?:stock )?markets? (?:has )?clos\w*|after (?:the )?close of (?:the )?(?:stock )?"
                      r"market|after (?:the )?(?:market|bell)|before (?:the )?(?:u\.s\. )?(?:stock )?markets? open\w*|"
                      r"prior to (?:the )?(?:u\.s\. )?(?:stock )?markets? open\w*|before (?:the )?(?:opening|open) of "
                      r"(?:the )?(?:stock )?market|pre-market|premarket|after-market)", re.I)
_CALL_TIME = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?\s?m\.?|p\.?\s?m\.?)\s*\(?\s*(?:E[SD]?T|Eastern|P[SD]?T|Pacific|"
                        r"C[SD]?T|Central|M[SD]?T|Mountain|ET|PT|CT|MT)\b", re.I)
_RESULTS = (re.compile(r"\bannounc\w*\b.{0,200}?\b(?:results|earnings)\b", re.I),
            re.compile(r"\b(?:report|releas|post)\w*\b.{0,200}?\b(?:results|earnings)\b", re.I))


def _contexts(pattern: re.Pattern, text: str, limit: int = 3, width: int = 110) -> str:
    out = []
    for match in pattern.finditer(text):
        out.append(text[max(0, match.start() - width):match.end() + width].strip())
        if len(out) >= limit:
            break
    return " || ".join(out)


def release_evidence(text: str) -> dict:
    """What a press release says about itself: its opening (dateline and headline), the first sentence
    announcing results, and the passages naming a market session or a call time."""
    found = next((m for m in (pattern.search(text) for pattern in _RESULTS) if m), None)
    sentence = text[max(0, found.start() - 150):found.end() + 150].strip() if found else ""
    return {"release_opening": text[:500], "results_sentence": sentence,
            "session_phrases": _contexts(_SESSION, text), "call_times": _contexts(_CALL_TIME, text)}


def filing_documents(cik: int, accession: str, offline: bool = False) -> dict:
    """The filing index, the 8-K itself and its EX-99 exhibits for one filing, cache first
    (``CACHE/raw/sec/docs/{cik}/{accession}/``), through this step's SEC limiter."""
    folder = DOCS_DIR / str(int(cik)) / accession
    url = INDEX_URL.format(cik=int(cik), folder=accession.replace("-", ""), accession=accession)
    data = _sec_get(url, folder / f"{accession}-index.htm.gz", "sec_docs", f"CIK{int(cik)}", offline)
    if data is None:
        return {"index_url": url, "info": {}, "documents": [], "texts": {}}
    parsed = parse_filing_index(data.decode("utf-8", "replace"))
    texts = {}
    for doc in parsed["documents"]:
        if doc["type"] in EVENT_FORMS or doc["type"].upper().startswith("EX-99"):
            body = _sec_get(doc["url"], folder / f"{doc['document']}.gz", "sec_docs", f"CIK{int(cik)}", offline)
            if body is not None:
                texts[doc["document"]] = html_text(body)
    return {"index_url": url, **parsed, "texts": texts}


def hand_sample_rows(drawn: pd.DataFrame, offline: bool = False) -> pd.DataFrame:
    """The drawn events with their SEC URLs and what the 8-K and its exhibits say (for the checker to read)."""
    rows = []
    for record in drawn.to_dict("records"):
        cik, accession = int(record["cik"]), record["accession"]
        docs = filing_documents(cik, accession, offline)
        main = next((d for d in docs["documents"] if d["type"] in EVENT_FORMS), None)
        exhibits = [d for d in docs["documents"] if d["type"].upper().startswith("EX-99")]
        release = exhibits[0] if exhibits else None
        evidence = release_evidence(docs["texts"].get(release["document"], "")) if release else release_evidence("")
        main_text = docs["texts"].get(main["document"], "") if main else ""
        item = re.search(r"Item\s*2\.02[^.]{0,400}", main_text, re.I)
        rows.append({
            "sample": record.get("sample", ""), "draw_order": record["draw_order"], "seed": record["seed"],
            "population": record["population"], "cik": cik, "security_id": record["security_id"],
            "calendar_quarter": record["quarter"], "fiscal_quarter_end": record["fiscal_quarter_end"],
            "accession": accession, "accession_drawn_round7": record.get("accession_drawn_round7", ""),
            "form": record["form"], "items": record["items"], "event_kind": record["event_kind"],
            "event_kind_basis": record["event_kind_basis"], "release_check": record.get("release_check", ""),
            "n_item202_in_fiscal_quarter": record["n_item202_in_fiscal_quarter"],
            "report_date": record.get("report_date", ""), "acceptance_et": record["acceptance_et"],
            "tz_resolution": record["tz_resolution"], "acceptance_timing": record["acceptance_timing"],
            "d0_session": record["d0_session"], "d0_session_acceptance": record.get("d0_session_acceptance", ""),
            "d0_basis": record.get("d0_basis", ""), "late_furnished": record.get("late_furnished", ""),
            "release_date": record.get("release_date", ""), "item202_kind": record.get("item202_kind", ""),
            "item202_date": record.get("item202_date", ""),
            "index_url": docs["index_url"], "index_accepted": docs["info"].get("Accepted", ""),
            "index_filing_date": docs["info"].get("Filing Date", ""), "index_period": docs["info"].get("Period of Report", ""),
            "index_items": docs["info"].get("Items", ""),
            "document_url": main["url"] if main else "", "exhibit_url": release["url"] if release else "",
            "exhibit_type": release["type"] if release else "", "n_ex99": len(exhibits),
            "item_202_text": item.group(0) if item else "", **evidence})
    return pd.DataFrame(rows)


def hand_check_table(rows: pd.DataFrame, verdicts: dict) -> pd.DataFrame:
    """The drawn rows joined to the checker's verdicts (keyed by accession), in HAND_CHECK_COLUMNS order
    (validate reads accession, security_id, ir_url, ir_release_et, d0_matches, checked_at)."""
    table = pd.DataFrame([{"accession": acc, **{k: v.get(k, "") for k in HAND_VERDICT_FIELDS}}
                          for acc, v in verdicts.items()], columns=["accession"] + HAND_VERDICT_FIELDS)
    out = rows.merge(table, on="accession", how="left").fillna("")
    return out[HAND_CHECK_COLUMNS]


def run_hand_sample(offline: bool = False) -> pd.DataFrame:
    """Re-score round 7's 20 company-quarters and draw the fresh 20 (HAND_SAMPLE_SEED_FRESH, the plan's
    population: top-300 file weeks), fetch their documents, and join the checker's verdicts (HAND_VERDICTS,
    keyed by accession, judged by HAND_CRITERION): writes HAND_DRAW, and HAND_SAMPLE plus HAND_CHECKS
    (INPUTS/earnings_hand_checks.csv) once every drawn event has a verdict."""
    events = read_csv_text(EVENTS_OUT)
    quarters = read_csv_text(QUARTERS_OUT)
    for column in ("listed_weeks", "scope_weeks", "quarter_weeks", "top300_weeks", "rank250_weeks"):
        quarters[column] = quarters[column].astype(int)
    quarters["full_quarter"] = quarters["full_quarter"] == "True"
    old = round7_rescored(events, read_csv_text(HAND_ROUND7_KEYS)).assign(sample="round7_rescored")
    population = hand_sample_population(events, quarters, "top300_weeks")
    fresh = draw_hand_sample(population, HAND_SAMPLE_SEED_FRESH).assign(sample="fresh")
    log(f"hand sample: round 7's {len(old)} company-quarters re-scored; fresh {len(fresh)} of {len(population)} universe "
        f"company-quarters with a results release (numpy default_rng({HAND_SAMPLE_SEED_FRESH}))")
    rows = hand_sample_rows(pd.concat([old, fresh], ignore_index=True), offline)
    _write_csv(HAND_DRAW, rows)
    log(f"wrote {HAND_DRAW}")
    if HAND_VERDICTS.exists():
        out = hand_check_table(rows, json.loads(HAND_VERDICTS.read_text(encoding="utf-8")))
        if out["d0_matches"].ne("").all():
            _write_csv(HAND_SAMPLE, rows.merge(out[["accession"] + HAND_VERDICT_FIELDS], on="accession", how="left"))
            _write_csv(HAND_CHECKS, out)
            log(f"wrote {HAND_SAMPLE} and {HAND_CHECKS}: d0_matches by sample "
                f"{out.groupby('sample')['d0_matches'].value_counts().to_dict()}")
        else:
            log(f"verdicts missing for {int(out['d0_matches'].eq('').sum())} drawn events: {HAND_CHECKS} not written")
    return rows


# ------------------------------------------------------------------ main

def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    common.atomic_write(path, frame.to_csv(index=False).encode("utf-8"))


def label_summary(events: pd.DataFrame, fallback: pd.DataFrame) -> dict:
    both = pd.concat([f for f in (events, fallback) if len(f)] or [pd.DataFrame(columns=["json_label", "filing_date"])],
                     ignore_index=True)
    if both.empty:
        return {}
    table = pd.crosstab(both["filing_date"].str[:4], both["json_label"])
    return {"overall": dict(Counter(both["json_label"])),
            "by_year": {year: {k: int(v) for k, v in row.items()} for year, row in table.iterrows()}}


def _histogram(values: pd.Series, edges: list[int]) -> dict[str, int]:
    labels = [f"{lo}-{hi - 1}" for lo, hi in zip(edges, edges[1:])] + [f"{edges[-1]}+"]
    bins = pd.cut(values, edges + [10 ** 6], right=False, labels=labels)
    return {str(k): int(v) for k, v in bins.value_counts().reindex(labels).fillna(0).items()}


def event_kind_summary(events: pd.DataFrame) -> dict:
    """event_kind counts, how the rule decided, and how the results release compares with the
    company's usual lag (median days after the period end over its single-event quarters)."""
    if events.empty:
        return {}
    assigned = events[events["fiscal_quarter_end"] != ""]
    sizes = assigned[assigned["event_kind"] != "amendment"].groupby(["cik", "fiscal_quarter_end"]).size()
    releases = events[events["event_kind"] == "results_release"]
    order = assigned.sort_values(["cik", "fiscal_quarter_end", "acceptance_sort", "accession"])
    first = order[order["event_kind"] != "amendment"].drop_duplicates(["cik", "fiscal_quarter_end"])
    later = first[first["n_item202_in_fiscal_quarter"].astype(int) > 1]
    lag = pd.to_numeric(events["days_after_period_end"], errors="coerce")
    annual = events["periodic_report_form"].str.startswith("10-K")
    single = events[(events["event_kind_basis"] == "single")]
    usual = lag[single.index].groupby([single["cik"], annual[single.index]]).median()
    multi = releases[releases["event_kind_basis"] != "single"]
    deviation = {}
    for name, frame in (("results_release", multi), ("first_event", first.loc[first.index.isin(
            events.index[events["event_kind_basis"] != "single"])])):
        typical = pd.Series([usual.get((c, a)) for c, a in zip(frame["cik"], annual[frame.index])], index=frame.index,
                            dtype=float)
        gap = (lag[frame.index] - typical).abs().dropna()
        deviation[name] = {"quarters": int(len(gap)), "within_7_days": round(float((gap <= 7).mean()), 3),
                           "within_14_days": round(float((gap <= 14).mean()), 3),
                           "median_days": float(gap.median()) if len(gap) else None}
    next_days = pd.to_numeric(later["days_to_next_item202_in_quarter"], errors="coerce")
    return {"by_kind": dict(Counter(events["event_kind"])), "by_basis": dict(Counter(events["event_kind_basis"])),
            "company_quarters": int(len(sizes)), "quarters_with_2plus_events": int((sizes >= 2).sum()),
            "quarters_with_3plus_events": int((sizes >= 3).sum()),
            "ciks_with_a_3plus_quarter": int(sizes[sizes >= 3].index.get_level_values(0).nunique()),
            "results_release_not_first_event": int(len(multi) - multi.index.isin(first.index).sum()),
            "first_events_with_a_later_event": int(len(later)),
            "first_events_with_a_later_event_1_to_45_days": int(next_days.between(1, 45).sum()),
            "quarters_without_results_release": int(len(set(sizes.index) - set(zip(releases["cik"], releases["fiscal_quarter_end"])))),
            "lag_vs_company_usual_multi_event_quarters": {
                "definition": "abs(days after period end - the company's median over its single-event quarters, "
                              "10-K quarters apart from 10-Q quarters), for quarters with 2+ events",
                **deviation}}


def release_rule_summary(events: pd.DataFrame, calendar: XnasCloses) -> dict:
    """Counts for rules (a) and (b): rows read, what their Item 2.02 text furnishes, late-furnished rows and
    how far D0 moved, quarters whose release moved or whose pick furnishes no release."""
    if events.empty or "evidence_reason" not in events:
        return {}
    read = events[events["evidence_reason"] != ""]
    lag = pd.to_numeric(events["report_date_lag_sessions"], errors="coerce")
    candidates = events[(lag >= LATE_MIN_SESSIONS) & (events["event_kind"] != "amendment")]
    late = events[events["late_furnished"] == "Y"]
    shift = [calendar.index_on_or_after(a) - calendar.index_on_or_after(b)
             for a, b in zip(late["d0_session_acceptance"], late["d0_session"])]
    moved = events[events["release_check"].str.startswith("moved_from")]
    releases = events[events["event_kind"] == "results_release"]
    return {
        "rows_read": int(len(read)), "by_reason": dict(Counter(read["evidence_reason"])),
        "item202_kind": dict(Counter(read["item202_kind"])),
        "documents_missing": int(read["item202_kind"].isin(["no_document"]).sum()),
        "late_rule": {"definition": f"non-amendment rows whose period of report is >= {LATE_MIN_SESSIONS} sessions before "
                                    "the acceptance D0", "candidates": int(len(candidates)),
                      "candidates_by_report_date_kind": dict(Counter(candidates["report_date_kind"])),
                      "release_date_basis": dict(Counter(candidates["release_date_basis"])),
                      "late_furnished": int(len(late)), "late_furnished_by_kind": dict(Counter(late["event_kind"])),
                      "late_furnished_by_basis": dict(Counter(late["release_date_basis"])),
                      "d0_moved_back_sessions": {str(k): int(v) for k, v in sorted(Counter(shift).items())},
                      "results_release_rows": int(len(releases)),
                      "results_release_late_furnished": int((releases["late_furnished"] == "Y").sum())},
        "event_kind_rule": {"picks_read": int((read["evidence_reason"].str.contains("release_not_first")).sum()),
                            "quarters_moved": int(moved.groupby(["cik", "fiscal_quarter_end"]).ngroups) if len(moved) else 0,
                            "moved_from_kind": dict(Counter(events.loc[events["accession"].isin(
                                moved["release_check"].str.split(":").str[1]), "item202_kind"])),
                            "picks_furnishing_no_release_kept": int((events["release_check"] == "pick_furnishes_no_release").sum()),
                            "results_release_not_first_event": int((releases["pick_not_first"] == "Y").sum())},
        "first_in_fiscal_quarter": dict(Counter(events["first_in_fiscal_quarter"])),
    }


def amendment_summary(events: pd.DataFrame) -> dict:
    amend = events[events["form"] == "8-K/A"] if len(events) else events
    if amend.empty:
        return {"rows": 0}
    linked = amend[amend["amends_accession"] != ""]
    target = events.set_index("accession")["d0_session"]
    target = target[~target.index.duplicated()]
    gap = (pd.to_datetime(linked["d0_session"].replace("", None))
           - pd.to_datetime(linked["amends_accession"].map(target).replace("", None))).dt.days.dropna()
    return {"rows": int(len(amend)), "by_amends_how": dict(Counter(amend["amends_how"])),
            "event_kind": dict(Counter(amend["event_kind"])),
            "linked_to_an_event_row": int(linked["amends_accession"].isin(events["accession"]).sum()),
            "d0_days_after_amended_event": {"median": float(gap.median()) if len(gap) else None,
                                            "p75": float(gap.quantile(0.75)) if len(gap) else None,
                                            "max": float(gap.max()) if len(gap) else None}}


def fallback_summary(fallback: pd.DataFrame) -> dict:
    if fallback.empty:
        return {"rows": 0}
    days = pd.to_numeric(fallback["days_after_period_end"])
    shared = fallback["catch_up_reason"].str.contains("shared_d0")
    return {"rows": int(len(fallback)), "ciks": int(fallback["cik"].nunique()),
            "with_other_8k_between": int((fallback["other_8k_between"] != "").sum()),
            "with_item202_between": int((fallback["item202_between"] != "").sum()),
            "ciks_with_item202_between": int(fallback.loc[fallback["item202_between"] != "", "cik"].nunique()),
            "catch_up_filing": int((fallback["catch_up_filing"] == "Y").sum()),
            "catch_up_reason": dict(Counter(fallback.loc[fallback["catch_up_filing"] == "Y", "catch_up_reason"])),
            "catch_up_limits_days_after_period_end": CATCH_UP_DAYS,
            "shared_d0_rows": int(shared.sum()), "shared_d0_ciks": int(fallback.loc[shared, "cik"].nunique()),
            "usable_as_announcement": dict(Counter(fallback["usable_as_announcement"])),
            "days_after_period_end": {"over_100": int((days > 100).sum()), "over_180": int((days > 180).sum()),
                                      "max": int(days.max()),
                                      "10-Q": _histogram(days[fallback["form"].str.startswith("10-Q")],
                                                         [0, 41, 46, 51, 54, 61, 92, 181]),
                                      "10-K": _histogram(days[fallback["form"].str.startswith("10-K")],
                                                         [0, 61, 76, 91, 106, 109, 181])},
            "by_form": dict(Counter(fallback["form"])),
            "by_filing_year": dict(sorted(Counter(fallback["filing_date"].str[:4]).items()))}


def json_label_detail(events: pd.DataFrame, fallback: pd.DataFrame) -> dict:
    """Whether the JSON label follows the company, the filer (self or agent) or the year."""
    both = pd.concat([f for f in (events, fallback) if len(f)] or [pd.DataFrame(columns=["json_label"])],
                     ignore_index=True)
    both = both[both["json_label"].isin(["utc", "et_labelled_z"])]
    if both.empty:
        return {}
    per_cik = both.groupby("cik")["json_label"].nunique()
    own = both["accession"].str[:10].astype(int) == both["cik"].astype(int)
    eastern = both["json_label"] == "et_labelled_z"
    table = eastern.groupby([both["filing_date"].str[:4], own.map({True: "self_filed", False: "agent_filed"})]).mean()
    key = both["accession"].str[:10] + "|" + both["filing_date"].str[:4]
    counts = both.groupby([key, "json_label"]).size().unstack(fill_value=0)
    return {"ciks_one_label": int((per_cik == 1).sum()), "ciks_both_labels": int((per_cik > 1).sum()),
            "agent_year_key_majority_share": round(float(counts.max(axis=1).sum() / counts.values.sum()), 4),
            "eastern_labelled_share_by_year": {year: {k: round(float(v), 3) for k, v in row.items()}
                                               for year, row in table.unstack().iterrows()}}


def data_notes(events: pd.DataFrame, fallback: pd.DataFrame, scope: pd.DataFrame, missing: pd.DataFrame) -> dict:
    """Points a downstream reader needs: multi-class security_id lists, accessions under two CIKs,
    and top-300 CIKs with no SEC earnings rows at all (bank-regulator filers)."""
    both = pd.concat([f for f in (events, fallback) if len(f)] or [pd.DataFrame(columns=["cik", "accession", "security_id"])],
                     ignore_index=True)
    shared = both.groupby("accession")["cik"].nunique()
    none = missing[(missing["n_fallback"] == 0) & (missing["in_top300"] == "Y")] if len(missing) else missing
    return {"rows_with_several_security_ids": {"events": int(events["security_id"].str.contains(" ").sum()) if len(events) else 0,
                                               "fallback": int(fallback["security_id"].str.contains(" ").sum()) if len(fallback) else 0,
                                               "note": "space-joined; split before joining on security_id"},
            "accessions_under_several_ciks": {acc: sorted(both.loc[both["accession"] == acc, "cik"].astype(int).unique().tolist())
                                              for acc in shared[shared > 1].index},
            "top300_ciks_without_any_sec_earnings_row": {str(r["cik"]): f"{r['name']} ({r['top300_weeks']} top-300 weeks)"
                                                         for r in none.to_dict("records")} if len(none) else {}}


def input_facts() -> dict:
    """sha256 and modification time of each upstream file this step reads."""
    return {str(path): {"sha256": common.sha256_file(path),
                        "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")}
            for path in (WEEKLY, UNIVERSE_TOP, CANDIDATES, MASTER, PERIODIC_HISTORY) if path.exists()}


def read_tables(folder: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(events, fallback) as written by an earlier build in ``folder`` (for a before/after comparison)."""
    return (read_csv_text(folder / EVENTS_OUT.name), read_csv_text(folder / FALLBACK_OUT.name))


def missing_quarters(quarters: pd.DataFrame, scope: pd.DataFrame) -> pd.DataFrame:
    """In-scope company-quarters (COVERAGE_DEFINITION) without a usable date, with what they do have."""
    q = scope_quarters(quarters)
    q = q[~q["usable_date"]]
    names = scope.set_index(scope["cik"].astype(int))[["name", "security_ids", "in_top300", "in_candidates"]]
    return q.join(names, on="cik")[["cik", "name", "security_ids", "in_top300", "in_candidates", "quarter",
                                    "listed_weeks", "scope_weeks", "n_item202", "n_fallback", "n_fallback_usable"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="never request; build from the cache only")
    parser.add_argument("--limit-ciks", type=int, default=0, help="a trial on the first N CIKs (writes CACHE/earnings/trial)")
    parser.add_argument("--plan-only", action="store_true", help="stop after counting the headers needed")
    parser.add_argument("--hand-sample", action="store_true",
                        help="draw the hand sample from the built tables, fetch its 8-Ks and exhibits, and write "
                             "hand_sample_draw.csv (and hand_sample.csv when the verdicts are recorded)")
    parser.add_argument("--fetch-only", action="store_true",
                        help="fetch the missing headers and scan the cache, then stop without writing any table")
    parser.add_argument("--fetch-evidence", action="store_true",
                        help="fetch the 8-K documents whose Item 2.02 text rules (a) and (b) read (evidence_rows, "
                             "stages 1 and 2), then stop without writing any table")
    parser.add_argument("--sec-rate", type=float, default=SEC_RATE_MAX,
                        help=f"SEC requests a second for this run (at most {SEC_RATE_MAX})")
    parser.add_argument("--sic-out", type=Path, default=SIC_OUT, help="where sic_history.csv is written")
    parser.add_argument("--before", type=Path, default=None,
                        help="a folder holding an earlier build's earnings_events.csv and earnings_fallback_periodic.csv: "
                             "its coverage of this build's company-quarters goes in the summary")
    args = parser.parse_args(argv)
    set_sec_rate(args.sec_rate)
    if args.hand_sample:
        run_hand_sample(args.offline)
        return 0
    started = time.time()
    log(f"scope: weekly top-300 file and candidate CIKs (SEC rate {args.sec_rate}/s)")
    inputs = input_facts()
    weekly = pd.read_pickle(WEEKLY)[["security_id", "cik", "week_end", "universe"]]
    top = load_universe_top()
    candidates = read_csv_text(CANDIDATES)
    master = read_csv_text(MASTER)
    history = load_history()
    if input_facts() != inputs:
        log("WARNING: an input changed while it was read (an upstream step is running): rerun when it is done")
        inputs["changed_while_read"] = True
    if history is None:
        log(f"WARNING: {PERIODIC_HISTORY} not found: MIXED CIKs count as domestic in every week")
    else:
        log(f"foreign regime: {PERIODIC_HISTORY} ({len(history)} rows, {history['cik'].nunique()} MIXED CIKs)")
    flags = cik_flags(master)
    scope_all = build_scope(top, weekly, candidates, master, history)
    scope = scope_all[scope_all["excluded_foreign"] == "N"].reset_index(drop=True)
    if args.limit_ciks:
        scope = scope.head(args.limit_ciks)
    out_dir = OUT / "trial" if args.limit_ciks else OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "scope.csv", scope)
    ciks = scope["cik"].astype(int).tolist()
    log(f"scope: {len(ciks)} CIKs (top300 {int((scope['in_top300'] == 'Y').sum())}, "
        f"candidates {int((scope['in_candidates'] == 'Y').sum())}, flags {dict(Counter(scope['foreign_filer']))}; "
        f"top-300 weeks dropped as foreign {int(scope['top300_weeks_foreign'].sum())})")
    windows = {int(c): (a, b) for c, a, b in zip(scope["cik"], scope["window_start"], scope["window_end"])}
    plans, failures = plan_all(ciks, args.offline, windows)
    jobs = header_jobs(plans)
    kinds = Counter(kind for *_, kind in jobs)
    pages = (sum(p["pages_needed"] for p in plans.values()), sum(p["pages_read"] for p in plans.values()))
    log(f"plan: {len(plans)} CIKs, older pages needed {pages[0]} read {pages[1]}; headers by kind {dict(kinds)}")
    if args.plan_only:
        return 0
    fetch_counts = fetch_headers(jobs, args.offline)
    if _STOP.is_set():
        log("stopped on an SEC refusal; tables are built from what is cached")
    log("scan: every needed header in the cache")
    scan = header_cache_scan(jobs)
    log(f"  header cache: {scan['by_status']} complete={scan['complete']}")
    if args.fetch_only:
        common.atomic_write(out_dir / "fetch_scan.json", (json.dumps(
            {"generated": datetime.now().isoformat(timespec="seconds"), "sec_rate_per_second": args.sec_rate,
             "fetch": dict(fetch_counts), "cache_scan": scan, "stopped_on_refusal": _STOP.is_set()},
            indent=1) + "\n").encode())
        return 0 if scan["complete"] else 1
    if args.fetch_evidence:
        calendar = XnasCloses()
        log("evidence: build the tables without it, then fetch the 8-K documents of stage 1 and stage 2")
        events, _ = build_tables(plans, scope, calendar, history, evidence=False)
        facts = {}
        for stage in (1, 2):
            rows = evidence_rows(events, stage)
            log(f"evidence stage {stage}: {len(rows)} rows {dict(Counter(rows['evidence_reason']))}")
            facts[f"stage{stage}"] = {"rows": int(len(rows)), "by_reason": dict(Counter(rows["evidence_reason"])),
                                      "fetch": dict(fetch_docs(rows, args.offline))}
            events = attach_evidence(events, rows)
            facts[f"stage{stage}"]["item202_kind"] = dict(Counter(events.loc[rows.index, "item202_kind"]))
            if _STOP.is_set():
                break
        common.atomic_write(out_dir / "evidence_fetch.json", (json.dumps(
            {"generated": datetime.now().isoformat(timespec="seconds"), "sec_rate_per_second": args.sec_rate,
             **facts, "stopped_on_refusal": _STOP.is_set()}, indent=1) + "\n").encode())
        return 0
    log("build: parse headers, D0 sessions, event kinds")
    calendar = XnasCloses()
    events, fallback = build_tables(plans, scope, calendar, history)
    sic_all = sic_history(events, fallback)
    sic = compact_sic_history(sic_all)
    event_out = events.reindex(columns=EVENT_COLUMNS)
    fallback_out = fallback.reindex(columns=FALLBACK_COLUMNS)
    if args.limit_ciks:
        targets = (out_dir / "earnings_events.csv", out_dir / "earnings_fallback_periodic.csv", out_dir / "sic_history.csv")
    else:
        targets = (EVENTS_OUT, FALLBACK_OUT, args.sic_out)
    for path, frame in zip(targets, (event_out, fallback_out, sic)):
        _write_csv(path, frame)
        log(f"wrote {path} ({len(frame)} rows)")
    evidence = events.loc[events["evidence_reason"] != "", EVIDENCE_COLUMNS] if len(events) else pd.DataFrame(columns=EVIDENCE_COLUMNS)
    _write_csv(out_dir / "release_evidence.csv", evidence)
    log(f"wrote {out_dir / 'release_evidence.csv'} ({len(evidence)} rows: the Item 2.02 text read for rules (a) and (b))")
    log("coverage (domestic weeks only)")
    weekly_domestic = domestic_weeks(weekly, flags, history)
    weeks = scope_weeks(top, weekly, candidate_windows(candidates), flags, history)
    ranked = _int_cik(load_universe_top(ranks=True))
    ranked = ranked[~regime_foreign(flags, history, ranked["cik"], ranked["week_end"])]
    rank250 = ranked[(ranked["rank"] <= 250) & (ranked["week_end"] >= RANK250_WEEKS[0]) & (ranked["week_end"] <= RANK250_WEEKS[1])]
    present = presence(weeks, weekly_domestic, set(ciks), extra={"top300_weeks": ranked, "rank250_weeks": rank250})
    quarters = quarter_coverage(present, events, fallback)
    years = company_year_table(quarters)
    _write_csv(out_dir / "company_year_coverage.csv", years)
    _write_csv(out_dir / "missing_company_quarters.csv", missing_quarters(quarters, scope))
    _write_csv(out_dir / QUARTERS_OUT.name, quarters)
    before = None
    if args.before is not None:
        old_events, old_fallback = read_tables(args.before)
        before = coverage_summary(quarter_coverage(present, old_events, old_fallback))
        before["tables"] = {str(args.before / EVENTS_OUT.name): len(old_events),
                            str(args.before / FALLBACK_OUT.name): len(old_fallback)}
        _write_csv(out_dir / "missing_company_quarters_before.csv",
                   missing_quarters(quarter_coverage(present, old_events, old_fallback), scope))
    missing = no_event_companies(scope, plans, events, fallback)
    _write_csv(out_dir / "no_event_companies.csv", missing)
    foreign_check = foreign_scope_check(scope, weekly, flags, history, events, fallback, top)
    _write_csv(out_dir / "foreign_scope_check.csv", foreign_check)
    both = pd.concat([f for f in (events, fallback) if len(f)] or [pd.DataFrame(columns=["filing_date", "acceptance_et"])],
                     ignore_index=True)
    both = both[both["acceptance_et"] != ""]
    lag = (pd.to_datetime(both["filing_date"]) - pd.to_datetime(both["acceptance_et"].str[:10])).dt.days
    headers_missing = int(((events.get("acceptance_header_et", pd.Series(dtype=str)) == "").sum() if len(events) else 0)
                          + ((fallback.get("acceptance_header_et", pd.Series(dtype=str)) == "").sum() if len(fallback) else 0))
    dropped = foreign_only_top300(top, scope_all) if not args.limit_ciks else []
    top_domestic = _int_cik(top)
    top_domestic = top_domestic[~regime_foreign(flags, history, top_domestic["cik"], top_domestic["week_end"])]
    planned = {"events": sum(len(p["events"]) for p in plans.values()),
               "events_in_window": sum(int(p["events"]["in_window"].sum()) for p in plans.values()),
               "fallback": sum(len(p["fallback"]) for p in plans.values()),
               "fallback_in_window": sum(int(p["fallback"]["in_window"].sum()) for p in plans.values())}
    summary = {
        "generated": datetime.now().isoformat(timespec="seconds"), "runtime_s": round(time.time() - started, 1),
        # the inputs as read (upstream steps rerun on their own; compare to see whether this build is stale)
        "inputs": inputs,
        "scope": {"ciks": len(ciks), "by_foreign_filer": dict(Counter(scope["foreign_filer"])),
                  "in_top300": int((scope["in_top300"] == "Y").sum()),
                  "in_candidates": int((scope["in_candidates"] == "Y").sum()),
                  "foreign_regime_source": str(PERIODIC_HISTORY) if history is not None else "missing: MIXED as domestic",
                  "periodic_form_history_rows": int(len(history)) if history is not None else 0,
                  "mixed_ciks_in_scope": int(scope["foreign_filer"].str.contains("MIXED").sum()),
                  "mixed_ciks_missing_from_history": [int(c) for c in scope.loc[scope["foreign_filer"].str.contains("MIXED"), "cik"]
                                                      if history is None or int(c) not in set(history["cik"].astype(int))],
                  "top300_security_weeks_dropped_as_foreign": int(scope["top300_weeks_foreign"].sum()),
                  "ciks_out_of_scope_because_every_top300_week_is_foreign": dropped,
                  "excluded_flag_y": int((scope_all["excluded_foreign"] == "Y").sum()),
                  "definition": ("a CIK is in scope when one of its securities is in the weekly top-300 file "
                                 f"({UNIVERSE_TOP}) in a domestic week or a candidate row ({CANDIDATES}) names it; its "
                                 f"event window runs from its first possible day less {WARMUP_DAYS} days (not before "
                                 f"{EVENTS_FROM}) to its last possible day plus {HOLD_DAYS} days, and at least to "
                                 "the end of that day's calendar quarter"),
                  "warmup_days": WARMUP_DAYS, "hold_days": HOLD_DAYS,
                  "window_start_by_year": dict(sorted(Counter(scope["window_start"].str[:4]).items())),
                  "scope_weeks": int(len(weeks)), "top300_file_weeks_domestic": int(len(top_domestic.drop_duplicates(["cik", "week_end"])))},
        "sec_rate_per_second": args.sec_rate,
        "planned_filings": {**planned, "note": "every filing from EVENTS_FROM is planned and classified; only those "
                                               "inside the CIK's window get a header and a row"},
        "submissions": {"planned_ciks": len(plans), "older_pages_needed": pages[0], "older_pages_read": pages[1],
                        "ciks_missing": dict(Counter(p["missing"] for p in plans.values() if p["missing"])),
                        "ciks_failed": {str(k): v for k, v in failures.items()}},
        "headers": {"needed": len(jobs), "by_kind": dict(kinds), "fetch": dict(fetch_counts),
                    "cache_scan": scan, "rows_without_header": headers_missing, "stopped_on_refusal": _STOP.is_set(),
                    "request_log_note": "raw_index.csv.gz was rebuilt from its readable gzip members on 2026-10-02 "
                                        "(about 66 log lines lost, among them 27 sec_headers lines; the cached raw "
                                        "files are intact), so completeness comes from cache_scan, not the log"},
        "events": {"rows": int(len(events)), "ciks": int(events["cik"].nunique()) if len(events) else 0,
                   "by_form": dict(Counter(events["form"])) if len(events) else {},
                   "event_kind": event_kind_summary(events),
                   "amendments": amendment_summary(events),
                   "fiscal_quarter_how": dict(Counter(events["fiscal_quarter_how"])) if len(events) else {},
                   "acceptance_timing": dict(Counter(events["acceptance_timing"])) if len(events) else {},
                   "tz_resolution": dict(Counter(events["tz_resolution"])) if len(events) else {},
                   "sic_match": dict(Counter(events["sic_match"])) if len(events) else {},
                   "foreign_regime_on_d0": dict(Counter(events["foreign_regime_on_d0"])) if len(events) else {},
                   "by_filing_year": dict(sorted(Counter(events["filing_date"].str[:4]).items())) if len(events) else {}},
        "fallback": {**fallback_summary(fallback),
                     "foreign_regime_on_d0": dict(Counter(fallback["foreign_regime_on_d0"])) if len(fallback) else {}},
        "json_vs_header": {**label_summary(events, fallback), "label_follows": json_label_detail(events, fallback)},
        "sic_history": {"headers_with_sic": int(len(sic_all)), "rows": int(len(sic)),
                        "ciks": int(sic["cik"].nunique()) if len(sic) else 0,
                        "header_form_differs_from_json_form": int(sum(
                            (f["header_form"] != f["form"]).sum() for f in (events, fallback) if len(f))),
                        "changes": int((sic["sic_changed"] == "Y").sum()) if len(sic) else 0,
                        "blank_check_6770_rows": int((sic["blank_check_6770"] == "Y").sum()) if len(sic) else 0,
                        "ciks_ever_6770": int(sic.loc[sic["blank_check_6770"] == "Y", "cik"].nunique()) if len(sic) else 0,
                        "written_to": str(args.sic_out),
                        "top300_name_weeks": sic_week_coverage(top_domestic, sic, ff49_lookup(), set(ciks))},
        "coverage_company_quarters": coverage_summary(quarters),
        # the plan's population: domestic company-quarters in the universe (weeks in the top-300 file), and
        # validate's members (rank <= 250 in the window weeks); the default above is every in-scope week
        "coverage_company_quarters_by_population": {name: {k: v for k, v in coverage_summary(quarters, column).items()
                                                           if k != "definition"} for name, column in POPULATIONS.items()},
        "gaps_by_population": {name: {"usable_fallback_only": gap_summary_population(events, fallback, quarters, column),
                                      "any_fallback": gap_summary_population(events, fallback, quarters, column, False)}
                               for name, column in POPULATIONS.items()},
        "release_rules": release_rule_summary(events, calendar),
        "coverage_company_quarters_before": before,
        "coverage_company_years": company_year_summary(years),
        "gaps_between_quarterly_events": gap_summary(events, fallback, scope),
        "gaps_between_quarterly_events_usable_fallback_only": gap_summary(events, fallback, scope, usable_only=True),
        "filing_date_minus_acceptance_date_days": {str(k): int(v) for k, v in lag.value_counts().sort_index().items()},
        "foreign_scope_check": {"mixed_ciks": int(len(foreign_check)),
                                "top300_weeks_foreign": int(foreign_check["top300_weeks_foreign"].sum()) if len(foreign_check) else 0,
                                "listed_weeks_foreign": int(foreign_check["listed_weeks_foreign"].sum()) if len(foreign_check) else 0,
                                "events_d0_foreign": int(foreign_check["events_d0_foreign"].sum()) if len(foreign_check) else 0,
                                "fallback_d0_foreign": int(foreign_check["fallback_d0_foreign"].sum()) if len(foreign_check) else 0},
        "no_event_companies": {"no_item202": int(len(missing)),
                               "no_item202_no_fallback": int((missing["n_fallback"] == 0).sum()) if len(missing) else 0},
        "data_notes": data_notes(events, fallback, scope, missing),
    }
    common.atomic_write(out_dir / "earnings_summary.json", (json.dumps(summary, indent=1, default=str) + "\n").encode())
    log(f"events {len(events)} ({dict(Counter(events['event_kind'])) if len(events) else {}}), fallback {len(fallback)} "
        f"(usable {int((fallback['usable_as_announcement'] == 'Y').sum()) if len(fallback) else 0}), sic rows {len(sic)}; "
        f"company-quarters with an event {summary['coverage_company_quarters'].get('share_any')}, "
        f"with a usable date {summary['coverage_company_quarters'].get('share_usable_date')}; "
        f"no Item 2.02: {len(missing)} CIKs; done in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
