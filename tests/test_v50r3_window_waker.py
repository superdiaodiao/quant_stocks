from __future__ import annotations

from datetime import datetime, timedelta
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


def _due(text: str, events: list[dict]) -> tuple[str, str] | None:
    due = waker.due_signal_session(_at(text), events, FIRST, MISSED)
    if due is None:
        return None
    session, opens = due
    return f"{session:%Y-%m-%d}", opens.isoformat(timespec="minutes")


def test_a_month_end_signal_is_due_from_its_window_until_it_closes() -> None:
    assert _due("2026-09-30T15:07:00Z", []) == ("2026-09-30", "2026-09-30T20:30+00:00")
    assert _due("2026-09-30T21:40:00Z", []) == ("2026-09-30", "2026-09-30T20:30+00:00")
    assert _due("2026-10-01T07:30:00Z", []) == ("2026-09-30", "2026-09-30T20:30+00:00")
    # A frozen signal, or any other day, needs no wake-up.
    assert _due("2026-09-30T15:07:00Z", [_signal("2026-09-30")]) is None
    assert _due("2026-10-29T15:07:00Z", [_signal("2026-09-30")]) is None
    # The winter window opens an hour later.
    assert _due("2026-11-30T15:07:00Z", [_signal("2026-10-30")]) == (
        "2026-11-30", "2026-11-30T21:30+00:00",
    )


def test_a_missed_month_is_due_in_each_later_window_until_caught_up() -> None:
    assert _due("2026-10-01T15:07:00Z", []) == ("2026-10-01", "2026-10-01T20:30+00:00")
    assert _due("2026-10-02T16:37:00Z", []) == ("2026-10-02", "2026-10-02T20:30+00:00")
    # Friday's window stays open until Monday's pre-market.
    assert _due("2026-10-03T16:37:00Z", []) == ("2026-10-02", "2026-10-02T20:30+00:00")
    caught_up = [_signal("2026-10-01"), _mark("2026-10-02")]
    assert _due("2026-10-05T16:37:00Z", caught_up) is None


PUBLISHED = _at("2026-10-01T01:00:00Z")


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(waker.r3, "_validated_protocol", lambda _path: ({
        "signal_policy": {
            "first_prospective_signal_date": "2026-09-30",
            "missed_signal_dates": ["2026-08-31"],
        },
    }, "0" * 64))
    monkeypatch.setattr(waker.v43, "read_ledger", lambda _path: [])
    state: dict = {"now": None, "checks": [], "commands": []}

    def sources_ready(session):
        state["checks"].append(state["now"])
        return {"session": f"{session:%Y-%m-%d}", "ready": state["now"] >= PUBLISHED}

    def sleep(seconds):
        state["now"] += timedelta(seconds=seconds)

    monkeypatch.setattr(waker, "_utc_now", lambda: state["now"])
    monkeypatch.setattr(waker.sources, "sources_ready", sources_ready)
    monkeypatch.setattr(waker.time, "sleep", sleep)
    monkeypatch.setattr(
        waker.subprocess, "run", lambda command, check: state["commands"].append(command)
    )
    return state


def _run(cli, capsys, argv, start) -> dict:
    cli["now"], cli["checks"] = _at(start), []
    assert waker.main(argv, now=_at(start)) == 0
    return json.loads(capsys.readouterr().out)


def test_the_waker_waits_for_the_published_session_then_dispatches(
    cli, capsys: pytest.CaptureFixture
) -> None:
    # From 19:07 it checks every ten minutes from 20:31 until its time runs
    # out at 00:37, before Nasdaq publishes the session.
    report = _run(cli, capsys, [], "2026-09-30T19:07:00Z")
    assert report["dispatched"] is False and "later run" in report["reason"]
    assert report["session"] == "2026-09-30"
    assert cli["checks"][0] == _at("2026-09-30T20:31:00Z")
    assert cli["checks"][-1] == _at("2026-10-01T00:31:00Z")
    assert not cli["commands"]

    # A later run finds it published and starts the scheduler at once.
    report = _run(cli, capsys, [], "2026-09-30T21:07:00Z")
    assert report["dispatched"] is True
    assert cli["checks"][-1] == _at("2026-10-01T01:07:00Z")
    assert cli["commands"] == [
        ["gh", "workflow", "run", "v50r3_scheduler.yml", "--ref", "master"]
    ]


def test_the_waker_checks_once_on_a_dry_run_and_skips_other_days(
    cli, capsys: pytest.CaptureFixture
) -> None:
    report = _run(cli, capsys, ["--dry-run"], "2026-09-30T16:37:00Z")
    assert report["reason"] == "dry run" and not cli["checks"]
    report = _run(cli, capsys, ["--dry-run"], "2026-09-30T22:00:00Z")
    assert report["sources"]["ready"] is False and len(cli["checks"]) == 1
    report = _run(cli, capsys, [], "2026-09-29T19:07:00Z")
    assert "no SIGNAL is due" in report["reason"] and not cli["checks"]
    assert not cli["commands"]
