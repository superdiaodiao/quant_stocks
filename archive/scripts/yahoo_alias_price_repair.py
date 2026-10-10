"""Repair sourced ticker-rename price heads after cross-source validation.

This tool is intentionally narrow: it only handles same-issuer ticker
renames with SEC evidence, validates Yahoo chart data against both the local
Nasdaq history and a pinned Stooq mirror, and writes a provenance record.  It
never replaces an existing local row and does not touch terminal returns or
formal financial files.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from io import StringIO
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH


ANALYSIS_END = pd.Timestamp("2026-07-17")
HISTORY_START = pd.Timestamp("2021-01-01")
STOOQ_REPOSITORY = "ARKMD/stooq"
STOOQ_COMMIT = "6ae7c9b04dc8b98612d1ee9594baa64362b4ade1"
STOOQ_PATHS = {
    "TBCH": "d_us_txt/data/daily/us/nasdaq stocks/2/tbch.us.txt",
    "STEX": "d_us_txt/data/daily/us/nasdaq stocks/1/bsgm.us.txt",
}
ALIASES = (
    {
        "provider_ticker": "TBCH",
        "historical_ticker": "HEAR",
        "last_historical_date": "2025-01-06",
        "current_ticker_first_date": "2025-01-07",
        "cik": 1493761,
        "identity_source_url": (
            "https://www.sec.gov/Archives/edgar/data/1493761/"
            "000089457925000004/turtlebeach8k01032025.htm"
        ),
    },
    {
        "provider_ticker": "STEX",
        "historical_ticker": "BSGM",
        "last_historical_date": "2025-09-11",
        "current_ticker_first_date": "2025-09-12",
        "cik": 1530766,
        "identity_source_url": (
            "https://www.sec.gov/Archives/edgar/data/1530766/"
            "000164117225027127/form8-k.htm"
        ),
    },
)
HEADERS = {
    "User-Agent": "quant-stocks-research-alias-repair",
    "Accept": "application/json,text/plain,*/*",
}
PRICE_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _yahoo_url(ticker: str) -> str:
    params = urlencode({
        "period1": int(pd.Timestamp("2012-01-01", tz="UTC").timestamp()),
        "period2": int((ANALYSIS_END + pd.Timedelta(days=1)).tz_localize("UTC").timestamp()),
        "interval": "1d",
        "events": "div,splits",
        "includeAdjustedClose": "true",
    })
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?{params}"


def _stooq_url(path: str) -> str:
    return (
        f"https://raw.githubusercontent.com/{STOOQ_REPOSITORY}/"
        f"{STOOQ_COMMIT}/{quote(path)}"
    )


def _fetch(url: str) -> bytes:
    with urlopen(Request(url, headers=HEADERS), timeout=60) as response:
        return response.read()


def _parse_yahoo(payload: bytes) -> pd.DataFrame:
    chart = json.loads(payload.decode("utf-8"))["chart"]
    if chart.get("error") or not chart.get("result"):
        raise ValueError(f"Yahoo chart returned no result: {chart.get('error')}")
    result = chart["result"][0]
    quote_frame = (result.get("indicators") or {}).get("quote") or []
    timestamps = result.get("timestamp") or []
    if not quote_frame or not timestamps:
        raise ValueError("Yahoo chart has no timestamps or quote rows")
    values = quote_frame[0]
    frame = pd.DataFrame({
        "date": pd.to_datetime(timestamps, unit="s", utc=True)
        .tz_convert(None).normalize(),
        **{column: values.get(column) for column in PRICE_COLUMNS[1:]},
    })
    frame = frame.dropna(subset=["date", "close"])
    frame = frame.drop_duplicates("date", keep="last").sort_values("date")
    if frame.empty or frame["close"].le(0).any():
        raise ValueError("Yahoo chart has no positive close series")
    return frame[PRICE_COLUMNS].reset_index(drop=True)


def _parse_stooq(payload: bytes) -> pd.DataFrame:
    frame = pd.read_csv(StringIO(payload.decode("utf-8-sig", errors="replace")))
    required = {"<DATE>", "<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>", "<VOL>"}
    if not required.issubset(frame.columns):
        raise ValueError("pinned Stooq source has invalid columns")
    frame = frame.rename(columns={
        "<DATE>": "date", "<OPEN>": "open", "<HIGH>": "high",
        "<LOW>": "low", "<CLOSE>": "close", "<VOL>": "volume",
    })
    frame["date"] = pd.to_datetime(frame["date"].astype(str), format="%Y%m%d")
    return frame[PRICE_COLUMNS].dropna(subset=["date", "close"]).drop_duplicates(
        "date", keep="last"
    ).sort_values("date").reset_index(drop=True)


def _overlap_validation(source: pd.DataFrame, reference: pd.DataFrame) -> dict:
    overlap = source.merge(reference, on="date", suffixes=("_source", "_reference"))
    if len(overlap) == 0:
        raise ValueError("no overlapping sessions for price validation")
    result = {"overlap_sessions": int(len(overlap))}
    for column in ("open", "high", "low", "close"):
        left = overlap[f"{column}_source"].astype(float)
        right = overlap[f"{column}_reference"].astype(float)
        relative = (left - right).abs() / right.abs().clip(lower=1e-9)
        result[f"{column}_within_1pct"] = float(relative.le(0.01).mean())
        result[f"{column}_median_ratio"] = float((left / right).median())
    left = overlap["volume_source"].astype(float)
    right = overlap["volume_reference"].astype(float)
    relative = (left - right).abs() / right.abs().clip(lower=1e-9)
    result["volume_within_5pct"] = float(relative.le(0.05).mean())
    result["volume_median_ratio"] = float((left / right).median())
    result["passed"] = bool(
        len(overlap) >= 20
        and result["close_within_1pct"] >= 0.95
        and result["volume_within_5pct"] >= 0.90
    )
    return result


def _read_prices(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=PRICE_COLUMNS + ["ticker"])
    frame = pd.read_csv(path, parse_dates=["date"])
    return frame


def _atomic_write(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(tmp, index=False)
    os.replace(tmp, path)


def repair_aliases(
    *,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    cache_dir: str | Path = Path(PROJECT_PATH) / "output/data_provenance/yahoo_alias_price_cache",
    provenance_path: str | Path = Path(PROJECT_PATH) / "output/data_provenance/yahoo_alias_price_repair.json",
) -> dict:
    price_dir = Path(price_dir)
    cache_dir = Path(cache_dir)
    results = []
    for alias in ALIASES:
        provider = alias["provider_ticker"]
        historical = alias["historical_ticker"]
        provider_path = price_dir / f"{provider.lower()}.csv"
        historical_path = price_dir / f"{historical.lower()}.csv"
        source_url = _yahoo_url(provider)
        payload = _fetch(source_url)
        source = _parse_yahoo(payload)
        source = source.loc[source["date"].le(ANALYSIS_END)].copy()
        local_provider = _read_prices(provider_path)
        local_provider["date"] = pd.to_datetime(local_provider["date"], errors="coerce")
        local_provider = local_provider.dropna(subset=["date"])
        local_reference = local_provider[PRICE_COLUMNS]
        local_validation = _overlap_validation(source, local_reference)
        stooq_payload = _fetch(_stooq_url(STOOQ_PATHS[provider]))
        stooq = _parse_stooq(stooq_payload)
        cross_validation = _overlap_validation(source, stooq)
        if not local_validation["passed"] or not cross_validation["passed"]:
            results.append({
                **alias,
                "status": "validation_failed",
                "source_url": source_url,
                "source_payload_sha256": _sha256(payload),
                "local_validation": local_validation,
                "cross_validation": cross_validation,
            })
            continue
        cutoff = pd.Timestamp(alias["last_historical_date"])
        existing_historical = _read_prices(historical_path)
        existing_historical["date"] = pd.to_datetime(
            existing_historical["date"], errors="coerce"
        )
        provider_old = local_provider.loc[local_provider["date"].le(cutoff)].copy()
        source_old = source.loc[
            source["date"].ge(HISTORY_START) & source["date"].le(cutoff)
        ].copy()
        existing_historical["_rank"] = 0
        provider_old["_rank"] = 1
        source_old["_rank"] = 2
        merged = pd.concat(
            [existing_historical, provider_old, source_old], ignore_index=True
        )
        merged = (
            merged.sort_values(["date", "_rank"])
            .drop_duplicates("date", keep="first")
            .drop(columns=["_rank"], errors="ignore")
            .sort_values("date")
        )
        merged["ticker"] = historical
        current = local_provider.loc[
            local_provider["date"].ge(pd.Timestamp(alias["current_ticker_first_date"]))
        ].copy()
        current["ticker"] = provider
        _atomic_write(historical_path, merged)
        _atomic_write(provider_path, current)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{provider}.json.gz"
        cache_path.write_bytes(gzip.compress(payload, mtime=0))
        results.append({
            **alias,
            "status": "updated",
            "source_url": source_url,
            "source_payload_sha256": _sha256(payload),
            "stooq_source_url": _stooq_url(STOOQ_PATHS[provider]),
            "stooq_payload_sha256": _sha256(stooq_payload),
            "local_validation": local_validation,
            "cross_validation": cross_validation,
            "historical_rows_before": int(len(existing_historical)),
            "historical_rows_after": int(len(merged)),
            "historical_first_date": merged["date"].min().strftime("%Y-%m-%d"),
            "historical_last_date": merged["date"].max().strftime("%Y-%m-%d"),
            "current_rows_after": int(len(current)),
            "cache_path": str(cache_path),
        })
    report = {
        "format_version": 1,
        "research_only": True,
        "formal_financial_files_modified": False,
        "terminal_returns_modified": False,
        "identity_source_required": True,
        "source_rights_review": "public_api_and_pinned_mirror; verify before release",
        "analysis_end": ANALYSIS_END.strftime("%Y-%m-%d"),
        "results": results,
    }
    provenance_path = Path(provenance_path)
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--cache-dir")
    parser.add_argument("--provenance")
    args = parser.parse_args()
    kwargs = {"price_dir": args.price_dir}
    if args.cache_dir:
        kwargs["cache_dir"] = args.cache_dir
    if args.provenance:
        kwargs["provenance_path"] = args.provenance
    print(json.dumps(repair_aliases(**kwargs), indent=2))


if __name__ == "__main__":
    main()
