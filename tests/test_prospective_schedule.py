from datetime import datetime

import pandas as pd
import pytest

from src.research import prospective_schedule as schedule


FIRST = pd.Timestamp("2026-09-30")
MISSED = [pd.Timestamp("2026-08-31")]


def _signal(date: str) -> dict:
    return {"event_type": "SIGNAL_FROZEN", "payload": {"signal_date": date}}


def _mark(date: str) -> dict:
    return {"event_type": "VALUATION_APPENDED", "payload": {"as_of": date}}


def _at(text: str) -> datetime:
    return pd.Timestamp(text).to_pydatetime()


def _decide(text: str, events: list[dict], first=FIRST, missed=MISSED) -> dict:
    return schedule.decide(
        _at(text), events, first_signal_date=first, missed_signal_dates=missed
    )


def test_windows_use_the_calendar_close_in_both_seasons() -> None:
    assert schedule.signal_window(pd.Timestamp("2026-09-30")) == (
        _at("2026-09-30T20:30:00Z"),
        _at("2026-09-30T23:59:59Z"),
    )
    assert schedule.signal_window(pd.Timestamp("2026-11-30"))[0] == _at(
        "2026-11-30T21:30:00Z"
    )
    assert schedule.session_close_utc(pd.Timestamp("2026-11-27")) == _at(
        "2026-11-27T18:00:00Z"
    )
    with pytest.raises(ValueError, match="not a month-end"):
        schedule.signal_window(pd.Timestamp("2026-10-15"))
    with pytest.raises(ValueError, match="timezone-aware"):
        schedule.as_utc(datetime(2026, 9, 30, 21, 0))


def test_month_end_sessions_cross_the_year_boundary() -> None:
    ends = schedule.month_end_sessions(
        pd.Timestamp("2026-12-01"), pd.Timestamp("2027-03-31")
    )
    assert [f"{day:%Y-%m-%d}" for day in ends] == [
        "2026-12-31",
        "2027-01-29",
        "2027-02-26",
        "2027-03-31",
    ]


def test_first_signal_date_moves_past_an_open_or_passed_window() -> None:
    assert schedule.first_signal_date_after(
        _at("2026-09-28T12:00:00Z"), FIRST
    ) == pd.Timestamp("2026-09-30")
    # Frozen after the September window opened: the next month-end is first.
    assert schedule.first_signal_date_after(
        _at("2026-09-30T21:00:00Z"), FIRST
    ) == pd.Timestamp("2026-10-30")
    assert schedule.first_signal_date_after(
        _at("2026-10-05T00:00:00Z"), FIRST
    ) == pd.Timestamp("2026-10-30")


def test_signal_lifecycle_and_remaining_window() -> None:
    assert _decide("2026-09-30T19:00:00Z", [])["action"] == (
        "WAIT_FOR_FIRST_SIGNAL_DATE"
    )
    assert _decide("2026-09-30T20:10:00Z", [])["action"] == "WAIT_FOR_SIGNAL_WINDOW"
    inside = _decide("2026-09-30T20:45:00Z", [])
    assert inside["action"] == "RUN_SIGNAL"
    assert inside["as_of"] == "2026-09-30"
    assert inside["minutes_left_in_window"] == pytest.approx(195.0, abs=0.1)
    assert _decide("2026-09-30T23:59:59Z", [])["action"] == "RUN_SIGNAL"
    assert _decide("2026-09-30T22:00:00Z", [_signal("2026-09-30")])["action"] == (
        "NO_ACTION"
    )


def test_missed_window_is_reported_and_never_backfilled() -> None:
    missed = _decide("2026-10-01T00:30:00Z", [])
    assert missed["action"] == "SIGNAL_WINDOW_MISSED"
    assert missed["as_of"] is None
    assert missed["missed_signal_dates"] == ["2026-08-31", "2026-09-30"]


def test_marks_follow_completed_sessions_after_a_frozen_signal() -> None:
    events = [_signal("2026-09-30")]
    assert _decide("2026-10-01T20:10:00Z", events)["action"] == "NO_ACTION"
    ready = _decide("2026-10-01T20:45:00Z", events)
    assert ready["action"] == "RUN_MARK" and ready["as_of"] == "2026-10-01"
    valued = events + [_mark("2026-10-01")]
    assert _decide("2026-10-02T08:00:00Z", valued)["action"] == "NO_ACTION"


def test_later_first_signal_date_comes_from_the_protocol() -> None:
    first = pd.Timestamp("2026-10-30")
    missed = [pd.Timestamp("2026-08-31"), pd.Timestamp("2026-09-30")]
    before = _decide("2026-10-05T12:00:00Z", [], first, missed)
    assert before["action"] == "WAIT_FOR_FIRST_SIGNAL_DATE"
    assert before["next_signal_window_utc"]["signal_date"] == "2026-10-30"
    assert before["missed_signal_dates"] == ["2026-08-31", "2026-09-30"]
    assert _decide("2026-10-30T21:00:00Z", [], first, missed)["action"] == (
        "RUN_SIGNAL"
    )
