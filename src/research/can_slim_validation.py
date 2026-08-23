"""Validate and freeze the canonical concentrated CAN SLIM policy."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

import pandas as pd

from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_FUNDAMENTALS_FILE,
    POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
)
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot
from src.financial.quarterly_fundamentals import quarterly_profit_ttm_snapshot
from src.io.fundamentals_update import (
    SEC_COMPANYFACTS_CACHE_DIR,
    cached_companyfacts_symbol_payload_profiles,
    verify_companyfacts_cache_manifest,
)
from src.research.can_slim import (
    CanSlimConfig,
    build_can_slim_technical_cross_section,
    calculate_can_slim_returns,
    calculate_can_slim_returns_with_ledger,
    can_slim_nonfinancial_candidate_mask,
    score_can_slim_cross_section,
)
from src.research import can_slim as can_slim_module
from src.research.data_quality import back_adjust_common_splits
from src.research.data_fingerprint import can_slim_input_fingerprints
from src.research.historical_data_audit import (
    backtest_data_readiness,
)
from src.research.metrics import moving_block_bootstrap
from src.research.panel_data import load_panel
from src.research.universe_history import load_universe_snapshots, universe_as_of
from src.research.validation_artifacts import (
    VALIDATION_ARTIFACT_MANIFEST_NAME,
    build_validation_artifact_manifest,
)
from src.strategy.common import market_regime_is_on, scheduled_signal_dates


MODEL_VERSION = "can-slim-top3-v1"
POLICY_FROZEN_AT = "2026-07-18"
_COST_STRESS_SELECTION_CACHE_LOCK = threading.RLock()


@contextmanager
def _memoized_cost_stress_selection():
    """Reuse cost-invariant selector results inside one validation loop."""
    with _COST_STRESS_SELECTION_CACHE_LOCK:
        original = can_slim_module.select_can_slim_portfolio
        cache: dict[tuple, pd.DataFrame] = {}

        def cached_select(
            date,
            close,
            dollar_volume,
            index_close,
            eps,
            config,
            eligible_symbols=None,
            quarterly_fundamentals=None,
            keltner_upper=None,
            eligibility_close=None,
        ):
            config_key = tuple(sorted(
                (name, value)
                for name, value in asdict(config).items()
                if name != "transaction_cost_bps"
            ))
            key = (
                pd.Timestamp(date),
                config_key,
                (
                    None
                    if eligible_symbols is None
                    else frozenset(eligible_symbols)
                ),
            )
            if key not in cache:
                cache[key] = original(
                    date,
                    close,
                    dollar_volume,
                    index_close,
                    eps,
                    config,
                    eligible_symbols,
                    quarterly_fundamentals,
                    keltner_upper,
                    eligibility_close,
                )
            return cache[key].copy(deep=True)

        can_slim_module.select_can_slim_portfolio = cached_select
        try:
            yield
        finally:
            can_slim_module.select_can_slim_portfolio = original


def _sec_cache_refresh_tier(
    reporting_profile: str,
    missing_signal_count: int,
    raw_cache_profile: str = "",
) -> int:
    """Prioritize actionable gaps without exhausting low-yield partial tails."""
    if raw_cache_profile == "FDIC_EXCHANGE_ACT_NO_SEC_COMPANYFACTS":
        return 98
    if reporting_profile == "SEC_QUARTERLY_PARTIAL":
        return 1 if missing_signal_count >= 3 else 3
    if reporting_profile == "NO_PARSED_SEC_FINANCIALS":
        return 2 if missing_signal_count >= 3 else 4
    return {
        "SEC_REVENUE_ONLY_NO_NET_INCOME_FACTS": 5,
        "SEC_NET_INCOME_ONLY_NO_REVENUE_FACTS": 6,
        "SEC_ANNUAL_ONLY_OR_UNMAPPED_QUARTERLY": 7,
        "FOREIGN_ANNUAL_ONLY_NEEDS_QUARTERLY_SOURCE": 8,
    }.get(reporting_profile, 99)


def _financial_reporting_profile(
    forms: set[str],
    reasons: Counter,
    quarterly_metrics: set[str],
) -> str:
    if forms & {"20-F", "20-F/A", "40-F", "40-F/A"}:
        return "FOREIGN_ANNUAL_ONLY_NEEDS_QUARTERLY_SOURCE"
    if quarterly_metrics == {"net_income"}:
        return "SEC_NET_INCOME_ONLY_NO_REVENUE_FACTS"
    if quarterly_metrics == {"revenue"}:
        return "SEC_REVENUE_ONLY_NO_NET_INCOME_FACTS"
    if reasons["insufficient_growth_history"]:
        return "SEC_QUARTERLY_PARTIAL"
    if forms:
        return "SEC_ANNUAL_ONLY_OR_UNMAPPED_QUARTERLY"
    return "NO_PARSED_SEC_FINANCIALS"


def _recommended_financial_data_action(
    reporting_profile: str,
    raw_cache_profile: str,
    has_supported_revenue_source: bool = False,
) -> str:
    if raw_cache_profile == "FDIC_EXCHANGE_ACT_NO_SEC_COMPANYFACTS":
        return "NEEDS_FDIC_ARCHIVED_QUARTERLY_SOURCE"
    if raw_cache_profile == "NOT_CACHED":
        return "FETCH_SEC_COMPANYFACTS"
    if raw_cache_profile in {
        "FOREIGN_PERIODIC_NO_10Q",
        "IFRS_WITHOUT_SUPPORTED_QUARTERS",
    }:
        return "NEEDS_FOREIGN_QUARTERLY_SOURCE"
    if (
        reporting_profile == "NO_PARSED_SEC_FINANCIALS"
        and raw_cache_profile == "US_GAAP_WITH_10Q"
    ):
        return "REVIEW_US_GAAP_PARSER"
    if (
        raw_cache_profile == "US_GAAP_WITH_10Q"
        and has_supported_revenue_source
    ):
        return "REPARSE_OR_ACCEPT_HISTORY_LIMIT"
    if (
        reporting_profile
        == "SEC_NET_INCOME_ONLY_NO_REVENUE_FACTS"
        and raw_cache_profile == "US_GAAP_WITH_10Q"
    ):
        return "CONFIRM_NO_OPERATING_REVENUE"
    if reporting_profile == "SEC_QUARTERLY_PARTIAL":
        return "REPARSE_OR_ACCEPT_HISTORY_LIMIT"
    return "REVIEW_FACT_CONCEPT_MAPPING"


def _effective_raw_financial_profile(
    raw_cache_profile: str,
    quarterly_taxonomies: set[str],
) -> str:
    """Route proven non-SEC filers away from the SEC refresh queue."""
    if any(
        str(taxonomy).strip().lower().startswith("fdic-")
        for taxonomy in quarterly_taxonomies
    ):
        return "FDIC_EXCHANGE_ACT_NO_SEC_COMPANYFACTS"
    return raw_cache_profile


def technical_candidate_financial_coverage(
    close: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    nasdaq: pd.Series,
    quarterly: pd.DataFrame,
    snapshots: dict[pd.Timestamp, set[str]],
    config: CanSlimConfig,
    start: str = "2021-01-01",
    annual_fundamentals: pd.DataFrame | None = None,
    raw_cache_profiles: dict[str, dict] | None = None,
    adjusted_close: pd.DataFrame | None = None,
) -> dict:
    """Audit financial coverage only where a missing fact could change trades."""
    prices = (
        adjusted_close
        if adjusted_close is not None
        else back_adjust_common_splits(close)
    ).sort_index()
    dollar_volume = dollar_volume.reindex_like(prices)
    eligibility_close = close.reindex_like(prices)
    nasdaq = nasdaq.reindex(prices.index).ffill()
    rows = []
    missing_union: set[str] = set()
    missing_counts: Counter = Counter()
    reason_counts: dict[str, Counter] = {}
    first_missing: dict[str, pd.Timestamp] = {}
    last_missing: dict[str, pd.Timestamp] = {}
    for signal_date in scheduled_signal_dates(
        prices.index, start, config.end, config.signal_frequency
    ):
        risk_on = market_regime_is_on(
            signal_date, nasdaq, config.market_ma_days
        )
        if not risk_on:
            rows.append({
                "signal_date": signal_date.strftime("%Y-%m-%d"),
                "market_regime_on": False,
                "technical_candidate_count": 0,
                "usable_financial_count": 0,
                "known_nonpositive_profit_count": 0,
                "missing_financial_count": 0,
                "financial_coverage": 1.0,
            })
            continue
        members = universe_as_of(snapshots, signal_date)
        if members is None:
            continue
        technical = build_can_slim_technical_cross_section(
            signal_date,
            prices,
            dollar_volume,
            nasdaq,
            config,
            members,
            eligibility_close=eligibility_close,
        )
        candidates = set()
        if not technical.empty:
            mask = can_slim_nonfinancial_candidate_mask(technical, config)
            candidates = set(technical.index[mask])
        financial = quarterly_growth_snapshot(
            quarterly,
            signal_date,
            config.maximum_financial_age_days,
        )
        current_ttm = quarterly_profit_ttm_snapshot(
            quarterly,
            signal_date,
            config.maximum_financial_age_days,
        )
        unbounded_financial = quarterly_growth_snapshot(
            quarterly,
            signal_date,
            100_000,
        )
        usable = candidates & set(financial.index)
        nonpositive_tickers = (
            set(current_ttm.index[current_ttm["net_income_ttm"].le(0)])
            if "net_income_ttm" in current_ttm.columns else set()
        )
        known_nonpositive_profit = (candidates - usable) & nonpositive_tickers
        missing = candidates - usable - known_nonpositive_profit
        raw_known = quarterly.loc[
            quarterly["available_date"].le(signal_date)
            & quarterly["metric"].isin(("net_income", "revenue"))
        ]
        raw_tickers = set(
            raw_known["ticker"].dropna().astype(str).str.upper()
        )
        unbounded_tickers = set(unbounded_financial.index)
        for ticker in missing:
            if ticker in unbounded_tickers:
                reason = "stale_growth_snapshot"
            elif ticker in raw_tickers:
                reason = "insufficient_growth_history"
            else:
                reason = "no_raw_pit_financial_facts"
            missing_counts[ticker] += 1
            reason_counts.setdefault(ticker, Counter())[reason] += 1
            first_missing.setdefault(ticker, signal_date)
            last_missing[ticker] = signal_date
        missing_union.update(missing)
        rows.append({
            "signal_date": signal_date.strftime("%Y-%m-%d"),
            "market_regime_on": True,
            "technical_candidate_count": len(candidates),
            "usable_financial_count": len(usable),
            "known_nonpositive_profit_count": len(known_nonpositive_profit),
            "missing_financial_count": len(missing),
            "financial_coverage": (
                (len(usable) + len(known_nonpositive_profit)) / len(candidates)
                if candidates else 1.0
            ),
        })
    risk_on_rows = [row for row in rows if row["market_regime_on"]]
    missing_observations = sum(
        row["missing_financial_count"] for row in risk_on_rows
    )
    candidate_observations = sum(
        row["technical_candidate_count"] for row in risk_on_rows
    )
    known_nonpositive_profit_observations = sum(
        row["known_nonpositive_profit_count"] for row in risk_on_rows
    )
    priorities = []
    annual_forms: dict[str, set[str]] = {}
    quarterly_metrics: dict[str, set[str]] = {}
    quarterly_taxonomies: dict[str, set[str]] = {}
    if len(quarterly):
        observed_quarterly = quarterly.loc[
            quarterly["metric"].isin(("net_income", "revenue"))
        ].dropna(subset=["ticker", "metric"]).copy()
        observed_quarterly["ticker"] = (
            observed_quarterly["ticker"].astype(str).str.upper()
        )
        quarterly_metrics = (
            observed_quarterly.groupby("ticker")["metric"]
            .agg(lambda values: set(map(str, values)))
            .to_dict()
        )
        if "taxonomy" in quarterly.columns:
            observed_taxonomies = quarterly.dropna(
                subset=["ticker", "taxonomy"]
            ).copy()
            observed_taxonomies["ticker"] = (
                observed_taxonomies["ticker"].astype(str).str.upper()
            )
            quarterly_taxonomies = (
                observed_taxonomies.groupby("ticker")["taxonomy"]
                .agg(lambda values: set(map(str, values)))
                .to_dict()
            )
    if annual_fundamentals is not None and len(annual_fundamentals):
        annual = annual_fundamentals.dropna(
            subset=["ticker", "form"]
        ).copy()
        annual["ticker"] = annual["ticker"].astype(str).str.upper()
        annual_forms = (
            annual.groupby("ticker")["form"]
            .agg(lambda values: set(map(str, values)))
            .to_dict()
        )
    for ticker in missing_union:
        reasons = reason_counts[ticker]
        forms = annual_forms.get(ticker, set())
        reporting_profile = _financial_reporting_profile(
            forms,
            reasons,
            quarterly_metrics.get(ticker, set()),
        )
        raw_profile = (raw_cache_profiles or {}).get(ticker, {})
        raw_cache_profile = _effective_raw_financial_profile(
            raw_profile.get("profile", "NOT_CACHED"),
            quarterly_taxonomies.get(ticker, set()),
        )
        priorities.append({
            "ticker": ticker,
            "missing_signal_count": int(missing_counts[ticker]),
            "first_missing_signal_date": first_missing[
                ticker
            ].strftime("%Y-%m-%d"),
            "last_missing_signal_date": last_missing[
                ticker
            ].strftime("%Y-%m-%d"),
            "no_raw_pit_financial_facts_signal_count": int(
                reasons["no_raw_pit_financial_facts"]
            ),
            "insufficient_growth_history_signal_count": int(
                reasons["insufficient_growth_history"]
            ),
            "stale_growth_snapshot_signal_count": int(
                reasons["stale_growth_snapshot"]
            ),
            "reporting_profile": reporting_profile,
            "raw_sec_cache_profile": raw_cache_profile,
            "recommended_data_action": _recommended_financial_data_action(
                reporting_profile,
                raw_cache_profile,
                bool(raw_profile.get("has_supported_revenue_source")),
            ),
            "observed_annual_forms": "|".join(sorted(forms)),
        })
    priorities.sort(
        key=lambda row: (-row["missing_signal_count"], row["ticker"])
    )
    for rank, row in enumerate(priorities, start=1):
        row["priority_rank"] = rank
        row["sec_cache_refresh_tier"] = _sec_cache_refresh_tier(
            row["reporting_profile"],
            row["missing_signal_count"],
            row["raw_sec_cache_profile"],
        )
    cache_refresh_priorities = sorted(
        priorities,
        key=lambda row: (
            row["sec_cache_refresh_tier"],
            -row["missing_signal_count"],
            row["ticker"],
        ),
    )
    for rank, row in enumerate(cache_refresh_priorities, start=1):
        row["cache_refresh_priority_rank"] = rank
    queue_actions = {
        "fetch_priority_rank": {"FETCH_SEC_COMPANYFACTS"},
        "reparse_priority_rank": {
            "REPARSE_OR_ACCEPT_HISTORY_LIMIT",
            "REVIEW_US_GAAP_PARSER",
        },
        "foreign_priority_rank": {"NEEDS_FOREIGN_QUARTERLY_SOURCE"},
        "fdic_priority_rank": {"NEEDS_FDIC_ARCHIVED_QUARTERLY_SOURCE"},
    }
    for column, actions in queue_actions.items():
        eligible = sorted(
            (
                row for row in priorities
                if row["recommended_data_action"] in actions
            ),
            key=lambda row: (-row["missing_signal_count"], row["ticker"]),
        )
        for row in priorities:
            row[column] = None
        for rank, row in enumerate(eligible, start=1):
            row[column] = rank
    profile_counts = Counter(
        row["reporting_profile"] for row in priorities
    )
    return {
        "method": (
            "Use the selector's shared non-financial cross section and exact "
            "minimum price, 50-day median dollar volume, relative volume, "
            "52-week-high, price-history and market-regime rules. Financial "
            "coverage is required only for otherwise eligible candidates on "
            "risk-on signals. A recent, consecutive four-quarter TTM net loss "
            "is a fully observed deterministic failure of the selector's "
            "positive-profit gate and does not require older growth history. "
            "Historical member-price completeness remains "
            "an independent prerequisite."
        ),
        "signal_count": len(rows),
        "risk_on_signal_count": len(risk_on_rows),
        "technical_candidate_observations": candidate_observations,
        "missing_financial_observations": missing_observations,
        "known_nonpositive_profit_observations": (
            known_nonpositive_profit_observations
        ),
        "financial_coverage": (
            (candidate_observations - missing_observations)
            / candidate_observations
            if candidate_observations else 1.0
        ),
        "minimum_signal_financial_coverage": min(
            (row["financial_coverage"] for row in risk_on_rows),
            default=1.0,
        ),
        "missing_financial_symbols": sorted(missing_union),
        "missing_financial_priorities": priorities,
        "missing_financial_reporting_profile_counts": dict(
            sorted(profile_counts.items())
        ),
        "complete": bool(rows) and not missing_union,
        "by_signal": rows,
    }


def fixed_top3_config(
    transaction_cost_bps: float = 10.0,
) -> CanSlimConfig:
    """Return the exact policy frozen after the historical research period."""
    return CanSlimConfig(
        start="2019-01-01",
        end="2026-07-17",
        top_n=3,
        maximum_position_weight=1 / 3,
        minimum_median_dollar_volume=10_000_000.0,
        transaction_cost_bps=transaction_cost_bps,
        signal_frequency="monthly",
        use_quarterly_fundamentals=True,
        price_channel="none",
        selection_mode="growth",
    )


def _annual(result: pd.DataFrame) -> pd.DataFrame:
    annual = (
        (1 + result[["strategy", "benchmark"]])
        .groupby(result.index.year)
        .prod()
        - 1
    )
    annual["excess_vs_nasdaq"] = annual["strategy"] - annual["benchmark"]
    annual.index.name = "year"
    return annual


def annual_cost_capacity_diagnostics(costs: pd.DataFrame) -> dict[str, dict]:
    """Explain annual cost sensitivity without using it to select parameters.

    Break-even cost is linearly interpolated only between actually replayed
    cost levels that bracket zero excess return. It is therefore a diagnostic
    estimate, not an execution-cost forecast or a release criterion.
    """
    diagnostics = {}
    for year, rows in costs.sort_values("cost_bps").groupby("year"):
        rows = rows.sort_values("cost_bps")
        gross = rows.loc[rows["cost_bps"].eq(0.0)]
        gross_excess = (
            float(gross.iloc[0]["excess_vs_nasdaq"])
            if len(gross)
            else None
        )
        break_even = None
        lower_bound = None
        classification = "NOT_BRACKETED"
        if gross_excess is not None and gross_excess <= 0:
            break_even = 0.0
            classification = "NONPOSITIVE_BEFORE_COST"
        else:
            previous = None
            for row in rows.itertuples(index=False):
                if previous is not None:
                    if (
                        previous.excess_vs_nasdaq > 0
                        and row.excess_vs_nasdaq <= 0
                    ):
                        fraction = (
                            previous.excess_vs_nasdaq
                            / (
                                previous.excess_vs_nasdaq
                                - row.excess_vs_nasdaq
                            )
                        )
                        break_even = float(
                            previous.cost_bps
                            + fraction * (row.cost_bps - previous.cost_bps)
                        )
                        classification = "BRACKETED_INTERPOLATION"
                        break
                previous = row
            if break_even is None and rows["excess_vs_nasdaq"].gt(0).all():
                lower_bound = float(rows["cost_bps"].max())
                classification = "ABOVE_HIGHEST_REPLAYED_COST"
        diagnostics[str(int(year))] = {
            "gross_excess_at_0bps": gross_excess,
            "estimated_break_even_one_way_cost_bps": break_even,
            "break_even_cost_lower_bound_bps": lower_bound,
            "classification": classification,
            "observed_excess_by_cost_bps": {
                str(int(row.cost_bps)): float(row.excess_vs_nasdaq)
                for row in rows.itertuples(index=False)
            },
        }
    return diagnostics


def transaction_cost_stress_failure_attribution(
    compounded_alpha_positive: bool,
    annual_breadth_passed: bool,
    incremental_failed_years: list[int],
) -> list[str]:
    """Attribute a combined cost-gate failure without weakening the gate."""
    reasons = []
    if not compounded_alpha_positive:
        reasons.append("COMPOUNDED_ALPHA_NONPOSITIVE_AT_STRESS_COST")
    if not annual_breadth_passed:
        reasons.append(
            "ANNUAL_BREADTH_WORSENED_UNDER_STRESS_COST"
            if incremental_failed_years
            else "ANNUAL_BREADTH_BELOW_THRESHOLD_BEFORE_STRESS_COST"
        )
    if incremental_failed_years:
        reasons.append("ADDITIONAL_FAILED_YEARS_CREATED_BY_STRESS_COST")
    return reasons or ["PASS"]


def trade_liquidity_capacity_diagnostics(
    ledger: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    account_sizes: tuple[float, ...] = (100_000.0, 1_000_000.0),
    liquidity_window: int = 50,
) -> tuple[pd.DataFrame, dict]:
    """Estimate trade participation using only liquidity known before execution.

    Each historical trade is normalized by simulated post-trade NAV and then
    rescaled to hypothetical account sizes. The denominator is the median
    dollar volume over the prior ``liquidity_window`` sessions, matching the
    policy's liquidity lookback while avoiding execution-day hindsight.
    """
    rows = []
    for trade in ledger.itertuples(index=False):
        execution_date = pd.Timestamp(trade.execution_date)
        ticker = str(trade.ticker)
        if ticker not in dollar_volume:
            median_dollar_volume = float("nan")
        else:
            known = dollar_volume.loc[
                dollar_volume.index < execution_date, ticker
            ].dropna().tail(liquidity_window)
            median_dollar_volume = (
                float(known.median()) if len(known) else float("nan")
            )
        nav = float(trade.portfolio_value_after)
        normalized_notional = (
            float(trade.gross_notional) / nav
            if nav > 0
            else float("nan")
        )
        row = {
            "trade_id": int(trade.trade_id),
            "signal_date": pd.Timestamp(trade.signal_date),
            "execution_date": execution_date,
            "ticker": ticker,
            "side": trade.side,
            "normalized_gross_notional": normalized_notional,
            "prior_50d_median_dollar_volume": median_dollar_volume,
        }
        for account_size in account_sizes:
            label = f"{int(account_size):d}"
            row[f"notional_at_{label}_account"] = (
                normalized_notional * account_size
            )
            row[f"participation_at_{label}_account"] = (
                normalized_notional * account_size / median_dollar_volume
                if median_dollar_volume > 0
                else float("nan")
            )
        for participation in (0.01, 0.05):
            label = f"{int(participation * 100)}pct"
            row[f"account_capacity_at_{label}_participation"] = (
                participation * median_dollar_volume / normalized_notional
                if median_dollar_volume > 0 and normalized_notional > 0
                else float("nan")
            )
        rows.append(row)
    detail = pd.DataFrame(rows)
    valid_liquidity = (
        detail["prior_50d_median_dollar_volume"].notna()
        if not detail.empty
        else pd.Series(dtype=bool)
    )
    summary = {
        "method": (
            "Gross trade notional is normalized by simulated portfolio NAV, "
            "then rescaled to hypothetical account sizes and divided by the "
            "prior 50-session median dollar volume. This is a full-day "
            "liquidity proxy, not a closing-auction fill or market-impact "
            "model."
        ),
        "liquidity_window_sessions": liquidity_window,
        "trade_count": int(len(detail)),
        "trades_with_liquidity": int(valid_liquidity.sum()),
        "trades_missing_liquidity": int((~valid_liquidity).sum()),
        "account_size_participation": {},
        "account_capacity": {},
    }
    for account_size in account_sizes:
        label = f"{int(account_size):d}"
        values = detail[
            f"participation_at_{label}_account"
        ].dropna()
        summary["account_size_participation"][label] = {
            "median": float(values.median()) if len(values) else None,
            "p95": float(values.quantile(0.95)) if len(values) else None,
            "maximum": float(values.max()) if len(values) else None,
            "trades_above_1pct": int(values.gt(0.01).sum()),
            "trades_above_5pct": int(values.gt(0.05).sum()),
        }
    for participation in (0.01, 0.05):
        label = f"{int(participation * 100)}pct"
        values = detail[
            f"account_capacity_at_{label}_participation"
        ].dropna()
        summary["account_capacity"][label] = {
            "minimum": float(values.min()) if len(values) else None,
            "p10": float(values.quantile(0.10)) if len(values) else None,
            "median": float(values.median()) if len(values) else None,
        }
    return detail, summary


def stale_snapshot_selection_diagnostics(
    stale_signal_dates: list[str],
    snapshots: dict[pd.Timestamp, set[str]],
    close: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    nasdaq: pd.Series,
    eps: pd.DataFrame,
    config: CanSlimConfig,
    quarterly: pd.DataFrame,
) -> dict:
    """Bound observed selection impact without treating a later snapshot as PIT."""
    rows = []
    snapshot_dates = sorted(snapshots)
    for raw_date in stale_signal_dates:
        signal_date = pd.Timestamp(raw_date)
        prior_dates = [date for date in snapshot_dates if date <= signal_date]
        later_dates = [date for date in snapshot_dates if date > signal_date]
        if not prior_dates or not later_dates:
            rows.append({
                "signal_date": raw_date,
                "bracket_available": False,
            })
            continue
        prior_date = max(prior_dates)
        later_date = min(later_dates)
        prior = snapshots[prior_date]
        later = snapshots[later_date]
        universes = {
            "prior": prior,
            "later": later,
            "union": prior | later,
            "all_observed_price_symbols": None,
        }
        scores = {
            label: score_can_slim_cross_section(
                signal_date,
                close,
                dollar_volume,
                nasdaq,
                eps,
                config,
                universe,
                quarterly,
            )
            for label, universe in universes.items()
        }
        top = {
            label: frame.head(config.top_n).index.astype(str).tolist()
            for label, frame in scores.items()
        }
        later_added = later - prior
        rows.append({
            "signal_date": raw_date,
            "bracket_available": True,
            "prior_snapshot_date": prior_date.strftime("%Y-%m-%d"),
            "later_snapshot_date": later_date.strftime("%Y-%m-%d"),
            "prior_snapshot_age_days": int((signal_date - prior_date).days),
            "later_snapshot_lead_days": int((later_date - signal_date).days),
            "later_added_symbols": len(later_added),
            "later_removed_symbols": len(prior - later),
            "later_added_eligible_symbols": sorted(
                set(scores["later"].index) & later_added
            ),
            "eligible_counts": {
                label: int(len(frame)) for label, frame in scores.items()
            },
            "top_symbols": top,
            "top3_stable_across_prior_later_and_union": (
                top["prior"] == top["later"] == top["union"]
            ),
            "top3_stable_under_all_observed_price_symbols_stress": (
                top["prior"] == top["all_observed_price_symbols"]
            ),
            "eligible_outside_prior_under_all_price_stress": sorted(
                set(scores["all_observed_price_symbols"].index)
                - set(scores["prior"].index)
            ),
        })
    bracketed = [row for row in rows if row["bracket_available"]]
    return {
        "method": (
            "Compare the last knowable snapshot with the next observed "
            "snapshot, their union, and an intentionally over-broad universe "
            "of every symbol with observed prices. The later and all-price "
            "universes are sensitivity bounds, not point-in-time evidence."
        ),
        "signals": rows,
        "all_bracketed_top3_stable": bool(
            bracketed
            and all(
                row["top3_stable_across_prior_later_and_union"]
                and row[
                    "top3_stable_under_all_observed_price_symbols_stress"
                ]
                for row in bracketed
            )
        ),
        "point_in_time_gap_resolved": False,
    }


def run_can_slim_validation(
    config: CanSlimConfig | None = None,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    dict,
    dict,
]:
    """Replay the frozen policy and report research evidence without relabeling it OOS."""
    config = config or fixed_top3_config()
    load_start = (
        pd.Timestamp(config.start) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")
    close, dollar_volume = load_panel(
        CLEANED_PRICE_DATA_DIR, load_start, config.end
    )
    adjusted_close = back_adjust_common_splits(close).sort_index()
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    quarterly = load_quarterly_fundamentals(
        POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
    )
    annual_fundamentals = pd.read_csv(
        POINT_IN_TIME_FUNDAMENTALS_FILE,
        usecols=["ticker", "form"],
    )
    snapshots = load_universe_snapshots()
    universe = lambda date: universe_as_of(snapshots, date)
    historical_readiness = backtest_data_readiness(
        "2021-01-01",
        config.end,
        universe_snapshots=snapshots,
        quarterly_fundamentals=quarterly,
    )
    raw_cache_profiles = {}
    if (SEC_COMPANYFACTS_CACHE_DIR / "manifest.json").exists():
        verify_companyfacts_cache_manifest(SEC_COMPANYFACTS_CACHE_DIR)
        raw_cache_profiles = cached_companyfacts_symbol_payload_profiles(
            SEC_COMPANYFACTS_CACHE_DIR
        )
    candidate_financial_coverage = technical_candidate_financial_coverage(
        close,
        dollar_volume,
        nasdaq,
        quarterly,
        snapshots,
        config,
        annual_fundamentals=annual_fundamentals,
        raw_cache_profiles=raw_cache_profiles,
        adjusted_close=adjusted_close,
    )
    candidate_financial_summary = {
        key: value
        for key, value in candidate_financial_coverage.items()
        if key not in {
            "missing_financial_symbols",
            "missing_financial_priorities",
            "by_signal",
        }
    }
    candidate_financial_summary.update({
        "missing_financial_symbol_count": len(
            candidate_financial_coverage["missing_financial_symbols"]
        ),
        "missing_financial_symbols_top20": (
            [
                row["ticker"]
                for row in candidate_financial_coverage[
                    "missing_financial_priorities"
                ][:20]
            ]
        ),
        "missing_financial_priorities_top20": (
            candidate_financial_coverage[
                "missing_financial_priorities"
            ][:20]
        ),
    })
    historical_checks = {
        **historical_readiness["checks"],
        "signal_technical_candidate_financials_complete": (
            candidate_financial_coverage["complete"]
        ),
    }
    stale_snapshot_diagnostics = stale_snapshot_selection_diagnostics(
        historical_readiness["signal_price_coverage"][
            "stale_signal_snapshot_dates"
        ],
        snapshots,
        close,
        dollar_volume,
        nasdaq,
        eps,
        config,
        quarterly,
    )

    cost_results: dict[float, pd.DataFrame] = {}
    cost_rows: list[dict] = []
    ledger = pd.DataFrame()
    with _memoized_cost_stress_selection():
        for cost_bps in (0.0, 10.0, 30.0, 50.0):
            stressed_config = replace(
                config, transaction_cost_bps=cost_bps
            )
            if cost_bps == 10.0:
                result, ledger = calculate_can_slim_returns_with_ledger(
                    adjusted_close,
                    dollar_volume,
                    nasdaq,
                    eps,
                    stressed_config,
                    universe,
                    quarterly,
                    adjust_splits=False,
                    eligibility_close=close,
                )
            else:
                result = calculate_can_slim_returns(
                    adjusted_close,
                    dollar_volume,
                    nasdaq,
                    eps,
                    stressed_config,
                    universe,
                    quarterly,
                    adjust_splits=False,
                    eligibility_close=close,
                )
            cost_results[cost_bps] = result
            for year, row in _annual(result).loc[2021:].iterrows():
                cost_rows.append({
                    "cost_bps": cost_bps,
                    "year": int(year),
                    "strategy": float(row["strategy"]),
                    "nasdaq": float(row["benchmark"]),
                    "excess_vs_nasdaq": float(
                        row["excess_vs_nasdaq"]
                    ),
                })

    result = cost_results[10.0]
    annual = _annual(result)
    evidence = annual.loc[2021:]
    costs = pd.DataFrame(cost_rows)
    active = result.loc["2021-01-01":, "strategy"] - result.loc[
        "2021-01-01":, "benchmark"
    ]
    uncertainty = moving_block_bootstrap(active)
    live_config = replace(config, end="2099-12-31")
    all_years_win = bool(evidence["excess_vs_nasdaq"].gt(0).all())
    three_x_cost = costs.loc[costs["cost_bps"].eq(30.0)]
    three_x_strategy = float((1 + three_x_cost["strategy"]).prod() - 1)
    three_x_nasdaq = float((1 + three_x_cost["nasdaq"]).prod() - 1)
    # Three times the assumed one-way cost is the actionable capacity stress:
    # require positive compounded alpha and at least 75% winning years.  The
    # 50 bps run remains a deliberately extreme diagnostic, not the base case.
    cost_stress_passed = bool(
        three_x_strategy > three_x_nasdaq
        and three_x_cost["excess_vs_nasdaq"].gt(0).mean() >= 0.75
    )
    baseline_cost = costs.loc[costs["cost_bps"].eq(10.0)]
    baseline_failed_years = sorted(
        baseline_cost.loc[
            baseline_cost["excess_vs_nasdaq"].le(0), "year"
        ].astype(int).tolist()
    )
    stress_failed_years = sorted(
        three_x_cost.loc[
            three_x_cost["excess_vs_nasdaq"].le(0), "year"
        ].astype(int).tolist()
    )
    incremental_stress_failed_years = sorted(
        set(stress_failed_years) - set(baseline_failed_years)
    )
    cost_stress_compounded_alpha_positive = bool(
        three_x_strategy > three_x_nasdaq
    )
    cost_stress_winning_year_fraction = float(
        three_x_cost["excess_vs_nasdaq"].gt(0).mean()
    )
    baseline_winning_year_fraction = float(
        baseline_cost["excess_vs_nasdaq"].gt(0).mean()
    )
    cost_stress_annual_breadth_passed = bool(
        cost_stress_winning_year_fraction >= 0.75
    )
    cost_stress_no_incremental_failed_years = bool(
        not incremental_stress_failed_years
    )
    incremental_cost_robustness_passed = bool(
        cost_stress_compounded_alpha_positive
        and cost_stress_no_incremental_failed_years
    )
    cost_stress_failure_reasons = (
        transaction_cost_stress_failure_attribution(
            cost_stress_compounded_alpha_positive,
            cost_stress_annual_breadth_passed,
            incremental_stress_failed_years,
        )
    )
    annual_turnover = (
        result.loc["2021-01-01":]
        .groupby(result.loc["2021-01-01":].index.year)["turnover"]
        .sum()
    )
    annual_ledger = ledger.copy()
    annual_ledger["year"] = pd.to_datetime(
        annual_ledger["execution_date"]
    ).dt.year
    annual_transaction_cost = annual_ledger.groupby("year")[
        "transaction_cost"
    ].sum()
    annual_gross_notional = annual_ledger.groupby("year")[
        "gross_notional"
    ].sum()
    cost_capacity = annual_cost_capacity_diagnostics(costs)
    liquidity_detail, liquidity_capacity = (
        trade_liquidity_capacity_diagnostics(ledger, dollar_volume)
    )
    unresolved_terminal_symbols = {
        str(item["ticker"]).upper()
        for item in historical_readiness[
            "unresolved_terminal_return_histories"
        ]
    }
    traded_symbols = set(
        ledger["ticker"].dropna().astype(str).str.upper()
    )
    unresolved_traded_symbols = sorted(
        unresolved_terminal_symbols & traded_symbols
    )
    observable_gap_details = historical_readiness[
        "signal_price_coverage"
    ]["missing_with_pit_financial_data_details"]
    observable_gap_summary = []
    for ticker in sorted({row["ticker"] for row in observable_gap_details}):
        ticker_rows = [
            row for row in observable_gap_details
            if row["ticker"] == ticker
        ]
        failed_thresholds = sorted({
            threshold
            for row in ticker_rows
            for threshold, passed in (
                ("positive_profit", row["passes_positive_profit"]),
                ("profit_growth", row["passes_profit_growth"]),
                ("revenue_growth", row["passes_revenue_growth"]),
            )
            if not passed
        })
        observable_gap_summary.append({
            "ticker": ticker,
            "affected_signal_count": len(ticker_rows),
            "failed_thresholds_observed": failed_thresholds,
            "classifications": sorted({
                row["final_observable_classification"]
                for row in ticker_rows
            }),
        })
    summary = {
        "model_version": MODEL_VERSION,
        "policy_status": "FROZEN_SHADOW",
        "release_status": "BLOCKED",
        "release_reason": (
            "Historical performance replay completed, but historical "
            "universe/price completeness has not passed and genuine forward "
            f"evidence starts after {POLICY_FROZEN_AT}."
        ),
        "historical_data_status": (
            "COMPLETE"
            if historical_readiness["complete"]
            else "INCOMPLETE_STRESSED"
        ),
        "historical_data_checks": historical_checks,
        "historical_quarterly_value_conflict_count": len(
            historical_readiness[
                "historical_quarterly_value_conflicts"
            ]
        ),
        "historical_quarterly_conflict_order_sensitivity": (
            historical_readiness[
                "historical_quarterly_conflict_order_sensitivity"
            ]
        ),
        "historical_technical_candidate_financial_coverage": (
            candidate_financial_summary
        ),
        "historical_temporal_security_type_filter": (
            historical_readiness["temporal_security_type_filter"]
        ),
        "historical_benchmark_calendar": historical_readiness[
            "benchmark_calendar"
        ],
        "historical_missing_price_symbols": len(
            historical_readiness["signal_price_coverage"][
                "missing_price_symbols"
            ]
        ),
        "historical_minimum_usable_pit_financial_growth_coverage": (
            historical_readiness["signal_price_coverage"][
                "minimum_usable_pit_financial_growth_coverage"
            ]
        ),
        "historical_missing_usable_pit_financial_growth_symbols": len(
            historical_readiness["signal_price_coverage"][
                "missing_usable_pit_financial_growth_symbols"
            ]
        ),
        "historical_missing_no_raw_pit_financial_facts_symbols": len(
            historical_readiness["signal_price_coverage"][
                "missing_no_raw_pit_financial_facts_symbols"
            ]
        ),
        "historical_missing_insufficient_financial_history_symbols": len(
            historical_readiness["signal_price_coverage"][
                "missing_insufficient_financial_history_symbols"
            ]
        ),
        "historical_missing_stale_financial_growth_symbols": len(
            historical_readiness["signal_price_coverage"][
                "missing_stale_financial_growth_symbols"
            ]
        ),
        "historical_financial_gap_observations": {
            "total": historical_readiness["signal_price_coverage"][
                "missing_usable_pit_financial_growth_observations"
            ],
            "no_raw_pit_facts": historical_readiness[
                "signal_price_coverage"
            ]["missing_no_raw_pit_financial_facts_observations"],
            "insufficient_history": historical_readiness[
                "signal_price_coverage"
            ]["missing_insufficient_financial_history_observations"],
            "stale_growth": historical_readiness[
                "signal_price_coverage"
            ]["missing_stale_financial_growth_observations"],
            "missing_raw_metric": {
                classification: sum(counts.values())
                for classification, counts in historical_readiness[
                    "signal_price_coverage"
                ].get("missing_financial_metric_gap_counts", {}).items()
            },
        },
        "historical_missing_price_symbols_with_pit_financial_data": len(
            historical_readiness["signal_price_coverage"][
                "missing_with_pit_financial_data_symbols"
            ]
        ),
        "historical_missing_price_symbols_without_pit_financial_data": len(
            historical_readiness["signal_price_coverage"][
                "missing_without_pit_financial_data_symbols"
            ]
        ),
        "historical_missing_price_symbols_never_with_pit_financial_data": len(
            historical_readiness["signal_price_coverage"][
                "missing_never_with_pit_financial_data_symbols"
            ]
        ),
        "historical_missing_price_symbols_with_mixed_pit_financial_coverage": (
            historical_readiness["signal_price_coverage"][
                "missing_with_and_without_pit_financial_data_symbols"
            ]
        ),
        "historical_pit_gap_priority_method": (
            historical_readiness["signal_price_coverage"][
                "pit_gap_priority_method"
            ]
        ),
        "historical_pit_gap_priority_top20": (
            historical_readiness["signal_price_coverage"][
                "pit_gap_priorities"
            ][:20]
        ),
        "historical_pit_gap_recovery_priority_method": (
            historical_readiness["signal_price_coverage"][
                "pit_gap_recovery_priority_method"
            ]
        ),
        "historical_pit_gap_recovery_top20": sorted(
            historical_readiness["signal_price_coverage"][
                "pit_gap_priorities"
            ],
            key=lambda row: row["recovery_priority_rank"],
        )[:20],
        "historical_maximum_signal_snapshot_age_days": (
            historical_readiness["signal_price_coverage"][
                "maximum_observed_signal_snapshot_age_days"
            ]
        ),
        "historical_allowed_signal_snapshot_age_days": (
            historical_readiness["signal_price_coverage"][
                "maximum_signal_snapshot_age_days"
            ]
        ),
        "historical_stale_signal_snapshot_dates": (
            historical_readiness["signal_price_coverage"][
                "stale_signal_snapshot_dates"
            ]
        ),
        "historical_stale_snapshot_selection_diagnostics": (
            stale_snapshot_diagnostics
        ),
        "historical_missing_price_symbols_passing_financial_screen": (
            historical_readiness["signal_price_coverage"][
                "missing_passing_financial_screen_symbols"
            ]
        ),
        "historical_confirmed_insufficient_listing_history_symbols": (
            historical_readiness["signal_price_coverage"][
                "confirmed_insufficient_listing_history_symbols"
            ]
        ),
        "historical_unresolved_observable_potential_competitors": (
            historical_readiness["signal_price_coverage"][
                "unresolved_observable_potential_competitor_symbols"
            ]
        ),
        "historical_observable_missing_price_detail_rows": len(
            observable_gap_details
        ),
        "historical_observable_missing_price_by_ticker": (
            observable_gap_summary
        ),
        "historical_financial_screen_interpretation": (
            historical_readiness["signal_price_coverage"][
                "financial_screen_interpretation"
            ]
        ),
        "historical_unresolved_terminal_returns": historical_readiness[
            "unresolved_terminal_returns"
        ],
        "selected_position_terminal_returns_complete": (
            not unresolved_traded_symbols
        ),
        "unresolved_terminal_returns_affecting_traded_symbols": (
            unresolved_traded_symbols
        ),
        "rules": (
            "Public CAN SLIM-inspired quarterly growth and leadership; at most "
            "three qualifying Nasdaq stocks, equal weighted when full; monthly "
            "close signal and next-session close execution."
        ),
        "configurations_tested_in_top_n_neighborhood": 4,
        "historical_research_period": "2021-01-01 through 2026-07-17",
        "policy_frozen_at": POLICY_FROZEN_AT,
        "forward_evidence_start": POLICY_FROZEN_AT,
        "parameter_update_frequency": "frozen",
        "adaptive_framework_status": "RESEARCH_ONLY",
        "adaptive_parameter_family": {
            "top_n": [3, 5, 10],
            "minimum_median_dollar_volume": [2_000_000, 10_000_000],
            "position_weight_rule": "1 / top_n",
            "selection_data_cutoff": "previous calendar year end",
            "promotion_rule": (
                "must beat the frozen policy in chronological walk-forward "
                "before it can update live parameters"
            ),
        },
        "signal_frequency": config.signal_frequency,
        "uses_quarterly_fundamentals": True,
        "uses_adaptive_channel": False,
        "historical_evidence_class": "RETROSPECTIVE_IN_SAMPLE",
        "historical_evidence_interpretation": (
            "The frozen policy was chosen after reviewing this period. These "
            "returns, annual win counts, and bootstrap statistics are "
            "retrospective sensitivity evidence, not out-of-sample proof."
        ),
        "bootstrap_evidence_class": "RETROSPECTIVE_IN_SAMPLE_INFORMATION_ONLY",
        "historical_years": int(len(evidence)),
        "wins_vs_nasdaq": int(evidence["excess_vs_nasdaq"].gt(0).sum()),
        "passed_every_historical_year": all_years_win,
        "minimum_historical_excess": float(
            evidence["excess_vs_nasdaq"].min()
        ),
        "median_historical_excess": float(
            evidence["excess_vs_nasdaq"].median()
        ),
        "transaction_cost_stress_passed": cost_stress_passed,
        "transaction_cost_stress_definition": (
            "30 bps one-way (3x assumed): compounded return above Nasdaq and "
            "at least 75% winning calendar years"
        ),
        "transaction_cost_stress_interpretation": (
            "The frozen combined gate is intentionally unchanged. Its "
            "winning-year requirement measures retrospective breadth, while "
            "the separate incremental diagnostics below isolate whether the "
            "increase from 10 to 30 bps creates additional losing years."
        ),
        "transaction_cost_stress_diagnostics": {
            "combined_gate_passed": cost_stress_passed,
            "combined_gate_failure_attribution": (
                cost_stress_failure_reasons
            ),
            "compounded_alpha_positive_at_30bps": (
                cost_stress_compounded_alpha_positive
            ),
            "incremental_cost_robustness_passed": (
                incremental_cost_robustness_passed
            ),
            "annual_breadth_passed_at_30bps": (
                cost_stress_annual_breadth_passed
            ),
            "winning_year_fraction_at_10bps": (
                baseline_winning_year_fraction
            ),
            "winning_year_fraction_at_30bps": (
                cost_stress_winning_year_fraction
            ),
            "required_winning_year_fraction": 0.75,
            "no_incremental_failed_years_at_30bps": (
                cost_stress_no_incremental_failed_years
            ),
            "baseline_10bps_failed_years": baseline_failed_years,
            "stress_30bps_failed_years": stress_failed_years,
            "incremental_failed_years_due_to_30bps": (
                incremental_stress_failed_years
            ),
            "annual_excess_at_30bps": {
                str(int(row["year"])): float(row["excess_vs_nasdaq"])
                for _, row in three_x_cost.iterrows()
            },
            "annual_turnover_at_10bps": {
                str(int(year)): float(value)
                for year, value in annual_turnover.items()
            },
            "annual_transaction_cost_simulated_dollars_at_10bps": {
                str(int(year)): float(value)
                for year, value in annual_transaction_cost.items()
                if year >= 2021
            },
            "annual_gross_notional_simulated_dollars_at_10bps": {
                str(int(year)): float(value)
                for year, value in annual_gross_notional.items()
                if year >= 2021
            },
            "annual_cost_capacity": cost_capacity,
            "break_even_method": (
                "Linear interpolation between replayed 0/10/30/50 bps "
                "one-way cost levels. Diagnostic only; not used for model "
                "selection or release."
            ),
        },
        "trade_liquidity_capacity": liquidity_capacity,
        "three_x_cost_strategy_return": three_x_strategy,
        "three_x_cost_nasdaq_return": three_x_nasdaq,
        "cost_stress_wins": {
            str(int(cost)): int(group["excess_vs_nasdaq"].gt(0).sum())
            for cost, group in costs.groupby("cost_bps")
        },
        **uncertainty,
        "current_shadow_config_ids": [0],
        "current_shadow_configs": [asdict(live_config)],
        "model_snapshots": [{
            "effective_start": POLICY_FROZEN_AT,
            "effective_end": "9999-12-31",
            "training_end": "2026-07-17",
            "config_ids": [0],
            "configs": [asdict(live_config)],
        }],
        "input_fingerprints": can_slim_input_fingerprints(),
    }
    return (
        result,
        annual,
        costs,
        ledger,
        liquidity_detail,
        summary,
        candidate_financial_coverage,
    )


def write_can_slim_validation_outputs(
    validation: tuple,
    output: Path = Path("output"),
) -> None:
    """Persist one validation result as an all-or-nothing artifact set."""
    (
        result,
        annual,
        costs,
        ledger,
        liquidity_detail,
        summary,
        candidate_financial_coverage,
    ) = validation
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    artifacts = [
        (
            output / "can_slim_fixed_top3_backtest.csv",
            lambda path: result.to_csv(path),
        ),
        (
            output / "can_slim_fixed_top3_trade_ledger.csv",
            lambda path: ledger.to_csv(path, index=False),
        ),
        (
            output / "can_slim_fixed_top3_annual.csv",
            lambda path: annual.to_csv(path),
        ),
        (
            output / "can_slim_fixed_top3_cost_stress.csv",
            lambda path: costs.to_csv(path, index=False),
        ),
        (
            output / "can_slim_fixed_top3_liquidity_capacity.csv",
            lambda path: liquidity_detail.to_csv(path, index=False),
        ),
        (
            output / "can_slim_fixed_top3_summary.json",
            lambda path: path.write_text(
                json.dumps(summary, indent=2), encoding="utf-8"
            ),
        ),
        (
            output
            / "can_slim_technical_candidate_financial_coverage.json",
            lambda path: path.write_text(
                json.dumps(candidate_financial_coverage, indent=2),
                encoding="utf-8",
            ),
        ),
        (
            output
            / "can_slim_technical_candidate_financial_priorities.csv",
            lambda path: pd.DataFrame(
                candidate_financial_coverage[
                    "missing_financial_priorities"
                ]
            ).to_csv(path, index=False),
        ),
    ]
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path | None] = {}
    replaced: list[Path] = []
    try:
        for target, writer in artifacts:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            staged[target] = temporary
            writer(temporary)
        manifest_payload = build_validation_artifact_manifest({
            target.name: staged[target] for target, _writer in artifacts
        })
        manifest_target = output / VALIDATION_ARTIFACT_MANIFEST_NAME
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{manifest_target.name}.",
            suffix=".tmp",
            dir=manifest_target.parent,
        )
        os.close(descriptor)
        manifest_temporary = Path(temporary_name)
        staged[manifest_target] = manifest_temporary
        manifest_temporary.write_text(
            json.dumps(manifest_payload, indent=2) + "\n",
            encoding="utf-8",
        )
        artifacts.append((manifest_target, lambda _path: None))
        for target, _writer in artifacts:
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
        for target, _writer in artifacts:
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
                "Validation artifact write failed and rollback was "
                "incomplete: " + "; ".join(rollback_errors)
            )
        raise
    finally:
        for path in (
            *staged.values(),
            *(path for path in backups.values() if path),
        ):
            Path(path).unlink(missing_ok=True)


def main() -> None:
    validation = run_can_slim_validation()
    write_can_slim_validation_outputs(validation)
    annual = validation[1]
    summary = validation[5]
    print(annual.loc[2021:].to_string(
        float_format=lambda value: f"{value:.2%}"
    ))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
