"""Plan step 1: Ken French factor and industry files, the Siccodes maps, CBOE VIX, and a QQQ coverage check.

See docs/reversal_2012_2026_data_plan.md (step 1, sections 5.2 and 5.3).

Data only. This script downloads published files, parses them into tidy
CSVs and checks dates and coverage. It computes no strategy, portfolio or
long-short return, no signal and no ranking by return. The QQQ check
compares dates, closes and dividends between two stored files; it does not
compute returns.

Every request goes through ``cached_get``, so a second run reads the cache
and fetches nothing. The Ken French files are revised monthly; the first
download is the pinned copy (its sha256 is recorded) and is not refreshed.

Outputs
- CACHE/raw/kf/*.zip and CACHE/raw/kf/pages/*.html (as downloaded)
- CACHE/raw/vix/VIX_History.csv and the CBOE page that links it
- CACHE/factors/{ff5_2x3_daily,mom_daily,st_rev_daily}.csv,
  CACHE/factors/ind49_daily.csv.gz, CACHE/factors/vix_daily.csv,
  CACHE/factors/ff_industry_maps_full.csv, CACHE/factors/kf_sources.json,
  CACHE/factors/step1_checks.json
- INPUTS/ff_industry_maps.csv (scheme, industry_id, short_name, sic_lo, sic_hi)
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import gzip
import io
import json
from pathlib import Path
import re
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

HEADERS = {"User-Agent": "quant_stocks research data acquisition (python urllib)"}
KF_LIMITER = common.SlidingWindowLimiter({1: 1})
CBOE_LIMITER = common.SlidingWindowLimiter({1: 1})

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


def tidy_industry_rows(parsed: dict) -> list[list[str]]:
    """Long rows: date, weighting, industry_id (column order, 1-based), series, value_pct, value_dec, missing."""
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


# ---------------------------------------------------------------- QQQ check (no returns)


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


def qqq_coverage_check(tiingo_path: Path = QQQ_TIINGO, nasdaq_path: Path = QQQ_NASDAQ,
                       sessions: list[str] | None = None) -> dict:
    """Dates, dividends and closes of the two stored QQQ files and of their join (no returns)."""
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
    # The join used by the plan: Tiingo before 2018, Nasdaq from 2018-01-02.
    joined = ([{"date": r["date"], "div": float(r["divCash"])} for r in tiingo if r["date"] < QQQ_JOIN_SWITCH]
              + [{"date": r["date"], "div": float(r["cash_dividend"])} for r in nasdaq if r["date"] >= QQQ_JOIN_SWITCH])
    joined_dates = [r["date"] for r in joined if WINDOW_START <= r["date"] <= WINDOW_END]
    diff = session_diff(joined_dates, sessions)
    dividend_days = [r["date"] for r in joined if r["div"] != 0 and WINDOW_START <= r["date"] <= WINDOW_END]
    per_quarter: dict[str, list[str]] = {}
    for day in dividend_days:
        per_quarter.setdefault(_quarter(day), []).append(day)
    lo, hi, expected = QQQ_DIVIDEND_COUNT
    last = joined_dates[-1]
    result["joined"] = {
        "rule": f"tiingo before {QQQ_JOIN_SWITCH}, nasdaq from {QQQ_JOIN_SWITCH}",
        "first_date_in_window": joined_dates[0], "last_date": last,
        "covers_window": joined_dates[0] == sessions[0] and last >= sessions[-1],
        "missing_sessions_to_window_end": diff["missing_sessions"],
        "missing_sessions_before_last_date": [d for d in diff["missing_sessions"] if d <= last],
        "extra_dates": diff["extra_dates"],
        "dividends_in_window": len(dividend_days),
        "dividend_count_check": {"from": lo, "to": hi, "expected": expected,
                                 "found": len([d for d in dividend_days if lo <= d <= hi])},
        "quarters_without_dividend": [q for q in _quarters(WINDOW_START, last)
                                      if q not in {_quarter(d) for d in dividend_days}
                                      and q != _quarter(last)],
        "quarters_with_several_dividends": {q: days for q, days in per_quarter.items() if len(days) > 1},
        "dividend_2020_09_21": next((r["div"] for r in joined if r["date"] == "2020-09-21"), None),
    }
    return result


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
    copy = SCOUT_KF / name
    if not copy.exists():
        return {"scout_copy": None}
    facts = {"scout_copy": str(copy), "scout_sha256_matches": common.sha256_file(copy) == sha256}
    headers = copy.with_suffix(".headers")
    if headers.exists():
        for line in headers.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition(":")
            if key.strip().lower() in ("last-modified", "etag", "date"):
                facts[f"scout_http_{key.strip().lower().replace('-', '_')}"] = value.strip()
    return facts


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
            rows = tidy_industry_rows(parsed)
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
    vix_dates = [r["date"] for r in vix_rows]
    checks["vix"] = window_facts(vix_dates, set(), sessions)
    checks["vix"]["extra_dates_on_weekends"] = [d for d in checks["vix"]["extra_dates"]
                                                if date.fromisoformat(d).weekday() >= 5]
    checks["vix"]["note"] = "rows on dates that are not XNAS sessions are flagged xnas_session=N in vix_daily.csv"

    checks["qqq"] = qqq_coverage_check(sessions=sessions)
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
    print("QQQ joined:", json.dumps({k: v for k, v in checks["qqq"]["joined"].items()}, default=str)[:1500])
    print(f"wrote {FACTORS / 'kf_sources.json'} and {FACTORS / 'step1_checks.json'}")


if __name__ == "__main__":
    main()
