#!/usr/bin/env python3
"""Prepare, install, or inspect the v6 LaunchAgent; dry-run by default."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess


LABEL = "com.quant-stocks.v6-shadow"
SOURCE = Path("ops/com.quant-stocks.v6-shadow.plist")
DEFAULT_LATEST_RUN = Path(
    "output/daily/can-slim-v6-walkforward-defensive-ensemble-shadow/"
    "latest_scheduled_run.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_source(source: Path = SOURCE) -> dict:
    payload = plistlib.loads(source.read_bytes())
    if payload.get("Label") != LABEL:
        raise ValueError("unexpected LaunchAgent label")
    arguments = payload.get("ProgramArguments") or []
    if not arguments or not str(arguments[-1]).endswith(
        "/scripts/research_v6_scheduled_run.py"
    ):
        raise ValueError("LaunchAgent does not run the v6 scheduled entrypoint")
    intervals = payload.get("StartCalendarInterval") or []
    if {item.get("Weekday") for item in intervals} != {2, 3, 4, 5, 6}:
        raise ValueError("LaunchAgent weekdays are not Tuesday through Saturday")
    if not all(item.get("Hour") == 9 and item.get("Minute") == 0 for item in intervals):
        raise ValueError("LaunchAgent schedule is not 09:00 local time")
    return {
        "label": LABEL,
        "source": str(source.resolve()),
        "source_sha256": _sha256(source),
        "schedule": "Tuesday-Saturday 09:00 local time",
    }


def launchd_status(
    *,
    source: Path = SOURCE,
    destination: Path | None = None,
    uid: int | None = None,
    runner=subprocess.run,
    latest_run_path: Path = DEFAULT_LATEST_RUN,
) -> dict:
    checked = validate_source(source)
    uid = int(uid if uid is not None else os.getuid())
    destination = destination or (
        Path.home() / "Library/LaunchAgents" / source.name
    )
    installed = destination.is_file()
    installed_matches = installed and _sha256(destination) == checked["source_sha256"]
    command = ["launchctl", "print", f"gui/{uid}/{LABEL}"]
    result = runner(command, capture_output=True, text=True, check=False)
    loaded = result.returncode == 0
    latest_run = None
    if latest_run_path.is_file():
        payload = json.loads(latest_run_path.read_text(encoding="utf-8"))
        latest_run = {
            "path": str(latest_run_path),
            "status": payload.get("status"),
            "expected_session": payload.get("expected_session"),
            "market_ready": (payload.get("market") or {}).get("readiness", {}).get(
                "ready_for_v6_signal"
            ),
            "release_status": payload.get("release_status"),
            "broker_action_authorized": payload.get("broker_action_authorized"),
        }
    return {
        **checked,
        "destination": str(destination),
        "installed": installed,
        "installed_matches_source": installed_matches,
        "loaded": loaded,
        "launchctl_returncode": int(result.returncode),
        "latest_scheduled_run": latest_run,
        "state": (
            "LOADED"
            if loaded and installed_matches
            else "INSTALLED_NOT_LOADED"
            if installed and installed_matches
            else "INSTALLED_SOURCE_MISMATCH"
            if installed
            else "PREPARED_NOT_INSTALLED"
        ),
    }


def install(
    *,
    apply: bool = False,
    source: Path = SOURCE,
    destination: Path | None = None,
    uid: int | None = None,
    runner=subprocess.run,
) -> dict:
    before = launchd_status(
        source=source, destination=destination, uid=uid, runner=runner
    )
    destination = Path(before["destination"])
    actions = [
        f"copy {source.resolve()} to {destination}",
        f"launchctl bootstrap gui/{uid if uid is not None else os.getuid()} {destination}",
    ]
    if not apply:
        return {
            "status": "DRY_RUN",
            "applied": False,
            "actions": actions,
            "before": before,
            "network_requests_started": False,
        }
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    effective_uid = int(uid if uid is not None else os.getuid())
    if not before["loaded"]:
        result = runner(
            ["launchctl", "bootstrap", f"gui/{effective_uid}", str(destination)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip()}")
    after = launchd_status(
        source=source, destination=destination, uid=effective_uid, runner=runner
    )
    if after["state"] != "LOADED":
        raise RuntimeError(f"LaunchAgent did not converge to LOADED: {after}")
    return {
        "status": "INSTALLED_AND_LOADED",
        "applied": True,
        "actions": actions,
        "before": before,
        "after": after,
        "network_requests_started": False,
        "note": "Bootstrap loads the schedule but does not kickstart an immediate run.",
    }


def unload(
    *,
    apply: bool = False,
    source: Path = SOURCE,
    destination: Path | None = None,
    uid: int | None = None,
    runner=subprocess.run,
) -> dict:
    before = launchd_status(
        source=source, destination=destination, uid=uid, runner=runner
    )
    effective_uid = int(uid if uid is not None else os.getuid())
    action = f"launchctl bootout gui/{effective_uid}/{LABEL}"
    if not apply:
        return {
            "status": "DRY_RUN_UNLOAD",
            "applied": False,
            "action": action,
            "before": before,
            "installed_plist_will_be_retained": True,
        }
    if before["loaded"]:
        result = runner(
            ["launchctl", "bootout", f"gui/{effective_uid}/{LABEL}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"launchctl bootout failed: {result.stderr.strip()}")
    after = launchd_status(
        source=source, destination=destination, uid=effective_uid, runner=runner
    )
    if after["loaded"]:
        raise RuntimeError("LaunchAgent remains loaded after bootout")
    return {
        "status": "UNLOADED_PLIST_RETAINED",
        "applied": True,
        "action": action,
        "before": before,
        "after": after,
        "installed_plist_retained": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--unload", action="store_true")
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.status:
        result = launchd_status(source=args.source, destination=args.destination)
    elif args.unload:
        result = unload(
            apply=args.apply,
            source=args.source,
            destination=args.destination,
        )
    else:
        result = install(
            apply=args.apply,
            source=args.source,
            destination=args.destination,
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
