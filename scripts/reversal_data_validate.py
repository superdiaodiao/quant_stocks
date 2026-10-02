"""Plan step 14 (docs/reversal_2012_2026_data_plan.md, section 6, with 3.3 and 5.3): every data check,
then ``INPUTS/validation_summary.json`` and ``INPUTS/manifest.json``.

Data only. Nothing here computes signals, strategy or portfolio returns, or long-short spreads, and
nothing averages, ranks or sorts stocks by return. The only returns computed are one security's own
daily returns, each from one source, to compare two sources of the same security on the same day
(the V sample, QQQ's two files) or to see that a known spin-off day is not a price-only drop. Ranks
are by dollar volume or market cap only. The summary holds counts, shares of days or names and
lists of ids, never a return.

Each check is one function ``check_<name>(ctx) -> dict`` with ``passed``, ``status`` (pass / fail /
no_input / error), the plan's threshold as written, the numbers behind it and short lists. A threshold
that is not met is a failure with its numbers; no threshold is loosened here. A check whose input is
missing fails with ``no_input`` and the paths it looked for.

The universe (step 12) is read from ``INPUTS/weekly_universe_top300.csv.gz`` (week_end, security_id,
dv50_rank, dv20_rank, ...). While that file does not exist, the checks that need universe weeks use
the step-6 ranks in ``CACHE/prefilter/weekly_metrics.pkl`` (listed common stocks, foreign filers out,
raw close >= $10 or unknown) and say so in ``basis``; ``universe_build`` then fails. A universe
name-week is a week whose dv50 or dv20 rank is <= 250 (both windows are stored; the protocol picks).

Reads only local files: no request is made, no key is read except to confirm that no key value
appears in the summary or the manifest (the values are never printed or stored).

Inputs that change during a run: the files the test reads are hashed before the checks, and every
file a check reads is hashed from the bytes it parsed. After the checks all of them are hashed again;
if one changed (an upstream step rewrote it), nothing is written and the run exits with 2. The live
files of the running Tiingo fetch (fetch_status.csv, raw_index.csv.gz) are read once, so every check
sees the same copy; their growth is reported. The summary and the manifest are built in memory and
written last, one atomic rename each (``common.update_manifest`` is not used: it re-hashes at write
time and keeps keys of earlier runs). Manifest keys are relative: INPUTS files by name, cache files as
``research_cache/reversal_2012_2026/...``; the other files the checks read are under
``validation_inputs`` with their sha256.

Usage::

    PYTHONPATH=. python scripts/reversal_data_validate.py                  # all checks, summary, manifest
    PYTHONPATH=. python scripts/reversal_data_validate.py --no-manifest    # all checks and the summary
    PYTHONPATH=. python scripts/reversal_data_validate.py --only factor_rows,listing_snapshots   # print only
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import gzip
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

import numpy as np
import pandas as pd

from scripts import reversal_data_common as common

CODE_VERSION = "2026-10-02.2"

# ------------------------------------------------------------------ constants (plan sections 1, 3, 5, 6)

WEEK_FIRST, WEEK_LAST = "2012-01-06", "2026-07-17"      # owner: window weeks
PRICE_START, PRICE_END = "2011-06-01", "2026-08-31"     # plan D3: fetch window
FACTOR_FROM, FACTOR_TO, FACTOR_ROWS = "2012-01-03", "2026-07-17", 3655   # plan 5.3
QQQ_DIVIDENDS = 60                                      # plan 5.3: 2012-01-01 to 2026-07-17
QQQ_TIINGO_SHA = ("3099", "9930")                       # plan 5.3 (first and last 4 hex digits)
QQQ_NASDAQ_SHA = ("ebfb", "d0d0")
KF_BUILD = "202608"
FF49_RANGES = 598
UNIVERSE_N = 250
PRICE_RANK = 300
MIN_PRICE = 10.0
VENDORS = ("tiingo", "yahoo", "wiki")
MISSING_CODES = (-99.99, -999.0)
SNAPSHOT_MAX_AGE = 160
SYMBOL_FILE_MIN_ROWS = 1000
AGREE_TOL = 0.005
AGREE_SHARE = 0.995
V_TOL, V_SHARE, V_NAMES = 1e-4, 0.995, 50
TIINGO_TOL = 1e-8
SPLIT_AGREE = 0.98
SPECIAL_PCT = 0.10
DIV_MATCH, DIV_AMOUNT_TOL, NASDAQ_DIV_SAMPLE, NASDAQ_DIV_FROM = 0.98, 0.001, 100, "2013-08-01"
TERMINAL_UNKNOWN_MAX = 0.05
EARNINGS_COVERAGE, GAP_SHARE, GAP_LO, GAP_HI, HAND_CHECKS = 0.95, 0.90, 60, 120, 20
EARNINGS_FROM = "2011-10-01"
FF49_COVERAGE = 0.99
PROXY_ZERO_SHARE, PROXY_MAX = 0.95, 3                   # plan 3.3 check 1
CAPTURE_TOP, CAPTURE_SHARE = 200, 0.99                  # plan 3.3 check 2
MCAP_COVER_BEFORE_2023, MCAP_COVER_FROM_2023 = 0.97, 0.99
FORM25_FLOAT, FORM25_SESSIONS = 1e9, 5                  # plan 3.3 check 4
UNFILLABLE_SLOT_SHARE = 0.02                            # plan 3.3 check 6
LIST_LIMIT = 40
# step 12's missing_reason values (reversal_data_universe.MISSING_REASONS); these block completeness by themselves
KNOWN_MISSING = {"tiingo_pending", "yahoo_pending", "fetched_pending_reconcile", "unfillable", "no_vendor_source",
                 "series_gap", "candidate_other", "not_candidate", "not_in_step6"}
TIER_BB, TIER_BC_SAMPLE, TIER_BC_REST = "B_B_float_500M_1B", "B_C_sample_300M_500M", "B_C_rest_300M_500M"
FETCH_DATA = {"done", "done_review", "partial"}         # a Tiingo answer with rows
FETCH_EMPTY = {"wrong_entity", "no_data"}               # a Tiingo answer (or range check) with no usable rows
BLOCKING_MISSING = {"series_gap", "fetched_pending_reconcile", "no_vendor_source", "not_in_step6"}
BREAK_DAY = "2025-06-24"                                # plan 4.2 / 6: the stored files' unit break
HOLD_WEEKS = 4                                          # plan D3: a hold of up to 4 weeks after the formation week
CACHE_KEY_PREFIX = "research_cache/reversal_2012_2026/"  # manifest keys of cache files (plan 1.1: relative paths)
# Files the running Tiingo fetch keeps writing: read once per run, their growth during the run is reported only.
LIVE_FILES = ("tiingo/fetch_status.csv", "raw_index.csv.gz", "quota_ledger.csv")

KNOWN_CASES = [  # plan 4.3: (ticker, ex-date, kind, expected split factor S = new shares per old)
    ("EBAY", "2015-07-20", "spinoff_cash", None), ("CTXS", "2017-02-01", "spinoff_cash", None),
    ("LVNTA", "2014-08-28", "spinoff_cash", None), ("DISCK", "2014-08-07", "spinoff_cash", None),
    ("HON", "2025-10-30", "spinoff_ratio", 1.061), ("NUAN", "2019-10-02", "spinoff_ratio", 1.155),
    ("HON", "2026-06-29", "spinoff_ratio", 0.9535),
    ("PRPL", "2026-07-20", "split", 1 / 25), ("SIRI", "2024-09-10", "split", 0.1),
    ("NFLX", "2025-11-17", "split", 10.0), ("BKNG", "2026-04-06", "split", 25.0),
    ("KLAC", "2026-06-12", "split", 10.0), ("MNST", "2026-08-11", "split", 2.0),
]
FORM25_CLASSES = {"common_delisting", "other_class", "reorg", "transfer"}   # plan 1.1
TERMINAL_RESOLVED = {"computed", "no_terminal_return", "awaiting_d5"}
UNIVERSE_SUMMARY_COLUMNS = ["week_end", "n_listed_common", "n_with_vendor_prices", "n_price_ge_10",
                            "cutoff_rank250_dv_bucket", "n_unresolved_candidates", "mcap_weighted_coverage",
                            "snapshot_age_days"]
UNIVERSE_TOP_COLUMNS = ["week_end", "security_id", "ticker", "dv50_rank", "dv20_rank", "price_ge_10"]
FACTOR_FILES = ["ff5_2x3_daily.csv", "mom_daily.csv", "st_rev_daily.csv", "ind49_daily.csv.gz",
                "qqq_joined.csv", "vix_daily.csv", "ff_industry_maps_full.csv"]

# plan 1.1: the committed inputs and their columns (manifest.json and validation_summary.json are written here)
_EARNINGS_COLUMNS = ["cik", "security_id", "accession", "form", "items", "acceptance_json_raw", "acceptance_header_et",
                     "tz_resolution", "filing_date", "d0_session", "first_in_fiscal_quarter", "source"]
PLAN_INPUTS = {
    "listing_snapshots_index.csv": ["snapshot_date", "source", "capture_timestamp", "original_url", "rows", "common_rows",
                                    "has_market_cap", "sha256"],
    "security_master.csv": ["security_id", "cik", "first_ticker", "name", "share_class", "first_listed", "last_listed",
                            "delist_date", "delist_form25_accession", "foreign_filer", "multi_class_group", "price_sources",
                            "identity_notes"],
    "ticker_intervals.csv": ["security_id", "ticker", "start", "end", "exchange", "source", "source_url"],
    "form25_nasdaq_2012_2026.csv": ["filing_date", "effective_date", "accession", "subject_cik", "subject_name", "filer_cik",
                                    "class_of_security", "classification", "public_float_usd", "float_check_flag"],
    "exchange_moves.csv": ["security_id", "ticker", "date", "from_exchange", "to_exchange", "source_url"],
    "candidate_fetch_list.csv": ["security_id", "ticker_for_source", "needed_start", "needed_end", "reason",
                                 "prefilter_metric", "prefilter_value", "planned_source", "tiingo_range_match", "status",
                                 "fetch_month"],
    "split_events.csv": ["security_id", "ticker", "ex_date", "split_factor", "event_type", "tiingo", "yahoo", "wiki",
                         "nasdaq", "agree", "sec_url", "verified_at", "notes"],
    "special_distributions.csv": ["security_id", "ex_date", "cash", "prior_close_raw", "pct_of_prior", "classification",
                                  "sec_url"],
    "reviewed_moves.csv": ["ticker", "event_date", "classification", "source_url", "verified_at", "notes", "security_id",
                           "sources_agreeing"],
    "terminal_returns_2012_2026.csv": ["ticker", "last_price_date", "terminal_return", "consideration_per_share",
                                       "source_url", "verified_at", "security_id", "delist_date", "terminal_type",
                                       "consideration_cash", "consideration_shares", "acquirer_security_id"],
    "earnings_events.csv": _EARNINGS_COLUMNS,
    "earnings_fallback_periodic.csv": _EARNINGS_COLUMNS,   # "the same fields for 10-Q/10-K acceptances"
    "sic_history.csv": ["cik", "observed_date", "sic", "source_accession"],
    "ff_industry_maps.csv": ["scheme", "industry_id", "short_name", "sic_lo", "sic_hi"],
    "weekly_universe_summary.csv": UNIVERSE_SUMMARY_COLUMNS,
    "weekly_universe_top300.csv.gz": UNIVERSE_TOP_COLUMNS,
    "unfillable.csv": ["security_id", "ticker", "needed_start", "needed_end", "sources_tried", "est_weeks_in_top250", "proxy"],
}
PRICE_COLUMNS = ["date", "close_raw", "volume_raw", "split_factor", "div_cash", "tr", "src_primary", "n_sources",
                 "max_src_diff", "flags"]
# plan 1.2: the local files the test reads, and the request logs (schema only)
PLAN_CACHE = {
    "prices/daily_panel.csv.gz": ["security_id"] + PRICE_COLUMNS,
    "dividends.csv": ["security_id", "ex_date", "cash_as_paid", "sources"],
    "universe/weekly_listed.csv.gz": ["week_end", "security_id"],
    "universe/weekly_liquidity.csv.gz": ["week_end", "security_id", "dv20", "dv50"],
}
PLAN_CACHE_INFRA = {
    "raw_index.csv.gz": ["path", "url_redacted", "fetched_utc", "http_status", "bytes", "sha256"],
    "quota_ledger.csv": ["source", "month", "symbol", "request_n", "status", "fetched_utc"],
}
PLAN_COLUMN_MAPPINGS = {   # (file, plan column) -> how the data holds it
    ("raw_index.csv.gz", "path"): "cache_path (the cached response file; reversal_data_common.log_request)",
    ("quota_ledger.csv", "request_n"): "not stored: one row per request, so request_n is the row's order within source and month",
}
PLAN_VALUES = {            # (file, column) -> (the plan's values, documented extensions)
    ("security_master.csv", "foreign_filer"): ({"Y", "N"}, {
        "MIXED": "owner decision D4: foreign only in some periods (regime from periodic_form_history.csv)",
        "UNKNOWN": "no periodic report seen; kept in the universe and listed by the security_master check"}),
    ("listing_snapshots_index.csv", "source"): ({"repo_symdir", "wayback_symdir", "wayback_companylist",
                                                 "commoncrawl_symdir"}, {}),
    ("form25_nasdaq_2012_2026.csv", "classification"): (FORM25_CLASSES, {}),
    ("split_events.csv", "event_type"): ({"split", "reverse_split", "spinoff", "distribution", "unit_break"}, {}),
    ("split_events.csv", "agree"): ({"Y", "N"}, {}),
    ("terminal_returns_2012_2026.csv", "terminal_type"): ({"cash_merger", "stock_merger", "mixed", "liquidation",
                                                           "exchange_move", "bankruptcy_otc", "unknown"}, {}),
    ("earnings_events.csv", "tz_resolution"): ({"header", "json_utc", "json_et_rule"}, {}),
    ("earnings_fallback_periodic.csv", "tz_resolution"): ({"header", "json_utc", "json_et_rule"}, {}),
    ("ff_industry_maps.csv", "scheme"): ({"FF49", "FF17", "FF12"}, {}),
    ("unfillable.csv", "proxy"): ({"mcap", "float"}, {}),
    ("weekly_universe_top300.csv.gz", "price_ge_10"): ({"Y", "N"}, {}),
}
PLAN_VALUE_PREFIXES = {    # (file, column) -> (accepted prefixes, the documented mapping)
    ("candidate_fetch_list.csv", "reason"): (
        ("A1_", "A2_", "A3_", "B_A_", "B_B_", "B_C_", "C_", "V_", "Y_", "S_"),
        "reason holds the 3.2 rule names (A1, A2, B-A, B-B, B-C sample / rest, C, V) and the step-6 extras (A3 float, "
        "S supplements, Y active names for Yahoo) in place of 1.1's older labels (A_delisted_2012_19, ...)"),
}
MANIFEST_PLAN_KEYS = ("generated_utc", "scripts_git_commit", "sources", "sha256", "raw_index_sha256")
MANIFEST_SOURCE_KEYS = ("endpoint_template", "fetched_from", "fetched_to", "requests", "license_note")


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def _num(values) -> pd.Series:
    return pd.to_numeric(pd.Series(values), errors="coerce")


def _round(value, digits: int = 6):
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return None
    return round(float(value), digits)


def _share(numerator, denominator):
    return _round(numerator / denominator) if denominator else None


# ------------------------------------------------------------------ context: paths and lazy loaders

class Context:
    """Where the inputs are and the frames read from them (each read once).

    Every file a check reads goes through ``read_bytes`` / ``sha``, which record the sha256 of the bytes
    used (``hashes``). At the end of the run ``input_changes`` hashes each file again: a file that changed
    while the checks ran (an upstream step rewriting it) blocks the summary and the manifest. The live
    files of the running Tiingo fetch (``LIVE_FILES``) are read once into memory, so every check sees the
    same copy; their later growth is reported, not blocking."""

    def __init__(self, inputs: Path | str = common.INPUTS, cache: Path | str = common.CACHE,
                 main: Path | str = common.MAIN_CHECKOUT, repo: Path | str = "."):
        self.inputs, self.cache, self.main, self.repo = Path(inputs), Path(cache), Path(main), Path(repo)
        self._memo: dict = {}
        self.hashes: dict[str, str] = {}      # str(path) -> sha256 of the bytes this run read
        self.reread_changed: set[str] = set()  # read twice in this run with different bytes

    @property
    def read(self) -> set[str]:
        return set(self.hashes)

    def memo(self, key, build):
        if key not in self._memo:
            self._memo[key] = build()
        return self._memo[key]

    # tracked reads ----------------------------------------------------------------------
    def _record(self, path: Path, digest: str) -> None:
        key = str(path)
        if key in self.hashes and self.hashes[key] != digest:
            self.reread_changed.add(key)
        self.hashes.setdefault(key, digest)

    def read_bytes(self, path: Path | str) -> bytes:
        """The file's bytes; their sha256 is recorded."""
        path = Path(path)
        data = path.read_bytes()
        self._record(path, common.sha256_bytes(data))
        return data

    def sha(self, path: Path | str) -> str:
        """The file's sha256 (streamed), recorded like a read."""
        path = Path(path)
        digest = common.sha256_file(path)
        self._record(path, digest)
        return digest

    def read_frame(self, path: Path | str, **kwargs) -> pd.DataFrame:
        """pd.read_csv on exactly the bytes whose hash is recorded."""
        path = Path(path)
        data = self.read_bytes(path)
        return pd.read_csv(io.BytesIO(data), compression="gzip" if path.suffix == ".gz" else None, **kwargs)

    def read_json(self, path: Path | str):
        return json.loads(self.read_bytes(path).decode("utf-8"))

    def live_frame(self, relative: str) -> pd.DataFrame | None:
        """A live cache file (``LIVE_FILES``) read once for the whole run (None when missing)."""
        def build():
            path = self.cache / relative
            if not path.exists():
                return None
            return self.read_frame(path, dtype=str, keep_default_na=False, on_bad_lines="skip")
        return self.memo(("live", relative), build)

    def key(self, path: Path | str) -> str:
        """The manifest key of a file: INPUTS files by name, cache files as research_cache/reversal_2012_2026/...,
        other repo files relative to the checkout (plan 1.1: relative paths)."""
        path = Path(path)
        for root, prefix in ((self.inputs, ""), (self.cache, CACHE_KEY_PREFIX), (self.main, "")):
            try:
                return prefix + str(path.relative_to(root))
            except ValueError:
                continue
        if not path.is_absolute():
            return str(path)
        try:
            return str(path.relative_to(self.repo.resolve()))
        except ValueError:
            return str(path)

    def is_live(self, path: Path | str) -> bool:
        return any(str(path) == str(self.cache / f) for f in LIVE_FILES)

    def input_changes(self) -> dict:
        """Hash every recorded file again: changed / gone (blocking) and live files that moved on."""
        changed, gone, live = [], [], []
        for key, digest in sorted(self.hashes.items()):
            path = Path(key)
            now = common.sha256_file(path) if path.exists() else None
            if now == digest:
                continue
            entry = {"file": self.key(path), "sha256_read": digest, "sha256_now": now}
            (live if self.is_live(path) else gone if now is None else changed).append(entry)
        changed += [{"file": self.key(k), "note": "read twice in this run with different bytes"}
                    for k in sorted(self.reread_changed) if not self.is_live(k)]
        return {"changed": changed, "gone": gone, "live_moved_on": live,
                "blocking": bool(changed or gone)}

    def path(self, relative: str) -> Path:
        """A path written in the data files: absolute, under the main checkout, or relative to it."""
        text = str(relative)
        cache_prefix = str(common.CACHE)
        if text.startswith(cache_prefix):
            return self.cache / text[len(cache_prefix):].lstrip("/")
        if text.startswith("research_cache/reversal_2012_2026/"):
            return self.cache / text[len("research_cache/reversal_2012_2026/"):]
        path = Path(text)
        return path if path.is_absolute() else self.main / path

    def find(self, relative: str) -> Path:
        """A repo file: from this checkout when it is there (tracked files), else from the main checkout
        (data files kept out of Git, such as stocks_list_dir/nasdaq)."""
        here = self.repo / relative
        return here if here.exists() else self.main / relative

    def csv(self, name: str) -> pd.DataFrame | None:
        """An INPUTS csv as strings (None when missing)."""
        def build():
            path = self.inputs / name
            if not path.exists():
                return None
            return self.read_frame(path, dtype=str, keep_default_na=False)
        return self.memo(("csv", name), build)

    def cache_csv(self, relative: str, **kwargs) -> pd.DataFrame | None:
        def build():
            path = self.cache / relative
            if not path.exists():
                return None
            return self.read_frame(path, dtype=str, keep_default_na=False, **kwargs)
        return self.memo(("cache_csv", relative), build)

    # calendar ---------------------------------------------------------------------------
    @property
    def sessions(self) -> pd.DatetimeIndex:
        def build():
            import exchange_calendars as xcals
            calendar = xcals.get_calendar("XNAS", start="2010-01-04", end="2026-12-31")
            return pd.DatetimeIndex(calendar.sessions).tz_localize(None).normalize()
        return self.memo("sessions", build)

    @property
    def all_week_ends(self) -> pd.DatetimeIndex:
        """The last XNAS session of every calendar week (Monday to Sunday)."""
        def build():
            s = self.sessions
            return pd.DatetimeIndex(pd.Series(s, index=s).groupby(s.to_period("W-SUN")).max().values)
        return self.memo("all_week_ends", build)

    @property
    def week_ends(self) -> pd.DatetimeIndex:
        w = self.all_week_ends
        return w[(w >= WEEK_FIRST) & (w <= WEEK_LAST)]

    def sessions_between(self, start, end) -> pd.DatetimeIndex:
        s = self.sessions
        return s[(s >= pd.Timestamp(start)) & (s <= pd.Timestamp(end))]

    def session_pos(self, dates) -> np.ndarray:
        """Position of each date among XNAS sessions (-1 when it is not a session)."""
        d = pd.DatetimeIndex(pd.to_datetime(pd.Series(dates).values))
        pos = self.sessions.searchsorted(d)
        pos = np.minimum(pos, len(self.sessions) - 1)
        ok = np.asarray(self.sessions[pos] == d)
        return np.where(ok, pos, -1)

    def week_of(self, dates) -> np.ndarray:
        """Index into ``all_week_ends`` of the calendar week holding each date."""
        d = pd.DatetimeIndex(pd.to_datetime(pd.Series(dates).values))
        return self.all_week_ends.searchsorted(d)

    # prices -----------------------------------------------------------------------------
    @property
    def panel(self) -> pd.DataFrame | None:
        def build():
            path = self.cache / "prices" / "daily_panel.csv.gz"
            if not path.exists():
                return None
            log("reading the price panel")
            frame = self.read_frame(path, dtype={"security_id": str, "date": str, "flags": str, "src_primary": str},
                                    keep_default_na=False, na_values=[""])
            frame["flags"] = frame["flags"].fillna("")
            frame["src_primary"] = frame["src_primary"].fillna("")
            frame["pos"] = self.session_pos(frame["date"])
            frame["week"] = self.week_of(frame["date"])
            return frame
        return self.memo("panel", build)

    @property
    def panel_spans(self) -> pd.DataFrame:
        def build():
            p = self.panel
            if p is None:
                return pd.DataFrame(columns=["first", "last"])
            return p.groupby("security_id")["date"].agg(first="min", last="max")
        return self.memo("panel_spans", build)

    @property
    def vendor_weeks(self) -> set:
        """(security_id, week index) pairs with at least one vendor row in that week."""
        def build():
            p = self.panel
            if p is None:
                return set()
            v = p[p["src_primary"].isin(VENDORS)]
            return set(zip(v["security_id"], v["week"]))
        return self.memo("vendor_weeks", build)

    # identity ---------------------------------------------------------------------------
    @property
    def intervals(self) -> pd.DataFrame | None:
        """Dated ticker intervals (the SEC current-ticker rows have no start date and are left out)."""
        def build():
            iv = self.csv("ticker_intervals.csv")
            return None if iv is None else iv[(iv["start"] != "") & (iv["end"] != "")]
        return self.memo("intervals", build)

    def holder_of(self, ticker: str, day: str, slack_days: int = 60) -> str:
        """The security that held ``ticker`` on ``day`` (ticker_intervals), else the nearest one."""
        iv = self.intervals
        if iv is None:
            return ""
        rows = iv[iv["ticker"] == ticker]
        if not len(rows):
            return ""
        inside = rows[(rows["start"] <= day) & (rows["end"] >= day)]
        if len(inside):
            ids = list(dict.fromkeys(inside["security_id"]))
            spans = self.panel_spans
            for sid in ids:  # prefer the one with a price series on that day
                if sid in spans.index and spans.loc[sid, "first"] <= day <= spans.loc[sid, "last"]:
                    return sid
            return ids[0]
        d = pd.Timestamp(day)
        gap = np.minimum((pd.to_datetime(rows["start"]) - d).abs(), (pd.to_datetime(rows["end"]) - d).abs())
        best = gap.idxmin()
        return rows.loc[best, "security_id"] if gap.loc[best] <= pd.Timedelta(days=slack_days) else ""

    @property
    def master(self) -> pd.DataFrame | None:
        return self.csv("security_master.csv")

    # universe ---------------------------------------------------------------------------
    @property
    def prefilter_weekly(self) -> pd.DataFrame | None:
        def build():
            path = self.cache / "prefilter" / "weekly_metrics.pkl"
            if not path.exists():
                return None
            log("reading the step-6 weekly metrics")
            frame = pd.read_pickle(io.BytesIO(self.read_bytes(path)))
            frame["week_end"] = pd.to_datetime(frame["week_end"])
            return frame
        return self.memo("prefilter_weekly", build)

    @property
    def universe(self) -> tuple[pd.DataFrame | None, str]:
        """Ranked names (rank <= 300 by dv50 or dv20) per week, and where they come from."""
        def build():
            path = self.inputs / "weekly_universe_top300.csv.gz"
            if path.exists():
                frame = self.read_frame(path, dtype={"security_id": str, "week_end": str}, keep_default_na=False,
                                        na_values=[""])
                if {"week_end", "security_id", "dv50_rank", "dv20_rank"} <= set(frame.columns):
                    frame["week_end"] = pd.to_datetime(frame["week_end"])
                    return frame, "official: INPUTS/weekly_universe_top300.csv.gz"
            weekly = self.prefilter_weekly
            if weekly is None:
                return None, "none"
            ranked = weekly[weekly["universe"] & ((weekly["dv50_rank"] <= PRICE_RANK) | (weekly["dv20_rank"] <= PRICE_RANK))]
            keep = ["week_end", "security_id", "ticker", "dv50_rank", "dv20_rank", "price_ge_10", "src", "cik"]
            return (ranked[keep].reset_index(drop=True),
                    "provisional: step-6 ranks (CACHE/prefilter/weekly_metrics.pkl); the step-12 universe is not built")
        return self.memo("universe", build)

    def members(self, n: int = UNIVERSE_N) -> pd.DataFrame | None:
        """Universe name-weeks: dv50 or dv20 rank <= n, inside the window weeks."""
        frame, _ = self.universe
        if frame is None:
            return None
        rank = frame[["dv50_rank", "dv20_rank"]].min(axis=1)
        out = frame[(rank <= n) & (frame["week_end"] >= WEEK_FIRST) & (frame["week_end"] <= WEEK_LAST)].copy()
        out["week"] = self.all_week_ends.searchsorted(out["week_end"])
        return out

    @property
    def listed(self) -> tuple[pd.DataFrame | None, str]:
        """Listed common stocks per week with their size proxies (market cap, float)."""
        def build():
            weekly = self.prefilter_weekly
            path = self.cache / "universe" / "weekly_listed.csv.gz"
            if path.exists():
                frame = self.read_frame(path, dtype={"security_id": str, "week_end": str}, keep_default_na=False,
                                        na_values=[""])
                if {"week_end", "security_id"} <= set(frame.columns):
                    frame["week_end"] = pd.to_datetime(frame["week_end"])
                    basis = "official: CACHE/universe/weekly_listed.csv.gz"
                    if "eligible" in frame.columns:   # the universe base: common, no SPAC shell, not foreign that week
                        frame = frame[frame["eligible"].astype(str) == "True"].reset_index(drop=True)
                        basis += " (eligible rows)"
                    want = [c for c in ("mcap", "float_usd", "pf_dv50") if c not in frame.columns]
                    if want and weekly is not None:
                        proxies = weekly[["week_end", "security_id", "mcap", "float_usd", "dv50"]].rename(
                            columns={"dv50": "pf_dv50"})
                        frame = frame.merge(proxies[["week_end", "security_id"] + want], on=["week_end", "security_id"],
                                            how="left")
                        basis += " (size proxies and step-6 dollar volume from CACHE/prefilter/weekly_metrics.pkl)"
                    for column in ("mcap", "float_usd", "pf_dv50"):
                        if column not in frame.columns:
                            frame[column] = np.nan
                    return frame, basis
            if weekly is None:
                return None, "none"
            frame = weekly[weekly["universe"]][["week_end", "security_id", "ticker", "dv50", "dv50_rank",
                                                "mcap", "float_usd", "price_ge_10"]].reset_index(drop=True)
            frame["pf_dv50"] = frame["dv50"]
            return frame, "provisional: step-6 listed set (CACHE/prefilter/weekly_metrics.pkl)"
        return self.memo("listed", build)

    @property
    def unfillable_ids(self) -> set:
        u = self.csv("unfillable.csv")
        return set(u["security_id"]) if u is not None else set()


# ------------------------------------------------------------------ result helpers

def result(name: str, dataset: str, plan: str, threshold: str, passed: bool, numbers: dict | None = None,
           details: dict | None = None, basis: str = "", note: str = "", status: str = "") -> dict:
    return {"check": name, "dataset": dataset, "plan": plan, "threshold": threshold, "passed": bool(passed),
            "status": status or ("pass" if passed else "fail"), "basis": basis, "numbers": numbers or {},
            "details": details or {}, "note": note}


def no_input(name: str, dataset: str, plan: str, threshold: str, missing: list, note: str = "") -> dict:
    return result(name, dataset, plan, threshold, False, details={"missing_inputs": [str(m) for m in missing]},
                  status="no_input", note=note or "an input this check needs does not exist yet")


# ================================================================== factors (plan 5.3, section 6)

def check_factor_rows(ctx: Context) -> dict:
    """Every Ken French daily series: 3,655 rows from 2012-01-03 to 2026-07-17 on the XNAS sessions, no
    missing codes; VIX on the same sessions."""
    name, dataset, plan = "factor_rows", "factors", "6 Factors; 5.3 row check"
    threshold = f"exactly {FACTOR_ROWS} rows {FACTOR_FROM}..{FACTOR_TO} per series, dates = XNAS sessions, no missing codes"
    files = ["ff5_2x3_daily.csv", "mom_daily.csv", "st_rev_daily.csv", "ind49_daily.csv.gz", "vix_daily.csv"]
    missing = [ctx.cache / "factors" / f for f in files if not (ctx.cache / "factors" / f).exists()]
    if missing:
        return no_input(name, dataset, plan, threshold, missing)
    expected = set(ctx.sessions_between(FACTOR_FROM, FACTOR_TO).strftime("%Y-%m-%d"))
    series, bad = {}, []

    def record(label, dates: pd.Series, values: pd.Series, missing_marks: int):
        values = pd.to_numeric(values, errors="coerce")
        present = set(dates)
        rec = {"rows": int(len(dates)), "duplicates": int(dates.duplicated().sum()),
               "missing_codes": int(missing_marks + values.isna().sum() + values.isin(MISSING_CODES).sum()),
               "sessions_missing": len(expected - present), "non_session_dates": len(present - expected)}
        series[label] = rec
        if (rec["rows"] != FACTOR_ROWS or rec["duplicates"] or rec["missing_codes"] or rec["sessions_missing"]
                or rec["non_session_dates"]):
            bad.append(label)

    for file in files[:4]:
        frame = ctx.cache_csv(f"factors/{file}")
        frame = frame[(frame["date"] >= FACTOR_FROM) & (frame["date"] <= FACTOR_TO)]
        keys = ["weighting", "industry_id", "series"] if "weighting" in frame.columns else ["series"]
        for key, group in frame.groupby(keys, sort=False):
            key = key if isinstance(key, tuple) else (key,)
            record(file.split(".")[0] + ":" + ":".join(map(str, key)), group["date"], group["value_pct"],
                   int((group["missing"] != "").sum()))
    vix = ctx.cache_csv("factors/vix_daily.csv")
    vix = vix[(vix["date"] >= FACTOR_FROM) & (vix["date"] <= FACTOR_TO) & (vix["xnas_session"] == "Y")]
    record("vix_daily:close", vix["date"], vix["close"], int((_num(vix["close"].values) <= 0).sum()))
    ind = Counter(label.split(":")[1] for label in series if label.startswith("ind49_daily"))
    counts_ok = (sum(label.startswith("ff5") for label in series) == 6 and "mom_daily:Mom" in series
                 and "st_rev_daily:ST_Rev" in series and ind == Counter({"vw": 49, "ew": 49}))
    return result(name, dataset, plan, threshold, not bad and counts_ok,
                  numbers={"series": len(series), "series_failing": len(bad), "expected_rows": FACTOR_ROWS,
                           "ind49_series_by_weighting": dict(ind)},
                  details={"failing": {k: series[k] for k in bad[:LIST_LIMIT]},
                           "example": {k: series[k] for k in list(series)[:3]}})


def check_factor_hashes(ctx: Context) -> dict:
    """The Ken French zips match the hashes recorded at download (the 202608 build) and the scout copies;
    the tidy files match the hashes recorded when they were written."""
    name, dataset, plan = "factor_hashes", "factors", "6 Factors; 5.3 (202608 build pinned by SHA-256)"
    threshold = "every zip's sha256 = the pinned one, CRSP build 202608, scout copy hash matches, tidy files unchanged"
    path = ctx.cache / "factors" / "kf_sources.json"
    if not path.exists():
        return no_input(name, dataset, plan, threshold, [path])
    sources = ctx.read_json(path)
    rows, bad = [], []
    for zip_name, rec in sources.items():
        if not (isinstance(rec, dict) and zip_name.endswith(".zip") and "sha256" in rec):
            continue
        file = ctx.cache / "raw" / "kf" / zip_name
        actual = ctx.sha(file) if file.exists() else ""
        build_ok = "daily" not in zip_name or rec.get("crsp_build") == KF_BUILD
        scout = rec.get("scout_sha256_matches")
        tidy_ok = None
        if rec.get("tidy_output") and rec.get("tidy_sha256"):
            tidy = ctx.path(rec["tidy_output"])
            tidy_ok = tidy.exists() and ctx.sha(tidy) == rec["tidy_sha256"]
        row = {"zip": zip_name, "sha256_ok": actual == rec["sha256"], "crsp_build": rec.get("crsp_build"),
               "scout_sha256_matches": scout, "tidy_unchanged": tidy_ok}
        rows.append(row)
        if not row["sha256_ok"] or not build_ok or scout is False or tidy_ok is False:
            bad.append(row)
    daily = [r for r in rows if "daily" in r["zip"]]
    passed = not bad and len(daily) == 4 and len(rows) == 7 and all(r["scout_sha256_matches"] for r in daily)
    return result(name, dataset, plan, threshold, passed,
                  numbers={"zips": len(rows), "daily_zips": len(daily), "failing": len(bad),
                           "scout_verified": sum(bool(r["scout_sha256_matches"]) for r in rows)},
                  details={"zips": rows})


def _own_returns(close: pd.Series, split: pd.Series, div: pd.Series) -> pd.Series:
    """One series' own daily total return (C_t S_t + D_t) / C_{t-1} - 1 (plan 4.1)."""
    return (close * split + div) / close.shift(1) - 1.0


def check_qqq_join(ctx: Context) -> dict:
    """QQQ: the two pinned source files, their 2018-2020 overlap within 1e-9 on returns, 60 dividends."""
    name, dataset, plan = "qqq_join", "factors", "6 Factors; 5.3 QQQ total return"
    threshold = ("source sha256 3099...9930 and ebfb...d0d0; 2018-2020 overlap returns within 1e-9; "
                 f"{QQQ_DIVIDENDS} dividends 2012-01-01..2026-07-17; joined dates = XNAS sessions")
    tiingo_path = ctx.find("research_cache/holdout_2011_2019/qqq_tiingo_2010_2020.csv")
    nasdaq_path = ctx.find("output/research_only/qqq_nasdaq_history.csv")
    joined_path = ctx.cache / "factors" / "qqq_joined.csv"
    missing = [p for p in (tiingo_path, nasdaq_path, joined_path) if not p.exists()]
    if missing:
        return no_input(name, dataset, plan, threshold, missing)
    t = ctx.read_frame(tiingo_path, dtype={"date": str})
    n = ctx.read_frame(nasdaq_path, dtype={"date": str})
    sha_t, sha_n = ctx.hashes[str(tiingo_path)], ctx.hashes[str(nasdaq_path)]
    sha_ok = sha_t.startswith(QQQ_TIINGO_SHA[0]) and sha_t.endswith(QQQ_TIINGO_SHA[1]) and \
        sha_n.startswith(QQQ_NASDAQ_SHA[0]) and sha_n.endswith(QQQ_NASDAQ_SHA[1])
    t["r"] = _own_returns(t["close"], t["splitFactor"].fillna(1.0), t["divCash"].fillna(0.0))
    n["r"] = _own_returns(n["close"], pd.Series(1.0, index=n.index), n["cash_dividend"].fillna(0.0))
    both = t[["date", "r"]].merge(n[["date", "r"]], on="date", suffixes=("_t", "_n")).dropna()
    both = both[(both["date"] >= "2018-01-02") & (both["date"] <= "2020-12-31")]
    diff = (both["r_t"] - both["r_n"]).abs()
    joined = ctx.read_frame(joined_path, dtype={"date": str, "in_window": str})
    window = joined[(joined["date"] >= PRICE_START) & (joined["date"] <= PRICE_END)]
    expected = set(ctx.sessions_between(PRICE_START, PRICE_END).strftime("%Y-%m-%d"))
    dates = set(window["date"])
    dividends = joined[(joined["date"] >= "2012-01-01") & (joined["date"] <= "2026-07-17") & (joined["dividend"] > 0)]
    overlap_ok = len(both) > 700 and float(diff.max()) <= 1e-9
    passed = sha_ok and overlap_ok and len(dividends) == QQQ_DIVIDENDS and dates == expected and \
        bool((window["close"] > 0).all())
    return result(name, dataset, plan, threshold, passed,
                  numbers={"overlap_days": int(len(both)), "overlap_max_abs_return_diff": _round(diff.max(), 12),
                           "overlap_days_over_1e-9": int((diff > 1e-9).sum()), "dividends_2012_to_2026_07_17": int(len(dividends)),
                           "joined_sessions_missing": len(expected - dates), "joined_non_session_dates": len(dates - expected),
                           "source_sha256_ok": sha_ok},
                  details={"tiingo_file_sha256": sha_t, "nasdaq_file_sha256": sha_n,
                           "overlap_days_over_1e-9": both.loc[diff > 1e-9, "date"].tolist()[:LIST_LIMIT]},
                  note="2020-09-21 ($0.38824) is missing from Yahoo; Yahoo is not used (plan 5.3)")


def check_ff_industry_maps(ctx: Context) -> dict:
    """Siccodes maps: FF49 598 ranges, no overlaps in any scheme, ids complete, same as the full cache table,
    and the 49-industry file's columns are the FF49 short names."""
    name, dataset, plan = "ff_industry_maps", "factors", "5.2 industry maps"
    threshold = f"FF49 {FF49_RANGES} ranges, ids 1..49; FF17 and FF12 present; no overlapping ranges; matches the cache table"
    maps = ctx.csv("ff_industry_maps.csv")
    full = ctx.cache_csv("factors/ff_industry_maps_full.csv")
    if maps is None or full is None:
        return no_input(name, dataset, plan, threshold, [ctx.inputs / "ff_industry_maps.csv",
                                                         ctx.cache / "factors" / "ff_industry_maps_full.csv"])
    overlaps, inverted, ids = {}, 0, {}
    for scheme, group in maps.groupby("scheme"):
        lo, hi = _num(group["sic_lo"].values).values, _num(group["sic_hi"].values).values
        order = np.argsort(lo, kind="stable")
        lo, hi = lo[order], hi[order]
        inverted += int((lo > hi).sum())
        overlaps[scheme] = int((lo[1:] <= np.maximum.accumulate(hi)[:-1]).sum())
        ids[scheme] = int(group["industry_id"].nunique())
    columns = ["scheme", "industry_id", "short_name", "sic_lo", "sic_hi"]
    same = maps[columns].reset_index(drop=True).equals(full[columns].reset_index(drop=True))
    ff49 = maps[maps["scheme"] == "FF49"]
    ff49_ids = set(_num(ff49["industry_id"].values).dropna().astype(int))
    names_ok = None
    ind = ctx.cache_csv("factors/ind49_daily.csv.gz")
    if ind is not None:
        got = set(zip(ind["industry_id"], ind["series"]))
        want = set(zip(ff49["industry_id"].str.lstrip("0"), ff49["short_name"]))
        names_ok = got == want
    passed = (len(ff49) == FF49_RANGES and ff49_ids == set(range(1, 50)) and {"FF17", "FF12"} <= set(ids)
              and not any(overlaps.values()) and not inverted and same and names_ok is not False)
    return result(name, dataset, plan, threshold, passed,
                  numbers={"ranges_by_scheme": maps["scheme"].value_counts().to_dict(), "industries_by_scheme": ids,
                           "overlapping_ranges": overlaps, "inverted_ranges": inverted, "same_as_cache_table": same,
                           "ind49_columns_are_ff49_names": names_ok})


# ================================================================== listings (section 6, 3.1)

def check_listing_snapshots(ctx: Context) -> dict:
    """Snapshot files intact, counts by year, >= 1,000 rows per symbol file, a snapshot on or before every
    week end; weeks whose snapshot is older than 160 days are listed."""
    name, dataset, plan = "listing_snapshots", "listings", "6 Listings; 3.1 staleness"
    threshold = ("every week has a snapshot; parsed rows >= 1,000 per symbol file; age <= 160 days, "
                 "longer gaps listed (the plan's acceptance)")
    index = ctx.csv("listing_snapshots_index.csv")
    if index is None:
        return no_input(name, dataset, plan, threshold, [ctx.inputs / "listing_snapshots_index.csv"])
    hash_bad, absent = [], []
    for row in index.itertuples(index=False):
        file = ctx.path(row.snapshot_file)
        if not file.exists():
            absent.append(row.snapshot_file)
        elif ctx.sha(file) != row.sha256:
            hash_bad.append(row.snapshot_file)
    symbol = index[index["source"].str.endswith("symdir")]
    rows = _num(symbol["rows"].values)
    small = symbol.loc[(rows < SYMBOL_FILE_MIN_ROWS).values, ["snapshot_date", "source", "rows"]].to_dict("records")
    dates = pd.DatetimeIndex(sorted(pd.to_datetime(index["snapshot_date"].unique())))
    weeks = ctx.week_ends
    pos = dates.searchsorted(weeks, side="right") - 1
    has = pos >= 0
    age = np.where(has, (weeks - dates[np.maximum(pos, 0)]).days, -1)
    old = [(w.strftime("%Y-%m-%d"), int(a), dates[p].strftime("%Y-%m-%d"))
           for w, a, p, h in zip(weeks, age, pos, has) if h and a > SNAPSHOT_MAX_AGE]
    gaps, current = [], None
    for week, a, snap in old:  # one row per stale stretch
        if current and current["snapshot"] == snap:
            current["last_week"], current["max_age_days"] = week, a
        else:
            current = {"snapshot": snap, "first_week": week, "last_week": week, "max_age_days": a}
            gaps.append(current)
    by_year = index.assign(year=index["snapshot_date"].str[:4]).groupby(["year", "source"]).size()
    counts = {}
    for (year, source), count in by_year.items():
        counts.setdefault(year, {})[source] = int(count)
    passed = bool(has.all()) and not small and not hash_bad and not absent
    return result(name, dataset, plan, threshold, passed,
                  numbers={"snapshots": int(len(index)), "weeks": int(len(weeks)), "weeks_with_snapshot": int(has.sum()),
                           "weeks_age_over_160": len(old), "age_le_160_every_week": not old,
                           "max_age_days": int(age.max()) if len(age) else None,
                           "symbol_files": int(len(symbol)), "symbol_files_under_1000_rows": len(small),
                           "files_missing": len(absent), "files_hash_mismatch": len(hash_bad)},
                  details={"snapshots_by_year": counts, "stale_stretches": gaps, "small_symbol_files": small,
                           "files_missing": absent[:LIST_LIMIT], "files_hash_mismatch": hash_bad[:LIST_LIMIT]},
                  note="ages over 160 days are listed, as the plan's acceptance asks; they do not fail the check")


# ================================================================== Form 25 and the security master

def check_form25(ctx: Context) -> dict:
    """The Form 25 list: one row per accession, dates in range, classes from the plan's set, and every
    2020-2026 row of the sue_lt list present."""
    name, dataset, plan = "form25", "form25", "1.1 form25_nasdaq_2012_2026.csv; step 3 cross-check"
    threshold = ("0 duplicate accessions, 0 blank subject CIKs, filing dates 2012-01..2026-09, effective >= filing, "
                 "classification in {common_delisting, other_class, reorg, transfer}; every sue_lt 2020-2026 row found")
    f = ctx.csv("form25_nasdaq_2012_2026.csv")
    if f is None:
        return no_input(name, dataset, plan, threshold, [ctx.inputs / "form25_nasdaq_2012_2026.csv"])
    dup = int(f["accession"].duplicated().sum())
    blank = int((f["subject_cik"] == "").sum())
    out_of_range = int(((f["filing_date"] < "2012-01-01") | (f["filing_date"] > "2026-09-30")).sum())
    both = f[(f["effective_date"] != "") & (f["filing_date"] != "")]
    early = both[both["effective_date"] < both["filing_date"]]
    odd = f[~f["classification"].isin(FORM25_CLASSES)]
    sue_path = ctx.find("output/research_only/sue_lt_2020_2026/inputs/sec_form25_nasdaq_2020_2026.csv")
    sue_missing, sue_rows = None, 0
    if sue_path.exists():
        sue = ctx.read_frame(sue_path, dtype=str, keep_default_na=False)
        sue_rows = len(sue)
        ours = {}
        for row in f.itertuples(index=False):
            for day in (row.filing_date, row.effective_date):
                if day:
                    ours.setdefault(str(int(row.subject_cik)) if row.subject_cik.isdigit() else row.subject_cik,
                                    []).append(pd.Timestamp(day))
        sue_missing = []
        for row in sue.itertuples(index=False):
            cik = str(int(row.cik)) if str(row.cik).isdigit() else str(row.cik)
            days = ours.get(cik, [])
            if not any(abs((d - pd.Timestamp(row.date)).days) <= 3 for d in days):
                sue_missing.append({"date": row.date, "cik": cik, "name": row.name})
    passed = not dup and not blank and not out_of_range and not len(early) and not len(odd) and sue_missing == []
    return result(name, dataset, plan, threshold, passed,
                  numbers={"rows": int(len(f)), "duplicate_accessions": dup, "blank_subject_cik": blank,
                           "filing_date_out_of_range": out_of_range, "effective_before_filing": int(len(early)),
                           "classification_outside_plan_set": int(len(odd)),
                           "by_classification": f["classification"].value_counts().to_dict(),
                           "by_form": f["form"].value_counts().to_dict(),
                           "filed_by_nasdaq": int((f["filer_cik"] == "1354457").sum()),
                           "float_check_flag": f["float_check_flag"].value_counts().to_dict(),
                           "sue_lt_rows": sue_rows, "sue_lt_rows_not_found": None if sue_missing is None else len(sue_missing)},
                  details={"classification_outside_plan_set": odd[["accession", "subject_name", "classification"]]
                           .to_dict("records")[:LIST_LIMIT],
                           "effective_before_filing": early[["accession", "filing_date", "effective_date"]]
                           .to_dict("records")[:LIST_LIMIT],
                           "sue_lt_rows_not_found": (sue_missing or [])[:LIST_LIMIT]},
                  note="the sue_lt list has no accession column, so rows are matched by CIK and a date within 3 days")


def check_security_master(ctx: Context) -> dict:
    """Security master and ticker intervals: unique ids, intervals that point to the master, no ticker held
    by two securities at once; foreign status known for every universe name; ADRs listed."""
    name, dataset, plan = "security_master", "security_master", "1.1 security_master / ticker_intervals; 3.1 ADR flag"
    threshold = ("unique security_id; every interval's security in the master; start <= end; no two securities "
                 "holding one ticker on the same day; no foreign filer (Y) among universe names")
    m, every, iv = ctx.master, ctx.csv("ticker_intervals.csv"), ctx.intervals
    if m is None or iv is None:
        return no_input(name, dataset, plan, threshold, [ctx.inputs / "security_master.csv", ctx.inputs / "ticker_intervals.csv"])
    dup = int(m["security_id"].duplicated().sum())
    orphans = sorted(set(every["security_id"]) - set(m["security_id"]))
    undated = every[(every["start"] == "") | (every["end"] == "")]
    inverted = iv[iv["start"] > iv["end"]]
    overlaps = []
    for ticker, group in iv[iv.duplicated("ticker", keep=False)].groupby("ticker"):
        g = group.sort_values("start")
        rows = list(g[["security_id", "start", "end"]].itertuples(index=False))
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                if b.start > a.end:
                    break
                if a.security_id != b.security_id:
                    overlaps.append({"ticker": ticker, "a": a.security_id, "b": b.security_id,
                                     "from": max(a.start, b.start), "to": min(a.end, b.end)})
    members = ctx.members()
    unknown_in_universe = []
    foreign_in_universe = []
    if members is not None:
        ids = set(members["security_id"])
        status = m.set_index("security_id")["foreign_filer"]
        unknown_in_universe = sorted(i for i in ids if status.get(i, "") in ("", "UNKNOWN"))
        foreign_in_universe = sorted(i for i in ids if status.get(i, "") == "Y")
    adr = m[(m["share_class"] == "ADS") | m["name"].str.contains(r"American Depositary|\bADR\b|\bADS\b", case=False, regex=True)]
    adr_universe = sorted(set(adr["security_id"]) & set(members["security_id"])) if members is not None else []
    passed = not dup and not orphans and not len(inverted) and not overlaps and not foreign_in_universe
    return result(name, dataset, plan, threshold, passed,
                  numbers={"securities": int(len(m)), "intervals": int(len(every)), "dated_intervals": int(len(iv)),
                           "undated_intervals_by_source": undated["source"].value_counts().to_dict(), "duplicate_ids": dup,
                           "interval_orphans": len(orphans), "inverted_intervals": int(len(inverted)),
                           "ticker_overlaps": len(overlaps), "foreign_filer": m["foreign_filer"].value_counts().to_dict(),
                           "universe_names_foreign_unknown": len(unknown_in_universe),
                           "universe_names_foreign_Y": len(foreign_in_universe),
                           "adr_securities": int(len(adr)), "adr_in_universe": len(adr_universe),
                           "multi_class_securities": int((m["multi_class_group"] != "").sum())},
                  details={"ticker_overlaps": overlaps[:LIST_LIMIT], "interval_orphans": orphans[:LIST_LIMIT],
                           "universe_names_foreign_unknown": unknown_in_universe[:LIST_LIMIT],
                           "universe_names_foreign_Y": foreign_in_universe[:LIST_LIMIT],
                           "adr_in_universe": adr_universe[:LIST_LIMIT]},
                  basis=ctx.universe[1],
                  note=("ADRs are listed for the protocol to decide (plan 3.1), and universe names whose foreign status is "
                        "UNKNOWN are listed (they stay in the universe, as the step-12 build keeps them); neither fails this check"))


def check_candidates_resolved(ctx: Context) -> dict:
    """Every candidate for price completion either has a vendor series over its needed window (95% of the
    sessions) or is documented in unfillable.csv."""
    name, dataset, plan = "candidates_resolved", "prices", "3.2 candidate list; step 8 status"
    threshold = "0 candidates (V sample aside) without a vendor series over 95% of the needed sessions and not in unfillable.csv"
    c = ctx.csv("candidate_fetch_list.csv")
    p = ctx.panel
    if c is None or p is None:
        return no_input(name, dataset, plan, threshold, [ctx.inputs / "candidate_fetch_list.csv",
                                                         ctx.cache / "prices" / "daily_panel.csv.gz"])
    rows = c[c["reason"] != "V_verify_sample"].copy()
    by_sid = p[p["pos"] >= 0].groupby("security_id")["pos"].apply(lambda s: np.sort(s.values))
    s = ctx.sessions
    coverage = []
    for row in rows.itertuples(index=False):
        lo = s.searchsorted(pd.Timestamp(max(row.needed_start or PRICE_START, PRICE_START)))
        hi = s.searchsorted(pd.Timestamp(min(row.needed_end or PRICE_END, PRICE_END)), side="right")
        need = max(hi - lo, 0)
        have = by_sid.get(row.security_id)
        got = int(((have >= lo) & (have < hi)).sum()) if have is not None and need else 0
        coverage.append(got / need if need else 1.0)
    rows["coverage"] = coverage
    rows["documented"] = rows["security_id"].isin(ctx.unfillable_ids)
    rows["covered"] = rows["coverage"] >= 0.95
    open_rows = rows[~rows["covered"] & ~rows["documented"]]
    fetch = ctx.live_frame("tiingo/fetch_status.csv")
    tiingo = {} if fetch is None else fetch["status"].value_counts().to_dict()
    return result(name, dataset, plan, threshold, not len(open_rows),
                  numbers={"candidate_rows": int(len(rows)), "covered": int(rows["covered"].sum()),
                           "documented_unfillable": int((~rows["covered"] & rows["documented"]).sum()),
                           "open": int(len(open_rows)),
                           "open_by_planned_source": open_rows["planned_source"].value_counts().to_dict(),
                           "open_by_status": open_rows["status"].value_counts().to_dict(),
                           "open_by_reason": open_rows["reason"].value_counts().to_dict(),
                           "tiingo_fetch_status": tiingo},
                  details={"open": open_rows[["security_id", "ticker_for_source", "reason", "planned_source", "status",
                                              "needed_start", "needed_end"]].assign(
                      coverage=open_rows["coverage"].round(3)).to_dict("records")[:LIST_LIMIT]},
                  note="the Tiingo month-1 run is still filling CACHE/tiingo; rows it has not reached are open")


# ================================================================== earnings and SIC (section 6, 5.1, 5.2)

def _explode_ids(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.assign(security_id=frame["security_id"].str.split()).explode("security_id")
    return out[out["security_id"].notna() & (out["security_id"] != "")]


def _company_quarters(ctx: Context, members: pd.DataFrame) -> pd.DataFrame:
    """Calendar quarters in which a security holds a universe week and is listed the whole quarter."""
    m = ctx.master.set_index("security_id")
    q = members.assign(quarter=members["week_end"].dt.to_period("Q"))[["security_id", "quarter"]].drop_duplicates()
    q = q[q["quarter"] <= pd.Period(WEEK_LAST, "Q") - 1]   # the last quarter is incomplete
    first = pd.to_datetime(q["security_id"].map(m["first_listed"]).replace("", np.nan))
    last_listed = pd.to_datetime(q["security_id"].map(m["last_listed"]).replace("", np.nan))
    delist = pd.to_datetime(q["security_id"].map(m["delist_date"]).replace("", np.nan))
    last = pd.Series(np.fmax(last_listed.values, delist.values), index=q.index)
    starts = q["quarter"].dt.start_time
    ends = q["quarter"].dt.end_time.dt.normalize()
    full = (first <= starts) & ((last >= ends) | last.isna())
    return q[full.values]


def check_earnings_coverage(ctx: Context) -> dict:
    """Domestic company-quarters in the universe with an earnings event (Item 2.02, or a 10-Q/10-K
    fallback) >= 95%."""
    name, dataset, plan = "earnings_coverage", "earnings", "6 Earnings; 5.1"
    threshold = f"share of universe company-quarters with an event (Item 2.02 or fallback) >= {EARNINGS_COVERAGE}"
    events, fallback, members = ctx.csv("earnings_events.csv"), ctx.csv("earnings_fallback_periodic.csv"), ctx.members()
    if events is None or fallback is None or members is None or ctx.master is None:
        return no_input(name, dataset, plan, threshold, [ctx.inputs / "earnings_events.csv",
                                                         ctx.inputs / "earnings_fallback_periodic.csv", "universe"])
    quarters = _company_quarters(ctx, members)
    e = _explode_ids(events[events["event_kind"] != "amendment"])
    fb = _explode_ids(fallback)
    fb_usable = fb[fb["usable_as_announcement"] == "Y"]

    def keys(frame):
        d = pd.to_datetime(frame["d0_session"].replace("", np.nan))
        return set(zip(frame["security_id"], d.dt.to_period("Q")))

    k202, kfb, kfb_usable = keys(e), keys(fb), keys(fb_usable)
    pairs = list(zip(quarters["security_id"], quarters["quarter"]))
    with202 = sum(p in k202 for p in pairs)
    strict = sum(p in k202 or p in kfb_usable for p in pairs)
    lenient = sum(p in k202 or p in kfb for p in pairs)
    quarters = quarters.assign(has=[p in k202 or p in kfb_usable for p in pairs])
    by_year = quarters.assign(year=quarters["quarter"].dt.year).groupby("year")["has"].agg(["size", "sum"])
    misses = quarters[~quarters["has"]]
    share = _share(strict, len(pairs))
    return result(name, dataset, plan, threshold, share is not None and share >= EARNINGS_COVERAGE,
                  numbers={"company_quarters": len(pairs), "with_item202": with202,
                           "with_item202_or_usable_fallback": strict, "share": share,
                           "share_any_fallback": _share(lenient, len(pairs)),
                           "by_year": {int(y): {"quarters": int(r["size"]), "share": _share(r["sum"], r["size"])}
                                       for y, r in by_year.iterrows()}},
                  details={"quarters_without_event": [{"security_id": s, "quarter": str(q)} for s, q in
                                                      zip(misses["security_id"], misses["quarter"])][:LIST_LIMIT]},
                  basis=ctx.universe[1],
                  note=("events: Item 2.02 8-Ks other than amendments; a fallback counts when usable_as_announcement = Y; "
                        "a quarter counts when the security is listed the whole quarter (security_master)"))


def check_earnings_gaps(ctx: Context) -> dict:
    """Gaps between a universe company's quarterly events of 60-120 days for >= 90% of gaps."""
    name, dataset, plan = "earnings_gaps", "earnings", "6 Earnings"
    threshold = f"share of gaps between quarterly events within {GAP_LO}-{GAP_HI} days >= {GAP_SHARE}"
    events, fallback, members = ctx.csv("earnings_events.csv"), ctx.csv("earnings_fallback_periodic.csv"), ctx.members()
    if events is None or fallback is None or members is None:
        return no_input(name, dataset, plan, threshold, [ctx.inputs / "earnings_events.csv", "universe"])
    ids = set(members["security_id"])
    e = _explode_ids(events[events["event_kind"] == "results_release"])
    fb = _explode_ids(fallback[fallback["usable_as_announcement"] == "Y"])
    both = pd.concat([e[["security_id", "d0_session"]], fb[["security_id", "d0_session"]]])
    both = both[both["security_id"].isin(ids) & (both["d0_session"] >= "2012-01-01") & (both["d0_session"] <= WEEK_LAST)]
    both = both.drop_duplicates().sort_values(["security_id", "d0_session"])
    d = pd.to_datetime(both["d0_session"])
    gap = d.groupby(both["security_id"]).diff().dt.days.dropna()
    inside = gap.between(GAP_LO, GAP_HI)
    share = _share(int(inside.sum()), len(gap))
    return result(name, dataset, plan, threshold, share is not None and share >= GAP_SHARE,
                  numbers={"securities": int(both["security_id"].nunique()), "gaps": int(len(gap)), "share_60_120": share,
                           "under_60": int((gap < GAP_LO).sum()), "over_120": int((gap > GAP_HI).sum()),
                           "median_days": _round(gap.median(), 1)},
                  basis=ctx.universe[1], note="quarterly events: results_release Item 2.02 events plus usable fallbacks")


def check_earnings_hand_sample(ctx: Context) -> dict:
    """20 events checked by hand against the companies' IR press releases."""
    name, dataset, plan = "earnings_hand_sample", "earnings", "6 Earnings (20 events by hand)"
    threshold = f">= {HAND_CHECKS} events checked by hand, all matching (INPUTS/earnings_hand_checks.csv)"
    path = ctx.inputs / "earnings_hand_checks.csv"
    if not path.exists():
        return no_input(name, dataset, plan, threshold, [path],
                        note=("no record of the hand check yet: expected columns accession, security_id, ir_url, "
                              "ir_release_et, d0_matches (Y/N), checked_at"))
    frame = ctx.csv("earnings_hand_checks.csv")
    matched = int((frame.get("d0_matches", pd.Series(dtype=str)) == "Y").sum())
    return result(name, dataset, plan, threshold, len(frame) >= HAND_CHECKS and matched == len(frame),
                  numbers={"checked": int(len(frame)), "matching": matched},
                  details={"not_matching": frame[frame.get("d0_matches", "") != "Y"].to_dict("records")[:LIST_LIMIT]})


def check_earnings_header_times(ctx: Context) -> dict:
    """Every universe company's event from 2011-10 has its acceptance time from the filing header; the
    JSON label mismatch rate is reported."""
    name, dataset, plan = "earnings_header_times", "earnings", "6 Earnings (header-resolved acceptance time); 5.1"
    threshold = "100% of the headers fetched (Item 2.02 events and fallbacks of universe companies from 2011-10); JSON mismatch rate reported"
    events, fallback, members = ctx.csv("earnings_events.csv"), ctx.csv("earnings_fallback_periodic.csv"), ctx.members()
    if events is None or fallback is None:
        return no_input(name, dataset, plan, threshold, [ctx.inputs / "earnings_events.csv"])
    rows = pd.concat([events.assign(kind="item202"), fallback.assign(kind="fallback")], ignore_index=True)
    rows = rows[rows["filing_date"] >= EARNINGS_FROM]
    if members is not None:
        ids = set(members["security_id"])
        rows = rows[rows["security_id"].str.split().apply(lambda xs: any(x in ids for x in xs))]
    fetched = rows["header_file"] != ""
    header = (rows["tz_resolution"] == "header") & (rows["acceptance_header_et"] != "")
    labelled = rows[header]
    mismatch = labelled["json_label"].value_counts().to_dict()
    by_year = labelled.assign(y=labelled["filing_date"].str[:4]).groupby("y")["json_label"].apply(
        lambda s: _share(int((s == "et_labelled_z").sum()), len(s))).to_dict()
    share = _share(int(header.sum()), len(rows))
    return result(name, dataset, plan, threshold, bool(len(rows)) and bool(fetched.all()),
                  numbers={"events": int(len(rows)), "headers_fetched": int(fetched.sum()),
                           "share_fetched": _share(int(fetched.sum()), len(rows)),
                           "header_resolved": int(header.sum()), "share_resolved": share,
                           "not_header": rows.loc[~header, "tz_resolution"].value_counts().to_dict(),
                           "json_labels": mismatch,
                           "json_mismatch_rate": _share(mismatch.get("et_labelled_z", 0), len(labelled)),
                           "json_mismatch_rate_by_year": by_year},
                  details={"not_header": rows.loc[~header, ["cik", "accession", "form", "tz_resolution", "header_file",
                                                            "acceptance_json_raw"]].to_dict("records")[:LIST_LIMIT]},
                  basis=ctx.universe[1],
                  note=("json_mismatch_rate: share of header-resolved filings whose JSON time is Eastern time labelled Z; "
                        "rows whose fetched header holds no acceptance time keep the JSON time (listed)"))


def check_sic_ff49(ctx: Context) -> dict:
    """Universe name-weeks with an FF49 code >= 99% (point-in-time SEC header SIC); SIC changes and 6770
    fixes listed."""
    name, dataset, plan = "sic_ff49", "sic_ff49", "6 SIC/FF49; 5.2"
    threshold = f"share of universe name-weeks with an FF49 code from a Siccodes49 range >= {FF49_COVERAGE}"
    sic, maps, members, m = ctx.csv("sic_history.csv"), ctx.csv("ff_industry_maps.csv"), ctx.members(), ctx.master
    if sic is None or maps is None or members is None or m is None:
        return no_input(name, dataset, plan, threshold, [ctx.inputs / "sic_history.csv", ctx.inputs / "ff_industry_maps.csv",
                                                         "universe"])
    w = members[["security_id", "week_end"]].copy()
    w["cik"] = w["security_id"].map(m.set_index("security_id")["cik"]).fillna("")
    w["cik"] = w["cik"].where(w["cik"] != "", w["security_id"].str.split(".").str[0])
    w["cik_n"] = _num(w["cik"].values).values
    h = sic.copy()
    h["cik_n"] = _num(h["cik"].values).values
    h["observed"] = pd.to_datetime(h["observed_date"])
    h["code"] = _num(h["sic"].values).values
    fixed = (h["code"] == 6770) & (h["operating_sic_after_6770"] != "")
    h.loc[fixed, "code"] = _num(h.loc[fixed, "operating_sic_after_6770"].values).values
    h = h.dropna(subset=["cik_n", "code"]).sort_values("observed")
    no_cik = int(w["cik_n"].isna().sum())
    w = w.dropna(subset=["cik_n"]).sort_values("week_end").reset_index(drop=True)
    w["cik_n"] = w["cik_n"].astype(float)
    h["cik_n"] = h["cik_n"].astype(float)
    merged = pd.merge_asof(w, h[["cik_n", "observed", "code"]], left_on="week_end", right_on="observed",
                           by="cik_n", direction="backward")
    merged = pd.concat([merged, pd.DataFrame({"code": [np.nan] * no_cik})], ignore_index=True)
    earliest = h.groupby("cik_n")["code"].first()
    before_first = merged["code"].isna() & merged["cik_n"].isin(earliest.index)
    merged.loc[before_first, "code"] = merged.loc[before_first, "cik_n"].map(earliest)
    ff49 = maps[maps["scheme"] == "FF49"]
    lo, hi = _num(ff49["sic_lo"].values).values, _num(ff49["sic_hi"].values).values
    order = np.argsort(lo)
    lo, hi = lo[order], hi[order]
    code = merged["code"].values
    pos = np.searchsorted(lo, code, side="right") - 1
    hit = (pos >= 0) & ~np.isnan(code)
    hit[hit] = code[hit] <= hi[pos[hit]]
    with_sic = int((~np.isnan(code)).sum())
    unmatched = Counter(int(c) for c in code[~hit & ~np.isnan(code)])
    universe_ciks = set(w["cik_n"].dropna())
    changes = sic[(sic["sic_changed"] == "Y") & _num(sic["cik"].values).isin(universe_ciks).values]
    fixes = sic[(sic["blank_check_6770"] == "Y") & _num(sic["cik"].values).isin(universe_ciks).values]
    share = _share(int(hit.sum()), len(merged))
    return result(name, dataset, plan, threshold, share is not None and share >= FF49_COVERAGE,
                  numbers={"name_weeks": int(len(merged)), "with_sic": with_sic, "with_ff49_range": int(hit.sum()),
                           "share": share, "share_with_unmatched_as_other": _share(with_sic, len(merged)),
                           "weeks_before_first_header": int(before_first.sum()),
                           "unmatched_sic_codes": dict(unmatched.most_common(20)),
                           "sic_changes_universe_ciks": int(len(changes)), "blank_check_6770_rows_universe_ciks": int(len(fixes)),
                           "blank_check_6770_rows_replaced": int(fixed.sum())},
                  details={"sic_changes": changes[["cik", "observed_date", "sic"]].to_dict("records")[:LIST_LIMIT],
                           "blank_check_6770": fixes[["cik", "observed_date", "operating_sic_after_6770"]]
                           .to_dict("records")[:LIST_LIMIT]},
                  basis=ctx.universe[1],
                  note=("SIC at a week = the latest header on or before it, else the earliest (flagged); 6770 replaced by the "
                        "first operating SIC after the merger; a SIC with no Siccodes49 range counts as missing here (the "
                        "'Other' rule of 5.2 is still to be registered; that share is reported too)"))


# ================================================================== prices (section 6, 4.1-4.4)

def check_panel_integrity(ctx: Context) -> dict:
    """The canonical panel: one row per security and session, sessions only, valid raw values, every row
    from a vendor raw source, the same rows as the per-security files."""
    name, dataset, plan = "panel_integrity", "prices", "1.2 prices/daily_panel.csv.gz; 4.1"
    threshold = ("0 duplicate (security, date); 0 non-session dates; dates in 2011-06-01..2026-08-31; close_raw > 0; "
                 "volume_raw >= 0; split_factor > 0; div_cash >= 0; 100% src_primary in tiingo/yahoo/wiki; "
                 "panel = per-security files")
    p = ctx.panel
    if p is None:
        return no_input(name, dataset, plan, threshold, [ctx.cache / "prices" / "daily_panel.csv.gz"])
    dup = int(p.duplicated(["security_id", "date"]).sum())
    non_session = int((p["pos"] < 0).sum())
    out_of_window = int(((p["date"] < PRICE_START) | (p["date"] > PRICE_END)).sum())
    bad_close = int((~(p["close_raw"] > 0)).sum())
    bad_volume = int((~(p["volume_raw"] >= 0)).sum())
    bad_split = int((~(p["split_factor"] > 0)).sum())
    bad_div = int((~(p["div_cash"] >= 0)).sum())
    not_vendor = int((~p["src_primary"].isin(VENDORS)).sum())
    files = {f.stem: f for f in (ctx.cache / "prices").glob("*.csv")}
    panel_ids = set(p["security_id"])
    rows_per_id = p.groupby("security_id").size()
    mismatched = []
    for sid, path in files.items():
        if sid in panel_ids:
            data = ctx.read_bytes(path)
            lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0) - 1
            if lines != rows_per_id[sid]:
                mismatched.append({"security_id": sid, "file_rows": lines, "panel_rows": int(rows_per_id[sid])})
    only_files, only_panel = sorted(set(files) - panel_ids), sorted(panel_ids - set(files))
    passed = not any([dup, non_session, out_of_window, bad_close, bad_volume, bad_split, bad_div, not_vendor,
                      mismatched, only_files, only_panel])
    return result(name, dataset, plan, threshold, passed,
                  numbers={"rows": int(len(p)), "securities": len(panel_ids), "duplicates": dup,
                           "non_session_dates": non_session, "outside_window": out_of_window, "close_not_positive": bad_close,
                           "volume_negative_or_blank": bad_volume, "split_factor_not_positive": bad_split,
                           "div_cash_negative_or_blank": bad_div, "src_not_vendor": not_vendor,
                           "rows_by_source": p["src_primary"].value_counts().to_dict(),
                           "files": len(files), "files_not_in_panel": len(only_files), "panel_ids_without_file": len(only_panel),
                           "files_with_other_row_count": len(mismatched)},
                  details={"files_not_in_panel": only_files[:LIST_LIMIT], "panel_ids_without_file": only_panel[:LIST_LIMIT],
                           "files_with_other_row_count": mismatched[:LIST_LIMIT]})


def _tiingo_frame(ctx: Context, raw: Path) -> pd.DataFrame:
    data = ctx.read_bytes(raw)
    rows = json.loads(gzip.decompress(data) if raw.suffix == ".gz" else data)
    if isinstance(rows, dict):
        rows = rows.get("prices", [])
    return pd.DataFrame(rows)


def adj_identity_error(frame: pd.DataFrame) -> np.ndarray:
    """Per row, |adjClose_t / adjClose_{t-1} / ((close_t S_t + D_t) / close_{t-1}) - 1| (plan 4.2)."""
    if len(frame) < 2:
        return np.array([])
    close, adj = frame["close"].astype(float).values, frame["adjClose"].astype(float).values
    split = frame["splitFactor"].astype(float).fillna(1.0).values
    div = frame["divCash"].astype(float).fillna(0.0).values
    with np.errstate(divide="ignore", invalid="ignore"):
        implied = (close[1:] * split[1:] + div[1:]) / close[:-1]
        err = np.abs((adj[1:] / adj[:-1]) / implied - 1.0)
    return err[np.isfinite(err)]


def check_tiingo_row_check(ctx: Context) -> dict:
    """Tiingo row check: adjClose ratios equal the raw formula within 1e-8 in every Tiingo file used."""
    name, dataset, plan = "tiingo_row_check", "prices", "6 Prices (Tiingo row check); 4.2"
    threshold = f"max |adjClose ratio / formula - 1| <= {TIINGO_TOL} over the Tiingo files used; 0 panel rows flagged tiingo_adj_identity"
    status_path = ctx.cache / "tiingo" / "fetch_status.csv"
    status = ctx.live_frame("tiingo/fetch_status.csv")
    if status is None:
        return no_input(name, dataset, plan, threshold, [status_path])
    used = status[status["status"].isin(["done", "done_review", "partial"])]
    per_file, worst, rows_checked = [], 0.0, 0
    unreadable = []
    for row in used.itertuples(index=False):
        raw = ctx.path(row.raw_path) if row.raw_path else None
        if raw is None or not raw.exists():
            unreadable.append(row.ticker_for_source)
            continue
        try:
            err = adj_identity_error(_tiingo_frame(ctx, raw))
        except (ValueError, KeyError, OSError) as exc:
            unreadable.append(f"{row.ticker_for_source}: {type(exc).__name__}")
            continue
        rows_checked += len(err)
        top = float(err.max()) if len(err) else 0.0
        worst = max(worst, top)
        if top > TIINGO_TOL:
            per_file.append({"ticker": row.ticker_for_source, "security_id": row.security_id, "max_err": top,
                             "rows_over": int((err > TIINGO_TOL).sum())})
    flagged = 0
    flagged_ids = []
    if ctx.panel is not None:
        hit = ctx.panel["flags"].str.contains("tiingo_adj_identity", regex=False)
        flagged = int(hit.sum())
        flagged_ids = ctx.panel.loc[hit, "security_id"].value_counts().head(LIST_LIMIT).to_dict()
    passed = worst <= TIINGO_TOL and not unreadable and flagged == 0 and len(used) > 0
    return result(name, dataset, plan, threshold, passed,
                  numbers={"files_used": int(len(used)), "rows_checked": rows_checked, "max_err": worst,
                           "files_over": len(per_file), "files_unreadable": len(unreadable),
                           "panel_rows_flagged_tiingo_adj_identity": flagged,
                           "fetch_status_rows": int(len(status)), "fetch_status": status["status"].value_counts().to_dict()},
                  details={"files_over": per_file[:LIST_LIMIT], "unreadable": unreadable[:LIST_LIMIT],
                           "panel_flagged_by_security": flagged_ids},
                  note=("month-1 files (fetch_status done/done_review/partial) are recomputed here; older Tiingo caches "
                        "the reconcile step also reads are covered by its per-row flag in the panel"))


def _series_ends(ctx: Context) -> dict:
    """security -> the last day its series is needed: the delisting date, or the last Nasdaq price of its
    terminal row (the terminal value covers the days after it; an exchange move does not end the series).
    An end is ignored when ticker_intervals shows a later Nasdaq interval (a name that listed again)."""
    def build():
        ends = {}
        m = ctx.master
        if m is not None:
            ends = {sid: d for sid, d in zip(m["security_id"], m["delist_date"]) if d}
        terminal = ctx.csv("terminal_returns_2012_2026.csv")
        if terminal is not None:
            rows = terminal[(terminal["terminal_type"] != "exchange_move") & (terminal["last_price_date"] != "")]
            for sid, day in zip(rows["security_id"], rows["last_price_date"]):
                ends[sid] = min(ends.get(sid, day), day)
        iv = ctx.intervals
        relisted = {}
        if iv is not None and "exchange" in iv.columns:
            nasdaq = iv[iv["exchange"].str.upper() == "NASDAQ"]
            later = nasdaq.groupby("security_id")["start"].max()
            relisted = {sid: day for sid, day in ends.items() if sid in later.index and later[sid] > day}
        return {sid: day for sid, day in ends.items() if sid not in relisted}, relisted
    return ctx.memo("series_ends", build)


def _universe_name_days(ctx: Context, hold_weeks: int = HOLD_WEEKS) -> pd.DataFrame | None:
    """(security_id, session position) for each universe name-week: the sessions of that week and of the
    ``hold_weeks`` weeks after it (plan D3: a hold of up to 4 weeks), those after the security's end left
    out (``_series_ends``)."""
    members = ctx.members()
    if members is None:
        return None
    weeks = ctx.all_week_ends
    pos_week = ctx.session_pos(weeks)               # every week end is a session
    prev = np.concatenate([[-1], pos_week[:-1]])
    idx = members["week"].values
    lo, hi = prev[idx] + 1, pos_week[np.minimum(idx + hold_weeks, len(pos_week) - 1)]
    counts = hi - lo + 1
    sid = np.repeat(members["security_id"].values, counts)
    pos = np.concatenate([np.arange(a, b + 1) for a, b in zip(lo, hi)]) if len(lo) else np.array([], dtype=int)
    week = np.repeat(idx, counts)
    frame = pd.DataFrame({"security_id": sid, "pos": pos, "week": week}).drop_duplicates(["security_id", "pos"])
    last_session = ctx.sessions.searchsorted(pd.Timestamp(PRICE_END), side="right") - 1
    frame = frame[frame["pos"] <= last_session]
    ends, _ = _series_ends(ctx)
    end = pd.to_datetime(frame["security_id"].map(ends))
    end_pos = ctx.sessions.searchsorted(end, side="right") - 1
    frame = frame[end.isna().values | (frame["pos"].values <= end_pos)]
    return frame


def check_universe_vendor_source(ctx: Context) -> dict:
    """Every universe name-day has a canonical row from a vendor raw source."""
    name, dataset, plan = "universe_vendor_source", "universe", "6 Universe (every name-day from a vendor raw source)"
    threshold = (f"100% of universe name-days (the formation week and the {HOLD_WEEKS} weeks after it, plan D3) have a "
                 "panel row whose src_primary is tiingo, yahoo or wiki")
    days, p = _universe_name_days(ctx), ctx.panel
    if days is None or p is None:
        return no_input(name, dataset, plan, threshold, ["universe", ctx.cache / "prices" / "daily_panel.csv.gz"])
    have = p.loc[p["src_primary"].isin(VENDORS) & (p["pos"] >= 0), ["security_id", "pos"]].assign(ok=True)

    def covered(frame):
        out = frame.merge(have, on=["security_id", "pos"], how="left")
        out["ok"] = out["ok"].fillna(False).astype(bool)
        return out

    merged = covered(days)
    one_week = covered(_universe_name_days(ctx, hold_weeks=1))
    merged["year"] = ctx.sessions[merged["pos"].values].year
    by_year = merged.groupby("year")["ok"].agg(["size", "sum"])
    missing = merged[~merged["ok"]]
    worst = missing.groupby("security_id").agg(name_days=("pos", "size"), first=("pos", "min"), last=("pos", "max"))
    worst = worst.sort_values("name_days", ascending=False)
    unfillable = missing["security_id"].isin(ctx.unfillable_ids)
    _, relisted = _series_ends(ctx)
    return result(name, dataset, plan, threshold, bool(len(merged)) and not len(missing),
                  numbers={"name_days": int(len(merged)), "with_vendor_row": int(merged["ok"].sum()),
                           "share": _share(int(merged["ok"].sum()), len(merged)), "missing": int(len(missing)),
                           "missing_securities": int(missing["security_id"].nunique()),
                           "missing_name_days_on_unfillable_names": int(unfillable.sum()),
                           "hold_weeks": HOLD_WEEKS,
                           "one_week_hold": {"name_days": int(len(one_week)), "missing": int((~one_week["ok"]).sum()),
                                             "share": _share(int(one_week["ok"].sum()), len(one_week))},
                           "ends_ignored_for_later_listing": len(relisted),
                           "by_year": {int(y): {"name_days": int(r["size"]), "share": _share(r["sum"], r["size"])}
                                       for y, r in by_year.iterrows()}},
                  details={"missing_by_security": [
                      {"security_id": s, "name_days": int(r["name_days"]),
                       "first": ctx.sessions[int(r["first"])].strftime("%Y-%m-%d"),
                       "last": ctx.sessions[int(r["last"])].strftime("%Y-%m-%d"), "unfillable": s in ctx.unfillable_ids}
                      for s, r in worst.head(LIST_LIMIT).iterrows()],
                      "ends_ignored_for_later_listing": dict(list(relisted.items())[:LIST_LIMIT])},
                  basis=ctx.universe[1],
                  note=(f"name-days: the sessions of each universe week and of the {HOLD_WEEKS} weeks after it, up to the "
                        "delisting date or the terminal row's last price (ignored when the name listed on Nasdaq again "
                        "later); one_week_hold gives the same count for the formation week and the next week only"))


def check_multi_source_agreement(ctx: Context) -> dict:
    """Days with two or more sources: the returns agree within 0.5% on >= 99.5% of name-days, and no day is
    left unresolved after the majority vote."""
    name, dataset, plan = "multi_source_agreement", "prices", "6 Prices (two or more sources); 4.4 R3"
    threshold = f"share of name-days with n_sources >= 2 and max_src_diff <= {AGREE_TOL} >= {AGREE_SHARE} (panel and universe days); 0 disagree_unresolved"
    p = ctx.panel
    if p is None:
        return no_input(name, dataset, plan, threshold, [ctx.cache / "prices" / "daily_panel.csv.gz"])
    multi = p[p["n_sources"] >= 2]
    agree = multi["max_src_diff"] <= AGREE_TOL
    unresolved = p["flags"].str.contains("disagree_unresolved", regex=False)
    share = _share(int(agree.sum()), len(multi))
    year = multi["date"].str[:4]
    by_year = agree.groupby(year).agg(["size", "sum"])
    universe_share, universe_unresolved, universe_days = None, None, 0
    days = _universe_name_days(ctx)
    if days is not None:
        sub = p.merge(days[["security_id", "pos"]], on=["security_id", "pos"], how="inner")
        sub_multi = sub[sub["n_sources"] >= 2]
        universe_days = int(len(sub_multi))
        universe_share = _share(int((sub_multi["max_src_diff"] <= AGREE_TOL).sum()), len(sub_multi))
        universe_unresolved = int(sub["flags"].str.contains("disagree_unresolved", regex=False).sum())
    passed = (share is not None and share >= AGREE_SHARE and int(unresolved.sum()) == 0
              and (universe_share is None or universe_share >= AGREE_SHARE))
    return result(name, dataset, plan, threshold, passed,
                  numbers={"name_days": int(len(p)), "name_days_2plus_sources": int(len(multi)),
                           "agree_within_0p5pct": int(agree.sum()), "share": share,
                           "disagree_unresolved": int(unresolved.sum()),
                           "universe_name_days_2plus_sources": universe_days, "universe_share": universe_share,
                           "universe_disagree_unresolved": universe_unresolved,
                           "by_year": {y: {"days": int(r["size"]), "share": _share(r["sum"], r["size"])}
                                       for y, r in by_year.iterrows()}},
                  details={"unresolved_by_year": p.loc[unresolved, "date"].str[:4].value_counts().sort_index().to_dict(),
                           "unresolved_by_security": p.loc[unresolved, "security_id"].value_counts().head(LIST_LIMIT).to_dict()},
                  basis=ctx.universe[1],
                  note="sources: Tiingo, Yahoo, WIKI and the stored file's vote where valid (n_sources as the reconcile step counts)")


def _source_returns(frame: pd.DataFrame, close: str, split: str, div: str, sessions: pd.DatetimeIndex) -> pd.Series:
    """One source's own daily returns, indexed by date string, only between consecutive XNAS sessions."""
    f = frame.sort_values("date").reset_index(drop=True)
    pos = sessions.searchsorted(pd.to_datetime(f["date"]))
    r = _own_returns(f[close].astype(float), f[split].astype(float).fillna(1.0), f[div].astype(float).fillna(0.0))
    consecutive = np.concatenate([[False], np.diff(pos) == 1])
    return pd.Series(np.where(consecutive, r, np.nan), index=f["date"].values).dropna()


def _tiingo_files(ctx: Context) -> dict:
    """security_id -> its month-1 Tiingo prices csv (fetch_status rows done/done_review/partial)."""
    status = ctx.live_frame("tiingo/fetch_status.csv")
    if status is None:
        return {}
    used = status[status["status"].isin(["done", "done_review", "partial"]) & (status["prices_path"] != "")]
    return {row.security_id: ctx.path(row.prices_path) for row in used.itertuples(index=False)
            if ctx.path(row.prices_path).exists()}


def _yahoo_file(ctx: Context, sid: str) -> Path | None:
    path = ctx.cache / "yahoo" / f"{sid}.csv.gz"
    return path if path.exists() else None


def check_v_sample(ctx: Context) -> dict:
    """Active names: Yahoo-derived returns against Tiingo returns for the 50-name V sample."""
    name, dataset, plan = "v_sample", "prices", "6 Prices (V sample); 3.2 rule V"
    threshold = f"all {V_NAMES} V names compared; |delta r| <= {V_TOL} on >= {V_SHARE} of days (the rest explained by hand)"
    c = ctx.csv("candidate_fetch_list.csv")
    if c is None:
        return no_input(name, dataset, plan, threshold, [ctx.inputs / "candidate_fetch_list.csv"])
    v = c[c["reason"] == "V_verify_sample"]
    tiingo = _tiingo_files(ctx)
    per, days, within, over = [], 0, 0, []
    for row in v.itertuples(index=False):
        t_path, y_path = tiingo.get(row.security_id), _yahoo_file(ctx, row.security_id)
        if t_path is None or y_path is None:
            per.append({"security_id": row.security_id, "ticker": row.ticker_for_source,
                        "compared": False, "tiingo": t_path is not None, "yahoo": y_path is not None})
            continue
        t = ctx.read_frame(t_path, dtype={"date": str})
        y = ctx.read_frame(y_path, dtype={"date": str, "junction": str}, keep_default_na=False)
        junction = set(y.loc[y["junction"] == "Y", "date"])
        rt = _source_returns(t, "close", "splitFactor", "divCash", ctx.sessions)
        ry = _source_returns(y, "close_raw", "split_factor", "div_cash", ctx.sessions)
        both = pd.concat([rt.rename("t"), ry.rename("y")], axis=1, join="inner")
        both = both[~both.index.isin(junction)]
        diff = (both["t"] - both["y"]).abs()
        days += len(both)
        within += int((diff <= V_TOL).sum())
        over += [{"security_id": row.security_id, "date": d} for d in diff[diff > V_TOL].index[:5]]
        per.append({"security_id": row.security_id, "ticker": row.ticker_for_source, "compared": True,
                    "days": int(len(both)), "share_1e4": _share(int((diff <= V_TOL).sum()), len(both))})
    compared = sum(r["compared"] for r in per)
    share = _share(within, days)
    passed = compared == V_NAMES == len(v) and share is not None and share >= V_SHARE
    return result(name, dataset, plan, threshold, passed,
                  numbers={"v_names": int(len(v)), "compared": compared, "days": days, "within_1e-4": within, "share": share},
                  details={"names": per[:V_NAMES], "days_over_1e-4": over[:LIST_LIMIT]},
                  note="Yahoo junction rows are left out (they carry another history's S and D); each source's own returns only")


def check_review_queue(ctx: Context) -> dict:
    """R1-R9 review queue: 0 open items; counts by rule and year."""
    name, dataset, plan = "review_queue", "prices", "6 Prices (R1-R9 queues); 4.4"
    threshold = "0 open (unreviewed) items in reviewed_moves.csv"
    r = ctx.csv("reviewed_moves.csv")
    if r is None:
        return no_input(name, dataset, plan, threshold, [ctx.inputs / "reviewed_moves.csv"])
    rule = r["notes"].str.extract(r"^\[([^\]]+)\]", expand=False).fillna("none")
    open_ = r["classification"].isin(["", "unreviewed"])
    by_rule_year = {}
    for (k, y), n in pd.Series(1, index=[rule, r["event_date"].str[:4]]).groupby(level=[0, 1]).sum().items():
        by_rule_year.setdefault(k, {})[y] = int(n)
    return result(name, dataset, plan, threshold, not open_.any(),
                  numbers={"items": int(len(r)), "open": int(open_.sum()), "by_rule": rule.value_counts().to_dict(),
                           "open_by_rule": rule[open_].value_counts().to_dict(),
                           "by_classification": r["classification"].value_counts().to_dict()},
                  details={"by_rule_and_year": by_rule_year})


def check_stored_comparison(ctx: Context) -> dict:
    """Stored-file comparison: days more than 0.5% apart by year; the 2025-06-24 unit break and the
    switch to price-only dividends from 2023 must show."""
    name, dataset, plan = "stored_comparison", "prices", "6 Prices (stored-file comparison); 4.2"
    threshold = ("counts by year reported; the 2025-06-24 cluster appears: every security on step 6's list of stored "
                 "files that break on 2025-06-24 and is priced that day has its stored vote left out (stored_excluded) "
                 "or a unit_break row on that day, and at least one unit_break row is on that day; stored votes are left "
                 "out on ex-dates from 2023 (price-only dividends)")
    p, splits = ctx.panel, ctx.csv("split_events.csv")
    summary = ctx.cache / "prefilter" / "prefilter_summary.json"
    if p is None or splits is None or not summary.exists():
        return no_input(name, dataset, plan, threshold, [ctx.cache / "prices" / "daily_panel.csv.gz",
                                                         ctx.inputs / "split_events.csv", summary])
    flags = p["flags"]
    year = p["date"].str[:4]
    differs = flags.str.contains(r"stored_disagrees|stored_glitch|stored_shift", regex=True)
    excluded = flags.str.contains("stored_excluded", regex=False)
    ex = p["div_cash"] > 0
    ex_excluded = (ex & excluded).groupby(year).sum()
    ex_all = ex.groupby(year).sum()
    share_ex = {y: _share(int(ex_excluded.get(y, 0)), int(n)) for y, n in ex_all.items() if n}
    before = [share_ex[y] for y in share_ex if y < "2023" and share_ex[y] is not None]
    after = [share_ex[y] for y in share_ex if "2023" <= y <= "2025" and share_ex[y] is not None]
    breaks = splits[(splits["event_type"] == "unit_break")]
    cluster = breaks[breaks["ex_date"] == BREAK_DAY]
    break_ids = set(cluster["security_id"])
    listed_files = ctx.read_json(summary).get("stored_break_files_2025_06_24", [])
    on_day = p[p["date"] == BREAK_DAY].drop_duplicates("security_id").set_index("security_id")["flags"]
    per, unmapped, not_priced, exceptions = {}, [], [], []
    for ticker in listed_files:
        sid = ctx.holder_of(str(ticker).upper(), BREAK_DAY)
        if not sid:
            unmapped.append(ticker)
            continue
        if sid in per:
            per[sid]["files"].append(ticker)
            continue
        if sid not in on_day.index:
            not_priced.append({"file": ticker, "security_id": sid})
            per[sid] = {"files": [ticker], "priced": False}
            continue
        row = {"security_id": sid, "files": [ticker], "priced": True,
               "stored_excluded": "stored_excluded" in on_day[sid], "unit_break_row": sid in break_ids}
        per[sid] = row
        if not (row["stored_excluded"] or row["unit_break_row"]):
            exceptions.append(row)
    priced = [r for r in per.values() if r["priced"]]
    no_break_row = [r for r in priced if not r["unit_break_row"]]
    switch_shows = bool(after) and min(after) >= 0.5 and (not before or max(before) < 0.1)
    cluster_shows = bool(listed_files) and bool(priced) and not exceptions and len(cluster) > 0
    passed = cluster_shows and switch_shows
    return result(name, dataset, plan, threshold, passed,
                  numbers={"days_stored_differs_by_year": differs.groupby(year).sum().astype(int).to_dict(),
                           "unit_breaks": int(len(breaks)), "unit_breaks_2025_06_24": int(len(cluster)),
                           "prefilter_stored_break_files_2025_06_24": len(listed_files),
                           "break_list_securities": len(per), "break_list_unmapped": len(unmapped),
                           "break_list_not_priced_that_day": len(not_priced),
                           "break_list_priced": len(priced),
                           "break_list_priced_stored_excluded": sum(r["stored_excluded"] for r in priced),
                           "break_list_priced_with_unit_break_row": sum(r["unit_break_row"] for r in priced),
                           "break_list_priced_exceptions": len(exceptions),
                           "panel_rows_2025_06_24": int(len(on_day)),
                           "panel_rows_2025_06_24_stored_excluded": int(on_day.str.contains("stored_excluded", regex=False).sum()),
                           "ex_dates_with_stored_vote_excluded_share_by_year": share_ex,
                           "cluster_shows": cluster_shows, "switch_shows": switch_shows},
                  details={"unit_breaks_2025_06_24": cluster[["security_id", "ticker"]].to_dict("records")[:LIST_LIMIT],
                           "exceptions": exceptions[:LIST_LIMIT],
                           "priced_without_unit_break_row": [{"security_id": r["security_id"], "files": r["files"]}
                                                             for r in no_break_row][:LIST_LIMIT],
                           "not_priced_that_day": not_priced[:LIST_LIMIT], "unmapped_files": unmapped[:LIST_LIMIT]},
                  note=("the plan's 69 files count the whole stored directory; step 6 lists the break files it read, and "
                        "each is mapped to the security holding that ticker on 2025-06-24 (ticker_intervals); one not "
                        "priced that day is outside this study. A priced one without a unit_break row is listed (the split "
                        "table misses it) but passes when its stored vote is left out. 'Switch shows': at least half of "
                        "the 2023-2025 ex-dates and under a tenth of the earlier ones have the stored vote left out"))


# ================================================================== splits and distributions (4.3, section 6)

def check_split_agreement(ctx: Context) -> dict:
    """Split events: >= 98% agree across sources, every disagreement reviewed, every distribution or spin-off
    and every distribution above 10% with an SEC URL."""
    name, dataset, plan = "split_agreement", "splits", "6 Splits; 4.3"
    threshold = (f"agree share >= {SPLIT_AGREE} (unit breaks aside); 100% of disagreements reviewed (verified_at); "
                 "every distribution/spin-off and every distribution > 10% of the prior close has an SEC URL")
    s, sp = ctx.csv("split_events.csv"), ctx.csv("special_distributions.csv")
    if s is None or sp is None:
        return no_input(name, dataset, plan, threshold, [ctx.inputs / "split_events.csv", ctx.inputs / "special_distributions.csv"])
    events = s[s["event_type"] != "unit_break"]
    agree = events["agree"] == "Y"
    vendor_values = (events[["tiingo", "yahoo", "wiki", "nasdaq"]] != "").sum(axis=1)
    multi = events[vendor_values >= 2]
    disagreements = events[~agree]
    unreviewed = disagreements[disagreements["verified_at"] == ""]
    needs_url = events[events["event_type"].isin(["distribution", "spinoff"])]
    no_url = needs_url[needs_url["sec_url"] == ""]
    big = sp[_num(sp["pct_of_prior"].values).values > SPECIAL_PCT]
    big_no_url = big[big["sec_url"] == ""]
    share = _share(int(agree.sum()), len(events))
    passed = share is not None and share >= SPLIT_AGREE and not len(unreviewed) and not len(no_url) and not len(big_no_url)
    return result(name, dataset, plan, threshold, passed,
                  numbers={"events": int(len(events)), "unit_breaks_left_out": int((s["event_type"] == "unit_break").sum()),
                           "agree": int(agree.sum()), "share": share,
                           "events_2plus_vendor_values": int(len(multi)),
                           "share_2plus_vendor_values": _share(int((multi["agree"] == "Y").sum()), len(multi)),
                           "disagreements": int(len(disagreements)), "disagreements_unreviewed": int(len(unreviewed)),
                           "distributions_spinoffs": int(len(needs_url)), "distributions_spinoffs_without_sec_url": int(len(no_url)),
                           "special_over_10pct": int(len(big)), "special_over_10pct_without_sec_url": int(len(big_no_url)),
                           "with_sec_candidates": int((s["sec_candidates"] != "").sum())},
                  details={"by_type": events["event_type"].value_counts().to_dict(),
                           "unreviewed_disagreements": unreviewed[["security_id", "ticker", "ex_date", "split_factor",
                                                                   "event_type"]].to_dict("records")[:LIST_LIMIT]})


def check_known_cases(ctx: Context) -> dict:
    """Plan 4.3's known cases are reproduced in the canonical series, and confirmed_price_adjustments.csv and
    corporate_actions.csv match it."""
    name, dataset, plan = "known_cases", "splits", "4.3 known cases; confirmed_price_adjustments / corporate_actions"
    threshold = ("every known split/ratio within 0.2% on its ex-date (+-1 session); every cash spin-off day carries D or S "
                 "and |tr| < 25% (no price-only drop); both repo files consistent")
    p = ctx.panel
    if p is None or ctx.intervals is None:
        return no_input(name, dataset, plan, threshold, [ctx.cache / "prices" / "daily_panel.csv.gz", ctx.inputs / "ticker_intervals.csv"])
    by_sid = {sid: g for sid, g in p.groupby("security_id")}
    rows, bad = [], []
    for ticker, day, kind, factor in KNOWN_CASES:
        sid = ctx.holder_of(ticker, day)
        g = by_sid.get(sid)
        out = {"ticker": ticker, "date": day, "kind": kind, "security_id": sid}
        if g is None:
            out["found"] = False
        else:
            target = ctx.session_pos([day])[0]
            near = g[(g["pos"] - target).abs() <= 1]
            if kind in ("split", "spinoff_ratio"):
                hit = near[((near["split_factor"] / factor) - 1).abs() <= 0.002]
                out.update(found=bool(len(hit)), split_factor=[_round(v) for v in near["split_factor"] if v != 1])
            else:
                hit = near[(near["div_cash"] > 0) | (near["split_factor"] != 1)]
                ok = bool(len(hit)) and bool((hit["tr"].abs() < 0.25).all())
                out.update(found=ok, with_div_cash=bool((hit["div_cash"] > 0).any()),
                           split_factor=[_round(v) for v in hit["split_factor"]],
                           tr_within_25pct=bool((hit["tr"].abs() < 0.25).all()) if len(hit) else None)
        rows.append(out)
        if not out["found"]:
            bad.append(out)
    repo = {}
    confirmed = ctx.find("stocks_list_dir/nasdaq/confirmed_price_adjustments.csv")
    if confirmed.exists():
        for row in ctx.read_frame(confirmed, dtype=str, keep_default_na=False).itertuples(index=False):
            sid = ctx.holder_of(row.ticker, row.effective_date)
            g = by_sid.get(sid)
            factor = float(row.adjustment_factor)
            want = 1 / factor if "REVERSE" in row.action_type.upper() else factor
            ok = g is not None and bool((((g.loc[g["date"] == row.effective_date, "split_factor"] / want) - 1).abs() <= 0.002).any())
            repo.setdefault("confirmed_price_adjustments", []).append(
                {"ticker": row.ticker, "date": row.effective_date, "security_id": sid, "consistent": ok})
    actions = ctx.find("stocks_list_dir/nasdaq/corporate_actions.csv")
    if actions.exists():
        ranked = set(ctx.universe[0]["security_id"]) if ctx.universe[0] is not None else set()
        for row in ctx.read_frame(actions, dtype=str, keep_default_na=False).itertuples(index=False):
            sid = ctx.holder_of(row.predecessor, row.last_price_date)
            succ = ctx.holder_of(row.successor, row.effective_date)
            g = by_sid.get(sid)
            ticker_change = _num([row.share_ratio]).iloc[0] == 1.0 and (_num([row.cash_per_share]).fillna(0).iloc[0] == 0.0)
            if g is None:
                state = "not_a_target" if sid not in ranked else "no_series"
            elif sid == succ or (ticker_change and not succ):
                state = "consistent" if g["date"].max() > row.effective_date else "series_stops_at_ticker_change"
            else:
                last_pos = ctx.session_pos([g["date"].max()])[0]
                want_pos = ctx.session_pos([row.last_price_date])[0]
                state = "consistent" if abs(last_pos - want_pos) <= 1 else (
                    "series_ends_early" if last_pos < want_pos else "series_runs_past_last_price_date")
            repo.setdefault("corporate_actions", []).append(
                {"predecessor": row.predecessor, "successor": row.successor, "last_price_date": row.last_price_date,
                 "security_id": sid, "successor_security_id": succ, "ticker_change": bool(ticker_change),
                 "series_last": g["date"].max() if g is not None else "", "state": state})
    repo_bad = [r for r in repo.get("confirmed_price_adjustments", []) if not r["consistent"]] + \
        [r for r in repo.get("corporate_actions", []) if r["state"] not in ("consistent", "not_a_target")]
    passed = not bad and not repo_bad and "confirmed_price_adjustments" in repo and "corporate_actions" in repo
    return result(name, dataset, plan, threshold, passed,
                  numbers={"known_cases": len(rows), "reproduced": len(rows) - len(bad),
                           "confirmed_price_adjustments": len(repo.get("confirmed_price_adjustments", [])),
                           "corporate_actions": len(repo.get("corporate_actions", [])), "repo_rows_inconsistent": len(repo_bad)},
                  details={"known_cases": rows, "repo_files": repo},
                  note="corporate_actions rows for securities never ranked <= 300 are 'not_a_target'")


def check_dividends(ctx: Context) -> dict:
    """Dividends: Tiingo against Nasdaq (100-name sample from 2013-08) and against Yahoo after split scaling."""
    name, dataset, plan = "dividends", "dividends", "6 Dividends; step 7b"
    threshold = (f"Nasdaq sample of {NASDAQ_DIV_SAMPLE} names and Tiingo-Yahoo pairs: ex-date match >= {DIV_MATCH}; "
                 f"amounts within ${DIV_AMOUNT_TOL}")
    tiingo = _tiingo_files(ctx)
    pairs, matched, union, amount_bad = 0, 0, 0, []
    for sid, t_path in tiingo.items():
        y_path = _yahoo_file(ctx, sid)
        if y_path is None:
            continue
        t = ctx.read_frame(t_path, dtype={"date": str})
        y = ctx.read_frame(y_path, dtype={"date": str})
        lo, hi = max(t["date"].min(), y["date"].min()), min(t["date"].max(), y["date"].max())
        td = t[(t["date"] >= lo) & (t["date"] <= hi) & (t["divCash"] > 0)].set_index("date")["divCash"]
        yd = y[(y["date"] >= lo) & (y["date"] <= hi) & (y["div_cash"] > 0)].set_index("date")["div_cash"]
        if not len(td) and not len(yd):
            continue
        pairs += 1
        common_dates = td.index.intersection(yd.index)
        matched += len(common_dates)
        union += len(td.index.union(yd.index))
        diff = (td[common_dates] - yd[common_dates]).abs()
        amount_bad += [{"security_id": sid, "date": d, "diff": _round(v)} for d, v in diff[diff > DIV_AMOUNT_TOL].items()]
    nasdaq_dir = ctx.cache / "raw" / "nasdaq" / "dividends"
    nasdaq_files = sorted(nasdaq_dir.glob("*.json*")) if nasdaq_dir.exists() else []
    share = _share(matched, union)
    yahoo_ok = share is not None and share >= DIV_MATCH and not amount_bad
    passed = yahoo_ok and len(nasdaq_files) >= NASDAQ_DIV_SAMPLE
    return result(name, dataset, plan, threshold, passed,
                  numbers={"tiingo_yahoo_securities": pairs, "ex_dates_matched": matched, "ex_dates_either": union,
                           "tiingo_yahoo_match_share": share, "amounts_over_0.001": len(amount_bad),
                           "nasdaq_sample_files": len(nasdaq_files), "nasdaq_sample_needed": NASDAQ_DIV_SAMPLE},
                  details={"amounts_over_0.001": amount_bad[:LIST_LIMIT]},
                  note=("the Nasdaq dividends sample (step 7b, CACHE/raw/nasdaq/dividends/) has not been fetched; "
                        "Yahoo dividends are compared as restored (already scaled by later splits)"
                        if not nasdaq_files else ""))


# ================================================================== terminal values (4.5, section 6)

def _ended_universe_ids(ctx: Context) -> tuple[list, list]:
    """Universe securities whose series ends before 2026-08-31, and those with no series at all."""
    members = ctx.members()
    spans = ctx.panel_spans
    last_session = ctx.sessions_between(PRICE_START, PRICE_END)[-1].strftime("%Y-%m-%d")
    ids = sorted(set(members["security_id"]))
    ended = [i for i in ids if i in spans.index and spans.loc[i, "last"] < last_session]
    unpriced = [i for i in ids if i not in spans.index]
    return ended, unpriced


def check_terminal_coverage(ctx: Context) -> dict:
    """Universe series that end early: 100% have a terminal row; unknown type <= 5%, each listed."""
    name, dataset, plan = "terminal_coverage", "terminal", "6 Terminal; 4.5"
    threshold = f"100% of universe series ending before {PRICE_END} have a row; terminal_type unknown <= {TERMINAL_UNKNOWN_MAX:.0%}"
    t = ctx.csv("terminal_returns_2012_2026.csv")
    if t is None or ctx.members() is None or ctx.panel is None:
        return no_input(name, dataset, plan, threshold, [ctx.inputs / "terminal_returns_2012_2026.csv", "universe", "panel"])
    ended, unpriced = _ended_universe_ids(ctx)
    have = set(t["security_id"])
    without = [i for i in ended if i not in have]
    unpriced_without = [i for i in unpriced if i not in have]
    unknown = t[t["terminal_type"] == "unknown"]
    share_unknown = _share(len(unknown), len(t))
    passed = not without and share_unknown is not None and share_unknown <= TERMINAL_UNKNOWN_MAX
    spans = ctx.panel_spans
    return result(name, dataset, plan, threshold, passed,
                  numbers={"universe_series_ending_early": len(ended), "with_row": len(ended) - len(without),
                           "without_row": len(without), "unknown_rows": int(len(unknown)), "rows": int(len(t)),
                           "unknown_share": share_unknown, "by_type": t["terminal_type"].value_counts().to_dict(),
                           "universe_names_without_series": len(unpriced),
                           "universe_names_without_series_or_row": len(unpriced_without)},
                  details={"without_row": [{"security_id": i, "series_last": spans.loc[i, "last"]} for i in without][:LIST_LIMIT],
                           "unknown": unknown[["security_id", "ticker", "status_note"]].to_dict("records")[:LIST_LIMIT]},
                  basis=ctx.universe[1])


def check_terminal_open(ctx: Context) -> dict:
    """Terminal rows still open: no value yet (pending prices, review, acquirer price) and the manual queue."""
    name, dataset, plan = "terminal_open", "terminal", "4.5; step 11 review"
    threshold = "0 rows outside computed / no_terminal_return (exchange move) / awaiting_d5 (owner's D5 rule); manual queue empty"
    t = ctx.csv("terminal_returns_2012_2026.csv")
    if t is None:
        return no_input(name, dataset, plan, threshold, [ctx.inputs / "terminal_returns_2012_2026.csv"])
    open_rows = t[~t["status"].isin(TERMINAL_RESOLVED)]
    queue = ctx.cache_csv("terminal/manual_review_queue.csv")
    queue_rows = 0 if queue is None else len(queue)
    return result(name, dataset, plan, threshold, not len(open_rows) and queue_rows == 0,
                  numbers={"rows": int(len(t)), "open": int(len(open_rows)), "by_status": t["status"].value_counts().to_dict(),
                           "open_by_status": open_rows["status"].value_counts().to_dict(),
                           "manual_review_queue": queue_rows,
                           "awaiting_owner_d5": int((t["status"] == "awaiting_d5").sum())},
                  details={"open": open_rows[["security_id", "ticker", "terminal_type", "status"]].to_dict("records")[:LIST_LIMIT]},
                  note="awaiting_d5 rows wait for the owner's D5 decision (-100% or another registered value)")


# ================================================================== universe (3.3, section 6)

def _universe_stale_inputs(ctx: Context) -> list | None:
    """Files whose sha256 in the step-12 summary (inputs_sha256, outputs_sha256) differs from the file on disk:
    the universe was built from other inputs, or its outputs were replaced. The live Tiingo status is left out
    (the build may use a fixed copy of it). None when the summary is missing."""
    path = ctx.cache / "universe" / "universe_summary.json"
    if not path.exists():
        return None
    summary = ctx.read_json(path)
    recorded = {**summary.get("inputs_sha256", {}), **summary.get("outputs_sha256", {})}
    inputs_prefix = str(common.INPUTS).rstrip("/") + "/"
    stale = []
    for key, digest in recorded.items():
        if not isinstance(digest, str) or key.startswith("tiingo_status"):
            continue
        file = ctx.inputs / key[len(inputs_prefix):] if key.startswith(inputs_prefix) else ctx.path(key)
        if not file.exists():
            stale.append({"file": ctx.key(file), "state": "gone"})
            continue
        now = ctx.hashes.get(str(file)) or ctx.sha(file)
        if now != digest:
            stale.append({"file": ctx.key(file), "state": "changed since the universe build"})
    return stale


def check_universe_build(ctx: Context) -> dict:
    """The step-12 files exist and are well formed: 759 Friday week ends, ranks 1..300 by dv50 and dv20,
    raw close >= $10, no foreign filer."""
    name, dataset, plan = "universe_build", "universe", "1.1 weekly_universe_*; 3.1; owner decisions"
    threshold = ("INPUTS/weekly_universe_summary.csv, INPUTS/weekly_universe_top300.csv.gz, CACHE/universe/weekly_listed.csv.gz "
                 "and weekly_liquidity.csv.gz exist; weeks = last XNAS session of each week 2012-01-06..2026-07-17; "
                 "per week dv50 and dv20 ranks 1..>=300 unique; price_ge_10 = Y; no foreign filer ranked; the inputs and "
                 "outputs hashed in CACHE/universe/universe_summary.json are the files on disk (not built from older inputs)")
    paths = [ctx.inputs / "weekly_universe_summary.csv", ctx.inputs / "weekly_universe_top300.csv.gz",
             ctx.cache / "universe" / "weekly_listed.csv.gz", ctx.cache / "universe" / "weekly_liquidity.csv.gz"]
    missing = [p for p in paths if not p.exists()]
    frame, basis = ctx.universe
    numbers, details = {"files_missing": len(missing)}, {"missing_inputs": [str(p) for p in missing]}
    ok = not missing
    if frame is not None:
        f = frame[(frame["week_end"] >= WEEK_FIRST) & (frame["week_end"] <= WEEK_LAST)]
        weeks = pd.DatetimeIndex(sorted(f["week_end"].unique()))
        expected = ctx.week_ends
        numbers.update(weeks=len(weeks), expected_weeks=len(expected),
                       weeks_not_week_end=len(weeks.difference(expected)), week_ends_missing=len(expected.difference(weeks)))
        rank_problems = {}
        for column in ("dv50_rank", "dv20_rank"):
            g = f[f[column] <= PRICE_RANK].groupby("week_end")[column]
            short = int((g.max() < PRICE_RANK).sum() + len(expected.difference(pd.DatetimeIndex(g.max().index))))
            dup = int(f[f[column] <= PRICE_RANK].duplicated(["week_end", column]).sum())
            rank_problems[column] = {"weeks_short_of_300": short, "duplicate_ranks": dup}
        numbers["ranks"] = rank_problems
        numbers["duplicate_name_weeks"] = int(f.duplicated(["week_end", "security_id"]).sum())
        if "price_ge_10" in f.columns:
            ranked = f[f[["dv50_rank", "dv20_rank"]].min(axis=1) <= PRICE_RANK]
            numbers["ranked_price_ge_10"] = ranked["price_ge_10"].value_counts().to_dict()
        m = ctx.master
        if m is not None:
            foreign = set(m.loc[m["foreign_filer"] == "Y", "security_id"])
            numbers["ranked_name_weeks_foreign_Y"] = int(f["security_id"].isin(foreign).sum())
        ok = (ok and numbers["weeks_not_week_end"] == 0 and numbers["week_ends_missing"] == 0
              and all(v["weeks_short_of_300"] == 0 and v["duplicate_ranks"] == 0 for v in rank_problems.values())
              and numbers["duplicate_name_weeks"] == 0 and numbers.get("ranked_name_weeks_foreign_Y", 0) == 0
              and set(numbers.get("ranked_price_ge_10", {"Y": 1})) <= {"Y"})
    summary = ctx.csv("weekly_universe_summary.csv")
    if summary is not None:
        cols = list(summary.columns)
        numbers["summary_columns_missing"] = [c for c in UNIVERSE_SUMMARY_COLUMNS if c not in cols]
        ok = ok and not numbers["summary_columns_missing"]
    if frame is not None and "official" in basis:
        numbers["top300_columns_missing"] = [c for c in UNIVERSE_TOP_COLUMNS if c not in frame.columns]
        ok = ok and not numbers["top300_columns_missing"]
    stale = _universe_stale_inputs(ctx)
    if stale is not None:
        numbers["built_from_inputs_on_disk"] = not stale
        details["inputs_or_outputs_changed_since_build"] = stale[:LIST_LIMIT]
        ok = ok and not stale
    ok = ok and "official" in basis
    return result(name, dataset, plan, threshold, ok, numbers=numbers, details=details, basis=basis,
                  status="pass" if ok else ("no_input" if missing else "fail"),
                  note="the structure numbers describe the basis named; the check passes only on the official step-12 files")


def _proxy_table(ctx: Context) -> tuple[pd.DataFrame | None, str]:
    """Listed common stocks per week with vendor coverage that week and whether their size proxy reaches
    the median of the names ranked 200-250 (plan 3.3 check 1)."""
    def build():
        listed, basis = ctx.listed
        if listed is None:
            return None, basis
        frame = listed[(listed["week_end"] >= WEEK_FIRST) & (listed["week_end"] <= WEEK_LAST)].copy()
        frame["outside"] = (frame["outside_trading"].astype(str) == "True") if "outside_trading" in frame else False
        frame["week"] = ctx.all_week_ends.searchsorted(frame["week_end"])
        vendor = ctx.vendor_weeks
        frame["vendor"] = [(s, w) in vendor for s, w in zip(frame["security_id"], frame["week"])]
        members, _ = ctx.universe
        ranks = members[["week_end", "security_id", "dv50_rank"]] if members is not None else None
        if "dv50_rank" not in frame.columns and ranks is not None:
            frame = frame.merge(ranks, on=["week_end", "security_id"], how="left")
        band = frame[(frame["dv50_rank"] >= 200) & (frame["dv50_rank"] <= 250)]
        cut = band.groupby("week_end").agg(cut_mcap=("mcap", "median"), cut_float=("float_usd", "median"))
        frame = frame.merge(cut, left_on="week_end", right_index=True, how="left")
        mcap, flt = frame["mcap"], frame["float_usd"]
        frame["above"] = ((mcap >= frame["cut_mcap"]) | (mcap.isna() & (flt >= frame["cut_float"]))).fillna(False)
        # Size evidence: a proxy (market cap or float), else step 6's dollar volume (stored files included).
        # A name-week with neither has an unknown size: it is never counted as small.
        frame["proxy_known"] = mcap.notna() | flt.notna()
        frame["dv_known"] = frame["pf_dv50"].notna() if "pf_dv50" in frame else False
        if "pf_ge_cut250" in frame:
            frame["dv_above"] = frame["pf_ge_cut250"].astype(str) == "True"
        elif ctx.prefilter_weekly is not None and "pf_dv50" in frame:
            weekly = ctx.prefilter_weekly
            cut250 = weekly[weekly["dv50_rank"] == UNIVERSE_N].drop_duplicates("week_end").set_index("week_end")["dv50"]
            frame["dv_above"] = (frame["pf_dv50"] >= frame["week_end"].map(cut250)).fillna(False)
        else:
            frame["dv_above"] = False
        frame["unknown_size"] = ~frame["proxy_known"] & ~frame["dv_known"]
        if "evidence" in frame:   # step 12's own class of a missing name-week: price_lt_10 / dv / proxy / unknown
            evidence = frame["evidence"].fillna("").astype(str)
            frame["dv_known"] = frame["dv_known"] | (evidence == "dv")
            frame["unknown_size"] = ((frame["unknown_size"] & ~evidence.isin(["dv", "price_lt_10"]))
                                     | (evidence == "unknown"))
        return frame, basis
    return ctx.memo("proxy_table", build)


def _unknown_size_summary(ctx: Context, rows: pd.DataFrame) -> dict:
    """Per-security spans of unpriced name-weeks with no size evidence (for the lists)."""
    if not len(rows):
        return {"securities": 0, "by_year": {}, "names": []}
    g = rows.groupby("security_id").agg(weeks=("week_end", "size"), first=("week_end", "min"), last=("week_end", "max"))
    g = g.sort_values("weeks", ascending=False)
    reason = rows.groupby("security_id")["missing_reason"].agg(lambda r: " ".join(sorted(set(r.fillna("").astype(str)) - {""}))) \
        if "missing_reason" in rows else pd.Series(dtype=str)
    ticker = rows.groupby("security_id")["ticker"].last() if "ticker" in rows else pd.Series(dtype=str)
    return {"securities": int(len(g)),
            "by_year": {int(y): int(n) for y, n in rows.groupby(rows["week_end"].dt.year).size().items()},
            "names": [{"security_id": sid, "ticker": str(ticker.get(sid, "")), "weeks": int(r["weeks"]),
                       "first": r["first"].strftime("%Y-%m-%d"), "last": r["last"].strftime("%Y-%m-%d"),
                       "missing_reason": str(reason.get(sid, "")), "unfillable": sid in ctx.unfillable_ids}
                      for sid, r in g.head(LIST_LIMIT).iterrows()]}


def _universe_unknown_columns(ctx: Context) -> dict:
    """The step-12 build's own per-week counts of missing names with unknown size or no evidence, when its
    weekly summary carries them (any n_missing_* column naming unknown / no evidence / not in step 6)."""
    summary = ctx.csv("weekly_universe_summary.csv")
    if summary is None:
        return {}
    out = {}
    for column in summary.columns:
        low = column.lower()
        if low.startswith("n_missing") and any(w in low for w in ("unknown", "no_evidence", "not_in_step6", "no_proxy")):
            values = _num(summary[column].values).fillna(0)
            out[column] = {"name_weeks": int(values.sum()), "weeks_above_zero": int((values > 0).sum())}
    return out


def check_universe_proxy_margin(ctx: Context) -> dict:
    """Plan 3.3 check 1: unpriced listed common stocks whose size proxy reaches the median of ranks 200-250.
    An unpriced name whose size is unknown (no proxy and no step-6 dollar volume) cannot be shown to be
    below that median, so it fails the check too."""
    name, dataset, plan = "universe_proxy_margin", "universe", "3.3 check 1"
    threshold = (f"0 such names in >= {PROXY_ZERO_SHARE:.0%} of weeks and never more than {PROXY_MAX}; 0 unpriced name-weeks "
                 "of unknown size (no market cap, no float, no step-6 dollar volume); 0 unpriced name-weeks with no proxy "
                 "whose step-6 dollar volume reaches the rank-250 cut; the step-12 build's unknown counts 0")
    frame, basis = _proxy_table(ctx)
    if frame is None:
        return no_input(name, dataset, plan, threshold, ["universe listed set"])
    unpriced_all = frame[~frame["vendor"]]
    unpriced = unpriced_all[~unpriced_all["outside"]]
    hit_all = unpriced_all[unpriced_all["above"]]
    hit = unpriced[unpriced["above"]]
    unknown = unpriced[unpriced["unknown_size"]]
    no_proxy = unpriced[~unpriced["proxy_known"]]
    dv_hits = no_proxy[no_proxy["dv_above"]]
    per_week = hit.groupby("week_end").size().reindex(ctx.week_ends, fill_value=0)
    per_week_all = hit_all.groupby("week_end").size().reindex(ctx.week_ends, fill_value=0)
    unknown_week = unknown.groupby("week_end").size().reindex(ctx.week_ends, fill_value=0)
    unfill = hit["security_id"].isin(ctx.unfillable_ids)
    per_week_excl = hit[~unfill].groupby("week_end").size().reindex(ctx.week_ends, fill_value=0)
    zero_share = _share(int((per_week == 0).sum()), len(per_week))
    build_unknown = _universe_unknown_columns(ctx)
    plan_rule = zero_share is not None and zero_share >= PROXY_ZERO_SHARE and int(per_week.max()) <= PROXY_MAX
    passed = (plan_rule and not len(unknown) and not len(dv_hits)
              and not any(v["name_weeks"] for v in build_unknown.values()))
    names = hit.groupby("security_id").agg(weeks=("week_end", "size"), first=("week_end", "min"), last=("week_end", "max"))
    names = names.sort_values("weeks", ascending=False).head(LIST_LIMIT)
    by_year = per_week.groupby(per_week.index.year).agg(["max", lambda s: int((s == 0).sum()), "size"])
    return result(name, dataset, plan, threshold, passed,
                  numbers={"weeks": int(len(per_week)), "weeks_zero": int((per_week == 0).sum()), "share_weeks_zero": zero_share,
                           "max_in_a_week": int(per_week.max()), "weeks_over_3": int((per_week > PROXY_MAX).sum()),
                           "plan_rule_met": plan_rule,
                           "name_weeks": int(len(hit)), "name_weeks_unfillable": int(unfill.sum()),
                           "share_weeks_zero_excluding_unfillable": _share(int((per_week_excl == 0).sum()), len(per_week_excl)),
                           "max_in_a_week_excluding_unfillable": int(per_week_excl.max()),
                           "name_weeks_incl_outside_trading": int(len(hit_all)),
                           "max_in_a_week_incl_outside_trading": int(per_week_all.max()),
                           "unpriced_name_weeks": int(len(unpriced)),
                           "unpriced_name_weeks_no_proxy": int(len(no_proxy)),
                           "unpriced_name_weeks_no_proxy_dv_at_or_above_cut250": int(len(dv_hits)),
                           "unpriced_name_weeks_unknown_size": int(len(unknown)),
                           "unknown_size_securities": int(unknown["security_id"].nunique()),
                           "weeks_with_unknown_size": int((unknown_week > 0).sum()),
                           "max_unknown_size_in_a_week": int(unknown_week.max()),
                           "step12_unknown_counts": build_unknown,
                           "by_year": {int(y): {"max": int(r.iloc[0]), "weeks_zero": int(r.iloc[1]), "weeks": int(r.iloc[2])}
                                       for y, r in by_year.iterrows()}},
                  details={"names": [{"security_id": s, "weeks": int(r["weeks"]), "first": r["first"].strftime("%Y-%m-%d"),
                                      "last": r["last"].strftime("%Y-%m-%d"), "unfillable": s in ctx.unfillable_ids}
                                     for s, r in names.iterrows()],
                           "unknown_size": _unknown_size_summary(ctx, unknown),
                           "no_proxy_dv_at_or_above_cut250": _unknown_size_summary(ctx, dv_hits)},
                  basis=basis,
                  note=("vendor prices in a week = a panel row that week; proxy = Wayback market cap carried up to 12 months, "
                        "else XBRL public float (as step 6 carried them); listing weeks the universe build marks outside "
                        "trading (listing start/end within 30 days of the series) are left out and counted separately. "
                        "Unknown size: no proxy and no step-6 dollar volume that week, so nothing shows the name is below "
                        "the median (step 12's evidence column, when present, adds its stored-file dollar volume and its "
                        "own 'unknown' class); a name with no proxy whose step-6 dollar volume reaches the canonical "
                        "rank-250 cut (pf_ge_cut250) is counted apart, and both fail the check"))


def check_universe_capture_coverage(ctx: Context) -> dict:
    """Plan 3.3 check 2: on each Wayback company-list date the top 200 Nasdaq common stocks by market cap
    have vendor prices; market-cap-weighted coverage by week."""
    name, dataset, plan = "universe_capture_coverage", "universe", "3.3 check 2"
    threshold = (f"top {CAPTURE_TOP} by market cap priced on each company-list date >= {CAPTURE_SHARE:.0%}, misses only "
                 f"from unfillable.csv; market-cap-weighted coverage >= {MCAP_COVER_BEFORE_2023:.0%} before 2023 and "
                 f">= {MCAP_COVER_FROM_2023:.0%} from 2023 (every week); 0 unpriced name-weeks of unknown size")
    index, p, m = ctx.csv("listing_snapshots_index.csv"), ctx.panel, ctx.master
    if index is None or p is None or m is None or ctx.intervals is None:
        return no_input(name, dataset, plan, threshold, ["listing_snapshots_index.csv", "panel", "security_master.csv"])
    foreign = set(m.loc[m["foreign_filer"] == "Y", "security_id"])
    listed, _ = ctx.listed
    eligible_by_week = None
    if listed is not None:
        eligible_by_week = set(zip(listed["week_end"], listed["security_id"]))
    iv = ctx.intervals
    lists = index[(index["source"] == "wayback_companylist") & (index["snapshot_date"] >= "2012-01-01")
                  & (index["snapshot_date"] <= WEEK_LAST)]
    days = {(r.as_of_session or r.snapshot_date) for r in lists.itertuples(index=False)}
    sessions_needed = {ctx.sessions[ctx.sessions.searchsorted(pd.Timestamp(d), side="right") - 1].strftime("%Y-%m-%d")
                       for d in days}
    on_day = p[p["date"].isin(sessions_needed) & p["src_primary"].isin(VENDORS)]
    have = set(zip(on_day["security_id"], on_day["date"]))
    dates, bad_dates = [], []
    for row in lists.itertuples(index=False):
        file = ctx.path(row.snapshot_file)
        if not file.exists():
            continue
        snap = ctx.read_frame(file, dtype=str, keep_default_na=False)
        day = row.as_of_session or row.snapshot_date
        pos = ctx.sessions.searchsorted(pd.Timestamp(day), side="right") - 1
        session = ctx.sessions[pos].strftime("%Y-%m-%d")
        week_end = ctx.all_week_ends[ctx.all_week_ends.searchsorted(ctx.sessions[pos])]
        on = iv[(iv["start"] <= session) & (iv["end"] >= session)].drop_duplicates("ticker").set_index("ticker")["security_id"]
        snap["security_id"] = snap["Symbol"].map(on)
        snap["mcap"] = _num(snap["MarketCap"].values).values
        snap = snap.dropna(subset=["security_id", "mcap"])
        snap = snap[~snap["security_id"].isin(foreign)]
        if eligible_by_week is not None:
            snap = snap[[(week_end, s) in eligible_by_week for s in snap["security_id"]]]
        snap = snap.sort_values("mcap", ascending=False).drop_duplicates("security_id")
        top = snap.head(CAPTURE_TOP)
        priced = np.array([(s, session) in have for s in top["security_id"]])
        miss = top.loc[~priced, ["Symbol", "security_id"]]
        miss_not_unfillable = miss[~miss["security_id"].isin(ctx.unfillable_ids)]
        all_priced = np.array([(s, session) in have for s in snap["security_id"]])
        weighted = _share(float(snap.loc[all_priced, "mcap"].sum()), float(snap["mcap"].sum()))
        rec = {"date": session, "top": int(len(top)), "priced": int(priced.sum()), "share": _share(int(priced.sum()), len(top)),
               "misses_not_unfillable": miss_not_unfillable["Symbol"].tolist()[:10], "mcap_weighted": weighted}
        dates.append(rec)
        if (rec["share"] or 0) < CAPTURE_SHARE or len(miss_not_unfillable) or (weighted or 0) < MCAP_COVER_BEFORE_2023:
            bad_dates.append(rec)
    weekly, weekly_bad, basis = {}, {"before_2023": 0, "from_2023": 0}, ""
    unknown = 0
    frame, basis = _proxy_table(ctx)
    if frame is not None:
        w = frame.assign(weight=frame["mcap"].where(frame["mcap"].notna(), frame["float_usd"]))
        unpriced = w[~w["vendor"] & ~w["outside"]]
        unknown = int(unpriced["unknown_size"].sum())
        no_weight = int((unpriced["weight"].isna() | ~(unpriced["weight"] > 0)).sum())
        w = w[w["weight"].notna() & (w["weight"] > 0)]
        cov = (w["weight"] * w["vendor"]).groupby(w["week_end"]).sum() / w.groupby("week_end")["weight"].sum()
        before, after = cov[cov.index < "2023-01-01"], cov[cov.index >= "2023-01-01"]
        weekly_bad = {"before_2023": int((before < MCAP_COVER_BEFORE_2023).sum()),
                      "from_2023": int((after < MCAP_COVER_FROM_2023).sum())}
        weekly = {"weeks": int(len(cov)), "min_before_2023": _round(before.min()), "min_from_2023": _round(after.min()),
                  "weeks_below_before_2023": weekly_bad["before_2023"], "weeks_below_from_2023": weekly_bad["from_2023"],
                  "unpriced_name_weeks_without_weight": no_weight, "unpriced_name_weeks_unknown_size": unknown,
                  "by_year_min": {int(y): _round(v) for y, v in cov.groupby(cov.index.year).min().items()}}
    passed = bool(dates) and not bad_dates and bool(weekly) and not any(weekly_bad.values()) and not unknown
    return result(name, dataset, plan, threshold, passed,
                  numbers={"company_list_dates": len(dates), "dates_failing": len(bad_dates),
                           "min_top200_share": min((d["share"] for d in dates if d["share"] is not None), default=None),
                           "min_mcap_weighted_on_capture_dates": min((d["mcap_weighted"] for d in dates
                                                                      if d["mcap_weighted"] is not None), default=None),
                           "weekly_mcap_weighted": weekly},
                  details={"dates_failing": bad_dates[:LIST_LIMIT]},
                  basis=basis,
                  note=("company lists end 2019-06; the weekly weighting uses the carried market cap, else the XBRL public "
                        "float (no market cap source after 2020), so from 2021 it is float-weighted. An unpriced name with "
                        "no weight drops out of both sides of the share; one with no weight and no step-6 dollar volume "
                        "(unknown size) could hold any weight, so it fails the check"))


def check_universe_nasdaq100(ctx: Context) -> dict:
    """Plan 3.3 check 3: every Nasdaq-100 year-end member 2011-2019 has vendor prices for that whole year."""
    name, dataset, plan = "universe_nasdaq100", "universe", "3.3 check 3"
    threshold = ("100% of year-end members have a vendor row on every session of that year in which they are in the "
                 "universe base (2011: from 2011-06-01 while listed); every member mapped to a security")
    path = ctx.find("output/research_only/holdout_2011_2019/inputs/nasdaq100_members_wikipedia_yearend.json")
    p, listed = ctx.panel, ctx.listed[0]
    if not path.exists() or p is None or ctx.intervals is None or ctx.master is None:
        return no_input(name, dataset, plan, threshold, [path, "panel", "security_master.csv"])
    members = ctx.read_json(path)
    vendor = p[p["src_primary"].isin(VENDORS) & (p["pos"] >= 0)]
    by_sid = vendor.groupby("security_id")["pos"].apply(lambda s: set(s.values))
    iv = ctx.intervals
    starts = iv.groupby("security_id")["start"].min()
    m = ctx.master.set_index("security_id")
    eligible = {}
    if listed is not None:
        for sid, weeks in listed.groupby("security_id")["week_end"]:
            eligible[sid] = pd.DatetimeIndex(weeks)
    week_pos = ctx.session_pos(ctx.all_week_ends)
    week_index = {w: i for i, w in enumerate(ctx.all_week_ends)}
    history = ctx.csv("periodic_form_history.csv")
    regimes = {} if history is None else {cik: g.sort_values("filing_date")[["filing_date", "regime_in_force"]]
                                          for cik, g in history.groupby("cik")}

    def regime_on(cik, day):
        g = regimes.get(str(cik))
        if g is None:
            return ""
        before = g[g["filing_date"] <= day]
        return before["regime_in_force"].iloc[-1] if len(before) else g["regime_in_force"].iloc[0]

    counts, failing, other = Counter(), [], []
    for year, tickers in sorted(members.items()):
        y = int(year)
        for ticker in tickers:
            year_end = f"{y}-12-31"
            rows = iv[(iv["ticker"] == ticker) & (iv["start"] <= year_end)]
            inside = rows[rows["end"] >= f"{y}-01-01"]
            pick = inside if len(inside) else rows   # else the latest earlier holder (a stale list entry)
            sid = pick.sort_values("end")["security_id"].iloc[-1] if len(pick) else ctx.holder_of(ticker, year_end)
            entry = {"year": y, "ticker": ticker, "security_id": sid}
            if not sid:
                counts["not_mapped"] += 1
                failing.append({**entry, "state": "not_mapped"})
                continue
            status = m.loc[sid, "foreign_filer"] if sid in m.index else ""
            if status == "Y" or (status == "MIXED" and y < 2012 and regime_on(m.loc[sid, "cik"], year_end) == "F"):
                counts["excluded_foreign_filer"] += 1
                other.append({**entry, "state": "excluded_foreign_filer"})
                continue
            if y >= 2012 and listed is not None:
                weeks = eligible.get(sid, pd.DatetimeIndex([]))
                weeks = weeks[weeks.year == y]
                if not len(weeks):
                    counts["not_in_universe_base_that_year"] += 1
                    other.append({**entry, "state": "not_in_universe_base_that_year"})
                    continue
                need = set()
                for w in weeks:
                    i = week_index[w]
                    need |= set(range(week_pos[i - 1] + 1, week_pos[i] + 1))
            else:
                first = max(f"{y}-01-01", PRICE_START, starts.get(sid, f"{y}-01-01"))
                last = min([year_end] + [d for d in (m.loc[sid, "delist_date"],) if d]) if sid in m.index else year_end
                need = set(ctx.session_pos(ctx.sessions_between(first, last).strftime("%Y-%m-%d")))
            missing = sorted(need - by_sid.get(sid, set()))
            counts["checked"] += 1
            if missing:
                counts["incomplete"] += 1
                failing.append({**entry, "state": "incomplete", "sessions": len(need), "missing": len(missing),
                                "first_missing": ctx.sessions[missing[0]].strftime("%Y-%m-%d"),
                                "unfillable": sid in ctx.unfillable_ids})
    checked = counts["checked"]
    return result(name, dataset, plan, threshold, checked > 0 and not failing,
                  numbers={"member_years_checked": checked, "complete": checked - counts["incomplete"],
                           "incomplete": counts["incomplete"], "not_mapped": counts["not_mapped"],
                           "share_complete": _share(checked - counts["incomplete"], checked),
                           "excluded_foreign_filer": counts["excluded_foreign_filer"],
                           "not_in_universe_base_that_year": counts["not_in_universe_base_that_year"],
                           "incomplete_unfillable": sum(bool(f.get("unfillable")) for f in failing)},
                  details={"failing": failing[:LIST_LIMIT], "not_required": other[:LIST_LIMIT]},
                  basis=ctx.listed[1],
                  note=("foreign filers are outside the universe by the owner's decision (2011: MIXED filers by the regime in "
                        "force at year end, periodic_form_history.csv); from 2012 a member is required in the weeks it is in the "
                        "universe base (MIXED filers' foreign weeks and weeks after a delisting are not); a list entry for a "
                        "security not in the base that year (a stale entry) is listed under not_required"))


def check_universe_form25(ctx: Context) -> dict:
    """Plan 3.3 check 4: every Nasdaq Form 25 for common stock with float >= $1B has a vendor series ending
    within 5 sessions of the effective date or the merger close, or a documented exclusion."""
    name, dataset, plan = "universe_form25", "universe", "3.3 check 4"
    threshold = (f"100% of common-stock Form 25s with float >= $1B (effective 2012..{PRICE_END}): series end within "
                 f"{FORM25_SESSIONS} sessions of the effective date or merger close, or documented (unfillable.csv, foreign filer)")
    f, m, t = ctx.csv("form25_nasdaq_2012_2026.csv"), ctx.master, ctx.csv("terminal_returns_2012_2026.csv")
    if f is None or m is None or ctx.panel is None:
        return no_input(name, dataset, plan, threshold, ["form25_nasdaq_2012_2026.csv", "security_master.csv", "panel"])
    rows = f[(f["classification"] == "common_delisting") & (_num(f["public_float_usd"].values).values >= FORM25_FLOAT)
             & (f["effective_date"] >= "2012-01-01") & (f["effective_date"] <= PRICE_END)]
    by_accession = m[m["delist_form25_accession"] != ""].groupby("delist_form25_accession")["security_id"].apply(list)
    by_cik = m.groupby("cik")["security_id"].apply(list)
    spans = ctx.panel_spans
    closes = {}
    if t is not None:
        for row in t.itertuples(index=False):
            closes[row.security_id] = [d for d in (row.last_price_date, row.end_date) if d]
    foreign = set(m.loc[m["foreign_filer"] == "Y", "security_id"])
    states, failing = Counter(), []
    for row in rows.itertuples(index=False):
        sids = by_accession.get(row.accession) or by_cik.get(row.subject_cik, [])
        state = "unmapped"
        for sid in sids:
            if sid in spans.index:
                last = ctx.session_pos([spans.loc[sid, "last"]])[0]
                targets = [row.effective_date] + closes.get(sid, [])
                target_pos = [ctx.sessions.searchsorted(pd.Timestamp(d)) for d in targets]
                if any(abs(last - tp) <= FORM25_SESSIONS for tp in target_pos):
                    state = "series_ends_near"
                    break
                state = "series_ends_elsewhere"
            elif sid in ctx.unfillable_ids:
                state = "documented_unfillable"
            elif sid in foreign:
                state = "documented_foreign_filer"
            elif state == "unmapped":
                state = "no_series"
        states[state] += 1
        if state not in ("series_ends_near", "documented_unfillable", "documented_foreign_filer"):
            failing.append({"accession": row.accession, "name": row.subject_name, "effective": row.effective_date,
                            "float_usd": row.public_float_usd, "security_ids": sids, "state": state})
    return result(name, dataset, plan, threshold, len(rows) > 0 and not failing,
                  numbers={"form25_rows": int(len(rows)), "by_state": dict(states), "failing": len(failing),
                           "share_ok": _share(len(rows) - len(failing), len(rows))},
                  details={"failing": failing[:LIST_LIMIT]})


def check_universe_listed_gaps(ctx: Context) -> dict:
    """Listed universe-base names the step-12 build marks missing for a reason that no fetch decision
    covers: a series that stops short (series_gap), a Tiingo answer step 9 has not read
    (fetched_pending_reconcile), no vendor source, or a reason this validator does not know."""
    name, dataset, plan = "universe_listed_gaps", "universe", "3.1 listed set; 3.3 (completeness); 6 Universe"
    threshold = ("0 eligible name-weeks in CACHE/universe/weekly_listed.csv.gz whose missing_reason is "
                 + " / ".join(sorted(BLOCKING_MISSING)) + " or not one of the known reasons; securities and week spans listed")
    path = ctx.cache / "universe" / "weekly_listed.csv.gz"
    listed, basis = ctx.listed
    if listed is None or "official" not in basis or "missing_reason" not in listed.columns:
        return no_input(name, dataset, plan, threshold, [path], note="needs the step-12 weekly_listed file with missing_reason")
    frame = listed[(listed["week_end"] >= WEEK_FIRST) & (listed["week_end"] <= WEEK_LAST)]
    reason = frame["missing_reason"].fillna("").astype(str)
    missing = frame[reason != ""]
    reasons = missing["missing_reason"].astype(str)
    unknown_reason = ~reasons.isin(KNOWN_MISSING)
    blocking = missing[reasons.isin(BLOCKING_MISSING) | unknown_reason]
    by_reason = reasons.value_counts().to_dict()
    spans = {}
    if len(blocking):
        b = blocking.assign(ticker=blocking["ticker"].astype(str) if "ticker" in blocking else "",
                            dv_above=(blocking["pf_ge_cut250"].astype(str) == "True") if "pf_ge_cut250" in blocking else False)
        g = b.groupby(["security_id", "missing_reason"]).agg(
            ticker=("ticker", "last"), weeks=("week_end", "size"), first=("week_end", "min"), last=("week_end", "max"),
            dv_above=("dv_above", "sum"))
        g = g.reset_index().sort_values("weeks", ascending=False)
        spans = {reason: [{"security_id": r.security_id, "ticker": r.ticker, "weeks": int(r.weeks),
                           "first": r.first.strftime("%Y-%m-%d"), "last": r.last.strftime("%Y-%m-%d"),
                           "weeks_step6_dv_at_or_above_cut250": int(r.dv_above)}
                          for r in group.head(LIST_LIMIT).itertuples(index=False)]
                 for reason, group in g.groupby("missing_reason", sort=True)}
    by_year = blocking.groupby([blocking["week_end"].dt.year, "missing_reason"]).size()
    year_counts = {}
    for (y, r), n in by_year.items():
        year_counts.setdefault(int(y), {})[r] = int(n)
    return result(name, dataset, plan, threshold, not len(blocking),
                  numbers={"eligible_name_weeks": int(len(frame)), "missing_name_weeks": int(len(missing)),
                           "missing_by_reason": {k: int(v) for k, v in by_reason.items()},
                           "blocking_name_weeks": int(len(blocking)),
                           "blocking_securities": int(blocking["security_id"].nunique()),
                           "blocking_by_reason": blocking["missing_reason"].value_counts().to_dict(),
                           "unknown_reasons": sorted(set(reasons[unknown_reason])),
                           "blocking_by_year": year_counts},
                  details={"blocking_spans_by_reason": spans},
                  basis=basis,
                  note=("reasons other than these are judged elsewhere: tiingo_pending / yahoo_pending (open candidates, "
                        "candidates_resolved), unfillable "
                        "(universe_unfillable), not_candidate / candidate_other (universe_proxy_margin and the unknown-size "
                        "count); a series_gap name has a series that does not reach the week (SMCI, CHRD after a later listing)"))


def _panel_dv(ctx: Context, sids) -> pd.DataFrame:
    """Per security and window week: the 50- and 20-session median raw dollar volume and the raw close at
    the week's last panel row (ranking by dollar volume only)."""
    p = ctx.panel
    sub = p[p["security_id"].isin(set(sids)) & (p["pos"] >= 0)].sort_values(["security_id", "pos"])
    dv = sub["close_raw"] * sub["volume_raw"]
    g = dv.groupby(sub["security_id"])
    sub = sub.assign(dv50=g.transform(lambda s: s.rolling(50, min_periods=50).median()),
                     dv20=g.transform(lambda s: s.rolling(20, min_periods=20).median()))
    last = sub.groupby(["security_id", "week"]).tail(1)
    return last[["security_id", "week", "dv50", "dv20", "close_raw"]]


def _landed(ctx: Context, sids: set, n: int) -> tuple[pd.DataFrame, str]:
    """(security_id, week_end) name-weeks of ``sids`` at dv50 or dv20 rank <= n, and how they were found."""
    frame, basis = ctx.universe
    if frame is not None and "official" in basis:
        top = ctx.members(n)
        return top.loc[top["security_id"].isin(sids), ["security_id", "week_end"]], "ranks from the official universe"
    weekly = ctx.prefilter_weekly
    if weekly is None or not sids:
        return pd.DataFrame(columns=["security_id", "week_end"]), "none"
    cut50 = weekly[weekly["dv50_rank"] == n].drop_duplicates("week_end").set_index("week_end")["dv50"]
    cut20 = weekly[weekly["dv20_rank"] == n].drop_duplicates("week_end").set_index("week_end")["dv20"]
    dv = _panel_dv(ctx, sids)
    dv["week_end"] = np.asarray(ctx.all_week_ends[dv["week"].clip(upper=len(ctx.all_week_ends) - 1).values])
    dv = dv[(dv["week_end"] >= WEEK_FIRST) & (dv["week_end"] <= WEEK_LAST)]
    land = (dv["close_raw"] >= MIN_PRICE) & ((dv["dv50"] >= dv["week_end"].map(cut50).values)
                                             | (dv["dv20"] >= dv["week_end"].map(cut20).values))
    return dv.loc[land, ["security_id", "week_end"]], f"panel dollar volume against the step-6 rank-{n} dollar volume of each week"


def check_universe_fetch_margin(ctx: Context) -> dict:
    """Plan 3.3 check 5 and the 3.2 B-C sample rule: names fetched in month 1 that land at rank <= 250, by
    tier; tier B-B or B-C names there mean the screen is too tight (lower it one tier in month 2); a B-C
    sample name at rank <= 300 in any week means all of tier C is fetched in month 2. The check waits
    (no_input) until every B-B and B-C sample name is fetched and, when Tiingo returned data, in the panel."""
    name, dataset, plan = "universe_fetch_margin", "universe", "3.3 check 5; 3.2 tier B-C sample rule"
    threshold = ("every tier B-B and B-C sample candidate fetched (fetch_status done / done_review / partial / wrong_entity / "
                 "no_data, or settled before a request) and, with data, present in the panel (else no_input, pending listed); "
                 "0 fetched tier B-B / B-C names at dv50 or dv20 rank <= 250 (else lower the screen one tier in month 2); "
                 "a B-C sample name at rank <= 300 in any week means all of tier C is fetched (month 2)")
    status_path = ctx.cache / "tiingo" / "fetch_status.csv"
    status, c = ctx.live_frame("tiingo/fetch_status.csv"), ctx.csv("candidate_fetch_list.csv")
    if status is None or ctx.panel is None or c is None:
        return no_input(name, dataset, plan, threshold, [status_path, "panel", ctx.inputs / "candidate_fetch_list.csv"])
    p = ctx.panel
    in_panel = set(p.loc[p["src_primary"] == "tiingo", "security_id"])
    last = status.drop_duplicates("security_id", keep="last").set_index("security_id")
    fetch = last["status"]
    fetched_utc = last["fetched_utc"] if "fetched_utc" in last else pd.Series(dtype=str)
    reconcile = ctx.cache / "reconcile" / "summary.json"
    panel_built = ctx.read_json(reconcile).get("built_utc", "") if reconcile.exists() else ""

    def state(row) -> str:
        got = fetch.get(row.security_id, "")
        if got in FETCH_DATA:
            newer = bool(panel_built) and str(fetched_utc.get(row.security_id, "")) > panel_built
            return "in_panel" if row.security_id in in_panel and not newer else "fetched_not_in_panel"
        if got in FETCH_EMPTY:
            return "fetched_no_data"
        if row.status in FETCH_EMPTY or row.planned_source == "unfillable":
            return "settled_before_fetch"       # the range check found no row of this company: no request needed
        return "pending_fetch"

    cand = c.drop_duplicates(["security_id", "reason"]).copy()
    cand["state"] = [state(r) for r in cand.itertuples(index=False)]
    fetched = cand[cand["state"] == "in_panel"]
    landed, method = _landed(ctx, set(fetched["security_id"]), UNIVERSE_N)
    landed = landed.merge(fetched[["security_id", "reason"]], on="security_id")
    by_tier = landed.groupby("reason")["security_id"].nunique().to_dict()
    weeks_by_tier = landed.groupby("reason").size().to_dict()
    tight = {k: v for k, v in by_tier.items() if k.startswith(("B_B", "B_C"))}
    required = cand[cand["reason"].isin([TIER_BB, TIER_BC_SAMPLE])]
    pending = required[required["state"].isin(["pending_fetch", "fetched_not_in_panel"])]
    sample = cand[(cand["reason"] == TIER_BC_SAMPLE) & (cand["state"] == "in_panel")]
    sample_300, _ = _landed(ctx, set(sample["security_id"]), PRICE_RANK)
    triggered = bool(len(sample_300))
    rest = cand[cand["reason"] == TIER_BC_REST]
    rest_open = rest[~rest["state"].isin(["in_panel", "fetched_no_data", "settled_before_fetch"])]
    states = {t: g["state"].value_counts().to_dict() for t, g in cand.groupby("reason")}
    actions = []
    if tight:
        actions.append("lower the screen one tier in month 2")
    if triggered:
        actions.append("fetch all of tier C in month 2 (a B-C sample name reached rank <= 300)")
    waiting = bool(len(pending)) or not len(required)
    passed = not waiting and not tight and (not triggered or not len(rest_open))
    pending_list = pending[["security_id", "ticker_for_source", "reason", "state"]].to_dict("records")
    return result(name, dataset, plan, threshold, passed,
                  status="no_input" if waiting else "",
                  numbers={"fetched_in_panel_names": int(fetched["security_id"].nunique()),
                           "states_by_tier": states,
                           "required_b_b_and_b_c_sample": int(len(required)),
                           "required_pending": int(len(pending)),
                           "required_pending_by_state": pending["state"].value_counts().to_dict(),
                           "names_at_rank_le_250_by_tier": by_tier, "name_weeks_at_rank_le_250_by_tier": weeks_by_tier,
                           "tier_b_b_or_b_c_names_in_top250": sum(tight.values()),
                           "b_c_sample_in_panel": int(len(sample)),
                           "b_c_sample_names_at_rank_le_300": int(sample_300["security_id"].nunique()) if len(sample_300) else 0,
                           "tier_c_rule_triggered": triggered, "tier_c_rest": int(len(rest)),
                           "tier_c_rest_open": int(len(rest_open))},
                  details={"action": "; ".join(actions) if actions else ("wait for the fetch and step 9" if waiting else "none"),
                           "method": method, "pending": pending_list[:LIST_LIMIT],
                           "b_c_sample_at_rank_le_300": sorted(set(sample_300["security_id"]))[:LIST_LIMIT] if len(sample_300) else []},
                  basis=ctx.universe[1],
                  note=("a name counts as fetched only when Tiingo returned data, the panel holds Tiingo rows for it and "
                        "the answer is older than the panel (reconcile/summary.json built_utc); fetched names step 9 has "
                        "not read are pending, as are those the Tiingo run has not reached"))


def check_universe_unfillable(ctx: Context) -> dict:
    """Plan 3.3 check 6: estimated top-250 name-weeks held by unfillable names <= 2% of the slots in any year.
    A week of an unfillable name whose size is unknown cannot be estimated, so it fails the check."""
    name, dataset, plan = "universe_unfillable", "universe", "3.3 check 6"
    threshold = (f"estimated unfillable name-weeks / (250 x weeks) <= {UNFILLABLE_SLOT_SHARE:.0%} in every year (else reported "
                 "as survivor bias); 0 unpriced unfillable name-weeks of unknown size (no proxy, no step-6 dollar volume)")
    u = ctx.csv("unfillable.csv")
    frame, basis = _proxy_table(ctx)
    if u is None or frame is None:
        return no_input(name, dataset, plan, threshold, [ctx.inputs / "unfillable.csv", "universe listed set"])
    hits, unknown = [], []
    by_sid = {sid: g for sid, g in frame[~frame["vendor"]].groupby("security_id")}
    for row in u.itertuples(index=False):
        mine = by_sid.get(row.security_id)
        if mine is None:
            continue
        mine = mine[mine["week_end"].between(pd.Timestamp(row.needed_start or WEEK_FIRST),
                                             pd.Timestamp(row.needed_end or WEEK_LAST))]
        estimated = mine["above"] | (~mine["proxy_known"] & mine["dv_above"])
        hits.append(mine.loc[estimated, ["security_id", "week_end"]])
        unknown.append(mine.loc[mine["unknown_size"] & ~mine["outside"], ["security_id", "week_end"]])
    empty = pd.DataFrame(columns=["security_id", "week_end"])
    est = pd.concat(hits).drop_duplicates() if hits else empty
    unk = pd.concat(unknown).drop_duplicates() if unknown else empty
    weeks_per_year = pd.Series(ctx.week_ends.year).value_counts()
    per_year = est.groupby(pd.to_datetime(est["week_end"]).dt.year).size() if len(est) else pd.Series(dtype=int)
    unk_year = unk.groupby(pd.to_datetime(unk["week_end"]).dt.year).size() if len(unk) else pd.Series(dtype=int)
    shares = {int(y): {"name_weeks": int(per_year.get(y, 0)), "slots": int(UNIVERSE_N * n),
                       "share": _share(int(per_year.get(y, 0)), UNIVERSE_N * n),
                       "unknown_size_name_weeks": int(unk_year.get(y, 0))} for y, n in sorted(weeks_per_year.items())}
    over = [y for y, r in shares.items() if (r["share"] or 0) > UNFILLABLE_SLOT_SHARE]
    no_proxy_rows = u[u["proxy"].isin(["", "none"])] if "proxy" in u else u.iloc[:0]
    unk_names = unk.groupby("security_id").size().sort_values(ascending=False)
    return result(name, dataset, plan, threshold, not over and not len(unk),
                  numbers={"unfillable_rows": int(len(u)), "estimated_name_weeks": int(len(est)),
                           "file_est_weeks_in_top250": int(_num(u["est_weeks_in_top250"].values).fillna(0).sum()),
                           "years_over_2pct": over, "unknown_size_name_weeks": int(len(unk)),
                           "unknown_size_securities": int(unk["security_id"].nunique()) if len(unk) else 0,
                           "rows_with_proxy_none": int(len(no_proxy_rows)), "by_year": shares},
                  details={"unknown_size": [{"security_id": s, "name_weeks": int(n)} for s, n in unk_names.head(LIST_LIMIT).items()],
                           "rows_with_proxy_none": no_proxy_rows[[c for c in ("security_id", "ticker", "needed_start",
                                                                             "needed_end", "proxy") if c in u]]
                           .to_dict("records")[:LIST_LIMIT]},
                  basis=basis,
                  note=("estimate: weeks in the needed window with no vendor price where the name's size proxy reaches the "
                        "ranks 200-250 median, or, with no proxy, its step-6 dollar volume reaches the rank-250 cut; a week "
                        "with neither is of unknown size (fails)"))


# ================================================================== manifest coverage (section 6) and the run

def files_for_test(ctx: Context) -> list[Path]:
    """Every file the future test reads: the committed inputs (the plan 1.1 list and any other file in
    INPUTS), the price panel and the dividend table (1.2), the factor files, the universe files and the
    pinned Ken French zips. Planned files that do not exist are kept in the list (they are missing)."""
    inputs = sorted(p for p in ctx.inputs.glob("*") if p.is_file() and p.name != "manifest.json"
                    and not p.name.endswith(".tmp"))
    for name in list(PLAN_INPUTS) + ["validation_summary.json"]:
        if ctx.inputs / name not in inputs:
            inputs.append(ctx.inputs / name)
    cache = [ctx.cache / relative for relative in PLAN_CACHE]
    cache += [ctx.cache / "factors" / f for f in FACTOR_FILES]
    universe = ctx.cache / "universe"
    found = sorted(p for p in universe.glob("*") if p.is_file() and not p.name.endswith(".tmp")) if universe.exists() else []
    cache += [p for p in found if p not in cache]
    cache += sorted((ctx.cache / "raw" / "kf").glob("*.zip"))
    return inputs + cache


def _header(ctx: Context, path: Path) -> list[str]:
    """A csv file's column names (gzip or plain); the file is hashed like any read (a live file is read once)."""
    if ctx.is_live(path):
        frame = ctx.live_frame(str(path.relative_to(ctx.cache)))
        return [] if frame is None else list(frame.columns)
    if str(path) not in ctx.hashes:
        ctx.sha(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as handle:
        line = handle.readline()
    return next(csv.reader([line])) if line else []


def check_plan_files(ctx: Context) -> dict:
    """Plan 1.1 (and the 1.2 files the test reads): every planned file exists with the plan's columns, or
    a mapping documented here, and the plan's enumerated values."""
    name, dataset, plan = "plan_files", "manifest", "1.1 committed inputs; 1.2 local files"
    threshold = ("every 1.1 file and the 1.2 files the test reads exist; each has every column the plan names (or a "
                 "documented mapping); enumerated columns hold only the plan's values (or documented extensions)")
    files, missing_files, missing_columns, odd_values, mapped, extended = {}, [], [], [], [], []
    for relative, columns, kind in ([(n, c, "input") for n, c in PLAN_INPUTS.items()]
                                    + [(n, c, "cache") for n, c in {**PLAN_CACHE, **PLAN_CACHE_INFRA}.items()]):
        path = (ctx.inputs if kind == "input" else ctx.cache) / relative
        label = relative if kind == "input" else CACHE_KEY_PREFIX + relative
        if not path.exists():
            missing_files.append(label)
            files[label] = {"exists": False}
            continue
        frame = ctx.csv(relative) if kind == "input" else None
        have = list(frame.columns) if frame is not None else _header(ctx, path)
        absent = [c for c in columns if c not in have]
        for column in absent:
            note = PLAN_COLUMN_MAPPINGS.get((relative, column))
            if note:
                mapped.append({"file": label, "column": column, "mapping": note})
            else:
                missing_columns.append({"file": label, "column": column})
        files[label] = {"exists": True, "columns": len(have), "plan_columns_missing": absent,
                        "extra_columns": len([c for c in have if c not in columns])}
        if frame is None:
            continue
        for (vname, column), (allowed, extensions) in PLAN_VALUES.items():
            if vname != relative or column not in frame.columns:
                continue
            counts = frame[column].value_counts()
            for value, n in counts.items():
                if value in allowed:
                    continue
                if value in extensions:
                    extended.append({"file": label, "column": column, "value": value, "rows": int(n),
                                     "documented": extensions[value]})
                else:
                    odd_values.append({"file": label, "column": column, "value": value, "rows": int(n)})
        for (vname, column), (prefixes, note) in PLAN_VALUE_PREFIXES.items():
            if vname == relative and column in frame.columns:
                bad = frame.loc[~frame[column].str.startswith(prefixes), column].value_counts()
                odd_values += [{"file": label, "column": column, "value": v, "rows": int(n)} for v, n in bad.items()]
                mapped.append({"file": label, "column": column, "mapping": note})
    prices = sorted(p for p in (ctx.cache / "prices").glob("*.csv")) if (ctx.cache / "prices").exists() else []
    bad_price_files = []
    for path in prices:
        have = _header(ctx, path)
        if [c for c in PRICE_COLUMNS if c not in have]:
            bad_price_files.append(path.name)
    passed = not missing_files and not missing_columns and not odd_values and not bad_price_files
    return result(name, dataset, plan, threshold, passed,
                  numbers={"planned_files": len(files), "files_missing": len(missing_files),
                           "columns_missing": len(missing_columns), "columns_mapped": len(mapped),
                           "values_outside_plan": len(odd_values), "values_documented_extensions": len(extended),
                           "per_security_price_files": len(prices), "price_files_missing_columns": len(bad_price_files)},
                  details={"files_missing": missing_files, "columns_missing": missing_columns,
                           "values_outside_plan": odd_values[:LIST_LIMIT], "documented_mappings": mapped,
                           "documented_extensions": extended, "price_files_missing_columns": bad_price_files[:LIST_LIMIT],
                           "files": files,
                           "written_by_this_run": {"validation_summary.json": "every check of section 6",
                                                   "manifest.json": "keys " + ", ".join(MANIFEST_PLAN_KEYS)
                                                                    + " (checked when it is built)"}},
                  note=("1.1 files are read whole; 1.2 files by their header; prices/{security_id}.csv headers are checked "
                        "for every per-security file"))


def check_manifest_coverage(ctx: Context) -> dict:
    """Every file the test will read exists, so the manifest can hash all of them; a planned file that is
    missing fails the check."""
    name, dataset, plan = "manifest_coverage", "manifest", "6 Manifest"
    threshold = ("100% of the files the test reads (the plan 1.1 list, the 1.2 panel and dividend table, factor and "
                 "universe files) exist and are hashed in manifest.json (built at the end of this run)")
    files = files_for_test(ctx)
    missing = [ctx.key(p) for p in files if not p.exists() and p.name != "validation_summary.json"]
    planned = set(PLAN_INPUTS) | {CACHE_KEY_PREFIX + r for r in PLAN_CACHE}
    return result(name, dataset, plan, threshold, not missing,
                  numbers={"files": len(files), "missing": len(missing),
                           "planned_missing": len([m for m in missing if m in planned])},
                  details={"missing": missing}, status="pass" if not missing else "fail",
                  note=("validation_summary.json is written by this run before the manifest is built; files a check reads "
                        "but the test does not are hashed under validation_inputs"))


CHECKS = [
    check_factor_rows, check_factor_hashes, check_qqq_join, check_ff_industry_maps,
    check_listing_snapshots, check_form25, check_security_master, check_candidates_resolved,
    check_earnings_coverage, check_earnings_gaps, check_earnings_hand_sample, check_earnings_header_times, check_sic_ff49,
    check_panel_integrity, check_tiingo_row_check, check_multi_source_agreement, check_v_sample, check_review_queue,
    check_stored_comparison, check_split_agreement, check_known_cases, check_dividends,
    check_terminal_coverage, check_terminal_open,
    check_universe_build, check_universe_vendor_source, check_universe_proxy_margin, check_universe_capture_coverage,
    check_universe_nasdaq100, check_universe_form25, check_universe_fetch_margin, check_universe_unfillable,
    check_universe_listed_gaps, check_plan_files, check_manifest_coverage,
]


def check_name(function) -> str:
    return function.__name__.removeprefix("check_")


def run_checks(ctx: Context, only: set | None = None) -> list[dict]:
    results = []
    for function in CHECKS:
        if only and check_name(function) not in only:
            continue
        started = time.time()
        try:
            out = function(ctx)
        except Exception as exc:  # reported as a failed check, the run goes on
            out = result(check_name(function), "", "", "", False, status="error",
                         details={"error": f"{type(exc).__name__}: {exc}",
                                  "traceback": traceback.format_exc().splitlines()[-6:]})
        out["seconds"] = round(time.time() - started, 1)
        log(f"{out['status']:8s} {out['check']} ({out['seconds']}s)")
        results.append(out)
    return results


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def scripts_state() -> dict:
    """sha256 of every reversal_data script and test, and which ones differ from the commit."""
    paths = sorted(Path("scripts").glob("reversal_data_*.py")) + sorted(Path("tests").glob("test_reversal_data_*.py"))
    dirty = set()
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--", *map(str, paths)], capture_output=True, text=True,
                             check=True).stdout
        dirty = {line[3:].strip() for line in out.splitlines()}
    except (OSError, subprocess.CalledProcessError):
        pass
    return {str(p): {"sha256": common.sha256_file(p), "uncommitted": str(p) in dirty} for p in paths}


def build_summary(ctx: Context, results: list[dict], changes: dict | None = None) -> dict:
    """validation_summary.json in memory: every check, and the sha256 of every file the checks read."""
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code_version": CODE_VERSION,
        "scripts_git_commit": git_commit(),
        "plan": "docs/reversal_2012_2026_data_plan.md section 6 (with 1.1, 3.3, 4, 5.3)",
        "universe_basis": ctx.universe[1],
        "universe_definition": f"name-weeks with dv50 or dv20 rank <= {UNIVERSE_N}; prices to rank {PRICE_RANK}",
        "counts": dict(Counter(r["status"] for r in results)),
        "inputs_read": {ctx.key(k): v for k, v in sorted(ctx.hashes.items())},
        "inputs_read_note": ("sha256 of the bytes each check read; every file was hashed again after the checks and was "
                             "unchanged (else nothing is written), except the live files of the running Tiingo fetch, which "
                             "were read once and are listed under live_inputs_moved_on when they grew"),
        "live_inputs_moved_on": (changes or {}).get("live_moved_on", []),
        "passed": [r["check"] for r in results if r["passed"]],
        "failed": [r["check"] for r in results if not r["passed"]],
        "no_returns_aggregated": ("counts, shares of days or names and id lists only; single-security returns are used only "
                                  "to compare two sources of the same security, never averaged or ranked across stocks"),
        "checks": results,
    }


def summary_bytes(summary: dict) -> bytes:
    return (json.dumps(summary, indent=1, default=str) + "\n").encode("utf-8")


def write_validation_summary(ctx: Context, results: list[dict], path: Path | None = None) -> dict:
    """Build and write the summary alone (atomic); the run itself uses write_outputs."""
    summary = build_summary(ctx, results)
    common.atomic_write(path or ctx.inputs / "validation_summary.json", summary_bytes(summary))
    return summary


# ------------------------------------------------------------------ manifest

ENDPOINTS = {  # templates only: keys travel in headers or are redacted
    "tiingo": ("https://api.tiingo.com/tiingo/daily/{ticker}/prices?startDate=2011-06-01&endDate=2026-08-31 "
               "(key in the Authorization header)", "Tiingo free tier: internal use only; values stay in research_cache"),
    "tiingo_supported_tickers": ("https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip", "Tiingo; local only"),
    "yahoo": ("https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?events=div,splits", "unofficial endpoint; values stay local"),
    "nasdaqdatalink_wiki": ("https://data.nasdaq.com/api/v3/datatables/WIKI/PRICES?ticker={ticker}&api_key=REDACTED",
                            "WIKI Prices (community, ended 2018-03-27); values stay local"),
    "sec_submissions": ("https://data.sec.gov/submissions/CIK{cik:010d}.json", "SEC EDGAR, public"),
    "sec_headers": ("https://www.sec.gov/Archives/edgar/data/{cik}/{accession}-index-headers.html", "SEC EDGAR, public"),
    "sec_archives": ("https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{document}", "SEC EDGAR, public"),
    "sec_terminal_docs": ("https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{document}", "SEC EDGAR, public"),
    "sec_efts": ("https://efts.sec.gov/LATEST/search-index?forms=25-NSE&startdt={start}&enddt={end}&from={from}", "SEC EDGAR, public"),
    "sec_frames": ("https://data.sec.gov/api/xbrl/frames/dei/{concept}/{unit}/{period}.json", "SEC EDGAR, public"),
    "sec_companyfacts": ("https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", "SEC EDGAR, public"),
    "kenfrench": ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/{zip}",
                  "Ken French data library; redistribution terms unclear (plan 8 item 8); 202608 build pinned"),
    "cboe": ("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv", "CBOE public file"),
    "wayback": ("https://web.archive.org/web/{timestamp}id_/{original_url}", "Internet Archive captures of Nasdaq pages"),
    "nasdaq": ("https://api.nasdaq.com/api/quote/{symbol}/{historical|dividends}?assetclass=etf&...",
               "Nasdaq public API (QQQ tail and dividends)"),
    "invesco": ("https://www.invesco.com/us/financial-products/etfs/product-detail/main/distributions/03?"
                "audienceType=Investor&action=download&ticker=QQQ", "issuer page (QQQ distributions)"),
    "sec": ("https://data.sec.gov/submissions/CIK{cik:010d}.json and https://www.sec.gov/Archives/edgar/data/{cik}/"
            "{accession_nodash}/{document} (QQQ trust reports)", "SEC EDGAR, public"),
    "sec_files": ("https://www.sec.gov/files/company_tickers_exchange.json", "SEC EDGAR, public"),
}


def source_facts(ctx: Context) -> dict:
    """Per source: endpoint template, first and last request, request and status counts (raw_index.csv.gz, read
    once in this run)."""
    out = {}
    index = ctx.live_frame("raw_index.csv.gz")
    if index is not None and len(index):
        index = index[index["source"] != "source"]
        for source, group in index.groupby("source"):
            template, licence = ENDPOINTS.get(source, ("", ""))
            out[source] = {"endpoint_template": template, "fetched_from": group["fetched_utc"].min(),
                           "fetched_to": group["fetched_utc"].max(), "requests": int(len(group)),
                           "http_status": group["http_status"].value_counts().head(8).to_dict(), "license_note": licence}
    kf = ctx.cache / "factors" / "kf_sources.json"
    if kf.exists():
        sources = ctx.read_json(kf)
        template, licence = ENDPOINTS["kenfrench"]
        entry = out.setdefault("kenfrench", {"endpoint_template": template, "fetched_from": "", "fetched_to": "",
                                             "requests": 0, "license_note": licence})
        entry["files"] = {k: {"sha256": v.get("sha256"), "crsp_build": v.get("crsp_build")}
                          for k, v in sources.items() if isinstance(v, dict) and k.endswith(".zip")}
    return out


LOCAL_REUSED_NOTE = ("files already in the repo or research_cache that steps reused without a request: stored price files, "
                     "holdout and sue_lt caches, QQQ files (hashed in the step summaries; the ones the checks read are "
                     "under validation_inputs)")


def secret_values() -> list[str]:
    """The key values of the .env files, to confirm none appears in what is written (never printed)."""
    values = []
    for env in (".env.tiingo", ".env.nasdaqdatalink"):
        path = common.MAIN_CHECKOUT / env
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                _, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                if len(value) >= 12:
                    values.append(value)
    return values


def manifest_facts(ctx: Context) -> dict:
    """What the manifest needs beyond the file hashes, read before the inputs are hashed again."""
    raw_index = ctx.cache / "raw_index.csv.gz"
    sources = source_facts(ctx)
    return {"scripts_git_commit": git_commit(), "scripts": scripts_state(), "sources": sources,
            "raw_index_sha256": ctx.hashes.get(str(raw_index), "")}


def build_manifest(ctx: Context, summary: dict, summary_data: bytes | None = None, facts: dict | None = None) -> dict:
    """manifest.json in memory: the plan's keys (generated_utc, scripts_git_commit, sources, sha256{relative_path},
    raw_index_sha256), the facts of every file the test reads (``files``) and the hashes of the other files the
    checks read (``validation_inputs``). The sha256 of each file is the one recorded when the run read it (files
    the checks did not read were hashed at the start of the run). Nothing is written here."""
    facts = facts or manifest_facts(ctx)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    files = files_for_test(ctx)
    summary_path = ctx.inputs / "validation_summary.json"
    entries, missing = {}, []
    for path in files:
        if path == summary_path and summary_data is not None:
            entries[ctx.key(path)] = {"kind": "input", "bytes": len(summary_data), "modified_utc": summary["generated_utc"],
                                      "sha256": common.sha256_bytes(summary_data)}
            continue
        if not path.exists():
            missing.append(ctx.key(path))
            continue
        digest = ctx.hashes.get(str(path)) or ctx.sha(path)
        stat = path.stat()
        entries[ctx.key(path)] = {"kind": "input" if path.parent == ctx.inputs else "cache", "bytes": stat.st_size,
                                  "modified_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                                  "sha256": digest}
    in_files = {str(p) for p in files}
    others = {ctx.key(k): v for k, v in sorted(ctx.hashes.items()) if k not in in_files}
    manifest = {
        "generated_utc": now,
        "updated_utc": now,
        "scripts_git_commit": facts["scripts_git_commit"],
        "scripts": facts["scripts"],
        "sources": facts["sources"],
        "local_reused_note": LOCAL_REUSED_NOTE,
        "raw_index_sha256": facts["raw_index_sha256"],
        "raw_index_note": ("raw_index.csv.gz keeps growing while the Tiingo run is active; this hash is of the copy this "
                           "run read"),
        "sha256": {key: e["sha256"] for key, e in sorted(entries.items())},
        "files": dict(sorted(entries.items())),
        "files_note": ("sha256{relative_path: hash} is the plan's map: INPUTS files by name, research_cache files as "
                       f"{CACHE_KEY_PREFIX}...; files{{}} holds the same hashes with size and time; every file the test "
                       "reads is here, and the other files the checks read are under validation_inputs"),
        "files_missing": missing,
        "validation_inputs": others,
        "validation": {"summary": "validation_summary.json", "counts": summary.get("counts", {}),
                       "failed": summary.get("failed", [])},
    }
    absent = [k for k in MANIFEST_PLAN_KEYS if k not in manifest]
    short = {name: [k for k in MANIFEST_SOURCE_KEYS if k not in rec] for name, rec in manifest["sources"].items()}
    short = {k: v for k, v in short.items() if v}
    if absent or short:
        raise RuntimeError(f"the manifest lacks plan keys: {absent} {short}")
    return manifest


class InputsChanged(RuntimeError):
    """An input changed while the checks ran; nothing was written."""


def write_outputs(ctx: Context, results: list[dict], manifest: bool = True) -> tuple[dict, dict | None]:
    """The run's last step: hash every input again, then write validation_summary.json and manifest.json.
    Both are built in memory first; nothing is written when an input changed during the run or a key
    value would appear in either file, so the files from the previous run stay as they were."""
    facts = manifest_facts(ctx) if manifest else None
    changes = ctx.input_changes()
    if changes["blocking"]:
        raise InputsChanged("inputs changed during the run, nothing written: "
                            + ", ".join(c["file"] for c in (changes["changed"] + changes["gone"])[:LIST_LIMIT]))
    summary = build_summary(ctx, results, changes)
    data = summary_bytes(summary)
    built = build_manifest(ctx, summary, data, facts) if manifest else None
    text = (json.dumps(built, indent=2, sort_keys=True) + "\n") if built is not None else ""
    secrets = secret_values()
    if any(value in data.decode("utf-8") or value in text for value in secrets):
        raise RuntimeError("a key value appears in the summary or the manifest; nothing was written")
    common.atomic_write(ctx.inputs / "validation_summary.json", data)
    if built is not None:
        common.atomic_write(ctx.inputs / "manifest.json", text.encode("utf-8"))
    return summary, built


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--only", default="", help="comma-separated check names (printed, nothing written)")
    parser.add_argument("--no-manifest", action="store_true", help="write the summary but not the manifest")
    args = parser.parse_args(argv)
    ctx = Context()
    only = {s.strip() for s in args.only.split(",") if s.strip()} or None
    if not only:
        log("hashing the files the test reads (compared again at the end)")
        for path in files_for_test(ctx):
            if path.exists() and path != ctx.inputs / "validation_summary.json":
                ctx.sha(path)
    results = run_checks(ctx, only)
    for r in results:
        print(json.dumps({"check": r["check"], "status": r["status"], "numbers": r["numbers"]}, default=str)[:1500])
    if only:
        changes = ctx.input_changes()
        log(f"inputs changed during the run: {[c['file'] for c in changes['changed'] + changes['gone']]}")
        return 0
    try:
        summary, manifest = write_outputs(ctx, results, manifest=not args.no_manifest)
    except InputsChanged as exc:
        log(str(exc))
        return 2
    log(f"validation_summary.json: {summary['counts']}")
    if manifest is not None:
        log(f"manifest.json: {len(manifest['files'])} files hashed, missing {len(manifest['files_missing'])}, "
            f"{len(manifest['validation_inputs'])} other validation inputs")
    if summary["live_inputs_moved_on"]:
        log(f"live files that grew during the run: {[c['file'] for c in summary['live_inputs_moved_on']]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
