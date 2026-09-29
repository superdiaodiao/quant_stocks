from datetime import datetime, timezone

import pandas as pd
import pytest

from scripts import sue_lt_v1 as runner
from src.research import sue_live as live


def _utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(runner, "LEDGER", path)
    live.append_event(path, "p", "PROTOCOL_FROZEN", {})
    return path


def test_decide_signal_window_mark_and_missed(ledger):
    events = live.read_ledger(ledger)
    # Before the first month end closes: nothing.
    assert runner.decide(_utc("2026-10-30T18:00:00"), events)["action"] == "NOTHING_DUE"
    # Inside the 2026-10-30 window (closes 2026-11-02 pre-market, 09:00 UTC).
    assert runner.decide(_utc("2026-10-31T02:00:00"), events) == {
        "action": "RUN_SIGNAL", "as_of": "2026-10-30", "missed": []}
    # Window closed without a signal: missed, nothing to mark.
    late = runner.decide(_utc("2026-11-03T02:00:00"), events)
    assert late["action"] == "NOTHING_DUE" and late["missed"] == ["2026-10-30"]
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30", "targets": ["A"]})
    events = live.read_ledger(ledger)
    # After the execution session (2026-11-02) closes: mark, which executes.
    assert runner.decide(_utc("2026-11-03T02:00:00"), events)["action"] == "RUN_MARK"
    assert runner.decide(_utc("2026-11-02T18:00:00"), events)["action"] == "NOTHING_DUE"


def test_mark_executes_then_values_with_fresh_returns(ledger, monkeypatch):
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30", "targets": ["A", "B"]})
    days = pd.to_datetime(["2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04"])
    series = {"A": pd.Series([9, 10, 10, 11, 11], index=days, dtype=float),
              "B": pd.Series([19, 20, 20, 20, 18], index=days, dtype=float)}
    monkeypatch.setattr(runner, "_closes", lambda tickers, start, end: {t: series[t] for t in tickers})
    monkeypatch.setattr(runner, "_qqq_total_returns", lambda end: pd.Series(0.01, index=days))
    result = runner.run_mark(pd.Timestamp("2026-11-04"), "p", live.read_ledger(ledger))
    assert result["sessions"] == ["2026-11-02", "2026-11-03", "2026-11-04"]
    events = live.read_ledger(ledger)
    kinds = [e["event_type"] for e in events]
    assert kinds == ["PROTOCOL_FROZEN", "SIGNAL_FROZEN", "TRADES_EXECUTED",
                     "VALUATION_APPENDED", "VALUATION_APPENDED", "VALUATION_APPENDED"]
    trade = events[2]["payload"]
    invested = {o["ticker"]: o["notional"] for o in trade["orders"]}
    marks = [e["payload"] for e in events if e["event_type"] == "VALUATION_APPENDED"]
    # QQQ starts at the execution close; the book moves by each name's own return.
    assert marks[0]["qqq_nav"] == pytest.approx(live.START_CASH)
    assert marks[-1]["qqq_nav"] == pytest.approx(live.START_CASH * 1.01 ** 2)
    expected = invested["A"] * 1.1 + invested["B"] * 0.9
    assert marks[-1]["nav"] == pytest.approx(expected)
    # A second mark with nothing new is a no-op.
    assert runner.run_mark(pd.Timestamp("2026-11-04"), "p", events) == {"action": "NOTHING_DUE"}


def test_writes_refuse_other_branches(monkeypatch):
    monkeypatch.setattr(runner, "_branch", lambda: "master")
    with pytest.raises(RuntimeError):
        runner._require_live_branch()
