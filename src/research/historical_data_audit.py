"""Audit retained price histories that end before the benchmark series."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path

import pandas as pd

from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_300M_STOCK_LIST_FILE,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
    PROJECT_PATH,
)
from src.financial.quarterly_fundamentals import (
    load_quarterly_fundamentals,
    quarterly_growth_snapshot,
)
from src.io.financial_update import investable_common_equities
from src.io.security_identity import load_security_identity
from src.io.terminal_returns import load_observed_terminal_returns
from src.research.universe_history import (
    known_non_common_symbols,
    load_universe_snapshots,
    snapshot_coverage,
    universe_as_of,
)
from src.research.shadow_evaluation import nasdaq_calendar_for_year
from src.research.data_audit import (
    quarterly_conflict_order_sensitivity,
    quarterly_value_conflicts,
)


SOURCE_CONFIRMED_NON_TRADING_EVIDENCE = (
    Path(PROJECT_PATH)
    / "output/data_provenance/source_confirmed_non_trading_intervals_2026-08-08.json"
)
CONFIRMED_TERMINAL_DATES_FILE = (
    Path(PROJECT_PATH) / "stocks_list_dir/nasdaq/confirmed_terminal_dates.csv"
)


def load_confirmed_terminal_dates(
    path: str | Path = CONFIRMED_TERMINAL_DATES_FILE,
) -> pd.DataFrame:
    """Load completion dates that prove a security was no longer tradable.

    This registry is deliberately separate from terminal returns: a merger can
    have an exact SEC-confirmed completion date while its CVR value remains
    unresolved.  Each row is bound to the exact cached SEC filing payload.
    """
    path = Path(path)
    columns = [
        "ticker",
        "terminal_date",
        "event_type",
        "source_url",
        "evidence_path",
        "evidence_payload_sha256",
        "verified_at",
        "note",
    ]
    if not path.exists():
        return pd.DataFrame(columns=columns)
    frame = pd.read_csv(path)
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(
            "confirmed terminal dates missing columns: "
            + ", ".join(sorted(missing))
        )
    frame = frame[columns].copy()
    frame["ticker"] = frame["ticker"].astype(str).str.upper().str.strip()
    frame["terminal_date"] = pd.to_datetime(
        frame["terminal_date"], errors="raise"
    ).dt.normalize()
    if frame["ticker"].duplicated().any():
        raise ValueError("confirmed terminal dates contain duplicate tickers")
    if frame["source_url"].fillna("").str.strip().eq("").any():
        raise ValueError("confirmed terminal dates require source URLs")
    for row in frame.itertuples(index=False):
        evidence_path = Path(PROJECT_PATH) / row.evidence_path
        envelope = json.loads(gzip.decompress(evidence_path.read_bytes()))
        payload = bytes.fromhex(envelope["payload_hex"])
        actual = hashlib.sha256(payload).hexdigest()
        if actual != row.evidence_payload_sha256:
            raise ValueError(
                f"confirmed terminal payload SHA mismatch for {row.ticker}"
            )
        if envelope.get("payload_sha256") != actual:
            raise ValueError(
                f"confirmed terminal cache envelope mismatch for {row.ticker}"
            )
        if envelope.get("source_url") != row.source_url:
            raise ValueError(
                f"confirmed terminal source URL mismatch for {row.ticker}"
            )
    return frame


def load_source_confirmed_non_trading_evidence(
    path: str | Path = SOURCE_CONFIRMED_NON_TRADING_EVIDENCE,
    *,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
) -> list[dict]:
    """Load exact, SHA-bound evidence that an apparent gap contains no trades."""
    evidence_path = Path(path)
    if not evidence_path.exists():
        return []
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    records = payload.get("records", [])
    if not isinstance(records, list):
        raise ValueError("non-trading evidence records must be a list")
    accepted = []
    seen: set[tuple[str, str, str]] = set()
    for raw in records:
        record = dict(raw)
        ticker = str(record.get("ticker", "")).upper().strip()
        last_price = pd.to_datetime(
            record.get("last_price_date"), errors="raise"
        ).strftime("%Y-%m-%d")
        last_membership = pd.to_datetime(
            record.get("last_membership_date"), errors="raise"
        ).strftime("%Y-%m-%d")
        key = (ticker, last_price, last_membership)
        if not ticker or key in seen:
            raise ValueError(f"duplicate or empty non-trading evidence key: {key}")
        seen.add(key)
        if record.get("resolution") not in {
            "SOURCE_CONFIRMED_NO_TRADES",
            "EXCHANGE_HALT_CONFIRMED",
        }:
            raise ValueError(f"unsupported non-trading resolution for {ticker}")
        price_path = Path(price_dir) / f"{ticker.lower()}.csv"
        actual_price_sha = hashlib.sha256(price_path.read_bytes()).hexdigest()
        if actual_price_sha != record.get("price_file_sha256"):
            raise ValueError(
                f"stale non-trading evidence for {ticker}: price SHA changed"
            )
        sha_fields = [
            value for field, value in record.items()
            if field.endswith("_sha256")
        ]
        if not sha_fields or any(
            len(str(value)) != 64
            or any(char not in "0123456789abcdef" for char in str(value).lower())
            for value in sha_fields
        ):
            raise ValueError(f"invalid SHA evidence for {ticker}")
        record.update({
            "ticker": ticker,
            "last_price_date": last_price,
            "last_membership_date": last_membership,
            "evidence_path": str(evidence_path),
        })
        accepted.append(record)
    return accepted


def partition_terminal_candidates(
    rows: list[dict],
    *,
    analysis_end: str | pd.Timestamp,
    observation_lag_days: int = 40,
) -> tuple[list[dict], list[dict]]:
    """Separate mature terminal candidates from right-censored price tails."""
    if observation_lag_days < 0:
        raise ValueError("observation_lag_days must be non-negative")
    cutoff = pd.Timestamp(analysis_end) - pd.Timedelta(days=observation_lag_days)
    candidates, right_censored = [], []
    for row in rows:
        target = (
            right_censored
            if pd.Timestamp(row["last_price_date"]) > cutoff
            else candidates
        )
        target.append(row)
    return candidates, right_censored


def snapshot_coverage_diagnostics(
    snapshots: dict[pd.Timestamp, set[str]],
    start: str,
    end: str | None,
    maximum_snapshot_gap_days: int = 40,
) -> dict:
    """Enrich the frozen membership coverage rule with actionable gaps."""
    report = snapshot_coverage(
        snapshots, start, end, maximum_snapshot_gap_days
    )
    requested_start = pd.Timestamp(start)
    requested_end = (
        pd.Timestamp(end) if end else pd.Timestamp.today().normalize()
    )
    dates = sorted(snapshots)
    relevant = [
        item for item in dates
        if requested_start <= item <= requested_end
    ]
    boundaries = [requested_start, *relevant, requested_end]
    gaps = []
    for index, (left, right) in enumerate(
        zip(boundaries, boundaries[1:])
    ):
        gap_days = int((right - left).days)
        if gap_days <= maximum_snapshot_gap_days:
            continue
        if not relevant:
            gap_type = "no_snapshots_in_requested_period"
        elif index == 0:
            gap_type = "start_boundary"
        elif index == len(boundaries) - 2:
            gap_type = "end_boundary"
        else:
            gap_type = "between_snapshots"
        gaps.append({
            "left_date": left.strftime("%Y-%m-%d"),
            "right_date": right.strftime("%Y-%m-%d"),
            "gap_days": gap_days,
            "gap_type": gap_type,
        })
    return {
        **report,
        "relevant_snapshot_count": len(relevant),
        "requested_start": requested_start.strftime("%Y-%m-%d"),
        "requested_end": requested_end.strftime("%Y-%m-%d"),
        "gaps_over_limit_count": len(gaps),
        "gaps_over_limit": gaps,
    }


def audit_benchmark_calendar(
    start: str,
    end: str,
    index_file: str | Path = NASDAQ_INDEX_FILE,
) -> dict:
    """Compare benchmark rows with every official Nasdaq session."""
    frame = pd.read_csv(index_file, parse_dates=["date"])
    dates = frame["date"].dt.normalize()
    start_date = pd.Timestamp(start).normalize()
    end_date = pd.Timestamp(end).normalize()
    in_range = dates.between(start_date, end_date)
    observed = dates.loc[in_range]
    duplicate_dates = sorted(
        observed.loc[observed.duplicated(keep=False)]
        .dt.strftime("%Y-%m-%d")
        .unique()
        .tolist()
    )
    expected = set()
    for year in range(start_date.year, end_date.year + 1):
        calendar = nasdaq_calendar_for_year(year)
        expected.update(
            calendar.sessions[
                (calendar.sessions >= start_date)
                & (calendar.sessions <= end_date)
            ].tz_localize(None)
        )
    actual = set(observed)
    missing_sessions = sorted(
        date.strftime("%Y-%m-%d") for date in expected - actual
    )
    non_session_rows = sorted(
        date.strftime("%Y-%m-%d") for date in actual - expected
    )
    invalid_close_dates = []
    if "close" in frame.columns:
        invalid = pd.to_numeric(
            frame.loc[in_range, "close"], errors="coerce"
        ).isna()
        invalid_close_dates = (
            frame.loc[in_range].loc[invalid, "date"]
            .dt.strftime("%Y-%m-%d")
            .tolist()
        )
    return {
        "start": start_date.strftime("%Y-%m-%d"),
        "end": end_date.strftime("%Y-%m-%d"),
        "expected_sessions": len(expected),
        "observed_unique_sessions": len(actual),
        "missing_sessions": missing_sessions,
        "duplicate_dates": duplicate_dates,
        "non_session_rows": non_session_rows,
        "invalid_close_dates": invalid_close_dates,
        "complete": not (
            missing_sessions
            or duplicate_dates
            or non_session_rows
            or invalid_close_dates
        ),
    }
from src.strategy.common import scheduled_signal_dates


def load_price_date_metadata(
    price_dir: str | Path | None = None,
) -> tuple[list[Path], dict[str, pd.Series]]:
    """Read each retained price calendar once for all readiness checks."""
    root = Path(
        CLEANED_PRICE_DATA_DIR if price_dir is None else price_dir
    )
    paths = sorted(root.glob("*.csv"))
    metadata = {}
    for path in paths:
        dates = pd.read_csv(
            path, usecols=["date"], parse_dates=["date"]
        )["date"].dropna()
        if len(dates):
            metadata[path.stem.upper()] = (
                dates.sort_values().reset_index(drop=True)
            )
    return paths, metadata


def _known_price_row_count(
    dates: pd.Series,
    observed_at: pd.Timestamp,
) -> int:
    """Return the sorted price-calendar prefix observable by one date."""
    return int(dates.searchsorted(pd.Timestamp(observed_at), side="right"))


def audit_snapshot_price_coverage(
    snapshots: dict[pd.Timestamp, set[str]],
    start: str = "2024-10-05",
    end: str | None = None,
    maximum_price_lag_days: int = 7,
    minimum_lookback_rows: int = 253,
    price_date_metadata: dict[str, pd.Series] | None = None,
) -> dict:
    """Measure whether every dated member was actually observable then."""
    metadata = (
        price_date_metadata
        if price_date_metadata is not None
        else load_price_date_metadata()[1]
    )
    rows = []
    missing_union: set[str] = set()
    for observed_at, members in sorted(snapshots.items()):
        if observed_at < pd.Timestamp(start):
            continue
        if end is not None and observed_at > pd.Timestamp(end):
            continue
        current, lookback_ready = set(), set()
        for ticker in members:
            dates = metadata.get(ticker)
            if dates is None:
                continue
            known_rows = _known_price_row_count(dates, observed_at)
            if (
                not known_rows
                or (observed_at - dates.iloc[known_rows - 1]).days
                > maximum_price_lag_days
            ):
                continue
            current.add(ticker)
            if known_rows >= minimum_lookback_rows:
                lookback_ready.add(ticker)
        missing = members - current
        missing_union.update(missing)
        rows.append({
            "observed_at": observed_at.strftime("%Y-%m-%d"),
            "members": len(members),
            "price_current": len(current),
            "price_current_coverage": len(current) / max(len(members), 1),
            "lookback_ready": len(lookback_ready),
            "lookback_ready_coverage": len(lookback_ready) / max(len(members), 1),
            "missing_price_count": len(missing),
        })
    minimum_current = min((row["price_current_coverage"] for row in rows), default=0.0)
    minimum_lookback = min((row["lookback_ready_coverage"] for row in rows), default=0.0)
    return {
        "start": start,
        "end": end,
        "maximum_price_lag_days": maximum_price_lag_days,
        "minimum_lookback_rows": minimum_lookback_rows,
        "snapshots_tested": len(rows),
        "minimum_price_current_coverage": minimum_current,
        "minimum_lookback_ready_coverage": minimum_lookback,
        # Recent IPOs legitimately have fewer than 253 observations and are
        # excluded by the strategy until mature. Completeness means their
        # observed history is present, not that every listing is signal-ready.
        "complete": bool(rows) and minimum_current == 1.0,
        "missing_price_symbols": sorted(missing_union),
        "by_snapshot": rows,
    }


def audit_signal_price_coverage(
    snapshots: dict[pd.Timestamp, set[str]],
    start: str,
    end: str,
    maximum_price_lag_days: int = 7,
    minimum_lookback_rows: int = 253,
    quarterly_fundamentals: pd.DataFrame | None = None,
    maximum_financial_age_days: int = 550,
    minimum_profit_growth: float = 0.25,
    minimum_revenue_growth: float = 0.10,
    confirmed_listings: pd.DataFrame | None = None,
    maximum_signal_snapshot_age_days: int = 40,
    price_date_metadata: dict[str, pd.Series] | None = None,
    observed_terminal_returns: pd.DataFrame | None = None,
    confirmed_terminal_dates: pd.DataFrame | None = None,
) -> dict:
    """Measure exact universe-price availability on strategy signal dates."""
    metadata = (
        price_date_metadata
        if price_date_metadata is not None
        else load_price_date_metadata()[1]
    )
    benchmark_dates = pd.read_csv(
        NASDAQ_INDEX_FILE, usecols=["date"], parse_dates=["date"]
    )["date"]
    signals = scheduled_signal_dates(benchmark_dates, start, end, "monthly")
    rows = []
    missing_union: set[str] = set()
    absent_file_union: set[str] = set()
    starts_later_union: set[str] = set()
    stale_union: set[str] = set()
    internal_gap_union: set[str] = set()
    missing_financial_data_union: set[str] = set()
    missing_usable_financial_growth_union: set[str] = set()
    no_raw_financial_facts_union: set[str] = set()
    insufficient_financial_history_union: set[str] = set()
    stale_financial_growth_union: set[str] = set()
    missing_without_financial_data_union: set[str] = set()
    missing_financial_screen_pass_union: set[str] = set()
    insufficient_listing_history_union: set[str] = set()
    confirmed_terminal_before_signal_union: set[str] = set()
    unresolved_potential_competitor_union: set[str] = set()
    missing_with_financial_data_details: list[dict] = []
    missing_financial_metric_gap_observations: list[dict] = []
    missing_financial_metric_gap_counts: dict[str, Counter] = {}
    missing_signal_counts: Counter = Counter()
    missing_gap_counts: dict[str, Counter] = {}
    financial_gap_counts: dict[str, Counter] = {}
    first_missing_signal: dict[str, pd.Timestamp] = {}
    last_missing_signal: dict[str, pd.Timestamp] = {}
    stale_signal_snapshot_dates: list[str] = []
    snapshot_dates = sorted(snapshots)
    listing_dates = {}
    if confirmed_listings is not None and len(confirmed_listings):
        listings = confirmed_listings.copy()
        listings["ticker"] = listings["ticker"].astype(str).str.upper()
        listings["first_trading_date"] = pd.to_datetime(
            listings["first_trading_date"], errors="raise"
        )
        listing_dates = listings.set_index("ticker")[
            "first_trading_date"
        ].to_dict()
    identities = load_security_identity()
    identity_by_historical = {
        row.historical_ticker: row
        for row in identities.itertuples(index=False)
    }
    observed = (
        load_observed_terminal_returns()
        if observed_terminal_returns is None
        else observed_terminal_returns.copy()
    )
    terminal_dates = {}
    if len(observed):
        observed["ticker"] = observed["ticker"].astype(str).str.upper()
        observed["last_price_date"] = pd.to_datetime(
            observed["last_price_date"], errors="raise"
        ).dt.normalize()
        terminal_dates = observed.groupby("ticker")["last_price_date"].max().to_dict()
    confirmed_dates = (
        load_confirmed_terminal_dates()
        if confirmed_terminal_dates is None
        else confirmed_terminal_dates.copy()
    )
    if len(confirmed_dates):
        confirmed_dates["ticker"] = (
            confirmed_dates["ticker"].astype(str).str.upper().str.strip()
        )
        confirmed_dates["terminal_date"] = pd.to_datetime(
            confirmed_dates["terminal_date"], errors="raise"
        ).dt.normalize()
        for ticker, terminal_date in confirmed_dates.groupby("ticker")[
            "terminal_date"
        ].max().items():
            existing = terminal_dates.get(ticker)
            terminal_dates[ticker] = (
                max(existing, terminal_date)
                if existing is not None
                else terminal_date
            )
    for signal_date in signals:
        members = universe_as_of(snapshots, signal_date)
        if members is None:
            continue
        snapshot_date = max(
            date for date in snapshot_dates if date <= signal_date
        )
        snapshot_age_days = int((signal_date - snapshot_date).days)
        if snapshot_age_days > maximum_signal_snapshot_age_days:
            stale_signal_snapshot_dates.append(
                signal_date.strftime("%Y-%m-%d")
            )
        current = set()
        lookback_ready = set()
        absent_file = set()
        starts_later = set()
        stale = set()
        internal_gap = set()
        for ticker in members:
            price_ticker = ticker
            identity = identity_by_historical.get(ticker)
            if (
                identity is not None
                and identity.identity_type == "issuer_rename"
                and signal_date >= identity.current_ticker_first_date
            ):
                price_ticker = identity.provider_ticker
            dates = metadata.get(price_ticker)
            if dates is None:
                absent_file.add(ticker)
                continue
            known_rows = _known_price_row_count(dates, signal_date)
            if not known_rows:
                starts_later.add(ticker)
            elif (
                (signal_date - dates.iloc[known_rows - 1]).days
                > maximum_price_lag_days
            ):
                if dates.iloc[-1] > signal_date:
                    internal_gap.add(ticker)
                else:
                    stale.add(ticker)
            else:
                current.add(ticker)
                if known_rows >= minimum_lookback_rows:
                    lookback_ready.add(ticker)
        missing = members - current
        for ticker in missing:
            missing_signal_counts[ticker] += 1
            gap_type = (
                "absent_price_file"
                if ticker in absent_file
                else "history_starts_after_signal"
                if ticker in starts_later
                else "internal_price_gap_at_signal"
                if ticker in internal_gap
                else "stale_or_ended_history"
            )
            missing_gap_counts.setdefault(ticker, Counter())[gap_type] += 1
            first_missing_signal.setdefault(ticker, signal_date)
            last_missing_signal[ticker] = signal_date
        missing_with_financial_data: set[str] = set()
        missing_financial_screen_pass: set[str] = set()
        insufficient_listing_history: set[str] = set()
        financial = pd.DataFrame()
        raw_pit_financial_tickers: set[str] = set()
        raw_pit_financial_tickers_all: set[str] = set()
        raw_metric_tickers: dict[str, set[str]] = {}
        unbounded_financial = pd.DataFrame()
        if quarterly_fundamentals is not None:
            financial = quarterly_growth_snapshot(
                quarterly_fundamentals,
                signal_date,
                maximum_financial_age_days,
            )
            required_raw_columns = {
                "ticker", "available_date", "metric"
            }
            if required_raw_columns.issubset(
                quarterly_fundamentals.columns
            ):
                raw_known = quarterly_fundamentals.loc[
                    quarterly_fundamentals["available_date"].le(
                        signal_date
                    )
                    & quarterly_fundamentals["metric"].isin(
                        ("net_income", "revenue")
                    )
                ]
                raw_metric_tickers = {
                    metric: (
                        members
                        & set(
                            raw_known.loc[
                                raw_known["metric"].eq(metric), "ticker"
                            ].dropna().astype(str).str.upper()
                        )
                    )
                    for metric in ("net_income", "revenue")
                }
                raw_pit_financial_tickers_all = (
                    members
                    & set(
                        raw_known["ticker"].dropna().astype(str).str.upper()
                    )
                )
                raw_pit_financial_tickers = (
                    missing & raw_pit_financial_tickers_all
                )
                unbounded_financial = quarterly_growth_snapshot(
                    quarterly_fundamentals,
                    signal_date,
                    100_000,
                )
            else:
                # Synthetic callers may provide only a mocked growth
                # snapshot. Treat its index as the strongest available
                # evidence instead of requiring raw SEC columns.
                raw_pit_financial_tickers = (
                    missing & set(financial.index)
                )
                raw_pit_financial_tickers_all = (
                    members & set(financial.index)
                )
                unbounded_financial = financial
            missing_with_financial_data = missing & set(financial.index)
            financially_eligible = (
                financial.loc[
                    financial["net_income_ttm"].gt(0)
                    & financial["net_income_growth"].ge(
                        minimum_profit_growth
                    )
                    & financial["revenue_growth"].ge(
                        minimum_revenue_growth
                    )
                ]
                if len(financial)
                else financial
            )
            missing_financial_screen_pass = missing & set(
                financially_eligible.index
            )
            for ticker in missing_financial_screen_pass:
                first_trading_date = listing_dates.get(ticker)
                if first_trading_date is None:
                    continue
                sessions = benchmark_dates.loc[
                    benchmark_dates.between(first_trading_date, signal_date)
                ]
                if len(sessions) < minimum_lookback_rows:
                    insufficient_listing_history.add(ticker)
            for ticker in missing - missing_with_financial_data:
                if ticker in set(unbounded_financial.index):
                    gap_reason = "stale_growth_snapshot"
                elif ticker in raw_pit_financial_tickers:
                    gap_reason = "insufficient_growth_history"
                else:
                    gap_reason = "no_raw_pit_financial_facts"
                financial_gap_counts.setdefault(
                    ticker, Counter()
                )[gap_reason] += 1
        confirmed_terminal_before_signal = {
            ticker
            for ticker in missing_financial_screen_pass
            if ticker in terminal_dates and terminal_dates[ticker] < signal_date
        }
        unresolved_potential_competitors = (
            missing_financial_screen_pass
            - insufficient_listing_history
            - confirmed_terminal_before_signal
        )
        usable_financial_growth_members = (
            members & set(financial.index)
        )
        missing_usable_financial_growth = (
            members - usable_financial_growth_members
        )
        stale_financial_growth = (
            missing_usable_financial_growth
            & set(unbounded_financial.index)
        )
        insufficient_financial_history = (
            missing_usable_financial_growth
            & raw_pit_financial_tickers_all
            - stale_financial_growth
        )
        no_raw_financial_facts = (
            missing_usable_financial_growth
            - raw_pit_financial_tickers_all
        )
        if raw_metric_tickers:
            for ticker in sorted(missing_usable_financial_growth):
                missing_metrics = {
                    metric
                    for metric, tickers in raw_metric_tickers.items()
                    if ticker not in tickers
                }
                if not missing_metrics:
                    continue
                if missing_metrics == {"revenue"}:
                    metric_gap_classification = "NO_REVENUE_FACT"
                elif missing_metrics == {"net_income"}:
                    metric_gap_classification = "NO_NET_INCOME_FACT"
                else:
                    metric_gap_classification = (
                        "NO_REVENUE_AND_NET_INCOME_FACTS"
                    )
                missing_financial_metric_gap_counts.setdefault(
                    metric_gap_classification, Counter()
                )[ticker] += 1
                missing_financial_metric_gap_observations.append({
                    "signal_date": signal_date.strftime("%Y-%m-%d"),
                    "ticker": ticker,
                    "missing_raw_pit_metrics": "|".join(
                        sorted(missing_metrics)
                    ),
                    "classification": metric_gap_classification,
                })
        missing_usable_financial_growth_union.update(
            missing_usable_financial_growth
        )
        no_raw_financial_facts_union.update(no_raw_financial_facts)
        insufficient_financial_history_union.update(
            insufficient_financial_history
        )
        stale_financial_growth_union.update(stale_financial_growth)
        for ticker in sorted(missing_with_financial_data):
            values = financial.loc[ticker]
            passes_positive_profit = bool(values["net_income_ttm"] > 0)
            passes_profit_growth = bool(
                values["net_income_growth"] >= minimum_profit_growth
            )
            passes_revenue_growth = bool(
                values["revenue_growth"] >= minimum_revenue_growth
            )
            passes_financial_screen = bool(
                passes_positive_profit
                and passes_profit_growth
                and passes_revenue_growth
            )
            first_trading_date = listing_dates.get(ticker)
            benchmark_sessions_since_listing = None
            listing_history_sufficient = None
            if first_trading_date is not None:
                benchmark_sessions_since_listing = int(
                    benchmark_dates.between(
                        first_trading_date, signal_date
                    ).sum()
                )
                listing_history_sufficient = bool(
                    benchmark_sessions_since_listing
                    >= minimum_lookback_rows
                )
            if ticker in confirmed_terminal_before_signal:
                classification = "confirmed_terminal_before_signal"
            elif not passes_financial_screen:
                classification = "fails_financial_screen"
            elif listing_history_sufficient is False:
                classification = (
                    "confirmed_insufficient_listing_history"
                )
            else:
                classification = (
                    "unresolved_observable_potential_competitor"
                )
            price_dates = metadata.get(ticker)
            missing_with_financial_data_details.append({
                "signal_date": signal_date.strftime("%Y-%m-%d"),
                "ticker": ticker,
                "price_gap_type": (
                    "absent_price_file"
                    if ticker in absent_file
                    else "history_starts_after_signal"
                    if ticker in starts_later
                    else "internal_price_gap_at_signal"
                    if ticker in internal_gap
                    else "stale_or_ended_history"
                ),
                "net_income_ttm": float(values["net_income_ttm"]),
                "net_income_growth": float(values["net_income_growth"]),
                "revenue_growth": float(values["revenue_growth"]),
                "financial_age_days": (
                    int(values["financial_age_days"])
                    if "financial_age_days" in values
                    else None
                ),
                "passes_positive_profit": passes_positive_profit,
                "passes_profit_growth": passes_profit_growth,
                "passes_revenue_growth": passes_revenue_growth,
                "passes_financial_screen": passes_financial_screen,
                "first_price_date": (
                    price_dates.iloc[0].strftime("%Y-%m-%d")
                    if price_dates is not None and len(price_dates)
                    else None
                ),
                "first_trading_date": (
                    first_trading_date.strftime("%Y-%m-%d")
                    if first_trading_date is not None
                    else None
                ),
                "benchmark_sessions_since_listing": (
                    benchmark_sessions_since_listing
                ),
                "listing_history_sufficient": (
                    listing_history_sufficient
                ),
                "final_observable_classification": classification,
            })
        missing_union.update(missing)
        absent_file_union.update(absent_file)
        starts_later_union.update(starts_later)
        stale_union.update(stale)
        internal_gap_union.update(internal_gap)
        missing_financial_data_union.update(missing_with_financial_data)
        missing_without_financial_data_union.update(
            missing - missing_with_financial_data
        )
        missing_financial_screen_pass_union.update(
            missing_financial_screen_pass
        )
        insufficient_listing_history_union.update(
            insufficient_listing_history
        )
        confirmed_terminal_before_signal_union.update(
            confirmed_terminal_before_signal
        )
        unresolved_potential_competitor_union.update(
            unresolved_potential_competitors
        )
        rows.append({
            "signal_date": signal_date.strftime("%Y-%m-%d"),
            "universe_snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
            "universe_snapshot_age_days": snapshot_age_days,
            "members": len(members),
            "price_current": len(current),
            "price_current_coverage": len(current) / max(len(members), 1),
            "lookback_ready": len(lookback_ready),
            "lookback_ready_coverage": (
                len(lookback_ready) / max(len(members), 1)
            ),
            "missing_price_count": len(missing),
            "usable_pit_financial_growth_count": len(
                usable_financial_growth_members
            ),
            "usable_pit_financial_growth_coverage": (
                len(usable_financial_growth_members)
                / max(len(members), 1)
            ),
            "missing_usable_pit_financial_growth_count": len(
                missing_usable_financial_growth
            ),
            "missing_no_raw_pit_financial_facts_count": len(
                no_raw_financial_facts
            ),
            "missing_insufficient_financial_history_count": len(
                insufficient_financial_history
            ),
            "missing_stale_financial_growth_count": len(
                stale_financial_growth
            ),
            "absent_price_file_count": len(absent_file),
            "history_starts_after_signal_count": len(starts_later),
            "stale_or_ended_history_count": len(stale),
            "internal_price_gap_at_signal_count": len(internal_gap),
            "missing_with_pit_financial_data_count": len(
                missing_with_financial_data
            ),
            "missing_without_pit_financial_data_count": (
                len(missing) - len(missing_with_financial_data)
            ),
            "missing_passing_financial_screen_count": len(
                missing_financial_screen_pass
            ),
            "missing_passing_financial_screen_symbols": "|".join(
                sorted(missing_financial_screen_pass)
            ),
            "confirmed_insufficient_listing_history_count": len(
                insufficient_listing_history
            ),
            "confirmed_terminal_before_signal_count": len(
                confirmed_terminal_before_signal
            ),
            "unresolved_observable_potential_competitor_count": len(
                unresolved_potential_competitors
            ),
        })
    minimum = min(
        (row["price_current_coverage"] for row in rows), default=0.0
    )
    minimum_lookback = min(
        (row["lookback_ready_coverage"] for row in rows), default=0.0
    )
    by_year = []
    signal_frame = pd.DataFrame(rows)
    if len(signal_frame):
        signal_frame["year"] = pd.to_datetime(
            signal_frame["signal_date"]
        ).dt.year
        for year, annual in signal_frame.groupby("year"):
            by_year.append({
                "year": int(year),
                "signal_count": int(len(annual)),
                "minimum_price_current_coverage": float(
                    annual["price_current_coverage"].min()
                ),
                "median_price_current_coverage": float(
                    annual["price_current_coverage"].median()
                ),
                "minimum_lookback_ready_coverage": float(
                    annual["lookback_ready_coverage"].min()
                ),
                "maximum_missing_price_count": int(
                    annual["missing_price_count"].max()
                ),
                "minimum_usable_pit_financial_growth_coverage": float(
                    annual[
                        "usable_pit_financial_growth_coverage"
                    ].min()
                ),
                "maximum_missing_usable_pit_financial_growth_count": int(
                    annual[
                        "missing_usable_pit_financial_growth_count"
                    ].max()
                ),
                "maximum_missing_no_raw_pit_financial_facts_count": int(
                    annual[
                        "missing_no_raw_pit_financial_facts_count"
                    ].max()
                ),
                "maximum_missing_insufficient_financial_history_count": int(
                    annual[
                        "missing_insufficient_financial_history_count"
                    ].max()
                ),
                "maximum_missing_stale_financial_growth_count": int(
                    annual[
                        "missing_stale_financial_growth_count"
                    ].max()
                ),
                "maximum_absent_price_file_count": int(
                    annual["absent_price_file_count"].max()
                ),
                "maximum_history_starts_after_signal_count": int(
                    annual["history_starts_after_signal_count"].max()
                ),
                "maximum_stale_or_ended_history_count": int(
                    annual["stale_or_ended_history_count"].max()
                ),
                "maximum_internal_price_gap_at_signal_count": int(
                    annual[
                        "internal_price_gap_at_signal_count"
                    ].max()
                ),
                "maximum_missing_with_pit_financial_data_count": int(
                    annual["missing_with_pit_financial_data_count"].max()
                ),
                "maximum_missing_without_pit_financial_data_count": int(
                    annual["missing_without_pit_financial_data_count"].max()
                ),
                "maximum_missing_passing_financial_screen_count": int(
                    annual["missing_passing_financial_screen_count"].max()
                ),
                "maximum_unresolved_observable_potential_competitor_count": int(
                    annual[
                        "unresolved_observable_potential_competitor_count"
                    ].max()
                ),
            })
    never_with_financial_data = (
        missing_union - missing_financial_data_union
    )
    pit_gap_priorities = []
    for ticker in never_with_financial_data:
        dates = metadata.get(ticker)
        gaps = missing_gap_counts[ticker]
        identity = identity_by_historical.get(ticker)
        if gaps["absent_price_file"]:
            remediation_scope = "ACQUIRE_PRICE_AND_PIT_FINANCIAL"
        elif sum(bool(gaps[name]) for name in (
            "history_starts_after_signal",
            "internal_price_gap_at_signal",
            "stale_or_ended_history",
        )) > 1:
            remediation_scope = (
                "REPAIR_MULTIPLE_PRICE_GAPS_PLUS_PIT_FINANCIAL"
            )
        elif gaps["history_starts_after_signal"]:
            remediation_scope = (
                "BACKFILL_PRICE_HEAD_PLUS_PIT_FINANCIAL"
            )
        elif gaps["internal_price_gap_at_signal"]:
            remediation_scope = (
                "FILL_INTERNAL_PRICE_GAPS_PLUS_PIT_FINANCIAL"
            )
        else:
            remediation_scope = (
                "RESTORE_PRICE_TAIL_PLUS_PIT_FINANCIAL"
            )
        pit_gap_priorities.append({
            "ticker": ticker,
            "provider_ticker": (
                identity.provider_ticker
                if identity is not None else ticker
            ),
            "security_identity_type": (
                identity.identity_type
                if identity is not None else None
            ),
            "security_identity_source_url": (
                identity.source_url
                if identity is not None else None
            ),
            "missing_signal_count": int(missing_signal_counts[ticker]),
            "first_missing_signal_date": first_missing_signal[
                ticker
            ].strftime("%Y-%m-%d"),
            "last_missing_signal_date": last_missing_signal[
                ticker
            ].strftime("%Y-%m-%d"),
            "absent_price_file_signal_count": int(
                gaps["absent_price_file"]
            ),
            "history_starts_after_signal_count": int(
                gaps["history_starts_after_signal"]
            ),
            "stale_or_ended_history_count": int(
                gaps["stale_or_ended_history"]
            ),
            "internal_price_gap_at_signal_count": int(
                gaps["internal_price_gap_at_signal"]
            ),
            "no_raw_pit_financial_facts_signal_count": int(
                financial_gap_counts.get(ticker, Counter())[
                    "no_raw_pit_financial_facts"
                ]
            ),
            "insufficient_growth_history_signal_count": int(
                financial_gap_counts.get(ticker, Counter())[
                    "insufficient_growth_history"
                ]
            ),
            "stale_growth_snapshot_signal_count": int(
                financial_gap_counts.get(ticker, Counter())[
                    "stale_growth_snapshot"
                ]
            ),
            "observed_price_file_first_date": (
                dates.iloc[0].strftime("%Y-%m-%d")
                if dates is not None and len(dates) else None
            ),
            "observed_price_file_last_date": (
                dates.iloc[-1].strftime("%Y-%m-%d")
                if dates is not None and len(dates) else None
            ),
            "remediation_scope": remediation_scope,
        })
    pit_gap_priorities.sort(
        key=lambda row: (
            -row["missing_signal_count"],
            row["ticker"],
        )
    )
    for rank, row in enumerate(pit_gap_priorities, start=1):
        row["priority_rank"] = rank
    recovery_order = sorted(
        pit_gap_priorities,
        key=lambda row: (
            not bool(
                row["insufficient_growth_history_signal_count"]
                or row["stale_growth_snapshot_signal_count"]
            ),
            bool(row["absent_price_file_signal_count"]),
            -row["missing_signal_count"],
            row["ticker"],
        ),
    )
    for rank, row in enumerate(recovery_order, start=1):
        row["recovery_priority_rank"] = rank
    return {
        "start": start,
        "end": end,
        "maximum_price_lag_days": maximum_price_lag_days,
        "minimum_lookback_rows": minimum_lookback_rows,
        "maximum_financial_age_days": maximum_financial_age_days,
        "maximum_signal_snapshot_age_days": (
            maximum_signal_snapshot_age_days
        ),
        "minimum_profit_growth": minimum_profit_growth,
        "minimum_revenue_growth": minimum_revenue_growth,
        "signal_count": len(rows),
        "minimum_price_current_coverage": minimum,
        "minimum_lookback_ready_coverage": minimum_lookback,
        "minimum_usable_pit_financial_growth_coverage": min(
            (
                row["usable_pit_financial_growth_coverage"]
                for row in rows
            ),
            default=0.0,
        ),
        "usable_pit_financial_growth_complete": bool(rows)
        and not missing_usable_financial_growth_union,
        "missing_usable_pit_financial_growth_observations": sum(
            row["missing_usable_pit_financial_growth_count"]
            for row in rows
        ),
        "missing_no_raw_pit_financial_facts_observations": sum(
            row["missing_no_raw_pit_financial_facts_count"]
            for row in rows
        ),
        "missing_insufficient_financial_history_observations": sum(
            row["missing_insufficient_financial_history_count"]
            for row in rows
        ),
        "missing_stale_financial_growth_observations": sum(
            row["missing_stale_financial_growth_count"]
            for row in rows
        ),
        "missing_financial_metric_gap_observations": (
            missing_financial_metric_gap_observations
        ),
        "missing_financial_metric_gap_counts": {
            classification: {
                ticker: int(count)
                for ticker, count in sorted(counts.items())
            }
            for classification, counts in sorted(
                missing_financial_metric_gap_counts.items()
            )
        },
        "financial_gap_reason_interpretation": (
            "Gap reasons are mutually exclusive within each signal-date "
            "observation. Union symbol lists can overlap because the same "
            "ticker may move from no raw facts to insufficient history, "
            "then to stale or usable growth on later signals."
        ),
        "complete": bool(rows) and minimum == 1.0,
        "maximum_observed_signal_snapshot_age_days": max(
            (
                row["universe_snapshot_age_days"]
                for row in rows
            ),
            default=None,
        ),
        "signal_membership_snapshots_complete": (
            bool(rows) and not stale_signal_snapshot_dates
        ),
        "stale_signal_snapshot_dates": stale_signal_snapshot_dates,
        "missing_price_symbols": sorted(missing_union),
        "missing_usable_pit_financial_growth_symbols": sorted(
            missing_usable_financial_growth_union
        ),
        "missing_no_raw_pit_financial_facts_symbols": sorted(
            no_raw_financial_facts_union
        ),
        "missing_insufficient_financial_history_symbols": sorted(
            insufficient_financial_history_union
        ),
        "missing_stale_financial_growth_symbols": sorted(
            stale_financial_growth_union
        ),
        "absent_price_file_symbols": sorted(absent_file_union),
        "history_starts_after_signal_symbols": sorted(starts_later_union),
        "stale_or_ended_history_symbols": sorted(stale_union),
        "internal_price_gap_at_signal_symbols": sorted(
            internal_gap_union
        ),
        "missing_with_pit_financial_data_symbols": sorted(
            missing_financial_data_union
        ),
        "missing_without_pit_financial_data_symbols": sorted(
            missing_without_financial_data_union
        ),
        "missing_never_with_pit_financial_data_symbols": sorted(
            never_with_financial_data
        ),
        "missing_with_and_without_pit_financial_data_symbols": sorted(
            missing_financial_data_union
            & missing_without_financial_data_union
        ),
        "missing_passing_financial_screen_symbols": sorted(
            missing_financial_screen_pass_union
        ),
        "confirmed_insufficient_listing_history_symbols": sorted(
            insufficient_listing_history_union
        ),
        "confirmed_terminal_before_signal_symbols": sorted(
            confirmed_terminal_before_signal_union
        ),
        "unresolved_observable_potential_competitor_symbols": sorted(
            unresolved_potential_competitor_union
        ),
        "missing_with_pit_financial_data_details": (
            missing_with_financial_data_details
        ),
        "pit_gap_priority_method": (
            "Rank symbols that never had usable PIT financial data on any "
            "missing-price signal by descending affected signal count, then "
            "ticker. Price-file dates and remediation scope describe current "
            "backfill work only; no future return, later financial result, or "
            "selection outcome is used for ranking."
        ),
        "pit_gap_recovery_priority_method": (
            "Operational quick-win rank: symbols with any raw PIT facts but "
            "no usable growth snapshot first, then symbols with an existing "
            "partial price file, then descending affected signal count and "
            "ticker. This rank estimates repair readiness, not investment "
            "importance, and uses no future return or selection outcome."
        ),
        "pit_gap_priorities": pit_gap_priorities,
        "financial_screen_interpretation": (
            "This is an observable upper bound under the current PIT financial "
            "dataset, not proof that missing-price symbols without PIT "
            "growth snapshots were truly ineligible at the time. A usable "
            "growth snapshot requires enough raw quarterly facts to calculate "
            "TTM year-over-year growth and must satisfy the configured age "
            "limit; it is not synonymous with the presence of any raw SEC "
            "fact. "
            "missing_without_pit_financial_data_symbols means absent on at "
            "least one missing-price signal and can overlap with symbols that "
            "had PIT financial data on another signal; "
            "missing_never_with_pit_financial_data_symbols is the mutually "
            "exclusive residual."
        ),
        "by_year": by_year,
        "by_signal": rows,
    }


def audit_historical_price_terminations(
    start: str = "2021-01-01",
    end: str | None = None,
    *,
    universe_snapshots: dict[pd.Timestamp, set[str]] | None = None,
    quarterly_fundamentals: pd.DataFrame | None = None,
    non_trading_evidence_path: str | Path = SOURCE_CONFIRMED_NON_TRADING_EVIDENCE,
    terminal_observation_lag_days: int = 40,
) -> dict:
    benchmark = pd.read_csv(NASDAQ_INDEX_FILE, usecols=["date"], parse_dates=["date"])
    benchmark_end = benchmark["date"].max()
    analysis_end = min(
        pd.Timestamp(end) if end is not None else benchmark_end,
        benchmark_end,
    )
    current = investable_common_equities(pd.read_csv(NASDAQ_300M_STOCK_LIST_FILE))
    current_symbols = set(current["Symbol"].dropna().astype(str).str.upper())
    universe_snapshots = (
        universe_snapshots
        if universe_snapshots is not None
        else load_universe_snapshots()
    )
    stateless_type_snapshots = load_universe_snapshots(
        carry_forward_confirmed_types=False
    )
    signal_dates = scheduled_signal_dates(
        benchmark["date"], start, analysis_end, "monthly"
    )
    inherited_type_exclusions: set[str] = set()
    inherited_type_exclusions_by_signal = []
    for signal_date in signal_dates:
        temporal_members = universe_as_of(
            universe_snapshots, signal_date
        ) or set()
        stateless_members = universe_as_of(
            stateless_type_snapshots, signal_date
        ) or set()
        excluded = stateless_members - temporal_members
        inherited_type_exclusions.update(excluded)
        inherited_type_exclusions_by_signal.append({
            "signal_date": signal_date.strftime("%Y-%m-%d"),
            "inherited_non_common_exclusion_count": len(excluded),
        })
    temporal_type_filter = {
        "method": (
            "Carry a symbol's previously observed non-common classification "
            "forward through later ambiguous or truncated names. A later "
            "explicit common stock, common/ordinary shares, or American "
            "Depositary Shares label prospectively re-admits it. No future "
            "record rewrites an earlier snapshot."
        ),
        "signals_tested": len(signal_dates),
        "symbols_excluded_vs_stateless_name_filter": len(
            inherited_type_exclusions
        ),
        "excluded_symbols": sorted(inherited_type_exclusions),
        "maximum_exclusions_on_one_signal": max(
            (
                row["inherited_non_common_exclusion_count"]
                for row in inherited_type_exclusions_by_signal
            ),
            default=0,
        ),
        "by_signal": inherited_type_exclusions_by_signal,
    }
    last_membership = {
        ticker: observed_at
        for observed_at, members in sorted(universe_snapshots.items())
        for ticker in members
    }
    ended = []
    price_paths, price_date_metadata = load_price_date_metadata()
    for path in price_paths:
        dates = price_date_metadata.get(path.stem.upper())
        if dates is None:
            continue
        known_rows = _known_price_row_count(dates, analysis_end)
        if not known_rows:
            continue
        latest = dates.iloc[known_rows - 1]
        if latest < analysis_end:
            ticker = path.stem.upper()
            last_listed = last_membership.get(ticker)
            listed_after_price_end = bool(
                last_listed is not None and last_listed > latest + pd.Timedelta(days=7)
            )
            ended.append({
                "ticker": ticker,
                "last_price_date": latest.strftime("%Y-%m-%d"),
                "last_membership_date": (
                    last_listed.strftime("%Y-%m-%d") if last_listed is not None else None
                ),
                "listed_after_price_end": listed_after_price_end,
                "in_current_common_equity_universe": ticker in current_symbols,
            })
    by_date = Counter(item["last_price_date"] for item in ended)
    current_ended = [item for item in ended if item["in_current_common_equity_universe"]]
    known_non_common = known_non_common_symbols()
    research_common_ended = [item for item in ended if item["ticker"] not in known_non_common]
    excluded_non_common_ended = [item for item in ended if item["ticker"] in known_non_common]
    raw_missing_price_while_listed = [
        item for item in research_common_ended if item["listed_after_price_end"]
    ]
    non_trading_evidence = load_source_confirmed_non_trading_evidence(
        non_trading_evidence_path
    )
    non_trading_by_key = {
        (row["ticker"], row["last_price_date"], row["last_membership_date"]): row
        for row in non_trading_evidence
    }
    source_confirmed_non_trading = []
    missing_price_while_listed = []
    for item in raw_missing_price_while_listed:
        evidence = non_trading_by_key.get((
            item["ticker"], item["last_price_date"], item["last_membership_date"]
        ))
        if evidence is None:
            missing_price_while_listed.append(item)
        else:
            source_confirmed_non_trading.append({**item, "evidence": evidence})
    identity = load_security_identity()
    rename_transition_keys = {
        (
            row.historical_ticker,
            row.last_historical_date.strftime("%Y-%m-%d"),
        )
        for row in identity.itertuples(index=False)
        if row.identity_type == "issuer_rename"
    }
    sourced_identity_transitions = [
        item for item in research_common_ended
        if (item["ticker"], item["last_price_date"])
        in rename_transition_keys
    ]
    apparent_terminal_histories = [
        item for item in research_common_ended
        if not item["listed_after_price_end"]
        and item not in sourced_identity_transitions
    ]
    candidate_terminal_histories, right_censored_terminal_histories = (
        partition_terminal_candidates(
            apparent_terminal_histories,
            analysis_end=analysis_end,
            observation_lag_days=terminal_observation_lag_days,
        )
    )
    observed = load_observed_terminal_returns()
    observed_keys = {
        (row.ticker, row.last_price_date.strftime("%Y-%m-%d"))
        for row in observed.itertuples(index=False)
    }
    for item in candidate_terminal_histories:
        item["observed_terminal_return"] = (
            item["ticker"], item["last_price_date"]
        ) in observed_keys
    unresolved_common_ended = [
        item for item in candidate_terminal_histories if not item["observed_terminal_return"]
    ]
    universe_coverage = snapshot_coverage_diagnostics(
        universe_snapshots, start, analysis_end.strftime("%Y-%m-%d")
    )
    recent_universe_coverage = snapshot_coverage_diagnostics(
        universe_snapshots, "2024-10-05", analysis_end.strftime("%Y-%m-%d")
    )
    snapshot_price_coverage = audit_snapshot_price_coverage(
        universe_snapshots,
        end=analysis_end.strftime("%Y-%m-%d"),
        price_date_metadata=price_date_metadata,
    )
    quarterly_fundamentals = (
        quarterly_fundamentals
        if quarterly_fundamentals is not None
        else load_quarterly_fundamentals(
            POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
        )
    )
    confirmed_listings_path = (
        Path(PROJECT_PATH)
        / "stocks_list_dir/nasdaq/confirmed_listings.csv"
    )
    confirmed_listings = (
        pd.read_csv(confirmed_listings_path)
        if confirmed_listings_path.exists() else None
    )
    signal_price_coverage = audit_signal_price_coverage(
        universe_snapshots,
        start,
        analysis_end.strftime("%Y-%m-%d"),
        quarterly_fundamentals=quarterly_fundamentals,
        confirmed_listings=confirmed_listings,
        price_date_metadata=price_date_metadata,
    )
    unresolved_by_year = dict(sorted(Counter(
        item["last_price_date"][:4] for item in unresolved_common_ended
    ).items()))
    historical_complete = bool(
        universe_coverage["full_period_covered"]
        and snapshot_price_coverage["complete"]
        and signal_price_coverage["complete"]
        and not missing_price_while_listed
        and not unresolved_common_ended
    )
    report = {
        "status": "PASS" if historical_complete else "INCOMPLETE_STRESSED",
        "benchmark_latest_date": benchmark_end.strftime("%Y-%m-%d"),
        "analysis_end": analysis_end.strftime("%Y-%m-%d"),
        "analysis_start": start,
        "retained_price_files": len(price_paths),
        "histories_ending_early": len(ended),
        "current_common_equities_ending_early": len(current_ended),
        "termination_dates": dict(sorted(by_date.items())),
        "research_common_equity_histories_ending_early": len(research_common_ended),
        "missing_price_histories_while_still_listed": len(missing_price_while_listed),
        "raw_apparent_missing_price_histories_while_still_listed": len(
            raw_missing_price_while_listed
        ),
        "source_confirmed_non_trading_histories": len(
            source_confirmed_non_trading
        ),
        "listed_price_histories_complete": not missing_price_while_listed,
        "candidate_terminal_histories": len(candidate_terminal_histories),
        "right_censored_terminal_histories": len(
            right_censored_terminal_histories
        ),
        "terminal_observation_lag_days": terminal_observation_lag_days,
        "sourced_identity_transition_histories": len(
            sourced_identity_transitions
        ),
        "observed_terminal_returns": len(candidate_terminal_histories) - len(unresolved_common_ended),
        "unresolved_terminal_returns": len(unresolved_common_ended),
        "excluded_non_common_histories_ending_early": len(excluded_non_common_ended),
        "delisting_returns_complete": not unresolved_common_ended,
        "universe_snapshot_coverage": universe_coverage,
        "temporal_security_type_filter": temporal_type_filter,
        "universe_snapshot_coverage_from_2024_10_05": recent_universe_coverage,
        "snapshot_price_coverage_from_2024_10_05": snapshot_price_coverage,
        "signal_price_coverage_from_2021": signal_price_coverage,
        "signal_price_coverage": signal_price_coverage,
        "unresolved_terminal_returns_by_last_price_year": unresolved_by_year,
        "point_in_time_universe_complete_from_2021": universe_coverage["full_period_covered"],
        "backtest_policy": (
            "first session after final observed price receives its sourced terminal return; "
            "unresolved rows receive -100% only in incomplete-data stress diagnostics"
        ),
        "ended_histories": ended,
        "research_common_equity_ended_histories": research_common_ended,
        "missing_price_while_listed_histories": missing_price_while_listed,
        "source_confirmed_non_trading_history_rows": source_confirmed_non_trading,
        "candidate_terminal_history_rows": candidate_terminal_histories,
        "right_censored_terminal_history_rows": right_censored_terminal_histories,
        "sourced_identity_transition_history_rows": (
            sourced_identity_transitions
        ),
        "unresolved_terminal_return_histories": unresolved_common_ended,
    }
    output = Path(PROJECT_PATH) / "output/historical_data_audit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    pd.DataFrame(
        signal_price_coverage["pit_gap_priorities"]
    ).to_csv(
        Path(PROJECT_PATH)
        / "output/historical_pit_gap_priorities.csv",
        index=False,
    )
    return report


def backtest_data_readiness(
    start: str,
    end: str,
    *,
    universe_snapshots: dict[pd.Timestamp, set[str]] | None = None,
    quarterly_fundamentals: pd.DataFrame | None = None,
) -> dict:
    """Return authoritative preflight evidence for a historical backtest."""
    history = audit_historical_price_terminations(
        start=start,
        end=end,
        universe_snapshots=universe_snapshots,
        quarterly_fundamentals=quarterly_fundamentals,
    )
    benchmark_calendar = audit_benchmark_calendar(start, end)
    membership = history["universe_snapshot_coverage"]
    prices = history["signal_price_coverage"]
    quarterly_for_conflicts = (
        quarterly_fundamentals
        if quarterly_fundamentals is not None
        else load_quarterly_fundamentals(
            POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
        )
    )
    conflict_start = pd.Timestamp(start) - pd.DateOffset(years=4)
    conflict_end = pd.Timestamp(end)
    quarterly_conflict_frame = quarterly_for_conflicts.copy()
    for column in ("fiscal_end", "available_date"):
        quarterly_conflict_frame[column] = pd.to_datetime(
            quarterly_conflict_frame[column], errors="coerce"
        )
    quarterly_conflict_frame = quarterly_conflict_frame.loc[
        quarterly_conflict_frame["fiscal_end"].ge(conflict_start)
        & quarterly_conflict_frame["available_date"].le(conflict_end)
    ]
    historical_quarterly_conflicts = quarterly_value_conflicts(
        quarterly_conflict_frame
    )
    benchmark_dates = pd.read_csv(
        NASDAQ_INDEX_FILE, usecols=["date"], parse_dates=["date"]
    )["date"]
    signal_dates = scheduled_signal_dates(
        pd.DatetimeIndex(benchmark_dates), start, end, "monthly"
    )
    quarterly_conflict_sensitivity = (
        quarterly_conflict_order_sensitivity(
            quarterly_conflict_frame,
            signal_dates,
        )
        if historical_quarterly_conflicts
        else {
            "analyzable": True,
            "reason": None,
            "conflict_group_count": 0,
            "conflict_ticker_count": 0,
            "signals_tested": len(signal_dates),
            "affected_signal_count": 0,
            "affected_ticker_signal_count": 0,
            "affected_tickers": [],
            "financial_eligibility_changed_ticker_signal_count": 0,
            "details": [],
        }
    )
    checks = {
        "benchmark_calendar_complete": benchmark_calendar["complete"],
        "point_in_time_membership_complete": prices[
            "signal_membership_snapshots_complete"
        ],
        "signal_member_prices_complete": prices["complete"],
        "signal_member_financials_complete": prices[
            "usable_pit_financial_growth_complete"
        ],
        "listed_price_histories_complete": history[
            "listed_price_histories_complete"
        ],
        "historical_quarterly_value_conflicts_absent": (
            not historical_quarterly_conflicts
        ),
        "observed_delisting_returns_complete": history["delisting_returns_complete"],
    }
    return {
        "start": start,
        "end": end,
        "checks": checks,
        "complete": all(checks.values()),
        "benchmark_calendar": benchmark_calendar,
        "universe_snapshot_coverage": membership,
        "temporal_security_type_filter": history[
            "temporal_security_type_filter"
        ],
        "snapshot_price_coverage": prices,
        "signal_price_coverage": prices,
        "research_common_equity_histories_ending_early": history[
            "research_common_equity_histories_ending_early"
        ],
        "missing_price_histories_while_still_listed": history[
            "missing_price_histories_while_still_listed"
        ],
        "unresolved_terminal_returns": history["unresolved_terminal_returns"],
        "unresolved_terminal_return_histories": history[
            "unresolved_terminal_return_histories"
        ],
        "historical_quarterly_value_conflicts": (
            historical_quarterly_conflicts
        ),
        "historical_quarterly_conflict_order_sensitivity": (
            quarterly_conflict_sensitivity
        ),
    }


def require_complete_backtest_data(start: str, end: str) -> dict:
    readiness = backtest_data_readiness(start, end)
    if not readiness["complete"]:
        failed = [name for name, passed in readiness["checks"].items() if not passed]
        missing_prices = len(readiness["snapshot_price_coverage"]["missing_price_symbols"])
        raise RuntimeError(
            "Backtest data is incomplete; refusing to produce a validation result. "
            f"Failed checks: {', '.join(failed)}; missing historical price symbols: "
            f"{missing_prices}; ended histories without observed terminal returns: "
            f"{readiness.get('unresolved_terminal_returns', readiness['research_common_equity_histories_ending_early'])}. "
            "Use the explicit incomplete-data research option only for diagnostics."
        )
    return readiness


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end")
    args = parser.parse_args()
    report = audit_historical_price_terminations(end=args.end)
    compact = {
        key: value
        for key, value in report.items()
        if not (
            key.endswith("_histories")
            or key.endswith("_rows")
            or key in {
                "termination_dates",
                "snapshot_price_coverage_from_2024_10_05",
                "signal_price_coverage_from_2021",
                "signal_price_coverage",
            }
        )
    }
    compact["snapshot_price_coverage_from_2024_10_05"] = {
        key: value
        for key, value in report[
            "snapshot_price_coverage_from_2024_10_05"
        ].items()
        if key not in {"missing_price_symbols", "by_snapshot"}
    }
    compact["signal_price_coverage_from_2021"] = {
        key: value
        for key, value in report["signal_price_coverage_from_2021"].items()
        if key not in {
            "missing_price_symbols",
            "missing_usable_pit_financial_growth_symbols",
            "missing_no_raw_pit_financial_facts_symbols",
            "missing_insufficient_financial_history_symbols",
            "missing_stale_financial_growth_symbols",
            "absent_price_file_symbols",
            "history_starts_after_signal_symbols",
            "stale_or_ended_history_symbols",
            "internal_price_gap_at_signal_symbols",
            "missing_with_pit_financial_data_symbols",
            "missing_without_pit_financial_data_symbols",
            "missing_never_with_pit_financial_data_symbols",
            "missing_with_and_without_pit_financial_data_symbols",
            "missing_with_pit_financial_data_details",
            "pit_gap_priorities",
            "missing_passing_financial_screen_symbols",
            "confirmed_insufficient_listing_history_symbols",
            "unresolved_observable_potential_competitor_symbols",
            "by_signal",
        }
    }
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
