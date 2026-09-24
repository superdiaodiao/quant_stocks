#!/usr/bin/env python3
"""Clock-and-calendar driven entry point for the v50r3 prospective observation.

Any scheduler (cron, launchd, GitHub Actions) may invoke this hourly from any
working directory, and a person may run it by hand at any time: it switches to
the repository root and decides from the UTC clock, the Nasdaq calendar, the
append-only ledger, and the dates in the frozen r3 protocol whether a SIGNAL
or MARK is due.  A SIGNAL window runs from 30 minutes after the month-end
close until pre-market trading opens on the next session.  It never backfills
a missed signal.

``run`` executes only in a checkout of the ``live/v50r3`` branch, the pinned
copy created by ``scripts/setup_v50r3_live.sh``; ``check`` works anywhere.
``run --commit`` records the result in git right after a successful freeze or
mark, committing only the ledger, the new signal file and the sourced event
supplement; ``--push`` also pushes the branch.  The GitHub watchdog reads
``live/v50r3``, so the ledger must reach it before the SIGNAL window closes.

Exit codes: 0 nothing due, done, or another staging holds the lock; 1 an
error, including a failed git record; 2 a SIGNAL window was missed (the held
portfolio is still marked); 3 the r3 protocol is not frozen or its ledger is
missing.

This module is research-only.  It cannot connect to a broker or create orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pandas as pd

from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v50r3_corrected_v47 as r3
from src.research import prospective_schedule as schedule


MISSED_EXIT_CODE = 2
NOT_READY_EXIT_CODE = 3
NOT_READY_ACTIONS = {"PROTOCOL_NOT_FROZEN", "LEDGER_MISSING"}


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=r3.REPO_ROOT,
        check=check,
        capture_output=True,
        text=True,
    )


def record_in_git(
    paths: list[str | Path],
    *,
    message: str,
    push: bool,
    remote: str = "origin",
) -> dict:
    """Commit exactly ``paths`` (never other staged work) and optionally push."""
    relative = [r3._portable_path(path) for path in paths]
    result: dict = {"paths": relative, "committed": False, "pushed": False}
    try:
        _git("add", "--", *relative)
        staged = _git("diff", "--cached", "--name-only", "--", *relative).stdout
        if staged.strip():
            _git("commit", "--only", "-m", message, "--", *relative)
            result["committed"] = True
        result["commit"] = _git("rev-parse", "HEAD").stdout.strip()
        if push:
            branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            _git("push", remote, f"HEAD:refs/heads/{branch}")
            result["pushed"] = True
            result["branch"] = branch
            if branch != r3.LIVE_BRANCH:
                result["warning"] = (
                    f"pushed {branch}, but the watchdog reads {r3.LIVE_BRANCH}"
                )
    except subprocess.CalledProcessError as exc:
        result["error"] = (exc.stderr or exc.stdout or str(exc)).strip()
    return result


def run(
    *,
    now: datetime | None = None,
    execute: bool,
    protocol_path: str | Path = r3.PROTOCOL_PATH,
    ledger_path: str | Path = r3.LEDGER_PATH,
    bundles_dir: str | Path = r3.BUNDLES_DIR,
    signals_dir: str | Path = r3.SIGNALS_DIR,
    lock_path: str | Path = r3.STAGING_LOCK_PATH,
    supplement_path: str | Path = r3.SUPPLEMENT_PATH,
    commit: bool = False,
    push: bool = False,
    remote: str = "origin",
) -> dict:
    now = schedule.as_utc(now or datetime.now(timezone.utc))
    if not r3.resolve(protocol_path).is_file():
        return {
            "now_utc": now.isoformat(timespec="seconds"),
            "action": "PROTOCOL_NOT_FROZEN",
            "protocol_path": str(protocol_path),
            "executed": False,
        }
    protocol, _protocol_sha = r3._validated_protocol(protocol_path)
    ledger = r3.resolve(ledger_path)
    if not ledger.is_file():
        # A missing ledger is never "no events": that is how a wrong working
        # directory used to look like a fresh observation.
        return {
            "now_utc": now.isoformat(timespec="seconds"),
            "action": "LEDGER_MISSING",
            "ledger_path": str(ledger_path),
            "executed": False,
        }
    events = v43.read_ledger(ledger)
    if not events or events[0]["protocol_sha256"] != r3._sha256(protocol_path):
        raise RuntimeError("v50r3 ledger does not bind the frozen protocol")
    first, missed = r3.signal_dates(protocol)
    decision = schedule.decide(
        now, events, first_signal_date=first, missed_signal_dates=missed
    )
    decision["executed"] = False
    if not execute or decision["action"] not in {"RUN_SIGNAL", "RUN_MARK"}:
        return decision
    as_of = decision["as_of"]
    purpose = "SIGNAL" if decision["action"] == "RUN_SIGNAL" else "MARK"
    bundle = Path(bundles_dir) / f"{as_of}_{purpose.lower()}"
    try:
        with r3.staging_lock(lock_path):
            staged = r3.stage_bundle(
                as_of=as_of,
                purpose=purpose,
                observed_at=datetime.now(timezone.utc),
                bundles_dir=bundles_dir,
                signals_dir=signals_dir,
                ledger_path=ledger_path,
                protocol_path=protocol_path,
                lock_path=lock_path,
                supplement_path=supplement_path,
            )
            if purpose == "SIGNAL":
                result = r3.freeze_signal(
                    bundle=bundle,
                    protocol_path=protocol_path,
                    ledger_path=ledger_path,
                    signals_dir=signals_dir,
                    lock_path=lock_path,
                )
            else:
                result = r3.append_mark(
                    bundle=bundle,
                    protocol_path=protocol_path,
                    ledger_path=ledger_path,
                    signals_dir=signals_dir,
                    lock_path=lock_path,
                    supplement_path=supplement_path,
                )
    except r3.StagingInProgress as exc:
        decision["action_status"] = "STAGING_IN_PROGRESS"
        decision["detail"] = str(exc)
        return decision
    decision["executed"] = True
    decision["staged"] = {
        "bundle": str(bundle),
        "status": staged.get("status"),
        "recovered_stale_builds": staged.get("recovered_stale_builds", []),
    }
    decision["result_status"] = result.get("status")
    if purpose == "SIGNAL":
        decision["targets"] = result.get("targets")
    if commit or push:
        paths: list[str | Path] = [ledger_path]
        message = f"research: append {as_of} v50r3 mark"
        if purpose == "SIGNAL":
            paths.append(Path(signals_dir) / f"signal_{as_of}.json")
            message = f"research: freeze {as_of} v50r3 signal"
        elif r3.resolve(supplement_path).is_file():
            # The mark binds the supplement rows it used; keep them together.
            paths.append(supplement_path)
        decision["git"] = record_in_git(
            paths, message=message, push=push, remote=remote
        )
    return decision


def exit_code(decision: dict) -> int:
    if decision["action"] in NOT_READY_ACTIONS:
        return NOT_READY_EXIT_CODE
    if decision["action"] == "SIGNAL_WINDOW_MISSED" or decision.get(
        "signal_window_missed"
    ):
        return MISSED_EXIT_CODE
    if (decision.get("git") or {}).get("error"):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    os.chdir(r3.REPO_ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "run"):
        sub = subparsers.add_parser(name)
        sub.add_argument(
            "--now",
            help="ISO-8601 aware timestamp to evaluate instead of the clock",
        )
        if name == "run":
            sub.add_argument(
                "--commit",
                action="store_true",
                help="commit only the ledger and new signal after success",
            )
            sub.add_argument(
                "--push",
                action="store_true",
                help="commit and push the current branch after success",
            )
            sub.add_argument("--remote", default="origin")
    args = parser.parse_args(argv)
    now = pd.Timestamp(args.now).to_pydatetime() if args.now else None
    execute = args.command == "run"
    if execute:
        r3.require_live_checkout()
    decision = run(
        now=now,
        execute=execute,
        commit=execute and (args.commit or args.push),
        push=execute and args.push,
        remote=getattr(args, "remote", "origin"),
    )
    print(json.dumps(decision, indent=2, sort_keys=True, default=str))
    return exit_code(decision)


if __name__ == "__main__":
    sys.exit(main())
