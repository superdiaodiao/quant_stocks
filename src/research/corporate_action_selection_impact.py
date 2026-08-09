"""Measure how corporate-action price handling changes frozen Top 3 selections."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
)
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.research.can_slim import (
    calculate_can_slim_returns,
    score_can_slim_cross_section,
)
from src.research.can_slim_robustness import annual_scenario_metrics
from src.research.can_slim_validation import fixed_top3_config
from src.research.data_fingerprint import can_slim_input_fingerprints
from src.research.data_quality import (
    apply_confirmed_price_adjustments,
    back_adjust_common_splits,
    restore_contemporaneous_prices,
)
from src.research.panel_data import load_panel
from src.research.universe_history import (
    load_universe_snapshots,
    universe_as_of,
)
from src.strategy.common import market_regime_is_on, scheduled_signal_dates


RESEARCH_STATUS = "RESEARCH_ONLY"
VALIDATION_PATH = Path(
    "output/can_slim_all_corporate_action_validation.csv"
)


def unresolved_event_selection_relevance(
    validation: pd.DataFrame,
    score_details: pd.DataFrame,
    trading_index: pd.DatetimeIndex,
    lookback_sessions: int = 253,
) -> dict:
    """Measure whether unresolved events enter a selected signal's lookback."""
    selected_tickers = set(
        score_details.loc[
            score_details["legacy_selected"].fillna(False)
            | score_details["confirmed_selected"].fillna(False),
            "ticker",
        ].astype(str)
    )
    unresolved = validation.loc[
        validation["validation_status"].isin([
            "UNRESOLVED_PRICE_JUMP", "SOURCE_FETCH_FAILED"
        ])
        & validation["ticker"].astype(str).isin(selected_tickers)
    ].copy()
    unresolved["split_date"] = pd.to_datetime(
        unresolved["split_date"], errors="raise"
    ).dt.normalize()
    rows = []
    for event in unresolved.itertuples(index=False):
        selected = score_details.loc[
            score_details["ticker"].astype(str).eq(str(event.ticker))
            & (
                score_details["legacy_selected"].fillna(False)
                | score_details["confirmed_selected"].fillna(False)
            )
        ].copy()
        selected["signal_date"] = pd.to_datetime(
            selected["signal_date"], errors="raise"
        )
        selected = selected.loc[selected["signal_date"] >= event.split_date]
        distances = []
        for signal_date in selected["signal_date"]:
            distance = int(
                (
                    (trading_index > event.split_date)
                    & (trading_index <= signal_date)
                ).sum()
            )
            distances.append({
                "signal_date": signal_date.strftime("%Y-%m-%d"),
                "trading_sessions_after_event": distance,
                "inside_price_lookback": distance <= lookback_sessions,
            })
        rows.append({
            "ticker": str(event.ticker),
            "event_date": event.split_date.strftime("%Y-%m-%d"),
            "validation_status": event.validation_status,
            "selected_signals_after_event": distances,
            "affects_selected_price_lookback": any(
                item["inside_price_lookback"] for item in distances
            ),
        })
    return {
        "price_lookback_sessions": lookback_sessions,
        "unresolved_events_on_selected_tickers": int(len(rows)),
        "events": rows,
        "events_affecting_selected_price_lookback": int(sum(
            row["affects_selected_price_lookback"] for row in rows
        )),
        "interpretation": (
            "An unresolved event on a ticker is selection-relevant only when "
            "a selected signal occurs within the policy's 253-session price "
            "lookback. This does not resolve the underlying event."
        ),
    }


def compare_scored_cross_sections(
    signal_date: pd.Timestamp,
    legacy: pd.DataFrame,
    confirmed: pd.DataFrame,
    top_n: int,
    risk_on: bool,
    action_statuses: dict[str, str] | None = None,
) -> tuple[dict, pd.DataFrame]:
    """Compare two score tables without treating rank shifts as direct events."""
    action_statuses = action_statuses or {}
    legacy_top = legacy.head(top_n).index.astype(str).tolist()
    confirmed_top = confirmed.head(top_n).index.astype(str).tolist()
    executed_legacy = legacy_top if risk_on else []
    executed_confirmed = confirmed_top if risk_on else []
    legacy_candidates = set(legacy.index.astype(str))
    confirmed_candidates = set(confirmed.index.astype(str))
    direct_changes = legacy_candidates ^ confirmed_candidates
    selected_changes = set(legacy_top) ^ set(confirmed_top)
    action_direct_changes = {
        ticker for ticker in direct_changes if ticker in action_statuses
    }
    unresolved_direct_changes = {
        ticker
        for ticker in action_direct_changes
        if (
            "UNRESOLVED_PRICE_JUMP" in action_statuses[ticker]
            or "SOURCE_FETCH_FAILED" in action_statuses[ticker]
        )
    }
    indirect_selected_changes = {
        ticker for ticker in selected_changes if ticker not in action_statuses
    }
    summary = {
        "signal_date": pd.Timestamp(signal_date),
        "risk_on": bool(risk_on),
        "legacy_candidate_count": len(legacy_candidates),
        "confirmed_candidate_count": len(confirmed_candidates),
        "legacy_top3": "|".join(legacy_top),
        "confirmed_top3": "|".join(confirmed_top),
        "raw_top3_changed": legacy_top != confirmed_top,
        "executed_top3_changed": executed_legacy != executed_confirmed,
        "direct_candidate_change_count": len(direct_changes),
        "action_direct_candidate_changes": "|".join(
            sorted(action_direct_changes)
        ),
        "unresolved_direct_candidate_changes": "|".join(
            sorted(unresolved_direct_changes)
        ),
        "indirect_selected_changes": "|".join(
            sorted(indirect_selected_changes)
        ),
        "has_indirect_selection_effect": bool(
            selected_changes and indirect_selected_changes and direct_changes
        ),
        "has_unresolved_indirect_selection_effect": bool(
            selected_changes
            and indirect_selected_changes
            and unresolved_direct_changes
        ),
    }

    legacy_rank = legacy["score"].rank(ascending=False, method="min")
    confirmed_rank = confirmed["score"].rank(ascending=False, method="min")
    relevant = (
        set(legacy_top)
        | set(confirmed_top)
        | direct_changes
        | set(
            legacy.index[
                legacy["score"].reindex(legacy.index).sub(
                    confirmed["score"].reindex(legacy.index)
                ).abs().gt(1e-12)
            ].astype(str)
        )
    )
    rows = []
    for ticker in sorted(relevant):
        legacy_score = (
            float(legacy.at[ticker, "score"])
            if ticker in legacy.index else np.nan
        )
        confirmed_score = (
            float(confirmed.at[ticker, "score"])
            if ticker in confirmed.index else np.nan
        )
        rows.append({
            "signal_date": pd.Timestamp(signal_date),
            "ticker": ticker,
            "legacy_candidate": ticker in legacy_candidates,
            "confirmed_candidate": ticker in confirmed_candidates,
            "legacy_selected": ticker in legacy_top,
            "confirmed_selected": ticker in confirmed_top,
            "legacy_score": legacy_score,
            "confirmed_score": confirmed_score,
            "score_delta": confirmed_score - legacy_score,
            "legacy_rank": (
                float(legacy_rank.at[ticker])
                if ticker in legacy_rank.index else np.nan
            ),
            "confirmed_rank": (
                float(confirmed_rank.at[ticker])
                if ticker in confirmed_rank.index else np.nan
            ),
            "candidate_membership_changed": ticker in direct_changes,
            "has_price_event": ticker in action_statuses,
            "has_unresolved_price_event": ticker in unresolved_direct_changes,
            "price_event_statuses": action_statuses.get(ticker, ""),
        })
    return summary, pd.DataFrame(rows)


def run_selection_impact_diagnostic() -> tuple[
    pd.DataFrame, pd.DataFrame, pd.DataFrame, dict
]:
    """Compare legacy heuristic and sourced corporate-action paths by signal."""
    if not VALIDATION_PATH.exists():
        raise FileNotFoundError(
            "Run corporate_action_validation before the selection-impact audit"
        )
    config = fixed_top3_config()
    load_start = (
        pd.Timestamp(config.start) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")
    close, dollar_volume = load_panel(
        CLEANED_PRICE_DATA_DIR, load_start, config.end
    )
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    quarterly = load_quarterly_fundamentals(
        POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
    )
    snapshots = load_universe_snapshots()
    universe = lambda date: universe_as_of(snapshots, date)
    validation = pd.read_csv(VALIDATION_PATH)
    confirmed_actions = validation.loc[
        validation["validation_status"].eq("CONFIRMED")
    ].rename(columns={
        "split_date": "effective_date",
        "confirmed_adjustment_factor": "adjustment_factor",
    })
    legacy_close = back_adjust_common_splits(close)
    confirmed_close = apply_confirmed_price_adjustments(
        close,
        confirmed_actions[[
            "ticker", "effective_date", "adjustment_factor"
        ]],
    )
    contemporaneous_close = restore_contemporaneous_prices(close, validation)
    statuses = (
        validation.groupby("ticker")["validation_status"]
        .agg(lambda values: "|".join(sorted(set(values.astype(str)))))
        .to_dict()
    )

    signal_rows = []
    detail_frames = []
    for signal_date in scheduled_signal_dates(
        close.index, config.start, config.end, config.signal_frequency
    ):
        symbols = universe(signal_date)
        legacy_scores = score_can_slim_cross_section(
            signal_date,
            legacy_close,
            dollar_volume,
            nasdaq,
            eps,
            config,
            symbols,
            quarterly,
        )
        confirmed_scores = score_can_slim_cross_section(
            signal_date,
            confirmed_close,
            dollar_volume,
            nasdaq,
            eps,
            config,
            symbols,
            quarterly,
            eligibility_close=contemporaneous_close,
        )
        signal_summary, details = compare_scored_cross_sections(
            signal_date,
            legacy_scores,
            confirmed_scores,
            config.top_n,
            market_regime_is_on(
                signal_date, nasdaq, config.market_ma_days
            ),
            statuses,
        )
        signal_rows.append(signal_summary)
        if len(details):
            detail_frames.append(details)

    signals = pd.DataFrame(signal_rows)
    details = (
        pd.concat(detail_frames, ignore_index=True)
        if detail_frames else pd.DataFrame()
    )
    legacy_result = calculate_can_slim_returns(
        close, dollar_volume, nasdaq, eps, config, universe, quarterly
    )
    confirmed_result = calculate_can_slim_returns(
        confirmed_close,
        dollar_volume,
        nasdaq,
        eps,
        config,
        universe,
        quarterly,
        adjust_splits=False,
        eligibility_close=contemporaneous_close,
    )
    legacy_annual = annual_scenario_metrics(legacy_result).set_index("year")
    confirmed_annual = annual_scenario_metrics(
        confirmed_result
    ).set_index("year")
    annual = legacy_annual.add_prefix("legacy_").join(
        confirmed_annual.add_prefix("confirmed_")
    )
    annual["strategy_return_delta"] = (
        annual["confirmed_strategy"] - annual["legacy_strategy"]
    )
    annual = annual.reset_index()
    changed = signals.loc[signals["raw_top3_changed"]]
    unresolved_relevance = unresolved_event_selection_relevance(
        validation, details, close.index
    )
    summary = {
        "status": RESEARCH_STATUS,
        "formal_model_changed": False,
        "signal_count": int(len(signals)),
        "raw_top3_changed_signals": int(
            signals["raw_top3_changed"].sum()
        ),
        "executed_top3_changed_signals": int(
            signals["executed_top3_changed"].sum()
        ),
        "signals_with_direct_candidate_changes": int(
            signals["direct_candidate_change_count"].gt(0).sum()
        ),
        "signals_with_indirect_selection_effect": int(
            signals["has_indirect_selection_effect"].sum()
        ),
        "signals_with_unresolved_indirect_selection_effect": int(
            signals["has_unresolved_indirect_selection_effect"].sum()
        ),
        "changed_signal_dates": [
            date.strftime("%Y-%m-%d")
            for date in pd.to_datetime(changed["signal_date"])
        ],
        "maximum_absolute_annual_return_delta": float(
            annual["strategy_return_delta"].abs().max()
        ),
        "unresolved_event_selection_relevance": unresolved_relevance,
        "interpretation": (
            "Direct candidate changes are separated by validation status. An "
            "indirect selection effect means a candidate-set change altered "
            "percentile scores or Top 3 membership of other tickers; the "
            "unresolved metric counts only unresolved or source-fetch-failed "
            "price events."
        ),
        "input_fingerprints": can_slim_input_fingerprints(),
    }
    return signals, details, annual, summary


def main() -> None:
    signals, details, annual, summary = run_selection_impact_diagnostic()
    output = Path("output")
    signals.to_csv(
        output / "can_slim_corporate_action_signal_impact.csv", index=False
    )
    details.to_csv(
        output / "can_slim_corporate_action_score_impact.csv", index=False
    )
    annual.to_csv(
        output / "can_slim_corporate_action_return_impact.csv", index=False
    )
    (
        output / "can_slim_corporate_action_selection_impact_summary.json"
    ).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
