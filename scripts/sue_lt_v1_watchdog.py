"""Dead-man check for sue-lt-v1 (outside the frozen closure).

Run in a checkout of live/sue-lt-v1 by the "sue-lt-v1 watchdog" workflow,
independently of the scheduler: it re-verifies the frozen protocol, code
closure and data release, asks what is due or was missed, and counts how
many completed sessions the marks are behind (a mark that keeps waiting for
a close ends each scheduler run quietly). It writes the decision for the
notifier and exits 1 when something needs a look, so GitHub also reports
the failed run: a check that fails, marks more than two sessions behind, a
SIGNAL window that closed in the last three days without a signal, or no
terminal record once two sessions after the end date have completed. A month
missed longer ago stays reported on the issue but no longer fails the job,
since a missed month is never caught up.

    PYTHONPATH=. python sue_lt_v1_watchdog.py decision.json
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sys
import traceback

import pandas as pd

from scripts import sue_lt_v1 as runner
from src.research import prospective_schedule as schedule
from src.research import sue_live as live

MAXIMUM_SESSIONS_BEHIND = 2
RECENTLY_MISSED = timedelta(days=3)


def sessions_behind(events: list[dict], now: datetime) -> tuple[int | None, str | None]:
    """Completed sessions after the latest valuation (before it: the first signal)."""
    signals = [e["payload"]["signal_date"] for e in events if e["event_type"] == "SIGNAL_FROZEN"]
    if not signals or any(e["event_type"] == "TERMINAL_RECORDED" for e in events):
        return None, None
    marks = [e["payload"]["as_of"] for e in events if e["event_type"] == "VALUATION_APPENDED"]
    reference = pd.Timestamp(marks[-1] if marks else signals[0])
    completed = schedule.latest_completed_session(now)
    if completed is not None:
        completed = min(completed, live.END_DATE)
    if completed is None or completed <= reference:
        return 0, marks[-1] if marks else None
    count = len(schedule.sessions_between(reference + pd.Timedelta(days=1), completed))
    return count, marks[-1] if marks else None


def terminal_overdue(events: list[dict], now: datetime) -> bool:
    """Two sessions after END_DATE have completed, and the observation has no end record.

    The final MARK can be settled only after the first of them; the second
    leaves the scheduler a day of runs before this calls it overdue.
    """
    if any(e["event_type"] == "TERMINAL_RECORDED" for e in events):
        return False
    if not any(e["event_type"] == "SIGNAL_FROZEN" for e in events):
        return False
    completed = schedule.latest_completed_session(now)
    return completed is not None and completed > schedule.next_session(live.END_DATE)


def needs_a_look(decision: dict, code: int, now: datetime) -> bool:
    if code not in (0, 2):
        return True
    behind = decision.get("sessions_behind")
    if behind is not None and behind > MAXIMUM_SESSIONS_BEHIND:
        return True
    if decision.get("terminal_overdue"):
        return True
    return any(now - schedule.signal_window(pd.Timestamp(day))[1] < RECENTLY_MISSED
               for day in decision.get("missed") or [])


def main(path: str, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    try:
        decision, code = runner.check()
        events = live.read_ledger(runner.LEDGER)
    except Exception:
        traceback.print_exc()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("")
        return 1
    behind, latest = sessions_behind(events, now)
    decision.update({"sessions_behind": behind, "latest_valuation": latest,
                     "terminal_overdue": terminal_overdue(events, now)})
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(decision, handle, indent=2, default=str)
    print(json.dumps(decision, indent=2, default=str))
    return 1 if needs_a_look(decision, code, now) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "decision.json"))
