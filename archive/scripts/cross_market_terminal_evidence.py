#!/usr/bin/env python3
"""Cache and replay official cross-market merger valuation evidence."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from src.conf import PROJECT_PATH


TASE_URL = "https://api.tase.co.il/api/security/historyeod"
TASE_REQUEST = {
    "pType": 4,
    "TotalRec": 241,
    "pageNum": 4,
    "oId": "00445015",
    "lang": "1",
}
ECB_ILS_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/D.ILS.EUR.SP00.A"
    "?startPeriod=2026-02-24&endPeriod=2026-02-24&format=csvdata"
)
ECB_USD_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
    "?startPeriod=2026-02-24&endPeriod=2026-02-24&format=csvdata"
)
DEFAULT_CACHE_DIR = (
    Path(PROJECT_PATH) / "output/data_provenance/cross_market_terminal_cache"
)
DEFAULT_OUTPUT = (
    Path(PROJECT_PATH)
    / "output/data_provenance/cross_market_terminal_evidence_mgic_2026-08-09.json"
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_cache(
    path: Path,
    *,
    source_url: str,
    request_body: dict | None,
    payload: bytes,
) -> dict:
    envelope = {
        "format_version": 1,
        "source_url": source_url,
        "request_body": request_body,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "payload_sha256": _sha256(payload),
        "payload_hex": payload.hex(),
    }
    encoded = gzip.compress(
        json.dumps(envelope, sort_keys=True).encode("utf-8"), mtime=0
    )
    _atomic_write(path, encoded)
    return envelope


def _read_cache(
    path: Path, *, source_url: str, request_body: dict | None
) -> tuple[dict, bytes]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    if envelope["source_url"] != source_url:
        raise ValueError(f"{path}: source URL mismatch")
    if envelope.get("request_body") != request_body:
        raise ValueError(f"{path}: request body mismatch")
    payload = bytes.fromhex(envelope["payload_hex"])
    if _sha256(payload) != envelope["payload_sha256"]:
        raise ValueError(f"{path}: payload SHA mismatch")
    return envelope, payload


def _fetch(url: str, request_body: dict | None) -> bytes:
    data = None
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv"}
    if request_body is not None:
        data = json.dumps(request_body, separators=(",", ":")).encode("utf-8")
        headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json;charset=UTF-8",
                "Referer": "https://market.tase.co.il/",
            }
        )
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _load_source(
    path: Path,
    *,
    source_url: str,
    request_body: dict | None,
    refresh: bool,
    refresh_missing_only: bool,
    fetcher=_fetch,
) -> tuple[dict, bytes]:
    should_fetch = refresh or (refresh_missing_only and not path.exists())
    if should_fetch:
        envelope = _write_cache(
            path,
            source_url=source_url,
            request_body=request_body,
            payload=fetcher(source_url, request_body),
        )
        return envelope, bytes.fromhex(envelope["payload_hex"])
    return _read_cache(
        path, source_url=source_url, request_body=request_body
    )


def _tase_close(payload: bytes, trade_date: str) -> float:
    document = json.loads(payload)
    matches = [
        row for row in document["Items"] if row["TradeDate"] == trade_date
    ]
    if len(matches) != 1:
        raise ValueError(f"TASE trade date {trade_date} matched {len(matches)} rows")
    # TASE reports CloseRate in 0.01 ILS.  Do not use the adjusted close:
    # subsequent corporate actions must not change merger-date consideration.
    return float(matches[0]["CloseRate"]) / 100.0


def _ecb_rate(payload: bytes, trade_date: str, currency: str) -> float:
    rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))
    matches = [
        row
        for row in rows
        if row["TIME_PERIOD"] == trade_date and row["CURRENCY"] == currency
    ]
    if len(matches) != 1:
        raise ValueError(
            f"ECB {currency} trade date {trade_date} matched {len(matches)} rows"
        )
    return float(matches[0]["OBS_VALUE"])


def build_mgic_evidence(
    *,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    refresh_missing_only: bool = False,
    fetcher=_fetch,
) -> dict:
    cache_dir = Path(cache_dir)
    specifications = [
        ("tase_mtrx_page4.json.gz", TASE_URL, TASE_REQUEST),
        ("ecb_ils_eur_2026-02-24.csv.gz", ECB_ILS_URL, None),
        ("ecb_usd_eur_2026-02-24.csv.gz", ECB_USD_URL, None),
    ]
    loaded = {}
    for name, url, body in specifications:
        loaded[name] = _load_source(
            cache_dir / name,
            source_url=url,
            request_body=body,
            refresh=refresh,
            refresh_missing_only=refresh_missing_only,
            fetcher=fetcher,
        )

    tase_envelope, tase_payload = loaded[specifications[0][0]]
    ils_envelope, ils_payload = loaded[specifications[1][0]]
    usd_envelope, usd_payload = loaded[specifications[2][0]]
    trade_date = "2026-02-24"
    tase_close_ils = _tase_close(tase_payload, "24/02/2026")
    ils_per_eur = _ecb_rate(ils_payload, trade_date, "ILS")
    usd_per_eur = _ecb_rate(usd_payload, trade_date, "USD")
    successor_close_usd = tase_close_ils / ils_per_eur * usd_per_eur
    exchange_ratio = 0.5878202
    last_close = 17.38
    consideration = exchange_ratio * successor_close_usd
    return {
        "format_version": 1,
        "research_only": True,
        "ticker": "MGIC",
        "last_price_date": "2026-02-23",
        "last_close_usd": last_close,
        "successor_ticker": "MTRX",
        "successor_exchange": "TASE",
        "successor_price_date": trade_date,
        "successor_close_ils": tase_close_ils,
        "ils_per_eur": ils_per_eur,
        "usd_per_eur": usd_per_eur,
        "successor_close_usd": successor_close_usd,
        "exchange_ratio": exchange_ratio,
        "total_consideration_usd": consideration,
        "terminal_return": consideration / last_close - 1.0,
        "sources": [
            {
                "role": "successor_close",
                "source_url": TASE_URL,
                "request_body": TASE_REQUEST,
                "cache_path": str(cache_dir / specifications[0][0]),
                "payload_sha256": tase_envelope["payload_sha256"],
            },
            {
                "role": "ils_per_eur",
                "source_url": ECB_ILS_URL,
                "cache_path": str(cache_dir / specifications[1][0]),
                "payload_sha256": ils_envelope["payload_sha256"],
            },
            {
                "role": "usd_per_eur",
                "source_url": ECB_USD_URL,
                "cache_path": str(cache_dir / specifications[2][0]),
                "payload_sha256": usd_envelope["payload_sha256"],
            },
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--refresh-missing-only", action="store_true")
    args = parser.parse_args()
    report = build_mgic_evidence(
        cache_dir=args.cache_dir,
        refresh=args.refresh,
        refresh_missing_only=args.refresh_missing_only,
    )
    _atomic_write(
        args.output,
        (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    print(json.dumps({"ticker": "MGIC", "terminal_return": report["terminal_return"]}))


if __name__ == "__main__":
    main()
