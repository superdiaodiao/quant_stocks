"""Research-only triage of StockAnalysis historical-price candidates.

The script caches the exact public HTML used for each review, verifies its
SHA-256 on offline replay, and measures the source's actual overlap and gap
coverage against the project's local price files.  It never writes formal
price CSVs, terminal returns, security identities, coverage files, or
validation artifacts.  A passing candidate only means that the evidence is
ready for a separate source/licensing and formal-data decision.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import math
import os
import re
from collections import Counter
from html import unescape
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE, PROJECT_PATH


SOURCE_NAME = "StockAnalysis public historical-price page"
SOURCE_PROVIDER = "S&P Global Market Intelligence (as disclosed by page)"
SOURCE_URL_TEMPLATE = "https://stockanalysis.com/stocks/{ticker}/history/"
SOURCE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": "Mozilla/5.0 (compatible; quant_stocks research-only triage)",
}
DEFAULT_SEC_TRIAGE = (
    Path(PROJECT_PATH) / "output/data_provenance/sec_submission_triage.json"
)
DEFAULT_CACHE_DIR = (
    Path(PROJECT_PATH) / "output/data_provenance/stockanalysis_price_triage_cache"
)
DEFAULT_OUTPUT = (
    Path(PROJECT_PATH) / "output/data_provenance/stockanalysis_price_triage.json"
)
DEFAULT_ANALYSIS_END = "2026-07-17"

PRICE_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
_JAVASCRIPT_KEY = re.compile(r"(?<=[{,])([A-Za-z_$][A-Za-z0-9_$]*)(?=\s*:)")
_JAVASCRIPT_LEADING_DECIMAL = re.compile(r"(?<![0-9])(-?)\.(\d+)")


def _normalized_tickers(tickers: list[str]) -> list[str]:
    return list(dict.fromkeys(
        str(ticker).upper().strip() for ticker in tickers if str(ticker).strip()
    ))


def _source_url(ticker: str) -> str:
    return SOURCE_URL_TEMPLATE.format(ticker=str(ticker).lower())


def _cache_path(cache_dir: str | Path, ticker: str) -> Path:
    return Path(cache_dir) / f"{str(ticker).upper()}.json.gz"


def _payload_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _portable_project_path(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(Path(PROJECT_PATH).resolve()).as_posix()
    except ValueError:
        return str(resolved)


def fetch_stockanalysis_history_page(ticker: str, timeout: int = 30) -> bytes:
    """Fetch one public history page for research-only source evaluation."""
    request = Request(_source_url(ticker), headers=SOURCE_HEADERS)
    with urlopen(request, timeout=timeout) as response:
        payload = response.read()
    if not payload.lstrip().lower().startswith(b"<!doctype html"):
        raise ValueError(f"{ticker}: expected HTML history page")
    return payload


def _write_cached_page(
    cache_dir: str | Path,
    ticker: str,
    payload: bytes,
) -> dict:
    normalized = str(ticker).upper()
    path = _cache_path(cache_dir, normalized)
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "format_version": 1,
        "ticker": normalized,
        "source_name": SOURCE_NAME,
        "source_provider": SOURCE_PROVIDER,
        "source_url": _source_url(normalized),
        "fetched_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "payload_sha256": _payload_sha256(payload),
        "payload_base64": base64.b64encode(payload).decode("ascii"),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(envelope, handle, ensure_ascii=False, sort_keys=True)
    os.replace(temporary, path)
    return envelope


def _read_cached_page(cache_dir: str | Path, ticker: str) -> tuple[dict, bytes]:
    normalized = str(ticker).upper()
    path = _cache_path(cache_dir, normalized)
    if not path.exists():
        raise FileNotFoundError(f"missing cached price-source page: {path}")
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    required = {
        "format_version",
        "ticker",
        "source_name",
        "source_provider",
        "source_url",
        "fetched_at",
        "payload_sha256",
        "payload_base64",
    }
    missing = required - set(envelope)
    if missing:
        raise ValueError(
            f"{normalized}: cached source page missing {sorted(missing)}"
        )
    if str(envelope["ticker"]).upper() != normalized:
        raise ValueError(f"{normalized}: cached page ticker does not match path")
    if envelope["source_url"] != _source_url(normalized):
        raise ValueError(f"{normalized}: cached page source URL does not match")
    try:
        payload = base64.b64decode(envelope["payload_base64"], validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{normalized}: cached page payload is not base64") from exc
    if _payload_sha256(payload) != envelope["payload_sha256"]:
        raise ValueError(f"{normalized}: cached page payload hash mismatch")
    return envelope, payload


def _load_page(
    ticker: str,
    cache_dir: str | Path,
    *,
    refresh: bool,
    fetcher: Callable[[str], bytes] = fetch_stockanalysis_history_page,
) -> tuple[dict, bytes]:
    if refresh:
        payload = fetcher(ticker)
        return _write_cached_page(cache_dir, ticker, payload), payload
    return _read_cached_page(cache_dir, ticker)


def _balanced_array(text: str, start: int) -> str:
    """Return the JavaScript array beginning at ``start`` without eval()."""
    if start < 0 or start >= len(text) or text[start] != "[":
        raise ValueError("history payload does not start with an array")
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"', "`"}:
            quote = character
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise ValueError("history payload has an unclosed array")


def _history_array_from_html(payload: bytes) -> list[dict]:
    text = payload.decode("utf-8", errors="strict")
    page_data = text.find("data:{id:")
    if page_data < 0:
        raise ValueError("history page lacks embedded source data")
    marker = text.find("data:[", page_data)
    if marker < 0:
        raise ValueError("history page lacks embedded price array")
    javascript = _balanced_array(text, marker + len("data:"))
    normalized = _JAVASCRIPT_KEY.sub(r'"\1"', javascript)
    # The Svelte payload uses JavaScript's valid ``-.07`` / ``.1`` literals;
    # normalize only those leading decimals before feeding the data to JSON.
    normalized = _JAVASCRIPT_LEADING_DECIMAL.sub(r"\g<1>0.\2", normalized)
    normalized = normalized.replace("void 0", "null")
    try:
        values = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ValueError("history page has an unparseable price array") from exc
    if not isinstance(values, list):
        raise ValueError("history page price payload is not a list")
    return values


def parse_stockanalysis_history(payload: bytes) -> tuple[pd.DataFrame, str | None]:
    """Extract the page's displayed OHLCV rows without treating them as formal data."""
    text = payload.decode("utf-8", errors="strict")
    provider_match = re.search(
        r"Historical price data is provided by\s*<a[^>]*>(.*?)</a>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    provider = (
        unescape(re.sub(r"<[^>]+>", "", provider_match.group(1))).strip()
        if provider_match else None
    )
    rows = []
    for raw in _history_array_from_html(payload):
        if not isinstance(raw, dict):
            raise ValueError("history page contains a non-object price row")
        missing = {"t", "o", "h", "l", "c", "v"} - set(raw)
        if missing:
            labels = {
                "t": "date", "o": "open", "h": "high", "l": "low",
                "c": "close", "v": "volume",
            }
            raise ValueError(
                "history page price row missing "
                + ", ".join(labels[value] for value in sorted(missing))
            )
        date = pd.to_datetime(raw["t"], errors="coerce")
        values = {
            "open": pd.to_numeric(raw["o"], errors="coerce"),
            "high": pd.to_numeric(raw["h"], errors="coerce"),
            "low": pd.to_numeric(raw["l"], errors="coerce"),
            "close": pd.to_numeric(raw["c"], errors="coerce"),
            "volume": pd.to_numeric(raw["v"], errors="coerce"),
        }
        if pd.isna(date) or any(pd.isna(value) for value in values.values()):
            raise ValueError("history page contains invalid OHLCV values")
        numeric_values = {name: float(value) for name, value in values.items()}
        if (
            any(
                not math.isfinite(numeric_values[name]) or numeric_values[name] <= 0
                for name in ("open", "high", "low", "close")
            )
            or not math.isfinite(numeric_values["volume"])
            or numeric_values["volume"] < 0
        ):
            raise ValueError("history page contains non-positive prices or volume")
        rows.append({"date": pd.Timestamp(date).normalize(), **numeric_values})
    frame = pd.DataFrame(rows, columns=PRICE_COLUMNS)
    if frame.empty:
        raise ValueError("history page contains no price rows")
    duplicates = frame["date"].duplicated(keep=False)
    if duplicates.any():
        values = frame.loc[duplicates, "date"].dt.strftime("%Y-%m-%d").tolist()
        raise ValueError("history page has duplicate dates: " + ", ".join(values))
    return frame.sort_values("date").reset_index(drop=True), provider


def _local_prices(ticker: str, price_dir: str | Path) -> pd.DataFrame:
    path = Path(price_dir) / f"{str(ticker).lower()}.csv"
    if not path.exists():
        return pd.DataFrame(columns=["date", "close", "volume"])
    frame = pd.read_csv(path, usecols=["date", "close", "volume"])
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    return frame.dropna(subset=["date"]).sort_values("date")


def _overlap_summary(
    local: pd.DataFrame,
    source: pd.DataFrame,
    tolerance: float,
) -> dict:
    overlap = local.rename(columns={
        "close": "local_close",
        "volume": "local_volume",
    }).merge(
        source[["date", "close", "volume"]].rename(columns={
            "close": "source_close",
            "volume": "source_volume",
        }),
        on="date",
    )
    overlap = overlap.loc[
        overlap["local_close"].gt(0)
        & overlap["source_close"].gt(0)
        & overlap["local_volume"].gt(0)
        & overlap["source_volume"].gt(0)
    ]
    if overlap.empty:
        return {
            "overlap_sessions": 0,
            "relative_tolerance": tolerance,
            "price_ratio_median": None,
            "volume_ratio_median": None,
            "price_ratio_within_tolerance_fraction": None,
            "volume_ratio_within_tolerance_fraction": None,
        }
    price_ratio = overlap["local_close"] / overlap["source_close"]
    volume_ratio = overlap["local_volume"] / overlap["source_volume"]
    price_median = float(price_ratio.median())
    volume_median = float(volume_ratio.median())
    return {
        "overlap_sessions": int(len(overlap)),
        "relative_tolerance": tolerance,
        "price_ratio_median": price_median,
        "volume_ratio_median": volume_median,
        "price_ratio_within_tolerance_fraction": float(
            ((price_ratio / price_median - 1).abs() <= tolerance).mean()
        ),
        "volume_ratio_within_tolerance_fraction": float(
            ((volume_ratio / volume_median - 1).abs() <= tolerance).mean()
        ),
    }


def _coverage_summary(
    local: pd.DataFrame,
    source: pd.DataFrame,
    benchmark_dates: pd.Series | pd.DatetimeIndex,
    analysis_end: pd.Timestamp,
) -> dict:
    benchmark = pd.DatetimeIndex(pd.to_datetime(benchmark_dates).dropna())
    benchmark = benchmark.normalize().unique().sort_values()
    last_local = (
        pd.Timestamp(local["date"].max()).normalize() if not local.empty else None
    )
    expected = benchmark[
        (benchmark <= analysis_end)
        & (benchmark > last_local if last_local is not None else True)
    ]
    source_dates = set(source["date"])
    covered = expected.intersection(pd.DatetimeIndex(source_dates))
    missing = expected.difference(pd.DatetimeIndex(source_dates))
    source_end = min(pd.Timestamp(source["date"].max()).normalize(), analysis_end)
    bridge_expected = benchmark[
        (benchmark <= source_end)
        & (benchmark > last_local if last_local is not None else False)
    ]
    bridge_covered = bridge_expected.intersection(pd.DatetimeIndex(source_dates))
    bridge_missing = bridge_expected.difference(pd.DatetimeIndex(source_dates))
    return {
        "last_local_price_date": (
            last_local.strftime("%Y-%m-%d") if last_local is not None else None
        ),
        "expected_gap_sessions": int(len(expected)),
        "source_covered_gap_sessions": int(len(covered)),
        "source_missing_gap_sessions": int(len(missing)),
        "source_missing_gap_session_sample": [
            value.strftime("%Y-%m-%d") for value in missing[:20]
        ],
        "source_covers_full_gap": bool(len(expected) == len(covered)),
        "source_bridge_end_date": source_end.strftime("%Y-%m-%d"),
        "source_bridge_expected_sessions": int(len(bridge_expected)),
        "source_bridge_covered_sessions": int(len(bridge_covered)),
        "source_bridge_missing_sessions": int(len(bridge_missing)),
        "source_bridge_missing_session_sample": [
            value.strftime("%Y-%m-%d") for value in bridge_missing[:20]
        ],
        "source_bridges_from_local_to_source_end": bool(
            last_local is not None and len(bridge_expected) == len(bridge_covered)
        ),
    }


def _assessment(
    coverage: dict,
    overlap: dict,
    minimum_overlap_sessions: int,
    sec_resolution_review: str | None,
) -> str:
    if not coverage["source_bridges_from_local_to_source_end"]:
        return "REVIEW_SOURCE_GAP_BEFORE_SOURCE_END"
    if (
        not coverage["source_covers_full_gap"]
        and sec_resolution_review == "PRICE_SOURCE_AND_TERMINAL_RETURN_REVIEW"
    ):
        return "REVIEW_CONTINUOUS_BRIDGE_REQUIRES_TERMINAL_EVIDENCE"
    if not coverage["source_covers_full_gap"]:
        return "REVIEW_SOURCE_COVERAGE_INCOMPLETE"
    if overlap["overlap_sessions"] < minimum_overlap_sessions:
        return "REVIEW_INSUFFICIENT_OVERLAP"
    price_consistent = (
        overlap["price_ratio_within_tolerance_fraction"] is not None
        and overlap["price_ratio_within_tolerance_fraction"] >= 0.95
    )
    volume_consistent = (
        overlap["volume_ratio_within_tolerance_fraction"] is not None
        and overlap["volume_ratio_within_tolerance_fraction"] >= 0.95
    )
    if not (price_consistent and volume_consistent):
        return "REVIEW_OVERLAP_MISMATCH"
    return "REVIEW_REQUIRES_LICENSE_AND_FORMAL_DATA_AUTHORIZATION"


def load_lead_tickers(path: str | Path = DEFAULT_SEC_TRIAGE) -> list[str]:
    """Load unresolved price-review tickers from the SEC research queue."""
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    return _normalized_tickers([
        row.get("ticker", "")
        for row in report.get("records", [])
        if row.get("status") == "RESEARCH_LEAD_ONLY"
    ])


def load_sec_review_context(path: str | Path = DEFAULT_SEC_TRIAGE) -> dict[str, str]:
    """Load existing SEC research labels without converting them into proof."""
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        str(row.get("ticker", "")).upper(): str(row["resolution_review"])
        for row in report.get("records", [])
        if row.get("ticker") and row.get("resolution_review")
    }


def triage_stockanalysis_prices(
    tickers: list[str],
    *,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    analysis_end: str | pd.Timestamp = DEFAULT_ANALYSIS_END,
    benchmark_dates: pd.Series | pd.DatetimeIndex | None = None,
    fetcher: Callable[[str], bytes] = fetch_stockanalysis_history_page,
    minimum_overlap_sessions: int = 20,
    relative_tolerance: float = 0.01,
    sec_review_context: dict[str, str] | None = None,
) -> dict:
    """Evaluate cached public-page coverage without touching formal market data."""
    if minimum_overlap_sessions <= 0:
        raise ValueError("minimum_overlap_sessions must be positive")
    if relative_tolerance < 0:
        raise ValueError("relative_tolerance must be non-negative")
    normalized = _normalized_tickers(tickers)
    parsed_end = pd.Timestamp(analysis_end).normalize()
    if benchmark_dates is None:
        benchmark_dates = pd.read_csv(
            NASDAQ_INDEX_FILE, usecols=["date"], parse_dates=["date"]
        )["date"]
    if sec_review_context is None:
        try:
            sec_review_context = load_sec_review_context()
        except (FileNotFoundError, json.JSONDecodeError):
            sec_review_context = {}
    rows = []
    for ticker in normalized:
        try:
            envelope, payload = _load_page(
                ticker, cache_dir, refresh=refresh, fetcher=fetcher
            )
            source, disclosed_provider = parse_stockanalysis_history(payload)
            local = _local_prices(ticker, price_dir)
            coverage = _coverage_summary(local, source, benchmark_dates, parsed_end)
            overlap = _overlap_summary(local, source, relative_tolerance)
            sec_resolution_review = sec_review_context.get(ticker)
            rows.append({
                "ticker": ticker,
                "status": "RESEARCH_LEAD_ONLY",
                "assessment": _assessment(
                    coverage,
                    overlap,
                    minimum_overlap_sessions,
                    sec_resolution_review,
                ),
                "source_name": SOURCE_NAME,
                "source_provider": disclosed_provider or SOURCE_PROVIDER,
                "source_url": envelope["source_url"],
                "source_date_start": source["date"].min().strftime("%Y-%m-%d"),
                "source_date_end": source["date"].max().strftime("%Y-%m-%d"),
                "source_row_count": int(len(source)),
                "coverage": coverage,
                "overlap": overlap,
                "sec_resolution_review": sec_resolution_review,
                "cache_path": _portable_project_path(_cache_path(cache_dir, ticker)),
                "cache_payload_sha256": envelope["payload_sha256"],
                "cache_fetched_at": envelope["fetched_at"],
                "next_required_evidence": (
                    "Obtain source/license approval, independently review the "
                    "cached overlap and coverage, then request explicit formal "
                    "price-data authorization before any import."
                ),
            })
        except FileNotFoundError as exc:
            rows.append({
                "ticker": ticker,
                "status": "CACHE_MISSING",
                "assessment": "REVIEW_SOURCE_CACHE_REQUIRED",
                "error": str(exc),
            })
        except Exception as exc:
            rows.append({
                "ticker": ticker,
                "status": "SOURCE_OR_CACHE_ERROR",
                "assessment": "REVIEW_SOURCE_PARSE_OR_FETCH_ERROR",
                "error": str(exc),
            })
    counts = Counter(row["status"] for row in rows)
    assessment_counts = Counter(row["assessment"] for row in rows)
    return {
        "format_version": 1,
        "mode": "refresh" if refresh else "offline_cache",
        "research_only": True,
        "analysis_end": parsed_end.strftime("%Y-%m-%d"),
        "source_name": SOURCE_NAME,
        "source_provider": SOURCE_PROVIDER,
        "warning": (
            "This report evaluates cached public-page evidence only. It never "
            "writes formal price files, terminal returns, security identities, "
            "coverage files, or validation artifacts. It is not a source license "
            "or approval to import data."
        ),
        "requested_ticker_count": len(normalized),
        "counts": dict(sorted(counts.items())),
        "assessment_counts": dict(sorted(assessment_counts.items())),
        "records": rows,
    }


def _write_report(path: str | Path, report: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tickers",
        help="Comma-separated tickers; defaults to the SEC research lead queue.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch and atomically cache the current public history pages.",
    )
    parser.add_argument("--sec-triage", default=str(DEFAULT_SEC_TRIAGE))
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--analysis-end", default=DEFAULT_ANALYSIS_END)
    parser.add_argument("--minimum-overlap-sessions", type=int, default=20)
    parser.add_argument("--relative-tolerance", type=float, default=0.01)
    args = parser.parse_args()
    tickers = (
        args.tickers.split(",") if args.tickers else load_lead_tickers(args.sec_triage)
    )
    report = triage_stockanalysis_prices(
        tickers,
        price_dir=args.price_dir,
        cache_dir=args.cache_dir,
        refresh=args.refresh,
        analysis_end=args.analysis_end,
        minimum_overlap_sessions=args.minimum_overlap_sessions,
        relative_tolerance=args.relative_tolerance,
        sec_review_context=load_sec_review_context(args.sec_triage),
    )
    _write_report(args.output, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
