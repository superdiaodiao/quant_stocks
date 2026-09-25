from __future__ import annotations

from datetime import datetime
import json

import pandas as pd
import pytest

from scripts import v50r3_window_waker as waker

FIRST = pd.Timestamp("2026-09-30")
MISSED = [pd.Timestamp("2026-08-31")]


def _at(text: str) -> datetime:
    return pd.Timestamp(text).to_pydatetime()


def _signal(date: str) -> dict:
    return {"event_type": "SIGNAL_FROZEN", "payload": {"signal_date": date}}


def _mark(date: str) -> dict:
    return {"event_type": "VALUATION_APPENDED", "payload": {"as_of": date}}


def _when(text: str, events: list[dict]) -> str | None:
    moment = waker.dispatch_time(_at(text), events, FIRST, MISSED)
    return None if moment is None else moment.isoformat(timespec="minutes")


def test_a_month_end_wakes_the_scheduler_a_minute_after_its_window_opens() -> None:
    assert _when("2026-09-30T15:07:00Z", []) == "2026-09-30T20:31+00:00"
    # Once the window is open the scheduler is started at once.
    assert _when("2026-09-30T21:40:00Z", []) == "2026-09-30T21:40+00:00"
    assert _when("2026-10-01T07:30:00Z", []) == "2026-10-01T07:30+00:00"
    # A frozen signal, or any other day, needs no wake-up.
    assert _when("2026-09-30T15:07:00Z", [_signal("2026-09-30")]) is None
    assert _when("2026-10-29T15:07:00Z", [_signal("2026-09-30")]) is None
    # The winter window opens an hour later.
    assert _when("2026-11-30T15:07:00Z", [_signal("2026-10-30")]) == (
        "2026-11-30T21:31+00:00"
    )


def test_a_missed_month_wakes_each_later_session_until_it_is_caught_up() -> None:
    assert _when("2026-10-01T15:07:00Z", []) == "2026-10-01T20:31+00:00"
    assert _when("2026-10-02T16:37:00Z", []) == "2026-10-02T20:31+00:00"
    # Friday's window stays open until Monday's pre-market.
    assert _when("2026-10-03T16:37:00Z", []) == "2026-10-03T16:37+00:00"
    caught_up = [_signal("2026-10-01"), _mark("2026-10-02")]
    assert _when("2026-10-05T16:37:00Z", caught_up) is None


def test_the_cli_sleeps_then_dispatches_the_scheduler_workflow(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(waker.r3, "_validated_protocol", lambda _path: ({
        "signal_policy": {
            "first_prospective_signal_date": "2026-09-30",
            "missed_signal_dates": ["2026-08-31"],
        },
    }, "0" * 64))
    monkeypatch.setattr(waker.v43, "read_ledger", lambda _path: [])
    slept, commands = [], []
    monkeypatch.setattr(waker.time, "sleep", slept.append)
    monkeypatch.setattr(
        waker.subprocess, "run", lambda command, check: commands.append(command)
    )

    # Too early: a later waker run waits for the window instead.
    assert waker.main([], now=_at("2026-09-30T13:07:00Z")) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dispatched"] is False and "too late" in report["reason"]
    assert waker.main(["--dry-run"], now=_at("2026-09-30T16:37:00Z")) == 0
    assert json.loads(capsys.readouterr().out)["dispatch_at_utc"] == (
        "2026-09-30T20:31:00+00:00"
    )
    assert not slept and not commands

    assert waker.main([], now=_at("2026-09-30T16:37:00Z")) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dispatched"] is True
    assert commands == [["gh", "workflow", "run", "v50r3_scheduler.yml", "--ref", "master"]]
    assert len(slept) == 1 and slept[0] >= 0.0
