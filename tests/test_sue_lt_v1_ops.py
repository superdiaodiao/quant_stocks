from datetime import datetime, timezone

import pandas as pd

from scripts import sue_lt_v1_notify as notify
from scripts import sue_lt_v1_watchdog as watchdog
from scripts import sue_lt_v1_window_waker as waker
from src.research import sue_live as live


def _utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def _frozen(tmp_path):
    path = tmp_path / "ledger.jsonl"
    live.append_event(path, "p", "PROTOCOL_FROZEN", {})
    return path


def test_a_check_that_cannot_be_read_is_reported_once_a_day():
    comments = notify.compose(None, None, None, None, "Traceback\nRuntimeError: code changed", [], "2026-11-03")
    assert [key for _, key in comments] == ["check-failed:2026-11-03"]


def test_quiet_when_not_ready_but_a_missed_month_is_reported_once():
    decision = {"action": "RUN_SIGNAL", "as_of": "2026-11-30", "missed": ["2026-10-30"]}
    comments = notify.compose("not_ready", "RUN_SIGNAL", decision, None, "retry later", [], "2026-12-01")
    assert [key for _, key in comments] == ["missed:2026-10-30"]
    assert notify.compose("not_ready", "RUN_MARK", {"missed": []}, None, "", [], "2026-12-01") == []


def test_mark_report_uses_the_protocol_points_and_flags_what_needs_a_look():
    events = [
        {"event_type": "VALUATION_APPENDED", "payload": {"as_of": "2026-11-03", "book": {"positions": {"A": 1000.0}}}},
        {"event_type": "VALUATION_APPENDED", "payload": {"as_of": "2026-11-04", "book": {"positions": {"A": 450.0}}}},
    ]
    inner = {"action": "MARKED", "sessions": ["2026-11-04"], "nav": 12_500.0, "qqq_nav": 15_000.0,
             "retired": [{"as_of": "2026-11-04", "ticker": "B", "value": 800.0, "missing_sessions": 10}],
             "terminal": {"as_of": "2026-11-04", "reason": "END_DATE", "outcome": "DID_NOT_BEAT_QQQ",
                          "nav": 12_500.0, "qqq_nav": 15_000.0, "points_behind_qqq": 25.0},
             "weights": {"A": 0.6}}
    comments = notify.compose("done", "RUN_MARK", {"missed": []}, {"as_of": "2026-11-04", "result": inner},
                              "", events, "2026-11-05")
    body = comments[0][0]
    assert "落后 25.0 点" in body and "B 连续 10 个开市日" in body and "A 2026-11-04 -55%" in body
    assert comments[1][1] == "terminal" and "没有跑赢" in comments[1][0]


def test_a_failure_is_keyed_by_action_session_and_error():
    comments = notify.compose("failed", "RUN_MARK", {"as_of": "2026-11-04", "missed": []},
                              {"as_of": "2026-11-04", "result": {}}, "x\nRuntimeError: boom", [], "2026-11-05")
    assert comments[0][1] == "failed:RUN_MARK:2026-11-04:RuntimeError: boom"


def test_waker_waits_for_today_s_month_end_window(tmp_path, monkeypatch):
    monkeypatch.setattr(waker.runner, "LEDGER", _frozen(tmp_path))
    events = live.read_ledger(waker.runner.LEDGER)
    session, opens = waker.due_signal_session(_utc("2026-10-30T15:00:00"), events)
    assert session == pd.Timestamp("2026-10-30") and opens == _utc("2026-10-30T20:30:00")
    assert waker.due_signal_session(_utc("2026-10-31T02:00:00"), events)[0] == pd.Timestamp("2026-10-30")
    assert waker.due_signal_session(_utc("2026-10-29T15:00:00"), events) is None


def test_watchdog_counts_sessions_behind_the_latest_valuation(tmp_path):
    path = _frozen(tmp_path)
    live.append_event(path, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30", "targets": ["A"]})
    events = live.read_ledger(path)
    # The execution session 11-02 and the next two have closed with no valuation.
    assert watchdog.sessions_behind(events, _utc("2026-11-05T03:00:00")) == (3, None)


def test_a_held_signal_is_reported_once_with_its_names():
    log = "RuntimeError: names near the pool have no 2026-11-30 close (AAA, BBB); retry later"
    comments = notify.compose("held", "RUN_SIGNAL", {"missed": []}, None, log, [], "2026-12-01")
    assert comments[0][1] == "held:2026-11-30:AAA,BBB" and "AAA, BBB" in comments[0][0]
    keys = {"held:2026-11-30:AAA,BBB", "missed:2026-10-30"}
    assert notify.still_held("2026-11-30", keys, lambda name: name == "AAA") == ["BBB"]
    assert notify.still_held("2026-12-31", keys, lambda name: False) == []


def test_the_end_is_reported_until_a_run_manages_to_post_it():
    terminal = {"as_of": "2028-10-31", "reason": "END_DATE", "outcome": "BEAT_QQQ",
                "nav": 15_100.0, "qqq_nav": 15_000.0, "points_behind_qqq": -1.0}
    decision = {"action": "NOTHING_DUE", "missed": [], "terminal": terminal}
    comments = notify.compose("none", "NOTHING_DUE", decision, None, "", [], "2028-11-02")
    assert [key for _, key in comments] == ["terminal"] and "期满" in comments[0][0]


def test_watchdog_fails_on_fresh_problems_not_on_an_old_missed_month():
    now = _utc("2027-01-10T12:00:00")
    assert not watchdog.needs_a_look({"missed": ["2026-10-30"], "sessions_behind": 0}, 2, now)
    assert watchdog.needs_a_look({"missed": ["2026-12-31"], "sessions_behind": 0}, 2, _utc("2027-01-03T12:00:00"))
    assert watchdog.needs_a_look({"missed": [], "sessions_behind": 3}, 0, now)
    assert watchdog.needs_a_look({"missed": []}, 1, now)
    assert watchdog.needs_a_look({"missed": [], "terminal_overdue": True}, 0, now)


def test_watchdog_flags_a_missing_end_record(tmp_path):
    path = _frozen(tmp_path)
    live.append_event(path, "p", "SIGNAL_FROZEN", {"signal_date": "2028-09-29", "targets": ["A"]})
    events = live.read_ledger(path)
    assert not watchdog.terminal_overdue(events, _utc("2028-11-01T02:00:00"))
    assert watchdog.terminal_overdue(events, _utc("2028-11-02T02:00:00"))
