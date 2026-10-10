"""Append a same-issuer ticker tail after an SEC-documented symbol change."""

from __future__ import annotations

import argparse
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
STOOQ_REPOSITORY = "ARKMD/stooq"
STOOQ_COMMIT = "6ae7c9b04dc8b98612d1ee9594baa64362b4ade1"
PRICE_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
SEC_URL = "https://www.sec.gov/Archives/edgar/data/912603/000115752323001375/a53546319.htm"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/RDUS"
STOOQ_PATH = "d_us_txt/data/daily/us/nasdaq stocks/2/rdus.us.txt"


def _fetch(url: str) -> bytes:
    return urlopen(Request(url, headers={"User-Agent": "quant-stocks-research"}), timeout=60).read()


def _yahoo_url() -> str:
    return YAHOO_URL + "?" + urlencode({
        "period1": int(pd.Timestamp("2023-09-01", tz="UTC").timestamp()),
        "period2": int((ANALYSIS_END + pd.Timedelta(days=1)).tz_localize("UTC").timestamp()),
        "interval": "1d", "events": "div,splits", "includeAdjustedClose": "true",
    })


def _parse_yahoo(payload: bytes) -> pd.DataFrame:
    result = (json.loads(payload)["chart"].get("result") or [None])[0]
    if not result:
        raise ValueError("Yahoo returned no RDUS history")
    quote_data = (result.get("indicators") or {}).get("quote", [{}])[0]
    frame = pd.DataFrame({
        "date": pd.to_datetime(result.get("timestamp", []), unit="s").normalize(),
        **{c: quote_data.get(c, []) for c in PRICE_COLUMNS[1:]},
    }).dropna(subset=["date", "close"])
    return frame[PRICE_COLUMNS].drop_duplicates("date").sort_values("date").reset_index(drop=True)


def _parse_stooq(payload: bytes) -> pd.DataFrame:
    frame = pd.read_csv(StringIO(payload.decode("utf-8-sig", errors="replace")))
    frame = frame.rename(columns={"<DATE>": "date", "<OPEN>": "open", "<HIGH>": "high", "<LOW>": "low", "<CLOSE>": "close", "<VOL>": "volume"})
    frame["date"] = pd.to_datetime(frame["date"].astype(str), format="%Y%m%d")
    return frame[PRICE_COLUMNS].dropna(subset=["date", "close"]).drop_duplicates("date").sort_values("date").reset_index(drop=True)


def _validate(left: pd.DataFrame, right: pd.DataFrame) -> dict:
    merged = left.merge(right, on="date", suffixes=("_yahoo", "_stooq"))
    if len(merged) < 20:
        raise ValueError(f"insufficient overlap: {len(merged)}")
    fields = {}
    for col in ("open", "high", "low", "close"):
        ratio = merged[f"{col}_yahoo"].astype(float) / merged[f"{col}_stooq"].astype(float)
        fields[col] = {"median_ratio": float(ratio.median()), "within_1pct": float(((ratio / ratio.median() - 1).abs() <= 0.01).mean())}
    if min(v["within_1pct"] for v in fields.values()) < 0.95:
        raise ValueError(f"OHLC cross-validation failed: {fields}")
    return {"sessions": int(len(merged)), "fields": fields, "passed": True}


def repair(price_dir: str | Path = CLEANED_PRICE_DATA_DIR, output: str | Path = Path(PROJECT_PATH) / "output/data_provenance/sec_ticker_tail_alias_price_repair.json") -> dict:
    price_dir, output = Path(price_dir), Path(output)
    yahoo_url = _yahoo_url()
    yahoo_error = None
    try:
        yahoo_payload = _fetch(yahoo_url)
        yahoo = _parse_yahoo(yahoo_payload)
    except Exception as exc:
        # RDUS was delisted from Yahoo; retain the explicit failure and use
        # the existing RDUS history as a reference for the independent Stooq
        # check instead of silently treating the provider as available.
        yahoo_payload = b""
        yahoo_error = repr(exc)
        yahoo = pd.read_csv(price_dir / "rdus.csv", parse_dates=["date"])[PRICE_COLUMNS]
    stooq_url = f"https://raw.githubusercontent.com/{STOOQ_REPOSITORY}/{STOOQ_COMMIT}/{quote(STOOQ_PATH)}"
    stooq_payload = _fetch(stooq_url)
    stooq = _parse_stooq(stooq_payload)
    validation = _validate(yahoo, stooq)
    target = price_dir / "schn.csv"
    existing = pd.read_csv(target, parse_dates=["date"])
    incoming = yahoo.copy(); incoming["ticker"] = "SCHN"
    incoming = incoming.loc[incoming["date"] >= pd.Timestamp("2023-09-01")]
    missing = incoming.loc[~incoming["date"].isin(existing["date"])].copy()
    merged = pd.concat([existing, missing], ignore_index=True).sort_values("date").drop_duplicates("date", keep="first")
    tmp = target.with_suffix(".csv.tmp"); merged.to_csv(tmp, index=False); os.replace(tmp, target)
    result = {"historical_ticker": "SCHN", "provider_ticker": "RDUS", "effective_date": "2023-09-01", "rows_added": int(len(missing)), "source_first_date": yahoo.date.min().strftime("%Y-%m-%d"), "source_last_date": yahoo.date.max().strftime("%Y-%m-%d"), "sec_source_url": SEC_URL, "yahoo_source_url": yahoo_url, "yahoo_payload_sha256": hashlib.sha256(yahoo_payload).hexdigest() if yahoo_payload else None, "yahoo_error": yahoo_error, "reference_price_source": str(price_dir / "rdus.csv") if yahoo_error else None, "stooq_source_url": stooq_url, "stooq_payload_sha256": hashlib.sha256(stooq_payload).hexdigest(), "cross_validation": validation, "formal_financial_files_modified": False, "terminal_returns_modified": False}
    output.parent.mkdir(parents=True, exist_ok=True); output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR)); parser.add_argument("--output", default=str(Path(PROJECT_PATH) / "output/data_provenance/sec_ticker_tail_alias_price_repair.json")); args = parser.parse_args(); print(json.dumps(repair(args.price_dir, args.output), indent=2))
