"""Repair daily OHLCV from a versioned public Kaggle minute dataset.

The Parquet files are queried remotely with DuckDB predicate pushdown. Full
monthly files are not downloaded. Only regular-session daily aggregates are
cached, SHA-bound, cross-validated, and appended to an existing research file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from functools import lru_cache

import pandas as pd
import requests

from src.conf import CLEANED_PRICE_DATA_DIR


DATASET_REF = "gpch2159/us-ohlcv-minute-data-2025"
DATASET_VERSION_ID = "18217722"
KAGGLE_DOWNLOAD = "https://www.kaggle.com/api/v1/datasets/download"
HF_REPO = "mito0o852/OHLCV-1m"
HF_COMMIT = "776328445b7ac6e7815ef3a483e9c8ded1eb6d56"
PRICE_COLUMNS = ["date", "ticker", "open", "high", "low", "close", "volume"]


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def validate_overlap(source: pd.DataFrame, local: pd.DataFrame) -> dict:
    source = source.copy()
    local = local.copy()
    source["date"] = pd.to_datetime(source["date"])
    local["date"] = pd.to_datetime(local["date"])
    merged = local.merge(source, on="date", suffixes=("_local", "_source"))
    fields = {}
    for field in ["open", "high", "low", "close"]:
        denominator = merged[f"{field}_source"].abs().clip(lower=1e-12)
        fields[field] = float(
            ((merged[f"{field}_local"] - merged[f"{field}_source"]).abs() / denominator <= 0.01).mean()
        ) if len(merged) else 0.0
    close_ratio = (
        float((merged["close_local"] / merged["close_source"]).median())
        if len(merged) else None
    )
    passed = (
        len(merged) >= 5
        and fields["close"] == 1.0
        and sum(value >= 0.8 for value in fields.values()) >= 3
        and min(fields.values()) >= 0.6
        and close_ratio is not None
        and abs(close_ratio - 1.0) <= 0.01
    )
    return {
        "passed": passed,
        "sessions": int(len(merged)),
        "field_within_1pct": fields,
        "close_median_ratio": close_ratio,
    }


def _signed_url(filename: str) -> str:
    response = requests.get(
        f"{KAGGLE_DOWNLOAD}/{DATASET_REF}/{filename}",
        allow_redirects=False,
        timeout=30,
        headers={"User-Agent": "quant_stocks-research"},
    )
    response.raise_for_status()
    location = response.headers.get("location")
    if not location or f"/datasets/11282714/{DATASET_VERSION_ID}/" not in location:
        raise RuntimeError("Kaggle redirect did not bind the expected dataset version")
    return location


@lru_cache(maxsize=1)
def _hf_files() -> dict[str, dict]:
    response = requests.get(
        f"https://huggingface.co/api/datasets/{HF_REPO}/tree/{HF_COMMIT}",
        params={"recursive": "true", "limit": 1000},
        timeout=30,
        headers={"User-Agent": "quant_stocks-research"},
    )
    response.raise_for_status()
    return {row["path"]: row for row in response.json() if row.get("type") == "file"}


def _source_url(filename: str, source: str) -> tuple[str, dict]:
    if source == "kaggle":
        return _signed_url(filename), {
            "source": "kaggle",
            "dataset_ref": DATASET_REF,
            "dataset_version_id": DATASET_VERSION_ID,
        }
    path = f"data/{filename}"
    metadata = _hf_files().get(path)
    if metadata is None:
        raise FileNotFoundError(f"Hugging Face source member not found: {path}")
    return (
        f"https://huggingface.co/datasets/{HF_REPO}/resolve/{HF_COMMIT}/{path}",
        {
            "source": "huggingface",
            "dataset_ref": HF_REPO,
            "dataset_commit": HF_COMMIT,
            "file_size": metadata.get("size"),
            "lfs_sha256": (metadata.get("lfs") or {}).get("oid"),
        },
    )


def query_month(
    duckdb: Path, ticker: str, month: str, source: str = "kaggle"
) -> tuple[pd.DataFrame, dict]:
    filename = f"ohlcv_{month}.parquet"
    raw_source_url, source_metadata = _source_url(filename, source)
    source_url = raw_source_url.replace("'", "''")
    local_ts = "timestamp AT TIME ZONE 'America/New_York'"
    sql = f"""
SELECT CAST({local_ts} AS DATE) AS date,
       first(open ORDER BY timestamp) AS open,
       max(high) AS high,
       min(low) AS low,
       last(close ORDER BY timestamp) AS close,
       sum(volume) AS volume
FROM read_parquet('{source_url}')
WHERE upper(ticker) = '{ticker.upper()}'
  AND CAST({local_ts} AS TIME) >= TIME '09:30:00'
  AND CAST({local_ts} AS TIME) <= TIME '16:00:00'
GROUP BY 1 ORDER BY 1
""".strip()
    result = subprocess.run(
        [str(duckdb), "-json", "-c", sql],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    rows = json.loads(result.stdout or "[]")
    frame = pd.DataFrame(rows)
    if frame.empty:
        frame = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    else:
        frame["date"] = pd.to_datetime(frame["date"])
        for field in ["open", "high", "low", "close", "volume"]:
            frame[field] = pd.to_numeric(frame[field], errors="raise")
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return frame, {
        **source_metadata,
        "month": month,
        "filename": filename,
        "query_sha256": hashlib.sha256(sql.replace(source_url, "<SIGNED_URL>").encode()).hexdigest(),
        "daily_rows": len(frame),
        "daily_payload_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def repair(
    *,
    duckdb: str | Path,
    provider_ticker: str,
    target_ticker: str,
    months: list[str],
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    output: str | Path,
    apply: bool = False,
    source: str = "kaggle",
    end: str | None = None,
) -> dict:
    duckdb = Path(duckdb)
    target = Path(price_dir) / f"{target_ticker.lower()}.csv"
    local = pd.read_csv(target, parse_dates=["date"])
    frames, sources = [], []
    for month in months:
        frame, evidence = query_month(duckdb, provider_ticker, month, source)
        frames.append(frame)
        sources.append(evidence)
    source_frame = (
        pd.concat(frames, ignore_index=True)
        .sort_values("date")
        .drop_duplicates("date")
    )
    source_frame["ticker"] = target_ticker.upper()
    source_frame = source_frame[PRICE_COLUMNS]
    if end is not None:
        source_frame = source_frame.loc[
            source_frame["date"] <= pd.Timestamp(end)
        ].copy()
    validation = validate_overlap(source_frame, local)
    missing = source_frame.loc[
        ~source_frame["date"].isin(local["date"])
    ].copy()
    report = {
        "status": "DRY_RUN_ELIGIBLE" if validation["passed"] else "REJECT_CROSS_VALIDATION",
        "research_only": True,
        "dataset_ref": DATASET_REF if source == "kaggle" else HF_REPO,
        "dataset_version_id": DATASET_VERSION_ID if source == "kaggle" else None,
        "dataset_commit": HF_COMMIT if source == "huggingface" else None,
        "source": source,
        "provider_ticker": provider_ticker.upper(),
        "target_ticker": target_ticker.upper(),
        "identity_end": end,
        "duckdb_path": str(duckdb),
        "duckdb_sha256": _sha256(duckdb),
        "price_file_before_sha256": _sha256(target),
        "cross_validation": validation,
        "rows_missing": len(missing),
        "first_missing_date": missing["date"].min().strftime("%Y-%m-%d") if len(missing) else None,
        "last_missing_date": missing["date"].max().strftime("%Y-%m-%d") if len(missing) else None,
        "monthly_sources": sources,
        "formal_financial_files_modified": False,
        "terminal_returns_modified": False,
    }
    if apply and validation["passed"]:
        merged = pd.concat([local, missing], ignore_index=True).sort_values("date").drop_duplicates("date")
        merged["ticker"] = target_ticker.upper()
        tmp = target.with_suffix(".csv.tmp")
        merged[PRICE_COLUMNS].to_csv(tmp, index=False)
        os.replace(tmp, target)
        report.update({
            "status": "UPDATED",
            "rows_added": len(missing),
            "price_file_after_sha256": _sha256(target),
        })
    _atomic_json(Path(output), report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb", required=True)
    parser.add_argument("--provider-ticker", required=True)
    parser.add_argument("--target-ticker", required=True)
    parser.add_argument("--months", required=True, help="Comma-separated YYYY-MM values")
    parser.add_argument("--source", choices=["kaggle", "huggingface"], default="kaggle")
    parser.add_argument("--end", help="Inclusive identity cutoff; required when a ticker is reused")
    parser.add_argument("--price-dir", default=CLEANED_PRICE_DATA_DIR)
    parser.add_argument("--output", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = repair(
        duckdb=args.duckdb,
        provider_ticker=args.provider_ticker,
        target_ticker=args.target_ticker,
        months=[month.strip() for month in args.months.split(",") if month.strip()],
        price_dir=args.price_dir,
        output=args.output,
        apply=args.apply,
        source=args.source,
        end=args.end,
    )
    print(json.dumps({key: report[key] for key in ["status", "rows_missing"]}, indent=2))


if __name__ == "__main__":
    main()
