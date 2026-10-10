"""Plan step 1: Ken French factor and industry files, the Siccodes maps, CBOE VIX, and the QQQ total-return inputs.

See docs/reversal_2012_2026_data_plan.md (step 1, sections 5.2 and 5.3).

Data only. This script downloads published files, parses them into tidy
CSVs and checks dates and coverage. It computes no strategy, portfolio or
long-short return, no signal and no ranking by return. For QQQ it writes the
inputs of a total return (raw close and cash dividend per date, with the
source of each) and checks dates, closes and dividends between sources; it
computes no return.

Every request goes through ``cached_get``, so a second run reads the cache
and fetches nothing. The Ken French files are revised monthly; the first
download is the pinned copy (its sha256 is recorded) and is not refreshed.
The QQQ tail and dividend payloads are pinned the same way (their cache
names carry the requested range and the fetch day).

QQQ dividends are checked against the issuer: Invesco's own distribution
table, read from Wayback captures of its QQQ page (the live page and its
data API refuse scripted clients), and the trust's per-share distributions
per fiscal period in its SEC reports (Financial Highlights).

Outputs
- CACHE/raw/kf/*.zip and CACHE/raw/kf/pages/*.html (as downloaded)
- CACHE/raw/vix/VIX_History.csv and the CBOE page that links it
- CACHE/raw/nasdaq/qqq/*.json (Nasdaq public API: QQQ history tail, dividend history)
- CACHE/raw/qqq_evidence/{wayback,sec,invesco}/ (the dividend evidence, as downloaded)
- CACHE/factors/{ff5_2x3_daily,mom_daily,st_rev_daily}.csv,
  CACHE/factors/ind49_daily.csv.gz, CACHE/factors/vix_daily.csv,
  CACHE/factors/ff_industry_maps_full.csv, CACHE/factors/kf_sources.json,
  CACHE/factors/step1_checks.json
- CACHE/factors/qqq_joined.csv (date, close, dividend, close_source, dividend_source, in_window),
  CACHE/factors/qqq_joined_sources.json, CACHE/factors/qqq_dividend_checks.csv
- INPUTS/ff_industry_maps.csv (scheme, industry_id, short_name, sic_lo, sic_hi)
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
import gzip
import html as html_lib
import io
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError
import zipfile

from scripts import reversal_data_common as common

KF_HOME = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/"
KF_LIBRARY_PAGE = KF_HOME + "data_library.html"
KF_FTP = KF_HOME + "ftp/"
# Pages that link each Siccodes file (the data library page does not).
KF_SIC_PAGES = {
    "FF49": "Data_Library/det_49_ind_port.html",
    "FF17": "Data_Library/det_17_ind_port.html",
    "FF12": "Data_Library/det_12_ind_port.html",
}
KF_DATA_ZIPS = {
    "ff5_2x3_daily": "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip",
    "mom_daily": "F-F_Momentum_Factor_daily_CSV.zip",
    "st_rev_daily": "F-F_ST_Reversal_Factor_daily_CSV.zip",
    "ind49_daily": "49_Industry_Portfolios_daily_CSV.zip",
}
KF_SIC_ZIPS = {"FF49": "Siccodes49.zip", "FF17": "Siccodes17.zip", "FF12": "Siccodes12.zip"}

CBOE_VIX_PAGE = "https://www.cboe.com/tradable_products/vix/vix_historical_data/"
CBOE_VIX_CSV = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
# A published fact used to verify that the file is the VIX history: the record close.
VIX_KNOWN_CLOSES = {"2020-03-16": Decimal("82.69")}

RAW_KF = common.RAW / "kf"
RAW_VIX = common.RAW / "vix"
FACTORS = common.CACHE / "factors"
SCOUT_KF = Path("/tmp/kf")  # the scouts' copies, compared by hash when present

WINDOW_START = "2011-06-01"
WINDOW_END = "2026-08-31"
# Plan section 5.3 row check.
ROW_CHECK = ("2012-01-03", "2026-07-17", 3655)
QQQ_TIINGO = Path("research_cache/holdout_2011_2019/qqq_tiingo_2010_2020.csv")
QQQ_NASDAQ = Path("output/research_only/qqq_nasdaq_history.csv")
QQQ_JOIN_SWITCH = "2018-01-01"  # Tiingo before, Nasdaq from (plan section 5.3)
QQQ_DIVIDEND_COUNT = ("2012-01-01", "2026-07-17", 60)
# The tail after the stored Nasdaq file (which ends 2026-08-14). The request overlaps that file
# from 2026-07-01 so the two can be compared, and ends on the last session before the fetch day.
QQQ_TAIL_REQUEST = ("2026-07-01", "2026-09-30")
QQQ_DIVIDENDS_FETCHED = "2026-10-01"  # names (pins) the cached Nasdaq dividend payload
NASDAQ_QQQ = common.RAW / "nasdaq" / "qqq"
QQQ_EVIDENCE = common.RAW / "qqq_evidence"
INVESCO_PAGE = "https://www.invesco.com/us/financial-products/etfs/product-detail?audienceType=Investor&ticker=QQQ"
# The page's distribution "download" link; it now serves Invesco's script-rendered app instead of a table.
INVESCO_LIVE_DOWNLOAD = ("https://www.invesco.com/us/financial-products/etfs/product-detail/main/distributions/03"
                         "?audienceType=Investor&action=download&ticker=QQQ")
# Wayback captures of INVESCO_PAGE; each carries the issuer's full distribution table (2003 on).
INVESCO_CAPTURES = ("20240112020134", "20250827045133")
QQQ_TRUST_CIK = "0001067839"  # Invesco QQQ Trust, Series 1
# Reports whose Financial Highlights give distributions per share for FY2010-FY2025 (fiscal years end
# September 30) and for the six months to 2026-03-31: N-30B-2 annual reports and the N-CSRS.
QQQ_SEC_REPORTS = ("0001104659-15-002429", "0001193125-19-320290", "0001193125-24-285734",
                   "0001193125-25-249697", "0001193125-26-250483")

HEADERS = {"User-Agent": "quant_stocks research data acquisition (python urllib)"}
WAYBACK_HEADERS = {"User-Agent": "quant_stocks-research/1.0 (QQQ distribution evidence; polite)"}
KF_LIMITER = common.SlidingWindowLimiter({1: 1})
CBOE_LIMITER = common.SlidingWindowLimiter({1: 1})
NASDAQ_LIMITER = common.SlidingWindowLimiter({1: 1})
ISSUER_LIMITER = common.SlidingWindowLimiter({1: 1})
WAYBACK_LIMITER = common.SlidingWindowLimiter({3: 1, 60: 15})

MISSING_MARKERS = (Decimal("-99.99"), Decimal("-999"))
_DATA_LINE = re.compile(r"^\s*(\d{8})\s*,(.*)$")
_STAMP = re.compile(r"created\s+(?:by\s+)?using\s+the\s+(\d{6})\s+CRSP\s+database", re.IGNORECASE)
_SIC_HEADER = re.compile(r"^\s*(\d{1,2})\s+(\S+)\s+(.*?)\s*$")
_SIC_RANGE = re.compile(r"^\s+(\d{4})\s*-\s*(\d{4})\s*(.*?)\s*$")


# ---------------------------------------------------------------- fetching


def fetch(url: str, cache_path: Path, source: str, limiter) -> bytes:
    return common.cached_get(url, cache_path, source=source, headers=HEADERS, limiter=limiter)


def fetch_kf(name: str) -> tuple[bytes, Path]:
    """A Ken French ftp file (zip), from the cache when present."""
    path = RAW_KF / name
    return fetch(KF_FTP + name, path, "kenfrench", KF_LIMITER), path


def fetch_page(url: str, path: Path, source: str, limiter) -> tuple[str, str | None]:
    """A web page used only to verify links; a failure is recorded, not raised."""
    try:
        return fetch(url, path, source, limiter).decode("utf-8", errors="replace"), None
    except Exception as exc:  # noqa: BLE001 - the error text is the result
        return "", f"{type(exc).__name__}: {common.redact(str(exc))}"


def fetch_kf_page(relative: str) -> tuple[str, str | None]:
    return fetch_page(KF_HOME + relative, RAW_KF / "pages" / Path(relative).name, "kenfrench", KF_LIMITER)


def linked(html: str, name: str) -> bool:
    """Whether ``html`` has an href ending in ``name``."""
    return re.search(r'href="[^"]*' + re.escape(name) + '"', html, re.IGNORECASE) is not None


def zip_text(data: bytes) -> tuple[dict, str]:
    """The single member of a published zip, as text, with its zip facts."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        if len(members) != 1:
            raise ValueError(f"expected one member, found {[m.filename for m in members]}")
        member = members[0]
        raw = archive.read(member)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    facts = {"member": member.filename, "member_bytes": member.file_size,
             "member_crc32": f"{member.CRC:08x}",
             "member_zip_datetime": datetime(*member.date_time).isoformat()}
    return facts, text


# ---------------------------------------------------------------- parsing


def to_decimal_text(pct: str) -> str:
    """'0.56' (percent) -> '0.0056' (decimal), exactly, without float rounding."""
    value = Decimal(pct).scaleb(-2)
    if value == 0:
        value = abs(value)
    return format(value, "f")


def is_missing(text: str) -> bool:
    try:
        return Decimal(text) in MISSING_MARKERS
    except InvalidOperation:
        return False


def parse_kf_daily(text: str) -> dict:
    """A Ken French daily CSV: preamble, CRSP build stamp, footer and every block of rows.

    A block is a header line starting with ',' followed by YYYYMMDD rows. Its
    title is the last text line before the header when that line is indented
    (the library's convention, e.g. '  Average Value Weighted Returns -- Daily'),
    else ''. Values are kept as published text. Raises on a ragged row, a
    non-numeric value, or dates that are not strictly increasing within a block.
    """
    lines = [line.rstrip("\r") for line in text.split("\n")]
    preamble, footer, blocks = [], [], []
    pending, current = None, None  # pending: (text, indented, list it was put in)
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        match = _DATA_LINE.match(line)
        if match and current is not None:
            values = [v.strip() for v in match.group(2).split(",")]
            if len(values) != len(current["columns"]):
                raise ValueError(f"line {number}: {len(values)} values for {len(current['columns'])} columns")
            day = match.group(1)
            if current["dates"] and day <= current["dates"][-1]:
                raise ValueError(f"line {number}: date {day} does not follow {current['dates'][-1]}")
            for value in values:
                Decimal(value)  # raises InvalidOperation on anything that is not a number
            current["dates"].append(day)
            current["values"].append(values)
            continue
        if match:
            raise ValueError(f"line {number}: data row before any header")
        if stripped.startswith(","):
            title = ""
            if pending is not None and pending[1]:
                title = pending[0]
                pending[2].pop()  # a title is not preamble or footer text
            current = {"title": title, "columns": [c.strip() for c in stripped[1:].split(",")],
                       "dates": [], "values": []}
            blocks.append(current)
            pending = None
            continue
        current = None
        if not stripped:
            continue
        target = footer if blocks else preamble
        target.append(stripped)
        pending = (stripped, line[:1].isspace(), target)
    if not blocks:
        raise ValueError("no data block found")
    stamp = _STAMP.search(" ".join(preamble))
    return {"preamble": preamble, "footer": footer, "blocks": blocks,
            "crsp_build": stamp.group(1) if stamp else None,
            "stamp_line": next((p for p in preamble if "created" in p.lower()), None)}


def weighting(title: str) -> str:
    lowered = title.lower()
    if "value weighted" in lowered:
        return "vw"
    if "equal weighted" in lowered:
        return "ew"
    return lowered.strip() or "main"


def iso(day: str) -> str:
    return f"{day[:4]}-{day[4:6]}-{day[6:]}"


def tidy_factor_rows(parsed: dict) -> list[list[str]]:
    """Long rows: date, series, value_pct (as published), value_dec, missing (the marker or '')."""
    if len(parsed["blocks"]) != 1:
        raise ValueError(f"factor file has {len(parsed['blocks'])} blocks")
    block = parsed["blocks"][0]
    rows = []
    for day, values in zip(block["dates"], block["values"]):
        for column, value in zip(block["columns"], values):
            rows.append(_value_row([iso(day), column], value))
    return rows


def check_industry_blocks(parsed: dict) -> None:
    """The 49-industry file must hold exactly one value-weighted and one equal-weighted block with the
    same columns; otherwise the tidy rows would repeat (date, weighting, industry_id) keys."""
    kinds = sorted(weighting(b["title"]) for b in parsed["blocks"])
    if kinds != ["ew", "vw"]:
        raise ValueError(f"industry file blocks are {kinds}, expected one 'vw' and one 'ew' block")
    first, second = parsed["blocks"]
    if first["columns"] != second["columns"]:
        raise ValueError("the vw and ew blocks have different columns")


def tidy_industry_rows(parsed: dict) -> list[list[str]]:
    """Long rows: date, weighting, industry_id (column order, 1-based), series, value_pct, value_dec, missing."""
    check_industry_blocks(parsed)
    rows = []
    for block in parsed["blocks"]:
        kind = weighting(block["title"])
        for day, values in zip(block["dates"], block["values"]):
            for position, (column, value) in enumerate(zip(block["columns"], values), start=1):
                rows.append(_value_row([iso(day), kind, str(position), column], value))
    return rows


def _value_row(keys: list[str], value: str) -> list[str]:
    if is_missing(value):
        return keys + ["", "", value]
    return keys + [value, to_decimal_text(value), ""]


def parse_siccodes(text: str, scheme: str) -> list[dict]:
    """Industries and SIC ranges of a Siccodes file. An industry with no ranges (the
    'everything else' Other of FF12/FF17) gets one row with blank sic_lo/sic_hi."""
    industries, current = [], None
    for number, line in enumerate(text.replace("\r", "").split("\n"), start=1):
        if not line.strip():
            continue
        match = _SIC_RANGE.match(line)
        if match:
            if current is None:
                raise ValueError(f"line {number}: SIC range before any industry")
            lo, hi = int(match.group(1)), int(match.group(2))
            if lo > hi:
                raise ValueError(f"line {number}: range {lo}-{hi} is reversed")
            current["ranges"].append((lo, hi, match.group(3)))
            continue
        match = _SIC_HEADER.match(line)
        if not match:
            raise ValueError(f"line {number}: cannot parse {line!r}")
        current = {"industry_id": int(match.group(1)), "short_name": match.group(2),
                   "industry_name": match.group(3), "ranges": []}
        industries.append(current)
    ids = [i["industry_id"] for i in industries]
    if ids != list(range(1, len(ids) + 1)):
        raise ValueError(f"{scheme}: industry ids are not 1..n in order: {ids}")
    rows = []
    for industry in industries:
        base = {"scheme": scheme, "industry_id": industry["industry_id"], "short_name": industry["short_name"],
                "industry_name": industry["industry_name"]}
        if not industry["ranges"]:
            rows.append({**base, "sic_lo": "", "sic_hi": "", "range_description": ""})
        for lo, hi, description in industry["ranges"]:
            rows.append({**base, "sic_lo": f"{lo:04d}", "sic_hi": f"{hi:04d}", "range_description": description})
    return rows


def sic_overlaps(rows: list[dict]) -> list[dict]:
    """Pairs of ranges in one scheme that share a SIC code."""
    ranges = sorted((int(r["sic_lo"]), int(r["sic_hi"]), r["industry_id"]) for r in rows if r["sic_lo"] != "")
    overlaps = []
    for index, (lo, hi, industry) in enumerate(ranges):
        for other_lo, other_hi, other in ranges[index + 1:]:
            if other_lo > hi:
                break
            overlaps.append({"a": f"{lo:04d}-{hi:04d} ({industry})", "b": f"{other_lo:04d}-{other_hi:04d} ({other})",
                             "same_industry": industry == other})
    return overlaps


def parse_vix_csv(text: str) -> list[dict]:
    """CBOE VIX_History.csv (DATE as MM/DD/YYYY, OPEN, HIGH, LOW, CLOSE) -> rows with ISO dates, values as published."""
    reader = csv.reader(io.StringIO(text.replace("\r", "")))
    header = [h.strip().upper() for h in next(reader)]
    if header != ["DATE", "OPEN", "HIGH", "LOW", "CLOSE"]:
        raise ValueError(f"unexpected VIX header {header}")
    rows, previous = [], ""
    for number, record in enumerate(reader, start=2):
        if not record or not "".join(record).strip():
            continue
        if len(record) != 5:
            raise ValueError(f"line {number}: {record}")
        day = datetime.strptime(record[0].strip(), "%m/%d/%Y").date().isoformat()
        if day <= previous:
            raise ValueError(f"line {number}: date {day} does not follow {previous}")
        values = [v.strip() for v in record[1:]]
        for value in values:
            Decimal(value)
        rows.append({"date": day, "open": values[0], "high": values[1], "low": values[2], "close": values[3]})
        previous = day
    return rows


# ---------------------------------------------------------------- calendar checks


@lru_cache(maxsize=1)
def _xnas():
    import exchange_calendars as xcals

    return xcals.get_calendar("XNAS", start="1990-01-02", end="2027-06-30")


def xnas_sessions(start: str, end: str) -> list[str]:
    return [d.date().isoformat() for d in _xnas().sessions_in_range(start, end)]


def session_diff(dates: list[str], sessions: list[str]) -> dict:
    """Sessions a series lacks and dates it has that are not sessions, within the sessions' span."""
    if not sessions:
        return {"missing_sessions": [], "extra_dates": []}
    have = {d for d in dates if sessions[0] <= d <= sessions[-1]}
    expected = set(sessions)
    return {"missing_sessions": sorted(expected - have), "extra_dates": sorted(have - expected)}


def window_facts(dates: list[str], missing_dates: set[str], sessions: list[str]) -> dict:
    """Coverage of a dated series over the price window (2011-06-01 to its last date)."""
    in_window = [d for d in dates if d >= WINDOW_START]
    last = dates[-1]
    diff = session_diff(in_window, [s for s in sessions if s <= last])
    lo, hi, expected_rows = ROW_CHECK
    return {
        "first_date": dates[0], "last_date": last, "last_month": last[:7],
        "covers_window_start": bool(in_window) and in_window[0] == sessions[0],
        "covers_window_end": last >= WINDOW_END,
        "rows_in_window": len(in_window),
        "rows_with_missing_marker_in_window": len([d for d in in_window if d in missing_dates]),
        "missing_sessions": diff["missing_sessions"], "extra_dates": diff["extra_dates"],
        "sessions_after_last_date_to_window_end": len([s for s in sessions if s > last]),
        "row_check": {"from": lo, "to": hi, "expected": expected_rows,
                      "found": len([d for d in dates if lo <= d <= hi])},
    }


def vix_window_checks(vix_rows: list[dict], sessions: list[str]) -> dict:
    """Window facts on the VIX rows that are XNAS sessions dated up to WINDOW_END.

    CBOE also publishes values on US market holidays (from 2022) and keeps
    publishing after the window; those rows are counted and listed apart, so
    rows_in_window and row_check compare sessions with sessions.
    """
    session_dates = [r["date"] for r in vix_rows if r["xnas_session"] == "Y" and r["date"] <= WINDOW_END]
    facts = window_facts(session_dates, set(), sessions)
    off_session = [r["date"] for r in vix_rows if r["xnas_session"] != "Y" and WINDOW_START <= r["date"] <= WINDOW_END]
    facts.update({
        "rows_basis": "rows with xnas_session=Y dated on or before the window end",
        "non_session_rows_in_window": off_session,
        "non_session_rows_in_window_on_weekends": [d for d in off_session if date.fromisoformat(d).weekday() >= 5],
        "rows_after_window_end": len([r for r in vix_rows if r["date"] > WINDOW_END]),
        "last_published_date": vix_rows[-1]["date"],
        "note": "rows on dates that are not XNAS sessions are flagged xnas_session=N in vix_daily.csv",
    })
    return facts


# ---------------------------------------------------------------- QQQ inputs (no returns)


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _quarters(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    quarters, year, quarter = [], first.year, (first.month - 1) // 3 + 1
    while (year, quarter) <= (last.year, (last.month - 1) // 3 + 1):
        quarters.append(f"{year}Q{quarter}")
        year, quarter = (year + 1, 1) if quarter == 4 else (year, quarter + 1)
    return quarters


def _quarter(day: str) -> str:
    return f"{day[:4]}Q{(int(day[5:7]) - 1) // 3 + 1}"


def qqq_file_facts(rows: list[dict], dividend_column: str, sessions: list[str]) -> dict:
    dates = [r["date"] for r in rows]
    span = [s for s in sessions if dates[0] <= s <= dates[-1]]
    diff = session_diff(dates, span)
    dividends = {r["date"]: r[dividend_column] for r in rows if float(r[dividend_column] or 0) != 0}
    blanks = sorted({k for r in rows for k, v in r.items() if v in ("", None)})
    facts = {"first_date": dates[0], "last_date": dates[-1], "rows": len(rows),
             "duplicate_dates": len(dates) - len(set(dates)), "sorted": dates == sorted(dates),
             "columns_with_blanks": blanks, "missing_sessions_in_span": diff["missing_sessions"],
             "extra_dates_in_span": diff["extra_dates"], "dividends": dividends}
    if "splitFactor" in rows[0]:
        facts["split_rows"] = {r["date"]: r["splitFactor"] for r in rows if float(r["splitFactor"]) != 1}
    return facts


def _dividend_text(text: str | None) -> str:
    """A published cash amount, or '0' on a day without one."""
    return "0" if Decimal(text or "0") == 0 else text


def qqq_total_return_join(tiingo: list[dict], nasdaq: list[dict], tail: list[dict] | None = None,
                          tail_dividends: dict[str, str] | None = None,
                          drop: dict[str, str] | None = None) -> list[dict]:
    """The QQQ total-return inputs, one row per session: date, raw close, cash dividend and their sources.

    Tiingo before QQQ_JOIN_SWITCH, the stored Nasdaq file from it to that
    file's last row, then the Nasdaq API ``tail`` after it with dividends from
    ``tail_dividends`` (ex-date -> amount). A dividend in ``drop`` (ex-date ->
    reason) is set to 0 and its source says why. Rows start at the last
    session before WINDOW_START, so the first window day has a previous close.
    No return is computed: the repo loaders take these columns as
    (close + dividend) / previous close - 1 (research_sue_lt_2020_2026.qqq_returns).
    """
    drop = drop or {}
    rows = [{"date": r["date"], "close": r["close"], "dividend": _dividend_text(r["divCash"]),
             "close_source": "tiingo_file", "dividend_source": "tiingo_file"}
            for r in tiingo if r["date"] < QQQ_JOIN_SWITCH]
    rows += [{"date": r["date"], "close": r["close"], "dividend": _dividend_text(r["cash_dividend"]),
              "close_source": "nasdaq_file", "dividend_source": "nasdaq_file"}
             for r in nasdaq if r["date"] >= QQQ_JOIN_SWITCH]
    if tail:
        stored_last = nasdaq[-1]["date"]
        tail_dividends = tail_dividends or {}
        rows += [{"date": r["date"], "close": r["close"], "dividend": _dividend_text(tail_dividends.get(r["date"])),
                  "close_source": "nasdaq_api_history", "dividend_source": "nasdaq_api_dividends"}
                 for r in tail if r["date"] > stored_last]
    dates = [r["date"] for r in rows]
    if dates != sorted(set(dates)):
        raise ValueError("joined QQQ dates are not strictly increasing")
    for row in rows:
        if row["date"] in drop and row["dividend"] != "0":
            row["dividend"], row["dividend_source"] = "0", f"dropped ({drop[row['date']]})"
    start = max((d for d in dates if d < WINDOW_START), default=WINDOW_START)
    joined = [r for r in rows if r["date"] >= start]
    for row in joined:
        row["in_window"] = "Y" if WINDOW_START <= row["date"] <= WINDOW_END else "N"
    return joined


def qqq_coverage_check(tiingo_path: Path = QQQ_TIINGO, nasdaq_path: Path = QQQ_NASDAQ,
                       sessions: list[str] | None = None, tail: list[dict] | None = None,
                       tail_dividends: dict[str, str] | None = None, sessions_after: list[str] | None = None,
                       drop: dict[str, str] | None = None) -> dict:
    """Dates, dividends and closes of the stored QQQ files, of the API tail and of their join (no returns).

    ``sessions`` are the window's XNAS sessions; ``sessions_after`` the
    sessions from the day after WINDOW_END to the tail's last date.
    """
    sessions = sessions if sessions is not None else xnas_sessions(WINDOW_START, WINDOW_END)
    tiingo, nasdaq = _read_csv(tiingo_path), _read_csv(nasdaq_path)
    result = {"files": {
        "tiingo": {"path": str(tiingo_path), "sha256": common.sha256_file(tiingo_path),
                   **qqq_file_facts(tiingo, "divCash", sessions)},
        "nasdaq": {"path": str(nasdaq_path), "sha256": common.sha256_file(nasdaq_path),
                   **qqq_file_facts(nasdaq, "cash_dividend", sessions)}}}
    # Overlap: same dates, same raw closes, same dividends.
    t_by, n_by = {r["date"]: r for r in tiingo}, {r["date"]: r for r in nasdaq}
    common_dates = sorted(set(t_by) & set(n_by))
    close_diffs = [(d, abs(float(t_by[d]["close"]) - float(n_by[d]["close"]))) for d in common_dates]
    t_div = {d: float(t_by[d]["divCash"]) for d in common_dates if float(t_by[d]["divCash"]) != 0}
    n_div = {d: float(n_by[d]["cash_dividend"]) for d in common_dates if float(n_by[d]["cash_dividend"]) != 0}
    overlap_span = (max(tiingo[0]["date"], nasdaq[0]["date"]), min(tiingo[-1]["date"], nasdaq[-1]["date"]))
    result["overlap"] = {
        "from": overlap_span[0], "to": overlap_span[1], "common_dates": len(common_dates),
        "dates_only_tiingo": sorted(d for d in t_by if overlap_span[0] <= d <= overlap_span[1] and d not in n_by),
        "dates_only_nasdaq": sorted(d for d in n_by if overlap_span[0] <= d <= overlap_span[1] and d not in t_by),
        "max_abs_close_diff": max((x for _, x in close_diffs), default=None),
        "dates_close_diff_over_1c": [d for d, x in close_diffs if x > 0.01],
        "dividend_dates_tiingo_only": sorted(set(t_div) - set(n_div)),
        "dividend_dates_nasdaq_only": sorted(set(n_div) - set(t_div)),
        "dividend_amount_mismatch_over_1e-6": sorted(d for d in set(t_div) & set(n_div)
                                                     if abs(t_div[d] - n_div[d]) > 1e-6),
        "dividends_matched": len(set(t_div) & set(n_div)),
    }
    if tail:
        result["tail"] = _tail_facts(nasdaq, tail, tail_dividends or {})
    # The join used by the plan: Tiingo before 2018, the Nasdaq file from 2018-01-02, then the API tail.
    joined = qqq_total_return_join(tiingo, nasdaq, tail, tail_dividends, drop)
    in_window = [r for r in joined if r["in_window"] == "Y"]
    joined_dates = [r["date"] for r in in_window]
    diff = session_diff(joined_dates, sessions)
    dividend_days = [r["date"] for r in in_window if r["dividend"] != "0"]
    per_quarter: dict[str, list[str]] = {}
    for day in dividend_days:
        per_quarter.setdefault(_quarter(day), []).append(day)
    lo, hi, expected = QQQ_DIVIDEND_COUNT
    last_in_window = joined_dates[-1]
    after = [r for r in joined if r["date"] > WINDOW_END]
    result["joined"] = {
        "rule": (f"tiingo before {QQQ_JOIN_SWITCH}, nasdaq file from {QQQ_JOIN_SWITCH} to its last row"
                 + (", nasdaq api tail after it" if tail else "")),
        "first_row": joined[0]["date"], "first_date_in_window": joined_dates[0],
        "last_date_in_window": last_in_window, "last_date": joined[-1]["date"],
        "rows": len(joined), "rows_in_window": len(in_window),
        "rows_by_close_source": dict(Counter(r["close_source"] for r in joined)),
        "covers_window": (joined_dates[0] == sessions[0] and last_in_window == sessions[-1]
                          and not diff["missing_sessions"]),
        "missing_sessions_to_window_end": diff["missing_sessions"],
        "missing_sessions_before_last_date": [d for d in diff["missing_sessions"] if d <= last_in_window],
        "extra_dates": diff["extra_dates"],
        "dividends_in_window": len(dividend_days),
        "dividend_count_check": {"from": lo, "to": hi, "expected": expected,
                                 "found": len([d for d in dividend_days if lo <= d <= hi])},
        "quarters_without_dividend": [q for q in _quarters(WINDOW_START, last_in_window)
                                      if q not in {_quarter(d) for d in dividend_days}
                                      and q != _quarter(last_in_window)],
        "quarters_with_several_dividends": {q: days for q, days in per_quarter.items() if len(days) > 1},
        "dividend_2020_09_21": next((r["dividend"] for r in joined if r["date"] == "2020-09-21"), None),
        "rows_after_window_end": len(after),
        "dividends_after_window_end": {r["date"]: r["dividend"] for r in after if r["dividend"] != "0"},
        "dropped_dividends": {r["date"]: r["dividend_source"] for r in joined
                              if r["dividend_source"].startswith("dropped")},
    }
    if sessions_after is not None:
        result["joined"]["sessions_after_window_end"] = session_diff([r["date"] for r in after], sessions_after)
    return result


def _tail_facts(nasdaq: list[dict], tail: list[dict], tail_dividends: dict[str, str]) -> dict:
    """The API tail against the stored Nasdaq file where they overlap, and what it adds after it."""
    stored, fresh = {r["date"]: r for r in nasdaq}, {r["date"]: r for r in tail}
    span = (tail[0]["date"], nasdaq[-1]["date"])
    both = sorted(set(stored) & set(fresh))
    close_diffs = [(d, abs(Decimal(stored[d]["close"]) - Decimal(fresh[d]["close"]))) for d in both]
    stored_divs = {d: r["cash_dividend"] for d, r in stored.items()
                   if span[0] <= d <= span[1] and Decimal(r["cash_dividend"] or "0") != 0}
    tail_dates = {r["date"] for r in tail}
    return {
        "first": tail[0]["date"], "last": tail[-1]["date"], "rows": len(tail),
        "rows_after_stored_file": len([r for r in tail if r["date"] > span[1]]),
        "overlap_with_stored_file": {
            "from": span[0], "to": span[1], "common_dates": len(both),
            "dates_only_stored": sorted(d for d in stored if span[0] <= d <= span[1] and d not in fresh),
            "dates_only_tail": sorted(d for d in fresh if d <= span[1] and d not in stored),
            "max_abs_close_diff": str(max((x for _, x in close_diffs), default=Decimal(0))),
            "dates_close_diff_over_1c": [d for d, x in close_diffs if x > Decimal("0.01")],
            "dividends_stored": stored_divs,
            "dividends_api": {d: v for d, v in tail_dividends.items() if span[0] <= d <= span[1]},
        },
        "dividends_after_stored_file": {d: v for d, v in tail_dividends.items() if d > span[1]},
        "dividend_dates_after_stored_file_without_a_price_row": sorted(
            d for d in tail_dividends if span[1] < d <= tail[-1]["date"] and d not in tail_dates),
    }


# ---------------------------------------------------------------- QQQ fetching (Nasdaq public API)


@contextmanager
def _urlopen_through_cache(module, cache_path: Path, source: str):
    """While the block runs, ``module.urlopen`` fetches through cached_get into ``cache_path``.

    src.io.nasdaq_update.fetch_history and research_v5's _nasdaq_dividend_history
    open their one URL with ``urlopen`` and have no transport hook. Routing that
    call lets their code run unchanged while the raw payload is cached, logged
    and limited like every other request; a re-run reads the cache and sends
    nothing (their cache-busting ``_=`` parameter changes the URL, not the path).
    """
    original = module.urlopen
    urls: list[str] = []

    def cached_urlopen(request, timeout=30):
        urls.append(request.full_url)
        data = common.cached_get(request.full_url, cache_path, source=source, headers=dict(request.header_items()),
                                 limiter=NASDAQ_LIMITER, timeout=timeout, retries=1)
        return io.BytesIO(data)

    module.urlopen = cached_urlopen
    try:
        yield urls
    finally:
        module.urlopen = original


def _request_url(urls: list[str]) -> str | None:
    """The last URL asked for, without the per-request cache-busting parameter."""
    return re.sub(r"[?&]_=\d+$", "", common.redact(urls[-1])) if urls else None


def _number_text(value) -> str:
    return repr(float(value))


def _us_date(text: str | None) -> str:
    """'06/22/2026' -> '2026-06-22'; anything else -> ''."""
    try:
        return datetime.strptime((text or "").strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return ""


def _fetch_pinned(fetch, path: Path):
    """Run ``fetch``; if it fails on a payload it has just written, delete that payload so it is not pinned."""
    existed = path.exists()
    try:
        return fetch(), existed
    except Exception:
        if not existed:
            path.unlink(missing_ok=True)
        raise


def fetch_qqq_tail(start: str = QQQ_TAIL_REQUEST[0], end: str = QQQ_TAIL_REQUEST[1]) -> tuple[list[dict], dict]:
    """QQQ raw closes from src.io.nasdaq_update.fetch_history (asset_class='etf'), cached under raw/nasdaq/qqq."""
    from src.io import nasdaq_update

    path = NASDAQ_QQQ / f"history_etf_{start}_{end}.json"

    def fetch():
        with _urlopen_through_cache(nasdaq_update, path, "nasdaq") as urls:
            frame = nasdaq_update.fetch_history("QQQ", date.fromisoformat(start), date.fromisoformat(end),
                                                asset_class="etf", retries=2)
        if frame.empty:
            raise ValueError(f"Nasdaq returned no QQQ rows for {start}..{end}")
        return frame, urls

    (frame, urls), from_cache = _fetch_pinned(fetch, path)
    rows = [{"date": stamp.date().isoformat(), "close": _number_text(close)}
            for stamp, close in zip(frame["date"], frame["close"])]
    return rows, {"function": "src.io.nasdaq_update.fetch_history('QQQ', start, end, asset_class='etf')",
                  "request": {"start": start, "end": end}, "url": _request_url(urls), "cache_path": str(path),
                  "from_cache": from_cache, "sha256": common.sha256_file(path), "rows": len(rows),
                  "first": rows[0]["date"], "last": rows[-1]["date"]}


def fetch_qqq_dividends(fetched: str = QQQ_DIVIDENDS_FETCHED) -> tuple[dict[str, dict], dict]:
    """QQQ cash dividends from research_v5_trend_core_satellite._nasdaq_dividend_history, cached under raw/nasdaq/qqq.

    Amounts are that function's; declaration, record and payment dates come from the same payload.
    """
    from scripts import research_v5_trend_core_satellite as v5

    path = NASDAQ_QQQ / f"dividends_etf_limit500_{fetched}.json"

    def fetch():
        with _urlopen_through_cache(v5, path, "nasdaq") as urls:
            payload, frame = v5._nasdaq_dividend_history()
        return payload, frame, urls

    (payload, frame, urls), from_cache = _fetch_pinned(fetch, path)
    amounts = {stamp.date().isoformat(): _number_text(value)
               for stamp, value in zip(frame["date"], frame["cash_dividend"])}
    detail = {}
    for row in ((json.loads(payload).get("data") or {}).get("dividends") or {}).get("rows") or []:
        ex_date = _us_date(row.get("exOrEffDate"))
        if ex_date in amounts and ex_date not in detail:
            detail[ex_date] = {"amount": amounts[ex_date], "type": row.get("type"),
                               "declaration_date": _us_date(row.get("declarationDate")),
                               "record_date": _us_date(row.get("recordDate")),
                               "payment_date": _us_date(row.get("paymentDate"))}
    ordered = sorted(detail)
    return {d: detail[d] for d in ordered}, {
        "function": "scripts.research_v5_trend_core_satellite._nasdaq_dividend_history()",
        "url": _request_url(urls), "cache_path": str(path), "from_cache": from_cache,
        "sha256": common.sha256_file(path), "rows": len(detail), "first": ordered[0], "last": ordered[-1]}


# ---------------------------------------------------------------- QQQ dividend evidence


def html_text(data: bytes) -> str:
    """Visible text of an HTML document on one line (tags removed, entities decoded, spaces collapsed)."""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", data.decode("utf-8", errors="replace"), flags=re.S | re.I)
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def parse_invesco_distributions(page: str) -> list[dict]:
    """Rows of the distribution table on Invesco's QQQ page (id="distributionTable"): ex, record and
    pay dates and the amount per share, as published. A page without that table gives []."""
    start = page.find('id="distributionTable"')
    if start < 0:
        return []
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page[start:page.find("</table>", start)], flags=re.S):
        cells = [re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", "", cell))).strip()
                 for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)]
        if len(cells) < 4 or not _us_date(cells[0]):
            continue
        Decimal(cells[3])  # raises on anything that is not an amount
        rows.append({"ex_date": _us_date(cells[0]), "record_date": _us_date(cells[1]),
                     "pay_date": _us_date(cells[2]), "amount": cells[3]})
    return rows


def wayback_view_url(timestamp: str, original: str) -> str:
    return f"https://web.archive.org/web/{timestamp}/{original}"


def fetch_wayback(timestamp: str, original: str, path: Path, attempts: int = 3) -> bytes:
    """A capture in its raw (id_) form, gunzipped when Wayback passes the original encoding through.
    Throttling (429/503) gets a pause and another try."""
    url = f"https://web.archive.org/web/{timestamp}id_/{original}"
    for attempt in range(attempts):
        try:
            data = common.cached_get(url, path, source="wayback", headers=WAYBACK_HEADERS, limiter=WAYBACK_LIMITER,
                                     timeout=120, retries=1)
            break
        except (RuntimeError, HTTPError) as exc:
            if attempt == attempts - 1 or (isinstance(exc, HTTPError) and exc.code not in (429, 503)):
                raise
            time.sleep(60 * (attempt + 1))
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def qqq_issuer_distributions() -> tuple[dict, dict]:
    """The issuer's distribution table, merged over INVESCO_CAPTURES (a later capture's row wins).

    Also records what the live issuer link serves today: its cached answer is
    an HTML application page with no table, so the live table is not readable
    without a browser.
    """
    live_path = QQQ_EVIDENCE / "invesco" / "QQQ_distributions_live_2026-10-01.csv"
    live = common.cached_get(INVESCO_LIVE_DOWNLOAD, live_path, source="invesco",
                             headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"},
                             limiter=ISSUER_LIMITER, retries=1)
    facts = {"issuer_page": INVESCO_PAGE,
             "live": {"url": INVESCO_LIVE_DOWNLOAD, "cache_path": str(live_path), "bytes": len(live),
                      "sha256": common.sha256_bytes(live), "is_html": b"<html" in live[:2000].lower(),
                      "distribution_rows": len(parse_invesco_distributions(live.decode("utf-8", errors="replace")))},
             "captures": []}
    merged: dict[str, dict] = {}
    for timestamp in INVESCO_CAPTURES:
        path = QQQ_EVIDENCE / "wayback" / f"{timestamp}__invesco-product-detail-QQQ.html"
        page = fetch_wayback(timestamp, INVESCO_PAGE, path)
        rows = parse_invesco_distributions(page.decode("utf-8", errors="replace"))
        if not rows:
            raise ValueError(f"capture {timestamp} has no distribution table")
        capture_day = f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
        for row in rows:
            previous = merged.get(row["ex_date"], {})
            row = {**row, "captures": previous.get("captures", []) + [timestamp],
                   "evidence_urls": previous.get("evidence_urls", []) + [wayback_view_url(timestamp, INVESCO_PAGE)]}
            if previous and Decimal(previous["amount"]) != Decimal(row["amount"]):
                row["earlier_capture_amount"] = previous["amount"]
            merged[row["ex_date"]] = row
        facts["captures"].append({"timestamp": timestamp, "evidence_url": wayback_view_url(timestamp, INVESCO_PAGE),
                                  "cache_path": str(path), "sha256": common.sha256_file(path), "rows": len(rows),
                                  "first_ex_date": min(r["ex_date"] for r in rows),
                                  "last_ex_date": max(r["ex_date"] for r in rows), "capture_day": capture_day})
    facts["covered_to"] = max(c["capture_day"] for c in facts["captures"])
    facts["amounts_changed_between_captures"] = {d: r for d, r in merged.items() if "earlier_capture_amount" in r}
    return dict(sorted(merged.items())), facts


_HIGHLIGHTS_HEADER = re.compile(
    r"Financial Highlights\b(?:(?!Financial Highlights).){0,300}?"
    r"(?:Six Months Ended (?P<month>[A-Z][a-z]+) (?P<day>\d{1,2}), (?P<year>\d{4}) (?:\(Unaudited\) )?)?"
    r"Years? Ended September 30, (?P<years>\d{4}(?: \d{4})*)")
_HIGHLIGHTS_DISTRIBUTIONS = re.compile(
    r"Distributions (?:to (?:share|unit)holders )?from: Net investment income "
    r"(?P<values>\( ?\d+\.\d+ ?\)(?: \( ?\d+\.\d+ ?\))*)")


def parse_distribution_highlights(text: str) -> list[dict]:
    """Distributions per share by period from a QQQ Trust report's Financial Highlights table.

    Fiscal years end September 30; a semi-annual report adds the six months
    ending at its date. Returns [{period, start, end, per_share}], newest
    first as printed, or [] when no table with matching counts is found.
    """
    for header in _HIGHLIGHTS_HEADER.finditer(text):
        found = _HIGHLIGHTS_DISTRIBUTIONS.search(text, header.end(), header.end() + 2500)
        if not found:
            continue
        values = re.findall(r"\d+\.\d+", found.group("values"))
        periods = []
        if header.group("year"):
            end = datetime.strptime(f"{header.group('month')} {header.group('day')} {header.group('year')}",
                                    "%B %d %Y").date()
            start = date(end.year if end.month >= 10 else end.year - 1, 10, 1)
            periods.append((f"six months to {end.isoformat()}", start, end))
        periods += [(f"FY{year}", date(int(year) - 1, 10, 1), date(int(year), 9, 30))
                    for year in header.group("years").split()]
        if len(values) == len(periods):
            return [{"period": label, "start": start.isoformat(), "end": end.isoformat(), "per_share": value}
                    for (label, start, end), value in zip(periods, values)]
    return []


def qqq_sec_reports() -> tuple[list[dict], list[dict]]:
    """The QQQ_SEC_REPORTS documents (through SEC_LIMITER and sec_headers) and their per-share periods."""
    def get(url: str, path: Path) -> bytes:
        return common.cached_get(url, path, source="sec", headers=common.sec_headers(), limiter=common.SEC_LIMITER)

    submissions_url = f"https://data.sec.gov/submissions/CIK{QQQ_TRUST_CIK}.json"
    recent = json.loads(get(submissions_url, QQQ_EVIDENCE / "sec" / f"CIK{QQQ_TRUST_CIK}.json"))["filings"]["recent"]
    position = {accession: i for i, accession in enumerate(recent["accessionNumber"])}
    reports, periods = [], []
    for accession in QQQ_SEC_REPORTS:
        i = position[accession]
        name = recent["primaryDocument"][i]
        url = f"https://www.sec.gov/Archives/edgar/data/{int(QQQ_TRUST_CIK)}/{accession.replace('-', '')}/{name}"
        path = QQQ_EVIDENCE / "sec" / accession / name
        found = parse_distribution_highlights(html_text(get(url, path)))
        if not found:
            raise ValueError(f"{accession}: no Financial Highlights distributions found")
        reports.append({"accession": accession, "form": recent["form"][i], "filing_date": recent["filingDate"][i],
                        "url": url, "cache_path": str(path), "sha256": common.sha256_file(path),
                        "periods": {p["period"]: p["per_share"] for p in found}})
        periods += [{**p, "accession": accession, "url": url} for p in found]
    return reports, periods


def fiscal_period_checks(joined: list[dict], periods: list[dict]) -> list[dict]:
    """Each reported period fully inside the join: the joined dividends with ex-dates in it, their sum,
    and whether the sum equals the reported per-share distributions to the cent."""
    dividends = [(r["date"], Decimal(r["dividend"])) for r in joined if r["dividend"] != "0"]
    first, last = joined[0]["date"], joined[-1]["date"]
    merged: dict[str, dict] = {}
    for period in periods:
        entry = merged.setdefault(period["period"], {"period": period["period"], "start": period["start"],
                                                     "end": period["end"], "reports": {}})
        entry["reports"][period["accession"]] = period["per_share"]
    checks = []
    for entry in sorted(merged.values(), key=lambda e: (e["end"], e["start"])):
        if entry["start"] < first or entry["end"] > last:
            continue
        stated = sorted(set(entry["reports"].values()))
        reported = Decimal(stated[0])
        inside = [(d, a) for d, a in dividends if entry["start"] <= d <= entry["end"]]
        total = sum((a for _, a in inside), Decimal(0))
        checks.append({**entry, "per_share": stated[0], "reports_agree": len(stated) == 1,
                       "ex_dates": [d for d, _ in inside], "dividend_sum": str(total),
                       "dividend_sum_to_cent": str(total.quantize(Decimal("0.01"), ROUND_HALF_UP)),
                       "matches": abs(total - reported) <= Decimal("0.005")})
    return checks


QQQ_CHECK_COLUMNS = ["ex_date", "amount", "dividend_source", "quarter", "dividends_in_quarter",
                     "issuer_amount", "issuer_diff", "issuer_record_date", "issuer_pay_date", "issuer_captures",
                     "issuer_match", "nasdaq_api_amount", "nasdaq_api_record_date", "nasdaq_api_payment_date",
                     "nasdaq_api_match", "tiingo_amount", "tiingo_match", "fiscal_period", "fiscal_period_per_share",
                     "fiscal_period_match", "status"]
DIVIDEND_TOLERANCE = Decimal("0.001")  # plan section 6: dividend amounts agree within $0.001


def _same(a: str | None, b: str | None) -> str:
    """'Y' when both amounts exist and agree within DIVIDEND_TOLERANCE, 'N' when they differ by more,
    '' when either is missing. Exact differences are reported separately."""
    if a in (None, "") or b in (None, ""):
        return ""
    return "Y" if abs(Decimal(a) - Decimal(b)) <= DIVIDEND_TOLERANCE else "N"


def qqq_dividend_verification(joined: list[dict], issuer: dict[str, dict], issuer_covered_to: str,
                              api: dict[str, dict], tiingo: list[dict],
                              period_checks: list[dict]) -> tuple[list[dict], dict]:
    """One row per dividend in the join, checked against the issuer table (through its capture date),
    the Nasdaq dividend API, Tiingo where its file covers the date, and the fiscal-period totals.

    status: issuer_confirmed, issuer_differs, fiscal_total_confirmed (issuer table not covering the
    date, the period total matching) or vendor_only.
    """
    dividends = [r for r in joined if r["dividend"] != "0"]
    per_quarter = Counter(_quarter(r["date"]) for r in dividends)
    tiingo_span = (tiingo[0]["date"], tiingo[-1]["date"])
    tiingo_divs = {r["date"]: r["divCash"] for r in tiingo if Decimal(r["divCash"] or "0") != 0}
    api_first = min(api) if api else None
    rows = []
    for row in dividends:
        day = row["date"]
        found = issuer.get(day, {})
        covered = day <= issuer_covered_to
        period = next((p for p in period_checks if p["start"] <= day <= p["end"]), None)
        nasdaq = api.get(day, {})
        check = {
            "ex_date": day, "amount": row["dividend"], "dividend_source": row["dividend_source"],
            "quarter": _quarter(day), "dividends_in_quarter": per_quarter[_quarter(day)],
            "issuer_amount": found.get("amount", ""),
            "issuer_diff": str(Decimal(row["dividend"]) - Decimal(found["amount"])) if found else "",
            "issuer_record_date": found.get("record_date", ""),
            "issuer_pay_date": found.get("pay_date", ""), "issuer_captures": ";".join(found.get("captures", [])),
            "issuer_match": (_same(row["dividend"], found.get("amount")) or "N") if covered else "",
            "nasdaq_api_amount": nasdaq.get("amount", ""), "nasdaq_api_record_date": nasdaq.get("record_date", ""),
            "nasdaq_api_payment_date": nasdaq.get("payment_date", ""),
            "nasdaq_api_match": ((_same(row["dividend"], nasdaq.get("amount")) or "N")
                                 if api_first and day >= api_first else ""),
            "tiingo_amount": tiingo_divs.get(day, ""),
            "tiingo_match": ((_same(row["dividend"], tiingo_divs.get(day)) or "N")
                             if tiingo_span[0] <= day <= tiingo_span[1] else ""),
            "fiscal_period": period["period"] if period else "",
            "fiscal_period_per_share": period["per_share"] if period else "",
            "fiscal_period_match": ("Y" if period["matches"] else "N") if period else "",
        }
        check["status"] = ("issuer_confirmed" if check["issuer_match"] == "Y" else
                           "issuer_differs" if check["issuer_match"] == "N" else
                           "fiscal_total_confirmed" if check["fiscal_period_match"] == "Y" else "vendor_only")
        rows.append(check)
    joined_days = {r["date"] for r in dividends}
    first, last = joined[0]["date"], min(joined[-1]["date"], issuer_covered_to)
    several = {}
    for check in rows:
        if check["dividends_in_quarter"] > 1:
            found = issuer.get(check["ex_date"], {})
            several.setdefault(check["quarter"], []).append({
                "ex_date": check["ex_date"], "amount": check["amount"], "status": check["status"],
                "issuer_record_date": check["issuer_record_date"], "issuer_pay_date": check["issuer_pay_date"],
                "issuer_evidence_urls": found.get("evidence_urls", []),
                "nasdaq_api_amount": check["nasdaq_api_amount"], "tiingo_amount": check["tiingo_amount"],
                "fiscal_period": check["fiscal_period"], "fiscal_period_per_share": check["fiscal_period_per_share"],
                "fiscal_period_match": check["fiscal_period_match"]})
    for quarter, items in several.items():
        for item in items:  # whether the fiscal total also matches without this distribution
            period = next((p for p in period_checks if p["period"] == item["fiscal_period"]), None)
            if period:
                without = Decimal(period["dividend_sum"]) - Decimal(item["amount"])
                item["fiscal_period_matches_without_it"] = abs(without - Decimal(period["per_share"])) <= Decimal(
                    "0.005")
    summary = {
        "dividends_checked": len(rows), "status_counts": dict(Counter(r["status"] for r in rows)),
        "tolerance": str(DIVIDEND_TOLERANCE),
        "issuer_differs": [r for r in rows if r["status"] == "issuer_differs"],
        "issuer_nonzero_differences_within_tolerance": [
            {k: r[k] for k in ("ex_date", "amount", "issuer_amount", "issuer_diff", "nasdaq_api_amount",
                               "tiingo_amount")}
            for r in rows if r["issuer_match"] == "Y" and Decimal(r["issuer_diff"]) != 0],
        "issuer_dividends_missing_from_join": sorted(d for d in issuer if first <= d <= last and d not in joined_days),
        "nasdaq_api_differs_or_missing": [r["ex_date"] for r in rows if r["nasdaq_api_match"] == "N"],
        "tiingo_differs": [r["ex_date"] for r in rows if r["tiingo_match"] == "N"],
        "fiscal_periods": period_checks,
        "fiscal_periods_not_matching": [p["period"] for p in period_checks if not p["matches"]],
        "quarters_with_several_dividends": several,
        "vendor_only": [r["ex_date"] for r in rows if r["status"] == "vendor_only"],
    }
    return rows, summary


def qqq_dividend_sources_check(tiingo: list[dict], nasdaq: list[dict], api: dict[str, dict]) -> dict:
    """The fresh Nasdaq dividend payload against the stored files over each file's span (ex-dates and amounts)."""
    def compare(file_divs: dict[str, str], lo: str, hi: str) -> dict:
        fresh = {d: v["amount"] for d, v in api.items() if lo <= d <= hi}
        both = set(file_divs) & set(fresh)
        return {"from": lo, "to": hi, "file_dividends": len(file_divs), "api_dividends": len(fresh),
                "dates_only_file": sorted(set(file_divs) - set(fresh)),
                "dates_only_api": sorted(set(fresh) - set(file_divs)),
                "amounts_differ": sorted(d for d in both if Decimal(file_divs[d]) != Decimal(fresh[d])),
                "matched": len([d for d in both if Decimal(file_divs[d]) == Decimal(fresh[d])])}

    api_first = min(api)
    t_lo, t_hi = max(api_first, tiingo[0]["date"]), min(tiingo[-1]["date"], "2017-12-31")
    n_lo, n_hi = nasdaq[0]["date"], nasdaq[-1]["date"]
    return {
        "api_first_ex_date": api_first, "api_last_ex_date": max(api),
        "tiingo_file_vs_api": compare({r["date"]: r["divCash"] for r in tiingo if t_lo <= r["date"] <= t_hi
                                       and Decimal(r["divCash"] or "0") != 0}, t_lo, t_hi),
        "nasdaq_file_vs_api": compare({r["date"]: r["cash_dividend"] for r in nasdaq
                                       if Decimal(r["cash_dividend"] or "0") != 0}, n_lo, n_hi),
    }


# ---------------------------------------------------------------- writing


def write_csv(path: Path, header: list[str], rows: list[list] | list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([row[k] for k in header] if isinstance(row, dict) else row)
    data = buffer.getvalue().encode("utf-8")
    common.atomic_write(path, gzip.compress(data, mtime=0) if path.suffix == ".gz" else data)
    return common.sha256_file(path)


def scout_comparison(name: str, sha256: str) -> dict:
    """The scout's copy against this download; its HTTP stamps describe this file only when the bytes match."""
    copy = SCOUT_KF / name
    if not copy.exists():
        return {"scout_copy": None}
    facts = {"scout_copy": str(copy), "scout_sha256_matches": common.sha256_file(copy) == sha256}
    headers = copy.with_suffix(".headers")
    if headers.exists() and not facts["scout_sha256_matches"]:
        facts["scout_http_headers"] = "not attached: the scout copy's bytes differ from this download"
    elif headers.exists():
        for line in headers.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition(":")
            if key.strip().lower() in ("last-modified", "etag", "date"):
                facts[f"scout_http_{key.strip().lower().replace('-', '_')}"] = value.strip()
    return facts


QQQ_JOINED_COLUMNS = ["date", "close", "dividend", "close_source", "dividend_source", "in_window"]


def build_qqq_inputs(sessions: list[str]) -> dict:
    """Fetch the QQQ tail and dividends, check the dividends against the issuer, write the joined inputs.

    A dividend in a quarter with several dividends is dropped from the join
    unless the issuer table confirms it (date and amount).
    """
    tiingo, nasdaq = _read_csv(QQQ_TIINGO), _read_csv(QQQ_NASDAQ)
    tail, tail_facts = fetch_qqq_tail()
    api, api_facts = fetch_qqq_dividends()
    tail_dividends = {day: facts["amount"] for day, facts in api.items()}
    issuer, issuer_facts = qqq_issuer_distributions()
    reports, periods = qqq_sec_reports()

    unchecked = qqq_total_return_join(tiingo, nasdaq, tail, tail_dividends)
    period_checks = fiscal_period_checks(unchecked, periods)
    check_rows, verification = qqq_dividend_verification(unchecked, issuer, issuer_facts["covered_to"], api, tiingo,
                                                         period_checks)
    drop = {r["ex_date"]: "not confirmed by the issuer table" for r in check_rows
            if r["dividends_in_quarter"] > 1 and r["status"] != "issuer_confirmed"}
    after_end = (date.fromisoformat(WINDOW_END) + timedelta(days=1)).isoformat()
    checks = qqq_coverage_check(QQQ_TIINGO, QQQ_NASDAQ, sessions=sessions, tail=tail, tail_dividends=tail_dividends,
                                sessions_after=xnas_sessions(after_end, tail[-1]["date"]), drop=drop)
    joined = qqq_total_return_join(tiingo, nasdaq, tail, tail_dividends, drop)

    joined_path, checks_path = FACTORS / "qqq_joined.csv", FACTORS / "qqq_dividend_checks.csv"
    joined_sha = write_csv(joined_path, QQQ_JOINED_COLUMNS, joined)
    checks_sha = write_csv(checks_path, QQQ_CHECK_COLUMNS, check_rows)
    by_source = Counter(r["close_source"] for r in joined)

    def span(source: str) -> dict:
        dates = [r["date"] for r in joined if r["close_source"] == source]
        return {"rows_used": by_source[source], "first_used": dates[0] if dates else None,
                "last_used": dates[-1] if dates else None}

    sources = {
        "output": str(joined_path), "sha256": joined_sha, "rows": len(joined), "columns": QQQ_JOINED_COLUMNS,
        "computes": ("nothing: these are the inputs of a QQQ total return, not returns. The repo loader "
                     "scripts/research_sue_lt_2020_2026.py qqq_returns reads the same columns as "
                     "(close + dividend) / previous close - 1."),
        "close": "raw (unadjusted) close; QQQ has no split in the joined span (tiingo splitFactor is 1 throughout)",
        "dividend": "cash dividend per share on its ex-date as published, '0' on other days",
        "join_rule": checks["joined"]["rule"],
        "first_row": ("the last session before the window start, kept so the first window day has a previous "
                      "close (in_window=N)"),
        "rows_after_window_end": "kept to the latest published session (in_window=N)",
        "sources": {
            "tiingo_file": {"path": str(QQQ_TIINGO), "sha256": common.sha256_file(QQQ_TIINGO), **span("tiingo_file")},
            "nasdaq_file": {"path": str(QQQ_NASDAQ), "sha256": common.sha256_file(QQQ_NASDAQ), **span("nasdaq_file")},
            "nasdaq_api_history": {**tail_facts, **span("nasdaq_api_history")},
            "nasdaq_api_dividends": api_facts,
        },
        "dividend_evidence": {"checks_file": str(checks_path), "checks_sha256": checks_sha,
                              "issuer": issuer_facts, "sec_reports": reports,
                              "status_counts": verification["status_counts"],
                              "quarters_with_several_dividends": verification["quarters_with_several_dividends"],
                              "dropped": checks["joined"]["dropped_dividends"]},
    }
    common.atomic_write(FACTORS / "qqq_joined_sources.json", (json.dumps(sources, indent=2) + "\n").encode("utf-8"))
    checks["dividend_sources"] = qqq_dividend_sources_check(tiingo, nasdaq, api)
    checks["dividend_verification"] = verification
    checks["outputs"] = {"joined": str(joined_path), "joined_sha256": joined_sha, "dividend_checks": str(checks_path),
                         "dividend_checks_sha256": checks_sha,
                         "sources": str(FACTORS / "qqq_joined_sources.json")}
    open_items = []
    if not checks["joined"]["covers_window"]:
        open_items.append("the join does not cover every window session")
    if verification["vendor_only"]:
        open_items.append(f"dividends resting on the Nasdaq API alone (after the latest issuer capture and the "
                          f"latest reported fiscal period): {verification['vendor_only']}")
    for key in ("issuer_differs", "issuer_dividends_missing_from_join", "fiscal_periods_not_matching"):
        if verification[key]:
            open_items.append(f"{key}: {verification[key]}")
    checks["open_items"] = open_items
    return checks


def run() -> dict:
    sessions = xnas_sessions(WINDOW_START, WINDOW_END)
    sources, checks = {"fetched_note": "first download is pinned; cached_get never refreshes it"}, {}

    library, error = fetch_kf_page("data_library.html")
    sources["links_verified"] = {name: linked(library, name) for name in KF_DATA_ZIPS.values()}
    sources["page_errors"] = {"data_library.html": error} if error else {}
    for scheme, page in KF_SIC_PAGES.items():
        html, error = fetch_kf_page(page)
        sources["links_verified"][KF_SIC_ZIPS[scheme]] = linked(html, KF_SIC_ZIPS[scheme])
        if error:
            sources["page_errors"][page] = error

    # Factor and industry files.
    series_dates, ind49_columns = {}, None
    for key, name in KF_DATA_ZIPS.items():
        data, path = fetch_kf(name)
        member, text = zip_text(data)
        parsed = parse_kf_daily(text)
        sha = common.sha256_bytes(data)
        if key == "ind49_daily":
            rows = tidy_industry_rows(parsed)  # raises unless one vw and one ew block with the same columns
            out = FACTORS / "ind49_daily.csv.gz"
            out_sha = write_csv(out, ["date", "weighting", "industry_id", "series", "value_pct", "value_dec",
                                      "missing"], rows)
            ind49_columns = parsed["blocks"][0]["columns"]
        else:
            rows = tidy_factor_rows(parsed)
            out = FACTORS / f"{key}.csv"
            out_sha = write_csv(out, ["date", "series", "value_pct", "value_dec", "missing"], rows)
        sources[name] = {"url": KF_FTP + name, "cache_path": str(path), "sha256": sha, "bytes": len(data), **member,
                         "crsp_build": parsed["crsp_build"], "stamp_line": parsed["stamp_line"],
                         "preamble": parsed["preamble"], "footer": parsed["footer"],
                         "blocks": [{"title": b["title"], "weighting": weighting(b["title"]), "columns": b["columns"],
                                     "rows": len(b["dates"]), "first": iso(b["dates"][0]),
                                     "last": iso(b["dates"][-1])} for b in parsed["blocks"]],
                         "tidy_output": str(out), "tidy_rows": len(rows), "tidy_sha256": out_sha,
                         **scout_comparison(name, sha)}
        for block in parsed["blocks"]:
            dates = [iso(d) for d in block["dates"]]
            missing = {iso(d) for d, values in zip(block["dates"], block["values"]) if any(map(is_missing, values))}
            label = f"{key}:{weighting(block['title'])}" if key == "ind49_daily" else key
            checks[label] = window_facts(dates, missing, sessions)
            series_dates[label] = [d for d in dates if d >= WINDOW_START]
    reference = series_dates["ff5_2x3_daily"]
    checks["same_dates_in_window_as_ff5"] = {label: dates == reference for label, dates in series_dates.items()}

    # Siccodes.
    map_rows = []
    for scheme, name in KF_SIC_ZIPS.items():
        data, path = fetch_kf(name)
        member, text = zip_text(data)
        rows = parse_siccodes(text, scheme)
        map_rows.extend(rows)
        sha = common.sha256_bytes(data)
        overlaps = sic_overlaps(rows)
        sources[name] = {"url": KF_FTP + name, "cache_path": str(path), "sha256": sha, "bytes": len(data), **member,
                         "industries": len({r["industry_id"] for r in rows}),
                         "ranges": len([r for r in rows if r["sic_lo"] != ""]),
                         "industries_without_ranges": sorted({r["short_name"] for r in rows if r["sic_lo"] == ""}),
                         "overlapping_range_pairs": len(overlaps), "overlaps": overlaps[:50],
                         **scout_comparison(name, sha)}
    ff49_names = [r["short_name"] for r in map_rows if r["scheme"] == "FF49"]
    ff49_order = list(dict.fromkeys(ff49_names))
    checks["ind49_columns_match_siccodes49"] = ind49_columns == ff49_order
    inputs_path = common.INPUTS / "ff_industry_maps.csv"
    sources["ff_industry_maps"] = {
        "path": str(inputs_path), "rows": len(map_rows),
        "sha256": write_csv(inputs_path, ["scheme", "industry_id", "short_name", "sic_lo", "sic_hi"], map_rows),
        "full_path": str(FACTORS / "ff_industry_maps_full.csv"),
        "full_sha256": write_csv(FACTORS / "ff_industry_maps_full.csv",
                                 ["scheme", "industry_id", "short_name", "industry_name", "sic_lo", "sic_hi",
                                  "range_description"], map_rows)}

    # CBOE VIX.
    page_text, error = fetch_page(CBOE_VIX_PAGE, RAW_VIX / "vix_historical_data.html", "cboe", CBOE_LIMITER)
    if error:
        sources["page_errors"]["cboe_vix_page"] = error
    vix_data = fetch(CBOE_VIX_CSV, RAW_VIX / "VIX_History.csv", "cboe", CBOE_LIMITER)
    vix_rows = parse_vix_csv(vix_data.decode("utf-8"))
    vix_by_date = {r["date"]: r for r in vix_rows}
    # The URL the official page itself links (a different CBOE host from the plan's), fetched to compare bytes.
    linked_urls = sorted(set(re.findall(r'https?://[^"\'\s>]*/VIX_History\.csv', page_text)))
    linked_facts = {}
    for url in linked_urls:
        if url == CBOE_VIX_CSV:
            linked_facts[url] = {"same_url_as_primary": True}
            continue
        host = re.sub(r"[^A-Za-z0-9.-]", "_", url.split("/")[2])
        other = fetch(url, RAW_VIX / host / "VIX_History.csv", "cboe", CBOE_LIMITER)
        other_rows = parse_vix_csv(other.decode("utf-8"))
        overlap = [r for r in other_rows if r["date"] in vix_by_date]
        linked_facts[url] = {"sha256": common.sha256_bytes(other), "bytes": len(other),
                             "same_bytes_as_primary": other == vix_data, "rows": len(other_rows),
                             "last": other_rows[-1]["date"],
                             "overlap_rows_differing": len([r for r in overlap if r != vix_by_date[r["date"]]])}
    all_sessions = set(xnas_sessions(vix_rows[0]["date"], vix_rows[-1]["date"]))
    for row in vix_rows:
        row["xnas_session"] = "Y" if row["date"] in all_sessions else "N"
    vix_out = FACTORS / "vix_daily.csv"
    sources["VIX_History.csv"] = {
        "url": CBOE_VIX_CSV, "official_page": CBOE_VIX_PAGE,
        "official_page_links_csv": "VIX_History.csv" in page_text,
        "official_page_link_urls": linked_urls, "linked_url_comparison": linked_facts,
        "cache_path": str(RAW_VIX / "VIX_History.csv"), "sha256": common.sha256_bytes(vix_data),
        "bytes": len(vix_data), "rows": len(vix_rows), "first": vix_rows[0]["date"], "last": vix_rows[-1]["date"],
        "rows_not_xnas_sessions": len([r for r in vix_rows if r["xnas_session"] == "N"]),
        "known_close_checks": {d: (vix_by_date.get(d, {}).get("close"), str(v),
                                   d in vix_by_date and Decimal(vix_by_date[d]["close"]) == v)
                               for d, v in VIX_KNOWN_CLOSES.items()},
        "tidy_output": str(vix_out),
        "tidy_sha256": write_csv(vix_out, ["date", "open", "high", "low", "close", "xnas_session"], vix_rows)}
    checks["vix"] = vix_window_checks(vix_rows, sessions)

    checks["qqq"] = build_qqq_inputs(sessions)
    checks["window"] = {"start": WINDOW_START, "end": WINDOW_END, "xnas_sessions": len(sessions),
                        "first_session": sessions[0], "last_session": sessions[-1]}
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sources["generated_utc"] = checks["generated_utc"] = stamp
    common.atomic_write(FACTORS / "kf_sources.json", (json.dumps(sources, indent=2) + "\n").encode("utf-8"))
    common.atomic_write(FACTORS / "step1_checks.json", (json.dumps(checks, indent=2) + "\n").encode("utf-8"))
    return {"sources": sources, "checks": checks}


def main() -> None:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    result = run()
    sources, checks = result["sources"], result["checks"]
    for name in list(KF_DATA_ZIPS.values()) + list(KF_SIC_ZIPS.values()) + ["VIX_History.csv"]:
        facts = sources[name]
        print(f"{name}: sha256={facts['sha256']} bytes={facts['bytes']} build={facts.get('crsp_build')} "
              f"scout_match={facts.get('scout_sha256_matches')}")
    for label, facts in checks.items():
        if isinstance(facts, dict) and "last_date" in facts and "row_check" in facts:
            print(f"{label}: {facts['first_date']}..{facts['last_date']} rows_in_window={facts['rows_in_window']} "
                  f"missing_markers={facts['rows_with_missing_marker_in_window']} "
                  f"missing_sessions={len(facts['missing_sessions'])} extra_dates={len(facts['extra_dates'])} "
                  f"row_check={facts['row_check']['found']}/{facts['row_check']['expected']}")
    print("links verified:", sources["links_verified"])
    print("ind49 columns match Siccodes49:", checks["ind49_columns_match_siccodes49"])
    qqq = checks["qqq"]
    joined, tail, verification = qqq["joined"], qqq["tail"], qqq["dividend_verification"]
    print(f"QQQ joined: {joined['first_row']}..{joined['last_date']} rows={joined['rows']} "
          f"in_window={joined['rows_in_window']} covers_window={joined['covers_window']} "
          f"missing={joined['missing_sessions_to_window_end']} by_source={joined['rows_by_close_source']}")
    print(f"QQQ tail: {tail['first']}..{tail['last']} rows={tail['rows']} added={tail['rows_after_stored_file']} "
          f"overlap_max_close_diff={tail['overlap_with_stored_file']['max_abs_close_diff']} "
          f"dividends_added={tail['dividends_after_stored_file']}")
    print(f"QQQ dividends: {verification['status_counts']} "
          f"fiscal_periods_not_matching={verification['fiscal_periods_not_matching']} "
          f"dropped={joined['dropped_dividends']}")
    for quarter, items in verification["quarters_with_several_dividends"].items():
        print(f"  {quarter}: " + "; ".join(f"{i['ex_date']} {i['amount']} {i['status']}" for i in items))
    print("QQQ open items:", qqq["open_items"] or "none")
    print(f"wrote {FACTORS / 'kf_sources.json'}, {FACTORS / 'step1_checks.json'}, {qqq['outputs']['joined']}, "
          f"{qqq['outputs']['dividend_checks']} and {qqq['outputs']['sources']}")


if __name__ == "__main__":
    main()
