import pandas as pd
import pytest

from src.research.corporate_action_validation import (
    apply_reviewed_market_moves,
    load_reviewed_market_moves,
    validate_candidate_events,
)


def _candidate(ticker="ABC", date="2025-01-02", factor=0.5):
    return pd.DataFrame([{
        "ticker": ticker,
        "split_date": date,
        "raw_price_ratio": factor,
        "matched_factor": factor,
    }])


def test_confirmed_split_uses_sourced_factor():
    splits = pd.DataFrame([{
        "ticker": "ABC",
        "effective_date": pd.Timestamp("2025-01-02"),
        "adjustment_factor": 0.25,
        "source": "secondary://split",
    }])

    result = validate_candidate_events(
        _candidate(),
        splits,
        pd.DataFrame(),
        {"ABC": pd.Series(dtype=float)},
    )

    assert result.iloc[0]["validation_status"] == "CONFIRMED"
    assert result.iloc[0]["confirmed_action_type"] == "SPLIT"
    assert result.iloc[0]["confirmed_adjustment_factor"] == 0.25


def test_cash_distribution_uses_prior_close_for_adjustment():
    cash = pd.DataFrame([{
        "ticker": "VISN",
        "effective_date": pd.Timestamp("2026-04-28"),
        "cash_amount": 10.0,
        "source": "official://cash",
    }])
    close = pd.Series(
        [19.0, 19.53, 9.90],
        index=pd.to_datetime(["2026-04-24", "2026-04-27", "2026-04-28"]),
    )

    result = validate_candidate_events(
        _candidate("VISN", "2026-04-28"),
        pd.DataFrame(),
        cash,
        {"VISN": close},
    )

    assert result.iloc[0]["confirmed_action_type"] == "CASH_DISTRIBUTION"
    assert result.iloc[0]["confirmed_adjustment_factor"] == pytest.approx(
        (19.53 - 10.0) / 19.53
    )


def test_unmatched_jump_remains_unresolved():
    result = validate_candidate_events(
        _candidate(),
        pd.DataFrame(),
        pd.DataFrame(),
        {"ABC": pd.Series(dtype=float)},
    )

    assert result.iloc[0]["validation_status"] == "UNRESOLVED_PRICE_JUMP"
    assert pd.isna(result.iloc[0]["confirmed_adjustment_factor"])


def test_sourced_market_move_is_resolved_without_price_adjustment(tmp_path):
    path = tmp_path / "moves.csv"
    path.write_text(
        "ticker,event_date,classification,source_url,verified_at,notes\n"
        "ABC,2025-01-02,market_move_no_adjustment,"
        "https://www.sec.gov/example,2026-07-30T00:00:00Z,earnings\n"
    )
    reviewed = load_reviewed_market_moves(path)

    result = validate_candidate_events(
        _candidate(),
        pd.DataFrame(),
        pd.DataFrame(),
        {"ABC": pd.Series(dtype=float)},
        reviewed_market_moves=reviewed,
    )

    event = result.iloc[0]
    assert event["validation_status"] == "CONFIRMED_MARKET_MOVE"
    assert event["confirmed_action_type"] == "MARKET_MOVE_NO_ADJUSTMENT"
    assert pd.isna(event["confirmed_adjustment_factor"])
    assert event["primary_source"] == "https://www.sec.gov/example"


def test_review_overlay_supports_csv_string_date_columns(tmp_path):
    path = tmp_path / "moves.csv"
    path.write_text(
        "ticker,event_date,classification,source_url,verified_at,notes\n"
        "ABC,2025-01-02,market_move_no_adjustment,"
        "https://www.sec.gov/example,2026-07-30T00:00:00Z,earnings\n"
    )
    validation = pd.DataFrame([{
        "ticker": "ABC",
        "split_date": "2025-01-02",
        "validation_status": "UNRESOLVED_PRICE_JUMP",
        "confirmed_action_type": pd.NA,
        "confirmed_action_date": pd.NA,
        "confirmed_adjustment_factor": pd.NA,
        "cash_amount": pd.NA,
        "primary_source": pd.NA,
        "fetch_error": pd.NA,
    }]).astype({"confirmed_action_date": "string"})

    result = apply_reviewed_market_moves(
        validation, load_reviewed_market_moves(path)
    )

    assert result.iloc[0]["confirmed_action_date"] == "2025-01-02"
    assert result.iloc[0]["validation_status"] == "CONFIRMED_MARKET_MOVE"


def test_nearby_real_split_confirms_provider_adjustment_discontinuity():
    splits = pd.DataFrame([{
        "ticker": "NFLX",
        "effective_date": pd.Timestamp("2025-11-17"),
        "adjustment_factor": 0.1,
        "source": "secondary://split",
        "source_tier": "secondary",
    }])

    result = validate_candidate_events(
        _candidate("NFLX", "2025-06-24", 0.1),
        splits,
        pd.DataFrame(),
        {"NFLX": pd.Series(dtype=float)},
    )

    assert result.iloc[0]["validation_status"] == "CONFIRMED"
    assert (
        result.iloc[0]["confirmed_action_type"]
        == "PROVIDER_ADJUSTMENT_DISCONTINUITY"
    )
    assert result.iloc[0]["confirmed_action_date"] == pd.Timestamp("2025-11-17")
    assert result.iloc[0]["confirmed_adjustment_factor"] == 0.1


def test_source_failure_is_not_mislabeled_as_no_action():
    result = validate_candidate_events(
        _candidate(),
        pd.DataFrame(),
        pd.DataFrame(),
        {"ABC": pd.Series(dtype=float)},
        fetch_errors={"ABC": "HTTP Error 404"},
    )

    assert result.iloc[0]["validation_status"] == "SOURCE_FETCH_FAILED"
    assert result.iloc[0]["fetch_error"] == "HTTP Error 404"
