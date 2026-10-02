"""Plan step 9 (docs/reversal_2012_2026_data_plan.md, sections 1, 2, 4 and 6): one canonical daily
series per security, the split and distribution tables, and a queue of suspicious data points.

Data only. Nothing here computes signals, strategy or portfolio returns, return rankings or spreads,
and nothing averages, ranks, sorts or aggregates stocks by return. The single-stock daily total
return ``tr`` is computed only to build each security's own canonical series and to flag bad data
points (plan 4.4); the reports count flags and covered name-weeks, never returns.

Which securities: every ``candidate_fetch_list.csv`` security, plus every security with a step-6
dollar-volume rank (dv20 or dv50) <= 300 in any week (``CACHE/prefilter/weekly_metrics.pkl``).

Sources (plan 4.2), each converted to the canonical record (raw close, raw volume, split factor S =
new shares per old on the ex-date, cash dividend D as paid on the ex-date):
- ``wiki``: WIKI raw files (``CACHE/wiki/by_ticker``, to 2018-03-27; ``close``, ``volume``,
  ``split_ratio``, ``ex-dividend`` as is). Rows go to securities through the prefilter's listing
  spans (``pf.TickerMap``), and WIKI files that failed step 6's LastSale check are left out. WIKI
  is missing most dividends from 2017-11 on, so it ranks last from then on (``wiki_div_gap``).
- ``tiingo``: the month-1 run (``CACHE/tiingo/fetch_status.csv``: rows ``done``, ``done_review``,
  ``partial``, read through the security's own ``prices_path``; files keep arriving, and a rerun
  picks them up), then the older Tiingo JSON caches the prefilter already counted
  (``research_cache/sue_lt_2020_2026/raw``, ``tiingo_delisted``, ``tiingo_overlap``) for the days
  the run does not have. ``close``, ``volume``, ``splitFactor`` and ``divCash`` map directly; the
  ``adjClose`` identity is checked per row (``tiingo_adj_identity`` above 1e-8). The repo's
  ``price_supplement`` CSVs are derived from those same JSON files, so they are not another source.
- ``yahoo``: the step-7 restored files (``CACHE/yahoo/{security_id}.csv.gz``); for a security without
  one, the holdout's v8 charts (``holdout_2011_2019/yahoo_nominal``) restored the same way (close x
  later split ratios). Odd-ratio Yahoo events (``events.csv`` flag ``odd_ratio``) are distributions:
  their factor stays in S so ``tr`` is right, the row is flagged ``distribution_factor`` and the
  event is typed ``distribution`` (not a split). Junction rows (``junction`` = Y, or an event
  flagged ``trim_junction``/``segment_junction``) carry S and D from another history: they are not
  splits, Yahoo gives no return for that day, and the row is flagged ``yahoo_junction``.
- ``stored``: the repo files (``cleaned_stocks_data/price``, Nasdaq's public endpoint; split- and to
  about 2023 dividend-adjusted). Only a vote on the day's return, never a level, and only where its
  return is comparable: not on the known unit breaks (2025-06-24, HON 2026-06-29), not on an
  ex-date from 2023 on (price-only after that), not on a vendor's split day where it shows that raw
  jump, and not on a stored-only unit change. That last one is decided against the vendors' own
  agreement, never against a default source: every vendor with a return agrees with every other,
  the stored file agrees with none and is off by an ordinary split ratio within 1%, and the
  vendors' own move is not split-sized (a ``unit_break`` in ``split_events.csv``; on a known break day
  any lasting factor beyond 2%, ordinary ratio or not: HON 2025-06-24, 1.896). A stored return
  that agrees with any vendor stays a vote (PTCT 2013-06-20..26: WIKI 10x too low, outvoted by
  Yahoo and the stored file). When the only vendor's own move is split-sized and the stored file
  does not show it, the day is a hidden split or a vendor error (``vendor_split_jump``, R2/R3;
  CMCT 2025-01-06, BPTH 2018-02-09), not a stored unit change.
- A split one vendor records as an ordinary ratio S > 1 and another, the same day, as cash worth
  the new shares (D within 1% of (S - 1) x C_t or (1 - 1/S) x C_{t-1}; WIKI's PZZA 2013-12-30 and
  HMSY 2011-08-17) is read as that split in both (S, no cash; ``cash_as_split:{source}``), so it is
  neither a special dividend nor a distribution. Real distributions have no ordinary ratio anywhere
  and keep the cash-versus-ratio typing (EBAY, ADP, THRX).

Canonical record (``CACHE/prices/{security_id}.csv``): date, close_raw, volume_raw, split_factor,
div_cash, tr, src_primary, n_sources, max_src_diff, flags.
- Rows are XNAS sessions inside the security's window: its listing spans (``pf.mapping_spans``) with
  75 calendar days before (the dv50 warm-up) and 28 after (plan 4.5 hold), within 2011-06-01 to
  2026-08-31; rows outside the spans are kept and flagged ``outside_listing`` (R9). A listing after a
  Form 25 cut (``pf.listing_spans`` ``after_cut``: SMCI from 2020-01, CHRD) is a span like any other,
  so the series runs on to 2026 (the OTC months between are ``outside_listing``).
- Relist junctions (``RELIST_JUNCTIONS``, hand-reviewed from the SEC filings: CHRD/Oasis 2020-11-20,
  CORZ 2024-01-24, WW 2025-06-27, OPI 2026-06-18): a bankruptcy plan cancelled or exchanged the old
  shares and the new ones were listed again. Old and new shares are separate segments of the file:
  the new shares' first row has no ``tr`` (``relist_junction``), any S or D a vendor records on it is
  dropped (reported), and nothing is chained, voted, spliced, flat-run or queued across it; R5's
  filler cut applies to each segment's end. The old shares end there with a terminal event
  (``series_ends.csv`` ``old_shares_at_relist_junction``, for the terminal step). Other relistings
  keep one series (the same shares: SMCI, removed for late filings), and a raw level change of 10x or
  more around one with no split is queued (R9, ``relist_jump``) for a junction entry or a market move.
- ``tr`` = (C_t x S_t + D_t) / C_{t-1} - 1 from one source's own rows (plan 4.1), so only returns
  are chained across sources, never levels (R8).
- Precedence per day: WIKI, Tiingo, Yahoo to 2017-10-31; Tiingo, Yahoo, WIKI from 2017-11-01. Each
  vendor's return, and the stored vote where valid, is compared with the others: agreeing within
  0.5% or not (R3). With a strict majority, the highest-ranked source in it is primary
  (``majority_override`` when that is not the default one, ``disagree_resolved`` naming the
  minority); without one (two vendors that disagree) the default stays and the day is
  ``disagree_unresolved`` and queued. When the only dissent is the stored file against a single
  vendor, the vendor stands: ``stored_glitch`` (the stored/vendor level comes back within two
  sessions: a bad row in one of them, LANC 2020-2022), ``stored_shift`` (it moves 2% or more and
  stays: queued, an event the vendor may lack), or ``stored_disagrees`` (a smaller lasting shift).
  Glitch and disagrees days more than 2% apart are queued too (R3, single vendor vs stored), since
  the level ratio cannot say which row is bad; summary.json counts them by difference size.
- ``n_sources`` = sources with a return that day (vendors plus a valid stored vote);
  ``max_src_diff`` = the largest absolute difference between one of them and ``tr``.
- Other flags: ``level_diff`` (R7, vendor raw closes more than 1% apart), ``move_2x`` and ``move_40``
  (R1), ``hidden_split`` (R2: S = 1 everywhere, the price ratio is within 1% of n:1, 1:n, 3:2 or 2:3,
  and the adjusted stored file does not move with it),
  ``flat_run`` / ``zero_volume`` (R4), ``gap_before:N`` (R6: N sessions missing before this row
  inside the listing), ``cross_source_return`` (the day's source lacks the prior session, so ``tr``
  uses the previous canonical close), ``splice:a>b`` and ``splice_weak`` (R8), ``split`` /
  ``reverse_split`` / ``distribution_factor``, ``special_div`` (D > 10% of the prior close),
  ``volume_diff`` (vendor volumes 2x apart), ``stored_excluded``, ``vendor_split_jump``,
  ``cash_as_split:{source}``, ``yahoo_junction``, ``wiki_div_gap``, ``tiingo_adj_identity``,
  ``tiingo_review`` (the fetcher's ``done_review``), and on Yahoo-primary rows dated before an
  events.csv event flagged ``volume_restore_unverified`` / ``volume_not_scaled_by_yahoo``,
  ``yahoo_volume_unverified`` / ``yahoo_volume_not_scaled`` (that row's volume_raw may be off by
  the event's ratio; counted in summary.json).
- R5: rows after the last session with volume > 0 at the end of the series are cut (SGEN, EVBG
  filler), then trailing rows that repeat the last real close with volume below 1% of the
  50-row median before it (SPLK 2024-03-18..22: Tiingo volumes 0, 90, 47, ...); both are counted.

Tables:
- ``INPUTS/split_events.csv``: every S != 1 in any vendor source plus the stored files' unit breaks,
  matched across sources (same ex-date +-1 session, ratio within 0.1%), typed split /
  reverse_split / spinoff (plan 4.3's known cases) / distribution (odd ratios, or a ratio in one source
  and cash in another that is not the split's value) / unit_break (the stored file's own unit change,
  as above; 2025-06-24). ``nasdaq`` stays blank until a Nasdaq calendar source exists.
  ``stored_implied_k`` is k = (1 + tr) / (1 + stored return) on the ex-date and ``stored_state``
  reads it: ``adjusted`` (k within 2% of 1: the stored file moves with the total return),
  ``raw`` (k within 2% of S: it shows the raw jump), ``ambiguous`` (S too close to 1 to tell),
  ``other``, ``none`` (no stored row), or ``unit_change`` for a unit_break. ``agree`` is Y when at
  least two independent sources confirm the ratio and no vendor covering the date shows another
  one: a vendor with the same ratio, a vendor that serves it as cash worth the new shares
  (``wiki(cash)``), or an adjusted stored file. A raw stored jump is not a confirmation: its k
  equals the vendor's own S whatever S is, so it shows only the date and the raw prices.
  ``tr_agree`` says whether the sources' total returns agree within 0.5% that day (a distribution
  served as cash by one vendor and as a ratio by another can agree on it). ``sec_url`` / ``verified_at`` stay blank for the
  hand review, except where ``confirmed_price_adjustments.csv`` gives them (PRPL);
  ``sec_candidates`` lists nearby 8-Ks with Items 2.01/3.03/5.03 (then 8.01) from the cached SEC
  submissions files (no request is made).
- ``INPUTS/special_distributions.csv``: cash above 10% of the prior raw close in any vendor (not a
  split served as cash), and every odd ratio (distributions served as splits; ``pct_of_prior`` is
  then 1 - 1/ratio for a ratio above 1, blank below 1), with the sources' total returns when they
  differ and the same ``sec_candidates``.
- ``INPUTS/reviewed_moves.csv``: the draft queue for the hand review, in
  ``stocks_list_dir/nasdaq/reviewed_market_moves.csv`` format plus ``security_id`` and
  ``sources_agreeing`` (sources whose return is within 0.5% of ``tr`` that day, or that show the same
  flat run); ``classification`` is ``unreviewed`` (or the reviewed file's entry for the same ticker
  and date) and ``notes`` starts with the rule: [R1] move of 2x or 0.5x, [R1/R2] one that is also a
  hidden-split candidate, [R1b] a 40% move no second source confirms, [R1c] |tr| >= 10% on an
  ex-date whose ratio no second source confirms (LGND 2022-11-02, 1.603 from Yahoo alone), [R2/R3]
  the only vendor's split-sized move that the stored file does not show, [R3] two vendors with no
  majority, a lasting stored shift, or a single vendor and the stored file more than 2% apart,
  [R4] a flat run no second source shows or zero volume, [R6]
  listed sessions with no vendor row, [R7] a vendor level run 2%+ apart or 3+ sessions long, [R9] a
  10x level change around an unreviewed relisting after a Form 25. Only
  listed days that can matter are queued: 10 weeks before to 5 weeks after a week ranked <= 300 by
  step 6, or whose canonical dollar volume reaches step 6's rank-300 cut; every entry, with that
  scope marked, is in ``CACHE/reconcile/moves_all.csv``.
- ``CACHE/reconcile/``: summary.json (coverage of ranks 1-300 by year with step 6's 5-session
  staleness rule, flag counts by type and year, the plan-6 multi-source agreement count with the
  stored vote and, separately, among vendors only and for the V sample's Tiingo-Yahoo days within
  1e-4, known-case checks), series_ends.csv (series that end before the delist date, or before the window end with
  none: the inputs for terminal values, with a likely cause), coverage_gaps.csv, no_series.csv,
  securities.csv, source_pairs.csv, moves_all.csv, relist_junctions.csv (every relisting after a Form
  25 and every junction: dates, rows on each side, status), summary.json ``break_days`` (each known
  break day's stored files that move 1.4x or more: ``unit_break``, or ``stored_moves_with_vendors`` for
  a real move both show: EYEN/HYPD +65%, NKTR +156%, UPXI -60% on 2025-06-24, which step 6's
  stored-only test lists as breaks) and ``run`` (timings, peak memory); ``sources/`` holds the source bundles and
  ``per_security/`` the state that makes a rerun redo only the securities whose inputs (or this
  file) changed. ``CACHE/prices/daily_panel.csv.gz`` is the long form of every canonical file.

Offline: the step reads local files only, and any socket connection in the process is refused
(``forbid_network``). Each phase logs its time and the peak resident memory; summary.json ``run`` and
``reconcile/logs/run_*.json`` keep them.

Usage::

    PYTHONPATH=. python scripts/reversal_data_reconcile.py                 # build (resumes)
    PYTHONPATH=. python scripts/reversal_data_reconcile.py --only 1065088  # one security, no tables
    PYTHONPATH=. python scripts/reversal_data_reconcile.py --rebuild       # everything from the raw files
    PYTHONPATH=. python scripts/reversal_data_reconcile.py --no-panel      # skip daily_panel.csv.gz
    # the full rebuild into a scratch directory, compared with the current outputs (compare.json,
    # compare_series.csv: securities, rows, changed series, table counts):
    PYTHONPATH=. python scripts/reversal_data_reconcile.py --rebuild --out-dir /tmp/reconcile_dry --compare
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import pickle
import sys
import time

import numpy as np
import pandas as pd

from scripts import reversal_data_common as common
from scripts import reversal_data_prefilter as pf

CODE_VERSION = "2026-10-02.3"
# Per-security results are rebuilt whenever this file changes (its hash is part of every signature).
CODE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
MAIN = common.MAIN_CHECKOUT
CACHE = common.CACHE
INPUTS = common.INPUTS
PRICES_DIR = CACHE / "prices"
OUT = CACHE / "reconcile"
SOURCE_CACHE = OUT / "sources"
STATE_DIR = OUT / "per_security"
LOG_DIR = OUT / "logs"
DEFAULT_SOURCE_CACHE = SOURCE_CACHE  # read (never written) by a build into another --out-dir, when its signature matches

CANDIDATES = INPUTS / "candidate_fetch_list.csv"
UNFILLABLE = INPUTS / "unfillable.csv"
SPLIT_EVENTS = INPUTS / "split_events.csv"
SPECIAL = INPUTS / "special_distributions.csv"
REVIEWED_MOVES = INPUTS / "reviewed_moves.csv"
WEEKLY_METRICS = CACHE / "prefilter" / "weekly_metrics.pkl"
WIKI_ENTITY = CACHE / "prefilter" / "entity.csv.gz"
WIKI_DIR = CACHE / "wiki" / "by_ticker"
YAHOO_DIR = CACHE / "yahoo"
YAHOO_EVENTS = YAHOO_DIR / "events.csv"
YAHOO_ENTITY = YAHOO_DIR / "entity_report.csv"
TIINGO_STATUS = CACHE / "tiingo" / "fetch_status.csv"
OLD_TIINGO_DIRS = list(pf.TIINGO_DIRS)
OLD_YAHOO_DIRS = list(pf.YAHOO_DIRS)
STORED_DIR = pf.STORED_DIR
NASDAQ_DIR = MAIN / "stocks_list_dir" / "nasdaq"
CONFIRMED_ADJUSTMENTS = NASDAQ_DIR / "confirmed_price_adjustments.csv"
CORPORATE_ACTIONS = NASDAQ_DIR / "corporate_actions.csv"
REVIEWED_FORMAT = NASDAQ_DIR / "reviewed_market_moves.csv"
TERMINAL_FILES = [NASDAQ_DIR / "terminal_returns.csv",
                  Path("output/research_only/sue_lt_2020_2026/inputs/terminal_returns_2020.csv"),
                  Path("output/research_only/holdout_2011_2019/inputs/terminal_returns_supplement.csv")]

WINDOW_START, WINDOW_END = pf.WINDOW_START, pf.WINDOW_END
WIKI_END = pf.WIKI_END
WIKI_DIV_GAP_FROM = "2017-11-01"  # WIKI's dividends are mostly missing from 2017-11 (wiki_build.json)
WARMUP_DAYS, HOLD_DAYS = 75, 28
VENDORS = ("wiki", "tiingo", "yahoo")
SOURCES = VENDORS + ("stored",)
PRECEDENCE_EARLY = ("wiki", "tiingo", "yahoo")
PRECEDENCE_LATE = ("tiingo", "yahoo", "wiki")
FETCHED_OK = {"done", "done_review", "partial"}

TOL_R = 0.005          # R3: two sources' returns agree within 0.5%
TOL_LEVEL = 0.01       # R7: raw closes within 1%
TOL_RATIO = 0.001      # split ratios agree within 0.1%
TOL_STORED_RATIO = 0.02  # the stored file's implied factor (its return carries dividend adjustments)
TOL_ADJ = 1e-8         # Tiingo adjClose identity (plan 4.2)
MOVE_2X = 2.0          # R1: a price ratio of 2x or 0.5x
MOVE_BIG = 0.40        # R1: |tr| >= 40%
SPLIT_LIKE = 1.4       # a jump this large (or 1/1.4) that is a split ratio is a split candidate
SPECIAL_PCT = 0.10     # cash above 10% of the prior raw close
FLAT_RUN = 3           # R4: identical raw closes in a row
VOLUME_DIFF = 2.0      # vendor volumes this many times apart
SPLICE_MIN = 20        # R8: overlapping sessions before a change of primary source
SPLICE_STAY = 20       # a change of primary source counts as a splice when it lasts this long
STORED_EX_FROM = "2023-01-01"
BREAK_DAYS = ("2025-06-24", "2026-06-29")  # stored-file unit breaks (plan 4.2: 2025-06-24, HON 2026-06-29)
TOL_UNIT = 0.01        # the stored file alone off by an ordinary split ratio within 1%: a stored unit change
TOL_CASH_SPLIT = 0.01  # a vendor's cash within 1% of the value of a split another vendor records
STORED_QUEUE_DIFF = 0.02  # a single vendor and the stored file this far apart in r: queued (R3)
RATIO_QUEUE_TR = 0.10  # |tr| this large on an ex-date whose ratio rests on one source: queued (R1c)
FILLER_VOLUME_SHARE = 0.01  # R5: trailing repeats of the last close with volume below 1% of the median before
YAHOO_LOADER_VERSION = "2"  # part of the yahoo_new bundle signature (2: volume flags from events.csv)
# events.csv volume tokens -> the row flag on Yahoo rows dated before that ex-date
YAHOO_VOLUME_TOKENS = {"volume_restore_unverified": "yahoo_volume_unverified",
                       "volume_not_scaled_by_yahoo": "yahoo_volume_not_scaled"}
V_SAMPLE_REASON = "V_verify_sample"  # candidate_fetch_list.csv reason of plan 6's 50-name V sample
# Plan 4.3's known cases: (ticker, ex_date, kind, factor or None).
KNOWN_CASES = [("EBAY", "2015-07-20", "spinoff_cash", None), ("CTXS", "2017-02-01", "spinoff_cash", None),
               ("LVNTA", "2014-08-28", "spinoff_cash", None), ("DISCK", "2014-08-07", "spinoff_cash", None),
               ("HON", "2025-10-30", "spinoff_ratio", 1.061), ("NUAN", "2019-10-02", "spinoff_ratio", 1.155),
               ("HON", "2026-06-29", "spinoff_ratio", 0.9535), ("PRPL", "2026-07-20", "reverse_split", 0.04),
               ("SIRI", "2024-09-10", "reverse_split", 0.1), ("NFLX", "2025-11-17", "split", 10.0),
               ("BKNG", "2026-04-06", "split", 25.0), ("KLAC", "2026-06-12", "split", 10.0),
               ("MNST", "2026-08-11", "split", 2.0)]
KNOWN_SPINOFFS = {(t, d) for t, d, kind, _ in KNOWN_CASES if kind.startswith("spinoff")}
# Plan 4.4 R3's known disagreements, checked in the summary.
KNOWN_DISAGREEMENTS = [("NFLX", "2013-10-22"), ("KLAC", "2015-01-23"), ("MNST", "2015-03-12"), ("MNST", "2015-03-13")]

# Listed again after a Form 25 with new shares: a bankruptcy plan cancelled the old shares and issued new
# ones (or exchanged the old for new). The old and the new shares are separate series joined by a junction
# on the first session of the new shares: no return is computed across it, and the old shares end there
# with a terminal event (step 11 values it; series_ends.csv and relist_junctions.csv carry the dates).
# Hand-reviewed from the SEC filings named (``read`` False: only the cached submissions index was seen,
# the document itself was not read here). A relisting that keeps the same shares (SMCI 2020-01, removed
# for late filings; SIGA, SCOR, MDXG) has no junction.
_SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/"
RELIST_JUNCTIONS = {
    "1486159": {"first_new_session": "2020-11-20", "kind": "bankruptcy_new_equity", "read": True,
                "url": _SEC_ARCHIVE + "1486159/000148615920000115/oas-20201119.htm",
                "note": "Oasis Petroleum's plan became effective on 2020-11-19 (the Effective Date): the existing common "
                        "stock was cancelled and its holders received warrants for the new common stock, which trades "
                        "on Nasdaq as OAS from 2020-11-20 (CHRD after the 2022 Whiting merger)"},
    "1839341": {"first_new_session": "2024-01-24", "kind": "bankruptcy_share_exchange", "read": False,
                "url": _SEC_ARCHIVE + "1839341/000119312524013078/d661343d8k.htm",
                "note": "emergence 8-K filed 2024-01-23 (Items 1.01, 1.02, 2.03, 3.02, 3.03, 5.01, 5.02, 5.03; not read "
                        "here); the new CORZ shares' first trade is 2024-01-24 (Yahoo); the old shares traded OTC as "
                        "CORZQ"},
    "105319": {"first_new_session": "2025-06-27", "kind": "bankruptcy_share_exchange", "read": True,
               "url": _SEC_ARCHIVE + "105319/000119312525146171/d906370d8k.htm",
               "note": "the plan became effective on 2025-06-24: the old common stock was cancelled, and 900,000 new "
                       "shares went to the holders of existing equity interests (9,100,000 to the first-lien lenders); "
                       "the new shares' first trade is 2025-06-27 (Yahoo)"},
    "1456772": {"first_new_session": "2026-06-18", "kind": "bankruptcy_new_equity", "read": False,
                "url": _SEC_ARCHIVE + "1456772/000110465926076652/tm2618043d2_8k.htm",
                "note": "emergence 8-K filed 2026-06-23 (Items 1.01, 1.02, 1.03, 2.03, 3.02, 3.03, 5.01, 5.02, 5.03; not "
                        "read here); Yahoo's first new-share row is 2026-06-18 (volume 0), its first traded row "
                        "2026-06-22"},
}
RELIST_JUMP = 10.0          # unreviewed relisting: a raw level change this large (or 1/10) with no split is queued (R9)
RELIST_SCREEN_BEFORE_DAYS, RELIST_SCREEN_AFTER_DAYS = 20, 60  # the screen's span: Form 25 - 20 days to relisting + 60

PRICE_COLUMNS = ["date", "close_raw", "volume_raw", "split_factor", "div_cash", "tr", "src_primary",
                 "n_sources", "max_src_diff", "flags"]
SPLIT_COLUMNS = ["security_id", "ticker", "ex_date", "split_factor", "event_type", "tiingo", "yahoo", "wiki",
                 "nasdaq", "agree", "sec_url", "verified_at", "notes", "tr_agree", "sources_confirming",
                 "stored_implied_k", "stored_state", "sec_candidates"]
SPECIAL_COLUMNS = ["security_id", "ex_date", "cash", "prior_close_raw", "pct_of_prior", "classification", "sec_url",
                   "ticker", "ratio", "sources", "notes", "sec_candidates"]
MOVE_COLUMNS = ["ticker", "event_date", "classification", "source_url", "verified_at", "notes", "security_id",
                "sources_agreeing"]


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def configure_paths(out_dir: Path | None) -> None:
    """Send every output under ``out_dir`` (``prices/``, ``reconcile/`` with its source bundles and
    per-security state, and ``inputs/`` for the three tables) instead of CACHE and INPUTS. The inputs
    (candidate list, caches, master) are still read from their usual places."""
    global PRICES_DIR, OUT, SOURCE_CACHE, STATE_DIR, LOG_DIR, SPLIT_EVENTS, SPECIAL, REVIEWED_MOVES
    if out_dir is None:
        return
    root = Path(out_dir).resolve()
    PRICES_DIR, OUT = root / "prices", root / "reconcile"
    SOURCE_CACHE, STATE_DIR, LOG_DIR = OUT / "sources", OUT / "per_security", OUT / "logs"
    SPLIT_EVENTS, SPECIAL, REVIEWED_MOVES = (root / "inputs" / name for name in
                                             ("split_events.csv", "special_distributions.csv", "reviewed_moves.csv"))


MIN_FREE_MB = 500  # stop before the disk this step writes to has less free space (other runs share it)
WRITE_SOURCE_CACHE = True  # --no-source-cache: source bundles are built in memory and not written


def ensure_disk(path: Path, min_free_mb: float | None = None) -> None:
    """Refuse to write when the disk holding ``path`` has less than MIN_FREE_MB free: a build must not
    fill the disk that a running fetch (the Tiingo month-1 run) writes to."""
    import shutil
    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    free = shutil.disk_usage(target).free / (1024 * 1024)
    limit = MIN_FREE_MB if min_free_mb is None else min_free_mb
    if free < limit:
        raise RuntimeError(f"only {free:,.0f} MB free on the disk of {path} (the limit is {limit:,.0f} MB): stopped "
                           "before writing; free space or pass --min-free-mb")


def peak_rss_mb() -> float:
    """The process's peak resident memory so far (ru_maxrss: bytes on macOS, kilobytes on Linux)."""
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1)


def forbid_network() -> None:
    """This step reads local files only (SEC candidates come from the cached submissions files): any
    socket connection in this process is refused, so a build is offline by construction."""
    import socket

    def refuse(*_args, **_kwargs):
        raise RuntimeError("reversal_data_reconcile is offline: a network connection was attempted")

    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    socket.create_connection = refuse


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_csv(path: Path, frame: pd.DataFrame, compress: bool = False, float_format: str | None = "%.10g") -> None:
    text = frame.to_csv(index=False, float_format=float_format)
    data = gzip.compress(text.encode("utf-8"), compresslevel=3, mtime=0) if compress else text.encode("utf-8")
    common.atomic_write(path, data)


def write_json(path: Path, payload) -> None:
    common.atomic_write(path, (json.dumps(payload, indent=1, default=str, sort_keys=False) + "\n").encode("utf-8"))


def file_signature(paths) -> str:
    """sha256 over (name, size, mtime) of ``paths``: a cache key that changes when any file does."""
    digest = hashlib.sha256(CODE_VERSION.encode())
    for path in sorted(str(p) for p in paths):
        try:
            stat = os.stat(path)
            digest.update(f"{path}|{stat.st_size}|{stat.st_mtime_ns}\n".encode())
        except FileNotFoundError:
            digest.update(f"{path}|missing\n".encode())
    return digest.hexdigest()


# ------------------------------------------------------------------ ratios and the return formula

def total_return(close: np.ndarray, split: np.ndarray, div: np.ndarray, prev_close: np.ndarray) -> np.ndarray:
    """Plan 4.1: r_t = (C_t x S_t + D_t) / C_{t-1} - 1 (NaN where a term is missing)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return (close * split + div) / prev_close - 1.0


def ordinary_ratio(value: float, tolerance: float = TOL_RATIO) -> bool:
    """A share split: n:1 or 1:n with n up to 100, or n:m with both up to 10 (the Yahoo builder's
    rule). Anything else (1275:1000, 1.061, 0.9535, 4.423) is a distribution served as a ratio."""
    if not value or value <= 0 or not np.isfinite(value):
        return False
    for whole in (value, 1 / value):
        if 1 <= round(whole) <= 100 and abs(round(whole) / whole - 1) <= tolerance:
            return True
    for denominator in range(1, 11):
        numerator = round(value * denominator)
        if 1 <= numerator <= 10 and abs(numerator / denominator / value - 1) <= tolerance:
            return True
    return False


def ratio_match(a: float, b: float, tolerance: float = TOL_RATIO) -> bool:
    return bool(np.isfinite(a) and np.isfinite(b) and b != 0 and abs(a / b - 1) <= tolerance)


# ------------------------------------------------------------------ identity: spans, tickers, targets

def load_identity() -> dict:
    identity = identity_from(pf.load_intervals(), pf.load_master())
    identity["signature"] = file_signature([pf.MASTER, pf.INTERVALS])
    return identity


def identity_from(intervals: pd.DataFrame, master: pd.DataFrame) -> dict:
    """Listing spans (``pf.listing_spans``: an interval that starts on or after the Form 25 delist date is
    a later listing, ``after_cut``, and is not cut to the delist date, so SMCI 2020-01..2026 and CHRD
    stay listed), the mapping spans that assign ticker-keyed rows, and per security its relistings:
    (Form 25 delist date, first day of the later listing, last day of it)."""
    spans = pf.listing_spans(intervals, master)
    mapping = pf.mapping_spans(spans)
    after = mapping["after_cut"].astype(str).eq("True") if "after_cut" in mapping else pd.Series(False, index=mapping.index)
    delist = dict(zip(master["security_id"], master["delist_date"]))
    relisted = {sid: [(delist.get(sid, ""), str(part["list_start"].min()), str(part["list_end"].max()))]
                for sid, part in mapping[after].groupby("security_id")}
    return {"master": master, "spans": spans, "mapping": mapping, "ticker_map": pf.TickerMap(mapping),
            "relisted": relisted}


def load_targets() -> pd.DataFrame:
    """Candidate-list securities plus every security ranked <= 300 (dv20 or dv50) in any week."""
    candidates = pd.read_csv(CANDIDATES, dtype=str, keep_default_na=False)
    weekly = pd.read_pickle(WEEKLY_METRICS)
    best = weekly.groupby("security_id").agg(best_dv50=("dv50_rank", "min"), best_dv20=("dv20_rank", "min"))
    top = weekly[(weekly["dv50_rank"] <= 300) | (weekly["dv20_rank"] <= 300)]
    weeks300 = top.groupby("security_id").size().rename("weeks_rank300")
    reasons = candidates.groupby("security_id").agg(
        candidate_reasons=("reason", lambda r: " ".join(sorted(set(r)))),
        planned_sources=("planned_source", lambda r: " ".join(sorted(set(r)))),
        candidate_status=("status", lambda r: " ".join(sorted(set(r)))))
    ids = sorted(set(candidates["security_id"]) | set(top["security_id"]))
    frame = pd.DataFrame({"security_id": ids}).set_index("security_id")
    frame = frame.join(reasons).join(best).join(weeks300)
    frame["in_candidates"] = frame["candidate_reasons"].notna()
    frame["rank300"] = frame["weeks_rank300"].fillna(0) > 0
    return frame.reset_index()


def security_windows(mapping: pd.DataFrame, targets: set[str]) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    """security -> (first day, last day) of its window: spans, 75 days of warm-up, 28 of hold."""
    part = mapping[mapping["security_id"].isin(targets)]
    whole = part.groupby("security_id").agg(first=("list_start", "min"), last=("list_end", "max"))
    low, high = pd.Timestamp(WINDOW_START), pd.Timestamp(WINDOW_END)
    return {sid: (max(low, pd.Timestamp(a) - pd.Timedelta(days=WARMUP_DAYS)),
                  min(high, pd.Timestamp(b) + pd.Timedelta(days=HOLD_DAYS)))
            for sid, a, b in zip(whole.index, whole["first"], whole["last"])}


def ticker_lookup(mapping: pd.DataFrame, sid: str):
    """date -> the ticker the security held then (its nearest span's ticker outside every span)."""
    rows = mapping[mapping["security_id"] == sid].sort_values("list_start")
    starts = pd.to_datetime(rows["list_start"]).values
    ends = pd.to_datetime(rows["list_end"]).values
    tickers = rows["ticker"].values

    def lookup(day) -> str:
        if not len(tickers):
            return ""
        day = np.datetime64(pd.Timestamp(day))
        inside = np.flatnonzero((starts <= day) & (ends >= day))
        if len(inside):
            return str(tickers[inside[-1]])
        distance = np.minimum(np.abs((starts - day).astype("timedelta64[D]").astype(int)),
                              np.abs((ends - day).astype("timedelta64[D]").astype(int)))
        return str(tickers[int(distance.argmin())])

    return lookup


def listed_mask(mapping: pd.DataFrame, sid: str, dates: pd.DatetimeIndex) -> np.ndarray:
    inside = np.zeros(len(dates), dtype=bool)
    values = dates.values
    for start, end in zip(mapping.loc[mapping["security_id"] == sid, "list_start"],
                          mapping.loc[mapping["security_id"] == sid, "list_end"]):
        inside |= (values >= np.datetime64(start)) & (values <= np.datetime64(end))
    return inside


# ------------------------------------------------------------------ source loaders (long rows)

ROW_COLUMNS = ["security_id", "date", "close", "volume", "split", "div", "rowflag", "file"]


def _rows(sid, dates, close, volume, split, div, rowflag, file) -> pd.DataFrame:
    n = len(dates)
    def column(value, fill):
        if np.isscalar(value):
            return np.full(n, float(value))
        return pd.to_numeric(pd.Series(value), errors="coerce").fillna(fill).values

    frame = pd.DataFrame({"date": pd.to_datetime(pd.Series(dates)).dt.normalize().values,
                          "close": pd.to_numeric(pd.Series(close), errors="coerce").values,
                          "volume": pd.to_numeric(pd.Series(volume), errors="coerce").values,
                          "split": column(split, 1.0), "div": column(div, 0.0)})
    frame["security_id"] = sid if isinstance(sid, str) else pd.Series(sid).values
    frame["rowflag"] = rowflag if isinstance(rowflag, str) else pd.Series(rowflag).values
    frame["file"] = file
    return frame[ROW_COLUMNS]


def _empty_rows() -> pd.DataFrame:
    return pd.DataFrame(columns=ROW_COLUMNS)


def _assign(frame: pd.DataFrame, ticker: str, ticker_map: pf.TickerMap, targets: set[str]) -> pd.DataFrame:
    """Rows of a ticker-keyed file given to the security that held the ticker then (``pf.TickerMap``)."""
    owner, _direct = ticker_map.assign(ticker, frame["date"].values)
    keep = np.isin(owner, list(targets))
    out = frame[keep].copy()
    out["security_id"] = owner[keep]
    return out


def target_tickers(mapping: pd.DataFrame, targets: set[str]) -> set[str]:
    return set(mapping.loc[mapping["security_id"].isin(targets), "ticker"])


def load_wiki_rows(identity: dict, targets: set[str]) -> pd.DataFrame:
    from scripts.reversal_data_wiki import READ_KW, safe_ticker

    failed = set()
    if WIKI_ENTITY.exists():  # step 6's LastSale check (plan 4.4 R9): files that fail are left out
        entity = pd.read_csv(WIKI_ENTITY, dtype=str, keep_default_na=False)
        bad = entity[entity["fails"] == "True"]
        failed = set(zip(bad["security_id"], bad["file"]))
    out = []
    for ticker in sorted(target_tickers(identity["mapping"], targets)):
        path = WIKI_DIR / f"{safe_ticker(ticker)}.csv.gz"
        if not path.exists():
            continue
        data = pd.read_csv(path, usecols=["date", "close", "volume", "ex-dividend", "split_ratio"], **READ_KW)
        rows = _rows("", data["date"], data["close"], data["volume"], data["split_ratio"], data["ex-dividend"], "",
                     path.name)
        rows = _assign(rows, ticker, identity["ticker_map"], targets)
        if failed:
            rows = rows[[(s, path.name) not in failed for s in rows["security_id"]]]
        if len(rows):
            out.append(rows)
    return pd.concat(out, ignore_index=True) if out else _empty_rows()


def _tiingo_frame(prices: list[dict], file: str) -> pd.DataFrame:
    data = pd.DataFrame(prices)
    if data.empty:
        return _empty_rows()
    rows = _rows("", data["date"].astype(str).str[:10], data["close"], data["volume"], data.get("splitFactor", 1.0),
                 data.get("divCash", 0.0), "", file)
    adj = pd.to_numeric(data.get("adjClose"), errors="coerce").values if "adjClose" in data else np.full(len(data), np.nan)
    rows["adj"] = adj
    return rows


def adj_identity_flags(rows: pd.DataFrame) -> np.ndarray:
    """Plan 4.2's Tiingo row check: adjClose_t / adjClose_{t-1} against the formula, within 1e-8."""
    if "adj" not in rows or rows.empty:
        return np.zeros(len(rows), dtype=bool)
    close, adj = rows["close"].values, rows["adj"].values
    formula = total_return(close[1:], rows["split"].values[1:], rows["div"].values[1:], close[:-1]) + 1.0
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = adj[1:] / adj[:-1]
        error = np.abs(ratio / formula - 1.0)
    bad = np.zeros(len(rows), dtype=bool)
    bad[1:] = np.nan_to_num(error, nan=0.0) > TOL_ADJ
    return bad


def load_old_tiingo_rows(identity: dict, targets: set[str]) -> pd.DataFrame:
    """The Tiingo JSON caches the prefilter counted ({meta, prices}); a Q ticker carries the Nasdaq
    history of the ticker without its Q (SIVBQ, BBBYQ), as in ``pf.read_tiingo``."""
    tickers = identity["ticker_map"].tickers()
    out = []
    for directory in OLD_TIINGO_DIRS:
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            payload = json.loads(path.read_text(encoding="utf-8"))
            ticker = str((payload.get("meta") or {}).get("ticker") or path.stem).upper()
            if ticker not in tickers and ticker.endswith("Q") and ticker[:-1] in tickers:
                ticker = ticker[:-1]
            rows = _tiingo_frame(payload.get("prices") or [], f"{directory.name}/{path.name}")
            if rows.empty:
                continue
            rows = rows.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
            rows["rowflag"] = np.where(adj_identity_flags(rows), "tiingo_adj_identity", "")
            rows = _assign(rows.drop(columns="adj"), ticker, identity["ticker_map"], targets)
            if len(rows):
                out.append(rows)
    return pd.concat(out, ignore_index=True) if out else _empty_rows()


def load_new_tiingo_rows(identity: dict, targets: set[str], windows: dict) -> tuple[pd.DataFrame, dict]:
    """The month-1 run's answers, through each security's own status row. A file shared by several
    securities is cut to each one's own listing spans; otherwise to its window."""
    facts = {"status_rows": 0, "status_counts": {}, "files_read": 0, "files_unreadable": [], "shared_files": 0}
    if not TIINGO_STATUS.exists():
        return _empty_rows(), facts
    status = pd.read_csv(TIINGO_STATUS, dtype=str, keep_default_na=False)
    facts["status_rows"] = int(len(status))
    facts["status_counts"] = {k: int(v) for k, v in status["status"].value_counts().items()}
    usable = status[status["status"].isin(FETCHED_OK) & status["security_id"].isin(targets) &
                    status["prices_path"].ne("")]
    users = usable.groupby("prices_path")["security_id"].nunique()
    mapping = identity["mapping"]
    out, cache = [], {}
    order = {"done": 0, "done_review": 1, "partial": 2}
    usable = usable.assign(rank=usable["status"].map(order)).sort_values(["security_id", "rank"])
    for row in usable.itertuples(index=False):
        path = Path(row.prices_path)
        if path not in cache:
            try:
                data = pd.read_csv(path, dtype={"date": str})
                frame = _tiingo_frame(data.to_dict("records"), f"tiingo_run/{path.name}")
                frame = frame.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
                frame["rowflag"] = np.where(adj_identity_flags(frame), "tiingo_adj_identity", "")
                cache[path] = frame.drop(columns="adj")
                facts["files_read"] += 1
            except Exception as exc:  # a file being written: the next run reads it
                facts["files_unreadable"].append(f"{path.name}: {type(exc).__name__}")
                cache[path] = None
        frame = cache[path]
        if frame is None or frame.empty:
            continue
        rows = frame.copy()
        rows["security_id"] = row.security_id
        dates = pd.DatetimeIndex(rows["date"])
        if users.get(row.prices_path, 1) > 1:
            facts["shared_files"] += 1
            keep = listed_mask(mapping, row.security_id, dates)
        else:
            low, high = windows.get(row.security_id, (pd.Timestamp(WINDOW_START), pd.Timestamp(WINDOW_END)))
            keep = (dates >= low) & (dates <= high)
        if row.status == "done_review":
            rows["rowflag"] = (rows["rowflag"] + " tiingo_done_review").str.strip()
        out.append(rows[keep])
    if not out:
        return _empty_rows(), facts
    rows = pd.concat(out, ignore_index=True)
    return rows.drop_duplicates(["security_id", "date"], keep="first"), facts


def yahoo_restore(result: dict) -> pd.DataFrame:
    """A v8 chart's daily bars restored to raw: close x F_t, volume / F_t, dividends x F_t, with
    F_t the product of the split ratios dated after t; S on the ex-date; odd ratios flagged."""
    stamps = pd.to_datetime(result.get("timestamp") or [], unit="s")
    if not len(stamps):
        return _empty_rows()
    dates = stamps.tz_localize("UTC").tz_convert("America/New_York").tz_localize(None).normalize()
    quote = result["indicators"]["quote"][0]
    close = pd.to_numeric(pd.Series(quote.get("close")), errors="coerce").values
    volume = pd.to_numeric(pd.Series(quote.get("volume")), errors="coerce").values
    events = result.get("events") or {}
    factor = np.ones(len(dates))
    split = np.ones(len(dates))
    flag = np.full(len(dates), "", dtype=object)
    day_index = {d: k for k, d in enumerate(dates)}
    for event in (events.get("splits") or {}).values():
        day = pd.to_datetime(event["date"], unit="s", utc=True).tz_convert("America/New_York").tz_localize(None).normalize()
        ratio = float(event["numerator"]) / float(event["denominator"])
        factor[dates < day] *= ratio
        if day in day_index:
            split[day_index[day]] = ratio
            if not ordinary_ratio(ratio):
                flag[day_index[day]] = "yahoo_odd_ratio"
    div = np.zeros(len(dates))
    for event in (events.get("dividends") or {}).values():
        day = pd.to_datetime(event["date"], unit="s", utc=True).tz_convert("America/New_York").tz_localize(None).normalize()
        if day in day_index:
            k = day_index[day]
            div[k] += float(event["amount"]) * factor[k]
    frame = _rows("", dates, close * factor, volume / factor, split, div, flag, "")
    return frame.dropna(subset=["close"]).drop_duplicates("date", keep="last")


def load_old_yahoo_rows(identity: dict, targets: set[str], have_new: set[str]) -> pd.DataFrame:
    """The holdout's v8 charts, for securities without a step-7 file (plan 4.2's restore)."""
    out = []
    wanted = targets - have_new
    for directory in OLD_YAHOO_DIRS:
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            result = json.loads(path.read_text(encoding="utf-8"))["chart"]["result"][0]
            if (result.get("meta") or {}).get("dataGranularity", "1d") != "1d":
                continue
            ticker = str((result.get("meta") or {}).get("symbol") or path.stem).upper()
            rows = yahoo_restore(result)
            if rows.empty:
                continue
            rows["file"] = f"{directory.name}/{path.name}"
            rows = _assign(rows, ticker, identity["ticker_map"], wanted)
            if len(rows):
                out.append(rows)
    return pd.concat(out, ignore_index=True) if out else _empty_rows()


def load_new_yahoo_rows(targets: set[str]) -> pd.DataFrame:
    """Step 7's restored files (one per security); junction rows and junction-flagged events are not
    splits, odd ratios are distributions (``events.csv`` flags). An event whose volume restore WIKI
    could not test (``volume_restore_unverified``) or that Yahoo served unscaled
    (``volume_not_scaled_by_yahoo``) marks every row dated before its ex-date, since the restore
    divides those rows' volume by the ratio (``yahoo_volume_unverified`` / ``yahoo_volume_not_scaled``)."""
    flags = defaultdict(set)
    volume_events = defaultdict(list)  # security -> [(ex_date, row flag)]
    if YAHOO_EVENTS.exists():
        events = pd.read_csv(YAHOO_EVENTS, dtype=str, keep_default_na=False)
        for sid, day, kind, text in zip(events["security_id"], events["ex_date"], events["event_type"], events["flags"]):
            for token in text.split():
                if token in ("odd_ratio", "trim_junction", "segment_junction", "not_applied_by_yahoo",
                             "volume_restore_unverified", "volume_not_scaled_by_yahoo"):
                    flags[(sid, day)].add(token)
                if token in YAHOO_VOLUME_TOKENS:
                    volume_events[sid].append((day, YAHOO_VOLUME_TOKENS[token]))
    out = []
    for sid in sorted(targets):
        path = YAHOO_DIR / f"{sid}.csv.gz"
        if not path.exists():
            continue
        data = pd.read_csv(path, dtype={"date": str, "junction": str}, keep_default_na=False, na_values=[""])
        rowflag = np.full(len(data), "", dtype=object)
        junction = data.get("junction", pd.Series([""] * len(data))).fillna("").eq("Y").values
        for k, day in enumerate(data["date"]):
            tokens = flags.get((sid, day))
            names = []
            if junction[k] or (tokens and tokens & {"trim_junction", "segment_junction"}):
                names.append("yahoo_junction")
            if tokens and "odd_ratio" in tokens:
                names.append("yahoo_odd_ratio")
            if tokens and "not_applied_by_yahoo" in tokens:
                names.append("yahoo_not_applied")
            if names:
                rowflag[k] = " ".join(names)
        dates = data["date"].astype(str).values
        for day, name in volume_events.get(sid, ()):
            for k in np.flatnonzero(dates < day):
                if name not in rowflag[k].split():
                    rowflag[k] = f"{rowflag[k]} {name}".strip()
        rows = _rows(sid, data["date"], data["close_raw"], data["volume_raw"], data["split_factor"], data["div_cash"],
                     rowflag, f"yahoo/{path.name}")
        out.append(rows)
    return pd.concat(out, ignore_index=True) if out else _empty_rows()


def load_stored_rows(identity: dict, targets: set[str]) -> pd.DataFrame:
    """The stored repo files: close only matters (its return is the vote); rows go to securities by
    ticker spans and the step-4 file-owner rule (``pf.keep_stored_owner``)."""
    tickers = target_tickers(identity["mapping"], targets)
    frames = []
    for path in sorted(STORED_DIR.glob("*.csv")):
        ticker = path.stem.upper().split("_")[0]
        if ticker not in tickers:
            continue
        data = pd.read_csv(path, usecols=lambda c: c in ("date", "close", "volume"))
        if not {"date", "close", "volume"} <= set(data.columns):
            continue
        frames.append(pf._frame(ticker, data["date"], data["close"], data["volume"], "stored", path.name))
    rows = pf.assign_securities(frames, identity["ticker_map"])
    rows, _facts = pf.keep_stored_owner(rows, identity["master"], identity["ticker_map"])
    rows = rows[rows["security_id"].isin(targets)]
    rows = rows.sort_values(["security_id", "date", "direct"], ascending=[True, True, False])
    rows = rows.drop_duplicates(["security_id", "date"], keep="first")
    out = _rows(rows["security_id"].values, rows["date"].values, rows["close"].values, rows["volume"].values,
                1.0, 0.0, "", "")
    out["file"] = rows["file"].values
    return out


def cached_bundle(name: str, signature: str, build, rebuild: bool = False) -> pd.DataFrame:
    """A source bundle from ``SOURCE_CACHE/{name}.pkl`` when its signature matches (and ``rebuild`` is
    off), else rebuilt from the raw files."""
    path = SOURCE_CACHE / f"{name}.pkl"
    for candidate in dict.fromkeys([path, DEFAULT_SOURCE_CACHE / f"{name}.pkl"]):
        if not candidate.exists() or rebuild:
            continue
        try:
            with candidate.open("rb") as handle:
                stored = pickle.load(handle)
            if stored.get("signature") == signature:
                log(f"source {name}: cached bundle {candidate}")
                return stored["rows"]
        except Exception:
            pass
    started = time.time()
    rows = build()
    if WRITE_SOURCE_CACHE:
        ensure_disk(path)
        common.atomic_write(path, pickle.dumps({"signature": signature, "rows": rows}, protocol=pickle.HIGHEST_PROTOCOL))
    log(f"source {name}: {len(rows):,} rows for {rows['security_id'].nunique() if len(rows) else 0} securities "
        f"({time.time() - started:.0f}s)")
    return rows


def load_sources(identity: dict, targets: set[str], windows: dict, rebuild: bool = False) -> tuple[dict[str, pd.DataFrame], dict]:
    base = identity["signature"] + hashlib.sha256(" ".join(sorted(targets)).encode()).hexdigest()
    wiki_sig = file_signature(list(WIKI_DIR.glob("*.csv.gz")) + [WIKI_ENTITY]) + base
    stored_sig = file_signature(list(STORED_DIR.glob("*.csv")) + [pf.PRICE_FILE_OWNERS]) + base
    old_tiingo_sig = file_signature([p for d in OLD_TIINGO_DIRS for p in d.glob("*.json")]) + base
    yahoo_files = list(YAHOO_DIR.glob("*.csv.gz")) + [YAHOO_EVENTS]
    new_yahoo_sig = file_signature(yahoo_files) + base + YAHOO_LOADER_VERSION
    have_new = {p.name[:-len(".csv.gz")] for p in YAHOO_DIR.glob("*.csv.gz")}
    old_yahoo_sig = file_signature([p for d in OLD_YAHOO_DIRS for p in d.glob("*.json")]) + base + \
        hashlib.sha256(" ".join(sorted(have_new)).encode()).hexdigest()
    sources = {
        "wiki": cached_bundle("wiki", wiki_sig, lambda: load_wiki_rows(identity, targets), rebuild),
        "stored": cached_bundle("stored", stored_sig, lambda: load_stored_rows(identity, targets), rebuild),
        "tiingo_old": cached_bundle("tiingo_old", old_tiingo_sig, lambda: load_old_tiingo_rows(identity, targets), rebuild),
        "yahoo_new": cached_bundle("yahoo_new", new_yahoo_sig, lambda: load_new_yahoo_rows(targets), rebuild),
        "yahoo_old": cached_bundle("yahoo_old", old_yahoo_sig, lambda: load_old_yahoo_rows(identity, targets, have_new),
                                   rebuild),
    }
    tiingo_new, tiingo_facts = load_new_tiingo_rows(identity, targets, windows)
    sources["tiingo_new"] = tiingo_new
    log(f"source tiingo_new: {len(tiingo_new):,} rows for {tiingo_new['security_id'].nunique() if len(tiingo_new) else 0}"
        f" securities ({tiingo_facts['files_read']} files read)")
    return sources, {"tiingo_run": tiingo_facts}


def split_by_security(rows: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if rows.empty:
        return {}
    rows = rows.sort_values(["security_id", "date"], kind="stable")
    return {sid: part.reset_index(drop=True) for sid, part in rows.groupby("security_id", sort=False)}


# ------------------------------------------------------------------ per-security reconciliation

SRC = list(SOURCES)  # wiki, tiingo, yahoo, stored (row order of the arrays below)
W, T_, Y, ST = 0, 1, 2, 3


_SPLIT_CANDIDATES = [float(n) for n in range(2, 101)] + [1.0 / n for n in range(2, 101)] + \
                    [n / m for m in range(2, 11) for n in range(1, 11) if n % m]


def near_split_factor(factor: float, tolerance: float = 0.025) -> float | None:
    """``factor`` as an ordinary split factor (n:1, 1:n up to 100, n:m up to 10) within ``tolerance``,
    when it is split-sized (>= 1.4 or <= 1/1.4); None otherwise. With 2.5% nearly every factor beyond
    1.4x qualifies (the n:m set is dense there), so it only says "split-sized"; 1% (``TOL_UNIT``)
    is the test for a stored unit change."""
    if factor is None or not np.isfinite(factor) or factor <= 0:
        return None
    if 1 / SPLIT_LIKE < factor < SPLIT_LIKE:
        return None
    best = min(_SPLIT_CANDIDATES, key=lambda c: abs(factor / c - 1))
    return float(best) if abs(factor / best - 1) <= tolerance else None


def strict_split_factor(factor: float, tolerance: float = 0.01) -> float | None:
    """For R2 (a split no source records): only n:1 or 1:n (n = 2..100), 3:2 and 2:3, within 1%."""
    if factor is None or not np.isfinite(factor) or factor <= 0:
        return None
    candidates = [float(n) for n in range(2, 101)] + [1.0 / n for n in range(2, 101)] + [1.5, 2 / 3]
    best = min(candidates, key=lambda c: abs(factor / c - 1))
    return float(best) if abs(factor / best - 1) <= tolerance else None


def stored_disagreements(C: np.ndarray, r: np.ndarray, valid: np.ndarray, choice: dict, p: np.ndarray) -> np.ndarray:
    """Days where the only dissent is the stored file against a single vendor (no majority). The
    stored file is a vote, never a level, and has its own bad rows (LANC 2020-2022 mixes adjusted
    and unadjusted closes), so such a day is not left unresolved:
    - ``stored_glitch``: the stored/vendor level ratio is back within 0.5% of the day before within
      two sessions, or is back today at its level of two or three sessions ago (a bad stored row);
    - ``stored_shift``: the ratio moves by 2% or more and stays (the stored file adjusts for something
      the vendor may lack, or changes units): queued for review;
    - ``stored_disagrees``: a smaller lasting shift (an adjustment on another date)."""
    n = C.shape[1]
    out = np.full(n, "", dtype=object)
    only_one_vendor = valid[:3].sum(axis=0) == 1
    days = np.flatnonzero(choice["unresolved"] & only_one_vendor & valid[ST])
    cols = np.arange(n)
    level = C[ST] / C[p, cols]
    for k in days:
        if k < 1 or not np.isfinite(level[k - 1]):
            out[k] = "stored_disagrees"
            continue
        base = level[k - 1]
        after = [level[j] / base - 1 for j in (k + 1, k + 2) if j < n and np.isfinite(level[j])]
        back = [level[k] / level[j] - 1 for j in (k - 2, k - 3) if j >= 0 and np.isfinite(level[j])]
        if any(abs(v) <= TOL_R for v in after + back):  # a bad row, or the day the stored file comes back
            out[k] = "stored_glitch"
        elif abs(level[k] / base - 1) >= 0.02 and after and all(abs(v) >= 0.02 for v in after):
            out[k] = "stored_shift"
        else:
            out[k] = "stored_disagrees"
    return out


def cash_encoded_splits(C: np.ndarray, S: np.ndarray, D: np.ndarray, has: np.ndarray,
                        junction: np.ndarray) -> dict[tuple[int, int], tuple[float, float, int]]:
    """(source j, day k) -> (ratio, cash, ratio source i): a share split (an ordinary ratio S > 1)
    that vendor i records as S and vendor j, on the same day with S = 1, as cash worth the new
    shares: D within 1% of (S - 1) x C_t (WIKI's PZZA 2013-12-30 and HMSY 2011-08-17 encoding, which
    leaves the total return as it is) or of (1 - 1/S) x C_{t-1}. Real distributions (EBAY, ADP, THRX)
    have no ordinary ratio in any source, so their cash-versus-ratio typing stays."""
    out = {}
    for i in range(3):
        mask = has[i] & (S[i] > 1.0 + 1e-9)
        if i == Y:
            mask &= ~junction
        for k in np.flatnonzero(mask):
            ratio = float(S[i, k])
            if not ordinary_ratio(ratio):
                continue
            for j in range(3):
                if j == i or not has[j, k] or abs(S[j, k] - 1.0) > 1e-9 or not D[j, k] > 0 or (j == Y and junction[k]):
                    continue
                worth = [(ratio - 1.0) * C[j, k]]
                if k and has[j, k - 1]:
                    worth.append((1.0 - 1.0 / ratio) * C[j, k - 1])
                if any(np.isfinite(v) and v > 0 and abs(D[j, k] / v - 1.0) <= TOL_CASH_SPLIT for v in worth):
                    out[(j, k)] = (ratio, float(D[j, k]), i)
    return out


def stored_vote(r: np.ndarray, valid: np.ndarray, S: np.ndarray, has: np.ndarray, rank: np.ndarray,
                junction: np.ndarray, C: np.ndarray, break_day: np.ndarray | None = None) -> dict:
    """Which stored returns are not comparable with the vendors' (decided against the vendors' own
    agreement, not against a default source, so a vendor error is never booked as a stored unit
    change; PTCT 2013-06-26, CMCT 2025-01-06):
    - ``raw_jump``: a vendor records S != 1 that day and the stored file shows that raw jump
      ((1 + r_vendor) / (1 + r_stored) within 2% of the vendor's S);
    - ``unit``: every vendor with a return agrees with every other, the stored file agrees with none,
      it is off by an ordinary split ratio within 1%, the vendors' own move is not split-sized, and
      the stored/vendor level stays shifted over the next two sessions (more than 2% from where it
      was the day before; a level that comes back is a bad row, CGC 2023-06-30): a stored-only unit
      change (``unit_break`` in split_events.csv; CBIO 2025-06-02, 0.2099 -> 20.20). On a known break
      day (``break_day``) any such lasting factor beyond 2% counts, ordinary ratio or not (HON
      2025-06-24, 1.896: the file's later rows carry other adjustments too).
    Both tolerances widen by the vendor's own cent rounding, 0.005 / C_t + 0.005 / C_{t-1}, which
    matters only below a few dollars (CBIO's Yahoo closes 0.21 -> 0.20).
    A stored return that agrees with any vendor stays a vote. ``vendor_jump``: the only vendor's own
    move is split-sized (1.4x or more either way) and the stored file, a valid vote, does not show
    it (the gap between the two is split-sized too): a split that vendor lacks or a vendor error
    (R2/R3), never a stored unit change."""
    n = r.shape[1]
    cols = np.arange(n)
    vendor_valid = valid[:3]
    n_vendor = vendor_valid.sum(axis=0)
    ref = first_by_rank(valid, rank)
    ref_r = np.where(ref >= 0, r[np.clip(ref, 0, 3), cols], np.nan)
    ref_i = np.clip(ref, 0, 2)
    with np.errstate(divide="ignore", invalid="ignore"):
        ref_close = np.where(ref >= 0, C[ref_i, cols], np.nan)
        ref_prev = np.where((ref >= 0) & (cols > 0), C[ref_i, np.maximum(cols - 1, 0)], np.nan)
        allowance = np.nan_to_num(0.005 / ref_close + 0.005 / ref_prev, nan=0.0, posinf=0.0)
    high = np.where(vendor_valid, r[:3], -np.inf).max(axis=0)
    low = np.where(vendor_valid, r[:3], np.inf).min(axis=0)
    vendors_agree = (n_vendor >= 1) & (high - low <= TOL_R)
    with np.errstate(invalid="ignore"):
        close_to = vendor_valid & valid[ST][None, :] & (np.abs(r[:3] - r[ST][None, :]) <= TOL_R)
    stored_agrees = close_to.any(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        implied = (1.0 + ref_r) / (1.0 + r[ST])
    events = has[:3] & (np.abs(S[:3] - 1.0) > 1e-9)
    events[Y] &= ~junction
    raw_jump = np.zeros(n, dtype=bool)
    for k in np.flatnonzero(events.any(axis=0) & valid[ST] & ~stored_agrees):
        raw_jump[k] = any(ratio_match(implied[k], S[i, k], TOL_STORED_RATIO + allowance[k])
                          for i in range(3) if events[i, k])
    split_sized = np.array([near_split_factor(1.0 + v) is not None for v in ref_r])
    candidate = valid[ST] & ~stored_agrees
    break_day = np.zeros(n, dtype=bool) if break_day is None else break_day
    unit = np.zeros(n, dtype=bool)
    for k in np.flatnonzero(candidate & vendors_agree & ~split_sized & ~raw_jump):
        if k < 1:
            continue
        if near_split_factor(implied[k], TOL_UNIT + allowance[k]) is None:
            # on a known break day (plan 4.2: 2025-06-24, HON 2026-06-29) the stored file's own unit change
            # need not be an ordinary ratio: its later rows may carry other adjustments compounded with it
            # (HON 2025-06-24: 224.74 -> 425.80 while the vendors show -0.06%, a factor of 1.896)
            if not (break_day[k] and np.isfinite(implied[k]) and abs(implied[k] - 1.0) > TOL_STORED_RATIO + allowance[k]):
                continue
        i = ref_i[k]
        with np.errstate(divide="ignore", invalid="ignore"):
            level = lambda j: C[ST, j] / C[i, j] if 0 <= j < n and has[ST, j] and has[i, j] else np.nan
            later = [level(j) / level(k - 1) - 1.0 for j in (k + 1, k + 2)]
            back = [level(k) / level(j) - 1.0 for j in (k - 2, k - 3)]
        stays = all(abs(v) > 0.02 for v in later if np.isfinite(v))  # (or the series ends)
        returns = any(abs(v) <= 0.02 for v in back if np.isfinite(v))  # the day a bad stored row ends
        unit[k] = stays and not returns
    # the stored file does not show the jump: the gap between the two is itself split-sized (OPTT
    # 2016-06-02, Yahoo -31.0% against stored -32.2%, is a market move both show)
    gap_sized = np.array([near_split_factor(v) is not None for v in implied])
    vendor_jump = candidate & (n_vendor == 1) & split_sized & gap_sized & ~raw_jump
    return {"raw_jump": raw_jump, "unit": unit, "vendor_jump": vendor_jump, "implied_vote": implied,
            "n_vendor": n_vendor, "stored_agrees": stored_agrees}


def first_by_rank(mask: np.ndarray, rank: np.ndarray) -> np.ndarray:
    """Per column, the row index of the best-ranked True among the vendor rows (-1 when none)."""
    ranked = np.where(mask[:3], rank[:3], np.inf)
    best = ranked.argmin(axis=0)
    return np.where(np.isfinite(ranked.min(axis=0)), best, -1)


def select_sources(r: np.ndarray, valid: np.ndarray, has: np.ndarray, rank: np.ndarray) -> dict:
    """Plan 4.4 R3 per day: the sources whose returns agree within 0.5% of each other; with a strict
    majority, the best-ranked vendor in it is primary, otherwise the best-ranked vendor with a return."""
    n_valid = valid.sum(axis=0)
    diff = np.abs(r[:, None, :] - r[None, :, :])
    agree = valid[:, None, :] & valid[None, :, :] & (np.nan_to_num(diff, nan=np.inf) <= TOL_R)
    count = agree.sum(axis=1)
    major = valid & (count * 2 > n_valid)
    has_major = major[:3].any(axis=0)
    first_valid = first_by_rank(valid, rank)
    first_major = first_by_rank(major, rank)
    first_has = first_by_rank(has, rank)
    primary = np.where(has_major & (first_major >= 0), first_major,
                       np.where(first_valid >= 0, first_valid, first_has))
    unresolved = (n_valid >= 2) & ~major.any(axis=0)
    # two sources apart by more than the tolerance with no strict majority among them
    spread = np.where(valid, r, np.nan)
    with np.errstate(invalid="ignore"):
        wide = (np.nanmax(np.where(valid, spread, -np.inf), axis=0) - np.nanmin(np.where(valid, spread, np.inf), axis=0)) > TOL_R
    unresolved |= (n_valid >= 2) & wide & ~has_major
    override = has_major & (first_valid >= 0) & (primary != first_valid)
    minority = valid & ~major & has_major[None, :]
    return {"primary": primary, "n_valid": n_valid, "major": major, "unresolved": unresolved, "override": override,
            "minority": minority, "first_valid": first_valid}


def source_arrays(frames: dict[str, pd.DataFrame], grid: pd.DatetimeIndex) -> dict:
    n = len(grid)
    out = {"close": np.full((4, n), np.nan), "volume": np.full((4, n), np.nan), "split": np.ones((4, n)),
           "div": np.zeros((4, n)), "has": np.zeros((4, n), dtype=bool), "rowflag": np.full((4, n), "", dtype=object),
           "dropped_non_session": {}, "dropped_outside_window": {}}
    low, high = (grid[0], grid[-1]) if n else (None, None)
    for i, name in enumerate(SRC):
        frame = frames.get(name)
        if frame is None or frame.empty or not n:
            continue
        frame = frame[frame["close"] > 0]
        inside = (frame["date"] >= low) & (frame["date"] <= high)
        out["dropped_outside_window"][name] = int((~inside).sum())
        frame = frame[inside]
        index = grid.get_indexer(frame["date"])
        off = index < 0
        out["dropped_non_session"][name] = int(off.sum())
        frame, index = frame[~off], index[~off]
        out["close"][i, index] = frame["close"].values
        out["volume"][i, index] = frame["volume"].values
        out["split"][i, index] = frame["split"].values
        out["div"][i, index] = frame["div"].values
        out["rowflag"][i, index] = frame["rowflag"].fillna("").values
        out["has"][i, index] = True
    return out


def splice_points(kept_primary: np.ndarray, idx: np.ndarray) -> list[tuple[int, int, int]]:
    """(row, old source, new source) where one lasting primary run (SPLICE_STAY rows or more) follows
    another of a different source; shorter runs in between (a one-source gap) are not splices."""
    runs, start = [], 0
    for pos in range(1, len(kept_primary) + 1):
        if pos == len(kept_primary) or kept_primary[pos] != kept_primary[start]:
            runs.append((start, pos - 1, int(kept_primary[start])))
            start = pos
    lasting = [run for run in runs if run[1] - run[0] + 1 >= SPLICE_STAY]
    return [(int(idx[b[0]]), a[2], b[2]) for a, b in zip(lasting, lasting[1:]) if a[2] != b[2]]


def run_lengths(mask: np.ndarray) -> list[tuple[int, int]]:
    """(start, end) index pairs, inclusive, of the True runs in ``mask``."""
    if not mask.any():
        return []
    padded = np.r_[False, mask, False].astype(int)
    starts = np.flatnonzero(np.diff(padded) == 1)
    ends = np.flatnonzero(np.diff(padded) == -1) - 1
    return list(zip(starts.tolist(), ends.tolist()))


def tiny_volume_tail(close: np.ndarray, volume: np.ndarray, keep: np.ndarray) -> np.ndarray:
    """R5's second cut: the kept rows at the end of the series that repeat the close of the last real
    trade (the first row of the final run of equal closes) with a volume below ``FILLER_VOLUME_SHARE``
    of the median positive volume over the 50 rows up to that trade. A row with more volume, or with
    no volume figure, ends the cut (a missing volume is not filler)."""
    rows = np.flatnonzero(keep)
    if len(rows) < 2:
        return rows[:0]
    c, v = close[rows], volume[rows]
    start = len(rows) - 1
    while start > 0 and c[start - 1] == c[-1]:
        start -= 1
    if start == len(rows) - 1:
        return rows[:0]
    before = v[max(0, start - 49): start + 1]
    before = before[np.isfinite(before) & (before > 0)]
    if not len(before):
        return rows[:0]
    limit = FILLER_VOLUME_SHARE * float(np.median(before))
    cut = len(rows)
    for j in range(len(rows) - 1, start, -1):
        if np.isfinite(v[j]) and v[j] < limit:
            cut = j
        else:
            break
    return rows[cut:]


def reconcile_security(sid: str, frames: dict[str, pd.DataFrame], ctx: dict) -> dict:
    """The canonical series of one security and its events, queue entries and checks."""
    sessions = ctx["sessions"]
    low, high = ctx["window"]
    grid = sessions[(sessions >= low) & (sessions <= high)]
    listed = ctx["listed"](grid)
    ticker_of = ctx["ticker_of"]
    a = source_arrays(frames, grid)
    C, V, S, D, has, RF = a["close"], a["volume"], a["split"], a["div"], a["has"], a["rowflag"]
    n = len(grid)
    result = {"security_id": sid, "events": [], "specials": [], "moves": [], "pairs": [],
              "summary": {"security_id": sid, "window_start": str(low.date()), "window_end": str(high.date()),
                          "sources": " ".join(s for i, s in enumerate(SRC) if has[i].any()),
                          "dropped_non_session": a["dropped_non_session"],
                          "dropped_outside_window": a["dropped_outside_window"]}}
    has_vendor = has[:3].any(axis=0)
    if not has_vendor.any():
        result["summary"].update(rows=0, first_date="", last_date="")
        result["canonical"] = pd.DataFrame(columns=PRICE_COLUMNS)
        return result

    # relist junctions (RELIST_JUNCTIONS, ``ctx["junctions"]``): segment s runs from its junction (the first
    # session of the new shares) on. No return is computed across a junction, and an S or D a vendor
    # records on it (another history's, or the exchange served as a split) is dropped and reported.
    seg = np.zeros(n, dtype=int)
    for day in sorted(ctx.get("junctions", ())):
        start = int(grid.searchsorted(pd.Timestamp(day)))
        if 0 < start < n:
            seg[start:] += 1
    seg_break = np.r_[False, seg[1:] != seg[:-1]] if n else np.zeros(0, dtype=bool)
    junction_dropped = [f"{SRC[i]} {grid[k].date()} S={S[i, k]:.6g} D={D[i, k]:.6g}"
                        for k in np.flatnonzero(seg_break) for i in range(3)
                        if has[i, k] and (abs(S[i, k] - 1.0) > 1e-9 or D[i, k] > 0)]
    S[:3, seg_break] = 1.0
    D[:3, seg_break] = 0.0
    junction = np.array(["yahoo_junction" in f for f in RF[Y]])
    S[Y, junction] = 1.0
    D[Y, junction] = 0.0
    # a split one vendor serves as cash worth the new shares is read as that split (S, and no cash)
    cash_split = cash_encoded_splits(C, S, D, has, junction)
    for (j, k), (ratio, _cash, _i) in cash_split.items():
        S[j, k], D[j, k] = ratio, 0.0
    prev = np.c_[np.full((4, 1), np.nan), C[:, :-1]]
    r = total_return(C, S, D, prev)
    r[Y, junction] = np.nan
    r[ST] = C[ST] / prev[ST] - 1.0  # stored: adjusted close, a vote only
    r[:, seg_break] = np.nan  # no source's return across a relist junction
    stored_r = r[ST].copy()  # its own return, also where it is no vote
    valid = np.isfinite(r)
    early = grid < pd.Timestamp(WIKI_DIV_GAP_FROM)
    rank = np.full((4, n), np.inf)
    for i, name in enumerate(SRC[:3]):
        rank[i] = np.where(early, PRECEDENCE_EARLY.index(name), PRECEDENCE_LATE.index(name))
    cols = np.arange(n)

    # the stored vote: dropped on the known breaks, on ex-dates from 2023 (price-only), on a raw jump
    # of a vendor's split, and on a stored-only unit change; kept wherever it agrees with a vendor
    break_day = np.isin(grid.strftime("%Y-%m-%d"), BREAK_DAYS)
    vote = stored_vote(r, valid, S, has, rank, junction, C, break_day)
    vendor_event = (np.abs(S[:3] - 1.0) > 1e-9).any(axis=0) | (D[:3] > 0).any(axis=0)
    stored_excluded = valid[ST] & (break_day | vote["raw_jump"] | vote["unit"] |
                                   (vendor_event & (grid >= pd.Timestamp(STORED_EX_FROM))))
    valid[ST] &= ~stored_excluded
    vendor_jump = vote["vendor_jump"] & valid[ST]

    choice = select_sources(r, valid, has, rank)
    primary = choice["primary"]
    p = np.clip(primary, 0, 3)
    stored_kind = stored_disagreements(C, r, valid, choice, p)
    choice["unresolved"] = choice["unresolved"] & (stored_kind == "")
    Cp, Vp, Sp, Dp = C[p, cols], V[p, cols], S[p, cols], D[p, cols]
    with np.errstate(divide="ignore", invalid="ignore"):
        implied = (1.0 + r[p, cols]) / (1.0 + stored_r)  # the factor the stored file's move is off by
    # stored unit changes for split_events.csv: the vote's rule, and the known break days
    stored_unit = vote["unit"] | (break_day & has[ST] & np.array([near_split_factor(v) is not None for v in implied]))

    # R5: cut filler after the last session with volume > 0 (a missing volume is not filler), then
    # trailing repeats of the last real close with volume below 1% of the 50-row median before them
    # (Tiingo's SPLK 2024-03-18..22: volumes 0, 90, 47, ...)
    # (each segment's own end too: the old shares' last trade before a relist junction)
    keep = has_vendor & (primary >= 0)
    filler_cut, filler_tiny = 0, np.zeros(0, dtype=int)
    for s in np.unique(seg[keep]):
        part = keep & (seg == s)
        traded = np.flatnonzero(part & ~(Vp == 0))
        if len(traded):
            tail = part.copy()
            tail[: traded[-1] + 1] = False
            filler_cut += int(tail.sum())
            keep &= ~tail
        tiny = tiny_volume_tail(Cp, Vp, keep & (seg == s))
        keep[tiny] = False
        filler_tiny = np.r_[filler_tiny, tiny]
    filler_cut += len(filler_tiny)
    idx = np.flatnonzero(keep)
    prev_idx = np.r_[-1, idx[:-1]]
    gap = np.where(prev_idx >= 0, idx - prev_idx - 1, 0)
    # the first kept row of the new shares after kept rows of the old ones: no return across it
    relist_first = np.zeros(n, dtype=bool)
    relist_first[idx[(prev_idx >= 0) & (seg[idx] != seg[np.maximum(prev_idx, 0)])]] = True
    tr = np.full(n, np.nan)
    own = valid[p, cols]
    tr[own] = r[p, cols][own]
    cross = np.zeros(n, dtype=bool)
    for k, j in zip(idx, prev_idx):
        if own[k] or j < 0 or (p[k] == Y and junction[k]) or seg[k] != seg[j]:
            continue
        if has[p[k], j]:  # the day's source has the previous canonical session itself (a gap before)
            tr[k] = total_return(Cp[k], Sp[k], Dp[k], C[p[k], j])
        else:
            tr[k] = total_return(Cp[k], Sp[k], Dp[k], Cp[j])
            cross[k] = True
    n_sources = choice["n_valid"]
    with np.errstate(invalid="ignore"):
        diffs = np.where(valid, np.abs(r - tr[None, :]), np.nan)
    max_diff = np.where(np.isfinite(diffs).any(axis=0), np.nanmax(np.where(np.isfinite(diffs), diffs, -1), axis=0), np.nan)
    max_diff[max_diff < 0] = np.nan
    agreeing = valid & (np.nan_to_num(diffs, nan=np.inf) <= TOL_R)

    # level and volume checks between vendors (R7)
    with np.errstate(divide="ignore", invalid="ignore"):
        level = np.abs(C[:3] / Cp[None, :] - 1.0)
        vol_ratio = V[:3] / Vp[None, :]
    level_off = has[:3] & (np.nan_to_num(level, nan=0.0) > TOL_LEVEL)
    volume_off = has[:3] & (Vp[None, :] > 0) & (V[:3] > 0) & ((vol_ratio >= VOLUME_DIFF) | (vol_ratio <= 1 / VOLUME_DIFF))

    # R1 / R2
    move = 1.0 + tr
    move_2x = np.isfinite(move) & ((move >= MOVE_2X) | (move <= 1 / MOVE_2X))
    move_big = np.isfinite(tr) & (np.abs(tr) >= MOVE_BIG)
    no_split_any = ~(has[:3] & (np.abs(S[:3] - 1.0) > 1e-9)).any(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        raw_ratio = Cp / np.where(own, prev[p, cols], np.r_[np.nan, Cp[:-1]])
    stored_moves_too = valid[ST] & (np.abs(r[ST] - tr) <= TOL_R)  # an adjusted file that jumps as well: a market move
    hidden = np.array([bool(no_split_any[k] and move_2x[k] and not stored_moves_too[k] and
                            strict_split_factor(1.0 / raw_ratio[k]) is not None)
                       if np.isfinite(raw_ratio[k]) else False for k in range(n)])
    # also: the only vendor jumps 2x or more by an n:1, 1:n, 3:2 or 2:3 ratio (2.5%: its closes may be
    # rounded to the cent) against a stored file that moves normally (CMCT 2025-01-06: Yahoo 0.17 ->
    # 1.68, a 1:10 it lacks, while the stored file goes 4352.5 -> 4200)
    for k in np.flatnonzero(vendor_jump & no_split_any & move_2x):
        hidden[k] = strict_split_factor(implied[k], 0.025) is not None
    # R9 screen of a relisting after a Form 25 that RELIST_JUNCTIONS does not cover (``ctx["relists"]``):
    # a raw level change of 10x or more (or 1/10) with no split, from 20 days before the Form 25 to 60
    # days after the later listing starts, may be new shares (a junction to review), or a market move
    relist_jump = np.zeros(n, dtype=bool)
    for cut, start, _end in ctx.get("relists", ()):
        if not cut or not start:
            continue
        lo_day = pd.Timestamp(cut) - pd.Timedelta(days=RELIST_SCREEN_BEFORE_DAYS)
        hi_day = pd.Timestamp(start) + pd.Timedelta(days=RELIST_SCREEN_AFTER_DAYS)
        for k, j in zip(idx, prev_idx):
            if j < 0 or seg[k] != seg[j] or not lo_day <= grid[k] <= hi_day:
                continue
            with np.errstate(divide="ignore", invalid="ignore"):
                level_ratio = Cp[k] * Sp[k] / Cp[j]
            if np.isfinite(level_ratio) and level_ratio > 0 and (level_ratio >= RELIST_JUMP or level_ratio <= 1 / RELIST_JUMP):
                relist_jump[k] = True
    # the known break days: the stored file, left out of the vote there, moving with the vendors anyway
    # (EYEN/HYPD, NKTR and UPXI 2025-06-24: real moves of +65%, +156% and -60% that step 6's stored-only
    # split-ratio test lists as breaks) or not (a unit break, HON)
    with np.errstate(invalid="ignore"):
        break_agree = break_day & has[ST] & np.isfinite(stored_r) & (np.abs(stored_r - tr) <= TOL_R)
    # R4
    flat = np.zeros(n, dtype=bool)
    if len(idx) >= FLAT_RUN:
        same = np.r_[False, (Cp[idx][1:] == Cp[idx][:-1]) & (seg[idx][1:] == seg[idx][:-1])]
        for s0, s1 in run_lengths(same):
            if s1 - s0 + 2 >= FLAT_RUN:
                flat[idx[s0 - 1: s1 + 1]] = True
    zero_vol = keep & (np.nan_to_num(Vp, nan=-1.0) == 0) & listed

    # splices (R8): a change of primary that lasts SPLICE_STAY rows
    splice_tokens = {}
    seg_first = {s: int(np.flatnonzero(seg == s)[0]) for s in np.unique(seg)}
    for k, old, new in splice_points(primary[idx], idx):
        if relist_first[k]:
            continue  # the new shares' own source from the junction on: nothing is chained, so no splice
        window = slice(max(seg_first[seg[k]], k - 60), k)
        both = valid[old, window] & valid[new, window]
        n_overlap = int(both.sum())
        r_ok = (np.abs(r[old, window] - r[new, window])[both] <= 1e-4).mean() if n_overlap else 0.0
        level_ok = (np.abs(C[old, window] / C[new, window] - 1.0)[both] <= 0.005).mean() if n_overlap else 0.0
        weak = n_overlap < SPLICE_MIN or r_ok < 0.95 or level_ok < 0.95
        splice_tokens[k] = [f"splice:{SRC[old]}>{SRC[new]}"] + (["splice_weak"] if weak else [])

    # flags per kept row
    tokens = defaultdict(list)
    odd_sources = (has[:3] & (np.abs(S[:3] - 1.0) > 1e-9))
    for k in idx:
        t = tokens[k]
        if not listed[k]:
            t.append("outside_listing")
        if gap[np.searchsorted(idx, k)] > 0:
            t.append(f"gap_before:{int(gap[np.searchsorted(idx, k)])}")
        if choice["override"][k]:
            t.append(f"majority_override:{SRC[choice['first_valid'][k]]}")
        minority = [SRC[i] for i in range(4) if choice["minority"][i, k]]
        if minority:
            t.append("disagree_resolved:" + "+".join(minority))
        if choice["unresolved"][k]:
            t.append("disagree_unresolved")
        if stored_kind[k]:
            t.append(stored_kind[k])
        offs = [SRC[i] for i in range(3) if level_off[i, k]]
        if offs:
            t.append("level_diff:" + "+".join(offs))
        if volume_off[:, k].any():
            t.append("volume_diff")
        if move_2x[k]:
            t.append("move_2x")
        elif move_big[k]:
            t.append("move_40")
        if hidden[k]:
            t.append("hidden_split")
        if flat[k]:
            t.append("flat_run")
        if zero_vol[k]:
            t.append("zero_volume")
        if cross[k]:
            t.append("cross_source_return")
        if abs(Sp[k] - 1.0) > 1e-9:
            t.append(("split" if Sp[k] > 1 else "reverse_split") if ordinary_ratio(Sp[k]) else "distribution_factor")
        prior = Cp[idx[np.searchsorted(idx, k) - 1]] if np.searchsorted(idx, k) > 0 else np.nan
        if Dp[k] > 0 and np.isfinite(prior) and Dp[k] > SPECIAL_PCT * prior:
            t.append("special_div")
        if stored_excluded[k]:
            t.append("stored_excluded")
        if vendor_jump[k]:
            t.append("vendor_split_jump")  # R2/R3: the only vendor's move is split-sized, the stored file's is not
        cash_names = [SRC[j] for j in range(3) if (j, k) in cash_split]
        if cash_names:
            t.append("cash_as_split:" + "+".join(cash_names))
        if p[k] == Y:
            t.extend(name for name in YAHOO_VOLUME_TOKENS.values() if name in RF[Y, k].split())
        if junction[k] and has[Y, k]:
            t.append("yahoo_junction")
        if p[k] == W and not early[k] and not has[1:3, k].any():
            t.append("wiki_div_gap")
        if "tiingo_adj_identity" in RF[T_, k] and has[T_, k]:
            t.append("tiingo_adj_identity")
        if p[k] == T_ and "tiingo_done_review" in RF[T_, k]:
            t.append("tiingo_review")  # the fetcher kept this answer with a flag to review (fetch_status.csv)
        if k in splice_tokens:
            t.extend(splice_tokens[k])
        if relist_first[k]:
            t.append("relist_junction")  # the new shares' first row: tr is blank, nothing is chained across
        if relist_jump[k]:
            t.append("relist_jump")  # R9: a 10x level change around an unreviewed relisting

    canonical = pd.DataFrame({
        "date": grid[idx].strftime("%Y-%m-%d"), "close_raw": Cp[idx], "volume_raw": Vp[idx],
        "split_factor": Sp[idx], "div_cash": Dp[idx], "tr": tr[idx], "src_primary": [SRC[i] for i in p[idx]],
        "n_sources": n_sources[idx], "max_src_diff": max_diff[idx], "flags": [";".join(tokens[k]) for k in idx]})
    result["canonical"] = canonical

    ctx_local = {"grid": grid, "idx": idx, "C": C, "S": S, "D": D, "has": has, "r": r, "valid": valid, "tr": tr,
                 "Cp": Cp, "Sp": Sp, "Dp": Dp, "p": p, "implied": implied, "junction": junction, "listed": listed,
                 "agreeing": agreeing, "RF": RF, "keep": keep, "stored_unit": stored_unit, "stored_r": stored_r,
                 "stored_excluded": stored_excluded, "cash_split": cash_split, "seg": seg,
                 "relist_first": relist_first, "relist_jump": relist_jump, "break_agree": break_agree,
                 "relists": ctx.get("relists", ())}
    result["events"] = split_events_of(sid, ctx_local, ticker_of)
    result["specials"] = specials_of(sid, ctx_local, ticker_of)
    # R1c: an ex-date whose ratio no second source confirms, with |tr| >= 10% resting on it (LGND 2022-11-02)
    day_of = {d: k for k, d in enumerate(grid.strftime("%Y-%m-%d"))}
    single_ratio = {day_of[e["ex_date"]]: e for e in result["events"]
                    if e["event_type"] != "unit_break" and e["in_canonical"]
                    and len([s for s in e["sources_confirming"].split("+") if s]) < 2}
    result["moves"] = moves_of(sid, ctx_local, ticker_of, choice=choice, move_2x=move_2x, move_big=move_big,
                               hidden=hidden, flat=flat, zero_vol=zero_vol, level_off=level_off, gap=gap,
                               stored_kind=stored_kind, vendor_jump=vendor_jump, single_ratio=single_ratio)
    result["pairs"] = pair_stats(sid, C, r, valid, has)
    counts = Counter(token.split(":")[0] for k in idx for token in tokens[k])
    # plan 6's agreement without the stored vote: kept days with two or more vendor returns
    n_vendor = valid[:3].sum(axis=0)
    with np.errstate(invalid="ignore"):
        vendor_gap = np.nan_to_num(np.where(valid[:3], np.abs(r[:3] - tr[None, :]), 0.0), nan=np.inf).max(axis=0)
    multi_vendor = keep & (n_vendor >= 2)
    result["summary"].update({
        "rows": int(len(idx)), "first_date": canonical["date"].iloc[0], "last_date": canonical["date"].iloc[-1],
        "filler_cut": filler_cut, "filler_cut_tiny_volume": int(len(filler_tiny)),
        "rows_by_primary": dict(Counter(canonical["src_primary"])),
        "rows_multi_source": int((canonical["n_sources"] >= 2).sum()),
        "rows_multi_vendor": int(multi_vendor.sum()),
        "rows_multi_vendor_agree": int((multi_vendor & (vendor_gap <= TOL_R)).sum()),
        "flag_counts": dict(counts), "last_volume_positive": bool(Vp[idx[-1]] > 0) if len(idx) else False,
        "tiingo_identity_rows": int(sum("tiingo_adj_identity" in f for f in RF[T_][has[T_]])),
        "listed_sessions_without_row": int((listed & ~keep & (cols >= idx[0]) & (cols <= idx[-1])).sum()),
        "ticker_last": ticker_of(grid[idx[-1]]), "last_src": SRC[p[idx[-1]]],
    })
    # each segment's kept rows (old shares, then the new shares from each relist junction)
    segments = []
    for s in np.unique(seg[idx]):
        rows = idx[seg[idx] == s]
        segments.append({"first": str(grid[rows[0]].date()), "last": str(grid[rows[-1]].date()), "rows": int(len(rows)),
                         "first_src": SRC[p[rows[0]]], "last_src": SRC[p[rows[-1]]], "ticker": ticker_of(grid[rows[-1]]),
                         "last_close": float(Cp[rows[-1]]), "first_close": float(Cp[rows[0]])})
    if len(segments) > 1 or ctx.get("junctions"):
        result["summary"]["segments"] = segments
        result["summary"]["junctions"] = [str(pd.Timestamp(d).date()) for d in sorted(ctx.get("junctions", ()))]
        result["summary"]["junction_dropped_events"] = junction_dropped
    if relist_jump.any():
        result["summary"]["relist_jumps"] = [str(grid[k].date()) for k in np.flatnonzero(relist_jump)]
    # the known break days where the stored file moves 1.4x or more either way (step 6's list of break
    # files) or changes units: ``unit_break`` (a row in split_events.csv), ``stored_moves_with_vendors``
    # (a real move: no unit break), ``unit_change_on_vendor_event`` (the stored file's change is on a
    # vendor's own split or distribution day, recorded with that event), ``stored_differs``
    unit_days = {e["ex_date"] for e in result["events"] if e["event_type"] == "unit_break"}
    breaks = []
    for k in np.flatnonzero(break_day & has[ST] & keep):
        stored_sized = np.isfinite(stored_r[k]) and stored_r[k] > -1 and \
            abs(np.log1p(stored_r[k])) >= np.log(SPLIT_LIKE)
        if not (stored_unit[k] or stored_sized):
            continue
        day = str(grid[k].date())
        state = ("unit_break" if day in unit_days else "stored_moves_with_vendors" if break_agree[k] else
                 "unit_change_on_vendor_event" if stored_unit[k] else "stored_differs")
        breaks.append({"date": day, "state": state, "stored_r": round(float(stored_r[k]), 6),
                       "tr": round(float(tr[k]), 6) if np.isfinite(tr[k]) else None,
                       "stored_implied_k": round(float(implied[k]), 4) if np.isfinite(implied[k]) else None})
    if breaks:
        result["summary"]["break_days"] = breaks
    return result


def split_events_of(sid: str, x: dict, ticker_of) -> list[dict]:
    """Vendor S != 1 events (Yahoo junction rows excluded) matched across sources, plus the stored
    file's unit breaks (plan 4.3)."""
    grid, C, S, D, has, r, valid = x["grid"], x["C"], x["S"], x["D"], x["has"], x["r"], x["valid"]
    n = len(grid)
    found = []
    for i in range(3):
        mask = has[i] & (np.abs(S[i] - 1.0) > 1e-9)
        if i == Y:
            mask &= ~x["junction"]
        found += [(k, i) for k in np.flatnonzero(mask)]
    implied = x["implied"]
    cash_split = x["cash_split"]
    stored_units = [int(k) for k in np.flatnonzero(x["stored_unit"])]
    events = []
    clusters = []
    for k, i in sorted(found):
        if clusters and k - clusters[-1][-1][0] <= 1:
            clusters[-1].append((k, i))
        else:
            clusters.append([(k, i)])
    used_units = set()
    for cluster in clusters:
        days = sorted({k for k, _ in cluster})
        canon_days = [k for k in days if abs(x["Sp"][k] - 1.0) > 1e-9 and x["keep"][k]]
        day = canon_days[0] if canon_days else Counter(k for k, _ in cluster).most_common(1)[0][0]
        factor = x["Sp"][day] if canon_days else float(np.median([S[i, k] for k, i in cluster]))
        lo, hi = max(0, day - 1), min(n - 1, day + 1)
        values, notes, confirm, contra = {}, [], [], []
        cash_instead = False
        for i, name in enumerate(SRC[:3]):
            as_cash = [(k, cash_split[(i, k)]) for k, j in cluster if j == i and (i, k) in cash_split]
            mine = [S[i, k] for k, j in cluster if j == i and (i, k) not in cash_split]
            covers = has[i, lo: hi + 1].any() and has[i, max(0, lo - 1): hi + 1].sum() >= 2
            if as_cash:
                # its own record is cash worth the new shares at this ratio: the same event, read as the split
                k, (ratio, cash, _src) = as_cash[0]
                values[name] = 1.0
                (confirm if ratio_match(ratio, factor) else contra).append(f"{name}(cash)")
                notes.append(f"{name} serves it as cash {cash:.6g} ((S - 1) x close): read as the split")
            elif mine:
                values[name] = mine[0]
                (confirm if ratio_match(mine[0], factor) else contra).append(name)
            elif covers:
                values[name] = 1.0
                contra.append(name)
                big_cash = [k for k in range(lo, hi + 1) if has[i, k] and D[i, k] > 0 and np.isfinite(C[i, k - 1] if k else np.nan)
                            and D[i, k] > SPECIAL_PCT * C[i, k - 1]]
                if big_cash:
                    k = big_cash[0]
                    cash_instead = True
                    notes.append(f"{name} shows cash {D[i, k] / C[i, k - 1]:.1%} of prior close instead")
            else:
                values[name] = ""
        # the stored file confirms the ratio only when it is adjusted for it (it moves with the total
        # return, k about 1); a raw jump shows only the date and the raw prices, since then k equals
        # the vendor's own S whatever S is
        stored_k, stored_state = "", "none"
        unit_days = [k for k in range(lo, hi + 1) if k in stored_units]
        used_units.update(unit_days)
        if has[ST, day] and np.isfinite(implied[day]):
            stored_k = round(float(implied[day]), 4)
            adjusted = ratio_match(stored_k, 1.0, TOL_STORED_RATIO)
            raw = ratio_match(stored_k, factor, TOL_STORED_RATIO)
            if adjusted and not raw:
                stored_state = "adjusted"
                confirm.append("stored")
                notes.append("stored adjusted for it")
            elif raw and not adjusted:
                stored_state = "raw"
                notes.append("stored raw jump: date and raw prices only")
            elif raw and adjusted:
                stored_state = "ambiguous"
                notes.append("stored: the ratio is too close to 1 to tell adjusted from raw")
            else:
                stored_state = "other"
                notes.append("stored implies another factor")
        ticker = ticker_of(grid[day])
        yahoo_flags = " ".join(sorted({f for k, j in cluster if j == Y for f in x["RF"][Y, k].split()
                                       if f in ("yahoo_odd_ratio", "yahoo_not_applied")}))
        if yahoo_flags:
            notes.append(yahoo_flags)
        if (ticker, str(grid[day].date())) in KNOWN_SPINOFFS:
            kind = "spinoff"
        elif not ordinary_ratio(factor) or "yahoo_odd_ratio" in yahoo_flags or cash_instead:
            kind = "distribution"
        else:
            kind = "split" if factor > 1 else "reverse_split"
        both = valid[:, day]
        tr_agree = ""
        if both.sum() >= 2:
            values_r = r[both, day]
            tr_agree = "Y" if values_r.max() - values_r.min() <= TOL_R else "N"
        if len(days) > 1:
            notes.append("dates " + " ".join(str(grid[k].date()) for k in days))
        events.append({"security_id": sid, "ticker": ticker, "ex_date": str(grid[day].date()),
                       "split_factor": float(factor), "event_type": kind,
                       "tiingo": values["tiingo"], "yahoo": values["yahoo"], "wiki": values["wiki"], "nasdaq": "",
                       "agree": "Y" if len(confirm) >= 2 and not contra else "N", "sec_url": "", "verified_at": "",
                       "notes": "; ".join(notes), "tr_agree": tr_agree, "sources_confirming": "+".join(confirm),
                       "stored_implied_k": stored_k, "stored_state": stored_state,
                       "in_canonical": bool(canon_days), "contra": "+".join(contra), "listed": bool(x["listed"][day])})
    for k in stored_units:
        if k in used_units or not x["keep"][k]:
            continue
        lo, hi = max(0, k - 1), min(n - 1, k + 1)
        values = {}
        confirm = []
        for i, name in enumerate(SRC[:3]):
            if has[i, k] and has[i, k - 1] if k else False:
                values[name] = 1.0
                confirm.append(name)
            else:
                values[name] = ""
        events.append({"security_id": sid, "ticker": ticker_of(grid[k]), "ex_date": str(grid[k].date()),
                       "split_factor": 1.0, "event_type": "unit_break", "tiingo": values["tiingo"],
                       "yahoo": values["yahoo"], "wiki": values["wiki"], "nasdaq": "",
                       "agree": "Y" if len(confirm) >= 2 else "N", "sec_url": "", "verified_at": "",
                       "notes": "stored-only unit change; vendors show no split" +
                                ("; known 2025-06-24 break" if str(grid[k].date()) == BREAK_DAYS[0] else "") +
                                ("; known 2026-06-29 break" if str(grid[k].date()) == BREAK_DAYS[1] else "") +
                                ("" if near_split_factor(implied[k], TOL_UNIT) is not None else
                                 "; the factor is no ordinary split ratio (the stored file's later rows carry other "
                                 "adjustments too)"),
                       "tr_agree": "", "sources_confirming": "+".join(confirm),
                       "stored_implied_k": round(float(implied[k]), 4), "stored_state": "unit_change",
                       "in_canonical": False, "contra": "stored", "listed": bool(x["listed"][k])})
    return events


def specials_of(sid: str, x: dict, ticker_of) -> list[dict]:
    """Cash above 10% of the prior raw close in any vendor, and every odd ratio (plan 4.3)."""
    grid, C, S, D, has = x["grid"], x["C"], x["S"], x["D"], x["has"]
    hits = []
    for i in range(3):
        for k in np.flatnonzero(has[i] & (D[i] > 0)):
            prior = C[i, k - 1] if k and has[i, k - 1] else (x["Cp"][k - 1] if k else np.nan)
            if np.isfinite(prior) and prior > 0 and D[i, k] > SPECIAL_PCT * prior:
                hits.append((k, i, "cash", D[i, k], prior))
        odd = has[i] & (np.abs(S[i] - 1.0) > 1e-9)
        if i == Y:
            odd &= ~x["junction"]
        for k in np.flatnonzero(odd):
            if not ordinary_ratio(S[i, k]):
                prior = C[i, k - 1] if k and has[i, k - 1] else np.nan
                hits.append((k, i, "ratio", S[i, k], prior))
    hits.sort()
    clusters = []
    for hit in hits:
        if clusters and hit[0] - clusters[-1][-1][0] <= 1:
            clusters[-1].append(hit)
        else:
            clusters.append([hit])
    out = []
    for cluster in clusters:
        day = cluster[0][0]
        cash = [h for h in cluster if h[2] == "cash"]
        ratio = [h for h in cluster if h[2] == "ratio"]
        canon_cash = x["Dp"][day] if x["keep"][day] else 0.0
        if cash:
            pick = max(cash, key=lambda h: (h[1] == x["p"][h[0]], h[3]))
            cash_value, prior = pick[3], pick[4]
        else:
            cash_value, prior = (canon_cash if canon_cash > 0 else np.nan), ratio[0][4]
        ticker = ticker_of(grid[day])
        if (ticker, str(grid[day].date())) in KNOWN_SPINOFFS:
            kind = "spinoff_known_plan_4_3"
        elif cash and ratio:
            kind = "distribution_ratio_and_cash"
        elif ratio:
            kind = "distribution_as_ratio"
        else:
            kind = "special_cash_or_spinoff"
        sources = " ".join(sorted({f"{SRC[h[1]]}:{h[2]}" for h in cluster}))
        both = x["valid"][:, day]
        spread = float(x["r"][both, day].max() - x["r"][both, day].min()) if both.sum() >= 2 else np.nan
        returns = ", ".join(f"{SRC[i]} {x['r'][i, day]:+.1%}" for i in range(4) if both[i])
        pct = cash_value / prior if np.isfinite(cash_value) and np.isfinite(prior) and prior > 0 else np.nan
        ratio_pct = bool(not np.isfinite(pct) and ratio and ratio[0][3] > 1)
        if ratio_pct:
            pct = 1.0 - 1.0 / ratio[0][3]  # the share of value a distribution served as a ratio takes away
        # a ratio below 1 (HON 2026-06-29, 0.9535) is no share of value taken away: pct stays blank
        out.append({"security_id": sid, "ex_date": str(grid[day].date()),
                    "cash": round(float(cash_value), 6) if np.isfinite(cash_value) else "",
                    "prior_close_raw": round(float(prior), 6) if np.isfinite(prior) else "",
                    "pct_of_prior": round(float(pct), 6) if np.isfinite(pct) else "",
                    "classification": kind, "sec_url": "", "ticker": ticker,
                    "ratio": round(float(ratio[0][3]), 6) if ratio else "", "sources": sources,
                    "notes": "; ".join(filter(None, [
                        "pct is 1 - 1/ratio" if ratio_pct else
                        ("ratio below 1: no pct" if ratio and not cash and ratio[0][3] < 1 else ""),
                        f"total returns differ: {returns}" if np.isfinite(spread) and spread > TOL_R else
                        ("sources agree on the total return" if np.isfinite(spread) else "one source only")])),
                    "listed": bool(x["listed"][day])})
    return out


def moves_of(sid: str, x: dict, ticker_of, *, choice, move_2x, move_big, hidden, flat, zero_vol, level_off, gap,
             stored_kind, vendor_jump=None, single_ratio=None) -> list[dict]:
    """Draft queue entries (plan 4.4): R1, R1/R2, R1b, R1c, R2/R3, R3, R4, R6, R7, and R9 (a 10x level change
    around a relisting that RELIST_JUNCTIONS does not cover). A relist junction row has no return, so no
    rule fires on a move across it."""
    grid, idx, agreeing, valid, has, C = x["grid"], x["idx"], x["agreeing"], x["valid"], x["has"], x["C"]
    n = len(grid)
    vendor_jump = np.zeros(n, dtype=bool) if vendor_jump is None else vendor_jump
    single_ratio = single_ratio or {}
    out = []

    def entry(k, rule, note, agreeing_sources=None):
        sources = agreeing_sources if agreeing_sources is not None else \
            "+".join(SRC[i] for i in range(4) if agreeing[i, k])
        out.append({"ticker": ticker_of(grid[k]), "event_date": str(grid[k].date()), "classification": "unreviewed",
                    "source_url": "", "verified_at": "", "notes": f"[{rule}] {note}", "security_id": sid,
                    "sources_agreeing": sources, "rule": rule, "listed": bool(x["listed"][k])})

    keep = x["keep"]
    tr = x["tr"]
    stored_r = x.get("stored_r", x["r"][ST])
    queued_jump = np.zeros(n, dtype=bool)
    break_agree = x.get("break_agree", np.zeros(n, dtype=bool))
    for k in idx:
        n_agree = int(agreeing[:, k].sum())
        stored_note = f"; the stored file moves {stored_r[k]:+.2%} (a split the vendor lacks, or a vendor error)" \
            if vendor_jump[k] else ""
        if break_agree[k]:
            stored_note += (f"; the stored file, left out of the vote on this known break day, moves the same "
                            f"({stored_r[k]:+.2%}): no unit break")
        ratio_note = ""
        if k in single_ratio and np.isfinite(tr[k]) and abs(tr[k]) >= RATIO_QUEUE_TR:
            event = single_ratio[k]
            ratio_note = (f"{event['event_type']} ratio {event['split_factor']:.6g} that only "
                          f"{event['sources_confirming'] or 'no source'} records (stored {event['stored_state']})")
        if move_2x[k]:
            rule = "R1/R2" if hidden[k] else "R1"
            note = f"price ratio {1 + tr[k]:.2f}x with no split in any source" if hidden[k] else \
                f"move of {1 + tr[k]:.2f}x"
            if hidden[k]:
                with np.errstate(divide="ignore", invalid="ignore"):
                    fit = near_split_factor(1 / (1 + tr[k])) or near_split_factor(1 / x["implied"][k])
                note += f" (fits {fit:.4g} split)" if fit else ""
            note += f"; {n_agree} source(s) agree within 0.5%" + stored_note
            entry(k, rule, note + (f"; it rests on a {ratio_note}" if ratio_note else ""))
            queued_jump[k] = bool(vendor_jump[k])
        elif vendor_jump[k]:
            entry(k, "R2/R3", f"the only vendor ({SRC[x['p'][k]]}) moves {tr[k]:+.2%}, split-sized{stored_note}")
            queued_jump[k] = True
        elif move_big[k] and n_agree < 2:
            entry(k, "R1b", f"move of {tr[k]:+.0%} confirmed by no second source" + stored_note +
                  (f"; it rests on a {ratio_note}" if ratio_note else ""))
        elif ratio_note:  # R1c: the R1 entries above carry the same note
            entry(k, "R1c", f"tr {tr[k]:+.2%} rests on a {ratio_note}; needs a document")
        if choice["unresolved"][k]:
            values = ", ".join(f"{SRC[i]} {x['r'][i, k]:+.2%}" for i in range(4) if valid[i, k])
            entry(k, "R3", f"sources disagree with no majority: {values}")
        elif stored_kind[k] == "stored_shift" and not queued_jump[k]:  # a vendor jump is queued above
            values = ", ".join(f"{SRC[i]} {x['r'][i, k]:+.2%}" for i in range(4) if valid[i, k])
            entry(k, "R3", f"stored file shifts against the only vendor and stays (an event the vendor may lack, "
                           f"or a stored adjustment): {values}")
    kept = np.zeros(len(grid), dtype=bool)
    kept[idx] = True
    relist_jump = x.get("relist_jump", np.zeros(n, dtype=bool))
    for k in np.flatnonzero(relist_jump & kept):
        before = idx[np.searchsorted(idx, k) - 1]
        cuts = ", ".join(f"Form 25 {cut}, listed again from {start}" for cut, start, _ in x.get("relists", ()))
        entry(k, "R9", f"raw close {x['Cp'][before]:.4g} on {grid[before].date()} -> {x['Cp'][k]:.4g} "
                       f"({x['Cp'][k] * x['Sp'][k] / x['Cp'][before]:.3g}x) with no split, around a relisting ({cuts}): "
                       "new shares out of a bankruptcy or an exchange (add a RELIST_JUNCTIONS entry: no return across "
                       "it) or a market move", "")
        out[-1]["listed"] = True  # it concerns the listing itself (the jump may fall in the OTC months)
    # R3, single vendor vs stored: a one-day (glitch) or small lasting (disagrees) gap above 2%. The
    # vendor stands, but which row is bad is not known (HSIC 2019-02-08, SWBI 2020-08-25, CGC 2022-10-07)
    with np.errstate(invalid="ignore"):
        gap_r = np.abs(x["r"][x["p"], np.arange(n)] - stored_r)
    weak = np.isin(stored_kind, ["stored_glitch", "stored_disagrees"]) & kept & ~queued_jump & \
        (np.nan_to_num(gap_r, nan=0.0) > STORED_QUEUE_DIFF)
    for s0, s1 in run_lengths(weak):
        days = np.arange(s0, s1 + 1)
        worst = days[int(np.nanargmax(gap_r[days]))]
        entry(s0, "R3", f"single vendor vs stored on {len(days)} session(s) to {grid[s1].date()} "
                        f"({', '.join(sorted(set(stored_kind[days])))}); largest on {grid[worst].date()}: "
                        f"{SRC[x['p'][worst]]} {x['r'][x['p'][worst], worst]:+.2%}, stored {stored_r[worst]:+.2%}; "
                        "the vendor stands until a document or the stored file's own OHLC says which row is bad")
    for s0, s1 in run_lengths(flat & kept):
        days = idx[(idx >= s0) & (idx <= s1)]
        confirmed = [SRC[i] for i in range(4) if i != x["p"][s0] and has[i, days].all()
                     and np.allclose(C[i, days], C[i, days[0]], rtol=0, atol=1e-9)]
        zero = bool(zero_vol[days].any())
        if confirmed and not zero:
            continue  # a second vendor shows the same flat run with volume: genuine, flagged only
        entry(s0, "R4", f"{len(days)} identical raw closes to {grid[s1].date()}" + ("; zero volume" if zero else "") +
              ("; no second source" if not confirmed else ""), "+".join(confirmed))
    for s0, s1 in run_lengths(zero_vol & ~flat):
        entry(s0, "R4", f"zero volume on {s1 - s0 + 1} session(s) to {grid[s1].date()}", "")
    if len(idx):
        inside = np.zeros(len(grid), dtype=bool)
        inside[idx[0]: idx[-1] + 1] = True
        missing = inside & ~kept & x["listed"]
        for s0, s1 in run_lengths(missing):
            entry(s0, "R6", f"{s1 - s0 + 1} listed session(s) with no vendor row, to {grid[s1].date()}", "")
    for i in range(3):
        for s0, s1 in run_lengths(level_off[i] & kept):
            days = idx[(idx >= s0) & (idx <= s1)]
            ratio = np.nanmedian(C[i, days] / x["Cp"][days])
            if len(days) < 3 and abs(ratio - 1) < 0.02:
                continue  # a day or two 1-2% apart: flagged only
            entry(s0, "R7", f"{SRC[i]} raw close {ratio:.4f}x of {SRC[x['p'][s0]]} on {len(days)} session(s) "
                            f"to {grid[s1].date()}", SRC[x["p"][s0]])
    return out


def pair_stats(sid: str, C: np.ndarray, r: np.ndarray, valid: np.ndarray, has: np.ndarray) -> list[dict]:
    out = []
    for i in range(4):
        for j in range(i + 1, 4):
            both = valid[i] & valid[j]
            level = has[i] & has[j] & (i < 3) & (j < 3)
            if not both.any() and not level.any():
                continue
            dr = np.abs(r[i] - r[j])[both]
            with np.errstate(divide="ignore", invalid="ignore"):
                ratio = (C[i] / C[j])[level]
            out.append({"security_id": sid, "a": SRC[i], "b": SRC[j], "days_r": int(both.sum()),
                        "days_r_1e4": int((dr <= 1e-4).sum()), "days_r_0p5pct": int((dr <= TOL_R).sum()),
                        "share_r_1e4": round(float((dr <= 1e-4).mean()), 4) if len(dr) else "",
                        "share_r_0p5pct": round(float((dr <= TOL_R).mean()), 4) if len(dr) else "",
                        "days_level": int(level.sum()),
                        "share_level_0p5pct": round(float((np.abs(ratio - 1) <= 0.005).mean()), 4) if len(ratio) else "",
                        "share_level_1pct": round(float((np.abs(ratio - 1) <= TOL_LEVEL).mean()), 4) if len(ratio) else "",
                        "median_level_ratio": round(float(np.median(ratio)), 6) if len(ratio) else ""})
    return out


# ------------------------------------------------------------------ driver: per-security runs with resumable state

def frames_for(sid: str, bundles: dict[str, dict[str, pd.DataFrame]]) -> dict[str, pd.DataFrame]:
    frames = {}
    if sid in bundles["wiki"]:
        frames["wiki"] = bundles["wiki"][sid]
    new, old = bundles["tiingo_new"].get(sid), bundles["tiingo_old"].get(sid)
    if new is not None and old is not None:
        frames["tiingo"] = pd.concat([new, old[~old["date"].isin(new["date"])]]).sort_values("date").reset_index(drop=True)
    elif new is not None or old is not None:
        frames["tiingo"] = new if new is not None else old
    yahoo = bundles["yahoo_new"].get(sid)
    frames_yahoo = yahoo if yahoo is not None else bundles["yahoo_old"].get(sid)
    if frames_yahoo is not None:
        frames["yahoo"] = frames_yahoo
    if sid in bundles["stored"]:
        frames["stored"] = bundles["stored"][sid]
    return frames


def tiingo_self_check(sid: str, bundles) -> dict:
    """The run's answer against an older Tiingo cache of the same security, on common days."""
    new, old = bundles["tiingo_new"].get(sid), bundles["tiingo_old"].get(sid)
    if new is None or old is None:
        return {}
    joined = new.merge(old, on="date", suffixes=("_new", "_old"))
    if joined.empty:
        return {"tiingo_run_vs_old_days": 0}
    off = (np.abs(joined["close_new"] / joined["close_old"] - 1) > 1e-6) | \
          (np.abs(joined["split_new"] - joined["split_old"]) > 1e-9) | (np.abs(joined["div_new"] - joined["div_old"]) > 1e-6)
    return {"tiingo_run_vs_old_days": int(len(joined)), "tiingo_run_vs_old_diff_days": int(off.sum())}


def signature_of(sid: str, frames: dict[str, pd.DataFrame], window, mapping: pd.DataFrame, extra: str = "") -> str:
    digest = hashlib.sha256(f"{CODE_VERSION}|{CODE_HASH}|{sid}|{window[0].date()}|{window[1].date()}|{extra}".encode())
    spans = mapping.loc[mapping["security_id"] == sid, ["ticker", "list_start", "list_end"]]
    digest.update(spans.to_csv(index=False).encode())
    for name in SRC:
        frame = frames.get(name)
        if frame is None:
            digest.update(f"{name}:none".encode())
            continue
        part = frame[["date", "close", "volume", "split", "div", "rowflag"]]
        digest.update(name.encode() + pd.util.hash_pandas_object(part, index=False).values.tobytes())
    return digest.hexdigest()


def state_path(sid: str) -> Path:
    return STATE_DIR / f"{sid}.pkl"


def load_state(sid: str) -> dict | None:
    path = state_path(sid)
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            return pickle.load(handle)
    except Exception:
        return None


def save_result(sid: str, result: dict, signature: str) -> dict:
    canonical = result.pop("canonical")
    path = PRICES_DIR / f"{sid}.csv"
    ensure_disk(path)
    if len(canonical):
        write_csv(path, canonical)
    elif path.exists():
        path.unlink()
    days = pd.to_datetime(canonical["date"]).values.astype("datetime64[D]").astype(np.int32) if len(canonical) else \
        np.zeros(0, dtype=np.int32)
    state = {**result, "signature": signature, "dates": days, "built_utc": now_utc()}
    common.atomic_write(state_path(sid), pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL))
    return state


def junctions_of(sid: str) -> list[str]:
    """The first sessions of new shares after a relisting (RELIST_JUNCTIONS)."""
    entry = RELIST_JUNCTIONS.get(sid)
    return [entry["first_new_session"]] if entry else []


def run_securities(ids: list[str], bundles, identity, windows, sessions, rebuild: bool = False) -> dict[str, dict]:
    mapping, master = identity["mapping"], identity["master"]
    relists = identity.get("relisted", {})
    first_ticker = dict(zip(master["security_id"], master["first_ticker"]))
    states, built, reused = {}, 0, 0
    started = time.time()
    for n, sid in enumerate(ids, 1):
        frames = frames_for(sid, bundles)
        dates = [f["date"] for f in frames.values() if len(f)]
        window = windows.get(sid)
        if window is None:
            if not dates:
                window = (pd.Timestamp(WINDOW_START), pd.Timestamp(WINDOW_START))
            else:
                window = (min(d.min() for d in dates), max(d.max() for d in dates))
        signature = signature_of(sid, frames, window, mapping, json.dumps([junctions_of(sid), relists.get(sid, [])]))
        state = None if rebuild else load_state(sid)
        if state is not None and state.get("signature") == signature and \
                (state["summary"].get("rows", 0) == 0 or (PRICES_DIR / f"{sid}.csv").exists()):
            states[sid] = state
            reused += 1
        else:
            lookup = ticker_lookup(mapping, sid)
            fallback = first_ticker.get(sid, "")
            ctx = {"sessions": sessions, "window": window,
                   "listed": lambda grid, sid=sid: listed_mask(mapping, sid, grid),
                   "ticker_of": (lambda day, lookup=lookup, fallback=fallback: lookup(day) or fallback),
                   "junctions": junctions_of(sid), "relists": [] if sid in RELIST_JUNCTIONS else relists.get(sid, [])}
            result = reconcile_security(sid, frames, ctx)
            result["summary"].update(tiingo_self_check(sid, bundles))
            states[sid] = save_result(sid, result, signature)
            built += 1
        if n % 200 == 0 or n == len(ids):
            log(f"securities {n}/{len(ids)}: built {built}, reused {reused} ({time.time() - started:.0f}s)")
    return states


def prepare(only: list[str] | None = None, rebuild: bool = False) -> dict:
    """Identity, targets, windows, sessions and the source bundles (cached on disk unless ``rebuild``)."""
    identity = load_identity()
    targets = load_targets()
    ids = set(targets["security_id"])
    windows = security_windows(identity["mapping"], ids)
    sessions = pf.xnas_sessions(WINDOW_START, WINDOW_END)
    log(f"targets: {len(ids)} securities ({int(targets['in_candidates'].sum())} candidates, "
        f"{int(targets['rank300'].sum())} ranked <= 300)")
    sources, facts = load_sources(identity, ids, windows, rebuild)
    bundles = {name: split_by_security(rows if only is None else rows[rows["security_id"].isin(only)])
               for name, rows in sources.items()}
    return {"identity": identity, "targets": targets, "windows": windows, "sessions": sessions,
            "bundles": bundles, "facts": facts}


# ------------------------------------------------------------------ tables and reports

SUBMISSIONS = common.RAW / "sec" / "submissions"
SEC_ITEMS_FIRST = {"2.01", "3.03", "5.03"}  # plan 4.3: completion of a disposition, rights, charter amendment
SEC_ITEMS_LATER = {"8.01"}                   # other events (special and stock dividends are often here)
_SUBMISSION_CACHE: dict[str, list[tuple]] = {}


def filings_of(cik: str) -> list[tuple]:
    """(form, filing_date, items, accession, primary document) from the cached submissions files
    (main file and older pages; no request is made)."""
    cik = str(cik or "").strip()
    if not cik.isdigit():
        return []
    if cik in _SUBMISSION_CACHE:
        return _SUBMISSION_CACHE[cik]
    rows = []
    for path in sorted(SUBMISSIONS.glob(f"CIK{int(cik):010d}*.json.gz")):
        try:
            payload = json.loads(gzip.decompress(path.read_bytes()))
        except Exception:
            continue
        block = payload.get("filings", {}).get("recent", payload) if "filings" in payload else payload
        for form, day, items, accession, document in zip(block.get("form", []), block.get("filingDate", []),
                                                         block.get("items", []), block.get("accessionNumber", []),
                                                         block.get("primaryDocument", [])):
            if form in ("8-K", "8-K/A"):
                rows.append((form, day, items or "", accession, document))
    _SUBMISSION_CACHE[cik] = rows
    return rows


def sec_candidates(cik: str, ex_date: str, before_days: int = 60, after_days: int = 10, limit: int = 3) -> str:
    """Up to ``limit`` 8-K filings near ``ex_date`` whose items fit a split or distribution, as
    'items@filing_date url' (Items 2.01/3.03/5.03 first, then 8.01, nearest first). Candidates for the
    hand review only: ``sec_url`` stays blank until someone reads the filing."""
    if not ex_date:
        return ""
    day = pd.Timestamp(ex_date)
    found = []
    for form, filed, items, accession, document in filings_of(cik):
        when = pd.Timestamp(filed)
        if not (day - pd.Timedelta(days=before_days) <= when <= day + pd.Timedelta(days=after_days)):
            continue
        codes = {c.strip() for c in str(items).split(",")}
        if codes & SEC_ITEMS_FIRST:
            rank = 0
        elif codes & SEC_ITEMS_LATER:
            rank = 1
        else:
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{document}"
        found.append((rank, abs((when - day).days), f"{items}@{filed} {url}"))
    return " | ".join(text for _, _, text in sorted(found)[:limit])


def holder_of(ticker: str, day: str, ticker_map: pf.TickerMap) -> str:
    owner, _ = ticker_map.assign(str(ticker).upper(), [np.datetime64(str(day)[:10])])
    return str(owner[0])


def build_split_table(states: dict[str, dict], identity: dict) -> tuple[pd.DataFrame, dict]:
    rows = [e for s in states.values() for e in s["events"]]
    frame = pd.DataFrame(rows)
    facts = {"events_all": int(len(frame))}
    if frame.empty:
        return pd.DataFrame(columns=SPLIT_COLUMNS), facts
    facts["events_outside_listing"] = int((~frame["listed"]).sum())
    frame = frame[frame["listed"]].copy()
    confirmed = pd.read_csv(CONFIRMED_ADJUSTMENTS, dtype=str, keep_default_na=False) if CONFIRMED_ADJUSTMENTS.exists() \
        else pd.DataFrame(columns=["ticker", "effective_date", "adjustment_factor", "action_type", "source_url", "verified_at"])
    for row in confirmed.itertuples(index=False):
        sid = holder_of(row.ticker, row.effective_date, identity["ticker_map"])
        hit = (frame["security_id"] == sid) & (frame["ex_date"] == row.effective_date)
        if hit.any():
            frame.loc[hit, "sec_url"] = row.source_url
            frame.loc[hit, "verified_at"] = row.verified_at
            frame.loc[hit, "notes"] = (frame.loc[hit, "notes"] + "; confirmed_price_adjustments.csv").str.strip("; ")
    cik_of = dict(zip(identity["master"]["security_id"], identity["master"]["cik"]))
    frame["sec_candidates"] = [sec_candidates(cik_of.get(sid, ""), day) if kind != "unit_break" else ""
                               for sid, day, kind in zip(frame["security_id"], frame["ex_date"], frame["event_type"])]
    frame = frame.sort_values(["security_id", "ex_date"], kind="stable")
    vendor = frame[frame["event_type"] != "unit_break"]
    facts.update({
        "events_listed": int(len(frame)),
        "by_type": {k: int(v) for k, v in frame["event_type"].value_counts().items()},
        "vendor_events": int(len(vendor)),
        "vendor_events_agree": int((vendor["agree"] == "Y").sum()),
        "vendor_events_agree_share": round(float((vendor["agree"] == "Y").mean()), 4) if len(vendor) else None,
        "vendor_events_ratio_or_tr_agree": int(((vendor["agree"] == "Y") | (vendor["tr_agree"] == "Y")).sum()),
        "vendor_events_single_source": int((vendor["sources_confirming"].str.count(r"\+") == 0).sum()),
        "vendor_events_with_contra": int((vendor["contra"] != "").sum()),
        "vendor_events_not_in_canonical": int((~vendor["in_canonical"]).sum()),
        "unit_breaks": int((frame["event_type"] == "unit_break").sum()),
        "unit_breaks_on_2025_06_24": int(((frame["event_type"] == "unit_break") & (frame["ex_date"] == BREAK_DAYS[0])).sum()),
        "tr_agree": {k: int(v) for k, v in vendor["tr_agree"].replace("", "n/a").value_counts().items()},
        "with_sec_candidates": int((vendor["sec_candidates"] != "").sum()),
    })
    return frame[SPLIT_COLUMNS], facts


def build_special_table(states: dict[str, dict], identity: dict) -> tuple[pd.DataFrame, dict]:
    frame = pd.DataFrame([e for s in states.values() for e in s["specials"]])
    if frame.empty:
        return pd.DataFrame(columns=SPECIAL_COLUMNS), {"rows": 0}
    facts = {"rows_all": int(len(frame)), "outside_listing": int((~frame["listed"]).sum())}
    frame = frame[frame["listed"]].sort_values(["security_id", "ex_date"], kind="stable")
    cik_of = dict(zip(identity["master"]["security_id"], identity["master"]["cik"]))
    frame["sec_candidates"] = [sec_candidates(cik_of.get(sid, ""), day) for sid, day in zip(frame["security_id"], frame["ex_date"])]
    facts.update({"rows": int(len(frame)), "with_sec_candidates": int((frame["sec_candidates"] != "").sum()),
                  "by_classification": {k: int(v) for k, v in frame["classification"].value_counts().items()}})
    return frame[SPECIAL_COLUMNS], facts


RELEVANT_BEFORE_DAYS, RELEVANT_AFTER_DAYS = 70, 35


def relevant_spans(dv_weeks: dict[str, pd.DatetimeIndex]) -> dict[str, list[tuple[str, str]]]:
    """Per security, the dates where its data can matter to the test: 10 weeks before to 5 weeks after
    every week it ranks <= 300 (dv20 or dv50, step 6) or its canonical dollar volume reaches step 6's
    rank-300 cut (``scan_series``: names step 6 could not price)."""
    weekly = pd.read_pickle(WEEKLY_METRICS)
    top = weekly[(weekly["dv50_rank"] <= 300) | (weekly["dv20_rank"] <= 300)][["security_id", "week_end"]]
    extra = pd.DataFrame([(sid, week) for sid, weeks in dv_weeks.items() for week in weeks],
                         columns=["security_id", "week_end"])
    top = pd.concat([top, extra], ignore_index=True).drop_duplicates()
    out = {}
    for sid, weeks in top.groupby("security_id")["week_end"]:
        spans = []
        for week in sorted(weeks):
            start = week - pd.Timedelta(days=RELEVANT_BEFORE_DAYS)
            end = week + pd.Timedelta(days=RELEVANT_AFTER_DAYS)
            if spans and start <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], end)
            else:
                spans.append([start, end])
        out[sid] = [(a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d")) for a, b in spans]
    return out


def build_move_queue(states: dict[str, dict], dv_weeks: dict) -> tuple[pd.DataFrame, dict]:
    frame = pd.DataFrame([e for s in states.values() for e in s["moves"]])
    if frame.empty:
        return pd.DataFrame(columns=MOVE_COLUMNS), {"rows": 0}
    spans = relevant_spans(dv_weeks)
    frame["relevant"] = [any(a <= day <= b for a, b in spans.get(sid, ())) for sid, day in
                         zip(frame["security_id"], frame["event_date"])]
    write_csv(OUT / "moves_all.csv", frame.sort_values(["security_id", "event_date", "rule"], kind="stable"))
    facts = {"entries_all": int(len(frame)), "outside_listing": int((~frame["listed"]).sum()),
             "not_relevant": int((frame["listed"] & ~frame["relevant"]).sum()),
             "by_rule_all": {k: int(v) for k, v in frame["rule"].value_counts().items()},
             "scope": f"listed days within {RELEVANT_BEFORE_DAYS} days before to {RELEVANT_AFTER_DAYS} days after a "
                      "week ranked <= 300 (dv20 or dv50, step 6) or whose canonical dollar volume reaches step 6's "
                      "rank-300 cut; every entry is in CACHE/reconcile/moves_all.csv"}
    frame = frame[frame["listed"] & frame["relevant"]].copy()
    if REVIEWED_FORMAT.exists():
        done = pd.read_csv(REVIEWED_FORMAT, dtype=str, keep_default_na=False)
        known = {(t, d): row for t, d, row in zip(done["ticker"], done["event_date"], done.itertuples(index=False))}
        for k in frame.index:
            row = known.get((frame.at[k, "ticker"], frame.at[k, "event_date"]))
            if row is not None:
                frame.loc[k, ["classification", "source_url", "verified_at"]] = \
                    [row.classification, row.source_url, row.verified_at]
                frame.at[k, "notes"] += f" | reviewed_market_moves.csv: {row.notes}"
    frame = frame.sort_values(["security_id", "event_date", "rule"], kind="stable")
    year = frame["event_date"].str[:4]
    facts.update({"entries": int(len(frame)),
                  "by_rule": {k: int(v) for k, v in frame["rule"].value_counts().items()},
                  "by_rule_year": {rule: {y: int(v) for y, v in year[frame["rule"] == rule].value_counts().sort_index().items()}
                                   for rule in sorted(frame["rule"].unique())},
                  "prefilled_from_reviewed_market_moves": int((frame["classification"] != "unreviewed").sum())})
    return frame[MOVE_COLUMNS], facts


def coverage(states: dict[str, dict], targets: pd.DataFrame, sessions: pd.DatetimeIndex) -> tuple[dict, pd.DataFrame]:
    """Per year: step-6 name-weeks ranked 1-300 (and 1-250) that have a canonical row. ``covered`` uses
    step 6's own staleness rule (a row within the 5 sessions up to the week end, so the week after a
    last trade counts as step 6 counted it); ``covered_same_week`` needs a row in that calendar week."""
    weekly = pd.read_pickle(WEEKLY_METRICS)
    weekly = weekly[weekly["universe"] & ((weekly["dv50_rank"] <= 300) | (weekly["dv20_rank"] <= 300))]
    weeks = pf.week_ends(sessions)
    session_days = sessions.values.astype("datetime64[D]").astype(np.int64)
    week_pos = np.searchsorted(session_days, weeks.values.astype("datetime64[D]").astype(np.int64))
    week_days = weeks.values.astype("datetime64[D]").astype(np.int64)
    stale, same = set(), set()
    for sid, state in states.items():
        days = state["dates"].astype(np.int64)
        if not len(days):
            continue
        same.update((sid, int(w)) for w in np.unique(np.searchsorted(week_days, days, side="left")))
        pos = np.searchsorted(session_days, days)
        low = np.searchsorted(week_pos, pos, side="left")
        high = np.searchsorted(week_pos, pos + pf.CLOSE_STALE_SESSIONS, side="right")
        for a, b in zip(low, high):
            stale.update((sid, int(w)) for w in range(a, b))
    weekly = weekly.assign(week_index=np.searchsorted(week_days, weekly["week_end"].values.astype("datetime64[D]").astype(np.int64)))
    pairs = list(zip(weekly["security_id"], weekly["week_index"]))
    weekly["covered"] = [pair in stale for pair in pairs]
    weekly["covered_same_week"] = [pair in same for pair in pairs]
    weekly["year"] = weekly["week_end"].dt.year
    out = {}
    for label, mask in (("dv50_rank_1_300", weekly["dv50_rank"] <= 300), ("dv50_rank_1_250", weekly["dv50_rank"] <= 250),
                        ("dv20_rank_1_300", weekly["dv20_rank"] <= 300)):
        part = weekly[mask]
        by_year = part.groupby("year").agg(name_weeks=("covered", "size"), covered=("covered", "sum"),
                                           covered_same_week=("covered_same_week", "sum"),
                                           securities=("security_id", "nunique"))
        by_year["missing"] = by_year["name_weeks"] - by_year["covered"]
        by_year["share"] = (by_year["covered"] / by_year["name_weeks"]).round(4)
        out[label] = {int(y): {k: (float(v) if k == "share" else int(v)) for k, v in row.items()}
                      for y, row in by_year.iterrows()}
        out[label]["all"] = {"name_weeks": int(len(part)), "covered": int(part["covered"].sum()),
                             "covered_same_week": int(part["covered_same_week"].sum()),
                             "share": round(float(part["covered"].mean()), 4) if len(part) else None}
    gaps = weekly[(weekly["dv50_rank"] <= 300) & ~weekly["covered"]]
    gap_table = gaps.groupby("security_id").agg(missing_weeks=("week_end", "size"), first_missing=("week_end", "min"),
                                                last_missing=("week_end", "max"), best_dv50_missing=("dv50_rank", "min"))
    gap_table = gap_table.join(targets.set_index("security_id")[["candidate_reasons", "planned_sources", "candidate_status"]])
    gap_table["has_series"] = [states.get(s, {}).get("summary", {}).get("rows", 0) > 0 for s in gap_table.index]
    waiting = set(tiingo_waiting()["security_id"])
    gap_table["tiingo_pending"] = [s in waiting for s in gap_table.index]
    for column in ("first_missing", "last_missing"):
        gap_table[column] = gap_table[column].dt.strftime("%Y-%m-%d")
    by_year_cause = gaps.assign(pending=gaps["security_id"].isin(waiting)).groupby(["year", "pending"]).size()
    out["missing_dv50_1_300_by_year"] = {
        int(y): {"tiingo_pending": int(by_year_cause.get((y, True), 0)), "other": int(by_year_cause.get((y, False), 0))}
        for y in sorted(gaps["year"].unique())}
    return out, gap_table.reset_index().sort_values(["missing_weeks", "security_id"], ascending=[False, True])


def tiingo_waiting() -> pd.DataFrame:
    """Candidate rows planned for Tiingo that the run has not answered yet (status pending or deferred)."""
    candidates = pd.read_csv(CANDIDATES, dtype=str, keep_default_na=False)
    planned = candidates[candidates["planned_source"] == "tiingo"]
    status = pd.read_csv(TIINGO_STATUS, dtype=str, keep_default_na=False) if TIINGO_STATUS.exists() else \
        pd.DataFrame(columns=["security_id", "ticker_for_source", "status"])
    answered = set(zip(status["security_id"], status["ticker_for_source"]))
    final = status[~status["status"].isin({"deferred_quota", "error"})]
    answered = set(zip(final["security_id"], final["ticker_for_source"]))
    waiting = planned[np.array([(s, t) not in answered for s, t in zip(planned["security_id"], planned["ticker_for_source"])],
                               dtype=bool)]
    return waiting


TERMINAL_2012_2026 = INPUTS / "terminal_returns_2012_2026.csv"  # the terminal step's own table (by security_id)


def series_ends(states: dict[str, dict], targets: pd.DataFrame, identity: dict, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    """Series that end before the delist date, or before the window end without one (plan 4.5 inputs).

    A security listed again after its Form 25 (``identity["relisted"]``: SMCI, CHRD) is measured against
    the end of its later listing, not the Form 25. The old shares before each relist junction end there
    (``old_shares_at_relist_junction``): their last row, the junction (first session of the new shares)
    and the terminal step's row for the Form 25, which values them (``terminal_2012_2026``)."""
    master = identity["master"].set_index("security_id")
    relisted = identity.get("relisted", {})
    terminal_now = {}
    if TERMINAL_2012_2026.exists():
        table = pd.read_csv(TERMINAL_2012_2026, dtype=str, keep_default_na=False)
        for row in table.itertuples(index=False):
            terminal_now[row.security_id] = (f"{row.terminal_type}/{row.status} last_price_date="
                                             f"{row.last_price_date or '-'} end_date={row.end_date or '-'}")
    last_session = sessions[sessions <= pd.Timestamp(WINDOW_END)][-1]
    terminal = {}
    for path in TERMINAL_FILES:
        path = path if path.is_absolute() else Path(path)
        if not path.exists():
            continue
        table = pd.read_csv(path, dtype=str, keep_default_na=False)
        for row in table.itertuples(index=False):
            day = str(row.last_price_date)[:10]
            sid = holder_of(row.ticker, day, identity["ticker_map"])
            if sid:
                terminal.setdefault(sid, []).append(f"{path.name}:{row.ticker}:{day}")
    rows = []
    position = {d: k for k, d in enumerate(sessions.strftime("%Y-%m-%d"))}
    target_info = targets.set_index("security_id")
    waiting = set(tiingo_waiting()["security_id"])
    def common_fields(sid, info, summary, existing):
        weeks300 = target_info.at[sid, "weeks_rank300"] if sid in target_info.index else 0
        return {"name": info["name"] if info is not None else "",
                "successor_security_id": info["successor_security_id"] if info is not None else "",
                "transfer_date": info["transfer_date"] if info is not None else "",
                "existing_terminal_rows": " ".join(existing), "terminal_2012_2026": terminal_now.get(sid, ""),
                "in_candidates": bool(target_info.at[sid, "in_candidates"]) if sid in target_info.index else False,
                "weeks_rank300": int(weeks300) if pd.notna(weeks300) else 0}

    for sid, state in states.items():
        summary = state["summary"]
        if not summary.get("rows"):
            continue
        last = summary["last_date"]
        info = master.loc[sid] if sid in master.index else None
        delist = info["delist_date"] if info is not None else ""
        last_listed = info["last_listed"] if info is not None else ""
        existing = terminal.get(sid, [])
        segments, junctions = summary.get("segments", []), summary.get("junctions", [])
        for old, new in zip(segments, segments[1:]):  # the old shares end at the relist junction
            junction = RELIST_JUNCTIONS.get(sid, {})
            short = position[new["first"]] - position[old["last"]] - 1 if {old["last"], new["first"]} <= set(position) else ""
            rows.append({"security_id": sid, "ticker_last": old.get("ticker", ""),
                         "category": "old_shares_at_relist_junction", "last_date": old["last"], "delist_date": delist,
                         "last_listed": last_listed, "sessions_after_last_row": short,
                         "likely_cause": f"relist_junction:{junction.get('kind', '')}", "tiingo_pending": sid in waiting,
                         "filler_cut": "", "last_src": old["last_src"],
                         "existing_last_price_date_match": ("Y" if any(e.endswith(":" + old["last"]) for e in existing)
                                                            else "N") if existing else "",
                         "junction_date": new["first"], "junction_first_new_session": ",".join(junctions),
                         "junction_url": junction.get("url", ""), **common_fields(sid, info, summary, existing)})
        if sid in relisted:  # listed again after the Form 25: the later listing's end is the target
            listing_end = min(max(end for _, _, end in relisted[sid]), WINDOW_END)
            target = sessions[sessions <= pd.Timestamp(listing_end)][-1]
            if pd.Timestamp(last) >= target - pd.Timedelta(days=7):
                continue
            category = "relisted_ends_before_listing_end"
            target_day = str(target.date())
        elif delist:
            category = "ends_before_delist" if last < delist else "reaches_delist"
            target_day = delist
        elif pd.Timestamp(last) < last_session - pd.Timedelta(days=7):
            category = "no_delist_ends_early"
            target_day = str(last_session.date())
        else:
            continue
        before = [d for d in position if d <= target_day]
        short = position[before[-1]] - position.get(last, position[before[-1]]) if before and last in position else ""
        pending = sid in waiting
        if last == WIKI_END and target_day > WIKI_END:
            cause = "wiki_end_no_later_source" + ("_tiingo_pending" if pending else "")
        elif pending:
            cause = "tiingo_pending"
        elif info is not None and info["transfer_date"] and info["transfer_date"] <= target_day:
            cause = "exchange_move"
        elif info is not None and info["successor_security_id"]:
            cause = "successor_link"  # a reorganisation or holding-company swap (step 4's successor)
        elif short != "" and short <= 10:
            cause = "last_trade_near_delist"
        else:
            cause = "ends_early"  # a halt, bankruptcy or merger whose last trade came well before the delisting
        rows.append({"security_id": sid, "ticker_last": summary.get("ticker_last", ""), "category": category,
                     "last_date": last, "delist_date": delist, "last_listed": last_listed,
                     "sessions_after_last_row": short, "likely_cause": cause, "tiingo_pending": pending,
                     "filler_cut": summary.get("filler_cut", 0), "last_src": summary.get("last_src", ""),
                     "existing_last_price_date_match": ("Y" if any(e.endswith(":" + last) for e in existing) else "N")
                     if existing else "", "junction_date": "", "junction_first_new_session": ",".join(junctions),
                     "junction_url": "", **common_fields(sid, info, summary, existing)})
    frame = pd.DataFrame(rows, columns=SERIES_END_COLUMNS)
    return frame.sort_values(["category", "security_id"]) if len(frame) else frame


SERIES_END_COLUMNS = ["security_id", "ticker_last", "name", "category", "last_date", "delist_date", "last_listed",
                      "successor_security_id", "transfer_date", "sessions_after_last_row", "likely_cause",
                      "tiingo_pending", "filler_cut", "last_src", "existing_terminal_rows",
                      "existing_last_price_date_match", "in_candidates", "weeks_rank300", "terminal_2012_2026",
                      "junction_date", "junction_first_new_session", "junction_url"]


def dv_cutoffs() -> pd.DataFrame:
    """Per step-6 week: the dv50 and dv20 of the 300th-ranked name (the smallest value ranked <= 300)."""
    weekly = pd.read_pickle(WEEKLY_METRICS)
    cut50 = weekly[weekly["dv50_rank"] <= 300].groupby("week_end")["dv50"].min()
    cut20 = weekly[weekly["dv20_rank"] <= 300].groupby("week_end")["dv20"].min()
    return pd.DataFrame({"cut50": cut50, "cut20": cut20}).sort_index()


STORED_GAP_BINS = (-np.inf, 0.005, 0.01, 0.02, 0.05, 0.10, np.inf)
STORED_GAP_LABELS = ("<=0.5%", "0.5-1%", "1-2%", "2-5%", "5-10%", ">10%")


def scan_series(ids: list[str], cutoffs: pd.DataFrame) -> dict:
    """One pass over the canonical files: flag counts by type (and type and year), plan 6's check of
    days with two or more sources agreeing within 0.5%, and the weeks where a series' own raw dollar
    volume (median close_raw x volume_raw over 50 / 20 rows) reaches step 6's rank-300 cut (relevance of
    the review queue for names step 6 could not rank). Dollar volume only: no return is aggregated."""
    by_type, by_year = Counter(), defaultdict(Counter)
    multi = Counter()
    stored_gaps = defaultdict(Counter)
    dv_weeks = {}
    week_index = cutoffs.index.values.astype("datetime64[D]")
    for sid in ids:
        path = PRICES_DIR / f"{sid}.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path, usecols=["date", "close_raw", "volume_raw", "flags", "n_sources", "max_src_diff"],
                            dtype={"date": str, "flags": str}, keep_default_na=False, na_values=[""])
        years = frame["date"].str[:4].values
        for year, flags in zip(years, frame["flags"].fillna("")):
            if flags:
                for token in flags.split(";"):
                    name = token.split(":")[0]
                    by_type[name] += 1
                    by_year[name][year] += 1
        for kind in ("stored_glitch", "stored_disagrees"):
            hit = frame["flags"].fillna("").str.contains(rf"(?:^|;){kind}(?:;|$)", regex=True)
            if hit.any():
                labels = pd.cut(frame.loc[hit, "max_src_diff"], STORED_GAP_BINS, labels=STORED_GAP_LABELS)
                stored_gaps[kind].update(labels.astype(str).replace("nan", "n/a"))
        two = frame["n_sources"] >= 2
        multi["name_days_2plus_sources"] += int(two.sum())
        multi["name_days_2plus_agree_0p5pct"] += int((two & (frame["max_src_diff"] <= TOL_R)).sum())
        multi["name_days_total"] += int(len(frame))
        dv = frame["close_raw"] * frame["volume_raw"]
        dates = pd.to_datetime(frame["date"]).values.astype("datetime64[D]")
        # the windows restart at a relist junction: the new shares' dollar volume only
        segment = frame["flags"].fillna("").str.contains(r"(?:^|;)relist_junction(?:;|$)", regex=True).cumsum()
        dv50 = dv.groupby(segment).transform(lambda s: s.rolling(50, min_periods=25).median()).values
        dv20 = dv.groupby(segment).transform(lambda s: s.rolling(20, min_periods=10).median()).values
        position = np.searchsorted(dates, week_index, side="right") - 1  # last row on or before each week end
        ok = position >= 0
        fresh = np.zeros(len(week_index), dtype=bool)
        fresh[ok] = (week_index[ok] - dates[position[ok]]).astype(int) <= 7
        above = np.zeros(len(week_index), dtype=bool)
        above[fresh] = (np.nan_to_num(dv50[position[fresh]]) >= cutoffs["cut50"].values[fresh]) | \
                       (np.nan_to_num(dv20[position[fresh]]) >= cutoffs["cut20"].values[fresh])
        dv_weeks[sid] = cutoffs.index[above]
    multi = dict(multi)
    if multi.get("name_days_2plus_sources"):
        multi["share_agree"] = round(multi["name_days_2plus_agree_0p5pct"] / multi["name_days_2plus_sources"], 5)
    return {"by_type": dict(by_type.most_common()), "by_year": {k: dict(sorted(v.items())) for k, v in by_year.items()},
            "multi": multi, "dv_weeks": dv_weeks,
            "stored_gaps": {kind: {label: int(counts.get(label, 0)) for label in list(STORED_GAP_LABELS) + ["n/a"]
                                   if counts.get(label, 0)} for kind, counts in stored_gaps.items()}}


def known_case_checks(states, split_table: pd.DataFrame, specials: pd.DataFrame, identity: dict) -> dict:
    """Plan 4.3's known cases, confirmed_price_adjustments.csv and corporate_actions.csv, and plan 4.4's
    known disagreements, against what this build produced."""
    ticker_map = identity["ticker_map"]
    out = {"plan_4_3": [], "corporate_actions": [], "known_disagreements": []}
    for ticker, day, kind, factor in KNOWN_CASES:
        sid = holder_of(ticker, day, ticker_map)
        window = pd.date_range(pd.Timestamp(day) - pd.Timedelta(days=4), pd.Timestamp(day) + pd.Timedelta(days=4)).strftime("%Y-%m-%d")
        ev = split_table[(split_table["security_id"] == sid) & split_table["ex_date"].isin(window)]
        sp = specials[(specials["security_id"] == sid) & specials["ex_date"].isin(window)]
        rows = PRICES_DIR / f"{sid}.csv"
        last = states.get(sid, {}).get("summary", {}).get("last_date", "")
        out["plan_4_3"].append({"ticker": ticker, "date": day, "kind": kind, "expected_factor": factor, "security_id": sid,
                                "split_event": ev[["ex_date", "split_factor", "event_type", "agree"]].to_dict("records"),
                                "special_distribution": sp[["ex_date", "pct_of_prior", "classification"]].to_dict("records"),
                                "series_last_date": last,
                                "found": bool(len(ev) or len(sp)),
                                "factor_matches": bool(factor is None or any(ratio_match(v, factor, 0.002) for v in ev["split_factor"]))})
    if CORPORATE_ACTIONS.exists():
        actions = pd.read_csv(CORPORATE_ACTIONS, dtype=str, keep_default_na=False)
        for row in actions.itertuples(index=False):
            sid = holder_of(row.predecessor, row.last_price_date, ticker_map)
            succ = holder_of(row.successor, row.effective_date, ticker_map)
            summary = states.get(sid, {}).get("summary", {})
            same = bool(sid) and sid == succ
            last = summary.get("last_date", "")
            ticker_change = float(row.share_ratio or 0) == 1.0 and float(row.cash_per_share or 0) == 0.0
            if sid not in states:
                ok = "not_a_target"
            elif not last:
                ok = "no_series_yet"
            elif same:
                ok = "consistent" if last > row.effective_date else "series_ends_before_the_change"
            elif ticker_change:
                ok = "ticker_change_not_linked_in_intervals"
            elif last == row.last_price_date:
                ok = "consistent"
            else:
                ok = "series_ends_early" if last < row.last_price_date else "series_runs_past_last_price_date"
            out["corporate_actions"].append({"predecessor": row.predecessor, "successor": row.successor,
                                             "last_price_date": row.last_price_date, "security_id": sid,
                                             "successor_security_id": succ, "same_security": same,
                                             "series_last_date": last, "status": ok})
    for ticker, day in KNOWN_DISAGREEMENTS:
        sid = holder_of(ticker, day, ticker_map)
        path = PRICES_DIR / f"{sid}.csv"
        flags, sources = "", ""
        if path.exists():
            frame = pd.read_csv(path, dtype={"date": str, "flags": str}, keep_default_na=False)
            hit = frame[frame["date"] == day]
            if len(hit):
                flags, sources = hit["flags"].iloc[0], f"{hit['src_primary'].iloc[0]} n={hit['n_sources'].iloc[0]}"
        out["known_disagreements"].append({"ticker": ticker, "date": day, "security_id": sid, "flags": flags,
                                           "primary": sources})
    return out


def write_panel(ids: list[str], states: dict[str, dict]) -> dict:
    """``CACHE/prices/daily_panel.csv.gz``: the long form of every canonical file (skipped when unchanged)."""
    signature = hashlib.sha256("|".join(f"{s}:{states[s]['signature']}" for s in ids).encode()).hexdigest()
    marker = PRICES_DIR / "daily_panel.signature"
    path = PRICES_DIR / "daily_panel.csv.gz"
    if path.exists() and marker.exists() and marker.read_text().strip() == signature:
        return {"panel": "unchanged"}
    parts = []
    for sid in ids:
        file = PRICES_DIR / f"{sid}.csv"
        if file.exists():
            part = pd.read_csv(file, dtype={"date": str, "flags": str, "src_primary": str}, keep_default_na=False,
                               na_values=[""])
            part.insert(0, "security_id", sid)
            parts.append(part)
    panel = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["security_id"] + PRICE_COLUMNS)
    ensure_disk(path, MIN_FREE_MB + 300)  # the gzip panel is a few hundred MB at most
    write_csv(path, panel, compress=True)
    common.atomic_write(marker, signature.encode())
    return {"panel_rows": int(len(panel)), "panel_securities": int(panel["security_id"].nunique()) if len(panel) else 0}


def no_series_table(states: dict[str, dict], targets: pd.DataFrame) -> pd.DataFrame:
    waiting = tiingo_waiting()
    waiting_ids = set(waiting["security_id"])
    unfillable = pd.read_csv(UNFILLABLE, dtype=str, keep_default_na=False) if UNFILLABLE.exists() else pd.DataFrame()
    unfillable_ids = set(unfillable.get("security_id", []))
    rows = []
    for row in targets.itertuples(index=False):
        if states.get(row.security_id, {}).get("summary", {}).get("rows", 0):
            continue
        reason = "tiingo_pending" if row.security_id in waiting_ids else \
            "unfillable" if row.security_id in unfillable_ids else "no_vendor_source"
        rows.append({"security_id": row.security_id, "reason": reason, "candidate_reasons": row.candidate_reasons,
                     "planned_sources": row.planned_sources, "candidate_status": row.candidate_status,
                     "weeks_rank300": int(row.weeks_rank300) if pd.notna(row.weeks_rank300) else 0,
                     "best_dv50": row.best_dv50})
    return pd.DataFrame(rows)


def relist_table(states: dict[str, dict], identity: dict) -> tuple[pd.DataFrame, dict]:
    """``CACHE/reconcile/relist_junctions.csv``: every target listed again after a Form 25 (``after_cut``
    spans) and every RELIST_JUNCTIONS entry: the later listing, the rows the canonical series has before
    and inside it, and the junction (old shares' last row, new shares' first row, their level ratio) or
    the R9 screen's hits. ``status``: ``junction`` (new shares: separate series), ``same_shares`` (the
    series runs through: SMCI), ``screen_hit`` (a 10x level change to review), ``no_rows_in_later_listing``
    (LLEX, PBIO: snapshot rows with no price), ``no_series``."""
    relisted = identity.get("relisted", {})
    ids = sorted((set(relisted) | set(RELIST_JUNCTIONS)) & set(states))
    rows = []
    for sid in ids:
        state = states[sid]
        summary = state["summary"]
        days = state.get("dates", np.zeros(0, dtype=np.int32)).astype("datetime64[D]")
        cut, start, end = (relisted.get(sid) or [("", "", "")])[0]
        entry = RELIST_JUNCTIONS.get(sid, {})
        segments = summary.get("segments", [])
        inside = int(((days >= np.datetime64(start)) & (days <= np.datetime64(end))).sum()) if start else 0
        before = int((days < np.datetime64(start)).sum()) if start else 0
        old, new = (segments[-2], segments[-1]) if len(segments) > 1 else ({}, {})
        if not summary.get("rows"):
            status = "no_series"
        elif len(segments) > 1:
            status = "junction"
        elif summary.get("relist_jumps"):
            status = "screen_hit"
        elif start and not inside:
            status = "no_rows_in_later_listing"
        elif entry:
            status = "junction_no_old_rows"  # the reviewed junction, but the series holds only one side of it yet
        else:
            status = "same_shares"
        rows.append({"security_id": sid, "ticker_last": summary.get("ticker_last", ""), "form25_delist_date": cut,
                     "later_listing_start": start, "later_listing_end": end, "status": status,
                     "series_first": summary.get("first_date", ""), "series_last": summary.get("last_date", ""),
                     "rows_before_later_listing": before, "rows_in_later_listing": inside,
                     "first_new_session": entry.get("first_new_session", ""), "kind": entry.get("kind", ""),
                     "document_read": ("Y" if entry.get("read") else "N") if entry else "",
                     "url": entry.get("url", ""), "old_last_date": old.get("last", ""), "old_last_src": old.get("last_src", ""),
                     "new_first_date": new.get("first", ""), "new_first_src": new.get("first_src", ""),
                     "level_ratio_new_first_to_old_last": round(new["first_close"] / old["last_close"], 4)
                     if old and old.get("last_close") else "",
                     "junction_dropped_events": " | ".join(summary.get("junction_dropped_events", [])),
                     "relist_jumps": " ".join(summary.get("relist_jumps", [])), "note": entry.get("note", "")})
    frame = pd.DataFrame(rows)
    facts = {"securities": int(len(frame)),
             "by_status": {k: int(v) for k, v in frame["status"].value_counts().items()} if len(frame) else {},
             "junctions": {r.security_id: f"{r.old_last_date} -> {r.new_first_date}" for r in frame.itertuples()
                           if r.status == "junction"} if len(frame) else {},
             "rule": "RELIST_JUNCTIONS entries split the series at the new shares' first session: no return across it, "
                     "the old shares end there (series_ends.csv old_shares_at_relist_junction); other relistings keep "
                     f"one series, and a raw level change of {RELIST_JUMP:g}x around them is queued (R9)"}
    return frame, facts


def break_day_table(states: dict[str, dict]) -> dict:
    """The known break days: where the stored file moves by a split-sized factor or is a unit break, what
    the vendors show (``unit_break``: the stored file alone changes units; ``stored_moves_with_vendors``: a
    real move both show, so no unit break, EYEN/HYPD, NKTR, UPXI 2025-06-24)."""
    out = defaultdict(dict)
    for sid, state in sorted(states.items()):
        for item in state["summary"].get("break_days", []):
            out[item["date"]][sid] = {k: v for k, v in item.items() if k != "date"}
    return {day: {"by_state": dict(Counter(v["state"] for v in items.values())), "securities": items}
            for day, items in sorted(out.items())}


def summarize_tables(states, prep, ids, args) -> dict:
    identity, targets, sessions = prep["identity"], prep["targets"], prep["sessions"]
    split_table, split_facts = build_split_table(states, identity)
    special_table, special_facts = build_special_table(states, identity)
    scan = scan_series(ids, dv_cutoffs())
    queue, queue_facts = build_move_queue(states, scan["dv_weeks"])
    write_csv(SPLIT_EVENTS, split_table)
    write_csv(SPECIAL, special_table)
    write_csv(REVIEWED_MOVES, queue)
    log(f"split_events.csv {len(split_table)} rows, special_distributions.csv {len(special_table)}, "
        f"reviewed_moves.csv {len(queue)}")
    cover, gaps = coverage(states, targets, sessions)
    write_csv(OUT / "coverage_gaps.csv", gaps)
    ends = series_ends(states, targets, identity, sessions)
    write_csv(OUT / "series_ends.csv", ends)
    missing = no_series_table(states, targets)
    write_csv(OUT / "no_series.csv", missing)
    pairs = pd.DataFrame([p for s in states.values() for p in s["pairs"]])
    write_csv(OUT / "source_pairs.csv", pairs)
    relists, relist_facts = relist_table(states, identity)
    write_csv(OUT / "relist_junctions.csv", relists)
    securities = pd.DataFrame([{**{k: (json.dumps(v) if isinstance(v, list) else v) for k, v in s["summary"].items()
                                   if not isinstance(v, dict)},
                                "rows_by_primary": json.dumps(s["summary"].get("rows_by_primary", {}), sort_keys=True),
                                "flag_counts": json.dumps(s["summary"].get("flag_counts", {}), sort_keys=True)}
                               for s in states.values()])
    write_csv(OUT / "securities.csv", securities)
    by_type, by_year, multi = scan["by_type"], scan["by_year"], scan["multi"]
    multi = {**multi, "note": "includes the stored file's vote as a source (the plan 6 count as first reported)"}
    multi_vendor = vendor_agreement(states, pairs, targets)
    checks = known_case_checks(states, split_table, special_table, identity)
    sources_per_security = Counter(len([x for x in s["summary"].get("sources", "").split() if x != "stored"])
                                   for s in states.values())
    summary = {
        "built_utc": now_utc(), "code_version": CODE_VERSION, "scripts_git_commit": git_commit(),
        "rules": {"tol_r": TOL_R, "tol_level": TOL_LEVEL, "tol_ratio": TOL_RATIO, "move_2x": MOVE_2X,
                  "move_big": MOVE_BIG, "special_pct": SPECIAL_PCT, "flat_run": FLAT_RUN,
                  "precedence_to_2017_10_31": PRECEDENCE_EARLY, "precedence_from_2017_11_01": PRECEDENCE_LATE,
                  "stored_vote_excluded": f"unit breaks {list(BREAK_DAYS)}, ex-dates from {STORED_EX_FROM}, the raw "
                                          "jump of a vendor's split, and a stored-only unit change (every vendor "
                                          f"agrees, the stored file agrees with none and is off by an ordinary "
                                          f"split ratio within {TOL_UNIT:.0%}, or on a known break day by any "
                                          f"lasting factor beyond {TOL_STORED_RATIO:.0%}, the vendors' own move is "
                                          "not split-sized); a stored return that agrees with any vendor stays a "
                                          "vote, except on the break days",
                  "relist_jump": RELIST_JUMP,
                  "stored_queue_diff": STORED_QUEUE_DIFF, "ratio_queue_tr": RATIO_QUEUE_TR,
                  "filler_volume_share": FILLER_VOLUME_SHARE, "tol_cash_split": TOL_CASH_SPLIT},
        "targets": {"securities": int(len(targets)), "candidates": int(targets["in_candidates"].sum()),
                    "rank300_any_week": int(targets["rank300"].sum()),
                    "with_series": int(sum(1 for s in states.values() if s["summary"].get("rows"))),
                    "without_series": int(len(missing)),
                    "without_series_by_reason": {k: int(v) for k, v in missing["reason"].value_counts().items()}
                    if len(missing) else {},
                    "vendor_sources_per_security": {str(k): int(v) for k, v in sorted(sources_per_security.items())}},
        "sources": prep["facts"],
        "tiingo_waiting_rows": int(len(tiingo_waiting())),
        "coverage_ranked": cover,
        "flag_counts": by_type, "flag_counts_by_year": by_year, "multi_source_days": multi,
        "multi_vendor_days": multi_vendor,
        "single_vendor_vs_stored_by_difference": scan["stored_gaps"],
        "yahoo_volume_flag_rows": {name: int(by_type.get(name, 0)) for name in YAHOO_VOLUME_TOKENS.values()},
        "split_events": split_facts, "special_distributions": special_facts, "review_queue": queue_facts,
        "series_ends": {"by_category": {k: int(v) for k, v in ends["category"].value_counts().items()},
                        "by_cause": {f"{c}|{k}": int(v) for (c, k), v in
                                     ends.groupby(["category", "likely_cause"]).size().items()}} if len(ends) else {},
        "filler_rows_cut": int(sum(s["summary"].get("filler_cut", 0) for s in states.values())),
        "filler_rows_cut_tiny_volume": int(sum(s["summary"].get("filler_cut_tiny_volume", 0) for s in states.values())),
        "non_session_rows_dropped": dict(sum((Counter(s["summary"].get("dropped_non_session", {})) for s in states.values()),
                                             Counter())),
        "tiingo_identity_rows": int(sum(s["summary"].get("tiingo_identity_rows", 0) for s in states.values())),
        "tiingo_run_vs_old_diff_days": int(sum(s["summary"].get("tiingo_run_vs_old_diff_days", 0) for s in states.values())),
        "relistings": relist_facts,
        "break_days": break_day_table(states),
        "checks": checks,
        "files": {"prices": str(PRICES_DIR), "split_events": str(SPLIT_EVENTS), "special_distributions": str(SPECIAL),
                  "reviewed_moves": str(REVIEWED_MOVES), "series_ends": str(OUT / "series_ends.csv"),
                  "coverage_gaps": str(OUT / "coverage_gaps.csv"), "no_series": str(OUT / "no_series.csv"),
                  "relist_junctions": str(OUT / "relist_junctions.csv")},
        "no_returns_aggregated": "tr is per security and day; this summary holds counts only",
    }
    if not args.no_panel:
        summary["panel"] = write_panel(ids, states)
    write_json(OUT / "summary.json", summary)
    return summary


def vendor_agreement(states: dict[str, dict], pairs: pd.DataFrame, targets: pd.DataFrame) -> dict:
    """Plan 6's two price checks without the stored vote: kept name-days with two or more vendor
    returns that agree within 0.5% of ``tr``, and the V sample's Yahoo r against Tiingo r within
    1e-4 (with all names' Tiingo-Yahoo days beside it). Counts of days only; no return is aggregated."""
    days = sum(s["summary"].get("rows_multi_vendor", 0) for s in states.values())
    agree = sum(s["summary"].get("rows_multi_vendor_agree", 0) for s in states.values())
    out = {"vendor_only": {"name_days_2plus_vendors": int(days), "agree_0p5pct": int(agree),
                           "share": round(agree / days, 5) if days else None}}
    candidates = pd.read_csv(CANDIDATES, dtype=str, keep_default_na=False)
    v_sample = set(candidates.loc[candidates["reason"] == V_SAMPLE_REASON, "security_id"])
    pair_rows = pairs if len(pairs) else pd.DataFrame(columns=["security_id", "a", "b", "days_r", "days_r_1e4",
                                                                  "days_r_0p5pct"])
    by_pair = {}
    for (a, b), part in pair_rows.groupby(["a", "b"]):
        by_pair[f"{a}-{b}"] = {"days": int(part["days_r"].sum()), "within_1e4": int(part["days_r_1e4"].sum()),
                               "within_0p5pct": int(part["days_r_0p5pct"].sum())}
    out["pair_days"] = by_pair
    ty = pair_rows[(pair_rows["a"] == "tiingo") & (pair_rows["b"] == "yahoo")]
    for label, part in (("v_sample_tiingo_yahoo", ty[ty["security_id"].isin(v_sample)]), ("all_tiingo_yahoo", ty)):
        n_days = int(part["days_r"].sum())
        hit = int(part["days_r_1e4"].sum())
        out[label] = {"securities": int(part.loc[part["days_r"] > 0, "security_id"].nunique()), "days": n_days,
                      "within_1e4": hit, "share_1e4": round(hit / n_days, 5) if n_days else None,
                      "within_0p5pct": int(part["days_r_0p5pct"].sum())}
    out["v_sample_tiingo_yahoo"]["v_sample_names"] = len(v_sample)
    return out


def git_commit() -> str:
    try:
        import subprocess
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


COMPARE_NUMERIC = {"close_raw": 1e-9, "volume_raw": 1e-9, "split_factor": 1e-12, "div_cash": 1e-12, "tr": 1e-9}


def compare_series(old: pd.DataFrame, new: pd.DataFrame) -> dict:
    """Row-level differences between two canonical files of one security: dates added and removed, and on
    common dates the rows whose close, volume, S, D or tr changed (relative 1e-9; a blank against a value
    counts), whose primary source changed, or whose flags changed. Counts of rows only."""
    o, w = old.set_index("date"), new.set_index("date")
    common = o.index.intersection(w.index)
    out = {"old_rows": int(len(o)), "new_rows": int(len(w)), "old_first": o.index.min() if len(o) else "",
           "old_last": o.index.max() if len(o) else "", "new_first": w.index.min() if len(w) else "",
           "new_last": w.index.max() if len(w) else "", "rows_added": int(len(w.index.difference(o.index))),
           "rows_removed": int(len(o.index.difference(w.index)))}
    for column, tolerance in COMPARE_NUMERIC.items():
        a = pd.to_numeric(o.loc[common, column], errors="coerce").to_numpy(float)
        b = pd.to_numeric(w.loc[common, column], errors="coerce").to_numpy(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            same = (np.isnan(a) & np.isnan(b)) | (np.abs(a - b) <= tolerance * np.maximum(np.abs(a), 1.0))
        out[f"{column}_changed"] = int((~same).sum())
    for column in ("src_primary", "flags"):
        a = o.loc[common, column].fillna("").astype(str).to_numpy()
        b = w.loc[common, column].fillna("").astype(str).to_numpy()
        out[f"{column}_changed"] = int((a != b).sum())
    values = sum(out[f"{c}_changed"] for c in list(COMPARE_NUMERIC) + ["src_primary"])
    out["kind"] = ("dates" if out["rows_added"] or out["rows_removed"] else
                   "values" if values else "flags_only" if out["flags_changed"] else "unchanged")
    return out


def compare_outputs(defaults: dict) -> dict:
    """The build in --out-dir against the default outputs (``defaults``: CACHE/prices, CACHE/reconcile and the
    INPUTS tables): securities with a canonical file, rows, which series changed and how, and the tables'
    row counts. Writes reconcile/compare.json and reconcile/compare_series.csv (one row per security)."""
    read = dict(dtype={"date": str, "flags": str, "src_primary": str}, keep_default_na=False, na_values=[""])
    old_ids = {p.stem for p in Path(defaults["prices"]).glob("*.csv")}
    new_ids = {p.stem for p in PRICES_DIR.glob("*.csv")}
    rows = []
    for sid in sorted(old_ids | new_ids):
        old_path, new_path = Path(defaults["prices"]) / f"{sid}.csv", PRICES_DIR / f"{sid}.csv"
        if sid not in new_ids:
            old = pd.read_csv(old_path, **read)
            rows.append({"security_id": sid, "kind": "removed", "old_rows": len(old), "new_rows": 0,
                         "old_first": old["date"].min(), "old_last": old["date"].max()})
        elif sid not in old_ids:
            new = pd.read_csv(new_path, **read)
            rows.append({"security_id": sid, "kind": "added", "old_rows": 0, "new_rows": len(new),
                         "new_first": new["date"].min(), "new_last": new["date"].max()})
        else:
            rows.append({"security_id": sid, **compare_series(pd.read_csv(old_path, **read), pd.read_csv(new_path, **read))})
    frame = pd.DataFrame(rows)
    write_csv(OUT / "compare_series.csv", frame)

    def table_rows(path: Path, column: str | None = None) -> dict:
        if not Path(path).exists():
            return {"rows": None}
        data = pd.read_csv(path, dtype=str, keep_default_na=False)
        out = {"rows": int(len(data))}
        if column and column in data:
            out["by"] = {k: int(v) for k, v in data[column].value_counts().items()}
        return out

    def summary_of(directory: Path) -> dict:
        path = Path(directory) / "summary.json"
        return json.loads(path.read_text()) if path.exists() else {}

    old_summary, new_summary = summary_of(defaults["reconcile"]), summary_of(OUT)
    rule = lambda path: (pd.read_csv(path, dtype=str, keep_default_na=False)["notes"].str.extract(r"^\[([^\]]+)\]")[0]
                         .value_counts().to_dict() if Path(path).exists() else {})
    kinds = frame["kind"].value_counts().to_dict() if len(frame) else {}
    headline = {"securities_old": len(old_ids), "securities_new": len(new_ids),
                "securities_added": len(new_ids - old_ids), "securities_removed": len(old_ids - new_ids),
                "rows_old": int(frame["old_rows"].fillna(0).sum()) if len(frame) else 0,
                "rows_new": int(frame["new_rows"].fillna(0).sum()) if len(frame) else 0,
                "series_by_kind": {k: int(v) for k, v in kinds.items()}}
    changed = frame[frame["kind"] != "unchanged"].copy() if len(frame) else frame
    if len(changed):
        changed["row_change"] = changed["new_rows"].fillna(0) - changed["old_rows"].fillna(0)
    out = {
        "built_utc": now_utc(), "old": {k: str(v) for k, v in defaults.items()},
        "new": {"prices": str(PRICES_DIR), "reconcile": str(OUT), "tables": str(SPLIT_EVENTS.parent)},
        "headline": headline,
        "largest_row_changes": changed.reindex(changed["row_change"].abs().sort_values(ascending=False).index)
        .head(40)[["security_id", "kind", "old_rows", "new_rows", "old_first", "old_last", "new_first", "new_last"]]
        .fillna("").to_dict("records") if len(changed) else [],
        "tables": {"split_events": {"old": table_rows(defaults["split_events"], "event_type"),
                                    "new": table_rows(SPLIT_EVENTS, "event_type")},
                   "special_distributions": {"old": table_rows(defaults["special"]), "new": table_rows(SPECIAL)},
                   "reviewed_moves": {"old": {"rows": table_rows(defaults["reviewed_moves"])["rows"],
                                              "by_rule": rule(defaults["reviewed_moves"])},
                                      "new": {"rows": table_rows(REVIEWED_MOVES)["rows"], "by_rule": rule(REVIEWED_MOVES)}}},
        "summary": {key: {"old": old_summary.get(key), "new": new_summary.get(key)}
                    for key in ("targets", "tiingo_waiting_rows", "series_ends")},
        "coverage_dv50_rank_1_300": {"old": (old_summary.get("coverage_ranked") or {}).get("dv50_rank_1_300", {}).get("all"),
                                     "new": (new_summary.get("coverage_ranked") or {}).get("dv50_rank_1_300", {}).get("all")},
        "note": "row and series counts only; tr is compared row by row for equality, never aggregated",
    }
    write_json(OUT / "compare.json", out)
    return out


def main(argv=None) -> int:
    global MIN_FREE_MB, WRITE_SOURCE_CACHE
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--only", default="", help="comma-separated security ids: build these, write no tables")
    parser.add_argument("--rebuild", action="store_true",
                        help="ignore every cache of this step (source bundles and per-security state): rebuild from the raw files")
    parser.add_argument("--no-panel", action="store_true", help="skip prices/daily_panel.csv.gz")
    parser.add_argument("--out-dir", default="",
                        help="write prices/, reconcile/ and inputs/ (the three tables) under this directory instead of "
                             "CACHE and INPUTS")
    parser.add_argument("--no-source-cache", action="store_true",
                        help="build the source bundles in memory and do not write them (saves about 0.7 GB of disk)")
    parser.add_argument("--min-free-mb", type=float, default=MIN_FREE_MB,
                        help="stop before writing when the output disk has less free space than this")
    parser.add_argument("--compare", action="store_true",
                        help="with --out-dir: compare the build with the default outputs (securities, rows, changed "
                             "series, tables) into reconcile/compare.json and compare_series.csv")
    args = parser.parse_args(argv)
    MIN_FREE_MB, WRITE_SOURCE_CACHE = args.min_free_mb, not args.no_source_cache
    forbid_network()
    defaults = {"prices": PRICES_DIR, "reconcile": OUT, "split_events": SPLIT_EVENTS, "special": SPECIAL,
                "reviewed_moves": REVIEWED_MOVES}
    configure_paths(Path(args.out_dir) if args.out_dir else None)
    for directory in (PRICES_DIR, OUT, SOURCE_CACHE, STATE_DIR, LOG_DIR, SPLIT_EVENTS.parent):
        directory.mkdir(parents=True, exist_ok=True)
    log(f"outputs: prices {PRICES_DIR}, reconcile {OUT}, tables {SPLIT_EVENTS.parent}; rebuild {args.rebuild}; "
        "network refused")
    only = [s.strip() for s in args.only.split(",") if s.strip()] or None
    started = time.time()
    timings = {}

    def phase(name: str, since: float) -> float:
        now = time.time()
        timings[name] = round(now - since, 1)
        log(f"phase {name}: {timings[name]:.1f}s (peak RSS so far {peak_rss_mb():,.0f} MB)")
        return now

    mark = time.time()
    prep = prepare(only, args.rebuild)
    mark = phase("prepare_sources", mark)
    ids = sorted(set(prep["targets"]["security_id"]) if only is None else set(only))
    states = run_securities(ids, prep["bundles"], prep["identity"], prep["windows"], prep["sessions"], args.rebuild)
    mark = phase("securities", mark)
    if only is not None:
        for sid in ids:
            summary = states[sid]["summary"]
            log(f"{sid}: {summary.get('rows', 0)} rows {summary.get('first_date')}..{summary.get('last_date')} "
                f"{summary.get('rows_by_primary')} flags {summary.get('flag_counts')}")
            log(f"  events {len(states[sid]['events'])}, specials {len(states[sid]['specials'])}, "
                f"queue {Counter(m['rule'] for m in states[sid]['moves'])}")
            for key in ("segments", "junctions", "relist_jumps", "break_days"):
                if summary.get(key):
                    log(f"  {key}: {summary[key]}")
        return 0
    summary = summarize_tables(states, prep, ids, args)
    mark = phase("tables_and_panel", mark)
    log(f"coverage dv50 1-300: {summary['coverage_ranked']['dv50_rank_1_300']['all']}")
    if args.compare and args.out_dir:
        compare = compare_outputs(defaults)
        mark = phase("compare", mark)
        log(f"compare with the default outputs: {json.dumps(compare['headline'])}")
    run = {"argv": sys.argv[1:] if argv is None else list(argv), "out_dir": str(Path(args.out_dir).resolve()) if args.out_dir else "",
           "rebuild": bool(args.rebuild), "source_cache_written": WRITE_SOURCE_CACHE,
           "network": "refused (forbid_network)", "timings_s": timings,
           "total_s": round(time.time() - started, 1), "peak_rss_mb": peak_rss_mb(), "finished_utc": now_utc()}
    summary["run"] = run
    write_json(OUT / "summary.json", summary)
    write_json(LOG_DIR / f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json", run)
    log(f"done in {run['total_s']:.0f}s, peak RSS {run['peak_rss_mb']:,.0f} MB; summary {OUT / 'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
