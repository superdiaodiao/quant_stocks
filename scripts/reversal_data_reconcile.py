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
- Relist junctions (``RELIST_JUNCTIONS``, each read by hand from the SEC documents it names on 2026-10-02:
  the emergence 8-Ks of CHRD/Oasis 2020-11-20, CORZ 2024-01-24, WW 2025-06-27 and OPI 2026-06-22, and for
  THRY/Dex Media 2018-04-18, which filed no 8-K after its 2016 deregistration, Thryv's 2020 prospectus;
  plus the 8-Ks that date each old share's last Nasdaq session): a bankruptcy plan cancelled or exchanged
  the old shares and the new ones were listed again. Old and new shares are separate segments of the
  file: the new shares' first traded row has no ``tr`` (``relist_junction``), any S or D a vendor
  records on it is dropped (reported), and nothing is chained, voted, spliced, flat-run or queued across
  it; R5's filler cut applies to each segment's end, and new-share rows with volume 0 in every vendor
  before the first trade, from the plan's effective date on, are cut (``junction_leading_zero_volume_cut``:
  OPI's Yahoo placeholder on 2026-06-18, after the 2026-06-17 effective date and before the entry's first
  traded session 2026-06-22; zero-volume rows before the effective date are the old shares' R5 filler). The old
  shares end with a terminal event (``series_ends.csv`` ``old_shares_at_relist_junction``, measured
  against their last Nasdaq session, for the terminal step). Other relistings keep one series (the same
  shares: SMCI, removed for late filings), and a raw level change of 10x or more around one with no
  split is queued (R9, ``relist_jump``) for a junction entry or a market move. A relisting is any
  ``after_cut`` span, and (round 9) any later listing span after a common-stock Form 25 of the issuer, under
  the same ticker or a new one (``form25_relistings``: Frontier's FTR, Form 25 effective 2020-05-09, listed
  again as FYBR from 2021-05-04); round 9 added the junctions of Frontier (2021-05-04), Vroom (2025-02-20,
  whose 2024 Form 25 is not in the step-3 table) and Capstone (CEPL, 2026-07-08).
- Plan R9 gaps: no return across a gap of more than ``GAP_RETURN_MAX`` = 10 sessions between two kept rows
  (tr blank, ``gap_return_blank``; summary.json ``gap_returns_blank``): Frontier 2018-03-01 after a 213-session
  WIKI gap that holds the 2017 1:15 reverse split, NANO 2019-10-28.
- Successor links (``SUCCESSOR_LINKS``: the 26 master successor links of target securities whose series ended at
  the link in round 8): every predecessor is cut at its last session (the terminal step's reading of the
  closing 8-K or Form 25), so no day is in two series. A 1:1 holding-company reorganisation or reincorporation
  (``continues``; owner convention of 2026-10-02, as CRSP keeps one PERMNO: Google -> Alphabet 2015-10-02) continues
  the security: the predecessor's later rows under the same ticker go to the successor, the successor's own rows
  up to the cut (the old company's history in a file of the ticker) are dropped, and the successor's first return
  is measured from the predecessor's last close (``successor_link:{predecessor}``; the predecessor's rows on the
  last session are the anchor, never kept); its listing is counted from the next session. A link that does not
  continue (an election, cash or another company's shares) only cuts: the successor starts a new series with no
  return on its first row (``successor_of:{predecessor}``). The successors of continuing targets are targets too
  (``successor_of_target``, along a chain). ``reconcile/successor_links.csv`` lists every pair after the build, with
  ``overlap_days`` (days in both series: 0 for every continuing pair) and ``short_after_continuation`` (a continued
  successor whose own files stop long before its listing ends: SOHU Ltd, Xperi, Ferroglobe, Stratasys Ltd, ...).
  Round 10 added the 1:1 reorganisations and renames of the terminal step's REVIEWED table with no master link
  (ESRX, MRVL, ASRT, SBGI, VNOM, RTIX, Z, LBTYB, LMCA/LMCK, LINTA/LINTB, QVCA/QVCB, QRTEA/QRTEB, OZRK: continued) and
  three cuts (ISBC's 2.55 conversion, UNIT's 0.6029 merger, AMTBB folded into AMTB). A predecessor is listed up to its
  reviewed last session and a successor from its first session (the master's snapshot-dated intervals start or end
  later or earlier: Bank OZK only from 2018-08-07), and a continuing predecessor gets back its successor's rows
  between its own last row and the cut (Zillow 2014-11-21..2015-02-17). A cut successor's first row books no split
  or cash (the conversion ratio is the predecessor's terminal value: ISBC 2.55, UNIT 0.602). The continuing anchor is the
  predecessor's last vendor row on or before its last session (LBTYB's last trade 2013-06-06). Step 7's trim mark
  on a successor's first Yahoo row (``yahoo_junction``) is cleared when rows of the link come before it (MRVL
  2021-04-27). Fox (21CF's cut): the successor's first session is the day it shares with 21CF (2019-03-19, 21CF
  trading as TFCFA; ``successor_first_session``), and its rows up to the step-7 file's start (2019-05-06, the
  master's snapshot-dated interval start) come from the cached raw Yahoo chart (``raw_yahoo_fill``).
- A target with no listing interval at all (Ford 37996, a NYSE stock whose only Nasdaq interval step 4 removed)
  gets no series (no_series.csv reason ``no_listing_interval``) instead of every vendor row of its ticker.
- A special dividend paid to holders at a merger closing (the terminal step's REVIEWED ``special_dividend``) is
  booked once, in the terminal value (the last close still carries it): a vendor booking of the same amount in
  the 45 days up to its record date is dropped from the series (``div_in_terminal_value``; CHNG 2022-09-28 $2.00,
  STAY 2021-06-11 $1.75; summary.json ``dividends.terminal_dividends_dropped``).
- ``tr`` = (C_t x S_t + D_t) / C_{t-1} - 1 from one source's own rows (plan 4.1), so only returns
  are chained across sources, never levels (R8). A listing's first traded row (vendor volume > 0) is blank
  too when it would be measured against a quote (``listing_start_after_quote``): every row since the listing
  start (a listed session after an unlisted one) is untraded, the row before it is untraded, no vendor row
  traded in the ``pf.CLOSE_STALE_SESSIONS`` sessions before it and the kept closes there equal that row's
  (THRY 2020-10-01: +88.8% against 480 identical zero-volume Yahoo closes; an IPO file whose first row has
  volume 0, then the first trade). The untraded rows themselves keep their tr (round 9: Yahoo files with
  volume 0 every day while the close moves are prices). summary.json lists them (``listing_starts_after_quote``).
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
- ``n_sources`` = sources with a return that day (vendors plus a valid stored vote; 0 on a
  ``listing_start_after_quote`` row);
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
  10x level change around an unreviewed relisting after a Form 25 (dates and the ratio only). Notes carry
  dates, ratios and percentages, never a vendor price level: the table is committed (plan 8), and the
  queue step refuses a note with one (``LEVEL_IN_NOTE``). Only
  listed days that can matter are queued: 10 weeks before to 5 weeks after a week ranked <= 300 by
  step 6, or whose canonical dollar volume reaches step 6's rank-300 cut; every R9 hit is queued
  whatever its scope; every entry, with that scope marked, is in ``CACHE/reconcile/moves_all.csv``.
  An R1 entry that two or more sources confirm within 0.5% (``sources_agreeing``: the day's own source and
  another vendor, or the stored file, which counts as a second source by the owner's convention of
  2026-10-02) is resolved by plan 4.4's own R1 rule: classified mechanically as ``market_move_second_source``
  with ``verified_at`` the build date and the basis in ``notes``; every other entry stays ``unreviewed``.
- The hand review (round 10, merged by ``scripts/reversal_data_review.py`` into ``CACHE/review/round10/merged``;
  ``--review-dir`` reads another merge, ``--no-review`` none): ``moves_verdicts.csv`` fills the queue's
  classification, source_url and verified_at keyed (security_id, event_date, rule), with a short note naming the
  item (an ``unresolved`` verdict leaves the row ``unreviewed``); ``split_verdicts.csv`` and
  ``distribution_verdicts.csv`` fill split_events.csv sec_url / verified_at and special_distributions.csv sec_url
  keyed (security_id, ex_date) (``sec_url`` = the document, or ``evidence: <sources>`` for a two-source verdict).
  A ``vendor_error`` verdict with a ``correct_source`` picks the day's canonical source (an R7 run: every session to
  its end_date; flags ``review_source:<src>`` and, on a day the vote left unresolved, ``disagree_reviewed`` instead of
  ``disagree_unresolved``); one without a correct_source flags the day ``review_vendor_error``. The queue's R3 and R7
  rules still read the vote, so the rows the verdicts answer stay in the queue. Verdicts that change S or D (an
  unrecorded event, a corrected or rejected split or distribution; ``merged/moves_data_changes.csv`` and
  ``merged/split_data_changes.csv``) are applied to the canonical row of their day (``review_event_plan``: S and/or D
  set, tr recomputed against the same prior close, flag ``review_event:<item>``) when their evidence is an SEC
  document and they state S and D as numbers; the others (a spin-off whose distributed shares need a value, a package
  with an unvalued part; ``REVIEW_EVENT_EXCEPTIONS``) are not applied and leave their queue rows open (no sec_url or
  verified_at, classification ``unreviewed``), so the validate step's open counts match the series.
  ``reconcile/review_event_changes.csv`` lists every such verdict with its status (applied / unused / not_applied /
  type_only) and summary.json ``review_event_changes`` counts them. A confirmed distribution verdict that gives a
  ratio, no cash, and says a vendor books both (``DOUBLE_COUNT_NOTE``) drops the cash from a canonical row that
  carries the factor and the cash. ``reconcile/review_data_changes.csv`` is the merge's two data-change lists with
  ``applied`` / ``how`` written from this build (the merged files stay as the merge wrote them). A day whose close
  the review calls doubtful while one build source has it (``DOUBTFUL_PRICE_DAYS``: COSM 2022-12-16 and -19) is
  flagged ``doubtful_price`` and its queue rows stay open. A Yahoo row in another unit (``scale`` in an exception:
  CBSH 2012-11-28) has its close and cash restated as traded after tr.
- ``HALTED_SPANS``: a documented halt after the last trade (UCFI, NUTR): its listed zero-volume rows are kept
  (flag ``halt``) instead of cut as R5 filler, and their R4 entries are classified by the evidence named up to the
  last day a document shows the halt (``documented_to``); the rows after it are flagged ``halt_end_undocumented`` and
  their R4 entry (the run is split at that day) stays open.
- The predecessor of a continuing SUCCESSOR_LINKS link marked ``tiingo_to_successor_end`` keeps its Tiingo answer to its
  successor's window end, so the successor gets the same shares' rows it lacks (ASRT from 2020-06-19; QVCGA from
  2025-03-03 via QRTEA's QVCAQ file); only those two links (round-10 gap review), so no other series changes source.
- ``CACHE/dividends.csv`` (plan 1.2): every cash dividend the canonical series books, as paid on the ex-date
  (security_id, ex_date, cash_as_paid, sources: the vendors with the same amount that day within $0.001;
  then ticker, src_primary, other_amounts, special).
- ``CACHE/reconcile/``: summary.json (coverage of ranks 1-300 by year with step 6's 5-session
  staleness rule, flag counts by type and year, the plan-6 multi-source agreement count with the
  stored vote and, separately, among vendors only and for the V sample's Tiingo-Yahoo days within
  1e-4, known-case checks), series_ends.csv (series that end before the delist date, or before the window end with
  none: the inputs for terminal values, with a likely cause; the old shares at a relist junction with
  their last Nasdaq session and cancellation date, and a WIKI-end cause when the series stops years
  before them: OPI), coverage_gaps.csv, no_series.csv,
  securities.csv, source_pairs.csv, moves_all.csv, relist_junctions.csv (every relisting after a Form
  25 and every junction: dates, rows on each side, status, document read and its dates), summary.json
  ``break_days`` (``how_to_read``, then per known break day the stored files that move 1.4x or more,
  by state with their tickers: ``unit_break``, or ``stored_moves_with_vendors`` for a real move both
  show: EYEN/HYPD +65%, NKTR +156%, UPXI -60% on 2025-06-24, which step 6's and the validate step's
  stored-only tests list as breaks with no unit_break row: none is expected) and ``run`` (timings,
  peak memory); ``sources/`` holds the source bundles and
  ``per_security/`` the state that makes a rerun redo only the securities whose inputs (or this
  file) changed. ``CACHE/prices/daily_panel.csv.gz`` is the long form of every canonical file. ``--rebuild``
  moves the price files of securities that are no longer targets (round 8's six D6 BDCs) into
  ``reconcile/superseded_prices_{date}/``, and an emptied series' file goes there too: data files are never
  deleted (summary.json ``price_files``).

Offline: the step reads local files only, and any socket connection in the process is refused
(``forbid_network``). Each phase logs its time and the peak resident memory; summary.json ``run`` and
``reconcile/logs/run_*.json`` keep them.

Usage::

    PYTHONPATH=. python scripts/reversal_data_reconcile.py                 # build (resumes)
    PYTHONPATH=. python scripts/reversal_data_reconcile.py --only 1065088  # one security, no tables
    PYTHONPATH=. python scripts/reversal_data_reconcile.py --rebuild       # everything from the raw files
    PYTHONPATH=. python scripts/reversal_data_reconcile.py --no-panel      # skip daily_panel.csv.gz
    # the full rebuild into a scratch directory, compared with the current outputs (compare.json,
    # compare_series.csv: securities, rows, changed series, table counts; the headline counts apart the
    # series whose dates changed and whose values on the common dates changed too):
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
import re
import sys
import time

import numpy as np
import pandas as pd

from scripts import reversal_data_common as common
from scripts import reversal_data_prefilter as pf
from scripts import reversal_data_review as review_merge

CODE_VERSION = "2026-10-03.1"
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
DIVIDENDS = CACHE / "dividends.csv"  # plan 1.2: security_id, ex_date, cash_as_paid, sources (from the canonical div_cash)
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
# a vendor price level in a queue note ("close 1.125", not the ratio "close 1.0214x"): reviewed_moves.csv is
# committed and must carry dates, ratios and percentages only (plan 8, the owner's 2026-10-02 decision)
LEVEL_IN_NOTE = re.compile(r"\bclose[sd]?\s+\$?\d+(?:\.\d+)?(?![\d.]*x)", re.I)
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
# on the first traded session of the new shares: no return is computed across it, and the old shares end
# with a terminal event (step 11 values it; series_ends.csv and relist_junctions.csv carry the dates).
# Each entry was read by hand on ``read_on`` from the SEC documents named: ``url`` (the emergence 8-K, or for
# Dex Media, which filed no 8-K after its 2016-02 deregistration, Thryv's 2020 prospectus) and
# ``nasdaq_end_url`` (the 8-K that dates the old shares' last Nasdaq session). ``effective_date`` is the plan's
# Effective Date (the old shares cancelled), ``old_nasdaq_last_session`` the old shares' last Nasdaq session
# (the session before the stated suspension); the old shares may trade OTC between the two.
# ``first_new_session`` is the new shares' first traded session in the vendor data (a zero-volume row before
# it is a placeholder, not a trade). A relisting that keeps the same shares (SMCI 2020-01, removed for late
# filings; SIGA, SCOR, MDXG) has no junction.
_SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/"
# Halts R5 must not cut as filler (plan 4.4 R4: identical closes with zero volume that a second independent source
# shows are kept and flagged): listed sessions after the last trade where the series stays listed and halted. Rows
# from ``start`` are kept (flag ``halt``) and their R4 queue entries are classified by the evidence named here, but
# only up to ``documented_to``, the last day a document shows the halt in effect (round-10 merge review: no document
# dates the end of either halt). Kept rows after it are flagged ``halt_end_undocumented`` and their R4 entry stays
# open for the owner (UCFI from 2026-07-22, NUTR after the SEC suspension's 2025-10-22 end).
# Round-10 gap review (universe_listed_gaps), checked on 2026-10-03 against the cached 8-Ks named.
HALTED_SPANS = {
    "1901203": {"start": "2025-10-02", "documented_to": "2026-07-21", "classification": "flat_genuine",
                "evidence": "yahoo+stored",
                "url": _SEC_ARCHIVE + "1901203/000121390026080024/ea0298572-8k_cnhealthy.htm",
                "note": "UCFI: Yahoo and the stored file both show one constant close and zero volume on every session "
                        "from 2025-10-02; the 8-K of 2026-07-21 (Item 3.01, Nasdaq delisting determination) names 'the "
                        "trading halt currently in effect' (its start is not stated)"},
    "2006468": {"start": "2025-10-09", "documented_to": "2025-10-22", "classification": "flat_genuine",
                "evidence": "yahoo+stored",
                "url": _SEC_ARCHIVE + "2006468/000149315225018007/form8-k.htm",
                "note": "NUTR: the 8-K of 2025-10-14 (Item 8.01) reports the SEC order suspending trading 2025-10-09 to "
                        "2025-10-22 and a Nasdaq information request; Yahoo and the stored file both show one constant "
                        "close and zero volume on every session from 2025-10-09 (no document dates the end of the halt)"},
}
HALTED_REVIEWED_AT = "2026-10-03T00:00:00Z"
RELIST_JUNCTIONS = {
    "1486159": {"first_new_session": "2020-11-20", "kind": "bankruptcy_new_equity", "read": True, "read_on": "2026-10-02",
                "effective_date": "2020-11-19", "old_nasdaq_last_session": "2020-10-09",
                "url": _SEC_ARCHIVE + "1486159/000148615920000115/oas-20201119.htm",
                "nasdaq_end_url": _SEC_ARCHIVE + "1486159/000148615920000080/oas-20201002.htm",
                "note": "Oasis Petroleum: Nasdaq delisted the common stock at the opening of business on 2020-10-12 and "
                        "it traded on OTC Pink as OASAQ from that day (8-K 2020-10-02, Item 3.01); the plan became "
                        "effective on 2020-11-19 (the Effective Date): the existing common stock was cancelled and its "
                        "holders received warrants for the new common stock, which trades on Nasdaq as OAS from "
                        "2020-11-20 (CHRD after the 2022 Whiting merger)"},
    "1839341": {"first_new_session": "2024-01-24", "kind": "bankruptcy_share_exchange", "read": True, "read_on": "2026-10-02",
                "effective_date": "2024-01-23", "old_nasdaq_last_session": "2022-12-30",
                "url": _SEC_ARCHIVE + "1839341/000119312524013078/d661343d8k.htm",
                "nasdaq_end_url": _SEC_ARCHIVE + "1839341/000119312524013078/d661343d8k.htm",
                "note": "Core Scientific (Chapter 11 filed 2022-12-21): the old common stock traded exclusively on OTC "
                        "Pink as CORZQ from 2023-01-03; the plan became effective on 2024-01-23 (the Effective Date), "
                        "the old common stock was cancelled and its trading terminated that day, Existing Common "
                        "Interests received 21.0% of the new common stock, and Nasdaq approved listing of and trading "
                        "in the new common stock as CORZ effective 2024-01-24 (emergence 8-K, cover note, Items 1.02 "
                        "and 5.01)"},
    "105319": {"first_new_session": "2025-06-27", "kind": "bankruptcy_share_exchange", "read": True, "read_on": "2026-10-02",
               "effective_date": "2025-06-24", "old_nasdaq_last_session": "2025-05-15",
               "url": _SEC_ARCHIVE + "105319/000119312525146171/d906370d8k.htm",
               "nasdaq_end_url": _SEC_ARCHIVE + "105319/000119312525142383/d943798d8k.htm",
               "note": "WW International: the common stock was suspended from Nasdaq on 2025-05-16 and traded on the Pink "
                       "market as WGHTQ (8-K 2025-06-18); the plan became effective on 2025-06-24: the old common "
                       "stock was cancelled, and 900,000 new shares went to the holders of existing equity interests "
                       "(9,100,000 to the first-lien lenders); the new shares' first trade is 2025-06-27 (Yahoo)"},
    "1456772": {"first_new_session": "2026-06-22", "kind": "bankruptcy_new_equity", "read": True, "read_on": "2026-10-02",
                "effective_date": "2026-06-17", "old_nasdaq_last_session": "2025-10-06",
                "url": _SEC_ARCHIVE + "1456772/000110465926076652/tm2618043d2_8k.htm",
                "nasdaq_end_url": _SEC_ARCHIVE + "1456772/000110465925107765/tm2530037d1_8k.htm",
                "note": "Office Properties Income Trust: the common shares were suspended from Nasdaq on 2025-10-07 and "
                        "quoted on OTC Pink as OPITS (8-K 2025-11-06, cover note); the plan became effective on "
                        "2026-06-17 (the Effective Date): the Old Common Shares were cancelled and their holders did "
                        "not receive any distribution (emergence 8-K, Item 3.03); the emergence 8-K's EX-99.1 states "
                        "that the new shares are listed on Nasdaq again starting on 2026-06-18, but Yahoo's row that "
                        "day has volume 0, so the first traded session, 2026-06-22, is the junction: cutting the "
                        "2026-06-18 row rests on the vendor volume alone (keep or cut it: the owner's call)"},
    "1556739": {"first_new_session": "2018-04-18", "kind": "bankruptcy_new_equity", "read": True, "read_on": "2026-10-02",
                "effective_date": "2016-07-29", "old_nasdaq_last_session": "2016-01-06",
                "url": _SEC_ARCHIVE + "1556739/000114036120022046/nt10007762x19_424b4.htm",
                "nasdaq_end_url": _SEC_ARCHIVE + "1556739/000110465916087891/a15-25698_18k.htm",
                "note": "Dex Media (DXM, now Thryv): Nasdaq suspended the common stock at the opening of business on "
                        "2016-01-07 (8-K 2016-01-05, Item 3.01); the company deregistered (15-12B 2016-02-05) and filed "
                        "no 8-K for its 2016 prepackaged Chapter 11, from which it emerged on 2016-07-29; Thryv's "
                        "2020 prospectus (424B4): the former lenders obtained 100% of the reorganized company's common "
                        "stock (it does not say what the old holders received: that they received nothing is an "
                        "inference from it); the new shares had no public market before "
                        "the Nasdaq direct listing on 2020-10-01 (a limited history of private trades); their first "
                        "vendor row is 2018-04-18 (Yahoo)"},
    # round 9 (final review of round 8): two relistings after a Form 25 that the R9 screen missed (Frontier, under a
    # new ticker; Vroom, whose 2024 Form 25 is not in the step-3 table), and Capstone (R9 hit, 56.6x)
    "20520": {"first_new_session": "2021-05-04", "kind": "bankruptcy_new_equity", "read": True, "read_on": "2026-10-02",
              "effective_date": "2021-04-30", "old_nasdaq_last_session": "2020-04-23",
              "url": _SEC_ARCHIVE + "20520/000114036121015200/brhc10023786_8k12g3.htm",
              "nasdaq_end_url": _SEC_ARCHIVE + "20520/000114036120009130/brhc10011071-8k.htm",
              "note": "Frontier Communications: Nasdaq suspended the common stock at the opening of business on "
                      "2020-04-24 and OTC trading as FTRQ was expected from that day (8-K 2020-04-17, Item 3.01); the "
                      "plan became effective on 2021-04-30 (the Effective Date): Old Frontier's common stock was "
                      "canceled, released and extinguished (Item 1.02), and the new parent (successor issuer under "
                      "Rule 12g-3) issued 244,400,000 shares to the holders of Allowed Senior Notes Claims (Item 3.02); "
                      "its common stock trades on Nasdaq as FYBR from 2021-05-04 (the 8-K12G3: 'expected to begin on "
                      "or about May 4, 2021'; the first vendor row is 2021-05-04); the old FTR series stops at the "
                      "WIKI end (2018-03-27 table, last FTR row 2018-03-07)"},
    "1580864": {"first_new_session": "2025-02-20", "kind": "bankruptcy_share_exchange", "read": True,
                "read_on": "2026-10-02", "effective_date": "2025-01-14", "old_nasdaq_last_session": "2024-11-29",
                "url": _SEC_ARCHIVE + "1580864/000095017025005647/vrm-20250108.htm",
                "nasdaq_end_url": _SEC_ARCHIVE + "1580864/000119312524266124/d882020d8k.htm",
                "note": "Vroom: Nasdaq suspended the common stock at the opening of business on 2024-12-02 (8-K "
                        "2024-11-26, Item 3.01); the prepackaged plan became effective on 2025-01-14: all previously "
                        "issued equity interests were cancelled and extinguished, and the 1,822,577 old shares were "
                        "converted at 1-for-5 (the Bankruptcy Emergence Issuance Adjustment, 5:00 p.m. on 2025-01-14) "
                        "into new common stock, with 364,516 warrants (exercise price $60.95) to the stockholders; the "
                        "convertible noteholders received 92.94% of the 5,163,109 new shares (emergence 8-K, Items 1.01, "
                        "1.03, 3.02, 5.01); the 8-A12B of 2025-02-19 registered the new common stock on the Nasdaq "
                        "Global Market, and the first vendor row is 2025-02-20 (Tiingo)"},
    "1009759": {"first_new_session": "2026-07-08", "kind": "bankruptcy_share_exchange", "read": True,
                "read_on": "2026-10-02", "effective_date": "2023-12-07", "old_nasdaq_last_session": "2023-10-04",
                "url": _SEC_ARCHIVE + "1009759/000110465923124877/tm2332548d1_8k12g3.htm",
                "nasdaq_end_url": _SEC_ARCHIVE + "1009759/000155837023016108/cgrn-20230926x8k.htm",
                "note": "Capstone Green Energy (CGRN): trading on Nasdaq was to be suspended at the opening of business "
                        "on 2023-10-05 (8-K 2023-09-28, Item 3.01; Form 25 filed 2023-10-12); the prepackaged plan "
                        "became effective on 2023-12-07: the Old Common Stock was canceled, released and extinguished, "
                        "and the successor (Capstone Green Energy Holdings, formerly Capstone Turbine International) "
                        "issued 18,540,877 new shares pro rata to the holders of Old Capstone's common stock (8-K12G3, "
                        "Items 1.02 and 3.02; no per-share ratio stated); the new shares were quoted OTC as CGEH and "
                        "trade on the Nasdaq Global Market as CEPL from 2026-07-08 (8-K 2026-07-07, Item 7.01; 8-A12B "
                        "2026-07-07): a separate segment, so the 56.6x level change is no return"},
}
GAP_RETURN_MAX = 10  # plan R9: no return across a gap of more than 10 sessions (tr blank, ``gap_return_blank``)
LIST_REVIEW_DAYS = 40  # summary: at most this many changed days per security
TERMINAL_DIVIDEND_DAYS = 45  # a closing special dividend the terminal value owns: a vendor booking this close before it
R1_MECHANICAL_CLASS = "market_move_second_source"  # an R1 entry the plan's rule resolves (two sources within 0.5%)
# The hand review's merged verdicts (scripts/reversal_data_review.py; CACHE/review/round10/merged): the moves queue's
# classifications, split_events/special_distributions sec_url and verified_at, and the per-day source choices of
# vendor_error verdicts (``source_overrides``). --review-dir reads another merge; --no-review reads none.
REVIEW_DIR = review_merge.MERGED_DIR
APPLY_REVIEW = True
APPLY_REVIEW_SOURCE_OVERRIDES = True  # vendor_error verdicts with a correct_source pick the day's canonical source
_REVIEW_CACHE: dict[str, pd.DataFrame | None] = {}


def review_table(name: str) -> pd.DataFrame | None:
    """A merged verdict table (moves_verdicts.csv, split_verdicts.csv, distribution_verdicts.csv), or None."""
    if not APPLY_REVIEW:
        return None
    if name not in _REVIEW_CACHE:
        _REVIEW_CACHE[name] = review_merge.load_merged(name, REVIEW_DIR)
    return _REVIEW_CACHE[name]


def review_overrides() -> dict[str, list[dict]]:
    """Per security, the reviewed source choices (vendor_error with a correct_source) and the reviewed vendor errors
    without one: {"source": [...], "flag": [...]} spans as reversal_data_review gives them."""
    moves = review_table("moves_verdicts.csv")
    choose = review_merge.source_overrides(moves) if APPLY_REVIEW_SOURCE_OVERRIDES else {}
    flags = review_merge.reviewed_days(moves)
    events = review_event_plan()
    applied = events[events["action"] == "apply"]
    per_sid: dict[str, list[dict]] = {}
    for r in applied.to_dict("records"):
        per_sid.setdefault(r["security_id"], []).append(
            {"date": r["date"], "split": r["split"], "cash": r["cash"], "item_id": r["item_id"], "source": r["source"],
             "mode": r["mode"], "scale": r["scale"]})
    out = {sid: {"source": choose.get(sid, []), "flag": flags.get(sid, [])} for sid in set(choose) | set(flags)}
    for sid, items in per_sid.items():
        out.setdefault(sid, {"source": [], "flag": []})["events"] = sorted(items, key=lambda e: (e["date"], e["item_id"]))
    return out


# Round-10 verdicts that change S or D on one day (the merge lists them in merged/moves_data_changes.csv and
# merged/split_data_changes.csv). ``review_event_plan`` turns each into an action on the canonical row of that day:
# ``apply`` (set S and/or D: ``split`` / ``cash``, NaN = unchanged; tr is recomputed from the same prior close),
# ``type_only`` (the verdict changes the event's type, not S or D: LSXMB's reclassification) or ``not_applied``
# (``reason``; the queue row stays open). A verdict is applied only when its evidence is a primary document (an SEC
# URL) and it states the day's S and D as numbers: a split or exchange ratio, a stock dividend of the same class (or
# of a class the document values at one common share: ATRO's Class B), cash, or a factor that values distributed
# shares at a close two vendors agree on (ZG, FLEX). A spin-off or reclassification whose verdict gives only the
# number of another security's shares (UNTD/FTD, LMCA/LMCK/LSXMK, LBTYK/LILAK, SPWR/MAXN, QRTEA's preferred, GLIBA's
# GLIBP leg, QVCA/LVNTA) needs a value no document gives: not applied. The rules, by queue and verdict:
# - splits corrected: S = split_factor on ex_date_confirmed (else ex_date); a moved date also sets S = 1 on ex_date.
# - splits not_a_split: S = 1. splits reclassify_distribution: type only (event_type distribution).
# - distributions corrected: special_cash: D = cash_per_share and S = ratio when given (LENZ's 1:7 the same day);
#   stock_dividend: S = ratio, D = cash_per_share (0 when none); spinoff with a ratio: S = ratio, D = 0; spinoff or
#   other without a factor: not applied.
# - distributions not_a_distribution: with a ratio (an ordinary or share-exchange ratio the vendor booked as a
#   distribution): S = ratio (the document's exact ratio), D = 0; without one: the booking is removed (S = 1, D = 0).
# - distributions confirmed with a ratio, no cash, and a note that a vendor books the factor and the cash together
#   ("apply one, not both", "a double count": LBTYK 2015, LBRDA/LBRDK 2025; LBTYA and ATRO, whose canonical rows
#   carry one of them, stay as they are): a canonical row with S != 1 and D > 0 keeps S and gets D = 0
#   (``mode`` drop_double_cash). The other confirmed verdicts whose rows carry both (a stock dividend paid with the
#   regular cash dividend: CBSH, CZFS, HWBK, PEBK, CASS; a reverse split with a special dividend: JBIO, BOTA, CMCT;
#   EXPE's 1-for-2 with the TripAdvisor value as cash, which the note calls consistent) are what the documents say.
# - moves unrecorded_event: split / reverse_split / stock_dividend: S = split_factor, D = cash_per_share when given
#   (else unchanged); spinoff: not applied (split_factor is the distributed shares per share).
# ``REVIEW_EVENT_EXCEPTIONS`` overrides these rules per item, each read against the verdict's note and the vendor rows.
REVIEW_EVENT_EXCEPTIONS = {
    # the note: the IPO split was before listing, so the factor on 2012-02-13 should be 1 (the ratio field is the
    # split's ratio, not the day's factor)
    "distributions-08-033": {"split": 1.0, "cash": 0.0, "why": "CZR: the 1.742 split was before the listing; S = 1"},
    # the note: WIKI's closes already carry the split (no step on 2011-11-03 or 2011-11-09), so 0.2 on the WIKI raw row
    # would create a false move; the vendor's 0.2 on 2011-11-09 is removed and nothing is booked on 2011-11-03
    "splits-10-002": {"split": 1.0, "cash": None,
                      "why": "GEVA: WIKI's closes are already split-adjusted (the note): S = 1 on 2011-11-09; the "
                             "verdict's 2011-11-03 ex-date is not booked on those closes"},
    # CBSH 2012-11-28: the canonical row is Yahoo's, whose levels are the as-traded close / 1.05 (Yahoo lacks the 2016
    # 5% factor: the verifier's note), so the declared 1.73 is 1.73 / 1.05 in that row's units (tr +0.67%, as on the
    # WIKI closes; stored +0.70%). Only on a Yahoo row
    # ``scale``: after tr, the row's close and cash are restated in as-traded units (close x 1.05 = WIKI's raw
    # close; cash 1.73), so close_raw is raw; tr is unchanged
    "distributions-06-019": {"split": 1.05, "cash": 1.73 / 1.05, "source": "yahoo", "scale": 1.05,
                             "why": "CBSH: 5% stock dividend and 1.73 cash, in the Yahoo row's units (close / 1.05); "
                                    "the close and cash are then restated as traded"},
    # cash plus notes (principal 1.08 per pre-split share): the notes have no value in any document
    "distributions-01-040": {"apply": False, "why": "CBIO: the Pre-Closing Dividend was cash plus notes; the notes "
                                                    "have no documented value"},
    # GLIBA: 0.63 GLIBA plus 0.2 GLIBP per Class A-1 share; the GLIBP leg has no local price
    "moves-08-005": {"apply": False, "why": "GLIBA: the 0.2 GLIBP leg per share has no value (the note)"},
    # LMCA 2014: factor 3 counts LMCK at par with LMCA; the note's own board value (convertible notes) implies 2.823
    "moves-05-022": {"apply": False, "why": "LMCA: factor 3 counts the 2 LMCK shares at par; the note's board value "
                                            "implies 2.823, so the value is not fixed by the document"},
    # ZG 2015-08-17: the distributions verdict (04-038) values the 2 Class C shares at the Z close WIKI and the stored
    # file agree on (2.941292); the moves verdicts' factor 3 counts them at par. One action per day: the distributions
    # verdict, and the moves verdicts close with it
    "moves-11-010": {"apply": False, "same_as": "distributions-04-038",
                     "why": "ZG: booked by distributions-04-038 (2.941292, Class C at its own close); this verdict's "
                            "factor 3 counts Class C at par"},
    "moves-11-011": {"apply": False, "same_as": "distributions-04-038",
                     "why": "ZG: booked by distributions-04-038 (2.941292, Class C at its own close); this verdict's "
                            "factor 3 counts Class C at par"},
}
_EVENT_PLAN: dict[str, pd.DataFrame] = {}
EVENT_PLAN_COLUMNS = ["item_id", "queue", "security_id", "ticker", "date", "verdict", "kind", "split", "cash", "source",
                      "action", "reason", "same_as", "source_url", "mode", "scale"]


# a confirmed distribution verdict whose note says a vendor carries the factor and the cash together, a double
# count ("apply one, not both"; "a double count")
DOUBLE_COUNT_NOTE = re.compile(r"apply one, not both|double count", re.IGNORECASE)
DROP_DOUBLE_CASH = "drop_double_cash"
# Days whose close the hand review itself calls doubtful while one build source has them: the row is flagged
# ``doubtful_price`` and its reviewed_moves rows stay open (whatever their verdicts) until another source or a
# document gives the close. COSM 2022-12-16: moves-01-004 applies the documented 1-for-25 (S = 0.04) but says the
# day's Yahoo close is doubtful (+179% with the factor, then -67% on 2022-12-19). moves-05-006's second source for
# 2022-12-19 ('yahoo+stored') is the repo's Stooq file his_data/us/nasdaq/stocks_price/1/cosm.us.txt (the
# cleaned stored cosm.csv starts 2023-01-03); it has the same closes on both days, so it agrees with Yahoo but is
# not a source of this build, and the 2022-12-19 return is measured from the doubtful close
DOUBTFUL_PRICE_DAYS = {
    ("1474167", "2022-12-16"): "COSM: moves-01-004 calls the Yahoo close doubtful; only Yahoo (and the repo's "
                               "Stooq his_data file, not a build source) has the day",
    ("1474167", "2022-12-19"): "COSM: the return is measured from the doubtful 2022-12-16 close; moves-05-006's "
                               "'stored' evidence is the repo's Stooq his_data file (the stored cosm.csv starts "
                               "2023-01-03)",
}


def _primary_document(url: str) -> bool:
    host = re.sub(r"^https?://", "", str(url or "")).split("/")[0].lower()
    return host in ("www.sec.gov", "sec.gov")


def _review_num(value) -> float:
    try:
        return float(value) if value is not None and str(value).strip() != "" else np.nan
    except ValueError:
        return np.nan


def snap_reverse_ratio(value: float, tolerance: float = 5e-4) -> float:
    """A reverse-split factor a verdict gives rounded (0.0074074 for 1-for-135, 0.003333 for 1-for-300) as the exact
    1/m, m an integer or a one- or two-decimal number (1-for-5.5, 1-for-17.85), when 1/value is within ``tolerance``
    of it; any other value (and every value of 1 or more) is kept as given."""
    if not np.isfinite(value) or value <= 0 or value >= 1:
        return value
    m = 1.0 / value
    for digits in (0, 1, 2):
        near = round(m, digits)
        if near > 0 and abs(m / near - 1.0) <= tolerance:
            return 1.0 / near
    return value


def event_actions(queue: str, v: dict) -> list[dict]:
    """The S/D actions one verdict states (before the exceptions and the primary-document check): a list of
    {date, split, cash} (NaN = unchanged), or [] with ``kind`` 'type_only' / 'open' in the first element's place.
    Returns [{"kind": ..., "date": ..., "split": ..., "cash": ..., "reason": ...}]."""
    verdict = v.get("verdict") or v.get("classification", "")
    day = v.get("ex_date") or v.get("event_date", "")
    confirmed = v.get("ex_date_confirmed") or day
    ratio = snap_reverse_ratio(_review_num(v.get("ratio", "")))
    factor = snap_reverse_ratio(_review_num(v.get("split_factor", "")))
    cash = _review_num(v.get("cash_per_share", ""))
    nan = np.nan
    if queue == "splits":
        if verdict == "corrected" and np.isfinite(factor):
            out = [{"kind": "apply", "date": confirmed, "split": factor, "cash": nan}]
            if confirmed != day:
                out.insert(0, {"kind": "apply", "date": day, "split": 1.0, "cash": nan})
            return out
        if verdict == "not_a_split":
            return [{"kind": "apply", "date": day, "split": 1.0, "cash": nan}]
        if verdict == "reclassify_distribution":
            return [{"kind": "type_only", "date": day, "split": nan, "cash": nan,
                     "reason": "a distribution, not a split: event_type distribution; S and D unchanged"}]
        return []
    if queue == "distributions":
        kind = v.get("distribution_type", "")
        if verdict == "corrected":
            if kind == "special_cash" and np.isfinite(cash):
                return [{"kind": "apply", "date": confirmed, "split": ratio if np.isfinite(ratio) else nan, "cash": cash}]
            if kind == "stock_dividend" and np.isfinite(ratio):
                return [{"kind": "apply", "date": confirmed, "split": ratio, "cash": cash if np.isfinite(cash) else 0.0}]
            if kind == "spinoff" and np.isfinite(ratio):
                return [{"kind": "apply", "date": confirmed, "split": ratio, "cash": 0.0}]
            return [{"kind": "open", "date": confirmed, "split": nan, "cash": nan,
                     "reason": f"{kind or 'distribution'} without a documented factor (the distributed security needs "
                               "a value)"}]
        if verdict == "confirmed" and np.isfinite(ratio) and not np.isfinite(cash) \
                and DOUBLE_COUNT_NOTE.search(str(v.get("notes", ""))):
            # the verdict confirms a ratio, no cash, and says a vendor books both (LBTYK, LBRDA, LBRDK): a canonical
            # row that carries a factor and cash keeps its factor and loses the cash; a row with one of them is kept
            return [{"kind": "apply", "date": confirmed, "split": nan, "cash": nan, "mode": DROP_DOUBLE_CASH}]
        if verdict == "not_a_distribution":
            if np.isfinite(ratio):
                return [{"kind": "apply", "date": day, "split": ratio, "cash": 0.0}]
            return [{"kind": "apply", "date": day, "split": 1.0, "cash": 0.0}]
        return []
    if queue == "moves" and verdict == "unrecorded_event":
        kind = v.get("event_type", "")
        if kind in ("split", "reverse_split", "stock_dividend") and np.isfinite(factor):
            return [{"kind": "apply", "date": day, "split": factor, "cash": cash if np.isfinite(cash) else nan}]
        return [{"kind": "open", "date": day, "split": nan, "cash": nan,
                 "reason": f"{kind or 'event'}: split_factor is the distributed shares per share, which need a value"}]
    return []


def review_event_plan() -> pd.DataFrame:
    """Every verdict that changes S or D (or an event's type), with its action (EVENT_PLAN_COLUMNS)."""
    key = str(REVIEW_DIR) if APPLY_REVIEW else ""
    if key in _EVENT_PLAN:
        return _EVENT_PLAN[key]
    rows = []
    sources = [("splits", review_table("split_verdicts.csv")), ("distributions", review_table("distribution_verdicts.csv")),
               ("moves", review_table("moves_verdicts.csv"))]
    for queue, table in sources:
        if table is None or not len(table):
            continue
        for v in table.to_dict("records"):
            actions = event_actions(queue, v)
            if not actions:
                continue
            exception = REVIEW_EVENT_EXCEPTIONS.get(v["item_id"], {})
            if exception and exception.get("apply") is not False:
                # the exception restates the day's single action (on ex_date unless it names another date)
                actions = [{"kind": "apply", "date": exception.get("date") or v.get("ex_date") or v.get("event_date"),
                            "split": np.nan if exception.get("split") is None else exception["split"],
                            "cash": np.nan if exception.get("cash") is None else exception["cash"],
                            "reason": exception["why"]}]
            primary = _primary_document(v.get("source_url", ""))
            for a in actions:
                action, reason = a["kind"], a.get("reason", "")
                if exception.get("apply") is False:
                    action, reason = "not_applied", exception["why"]
                if action == "open":
                    action = "not_applied"
                if action == "apply" and not primary:
                    action, reason = "not_applied", "no primary document (the verdict's source is not an SEC URL)"
                rows.append({"item_id": v["item_id"], "queue": queue, "security_id": v["security_id"],
                             "ticker": v.get("ticker", ""), "date": a["date"],
                             "verdict": v.get("verdict") or v.get("classification", ""),
                             "kind": v.get("distribution_type") or v.get("event_type", ""),
                             "split": a["split"], "cash": a["cash"], "source": exception.get("source", ""),
                             "action": action, "reason": reason,
                             "same_as": exception.get("same_as", ""), "source_url": v.get("source_url", ""),
                             "mode": a.get("mode", ""), "scale": exception.get("scale", np.nan)})
    frame = pd.DataFrame(rows, columns=EVENT_PLAN_COLUMNS)
    _EVENT_PLAN[key] = frame
    return frame

# Successor links (``security_master.successor_security_id``) of target securities whose series ended at the link
# (series_ends.csv ``successor_link`` in round 8: 26 pairs). Every predecessor is cut at its real last session,
# ``last_session`` (the terminal step's reading of the closing 8-K or the Form 25, ``basis``; ``url`` is that
# document), so its later rows, under the same ticker, go to the successor and no day is in both series.
# ``continues`` (owner convention of 2026-10-02, as CRSP keeps one PERMNO): a 1:1 holding-company reorganisation or
# reincorporation (each share became one successor share, no cash or election: the terminal step's
# stock_merger / reorganization of 1 share) continues the same security, so the successor's first return is measured
# from the predecessor's last close (``successor_link`` on that row) and no terminal return is booked; the successor's
# rows dated on or before ``last_session`` (a Yahoo or Tiingo file of the ticker carries the old company's history)
# are the predecessor's and are dropped. Pairs that do not continue (an election, cash, or another company's shares:
# 21CF -> Fox 2019, Pinnacle 2016, Angie's List 2017, AspenTech 2022) are cut the same way; their successor starts as a
# new series (no return on its first row) and the predecessor keeps its terminal value. Reviewed on 2026-10-02
# against the terminal step's dates and the vendor rows around each date (``successor_links.csv``).
# Optional keys: ``successor_first_session`` (the successor's first session when it trades on the predecessor's
# last one, under the ticker: Fox on 2019-03-19, the day 21CF traded as TFCFA); ``raw_yahoo_fill`` (a ticker: the
# successor's rows from that first session to its first step-7 Yahoo row come from the cached raw v8 chart of the
# ticker, which step 7 cut at a later snapshot-dated interval start); ``handover_files`` (a cut-only link whose
# predecessor's own per-security files run on in the successor: UNIT); ``handover`` False (a class reclassified
# into another existing class: only the predecessor is cut, the other class keeps its rows and gets none).
# Round 10 added the 1:1 reorganisations and renames of the terminal step's REVIEWED table that the master gives no
# successor link (ESRX, MRVL, ASRT, SBGI, VNOM, RTIX, Z, LBTYB, LMCA/LMCK, LINTA/LINTB, QVCA/QVCB, QRTEA/QRTEB, OZRK)
# and three cuts (ISBC, UNIT, AMTBB), each dated from the cached closing document.
_SUCC = "https://www.sec.gov/Archives/edgar/data/"
SUCCESSOR_LINKS = {
    "1288776.A": {"successor": "1652044.A", "ticker": "GOOGL", "last_session": "2015-10-02", "continues": True,
                  "basis": "last_closing_8k_filing",
                  "url": _SUCC + "1288776/000119312515336550/0001193125-15-336550-index.htm",
                  "note": "Google -> Alphabet holding-company reorganisation; Alphabet trades from 2015-10-05"},
    "1288776.C": {"successor": "1652044.C", "ticker": "GOOG", "last_session": "2015-10-02", "continues": True,
                  "basis": "last_closing_8k_filing",
                  "url": _SUCC + "1288776/000119312515336550/0001193125-15-336550-index.htm",
                  "note": "Google -> Alphabet holding-company reorganisation; Alphabet trades from 2015-10-05"},
    "1100962": {"successor": "1593034", "ticker": "ENDP", "last_session": "2014-02-28", "continues": True,
                "basis": "successor_form25_filing", "url": _SUCC + "1100962/000119312514077915/d683967d8k.htm",
                "note": "Endo Health Solutions -> Endo International at the Paladin closing, one share per share"},
    "1104188": {"successor": "1734107", "ticker": "SOHU", "last_session": "2018-05-31", "continues": True,
                "basis": "successor_form25_filing",
                "url": _SUCC + "1104188/000135445718000173/0001354457-18-000173-index.htm",
                "note": "Sohu.com Inc -> Sohu.com Limited (redomicile, one ADS per share)"},
    "1141107": {"successor": "1645494", "ticker": "ARRS", "last_session": "2016-01-04", "continues": True,
                "basis": "last_closing_8k_filing", "url": _SUCC + "1141107/000119312516420003/d112940d8k.htm",
                "note": "ARRIS Group -> ARRIS International plc at the Pace closing, one share per share"},
    "1261694": {"successor": "1690666", "ticker": "TSRA", "last_session": "2016-12-01", "continues": True,
                "basis": "last_closing_8k_filing", "url": _SUCC + "1261694/000119312516782591/d298517d8k.htm",
                "note": "Tessera Technologies -> Tessera Holding (later Xperi) at the DTS acquisition, one share per share"},
    "1277856": {"successor": "1671013", "ticker": "CATM", "last_session": "2016-06-30", "continues": True,
                "basis": "session_before_stated_successor_start",
                "url": _SUCC + "1277856/000110465916130585/0001104659-16-130585-index.htm",
                "note": "Cardtronics Inc -> Cardtronics plc (reincorporation); the plc trades as CATM from 2016-07-01"},
    "1316631.A": {"successor": "1570585.T-LBTYA", "ticker": "LBTYA", "last_session": "2013-06-07", "continues": True,
                  "basis": "last_closing_8k_filing", "url": _SUCC + "1316631/000119312513251856/d548311d8k.htm",
                  "note": "Liberty Global Inc -> Liberty Global plc at the Virgin Media closing, one share per share"},
    "1316631.C": {"successor": "1570585.T-LBTYK", "ticker": "LBTYK", "last_session": "2013-06-07", "continues": True,
                  "basis": "last_closing_8k_filing", "url": _SUCC + "1316631/000119312513251856/d548311d8k.htm",
                  "note": "Liberty Global Inc -> Liberty Global plc at the Virgin Media closing, one share per share"},
    "1383571": {"successor": "1639877", "ticker": "GSM", "last_session": "2015-12-23", "continues": True,
                "basis": "successor_form25_filing", "url": _SUCC + "1383571/000119312515413088/d106496d8k.htm",
                "note": "Globe Specialty Metals -> Ferroglobe, one share per share; GSM suspended prior to the open on "
                        "2015-12-24"},
    "1441634": {"successor": "1649338", "ticker": "AVGO", "last_session": "2016-01-29", "continues": True,
                "basis": "successor_form25_filing", "url": _SUCC + "1441634/000119312516446897/d121614d8k.htm",
                "note": "Avago -> Broadcom Limited (the Avago scheme), one share per share; Form 25 filed after the "
                        "close of trading on 2016-01-29"},
    "1566895": {"successor": "1902733", "ticker": "NCNO", "last_session": "2022-01-07", "continues": True,
                "basis": "session_before_halt_stated_in_closing_8k",
                "url": _SUCC + "1566895/000119312522005080/d272832d8k.htm",
                "note": "nCino OpCo -> nCino, Inc. (holding company at the SimpleNexus closing), one share per share"},
    "1570585.T-LILA": {"successor": "1712184.A", "ticker": "LILA", "last_session": "2017-12-29", "continues": True,
                       "basis": "successor_form25_filing",
                       "url": _SUCC + "1570585/000157058518000013/0001570585-18-000013-index.htm",
                       "note": "LiLAC Class A -> Liberty Latin America Class A (split-off at 5:00 p.m. on 2017-12-29), "
                               "one share per share"},
    "1570585.T-LILAK": {"successor": "1712184.C", "ticker": "LILAK", "last_session": "2017-12-29", "continues": True,
                        "basis": "successor_form25_filing",
                        "url": _SUCC + "1570585/000157058518000013/0001570585-18-000013-index.htm",
                        "note": "LiLAC Class C -> Liberty Latin America Class C (split-off at 5:00 p.m. on 2017-12-29), "
                                "one share per share"},
    "1649338": {"successor": "1730168", "ticker": "AVGO", "last_session": "2018-04-04", "continues": True,
                "basis": "successor_form25_filing",
                "url": _SUCC + "1649338/000119312518107587/0001193125-18-107587-index.htm",
                "note": "Broadcom Limited -> Broadcom Inc. (redomicile), one share per share"},
    "1772757": {"successor": "1883685", "ticker": "DKNG", "last_session": "2022-05-04", "continues": True,
                "basis": "session_before_stated_successor_start",
                "url": _SUCC + "1772757/000110465921101852/tm2124529d1_8k.htm",
                "note": "DraftKings Inc. -> New DraftKings (holding company at the GNOG closing), one share per share; "
                        "New DraftKings trades from the open on 2022-05-05"},
    "353569": {"successor": "1906324", "ticker": "QDEL", "last_session": "2022-05-26", "continues": True,
               "basis": "reviewed", "url": _SUCC + "1906324/000119312522161806/d323352d8k12b.htm",
               "note": "Quidel -> QuidelOrtho, one share per share; QuidelOrtho trades as QDEL from 2022-05-27"},
    "6769": {"successor": "1841666", "ticker": "APA", "last_session": "2021-03-01", "continues": True,
             "basis": "last_closing_8k_filing", "url": _SUCC + "1841666/000119312521063695/d127090d8k12b.htm",
             "note": "Apache -> APA Corporation holding-company reorganisation; the 8-K12B states no effective time, so "
                     "2021-03-01 may already be APA's first session: under the continuation no return depends on it"},
    "69499": {"successor": "1623613", "ticker": "MYL", "last_session": "2015-02-27", "continues": True,
              "basis": "last_closing_8k_filing", "url": _SUCC + "69499/000119312515068777/d882093d8k.htm",
              "note": "Mylan Inc. -> Mylan N.V. at the Abbott EPD closing, one share per share"},
    "904163": {"successor": "1801075", "ticker": "ANAT", "last_session": "2020-07-01", "continues": True,
               "basis": "session_before_stated_successor_start",
               "url": _SUCC + "904163/000119312520186645/0001193125-20-186645-index.htm",
               "note": "American National Insurance -> American National Group holding-company reorganisation"},
    "915735": {"successor": "1517396", "ticker": "SSYS", "last_session": "2012-11-30", "continues": True,
               "basis": "successor_form25_filing", "url": _SUCC + "915735/000120677412001496/stratasys_8k.htm",
               "note": "Stratasys Inc -> Stratasys Ltd (the Objet merger), one share per share; Stratasys Ltd from "
                       "2012-12-03"},
    # round 10: the 1:1 reorganisations, reincorporations and renames of the terminal step's REVIEWED table
    # (stock_merger / reorganization or rename, one share) that the master gives no successor link; each date read
    # in the cached closing document (``url``)
    "885721": {"successor": "1532063", "ticker": "ESRX", "last_session": "2012-03-30", "continues": True,
               "basis": "session_before_halt_stated_in_closing_8k",
               "url": _SUCC + "885721/000119312512144955/d328743d8k.htm",
               "note": "Express Scripts -> Express Scripts Holding at the Medco closing (effective 2012-04-02), one share "
                       "per share; the 8-K accepted at 08:07 on 2012-04-02 says trading in the Company's stock 'has been "
                       "halted' and the Parent's stock 'will trade' as ESRX, so the old shares' last session is "
                       "2012-03-30"},
    "1058057": {"successor": "1835632", "ticker": "MRVL", "last_session": "2021-04-20", "continues": True,
                "basis": "effective_after_close_stated_in_closing_8k",
                "url": _SUCC + "1058057/000119312521122807/d156000d8k.htm",
                "note": "Marvell Technology Group (Bermuda) -> Marvell Technology, Inc. (Delaware) at the Inphi closing, "
                        "one share per share; the Bermuda Merger took effect at 4:01 p.m. ET on 2021-04-20"},
    "1005201": {"successor": "1808665", "ticker": "ASRT", "last_session": "2020-05-19", "continues": True,
                "basis": "session_before_stated_listing_transfer",
                "url": _SUCC + "1005201/000110465920065440/tm2020220-1_8k.htm",
                "note": "Assertio Therapeutics -> Assertio Holdings (DGCL 251(g) holding company, 2020-05-19), one share "
                        "per share; the Nasdaq listing passed to Assertio Holdings 'effective as of May 20, 2020' (Item "
                        "3.01; Nasdaq's Form 25 for the old shares is dated 2020-05-19)",
                "tiingo_to_successor_end": "round-10 gap review: Assertio Holdings has no Tiingo answer of its own; "
                                           "the answer fetched for 1005201 (ticker ASRT) runs on to 2026-06"},
    "912752": {"successor": "1971213", "ticker": "SBGI", "last_session": "2023-05-31", "continues": True,
               "basis": "effective_before_open_stated_in_closing_8k",
               "url": _SUCC + "912752/000119312523158935/d530850d8k.htm",
               "note": "Sinclair Broadcast Group -> Sinclair, Inc. (holding company share exchange), one share per "
                       "share, effective at 12:00 a.m. ET on 2023-06-01, so SBG's last session is 2023-05-31"},
    "1602065": {"successor": "2074176", "ticker": "VNOM", "last_session": "2025-08-18", "continues": True,
                "basis": "session_before_stated_successor_start",
                "url": _SUCC + "1602065/000119312525183040/d65540d8k.htm",
                "note": "Viper Energy -> New Viper (holding company at the Sitio combination), one Class A share per "
                        "share at 12:01 a.m. ET on 2025-08-19; New Viper Class A began trading on Nasdaq as VNOM on "
                        "2025-08-19"},
    "1100441": {"successor": "1760173", "ticker": "RTIX", "last_session": "2019-03-08", "continues": True,
                "basis": "session_before_halt_stated_in_closing_8k",
                "url": _SUCC + "1100441/000119312519069904/d719647d8k.htm",
                "note": "RTI Surgical -> RTI Surgical Holdings (later Surgalign) at the Paradigm closing, one share per "
                        "share; the old shares were suspended prior to the open on 2019-03-11 and the holding company "
                        "continued regular-way trading as RTIX 'using the Company's trading history'"},
    "1334814": {"successor": "1617640.A", "ticker": "Z", "last_session": "2015-02-17", "continues": True,
                "basis": "last_session_stated_in_closing_8k",
                "url": _SUCC + "1334814/000119312515050780/d874732d8k.htm",
                "note": "Zillow, Inc. Class A -> Zillow Group Class A at the Trulia closing, one share per share; the "
                        "8-K filed on 2015-02-17: 'After close of market today' trading in Zillow's Class A ceases and "
                        "the holding company's Class A trades as Z"},
    "1316631.B": {"successor": "1570585.T-LBTYB", "ticker": "LBTYB", "last_session": "2013-06-07", "continues": True,
                  "basis": "last_closing_8k_filing", "url": _SUCC + "1316631/000119312513251856/d548311d8k.htm",
                  "note": "Liberty Global Inc Series B -> Liberty Global plc Class B at the Virgin Media closing, one "
                          "share per share (as LBTYA and LBTYK)"},
    "1560385.T-LMCA": {"successor": "1560385.T-FWONA", "ticker": "LMCA", "last_session": "2017-01-24", "continues": True,
                       "basis": "symbol_change_after_8a12b_amendment",
                       "url": _SUCC + "1560385/000110465917003788/a17-3007_18a12ba.htm",
                       "note": "rename of the Liberty Media Group Series A tracking stock to Formula One Group (LMCA -> "
                               "FWONA); the 8-A12B/A filed after the close on 2017-01-24 expects the symbols to change "
                               "'shortly following the filing', and WIKI's last LMCA row is 2017-01-24"},
    "1560385.T-LMCK": {"successor": "1560385.T-FWONK", "ticker": "LMCK", "last_session": "2017-01-24", "continues": True,
                       "basis": "symbol_change_after_8a12b_amendment",
                       "url": _SUCC + "1560385/000110465917003788/a17-3007_18a12ba.htm",
                       "note": "rename of the Liberty Media Group Series C tracking stock to Formula One Group (LMCK -> "
                               "FWONK), with Series A"},
    "1355096.T-LINTA": {"successor": "1355096.T-QVCA", "ticker": "LINTA", "last_session": "2014-10-06", "continues": True,
                        "basis": "session_before_stated_successor_start",
                        "url": _SUCC + "1355096/000135509614000070/lint-20141006x8k.htm",
                        "note": "rename LINTA -> QVCA (Series A QVC Group tracking stock), effective at the market open "
                                "on 2014-10-07"},
    "1355096.T-LINTB": {"successor": "1355096.T-QVCB", "ticker": "LINTB", "last_session": "2014-10-06", "continues": True,
                        "basis": "session_before_stated_successor_start",
                        "url": _SUCC + "1355096/000135509614000070/lint-20141006x8k.htm",
                        "note": "rename LINTB -> QVCB, effective at the market open on 2014-10-07"},
    "1355096.T-QVCA": {"successor": "1355096.T-QRTEA", "ticker": "QVCA", "last_session": "2018-03-09", "continues": True,
                       "basis": "session_before_stated_successor_start",
                       "url": _SUCC + "1355096/000110465918017857/a18-8242_1ex99d1.htm",
                       "note": "rename QVCA -> QRTEA (Qurate Retail) after the GCI Liberty split-off: 'Beginning on "
                               "Monday, March 12, 2018' the Series A shares trade as QRTEA"},
    "1355096.T-QVCB": {"successor": "1355096.T-QRTEB", "ticker": "QVCB", "last_session": "2018-03-09", "continues": True,
                       "basis": "session_before_stated_successor_start",
                       "url": _SUCC + "1355096/000110465918017857/a18-8242_1ex99d1.htm",
                       "note": "rename QVCB -> QRTEB from 2018-03-12"},
    "1355096.T-QRTEA": {"successor": "1355096.T-QVCGA", "ticker": "QRTEA", "last_session": "2025-02-21", "continues": True,
                        "basis": "session_before_stated_successor_start",
                        "url": _SUCC + "1355096/000110465925016368/tm257272d1_8k.htm",
                        "note": "rename QRTEA -> QVCGA (QVC Group), 'effective as of open of trading on February 24, "
                                "2025'",
                        "tiingo_to_successor_end": "round-10 gap review: QVCGA has 5 rows of its own; the QVCAQ answer "
                                                   "fetched for QRTEA covers 2025-02-20..2026-06-30 and agrees with "
                                                   "the repo's stored qvcga.csv (290 common days, 97.9% of daily "
                                                   "returns within 1e-3)"},
    "1355096.T-QRTEB": {"successor": "1355096.T-QVCGB", "ticker": "QRTEB", "last_session": "2025-02-21", "continues": True,
                        "basis": "session_before_stated_successor_start",
                        "url": _SUCC + "1355096/000110465925016368/tm257272d1_8k.htm",
                        "note": "rename QRTEB -> QVCGB from the open of 2025-02-24"},
    "1038205": {"successor": "1569650", "ticker": "OZRK", "last_session": "2017-06-26", "continues": True,
                "basis": "effective_after_close_stated_in_closing_8k",
                "url": _SUCC + "1038205/000156459017012994/ozrk-8k_20170626.htm",
                "note": "Bank of the Ozarks, Inc. merged into its bank (Bank OZK, 1569650) at 4:00 p.m. Central on "
                        "2017-06-26 to drop the holding company: each share one bank share, same ticker and CUSIP; the "
                        "master lists the bank only from 2018-08-07 (OZK), so its listing is counted from 2017-06-27 "
                        "through the link, where WIKI's OZRK rows (to 2017-07-06) and the bank's own Yahoo file carry "
                        "it"},
    # cut only: the successor is another security (no 1:1 continuation)
    # 21CF: Fox Corporation's shares were distributed at 7:25 a.m. ET on 2019-03-19 and traded as FOXA / FOX from
    # that day, while 21CF traded that one day as TFCFA / TFCF (closing 8-K): ``successor_first_session`` is the
    # shared day (the ticker's rows that day are Fox's, the predecessor keeps its own per-security file's row);
    # step 7 cut the Yahoo file at the master's later interval start (2019-05-06, a snapshot), so the rows from
    # 2019-03-19 to that start come from the cached raw chart of the ticker (``raw_yahoo_fill``)
    "1308161.A": {"successor": "1754301.A", "ticker": "FOXA", "last_session": "2019-03-19", "continues": False,
                  "basis": "session_before_halt_stated_in_closing_8k",
                  "url": _SUCC + "1308161/000095015719000308/form8k.htm",
                  "successor_first_session": "2019-03-19", "raw_yahoo_fill": "FOXA",
                  "note": "21CF -> Disney (cash or 0.4517 share by election); Fox Corporation is a distribution, "
                          "trading as FOXA from 2019-03-19"},
    "1308161.B": {"successor": "1754301.B", "ticker": "FOX", "last_session": "2019-03-19", "continues": False,
                  "basis": "session_before_halt_stated_in_closing_8k",
                  "url": _SUCC + "1308161/000095015719000308/form8k.htm",
                  "successor_first_session": "2019-03-19", "raw_yahoo_fill": "FOX",
                  "note": "21CF -> Disney (0.4517 share); Fox Corporation is a distribution, trading as FOX from "
                          "2019-03-19"},
    "356213": {"successor": "1656239", "ticker": "PNK", "last_session": "2016-04-28", "continues": False,
               "basis": "last_closing_8k_filing", "url": _SUCC + "356213/000119312516564276/d188315d8k.htm",
               "note": "old Pinnacle: 0.85 GLPI share plus one new Pinnacle share per share"},
    "1491778": {"successor": "1705110", "ticker": "ANGI", "last_session": "2017-09-29", "continues": False,
                "basis": "reviewed", "url": _SUCC + "1491778/000149177817000194/angi2017102-8k.htm",
                "note": "Angie's List: one ANGI Homeservices share, or $8.50 cash by election"},
    "929940": {"successor": "1897982", "ticker": "AZPN", "last_session": "2022-05-16", "continues": False,
               "basis": "successor_form25_filing", "url": _SUCC + "929940/000114036122019468/ny20004077x9_8k.htm",
               "note": "old AspenTech: $87.69 cash plus 0.42 New AspenTech share (the Emerson transaction)"},
    # round 10, cut only
    "1326807": {"successor": "1594012", "ticker": "ISBC", "last_session": "2014-05-07", "continues": False,
                "basis": "reviewed", "url": _SUCC + "1594012/000119312514179187/d718726d8k.htm",
                "note": "Investors Bancorp second-step conversion: each public share became 2.55 shares of the new "
                        "Investors Bancorp (not one for one: the terminal value books the 2.55)"},
    "1620280": {"successor": "2020795", "ticker": "UNIT", "last_session": "2025-08-01", "continues": False,
                "basis": "last_closing_8k_filing", "url": _SUCC + "1620280/000095010325009717/dp232456_8k-wizard.htm",
                "handover_files": True,
                "note": "Uniti Group -> New Uniti (Windstream combination): 0.6029 New Uniti share per share; the "
                        "Yahoo file of UNIT runs on in New Uniti after the closing (with the 0.602 ratio as a split), "
                        "so its later rows are the successor's (``handover_files``)"},
    # a class reclassified into another existing class of the issuer: the predecessor is cut at its last session and
    # the other class is left as it is (``handover`` False: no rows go to it, its own rows are kept)
    "1734342.B": {"successor": "1734342.A", "ticker": "AMTBB", "last_session": "2021-11-17", "continues": False,
                  "handover": False, "basis": "effective_before_open_stated_in_closing_8k",
                  "url": _SUCC + "1734342/000173434221000071/amtb-20211115.htm",
                  "note": "Amerant Class B converted into Class A one for one by a merger effective at 12:01 a.m. on "
                          "2021-11-18; the Class B rows after 2021-11-17 are filler"},
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
    per-security state, ``inputs/`` for the three tables and ``dividends.csv``) instead of CACHE and INPUTS. The inputs
    (candidate list, caches, master) are still read from their usual places."""
    global PRICES_DIR, OUT, SOURCE_CACHE, STATE_DIR, LOG_DIR, SPLIT_EVENTS, SPECIAL, REVIEWED_MOVES, DIVIDENDS
    if out_dir is None:
        return
    root = Path(out_dir).resolve()
    PRICES_DIR, OUT, DIVIDENDS = root / "prices", root / "reconcile", root / "dividends.csv"
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
    form25 = pd.read_csv(pf.FORM25, dtype=str, keep_default_na=False) if pf.FORM25.exists() else None
    identity = identity_from(pf.load_intervals(), pf.load_master(), form25)
    identity["signature"] = file_signature([pf.MASTER, pf.INTERVALS, pf.FORM25])
    return identity


def form25_relistings(mapping: pd.DataFrame, master: pd.DataFrame, form25: pd.DataFrame | None) -> dict:
    """security -> [(Form 25 effective date, first day of the later listing, last day of it)] for every Nasdaq
    Form 25 that delisted the issuer's common stock (``classification`` common_delisting) between two of the
    security's listing spans, whatever the later span's ticker: Frontier's FTR (Form 25 effective 2020-05-09)
    listed again as FYBR from 2021-05-04, which the master's single delist date (the 2026 Form 25) and the
    ``after_cut`` rule cannot see."""
    if form25 is None or not len(form25) or "classification" not in form25:
        return {}
    common_rows = form25[form25["classification"] == "common_delisting"]
    by_cik = defaultdict(set)
    for cik, effective, filed in zip(common_rows["subject_cik"], common_rows["effective_date"], common_rows["filing_date"]):
        try:
            key = str(int(str(cik).strip()))
        except ValueError:
            continue
        if effective or filed:
            by_cik[key].add(effective or filed)
    if not by_cik:
        return {}
    cik_of = dict(zip(master["security_id"], master["cik"])) if "cik" in master else {}
    out = {}
    for sid, part in mapping.groupby("security_id"):
        try:
            key = str(int(str(cik_of.get(sid, "") or sid.split(".")[0]).strip()))
        except ValueError:
            continue
        for cut in sorted(by_cik.get(key, ())):
            before = part[part["list_start"] < cut]
            later = part[part["list_start"] > cut]
            if len(before) and len(later):
                out.setdefault(sid, []).append((cut, str(later["list_start"].min()), str(later["list_end"].max())))
    return out


def identity_from(intervals: pd.DataFrame, master: pd.DataFrame, form25: pd.DataFrame | None = None) -> dict:
    """Listing spans (``pf.listing_spans``: an interval that starts on or after the Form 25 delist date is
    a later listing, ``after_cut``, and is not cut to the delist date, so SMCI 2020-01..2026 and CHRD
    stay listed), the mapping spans that assign ticker-keyed rows, and per security its relistings:
    (Form 25 delist date, first day of the later listing, last day of it). Relistings come from the
    ``after_cut`` spans and from every common-stock Form 25 of the issuer followed by a later listing span,
    under the same ticker or a new one (``form25_relistings``: Frontier's FYBR); ``relisted_via`` says which."""
    spans = pf.listing_spans(intervals, master)
    mapping = pf.mapping_spans(spans)
    after = mapping["after_cut"].astype(str).eq("True") if "after_cut" in mapping else pd.Series(False, index=mapping.index)
    delist = dict(zip(master["security_id"], master["delist_date"]))
    relisted = {sid: [(delist.get(sid, ""), str(part["list_start"].min()), str(part["list_end"].max()))]
                for sid, part in mapping[after].groupby("security_id")}
    via = {sid: "after_cut" for sid in relisted}
    for sid, items in form25_relistings(mapping, master, form25).items():
        if sid in relisted:
            continue  # the after_cut listing already covers it
        relisted[sid] = items[-1:]  # the latest Form 25 with a later listing
        via[sid] = "form25_then_later_listing"
    return {"master": master, "spans": spans, "mapping": mapping, "ticker_map": pf.TickerMap(mapping),
            "relisted": relisted, "relisted_via": via}


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
    base = set(candidates["security_id"]) | set(top["security_id"])
    # the successor of a target that continues it (SUCCESSOR_LINKS ``continues``): the same security under the
    # owner's convention, so it is priced too (Sohu.com Ltd, Tessera Holding, Ferroglobe, Stratasys Ltd)
    # (along a chain too: LINTA -> QVCA -> QRTEA -> QVCGA)
    added: set[str] = set()
    while True:
        more = {link["successor"] for pred, link in SUCCESSOR_LINKS.items()
                if link["continues"] and pred in base | added and link["successor"] not in base | added}
        if not more:
            break
        added |= more
    ids = sorted(base | added)
    frame = pd.DataFrame({"security_id": ids}).set_index("security_id")
    frame = frame.join(reasons).join(best).join(weeks300)
    frame["in_candidates"] = frame["candidate_reasons"].notna()
    frame["rank300"] = frame["weeks_rank300"].fillna(0) > 0
    frame["successor_of_target"] = frame.index.isin(sorted(added))
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


def first_listed(mapping: pd.DataFrame, sid: str) -> pd.Timestamp:
    """The first day of the security's earliest listing span (far in the future without one)."""
    starts = mapping.loc[mapping["security_id"] == sid, "list_start"]
    return pd.Timestamp(starts.min()) if len(starts) else pd.Timestamp("2100-01-01")


def last_listed(mapping: pd.DataFrame, sid: str) -> pd.Timestamp:
    """The last day of the security's latest listing span (far in the past without one)."""
    ends = mapping.loc[mapping["security_id"] == sid, "list_end"]
    return pd.Timestamp(ends.max()) if len(ends) else pd.Timestamp("1900-01-01")


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
    securities is cut to each one's own listing spans; otherwise to its window, which for the predecessor of a
    continuing SUCCESSOR_LINKS link with ``tiingo_to_successor_end`` runs to its successor's window end: the file
    fetched for the predecessor's ticker carries the same shares on after the 1:1 reorganisation or rename, and
    link_frames hands those rows to the successor where it has none of its own (ASRT: the Tiingo answer fetched for
    1005201 to 2026-06; QVCGA: the QVCAQ answer fetched for QRTEA; round-10 gap review). Only those two links: for the
    other continuing links the extension rewrote series that already had their own vendor rows (merge review of
    round 10: LBTYB moved from Yahoo to Tiingo and started earlier, SBGI 1971213 and VNOM 2074176 moved to Tiingo,
    QVCB gained a 2017-11-14 row with another class's volume, the predecessor 1316631.B gained 68 thin rows), which no
    review asked for."""
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
    continued_to = {pred: (pd.Timestamp(link["last_session"]), windows[link["successor"]][1])
                    for pred, link in SUCCESSOR_LINKS.items()
                    if link.get("continues") and link.get("tiingo_to_successor_end") and link["successor"] in windows}
    facts["predecessor_windows_extended"] = sorted(continued_to)
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
        cut, until = continued_to.get(row.security_id, (None, None))
        if users.get(row.prices_path, 1) > 1:
            facts["shared_files"] += 1
            keep = listed_mask(mapping, row.security_id, dates)
            if cut is not None:  # after the predecessor's last session, to its successor's window end
                keep |= (dates > cut) & (dates <= until)
        else:
            low, high = windows.get(row.security_id, (pd.Timestamp(WINDOW_START), pd.Timestamp(WINDOW_END)))
            high = max(high, until) if until is not None else high
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
    # a successor that continues its predecessor (SUCCESSOR_LINKS ``continues``): the predecessor's rows on its last
    # session (``link_cut``) are in the arrays as the anchor of the successor's first return, never kept
    link_cut = ctx.get("link_cut") or ""
    anchor = grid <= pd.Timestamp(link_cut) if link_cut else np.zeros(n, dtype=bool)
    # the anchor is the predecessor's last vendor row on or before its last session (a thin series may have no row
    # on the last session itself: LBTYB's last trade 2013-06-06)
    anchored = anchor & has[:3].any(axis=0)
    k_anchor = int(np.flatnonzero(anchored)[-1]) if anchored.any() else -1
    has_vendor = has[:3].any(axis=0) & ~anchor
    if not has_vendor.any():
        result["summary"].update(rows=0, first_date="", last_date="")
        result["canonical"] = pd.DataFrame(columns=PRICE_COLUMNS)
        return result
    # a special dividend paid to holders at a merger closing belongs to the terminal value (the terminal step's
    # REVIEWED ``special_dividend``; the last close still carries it): a vendor booking of it in the 45 days up to
    # its record date (the series end without one) is not an ex-date, so it is dropped here and counted once, in
    # the terminal value (CHNG 2022-09-28 $2.00, STAY 2021-06-11 $1.75)
    terminal_div_dropped = []
    for item in ctx.get("terminal_dividends", ()):
        hi = pd.Timestamp(item.get("record") or grid[-1])
        lo = hi - pd.Timedelta(days=TERMINAL_DIVIDEND_DAYS)
        for k in np.flatnonzero((grid >= lo) & (grid <= hi)):
            for i in range(3):
                if has[i, k] and abs(D[i, k] - float(item["amount"])) < 0.005:
                    terminal_div_dropped.append({"date": str(grid[k].date()), "source": SRC[i],
                                                 "amount": float(item["amount"]), "record": item.get("record", ""),
                                                 "url": item.get("url", "")})
                    D[i, k] = 0.0
    terminal_div_days = {d["date"] for d in terminal_div_dropped}

    # relist junctions (RELIST_JUNCTIONS, ``ctx["junctions"]``): segment s runs from its junction (the first
    # session of the new shares) on. No return is computed across a junction, and an S or D a vendor
    # records on it (another history's, or the exchange served as a split) is dropped and reported.
    # The new shares' rows before their first trade (every vendor row that day has volume 0: a placeholder at a
    # reference price) are not sessions of either side: they are cut (``leading``), and the segment starts at
    # the first traded row, so no return is computed against a placeholder. They are looked for from the
    # junction on (an entry dated on a placeholder) and back from it to the plan's effective date
    # (``ctx["junction_effective"]``, the day the new shares were issued: OPI's Yahoo row on 2026-06-18,
    # before its entry's first traded session 2026-06-22); rows before the effective date are the old shares'
    # and stay with R5's filler cut
    seg = np.zeros(n, dtype=int)
    no_trade = ~(has[:3] & ~(V[:3] == 0)).any(axis=0)  # no vendor row with a volume other than 0
    leading = np.zeros(n, dtype=bool)
    effective = ctx.get("junction_effective", {})
    for day in sorted(ctx.get("junctions", ())):
        start = int(grid.searchsorted(pd.Timestamp(day)))
        if effective.get(day):
            back, issued = start - 1, pd.Timestamp(effective[day])
            while back > 0 and grid[back] >= issued and no_trade[back]:
                leading[back] = has[:3, back].any()
                back -= 1
        while 0 < start < n and no_trade[start]:
            leading[start] = has[:3, start].any()
            start += 1
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
    # the hand review (reversal_data_review, ``ctx["review"]``): a vendor_error verdict with a correct_source picks the
    # day's canonical source over the vote (the vote's own result stays what the queue rules R3 and R7 read, so the
    # queue keeps the rows the verdicts answer); a day the vote left unresolved is then resolved with the others as
    # the minority. A verdict without a correct_source (no other vendor has the day, or none is right) only flags it.
    p_vote, unresolved_vote = p.copy(), choice["unresolved"].copy()
    review_pick = np.full(n, -1)
    review_item = np.full(n, "", dtype=object)
    review_flag = np.zeros(n, dtype=bool)
    day_str = grid.strftime("%Y-%m-%d")
    review = ctx.get("review") or {}
    for item in review.get("source", ()):
        i = SRC.index(item["source"])
        span = (day_str >= item["start"]) & (day_str <= item["end"]) & has[i]
        review_pick[span] = i
        review_item[span] = item["item_id"]
    for item in review.get("flag", ()):
        review_flag |= (day_str >= item["start"]) & (day_str <= item["end"])
    review_unused = [item["item_id"] for item in review.get("source", ())
                     if not ((day_str >= item["start"]) & (day_str <= item["end"]) & has[SRC.index(item["source"])]).any()]
    for k in np.flatnonzero(review_pick >= 0):
        i = int(review_pick[k])
        primary[k] = i
        choice["unresolved"][k] = False
        if valid[i, k]:
            with np.errstate(invalid="ignore"):
                choice["minority"][:, k] = valid[:, k] & ~(np.abs(r[:, k] - r[i, k]) <= TOL_R)
    p = np.clip(primary, 0, 3)
    Cp, Vp, Sp, Dp = C[p, cols], V[p, cols], S[p, cols], D[p, cols]
    with np.errstate(divide="ignore", invalid="ignore"):
        implied = (1.0 + r[p, cols]) / (1.0 + stored_r)  # the factor the stored file's move is off by
    # stored unit changes for split_events.csv: the vote's rule, and the known break days
    stored_unit = vote["unit"] | (break_day & has[ST] & np.array([near_split_factor(v) is not None for v in implied]))

    # R5: cut filler after the last session with volume > 0 (a missing volume is not filler), then
    # trailing repeats of the last real close with volume below 1% of the 50-row median before them
    # (Tiingo's SPLK 2024-03-18..22: volumes 0, 90, 47, ...)
    # (each segment's own end too: the old shares' last trade before a relist junction); the new shares'
    # placeholder rows before their first trade are cut first (``leading``)
    keep = has_vendor & (primary >= 0)  # never the successor's anchor rows (``has_vendor`` leaves them out)
    leading_cut = [str(grid[k].date()) for k in np.flatnonzero(keep & leading)]
    keep &= ~leading
    # a documented halt (HALTED_SPANS): its listed zero-volume rows are kept (R4), not cut as filler (R5)
    halted = np.zeros(n, dtype=bool)
    halt_documented = np.zeros(n, dtype=bool)
    halt_entry = HALTED_SPANS.get(sid)
    if halt_entry:
        halted = (grid >= pd.Timestamp(halt_entry["start"])) & listed
        halt_documented = halted & (grid <= pd.Timestamp(halt_entry.get("documented_to") or halt_entry["start"]))
    filler_cut, filler_tiny = 0, np.zeros(0, dtype=int)
    for s in np.unique(seg[keep]):
        part = keep & (seg == s)
        traded = np.flatnonzero(part & ~(Vp == 0))
        if len(traded):
            tail = part.copy()
            tail[: traded[-1] + 1] = False
            tail &= ~halted
            filler_cut += int(tail.sum())
            keep &= ~tail
        tiny = tiny_volume_tail(Cp, Vp, keep & (seg == s))
        tiny = tiny[~halted[tiny]] if len(tiny) else tiny
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
    # the successor's first row (SUCCESSOR_LINKS): when it continues the predecessor (a 1:1 reorganisation, the
    # owner's convention) its return runs from the predecessor's last close (the anchor: the day's own source,
    # else another vendor's close that session, ``cross_source_return``); otherwise it starts a new series
    gap_rows = np.zeros(n, dtype=int)
    gap_rows[idx] = gap
    link_first = np.zeros(n, dtype=bool)
    if link_cut and len(idx):
        k0 = int(idx[0])
        link_first[k0] = True
        if ctx.get("link_continues") and k_anchor >= 0:
            gap_rows[k0] = k0 - k_anchor - 1
            if not own[k0]:
                for i in [int(p[k0])] + [i for i in range(3) if i != p[k0]]:
                    if i < 3 and has[i, k_anchor]:
                        tr[k0] = total_return(Cp[k0], Sp[k0], Dp[k0], C[i, k_anchor])
                        cross[k0] = i != p[k0]
                        break
        else:
            tr[k0] = np.nan
            if not ctx.get("link_continues"):
                # a new series: a split or cash a vendor books on its first row is the predecessor's conversion (ISBC's
                # 2.55, Yahoo's 0.602 for UNIT), which the predecessor's terminal value holds: not this series' event
                if (np.abs(S[:3, k0] - 1.0) > 1e-9).any() or (D[:3, k0] > 0).any():
                    result["summary"]["link_first_row_event_dropped"] = {
                        "date": str(grid[k0].date()), "split": float(Sp[k0]), "cash": float(Dp[k0])}
                S[:, k0], D[:, k0], Sp[k0], Dp[k0] = 1.0, 0.0, 1.0, 0.0
    # plan R9: no return across a gap of more than GAP_RETURN_MAX sessions (Frontier 2018-03-01 across a 213-session
    # WIKI gap with a 1:15 reverse split in it; 2021-05-04 across the bankruptcy; Vroom 2025-02-20)
    gap_blank = np.zeros(n, dtype=bool)
    gap_blank[idx] = (gap_rows[idx] > GAP_RETURN_MAX) & np.isfinite(tr[idx])
    tr[gap_blank] = np.nan
    # a listing's first traded row priced against a quote: from the listing's start (or a segment's) to its first row
    # with a trade (vendor volume > 0) every row is untraded, the row before it is untraded and carries the same close
    # as every kept row in the step-6 staleness span before it (pf.CLOSE_STALE_SESSIONS sessions), in which no vendor
    # row traded: the close it is measured against is a carried or reference price, not a trade (THRY 2020-10-01,
    # +88.8% against 480 identical zero-volume Yahoo closes; an IPO whose file starts with a zero-volume row). Its tr
    # is blank (``listing_start_after_quote``), as at a relist junction. The untraded rows themselves keep their tr
    # (Yahoo files whose volume is 0 every day while the close moves are prices, not quotes)
    quote_start = np.zeros(n, dtype=bool)
    # listing starts the grid shows (a listed session after an unlisted one; a listing older than the grid's first
    # session shows none, so a series that merely begins at the window start is no listing start)
    starts = np.flatnonzero(listed[1:] & ~listed[:-1]) + 1 if n > 1 else np.zeros(0, dtype=int)
    traded = keep & ~no_trade
    for s0 in starts:
        later = idx[(idx >= s0) & traded[idx]]
        if not len(later):
            continue
        k = int(later[0])
        if not listed[idx[(idx >= s0) & (idx <= k)]].all():
            continue  # the listing ended again before its first trade
        position = int(np.searchsorted(idx, k))
        j = int(idx[position - 1]) if position > 0 else -1
        if j < 0 or not no_trade[j] or seg[k] != seg[j] or not np.isfinite(tr[k]):
            continue
        span = slice(max(0, k - pf.CLOSE_STALE_SESSIONS), k)
        before = np.flatnonzero(keep[span]) + span.start
        if no_trade[span].all() and (Cp[before] == Cp[j]).all():
            quote_start[k] = True
    tr[quote_start] = np.nan
    # the hand review's S/D verdicts (``ctx["review"]["events"]``, review_event_plan): the day's S and/or D are set to
    # the reviewed values on the kept row and tr is recomputed against the same prior close (so a cross-source or
    # gap return keeps its basis); a blank tr stays blank. An item whose day has no kept row (or, with ``source``, a
    # row from another source) is unused and its queue row stays open
    review_event_item = np.full(n, "", dtype=object)
    review_events_applied, review_events_unused = [], []
    kept_at = {day_str[k]: k for k in idx}
    for item in review.get("events", ()):
        k = kept_at.get(item["date"])
        if k is None or (item.get("source") and SRC[p[k]] != item["source"]):
            review_events_unused.append(item["item_id"])
            continue
        s_new = Sp[k] if not np.isfinite(_review_num(item.get("split"))) else float(item["split"])
        d_new = Dp[k] if not np.isfinite(_review_num(item.get("cash"))) else float(item["cash"])
        if item.get("mode") == DROP_DOUBLE_CASH:
            # a confirmed ratio-only verdict: a row with a factor and cash drops the cash (the double count)
            s_new, d_new = Sp[k], (0.0 if Sp[k] != 1.0 and Dp[k] > 0 else Dp[k])
        old = (float(Sp[k]), float(Dp[k]), float(tr[k]))
        if np.isfinite(tr[k]) and 1.0 + tr[k] != 0.0:
            prior_close = (Cp[k] * Sp[k] + Dp[k]) / (1.0 + tr[k])
            tr[k] = total_return(Cp[k], s_new, d_new, prior_close)
        Sp[k], Dp[k] = s_new, d_new
        scale = _review_num(item.get("scale"))
        if np.isfinite(scale) and scale > 0:
            Cp[k], Dp[k] = Cp[k] * scale, Dp[k] * scale  # restated as traded: tr is unchanged
        review_event_item[k] = "+".join(x for x in (review_event_item[k], item["item_id"]) if x)
        review_events_applied.append({"item_id": item["item_id"], "date": item["date"],
                                      "split": f"{old[0]:.6g}>{s_new:.6g}", "cash": f"{old[1]:.6g}>{d_new:.6g}",
                                      "tr": (f"{old[2]:+.4f}>{tr[k]:+.4f}" if np.isfinite(old[2]) else "blank")})
    n_sources =np.where(quote_start | gap_blank | (link_first & ~np.isfinite(tr)), 0, choice["n_valid"])
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
    # R7 for the queue: against the vote's primary (the hand review answers the vote's level runs)
    Cp_vote = C[p_vote, cols]
    with np.errstate(divide="ignore", invalid="ignore"):
        level_vote = np.abs(C[:3] / Cp_vote[None, :] - 1.0)
    level_off_vote = has[:3] & (np.nan_to_num(level_vote, nan=0.0) > TOL_LEVEL)
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
        if review_pick[k] >= 0:
            t.append(f"review_source:{SRC[p[k]]}")  # the hand review's vendor_error verdict chose this day's source
            if unresolved_vote[k]:
                t.append("disagree_reviewed")
        elif review_flag[k]:
            t.append("review_vendor_error")  # a reviewed vendor error with no other vendor to use: the row stays
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
        if halted[k]:
            t.append("halt")  # HALTED_SPANS: a documented halt kept, not cut as filler
            if not halt_documented[k]:
                t.append("halt_end_undocumented")  # after the last day a document shows the halt: R4 stays open
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
        if p[k] == Y and "raw_yahoo_fill" in RF[Y, k].split():
            t.append("raw_yahoo_fill")  # a successor's first rows from the ticker's cached raw chart (SUCCESSOR_LINKS)
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
        if quote_start[k]:
            t.append("listing_start_after_quote")  # tr is blank: no trade before it to measure against
        if relist_jump[k]:
            t.append("relist_jump")  # R9: a 10x level change around an unreviewed relisting
        if gap_blank[k]:
            t.append("gap_return_blank")  # R9: no return across a gap of more than GAP_RETURN_MAX sessions
        if link_first[k]:
            t.append(f"{'successor_link' if ctx.get('link_continues') else 'successor_of'}:{ctx.get('link_predecessor', '')}")
            if ctx.get("link_continues") and gap_rows[k] > 0:
                t.append(f"gap_before:{int(gap_rows[k])}")
        if str(grid[k].date()) in terminal_div_days:
            t.append("div_in_terminal_value")  # a vendor's booking of a closing special dividend, dropped
        if review_event_item[k]:
            t.append(f"review_event:{review_event_item[k]}")  # the hand review's S/D verdict set this row's S or D
        if (sid, day_str[k]) in DOUBTFUL_PRICE_DAYS:
            t.append("doubtful_price")  # the hand review calls the close doubtful: its queue rows stay open

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
                 "relists": ctx.get("relists", ()), "Cp_vote": Cp_vote, "p_vote": p_vote, "halted": halted,
                 "halt_documented": halt_documented,
                 "halt_entry": halt_entry or {}}
    result["events"] = split_events_of(sid, ctx_local, ticker_of)
    result["specials"] = specials_of(sid, ctx_local, ticker_of)
    # R1c: an ex-date whose ratio no second source confirms, with |tr| >= 10% resting on it (LGND 2022-11-02)
    day_of = {d: k for k, d in enumerate(grid.strftime("%Y-%m-%d"))}
    single_ratio = {day_of[e["ex_date"]]: e for e in result["events"]
                    if e["event_type"] != "unit_break" and e["in_canonical"]
                    and len([s for s in e["sources_confirming"].split("+") if s]) < 2}
    result["moves"] = moves_of(sid, ctx_local, ticker_of, choice={**choice, "unresolved": unresolved_vote},
                               move_2x=move_2x, move_big=move_big,
                               hidden=hidden, flat=flat, zero_vol=zero_vol, level_off=level_off_vote, gap=gap,
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
        result["summary"]["junction_leading_zero_volume_cut"] = leading_cut
    if relist_jump.any():
        result["summary"]["relist_jumps"] = [str(grid[k].date()) for k in np.flatnonzero(relist_jump)]
    if quote_start.any():
        result["summary"]["listing_starts_after_quote"] = [str(grid[k].date()) for k in np.flatnonzero(quote_start)]
    if gap_blank.any():
        result["summary"]["gap_returns_blank"] = [f"{grid[k].date()} (gap {int(gap_rows[k])})" for k in np.flatnonzero(gap_blank)]
    if terminal_div_dropped:
        result["summary"]["terminal_dividends_dropped"] = terminal_div_dropped
    if review.get("source") or review.get("flag"):
        kept_pick = keep & (review_pick >= 0)
        result["summary"]["review_sources"] = {
            "days": int(kept_pick.sum()), "changed": int((kept_pick & (p != p_vote)).sum()),
            "unresolved_resolved": int((kept_pick & unresolved_vote).sum()),
            "flagged": int((keep & review_flag & (review_pick < 0)).sum()), "unused_items": review_unused,
            "changed_days": [f"{grid[k].date()} {SRC[p_vote[k]]}>{SRC[p[k]]}" for k in
                             np.flatnonzero(kept_pick & (p != p_vote))][:LIST_REVIEW_DAYS]}
    if review.get("events"):
        result["summary"]["review_events"] = {"applied": review_events_applied, "unused": review_events_unused}
    if link_cut:
        k0 = int(idx[0]) if len(idx) else -1
        result["summary"]["successor_link"] = {
            "predecessor": ctx.get("link_predecessor", ""), "predecessor_last_session": link_cut,
            "continues": bool(ctx.get("link_continues")), "anchor_sources": [SRC[i] for i in range(4) if k_anchor >= 0
                                                                              and has[i, k_anchor]],
            "first_row": str(grid[k0].date()) if k0 >= 0 else "",
            "first_row_return": "measured" if k0 >= 0 and np.isfinite(tr[k0]) else "blank",
            "sessions_between": int(gap_rows[k0]) if k0 >= 0 and ctx.get("link_continues") and k_anchor >= 0 else ""}
    # the cash dividends the series books (CACHE/dividends.csv): as paid on the ex-date, from the day's source, and
    # the vendors that carry the same amount that day (within $0.001)
    dividends = []
    for k in idx[Dp[idx] > 0]:
        same = [SRC[i] for i in range(3) if has[i, k] and abs(D[i, k] - Dp[k]) <= 0.001]
        other = [f"{SRC[i]}:{D[i, k]:.6g}" for i in range(3) if has[i, k] and abs(D[i, k] - Dp[k]) > 0.001]
        dividends.append({"ex_date": str(grid[k].date()), "cash_as_paid": float(Dp[k]), "sources": "+".join(same),
                          "ticker": ticker_of(grid[k]), "src_primary": SRC[p[k]], "other_amounts": " ".join(other),
                          "special": "Y" if "special_div" in tokens[k] else ""})
    result["dividends"] = dividends
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

    halted = x.get("halted", np.zeros(n, dtype=bool))
    halt_documented = x.get("halt_documented", halted)
    halt_entry = x.get("halt_entry") or {}

    def entry(k, rule, note, agreeing_sources=None):
        sources = agreeing_sources if agreeing_sources is not None else \
            "+".join(SRC[i] for i in range(4) if agreeing[i, k])
        out.append({"ticker": ticker_of(grid[k]), "event_date": str(grid[k].date()), "classification": "unreviewed",
                    "source_url": "", "verified_at": "", "notes": f"[{rule}] {note}", "security_id": sid,
                    "sources_agreeing": sources, "rule": rule, "listed": bool(x["listed"][k])})

    def settle_halt(days):  # an R4 run inside a documented halt (HALTED_SPANS): settled by its evidence
        if halt_entry and halted[days].any():
            if halt_documented[days][halted[days]].all():  # days before the start (the last close) do not count
                out[-1].update({"classification": halt_entry["classification"], "source_url": halt_entry["url"],
                                "verified_at": HALTED_REVIEWED_AT})
                out[-1]["notes"] += f" | HALTED_SPANS (evidence: {halt_entry['evidence']}): {halt_entry['note']}"
            else:
                out[-1]["notes"] += (f" | HALTED_SPANS: halted from {halt_entry['start']}, but no document shows the "
                                     f"halt after {halt_entry.get('documented_to', halt_entry['start'])}: open")

    def halt_parts(days):  # an R4 run split at the halt's last documented day (each part its own entry)
        undocumented = halted[days] & ~halt_documented[days]
        if not (halt_entry and halt_documented[days].any() and undocumented.any()):
            return [days]
        cut = int(np.flatnonzero(halt_documented[days])[-1]) + 1
        return [days[:cut], days[cut:]]

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
        # dates and the ratio only: reviewed_moves.csv is committed, and vendor price levels stay local
        # (moves_all.csv and the canonical file have the closes)
        entry(k, "R9", f"raw level change {x['Cp'][k] * x['Sp'][k] / x['Cp'][before]:.3g}x from "
                       f"{grid[before].date()} to {grid[k].date()} with no split, around a relisting ({cuts}): "
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
        for part in halt_parts(days):
            entry(int(part[0]), "R4", f"{len(part)} identical raw closes to {grid[part[-1]].date()}" +
                  ("; zero volume" if zero else "") + ("; no second source" if not confirmed else ""),
                  "+".join(confirmed))
            settle_halt(part)
    for s0, s1 in run_lengths(zero_vol & ~flat):
        for part in halt_parts(np.arange(s0, s1 + 1)):
            entry(int(part[0]), "R4", f"zero volume on {len(part)} session(s) to {grid[part[-1]].date()}", "")
            settle_halt(part)
    if len(idx):
        inside = np.zeros(len(grid), dtype=bool)
        inside[idx[0]: idx[-1] + 1] = True
        missing = inside & ~kept & x["listed"]
        for s0, s1 in run_lengths(missing):
            entry(s0, "R6", f"{s1 - s0 + 1} listed session(s) with no vendor row, to {grid[s1].date()}", "")
    # R7 against the vote's primary (``Cp_vote``/``p_vote``; the same as the canonical's without a reviewed source)
    Cv, pv = x.get("Cp_vote", x["Cp"]), x.get("p_vote", x["p"])
    for i in range(3):
        for s0, s1 in run_lengths(level_off[i] & kept):
            days = idx[(idx >= s0) & (idx <= s1)]
            ratio = np.nanmedian(C[i, days] / Cv[days])
            if len(days) < 3 and abs(ratio - 1) < 0.02:
                continue  # a day or two 1-2% apart: flagged only
            entry(s0, "R7", f"{SRC[i]} raw close {ratio:.4f}x of {SRC[pv[s0]]} on {len(days)} session(s) "
                            f"to {grid[s1].date()}", SRC[pv[s0]])
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
        supersede_price_file(path)  # a series that is empty now: moved aside, never deleted
    days = pd.to_datetime(canonical["date"]).values.astype("datetime64[D]").astype(np.int32) if len(canonical) else \
        np.zeros(0, dtype=np.int32)
    state = {**result, "signature": signature, "dates": days, "built_utc": now_utc()}
    common.atomic_write(state_path(sid), pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL))
    return state


def superseded_dir() -> Path:
    """``reconcile/superseded_prices_{UTC date}/``: where price files that are no longer this step's output go."""
    return OUT / f"superseded_prices_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"


def supersede_price_file(path: Path) -> Path:
    """Move ``path`` into ``superseded_dir()`` (a later copy of the same name gets a numbered suffix): data files
    are never deleted."""
    target_dir = superseded_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    n = 1
    while target.exists():
        target = target_dir / f"{path.stem}.{n}{path.suffix}"
        n += 1
    os.replace(path, target)
    return target


def supersede_non_targets(ids: list[str]) -> list[dict]:
    """``--rebuild``: the per-security price files in PRICES_DIR of securities that are no longer targets (the six
    D6 business development companies of round 8: PSEC, ARCC, FSC, OXLC, GSVC, ACAS) are moved to
    ``superseded_dir()``, so CACHE/prices holds exactly the panel's securities."""
    wanted = set(ids)
    moved = []
    for path in sorted(PRICES_DIR.glob("*.csv")):
        if path.stem in wanted:
            continue
        target = supersede_price_file(path)
        moved.append({"security_id": path.stem, "moved_to": str(target)})
    return moved


def junctions_of(sid: str) -> list[str]:
    """The first sessions of new shares after a relisting (RELIST_JUNCTIONS)."""
    entry = RELIST_JUNCTIONS.get(sid)
    return [entry["first_new_session"]] if entry else []


def junction_effective_of(sid: str) -> dict[str, str]:
    """first_new_session -> the plan's effective date (the new shares issued), where the entry gives one."""
    entry = RELIST_JUNCTIONS.get(sid)
    return {entry["first_new_session"]: entry["effective_date"]} if entry and entry.get("effective_date") else {}


_TERMINAL_DIVIDENDS: dict[str, list[dict]] | None = None


def terminal_owned_dividends() -> dict[str, list[dict]]:
    """security -> the special dividends paid to holders at a merger closing that the terminal value owns: the
    terminal step's REVIEWED entries with ``special_dividend`` (and not ``special_dividend_before_last_trade``),
    {amount, record (record date, may be blank), url}. Rule (round 9, recorded in summary.json): such a dividend
    is booked once, in the terminal value (the last close still carries it), never in the series."""
    global _TERMINAL_DIVIDENDS
    if _TERMINAL_DIVIDENDS is None:
        from scripts import reversal_data_terminal as terminal  # imported here: terminal imports this module
        out = {}
        for sid, entry in terminal.REVIEWED.items():
            if entry.get("special_dividend") is None or entry.get("special_dividend_before_last_trade"):
                continue
            out[sid] = [{"amount": float(entry["special_dividend"]), "record": entry.get("special_dividend_record") or "",
                         "url": entry.get("url", "")}]
        _TERMINAL_DIVIDENDS = out
    return _TERMINAL_DIVIDENDS


def successor_of() -> dict[str, str]:
    """successor -> predecessor for SUCCESSOR_LINKS (a successor with two predecessors is not expected); a link
    with ``handover`` False (a class folded into another existing class) gives the other class no predecessor."""
    return {link["successor"]: pred for pred, link in SUCCESSOR_LINKS.items()
            if link.get("successor") and link.get("handover", True)}


PER_SECURITY_FILE_PREFIXES = ("tiingo_run/", "yahoo/")  # the step-8 Tiingo and step-7 Yahoo files, one per security
RAW_YAHOO_DIR = common.RAW / "yahoo"  # step 7's cached raw v8 charts, ``{TICKER}__{stamp}.json.gz`` (read only)


def successor_start(link: dict, sessions: pd.DatetimeIndex | None = None) -> pd.Timestamp:
    """The successor's first session at a link: ``successor_first_session`` when the entry gives one (a day it
    shares with the predecessor's last), else the session after the predecessor's last (the next calendar day when
    no session calendar is given; the grid drops non-sessions anyway)."""
    if link.get("successor_first_session"):
        return pd.Timestamp(link["successor_first_session"])
    cut = pd.Timestamp(link["last_session"])
    if sessions is not None:
        later = sessions[sessions > cut]
        if len(later):
            return later[0]
    return cut + pd.Timedelta(days=1)


def raw_yahoo_rows(ticker: str) -> pd.DataFrame:
    """The latest cached raw v8 chart of ``ticker`` (step 7's raw cache), restored like the old Yahoo caches."""
    paths = sorted(RAW_YAHOO_DIR.glob(f"{ticker}__*.json.gz")) if RAW_YAHOO_DIR.exists() else []
    if not paths:
        return _empty_rows()
    path = paths[-1]
    result = json.loads(gzip.decompress(path.read_bytes()))["chart"]["result"][0]
    rows = yahoo_restore(result)
    rows["file"] = f"raw_yahoo/{path.name}"
    return rows


def link_frames(sid: str, frames: dict[str, pd.DataFrame], bundles, pred: str,
                sessions: pd.DatetimeIndex | None = None) -> tuple[dict[str, pd.DataFrame], dict]:
    """The successor's frames at a SUCCESSOR_LINKS link: its own rows from its first session (``successor_start``),
    the predecessor's rows from then on that the successor lacks (the same ticker's later rows, which the ticker map
    gave the predecessor; for a cut-only link the ticker-mapped ones only, unless ``handover_files``), and, when the
    link continues, the predecessor's last row on or before its last session in each source (the anchor of the
    first return; the successor's own row there when the predecessor has none). The successor's own rows before
    its first session are the predecessor's history under the ticker and are dropped. With ``raw_yahoo_fill`` the
    successor's missing rows from its first session to its first step-7 Yahoo row come from the ticker's cached raw
    chart (flag ``raw_yahoo_fill``). Step 7 marks the first row of a file it trimmed (``yahoo_junction``: no Yahoo
    return); when rows handed over or filled here come before that row, the trim is undone, so the mark is
    cleared (MRVL 2021-04-27 after the predecessor's rows to 2021-04-26)."""
    link = SUCCESSOR_LINKS[pred]
    cut = pd.Timestamp(link["last_session"])
    start = successor_start(link, sessions)
    theirs_all = frames_for(pred, bundles)
    out, facts = {}, {"own_rows_dropped": 0, "predecessor_rows_added": 0, "anchor_sources": [],
                      "first_session": str(start.date())}
    for name in SRC:
        own, theirs = frames.get(name), theirs_all.get(name)
        parts = []
        mine = own[own["date"] >= start] if own is not None else None
        if own is not None:
            facts["own_rows_dropped"] += int((own["date"] < start).sum())
        if mine is not None and len(mine):
            parts.append(mine)
        if theirs is not None:
            extra = theirs[theirs["date"] >= start]
            per_file = extra["file"].astype(str).str.startswith(PER_SECURITY_FILE_PREFIXES)
            if not link["continues"] and not link.get("handover_files"):
                # a cut-only link hands over only the ticker-mapped rows (the ticker's later rows in WIKI, the old
                # caches and the stored files: ANGI Homeservices from 2017-10-02); a file fetched for the predecessor
                # itself may carry its own filler after its last session (21CF's Tiingo row of 2019-03-20)
                extra = extra[~per_file]
            elif start <= cut:
                extra = extra[~(per_file & (extra["date"] <= cut))]  # the predecessor's own rows on a shared day
            if mine is not None:
                extra = extra[~extra["date"].isin(mine["date"])]
            if len(extra):
                parts.append(extra.assign(security_id=sid))
                facts["predecessor_rows_added"] += int(len(extra))
        if link["continues"]:
            # the row on the last session itself (the predecessor's, else the successor's own file's), else the
            # latest earlier one (a thin series: LBTYB's last trade 2013-06-06)
            anchor = None
            for pool in (theirs, own):
                if pool is not None and (pool["date"] == cut).any():
                    anchor = pool[pool["date"] == cut].tail(1)
                    break
            if anchor is None:
                earlier = [f[f["date"] <= cut].tail(1) for f in (theirs, own) if f is not None and (f["date"] <= cut).any()]
                anchor = max(earlier, key=lambda f: f["date"].iloc[0]) if earlier else None
            if anchor is not None and len(anchor):
                parts.append(anchor.assign(security_id=sid))
                facts["anchor_sources"].append(name)
        if name == "yahoo" and link.get("raw_yahoo_fill"):
            raw = raw_yahoo_rows(link["raw_yahoo_fill"])
            own_first = mine["date"].min() if mine is not None and len(mine) else pd.Timestamp.max
            have = set(pd.concat(parts)["date"]) if parts else set()
            fill = raw[(raw["date"] >= start) & (raw["date"] < own_first) & ~raw["date"].isin(have)]
            if len(fill):
                parts.append(fill.assign(security_id=sid, rowflag="raw_yahoo_fill"))
            facts["raw_yahoo_fill_rows"] = int(len(fill))
            facts["raw_yahoo_fill_file"] = str(raw["file"].iloc[0]) if len(raw) else ""
        if parts:
            out[name] = pd.concat(parts, ignore_index=True).sort_values("date", kind="stable").reset_index(drop=True)
    # step 7's trim mark on the successor's own first Yahoo row, when rows of the link come before it
    y = out.get("yahoo")
    own_y = frames.get("yahoo")
    if y is not None and own_y is not None and len(own_y):
        own_y = own_y[own_y["date"] >= start]
        if len(own_y):
            first = own_y["date"].min()
            before = [f[(f["date"] >= start) & (f["date"] < first)] for f in out.values()]
            hit = y.index[(y["date"] == first) & y["rowflag"].fillna("").str.contains("yahoo_junction")]
            if len(hit) and any(len(b) for b in before):
                y.loc[hit, "rowflag"] = [" ".join(t for t in str(v).split() if t != "yahoo_junction") for v in y.loc[hit, "rowflag"]]
                facts["yahoo_junction_cleared"] = str(first.date())
    return out, facts


def predecessor_frames(frames: dict[str, pd.DataFrame], link: dict, successor_own: dict[str, pd.DataFrame] | None = None,
                       sid: str = "") -> tuple[dict[str, pd.DataFrame], dict]:
    """A SUCCESSOR_LINKS predecessor's frames: cut at its last session (its later rows are its successor's). When the
    successor trades on that session under the ticker (``successor_first_session``: Fox on 2019-03-19, while 21CF
    traded that day as TFCFA), the ticker-mapped rows from that day are the successor's too; the predecessor keeps
    its own per-security files' rows (21CF's Tiingo row of 2019-03-19). When the link continues, the successor's own
    rows after the predecessor's last row and up to the last session (its files carry the ticker's history, the
    same security's) are given back to the predecessor (Zillow: its own rows stop at 2014-11-20, Zillow Group's
    Yahoo file has the days to 2015-02-17), so ``link_frames`` drops no day that neither series would have; only
    that tail gap is filled (the predecessor's earlier history keeps its own sources)."""
    cut = pd.Timestamp(link["last_session"])
    facts = {"rows_after_last_session_cut": int(sum((f["date"] > cut).sum() for f in frames.values()))}
    out = {name: f[f["date"] <= cut].reset_index(drop=True) for name, f in frames.items()}
    if link.get("continues") and successor_own:
        given = 0
        last_own = max((f["date"].max() for f in out.values() if len(f)), default=pd.Timestamp("1900-01-01"))
        for name, theirs in successor_own.items():
            if theirs is None or not len(theirs):
                continue
            back = theirs[(theirs["date"] > last_own) & (theirs["date"] <= cut)]
            if len(back):
                parts = [f for f in (out.get(name), back.assign(security_id=sid)) if f is not None and len(f)]
                out[name] = pd.concat(parts, ignore_index=True).sort_values("date", kind="stable").reset_index(drop=True)
                given += int(len(back))
        facts["successor_rows_given_back"] = given
    shared = link.get("successor_first_session", "")
    if shared and pd.Timestamp(shared) <= cut and link.get("handover", True):
        def mapped_late(f):
            return (f["date"] >= pd.Timestamp(shared)) & ~f["file"].astype(str).str.startswith(PER_SECURITY_FILE_PREFIXES)
        facts["ticker_rows_on_shared_day_given_to_successor"] = int(sum(mapped_late(f).sum() for f in out.values()))
        out = {name: f[~mapped_late(f)].reset_index(drop=True) for name, f in out.items()}
    return out, facts


def run_securities(ids: list[str], bundles, identity, windows, sessions, rebuild: bool = False) -> dict[str, dict]:
    mapping, master = identity["mapping"], identity["master"]
    relists = identity.get("relisted", {})
    first_ticker = dict(zip(master["security_id"], master["first_ticker"]))
    predecessor_of = successor_of()
    dividends_owned = terminal_owned_dividends()
    reviewed = review_overrides()
    states, built, reused = {}, 0, 0
    started = time.time()
    for n, sid in enumerate(ids, 1):
        frames = frames_for(sid, bundles)
        link_facts = {}
        pred = predecessor_of.get(sid, "")
        link = SUCCESSOR_LINKS.get(pred) if pred else None
        if link:  # a successor: the predecessor's rows after its last session, and the anchor
            frames, facts = link_frames(sid, frames, bundles, pred, sessions)
            link_facts.update(facts)
        own_link = SUCCESSOR_LINKS.get(sid, {})
        cut_at = own_link.get("last_session", "")
        if cut_at:  # a predecessor (also in a chain: Avago -> Broadcom Ltd -> Broadcom Inc.): its later rows are its successor's
            succ_own = frames_for(own_link["successor"], bundles) if own_link.get("continues") else None
            frames, facts = predecessor_frames(frames, own_link, succ_own, sid)
            link_facts.update(facts)
        dates = [f["date"] for f in frames.values() if len(f)]
        window = windows.get(sid)
        if window is None and not link:
            # a target with no listing interval at all (step 4 removed the only one: Ford 37996, a NYSE stock):
            # no series is built from its ticker's vendor rows (they would all lie outside any listing)
            state = {"signature": "no_listing_interval", "summary": {"security_id": sid, "rows": 0,
                                                                      "no_listing_interval": True},
                     "events": [], "specials": [], "moves": [], "pairs": []}
            states[sid] = save_result(sid, {**state, "canonical": pd.DataFrame(columns=PRICE_COLUMNS)},
                                      "no_listing_interval")
            built += 1
            continue
        if window is None:
            if not dates:
                window = (pd.Timestamp(WINDOW_START), pd.Timestamp(WINDOW_START))
            else:
                window = (min(d.min() for d in dates), max(d.max() for d in dates))
        if link:  # the anchor (the predecessor's last session) and the handed-over rows lie inside the window
            window = (min(window[0], pd.Timestamp(link["last_session"])), window[1])
        if cut_at:  # a predecessor is listed up to its reviewed last session (the closing document dates it)
            window = (window[0], max(window[1], pd.Timestamp(cut_at)))
        owned = dividends_owned.get(sid, [])
        review = reviewed.get(sid, {})
        signature = signature_of(sid, frames, window, mapping, json.dumps([junctions_of(sid), junction_effective_of(sid),
                                                                           relists.get(sid, []), cut_at, pred,
                                                                           link or {}, owned]
                                                                          + ([review] if review else [])))
        state = None if rebuild else load_state(sid)
        if state is not None and state.get("signature") == signature and \
                (state["summary"].get("rows", 0) == 0 or (PRICES_DIR / f"{sid}.csv").exists()):
            states[sid] = state
            reused += 1
        else:
            lookup = ticker_lookup(mapping, sid)
            fallback = first_ticker.get(sid, "")
            listed_from = None
            if link:  # listed from its first session (the session after the predecessor's last one: the ticker passed)
                listed_from = successor_start(link, sessions)
            until = pd.Timestamp(cut_at) if cut_at else None  # a predecessor: listed to its reviewed last session
            ctx = {"sessions": sessions, "window": window,
                   "listed": (lambda grid, sid=sid, since=listed_from, until=until: listed_mask(mapping, sid, grid) |
                              ((grid >= since) & (grid < first_listed(mapping, sid)) if since is not None else False) |
                              ((grid > last_listed(mapping, sid)) & (grid <= until) if until is not None else False)),
                   "ticker_of": (lambda day, lookup=lookup, fallback=fallback: lookup(day) or fallback),
                   "junctions": junctions_of(sid), "junction_effective": junction_effective_of(sid),
                   "relists": [] if sid in RELIST_JUNCTIONS else relists.get(sid, []),
                   "terminal_dividends": owned, "review": review}
            if link:
                # ``link_cut``: the last session before the successor's first one (the predecessor's last session,
                # or the day before a shared first session: Fox 2019-03-18); rows up to it are only the anchor
                ctx.update({"link_cut": previous_session_of(str(successor_start(link, sessions).date()), sessions),
                            "link_continues": bool(link["continues"]), "link_predecessor": pred})
            result = reconcile_security(sid, frames, ctx)
            result["summary"].update(tiingo_self_check(sid, bundles))
            if link_facts:
                result["summary"]["successor_link_rows"] = link_facts
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
    facts["hand_review"] = apply_event_verdicts(frame, "split_events", review_event_status(states))
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


def event_verdict_note(v: dict, queue: str) -> str:
    """The short note a split/distribution verdict adds to the table row (the full note is in the merged file)."""
    terms = []
    if queue == "splits":
        if v["verdict"] == "corrected" and v["split_factor"]:
            terms.append(f"factor {float(v['split_factor']):.6g}")
    else:
        terms += [v["distribution_type"]] if v["distribution_type"] else []
        terms += [f"ratio {float(v['ratio']):.6g}"] if v["ratio"] else []
    if v["ex_date_confirmed"] and v["ex_date_confirmed"] != v["ex_date"]:
        terms.append(f"ex-date {v['ex_date_confirmed']}")
    return f"hand review {v['item_id']}: {v['verdict']}" + (f" ({', '.join(terms)})" if terms else "")


def review_event_status(states: dict[str, dict] | None) -> dict[str, dict]:
    """Per verdict item that changes S or D (review_event_plan): {"status", "reason", "change"}. ``applied``: the
    series carries it (the security's summary ``review_events.applied``); ``unused``: planned, but the day has no kept
    row (or a row from another source than the exception names); ``not_applied``: the plan's reason; ``type_only``;
    ``same_as``: closed with the item it names when that one is applied (ZG's moves verdicts), else open."""
    plan = review_event_plan()
    if not len(plan):
        return {}
    applied, unused = {}, set()
    for state in (states or {}).values():
        info = state.get("summary", {}).get("review_events") or {}
        for e in info.get("applied", ()):
            applied.setdefault(e["item_id"], []).append(f"{e['date']} S {e['split']}, {_cash_text(e['cash'])}, "
                                                        f"tr {_tr_text(e['tr'])}")
        unused.update(info.get("unused", ()))
    out = {}
    for r in plan.to_dict("records"):
        item = r["item_id"]
        if r["action"] == "apply":
            if item in applied and item not in unused:
                out[item] = {"status": "applied", "reason": "", "change": "; ".join(applied[item])}
            else:
                out[item] = {"status": "unused", "change": "",
                             "reason": "planned, but the day has no kept row" + (f" from {r['source']}" if r["source"]
                                                                                else "")}
        elif r["action"] == "type_only":
            out[item] = {"status": "type_only", "reason": r["reason"], "change": ""}
        elif r["same_as"]:
            out[item] = {"status": "same_as", "reason": r["reason"], "change": "", "same_as": r["same_as"]}
        else:
            out[item] = {"status": "not_applied", "reason": r["reason"], "change": ""}
    for item, st in out.items():
        if st["status"] == "same_as":
            if out.get(st["same_as"], {}).get("status") == "applied":
                st.update(status="applied", change=f"with {st['same_as']}: {out[st['same_as']]['change']}")
            else:
                st["status"] = "not_applied"
    return out


OPEN_EVENT_STATES = ("not_applied", "unused")


def review_event_table(states: dict[str, dict]) -> tuple[pd.DataFrame, dict]:
    """reconcile/review_event_changes.csv: every verdict that changes S or D, its plan and what the series carries
    (status applied / unused / not_applied / type_only), and the counts for summary.json."""
    plan = review_event_plan()
    status = review_event_status(states)
    if not len(plan):
        return pd.DataFrame(columns=EVENT_PLAN_COLUMNS + ["status", "change"]), {"items": 0}
    frame = plan.copy()
    frame["status"] = [status.get(i, {}).get("status", "") for i in frame["item_id"]]
    frame["change"] = [status.get(i, {}).get("change", "") for i in frame["item_id"]]
    items = frame.drop_duplicates("item_id")
    facts = {"items": int(len(items)), "rows": int(len(frame)),
             "by_queue_status": {q: {k: int(v) for k, v in g["status"].value_counts().items()}
                                 for q, g in items.groupby("queue")},
             "open_items": sorted(items.loc[items["status"].isin(OPEN_EVENT_STATES), "item_id"]),
             "rule": "a verdict that changes S or D is applied to the canonical row of its day when its evidence is an "
                     "SEC document and it states S and D as numbers (REVIEW_EVENT_EXCEPTIONS aside); one not applied "
                     "keeps its queue row open"}
    return frame, facts


DATA_CHANGE_COLUMNS = ["queue", "item_id", "security_id", "ticker", "date", "verdict", "change", "applied", "how",
                       "source_url", "evidence_sources"]


def review_data_changes(event_changes: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """reconcile/review_data_changes.csv: the merge's two data-change lists (merged/moves_data_changes.csv and
    merged/split_data_changes.csv) with ``applied`` and ``how`` written from this build: an item that changes S or D
    (``event_changes``, review_event_changes.csv) is Y when the series carries it (applied, or type_only for a
    retyped event) and N otherwise, with the status and the change or reason; a source choice or a vendor-error
    flag keeps the merge's Y (reconcile source_overrides / flag review_vendor_error). Items the build applies that
    the merge does not list (a confirmed ratio-only verdict over a double-counted vendor row) are added. The merged
    files are left as the merge wrote them."""
    frames = [t for t in (review_table("moves_data_changes.csv"), review_table("split_data_changes.csv"))
              if t is not None and len(t)]
    merged = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=DATA_CHANGE_COLUMNS)
    merged = merged.reindex(columns=DATA_CHANGE_COLUMNS).fillna("").astype(str)
    by_item = {}
    if len(event_changes):
        for r in event_changes.to_dict("records"):
            by_item.setdefault(r["item_id"], []).append(r)
    rows = []
    seen = set()
    for r in merged.to_dict("records"):
        found = by_item.get(r["item_id"])
        if found:
            st = found[0]["status"]
            text = "; ".join(x for x in (found[0]["change"] if st == "applied" else found[0]["reason"],) if x)
            r["applied"] = "Y" if st in ("applied", "type_only") else "N"
            r["how"] = f"reconcile review_event: {st}" + (f" ({text})" if text else "")
            seen.add(r["item_id"])
        rows.append(r)
    for item, found in by_item.items():
        if item in seen:
            continue
        f = found[0]
        st = f["status"]
        text = f["change"] if st == "applied" else f["reason"]
        rows.append({"queue": f["queue"], "item_id": item, "security_id": f["security_id"], "ticker": f["ticker"],
                     "date": f["date"], "verdict": f["verdict"],
                     "change": "confirmed ratio, no cash: a vendor row with both keeps the factor and drops the cash"
                     if f.get("mode") == DROP_DOUBLE_CASH else f"{f['kind']}".strip(),
                     "applied": "Y" if st in ("applied", "type_only") else "N",
                     "how": f"reconcile review_event: {st}" + (f" ({text})" if text else ""),
                     "source_url": f["source_url"], "evidence_sources": ""})
    frame = pd.DataFrame(rows, columns=DATA_CHANGE_COLUMNS)
    facts = {"rows": int(len(frame)), "added_by_build": int(len(frame) - len(merged)),
             "by_queue_applied": {q: {k: int(v) for k, v in g["applied"].value_counts().items()}
                                  for q, g in frame.groupby("queue")} if len(frame) else {}}
    return frame, facts


def _cash_text(change: str) -> str:
    """'a>b' cash change in words (the committed tables carry no vendor amounts that would show a price level)."""
    old, new = (float(x) for x in change.split(">"))
    return "D unchanged" if abs(old - new) < 1e-12 else "D removed" if new == 0 else "D set to the reviewed cash"


def _tr_text(change: str) -> str:
    if change == "blank":
        return "blank"
    old, new = (float(x) for x in change.split(">"))
    return f"{old:+.1%} > {new:+.1%}"


def apply_event_verdicts(frame: pd.DataFrame, table: str, status: dict[str, dict] | None = None) -> dict:
    """Fill sec_url (and verified_at in split_events.csv) from the hand review's merged verdicts, keyed
    (security_id, ex_date): distribution verdicts on both tables, then split verdicts on split_events rows (the
    first verdict to fill a row's sec_url, a distribution verdict where both answer one row, gives it; a later one
    adds only a missing verified_at). ``sec_url`` is the verdict's source_url, or 'evidence: ' and the sources for a
    two-source verdict; an ``unresolved`` verdict fills nothing (the row stays open) and adds its note. A verdict
    that changes S or D (``status``, review_event_status) closes the row only when the series carries it
    (``applied``; its note says what changed) or it only retypes the event (``type_only``: event_type distribution);
    one that is ``not_applied`` or ``unused`` leaves the row open (no sec_url, no verified_at) with the reason in the
    note, and keeps it open whatever another verdict on the same row says. A row confirmed_price_adjustments.csv
    already filled keeps its values. In place; returns counts."""
    status = status or {}
    tables = [("distributions", review_table("distribution_verdicts.csv"))]
    if table == "split_events":
        tables.append(("splits", review_table("split_verdicts.csv")))
    facts = {"rows_matched": 0, "filled": 0, "unresolved": 0, "by_verdict": {}, "verdicts_unmatched": {},
             "series_changed": 0, "type_only": 0, "open_not_applied": 0, "open_items": []}
    if not len(frame):
        return facts
    position = {key: k for k, key in zip(frame.index, zip(frame["security_id"], frame["ex_date"]))}
    open_rows = set()
    for queue, verdicts in tables:
        if verdicts is None or not len(verdicts):
            continue
        for v in verdicts.to_dict("records"):
            k = position.get((v["security_id"], v["ex_date"]))
            if k is not None and status.get(v["item_id"], {}).get("status") in OPEN_EVENT_STATES:
                open_rows.add(k)
    for queue, verdicts in tables:
        if verdicts is None or not len(verdicts):
            continue
        unmatched = 0
        for v in verdicts.to_dict("records"):
            k = position.get((v["security_id"], v["ex_date"]))
            if k is None:
                unmatched += 1
                continue
            facts["rows_matched"] += 1
            facts["by_verdict"][v["verdict"]] = facts["by_verdict"].get(v["verdict"], 0) + 1
            note = event_verdict_note(v, queue)
            st = status.get(v["item_id"], {})
            if st.get("status") in OPEN_EVENT_STATES:
                facts["open_not_applied"] += 1
                facts["open_items"].append(v["item_id"])
                note += f" (not applied to the series: {st['reason']}; open)"
            elif v["verdict"] == "unresolved":
                facts["unresolved"] += 1
            elif k in open_rows:
                note += " (the row stays open: another verdict on it is not applied)"
            else:
                if st.get("status") == "applied":
                    facts["series_changed"] += 1
                    note += f" (applied to the series: {st['change']})"
                elif st.get("status") == "type_only":
                    facts["type_only"] += 1
                    if "event_type" in frame:
                        frame.at[k, "event_type"] = "distribution"
                    note += " (type only: event_type distribution)"
                if not str(frame.at[k, "sec_url"]):
                    evidence = v.get("evidence_sources", "")
                    frame.at[k, "sec_url"] = v["source_url"] or (f"evidence: {evidence}" if evidence else "")
                    if "verified_at" in frame:
                        frame.at[k, "verified_at"] = v["verified_at"]
                    facts["filled"] += 1
                elif queue == "splits" and "verified_at" in frame and not str(frame.at[k, "verified_at"]):
                    frame.at[k, "verified_at"] = v["verified_at"]
            frame.at[k, "notes"] = "; ".join(x for x in (str(frame.at[k, "notes"]), note) if x)
        facts["verdicts_unmatched"][queue] = unmatched
    facts["rows_open_by_verdict"] = int(len(open_rows))
    if frame["notes"].str.contains(LEVEL_IN_NOTE).any():  # the merged notes were checked; this is a code guard
        raise ValueError(f"{table}: a hand-review note carries a price level")
    return facts


def build_special_table(states: dict[str, dict], identity: dict) -> tuple[pd.DataFrame, dict]:
    frame = pd.DataFrame([e for s in states.values() for e in s["specials"]])
    if frame.empty:
        return pd.DataFrame(columns=SPECIAL_COLUMNS), {"rows": 0}
    facts = {"rows_all": int(len(frame)), "outside_listing": int((~frame["listed"]).sum())}
    frame = frame[frame["listed"]].sort_values(["security_id", "ex_date"], kind="stable")
    if "sec_url" not in frame:
        frame["sec_url"] = ""
    facts["hand_review"] = apply_event_verdicts(frame, "special_distributions", review_event_status(states))
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


def apply_move_verdicts(frame: pd.DataFrame, status: dict[str, dict] | None = None) -> dict:
    """The hand review's merged moves verdicts (moves_verdicts.csv), keyed (security_id, event_date, rule): a
    verdict sets classification, source_url and verified_at and adds a short note (classification, item, and the
    URL or the agreeing sources); an ``unresolved`` verdict leaves the row ``unreviewed`` (open) and adds its item
    to the notes. A row with two queue entries on one key (two vendors' R7 runs) gets the verdict on both. An
    ``unrecorded_event`` verdict (it changes S or D) closes its row only when the series carries it (``status``,
    review_event_status: ``applied``); one that is not applied leaves the row ``unreviewed`` (open) with the reason
    in the notes. In place; returns counts."""
    status = status or {}
    verdicts = review_table("moves_verdicts.csv")
    facts = {"verdicts": 0, "rows_matched": 0, "applied": 0, "unresolved": 0, "verdicts_unmatched": 0,
             "by_classification": {}, "open_not_applied": 0, "open_items": [], "series_changed": 0}
    if verdicts is None or not len(verdicts) or not len(frame):
        return facts
    facts["verdicts"] = int(len(verdicts))
    by_key = {}
    for v in verdicts.to_dict("records"):
        by_key.setdefault((v["security_id"], v["event_date"], v["rule"]), []).append(v)
    matched = set()
    for k in frame.index:
        key = (frame.at[k, "security_id"], frame.at[k, "event_date"], frame.at[k, "rule"])
        found = by_key.get(key)
        if not found:
            continue
        matched.add(key)
        v = found[0]  # one classification per key (the merge rejects a key with two)
        facts["rows_matched"] += 1
        items = "+".join(x["item_id"] for x in found)
        if v["classification"] == "unresolved":
            facts["unresolved"] += 1
            frame.at[k, "notes"] += f" | hand review {items}: unresolved, open (moves_verdicts.csv)"
            continue
        states_of = [status.get(x["item_id"], {}) for x in found]
        doubtful = DOUBTFUL_PRICE_DAYS.get((key[0], key[1]))
        if doubtful:
            changed = [st["change"] for st in states_of if st.get("status") == "applied"]
            facts["open_doubtful_price"] = facts.get("open_doubtful_price", 0) + 1
            facts["open_items"] += [x["item_id"] for x in found]
            frame.at[k, "notes"] += (f" | hand review {items}: {v['classification']}"
                                     + (f", applied to the series ({changed[0]})" if changed else "")
                                     + f"; the close is doubtful ({doubtful}): flag doubtful_price, open until "
                                       "another source or a document gives the close (moves_verdicts.csv)")
            if changed:
                facts["series_changed"] += 1
            continue
        if any(st.get("status") in OPEN_EVENT_STATES for st in states_of):
            reason = next(st["reason"] for st in states_of if st.get("status") in OPEN_EVENT_STATES)
            facts["open_not_applied"] += 1
            facts["open_items"] += [x["item_id"] for x in found]
            frame.at[k, "notes"] += (f" | hand review {items}: {v['classification']}, not applied to the series "
                                     f"({reason}); open (moves_verdicts.csv)")
            continue
        changed = [st["change"] for st in states_of if st.get("status") == "applied"]
        frame.loc[k, ["classification", "source_url", "verified_at"]] = [v["classification"], v["source_url"],
                                                                         v["verified_at"]]
        frame.at[k, "notes"] += " | " + review_merge.move_note({**v, "item_id": items})
        if changed:
            facts["series_changed"] += 1
            frame.at[k, "notes"] += f" (applied to the series: {changed[0]})"
        facts["applied"] += 1
        facts["by_classification"][v["classification"]] = facts["by_classification"].get(v["classification"], 0) + 1
    facts["verdicts_unmatched"] = int(sum(len(v) for key, v in by_key.items() if key not in matched))
    facts["unmatched_items"] = sorted(x["item_id"] for key, v in by_key.items() if key not in matched for x in v)
    if frame["notes"].str.contains(LEVEL_IN_NOTE).any():  # the merge checked the verdicts; a code guard
        raise ValueError("reviewed_moves: a hand-review note carries a price level")
    return facts


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
    # R9 (a 10x level change around an unreviewed relisting: new shares, or a market move) is queued whatever its
    # scope, so a missing junction entry is seen even for a name that never ranked (THRY 2018-04-18 before its
    # entry, SSM 2024-05-28)
    r9_outside = frame["rule"].eq("R9") & ~(frame["listed"] & frame["relevant"])
    frame.loc[r9_outside, "notes"] += " (queued outside the relevant scope: every R9 hit is queued)"
    facts["r9_queued_outside_scope"] = int(r9_outside.sum())
    frame = frame[(frame["listed"] & frame["relevant"]) | frame["rule"].eq("R9")].copy()
    leaking = frame[frame["notes"].str.contains(LEVEL_IN_NOTE)]
    if len(leaking):  # a code bug: the committed table would carry a vendor price level
        raise ValueError(f"queue notes with a price level ({len(leaking)}), e.g. {leaking['notes'].iloc[0][:80]!r}")
    if REVIEWED_FORMAT.exists():
        done = pd.read_csv(REVIEWED_FORMAT, dtype=str, keep_default_na=False)
        known = {(t, d): row for t, d, row in zip(done["ticker"], done["event_date"], done.itertuples(index=False))}
        for k in frame.index:
            row = known.get((frame.at[k, "ticker"], frame.at[k, "event_date"]))
            if row is not None:
                frame.loc[k, ["classification", "source_url", "verified_at"]] = \
                    [row.classification, row.source_url, row.verified_at]
                frame.at[k, "notes"] += f" | reviewed_market_moves.csv: {row.notes}"
    facts["hand_review"] = apply_move_verdicts(frame, review_event_status(states))
    # plan 4.4 R1 resolves a big move with a second source within 0.5% (or a document): with the stored files
    # counted as a second source (owner convention of 2026-10-02), an R1 entry that two or more sources confirm
    # (``sources_agreeing``: the day's own source and at least one other, vendor or stored) is resolved by the
    # plan's own rule. It is classified mechanically, with that basis written in the notes; every other entry
    # (R1/R2, R1b, R1c, R3, ...) stays unreviewed for the hand review
    agreeing = frame["sources_agreeing"].fillna("").map(lambda s: [x for x in s.split("+") if x])
    doubtful = pd.Series([(a, b) in DOUBTFUL_PRICE_DAYS for a, b in zip(frame["security_id"], frame["event_date"])],
                         index=frame.index)
    mechanical = (frame["rule"].eq("R1") & frame["classification"].eq("unreviewed") & agreeing.map(len).ge(2)
                  & ~doubtful)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    for k in frame.index[mechanical]:
        sources = agreeing[k]
        frame.loc[k, ["classification", "verified_at"]] = [R1_MECHANICAL_CLASS, stamp]
        frame.at[k, "notes"] += (f" | classified mechanically (plan 4.4 R1: a second source within 0.5%): "
                                 f"{'+'.join(sources)} agree"
                                 + (" (the stored file counts as a second source: owner convention 2026-10-02)"
                                    if "stored" in sources and len([s for s in sources if s != "stored"]) < 2 else ""))
    facts_mechanical = {"rows": int(mechanical.sum()),
                        "with_two_vendors": int((mechanical & agreeing.map(lambda s: len([x for x in s if x != "stored"]) >= 2)).sum()),
                        "vendor_and_stored": int((mechanical & agreeing.map(lambda s: "stored" in s and
                                                                            len([x for x in s if x != "stored"]) < 2)).sum()),
                        "classification": R1_MECHANICAL_CLASS,
                        "rule": "R1 entries with two or more sources agreeing within 0.5% (the day's own and another, "
                                "a vendor or the stored file) are resolved by plan 4.4's R1 rule and classified "
                                "mechanically; the rest stay unreviewed"}
    frame = frame.sort_values(["security_id", "event_date", "rule"], kind="stable")
    year = frame["event_date"].str[:4]
    facts.update({"entries": int(len(frame)),
                  "by_rule": {k: int(v) for k, v in frame["rule"].value_counts().items()},
                  "by_rule_year": {rule: {y: int(v) for y, v in year[frame["rule"] == rule].value_counts().sort_index().items()}
                                   for rule in sorted(frame["rule"].unique())},
                  "prefilled_from_reviewed_market_moves": int((frame["classification"] != "unreviewed").sum()
                                                              - facts_mechanical["rows"]),
                  "classified_mechanically": facts_mechanical,
                  "unreviewed": int((frame["classification"] == "unreviewed").sum())})
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
    unfillable = unfillable_status()
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
            # the old shares' own end: their last Nasdaq session (the SEC documents), not the junction; a series
            # that stops well before it has a data gap there (OPI: the WIKI table ends 2018-03-27, the old shares
            # traded on Nasdaq to 2025-10-06), one that runs on into the OTC months reaches it (CHRD)
            nasdaq_end = junction.get("old_nasdaq_last_session", "")
            reference = nasdaq_end or previous_session_of(new["first"], sessions)
            if old["last"] in position and reference in position:
                short = max(0, position[reference] - position[old["last"]])
            else:
                short = ""
            pending = sid in waiting
            if old["last_src"] == "wiki" and old["last"] == WIKI_END and reference > WIKI_END:
                cause = "wiki_end_no_later_source" + ("_tiingo_pending" if pending else "")
            elif short and pending:
                cause = "tiingo_pending"
            elif short and short > 10:
                cause = "ends_early"
            else:
                cause = f"relist_junction:{junction.get('kind', '')}"
            rows.append({"security_id": sid, "ticker_last": old.get("ticker", ""),
                         "category": "old_shares_at_relist_junction", "last_date": old["last"], "delist_date": delist,
                         "last_listed": last_listed, "sessions_after_last_row": short,
                         "likely_cause": cause, "tiingo_pending": pending,
                         "filler_cut": "", "last_src": old["last_src"],
                         "existing_last_price_date_match": ("Y" if any(e.endswith(":" + old["last"]) for e in existing)
                                                            else "N") if existing else "",
                         "junction_date": new["first"], "junction_first_new_session": ",".join(junctions),
                         "junction_url": junction.get("url", ""), "old_nasdaq_last_session": nasdaq_end,
                         "old_shares_cancelled": junction.get("effective_date", ""),
                         "unfillable_status": unfillable.get(sid, ""), **common_fields(sid, info, summary, existing)})
        later_start = max((start for _, start, _ in relisted.get(sid, [])), default="")
        if sid in relisted and not (delist and later_start and delist >= later_start):
            # listed again after the Form 25: the later listing's end is the target (unless a later Form 25 ended
            # that listing too: Frontier's FYBR, 2026-01-30, is measured against that delisting)
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
        elif sid in SUCCESSOR_LINKS:  # cut at its last session (SUCCESSOR_LINKS); a 1:1 reorganisation continues
            cause = "successor_continuation" if SUCCESSOR_LINKS[sid]["continues"] else "successor_link"
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
                     "junction_url": "", "old_nasdaq_last_session": "", "old_shares_cancelled": "",
                     "unfillable_status": unfillable.get(sid, ""), **common_fields(sid, info, summary, existing)})
    frame = pd.DataFrame(rows, columns=SERIES_END_COLUMNS)
    return frame.sort_values(["category", "security_id"]) if len(frame) else frame


def previous_session_of(day: str, sessions: pd.DatetimeIndex) -> str:
    """The last session strictly before ``day``."""
    k = int(sessions.searchsorted(pd.Timestamp(day), side="left"))
    return str(sessions[k - 1].date()) if k > 0 else ""


def unfillable_status() -> dict[str, str]:
    """security -> its ``unfillable.csv`` status (a part of its need no free source fills: OPI wrong_entity)."""
    if not UNFILLABLE.exists():
        return {}
    table = pd.read_csv(UNFILLABLE, dtype=str, keep_default_na=False)
    if "status" not in table:
        return {sid: "unfillable" for sid in table.get("security_id", [])}
    return {sid: status or "unfillable" for sid, status in zip(table["security_id"], table["status"])}


# ``sessions_after_last_row``: the listed sessions after the series' last row up to the end it is measured
# against (the delist date, the later listing's end, the window end; for old shares at a relist junction their
# last Nasdaq session, ``old_nasdaq_last_session``, 0 when the series reaches it or runs on into the OTC months)
SERIES_END_COLUMNS = ["security_id", "ticker_last", "name", "category", "last_date", "delist_date", "last_listed",
                      "successor_security_id", "transfer_date", "sessions_after_last_row", "likely_cause",
                      "tiingo_pending", "filler_cut", "last_src", "existing_terminal_rows",
                      "existing_last_price_date_match", "in_candidates", "weeks_rank300", "terminal_2012_2026",
                      "junction_date", "junction_first_new_session", "junction_url", "old_nasdaq_last_session",
                      "old_shares_cancelled", "unfillable_status"]


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
        no_interval = states.get(row.security_id, {}).get("summary", {}).get("no_listing_interval")
        reason = "no_listing_interval" if no_interval else "tiingo_pending" if row.security_id in waiting_ids else \
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
                     "relisted_via": identity.get("relisted_via", {}).get(sid, ""),
                     "later_listing_start": start, "later_listing_end": end, "status": status,
                     "series_first": summary.get("first_date", ""), "series_last": summary.get("last_date", ""),
                     "rows_before_later_listing": before, "rows_in_later_listing": inside,
                     "first_new_session": entry.get("first_new_session", ""), "kind": entry.get("kind", ""),
                     "document_read": ("Y" if entry.get("read") else "N") if entry else "",
                     "read_on": entry.get("read_on", ""), "effective_date": entry.get("effective_date", ""),
                     "old_nasdaq_last_session": entry.get("old_nasdaq_last_session", ""),
                     "url": entry.get("url", ""), "nasdaq_end_url": entry.get("nasdaq_end_url", ""),
                     "old_last_date": old.get("last", ""), "old_last_src": old.get("last_src", ""),
                     "new_first_date": new.get("first", ""), "new_first_src": new.get("first_src", ""),
                     "level_ratio_new_first_to_old_last": round(new["first_close"] / old["last_close"], 4)
                     if old and old.get("last_close") else "",
                     "junction_dropped_events": " | ".join(summary.get("junction_dropped_events", [])),
                     "junction_leading_zero_volume_cut": " ".join(summary.get("junction_leading_zero_volume_cut", [])),
                     "relist_jumps": " ".join(summary.get("relist_jumps", [])), "note": entry.get("note", "")})
    frame = pd.DataFrame(rows)
    facts = {"securities": int(len(frame)),
             "by_status": {k: int(v) for k, v in frame["status"].value_counts().items()} if len(frame) else {},
             "junctions": {r.security_id: f"{r.old_last_date} -> {r.new_first_date}" for r in frame.itertuples()
                           if r.status == "junction"} if len(frame) else {},
             "documents_read": {sid: {"read": bool(e.get("read")), "read_on": e.get("read_on", ""),
                                      "effective_date": e.get("effective_date", ""),
                                      "old_nasdaq_last_session": e.get("old_nasdaq_last_session", ""),
                                      "first_new_session": e.get("first_new_session", "")}
                                for sid, e in sorted(RELIST_JUNCTIONS.items())},
             "leading_zero_volume_rows_cut": {r.security_id: r.junction_leading_zero_volume_cut for r in frame.itertuples()
                                              if r.junction_leading_zero_volume_cut} if len(frame) else {},
             "relisted_via": {k: int(v) for k, v in Counter(identity.get("relisted_via", {}).values()).items()},
             "rule": "RELIST_JUNCTIONS entries split the series at the new shares' first traded session: no return "
                     "across it (new-share rows with volume 0 before the first trade are cut), the old shares end "
                     "there (series_ends.csv old_shares_at_relist_junction, measured against their last Nasdaq "
                     "session); other relistings keep one series, and a raw level change of "
                     f"{RELIST_JUMP:g}x around them is queued (R9, in reviewed_moves.csv whatever its scope)"}
    return frame, facts


DIVIDEND_COLUMNS = ["security_id", "ex_date", "cash_as_paid", "sources", "ticker", "src_primary", "other_amounts",
                    "special"]


def dividend_table(states: dict[str, dict]) -> tuple[pd.DataFrame, dict]:
    """``CACHE/dividends.csv`` (plan 1.2): every cash dividend the canonical series books (div_cash > 0), as paid
    on the ex-date: ``sources`` the vendors carrying the same amount that day (within $0.001), ``other_amounts``
    the vendors with a row that day and another amount (0 when they book none), ``special`` Y above 10% of the
    prior close. Closing special dividends the terminal value owns are not in the series, so not here."""
    rows = [{"security_id": sid, **{k: d.get(k, "") for k in DIVIDEND_COLUMNS if k != "security_id"}}
            for sid, state in sorted(states.items()) for d in state.get("dividends", [])]
    frame = pd.DataFrame(rows, columns=DIVIDEND_COLUMNS)
    single = frame["sources"].fillna("").map(lambda s: len([x for x in s.split("+") if x]) < 2) if len(frame) else []
    facts = {"rows": int(len(frame)), "securities": int(frame["security_id"].nunique()) if len(frame) else 0,
             "rows_one_source": int(np.sum(single)) if len(frame) else 0,
             "rows_special": int(frame["special"].eq("Y").sum()) if len(frame) else 0,
             "terminal_dividends_dropped": {sid: s["summary"]["terminal_dividends_dropped"]
                                            for sid, s in sorted(states.items())
                                            if s["summary"].get("terminal_dividends_dropped")},
             "rule": "a special dividend paid to holders at a merger closing (the terminal step's REVIEWED "
                     "special_dividend: CHNG, STAY, NGHC, DELL, KRFT) is booked once, in the terminal value, since "
                     "the last close still carries it; a vendor's booking of it within "
                     f"{TERMINAL_DIVIDEND_DAYS} days up to its record date is dropped from the series "
                     "(div_in_terminal_value)"}
    return frame, facts


SUCCESSOR_LINK_COLUMNS = ["predecessor_id", "successor_id", "ticker", "last_session", "continues", "basis", "url",
                          "predecessor_last_row", "predecessor_rows_cut", "successor_first_session",
                          "successor_first_row", "successor_first_return", "sessions_between", "anchor_sources",
                          "successor_own_rows_dropped", "predecessor_rows_added", "raw_yahoo_fill_rows",
                          "yahoo_junction_cleared", "overlap_days", "successor_rows", "successor_last_row",
                          "successor_last_listed", "short_after_continuation", "status", "note"]
SHORT_SUCCESSOR_DAYS = 60  # a continued successor whose series ends this long before its last listed day is short


def successor_link_table(states: dict[str, dict], master: pd.DataFrame | None = None) -> tuple[pd.DataFrame, dict]:
    """``CACHE/reconcile/successor_links.csv``: each SUCCESSOR_LINKS pair after the build. ``status``: ``continued``
    (the successor's first return runs from the predecessor's last close), ``continued_no_return`` (a continuing
    link whose first return could not be measured: no anchor close, or a gap of more than GAP_RETURN_MAX sessions),
    ``cut`` (a pair that does not continue), ``cut_class_kept`` (a class folded into another existing class:
    ``handover`` False, the other class untouched), ``predecessor_ends_early`` (its series stops before the last
    session), ``no_successor_series`` / ``no_predecessor_series``. ``overlap_days``: days in both series (0 by
    construction, checked here; a ``handover`` False pair is not counted: the other class trades on). A continued
    successor whose series ends more than SHORT_SUCCESSOR_DAYS days before its last listed day (master) is
    ``short_after_continuation`` (its own files are missing: not fetched, or fetched for a later span only)."""
    last_listed = dict(zip(master["security_id"], master["last_listed"])) if master is not None else {}
    rows = []
    for pred, link in sorted(SUCCESSOR_LINKS.items()):
        succ = link["successor"]
        p_state, s_state = states.get(pred, {}), states.get(succ, {})
        p_sum, s_sum = p_state.get("summary", {}), s_state.get("summary", {})
        handover = link.get("handover", True)
        info = s_sum.get("successor_link", {}) if handover else {}
        s_rows = s_sum.get("successor_link_rows", {}) if handover else {}
        p_rows = p_sum.get("successor_link_rows", {})
        p_days, s_days = p_state.get("dates"), s_state.get("dates")
        overlap = int(len(np.intersect1d(p_days, s_days))) if handover and p_days is not None and s_days is not None else ""
        if not p_sum.get("rows"):
            status = "no_predecessor_series"
        elif not s_sum.get("rows"):
            status = "no_successor_series"
        elif p_sum.get("last_date", "") < link["last_session"]:
            status = "predecessor_ends_early"
        elif not handover:
            status = "cut_class_kept"
        elif not link["continues"]:
            status = "cut"
        else:
            status = "continued" if info.get("first_row_return") == "measured" else "continued_no_return"
        s_last, s_listed = s_sum.get("last_date", ""), last_listed.get(succ, "")
        short = bool(link["continues"] and s_last and s_listed and
                     pd.Timestamp(s_last) < min(pd.Timestamp(s_listed), pd.Timestamp(WINDOW_END)) -
                     pd.Timedelta(days=SHORT_SUCCESSOR_DAYS))
        rows.append({"predecessor_id": pred, "successor_id": succ, "ticker": link["ticker"],
                     "last_session": link["last_session"], "continues": bool(link["continues"]), "basis": link["basis"],
                     "url": link["url"], "predecessor_last_row": p_sum.get("last_date", ""),
                     "predecessor_rows_cut": p_rows.get("rows_after_last_session_cut", ""),
                     "successor_first_session": s_rows.get("first_session", ""),
                     "successor_first_row": info.get("first_row", s_sum.get("first_date", "") if handover else ""),
                     "successor_first_return": info.get("first_row_return", ""),
                     "sessions_between": info.get("sessions_between", ""),
                     "anchor_sources": "+".join(info.get("anchor_sources", [])),
                     "successor_own_rows_dropped": s_rows.get("own_rows_dropped", ""),
                     "predecessor_rows_added": s_rows.get("predecessor_rows_added", ""),
                     "raw_yahoo_fill_rows": s_rows.get("raw_yahoo_fill_rows", ""),
                     "yahoo_junction_cleared": s_rows.get("yahoo_junction_cleared", ""), "overlap_days": overlap,
                     "successor_rows": s_sum.get("rows", 0), "successor_last_row": s_last,
                     "successor_last_listed": s_listed, "short_after_continuation": short, "status": status,
                     "note": link.get("note", "")})
    frame = pd.DataFrame(rows, columns=SUCCESSOR_LINK_COLUMNS)
    facts = {"pairs": int(len(frame)), "continuing": int(frame["continues"].sum()) if len(frame) else 0,
             "by_status": {k: int(v) for k, v in frame["status"].value_counts().items()} if len(frame) else {},
             "overlap_days_total": int(pd.to_numeric(frame["overlap_days"], errors="coerce").fillna(0).sum())
             if len(frame) else 0,
             "short_after_continuation": {
                 "rule": f"a continued successor whose series ends more than {SHORT_SUCCESSOR_DAYS} days before its last "
                         "listed day (master last_listed, at most the window end): its own price files are missing "
                         "(the prefilter / Yahoo / Tiingo lines have not fetched it, or only a later span); the "
                         "positions held across the link run into that gap",
                 "pairs": [{"predecessor": r.predecessor_id, "successor": r.successor_id,
                            "successor_last_row": r.successor_last_row, "successor_last_listed": r.successor_last_listed,
                            "successor_rows": int(r.successor_rows)}
                           for r in frame.itertuples(index=False) if r.short_after_continuation]} if len(frame) else {},
             "rule": "owner convention 2026-10-02 (CRSP keeps one PERMNO): a 1:1 holding-company reorganisation or "
                     "reincorporation continues the same security; every SUCCESSOR_LINKS predecessor is cut at its "
                     "last session (no day is in both series), and a continuing successor's first return runs from "
                     "the predecessor's last close (flag successor_link:{predecessor}); the terminal step books no "
                     "terminal return for a continuing predecessor"}
    return frame, facts


BREAK_DAY_STATES = {
    "unit_break": "the stored file alone changes units that day (every vendor moves normally): a unit_break row in "
                  "split_events.csv, and the stored file is left out of the vote",
    "stored_moves_with_vendors": "the stored file moves 1.4x or more and the vendors show the same move within 0.5%: a "
                                 "real price move, not a unit change, so split_events.csv has no unit_break row for it "
                                 "(the stored file is still left out of the vote on a known break day)",
    "unit_change_on_vendor_event": "the stored file's change falls on a vendor's own split or distribution day: "
                                   "recorded with that event in split_events.csv, not as a unit_break",
    "stored_differs": "the stored file moves 1.4x or more, the vendors do not move with it, and no unit change was "
                      "recorded: left out of the vote, no row (a data point to look at)",
}


def break_day_table(states: dict[str, dict]) -> dict:
    """The known break days: where the stored file moves by a split-sized factor or is a unit break, what
    the vendors show (``unit_break``: the stored file alone changes units; ``stored_moves_with_vendors``: a
    real move both show, so no unit break, EYEN/HYPD, NKTR, UPXI 2025-06-24). Each day lists, by state,
    the securities and their last ticker, so a stored-only test (step 6's split-ratio list, the validate
    step's stored comparison) can tell a file it lists as a break with no unit_break row from a miss."""
    out = defaultdict(dict)
    for sid, state in sorted(states.items()):
        ticker = state["summary"].get("ticker_last", "")
        for item in state["summary"].get("break_days", []):
            out[item["date"]][sid] = {"ticker": ticker, **{k: v for k, v in item.items() if k != "date"}}
    days = {}
    for day, items in sorted(out.items()):
        by_state = defaultdict(list)
        for sid, item in items.items():
            by_state[item["state"]].append(f"{sid} {item['ticker']}".strip())
        days[day] = {"by_state": dict(Counter(v["state"] for v in items.values())),
                     "securities_by_state": {k: sorted(v) for k, v in sorted(by_state.items())},
                     "securities": items}
    return {"how_to_read": {
        "scope": f"the known stored-file break days {list(BREAK_DAYS)}: every security whose stored file moves 1.4x or "
                 "more either way that day, or changes units there; the stored file is never a vote on these days",
        "states": BREAK_DAY_STATES,
        "for_a_stored_only_test": "a security listed as a break by a test that reads only the stored file "
                                  "(priced_without_unit_break_row) is explained here when its state is "
                                  "stored_moves_with_vendors: the vendors confirm the move (EYEN/HYPD +65%, NKTR +156%, "
                                  "UPXI -60% on 2025-06-24), so no unit_break row is expected and none is missing; "
                                  "a listed security absent from this table or in another state needs a look",
        "fields": "per security: ticker (its last), state, stored_r (the stored file's own return that day), tr (the "
                  "canonical return), stored_implied_k ((1 + tr) / (1 + stored_r))"},
        "days": days}


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
    dividends, dividend_facts = dividend_table(states)
    write_csv(DIVIDENDS, dividends)
    links, link_facts = successor_link_table(states, identity["master"])
    write_csv(OUT / "successor_links.csv", links)
    event_changes, event_facts = review_event_table(states)
    write_csv(OUT / "review_event_changes.csv", event_changes)
    data_changes, data_change_facts = review_data_changes(event_changes)
    write_csv(OUT / "review_data_changes.csv", data_changes)
    event_facts = {**event_facts, "data_changes": data_change_facts}
    log(f"dividends.csv {len(dividends)} rows; successor_links.csv {link_facts['by_status']}")
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
        "review_event_changes": event_facts,
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
        "listing_starts_after_quote": {
            "rule": "a listing's (or segment's) first row with a trade (vendor volume > 0), when every row before it "
                    "since the listing start is untraded, the row before it is untraded, no vendor row traded in the "
                    f"{pf.CLOSE_STALE_SESSIONS} sessions before it and the kept closes there all equal that row's "
                    "(a carried quote), has a blank tr (flag listing_start_after_quote); untraded rows keep their tr",
            "securities": {sid: s["summary"]["listing_starts_after_quote"] for sid, s in sorted(states.items())
                           if s["summary"].get("listing_starts_after_quote")}},
        "gap_returns_blank": {
            "rule": f"plan R9: no return across a gap of more than {GAP_RETURN_MAX} sessions (tr blank, flag "
                    "gap_return_blank)",
            "securities": {sid: s["summary"]["gap_returns_blank"] for sid, s in sorted(states.items())
                           if s["summary"].get("gap_returns_blank")}},
        "dividends": dividend_facts,
        "successor_links": link_facts,
        "break_days": break_day_table(states),
        "checks": checks,
        "files": {"prices": str(PRICES_DIR), "split_events": str(SPLIT_EVENTS), "special_distributions": str(SPECIAL),
                  "reviewed_moves": str(REVIEWED_MOVES), "series_ends": str(OUT / "series_ends.csv"),
                  "coverage_gaps": str(OUT / "coverage_gaps.csv"), "no_series": str(OUT / "no_series.csv"),
                  "relist_junctions": str(OUT / "relist_junctions.csv"), "dividends": str(DIVIDENDS),
                  "successor_links": str(OUT / "successor_links.csv")},
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
    value_rows = np.zeros(len(common), dtype=bool)  # common dates where any value (not only the flags) changed
    for column, tolerance in COMPARE_NUMERIC.items():
        a = pd.to_numeric(o.loc[common, column], errors="coerce").to_numpy(float)
        b = pd.to_numeric(w.loc[common, column], errors="coerce").to_numpy(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            same = (np.isnan(a) & np.isnan(b)) | (np.abs(a - b) <= tolerance * np.maximum(np.abs(a), 1.0))
        out[f"{column}_changed"] = int((~same).sum())
        value_rows |= ~same
    for column in ("src_primary", "flags"):
        a = o.loc[common, column].fillna("").astype(str).to_numpy()
        b = w.loc[common, column].fillna("").astype(str).to_numpy()
        out[f"{column}_changed"] = int((a != b).sum())
        if column == "src_primary":
            value_rows |= a != b
    out["common_rows"] = int(len(common))
    out["common_rows_values_changed"] = int(value_rows.sum())
    out["kind"] = ("dates" if out["rows_added"] or out["rows_removed"] else
                   "values" if value_rows.any() else "flags_only" if out["flags_changed"] else "unchanged")
    # a series whose dates changed can also change on the dates both builds have (Tiingo replacing WIKI rows
    # 2017-11..2018-03): ``kind`` stays ``dates``, ``dates_values`` says the common dates changed too
    out["dates_values"] = bool(out["kind"] == "dates" and value_rows.any())
    return out


def dates_value_changes(frame: pd.DataFrame) -> dict:
    """Among the series of kind ``dates`` (rows added or removed), those whose values also changed on the dates
    both builds have: securities and rows, in all and per column (close, volume, S, D, tr, primary source).
    Counts of rows only."""
    columns = [f"{c}_changed" for c in list(COMPARE_NUMERIC) + ["src_primary"]]
    if not len(frame) or "dates_values" not in frame:
        return {"securities": 0, "rows": 0, "by_column": {c: 0 for c in columns}}
    part = frame[frame["kind"].eq("dates") & frame["dates_values"].fillna(False).astype(bool)]
    return {"securities": int(len(part)),
            "rows": int(pd.to_numeric(part["common_rows_values_changed"], errors="coerce").fillna(0).sum()),
            "by_column": {c: int(pd.to_numeric(part[c], errors="coerce").fillna(0).sum()) for c in columns},
            "dates_series": int(frame["kind"].eq("dates").sum()),
            "note": "series with dates added or removed whose values on the common dates changed as well (another "
                    "source became primary, a return was recomputed); compare_series.csv has the counts per security"}


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
                "series_by_kind": {k: int(v) for k, v in kinds.items()},
                "dates_series_with_value_changes_on_common_dates": dates_value_changes(frame)}
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


def review_source_summary(states: dict[str, dict]) -> dict:
    """The hand review's per-day source choices as the series applied them (reconcile_security ``review_sources``)."""
    per = {sid: s["summary"]["review_sources"] for sid, s in states.items() if s["summary"].get("review_sources")}
    return {"review_dir": str(REVIEW_DIR) if APPLY_REVIEW else "", "apply_source_overrides": APPLY_REVIEW_SOURCE_OVERRIDES,
            "securities": len(per), "days": sum(v["days"] for v in per.values()),
            "days_source_changed": sum(v["changed"] for v in per.values()),
            "unresolved_days_resolved": sum(v["unresolved_resolved"] for v in per.values()),
            "days_flagged_only": sum(v["flagged"] for v in per.values()),
            "unused_items": sorted(i for v in per.values() for i in v["unused_items"]),
            "per_security": per}


def main(argv=None) -> int:
    global MIN_FREE_MB, WRITE_SOURCE_CACHE, REVIEW_DIR, APPLY_REVIEW
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
    parser.add_argument("--review-dir", default="",
                        help=f"read the merged hand-review verdicts from this folder (default {REVIEW_DIR})")
    parser.add_argument("--no-review", action="store_true",
                        help="read no hand-review verdicts (the queues as the vote leaves them)")
    args = parser.parse_args(argv)
    MIN_FREE_MB, WRITE_SOURCE_CACHE = args.min_free_mb, not args.no_source_cache
    if args.review_dir:
        REVIEW_DIR = Path(args.review_dir)
    APPLY_REVIEW = not args.no_review
    _REVIEW_CACHE.clear()
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
    superseded = []
    if only is None and args.rebuild:  # price files of securities that are no longer targets: moved aside
        superseded = supersede_non_targets(ids)
        if superseded:
            log(f"moved {len(superseded)} price files of non-targets to {superseded_dir()}: "
                f"{' '.join(m['security_id'] for m in superseded)}")
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
    summary["hand_review_sources"] = review_source_summary(states)
    mark = phase("tables_and_panel", mark)
    stale = sorted(path.stem for path in PRICES_DIR.glob("*.csv") if path.stem not in set(ids))
    summary["price_files"] = {"superseded_this_run": superseded, "not_targets_left": stale,
                              "rule": "--rebuild moves the price files of securities that are no longer targets to "
                                      "reconcile/superseded_prices_{date}/ (never deleted); a build without --rebuild "
                                      "lists them here"}
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
