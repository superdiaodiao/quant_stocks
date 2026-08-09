"""Repair missing historical price tails from Yahoo Chart evidence.

Yahoo's chart endpoint returns historical quotes adjusted for later split
events.  This research-only repair reverses those future split adjustments,
requires a stable local overlap before importing anything, and appends only
dates that are absent from the local price file.  Existing rows are never
replaced.  Raw payloads and validation results are retained for review; this
script does not modify financial inputs or terminal-return evidence.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH


HISTORY_START = pd.Timestamp("2021-01-01")
ANALYSIS_END = pd.Timestamp("2026-07-17")
YAHOO_BASE = "https://query2.finance.yahoo.com/v8/finance/chart"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
PRICE_COLUMNS = ["date", "ticker", "open", "high", "low", "close", "volume"]
PRICE_FIELDS = ["open", "high", "low", "close"]
MIN_OVERLAP_SESSIONS = 20
OVERLAP_TOLERANCE = 0.01
MIN_OVERLAP_FRACTION = 0.95
DEFAULT_CACHE_DIR = Path(PROJECT_PATH) / "output/data_provenance/yahoo_historical_price_cache"
DEFAULT_OUTPUT = Path(PROJECT_PATH) / "output/data_provenance/yahoo_historical_price_repair.json"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _yahoo_url(ticker: str, start: str, end: str) -> str:
    period1 = int(pd.Timestamp(start, tz="UTC").timestamp())
    period2 = int((pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)).timestamp())
    query = urlencode(
        {
            "period1": period1,
            "period2": period2,
            "interval": "1d",
            "events": "div,splits",
            "includeAdjustedClose": "true",
        }
    )
    return f"{YAHOO_BASE}/{ticker}?{query}"


def _request_bytes(url: str, retries: int = 3) -> bytes:
    error: Exception | None = None
    for attempt in range(retries):
        try:
            return urlopen(Request(url, headers=HEADERS), timeout=30).read()
        except HTTPError as exc:  # pragma: no cover - network-dependent path
            if exc.code in {400, 404}:
                raise RuntimeError(f"Yahoo source unavailable ({exc.code}): {url}") from exc
            error = exc
            if attempt + 1 < retries:
                time.sleep((2**attempt) + 0.1)
        except Exception as exc:  # pragma: no cover - network-dependent path
            error = exc
            if attempt + 1 < retries:
                time.sleep((2**attempt) + 0.1)
    raise RuntimeError(f"Yahoo request failed: {error}")


def _split_events(result: dict) -> list[dict]:
    events = ((result.get("events") or {}).get("splits") or {}).items()
    parsed: list[dict] = []
    for timestamp, event in events:
        try:
            numerator = float(event["numerator"])
            denominator = float(event["denominator"])
            event_date = pd.to_datetime(int(timestamp), unit="s", utc=True).tz_convert(None).normalize()
            factor = denominator / numerator
            if factor <= 0 or not math.isfinite(factor):
                continue
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        parsed.append(
            {
                "date": event_date.strftime("%Y-%m-%d"),
                "numerator": numerator,
                "denominator": denominator,
                "split_ratio": event.get("splitRatio"),
                "future_price_factor": factor,
            }
        )
    return sorted(parsed, key=lambda item: item["date"])


def _parse_yahoo(payload: bytes, ticker: str) -> tuple[pd.DataFrame, dict]:
    document = json.loads(payload.decode("utf-8"))
    chart = document.get("chart") or {}
    if chart.get("error") or not chart.get("result"):
        raise ValueError(f"Yahoo returned no result for {ticker}: {chart.get('error')}")
    result = chart["result"][0]
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    if not timestamps or not quote.get("close"):
        raise ValueError(f"Yahoo returned no quote rows for {ticker}")

    dates = pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(None).normalize()
    raw = pd.DataFrame(
        {
            "date": dates,
            "ticker": ticker,
            "open": quote.get("open", []),
            "high": quote.get("high", []),
            "low": quote.get("low", []),
            "close": quote.get("close", []),
            "volume": quote.get("volume", []),
        },
        columns=PRICE_COLUMNS,
    )
    for column in PRICE_COLUMNS[2:]:
        raw[column] = pd.to_numeric(raw[column], errors="coerce")
    raw = raw.dropna(subset=["date", "close"])
    raw = raw.loc[raw["close"] > 0].drop_duplicates("date", keep="last").sort_values("date")
    if raw.empty:
        raise ValueError(f"Yahoo returned no positive close rows for {ticker}")

    splits = _split_events(result)
    # Do not blindly apply the split events.  Yahoo and the local Nasdaq
    # history may both encode a split as a discontinuity, or both may already
    # be adjusted.  The overlap check below determines the stable scalar
    # conversion actually needed for this provider pair; an event list is
    # retained as provenance and as a diagnostic for scale changes.
    raw["volume"] = raw["volume"].fillna(0.0)
    metadata = result.get("meta") or {}
    metadata = {
        "symbol": metadata.get("symbol"),
        "exchange_name": metadata.get("exchangeName"),
        "full_exchange_name": metadata.get("fullExchangeName"),
        "instrument_type": metadata.get("instrumentType"),
        "long_name": metadata.get("longName"),
        "short_name": metadata.get("shortName"),
        "first_trade_date": metadata.get("firstTradeDate"),
        "split_events": splits,
    }
    return raw[PRICE_COLUMNS].reset_index(drop=True), metadata


def _read_prices(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=PRICE_COLUMNS)
    frame = pd.read_csv(path, parse_dates=["date"])
    for column in PRICE_COLUMNS:
        if column not in frame.columns:
            raise ValueError(f"{path} is missing required column {column}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    return frame.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def _overlap_validation(source: pd.DataFrame, local: pd.DataFrame) -> dict:
    source = source[["date", *PRICE_FIELDS, "volume"]].copy()
    local = local[["date", *PRICE_FIELDS, "volume"]].copy()
    overlap = local.merge(source, on="date", suffixes=("_local", "_source"))
    if overlap.empty:
        return {
            "sessions": 0,
            "close_median_ratio": None,
            "close_within_1pct": 0.0,
            "ohlc_within_1pct": 0.0,
            "field_median_ratios": {},
            "volume_median_ratio": None,
            "passed": False,
        }
    close_local = pd.to_numeric(overlap["close_local"], errors="coerce")
    close_source = pd.to_numeric(overlap["close_source"], errors="coerce")
    meaningful = close_local.gt(0) & close_source.gt(0)
    ratios = (close_local[meaningful] / close_source[meaningful]).replace([math.inf, -math.inf], pd.NA).dropna()
    if ratios.empty:
        return {
            "sessions": int(len(overlap)),
            "close_median_ratio": None,
            "close_within_1pct": 0.0,
            "ohlc_within_1pct": 0.0,
            "field_median_ratios": {},
            "volume_median_ratio": None,
            "passed": False,
        }
    median_ratio = float(ratios.median())
    normalized_close = (ratios / median_ratio - 1.0).abs().le(OVERLAP_TOLERANCE)
    field_fractions = {}
    field_median_ratios = {}
    for field in PRICE_FIELDS:
        left = pd.to_numeric(overlap[f"{field}_local"], errors="coerce")
        right = pd.to_numeric(overlap[f"{field}_source"], errors="coerce")
        valid = left.gt(0) & right.gt(0)
        if not valid.any():
            field_fractions[field] = 0.0
            continue
        field_ratios = (left[valid] / right[valid]).replace([math.inf, -math.inf], pd.NA).dropna()
        field_median_ratios[field] = float(field_ratios.median())
        field_fractions[field] = float(
            (field_ratios / field_median_ratios[field] - 1.0).abs().le(OVERLAP_TOLERANCE).mean()
        )
    ohlc_fraction = min(field_fractions.values()) if field_fractions else 0.0
    volume_local = pd.to_numeric(overlap.get("volume_local"), errors="coerce")
    volume_source = pd.to_numeric(overlap.get("volume_source"), errors="coerce")
    volume_valid = volume_local.gt(0) & volume_source.gt(0)
    volume_ratios = (volume_local[volume_valid] / volume_source[volume_valid]).replace(
        [math.inf, -math.inf], pd.NA
    ).dropna()
    volume_median_ratio = float(volume_ratios.median()) if not volume_ratios.empty else None
    scale_consistent = all(
        abs(value / median_ratio - 1.0) <= 0.05 for value in field_median_ratios.values()
    )
    return {
        "sessions": int(len(overlap)),
        "close_median_ratio": median_ratio,
        "close_within_1pct": float(normalized_close.mean()),
        "ohlc_within_1pct": float(ohlc_fraction),
        "field_within_1pct": field_fractions,
        "field_median_ratios": field_median_ratios,
        "volume_median_ratio": volume_median_ratio,
        "scale_consistent": scale_consistent,
        "passed": bool(
            len(ratios) >= MIN_OVERLAP_SESSIONS
            and float(normalized_close.mean()) >= MIN_OVERLAP_FRACTION
            and ohlc_fraction >= MIN_OVERLAP_FRACTION
            and scale_consistent
        ),
    }


def _merge_missing(path: Path, incoming: pd.DataFrame, ticker: str) -> int:
    if incoming.empty:
        return 0
    existing = _read_prices(path)
    existing_dates = set(existing["date"])
    incoming = incoming.loc[~incoming["date"].isin(existing_dates)].copy()
    if incoming.empty:
        return 0
    incoming["ticker"] = ticker
    incoming = incoming[PRICE_COLUMNS]
    combined = pd.concat([existing, incoming], ignore_index=True)
    combined = combined.drop_duplicates("date", keep="first").sort_values("date")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    combined.to_csv(temp, index=False)
    temp.replace(path)
    return int(len(incoming))


def _load_or_fetch(cache_dir: Path, ticker: str, url: str, refresh: bool) -> tuple[bytes, str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{ticker.upper()}.json.gz"
    if target.exists() and not refresh:
        return gzip.decompress(target.read_bytes()), str(target)
    payload = _request_bytes(url)
    target.write_bytes(gzip.compress(payload, mtime=0))
    return payload, str(target)


def repair_one(
    ticker: str,
    start: str = HISTORY_START.strftime("%Y-%m-%d"),
    end: str = ANALYSIS_END.strftime("%Y-%m-%d"),
    *,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
) -> dict:
    ticker = ticker.upper().strip()
    price_path = Path(price_dir) / f"{ticker.lower()}.csv"
    url = _yahoo_url(ticker, start, end)
    result = {
        "ticker": ticker,
        "research_only": True,
        "status": "",
        "source_url": url,
        "price_path": str(price_path),
    }
    try:
        if not price_path.exists():
            result["status"] = "NO_LOCAL_REFERENCE"
            return result
        payload, cache_path = _load_or_fetch(Path(cache_dir), ticker, url, refresh)
        source, metadata = _parse_yahoo(payload, ticker)
        local = _read_prices(price_path)
        full_overlap = _overlap_validation(source, local)
        overlap = full_overlap
        if not full_overlap["passed"]:
            # A provider can encode an old split differently while agreeing
            # exactly on the current tail.  Limit this fallback to the last
            # 60 local sessions and retain the failed full-history result.
            tail_dates = set(local["date"].sort_values().tail(60))
            tail_local = local.loc[local["date"].isin(tail_dates)]
            tail_source = source.loc[source["date"].isin(tail_dates)]
            tail_overlap = _overlap_validation(tail_source, tail_local)
            if tail_overlap["passed"]:
                overlap = {
                    **tail_overlap,
                    "validation_scope": "recent_tail_60",
                    "full_history_overlap": full_overlap,
                }
            else:
                overlap = {
                    **full_overlap,
                    "validation_scope": "full_history",
                    "recent_tail_overlap": tail_overlap,
                }
        else:
            overlap = {**full_overlap, "validation_scope": "full_history"}
        result.update(
            {
                "source_payload_sha256": _sha256(payload),
                "cache_path": cache_path,
                "metadata": metadata,
                "source_rows": int(len(source)),
                "source_first_date": source["date"].min().strftime("%Y-%m-%d") if len(source) else None,
                "source_last_date": source["date"].max().strftime("%Y-%m-%d") if len(source) else None,
                "local_rows_before": int(len(local)),
                "local_first_date": local["date"].min().strftime("%Y-%m-%d") if len(local) else None,
                "local_last_date": local["date"].max().strftime("%Y-%m-%d") if len(local) else None,
                "overlap": overlap,
            }
        )
        if metadata.get("instrument_type") not in {None, "EQUITY"}:
            result["status"] = "REJECT_NON_EQUITY"
            return result
        if not overlap["passed"]:
            result["status"] = "REJECT_CROSS_VALIDATION"
            return result
        source = source.copy()
        price_scale = float(overlap["close_median_ratio"])
        for field in PRICE_FIELDS:
            source[field] = source[field] * price_scale
        if overlap.get("volume_median_ratio") is not None:
            source["volume"] = source["volume"] * float(overlap["volume_median_ratio"])
        rows_added = _merge_missing(price_path, source, ticker)
        updated = _read_prices(price_path)
        result.update(
            {
                "status": "UPDATED" if rows_added else "NO_NEW_ROWS",
                "rows_added": rows_added,
                "local_rows_after": int(len(updated)),
                "local_last_date_after": updated["date"].max().strftime("%Y-%m-%d") if len(updated) else None,
            }
        )
        return result
    except Exception as exc:  # pragma: no cover - network-dependent path
        message = repr(exc)
        result.update(
            {
                "status": "SOURCE_UNAVAILABLE" if "source unavailable" in message else "ERROR",
                "error": message,
            }
        )
        return result


def repair_tickers(
    tickers: list[str],
    *,
    start: str = HISTORY_START.strftime("%Y-%m-%d"),
    end: str = ANALYSIS_END.strftime("%Y-%m-%d"),
    workers: int = 3,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    output: str | Path = DEFAULT_OUTPUT,
    refresh: bool = False,
) -> dict:
    unique = sorted({ticker.upper().strip() for ticker in tickers if ticker.strip()})
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                repair_one,
                ticker,
                start,
                end,
                price_dir=price_dir,
                cache_dir=cache_dir,
                refresh=refresh,
            ): ticker
            for ticker in unique
        }
        results = [future.result() for future in as_completed(futures)]
    results.sort(key=lambda item: item["ticker"])
    run = {
        "observed_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "research_only": True,
        "formal_financial_files_modified": False,
        "terminal_returns_modified": False,
        "start": start,
        "end": end,
        "results": results,
    }
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    previous: list[dict] = []
    if target.exists():
        try:
            previous = json.loads(target.read_text(encoding="utf-8")).get("runs", [])
        except (OSError, json.JSONDecodeError):
            previous = []
    target.write_text(json.dumps({"runs": [*previous, run]}, indent=2), encoding="utf-8")
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", required=True)
    parser.add_argument("--start", default=HISTORY_START.strftime("%Y-%m-%d"))
    parser.add_argument("--end", default=ANALYSIS_END.strftime("%Y-%m-%d"))
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    args = parser.parse_args()
    result = repair_tickers(
        args.tickers.split(","),
        start=args.start,
        end=args.end,
        workers=args.workers,
        output=args.output,
        cache_dir=args.cache_dir,
        refresh=args.refresh,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
