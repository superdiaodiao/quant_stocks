"""Start the v50r3 scheduler once a due SIGNAL's inputs are published.

GitHub starts scheduled workflow runs late, by hours at busy times: in this
repository a 00:30 UTC schedule started around 03:45 every day, and a 12:15
UTC one at 17:11.  Nasdaq publishes a session's rows hours after the close
(scripts/v50r3_sources_ready.py), and a SIGNAL takes about two and a half
hours on a runner, so the window has room for only two or three attempts.

This decides, from the frozen protocol, the ledger and the calendar, whether a
SIGNAL (a month end, or the catch-up of a missed one) is due in the open
window or in today's.  It then waits until the window is open and Nasdaq has
published that session, checking every ten minutes, and dispatches the
"v50r3 scheduler" workflow, which GitHub starts at once.  A run that is out
of time leaves the wait to a later one.  It never stages, freezes or marks,
and it is not part of the r3 code closure.

    PYTHONPATH=. python scripts/v50r3_window_waker.py [--dry-run]

This module is research-only.  It cannot connect to a broker or create orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys
import time

import pandas as pd

from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v50r3_corrected_v47 as r3
from scripts import v50r3_sources_ready as sources
from src.research import prospective_schedule as schedule

WORKFLOW = "v50r3_scheduler.yml"
DISPATCH_DELAY = timedelta(minutes=1)
POLL_INTERVAL = timedelta(minutes=10)
# A GitHub job ends after six hours.
MAXIMUM_WAIT = timedelta(hours=5, minutes=30)


def due_signal_session(
    now: datetime,
    events: list[dict],
    first: pd.Timestamp,
    missed: list[pd.Timestamp],
) -> tuple[pd.Timestamp, datetime] | None:
    """The session of a due SIGNAL and when its window opens, or None.

    The open window's SIGNAL, else one due once today's window opens.
    """
    now = schedule.as_utc(now)

    def decide(moment: datetime) -> dict:
        return schedule.decide(
            moment, events, first_signal_date=first, missed_signal_dates=missed
        )

    decision = decide(now)
    if decision["action"] == "RUN_SIGNAL":
        session = pd.Timestamp(decision["as_of"]).normalize()
        return session, schedule.staging_window(session)[0]
    today = pd.Timestamp(now.date())
    if not schedule.is_session(today):
        return None
    opens, _closes = schedule.staging_window(today)
    if opens <= now:
        return None
    later = decide(opens + DISPATCH_DELAY)
    if later["action"] != "RUN_SIGNAL":
        return None
    return pd.Timestamp(later["as_of"]).normalize(), opens


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="check once, never wait")
    parser.add_argument("--ref", default="master", help="branch holding the workflow")
    args = parser.parse_args(argv)
    os.chdir(r3.REPO_ROOT)
    protocol, _protocol_sha = r3._validated_protocol(r3.PROTOCOL_PATH)
    first, missed = r3.signal_dates(protocol)
    events = v43.read_ledger(r3.resolve(r3.LEDGER_PATH))
    now = schedule.as_utc(now or _utc_now())
    deadline = now + MAXIMUM_WAIT
    report: dict = {"started_utc": now.isoformat(timespec="seconds"), "dispatched": False}
    while True:
        due = due_signal_session(now, events, first, missed)
        if due is None:
            report["reason"] = "no SIGNAL is due in the open window or today's"
            break
        session, opens = due
        report["session"] = f"{session:%Y-%m-%d}"
        report["window_opens_utc"] = opens.isoformat(timespec="seconds")
        if now >= opens:
            published = sources.sources_ready(session)
            report["sources"] = published
            if published["ready"]:
                subprocess.run(
                    ["gh", "workflow", "run", WORKFLOW, "--ref", args.ref], check=True
                )
                report["dispatched"] = True
                report["dispatched_utc"] = _utc_now().isoformat(timespec="seconds")
                break
        if args.dry_run:
            report["reason"] = "dry run"
            break
        wake = max(opens + DISPATCH_DELAY, now + POLL_INTERVAL)
        if wake > deadline:
            report["reason"] = "not published within this run's time; a later run waits"
            break
        time.sleep(max(0.0, (wake - _utc_now()).total_seconds()))
        now = max(_utc_now(), wake)
    report["finished_utc"] = _utc_now().isoformat(timespec="seconds")
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
