"""Hard data-readiness checks for backtests and daily recommendations."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_300M_STOCK_LIST_FILE,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
    PROJECT_PATH,
)
from src.io.financial_update import audit_financial_coverage, investable_common_equities
from src.io.fundamentals_update import audit_quarterly_coverage
from src.io.security_identity import load_security_identity
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot
from src.research.shadow_evaluation import nasdaq_calendar_for_year


def audit_security_identity_integrity(frame: pd.DataFrame) -> dict:
    """Reject ambiguous identity rows before research or daily selection."""
    issues = []
    for column in ("provider_ticker", "historical_ticker"):
        values = frame[column].fillna("").astype(str).str.strip().str.upper()
        if values.eq("").any():
            issues.append(f"blank_{column}")
    if (
        frame["provider_ticker"].astype(str).str.upper()
        == frame["historical_ticker"].astype(str).str.upper()
    ).any():
        issues.append("self_mapping")
    verified_at = pd.to_datetime(
        frame["verified_at"], errors="coerce", utc=True
    )
    if verified_at.isna().any():
        issues.append("invalid_verified_at")
    identity_keys = [
        "provider_ticker",
        "historical_ticker",
        "last_historical_date",
        "current_ticker_first_date",
        "identity_type",
    ]
    if frame.duplicated(identity_keys).any():
        issues.append("duplicate_identity_rows")
    renames = frame.loc[frame["identity_type"].eq("issuer_rename")]
    if renames["historical_ticker"].duplicated().any():
        issues.append("issuer_rename_old_ticker_not_one_to_one")
    if renames["provider_ticker"].duplicated().any():
        issues.append("issuer_rename_new_ticker_not_one_to_one")
    return {
        "complete": not issues,
        "row_count": len(frame),
        "issuer_rename_count": len(renames),
        "issues": sorted(set(issues)),
    }


def audit_selected_price_calendars(
    tickers: list[str] | set[str],
    signal_date,
    history_sessions: int = 253,
    price_dir: str | Path | None = None,
) -> dict:
    """Check the exact pre-signal calendar used by selected positions."""
    if history_sessions <= 0:
        raise ValueError("history_sessions must be positive")
    selected = sorted({
        str(ticker).strip().upper()
        for ticker in tickers
        if str(ticker).strip()
        and str(ticker).strip().upper() != "__CASH__"
    })
    signal = pd.Timestamp(signal_date).normalize()
    session_pool = set()
    for year in (signal.year - 1, signal.year):
        session_pool.update(
            nasdaq_calendar_for_year(year)
            .sessions.tz_localize(None).normalize()
        )
    expected = sorted(
        session for session in session_pool if session <= signal
    )[-history_sessions:]
    signal_is_session = signal in session_pool
    root = Path(
        CLEANED_PRICE_DATA_DIR if price_dir is None else price_dir
    )
    gaps = []
    for ticker in selected:
        path = root / f"{ticker.lower()}.csv"
        if not path.exists():
            gaps.append({
                "ticker": ticker,
                "status": "MISSING_PRICE_FILE",
                "missing_session_count": len(expected),
                "missing_sessions": [
                    value.strftime("%Y-%m-%d") for value in expected
                ],
            })
            continue
        dates = pd.to_datetime(
            pd.read_csv(path, usecols=["date"])["date"],
            errors="coerce",
        ).dropna().dt.normalize()
        missing = sorted(set(expected) - set(dates.loc[dates <= signal]))
        if missing:
            gaps.append({
                "ticker": ticker,
                "status": "INCOMPLETE_PRICE_CALENDAR",
                "missing_session_count": len(missing),
                "missing_sessions": [
                    value.strftime("%Y-%m-%d") for value in missing
                ],
            })
    return {
        "complete": bool(
            signal_is_session
            and len(expected) == history_sessions
            and not gaps
        ),
        "signal_date": signal.strftime("%Y-%m-%d"),
        "signal_is_nasdaq_session": signal_is_session,
        "history_sessions": history_sessions,
        "selected_tickers": selected,
        "gaps": gaps,
    }


def quarterly_value_conflicts(frame: pd.DataFrame) -> list[dict]:
    """Return same-availability facts whose values are not deterministic."""
    keys = ["ticker", "fiscal_end", "metric", "available_date"]
    working = frame.copy()
    working["ticker"] = (
        working["ticker"].fillna("").astype(str).str.upper()
    )
    working["value"] = pd.to_numeric(
        working["value"], errors="coerce"
    )
    working = working.dropna(subset=keys + ["value"])
    grouped = working.groupby(keys, dropna=False)
    conflicting_keys = grouped["value"].nunique(dropna=False)
    conflicting_keys = conflicting_keys.loc[conflicting_keys > 1]
    rows = []
    for key in conflicting_keys.index:
        group = grouped.get_group(key)
        rows.append({
            "ticker": str(key[0]),
            "fiscal_end": pd.Timestamp(key[1]).strftime("%Y-%m-%d"),
            "metric": str(key[2]),
            "available_date": pd.Timestamp(key[3]).strftime("%Y-%m-%d"),
            "values": sorted({
                float(value)
                for value in group["value"].dropna()
            }),
            "accessions": sorted({
                str(value)
                for value in group.get(
                    "accession",
                    pd.Series(dtype=str),
                ).dropna()
            }),
        })
    return rows


def quarterly_conflict_order_sensitivity(
    frame: pd.DataFrame,
    signal_dates,
    *,
    maximum_age_days: int = 550,
    minimum_profit_growth: float = 0.25,
    minimum_revenue_growth: float = 0.10,
) -> dict:
    """Measure whether admissible row order changes quarterly snapshots.

    The production snapshot keeps the last fact for a fiscal-period metric.
    Conflicting facts with the same availability date therefore need an
    explicit diagnostic: reversing their input order must not silently change
    historical evidence.  Only conflicting tickers are replayed, plus a
    non-conflicting context ticker when needed to preserve the two-metric
    pivot shape used by the full-universe production call.
    """
    conflicts = quarterly_value_conflicts(frame)
    tickers = sorted({row["ticker"] for row in conflicts})
    signals = pd.DatetimeIndex(pd.to_datetime(signal_dates)).dropna()
    signals = signals.drop_duplicates().sort_values()
    base = {
        "conflict_group_count": len(conflicts),
        "conflict_ticker_count": len(tickers),
        "signals_tested": len(signals),
        "maximum_age_days": maximum_age_days,
        "minimum_profit_growth": minimum_profit_growth,
        "minimum_revenue_growth": minimum_revenue_growth,
        "affected_signal_count": 0,
        "affected_ticker_signal_count": 0,
        "affected_tickers": [],
        "financial_eligibility_changed_ticker_signal_count": 0,
        "context_anchor_ticker": None,
        "details": [],
    }
    if not conflicts or signals.empty:
        return {**base, "analyzable": True, "reason": None}

    working = frame.copy()
    working["ticker"] = (
        working["ticker"].fillna("").astype(str).str.upper()
    )
    for column in ("fiscal_end", "available_date"):
        working[column] = pd.to_datetime(
            working[column], errors="coerce"
        )
    working["value"] = pd.to_numeric(working["value"], errors="coerce")
    conflicted = working.loc[working["ticker"].isin(tickers)].copy()

    required_metrics = {"net_income", "revenue"}
    earliest_signal = signals[0]
    known_conflicted_metrics = set(
        conflicted.loc[
            conflicted["available_date"] <= earliest_signal,
            "metric",
        ]
    )
    anchor = pd.DataFrame(columns=working.columns)
    anchor_ticker = None
    if not required_metrics.issubset(known_conflicted_metrics):
        known_context = working.loc[
            (working["available_date"] <= earliest_signal)
            & ~working["ticker"].isin(tickers)
        ]
        metric_sets = known_context.groupby("ticker")["metric"].agg(set)
        anchors = sorted(
            ticker
            for ticker, metrics in metric_sets.items()
            if required_metrics.issubset(metrics)
        )
        if not anchors:
            return {
                **base,
                "analyzable": False,
                "reason": (
                    "no non-conflicting ticker supplies both quarterly "
                    "metrics at the first signal"
                ),
            }
        anchor_ticker = anchors[0]
        anchor = working.loc[
            working["ticker"].eq(anchor_ticker)
        ].copy()

    # Resolve exact same-availability ties before the production snapshot.
    # Keeping the original first versus last row is equivalent to placing
    # either row last within that tie, but avoids relying on pandas' current
    # unstable sort implementation to preserve a particular input order.
    conflict_keys = [
        "ticker",
        "fiscal_end",
        "metric",
        "available_date",
    ]
    forward = pd.concat([
        anchor,
        conflicted.drop_duplicates(conflict_keys, keep="last"),
    ], ignore_index=True)
    reverse = pd.concat([
        anchor,
        conflicted.drop_duplicates(conflict_keys, keep="first"),
    ], ignore_index=True)
    compared_columns = [
        "net_income_ttm",
        "net_income_growth",
        "revenue_ttm",
        "revenue_growth",
        "growth_available_date",
    ]
    details = []
    for signal_date in signals:
        forward_snapshot = quarterly_growth_snapshot(
            forward, signal_date, maximum_age_days
        )
        reverse_snapshot = quarterly_growth_snapshot(
            reverse, signal_date, maximum_age_days
        )
        for ticker in tickers:
            forward_present = ticker in forward_snapshot.index
            reverse_present = ticker in reverse_snapshot.index
            differences = {}
            if forward_present and reverse_present:
                for column in compared_columns:
                    forward_value = forward_snapshot.at[ticker, column]
                    reverse_value = reverse_snapshot.at[ticker, column]
                    if column == "growth_available_date":
                        equal = (
                            pd.Timestamp(forward_value)
                            == pd.Timestamp(reverse_value)
                        )
                        forward_value = pd.Timestamp(
                            forward_value
                        ).strftime("%Y-%m-%d")
                        reverse_value = pd.Timestamp(
                            reverse_value
                        ).strftime("%Y-%m-%d")
                    else:
                        equal = bool(np.isclose(
                            float(forward_value),
                            float(reverse_value),
                            rtol=1e-12,
                            atol=1e-9,
                            equal_nan=True,
                        ))
                        forward_value = float(forward_value)
                        reverse_value = float(reverse_value)
                    if not equal:
                        differences[column] = {
                            "forward": forward_value,
                            "reverse": reverse_value,
                        }
            if forward_present == reverse_present and not differences:
                continue

            def financially_eligible(snapshot, present):
                if not present:
                    return False
                row = snapshot.loc[ticker]
                return bool(
                    row["net_income_ttm"] > 0
                    and row["net_income_growth"] >= minimum_profit_growth
                    and row["revenue_growth"] >= minimum_revenue_growth
                )

            forward_eligible = financially_eligible(
                forward_snapshot, forward_present
            )
            reverse_eligible = financially_eligible(
                reverse_snapshot, reverse_present
            )
            details.append({
                "ticker": ticker,
                "signal_date": signal_date.strftime("%Y-%m-%d"),
                "forward_present": forward_present,
                "reverse_present": reverse_present,
                "differences": differences,
                "forward_growth_mode_financially_eligible": (
                    forward_eligible
                ),
                "reverse_growth_mode_financially_eligible": (
                    reverse_eligible
                ),
                "financial_eligibility_changed": (
                    forward_eligible != reverse_eligible
                ),
            })

    affected_dates = sorted({
        row["signal_date"] for row in details
    })
    affected_tickers = sorted({
        row["ticker"] for row in details
    })
    return {
        **base,
        "analyzable": True,
        "reason": None,
        "affected_signal_count": len(affected_dates),
        "affected_ticker_signal_count": len(details),
        "affected_tickers": affected_tickers,
        "financial_eligibility_changed_ticker_signal_count": sum(
            row["financial_eligibility_changed"] for row in details
        ),
        "context_anchor_ticker": anchor_ticker,
        "details": details,
    }


def audit_project_data(
    as_of: date,
    minimum_price_coverage: float = 0.95,
    minimum_financial_coverage: float = 0.90,
    minimum_quarterly_financial_coverage: float = 0.90,
    strategy_minimum_price: float = 10.0,
    strategy_minimum_median_dollar_volume: float = 10_000_000.0,
    strategy_minimum_history_sessions: int = 253,
    strategy_market_moving_average_sessions: int = 200,
) -> dict:
    if strategy_market_moving_average_sessions <= 0:
        raise ValueError(
            "strategy_market_moving_average_sessions must be positive"
        )
    universe = investable_common_equities(pd.read_csv(NASDAQ_300M_STOCK_LIST_FILE))
    symbols = universe["Symbol"].dropna().astype(str).str.upper().tolist()
    session_dates_by_year: dict[int, set[pd.Timestamp]] = {}
    index = pd.read_csv(NASDAQ_INDEX_FILE)
    benchmark_missing_columns = sorted(
        {"date", "close"} - set(index.columns)
    )
    if "date" not in index:
        raise ValueError("Nasdaq index file must contain a date column")
    benchmark_dates = pd.to_datetime(
        index["date"], errors="coerce"
    ).dt.normalize()
    valid_benchmark_dates = benchmark_dates.dropna()
    if valid_benchmark_dates.empty:
        raise ValueError("Nasdaq index file has no valid dates")
    benchmark_date = valid_benchmark_dates.max()
    benchmark_duplicate_dates = sorted(
        valid_benchmark_dates.loc[
            valid_benchmark_dates.duplicated(keep=False)
        ]
        .drop_duplicates()
        .dt.strftime("%Y-%m-%d")
        .tolist()
    )
    benchmark_non_session_dates = []
    for year, year_dates in valid_benchmark_dates.groupby(
        valid_benchmark_dates.dt.year
    ):
        year = int(year)
        if year not in session_dates_by_year:
            calendar = nasdaq_calendar_for_year(year)
            session_dates_by_year[year] = set(
                calendar.sessions.tz_localize(None).normalize()
            )
        benchmark_non_session_dates.extend(
            sorted(set(year_dates) - session_dates_by_year[year])
        )
    benchmark_close = (
        pd.to_numeric(index["close"], errors="coerce")
        if "close" in index
        else pd.Series(np.nan, index=index.index)
    )
    invalid_benchmark_close = (
        benchmark_close.isna()
        | ~np.isfinite(benchmark_close)
        | benchmark_close.le(0)
    )
    price_dates, missing_price_files, future_price_rows = {}, [], []
    duplicate_price_dates = []
    non_session_price_rows = []
    invalid_price_schema = []
    invalid_price_values = []
    missing_volume_rows = []
    internal_price_gaps = []
    material_internal_price_gaps = []
    recent_price_session_pool = set()
    for year in (benchmark_date.year - 1, benchmark_date.year):
        recent_price_session_pool.update(
            session_dates_by_year.get(year)
            or set(
                nasdaq_calendar_for_year(year)
                .sessions.tz_localize(None).normalize()
            )
        )
    recent_price_sessions = sorted(
        session for session in recent_price_session_pool
        if session <= benchmark_date
    )[-strategy_minimum_history_sessions:]
    recent_price_session_set = set(recent_price_sessions)
    for ticker in symbols:
        path = Path(CLEANED_PRICE_DATA_DIR) / f"{ticker.lower()}.csv"
        if not path.exists():
            missing_price_files.append(ticker)
            continue
        try:
            price_frame = pd.read_csv(
                path, usecols=["date", "close", "volume"]
            )
        except (OSError, ValueError) as exc:
            invalid_price_schema.append({
                "ticker": ticker,
                "error": str(exc),
            })
            continue
        dates = pd.to_datetime(
            price_frame["date"], errors="coerce"
        ).dt.normalize()
        if dates.empty:
            missing_price_files.append(ticker)
            continue
        close = pd.to_numeric(price_frame["close"], errors="coerce")
        volume = pd.to_numeric(price_frame["volume"], errors="coerce")
        invalid_dates = dates.isna()
        invalid_close = (
            close.isna() | ~np.isfinite(close) | close.le(0)
        )
        invalid_nonempty_volume = (
            price_frame["volume"].notna()
            & (
                volume.isna()
                | ~np.isfinite(volume)
                | volume.lt(0)
            )
        )
        invalid_values = (
            invalid_dates | invalid_close | invalid_nonempty_volume
        )
        if invalid_values.any():
            invalid_price_values.append({
                "ticker": ticker,
                "invalid_row_count": int(invalid_values.sum()),
                "invalid_date_count": int(invalid_dates.sum()),
                "invalid_close_count": int(invalid_close.sum()),
                "invalid_nonempty_volume_count": int(
                    invalid_nonempty_volume.sum()
                ),
            })
        missing_volume_count = int(volume.isna().sum())
        if missing_volume_count:
            missing_volume_rows.append({
                "ticker": ticker,
                "missing_volume_rows": missing_volume_count,
            })
        valid_dates = dates.dropna()
        duplicates = sorted(
            valid_dates.loc[valid_dates.duplicated(keep=False)]
            .drop_duplicates()
            .dt.strftime("%Y-%m-%d")
            .tolist()
        )
        if duplicates:
            duplicate_price_dates.append({
                "ticker": ticker,
                "dates": duplicates,
            })
        invalid_sessions = []
        for year, year_dates in valid_dates.groupby(valid_dates.dt.year):
            year = int(year)
            if year not in session_dates_by_year:
                calendar = nasdaq_calendar_for_year(year)
                session_dates_by_year[year] = set(
                    calendar.sessions.tz_localize(None).normalize()
                )
            invalid_sessions.extend(
                sorted(
                    set(year_dates)
                    - session_dates_by_year[year]
                )
            )
        if invalid_sessions:
            non_session_price_rows.append({
                "ticker": ticker,
                "dates": [
                    value.strftime("%Y-%m-%d")
                    for value in invalid_sessions
                ],
            })
        if valid_dates.empty:
            missing_price_files.append(ticker)
            continue
        price_dates[ticker] = valid_dates.max()
        known = pd.DataFrame({
            "date": dates,
            "close": close,
            "volume": volume,
        }).loc[dates.le(benchmark_date)].sort_values("date")
        if (
            len(recent_price_sessions)
            == strategy_minimum_history_sessions
            and len(known) >= strategy_minimum_history_sessions
            and valid_dates.max() == benchmark_date
        ):
            missing_sessions = sorted(
                recent_price_session_set
                - set(known["date"].dropna())
            )
            if missing_sessions:
                gap = {
                    "ticker": ticker,
                    "missing_session_count": len(missing_sessions),
                    "missing_sessions": [
                        value.strftime("%Y-%m-%d")
                        for value in missing_sessions
                    ],
                }
                internal_price_gaps.append(gap)
                recent = known.tail(50)
                latest_close = known["close"].iloc[-1]
                median_dollar_volume = (
                    recent["close"] * recent["volume"]
                ).median()
                if (
                    np.isfinite(latest_close)
                    and latest_close >= strategy_minimum_price
                    and np.isfinite(median_dollar_volume)
                    and median_dollar_volume
                    >= strategy_minimum_median_dollar_volume
                ):
                    material_internal_price_gaps.append({
                        **gap,
                        "latest_close": float(latest_close),
                        "median_dollar_volume_50d": float(
                            median_dollar_volume
                        ),
                    })
        if valid_dates.max() > pd.Timestamp(as_of):
            future_price_rows.append(ticker)
    current_prices = {
        ticker for ticker, latest in price_dates.items() if latest >= benchmark_date
    }
    price_coverage = len(current_prices) / max(len(symbols), 1)
    material_missing_prices = []
    for ticker in sorted(set(symbols) - current_prices):
        path = Path(CLEANED_PRICE_DATA_DIR) / f"{ticker.lower()}.csv"
        if not path.exists():
            continue
        try:
            history = pd.read_csv(
                path,
                usecols=["date", "close", "volume"],
                parse_dates=["date"],
            ).sort_values("date")
        except (OSError, ValueError):
            continue
        history = history.loc[history["date"] <= benchmark_date]
        if len(history) < strategy_minimum_history_sessions:
            continue
        recent = history.tail(50)
        latest_close = pd.to_numeric(
            history["close"], errors="coerce"
        ).iloc[-1]
        median_dollar_volume = (
            pd.to_numeric(recent["close"], errors="coerce")
            * pd.to_numeric(recent["volume"], errors="coerce")
        ).median()
        if (
            latest_close >= strategy_minimum_price
            and median_dollar_volume
            >= strategy_minimum_median_dollar_volume
        ):
            material_missing_prices.append({
                "ticker": ticker,
                "latest_price_date": history["date"].iloc[-1].strftime(
                    "%Y-%m-%d"
                ),
                "latest_close": float(latest_close),
                "median_dollar_volume_50d": float(median_dollar_volume),
            })

    eps = pd.read_csv(
        POINT_IN_TIME_EPS_FILE,
        parse_dates=["period_end", "available_date", "fetched_at"],
    )
    financial = audit_financial_coverage(eps, symbols, as_of)
    quarterly = pd.read_csv(
        POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
        parse_dates=["fiscal_end", "available_date", "fetched_at"],
    )
    quarterly["ticker"] = quarterly["ticker"].astype(str).str.upper()
    quarterly_conflicts = quarterly_value_conflicts(quarterly)
    known_quarterly = quarterly.loc[
        quarterly["available_date"] <= pd.Timestamp(as_of)
    ]
    metric_sets = known_quarterly.groupby("ticker")["metric"].agg(set)
    addressable_quarterly_symbols = sorted(
        set(symbols)
        & {
            ticker
            for ticker, metrics in metric_sets.items()
            if {"net_income", "revenue"}.issubset(metrics)
        }
    )
    quarterly_universe_coverage = audit_quarterly_coverage(
        quarterly, symbols, as_of
    )
    quarterly_addressable_coverage = audit_quarterly_coverage(
        quarterly, addressable_quarterly_symbols, as_of
    )
    formal_quarterly = quarterly_growth_snapshot(
        quarterly, pd.Timestamp(as_of), maximum_age_days=550
    )
    fresh_quarterly = quarterly_growth_snapshot(
        quarterly, pd.Timestamp(as_of), maximum_age_days=200
    )
    universe_set = set(symbols)
    formal_quarterly_symbols = set(formal_quarterly.index) & universe_set
    fresh_quarterly_symbols = (
        set(fresh_quarterly.index) & formal_quarterly_symbols
    )
    quarterly_strategy_coverage = (
        len(fresh_quarterly_symbols) / len(formal_quarterly_symbols)
        if formal_quarterly_symbols
        else 0.0
    )
    quarterly_formal_universe_coverage = (
        len(formal_quarterly_symbols) / max(len(universe_set), 1)
    )
    quarterly_fresh_universe_coverage = (
        len(fresh_quarterly_symbols) / max(len(universe_set), 1)
    )
    quarterly_fresh_addressable_coverage = (
        len(fresh_quarterly_symbols)
        / max(len(addressable_quarterly_symbols), 1)
    )
    identity_integrity = audit_security_identity_integrity(
        load_security_identity()
    )
    benchmark_age_days = (pd.Timestamp(as_of) - benchmark_date).days
    calendar = nasdaq_calendar_for_year(as_of.year)
    expected_benchmark_date = calendar.date_to_session(
        pd.Timestamp(as_of), direction="previous"
    )
    nearby_sessions = set()
    for year in (as_of.year - 1, as_of.year):
        nearby_sessions.update(
            nasdaq_calendar_for_year(year)
            .sessions.tz_localize(None).normalize()
        )
    eligible_sessions = sorted(
        session for session in nearby_sessions
        if session <= expected_benchmark_date
    )
    expected_recent_benchmark_sessions = eligible_sessions[
        -strategy_market_moving_average_sessions:
    ]
    benchmark_recent_missing_sessions = sorted(
        set(expected_recent_benchmark_sessions)
        - set(valid_benchmark_dates)
    )
    benchmark_recent_history_complete = bool(
        len(expected_recent_benchmark_sessions)
        == strategy_market_moving_average_sessions
        and not benchmark_recent_missing_sessions
    )
    benchmark_on_calendar = (
        calendar.first_session <= benchmark_date <= calendar.last_session
        and calendar.is_session(benchmark_date)
    )
    benchmark_missing_sessions = None
    if benchmark_on_calendar and benchmark_date <= expected_benchmark_date:
        benchmark_missing_sessions = int(
            (
                (calendar.sessions > benchmark_date)
                & (calendar.sessions <= expected_benchmark_date)
            ).sum()
        )
    checks = {
        "benchmark_not_future": bool(benchmark_date <= pd.Timestamp(as_of)),
        "benchmark_fresh": bool(
            benchmark_date.normalize() == expected_benchmark_date
        ),
        "benchmark_schema_complete": not benchmark_missing_columns,
        "benchmark_dates_valid": not benchmark_dates.isna().any(),
        "benchmark_dates_unique": not benchmark_duplicate_dates,
        "benchmark_dates_on_nasdaq_sessions": (
            not benchmark_non_session_dates
        ),
        "benchmark_close_valid": not invalid_benchmark_close.any(),
        "benchmark_recent_history_complete": (
            benchmark_recent_history_complete
        ),
        "price_coverage": price_coverage >= minimum_price_coverage,
        "no_material_missing_strategy_prices": (
            not material_missing_prices
        ),
        "financial_coverage": financial["fresh_coverage"] >= minimum_financial_coverage,
        "quarterly_fundamentals_coverage": (
            quarterly_strategy_coverage
            >= minimum_quarterly_financial_coverage
        ),
        "no_future_price_rows": not future_price_rows,
        "no_duplicate_price_dates": not duplicate_price_dates,
        "price_dates_on_nasdaq_sessions": not non_session_price_rows,
        "price_schema_complete": not invalid_price_schema,
        "price_values_valid": not invalid_price_values,
        "point_in_time_columns_present": {
            "period_end", "available_date", "quarterly_eps", "source"
        }.issubset(eps.columns),
        "security_identity_integrity": identity_integrity["complete"],
    }
    report = {
        "as_of": as_of.isoformat(),
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "common_equity_universe": len(symbols),
        "benchmark_latest_date": benchmark_date.strftime("%Y-%m-%d"),
        "expected_latest_benchmark_session": (
            expected_benchmark_date.strftime("%Y-%m-%d")
        ),
        "benchmark_age_days": benchmark_age_days,
        "benchmark_missing_sessions": benchmark_missing_sessions,
        "benchmark_missing_columns": benchmark_missing_columns,
        "benchmark_invalid_date_rows": int(benchmark_dates.isna().sum()),
        "benchmark_duplicate_dates": benchmark_duplicate_dates,
        "benchmark_non_session_dates": [
            value.strftime("%Y-%m-%d")
            for value in benchmark_non_session_dates
        ],
        "benchmark_invalid_close_rows": int(
            invalid_benchmark_close.sum()
        ),
        "strategy_market_moving_average_sessions": (
            strategy_market_moving_average_sessions
        ),
        "benchmark_recent_expected_sessions": len(
            expected_recent_benchmark_sessions
        ),
        "benchmark_recent_missing_sessions": [
            value.strftime("%Y-%m-%d")
            for value in benchmark_recent_missing_sessions
        ],
        "current_price_files": len(current_prices),
        "price_coverage": price_coverage,
        "minimum_price_coverage": minimum_price_coverage,
        "material_missing_strategy_prices": material_missing_prices,
        "strategy_minimum_price": strategy_minimum_price,
        "strategy_minimum_median_dollar_volume": (
            strategy_minimum_median_dollar_volume
        ),
        "strategy_minimum_history_sessions": (
            strategy_minimum_history_sessions
        ),
        "missing_price_files": missing_price_files,
        "future_price_rows": future_price_rows,
        "duplicate_price_dates": duplicate_price_dates,
        "non_session_price_rows": non_session_price_rows,
        "invalid_price_schema": invalid_price_schema,
        "invalid_price_values": invalid_price_values,
        "missing_volume_rows": missing_volume_rows,
        "internal_price_gaps": internal_price_gaps,
        "material_internal_price_gaps": material_internal_price_gaps,
        "fresh_financial_tickers": financial["fresh_tickers"],
        "financial_coverage": financial["fresh_coverage"],
        "minimum_financial_coverage": minimum_financial_coverage,
        "missing_financials": financial["missing"],
        "stale_financials": financial["stale"],
        "missing_or_stale_financials": financial["missing_or_stale"],
        "quarterly_fundamentals_universe_coverage": (
            quarterly_universe_coverage["fresh_complete_coverage"]
        ),
        "quarterly_fundamentals_addressable_tickers": len(
            addressable_quarterly_symbols
        ),
        "quarterly_fundamentals_simple_addressable_coverage": (
            quarterly_addressable_coverage["fresh_complete_coverage"]
        ),
        "quarterly_fundamentals_formal_usable_tickers": len(
            formal_quarterly_symbols
        ),
        "fresh_quarterly_fundamentals_tickers": (
            len(fresh_quarterly_symbols)
        ),
        "quarterly_fundamentals_coverage": quarterly_strategy_coverage,
        "quarterly_fundamentals_coverage_basis": (
            "fresh_among_formal_growth_usable"
        ),
        "quarterly_fundamentals_formal_universe_coverage": (
            quarterly_formal_universe_coverage
        ),
        "quarterly_fundamentals_fresh_universe_coverage": (
            quarterly_fresh_universe_coverage
        ),
        "quarterly_fundamentals_fresh_addressable_coverage": (
            quarterly_fresh_addressable_coverage
        ),
        "minimum_quarterly_financial_coverage": (
            minimum_quarterly_financial_coverage
        ),
        "missing_or_stale_quarterly_fundamentals": (
            sorted(formal_quarterly_symbols - fresh_quarterly_symbols)
        ),
        "quarterly_value_conflict_count": len(quarterly_conflicts),
        "quarterly_value_conflicts": quarterly_conflicts,
        "security_identity_integrity": identity_integrity,
    }
    output = Path(PROJECT_PATH) / "output/data_audit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def require_project_data(as_of: date) -> dict:
    report = audit_project_data(as_of)
    if report["status"] != "PASS":
        failed = [name for name, passed in report["checks"].items() if not passed]
        raise RuntimeError(f"Data readiness failed: {', '.join(failed)}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=date.today().isoformat())
    args = parser.parse_args()
    result = audit_project_data(date.fromisoformat(args.as_of))
    compact = {key: value for key, value in result.items() if not isinstance(value, list)}
    compact["missing_price_files_count"] = len(result["missing_price_files"])
    compact["missing_financials_count"] = len(result["missing_financials"])
    compact["stale_financials_count"] = len(result["stale_financials"])
    compact["missing_or_stale_financials_count"] = len(result["missing_or_stale_financials"])
    compact["missing_or_stale_quarterly_fundamentals_count"] = len(
        result["missing_or_stale_quarterly_fundamentals"]
    )
    compact["material_missing_strategy_prices_count"] = len(
        result["material_missing_strategy_prices"]
    )
    print(json.dumps(compact, indent=2))
    if result["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
