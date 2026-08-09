"""Incrementally update local US prices from Nasdaq's public historical API."""

from __future__ import annotations

import argparse
import gzip
from io import StringIO
import json
import os
import random
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_300M_STOCK_LIST_FILE,
    NASDAQ_INDEX_FILE,
    PROJECT_PATH,
)
from src.io.financial_update import investable_common_equities
from src.research.universe_history import load_universe_snapshots

API = "https://api.nasdaq.com/api/quote/{symbol}/historical"
SCREENER_API = "https://api.nasdaq.com/api/screener/stocks"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
HISTORICAL_UNIVERSE_PATHS = (
    "stocks_list_dir/nasdaq/nasdaq_300M.csv",
    "stocks_list_dir/nasdaq_300M.csv",
    "nasdaq_300M.csv",
)


def _github_raw_provenance(source: str) -> tuple[str | None, str | None]:
    match = re.fullmatch(
        r"https?://raw\.githubusercontent\.com/([^/]+/[^/]+)/([^/]+)/.+",
        source,
    )
    if match is None:
        match = re.fullmatch(
            r"https?://media\.githubusercontent\.com/media/([^/]+/[^/]+)/([^/]+)/.+",
            source,
        )
    if match is None:
        return None, None
    return f"https://github.com/{match.group(1)}", match.group(2)


def _portable_project_path(path: str | Path) -> str:
    """Prefer a repository-relative path in persisted provenance manifests."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(Path(PROJECT_PATH).resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _read_archived_nasdaq_trader_source(source: str) -> str:
    """Read a raw file or a byte-ranged Common Crawl WARC record."""
    parsed = urlsplit(source)
    fragment = parse_qs(parsed.fragment)
    if (
        parsed.netloc == "data.commoncrawl.org"
        and {"offset", "length"}.issubset(fragment)
    ):
        offset = int(fragment["offset"][0])
        length = int(fragment["length"][0])
        request_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
        )
        request = Request(
            request_url,
            headers={**HEADERS, "Range": f"bytes={offset}-{offset + length - 1}"},
        )
        with urlopen(request, timeout=60) as response:
            record = gzip.decompress(response.read()).decode(
                "utf-8-sig", errors="replace"
            )
        header_offset = record.find("Symbol|Security Name|")
        if header_offset < 0:
            return record
        return record[header_offset:]
    with urlopen(Request(source, headers=HEADERS), timeout=60) as response:
        return response.read().decode("utf-8-sig", errors="replace")


def _write_merged_import_manifest(path: Path, result: dict) -> None:
    """Retain earlier import evidence instead of overwriting every run."""
    previous = {}
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    combined = [*(previous.get("imported") or []), *(result.get("imported") or [])]
    deduplicated = {
        (item.get("observed_at"), item.get("source_file"), item.get("snapshot")): item
        for item in combined
    }
    result["imported"] = sorted(
        deduplicated.values(), key=lambda item: (item.get("observed_at") or "", item.get("source_file") or "")
    )
    result["skipped"] = [*(previous.get("skipped") or []), *(result.get("skipped") or [])]
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")


def _number(value):
    if value in (None, "", "--", "NA", "N/A"):
        return None
    return float(str(value).replace("$", "").replace(",", ""))


def fetch_history(symbol: str, start: date, end: date, asset_class="stocks", retries=3) -> pd.DataFrame:
    params = urlencode({
        "assetclass": asset_class,
        "fromdate": start.isoformat(),
        "todate": end.isoformat(),
        "limit": 5000,
    })
    request = Request(API.format(symbol=symbol) + "?" + params, headers=HEADERS)
    error = None
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.load(response)
            data = payload.get("data") or {}
            rows = ((data.get("tradesTable") or {}).get("rows") or [])
            records = []
            for row in rows:
                records.append({
                    "date": pd.to_datetime(row["date"]),
                    "open": _number(row.get("open")),
                    "high": _number(row.get("high")),
                    "low": _number(row.get("low")),
                    "close": _number(row.get("close")),
                    "volume": _number(row.get("volume")),
                })
            if not records:
                return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
            return pd.DataFrame(records).dropna(subset=["date", "close"]).sort_values("date")
        except Exception as exc:  # network failures are reported per symbol
            error = exc
            time.sleep((2**attempt) + random.random())
    raise RuntimeError(f"{symbol}: {error}")


def refresh_universe(end: date, min_market_cap=300_000_000) -> dict:
    params = urlencode({"tableonly": "true", "limit": 10000, "exchange": "nasdaq"})
    request = Request(SCREENER_API + "?" + params, headers=HEADERS)
    with urlopen(request, timeout=60) as response:
        payload = json.load(response)
    rows = (((payload.get("data") or {}).get("table") or {}).get("rows") or [])
    current = pd.DataFrame(rows)
    if current.empty:
        raise RuntimeError("Nasdaq screener returned an empty universe")
    current["Market Cap"] = current["marketCap"].map(_number)
    current = current[current["Market Cap"] >= min_market_cap].copy()
    current = current.rename(columns={
        "symbol": "Symbol", "name": "Name", "lastsale": "Last Sale",
        "netchange": "Net Change", "pctchange": "% Change",
    })
    target = Path(NASDAQ_300M_STOCK_LIST_FILE)
    old = pd.read_csv(target)
    columns = old.columns.tolist()
    old_by_symbol = old.set_index("Symbol")
    for column in columns:
        if column not in current:
            current[column] = current["Symbol"].map(old_by_symbol[column]).fillna("")
    current = current[columns].sort_values("Symbol")
    old_symbols, new_symbols = set(old["Symbol"]), set(current["Symbol"])
    snapshot = target.parent / "snapshots" / f"nasdaq_300M_{end.isoformat()}.csv"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    current.to_csv(snapshot, index=False)
    tmp = target.with_suffix(target.suffix + ".tmp")
    current.to_csv(tmp, index=False)
    os.replace(tmp, target)
    return {
        "count": len(current),
        "added": sorted(new_symbols - old_symbols),
        "removed": sorted(old_symbols - new_symbols),
        "snapshot": str(snapshot),
    }


def recover_git_universe_snapshots(minimum_rows: int = 1000) -> dict:
    """Recover conservative dated universe snapshots retained in Git history."""
    project = Path(PROJECT_PATH)
    log = subprocess.run(
        ["git", "log", "--all", "--format=%H|%ad", "--date=short", "--", *HISTORICAL_UNIVERSE_PATHS],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    snapshot_dir = Path(NASDAQ_300M_STOCK_LIST_FILE).parent / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    recovered, skipped = [], []
    seen_dates: set[str] = set()
    for line in log.stdout.splitlines():
        if not line.strip():
            continue
        commit, available_date = line.split("|", 1)
        if available_date in seen_dates:
            continue
        frame = None
        source_path = None
        for candidate in HISTORICAL_UNIVERSE_PATHS:
            shown = subprocess.run(
                ["git", "show", f"{commit}:{candidate}"],
                cwd=project,
                capture_output=True,
                text=True,
            )
            if shown.returncode == 0:
                try:
                    from io import StringIO

                    frame = pd.read_csv(StringIO(shown.stdout))
                    source_path = candidate
                    break
                except Exception:
                    continue
        if frame is None or not {"Symbol", "Name"}.issubset(frame.columns) or len(frame) < minimum_rows:
            skipped.append({"commit": commit, "available_date": available_date, "rows": 0 if frame is None else len(frame)})
            continue
        target = snapshot_dir / f"nasdaq_300M_{available_date}.csv"
        if not target.exists():
            frame.to_csv(target, index=False)
        recovered.append({
            "commit": commit,
            "available_date": available_date,
            "rows": len(frame),
            "source_path": source_path,
            "snapshot": str(target),
        })
        seen_dates.add(available_date)
    result = {"minimum_rows": minimum_rows, "recovered": recovered, "skipped": skipped}
    manifest = snapshot_dir / "git_recovery_manifest.json"
    manifest.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["manifest"] = str(manifest)
    return result


def import_nasdaq_listings_history(
    repository: str | Path,
    minimum_rows: int = 1000,
) -> dict:
    """Import dated Nasdaq Trader listing snapshots from a Git dataset.

    The source repository must retain ``data/nasdaq-listed.csv`` in history.
    Commit dates are treated as observation dates; no snapshot is backdated.
    This restores historical listed/delisted names without pretending that the
    source provides historical market capitalisation.
    """
    repository = Path(repository).resolve()
    if not (repository / ".git").exists():
        raise ValueError(f"Not a Git repository: {repository}")
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip() or str(repository)
    log = subprocess.run(
        [
            "git", "log", "--format=%H|%ad", "--date=short", "--",
            "data/nasdaq-listed.csv",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    snapshot_dir = Path(NASDAQ_300M_STOCK_LIST_FILE).parent / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    imported, skipped = [], []
    seen_dates: set[str] = set()
    for line in log.stdout.splitlines():
        if not line.strip():
            continue
        commit, observed_at = line.split("|", 1)
        if observed_at in seen_dates:
            continue
        seen_dates.add(observed_at)
        shown = None
        source_path = None
        for candidate in (
            "data/nasdaq-listed-symbols.csv",
            "data/nasdaq-listed.csv",
        ):
            if candidate.endswith("-symbols.csv"):
                last_change = subprocess.run(
                    ["git", "log", "-1", "--format=%H", commit, "--", candidate],
                    cwd=repository,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if last_change.stdout.strip() != commit:
                    continue
            candidate_result = subprocess.run(
                ["git", "show", f"{commit}:{candidate}"],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
            )
            if candidate_result.returncode == 0:
                shown = candidate_result
                source_path = candidate
                break
        if shown is None:
            skipped.append({"commit": commit, "observed_at": observed_at, "reason": "git_show_failed"})
            continue
        try:
            raw = pd.read_csv(StringIO(shown.stdout))
        except Exception as exc:
            skipped.append({"commit": commit, "observed_at": observed_at, "reason": str(exc)})
            continue
        name_column = next(
            (column for column in ("Security Name", "Name", "Company Name") if column in raw),
            None,
        )
        if "Symbol" not in raw or name_column is None:
            skipped.append({"commit": commit, "observed_at": observed_at, "reason": "missing_columns"})
            continue
        normalized = raw.rename(columns={name_column: "Name"}).copy()
        normalized["Symbol"] = normalized["Symbol"].astype(str).str.upper().str.strip()
        normalized = normalized.loc[
            normalized["Symbol"].ne("") & normalized["Name"].notna()
        ].drop_duplicates("Symbol")
        if len(normalized) < minimum_rows:
            skipped.append({"commit": commit, "observed_at": observed_at, "reason": "too_few_common_equities", "rows": len(normalized)})
            continue
        optional_type_columns = [
            column for column in ("ETF", "Test Issue", "NextShares") if column in normalized
        ]
        normalized = normalized[["Symbol", "Name", *optional_type_columns]].sort_values("Symbol")
        normalized["Source File"] = source_path
        normalized["Source Repository"] = remote
        normalized["Source Commit"] = commit
        normalized["Observed At"] = observed_at
        target = snapshot_dir / f"nasdaq_listed_{observed_at}.csv"
        normalized.to_csv(target, index=False)
        imported.append({
            "commit": commit,
            "observed_at": observed_at,
            "rows": len(normalized),
            "source_path": source_path,
            "source_repository": remote,
            "snapshot": _portable_project_path(target),
        })
    result = {
        "source_repository": remote,
        "minimum_rows": minimum_rows,
        "imported": sorted(imported, key=lambda item: item["observed_at"]),
        "skipped": skipped,
    }
    manifest = snapshot_dir / "listings_git_import_manifest.json"
    manifest.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["manifest"] = _portable_project_path(manifest)
    return result


def import_nasdaq_trader_git_history(
    repository: str | Path,
    source_path: str = "data/nasdaqlisted.txt",
    minimum_rows: int = 1000,
) -> dict:
    """Import every committed Nasdaq Trader symbol file as a PIT snapshot.

    The commit date is the conservative availability date.  Existing snapshots
    for the same date are retained so a later source cannot silently replace
    evidence already used by a backtest.
    """
    repository = Path(repository).resolve()
    if not (repository / ".git").exists():
        raise ValueError(f"Not a Git repository: {repository}")
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=repository,
        check=False, capture_output=True, text=True,
    ).stdout.strip() or str(repository)
    log = subprocess.run(
        ["git", "log", "--format=%H|%ad", "--date=short", "--", source_path],
        cwd=repository, check=True, capture_output=True, text=True,
    )
    snapshot_dir = Path(NASDAQ_300M_STOCK_LIST_FILE).parent / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    imported, skipped = [], []
    seen_dates: set[str] = set()
    for line in log.stdout.splitlines():
        if not line.strip():
            continue
        commit, observed_at = line.split("|", 1)
        if observed_at in seen_dates:
            continue
        seen_dates.add(observed_at)
        shown = subprocess.run(
            ["git", "show", f"{commit}:{source_path}"], cwd=repository,
            check=False, capture_output=True, text=True,
        )
        if shown.returncode != 0:
            skipped.append({"commit": commit, "observed_at": observed_at, "reason": "git_show_failed"})
            continue
        header_offset = shown.stdout.find("Symbol|Security Name|")
        if header_offset < 0:
            skipped.append({"commit": commit, "observed_at": observed_at, "reason": "missing_header"})
            continue
        try:
            frame = pd.read_csv(StringIO(shown.stdout[header_offset:]), sep="|", dtype=str)
        except Exception as exc:
            skipped.append({"commit": commit, "observed_at": observed_at, "reason": str(exc)})
            continue
        if "Symbol" not in frame or "Security Name" not in frame:
            skipped.append({"commit": commit, "observed_at": observed_at, "reason": "missing_columns"})
            continue
        frame = frame.rename(columns={"Security Name": "Name"})
        frame = frame.loc[
            ~frame["Symbol"].astype(str).str.startswith("File Creation Time:")
        ].copy()
        frame["Symbol"] = frame["Symbol"].astype(str).str.upper().str.strip()
        frame = frame.loc[frame["Symbol"].ne("") & frame["Name"].notna()].drop_duplicates("Symbol")
        if len(frame) < minimum_rows:
            skipped.append({"commit": commit, "observed_at": observed_at, "reason": "too_few_rows", "rows": len(frame)})
            continue
        optional = [column for column in ("ETF", "Test Issue", "NextShares") if column in frame]
        normalized = frame[["Symbol", "Name", *optional]].sort_values("Symbol")
        normalized["Source Repository"] = remote
        normalized["Source Commit"] = commit
        normalized["Source File"] = source_path
        normalized["Observed At"] = observed_at
        target = snapshot_dir / f"nasdaq_listed_{observed_at}.csv"
        if target.exists():
            skipped.append({"commit": commit, "observed_at": observed_at, "reason": "snapshot_exists"})
            continue
        normalized.to_csv(target, index=False)
        imported.append({
            "commit": commit, "observed_at": observed_at, "rows": len(normalized),
            "source_repository": remote, "source_path": source_path,
            "snapshot": str(target),
        })
    result = {
        "source_repository": remote, "source_path": source_path,
        "minimum_rows": minimum_rows,
        "imported": sorted(imported, key=lambda item: item["observed_at"]),
        "skipped": skipped,
    }
    manifest = snapshot_dir / "nasdaq_trader_git_import_manifest.json"
    prior_runs = []
    if manifest.exists():
        try:
            prior = json.loads(manifest.read_text(encoding="utf-8"))
            prior_runs = prior.get("runs", [
                {key: prior[key] for key in (
                    "source_repository", "source_path", "minimum_rows", "imported", "skipped"
                ) if key in prior}
            ])
        except (OSError, json.JSONDecodeError):
            prior_runs = []
    result["runs"] = [*prior_runs, {
        key: result[key] for key in (
            "source_repository", "source_path", "minimum_rows", "imported", "skipped"
        )
    }]
    manifest.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["manifest"] = str(manifest)
    return result


def import_nasdaq_trader_files(
    paths: list[str | Path], minimum_rows: int = 1000
) -> dict:
    """Import independently archived Nasdaq Trader pipe-delimited snapshots."""
    snapshot_dir = Path(NASDAQ_300M_STOCK_LIST_FILE).parent / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    imported, skipped = [], []
    for raw_path in paths:
        source = str(raw_path)
        path = None
        if source.startswith(("https://", "http://")):
            try:
                text = _read_archived_nasdaq_trader_source(source)
            except Exception as exc:
                skipped.append({"path": source, "reason": str(exc)})
                continue
        else:
            path = Path(raw_path).resolve()
            try:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError as exc:
                skipped.append({"path": str(path), "reason": str(exc)})
                continue
        source_label = source if path is None else str(path)
        footer = re.search(r"File Creation Time:\s*(\d{8})", text)
        if footer is None:
            skipped.append({"path": source_label, "reason": "missing_file_creation_time"})
            continue
        observed_at = pd.to_datetime(footer.group(1), format="%m%d%Y", errors="coerce")
        if pd.isna(observed_at):
            skipped.append({"path": source_label, "reason": "invalid_file_creation_time"})
            continue
        header_offset = text.find("Symbol|Security Name|")
        if header_offset < 0:
            skipped.append({"path": source_label, "reason": "missing_header"})
            continue
        try:
            frame = pd.read_csv(StringIO(text[header_offset:]), sep="|", dtype=str)
        except Exception as exc:
            skipped.append({"path": source_label, "reason": str(exc)})
            continue
        name_column = next(
            (column for column in ("Security Name", "Name", "Company Name") if column in frame),
            None,
        )
        if "Symbol" not in frame or name_column is None:
            skipped.append({"path": source_label, "reason": "missing_columns"})
            continue
        frame = frame.rename(columns={name_column: "Name"})
        frame = frame.loc[
            ~frame["Symbol"].astype(str).str.startswith("File Creation Time:")
        ].copy()
        frame["Symbol"] = frame["Symbol"].astype(str).str.upper().str.strip()
        frame = frame.loc[frame["Symbol"].ne("") & frame["Name"].notna()].drop_duplicates("Symbol")
        if len(frame) < minimum_rows:
            skipped.append({"path": source_label, "reason": "too_few_rows", "rows": len(frame)})
            continue
        optional = [column for column in ("ETF", "Test Issue", "NextShares") if column in frame]
        normalized = frame[["Symbol", "Name", *optional]].sort_values("Symbol")
        repository = (
            next((parent for parent in path.parents if (parent / ".git").exists()), None)
            if path is not None else None
        )
        source_commit = None
        source_repository = None
        if repository is not None:
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repository,
                check=False, capture_output=True, text=True,
            )
            remote = subprocess.run(
                ["git", "remote", "get-url", "origin"], cwd=repository,
                check=False, capture_output=True, text=True,
            )
            source_commit = commit.stdout.strip() or None
            source_repository = remote.stdout.strip() or None
        elif path is None:
            source_repository, source_commit = _github_raw_provenance(source_label)
        observed_date = observed_at.strftime("%Y-%m-%d")
        normalized["Source File"] = source_label
        normalized["Source Repository"] = source_repository
        normalized["Source Commit"] = source_commit
        normalized["Observed At"] = observed_date
        target = snapshot_dir / f"nasdaq_listed_{observed_date}.csv"
        normalized.to_csv(target, index=False)
        imported.append({
            "observed_at": observed_date,
            "rows": len(normalized),
            "source_file": source_label,
            "source_repository": source_repository,
            "source_commit": source_commit,
            "snapshot": str(target),
        })
    result = {"minimum_rows": minimum_rows, "imported": imported, "skipped": skipped}
    manifest = snapshot_dir / "nasdaq_trader_file_import_manifest.json"
    _write_merged_import_manifest(manifest, result)
    result["manifest"] = str(manifest)
    return result


def import_nasdaq_symbol_list(
    path: str | Path, observed_at: date, minimum_rows: int = 1000
) -> dict:
    """Import a stripped one-symbol-per-line archive at its first known date."""
    path = Path(path).resolve()
    symbols = pd.Series(
        path.read_text(encoding="utf-8-sig", errors="replace").splitlines(),
        dtype="object",
    ).astype(str).str.upper().str.strip()
    symbols = symbols.loc[symbols.str.fullmatch(r"[A-Z][A-Z0-9.\-]*")].drop_duplicates()
    if len(symbols) < minimum_rows:
        raise ValueError(f"Too few symbols in {path}: {len(symbols)}")
    repository = next((parent for parent in path.parents if (parent / ".git").exists()), None)
    commit, remote = None, None
    if repository is not None:
        commit_result = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", str(path.relative_to(repository))],
            cwd=repository, check=False, capture_output=True, text=True,
        )
        remote_result = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=repository,
            check=False, capture_output=True, text=True,
        )
        commit = commit_result.stdout.strip() or None
        remote = remote_result.stdout.strip() or None
    frame = pd.DataFrame({"Symbol": symbols, "Name": symbols})
    frame["Source File"] = str(path)
    frame["Source Repository"] = remote
    frame["Source Commit"] = commit
    frame["Observed At"] = observed_at.isoformat()
    frame["Source Format"] = "symbol_only"
    target = Path(NASDAQ_300M_STOCK_LIST_FILE).parent / "snapshots" / f"nasdaq_listed_{observed_at.isoformat()}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(target, index=False)
    return {
        "observed_at": observed_at.isoformat(), "rows": len(frame),
        "source_file": str(path), "source_repository": remote,
        "source_commit": commit, "snapshot": str(target),
    }


def import_nasdaq_json_catalog(
    source: str | Path,
    observed_at: date,
    minimum_rows: int = 1000,
) -> dict:
    """Import a dated JSON array containing Nasdaq ``symbol``/``name`` rows.

    ``observed_at`` is deliberately explicit: third-party catalogs usually do
    not carry an exchange publication timestamp, so the first verifiable Git
    commit date is the earliest safe point at which the data may be used.
    """
    source_label = str(source)
    if source_label.startswith(("https://", "http://")):
        with urlopen(Request(source_label, headers=HEADERS), timeout=60) as response:
            payload = json.load(response)
    else:
        source_path = Path(source).resolve()
        payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
        source_label = str(source_path)
    if not isinstance(payload, list):
        raise ValueError("Nasdaq JSON catalog must be a top-level array")
    frame = pd.DataFrame(payload)
    symbol_column = next((column for column in ("symbol", "Symbol") if column in frame), None)
    name_column = next((column for column in ("name", "Name", "Security Name") if column in frame), None)
    if symbol_column is None or name_column is None:
        raise ValueError("Nasdaq JSON catalog requires symbol and name fields")
    frame = frame.rename(columns={symbol_column: "Symbol", name_column: "Name"})
    frame["Symbol"] = frame["Symbol"].astype(str).str.upper().str.strip()
    frame["Name"] = frame["Name"].astype(str).str.strip()
    frame = frame.loc[
        frame["Symbol"].str.fullmatch(r"[A-Z][A-Z0-9.\-]*")
        & frame["Name"].ne("")
    ].drop_duplicates("Symbol")
    if len(frame) < minimum_rows:
        raise ValueError(f"Too few symbols in {source_label}: {len(frame)}")

    repository, commit = _github_raw_provenance(source_label)
    normalized = frame[["Symbol", "Name"]].sort_values("Symbol")
    normalized["Source File"] = source_label
    normalized["Source Repository"] = repository
    normalized["Source Commit"] = commit
    normalized["Observed At"] = observed_at.isoformat()
    normalized["Source Format"] = "json_catalog"
    target = (
        Path(NASDAQ_300M_STOCK_LIST_FILE).parent
        / "snapshots"
        / f"nasdaq_listed_{observed_at.isoformat()}.csv"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized.to_csv(target, index=False)
    return {
        "observed_at": observed_at.isoformat(),
        "rows": len(normalized),
        "source_file": source_label,
        "source_repository": repository,
        "source_commit": commit,
        "snapshot": str(target),
    }


def import_nasdaq_csv_catalog(
    source: str | Path,
    observed_at: date,
    minimum_rows: int = 1000,
) -> dict:
    """Import a dated two-column symbol/name CSV catalog."""
    source_label = str(source)
    if source_label.startswith(("https://", "http://")):
        with urlopen(Request(source_label, headers=HEADERS), timeout=60) as response:
            text = response.read().decode("utf-8-sig", errors="replace")
    else:
        source_path = Path(source).resolve()
        text = source_path.read_text(encoding="utf-8-sig", errors="replace")
        source_label = str(source_path)
    frame = pd.read_csv(
        StringIO(text), header=None, names=["Symbol", "Name"], usecols=[0, 1], dtype=str
    )
    frame["Symbol"] = frame["Symbol"].astype(str).str.upper().str.strip()
    frame["Name"] = frame["Name"].astype(str).str.strip()
    frame = frame.loc[
        frame["Symbol"].str.fullmatch(r"[A-Z][A-Z0-9.\-]*")
        & frame["Name"].ne("")
    ].drop_duplicates("Symbol")
    if len(frame) < minimum_rows:
        raise ValueError(f"Too few symbols in {source_label}: {len(frame)}")
    repository, commit = _github_raw_provenance(source_label)
    frame = frame.sort_values("Symbol")
    frame["Source File"] = source_label
    frame["Source Repository"] = repository
    frame["Source Commit"] = commit
    frame["Observed At"] = observed_at.isoformat()
    frame["Source Format"] = "csv_catalog"
    target = (
        Path(NASDAQ_300M_STOCK_LIST_FILE).parent
        / "snapshots"
        / f"nasdaq_listed_{observed_at.isoformat()}.csv"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(target, index=False)
    return {
        "observed_at": observed_at.isoformat(),
        "rows": len(frame),
        "source_file": source_label,
        "source_repository": repository,
        "source_commit": commit,
        "snapshot": str(target),
    }


def _atomic_merge(path: Path, incoming: pd.DataFrame, ticker: str | None = None) -> int:
    if incoming.empty:
        return 0
    old = pd.read_csv(path, parse_dates=["date"]) if path.exists() else pd.DataFrame()
    if ticker is not None:
        incoming["ticker"] = ticker
        incoming = incoming[["date", "ticker", "open", "high", "low", "close", "volume"]]
    combined = pd.concat([old, incoming], ignore_index=True)
    combined = combined.drop_duplicates("date", keep="last").sort_values("date")
    tmp = path.with_suffix(path.suffix + ".tmp")
    combined.to_csv(tmp, index=False)
    os.replace(tmp, path)
    return len(incoming)


def _exclude_existing_price_dates(
    target: Path, incoming: pd.DataFrame
) -> pd.DataFrame:
    """Keep every missing session while preserving existing provider rows."""
    if not target.exists():
        return incoming
    existing_dates = pd.to_datetime(
        pd.read_csv(target, usecols=["date"])["date"],
        errors="coerce",
    )
    return incoming.loc[~incoming["date"].isin(existing_dates)]


def _validate_price_adjustment_overlap(
    target: Path,
    incoming: pd.DataFrame,
    price_factor: float,
    volume_factor: float,
    minimum_sessions: int,
    tolerance: float,
) -> dict:
    """Require requested scaling to agree with existing overlapping rows."""
    if price_factor == 1.0 and volume_factor == 1.0:
        return {"status": "NOT_REQUIRED"}
    if not target.exists():
        raise ValueError(
            "non-unit adjustment factors require an existing overlap file"
        )
    existing = pd.read_csv(
        target,
        usecols=["date", "close", "volume"],
        parse_dates=["date"],
    ).rename(columns={
        "close": "existing_close",
        "volume": "existing_volume",
    })
    overlap = existing.merge(
        incoming[["date", "close", "volume"]].rename(columns={
            "close": "incoming_close",
            "volume": "incoming_volume",
        }),
        on="date",
    )
    overlap = overlap.loc[
        overlap["existing_close"].gt(0)
        & overlap["incoming_close"].gt(0)
        & overlap["existing_volume"].gt(0)
        & overlap["incoming_volume"].gt(0)
    ]
    if len(overlap) < minimum_sessions:
        raise ValueError(
            "insufficient overlap to validate adjustment factors: "
            f"{len(overlap)} < {minimum_sessions}"
        )
    price_ratios = (
        overlap["existing_close"] / overlap["incoming_close"]
    )
    volume_ratios = (
        overlap["existing_volume"] / overlap["incoming_volume"]
    )
    price_median = float(price_ratios.median())
    volume_median = float(volume_ratios.median())
    price_within = float(
        ((price_ratios / price_median - 1).abs() <= tolerance).mean()
    )
    volume_within = float(
        ((volume_ratios / volume_median - 1).abs() <= tolerance).mean()
    )
    if (
        abs(price_median / price_factor - 1) > tolerance
        or abs(volume_median / volume_factor - 1) > tolerance
        or price_within < 0.95
        or volume_within < 0.95
    ):
        raise ValueError(
            "requested adjustment factors do not match stable overlap: "
            f"price median={price_median}, volume median={volume_median}, "
            f"price within={price_within}, volume within={volume_within}"
        )
    return {
        "status": "VERIFIED",
        "overlap_sessions": int(len(overlap)),
        "price_ratio_median": price_median,
        "volume_ratio_median": volume_median,
        "relative_tolerance": tolerance,
        "price_ratio_within_tolerance_fraction": price_within,
        "volume_ratio_within_tolerance_fraction": volume_within,
    }


def update_ticker(ticker: str, end: date, price_dir: Path) -> dict:
    path = price_dir / f"{ticker.lower()}.csv"
    if path.exists():
        old = pd.read_csv(path, usecols=["date"], parse_dates=["date"])
        start = old["date"].max().date() + timedelta(days=1)
    else:
        # Enough history for long trend/momentum features, while also covering
        # stocks omitted from the old snapshot and subsequent IPOs.
        start = date(2020, 1, 1)
    if start > end:
        return {"ticker": ticker, "status": "current", "rows": 0}
    data = fetch_history(ticker, start, end)
    rows = _atomic_merge(path, data, ticker)
    return {"ticker": ticker, "status": "updated" if rows else "no_data", "rows": rows}


def update_all(
    end: date,
    workers: int = 8,
    limit: int | None = None,
    tickers: list[str] | None = None,
) -> dict:
    price_dir = Path(CLEANED_PRICE_DATA_DIR)
    targeted = list(dict.fromkeys(
        ticker.strip().upper()
        for ticker in (tickers or [])
        if ticker.strip()
    ))
    partial = bool(targeted) or limit is not None
    if partial:
        current = pd.read_csv(NASDAQ_300M_STOCK_LIST_FILE)
        universe = {
            "mode": "retained_for_partial_update",
            "count": len(current),
            "added": [],
            "removed": [],
            "snapshot": None,
        }
    else:
        universe = refresh_universe(end)
        current = pd.read_csv(NASDAQ_300M_STOCK_LIST_FILE)
    requested = (
        targeted
        if targeted
        else current["Symbol"].dropna().astype(str).tolist()
    )
    if limit is not None:
        requested = requested[:limit]
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(update_ticker, ticker, end, price_dir): ticker
            for ticker in requested
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"ticker": ticker, "status": "failed", "error": str(exc)})

    index_path = Path(NASDAQ_INDEX_FILE)
    index_old = pd.read_csv(index_path, parse_dates=["date"])
    index_start = index_old["date"].max().date() + timedelta(days=1)
    if index_start <= end:
        index_data = fetch_history("COMP", index_start, end, asset_class="index")
        index_data["change_rate"] = index_data["close"].pct_change()
        _atomic_merge(index_path, index_data)

    counts = pd.Series([item["status"] for item in results]).value_counts().to_dict()
    return {
        "end": end.isoformat(),
        "universe": universe,
        "requested_ticker_count": len(requested),
        "requested_tickers": requested if targeted else None,
        "counts": counts,
        "failures": [x for x in results if x["status"] == "failed"],
    }


def backfill_existing_price_files(end: date, workers: int = 8) -> dict:
    """Extend every retained historical ticker, including names outside today's universe.

    The normal daily updater follows the current market-cap universe. This
    explicit repair command prevents a universe-refresh date from truncating
    former constituents that still traded afterward.
    """
    price_dir = Path(CLEANED_PRICE_DATA_DIR)
    lagging = []
    for path in sorted(price_dir.glob("*.csv")):
        dates = pd.read_csv(path, usecols=["date"], parse_dates=["date"])["date"]
        if not dates.empty and dates.max().date() < end:
            lagging.append(path.stem.upper())
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(update_ticker, ticker, end, price_dir): ticker for ticker in lagging
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"ticker": ticker, "status": "failed", "error": str(exc)})
    counts = pd.Series([item["status"] for item in results], dtype="object").value_counts().to_dict()
    return {
        "end": end.isoformat(),
        "requested": len(lagging),
        "counts": counts,
        "no_data": sorted(item["ticker"] for item in results if item["status"] == "no_data"),
        "failures": [item for item in results if item["status"] == "failed"],
    }


def backfill_official_history(
    ticker: str,
    start: date,
    end: date,
    price_factor: float = 1.0,
    volume_factor: float = 1.0,
    source_note: str = "",
    source_url: str = "",
) -> dict:
    """Fill missing Nasdaq dates without replacing existing observations."""
    if price_factor <= 0 or volume_factor <= 0:
        raise ValueError("price and volume factors must be positive")
    ticker = ticker.upper().strip()
    incoming = fetch_history(ticker, start, end)
    for column in ("open", "high", "low", "close"):
        incoming[column] = incoming[column] * price_factor
    incoming["volume"] = incoming["volume"] * volume_factor
    price_dir = Path(CLEANED_PRICE_DATA_DIR)
    target = price_dir / f"{ticker.lower()}.csv"
    existing_dates = (
        set(pd.read_csv(target, usecols=["date"], parse_dates=["date"])["date"])
        if target.exists() else set()
    )
    missing = incoming.loc[~incoming["date"].isin(existing_dates)].copy()
    rows = _atomic_merge(target, missing, ticker)
    result = {
        "ticker": ticker,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "status": "updated" if rows else "no_missing_rows",
        "rows_added": rows,
        "first_added_date": (
            missing["date"].min().strftime("%Y-%m-%d") if rows else None
        ),
        "last_added_date": (
            missing["date"].max().strftime("%Y-%m-%d") if rows else None
        ),
        "price_factor": price_factor,
        "volume_factor": volume_factor,
        "nasdaq_api": API.format(symbol=ticker),
        "source_url": source_url,
        "source_note": source_note,
        "verified_at": pd.Timestamp.now(tz="UTC").isoformat(),
    }
    provenance = (
        Path(PROJECT_PATH)
        / "output/data_provenance/nasdaq_official_history_backfill.json"
    )
    provenance.parent.mkdir(parents=True, exist_ok=True)
    previous = []
    if provenance.exists():
        try:
            previous = json.loads(
                provenance.read_text(encoding="utf-8")
            ).get("runs", [])
        except (OSError, json.JSONDecodeError):
            previous = []
    provenance.write_text(
        json.dumps({"runs": [*previous, result]}, indent=2),
        encoding="utf-8",
    )
    result["provenance"] = str(provenance)
    return result


def backfill_listed_universe_price_files(
    end: date,
    workers: int = 8,
    start: date = date(2023, 1, 1),
    universe_start: date = date(2024, 10, 5),
    limit: int | None = None,
) -> dict:
    """Fetch histories absent for members of the recovered listed universe."""
    snapshots = load_universe_snapshots()
    members = sorted({
        ticker
        for observed_at, symbols in snapshots.items()
        if pd.Timestamp(universe_start) <= observed_at <= pd.Timestamp(end)
        for ticker in symbols
    })
    price_dir = Path(CLEANED_PRICE_DATA_DIR)
    missing = [ticker for ticker in members if not (price_dir / f"{ticker.lower()}.csv").exists()]
    requested = missing[:limit] if limit else missing

    def fetch_full(ticker: str) -> dict:
        incoming = fetch_history(ticker, start, end)
        rows = _atomic_merge(price_dir / f"{ticker.lower()}.csv", incoming, ticker)
        return {"ticker": ticker, "status": "updated" if rows else "no_data", "rows": rows}

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_full, ticker): ticker for ticker in requested}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"ticker": ticker, "status": "failed", "error": str(exc)})
    counts = pd.Series([item["status"] for item in results], dtype="object").value_counts().to_dict()
    return {
        "start": start.isoformat(),
        "universe_start": universe_start.isoformat(),
        "end": end.isoformat(),
        "listed_members": len(members),
        "missing_before_run": len(missing),
        "requested": len(requested),
        "counts": counts,
        "no_data": sorted(item["ticker"] for item in results if item["status"] == "no_data"),
        "failures": [item for item in results if item["status"] == "failed"],
    }


def import_stooq_github_mirror(
    repository: str,
    commit: str,
    tickers: list[str],
    workers: int = 8,
    source_paths: dict[str, str] | None = None,
    adjustment_factors: dict[str, dict] | None = None,
) -> dict:
    """Append missing sessions from an immutable GitHub mirror of Stooq data.

    Existing sessions are never replaced, so this recovery source fills holes
    without silently changing prices already obtained from Nasdaq.
    """
    requested = {ticker.upper().strip() for ticker in tickers if ticker.strip()}
    paths = {
        ticker.upper(): path
        for ticker, path in (source_paths or {}).items()
        if ticker.upper() in requested
    }
    if source_paths is None:
        tree_url = (
            f"https://api.github.com/repos/{repository}/git/trees/"
            f"{commit}?recursive=1"
        )
        with urlopen(
            Request(tree_url, headers=HEADERS), timeout=60
        ) as response:
            tree = json.loads(response.read().decode("utf-8"))
        if tree.get("truncated"):
            raise RuntimeError(
                "GitHub tree response is truncated; refusing partial import"
            )
        for item in tree.get("tree", []):
            path = item.get("path", "")
            match = re.search(
                r"(?:^|/)([^/]+)\.us\.txt$",
                path,
                flags=re.IGNORECASE,
            )
            if match and match.group(1).upper() in requested:
                paths.setdefault(match.group(1).upper(), path)

    price_dir = Path(CLEANED_PRICE_DATA_DIR)

    def import_one(ticker: str) -> dict:
        path = paths.get(ticker)
        if path is None:
            return {"ticker": ticker, "status": "not_in_mirror", "rows": 0}
        source_url = (
            f"https://raw.githubusercontent.com/{repository}/{commit}/{quote(path)}"
        )
        with urlopen(Request(source_url, headers=HEADERS), timeout=60) as response:
            text = response.read().decode("utf-8-sig", errors="replace")
        try:
            raw = pd.read_csv(StringIO(text))
        except pd.errors.EmptyDataError:
            # Keep an empty pinned mirror file as explicit negative evidence;
            # it is deterministic and must not abort the rest of the batch.
            return {
                "ticker": ticker,
                "status": "empty_source",
                "rows": 0,
                "source_url": source_url,
            }
        required = {"<DATE>", "<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>", "<VOL>"}
        if not required.issubset(raw.columns):
            raise ValueError(f"{ticker}: invalid Stooq columns")
        incoming = raw.rename(columns={
            "<DATE>": "date", "<OPEN>": "open", "<HIGH>": "high",
            "<LOW>": "low", "<CLOSE>": "close", "<VOL>": "volume",
        })[["date", "open", "high", "low", "close", "volume"]]
        incoming["date"] = pd.to_datetime(
            incoming["date"].astype(str), format="%Y%m%d", errors="raise"
        )
        adjustment = (adjustment_factors or {}).get(ticker, {})
        price_factor = float(adjustment.get("price_factor", 1.0))
        volume_factor = float(adjustment.get("volume_factor", 1.0))
        if price_factor <= 0 or volume_factor <= 0:
            raise ValueError(
                f"{ticker}: adjustment factors must be positive"
            )
        target = price_dir / f"{ticker.lower()}.csv"
        adjustment_validation = _validate_price_adjustment_overlap(
            target,
            incoming,
            price_factor,
            volume_factor,
            int(adjustment.get("minimum_overlap_sessions", 20)),
            float(adjustment.get("relative_tolerance", 0.01)),
        )
        for column in ("open", "high", "low", "close"):
            incoming[column] = incoming[column] * price_factor
        incoming["volume"] = (
            incoming["volume"] * volume_factor
        ).round()
        incoming = _exclude_existing_price_dates(target, incoming)
        rows = _atomic_merge(target, incoming, ticker)
        return {
            "ticker": ticker,
            "status": "updated" if rows else "no_new_rows",
            "rows": rows,
            "first_date": incoming["date"].min().strftime("%Y-%m-%d") if rows else None,
            "last_date": incoming["date"].max().strftime("%Y-%m-%d") if rows else None,
            "source_url": source_url,
            "price_factor": price_factor,
            "volume_factor": volume_factor,
            "adjustment_source_url": adjustment.get(
                "source_url", ""
            ),
            "adjustment_note": adjustment.get("note", ""),
            "adjustment_validation": adjustment_validation,
        }

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(import_one, ticker): ticker for ticker in sorted(requested)}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"ticker": ticker, "status": "failed", "error": str(exc)})
    results.sort(key=lambda item: item["ticker"])
    report = {
        "repository": repository,
        "commit": commit,
        "path_discovery": (
            "verified_source_paths"
            if source_paths is not None else "recursive_git_tree"
        ),
        "requested": len(requested),
        "matched": len(paths),
        "counts": pd.Series(
            [item["status"] for item in results], dtype="object"
        ).value_counts().to_dict(),
        "results": results,
    }
    provenance = Path(PROJECT_PATH) / "output/data_provenance/stooq_github_import.json"
    provenance.parent.mkdir(parents=True, exist_ok=True)
    # Each batch can cover a different subset of historical symbols.  Keep the
    # full import ledger rather than replacing earlier source URLs and commits.
    # Top-level fields remain the latest run for backwards-compatible readers.
    prior_runs = []
    if provenance.exists():
        try:
            prior = json.loads(provenance.read_text(encoding="utf-8"))
            prior_runs = prior.get("runs", [
                {
                    key: prior[key]
                    for key in (
                        "repository", "commit", "path_discovery",
                        "requested", "matched", "counts", "results",
                    )
                    if key in prior
                }
            ])
        except (json.JSONDecodeError, OSError):
            prior_runs = []
    report["runs"] = [*prior_runs, {
        key: report[key]
        for key in (
            "repository", "commit", "path_discovery",
            "requested", "matched", "counts", "results",
        )
    }]
    provenance.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["provenance"] = str(provenance)
    return report


def import_stooq_git_mirror(
    repository: str | Path,
    commit: str,
    tickers: list[str],
    workers: int = 8,
) -> dict:
    """Fill missing sessions from a local immutable Stooq Git mirror.

    Unlike the historical GitHub helper, this path deliberately fills *every*
    missing date in an existing price file, not merely dates after its current
    last row.  Locally sourced Nasdaq rows always win on a date collision.
    A local clone avoids one HTTP request per ticker and makes a large initial
    historical repair practical.
    """
    repository = Path(repository).resolve()
    if not (repository / ".git").exists():
        raise ValueError(f"Not a Git repository: {repository}")
    resolved = subprocess.run(
        ["git", "rev-parse", f"{commit}^{{commit}}"],
        cwd=repository, check=True, capture_output=True, text=True,
    ).stdout.strip()
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", resolved],
        cwd=repository, check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    requested = {ticker.upper().strip() for ticker in tickers if ticker.strip()}
    paths: dict[str, str] = {}
    for path in listing:
        match = re.search(r"(?:^|/)([^/]+)\.us\.txt$", path, flags=re.IGNORECASE)
        if match and match.group(1).upper() in requested:
            paths.setdefault(match.group(1).upper(), path)
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=repository,
        capture_output=True, text=True,
    ).stdout.strip() or str(repository)
    price_dir = Path(CLEANED_PRICE_DATA_DIR)

    def import_one(ticker: str) -> dict:
        source_path = paths.get(ticker)
        if source_path is None:
            return {"ticker": ticker, "status": "not_in_mirror", "rows": 0}
        raw_text = subprocess.run(
            ["git", "show", f"{resolved}:{source_path}"], cwd=repository,
            check=True, capture_output=True, text=True,
        ).stdout
        try:
            raw = pd.read_csv(StringIO(raw_text))
        except pd.errors.EmptyDataError:
            # A pinned mirror can contain an intentionally empty placeholder
            # for a ticker.  This is a deterministic source result, not a
            # transient import failure; keep it visible in provenance so a
            # later batch can safely continue with the remaining symbols.
            return {
                "ticker": ticker,
                "status": "empty_source",
                "rows": 0,
                "source_path": source_path,
            }
        required = {"<DATE>", "<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>", "<VOL>"}
        if not required.issubset(raw.columns):
            raise ValueError(f"{ticker}: invalid Stooq columns")
        incoming = raw.rename(columns={
            "<DATE>": "date", "<OPEN>": "open", "<HIGH>": "high",
            "<LOW>": "low", "<CLOSE>": "close", "<VOL>": "volume",
        })[["date", "open", "high", "low", "close", "volume"]]
        incoming["date"] = pd.to_datetime(incoming["date"].astype(str), format="%Y%m%d")
        target = price_dir / f"{ticker.lower()}.csv"
        incoming = _exclude_existing_price_dates(target, incoming)
        rows = _atomic_merge(target, incoming, ticker)
        return {
            "ticker": ticker, "status": "updated" if rows else "no_missing_rows",
            "rows": rows, "source_path": source_path,
        }

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(import_one, ticker): ticker for ticker in sorted(requested)}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"ticker": ticker, "status": "failed", "error": str(exc)})
    results.sort(key=lambda item: item["ticker"])
    run = {
        "repository": remote, "commit": resolved, "requested": len(requested),
        "matched": len(paths),
        "counts": pd.Series([item["status"] for item in results], dtype="object").value_counts().to_dict(),
        "results": results,
    }
    provenance = Path(PROJECT_PATH) / "output/data_provenance/stooq_git_import.json"
    provenance.parent.mkdir(parents=True, exist_ok=True)
    previous_runs = []
    if provenance.exists():
        try:
            previous = json.loads(provenance.read_text(encoding="utf-8"))
            previous_runs = list(previous.get("runs", []))
        except (OSError, ValueError, TypeError):
            previous_runs = []
    report = dict(run)
    report["runs"] = previous_runs + [run]
    provenance.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["provenance"] = str(provenance)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat())
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--backfill-existing",
        action="store_true",
        help="Update every retained historical price file, not only the current universe",
    )
    parser.add_argument(
        "--backfill-listed-universe",
        action="store_true",
        help="Fetch missing price files for every recovered Nasdaq listed member",
    )
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--universe-start", default="2024-10-05")
    parser.add_argument(
        "--recover-universe-history",
        action="store_true",
        help="Recover validated Nasdaq universe snapshots from this repository's Git history",
    )
    parser.add_argument(
        "--import-listings-history",
        metavar="GIT_REPOSITORY",
        help="Import dated data/nasdaq-listed.csv revisions from a Git repository",
    )
    parser.add_argument(
        "--import-nasdaq-trader-git-history",
        metavar="GIT_REPOSITORY",
        help="Import every committed Nasdaq Trader nasdaqlisted.txt snapshot",
    )
    parser.add_argument(
        "--nasdaq-trader-source-path",
        default="data/nasdaqlisted.txt",
        help="Repository-relative path used by --import-nasdaq-trader-git-history",
    )
    parser.add_argument(
        "--import-nasdaq-trader-file",
        action="append",
        default=[],
        help="Import an archived Nasdaq Trader nasdaqlisted pipe-delimited file",
    )
    parser.add_argument(
        "--import-symbol-list",
        help="Import PATH=YYYY-MM-DD for a stripped one-symbol-per-line archive",
    )
    parser.add_argument(
        "--import-json-catalog",
        help="Import SOURCE=YYYY-MM-DD for a JSON symbol/name catalog",
    )
    parser.add_argument(
        "--import-csv-catalog",
        help="Import SOURCE=YYYY-MM-DD for a two-column symbol/name CSV",
    )
    parser.add_argument(
        "--import-stooq-github-mirror",
        help="Import missing sessions from immutable OWNER/REPO@COMMIT Stooq files",
    )
    parser.add_argument(
        "--import-stooq-git-mirror",
        help="Fill all missing sessions from local GIT_REPOSITORY@COMMIT Stooq files",
    )
    parser.add_argument(
        "--tickers",
        help=(
            "Comma-separated tickers for a targeted price update or a Stooq "
            "mirror import. Targeted updates retain the formal universe."
        ),
    )
    args = parser.parse_args()
    if args.limit is not None and args.tickers:
        parser.error("--limit and --tickers are mutually exclusive")
    if args.import_stooq_git_mirror:
        if not args.tickers:
            parser.error("--tickers is required with --import-stooq-git-mirror")
        repository, commit = args.import_stooq_git_mirror.rsplit("@", 1)
        result = import_stooq_git_mirror(
            repository, commit, args.tickers.split(","), args.workers
        )
    elif args.import_nasdaq_trader_git_history:
        result = import_nasdaq_trader_git_history(
            args.import_nasdaq_trader_git_history, args.nasdaq_trader_source_path
        )
    elif args.import_listings_history:
        result = import_nasdaq_listings_history(args.import_listings_history)
    elif args.import_nasdaq_trader_file:
        result = import_nasdaq_trader_files(args.import_nasdaq_trader_file)
    elif args.import_symbol_list:
        symbol_path, observed_at = args.import_symbol_list.rsplit("=", 1)
        result = import_nasdaq_symbol_list(
            symbol_path, date.fromisoformat(observed_at)
        )
    elif args.import_json_catalog:
        catalog_source, observed_at = args.import_json_catalog.rsplit("=", 1)
        result = import_nasdaq_json_catalog(
            catalog_source, date.fromisoformat(observed_at)
        )
    elif args.import_csv_catalog:
        catalog_source, observed_at = args.import_csv_catalog.rsplit("=", 1)
        result = import_nasdaq_csv_catalog(
            catalog_source, date.fromisoformat(observed_at)
        )
    elif args.import_stooq_github_mirror:
        if not args.tickers:
            parser.error("--tickers is required with --import-stooq-github-mirror")
        repository, commit = args.import_stooq_github_mirror.rsplit("@", 1)
        result = import_stooq_github_mirror(
            repository, commit, args.tickers.split(","), args.workers
        )
    elif args.recover_universe_history:
        result = recover_git_universe_snapshots()
    elif args.backfill_listed_universe:
        result = backfill_listed_universe_price_files(
            date.fromisoformat(args.end), args.workers, date.fromisoformat(args.start),
            date.fromisoformat(args.universe_start), args.limit
        )
    elif args.backfill_existing:
        result = backfill_existing_price_files(date.fromisoformat(args.end), args.workers)
    else:
        result = update_all(
            date.fromisoformat(args.end),
            args.workers,
            args.limit,
            args.tickers.split(",") if args.tickers else None,
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
