"""Clock-and-calendar rules for a monthly prospective SIGNAL/MARK ledger.

Pure functions only.  Given an aware instant, the Nasdaq calendar (including
early closes), the events already in an append-only ledger, and the dates a
frozen protocol declares, decide whether a month-end SIGNAL or a daily MARK is
due.  A SIGNAL window opens a buffer after the official close (so a
provisional close is not frozen) and closes at 23:59:59 UTC of the same date,
the same-UTC-date staging rule.  Missed windows are reported, never
backfilled.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from src.research.shadow_evaluation import nasdaq_calendar_for_year


SIGNAL_WINDOW_OPEN_BUFFER = timedelta(minutes=30)
MARK_READY_BUFFER = timedelta(minutes=30)
MONTH_END_SEARCH_DAYS = 400


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("scheduler timestamps must be timezone-aware")
    return moment.astimezone(timezone.utc)


def sessions_between(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()
    sessions: list[pd.Timestamp] = []
    for year in range(start.year, end.year + 1):
        calendar = nasdaq_calendar_for_year(year)
        lower = max(start, pd.Timestamp(year, 1, 1))
        upper = min(end, pd.Timestamp(year, 12, 31))
        if lower <= upper:
            sessions.extend(calendar.sessions_in_range(lower, upper))
    return pd.DatetimeIndex(sessions).tz_localize(None).normalize()


def is_session(stamp: pd.Timestamp) -> bool:
    stamp = pd.Timestamp(stamp).normalize()
    sessions = sessions_between(stamp, stamp)
    return bool(len(sessions) and sessions[0] == stamp)


def is_month_end_session(stamp: pd.Timestamp) -> bool:
    stamp = pd.Timestamp(stamp).normalize()
    sessions = sessions_between(
        stamp.replace(day=1), stamp + pd.offsets.MonthEnd(0)
    )
    return bool(len(sessions) and sessions[-1] == stamp)


def session_close_utc(session: pd.Timestamp) -> datetime:
    """Official Nasdaq close for ``session`` as an aware UTC datetime."""
    stamp = pd.Timestamp(session).normalize()
    if not is_session(stamp):
        raise ValueError(f"{stamp:%Y-%m-%d} is not a Nasdaq session")
    close = nasdaq_calendar_for_year(stamp.year).session_close(stamp)
    return close.to_pydatetime().astimezone(timezone.utc)


def signal_window(signal_date: pd.Timestamp) -> tuple[datetime, datetime]:
    """Return the (open, close) UTC window in which a SIGNAL may be staged."""
    stamp = pd.Timestamp(signal_date).normalize()
    if not is_month_end_session(stamp):
        raise ValueError(f"{stamp:%Y-%m-%d} is not a month-end Nasdaq session")
    opens = session_close_utc(stamp) + SIGNAL_WINDOW_OPEN_BUFFER
    closes = datetime(
        stamp.year, stamp.month, stamp.day, 23, 59, 59, tzinfo=timezone.utc
    )
    if opens >= closes:
        raise ValueError("signal window closes before the session close buffer")
    return opens, closes


def month_end_sessions(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    return [
        session
        for session in sessions_between(start, end)
        if is_month_end_session(session)
    ]


def first_signal_date_after(
    moment: datetime, earliest: pd.Timestamp
) -> pd.Timestamp:
    """First month-end session on/after ``earliest`` whose window is still ahead."""
    moment = as_utc(moment)
    earliest = pd.Timestamp(earliest).normalize()
    horizon = max(earliest, pd.Timestamp(moment.date())) + pd.Timedelta(
        days=MONTH_END_SEARCH_DAYS
    )
    for session in month_end_sessions(earliest, horizon):
        opens, _closes = signal_window(session)
        if opens > moment:
            return session
    raise RuntimeError("no future month-end signal session found")


def latest_due_signal_date(
    now: datetime, first_signal_date: pd.Timestamp
) -> pd.Timestamp | None:
    """Most recent month-end session whose close has already happened."""
    now = as_utc(now)
    candidates = month_end_sessions(first_signal_date, pd.Timestamp(now.date()))
    due = [session for session in candidates if session_close_utc(session) <= now]
    return due[-1] if due else None


def latest_completed_session(now: datetime) -> pd.Timestamp | None:
    """Latest session whose close plus the mark buffer has passed."""
    now = as_utc(now)
    today = pd.Timestamp(now.date())
    completed = [
        session
        for session in sessions_between(today - pd.Timedelta(days=10), today)
        if session_close_utc(session) + MARK_READY_BUFFER <= now
    ]
    return completed[-1] if completed else None


def frozen_signal_dates(events: list[dict]) -> list[pd.Timestamp]:
    return sorted(
        pd.Timestamp(event["payload"]["signal_date"]).normalize()
        for event in events
        if event["event_type"] == "SIGNAL_FROZEN"
    )


def latest_event_date(
    events: list[dict], event_type: str, key: str
) -> pd.Timestamp | None:
    dates = [
        pd.Timestamp(event["payload"][key])
        for event in events
        if event["event_type"] == event_type
    ]
    return max(dates) if dates else None


def decide(
    now: datetime,
    events: list[dict],
    *,
    first_signal_date: pd.Timestamp,
    missed_signal_dates: list[pd.Timestamp] | tuple[pd.Timestamp, ...],
) -> dict:
    """Pure decision from the clock, calendar, and ledger; no side effects."""
    now = as_utc(now)
    first_signal_date = pd.Timestamp(first_signal_date).normalize()
    frozen = frozen_signal_dates(events)
    latest_mark = latest_event_date(events, "VALUATION_APPENDED", "as_of")
    decision: dict = {
        "now_utc": now.isoformat(timespec="seconds"),
        "first_prospective_signal_date": f"{first_signal_date:%Y-%m-%d}",
        "frozen_signal_dates": [f"{date:%Y-%m-%d}" for date in frozen],
        "latest_mark": None if latest_mark is None else f"{latest_mark:%Y-%m-%d}",
        "missed_signal_dates": [
            f"{pd.Timestamp(date):%Y-%m-%d}" for date in missed_signal_dates
        ],
        "backfill_allowed": False,
        "action": "NO_ACTION",
        "as_of": None,
    }
    due = latest_due_signal_date(now, first_signal_date)
    if due is None:
        decision["action"] = "WAIT_FOR_FIRST_SIGNAL_DATE"
        decision["next_signal_window_utc"] = _window_payload(
            first_signal_date
        )
        return decision
    decision["due_signal_date"] = f"{due:%Y-%m-%d}"
    opens, closes = signal_window(due)
    decision["signal_window_utc"] = {
        "opens": opens.isoformat(timespec="seconds"),
        "closes": closes.isoformat(timespec="seconds"),
    }
    if due not in frozen:
        if now < opens:
            decision["action"] = "WAIT_FOR_SIGNAL_WINDOW"
            return decision
        if now <= closes:
            decision["action"] = "RUN_SIGNAL"
            decision["as_of"] = f"{due:%Y-%m-%d}"
            decision["minutes_left_in_window"] = round(
                (closes - now).total_seconds() / 60.0, 1
            )
            return decision
        decision["missed_signal_dates"].append(f"{due:%Y-%m-%d}")
        decision["action"] = "SIGNAL_WINDOW_MISSED"
        # fall through: earlier frozen signals may still need marks
    if not frozen:
        return decision
    latest_signal = frozen[-1]
    completed = latest_completed_session(now)
    floor = latest_signal if latest_mark is None else max(latest_signal, latest_mark)
    if completed is not None and completed > floor:
        if decision["action"] == "SIGNAL_WINDOW_MISSED":
            decision["pending_mark_as_of"] = f"{completed:%Y-%m-%d}"
        else:
            decision["action"] = "RUN_MARK"
            decision["as_of"] = f"{completed:%Y-%m-%d}"
    return decision


def _window_payload(signal_date: pd.Timestamp) -> dict:
    opens, closes = signal_window(signal_date)
    return {
        "signal_date": f"{pd.Timestamp(signal_date):%Y-%m-%d}",
        "opens": opens.isoformat(timespec="seconds"),
        "closes": closes.isoformat(timespec="seconds"),
    }
