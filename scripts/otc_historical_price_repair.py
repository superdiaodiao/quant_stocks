"""Research-only OTC historical OHLCV repair with an independent Yahoo check.

The project price directory is primarily populated from Nasdaq.  A number of
former Nasdaq issuers still have a continuous OTC quote history, however, and
the public Edgar Online chart feed (used by the OTC Markets chart) exposes that
history as JSON.  This tool keeps the raw responses locally, cross-checks the
Edgar feed against Yahoo in the overlapping period, and only fills dates that
are not already present in a local price file.

It intentionally does not touch financial files, terminal-return files,
security identities, or validation artifacts.  The imported rows remain
research-only because the market-data licence and point-in-time rights are not
verified by this script.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from scripts.historicaldata_price_import import (
    _frame_sha256,
    _read_local,
    _sha256 as _file_sha256,
    _validate_overlap,
)
from src.conf import CLEANED_PRICE_DATA_DIR, OUTPUT_PATH


DEFAULT_CACHE_DIR = Path(OUTPUT_PATH) / "data_provenance/otc_historical_price_cache"
DEFAULT_OUTPUT = Path(OUTPUT_PATH) / "data_provenance/otc_historical_price_repair.json"
EDGAR_BASE = "https://charting.edgar-online.com/data/charting/historical"
YAHOO_BASE = "https://query2.finance.yahoo.com/v8/finance/chart"
USER_AGENT = "quant-stocks-research contact@example.com"
REQUIRED_PRICE_COLUMNS = ["date", "ticker", "open", "high", "low", "close", "volume"]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _request_bytes(url: str, retries: int = 3) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    # The public chart feed checks that requests originate from its chart
    # iframe.  Keep the referer explicit so cached and refreshed runs behave
    # the same way as the website's own request.
    if "charting.edgar-online.com" in url:
        headers["Referer"] = "https://charting.edgar-online.com/dynamic/chart.html"
    request = Request(url, headers=headers)
    error: Exception | None = None
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=60) as response:
                return response.read()
        except Exception as exc:  # pragma: no cover - network-dependent path
            error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"request failed: {url}: {error}")


def _cache_file(cache_dir: Path, provider: str, ticker: str) -> Path:
    return cache_dir / f"{provider}_{ticker.lower()}.json.gz"


def _load_or_fetch(cache_dir: Path, provider: str, ticker: str, url: str, refresh: bool) -> tuple[bytes, str]:
    path = _cache_file(cache_dir, provider, ticker)
    if path.exists() and not refresh:
        with gzip.open(path, "rb") as handle:
            payload = handle.read()
        return payload, str(path)
    payload = _request_bytes(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wb") as handle:
        handle.write(payload)
    os.replace(temporary, path)
    return payload, str(path)


def _parse_edgar(payload: bytes, ticker: str) -> tuple[pd.DataFrame, str | None]:
    document = json.loads(payload.decode("utf-8"))
    company_name = document.get("companyName")
    rows = document.get("marketData") or []
    records = []
    for row in rows:
        records.append(
            {
                "date": row.get("Date"),
                "ticker": ticker,
                "open": row.get("Open"),
                "high": row.get("High"),
                "low": row.get("Low"),
                "close": row.get("Close"),
                "volume": row.get("Volume"),
            }
        )
    frame = pd.DataFrame(records, columns=REQUIRED_PRICE_COLUMNS)
    if frame.empty:
        return frame, company_name
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    for column in REQUIRED_PRICE_COLUMNS[2:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).copy()
    frame = frame[frame["close"] > 0].drop_duplicates("date").sort_values("date")
    return frame.reset_index(drop=True), company_name


def _parse_yahoo(payload: bytes, ticker: str) -> tuple[pd.DataFrame, dict]:
    document = json.loads(payload.decode("utf-8"))
    chart = document.get("chart") or {}
    if chart.get("error") or not chart.get("result"):
        raise ValueError(f"Yahoo returned no result for {ticker}: {chart.get('error')}")
    result = chart["result"][0]
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(None).normalize(),
            "ticker": ticker,
            "open": quote.get("open", []),
            "high": quote.get("high", []),
            "low": quote.get("low", []),
            "close": quote.get("close", []),
            "volume": quote.get("volume", []),
        },
        columns=REQUIRED_PRICE_COLUMNS,
    )
    for column in REQUIRED_PRICE_COLUMNS[2:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "close"])
    frame = frame[frame["close"] > 0].drop_duplicates("date").sort_values("date")
    metadata = result.get("meta") or {}
    return frame.reset_index(drop=True), metadata


def _overlap(edgar: pd.DataFrame, yahoo: pd.DataFrame) -> dict:
    if edgar.empty or yahoo.empty:
        return {
            "sessions": 0,
            "meaningful_sessions": 0,
            "micro_price_sessions": 0,
            "price_ratio_median": None,
            "price_within_1pct": None,
            "volume_ratio_median": None,
            "volume_within_5pct": None,
        }
    left = edgar[["date", "close", "volume"]].rename(columns={"close": "edgar_close", "volume": "edgar_volume"})
    right = yahoo[["date", "close", "volume"]].rename(columns={"close": "yahoo_close", "volume": "yahoo_volume"})
    merged = left.merge(right, on="date", how="inner")
    merged = merged[(merged["edgar_close"] > 0) & (merged["yahoo_close"] > 0)].copy()
    if merged.empty:
        return {
            "sessions": 0,
            "meaningful_sessions": 0,
            "micro_price_sessions": 0,
            "price_ratio_median": None,
            "price_within_1pct": None,
            "volume_ratio_median": None,
            "volume_within_5pct": None,
        }
    # OTC feeds sometimes encode a sub-cent quote as 0.000001 while Yahoo
    # rounds the same quote to the displayed 0.0001/0.0003 tick.  Such rows
    # are retained as a diagnostic but are not allowed to decide whether the
    # two feeds agree on economically meaningful prices.
    meaningful = (merged["edgar_close"] >= 0.01) & (merged["yahoo_close"] >= 0.01)
    meaningful_rows = merged[meaningful]
    price_ratio = meaningful_rows["edgar_close"] / meaningful_rows["yahoo_close"]
    if price_ratio.empty:
        median_price = None
        price_ok = None
    else:
        median_price = float(price_ratio.median())
        price_ok = float(((price_ratio / median_price - 1).abs() <= 0.01).mean())
    volume = merged[(merged["edgar_volume"] > 0) & (merged["yahoo_volume"] > 0)]
    if volume.empty:
        volume_median = None
        volume_ok = None
    else:
        volume_ratio = volume["edgar_volume"] / volume["yahoo_volume"]
        volume_median = float(volume_ratio.median())
        volume_ok = float(((volume_ratio / volume_median - 1).abs() <= 0.05).mean())
    return {
        "sessions": int(len(merged)),
        "meaningful_sessions": int(len(meaningful_rows)),
        "micro_price_sessions": int((~meaningful).sum()),
        "price_ratio_median": median_price,
        "price_within_1pct": price_ok,
        "volume_ratio_median": volume_median,
        "volume_within_5pct": volume_ok,
    }


def _eligible(overlap: dict, minimum_sessions: int = 20) -> bool:
    return (
        overlap["meaningful_sessions"] >= minimum_sessions
        and overlap["price_ratio_median"] is not None
        and overlap["price_within_1pct"] >= 0.95
    )


def _local_overlap_validation(
    edgar: pd.DataFrame, local: pd.DataFrame, minimum_sessions: int = 20
) -> dict:
    """Require direct same-ticker OHLCV agreement before skipping Yahoo."""
    validation = _validate_overlap(local, edgar)
    return {
        **validation,
        "validation_scope": "same_ticker_local_ohlcv_overlap",
        "minimum_sessions": minimum_sessions,
        "passed": bool(
            validation.get("passed")
            and int(validation.get("sessions") or 0) >= minimum_sessions
        ),
    }


def _merge_missing(path: Path, incoming: pd.DataFrame, ticker: str) -> int:
    if incoming.empty:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old = pd.read_csv(path, parse_dates=["date"])
        old["date"] = pd.to_datetime(old["date"], errors="coerce").dt.normalize()
        incoming = incoming[~incoming["date"].isin(old["date"])]
        if incoming.empty:
            return 0
        combined = pd.concat([old, incoming], ignore_index=True)
    else:
        combined = incoming.copy()
    combined["ticker"] = ticker
    combined = combined[REQUIRED_PRICE_COLUMNS].drop_duplicates("date", keep="first").sort_values("date")
    temporary = path.with_suffix(path.suffix + ".tmp")
    combined.to_csv(temporary, index=False)
    os.replace(temporary, path)
    return len(incoming)


def _count_missing(path: Path, incoming: pd.DataFrame) -> int:
    if not path.exists():
        return int(len(incoming))
    old_dates = set(pd.to_datetime(pd.read_csv(path, usecols=["date"])["date"]).dt.normalize())
    return int((~incoming["date"].isin(old_dates)).sum())


def repair_one(
    ticker: str,
    start: str,
    end: str,
    cache_dir: Path,
    refresh: bool = False,
    minimum_sessions: int = 20,
    allow_edgar_only: bool = False,
    allow_local_overlap: bool = False,
    apply: bool = False,
) -> dict:
    ticker = ticker.upper().strip()
    edgar_url = EDGAR_BASE + "?" + urlencode({"symbol": ticker, "frequencyID": 0, "date": f"{start}~{end}", "includeLatestIntradayData": 1})
    start_epoch = int(pd.Timestamp(start, tz="UTC").timestamp())
    end_epoch = int((pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)).timestamp())
    yahoo_url = YAHOO_BASE + f"/{ticker}?" + urlencode({"period1": start_epoch, "period2": end_epoch, "interval": "1d", "events": "div,splits", "includeAdjustedClose": "true"})
    result = {
        "ticker": ticker,
        "research_only": True,
        "edgar_source_url": edgar_url,
        "yahoo_source_url": yahoo_url,
        "status": "",
    }
    try:
        edgar_payload, edgar_cache = _load_or_fetch(cache_dir, "edgar", ticker, edgar_url, refresh)
        edgar, company_name = _parse_edgar(edgar_payload, ticker)
        price_path = Path(CLEANED_PRICE_DATA_DIR) / f"{ticker.lower()}.csv"
        if allow_local_overlap and price_path.exists() and company_name and not edgar.empty:
            local = _read_local(price_path)
            local_validation = _local_overlap_validation(
                edgar, local, minimum_sessions
            )
            if local_validation["passed"]:
                missing = edgar.loc[~edgar["date"].isin(local["date"])].copy()
                local_sha256_before = _file_sha256(price_path)
                rows_added = (
                    _merge_missing(price_path, missing, ticker)
                    if apply else int(len(missing))
                )
                result.update({
                    "status": (
                        "UPDATED_LOCAL_OVERLAP" if apply and rows_added
                        else "DRY_RUN_ELIGIBLE_LOCAL_OVERLAP" if rows_added
                        else "NO_NEW_ROWS_LOCAL_OVERLAP"
                    ),
                    "edgar_payload_sha256": _sha256(edgar_payload),
                    "edgar_cache_path": edgar_cache,
                    "edgar_company_name": company_name,
                    "edgar_rows": int(len(edgar)),
                    "local_overlap_validation": local_validation,
                    "local_sha256_before": local_sha256_before,
                    "missing_rows_sha256": (
                        _frame_sha256(missing) if not missing.empty else None
                    ),
                    "rows_available": int(len(edgar)),
                    "rows_added": int(rows_added),
                    "price_path": str(price_path),
                    "first_date": str(edgar["date"].min().date()),
                    "last_date": str(edgar["date"].max().date()),
                    "validation_note": (
                        "Yahoo was not requested because the cached same-ticker "
                        "Edgar series passed direct local OHLCV overlap."
                    ),
                })
                if apply:
                    result["local_sha256_after"] = _file_sha256(price_path)
                else:
                    result["local_sha256_after"] = result["local_sha256_before"]
                return result
        try:
            yahoo_payload, yahoo_cache = _load_or_fetch(cache_dir, "yahoo", ticker, yahoo_url, refresh)
            yahoo, yahoo_meta = _parse_yahoo(yahoo_payload, ticker)
        except Exception as yahoo_error:
            if not allow_edgar_only or not company_name or len(edgar) < minimum_sessions:
                raise
            price_path = Path(CLEANED_PRICE_DATA_DIR) / f"{ticker.lower()}.csv"
            rows_added = (
                _merge_missing(price_path, edgar, ticker)
                if apply else _count_missing(price_path, edgar)
            )
            result.update(
                {
                    "status": (
                        "EDGAR_ONLY_UNVERIFIED" if apply and rows_added
                        else "EDGAR_ONLY_UNVERIFIED_DRY_RUN" if rows_added
                        else "EDGAR_ONLY_NO_NEW_ROWS"
                    ),
                    "edgar_payload_sha256": _sha256(edgar_payload),
                    "edgar_cache_path": edgar_cache,
                    "edgar_company_name": company_name,
                    "edgar_rows": int(len(edgar)),
                    "rows_available": int(len(edgar)),
                    "rows_added": int(rows_added),
                    "price_path": str(price_path),
                    "first_date": str(edgar["date"].min().date()),
                    "last_date": str(edgar["date"].max().date()),
                    "yahoo_error": str(yahoo_error),
                    "warning": "No independent Yahoo overlap was available; this row set is not eligible for formal import.",
                }
            )
            return result
        overlap = _overlap(edgar, yahoo)
        result.update(
            {
                "edgar_payload_sha256": _sha256(edgar_payload),
                "yahoo_payload_sha256": _sha256(yahoo_payload),
                "edgar_cache_path": edgar_cache,
                "yahoo_cache_path": yahoo_cache,
                "edgar_company_name": company_name,
                "yahoo_meta": {k: yahoo_meta.get(k) for k in ["symbol", "exchangeName", "fullExchangeName", "instrumentType", "longName", "shortName"]},
                "edgar_rows": int(len(edgar)),
                "yahoo_rows": int(len(yahoo)),
                "overlap": overlap,
            }
        )
        if not company_name or edgar.empty:
            result["status"] = "REJECT_NO_EDGAR_HISTORY"
            return result
        if not _eligible(overlap, minimum_sessions):
            result["status"] = "REJECT_CROSS_VALIDATION"
            return result
        # Edgar takes precedence on its dates.  For sub-cent dates where the
        # feeds use visibly different quote precision, prefer Yahoo's
        # displayed tick rather than importing an Edgar 0.000001 artefact.
        merged = pd.concat([edgar, yahoo], ignore_index=True).drop_duplicates("date", keep="first")
        edgar_by_date = edgar.set_index("date")
        yahoo_by_date = yahoo.set_index("date")
        common_dates = edgar_by_date.index.intersection(yahoo_by_date.index)
        for common_date in common_dates:
            e_close = float(edgar_by_date.at[common_date, "close"])
            y_close = float(yahoo_by_date.at[common_date, "close"])
            if 0 < e_close < 0.01 and 0 < y_close < 0.01:
                merged.loc[merged["date"].eq(common_date), REQUIRED_PRICE_COLUMNS[1:]] = yahoo_by_date.loc[
                    common_date, REQUIRED_PRICE_COLUMNS[1:]
                ].to_numpy()
        merged = merged.sort_values("date").reset_index(drop=True)
        price_path = Path(CLEANED_PRICE_DATA_DIR) / f"{ticker.lower()}.csv"
        rows_added = (
            _merge_missing(price_path, merged, ticker)
            if apply else _count_missing(price_path, merged)
        )
        result.update(
            {
                "status": (
                    "UPDATED" if apply and rows_added
                    else "DRY_RUN_ELIGIBLE" if rows_added
                    else "NO_NEW_ROWS"
                ),
                "rows_available": int(len(merged)),
                "rows_added": int(rows_added),
                "price_path": str(price_path),
                "first_date": str(merged["date"].min().date()),
                "last_date": str(merged["date"].max().date()),
            }
        )
        return result
    except Exception as exc:  # pragma: no cover - network-dependent path
        result.update({"status": "ERROR", "error": str(exc)})
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", required=True, help="Comma-separated ticker symbols")
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-07-17")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--allow-edgar-only",
        action="store_true",
        help="Explicitly allow research-only Edgar rows when Yahoo has no symbol; never a formal-data gate",
    )
    parser.add_argument(
        "--allow-local-overlap",
        action="store_true",
        help=(
            "Skip Yahoo only when cached Edgar data directly agrees with an "
            "existing same-ticker local file for at least 20 OHLCV sessions"
        ),
    )
    args = parser.parse_args()
    cache_dir = Path(args.cache_dir)
    records = [
        repair_one(
            ticker,
            args.start,
            args.end,
            cache_dir,
            refresh=args.refresh,
            allow_edgar_only=args.allow_edgar_only,
            allow_local_overlap=args.allow_local_overlap,
            apply=args.apply,
        )
        for ticker in sorted({item.strip().upper() for item in args.tickers.split(",") if item.strip()})
    ]
    report = {
        "schema_version": 1,
        "research_only": True,
        "formal_financial_files_modified": False,
        "terminal_returns_modified": False,
        "license_verified": False,
        "start": args.start,
        "end": args.end,
        "records": records,
        "counts": pd.Series([record["status"] for record in records]).value_counts().to_dict(),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, output)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
