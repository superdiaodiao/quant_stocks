"""Build point-in-time annual quality fundamentals from SEC Company Facts."""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from datetime import date
from numbers import Number
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
from src.io.financial_update import (
    SEC_FACTS_API,
    SEC_HEADERS,
    SEC_TICKERS_API,
    fetch_sec_ticker_map,
    investable_common_equities,
)

ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
SEC_COMPANYFACTS_CACHE_DIR = (
    Path(POINT_IN_TIME_FUNDAMENTALS_FILE).parent / "sec_companyfacts_cache"
)
SEC_COMPANY_BROWSE_API = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcompany&owner=exclude&output=atom&CIK={ticker}"
)
VALIDATED_FOREIGN_QUARTERLY_FILE = (
    Path(__file__).resolve().parents[2]
    / "stocks_list_dir/nasdaq/validated_foreign_quarterly.csv"
)
COMPANYFACTS_REPARSE_STATE_NAME = "reparse_state.json"
RAW_CACHE_REFRESH_STATE_NAME = "raw_cache_refresh_state.json"
RAW_CACHE_CHECKPOINT_PENDING_NAME = ".raw_cache_checkpoint.pending.json"
RAW_CACHE_CHECKPOINT_CIK_INTERVAL = 5
# Keep the default online refresh bounded after observed SEC interruptions.
# Callers can explicitly choose a smaller/larger batch with ``--limit``.
SEC_REFRESH_DEFAULT_CIK_BATCH_LIMIT = 25
SEC_REQUEST_MIN_INTERVAL_SECONDS = 0.125
_SEC_REQUEST_LOCK = threading.Lock()
_SEC_NEXT_REQUEST_AT = 0.0


@contextmanager
def companyfacts_cache_lock(
    cache_dir: Path, timeout_seconds: float = 600
):
    """Serialize processes that can update the raw cache or parsed outputs."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cache_dir / ".update.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Timed out waiting for Company Facts cache lock "
                        f"{lock_path}"
                    )
                time.sleep(0.1)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
METRIC_CONCEPTS = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "RevenuesNetOfInterestExpense",
        "RegulatedAndUnregulatedOperatingRevenue",
        "GrossInvestmentIncomeOperating",
        "Revenues",
        "SalesRevenueNet",
    ),
    "net_income": (
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ),
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
BANK_NET_INTEREST_CONCEPTS = (
    "InterestIncomeExpenseNet",
    "InterestRevenueExpenseNet",
)
BANK_NONINTEREST_CONCEPTS = ("NoninterestIncome",)


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
    records.extend(_bank_revenue_rows(
        _annual_rows(
            facts, "bank_net_interest_income",
            BANK_NET_INTEREST_CONCEPTS,
        ),
        _annual_rows(
            facts, "bank_noninterest_income",
            BANK_NONINTEREST_CONCEPTS,
        ),
    ))
    if not records:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    frame = pd.DataFrame(records)
    frame["_derived"] = frame["concept"].str.startswith("derived_")
    frame = (
        frame.sort_values("_derived")
        .drop_duplicates(
            ["fiscal_end", "available_date", "metric", "accession"],
            keep="first",
        )
        .drop(columns="_derived")
    )
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
                fp = str(row.get("fp") or "")
                frame = str(row.get("frame") or "")
                # SEC occasionally labels quarter-length facts carried in a
                # 10-Q as fp=FY (including both the current and comparative
                # quarter).  The form plus the duration check below is the
                # reliable quarter marker in that case.  A 10-K still needs
                # an explicit quarter frame so annual facts are not admitted.
                quarter_marked = (
                    fp in {"Q1", "Q2", "Q3", "Q4"}
                    or row.get("form") in {"10-Q", "10-Q/A"}
                    or bool(
                        row.get("form") in {"10-K", "10-K/A"}
                        and re.search(r"Q[1-4]$", frame)
                    )
                )
                if not quarter_marked:
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


def _derived_ytd_quarter_rows(
    facts: dict, metric: str, concepts: tuple[str, ...]
) -> list[dict]:
    """Derive Q2/Q3 single-quarter values from filed cumulative YTD facts."""
    candidates = []
    for taxonomy_priority, taxonomy in enumerate(("us-gaap", "ifrs-full")):
        namespace = facts.get(taxonomy, {})
        for concept_priority, concept in enumerate(concepts):
            units = namespace.get(concept, {}).get("units", {}).get("USD", [])
            facts_for_concept = []
            for row in units:
                if row.get("form") not in QUARTERLY_FORMS:
                    continue
                start = pd.to_datetime(row.get("start"), errors="coerce")
                end = pd.to_datetime(row.get("end"), errors="coerce")
                filed = pd.to_datetime(row.get("filed"), errors="coerce")
                value = pd.to_numeric(row.get("val"), errors="coerce")
                if (
                    pd.isna(start)
                    or pd.isna(end)
                    or pd.isna(filed)
                    or pd.isna(value)
                ):
                    continue
                duration = (end - start).days
                if not 60 <= duration <= 320:
                    continue
                # A Q1 filing can legitimately repeat the prior-year Q1
                # comparative, but it cannot contain a prior-year Q2/Q3 YTD
                # period.  Some issuer XBRL contexts mis-tag the current Q1
                # amount with such a long comparative period; subtracting a
                # real prior YTD value then creates a fictitious negative
                # quarter (for example DAVE 2024-Q3 in its 2025-Q1 filing).
                if duration > 135 and str(row.get("fp", "")).upper() == "Q1":
                    continue
                facts_for_concept.append({
                    **row,
                    "start": start,
                    "end": end,
                    "filed": filed,
                    "val": float(value),
                    "duration": duration,
                })
            for current in facts_for_concept:
                if not 136 <= current["duration"] <= 320:
                    continue
                prior = [
                    row
                    for row in facts_for_concept
                    if row["start"] == current["start"]
                    and row["end"] < current["end"]
                    and row["filed"] <= current["filed"]
                    and 60 <= row["duration"] < current["duration"]
                ]
                if not prior:
                    continue
                previous = max(
                    prior,
                    key=lambda row: (
                        row["end"],
                        row["filed"],
                    ),
                )
                gap = (current["end"] - previous["end"]).days
                if not 60 <= gap <= 135:
                    continue
                candidates.append({
                    "fiscal_end": current["end"],
                    "available_date": current["filed"],
                    "metric": metric,
                    "value": current["val"] - previous["val"],
                    "taxonomy": taxonomy,
                    "concept": f"derived_ytd:{concept}",
                    "form": current.get("form"),
                    "accession": current.get("accn"),
                    "priority": taxonomy_priority * 100 + concept_priority,
                })
    if not candidates:
        return []
    return (
        pd.DataFrame(candidates)
        .sort_values("priority")
        .drop_duplicates(
            ["fiscal_end", "available_date", "accession"],
            keep="first",
        )
        .drop(columns="priority")
        .to_dict("records")
    )


def _bank_revenue_rows(
    net_interest_rows: list[dict],
    noninterest_rows: list[dict],
) -> list[dict]:
    """Combine same-filing bank net-interest and noninterest income."""
    if not net_interest_rows or not noninterest_rows:
        return []
    keys = ["fiscal_end", "available_date", "accession"]
    def deduplicate(rows: list[dict]) -> pd.DataFrame:
        frame = pd.DataFrame(rows)
        frame["_derived"] = frame["concept"].str.startswith("derived_")
        return (
            frame.sort_values("_derived")
            .drop_duplicates(keys, keep="first")
            .drop(columns="_derived")
        )

    net = deduplicate(net_interest_rows)
    noninterest = deduplicate(noninterest_rows)
    combined = net.merge(
        noninterest,
        on=keys,
        how="inner",
        suffixes=("_net_interest", "_noninterest"),
        validate="one_to_one",
    )
    rows = []
    for row in combined.itertuples(index=False):
        rows.append({
            "fiscal_end": row.fiscal_end,
            "available_date": row.available_date,
            "metric": "revenue",
            "value": float(
                row.value_net_interest + row.value_noninterest
            ),
            "taxonomy": row.taxonomy_net_interest,
            "concept": (
                "derived_bank_revenue:"
                f"{row.concept_net_interest}+{row.concept_noninterest}"
            ),
            "form": row.form_net_interest,
            "accession": row.accession,
        })
    return rows


def _coalesce_equivalent_quarter_ends(
    frame: pd.DataFrame,
    tolerance_days: int = 7,
) -> pd.DataFrame:
    """Collapse duplicate coordinates for one reported fiscal quarter.

    SEC Company Facts can expose the same fact with adjacent fiscal-end dates
    (for example, both the Saturday and Sunday at a 52/53-week quarter end).
    Counting both coordinates as separate quarters can displace Q1 from an
    annual-residual calculation.  Treat near dates as equivalent only when the
    metric and reported value agree; retain the latest available coordinate so
    amendments and later comparative filings keep their existing precedence.
    """
    if frame.empty or len(frame) == 1:
        return frame.copy()
    required = {"fiscal_end", "available_date", "metric", "value"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(
            "quarter coordinate frame is missing columns: "
            f"{sorted(missing)}"
        )
    kept = []
    for _, metric_rows in frame.groupby("metric", sort=False):
        ordered = metric_rows.sort_values(
            ["fiscal_end", "available_date"], kind="stable"
        )
        clusters: list[list[pd.Series]] = []
        for _, row in ordered.iterrows():
            if clusters:
                previous = clusters[-1][-1]
                day_gap = abs(
                    (pd.Timestamp(row["fiscal_end"])
                     - pd.Timestamp(previous["fiscal_end"])).days
                )
                same_value = math.isclose(
                    float(row["value"]),
                    float(previous["value"]),
                    rel_tol=1e-9,
                    abs_tol=1e-6,
                )
                if day_gap <= tolerance_days and same_value:
                    clusters[-1].append(row)
                    continue
            clusters.append([row])
        for cluster in clusters:
            kept.append(max(
                cluster,
                key=lambda row: (
                    pd.Timestamp(row["available_date"]),
                    pd.Timestamp(row["fiscal_end"]),
                ),
            ))
    return pd.DataFrame(kept, columns=frame.columns).reset_index(drop=True)


def parse_companyfacts_quarterly(symbol: str, payload: dict, fetched_at=None) -> pd.DataFrame:
    """Parse explicit quarters and derive Q2/Q3 from YTD plus Q4 from FY."""
    facts = payload.get("facts", {})
    explicit = []
    derived_ytd = []
    for metric, concepts in QUARTERLY_METRICS.items():
        explicit.extend(_explicit_quarter_rows(facts, metric, concepts))
        derived_ytd.extend(
            _derived_ytd_quarter_rows(facts, metric, concepts)
        )
    bank_net_interest = (
        _explicit_quarter_rows(
            facts, "bank_net_interest_income",
            BANK_NET_INTEREST_CONCEPTS,
        )
        + _derived_ytd_quarter_rows(
            facts, "bank_net_interest_income",
            BANK_NET_INTEREST_CONCEPTS,
        )
    )
    bank_noninterest = (
        _explicit_quarter_rows(
            facts, "bank_noninterest_income",
            BANK_NONINTEREST_CONCEPTS,
        )
        + _derived_ytd_quarter_rows(
            facts, "bank_noninterest_income",
            BANK_NONINTEREST_CONCEPTS,
        )
    )
    derived_bank_revenue = _bank_revenue_rows(
        bank_net_interest, bank_noninterest
    )
    frame = pd.concat(
        [
            pd.DataFrame(explicit),
            pd.DataFrame(derived_ytd),
            pd.DataFrame(derived_bank_revenue),
        ],
        ignore_index=True,
    )
    if len(frame):
        frame["_derived"] = frame["concept"].str.startswith("derived_")
        frame = (
            frame.sort_values("_derived")
            .drop_duplicates(
                ["fiscal_end", "available_date", "metric", "accession"],
                keep="first",
            )
            .drop(columns="_derived")
        )
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
        )
        quarters = _coalesce_equivalent_quarter_ends(quarters)
        quarters = quarters.nlargest(3, "fiscal_end").sort_values(
            "fiscal_end"
        )
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


def parse_validated_foreign_quarterly(
    symbol: str,
    cik: int,
    payload: dict,
    fetched_at,
    expected_currency: str | None = None,
) -> pd.DataFrame:
    """Parse foreign quarters only after the strict research gate passes.

    The import is local to keep the normal SEC parser independent of the
    optional research path.  Callers must opt in explicitly.
    """
    from src.research.foreign_quarterly_diagnostics import (
        diagnose_foreign_payload,
        foreign_quarters_to_point_in_time,
    )

    diagnosis = diagnose_foreign_payload(symbol, cik, payload)
    if not diagnosis["eligible_for_parser_research"]:
        raise RuntimeError(
            f"{symbol} failed validated foreign-quarter parsing: "
            f"{diagnosis['diagnostic_status']}"
        )
    if (
        expected_currency
        and diagnosis["selected_currency"] != expected_currency
    ):
        raise RuntimeError(
            f"{symbol} validated currency changed from "
            f"{expected_currency} to {diagnosis['selected_currency']}"
        )
    return foreign_quarters_to_point_in_time(
        symbol,
        payload,
        fetched_at,
        diagnosis["selected_currency"],
    )[OUTPUT_COLUMNS].sort_values(
        ["ticker", "available_date", "fiscal_end", "metric"]
    )


def validated_foreign_quarterly_registry(
    path: Path | None = None,
) -> dict[str, dict]:
    """Load the small reviewed registry used by every parse path."""
    path = Path(path or VALIDATED_FOREIGN_QUARTERLY_FILE)
    if not path.exists():
        return {}
    frame = pd.read_csv(path, dtype={"ticker": str, "currency": str})
    required = {"ticker", "cik", "currency", "validation_rule"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(
            f"Validated foreign registry missing columns: {sorted(missing)}"
        )
    frame["ticker"] = frame["ticker"].str.strip().str.upper()
    frame["currency"] = frame["currency"].str.strip().str.upper()
    frame["cik"] = pd.to_numeric(frame["cik"], errors="raise").astype(int)
    if frame["ticker"].eq("").any() or frame["ticker"].duplicated().any():
        raise RuntimeError(
            "Validated foreign registry has blank or duplicate tickers"
        )
    return {
        row.ticker: {
            "cik": int(row.cik),
            "currency": row.currency,
            "validation_rule": str(row.validation_rule),
        }
        for row in frame.itertuples(index=False)
    }


def parse_registered_companyfacts_quarterly(
    symbol: str,
    cik: int,
    payload: dict,
    fetched_at,
) -> tuple[pd.DataFrame, bool]:
    """Parse normal quarters plus any still-valid reviewed foreign series."""
    standard = parse_companyfacts_quarterly(symbol, payload, fetched_at)
    registered = validated_foreign_quarterly_registry().get(symbol.upper())
    if registered is None:
        return standard, False
    if int(cik) != registered["cik"]:
        raise RuntimeError(
            f"{symbol} registry CIK {registered['cik']} does not match "
            f"payload CIK {int(cik)}"
        )
    foreign = parse_validated_foreign_quarterly(
        symbol,
        cik,
        payload,
        fetched_at,
        expected_currency=registered["currency"],
    )
    return pd.concat([standard, foreign], ignore_index=True), True


def _companyfacts_cache_path(cik: int, cache_dir: Path) -> Path:
    legacy = cache_dir / f"CIK{int(cik):010d}.json"
    compressed = cache_dir / f"CIK{int(cik):010d}.json.gz"
    if legacy.exists() and compressed.exists():
        raise RuntimeError(
            f"CIK {int(cik)} has both legacy and compressed cache files"
        )
    return legacy if legacy.exists() else compressed


def _companyfacts_cache_files(cache_dir: Path) -> list[Path]:
    cache_dir = Path(cache_dir)
    paths = sorted({
        *cache_dir.glob("CIK*.json"),
        *cache_dir.glob("CIK*.json.gz"),
    })
    seen_ciks = set()
    for path in paths:
        raw_cik = path.name.removeprefix("CIK").split(".json", 1)[0]
        if raw_cik in seen_ciks:
            raise RuntimeError(
                f"CIK {raw_cik} has both legacy and compressed cache files"
            )
        seen_ciks.add(raw_cik)
    return paths


def _read_companyfacts_cache_envelope(path: Path) -> dict:
    try:
        if path.name.endswith(".json.gz"):
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                return json.load(handle)
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{path} is unavailable or invalid: {exc}") from exc


def _validated_companyfacts_cache_payload(
    envelope: dict,
    cik: int,
) -> tuple[dict, pd.Timestamp]:
    """Validate one decoded cache envelope and return parser inputs."""
    payload = envelope.get("payload")
    fetched_at = pd.to_datetime(envelope.get("fetched_at"), errors="coerce")
    if (
        envelope.get("cik") != int(cik)
        or not isinstance(payload, dict)
        or not isinstance(payload.get("facts"), dict)
        or pd.isna(fetched_at)
    ):
        raise RuntimeError(f"CIK {int(cik)} cache has an invalid envelope")
    return payload, pd.Timestamp(fetched_at)


def _read_companyfacts_cache(cik: int, cache_dir: Path) -> tuple[dict, pd.Timestamp]:
    path = _companyfacts_cache_path(cik, cache_dir)
    try:
        envelope = _read_companyfacts_cache_envelope(path)
    except RuntimeError as exc:
        raise RuntimeError(f"CIK {int(cik)} cache unavailable or invalid: {exc}") from exc
    return _validated_companyfacts_cache_payload(envelope, cik)


def _wait_for_sec_request_slot() -> None:
    """Limit all worker threads together to at most eight SEC requests/sec."""
    global _SEC_NEXT_REQUEST_AT
    with _SEC_REQUEST_LOCK:
        now = time.monotonic()
        wait_seconds = max(0.0, _SEC_NEXT_REQUEST_AT - now)
        if wait_seconds:
            time.sleep(wait_seconds)
        _SEC_NEXT_REQUEST_AT = (
            max(time.monotonic(), _SEC_NEXT_REQUEST_AT)
            + SEC_REQUEST_MIN_INTERVAL_SECONDS
        )


def _write_companyfacts_cache(
    symbols: str | list[str],
    cik: int,
    payload: dict,
    fetched_at: pd.Timestamp,
    cache_dir: Path,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _companyfacts_cache_path(cik, cache_dir)
    temporary = path.with_suffix(path.suffix + ".tmp")
    prior_symbols = set()
    if path.exists():
        try:
            prior = _read_companyfacts_cache_envelope(path)
            if prior.get("cik") == int(cik):
                prior_symbols.update(map(str, prior.get("symbols", [])))
        except RuntimeError:
            pass
    incoming_symbols = (
        [symbols] if isinstance(symbols, str) else symbols
    )
    envelope = {
        "cik": int(cik),
        "symbols": sorted({
            *(str(symbol).strip().upper() for symbol in incoming_symbols),
            *(str(symbol).strip().upper() for symbol in prior_symbols),
        } - {""}),
        "fetched_at": pd.Timestamp(fetched_at).isoformat(),
        "source_url": SEC_FACTS_API.format(cik=int(cik)),
        "payload": payload,
    }
    serialized = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    if path.name.endswith(".json.gz"):
        temporary.write_bytes(gzip.compress(serialized, compresslevel=6, mtime=0))
    else:
        temporary.write_bytes(serialized)
    os.replace(temporary, path)


def _fetch_companyfacts_payload(
    symbols: str | list[str],
    cik: int,
    retries: int,
    cache_dir: Path,
    offline_cache: bool,
) -> tuple[dict, pd.Timestamp]:
    if offline_cache:
        return _read_companyfacts_cache(cik, cache_dir)
    error = None
    for attempt in range(retries):
        try:
            _wait_for_sec_request_slot()
            request = Request(SEC_FACTS_API.format(cik=cik), headers=SEC_HEADERS)
            with urlopen(request, timeout=45) as response:
                payload = json.load(response)
            fetched_at = pd.Timestamp.now(tz="UTC").tz_localize(None)
            _write_companyfacts_cache(
                symbols, cik, payload, fetched_at, cache_dir
            )
            return payload, fetched_at
        except Exception as exc:
            error = exc
            time.sleep((2**attempt) + random.random())
    raise RuntimeError(f"CIK {int(cik)}: {error}")


def fetch_sec_annual_fundamentals(
    symbol: str,
    cik: int,
    retries: int = 3,
    cache_dir: Path = SEC_COMPANYFACTS_CACHE_DIR,
    offline_cache: bool = False,
) -> pd.DataFrame:
    error = None
    try:
        payload, fetched_at = _fetch_companyfacts_payload(
            symbol, cik, retries, Path(cache_dir), offline_cache
        )
        return parse_companyfacts_annual(symbol, payload, fetched_at)
    except Exception as exc:
        error = exc
    raise RuntimeError(f"{symbol}: {error}")


def fetch_sec_fundamentals(
    symbol: str,
    cik: int,
    retries: int = 3,
    cache_dir: Path = SEC_COMPANYFACTS_CACHE_DIR,
    offline_cache: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch one SEC payload and parse both annual and quarterly datasets."""
    try:
        payload, fetched_at = _fetch_companyfacts_payload(
            symbol, cik, retries, Path(cache_dir), offline_cache
        )
        return (
            parse_companyfacts_annual(symbol, payload, fetched_at),
            parse_registered_companyfacts_quarterly(
                symbol, cik, payload, fetched_at
            )[0],
        )
    except Exception as exc:
        raise RuntimeError(f"{symbol}: {exc}") from exc


def fetch_sec_fundamentals_for_symbols(
    symbols: list[str],
    cik: int,
    retries: int = 3,
    cache_dir: Path = SEC_COMPANYFACTS_CACHE_DIR,
    offline_cache: bool = False,
) -> dict[str, tuple[pd.DataFrame, pd.DataFrame]]:
    """Fetch one CIK payload once and parse it for every requested symbol."""
    normalized = list(dict.fromkeys(
        str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()
    ))
    if not normalized:
        raise ValueError("at least one symbol is required for a CIK fetch")
    try:
        payload, fetched_at = _fetch_companyfacts_payload(
            normalized, cik, retries, Path(cache_dir), offline_cache
        )
        return {
            symbol: (
                parse_companyfacts_annual(symbol, payload, fetched_at),
                parse_registered_companyfacts_quarterly(
                    symbol, cik, payload, fetched_at
                )[0],
            )
            for symbol in normalized
        }
    except Exception as exc:
        raise RuntimeError(
            f"CIK {int(cik)} ({', '.join(normalized)}): {exc}"
        ) from exc


def _group_tickers_by_cik(
    tickers: list[str], cik_map: dict[str, int]
) -> dict[int, list[str]]:
    grouped: dict[int, list[str]] = {}
    for ticker in tickers:
        grouped.setdefault(int(cik_map[ticker]), []).append(ticker)
    return grouped


def limit_refresh_tickers_by_cik(
    tickers: list[str],
    cik_map: dict[str, int],
    limit: int | None,
) -> list[str]:
    """Limit unique SEC requests without splitting symbols sharing a CIK."""
    if limit is None:
        return list(tickers)
    if limit <= 0:
        raise ValueError("limit must be a positive integer")
    selected_ciks: set[int] = set()
    selected = []
    for ticker in tickers:
        cik = int(cik_map[ticker])
        if cik not in selected_ciks:
            if len(selected_ciks) >= limit:
                continue
            selected_ciks.add(cik)
        selected.append(ticker)
    return selected


def expand_selected_cik_aliases(
    selected: list[str],
    requested_universe: list[str],
    cik_map: dict[str, int],
    excluded_symbols: set[str] | None = None,
) -> list[str]:
    """Include every uncached universe alias for already selected CIKs."""
    selected_ciks = {int(cik_map[ticker]) for ticker in selected}
    excluded = {
        str(symbol).strip().upper()
        for symbol in (excluded_symbols or set())
    }
    return [
        ticker
        for ticker in requested_universe
        if int(cik_map[ticker]) in selected_ciks and ticker not in excluded
    ]


def unmapped_fundamentals_tickers(
    tickers: list[str], cik_map: dict[str, int]
) -> list[str]:
    return [ticker for ticker in tickers if ticker not in cik_map]


def prioritize_refresh_tickers(
    requested_universe: list[str],
    priority_tickers: list[str] | None,
) -> tuple[list[str], int]:
    """Move prioritized symbols first without dropping the remaining universe."""
    universe = list(dict.fromkeys(
        str(ticker).strip().upper()
        for ticker in requested_universe
        if str(ticker).strip()
    ))
    if not priority_tickers:
        return universe, 0
    universe_set = set(universe)
    prioritized = [
        ticker
        for ticker in dict.fromkeys(
            str(ticker).strip().upper()
            for ticker in priority_tickers
            if str(ticker).strip()
        )
        if ticker in universe_set
    ]
    prioritized_set = set(prioritized)
    return prioritized + [
        ticker for ticker in universe if ticker not in prioritized_set
    ], len(prioritized)


def build_requested_refresh_universe(
    universe: list[str],
    explicit_tickers: list[str] | None,
    priority_tickers: list[str] | None,
    cache_missing_only: bool,
) -> list[str]:
    """Include historical priority symbols in cache-repair batches."""
    if explicit_tickers:
        ordered = explicit_tickers
    else:
        ordered = list(universe)
        if cache_missing_only and priority_tickers:
            ordered = list(priority_tickers) + ordered
    return list(dict.fromkeys(
        str(ticker).strip().upper()
        for ticker in ordered
        if str(ticker).strip()
    ))


def select_fundamentals_refresh_tickers(
    requested_universe: list[str],
    cik_map: dict[str, int],
    state: dict,
    as_of: date,
    refresh_after_days: int,
    force: bool = False,
    cache_missing_only: bool = False,
    cached_symbols: set[str] | None = None,
) -> list[str]:
    """Select refresh work deterministically while supporting cache resumes."""
    cached = {
        str(symbol).strip().upper()
        for symbol in (cached_symbols or set())
    }
    refresh_cutoff = pd.Timestamp(as_of) - pd.Timedelta(
        days=refresh_after_days
    )
    requested = []
    for ticker in requested_universe:
        if ticker not in cik_map:
            continue
        if cache_missing_only:
            cache_state = state.get(ticker) or {}
            cache_last_attempt = pd.to_datetime(
                cache_state.get("cache_last_attempt"),
                errors="coerce",
            )
            failure_reason = str(
                cache_state.get("cache_failure_reason") or ""
            )
            permanent_unavailable = (
                cache_state.get("cache_status")
                == "companyfacts_not_available"
                or "HTTP Error 404: Not Found" in failure_reason
            )
            if (
                ticker not in cached
                and (
                    force
                    or (
                        not permanent_unavailable
                        and (
                            pd.isna(cache_last_attempt)
                            or cache_last_attempt < refresh_cutoff
                        )
                    )
                )
            ):
                requested.append(ticker)
            continue
        last_attempt = pd.to_datetime(
            (state.get(ticker) or {}).get("last_attempt"), errors="coerce"
        )
        if force or pd.isna(last_attempt) or last_attempt < refresh_cutoff:
            requested.append(ticker)
    return requested


def classify_cache_refresh_backlog(
    requested_universe: list[str],
    eligible_before_limit: list[str],
    requested: list[str],
    cached_symbols: set[str],
) -> tuple[list[str], list[str]]:
    """Separate limit-deferred work from genuine refresh cooldowns."""
    requested_set = set(requested)
    eligible_set = set(eligible_before_limit)
    cached_set = {
        str(symbol).strip().upper() for symbol in cached_symbols
    }
    deferred = [
        ticker
        for ticker in eligible_before_limit
        if ticker not in requested_set
    ]
    cooldown = [
        ticker
        for ticker in requested_universe
        if (
            ticker not in eligible_set
            and ticker not in requested_set
            and ticker not in cached_set
        )
    ]
    return deferred, cooldown


def merge_fundamentals(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    combined = pd.concat([existing, incoming], ignore_index=True)
    if combined.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    for column in ("fiscal_end", "available_date", "fetched_at"):
        combined[column] = pd.to_datetime(combined[column], errors="coerce")
    combined["ticker"] = combined["ticker"].astype(str).str.upper()
    combined["value"] = pd.to_numeric(combined["value"], errors="coerce")
    combined = combined.dropna(subset=["ticker", "fiscal_end", "available_date", "metric", "value"])
    combined = combined.sort_values(
        "fetched_at", kind="stable"
    ).drop_duplicates(
        ["ticker", "fiscal_end", "available_date", "metric", "accession"], keep="last"
    )
    return combined[OUTPUT_COLUMNS].sort_values(
        [
            "ticker",
            "available_date",
            "fiscal_end",
            "metric",
            "accession",
            "taxonomy",
            "concept",
            "form",
            "fetched_at",
            "value",
        ],
        kind="stable",
        na_position="last",
    ).reset_index(drop=True)


def replace_ticker_fundamentals(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
    tickers: set[str],
) -> pd.DataFrame:
    """Replace complete ticker histories while preserving unrelated rows."""
    normalized = {str(ticker).strip().upper() for ticker in tickers}
    if existing.empty:
        retained = existing
    else:
        retained = existing.loc[
            ~existing["ticker"].astype(str).str.upper().isin(normalized)
        ]
    return merge_fundamentals(retained, incoming)


def integrate_refreshed_fundamentals(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
    tickers: set[str],
    *,
    non_destructive: bool,
) -> pd.DataFrame:
    """Apply parsed refresh rows under an explicit integration policy."""
    if not non_destructive:
        return replace_ticker_fundamentals(existing, incoming, tickers)
    if existing.empty or incoming.empty:
        return merge_fundamentals(existing, incoming)
    period_keys = ["ticker", "fiscal_end", "metric"]
    existing_keys = existing[period_keys].copy()
    incoming_keys = incoming[period_keys].copy()
    for frame in (existing_keys, incoming_keys):
        frame["ticker"] = frame["ticker"].astype(str).str.upper()
        frame["fiscal_end"] = pd.to_datetime(
            frame["fiscal_end"], errors="coerce"
        )
        frame["metric"] = frame["metric"].astype(str)
    incoming_periods = pd.MultiIndex.from_frame(
        incoming_keys.drop_duplicates()
    )
    existing_periods = pd.MultiIndex.from_frame(existing_keys)
    retained = existing.loc[~existing_periods.isin(incoming_periods)]
    return merge_fundamentals(retained, incoming)


def restore_uncovered_fundamental_periods(
    existing: pd.DataFrame,
    fallback: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Restore fallback rows only where no current ticker/period/metric exists."""
    if fallback.empty:
        return merge_fundamentals(existing, fallback), fallback.copy()
    period_keys = ["ticker", "fiscal_end", "metric"]
    existing_keys = existing[period_keys].copy()
    fallback_keys = fallback[period_keys].copy()
    for frame in (existing_keys, fallback_keys):
        frame["ticker"] = frame["ticker"].astype(str).str.upper()
        frame["fiscal_end"] = pd.to_datetime(
            frame["fiscal_end"], errors="coerce"
        )
        frame["metric"] = frame["metric"].astype(str)
    existing_periods = pd.MultiIndex.from_frame(
        existing_keys.drop_duplicates()
    )
    fallback_periods = pd.MultiIndex.from_frame(fallback_keys)
    restored = fallback.loc[~fallback_periods.isin(existing_periods)].copy()
    return merge_fundamentals(existing, restored), restored


def write_fundamentals_pair(
    annual: pd.DataFrame | None,
    output: Path,
    quarterly: pd.DataFrame | None,
    quarterly_output: Path,
) -> None:
    """Atomically replace every changed fundamentals CSV, with rollback."""
    targets = tuple(
        (path, frame)
        for path, frame in (
            (Path(output), annual),
            (Path(quarterly_output), quarterly),
        )
        if frame is not None
    )
    if not targets:
        raise ValueError("At least one fundamentals output must be provided")
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path | None] = {}
    replaced: list[Path] = []
    try:
        for target, frame in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            staged[target] = temporary
            frame.to_csv(temporary, index=False)
        for target, _frame in targets:
            if not target.exists():
                backups[target] = None
                continue
            backup = target.with_name(
                f".{target.name}.bak.{os.getpid()}.{time.time_ns()}"
            )
            try:
                os.link(target, backup)
            except OSError:
                shutil.copy2(target, backup)
            backups[target] = backup
        for target, _frame in targets:
            os.replace(staged[target], target)
            replaced.append(target)
    except Exception:
        rollback_errors = []
        for target in reversed(replaced):
            backup = backups.get(target)
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    os.replace(backup, target)
            except Exception as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        if rollback_errors:
            raise RuntimeError(
                "Fundamentals output write failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            )
        raise
    finally:
        for path in (*staged.values(), *(p for p in backups.values() if p)):
            Path(path).unlink(missing_ok=True)


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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


LEGACY_COMPANYFACTS_FULL_REBUILD_SCOPE_FORMAT_VERSION = 1
COMPANYFACTS_FULL_REBUILD_SCOPE_FORMAT_VERSION = 2
COMPANYFACTS_FULL_REBUILD_RECIPE_FORMAT_VERSION = 1


def _canonical_json_sha256(value: object) -> str:
    """Hash a JSON-compatible value with a stable serialization."""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def companyfacts_full_rebuild_recipe() -> dict:
    """Describe every local parser/runtime input for a full replacement.

    The raw-cache manifest alone is insufficient provenance: a future change
    to the parser or its dataframe runtime can produce different formal rows
    from byte-identical SEC payloads.  The recipe is deliberately compact and
    clone-independent, while still causing a full rebuild to stop when either
    the parser source, output schema, or serialization runtime changes.
    """
    foreign_parser = (
        Path(__file__).resolve().parents[1]
        / "research/foreign_quarterly_diagnostics.py"
    )
    registry_path = VALIDATED_FOREIGN_QUARTERLY_FILE
    return {
        "format_version": COMPANYFACTS_FULL_REBUILD_RECIPE_FORMAT_VERSION,
        "parser_module": "src/io/fundamentals_update.py",
        "parser_sha256": _file_sha256(Path(__file__)),
        "foreign_quarterly_parser": {
            "path": "src/research/foreign_quarterly_diagnostics.py",
            "sha256": _file_sha256(foreign_parser),
        },
        "validated_foreign_quarterly_registry": {
            "path": "stocks_list_dir/nasdaq/validated_foreign_quarterly.csv",
            "sha256": (
                _file_sha256(registry_path) if registry_path.is_file() else None
            ),
        },
        "output_columns_sha256": _canonical_json_sha256(OUTPUT_COLUMNS),
        "parser_options": {
            "include_validated_foreign_quarters": False,
            "replace_complete_outputs": True,
            "scope_bound_tickers_only": True,
        },
        "runtime": {
            "python_implementation": sys.implementation.name,
            "python_version": list(sys.version_info[:3]),
            "pandas_version": pd.__version__,
        },
    }


def companyfacts_full_rebuild_recipe_sha256(recipe: dict | None = None) -> str:
    """Return the content hash for a full-rebuild parser recipe."""
    if recipe is None:
        recipe = companyfacts_full_rebuild_recipe()
    if not isinstance(recipe, dict):
        raise ValueError("Company Facts full-rebuild recipe must be an object")
    return _canonical_json_sha256(recipe)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_companyfacts_full_rebuild_provenance_inputs(
    expected_cache_manifest_sha256: str | None,
    expected_rebuild_recipe_sha256: str | None,
) -> None:
    """Require raw-snapshot and parser-recipe bindings as one provenance pair."""
    if (expected_cache_manifest_sha256 is None) != (
        expected_rebuild_recipe_sha256 is None
    ):
        raise ValueError(
            "immutable Company Facts full rebuild requires both cache manifest "
            "and parser recipe SHA-256 values"
        )
    if (
        expected_cache_manifest_sha256 is not None
        and not _is_sha256(expected_cache_manifest_sha256)
    ):
        raise ValueError("Company Facts full-rebuild manifest SHA is invalid")


def _validate_companyfacts_full_rebuild_recipe_record(
    recipe: object,
    recipe_sha256: object,
) -> None:
    """Validate a recorded recipe without requiring it to match this runtime."""
    if not isinstance(recipe, dict):
        raise ValueError("Company Facts full-rebuild scope recipe is invalid")
    required = {
        "format_version",
        "parser_module",
        "parser_sha256",
        "foreign_quarterly_parser",
        "validated_foreign_quarterly_registry",
        "output_columns_sha256",
        "parser_options",
        "runtime",
    }
    if set(recipe) != required:
        raise ValueError("Company Facts full-rebuild scope recipe has invalid fields")
    if recipe["format_version"] != COMPANYFACTS_FULL_REBUILD_RECIPE_FORMAT_VERSION:
        raise ValueError("unsupported Company Facts full-rebuild recipe format")
    if recipe["parser_module"] != "src/io/fundamentals_update.py":
        raise ValueError("Company Facts full-rebuild scope recipe parser is invalid")
    if not _is_sha256(recipe["parser_sha256"]):
        raise ValueError("Company Facts full-rebuild scope recipe parser hash is invalid")
    foreign_parser = recipe["foreign_quarterly_parser"]
    if (
        not isinstance(foreign_parser, dict)
        or set(foreign_parser) != {"path", "sha256"}
        or foreign_parser["path"] != "src/research/foreign_quarterly_diagnostics.py"
        or not _is_sha256(foreign_parser["sha256"])
    ):
        raise ValueError(
            "Company Facts full-rebuild scope recipe foreign parser is invalid"
        )
    registry = recipe["validated_foreign_quarterly_registry"]
    if (
        not isinstance(registry, dict)
        or set(registry) != {"path", "sha256"}
        or registry["path"]
        != "stocks_list_dir/nasdaq/validated_foreign_quarterly.csv"
        or (registry["sha256"] is not None and not _is_sha256(registry["sha256"]))
    ):
        raise ValueError(
            "Company Facts full-rebuild scope recipe foreign registry is invalid"
        )
    if not _is_sha256(recipe["output_columns_sha256"]):
        raise ValueError("Company Facts full-rebuild scope recipe schema hash is invalid")
    if recipe["parser_options"] != {
        "include_validated_foreign_quarters": False,
        "replace_complete_outputs": True,
        "scope_bound_tickers_only": True,
    }:
        raise ValueError("Company Facts full-rebuild scope recipe options are invalid")
    runtime = recipe["runtime"]
    if (
        not isinstance(runtime, dict)
        or set(runtime) != {
            "python_implementation",
            "python_version",
            "pandas_version",
        }
        or not isinstance(runtime["python_implementation"], str)
        or not runtime["python_implementation"]
        or not isinstance(runtime["python_version"], list)
        or len(runtime["python_version"]) != 3
        or not all(isinstance(part, int) and part >= 0 for part in runtime["python_version"])
        or not isinstance(runtime["pandas_version"], str)
        or not runtime["pandas_version"]
    ):
        raise ValueError("Company Facts full-rebuild scope recipe runtime is invalid")
    if not _is_sha256(recipe_sha256):
        raise ValueError("Company Facts full-rebuild scope recipe hash is invalid")
    if recipe_sha256 != companyfacts_full_rebuild_recipe_sha256(recipe):
        raise ValueError("Company Facts full-rebuild scope recipe hash does not match")


def verify_companyfacts_full_rebuild_recipe(
    expected_sha256: str | None = None,
) -> dict:
    """Return the live recipe and reject an immutable scope mismatch."""
    if expected_sha256 is not None and not _is_sha256(expected_sha256):
        raise ValueError("Company Facts full-rebuild recipe SHA is invalid")
    recipe = companyfacts_full_rebuild_recipe()
    actual_sha256 = companyfacts_full_rebuild_recipe_sha256(recipe)
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise ValueError(
            "Company Facts full-rebuild parser recipe does not match the "
            "declared immutable scope"
        )
    return {
        "recipe": recipe,
        "recipe_sha256": actual_sha256,
        "declared_recipe_sha256": expected_sha256,
        "recipe_matched": (
            expected_sha256 is None or actual_sha256 == expected_sha256
        ),
    }


def companyfacts_full_rebuild_symbol_sha256(
    symbols: list[str] | set[str] | tuple[str, ...],
) -> str:
    """Hash a canonical, explicit Company Facts full-rebuild ticker scope."""
    normalized = sorted({
        str(symbol).strip().upper()
        for symbol in symbols
        if str(symbol).strip()
    })
    payload = ("\n".join(normalized) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_companyfacts_full_rebuild_scope(path: str | Path) -> dict:
    """Load a content-bound, explicit ticker scope for a full cache rebuild.

    A full rebuild must never infer its protected ticker set from a mutable
    current-universe file.  This validates a research-provenance scope that
    records the exact ordered set and the raw-cache snapshot manifest it was
    bound to.
    """
    scope_path = Path(path)
    try:
        scope = json.loads(scope_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Unable to read Company Facts full-rebuild scope {scope_path}: {exc}"
        ) from exc
    if not isinstance(scope, dict):
        raise ValueError("Company Facts full-rebuild scope must be an object")
    format_version = scope.get("format_version")
    if format_version not in {
        LEGACY_COMPANYFACTS_FULL_REBUILD_SCOPE_FORMAT_VERSION,
        COMPANYFACTS_FULL_REBUILD_SCOPE_FORMAT_VERSION,
    }:
        raise ValueError("unsupported Company Facts full-rebuild scope format")
    required = {
        "format_version",
        "research_only",
        "snapshot",
        "formal_outputs",
        "required_symbols",
        "required_symbol_count",
        "required_symbols_sha256",
    }
    if format_version == COMPANYFACTS_FULL_REBUILD_SCOPE_FORMAT_VERSION:
        required.update({"rebuild_recipe", "rebuild_recipe_sha256"})
    missing = sorted(required - set(scope))
    if missing:
        raise ValueError(
            "Company Facts full-rebuild scope is missing fields: "
            + ", ".join(missing)
        )
    if scope["research_only"] is not True:
        raise ValueError(
            "Company Facts full-rebuild scope must be explicitly research-only"
        )
    snapshot = scope["snapshot"]
    if not isinstance(snapshot, dict):
        raise ValueError("Company Facts full-rebuild scope snapshot is invalid")
    manifest_sha256 = snapshot.get("cache_manifest_sha256")
    if (
        not isinstance(manifest_sha256, str)
        or len(manifest_sha256) != 64
        or any(character not in "0123456789abcdef" for character in manifest_sha256)
    ):
        raise ValueError(
            "Company Facts full-rebuild scope has an invalid snapshot manifest SHA"
        )
    if not isinstance(snapshot.get("snapshot_id"), str) or not snapshot["snapshot_id"]:
        raise ValueError(
            "Company Facts full-rebuild scope has an invalid snapshot id"
        )
    formal_outputs = scope["formal_outputs"]
    if not isinstance(formal_outputs, dict) or set(formal_outputs) != {
        "annual", "quarterly"
    }:
        raise ValueError(
            "Company Facts full-rebuild scope must describe annual and quarterly outputs"
        )
    for output_name, output_entry in formal_outputs.items():
        if not isinstance(output_entry, dict):
            raise ValueError(
                f"Company Facts full-rebuild scope {output_name} output is invalid"
            )
        output_sha256 = output_entry.get("sha256")
        if (
            not isinstance(output_entry.get("path"), str)
            or not isinstance(output_sha256, str)
            or len(output_sha256) != 64
            or any(character not in "0123456789abcdef" for character in output_sha256)
        ):
            raise ValueError(
                f"Company Facts full-rebuild scope {output_name} output is invalid"
            )
    raw_symbols = scope["required_symbols"]
    if not isinstance(raw_symbols, list) or not raw_symbols:
        raise ValueError(
            "Company Facts full-rebuild scope must contain at least one ticker"
        )
    normalized = [str(symbol).strip().upper() for symbol in raw_symbols]
    if any(not symbol for symbol in normalized):
        raise ValueError("Company Facts full-rebuild scope contains blank tickers")
    if normalized != sorted(set(normalized)):
        raise ValueError(
            "Company Facts full-rebuild scope tickers must be sorted and unique"
        )
    if scope["required_symbol_count"] != len(normalized):
        raise ValueError(
            "Company Facts full-rebuild scope ticker count does not match its list"
        )
    if scope["required_symbols_sha256"] != companyfacts_full_rebuild_symbol_sha256(
        normalized
    ):
        raise ValueError(
            "Company Facts full-rebuild scope ticker hash does not match its list"
        )
    recipe_bound = format_version == COMPANYFACTS_FULL_REBUILD_SCOPE_FORMAT_VERSION
    if recipe_bound:
        _validate_companyfacts_full_rebuild_recipe_record(
            scope["rebuild_recipe"],
            scope["rebuild_recipe_sha256"],
        )
    return {
        **scope,
        "scope_path": str(scope_path),
        "required_symbols": normalized,
        "rebuild_recipe": scope.get("rebuild_recipe"),
        "rebuild_recipe_sha256": scope.get("rebuild_recipe_sha256"),
        "rebuild_recipe_bound": recipe_bound,
    }


def load_companyfacts_full_rebuild_inputs(
    snapshot_dir: str | Path,
    scope_path: str | Path,
) -> dict:
    """Bind an explicit full-rebuild scope to one immutable cache snapshot."""
    snapshot = Path(snapshot_dir)
    metadata_path = snapshot / "snapshot.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(
            f"missing immutable Company Facts snapshot metadata: {metadata_path}"
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Unable to read immutable Company Facts snapshot metadata {metadata_path}: {exc}"
        ) from exc
    if not isinstance(metadata, dict):
        raise ValueError("immutable Company Facts snapshot metadata must be an object")
    required_snapshot_fields = {
        "format_version",
        "snapshot_id",
        "cache_manifest",
        "cache_manifest_sha256",
        "referenced_file_count",
        "referenced_files",
        "storage_method",
        "research_only",
    }
    missing_snapshot_fields = sorted(required_snapshot_fields - set(metadata))
    if missing_snapshot_fields:
        raise ValueError(
            "immutable Company Facts snapshot metadata is missing fields: "
            + ", ".join(missing_snapshot_fields)
        )
    if metadata["format_version"] != 1:
        raise ValueError("unsupported immutable Company Facts snapshot format")
    if metadata["cache_manifest"] != "manifest.json":
        raise ValueError(
            "immutable Company Facts snapshot must reference manifest.json"
        )
    if metadata["storage_method"] not in {"hardlink", "copy"}:
        raise ValueError(
            "immutable Company Facts snapshot must use copied or legacy hard-linked raw files"
        )
    if metadata["research_only"] is not True:
        raise ValueError(
            "immutable Company Facts snapshot must remain explicitly research-only"
        )
    if (
        not isinstance(metadata["referenced_files"], dict)
        or not metadata["referenced_files"]
        or metadata["referenced_file_count"] != len(metadata["referenced_files"])
    ):
        raise ValueError(
            "immutable Company Facts snapshot has inconsistent referenced files"
        )
    manifest_sha256 = metadata.get("cache_manifest_sha256")
    if (
        not isinstance(manifest_sha256, str)
        or len(manifest_sha256) != 64
        or any(character not in "0123456789abcdef" for character in manifest_sha256)
    ):
        raise ValueError("immutable Company Facts snapshot has an invalid manifest SHA")
    manifest_path = snapshot / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"immutable Company Facts snapshot is missing manifest: {manifest_path}"
        )
    if _file_sha256(manifest_path) != manifest_sha256:
        raise ValueError(
            "immutable Company Facts snapshot manifest hash does not match metadata"
        )
    scope = load_companyfacts_full_rebuild_scope(scope_path)
    scope_snapshot = scope["snapshot"]
    if (
        not isinstance(metadata.get("snapshot_id"), str)
        or not metadata["snapshot_id"]
        or scope_snapshot["snapshot_id"] != metadata.get("snapshot_id")
        or scope_snapshot["cache_manifest_sha256"] != manifest_sha256
    ):
        raise ValueError(
            "Company Facts full-rebuild scope is not bound to this immutable snapshot"
        )
    return {
        "cache_dir": snapshot,
        "required_symbols": scope["required_symbols"],
        "cache_manifest_sha256": manifest_sha256,
        "snapshot_id": metadata["snapshot_id"],
        "scope": scope,
        "rebuild_recipe": scope["rebuild_recipe"],
        "rebuild_recipe_sha256": scope["rebuild_recipe_sha256"],
        "rebuild_recipe_bound": scope["rebuild_recipe_bound"],
    }


def load_refresh_priority_file(path: Path) -> dict:
    """Load and fingerprint a deterministic ticker refresh priority list."""
    path = Path(path)
    try:
        frame = pd.read_csv(path)
    except (OSError, pd.errors.ParserError) as exc:
        raise ValueError(
            f"Unable to read cache priority file {path}: {exc}"
        ) from exc
    if "ticker" not in frame.columns:
        raise ValueError(
            f"Cache priority file {path} must contain a ticker column"
        )
    tickers = frame["ticker"].fillna("").astype(str).str.strip().str.upper()
    if (tickers == "").any():
        raise ValueError(f"Cache priority file {path} contains blank tickers")
    if tickers.duplicated().any():
        duplicates = sorted(tickers.loc[tickers.duplicated()].unique())
        raise ValueError(
            f"Cache priority file {path} contains duplicate tickers: "
            + ", ".join(duplicates)
        )
    ordered = frame.assign(_ticker=tickers, _row=range(len(frame)))
    ordering = "file_order"
    rank_column = next(
        (
            column
            for column in (
                "fetch_priority_rank",
                "cache_refresh_priority_rank",
                "priority_rank",
            )
            if column in frame.columns
        ),
        None,
    )
    if rank_column:
        ranks = pd.to_numeric(frame[rank_column], errors="coerce")
        if rank_column == "fetch_priority_rank":
            eligible = ranks.notna()
            if not eligible.any():
                raise ValueError(
                    f"Cache priority file {path} has no fetch-eligible rows"
                )
            ordered = ordered.loc[eligible].copy()
            ranks = ranks.loc[eligible]
        finite = ranks.map(
            lambda value: (
                pd.notna(value)
                and float("-inf") < float(value) < float("inf")
            )
        )
        if not finite.all():
            raise ValueError(
                f"Cache priority file {path} has invalid {rank_column} values"
            )
        ordered = ordered.assign(_rank=ranks).sort_values(
            ["_rank", "_row"], kind="stable"
        )
        ordering = f"{rank_column}_then_file_order"
    return {
        "path": str(path),
        "sha256": _file_sha256(path),
        "ticker_count": len(ordered),
        "ordering": ordering,
        "tickers": ordered["_ticker"].tolist(),
    }


def load_reparse_priority_file(path: Path) -> dict:
    """Load only ranked offline-reparse candidates from a diagnostics CSV."""
    path = Path(path)
    try:
        frame = pd.read_csv(path)
    except (OSError, pd.errors.ParserError) as exc:
        raise ValueError(
            f"Unable to read reparse priority file {path}: {exc}"
        ) from exc
    required = {"ticker", "reparse_priority_rank"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(
            f"Reparse priority file {path} is missing: {', '.join(missing)}"
        )
    tickers = frame["ticker"].fillna("").astype(str).str.strip().str.upper()
    if (tickers == "").any() or tickers.duplicated().any():
        raise ValueError(
            f"Reparse priority file {path} has blank or duplicate tickers"
        )
    ranks = pd.to_numeric(
        frame["reparse_priority_rank"], errors="coerce"
    )
    eligible = ranks.notna()
    if not eligible.any():
        raise ValueError(
            f"Reparse priority file {path} has no reparse-eligible rows"
        )
    eligible_ranks = ranks.loc[eligible]
    if not eligible_ranks.map(
        lambda value: float("-inf") < float(value) < float("inf")
    ).all():
        raise ValueError(
            f"Reparse priority file {path} has invalid ranks"
        )
    ordered = (
        frame.assign(
            _ticker=tickers,
            _rank=ranks,
            _row=range(len(frame)),
        )
        .loc[eligible]
        .sort_values(["_rank", "_row"], kind="stable")
    )
    result = ordered["_ticker"].tolist()
    return {
        "path": str(path),
        "sha256": _file_sha256(path),
        "tickers": result,
        "ticker_count": len(result),
        "ordering": "reparse_priority_rank_then_file_order",
    }


def fetch_sec_ticker_map_snapshot(cache_dir: Path) -> tuple[dict[str, int], dict]:
    """Fetch and content-address the exact normalized SEC ticker map used."""
    mapping = {
        str(ticker).upper(): int(cik)
        for ticker, cik in fetch_sec_ticker_map().items()
    }
    canonical = json.dumps(
        mapping, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    mapping_sha256 = hashlib.sha256(canonical).hexdigest()
    snapshot_dir = Path(cache_dir) / "ticker_maps"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / f"ticker_map_{mapping_sha256}.json.gz"
    if not path.exists():
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(
            gzip.compress(canonical, compresslevel=6, mtime=0)
        )
        os.replace(temporary, path)
    return mapping, {
        "path": str(path),
        "relative_path": str(path.relative_to(cache_dir)),
        "mapping_sha256": mapping_sha256,
        "compressed_sha256": _file_sha256(path),
        "symbol_count": len(mapping),
        "source_url": SEC_TICKERS_API,
    }


def historical_ticker_cik_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / "historical_ticker_ciks.json"


def _historical_ticker_cik_entries(cache_dir: Path) -> dict[str, dict]:
    """Load and validate the manifest-bound historical CIK registry."""
    path = historical_ticker_cik_path(cache_dir)
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("format_version") != 1:
        raise RuntimeError(f"Unsupported historical CIK cache {path}")
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, dict):
        raise RuntimeError(f"Invalid historical CIK cache {path}")
    entries = {}
    for ticker, raw_entry in raw_entries.items():
        normalized = str(ticker).strip().upper()
        cik = raw_entry.get("cik") if isinstance(raw_entry, dict) else None
        predecessors = (
            raw_entry.get("predecessor_ciks", [])
            if isinstance(raw_entry, dict) else None
        )
        if (
            not normalized
            or not isinstance(cik, int)
            or cik <= 0
            or not isinstance(predecessors, list)
            or any(
                not isinstance(predecessor, int) or predecessor <= 0
                for predecessor in predecessors
            )
            or len(set(predecessors)) != len(predecessors)
            or cik in predecessors
        ):
            raise RuntimeError(f"Invalid historical CIK entry for {ticker}")
        if predecessors and not str(raw_entry.get("source_url") or "").strip():
            raise RuntimeError(
                f"Historical CIK transition for {ticker} requires source_url"
            )
        entries[normalized] = {
            **raw_entry,
            "cik": cik,
            "predecessor_ciks": predecessors,
        }
    return entries


def load_historical_ticker_ciks(cache_dir: Path) -> dict[str, int]:
    """Load previously resolved SEC historical ticker mappings."""
    return {
        ticker: int(entry["cik"])
        for ticker, entry in _historical_ticker_cik_entries(cache_dir).items()
    }


def historical_ticker_cik_chains(cache_dir: Path) -> dict[str, tuple[int, ...]]:
    """Return current CIK plus sourced predecessor CIKs for each ticker."""
    return {
        ticker: tuple([int(entry["cik"]), *entry["predecessor_ciks"]])
        for ticker, entry in _historical_ticker_cik_entries(cache_dir).items()
    }


def resolve_historical_ticker_ciks(
    tickers: list[str],
    cache_dir: Path = SEC_COMPANYFACTS_CACHE_DIR,
    retries: int = 3,
) -> dict:
    """Resolve old tickers through SEC Atom company lookup and cache results."""
    cache_dir = Path(cache_dir)
    path = historical_ticker_cik_path(cache_dir)
    existing_document = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists() else {"format_version": 1, "entries": {}}
    )
    if existing_document.get("format_version") != 1:
        raise RuntimeError(f"Unsupported historical CIK cache {path}")
    entries = dict(existing_document.get("entries") or {})
    requested = list(dict.fromkeys(
        str(ticker).strip().upper()
        for ticker in tickers
        if str(ticker).strip()
    ))
    resolved = {}
    failures = []
    for ticker in requested:
        cached = entries.get(ticker)
        if isinstance(cached, dict) and isinstance(cached.get("cik"), int):
            resolved[ticker] = int(cached["cik"])
            continue
        source_url = SEC_COMPANY_BROWSE_API.format(ticker=ticker)
        error = None
        for attempt in range(retries):
            try:
                request = Request(source_url, headers=SEC_HEADERS)
                with urlopen(request, timeout=45) as response:
                    root = ET.fromstring(response.read())
                cik_node = root.find(".//{*}company-info/{*}cik")
                name_node = root.find(
                    ".//{*}company-info/{*}conformed-name"
                )
                if cik_node is None or not (cik_node.text or "").isdigit():
                    raise RuntimeError("SEC returned no company CIK")
                cik = int(cik_node.text)
                entries[ticker] = {
                    "cik": cik,
                    "conformed_name": (
                        (name_node.text or "").strip()
                        if name_node is not None else ""
                    ),
                    "source_url": source_url,
                    "resolved_at": pd.Timestamp.now(tz="UTC").isoformat(),
                }
                resolved[ticker] = cik
                break
            except Exception as exc:
                error = exc
                if attempt + 1 < retries:
                    time.sleep((2**attempt) + random.random())
        if ticker not in resolved:
            failures.append({"ticker": ticker, "reason": str(error)})
        time.sleep(0.12)
    document = {
        "format_version": 1,
        "entries": {
            ticker: entries[ticker] for ticker in sorted(entries)
        },
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    if (cache_dir / "manifest.json").exists():
        write_companyfacts_cache_manifest(cache_dir)
    return {
        "requested_tickers": requested,
        "resolved": resolved,
        "resolved_count": len(resolved),
        "failures": failures,
        "cache_path": str(path),
        "cache_sha256": _file_sha256(path),
    }


def write_companyfacts_cache_manifest(
    cache_dir: Path,
    changed_payload_paths: set[str] | None = None,
) -> Path:
    """Fingerprint every raw CIK payload used for deterministic reparsing."""
    cache_dir = Path(cache_dir)
    reusable_entries: dict[str, dict] = {}
    if changed_payload_paths is not None:
        try:
            prior = json.loads(
                (cache_dir / "manifest.json").read_text(encoding="utf-8")
            )
            if prior.get("format_version") == 1:
                reusable_entries = {
                    str(entry["path"]): entry
                    for entry in prior.get("entries", [])
                    if isinstance(entry, dict) and entry.get("path")
                }
        except (OSError, json.JSONDecodeError, TypeError, KeyError):
            reusable_entries = {}
    entries = []
    for path in _companyfacts_cache_files(cache_dir):
        if (
            changed_payload_paths is not None
            and path.name not in changed_payload_paths
            and path.name in reusable_entries
            and reusable_entries[path.name].get("bytes") == path.stat().st_size
        ):
            entries.append(dict(reusable_entries[path.name]))
            continue
        envelope = _read_companyfacts_cache_envelope(path)
        entries.append({
            "path": path.name,
            "cik": int(envelope["cik"]),
            "symbols": sorted(map(str, envelope.get("symbols", []))),
            "fetched_at": envelope["fetched_at"],
            "source_url": envelope["source_url"],
            "bytes": path.stat().st_size,
            "sha256": _file_sha256(path),
        })
    ticker_map_entries = []
    ticker_map_dir = cache_dir / "ticker_maps"
    for path in sorted(ticker_map_dir.glob("ticker_map_*.json.gz")):
        with gzip.open(path, "rb") as handle:
            canonical = handle.read()
        mapping = json.loads(canonical)
        ticker_map_entries.append({
            "path": str(path.relative_to(cache_dir)),
            "mapping_sha256": hashlib.sha256(canonical).hexdigest(),
            "symbol_count": len(mapping),
            "source_url": SEC_TICKERS_API,
            "bytes": path.stat().st_size,
            "sha256": _file_sha256(path),
        })
    historical_path = historical_ticker_cik_path(cache_dir)
    historical_entry = (
        {
            "path": historical_path.name,
            "bytes": historical_path.stat().st_size,
            "sha256": _file_sha256(historical_path),
        }
        if historical_path.exists() else None
    )
    raw_state_path = raw_cache_refresh_state_path(cache_dir)
    raw_state_entry = (
        {
            "path": raw_state_path.name,
            "bytes": raw_state_path.stat().st_size,
            "sha256": _file_sha256(raw_state_path),
        }
        if raw_state_path.exists() else None
    )
    manifest = {
        "format_version": 1,
        "cache_dir": str(cache_dir),
        "entry_count": len(entries),
        "entries": entries,
        "ticker_map_entry_count": len(ticker_map_entries),
        "ticker_map_entries": ticker_map_entries,
        "historical_ticker_cik_entry": historical_entry,
        "raw_cache_refresh_state_entry": raw_state_entry,
    }
    path = cache_dir / "manifest.json"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return path


def verify_companyfacts_cache_manifest(
    cache_dir: Path,
    payload_paths: set[str] | None = None,
) -> dict:
    """Verify the cache inventory and all payloads used by the caller.

    ``payload_paths=None`` performs the original full-byte verification.
    Incremental reparses may pass their exact CIK payload names: the complete
    inventory and manifest structure are still checked, while unrelated raw
    payloads are not rehashed on every small batch.
    """
    cache_dir = Path(cache_dir)
    manifest_path = cache_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Company Facts cache manifest unavailable or invalid: {exc}"
        ) from exc
    if (
        manifest.get("format_version") != 1
        or not isinstance(manifest.get("entries"), list)
    ):
        raise RuntimeError("Company Facts cache manifest has an invalid format")
    entries = manifest["entries"]
    ticker_map_entries = manifest.get("ticker_map_entries", [])
    historical_entry = manifest.get("historical_ticker_cik_entry")
    raw_state_entry = manifest.get("raw_cache_refresh_state_entry")
    if not isinstance(ticker_map_entries, list):
        raise RuntimeError(
            "Company Facts cache manifest ticker_map_entries is invalid"
        )
    historical_path = historical_ticker_cik_path(cache_dir)
    if historical_path.exists() != (historical_entry is not None):
        raise RuntimeError(
            "Company Facts historical ticker CIK inventory mismatch"
        )
    if historical_entry is not None:
        if (
            not isinstance(historical_entry, dict)
            or historical_entry.get("path") != historical_path.name
            or historical_entry.get("bytes") != historical_path.stat().st_size
            or historical_entry.get("sha256") != _file_sha256(historical_path)
        ):
            raise RuntimeError(
                "Company Facts historical ticker CIK integrity mismatch"
            )
    raw_state_path = raw_cache_refresh_state_path(cache_dir)
    if raw_state_path.exists() != (raw_state_entry is not None):
        raise RuntimeError(
            "Company Facts raw-cache state inventory mismatch"
        )
    if raw_state_entry is not None:
        if (
            not isinstance(raw_state_entry, dict)
            or raw_state_entry.get("path") != raw_state_path.name
            or raw_state_entry.get("bytes") != raw_state_path.stat().st_size
            or raw_state_entry.get("sha256") != _file_sha256(raw_state_path)
        ):
            raise RuntimeError(
                "Company Facts raw-cache state integrity mismatch"
            )
    manifest_paths = [entry.get("path") for entry in entries]
    if (
        len(manifest_paths) != len(set(manifest_paths))
        or any(
            not isinstance(path, str)
            or Path(path).name != path
            or not path.startswith("CIK")
            or not (
                path.endswith(".json")
                or path.endswith(".json.gz")
            )
            for path in manifest_paths
        )
    ):
        raise RuntimeError(
            "Company Facts cache manifest has duplicate or unsafe paths"
        )
    actual_paths = [path.name for path in _companyfacts_cache_files(cache_dir)]
    if sorted(manifest_paths) != actual_paths:
        missing = sorted(set(manifest_paths) - set(actual_paths))
        unrecorded = sorted(set(actual_paths) - set(manifest_paths))
        raise RuntimeError(
            "Company Facts cache manifest inventory mismatch: "
            f"missing={missing}, unrecorded={unrecorded}"
        )
    if payload_paths is None:
        verified_payload_paths = set(manifest_paths)
        verification_scope = "full"
    else:
        verified_payload_paths = {
            str(path) for path in payload_paths
        }
        unknown_paths = sorted(
            verified_payload_paths - set(manifest_paths)
        )
        if unknown_paths:
            raise RuntimeError(
                "Company Facts cache manifest lacks requested payloads: "
                + ", ".join(unknown_paths)
            )
        verification_scope = "selected_payloads"
    for entry in entries:
        if entry["path"] not in verified_payload_paths:
            continue
        path = cache_dir / entry["path"]
        actual_bytes = path.stat().st_size
        actual_sha256 = _file_sha256(path)
        if (
            entry.get("bytes") != actual_bytes
            or entry.get("sha256") != actual_sha256
        ):
            raise RuntimeError(
                "Company Facts cache integrity mismatch for "
                f"{entry['path']}: expected bytes={entry.get('bytes')} "
                f"sha256={entry.get('sha256')}, actual bytes={actual_bytes} "
                f"sha256={actual_sha256}"
            )
    ticker_map_paths = [entry.get("path") for entry in ticker_map_entries]
    if (
        len(ticker_map_paths) != len(set(ticker_map_paths))
        or any(
            not isinstance(path, str)
            or Path(path).parts != ("ticker_maps", Path(path).name)
            or not Path(path).name.startswith("ticker_map_")
            or not Path(path).name.endswith(".json.gz")
            for path in ticker_map_paths
        )
    ):
        raise RuntimeError(
            "Company Facts cache manifest has unsafe ticker map paths"
        )
    actual_ticker_map_paths = sorted(
        str(path.relative_to(cache_dir))
        for path in (cache_dir / "ticker_maps").glob("ticker_map_*.json.gz")
    )
    if sorted(ticker_map_paths) != actual_ticker_map_paths:
        raise RuntimeError(
            "Company Facts cache ticker map inventory mismatch"
        )
    for entry in ticker_map_entries:
        path = cache_dir / entry["path"]
        if (
            entry.get("bytes") != path.stat().st_size
            or entry.get("sha256") != _file_sha256(path)
        ):
            raise RuntimeError(
                f"Company Facts ticker map integrity mismatch for {entry['path']}"
            )
        with gzip.open(path, "rb") as handle:
            canonical = handle.read()
        if entry.get("mapping_sha256") != hashlib.sha256(canonical).hexdigest():
            raise RuntimeError(
                f"Company Facts ticker map content mismatch for {entry['path']}"
            )
    if manifest.get("entry_count") != len(entries):
        raise RuntimeError(
            "Company Facts cache manifest entry_count does not match entries"
        )
    if manifest.get("ticker_map_entry_count", 0) != len(ticker_map_entries):
        raise RuntimeError(
            "Company Facts cache manifest ticker_map_entry_count is invalid"
        )
    return {
        "manifest": str(manifest_path),
        "entry_count": len(entries),
        "verified_payload_count": len(verified_payload_paths),
        "verification_scope": verification_scope,
        "ticker_map_entry_count": len(ticker_map_entries),
        "verified": True,
    }


def cached_companyfacts_symbols(cache_dir: Path) -> set[str]:
    cache_dir = Path(cache_dir)
    manifest_path = cache_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        actual_paths = {
            path.name for path in _companyfacts_cache_files(cache_dir)
        }
        entries = manifest.get("entries")
        if (
            manifest.get("format_version") == 1
            and isinstance(entries, list)
            and {entry.get("path") for entry in entries} == actual_paths
        ):
            return {
                str(symbol).upper()
                for entry in entries
                for symbol in entry.get("symbols", [])
                if str(symbol).strip()
            }
    except (OSError, json.JSONDecodeError, RuntimeError):
        pass
    symbols = set()
    for path in _companyfacts_cache_files(cache_dir):
        envelope = _read_companyfacts_cache_envelope(path)
        symbols.update(
            str(symbol).upper()
            for symbol in envelope.get("symbols", [])
            if str(symbol).strip()
        )
    return symbols


def classify_companyfacts_payload(payload: dict) -> dict:
    """Summarize whether a raw SEC payload can plausibly supply US quarters."""
    facts = payload.get("facts") or {}
    namespaces = sorted(map(str, facts))
    forms = set()
    units = set()
    concept_count = 0
    for namespace in facts.values():
        if not isinstance(namespace, dict):
            continue
        for fact in namespace.values():
            concept_count += 1
            for unit, rows in (fact.get("units") or {}).items():
                units.add(str(unit))
                for row in rows:
                    if row.get("form"):
                        forms.add(str(row["form"]))
    has_us_gaap = "us-gaap" in facts
    has_ifrs = "ifrs-full" in facts
    has_10q = bool(forms & {"10-Q", "10-Q/A"})
    has_foreign_periodic = bool(
        forms & {"20-F", "20-F/A", "40-F", "40-F/A", "6-K"}
    )
    us_gaap = facts.get("us-gaap") or {}
    direct_revenue_concepts = sorted(
        set(METRIC_CONCEPTS["revenue"]) & set(us_gaap)
    )
    bank_net_interest_concepts = sorted(
        set(BANK_NET_INTEREST_CONCEPTS) & set(us_gaap)
    )
    bank_noninterest_concepts = sorted(
        set(BANK_NONINTEREST_CONCEPTS) & set(us_gaap)
    )
    has_bank_revenue_components = bool(
        bank_net_interest_concepts and bank_noninterest_concepts
    )
    if not concept_count:
        profile = "NO_FACTS"
    elif has_10q and has_us_gaap:
        profile = "US_GAAP_WITH_10Q"
    elif has_foreign_periodic and not has_10q:
        profile = "FOREIGN_PERIODIC_NO_10Q"
    elif has_us_gaap:
        profile = "US_GAAP_NO_10Q"
    elif has_ifrs:
        profile = "IFRS_WITHOUT_SUPPORTED_QUARTERS"
    else:
        profile = "OTHER_TAXONOMY"
    return {
        "profile": profile,
        "taxonomy_namespaces": namespaces,
        "forms": sorted(forms),
        "units": sorted(units),
        "concept_count": concept_count,
        "direct_revenue_concepts": direct_revenue_concepts,
        "bank_net_interest_concepts": bank_net_interest_concepts,
        "bank_noninterest_concepts": bank_noninterest_concepts,
        "has_supported_revenue_source": bool(
            direct_revenue_concepts or has_bank_revenue_components
        ),
    }


def audit_cached_companyfacts_payload_profiles(cache_dir: Path) -> dict:
    """Aggregate raw-payload structure without modifying cache bytes."""
    rows = []
    for path in _companyfacts_cache_files(Path(cache_dir)):
        envelope = _read_companyfacts_cache_envelope(path)
        profile = classify_companyfacts_payload(envelope.get("payload") or {})
        rows.append({
            "cik": int(envelope["cik"]),
            "symbols": sorted(map(str, envelope.get("symbols", []))),
            **profile,
        })
    counts = Counter(row["profile"] for row in rows)
    nonstandard = [
        row for row in rows if row["profile"] != "US_GAAP_WITH_10Q"
    ]
    return {
        "payload_profile_counts": dict(sorted(counts.items())),
        "nonstandard_payload_count": len(nonstandard),
        "nonstandard_payload_sample": nonstandard[:20],
    }


def cached_companyfacts_symbol_payload_profiles(
    cache_dir: Path,
    tickers: list[str] | set[str] | None = None,
) -> dict[str, dict]:
    """Map cached symbol bindings to raw-payload profiles.

    When tickers are supplied, use the manifest index to decode only their
    distinct CIK payloads. Missing tickers are omitted so callers can classify
    them explicitly as not cached.
    """
    cache_dir = Path(cache_dir)
    requested = (
        {
            str(ticker).strip().upper()
            for ticker in tickers
            if str(ticker).strip()
        }
        if tickers is not None else None
    )
    paths = _companyfacts_cache_files(cache_dir)
    primary_cik_map = cached_companyfacts_cik_map(cache_dir)
    declared_chains = historical_ticker_cik_chains(cache_dir)
    if requested is not None:
        selected_chains = cached_companyfacts_cik_chains_for_symbols(
            requested & set(primary_cik_map), cache_dir
        )
        requested_ciks = {
            int(cik)
            for chain in selected_chains.values()
            for cik in chain
        }
        paths = [
            _companyfacts_cache_path(cik, cache_dir)
            for cik in sorted(requested_ciks)
        ]
    profiles: dict[str, dict] = {}
    for path in paths:
        envelope = _read_companyfacts_cache_envelope(path)
        profile = classify_companyfacts_payload(envelope.get("payload") or {})
        for raw_symbol in envelope.get("symbols", []):
            symbol = str(raw_symbol).strip().upper()
            if not symbol or (
                requested is not None and symbol not in requested
            ):
                continue
            prior = profiles.get(symbol)
            row = {"cik": int(envelope["cik"]), **profile}
            if prior is not None and prior["cik"] != row["cik"]:
                allowed = set(declared_chains.get(symbol, ()))
                if {prior["cik"], row["cik"]}.issubset(allowed):
                    priority = {
                        "US_GAAP_WITH_10Q": 5,
                        "US_GAAP_NO_10Q": 4,
                        "FOREIGN_PERIODIC_NO_10Q": 3,
                        "IFRS_WITHOUT_SUPPORTED_QUARTERS": 2,
                        "OTHER_TAXONOMY": 1,
                        "NO_FACTS": 0,
                    }
                    chosen = (
                        row
                        if priority.get(row["profile"], -1)
                        > priority.get(prior["profile"], -1)
                        else prior
                    )
                    source_ciks = sorted({
                        int(prior.get("profile_source_cik", prior["cik"])),
                        int(row["cik"]),
                    })
                    profiles[symbol] = {
                        **chosen,
                        "cik": int(primary_cik_map[symbol]),
                        "profile_source_cik": int(
                            chosen.get("profile_source_cik", chosen["cik"])
                        ),
                        "cik_chain": [
                            cik for cik in declared_chains[symbol]
                            if cik in source_ciks
                        ],
                        "has_supported_revenue_source": bool(
                            prior["has_supported_revenue_source"]
                            or row["has_supported_revenue_source"]
                        ),
                    }
                    continue
                raise RuntimeError(
                    f"Multiple cached payload profiles exist for {symbol}"
                )
            profiles[symbol] = row
    return profiles


def audit_companyfacts_cache_coverage(
    cache_dir: Path,
    required_symbols: list[str] | set[str],
    *,
    include_payload_profiles: bool = True,
) -> dict:
    """Verify raw-cache integrity and report symbol coverage without writes.

    Detailed payload profiles require decoding every cached Company Facts
    envelope. Full-rebuild preflight can disable them because inventory,
    symbol coverage, byte sizes, and SHA-256 are sufficient to guard writes.
    """
    cache_dir = Path(cache_dir)
    verification = verify_companyfacts_cache_manifest(cache_dir)
    required = {
        str(symbol).strip().upper()
        for symbol in required_symbols
        if str(symbol).strip()
    }
    cached = cached_companyfacts_symbols(cache_dir)
    known_unavailable = known_companyfacts_unavailable(cache_dir)
    missing = sorted(required - cached)
    unresolved = sorted(required - cached - known_unavailable)
    files = _companyfacts_cache_files(cache_dir)
    compressed_files = [
        path for path in files if path.name.endswith(".json.gz")
    ]
    result = {
        "mode": "companyfacts_cache_audit",
        "cache_dir": str(cache_dir),
        "manifest": verification["manifest"],
        "manifest_verified": verification["verified"],
        "cached_ciks": verification["entry_count"],
        "ticker_map_snapshot_count": verification["ticker_map_entry_count"],
        "required_symbol_count": len(required),
        "cached_required_symbol_count": len(required & cached),
        "missing_cache_symbol_count": len(missing),
        "cache_symbol_coverage": (
            len(required & cached) / len(required) if required else 1.0
        ),
        "missing_cache_symbols": missing,
        "known_unavailable_required_symbol_count": len(
            required & known_unavailable
        ),
        "known_unavailable_required_symbols": sorted(
            required & known_unavailable
        ),
        "unresolved_cache_symbol_count": len(unresolved),
        "unresolved_cache_symbols": unresolved,
        "cache_resolution_coverage": (
            len(required & (cached | known_unavailable)) / len(required)
            if required else 1.0
        ),
        "unrequired_cached_symbol_count": len(cached - required),
        "compressed_cache_file_count": len(compressed_files),
        "legacy_cache_file_count": len(files) - len(compressed_files),
        "cache_bytes": sum(path.stat().st_size for path in files),
        "payload_profiles_included": include_payload_profiles,
    }
    if include_payload_profiles:
        result.update(audit_cached_companyfacts_payload_profiles(cache_dir))
    return result


def raw_cache_refresh_state_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / RAW_CACHE_REFRESH_STATE_NAME


def known_companyfacts_unavailable(cache_dir: Path) -> set[str]:
    """Return symbols with manifest-bound official negative evidence."""
    path = raw_cache_refresh_state_path(cache_dir)
    if not path.exists():
        return set()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Invalid raw Company Facts refresh state: {exc}"
        ) from exc
    unavailable = set()
    for raw_ticker, raw_entry in state.items():
        if not isinstance(raw_entry, dict):
            continue
        ticker = str(raw_ticker).strip().upper()
        status = raw_entry.get("cache_status")
        reason = str(raw_entry.get("cache_failure_reason") or "")
        if (
            status == "companyfacts_not_available"
            and "HTTP Error 404: Not Found" in reason
        ):
            unavailable.add(ticker)
        elif (
            status == "not_in_sec_ticker_map"
            and raw_entry.get("cache_ticker_map_sha256")
        ):
            unavailable.add(ticker)
    return unavailable


def record_unmapped_companyfacts_symbols(
    cache_dir: Path,
    symbols: list[str] | set[str],
    *,
    as_of: date,
    ticker_map_sha256: str,
) -> Path:
    """Atomically persist official ticker-map absence as negative evidence."""
    if not ticker_map_sha256:
        raise ValueError("ticker_map_sha256 is required")
    cache_dir = Path(cache_dir)
    state_path = raw_cache_refresh_state_path(cache_dir)
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists() else {}
    )
    for raw_ticker in symbols:
        ticker = str(raw_ticker).strip().upper()
        if not ticker:
            continue
        entry = dict(state.get(ticker) or {})
        entry.update({
            "cache_last_attempt": as_of.isoformat(),
            "cache_status": "not_in_sec_ticker_map",
            "cache_failure_reason": (
                "Ticker absent from the exact SEC ticker-map snapshot"
            ),
            "cache_ticker_map_sha256": ticker_map_sha256,
        })
        state[ticker] = entry
    temporary = state_path.with_suffix(state_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, indent=2), encoding="utf-8"
    )
    os.replace(temporary, state_path)
    write_companyfacts_cache_manifest(cache_dir)
    return state_path


def _reconcile_raw_cache_refresh_state(
    state: dict,
    cached_symbols: set[str],
    include_missing: bool = False,
) -> None:
    """Reconcile state entries once a manifest-bound payload exists."""
    if include_missing:
        for ticker in cached_symbols:
            state.setdefault(ticker, {
                "cache_status": "raw_cached",
                "cache_failure_reason": None,
            })
    for ticker, raw_entry in state.items():
        if str(ticker).upper() not in cached_symbols:
            continue
        entry = dict(raw_entry or {})
        if (
            entry.get("cache_status") == "raw_cached"
            and entry.get("cache_failure_reason") is None
        ):
            continue
        entry["cache_status"] = "raw_cached"
        entry["cache_failure_reason"] = None
        state[ticker] = entry


def _checkpoint_raw_cache_refresh(
    cache_dir: Path,
    state: dict,
    changed_payload_paths: set[str] | None = None,
    keep_refresh_journal: bool = False,
) -> Path:
    """Atomically replace state, then bind it and all payloads in a manifest."""
    cache_dir = Path(cache_dir)
    _reconcile_raw_cache_refresh_state(
        state,
        cached_companyfacts_symbols(cache_dir),
    )
    state_path = raw_cache_refresh_state_path(cache_dir)
    pending_path = cache_dir / RAW_CACHE_CHECKPOINT_PENDING_NAME
    temporary_state = state_path.with_suffix(state_path.suffix + ".tmp")
    pending = pending_path.with_suffix(pending_path.suffix + ".tmp")
    pending.write_text(
        json.dumps({"phase": "checkpoint", "state_sha256": hashlib.sha256(
            json.dumps(state, indent=2).encode("utf-8")
        ).hexdigest()}, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(pending, pending_path)
    temporary_state.write_text(
        json.dumps(state, indent=2), encoding="utf-8"
    )
    os.replace(temporary_state, state_path)
    try:
        manifest_path = write_companyfacts_cache_manifest(
            cache_dir,
            changed_payload_paths=changed_payload_paths,
        )
    except BaseException:
        # Leave the marker behind so the next locked reader can safely finish
        # this checkpoint without asking the operator to re-sign the manifest.
        raise
    if keep_refresh_journal:
        refresh_marker = pending_path.with_suffix(pending_path.suffix + ".tmp")
        refresh_marker.write_text(
            json.dumps({
                "phase": "refresh",
                "base_manifest_sha256": _file_sha256(manifest_path),
            }, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(refresh_marker, pending_path)
    else:
        pending_path.unlink(missing_ok=True)
    return manifest_path


def _begin_raw_cache_refresh(cache_dir: Path) -> None:
    """Journal an in-flight refresh so hard interruption can be recovered."""
    cache_dir = Path(cache_dir)
    manifest_path = cache_dir / "manifest.json"
    marker_path = cache_dir / RAW_CACHE_CHECKPOINT_PENDING_NAME
    temporary = marker_path.with_suffix(marker_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({
            "phase": "refresh",
            "base_manifest_sha256": (
                _file_sha256(manifest_path)
                if manifest_path.exists() else None
            ),
        }, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, marker_path)


def _recover_pending_raw_cache_checkpoint(cache_dir: Path) -> None:
    """Finish a checkpoint interrupted between state and manifest replacement."""
    cache_dir = Path(cache_dir)
    pending_path = cache_dir / RAW_CACHE_CHECKPOINT_PENDING_NAME
    if not pending_path.exists():
        return
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        if not isinstance(pending, dict):
            raise ValueError("pending marker must be an object")
        phase = pending.get("phase", "checkpoint")
        state_path = raw_cache_refresh_state_path(cache_dir)
        if phase == "refresh":
            manifest_path = cache_dir / "manifest.json"
            expected_manifest = pending.get("base_manifest_sha256")
            actual_manifest = (
                _file_sha256(manifest_path)
                if manifest_path.exists() else None
            )
            if actual_manifest != expected_manifest:
                raise RuntimeError(
                    "Company Facts pending refresh manifest changed outside "
                    "the refresh transaction"
                )
            if manifest_path.exists():
                base_manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                for entry in base_manifest.get("entries", []):
                    payload = cache_dir / str(entry["path"])
                    if (
                        not payload.exists()
                        or payload.stat().st_size != int(entry["bytes"])
                        or _file_sha256(payload) != entry["sha256"]
                    ):
                        raise RuntimeError(
                            "Company Facts pending refresh found a changed "
                            "pre-existing payload"
                        )
            state = (
                json.loads(state_path.read_text(encoding="utf-8"))
                if state_path.exists() else {}
            )
            if not isinstance(state, dict):
                raise ValueError("raw-cache state must be an object")
            _reconcile_raw_cache_refresh_state(
                state,
                cached_companyfacts_symbols(cache_dir),
                include_missing=True,
            )
            _checkpoint_raw_cache_refresh(cache_dir, state)
            return
        expected = str(pending["state_sha256"])
        state_bytes = state_path.read_bytes()
        actual = hashlib.sha256(state_bytes).hexdigest()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Company Facts pending checkpoint marker is invalid"
        ) from exc
    if actual == expected:
        write_companyfacts_cache_manifest(cache_dir)
    pending_path.unlink(missing_ok=True)


def _record_raw_cache_attempt(
    state: dict,
    symbols: list[str],
    *,
    as_of: date,
    cached_symbols: set[str],
    failure_reason: str | None,
) -> None:
    for ticker in symbols:
        entry = dict(state.get(ticker) or {})
        raw_cached = ticker in cached_symbols
        reason = None if raw_cached else failure_reason
        entry.update({
            "cache_last_attempt": as_of.isoformat(),
            "cache_status": (
                "raw_cached"
                if raw_cached
                else (
                    "companyfacts_not_available"
                    if "HTTP Error 404: Not Found" in str(reason)
                    else "fetch_failed"
                )
            ),
            "cache_failure_reason": reason,
        })
        state[ticker] = entry


def _populate_missing_companyfacts_cache_unlocked(
    as_of: date,
    *,
    workers: int = 4,
    limit: int | None = None,
    refresh_after_days: int = 30,
    force: bool = False,
    tickers: list[str] | None = None,
    cik_overrides: dict[str, int] | None = None,
    cache_dir: Path = SEC_COMPANYFACTS_CACHE_DIR,
    refresh_priority: dict | None = None,
) -> dict:
    """Populate raw SEC cache without reading or writing parsed CSV outputs."""
    if limit is not None and limit <= 0:
        raise ValueError("limit must be a positive integer")
    cache_dir = Path(cache_dir)
    manifest_path = cache_dir / "manifest.json"
    cache_files_before = _companyfacts_cache_files(cache_dir)
    _recover_pending_raw_cache_checkpoint(cache_dir)
    if manifest_path.exists():
        verify_companyfacts_cache_manifest(cache_dir)
    elif cache_files_before:
        raise RuntimeError(
            "Company Facts cache contains payloads but has no manifest; "
            "refusing to rebaseline unverified cache bytes"
        )

    universe_frame = investable_common_equities(
        pd.read_csv(NASDAQ_300M_STOCK_LIST_FILE)
    )
    universe = (
        universe_frame["Symbol"].dropna().astype(str).str.upper().tolist()
    )
    cik_map, ticker_map_snapshot = fetch_sec_ticker_map_snapshot(cache_dir)
    write_companyfacts_cache_manifest(cache_dir)
    cik_map.update(load_historical_ticker_ciks(cache_dir))
    cik_map.update({
        str(ticker).upper(): int(cik)
        for ticker, cik in (cik_overrides or {}).items()
    })
    priority_tickers = (refresh_priority or {}).get("tickers")
    requested_universe = build_requested_refresh_universe(
        universe,
        tickers,
        priority_tickers,
        True,
    )
    priority_only_tickers = sorted(
        set(requested_universe) - set(universe)
    )
    unknown_tickers = unmapped_fundamentals_tickers(
        requested_universe, cik_map
    )
    if unknown_tickers and tickers:
        raise ValueError(
            "No SEC CIK mapping for explicitly requested tickers: "
            + ", ".join(unknown_tickers)
            + ". Supply --ticker-cik TICKER=CIK for historical symbols."
        )
    requested_universe = [
        ticker for ticker in requested_universe if ticker in cik_map
    ]
    requested_universe, matched_priority_tickers = (
        prioritize_refresh_tickers(
            requested_universe,
            priority_tickers,
        )
    )

    state_path = raw_cache_refresh_state_path(cache_dir)
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists() else {}
    )
    for ticker in unmapped_fundamentals_tickers(universe, cik_map):
        entry = dict(state.get(ticker) or {})
        entry.update({
            "cache_last_attempt": as_of.isoformat(),
            "cache_status": "not_in_sec_ticker_map",
            "cache_failure_reason": (
                "Ticker absent from the exact SEC ticker-map snapshot"
            ),
            "cache_ticker_map_sha256": ticker_map_snapshot[
                "mapping_sha256"
            ],
        })
        state[ticker] = entry
    cached_symbols_before = cached_companyfacts_symbols(cache_dir)
    cached_ciks_before = cached_companyfacts_ciks(cache_dir)
    cached_symbol_cik_bindings = cached_companyfacts_symbol_cik_bindings(
        cache_dir
    )
    explicit_override_missing_payload = {
        str(ticker).strip().upper()
        for ticker, cik in (cik_overrides or {}).items()
        if str(ticker).strip()
        and int(cik)
        not in cached_symbol_cik_bindings.get(
            str(ticker).strip().upper(), set()
        )
    }
    cached_cik_alias_tickers = [
        ticker
        for ticker in requested_universe
        if (
            ticker not in cached_symbols_before
            and ticker not in explicit_override_missing_payload
            and int(cik_map[ticker]) in cached_ciks_before
        )
    ]
    effective_cached_symbols = (
        cached_symbols_before | set(cached_cik_alias_tickers)
    ) - explicit_override_missing_payload
    eligible_before_limit = select_fundamentals_refresh_tickers(
        requested_universe,
        cik_map,
        state,
        as_of,
        refresh_after_days,
        force=force,
        cache_missing_only=True,
        cached_symbols=effective_cached_symbols,
    )
    requested = limit_refresh_tickers_by_cik(
        eligible_before_limit,
        cik_map,
        limit,
    )
    requested = expand_selected_cik_aliases(
        requested,
        requested_universe,
        cik_map,
        excluded_symbols=effective_cached_symbols,
    )
    deferred_tickers, cache_cooldown_tickers = (
        classify_cache_refresh_backlog(
            requested_universe,
            eligible_before_limit,
            requested,
            effective_cached_symbols,
        )
    )
    requested_by_cik = _group_tickers_by_cik(requested, cik_map)
    cached_aliases_by_cik = _group_tickers_by_cik(
        cached_cik_alias_tickers, cik_map
    )
    _begin_raw_cache_refresh(cache_dir)
    parse_warnings = []
    fetch_failures = []
    for cik, symbols in cached_aliases_by_cik.items():
        try:
            results = parse_and_bind_cached_companyfacts_symbols(
                symbols, cik, cache_dir
            )
            parse_warnings.extend(
                {
                    "ticker": ticker,
                    "reason": "no_cached_sec_fundamentals",
                }
                for ticker, (annual, quarterly) in results.items()
                if annual.empty and quarterly.empty
            )
        except Exception as exc:
            fetch_failures.extend(
                {"ticker": ticker, "reason": str(exc)}
                for ticker in symbols
            )
    cached_symbols_after_aliases = cached_companyfacts_symbols(cache_dir)
    alias_failure_reasons = {
        str(failure["ticker"]).upper(): failure["reason"]
        for failure in fetch_failures
    }
    for ticker in cached_cik_alias_tickers:
        _record_raw_cache_attempt(
            state,
            [ticker],
            as_of=as_of,
            cached_symbols=cached_symbols_after_aliases,
            failure_reason=alias_failure_reasons.get(ticker),
        )

    completed_ciks_since_checkpoint = 0
    changed_payload_paths: set[str] = set()
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {
        pool.submit(
            fetch_sec_fundamentals_for_symbols,
            symbols,
            cik,
            3,
            cache_dir,
            False,
        ): (cik, symbols)
        for cik, symbols in requested_by_cik.items()
    }
    interrupted = None
    try:
        for future in as_completed(futures):
            _cik, symbols = futures[future]
            failure_reason = None
            try:
                results = future.result()
                parse_warnings.extend(
                    {
                        "ticker": ticker,
                        "reason": "no_sec_fundamentals",
                    }
                    for ticker, (annual, quarterly) in results.items()
                    if annual.empty and quarterly.empty
                )
            except Exception as exc:
                failure_reason = str(exc)
                fetch_failures.extend(
                    {"ticker": ticker, "reason": failure_reason}
                    for ticker in symbols
                )
            cached_symbols_now = cached_companyfacts_symbols(cache_dir)
            changed_payload_paths.add(
                _companyfacts_cache_path(_cik, cache_dir).name
            )
            _record_raw_cache_attempt(
                state,
                symbols,
                as_of=as_of,
                cached_symbols=cached_symbols_now,
                failure_reason=failure_reason,
            )
            completed_ciks_since_checkpoint += 1
            if (
                completed_ciks_since_checkpoint
                >= RAW_CACHE_CHECKPOINT_CIK_INTERVAL
            ):
                _checkpoint_raw_cache_refresh(
                    cache_dir,
                    state,
                    changed_payload_paths,
                    keep_refresh_journal=True,
                )
                changed_payload_paths.clear()
                completed_ciks_since_checkpoint = 0
    except BaseException as exc:
        interrupted = exc
        for future in futures:
            future.cancel()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        cached_symbols_after_shutdown = cached_companyfacts_symbols(cache_dir)
        for symbols in requested_by_cik.values():
            successful_symbols = [
                ticker
                for ticker in symbols
                if ticker in cached_symbols_after_shutdown
            ]
            if successful_symbols:
                _record_raw_cache_attempt(
                    state,
                    successful_symbols,
                    as_of=as_of,
                    cached_symbols=cached_symbols_after_shutdown,
                    failure_reason=None,
                )
        _checkpoint_raw_cache_refresh(
            cache_dir, state, changed_payload_paths
        )
        changed_payload_paths.clear()
    if interrupted is not None:
        raise interrupted

    cached_symbols_after = cached_companyfacts_symbols(cache_dir)
    processed_tickers = list(dict.fromkeys([
        *requested,
        *cached_cik_alias_tickers,
    ]))
    manifest_path = cache_dir / "manifest.json"

    coverage = audit_companyfacts_cache_coverage(
        cache_dir,
        universe,
        include_payload_profiles=False,
    )
    coverage.update({
        "mode": "raw_companyfacts_cache_missing_only",
        "as_of": as_of.isoformat(),
        "sec_ticker_map_snapshot": ticker_map_snapshot,
        "requested_tickers": requested,
        "requested": len(requested),
        "requested_ciks": len(requested_by_cik),
        "cached_cik_alias_tickers": cached_cik_alias_tickers,
        "cached_cik_alias_count": len(cached_cik_alias_tickers),
        "eligible_before_limit_ticker_count": len(
            eligible_before_limit
        ),
        "eligible_before_limit_cik_count": len(
            _group_tickers_by_cik(eligible_before_limit, cik_map)
        ),
        "deferred_by_limit_ticker_count": len(deferred_tickers),
        "deferred_by_limit_cik_count": len(
            set(_group_tickers_by_cik(deferred_tickers, cik_map))
            - set(requested_by_cik)
        ),
        "deferred_by_limit_ticker_sample": deferred_tickers[:20],
        "cache_cooldown_ticker_count": len(cache_cooldown_tickers),
        "cache_cooldown_ticker_sample": cache_cooldown_tickers[:20],
        "cached_symbol_count_before": len(cached_symbols_before),
        "cached_symbol_count_after": len(cached_symbols_after),
        "new_cached_symbol_count": len(
            cached_symbols_after - cached_symbols_before
        ),
        "failures": fetch_failures,
        "parse_warnings": parse_warnings,
        "unmapped_universe_ticker_count": len(
            unmapped_fundamentals_tickers(universe, cik_map)
        ),
        "unmapped_universe_tickers": (
            unmapped_fundamentals_tickers(universe, cik_map)
        ),
        "priority_only_ticker_count": len(priority_only_tickers),
        "priority_only_unmapped_tickers": sorted(
            set(priority_only_tickers) & set(unknown_tickers)
        ),
        "matched_refresh_priority_ticker_count": (
            matched_priority_tickers
        ),
        "refresh_priority": (
            {
                key: value
                for key, value in refresh_priority.items()
                if key != "tickers"
            }
            if refresh_priority else None
        ),
        "raw_cache_refresh_state": str(state_path),
        "companyfacts_cache_manifest": str(manifest_path),
        "formal_outputs_read": False,
        "formal_outputs_written": False,
        "parsed_outputs_written": False,
    })
    return coverage


def populate_missing_companyfacts_cache(
    as_of: date,
    *,
    workers: int = 4,
    limit: int | None = None,
    refresh_after_days: int = 30,
    force: bool = False,
    tickers: list[str] | None = None,
    cik_overrides: dict[str, int] | None = None,
    cache_dir: Path = SEC_COMPANYFACTS_CACHE_DIR,
    refresh_priority: dict | None = None,
) -> dict:
    """Lock and populate only the raw Company Facts cache."""
    with companyfacts_cache_lock(cache_dir):
        return _populate_missing_companyfacts_cache_unlocked(
            as_of,
            workers=workers,
            limit=limit,
            refresh_after_days=refresh_after_days,
            force=force,
            tickers=tickers,
            cik_overrides=cik_overrides,
            cache_dir=cache_dir,
            refresh_priority=refresh_priority,
        )


def _companyfacts_cache_index_entries(cache_dir: Path) -> list[dict]:
    """Use the manifest index when valid, with envelope fallback."""
    cache_dir = Path(cache_dir)
    entries = None
    try:
        manifest = json.loads(
            (cache_dir / "manifest.json").read_text(encoding="utf-8")
        )
        actual_paths = {
            path.name for path in _companyfacts_cache_files(cache_dir)
        }
        candidate_entries = manifest.get("entries")
        if (
            manifest.get("format_version") == 1
            and isinstance(candidate_entries, list)
            and {
                entry.get("path")
                for entry in candidate_entries
                if isinstance(entry, dict)
            } == actual_paths
            and len(candidate_entries) == len(actual_paths)
        ):
            entries = candidate_entries
    except (OSError, json.JSONDecodeError, RuntimeError):
        pass
    if entries is None:
        entries = []
        for path in _companyfacts_cache_files(cache_dir):
            envelope = _read_companyfacts_cache_envelope(path)
            entries.append({
                "cik": envelope.get("cik"),
                "symbols": envelope.get("symbols", []),
            })
    return entries


def cached_companyfacts_symbol_cik_bindings(
    cache_dir: Path,
) -> dict[str, set[int]]:
    """Return every cached CIK presently bound to each ticker."""
    symbol_ciks: dict[str, set[int]] = {}
    for entry in _companyfacts_cache_index_entries(cache_dir):
        try:
            cik = int(entry["cik"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "Company Facts cache index has an invalid CIK"
            ) from exc
        raw_symbols = entry.get("symbols", [])
        if not isinstance(raw_symbols, list):
            raise RuntimeError(
                f"Company Facts cache index for CIK {cik} has invalid symbols"
            )
        for raw_symbol in raw_symbols:
            symbol = str(raw_symbol).strip().upper()
            if not symbol:
                continue
            symbol_ciks.setdefault(symbol, set()).add(cik)
    return symbol_ciks


def cached_companyfacts_cik_map(cache_dir: Path) -> dict[str, int]:
    symbol_ciks = cached_companyfacts_symbol_cik_bindings(cache_dir)
    transition_chains = historical_ticker_cik_chains(cache_dir)
    mapping = {}
    ambiguous = set()
    for symbol, ciks in symbol_ciks.items():
        declared_chain = transition_chains.get(symbol)
        if len(ciks) > 1 and (
            declared_chain is None
            or not ciks.issubset(set(declared_chain))
        ):
            ambiguous.add(symbol)
            continue
        if declared_chain is not None:
            cached_chain = [
                cik for cik in declared_chain if cik in ciks
            ]
            if cached_chain:
                mapping[symbol] = cached_chain[0]
                continue
        mapping[symbol] = next(iter(ciks))
    if ambiguous:
        raise RuntimeError(
            "Multiple cached CIKs exist for symbols "
            + ", ".join(sorted(ambiguous))
            + "; declare a manifest-bound historical CIK transition before "
            "reparsing"
        )
    return mapping


def cached_companyfacts_cik_chains(
    cache_dir: Path,
) -> dict[str, tuple[int, ...]]:
    """Return every validated cached CIK contributing to each symbol."""
    cache_dir = Path(cache_dir)
    primary_map = cached_companyfacts_cik_map(cache_dir)
    observed: dict[str, set[int]] = {}
    for path in _companyfacts_cache_files(cache_dir):
        envelope = _read_companyfacts_cache_envelope(path)
        cik = int(envelope["cik"])
        for raw_symbol in envelope.get("symbols", []):
            symbol = str(raw_symbol).strip().upper()
            if symbol:
                observed.setdefault(symbol, set()).add(cik)
    declared = historical_ticker_cik_chains(cache_dir)
    chains = {}
    for symbol, ciks in observed.items():
        declared_chain = declared.get(symbol)
        if declared_chain is not None:
            ordered = tuple(cik for cik in declared_chain if cik in ciks)
            if ordered:
                chains[symbol] = ordered
                continue
        chains[symbol] = (int(primary_map[symbol]),)
    return chains


def cached_companyfacts_cik_chains_for_symbols(
    symbols: list[str] | set[str],
    cache_dir: Path,
) -> dict[str, tuple[int, ...]]:
    """Resolve selected symbol CIK chains without decoding unrelated payloads."""
    normalized = list(dict.fromkeys(
        str(symbol).strip().upper()
        for symbol in symbols
        if str(symbol).strip()
    ))
    primary_map = cached_companyfacts_cik_map(cache_dir)
    declared = historical_ticker_cik_chains(cache_dir)
    missing = sorted(set(normalized) - set(primary_map))
    if missing:
        raise RuntimeError(
            "No raw Company Facts cache for requested tickers: "
            + ", ".join(missing)
        )
    chains = {}
    for symbol in normalized:
        candidate_chain = declared.get(
            symbol, (int(primary_map[symbol]),)
        )
        cached_chain = tuple(
            cik for cik in candidate_chain
            if _companyfacts_cache_path(cik, Path(cache_dir)).exists()
        )
        if not cached_chain:
            raise RuntimeError(
                f"No raw Company Facts cache for requested ticker: {symbol}"
            )
        chains[symbol] = cached_chain
    return chains


def cached_companyfacts_ciks(cache_dir: Path) -> set[int]:
    """Return raw payload CIKs after validating every cache envelope."""
    return {
        int(_read_companyfacts_cache_envelope(path)["cik"])
        for path in _companyfacts_cache_files(Path(cache_dir))
    }


def parse_and_bind_cached_companyfacts_symbols(
    symbols: list[str],
    cik: int,
    cache_dir: Path,
) -> dict[str, tuple[pd.DataFrame, pd.DataFrame]]:
    """Parse new aliases from an existing payload and persist their binding."""
    normalized = list(dict.fromkeys(
        str(symbol).strip().upper()
        for symbol in symbols
        if str(symbol).strip()
    ))
    if not normalized:
        raise ValueError("at least one cached alias is required")
    payload, fetched_at = _read_companyfacts_cache(cik, Path(cache_dir))
    _write_companyfacts_cache(
        normalized, cik, payload, fetched_at, Path(cache_dir)
    )
    return {
        symbol: (
            parse_companyfacts_annual(symbol, payload, fetched_at),
            parse_registered_companyfacts_quarterly(
                symbol, cik, payload, fetched_at
            )[0],
        )
        for symbol in normalized
    }


def _reparse_companyfacts_cache_unlocked(
    cache_dir: Path = SEC_COMPANYFACTS_CACHE_DIR,
    output: Path = Path(POINT_IN_TIME_FUNDAMENTALS_FILE),
    quarterly_output: Path = Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE),
    tickers: list[str] | None = None,
    include_validated_foreign_quarters: bool = False,
    replace_complete_outputs: bool = False,
) -> dict:
    """Rebuild all or selected parsed fundamentals from the raw SEC cache.

    With ``tickers=None`` both outputs are rebuilt from scratch.  Otherwise
    parsed rows for the requested tickers are upserted without deleting
    existing facts that are absent from the selected cache payloads.  A
    scope-bound full rebuild supplies explicit ``tickers`` together with
    ``replace_complete_outputs=True``: it parses only the frozen scope but
    still replaces the paired outputs atomically.
    """
    cache_dir = Path(cache_dir)
    requested = (
        set(dict.fromkeys(str(ticker).strip().upper() for ticker in tickers))
        if tickers is not None
        else None
    )
    if requested is not None and (not requested or "" in requested):
        raise ValueError("incremental cache reparse requires non-empty tickers")
    if replace_complete_outputs and requested is None:
        raise ValueError(
            "scope-bound full rebuild requires explicit parsed tickers"
        )
    cache_paths = _companyfacts_cache_files(cache_dir)
    if requested is not None:
        cached_cik_map = cached_companyfacts_cik_map(cache_dir)
        missing_index_symbols = sorted(requested - set(cached_cik_map))
        if missing_index_symbols:
            raise RuntimeError(
                "No raw Company Facts cache for requested tickers: "
                + ", ".join(missing_index_symbols)
            )
        cached_cik_chains = cached_companyfacts_cik_chains_for_symbols(
            requested, cache_dir
        )
        requested_ciks = {
            int(cik)
            for symbol in requested
            for cik in cached_cik_chains[symbol]
        }
        cache_paths = [
            _companyfacts_cache_path(cik, cache_dir)
            for cik in sorted(requested_ciks)
        ]
    annual_frames = []
    quarterly_frames = []
    parsed_ciks = 0
    parsed_symbols = 0
    cached_requested = set()
    validated_foreign_symbols = []
    for path in cache_paths:
        envelope = _read_companyfacts_cache_envelope(path)
        cik = int(envelope["cik"])
        symbols = sorted({
            str(symbol).upper()
            for symbol in envelope.get("symbols", [])
            if str(symbol).strip()
        })
        if not symbols:
            raise RuntimeError(f"{path} has no symbols for offline reparse")
        selected_symbols = (
            symbols if requested is None else sorted(set(symbols) & requested)
        )
        if not selected_symbols:
            continue
        payload, fetched_at = _validated_companyfacts_cache_payload(
            envelope, cik
        )
        parsed_ciks += 1
        for symbol in selected_symbols:
            annual_frames.append(
                parse_companyfacts_annual(symbol, payload, fetched_at)
            )
            quarterly, used_registered_foreign = (
                parse_registered_companyfacts_quarterly(
                    symbol, cik, payload, fetched_at
                )
            )
            if (
                include_validated_foreign_quarters
                and not used_registered_foreign
            ):
                raise RuntimeError(
                    f"{symbol} is not in the validated foreign registry"
                )
            if used_registered_foreign:
                validated_foreign_symbols.append(symbol)
            quarterly_frames.append(quarterly)
            parsed_symbols += 1
            cached_requested.add(symbol)
    if requested is not None:
        missing = sorted(requested - cached_requested)
        if missing:
            raise RuntimeError(
                "No raw Company Facts cache for requested tickers: "
                + ", ".join(missing)
            )
    elif not parsed_ciks:
        raise RuntimeError(f"No cached Company Facts payloads in {cache_dir}")
    manifest_verification = verify_companyfacts_cache_manifest(
        cache_dir,
        payload_paths=(
            {path.name for path in cache_paths}
            if requested is not None else None
        ),
    )
    annual_incoming = (
        pd.concat(annual_frames, ignore_index=True)
        if annual_frames else pd.DataFrame(columns=OUTPUT_COLUMNS)
    )
    quarterly_incoming = (
        pd.concat(quarterly_frames, ignore_index=True)
        if quarterly_frames else pd.DataFrame(columns=OUTPUT_COLUMNS)
    )
    if (
        requested is not None
        and annual_incoming.empty
        and quarterly_incoming.empty
        and not replace_complete_outputs
    ):
        return {
            "mode": "offline_cache_incremental_rebuild",
            "merge_policy": "non_destructive_upsert",
            "requested_tickers": sorted(requested),
            "cache_dir": str(cache_dir),
            "cache_manifest": manifest_verification["manifest"],
            "cache_manifest_verified": manifest_verification["verified"],
            "cache_manifest_verification_scope": manifest_verification[
                "verification_scope"
            ],
            "cache_manifest_verified_payload_count": manifest_verification[
                "verified_payload_count"
            ],
            "cached_ciks": parsed_ciks,
            "parsed_symbol_bindings": parsed_symbols,
            "validated_foreign_symbols": sorted(validated_foreign_symbols),
            "annual_incoming_rows": 0,
            "quarterly_incoming_rows": 0,
            "annual_rows": None,
            "quarterly_rows": None,
            "annual_output_written": False,
            "quarterly_output_written": False,
            "parsed_outputs_written": False,
            "output": str(output),
            "quarterly_output": str(quarterly_output),
        }
    full_replacement = replace_complete_outputs or requested is None
    if full_replacement:
        annual_existing = pd.DataFrame(columns=OUTPUT_COLUMNS)
        quarterly_existing = pd.DataFrame(columns=OUTPUT_COLUMNS)
    else:
        annual_existing = (
            (
                pd.read_csv(output)
                if Path(output).exists()
                else pd.DataFrame(columns=OUTPUT_COLUMNS)
            )
            if not annual_incoming.empty else None
        )
        quarterly_existing = (
            (
                pd.read_csv(quarterly_output)
                if Path(quarterly_output).exists()
                else pd.DataFrame(columns=OUTPUT_COLUMNS)
            )
            if not quarterly_incoming.empty else None
        )
    annual = (
        integrate_refreshed_fundamentals(
            annual_existing,
            annual_incoming,
            requested or set(),
            non_destructive=not full_replacement,
        )
        if annual_existing is not None else None
    )
    quarterly = (
        integrate_refreshed_fundamentals(
            quarterly_existing,
            quarterly_incoming,
            requested or set(),
            non_destructive=not full_replacement,
        )
        if quarterly_existing is not None else None
    )
    write_fundamentals_pair(
        annual, Path(output), quarterly, Path(quarterly_output)
    )
    return {
        "mode": (
            "offline_cache_full_rebuild"
            if full_replacement else "offline_cache_incremental_rebuild"
        ),
        "merge_policy": (
            "replace_complete_outputs"
            if full_replacement else "non_destructive_upsert"
        ),
        "requested_tickers": sorted(requested) if requested is not None else None,
        "cache_dir": str(cache_dir),
        "cache_manifest": manifest_verification["manifest"],
        "cache_manifest_verified": manifest_verification["verified"],
        "cache_manifest_verification_scope": manifest_verification[
            "verification_scope"
        ],
        "cache_manifest_verified_payload_count": manifest_verification[
            "verified_payload_count"
        ],
        "cached_ciks": parsed_ciks,
        "parsed_symbol_bindings": parsed_symbols,
        "validated_foreign_symbols": sorted(validated_foreign_symbols),
        "annual_incoming_rows": len(annual_incoming),
        "quarterly_incoming_rows": len(quarterly_incoming),
        "annual_rows": len(annual) if annual is not None else None,
        "quarterly_rows": (
            len(quarterly) if quarterly is not None else None
        ),
        "annual_output_written": annual is not None,
        "quarterly_output_written": quarterly is not None,
        "parsed_outputs_written": True,
        "output": str(output),
        "quarterly_output": str(quarterly_output),
    }


def companyfacts_reparse_state_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / COMPANYFACTS_REPARSE_STATE_NAME


def _companyfacts_reparse_fingerprints(
    tickers: list[str],
    cache_dir: Path,
) -> dict[str, dict]:
    """Fingerprint parser inputs with one cache inventory scan."""
    normalized = list(dict.fromkeys(
        str(ticker).strip().upper()
        for ticker in tickers
        if str(ticker).strip()
    ))
    cik_map = cached_companyfacts_cik_map(cache_dir)
    missing = sorted(set(normalized) - set(cik_map))
    if missing:
        raise RuntimeError(
            "No raw Company Facts cache for requested tickers: "
            + ", ".join(missing)
        )
    cik_chains = cached_companyfacts_cik_chains_for_symbols(
        normalized, cache_dir
    )
    recipe = companyfacts_full_rebuild_recipe()
    parser_sha256 = recipe["parser_sha256"]
    parser_runtime = recipe["runtime"]
    output_columns_sha256 = recipe["output_columns_sha256"]
    foreign_parser_sha256 = recipe["foreign_quarterly_parser"]["sha256"]
    foreign_registry = validated_foreign_quarterly_registry()
    payloads = {}
    fingerprints = {}
    for ticker in normalized:
        cik = int(cik_map[ticker])
        chain_payloads = []
        for chain_cik in cik_chains[ticker]:
            if chain_cik not in payloads:
                cache_path = _companyfacts_cache_path(
                    chain_cik, Path(cache_dir)
                )
                payloads[chain_cik] = (
                    cache_path, _file_sha256(cache_path)
                )
            cache_path, payload_sha256 = payloads[chain_cik]
            chain_payloads.append({
                "cik": int(chain_cik),
                "payload_path": cache_path.name,
                "payload_sha256": payload_sha256,
            })
        foreign_registry_entry = foreign_registry.get(ticker)
        components = {
            "ticker": ticker,
            "cik": cik,
            "cik_chain": [entry["cik"] for entry in chain_payloads],
            "payloads": chain_payloads,
            "parser_sha256": parser_sha256,
            "parser_runtime": parser_runtime,
            "output_columns_sha256": output_columns_sha256,
            # The foreign parser is dynamically imported only for tickers in
            # the reviewed registry.  Its code changes must therefore
            # invalidate those tickers without needlessly reparsing every
            # domestic payload.
            "foreign_parser_sha256": (
                foreign_parser_sha256
                if foreign_registry_entry is not None
                else None
            ),
            # Registry changes should invalidate only the affected ticker,
            # rather than forcing an unrelated full-universe reparse.
            "foreign_registry_entry": foreign_registry_entry,
        }
        canonical = json.dumps(
            components, sort_keys=True, separators=(",", ":")
        ).encode()
        components["fingerprint"] = hashlib.sha256(canonical).hexdigest()
        fingerprints[ticker] = components
    return fingerprints


def companyfacts_reparse_fingerprint(
    ticker: str,
    cache_dir: Path,
) -> dict:
    """Fingerprint every input that can change a ticker's parsed rows."""
    normalized = str(ticker).strip().upper()
    return _companyfacts_reparse_fingerprints(
        [normalized], cache_dir
    )[normalized]


def load_companyfacts_reparse_state(cache_dir: Path) -> dict:
    path = companyfacts_reparse_state_path(cache_dir)
    if not path.exists():
        return {"format_version": 1, "tickers": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Invalid Company Facts reparse state: {exc}"
        ) from exc
    if (
        state.get("format_version") != 1
        or not isinstance(state.get("tickers"), dict)
    ):
        raise RuntimeError("Invalid Company Facts reparse state format")
    return state


def select_changed_companyfacts_reparse_tickers(
    tickers: list[str],
    cache_dir: Path,
) -> tuple[list[str], list[str]]:
    """Return changed and unchanged ticker inputs in caller priority order."""
    normalized = list(dict.fromkeys(
        str(ticker).strip().upper()
        for ticker in tickers
        if str(ticker).strip()
    ))
    state = load_companyfacts_reparse_state(cache_dir)["tickers"]
    fingerprints = _companyfacts_reparse_fingerprints(
        normalized, cache_dir
    )
    changed, unchanged = [], []
    for ticker in normalized:
        current = fingerprints[ticker]
        prior = state.get(ticker) or {}
        target = (
            unchanged
            if prior.get("fingerprint") == current["fingerprint"]
            else changed
        )
        target.append(ticker)
    return changed, unchanged


def record_companyfacts_reparse_state(
    tickers: list[str],
    cache_dir: Path,
) -> Path:
    """Atomically record successful parser inputs for resumable reparses."""
    state = load_companyfacts_reparse_state(cache_dir)
    fingerprints = _companyfacts_reparse_fingerprints(tickers, cache_dir)
    for ticker, fingerprint in fingerprints.items():
        state["tickers"][str(ticker).strip().upper()] = {
            **fingerprint,
            "reparsed_at": date.today().isoformat(),
        }
    path = companyfacts_reparse_state_path(cache_dir)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return path


def _full_reparse_required_symbols(
    output: Path,
    quarterly_output: Path,
) -> set[str]:
    """Return the complete protected ticker set for a full rebuild."""
    universe_frame = investable_common_equities(
        pd.read_csv(NASDAQ_300M_STOCK_LIST_FILE)
    )
    required_symbols = set(
        universe_frame["Symbol"]
        .dropna()
        .astype(str)
        .str.upper()
        .tolist()
    )
    for formal_output in (Path(output), Path(quarterly_output)):
        if not formal_output.exists():
            continue
        formal_symbols = pd.read_csv(
            formal_output, usecols=["ticker"]
        )["ticker"]
        required_symbols.update(
            formal_symbols.dropna().astype(str).str.upper()
        )
    return required_symbols


def _full_reparse_output_comparison(
    formal_output: Path,
    rebuilt_output: Path,
    *,
    include_ticker_deltas: bool = False,
) -> dict:
    """Compare a temporary full rebuild with formal output content by ticker.

    ``fetched_at`` is intentionally excluded from row-content comparison: it
    is a raw-fetch timestamp, not a point-in-time financial fact.  This makes
    a snapshot diagnostic distinguish a harmless cache refresh timestamp from
    a real parser or SEC-fact difference.  Full per-ticker deltas are opt-in
    because they can be large for a complete universe rebuild.
    """
    formal_output = Path(formal_output)
    rebuilt_output = Path(rebuilt_output)
    formal = (
        pd.read_csv(formal_output)
        if formal_output.exists()
        else pd.DataFrame(columns=OUTPUT_COLUMNS)
    )
    rebuilt = pd.read_csv(rebuilt_output)

    def ticker_counts(frame: pd.DataFrame) -> pd.Series:
        if "ticker" not in frame:
            return pd.Series(dtype="int64")
        return (
            frame["ticker"].dropna().astype(str).str.upper()
            .value_counts().sort_index()
        )

    formal_counts = ticker_counts(formal)
    rebuilt_counts = ticker_counts(rebuilt)
    formal_tickers = set(formal_counts.index)
    rebuilt_tickers = set(rebuilt_counts.index)
    differing_tickers = []
    for ticker in sorted(formal_tickers | rebuilt_tickers):
        formal_rows = int(formal_counts.get(ticker, 0))
        rebuilt_rows = int(rebuilt_counts.get(ticker, 0))
        if formal_rows != rebuilt_rows:
            differing_tickers.append({
                "ticker": ticker,
                "formal_rows": formal_rows,
                "rebuilt_rows": rebuilt_rows,
            })

    comparison_columns = [
        column for column in OUTPUT_COLUMNS if column != "fetched_at"
    ]

    def normalized_value(value):
        if pd.isna(value):
            return None
        if isinstance(value, Number) and not isinstance(value, bool):
            try:
                numeric = Decimal(str(value))
            except InvalidOperation:
                return str(value)
            if numeric.is_finite():
                return f"number:{numeric.normalize()}"
        return str(value).strip()

    def ticker_fact_counters(frame: pd.DataFrame) -> dict[str, Counter]:
        missing_columns = [
            column for column in comparison_columns if column not in frame
        ]
        if missing_columns:
            raise ValueError(
                "full-rebuild comparison is missing output columns: "
                + ", ".join(missing_columns)
            )
        ticker_index = comparison_columns.index("ticker")
        result: dict[str, Counter] = {}
        for row in frame[comparison_columns].itertuples(index=False, name=None):
            ticker = str(row[ticker_index]).strip().upper()
            if not ticker or ticker.lower() == "nan":
                raise ValueError("full-rebuild comparison contains a blank ticker")
            fingerprint = json.dumps(
                [normalized_value(value) for value in row],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            result.setdefault(ticker, Counter())[fingerprint] += 1
        return result

    formal_facts = ticker_fact_counters(formal)
    rebuilt_facts = ticker_fact_counters(rebuilt)
    content_deltas = []
    for ticker in sorted(set(formal_facts) | set(rebuilt_facts)):
        formal_counter = formal_facts.get(ticker, Counter())
        rebuilt_counter = rebuilt_facts.get(ticker, Counter())
        formal_only_rows = sum((formal_counter - rebuilt_counter).values())
        rebuilt_only_rows = sum((rebuilt_counter - formal_counter).values())
        if formal_only_rows or rebuilt_only_rows:
            content_deltas.append({
                "ticker": ticker,
                "formal_rows": int(formal_counts.get(ticker, 0)),
                "rebuilt_rows": int(rebuilt_counts.get(ticker, 0)),
                "formal_only_content_rows": formal_only_rows,
                "rebuilt_only_content_rows": rebuilt_only_rows,
            })
    formal_sha256 = (
        _file_sha256(formal_output) if formal_output.exists() else None
    )
    rebuilt_sha256 = _file_sha256(rebuilt_output)
    result = {
        "comparison_format_version": 2,
        "formal_output": str(formal_output),
        "formal_sha256": formal_sha256,
        "rebuilt_sha256": rebuilt_sha256,
        "exact_byte_match": formal_sha256 == rebuilt_sha256,
        "formal_rows": len(formal),
        "rebuilt_rows": len(rebuilt),
        "row_count_difference": len(rebuilt) - len(formal),
        "formal_ticker_count": len(formal_tickers),
        "rebuilt_ticker_count": len(rebuilt_tickers),
        "formal_only_ticker_count": len(formal_tickers - rebuilt_tickers),
        "formal_only_ticker_sample": sorted(
            formal_tickers - rebuilt_tickers
        )[:20],
        "rebuilt_only_ticker_count": len(rebuilt_tickers - formal_tickers),
        "rebuilt_only_ticker_sample": sorted(
            rebuilt_tickers - formal_tickers
        )[:20],
        "ticker_row_count_difference_count": len(differing_tickers),
        "ticker_row_count_difference_sample": differing_tickers[:20],
        "content_comparison_excluded_columns": ["fetched_at"],
        "content_difference_ticker_count": len(content_deltas),
        "formal_only_content_row_count": sum(
            item["formal_only_content_rows"] for item in content_deltas
        ),
        "rebuilt_only_content_row_count": sum(
            item["rebuilt_only_content_rows"] for item in content_deltas
        ),
        "formal_content_match": (
            len(rebuilt) == len(formal)
            and not content_deltas
        ),
        "content_difference_sample": content_deltas[:20],
    }
    if include_ticker_deltas:
        result["content_difference_tickers"] = content_deltas
    return result


def dry_run_companyfacts_full_reparse(
    cache_dir: Path = SEC_COMPANYFACTS_CACHE_DIR,
    output: Path = Path(POINT_IN_TIME_FUNDAMENTALS_FILE),
    quarterly_output: Path = Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE),
    required_symbols: list[str] | set[str] | None = None,
    expected_cache_manifest_sha256: str | None = None,
    expected_rebuild_recipe_sha256: str | None = None,
    include_ticker_deltas: bool = False,
) -> dict:
    """Fully rebuild into disposable files and compare without input writes.

    The same cache coverage gate as a real full reparse is applied.  Raw cache
    bytes, reparse state, formal annual/quarterly CSVs, and coverage artifacts
    remain untouched; only temporary paired CSVs are created and removed.
    """
    cache_dir = Path(cache_dir)
    output = Path(output)
    quarterly_output = Path(quarterly_output)
    if required_symbols is None:
        raise ValueError(
            "full rebuild dry runs require an explicit immutable required_symbols scope"
        )
    _validate_companyfacts_full_rebuild_provenance_inputs(
        expected_cache_manifest_sha256,
        expected_rebuild_recipe_sha256,
    )
    recipe_verification = verify_companyfacts_full_rebuild_recipe(
        expected_rebuild_recipe_sha256
    )
    formal_before = {
        output: _file_sha256(output) if output.exists() else None,
        quarterly_output: (
            _file_sha256(quarterly_output)
            if quarterly_output.exists() else None
        ),
    }
    with companyfacts_cache_lock(cache_dir):
        _recover_pending_raw_cache_checkpoint(cache_dir)
        if expected_cache_manifest_sha256 is not None:
            actual_manifest_sha256 = _file_sha256(cache_dir / "manifest.json")
            if actual_manifest_sha256 != expected_cache_manifest_sha256:
                raise ValueError(
                    "Company Facts cache manifest does not match the declared "
                    "immutable snapshot"
                )
        cache_audit = audit_companyfacts_cache_coverage(
            cache_dir,
            required_symbols,
            include_payload_profiles=False,
        )
        missing = cache_audit["unresolved_cache_symbols"]
        if missing:
            raise RuntimeError(
                "Raw Company Facts sources are incomplete for full rebuild "
                f"({len(missing)} missing): {', '.join(missing[:20])}"
                + (" ..." if len(missing) > 20 else "")
            )
        parsed_scope_tickers = sorted(
            {
                str(symbol).strip().upper()
                for symbol in required_symbols
                if str(symbol).strip()
            }
            & cached_companyfacts_symbols(cache_dir)
        )
        if not parsed_scope_tickers:
            raise RuntimeError(
                "No manifest-bound raw Company Facts payloads exist for the "
                "declared full-rebuild scope"
            )
        cached_companyfacts_cik_map(cache_dir)
        with tempfile.TemporaryDirectory(
            prefix="companyfacts_full_reparse_"
        ) as temporary_dir:
            temporary_root = Path(temporary_dir)
            temporary_annual = temporary_root / output.name
            temporary_quarterly = temporary_root / quarterly_output.name
            rebuild = _reparse_companyfacts_cache_unlocked(
                cache_dir,
                temporary_annual,
                temporary_quarterly,
                parsed_scope_tickers,
                False,
                replace_complete_outputs=True,
            )
            annual_comparison = _full_reparse_output_comparison(
                output,
                temporary_annual,
                include_ticker_deltas=include_ticker_deltas,
            )
            quarterly_comparison = _full_reparse_output_comparison(
                quarterly_output,
                temporary_quarterly,
                include_ticker_deltas=include_ticker_deltas,
            )
    formal_after = {
        output: _file_sha256(output) if output.exists() else None,
        quarterly_output: (
            _file_sha256(quarterly_output)
            if quarterly_output.exists() else None
        ),
    }
    if formal_after != formal_before:
        raise RuntimeError(
            "Formal fundamentals changed during a full-reparse dry run"
        )
    formal_content_match = bool(
        annual_comparison["formal_content_match"]
        and quarterly_comparison["formal_content_match"]
    )
    return {
        "mode": "offline_cache_full_rebuild_dry_run",
        "dry_run": True,
        "cache_dir": str(cache_dir),
        "cache_manifest": rebuild["cache_manifest"],
        "cache_manifest_verified": rebuild["cache_manifest_verified"],
        "cache_manifest_verification_scope": rebuild[
            "cache_manifest_verification_scope"
        ],
        "cache_manifest_verified_payload_count": rebuild[
            "cache_manifest_verified_payload_count"
        ],
        "cached_ciks": rebuild["cached_ciks"],
        "parsed_symbol_bindings": rebuild["parsed_symbol_bindings"],
        "validated_foreign_symbols": rebuild["validated_foreign_symbols"],
        "required_symbol_count": cache_audit["required_symbol_count"],
        "parsed_scope_symbol_count": len(parsed_scope_tickers),
        "required_symbols_sha256": companyfacts_full_rebuild_symbol_sha256(
            required_symbols
        ),
        "declared_cache_manifest_sha256": expected_cache_manifest_sha256,
        "rebuild_recipe": recipe_verification["recipe"],
        "rebuild_recipe_sha256": recipe_verification["recipe_sha256"],
        "declared_rebuild_recipe_sha256": recipe_verification[
            "declared_recipe_sha256"
        ],
        "rebuild_recipe_matched": recipe_verification["recipe_matched"],
        "cache_symbol_coverage": cache_audit["cache_symbol_coverage"],
        "known_unavailable_required_symbol_count": cache_audit[
            "known_unavailable_required_symbol_count"
        ],
        "known_unavailable_required_symbols": cache_audit[
            "known_unavailable_required_symbols"
        ],
        "unresolved_cache_symbol_count": cache_audit[
            "unresolved_cache_symbol_count"
        ],
        "cache_resolution_coverage": cache_audit[
            "cache_resolution_coverage"
        ],
        "formal_outputs_read": True,
        "formal_outputs_written": False,
        "annual_output_written": False,
        "quarterly_output_written": False,
        "parsed_outputs_written": False,
        "temporary_outputs_removed": True,
        "formal_outputs_unchanged": True,
        "formal_content_match": formal_content_match,
        "formal_rebuild_gate": (
            "PASS" if formal_content_match
            else "BLOCKED_FORMAL_CONTENT_MISMATCH"
        ),
        "annual_comparison": annual_comparison,
        "quarterly_comparison": quarterly_comparison,
    }


def reparse_companyfacts_cache(
    cache_dir: Path = SEC_COMPANYFACTS_CACHE_DIR,
    output: Path = Path(POINT_IN_TIME_FUNDAMENTALS_FILE),
    quarterly_output: Path = Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE),
    tickers: list[str] | None = None,
    required_symbols: list[str] | set[str] | None = None,
    include_validated_foreign_quarters: bool = False,
    skip_unchanged: bool = False,
    limit: int | None = None,
    expected_cache_manifest_sha256: str | None = None,
    expected_rebuild_recipe_sha256: str | None = None,
) -> dict:
    """Reparse cache without allowing a partial full overwrite.

    Passing ``tickers`` selects a non-destructive incremental upsert.  With
    ``tickers=None`` the raw cache must cover every required symbol before
    either parsed output is replaced.  By default, full rebuild coverage must
    use an explicit immutable ticker scope; it must never be inferred from a
    mutable current-universe file.  ``expected_cache_manifest_sha256`` binds
    a caller to one immutable raw-cache snapshot. Priority-driven reparses
    can skip unchanged payload/parser fingerprints and resume in bounded
    batches.
    """
    if limit is not None and limit <= 0:
        raise ValueError("reparse limit must be a positive integer")
    with companyfacts_cache_lock(cache_dir):
        _recover_pending_raw_cache_checkpoint(cache_dir)
        if include_validated_foreign_quarters and tickers is None:
            raise ValueError(
                "validated foreign quarters require incremental tickers"
            )
        requested_tickers = (
            list(dict.fromkeys(
                str(ticker).strip().upper()
                for ticker in (tickers or [])
                if str(ticker).strip()
            ))
            if tickers is not None else None
        )
        candidate_ticker_count = (
            len(requested_tickers)
            if requested_tickers is not None else None
        )
        unchanged_tickers = []
        if skip_unchanged and requested_tickers is not None:
            requested_tickers, unchanged_tickers = (
                select_changed_companyfacts_reparse_tickers(
                    requested_tickers, cache_dir
                )
            )
        changed_ticker_count = (
            len(requested_tickers)
            if requested_tickers is not None else None
        )
        deferred_changed_tickers = []
        if limit is not None and requested_tickers is not None:
            deferred_changed_tickers = requested_tickers[limit:]
            requested_tickers = requested_tickers[:limit]
        if tickers is not None and not requested_tickers:
            cached_cik_chains = cached_companyfacts_cik_chains_for_symbols(
                unchanged_tickers, cache_dir
            )
            unchanged_paths = {
                _companyfacts_cache_path(cik, Path(cache_dir)).name
                for ticker in unchanged_tickers
                for cik in cached_cik_chains[ticker]
            }
            manifest = verify_companyfacts_cache_manifest(
                cache_dir,
                payload_paths=unchanged_paths,
            )
            return {
                "mode": "offline_cache_incremental_noop",
                "merge_policy": "non_destructive_upsert",
                "requested_tickers": [],
                "candidate_ticker_count": candidate_ticker_count,
                "changed_ticker_count": changed_ticker_count,
                "selected_ticker_count": 0,
                "deferred_changed_ticker_count": len(
                    deferred_changed_tickers
                ),
                "next_deferred_ticker": (
                    deferred_changed_tickers[0]
                    if deferred_changed_tickers else None
                ),
                "batch_complete": not deferred_changed_tickers,
                "unchanged_tickers": unchanged_tickers,
                "unchanged_ticker_count": len(unchanged_tickers),
                "cache_dir": str(cache_dir),
                "cache_manifest": manifest["manifest"],
                "cache_manifest_verified": manifest["verified"],
                "cache_manifest_verification_scope": manifest[
                    "verification_scope"
                ],
                "cache_manifest_verified_payload_count": manifest[
                    "verified_payload_count"
                ],
                "parsed_outputs_written": False,
                "output": str(output),
                "quarterly_output": str(quarterly_output),
            }
        cache_audit = None
        parsed_scope_tickers = None
        recipe_verification = None
        if tickers is None:
            if required_symbols is None:
                raise ValueError(
                    "full rebuild requires an explicit immutable required_symbols scope"
                )
            _validate_companyfacts_full_rebuild_provenance_inputs(
                expected_cache_manifest_sha256,
                expected_rebuild_recipe_sha256,
            )
            recipe_verification = verify_companyfacts_full_rebuild_recipe(
                expected_rebuild_recipe_sha256
            )
            if expected_cache_manifest_sha256 is not None:
                actual_manifest_sha256 = _file_sha256(
                    Path(cache_dir) / "manifest.json"
                )
                if actual_manifest_sha256 != expected_cache_manifest_sha256:
                    raise ValueError(
                        "Company Facts cache manifest does not match the declared "
                        "immutable snapshot"
                    )
            cache_audit = audit_companyfacts_cache_coverage(
                cache_dir,
                required_symbols,
                include_payload_profiles=False,
            )
            missing = cache_audit["unresolved_cache_symbols"]
            if missing:
                raise RuntimeError(
                    "Raw Company Facts sources are incomplete for full rebuild "
                    f"({len(missing)} missing): {', '.join(missing[:20])}"
                    + (" ..." if len(missing) > 20 else "")
                )
            parsed_scope_tickers = sorted(
                {
                    str(symbol).strip().upper()
                    for symbol in required_symbols
                    if str(symbol).strip()
                }
                & cached_companyfacts_symbols(cache_dir)
            )
            if not parsed_scope_tickers:
                raise RuntimeError(
                    "No manifest-bound raw Company Facts payloads exist for the "
                    "declared full-rebuild scope"
                )
            # Reject an undeclared ticker-to-multiple-CIK collision before
            # temporary parsing or either formal output can be written.
            cached_companyfacts_cik_map(cache_dir)
        result = _reparse_companyfacts_cache_unlocked(
            cache_dir,
            output,
            quarterly_output,
            (
                parsed_scope_tickers
                if parsed_scope_tickers is not None else requested_tickers
            ),
            include_validated_foreign_quarters,
            replace_complete_outputs=parsed_scope_tickers is not None,
        )
        if recipe_verification is not None:
            result.update({
                "rebuild_recipe": recipe_verification["recipe"],
                "rebuild_recipe_sha256": recipe_verification["recipe_sha256"],
                "declared_rebuild_recipe_sha256": recipe_verification[
                    "declared_recipe_sha256"
                ],
                "rebuild_recipe_matched": recipe_verification["recipe_matched"],
            })
        result["unchanged_tickers"] = unchanged_tickers
        result["unchanged_ticker_count"] = len(unchanged_tickers)
        if requested_tickers is not None:
            result.update({
                "candidate_ticker_count": candidate_ticker_count,
                "changed_ticker_count": changed_ticker_count,
                "selected_ticker_count": len(requested_tickers),
                "deferred_changed_ticker_count": len(
                    deferred_changed_tickers
                ),
                "next_deferred_ticker": (
                    deferred_changed_tickers[0]
                    if deferred_changed_tickers else None
                ),
                "batch_complete": not deferred_changed_tickers,
            })
        state_tickers = (
            parsed_scope_tickers
            if parsed_scope_tickers is not None
            else requested_tickers
            if requested_tickers is not None
            else sorted(cached_companyfacts_cik_map(cache_dir))
        )
        if state_tickers:
            result["reparse_state"] = str(
                record_companyfacts_reparse_state(
                    state_tickers, cache_dir
                )
            )
            result["reparse_state_ticker_count"] = len(state_tickers)
        if cache_audit is not None:
            result["parsed_payload_manifest_verification_scope"] = result[
                "cache_manifest_verification_scope"
            ]
            result["parsed_payload_manifest_verified_payload_count"] = result[
                "cache_manifest_verified_payload_count"
            ]
            # Full replacement preflight hashes the entire snapshot manifest.
            # The parser intentionally reads only scope-bound payloads, but
            # callers should still see that the full immutable input was
            # verified before a replace-complete operation.
            result["cache_manifest_verification_scope"] = "full"
            result["cache_manifest_verified_payload_count"] = cache_audit[
                "cached_ciks"
            ]
            result["required_symbol_count"] = cache_audit[
                "required_symbol_count"
            ]
            result["parsed_scope_symbol_count"] = len(parsed_scope_tickers)
            result["required_symbols_sha256"] = (
                companyfacts_full_rebuild_symbol_sha256(required_symbols)
            )
            result["declared_cache_manifest_sha256"] = (
                expected_cache_manifest_sha256
            )
            result["cache_symbol_coverage"] = cache_audit[
                "cache_symbol_coverage"
            ]
            result["known_unavailable_required_symbol_count"] = cache_audit[
                "known_unavailable_required_symbol_count"
            ]
            result["known_unavailable_required_symbols"] = cache_audit[
                "known_unavailable_required_symbols"
            ]
            result["unresolved_cache_symbol_count"] = cache_audit[
                "unresolved_cache_symbol_count"
            ]
            result["cache_resolution_coverage"] = cache_audit[
                "cache_resolution_coverage"
            ]
        return result


def _update_fundamentals_unlocked(
    as_of: date,
    workers: int = 4,
    limit: int | None = None,
    refresh_after_days: int = 30,
    output: Path = Path(POINT_IN_TIME_FUNDAMENTALS_FILE),
    quarterly_output: Path = Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE),
    force: bool = False,
    tickers: list[str] | None = None,
    cik_overrides: dict[str, int] | None = None,
    cache_dir: Path = SEC_COMPANYFACTS_CACHE_DIR,
    offline_cache: bool = False,
    cache_missing_only: bool = False,
    refresh_priority: dict | None = None,
) -> dict:
    if limit is not None and limit <= 0:
        raise ValueError("limit must be a positive integer")
    cache_dir = Path(cache_dir)
    manifest_path = cache_dir / "manifest.json"
    cache_files_before = _companyfacts_cache_files(cache_dir)
    _recover_pending_raw_cache_checkpoint(cache_dir)
    if manifest_path.exists():
        verify_companyfacts_cache_manifest(cache_dir)
    elif cache_files_before:
        raise RuntimeError(
            "Company Facts cache contains payloads but has no manifest; "
            "refusing to rebaseline unverified cache bytes"
        )
    universe_frame = investable_common_equities(pd.read_csv(NASDAQ_300M_STOCK_LIST_FILE))
    universe = universe_frame["Symbol"].dropna().astype(str).str.upper().tolist()
    if offline_cache:
        cik_map = cached_companyfacts_cik_map(cache_dir)
        ticker_map_snapshot = None
    else:
        cik_map, ticker_map_snapshot = fetch_sec_ticker_map_snapshot(
            cache_dir
        )
        write_companyfacts_cache_manifest(cache_dir)
    historical_cik_map = load_historical_ticker_ciks(cache_dir)
    cik_map.update(historical_cik_map)
    cik_map.update({
        str(ticker).upper(): int(cik)
        for ticker, cik in (cik_overrides or {}).items()
    })
    unmapped_universe_tickers = unmapped_fundamentals_tickers(
        universe, cik_map
    )
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
    audit_path = Path(FUNDAMENTALS_COVERAGE_FILE)
    prior_audit = (
        json.loads(audit_path.read_text(encoding="utf-8"))
        if audit_path.exists() else {}
    )
    if not state and len(existing):
        last_fetch = existing.groupby("ticker")["fetched_at"].max()
        state.update({
            str(ticker): {"last_attempt": value.date().isoformat(), "status": "has_data"}
            for ticker, value in last_fetch.items() if pd.notna(value)
        })
        for failure in prior_audit.get("failures", []):
            state[str(failure["ticker"]).upper()] = {
                "last_attempt": as_of.isoformat(), "status": "no_data_or_failed"
            }
    if cache_missing_only and prior_audit.get("cache_missing_only"):
        prior_attempt = prior_audit.get("as_of", as_of.isoformat())
        for failure in prior_audit.get("failures", []):
            ticker = str(failure["ticker"]).upper()
            entry = dict(state.get(ticker) or {})
            entry.setdefault("cache_last_attempt", prior_attempt)
            entry.setdefault("cache_status", "fetch_failed")
            entry.setdefault("cache_failure_reason", failure.get("reason"))
            state[ticker] = entry
    priority_tickers = (refresh_priority or {}).get("tickers")
    requested_universe = build_requested_refresh_universe(
        universe,
        tickers,
        priority_tickers,
        cache_missing_only,
    )
    priority_only_tickers = sorted(
        set(requested_universe) - set(universe)
    )
    unknown_tickers = unmapped_fundamentals_tickers(
        requested_universe, cik_map
    )
    if unknown_tickers and tickers:
        raise ValueError(
            "No SEC CIK mapping for explicitly requested tickers: "
            + ", ".join(unknown_tickers)
            + ". Supply --ticker-cik TICKER=CIK for historical symbols."
        )
    requested_universe = [
        ticker for ticker in requested_universe if ticker in cik_map
    ]
    requested_universe, matched_priority_tickers = prioritize_refresh_tickers(
        requested_universe,
        priority_tickers,
    )
    cached_symbols_before = (
        cached_companyfacts_symbols(cache_dir)
        if cache_missing_only else set()
    )
    cached_ciks_before = (
        cached_companyfacts_ciks(cache_dir)
        if cache_missing_only else set()
    )
    cached_cik_alias_tickers = (
        [
            ticker
            for ticker in requested_universe
            if (
                ticker not in cached_symbols_before
                and int(cik_map[ticker]) in cached_ciks_before
            )
        ]
        if cache_missing_only else []
    )
    effective_cached_symbols = (
        cached_symbols_before | set(cached_cik_alias_tickers)
    )
    eligible_before_limit = select_fundamentals_refresh_tickers(
        requested_universe,
        cik_map,
        state,
        as_of,
        refresh_after_days,
        force=force,
        cache_missing_only=cache_missing_only,
        cached_symbols=effective_cached_symbols,
    )
    requested = limit_refresh_tickers_by_cik(
        eligible_before_limit,
        cik_map,
        limit,
    )
    if cache_missing_only:
        requested = expand_selected_cik_aliases(
            requested,
            requested_universe,
            cik_map,
            excluded_symbols=effective_cached_symbols,
        )
    if cache_missing_only:
        deferred_tickers, cache_cooldown_tickers = (
            classify_cache_refresh_backlog(
                requested_universe,
                eligible_before_limit,
                requested,
                effective_cached_symbols,
            )
        )
    else:
        deferred_tickers, cache_cooldown_tickers = [], []
    rows, quarterly_rows, failures = [], [], []
    requested_by_cik = _group_tickers_by_cik(requested, cik_map)
    cached_aliases_by_cik = _group_tickers_by_cik(
        cached_cik_alias_tickers, cik_map
    )
    for cik, symbols in cached_aliases_by_cik.items():
        try:
            cached_results = parse_and_bind_cached_companyfacts_symbols(
                symbols, cik, cache_dir
            )
            for ticker, (annual_frame, quarterly_frame) in cached_results.items():
                if len(annual_frame):
                    rows.append(annual_frame)
                if len(quarterly_frame):
                    quarterly_rows.append(quarterly_frame)
                if not len(annual_frame) and not len(quarterly_frame):
                    failures.append({
                        "ticker": ticker,
                        "reason": "no_cached_sec_fundamentals",
                    })
        except Exception as exc:
            failures.extend(
                {"ticker": ticker, "reason": str(exc)}
                for ticker in symbols
            )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                fetch_sec_fundamentals_for_symbols,
                symbols,
                cik,
                3,
                cache_dir,
                offline_cache,
            ): (cik, symbols)
            for cik, symbols in requested_by_cik.items()
        }
        for future in as_completed(futures):
            _cik, symbols = futures[future]
            try:
                results = future.result()
                for ticker, (annual_frame, quarterly_frame) in results.items():
                    if len(annual_frame):
                        rows.append(annual_frame)
                    if len(quarterly_frame):
                        quarterly_rows.append(quarterly_frame)
                    if not len(annual_frame) and not len(quarterly_frame):
                        failures.append({
                            "ticker": ticker,
                            "reason": "no_sec_fundamentals",
                        })
            except Exception as exc:
                failures.extend(
                    {"ticker": ticker, "reason": str(exc)}
                    for ticker in symbols
                )
    incoming = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=OUTPUT_COLUMNS)
    quarterly_incoming = (
        pd.concat(quarterly_rows, ignore_index=True)
        if quarterly_rows else pd.DataFrame(columns=OUTPUT_COLUMNS)
    )
    successful_tickers = set(incoming["ticker"].astype(str)) | set(
        quarterly_incoming["ticker"].astype(str)
    )
    merged = integrate_refreshed_fundamentals(
        existing,
        incoming,
        successful_tickers,
        non_destructive=cache_missing_only,
    )
    quarterly_merged = integrate_refreshed_fundamentals(
        quarterly_existing,
        quarterly_incoming,
        successful_tickers,
        non_destructive=cache_missing_only,
    )
    outputs_written = bool(successful_tickers)
    if outputs_written:
        write_fundamentals_pair(
            merged, output, quarterly_merged, quarterly_output
        )
    manifest_path = write_companyfacts_cache_manifest(cache_dir)
    cached_symbols_after = (
        cached_companyfacts_symbols(cache_dir)
        if cache_missing_only else set()
    )
    processed_tickers = list(dict.fromkeys([
        *requested, *cached_cik_alias_tickers
    ]))
    result_by_ticker = {
        ticker: ("has_data" if ticker in successful_tickers else "no_data_or_failed")
        for ticker in processed_tickers
    }
    failure_reasons = {
        str(failure["ticker"]).upper(): failure["reason"]
        for failure in failures
    }
    for ticker, status in result_by_ticker.items():
        entry = dict(state.get(ticker) or {})
        entry.update({
            "last_attempt": as_of.isoformat(),
            "status": status,
        })
        if cache_missing_only:
            raw_cached = ticker in cached_symbols_after
            failure_reason = (
                None if raw_cached else failure_reasons.get(ticker)
            )
            entry.update({
                "cache_last_attempt": as_of.isoformat(),
                "cache_status": (
                    "raw_cached"
                    if raw_cached
                    else (
                        "companyfacts_not_available"
                        if "HTTP Error 404: Not Found" in str(failure_reason)
                        else "fetch_failed"
                    )
                ),
                "cache_failure_reason": failure_reason,
            })
        state[ticker] = entry
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
        "companyfacts_cache_dir": str(cache_dir),
        "offline_cache": offline_cache,
        "sec_ticker_map_snapshot": ticker_map_snapshot,
        "historical_ticker_cik_count": len(historical_cik_map),
        "historical_ticker_cik_cache": str(
            historical_ticker_cik_path(cache_dir)
        ),
        "cache_missing_only": cache_missing_only,
        "merge_policy": (
            "non_destructive_upsert"
            if cache_missing_only else "replace_successful_ticker_histories"
        ),
        "requested_tickers": requested,
        "cached_cik_alias_reparse_ticker_count": len(
            cached_cik_alias_tickers
        ),
        "cached_cik_alias_reparse_cik_count": len(cached_aliases_by_cik),
        "cached_cik_alias_reparse_tickers": cached_cik_alias_tickers,
        "refresh_priority": (
            {
                key: value
                for key, value in refresh_priority.items()
                if key != "tickers"
            }
            if refresh_priority else None
        ),
        "matched_refresh_priority_ticker_count": matched_priority_tickers,
        "priority_only_ticker_count": len(priority_only_tickers),
        "priority_only_unmapped_tickers": sorted(
            set(priority_only_tickers) & set(unknown_tickers)
        ),
        "eligible_before_limit_ticker_count": len(eligible_before_limit),
        "eligible_before_limit_cik_count": len(
            _group_tickers_by_cik(eligible_before_limit, cik_map)
        ),
        "deferred_by_limit_ticker_count": len(deferred_tickers),
        "deferred_by_limit_cik_count": len(
            set(_group_tickers_by_cik(deferred_tickers, cik_map))
            - set(requested_by_cik)
        ),
        "deferred_by_limit_ticker_sample": deferred_tickers[:20],
        "cached_symbol_count_before": len(cached_symbols_before),
        "cached_symbol_count_after": len(cached_symbols_after),
        "cache_cooldown_ticker_count": len(cache_cooldown_tickers),
        "cache_cooldown_ticker_sample": cache_cooldown_tickers[:20],
        "parsed_outputs_written": outputs_written,
        "unmapped_universe_ticker_count": len(unmapped_universe_tickers),
        "unmapped_universe_tickers": unmapped_universe_tickers,
        "requested_ciks": len(requested_by_cik),
    })
    audit["companyfacts_cache_manifest"] = str(manifest_path)
    audit["audit_output"] = str(audit_path)
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
    return audit


def update_fundamentals(
    as_of: date,
    workers: int = 4,
    limit: int | None = None,
    refresh_after_days: int = 30,
    output: Path = Path(POINT_IN_TIME_FUNDAMENTALS_FILE),
    quarterly_output: Path = Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE),
    force: bool = False,
    tickers: list[str] | None = None,
    cik_overrides: dict[str, int] | None = None,
    cache_dir: Path = SEC_COMPANYFACTS_CACHE_DIR,
    offline_cache: bool = False,
    cache_missing_only: bool = False,
    refresh_priority: dict | None = None,
) -> dict:
    with companyfacts_cache_lock(cache_dir):
        return _update_fundamentals_unlocked(
            as_of=as_of,
            workers=workers,
            limit=limit,
            refresh_after_days=refresh_after_days,
            output=output,
            quarterly_output=quarterly_output,
            force=force,
            tickers=tickers,
            cik_overrides=cik_overrides,
            cache_dir=cache_dir,
            offline_cache=offline_cache,
            cache_missing_only=cache_missing_only,
            refresh_priority=refresh_priority,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Maximum unique SEC CIK requests per online batch (default: 25); "
            "shared-CIK symbols stay together."
        ),
    )
    parser.add_argument("--refresh-after-days", type=int, default=30)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--offline-cache",
        action="store_true",
        help=(
            "Do not contact SEC; reparse requested CIKs from the local raw "
            "Company Facts cache. Usually combine with --force."
        ),
    )
    parser.add_argument(
        "--cache-missing-only",
        action="store_true",
        help=(
            "Fetch only requested/current-universe symbols absent from the "
            "raw cache. Combine with --limit for resumable batches."
        ),
    )
    parser.add_argument(
        "--raw-cache-only",
        action="store_true",
        help=(
            "With --cache-missing-only, update only raw SEC payloads, "
            "ticker-map snapshots, cache manifest, and resumable cache "
            "state. Never read or write parsed annual/quarterly CSV outputs."
        ),
    )
    parser.add_argument(
        "--cache-priority-file",
        type=Path,
        help=(
            "With --cache-missing-only, request tickers in this CSV first. "
            "Requires ticker; fetch_priority_rank, "
            "cache_refresh_priority_rank, or priority_rank controls order."
        ),
    )
    parser.add_argument(
        "--cache-audit-only",
        action="store_true",
        help=(
            "Verify the raw-cache manifest and report current universe/ticker "
            "coverage without network access or file writes."
        ),
    )
    parser.add_argument(
        "--reparse-cache",
        choices=("incremental", "full"),
        help=(
            "Reparse the local raw Company Facts cache with no SEC network "
            "access. 'incremental' non-destructively upserts only --tickers; "
            "'full' replaces both parsed files and requires full universe "
            "coverage."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "With --reparse-cache full, rebuild into disposable files and "
            "report differences without writing formal outputs or state."
        ),
    )
    parser.add_argument(
        "--cache-snapshot",
        type=Path,
        help=(
            "With --reparse-cache full, use this immutable Company Facts raw "
            "snapshot instead of the refreshable active cache."
        ),
    )
    parser.add_argument(
        "--full-rebuild-scope",
        type=Path,
        help=(
            "With --reparse-cache full, explicit frozen ticker-scope JSON "
            "bound to --cache-snapshot."
        ),
    )
    parser.add_argument(
        "--reparse-priority-file",
        type=Path,
        help=(
            "With --reparse-cache incremental, select rows having "
            "reparse_priority_rank, skip unchanged payload/parser "
            "fingerprints, and optionally apply --limit."
        ),
    )
    parser.add_argument(
        "--include-validated-foreign-quarters",
        action="store_true",
        help=(
            "With incremental cache reparse, include foreign 6-K/20-F "
            "quarters only when same-currency, timeliness, continuity, and "
            "concept-transition checks all pass."
        ),
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        help="Refresh only these tickers; historical symbols may need --ticker-cik.",
    )
    parser.add_argument(
        "--resolve-ticker-ciks",
        nargs="+",
        help=(
            "Resolve historical/inactive tickers through the official SEC "
            "company lookup and cache the mappings; performs no data refresh."
        ),
    )
    parser.add_argument(
        "--ticker-cik",
        nargs="*",
        default=[],
        metavar="TICKER=CIK",
        help="Explicit SEC CIK mapping for historical or inactive tickers.",
    )
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args()
    # The bounded default applies only to online refreshes.  Reparse modes
    # use ``None`` unless a priority queue explicitly supplies its own limit;
    # otherwise a refresh default would trip their mode-specific validation.
    online_limit = (
        args.limit
        if args.limit is not None or args.offline_cache
        else SEC_REFRESH_DEFAULT_CIK_BATCH_LIMIT
    )
    as_of = date.fromisoformat(args.as_of)
    if args.audit_only and args.reparse_cache:
        parser.error("--audit-only and --reparse-cache are mutually exclusive")
    if args.reparse_cache and (
        args.offline_cache
        or args.force
        or args.ticker_cik
        or args.workers != 4
        or args.refresh_after_days != 30
    ):
        parser.error(
            "--reparse-cache cannot be combined with online-refresh options "
            "(--offline-cache, --force, --ticker-cik, --workers, or "
            "--refresh-after-days)"
        )
    if args.dry_run and args.reparse_cache != "full":
        parser.error("--dry-run requires --reparse-cache full")
    if (
        args.cache_snapshot is not None or args.full_rebuild_scope is not None
    ) and args.reparse_cache != "full":
        parser.error(
            "--cache-snapshot and --full-rebuild-scope require "
            "--reparse-cache full"
        )
    if args.cache_audit_only and (
        args.audit_only
        or args.reparse_cache
        or args.cache_missing_only
        or args.offline_cache
        or args.force
        or args.limit is not None
        or args.ticker_cik
        or args.cache_priority_file
        or args.reparse_priority_file
        or args.include_validated_foreign_quarters
        or args.resolve_ticker_ciks
    ):
        parser.error(
            "--cache-audit-only cannot be combined with update, reparse, "
            "or parsed-data audit modes"
        )
    if args.cache_missing_only and args.reparse_cache:
        parser.error("--cache-missing-only cannot be used with --reparse-cache")
    if args.cache_missing_only and args.audit_only:
        parser.error("--cache-missing-only cannot be used with --audit-only")
    if args.cache_missing_only and args.offline_cache:
        parser.error("--cache-missing-only requires SEC network access")
    if args.raw_cache_only and not args.cache_missing_only:
        parser.error("--raw-cache-only requires --cache-missing-only")
    if args.cache_priority_file and not args.cache_missing_only:
        parser.error("--cache-priority-file requires --cache-missing-only")
    if args.reparse_priority_file and (
        args.reparse_cache != "incremental"
    ):
        parser.error(
            "--reparse-priority-file requires --reparse-cache incremental"
        )
    if args.reparse_priority_file and args.tickers:
        parser.error(
            "--reparse-priority-file and --tickers are mutually exclusive"
        )
    if args.reparse_cache and args.limit is not None and not (
        args.reparse_priority_file
    ):
        parser.error(
            "--limit with --reparse-cache requires --reparse-priority-file"
        )
    if args.include_validated_foreign_quarters and (
        args.reparse_cache != "incremental"
    ):
        parser.error(
            "--include-validated-foreign-quarters requires "
            "--reparse-cache incremental"
        )
    if args.resolve_ticker_ciks and (
        args.audit_only
        or args.reparse_cache
        or args.cache_missing_only
        or args.offline_cache
        or args.force
        or args.limit is not None
        or args.tickers
        or args.ticker_cik
        or args.cache_priority_file
        or args.reparse_priority_file
    ):
        parser.error(
            "--resolve-ticker-ciks cannot be combined with refresh, "
            "reparse, or audit options"
        )
    refresh_priority = (
        load_refresh_priority_file(args.cache_priority_file)
        if args.cache_priority_file else None
    )
    cik_overrides = {}
    for item in args.ticker_cik:
        ticker, separator, cik = item.partition("=")
        if (
            not separator
            or not ticker.strip()
            or not cik.strip().isdigit()
        ):
            parser.error(
                f"invalid --ticker-cik value {item!r}; "
                "expected TICKER=CIK"
            )
        cik_overrides[ticker.strip().upper()] = int(cik)

    if args.resolve_ticker_ciks:
        result = resolve_historical_ticker_ciks(
            args.resolve_ticker_ciks
        )
    elif args.cache_audit_only:
        if args.tickers:
            required_symbols = args.tickers
        else:
            universe_frame = investable_common_equities(
                pd.read_csv(NASDAQ_300M_STOCK_LIST_FILE)
            )
            required_symbols = (
                universe_frame["Symbol"]
                .dropna()
                .astype(str)
                .str.upper()
                .tolist()
            )
        result = audit_companyfacts_cache_coverage(
            SEC_COMPANYFACTS_CACHE_DIR, required_symbols
        )
    elif args.reparse_cache:
        if (
            args.reparse_cache == "incremental"
            and not args.tickers
            and not args.reparse_priority_file
        ):
            parser.error(
                "--reparse-cache incremental requires --tickers or "
                "--reparse-priority-file"
            )
        if args.reparse_cache == "full" and args.tickers:
            parser.error("--reparse-cache full does not accept --tickers")
        full_rebuild_inputs = None
        if args.reparse_cache == "full":
            if args.cache_snapshot is None or args.full_rebuild_scope is None:
                parser.error(
                    "--reparse-cache full requires both --cache-snapshot and "
                    "--full-rebuild-scope"
                )
            full_rebuild_inputs = load_companyfacts_full_rebuild_inputs(
                args.cache_snapshot,
                args.full_rebuild_scope,
            )
            if not full_rebuild_inputs["rebuild_recipe_bound"]:
                parser.error(
                    "--reparse-cache full requires a parser-recipe-bound "
                    "format-v2 --full-rebuild-scope"
                )
        if args.dry_run:
            result = dry_run_companyfacts_full_reparse(
                cache_dir=full_rebuild_inputs["cache_dir"],
                required_symbols=full_rebuild_inputs["required_symbols"],
                expected_cache_manifest_sha256=full_rebuild_inputs[
                    "cache_manifest_sha256"
                ],
                expected_rebuild_recipe_sha256=full_rebuild_inputs[
                    "rebuild_recipe_sha256"
                ],
            )
        else:
            reparse_priority = (
                load_reparse_priority_file(args.reparse_priority_file)
                if args.reparse_priority_file else None
            )
            result = reparse_companyfacts_cache(
                cache_dir=(
                    full_rebuild_inputs["cache_dir"]
                    if full_rebuild_inputs is not None
                    else SEC_COMPANYFACTS_CACHE_DIR
                ),
                tickers=(
                    reparse_priority["tickers"]
                    if reparse_priority
                    else (
                        args.tickers
                        if args.reparse_cache == "incremental" else None
                    )
                ),
                include_validated_foreign_quarters=(
                    args.include_validated_foreign_quarters
                ),
                skip_unchanged=bool(reparse_priority),
                limit=args.limit if reparse_priority else None,
                required_symbols=(
                    full_rebuild_inputs["required_symbols"]
                    if full_rebuild_inputs is not None
                    else None
                ),
                expected_cache_manifest_sha256=(
                    full_rebuild_inputs["cache_manifest_sha256"]
                    if full_rebuild_inputs is not None
                    else None
                ),
                expected_rebuild_recipe_sha256=(
                    full_rebuild_inputs["rebuild_recipe_sha256"]
                    if full_rebuild_inputs is not None
                    else None
                ),
            )
            if reparse_priority:
                result["reparse_priority"] = reparse_priority
        if full_rebuild_inputs is not None:
            result["full_rebuild_snapshot"] = {
                "snapshot_dir": str(full_rebuild_inputs["cache_dir"]),
                "snapshot_id": full_rebuild_inputs["snapshot_id"],
                "cache_manifest_sha256": full_rebuild_inputs[
                    "cache_manifest_sha256"
                ],
            }
            result["full_rebuild_scope"] = {
                "scope_path": full_rebuild_inputs["scope"]["scope_path"],
                "required_symbol_count": len(
                    full_rebuild_inputs["required_symbols"]
                ),
                "required_symbols_sha256": full_rebuild_inputs["scope"][
                    "required_symbols_sha256"
                ],
                "rebuild_recipe_sha256": full_rebuild_inputs[
                    "rebuild_recipe_sha256"
                ],
            }
    elif args.raw_cache_only:
        result = populate_missing_companyfacts_cache(
            as_of,
            workers=args.workers,
            limit=online_limit,
            refresh_after_days=args.refresh_after_days,
            force=args.force,
            tickers=args.tickers,
            cik_overrides=cik_overrides,
            refresh_priority=refresh_priority,
        )
    elif args.audit_only:
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
            as_of,
            args.workers,
            online_limit,
            args.refresh_after_days,
            force=args.force,
            tickers=args.tickers,
            cik_overrides=cik_overrides,
            offline_cache=args.offline_cache,
            cache_missing_only=args.cache_missing_only,
            refresh_priority=refresh_priority,
        )
    compact = {
        key: value for key, value in result.items()
        if key not in {
            "missing_or_incomplete",
            "missing_cache_symbols",
            "failures",
        }
    }
    if "missing_or_incomplete" in result:
        compact["missing_or_incomplete_count"] = len(
            result["missing_or_incomplete"]
        )
    if "failures" in result:
        compact["failure_count"] = len(result["failures"])
        compact["failure_sample"] = result["failures"][:20]
    if "missing_cache_symbols" in result:
        compact["missing_cache_symbol_count"] = len(
            result["missing_cache_symbols"]
        )
        compact["missing_cache_symbol_sample"] = (
            result["missing_cache_symbols"][:20]
        )
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
