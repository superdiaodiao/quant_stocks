"""Plan step 6 (docs/reversal_2012_2026_data_plan.md, section 3.2): liquidity pre-filter and fetch list.

Data only. Nothing here computes returns, signals, return rankings or spreads.
The only ranks are dollar-volume ranks (median raw close x raw volume) and
market-cap / public-float ranks, used to decide which price histories to fetch.

Inputs (all local, read only):
- ``security_master.csv`` and ``ticker_intervals.csv`` (steps 3-4): which
  security held which Nasdaq ticker when, delist dates, the foreign-filer flag;
- ``form25_nasdaq_2012_2026.csv`` (step 3): public float before each delisting;
- ``listing_snapshots_index.csv`` and ``CACHE/listings/companylist`` (step 2):
  Wayback company lists with LastSale and MarketCap, 2011-2019;
- price series, every one we already hold:
  * WIKI raw (``CACHE/wiki/by_ticker``, 2011-06-01..2018-03-27): vendor raw;
  * Tiingo JSON caches (``research_cache/sue_lt_2020_2026/raw``,
    ``research_cache/tiingo_delisted``, ``research_cache/tiingo_overlap``): vendor raw
    (``close``/``volume`` as traded);
  * Yahoo charts (``research_cache/holdout_2011_2019/yahoo_nominal``): vendor raw
    after the documented split restore (close x later split ratios, volume / the
    same; holdout data report section 5.1);
  * the stored repo files (``cleaned_stocks_data/price``): NOT vendor raw. They are
    split- and (to about 2023) dividend-adjusted, with volume scaled the opposite
    way, so close x volume is the raw dollar volume (checked against WIKI:
    median ratio 0.999-1.000 in 2011-2015, see ``stored_dv_check``). Their close
    is restored to raw only by the split events we know (Yahoo, Tiingo,
    WIKI, ``confirmed_price_adjustments.csv``); a stored close without such a
    restore is an estimate (``price_flag`` E), never a vendor level.

Universe: Nasdaq common stock that is not a foreign filer that week (the owner excludes foreign
filers; a MIXED filer only in the weeks whose latest periodic report before that week is foreign,
from step 4's ``periodic_form_history.csv``: TEVA to 2018-02-11, Atlassian to 2022-11-03), not an
unmerged SPAC shell (current SIC 6770 that never turned operating, no merger while listed:
``spac_shells``; plan 3.1's name pattern misses Sentinel Energy Services, Bridgetown Holdings ...), and
(round 7, owner decision D6) not an investment company that week: closed-end funds and BDCs, by step 12's
rule on the cached SEC submissions (``investment_spans``; ARCC, BANX, OFS, RAND, ...).

Rules (plan section 3.2):
- A1: 2012-01..2018-03, dv rank <= 300 in any week, from WIKI raw, and the listed
  interval is not covered by a vendor raw series (a 2016 week priced only by a stored file
  meets it at rank 330: stored dollar volume runs about 12% low that year);
- A2: 2011-11..2019-06, Wayback company-list market cap rank <= 400 on any capture;
- A3 (proxy fallback, reported separately): a Form 25 delisting 2012-01..2018-03 with
  float >= $1B, not covered and not caught by A1/A2 (mostly the 2012-2014-03
  delistings that WIKI lacks);
- B-A / B-B / B-C: Form 25 delistings 2018-04..2024-06, maximum checked public float
  in the 3 years before (>= $1B / $500M-$1B / $300M-$500M, tier C a seeded sample
  of 20 names some request can price, the rest conditional on the sample: round 7,
  ``tier_c_sample_result`` ranks the sample's own Tiingo answers by dv50 against step 6's universe; one
  at rank <= 300 triggers the rest for month 2, none (all answered) makes it ``tier_c_not_triggered``);
- C: a series that starts more than 60 days after the first snapshot appearance,
  where A1/A2/B-A hold for the missing window;
- S: names delisted after 2024-06 whose only series is a stored file and which rank <= 300,
  and (S_float) Form 25 delistings after 2024-06 with a tier-A/B float and no price at all in
  their uncovered weeks (Cerevel, Encore Wire); month 1, after C and before the samples;
- V: a stratified 50-name verification sample of active names (Tiingo next to Yahoo), each
  category keeping 8 slots (special dividends included), under the Yahoo ticker;
- Y: active names ranked <= 300 in any week whose listed interval no vendor raw series
  covers (plan step 7, Yahoo);
- Y_active_all: every name listed now (an open interval at the latest snapshot, common stock in
  the last week, not a SPAC shell; nor a blank-check shell ``spac_shells`` misses, ``spac_like_now``)
  with a universe (non-foreign) week that no vendor raw series
  covers, whatever its dollar volume or size (round 5: SMCI, CHRD, CORZ and SIGA were listed but
  never ranked here, because their later listings after a Form 25 were cut to one day). Yahoo.
  Round 6: also every name still trading by SEC (``active``: a current ticker, no delisting) that is
  not on Nasdaq now (moved to NYSE: UCBI -> UCB, CNMD; out of the latest snapshots: BANX, OFS, RAND),
  under its SEC current ticker, unless Yahoo cannot be asked for it (``yahoo_excluded``: a when-issued
  or when-distributed line, LBYAV; a class or tracking stock whose CIK tickers name another one, LLYVA).

Routing (plan section 3.2): 2011-06..2018-03-27 -> WIKI when it has the security
and the entity check passes; delisted names after that -> Tiingo only when one
``supported_tickers`` row covers the needed interval and is the row the API serves for its
ticker (the one that ends latest: trial 2026-10-01 CA, CZR, GPOR; probes 2026-10-02 FRG). The
tickers tried: the security's own (newest first), a reviewed alias (TFCFA/TFCF for 21st Century
Fox, MSGN, ZG for old Zillow), own + Q (Tiingo keeps a bankrupt company's whole Nasdaq history under
its OTC ticker: CLVSQ, ENDPQ, AKRXQ), the CIK's current SEC tickers, own + one letter + Q (SDCCQ).
A row labelled ETF counts when it starts and ends with the security (PAND). A covering row that
the API does not serve is ``hidden``; a row of another company (starting after the security had
used the ticker, inside the need: ACET; or served while a hidden row is the security's own:
COHR, VIVO) is ``newer_company``; both go to ``unfillable.csv`` without a request. Ambiguous
matches carry ``tiingo_flags`` (several rows, SEC holder another CIK, late start, shared ticker,
alias, non-stock row) and the fetcher confirms their entity. Active names -> Yahoo; nothing ->
``unfillable.csv``. ``fetch_order`` numbers the Tiingo tickers in the order the fetcher asks.
The fetcher's verdicts on answers it holds (``CACHE/tiingo/fetch_status.csv``) feed back: a ticker
whose answer was another company is passed over for that security. The Yahoo build's verdicts
(``CACHE/yahoo/entity_report.csv``) feed back too (round 6, ``yahoo_fallback``): each Yahoo row's status
(done / done_review / partial / yahoo_failed, pending without an answer), and for an answer that failed
or covers under half of the need a Tiingo row for the uncovered span (month 2, ``pending_month2`` from round 7,
which the month-2 command selects, ``MONTH_2_STATUSES``; old and SEC current tickers; Wolfspeed's hidden WOLF
row is asked only on purpose, ``ask_shadowed``), else an unfillable row. A relisting after a Form 25 is a ``junction_date`` for the Yahoo build only when it
is new equity, a bankruptcy or a share exchange (``relisting_kinds``: step 9's RELIST_JUNCTIONS, the
terminal table, the Form 25 basis), and the junction is the new shares' first session (step 9's
``first_new_session``: CHRD 2020-11-20, CORZ 2024-01-24, WW 2025-06-27); there the Yahoo need has no warm-up
in the old shares. The same shares listed again (SMCI, SIGA, SCOR, MDXG) keep one series, no junction.

Round 9:
- a need that crosses a listing gap of more than 103 days (the 75-day warm-up plus the 28-day hold) is cut there,
  one row per listing run (``listing_runs``, ``split_at_listing_gaps``): a piece ends on its run's last listed day
  and the next starts on its run's first, pieces without an uncovered universe week are dropped, and the note says
  ``need split at the listing gaps``. The weeks between two listings are not universe weeks, so they no longer make
  a Tiingo match partial or open an unfillable window (Capstone: CGRN to its 2023-10-22 Form 25, OTC as CGRNQ, CEPL
  from 2026-07-02; Frontier's old FTR shares and the FYBR shares after the bankruptcy). A Tiingo match prefers an
  answer the fetcher already holds when it serves the need as well (HGENQ for Humanigen's KBIO years);
- each Yahoo answer is judged on the row's own need (``row_verdict``): Yahoo's first trade against the row's own
  start (its junction date, else its need start) rather than the security's first listing (WW, CORZ, THRY), and a
  partial answer to the merged request is ``done`` for a row whose own, shorter need it covers (CEPL's piece);
- the Tiingo fetcher's verdicts are written back to the Tiingo rows (``tiingo_answers_back``): status and the
  month of the answer, a changed need re-checked on the answer's rows; rows not answered keep the plan's status;
- step 6's own float check leaves out a $10B+ XBRL float with no close to test it that is 20x the CIK's checked
  Form 25 float (DIRTT's $17.07B of 2023), so weekly_metrics.pkl (step 12's proxies, validate) no longer carries it;
- the budget block names the symbols spent above the fetcher's default stop (October: 500, with --month-stop 500).

Data hygiene applied on the way (each counted in prefilter_summary.json):
- WIKI rows are cut to the security's listing spans and a WIKI file whose raw close
  disagrees with the company lists' LastSale is dropped (plan 4.4 R9: LSI, TIVO, ...);
- a stored file's rows belong to the file's company (step 4's price_file_cik_map): rows the
  ticker map gave to an earlier holder of the ticker move to the owner when it was listed
  then (ContextLogic's LOGC over LogicBio), and stay with the predecessor otherwise;
- weeks after the last trade (before the Form 25 takes effect) are not "uncovered";
- XBRL floats with x1000 unit errors are dropped ($3,000 a share, 50x the CIK's other facts,
  and from $10B more than 3x shares x the security's close: Mister Car Wash $604B, DIRTT $17.1B);
- a security whose successor (step-4 link) holds its history in the successor's stored file
  goes to Yahoo under the successor's ticker; Tiingo is asked only for tickers the security
  still held during the need;
- a needed window that would run into a foreign-filer span at the end of the listing (CBPO)
  stops 4 weeks after the last universe week;
- round 6: a later listing after a Form 25 needs a symbol-directory snapshot or a price row inside it
  (``confirm_after_cut``: LLEX, PBIO are cut to one day again); the IPO rule keeps the snapshot start at
  step 12's published mapping boundary and drops the rows before a security's own later stored file
  there (``boundary_trims``: TrueCar 2014-03-26..05-15); an XBRL float with an x1000 unit error is
  corrected when the corrected value checks out (``unit_fix``: Codiak, Vericity, Vintage Wine Estates 2021)
  and left out otherwise (``float_unit_fixes.csv`` says which and why).

Outputs:
  INPUTS/candidate_fetch_list.csv, INPUTS/unfillable.csv   (ids, dates, ranks, SEC floats; no price levels;
                                        this step owns unfillable.csv again from round 6)
  CACHE/prefilter/tiingo_month2_plan.csv every name only Tiingo can serve, ranked by expected top-250
                                        name-weeks (no investment-company weeks; a share class or tracking
                                        stock gets nothing from its company's market cap or float), with the
                                        500-symbol and the fetcher's cut lines and ``fetch_selectable`` (what
                                        ``reversal_data_tiingo.py --month2`` asks)
  CACHE/prefilter/investment_company_spans.csv  the D6 spans applied (security_id, cik, ticker, start, end)
  CACHE/prefilter/float_unit_fixes.csv  the XBRL floats taken for unit errors: corrected or left out, why
  CACHE/prefilter/daily_series.pkl      per security-day close/volume/dv/source (local only)
  CACHE/prefilter/weekly_metrics.pkl    per listed security-week: dv20, dv50, ranks, flags, proxies
  CACHE/prefilter/weekly_coverage.csv   per week: how many of ranks 1-300 have vendor raw
  CACHE/prefilter/security_facts.csv.gz, event_flags.csv, spans/lists/files/entity.csv.gz
  CACHE/prefilter/supported_tickers_{date}.zip   Tiingo's public ticker list (no key)
  CACHE/prefilter/unknown_size_delisted.csv  delisted names whose universe weeks have neither a series
                                        (vendor, stored, or fetched since) nor a size proxy; counts only
  CACHE/prefilter/prefilter_summary.json counts, checks, budget

Usage::

    PYTHONPATH=. python scripts/reversal_data_prefilter.py            # build (downloads the Tiingo list once)
    PYTHONPATH=. python scripts/reversal_data_prefilter.py --offline  # fail instead of downloading
    PYTHONPATH=. python scripts/reversal_data_prefilter.py --offline --tiingo-already-used N  # budget from N
    PYTHONPATH=. python scripts/reversal_data_prefilter.py --offline --out-dir DIR  # a trial build: every output in DIR
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import io
import json
from pathlib import Path
import re
import sys
import time
import zipfile

import numpy as np
import pandas as pd

from pipelines.reversal_data import common
from quant.data import version as _version

MAIN = common.MAIN_CHECKOUT
OUT = common.CACHE / "prefilter"
INPUTS = common.INPUTS
MASTER = INPUTS / "security_master.csv"
INTERVALS = INPUTS / "ticker_intervals.csv"
FORM25 = INPUTS / "form25_nasdaq_2012_2026.csv"
SNAPSHOT_INDEX = INPUTS / "listing_snapshots_index.csv"
CANDIDATES = INPUTS / "candidate_fetch_list.csv"
UNFILLABLE = INPUTS / "unfillable.csv"
TERMINAL = INPUTS / "terminal_returns_2012_2026.csv"

WIKI_DIR = common.CACHE / "wiki" / "by_ticker"
STORED_DIR = MAIN / "cleaned_stocks_data" / "price"
YAHOO_DIRS = [MAIN / "research_cache" / "holdout_2011_2019" / "yahoo_nominal"]
TIINGO_DIRS = [MAIN / "research_cache" / "sue_lt_2020_2026" / "raw",
               MAIN / "research_cache" / "tiingo_delisted",
               MAIN / "research_cache" / "tiingo_overlap"]
CONFIRMED_ADJUSTMENTS = MAIN / "stocks_list_dir" / "nasdaq" / "confirmed_price_adjustments.csv"

WINDOW_START, WINDOW_END = "2011-06-01", "2026-08-31"
FIRST_WEEK, LAST_WEEK = "2012-01-06", "2026-07-17"
WIKI_END = "2018-03-27"
FETCH_RANK = 300
STORED_2016_RANK = 330  # A1's cut for 2016 weeks priced only by a stored file (see security_facts)
MCAP_RANK = 400
MIN_PRICE = 10.0
DV_WINDOWS = {20: 10, 50: 25}  # sessions -> minimum observations for a median
CLOSE_STALE_SESSIONS = 5  # a week-end close may be this many sessions old
LATE_START_DAYS = 60
A1_WINDOW = ("2012-01-01", WIKI_END)
A2_WINDOW = ("2011-11-01", "2019-06-30")
B_WINDOW = ("2018-04-01", "2024-06-30")
S_AFTER = "2024-06-30"
FLOAT_TIERS = (("B_A", 1e9, float("inf")), ("B_B", 5e8, 1e9), ("B_C", 3e8, 5e8))
A3_FLOAT = 1e9
GOOD_FLOAT_FLAGS = {"ok", "review", "no_shares"}
TIER_C_SAMPLE, V_SAMPLE = 20, 50
SEED = 20261001
MONTH_1, MONTH_2 = "2026-10", "2026-11"
TIINGO_MONTHLY_SYMBOLS = 500
TIINGO_MONTH_STOP = 480  # unique symbols a month the fetcher stops at (the free tier allows 500)
TIINGO_RANGE_SLACK_DAYS = 7
TIINGO_SUPPORTED_URL = "https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip"
# Plan section 3.2: known names no free source has.
KNOWN_UNFILLABLE = {"BMC", "LNCR", "MOLX", "ONXX", "PMTC", "CNSIV", "FMCN", "MRKT"}
# Plan section 3.2 V: the 2025-06-24 break names it lists, and the splits it names after 2023.
V_NAMED = ["KLAC", "NFLX", "BKNG", "HON", "CRWD", "AZN", "LCID", "TLRY"]
V_KNOWN_EVENTS = {  # ticker -> (date, kind) from plan section 4.3
    "PRPL": ("2026-07-20", "split_after_2023"), "SIRI": ("2024-09-10", "split_after_2023"),
    "NFLX": ("2025-11-17", "split_after_2023"), "BKNG": ("2026-04-06", "split_after_2023"),
    "KLAC": ("2026-06-12", "split_after_2023"), "MNST": ("2026-08-11", "split_after_2023"),
    "HON": ("2025-10-30", "odd_split"),
}
BREAK_DATE = "2025-06-24"
SOURCE_RANK = {"wiki": 0, "tiingo": 1, "yahoo": 2, "stored": 3}
VENDOR = {"wiki", "tiingo", "yahoo"}


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


# ------------------------------------------------------------------ calendar

def xnas_sessions(start: str = WINDOW_START, end: str = WINDOW_END) -> pd.DatetimeIndex:
    import exchange_calendars as xcals

    calendar = xcals.get_calendar("XNAS", start="2010-01-04", end="2026-12-31")
    return pd.DatetimeIndex(calendar.sessions_in_range(start, end)).tz_localize(None)


def week_ends(sessions: pd.DatetimeIndex, first: str = FIRST_WEEK, last: str = LAST_WEEK) -> pd.DatetimeIndex:
    """The last XNAS session of each calendar week (Monday-Sunday) from ``first`` to ``last``."""
    frame = pd.Series(sessions, index=sessions)
    ends = frame.groupby(sessions.to_period("W-SUN")).max()
    ends = pd.DatetimeIndex(ends.values)
    return ends[(ends >= pd.Timestamp(first)) & (ends <= pd.Timestamp(last))]


# ------------------------------------------------------------------ identity and listing

def read_csv_text(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def load_master(path: Path = MASTER) -> pd.DataFrame:
    return read_csv_text(path)


def load_intervals(path: Path = INTERVALS) -> pd.DataFrame:
    """Snapshot-evidence Nasdaq intervals only (SEC current-ticker rows carry no listing dates)."""
    intervals = read_csv_text(path)
    return intervals[(intervals["start"] != "") & (intervals["exchange"] == "NASDAQ")].reset_index(drop=True)


def _day_before(day: str) -> str:
    return (pd.Timestamp(day) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def listing_spans(intervals: pd.DataFrame, master: pd.DataFrame, window_end: str = WINDOW_END) -> pd.DataFrame:
    """One row per interval with the dates it counts as listed (plan section 3.1, simplified).

    ``list_start`` is the first snapshot that shows it; ``list_end`` reaches to the day before
    the next full-list snapshot without it (``end_next_absent``) or, when no later snapshot
    misses it, to ``window_end``; a Form 25 delist date earlier than that cuts it. An interval
    that starts on or after the delist date is a later listing and is not cut (``after_cut``, as
    in step 12: SMCI relisted 2020-02 after its 2019-03 Form 25, Oasis/Chord 2020-11, Core
    Scientific 2024-03; cut to one day, step 6 never saw their later weeks). Such a later listing
    still needs evidence (``confirm_after_cut``, once the prices are read). The IPO rule (prices
    between two snapshots) is applied later, in ``extend_starts``. ``sources`` keeps the
    interval's snapshot sources and ``snapshot_start`` its first snapshot.
    """
    delist = dict(zip(master["security_id"], master["delist_date"]))
    rows = []
    for row in intervals.itertuples(index=False):
        end = _day_before(row.end_next_absent) if row.end_next_absent else window_end
        end = max(end, row.end)
        cut = delist.get(row.security_id, "")
        after = bool(cut) and cut <= row.start
        if cut and not after and cut < end:
            end = max(cut, row.start)
        rows.append({"security_id": row.security_id, "ticker": row.ticker, "list_start": row.start,
                     "list_end": end, "start_prev_absent": row.start_prev_absent, "obs_end": row.end,
                     "name": row.name_in_source, "share_class": row.share_class, "after_cut": after,
                     "sources": str(getattr(row, "sources", "") or getattr(row, "source", "") or ""),
                     "snapshot_start": row.start})
    return pd.DataFrame(rows)


# A later listing after a Form 25 counts only with a symbol-directory file among its snapshots
# (nasdaqlisted.txt: repo_symdir / wayback_symdir) or a price row inside it (round 6: LLEX
# 2017-03-16..09-11 and PBIO 2017-08-13..09-11 came from 14 and 3 Wayback company lists only, with
# no price at all; Pressure BioSciences traded OTC from 2012). Step 12 needs the same rule.
AFTER_CUT_EVIDENCE = re.compile(r"symdir")


def confirm_after_cut(spans: pd.DataFrame, best: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """``spans`` with each unconfirmed later listing (``after_cut`` with neither a symbol-directory
    source nor a price row of the security inside it) cut as before round 5: to its first day,
    ``after_cut`` False and ``after_cut_unconfirmed`` True. Returns (spans, the intervals cut)."""
    spans = spans.copy()
    spans["after_cut_unconfirmed"] = False
    if "after_cut" not in spans:
        return spans, []
    spans["after_cut"] = spans["after_cut"].astype(str).eq("True")
    dates = {s: np.sort(d.values.astype("datetime64[D]")) for s, d in best.groupby("security_id")["date"]}
    cut = []
    for k in spans.index[spans["after_cut"]]:
        row = spans.loc[k]
        symdir = bool(AFTER_CUT_EVIDENCE.search(str(row.get("sources", "") or "")))
        mine = dates.get(row["security_id"], np.array([], dtype="datetime64[D]"))
        inside = int(((mine >= np.datetime64(row["list_start"])) & (mine <= np.datetime64(row["list_end"]))).sum())
        if symdir or inside:
            continue
        cut.append({"security_id": row["security_id"], "ticker": row["ticker"], "list_start": row["list_start"],
                    "list_end_was": row["list_end"], "sources": row.get("sources", ""), "price_rows": inside})
        spans.loc[k, ["list_end", "after_cut", "after_cut_unconfirmed"]] = [row["list_start"], False, True]
    return spans, cut


def non_common_interval(name: str, share_class: str) -> bool:
    """The investable filter on the listed name (SPACs, LPs, funds...); ADRs are left to the foreign flag."""
    from src.io.security_universe import NON_COMMON_SECURITY_PATTERN

    if share_class == "ADS":
        return False
    return bool(re.search(NON_COMMON_SECURITY_PATTERN, str(name or ""), re.IGNORECASE))


# ------------------------------------------------------------------ foreign filers

# The owner excludes foreign filers; a MIXED filer only in the weeks whose latest periodic report
# before that week is foreign (20-F/40-F, or 6-Ks with no 10-K/10-Q around them). Step 4 settles the
# regime from every submissions page and writes it per filing to periodic_form_history.csv
# (regime_in_force from each filing date; before a CIK's first filing, that filing's regime): TEVA
# is foreign to 2018-02-11 and Atlassian to 2022-11-03, which the recent submissions block alone
# did not show.
PERIODIC_HISTORY = INPUTS / "periodic_form_history.csv"
ALWAYS = [("1900-01-01", "2100-01-01")]


def foreign_spans_from_timeline(timeline: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Spans [start, end] in which the regime in force is 'F', from (filing date, regime in force from
    that date) pairs, oldest first; before the first filing the first filing's regime applies."""
    if not timeline:
        return []
    spans, current, since = [], timeline[0][1], "1900-01-01"
    for day, kind in timeline[1:]:
        if kind != current:
            if current == "F":
                spans.append((since, _day_before(day)))
            current, since = kind, day
    if current == "F":
        spans.append((since, "2100-01-01"))
    return spans


def parse_spans(text: str) -> list[tuple[str, str]]:
    """'a..b c..d' (security_master.foreign_spans) as [(a, b), (c, d)]."""
    return [tuple(part.split("..", 1)) for part in str(text or "").split() if ".." in part]


def clip_spans(spans: list[tuple[str, str]], low: str = WINDOW_START, high: str = WINDOW_END) -> list[tuple[str, str]]:
    return [(max(a, low), min(b, high)) for a, b in spans if b >= low and a <= high]


def history_timelines(history: pd.DataFrame) -> dict[int, list[tuple[str, str]]]:
    """cik -> [(filing date, regime in force from it)], one entry per filing day, oldest first."""
    out = {}
    frame = history.assign(cik=history["cik"].astype(int)).sort_values(["cik", "filing_date"], kind="stable")
    for cik, group in frame.groupby("cik"):
        last = group.drop_duplicates("filing_date", keep="last")
        out[int(cik)] = list(zip(last["filing_date"].astype(str), last["regime_in_force"].astype(str)))
    return out


def foreign_spans(master: pd.DataFrame, history_path: Path = PERIODIC_HISTORY) -> tuple[dict, dict]:
    """(security_id -> spans when it is a foreign filer, facts). Y: always. MIXED: from
    periodic_form_history.csv; a MIXED CIK missing there falls back to security_master.foreign_spans,
    and with neither it is foreign throughout (counted). N and UNKNOWN: never. The facts also list
    MIXED CIKs whose history spans, clipped to the data window, differ from security_master's."""
    history = read_csv_text(history_path) if Path(history_path).exists() else pd.DataFrame(
        columns=["cik", "filing_date", "regime_in_force"])
    timelines = history_timelines(history) if len(history) else {}
    out, facts = {}, {"mixed_from_history": 0, "mixed_from_master_spans": 0, "mixed_foreign_throughout": 0,
                      "history_ciks": len(timelines), "history_vs_master_spans_differ": []}
    for row in master.itertuples(index=False):
        if row.foreign_filer == "Y":
            out[row.security_id] = ALWAYS
        elif row.foreign_filer == "MIXED":
            cik = int(row.cik)
            master_spans = parse_spans(row.foreign_spans)
            if cik in timelines:
                spans = foreign_spans_from_timeline(timelines[cik])
                facts["mixed_from_history"] += 1
                if clip_spans(spans) != clip_spans(master_spans):
                    facts["history_vs_master_spans_differ"].append(row.security_id)
            elif master_spans:
                spans = master_spans
                facts["mixed_from_master_spans"] += 1
            else:
                spans = ALWAYS
                facts["mixed_foreign_throughout"] += 1
            out[row.security_id] = spans
    return out, facts


def is_foreign_on(spans: list[tuple[str, str]] | None, day: str) -> bool:
    return bool(spans) and any(a <= day <= b for a, b in spans)


# ------------------------------------------------------------------ investment companies (owner decision D6)

# Owner decision D6 (plan section 0, 2026-10-02): closed-end funds and business development companies are not
# common stock; they leave the universe base from the first week the issuer is one (CRSP share codes 10/11).
# Step 12 owns the rule (``reversal_data_universe.investment_companies``: an N-54A election to its N-54C
# withdrawal, runs of investment-company filings, SIC 6726), read from the cached SEC submissions only (no
# request). Step 6 applies the same spans, so it no longer plans prices for those weeks (round 7: the
# off-Nasdaq Yahoo rows BANX, OFS, RAND, GECC, WHF, TCPC ...; some size-unknown delisted names).
IC_SPANS_OUT = OUT / "investment_company_spans.csv"


def investment_spans(master: pd.DataFrame, security_ids, spans: pd.DataFrame | None = None
                     ) -> tuple[dict[str, list[tuple[str, str]]], pd.DataFrame, dict]:
    """(security -> [(start, end)] investment-company spans, one row per issuer with evidence, read facts),
    by step 12's rule on the cached SEC submissions and ``sic_history.csv`` (with its listing ends, when step 12
    has them: a span closing near a delisting runs on to the listing's end, the merger tail)."""
    from pipelines.reversal_data import universe

    sic = read_csv_text(universe.SIC_HISTORY) if Path(universe.SIC_HISTORY).exists() else None
    if spans is not None and hasattr(universe, "listing_ends"):
        terminal = read_csv_text(TERMINAL) if TERMINAL.exists() else None
        ends = universe.listing_ends(spans, master, terminal)
        return universe.investment_companies(master, security_ids, sic, ends=ends)
    return universe.investment_companies(master, security_ids, sic)


def investment_span_table(spans: dict, master: pd.DataFrame) -> pd.DataFrame:
    """security_id, cik, ticker, start, end: the spans step 6 applied (as step 12 writes them)."""
    info = master.set_index("security_id")
    rows = [{"security_id": sid, "cik": info["cik"].get(sid, ""), "ticker": info["first_ticker"].get(sid, ""),
             "start": a, "end": b} for sid, mine in sorted(spans.items()) for a, b in mine]
    return pd.DataFrame(rows, columns=["security_id", "cik", "ticker", "start", "end"])


# ------------------------------------------------------------------ SPAC shells

# Plan 3.1 removes SPACs by name, and the listed names miss many (Sentinel Energy Services,
# Bridgetown Holdings, Gores Holdings VII, Trinity Merger Corp.). An unmerged blank-check shell is
# found from SEC facts instead: current SIC 6770 that never turned into an operating SIC, and no
# rename to an operating name while it was listed (a merged SPAC takes the target's name: Inspirato,
# JetPay, IEA). Its Form 25 float is the trust balance, so it is no tier-B name either.
SIC_HISTORY = INPUTS / "sic_history.csv"
SPAC_NAME = re.compile(
    r"\bAcquisitions?\b.*\b(?:Corp|Corporation|Co|Company|Ltd|Limited|Inc|Holdings?|Group)\b|"
    r"\bMerger\s+(?:Corp|Corporation)\b|\bBlank Check\b|\bSPAC\b|"
    r"\b(?:Holdings?|Corp\.?|Corporation|Co\.?|Inc\.?|Capital|Investment|Growth|Partners|Tech|Technology|Opportunity|"
    r"Opportunities|Innovation|Ventures)\s*,?\s+(?:I|II|III|IV|V|VI|VII|VIII|IX|X|[2-9]|One|Two|Three|Four|Five)\b",
    re.IGNORECASE)
UNIT_SUFFIX = re.compile(r"(?:W|WS|U|R)$")


def former_name_spans(text: str) -> list[tuple[str, str, str]]:
    """security_master.former_names 'NAME (a..b) | ...' as [(name, a, b)]."""
    out = []
    for part in str(text or "").split(" | "):
        found = re.match(r"(.*)\s+\((\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})\)$", part.strip())
        if found:
            out.append(found.groups())
    return out


def spac_shells(master: pd.DataFrame, intervals: pd.DataFrame, sic_path: Path = SIC_HISTORY) -> dict[str, str]:
    """security_id -> why it is an unmerged SPAC shell for its whole Nasdaq life.

    Every shell has current SIC 6770 and did not merge while listed: a merger shows as a rename to a
    name that is not SPAC-like together with a new Nasdaq ticker (UBPS -> JetPay's JTPY, TVAC ->
    Inspirato's ISPO); a rename that keeps the ticker is a SPAC renaming itself or a shell handing
    over to a foreign target (Gazelle Opportunities I -> SVF Investment Corp., DD3 Acquisition Corp.
    II -> Codere Online U.S. Corp.). Then either (a) the security is delisted, sic_history shows no
    operating SIC after 6770, and it kept one Nasdaq ticker (units and warrants aside), or (b) the
    master name or a listed name looks like a SPAC (Acquisition Corp, Merger Corp, Holdings IV ...).
    A merger sub's name ("Grizzly Merger Sub 1") never counts alone: the SIC must be 6770 too.
    """
    sic = read_csv_text(sic_path) if Path(sic_path).exists() else pd.DataFrame(
        columns=["cik", "operating_sic_after_6770"])
    operating = set(sic.loc[sic["operating_sic_after_6770"] != "", "cik"])
    listed_names = intervals.groupby("security_id")["name_in_source"].apply(lambda s: " | ".join(sorted(set(s))))
    tickers = intervals.groupby("security_id")["ticker"].apply(
        lambda s: {t[:-1] if len(t) == 5 and UNIT_SUFFIX.search(t) else t for t in s})
    out = {}
    for row in master[master["sic"] == "6770"].itertuples(index=False):
        sid = row.security_id
        last = row.last_listed or "2100-01-01"
        single = len(tickers.get(sid, set())) <= 1
        renamed = [n for n, _, end in former_name_spans(row.former_names) if end < last]
        if renamed and not SPAC_NAME.search(row.name) and not single:
            continue  # merged while listed: an operating company from then on
        named = SPAC_NAME.search(row.name) or SPAC_NAME.search(str(listed_names.get(sid, "")))
        delisted = bool(row.delist_date) or (row.last_listed and row.last_listed < WINDOW_END)
        if delisted and row.cik not in operating and single:
            out[sid] = "sic_6770_never_operating_one_ticker"
        elif named:
            out[sid] = "sic_6770_spac_name"
    return out


# Roman numerals past X (Churchill Capital Corp XI), for the narrower test of a shell listed now.
SPAC_NAME_NOW = re.compile(SPAC_NAME.pattern.replace("(?:I|II|III|IV|V|VI|VII|VIII|IX|X|",
                                                     "(?:I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII|XIV|XV|XX|"),
                           re.IGNORECASE)


def spac_like_now(master: pd.DataFrame, intervals: pd.DataFrame, listed_now, sic_path: Path = SIC_HISTORY) -> set[str]:
    """Securities listed now that are still blank-check shells, which ``spac_shells`` misses because their
    SIC is not 6770 or their name is not SPAC-like (round 5: Dynamix Corp, SIC 6770 with three tickers;
    Iron Horse Acquisition II, Churchill Capital Corp XI and Research Alliance Corp III, SIC 7389 /
    3569 / 2834): SIC 6770 with no operating SIC after it, or a SPAC-like master name whose every listed
    name is SPAC-like too (never renamed to an operating company). Used by rule Y_active_all only; the
    universe base (``spac_shells``) is unchanged."""
    sic = read_csv_text(sic_path) if Path(sic_path).exists() else pd.DataFrame(
        columns=["cik", "operating_sic_after_6770"])
    operating = set(sic.loc[sic["operating_sic_after_6770"] != "", "cik"])
    names = intervals.groupby("security_id")["name_in_source"].apply(list).to_dict()
    now = set(listed_now)
    out = set()
    for row in master[master["security_id"].isin(now)].itertuples(index=False):
        blank = row.sic == "6770" and row.cik not in operating
        named = bool(SPAC_NAME_NOW.search(row.name)) and all(SPAC_NAME_NOW.search(str(n))
                                                              for n in names.get(row.security_id, []))
        if blank or named:
            out.add(row.security_id)
    return out


# ------------------------------------------------------------------ ticker -> security by date

class TickerMap:
    """Which security a ticker-keyed price row belongs to.

    A row dated inside a listing span of the ticker belongs to that span's security
    (``direct``). A row outside every span of the ticker goes to the security of the
    ticker's nearest span when that security was listed then under another ticker (a vendor
    keeps a renamed company's history under its newest ticker: FB, GOOGL); otherwise it is
    dropped (another company, another exchange, or before the listing). Direct rows win over
    such continuity rows when both give the same security-day.
    """

    def __init__(self, spans: pd.DataFrame):
        self.by_ticker: dict[str, list[tuple[np.datetime64, np.datetime64, str]]] = defaultdict(list)
        for row in spans.itertuples(index=False):
            self.by_ticker[row.ticker].append(
                (np.datetime64(row.list_start), np.datetime64(row.list_end), row.security_id))
        for ticker in self.by_ticker:
            self.by_ticker[ticker].sort()
        whole = spans.groupby("security_id").agg(first=("list_start", "min"), last=("list_end", "max"))
        self.security_span = {s: (np.datetime64(a), np.datetime64(b)) for s, a, b in
                              zip(whole.index, whole["first"], whole["last"])}

    def tickers(self) -> set[str]:
        return set(self.by_ticker)

    def assign(self, ticker: str, dates) -> tuple[np.ndarray, np.ndarray]:
        """(security ids, '' where none; direct flags) for ``dates`` of ``ticker``."""
        dates = np.asarray(dates, dtype="datetime64[D]")
        owner = np.full(len(dates), "", dtype=object)
        direct = np.zeros(len(dates), dtype=bool)
        spans = self.by_ticker.get(ticker, [])
        if not spans or not len(dates):
            return owner, direct
        for start, end, security in spans:
            inside = (dates >= start) & (dates <= end) & (owner == "")
            owner[inside] = security
            direct[inside] = True
        rest = np.flatnonzero(owner == "")
        if len(rest):
            starts = np.array([s for s, _, _ in spans], dtype="datetime64[D]")
            ends = np.array([e for _, e, _ in spans], dtype="datetime64[D]")
            days = dates[rest][:, None]
            distance = np.where(days < starts[None, :], starts[None, :] - days, days - ends[None, :]).astype("int64")
            nearest = distance.argmin(axis=1)
            securities = np.array([s for _, _, s in spans], dtype=object)[nearest]
            first = np.array([self.security_span[s][0] for s in securities], dtype="datetime64[D]")
            last = np.array([self.security_span[s][1] for s in securities], dtype="datetime64[D]")
            ok = (dates[rest] >= first) & (dates[rest] <= last)
            owner[rest[ok]] = securities[ok]
        return owner, direct


# ------------------------------------------------------------------ price series

SERIES_COLUMNS = ["ticker", "date", "close", "volume", "src", "file"]


def _frame(ticker: str, dates, close, volume, src: str, file: str) -> pd.DataFrame:
    frame = pd.DataFrame({"date": pd.to_datetime(pd.Series(dates)).dt.normalize().values,
                          "close": pd.to_numeric(pd.Series(close), errors="coerce").values,
                          "volume": pd.to_numeric(pd.Series(volume), errors="coerce").values})
    frame["ticker"], frame["src"], frame["file"] = ticker, src, file
    frame = frame[(frame["date"] >= WINDOW_START) & (frame["date"] <= WINDOW_END)]
    frame = frame[(frame["close"] > 0) & frame["volume"].notna()]
    return frame.drop_duplicates("date", keep="last")[SERIES_COLUMNS]


def read_wiki(tickers: set[str], directory: Path = WIKI_DIR) -> list[pd.DataFrame]:
    """WIKI raw close and volume (as traded) for every WIKI file whose ticker is a Nasdaq ticker."""
    from pipelines.reversal_data.wiki import READ_KW, safe_ticker

    frames = []
    for ticker in sorted(tickers):
        path = directory / f"{safe_ticker(ticker)}.csv.gz"
        if path.exists():
            data = pd.read_csv(path, usecols=["ticker", "date", "close", "volume"], **READ_KW)
            frames.append(_frame(ticker, data["date"], data["close"], data["volume"], "wiki", path.name))
    return frames


def read_tiingo(tickers: set[str], directories: list[Path] = TIINGO_DIRS) -> list[pd.DataFrame]:
    """Tiingo daily JSON caches ({meta, prices}): ``close`` and ``volume`` are as traded.

    A post-delisting OTC ticker (SIVBQ, BBBYQ, SPWRQ) carries the Nasdaq history of the
    ticker without its Q, which is the ticker the listing intervals know.
    """
    frames = []
    for directory in directories:
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            payload = json.loads(path.read_text(encoding="utf-8"))
            ticker = str((payload.get("meta") or {}).get("ticker") or path.stem).upper()
            if ticker not in tickers and ticker.endswith("Q") and ticker[:-1] in tickers:
                ticker = ticker[:-1]
            prices = payload.get("prices") or []
            if not prices:
                continue
            data = pd.DataFrame(prices)
            frames.append(_frame(ticker, data["date"].str[:10], data["close"], data["volume"], "tiingo",
                                 f"{directory.name}/{path.name}"))
    return frames


def yahoo_split_factor(stamps: pd.DatetimeIndex, splits: dict) -> np.ndarray:
    """Product of the split ratios (numerator/denominator) dated after each stamp."""
    factor = np.ones(len(stamps))
    for event in (splits or {}).values():
        day = pd.to_datetime(event["date"], unit="s").normalize()
        ratio = float(event["numerator"]) / float(event["denominator"])
        factor[stamps < day] *= ratio
    return factor


def read_yahoo(directories: list[Path] = YAHOO_DIRS) -> list[pd.DataFrame]:
    """Yahoo v8 charts: close and volume are split-adjusted, so raw close = close x later split
    ratios and raw volume = volume / the same (the documented split restore, holdout data
    report 5.1); dollar volume is unchanged by it."""
    frames = []
    for directory in directories:
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            result = json.loads(path.read_text(encoding="utf-8"))["chart"]["result"][0]
            stamps = pd.to_datetime(result.get("timestamp") or [], unit="s").normalize()
            if not len(stamps):
                continue
            quote = result["indicators"]["quote"][0]
            factor = yahoo_split_factor(stamps, (result.get("events") or {}).get("splits"))
            close = pd.to_numeric(pd.Series(quote["close"]), errors="coerce").values * factor
            volume = pd.to_numeric(pd.Series(quote["volume"]), errors="coerce").values / factor
            ticker = str(result.get("meta", {}).get("symbol") or path.stem).upper()
            frames.append(_frame(ticker, stamps, close, volume, "yahoo", f"{directory.name}/{path.name}"))
    return frames


def read_stored(directory: Path = STORED_DIR) -> list[pd.DataFrame]:
    """The stored repo files: close x volume is the raw dollar volume; the close itself stays an
    estimate (split- and partly dividend-adjusted), see the module notes."""
    frames = []
    for path in sorted(directory.glob("*.csv")):
        ticker = path.stem.upper().split("_")[0]
        data = pd.read_csv(path, usecols=lambda c: c in ("date", "close", "volume"))
        if not {"date", "close", "volume"} <= set(data.columns):
            continue
        frames.append(_frame(ticker, data["date"], data["close"], data["volume"], "stored", path.name))
    return frames


def assign_securities(frames: list[pd.DataFrame], ticker_map: TickerMap) -> pd.DataFrame:
    """Every source row with its security (rows no security held are dropped)."""
    out = []
    for frame in frames:
        if frame.empty:
            continue
        owner, direct = ticker_map.assign(frame["ticker"].iloc[0], frame["date"].values)
        keep = owner != ""
        if keep.any():
            part = frame[keep].copy()
            part["security_id"], part["direct"] = owner[keep], direct[keep]
            out.append(part)
    if not out:
        return pd.DataFrame(columns=SERIES_COLUMNS + ["security_id", "direct"])
    return pd.concat(out, ignore_index=True)


PRICE_FILE_OWNERS = common.RAW / "sec" / "derived" / "price_file_cik_map.csv"


def predecessors(master: pd.DataFrame) -> dict[str, set[str]]:
    """security -> every security linked to it through step 4's successor links (any number of hops)."""
    parent = defaultdict(set)
    for sid, successor in zip(master["security_id"], master["successor_security_id"]):
        if successor:
            parent[successor].add(sid)
    out = {}
    for sid in list(parent):
        seen, todo = set(), [sid]
        while todo:
            for prior in parent.get(todo.pop(), ()):
                if prior not in seen:
                    seen.add(prior)
                    todo.append(prior)
        out[sid] = seen
    return out


def keep_stored_owner(rows: pd.DataFrame, master: pd.DataFrame, ticker_map: TickerMap,
                      owners_path: Path = PRICE_FILE_OWNERS) -> tuple[pd.DataFrame, dict]:
    """A stored file is a vendor's history of its current company under the current ticker (the
    file's CIK from step 4). A row the ticker map gave to another company is moved to the owner's
    security when the owner was listed that day under another ticker (ContextLogic's LOGC file
    over LogicBio's LOGC years; II-VI's history in COHR over old Coherent). When the owner was not
    listed yet, the row is its predecessor's history (a reorganisation: old Marvell, old Sinclair)
    and stays where it is."""
    if not owners_path.exists():
        return rows, {"stored_owner_rule": "price_file_cik_map.csv missing; not applied"}
    owners = read_csv_text(owners_path)
    owner_cik = {f: c for f, c in zip(owners["file"], owners["cik"]) if c}
    cik_of = dict(zip(master["security_id"], master["cik"]))
    securities_of = defaultdict(list)
    for sid, cik in cik_of.items():
        securities_of[cik].append(sid)
    links = predecessors(master)
    rows = rows.reset_index(drop=True)
    files, owner_rows = rows["file"].values, rows["security_id"].values
    stored = np.flatnonzero(rows["src"].eq("stored").values)
    file_cik = pd.Series(files[stored]).map(owner_cik).fillna("").values
    row_cik = pd.Series(owner_rows[stored]).map(cik_of).fillna("").values
    mismatch = stored[(file_cik != "") & (file_cik != row_cik)]
    moved = dropped = 0
    keep = np.ones(len(rows), dtype=bool)
    new_owner = owner_rows.copy()
    dates = rows["date"].values.astype("datetime64[D]")
    for k in mismatch:
        cik, sid = owner_cik[files[k]], owner_rows[k]
        mine = securities_of.get(cik, [])
        if any(sid in links.get(own, ()) for own in mine):
            continue
        day = dates[k]
        target = [own for own in mine if own in ticker_map.security_span
                  and ticker_map.security_span[own][0] <= day <= ticker_map.security_span[own][1]]
        if len(target) == 1:
            new_owner[k] = target[0]
            moved += 1
        elif len(target) > 1:
            keep[k] = False  # a multi-class owner: no way to tell which class the file is
            dropped += 1
    changed = new_owner != owner_rows
    rows = rows.assign(security_id=new_owner, direct=rows["direct"].values & ~changed)[keep].copy()
    return rows.drop_duplicates(["security_id", "date", "src", "file"]), {
        "stored_rows_moved_to_file_owner": int(moved), "stored_rows_dropped_not_owner": int(dropped)}


def best_rows(rows: pd.DataFrame) -> pd.DataFrame:
    """One row per security-day: vendor before stored (WIKI, Tiingo, Yahoo, stored), direct
    before continuity rows. ``n_src`` counts the sources that had the day."""
    rows = rows.assign(rank=rows["src"].map(SOURCE_RANK), indirect=~rows["direct"].astype(bool))
    rows = rows.sort_values(["security_id", "date", "rank", "indirect"], kind="stable")
    counts = rows.groupby(["security_id", "date"])["src"].nunique().rename("n_src")
    best = rows.drop_duplicates(["security_id", "date"], keep="first").set_index(["security_id", "date"])
    best = best.join(counts).reset_index()
    best["dv"] = best["close"] * best["volume"]
    best["vendor"] = best["src"].isin(VENDOR)
    return best.drop(columns=["rank", "indirect"])


# ------------------------------------------------------------------ Wayback company lists

def load_company_lists(ticker_map: TickerMap, index_path: Path = SNAPSHOT_INDEX) -> pd.DataFrame:
    """Company-list rows mapped to securities: snapshot_date, as_of_session, security_id, ticker,
    last_sale, market_cap, as_of_check. Only rows inside a Nasdaq listing span of the ticker
    count (the lists also carry NYSE and OTC names)."""
    index = read_csv_text(index_path)
    index = index[index["source"] == "wayback_companylist"]
    out = []
    for row in index.itertuples(index=False):
        path = MAIN / row.snapshot_file
        if not path.exists():
            continue
        data = pd.read_csv(path, dtype=str, keep_default_na=False)
        symbols = data["Symbol"].str.strip().str.upper()
        day = np.datetime64(row.snapshot_date)
        for symbol, last_sale, cap in zip(symbols, data["LastSale"], data["MarketCap Raw"]):
            owner, direct = ticker_map.assign(symbol, [day])
            if owner[0] and direct[0]:
                out.append((row.snapshot_date, row.as_of_session or row.snapshot_date, owner[0], symbol,
                            pd.to_numeric(str(last_sale).lstrip("$"), errors="coerce"),
                            pd.to_numeric(cap, errors="coerce"), row.as_of_check))
    frame = pd.DataFrame(out, columns=["snapshot_date", "as_of_session", "security_id", "ticker", "last_sale",
                                       "market_cap", "as_of_check"])
    return frame.drop_duplicates(["snapshot_date", "security_id"], keep="first")


def wiki_entity_check(rows: pd.DataFrame, lists: pd.DataFrame) -> pd.DataFrame:
    """Plan 4.4 R9, the level part: per (security, WIKI file), WIKI raw close on a company list's
    as-of session against its LastSale. Within 2% (5% where the as-of session is unverified)
    counts as agreeing; a file with 2+ comparisons of which fewer than half agree fails."""
    wiki = rows[rows["src"] == "wiki"][["security_id", "file", "date", "close"]]
    quotes = lists[lists["last_sale"] > 0].assign(date=lambda f: pd.to_datetime(f["as_of_session"]))
    joined = wiki.merge(quotes[["security_id", "date", "last_sale", "as_of_check"]], on=["security_id", "date"])
    if joined.empty:
        return pd.DataFrame(columns=["security_id", "file", "n_compared", "n_agree", "fails"])
    band = np.where(joined["as_of_check"] == "confirmed", 0.02, 0.05)
    joined["agree"] = (joined["close"] / joined["last_sale"] - 1.0).abs() <= band
    result = joined.groupby(["security_id", "file"]).agg(n_compared=("agree", "size"), n_agree=("agree", "sum"))
    result = result.reset_index()
    result["fails"] = (result["n_compared"] >= 2) & (result["n_agree"] < 0.5 * result["n_compared"])
    return result


def stored_dv_check(rows: pd.DataFrame) -> dict:
    """Stored close x volume against WIKI raw close x volume on the same security-days, by year
    (median and 5th/95th percentiles of the ratio): the basis for using stored dollar volume."""
    wiki = rows[(rows["src"] == "wiki") & rows["direct"]][["security_id", "date", "close", "volume"]]
    stored = rows[(rows["src"] == "stored") & rows["direct"]][["security_id", "date", "close", "volume"]]
    joined = wiki.merge(stored, on=["security_id", "date"], suffixes=("_w", "_s"))
    joined = joined[(joined["volume_w"] > 0) & (joined["volume_s"] > 0)]
    ratio = (joined["close_s"] * joined["volume_s"]) / (joined["close_w"] * joined["volume_w"])
    by_year = ratio.groupby(joined["date"].dt.year)
    return {"security_days": int(len(joined)), "securities": int(joined["security_id"].nunique()),
            "by_year": {int(y): {"median": round(float(r.median()), 4), "p05": round(float(r.quantile(0.05)), 4),
                                 "p95": round(float(r.quantile(0.95)), 4)} for y, r in by_year}}


# ------------------------------------------------------------------ stage 1: daily series

def mapping_spans(spans: pd.DataFrame) -> pd.DataFrame:
    """Spans widened back to the day after ``start_prev_absent`` so rows of an IPO (or a rename)
    between two snapshots are assigned; ``extend_starts`` then keeps only what prices show."""
    widened = spans.copy()
    has_prev = widened["start_prev_absent"] != ""
    widened.loc[has_prev, "list_start"] = [
        (pd.Timestamp(d) + pd.Timedelta(days=1)).strftime("%Y-%m-%d") for d in widened.loc[has_prev, "start_prev_absent"]]
    widened["list_start"] = np.minimum(widened["list_start"], spans["list_start"])
    return widened


def _boundary_session(start_prev_absent: str, sessions: pd.DatetimeIndex) -> np.datetime64:
    """The first session the widened mapping span (``mapping_spans``) lets a ticker's rows start on:
    the first session after ``start_prev_absent`` (step 12's ``next_session``; a series that starts at
    the window start, 2011-06-01, after an earlier ``start_prev_absent`` is not at the boundary)."""
    low = pd.Timestamp(start_prev_absent) + pd.Timedelta(days=1)
    position = sessions.searchsorted(low)
    return np.datetime64(sessions[min(position, len(sessions) - 1)].strftime("%Y-%m-%d"))


def _window_rows(dates: np.ndarray, start_prev_absent: str, before: str) -> np.ndarray:
    low = np.datetime64(start_prev_absent) + np.timedelta64(1, "D")
    return dates[(dates >= low) & (dates < np.datetime64(before))]


STORED_FLOOR_FILES = 25  # as step 12: a first date shared by this many stored files is the repo's coverage start


def stored_first_rows(rows: pd.DataFrame) -> tuple[dict, set]:
    """((security, ticker) -> first day of each stored file the security holds under that ticker, the
    repo's coverage-start dates: first days shared by STORED_FLOOR_FILES or more stored files)."""
    stored = rows[rows["src"] == "stored"]
    if stored.empty:
        return {}, set()
    first = stored.groupby(["security_id", "file"])["date"].min().reset_index()
    first["ticker"] = first["file"].map(lambda f: Path(f).stem.upper().split("_")[0])
    first["day"] = first["date"].dt.strftime("%Y-%m-%d")
    counts = stored.groupby("file")["date"].min().dt.strftime("%Y-%m-%d").value_counts()
    out: dict = defaultdict(list)
    for sid, ticker, day in zip(first["security_id"], first["ticker"], first["day"]):
        out[(sid, ticker)].append(day)
    return dict(out), set(counts.index[counts >= STORED_FLOOR_FILES])


def boundary_trims(spans: pd.DataFrame, best: pd.DataFrame, stored_first: dict, sessions: pd.DatetimeIndex,
                   floor_dates: set = frozenset()) -> dict[tuple[str, str, str], tuple[str, str]]:
    """Step 12's mapping-boundary trim in step 6 (round 6, TrueCar): a security whose first price row is
    after the snapshot that did not show an interval of it, before the one that does, and on the first
    session the widened mapping span allows (a cut series, not an IPO), while its own stored file under
    that ticker starts later, after the snapshot that did not show it, on or before the one that does,
    and more than 5 sessions after that first row. The rows before the stored file are another
    company's under the reused ticker (WIKI TRUE from 2014-03-26; TrueCar's IPO and its stored file
    2014-05-16). Returns (security, ticker, snapshot start) -> (first day dropped, first day kept)."""
    first_row = best.groupby("security_id")["date"].min()
    out = {}
    for row in spans.itertuples(index=False):
        snapshot = str(getattr(row, "snapshot_start", "") or row.list_start)
        first = first_row.get(row.security_id)
        if not row.start_prev_absent or first is None or pd.isna(first):
            continue
        first = first.strftime("%Y-%m-%d")
        if not (row.start_prev_absent < first < snapshot) or (
                np.datetime64(first) != _boundary_session(row.start_prev_absent, sessions)):
            continue
        for day in sorted(stored_first.get((row.security_id, row.ticker), [])):
            if day in floor_dates or not (row.start_prev_absent < day <= snapshot):
                continue
            gap = int(sessions.searchsorted(pd.Timestamp(day))) - int(sessions.searchsorted(pd.Timestamp(first)))
            if gap > CLOSE_STALE_SESSIONS:
                out[(row.security_id, row.ticker, snapshot)] = (first, day)
                break
    return out


def trim_rows(best: pd.DataFrame, trims: dict) -> tuple[pd.DataFrame, int]:
    """``best`` without the rows each boundary trim drops ([first dropped, first kept) of the security)."""
    drop = np.zeros(len(best), dtype=bool)
    for (sid, _, _), (low, keep) in trims.items():
        drop |= (best["security_id"].values == sid) & (best["date"].values >= np.datetime64(low)) \
            & (best["date"].values < np.datetime64(keep))
    return best[~drop].reset_index(drop=True), int(drop.sum())


STEP12_SUMMARY = common.CACHE / "universe" / "universe_summary.json"


def step12_boundaries(path: Path = STEP12_SUMMARY) -> tuple[set, dict] | None:
    """Step 12's published mapping-boundary intervals (``listing.mapping_boundary_starts_to_check``,
    'security:ticker:snapshot start', judged on the canonical panel's first rows after its trims) and its
    trims (security -> first day its rows count); None when step 12 has not been built."""
    if not Path(path).exists():
        return None
    listing = json.loads(Path(path).read_text(encoding="utf-8")).get("listing", {})
    if "mapping_boundary_starts_to_check" not in listing:
        return None
    starts = {tuple(x.split(":", 2)) for x in listing["mapping_boundary_starts_to_check"]}
    trims = {sid: v.get("rows_from", "") for sid, v in (listing.get("mapping_boundary_trims") or {}).items()}
    return starts, trims


def extend_starts(spans: pd.DataFrame, best: pd.DataFrame, sessions: pd.DatetimeIndex | None = None,
                  boundary: set | None = None) -> pd.DataFrame:
    """The IPO rule of plan 3.1: an interval starts at its first price row after the snapshot that did not
    show it, when that row comes before the first snapshot that does; not at step 12's mapping boundary
    (``ipo_boundary``: a series cut on the first session the widened mapping span allows is a transfer
    from another exchange or another company's rows under a reused ticker, not an IPO), where the start
    stays the snapshot's (round 6: TrueCar's start had moved back 7 weeks onto another company's WIKI rows;
    ``boundary_trims`` drops such rows first). ``boundary``: the (security, ticker, snapshot start) keys of
    step 12's published list (``step12_boundaries``); without it, step 12's rule on step 6's rows (the
    security's first row at all on that session). ``ipo_start`` marks the intervals the rule moved."""
    sessions = sessions if sessions is not None else xnas_sessions()
    dates = {s: np.sort(d.values.astype("datetime64[D]")) for s, d in best.groupby("security_id")["date"]}
    starts, ipo, cuts = [], [], []
    for row in spans.itertuples(index=False):
        start, moved, cut = row.list_start, False, False
        mine = dates.get(row.security_id)
        if row.start_prev_absent and mine is not None:
            inside = _window_rows(mine, row.start_prev_absent, row.list_start)
            if len(inside):
                snapshot = str(getattr(row, "snapshot_start", "") or row.list_start)
                if boundary is not None:
                    cut = (row.security_id, row.ticker, snapshot) in boundary
                else:
                    cut = inside[0] == mine[0] and inside[0] == _boundary_session(row.start_prev_absent, sessions)
                if not cut:
                    start, moved = str(inside[0])[:10], True
        starts.append(start), ipo.append(moved), cuts.append(cut)
    return spans.assign(list_start=starts, ipo_start=ipo, ipo_boundary=cuts)


def build_daily(master: pd.DataFrame, intervals: pd.DataFrame) -> dict:
    spans = listing_spans(intervals, master)
    ticker_map = TickerMap(mapping_spans(spans))
    tickers = ticker_map.tickers()
    log(f"spans: {len(spans)} intervals, {spans['security_id'].nunique()} securities, {len(tickers)} tickers")
    frames = read_wiki(tickers)
    log(f"wiki files: {len(frames)}")
    frames += read_tiingo(tickers)
    frames += read_yahoo()
    log(f"vendor files (wiki+tiingo+yahoo): {len(frames)}")
    stored = read_stored()
    log(f"stored files: {len(stored)}")
    rows = assign_securities(frames + stored, ticker_map)
    log(f"assigned rows: {len(rows)} ({rows['src'].value_counts().to_dict()})")
    rows, owner_facts = keep_stored_owner(rows, master, ticker_map)
    log(f"stored owner rule: {owner_facts}")
    lists = load_company_lists(ticker_map)
    log(f"company-list rows mapped: {len(lists)}")
    entity = wiki_entity_check(rows, lists)
    failing = set(zip(entity.loc[entity["fails"], "security_id"], entity.loc[entity["fails"], "file"]))
    dropped = rows["src"].eq("wiki") & pd.Series(list(zip(rows["security_id"], rows["file"]))).isin(failing).values
    rows_kept = rows[~dropped]
    dv_check = stored_dv_check(rows_kept)
    best = best_rows(rows_kept)
    spans, unconfirmed = confirm_after_cut(spans, best)
    named = [":".join((u["security_id"], u["ticker"], u["list_start"])) for u in unconfirmed]
    log(f"later listings after a Form 25 without a symbol file or a price row, cut to one day: {named}")
    sessions = xnas_sessions()
    stored_first, floor_dates = stored_first_rows(rows_kept)
    trims = boundary_trims(spans, best, stored_first, sessions, floor_dates)
    published = step12_boundaries()
    if published is not None:
        # step 12's own trims (judged on the canonical panel) too, from the first row it names
        first_row = best.groupby("security_id")["date"].min()
        for sid, keep_from in published[1].items():
            for row in spans[spans["security_id"] == sid].itertuples(index=False):
                first = first_row.get(sid)
                if keep_from and first is not None and first.strftime("%Y-%m-%d") < keep_from and row.start_prev_absent:
                    trims.setdefault((sid, row.ticker, row.snapshot_start), (first.strftime("%Y-%m-%d"), keep_from))
    best, trimmed = trim_rows(best, trims)
    log(f"mapping-boundary trims: {trims} ({trimmed} rows dropped); step 12's boundary list: "
        f"{'not built' if published is None else len(published[0])}")
    spans = extend_starts(spans, best, sessions, None if published is None else published[0])
    files = (rows.groupby(["security_id", "src", "file"])
             .agg(first=("date", "min"), last=("date", "max"), rows=("date", "size"), direct=("direct", "mean"))
             .reset_index())
    files["first"], files["last"] = files["first"].dt.strftime("%Y-%m-%d"), files["last"].dt.strftime("%Y-%m-%d")
    files["entity_check_failed"] = [(s, f) in failing for s, f in zip(files["security_id"], files["file"])]
    facts = {"intervals": int(len(spans)), "securities_with_rows": int(best["security_id"].nunique()),
             "rows_by_source": {k: int(v) for k, v in rows["src"].value_counts().items()},
             "best_rows_by_source": {k: int(v) for k, v in best["src"].value_counts().items()},
             "wiki_entity_check": {"files_compared": int(len(entity)), "files_failed": int(entity["fails"].sum()),
                                   "rows_dropped": int(dropped.sum()),
                                   "failed": [f"{s}:{f}" for s, f in sorted(failing)]},
             "stored_dv_check": dv_check, **owner_facts,
             "after_cut_unconfirmed": unconfirmed,
             "mapping_boundary": {"rule": "step 12's published list" if published is not None else "step 12's rule on step 6 rows",
                                  "intervals_kept_at_snapshot_start": int(spans["ipo_boundary"].sum()),
                                  "securities": int(spans.loc[spans["ipo_boundary"], "security_id"].nunique()),
                                  "ipo_rule_starts": int(spans["ipo_start"].sum()),
                                  "trims": {f"{s}:{t}:{d}": {"rows_from": a, "rows_kept_from": b}
                                            for (s, t, d), (a, b) in sorted(trims.items())},
                                  "rows_trimmed": trimmed,
                                  "boundary_starts": [f"{r.security_id}:{r.ticker}:{r.list_start}" for r in
                                                      spans[spans["ipo_boundary"]].itertuples(index=False)]}}
    return {"spans": spans, "best": best, "lists": lists, "files": files, "entity": entity, "facts": facts}


def save_daily(stage: dict, out: Path = OUT) -> None:
    out.mkdir(parents=True, exist_ok=True)
    stage["best"].to_pickle(out / "daily_series.pkl")
    for name in ("spans", "lists", "files", "entity"):
        common.atomic_write(out / f"{name}.csv.gz", gzip.compress(stage[name].to_csv(index=False).encode(), mtime=0))
    common.atomic_write(out / "daily_facts.json", (json.dumps(stage["facts"], indent=1, default=str) + "\n").encode())


def load_daily(out: Path = OUT) -> dict:
    read = lambda name: pd.read_csv(out / f"{name}.csv.gz", dtype=str, keep_default_na=False)
    stage = {name: read(name) for name in ("spans", "lists", "files", "entity")}
    stage["lists"]["last_sale"] = pd.to_numeric(stage["lists"]["last_sale"], errors="coerce")
    stage["lists"]["market_cap"] = pd.to_numeric(stage["lists"]["market_cap"], errors="coerce")
    stage["best"] = pd.read_pickle(out / "daily_series.pkl")
    stage["facts"] = json.loads((out / "daily_facts.json").read_text())
    return stage


# ------------------------------------------------------------------ stage 2: weekly metrics

SRC_CODES = {"wiki": 0, "tiingo": 1, "yahoo": 2, "stored": 3}
SRC_NAMES = {v: k for k, v in SRC_CODES.items()}


def listed_weeks(spans: pd.DataFrame, master: pd.DataFrame, foreign: dict, weeks: pd.DatetimeIndex,
                 shells: set | dict = frozenset(), investment: dict | None = None) -> pd.DataFrame:
    """One row per (security, week end) it is listed on: the ticker of the covering interval,
    whether that interval's name is non-common (or the security is an unmerged SPAC shell), whether
    the security is a foreign filer then, and whether its issuer is an investment company then (D6)."""
    values = weeks.values.astype("datetime64[D]")
    rows = []
    for row in spans.sort_values("list_start").itertuples(index=False):
        low = np.searchsorted(values, np.datetime64(row.list_start), "left")
        high = np.searchsorted(values, np.datetime64(row.list_end), "right")
        if high > low:
            bad = row.security_id in shells or non_common_interval(row.name, row.share_class)
            for k in range(low, high):
                rows.append((row.security_id, k, row.ticker, bad))
    frame = pd.DataFrame(rows, columns=["security_id", "week_index", "ticker", "non_common"])
    frame = frame.drop_duplicates(["security_id", "week_index"], keep="last")
    frame["week_end"] = weeks[frame["week_index"].values]
    days = frame["week_end"].dt.strftime("%Y-%m-%d")
    frame["foreign"] = [is_foreign_on(foreign.get(s), d) for s, d in zip(frame["security_id"], days)]
    investment = investment or {}
    frame["investment_company"] = [is_foreign_on(investment.get(s), d) for s, d in zip(frame["security_id"], days)]
    return frame.reset_index(drop=True)


def wide_panels(best: pd.DataFrame, sessions: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    """Session x security panels: dv, close, source code, vendor flag (NaN where no row)."""
    best = best[best["date"].isin(sessions)]
    pivot = lambda column: best.pivot(index="date", columns="security_id", values=column).reindex(sessions)
    panels = {"dv": pivot("dv"), "close": pivot("close")}
    panels["src"] = best.assign(code=best["src"].map(SRC_CODES).astype(float)).pivot(
        index="date", columns="security_id", values="code").reindex(sessions)[panels["dv"].columns]
    panels["close"] = panels["close"][panels["dv"].columns]
    return panels


def series_metrics(panels: dict[str, pd.DataFrame], weeks: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    """Week-end x security panels: dv20/dv50 medians (minimum observations in DV_WINDOWS), the last
    close within CLOSE_STALE_SESSIONS and its source, the vendor-only close, and vendor rows in 50."""
    out = {}
    for window, minimum in DV_WINDOWS.items():
        out[f"dv{window}"] = panels["dv"].rolling(window, min_periods=minimum).median().reindex(weeks)
        log(f"dv{window} medians done")
    out["close"] = panels["close"].ffill(limit=CLOSE_STALE_SESSIONS).reindex(weeks)
    out["src"] = panels["src"].ffill(limit=CLOSE_STALE_SESSIONS).reindex(weeks)
    vendor = panels["src"].lt(SRC_CODES["stored"])
    out["vendor_close"] = panels["close"].where(vendor).ffill(limit=CLOSE_STALE_SESSIONS).reindex(weeks)
    out["vendor_n50"] = vendor.astype(float).rolling(50, min_periods=1).sum().reindex(weeks)
    out["n50"] = panels["dv"].notna().astype(float).rolling(50, min_periods=1).sum().reindex(weeks)
    return out


def take(panel: pd.DataFrame, weeks_idx: np.ndarray, securities: pd.Series) -> np.ndarray:
    """panel values at (week position, security) pairs; NaN where the security has no column."""
    columns = {s: k for k, s in enumerate(panel.columns)}
    positions = securities.map(columns)
    values = np.full(len(securities), np.nan)
    ok = positions.notna().values
    values[ok] = panel.values[weeks_idx[ok], positions[ok].astype(int).values]
    return values


def price_flag(close: np.ndarray, src: np.ndarray) -> np.ndarray:
    """Y/N from a vendor raw close; from a stored close (an estimate that later forward splits
    and dividends only push down) Y at $10 or more, else U (unknown); '' with no close."""
    flag = np.full(len(close), "", dtype=object)
    has = ~np.isnan(close)
    vendor = has & (src < SRC_CODES["stored"])
    stored = has & (src == SRC_CODES["stored"])
    flag[vendor] = np.where(close[vendor] >= MIN_PRICE, "Y", "N")
    flag[stored] = np.where(close[stored] >= MIN_PRICE, "Y", "U")
    return flag


def rank_within_weeks(frame: pd.DataFrame, value: str, eligible: pd.Series) -> pd.Series:
    """Descending rank of ``value`` within each week among ``eligible`` rows (NaN elsewhere)."""
    ranks = frame.loc[eligible].groupby("week_end")[value].rank(ascending=False, method="first")
    return ranks.reindex(frame.index)


def weekly_table(stage: dict, master: pd.DataFrame, foreign: dict, sessions: pd.DatetimeIndex,
                 weeks: pd.DatetimeIndex, shells: set | dict = frozenset(), investment: dict | None = None) -> pd.DataFrame:
    weekly = listed_weeks(stage["spans"], master, foreign, weeks, shells, investment)
    log(f"listed security-weeks: {len(weekly)}")
    panels = wide_panels(stage["best"], sessions)
    metrics = series_metrics(panels, weeks)
    idx, securities = weekly["week_index"].values, weekly["security_id"]
    for name, panel in metrics.items():
        weekly[name] = take(panel, idx, securities)
    weekly["src"] = [SRC_NAMES.get(int(c), "") if not np.isnan(c) else "" for c in weekly["src"].values]
    codes = weekly["src"].map(SRC_CODES).fillna(-1).values
    weekly["price_ge_10"] = price_flag(weekly["close"].values, codes)
    # Covered by a vendor raw series: a vendor close this week, and vendor rows make up at least
    # 80% of the rows in the 50-session window (so a new listing's first weeks count too).
    weekly["vendor_ok"] = weekly["vendor_close"].notna() & (weekly["vendor_n50"] >= 0.8 * weekly["n50"])
    weekly["universe"] = ~weekly["foreign"] & ~weekly["non_common"] & ~weekly["investment_company"]
    priced = weekly["universe"] & weekly["price_ge_10"].isin(["Y", "U"])
    weekly["dv50_rank"] = rank_within_weeks(weekly, "dv50", priced & weekly["dv50"].notna())
    weekly["dv20_rank"] = rank_within_weeks(weekly, "dv20", priced & weekly["dv20"].notna())
    weekly["dv50_rank_vendor_only"] = rank_within_weeks(weekly, "dv50", priced & weekly["dv50"].notna()
                                                        & weekly["vendor_ok"])
    return weekly.drop(columns=["week_index"])


TRADING_BOUND_DAYS = 30


def mark_trading_bounds(weekly: pd.DataFrame, spans: pd.DataFrame, best: pd.DataFrame) -> pd.DataFrame:
    """``outside_trading``: a listed week after the last row of every source when the listing ends
    within 30 days of that row (between the last trade and the Form 25 taking effect), or before
    the first row when trading starts within 30 days of the first listing. Such weeks have no
    prices to fetch, so they never count as uncovered."""
    first_row = best.groupby("security_id")["date"].min()
    last_row = best.groupby("security_id")["date"].max()
    listed = spans.groupby("security_id").agg(first=("list_start", "min"), last=("list_end", "max"))
    first_listed = pd.to_datetime(weekly["security_id"].map(listed["first"]))
    last_listed = pd.to_datetime(weekly["security_id"].map(listed["last"]))
    first, last = weekly["security_id"].map(first_row), weekly["security_id"].map(last_row)
    ended = (weekly["week_end"] > last) & ((last_listed - last).dt.days <= TRADING_BOUND_DAYS)
    unstarted = (weekly["week_end"] < first) & ((first - first_listed).dt.days <= TRADING_BOUND_DAYS)
    return weekly.assign(outside_trading=(ended | unstarted).fillna(False).astype(bool))


# ------------------------------------------------------------------ Tiingo supported tickers

TIINGO_START_SLACK_DAYS = 7
# A delisted name stops trading up to about ten sessions before its Form 25 takes effect.
TIINGO_END_SLACK_DAYS = 21


def supported_tickers_path(day: str | None = None) -> Path:
    existing = sorted(OUT.glob("supported_tickers_*.zip"))
    if existing and day is None:
        return existing[-1]
    return OUT / f"supported_tickers_{day or datetime.now(timezone.utc).strftime('%Y-%m-%d')}.zip"


def load_supported_tickers(offline: bool = False) -> pd.DataFrame:
    """Tiingo's public ticker list (no key; fetched once through ``cached_get``, then from cache)."""
    path = supported_tickers_path()
    if not path.exists():
        if offline:
            raise FileNotFoundError(f"{path} is not cached and --offline was given")
        common.cached_get(TIINGO_SUPPORTED_URL, path, source="tiingo_supported_tickers",
                          headers={"User-Agent": "quant_stocks research"}, symbol="supported_tickers")
    with zipfile.ZipFile(path) as archive:
        name = [n for n in archive.namelist() if n.endswith(".csv")][0]
        frame = pd.read_csv(archive.open(name), dtype=str, keep_default_na=False)
    frame["ticker"] = frame["ticker"].str.upper().str.strip()
    return frame


def tiingo_index(supported: pd.DataFrame) -> dict[str, list[dict]]:
    """ticker -> every US-dollar row of Tiingo's list, any asset type (Tiingo labels some stocks ETF:
    INFO 2014-06-19..2022-02-28 is IHS Markit, PAND is Pandion), each marked ``served`` when the API
    answers the ticker with that row (``served_row``)."""
    usd = supported[supported["priceCurrency"].isin(["USD", ""])]
    index = defaultdict(list)
    for row in usd.itertuples(index=False):
        index[row.ticker].append({"ticker": row.ticker, "exchange": row.exchange, "asset_type": row.assetType,
                                  "start": row.startDate[:10], "end": row.endDate[:10], "served": False})
    for rows in index.values():
        top = served_row(rows)
        if top is not None:
            top["served"] = True
    return index


def served_row(rows: list[dict]) -> dict | None:
    """The row the API answers a ticker with: the one that ends latest (then starts latest). The
    trial of 2026-10-01 showed it for all three tickers with two rows: CA came back as the 2023 ETF,
    CZR as Eldorado's row (2014-09-22..) though old Caesars' row overlaps it, GPOR as the 2021
    Gulfport although the old row ends the day before. Every other row of the ticker is hidden."""
    dated = [r for r in rows if r["start"] and r["end"]]
    return max(dated, key=lambda r: (r["end"], r["start"])) if dated else None


def _days(a: str, b: str) -> int:
    return (pd.Timestamp(b) - pd.Timestamp(a)).days


# Candidate-ticker kinds, in order of preference when two rows match equally well.
TICKER_KINDS = ["own", "reviewed_alias", "q_suffix", "sec_current", "q_prefix"]
# A row that starts this long after the security began using the ticker (or, for a ticker it never
# listed under, after its first listing) is late: a truncated history or another company. When such
# a row covers the need only through the start slack, it is another company that began then (ACET:
# resTORbio/Adicet's row starts 2018-01-26; Aceto used ACET for decades and needs 2018-01-21 on).
LATE_ROW_DAYS = 30
TIINGO_CUT = "2016-01-04"  # Tiingo starts some histories here (plan 8.9): a truncation, not a newer company
ORIGIN_DAYS = 60  # a pattern or non-stock row must start this close to a post-2011 listing
PRE_WINDOW_LISTING = "2011-01-31"  # first snapshot floor: listed before the window, origin unknown


def row_origin_ok(row: dict, security: dict | None) -> bool:
    """A row of a pattern alias or a non-stock row starts near the security's own beginning."""
    if not security or not security.get("first_listed"):
        return False
    first = str(security["first_listed"])[:10]
    return first <= PRE_WINDOW_LISTING or abs(_days(first, row["start"])) <= ORIGIN_DAYS


def row_end_ok(row: dict, security: dict | None) -> bool:
    """A non-stock row ends when the security's listing ends (or later after an exchange move; an
    active security's row runs to the list date)."""
    if not security:
        return False
    if security.get("active"):
        return row["end"] >= _shift(WINDOW_END, -30)
    last = str(security.get("last_listed") or "")[:10]
    if not last:
        return False
    if security.get("transfer_date"):
        return row["end"] >= _shift(last, -30)
    return abs(_days(last, row["end"])) <= 30


ENDS_WITH_DAYS = 30  # a row ends with the security when it ends this close to its last listed day
OUTLIVES_DAYS = 60  # a served row that runs this long past it belongs to a company still trading


def tiingo_range_match(tickers: list, need_start: str, need_end: str, index: dict[str, list[dict]],
                       security: dict | None = None) -> dict:
    """Plan 3.2: a single supported_tickers row whose start and end cover the needed interval, and
    which the API serves for its ticker.

    ``tickers``: candidate tickers in order, each a ticker or (ticker, kind) with kind one of
    TICKER_KINDS. ``security``: first_listed, last_listed, active, transfer_date, ticker_starts.
    Result ``match``: Y (a served row covers it), partial (the best served row covers part of it),
    hidden (a row covers it but the API serves another row of that ticker), newer_company (the row
    is another company: see below), wrong_entity (the ticker has rows, none overlapping the need),
    no_row. Non-stock rows count only when they start and end with the security (INFO, PAND); a
    q_prefix row (SDC -> SDCCQ) only when it starts with it.

    A row is another company (newer_company) when it starts more than LATE_ROW_DAYS after the
    security began using the ticker and inside the need, covering it only through the start slack
    (ACET) or less than half of it; or when it is the served row and a hidden row of the same ticker
    covering the need is the security's own (VIVO: Meridian's row starts in 1992 and the served one
    in 2016, after Meridian had used VIVO for decades; COHR: old Coherent's row ends at its 2022 close,
    the served one, II-VI's, runs on).
    """
    best = {"match": "no_row", "ticker": "", "row_start": "", "row_end": "", "coverage": 0.0, "reused": False,
            "exchange": "", "asset_type": "", "kind": "", "hidden_by": None, "starts_late": False}
    order = {"Y": 6, "partial": 5, "hidden": 4, "newer_company": 3, "wrong_entity": 2, "no_row": 0}
    need_days = max(_days(need_start, need_end), 1)
    security = security or {}
    first_listed = str(security.get("first_listed") or "")[:10]
    last_listed = str(security.get("last_listed") or "")[:10]
    delisted = bool(last_listed) and not security.get("active") and not security.get("transfer_date")
    held_from = security.get("ticker_starts") or {}
    best_key = (-1,)
    for item in tickers:
        ticker, kind = (item, "own") if isinstance(item, str) else item
        if not best["ticker"]:
            best["ticker"] = ticker
        rows = [r for r in index.get(ticker, []) if r["start"] and r["end"]]
        if not rows:
            continue
        served = served_row(rows)
        others = [r for r in rows if r is not served and r["end"] >= need_start]
        base = ticker[:-1] if kind == "q_suffix" else ticker
        reference = "" if kind == "reviewed_alias" else str(held_from.get(base) or first_listed)[:10]
        found = []
        for r in rows:
            stock = (r.get("asset_type") or "Stock").lower() == "stock"
            if not stock and not (row_origin_ok(r, security) and row_end_ok(r, security)):
                continue
            if kind == "q_prefix" and not row_origin_ok(r, security):
                continue
            covers = (_days(r["start"], need_start) >= -TIINGO_START_SLACK_DAYS
                      and _days(need_end, r["end"]) >= -TIINGO_END_SLACK_DAYS)
            overlap = max(0, _days(max(r["start"], need_start), min(r["end"], need_end)))
            coverage = 1.0 if covers else min(1.0, overlap / need_days)
            match = "Y" if covers else ("partial" if overlap > 0 else "wrong_entity")
            late = bool(reference) and _days(reference, r["start"]) > LATE_ROW_DAYS and r["start"] != TIINGO_CUT
            inside = _days(need_start, r["start"]) >= -TIINGO_START_SLACK_DAYS
            if late and inside and (match == "Y" or (match == "partial" and coverage < MIN_PARTIAL_COVERAGE)):
                match = "newer_company"
            if match in ("Y", "partial") and r is not served:
                match = "hidden"
            found.append({"match": match, "ticker": ticker, "row_start": r["start"], "row_end": r["end"],
                          "coverage": round(coverage, 4), "reused": bool(others) or r is not served,
                          "exchange": r["exchange"], "asset_type": r.get("asset_type", ""), "kind": kind,
                          "hidden_by": served if r is not served else None, "starts_late": late,
                          "outlives": delisted and _days(last_listed, r["end"]) > OUTLIVES_DAYS})
        on_served = [c for c in found if c["hidden_by"] is None and c["match"] in ("Y", "partial")]
        own_hidden = [c for c in found if c["match"] == "hidden" and (
            (not c["starts_late"] and any(s["starts_late"] for s in on_served))
            or (delisted and abs(_days(last_listed, c["row_end"])) <= ENDS_WITH_DAYS
                and any(s["outlives"] for s in on_served)))]
        if own_hidden:
            for c in on_served:
                c["match"] = "newer_company"
        for c in found:
            # A partial row worth no symbol ranks below a hidden or newer-company row, which explains more.
            rank = order[c["match"]] if c["match"] != "partial" or useful_partial(c, need_start) else 2.5
            key = (rank, c["coverage"], -TICKER_KINDS.index(kind) if kind in TICKER_KINDS else -9,
                   c["exchange"] == "NASDAQ")
            if key > best_key:
                best, best_key = c, key
    return best


# ------------------------------------------------------------------ stage 3: proxies

MAX_PROXY_FLOAT = 3e12  # larger XBRL floats are unit errors (SeqLL $4.8T)
PROXY_CARRY_DAYS = 365  # plan 3.3: proxies are carried forward up to 12 months


def interval_flags(spans: pd.DataFrame) -> dict[tuple[str, str], bool]:
    """(security, ticker) -> whether that listing's name is non-common."""
    return {(s, t): non_common_interval(n, c) for s, t, n, c in
            zip(spans["security_id"], spans["ticker"], spans["name"], spans["share_class"])}


def mcap_ranks(lists: pd.DataFrame, spans: pd.DataFrame, foreign: dict, shells: set | dict = frozenset(),
               investment: dict | None = None) -> pd.DataFrame:
    """Company-list market-cap rank per capture date among universe names (non-foreign then, common,
    not a SPAC shell, not an investment company then: D6)."""
    flags = interval_flags(spans)
    investment = investment or {}
    frame = lists[lists["market_cap"] > 0].copy()
    frame["universe"] = [s not in shells and not flags.get((s, t), False) and not is_foreign_on(foreign.get(s), d)
                         and not is_foreign_on(investment.get(s), d)
                         for s, t, d in zip(frame["security_id"], frame["ticker"], frame["snapshot_date"])]
    frame = frame[frame["universe"]]
    frame["mcap_rank"] = frame.groupby("snapshot_date")["market_cap"].rank(ascending=False, method="first")
    return frame


# A float of $20B or more is kept only when its shares count gives at most $3,000 a share
# (x1000 unit errors: Codiak $100.8B and $412.7B, Sientra $108.8B-$442.7B).
BIG_FLOAT, MAX_FLOAT_PER_SHARE = 2e10, 3000.0
FLOAT_JUMP_RATIO = 50
SHARES_MATCH_DAYS = 180


FLOAT_COLUMNS = ["cik", "end", "val", "shares", "val_reported", "unit_fix"]
# An x1000 unit error (a filer tagging thousands as dollars: Codiak $412.7B, Vericity $18.4B, Vintage
# Wine Estates $429.6B) is corrected, not dropped, when the corrected float looks like a float: per share
# outstanding an ordinary price, within 5x of the CIK's other kept facts when it has some, and at most
# 1.5x shares x the security's close near the fact when there is one. Otherwise the fact is left out, and
# the reason is recorded (float_unit_fixes.csv).
UNIT_FIX, UNIT_FIX_PER_SHARE, UNIT_FIX_OTHERS, UNIT_FIX_OF_MCAP = 1000.0, (0.10, 300.0), 5.0, 1.5


def unit_fix(val: float, shares, others=(), close=None) -> tuple[bool, str]:
    """(apply, check or reason) for the x1000 correction of a float fact (see UNIT_FIX)."""
    fixed = val / UNIT_FIX
    if pd.isna(shares) or not shares > 0:
        return False, "no shares count within 180 days: the x1000 correction cannot be checked"
    per_share = fixed / shares
    if not UNIT_FIX_PER_SHARE[0] <= per_share <= UNIT_FIX_PER_SHARE[1]:
        return False, f"x1000 gives ${per_share:,.2f} a share outstanding: not an ordinary price"
    others = np.asarray([o for o in others if o > 0], dtype=float)
    checks = ["per_share"]
    if len(others):
        ratio = fixed / float(np.median(others))
        if not 1 / UNIT_FIX_OTHERS <= ratio <= UNIT_FIX_OTHERS:
            return False, (f"x1000 gives ${fixed / 1e6:,.1f}M, {ratio:.2f}x the median of the CIK's other kept facts "
                           f"(${np.median(others) / 1e6:,.1f}M): not an obvious x1000 error")
        checks.append("other_facts")
    if close is not None and close > 0:
        ratio = fixed / (shares * close)
        if ratio > UNIT_FIX_OF_MCAP:
            return False, f"x1000 is still {ratio:.1f}x shares x close: not an x1000 error"
        checks.append("price")
    return True, "+".join(checks)


def float_facts(offline: bool = True) -> tuple[pd.DataFrame, list[dict]]:
    """XBRL dei:EntityPublicFloat from the cached frames (step 3), plausible values only: positive,
    at most $3T, and a float of $20B or more only with a shares count (nearest
    EntityCommonStockSharesOutstanding within 180 days) giving <= $3,000 a share. A fact failing that
    or the 50x rule is corrected by x1000 when ``unit_fix`` accepts it (checked against the per-share
    level and the CIK's other facts; ``float_price_check`` adds the price). Returns (facts with
    FLOAT_COLUMNS, the decisions on the unit errors)."""
    from pipelines.reversal_data.form25 import fetch_float_frames, frame_periods

    floats = fetch_float_frames("EntityPublicFloat", "USD", frame_periods(), offline=offline)
    floats = floats[(floats["val"] > 0) & (floats["val"] <= MAX_PROXY_FLOAT) & floats["end"].notna()]
    floats = floats.sort_values(["cik", "end"]).drop_duplicates(["cik", "end"], keep="last")[["cik", "end", "val"]]
    shares = fetch_float_frames("EntityCommonStockSharesOutstanding", "shares", frame_periods(), offline=offline)
    shares = shares[(shares["val"] > 0) & shares["end"].notna()].rename(columns={"val": "shares"})
    return screen_floats(floats, shares)


def screen_floats(floats: pd.DataFrame, shares: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """``float_facts`` on given float (cik, end, val) and shares (cik, end, shares) facts."""
    floats = floats.astype({"end": "datetime64[ns]"}).sort_values("end")
    shares = shares[["cik", "end", "shares"]].astype({"end": "datetime64[ns]"}).sort_values("end")
    joined = pd.merge_asof(floats, shares, on="end", by="cik", direction="nearest",
                           tolerance=pd.Timedelta(days=SHARES_MATCH_DAYS))
    big = joined["val"] >= BIG_FLOAT
    bad = big & (joined["shares"].isna() | (joined["val"] / joined["shares"] > MAX_FLOAT_PER_SHARE))
    why = pd.Series(np.where(bad, "per_share_over_3000", ""), index=joined.index, dtype=object)
    # A fact of $1B or more that is 50 times the lower median of the CIK's other facts is a unit
    # error too (Astrotech $55B among $10M-$50M floats, Vintage Wine Estates $35B).
    def others_lower_median(values: pd.Series) -> pd.Series:
        array = values.to_numpy()
        out = np.full(len(array), np.nan)
        for k in range(len(array)):
            rest = np.sort(np.delete(array, k))
            if len(rest):
                out[k] = rest[(len(rest) - 1) // 2]
        return pd.Series(out, index=values.index)

    reference = joined.groupby("cik", group_keys=False)["val"].apply(others_lower_median)
    jump = (joined["val"] >= 1e9) & (joined["val"] > FLOAT_JUMP_RATIO * reference.reindex(joined.index))
    why[jump & ~bad] = "50x_other_facts"
    bad |= jump
    joined["val_reported"], joined["unit_fix"] = joined["val"], ""
    kept_by_cik = {c: g["val"].to_numpy() for c, g in joined[~bad].groupby("cik")}
    decisions, keep = [], ~bad
    for k in joined.index[bad]:
        fact = joined.loc[k]
        ok, check = unit_fix(fact["val"], fact["shares"], kept_by_cik.get(fact["cik"], ()))
        decisions.append({"cik": int(fact["cik"]), "end": str(fact["end"])[:10], "float_reported": float(fact["val"]),
                          "shares": fact["shares"], "flagged_by": why[k], "stage": "float_facts",
                          "action": "fixed_x1000" if ok else "dropped",
                          "float_used": float(fact["val"] / UNIT_FIX) if ok else np.nan, "check_or_reason": check})
        if ok:
            joined.loc[k, ["val", "unit_fix"]] = [fact["val"] / UNIT_FIX, check]
            keep[k] = True
    out = joined[keep].sort_values(["cik", "end"])[FLOAT_COLUMNS].reset_index(drop=True)
    return out, decisions


# A float of $10B or more must also fit the price: at most 10x shares outstanding x the security's
# median vendor raw close within 90 days of the fact (10x leaves room for one class's shares count
# only). Without a vendor close, a stored close is split-adjusted (NVIDIA's 2021 close is a 40th of
# the raw one), so it must be 50x off: unit errors are 100-1000x (Mister Car Wash $604B, DIRTT $17.1B,
# Vericity $18.4B, Great Elm $41B passed the per-share test).
PRICE_CHECK_FLOAT, PRICE_CHECK_DAYS = 1e10, 90
PRICE_CHECK_RATIO, PRICE_CHECK_RATIO_STORED = 10.0, 50.0
NO_CLOSE_MAX_PER_SHARE = 1000.0  # with no close near the fact: Mister Car Wash's $880B is $2,894 a share
# Round 9: with no close near it, a fact of $10B or more that is DOUBTFUL_FLOAT_RATIO (20) times the largest checked
# Form 25 float of the CIK's Nasdaq securities (``float_check_flag`` ok) is left out too, unless x1000 fixes it:
# DIRTT's $17.07B of 2023-06-30 is 47x its $364.7M Form 25 float and under the 50x rule (round 8 kept it in
# weekly_metrics.pkl, which step 12's proxies and validate read; the month-2 plan alone left it out).
NO_CLOSE_FORM25_RATIO = 20.0


def form25_checked_floats(master: pd.DataFrame, form25: pd.DataFrame | None) -> dict[int, tuple[float, str]]:
    """CIK -> (the largest checked public float of its securities' Form 25s, that Form 25's accession)."""
    if form25 is None or not len(form25) or "delist_form25_accession" not in master or "accession" not in form25:
        return {}
    flag = form25["float_check_flag"] if "float_check_flag" in form25 else pd.Series("ok", index=form25.index)
    checked = form25[flag.astype(str) == "ok"]
    value = dict(zip(checked["accession"], pd.to_numeric(checked["public_float_usd"], errors="coerce")))
    out: dict[int, tuple[float, str]] = {}
    for cik, accession in zip(master["cik"], master["delist_form25_accession"].fillna("")):
        v = value.get(accession)
        if v is not None and v > 0 and (int(cik) not in out or v > out[int(cik)][0]):
            out[int(cik)] = (float(v), accession)
    return out


def float_price_check(floats: pd.DataFrame, weekly: pd.DataFrame, master: pd.DataFrame,
                      fixed: list | None = None, form25: pd.DataFrame | None = None) -> tuple[pd.DataFrame, list]:
    """``floats`` without the facts of $10B or more that exceed the ratio above (closes from the weekly
    table), or, with no close near the fact, give more than $1,000 a share or are NO_CLOSE_FORM25_RATIO times
    the CIK's checked Form 25 float (``form25``: the step-3 table, read from INPUTS when not given, so step 12's
    call gets the same rule); a fact with no shares count is kept. A failing fact is corrected by x1000 instead
    when ``unit_fix`` accepts it (with the close, and the CIK's other facts). Returns (floats, the dropped facts'
    records); the corrected ones' records (``action`` fixed_x1000) are appended to ``fixed`` when a list is given."""
    floats = floats.copy()
    for column, default in (("val_reported", None), ("unit_fix", "")):
        if column not in floats:
            floats[column] = floats["val"] if default is None else default
    if form25 is None and "delist_form25_accession" in master and FORM25.exists():
        form25 = read_csv_text(FORM25)
    checked = form25_checked_floats(master, form25)
    cik_of = dict(zip(master["security_id"], master["cik"].astype(int)))
    # Only CIKs with a Nasdaq security matter (AutoZone's or NVR's real $1,000+ shares are NYSE's).
    big = floats[(floats["val"] >= PRICE_CHECK_FLOAT) & floats["cik"].astype(int).isin(set(cik_of.values()))]
    if big.empty:
        return floats, []
    columns = ["security_id", "week_end", "close"] + (["vendor_close"] if "vendor_close" in weekly else [])
    closes = weekly[columns].dropna(subset=["close"])
    closes = closes.assign(cik=closes["security_id"].map(cik_of))
    by_cik = {c: g for c, g in closes.groupby("cik")}
    small = floats[floats["val"] < PRICE_CHECK_FLOAT]
    others = {c: g["val"].to_numpy() for c, g in small.groupby(small["cik"].astype(int))}
    dropped, facts = [], []
    for k, fact in big.iterrows():
        if pd.isna(fact.get("shares")) or not fact["shares"] > 0:
            continue
        own = by_cik.get(int(fact["cik"]))
        near = (own[(own["week_end"] - pd.Timestamp(fact["end"])).abs() <= pd.Timedelta(days=PRICE_CHECK_DAYS)]
                if own is not None else closes.iloc[0:0])
        vendor = near["vendor_close"].dropna() if "vendor_close" in near else pd.Series(dtype=float)
        close, limit = ((float(vendor.median()), PRICE_CHECK_RATIO) if len(vendor)
                        else (float(near["close"].median()) if len(near) else np.nan, PRICE_CHECK_RATIO_STORED))
        record = {"cik": int(fact["cik"]), "end": str(fact["end"])[:10], "float_usd": float(fact["val"])}
        if not close > 0:
            form25_float, accession = checked.get(int(fact["cik"]), (np.nan, ""))
            dwarfs = form25_float > 0 and fact["val"] >= NO_CLOSE_FORM25_RATIO * form25_float
            if fact["val"] / fact["shares"] <= NO_CLOSE_MAX_PER_SHARE and not dwarfs:
                continue
            record.update(per_share=round(float(fact["val"] / fact["shares"]), 1), close="none")
            if dwarfs:
                record.update(form25_ratio=round(float(fact["val"] / form25_float), 1), form25_float=form25_float,
                              form25_accession=accession)
        else:
            ratio = fact["val"] / (fact["shares"] * close)
            if ratio <= limit:
                continue
            record.update(ratio=round(float(ratio), 1), close="vendor" if len(vendor) else "stored")
        # A stored close is split-adjusted (a later forward split pushes it down): it can only cap the fix.
        ok, check = unit_fix(fact["val"], fact["shares"], others.get(int(fact["cik"]), ()),
                             close if close > 0 and len(vendor) else None)
        if "form25_ratio" in record and not ok:
            check = (f"no close within {PRICE_CHECK_DAYS} days to test it, and {record['form25_ratio']}x the checked "
                     f"Form 25 float (${record['form25_float'] / 1e6:,.1f}M, {record['form25_accession']}); {check}")
        record.update(action="fixed_x1000" if ok else "dropped", check_or_reason=check)
        if ok:
            floats.loc[k, ["val", "unit_fix"]] = [fact["val"] / UNIT_FIX, check]
            record["float_used"] = float(fact["val"] / UNIT_FIX)
            if fixed is not None:
                fixed.append(record)
        else:
            dropped.append(k)
            facts.append(record)
    return floats.drop(index=dropped).reset_index(drop=True), facts


def attach_proxies(weekly: pd.DataFrame, master: pd.DataFrame, ranks: pd.DataFrame, floats: pd.DataFrame) -> pd.DataFrame:
    """Add ``mcap`` (latest company-list market cap of the security within 12 months before) and
    ``float_usd`` (the CIK's XBRL public float measured within 12 months either side, nearest)."""
    cik = dict(zip(master["security_id"], master["cik"].astype(int)))
    weekly = weekly.assign(cik=weekly["security_id"].map(cik).astype("Int64"), _order=np.arange(len(weekly)),
                           week_end=weekly["week_end"].astype("datetime64[ns]"))
    caps = ranks.assign(week_end=pd.to_datetime(ranks["snapshot_date"]).astype("datetime64[ns]"))[
        ["security_id", "week_end", "market_cap"]]
    left = weekly[["security_id", "week_end", "_order"]].sort_values("week_end")
    merged = pd.merge_asof(left, caps.sort_values("week_end"), on="week_end", by="security_id", direction="backward",
                           tolerance=pd.Timedelta(days=PROXY_CARRY_DAYS))
    weekly["mcap"] = merged.set_index("_order")["market_cap"].reindex(weekly["_order"]).values
    left = weekly[["cik", "week_end", "_order"]].dropna(subset=["cik"]).astype({"cik": int}).sort_values("week_end")
    facts = floats.rename(columns={"end": "week_end"}).astype({"cik": int, "week_end": "datetime64[ns]"})
    facts = facts.sort_values("week_end")
    merged = pd.merge_asof(left, facts, on="week_end", by="cik", direction="nearest",
                           tolerance=pd.Timedelta(days=PROXY_CARRY_DAYS))
    weekly["float_usd"] = merged.set_index("_order")["val"].reindex(weekly["_order"]).values
    return weekly.drop(columns=["_order"])


def proxy_cutoffs(weekly: pd.DataFrame, low: int = 200, high: int = 250) -> pd.DataFrame:
    """Per week, the median market cap and the median float of the names ranked ``low``-``high``
    by dv50 (plan 3.3 check 1)."""
    band = weekly[(weekly["dv50_rank"] >= low) & (weekly["dv50_rank"] <= high)]
    return band.groupby("week_end").agg(cut_mcap=("mcap", "median"), cut_float=("float_usd", "median"))


def proxy_above(weekly: pd.DataFrame, cutoffs: pd.DataFrame) -> pd.Series:
    """True where a row's market cap (or, without one, its float) reaches that week's cutoff."""
    cut = cutoffs.reindex(weekly["week_end"]).reset_index(drop=True)
    mcap, flt = weekly["mcap"].reset_index(drop=True), weekly["float_usd"].reset_index(drop=True)
    above = (mcap >= cut["cut_mcap"]) | (mcap.isna() & (flt >= cut["cut_float"]))
    return pd.Series(above.fillna(False).values, index=weekly.index)


# ------------------------------------------------------------------ stage 4: per-security facts

TRADING_EXCHANGES = {"Nasdaq", "NYSE", "CBOE"}
# Successor routing needs this many predecessor sessions in the successor's stored file, dated
# this many days before the successor's own first listing (not a few overlap days at a handover).
SUCCESSOR_MIN_ROWS, SUCCESSOR_LEAD_DAYS = 120, 90
WARMUP_DAYS = 75  # calendar days before the first uncovered week (50 sessions for the median)
HOLD_DAYS = 28  # an exchange move keeps a vendor series up to 4 weeks after it (plan 4.5)


WHEN_ISSUED = re.compile(r"when[- ](issued|distributed)", re.IGNORECASE)


def same_ticker(a: str, b: str) -> bool:
    """The same ticker in Nasdaq's and SEC's spelling (UHALB and UHAL-B)."""
    clean = lambda t: re.sub(r"[^A-Z0-9]", "", str(t).upper())
    return bool(a) and clean(a) == clean(b)


def sec_current_tickers(row) -> list[str]:
    """The CIK's current SEC tickers on Nasdaq, NYSE or CBOE (warrants and units dropped)."""
    tickers, exchanges = row.tickers_sec_current.split(), row.exchanges_sec_current.split()
    if len(exchanges) != len(tickers):
        exchanges = [""] * len(tickers)
    return [t for t, x in zip(tickers, exchanges) if x in TRADING_EXCHANGES and not WARRANT_LIKE.search(t)]


def security_facts(weekly: pd.DataFrame, spans: pd.DataFrame, master: pd.DataFrame, best: pd.DataFrame,
                   form25: pd.DataFrame) -> pd.DataFrame:
    """One row per security: listing dates, activity, coverage and rank facts used by the rules."""
    universe = weekly[weekly["universe"]]
    missing = ~universe["vendor_ok"] & ~universe["outside_trading"]
    uncovered = universe[missing]
    rank = universe[["dv50_rank", "dv20_rank"]].min(axis=1)
    facts = pd.DataFrame({
        "universe_weeks": universe.groupby("security_id").size(),
        "first_universe_week": universe.groupby("security_id")["week_end"].min(),
        "last_universe_week": universe.groupby("security_id")["week_end"].max(),
        "uncovered_weeks": uncovered.groupby("security_id").size(),
        "first_uncovered": uncovered.groupby("security_id")["week_end"].min(),
        "last_uncovered": uncovered.groupby("security_id")["week_end"].max(),
        "best_rank": rank.groupby(universe["security_id"]).min(),
        "best_rank_uncovered": rank[missing].groupby(uncovered["security_id"]).min(),
        "uncovered_unpriced_weeks": uncovered["dv50"].isna().groupby(uncovered["security_id"]).sum(),
    })
    a1 = universe["week_end"].between(*A1_WINDOW)
    facts["best_rank_a1"] = rank[a1].groupby(universe.loc[a1, "security_id"]).min()
    # Stored-file dollar volume runs low in 2016 (median 0.876 of WIKI's, 0.936 of Yahoo/Tiingo's;
    # about 1.000 in every other year), so a stored-only week of 2016 meets A1 at rank 330.
    stored_2016 = (universe["week_end"].dt.year == 2016) & (universe["src"] == "stored") & ~universe["vendor_ok"]
    effective = rank.where(~stored_2016, rank * FETCH_RANK / STORED_2016_RANK)
    facts["best_rank_a1_effective"] = effective[a1].groupby(universe.loc[a1, "security_id"]).min()
    facts["uncovered_weeks"] = facts["uncovered_weeks"].fillna(0).astype(int)
    by_security = spans.sort_values("list_start").groupby("security_id")
    facts = facts.join(pd.DataFrame({
        "first_listed": by_security["list_start"].min(), "last_listed": by_security["list_end"].max(),
        "last_ticker": by_security["ticker"].last(),
        "tickers": by_security["ticker"].apply(lambda t: " ".join(dict.fromkeys(reversed(list(t))))),
    }), how="outer")
    # The ticker of the interval open at the latest snapshot (AGEN, not AGEND: the reverse-split
    # ticker of 2011-10-07 starts after AGEN's open interval does, so it is ``last_ticker``).
    open_spans = spans[spans["list_end"] == WINDOW_END].sort_values("list_start")
    facts["open_ticker"] = open_spans.groupby("security_id")["ticker"].last().reindex(facts.index).fillna("")
    last_held = spans.groupby(["security_id", "ticker"])["list_end"].max().reset_index()
    facts["ticker_last_held"] = last_held.groupby("security_id").apply(
        lambda g: " ".join(f"{t}:{e}" for t, e in zip(g["ticker"], g["list_end"])), include_groups=False)
    first_held = spans.groupby(["security_id", "ticker"])["list_start"].min().reset_index()
    facts["ticker_first_held"] = first_held.groupby("security_id").apply(
        lambda g: " ".join(f"{t}:{e}" for t, e in zip(g["ticker"], g["list_start"])), include_groups=False)
    for column in ("uncovered_weeks", "universe_weeks", "uncovered_unpriced_weeks"):
        facts[column] = facts[column].fillna(0).astype(int)
    first_price = best.groupby("security_id")["date"].min()
    facts["first_price"] = first_price.reindex(facts.index)
    info = master.set_index("security_id")
    facts = facts.join(info[["cik", "name", "share_class", "delist_date", "delist_form25_accession", "foreign_filer",
                             "transfer_date", "tickers_sec_current", "exchanges_sec_current"]], how="left")
    floats = form25.set_index("accession")[["public_float_usd", "float_check_flag", "implied_float_per_share",
                                             "classification"]]
    facts = facts.join(floats, on="delist_form25_accession")
    facts["public_float_usd"] = pd.to_numeric(facts["public_float_usd"], errors="coerce")
    facts["active_nasdaq"] = facts["last_listed"].eq(WINDOW_END)
    # Listed now (rule Y_active_all): an open interval at the latest snapshot that is common stock in the
    # last week (not a SPAC shell or a non-common name); ``foreign_now`` is reported, the rule itself
    # counts only universe (non-foreign) weeks.
    last = weekly[weekly["week_end"] == weekly["week_end"].max()]
    facts["listed_now"] = facts["active_nasdaq"] & facts.index.isin(set(last.loc[~last["non_common"], "security_id"]))
    facts["foreign_now"] = facts.index.isin(set(last.loc[last["foreign"], "security_id"]))
    # Listed again after a Form 25 (``listing_spans`` after_cut): where the later listing starts, and the
    # last day of the listing before the cut (``relisted_parts`` splits the need there).
    after = (spans["after_cut"].astype(str).eq("True") if "after_cut" in spans
             else pd.Series(False, index=spans.index))
    relist = spans[after].groupby("security_id")["list_start"].min()
    before_cut = spans[~after].groupby("security_id")["list_end"].max()
    facts["listed_before_cut"] = before_cut.reindex(facts.index).where(facts.index.isin(relist.index)).fillna("")
    facts["relisted_from"] = relist.reindex(facts.index).where(facts["listed_now"] & (facts["listed_before_cut"] != "")).fillna("")
    current = {r.security_id: sec_current_tickers(r) for r in master.itertuples(index=False)}
    facts["yahoo_tickers"] = [" ".join(current.get(s, [])) for s in facts.index]
    facts["active"] = facts["active_nasdaq"] | ((facts["delist_date"].fillna("") == "") & (facts["yahoo_tickers"] != ""))
    # Still trading but not listed now (round 6: moved to NYSE, UCBI -> UCB; or out of the latest
    # snapshots, BANX, OFS, RAND): Yahoo is asked under the SEC current ticker, except for a when-issued
    # or when-distributed line (LBYAV, LBYKV) and a class or tracking stock none of whose own tickers is
    # an SEC current ticker (LLYVA, LLYVK: the CIK's tickers now name other tracking stocks).
    group = master.set_index("security_id")["multi_class_group"] if "multi_class_group" in master else pd.Series(dtype=str)
    names = (spans.groupby("security_id")["name"].apply(lambda n: " | ".join(str(x) for x in n))
             if "name" in spans else pd.Series(dtype=str))
    excluded = {}
    for sid in facts.index[facts["active"] & ~facts["active_nasdaq"]]:
        if WHEN_ISSUED.search(str(names.get(sid, ""))):
            excluded[sid] = "when_issued"
        elif (".T-" in sid or str(group.get(sid, "") or "")) and not any(
                same_ticker(t, c) for t in str(facts.at[sid, "tickers"]).split() for c in current.get(sid, [])):
            excluded[sid] = "sec_tickers_of_another_class"
    facts["yahoo_excluded"] = pd.Series(excluded, dtype=object).reindex(facts.index).fillna("")
    # A security whose step-4 successor chain reaches an active security, when the vendor keeps
    # its history under the successor's ticker: some of its rows come from a stored file the
    # successor's CIK owns (old Quidel in QuidelOrtho's QDEL, old DraftKings in DKNG). A link
    # that only hands a ticker to another company (21st Century Fox -> Fox Corp) has no such rows.
    successor = dict(zip(master["security_id"], master["successor_security_id"]))
    cik_of = dict(zip(master["security_id"], master["cik"]))
    owners = (dict(zip(read_csv_text(PRICE_FILE_OWNERS)["file"], read_csv_text(PRICE_FILE_OWNERS)["cik"]))
              if PRICE_FILE_OWNERS.exists() else {})
    stored_rows = best[best["src"] == "stored"][["security_id", "file", "date"]]
    stored_rows = stored_rows.assign(owner=stored_rows["file"].map(owners))
    via = {}
    for sid in facts.index[~facts["active"]]:
        nxt, hops = successor.get(sid, ""), 0
        while nxt and hops < 5 and nxt in facts.index and not facts.at[nxt, "active"]:
            nxt, hops = successor.get(nxt, ""), hops + 1
        if not (nxt and nxt in facts.index and facts.at[nxt, "active"]):
            continue
        heir_listed = pd.Timestamp(facts.at[nxt, "first_listed"]) - pd.Timedelta(days=SUCCESSOR_LEAD_DAYS)
        mine = stored_rows[(stored_rows["security_id"] == sid) & (stored_rows["owner"] == cik_of.get(nxt))
                           & (stored_rows["date"] < heir_listed)]
        if len(mine) >= SUCCESSOR_MIN_ROWS:
            via[sid] = nxt
    facts["via_successor"] = pd.Series(via).reindex(facts.index).fillna("")
    return facts


def needed_window(row) -> tuple[str, str]:
    """[start, end] of the dates a fetch must cover: from 75 days before the first uncovered week
    (not before the listing or 2011-06-01) to the last uncovered week, or to the end of the
    listing when the gap runs to it (plus 4 weeks after an exchange move, to 2026-08-31 at most).
    When the listing ends in a foreign-filer span (``ends_foreign``: CBPO turned foreign in 2018
    and was listed to 2021), the listed weeks after the last universe week are foreign, so the
    window ends 4 weeks after that week (a position held through the exit is still priced)."""
    start = max(WINDOW_START, str(row.first_listed)[:10],
                (row.first_uncovered - pd.Timedelta(days=WARMUP_DAYS)).strftime("%Y-%m-%d"))
    if (row.last_universe_week - row.last_uncovered).days <= 7:
        end = str(row.last_listed)[:10]
        if isinstance(row.transfer_date, str) and row.transfer_date:
            end = (pd.Timestamp(end) + pd.Timedelta(days=HOLD_DAYS)).strftime("%Y-%m-%d")
        if bool(getattr(row, "ends_foreign", False)):
            end = min(end, (row.last_universe_week + pd.Timedelta(days=HOLD_DAYS)).strftime("%Y-%m-%d"))
    else:
        end = row.last_uncovered.strftime("%Y-%m-%d")
    return start, min(end, WINDOW_END)


# Round 9: a need that crosses a listing gap longer than the dv50 warm-up plus the hold (75 + 28 days) is split
# there. The weeks between two listings are not universe weeks (Capstone: Form 25 2023-10-22 as CGRN, OTC as CGRNQ,
# listed again as CEPL from 2026-07-02), so a source that lacks them must not make the row partial or unfillable.
# A shorter gap stays inside one need: the warm-up and the hold of its two sides would meet anyway.
SPLIT_GAP_DAYS = WARMUP_DAYS + HOLD_DAYS


def listing_runs(spans: pd.DataFrame, weekly: pd.DataFrame | None = None,
                 gap_days: int = SPLIT_GAP_DAYS) -> dict[str, list[tuple[str, str, np.ndarray | None]]]:
    """security -> its listing runs (first day, last day, the week ends of its uncovered universe weeks in the
    run, or None without ``weekly``): the listing spans merged across gaps of at most ``gap_days`` days. Only
    securities with two runs or more are returned. Uncovered weeks are counted as ``security_facts`` counts them
    (a universe week without vendor raw cover, inside the trading bounds)."""
    uncovered = {}
    if weekly is not None and len(weekly):
        mask = weekly["universe"].astype(bool) & ~weekly["vendor_ok"].astype(bool)
        if "outside_trading" in weekly:
            mask &= ~weekly["outside_trading"].astype(bool)
        u = weekly[mask]
        uncovered = {s: np.sort(g.values.astype("datetime64[D]")) for s, g in u.groupby("security_id")["week_end"]}
    out = {}
    for sid, g in spans.groupby("security_id"):
        runs: list[list[str]] = []
        for a, b in sorted(zip(g["list_start"].astype(str), g["list_end"].astype(str))):
            if runs and _days(runs[-1][1], a) <= gap_days:
                runs[-1][1] = max(runs[-1][1], b)
            else:
                runs.append([a, b])
        if len(runs) < 2:
            continue
        weeks = uncovered.get(sid, np.array([], dtype="datetime64[D]")) if weekly is not None else None
        out[sid] = [(a, b, None if weeks is None else weeks[(weeks >= np.datetime64(a)) & (weeks <= np.datetime64(b))])
                    for a, b in runs]
    return out


def split_at_listing_gaps(start: str, end: str, runs: list | None) -> list[tuple[str, str, str]]:
    """[start, end] cut at the listing gaps it crosses: one (start, end, gap note) piece per listing run it meets
    (``listing_runs``). A piece ends on its run's last listed day and the next starts on its run's first listed
    day, as a Form 25 delisting ends a need and an IPO starts one (plan 3.2: "not before the listing"; the
    canonical panel still keeps a vendor's rows around a listing, flagged ``outside_listing``); the first piece
    keeps the need's start and the last its end. A piece without an uncovered universe week of the security is
    dropped (when the runs carry them). A need inside one run comes back whole, with an empty note."""
    met = [r for r in (runs or []) if r[0] <= end and r[1] >= start]
    if len(met) < 2:
        return [(start, end, "")]
    gaps = " ".join(f"{_shift(b, 1)}..{_shift(a, -1)}" for (_, b, _), (a, _, _) in zip(met, met[1:]))
    pieces = []
    for k, (a, b, weeks) in enumerate(met):
        low = start if k == 0 else max(start, a)
        high = end if k == len(met) - 1 else min(end, b)
        if low > high:
            continue
        if weeks is not None:
            inside = (weeks >= np.datetime64(max(low, a))) & (weeks <= np.datetime64(min(high, b)))
            if not inside.any():
                continue
        pieces.append((low, high, f"{GAP_NOTE} {gaps} (not listed: no universe weeks there)"))
    return pieces or [(start, end, "")]


# ------------------------------------------------------------------ split / dividend flags (V sample)

def _ordinary_ratio(value: float, tolerance: float = 0.001) -> bool:
    for denominator in range(1, 11):
        numerator = round(value * denominator)
        if 1 <= numerator <= 10 and abs(numerator / denominator / value - 1) <= tolerance:
            return True
    return False


def event_flags() -> pd.DataFrame:
    """Single-stock data flags for the V sample: ticker, date, kind (odd_split, split_after_2023,
    special_dividend), value, source. From the cached Yahoo and Tiingo files and WIKI's tables."""
    rows = []
    for directory in YAHOO_DIRS:
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            result = json.loads(path.read_text(encoding="utf-8"))["chart"]["result"][0]
            ticker = str(result.get("meta", {}).get("symbol") or path.stem).upper()
            events = result.get("events") or {}
            stamps = pd.to_datetime(result.get("timestamp") or [], unit="s").normalize()
            close = pd.Series(result["indicators"]["quote"][0]["close"], index=stamps, dtype=float)
            close = close * yahoo_split_factor(stamps, events.get("splits"))
            for event in (events.get("splits") or {}).values():
                day = pd.to_datetime(event["date"], unit="s").normalize()
                ratio = float(event["numerator"]) / float(event["denominator"])
                if not _ordinary_ratio(ratio):
                    rows.append((ticker, day, "odd_split", ratio, "yahoo"))
                if day >= pd.Timestamp("2024-01-01"):
                    rows.append((ticker, day, "split_after_2023", ratio, "yahoo"))
            for event in (events.get("dividends") or {}).values():
                day = pd.to_datetime(event["date"], unit="s").normalize()
                prior = close[close.index < day].dropna()
                factor = yahoo_split_factor(pd.DatetimeIndex([day]), events.get("splits"))[0]
                if len(prior) and float(event["amount"]) * factor > 0.10 * prior.iloc[-1]:
                    rows.append((ticker, day, "special_dividend", float(event["amount"]) * factor / prior.iloc[-1], "yahoo"))
    for directory in TIINGO_DIRS:
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            payload = json.loads(path.read_text(encoding="utf-8"))
            ticker = str((payload.get("meta") or {}).get("ticker") or path.stem).upper()
            prices = pd.DataFrame(payload.get("prices") or [])
            if prices.empty:
                continue
            prices["date"] = pd.to_datetime(prices["date"].str[:10])
            prior = prices["close"].shift(1)
            for day, split, cash, before in zip(prices["date"], prices["splitFactor"], prices["divCash"], prior):
                if split != 1.0 and not _ordinary_ratio(float(split)):
                    rows.append((ticker, day, "odd_split", float(split), "tiingo"))
                if split != 1.0 and day >= pd.Timestamp("2024-01-01"):
                    rows.append((ticker, day, "split_after_2023", float(split), "tiingo"))
                if cash and before and cash > 0.10 * before:
                    rows.append((ticker, day, "special_dividend", cash / before, "tiingo"))
    wiki = common.CACHE / "wiki"
    if (wiki / "wiki_split_events.csv").exists():
        splits = pd.read_csv(wiki / "wiki_split_events.csv", dtype={"ticker": str}, keep_default_na=False)
        for ticker, day, ratio in zip(splits["ticker"], splits["date"], splits["split_ratio"]):
            if not _ordinary_ratio(float(ratio)):
                rows.append((ticker, pd.Timestamp(day), "odd_split", float(ratio), "wiki"))
    if (wiki / "wiki_dividends.csv").exists():
        dividends = pd.read_csv(wiki / "wiki_dividends.csv", dtype={"ticker": str}, keep_default_na=False)
        big = dividends[pd.to_numeric(dividends["pct_of_prior_close"], errors="coerce") > 0.10]
        for ticker, day, pct in zip(big["ticker"], big["date"], big["pct_of_prior_close"]):
            rows.append((ticker, pd.Timestamp(day), "special_dividend", float(pct), "wiki"))
    for ticker, (day, kind) in V_KNOWN_EVENTS.items():
        rows.append((ticker, pd.Timestamp(day), kind, np.nan, "plan_4.3"))
    return pd.DataFrame(rows, columns=["ticker", "date", "kind", "value", "source"])


def stored_break_names(day: str = BREAK_DATE, directory: Path = STORED_DIR) -> list[str]:
    """Stored files whose close jumps by a split-sized ratio on ``day`` (the 2025-06-24 unit break:
    rows from it on are in post-split units, earlier rows were not rescaled)."""
    from src.io.nasdaq_update import _split_ratio

    names = []
    for path in sorted(directory.glob("*.csv")):
        data = pd.read_csv(path, usecols=lambda c: c in ("date", "close"))
        data = data[(data["date"] >= "2025-06-01") & (data["date"] <= day)]
        if len(data) >= 2 and data["date"].iloc[-1] == day and data["close"].iloc[-2] > 0:
            ratio = data["close"].iloc[-1] / data["close"].iloc[-2]
            if (ratio >= 1.4 or ratio <= 1 / 1.4) and _split_ratio(1 / ratio) is not None:
                names.append(path.stem.upper())
    return names


# ------------------------------------------------------------------ stage 5: rules

# Fetch priority (plan 7 fallback 2: B-A, A, B-B, C), then the names delisted after 2024-06 that rank
# in the top 300 or carry a tier-A/B float with no price at all (S: PARA, LOGC, NKLA, LAZR, XELA rank
# 8-46; the plan's tiers stop at 2024-06), then the tier-C sample and the V sample.
REASON_PRIORITY = ["B_A_float_ge_1B", "A1_wiki_dv_rank300", "A2_mcap_rank400", "A3_float_ge_1B_2012_2018",
                   "B_B_float_500M_1B", "C_late_start", "S_stored_only_delisted_rank300",
                   "S_float_delisted_after_2024_06", "B_C_sample_300M_500M", "V_verify_sample",
                   "Y_active_rank300", "B_C_rest_300M_500M", "Y_active_all"]
MONTH_2_REASONS = {"B_C_rest_300M_500M"}
# The candidate-list statuses the Tiingo fetcher's month-2 run selects (``reversal_data_tiingo.py --month2``, the
# same as ``--statuses pending,deferred_quota,pending_month2,conditional_tier_c``): rows not asked yet, rows the
# budget deferred, rows planned for month 2 (the Yahoo fallbacks among them, ``FALLBACK_STATUS``) and the tier-C
# rest while its sample has not ruled it out (``apply_tier_c_result``).
FALLBACK_STATUS = "pending_month2"
MONTH_2_STATUSES = ("pending", "deferred_quota", "pending_month2", "conditional_tier_c")
S_FLOAT_TIERS = {"B_A", "B_B"}


def seeded_sample(ids: list[str], n: int, seed: int = SEED) -> list[str]:
    """The first ``n`` of ``seeded_order(ids, seed)``, sorted."""
    ids = sorted(ids)
    if len(ids) <= n:
        return ids
    return sorted(seeded_order(ids, seed)[:n])


def float_usable(value: float, flag: str, per_share=None) -> bool:
    """A Form 25 float that passed step 3's check, and (at $20B or more) $3,000 a share or less."""
    if pd.isna(value) or flag not in GOOD_FLOAT_FLAGS:
        return False
    per_share = pd.to_numeric(per_share, errors="coerce")
    return not (value >= BIG_FLOAT and (pd.isna(per_share) or per_share > MAX_FLOAT_PER_SHARE))


def float_tier(value: float, flag: str, per_share=None) -> str:
    if not float_usable(value, flag, per_share):
        return ""
    for name, low, high in FLOAT_TIERS:
        if low <= value < high:
            return name
    return ""


def late_start(row) -> tuple[str, str] | None:
    """The missing window of a series that starts more than LATE_START_DAYS after the listing."""
    if pd.isna(row.first_price):
        return None
    listed = max(pd.Timestamp(WINDOW_START), pd.Timestamp(row.first_listed))
    if (row.first_price - listed).days <= LATE_START_DAYS:
        return None
    return listed.strftime("%Y-%m-%d"), (row.first_price - pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def rule_hits(facts: pd.DataFrame, ranks: pd.DataFrame, floats: pd.DataFrame) -> dict[str, dict[str, tuple]]:
    """security -> {reason: (metric, value)} for every rule that holds (V is added later)."""
    hits: dict[str, dict[str, tuple]] = defaultdict(dict)
    a2 = ranks[ranks["snapshot_date"].between(*A2_WINDOW)]
    best_mcap = a2.groupby("security_id")["mcap_rank"].min()
    float_by_cik = {c: g for c, g in floats.groupby("cik")}
    caps_by_security = {s: g for s, g in ranks.groupby("security_id")}
    for sid, row in facts.iterrows():
        if row.uncovered_weeks <= 0:
            continue
        delist = row.delist_date or ""
        if pd.notna(row.best_rank_a1_effective) and row.best_rank_a1_effective <= FETCH_RANK:
            metric = "dv_rank_min_2012_2018" if row.best_rank_a1 <= FETCH_RANK else "dv_rank_min_2012_2018_stored_2016_cut_330"
            hits[sid]["A1_wiki_dv_rank300"] = (metric, int(row.best_rank_a1))
        if sid in best_mcap.index and best_mcap[sid] <= MCAP_RANK:
            hits[sid]["A2_mcap_rank400"] = ("mcap_rank_min_2011_11_2019_06", int(best_mcap[sid]))
        tier = float_tier(row.public_float_usd, row.float_check_flag, row.implied_float_per_share)
        if delist and B_WINDOW[0] <= delist <= B_WINDOW[1] and tier:
            reason = {"B_A": "B_A_float_ge_1B", "B_B": "B_B_float_500M_1B", "B_C": "B_C_tier"}[tier]
            hits[sid][reason] = ("form25_max_float_3y_usd", float(row.public_float_usd))
        if (delist and "2012-01-01" <= delist <= WIKI_END
                and float_usable(row.public_float_usd, row.float_check_flag, row.implied_float_per_share)
                and row.public_float_usd >= A3_FLOAT and not hits[sid]):
            hits[sid]["A3_float_ge_1B_2012_2018"] = ("form25_max_float_3y_usd", float(row.public_float_usd))
        window = late_start(row)
        if window:
            caps = caps_by_security.get(sid)
            in_caps = caps[caps["snapshot_date"].between(*window)] if caps is not None else None
            facts_cik = float_by_cik.get(int(row.cik)) if pd.notna(row.cik) else None
            in_float = (facts_cik[(facts_cik["end"] >= window[0]) & (facts_cik["end"] <= window[1])]
                        if facts_cik is not None else None)
            reasons = []
            if in_caps is not None and len(in_caps) and in_caps["mcap_rank"].min() <= MCAP_RANK:
                reasons.append(("mcap_rank_min_missing_window", int(in_caps["mcap_rank"].min())))
            if in_float is not None and len(in_float) and in_float["val"].max() >= A3_FLOAT:
                reasons.append(("xbrl_float_max_missing_window_usd", float(in_float["val"].max())))
            if reasons:
                hits[sid]["C_late_start"] = reasons[0] + (window,)
        if ((row.active or row.via_successor) and pd.notna(row.best_rank_uncovered)
                and row.best_rank_uncovered <= FETCH_RANK):
            hits[sid]["Y_active_rank300"] = ("dv_rank_min_uncovered_weeks", int(row.best_rank_uncovered))
        # Every name still trading whose universe weeks a vendor raw series does not cover, whatever its
        # dollar volume or size (round 5: SMCI, CHRD, CORZ, SIGA were listed but never ranked here): listed
        # now, or (round 6) active by SEC (a current ticker, no delisting) after a move to NYSE (UCBI, CNMD)
        # or out of the latest snapshots (BANX, OFS, RAND), unless Yahoo cannot be asked for it
        # (``yahoo_excluded``: a when-issued line, or SEC tickers that name another class).
        # (A name still on Nasdaq that is not common stock in the last week stays out, as before.)
        listed = bool(getattr(row, "listed_now", False)) and not bool(getattr(row, "spac_like_now", False))
        excluded = getattr(row, "yahoo_excluded", "")
        trading = (bool(row.active) and str(row.last_listed)[:10] != WINDOW_END
                   and (pd.isna(excluded) or not str(excluded)))
        if listed or trading:
            hits[sid]["Y_active_all"] = ("uncovered_universe_weeks", int(row.uncovered_weeks))
        if (not row.active and not row.via_successor and str(row.last_listed)[:10] > S_AFTER
                and pd.notna(row.best_rank_uncovered)
                and row.best_rank_uncovered <= FETCH_RANK):
            hits[sid]["S_stored_only_delisted_rank300"] = ("dv_rank_min_uncovered_weeks", int(row.best_rank_uncovered))
        # The float tiers A and B for delistings after the plan's B window, when the uncovered weeks
        # have no rank or almost no price (no stored file to rank: Cerevel, Encore Wire, National
        # Western, 323 of their 327 uncovered weeks unpriced).
        unranked = pd.isna(row.best_rank_uncovered) or row.uncovered_unpriced_weeks >= 0.9 * row.uncovered_weeks
        if (not row.active and not row.via_successor and delist and S_AFTER < delist <= WINDOW_END
                and tier in S_FLOAT_TIERS and unranked and "S_stored_only_delisted_rank300" not in hits[sid]):
            hits[sid]["S_float_delisted_after_2024_06"] = ("form25_max_float_3y_usd", float(row.public_float_usd))
    tier_c = sorted(s for s, h in hits.items() if "B_C_tier" in h)
    # The sample tests tier C on its own, so it is drawn from tier-C names that no higher rule
    # (A, B-A, B-B, C) already fetches; the others are fetched for that rule anyway.
    higher = set(REASON_PRIORITY[:REASON_PRIORITY.index("B_C_sample_300M_500M")])
    alone = [s for s in tier_c if not (set(hits[s]) & higher)]
    sample = set(seeded_sample(alone, TIER_C_SAMPLE))
    for sid in tier_c:
        value = hits[sid].pop("B_C_tier")
        hits[sid]["B_C_sample_300M_500M" if sid in sample else "B_C_rest_300M_500M"] = value
    return {s: h for s, h in hits.items() if h}


# Slots each V category keeps before any category takes more (the cap of 50 used to fill before
# special dividends were reached).
V_RESERVE = {"stored_break_2025_06_24": 8, "split_after_2023": 8, "special_dividend": 8, "odd_split": 8}


def verification_sample(facts: pd.DataFrame, weekly: pd.DataFrame, events: pd.DataFrame,
                        breaks: list[str], usable=None, keep: dict[str, str] | None = None) -> dict[str, str]:
    """security -> V category for 50 active names: the plan's named break names and named events, then up to
    V_RESERVE names of each category (the stored files' 2025-06-24 break names, splits after 2023,
    special dividends above 10%, odd split factors; each by best dv rank), then the categories in
    turn until 50, then a seeded draw from active names ranked <= 300 in the last year, in three rank
    buckets. ``usable(sid)``: whether Tiingo serves a row for the name (others are skipped).
    ``keep``: the sample already drawn (security -> category, from the candidate list on disk). Its
    names stay, with their categories, while they are still active and usable, so that a later run (new
    active names, shifted ranks: SMCI's relisting in round 5) does not redraw a sample Tiingo is already
    fetching; only freed slots are filled by the rules above."""
    # Active names that are in the universe (non-foreign common stock) in the last year.
    recent_start = pd.Timestamp(LAST_WEEK) - pd.DateOffset(years=1)
    active = facts[facts["active"] & (facts["last_universe_week"] >= recent_start)]
    if usable is not None:
        active = active[[bool(usable(sid)) for sid in active.index]]
    by_ticker = {}
    for sid, row in active.iterrows():
        # Its own Nasdaq tickers and the one Yahoo would be asked for (not every SEC ticker of the
        # CIK: those include notes and preferreds, e.g. OXLCN of Oxford Lane).
        for ticker in str(row.tickers).split() + [yahoo_ticker(row)]:
            by_ticker.setdefault(ticker, sid)
    order = active["best_rank"].fillna(1e9)
    chosen: dict[str, str] = {}
    for sid, kind in sorted((keep or {}).items()):
        if sid in active.index and len(chosen) < V_SAMPLE:
            chosen[sid] = kind

    def ranked(sids):
        return [s for s in sorted(set(sids), key=lambda s: (order.get(s, 1e9), s)) if s not in chosen]

    for sid in ranked(by_ticker[t] for t in V_NAMED if t in by_ticker):
        if len(chosen) < V_SAMPLE:
            chosen[sid] = "named_break_2025_06_24"
    # The events plan 4.3 names to reproduce (PRPL 1:25 on 2026-07-20, SIRI, MNST ...) are always in.
    for ticker, (_, kind) in V_KNOWN_EVENTS.items():
        sid = by_ticker.get(ticker)
        if sid and sid not in chosen and len(chosen) < V_SAMPLE:
            chosen[sid] = kind
    pools = {"stored_break_2025_06_24": [by_ticker[t] for t in breaks if t in by_ticker]}
    for kind in ("split_after_2023", "special_dividend", "odd_split"):
        pools[kind] = [by_ticker[t] for t in events.loc[events["kind"] == kind, "ticker"] if t in by_ticker]
    for kind, reserve in V_RESERVE.items():
        for sid in ranked(pools[kind])[:reserve]:
            if len(chosen) < V_SAMPLE:
                chosen[sid] = kind
    while len(chosen) < V_SAMPLE:
        added = False
        for kind in V_RESERVE:
            rest = ranked(pools[kind])
            if rest and len(chosen) < V_SAMPLE:
                chosen[rest[0]] = kind
                added = True
        if not added:
            break
    if len(chosen) < V_SAMPLE:
        recent = weekly[(weekly["week_end"] >= recent_start) & weekly["universe"]
                        & weekly["security_id"].isin(active.index) & ~weekly["security_id"].isin(list(chosen))]
        best = recent.groupby("security_id")["dv50_rank"].min().dropna()
        buckets = [best[(best > lo) & (best <= hi)].index.tolist() for lo, hi in ((0, 100), (100, 200), (200, 300))]
        need = V_SAMPLE - len(chosen)
        for k, ids in enumerate(buckets):
            share = need // 3 + (1 if k < need % 3 else 0)
            for sid in seeded_sample(ids, share, SEED + k):
                chosen.setdefault(sid, f"random_rank_{k * 100 + 1}_{k * 100 + 100}")
    return chosen


# ------------------------------------------------------------------ stage 6: routing

CANDIDATE_COLUMNS = ["security_id", "ticker_for_source", "needed_start", "needed_end", "reason", "prefilter_metric",
                     "prefilter_value", "planned_source", "tiingo_range_match", "status", "fetch_month",
                     # extras
                     "reasons_all", "priority", "cik", "name", "active", "delist_date", "uncovered_weeks",
                     "best_rank", "tiingo_row_start", "tiingo_row_end", "tiingo_coverage", "tiingo_reused_ticker",
                     "v_category", "note", "tiingo_flags", "fetch_order",
                     # round 6: the Yahoo answer written back, the Tiingo fallback it calls for, and the
                     # day a relisted security's new listing starts in its Yahoo series
                     "yahoo_row_start", "yahoo_row_end", "yahoo_coverage", "fallback_from", "junction_date"]
UNFILLABLE_COLUMNS = ["security_id", "ticker", "needed_start", "needed_end", "sources_tried", "est_weeks_in_top250",
                      "proxy", "proxy_value", "reason", "cik", "name", "delist_date", "status"]


def yahoo_ticker(row) -> str:
    """The Yahoo symbol of a security: the open interval's ticker for a name listed on Nasdaq now (the
    Yahoo build asks the SEC current ticker as well when that answer is poor: LIXT -> NMAD); otherwise
    its SEC current ticker, the last Nasdaq ticker when SEC still lists it, else the one spelled like it
    (UHALB -> UHAL-B), else the first."""
    current = str(row.yahoo_tickers).split()
    if row.active_nasdaq:
        return str(getattr(row, "open_ticker", "") or "") or row.last_ticker
    if not current:
        return row.last_ticker
    if row.last_ticker in current:
        return row.last_ticker
    return next((c for c in current if same_ticker(row.last_ticker, c)), current[0])


WARRANT_LIKE = re.compile(r"^[A-Z]{4}(W|WS|U|R)$|[-.](W|WS|U|R|WT)$")


# Reviewed aliases (round 2): Tiingo keeps these histories under a ticker no listing interval of the
# security names. 21st Century Fox renamed its classes TFCFA/TFCF on 2019-03-12/13, when Fox Corp
# took FOXA/FOX (rows 1996-03-11 and 1987-12-30 to 2019-03-20, the Disney close); MSG Networks moved
# to NYSE as MSGN in 2015 (row 2010-01-25..2021-07-09); old Zillow Inc's Z (2011-2015) continues
# under Zillow Group's class A ZG (row from 2011-07-20, Zillow's IPO).
TIINGO_ALIASES = {"1308161.A": "TFCFA", "1308161.B": "TFCF", "1469372": "MSGN", "1334814": "ZG"}


def q_suffix_map(index: dict) -> dict[str, list[str]]:
    """base ticker -> Tiingo tickers of the form base + one letter + 'Q' (SDC -> SDCCQ, WIN -> WINMQ,
    FTD -> FTDCQ): the OTC tickers of bankrupt companies that do not just append Q."""
    out = defaultdict(list)
    for ticker in index:
        if len(ticker) >= 4 and ticker.endswith("Q") and ticker[-2].isalpha():
            out[ticker[:-2]].append(ticker)
    return out


def tiingo_tickers(row, need_start: str = "", sid: str = "", qmap: dict | None = None) -> list[tuple[str, str]]:
    """(ticker, kind) to try, in order: the security's own Nasdaq tickers (newest first) that it
    still held on or after ``need_start`` (a vendor keeps a history under the later ticker, never
    under one the company gave up before: 21st Century Fox's old NWSA is News Corp's from 2013);
    a reviewed alias; each own ticker plus Q (Tiingo keeps a bankrupt company's whole Nasdaq history
    under its OTC ticker: SIVBQ, BBBYQ, CLVSQ, ENDPQ); the CIK's current SEC tickers (warrants, units
    and rights dropped); own ticker + one letter + Q (``qmap``)."""
    held = dict(pair.rsplit(":", 1) for pair in str(row.ticker_last_held).split() if ":" in pair)
    own = [t for t in str(row.tickers).split() if not need_start or held.get(t, "9999") >= need_start]
    out = [(t, "own") for t in own]
    if sid in TIINGO_ALIASES:
        out.append((TIINGO_ALIASES[sid], "reviewed_alias"))
    out += [(t + "Q", "q_suffix") for t in own if not t.endswith("Q")]
    seen = {t for t, _ in out}
    out += [(t, "sec_current") for t in str(row.tickers_sec_current).split()
            if t not in seen and not WARRANT_LIKE.search(t)]
    seen = {t for t, _ in out}
    for t in own:
        out += [(q, "q_prefix") for q in sorted((qmap or {}).get(t, [])) if q not in seen]
    return out


def security_info(row) -> dict:
    """What tiingo_range_match needs to know about a security (a security_facts row)."""
    starts = dict(pair.rsplit(":", 1) for pair in str(getattr(row, "ticker_first_held", "")).split() if ":" in pair)
    return {"first_listed": str(row.first_listed)[:10], "last_listed": str(row.last_listed)[:10],
            "active": bool(row.active), "transfer_date": row.transfer_date if isinstance(row.transfer_date, str) else "",
            "ticker_starts": starts}


def wiki_alternative(sid: str, row, start: str, end: str, lists: pd.DataFrame) -> str:
    """A WIKI file under one of the CIK's current SEC tickers that no listing interval of this
    security names, covering at least half of [start, min(end, WIKI_END)] and agreeing with the
    company lists' LastSale: returned as 'TICKER', else ''."""
    from pipelines.reversal_data.wiki import READ_KW, safe_ticker

    if start > WIKI_END:
        return ""
    stop = min(end, WIKI_END)
    for ticker in [t for t in str(row.tickers_sec_current).split() if t not in str(row.tickers).split()]:
        path = WIKI_DIR / f"{safe_ticker(ticker)}.csv.gz"
        if not path.exists():
            continue
        data = pd.read_csv(path, usecols=["ticker", "date", "close"], **READ_KW)
        inside = data[(data["date"] >= start) & (data["date"] <= stop)]
        if len(inside) < 0.5 * 252 * max(_days(start, stop), 1) / 365:
            continue
        quotes = lists[(lists["security_id"] == sid) & (lists["last_sale"] > 0)]
        joined = inside.merge(quotes, left_on="date", right_on="as_of_session")
        if len(joined) and ((joined["close"] / joined["last_sale"] - 1).abs() <= 0.02).mean() >= 0.5:
            return ticker
    return ""


MIN_PARTIAL_COVERAGE, LOW_PARTIAL_COVERAGE = 0.5, 0.1


def useful_partial(match: dict, need_start: str) -> bool:
    """Spend a Tiingo symbol on a full match; on a partial one only when it covers at least half
    the need, or at least a tenth of it from its start (the same company until Tiingo stops). A
    short row that starts inside the need is most likely a newer company on a reused ticker."""
    if match["match"] == "Y":
        return True
    if match["match"] != "partial":
        return False
    if match["coverage"] >= MIN_PARTIAL_COVERAGE:
        return True
    starts_in_time = _days(match["row_start"], need_start) >= -TIINGO_START_SLACK_DAYS
    return starts_in_time and match["coverage"] >= LOW_PARTIAL_COVERAGE


# Flags on a Tiingo match that make the fetcher confirm the entity after the answer (the served
# date range must be the matched row's, and a dollar-volume or LastSale reference must agree).
AMBIGUOUS_FLAGS = ("multi_row", "sec_holder", "starts_late", "shared", "alias:q_prefix", "alias:sec_current",
                   "non_stock_row", "outlives_listing")
FETCH_STATUS = common.CACHE / "tiingo" / "fetch_status.csv"
FETCHED_OK = {"done", "done_review", "partial"}


def fetched_outcomes(path: Path = FETCH_STATUS) -> dict[tuple[str, str], dict]:
    """(security_id, ticker) -> the Tiingo fetcher's verdict on an answer it holds (status, entity
    check, notes); requests that were never answered (precheck, deferred, error) are left out."""
    if not Path(path).exists():
        return {}
    status = read_csv_text(path)
    status = status[status["http_status"].isin(["200", "404"]) & (status["entity_check"] != "precheck")]
    return {(r.security_id, r.ticker_for_source.upper()): {"status": r.status, "entity_check": r.entity_check,
                                                          "notes": r.entity_notes, "first": r.first_date,
                                                          "last": r.last_date}
            for r in status.itertuples(index=False)}


def sec_ticker_holders(master: pd.DataFrame) -> dict[str, set[str]]:
    """ticker -> CIKs SEC currently lists for it."""
    out = defaultdict(set)
    for cik, tickers in zip(master["cik"], master["tickers_sec_current"]):
        for ticker in str(tickers).split():
            out[ticker.upper()].add(str(cik))
    return out


def match_flags(match: dict, index: dict, cik: str, need_start: str, holders: dict[str, set[str]]) -> list[str]:
    """Why a match may serve another company (see AMBIGUOUS_FLAGS); ``shared`` is added later."""
    flags = []
    ticker = match["ticker"]
    if match.get("kind") and match["kind"] != "own":
        flags.append(f"alias:{match['kind']}")
    if (match.get("asset_type") or "Stock").lower() != "stock":
        flags.append(f"non_stock_row:{match['asset_type']}")
    rows = [r for r in index.get(ticker, []) if r["start"] and r["end"]]
    if any(r["end"] >= need_start and (r["start"], r["end"]) != (match["row_start"], match["row_end"]) for r in rows):
        flags.append("multi_row")
    if match.get("starts_late"):
        flags.append("starts_late")
    if match.get("outlives") and match.get("kind") in ("own", "sec_current"):
        flags.append("outlives_listing")
    others = holders.get(ticker, set()) - {str(cik)}
    if others and str(cik) not in holders.get(ticker, set()):
        flags.append("sec_holder:" + "/".join(sorted(others)))
    return flags


def match_note(match: dict) -> str:
    """Plain words for an unusable match."""
    if match["match"] == "hidden" and match.get("hidden_by"):
        later = match["hidden_by"]
        return (f"Tiingo row {match['row_start']}..{match['row_end']} of {match['ticker']} covers the need, but the "
                f"API serves the {match['ticker']} row {later['start']}..{later['end']} ({later['asset_type']}, "
                f"{later['exchange'] or 'no exchange'}): another company")
    if match["match"] == "newer_company":
        return (f"Tiingo's {match['ticker']} row {match['row_start']}..{match['row_end']} is another company: it starts "
                f"long after the security began using the ticker, or another row of the ticker is the security's own")
    if match["match"] == "partial":
        return (f"Tiingo row {match['row_start']}..{match['row_end']} covers only {match['coverage']:.0%} of the need")
    if match["match"] == "fetched_wrong_entity":
        return f"fetched {match['ticker']}: another company ({match.get('fetched_notes', '')})"
    return ""


def _join_notes(*notes: str) -> str:
    return "; ".join(n for n in notes if n)


def relisted_parts(row, reasons: list[str], hits: dict) -> list[tuple[list[str], object, str, str, str]]:
    """The candidate rows of a security as (reasons, facts row, need start, need end, note).

    Usually one row: every reason, the security's own facts and ``needed_window``. A security listed
    again after a Form 25 (``relisted_from``: SMCI 2020-02, Oasis/Chord 2020-11, Core Scientific 2024-03
    after its bankruptcy, WW 2025) is listed now, but Yahoo serves the current ticker's history, which
    may start at the relisting (new equity out of a bankruptcy). So when it has uncovered weeks before
    the relisting and another rule than Y holds, that rule's row keeps the delisted routing (WIKI / Tiingo,
    the security taken as delisted on ``listed_before_cut``) for the need up to that day, and a Y row
    (Y_active_rank300, else Y_active_all) asks Yahoo for the need from the relisting on."""
    if not reasons:
        return []
    start, end = needed_window(row)
    relist = str(getattr(row, "relisted_from", "") or "")
    early = [r for r in reasons if not r.startswith("Y_")]
    late = [r for r in reasons if r.startswith("Y_")]
    if not (bool(row.active) and relist and str(getattr(row, "listed_before_cut", "") or "")
            and early and late and pd.Timestamp(row.first_uncovered) < pd.Timestamp(relist)):
        return [(reasons, row, start, end, "")]
    cut_end = str(row.listed_before_cut)[:10]
    pre = row.copy()
    pre["active"], pre["active_nasdaq"], pre["via_successor"] = False, False, ""
    pre["last_listed"] = cut_end
    # After a bankruptcy or a share exchange the relisted equity is a new security (round 6: Yahoo's CHRD
    # joins old Oasis at $0.12 to the new shares at $34): its warm-up is in the new shares only, from their
    # first session (``relist_first_session``: step 9's RELIST_JUNCTIONS, WW 2025-06-27 and CORZ 2024-01-24
    # before their 2025-07-02 and 2024-02-28 snapshots; else the relisting), never in the old shares
    # (``relist_new_equity``, from RELIST_JUNCTIONS, the terminal-return table and the Form 25 basis).
    new_equity = str(getattr(row, "relist_new_equity", "") or "")
    first_new = str(getattr(row, "relist_first_session", "") or "") or relist
    warmed = (pd.Timestamp(relist) - pd.Timedelta(days=WARMUP_DAYS)).strftime("%Y-%m-%d")
    post_start = max(start, warmed, first_new) if new_equity else max(start, warmed)
    note_pre = f"the need before the {row.delist_date} Form 25; listed again from {relist} (Yahoo row {late[0]})"
    note_post = f"listed again from {relist} after the {row.delist_date} Form 25; the need before it is the {early[0]} row"
    if new_equity:
        note_post += (f"; new equity ({new_equity}), first session {first_new}: no warm-up in the old shares")
    return [(early, pre, start, min(end, cut_end), note_pre), (late, row, post_start, end, note_post)]


# Step 9's hand-reviewed relist junctions (``reversal_data_reconcile.RELIST_JUNCTIONS``) are the reference for
# which relistings are new equity and where the new shares start; they are read from that file's source (its
# literal fields only, the module is not imported). This copy (round 7) is used only when the file cannot be read.
RECONCILE_SOURCE = Path(__file__).resolve().parent / "reconcile.py"
RELIST_JUNCTIONS_FALLBACK = {
    "1486159": {"first_new_session": "2020-11-20", "kind": "bankruptcy_new_equity"},      # Oasis -> CHRD
    "1839341": {"first_new_session": "2024-01-24", "kind": "bankruptcy_share_exchange"},  # Core Scientific
    "105319": {"first_new_session": "2025-06-27", "kind": "bankruptcy_share_exchange"},   # WW International
    "1456772": {"first_new_session": "2026-06-22", "kind": "bankruptcy_new_equity"},      # Office Properties
    "1556739": {"first_new_session": "2018-04-18", "kind": "bankruptcy_new_equity"},      # Dex Media -> Thryv
}
JUNCTION_FIELDS = ("first_new_session", "kind", "read")


def source_label(path: Path) -> str:
    """The name the prefilter summary records for where the junctions came from. Step 9 keeps its historical file
    name (``reversal_data_reconcile.py``) after the phase-3 move to pipelines/reversal_data/reconcile.py, so the
    summary is unchanged by the move."""
    return "reversal_data_reconcile.py" if Path(path).resolve() == RECONCILE_SOURCE else Path(path).name


def relist_junctions(path: Path = RECONCILE_SOURCE) -> tuple[dict[str, dict], str]:
    """(security -> {first_new_session, kind, read}, where it came from) of step 9's RELIST_JUNCTIONS."""
    import ast

    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return {k: dict(v) for k, v in RELIST_JUNCTIONS_FALLBACK.items()}, "fallback (reconcile source unreadable)"
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if not any(isinstance(t, ast.Name) and t.id == "RELIST_JUNCTIONS" for t in targets):
            continue
        if not isinstance(node.value, ast.Dict):
            break
        out = {}
        for key, value in zip(node.value.keys, node.value.values):
            if not (isinstance(key, ast.Constant) and isinstance(value, ast.Dict)):
                continue
            entry = {k.value: v.value for k, v in zip(value.keys, value.values)
                     if isinstance(k, ast.Constant) and k.value in JUNCTION_FIELDS and isinstance(v, ast.Constant)}
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(entry.get("first_new_session", ""))):
                out[str(key.value)] = entry
        if out:
            return out, f"{source_label(path)} RELIST_JUNCTIONS"
        break
    return {k: dict(v) for k, v in RELIST_JUNCTIONS_FALLBACK.items()}, "fallback (no RELIST_JUNCTIONS in the reconcile source)"


def relist_first_sessions(facts: pd.DataFrame, junctions: dict[str, dict]) -> pd.Series:
    """security -> the new shares' first session of a new-equity relisting (``relist_new_equity``): step 9's
    ``first_new_session`` when RELIST_JUNCTIONS has the security, else the relisting day; '' otherwise."""
    out = {}
    for sid, row in facts.iterrows():
        if str(row.get("relist_new_equity", "") or "") and str(row.get("relisted_from", "") or ""):
            out[sid] = str(junctions.get(sid, {}).get("first_new_session", "") or row["relisted_from"])
    return pd.Series(out, dtype=object).reindex(facts.index).fillna("")


def relisting_kinds(facts: pd.DataFrame, form25: pd.DataFrame, terminal_path: Path | None = None,
                    junctions: dict[str, dict] | None = None) -> pd.Series:
    """security -> 'bankruptcy' or 'share_exchange' for a relisted security (``relisted_from``) that step 9's
    RELIST_JUNCTIONS lists (its kind; Core Scientific, whose terminal subtype is removed_by_exchange), or whose
    Form 25 came with a bankruptcy (the terminal-return table's event_subtype: Oasis/Chord, WW, OPI) or a
    substituted share exchange (Rule 12d2-2(a)(3)); '' otherwise: the same shares listed again (SMCI's
    late-filing removal, SIGA, SCOR, MDXG), which keep one series and get no junction."""
    terminal_path = terminal_path or TERMINAL
    junctions = relist_junctions()[0] if junctions is None else junctions
    subtype = {}
    if Path(terminal_path).exists():
        terminal = read_csv_text(terminal_path)
        if "event_subtype" in terminal:
            subtype = dict(zip(terminal["security_id"], terminal["event_subtype"]))
    basis = dict(zip(form25["accession"], form25["delisting_basis"])) if "delisting_basis" in form25 else {}
    out = {}
    for sid, row in facts[facts["relisted_from"].fillna("") != ""].iterrows():
        if sid in junctions:
            out[sid] = "bankruptcy" if "bankruptcy" in str(junctions[sid].get("kind", "")) else "share_exchange"
        elif subtype.get(sid, "") == "bankruptcy":
            out[sid] = "bankruptcy"
        elif basis.get(row.get("delist_form25_accession", ""), "") == "substituted_merger_or_exchange":
            out[sid] = "share_exchange"
    return pd.Series(out, dtype=object).reindex(facts.index).fillna("")


def tiingo_match_fields(match: dict, sid: str, cik, start: str, index: dict, holders: dict, fetched: dict) -> dict:
    """The candidate-list columns of a Tiingo match (its flags include the fetcher's verdict, if any)."""
    flags = match_flags(match, index, cik, start, holders) if match["row_start"] else []
    seen = fetched.get((sid, match["ticker"].upper()))
    if seen:
        flags.append(f"fetched:{seen['status']}")
    return {"tiingo_range_match": match["match"], "tiingo_row_start": match["row_start"],
            "tiingo_row_end": match["row_end"], "tiingo_coverage": match["coverage"],
            "tiingo_flags": " ".join(flags), "ticker_for_source": match["ticker"]}


def best_tiingo_match(sid: str, candidates: list, start: str, end: str, index: dict, info: dict,
                      fetched: dict) -> dict:
    """The range match, with the fetcher's verdicts on answers it holds: a ticker whose answer failed
    the entity check for this security is passed over (and reported, ``fetched_wrong_entity``,
    when nothing else serves the need); a hidden or newer-company row whose answer passed it counts."""
    wrong = {t for t, _ in candidates if fetched.get((sid, t.upper()), {}).get("status") == "wrong_entity"}
    match = tiingo_range_match([c for c in candidates if c[0] not in wrong], start, end, index, info)
    held = fetched.get((sid, str(match.get("ticker", "")).upper()), {}).get("status") in FETCHED_OK
    if useful_partial(match, start) and not held:
        # Round 9: an answer the fetcher already holds for this security that serves the need as well costs no
        # symbol (Humanigen: HGENQ, asked for 2013-2023, also serves the KBIO piece 2013-02..2016-01 of a need split
        # at a listing gap, where the own ticker KBIO would rank first).
        for ticker, kind in candidates:
            if ticker in wrong or fetched.get((sid, ticker.upper()), {}).get("status") not in FETCHED_OK:
                continue
            alone = tiingo_range_match([(ticker, kind)], start, end, index, info)
            if alone["match"] == "Y" or (alone["match"] == match["match"] and alone["coverage"] >= match["coverage"]):
                return alone
    if not useful_partial(match, start):
        for ticker, kind in candidates:
            seen = fetched.get((sid, ticker.upper()))
            if seen and seen["status"] in FETCHED_OK:
                alone = tiingo_range_match([(ticker, kind)], start, end, index, info)
                alone["match"] = "Y" if seen["status"] != "partial" else "partial"
                return alone
        if wrong:
            first = [c for c in candidates if c[0] in wrong][0]
            failed = tiingo_range_match([first], start, end, index, info)
            seen = fetched[(sid, first[0].upper())]
            failed.update(match="fetched_wrong_entity", fetched_notes=seen["notes"])
            return failed
    return match


def route(facts: pd.DataFrame, hits: dict, vsample: dict, index: dict, lists: pd.DataFrame,
          holders: dict[str, set[str]] | None = None, fetched: dict | None = None,
          runs: dict | None = None) -> pd.DataFrame:
    """One candidate row per (security, rule part, listing run): ``relisted_parts`` gives the parts, and
    ``runs`` (``listing_runs``; round 9) cuts each part's need at the listing gaps it crosses
    (``split_at_listing_gaps``), each piece routed on its own (WIKI, Tiingo, Yahoo or unfillable)."""
    holders, fetched, runs = holders or {}, fetched or {}, runs or {}
    qmap = q_suffix_map(index)
    rows = []
    for sid in sorted(set(hits) | set(vsample)):
        row = facts.loc[sid]
        info = security_info(row)
        reasons = sorted(hits.get(sid, {}), key=REASON_PRIORITY.index)
        base = {"security_id": sid, "cik": row.cik, "name": row["name"], "active": "Y" if row.active else ("successor" if row.via_successor else "N"),
                "delist_date": row.delist_date or "", "uncovered_weeks": int(row.uncovered_weeks),
                "best_rank": "" if pd.isna(row.best_rank) else int(row.best_rank), "v_category": vsample.get(sid, ""),
                "tiingo_range_match": "", "tiingo_row_start": "", "tiingo_row_end": "", "tiingo_coverage": "",
                "tiingo_reused_ticker": "", "tiingo_flags": "", "note": ""}

        def tiingo_fields(match: dict, start: str) -> dict:
            return tiingo_match_fields(match, sid, row.cik, start, index, holders, fetched)

        def best_match(candidates: list, start: str, end: str, info: dict = info) -> dict:
            return best_tiingo_match(sid, candidates, start, end, index, info, fetched)

        pieces = []
        for part_reasons, part, start, end, part_note in relisted_parts(row, reasons, hits.get(sid, {})):
            metric = hits[sid][part_reasons[0]]
            if part_reasons[0] == "C_late_start" and len(metric) > 2:
                start = min(start, metric[2][0])
            first_new = str(getattr(row, "relist_first_session", "") or "")
            relist = str(getattr(row, "relisted_from", "") or "")
            for low, high, gap_note in split_at_listing_gaps(start, end, runs.get(sid)):
                # The piece of a new-equity relisting starts with the new shares, as in ``relisted_parts`` (Capstone:
                # listed again 2026-07-02, step 9's first new session 2026-07-08).
                if (gap_note and part.active and str(getattr(row, "relist_new_equity", "") or "") and first_new
                        and relist and low < first_new <= high and low >= _shift(relist, -WARMUP_DAYS)):
                    low = first_new
                    gap_note += f"; new equity: from its first session {first_new}"
                pieces.append((part_reasons, part, low, high, _join_notes(part_note, gap_note)))
        for part_reasons, part, start, end, part_note in pieces:
            primary = part_reasons[0]
            metric = hits[sid][primary]
            out = {**base, "needed_start": start, "needed_end": end, "reason": primary,
                   "reasons_all": " ".join(part_reasons), "priority": REASON_PRIORITY.index(primary) + 1,
                   "prefilter_metric": metric[0], "prefilter_value": metric[1],
                   "active": "Y" if part.active else ("successor" if part.via_successor else "N"), "note": part_note}
            month = MONTH_2 if primary in MONTH_2_REASONS else MONTH_1
            alternative = "" if part.active else wiki_alternative(sid, part, start, end, lists)
            if part.active:
                out.update(planned_source="yahoo", ticker_for_source=yahoo_ticker(row), status="pending",
                           fetch_month=MONTH_1)
                # Only new equity (a bankruptcy or a share exchange) is a junction, on the new shares' first
                # session (round 7: step 9's RELIST_JUNCTIONS, WW 2025-06-27, CORZ 2024-01-24). The same shares
                # listed again (SMCI, SIGA, SCOR, MDXG) keep one series: no junction, so no return is dropped.
                first_new = str(getattr(row, "relist_first_session", "") or "")
                if str(getattr(row, "relist_new_equity", "") or "") and first_new and start <= first_new <= end:
                    out["junction_date"] = first_new
            elif part.via_successor:
                heir = facts.loc[part.via_successor]
                note = f"history under successor {part.via_successor} (step-4 link)"
                if heir.foreign_filer in ("Y", "MIXED"):
                    note += (f"; the successor is a foreign filer ({heir.foreign_filer}): use the series only for "
                             f"this security's own window {start}..{end}")
                out.update(planned_source="yahoo", ticker_for_source=yahoo_ticker(heir), status="pending",
                           fetch_month=MONTH_1, note=_join_notes(part_note, note))
            elif alternative and end <= WIKI_END:
                out.update(planned_source="wiki", ticker_for_source=alternative, status="cached", fetch_month="",
                           note=_join_notes(part_note, "WIKI file under a current SEC ticker of the CIK; LastSale level "
                                                       "check passed"))
            else:
                match = best_match(tiingo_tickers(part, start, sid, qmap), start, end, security_info(part))
                out.update(tiingo_fields(match, start))
                if useful_partial(match, start):
                    status = ("conditional_tier_c" if primary == "B_C_rest_300M_500M"
                              else "pending_month2" if month == MONTH_2 else "pending")
                    out.update(planned_source="tiingo", status=status, fetch_month=month)
                else:
                    newer = match["match"] in ("wrong_entity", "hidden", "newer_company", "fetched_wrong_entity") or (
                        match["match"] == "partial" and _days(match["row_start"], start) < -TIINGO_START_SLACK_DAYS)
                    out.update(planned_source="unfillable", fetch_month="", status="wrong_entity" if newer else "no_data",
                               note=_join_notes(part_note, match_note(match)))
            rows.append(out)
        if sid in vsample:
            start = max(WINDOW_START, str(row.first_listed)[:10])
            ticker = yahoo_ticker(row)
            # The Yahoo ticker whenever Tiingo serves a row for it (PRPL, not the SPAC's GPAC; OXLC, not
            # the notes' OXLCN), else the best of the security's other tickers.
            match = tiingo_range_match([(ticker, "own")], start, WINDOW_END, index, info)
            if match["match"] not in ("Y", "partial"):
                match = best_match([(ticker, "own")] + [c for c in tiingo_tickers(row, start, sid, qmap) if c[0] != ticker],
                                   start, WINDOW_END)
            usable = match["match"] in ("Y", "partial")
            rows.append({**base, "needed_start": start, "needed_end": WINDOW_END, "reason": "V_verify_sample",
                         "reasons_all": " ".join(["V_verify_sample"] + reasons), "priority": REASON_PRIORITY.index("V_verify_sample") + 1,
                         "prefilter_metric": "v_category", "prefilter_value": vsample[sid],
                         "planned_source": "tiingo" if usable else "unfillable",
                         "status": "pending" if usable else "wrong_entity", "fetch_month": MONTH_1 if usable else "",
                         **tiingo_fields(match, start), "ticker_for_source": match["ticker"] or ticker,
                         "note": "verification: Tiingo next to the Yahoo series" if usable else match_note(match)})
    frame = pd.DataFrame(rows).reindex(columns=CANDIDATE_COLUMNS)
    return mark_shared(frame)


def seeded_order(ids: list[str], seed: int = SEED) -> list[str]:
    """``ids`` in a seeded random order (the first n are ``seeded_sample(ids, n)``'s draw)."""
    ids = sorted(ids)
    rng = np.random.default_rng(seed)
    return rng.choice(ids, size=len(ids), replace=False).tolist() if ids else []


def refill_tier_c_sample(frame: pd.DataFrame, n: int = TIER_C_SAMPLE,
                         keep: set[str] | frozenset = frozenset()) -> tuple[pd.DataFrame, dict]:
    """The tier-C sample as the first ``n`` names, in the seeded order of the whole tier-C pool (rows
    whose primary reason is the sample or the rest), that some planned request can price: a sampled
    name no free source has cannot show whether tier C reaches rank 300 (round 1: 5 of 20). The
    others become ``B_C_rest`` (conditional on the sample, or unfillable as before). ``keep``: the sample
    already drawn (from the candidate list on disk); its names stay first while they are in the pool and
    fetchable, and only freed places are filled in the seeded order."""
    frame = frame.copy()
    reasons = ("B_C_sample_300M_500M", "B_C_rest_300M_500M")
    pool = frame[frame["reason"].isin(reasons)]
    fetchable = set(pool.loc[pool["planned_source"].isin(["tiingo", "wiki"]), "security_id"])
    # one place per security (round 9: a need split at a listing gap gives a security two rows)
    order = [sid for sid in seeded_order(list(dict.fromkeys(pool["security_id"]))) if sid in fetchable]
    kept = [sid for sid in order if sid in set(keep)][:n]
    chosen = kept + [sid for sid in order if sid not in set(kept)][:n - len(kept)]
    swapped = 0
    for k in pool.index:
        sid, was = frame.at[k, "security_id"], frame.at[k, "reason"]
        now = reasons[0] if sid in chosen else reasons[1]
        if now == was:
            continue
        swapped += 1
        frame.at[k, "reason"] = now
        frame.at[k, "priority"] = REASON_PRIORITY.index(now) + 1
        frame.at[k, "reasons_all"] = " ".join(now if r in reasons else r for r in str(frame.at[k, "reasons_all"]).split())
        if frame.at[k, "planned_source"] == "tiingo":
            status, month = ("pending", MONTH_1) if now == reasons[0] else ("conditional_tier_c", MONTH_2)
            frame.at[k, "status"], frame.at[k, "fetch_month"] = status, month
    unfetchable = sorted(set(pool["security_id"]) - fetchable)
    return frame, {"pool": int(pool["security_id"].nunique()), "sample": len(chosen), "kept_from_previous": len(kept),
                   "rows_swapped": swapped,
                   "pool_unfetchable": len(unfetchable), "seed": SEED}


def mark_shared(frame: pd.DataFrame) -> pd.DataFrame:
    """Flag a Tiingo ticker that serves two or more securities (old and new Caesars on CZR, four
    Qurate tracking stocks on QVCAQ), and set ``tiingo_reused_ticker`` and the note for every
    ambiguous match: the fetcher then confirms the entity of each row after the answer."""
    frame = frame.copy()
    tiingo = frame["planned_source"] == "tiingo"
    users = frame[tiingo].groupby("ticker_for_source")["security_id"].agg(lambda s: sorted(set(s)))
    for k in frame.index[tiingo]:
        mine = users.get(frame.at[k, "ticker_for_source"], [])
        # (idempotent: a second call, after rows were added, replaces what the first one wrote)
        flags = [f for f in str(frame.at[k, "tiingo_flags"] or "").split() if not f.startswith("shared:")]
        if len(mine) > 1:
            flags.append("shared:" + "/".join(s for s in mine if s != frame.at[k, "security_id"]))
        frame.at[k, "tiingo_flags"] = " ".join(flags)
        ambiguous = [f for f in flags if f.startswith(AMBIGUOUS_FLAGS)]
        frame.at[k, "tiingo_reused_ticker"] = "Y" if ambiguous else ""
        note = AMBIGUOUS_NOTE.sub("", str(frame.at[k, "note"] or "") if pd.notna(frame.at[k, "note"]) else "")
        if ambiguous:
            text = "ambiguous Tiingo ticker (" + ", ".join(f.split(":")[0] for f in ambiguous) + "): the fetcher confirms the entity"
            note = f"{note}; {text}" if note else text
        frame.at[k, "note"] = note
    return frame


AMBIGUOUS_NOTE = re.compile(r"(; )?ambiguous Tiingo ticker \([^)]*\): the fetcher confirms the entity")


# ------------------------------------------------------------------ stage 6b: Yahoo answers fed back

YAHOO_REPORT = common.CACHE / "yahoo" / "entity_report.csv"
YAHOO_STATUS = {"ok": "done", "review": "done_review", "partial": "partial",
                "failed": "yahoo_failed", "no_rows": "yahoo_failed", "wrong_entity": "yahoo_failed"}
YAHOO_FAILED = {"failed", "no_rows", "wrong_entity"}
VERDICT_ORDER = ["failed", "no_rows", "wrong_entity", "partial", "review", "ok"]  # worst first
GAP_DAYS = 30  # a gap a partial series leaves at either end is reported from this length


def yahoo_answers(path: Path = YAHOO_REPORT) -> dict[tuple[str, str], list[dict]]:
    """(security_id, candidate ticker) -> the Yahoo build's entity-report rows for it (one per symbol or
    segment): symbol, verdict, need, first and last rows, need coverage, reasons."""
    if not Path(path).exists():
        return {}
    report = read_csv_text(path)
    out = defaultdict(list)
    for r in report.to_dict("records"):
        key = (str(r["security_id"]), str(r.get("candidate_symbol") or r.get("symbol", "")).upper())
        out[key].append({"symbol": r.get("symbol", ""), "verdict": r.get("verdict", ""),
                         "needed_start": r.get("needed_start", ""), "needed_end": r.get("needed_end", ""),
                         "first_row": r.get("first_row", ""), "last_row": r.get("last_row", ""),
                         "need_coverage": pd.to_numeric(r.get("need_coverage", ""), errors="coerce"),
                         "reasons": str(r.get("verdict_reasons", ""))[:200],
                         "verdict_reasons": str(r.get("verdict_reasons", "")),
                         "missing_inside": pd.to_numeric(r.get("missing_inside", ""), errors="coerce"),
                         "first_trade": r.get("yahoo_first_trade", "")})
    return dict(out)


# The Yahoo build judges an answer on the request's need, the union of the security's Yahoo rows for the symbol,
# and compares Yahoo's first trade with the security's first listing. Round 9: each candidate row is judged on its
# own need. A ``first_trade_<day>_after_listing_<day>`` review item is not one for a row whose own start (its
# ``junction_date``, else its ``needed_start``) is within FIRST_TRADE_SLACK_DAYS of that first trade: the row asks
# for the new shares or a later listing on purpose (WW 2025-06-27, CORZ 2024-01-24, THRY, OPI). A ``partial``
# answer covers a row whose own need is shorter than the request's when its first and last rows reach the row's
# start and end within COVER_SLACK_DAYS and no session is missing between them (a need split at a listing gap:
# CEPL from 2026-07-02, the answer from 2026-07-08). Only these items and the build's notes may stand in the way;
# any other review item keeps the verdict.
FIRST_TRADE_SLACK_DAYS, COVER_SLACK_DAYS = 30, 7  # as the Yahoo build's
FIRST_TRADE_ITEM = re.compile(r"^first_trade_(\d{4}-\d{2}-\d{2})_after_listing_(\d{4}-\d{2}-\d{2})$")
YAHOO_NOTE_ITEM = re.compile(r"^(need_starts_before_first_trade|split_not_applied_by_yahoo|volume_restore_unverified|"
                             r"volume_not_scaled_by_yahoo):")
YAHOO_COVER_ITEM = re.compile(r"^(starts|ends) \d{4}-\d{2}-\d{2}\b")
YAHOO_REVIEW_ITEM = re.compile(r"^(level_offset_(lastsale|wiki):|wiki_short_disagrees:|name_mismatch|stored_dv_ratio:|"
                               r"first_trade_\d{4}-\d{2}-\d{2}_after_listing_|(segment|trim)_junction_event:|"
                               r"relist_junction:|level_jump_not_a_split:)")


def row_verdict(answer: dict, start: str, end: str, junction: str = "") -> tuple[str, str]:
    """(the verdict of a Yahoo answer for one candidate row's own need [start, end], a note when it differs
    from the build's): see FIRST_TRADE_ITEM above. ``junction``: the row's ``junction_date``, its own start when
    given. Other review items stand (OPI keeps its ``relist_junction`` review)."""
    verdict = str(answer.get("verdict", ""))
    if verdict not in ("review", "partial"):
        return verdict, ""
    own_start = junction or start
    items = [i.strip() for i in str(answer.get("verdict_reasons", "") or "").split("; ") if i.strip()]
    met = []
    for item in items:
        m = FIRST_TRADE_ITEM.match(item)
        if m and _days(own_start, m.group(1)) <= FIRST_TRADE_SLACK_DAYS:
            met.append((item, m.group(1), m.group(2)))
    rest = [i for i in items if i not in {m[0] for m in met}]
    first_note = "; ".join(f"Yahoo's first trade {d} is no later than {FIRST_TRADE_SLACK_DAYS} days after this row's own "
                           f"start {own_start} (the build compared it with the first listing {l})" for _, d, l in met)
    if verdict == "review":
        if met and all(YAHOO_NOTE_ITEM.match(i) for i in rest):
            return "ok", first_note
        return verdict, ""
    if (str(answer.get("needed_start", "")), str(answer.get("needed_end", ""))) == (start, end):
        return verdict, ""
    missing, first, last = answer.get("missing_inside"), str(answer.get("first_row", "") or ""), str(answer.get("last_row", "") or "")
    if not (pd.notna(missing) and int(missing) == 0 and first and last):
        return verdict, ""
    if _days(start, first) > COVER_SLACK_DAYS or _days(last, min(end, WINDOW_END)) > COVER_SLACK_DAYS:
        return verdict, ""
    rest = [i for i in rest if not YAHOO_COVER_ITEM.match(i)]
    if not all(YAHOO_NOTE_ITEM.match(i) or YAHOO_REVIEW_ITEM.match(i) for i in rest):
        return verdict, ""
    note = _join_notes(f"Yahoo's rows {first}..{last} cover this row's own need {start}..{end} (the build's partial "
                       f"is on its request {answer.get('needed_start')}..{answer.get('needed_end')})", first_note)
    return ("review" if any(YAHOO_REVIEW_ITEM.match(i) for i in rest) else "ok"), note


def answer_coverage(answer: dict, start: str, end: str, sessions: pd.DatetimeIndex) -> float:
    """The share of the need [start, end] a Yahoo answer covers: 0 for a failed one; the report's own
    figure when its need is this one, else the need sessions between its first and last row."""
    if answer["verdict"] in YAHOO_FAILED or not answer["first_row"]:
        return 0.0
    if (answer["needed_start"], answer["needed_end"]) == (start, end) and pd.notna(answer["need_coverage"]):
        return float(answer["need_coverage"])
    need = sessions[(sessions >= pd.Timestamp(start)) & (sessions <= pd.Timestamp(min(end, WINDOW_END)))]
    if not len(need):
        return 1.0
    inside = (need >= pd.Timestamp(answer["first_row"])) & (need <= pd.Timestamp(answer["last_row"]))
    return round(float(inside.mean()), 4)


def uncovered_gaps(answers: list[dict], start: str, end: str) -> list[tuple[str, str]]:
    """The parts of [start, end] outside every accepted answer's first..last rows, of GAP_DAYS or more
    (the whole need when no answer has a row in it)."""
    rows = [(a["first_row"], a["last_row"]) for a in answers
            if a["verdict"] not in YAHOO_FAILED and a["first_row"] and a["first_row"] <= end and a["last_row"] >= start]
    if not rows:
        return [(start, end)]
    first, last = min(r[0] for r in rows), max(r[1] for r in rows)
    gaps = []
    if _days(start, first) >= GAP_DAYS:
        gaps.append((start, _shift(first, -1)))
    if _days(last, end) >= GAP_DAYS:
        gaps.append((_shift(last, 1), end))
    return gaps


def yahoo_fallback(candidates: pd.DataFrame, facts: pd.DataFrame, answers: dict, index: dict,
                   holders: dict | None = None, fetched: dict | None = None,
                   sessions: pd.DatetimeIndex | None = None) -> tuple[pd.DataFrame, dict]:
    """Plan 3.2 ("Tiingo ... only if Yahoo fails"), round 6: the Yahoo answers written back to the Yahoo
    rows (status done / done_review / partial / yahoo_failed, the first and last row, the coverage of the
    row's need; pending without an answer), and for a row whose answer failed (failed, no_rows,
    wrong_entity) or covers less than MIN_PARTIAL_COVERAGE of its need, a second row for the uncovered
    span, ``fallback_from`` yahoo_<verdict>:
    - Tiingo when ``tiingo_range_match`` (old and SEC current tickers, the fetcher's verdicts) finds a
      useful row: ``pending_month2`` for month 2 (round 7: not ``deferred_quota``, a status only the budget
      and the fetcher give; ``MONTH_2_STATUSES`` is what the month-2 command selects);
    - Tiingo, flagged ``ask_shadowed``, when the covering row is hidden by the row the API serves for that
      ticker and the ticker is the security's own SEC current ticker (Wolfspeed: the old WOLF row
      1993-02-09..2025-09-26 under the post-bankruptcy NYSE row; GPOR's trial answered only its served
      row, so the fetcher prechecks it unless asked with --fetch-shadowed): ``pending_month2`` too;
    - else ``unfillable`` (wrong_entity or no_data, as ``route`` does), which ``unfillable_rows`` lists.
    The V sample's Tiingo rows are not touched. Returns (candidates, counts)."""
    holders, fetched = holders or {}, fetched or {}
    sessions = sessions if sessions is not None else xnas_sessions()
    qmap = q_suffix_map(index)
    frame = candidates.copy()
    for column in ("yahoo_row_start", "yahoo_row_end", "yahoo_coverage", "fallback_from", "junction_date", "status"):
        if column not in frame:
            frame[column] = ""
        frame[column] = frame[column].astype(object).where(frame[column].notna(), "")
    frame = frame[frame["fallback_from"].astype(str).eq("")].copy()  # rebuilt here every run
    added, counts = [], Counter()
    for k in frame.index[frame["planned_source"] == "yahoo"]:
        row = frame.loc[k]
        sid, start, end = row["security_id"], row["needed_start"], row["needed_end"]
        mine = answers.get((sid, str(row["ticker_for_source"]).upper()), [])
        if not mine:
            frame.loc[k, "status"] = "pending"
            counts["yahoo_pending"] += 1
            continue
        # round 9: each answer judged on this row's own need (``row_verdict``)
        judged, own_notes = [], []
        for a in mine:
            verdict_here, why_here = row_verdict(a, start, end, str(row.get("junction_date", "") or ""))
            if why_here:
                own_notes.append(why_here)
                counts["yahoo_verdict_on_own_need:" + f"{a['verdict']}->{verdict_here}"] += 1
            judged.append({**a, "verdict": verdict_here})
        mine = judged
        if own_notes:
            note = row.get("note", "")
            frame.loc[k, "note"] = _join_notes(note if isinstance(note, str) else "", *own_notes)
        worst = min(mine, key=lambda a: VERDICT_ORDER.index(a["verdict"]) if a["verdict"] in VERDICT_ORDER else 0)
        coverage = min(answer_coverage(a, start, end, sessions) for a in mine) if len(mine) == 1 else round(
            float(np.mean([answer_coverage(a, start, end, sessions) for a in mine])), 4)
        first = min((a["first_row"] for a in mine if a["first_row"]), default="")
        last = max((a["last_row"] for a in mine if a["last_row"]), default="")
        verdict = worst["verdict"]
        frame.loc[k, ["status", "yahoo_row_start", "yahoo_row_end", "yahoo_coverage"]] = [
            YAHOO_STATUS.get(verdict, "yahoo_failed"), first, last, coverage]
        row = frame.loc[k]
        counts[f"yahoo_{YAHOO_STATUS.get(verdict, 'yahoo_failed')}"] += 1
        poor = verdict in YAHOO_FAILED or (verdict == "partial" and coverage < MIN_PARTIAL_COVERAGE)
        if not poor or row["reason"] == "V_verify_sample":
            continue
        gaps = uncovered_gaps(mine, start, end)
        if not gaps:
            continue
        span_start, span_end = gaps[0][0], gaps[-1][1]
        fact = facts.loc[sid]
        part = fact.copy()
        if str(row.get("active", "")) == "successor":
            part["active"] = False
        info = security_info(part)
        tickers = tiingo_tickers(part, span_start, sid, qmap)
        match = best_tiingo_match(sid, tickers, span_start, span_end, index, info, fetched)
        why = (f"Yahoo {worst['symbol'] or row['ticker_for_source']} answered {verdict}"
               + (f" ({coverage:.1%} of the need)" if verdict == "partial" else "")
               + f"; the uncovered span {span_start}..{span_end} is planned here")
        out = {**row.to_dict(), "needed_start": span_start, "needed_end": span_end, "fallback_from": f"yahoo_{verdict}",
               "junction_date": "", "fetch_order": "", "v_category": "",
               **tiingo_match_fields(match, sid, fact.cik, span_start, index, holders, fetched)}
        served = match.get("hidden_by") or {}
        own_later = (match["match"] == "hidden" and match["ticker"] in str(fact.tickers_sec_current).split()
                     and served.get("start", "") >= str(match["row_end"]))
        if useful_partial(match, span_start):
            out.update(planned_source="tiingo", status=FALLBACK_STATUS, fetch_month=MONTH_2,
                       note=_join_notes(why, "Tiingo for month 2"))
            counts["fallback_tiingo"] += 1
        elif own_later:
            out.update(planned_source="tiingo", status=FALLBACK_STATUS, fetch_month=MONTH_2,
                       tiingo_flags=" ".join(filter(None, [out["tiingo_flags"], "ask_shadowed"])),
                       note=_join_notes(why, (
                           f"Tiingo row {match['row_start']}..{match['row_end']} of {match['ticker']} covers it, but the "
                           f"API serves the row {served.get('start')}..{served.get('end')} ({served.get('exchange') or 'no exchange'}), "
                           f"this security's own later equity (its SEC current ticker); asked only on purpose "
                           f"(--fetch-shadowed): Tiingo may answer with the served row only, as for GPOR")))
            counts["fallback_tiingo_ask_shadowed"] += 1
        else:
            newer = match["match"] in ("wrong_entity", "hidden", "newer_company", "fetched_wrong_entity") or (
                match["match"] == "partial" and _days(match["row_start"], span_start) < -TIINGO_START_SLACK_DAYS)
            out.update(planned_source="unfillable", fetch_month="", status="wrong_entity" if newer else "no_data",
                       note=_join_notes(why, match_note(match) or "no Tiingo row covers it"))
            counts["fallback_unfillable"] += 1
        added.append(out)
    if added:
        frame = pd.concat([frame, pd.DataFrame(added)], ignore_index=True)
    frame = frame.reindex(columns=CANDIDATE_COLUMNS)
    return mark_shared(frame), dict(counts)


# ------------------------------------------------------------------ stage 8: the Tiingo month-2 plan

STEP12_LISTED = common.CACHE / "universe" / "weekly_listed.csv.gz"
MONTH2_PLAN = OUT / "tiingo_month2_plan.csv"
PLAN_CUT = 500  # the free tier's unique symbols a month (the fetcher itself stops at TIINGO_MONTH_STOP)
TIINGO_FINAL = {"done", "done_review", "partial", "wrong_entity", "no_data", "no_data_in_window", "refused"}
TIINGO_RETRY = {"deferred_quota", "error"}  # the fetcher's own statuses it asks again (its RETRY)
PLAN_COLUMNS = ["plan_rank", "security_id", "ticker_for_source", "group", "reason", "needed_start", "needed_end",
                "status", "fetch_month", "fetcher_status", "fetch_selectable", "tiingo_range_match", "tiingo_row_start",
                "tiingo_row_end", "tiingo_flags", "expected_top250_weeks", "proxy_not_counted_weeks", "unknown_weeks",
                "missing_weeks", "step6_weeks_rank250", "new_symbol", "cum_new_symbols", f"within_{PLAN_CUT}",
                f"within_{TIINGO_MONTH_STOP}", "name", "cik", "delist_date", "note"]
SHADOWED_MATCHES = {"hidden", "newer_company", "fetched_wrong_entity"}  # as the fetcher's (asked only on purpose)


def tiingo_cached(ticker: str, raw_dir: Path = common.RAW / "tiingo") -> bool:
    """Whether the fetcher holds an answer for ``ticker`` (asking it again costs no symbol)."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(ticker).upper())  # the fetcher's safe_name
    return any(Path(raw_dir).glob(f"{safe}__*.json.gz")) or any(Path(raw_dir).glob(f"{safe}__*.json.gz.404"))


def week_evidence(listed_path: Path = STEP12_LISTED) -> pd.DataFrame:
    """Step 12's missing name-weeks with their expected top-250 membership (``p_top250``), whether the week
    has no evidence at all (``unknown``) and whether its only evidence is a market-cap or float proxy
    (``proxy``): week_end, security_id, p_top250, unknown, proxy."""
    columns = ["week_end", "security_id", "p_top250", "unknown", "proxy"]
    if not Path(listed_path).exists():
        return pd.DataFrame(columns=columns)
    frame = pd.read_csv(listed_path, usecols=["week_end", "security_id", "missing", "evidence", "p_top250"],
                        dtype={"security_id": str, "evidence": str})
    frame = frame[frame["missing"].astype(str).isin(["True", "true", "1"])]
    return pd.DataFrame({"week_end": pd.to_datetime(frame["week_end"]), "security_id": frame["security_id"],
                         "p_top250": pd.to_numeric(frame["p_top250"], errors="coerce").fillna(0.0),
                         "unknown": frame["evidence"].eq("unknown"), "proxy": frame["evidence"].eq("proxy")},
                        columns=columns)


def class_securities(master: pd.DataFrame) -> set[str]:
    """Classes of a multi-class company and tracking stocks (``multi_class_group``, or a ``.T-`` id): their
    company-list market cap and XBRL float are the company's, not the class's (DISCB, LMCB)."""
    group = master["multi_class_group"].fillna("") if "multi_class_group" in master else pd.Series("", index=master.index)
    return set(master.loc[group.astype(str) != "", "security_id"]) | {s for s in master["security_id"] if ".T-" in str(s)}


# A float of $10B or more that no close could test (``float_price_check`` needs one within 90 days) and that is this
# many times the security's own checked Form 25 float is not taken as its size in the month-2 plan (round 7: DIRTT's
# $17.07B XBRL float of 2022-23, 47x its $365M Form 25 float, under the 50x rule; 63 of its 65 expected weeks).
DOUBTFUL_FLOAT_RATIO = 20.0


def doubtful_float_weeks(weekly: pd.DataFrame, facts: pd.DataFrame) -> set[tuple[str, pd.Timestamp]]:
    """(security, week end) whose only size proxy is such an untested float (see DOUBTFUL_FLOAT_RATIO)."""
    need = {"security_id", "week_end", "mcap", "float_usd", "close"}
    if not len(weekly) or not need <= set(weekly.columns) or "public_float_usd" not in facts:
        return set()
    flag = facts["float_check_flag"] if "float_check_flag" in facts else pd.Series("ok", index=facts.index)
    form25 = pd.to_numeric(facts["public_float_usd"], errors="coerce").where(flag.astype(str) == "ok")
    form25 = form25[form25 > 0]
    rows = weekly[weekly["mcap"].isna() & weekly["close"].isna() & (weekly["float_usd"] >= PRICE_CHECK_FLOAT)
                  & weekly["security_id"].isin(form25.index)]
    rows = rows[rows["float_usd"] >= DOUBTFUL_FLOAT_RATIO * rows["security_id"].map(form25)]
    return set(zip(rows["security_id"], rows["week_end"]))


def dropped_float_weeks(weekly: pd.DataFrame, master: pd.DataFrame, ranks: pd.DataFrame, floats_before: pd.DataFrame,
                        dropped: list[dict]) -> set[tuple[str, pd.Timestamp]]:
    """(security, week end) whose float proxy would be a fact ``float_price_check`` dropped (``floats_before`` is
    the table it was given), in a week with no market cap: step 12's published weeks may still carry that proxy
    (round 9: DIRTT's $17.07B is now left out of step 6's own weekly table, so ``doubtful_float_weeks`` no longer
    sees it there)."""
    if not dropped or not len(weekly):
        return set()
    ciks = {int(d["cik"]) for d in dropped}
    values = {float(d["float_usd"]) for d in dropped}
    cik_of = dict(zip(master["security_id"], master["cik"].astype(int)))
    rows = weekly[weekly["security_id"].map(cik_of).isin(ciks)]
    if not len(rows):
        return set()
    caps = ranks[ranks["security_id"].isin(set(rows["security_id"]))]
    got = attach_proxies(rows[["security_id", "week_end"]].reset_index(drop=True), master, caps,
                         floats_before[floats_before["cik"].astype(int).isin(ciks)])
    hit = got["float_usd"].isin(values).to_numpy() & got["mcap"].isna().to_numpy()
    return set(zip(got.loc[hit, "security_id"], got.loc[hit, "week_end"]))


def plan_evidence(evidence: pd.DataFrame, classes: set[str] = frozenset(), investment: dict | None = None,
                  doubtful: set | frozenset = frozenset()) -> tuple[pd.DataFrame, dict]:
    """Step 12's missing weeks as the month-2 plan counts them (round 7):
    - D6: weeks in an investment-company span are not universe weeks, so they are dropped (step 12's
      published file may predate D6);
    - share-class level: for a class of a multi-class company or a tracking stock (``class_securities``), a week
      whose only evidence is the market-cap / float proxy expects nothing (``p_top250`` 0, ``class_proxy``):
      that proxy is the whole company's ($10.7B for Discovery's thinly traded DISCB), not the class's. Weeks with
      the class's own dollar volume keep step 12's estimate;
    - a proxy week whose float no close could test and that dwarfs the security's Form 25 float (``doubtful``,
      ``doubtful_float_weeks``) expects nothing either (``float_doubtful``)."""
    evidence = evidence.copy()
    for column, default in (("proxy", False), ("unknown", False)):
        if column not in evidence:
            evidence[column] = default
    investment = investment or {}
    days = evidence["week_end"].dt.strftime("%Y-%m-%d") if len(evidence) else pd.Series([], dtype=str)
    ic = np.array([is_foreign_on(investment.get(s), d) for s, d in zip(evidence["security_id"], days)], dtype=bool)
    facts = {"investment_company_weeks_dropped": int(ic.sum()),
             "investment_company_expected_dropped": round(float(evidence.loc[ic, "p_top250"].sum()), 1)}
    evidence = evidence[~ic].copy()
    cls = evidence["security_id"].isin(set(classes)) & evidence["proxy"].astype(bool)
    facts.update(class_proxy_weeks=int(cls.sum()), class_proxy_expected_zeroed=round(float(evidence.loc[cls, "p_top250"].sum()), 1),
                 class_proxy_securities=int(evidence.loc[cls, "security_id"].nunique()))
    evidence["class_proxy"] = cls
    evidence.loc[cls, "p_top250"] = 0.0
    keys = pd.Series(list(zip(evidence["security_id"], evidence["week_end"])), index=evidence.index, dtype=object)
    odd = evidence["proxy"].astype(bool) & keys.isin(set(doubtful)) if len(evidence) else evidence["proxy"].astype(bool)
    facts.update(float_doubtful_weeks=int(odd.sum()),
                 float_doubtful_expected_zeroed=round(float(evidence.loc[odd, "p_top250"].sum()), 1),
                 float_doubtful_securities=sorted(evidence.loc[odd, "security_id"].unique().tolist()))
    evidence["float_doubtful"] = odd
    evidence.loc[odd, "p_top250"] = 0.0
    return evidence.reset_index(drop=True), facts


# Plan 3.2 B-C: the 20-name sample decides whether the rest of tier C is fetched in month 2 ("if any one reaches
# rank <= 300 in any week, fetch all of tier C"). The sample's Tiingo answers are ranked by their own dv50 (median
# raw close x raw volume over 50 sessions, 25 at least; raw close >= $10 that week) against step 6's priced universe
# names that week: dollar-volume ranks only, no return.
TIER_C_SAMPLE_REASON = "B_C_sample_300M_500M"
TIER_C_REST_REASON = "B_C_rest_300M_500M"


def answer_dv50(path: str, sessions: pd.DatetimeIndex) -> tuple[pd.Series, pd.Series]:
    """(dv50, close carried up to CLOSE_STALE_SESSIONS) on every session, from one Tiingo answer file."""
    rows = pd.read_csv(path, usecols=["date", "close", "volume"], parse_dates=["date"])
    rows = rows.drop_duplicates("date").set_index("date").reindex(sessions)
    dv = rows["close"] * rows["volume"]
    window, minimum = 50, DV_WINDOWS[50]
    return dv.rolling(window, min_periods=minimum).median(), rows["close"].ffill(limit=CLOSE_STALE_SESSIONS)


def tier_c_sample_result(candidates: pd.DataFrame, fetch_status: pd.DataFrame, weekly: pd.DataFrame,
                         sessions: pd.DatetimeIndex | None = None, cut: int = FETCH_RANK) -> dict:
    """Whether the B-C sample reached dv50 rank <= ``cut`` in a universe week of its need: ``triggered`` (one
    did: the rest is fetched in month 2), ``not_triggered`` (every sampled name answered or final, none did),
    ``undecided`` (some still unanswered). Per name: the fetcher's status and its best rank and week."""
    sessions = sessions if sessions is not None else xnas_sessions()
    sample = candidates[(candidates["reason"] == TIER_C_SAMPLE_REASON) & (candidates["planned_source"] == "tiingo")]
    status = {}
    if len(fetch_status):
        for r in fetch_status.to_dict("records"):
            status[(str(r["security_id"]), str(r["ticker_for_source"]))] = r
    universe = weekly[weekly["universe"] & weekly["price_ge_10"].isin(["Y", "U"]) & weekly["dv50"].notna()]
    by_week = {w: g for w, g in universe.groupby("week_end")}
    names, answered, open_rows = {}, 0, 0
    for r in sample.to_dict("records"):
        sid, ticker = str(r["security_id"]), str(r["ticker_for_source"])
        mine = status.get((sid, ticker), {})
        state = str(mine.get("status", "") or "")
        entry = {"ticker": ticker, "fetcher_status": state or "not_asked", "best_dv50_rank": None, "week": ""}
        path = str(mine.get("prices_path", "") or "")
        if state in FETCHED_OK and path and Path(path).exists():
            answered += 1
            dv50, close = answer_dv50(path, sessions)
            own = weekly[(weekly["security_id"] == sid) & weekly["universe"] & ~weekly["outside_trading"].astype(bool)
                         & weekly["week_end"].between(pd.Timestamp(r["needed_start"]), pd.Timestamp(r["needed_end"]))]
            best = None
            for week in own["week_end"]:
                value, price = dv50.get(week, np.nan), close.get(week, np.nan)
                if not (value > 0 and price >= MIN_PRICE) or week not in by_week:
                    continue
                others = by_week[week]
                rank = 1 + int((others.loc[others["security_id"] != sid, "dv50"].to_numpy() > value).sum())
                if best is None or rank < best[0]:
                    best = (rank, week.strftime("%Y-%m-%d"))
            if best:
                entry["best_dv50_rank"], entry["week"] = best
        elif state in TIINGO_FINAL:
            answered += 1  # final without usable rows (wrong_entity, no_data ...)
        else:
            open_rows += 1
        names[sid] = entry
    hit = sorted(s for s, e in names.items() if e["best_dv50_rank"] is not None and e["best_dv50_rank"] <= cut)
    result = "triggered" if hit else ("not_triggered" if names and not open_rows else "undecided")
    return {"result": result, "cut_rank": cut, "sample_rows": int(len(sample)), "answered": answered,
            "unanswered": open_rows, "names_reaching_cut": {s: names[s] for s in hit}, "names": names,
            "rule": "plan 3.2 B-C: any sampled name with a dv50 rank <= 300 in a universe week of its need (its own "
                    "Tiingo answer ranked against step 6's priced universe that week, raw close >= $10) triggers the "
                    "whole tier C for month 2"}


def apply_tier_c_result(candidates: pd.DataFrame, result: dict) -> pd.DataFrame:
    """When the sample did not trigger tier C, its rest is not fetched: ``conditional_tier_c`` rows become
    ``tier_c_not_triggered`` (no month). Triggered or undecided: unchanged (the month-2 command selects
    ``conditional_tier_c``)."""
    frame = candidates.copy()
    if result.get("result") == "not_triggered":
        rest = frame["status"] == "conditional_tier_c"
        frame.loc[rest, ["status", "fetch_month"]] = ["tier_c_not_triggered", ""]
    return frame


def fetch_selectable(row: dict, fetcher_status: str) -> str:
    """How the Tiingo fetcher's month-2 run (``--month2``: MONTH_2_STATUSES) treats a plan row: ``Y`` (selected and
    asked), ``fetch_shadowed_only`` (selected, but Tiingo serves another row for the ticker, so it is answered
    wrong_entity without a request unless asked with --tickers ... --fetch-shadowed: WOLF), ``not_in_candidate_list``
    (an unknown-size name the plan proposes; the candidate list has no row for it yet), ``N`` otherwise."""
    if row.get("group") == "unknown_size_delisted":
        return "not_in_candidate_list"
    if row.get("status") not in MONTH_2_STATUSES and fetcher_status not in TIINGO_RETRY:
        return "N"
    flags = str(row.get("tiingo_flags", "") or "").split()
    if "ask_shadowed" in flags or str(row.get("tiingo_range_match", "")) in SHADOWED_MATCHES:
        return "fetch_shadowed_only"
    return "Y"


def tiingo_month2_plan(candidates: pd.DataFrame, unknown: pd.DataFrame, facts: pd.DataFrame, index: dict,
                       weekly: pd.DataFrame, evidence: pd.DataFrame, fetch_status: pd.DataFrame,
                       holders: dict | None = None, fetched: dict | None = None,
                       cached=tiingo_cached, classes: set[str] = frozenset(),
                       investment: dict | None = None, doubtful: set | None = None) -> tuple[pd.DataFrame, dict]:
    """Every name only Tiingo can serve, for month 2, ranked by expected top-250 name-weeks (step 12's
    ``p_top250`` summed over the name's missing weeks in the row's need, as ``plan_evidence`` counts them: no
    investment-company weeks, nothing from a company-level proxy on a share class; ties by the weeks with no
    evidence at all, then by step 6's dv50 rank <= 250 weeks without vendor raw), with the cut lines of 500 new
    symbols (the free tier) and of the fetcher's stop, and whether the month-2 command selects each row
    (``fetch_selectable``). Groups: ``yahoo_fallback`` (a Yahoo answer that failed), ``deferred_quota`` (month 1
    overflow, also rows the fetcher itself deferred or failed), ``tier_c`` (the B-C sample while unanswered, and
    the conditional rest), ``month2_other``, and ``unknown_size_delisted`` (names with neither a series nor a size
    proxy whose unknown weeks a served Tiingo row covers; a new candidate row). A ticker already answered costs
    no new symbol."""
    holders, fetched = holders or {}, fetched or {}
    status = {}
    if len(fetch_status):
        for r in fetch_status.to_dict("records"):
            status[(str(r["security_id"]), str(r["ticker_for_source"]), str(r["needed_start"]), str(r["needed_end"]))] = r["status"]
    rows = []
    tiingo = candidates[candidates["planned_source"] == "tiingo"].fillna("")
    for r in tiingo.to_dict("records"):
        mine = status.get((r["security_id"], r["ticker_for_source"], r["needed_start"], r["needed_end"]), "")
        # (round 9: a row whose answer was written back, ``tiingo_answers_back``, is final too)
        if mine in TIINGO_FINAL or r["status"] in TIINGO_FINAL or r["status"] == "tier_c_not_triggered":
            continue
        if r.get("fallback_from"):
            group = "yahoo_fallback"
        elif r["status"] == "deferred_quota" or mine in TIINGO_RETRY:
            group = "deferred_quota"
        elif r["reason"].startswith("B_C_"):
            group = "tier_c"
        elif r["fetch_month"] == MONTH_2:
            group = "month2_other"
        else:
            continue  # month 1, still in the running fetch's queue
        rows.append({**r, "group": group, "fetcher_status": mine})
    planned = {r["security_id"] for r in rows} | set(tiingo["security_id"])
    qmap = q_suffix_map(index)
    unknown_left = Counter()
    for u in unknown.to_dict("records") if len(unknown) else []:
        sid = u["security_id"]
        if sid in planned or sid not in facts.index:
            unknown_left["already_a_tiingo_row" if sid in planned else "no_facts"] += 1
            continue
        if str(u.get("tiingo_answers", "")):
            unknown_left["tiingo_answered_already"] += 1
            continue
        fact = facts.loc[sid]
        start = max(WINDOW_START, _shift(u["first_unknown_week"], -WARMUP_DAYS), str(fact.first_listed)[:10])
        end = min(WINDOW_END, _shift(u["last_unknown_week"], 7), max(str(fact.last_listed)[:10], u["last_unknown_week"]))
        match = best_tiingo_match(sid, tiingo_tickers(fact, start, sid, qmap), start, end, index, security_info(fact),
                                  fetched)
        if not useful_partial(match, start):
            unknown_left[f"no_tiingo_row:{match['match']}"] += 1
            continue
        rows.append({"security_id": sid, "reason": "", "needed_start": start, "needed_end": end, "status": "new_row",
                     "fetch_month": MONTH_2, "group": "unknown_size_delisted", "fetcher_status": "",
                     "name": fact["name"], "cik": fact.cik, "delist_date": fact.delist_date or "",
                     "note": f"{u['unknown_weeks']} universe weeks with neither a series nor a size proxy "
                             f"({u['first_unknown_week']}..{u['last_unknown_week']})",
                     **tiingo_match_fields(match, sid, fact.cik, start, index, holders, fetched)})
        unknown_left["planned"] += 1
    plan = pd.DataFrame(rows)
    # (round 9: plus the weeks whose proxy was a fact step 6 now drops, ``dropped_float_weeks``)
    doubtful = (doubtful_float_weeks(weekly, facts) if len(facts) else set()) | set(doubtful or ())
    evidence, evidence_facts = plan_evidence(evidence, classes, investment, doubtful)
    if plan.empty:
        return pd.DataFrame(columns=PLAN_COLUMNS), {"rows": 0, "unknown_size_delisted": dict(unknown_left),
                                                    "evidence_adjustments": evidence_facts}
    by_sid = {s: g for s, g in evidence.groupby("security_id")} if len(evidence) else {}
    universe = weekly[weekly["universe"] & ~weekly["vendor_ok"].astype(bool)]
    rank250 = universe[universe["dv50_rank"] <= 250][["security_id", "week_end"]]
    rank_by_sid = {s: g["week_end"].values for s, g in rank250.groupby("security_id")}
    expected, class_weeks, unknown_weeks, missing, step6 = [], [], [], [], []
    for r in plan.itertuples(index=False):
        low, high = np.datetime64(r.needed_start), np.datetime64(r.needed_end)
        g = by_sid.get(r.security_id)
        if g is not None:
            inside = g[(g["week_end"].values >= low) & (g["week_end"].values <= high)]
            expected.append(round(float(inside["p_top250"].sum()), 2))
            class_weeks.append(int(inside["class_proxy"].sum() + inside["float_doubtful"].sum()))
            unknown_weeks.append(int(inside["unknown"].sum()))
            missing.append(int(len(inside)))
        else:
            expected.append(0.0), class_weeks.append(0), unknown_weeks.append(0), missing.append(0)
        weeks = rank_by_sid.get(r.security_id, np.array([], dtype="datetime64[ns]"))
        step6.append(int(((weeks >= low) & (weeks <= high)).sum()))
    plan = plan.assign(expected_top250_weeks=expected, proxy_not_counted_weeks=class_weeks, unknown_weeks=unknown_weeks,
                       missing_weeks=missing, step6_weeks_rank250=step6)
    plan["fetch_selectable"] = [fetch_selectable(r, r.get("fetcher_status", "")) for r in plan.to_dict("records")]
    group_order = {g: k for k, g in enumerate(["yahoo_fallback", "deferred_quota", "tier_c", "month2_other",
                                                "unknown_size_delisted"])}
    plan = plan.assign(_g=plan["group"].map(group_order)).sort_values(
        ["expected_top250_weeks", "unknown_weeks", "step6_weeks_rank250", "_g", "security_id"],
        ascending=[False, False, False, True, True], kind="stable").drop(columns="_g").reset_index(drop=True)
    seen, new, cum = set(), [], []
    for ticker in plan["ticker_for_source"].astype(str):
        fresh = bool(ticker) and ticker not in seen and not cached(ticker)
        seen.add(ticker)
        new.append("Y" if fresh else "")
        cum.append((cum[-1] if cum else 0) + int(fresh))
    plan["new_symbol"], plan["cum_new_symbols"] = new, cum
    plan[f"within_{PLAN_CUT}"] = np.where(plan["cum_new_symbols"] <= PLAN_CUT, "Y", "")
    plan[f"within_{TIINGO_MONTH_STOP}"] = np.where(plan["cum_new_symbols"] <= TIINGO_MONTH_STOP, "Y", "")
    plan["plan_rank"] = np.arange(1, len(plan) + 1)
    plan = plan.reindex(columns=PLAN_COLUMNS)
    above = plan[plan[f"within_{PLAN_CUT}"] == "Y"]
    facts_out = {"rows": int(len(plan)), "securities": int(plan["security_id"].nunique()),
                 "new_symbols": int(plan["new_symbol"].eq("Y").sum()),
                 "rows_by_group": dict(Counter(plan["group"])),
                 "rows_by_fetch_selectable": dict(Counter(plan["fetch_selectable"])),
                 "expected_top250_weeks_by_group": {g: round(float(v), 1) for g, v in
                                                    plan.groupby("group")["expected_top250_weeks"].sum().items()},
                 f"expected_top250_weeks_within_{PLAN_CUT}": round(float(above["expected_top250_weeks"].sum()), 1),
                 f"expected_top250_weeks_below_{PLAN_CUT}": round(float(plan.loc[plan[f"within_{PLAN_CUT}"] != "Y",
                                                                                "expected_top250_weeks"].sum()), 1),
                 "rows_with_no_expected_weeks": int((plan["expected_top250_weeks"] == 0).sum()),
                 "unknown_size_delisted": dict(unknown_left),
                 "evidence_adjustments": evidence_facts,
                 "month2_command": ("PYTHONPATH=. python scripts/reversal_data_tiingo.py --month2 --already-used N "
                                    f"(= --statuses {','.join(MONTH_2_STATUSES)}); fetch_selectable says which rows it asks"),
                 "evidence": "step 12 weekly_listed.csv.gz p_top250 (as of its last build) over the row's need, without "
                             "investment-company weeks (D6) and with nothing from a company-level market-cap / float "
                             "proxy on a share class or tracking stock, nor from an untested $10B+ float 20x the "
                             "security's Form 25 float (proxy_not_counted_weeks); ties by unknown weeks and "
                             "step 6's dv50 rank <= 250 weeks without vendor raw"}
    return plan, facts_out


V_CATEGORY_ORDER = ["named_break_2025_06_24", "stored_break_2025_06_24", "split_after_2023", "special_dividend",
                    "odd_split"]


def assign_fetch_order(candidates: pd.DataFrame) -> pd.DataFrame:
    """``fetch_order``: one number per Tiingo ticker, the order the fetcher asks them in (month 1 before
    month 2; then reason priority; V by category; then the most liquid name). Rows sharing a ticker
    take its earliest place."""
    frame = candidates.copy()
    tiingo = frame[frame["planned_source"] == "tiingo"].copy()
    category = tiingo["v_category"].map({c: k for k, c in enumerate(V_CATEGORY_ORDER)}).fillna(len(V_CATEGORY_ORDER))
    tiingo = tiingo.assign(_month=(tiingo["fetch_month"] != MONTH_1).astype(int), _category=category,
                           _rank=pd.to_numeric(tiingo["best_rank"], errors="coerce").fillna(1e9))
    tiingo = tiingo.sort_values(["_month", "priority", "_category", "_rank", "security_id"], kind="stable")
    order: dict[str, int] = {}
    for ticker in tiingo["ticker_for_source"]:
        order.setdefault(ticker, len(order) + 1)
    frame["fetch_order"] = ""
    frame.loc[tiingo.index, "fetch_order"] = tiingo["ticker_for_source"].map(order).astype(int).astype(str)
    return frame




def apply_budget(candidates: pd.DataFrame, used: int, already_used: int | None = None,
                 stop: int = TIINGO_MONTH_STOP, ledger_symbols: set[str] = frozenset()) -> tuple[pd.DataFrame, dict]:
    """Month-1 Tiingo symbols in ``fetch_order`` up to the fetcher's stop, less the unique symbols the
    quota ledger shows this month (``used``; a ticker among them, ``ledger_symbols``, costs nothing
    more) and those the owner reads off the account page that the ledger does not hold
    (``already_used``; None when not given, then counted as 0 and said so). New symbols past that
    are ``deferred_quota`` to month 2."""
    frame = candidates.copy()
    month1 = frame[(frame["planned_source"] == "tiingo") & (frame["fetch_month"] == MONTH_1)]
    order = month1.assign(_order=pd.to_numeric(month1["fetch_order"], errors="coerce")).sort_values("_order")
    room = stop - used - (already_used or 0)
    keep, new = [], 0
    for ticker in order["ticker_for_source"]:
        if ticker in keep:
            continue
        if str(ticker).upper() in ledger_symbols:
            keep.append(ticker)
        elif new < room:
            keep.append(ticker)
            new += 1
    for k, row in order.iterrows():
        if row.ticker_for_source not in set(keep):
            frame.loc[k, ["status", "fetch_month"]] = ["deferred_quota", MONTH_2]
    month2 = frame[(frame["planned_source"] == "tiingo") & (frame["fetch_month"] == MONTH_2)]
    # Round 9: the month's own stop. The fetcher stops at ``stop`` unless run with --month-stop (at most the free
    # tier's TIINGO_MONTHLY_SYMBOLS), so symbols counted above ``stop`` were spent on purpose with a higher stop
    # (October: 500, the last 20 asked 17:13-17:40 with --month-stop 500); the room left is never below 0.
    counted = used + (already_used or 0)
    over = max(counted - stop, 0)
    stop_used = min(max(counted, stop), TIINGO_MONTHLY_SYMBOLS)
    if not over:
        note = f"{counted} symbols counted this month; {max(room, 0)} more planned up to the fetcher's stop of {stop}"
    else:
        note = (f"{counted} symbols counted this month, {over} above the fetcher's default stop of {stop}: asked on "
                f"purpose with --month-stop {stop_used} (at most the free tier's {TIINGO_MONTHLY_SYMBOLS}); "
                f"{max(TIINGO_MONTHLY_SYMBOLS - counted, 0)} left up to {TIINGO_MONTHLY_SYMBOLS}, none planned "
                f"(the plan keeps to the default stop)")
        if counted > TIINGO_MONTHLY_SYMBOLS:
            note += f"; OVER the free tier's {TIINGO_MONTHLY_SYMBOLS}: read the account page"
    return frame, {"month1_symbols": len(keep), "month1_new_symbols": new, "month1_room": max(room, 0), "month_stop": stop,
                   "month_stop_used": stop_used, "month_limit": TIINGO_MONTHLY_SYMBOLS,
                   "symbols_above_default_stop": over, "budget_note": note,
                   "ledger_used_this_month": used,
                   "already_used_outside_ledger": already_used if already_used is not None else "not given (counted as 0)",
                   "deferred_quota_symbols": int(frame.loc[frame["status"] == "deferred_quota", "ticker_for_source"].nunique()),
                   "month2_symbols": int(month2["ticker_for_source"].nunique()),
                   "month2_symbols_unconditional": int(month2.loc[month2["status"] != "conditional_tier_c",
                                                                  "ticker_for_source"].nunique()),
                   "month_kind": "unverified: calendar month or rolling 30 days (read the account page before the run)"}


# ------------------------------------------------------------------ stage 6c: Tiingo answers fed back

TIINGO_EMPTY = {"wrong_entity", "no_data", "no_data_in_window", "refused"}  # final, with no usable rows
FETCH_COVERAGE_OK, FETCH_START_SLACK, FETCH_END_SLACK = 0.95, 7, 21  # as the fetcher's entity check
COVERAGE_REVIEW = re.compile(r"^covers \d+% of the needed sessions$")


def recheck_answer(answer: dict, start: str, end: str, sessions: pd.DatetimeIndex) -> tuple[str, str]:
    """(status, note) of a Tiingo answer the fetcher checked on another need, on [start, end]: the fetcher's
    coverage rule (first row within 7 days of the start, last within 21 of the end, 95% of the sessions between)
    on the answer's own rows; its other entity findings (dollar volume, LastSale, gap jumps) stand, so only its
    coverage item is dropped from them. An answer without a readable prices file keeps its status."""
    status = str(answer.get("status", ""))
    path = str(answer.get("prices_path", "") or "")
    if status not in FETCHED_OK or not path or not Path(path).exists():
        return status, "not re-checked (no prices file)" if status in FETCHED_OK else ""
    dates = pd.DatetimeIndex(pd.to_datetime(pd.read_csv(path, usecols=["date"], dtype=str)["date"])).unique()
    lo_day, hi_day = pd.Timestamp(start), pd.Timestamp(end)
    need = sessions[(sessions >= lo_day) & (sessions <= hi_day)]
    inside = dates[(dates >= lo_day) & (dates <= hi_day)]
    if not len(inside):
        return "no_data_in_window", "no answer row in this row's need"
    first, last = dates.min(), dates.max()
    covers_start = first <= lo_day + pd.Timedelta(days=FETCH_START_SLACK)
    covers_end = last >= hi_day - pd.Timedelta(days=FETCH_END_SLACK)
    lo = max(lo_day, first) if covers_start else lo_day
    hi = min(hi_day, last) if covers_end else hi_day
    counted = need[(need >= lo) & (need <= hi)]
    coverage = float(np.isin(counted, inside).mean()) if len(counted) else 1.0
    note = f"{coverage:.0%} of its sessions"
    if coverage < FETCH_COVERAGE_OK or not covers_start or not covers_end:
        return "partial", note
    others = [n for n in str(answer.get("entity_notes", "") or "").split("; ") if n and not COVERAGE_REVIEW.match(n)]
    return ("done_review" if answer.get("entity_check") == "review" and others else "done"), note


def tiingo_answers_back(candidates: pd.DataFrame, fetch_status: pd.DataFrame,
                        sessions: pd.DatetimeIndex | None = None) -> tuple[pd.DataFrame, dict]:
    """Plan 1.1 ``status``, round 9: the Tiingo fetcher's verdicts written back to the Tiingo rows, as the Yahoo
    answers are (``yahoo_fallback``). A row the fetcher answered for this very need (same security, ticker and
    need) takes its final status (done, done_review, partial, wrong_entity, no_data, no_data_in_window, refused)
    and, as ``fetch_month``, the month of the answer. A row whose need changed since (a need split at a listing gap:
    CGRNQ 2018-01-21..2023-10-22, asked as 2018-01-21..2026-07-07) takes the answer the fetcher holds for its
    security and ticker, re-checked on its own need (``recheck_answer``), with a note. Rows the fetcher has not
    answered (never asked, deferred, failed, or only prechecked: a hidden row asked on purpose later) keep the
    plan's status, so the month-2 command still selects them. Returns (candidates, counts)."""
    frame = candidates.copy()
    counts: Counter = Counter()
    if fetch_status is None or not len(fetch_status):
        return frame, {"rows_written_back": 0}
    sessions = sessions if sessions is not None else xnas_sessions()
    status = fetch_status.copy()
    for column in ("http_status", "entity_check", "updated_utc", "fetched_utc", "prices_path", "entity_notes"):
        if column not in status:
            status[column] = ""
    status = status[status["status"].isin(TIINGO_FINAL) & (status["entity_check"] != "precheck")]
    status = status.sort_values("updated_utc", kind="stable")
    key = ["security_id", "ticker_for_source", "needed_start", "needed_end"]
    exact = {tuple(r[c] for c in key): r for r in status.to_dict("records")}
    by_pair = {(r["security_id"], str(r["ticker_for_source"]).upper()): r for r in status.to_dict("records")}
    for k in frame.index[frame["planned_source"] == "tiingo"]:
        row = frame.loc[k]
        sid, ticker = str(row["security_id"]), str(row["ticker_for_source"])
        start, end = str(row["needed_start"]), str(row["needed_end"])
        answer = exact.get((sid, ticker, start, end))
        if answer is not None:
            new, note = answer["status"], ""
            counts["same_need"] += 1
        elif (sid, ticker.upper()) in by_pair:
            answer = by_pair[(sid, ticker.upper())]
            new, checked = recheck_answer(answer, start, end, sessions)
            note = (f"Tiingo answer of {str(answer.get('fetched_utc', ''))[:10]} for the need {answer['needed_start']}.."
                    f"{answer['needed_end']} ({answer['status']}), re-checked on this row's need: {new}"
                    + (f", {checked}" if checked else ""))
            counts["other_need_rechecked"] += 1
        else:
            counts[f"not_answered:{row['status']}"] += 1
            continue
        counts[f"status:{new}"] += 1
        month = str(answer.get("fetched_utc", "") or "")[:7]
        frame.loc[k, "status"] = new
        if re.fullmatch(r"\d{4}-\d{2}", month):
            frame.loc[k, "fetch_month"] = month
        if note:
            old = frame.at[k, "note"]
            frame.loc[k, "note"] = _join_notes(old if isinstance(old, str) else "", note)
    counts["rows_written_back"] = counts["same_need"] + counts["other_need_rechecked"]
    return frame, dict(counts)


# ------------------------------------------------------------------ stage 7: unfillable and coverage

def unfillable_rows(candidates: pd.DataFrame, facts: pd.DataFrame, weekly: pd.DataFrame, above: pd.Series,
                    ranks: pd.DataFrame) -> pd.DataFrame:
    """Names (or parts of a needed window) no free source has: routed unfillable (a Yahoo answer that
    failed included, round 6), the part of the window a partial Tiingo row leaves out (30 days or more),
    or the gap a partial Yahoo answer covering at least MIN_PARTIAL_COVERAGE leaves (no Tiingo symbol is
    spent on it)."""
    gaps = []
    frame = candidates.copy()
    for column in ("fallback_from", "yahoo_row_start", "yahoo_row_end", "yahoo_coverage"):
        if column not in frame:
            frame[column] = ""
    frame = frame.fillna({"fallback_from": "", "yahoo_row_start": "", "yahoo_row_end": "", "yahoo_coverage": ""})
    for row in frame[frame["reason"] != "V_verify_sample"].itertuples(index=False):
        wiki = "wiki(ends 2018-03-27)" if row.needed_start >= "2018-01-01" else "wiki(no usable file)"
        yahoo = (f"yahoo({str(row.fallback_from)[6:]} {row.yahoo_row_start or '-'}..{row.yahoo_row_end or '-'})"
                 if row.fallback_from else "yahoo(delisted)")
        if row.planned_source == "unfillable":
            tried = (f"{wiki}; tiingo_supported({row.tiingo_range_match or 'no_row'} {row.ticker_for_source}; own, +Q, "
                     f"alias and SEC tickers tried); {yahoo}")
            gaps.append((row, row.needed_start, row.needed_end, tried))
        elif (row.planned_source == "yahoo" and row.status == "partial" and row.yahoo_row_start
              and float(pd.to_numeric(row.yahoo_coverage, errors="coerce") or 0) >= MIN_PARTIAL_COVERAGE):
            tried = (f"{wiki}; yahoo(partial {row.yahoo_row_start}..{row.yahoo_row_end}, "
                     f"{float(row.yahoo_coverage):.0%} of the need); tiingo not asked")
            for start, end in uncovered_gaps([{"verdict": "partial", "first_row": row.yahoo_row_start,
                                               "last_row": row.yahoo_row_end}], row.needed_start, row.needed_end):
                gaps.append((row, start, end, tried))
        elif row.planned_source == "tiingo" and row.tiingo_range_match == "partial":
            tried = f"{wiki}; tiingo_supported(partial {row.tiingo_row_start}..{row.tiingo_row_end})"
            if _days(row.needed_start, row.tiingo_row_start) >= 30:
                gaps.append((row, row.needed_start, _shift(row.tiingo_row_start, -1), tried))
            if _days(row.tiingo_row_end, row.needed_end) >= 30:
                gaps.append((row, _shift(row.tiingo_row_end, 1), row.needed_end, tried))
    universe = weekly[weekly["universe"]]
    out = []
    for row, start, end, tried in gaps:
        mine = universe[(universe["security_id"] == row.security_id)
                        & universe["week_end"].between(pd.Timestamp(start), pd.Timestamp(end))]
        # The same proxies the weekly estimate used: market caps carried up to 12 months into the window.
        carried_from = _shift(start, -PROXY_CARRY_DAYS)
        caps = ranks[(ranks["security_id"] == row.security_id) & ranks["snapshot_date"].between(carried_from, end)]
        if len(caps) and mine["mcap"].notna().any():
            proxy, value = "mcap", f"best_rank_{int(caps['mcap_rank'].min())}"
        elif mine["float_usd"].notna().any():
            proxy, value = "float", float(mine["float_usd"].max())
        else:
            proxy, value = "none", ""
        out.append({"security_id": row.security_id, "ticker": row.ticker_for_source, "needed_start": start,
                    "needed_end": end, "sources_tried": tried, "est_weeks_in_top250": int(above.loc[mine.index].sum()),
                    "proxy": proxy, "proxy_value": value, "reason": row.reason, "cik": row.cik, "name": row.name,
                    "delist_date": row.delist_date, "status": row.status})
    return pd.DataFrame(out, columns=UNFILLABLE_COLUMNS)


def _shift(day: str, days: int) -> str:
    return (pd.Timestamp(day) + pd.Timedelta(days=days)).strftime("%Y-%m-%d")


def planned_cover(candidates: pd.DataFrame) -> dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]]:
    """security -> windows a planned (or cached) Yahoo, Tiingo or WIKI request should fill."""
    spans = defaultdict(list)
    usable = candidates[candidates["planned_source"].isin(["yahoo", "tiingo", "wiki"])
                        & ~candidates["status"].isin(["conditional_tier_c", "yahoo_failed"])
                        # round 9: a Tiingo answer without usable rows (its status written back) fills nothing
                        & ~((candidates["planned_source"] == "tiingo") & candidates["status"].isin(TIINGO_EMPTY))]
    for row in usable.itertuples(index=False):
        start, end = pd.Timestamp(row.needed_start), pd.Timestamp(row.needed_end)
        if row.planned_source == "tiingo" and row.tiingo_row_start:
            start, end = max(start, pd.Timestamp(row.tiingo_row_start)), min(end, pd.Timestamp(row.tiingo_row_end))
        yahoo_start, yahoo_end = getattr(row, "yahoo_row_start", ""), getattr(row, "yahoo_row_end", "")
        if row.planned_source == "yahoo" and isinstance(yahoo_start, str) and yahoo_start:
            start, end = max(start, pd.Timestamp(yahoo_start)), min(end, pd.Timestamp(yahoo_end))
        if start <= end:
            spans[row.security_id].append((start, end))
    return spans


def weekly_coverage(weekly: pd.DataFrame, above: pd.Series, candidates: pd.DataFrame) -> pd.DataFrame:
    """Per week, before any fetch: of the names ranked 1-300 by dv50 (raw close >= $10, U counted in),
    how many have a usable vendor raw series, how many only a stored file, how many only an
    estimated (U) price; how many listed universe names have no price at all, and how many of
    those have a proxy at or above the median of ranks 200-250. The *_after_plan columns count
    what the planned requests would add (window overlap only, not a promise of data)."""
    universe = weekly[weekly["universe"]].copy()
    cover = planned_cover(candidates)
    planned = np.zeros(len(universe), dtype=bool)
    positions = {s: np.flatnonzero(universe["security_id"].values == s) for s in cover}
    days = universe["week_end"].values
    for sid, windows in cover.items():
        rows = positions[sid]
        for start, end in windows:
            planned[rows[(days[rows] >= np.datetime64(start)) & (days[rows] <= np.datetime64(end))]] = True
    universe["planned"] = planned
    universe["above"] = above.loc[universe.index].values
    top = universe[universe["dv50_rank"] <= FETCH_RANK]
    unpriced = universe[(universe["dv50"].isna() | universe["close"].isna()) & ~universe["outside_trading"]]
    g, t, u = universe.groupby("week_end"), top.groupby("week_end"), unpriced.groupby("week_end")
    table = pd.DataFrame({
        "n_listed_universe": g.size(),
        "n_ranked": g["dv50_rank"].count(),
        "top300_vendor_raw": t["vendor_ok"].sum(),
        "top300_stored_only": (~top["vendor_ok"] & (top["src"] == "stored")).groupby(top["week_end"]).sum(),
        "top300_price_unknown": (top["price_ge_10"] == "U").groupby(top["week_end"]).sum(),
        "top300_vendor_after_plan": (top["vendor_ok"] | top["planned"]).groupby(top["week_end"]).sum(),
        "n_unpriced": u.size(),
        "n_unpriced_proxy_above_rank200_250": u["above"].sum(),
        "n_unpriced_proxy_above_unplanned": (unpriced["above"] & ~unpriced["planned"]).groupby(unpriced["week_end"]).sum(),
    }).fillna(0).astype(int)
    table.index.name = "week_end"
    return table.reset_index()


# ------------------------------------------------------------------ delisted names with no evidence

UNKNOWN_SIZE = OUT / "unknown_size_delisted.csv"
CANONICAL_DIR = common.CACHE / "prices"
YAHOO_SERIES_DIR = common.CACHE / "yahoo"
UNKNOWN_SIZE_COLUMNS = ["security_id", "tickers", "name", "cik", "delist_date", "last_listed", "unknown_weeks",
                        "first_unknown_week", "last_unknown_week", "unknown_weeks_by_year", "candidate_reasons",
                        "planned_source", "candidate_status", "fetch_month", "tiingo_answers", "next_step"]


def fetched_dates(sids, fetch_status_path: Path = FETCH_STATUS, canonical_dir: Path = CANONICAL_DIR,
                  yahoo_dir: Path = YAHOO_SERIES_DIR) -> dict[str, np.ndarray]:
    """security -> sorted dates of the vendor series fetched or reconciled since step 6 read its files:
    the canonical panel file (step 9), the Yahoo series (step 7) and the Tiingo answers the fetcher
    kept for the security (step 8, ``done``/``done_review``/``partial``). Only the ``date`` column is read."""
    sids = set(sids)
    paths = defaultdict(list)
    for sid in sids:
        paths[sid] += [p for p in (Path(canonical_dir) / f"{sid}.csv", Path(yahoo_dir) / f"{sid}.csv.gz") if p.exists()]
    if Path(fetch_status_path).exists():
        status = read_csv_text(fetch_status_path)
        status = status[status["security_id"].isin(sids) & status["status"].isin(FETCHED_OK) & (status["prices_path"] != "")]
        for row in status.itertuples(index=False):
            if Path(row.prices_path).exists():
                paths[row.security_id].append(Path(row.prices_path))
    out = {}
    for sid, files in paths.items():
        dates = [pd.read_csv(p, usecols=["date"], dtype=str)["date"] for p in files]
        if dates:
            out[sid] = np.unique(pd.to_datetime(pd.concat(dates)).values.astype("datetime64[D]"))
    return out


def unknown_size_delisted(weekly: pd.DataFrame, facts: pd.DataFrame, candidates: pd.DataFrame,
                          fetched: dict | None = None, dates: dict | None = None) -> pd.DataFrame:
    """Names no longer trading (not ``active``: no SEC current ticker or a delisting; round 6: a name that
    moved to NYSE or fell out of the latest snapshots is still trading and goes to Yahoo, not here) with
    universe weeks that have neither a series nor a size proxy: no vendor raw cover and no step-6 dollar volume from any source (the stored
    files included), no market cap or XBRL float within 12 months, and no row of a series fetched since
    (``dates``, from ``fetched_dates``) in the week's last 5 sessions. Weeks before the first / after the
    last trade and weeks with a vendor close under $10 are left out (as in step 12). One row per security;
    ``next_step`` says what could still fill it (a later Tiingo month, or a proxy)."""
    fetched = fetched or {}
    active = facts["active"] if "active" in facts else facts["active_nasdaq"]
    gone = set(facts.index[~active.astype(str).isin(["True", "true", "1"])])
    w = weekly[weekly["universe"] & weekly["security_id"].isin(gone) & ~weekly["vendor_ok"].astype(bool)
               & ~weekly["outside_trading"].astype(bool)]
    has_dv = w["dv50"].notna() & w["price_ge_10"].isin(["Y", "U"])
    w = w[~has_dv & w["mcap"].isna() & w["float_usd"].isna() & (w["price_ge_10"] != "N")]
    if dates is None:
        dates = fetched_dates(w["security_id"].unique())
    if len(w) and dates:
        week = w["week_end"].values.astype("datetime64[D]")
        seen = np.zeros(len(w), dtype=bool)
        for sid, index in w.groupby("security_id").indices.items():
            mine = dates.get(sid)
            if mine is None or not len(mine):
                continue
            # a row in the week's last 5 sessions (7 calendar days back from the week end)
            hi = np.searchsorted(mine, week[index], "right")
            lo = np.searchsorted(mine, week[index] - np.timedelta64(6, "D"), "left")
            seen[index] = hi > lo
        w = w[~seen]
    cand = candidates.fillna("")
    by_sid = {s: g for s, g in cand[cand["reason"] != "V_verify_sample"].groupby("security_id")}
    answers = defaultdict(list)
    for (sid, ticker), seen in fetched.items():
        answers[sid].append(f"{ticker}:{seen['status']}")
    rows = []
    for sid, g in w.groupby("security_id"):
        info = facts.loc[sid]
        mine = by_sid.get(sid)
        reasons = " ".join(sorted(set(mine["reason"]))) if mine is not None else ""
        source = " ".join(sorted(set(mine["planned_source"]))) if mine is not None else ""
        status = " ".join(sorted(set(mine["status"]))) if mine is not None else ""
        month = " ".join(sorted(set(m for m in mine["fetch_month"] if m))) if mine is not None else ""
        told = sorted(answers.get(sid, []))
        if mine is None:
            step = "not_a_candidate: needs a proxy or a later Tiingo month"
        elif "tiingo" in source and MONTH_1 in month and "pending" in status.split():
            step = "tiingo_month1_pending"
        elif "tiingo" in source and set(mine.loc[mine["planned_source"] == "tiingo", "status"]) <= TIINGO_FINAL:
            step = "tiingo_answered_still_unknown: needs a proxy"  # round 9: every Tiingo row answered
        elif "tiingo" in source:
            step = "tiingo_later_month"
        elif "yahoo" in source:
            step = "yahoo_pending (successor)"
        elif told:
            step = "tiingo_answered_still_unknown: needs a proxy"
        else:
            step = "no_free_source: needs a proxy"
        years = g["week_end"].dt.year.value_counts().sort_index()
        rows.append({"security_id": sid, "tickers": info.get("tickers", ""), "name": info.get("name", ""),
                     "cik": info.get("cik", ""), "delist_date": info.get("delist_date", "") or "",
                     "last_listed": str(info.get("last_listed", ""))[:10], "unknown_weeks": int(len(g)),
                     "first_unknown_week": g["week_end"].min().strftime("%Y-%m-%d"),
                     "last_unknown_week": g["week_end"].max().strftime("%Y-%m-%d"),
                     "unknown_weeks_by_year": " ".join(f"{y}:{n}" for y, n in years.items()),
                     "candidate_reasons": reasons, "planned_source": source, "candidate_status": status,
                     "fetch_month": month, "tiingo_answers": " ".join(told), "next_step": step})
    frame = pd.DataFrame(rows, columns=UNKNOWN_SIZE_COLUMNS)
    return frame.sort_values(["unknown_weeks", "security_id"], ascending=[False, True]).reset_index(drop=True)


# ------------------------------------------------------------------ main

def coverage_by_year(table: pd.DataFrame) -> dict:
    years = table.assign(year=pd.to_datetime(table["week_end"]).dt.year).groupby("year")
    columns = [c for c in table.columns if c != "week_end"]
    return {int(y): {c: {"median": float(g[c].median()), "min": int(g[c].min()), "max": int(g[c].max())}
                     for c in columns} for y, g in years}


def summarise(candidates: pd.DataFrame, unfillable: pd.DataFrame, budget: dict, coverage: pd.DataFrame,
              facts: pd.DataFrame, hits: dict, stage_facts: dict, extra: dict) -> dict:
    fetch = candidates[candidates["reason"] != "V_verify_sample"]
    any_reason = Counter(r for h in hits.values() for r in h)
    known = {}
    for ticker in sorted(KNOWN_UNFILLABLE):
        mine = facts[facts["tickers"].fillna("").str.split().apply(lambda ts: ticker in ts)]
        rows = candidates[candidates["security_id"].isin(mine.index)]
        known[ticker] = (" ".join(f"{r.planned_source}/{r.status}/{r.tiingo_range_match or '-'}" for r in rows.itertuples())
                         or ("not a candidate" if len(mine) else "no Nasdaq interval"))
    return {
        "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": int(len(candidates)), "securities": int(candidates["security_id"].nunique()),
        "rows_by_reason": dict(Counter(candidates["reason"])),
        "securities_by_any_reason": dict(any_reason),
        "rows_by_planned_source": dict(Counter(candidates["planned_source"])),
        "rows_by_source_and_reason": {f"{s}|{r}": int(n) for (s, r), n in
                                      candidates.groupby(["planned_source", "reason"]).size().items()},
        "rows_by_status": dict(Counter(candidates["status"])),
        "tiingo_range_match": dict(Counter(candidates.loc[candidates["tiingo_range_match"] != "", "tiingo_range_match"])),
        "tiingo_budget": budget,
        "yahoo_symbols": int(candidates.loc[candidates["planned_source"] == "yahoo", "ticker_for_source"].nunique()),
        "unfillable_rows": int(len(unfillable)),
        "unfillable_est_weeks_in_top250": int(unfillable["est_weeks_in_top250"].sum()) if len(unfillable) else 0,
        "known_unfillable_from_plan": known,
        "fetch_rows_active": int((fetch["active"] == "Y").sum()),
        "coverage_by_year": coverage_by_year(coverage),
        "daily_stage": stage_facts,
        **extra,
    }


def shell_facts(shells: dict, master: pd.DataFrame, form25: pd.DataFrame, before: pd.DataFrame | None) -> dict:
    """What leaving out the SPAC shells removed: their Form 25 float tiers in the B window, and the
    rows of the previous candidate list (if one was there) that were shells."""
    info = master.set_index("security_id")
    floats = form25.set_index("accession")[["public_float_usd", "float_check_flag", "implied_float_per_share"]]
    tiers = Counter()
    for sid in shells:
        row = info.loc[sid]
        if row.delist_date and B_WINDOW[0] <= row.delist_date <= B_WINDOW[1] and row.delist_form25_accession in floats.index:
            fact = floats.loc[row.delist_form25_accession]
            tier = float_tier(pd.to_numeric(fact["public_float_usd"], errors="coerce"), fact["float_check_flag"],
                              fact["implied_float_per_share"])
            if tier:
                tiers[tier] += 1
    out = {"securities": len(shells), "by_rule": dict(Counter(shells.values())), "b_window_float_tiers": dict(tiers)}
    if before is not None and len(before):
        gone = before[before["security_id"].isin(list(shells))]
        out["previous_candidate_rows_removed"] = {f"{r}|{s}": int(n) for (r, s), n in
                                                  gone.groupby(["reason", "planned_source"]).size().items()}
    return out


FLOAT_FIXES = OUT / "float_unit_fixes.csv"
FLOAT_NAMED = {1575434: "VERY", 1659352: "CDAK", 1834045: "VWE",  # round 6: dropped as unit errors before
               1340476: "DRTT"}  # round 9: no close, 47x its Form 25 float


def float_unit_table(screen: list[dict], price: list[dict], master: pd.DataFrame) -> pd.DataFrame:
    """One row per XBRL float fact taken for a unit error (``float_facts``' per-share and 50x rules,
    ``float_price_check``'s price rule): reported value, the value used (x1000 corrected) or why it was
    left out, and whether the CIK has a Nasdaq security."""
    nasdaq = set(master["cik"].astype(int))
    rows = [{**d, "stage": "float_facts"} for d in screen]
    for d in price:
        rows.append({"cik": d["cik"], "end": d["end"], "float_reported": d["float_usd"], "shares": np.nan,
                     "flagged_by": f"price_check:{d.get('close', '')}" + (f":ratio {d['ratio']}" if "ratio" in d
                                                                          else f":per_share {d.get('per_share', '')}")
                                   + (f":form25_ratio {d['form25_ratio']}" if "form25_ratio" in d else ""),
                     "stage": "float_price_check", "action": d.get("action", "dropped"),
                     "float_used": d.get("float_used", np.nan), "check_or_reason": d.get("check_or_reason", "")})
    columns = ["cik", "end", "float_reported", "shares", "flagged_by", "stage", "action", "float_used",
               "check_or_reason"]
    table = pd.DataFrame(rows, columns=columns)
    table["nasdaq_cik"] = table["cik"].astype(int).isin(nasdaq)
    table["named"] = table["cik"].astype(int).map(FLOAT_NAMED).fillna("")
    return table.sort_values(["cik", "end", "stage"]).reset_index(drop=True)


def float_unit_summary(table: pd.DataFrame) -> dict:
    nasdaq = table[table["nasdaq_cik"]]
    named = table[table["named"] != ""]
    return {"facts": int(len(table)), "by_action": dict(Counter(table["action"])),
            "nasdaq_ciks_by_action": dict(Counter(nasdaq["action"])),
            "fixed_by_check": dict(Counter(table.loc[table["action"] == "fixed_x1000", "check_or_reason"])),
            "named": {f"{r.named} {r.end}": f"{r.action}: {r.float_reported:.4g} -> "
                                            f"{'' if pd.isna(r.float_used) else f'{r.float_used:.4g}'} ({r.check_or_reason})"
                      for r in named.itertuples(index=False)},
            "file": str(FLOAT_FIXES)}


def list_changes(before: pd.DataFrame | None, after: pd.DataFrame) -> dict:
    """Rows of the previous candidate list on disk, by (security, reason, planned source), that this build
    dropped or added, and rows whose status changed: so a "no row was lost" claim can be checked."""
    if before is None or not len(before):
        return {"previous_list": "none"}
    key = ["security_id", "reason", "planned_source"]
    old = before.drop_duplicates(key).set_index(key)
    new = after.fillna("").drop_duplicates(key).set_index(key)
    gone, added = old.index.difference(new.index), new.index.difference(old.index)
    both = old.index.intersection(new.index)
    moved = [k for k in both if old.at[k, "status"] != new.at[k, "status"]]
    text = lambda keys: ["|".join(k) for k in keys][:200]
    return {"previous_rows": int(len(before)), "rows": int(len(after)),
            "dropped": len(gone), "added": len(added), "status_changed": len(moved),
            "dropped_keys": text(gone), "added_by_reason_source": dict(Counter(f"{r}|{p}" for _, r, p in added)),
            "status_changes": dict(Counter(f"{old.at[k, 'status']}->{new.at[k, 'status']}" for k in moved))}


ROUND6_NAMED = {"895419": "CREE/WOLF", "857855": "UCBI/UCB", "816956": "CNMD", "1578987": "BANX", "1335105": "LIXT/NMAD",
                "1486159": "CHRD", "1327318": "TRUE", "1437557": "LLEX", "830656": "PBIO", "1974640": "APGE"}


# Round 7 defect names: same-share relistings (no junction), new-equity relistings (junction on the first new
# session), class-level month-2 expectations, and D6 investment companies.
ROUND7_NAMED = {"1375365": "SMCI", "1010086": "SIGA", "1158172": "SCOR", "1376339": "MDXG", "105319": "WW",
                "1839341": "CORZ", "1486159": "CHRD", "1456772": "OPI", "1437107.B": "DISCB", "1560385.T-LMCB": "LMCB",
                "1578987": "BANX", "1487918": "OFS", "81955": "RAND", "1287750": "ARCC"}


# Round 9 names: needs split at a listing gap (Capstone CGRN/CEPL, Frontier FTR/FYBR, NII, ViewRay), Yahoo
# first trades judged on the row's own start (WW, CORZ, THRY; OPI keeps its junction review), DIRTT's float.
ROUND9_NAMED = {"1009759": "CGRN/CEPL", "20520": "FTR/FYBR", "1037016": "NIHD", "1597313": "VRAY", "105319": "WW",
                "1839341": "CORZ", "1556739": "THRY", "1456772": "OPI", "1340476": "DRTT"}
GAP_NOTE = "need split at the listing gaps"


def split_facts(candidates: pd.DataFrame, runs: dict) -> dict:
    """What the round-9 split at listing gaps did: rows and securities whose need was cut, by planned source."""
    note = candidates["note"].fillna("").astype(str)
    split = candidates[note.str.contains(GAP_NOTE, regex=False)]
    return {"rule": f"a need crossing a listing gap of more than {SPLIT_GAP_DAYS} days (warm-up {WARMUP_DAYS} + hold "
                    f"{HOLD_DAYS}) is cut there: a piece per listing run, from the run's first listed day to its last "
                    "(the need's own start and end kept), pieces without an uncovered universe week dropped",
            "securities_listed_in_two_runs_or_more": len(runs), "rows": int(len(split)),
            "securities": int(split["security_id"].nunique()),
            "rows_by_planned_source": dict(Counter(split["planned_source"])),
            "rows_by_status": dict(Counter(split["status"]))}


def round6_named(candidates: pd.DataFrame, named: dict | None = None) -> dict:
    frame = candidates.fillna("")
    return {name: " | ".join(f"{r.reason}/{r.planned_source}/{r.ticker_for_source}/{r.status}/{r.needed_start}..{r.needed_end}"
                             + (f"/fallback {r.fallback_from}" if r.fallback_from else "")
                             + (f"/junction {r.junction_date}" if r.junction_date else "")
                             for r in frame[frame["security_id"] == sid].itertuples()) or "not a candidate"
            for sid, name in (named or ROUND6_NAMED).items()}


def _tiingo_ledger_rows(month: str) -> list[list[str]]:
    """The quota ledger's Tiingo rows of ``month`` logged before the version's freeze (``quant.data.version.FROZEN_UTC``;
    every row when the version is not frozen). The ledger is shared and keeps growing after a freeze, so a frozen
    version's rebuild must not read the later rows."""
    if not common.QUOTA_LEDGER.exists():
        return []
    cutoff = _version.FROZEN_UTC.get(common.DATA_VERSION)
    rows = [line.split(",") for line in common.QUOTA_LEDGER.read_text(encoding="utf-8").splitlines()[1:]]
    return [r for r in rows if len(r) >= 5 and r[1] == "tiingo" and r[2] == month and (not cutoff or r[0] < cutoff)]


def tiingo_used(month: str) -> int:
    """Unique Tiingo symbols the quota ledger shows asked in ``month`` (``common.quota_used(..., unique_symbols=True)``
    cut at the version's freeze)."""
    return len({r[3] for r in _tiingo_ledger_rows(month)})


def ledger_symbols(month: str) -> set[str]:
    """Tiingo symbols the quota ledger shows asked in ``month`` (cut at the version's freeze)."""
    return {r[3].upper() for r in _tiingo_ledger_rows(month)}


def tiingo_queue(candidates: pd.DataFrame) -> dict:
    """The Tiingo symbols by month, status and reason (each ticker counted at its first place)."""
    tiingo = candidates[candidates["planned_source"] == "tiingo"].copy()
    tiingo["_order"] = pd.to_numeric(tiingo["fetch_order"], errors="coerce")
    first = tiingo.sort_values("_order").drop_duplicates("ticker_for_source")
    return {"symbols": int(len(first)),
            "by_month_status_reason": {f"{m}|{s}|{r}": int(n) for (m, s, r), n in
                                       first.groupby(["fetch_month", "status", "reason"]).size().items()},
            "ambiguous_symbols": int((first["tiingo_reused_ticker"] == "Y").sum()),
            "match_kinds": dict(Counter(f.split(":")[1] for f in " ".join(first["tiingo_flags"]).split()
                                        if f.startswith("alias:")))}


Y_NAMED = {"1375365": "SMCI", "1486159": "CHRD", "1839341": "CORZ", "1010086": "SIGA"}  # round 5 defect names


def y_active_all_facts(candidates: pd.DataFrame, facts: pd.DataFrame, before: pd.DataFrame | None) -> dict:
    """What rule Y_active_all adds: securities it holds for, those for which it is the only rule, the Yahoo
    symbols, and those that the previous candidate list did not ask Yahoo for."""
    hit = candidates[candidates["reasons_all"].fillna("").str.split().apply(lambda r: "Y_active_all" in r)]
    only = candidates[candidates["reason"] == "Y_active_all"]
    yahoo = set(candidates.loc[candidates["planned_source"] == "yahoo", "ticker_for_source"])
    old = set(before.loc[before["planned_source"] == "yahoo", "ticker_for_source"]) if before is not None else set()
    listed_now = facts[facts["listed_now"].astype(bool)]
    # round 6: still trading by SEC (a current ticker, no delisting) but not on Nasdaq now
    elsewhere = facts[facts["active"].astype(bool) & (facts["last_listed"].astype(str).str[:10] != WINDOW_END)
                      & (facts["uncovered_weeks"] > 0)]
    excluded = elsewhere[elsewhere["yahoo_excluded"].astype(str) != ""] if "yahoo_excluded" in elsewhere else elsewhere.iloc[0:0]
    return {"trading_off_nasdaq_uncovered": int(len(elsewhere)),
            "trading_off_nasdaq_moved": int((elsewhere["transfer_date"].fillna("") != "").sum()),
            "trading_off_nasdaq_left_out": {s: f"{facts.at[s, 'tickers']}: {r}" for s, r in excluded["yahoo_excluded"].items()},
            "trading_off_nasdaq_uncovered_weeks": int(elsewhere["uncovered_weeks"].sum()),
            "listed_now_securities": int(len(listed_now)),
            "listed_now_uncovered": int((listed_now["uncovered_weeks"] > 0).sum()),
            "listed_now_foreign_now_uncovered": int(((listed_now["uncovered_weeks"] > 0) & listed_now["foreign_now"]).sum()),
            "listed_now_blank_check_shells_left_out": sorted(listed_now.index[listed_now["spac_like_now"].astype(bool)]),
            "securities_hit": int(hit["security_id"].nunique()), "securities_only_this_rule": int(only["security_id"].nunique()),
            "yahoo_symbols": len(yahoo), "yahoo_symbols_new_vs_previous_list": len(yahoo - old),
            "yahoo_symbols_dropped_vs_previous_list": sorted(old - yahoo),
            "named": {t: " ".join(f"{r.reason}/{r.planned_source}/{r.ticker_for_source}/{r.needed_start}..{r.needed_end}"
                                  for r in candidates[candidates["security_id"] == s].itertuples()) or "not a candidate"
                      for s, t in Y_NAMED.items()}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="never download (the Tiingo list must be cached)")
    parser.add_argument("--rebuild-daily", action="store_true", help="re-read every price file")
    parser.add_argument("--tiingo-already-used", type=int, default=None,
                        help="unique Tiingo symbols used this month that the quota ledger does not show (the account "
                             "page's count minus the ledger's); without it the budget counts them as 0 and says so")
    parser.add_argument("--redraw-samples", action="store_true",
                        help="draw the V and tier-C samples afresh instead of keeping those of the list on disk")
    parser.add_argument("--out-dir", default="",
                        help="write every output (the candidate and unfillable lists included) into this directory "
                             "instead of CACHE/prefilter and INPUTS, for a trial build; stage 1 is read from "
                             "CACHE/prefilter unless --rebuild-daily (then it is written here too)")
    parser.add_argument("--unfillable-to-cache", action="store_true",
                        help="write the unfillable list to CACHE/prefilter/unfillable_rebuilt.csv and leave "
                             "INPUTS/unfillable.csv as it is (when another owner holds that file)")
    args = parser.parse_args(argv)
    started = time.time()
    trial = Path(args.out_dir) if args.out_dir else None
    if trial is not None:
        trial.mkdir(parents=True, exist_ok=True)
        log(f"trial build: every output goes to {trial}")
    dest = (lambda path: trial / Path(path).name) if trial is not None else (lambda path: Path(path))
    cache_out = trial if trial is not None else OUT
    master, intervals = load_master(), load_intervals()
    form25 = read_csv_text(FORM25)
    before = read_csv_text(CANDIDATES) if CANDIDATES.exists() else None
    if args.rebuild_daily or not (OUT / "daily_series.pkl").exists():
        log("stage 1: daily series from every price source")
        save_daily(build_daily(master, intervals), cache_out)
        stage = load_daily(cache_out)
    else:
        stage = load_daily()
    log(f"stage 1 loaded: {len(stage['best'])} security-days")
    foreign, foreign_facts = foreign_spans(master)
    log(f"foreign filers: {foreign_facts['mixed_from_history']} MIXED from periodic_form_history.csv, "
        f"{len(foreign_facts['history_vs_master_spans_differ'])} differ from security_master.foreign_spans")
    shells = spac_shells(master, intervals)
    log(f"SPAC shells left out of the universe: {len(shells)}")
    investment, ic_issuers, ic_read = investment_spans(master, set(stage["spans"]["security_id"]), stage["spans"])
    common.atomic_write(dest(IC_SPANS_OUT), investment_span_table(investment, master).to_csv(index=False).encode())
    log(f"investment companies (D6, step 12's rule on the cached SEC submissions): {len(investment)} securities, "
        f"{len(ic_issuers)} issuers with evidence; CIKs read {ic_read['ciks']}")
    sessions = xnas_sessions()
    weeks = week_ends(sessions)
    log(f"stage 2: weekly metrics, {len(weeks)} weeks {weeks[0].date()}..{weeks[-1].date()}")
    weekly = weekly_table(stage, master, foreign, sessions, weeks, shells, investment)
    log(f"investment-company security-weeks left out of the universe: "
        f"{int((weekly['investment_company'] & ~weekly['non_common'] & ~weekly['foreign']).sum())}")
    weekly = mark_trading_bounds(weekly, stage["spans"], stage["best"])
    log("stage 3: proxies")
    ranks = mcap_ranks(stage["lists"], stage["spans"], foreign, shells, investment)
    floats_raw, unit_decisions = float_facts(offline=True)
    floats_fixed: list = []
    floats, floats_dropped = float_price_check(floats_raw, weekly, master, floats_fixed, form25)
    unit_table = float_unit_table(unit_decisions, floats_dropped + floats_fixed, master)
    common.atomic_write(dest(FLOAT_FIXES), unit_table.to_csv(index=False).encode())
    log(f"float unit errors: {dict(Counter(unit_table['action']))} ({FLOAT_FIXES.name}); "
        f"$10B+ facts failing the price check: {len(floats_dropped)}")
    weekly = attach_proxies(weekly, master, ranks, floats)
    cutoffs = proxy_cutoffs(weekly)
    above = proxy_above(weekly, cutoffs)
    weekly.to_pickle(dest(OUT / "weekly_metrics.pkl"))
    dropped_weeks = dropped_float_weeks(weekly, master, ranks, floats_raw, floats_dropped)
    runs = listing_runs(stage["spans"], weekly)
    log(f"listing runs: {len(runs)} securities listed in two runs or more (gaps over {SPLIT_GAP_DAYS} days)")
    log("stage 4: security facts and rules")
    facts = security_facts(weekly, stage["spans"], master, stage["best"], form25)
    # A listing that ends outside the universe (a foreign filer then, CBPO; or an investment company then, D6)
    # needs prices only to 4 weeks after its last universe week (``needed_window``).
    facts["ends_investment_company"] = [is_foreign_on(investment.get(s), str(d)[:10])
                                        for s, d in zip(facts.index, facts["last_listed"])]
    facts["ends_foreign"] = [is_foreign_on(foreign.get(s), str(d)[:10]) or bool(ic)
                             for s, d, ic in zip(facts.index, facts["last_listed"], facts["ends_investment_company"])]
    facts["spac_like_now"] = facts.index.isin(spac_like_now(master, intervals, facts.index[facts["listed_now"]]))
    junctions, junctions_from = relist_junctions()
    facts["relist_new_equity"] = relisting_kinds(facts, form25, junctions=junctions)
    facts["relist_first_session"] = relist_first_sessions(facts, junctions)
    new_equity = {s: f"{k} {facts.at[s, 'relist_first_session']}" for s, k in facts["relist_new_equity"].items() if k}
    log(f"relist junctions from {junctions_from}: new equity {new_equity}")
    log(f"listed now: {int(facts['listed_now'].sum())}; still blank-check shells (no Y_active_all): "
        f"{sorted(facts.index[facts['spac_like_now']])}")
    hits = rule_hits(facts, ranks, floats)
    log(f"rule hits: {len(hits)} securities")
    supported = load_supported_tickers(offline=args.offline)
    index = tiingo_index(supported)
    events = event_flags()
    breaks = stored_break_names()
    log(f"V inputs: {len(events)} event flags, {len(breaks)} stored 2025-06-24 break files")

    def v_usable(sid: str) -> bool:
        row = facts.loc[sid]
        start = max(WINDOW_START, str(row.first_listed)[:10])
        match = tiingo_range_match([(yahoo_ticker(row), "own")], start, WINDOW_END, index, security_info(row))
        return match["match"] in ("Y", "partial") and match["coverage"] >= MIN_PARTIAL_COVERAGE

    keep_v, keep_c = {}, set()
    if before is not None and not args.redraw_samples:
        previous_v = before[before["reason"] == "V_verify_sample"]
        keep_v = dict(zip(previous_v["security_id"], previous_v["v_category"]))
        keep_c = set(before.loc[before["reason"] == "B_C_sample_300M_500M", "security_id"])
    vsample = verification_sample(facts, weekly, events, breaks, v_usable, keep=keep_v)
    log(f"V sample: {len(vsample)} names, {len(set(vsample) & set(keep_v))} kept from the list on disk")
    log("stage 5: routing")
    fetched = fetched_outcomes()
    candidates = route(facts, hits, vsample, index, stage["lists"], sec_ticker_holders(master), fetched, runs=runs)
    candidates, sample_facts = refill_tier_c_sample(candidates, keep=keep_c)
    log(f"tier-C sample: {sample_facts}")
    answers = yahoo_answers()
    candidates, yahoo_counts = yahoo_fallback(candidates, facts, answers, index, sec_ticker_holders(master), fetched,
                                              sessions)
    log(f"Yahoo answers fed back ({len(answers)} answered pairs): {yahoo_counts}")
    candidates = assign_fetch_order(candidates)
    used = tiingo_used(MONTH_1)
    candidates, budget = apply_budget(candidates, used, args.tiingo_already_used, ledger_symbols=ledger_symbols(MONTH_1))
    candidates = candidates.sort_values(["priority", "security_id", "planned_source", "needed_start"],
                                        kind="stable").reset_index(drop=True)
    tiingo_status = read_csv_text(FETCH_STATUS) if FETCH_STATUS.exists() else pd.DataFrame()
    candidates, tiingo_back = tiingo_answers_back(candidates, tiingo_status, sessions)
    log(f"Tiingo answers fed back: {tiingo_back}")
    unfillable = unfillable_rows(candidates, facts, weekly, above, ranks)
    coverage = weekly_coverage(weekly, above, candidates)
    unknown = unknown_size_delisted(weekly, facts, candidates, fetched)
    log(f"delisted names with neither a series nor a size proxy: {len(unknown)} securities, "
        f"{int(unknown['unknown_weeks'].sum())} name-weeks")
    tier_c = tier_c_sample_result(candidates, tiingo_status, weekly, sessions)
    candidates = apply_tier_c_result(candidates, tier_c)
    reaching = {s: (e["ticker"], e["best_dv50_rank"], e["week"]) for s, e in tier_c["names_reaching_cut"].items()}
    log(f"tier-C sample: {tier_c['result']} ({tier_c['answered']} answered, {tier_c['unanswered']} not; reaching rank "
        f"{tier_c['cut_rank']}: {reaching})")
    plan, plan_facts = tiingo_month2_plan(candidates, unknown, facts, index, weekly, week_evidence(), tiingo_status,
                                          sec_ticker_holders(master), fetched, classes=class_securities(master),
                                          investment=investment, doubtful=dropped_weeks)
    log(f"Tiingo month-2 plan: {plan_facts}")
    common.atomic_write(dest(CANDIDATES), candidates.to_csv(index=False).encode())
    common.atomic_write(dest(UNKNOWN_SIZE), unknown.to_csv(index=False).encode())
    common.atomic_write(dest(MONTH2_PLAN), plan.to_csv(index=False).encode())
    # This step owns INPUTS/unfillable.csv again (round 6); the cache copy of the last run is superseded.
    unfillable_path = cache_out / "unfillable_rebuilt.csv" if args.unfillable_to_cache else dest(UNFILLABLE)
    common.atomic_write(unfillable_path, unfillable.to_csv(index=False).encode())
    stale = OUT / "unfillable_rebuilt.csv"
    if trial is None and not args.unfillable_to_cache and stale.exists():
        (OUT / "superseded").mkdir(exist_ok=True)
        stale.replace(OUT / "superseded" / "unfillable_rebuilt_round5.csv")
    log(f"unfillable list written to {unfillable_path}")
    common.atomic_write(dest(OUT / "weekly_coverage.csv"), coverage.to_csv(index=False).encode())
    common.atomic_write(dest(OUT / "security_facts.csv.gz"),
                        gzip.compress(facts.reset_index().rename(columns={"index": "security_id"}).to_csv(index=False).encode(), mtime=0))
    events.to_csv(dest(OUT / "event_flags.csv"), index=False)
    extra = {"stored_break_files_2025_06_24": breaks, "v_sample_categories": dict(Counter(vsample.values())),
             "event_flags": dict(Counter(events["kind"])),
             "supported_tickers": {"file": supported_tickers_path().name, "sha256": common.sha256_file(supported_tickers_path()),
                                   "rows": int(len(supported))},
             "foreign_filers": {k: (v if not isinstance(v, list) else v[:50]) for k, v in foreign_facts.items()},
             "spac_shells": shell_facts(shells, master, form25, before),
             "floats_dropped_by_price_check": floats_dropped,
             "float_unit_errors": float_unit_summary(unit_table),
             "tiingo_queue": tiingo_queue(candidates), "tier_c_sample": {**sample_facts, "outcome": tier_c},
             "investment_companies_d6": {
                 "rule": "owner decision D6: step 12's investment_companies() on the cached SEC submissions; the "
                         "weeks an issuer is one are not universe weeks here (dv and market-cap ranks, uncovered "
                         "weeks, needs, unknown-size names, the month-2 plan)",
                 "securities": len(investment), "issuers_with_evidence": int(len(ic_issuers)),
                 "ciks_read": ic_read.get("ciks"), "older_pages_not_cached": ic_read.get("pages_missing"),
                 "submissions_digest": ic_read.get("digest", ""), "merger_tail_ciks": ic_read.get("merger_tail_ciks", []),
                 "security_weeks_left_out": int((weekly["investment_company"] & ~weekly["non_common"]
                                                 & ~weekly["foreign"]).sum()),
                 # rows of investment-company securities in the list on disk that this build no longer has
                 "rows_dropped_vs_list_on_disk": (int(before["security_id"].isin(list(investment)).sum()
                                                      - candidates["security_id"].isin(list(investment)).sum())
                                                  if before is not None else None),
                 "candidate_rows_of_investment_company_securities": int(candidates["security_id"].isin(list(investment)).sum()),
                 "spans_file": str(dest(IC_SPANS_OUT))},
             "relist_junctions": {"source": junctions_from,
                                  "new_equity": {s: {"kind": k, "first_new_session": facts.at[s, "relist_first_session"]}
                                                 for s, k in facts["relist_new_equity"].items() if k},
                                  "candidate_junction_dates": {r.security_id: r.junction_date for r in candidates.itertuples()
                                                               if str(r.junction_date or "") not in ("", "nan")},
                                  "rule": "a junction only for new equity (bankruptcy or share exchange), on the new "
                                          "shares' first session (step 9's RELIST_JUNCTIONS); the same shares listed "
                                          "again keep one series"},
             "uncovered_universe_securities": int((facts["uncovered_weeks"] > 0).sum()),
             "uncovered_not_candidates": int(((facts["uncovered_weeks"] > 0) & ~facts.index.isin(list(hits))).sum()),
             "y_active_all": y_active_all_facts(candidates, facts, before),
             "yahoo_answers_fed_back": {**yahoo_counts, "answered_pairs": len(answers), "report": str(YAHOO_REPORT)},
             "tiingo_month2_plan": {**plan_facts, "file": str(dest(MONTH2_PLAN))},
             "changes_vs_previous_list": list_changes(before, candidates),
             "round6_named": round6_named(candidates),
             "round7_named": round6_named(candidates, ROUND7_NAMED),
             "round9_named": round6_named(candidates, ROUND9_NAMED),
             "need_split_at_listing_gaps": split_facts(candidates, runs),
             "tiingo_answers_fed_back": {**tiingo_back, "fetch_status": str(FETCH_STATUS),
                                         "rule": "the fetcher's final verdict on the same need (else on the same "
                                                 "security and ticker, re-checked on the row's own need) is the "
                                                 "row's status, and the month of the answer its fetch_month; rows "
                                                 "not answered keep the plan's status"},
             "doubtful_float_weeks_from_dropped_facts": {
                 "weeks": len(dropped_weeks), "securities": sorted({s for s, _ in dropped_weeks}),
                 "rule": "weeks whose float proxy was a fact float_price_check drops (no market cap): the month-2 "
                         "plan counts nothing from step 12's proxy there while step 12's file predates the drop"},
             "later_listings_after_a_form25_cut": {
                 "intervals": int(stage["spans"]["after_cut"].astype(str).eq("True").sum()),
                 "securities": int(stage["spans"].loc[stage["spans"]["after_cut"].astype(str).eq("True"), "security_id"].nunique()),
                 "unconfirmed_cut_to_one_day": stage["facts"].get("after_cut_unconfirmed", "stage 1 not rebuilt"),
                 "relisted_new_equity": {k: v for k, v in facts["relist_new_equity"].items() if v}},
             "mapping_boundary": stage["facts"].get("mapping_boundary", "stage 1 not rebuilt"),
             "unknown_size_delisted": {"securities": int(len(unknown)), "name_weeks": int(unknown["unknown_weeks"].sum()),
                                       "by_next_step": dict(Counter(unknown["next_step"])),
                                       "name_weeks_by_next_step": {k: int(v) for k, v in
                                                                   unknown.groupby("next_step")["unknown_weeks"].sum().items()},
                                       "file": str(dest(UNKNOWN_SIZE))},
             "seed": SEED, "runtime_s": round(time.time() - started, 1)}
    summary = summarise(candidates, unfillable, budget, coverage, facts, hits, stage["facts"], extra)
    common.atomic_write(dest(OUT / "prefilter_summary.json"), (json.dumps(summary, indent=1, default=str) + "\n").encode())
    log(f"candidates: {len(candidates)} rows; by reason {summary['rows_by_reason']}")
    log(f"by planned source {summary['rows_by_planned_source']}; status {summary['rows_by_status']}")
    log(f"tiingo budget {budget}; yahoo symbols {summary['yahoo_symbols']}; unfillable {len(unfillable)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
