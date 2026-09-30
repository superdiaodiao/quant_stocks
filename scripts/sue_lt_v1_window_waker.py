"""Start the sue-lt-v1 scheduler once a due SIGNAL's session is published.

GitHub starts scheduled runs late, by hours at busy times, and a sue-lt-v1
SIGNAL downloads about 3,000 names one at a time, so a weekday month-end
window (11.5 hours, of which Nasdaq's four-to-five-hour publication delay
takes the first part) has room for few attempts. This decides from the
ledger and the calendar whether a SIGNAL is due in the open window or in
today's, waits until the window is open and Nasdaq has published that
session (scripts/sue_lt_v1_sources_ready.py), checking every ten minutes,
and dispatches the "sue-lt-v1 scheduler" workflow, which GitHub starts at
once. It never stages, freezes or marks. Outside the frozen code closure:
the waker workflow fetches it from master and runs it in a checkout of the
live branch.

    PYTHONPATH=. python sue_lt_v1_window_waker.py [--dry-run]
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
import time

import pandas as pd

from scripts import sue_lt_v1 as runner
from src.research import prospective_schedule as schedule
from src.research import sue_live as live

try:  # fetched from master next to this file, or run from a master checkout
    import sue_lt_v1_sources_ready as sources
except ImportError:
    from scripts import sue_lt_v1_sources_ready as sources

WORKFLOW = "sue_lt_v1_scheduler.yml"
DISPATCH_DELAY = timedelta(minutes=1)
POLL_INTERVAL = timedelta(minutes=10)
# A GitHub job ends after six hours.
MAXIMUM_WAIT = timedelta(hours=5, minutes=30)


def due_signal_session(now: datetime, events: list[dict]) -> tuple[pd.Timestamp, datetime] | None:
    """The session of a due SIGNAL and when its window opens, or None."""
    now = schedule.as_utc(now)
    decision = runner.decide(now, events)
    if decision["action"] == "RUN_SIGNAL":
        session = pd.Timestamp(decision["as_of"])
        return session, schedule.signal_window(session)[0]
    today = pd.Timestamp(now.date())
    if not schedule.is_month_end_session(today):
        return None
    opens, _closes = schedule.signal_window(today)
    if opens <= now:
        return None
    later = runner.decide(opens + DISPATCH_DELAY, events)
    if later["action"] != "RUN_SIGNAL":
        return None
    return pd.Timestamp(later["as_of"]), opens


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="check once, never wait")
    parser.add_argument("--ref", default="master", help="branch holding the workflow")
    args = parser.parse_args(argv)
    events = live.read_ledger(runner.LEDGER)
    now = datetime.now(timezone.utc)
    deadline = now + MAXIMUM_WAIT
    report: dict = {"started_utc": now.isoformat(timespec="seconds"), "dispatched": False}
    while True:
        due = due_signal_session(now, events)
        if due is None:
            report["reason"] = "no SIGNAL is due in the open window or today's"
            break
        session, opens = due
        report.update({"session": f"{session:%Y-%m-%d}", "window_opens_utc": opens.isoformat(timespec="seconds")})
        if now >= opens:
            published = sources.sources_ready(session)
            report["sources"] = published
            if published["ready"]:
                subprocess.run(["gh", "workflow", "run", WORKFLOW, "--ref", args.ref], check=True)
                report.update({"dispatched": True,
                               "dispatched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                break
        if args.dry_run:
            report["reason"] = "dry run"
            break
        wake = max(opens + DISPATCH_DELAY, now + POLL_INTERVAL)
        if wake > deadline:
            report["reason"] = "not published within this run's time; a later run waits"
            break
        time.sleep(max(0.0, (wake - datetime.now(timezone.utc)).total_seconds()))
        now = max(datetime.now(timezone.utc), wake)
    report["finished_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
