"""Repair U.S. price histories from Eastmoney with reproducible evidence.

The legacy updater used AkShare's ``stock_us_hist`` endpoint but assumed every
symbol belonged to market 105.  This tool probes the three U.S. market ids used
by Eastmoney, caches the exact JSON responses, and only accepts a candidate
whose ticker identity and unadjusted OHLC overlap agree with the local file.
It is a dry run unless ``--apply`` is supplied.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from scripts.historicaldata_price_import import _atomic_write_json, _sha256
from scripts.sina_historical_price_repair import (
    _audit_targets,
    _longest_stable_tail_validation,
)
from scripts.yahoo_historical_price_repair import (
    PRICE_COLUMNS,
    PRICE_FIELDS,
    _merge_missing,
    _overlap_validation,
    _read_prices,
)
from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH


SOURCE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
MARKET_IDS = ("105", "106", "107")
DEFAULT_CACHE_DIR = (
    Path(PROJECT_PATH) / "output/data_provenance/eastmoney_historical_price_cache"
)
DEFAULT_OUTPUT = (
    Path(PROJECT_PATH)
    / "output/data_provenance/eastmoney_historical_price_repair_2026-08-09.json"
)
DEFAULT_START = "2021-01-01"
DEFAULT_END = "2026-07-17"


def _akshare_implementation() -> tuple[Path, str]:
    """Return the installed AkShare implementation and exact function source."""
    spec = importlib.util.find_spec("akshare")
    if spec is None or spec.origin is None:
        raise RuntimeError("AkShare is not installed")
    path = Path(spec.origin).parent / "stock_feature" / "stock_hist_em.py"
    source = path.read_text(encoding="utf-8")
    start = source.index("def stock_us_hist(")
    end = source.index("\ndef ", start + 1)
    return path, source[start:end]


def _source_url(ticker: str, market_id: str) -> str:
    params = {
        "secid": f"{market_id}.{ticker}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "0",
        "end": "20500000",
        "lmt": "1000000",
    }
    return f"{SOURCE_URL}?{urlencode(params)}"


def _request_bytes(url: str, retries: int = 3) -> bytes:
    error: Exception | None = None
    for attempt in range(retries):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://quote.eastmoney.com/",
                },
            )
            return urlopen(request, timeout=30).read()
        except Exception as exc:  # pragma: no cover - network-dependent
            error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"Eastmoney request failed: {error}")


def _load_or_fetch(
    cache_dir: Path, ticker: str, market_id: str, url: str, refresh: bool
) -> tuple[bytes, Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{ticker.lower()}_{market_id}.json.gz"
    if path.exists() and not refresh:
        return gzip.decompress(path.read_bytes()), path
    payload = _request_bytes(url)
    path.write_bytes(gzip.compress(payload, mtime=0))
    return payload, path


def _parse_prices(payload: bytes, ticker: str) -> tuple[pd.DataFrame, dict]:
    envelope = json.loads(payload)
    data = envelope.get("data")
    if not isinstance(data, dict):
        return pd.DataFrame(columns=PRICE_COLUMNS), {
            "response_code": envelope.get("rc"),
            "provider_code": None,
            "provider_name": None,
        }
    provider_code = str(data.get("code") or "").upper()
    if provider_code != ticker.upper():
        raise ValueError(
            f"Eastmoney identity mismatch: requested {ticker}, returned {provider_code}"
        )
    rows = []
    for raw in data.get("klines") or []:
        fields = str(raw).split(",")
        if len(fields) < 6:
            raise ValueError("Eastmoney kline row has fewer than six fields")
        rows.append({
            "date": fields[0],
            "open": fields[1],
            "close": fields[2],
            "high": fields[3],
            "low": fields[4],
            "volume": fields[5],
        })
    frame = pd.DataFrame(rows)
    if frame.empty:
        frame = pd.DataFrame(columns=PRICE_COLUMNS)
    else:
        frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
        for field in [*PRICE_FIELDS, "volume"]:
            frame[field] = pd.to_numeric(frame[field], errors="coerce")
        frame = frame.dropna(subset=["date", *PRICE_FIELDS])
        frame = frame.loc[frame["close"].gt(0)]
        frame.insert(1, "ticker", ticker.upper())
        frame = frame[PRICE_COLUMNS].drop_duplicates("date", keep="last").sort_values("date")
    return frame.reset_index(drop=True), {
        "response_code": envelope.get("rc"),
        "provider_code": provider_code,
        "provider_name": data.get("name"),
    }


def _validate(source: pd.DataFrame, local: pd.DataFrame) -> dict:
    full = _overlap_validation(source, local)
    if full["passed"]:
        return {**full, "validation_scope": "full_history"}
    tail_dates = set(local["date"].sort_values().tail(60))
    tail = _overlap_validation(
        source.loc[source["date"].isin(tail_dates)],
        local.loc[local["date"].isin(tail_dates)],
    )
    if tail["passed"]:
        return {
            **tail,
            "validation_scope": "recent_tail_60",
            "full_history_overlap": full,
        }
    stable = _longest_stable_tail_validation(source, local)
    return {
        **stable,
        "validation_scope": "longest_stable_recent_tail" if stable["passed"] else "full_history",
        "full_history_overlap": full,
        "recent_tail_overlap": tail,
    }


def repair_tickers(
    tickers: list[str],
    *,
    membership_ends: dict[str, str] | None = None,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    output: str | Path = DEFAULT_OUTPUT,
    refresh: bool = False,
    apply: bool = False,
) -> dict:
    normalized = sorted({item.strip().upper() for item in tickers if item.strip()})
    membership_ends = membership_ends or {}
    price_dir, cache_dir, output = Path(price_dir), Path(cache_dir), Path(output)
    implementation_path, implementation_source = _akshare_implementation()
    report = {
        "schema_version": 1,
        "research_only": True,
        "status": "IN_PROGRESS",
        "applied": bool(apply),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_provider": "Eastmoney via the AkShare stock_us_hist interface",
        "source_url": SOURCE_URL,
        "market_ids_probed": list(MARKET_IDS),
        "akshare_implementation_path": str(implementation_path),
        "akshare_implementation_file_sha256": _sha256(implementation_path),
        "akshare_function_source_sha256": hashlib.sha256(
            implementation_source.encode()
        ).hexdigest(),
        "formal_financial_files_modified": False,
        "terminal_returns_modified": False,
        "start": start,
        "end": end,
        "requested_tickers": normalized,
        "records": [],
    }
    _atomic_write_json(output, report)
    for ticker in normalized:
        price_path = price_dir / f"{ticker.lower()}.csv"
        record: dict[str, object] = {
            "ticker": ticker,
            "price_path": str(price_path),
            "last_membership_date": membership_ends.get(ticker),
            "market_attempts": [],
        }
        try:
            if not price_path.exists():
                record["status"] = "REJECT_NO_LOCAL_PRICE_FILE"
            else:
                local = _read_prices(price_path)
                accepted = []
                for market_id in MARKET_IDS:
                    url = _source_url(ticker, market_id)
                    attempt: dict[str, object] = {"market_id": market_id, "source_url": url}
                    try:
                        payload, cache_path = _load_or_fetch(
                            cache_dir, ticker, market_id, url, refresh
                        )
                        source, identity = _parse_prices(payload, ticker)
                        source = source.loc[
                            source["date"].between(pd.Timestamp(start), pd.Timestamp(end))
                        ]
                        attempt.update({
                            **identity,
                            "raw_cache_path": str(cache_path),
                            "raw_payload_size_bytes": len(payload),
                            "raw_payload_sha256": hashlib.sha256(payload).hexdigest(),
                            "source_rows": int(len(source)),
                        })
                        if source.empty:
                            attempt["status"] = "NO_DATA"
                        else:
                            validation = _validate(source, local)
                            attempt["cross_validation"] = validation
                            attempt["status"] = (
                                "IDENTITY_AND_OVERLAP_CONFIRMED"
                                if validation["passed"] else "REJECT_CROSS_VALIDATION"
                            )
                            if validation["passed"]:
                                accepted.append((market_id, source, attempt))
                    except Exception as exc:  # pragma: no cover - network-dependent
                        attempt.update({"status": "ERROR", "error": repr(exc)})
                    record["market_attempts"].append(attempt)
                if len(accepted) != 1:
                    record["status"] = (
                        "REJECT_AMBIGUOUS_MARKET" if len(accepted) > 1 else "REJECT_NO_VALIDATED_MARKET"
                    )
                else:
                    market_id, source, selected = accepted[0]
                    validation = selected["cross_validation"]
                    normalized_source = source.copy()
                    scale = float(validation["close_median_ratio"])
                    for field in PRICE_FIELDS:
                        normalized_source[field] *= scale
                    if validation.get("volume_median_ratio") is not None:
                        normalized_source["volume"] *= float(validation["volume_median_ratio"])
                    cutoff = min(
                        pd.Timestamp(end),
                        pd.Timestamp(membership_ends[ticker])
                        if ticker in membership_ends else pd.Timestamp(end),
                    )
                    missing = normalized_source.loc[
                        normalized_source["date"].le(cutoff)
                        & ~normalized_source["date"].isin(local["date"])
                    ].copy()
                    record.update({
                        "selected_market_id": market_id,
                        "selected_raw_payload_sha256": selected["raw_payload_sha256"],
                        "local_sha256_before": _sha256(price_path),
                        "local_rows_before": int(len(local)),
                        "append_cutoff": cutoff.strftime("%Y-%m-%d"),
                        "rows_missing": int(len(missing)),
                        "first_missing_date": missing["date"].min().strftime("%Y-%m-%d") if not missing.empty else None,
                        "last_missing_date": missing["date"].max().strftime("%Y-%m-%d") if not missing.empty else None,
                    })
                    if apply and not missing.empty:
                        record["rows_added"] = _merge_missing(price_path, missing, ticker)
                        record["status"] = "UPDATED"
                        record["local_sha256_after"] = _sha256(price_path)
                    else:
                        record["status"] = "DRY_RUN_ELIGIBLE" if not missing.empty else "NO_NEW_ROWS"
                        record["local_sha256_after"] = record["local_sha256_before"]
        except Exception as exc:
            record.update({"status": "ERROR", "error": repr(exc)})
        report["records"].append(record)
        report["checkpointed_records"] = len(report["records"])
        report["last_checkpoint_ticker"] = ticker
        report["last_checkpoint_at_utc"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(output, report)
    report["status"] = "COMPLETE"
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", help="Optional comma-separated ticker subset")
    parser.add_argument("--historical-audit", help="Use current missing-price rows and PIT cutoffs")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.tickers and not args.historical_audit:
        parser.error("one of --tickers or --historical-audit is required")
    membership_ends = _audit_targets(args.historical_audit) if args.historical_audit else {}
    tickers = args.tickers.split(",") if args.tickers else list(membership_ends)
    repair_tickers(
        tickers,
        membership_ends=membership_ends,
        start=args.start,
        end=args.end,
        price_dir=args.price_dir,
        cache_dir=args.cache_dir,
        output=args.output,
        refresh=args.refresh,
        apply=args.apply,
    )


if __name__ == "__main__":
    main()
