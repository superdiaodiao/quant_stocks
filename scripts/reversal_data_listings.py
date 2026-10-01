"""Step 2 of docs/reversal_2012_2026_data_plan.md: Nasdaq listing snapshots, 2011-2019, from the Wayback Machine.

Data only. This recovers archived copies of two public listing files and
normalises them; it computes no signal and no return.

* Nasdaq Trader symbol directory (``nasdaqlisted.txt``, pipe-delimited),
  dated by its "File Creation Time" footer and normalised with
  ``src.io.nasdaq_update.import_nasdaq_trader_files``.
* nasdaq.com company list (``companies-by-name.aspx`` /
  ``companies-by-industry.aspx`` with ``exchange=nasdaq&render=download``),
  which carries LastSale and MarketCap, dated by the capture timestamp.

Captures are listed with the CDX API and fetched in their raw ``id_`` form,
one request every few seconds, through ``cached_get`` so a stopped run
resumes and every request is logged.  Outputs:

* ``CACHE/raw/wayback/cdx/*.json`` and ``CACHE/raw/wayback/{ts}__{slug}.txt|csv``
* ``CACHE/listings/symdir/nasdaq_listed_{date}.csv``
* ``CACHE/listings/companylist/nasdaq_companylist_{date}.csv``
* ``CACHE/listings/wayback_captures.csv`` (every candidate capture and what happened to it)
* ``CACHE/listings/repo_overlap_check.json`` (Wayback against the repo snapshots)
* ``INPUTS/listing_snapshots_index.csv``
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import date, datetime
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import parse_qsl, quote, urlsplit

import pandas as pd

from scripts.reversal_data_common import (
    CACHE, INPUTS, RAW, SlidingWindowLimiter, atomic_write, cached_get, sha256_bytes, sha256_file,
)
from src.io.nasdaq_update import import_nasdaq_trader_files
from src.io.security_universe import investable_common_equities

SOURCE = "wayback"
WAYBACK_RAW = RAW / "wayback"
LISTINGS = CACHE / "listings"
SYMDIR_DIR = LISTINGS / "symdir"
COMPANYLIST_DIR = LISTINGS / "companylist"
CAPTURES_FILE = LISTINGS / "wayback_captures.csv"
OVERLAP_FILE = LISTINGS / "repo_overlap_check.json"
INDEX_FILE = INPUTS / "listing_snapshots_index.csv"
REPO_SNAPSHOTS = Path("stocks_list_dir/nasdaq/snapshots")
# snapshot_file in the index is relative to the repo root; the cache is linked there under this name.
CACHE_LINK = Path("research_cache") / CACHE.name
INDEX_COLUMNS = ["snapshot_date", "source", "capture_timestamp", "original_url", "rows", "common_rows",
                 "has_market_cap", "sha256", "raw_sha256", "snapshot_file"]
# About one request every three seconds, and never more than 15 in a minute
# (the Internet Archive throttles clients that go faster).
WAYBACK_LIMITER = SlidingWindowLimiter({3: 1, 60: 15})
HEADERS = {"User-Agent": "quant_stocks-research/1.0 (listing snapshot recovery; polite, 1 req/3-4s)",
           "Accept-Encoding": "identity"}
MINIMUM_ROWS = 1000

# (kind, CDX url, matchType).  The CDX key ignores the scheme and "www.", so
# the http and https captures of each come back together.
CDX_TARGETS = [
    ("symdir", "nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "exact"),
    ("symdir", "ftp.nasdaqtrader.com/SymbolDirectory/nasdaqlisted.txt", "exact"),
    ("companylist", "nasdaq.com/screening/companies-by-name.aspx", "prefix"),
    ("companylist", "nasdaq.com/screening/companies-by-industry.aspx", "prefix"),
    ("companylist", "old.nasdaq.com/screening/companies-by-name.aspx", "prefix"),
    ("companylist", "old.nasdaq.com/screening/companies-by-industry.aspx", "prefix"),
]
CDX_FIELDS = ["timestamp", "original", "statuscode", "mimetype", "digest", "length"]
TEXT_MIMETYPES = {"text/plain", "application/text", "text/csv", "application/csv", "application/octet-stream",
                  "application/vnd.ms-excel", "application/x-csv", "text/comma-separated-values"}
# Query keys that make a company-list download a subset of the exchange.
SUBSET_KEYS = {"market", "page", "pagesize", "region", "marketcap", "country", "sector", "ipoyear"}


# --------------------------------------------------------------------------- CDX


def cdx_url(url: str, match_type: str = "exact", from_: str = "2011", to: str = "2019") -> str:
    return (f"https://web.archive.org/cdx/search/cdx?url={quote(url, safe='/:')}&matchType={match_type}"
            f"&from={from_}&to={to}&output=json&fl={','.join(CDX_FIELDS)}")


def slug(text: str, limit: int = 110) -> str:
    """A filesystem-safe name for a URL (host, path and query), with a short hash against collisions."""
    parts = urlsplit(text if "://" in text else "http://" + text)
    host = parts.hostname or ""
    host = host[4:] if host.startswith("www.") else host
    body = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{host}{parts.path}_{parts.query}").strip("-_")
    return f"{body[:limit]}-{sha256_bytes(text.encode())[:8]}"


def parse_cdx_json(data: bytes | str) -> list[dict]:
    """CDX ``output=json`` rows as dicts (the first row is the header); empty input gives []."""
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    if not text.strip():
        return []
    rows = json.loads(text)
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, row)) for row in rows[1:]]


def wayback_cdx(url: str, match_type: str = "exact", from_: str = "2011", to: str = "2019") -> list[dict]:
    query = cdx_url(url, match_type, from_, to)
    cache = WAYBACK_RAW / "cdx" / f"{slug(url)}__{match_type}_{from_}_{to}.json"
    return parse_cdx_json(_get(query, cache))


def query_params(original: str) -> dict[str, str]:
    """Lower-cased query parameters; some archived URLs use "!" where "&" was meant."""
    query = urlsplit(original).query.replace("!", "&")
    return {key.lower(): value.lower() for key, value in parse_qsl(query, keep_blank_values=True)}


def is_full_nasdaq_company_list(original: str) -> bool:
    """True for a download of the whole Nasdaq company list, not a letter, industry or tier subset."""
    params = query_params(original)
    if params.get("render") != "download" or params.get("exchange") != "nasdaq":
        return False
    if params.get("letter", "0") not in ("0", ""):
        return False
    if params.get("industry", "all") not in ("all", ""):
        return False
    return not (SUBSET_KEYS & set(params))


def candidate_reason(kind: str, row: dict) -> str:
    """'' when the CDX row is worth fetching, else why it is skipped."""
    if row.get("statuscode") != "200":
        return f"status_{row.get('statuscode')}"
    if kind == "companylist" and not is_full_nasdaq_company_list(row["original"]):
        return "not_full_nasdaq_list"
    if (row.get("mimetype") or "").lower() not in TEXT_MIMETYPES:
        return f"mimetype_{row.get('mimetype')}"
    return ""


def select_captures(kind: str, rows: list[dict]) -> list[dict]:
    """Every CDX row with an action: fetch the first capture of each digest, skip the rest with a reason."""
    seen: dict[str, str] = {}
    out = []
    for row in sorted(rows, key=lambda r: (r["timestamp"], r["original"])):
        reason = candidate_reason(kind, row)
        if not reason:
            if row["digest"] in seen:
                reason = f"duplicate_digest_of_{seen[row['digest']]}"
            else:
                seen[row["digest"]] = row["timestamp"]
        out.append({**row, "kind": kind, "action": "fetch" if not reason else "skip", "reason": reason})
    return out


# --------------------------------------------------------------------------- fetching


def raw_capture_url(timestamp: str, original: str) -> str:
    return f"https://web.archive.org/web/{timestamp}id_/{original}"


def raw_cache_path(kind: str, timestamp: str, original: str) -> Path:
    suffix = ".txt" if kind == "symdir" else ".csv"
    return WAYBACK_RAW / f"{timestamp}__{slug(original)}{suffix}"


def _get(url: str, cache: Path, attempts: int = 4) -> bytes:
    """cached_get with a pause on Wayback throttling (429/503) instead of a crash."""
    for attempt in range(attempts):
        try:
            return cached_get(url, cache, source=SOURCE, headers=HEADERS, limiter=WAYBACK_LIMITER, timeout=120)
        except HTTPError as exc:
            if exc.code not in (429, 503) or attempt == attempts - 1:
                raise
            pause = 90 * (attempt + 1)
            print(f"  wayback answered {exc.code}; pausing {pause}s", flush=True)
            time.sleep(pause)
        except RuntimeError as exc:  # repeated network errors inside cached_get
            if attempt == attempts - 1:
                raise
            print(f"  {exc}; pausing 60s", flush=True)
            time.sleep(60)
    raise AssertionError("unreachable")


def fetch_wayback_raw(kind: str, timestamp: str, original: str) -> bytes:
    return _get(raw_capture_url(timestamp, original), raw_cache_path(kind, timestamp, original))


# --------------------------------------------------------------------------- parsing


def decode(data: bytes) -> str:
    return data.decode("utf-8-sig", errors="replace")


def symdir_footer(text: str) -> tuple[date | None, str]:
    """(file creation date, raw footer stamp) from a symbol-directory file."""
    match = re.search(r"File Creation Time:\s*(\d{8})(\d{2}:\d{2})?", text)
    if match is None:
        return None, ""
    try:
        return datetime.strptime(match.group(1), "%m%d%Y").date(), match.group(0)
    except ValueError:
        return None, match.group(0)


def normalise_symdir(text: str, minimum_rows: int | None = None) -> tuple[pd.DataFrame | None, str]:
    """The symbol file as import_nasdaq_trader_files normalises it, or (None, reason).

    The text is copied into a fresh temporary directory first so that the
    importer's git lookup finds no repository and records no misleading
    commit; provenance columns are set by the caller.
    """
    minimum_rows = MINIMUM_ROWS if minimum_rows is None else minimum_rows
    with tempfile.TemporaryDirectory(prefix="symdir_") as tmp:
        source = Path(tmp) / "in" / "nasdaqlisted.txt"
        source.parent.mkdir()
        source.write_text(text, encoding="utf-8")
        result = import_nasdaq_trader_files([source], minimum_rows=minimum_rows, snapshot_dir=Path(tmp) / "out")
        if not result["imported"]:
            skipped = result["skipped"][0] if result["skipped"] else {"reason": "not_imported"}
            return None, str(skipped.get("reason", "not_imported"))
        frame = pd.read_csv(result["imported"][0]["snapshot"], dtype=str, keep_default_na=False)
    frame = frame.drop(columns=[c for c in ("Source File", "Source Repository", "Source Commit") if c in frame])
    return frame, ""


_MCAP = re.compile(r"^\$?\s*([0-9]*\.?[0-9]+)\s*([KMBT]?)$", re.IGNORECASE)
_SCALE = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def parse_market_cap(value) -> float | None:
    """Dollars from '532880000', '532880000.5', '$532.88M', '$1.2B' or '$120K'; None for n/a or blank."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text.lower() in ("", "n/a", "na", "nan", "none", "-", "--"):
        return None
    match = _MCAP.match(text)
    if match is None:
        return None
    return float(match.group(1)) * _SCALE[match.group(2).upper()]


def parse_number(value) -> float | None:
    text = "" if value is None else str(value).strip().replace(",", "").replace("$", "")
    if text.lower() in ("", "n/a", "na", "nan", "none", "-", "--"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


COMPANYLIST_COLUMNS = ["Symbol", "Name", "LastSale", "MarketCap", "MarketCap Raw", "ADR TSO", "IPOyear",
                       "Sector", "Industry"]


def parse_company_list(text: str) -> pd.DataFrame:
    """The nasdaq.com company-list CSV with MarketCap in dollars; raises ValueError if it is not one.

    Handles the quoted header with a trailing comma (an empty last column),
    the 2011-2014 plain-number MarketCap and the later '$1.2B' form, and the
    lower-case 'industry' header of later files.
    """
    stripped = text.lstrip()
    if not stripped or stripped[:1] == "<" or "symbol" not in stripped[:200].lower():
        raise ValueError("not a company-list CSV")
    frame = pd.read_csv(io.StringIO(stripped), dtype=str, keep_default_na=False)
    frame = frame.loc[:, [c for c in frame.columns if c and not str(c).startswith("Unnamed")]]
    rename = {c: c.strip() for c in frame.columns}
    lowered = {c.strip().lower(): c for c in frame.columns}
    for wanted in ("Symbol", "Name", "LastSale", "MarketCap", "ADR TSO", "IPOyear", "Sector", "Industry"):
        original = lowered.get(wanted.lower())
        if original is not None:
            rename[original] = wanted
    frame = frame.rename(columns=rename)
    if "Symbol" not in frame or "Name" not in frame:
        raise ValueError("company-list CSV without Symbol and Name")
    frame["Symbol"] = frame["Symbol"].astype(str).str.strip().str.upper()
    frame["Name"] = frame["Name"].astype(str).str.strip()
    frame = frame.loc[frame["Symbol"].ne("")].drop_duplicates("Symbol")
    raw_cap = frame["MarketCap"] if "MarketCap" in frame else pd.Series("", index=frame.index)
    frame["MarketCap Raw"] = raw_cap.astype(str).str.strip()
    frame["MarketCap"] = [parse_market_cap(v) for v in frame["MarketCap Raw"]]
    frame["LastSale"] = [parse_number(v) for v in frame.get("LastSale", pd.Series("", index=frame.index))]
    for column in COMPANYLIST_COLUMNS:
        if column not in frame:
            frame[column] = ""
    return frame[COMPANYLIST_COLUMNS].sort_values("Symbol").reset_index(drop=True)


def company_list_problem(text: str, frame: pd.DataFrame, minimum_rows: int | None = None) -> str:
    """'' for a complete company list, else why not.

    Some captures are cut off part-way (one 2016 file stops at "AXT Inc"), so
    besides the row count the last line must have as many fields as the
    header, and the names must reach across the alphabet (the file is sorted
    by name, or by market cap in the sorted variant).
    """
    minimum_rows = MINIMUM_ROWS if minimum_rows is None else minimum_rows
    if len(frame) < minimum_rows:
        return f"too_few_rows_{len(frame)}"
    lines = [line for line in text.strip().splitlines() if line.strip()]
    header, last = next(csv.reader([lines[0]])), next(csv.reader([lines[-1]]))
    if len(last) != len(header) or not last[-1 if header[-1] else -2].strip():
        return "truncated_last_row"
    initials = {name[:1].upper() for name in frame["Name"] if name[:1].isalpha()}
    if len(initials) < 20:
        return f"names_cover_{len(initials)}_letters"
    return ""


def common_rows(frame: pd.DataFrame) -> int:
    return len(investable_common_equities(frame))


def market_cap_share(frame: pd.DataFrame) -> float:
    caps = pd.to_numeric(frame.get("MarketCap"), errors="coerce")
    return float((caps > 0).mean()) if len(frame) else 0.0


def market_cap_checks(frame: pd.DataFrame) -> dict:
    """Unit checks on MarketCap: Apple's value in $bn and rows above $1.5T (none was that large before 2020)."""
    caps = pd.to_numeric(frame["MarketCap"], errors="coerce")
    apple = caps[frame["Symbol"].eq("AAPL")]
    big = frame.loc[caps > 1.5e12, "Symbol"]
    return {"aapl_mcap_bn": round(float(apple.iloc[0]) / 1e9, 1) if len(apple) and pd.notna(apple.iloc[0]) else "",
            "mcap_over_1p5t": ";".join(big)}


def capture_date(timestamp: str) -> date:
    return datetime.strptime(timestamp[:8], "%Y%m%d").date()


def _csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False, lineterminator="\n").encode("utf-8")


# --------------------------------------------------------------------------- building


def process_capture(capture: dict) -> dict:
    """Fetch (or read) one capture and parse it; the capture row gains the parse facts."""
    kind, ts, original = capture["kind"], capture["timestamp"], capture["original"]
    out = {**capture, "raw_url": raw_capture_url(ts, original), "raw_path": "", "raw_sha256": "",
           "parsed_date": "", "footer": "", "footer_lag_days": "", "rows": "", "common_rows": "",
           "market_cap_share": "", "parse_error": ""}
    try:
        data = fetch_wayback_raw(kind, ts, original)
    except FileNotFoundError:
        out["parse_error"] = "http_404"
        return out
    except HTTPError as exc:
        out["parse_error"] = f"http_{exc.code}"
        return out
    path = raw_cache_path(kind, ts, original)
    out["raw_path"] = str(path.relative_to(CACHE))
    out["raw_sha256"] = sha256_bytes(data)
    text = decode(data)
    if kind == "symdir":
        footer_date, footer = symdir_footer(text)
        out["footer"] = footer
        frame, reason = normalise_symdir(text)
        if frame is None:
            out["parse_error"] = reason
            return out
        out["parsed_date"] = frame["Observed At"].iloc[0]
        out["footer_lag_days"] = (capture_date(ts) - footer_date).days if footer_date else ""
    else:
        try:
            frame = parse_company_list(text)
        except Exception as exc:  # an HTML page or a truncated file
            out["parse_error"] = f"parse_failed: {exc}"[:200]
            return out
        problem = company_list_problem(text, frame)
        if problem:
            out["parse_error"] = problem
            out["rows"] = len(frame)
            return out
        out["parsed_date"] = capture_date(ts).isoformat()
        out["market_cap_share"] = round(market_cap_share(frame), 4)
        out.update(market_cap_checks(frame))
    out["rows"] = len(frame)
    out["common_rows"] = common_rows(frame)
    out["_frame"] = frame
    return out


def choose_per_date(parsed: list[dict]) -> dict[tuple[str, str], dict]:
    """One capture per (kind, date): the most rows, then the earliest capture."""
    best: dict[tuple[str, str], dict] = {}
    for row in parsed:
        if row.get("_frame") is None:
            continue
        key = (row["kind"], row["parsed_date"])
        current = best.get(key)
        if current is None or (row["rows"], -int(row["timestamp"])) > (current["rows"], -int(current["timestamp"])):
            best[key] = row
    return best


def write_snapshot(row: dict) -> tuple[Path, str]:
    frame = row["_frame"].copy()
    if row["kind"] == "symdir":
        frame["Source File"] = row["raw_url"]
        frame["Source Repository"] = "https://web.archive.org"
        frame["Source Commit"] = ""
        frame["Capture Timestamp"] = row["timestamp"]
        frame["Raw SHA256"] = row["raw_sha256"]
        target = SYMDIR_DIR / f"nasdaq_listed_{row['parsed_date']}.csv"
    else:
        frame["Source File"] = row["raw_url"]
        frame["Capture Timestamp"] = row["timestamp"]
        frame["Raw SHA256"] = row["raw_sha256"]
        frame["Observed At"] = row["parsed_date"]
        target = COMPANYLIST_DIR / f"nasdaq_companylist_{row['parsed_date']}.csv"
    data = _csv_bytes(frame)
    atomic_write(target, data)
    return target, sha256_bytes(data)


def repo_index_rows(directory: Path = REPO_SNAPSHOTS) -> list[dict]:
    """Index rows for the repo's own nasdaq_listed_* snapshots (source repo_symdir)."""
    rows = []
    for path in sorted(directory.glob("nasdaq_listed_*.csv")):
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        source_file = frame["Source File"].iloc[0] if "Source File" in frame and len(frame) else ""
        commit = frame["Source Commit"].iloc[0] if "Source Commit" in frame and len(frame) else ""
        stamp = re.search(r"timestamp=(\d{14})", source_file or "")
        rows.append({
            "snapshot_date": path.stem.removeprefix("nasdaq_listed_"),
            "source": "repo_symdir",
            "capture_timestamp": stamp.group(1) if stamp else "",
            "original_url": source_file or (f"git:{commit}" if commit else ""),
            "rows": len(frame), "common_rows": common_rows(frame), "has_market_cap": "N",
            "sha256": sha256_file(path), "raw_sha256": "", "snapshot_file": path.as_posix(),
        })
    return rows


def resolve_snapshot(snapshot_file: str) -> Path:
    """The file behind an index ``snapshot_file`` (cache paths resolve to CACHE without the link)."""
    path = Path(snapshot_file)
    if path.parts[:2] == CACHE_LINK.parts:
        return CACHE.joinpath(*path.parts[2:])
    return path


def compare_snapshots(a: pd.DataFrame, b: pd.DataFrame, examples: int = 15) -> dict:
    """Symbol-set and name agreement between two listing frames (Symbol, Name columns)."""
    def names(frame):
        return {s: re.sub(r"\s+", " ", str(n)).strip().lower() for s, n in zip(frame["Symbol"], frame["Name"])}
    left, right = names(a), names(b)
    both = sorted(set(left) & set(right))
    mismatched = [s for s in both if left[s] != right[s]]
    only_a, only_b = sorted(set(left) - set(right)), sorted(set(right) - set(left))
    common_a = set(investable_common_equities(a)["Symbol"])
    common_b = set(investable_common_equities(b)["Symbol"])
    return {
        "rows_a": len(left), "rows_b": len(right), "in_both": len(both),
        "only_a": len(only_a), "only_b": len(only_b),
        "jaccard": round(len(both) / max(1, len(set(left) | set(right))), 4),
        "name_mismatch_in_both": len(mismatched),
        "common_a": len(common_a), "common_b": len(common_b), "common_in_both": len(common_a & common_b),
        "examples_only_a": only_a[:examples], "examples_only_b": only_b[:examples],
        "examples_name_mismatch": [[s, left[s], right[s]] for s in mismatched[:examples]],
    }


def overlap_check(index: pd.DataFrame, max_days: int = 7) -> dict:
    """Compare each repo snapshot (2011-2019) with the nearest Wayback symbol file within ``max_days``."""
    wayback = index.loc[index["source"].eq("wayback_symdir")].copy()
    repo = index.loc[index["source"].eq("repo_symdir") & index["snapshot_date"].lt("2020-01-01")].copy()
    pairs = []
    for _, r in repo.iterrows():
        if wayback.empty:
            break
        gaps = (pd.to_datetime(wayback["snapshot_date"]) - pd.to_datetime(r["snapshot_date"])).dt.days
        nearest = gaps.abs().idxmin()
        if abs(gaps[nearest]) <= max_days:
            w = wayback.loc[nearest]
            repo_frame = pd.read_csv(resolve_snapshot(r["snapshot_file"]), dtype=str, keep_default_na=False)
            wb_frame = pd.read_csv(resolve_snapshot(w["snapshot_file"]), dtype=str, keep_default_na=False)
            pairs.append({"repo_date": r["snapshot_date"], "repo_origin": r["original_url"],
                          "wayback_date": w["snapshot_date"], "wayback_capture": w["capture_timestamp"],
                          "days_apart": int(gaps[nearest]),
                          "a": "wayback", "b": "repo", **compare_snapshots(wb_frame, repo_frame)})
    # The company list against the symbol file of the same (or nearest) day.
    cross = []
    companylists = index.loc[index["source"].eq("wayback_companylist")]
    symdirs = index.loc[index["source"].isin(["wayback_symdir", "repo_symdir"])]
    for _, c in companylists.iterrows():
        gaps = (pd.to_datetime(symdirs["snapshot_date"]) - pd.to_datetime(c["snapshot_date"])).dt.days.abs()
        nearest = gaps.idxmin()
        if gaps[nearest] <= 3:
            s = symdirs.loc[nearest]
            s_frame = pd.read_csv(resolve_snapshot(s["snapshot_file"]), dtype=str, keep_default_na=False)
            c_frame = pd.read_csv(resolve_snapshot(c["snapshot_file"]), dtype=str, keep_default_na=False)
            stats = compare_snapshots(c_frame, s_frame)
            cross.append({"companylist_date": c["snapshot_date"], "symdir_date": s["snapshot_date"],
                          "symdir_source": s["source"], "days_apart": int(gaps[nearest]),
                          **{k: v for k, v in stats.items() if not k.startswith("examples")},
                          "examples_only_companylist": stats["examples_only_a"][:8],
                          "examples_only_symdir": stats["examples_only_b"][:8]})
    return {"symdir_vs_repo": pairs, "companylist_vs_symdir": cross}


def gap_table(dates: list[str], start: str = "2011-01-01", end: str = "2019-12-31", top: int = 10) -> list[dict]:
    """The longest gaps between consecutive snapshot dates inside [start, end].

    The nearest snapshot before ``start`` (or after ``end``) bounds the first
    (last) gap when there is one; otherwise the window edge does.
    """
    every = sorted(set(dates))
    points = [d for d in every if start <= d <= end]
    if not points:
        return []
    before = [d for d in every if d < start]
    after = [d for d in every if d > end]
    bounds = [before[-1] if before else start, *points, after[0] if after else end]
    gaps = [{"from": a, "to": b, "days": (date.fromisoformat(b) - date.fromisoformat(a)).days}
            for a, b in zip(bounds, bounds[1:])]
    return sorted(gaps, key=lambda g: -g["days"])[:top]


def write_captures(rows: list[dict]) -> None:
    columns = ["kind", "timestamp", "original", "statuscode", "mimetype", "digest", "length", "action", "reason",
               "raw_url", "raw_path", "raw_sha256", "footer", "parsed_date", "footer_lag_days", "rows",
               "common_rows", "market_cap_share", "aapl_mcap_bn", "mcap_over_1p5t", "parse_error", "kept"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in sorted(rows, key=lambda r: (r["kind"], r["timestamp"], r["original"])):
        writer.writerow({c: row.get(c, "") for c in columns})
    atomic_write(CAPTURES_FILE, buffer.getvalue().encode("utf-8"))


def build(from_: str = "2011", to: str = "2019", max_fetch: int | None = None) -> dict:
    captures = []
    for kind, url, match_type in CDX_TARGETS:
        rows = wayback_cdx(url, match_type, from_, to)
        print(f"CDX {url} ({match_type}): {len(rows)} rows", flush=True)
        captures.extend(select_captures(kind, rows))
    # The same capture can come back from two CDX queries (www and old. are separate keys, but be safe).
    unique = {}
    for row in captures:
        unique.setdefault((row["kind"], row["timestamp"], row["original"]), row)
    captures = list(unique.values())
    to_fetch = [c for c in captures if c["action"] == "fetch"]
    print(f"{len(to_fetch)} captures to fetch or read from cache "
          f"({Counter(c['kind'] for c in to_fetch)})", flush=True)
    processed = []
    for n, capture in enumerate(sorted(to_fetch, key=lambda c: (c["kind"], c["timestamp"]))):
        if max_fetch is not None and n >= max_fetch:
            break
        row = process_capture(capture)
        processed.append(row)
        print(f"  [{n + 1}/{len(to_fetch)}] {row['kind']} {row['timestamp']} -> "
              f"{row['parsed_date'] or row['parse_error']} rows={row['rows']}", flush=True)
    chosen = choose_per_date(processed)
    index_rows = []
    for (kind, _), row in sorted(chosen.items()):
        target, digest = write_snapshot(row)
        row["kept"] = "Y"
        index_rows.append({
            "snapshot_date": row["parsed_date"], "source": f"wayback_{kind}",
            "capture_timestamp": row["timestamp"], "original_url": row["original"],
            "rows": row["rows"], "common_rows": row["common_rows"],
            "has_market_cap": "Y" if kind == "companylist" and float(row["market_cap_share"] or 0) >= 0.5 else "N",
            "sha256": digest, "raw_sha256": row["raw_sha256"],
            "snapshot_file": (CACHE_LINK / target.relative_to(CACHE)).as_posix(),
        })
    for row in processed:
        if row.get("kept") != "Y":
            row["kept"] = "N"
            if row.get("_frame") is not None:
                row["reason"] = (row["reason"] + ";" if row["reason"] else "") + "same_date_as_kept_capture"
    done = {(r["kind"], r["timestamp"], r["original"]): r for r in processed}
    write_captures([done.get((c["kind"], c["timestamp"], c["original"]), c) for c in captures])
    index_rows.extend(repo_index_rows())
    index = pd.DataFrame(index_rows, columns=INDEX_COLUMNS).sort_values(["snapshot_date", "source"])
    atomic_write(INDEX_FILE, _csv_bytes(index))
    overlap = overlap_check(index)
    atomic_write(OVERLAP_FILE, (json.dumps(overlap, indent=2) + "\n").encode("utf-8"))
    return summarise(index, captures, processed, overlap)


def summarise(index: pd.DataFrame, captures: list[dict], processed: list[dict], overlap: dict) -> dict:
    wayback = index.loc[index["source"].str.startswith("wayback")]
    per_year = {}
    for source, group in wayback.groupby("source"):
        per_year[source] = {year: sorted(g["snapshot_date"]) for year, g in group.groupby(group["snapshot_date"].str[:4])}
    any_nasdaq = index.loc[index["snapshot_date"].between("2011-01-01", "2019-12-31")]
    return {
        "cdx_rows": len(captures),
        "fetched_or_cached": len(processed),
        "parse_failures": Counter(r["parse_error"].split(":")[0] for r in processed if r["parse_error"]),
        "snapshots": wayback.groupby("source").size().to_dict(),
        "dates_per_year": per_year,
        "gaps_symdir_wayback": gap_table(list(index.loc[index["source"].eq("wayback_symdir"), "snapshot_date"])),
        "gaps_companylist": gap_table(list(index.loc[index["source"].eq("wayback_companylist"), "snapshot_date"])),
        "gaps_symdir_any": gap_table(list(index.loc[index["source"].isin(["wayback_symdir", "repo_symdir"]),
                                                    "snapshot_date"])),
        "gaps_any_source": gap_table(list(any_nasdaq["snapshot_date"])),
        "overlap_pairs": [{k: v for k, v in p.items() if not k.startswith("examples")}
                          for p in overlap["symdir_vs_repo"]],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="from_", default="2011")
    parser.add_argument("--to", default="2019")
    parser.add_argument("--max-fetch", type=int, default=None, help="stop after this many captures (testing)")
    args = parser.parse_args(argv)
    summary = build(args.from_, args.to, args.max_fetch)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
