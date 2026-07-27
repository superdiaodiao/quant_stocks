"""Build and refresh a point-in-time quarterly EPS dataset.

Legacy rows only contain fiscal period ends, so they are migrated with a
conservative 60-day availability lag. Nasdaq's earnings endpoint supplies the
actual reported date for recent quarters. Raw legacy files are never replaced.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import (
    CLEANED_EPS_DATA_FILE,
    FINANCIAL_COVERAGE_FILE,
    NASDAQ_300M_STOCK_LIST_FILE,
    POINT_IN_TIME_EPS_FILE,
)

API = "https://api.nasdaq.com/api/company/{symbol}/earnings-surprise?limit=4"
SEC_TICKERS_API = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_API = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
SEC_HEADERS = {"User-Agent": "quant_stocks research data@example.com", "Accept": "application/json"}
POINT_IN_TIME_EPS_FILE = Path(POINT_IN_TIME_EPS_FILE)
NON_COMMON_SECURITY_PATTERN = (
    r"\bPreferred\b|\bPreference Shares?\b|\bWarrants?\b|\bUnits?\b|Notes? due|"
    r"Debenture|\bRights?\b|Tangible Equity| - Depositary Shares$|"
    r"Depositary Shares, each Representing|Depositary Shares Each Representing|"
    r"Depositary Shares rep|Trust Preferred|Preferred Units|Senior Notes|Subordinated Notes|"
    r"\bETF\b|\bETN\b|\bIndex Fund\b|\bTest Stock\b|\bWhen Issued\b|"
    r"\bAcquisition\b.*\b(?:Corp(?:oration)?|Co(?:mpany)?|Ltd\.?)\b"
)


def investable_common_equities(universe: pd.DataFrame) -> pd.DataFrame:
    """Remove preferreds, warrants, units, rights, and debt from stock research."""
    eligible = universe.loc[
        ~universe["Name"].astype(str).str.contains(
            NON_COMMON_SECURITY_PATTERN, case=False, na=False, regex=True
        )
    ].copy()
    for flag in ("ETF", "Test Issue", "NextShares"):
        if flag in eligible:
            eligible = eligible.loc[
                ~eligible[flag].astype(str).str.upper().eq("Y")
            ]
    return eligible


def _period_end(value: str) -> pd.Timestamp:
    return pd.to_datetime(value, format="%b %Y") + pd.offsets.MonthEnd(0)


def fetch_recent_eps(symbol: str, retries: int = 3) -> pd.DataFrame:
    error = None
    for attempt in range(retries):
        try:
            request = Request(API.format(symbol=symbol), headers=HEADERS)
            with urlopen(request, timeout=30) as response:
                payload = json.load(response)
            rows = (((payload.get("data") or {}).get("earningsSurpriseTable") or {}).get("rows") or [])
            records = []
            for row in rows:
                eps = pd.to_numeric(row.get("eps"), errors="coerce")
                if pd.isna(eps) or not row.get("fiscalQtrEnd") or not row.get("dateReported"):
                    continue
                records.append({
                    "ticker": symbol.upper(),
                    "period_end": _period_end(row["fiscalQtrEnd"]),
                    "available_date": pd.to_datetime(row["dateReported"]),
                    "quarterly_eps": float(eps),
                    "source": "nasdaq_earnings_surprise",
                    "fetched_at": pd.Timestamp.utcnow().tz_localize(None).normalize(),
                })
            return pd.DataFrame(records)
        except Exception as exc:
            error = exc
            time.sleep((2**attempt) + random.random())
    raise RuntimeError(f"{symbol}: {error}")


def fetch_sec_ticker_map() -> dict[str, int]:
    with urlopen(Request(SEC_TICKERS_API, headers=SEC_HEADERS), timeout=60) as response:
        payload = json.load(response)
    return {str(row["ticker"]).upper(): int(row["cik_str"]) for row in payload.values()}


def fetch_sec_eps(symbol: str, cik: int, retries: int = 3) -> pd.DataFrame:
    """Fetch true filing dates for single-quarter SEC EPS facts."""
    error = None
    for attempt in range(retries):
        try:
            with urlopen(Request(SEC_FACTS_API.format(cik=cik), headers=SEC_HEADERS), timeout=45) as response:
                payload = json.load(response)
            facts = payload.get("facts", {}).get("us-gaap", {})
            concept = facts.get("EarningsPerShareDiluted") or facts.get("EarningsPerShareBasic") or {}
            units = concept.get("units", {}).get("USD/shares", [])
            records = []
            for row in units:
                frame = str(row.get("frame") or "")
                if not re.fullmatch(r"CY\d{4}Q[1-4]", frame):
                    continue
                start, end = pd.to_datetime(row.get("start")), pd.to_datetime(row.get("end"))
                if pd.isna(start) or pd.isna(end) or (end - start).days > 120:
                    continue
                if row.get("form") not in {"10-Q", "10-K", "10-Q/A", "10-K/A"}:
                    continue
                records.append({
                    "ticker": symbol.upper(),
                    "period_end": end,
                    "available_date": pd.to_datetime(row["filed"]),
                    "quarterly_eps": float(row["val"]),
                    "source": "sec_companyfacts",
                    "fetched_at": pd.Timestamp.utcnow().tz_localize(None).normalize(),
                })
            if not records:
                return pd.DataFrame()
            return pd.DataFrame(records).sort_values("available_date").drop_duplicates(
                ["ticker", "period_end"], keep="last"
            )
        except Exception as exc:
            error = exc
            time.sleep((2**attempt) + random.random())
    raise RuntimeError(f"{symbol}: {error}")


def migrate_legacy_eps(path: str | Path = CLEANED_EPS_DATA_FILE, lag_days: int = 60) -> pd.DataFrame:
    legacy = pd.read_csv(path)
    legacy["ticker"] = legacy["ticker"].astype(str).str.upper()
    legacy["period_end"] = pd.to_datetime(legacy["report_date"], errors="coerce")
    legacy["quarterly_eps"] = pd.to_numeric(
        legacy.get("diluted_eps", legacy.get("basic_eps")), errors="coerce"
    )
    if "diluted_eps" in legacy and "basic_eps" in legacy:
        legacy["quarterly_eps"] = pd.to_numeric(
            legacy["diluted_eps"].fillna(legacy["basic_eps"]), errors="coerce"
        )
    legacy["available_date"] = legacy["period_end"] + pd.Timedelta(days=lag_days)
    legacy["source"] = "legacy_conservative_60d_lag"
    legacy["fetched_at"] = pd.NaT
    return legacy[[
        "ticker", "period_end", "available_date", "quarterly_eps", "source", "fetched_at"
    ]].dropna(subset=["ticker", "period_end", "quarterly_eps"])


def merge_point_in_time_eps(legacy: pd.DataFrame, recent: pd.DataFrame) -> pd.DataFrame:
    combined = pd.concat([legacy, recent], ignore_index=True)
    combined["period_end"] = pd.to_datetime(combined["period_end"])
    combined["available_date"] = pd.to_datetime(combined["available_date"])
    combined["exact_report_date"] = combined["source"].isin(
        ["nasdaq_earnings_surprise", "sec_companyfacts"]
    )
    combined = combined.sort_values(
        ["ticker", "period_end", "exact_report_date", "fetched_at"]
    ).drop_duplicates(["ticker", "period_end"], keep="last")
    return combined.sort_values(["ticker", "available_date", "period_end"]).reset_index(drop=True)


def audit_financial_coverage(
    frame: pd.DataFrame,
    universe: list[str],
    as_of: date,
    maximum_age_days: int = 200,
) -> dict:
    cutoff = pd.Timestamp(as_of) - pd.Timedelta(days=maximum_age_days)
    usable = frame.loc[frame["available_date"] <= pd.Timestamp(as_of)]
    latest = usable.groupby("ticker")["available_date"].max()
    universe_set = set(map(str.upper, universe))
    covered = {ticker for ticker, available in latest.items() if available >= cutoff} & universe_set
    exact = set(frame.loc[frame["exact_report_date"], "ticker"]) & universe_set
    return {
        "as_of": as_of.isoformat(),
        "universe_count": len(universe_set),
        "fresh_tickers": len(covered),
        "fresh_coverage": len(covered) / max(len(universe_set), 1),
        "tickers_with_exact_recent_report_date": len(exact),
        "missing_or_stale": sorted(universe_set - covered),
        "maximum_age_days": maximum_age_days,
    }


def update_financials(
    as_of: date,
    workers: int = 6,
    limit: int | None = None,
    output: Path = POINT_IN_TIME_EPS_FILE,
) -> dict:
    universe = pd.read_csv(NASDAQ_300M_STOCK_LIST_FILE)["Symbol"].dropna().astype(str).str.upper().tolist()
    requested = universe[:limit] if limit else universe
    rows, failures = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_recent_eps, ticker): ticker for ticker in requested}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                frame = future.result()
                if len(frame):
                    rows.append(frame)
                else:
                    failures.append({"ticker": ticker, "reason": "no_eps_rows"})
            except Exception as exc:
                failures.append({"ticker": ticker, "reason": str(exc)})
    recent = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    existing = pd.read_csv(output, parse_dates=["period_end", "available_date", "fetched_at"]) if output.exists() else pd.DataFrame()
    legacy = migrate_legacy_eps()
    merged = merge_point_in_time_eps(pd.concat([legacy, existing], ignore_index=True), recent)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    merged.to_csv(temporary, index=False)
    os.replace(temporary, output)
    audit = audit_financial_coverage(merged, universe, as_of)
    audit.update({"requested": len(requested), "failures": failures, "output": str(output)})
    audit_path = Path(FINANCIAL_COVERAGE_FILE)
    temporary_audit = audit_path.with_suffix(audit_path.suffix + ".tmp")
    temporary_audit.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    os.replace(temporary_audit, audit_path)
    audit["audit_output"] = str(audit_path)
    return audit


def update_sec_fallback(
    as_of: date,
    workers: int = 4,
    output: Path = POINT_IN_TIME_EPS_FILE,
) -> dict:
    universe = pd.read_csv(NASDAQ_300M_STOCK_LIST_FILE)["Symbol"].dropna().astype(str).str.upper().tolist()
    existing = pd.read_csv(output, parse_dates=["period_end", "available_date", "fetched_at"])
    current_audit = audit_financial_coverage(existing, universe, as_of)
    missing = current_audit["missing_or_stale"]
    cik_map = fetch_sec_ticker_map()
    requested = [ticker for ticker in missing if ticker in cik_map]
    rows, failures = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_sec_eps, ticker, cik_map[ticker]): ticker for ticker in requested
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                frame = future.result()
                if len(frame):
                    rows.append(frame)
                else:
                    failures.append({"ticker": ticker, "reason": "no_sec_quarterly_eps"})
            except Exception as exc:
                failures.append({"ticker": ticker, "reason": str(exc)})
    recent = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    merged = merge_point_in_time_eps(existing, recent)
    temporary = output.with_suffix(output.suffix + ".tmp")
    merged.to_csv(temporary, index=False)
    os.replace(temporary, output)
    audit = audit_financial_coverage(merged, universe, as_of)
    audit.update({"requested": len(requested), "failures": failures, "output": str(output)})
    audit_path = Path(FINANCIAL_COVERAGE_FILE)
    temporary_audit = audit_path.with_suffix(audit_path.suffix + ".tmp")
    temporary_audit.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    os.replace(temporary_audit, audit_path)
    audit["audit_output"] = str(audit_path)
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sec-fallback-only", action="store_true")
    args = parser.parse_args()
    if args.sec_fallback_only:
        result = update_sec_fallback(date.fromisoformat(args.as_of), min(args.workers, 4))
    else:
        result = update_financials(date.fromisoformat(args.as_of), args.workers, args.limit)
    compact = {key: value for key, value in result.items() if key not in {"missing_or_stale", "failures"}}
    compact["missing_or_stale_count"] = len(result["missing_or_stale"])
    compact["failure_count"] = len(result["failures"])
    compact["failure_sample"] = result["failures"][:20]
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
