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
  ex-date from 2023 on (price-only after that), not on a day its own close jumps by a split-sized
  ratio that the vendors do not show (a ``unit_break`` in ``split_events.csv``).

Canonical record (``CACHE/prices/{security_id}.csv``): date, close_raw, volume_raw, split_factor,
div_cash, tr, src_primary, n_sources, max_src_diff, flags.
- Rows are XNAS sessions inside the security's window: its listing spans (``pf.mapping_spans``) with
  75 calendar days before (the dv50 warm-up) and 28 after (plan 4.5 hold), within 2011-06-01 to
  2026-08-31; rows outside the spans are kept and flagged ``outside_listing`` (R9).
- ``tr`` = (C_t x S_t + D_t) / C_{t-1} - 1 from one source's own rows (plan 4.1), so only returns
  are chained across sources, never levels (R8).
- Precedence per day: WIKI, Tiingo, Yahoo to 2017-10-31; Tiingo, Yahoo, WIKI from 2017-11-01. Each
  vendor's return, and the stored vote where valid, is compared with the others: agreeing within
  0.5% or not (R3). With a strict majority, the highest-ranked source in it is primary
  (``majority_override`` when that is not the default one, ``disagree_resolved`` naming the
  minority); without one (two vendors that disagree) the default stays and the day is
  ``disagree_unresolved`` and queued. When the only dissent is the stored file against a single
  vendor, the vendor stands: ``stored_glitch`` (the stored level comes back within two sessions: a
  bad stored row, LANC 2020-2022), ``stored_shift`` (it moves 2% or more and stays: queued, an event
  the vendor may lack), or ``stored_disagrees`` (a smaller lasting shift).
- ``n_sources`` = sources with a return that day (vendors plus a valid stored vote);
  ``max_src_diff`` = the largest absolute difference between one of them and ``tr``.
- Other flags: ``level_diff`` (R7, vendor raw closes more than 1% apart), ``move_2x`` and ``move_40``
  (R1), ``hidden_split`` (R2: S = 1 everywhere, the price ratio is within 1% of n:1, 1:n, 3:2 or 2:3,
  and the adjusted stored file does not move with it),
  ``flat_run`` / ``zero_volume`` (R4), ``gap_before:N`` (R6: N sessions missing before this row
  inside the listing), ``cross_source_return`` (the day's source lacks the prior session, so ``tr``
  uses the previous canonical close), ``splice:a>b`` and ``splice_weak`` (R8), ``split`` /
  ``reverse_split`` / ``distribution_factor``, ``special_div`` (D > 10% of the prior close),
  ``volume_diff`` (vendor volumes 2x apart), ``stored_excluded``, ``yahoo_junction``,
  ``wiki_div_gap``, ``tiingo_adj_identity``, ``tiingo_review`` (the fetcher's ``done_review``).
- R5: rows after the last session with volume > 0 at the end of the series are cut (SGEN, SPLK,
  EVBG filler) and counted.

Tables:
- ``INPUTS/split_events.csv``: every S != 1 in any vendor source plus the stored files' unit breaks,
  matched across sources (same ex-date +-1 session, ratio within 0.1%), typed split /
  reverse_split / spinoff (plan 4.3's known cases) / distribution (odd ratios, or a ratio in one source
  and cash in another) / unit_break (a split-sized jump in the stored file alone; 2025-06-24). The
  ``nasdaq`` column is k = (1 + tr) / (1 + stored return) on the ex-date: about 1 where the stored
  file is adjusted for the event (it moves with the total return), about S where it shows the raw
  jump, and for a unit_break the size of the stored file's own unit change. ``agree`` is Y when at
  least two sources confirm the ratio (a vendor with the same ratio, or the stored file at k = 1 or
  k = S within 2%) and no vendor covering the date shows another one; ``tr_agree`` says whether the
  sources' total returns agree within 0.5% that day (a distribution served as cash by one vendor
  and as a ratio by another can agree on it). ``sec_url`` / ``verified_at`` stay blank for the
  hand review, except where ``confirmed_price_adjustments.csv`` gives them (PRPL);
  ``sec_candidates`` lists nearby 8-Ks with Items 2.01/3.03/5.03 (then 8.01) from the cached SEC
  submissions files (no request is made).
- ``INPUTS/special_distributions.csv``: cash above 10% of the prior raw close in any vendor, and
  every odd ratio (distributions served as splits; ``pct_of_prior`` is then 1 - 1/ratio), with the
  sources' total returns when they differ and the same ``sec_candidates``.
- ``INPUTS/reviewed_moves.csv``: the draft queue for the hand review, in
  ``stocks_list_dir/nasdaq/reviewed_market_moves.csv`` format plus ``security_id`` and
  ``sources_agreeing`` (sources whose return is within 0.5% of ``tr`` that day, or that show the same
  flat run); ``classification`` is ``unreviewed`` (or the reviewed file's entry for the same ticker
  and date) and ``notes`` starts with the rule: [R1] move of 2x or 0.5x, [R1/R2] one that is also a
  hidden-split candidate, [R1b] a 40% move no second source confirms, [R3] two vendors with no
  majority or a lasting stored shift, [R4] a flat run no second source shows or zero volume, [R6]
  listed sessions with no vendor row, [R7] a vendor level run 2%+ apart or 3+ sessions long. Only
  listed days that can matter are queued: 10 weeks before to 5 weeks after a week ranked <= 300 by
  step 6, or whose canonical dollar volume reaches step 6's rank-300 cut; every entry, with that
  scope marked, is in ``CACHE/reconcile/moves_all.csv``.
- ``CACHE/reconcile/``: summary.json (coverage of ranks 1-300 by year with step 6's 5-session
  staleness rule, flag counts by type and year, the plan-6 multi-source agreement count, known-case
  checks), series_ends.csv (series that end before the delist date, or before the window end with
  none: the inputs for terminal values, with a likely cause), coverage_gaps.csv, no_series.csv,
  securities.csv, source_pairs.csv, moves_all.csv; ``sources/`` holds the source bundles and
  ``per_security/`` the state that makes a rerun redo only the securities whose inputs (or this
  file) changed. ``CACHE/prices/daily_panel.csv.gz`` is the long form of every canonical file.

Usage::

    PYTHONPATH=. python scripts/reversal_data_reconcile.py                 # build (resumes)
    PYTHONPATH=. python scripts/reversal_data_reconcile.py --only 1065088  # one security, no tables
    PYTHONPATH=. python scripts/reversal_data_reconcile.py --rebuild       # ignore the per-security state
    PYTHONPATH=. python scripts/reversal_data_reconcile.py --no-panel      # skip daily_panel.csv.gz
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

CODE_VERSION = "2026-10-02.1"
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

PRICE_COLUMNS = ["date", "close_raw", "volume_raw", "split_factor", "div_cash", "tr", "src_primary",
                 "n_sources", "max_src_diff", "flags"]
SPLIT_COLUMNS = ["security_id", "ticker", "ex_date", "split_factor", "event_type", "tiingo", "yahoo", "wiki",
                 "nasdaq", "agree", "sec_url", "verified_at", "notes", "tr_agree", "sources_confirming", "sec_candidates"]
SPECIAL_COLUMNS = ["security_id", "ex_date", "cash", "prior_close_raw", "pct_of_prior", "classification", "sec_url",
                   "ticker", "ratio", "sources", "notes", "sec_candidates"]
MOVE_COLUMNS = ["ticker", "event_date", "classification", "source_url", "verified_at", "notes", "security_id",
                "sources_agreeing"]


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


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
    master = pf.load_master()
    intervals = pf.load_intervals()
    spans = pf.listing_spans(intervals, master)
    mapping = pf.mapping_spans(spans)
    return {"master": master, "spans": spans, "mapping": mapping, "ticker_map": pf.TickerMap(mapping),
            "signature": file_signature([pf.MASTER, pf.INTERVALS])}


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
    splits, odd ratios are distributions (``events.csv`` flags)."""
    flags = defaultdict(set)
    if YAHOO_EVENTS.exists():
        events = pd.read_csv(YAHOO_EVENTS, dtype=str, keep_default_na=False)
        for sid, day, kind, text in zip(events["security_id"], events["ex_date"], events["event_type"], events["flags"]):
            for token in text.split():
                if token in ("odd_ratio", "trim_junction", "segment_junction", "not_applied_by_yahoo",
                             "volume_restore_unverified", "volume_not_scaled_by_yahoo"):
                    flags[(sid, day)].add(token)
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


def cached_bundle(name: str, signature: str, build) -> pd.DataFrame:
    """A source bundle from ``SOURCE_CACHE/{name}.pkl`` when its signature matches, else rebuilt."""
    path = SOURCE_CACHE / f"{name}.pkl"
    if path.exists():
        try:
            with path.open("rb") as handle:
                stored = pickle.load(handle)
            if stored.get("signature") == signature:
                return stored["rows"]
        except Exception:
            pass
    started = time.time()
    rows = build()
    common.atomic_write(path, pickle.dumps({"signature": signature, "rows": rows}, protocol=pickle.HIGHEST_PROTOCOL))
    log(f"source {name}: {len(rows):,} rows for {rows['security_id'].nunique() if len(rows) else 0} securities "
        f"({time.time() - started:.0f}s)")
    return rows


def load_sources(identity: dict, targets: set[str], windows: dict) -> tuple[dict[str, pd.DataFrame], dict]:
    base = identity["signature"] + hashlib.sha256(" ".join(sorted(targets)).encode()).hexdigest()
    wiki_sig = file_signature(list(WIKI_DIR.glob("*.csv.gz")) + [WIKI_ENTITY]) + base
    stored_sig = file_signature(list(STORED_DIR.glob("*.csv")) + [pf.PRICE_FILE_OWNERS]) + base
    old_tiingo_sig = file_signature([p for d in OLD_TIINGO_DIRS for p in d.glob("*.json")]) + base
    yahoo_files = list(YAHOO_DIR.glob("*.csv.gz")) + [YAHOO_EVENTS]
    new_yahoo_sig = file_signature(yahoo_files) + base
    have_new = {p.name[:-len(".csv.gz")] for p in YAHOO_DIR.glob("*.csv.gz")}
    old_yahoo_sig = file_signature([p for d in OLD_YAHOO_DIRS for p in d.glob("*.json")]) + base + \
        hashlib.sha256(" ".join(sorted(have_new)).encode()).hexdigest()
    sources = {
        "wiki": cached_bundle("wiki", wiki_sig, lambda: load_wiki_rows(identity, targets)),
        "stored": cached_bundle("stored", stored_sig, lambda: load_stored_rows(identity, targets)),
        "tiingo_old": cached_bundle("tiingo_old", old_tiingo_sig, lambda: load_old_tiingo_rows(identity, targets)),
        "yahoo_new": cached_bundle("yahoo_new", new_yahoo_sig, lambda: load_new_yahoo_rows(targets)),
        "yahoo_old": cached_bundle("yahoo_old", old_yahoo_sig, lambda: load_old_yahoo_rows(identity, targets, have_new)),
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


def near_split_factor(factor: float) -> float | None:
    """``factor`` as an ordinary split factor (n:1, 1:n up to 100, n:m up to 10) within 2.5%, when it
    is split-sized (>= 1.4 or <= 1/1.4); None otherwise."""
    if factor is None or not np.isfinite(factor) or factor <= 0:
        return None
    if 1 / SPLIT_LIKE < factor < SPLIT_LIKE:
        return None
    candidates = [float(n) for n in range(2, 101)] + [1.0 / n for n in range(2, 101)] + \
                 [n / m for m in range(2, 11) for n in range(1, 11) if n % m]
    best = min(candidates, key=lambda c: abs(factor / c - 1))
    return float(best) if abs(factor / best - 1) <= 0.025 else None


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

    junction = np.array(["yahoo_junction" in f for f in RF[Y]])
    S[Y, junction] = 1.0
    D[Y, junction] = 0.0
    prev = np.c_[np.full((4, 1), np.nan), C[:, :-1]]
    r = total_return(C, S, D, prev)
    r[Y, junction] = np.nan
    r[ST] = C[ST] / prev[ST] - 1.0  # stored: adjusted close, a vote only
    valid = np.isfinite(r)
    early = grid < pd.Timestamp(WIKI_DIV_GAP_FROM)
    rank = np.full((4, n), np.inf)
    for i, name in enumerate(SRC[:3]):
        rank[i] = np.where(early, PRECEDENCE_EARLY.index(name), PRECEDENCE_LATE.index(name))

    # pass 1: vendors only, for the stored vote's validity
    vendor_valid = valid.copy()
    vendor_valid[ST] = False
    first = select_sources(r, vendor_valid, has, rank)
    p1 = first["primary"]
    tr1 = np.where(p1 >= 0, r[np.clip(p1, 0, 3), np.arange(n)], np.nan)
    vendor_event = (np.abs(S[:3] - 1.0) > 1e-9).any(axis=0) | (D[:3] > 0).any(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        implied = (1.0 + tr1) / (1.0 + r[ST])  # the split factor the stored file's jump amounts to
    stored_unit = np.array([near_split_factor(k) is not None for k in implied])
    break_day = np.isin(grid.strftime("%Y-%m-%d"), BREAK_DAYS)
    stored_excluded = valid[ST] & (break_day | stored_unit | (vendor_event & (grid >= pd.Timestamp(STORED_EX_FROM))))
    valid[ST] &= ~stored_excluded

    choice = select_sources(r, valid, has, rank)
    primary = choice["primary"]
    cols = np.arange(n)
    p = np.clip(primary, 0, 3)
    stored_kind = stored_disagreements(C, r, valid, choice, p)
    choice["unresolved"] = choice["unresolved"] & (stored_kind == "")
    Cp, Vp, Sp, Dp = C[p, cols], V[p, cols], S[p, cols], D[p, cols]

    # R5: cut filler after the last session with volume > 0 (a missing volume is not filler)
    keep = has_vendor & (primary >= 0)
    traded = np.flatnonzero(keep & ~(Vp == 0))
    filler_cut = 0
    if len(traded):
        tail = keep.copy()
        tail[: traded[-1] + 1] = False
        filler_cut = int(tail.sum())
        keep &= ~tail
    idx = np.flatnonzero(keep)
    prev_idx = np.r_[-1, idx[:-1]]
    gap = np.where(prev_idx >= 0, idx - prev_idx - 1, 0)
    tr = np.full(n, np.nan)
    own = valid[p, cols]
    tr[own] = r[p, cols][own]
    cross = np.zeros(n, dtype=bool)
    for k, j in zip(idx, prev_idx):
        if own[k] or j < 0 or (p[k] == Y and junction[k]):
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
    # R4
    flat = np.zeros(n, dtype=bool)
    if len(idx) >= FLAT_RUN:
        same = np.r_[False, Cp[idx][1:] == Cp[idx][:-1]]
        for s0, s1 in run_lengths(same):
            if s1 - s0 + 2 >= FLAT_RUN:
                flat[idx[s0 - 1: s1 + 1]] = True
    zero_vol = keep & (np.nan_to_num(Vp, nan=-1.0) == 0) & listed

    # splices (R8): a change of primary that lasts SPLICE_STAY rows
    splice_tokens = {}
    for k, old, new in splice_points(primary[idx], idx):
        window = slice(max(0, k - 60), k)
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

    canonical = pd.DataFrame({
        "date": grid[idx].strftime("%Y-%m-%d"), "close_raw": Cp[idx], "volume_raw": Vp[idx],
        "split_factor": Sp[idx], "div_cash": Dp[idx], "tr": tr[idx], "src_primary": [SRC[i] for i in p[idx]],
        "n_sources": n_sources[idx], "max_src_diff": max_diff[idx], "flags": [";".join(tokens[k]) for k in idx]})
    result["canonical"] = canonical

    ctx_local = {"grid": grid, "idx": idx, "C": C, "S": S, "D": D, "has": has, "r": r, "valid": valid, "tr": tr,
                 "Cp": Cp, "Sp": Sp, "Dp": Dp, "p": p, "implied": implied, "junction": junction, "listed": listed,
                 "agreeing": agreeing, "RF": RF, "keep": keep}
    result["events"] = split_events_of(sid, ctx_local, ticker_of)
    result["specials"] = specials_of(sid, ctx_local, ticker_of)
    result["moves"] = moves_of(sid, ctx_local, ticker_of, choice=choice, move_2x=move_2x, move_big=move_big,
                               hidden=hidden, flat=flat, zero_vol=zero_vol, level_off=level_off, gap=gap,
                               stored_kind=stored_kind)
    result["pairs"] = pair_stats(sid, C, r, valid, has)
    counts = Counter(token.split(":")[0] for k in idx for token in tokens[k])
    result["summary"].update({
        "rows": int(len(idx)), "first_date": canonical["date"].iloc[0], "last_date": canonical["date"].iloc[-1],
        "filler_cut": filler_cut, "rows_by_primary": dict(Counter(canonical["src_primary"])),
        "rows_multi_source": int((canonical["n_sources"] >= 2).sum()),
        "flag_counts": dict(counts), "last_volume_positive": bool(Vp[idx[-1]] > 0) if len(idx) else False,
        "tiingo_identity_rows": int(sum("tiingo_adj_identity" in f for f in RF[T_][has[T_]])),
        "listed_sessions_without_row": int((listed & ~keep & (cols >= idx[0]) & (cols <= idx[-1])).sum()),
        "ticker_last": ticker_of(grid[idx[-1]]), "last_src": SRC[p[idx[-1]]],
    })
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
    stored_units = [k for k in range(n) if has[ST, k] and np.isfinite(implied[k]) and near_split_factor(implied[k])]
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
        for i, name in enumerate(SRC[:3]):
            mine = [S[i, k] for k, j in cluster if j == i]
            covers = has[i, lo: hi + 1].any() and has[i, max(0, lo - 1): hi + 1].sum() >= 2
            if mine:
                values[name] = mine[0]
                (confirm if ratio_match(mine[0], factor) else contra).append(name)
            elif covers:
                values[name] = 1.0
                contra.append(name)
                big_cash = [k for k in range(lo, hi + 1) if has[i, k] and D[i, k] > 0 and np.isfinite(C[i, k - 1] if k else np.nan)
                            and D[i, k] > SPECIAL_PCT * C[i, k - 1]]
                if big_cash:
                    k = big_cash[0]
                    notes.append(f"{name} shows cash {D[i, k] / C[i, k - 1]:.1%} of prior close instead")
            else:
                values[name] = ""
        stored_k = ""
        unit_days = [k for k in range(lo, hi + 1) if k in stored_units]
        used_units.update(unit_days)
        if has[ST, day] and np.isfinite(implied[day]):
            stored_k = round(float(implied[day]), 4)
            if ratio_match(stored_k, 1.0, TOL_STORED_RATIO):
                confirm.append("stored")  # the stored file moves with the total return: adjusted for S
                notes.append("stored adjusted for it")
            elif ratio_match(stored_k, factor, TOL_STORED_RATIO):
                confirm.append("stored")  # a raw jump of 1/S on the same day: the same date and ratio
                notes.append("stored shows the raw jump (not adjusted)")
            else:
                notes.append("stored implies another factor")
        ticker = ticker_of(grid[day])
        yahoo_flags = " ".join(sorted({f for k, j in cluster if j == Y for f in x["RF"][Y, k].split()
                                       if f in ("yahoo_odd_ratio", "yahoo_not_applied")}))
        if yahoo_flags:
            notes.append(yahoo_flags)
        if (ticker, str(grid[day].date())) in KNOWN_SPINOFFS:
            kind = "spinoff"
        elif not ordinary_ratio(factor) or "yahoo_odd_ratio" in yahoo_flags or any("cash" in t for t in notes):
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
                       "tiingo": values["tiingo"], "yahoo": values["yahoo"], "wiki": values["wiki"], "nasdaq": stored_k,
                       "agree": "Y" if len(confirm) >= 2 and not contra else "N", "sec_url": "", "verified_at": "",
                       "notes": "; ".join(notes), "tr_agree": tr_agree, "sources_confirming": "+".join(confirm),
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
                       "yahoo": values["yahoo"], "wiki": values["wiki"], "nasdaq": round(float(implied[k]), 4),
                       "agree": "Y" if len(confirm) >= 2 else "N", "sec_url": "", "verified_at": "",
                       "notes": "stored-only unit change; vendors show no split" +
                                ("; known 2025-06-24 break" if str(grid[k].date()) == BREAK_DAYS[0] else ""),
                       "tr_agree": "", "sources_confirming": "+".join(confirm), "in_canonical": False, "contra": "stored",
                       "listed": bool(x["listed"][k])})
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
        if not np.isfinite(pct) and ratio:
            pct = 1.0 - 1.0 / ratio[0][3]  # the share of value a distribution served as a ratio takes away
        out.append({"security_id": sid, "ex_date": str(grid[day].date()),
                    "cash": round(float(cash_value), 6) if np.isfinite(cash_value) else "",
                    "prior_close_raw": round(float(prior), 6) if np.isfinite(prior) else "",
                    "pct_of_prior": round(float(pct), 6) if np.isfinite(pct) else "",
                    "classification": kind, "sec_url": "", "ticker": ticker,
                    "ratio": round(float(ratio[0][3]), 6) if ratio else "", "sources": sources,
                    "notes": "; ".join(filter(None, [
                        "pct is 1 - 1/ratio" if ratio and not cash else "",
                        f"total returns differ: {returns}" if np.isfinite(spread) and spread > TOL_R else
                        ("sources agree on the total return" if np.isfinite(spread) else "one source only")])),
                    "listed": bool(x["listed"][day])})
    return out


def moves_of(sid: str, x: dict, ticker_of, *, choice, move_2x, move_big, hidden, flat, zero_vol, level_off, gap,
             stored_kind) -> list[dict]:
    """Draft queue entries (plan 4.4): R1, R1b, R2, R3, R4, R6, R7."""
    grid, idx, agreeing, valid, has, C = x["grid"], x["idx"], x["agreeing"], x["valid"], x["has"], x["C"]
    out = []

    def entry(k, rule, note, agreeing_sources=None):
        sources = agreeing_sources if agreeing_sources is not None else \
            "+".join(SRC[i] for i in range(4) if agreeing[i, k])
        out.append({"ticker": ticker_of(grid[k]), "event_date": str(grid[k].date()), "classification": "unreviewed",
                    "source_url": "", "verified_at": "", "notes": f"[{rule}] {note}", "security_id": sid,
                    "sources_agreeing": sources, "rule": rule, "listed": bool(x["listed"][k])})

    keep = x["keep"]
    tr = x["tr"]
    for k in idx:
        n_agree = int(agreeing[:, k].sum())
        if move_2x[k]:
            rule = "R1/R2" if hidden[k] else "R1"
            note = f"price ratio {1 + tr[k]:.2f}x with no split in any source" if hidden[k] else \
                f"move of {1 + tr[k]:.2f}x"
            if hidden[k]:
                note += f" (fits {near_split_factor(1 / (1 + tr[k])):.4g} split)"
            note += f"; {n_agree} source(s) agree within 0.5%"
            entry(k, rule, note)
        elif move_big[k] and n_agree < 2:
            entry(k, "R1b", f"move of {tr[k]:+.0%} confirmed by no second source")
        if choice["unresolved"][k]:
            values = ", ".join(f"{SRC[i]} {x['r'][i, k]:+.2%}" for i in range(4) if valid[i, k])
            entry(k, "R3", f"sources disagree with no majority: {values}")
        elif stored_kind[k] == "stored_shift":
            values = ", ".join(f"{SRC[i]} {x['r'][i, k]:+.2%}" for i in range(4) if valid[i, k])
            entry(k, "R3", f"stored file shifts against the only vendor and stays (an event the vendor may lack, "
                           f"or a stored adjustment): {values}")
    kept = np.zeros(len(grid), dtype=bool)
    kept[idx] = True
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


def signature_of(sid: str, frames: dict[str, pd.DataFrame], window, mapping: pd.DataFrame) -> str:
    digest = hashlib.sha256(f"{CODE_VERSION}|{CODE_HASH}|{sid}|{window[0].date()}|{window[1].date()}".encode())
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
    if len(canonical):
        write_csv(path, canonical)
    elif path.exists():
        path.unlink()
    days = pd.to_datetime(canonical["date"]).values.astype("datetime64[D]").astype(np.int32) if len(canonical) else \
        np.zeros(0, dtype=np.int32)
    state = {**result, "signature": signature, "dates": days, "built_utc": now_utc()}
    common.atomic_write(state_path(sid), pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL))
    return state


def run_securities(ids: list[str], bundles, identity, windows, sessions, rebuild: bool = False) -> dict[str, dict]:
    mapping, master = identity["mapping"], identity["master"]
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
        signature = signature_of(sid, frames, window, mapping)
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
                   "ticker_of": (lambda day, lookup=lookup, fallback=fallback: lookup(day) or fallback)}
            result = reconcile_security(sid, frames, ctx)
            result["summary"].update(tiingo_self_check(sid, bundles))
            states[sid] = save_result(sid, result, signature)
            built += 1
        if n % 200 == 0 or n == len(ids):
            log(f"securities {n}/{len(ids)}: built {built}, reused {reused} ({time.time() - started:.0f}s)")
    return states


def prepare(only: list[str] | None = None) -> dict:
    """Identity, targets, windows, sessions and the source bundles (cached on disk)."""
    identity = load_identity()
    targets = load_targets()
    ids = set(targets["security_id"])
    windows = security_windows(identity["mapping"], ids)
    sessions = pf.xnas_sessions(WINDOW_START, WINDOW_END)
    log(f"targets: {len(ids)} securities ({int(targets['in_candidates'].sum())} candidates, "
        f"{int(targets['rank300'].sum())} ranked <= 300)")
    sources, facts = load_sources(identity, ids, windows)
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
    waiting = planned[[(s, t) not in answered for s, t in zip(planned["security_id"], planned["ticker_for_source"])]]
    return waiting


def series_ends(states: dict[str, dict], targets: pd.DataFrame, identity: dict, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    """Series that end before the delist date, or before the window end without one (plan 4.5 inputs)."""
    master = identity["master"].set_index("security_id")
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
    for sid, state in states.items():
        summary = state["summary"]
        if not summary.get("rows"):
            continue
        last = summary["last_date"]
        info = master.loc[sid] if sid in master.index else None
        delist = info["delist_date"] if info is not None else ""
        last_listed = info["last_listed"] if info is not None else ""
        if delist:
            category = "ends_before_delist" if last < delist else "reaches_delist"
            target_day = delist
        elif pd.Timestamp(last) < last_session - pd.Timedelta(days=7):
            category = "no_delist_ends_early"
            target_day = str(last_session.date())
        else:
            continue
        before = [d for d in position if d <= target_day]
        short = position[before[-1]] - position.get(last, position[before[-1]]) if before and last in position else ""
        existing = terminal.get(sid, [])
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
        weeks300 = target_info.at[sid, "weeks_rank300"] if sid in target_info.index else 0
        rows.append({"security_id": sid, "ticker_last": summary.get("ticker_last", ""),
                     "name": info["name"] if info is not None else "", "category": category, "last_date": last,
                     "delist_date": delist, "last_listed": last_listed,
                     "successor_security_id": info["successor_security_id"] if info is not None else "",
                     "transfer_date": info["transfer_date"] if info is not None else "",
                     "sessions_after_last_row": short, "likely_cause": cause, "tiingo_pending": pending,
                     "filler_cut": summary.get("filler_cut", 0),
                     "last_src": summary.get("last_src", ""), "existing_terminal_rows": " ".join(existing),
                     "existing_last_price_date_match": ("Y" if any(e.endswith(":" + last) for e in existing) else "N")
                     if existing else "",
                     "in_candidates": bool(target_info.at[sid, "in_candidates"]) if sid in target_info.index else False,
                     "weeks_rank300": int(weeks300) if pd.notna(weeks300) else 0})
    frame = pd.DataFrame(rows)
    return frame.sort_values(["category", "security_id"]) if len(frame) else frame


def dv_cutoffs() -> pd.DataFrame:
    """Per step-6 week: the dv50 and dv20 of the 300th-ranked name (the smallest value ranked <= 300)."""
    weekly = pd.read_pickle(WEEKLY_METRICS)
    cut50 = weekly[weekly["dv50_rank"] <= 300].groupby("week_end")["dv50"].min()
    cut20 = weekly[weekly["dv20_rank"] <= 300].groupby("week_end")["dv20"].min()
    return pd.DataFrame({"cut50": cut50, "cut20": cut20}).sort_index()


def scan_series(ids: list[str], cutoffs: pd.DataFrame) -> dict:
    """One pass over the canonical files: flag counts by type (and type and year), plan 6's check of
    days with two or more sources agreeing within 0.5%, and the weeks where a series' own raw dollar
    volume (median close_raw x volume_raw over 50 / 20 rows) reaches step 6's rank-300 cut (relevance of
    the review queue for names step 6 could not rank). Dollar volume only: no return is aggregated."""
    by_type, by_year = Counter(), defaultdict(Counter)
    multi = Counter()
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
        two = frame["n_sources"] >= 2
        multi["name_days_2plus_sources"] += int(two.sum())
        multi["name_days_2plus_agree_0p5pct"] += int((two & (frame["max_src_diff"] <= TOL_R)).sum())
        multi["name_days_total"] += int(len(frame))
        dv = frame["close_raw"] * frame["volume_raw"]
        dates = pd.to_datetime(frame["date"]).values.astype("datetime64[D]")
        dv50 = dv.rolling(50, min_periods=25).median().values
        dv20 = dv.rolling(20, min_periods=10).median().values
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
            "multi": multi, "dv_weeks": dv_weeks}


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
    securities = pd.DataFrame([{**{k: v for k, v in s["summary"].items() if not isinstance(v, dict)},
                                "rows_by_primary": json.dumps(s["summary"].get("rows_by_primary", {}), sort_keys=True),
                                "flag_counts": json.dumps(s["summary"].get("flag_counts", {}), sort_keys=True)}
                               for s in states.values()])
    write_csv(OUT / "securities.csv", securities)
    by_type, by_year, multi = scan["by_type"], scan["by_year"], scan["multi"]
    checks = known_case_checks(states, split_table, special_table, identity)
    sources_per_security = Counter(len([x for x in s["summary"].get("sources", "").split() if x != "stored"])
                                   for s in states.values())
    summary = {
        "built_utc": now_utc(), "code_version": CODE_VERSION, "scripts_git_commit": git_commit(),
        "rules": {"tol_r": TOL_R, "tol_level": TOL_LEVEL, "tol_ratio": TOL_RATIO, "move_2x": MOVE_2X,
                  "move_big": MOVE_BIG, "special_pct": SPECIAL_PCT, "flat_run": FLAT_RUN,
                  "precedence_to_2017_10_31": PRECEDENCE_EARLY, "precedence_from_2017_11_01": PRECEDENCE_LATE,
                  "stored_vote_excluded": f"unit breaks {list(BREAK_DAYS)}, ex-dates from {STORED_EX_FROM}, "
                                          "split-sized stored jumps"},
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
        "split_events": split_facts, "special_distributions": special_facts, "review_queue": queue_facts,
        "series_ends": {"by_category": {k: int(v) for k, v in ends["category"].value_counts().items()},
                        "by_cause": {f"{c}|{k}": int(v) for (c, k), v in
                                     ends.groupby(["category", "likely_cause"]).size().items()}} if len(ends) else {},
        "filler_rows_cut": int(sum(s["summary"].get("filler_cut", 0) for s in states.values())),
        "non_session_rows_dropped": dict(sum((Counter(s["summary"].get("dropped_non_session", {})) for s in states.values()),
                                             Counter())),
        "tiingo_identity_rows": int(sum(s["summary"].get("tiingo_identity_rows", 0) for s in states.values())),
        "tiingo_run_vs_old_diff_days": int(sum(s["summary"].get("tiingo_run_vs_old_diff_days", 0) for s in states.values())),
        "checks": checks,
        "files": {"prices": str(PRICES_DIR), "split_events": str(SPLIT_EVENTS), "special_distributions": str(SPECIAL),
                  "reviewed_moves": str(REVIEWED_MOVES), "series_ends": str(OUT / "series_ends.csv"),
                  "coverage_gaps": str(OUT / "coverage_gaps.csv"), "no_series": str(OUT / "no_series.csv")},
        "no_returns_aggregated": "tr is per security and day; this summary holds counts only",
    }
    if not args.no_panel:
        summary["panel"] = write_panel(ids, states)
    write_json(OUT / "summary.json", summary)
    return summary


def git_commit() -> str:
    try:
        import subprocess
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--only", default="", help="comma-separated security ids: build these, write no tables")
    parser.add_argument("--rebuild", action="store_true", help="ignore the per-security state")
    parser.add_argument("--no-panel", action="store_true", help="skip CACHE/prices/daily_panel.csv.gz")
    args = parser.parse_args(argv)
    for directory in (PRICES_DIR, OUT, SOURCE_CACHE, STATE_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    only = [s.strip() for s in args.only.split(",") if s.strip()] or None
    started = time.time()
    prep = prepare(only)
    ids = sorted(set(prep["targets"]["security_id"]) if only is None else set(only))
    states = run_securities(ids, prep["bundles"], prep["identity"], prep["windows"], prep["sessions"], args.rebuild)
    if only is not None:
        for sid in ids:
            summary = states[sid]["summary"]
            log(f"{sid}: {summary.get('rows', 0)} rows {summary.get('first_date')}..{summary.get('last_date')} "
                f"{summary.get('rows_by_primary')} flags {summary.get('flag_counts')}")
            log(f"  events {len(states[sid]['events'])}, specials {len(states[sid]['specials'])}, "
                f"queue {Counter(m['rule'] for m in states[sid]['moves'])}")
        return 0
    summary = summarize_tables(states, prep, ids, args)
    log(f"coverage dv50 1-300: {summary['coverage_ranked']['dv50_rank_1_300']['all']}")
    log(f"done in {time.time() - started:.0f}s; summary {OUT / 'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
