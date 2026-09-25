from datetime import datetime

import pandas as pd

from scripts import v50r3_mark_freshness as freshness


def _event(event_type: str, **payload) -> dict:
    return {"event_type": event_type, "payload": payload}


def _at(text: str) -> datetime:
    return pd.Timestamp(text).to_pydatetime()


def test_nothing_is_behind_before_the_first_signal() -> None:
    assert freshness.sessions_behind([], _at("2026-10-06T12:00:00Z")) is None


def test_marks_that_stop_are_counted_in_completed_sessions() -> None:
    events = [_event("SIGNAL_FROZEN", signal_date="2026-09-30")]
    # The first mark is due after the 10-01 close; the next morning it is one
    # session behind, which is normal between runs.
    assert freshness.sessions_behind(events, _at("2026-10-02T12:00:00Z")) == 1
    events.append(_event("VALUATION_APPENDED", as_of="2026-10-01"))
    assert freshness.sessions_behind(events, _at("2026-10-02T12:00:00Z")) == 0
    # No mark after 10-01 by the 10-06 close: 10-02, 10-05 and 10-06.
    assert freshness.sessions_behind(events, _at("2026-10-06T21:00:00Z")) == 3
    assert 3 > freshness.MAXIMUM_SESSIONS_BEHIND
