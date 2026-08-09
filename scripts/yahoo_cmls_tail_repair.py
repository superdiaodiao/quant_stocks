"""Research-only CMLS -> CMLSQ OTC tail repair with overlap validation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH

END = pd.Timestamp("2026-07-17")
PRICE_FIELDS = ["open", "high", "low", "close"]


def _url() -> str:
    params = {
        "period1": int(pd.Timestamp("2025-01-01", tz="UTC").timestamp()),
        "period2": int((END + pd.Timedelta(days=1)).tz_localize("UTC").timestamp()),
        "interval": "1d",
        "events": "div,splits",
        "includeAdjustedClose": "true",
    }
    return "https://query2.finance.yahoo.com/v8/finance/chart/CMLSQ?" + urlencode(params)


def _fetch() -> bytes:
    return urlopen(Request(_url(), headers={"User-Agent": "quant-stocks-research"}), timeout=60).read()


def _parse(payload: bytes) -> pd.DataFrame:
    result = (json.loads(payload)["chart"].get("result") or [None])[0]
    if not result:
        raise ValueError("Yahoo returned no CMLSQ history")
    quote = (result.get("indicators") or {}).get("quote", [{}])[0]
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(result.get("timestamp", []), unit="s").normalize(),
            **{field: quote.get(field, []) for field in [*PRICE_FIELDS, "volume"]},
        }
    )
    return frame.dropna(subset=["date", "close"]).drop_duplicates("date").sort_values("date")


def _validate(local: pd.DataFrame, source: pd.DataFrame) -> dict:
    overlap = local.merge(source, on="date", suffixes=("_local", "_source"))
    if len(overlap) < 20:
        raise ValueError(f"insufficient CMLS/CMLSQ overlap: {len(overlap)}")
    fields = {}
    for field in PRICE_FIELDS:
        ratio = overlap[f"{field}_local"].astype(float) / overlap[f"{field}_source"].astype(float)
        fields[field] = {
            "median_ratio": float(ratio.median()),
            "within_1pct": float(((ratio - ratio.median()).abs() / ratio.median() <= 0.01).mean()),
        }
    if min(item["within_1pct"] for item in fields.values()) < 0.95:
        raise ValueError(f"CMLS/CMLSQ OHLC validation failed: {fields}")
    return {"sessions": int(len(overlap)), "fields": fields, "passed": True}


def repair(
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    output: str | Path = Path(PROJECT_PATH) / "output/data_provenance/yahoo_cmls_tail_repair.json",
) -> dict:
    target = Path(price_dir) / "cmls.csv"
    existing = pd.read_csv(target, parse_dates=["date"])
    payload = _fetch()
    source = _parse(payload)
    validation = _validate(existing, source)
    missing = source.loc[(source["date"] > existing["date"].max()) & (source["date"] <= END)].copy()
    missing.insert(1, "ticker", "CMLS")
    merged = pd.concat([existing, missing], ignore_index=True).sort_values("date").drop_duplicates("date", keep="first")
    temporary = target.with_suffix(".csv.tmp")
    merged.to_csv(temporary, index=False)
    os.replace(temporary, target)
    result = {
        "research_only": True,
        "historical_ticker": "CMLS",
        "provider_ticker": "CMLSQ",
        "same_issuer_cik": 1058623,
        "effective_transition_evidence": "SEC 25-NSE filed 2025-07-21; SEC submissions current tickers include CMLS and CMLSQ",
        "sec_25_nse_url": "https://www.sec.gov/Archives/edgar/data/1058623/000135445725000698/xslF25X02/primary_doc.xml",
        "yahoo_source_url": _url(),
        "yahoo_payload_sha256": hashlib.sha256(payload).hexdigest(),
        "rows_added": int(len(missing)),
        "first_added_date": missing["date"].min().strftime("%Y-%m-%d") if len(missing) else None,
        "last_added_date": missing["date"].max().strftime("%Y-%m-%d") if len(missing) else None,
        "overlap_validation": validation,
        "formal_financial_files_modified": False,
        "terminal_returns_modified": False,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(repair(), indent=2))
