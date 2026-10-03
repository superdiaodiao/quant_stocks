"""Plan step 12 (docs/reversal_2012_2026_data_plan.md, sections 1, 3.1, 3.3 and 6): the weekly listed set,
the weekly dollar-volume ranks, and the completeness checks of the top 250.

Data only. Nothing here computes signals, single-stock, strategy or portfolio returns, return ranks or
spreads. The canonical panel is read for raw close and raw volume only (its ``tr`` column is never read);
the only ranks are dollar-volume ranks (median raw close x raw volume) and market-cap proxy comparisons.

Week end (plan 3.1): the last XNAS session of each calendar week, 2012-01-06 to 2026-07-17 (759 weeks).

Listed set on week end t (plan 3.1), from ``ticker_intervals.csv`` (Nasdaq snapshot runs; a run breaks
only after two full-list snapshots without the symbol, so the one-missing-snapshot tolerance is in it):
- an interval counts from its first snapshot (``start``) to the day before the next full-list snapshot
  without it (``end_next_absent``), or to 2026-08-31 when no later snapshot misses it;
- IPO rule: when the security's first canonical price is after the snapshot that did not show it
  (``start_prev_absent``) and before the first one that does, the interval starts at that price; not when
  that price is on the first session after ``start_prev_absent``, where step 9 starts every ticker's rows
  (a cut series: a transfer from NYSE, or another company's rows under a reused ticker; ``ipo_boundary``);
  where the security's own stored file under that ticker starts later, between the two snapshots and more
  than 5 sessions after the canonical first row, the canonical rows before it are another company's (TRUE:
  WIKI rows from 2014-03-26, TrueCar's IPO 2014-05-16): they are dropped before any median is taken, and
  the IPO rule then starts the listing at the stored file's first day (``boundary_trims``); the other
  boundary intervals keep their rows (AMD, CSX: their own NYSE history), and their name-weeks whose
  50-session window reaches before the interval start are flagged (``dv_window_before_listing``);
- a Nasdaq Form 25 (``security_master.delist_date``, the effective date) or a transfer away from Nasdaq
  (``transfer_date``) removes it from that date on (listed while t is before it); an interval that starts
  on or after such a date is a later listing and is kept (counted as ``after_cut``);
- one row per (security, week); where two intervals of a security cover a week (a rename), the one that
  started last gives the ticker;
- an interval resting on one snapshot row matched by ticker alone, of a security whose SEC current
  exchanges are all other exchanges, is left out and reported (``doubtful_intervals``: Ford, NYSE, on one
  2019-06-17 symbol-directory file); the security master owns the fix.

Universe base (owner, the CRSP common-stock convention of share codes 10/11): Nasdaq common stock
(``pf.non_common_interval`` on the listed name), not an unmerged SPAC shell (``pf.spac_shells``, plus the
shells listed now that step 6's ``pf.spac_like_now`` finds: Dynamix, Iron Horse II, Churchill XI, Research
Alliance III), not a foreign filer that week (Y always; MIXED in the weeks whose regime in force is
foreign, from ``periodic_form_history.csv`` via ``pf.foreign_spans``; N and UNKNOWN never, UNKNOWN
counted), and not an investment company that week (``investment_company``: closed-end funds and business
development companies, from the first week the issuer is one). The investment-company evidence is SEC
filings only, read from the cached submissions (``CACHE/raw/sec/submissions``, the recent block and every
cached older page) and ``sic_history.csv``: a BDC election (N-54A) counts until it is withdrawn (N-54C);
otherwise a run of at least two filings only an investment company makes (N-2, N-8A, N-6F, N-CSR/N-CSRS,
N-Q, N-CEN, N-PORT, N-30D, N-30B-2, N-23C, N-14 8C, POS 8C, 40-17G, 40-17F, N-PX before 2024-07-01 when
every 13F manager began to file it, or a header with SIC 6726), no more than 550 days apart, counts from
its first to its last filing, the last run carried forward while the issuer still files them (no N-54C or
N-8F since). A closed span whose end (or an N-54C / N-8F up to 550 days after it) lies within 30 days of
a delisting of the listing it ends inside (the last listed day, the Form 25 effective date, or the last
price in ``terminal_returns_2012_2026.csv``) runs to that listing's end (``merger_tail``: a BDC that
withdraws its election on the day its merger closes, such as American Capital on 2017-01-03, stays out
of the base through the weeks to its delisting). Every class of an issuer goes with it. The sha256 of
every submissions file read (or ``missing`` for one looked for and not cached) is written to
``sec_submissions_read.csv.gz``, and their digest is in ``inputs_sha256``, so a later fetch of a page
shows the build as stale. A bank such as OZK (N-PX from 2025 only) stays; so
does an operating company whose election was withdrawn before it listed (Red Cat). ``--fetch-sec-submissions``
fetches the cached submissions a listed CIK lacks (2 requests a second); the build itself reads local files.
Every share class is kept (GOOG and GOOGL both, owner decision: the protocol picks later).

Canonical metrics (``CACHE/prices/daily_panel.csv.gz``, step 9), on the XNAS session grid 2011-06-01 to
2026-08-31: dv = close_raw x volume_raw; dv20 / dv50 = rolling medians over the last 20 / 50 sessions
(at least 10 / 25 rows, as in step 6), restarted at a relist junction (step 9's ``relist_junction``
flag on the new shares' first row after a bankruptcy relisting: CHRD 2020-11-20, WW 2025-06-27, OPI
2026-06-18), so the old shares' dollar volume never enters the new shares' medians (only the ``flags``
column is read for this, never ``tr``), and run across a 1:1 successor link (owner convention of
2026-10-02, CRSP's single PERMNO; ``successor_links``): a successor link (a ``security_master`` link, or
for a predecessor the master links to nothing step 11's own 1:1 booking with the successor as acquirer:
Express Scripts Holding 2012-04, Marvell 2021-04) whose successor is listed within 10 days of the successor
date, of its own first canonical row or of the predecessor's last listed day, whose predecessor is not
listed on for more than 30 days after it (new IAC 2020: old IAC went on as MTCH, so new IAC is a new
security), and whose end step 11 books as a stock_merger / reorganization of one share per share
(Alphabet 2015-10, APA 2021, Broadcom 2016 and 2018), gives the successor's windows the predecessor's rows
on the sessions before the successor's first listed day (one row per session: no day counted twice), so
GOOGL ranks 6th from 2015-10-09 instead of being young for five weeks; the week's close is the last
canonical close at most 5 sessions before the week end (step 6's staleness rule). ``price_ge_10`` is that
raw close >= $10.
- ``dv50_rank`` / ``dv20_rank``: descending rank within the week among universe-base names with a close
  >= $10 and the median (ties by security_id). ``*_any_price`` ranks skip the $10 test.
- ``close_in_week``: a canonical row after the previous week end; ``close_on_week_end``: a row on t.

Industry: SIC on t from ``sic_history.csv`` (latest header on or before t; before the first header the
earliest, flagged ``earliest_header``; a CIK with no header takes ``security_master.sic``, flagged
``master``); 6770 is replaced by ``operating_sic_after_6770`` (plan 5.2, flagged); FF49 from
``ff_industry_maps.csv``, an unmatched SIC goes to 49 Other (flagged ``unmatched_other``).

Earnings: ``earnings_event_within_3_sessions`` = Y when an Item 2.02 event of the CIK
(``earnings_events.csv`` ``d0_session``, every row) has D0 within 3 XNAS sessions of t, before or
after; ``results_release_within_3_sessions`` uses only ``event_kind`` = results_release;
``earnings_nearest_d0_offset`` is the signed session distance of the nearest D0 within 10 sessions,
D0's session minus the week end's: positive means D0 comes after the week end (a tie takes the earlier).

Completeness checks (plan 3.3, no returns):
- per week, how many of ranks 1-250 and 1-300 have a canonical close in the week / on t;
- ``missing``: universe-base names listed that week with no canonical rank (no series, or the series
  does not cover the week), outside the weeks before the first / after the last trade (``outside_trading``:
  the listing starts or ends within 30 days of the series, or step 6 marked it so, or, at a relist junction
  step 9 documented from SEC filings (``CACHE/reconcile/relist_junctions.csv``: ``old_nasdaq_last_session``,
  ``first_new_session``), a week with no canonical close after the old shares' last Nasdaq session and
  before the new shares' first one: the old shares were suspended from Nasdaq and the new ones not yet
  listed, so nothing traded there (WW 2025-05-16, VRM 2024-12-02, OPI 2025-10-07, CORZ 2023-01;
  ``relist_gap_weeks``)) and not a new listing
  that is still short of 25 sessions (``young``, ``young_rule``: ``canonical``, a canonical close without
  a dv50 within 75 days of the first canonical row, only at a relist junction or for a new listing whose
  series starts within 5 sessions of its earliest start (``series_starts_listing``; a transfer from NYSE
  whose rows start at the Nasdaq start, KDP 2020-09 and HST 2020-11, stays missing, so its proxy blocks the
  week); or, without a dv50 from step 6 either,
  ``new_listing``, the security's first listing run in its first 24 XNAS sessions counted from the
  earliest day it can have begun trading, only for a real new listing: the IPO rule's first price, or a
  snapshot start counted from the session after the last snapshot without it (or, when a final IPO
  prospectus, 424B4 / 424B1, was filed in that gap, from two sessions before it), of an issuer that filed no
  10-K / 10-Q more than 90 days before and had no other Nasdaq security listed within 90 days before
  (``new_listing_evidence``: a transfer from NYSE such as NBL or LSI, a reorganised tracking stock such as
  QVCA or QRTEB, the successor of a successor link such as AspenTech 2022 (``successor_link``), and a
  start no snapshot dates stay missing); or ``short_series``, step 6 holding 1-24
  rows in the 50-session window of a series that began within 75 days, at most 75 days after the
  listing, of an issuer not public before: IRHO and RACC in their first weeks; a name step 6 holds no
  row for, n50 = 0, is not young; the summary counts the young weeks by rule, the rule-1 weeks whose
  proxy reaches the band median, the first-run weeks rule 1 leaves missing, the canonical-rule weeks by
  basis, predecessor link and proxy_above, and the weeks the canonical rule leaves missing, with how many
  block; ``successor_links`` lists every successor link, master and step 11, with its treatment and the
  successor's first five weeks). Each missing
  name-week gets evidence of top-250 membership
  (``evidence``): step 6's dollar volume (``weekly_metrics.pkl``: any source, the stored files included)
  against this week's canonical rank-250 cut (``dv``), else a market-cap proxy (Wayback market cap, or XBRL
  public float, carried up to 12 months, from step 6) against the median proxy of canonical ranks 200-250
  (plan 3.3 check 1; ``proxy``); a raw close under $10 in step 6 settles it (``price_lt_10``). A listed
  name step 6 has no row for (later listings after a Form 25 cut, such as SMCI, CHRD and CORZ in the
  first round-5 step-6 build) takes the same evidence directly: the dollar volume of the stored files the master
  names for it, on its listed days only (``stored_direct``), and the market cap / float as step 6 attaches
  them. A name-week with none of these is ``unknown``: it gets no estimate, is counted apart, counts as a
  top-250 name-week in the upper bounds and makes its week incomplete (never taken as small).
  The expected count (``p_top250``) uses each rule's hit rate by year and by distance to the cut
  (value / cut in bins; the proxy's value is the larger of market cap / band median market cap and float /
  band median float, the either-one reading of ``proxy_above``, so a float-only week is binned by its float;
  check 6 also gives each year's shares with the plan's market-cap-first binning): the dollar-volume rule
  measured on canonical names step 6 priced from a stored
  file only (like most missing names); the proxy rule, separately for single-class securities and for
  classes of a multi-class company (whose market cap and float are the company's), measured on every
  universe-base name-week whose top-250 status dollar volume settles (canonical ranks, or step 6's
  dollar volume for the missing ones), not on the canonical names alone, which were picked for
  liquidity; a class of a multi-class company with only that proxy whose own dv50 (canonical, else step
  6's at any price) in the nearest week within 13 weeks is under a quarter of that week's rank-250 cut
  takes the dollar-volume rule's lowest bin instead (``proxy_class_capped``: QRTEB 2018-03..05 carried
  Qurate's $9.0B float while its own dv50 from 2018-05-25 ranks about 1,092nd); ``proxy_above`` (check 1,
  complete_250) is not changed by it; names with no evidence at all are unknown (above), with no rate;
- the reason a name is missing (per week): ``tiingo_pending`` (a Tiingo request the running fetch has
  not answered), ``yahoo_pending`` (planned for Yahoo, no Yahoo answer yet); both only in the weeks a
  candidate row of that planned source needs (``needed_start``..``needed_end``), other weeks fall through
  to the security's next reason (CORZ: Tiingo for CORZQ 2021-11..2023-01, Yahoo from 2023-12),
  ``fetched_pending_reconcile``
  (inside a Tiingo or Yahoo answer newer than the panel: step 9 has to be rerun), ``answer_not_in_panel``
  (inside an answer older than the panel that the panel still lacks: for step 9 to fix, not pending; SMCI
  and CHRD after their Form 25 cuts), ``unfillable`` (inside an
  ``unfillable.csv`` window, or every planned source answered without rows: Tiingo wrong_entity / no_data,
  Yahoo no_rows / failed), ``no_vendor_source``, ``series_gap`` (a series that does not reach this week),
  ``short_window`` (the week has a canonical close but the 50-session window holds fewer than 25 rows and
  no young rule applies: a series that starts inside the window, such as a transfer from NYSE whose rows
  start at the Nasdaq start (KDP 2020-09), a tracking stock or a 1:1 successor not linked, or a hole in the
  window; it replaces ``series_gap`` and ``answer_not_in_panel``, which say a row is lacking, so these
  weeks are judged by their evidence like ``not_candidate``; round 10: 422 of the 1,055 name-weeks so labelled),
  ``candidate_other``, ``not_candidate``. The residual survivorship estimate leaves out the three pending
  reasons; ``*_ex_siblings`` also leaves out missing classes whose sibling class ranks that week (owner
  question 8.4: if only the most liquid class is kept, they do not matter); ``*_upper`` adds the unknown
  name-weeks as top-250 name-weeks; the report splits the residual by evidence and distance to the cut
  and gives a lower bound with the proxy's lowest bin (under 25% of the band median) set to 0;
- snapshot age (days since the latest snapshot of any source in ``listing_snapshots_index.csv``,
  flagged above 160 days); mcap-weighted coverage of the universe base (a multi-class company's value
  split among its listed classes): by a canonical close in the week, and (``*_known_dv``) by a canonical
  close or step-6 dollar volume, i.e. names whose liquidity is known even if no series was needed;
- step 6 comparison: the canonical rank-250/300 dv50 cut against step 6's, and the overlap of the two
  top-250 sets (with the reasons for step 6 names not in the canonical top 250);
- plan 3.3 checks 2 (company-list capture dates), 3 (Nasdaq-100 year-end members 2011-2019),
  4 (Form 25 delistings with float >= $1B), 5 (fetch margin of the month-1 names) and 6 (survivorship
  by year: the residual of every reason but the pending ones, unknown name-weeks counted as top-250
  name-weeks, at most 2% of slots; the plan's unfillable-only share is kept beside it); check 7 (FINRA)
  needs requests and is not run.

A week is ``complete_250`` when at least 250 names are ranked, all of ranks 1-250 have a canonical close
in the week, no missing name has dollar-volume evidence at or above the rank-250 cut, none has only a
proxy that reaches the band median (its market cap or its float, either one: a carried market cap
below the median does not outweigh a float that reaches it, as in validate's check 1;
``n_missing_proxy_above_float_only`` counts the names only the float puts there), and none is unknown;
``complete_250_strict`` also needs fewer than one expected missing top-250 name (``est_missing_top250``
< 1, all reasons); ``complete_250_after_pending`` applies the ``complete_250`` conditions with the pending reasons set aside (what the week becomes once
the running fetches and a step-9 rerun land, if they fill those names).

The investment companies left out are listed with their evidence and with what they held while they were
in the base (``investment_companies.csv``: top-250 name-weeks by a dv50 rank that counts them, and missing
name-weeks with the evidence class they would have had); top-250 names with no SEC SIC that are not
investment companies (OZK) are listed in the summary.

Outputs:
  INPUTS/weekly_universe_top300.csv.gz  ranks, flags, FF49, earnings flags (no vendor values)
  INPUTS/weekly_universe_summary.csv    one row per week: counts, buckets, shares (no vendor values)
  CACHE/universe/weekly_listed.csv.gz   every listed security-week: ticker, flags, ranks, missing reason
  CACHE/universe/weekly_liquidity.csv.gz  canonical dv20/dv50 and close for every name-week with data
  CACHE/universe/cutoffs.csv            per week canonical and step-6 dv cuts and proxy cuts (local)
  CACHE/universe/missing_by_security.csv  missing name-weeks per security with their evidence
  CACHE/universe/month2_leads.csv       securities the checks point to for the month-2 fetch
  CACHE/universe/capture_coverage.csv, nasdaq100_check.csv, form25_check.csv
  CACHE/universe/investment_companies.csv  one row per left-out security: spans, evidence, former weeks
  CACHE/universe/investment_company_spans.csv  security_id, cik, ticker, start, end (read by step 14)
  CACHE/universe/sec_submissions_read.csv.gz  name, sha256 of every SEC submissions file read (or missing)
  CACHE/universe/completeness_by_year.csv  the complete shares and check 6 by year (also printed)
  CACHE/universe/universe_summary.json  the checks by year, inputs' sha256, counts

Usage (everything is read from local files; rerun after the Tiingo / Yahoo fetches and a step-9 rerun)::

    PYTHONPATH=. python scripts/reversal_data_universe.py
    # into another directory, with a fixed copy of the running fetch's status, to compare two builds:
    PYTHONPATH=. python scripts/reversal_data_universe.py --tiingo-status COPY.csv --out-dir DIR
    # once, to fetch the SEC submissions (main file or an older page reaching the window) a listed CIK
    # lacks in the cache (at most 2 requests a second, data.sec.gov only), before the build:
    PYTHONPATH=. python scripts/reversal_data_universe.py --fetch-sec-submissions --out-dir DIR
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re
import time

import numpy as np
import pandas as pd

from scripts import reversal_data_common as common
from scripts import reversal_data_prefilter as pf

CODE_VERSION = "2026-10-03.1"
MAIN = common.MAIN_CHECKOUT
CACHE = common.CACHE
INPUTS = common.INPUTS
OUT = CACHE / "universe"

MASTER = INPUTS / "security_master.csv"
INTERVALS = INPUTS / "ticker_intervals.csv"
PERIODIC_HISTORY = INPUTS / "periodic_form_history.csv"
SNAPSHOT_INDEX = INPUTS / "listing_snapshots_index.csv"
FORM25 = INPUTS / "form25_nasdaq_2012_2026.csv"
CANDIDATES = INPUTS / "candidate_fetch_list.csv"
UNFILLABLE = INPUTS / "unfillable.csv"
EARNINGS = INPUTS / "earnings_events.csv"
SIC_HISTORY = INPUTS / "sic_history.csv"
FF_MAPS = INPUTS / "ff_industry_maps.csv"
TERMINAL = INPUTS / "terminal_returns_2012_2026.csv"
PANEL = CACHE / "prices" / "daily_panel.csv.gz"
WEEKLY_METRICS = CACHE / "prefilter" / "weekly_metrics.pkl"
LISTS = CACHE / "prefilter" / "lists.csv.gz"
NO_SERIES = CACHE / "reconcile" / "no_series.csv"
TIINGO_STATUS = CACHE / "tiingo" / "fetch_status.csv"
YAHOO_REPORT = CACHE / "yahoo" / "entity_report.csv"
YAHOO_STATUS = CACHE / "yahoo" / "fetch_status.csv"
TERMINAL_QUEUE = CACHE / "terminal" / "manual_review_queue.csv"
RELIST_TABLE = CACHE / "reconcile" / "relist_junctions.csv"  # step 9: relist junctions read from SEC filings
STORED_DIR = pf.STORED_DIR
NASDAQ100 = Path("output/research_only/holdout_2011_2019/inputs/nasdaq100_members_wikipedia_yearend.json")

TOP300_FILE = INPUTS / "weekly_universe_top300.csv.gz"
SUMMARY_FILE = INPUTS / "weekly_universe_summary.csv"

WINDOW_START, WINDOW_END = pf.WINDOW_START, pf.WINDOW_END
FIRST_WEEK, LAST_WEEK = pf.FIRST_WEEK, pf.LAST_WEEK
MIN_PRICE = 10.0
DV_WINDOWS = dict(pf.DV_WINDOWS)  # {20: 10, 50: 25}: sessions -> minimum rows for the median
CLOSE_STALE_SESSIONS = pf.CLOSE_STALE_SESSIONS  # 5
TOP_N, PRICE_RANK = 250, 300
TOP_RANGE = (150, 200, 250)  # the owner's N candidates
BAND = (200, 250)  # plan 3.3 check 1: the proxy cut is the median of these canonical ranks
EARNINGS_WINDOW = 3
EARNINGS_OFFSET_MAX = 10
STALE_SNAPSHOT_DAYS = 160
TRADING_BOUND_DAYS = pf.TRADING_BOUND_DAYS  # 30
CAPTURE_FROM = "2011-12-30"  # check 2 reads company lists from the week before the first week end
YOUNG_DAYS = pf.WARMUP_DAYS  # 75 calendar days: a new listing without 25 sessions is not missing
YOUNG_ISSUER_DAYS = 90  # an issuer filing 10-K / 10-Q this long before a listing starts was public already
UNFILLABLE_SHARE_LIMIT = 0.02  # plan 3.3 check 6
SRC_CODES = {"wiki": 0, "tiingo": 1, "yahoo": 2}
FETCH_FINAL = {"done", "done_review", "partial", "wrong_entity", "no_data"}
FETCH_EMPTY = {"wrong_entity", "no_data"}
YAHOO_EMPTY = {"no_rows", "failed"}  # Yahoo entity-report verdicts with no usable rows in the need
STRICT_EXPECTED_LIMIT = 1.0  # complete_250_strict: fewer than one expected missing top-250 name in the week
EVIDENCE = ["price_lt_10", "dv", "proxy", "unknown"]  # a missing name-week's evidence of its top-250 status
MISSING_REASONS = ["tiingo_pending", "yahoo_pending", "fetched_pending_reconcile", "answer_not_in_panel", "unfillable",
                   "no_vendor_source", "series_gap", "short_window", "candidate_other", "not_candidate"]
# filled once the fetches and a step-9 rerun finish
PENDING_REASONS = {"tiingo_pending", "yahoo_pending", "fetched_pending_reconcile"}
# An answer step 9 already had but left out of the panel says more than these (a second fetch of the same
# history would be dropped the same way; SMCI: Tiingo answered 2011-06..2026-08, a Yahoo request is planned).
IN_HAND_OVERRIDES = ["yahoo_pending", "series_gap", "no_vendor_source", "candidate_other", "not_candidate"]
# Labels that say a row is lacking; a missing week with a canonical close and no dv50 (fewer than 25 rows in
# the 50-session window, no young rule) is short_window instead (round 10: CDW 2013-06, KDP 2020-09, QVCA 2014-10).
SHORT_WINDOW_FROM = ("series_gap", "answer_not_in_panel")

TOP300_COLUMNS = ["week_end", "security_id", "ticker", "dv50_rank", "dv20_rank", "price_ge_10", "ff49",
                  "earnings_event_within_3_sessions",
                  # extras (ranks, flags and SEC facts only)
                  "dv50_rank_any_price", "dv20_rank_any_price", "close_in_week", "close_on_week_end",
                  "close_lag_sessions", "ff49_name", "sic", "sic_basis", "results_release_within_3_sessions",
                  "earnings_nearest_d0_offset", "cik", "foreign_filer", "multi_class_group",
                  "dv_window_before_listing"]
SUMMARY_COLUMNS = ["week_end", "n_listed_common", "n_with_vendor_prices", "n_price_ge_10",
                   "cutoff_rank250_dv_bucket", "n_unresolved_candidates", "mcap_weighted_coverage",
                   "snapshot_age_days",
                   # extras
                   "n_listed_nasdaq", "n_foreign_excluded", "n_non_common_excluded", "n_spac_shell_excluded",
                   "n_investment_company_excluded",
                   "n_unknown_foreign_flag", "n_ranked_dv50", "n_ranked_dv20", "cutoff_rank300_dv_bucket",
                   "top250_close_in_week", "top300_close_in_week", "top250_close_on_week_end",
                   "top300_close_on_week_end", "top250_zero_volume_close", "n_outside_trading", "n_young",
                   "n_missing", "n_missing_with_pf_dv", "n_missing_pf_dv_ge_cut250", "n_missing_pf_dv_ge_cut300",
                   "n_missing_pf_dv_ge_cut250_not_pending", "n_missing_proxy_above",
                   "n_missing_proxy_above_single_class", "n_missing_proxy_above_no_pf_dv",
                   "n_missing_proxy_above_no_pf_dv_not_pending", "n_missing_proxy_above_float_only",
                   "n_missing_dv_stored_direct",
                   "n_missing_unknown", "n_missing_unknown_not_pending", "est_missing_top250",
                   "est_missing_top250_residual", "est_missing_top250_residual_ex_siblings",
                   "est_missing_top250_residual_upper", "n_unresolved_candidates_ge_cut300",
                   *[f"n_missing_{r}" for r in MISSING_REASONS],
                   "mcap_weighted_coverage_names", "mcap_weighted_coverage_known_dv", "snapshot_stale", "snapshot_source",
                   "pf_cut250_ratio", "pf_cut300_ratio", "top250_overlap_prefilter", "pf_top250_not_top250_here",
                   "top250_ff49_missing", "top250_earnings_flag", "top250_series_ends_within_4w",
                   "top250_series_ends_unresolved_terminal", "complete_250", "complete_250_strict",
                   "complete_250_after_pending"]


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def read_csv_text(path: Path, **kwargs) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, **kwargs)


def read_stable(path: Path, reader, tries: int = 6, wait: float = 10.0):
    """``reader(path)`` once the file is the same (size and mtime) before and after the read: step 6 may
    be rewriting its weekly table while this runs."""
    for _ in range(tries):
        before = (path.stat().st_size, path.stat().st_mtime_ns)
        try:
            data = reader(path)
        except Exception:  # a half-written file
            data = None
        if data is not None and (path.stat().st_size, path.stat().st_mtime_ns) == before:
            return data
        log(f"{path.name} changed while it was read; reading it again in {wait:g} s")
        time.sleep(wait)
    raise RuntimeError(f"{path} kept changing while it was read")


def day_before(day: str) -> str:
    return (pd.Timestamp(day) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def yes_no(mask) -> np.ndarray:
    return np.where(np.asarray(mask, dtype=bool), "Y", "N")


# ------------------------------------------------------------------ calendar

def calendar() -> tuple[pd.DatetimeIndex, pd.DatetimeIndex, np.ndarray, np.ndarray]:
    """(sessions 2011-06-01..2026-08-31, the 759 week ends, each week end's session position, the
    session position of the week end before it)."""
    sessions = pf.xnas_sessions(WINDOW_START, WINDOW_END)
    every = pf.week_ends(sessions, WINDOW_START, WINDOW_END)
    weeks = pf.week_ends(sessions, FIRST_WEEK, LAST_WEEK)
    week_pos = sessions.get_indexer(weeks)
    every_pos = sessions.get_indexer(every)
    prev_pos = every_pos[np.searchsorted(every_pos, week_pos) - 1]
    return sessions, weeks, week_pos, prev_pos


# ------------------------------------------------------------------ listed set (plan 3.1)

def load_intervals(path: Path = INTERVALS) -> pd.DataFrame:
    """Snapshot-evidence Nasdaq intervals (SEC current-ticker rows carry no listing dates)."""
    intervals = read_csv_text(path)
    return intervals[(intervals["start"] != "") & (intervals["exchange"] == "NASDAQ")].reset_index(drop=True)


def doubtful_intervals(intervals: pd.DataFrame, master: pd.DataFrame) -> pd.Series:
    """Nasdaq intervals that rest on one snapshot row matched by ticker alone (``n_snapshots`` 1, ``match``
    ticker_only) of a security whose SEC current-ticker rows name only other exchanges: Ford (F, NYSE) on
    one 2019-06-17 symbol-directory file. Such an interval is left out of the listed set and reported,
    since one row that matched no SEC name cannot outweigh the SEC's exchange; the security master owns
    the fix. A delisted security (no SEC current exchange) keeps its interval."""
    if intervals.empty or not {"n_snapshots", "match"} <= set(intervals.columns):
        return pd.Series(False, index=intervals.index)
    exchanges = (dict(zip(master["security_id"], master["exchanges_sec_current"]))
                 if "exchanges_sec_current" in master else {})
    current = intervals["security_id"].map(exchanges).fillna("").astype(str)
    elsewhere = current.str.strip().ne("") & ~current.str.upper().str.contains("NASDAQ", regex=False)
    one_row = pd.to_numeric(intervals["n_snapshots"], errors="coerce").eq(1)
    return one_row & intervals["match"].eq("ticker_only") & elsewhere


def drop_doubtful_intervals(intervals: pd.DataFrame, master: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """(the intervals without ``doubtful_intervals``, a report of those left out)."""
    doubtful = doubtful_intervals(intervals, master)
    exchanges = (dict(zip(master["security_id"], master["exchanges_sec_current"]))
                 if "exchanges_sec_current" in master else {})
    report = [{"security_id": r.security_id, "ticker": r.ticker, "start": r.start, "end": r.end,
               "start_prev_absent": r.start_prev_absent, "end_next_absent": r.end_next_absent,
               "source": r.source, "source_url": getattr(r, "source_url", ""), "match": r.match,
               "n_snapshots": r.n_snapshots, "sec_current_exchanges": exchanges.get(r.security_id, ""),
               "hand_off": "security master: one ticker-only snapshot row against an SEC exchange that is not "
                           "Nasdaq; drop the interval in ticker_intervals.csv"}
              for r in intervals[doubtful].itertuples(index=False)]
    return intervals[~doubtful].reset_index(drop=True), report


def listing_spans(intervals: pd.DataFrame, master: pd.DataFrame, first_row: dict | None = None,
                  window_end: str = WINDOW_END, sessions: pd.DatetimeIndex | None = None) -> pd.DataFrame:
    """One row per interval with the first and last day it counts as listed (see the module notes).

    The IPO rule needs the first price to be later than the first session after ``start_prev_absent``:
    step 9 keeps a ticker's rows only from that session on (the widened mapping span), so a series that
    starts exactly there was cut, not born (a transfer from NYSE such as AMD or CSX, or another company's
    rows under a reused ticker such as TRUE before TrueCar's IPO), and its start stays the snapshot's
    (``ipo_boundary``)."""
    delist = dict(zip(master["security_id"], master["delist_date"]))
    transfer = dict(zip(master["security_id"], master["transfer_date"])) if "transfer_date" in master else {}
    first_row = first_row or {}
    if sessions is not None:
        values = sessions.strftime("%Y-%m-%d").to_numpy(dtype=object)
        next_session = lambda day: values[min(np.searchsorted(values, day, "right"), len(values) - 1)]
    else:
        next_session = lambda day: (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    rows = []
    for row in intervals.itertuples(index=False):
        sid = row.security_id
        end = day_before(row.end_next_absent) if row.end_next_absent else window_end
        end = max(end, row.end)
        start, ipo, boundary = row.start, False, False
        first = first_row.get(sid, "")
        if row.start_prev_absent and first and row.start_prev_absent < first < row.start:
            if first <= next_session(row.start_prev_absent):
                boundary = True
            else:
                start, ipo = first, True
        cut, kind, after = "", "", False
        for day, what in ((delist.get(sid, ""), "form25"), (transfer.get(sid, ""), "transfer")):
            if not day:
                continue
            if day <= start:
                after = True  # this interval begins on or after the removal: a later listing
            elif day <= end and (not cut or day < cut):
                cut, kind = day, what
        if cut:
            end = day_before(cut)
        rows.append({"security_id": sid, "ticker": row.ticker, "list_start": start, "list_end": end,
                     "snapshot_start": row.start, "start_prev_absent": row.start_prev_absent, "obs_end": row.end,
                     "end_next_absent": row.end_next_absent, "name": row.name_in_source,
                     "share_class": row.share_class, "cut": kind, "ipo_start": ipo, "ipo_boundary": boundary,
                     "after_cut": after})
    spans = pd.DataFrame(rows)
    if len(spans):
        spans["non_common"] = [pf.non_common_interval(n, c) for n, c in zip(spans["name"], spans["share_class"])]
    return spans


def foreign_mask(security: np.ndarray, days: np.ndarray, foreign: dict) -> np.ndarray:
    """True where the security is a foreign filer on that day (YYYY-MM-DD strings)."""
    out = np.zeros(len(security), dtype=bool)
    days = np.asarray(days, dtype="U10")
    frame = pd.DataFrame({"s": np.asarray(security, dtype=object)})
    for sid, index in frame.groupby("s").indices.items():
        spans = foreign.get(sid)
        if not spans:
            continue
        d = days[index]
        hit = np.zeros(len(index), dtype=bool)
        for a, b in spans:
            hit |= (d >= a) & (d <= b)
        out[index] = hit
    return out


def listed_now_shells(spans: pd.DataFrame, master: pd.DataFrame, intervals: pd.DataFrame, shells,
                      sic_path: Path = SIC_HISTORY, spac_like=None) -> tuple[list[str], list[str]]:
    """(blank-check shells listed now that ``spac_shells`` misses, by step 6's ``spac_like_now`` on step 6's
    listed-now set: a common-stock listing in the last week that runs to the end of the data; the
    shell-like names in the last week whose listing ends earlier, which that rule does not take and the
    base keeps, reported)."""
    spac_like = spac_like or (lambda ids: pf.spac_like_now(master, intervals, ids, sic_path))
    in_last = spans[(spans["list_start"] <= LAST_WEEK) & (spans["list_end"] >= LAST_WEEK) & ~spans["non_common"]]
    now = set(in_last.loc[in_last["list_end"] >= WINDOW_END, "security_id"])
    added = sorted(set(spac_like(now)) - set(shells))
    ending = sorted(set(spac_like(set(in_last["security_id"]) - now)) - set(shells))
    return added, ending


def run_starts(spans: pd.DataFrame, slack_days: int = CLOSE_STALE_SESSIONS * 2) -> np.ndarray:
    """For each span, the first day of the security's continuous listing run that holds it (spans of a
    security that overlap or follow within ``slack_days`` are one run: a rename is not a new listing)."""
    out = spans["list_start"].to_numpy(dtype=object).copy()
    if not len(spans):
        return out
    order = spans.sort_values(["security_id", "list_start"], kind="stable").index
    sid_prev, run_start, run_end = None, "", ""
    for i in order:
        sid, a, b = spans.at[i, "security_id"], spans.at[i, "list_start"], spans.at[i, "list_end"]
        gap = (pd.Timestamp(a) - pd.Timestamp(run_end)).days if sid == sid_prev and run_end else None
        if gap is None or gap > slack_days:
            run_start, run_end = a, b
        else:
            run_end = max(run_end, b)
        out[spans.index.get_loc(i)] = run_start
        sid_prev = sid
    return out


def weekly_listed(spans: pd.DataFrame, weeks: pd.DatetimeIndex, foreign: dict,
                  shells: set | dict = frozenset(), investment: dict | None = None) -> pd.DataFrame:
    """One row per (security, week end) listed on that day, with the universe-base flags
    (``investment``: security -> the spans it is an investment company, see ``investment_company_spans``)."""
    values = weeks.values.astype("datetime64[D]")
    starts = np.array(spans["list_start"].to_numpy(dtype=str), dtype="datetime64[D]")
    ends = np.array(spans["list_end"].to_numpy(dtype=str), dtype="datetime64[D]")
    low = np.searchsorted(values, starts, "left")
    high = np.searchsorted(values, ends, "right")
    count = np.clip(high - low, 0, None)
    which = np.repeat(np.arange(len(spans)), count)
    k = (np.concatenate([np.arange(a, b) for a, b in zip(low, high) if b > a]) if count.sum()
         else np.array([], dtype=int))
    boundary = spans["ipo_boundary"].values if "ipo_boundary" in spans else np.zeros(len(spans), dtype=bool)
    starts_of_run = run_starts(spans)
    frame = pd.DataFrame({"security_id": spans["security_id"].values[which], "week_index": k.astype(int),
                          "ticker": spans["ticker"].values[which], "interval_start": spans["list_start"].values[which],
                          "listing_start": starts_of_run[which],
                          "ipo_boundary": boundary[which].astype(bool),
                          "non_common": spans["non_common"].values[which].astype(bool)})
    frame = frame.sort_values(["security_id", "week_index", "interval_start"], kind="stable")
    frame = frame.drop_duplicates(["security_id", "week_index"], keep="last").reset_index(drop=True)
    frame["week_end"] = weeks[frame["week_index"].values]
    days = frame["week_end"].dt.strftime("%Y-%m-%d").values
    frame["spac_shell"] = frame["security_id"].isin(set(shells))
    frame["foreign"] = foreign_mask(frame["security_id"].values, days, foreign)
    frame["investment_company"] = foreign_mask(frame["security_id"].values, days, investment or {})
    frame["eligible"] = ~(frame["non_common"] | frame["spac_shell"] | frame["foreign"] | frame["investment_company"])
    return frame.sort_values(["week_index", "security_id"]).reset_index(drop=True)


# ------------------------------------------------------------------ investment companies (owner, CRSP share codes 10/11)

SUBMISSIONS_DIR = common.RAW / "sec" / "submissions"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSION_PAGE_URL = "https://data.sec.gov/submissions/{name}"
SEC_FETCH_PER_SECOND = 2  # this round's own limit (the SEC allows 10 a second for every process together)
IC_ELECTION, IC_WITHDRAWAL = "N-54A", "N-54C"  # a BDC's election under section 54(a), and its withdrawal
IC_DEREGISTRATION = "N-8F"  # a registered fund's application to deregister (N-8F, N-8F NTC, N-8F ORDR)
# Forms only an investment company files (base form, an amendment's /A dropped): registration (N-2 family,
# N-8A, N-6F notice of a BDC election), shareholder and holdings reports (N-CSR, N-CSRS, N-Q, N-CEN, N-PORT,
# N-30D, N-30B-2), repurchase and merger forms (N-23C, N-14 8C, POS 8C), fidelity bond and custody filings
# under rules 17g-1 and 17f (40-17G, 40-17F1, 40-17F2). N-PX counts only before IC_NPX_BEFORE.
IC_FORMS = {"N-2", "N-2ASR", "N-2MEF", "N-2 POSASR", "N-8A", "N-6F", IC_ELECTION, "N-CSR", "N-CSRS", "N-Q", "N-CEN",
            "NPORT-P", "NPORT-EX", "N-30D", "N-30B-2", "N-23C-1", "N-23C-2", "N-23C3A", "N-23C3B", "N-14 8C", "POS 8C",
            "40-17G", "40-17F1", "40-17F2", "N-PX"}
IC_NPX_BEFORE = "2024-07-01"  # from the 2024 say-on-pay rule every 13F manager files N-PX (banks, Intel, OZK)
IC_RUN_GAP_DAYS = 550  # filings this far apart or closer are one run (registered funds report twice a year)
IC_MIN_RUN = 2  # a single stray filing (Medical Action's N-30D of 2001) is not a run
IC_SIC = "6726"  # unit investment trusts, face-amount certificate and closed-end management investment offices
IC_OPEN_END = "2099-12-31"
IC_TAIL_DAYS = 30  # a span ending this close to a delisting runs on to the listing's end (a merger tail)


def base_form(form: str) -> str:
    return form[:-2] if str(form).endswith("/A") else str(form)


SUBMISSIONS_MISSING = "missing"  # the state recorded for a submissions file the build looked for and lacked
SUBMISSIONS_TABLE = "sec_submissions_read.csv.gz"  # name, sha256 of every submissions file the build looked for
SUBMISSIONS_DIGEST_KEY = "sec_submissions_digest"   # its inputs_sha256 key: the digest of that table's lines


def read_submission_json(path: Path, files: dict | None = None) -> dict | None:
    """The parsed submissions file (None when it is not cached); ``files`` (name -> sha256 of the bytes
    parsed, or SUBMISSIONS_MISSING) records what was read, for the build's input digest."""
    if not path.exists():
        if files is not None:
            files[path.name] = SUBMISSIONS_MISSING
        return None
    data = path.read_bytes()
    if files is not None:
        files[path.name] = common.sha256_bytes(data)
    return json.loads(gzip.decompress(data) if path.suffix == ".gz" else data)


def submissions_digest(files: dict) -> str:
    """sha256 over the sorted "name<TAB>sha256-or-missing" lines of every submissions file looked for: it
    changes when a file read is fetched again, a missing page or main file is cached later, or a file
    is removed (round-7 review: these inputs were only counted)."""
    lines = "".join(f"{name}\t{files[name]}\n" for name in sorted(files))
    return common.sha256_bytes(lines.encode("utf-8"))


def cached_filings(cik: str, directory: Path = SUBMISSIONS_DIR,
                   files: dict | None = None) -> tuple[list[tuple[str, str, str]], dict]:
    """(form, filing date, accession) of every filing in the cached submissions of ``cik`` (the recent block
    and every cached older page), and what was read: ``main`` (the main file is cached), ``sic``,
    ``pages_missing`` (older pages listed in the main file but not cached: name, filingFrom, filingTo);
    ``files`` collects the sha256 of each file read (see ``read_submission_json``)."""
    payload = read_submission_json(directory / f"CIK{int(cik):010d}.json.gz", files)
    if payload is None:
        return [], {"main": False, "sic": "", "pages_missing": [], "pages_read": 0}
    filings = (payload.get("filings") or {})
    blocks, missing = [filings.get("recent") or {}], []
    for page in filings.get("files") or []:
        block = read_submission_json(directory / f"{page.get('name')}.gz", files)
        if block is None:
            missing.append({"name": page.get("name", ""), "from": page.get("filingFrom", ""),
                            "to": page.get("filingTo", "")})
        else:
            blocks.append(block)
    rows = []
    for block in blocks:
        forms, dates = block.get("form") or [], block.get("filingDate") or []
        accessions = block.get("accessionNumber") or [""] * len(forms)
        rows.extend(zip(forms, dates, accessions))
    return rows, {"main": True, "sic": str(payload.get("sic") or ""), "pages_missing": missing,
                  "pages_read": len(blocks) - 1}


def ic_evidence(filings: list[tuple[str, str, str]], sic_dates=()) -> list[tuple[str, str, str]]:
    """The (date, form, accession) filings that show an investment company (IC_FORMS; N-PX only before
    IC_NPX_BEFORE), the end markers (the withdrawal N-54C ends an election; an N-8F stops a run from being
    carried forward), plus SIC 6726 header dates."""
    out = []
    for form, day, accession in filings:
        base = base_form(form)
        if base.startswith(IC_DEREGISTRATION):
            out.append((day, IC_DEREGISTRATION, accession))
        elif base == IC_WITHDRAWAL or (base in IC_FORMS and (base != "N-PX" or day < IC_NPX_BEFORE)):
            out.append((day, base, accession))
    out += [(day, f"SIC {IC_SIC}", accession) for day, accession in sic_dates]
    return sorted(out)


def _merge_ic_parts(parts) -> list[list[str]]:
    merged: list[list[str]] = []
    for a, b, basis in sorted(parts):
        if merged and a <= (pd.Timestamp(merged[-1][1]) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"):
            merged[-1][1] = max(merged[-1][1], b)
            merged[-1][2] = "+".join(sorted(set(merged[-1][2].split("+")) | set(basis.split("+"))))
        else:
            merged.append([a, b, basis])
    return merged


def _near(day: str, other: str, days: int) -> bool:
    return abs((pd.Timestamp(day) - pd.Timestamp(other)).days) <= days


def extend_to_listing_end(merged: list[list[str]], end_markers: list[str],
                          listing_ends=(), tail_days: int = IC_TAIL_DAYS) -> list[list[str]]:
    """Each closed span that ends while a security of the issuer is listed is carried to that listing's
    end (``merger_tail``) when the end of the span, or an N-54C / N-8F dated after it within
    IC_RUN_GAP_DAYS (``end_markers``), lies within ``tail_days`` of a delisting of that listing: its last
    listed day, its Form 25 effective date or its last price (``listing_ends``: [(first day, last day,
    [delisting dates])] of the issuer's listing runs that end inside the window). A BDC that withdraws its
    election on the day its merger closes (American Capital 2017-01-03, Oaktree Strategic Income,
    Harvest Capital, Solar Senior, Full Circle, Alcentra, Logan Ridge) stays out of the base through the
    weeks to its delisting; one that withdraws and keeps trading as an operating company (Medallion
    2018, Red Cat) does not."""
    out = []
    for a, b, basis in merged:
        if b == IC_OPEN_END:
            out.append([a, b, basis])
            continue
        horizon = (pd.Timestamp(b) + pd.Timedelta(days=IC_RUN_GAP_DAYS)).strftime("%Y-%m-%d")
        marks = [b] + [w for w in end_markers if b < w <= horizon]
        end = b
        for first, last, delistings in listing_ends:
            if first <= b < last and any(_near(d, t, tail_days) for d in [last, *delistings] if d for t in marks):
                end = max(end, last)
        out.append([a, end, basis if end == b else "+".join(sorted(set(basis.split("+")) | {"merger_tail"}))])
    return _merge_ic_parts(out)


def investment_company_spans(evidence: list[tuple[str, str, str]], latest_filing: str = "",
                             listing_ends=()) -> list[tuple[str, str, str]]:
    """[(first day, last day, basis)] on which the issuer is an investment company, merged:
    ``bdc_election`` from an N-54A to the day before the next N-54C (open-ended without one); a withdrawal
    with no election in the cached filings (one filed before EDGAR, or on a page not cached: American
    Capital, Medallion) shows a BDC from the first evidence on (``bdc_until_withdrawal``);
    ``filings``: each run of at least IC_MIN_RUN investment-company filings no more than IC_RUN_GAP_DAYS
    apart, from its first to its last filing (a fund that turned operating, such as Global Self Storage in
    2016, stops filing them; one that merged is delisted anyway). The last run is carried forward
    (``filings_current``) when it ends within IC_RUN_GAP_DAYS of the issuer's latest filing of any form
    (``latest_filing``) and no N-54C or N-8F is dated within IC_RUN_GAP_DAYS before its end or later: a
    BDC whose election predates the cached filings (Capital Southwest, 1988) is one still. A closed span
    near a delisting runs to the listing's end (``merger_tail``, see ``extend_to_listing_end``)."""
    parts = []
    election, last_withdrawal = "", ""
    first = evidence[0][0] if evidence else ""
    for day, form, _ in evidence:
        if form == IC_ELECTION and not election:
            election = day
        elif form == IC_WITHDRAWAL:
            if election:
                parts.append((election, day_before(day), "bdc_election"))
            elif not last_withdrawal and first < day:
                parts.append((first, day_before(day), "bdc_until_withdrawal"))
            election, last_withdrawal = "", day
    if election:
        parts.append((election, IC_OPEN_END, "bdc_election"))
    ends = [day for day, form, _ in evidence if form in (IC_WITHDRAWAL, IC_DEREGISTRATION)]
    days = sorted({day for day, form, _ in evidence if form not in (IC_WITHDRAWAL, IC_DEREGISTRATION)})
    run, runs = [], []
    for day in days + [None]:
        if run and (day is None or (pd.Timestamp(day) - pd.Timestamp(run[-1])).days > IC_RUN_GAP_DAYS):
            if len(run) >= IC_MIN_RUN:
                runs.append((run[0], run[-1]))
            run = []
        if day is not None:
            run.append(day)
    for k, (a, b) in enumerate(runs):
        end, basis = b, "filings"
        if k == len(runs) - 1 and latest_filing:
            since = (pd.Timestamp(b) - pd.Timedelta(days=IC_RUN_GAP_DAYS)).strftime("%Y-%m-%d")
            current = (pd.Timestamp(latest_filing) - pd.Timestamp(b)).days <= IC_RUN_GAP_DAYS
            if current and not any(day >= since for day in ends):
                end, basis = IC_OPEN_END, "filings_current"
        parts.append((a, end, basis))
    merged = _merge_ic_parts(parts)
    if listing_ends:
        merged = extend_to_listing_end(merged, ends, listing_ends)
    return [tuple(m) for m in merged]


def listing_ends(spans: pd.DataFrame, master: pd.DataFrame, terminal: pd.DataFrame | None = None,
                 window_end: str = WINDOW_END, slack_days: int = CLOSE_STALE_SESSIONS * 2) -> dict:
    """security -> [(first day, last day, [delisting dates])] of each listing run (spans that overlap or
    follow within ``slack_days``: a rename is no end) that ends before ``window_end``; the delisting dates
    are the security's Form 25 effective date (``security_master.delist_date``) and its last price
    (``terminal_returns_2012_2026.csv`` ``last_price_date``) when they fall in or just after the run."""
    delist = dict(zip(master["security_id"], master["delist_date"])) if "delist_date" in master else {}
    last_price = defaultdict(list)
    if terminal is not None and len(terminal) and {"security_id", "last_price_date"} <= set(terminal.columns):
        for sid, day in zip(terminal["security_id"], terminal["last_price_date"]):
            if day:
                last_price[sid].append(day)
    out: dict[str, list] = defaultdict(list)
    if not len(spans):
        return {}
    for sid, group in spans.sort_values(["security_id", "list_start"], kind="stable").groupby("security_id"):
        runs: list[list[str]] = []
        for a, b in zip(group["list_start"], group["list_end"]):
            if runs and (pd.Timestamp(a) - pd.Timestamp(runs[-1][1])).days <= slack_days:
                runs[-1][1] = max(runs[-1][1], b)
            else:
                runs.append([a, b])
        for a, b in runs:
            if b >= window_end:
                continue
            horizon = (pd.Timestamp(b) + pd.Timedelta(days=IC_TAIL_DAYS)).strftime("%Y-%m-%d")
            marks = [d for d in [delist.get(sid, ""), *last_price.get(sid, [])] if d and a <= d <= horizon]
            out[sid].append((a, b, marks))
    return dict(out)


def investment_companies(master: pd.DataFrame, security_ids, sic_history: pd.DataFrame | None = None,
                         directory: Path = SUBMISSIONS_DIR, ends: dict | None = None) -> tuple[dict, pd.DataFrame, dict]:
    """(security -> [(start, end)] investment-company spans, one row per issuer with evidence, facts about
    what was read) for the securities in ``security_ids``. An issuer's spans apply to every security with
    its CIK (all its share classes). ``ends`` (``listing_ends``) carries a span that closes near a
    delisting to the listing's end (``merger_tail``). ``facts["files"]`` holds the sha256 of every
    submissions file read (or SUBMISSIONS_MISSING for one looked for and not cached), ``facts["digest"]``
    their ``submissions_digest``; ``facts["prospectus"]`` the filing dates of each CIK's final IPO
    prospectuses (PROSPECTUS_FORMS), for ``new_listing_evidence``."""
    wanted = master[master["security_id"].isin(set(security_ids)) & (master["cik"] != "")]
    ends = ends or {}
    sic_dates = defaultdict(list)
    if sic_history is not None and len(sic_history):
        hits = sic_history[sic_history["sic"].astype(str) == IC_SIC]
        for cik, day, accession in zip(hits["cik"], hits["observed_date"], hits["source_accession"]):
            sic_dates[str(cik)].append((day, accession))
    spans: dict[str, list] = {}
    files: dict[str, str] = {}
    prospectus: dict[str, list] = {}
    rows, facts = [], {"ciks": 0, "main_missing": [], "pages_missing": 0, "ciks_with_pages_missing": 0,
                       "ic_ciks_with_pages_missing": [], "merger_tail_ciks": []}
    by_cik = wanted.groupby("cik")["security_id"].apply(list)
    for cik, sids in by_cik.items():
        facts["ciks"] += 1
        filings, read = cached_filings(cik, directory, files)
        filed = sorted({d for f, d, _ in filings if base_form(f) in PROSPECTUS_FORMS and d})
        if filed:
            prospectus[cik] = filed
        if not read["main"]:
            facts["main_missing"].append(cik)
        facts["pages_missing"] += len(read["pages_missing"])
        facts["ciks_with_pages_missing"] += bool(read["pages_missing"])
        evidence = ic_evidence(filings, sic_dates.get(str(cik), []))
        if not evidence:
            continue
        if read["pages_missing"]:
            facts["ic_ciks_with_pages_missing"].append(cik)
        issuer_ends = sorted(run for sid in sids for run in ends.get(sid, []))
        issuer = investment_company_spans(evidence, max((d for _, d, _ in filings), default=""), issuer_ends)
        if any("merger_tail" in basis for _, _, basis in issuer):
            facts["merger_tail_ciks"].append(cik)
        first = {}
        for day, form, accession in evidence:
            first.setdefault(form, (day, accession))
        rows.append({"cik": cik, "security_ids": " ".join(sorted(sids)), "sic_now": read["sic"],
                     "spans": " ".join(f"{a}..{b}:{basis}" for a, b, basis in issuer),
                     "first_evidence": evidence[0][0], "last_evidence": evidence[-1][0],
                     "election": " ".join(f"{d}:{a}" for d, f, a in evidence if f == IC_ELECTION),
                     "withdrawal": " ".join(f"{d}:{a}" for d, f, a in evidence if f == IC_WITHDRAWAL),
                     "deregistration": " ".join(f"{d}:{a}" for d, f, a in evidence if f == IC_DEREGISTRATION),
                     "evidence_forms": " ".join(f"{f}:{d}({a})" for f, (d, a) in sorted(first.items())),
                     "n_evidence": len(evidence), "pages_missing": len(read["pages_missing"])})
        if issuer:
            for sid in sids:
                spans[sid] = [(a, b) for a, b, _ in issuer]
    facts["files"] = files
    facts["prospectus"] = prospectus
    facts["files_read"] = sum(v != SUBMISSIONS_MISSING for v in files.values())
    facts["files_missing"] = sum(v == SUBMISSIONS_MISSING for v in files.values())
    facts["digest"] = submissions_digest(files)
    return spans, pd.DataFrame(rows), facts


def fetch_missing_submissions(master: pd.DataFrame, security_ids, directory: Path = SUBMISSIONS_DIR,
                              per_second: int = SEC_FETCH_PER_SECOND) -> dict:
    """Fetch (cache first, ``common.cached_get``) the main submissions file of every listed CIK the cache
    lacks, and the older pages the cache lacks of every CIK whose cached filings hold investment-company
    evidence (an election from before the recent block can matter), at ``per_second`` requests a second."""
    limiter = common.SlidingWindowLimiter({1: per_second})
    headers = common.sec_headers()
    ciks = sorted(set(master.loc[master["security_id"].isin(set(security_ids)), "cik"]) - {""}, key=int)
    asked, failed = [], []

    def get(url: str, path: Path, symbol: str) -> None:
        try:
            common.cached_get(url, path, source="sec_submissions", headers=headers, limiter=limiter, symbol=symbol)
            asked.append(path.name)
        except FileNotFoundError:
            failed.append(f"{path.name}: 404")
        except Exception as exc:  # a refusal or a quota stop: stop asking
            failed.append(f"{path.name}: {type(exc).__name__}")
            raise

    for cik in ciks:
        main_path = directory / f"CIK{int(cik):010d}.json.gz"
        if not main_path.exists() and not main_path.with_name(main_path.name + ".404").exists():
            get(SUBMISSIONS_URL.format(cik=int(cik)), main_path, f"CIK{int(cik)}")
        filings, read = cached_filings(cik, directory)
        if not read["pages_missing"] or not ic_evidence(filings):
            continue
        for page in read["pages_missing"]:
            path = directory / f"{page['name']}.gz"
            if not path.with_name(path.name + ".404").exists():
                get(SUBMISSION_PAGE_URL.format(name=page["name"]), path, f"CIK{int(cik)}")
    return {"requests_or_cache_reads": len(asked), "failed": failed, "files": asked}


# ------------------------------------------------------------------ canonical metrics

PANEL_COLUMNS = ("security_id", "date", "close_raw", "volume_raw", "src_primary", "flags")  # never ``tr``
RELIST_JUNCTION = re.compile(r"(?:^|;)relist_junction(?:;|$)")


def load_panel(path: Path = PANEL) -> pd.DataFrame:
    """Raw close, raw volume and the primary source of every canonical row (``tr`` is not read), and
    ``relist_junction``: the row is the first of new shares after a bankruptcy relisting (step 9's
    ``flags`` token, CHRD 2020-11-20, WW 2025-06-27, OPI 2026-06-18): the dollar-volume windows restart
    there (``canonical_metrics``). The flags text itself is not kept."""
    panel = pd.read_csv(path, usecols=lambda c: c in PANEL_COLUMNS,
                        dtype={"security_id": str, "date": str, "src_primary": str, "flags": str})
    panel["date"] = pd.to_datetime(panel["date"])
    flags = panel.pop("flags") if "flags" in panel else pd.Series("", index=panel.index)
    panel["relist_junction"] = flags.fillna("").str.contains(RELIST_JUNCTION).to_numpy(dtype=bool)
    return panel


# ------------------------------------------------------------------ stored files (named in the master)

STORED_SOURCE = re.compile(r"stored:([^\[\s]+)\[([^\]]*)\]")
STORED_FLOOR_FILES = 25  # a first date shared by this many stored files is the repo's coverage start, not a listing


def stored_sources(text: str) -> list[tuple[str, str, str]]:
    """(file, first date, last date) of each stored file a ``security_master.price_sources`` cell names."""
    out = []
    for match in STORED_SOURCE.finditer(text or ""):
        first, _, last = match.group(2).partition("..")
        out.append((match.group(1), first, last))
    return out


def stored_ticker(file: str) -> str:
    """The ticker a stored file holds (``corz_pre2024.csv`` -> CORZ), as step 6 reads it."""
    return Path(file).stem.upper().split("_")[0]


def stored_floor_dates(master: pd.DataFrame) -> set[str]:
    """First dates shared by STORED_FLOOR_FILES or more stored files (2023-01-03, 2004-01-02, ...)."""
    starts = pd.Series([first for text in master["price_sources"] for _, first, _ in stored_sources(text) if first])
    counts = starts.value_counts()
    return set(counts.index[counts >= STORED_FLOOR_FILES])


def first_rows(panel: pd.DataFrame, sessions: pd.DatetimeIndex) -> dict[str, str]:
    """security -> its first canonical row on the session grid."""
    on_grid = panel[panel["date"].isin(sessions)]
    return on_grid.groupby("security_id")["date"].min().dt.strftime("%Y-%m-%d").to_dict()


def boundary_trims(spans: pd.DataFrame, master: pd.DataFrame, first_row: dict, sessions: pd.DatetimeIndex,
                   floor_dates: set = frozenset()) -> dict[str, str]:
    """security -> the first day its canonical rows count, for a series that starts where step 9 cut it
    (``ipo_boundary``) while the security's own stored file under that ticker starts later, after the
    snapshot that did not show it, on or before the one that does, and more than 5 sessions after the
    canonical first row. The canonical rows before are another company's under the reused ticker (TRUE:
    WIKI rows from 2014-03-26, TrueCar's IPO and its stored file 2014-05-16), so they are dropped; the
    IPO rule then starts the listing at the stored file's first day. A stored file that starts on the
    repo's coverage start (``floor_dates``) says nothing about the listing."""
    sources = dict(zip(master["security_id"], master["price_sources"]))
    values = sessions.strftime("%Y-%m-%d").to_numpy(dtype=object)
    out: dict[str, str] = {}
    for row in spans[spans["ipo_boundary"]].itertuples(index=False):
        first = first_row.get(row.security_id, "")
        if not first:
            continue
        for file, start, _ in stored_sources(sources.get(row.security_id, "")):
            if stored_ticker(file) != row.ticker or not start or start in floor_dates:
                continue
            if not (row.start_prev_absent < start <= row.snapshot_start):
                continue
            gap = int(np.searchsorted(values, start)) - int(np.searchsorted(values, first))
            if gap > CLOSE_STALE_SESSIONS:
                out[row.security_id] = min(out.get(row.security_id, start), start)
    return out


def trim_panel(panel: pd.DataFrame, trims: dict[str, str]) -> pd.DataFrame:
    """``panel`` without each trimmed security's rows before its first counted day."""
    if not trims:
        return panel
    start = pd.to_datetime(panel["security_id"].map(trims))
    return panel[~(start.notna() & (panel["date"] < start))].reset_index(drop=True)


def read_stored_file(path: Path) -> pd.DataFrame:
    """date, raw dollar volume (stored close x volume; the close itself is split-adjusted) of a stored file."""
    data = pd.read_csv(path, usecols=lambda c: c in ("date", "close", "volume"))
    if not {"date", "close", "volume"} <= set(data.columns):
        return pd.DataFrame(columns=["date", "dv"])
    close = pd.to_numeric(data["close"], errors="coerce")
    volume = pd.to_numeric(data["volume"], errors="coerce")
    frame = pd.DataFrame({"date": pd.to_datetime(data["date"]).dt.normalize(), "dv": close * volume})
    return frame[(close > 0).values & volume.notna().values].drop_duplicates("date", keep="last")


def stored_direct_dv(listed: pd.DataFrame, spans: pd.DataFrame, master: pd.DataFrame, sessions: pd.DatetimeIndex,
                     week_pos: np.ndarray, directory: Path = STORED_DIR) -> pd.DataFrame:
    """dv20 / dv50 (``sd_dv20``, ``sd_dv50``, aligned with ``listed``) for universe-base name-weeks without
    a canonical rank that step 6 has no row for (``in_pf`` False: later listings after a Form 25 cut, such
    as SMCI from 2020-01, CHRD, CORZ), from the stored files ``security_master.price_sources`` names for the
    security. A file's rows count only on days the security was listed under that file's ticker, so
    neither another company's rows under a reused ticker nor rows before the listing start enter the
    medians (same windows and minimum rows as the canonical ones)."""
    out = pd.DataFrame({"sd_dv20": np.nan, "sd_dv50": np.nan}, index=listed.index)
    target = (listed["eligible"].to_numpy(dtype=bool) & ~listed["in_pf"].to_numpy(dtype=bool)
              & listed["dv50_rank_any_price"].isna().to_numpy())
    if not target.any():
        return out
    sources = dict(zip(master["security_id"], master["price_sources"]))
    by_security = pd.Series(np.flatnonzero(target)).groupby(listed["security_id"].to_numpy(dtype=object)[target])
    grid = sessions.values.astype("datetime64[D]")
    for sid, positions in by_security:
        mine = spans[spans["security_id"] == sid]
        dv = np.full(len(sessions), np.nan)
        for file, _, _ in stored_sources(sources.get(sid, "")):
            path = directory / file
            own = mine[mine["ticker"] == stored_ticker(file)]
            if own.empty or not path.exists():
                continue
            rows = read_stored_file(path)
            days = rows["date"].values.astype("datetime64[D]")
            listed_day = np.zeros(len(rows), dtype=bool)
            for a, b in zip(own["list_start"], own["list_end"]):
                listed_day |= (days >= np.datetime64(a)) & (days <= np.datetime64(b))
            pos = np.searchsorted(grid, days)
            on_grid = (pos < len(grid)) & (grid[np.minimum(pos, len(grid) - 1)] == days) & listed_day
            free = on_grid.copy()
            free[on_grid] = np.isnan(dv[pos[on_grid]])  # the first file named keeps a day
            dv[pos[free]] = rows["dv"].values[free]
        if np.isnan(dv).all():
            continue
        series = pd.Series(dv)
        index = positions.to_numpy()
        session_of_row = week_pos[listed["week_index"].to_numpy()[index]]
        for window, minimum in DV_WINDOWS.items():
            median = series.rolling(window, min_periods=minimum).median().to_numpy()
            out.iloc[index, out.columns.get_loc(f"sd_dv{window}")] = median[session_of_row]
    return out


def canonical_metrics(panel: pd.DataFrame, sessions: pd.DatetimeIndex, week_pos: np.ndarray,
                      prev_pos: np.ndarray, links: pd.DataFrame | None = None) -> dict:
    """Week x security arrays from the canonical rows on the session grid (see the module notes). With
    ``links`` (``successor_links``), the dv20 / dv50 windows of each ``windows_cross`` successor read its
    predecessor's rows before the handover (``link_successor_rows``; ``successor_windows`` holds the facts);
    the closes, first and last rows stay each security's own."""
    rows = panel[panel["date"].isin(sessions)]
    ids = np.array(sorted(rows["security_id"].unique()), dtype=object)
    column = {s: k for k, s in enumerate(ids)}
    r = sessions.get_indexer(rows["date"])
    c = rows["security_id"].map(column).values
    shape = (len(sessions), len(ids))
    close = np.full(shape, np.nan)
    volume = np.full(shape, np.nan)
    source = np.full(shape, np.nan)
    close[r, c] = rows["close_raw"].values
    volume[r, c] = rows["volume_raw"].values
    source[r, c] = rows["src_primary"].map(SRC_CODES).astype(float).values
    has = ~np.isnan(close)
    dv_values = close * volume
    out = {"ids": ids, "successor_windows": link_successor_rows(dv_values, ids, sessions, links)}
    dv = pd.DataFrame(dv_values)
    for window, minimum in DV_WINDOWS.items():
        out[f"dv{window}"] = dv.rolling(window, min_periods=minimum).median().values[week_pos]
    # A relist junction (the new shares' first row) starts new windows: the old shares' dollar volume
    # does not enter the new shares' medians (the new shares need their own 10 / 25 rows).
    out["relist_junctions"] = {}
    if "relist_junction" in rows and rows["relist_junction"].any():
        junction = np.zeros(shape, dtype=bool)
        junction[r, c] = rows["relist_junction"].to_numpy(dtype=bool)
        for k in np.flatnonzero(junction.any(axis=0)):
            segment = np.cumsum(junction[:, k])
            column_dv = pd.Series(dv_values[:, k])
            for window, minimum in DV_WINDOWS.items():
                median = column_dv.groupby(segment).transform(
                    lambda x: x.rolling(window, min_periods=minimum).median()).to_numpy()
                out[f"dv{window}"][:, k] = median[week_pos]
            out["relist_junctions"][ids[k]] = [sessions[j].strftime("%Y-%m-%d") for j in np.flatnonzero(junction[:, k])]
    position = pd.DataFrame(np.where(has, np.arange(len(sessions))[:, None], np.nan)).ffill().values[week_pos]
    lag = week_pos[:, None] - position
    fresh = ~np.isnan(position) & (lag <= CLOSE_STALE_SESSIONS)
    safe = np.where(fresh, position, 0).astype(int)
    columns = np.broadcast_to(np.arange(len(ids)), safe.shape)
    out["close"] = np.where(fresh, close[safe, columns], np.nan)
    out["lag"] = np.where(fresh, lag, np.nan)
    out["src"] = np.where(fresh, source[safe, columns], np.nan)
    out["zero_volume"] = fresh & (np.where(fresh, volume[safe, columns], 1.0) == 0)
    out["in_week"] = ~np.isnan(position) & (position > prev_pos[:, None])
    out["on_week_end"] = has[week_pos]
    first = has.argmax(axis=0)
    last = len(sessions) - 1 - has[::-1].argmax(axis=0)
    out["first_row"] = {s: sessions[first[k]].strftime("%Y-%m-%d") for k, s in enumerate(ids) if has[:, k].any()}
    out["last_row"] = {s: sessions[last[k]].strftime("%Y-%m-%d") for k, s in enumerate(ids) if has[:, k].any()}
    return out


def take(matrix: np.ndarray, week_index: np.ndarray, columns: np.ndarray, fill=np.nan) -> np.ndarray:
    """matrix[week, column] where column >= 0, else ``fill``."""
    ok = columns >= 0
    out = np.full(len(columns), fill, dtype=matrix.dtype if matrix.dtype == bool else float)
    out[ok] = matrix[week_index[ok], columns[ok]]
    return out


def attach_metrics(listed: pd.DataFrame, metrics: dict) -> pd.DataFrame:
    column = {s: k for k, s in enumerate(metrics["ids"])}
    cols = listed["security_id"].map(column).fillna(-1).astype(int).values
    k = listed["week_index"].values
    out = listed.copy()
    out["has_series"] = cols >= 0
    for name in ("dv20", "dv50", "close", "lag", "src"):
        out[name] = take(metrics[name], k, cols)
    for name in ("in_week", "on_week_end", "zero_volume"):
        out[name] = take(metrics[name], k, cols, fill=False).astype(bool)
    out["first_row"] = out["security_id"].map(metrics["first_row"]).fillna("")
    out["last_row"] = out["security_id"].map(metrics["last_row"]).fillna("")
    junctions = metrics.get("relist_junctions") or {}
    out["segment_first_row"] = segment_first_rows(out, junctions)
    out["segment_junction"] = segment_at_junction(out, junctions)
    return out


def segment_at_junction(listed: pd.DataFrame, junctions: dict[str, list[str]]) -> np.ndarray:
    """True where the week's segment starts at a relist junction (``segment_first_row`` is one of the
    security's junction days, the security's first row included)."""
    out = np.zeros(len(listed), dtype=bool)
    if not junctions or "segment_first_row" not in listed:
        return out
    mask = listed["security_id"].isin(list(junctions)).to_numpy()
    days = {(s, d) for s, ds in junctions.items() for d in ds}
    out[mask] = [(s, d) in days for s, d in zip(listed.loc[mask, "security_id"], listed.loc[mask, "segment_first_row"])]
    return out


def segment_first_rows(listed: pd.DataFrame, junctions: dict[str, list[str]]) -> pd.Series:
    """The first canonical row of the segment a week falls in: the last relist junction on or before the
    week end (the new shares' first row, where the dv20 / dv50 windows restart), else ``first_row``. The
    canonical young rule counts from it, so the new shares' first weeks are young, not missing (CHRD
    2020-11-20, whose old shares' rows start in 2019)."""
    out = listed["first_row"].astype(str).copy()
    if not junctions:
        return out
    mask = listed["security_id"].isin(list(junctions)).to_numpy()
    if not mask.any():
        return out
    week = pd.to_datetime(listed.loc[mask, "week_end"]).dt.strftime("%Y-%m-%d")
    out.loc[mask] = [max((d for d in junctions[s] if d <= w), default=f)
                     for s, w, f in zip(listed.loc[mask, "security_id"], week, out.loc[mask])]
    return out


def rank_within_weeks(frame: pd.DataFrame, value: str, eligible: np.ndarray) -> pd.Series:
    """Descending rank of ``value`` within each week among ``eligible`` rows; ties by security_id
    (``frame`` is sorted by week and security_id)."""
    ranks = frame.loc[eligible].groupby("week_index")[value].rank(ascending=False, method="first")
    return ranks.reindex(frame.index)


def rank_weeks(listed: pd.DataFrame) -> pd.DataFrame:
    """dv50 / dv20 ranks among the universe base (with and without the $10 test); and, for the report on
    the investment companies left out, the dv50 ranks of a base that still counts them (``*_incl_investment``)."""
    out = listed.sort_values(["week_index", "security_id"]).reset_index(drop=True)
    closed = ~np.isnan(out["close"].values)
    base = out["eligible"].values & closed
    priced = base & (out["close"].values >= MIN_PRICE)
    out["price_ge_10"] = np.where(np.isnan(out["close"].values), "",
                                  np.where(out["close"].values >= MIN_PRICE, "Y", "N"))
    for window in DV_WINDOWS:
        has = ~np.isnan(out[f"dv{window}"].values)
        out[f"dv{window}_rank"] = rank_within_weeks(out, f"dv{window}", priced & has)
        out[f"dv{window}_rank_any_price"] = rank_within_weeks(out, f"dv{window}", base & has)
    if "investment_company" in out:
        wide = out["eligible"].values | (out["investment_company"].values
                                         & ~(out["non_common"].values | out["spac_shell"].values | out["foreign"].values))
        has = ~np.isnan(out["dv50"].values)
        out["dv50_rank_incl_investment"] = rank_within_weeks(out, "dv50", wide & closed & has
                                                             & (out["close"].values >= MIN_PRICE))
        out["dv50_rank_any_price_incl_investment"] = rank_within_weeks(out, "dv50", wide & closed & has)
    return out


# ------------------------------------------------------------------ step-6 join, trading bounds, missing

PF_COLUMNS = {"dv50": "pf_dv50", "dv20": "pf_dv20", "price_ge_10": "pf_price", "src": "pf_src",
              "universe": "pf_universe", "dv50_rank": "pf_dv50_rank", "dv20_rank": "pf_dv20_rank",
              "outside_trading": "pf_outside_trading", "mcap": "mcap", "float_usd": "float_usd",
              "n50": "pf_n50"}  # n50: step 6's rows with dollar volume in the 50-session window (any source)


def join_prefilter(listed: pd.DataFrame, weekly_metrics: pd.DataFrame) -> pd.DataFrame:
    columns = {k: v for k, v in PF_COLUMNS.items() if k in weekly_metrics.columns}
    pfw = weekly_metrics[["security_id", "week_end", *columns]].rename(columns=columns)
    for name in set(PF_COLUMNS.values()) - set(pfw.columns):
        pfw[name] = np.nan
    pfw = pfw.assign(week_end=pd.to_datetime(pfw["week_end"]).astype("datetime64[ns]"), in_pf=True)
    out = listed.assign(week_end=listed["week_end"].astype("datetime64[ns]"))
    out = out.merge(pfw, on=["security_id", "week_end"], how="left")
    out["in_pf"] = out["in_pf"].fillna(False).astype(bool)
    for flag in ("pf_universe", "pf_outside_trading"):
        out[flag] = out[flag].fillna(False).astype(bool)
    for text in ("pf_price", "pf_src"):
        out[text] = out[text].fillna("").astype(str)
    out["proxy"] = out["mcap"].where(out["mcap"].notna(), out["float_usd"])
    # The first week step 6 holds any dollar-volume row of the security (a new series, for ``young_weeks``).
    if "n50" in weekly_metrics:
        has = weekly_metrics[weekly_metrics["n50"] > 0]
        first = pd.to_datetime(has.groupby("security_id")["week_end"].min()).astype("datetime64[ns]")
        out["pf_first_data"] = out["security_id"].map(first)
    return out


def apply_stored_direct(listed: pd.DataFrame, direct: pd.DataFrame) -> pd.DataFrame:
    """Use the stored-file medians of ``stored_direct_dv`` as the dollar-volume evidence (``pf_dv*``) of
    the name-weeks step 6 has no row for: source ``stored_direct``, price test unknown (``U``: the stored
    close is split-adjusted). Changes ``listed`` in place."""
    has = (direct["sd_dv50"].notna() | direct["sd_dv20"].notna()).to_numpy()
    listed["dv_stored_direct"] = has
    for window in DV_WINDOWS:
        listed.loc[has, f"pf_dv{window}"] = direct.loc[has, f"sd_dv{window}"].to_numpy()
    listed.loc[has, "pf_price"] = "U"
    listed.loc[has, "pf_src"] = "stored_direct"
    return listed


def direct_proxies(listed: pd.DataFrame, master: pd.DataFrame, lists: pd.DataFrame, floats: pd.DataFrame) -> pd.DataFrame:
    """Market cap (company lists, carried up to 12 months) and XBRL float (the CIK's, within 12 months)
    for the universe-base name-weeks step 6 has no row for, attached as step 6 attaches them
    (``pf.attach_proxies``; ``floats`` already without step 6's unit errors). Changes ``listed`` in place."""
    rows = listed.index[listed["eligible"].to_numpy(dtype=bool) & ~listed["in_pf"].to_numpy(dtype=bool)]
    listed["proxy_direct"] = False
    if not len(rows) or floats is None:
        return listed
    caps = lists.loc[lists["market_cap"] > 0, ["security_id", "snapshot_date", "market_cap"]]
    got = pf.attach_proxies(listed.loc[rows, ["security_id", "week_end"]].reset_index(drop=True), master, caps, floats)
    for column in ("mcap", "float_usd"):
        listed.loc[rows, column] = got[column].to_numpy(dtype=float)
    listed.loc[rows, "proxy_direct"] = (got["mcap"].notna() | got["float_usd"].notna()).to_numpy()
    listed["proxy"] = listed["mcap"].where(listed["mcap"].notna(), listed["float_usd"])
    return listed


def trading_bounds(listed: pd.DataFrame, spans: pd.DataFrame) -> pd.Series:
    """Listed weeks after the last canonical row when the listing ends within 30 days of it (the last
    trade before the Form 25 takes effect), or before the first row when the listing starts within 30
    days before it; for a security with no series, step 6's flag (from every source it read)."""
    listed_range = spans.groupby("security_id").agg(first=("list_start", "min"), last=("list_end", "max"))
    first_listed = pd.to_datetime(listed["security_id"].map(listed_range["first"]))
    last_listed = pd.to_datetime(listed["security_id"].map(listed_range["last"]))
    first = pd.to_datetime(listed["first_row"].replace("", None))
    last = pd.to_datetime(listed["last_row"].replace("", None))
    week = listed["week_end"]
    ended = (week > last) & ((last_listed - last).dt.days <= TRADING_BOUND_DAYS)
    unstarted = (week < first) & ((first - first_listed).dt.days <= TRADING_BOUND_DAYS)
    own = (ended | unstarted).fillna(False).astype(bool)
    stepsix = listed["pf_outside_trading"] if "pf_outside_trading" in listed else False
    return own | (~listed["has_series"] & stepsix)


def relist_gaps(table: pd.DataFrame | None) -> dict[str, list[tuple[str, str]]]:
    """security -> [(old_nasdaq_last_session, first_new_session)] from step 9's relist table: the junctions
    it read from SEC filings (status ``junction`` / ``junction_no_old_rows``, ``document_read`` Y, both
    dates given): the old shares' last Nasdaq session before their suspension and the new shares' first."""
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    if table is None or not len(table):
        return dict(out)
    need = {"security_id", "status", "document_read", "old_nasdaq_last_session", "first_new_session"}
    if not need <= set(table.columns):
        return dict(out)
    rows = table[table["status"].astype(str).str.startswith("junction") & (table["document_read"] == "Y")
                 & (table["old_nasdaq_last_session"] != "") & (table["first_new_session"] != "")]
    for sid, a, b in zip(rows["security_id"], rows["old_nasdaq_last_session"], rows["first_new_session"]):
        if a < b:
            out[sid].append((a, b))
    return dict(out)


def relist_gap_weeks(listed: pd.DataFrame, gaps: dict | None) -> np.ndarray:
    """Weeks with no canonical close that end after the old shares' last Nasdaq session and before the new
    shares' first session of a documented relist junction (``relist_gaps``): nothing traded on Nasdaq in
    them (WW: suspended 2025-05-16, new shares from 2025-06-27), so they are outside trading, not missing.
    A week with a close (an OTC row step 9 kept, CEPL 2023-10) is left as it is."""
    out = np.zeros(len(listed), dtype=bool)
    if not gaps or not len(listed):
        return out
    days = listed["week_end"].dt.strftime("%Y-%m-%d").to_numpy(dtype="U10")
    no_close = np.isnan(listed["close"].to_numpy(dtype=float)) if "close" in listed else np.ones(len(listed), bool)
    frame = pd.DataFrame({"s": listed["security_id"].to_numpy(dtype=object)})
    for sid, index in frame.groupby("s").indices.items():
        for a, b in gaps.get(sid, []):
            out[index] |= (days[index] > a) & (days[index] < b)
    return out & no_close


FETCH_STATUS_COLUMNS = ["security_id", "status", "first_date", "last_date", "fetched_utc", "updated_utc"]


def yahoo_answers(report: pd.DataFrame | None, status: pd.DataFrame | None) -> pd.DataFrame:
    """The Yahoo run's answers in the shape of the Tiingo fetch status (FETCH_STATUS_COLUMNS): ``done``
    for a usable verdict (ok, review, partial), ``no_data`` for no_rows / failed, with the answer's first
    and last row and the time its symbol was fetched; so a Yahoo answer newer than the panel makes its
    missing weeks ``fetched_pending_reconcile`` as a Tiingo answer does."""
    if report is None or not len(report):
        return pd.DataFrame(columns=FETCH_STATUS_COLUMNS)
    fetched = {}
    if status is not None and len(status):
        latest = status.sort_values("fetched_utc", kind="stable").drop_duplicates("symbol", keep="last")
        fetched = dict(zip(latest["symbol"], latest["fetched_utc"]))
    when = report["symbol"].map(fetched).fillna("")
    out = pd.DataFrame({"security_id": report["security_id"].values,
                        "status": np.where(report["verdict"].isin(YAHOO_EMPTY), "no_data", "done"),
                        "first_date": report["first_row"].values, "last_date": report["last_row"].values,
                        "fetched_utc": when.values})
    out["updated_utc"] = out["fetched_utc"]
    return out


def yahoo_empty_ids(report: pd.DataFrame | None) -> set[str]:
    """Securities whose Yahoo answer had no usable rows in the need (entity-report verdict no_rows or
    failed, e.g. WOLF: Yahoo's history starts at the 2025 reorganisation)."""
    if report is None or not len(report) or "verdict" not in report:
        return set()
    empty = report["verdict"].isin(YAHOO_EMPTY).groupby(report["security_id"]).all()
    return set(empty.index[empty.to_numpy()])


PENDING_SOURCE = {"tiingo_pending": "tiingo", "yahoo_pending": "yahoo"}  # pending reasons tied to a candidate row
WINDOW_WEEK_DAYS = 6  # a need window covers a week when it reaches into the week's seven days


def need_windows(candidates: pd.DataFrame, source: str) -> dict[str, list[tuple[str, str]]]:
    """security -> the needed windows (needed_start..needed_end, blank = open) of its candidate rows
    planned for ``source``."""
    rows = candidates[candidates["planned_source"] == source]
    blank = pd.Series("", index=rows.index)
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for sid, a, b in zip(rows["security_id"], rows.get("needed_start", blank).fillna(""),
                         rows.get("needed_end", blank).fillna("")):
        out[sid].append((a or "1900-01-01", b or "2100-01-01"))
    return dict(out)


def in_need_window(security, days, windows: dict) -> np.ndarray:
    """True where a window of the security reaches into the week ending on that day (YYYY-MM-DD)."""
    days = np.asarray(days, dtype="U10")
    starts = (pd.to_datetime(pd.Series(days)) - pd.Timedelta(days=WINDOW_WEEK_DAYS)).dt.strftime("%Y-%m-%d").to_numpy(
        dtype="U10") if len(days) else days
    out = np.zeros(len(days), dtype=bool)
    frame = pd.DataFrame({"s": np.asarray(security, dtype=object)})
    for sid, index in frame.groupby("s").indices.items():
        spans = windows.get(sid)
        if not spans:
            continue
        hit = np.zeros(len(index), dtype=bool)
        for a, b in spans:
            hit |= (days[index] >= a) & (starts[index] <= b)
        out[index] = hit
    return out


class MissingReasons:
    """Why a listed name-week may lack a canonical rank (see MISSING_REASONS).

    Each security has a chain of reasons in precedence order: tiingo_pending (a Tiingo candidate the fetch
    has not answered), unfillable (unfillable.csv, or every planned source answered without rows: Tiingo
    wrong_entity / no_data, Yahoo no_rows / failed, even with part of a series: Wolfspeed), yahoo_pending (a
    Yahoo candidate with no entity-report row), series_gap, no_vendor_source, tiingo_pending (step 9's
    no-series reason), then candidate_other / not_candidate. A pending reason holds only in the weeks a
    candidate row of its planned source needs (``needed_start``..``needed_end``); other weeks take the
    next reason of the chain (CORZ: the Tier-C Tiingo row for CORZQ needs 2021-11..2023-01, the Yahoo row
    from 2023-12). ``get`` gives a security's first reason that holds in some week (the security view the
    checks use), ``on`` the reason on one day, ``weekly`` the reason of each (security, week end) row."""

    def __init__(self, master: pd.DataFrame, candidates: pd.DataFrame, unfillable: pd.DataFrame,
                 no_series: pd.DataFrame, fetch_status: pd.DataFrame, series_ids: set,
                 yahoo_report: pd.DataFrame | None = None):
        status = {}
        if len(fetch_status):
            latest = fetch_status.sort_values("updated_utc", kind="stable").drop_duplicates("security_id", keep="last")
            status = dict(zip(latest["security_id"], latest["status"]))
        self.windows = {reason: need_windows(candidates, source) for reason, source in PENDING_SOURCE.items()}
        tiingo, yahoo = set(self.windows["tiingo_pending"]), set(self.windows["yahoo_pending"])
        answered = set(yahoo_report["security_id"]) if yahoo_report is not None and len(yahoo_report) else set()
        candidate = set(candidates["security_id"])
        unfill = set(unfillable["security_id"])
        yahoo_empty = yahoo_empty_ids(yahoo_report)
        no_series_reason = dict(zip(no_series["security_id"], no_series["reason"])) if len(no_series) else {}
        self.chains: dict[str, list[str]] = {}
        for sid in master["security_id"]:
            fetched, ns = status.get(sid, ""), no_series_reason.get(sid, "")
            chain = []
            if sid in tiingo and fetched not in FETCH_FINAL and sid not in unfill:
                chain.append("tiingo_pending")
            if sid in unfill or fetched in FETCH_EMPTY or ns == "unfillable" or sid in yahoo_empty:
                chain.append("unfillable")
            if sid in yahoo and sid not in answered:
                chain.append("yahoo_pending")
            if sid in series_ids:
                chain.append("series_gap")
            if ns == "no_vendor_source":
                chain.append("no_vendor_source")
            if ns == "tiingo_pending" and "tiingo_pending" not in chain:
                chain.append("tiingo_pending")
            chain.append("candidate_other" if sid in candidate else "not_candidate")
            self.chains[sid] = chain

    def _holds(self, reason: str, sid: str) -> bool:
        return reason not in PENDING_SOURCE or bool(self.windows[reason].get(sid))

    def get(self, sid: str, default: str = "not_candidate") -> str:
        for reason in self.chains.get(sid, []):
            if self._holds(reason, sid):
                return reason
        return default

    def items(self):
        return ((sid, self.get(sid)) for sid in self.chains)

    def as_dict(self) -> dict[str, str]:
        return dict(self.items())

    def on(self, sid: str, day: str) -> str:
        for reason in self.chains.get(sid, []):
            if reason not in PENDING_SOURCE or in_need_window([sid], [day], self.windows[reason])[0]:
                return reason
        return self.get(sid)

    def weekly(self, security, days) -> np.ndarray:
        """The reason of each (security, YYYY-MM-DD week end) pair."""
        security = np.asarray(security, dtype=object)
        days = np.asarray(days, dtype="U10")
        out = np.full(len(security), "", dtype=object)
        open_ = np.ones(len(security), dtype=bool)
        chains = pd.Series([self.chains.get(s, ["not_candidate"]) for s in security], dtype=object)
        depth = max((len(c) for c in self.chains.values()), default=1)
        for level in range(depth):
            reason = np.array([c[level] if level < len(c) else "" for c in chains], dtype=object)
            for name in set(reason[open_]) - {""}:
                rows = open_ & (reason == name)
                if name in PENDING_SOURCE:
                    rows &= in_need_window(security, days, self.windows[name])
                out[rows] = name
                open_ &= ~rows
            if not open_.any():
                break
        out[open_] = "not_candidate"
        return out


def security_reasons(master: pd.DataFrame, candidates: pd.DataFrame, unfillable: pd.DataFrame,
                     no_series: pd.DataFrame, fetch_status: pd.DataFrame, series_ids: set,
                     yahoo_report: pd.DataFrame | None = None) -> dict[str, str]:
    """security -> why a listed week of it may lack a canonical rank (the security view of
    ``MissingReasons``)."""
    return MissingReasons(master, candidates, unfillable, no_series, fetch_status, series_ids, yahoo_report).as_dict()


def week_cutoffs(listed: pd.DataFrame) -> pd.DataFrame:
    """Per week: canonical dv50 at ranks 250 and 300, step 6's at its ranks 250 and 300, and the median
    market cap / float of canonical ranks 200-250 (plan 3.3 check 1)."""
    rows = listed
    by = lambda mask, column: rows.loc[mask].groupby("week_index")[column]
    weeks = pd.Index(sorted(rows["week_index"].unique()), name="week_index")
    cut = pd.DataFrame(index=weeks)
    for rank in (TOP_N, PRICE_RANK):
        cut[f"cut{rank}"] = by(rows["dv50_rank"] == rank, "dv50").first()
        cut[f"cut{rank}_dv20"] = by(rows["dv20_rank"] == rank, "dv20").first()
        cut[f"pf_cut{rank}"] = by(rows["pf_dv50_rank"] == rank, "pf_dv50").first()
    band = rows["dv50_rank"].between(*BAND)
    cut["cut_mcap"] = by(band, "mcap").median()
    cut["cut_float"] = by(band, "float_usd").median()
    return cut


def proxy_ratios(frame: pd.DataFrame, cut: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(market cap / the week's band median market cap, float / the band median float), NaN without both."""
    c = cut.reindex(frame["week_index"].values)
    mcap = frame["mcap"].to_numpy(dtype=float)
    flt = frame["float_usd"].to_numpy(dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        return mcap / c["cut_mcap"].to_numpy(dtype=float), flt / c["cut_float"].to_numpy(dtype=float)


def proxy_ratio_of(frame: pd.DataFrame, cut: pd.DataFrame) -> np.ndarray:
    """The larger of market cap / the week's band median market cap and float / the band median float
    (either one where only one exists; NaN without both): the ratio whose bins the proxy rule's hit rates
    are measured on, the same way for the calibration population and the missing names. It is the either-one
    reading of ``proxy_above`` (ratio >= 1 exactly when ``proxy_above``), so a float-only week (a carried
    market cap under the median, a float at or above it: INO 2020-01-03) is binned by its float, not by the
    stale market cap (round-8 review). ``proxy_ratio_mcap_first`` keeps the plan's market-cap-first ratio;
    the expected count with it is reported beside check 6 (``market_cap_first_binning``)."""
    by_mcap, by_float = proxy_ratios(frame, cut)
    return np.fmax(by_mcap, by_float)


def proxy_ratio_mcap_first(frame: pd.DataFrame, cut: pd.DataFrame) -> np.ndarray:
    """Market cap / the week's band median market cap, or, without both, float / the band median float
    (the plan's market-cap-first ratio, the binning of builds before round 8's fix)."""
    by_mcap, by_float = proxy_ratios(frame, cut)
    return np.where(~np.isnan(by_mcap), by_mcap, by_float)


def proxy_above(frame: pd.DataFrame, cut: pd.DataFrame) -> np.ndarray:
    """Market cap at or above the week's band median, or float at or above the band median float, either
    one: a market cap below the median (often carried up to 12 months from an old company list) does not
    outweigh a float that reaches it (round-6 review, INO 2020: a $0.24B cap from 2019-06 against a $4.2B
    float). The same rule as validate's ``universe_proxy_margin``; ``proxy_above_mcap_first`` keeps the
    plan's market-cap-first reading."""
    by_mcap, by_float = proxy_ratios(frame, cut)
    with np.errstate(invalid="ignore"):
        return (by_mcap >= 1.0) | (by_float >= 1.0)


def proxy_above_mcap_first(frame: pd.DataFrame, cut: pd.DataFrame) -> np.ndarray:
    """The plan's reading of check 1: the market cap at or above the band median, or, without one (or
    without a band median of market caps), the float at or above the band median float."""
    ratio = proxy_ratio_mcap_first(frame, cut)
    with np.errstate(invalid="ignore"):
        return ~np.isnan(ratio) & (ratio >= 1.0)


def pending_reconcile(listed: pd.DataFrame, fetch_status: pd.DataFrame | None, panel_built: str,
                      newer: bool = True) -> np.ndarray:
    """Missing weeks inside a Tiingo or Yahoo answer (done / done_review / partial) fetched after the
    canonical panel was built: step 9 has not read it yet, so the week is pending, not lost. With
    ``newer`` False: inside an answer fetched before the panel was built, which step 9 read but did not
    put in the panel (``answer_not_in_panel``: SMCI and CHRD after their Form 25 cuts)."""
    mask = np.zeros(len(listed), dtype=bool)
    if fetch_status is None or not len(fetch_status) or not panel_built:
        return mask
    when = fetch_status["fetched_utc"].fillna("").astype(str)
    timing = (when > panel_built) if newer else ((when != "") & (when <= panel_built))
    ok = fetch_status[fetch_status["status"].isin(FETCH_FINAL - FETCH_EMPTY)
                      & timing & (fetch_status["first_date"] != "")]
    if ok.empty:
        return mask
    ranges = ok.groupby("security_id").agg(first=("first_date", "min"), last=("last_date", "max"))
    first = pd.to_datetime(listed["security_id"].map(ranges["first"]))
    last = pd.to_datetime(listed["security_id"].map(ranges["last"]))
    inside = (listed["week_end"] >= first) & (listed["week_end"] <= last + pd.Timedelta(days=CLOSE_STALE_SESSIONS))
    return (listed["missing"] & inside.fillna(False)).to_numpy(dtype=bool)


def windows_of(unfillable: pd.DataFrame) -> dict[str, list[tuple[str, str]]]:
    """security -> the needed windows ``unfillable.csv`` gives up on."""
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for sid, a, b in zip(unfillable["security_id"], unfillable["needed_start"], unfillable["needed_end"]):
        out[sid].append((a or "1900-01-01", b or "2100-01-01"))
    return dict(out)


def in_windows(listed: pd.DataFrame, windows: dict | None) -> np.ndarray:
    if not windows:
        return np.zeros(len(listed), dtype=bool)
    days = listed["week_end"].dt.strftime("%Y-%m-%d")
    return foreign_mask(listed["security_id"].to_numpy(dtype=object), days.to_numpy(dtype=object), windows)


NEW_LISTING_BASES = ("ipo_rule", "prospectus", "snapshot_gap")   # the first-run starts that are real new listings
PROSPECTUS_FORMS = {"424B4", "424B1"}  # a final IPO prospectus (Rule 424(b)(4) / (b)(1)), filed a day or two after pricing
PROSPECTUS_LEAD_SESSIONS = 2           # the first trade can be up to this many sessions before the prospectus is filed
PROSPECTUS_SLACK_DAYS = 7              # a prospectus this long before the last snapshot without the name still counts
# the security continues another one, or the issuer was public before: not a new listing
CONTINUING_BASES = ("successor_link", "older_issuer", "issuer_listed_before")
SUCCESSOR_LINK_DAYS = 10    # a successor link counts when the successor's listing starts this close to it
SERIES_START_SESSIONS = CLOSE_STALE_SESSIONS  # the canonical young rule: first row this close to the earliest start
ONE_TO_ONE = ("stock_merger", "reorganization", 1.0)  # step 11's booking of a 1:1 reorganisation or redomicile
PREDECESSOR_OVERLAP_DAYS = 30  # a predecessor listed this long past the successor's start continues itself
LINK_SOURCES = ("security_master", "step11")
LINK_COLUMNS = ["predecessor_id", "successor_id", "successor_date", "successor_start", "successor_first_row",
                "predecessor_end", "continuing", "continuing_basis", "one_to_one", "one_to_one_basis", "windows_cross",
                "handover", "link_source"]


def _one_to_one(t: dict | None, successor: str) -> tuple[bool, str]:
    """Whether step 11's row ``t`` books the end as a 1:1 reorganisation into ``successor`` (and why)."""
    if t is None:
        return False, "no step-11 row"
    shares = pd.to_numeric(pd.Series([t.get("consideration_shares", "")]), errors="coerce").iloc[0]
    acquirer = str(t.get("acquirer_security_id", "") or "")
    kind = (str(t.get("terminal_type", "") or ""), str(t.get("event_subtype", "") or ""))
    one = kind == ONE_TO_ONE[:2] and shares == ONE_TO_ONE[2] and acquirer in ("", successor)
    return bool(one), f"step 11: {kind[0] or '-'} / {kind[1] or '-'} / {'' if pd.isna(shares) else f'{shares:g}'} share"


def successor_links(master: pd.DataFrame, spans: pd.DataFrame, first_row: dict, terminal: pd.DataFrame | None = None,
                    days: int = SUCCESSOR_LINK_DAYS, overlap_days: int = PREDECESSOR_OVERLAP_DAYS) -> pd.DataFrame:
    """One row per successor link: each ``security_master`` link (the predecessor's ``successor_security_id``
    and ``successor_date``, the Form 25 Nasdaq filed for a reorganisation with a ticker handover;
    ``link_source`` security_master), and each 1:1 reorganisation step 11 books for a predecessor the master
    links to nothing (``link_source`` step11: terminal_type stock_merger, event_subtype reorganization,
    consideration_shares 1, acquirer_security_id a listed security that no master link names as a successor;
    the successor date is step 11's ``end_date``, else its ``delist_date``): Express Scripts -> Express
    Scripts Holding 2012-04, Marvell's Delaware redomicile 2021-04, Sinclair 2023, Zillow Group 2015.

    - ``continuing`` (``continuing_basis``): the successor's first listing span starts within ``days`` of the
      successor date (``successor_date``), or its first canonical row does (``first_row``), or the span
      starts within ``days`` of the predecessor's last listed day (``handover``: the ticker passes at a
      snapshot boundary, so QuidelOrtho is listed from 2022-06-07, the day after Quidel's last listed day,
      though the Form 25 is dated 2022-05-27; Express Scripts Holding from 2012-04-05, the day after the old
      shares' last listed day, though their Form 25 is dated 2012-04-22) -- and the predecessor's listing
      (``predecessor_end``: the end of its last listing run, renames joined, starting on or before the
      successor's start) ends no more than ``overlap_days`` after the successor's listing starts. A
      predecessor listed on for longer continues itself, so the successor is a new security
      (``predecessor_listed_on``: new IAC 2020-07, spun off while old IAC went on as Match Group to 2026;
      RCM 2022, 28 days of overlap, still continues).
      A continuing successor is no new listing (``new_listing_evidence`` basis ``successor_link``: not young
      by any rule);
    - ``one_to_one``: step 11 books the predecessor's end as a ``stock_merger`` / ``reorganization`` of one
      successor share per share (``terminal_returns_2012_2026.csv``: terminal_type, event_subtype,
      consideration_shares, and acquirer_security_id the successor or blank): Google -> Alphabet 2015,
      Apache -> APA 2021. Not AspenTech 2022 ($87.69 cash + 0.42 share), 21CF -> Fox 2019 (Disney's election),
      IAC 2020 (no step-11 row: old IAC went on as Match Group);
    - ``windows_cross``: continuing and one_to_one (owner convention of 2026-10-02, CRSP's single PERMNO): the
      successor's dv20 / dv50 windows read the predecessor's rows on the sessions before ``handover`` (the
      successor's first listed day) and its own rows from then on, one row per session
      (``link_successor_rows``)."""
    if len(spans):
        start = spans.groupby("security_id")["list_start"].min().to_dict()
        # listing runs (a rename joins the spans: old IAC's run goes on as MTCH to 2026)
        joined = spans.assign(run_start=run_starts(spans)).groupby(["security_id", "run_start"])["list_end"].max()
        runs = defaultdict(list)
        for (sid, a), b in joined.items():
            runs[sid].append((a, b))
    else:
        start, runs = {}, {}
    booked = {}
    if terminal is not None and len(terminal) and "security_id" in terminal:
        booked = terminal.drop_duplicates("security_id", keep="last").set_index("security_id").to_dict(orient="index")
    pairs = []
    if "successor_security_id" in master:
        linked = master[master["successor_security_id"].fillna("").astype(str) != ""]
        dates = linked["successor_date"] if "successor_date" in linked else pd.Series("", index=linked.index)
        pairs = [(p, str(s), str(d or ""), LINK_SOURCES[0])
                 for p, s, d in zip(linked["security_id"], linked["successor_security_id"], dates.fillna(""))]
    # a step-11 link only where no link leaves the predecessor and none reaches the successor (a chain
    # through a master successor, Tessera -> Xperi -> Xperi Holding, is fine)
    leaving, reaching = {p for p, _, _, _ in pairs}, {s for _, s, _, _ in pairs}
    for p, t in booked.items():
        s = str(t.get("acquirer_security_id", "") or "")
        if not s or s == p or p in leaving or s in reaching or s not in start or not _one_to_one(t, s)[0]:
            continue
        day = str(t.get("end_date", "") or "") or str(t.get("delist_date", "") or "")
        pairs.append((p, s, day, LINK_SOURCES[1]))
        leaving.add(p)
        reaching.add(s)
    near = lambda a, b: bool(a and b) and abs((pd.Timestamp(a) - pd.Timestamp(b)).days) <= days

    def predecessor_end(p: str, s_start: str) -> str:
        """The end of the predecessor's last listing run starting on or before the successor's start."""
        before = [(a, b) for a, b in runs.get(p, []) if not s_start or a <= s_start]
        return max(before)[1] if before else ""

    rows = []
    for p, s, day, source in pairs:
        s_start, s_first = start.get(s, ""), first_row.get(s, "")
        p_end = predecessor_end(p, s_start)
        if not s_start:
            basis = "successor_not_listed"
        elif p_end and (pd.Timestamp(p_end) - pd.Timestamp(s_start)).days > overlap_days:
            basis = "predecessor_listed_on"
        elif near(s_start, day):
            basis = "successor_date"
        elif near(s_first, day):
            basis = "first_row"
        elif near(s_start, p_end):
            basis = "handover"
        else:
            basis = ""
        one, why = _one_to_one(booked.get(p), s)
        continuing = basis in ("successor_date", "first_row", "handover")
        rows.append({"predecessor_id": p, "successor_id": s, "successor_date": day, "successor_start": s_start,
                     "successor_first_row": s_first, "predecessor_end": p_end, "continuing": continuing,
                     "continuing_basis": basis, "one_to_one": one, "one_to_one_basis": why,
                     "windows_cross": bool(continuing and one), "handover": s_start, "link_source": source})
    return pd.DataFrame(rows, columns=LINK_COLUMNS)


def link_successor_rows(dv: np.ndarray, ids: np.ndarray, sessions: pd.DatetimeIndex,
                        links: pd.DataFrame | None) -> dict:
    """Run the dollar-volume windows across each ``windows_cross`` successor link (owner convention: a 1:1
    reorganisation continues the security): on the sessions before the handover (the successor's first
    listed day) the successor's column takes the predecessor's dollar volume where the predecessor has a row
    (its own rows there, if any, are kept only where the predecessor has none); from the handover on only
    its own rows count. One value per session, so no day is counted twice. ``dv`` (sessions x ids) is
    changed in place; links are applied in handover order, so a chain (Tessera -> Xperi -> Xperi Holding)
    carries through. Returns successor -> facts."""
    out: dict[str, dict] = {}
    if links is None or not len(links) or "windows_cross" not in links:
        return out
    column = {s: k for k, s in enumerate(ids)}
    window = max(DV_WINDOWS)
    for row in links[links["windows_cross"].astype(bool)].sort_values("handover", kind="stable").itertuples(index=False):
        k, j = column.get(row.successor_id), column.get(row.predecessor_id)
        facts = {"predecessor": row.predecessor_id, "handover": row.handover, "predecessor_rows_in_first_window": 0,
                 "own_rows_replaced": 0, "predecessor_rows_on_or_after_handover_unused": 0}
        if k is None or j is None:
            facts["note"] = "no canonical rows for the " + ("successor" if k is None else "predecessor")
            out[row.successor_id] = facts
            continue
        pos = int(sessions.searchsorted(pd.Timestamp(row.handover)))
        theirs = ~np.isnan(dv[:pos, j])
        facts["own_rows_replaced"] = int((theirs & ~np.isnan(dv[:pos, k])).sum())
        dv[:pos, k] = np.where(theirs, dv[:pos, j], dv[:pos, k])
        facts["predecessor_rows_in_first_window"] = int(theirs[max(pos - window, 0):].sum())
        facts["predecessor_rows_on_or_after_handover_unused"] = int((~np.isnan(dv[pos:, j])).sum())
        out[row.successor_id] = facts
    return out


def new_listing_evidence(spans: pd.DataFrame, master: pd.DataFrame, sessions: pd.DatetimeIndex,
                         prospectus: dict | None = None, issuer_days: int = YOUNG_ISSUER_DAYS,
                         links: pd.DataFrame | None = None, first_row: dict | None = None) -> pd.DataFrame:
    """Per security, whether its first listing run is a real new listing and the earliest XNAS session it
    can have begun trading on (``young_weeks`` rule 1; the snapshot start alone is not that evidence).

    ``new_listing_basis`` of the security's first span:
    - ``successor_link``: the security is the successor of a ``continuing`` successor link
      (``successor_links``: a master link, Alphabet 2015-10, AspenTech 2022-06, or a step-11 1:1
      reorganisation the master does not link, Express Scripts Holding 2012-04, Marvell 2021-04): it
      continues the predecessor, so it is no new listing (``predecessor_security_id`` names it);
    - ``older_issuer``: the issuer filed a 10-K / 10-Q (``security_master.domestic_periodic_first``) more
      than ``issuer_days`` before the listing started: a public company before (a transfer from NYSE such
      as NBL 2020 or LSI 2013, a reclassified tracking stock such as QVCA 2014 or QRTEA 2018, VIACA 2019);
    - ``issuer_listed_before``: another security of the same CIK was listed on Nasdaq up to ``issuer_days``
      before this start (a tracking-stock reorganisation under a new security_id: LINTA -> QVCA);
    - ``ipo_rule``: the span starts at its first canonical price (the IPO rule): that day;
    - ``prospectus``: a snapshot start with a final IPO prospectus (424B4 / 424B1, from the cached SEC
      submissions: ``prospectus`` = CIK -> filing dates) filed between a week before the last snapshot
      without it and the start: the first trade is at most PROSPECTUS_LEAD_SESSIONS sessions before the
      filing, so the earliest start is the later of that session and the session after the last absent
      snapshot;
    - ``snapshot_gap``: the start is a snapshot date, so the listing may have begun on any session after
      the last snapshot without it (``start_prev_absent``): the first session after it is the earliest
      start (VWR, IPO 2014-10-02, first in a snapshot of 2014-11-21, absent from 2014-09-21);
    - ``no_earlier_snapshot``: no snapshot before shows it absent, so nothing dates the start.
    ``new_listing`` is True for ``ipo_rule``, ``prospectus`` and ``snapshot_gap`` only; ``earliest_start`` is
    blank for the others. ``series_start_gap``: XNAS sessions from ``earliest_start`` to the security's first
    canonical row (``first_row``; NaN without both); ``series_starts_listing``: a new listing whose canonical
    series starts within SERIES_START_SESSIONS of its earliest start, so the series holds the listing's first
    sessions (the canonical young rule's condition; not GoPro 2014, whose rows start 8 sessions after the
    earliest start its IPO prospectus gives, nor a snapshot start whose series begins weeks after it)."""
    columns = ["security_id", "earliest_start", "new_listing", "new_listing_basis", "listing_continues",
               "predecessor_security_id", "series_start_gap", "series_starts_listing"]
    if not len(spans):
        return pd.DataFrame(columns=columns)
    values = sessions.strftime("%Y-%m-%d").to_numpy(dtype=object)
    next_session = lambda day: values[min(np.searchsorted(values, day, "right"), len(values) - 1)]
    lead = lambda day: values[min(max(int(np.searchsorted(values, day, "left")) - PROSPECTUS_LEAD_SESSIONS, 0),
                                  len(values) - 1)]
    prospectus = prospectus or {}
    info = master.set_index("security_id")
    periodic = info["domestic_periodic_first"].to_dict() if "domestic_periodic_first" in info else {}
    cik = info["cik"].to_dict() if "cik" in info else {}
    by_cik = defaultdict(list)
    for sid, a, b in zip(spans["security_id"], spans["list_start"], spans["list_end"]):
        if cik.get(sid, ""):
            by_cik[cik[sid]].append((sid, a, b))
    first = spans.sort_values(["security_id", "list_start"], kind="stable").drop_duplicates("security_id")
    predecessor: dict[str, str] = {}
    if links is not None and len(links):
        for p, s, ok in zip(links["predecessor_id"], links["successor_id"], links["continuing"].astype(bool)):
            if ok and s not in predecessor:
                predecessor[s] = p
    first_row = first_row or {}
    position = lambda day: int(np.searchsorted(values, day, "left"))
    rows = []
    for row in first.itertuples(index=False):
        sid, start = row.security_id, row.list_start
        since = (pd.Timestamp(start) - pd.Timedelta(days=issuer_days)).strftime("%Y-%m-%d")
        filed = periodic.get(sid, "")
        before = [o for o, a, b in by_cik.get(cik.get(sid, ""), []) if o != sid and a < start and b >= since]
        earliest = ""
        if sid in predecessor:
            basis = "successor_link"
        elif filed and filed < since:
            basis = "older_issuer"
        elif before:
            basis = "issuer_listed_before"
        elif getattr(row, "ipo_start", False):
            basis, earliest = "ipo_rule", start
        elif getattr(row, "start_prev_absent", ""):
            basis, earliest = "snapshot_gap", next_session(row.start_prev_absent)
            low = (pd.Timestamp(row.start_prev_absent) - pd.Timedelta(days=PROSPECTUS_SLACK_DAYS)).strftime("%Y-%m-%d")
            filed = [d for d in prospectus.get(cik.get(sid, ""), []) if low <= d <= start]
            if filed:
                basis, earliest = "prospectus", max(earliest, lead(min(filed)))
        else:
            basis = "no_earlier_snapshot"
        series = first_row.get(sid, "")
        gap = position(series) - position(earliest) if series and earliest else np.nan
        new = basis in NEW_LISTING_BASES
        rows.append({"security_id": sid, "earliest_start": earliest, "new_listing": new,
                     "new_listing_basis": basis, "listing_continues": basis in CONTINUING_BASES,
                     "predecessor_security_id": predecessor.get(sid, ""), "series_start_gap": gap,
                     "series_starts_listing": bool(new and not np.isnan(gap) and abs(gap) <= SERIES_START_SESSIONS)})
    return pd.DataFrame(rows, columns=columns)


def attach_new_listing(listed: pd.DataFrame, spans: pd.DataFrame, newness: pd.DataFrame,
                       sessions: pd.DatetimeIndex, week_pos: np.ndarray) -> pd.DataFrame:
    """Add the young-rule inputs to ``listed``: ``new_listing``, ``new_listing_basis``, ``listing_continues``,
    ``earliest_start``, ``predecessor_security_id`` and ``series_starts_listing`` (``new_listing_evidence``),
    ``first_listing_run`` (the row's listing run is the security's first) and ``listing_sessions``: XNAS
    sessions from ``earliest_start`` to the week end (NaN without an earliest start). Changes ``listed`` in
    place."""
    evidence = newness.set_index("security_id")
    for column in ("earliest_start", "new_listing_basis", "predecessor_security_id"):
        values = evidence[column] if column in evidence else pd.Series(dtype=object)
        listed[column] = listed["security_id"].map(values).fillna("")
    for column in ("new_listing", "listing_continues", "series_starts_listing"):
        values = evidence[column] if column in evidence else pd.Series(dtype=bool)
        listed[column] = listed["security_id"].map(values).fillna(False).astype(bool)
    earliest = pd.to_datetime(listed["earliest_start"].replace("", None))
    first = np.where(earliest.notna(), sessions.searchsorted(earliest.fillna(sessions[0])), -1)
    listed["listing_sessions"] = np.where(first >= 0, week_pos[listed["week_index"].to_numpy()] - first + 1, np.nan)
    listed["first_listing_run"] = listed["listing_start"] == listed["security_id"].map(
        spans.groupby("security_id")["list_start"].min())
    return listed


def young_rules(frame: pd.DataFrame) -> np.ndarray:
    """The young rule each name-week meets ("" for none; see ``young_weeks``)."""
    days = lambda column: (frame["week_end"] - pd.to_datetime(frame[column].replace("", None))).dt.days
    flag = lambda column, default: (frame[column].fillna(default).to_numpy(dtype=bool) if column in frame
                                    else np.full(len(frame), default, dtype=bool))
    has_close = ~np.isnan(frame["close"].to_numpy(dtype=float))
    no_dv = np.isnan(frame["dv50"].to_numpy(dtype=float))
    first = "segment_first_row" if "segment_first_row" in frame else "first_row"   # restarts at a relist junction
    if "segment_junction" in frame:
        junction = flag("segment_junction", False)
    elif {"segment_first_row", "first_row"} <= set(frame.columns):
        junction = (frame["segment_first_row"].astype(str) != frame["first_row"].astype(str)).to_numpy(dtype=bool)
    else:
        junction = np.zeros(len(frame), bool)
    # Only where the segment starts at a relist junction or the series starts a real new listing: a series
    # that begins at a transfer from NYSE (KDP, HST) or at a successor link (Alphabet) is no new listing.
    starts = junction | flag("series_starts_listing", False)
    canonical = has_close & no_dv & starts & (days(first) <= YOUNG_DAYS).fillna(False).to_numpy(dtype=bool)
    pf_no_dv = np.isnan(frame["pf_dv50"].to_numpy(dtype=float)) if "pf_dv50" in frame else np.ones(len(frame), bool)
    minimum = DV_WINDOWS[max(DV_WINDOWS)]
    first_sessions = ((frame["listing_sessions"] < minimum).fillna(False).to_numpy(dtype=bool)
                      if "listing_sessions" in frame else np.zeros(len(frame), bool))
    first_sessions = first_sessions & flag("first_listing_run", True) & flag("new_listing", False)
    short = np.zeros(len(frame), bool)
    if {"pf_n50", "pf_first_data", "listing_start"} <= set(frame.columns):
        n50 = frame["pf_n50"]
        data_start = pd.to_datetime(frame["pf_first_data"])
        listing_start = pd.to_datetime(frame["listing_start"].replace("", None))
        new_series = (frame["week_end"] - data_start).dt.days <= YOUNG_DAYS
        new_listing = (data_start - listing_start).dt.days <= YOUNG_DAYS
        short = ((n50 > 0) & (n50 < minimum) & new_series & new_listing).fillna(False).to_numpy(dtype=bool)
        short = short & ~flag("listing_continues", False)
    unpriced = no_dv & pf_no_dv
    return np.select([canonical, unpriced & first_sessions, unpriced & short],
                     ["canonical", "new_listing", "short_series"], default="")


def young_weeks(frame: pd.DataFrame) -> np.ndarray:
    """New listings still short of 25 sessions (not missing), three rules:
    - ``canonical``: a canonical close without a dv50 within YOUNG_DAYS of the first canonical row of its
      segment (``segment_first_row``: the last relist junction on or before the week, where the windows
      restart, else the security's first row), only where that segment starts at a relist junction
      (``segment_junction``) or the security is a real new listing whose series starts within
      SERIES_START_SESSIONS of its earliest start (``series_starts_listing``, see ``new_listing_evidence``);
      a transfer from NYSE whose rows start at the Nasdaq start (KDP 2020-09, HST 2020-11), a successor link
      (Alphabet 2015-10, ``successor_link``) or a series that starts weeks after the listing could have
      begun stays missing, so its proxy or step-6 evidence speaks for the week;
    - with no dv50 from step 6 either, ``new_listing``: the security's first listing run
      (``first_listing_run``; a later run after a gap in the snapshots, such as Ur-Energy's 2015-2017
      runs, is no new listing) whose start is a real new listing (``new_listing``, see
      ``new_listing_evidence``: the IPO rule, or a snapshot start of an issuer not public before), in its
      first 25 sessions counted from the earliest day it can have begun trading (``listing_sessions``:
      XNAS sessions from ``earliest_start`` to the week end). A transfer from NYSE, a reorganised tracking
      stock or an issuer without a dated start stays missing (NBL, LSI, QVCA, QRTEB, VIACA); a snapshot
      that came weeks after the IPO counts from the session after the last snapshot without it (VWR, ZU,
      BIVV, MRD);
    - with no dv50 from step 6 either, ``short_series``: step 6 holding 1 to 24 dollar-volume rows in the
      50-session window (``pf_n50``) of a series it first holds within YOUNG_DAYS (``pf_first_data``) of a
      listing run that started at most YOUNG_DAYS before that series, of an issuer not public before
      (``listing_continues`` False; a SPAC's shares trade weeks after its units list: IRHO, RACC).
    A name step 6 holds no row for (n50 = 0: Altaba after 2017-06) or whose data merely starts late in an
    old listing is not young; nor is the successor of a continuing successor link
    (``successor_link``, by any rule: AspenTech 2022, Alphabet 2015, whose 1:1 link also runs its windows
    across, so it ranks from its first week)."""
    return young_rules(frame) != ""


def reasons_of_weeks(frame: pd.DataFrame, reasons) -> np.ndarray:
    """The missing reason of each row: per week from a ``MissingReasons``, else per security (a dict)."""
    if hasattr(reasons, "weekly"):
        return reasons.weekly(frame["security_id"].to_numpy(dtype=object),
                              frame["week_end"].dt.strftime("%Y-%m-%d").to_numpy(dtype=object))
    return frame["security_id"].map(reasons).fillna("not_candidate").to_numpy(dtype=object)


def mark_missing(listed: pd.DataFrame, spans: pd.DataFrame, reasons, cut: pd.DataFrame,
                 fetch_status: pd.DataFrame | None = None, panel_built: str = "",
                 unfillable_windows: dict | None = None, gaps: dict | None = None) -> pd.DataFrame:
    """Flag missing name-weeks and their evidence. ``reasons`` is a ``MissingReasons`` (a pending reason
    holds only inside the need windows of its candidate rows) or a security -> reason dict; a week inside
    an ``unfillable.csv`` window is ``unfillable`` and one inside a Tiingo answer newer than the panel is
    ``fetched_pending_reconcile``, whatever the security's reason; a week with a canonical close whose
    reason would be ``series_gap`` or ``answer_not_in_panel`` is ``short_window`` (the window is short, no
    row is lacking). ``gaps`` (``relist_gaps``) puts the weeks between a documented relist junction's old
    and new shares outside trading. ``young_any`` is the new-listing test without the universe-base
    condition (for the report on the investment companies left out)."""
    out = listed.copy()
    out["relist_gap"] = relist_gap_weeks(out, gaps)
    out["outside_trading"] = trading_bounds(out, spans).to_numpy(dtype=bool) | out["relist_gap"].to_numpy()
    ranked_any = out["dv50_rank_any_price"].notna().values
    out["young_rule"] = young_rules(out)
    young = (out["young_rule"] != "").to_numpy()
    out["young_any"] = young
    out["young"] = young & out["eligible"].values
    out["missing"] = out["eligible"].values & ~ranked_any & ~out["outside_trading"].values & ~young
    # A canonical close under $10 with no median is not missing: the $10 test already excludes it.
    out.loc[out["missing"] & (out["price_ge_10"] == "N"), "missing"] = False
    out["missing_reason"] = ""
    rows = out.index[out["missing"].to_numpy(dtype=bool)]
    if len(rows):
        out.loc[rows, "missing_reason"] = reasons_of_weeks(out.loc[rows, ["security_id", "week_end"]], reasons)
    out.loc[in_windows(out, unfillable_windows) & out["missing"], "missing_reason"] = "unfillable"
    out.loc[pending_reconcile(out, fetch_status, panel_built), "missing_reason"] = "fetched_pending_reconcile"
    in_hand = (pending_reconcile(out, fetch_status, panel_built, newer=False)
               & out["missing_reason"].isin(IN_HAND_OVERRIDES).to_numpy())
    out.loc[in_hand, "missing_reason"] = "answer_not_in_panel"
    with np.errstate(invalid="ignore"):
        short = (out["missing"].to_numpy(dtype=bool) & ~np.isnan(out["close"].to_numpy(dtype=float))
                 & np.isnan(out["dv50"].to_numpy(dtype=float))
                 & out["missing_reason"].isin(SHORT_WINDOW_FROM).to_numpy())
    out.loc[short, "missing_reason"] = "short_window"
    c = cut.reindex(out["week_index"].values)
    pf_ok = ~np.isnan(out["pf_dv50"].values) & out["pf_price"].isin(["Y", "U"]).values
    with np.errstate(invalid="ignore"):
        out["pf_ge_cut250"] = pf_ok & (out["pf_dv50"].values >= c["cut250"].values)
        out["pf_ge_cut300"] = pf_ok & (out["pf_dv50"].values >= c["cut300"].values)
    out["pf_dv_ok"] = pf_ok
    out["pf_price_low"] = (out["pf_price"] == "N").to_numpy()
    out["proxy_above"] = proxy_above(out, cut)
    out["proxy_above_float_only"] = out["proxy_above"].to_numpy() & ~proxy_above_mcap_first(out, cut)
    out["dv_ratio"], out["proxy_ratio"] = evidence_ratios(out, cut)
    out["evidence"] = np.where(out["missing"].to_numpy(dtype=bool), evidence_class(out), "")
    out["sibling_priced"] = sibling_priced(out)
    out["class_dv_ratio"], out["class_dv_weeks_away"] = class_dv_ratios(out, cut)
    with np.errstate(invalid="ignore"):
        out["proxy_class_capped"] = ((out["evidence"] == "proxy").to_numpy()
                                     & (out["class_dv_ratio"].to_numpy(dtype=float) < CLASS_DV_CAP_RATIO))
    return out


def sibling_priced(listed: pd.DataFrame) -> np.ndarray:
    """Missing classes of a multi-class company whose other class has a canonical rank that week (the
    owner's open question 8.4: if only the most liquid class is kept, these do not matter)."""
    if "multi_class" not in listed or "cik" not in listed:
        return np.zeros(len(listed), dtype=bool)
    ranked = listed["dv50_rank_any_price"].notna().astype(int)
    total = ranked.groupby([listed["week_index"].values, listed["cik"].values]).transform("sum")
    multi = listed["multi_class"].to_numpy(dtype=bool)
    return multi & listed["missing"].to_numpy(dtype=bool) & ((total - ranked).to_numpy() > 0)


# ------------------------------------------------------------------ calibration and estimates

def rule_rates(rows: pd.DataFrame, predicted: np.ndarray, actual: np.ndarray, min_n: int = 50) -> dict:
    """P(actual | predicted) and P(actual | not predicted) by year (all years pooled where a year has
    fewer than ``min_n`` predicted rows)."""
    years = rows["week_end"].dt.year.values
    pooled_hit = actual[predicted].mean() if predicted.any() else 0.0
    pooled_miss = actual[~predicted].mean() if (~predicted).any() else 0.0
    rates = {"pooled": {"n_pred": int(predicted.sum()), "p_given_pred": float(pooled_hit),
                        "n_not_pred": int((~predicted).sum()), "p_given_not_pred": float(pooled_miss)}}
    for year in sorted(set(years)):
        sel = years == year
        p, a = predicted[sel], actual[sel]
        hit = a[p].mean() if p.sum() >= min_n else pooled_hit
        miss = a[~p].mean() if (~p).sum() >= min_n else pooled_miss
        rates[int(year)] = {"n_pred": int(p.sum()), "p_given_pred": float(hit), "n_not_pred": int((~p).sum()),
                            "p_given_not_pred": float(miss),
                            "recall": float(p[a].mean()) if a.any() else None}
    return rates


# Evidence is binned by its distance to the week's cut (value / cut), so names far below it, the bulk of
# the missing names, are not given the hit rate of names just under it.
RATIO_EDGES = [0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, np.inf]
MIN_BIN_ROWS = 30
YEARS = list(range(int(FIRST_WEEK[:4]), int(LAST_WEEK[:4]) + 1))


# A class of a multi-class company carries the company's market cap and float: when the class's own
# dollar volume, in the nearest week within CLASS_DV_WEEKS that has one, is under CLASS_DV_CAP_RATIO of that
# week's rank-250 cut, that dollar volume, not the company-level proxy, sets the expected count (QRTEB
# 2018-03..05: Qurate's $9.0B float, while QRTEB's own dv50 from 2018-05-25 ranks about 1,092nd).
CLASS_DV_WEEKS = 13
CLASS_DV_CAP_RATIO = RATIO_EDGES[1]


def class_dv_ratios(frame: pd.DataFrame, cut: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(the class's own dollar volume / the rank-250 cut in the nearest week of the same security within
    CLASS_DV_WEEKS that has one, how many weeks away), for classes of multi-class companies; NaN elsewhere.
    Own dollar volume: the canonical dv50, else step 6's (or the stored-direct) dv50 at any price. The
    earlier week wins a tie."""
    ratio, away = np.full(len(frame), np.nan), np.full(len(frame), np.nan)
    if "multi_class" not in frame or not frame["multi_class"].to_numpy(dtype=bool).any():
        return ratio, away
    c = cut.reindex(frame["week_index"].values)["cut250"].to_numpy(dtype=float)
    dv = frame["dv50"].to_numpy(dtype=float)
    pf_dv = frame["pf_dv50"].to_numpy(dtype=float) if "pf_dv50" in frame else np.full(len(frame), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        own = np.where(~np.isnan(dv), dv, pf_dv) / c
    multi = np.flatnonzero(frame["multi_class"].to_numpy(dtype=bool))
    weeks = frame["week_index"].to_numpy()
    for _, index in pd.Series(multi).groupby(frame["security_id"].to_numpy(dtype=object)[multi]):
        index = index.to_numpy()
        have = index[~np.isnan(own[index])]
        if not len(have):
            continue
        have = have[np.argsort(weeks[have], kind="stable")]
        hw = weeks[have]
        pos = np.searchsorted(hw, weeks[index])
        left = np.clip(pos - 1, 0, len(hw) - 1)
        right = np.clip(pos, 0, len(hw) - 1)
        exact = (pos < len(hw)) & (hw[right] == weeks[index])
        d_left = np.where(pos > 0, weeks[index] - hw[left], np.inf)
        d_right = np.where(pos < len(hw), hw[right] - weeks[index], np.inf)
        pick = np.where(exact | (d_right < d_left), right, left)
        dist = np.where(exact, 0, np.minimum(d_left, d_right))
        ok = dist <= CLASS_DV_WEEKS
        ratio[index[ok]] = own[have[pick[ok]]]
        away[index[ok]] = dist[ok]
    return ratio, away


def ratio_bins(ratio: np.ndarray) -> np.ndarray:
    """Bin index 0..6 of value / cut (NaN -> -1)."""
    out = np.digitize(np.nan_to_num(ratio, nan=-1.0), RATIO_EDGES[1:-1])
    return np.where(np.isnan(ratio), -1, out)


def binned_rates(bins: np.ndarray, actual: np.ndarray, years: np.ndarray,
                 fallback: np.ndarray | None = None) -> dict:
    """P(actual | bin), by year where a year's bin has MIN_BIN_ROWS rows, else pooled over years, else
    ``fallback`` (a pooled table from a wider population). ``table`` is years x bins."""
    n_bins = len(RATIO_EDGES) - 1
    pooled_n = np.array([int((bins == b).sum()) for b in range(n_bins)])
    pooled_p = np.array([actual[bins == b].mean() if pooled_n[b] else np.nan for b in range(n_bins)])
    if fallback is not None:
        pooled_p = np.where(pooled_n >= MIN_BIN_ROWS, pooled_p, fallback)
    pooled_p = np.nan_to_num(pooled_p, nan=0.0)
    table = np.tile(pooled_p, (len(YEARS), 1))
    counts = np.zeros((len(YEARS), n_bins), dtype=int)
    for i, year in enumerate(YEARS):
        sel = years == year
        for b in range(n_bins):
            hit = sel & (bins == b)
            counts[i, b] = int(hit.sum())
            if counts[i, b] >= MIN_BIN_ROWS:
                table[i, b] = actual[hit].mean()
    return {"edges": [e if np.isfinite(e) else "inf" for e in RATIO_EDGES], "pooled_n": pooled_n.tolist(),
            "pooled_p": [round(float(v), 5) for v in pooled_p], "table": table, "counts": counts}


def evidence_ratios(frame: pd.DataFrame, cut: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(step-6 or stored-direct dv50 / canonical rank-250 cut where that dv50 is usable, proxy / band
    median)."""
    c = cut.reindex(frame["week_index"].values)
    dv = np.where(frame["pf_dv_ok"].values, frame["pf_dv50"].values / c["cut250"].values, np.nan)
    return dv, proxy_ratio_of(frame, cut)


def evidence_class(frame: pd.DataFrame) -> np.ndarray:
    """A missing name-week's evidence (EVIDENCE): ``price_lt_10`` (step 6's raw close under $10),
    ``dv`` (usable step-6 or stored-direct dv50), ``proxy`` (a market cap or float with a band median to
    compare it to), else ``unknown``: no series and no size proxy, so its top-250 status is not known and
    is never taken as small."""
    proxy = ~np.isnan(frame["proxy_ratio"].to_numpy(dtype=float))
    return np.select([frame["pf_price_low"].to_numpy(dtype=bool), frame["pf_dv_ok"].to_numpy(dtype=bool), proxy],
                     EVIDENCE[:3], default=EVIDENCE[3])


def known_status(listed: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    """Universe-base name-weeks (traded, not a new listing) whose top-250 status dollar volume settles, and
    that status: a canonical rank (rank <= 250) or a canonical close under $10 (no); for a missing name,
    step 6's dollar volume against the canonical cut (the dollar-volume rule is about 98% right) or step 6's
    raw close under $10 (no). Missing names with neither are the ones the proxy has to stand in for."""
    base = listed[listed["eligible"] & ~listed["outside_trading"] & ~listed["young"]]
    ranked = base["dv50_rank_any_price"].notna().to_numpy()
    low = (base["price_ge_10"] == "N").to_numpy()
    missing = base["missing"].to_numpy(dtype=bool)
    miss_dv = missing & base["pf_dv_ok"].to_numpy(dtype=bool)
    miss_low = missing & base["pf_price_low"].to_numpy(dtype=bool)
    known = ranked | low | miss_dv | miss_low
    actual = np.where(ranked, (base["dv50_rank"] <= TOP_N).fillna(False).to_numpy(dtype=bool),
                      np.where(miss_dv, base["pf_ge_cut250"].to_numpy(dtype=bool), False))
    out = base[known].assign(status_from_step6=(~ranked & ~low)[known])
    return out, actual[known].astype(bool)


def calibrate(listed: pd.DataFrame) -> dict:
    """Hit rates of the evidence rules by year and by distance to the cut.

    - dollar-volume rule: on name-weeks with a canonical rank that step 6 priced from a stored file only
      (as the missing names mostly are; thin bins fall back to all step-6 sources), actual = canonical
      dv50 rank <= 250;
    - proxy rule: on every universe-base name-week whose status dollar volume settles (``known_status``),
      separately for single-class securities and for classes of a multi-class company (whose market cap
      and float are the company's, not the class's);
    - no evidence: name-weeks of that population without a proxy whose status comes from step 6, reported
      only: a missing name-week without any evidence is ``unknown`` and gets no rate (``expected_top250``).
    Threshold views (ratio >= 1) are reported next to the bins."""
    ranked = listed[listed["eligible"] & listed["dv50_rank_any_price"].notna()]
    actual = (ranked["dv50_rank"] <= TOP_N).fillna(False).to_numpy(dtype=bool)
    years = ranked["week_end"].dt.year.values
    dv = ranked["pf_dv_ok"].to_numpy(dtype=bool)
    stored = dv & (ranked["pf_src"] == "stored").to_numpy()
    dv_bins = ratio_bins(ranked["dv_ratio"].values)
    dv_all = binned_rates(dv_bins[dv], actual[dv], years[dv])
    dv_stored = binned_rates(dv_bins[stored], actual[stored], years[stored], fallback=np.array(dv_all["pooled_p"]))
    known, status = known_status(listed)
    k_years = known["week_end"].dt.year.values
    k_bins = ratio_bins(known["proxy_ratio"].values)
    has_proxy = k_bins >= 0
    multi = known["multi_class"].to_numpy(dtype=bool)
    single_rows, multi_rows = has_proxy & ~multi, has_proxy & multi
    proxy_single = binned_rates(k_bins[single_rows], status[single_rows], k_years[single_rows])
    proxy_multi = binned_rates(k_bins[multi_rows], status[multi_rows], k_years[multi_rows],
                               fallback=np.array(proxy_single["pooled_p"]))
    above = known["proxy_above"].to_numpy(dtype=bool)
    no_evidence = ~has_proxy & known["status_from_step6"].to_numpy(dtype=bool)
    return {"dv_rule": dv_stored, "dv_rule_all_sources": dv_all, "proxy_rule": proxy_single,
            "proxy_rule_multi_class": proxy_multi,
            "no_evidence": {"n": int(no_evidence.sum()),
                            "p": float(status[no_evidence].mean()) if no_evidence.any() else 0.0,
                            "p_with_canonical_names": float(status[~has_proxy].mean()) if (~has_proxy).any() else 0.0},
            "population": {"ranked_name_weeks": int(len(ranked)), "known_status_name_weeks": int(len(known)),
                           "known_status_top250": int(status.sum())},
            "threshold_views": {
                "dv_rule_stored_only": rule_rates(ranked[stored], ranked["pf_ge_cut250"].to_numpy(dtype=bool)[stored],
                                                  actual[stored]),
                "dv_rule_all_sources": rule_rates(ranked[dv], ranked["pf_ge_cut250"].to_numpy(dtype=bool)[dv], actual[dv]),
                "proxy_rule": rule_rates(known[single_rows], above[single_rows], status[single_rows]),
                "proxy_rule_multi_class": rule_rates(known[multi_rows], above[multi_rows], status[multi_rows])}}


# The columns ``calibrate`` and ``expected_top250`` read (a narrow copy serves the binning sensitivity).
CALIBRATION_COLUMNS = ["eligible", "outside_trading", "young", "missing", "week_end", "dv50_rank", "dv50_rank_any_price",
                       "price_ge_10", "pf_dv_ok", "pf_src", "pf_ge_cut250", "pf_price_low", "dv_ratio", "proxy_ratio",
                       "proxy_above", "multi_class", "proxy_class_capped"]


def expected_top250(missing: pd.DataFrame, rates: dict, proxy_bin0: float | None = None) -> np.ndarray:
    """Probability that each missing name-week is a top-250 name-week, from the evidence it has: the
    step-6 or stored-direct dollar volume where usable, else the proxy (single- or multi-class table;
    a class whose own dollar volume within CLASS_DV_WEEKS is under a quarter of the cut takes the
    dollar-volume rule's lowest bin instead, ``proxy_class_capped``);
    0 where step 6 saw a raw close under $10; NaN with no evidence at all (``unknown``: the status is not
    known, so no small rate is put on it; the summaries count these name-weeks apart). ``proxy_bin0``
    replaces the proxy rate of the lowest bin (proxy under 25% of the band median), for a sensitivity."""
    year_index = np.clip(missing["week_end"].dt.year.values - YEARS[0], 0, len(YEARS) - 1)
    dv_bins = ratio_bins(missing["dv_ratio"].values)
    proxy_bins = ratio_bins(missing["proxy_ratio"].values)
    multi = missing["multi_class"].to_numpy(dtype=bool) if "multi_class" in missing else np.zeros(len(missing), bool)
    p = np.full(len(missing), np.nan)
    for rows, table in ((~multi, rates["proxy_rule"]["table"]), (multi, rates.get("proxy_rule_multi_class",
                                                                                     rates["proxy_rule"])["table"])):
        use = rows & (proxy_bins >= 0)
        p[use] = table[year_index[use], proxy_bins[use]]
        if proxy_bin0 is not None:
            p[use & (proxy_bins == 0)] = proxy_bin0
    has_dv = dv_bins >= 0
    p[has_dv] = rates["dv_rule"]["table"][year_index[has_dv], dv_bins[has_dv]]
    if "proxy_class_capped" in missing:
        # A class whose own dollar volume a few weeks away is far under the cut: the dollar-volume rule's
        # lowest bin, not the company-level proxy (``class_dv_ratios``).
        capped = missing["proxy_class_capped"].to_numpy(dtype=bool) & ~has_dv
        p[capped] = rates["dv_rule"]["table"][year_index[capped], 0]
    p[missing["pf_price_low"].to_numpy(dtype=bool)] = 0.0
    return p


def rates_for_json(rates: dict) -> dict:
    out = {}
    for name, value in rates.items():
        if isinstance(value, dict) and "table" in value:
            out[name] = {k: v for k, v in value.items() if k not in ("table", "counts")}
            out[name]["by_year"] = {year: {"p": [round(float(x), 5) for x in value["table"][i]],
                                           "n": value["counts"][i].tolist()} for i, year in enumerate(YEARS)}
        else:
            out[name] = value
    return out


# ------------------------------------------------------------------ industry and earnings

def sic_on_dates(cik: np.ndarray, days: pd.Series, sic_history: pd.DataFrame,
                 master_sic: dict[str, str]) -> tuple[np.ndarray, np.ndarray]:
    """(SIC, basis) on each date: the latest header on or before it, else the earliest header
    (``earliest_header``), else the master SIC (``master``); 6770 becomes the operating SIC after it."""
    left = pd.DataFrame({"cik": pd.Series(cik).astype(str).values,
                         "date": pd.to_datetime(pd.Series(days).values).astype("datetime64[ns]"),
                         "_order": np.arange(len(cik))})
    history = sic_history.assign(date=pd.to_datetime(sic_history["observed_date"]).astype("datetime64[ns]"))
    history = history.sort_values("date")[["cik", "date", "sic", "operating_sic_after_6770"]]
    merged = pd.merge_asof(left.sort_values("date"), history, on="date", by="cik", direction="backward")
    merged = merged.set_index("_order").sort_index()
    text = lambda series: series.fillna("").astype(str).to_numpy(dtype=object)
    sic = text(merged["sic"])
    operating = text(merged["operating_sic_after_6770"])
    basis = np.where(sic != "", "header", "").astype(object)
    earliest = history.drop_duplicates("cik", keep="first").set_index("cik")
    gap = sic == ""
    if gap.any():
        first_sic = text(left["cik"].map(earliest["sic"]))
        first_op = text(left["cik"].map(earliest["operating_sic_after_6770"]))
        take_first = gap & (first_sic != "")
        sic[take_first] = first_sic[take_first]
        operating = np.where(take_first, first_op, operating)
        basis[take_first] = "earliest_header"
    gap = sic == ""
    if gap.any():
        fallback = text(left["cik"].map(master_sic))
        use = gap & (fallback != "")
        sic[use] = fallback[use]
        basis[use] = "master"
    fix = (sic == "6770") & (operating != "")
    sic[fix] = operating[fix]
    basis[fix] = basis[fix] + "+6770_operating"
    return sic, basis


def ff49_of(sic: np.ndarray, maps: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(FF49 id, short name, unmatched flag); an unmatched SIC goes to 49 Other, a blank SIC to ''."""
    ff = maps[maps["scheme"] == "FF49"].astype({"sic_lo": int, "sic_hi": int}).sort_values("sic_lo")
    lo, hi = ff["sic_lo"].values, ff["sic_hi"].values
    ids, names = ff["industry_id"].values, ff["short_name"].values
    other_name = ff.loc[ff["industry_id"] == "49", "short_name"].iloc[0] if (ff["industry_id"] == "49").any() else "Other"
    out_id = np.full(len(sic), "", dtype=object)
    out_name = np.full(len(sic), "", dtype=object)
    unmatched = np.zeros(len(sic), dtype=bool)
    for k, value in enumerate(sic):
        if not value or not str(value).isdigit():
            continue
        code = int(value)
        j = np.searchsorted(lo, code, "right") - 1
        if j >= 0 and code <= hi[j]:
            out_id[k], out_name[k] = ids[j], names[j]
        else:
            out_id[k], out_name[k], unmatched[k] = "49", other_name, True
    return out_id, out_name, unmatched


def earnings_offsets(cik: np.ndarray, week_pos: np.ndarray, events: pd.DataFrame,
                     sessions: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """(signed session offset of the nearest D0 within EARNINGS_OFFSET_MAX sessions or NaN, and the same
    for results releases only), for rows given by CIK and week-end session position."""
    events = events[events["d0_session"] != ""]
    d0 = sessions.searchsorted(pd.to_datetime(events["d0_session"]))
    events = events.assign(pos=d0)

    def nearest(table: pd.DataFrame) -> np.ndarray:
        out = np.full(len(cik), np.nan)
        positions = {c: np.sort(g["pos"].values) for c, g in table.groupby("cik")}
        frame = pd.DataFrame({"cik": cik})
        for c, index in frame.groupby("cik").indices.items():
            array = positions.get(c)
            if array is None or not len(array):
                continue
            p = week_pos[index]
            j = np.searchsorted(array, p)
            before = np.where(j > 0, array[np.maximum(j - 1, 0)] - p, -10 ** 6)
            after = np.where(j < len(array), array[np.minimum(j, len(array) - 1)] - p, 10 ** 6)
            best = np.where(np.abs(before) <= np.abs(after), before, after).astype(float)
            best[np.abs(best) > EARNINGS_OFFSET_MAX] = np.nan
            out[index] = best
        return out

    return nearest(events), nearest(events[events["event_kind"] == "results_release"])


# ------------------------------------------------------------------ outputs

def dv_bucket(value: float) -> str:
    """A 1-2-5 bucket label for a dollar amount (no vendor value is written)."""
    if value is None or not np.isfinite(value) or value <= 0:
        return ""
    steps = [1, 2, 5]
    exponent = int(np.floor(np.log10(value)))
    edges = [s * 10 ** e for e in (exponent - 1, exponent, exponent + 1) for s in steps]
    low = max(e for e in edges if e <= value)
    high = min(e for e in edges if e > value)

    def label(x: float) -> str:
        for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
            if x >= size:
                return f"{x / size:g}{unit}"
        return f"{x:g}"

    return f"{label(low)}-{label(high)}"


def window_before_listing(rows: pd.DataFrame, sessions: pd.DatetimeIndex, week_pos: np.ndarray) -> np.ndarray:
    """Name-weeks of an interval whose series starts where step 9 cut it (``ipo_boundary``: a transfer
    from another exchange such as AMD or CSX, or rows step 9 kept before the listing) whose 50-session
    window still holds canonical rows from before the interval's start."""
    if "interval_start" not in rows or "ipo_boundary" not in rows:
        return np.zeros(len(rows), dtype=bool)
    start = rows["interval_start"].astype(str)
    start_pos = sessions.searchsorted(pd.to_datetime(start))
    window_first = week_pos[rows["week_index"].to_numpy()] - (max(DV_WINDOWS) - 1)
    first = rows["first_row"].astype(str)
    return (rows["ipo_boundary"].to_numpy(dtype=bool) & (start_pos > window_first)
            & (first != "").to_numpy() & (first < start).to_numpy())


def top300_table(listed: pd.DataFrame, master: pd.DataFrame, sic_history: pd.DataFrame, maps: pd.DataFrame,
                 events: pd.DataFrame, sessions: pd.DatetimeIndex, week_pos: np.ndarray) -> pd.DataFrame:
    ranks = listed[[f"dv{w}_rank{s}" for w in DV_WINDOWS for s in ("", "_any_price")]]
    top = listed[(ranks <= PRICE_RANK).any(axis=1)].copy()
    info = master.set_index("security_id")
    top["cik"] = top["security_id"].map(info["cik"]).fillna("")
    top["foreign_filer"] = top["security_id"].map(info["foreign_filer"]).fillna("")
    top["multi_class_group"] = top["security_id"].map(info["multi_class_group"]).fillna("")
    master_sic = dict(zip(master["cik"], master["sic"]))
    sic, basis = sic_on_dates(top["cik"].values, top["week_end"], sic_history, master_sic)
    top["sic"], top["sic_basis"] = sic, basis
    ff, names, unmatched = ff49_of(sic, maps)
    top["ff49"], top["ff49_name"] = ff, names
    top.loc[unmatched, "sic_basis"] = top.loc[unmatched, "sic_basis"] + "+unmatched_other"
    offset, release = earnings_offsets(top["cik"].values, week_pos[top["week_index"].values], events, sessions)
    top["earnings_nearest_d0_offset"] = pd.array(np.where(np.isnan(offset), np.nan, offset), dtype="Int64")
    top["earnings_event_within_3_sessions"] = yes_no(np.abs(np.nan_to_num(offset, nan=99)) <= EARNINGS_WINDOW)
    top["results_release_within_3_sessions"] = yes_no(np.abs(np.nan_to_num(release, nan=99)) <= EARNINGS_WINDOW)
    top["dv_window_before_listing"] = yes_no(window_before_listing(top, sessions, week_pos))
    top["close_in_week"] = yes_no(top["in_week"])
    top["close_on_week_end"] = yes_no(top["on_week_end"])
    top["close_lag_sessions"] = pd.array(top["lag"].values, dtype="Int64")
    for column in ("dv50_rank", "dv20_rank", "dv50_rank_any_price", "dv20_rank_any_price"):
        top[column] = pd.array(top[column].values, dtype="Int64")
    top["week_end"] = top["week_end"].dt.strftime("%Y-%m-%d")
    return top.sort_values(["week_end", "dv50_rank_any_price", "security_id"])[TOP300_COLUMNS + ["week_index"]]


def snapshot_ages(weeks: pd.DatetimeIndex, index: pd.DataFrame) -> pd.DataFrame:
    """Per week: days since the latest snapshot of any source on or before it, and that source."""
    snaps = index[["snapshot_date", "source"]].assign(date=pd.to_datetime(index["snapshot_date"])).sort_values("date")
    left = pd.DataFrame({"week_end": weeks.astype("datetime64[ns]")})
    merged = pd.merge_asof(left, snaps.rename(columns={"date": "week_end_snap"}).astype({"week_end_snap": "datetime64[ns]"}),
                           left_on="week_end", right_on="week_end_snap", direction="backward")
    merged["snapshot_age_days"] = (merged["week_end"] - merged["week_end_snap"]).dt.days
    return merged[["week_end", "snapshot_age_days", "source"]].rename(columns={"source": "snapshot_source"})


def coverage_weights(rows: pd.DataFrame) -> pd.DataFrame:
    """Rows with ``weight``: the proxy, split evenly among the listed classes of a multi-class company that
    week (Nasdaq's company lists and XBRL floats give every class the company's whole value)."""
    company = np.where(rows["multi_class"].to_numpy(dtype=bool), rows["cik"].astype(str).to_numpy(dtype=object),
                       rows["security_id"].to_numpy(dtype=object))
    classes = pd.Series(1, index=rows.index).groupby([rows["week_index"].values, company]).transform("sum")
    return rows.assign(weight=rows["proxy"].values / classes.values)


def weekly_summary(listed: pd.DataFrame, cut: pd.DataFrame, weeks: pd.DatetimeIndex, ages: pd.DataFrame,
                   top: pd.DataFrame) -> pd.DataFrame:
    g = listed.groupby("week_index")
    elig = listed[listed["eligible"]]
    e = elig.groupby("week_index")
    top250 = listed[listed["dv50_rank"] <= TOP_N]
    top300 = listed[listed["dv50_rank"] <= PRICE_RANK]
    t250, t300 = top250.groupby("week_index"), top300.groupby("week_index")
    miss = listed[listed["missing"]]
    m = miss.groupby("week_index")
    idx = pd.RangeIndex(len(weeks), name="week_index")
    s = pd.DataFrame(index=idx)
    s["week_end"] = weeks.strftime("%Y-%m-%d")
    s["n_listed_nasdaq"] = g.size()
    s["n_listed_common"] = e.size()
    s["n_foreign_excluded"] = listed[listed["foreign"]].groupby("week_index").size()
    s["n_non_common_excluded"] = listed[listed["non_common"]].groupby("week_index").size()
    s["n_spac_shell_excluded"] = listed[listed["spac_shell"] & ~listed["non_common"]].groupby("week_index").size()
    if "investment_company" in listed:
        ic = listed["investment_company"] & ~(listed["non_common"] | listed["spac_shell"] | listed["foreign"])
        s["n_investment_company_excluded"] = listed[ic].groupby("week_index").size()
    else:
        s["n_investment_company_excluded"] = 0
    s["n_unknown_foreign_flag"] = elig[elig["foreign_filer"] == "UNKNOWN"].groupby("week_index").size()
    s["n_with_vendor_prices"] = e["in_week"].sum()
    s["n_price_ge_10"] = (elig["price_ge_10"] == "Y").groupby(elig["week_index"]).sum()
    s["n_ranked_dv50"] = e["dv50_rank"].count()
    s["n_ranked_dv20"] = e["dv20_rank"].count()
    s["cutoff_rank250_dv_bucket"] = [dv_bucket(v) for v in cut["cut250"].reindex(idx).values]
    s["cutoff_rank300_dv_bucket"] = [dv_bucket(v) for v in cut["cut300"].reindex(idx).values]
    s["top250_close_in_week"] = t250["in_week"].sum()
    s["top300_close_in_week"] = t300["in_week"].sum()
    s["top250_close_on_week_end"] = t250["on_week_end"].sum()
    s["top300_close_on_week_end"] = t300["on_week_end"].sum()
    s["top250_zero_volume_close"] = t250["zero_volume"].sum()
    s["n_outside_trading"] = elig[elig["outside_trading"]].groupby("week_index").size()
    s["n_young"] = elig[elig["young"]].groupby("week_index").size()
    s["n_missing"] = m.size()
    s["n_missing_with_pf_dv"] = m["pf_dv_ok"].sum()
    s["n_missing_pf_dv_ge_cut250"] = m["pf_ge_cut250"].sum()
    s["n_missing_pf_dv_ge_cut300"] = m["pf_ge_cut300"].sum()
    s["n_missing_proxy_above"] = m["proxy_above"].sum()
    float_only = miss["proxy_above_float_only"] if "proxy_above_float_only" in miss else pd.Series(False, index=miss.index)
    s["n_missing_proxy_above_float_only"] = float_only.groupby(miss["week_index"]).sum()
    s["n_missing_proxy_above_single_class"] = (miss["proxy_above"] & ~miss["multi_class"]).groupby(miss["week_index"]).sum()
    settled = miss[~miss["missing_reason"].isin(PENDING_REASONS)]
    s["n_missing_pf_dv_ge_cut250_not_pending"] = settled["pf_ge_cut250"].groupby(settled["week_index"]).sum()
    no_dv = miss[~miss["pf_dv_ok"] & ~miss["pf_price_low"]]
    s["n_missing_proxy_above_no_pf_dv"] = no_dv["proxy_above"].groupby(no_dv["week_index"]).sum()
    no_dv_settled = no_dv[~no_dv["missing_reason"].isin(PENDING_REASONS)]
    s["n_missing_proxy_above_no_pf_dv_not_pending"] = no_dv_settled["proxy_above"].groupby(no_dv_settled["week_index"]).sum()
    direct = miss["dv_stored_direct"] if "dv_stored_direct" in miss else pd.Series(False, index=miss.index)
    s["n_missing_dv_stored_direct"] = (direct & miss["pf_dv_ok"]).groupby(miss["week_index"]).sum()
    unknown = miss["evidence"] == "unknown"
    s["n_missing_unknown"] = unknown.groupby(miss["week_index"]).sum()
    s["n_missing_unknown_not_pending"] = (unknown & ~miss["missing_reason"].isin(PENDING_REASONS)).groupby(
        miss["week_index"]).sum()
    s["est_missing_top250"] = m["p_top250"].sum().round(2)
    residual = miss[~miss["missing_reason"].isin(PENDING_REASONS)]
    s["est_missing_top250_residual"] = residual.groupby("week_index")["p_top250"].sum().round(2)
    lone = residual[~residual["sibling_priced"]]
    s["est_missing_top250_residual_ex_siblings"] = lone.groupby("week_index")["p_top250"].sum().round(2)
    for reason in MISSING_REASONS:
        s[f"n_missing_{reason}"] = miss[miss["missing_reason"] == reason].groupby("week_index").size()
    pending = miss[miss["missing_reason"].isin(PENDING_REASONS)]
    s["n_unresolved_candidates"] = pending.groupby("week_index").size()
    s["n_unresolved_candidates_ge_cut300"] = pending["pf_ge_cut300"].groupby(pending["week_index"]).sum()
    weighted = coverage_weights(elig[~elig["outside_trading"] & elig["proxy"].notna()])
    w = weighted.groupby("week_index")
    s["mcap_weighted_coverage"] = (weighted["weight"].where(weighted["in_week"], 0.0).groupby(weighted["week_index"]).sum()
                                   / w["weight"].sum()).round(5)
    s["mcap_weighted_coverage_names"] = w.size()
    known = weighted["in_week"] | weighted["pf_dv_ok"] | weighted["pf_price_low"]
    s["mcap_weighted_coverage_known_dv"] = (weighted["weight"].where(known, 0.0).groupby(weighted["week_index"]).sum()
                                            / w["weight"].sum()).round(5)
    ages = ages.set_index(idx)
    s["snapshot_age_days"] = ages["snapshot_age_days"]
    s["snapshot_stale"] = yes_no(ages["snapshot_age_days"] > STALE_SNAPSHOT_DAYS)
    s["snapshot_source"] = ages["snapshot_source"]
    c = cut.reindex(idx)
    s["pf_cut250_ratio"] = (c["cut250"] / c["pf_cut250"]).round(4)
    s["pf_cut300_ratio"] = (c["cut300"] / c["pf_cut300"]).round(4)
    both = listed[(listed["dv50_rank"] <= TOP_N) & (listed["pf_dv50_rank"] <= TOP_N)]
    s["top250_overlap_prefilter"] = both.groupby("week_index").size()
    pf_only = listed[(listed["pf_dv50_rank"] <= TOP_N) & ~(listed["dv50_rank"] <= TOP_N)]
    s["pf_top250_not_top250_here"] = pf_only.groupby("week_index").size()
    t = top[top["dv50_rank"] <= TOP_N]
    tg = t.groupby("week_index")
    s["top250_ff49_missing"] = (t["ff49"] == "").groupby(t["week_index"]).sum()
    s["top250_earnings_flag"] = (t["earnings_event_within_3_sessions"] == "Y").groupby(t["week_index"]).sum()
    s["top250_series_ends_within_4w"] = top250.loc[top250["ends_within_4w"]].groupby("week_index").size()
    unresolved = top250["ends_within_4w"] & top250["terminal_unresolved"]
    s["top250_series_ends_unresolved_terminal"] = top250.loc[unresolved].groupby("week_index").size()
    int_columns = [col for col in SUMMARY_COLUMNS if col.startswith(("n_", "top2", "top3", "pf_top"))]
    for col in int_columns:
        s[col] = s[col].fillna(0).astype(int)
    for col in ("est_missing_top250", "est_missing_top250_residual", "est_missing_top250_residual_ex_siblings"):
        s[col] = s[col].fillna(0.0)
    # Unknown name-weeks count as top-250 name-weeks in the upper bound (never as small).
    s["est_missing_top250_residual_upper"] = (s["est_missing_top250_residual"] + s["n_missing_unknown_not_pending"]).round(2)
    ranked_and_closed = (s["n_ranked_dv50"] >= TOP_N) & (s["top250_close_in_week"] == TOP_N)
    s["complete_250"] = yes_no(ranked_and_closed & (s["n_missing_pf_dv_ge_cut250"] == 0)
                               & (s["n_missing_proxy_above_no_pf_dv"] == 0) & (s["n_missing_unknown"] == 0))
    s["complete_250_strict"] = yes_no((s["complete_250"] == "Y") & (s["est_missing_top250"] < STRICT_EXPECTED_LIMIT))
    s["complete_250_after_pending"] = yes_no(ranked_and_closed & (s["n_missing_pf_dv_ge_cut250_not_pending"] == 0)
                                             & (s["n_missing_proxy_above_no_pf_dv_not_pending"] == 0)
                                             & (s["n_missing_unknown_not_pending"] == 0))
    return s.reset_index(drop=True)[SUMMARY_COLUMNS]


# ------------------------------------------------------------------ plan 3.3 checks 2-6

def recent_fetches(fetch_status: pd.DataFrame, panel_built: str) -> set[str]:
    """Securities with a Tiingo answer (done / done_review / partial) fetched after the panel was built."""
    if fetch_status is None or not len(fetch_status) or not panel_built:
        return set()
    ok = fetch_status["status"].isin(FETCH_FINAL - FETCH_EMPTY) & (fetch_status["fetched_utc"] > panel_built)
    return set(fetch_status.loc[ok, "security_id"])


def security_reason(sid: str, reasons, recent: set, day: str = "") -> str:
    """The reason a security lacks rows (on ``day`` when ``reasons`` is a ``MissingReasons``)."""
    if sid in recent:
        return "fetched_pending_reconcile"
    if day and hasattr(reasons, "on"):
        return reasons.on(sid, day)
    return reasons.get(sid, "not_candidate")


def capture_coverage(lists: pd.DataFrame, spans: pd.DataFrame, foreign: dict, shells, panel: pd.DataFrame,
                     sessions: pd.DatetimeIndex, unfillable_ids: set, master: pd.DataFrame | None = None,
                     reasons=None, recent: set = frozenset(),
                     step6: pd.DataFrame | None = None, investment: dict | None = None) -> pd.DataFrame:
    """Check 2: on each company-list capture, the top 200 universe-base names by market cap (investment
    companies that day left out, as the base leaves them out) and whether
    each has a canonical row on the as-of session (or within 5 sessions before it). The lists give every
    class of a multi-class company the company's market cap, so a class without a row whose sibling class
    has one is marked ``sibling_priced``; ``reason`` says why a name without a row has none
    (``vendor_outside_canonical_set``: step 6 held vendor rows for it that week but ranked it below the
    fetch cut, so step 9 never built its series). Captures from the week before the first week end on."""
    frame = lists[(lists["market_cap"] > 0)].copy()
    frame["as_of"] = frame["as_of_session"].where(frame["as_of_session"] != "", frame["snapshot_date"])
    frame = frame[(frame["as_of"] >= CAPTURE_FROM) & (frame["as_of"] <= LAST_WEEK)]
    merged = frame.merge(spans[["security_id", "list_start", "list_end", "non_common"]], on="security_id")
    merged = merged[(merged["list_start"] <= merged["as_of"]) & (merged["as_of"] <= merged["list_end"])]
    merged = merged.sort_values("list_start").drop_duplicates(["snapshot_date", "security_id"], keep="last")
    merged = merged[~merged["non_common"] & ~merged["security_id"].isin(set(shells))]
    merged = merged[~foreign_mask(merged["security_id"].values, merged["as_of"].values, foreign)]
    merged = merged[~foreign_mask(merged["security_id"].values, merged["as_of"].values, investment or {})]
    merged["mcap_rank"] = merged.groupby("snapshot_date")["market_cap"].rank(ascending=False, method="first")
    top = merged[merged["mcap_rank"] <= 200].copy()
    days = panel[["security_id", "date"]].assign(pos=sessions.get_indexer(panel["date"]))
    top["as_of_pos"] = sessions.searchsorted(pd.to_datetime(top["as_of"]), "right") - 1
    cik = dict(zip(master["security_id"], master["cik"])) if master is not None else {}
    top["cik"] = top["security_id"].map(cik).fillna(top["security_id"])
    wanted = set(top["security_id"]) | set(k for k, c in cik.items() if c in set(top["cik"]))
    rows = days[days["security_id"].isin(wanted)]
    have = set(zip(rows["security_id"], rows["pos"]))
    within = lambda sid, pos: any((sid, pos - d) in have for d in range(CLOSE_STALE_SESSIONS + 1))
    top["row_on_as_of"] = [(s, p) in have for s, p in zip(top["security_id"], top["as_of_pos"])]
    top["row_within_5"] = [within(s, p) for s, p in zip(top["security_id"], top["as_of_pos"])]
    classes = defaultdict(list)
    for sid, c in cik.items():
        classes[c].append(sid)
    top["sibling_priced"] = [not ok and any(within(o, p) for o in classes.get(c, []) if o != s)
                             for s, c, p, ok in zip(top["security_id"], top["cik"], top["as_of_pos"], top["row_within_5"])]
    top["unfillable"] = top["security_id"].isin(unfillable_ids)
    reasons = reasons or {}
    top["reason"] = [("" if ok else security_reason(s, reasons, recent, day))
                     for s, ok, day in zip(top["security_id"], top["row_within_5"], top["as_of"])]
    if step6 is not None and len(top):
        left = top[["security_id", "as_of"]].assign(week_end=pd.to_datetime(top["as_of"]).astype("datetime64[ns]"),
                                                    _order=np.arange(len(top)))
        right = step6[["security_id", "week_end", "pf_src"]].astype({"week_end": "datetime64[ns]"}).sort_values("week_end")
        near = pd.merge_asof(left.sort_values("week_end"), right, on="week_end", by="security_id", direction="forward",
                             tolerance=pd.Timedelta(days=7)).set_index("_order").sort_index()
        vendor = near["pf_src"].isin(list(SRC_CODES)).to_numpy()
        gap = (~top["row_within_5"]).to_numpy() & (top["reason"] == "not_candidate").to_numpy()
        top.loc[gap & vendor, "reason"] = "vendor_outside_canonical_set"
    return top[["snapshot_date", "as_of", "security_id", "ticker", "mcap_rank", "row_on_as_of", "row_within_5",
                "sibling_priced", "unfillable", "reason"]].sort_values(["snapshot_date", "mcap_rank"])


def capture_summary(capture: pd.DataFrame) -> dict:
    """Check 2 per capture date: the share with a canonical row; the plan's share (a canonical row, vendor
    rows step 6 held outside the canonical set, or an ``unfillable.csv`` exception); and that share once
    pending fetches and classes whose sibling class is priced are set aside."""
    if capture.empty:
        return {"dates": 0}
    c = capture.assign(gap=~capture["row_within_5"])
    c["excused"] = c["gap"] & c["unfillable"]
    c["outside"] = c["gap"] & ~c["excused"] & (c["reason"] == "vendor_outside_canonical_set")
    c["pending"] = c["gap"] & ~c["excused"] & c["reason"].isin(PENDING_REASONS)
    c["sibling"] = c["gap"] & ~c["excused"] & ~c["pending"] & c["sibling_priced"]
    g = c.groupby("snapshot_date")
    table = pd.DataFrame({"n": g.size(), "covered": g["row_within_5"].sum(), "excused": g["excused"].sum(),
                          "outside": g["outside"].sum(), "pending": g["pending"].sum(), "sibling": g["sibling"].sum()})
    table["share"] = table["covered"] / table["n"]
    table["share_plan"] = (table["covered"] + table["outside"] + table["excused"]) / table["n"]
    table["share_after_pending_siblings"] = (table["covered"] + table["outside"] + table["excused"] + table["pending"]
                                             + table["sibling"]) / table["n"]
    other = c[c["gap"] & ~c["excused"] & ~c["outside"] & ~c["pending"] & ~c["sibling"]]
    return {"dates": int(len(table)), "first": str(table.index.min()), "last": str(table.index.max()),
            "min_share": round(float(table["share"].min()), 4),
            "min_share_plan": round(float(table["share_plan"].min()), 4),
            "dates_below_99pct_plan": int((table["share_plan"] < 0.99).sum()),
            "min_share_after_pending_siblings": round(float(table["share_after_pending_siblings"].min()), 4),
            "dates_below_99pct_after_pending_siblings": int((table["share_after_pending_siblings"] < 0.99).sum()),
            "gaps_by_reason": {k: int(v) for k, v in c.loc[c["gap"], "reason"].value_counts().items()},
            "gaps_unfillable": int(c["excused"].sum()), "gaps_sibling_priced": int(c["sibling"].sum()),
            "note": "vendor_outside_canonical_set gaps have vendor rows (step 6) but a dollar-volume rank below "
                    "the fetch cut, so no canonical series was built",
            "other_gap_securities": sorted(set(other["security_id"]))[:100]}


def nasdaq100_check(members: dict, spans: pd.DataFrame, master: pd.DataFrame, foreign: dict,
                    panel: pd.DataFrame, sessions: pd.DatetimeIndex, reasons=None,
                    recent: set = frozenset(), investment: dict | None = None) -> pd.DataFrame:
    """Check 3: each year-end Nasdaq-100 member (2011-2019) against its canonical rows over the listed
    sessions of that year (from 2011-06-01 for 2011). A ticker is found by the interval that holds it on
    the year's last session, else by a security that used it (``tickers_observed``) and was listed then;
    a member not listed in that year at all is ``not_listed_in_year`` (the list file carries some names
    past their exit)."""
    have = panel.groupby("security_id")["date"].apply(lambda d: set(d.values.astype("datetime64[D]")))
    flag = dict(zip(master["security_id"], master["foreign_filer"]))
    users = defaultdict(set)
    for sid, tickers in zip(master["security_id"], master["tickers_observed"]):
        for t in str(tickers).split():
            users[t].add(sid)
    reasons = reasons or {}
    rows = []
    for year, tickers in sorted(members.items()):
        year_sessions = sessions[(sessions.year == int(year))]
        last = year_sessions[-1].strftime("%Y-%m-%d")
        first = year_sessions[0].strftime("%Y-%m-%d")
        days = year_sessions.strftime("%Y-%m-%d")
        for ticker in tickers:
            hit = spans[(spans["ticker"] == ticker) & (spans["list_start"] <= last) & (spans["list_end"] >= last)]
            if hit.empty:
                alt = spans[spans["security_id"].isin(users.get(ticker, set())) & (spans["list_start"] <= last)
                            & (spans["list_end"] >= last)]
                hit = alt if len(alt) else spans[(spans["ticker"] == ticker) & (spans["list_start"] <= last)
                                                 & (spans["list_end"] >= first)].sort_values("list_end").tail(1)
            if hit.empty:
                listed_before = spans[(spans["ticker"] == ticker) & (spans["list_end"] < first)]
                rows.append({"year": int(year), "ticker": ticker, "security_id": "",
                             "status": "not_listed_in_year" if len(listed_before) else "not_mapped"})
                continue
            sid = hit["security_id"].iloc[-1]
            mine = spans[spans["security_id"] == sid]
            listed = np.zeros(len(days), dtype=bool)
            for a, b in zip(mine["list_start"], mine["list_end"]):
                listed |= (days >= a) & (days <= b)
            got = have.get(sid, set())
            covered = np.array([d in got for d in year_sessions.values.astype("datetime64[D]")])
            n_listed, n_cov = int(listed.sum()), int((covered & listed).sum())
            missing_days = [d for d, l, c in zip(days, listed, covered) if l and not c]
            if pf.is_foreign_on(foreign.get(sid), last):
                status = "foreign_excluded"
            elif pf.is_foreign_on((investment or {}).get(sid), last):
                status = "investment_company_excluded"
            elif not n_listed:
                status = "not_listed_in_year"
            else:
                status = "covered" if n_cov == n_listed else "partial" if n_cov else "no_series"
            rows.append({"year": int(year), "ticker": ticker, "security_id": sid, "status": status,
                         "listed_sessions": n_listed, "covered_sessions": n_cov,
                         "missing_sessions_first": " ".join(missing_days[:5]),
                         "reason": security_reason(sid, reasons, recent, missing_days[0]) if missing_days else "",
                         "foreign_filer": flag.get(sid, "")})
    return pd.DataFrame(rows)


def form25_check(form25: pd.DataFrame, master: pd.DataFrame, last_row: dict, sessions: pd.DatetimeIndex,
                 reasons, foreign: dict, shells, spans: pd.DataFrame, recent: set = frozenset(),
                 step6_best: dict | None = None, investment: dict | None = None) -> pd.DataFrame:
    """Check 4: every Nasdaq Form 25 for common stock with float >= $1B, effective in the window: does the
    security's canonical series end within 5 sessions of the effective date or of the filing date (the
    merger close), and if not, why (``reason``: the security's missing reason; ``not_listed_here`` for a
    security with no Nasdaq listing in the intervals; ``step6_rank_gt300`` for one whose best step-6
    dollar-volume rank was above 300, so no series was needed)."""
    step6_best = step6_best or {}
    listed_here = set(spans["security_id"])
    f = form25[(form25["classification"] == "common_delisting")].copy()
    f["float"] = pd.to_numeric(f["public_float_usd"], errors="coerce")
    f = f[(f["float"] >= 1e9) & f["float_check_flag"].isin(pf.GOOD_FLOAT_FLAGS)
          & (f["effective_date"] >= FIRST_WEEK) & (f["effective_date"] <= WINDOW_END)]
    owner = master[master["delist_form25_accession"] != ""].groupby("delist_form25_accession")["security_id"].apply(list)
    noncommon = set(spans.loc[spans["non_common"], "security_id"]) - set(spans.loc[~spans["non_common"], "security_id"])
    later = set(spans.loc[spans["after_cut"], "security_id"]) if "after_cut" in spans else set()
    rows = []
    for r in f.itertuples(index=False):
        for sid in owner.get(r.accession, []):
            last = last_row.get(sid, "")
            out = {"accession": r.accession, "security_id": sid, "subject_name": r.subject_name,
                   "filing_date": r.filing_date, "effective_date": r.effective_date, "public_float_usd": r.float,
                   "last_row": last, "reason": ""}
            if last:
                p = sessions.searchsorted(pd.Timestamp(last))
                gaps = [abs(int(sessions.searchsorted(pd.Timestamp(d))) - int(p)) for d in (r.effective_date, r.filing_date)]
                out["sessions_to_effective"], out["sessions_to_filing"] = gaps
                ok = min(gaps) <= CLOSE_STALE_SESSIONS
            else:
                ok = False
            if ok:
                out["status"] = "series_ends_near"
            elif pf.is_foreign_on(foreign.get(sid), r.effective_date):
                out["status"] = "excluded_foreign"
            elif pf.is_foreign_on((investment or {}).get(sid), day_before(r.effective_date)):
                out["status"] = "excluded_investment_company"
            elif sid in set(shells) or sid in noncommon:
                out["status"] = "excluded_non_common"
            elif last and last > r.filing_date:
                out["status"] = "series_ends_late"
                out["reason"] = "listed_again_later" if sid in later else "rows_after_filing"
            else:
                out["status"] = "series_ends_early" if last else "no_series"
                best = step6_best.get(sid, np.nan)
                if sid not in listed_here:
                    out["reason"] = "not_listed_here"
                elif not last and best > PRICE_RANK:
                    out["reason"] = "step6_rank_gt300"
                else:
                    out["reason"] = security_reason(sid, reasons, recent, day_before(r.effective_date))
            out["step6_best_rank"] = step6_best.get(sid, np.nan)
            rows.append(out)
    return pd.DataFrame(rows)


def fetch_margin(candidates: pd.DataFrame, listed: pd.DataFrame) -> dict:
    """Check 5: of the month-1 names, how many reach canonical rank <= 250 (by reason tier)."""
    month1 = candidates[candidates["fetch_month"] == "2026-10"]
    best = listed.groupby("security_id")["dv50_rank"].min()
    out = {}
    for reason, group in month1.groupby("reason"):
        ranks = best.reindex(group["security_id"].unique())
        out[reason] = {"names": int(len(ranks)), "priced": int(ranks.notna().sum()),
                       "best_rank_le_250": int((ranks <= TOP_N).sum()), "best_rank_le_300": int((ranks <= PRICE_RANK).sum())}
    return out


# ------------------------------------------------------------------ reports

EVIDENCE_BINS = ["dv_ge_cut", "dv_below_cut", "proxy_ge_0_5", "proxy_0_25_to_0_5", "proxy_lt_0_25", "price_lt_10",
                 "unknown"]


def evidence_bins(frame: pd.DataFrame) -> np.ndarray:
    """Missing name-weeks by evidence and its distance to the cut (EVIDENCE_BINS)."""
    ev = frame["evidence"].to_numpy(dtype=object)
    dv, proxy = frame["dv_ratio"].to_numpy(dtype=float), frame["proxy_ratio"].to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        conditions = [(ev == "dv") & (dv >= 1.0), ev == "dv", (ev == "proxy") & (proxy >= 0.5),
                      (ev == "proxy") & (proxy >= 0.25), ev == "proxy", ev == "price_lt_10"]
    return np.select(conditions, EVIDENCE_BINS[:6], default="unknown")


def check_6(settled: pd.DataFrame, my: pd.DataFrame, slots: int) -> dict:
    """Plan 3.3 check 6 over every missing reason except the pending ones: the expected top-250 name-weeks
    held by missing names (the model, on known evidence), plus the unknown name-weeks counted as top-250
    name-weeks (``upper``: they are never taken as small); ``lower`` puts 0 on the proxy's lowest bin
    (the bin-0 base rate is measured on known-status names and probably overstates); the plan's own
    population (``unfillable`` only) is kept beside it. The proxy bins use the either-one ratio
    (``proxy_ratio_of``, the reading of ``proxy_above``); ``market_cap_first_binning`` gives the same shares
    with the plan's market-cap-first ratio (``p_top250_mcap_first``) and the float-only name-weeks' expected
    count under both."""
    residual = float(settled["p_top250"].sum())
    low = float(settled["p_top250_low"].sum()) if "p_top250_low" in settled else residual
    unknown = int((settled["evidence"] == "unknown").sum())
    unfill_rows = my[my["missing_reason"] == "unfillable"]
    unfill = float(unfill_rows["p_top250"].sum())
    unfill_unknown = int((unfill_rows["evidence"] == "unknown").sum())
    share = lambda value: round(value / slots, 5)
    out = {"limit_share_of_slots": UNFILLABLE_SHARE_LIMIT,
           "residual_share": share(residual), "unknown_name_weeks": unknown, "unknown_share": share(unknown),
           "upper_share": share(residual + unknown), "lower_share_bin0_zero": share(low),
           "range_share": [share(low), share(residual), share(residual + unknown)],
           "unfillable_only_share": share(unfill), "unfillable_only_upper_share": share(unfill + unfill_unknown),
           "pass": bool((residual + unknown) / slots <= UNFILLABLE_SHARE_LIMIT),
           "pass_known_evidence_only": bool(residual / slots <= UNFILLABLE_SHARE_LIMIT),
           "pass_unfillable_only": bool(unfill / slots <= UNFILLABLE_SHARE_LIMIT)}
    if "p_top250_uncapped" in my:
        # The same shares with a class's company-level proxy left uncapped (``class_dv_ratios``), for comparison.
        out["without_class_cap"] = {
            "residual_share": share(float(settled["p_top250_uncapped"].sum())),
            "unfillable_only_share": share(float(unfill_rows["p_top250_uncapped"].sum())),
            "pass_unfillable_only": bool(float(unfill_rows["p_top250_uncapped"].sum()) / slots
                                         <= UNFILLABLE_SHARE_LIMIT)}
    if "p_top250_mcap_first" in my:
        # The expected count binned by the plan's market-cap-first ratio (builds before round 8's fix): a
        # float-only week (proxy_above by its float alone) then sits in the bin of its stale market cap.
        alt = float(settled["p_top250_mcap_first"].sum())
        alt_unfill = float(unfill_rows["p_top250_mcap_first"].sum())
        float_only = (settled["proxy_above_float_only"].to_numpy(dtype=bool) if "proxy_above_float_only" in settled
                      else np.zeros(len(settled), bool))
        out["market_cap_first_binning"] = {
            "residual_share": share(alt), "upper_share": share(alt + unknown),
            "unfillable_only_share": share(alt_unfill),
            "pass": bool((alt + unknown) / slots <= UNFILLABLE_SHARE_LIMIT),
            "pass_unfillable_only": bool(alt_unfill / slots <= UNFILLABLE_SHARE_LIMIT),
            "residual_name_weeks_difference": round(residual - alt, 2),
            "float_only_name_weeks": int(float_only.sum()),
            "float_only_est_top250": {"either_one_binning": round(float(settled.loc[float_only, "p_top250"].sum()), 2),
                                      "market_cap_first_binning": round(float(settled.loc[float_only,
                                                                                          "p_top250_mcap_first"].sum()), 2)}}
    return out


def blocks_week(rows: pd.DataFrame) -> pd.Series:
    """Missing name-weeks that make a week incomplete: dollar-volume evidence at or above the rank-250
    cut, only a proxy that reaches the band median, or no evidence at all (unknown)."""
    proxy_only = rows["proxy_above"] & ~rows["pf_dv_ok"] & ~rows["pf_price_low"]
    return rows["pf_ge_cut250"] | proxy_only | (rows["evidence"] == "unknown")


def after_pending_ex_siblings(summary: pd.DataFrame, listed: pd.DataFrame) -> pd.Series:
    """``complete_250_after_pending`` with missing classes whose sibling class ranks that week also set
    aside (owner question 8.4: if only the most liquid class is kept), per summary row."""
    miss = listed[listed["missing"] & ~listed["missing_reason"].isin(PENDING_REASONS) & ~listed["sibling_priced"]]
    blocked = blocks_week(miss).groupby(miss["week_index"]).any().reindex(range(len(summary)), fill_value=False)
    ranked_and_closed = (summary["n_ranked_dv50"] >= TOP_N) & (summary["top250_close_in_week"] == TOP_N)
    return pd.Series(ranked_and_closed.to_numpy() & ~blocked.to_numpy(dtype=bool), index=summary.index)


def by_year(summary: pd.DataFrame, listed: pd.DataFrame, top: pd.DataFrame) -> dict:
    s = summary.assign(year=summary["week_end"].str[:4].astype(int),
                       ex_siblings=after_pending_ex_siblings(summary, listed))
    miss = listed[listed["missing"]]
    out = {}
    for year, g in s.groupby("year"):
        slots = TOP_N * len(g)
        my = miss[miss["week_end"].dt.year == year]
        est = float(my["p_top250"].sum())
        settled = my[~my["missing_reason"].isin(PENDING_REASONS)]
        residual = float(settled["p_top250"].sum())
        unfill = float(my.loc[my["missing_reason"] == "unfillable", "p_top250"].sum())
        lone = float(settled.loc[~settled["sibling_priced"], "p_top250"].sum())
        blind = settled[~settled["pf_dv_ok"] & ~settled["pf_price_low"]]
        bins = pd.Series(evidence_bins(settled), index=settled.index)
        six = check_6(settled, my, slots)
        blockers = settled[blocks_week(settled)]
        blockers = (blockers.groupby(["ticker", "missing_reason", "evidence"]).size().sort_values(ascending=False)
                    .head(12))
        t = top[(top["week_end"].str[:4].astype(int) == year) & (top["dv50_rank"] <= TOP_N)]
        out[int(year)] = {
            "weeks": int(len(g)),
            "complete_250_share": round(float((g["complete_250"] == "Y").mean()), 4),
            "complete_250_strict_share": round(float((g["complete_250_strict"] == "Y").mean()), 4),
            "complete_250_after_pending_share": round(float((g["complete_250_after_pending"] == "Y").mean()), 4),
            "complete_250_after_pending_ex_sibling_classes_share": round(float(g["ex_siblings"].mean()), 4),
            "weeks_blocked_by": {  # a week can be blocked by several conditions
                "complete_250": {"ranked_lt_250": int((g["n_ranked_dv50"] < TOP_N).sum()),
                                 "top250_not_all_closed": int((g["top250_close_in_week"] < TOP_N).sum()),
                                 "dv_ge_cut250": int((g["n_missing_pf_dv_ge_cut250"] > 0).sum()),
                                 "proxy_only_ge_band_median": int((g["n_missing_proxy_above_no_pf_dv"] > 0).sum()),
                                 "unknown": int((g["n_missing_unknown"] > 0).sum())},
                "complete_250_after_pending": {
                    "dv_ge_cut250": int((g["n_missing_pf_dv_ge_cut250_not_pending"] > 0).sum()),
                    "proxy_only_ge_band_median": int((g["n_missing_proxy_above_no_pf_dv_not_pending"] > 0).sum()),
                    "unknown": int((g["n_missing_unknown_not_pending"] > 0).sum())}},
            "after_pending_blockers_name_weeks": {f"{t}:{r}:{e}": int(n) for (t, r, e), n in blockers.items()},
            "weeks_ranked_lt_250": int((g["n_ranked_dv50"] < TOP_N).sum()),
            "top250_close_in_week_min": int(g["top250_close_in_week"].min()),
            "top250_close_in_week_share": round(float(g["top250_close_in_week"].sum() / slots), 5),
            "top300_close_in_week_share": round(float(g["top300_close_in_week"].sum() / (PRICE_RANK * len(g))), 5),
            "top250_close_on_week_end_share": round(float(g["top250_close_on_week_end"].sum() / slots), 5),
            "missing_name_weeks": int(g["n_missing"].sum()),
            "missing_by_reason": {r: int(g[f"n_missing_{r}"].sum()) for r in MISSING_REASONS},
            "missing_pf_dv_ge_cut250_name_weeks": int(g["n_missing_pf_dv_ge_cut250"].sum()),
            "missing_pf_dv_ge_cut300_name_weeks": int(g["n_missing_pf_dv_ge_cut300"].sum()),
            "proxy_check_1": {"weeks_zero": int((g["n_missing_proxy_above"] == 0).sum()),
                              "share_weeks_zero": round(float((g["n_missing_proxy_above"] == 0).mean()), 4),
                              "max": int(g["n_missing_proxy_above"].max()),
                              "single_class_share_weeks_zero": round(float((g["n_missing_proxy_above_single_class"] == 0).mean()), 4),
                              "single_class_max": int(g["n_missing_proxy_above_single_class"].max()),
                              "no_pf_dv_weeks_zero_share": round(float((g["n_missing_proxy_above_no_pf_dv"] == 0).mean()), 4),
                              "no_pf_dv_max": int(g["n_missing_proxy_above_no_pf_dv"].max()),
                              "pass": bool((g["n_missing_proxy_above"] == 0).mean() >= 0.95
                                           and g["n_missing_proxy_above"].max() <= 3)},
            "est_missing_top250_name_weeks": round(est, 1),
            "est_missing_top250_share_of_slots": round(est / slots, 5),
            "residual_survivorship_name_weeks": round(residual, 1),
            "residual_survivorship_share_of_slots": round(residual / slots, 5),
            "residual_ex_sibling_classes_name_weeks": round(lone, 1),
            "residual_ex_sibling_classes_share_of_slots": round(lone / slots, 5),
            "residual_upper_name_weeks": round(residual + six["unknown_name_weeks"], 1),
            "residual_by_reason": {r: round(float(v), 1) for r, v in settled.groupby("missing_reason")["p_top250"].sum().items()},
            "unknown_name_weeks_by_reason": {r: int(v) for r, v in
                                             settled.loc[settled["evidence"] == "unknown", "missing_reason"].value_counts().items()},
            "residual_by_evidence": {b: {"name_weeks": int((bins == b).sum()),
                                         "est_top250_name_weeks": (round(float(settled.loc[bins == b, "p_top250"].sum()), 1)
                                                                   if b != "unknown" else None)}
                                     for b in EVIDENCE_BINS},
            "no_step6_dv": {"name_weeks": int(len(blind)),
                            "proxy_ratio_ge_0_5": int((blind["proxy_ratio"] >= 0.5).sum()),
                            "proxy_ratio_ge_1": int((blind["proxy_ratio"] >= 1.0).sum()),
                            "no_proxy": int(blind["proxy_ratio"].isna().sum()),
                            "est_top250_name_weeks": round(float(blind["p_top250"].sum()), 1)},
            "unknown_name_weeks_all_reasons": int((my["evidence"] == "unknown").sum()),
            "dv_stored_direct_name_weeks": int((my["dv_stored_direct"] & my["pf_dv_ok"]).sum()) if "dv_stored_direct" in my else 0,
            "unfillable_est_name_weeks": round(unfill, 1),
            "unfillable_share_of_slots": round(unfill / slots, 5),
            "check_6": six,
            "check_6_pass": six["pass"],
            "mcap_weighted_coverage_min": float(g["mcap_weighted_coverage"].min()),
            "mcap_weighted_coverage_median": float(g["mcap_weighted_coverage"].median()),
            "mcap_weighted_coverage_known_dv_min": float(g["mcap_weighted_coverage_known_dv"].min()),
            "mcap_weighted_coverage_target": 0.97 if year < 2023 else 0.99,
            "snapshot_age_max": int(g["snapshot_age_days"].max()),
            "snapshot_stale_weeks": int((g["snapshot_stale"] == "Y").sum()),
            "pf_cut250_ratio": {"median": float(g["pf_cut250_ratio"].median()), "min": float(g["pf_cut250_ratio"].min()),
                                "max": float(g["pf_cut250_ratio"].max())},
            "top250_overlap_prefilter_median": float(g["top250_overlap_prefilter"].median()),
            "top250_overlap_prefilter_min": int(g["top250_overlap_prefilter"].min()),
            "top250_ff49_share": round(float((t["ff49"] != "").mean()), 5) if len(t) else None,
            "top250_earnings_flag_share": round(float((t["earnings_event_within_3_sessions"] == "Y").mean()), 4) if len(t) else None,
            "top250_unknown_foreign_flag_name_weeks": int((t["foreign_filer"] == "UNKNOWN").sum()),
        }
    return out


def missing_by_security(listed: pd.DataFrame, master: pd.DataFrame, spans: pd.DataFrame) -> pd.DataFrame:
    """One row per security with missing name-weeks: when, why, and the evidence of top-250 membership."""
    miss = listed[listed["missing"]].assign(
        no_pf_dv=lambda f: ~f["pf_dv_ok"] & ~f["pf_price_low"],
        no_pf_dv_half=lambda f: ~f["pf_dv_ok"] & ~f["pf_price_low"] & (f["proxy_ratio"] >= 0.5),
        unknown=lambda f: f["evidence"] == "unknown",
        stored_direct=lambda f: (f["dv_stored_direct"] if "dv_stored_direct" in f else False) & f["pf_dv_ok"],
        not_in_step6=lambda f: ~f["in_pf"])
    g = miss.groupby("security_id")
    table = pd.DataFrame({
        "ticker": g["ticker"].last(),
        "missing_reason": g["missing_reason"].agg(lambda r: r.value_counts().index[0]),  # the reason of most weeks
        "reasons_all": g["missing_reason"].agg(lambda r: " ".join(sorted(set(r)))),
        "missing_weeks": g.size(), "first_missing": g["week_end"].min().dt.strftime("%Y-%m-%d"),
        "last_missing": g["week_end"].max().dt.strftime("%Y-%m-%d"),
        "weeks_pf_dv_ge_cut250": g["pf_ge_cut250"].sum(), "weeks_pf_dv_ge_cut300": g["pf_ge_cut300"].sum(),
        "weeks_with_pf_dv": g["pf_dv_ok"].sum(), "weeks_no_pf_dv": g["no_pf_dv"].sum(),
        "weeks_no_pf_dv_proxy_ge_half": g["no_pf_dv_half"].sum(), "weeks_proxy_above": g["proxy_above"].sum(),
        "weeks_sibling_priced": g["sibling_priced"].sum(),
        "weeks_unknown": g["unknown"].sum(), "weeks_dv_stored_direct": g["stored_direct"].sum(),
        "weeks_not_in_step6": g["not_in_step6"].sum(),
        "best_pf_dv50_rank": g["pf_dv50_rank"].min(), "max_proxy_ratio": g["proxy_ratio"].max().round(3),
        "max_dv_ratio": g["dv_ratio"].max().round(3),
        "est_top250_weeks": g["p_top250"].sum().round(2), "has_series": g["has_series"].any(),
        "weeks_proxy_class_capped": (g["proxy_class_capped"].sum() if "proxy_class_capped" in miss
                                     else pd.Series(0, index=g.size().index)),
    })
    last_listed = spans.groupby("security_id")["list_end"].max()
    table["active_now"] = table.index.map(last_listed).fillna("") >= LAST_WEEK
    info = master.set_index("security_id")
    for column in ("cik", "name", "delist_date", "foreign_filer", "multi_class_group"):
        table[column] = table.index.map(info[column])
    return table.sort_values(["est_top250_weeks", "weeks_pf_dv_ge_cut300"], ascending=False).reset_index()


def month2_leads(missing: pd.DataFrame, candidates: pd.DataFrame, fetch_status: pd.DataFrame) -> pd.DataFrame:
    """Securities the checks point to (plan step 12's list for month 2): missing weeks with step-6 dollar
    volume at the rank-300 cut, or with no step-6 dollar volume and a proxy at half the band median or more
    in 4+ weeks, or a proxy above it, or a name still listed with 4+ unknown weeks (no series and no size
    proxy; Yahoo serves it without a Tiingo symbol); with what the fetch lists already say and the source
    that would serve them (Yahoo for a name still listed, which costs no Tiingo symbol; none for a
    ``short_window`` name, whose series already holds the week). Unknown names no longer listed are counted
    in the summary, not listed here (each would cost a Tiingo symbol)."""
    dv = missing["weeks_pf_dv_ge_cut300"] > 0
    proxy = (missing["weeks_no_pf_dv_proxy_ge_half"] >= 4) | ((missing["weeks_no_pf_dv"] > 0) & (missing["weeks_proxy_above"] > 0))
    unknown = (missing["weeks_unknown"] >= 4) & missing["active_now"] if "weeks_unknown" in missing else False
    leads = missing[dv | proxy | unknown].copy()
    leads["lead_rule"] = np.select([dv[leads.index], proxy[leads.index]], ["dv_ge_cut300", "proxy"], default="unknown_active")
    planned = candidates.drop_duplicates("security_id").set_index("security_id")
    leads["in_candidates"] = leads["security_id"].isin(planned.index)
    leads["planned_source"] = leads["security_id"].map(planned["planned_source"]).fillna("")
    leads["candidate_reason"] = leads["security_id"].map(planned["reason"]).fillna("")
    status = (fetch_status.sort_values("updated_utc", kind="stable").drop_duplicates("security_id", keep="last")
              .set_index("security_id")["status"] if len(fetch_status) else pd.Series(dtype=str))
    leads["tiingo_status"] = leads["security_id"].map(status).fillna("")
    leads["suggested_source"] = np.select(
        [leads["missing_reason"].isin(PENDING_REASONS), leads["missing_reason"] == "answer_not_in_panel",
         leads["missing_reason"] == "short_window", leads["active_now"].astype(bool)],
        ["pending", "step9_has_it", "no_fetch_short_window", "yahoo"], default="tiingo")
    return leads


def write_csv(path: Path, frame: pd.DataFrame, compress: bool = False) -> None:
    data = frame.to_csv(index=False, float_format="%.10g").encode("utf-8")
    common.atomic_write(path, gzip.compress(data, mtime=0) if compress else data)


def write_json(path: Path, payload) -> None:
    common.atomic_write(path, (json.dumps(payload, indent=1, sort_keys=False, default=str) + "\n").encode("utf-8"))


def assert_no_vendor_values(top: pd.DataFrame, summary: pd.DataFrame) -> None:
    """The committed files carry ranks, flags, counts, buckets and ratios only."""
    banned = {"dv20", "dv50", "close", "close_raw", "volume", "volume_raw", "tr", "mcap", "float_usd", "proxy",
              "pf_dv50", "pf_dv20", "cut250", "cut300"}
    for frame, name in ((top, "top300"), (summary, "summary")):
        bad = banned & set(frame.columns)
        if bad:
            raise AssertionError(f"{name} would commit vendor values: {sorted(bad)}")


# ------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--tiingo-status", type=Path, default=TIINGO_STATUS,
                        help="the Tiingo fetch status file to read (default: the running fetch's own); a fixed "
                             "copy makes two runs comparable while the fetch is still writing")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="write every output (the two INPUTS files included) under this directory instead of "
                             "the published locations, e.g. to compare a rerun with the published build")
    parser.add_argument("--fetch-sec-submissions", action="store_true",
                        help="before the build, fetch the SEC submissions files the cache lacks for the listed CIKs "
                             f"(main files; older pages of CIKs with investment-company evidence), at "
                             f"{SEC_FETCH_PER_SECOND} requests a second; the build itself reads local files only")
    args = parser.parse_args(argv)
    started = datetime.now(timezone.utc)
    out_dir = Path(args.out_dir) if args.out_dir else OUT
    top300_file = out_dir / TOP300_FILE.name if args.out_dir else TOP300_FILE
    summary_file = out_dir / SUMMARY_FILE.name if args.out_dir else SUMMARY_FILE
    out_dir.mkdir(parents=True, exist_ok=True)

    log("calendar")
    sessions, weeks, week_pos, prev_pos = calendar()
    log(f"{len(sessions)} sessions, {len(weeks)} weeks {weeks[0].date()}..{weeks[-1].date()}")

    master = read_csv_text(MASTER)
    intervals, doubtful = drop_doubtful_intervals(load_intervals(), master)
    log(f"master {len(master)} securities, {len(intervals)} Nasdaq intervals; left out as one ticker-only snapshot "
        f"row against a non-Nasdaq SEC exchange: {[(d['security_id'], d['ticker'], d['start']) for d in doubtful]}")

    log("canonical panel (close_raw, volume_raw only)")
    panel = load_panel()
    panel_sha = common.sha256_file(PANEL)
    log(f"panel rows {len(panel)}, securities {panel['security_id'].nunique()}")
    # Rows step 9 kept before a listing that belong to another company under the reused ticker (TRUE).
    untrimmed_first = first_rows(panel, sessions)
    spans_raw = listing_spans(intervals, master, untrimmed_first, sessions=sessions)
    trims = boundary_trims(spans_raw, master, untrimmed_first, sessions, stored_floor_dates(master))
    rows_before = len(panel)
    panel = trim_panel(panel, trims)
    log(f"mapping-boundary trims {len(trims)} ({', '.join(f'{s}>={d}' for s, d in sorted(trims.items()))}): "
        f"{rows_before - len(panel)} canonical rows not used")
    metrics = canonical_metrics(panel, sessions, week_pos, prev_pos)
    log("canonical medians and week closes done")

    spans = listing_spans(intervals, master, metrics["first_row"], sessions=sessions)
    foreign, foreign_facts = pf.foreign_spans(master, PERIODIC_HISTORY)
    shells = pf.spac_shells(master, intervals, SIC_HISTORY)
    shells_now, shells_like_ending = listed_now_shells(spans, master, intervals, shells)
    shells.update({sid: "spac_like_now" for sid in shells_now})
    log(f"spans {len(spans)} (IPO-rule starts {int(spans['ipo_start'].sum())}, at the mapping boundary "
        f"{int(spans['ipo_boundary'].sum())}, cut by Form 25 "
        f"{int((spans['cut'] == 'form25').sum())}, by transfer {int((spans['cut'] == 'transfer').sum())}, "
        f"after a cut {int(spans['after_cut'].sum())}); SPAC shells {len(shells)} (listed now, by step 6's "
        f"spac_like_now: {shells_now}; shell-like names in the last week whose listing ends before "
        f"{WINDOW_END}, kept: {shells_like_ending})")

    sic_history = read_csv_text(SIC_HISTORY)
    listed_ids = set(spans["security_id"])
    sec_fetch = {}
    if args.fetch_sec_submissions:
        log(f"fetching missing SEC submissions files at {SEC_FETCH_PER_SECOND} requests a second")
        sec_fetch = fetch_missing_submissions(master, listed_ids)
        log(f"SEC submissions: {sec_fetch['requests_or_cache_reads']} files fetched, failed {sec_fetch['failed']}")
    log("investment companies from the cached SEC submissions")
    terminal = read_csv_text(TERMINAL)
    # Owner convention (2026-10-02): a 1:1 reorganisation continues the security, so the successor's
    # dollar-volume windows read the predecessor's rows before the handover (the closes stay its own).
    links = successor_links(master, spans, metrics["first_row"], terminal)
    metrics = canonical_metrics(panel, sessions, week_pos, prev_pos, links=links)
    crossed = {s: f for s, f in metrics["successor_windows"].items() if f.get("predecessor_rows_in_first_window")}
    from_step11 = links[links["link_source"] == LINK_SOURCES[1]]
    log(f"successor links {len(links)} ({links['link_source'].value_counts().to_dict()}; step 11's: "
        f"{', '.join(a + '->' + b for a, b in zip(from_step11['predecessor_id'], from_step11['successor_id']))}): "
        f"continuing {int(links['continuing'].sum())} "
        f"({links['continuing_basis'].value_counts().to_dict()}), 1:1 by step 11 {int(links['one_to_one'].sum())}, "
        f"windows run across {len(crossed)} ({', '.join(sorted(crossed))})")
    ends = listing_ends(spans, master, terminal)
    investment, ic_issuers, ic_facts = investment_companies(master, listed_ids, sic_history, ends=ends)
    log(f"issuers with investment-company evidence {len(ic_issuers)}, securities with spans {len(investment)}; "
        f"CIKs read {ic_facts['ciks']}, main files missing {len(ic_facts['main_missing'])}, older pages not cached "
        f"{ic_facts['pages_missing']} (of CIKs with evidence: {len(ic_facts['ic_ciks_with_pages_missing'])}); "
        f"submissions files read {ic_facts['files_read']}, looked for and missing {ic_facts['files_missing']}, "
        f"digest {ic_facts['digest'][:12]}; spans carried to a delisting (merger tail): {ic_facts['merger_tail_ciks']}")

    listed = weekly_listed(spans, weeks, foreign, shells, investment)
    newness = new_listing_evidence(spans, master, sessions, ic_facts.get("prospectus"), links=links,
                                   first_row=metrics["first_row"])
    listed = attach_new_listing(listed, spans, newness, sessions, week_pos)
    log(f"first listing runs by new-listing basis: {newness['new_listing_basis'].value_counts().to_dict()}")
    info = master.set_index("security_id")
    listed["foreign_filer"] = listed["security_id"].map(info["foreign_filer"]).fillna("")
    listed["multi_class"] = listed["security_id"].map(info["multi_class_group"]).fillna("").ne("")
    listed["cik"] = listed["security_id"].map(info["cik"]).fillna("")
    log(f"listed security-weeks {len(listed)}, universe base {int(listed['eligible'].sum())}, investment-company "
        f"weeks left out {int((listed['investment_company'] & ~listed['non_common']).sum())}")
    listed = rank_weeks(attach_metrics(listed, metrics))
    log(f"ranked dv50 name-weeks {int(listed['dv50_rank'].notna().sum())}")

    log("step-6 weekly metrics")
    weekly_metrics = read_stable(WEEKLY_METRICS, pd.read_pickle)
    listed = join_prefilter(listed, weekly_metrics)
    # Listed names step 6 never saw (later listings after a Form 25 cut): dollar volume from the stored
    # files the master names for them, and market cap / XBRL float as step 6 attaches them.
    direct = stored_direct_dv(listed, spans, master, sessions, week_pos)
    listed = apply_stored_direct(listed, direct)
    lists = read_csv_text(LISTS)
    lists["market_cap"] = pd.to_numeric(lists["market_cap"], errors="coerce")
    facts = pf.float_facts(offline=True)
    facts = facts[0] if isinstance(facts, tuple) else facts  # newer step-6 code returns (facts, unit decisions)
    floats, float_drops = pf.float_price_check(facts, weekly_metrics, master)
    listed = direct_proxies(listed, master, lists, floats)
    not_in_pf = listed["eligible"] & ~listed["in_pf"]
    log(f"name-weeks step 6 has no row for {int(not_in_pf.sum())}: stored-direct dv50 "
        f"{int((not_in_pf & listed['pf_dv50'].notna()).sum())}, proxy {int(listed['proxy_direct'].sum())}")
    candidates = read_csv_text(CANDIDATES)
    unfillable = read_csv_text(UNFILLABLE)
    no_series = read_csv_text(NO_SERIES) if NO_SERIES.exists() else pd.DataFrame(columns=["security_id", "reason"])
    status_path = Path(args.tiingo_status)
    fetch_status = read_csv_text(status_path) if status_path.exists() else pd.DataFrame(
        columns=["security_id", "status", "updated_utc", "first_date", "last_date", "fetched_utc"])
    status_sha = common.sha256_file(status_path) if status_path.exists() else ""
    yahoo_report = read_csv_text(YAHOO_REPORT) if YAHOO_REPORT.exists() else None
    yahoo_status = read_csv_text(YAHOO_STATUS) if YAHOO_STATUS.exists() else None
    # Answers newer than the panel, Tiingo's and Yahoo's: step 9 has to be rerun to read them.
    answers = pd.concat([fetch_status.reindex(columns=FETCH_STATUS_COLUMNS).fillna(""),
                         yahoo_answers(yahoo_report, yahoo_status)], ignore_index=True)
    # Per week without unfillable.csv (its windows are applied per week in mark_missing); the view with
    # it serves the checks. Pending reasons hold only in the need windows of their candidate rows.
    series_ids = set(metrics["ids"])
    week_reasons = MissingReasons(master, candidates, unfillable.iloc[0:0], no_series, fetch_status, series_ids,
                                  yahoo_report)
    reasons = MissingReasons(master, candidates, unfillable, no_series, fetch_status, series_ids, yahoo_report)
    cut = week_cutoffs(listed)
    panel_built = datetime.fromtimestamp(PANEL.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    relist_table = read_csv_text(RELIST_TABLE) if RELIST_TABLE.exists() else None
    gaps = relist_gaps(relist_table)
    listed = mark_missing(listed, spans, week_reasons, cut, answers, panel_built, windows_of(unfillable), gaps)
    gap_weeks = listed[listed["relist_gap"] & listed["eligible"]]
    log(f"relist gaps (step 9's documented junctions) {len(gaps)}: base name-weeks outside trading "
        f"{len(gap_weeks)} {gap_weeks.groupby('ticker').size().to_dict()}; short_window name-weeks "
        f"{int((listed['missing_reason'] == 'short_window').sum())}")
    rates = calibrate(listed)
    listed["p_top250"] = 0.0
    listed["p_top250_low"] = 0.0
    miss_index = listed.index[listed["missing"]]
    listed.loc[miss_index, "p_top250"] = expected_top250(listed.loc[miss_index], rates)
    listed.loc[miss_index, "p_top250_low"] = expected_top250(listed.loc[miss_index], rates, proxy_bin0=0.0)
    listed["p_top250_uncapped"] = 0.0      # not written out: the class cap's effect, for the summary
    listed.loc[miss_index, "p_top250_uncapped"] = expected_top250(
        listed.loc[miss_index].drop(columns="proxy_class_capped"), rates)
    # Not written out either: the expected count with the plan's market-cap-first proxy binning, beside check 6.
    alt = listed[CALIBRATION_COLUMNS].assign(proxy_ratio=proxy_ratio_mcap_first(listed, cut))
    listed["p_top250_mcap_first"] = 0.0
    listed.loc[miss_index, "p_top250_mcap_first"] = expected_top250(alt.loc[miss_index], calibrate(alt))
    del alt
    log(f"proxy binning: expected top-250 {listed['p_top250'].sum():.1f} by the either-one ratio, "
        f"{listed['p_top250_mcap_first'].sum():.1f} by the market-cap-first ratio; float-only proxy_above "
        f"name-weeks {int((listed['missing'] & listed['proxy_above_float_only']).sum())}")
    log(f"class cap: {int(listed['proxy_class_capped'].sum())} proxy-only name-weeks of a class whose own dv50 "
        f"within {CLASS_DV_WEEKS} weeks is under {CLASS_DV_CAP_RATIO} of the cut; expected top-250 "
        f"{listed['p_top250_uncapped'].sum():.1f} -> {listed['p_top250'].sum():.1f}")
    evidence_counts = listed.loc[miss_index, "evidence"].value_counts().to_dict()
    log(f"missing name-weeks {len(miss_index)} by evidence {evidence_counts}; expected top-250 among those with "
        f"evidence {listed['p_top250'].sum():.1f}")
    pending_facts = pending_window_facts(listed, week_reasons)
    log(f"pending reasons per week: {pending_facts['fell_through_by_reason']} security-level pending name-weeks "
        f"fell outside every need window of that source")
    ic_table = investment_company_report(listed, ic_issuers, investment, cut)
    log(f"investment companies left out: {len(ic_table)} securities, {int(ic_table['weeks_excluded'].sum())} "
        f"name-weeks, former top-250 name-weeks {int(ic_table['former_top250_name_weeks'].sum())}, former blocking "
        f"missing name-weeks {int(ic_table['former_blocking_name_weeks'].sum())}")

    # Series that end within 4 weeks after a top-250 week, and whether a terminal value is settled.
    settled = set(terminal.loc[terminal["status"].isin(["computed", "no_terminal_return"]), "security_id"])
    last = pd.to_datetime(listed["last_row"].replace("", None))
    listed["ends_within_4w"] = ((last > listed["week_end"]) & (last <= listed["week_end"] + pd.Timedelta(days=28))
                                & (last < pd.Timestamp(WINDOW_END) - pd.Timedelta(days=5))).fillna(False).astype(bool)
    listed["terminal_unresolved"] = ~listed["security_id"].isin(settled)

    log("industry and earnings for the top 300")
    maps = read_csv_text(FF_MAPS)
    events = read_csv_text(EARNINGS, usecols=["cik", "security_id", "d0_session", "event_kind"])
    top = top300_table(listed, master, sic_history, maps, events, sessions, week_pos)
    log(f"top-300 rows {len(top)}")

    ages = snapshot_ages(weeks, read_csv_text(SNAPSHOT_INDEX))
    summary = weekly_summary(listed, cut, weeks, ages, top)
    assert_no_vendor_values(top, summary)
    years = by_year(summary, listed, top)

    log("plan 3.3 checks 2-5")
    unfillable_ids = set(unfillable["security_id"]) | {s for s, r in reasons.items() if r == "unfillable"}
    recent = recent_fetches(answers, panel_built)
    capture = capture_coverage(lists, spans, foreign, shells, panel, sessions, unfillable_ids, master, reasons, recent,
                               listed[["security_id", "week_end", "pf_src"]], investment)
    members = json.loads(NASDAQ100.read_text()) if NASDAQ100.exists() else {}
    n100 = nasdaq100_check(members, spans, master, foreign, panel, sessions, reasons, recent, investment)
    universe6 = weekly_metrics[weekly_metrics["universe"]]
    best6 = universe6[["dv50_rank", "dv20_rank"]].min(axis=1).groupby(universe6["security_id"]).min().to_dict()
    f25 = form25_check(read_csv_text(FORM25), master, metrics["last_row"], sessions, reasons, foreign, shells, spans,
                       recent, best6, investment)
    margin = fetch_margin(candidates, listed)

    log("writing outputs")
    write_csv(top300_file, top[TOP300_COLUMNS], compress=True)
    write_csv(summary_file, summary)
    keep = ["week_end", "security_id", "ticker", "eligible", "non_common", "spac_shell", "foreign", "investment_company",
            "foreign_filer",
            "has_series", "in_week", "on_week_end", "lag", "price_ge_10", "dv50_rank", "dv20_rank",
            "dv50_rank_any_price", "dv20_rank_any_price", "outside_trading", "young", "young_rule", "new_listing_basis",
            "missing", "missing_reason",
            "evidence", "in_pf", "pf_universe", "pf_dv50_rank", "pf_price", "pf_src", "pf_ge_cut250", "pf_ge_cut300",
            "proxy_above", "proxy_class_capped", "p_top250"]
    out_listed = listed[keep].assign(week_end=listed["week_end"].dt.strftime("%Y-%m-%d"))
    write_csv(out_dir / "weekly_listed.csv.gz", out_listed, compress=True)
    liquid = listed[listed["has_series"] & (listed["dv50"].notna() | listed["close"].notna())]
    write_csv(out_dir / "weekly_liquidity.csv.gz",
              liquid[["week_end", "security_id", "ticker", "dv20", "dv50", "close", "lag", "dv50_rank", "dv20_rank"]]
              .assign(week_end=liquid["week_end"].dt.strftime("%Y-%m-%d")), compress=True)
    cut_out = cut.reindex(pd.RangeIndex(len(weeks))).assign(week_end=weeks.strftime("%Y-%m-%d"))
    write_csv(out_dir / "cutoffs.csv", cut_out.reset_index(drop=True))
    missing = missing_by_security(listed, master, spans)
    write_csv(out_dir / "missing_by_security.csv", missing)
    leads = month2_leads(missing, candidates, fetch_status)
    write_csv(out_dir / "month2_leads.csv", leads)
    write_csv(out_dir / "capture_coverage.csv", capture)
    write_csv(out_dir / "nasdaq100_check.csv", n100)
    write_csv(out_dir / "form25_check.csv", f25)
    write_csv(out_dir / "investment_companies.csv", ic_table)
    ic_spans_file = out_dir / "investment_company_spans.csv"
    write_csv(ic_spans_file, investment_span_rows(investment, master))
    submissions_file = out_dir / SUBMISSIONS_TABLE
    write_csv(submissions_file, pd.DataFrame(sorted(ic_facts["files"].items()), columns=["name", "sha256"]),
              compress=True)
    completeness = completeness_table(years)
    write_csv(out_dir / "completeness_by_year.csv", completeness)

    payload = {
        "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runtime_seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        "code_version": CODE_VERSION,
        "no_returns": "no return of any kind is read or computed: the panel's tr column is not loaded; "
                      "ranks are by median raw close x raw volume only",
        "definitions": {"week_end": "last XNAS session of the calendar week", "weeks": len(weeks),
                        "dv_windows": DV_WINDOWS, "close_stale_sessions": CLOSE_STALE_SESSIONS,
                        "min_price": MIN_PRICE, "top_n": TOP_N, "price_rank": PRICE_RANK, "proxy_band": BAND,
                        "earnings_window_sessions": EARNINGS_WINDOW, "stale_snapshot_days": STALE_SNAPSHOT_DAYS,
                        "evidence": "a missing name-week's evidence of its top-250 status: price_lt_10 (step 6's "
                                    "raw close under $10), dv (step-6 dv50, or for names step 6 has no row for the "
                                    "stored-file dv50, against the canonical rank-250 cut), proxy (market cap or "
                                    "float against the median of canonical ranks 200-250; the proxy ratio is the "
                                    "larger of the two, the either-one reading of proxy_above), unknown (none: never "
                                    "taken as small). In the expected count a class of a multi-class company with "
                                    f"only a proxy whose own dv50 within {CLASS_DV_WEEKS} weeks is under "
                                    f"{CLASS_DV_CAP_RATIO} of the cut takes the dv rule's lowest bin "
                                    "(proxy_class_capped)",
                        "complete_250": "n_ranked_dv50 >= 250, all ranks 1-250 have a canonical close in the "
                                        "week, and no missing name with dv50 evidence >= the canonical rank-250 cut, "
                                        "none with only a proxy that reaches the band median, none unknown",
                        "complete_250_strict": f"complete_250 and est_missing_top250 < {STRICT_EXPECTED_LIMIT} "
                                               "(fewer than one expected missing top-250 name, all reasons)",
                        **pending_definitions(),
                        "young": young_definition(),
                        "universe_base": "Nasdaq common stock (CRSP share codes 10/11): no non-common listed name, no "
                                         "unmerged SPAC shell (spac_shells, plus spac_like_now for shells listed now), "
                                         "no foreign filer that week, no investment company that week (closed-end "
                                         "funds and BDCs, from SEC filings); every share class kept",
                        "investment_company": investment_company_definition(),
                        "check_6": "plan 3.3 check 6 on the residual of every non-pending reason, unknown name-weeks "
                                   "counted as top-250 name-weeks (pass = upper share <= 2% of slots); the "
                                   "unfillable-only share is kept beside it. The expected count (p_top250) bins a "
                                   "proxy-only name-week by the larger of market cap / band median market cap and "
                                   "float / band median float, so a float-only week (proxy_above by its float alone, "
                                   "n_missing_proxy_above_float_only) sits in the bin of its float, as proxy_above "
                                   "reads it; check_6.market_cap_first_binning gives each year's shares with the "
                                   "plan's market-cap-first binning and the float-only name-weeks' expected count "
                                   "under both (completeness_by_year.csv: residual_mcap_first)",
                        "successor_link": successor_link_definition(),
                        "earnings_nearest_d0_offset": "D0 session minus the week-end session: positive = D0 after "
                                                      "the week end"},
        "inputs_sha256": {str(p): common.sha256_file(p) for p in (
            MASTER, INTERVALS, PERIODIC_HISTORY, SNAPSHOT_INDEX, FORM25, CANDIDATES, UNFILLABLE, EARNINGS,
            SIC_HISTORY, FF_MAPS, TERMINAL, WEEKLY_METRICS, LISTS, NO_SERIES, YAHOO_REPORT, YAHOO_STATUS, RELIST_TABLE)
            if Path(p).exists()}
        | {str(PANEL): panel_sha, f"tiingo_status ({status_path})": status_sha,
           f"{SUBMISSIONS_DIGEST_KEY} ({SUBMISSIONS_DIR})": ic_facts["digest"]},
        "panel_built_utc": panel_built,
        "outputs_sha256": {str(top300_file): common.sha256_file(top300_file),
                           str(summary_file): common.sha256_file(summary_file),
                           str(out_dir / "weekly_listed.csv.gz"): common.sha256_file(out_dir / "weekly_listed.csv.gz"),
                           str(ic_spans_file): common.sha256_file(ic_spans_file),
                           str(submissions_file): common.sha256_file(submissions_file)},
        "panel": {"rows": int(len(panel)), "securities": int(panel["security_id"].nunique()),
                  "rows_by_source": {k: int(v) for k, v in panel["src_primary"].value_counts().items()},
                  "relist_junctions_windows_restart": metrics["relist_junctions"],
                  "relist_junction_rows": int(panel["relist_junction"].sum())},
        "listing": {"intervals": int(len(spans)), "ipo_rule_starts": int(spans["ipo_start"].sum()),
                    "doubtful_intervals_left_out": doubtful,
                    "ipo_rule_not_applied_at_mapping_boundary": int(spans["ipo_boundary"].sum()),
                    "mapping_boundary_starts_to_check": [f"{r.security_id}:{r.ticker}:{r.snapshot_start}" for r in
                                                         spans[spans["ipo_boundary"]].itertuples(index=False)],
                    "mapping_boundary_trims": {s: {"rows_from": d, "untrimmed_first_row": untrimmed_first.get(s, "")}
                                               for s, d in sorted(trims.items())},
                    "mapping_boundary_trimmed_rows": int(rows_before - len(panel)),
                    "top300_rows_dv_window_before_listing": int((top["dv_window_before_listing"] == "Y").sum()),
                    "relist_gap_weeks_outside_trading": {
                        "junctions": {sid: [list(g) for g in v] for sid, v in sorted(gaps.items())},
                        "base_name_weeks": int(len(gap_weeks)),
                        "by_security": {f"{s}:{t}": int(n) for (s, t), n in
                                        gap_weeks.groupby(["security_id", "ticker"]).size().items()},
                        "note": "weeks with no canonical close between the old shares' last Nasdaq session and "
                                "the new shares' first (step 9's relist_junctions.csv, read from SEC filings): "
                                "outside trading, not missing"},
                    "cut_by_form25": int((spans["cut"] == "form25").sum()),
                    "cut_by_transfer": int((spans["cut"] == "transfer").sum()),
                    "intervals_after_a_cut": int(spans["after_cut"].sum()),
                    "intervals_after_a_cut_ids": sorted(set(spans.loc[spans["after_cut"], "security_id"]))[:100],
                    "spac_shells": len(shells), "foreign": {k: (v if not isinstance(v, list) else len(v))
                                                            for k, v in foreign_facts.items()},
                    "listed_security_weeks": int(len(listed)), "universe_base_security_weeks": int(listed["eligible"].sum())},
        "listed_vs_step6": {
            "base_here_not_step6": int((listed["eligible"] & ~listed["pf_universe"]).sum()),
            "base_here_not_listed_step6": int((listed["eligible"] & ~listed["in_pf"]).sum()),
            "step6_universe_not_base_here": int(len(weekly_metrics[weekly_metrics["universe"]])
                                                - (listed["eligible"] & listed["pf_universe"]).sum()),
            "ranked_here_not_step6_universe": int((listed["dv50_rank"].notna() & ~listed["pf_universe"]).sum()),
            "missing_not_in_step6": not_in_step6_facts(listed),
            "note": "listed names step 6 has no row for (the first round-5 step-6 build dropped later listings "
                    "after a Form 25 cut: SMCI relisted 2020-01, CHRD/Oasis 2020-11, CORZ 2024-01) take stored-file "
                    "dv and proxies here; a fetched answer step 9 left out of the panel is answer_not_in_panel"},
        "float_price_check_dropped": len(float_drops),
        "investment_companies": investment_company_summary(ic_table, ic_issuers, ic_facts, sec_fetch, ic_spans_file),
        "spac_shells_listed_now_left_out": {sid: master.set_index("security_id")["first_ticker"].get(sid, "")
                                            for sid in shells_now},
        "spac_like_in_last_week_kept": {
            sid: {"ticker": master.set_index("security_id")["first_ticker"].get(sid, ""),
                  "base_name_weeks": int((listed["security_id"] == sid).mul(listed["eligible"]).sum()),
                  "missing_name_weeks": int(((listed["security_id"] == sid) & listed["missing"]).sum()),
                  "top250_name_weeks": int(((listed["security_id"] == sid) & (listed["dv50_rank"] <= TOP_N)).sum()),
                  "note": "step 6's spac_like_now does not count it as listed now: its listing ends before "
                          f"{WINDOW_END} (a Form 25)"} for sid in shells_like_ending},
        "pending_per_week": pending_facts,
        "young": young_summary(listed, sessions, week_pos),
        "successor_links": successor_link_summary(listed, links, metrics["successor_windows"]),
        "proxy_class_cap": class_cap_summary(listed),
        "share_classes": "every class is kept (owner decision); after_pending_ex_sibling_classes shows the view with "
                         "missing classes whose sibling class ranks set aside",
        "no_sic_or_investment_entity_in_top250_kept": no_sic_in_top250(top, master),
        "calibration": rates_for_json(rates),
        "by_year": years,
        "overall": {
            "complete_250_share": round(float((summary["complete_250"] == "Y").mean()), 4),
            "complete_250_strict_share": round(float((summary["complete_250_strict"] == "Y").mean()), 4),
            "complete_250_after_pending_share": round(float((summary["complete_250_after_pending"] == "Y").mean()), 4),
            "top250_close_in_week_share": round(float(summary["top250_close_in_week"].sum() / (TOP_N * len(summary))), 5),
            "est_missing_top250_name_weeks": round(float(listed["p_top250"].sum()), 1),
            "residual_survivorship_name_weeks": round(float(listed.loc[~listed["missing_reason"].isin(PENDING_REASONS),
                                                                       "p_top250"].sum()), 1),
            "residual_ex_sibling_classes_name_weeks": round(float(summary["est_missing_top250_residual_ex_siblings"].sum()), 1),
            "unknown_name_weeks": int(summary["n_missing_unknown"].sum()),
            "unknown_name_weeks_not_pending": int(summary["n_missing_unknown_not_pending"].sum()),
            "residual_upper_name_weeks": round(float(summary["est_missing_top250_residual_upper"].sum()), 1),
            "missing_by_evidence": {k: int(v) for k, v in evidence_counts.items()},
            "proxy_check_1_share_weeks_zero": round(float((summary["n_missing_proxy_above"] == 0).mean()), 4),
            "proxy_check_1_max": int(summary["n_missing_proxy_above"].max()),
            "top250_vendor_rows_share": round(float(listed.loc[listed["dv50_rank"] <= TOP_N, "src"].notna().mean()), 6),
            "top250_ff49_share": round(float((top.loc[top["dv50_rank"] <= TOP_N, "ff49"] != "").mean()), 5),
            "top250_sic_basis": {k: int(v) for k, v in top.loc[top["dv50_rank"] <= TOP_N, "sic_basis"].value_counts().items()},
            "top250_without_ff49": {f"{s}:{t}": int(n) for (s, t), n in top[(top["dv50_rank"] <= TOP_N) & (top["ff49"] == "")]
                                    .groupby(["security_id", "ticker"]).size().items()},
            "top_n_candidates_close_in_week_share": {
                n: round(float(listed.loc[listed["dv50_rank"] <= n, "in_week"].mean()), 5) for n in TOP_RANGE},
        },
        "pf_top250_not_top250_here": {k: int(v) for k, v in pf_only_reasons(listed).items()},
        "check_2_capture_dates": capture_summary(capture),
        "check_3_nasdaq100": n100.groupby(["year", "status"]).size().unstack(fill_value=0).to_dict(orient="index")
        if len(n100) else {},
        "check_3_partial_or_missing": n100[n100["status"].isin(["partial", "no_series", "not_mapped"])]
        .fillna("").to_dict(orient="records") if len(n100) else [],
        "check_3_domestic_member_years_covered_share": round(float(
            (n100["status"] == "covered").sum() / n100["status"].isin(["covered", "partial", "no_series", "not_mapped"]).sum()),
            4) if len(n100) else None,
        "check_4_form25_float_ge_1b": {k: int(v) for k, v in (f25["status"] + ":" + f25["reason"].replace("", "-"))
                                       .value_counts().items()} if len(f25) else {},
        "check_5_fetch_margin": margin,
        "check_7_finra": "not run: it needs requests and this round makes none",
        "month2_leads": {"securities": int(len(leads)), "not_in_candidates": int((~leads["in_candidates"]).sum()),
                         "by_reason": {k: int(v) for k, v in leads["missing_reason"].value_counts().items()},
                         "by_suggested_source": {k: int(v) for k, v in leads["suggested_source"].value_counts().items()},
                         "by_lead_rule": {k: int(v) for k, v in leads["lead_rule"].value_counts().items()},
                         "est_top250_weeks_by_suggested_source": {
                             k: round(float(v), 1) for k, v in leads.groupby("suggested_source")["est_top250_weeks"].sum().items()},
                         "unknown_not_listed_now": {
                             "securities": int(((missing["weeks_unknown"] > 0) & ~missing["active_now"]).sum()),
                             "unknown_name_weeks": int(missing.loc[~missing["active_now"], "weeks_unknown"].sum()),
                             "note": "names with unknown weeks that are no longer listed: Tiingo would serve them "
                                     "(one symbol each); not in month2_leads.csv"}},
        "missing_securities": {"securities": int(len(missing)),
                               "by_reason": {k: int(v) for k, v in missing["missing_reason"].value_counts().items()},
                               "with_unknown_weeks": int((missing["weeks_unknown"] > 0).sum())},
    }
    write_json(out_dir / "universe_summary.json", payload)
    log("by year (complete shares of weeks; residual, unknown and upper as shares of top-250 slots):")
    for line in completeness.to_string(index=False).splitlines():
        log("  " + line)
    log(f"done in {payload['runtime_seconds']} s; outputs in {out_dir} ({top300_file.name}, {summary_file.name})")
    return 0


def completeness_table(years: dict) -> pd.DataFrame:
    """Per year: the three complete flags' share of weeks and check 6's residual, unknown and upper shares
    of top-250 slots (written to completeness_by_year.csv and printed at the end of a run)."""
    rows = []
    for year, f in years.items():
        six = f["check_6"]
        rows.append({"year": year, "weeks": f["weeks"], "complete_250": f["complete_250_share"],
                     "strict": f["complete_250_strict_share"], "after_pending": f["complete_250_after_pending_share"],
                     "after_pending_ex_sib": f["complete_250_after_pending_ex_sibling_classes_share"],
                     "top250_closed": f["top250_close_in_week_share"],
                     "residual_low": six["lower_share_bin0_zero"], "residual": six["residual_share"],
                     "unknown_nw": six["unknown_name_weeks"], "upper": six["upper_share"],
                     "check_6_pass": six["pass"], "pass_known_only": six["pass_known_evidence_only"],
                     "unfillable_only": six["unfillable_only_share"],
                     "residual_mcap_first": (six.get("market_cap_first_binning") or {}).get("residual_share", np.nan)})
    return pd.DataFrame(rows)


def not_in_step6_facts(listed: pd.DataFrame) -> dict:
    """Missing universe-base name-weeks with no step-6 row, by the evidence this build found for them."""
    rows = listed[listed["missing"] & ~listed["in_pf"]]
    return {"name_weeks": int(len(rows)), "securities": int(rows["security_id"].nunique()),
            "by_evidence": {k: int(v) for k, v in rows["evidence"].value_counts().items()},
            "stored_direct_dv_ge_cut250_name_weeks": int(rows["pf_ge_cut250"].sum()),
            "by_security": {f"{s}:{t}": int(n) for (s, t), n in rows.groupby(["security_id", "ticker"]).size()
                            .sort_values(ascending=False).head(40).items()}}


def no_sic_in_top250(top: pd.DataFrame, master: pd.DataFrame) -> dict:
    """Top-250 name-weeks of securities with no SEC SIC or an investment entity type that stay in the base
    (no investment-company filing evidence those weeks: OZK, a bank)."""
    info = master.set_index("security_id")
    t = top[top["dv50_rank"] <= TOP_N]
    sic = t["security_id"].map(info["sic"]).fillna("")
    kind = t["security_id"].map(info["entity_type"]).fillna("")
    hit = t[(sic == "") | (kind == "investment")]
    return {"name_weeks": int(len(hit)),
            "securities": {f"{s}:{t_}": int(n) for (s, t_), n in hit.groupby(["security_id", "ticker"]).size()
                           .sort_values(ascending=False).items()}}


def investment_company_report(listed: pd.DataFrame, issuers: pd.DataFrame, spans: dict, cut: pd.DataFrame) -> pd.DataFrame:
    """One row per security left out as an investment company in some listed week: the weeks left out,
    and what it held while it was in the base: top-250 / top-300 name-weeks by a dv50 rank that counts it
    (``dv50_rank_incl_investment``), and the name-weeks it would have been missing (no such rank, not
    outside trading, not a new listing) with the evidence class they would have had and how many would
    block a week (dv50 at the cut, a proxy-only name at the band median, or unknown)."""
    rows = listed[listed["investment_company"].to_numpy(dtype=bool)]
    if rows.empty:
        return pd.DataFrame(columns=["security_id", "ticker", "weeks_excluded"])
    other = (rows["non_common"] | rows["spac_shell"] | rows["foreign"]).to_numpy(dtype=bool)
    rows = rows[~other].copy()
    rank = rows["dv50_rank_incl_investment"]
    priced = rows["dv50_rank_any_price_incl_investment"].notna().to_numpy()
    low_close = (rows["price_ge_10"] == "N").to_numpy()
    would_miss = ~priced & ~rows["outside_trading"].to_numpy(dtype=bool) & ~rows["young_any"].to_numpy(dtype=bool) & ~low_close
    sub = rows.assign(would_miss=would_miss)
    sub["evidence_if_in"] = np.where(would_miss, evidence_class(sub), "")
    sub["blocks_if_in"] = would_miss & blocks_week(sub.assign(evidence=sub["evidence_if_in"])).to_numpy(dtype=bool)
    g = sub.groupby("security_id")
    table = pd.DataFrame({
        "ticker": g["ticker"].last(), "weeks_excluded": g.size(),
        "first_excluded": g["week_end"].min().dt.strftime("%Y-%m-%d"),
        "last_excluded": g["week_end"].max().dt.strftime("%Y-%m-%d"),
        "former_top250_name_weeks": (rank <= TOP_N).groupby(sub["security_id"]).sum(),
        "former_top300_name_weeks": (rank <= PRICE_RANK).groupby(sub["security_id"]).sum(),
        "former_best_dv50_rank": rank.groupby(sub["security_id"]).min(),
        "former_missing_name_weeks": g["would_miss"].sum(),
        "former_missing_unknown": (sub["evidence_if_in"] == "unknown").groupby(sub["security_id"]).sum(),
        "former_missing_dv_ge_cut250": (sub["would_miss"] & sub["pf_ge_cut250"]).groupby(sub["security_id"]).sum(),
        "former_blocking_name_weeks": g["blocks_if_in"].sum(),
    }).reset_index()
    info = issuers.assign(security_id=issuers["security_ids"].str.split()).explode("security_id") if len(issuers) else None
    if info is not None:
        info = info.drop_duplicates("security_id").set_index("security_id")
        for column in ("cik", "spans", "election", "withdrawal", "first_evidence", "last_evidence", "evidence_forms",
                       "pages_missing"):
            table[column] = table["security_id"].map(info[column])
    table["spans_here"] = table["security_id"].map(lambda s: " ".join(f"{a}..{b}" for a, b in spans.get(s, [])))
    return table.sort_values(["former_top250_name_weeks", "weeks_excluded"], ascending=False).reset_index(drop=True)


def young_definition() -> str:
    """The summary's definition of ``young``, built from the constants ``young_rules`` uses."""
    minimum = DV_WINDOWS[max(DV_WINDOWS)]
    window = max(DV_WINDOWS)
    return ("not missing: a new listing still short of the dv50's minimum rows, by any of three rules. "
            f"(canonical) a canonical close without a dv50 within {YOUNG_DAYS} days of the first canonical row of its "
            "segment (the last relist junction on or before the week, where the dv20 / dv50 windows restart, else the "
            "security's first canonical row), only where the segment starts at a relist junction or the security is a "
            f"new listing (ipo_rule, prospectus, snapshot_gap) whose canonical series starts within "
            f"{SERIES_START_SESSIONS} sessions of its earliest start; a series that starts at a transfer from another "
            "exchange (older_issuer), at a successor link (successor_link) or later than that stays missing. "
            f"With no dv50 from step 6 either: (new_listing) the security's first listing run, in its first "
            f"{minimum - 1} or fewer XNAS sessions counted from the earliest day it can have begun trading, and only when "
            "the run's start is a real new listing: the IPO rule's first price (ipo_rule), or a snapshot start counted "
            f"from {PROSPECTUS_LEAD_SESSIONS} sessions before a final IPO prospectus ({' / '.join(sorted(PROSPECTUS_FORMS))}) "
            f"filed from {PROSPECTUS_SLACK_DAYS} days before the last snapshot without it to the start (prospectus), "
            "else from the first session after the last snapshot without it (snapshot_gap), whichever is later; a "
            "security that continues another through a successor link (successor_link, see successor_link), an "
            "issuer that filed "
            f"a 10-K / 10-Q more than {YOUNG_ISSUER_DAYS} days before the start (older_issuer: a transfer from another "
            "exchange, a reclassified tracking stock), or one with another security listed on Nasdaq up to "
            f"{YOUNG_ISSUER_DAYS} days before (issuer_listed_before), or a start no earlier snapshot dates "
            "(no_earlier_snapshot) is not a new listing, and its weeks stay missing; (short_series) step 6 holding "
            f"1 to {minimum - 1} dollar-volume rows in the {window}-session window of a series it first holds no more "
            f"than {YOUNG_DAYS} days before the week end and no more than {YOUNG_DAYS} days after the listing run "
            "started, for an issuer that was not public before (not successor_link / older_issuer / "
            "issuer_listed_before). A name step 6 holds no row for (n50 = 0) is not young")


def successor_link_definition() -> str:
    """The summary's definition of the successor-link treatment (``successor_links``)."""
    kind, subtype, shares = ONE_TO_ONE
    return ("owner convention of 2026-10-02 (CRSP keeps one PERMNO): a successor link is a master link (the "
            "predecessor's successor_security_id / successor_date; link_source security_master) or, for a predecessor "
            f"the master links to nothing, step 11's booking of its end as {kind} / {subtype} of {shares:g} successor "
            "share per share with acquirer_security_id a listed security no master link names as a successor "
            "(link_source step11, successor date step 11's end_date: Express Scripts Holding 2012, Marvell 2021). A "
            "link continues the security when the successor's first listing starts "
            f"within {SUCCESSOR_LINK_DAYS} days of the successor date, its first canonical row does, or its listing "
            f"starts within {SUCCESSOR_LINK_DAYS} days of the predecessor's last listed day, and the predecessor's "
            f"listing run (renames joined) ends no more than {PREDECESSOR_OVERLAP_DAYS} days after the successor's "
            "listing starts (a predecessor listed on for longer continues itself and the successor is a new "
            "security, continuing_basis predecessor_listed_on: new IAC 2020, old IAC listed on as MTCH): the "
            "successor is no new listing (new_listing_basis successor_link; no young rule applies, so a week without "
            "a dv50 is missing). "
            f"When step 11 books the predecessor's end as {kind} / {subtype} of {shares:g} successor share per share "
            "(a 1:1 holding-company reorganisation or redomicile: Alphabet 2015, APA 2021), the successor's dv20 / dv50 "
            "windows run across the link: on the sessions before the successor's first listed day they read the "
            "predecessor's rows (the successor's own rows there only where the predecessor has none), from that day "
            "on only its own; one row per session, so no day is counted twice. Closes, first and last rows stay each "
            "security's own")


def investment_company_definition() -> str:
    """The summary's definition of ``investment_company``, built from the constants
    ``investment_company_spans`` and ``extend_to_listing_end`` use."""
    return (f"SEC filings only. bdc_election: an {IC_ELECTION} BDC election to the day before the next {IC_WITHDRAWAL} "
            f"withdrawal, open-ended ({IC_OPEN_END}) without one; a withdrawal with no cached election counts from "
            f"the issuer's first evidence (bdc_until_withdrawal). filings: a run of >= {IC_MIN_RUN} filings only an "
            f"investment company makes ({', '.join(sorted(IC_FORMS))}; N-PX only before {IC_NPX_BEFORE}; SIC {IC_SIC} "
            f"headers) no more than {IC_RUN_GAP_DAYS} days apart, from its first to its last filing. filings_current: "
            f"the last run is carried forward to {IC_OPEN_END} while the issuer still files (its latest filing of any "
            f"form within {IC_RUN_GAP_DAYS} days of the run's end) and no {IC_WITHDRAWAL} or {IC_DEREGISTRATION} "
            f"(deregistration) is dated within {IC_RUN_GAP_DAYS} days before the run's end or later. merger_tail: a "
            f"closed span whose end, or an {IC_WITHDRAWAL} / {IC_DEREGISTRATION} within {IC_RUN_GAP_DAYS} days after "
            f"it, lies within {IC_TAIL_DAYS} days of a delisting of a listing it ends inside (the listing's last day, "
            "its Form 25 effective date or its last price) runs to that listing's end (a BDC that withdraws its "
            "election on its merger date). Every class of the issuer goes with it")


def pending_definitions(pending=PENDING_REASONS) -> dict:
    """The summary's definitions that name the pending reasons, built from ``pending``."""
    text = " / ".join(sorted(pending))
    return {"complete_250_after_pending": f"the complete_250 conditions with the {text} names set aside "
                                          "(tiingo_pending / yahoo_pending only in the weeks a candidate row of that "
                                          "source needs)",
            "residual_survivorship": f"expected top-250 name-weeks held by missing names other than {text}, from the "
                                     "evidence rules' hit rates by year; unknown name-weeks are not in it and are added "
                                     "as top-250 name-weeks in *_upper",
            "pending_reasons": sorted(pending)}


def pending_window_facts(listed: pd.DataFrame, reasons: "MissingReasons") -> dict:
    """Missing name-weeks whose security-level reason is pending but whose week no candidate row of that
    source needs, so they took the next reason of the chain (CORZ's Yahoo-need weeks are no longer
    tiingo_pending), before the week overrides (unfillable.csv windows, answers newer or older than the panel)."""
    miss = listed[listed["missing"]]
    level = miss["security_id"].map(reasons.as_dict()).fillna("not_candidate")
    pending = level.isin(list(PENDING_SOURCE)).to_numpy()
    weekly = pd.Series("", index=miss.index, dtype=object)
    weekly[pending] = reasons.weekly(miss["security_id"].to_numpy(dtype=object)[pending],
                                     miss["week_end"].dt.strftime("%Y-%m-%d").to_numpy(dtype=object)[pending])
    moved = miss[pending & (level != weekly).to_numpy()]
    pairs = (level[moved.index] + "->" + weekly[moved.index]).value_counts()
    by_security = moved.groupby(["security_id", "ticker"]).size().sort_values(ascending=False).head(30)
    return {"fell_through_name_weeks": int(len(moved)),
            "fell_through_by_reason": {k: int(v) for k, v in pairs.items()},
            "fell_through_securities": int(moved["security_id"].nunique()),
            "top_securities": {f"{s}:{t}": int(n) for (s, t), n in by_security.items()}}


def investment_span_rows(investment: dict, master: pd.DataFrame) -> pd.DataFrame:
    """security_id, cik, ticker, start, end: the investment-company spans step 14 reads."""
    info = master.set_index("security_id")
    rows = [{"security_id": sid, "cik": info["cik"].get(sid, ""), "ticker": info["first_ticker"].get(sid, ""),
             "start": a, "end": b} for sid, spans in sorted(investment.items()) for a, b in spans]
    return pd.DataFrame(rows, columns=["security_id", "cik", "ticker", "start", "end"])


def investment_company_summary(table: pd.DataFrame, issuers: pd.DataFrame, facts: dict, fetched: dict,
                               spans_file: Path) -> dict:
    """The investment companies left out of the base, for universe_summary.json."""
    names = {}
    for row in table.itertuples(index=False):
        names[f"{row.security_id}:{row.ticker}"] = {
            "weeks_excluded": int(row.weeks_excluded), "first": row.first_excluded, "last": row.last_excluded,
            "former_top250_name_weeks": int(row.former_top250_name_weeks),
            "former_missing_name_weeks": int(row.former_missing_name_weeks),
            "former_blocking_name_weeks": int(row.former_blocking_name_weeks),
            "evidence": getattr(row, "evidence_forms", "")}
    return {"securities": int(len(table)), "name_weeks_excluded": int(table["weeks_excluded"].sum()) if len(table) else 0,
            "former_top250_name_weeks": int(table["former_top250_name_weeks"].sum()) if len(table) else 0,
            "former_missing_name_weeks": int(table["former_missing_name_weeks"].sum()) if len(table) else 0,
            "former_blocking_name_weeks": int(table["former_blocking_name_weeks"].sum()) if len(table) else 0,
            "issuers_with_evidence": int(len(issuers)),
            "issuers_with_evidence_never_left_out": sorted(
                set(issuers["cik"]) - set(table["cik"].dropna())) if len(issuers) and "cik" in table else [],
            "submissions_read": {k: (v if not isinstance(v, list) else (len(v) if len(v) > 20 else v))
                                 for k, v in facts.items() if k not in ("files", "prospectus")},
            "sec_fetch_this_run": {k: v for k, v in fetched.items() if k != "files"},
            "spans_file": str(spans_file), "by_security": names}


def class_cap_summary(listed: pd.DataFrame) -> dict:
    """What the class cap (``class_dv_ratios``) changes: the capped name-weeks and the expected missing
    top-250 name-weeks with and without it, by year and by security."""
    capped = listed[listed["missing"] & listed["proxy_class_capped"]]
    miss = listed[listed["missing"]]
    year = lambda f: f["week_end"].dt.year
    est = lambda f, col: round(float(f[col].sum()), 2)
    by_security = capped.groupby("security_id").agg(
        ticker=("ticker", "last"), weeks=("week_end", "size"), first=("week_end", "min"), last=("week_end", "max"),
        est_capped=("p_top250", "sum"), est_uncapped=("p_top250_uncapped", "sum"),
        max_class_dv_ratio=("class_dv_ratio", "max"), max_weeks_away=("class_dv_weeks_away", "max"))
    by_security = by_security.sort_values("est_uncapped", ascending=False)
    return {"rule": (f"a missing name-week of a class of a multi-class company with only a proxy (the company's "
                     f"market cap / float), whose own dv50 (canonical, else step 6's at any price) in the nearest "
                     f"week within {CLASS_DV_WEEKS} weeks is under {CLASS_DV_CAP_RATIO} of that week's rank-250 cut, "
                     "takes the dollar-volume rule's lowest bin; proxy_above (check 1, complete_250) is unchanged"),
            "name_weeks": int(len(capped)), "securities": int(capped["security_id"].nunique()),
            "est_missing_top250_name_weeks": {"with_cap": est(miss, "p_top250"), "without_cap": est(miss, "p_top250_uncapped")},
            "by_year": {int(y): {"name_weeks": int(len(g)), "est_with_cap": est(g, "p_top250"),
                                 "est_without_cap": est(g, "p_top250_uncapped"),
                                 "unfillable_est_with_cap": est(g[g["missing_reason"] == "unfillable"], "p_top250"),
                                 "unfillable_est_without_cap": est(g[g["missing_reason"] == "unfillable"],
                                                                   "p_top250_uncapped")}
                        for y, g in capped.groupby(year(capped))},
            "by_security": [{"security_id": sid, "ticker": r["ticker"], "weeks": int(r["weeks"]),
                             "first": r["first"].strftime("%Y-%m-%d"), "last": r["last"].strftime("%Y-%m-%d"),
                             "est_with_cap": round(float(r["est_capped"]), 2),
                             "est_without_cap": round(float(r["est_uncapped"]), 2),
                             "max_class_dv_ratio": round(float(r["max_class_dv_ratio"]), 4),
                             "max_weeks_away": int(r["max_weeks_away"])}
                            for sid, r in by_security.head(40).iterrows()]}


def _column(frame: pd.DataFrame, name: str, default) -> pd.Series:
    """``frame[name]``, or ``default`` on every row when the frame lacks the column."""
    return frame[name] if name in frame else pd.Series(default, index=frame.index)


def _counts(values: pd.Series) -> dict:
    return {str(k): int(v) for k, v in values.value_counts().items()}


def young_summary(listed: pd.DataFrame, sessions: pd.DatetimeIndex, week_pos: np.ndarray) -> dict:
    """The young name-weeks by rule, the rule-1 (``new_listing``) weeks whose market-cap proxy reaches the
    band median (``proxy_above``: they would block the week if they were missing), and the first-run weeks
    the snapshot start alone would have called young (fewer than 25 sessions since the run's first snapshot,
    no dv50 anywhere) that rule 1 now leaves missing, by ``new_listing_basis`` (round-7 review); the
    canonical-rule weeks by basis, by predecessor link (``predecessor_security_id``: a continuing master
    successor link) and by ``proxy_above``, and the weeks with a canonical close and no dv50 within
    YOUNG_DAYS of their segment's first row that the canonical rule no longer calls young (a transfer such as
    KDP or HST, a successor link, a series that starts after the listing could have begun), with what they
    do to the week (round-8 review)."""
    base = listed[listed["eligible"]]
    young = base[base["young"]]
    rule1 = young[young["young_rule"] == "new_listing"]
    above = rule1[rule1["proxy_above"]]
    minimum = DV_WINDOWS[max(DV_WINDOWS)]
    start = sessions.searchsorted(pd.to_datetime(base["listing_start"].replace("", None)).fillna(sessions[0]))
    from_snapshot = week_pos[base["week_index"].to_numpy()] - start + 1
    unpriced = base["dv50"].isna() & base["pf_dv50"].isna()
    before = base[(from_snapshot < minimum) & base["first_listing_run"].to_numpy(dtype=bool) & unpriced.to_numpy()
                  & ~base["young"].to_numpy(dtype=bool)]
    names = lambda rows: {f"{s}:{t}": int(n) for (s, t), n in rows.groupby(["security_id", "ticker"]).size()
                          .sort_values(ascending=False).head(40).items()}
    linked = lambda rows: _column(rows, "predecessor_security_id", "").fillna("").astype(str).ne("").to_numpy()
    first = "segment_first_row" if "segment_first_row" in base else "first_row"
    if first in base:
        days = (base["week_end"] - pd.to_datetime(base[first].replace("", None))).dt.days
        within = (days <= YOUNG_DAYS).fillna(False).to_numpy(dtype=bool)
    else:
        within = np.zeros(len(base), bool)
    could = base["close"].notna().to_numpy() & base["dv50"].isna().to_numpy() & within
    canonical = young[young["young_rule"] == "canonical"]
    left = base[could & ~base["young"].to_numpy(dtype=bool)]
    left_missing = left[left["missing"].to_numpy(dtype=bool)]
    blocking = (blocks_week(left_missing).to_numpy(dtype=bool)
                if len(left_missing) and {"pf_ge_cut250", "pf_dv_ok", "pf_price_low", "evidence"} <= set(left.columns)
                else np.zeros(len(left_missing), bool))
    canonical_above = canonical[canonical["proxy_above"].to_numpy(dtype=bool)]
    left_above = left[left["proxy_above"].to_numpy(dtype=bool)]
    return {"name_weeks": int(len(young)),
            "by_rule": {k: int(v) for k, v in young["young_rule"].value_counts().items()},
            "without_canonical_close": int(young["close"].isna().sum()),
            "securities_without_canonical_close": sorted(set(young.loc[young["close"].isna(), "ticker"]))[:60],
            "by_rule_proxy_above": {k: int(v) for k, v in young.loc[young["proxy_above"], "young_rule"].value_counts().items()},
            "with_predecessor_link": {"name_weeks": int(linked(young).sum()),
                                      "by_rule": _counts(young.loc[linked(young), "young_rule"])},
            "new_listing_rule": {"name_weeks": int(len(rule1)), "securities": int(rule1["security_id"].nunique()),
                                 "by_basis": {k: int(v) for k, v in rule1["new_listing_basis"].value_counts().items()},
                                 "proxy_above_name_weeks": int(len(above)),
                                 "proxy_above_securities": names(above)},
            "canonical_rule": {
                "name_weeks": int(len(canonical)), "securities": int(canonical["security_id"].nunique()),
                "by_basis": _counts(_column(canonical, "new_listing_basis", "")),
                "at_relist_junction": int(_column(canonical, "segment_junction", False).astype(bool).sum()),
                "with_predecessor_link": int(linked(canonical).sum()),
                "proxy_above_name_weeks": int(len(canonical_above)),
                "proxy_above_by_basis": _counts(_column(canonical_above, "new_listing_basis", "")),
                "proxy_above_with_predecessor_link": int(linked(canonical_above).sum()),
                "proxy_above_securities": names(canonical_above)},
            "canonical_rule_not_applied": {
                "name_weeks": int(len(left)), "securities": int(left["security_id"].nunique()),
                "by_basis": _counts(_column(left, "new_listing_basis", "")),
                "with_predecessor_link": int(linked(left).sum()),
                "missing_name_weeks": int(len(left_missing)),
                "missing_blocking_name_weeks": int(blocking.sum()),
                "proxy_above_name_weeks": int(len(left_above)),
                "proxy_above_by_basis": _counts(_column(left_above, "new_listing_basis", "")),
                "proxy_above_with_predecessor_link": int(linked(left_above).sum()),
                "dv_ge_cut250_name_weeks": int(_column(left, "pf_ge_cut250", False).astype(bool).sum()),
                "blocking_securities": names(left_missing[blocking]),
                "note": "weeks with a canonical close and no dv50 within the young window of the segment's first "
                        "row that the canonical rule leaves to the other rules: the segment starts at no relist "
                        "junction and the security is no new listing whose series starts within "
                        f"{SERIES_START_SESSIONS} sessions of its earliest start (a transfer from NYSE whose rows "
                        "start at the Nasdaq start, a successor link, a late series); missing unless rule 1 or "
                        "short_series calls them young, so the proxy or step-6 evidence speaks for the week"},
            "first_run_weeks_not_young": {
                "name_weeks": int(len(before)),
                "by_basis": {k: int(v) for k, v in before["new_listing_basis"].value_counts().items()},
                "missing_name_weeks": int(before["missing"].sum()),
                "proxy_above_name_weeks": int(before["proxy_above"].sum()),
                "proxy_above_securities": names(before[before["proxy_above"]]),
                "note": "first-run weeks within 25 sessions of the run's first snapshot with no dv50 anywhere, "
                        "that rule 1 does not call young: the issuer was public before (older_issuer, "
                        "issuer_listed_before), the security continues another (successor_link), no snapshot "
                        "dates the start (no_earlier_snapshot), or the earliest start the IPO prospectus or the "
                        "snapshot gap gives is 25 or more sessions back (prospectus, snapshot_gap)"}}


def successor_link_summary(listed: pd.DataFrame, links: pd.DataFrame, windows: dict, weeks_after: int = 5) -> dict:
    """The successor links (master and step 11, ``link_source``) and what they do here: continuing (the
    successor is no new listing), one_to_one by step 11, the windows run across (with the predecessor rows in
    the successor's first 50-session window), and the successor's first ``weeks_after`` listed weeks: base,
    ranked, missing (and blocking), young (round-8 review: Alphabet 2015-10 was young for 5 weeks while ranked
    6th and 7th; round-9 review: Express Scripts Holding 2012-04 and Marvell 2021-04, 1:1 reorganisations the
    master does not link, were young while ranked 12th-13th and 63rd-66th)."""
    if links is None or not len(links):
        return {"links": 0}
    rows = listed[listed["security_id"].isin(set(links["successor_id"]))].sort_values(["security_id", "week_index"])
    head = rows.groupby("security_id", sort=False).head(weeks_after)
    blocking = pd.Series(False, index=head.index)
    miss = head["missing"].to_numpy(dtype=bool)
    if miss.any():
        blocking.loc[head.index[miss]] = blocks_week(head[miss]).to_numpy(dtype=bool)
    first = head.groupby("security_id")
    by_link = []
    for row in links.itertuples(index=False):
        sid = row.successor_id
        entry = {k: (bool(v) if isinstance(v, (bool, np.bool_)) else v) for k, v in row._asdict().items()}
        if sid in first.groups:
            g = head.loc[first.groups[sid]]
            entry.update({"ticker": str(g["ticker"].iloc[0]), "first_week": g["week_end"].min().strftime("%Y-%m-%d"),
                          f"first_{weeks_after}_weeks": {
                              "base": int(g["eligible"].sum()), "ranked_dv50": int(g["dv50_rank_any_price"].notna().sum()),
                              "best_dv50_rank": (int(g["dv50_rank"].min()) if g["dv50_rank"].notna().any() else None),
                              "missing": int(g["missing"].sum()), "missing_blocking": int(blocking.loc[g.index].sum()),
                              "young": int(g["young"].sum())}})
        entry.update(windows.get(sid, {}))
        by_link.append(entry)
    crossed = sum(1 for f in windows.values() if f.get("predecessor_rows_in_first_window"))
    source = links["link_source"] if "link_source" in links else pd.Series(index=links.index, dtype=object)
    source = source.fillna(LINK_SOURCES[0])
    by_source = {}
    for name, g in links.groupby(source, sort=True):
        by_source[str(name)] = {"links": int(len(g)), "continuing": int(g["continuing"].sum()),
                                "windows_cross": int(g["windows_cross"].sum()),
                                "continuing_by_basis": _counts(g["continuing_basis"])}
    return {"links": int(len(links)), "continuing": int(links["continuing"].sum()),
            "continuing_by_basis": _counts(links["continuing_basis"]),
            "one_to_one": int(links["one_to_one"].sum()), "windows_cross": int(links["windows_cross"].sum()),
            "windows_cross_with_predecessor_rows": crossed, "by_source": by_source,
            "rule": ("a successor link (a master link, or step 11's 1:1 stock_merger / reorganization for a "
                     "predecessor the master links to nothing, link_source step11) continues the security (no "
                     "young rule; basis successor_link) when the successor's first listing starts within "
                     f"{SUCCESSOR_LINK_DAYS} days of the successor date, its first canonical row does, or its listing "
                     f"starts within {SUCCESSOR_LINK_DAYS} days of the predecessor's last listed day, and the "
                     f"predecessor's listing run ends no more than {PREDECESSOR_OVERLAP_DAYS} days after the "
                     "successor's start (else predecessor_listed_on: a new security); its dv20 / dv50 windows run "
                     "across (the predecessor's rows before the successor's first listed day, one row per session) "
                     "when step 11 books the predecessor's end as a stock_merger / reorganization of one successor "
                     "share per share"),
            "by_link": by_link}


def pf_only_reasons(listed: pd.DataFrame) -> dict:
    """Why step 6's top-250 names are not in the canonical top 250 (name-weeks over all weeks)."""
    rows = listed[(listed["pf_dv50_rank"] <= TOP_N) & ~(listed["dv50_rank"] <= TOP_N)]
    reason = np.select(
        [~rows["eligible"], rows["outside_trading"], rows["missing"], rows["price_ge_10"] == "N",
         rows["dv50_rank"] <= PRICE_RANK, rows["dv50_rank"].notna(), rows["young"]],
        ["not_universe_base_here", "outside_trading", "missing_here", "price_lt_10_here", "rank_251_300_here",
         "rank_gt_300_here", "young_here"], default="other")
    return pd.Series(reason).value_counts().to_dict()


if __name__ == "__main__":
    raise SystemExit(main())
