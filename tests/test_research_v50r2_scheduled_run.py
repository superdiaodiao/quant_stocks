from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from scripts import research_v50r2_corrected_v47 as r2
from scripts import research_v50r2_scheduled_run as sched


def _signal(date: str) -> dict:
    return {"event_type": "SIGNAL_FROZEN", "payload": {"signal_date": date}}


def _mark(date: str) -> dict:
    return {"event_type": "VALUATION_APPENDED", "payload": {"as_of": date}}


def _at(text: str) -> datetime:
    return pd.Timestamp(text).to_pydatetime()


def test_signal_window_uses_official_close_in_edt_and_est() -> None:
    # September month-end: 16:00 EDT = 20:00 UTC.
    opens, closes = sched.signal_window(pd.Timestamp("2026-09-30"))
    assert opens == _at("2026-09-30T20:30:00Z")
    assert closes == _at("2026-09-30T23:59:59Z")
    # November month-end: 16:00 EST = 21:00 UTC.
    opens, closes = sched.signal_window(pd.Timestamp("2026-11-30"))
    assert opens == _at("2026-11-30T21:30:00Z")
    assert closes == _at("2026-11-30T23:59:59Z")
    # An early close is taken from the calendar, not a hard-coded 16:00.
    assert sched.session_close_utc(pd.Timestamp("2026-11-27")) == _at(
        "2026-11-27T18:00:00Z"
    )
    with pytest.raises(ValueError, match="not a month-end"):
        sched.signal_window(pd.Timestamp("2026-10-15"))


def test_month_end_sessions_follow_the_nasdaq_calendar() -> None:
    ends = sched.month_end_sessions(
        pd.Timestamp("2026-09-01"), pd.Timestamp("2026-12-31")
    )
    assert [f"{d:%Y-%m-%d}" for d in ends] == [
        "2026-09-30",
        "2026-10-30",
        "2026-11-30",
        "2026-12-31",
    ]


def test_scheduler_waits_before_the_first_prospective_signal_date() -> None:
    decision = sched.decide(_at("2026-09-05T12:00:00Z"), [])
    assert decision["action"] == "WAIT_FOR_FIRST_SIGNAL_DATE"
    assert decision["missed_signal_dates"] == ["2026-08-31"]
    assert decision["backfill_allowed"] is False


def test_scheduler_signal_lifecycle_on_the_september_month_end() -> None:
    # Before the close: nothing to do yet.
    early = sched.decide(_at("2026-09-30T19:00:00Z"), [])
    assert early["action"] == "WAIT_FOR_FIRST_SIGNAL_DATE"
    # Right at the close the buffer has not elapsed.
    at_close = sched.decide(_at("2026-09-30T20:10:00Z"), [])
    assert at_close["action"] == "WAIT_FOR_SIGNAL_WINDOW"
    # Inside the window.
    inside = sched.decide(_at("2026-09-30T20:45:00Z"), [])
    assert inside["action"] == "RUN_SIGNAL"
    assert inside["as_of"] == "2026-09-30"
    assert inside["signal_window_utc"] == {
        "opens": "2026-09-30T20:30:00+00:00",
        "closes": "2026-09-30T23:59:59+00:00",
    }
    # Last second of the UTC date is still legal.
    edge = sched.decide(_at("2026-09-30T23:59:59Z"), [])
    assert edge["action"] == "RUN_SIGNAL"
    # Already frozen: no duplicate signal, and no mark before a later session.
    done = sched.decide(_at("2026-09-30T22:00:00Z"), [_signal("2026-09-30")])
    assert done["action"] == "NO_ACTION"


def test_scheduler_reports_a_missed_window_and_never_backfills() -> None:
    missed = sched.decide(_at("2026-10-01T00:30:00Z"), [])
    assert missed["action"] == "SIGNAL_WINDOW_MISSED"
    assert missed["as_of"] is None
    assert missed["missed_signal_dates"] == ["2026-08-31", "2026-09-30"]
    # A week later the September window is still reported as missed, and the
    # October month-end is not yet due.
    later = sched.decide(_at("2026-10-08T12:00:00Z"), [])
    assert later["action"] == "SIGNAL_WINDOW_MISSED"
    assert later["due_signal_date"] == "2026-09-30"


def test_scheduler_marks_completed_sessions_after_a_frozen_signal() -> None:
    events = [_signal("2026-09-30")]
    # Execution session 2026-10-01 closes 20:00 UTC; buffer makes it 20:30.
    too_early = sched.decide(_at("2026-10-01T20:10:00Z"), events)
    assert too_early["action"] == "NO_ACTION"
    ready = sched.decide(_at("2026-10-01T20:45:00Z"), events)
    assert ready["action"] == "RUN_MARK"
    assert ready["as_of"] == "2026-10-01"
    # Once valued, the same session is not marked again.
    valued = events + [_mark("2026-10-01")]
    assert sched.decide(_at("2026-10-02T08:00:00Z"), valued)["action"] == "NO_ACTION"
    next_session = sched.decide(_at("2026-10-02T21:00:00Z"), valued)
    assert next_session["action"] == "RUN_MARK"
    assert next_session["as_of"] == "2026-10-02"
    # A Friday mark that was skipped is still due over the weekend; once it is
    # valued the weekend is idle.
    weekend = sched.decide(_at("2026-10-03T12:00:00Z"), valued)
    assert weekend["action"] == "RUN_MARK" and weekend["as_of"] == "2026-10-02"
    caught_up = valued + [_mark("2026-10-02")]
    assert sched.decide(_at("2026-10-03T12:00:00Z"), caught_up)["action"] == "NO_ACTION"
    assert sched.decide(_at("2026-10-04T12:00:00Z"), caught_up)["action"] == "NO_ACTION"


def test_scheduler_prefers_a_due_signal_over_a_mark_and_flags_missed_marks() -> None:
    events = [_signal("2026-09-30"), _mark("2026-10-29")]
    # October month-end window open: SIGNAL first.
    assert sched.decide(_at("2026-10-30T21:00:00Z"), events)["action"] == "RUN_SIGNAL"
    # October window missed: report it, but surface the pending mark.
    missed = sched.decide(_at("2026-11-02T12:00:00Z"), events)
    assert missed["action"] == "SIGNAL_WINDOW_MISSED"
    assert missed["pending_mark_as_of"] == "2026-10-30"


def test_run_executes_stage_then_freeze_for_a_due_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        r2,
        "stage_bundle",
        lambda **kwargs: calls.append(("stage", kwargs)) or {"status": "STAGED"},
    )
    monkeypatch.setattr(
        r2,
        "freeze_signal",
        lambda **kwargs: calls.append(("freeze", kwargs)) or {"status": "SIGNAL_FROZEN"},
    )
    monkeypatch.setattr(
        r2,
        "append_mark",
        lambda **kwargs: pytest.fail("mark must not run for a due signal"),
    )
    ledger = tmp_path / "ledger.jsonl"

    checked = sched.run(
        now=_at("2026-09-30T21:00:00Z"),
        execute=False,
        ledger_path=ledger,
        bundles_dir=tmp_path,
    )
    assert checked["action"] == "RUN_SIGNAL" and checked["executed"] is False
    assert calls == []

    executed = sched.run(
        now=_at("2026-09-30T21:00:00Z"),
        execute=True,
        ledger_path=ledger,
        bundles_dir=tmp_path,
    )
    assert executed["executed"] is True
    assert executed["result_status"] == "SIGNAL_FROZEN"
    assert [name for name, _ in calls] == ["stage", "freeze"]
    assert calls[0][1]["as_of"] == "2026-09-30"
    assert calls[0][1]["purpose"] == "SIGNAL"
    assert calls[1][1]["bundle"] == tmp_path / "2026-09-30_signal"


def test_run_never_stages_when_the_window_was_missed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        r2, "stage_bundle", lambda **_kwargs: pytest.fail("backfill attempted")
    )
    decision = sched.run(
        now=_at("2026-10-01T03:00:00Z"),
        execute=True,
        ledger_path=tmp_path / "ledger.jsonl",
        bundles_dir=tmp_path,
    )
    assert decision["action"] == "SIGNAL_WINDOW_MISSED"
    assert decision["executed"] is False


def test_cli_exit_code_is_nonzero_for_a_missed_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(sched.r2, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(sched.r2, "BUNDLES_DIR", tmp_path)

    assert sched.main(["check", "--now", "2026-09-30T21:00:00Z"]) == 0
    assert '"action": "RUN_SIGNAL"' in capsys.readouterr().out
    assert sched.main(["check", "--now", "2026-10-01T01:00:00Z"]) == 2
    assert '"SIGNAL_WINDOW_MISSED"' in capsys.readouterr().out
