"""Evaluate recorded monthly recommendation portfolios without backdating evidence."""

from __future__ import annotations

import argparse
import json
import re
from functools import lru_cache
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE
from src.io.security_identity import (
    issuer_rename_transitions,
    remap_weights_after_issuer_rename,
)
from src.research.data_quality import (
    back_adjust_common_splits,
    stock_returns_with_delisting_penalty,
)
from src.research.data_fingerprint import (
    CAN_SLIM_DATA_COMPONENTS,
    data_manifest_sha256_from_components,
)
from src.research.shadow_ledger import (
    PORTFOLIO_SOURCE_COLUMN_MAP,
    SOURCE_IDENTITY_FIELDS,
    github_actions_source_is_valid,
    verify_shadow_ledger,
)


FIXED_TOP3_MODEL_VERSION = "can-slim-top3-v1"
FIXED_TOP3_WEIGHT = 1 / 3
BENCHMARK_ID = "nasdaq-composite"
BENCHMARK_RETURN_SERIES = "close-price-index"
PRICE_ADJUSTMENT_POLICY = (
    "confirmed-actions-plus-common-split-heuristic"
)


@lru_cache(maxsize=16)
def nasdaq_calendar_for_year(year: int):
    """Load a year with boundaries needed for adjacent-session checks."""
    return xcals.get_calendar(
        "XNAS",
        start=f"{year - 1}-12-01",
        end=f"{year + 1}-01-15",
    )


def recorded_signal_provenance(
    records: pd.DataFrame,
    trusted_sources: list[dict] | None = None,
    expected_strategy_sha256: str | None = None,
) -> dict:
    missing = sorted(
        set(PORTFOLIO_SOURCE_COLUMN_MAP.values()) - set(records.columns)
    )
    if missing:
        return {
            "status": "LEGACY_UNANCHORED",
            "externally_anchored": False,
            "missing_columns": missing,
        }
    source = {}
    for field, column in PORTFOLIO_SOURCE_COLUMN_MAP.items():
        missing_values = records[column].isna()
        if missing_values.any():
            return {
                "status": "INCOMPLETE_SOURCE",
                "externally_anchored": False,
                "incomplete_column": column,
                "missing_value_count": int(missing_values.sum()),
            }
        values = records[column].unique()
        if len(values) > 1:
            return {
                "status": "INCONSISTENT_SOURCE",
                "externally_anchored": False,
                "inconsistent_column": column,
            }
        value = values[0] if len(values) else None
        if field in {"run_id", "run_attempt"} and isinstance(value, float):
            value = str(int(value)) if value.is_integer() else str(value)
        elif value is not None:
            value = str(value)
        source[field] = value
    anchored = github_actions_source_is_valid(source)
    if anchored and trusted_sources is not None:
        source_identity = tuple(
            str(source.get(field) or "")
            for field in SOURCE_IDENTITY_FIELDS
        )
        anchored = any(
            source_identity
            == tuple(
                str(candidate.get(field) or "")
                for field in SOURCE_IDENTITY_FIELDS
            )
            for candidate in trusted_sources
            if isinstance(candidate, dict)
        )
        if not anchored:
            return {
                "status": "SOURCE_NOT_IN_LEDGER_CHAIN",
                "externally_anchored": False,
                "source": source,
            }
    if expected_strategy_sha256 is not None:
        column = "portfolio_strategy_sha256"
        if column not in records.columns:
            return {
                "status": "LEGACY_STRATEGY_FINGERPRINT_MISSING",
                "externally_anchored": False,
                "source": source,
            }
        values = records[column]
        if values.isna().any():
            return {
                "status": "INCOMPLETE_STRATEGY_FINGERPRINT",
                "externally_anchored": False,
                "source": source,
            }
        unique = values.astype(str).str.strip().unique()
        if len(unique) != 1 or unique[0] != expected_strategy_sha256:
            return {
                "status": "STRATEGY_FINGERPRINT_MISMATCH",
                "externally_anchored": False,
                "source": source,
                "observed_strategy_sha256": unique.tolist(),
                "expected_strategy_sha256": expected_strategy_sha256,
            }
        source["strategy_sha256"] = unique[0]
        manifest_column = "portfolio_data_manifest_sha256"
        components_column = "portfolio_data_components_json"
        missing_columns = sorted(
            {manifest_column, components_column} - set(records.columns)
        )
        if missing_columns:
            return {
                "status": "LEGACY_DATA_FINGERPRINT_MISSING",
                "externally_anchored": False,
                "source": source,
                "missing_columns": missing_columns,
            }
        if (
            records[manifest_column].isna().any()
            or records[components_column].isna().any()
        ):
            return {
                "status": "INCOMPLETE_DATA_FINGERPRINT",
                "externally_anchored": False,
                "source": source,
            }
        manifest_values = (
            records[manifest_column].astype(str).str.strip().unique()
        )
        component_values = (
            records[components_column].astype(str).str.strip().unique()
        )
        if len(manifest_values) != 1 or len(component_values) != 1:
            return {
                "status": "INCONSISTENT_DATA_FINGERPRINT",
                "externally_anchored": False,
                "source": source,
            }
        try:
            components = json.loads(component_values[0])
        except json.JSONDecodeError:
            components = None
        if (
            not isinstance(components, dict)
            or set(components) != set(CAN_SLIM_DATA_COMPONENTS)
            or any(
                re.fullmatch(r"[0-9a-f]{64}", str(value)) is None
                for value in components.values()
            )
            or data_manifest_sha256_from_components(components)
            != manifest_values[0]
        ):
            return {
                "status": "DATA_FINGERPRINT_INTEGRITY_MISMATCH",
                "externally_anchored": False,
                "source": source,
            }
        source["data_manifest_sha256"] = manifest_values[0]
    return {
        "status": (
            "VERIFIED_GITHUB_ACTIONS"
            if anchored
            else "LOCAL_OR_INVALID_SOURCE"
        ),
        "externally_anchored": anchored,
        "source": source,
    }


def validate_shadow_history(history: pd.DataFrame) -> None:
    """Reject ambiguous ledger structure instead of guessing evidence dates."""
    required = {
        "signal_date",
        "execution_date",
        "generated_at",
        "ticker",
        "target_weight",
        "model_version",
    }
    missing = sorted(required - set(history.columns))
    if missing:
        raise ValueError(f"History lacks forward-audit columns: {missing}")

    model_versions = history["model_version"].astype("string").str.strip()
    if (
        model_versions.isna().any()
        or model_versions.eq("").any()
        or model_versions.nunique() != 1
    ):
        raise ValueError(
            "Shadow ledger must contain exactly one non-empty model_version"
        )

    signal_dates = pd.to_datetime(history["signal_date"], errors="coerce")
    if signal_dates.isna().any():
        raise ValueError(
            "Shadow ledger contains missing or invalid signal_date values"
        )
    generated_at = pd.to_datetime(
        history["generated_at"], utc=True, errors="coerce"
    )
    if generated_at.isna().any():
        raise ValueError(
            "Shadow ledger contains missing or invalid generated_at values"
        )
    tickers = history["ticker"].astype("string").str.strip()
    if tickers.isna().any() or tickers.eq("").any():
        raise ValueError("Shadow ledger contains missing ticker values")

    for signal_date, records in history.groupby("signal_date"):
        raw = records["execution_date"].dropna().astype(str).str.strip()
        raw = raw.loc[raw.ne("")]
        if raw.empty:
            continue
        parsed = pd.to_datetime(raw, errors="coerce")
        if parsed.isna().any():
            raise ValueError(
                f"Signal {signal_date} has invalid execution_date values"
            )
        if len(parsed.dt.normalize().unique()) != 1:
            raise ValueError(
                f"Signal {signal_date} has conflicting execution dates"
            )


def validate_fixed_top3_portfolio(
    normalized_tickers: pd.Series,
    target_weights: pd.Series,
) -> None:
    """Reject shadow targets that differ from the frozen Top 3 construction."""
    cash = normalized_tickers.eq("__CASH__")
    positive = target_weights.gt(1e-12)
    positive_stocks = positive & ~cash
    zero_weight_stocks = ~positive & ~cash
    if zero_weight_stocks.any():
        raise ValueError(
            "Fixed Top 3 portfolio cannot contain zero-weight stock rows"
        )
    if int(positive_stocks.sum()) > 3:
        raise ValueError("Fixed Top 3 portfolio cannot hold more than 3 stocks")
    if (
        positive_stocks.any()
        and not np.allclose(
            target_weights.loc[positive_stocks],
            FIXED_TOP3_WEIGHT,
            rtol=0,
            atol=1e-12,
        )
    ):
        raise ValueError(
            "Fixed Top 3 positive stock weights must each equal 1/3"
        )
    if positive_stocks.any() and cash.any():
        raise ValueError(
            "Fixed Top 3 invested portfolio cannot include the cash sentinel"
        )
    if not positive_stocks.any() and (
        len(normalized_tickers) != 1 or not cash.all()
    ):
        raise ValueError(
            "Fixed Top 3 empty portfolio must use one cash sentinel row"
        )


def empty_shadow_result(
    status: str = "NO_RECORDED_POSITIONS",
    ledger_provenance: dict | None = None,
    transaction_cost_bps: float = 10.0,
    expected_strategy_sha256: str | None = None,
) -> dict:
    return {
        "status": status,
        "accounting_method": "self_financing_fixed_positions",
        "transaction_cost_bps": transaction_cost_bps,
        "benchmark_id": BENCHMARK_ID,
        "benchmark_return_series": BENCHMARK_RETURN_SERIES,
        "price_adjustment_policy": PRICE_ADJUSTMENT_POLICY,
        "strategy_sha256": expected_strategy_sha256,
        "ledger_provenance": ledger_provenance or {
            "status": "NO_LEDGER",
            "integrity_verified": False,
            "externally_anchored": False,
        },
        "recorded_periods": 0,
        "forward_periods": 0,
        "completed_forward_periods": 0,
        "open_forward_periods": 0,
        "completed_period_wins_vs_nasdaq": 0,
        "completed_period_win_rate": None,
        "contiguous_forward_sessions": 0,
        "contiguous_completed_forward_periods": 0,
        "contiguous_completed_period_wins_vs_nasdaq": 0,
        "contiguous_completed_period_win_rate": None,
        "contiguous_forward_strategy_return": None,
        "contiguous_forward_benchmark_return": None,
        "evidence_gap_count": 0,
        "all_contiguous_forward_periods_externally_anchored": False,
        "anchored_forward_periods": 0,
        "unanchored_forward_periods": 0,
        "all_forward_periods_externally_anchored": False,
        "pending_periods": 0,
        "forward_sessions": 0,
        "forward_strategy_return": None,
        "forward_benchmark_return": None,
        "total_transaction_cost": 0.0,
        "total_turnover": 0.0,
        "rebalances": [],
        "periods": [],
    }


def execution_close_utc(execution_date: pd.Timestamp) -> pd.Timestamp:
    """Return the official Nasdaq session close, including early closes."""
    session = pd.Timestamp(execution_date).normalize().tz_localize(None)
    calendar = nasdaq_calendar_for_year(session.year)
    if not calendar.is_session(session):
        raise ValueError(
            f"Execution date is not a Nasdaq trading session: "
            f"{session.date()}"
        )
    return calendar.session_close(session)


def next_nasdaq_session(signal_date: pd.Timestamp) -> pd.Timestamp:
    """Return the first Nasdaq session strictly after a valid signal session."""
    signal = pd.Timestamp(signal_date).normalize().tz_localize(None)
    calendar = nasdaq_calendar_for_year(signal.year)
    if not calendar.is_session(signal):
        raise ValueError(
            f"Signal date is not a Nasdaq trading session: {signal.date()}"
        )
    later_sessions = calendar.sessions[calendar.sessions > signal]
    if not len(later_sessions):
        raise ValueError(
            f"No Nasdaq execution session found after {signal.date()}"
        )
    return pd.Timestamp(later_sessions[0]).tz_localize(None)


def monthly_execution_session(signal_date: pd.Timestamp) -> pd.Timestamp:
    """Validate the frozen month-end signal and return its execution session."""
    signal = pd.Timestamp(signal_date).normalize().tz_localize(None)
    calendar = nasdaq_calendar_for_year(signal.year)
    if not calendar.is_session(signal):
        raise ValueError(
            f"Signal date is not a Nasdaq trading session: {signal.date()}"
        )
    month_sessions = calendar.sessions[
        calendar.sessions.to_period("M") == signal.to_period("M")
    ]
    expected_signal = pd.Timestamp(month_sessions[-1]).tz_localize(None)
    if signal != expected_signal:
        raise ValueError(
            "Signal date must be the final Nasdaq trading session of its "
            f"calendar month: expected {expected_signal.date()}, "
            f"got {signal.date()}"
        )
    return next_nasdaq_session(signal)


def evaluate_recorded_portfolio(
    records: pd.DataFrame,
    close: pd.DataFrame,
    benchmark_close: pd.Series,
    transaction_cost_bps: float = 10.0,
    evaluation_end: pd.Timestamp | None = None,
    trusted_sources: list[dict] | None = None,
    model_version: str | None = None,
    expected_strategy_sha256: str | None = None,
) -> dict:
    if records.empty:
        raise ValueError("No recommendation records")
    if (
        not np.isfinite(transaction_cost_bps)
        or transaction_cost_bps < 0
    ):
        raise ValueError(
            "transaction_cost_bps must be finite and non-negative"
        )
    normalized_tickers = records["ticker"].astype(str).str.upper()
    if normalized_tickers.duplicated().any():
        raise ValueError("Recorded portfolio contains duplicate tickers")
    target_weights = pd.to_numeric(
        records["target_weight"], errors="coerce"
    )
    if (
        target_weights.isna().any()
        or not np.isfinite(target_weights).all()
        or target_weights.lt(0).any()
        or target_weights.sum() > 1.0 + 1e-9
    ):
        raise ValueError(
            "Recorded portfolio weights must be finite, non-negative, "
            "and sum to at most 1"
        )
    if target_weights.loc[
        normalized_tickers.eq("__CASH__")
    ].ne(0).any():
        raise ValueError("The cash sentinel must have zero target weight")
    if model_version == FIXED_TOP3_MODEL_VERSION:
        validate_fixed_top3_portfolio(
            normalized_tickers,
            target_weights,
        )
    signal_date = pd.Timestamp(records["signal_date"].iloc[0])
    signal_provenance = recorded_signal_provenance(
        records,
        trusted_sources=trusted_sources,
        expected_strategy_sha256=expected_strategy_sha256,
    )
    execution_raw = records["execution_date"].iloc[0]
    if pd.isna(execution_raw) or str(execution_raw).strip() == "":
        return {
            "signal_date": signal_date.strftime("%Y-%m-%d"),
            "execution_date": None,
            "status": "PENDING_EXECUTION",
            "signal_provenance": signal_provenance,
            "forward_eligible": True,
            "forward_sessions": 0,
        }
    execution_date = pd.Timestamp(execution_raw)
    portfolio_time_column = (
        "portfolio_generated_at"
        if "portfolio_generated_at" in records.columns
        else "generated_at"
    )
    portfolio_times = pd.to_datetime(
        records[portfolio_time_column],
        utc=True,
        errors="coerce",
    )
    portfolio_timestamp_complete = not portfolio_times.isna().any()
    first_generated_at = (
        portfolio_times.min()
        if portfolio_timestamp_complete
        else None
    )
    generated_at = (
        portfolio_times.max()
        if portfolio_timestamp_complete
        else None
    )
    close_cutoff = execution_close_utc(execution_date)
    expected_execution_date = monthly_execution_session(signal_date)
    signal_close_cutoff = execution_close_utc(signal_date)
    normalized_execution_date = (
        execution_date.normalize().tz_localize(None)
    )
    if normalized_execution_date != expected_execution_date:
        raise ValueError(
            "Execution date must be the first Nasdaq trading session after "
            f"the signal date: expected {expected_execution_date.date()}, "
            f"got {normalized_execution_date.date()}"
        )
    # Every row must be generated from the completed signal close and before
    # the execution close.  Checking both ends prevents mixed early/late rows
    # from making an invalid portfolio look eligible.
    portfolio_timestamp_window_valid = (
        portfolio_timestamp_complete
        and first_generated_at >= signal_close_cutoff
        and generated_at < close_cutoff
    )
    forward_eligible = (
        portfolio_timestamp_window_valid
    )
    weights = records.set_index(
        records["ticker"].astype(str).str.upper()
    )["target_weight"].astype(float)
    tickers = weights.loc[weights > 0].index.tolist()
    available_tickers = [ticker for ticker in tickers if ticker in close.columns]
    if set(available_tickers) != set(tickers):
        missing = sorted(set(tickers) - set(available_tickers))
        raise ValueError(f"Missing shadow prices: {missing}")
    common_index = close.index.intersection(benchmark_close.index).sort_values()
    evaluation_index = common_index[common_index >= execution_date]
    if evaluation_end is not None:
        evaluation_index = evaluation_index[evaluation_index <= evaluation_end]
    if not len(evaluation_index) or evaluation_index[0] != execution_date:
        raise ValueError(f"Execution close {execution_date.date()} is unavailable")
    if tickers:
        panel = close.loc[evaluation_index, tickers]
        returns = stock_returns_with_delisting_penalty(panel).iloc[1:].fillna(0.0)
        growth = (1 + returns).prod()
    else:
        returns = pd.DataFrame(index=evaluation_index[1:])
        growth = pd.Series(dtype=float)
    exposure = float(weights.abs().sum())
    cost_rate = transaction_cost_bps / 10_000
    post_trade_nav = 1 / (1 + exposure * cost_rate)
    transaction_cost = 1 - post_trade_nav
    ending_nav = post_trade_nav * (1 - float(weights.sum()))
    if tickers:
        ending_nav += float(
            (
                weights.reindex(tickers)
                * post_trade_nav
                * growth.reindex(tickers)
            ).sum()
        )
    strategy_return = ending_nav - 1
    benchmark_window = benchmark_close.loc[evaluation_index]
    benchmark_return = benchmark_window.iloc[-1] / benchmark_window.iloc[0] - 1
    sessions = len(returns)
    return {
        "accounting_method": "standalone_fixed_positions",
        "signal_date": signal_date.strftime("%Y-%m-%d"),
        "execution_date": execution_date.strftime("%Y-%m-%d"),
        "evaluation_end": evaluation_index[-1].strftime("%Y-%m-%d"),
        "generated_at": (
            generated_at.isoformat() if generated_at is not None else None
        ),
        "portfolio_generated_at": (
            generated_at.isoformat() if generated_at is not None else None
        ),
        "portfolio_first_row_generated_at": (
            first_generated_at.isoformat()
            if first_generated_at is not None
            else None
        ),
        "portfolio_timestamp_complete": portfolio_timestamp_complete,
        "portfolio_timestamp_window_valid": (
            portfolio_timestamp_window_valid
        ),
        "signal_close_utc": signal_close_cutoff.isoformat(),
        "execution_close_utc": close_cutoff.isoformat(),
        "status": "FORWARD" if forward_eligible else "RETROSPECTIVE_SEED",
        "signal_provenance": signal_provenance,
        "forward_eligible": forward_eligible,
        "forward_sessions": sessions if forward_eligible else 0,
        "observed_sessions": sessions,
        "strategy_return": float(strategy_return),
        "benchmark_return": float(benchmark_return),
        "excess_return": float(strategy_return - benchmark_return),
        "target_exposure": float(weights.sum()),
        "transaction_cost": transaction_cost,
        "transaction_cost_bps": transaction_cost_bps,
    }


def evaluate_forward_account(
    schedules: list[dict],
    close: pd.DataFrame,
    benchmark_close: pd.Series,
    transaction_cost_bps: float,
    identity_transitions: pd.DataFrame | None = None,
) -> dict:
    """Replay recorded targets as one self-financing fixed-position account."""
    if (
        not np.isfinite(transaction_cost_bps)
        or transaction_cost_bps < 0
    ):
        raise ValueError(
            "transaction_cost_bps must be finite and non-negative"
        )
    if not schedules:
        return {
            "forward_sessions": 0,
            "forward_strategy_return": None,
            "forward_benchmark_return": None,
            "total_transaction_cost": 0.0,
            "total_turnover": 0.0,
            "rebalances": [],
            "continuous_periods": [],
            "completed_forward_periods": 0,
            "open_forward_periods": 0,
            "completed_period_wins_vs_nasdaq": 0,
            "completed_period_win_rate": None,
            "contiguous_forward_sessions": 0,
            "contiguous_completed_forward_periods": 0,
            "contiguous_completed_period_wins_vs_nasdaq": 0,
            "contiguous_completed_period_win_rate": None,
            "contiguous_forward_strategy_return": None,
            "contiguous_forward_benchmark_return": None,
            "evidence_gap_count": 0,
            "all_contiguous_forward_periods_externally_anchored": False,
        }
    schedules = sorted(schedules, key=lambda item: item["execution_date"])
    start = schedules[0]["execution_date"]
    end = max(item["evaluation_end"] for item in schedules)
    common_index = close.index.intersection(
        benchmark_close.index
    ).sort_values()
    timeline = common_index[(common_index >= start) & (common_index <= end)]
    if not len(timeline) or timeline[0] != start:
        raise ValueError(
            f"First forward execution close {start.date()} is unavailable"
        )
    schedule_by_date = {
        item["execution_date"]: item for item in schedules
    }
    returns = stock_returns_with_delisting_penalty(
        close.reindex(timeline)
    ).fillna(0.0)
    identity_transitions = (
        issuer_rename_transitions()
        if identity_transitions is None
        else identity_transitions
    )
    position_values = pd.Series(0.0, index=close.columns)
    cash = 1.0
    cost_rate = transaction_cost_bps / 10_000
    total_cost = 0.0
    total_turnover = 0.0
    rebalances = []
    continuous_periods = []
    active_period = None
    nav = 1.0
    for row_index, current_date in enumerate(timeline):
        if row_index:
            daily_returns = returns.loc[current_date].copy()
            for transition in identity_transitions.itertuples(index=False):
                if current_date != transition.current_ticker_first_date:
                    continue
                old = transition.historical_ticker
                new = transition.provider_ticker
                if old not in close or new not in close:
                    continue
                old_value = float(position_values[old])
                if abs(old_value) <= 1e-12:
                    continue
                position_values[new] += old_value
                position_values[old] = 0.0
                old_history = close.loc[
                    close.index < current_date, old
                ].dropna()
                old_price = (
                    float(old_history.iloc[-1])
                    if len(old_history)
                    else float("nan")
                )
                new_price = float(close.loc[current_date, new])
                if (
                    np.isfinite(old_price) and old_price > 0
                    and np.isfinite(new_price) and new_price > 0
                ):
                    daily_returns.loc[new] = new_price / old_price - 1
            position_values = position_values.mul(1 + daily_returns)
        pre_trade_nav = float(cash + position_values.sum())
        scheduled = schedule_by_date.get(current_date)
        if scheduled is not None:
            if active_period is not None:
                prior_signal = pd.Timestamp(
                    active_period["identity"]["signal_date"]
                )
                current_signal = pd.Timestamp(scheduled["signal_date"])
                signal_month_gap = (
                    (current_signal.year - prior_signal.year) * 12
                    + current_signal.month
                    - prior_signal.month
                )
                period_strategy_return = (
                    pre_trade_nav / active_period["start_nav"] - 1
                )
                period_benchmark_return = float(
                    benchmark_close.loc[current_date]
                    / active_period["start_benchmark"]
                    - 1
                )
                continuous_periods.append({
                    **active_period["identity"],
                    "period_end": current_date.strftime("%Y-%m-%d"),
                    "completed": True,
                    "next_signal_date": current_signal.strftime(
                        "%Y-%m-%d"
                    ),
                    "signal_month_gap": signal_month_gap,
                    "monthly_evidence_contiguous": (
                        signal_month_gap == 1
                    ),
                    "strategy_return": period_strategy_return,
                    "benchmark_return": period_benchmark_return,
                    "excess_return": (
                        period_strategy_return - period_benchmark_return
                    ),
                })
            active_period = {
                "start_nav": pre_trade_nav,
                "start_benchmark": float(
                    benchmark_close.loc[current_date]
                ),
                "identity": {
                    "signal_date": scheduled["signal_date"].strftime(
                        "%Y-%m-%d"
                    ),
                    "execution_date": current_date.strftime("%Y-%m-%d"),
                    "signal_provenance": scheduled.get(
                        "signal_provenance", {}
                    ),
                },
            }
            target = pd.Series(0.0, index=close.columns)
            target.update(scheduled["weights"])
            target = remap_weights_after_issuer_rename(
                target, current_date, identity_transitions
            )
            post_trade_nav = pre_trade_nav
            for _ in range(20):
                desired = target * post_trade_nav
                traded = float((desired - position_values).abs().sum())
                updated = pre_trade_nav - traded * cost_rate
                if abs(updated - post_trade_nav) < 1e-12:
                    post_trade_nav = updated
                    break
                post_trade_nav = updated
            desired = target * post_trade_nav
            traded = float((desired - position_values).abs().sum())
            transaction_cost = traded * cost_rate
            turnover = traded / pre_trade_nav if pre_trade_nav else 0.0
            position_values = desired
            cash = float(post_trade_nav - desired.sum())
            total_cost += transaction_cost
            total_turnover += turnover
            rebalances.append({
                "signal_date": scheduled["signal_date"].strftime(
                    "%Y-%m-%d"
                ),
                "execution_date": current_date.strftime("%Y-%m-%d"),
                "pre_trade_nav": pre_trade_nav,
                "post_trade_nav": float(post_trade_nav),
                "turnover": turnover,
                "transaction_cost": transaction_cost,
                "target_exposure": float(target.sum()),
            })
        nav = float(cash + position_values.sum())
    if active_period is not None:
        period_strategy_return = nav / active_period["start_nav"] - 1
        period_benchmark_return = float(
            benchmark_close.loc[timeline[-1]]
            / active_period["start_benchmark"]
            - 1
        )
        continuous_periods.append({
            **active_period["identity"],
            "period_end": timeline[-1].strftime("%Y-%m-%d"),
            "completed": False,
            "next_signal_date": None,
            "signal_month_gap": None,
            "monthly_evidence_contiguous": None,
            "strategy_return": period_strategy_return,
            "benchmark_return": period_benchmark_return,
            "excess_return": (
                period_strategy_return - period_benchmark_return
            ),
        })
    benchmark_window = benchmark_close.loc[timeline]
    completed_periods = [
        period for period in continuous_periods if period["completed"]
    ]
    completed_wins = sum(
        period["excess_return"] > 0 for period in completed_periods
    )
    gap_indexes = [
        index
        for index, period in enumerate(continuous_periods)
        if (
            period["completed"]
            and not period["monthly_evidence_contiguous"]
        )
    ]
    streak_start = gap_indexes[-1] + 1 if gap_indexes else 0
    contiguous_periods = continuous_periods[streak_start:]
    contiguous_completed = [
        period for period in contiguous_periods if period["completed"]
    ]
    contiguous_wins = sum(
        period["excess_return"] > 0
        for period in contiguous_completed
    )
    if contiguous_periods:
        contiguous_start = pd.Timestamp(
            contiguous_periods[0]["execution_date"]
        )
        contiguous_forward_sessions = max(
            int((timeline >= contiguous_start).sum()) - 1,
            0,
        )
        contiguous_strategy_return = float(
            pd.Series([
                1 + period["strategy_return"]
                for period in contiguous_periods
            ]).prod()
            - 1
        )
        contiguous_benchmark_return = float(
            pd.Series([
                1 + period["benchmark_return"]
                for period in contiguous_periods
            ]).prod()
            - 1
        )
    else:
        contiguous_forward_sessions = 0
        contiguous_strategy_return = None
        contiguous_benchmark_return = None
    return {
        "forward_sessions": max(len(timeline) - 1, 0),
        "forward_strategy_return": nav - 1,
        "forward_benchmark_return": float(
            benchmark_window.iloc[-1] / benchmark_window.iloc[0] - 1
        ),
        "total_transaction_cost": total_cost,
        "total_turnover": total_turnover,
        "rebalances": rebalances,
        "continuous_periods": continuous_periods,
        "completed_forward_periods": len(completed_periods),
        "open_forward_periods": (
            len(continuous_periods) - len(completed_periods)
        ),
        "completed_period_wins_vs_nasdaq": int(completed_wins),
        "completed_period_win_rate": (
            completed_wins / len(completed_periods)
            if completed_periods else None
        ),
        "contiguous_forward_sessions": contiguous_forward_sessions,
        "contiguous_completed_forward_periods": len(
            contiguous_completed
        ),
        "contiguous_completed_period_wins_vs_nasdaq": int(
            contiguous_wins
        ),
        "contiguous_completed_period_win_rate": (
            contiguous_wins / len(contiguous_completed)
            if contiguous_completed else None
        ),
        "contiguous_forward_strategy_return": (
            contiguous_strategy_return
        ),
        "contiguous_forward_benchmark_return": (
            contiguous_benchmark_return
        ),
        "evidence_gap_count": len(gap_indexes),
        "all_contiguous_forward_periods_externally_anchored": (
            bool(contiguous_periods)
            and all(
                period.get("signal_provenance", {}).get(
                    "externally_anchored", False
                )
                for period in contiguous_periods
            )
        ),
    }


def evaluate_history(
    history_file: str | Path,
    output_file: str | Path,
    transaction_cost_bps: float = 10.0,
    expected_strategy_sha256: str | None = None,
) -> dict:
    if (
        not np.isfinite(transaction_cost_bps)
        or transaction_cost_bps < 0
    ):
        raise ValueError(
            "transaction_cost_bps must be finite and non-negative"
        )
    history_path = Path(history_file)
    ledger_provenance = verify_shadow_ledger(history_path)
    if not history_path.exists() or history_path.stat().st_size == 0:
        result = empty_shadow_result(
            ledger_provenance=ledger_provenance,
            transaction_cost_bps=transaction_cost_bps,
            expected_strategy_sha256=expected_strategy_sha256,
        )
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        Path(output_file).write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        return result
    history = pd.read_csv(history_path)
    # Older runs may have written a header-only history before forward-audit
    # columns such as ``execution_date`` were introduced.  With no rows there
    # is nothing to migrate or evaluate, so treat it exactly like a missing
    # history instead of blocking the daily shadow pipeline.
    if history.empty:
        result = empty_shadow_result(
            ledger_provenance=ledger_provenance,
            transaction_cost_bps=transaction_cost_bps,
            expected_strategy_sha256=expected_strategy_sha256,
        )
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        Path(output_file).write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        return result
    validate_shadow_history(history)
    history = history.loc[history["signal_date"].notna()].copy()
    history["signal_date"] = pd.to_datetime(history["signal_date"])
    signal_dates = sorted(history["signal_date"].unique())
    if not signal_dates:
        raise ValueError("History has no recorded signal date")
    positive_weights = pd.to_numeric(
        history["target_weight"], errors="coerce"
    ).fillna(0).gt(0)
    tickers = (
        history.loc[positive_weights, "ticker"]
        .astype(str)
        .str.lower()
        .unique()
        .tolist()
    )
    benchmark = pd.read_csv(NASDAQ_INDEX_FILE, index_col="date", parse_dates=True)["close"].sort_index()
    close = pd.DataFrame({
        ticker.upper(): pd.read_csv(
            Path(CLEANED_PRICE_DATA_DIR) / f"{ticker}.csv",
            index_col="date",
            parse_dates=True,
        )["close"]
        for ticker in tickers
    }).sort_index()
    if not tickers:
        close = pd.DataFrame(index=benchmark.index)
    close = back_adjust_common_splits(close)
    model_version = str(history["model_version"].iloc[0])
    periods = []
    forward_schedules = []
    for index, signal_date in enumerate(signal_dates):
        all_records = history.loc[history["signal_date"] == signal_date].copy()
        generated = pd.to_datetime(all_records["generated_at"], utc=True)
        first_generation = generated.min()
        records = all_records.loc[generated == first_generation].copy()
        known_executions = pd.to_datetime(
            all_records["execution_date"], errors="coerce"
        ).dropna().dt.normalize().drop_duplicates()
        if len(known_executions):
            records["execution_date"] = known_executions.min().strftime("%Y-%m-%d")
        period_end = None
        if index + 1 < len(signal_dates):
            next_records = history.loc[history["signal_date"] == signal_dates[index + 1]]
            next_executions = pd.to_datetime(
                next_records["execution_date"], errors="coerce"
            ).dropna()
            if len(next_executions):
                candidates = benchmark.index[benchmark.index < next_executions.min()]
                period_end = candidates[-1] if len(candidates) else None
        period = evaluate_recorded_portfolio(
            records,
            close,
            benchmark,
            transaction_cost_bps,
            period_end,
            trusted_sources=ledger_provenance.get("trusted_sources", []),
            model_version=model_version,
            expected_strategy_sha256=expected_strategy_sha256,
        )
        periods.append(period)
        if (
            period.get("forward_eligible")
            and period.get("execution_date") is not None
        ):
            weights = records.set_index(
                records["ticker"].astype(str).str.upper()
            )["target_weight"].astype(float)
            weights = weights.loc[weights > 0]
            forward_schedules.append({
                "signal_date": signal_date,
                "execution_date": pd.Timestamp(period["execution_date"]),
                "evaluation_end": pd.Timestamp(period["evaluation_end"]),
                "weights": weights,
                "signal_provenance": period.get(
                    "signal_provenance", {}
                ),
            })
    forward_periods = [
        period for period in periods
        if period.get("forward_eligible")
        and period.get("execution_date") is not None
    ]
    pending_periods = [
        period for period in periods
        if period.get("status") == "PENDING_EXECUTION"
    ]
    account = evaluate_forward_account(
        forward_schedules, close, benchmark, transaction_cost_bps
    )
    anchored_forward_periods = sum(
        bool(
            period.get("signal_provenance", {}).get(
                "externally_anchored"
            )
        )
        for period in forward_periods
    )
    result = {
        "status": (
            "PASS" if forward_periods else "NO_FORWARD_EVIDENCE"
        ),
        "model_version": model_version,
        "accounting_method": "self_financing_fixed_positions",
        "transaction_cost_bps": transaction_cost_bps,
        "benchmark_id": BENCHMARK_ID,
        "benchmark_return_series": BENCHMARK_RETURN_SERIES,
        "price_adjustment_policy": PRICE_ADJUSTMENT_POLICY,
        "strategy_sha256": expected_strategy_sha256,
        "ledger_provenance": ledger_provenance,
        "recorded_periods": len(periods),
        "forward_periods": len(forward_periods),
        "anchored_forward_periods": anchored_forward_periods,
        "unanchored_forward_periods": (
            len(forward_periods) - anchored_forward_periods
        ),
        "all_forward_periods_externally_anchored": (
            bool(forward_periods)
            and anchored_forward_periods == len(forward_periods)
        ),
        "pending_periods": len(pending_periods),
        **account,
        "periods": periods,
    }
    output = Path(output_file)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--expected-strategy-sha256")
    args = parser.parse_args()
    print(json.dumps(evaluate_history(
        args.history,
        args.output,
        args.cost_bps,
        args.expected_strategy_sha256,
    ), indent=2))


if __name__ == "__main__":
    main()
