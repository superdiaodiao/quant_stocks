"""Build point-in-time annual quality fundamentals from SEC Company Facts."""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import (
    FUNDAMENTALS_COVERAGE_FILE,
    FUNDAMENTALS_REFRESH_STATE_FILE,
    NASDAQ_300M_STOCK_LIST_FILE,
    POINT_IN_TIME_FUNDAMENTALS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
    QUARTERLY_FUNDAMENTALS_COVERAGE_FILE,
)
from src.io.financial_update import SEC_FACTS_API, SEC_HEADERS, fetch_sec_ticker_map, investable_common_equities

ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
METRIC_CONCEPTS = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "gross_profit": ("GrossProfit",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "assets": ("Assets",),
    "equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
}
INSTANT_METRICS = {"assets", "equity"}
OUTPUT_COLUMNS = [
    "ticker", "fiscal_end", "available_date", "metric", "value", "taxonomy",
    "concept", "form", "accession", "fetched_at",
]
CORE_QUALITY_METRICS = {"net_income", "operating_cash_flow", "assets", "equity"}
QUARTERLY_FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A"}
QUARTERLY_METRICS = {key: METRIC_CONCEPTS[key] for key in ("revenue", "net_income")}


def _annual_rows(facts: dict, metric: str, concepts: tuple[str, ...]) -> list[dict]:
    """Merge concept generations while preferring the declared modern concept."""
    candidates = []
    for taxonomy_priority, taxonomy in enumerate(("us-gaap", "ifrs-full")):
        namespace = facts.get(taxonomy, {})
        for concept_priority, concept in enumerate(concepts):
            units = namespace.get(concept, {}).get("units", {}).get("USD", [])
            for row in units:
                if row.get("form") not in ANNUAL_FORMS or row.get("fp") != "FY":
                    continue
                end = pd.to_datetime(row.get("end"), errors="coerce")
                filed = pd.to_datetime(row.get("filed"), errors="coerce")
                value = pd.to_numeric(row.get("val"), errors="coerce")
                if pd.isna(end) or pd.isna(filed) or pd.isna(value):
                    continue
                if metric not in INSTANT_METRICS:
                    start = pd.to_datetime(row.get("start"), errors="coerce")
                    if pd.isna(start) or not 250 <= (end - start).days <= 450:
                        continue
                candidates.append({
                    **row,
                    "end": end,
                    "filed": filed,
                    "val": float(value),
                    "taxonomy": taxonomy,
                    "concept": concept,
                    "priority": taxonomy_priority * 100 + concept_priority,
                })
    if not candidates:
        return []
    rows = pd.DataFrame(candidates).sort_values("priority").drop_duplicates(
        ["end", "filed", "accn"], keep="first"
    ).to_dict("records")
    return [
        {
            "fiscal_end": row["end"],
            "available_date": row["filed"],
            "metric": metric,
            "value": row["val"],
            "taxonomy": row["taxonomy"],
            "concept": row["concept"],
            "form": row.get("form"),
            "accession": row.get("accn"),
        }
        for row in rows
    ]


def parse_companyfacts_annual(symbol: str, payload: dict, fetched_at=None) -> pd.DataFrame:
    records = []
    facts = payload.get("facts", {})
    for metric, concepts in METRIC_CONCEPTS.items():
        records.extend(_annual_rows(facts, metric, concepts))
    if not records:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    frame = pd.DataFrame(records)
    frame.insert(0, "ticker", symbol.upper())
    now = fetched_at or pd.Timestamp.now(tz="UTC")
    frame["fetched_at"] = pd.Timestamp(now).tz_localize(None).normalize()
    return frame[OUTPUT_COLUMNS].sort_values(["ticker", "available_date", "fiscal_end", "metric"])


def _explicit_quarter_rows(
    facts: dict, metric: str, concepts: tuple[str, ...]
) -> list[dict]:
    candidates = []
    for taxonomy_priority, taxonomy in enumerate(("us-gaap", "ifrs-full")):
        namespace = facts.get(taxonomy, {})
        for concept_priority, concept in enumerate(concepts):
            for row in namespace.get(concept, {}).get("units", {}).get("USD", []):
                if row.get("form") not in QUARTERLY_FORMS:
                    continue
                if str(row.get("fp") or "") not in {"Q1", "Q2", "Q3", "Q4"}:
                    continue
                start = pd.to_datetime(row.get("start"), errors="coerce")
                end = pd.to_datetime(row.get("end"), errors="coerce")
                filed = pd.to_datetime(row.get("filed"), errors="coerce")
                value = pd.to_numeric(row.get("val"), errors="coerce")
                if (
                    pd.isna(start) or pd.isna(end) or pd.isna(filed) or pd.isna(value)
                    or not 60 <= (end - start).days <= 135
                ):
                    continue
                candidates.append({
                    "fiscal_end": end, "available_date": filed, "metric": metric,
                    "value": float(value), "taxonomy": taxonomy, "concept": concept,
                    "form": row.get("form"), "accession": row.get("accn"),
                    "priority": taxonomy_priority * 100 + concept_priority,
                })
    if not candidates:
        return []
    return pd.DataFrame(candidates).sort_values("priority").drop_duplicates(
        ["fiscal_end", "available_date", "accession"], keep="first"
    ).drop(columns="priority").to_dict("records")


def parse_companyfacts_quarterly(symbol: str, payload: dict, fetched_at=None) -> pd.DataFrame:
    """Parse explicit single quarters and derive Q4 from FY minus Q1-Q3."""
    facts = payload.get("facts", {})
    explicit = []
    for metric, concepts in QUARTERLY_METRICS.items():
        explicit.extend(_explicit_quarter_rows(facts, metric, concepts))
    frame = pd.DataFrame(explicit)
    annual = parse_companyfacts_annual(symbol, payload, fetched_at)
    annual = annual.loc[annual["metric"].isin(QUARTERLY_METRICS)]
    derived = []
    for annual_row in annual.itertuples(index=False):
        candidates = frame.loc[
            (frame["metric"] == annual_row.metric)
            & (frame["fiscal_end"] < annual_row.fiscal_end)
            & (frame["fiscal_end"] >= annual_row.fiscal_end - pd.Timedelta(days=330))
            & (frame["available_date"] <= annual_row.available_date)
        ] if len(frame) else pd.DataFrame()
        if candidates.empty:
            continue
        quarters = candidates.sort_values("available_date").drop_duplicates(
            "fiscal_end", keep="last"
        ).nlargest(3, "fiscal_end").sort_values("fiscal_end")
        ends = quarters["fiscal_end"].tolist() + [annual_row.fiscal_end]
        gaps = pd.Series(ends).diff().dt.days.dropna()
        if len(quarters) != 3 or not gaps.between(60, 135).all():
            continue
        derived.append({
            "fiscal_end": annual_row.fiscal_end,
            "available_date": annual_row.available_date,
            "metric": annual_row.metric,
            "value": float(annual_row.value - quarters["value"].sum()),
            "taxonomy": annual_row.taxonomy,
            "concept": f"derived_q4:{annual_row.concept}",
            "form": annual_row.form,
            "accession": annual_row.accession,
        })
    combined = pd.concat([frame, pd.DataFrame(derived)], ignore_index=True)
    if combined.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    combined.insert(0, "ticker", symbol.upper())
    now = fetched_at or pd.Timestamp.now(tz="UTC")
    combined["fetched_at"] = pd.Timestamp(now).tz_localize(None).normalize()
    return combined[OUTPUT_COLUMNS].sort_values(
        ["ticker", "available_date", "fiscal_end", "metric"]
    )


def fetch_sec_annual_fundamentals(symbol: str, cik: int, retries: int = 3) -> pd.DataFrame:
    error = None
    for attempt in range(retries):
        try:
            request = Request(SEC_FACTS_API.format(cik=cik), headers=SEC_HEADERS)
            with urlopen(request, timeout=45) as response:
                payload = json.load(response)
            return parse_companyfacts_annual(symbol, payload)
        except Exception as exc:
            error = exc
            time.sleep((2**attempt) + random.random())
    raise RuntimeError(f"{symbol}: {error}")


def fetch_sec_fundamentals(
    symbol: str, cik: int, retries: int = 3
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch one SEC payload and parse both annual and quarterly datasets."""
    error = None
    for attempt in range(retries):
        try:
            request = Request(SEC_FACTS_API.format(cik=cik), headers=SEC_HEADERS)
            with urlopen(request, timeout=45) as response:
                payload = json.load(response)
            return (
                parse_companyfacts_annual(symbol, payload),
                parse_companyfacts_quarterly(symbol, payload),
            )
        except Exception as exc:
            error = exc
            time.sleep((2**attempt) + random.random())
    raise RuntimeError(f"{symbol}: {error}")


def merge_fundamentals(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    combined = pd.concat([existing, incoming], ignore_index=True)
    if combined.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    for column in ("fiscal_end", "available_date", "fetched_at"):
        combined[column] = pd.to_datetime(combined[column], errors="coerce")
    combined["ticker"] = combined["ticker"].astype(str).str.upper()
    combined["value"] = pd.to_numeric(combined["value"], errors="coerce")
    combined = combined.dropna(subset=["ticker", "fiscal_end", "available_date", "metric", "value"])
    combined = combined.sort_values("fetched_at").drop_duplicates(
        ["ticker", "fiscal_end", "available_date", "metric", "accession"], keep="last"
    )
    return combined[OUTPUT_COLUMNS].sort_values(
        ["ticker", "available_date", "fiscal_end", "metric"]
    ).reset_index(drop=True)


def audit_fundamentals_coverage(frame: pd.DataFrame, universe: list[str], as_of: date) -> dict:
    usable = frame.loc[frame["available_date"] <= pd.Timestamp(as_of)].copy()
    recent_cutoff = pd.Timestamp(as_of) - pd.Timedelta(days=550)
    required = CORE_QUALITY_METRICS
    recent = usable.loc[usable["available_date"] >= recent_cutoff]
    metric_sets = recent.groupby("ticker")["metric"].agg(set) if len(recent) else pd.Series(dtype=object)
    universe_set = set(map(str.upper, universe))
    covered = {ticker for ticker, metrics in metric_sets.items() if required.issubset(metrics)} & universe_set
    optional_coverage = {
        metric: len(set(recent.loc[recent["metric"] == metric, "ticker"]) & universe_set)
        / max(len(universe_set), 1)
        for metric in ("revenue", "gross_profit")
    }
    return {
        "as_of": as_of.isoformat(),
        "universe_count": len(universe_set),
        "fresh_complete_tickers": len(covered),
        "fresh_complete_coverage": len(covered) / max(len(universe_set), 1),
        "required_metrics": sorted(required),
        "optional_metric_coverage": optional_coverage,
        "maximum_age_days": 550,
        "missing_or_incomplete": sorted(universe_set - covered),
    }


def audit_quarterly_coverage(
    frame: pd.DataFrame,
    universe: list[str],
    as_of: date,
    maximum_age_days: int = 200,
) -> dict:
    cutoff = pd.Timestamp(as_of) - pd.Timedelta(days=maximum_age_days)
    usable = frame.loc[
        (frame["available_date"] <= pd.Timestamp(as_of))
        & (frame["available_date"] >= cutoff)
    ]
    metric_sets = (
        usable.groupby("ticker")["metric"].agg(set)
        if len(usable) else pd.Series(dtype=object)
    )
    universe_set = set(map(str.upper, universe))
    covered = {
        ticker for ticker, metrics in metric_sets.items()
        if {"revenue", "net_income"}.issubset(metrics)
    } & universe_set
    return {
        "as_of": as_of.isoformat(),
        "universe_count": len(universe_set),
        "fresh_complete_tickers": len(covered),
        "fresh_complete_coverage": len(covered) / max(len(universe_set), 1),
        "required_metrics": ["net_income", "revenue"],
        "maximum_age_days": maximum_age_days,
        "missing_or_incomplete": sorted(universe_set - covered),
    }


def update_fundamentals(
    as_of: date,
    workers: int = 4,
    limit: int | None = None,
    refresh_after_days: int = 30,
    output: Path = Path(POINT_IN_TIME_FUNDAMENTALS_FILE),
    quarterly_output: Path = Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE),
    force: bool = False,
) -> dict:
    universe_frame = investable_common_equities(pd.read_csv(NASDAQ_300M_STOCK_LIST_FILE))
    universe = universe_frame["Symbol"].dropna().astype(str).str.upper().tolist()
    cik_map = fetch_sec_ticker_map()
    existing = (
        pd.read_csv(output, parse_dates=["fiscal_end", "available_date", "fetched_at"])
        if output.exists() else pd.DataFrame(columns=OUTPUT_COLUMNS)
    )
    quarterly_existing = (
        pd.read_csv(
            quarterly_output,
            parse_dates=["fiscal_end", "available_date", "fetched_at"],
        )
        if quarterly_output.exists() else pd.DataFrame(columns=OUTPUT_COLUMNS)
    )
    state_path = Path(FUNDAMENTALS_REFRESH_STATE_FILE)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if not state and len(existing):
        last_fetch = existing.groupby("ticker")["fetched_at"].max()
        state.update({
            str(ticker): {"last_attempt": value.date().isoformat(), "status": "has_data"}
            for ticker, value in last_fetch.items() if pd.notna(value)
        })
        audit_path = Path(FUNDAMENTALS_COVERAGE_FILE)
        if audit_path.exists():
            prior_audit = json.loads(audit_path.read_text(encoding="utf-8"))
            for failure in prior_audit.get("failures", []):
                state[str(failure["ticker"]).upper()] = {
                    "last_attempt": as_of.isoformat(), "status": "no_data_or_failed"
                }
    refresh_cutoff = pd.Timestamp(as_of) - pd.Timedelta(days=refresh_after_days)
    requested = []
    for ticker in universe:
        if ticker not in cik_map:
            continue
        last_attempt = pd.to_datetime(
            (state.get(ticker) or {}).get("last_attempt"), errors="coerce"
        )
        if force or pd.isna(last_attempt) or last_attempt < refresh_cutoff:
            requested.append(ticker)
    if limit:
        requested = requested[:limit]
    rows, quarterly_rows, failures = [], [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_sec_fundamentals, ticker, cik_map[ticker]): ticker
            for ticker in requested
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                annual_frame, quarterly_frame = future.result()
                if len(annual_frame):
                    rows.append(annual_frame)
                if len(quarterly_frame):
                    quarterly_rows.append(quarterly_frame)
                if not len(annual_frame) and not len(quarterly_frame):
                    failures.append({"ticker": ticker, "reason": "no_sec_fundamentals"})
            except Exception as exc:
                failures.append({"ticker": ticker, "reason": str(exc)})
    incoming = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=OUTPUT_COLUMNS)
    quarterly_incoming = (
        pd.concat(quarterly_rows, ignore_index=True)
        if quarterly_rows else pd.DataFrame(columns=OUTPUT_COLUMNS)
    )
    merged = merge_fundamentals(existing, incoming)
    quarterly_merged = merge_fundamentals(quarterly_existing, quarterly_incoming)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    merged.to_csv(temporary, index=False)
    os.replace(temporary, output)
    quarterly_output.parent.mkdir(parents=True, exist_ok=True)
    quarterly_temporary = quarterly_output.with_suffix(quarterly_output.suffix + ".tmp")
    quarterly_merged.to_csv(quarterly_temporary, index=False)
    os.replace(quarterly_temporary, quarterly_output)
    successful_tickers = set(incoming["ticker"].astype(str)) | set(
        quarterly_incoming["ticker"].astype(str)
    )
    result_by_ticker = {
        ticker: ("has_data" if ticker in successful_tickers else "no_data_or_failed")
        for ticker in requested
    }
    for ticker, status in result_by_ticker.items():
        state[ticker] = {"last_attempt": as_of.isoformat(), "status": status}
    temporary_state = state_path.with_suffix(state_path.suffix + ".tmp")
    temporary_state.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary_state, state_path)
    audit = audit_fundamentals_coverage(merged, universe, as_of)
    quarterly_audit = audit_quarterly_coverage(quarterly_merged, universe, as_of)
    audit.update({
        "requested": len(requested), "failures": failures, "output": str(output),
        "quarterly_output": str(quarterly_output),
        "quarterly_fresh_complete_tickers": quarterly_audit["fresh_complete_tickers"],
        "quarterly_fresh_complete_coverage": quarterly_audit["fresh_complete_coverage"],
    })
    audit_path = Path(FUNDAMENTALS_COVERAGE_FILE)
    temporary_audit = audit_path.with_suffix(audit_path.suffix + ".tmp")
    temporary_audit.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    os.replace(temporary_audit, audit_path)
    quarterly_audit.update({
        "requested": len(requested), "failures": failures,
        "output": str(quarterly_output),
    })
    quarterly_audit_path = Path(QUARTERLY_FUNDAMENTALS_COVERAGE_FILE)
    quarterly_audit_temporary = quarterly_audit_path.with_suffix(
        quarterly_audit_path.suffix + ".tmp"
    )
    quarterly_audit_temporary.write_text(
        json.dumps(quarterly_audit, indent=2), encoding="utf-8"
    )
    os.replace(quarterly_audit_temporary, quarterly_audit_path)
    audit["audit_output"] = str(audit_path)
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--refresh-after-days", type=int, default=30)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args()
    as_of = date.fromisoformat(args.as_of)
    if args.audit_only:
        frame = pd.read_csv(
            POINT_IN_TIME_FUNDAMENTALS_FILE,
            parse_dates=["fiscal_end", "available_date", "fetched_at"],
        )
        universe_frame = investable_common_equities(pd.read_csv(NASDAQ_300M_STOCK_LIST_FILE))
        universe = universe_frame["Symbol"].dropna().astype(str).str.upper().tolist()
        result = audit_fundamentals_coverage(frame, universe, as_of)
        result.update({"requested": 0, "failures": [], "output": str(POINT_IN_TIME_FUNDAMENTALS_FILE)})
        audit_path = Path(FUNDAMENTALS_COVERAGE_FILE)
        temporary_audit = audit_path.with_suffix(audit_path.suffix + ".tmp")
        temporary_audit.write_text(json.dumps(result, indent=2), encoding="utf-8")
        os.replace(temporary_audit, audit_path)
    else:
        result = update_fundamentals(
            as_of, args.workers, args.limit, args.refresh_after_days, force=args.force
        )
    compact = {key: value for key, value in result.items() if key not in {"missing_or_incomplete", "failures"}}
    compact["missing_or_incomplete_count"] = len(result["missing_or_incomplete"])
    compact["failure_count"] = len(result["failures"])
    compact["failure_sample"] = result["failures"][:20]
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
