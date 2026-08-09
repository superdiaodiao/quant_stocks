import pandas as pd

from src.research.corporate_action_selection_impact import (
    compare_scored_cross_sections,
    unresolved_event_selection_relevance,
)


def test_compare_scored_cross_sections_distinguishes_indirect_rank_effect():
    legacy = pd.DataFrame(
        {"score": [0.90, 0.80, 0.70, 0.60]},
        index=["A", "B", "C", "FLAGGED"],
    )
    confirmed = pd.DataFrame(
        {"score": [0.90, 0.80, 0.75]},
        index=["A", "B", "D"],
    )

    summary, details = compare_scored_cross_sections(
        pd.Timestamp("2025-09-30"),
        legacy,
        confirmed,
        top_n=3,
        risk_on=True,
        action_statuses={"FLAGGED": "UNRESOLVED_PRICE_JUMP"},
    )

    assert summary["raw_top3_changed"]
    assert summary["executed_top3_changed"]
    assert summary["action_direct_candidate_changes"] == "FLAGGED"
    assert summary["unresolved_direct_candidate_changes"] == "FLAGGED"
    assert summary["indirect_selected_changes"] == "C|D"
    assert summary["has_indirect_selection_effect"]
    assert summary["has_unresolved_indirect_selection_effect"]
    flagged = details.set_index("ticker").loc["FLAGGED"]
    assert bool(flagged["candidate_membership_changed"])
    assert bool(flagged["has_price_event"])
    assert bool(flagged["has_unresolved_price_event"])


def test_risk_off_rank_change_is_not_an_executed_change():
    legacy = pd.DataFrame({"score": [0.9]}, index=["A"])
    confirmed = pd.DataFrame({"score": [0.9]}, index=["B"])

    summary, _ = compare_scored_cross_sections(
        pd.Timestamp("2022-01-31"),
        legacy,
        confirmed,
        top_n=1,
        risk_on=False,
    )

    assert summary["raw_top3_changed"]
    assert not summary["executed_top3_changed"]


def test_old_unresolved_event_outside_selected_price_lookback():
    validation = pd.DataFrame([{
        "ticker": "A",
        "split_date": "2020-01-02",
        "validation_status": "UNRESOLVED_PRICE_JUMP",
    }])
    details = pd.DataFrame([{
        "signal_date": "2021-06-30",
        "ticker": "A",
        "legacy_selected": True,
        "confirmed_selected": True,
    }])
    trading_index = pd.bdate_range("2020-01-02", "2021-06-30")

    result = unresolved_event_selection_relevance(
        validation, details, trading_index, lookback_sessions=253
    )

    event = result["events"][0]
    assert event["selected_signals_after_event"][0][
        "trading_sessions_after_event"
    ] > 253
    assert event["affects_selected_price_lookback"] is False
    assert result["events_affecting_selected_price_lookback"] == 0
