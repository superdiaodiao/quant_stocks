#!/usr/bin/env python3
"""Clock-and-calendar driven entry point for the v50r2 prospective observation.

The 2026-08-31 window was missed because an external cron displayed one time
zone and executed in another.  This module removes that class of failure:
any scheduler (cron, launchd, GitHub Actions) may invoke it hourly, and it
decides from the UTC clock, the Nasdaq calendar, and the append-only ledger
whether a SIGNAL or MARK is due.  It never backfills a missed signal; a
missed window is reported loudly with a non-zero exit so that "the run did
not happen" becomes a detectable failure instead of a silent one.

Decision rules:

* a SIGNAL is due on the final Nasdaq session of a month on or after the
  first prospective signal date; its window opens a buffer after the
  official session close (so a provisional close is not frozen) and closes
  at 23:59:59 UTC on the same date (the same-UTC-date staging rule);
* a MARK is due for the latest completed session strictly after the most
  recently frozen signal date that has not yet been valued;
* a missed SIGNAL window is `SIGNAL_WINDOW_MISSED` and exit status 2.

This module is research-only.  It cannot connect to a broker or create orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pandas as pd

from scripts import research_v42_prospective_v28_observation as v42
from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v50r2_corrected_v47 as r2
from src.research.shadow_evaluation import nasdaq_calendar_for_year


SIGNAL_WINDOW_OPEN_BUFFER = timedelta(minutes=30)
MARK_READY_BUFFER = timedelta(minutes=30)
MISSED_EXIT_CODE = 2


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("scheduler timestamps must be timezone-aware")
    return moment.astimezone(timezone.utc)


def session_close_utc(session: pd.Timestamp) -> datetime:
    """Official Nasdaq close for ``session`` as an aware UTC datetime."""
    stamp = pd.Timestamp(session).normalize()
    calendar = nasdaq_calendar_for_year(stamp.year)
    if not v43._is_nasdaq_session(stamp):
        raise ValueError(f"{stamp:%Y-%m-%d} is not a Nasdaq session")
    return calendar.session_close(stamp).to_pydatetime().astimezone(timezone.utc)


def signal_window(signal_date: pd.Timestamp) -> tuple[datetime, datetime]:
    """Return the (open, close) UTC window in which a SIGNAL may be staged."""
    stamp = pd.Timestamp(signal_date).normalize()
    if not v42._is_month_end_signal(stamp):
        raise ValueError(f"{stamp:%Y-%m-%d} is not a month-end Nasdaq session")
    opens = session_close_utc(stamp) + SIGNAL_WINDOW_OPEN_BUFFER
    closes = datetime(
        stamp.year, stamp.month, stamp.day, 23, 59, 59, tzinfo=timezone.utc
    )
    if opens >= closes:
        raise ValueError("signal window closes before the session close buffer")
    return opens, closes


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


def month_end_sessions(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    return [
        session
        for session in sessions_between(start, end)
        if v42._is_month_end_signal(session)
    ]


def latest_due_signal_date(now: datetime) -> pd.Timestamp | None:
    """Most recent month-end session whose close has already happened."""
    now = _utc(now)
    today = pd.Timestamp(now.date())
    candidates = month_end_sessions(r2.FIRST_PROSPECTIVE_SIGNAL_DATE, today)
    due = [
        session for session in candidates if session_close_utc(session) <= now
    ]
    return due[-1] if due else None


def latest_completed_session(now: datetime) -> pd.Timestamp | None:
    """Latest session whose close plus the mark buffer has passed."""
    now = _utc(now)
    today = pd.Timestamp(now.date())
    sessions = sessions_between(today - pd.Timedelta(days=10), today)
    completed = [
        session
        for session in sessions
        if session_close_utc(session) + MARK_READY_BUFFER <= now
    ]
    return completed[-1] if completed else None


def _frozen_signal_dates(events: list[dict]) -> list[pd.Timestamp]:
    return sorted(
        pd.Timestamp(event["payload"]["signal_date"]).normalize()
        for event in events
        if event["event_type"] == "SIGNAL_FROZEN"
    )


def decide(now: datetime, events: list[dict]) -> dict:
    """Pure decision from the clock, calendar, and ledger; no side effects."""
    now = _utc(now)
    frozen = _frozen_signal_dates(events)
    latest_mark = v43._latest_event_date(events, "VALUATION_APPENDED", "as_of")
    decision: dict = {
        "now_utc": now.isoformat(timespec="seconds"),
        "first_prospective_signal_date": (
            r2.FIRST_PROSPECTIVE_SIGNAL_DATE.strftime("%Y-%m-%d")
        ),
        "frozen_signal_dates": [f"{d:%Y-%m-%d}" for d in frozen],
        "latest_mark": None if latest_mark is None else f"{latest_mark:%Y-%m-%d}",
        "missed_signal_dates": [f"{r2.MISSED_SIGNAL_DATE:%Y-%m-%d}"],
        "backfill_allowed": False,
        "action": "NO_ACTION",
        "as_of": None,
    }
    due = latest_due_signal_date(now)
    if due is None:
        decision["action"] = "WAIT_FOR_FIRST_SIGNAL_DATE"
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


def run(
    *,
    now: datetime | None = None,
    execute: bool,
    ledger_path: str | Path = r2.LEDGER_PATH,
    bundles_dir: str | Path = r2.BUNDLES_DIR,
) -> dict:
    now = _utc(now or datetime.now(timezone.utc))
    ledger = Path(ledger_path)
    events = v43.read_ledger(ledger) if ledger.exists() else []
    decision = decide(now, events)
    decision["executed"] = False
    if not execute or decision["action"] not in {"RUN_SIGNAL", "RUN_MARK"}:
        return decision
    as_of = decision["as_of"]
    purpose = "SIGNAL" if decision["action"] == "RUN_SIGNAL" else "MARK"
    staged = r2.stage_bundle(
        as_of=as_of,
        purpose=purpose,
        observed_at=datetime.now(timezone.utc),
        bundles_dir=bundles_dir,
        ledger_path=ledger,
    )
    bundle = Path(bundles_dir) / f"{as_of}_{purpose.lower()}"
    if purpose == "SIGNAL":
        result = r2.freeze_signal(bundle=bundle, ledger_path=ledger)
    else:
        result = r2.append_mark(bundle=bundle, ledger_path=ledger)
    decision["executed"] = True
    decision["staged"] = {"bundle": str(bundle), "status": staged.get("status")}
    decision["result_status"] = result.get("status")
    return decision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "run"):
        sub = subparsers.add_parser(name)
        sub.add_argument(
            "--now",
            help="ISO-8601 aware timestamp to evaluate instead of the clock",
        )
    args = parser.parse_args(argv)
    now = pd.Timestamp(args.now).to_pydatetime() if args.now else None
    decision = run(now=now, execute=args.command == "run")
    print(json.dumps(decision, indent=2, sort_keys=True))
    return MISSED_EXIT_CODE if decision["action"] == "SIGNAL_WINDOW_MISSED" else 0


if __name__ == "__main__":
    sys.exit(main())
