"""Fail when the v50r3 marks fall behind (a watchdog check, not the runtime).

A mark that waits for a close that never comes (a held stock halted for good,
a vendor that stops publishing one) ends every scheduler run with a warning,
not an error, so nothing would report it.  Once a signal is frozen, the
latest mark (before the first mark: the first signal date) may be at most
MAXIMUM_SESSIONS_BEHIND completed sessions old; beyond that this exits 1.

This module is research-only.  It cannot connect to a broker or create orders.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import sys

import pandas as pd

from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v50r3_corrected_v47 as r3
from src.research import prospective_schedule as schedule

MAXIMUM_SESSIONS_BEHIND = 2


def sessions_behind(events: list[dict], now: datetime) -> int | None:
    """Completed sessions after the latest mark; None before any signal."""
    signals = schedule.frozen_signal_dates(events)
    if not signals:
        return None
    reference = schedule.latest_event_date(events, "VALUATION_APPENDED", "as_of")
    reference = pd.Timestamp(reference if reference is not None else signals[0])
    completed = schedule.latest_completed_session(now)
    if completed is None or completed <= reference:
        return 0
    return len(schedule.sessions_between(reference + pd.Timedelta(days=1), completed))


def main(now: datetime | None = None) -> int:
    os.chdir(r3.REPO_ROOT)
    ledger = r3.resolve(r3.LEDGER_PATH)
    events = v43.read_ledger(ledger) if ledger.is_file() else []
    behind = sessions_behind(events, now or datetime.now(timezone.utc))
    print(json.dumps({
        "sessions_behind": behind,
        "maximum_sessions_behind": MAXIMUM_SESSIONS_BEHIND,
    }))
    if behind is not None and behind > MAXIMUM_SESSIONS_BEHIND:
        print(
            f"::error::v50r3 marks are {behind} completed sessions behind; "
            "see the v50r3 scheduler runs"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
