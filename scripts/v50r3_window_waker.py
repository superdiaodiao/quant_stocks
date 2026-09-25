"""Start the v50r3 scheduler when a SIGNAL window opens (a GitHub helper).

GitHub starts scheduled workflow runs late, by hours at busy times: in this
repository a 00:30 UTC schedule started around 03:45 every day, and a 12:15
UTC one at 17:11.  A SIGNAL takes about three hours on a GitHub runner, so a
late half-hourly trigger would leave its window little room for a retry.

This decides, from the frozen protocol, the ledger and the calendar, whether a
SIGNAL (a month end, or the catch-up of a missed one) is due now or once
today's window opens.  It then sleeps until that moment and dispatches the
"v50r3 scheduler" workflow, which GitHub starts at once.  It never stages,
freezes or marks, and it is not part of the r3 code closure.

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
from src.research import prospective_schedule as schedule

WORKFLOW = "v50r3_scheduler.yml"
DISPATCH_DELAY = timedelta(minutes=1)
# A GitHub job ends after six hours; a run that would sleep longer leaves the
# window to a later waker run.
MAXIMUM_SLEEP = timedelta(hours=5, minutes=30)


def dispatch_time(
    now: datetime,
    events: list[dict],
    first: pd.Timestamp,
    missed: list[pd.Timestamp],
) -> datetime | None:
    """When a SIGNAL run should start: now, when today's window opens, or never."""
    now = schedule.as_utc(now)

    def signal_due(moment: datetime) -> bool:
        decision = schedule.decide(
            moment, events, first_signal_date=first, missed_signal_dates=missed
        )
        return decision["action"] == "RUN_SIGNAL"

    if signal_due(now):
        return now
    today = pd.Timestamp(now.date())
    if not schedule.is_session(today):
        return None
    opens, _closes = schedule.staging_window(today)
    if opens <= now:
        return None
    moment = opens + DISPATCH_DELAY
    return moment if signal_due(moment) else None


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="decide only")
    parser.add_argument("--ref", default="master", help="branch holding the workflow")
    args = parser.parse_args(argv)
    os.chdir(r3.REPO_ROOT)
    protocol, _protocol_sha = r3._validated_protocol(r3.PROTOCOL_PATH)
    first, missed = r3.signal_dates(protocol)
    events = v43.read_ledger(r3.resolve(r3.LEDGER_PATH))
    now = schedule.as_utc(now or datetime.now(timezone.utc))
    moment = dispatch_time(now, events, first, missed)
    report: dict = {
        "now_utc": now.isoformat(timespec="seconds"),
        "dispatch_at_utc": None if moment is None else moment.isoformat(timespec="seconds"),
        "dispatched": False,
    }
    if moment is None:
        report["reason"] = "no SIGNAL is due now or in today's window"
    elif moment - now > MAXIMUM_SLEEP:
        report["reason"] = "the window opens too late for this run; a later one waits"
    elif args.dry_run:
        report["reason"] = "dry run"
    else:
        time.sleep(max(0.0, (moment - datetime.now(timezone.utc)).total_seconds()))
        subprocess.run(
            ["gh", "workflow", "run", WORKFLOW, "--ref", args.ref], check=True
        )
        report["dispatched"] = True
        report["dispatched_at_utc"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
