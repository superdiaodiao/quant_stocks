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


# ------------------------------------------------------------------ MARK timing


def _sessions(start, end):
    return list(runner.schedule.sessions_between(pd.Timestamp(start), pd.Timestamp(end)))


def _flat(days, value=10.0, growth=0.0):
    return pd.Series([value * (1 + growth) ** i for i in range(len(days))], index=pd.DatetimeIndex(days), dtype=float)


def _patch_market(monkeypatch, series, qqq_days, qqq_return=0.0):
    monkeypatch.setattr(runner, "_closes", lambda tickers, start, end: {
        t: series.get(t, pd.Series(dtype=float)).loc[start:end] for t in tickers})
    monkeypatch.setattr(runner, "_qqq_total_returns", lambda end: pd.Series(
        qqq_return, index=pd.DatetimeIndex(qqq_days)).loc[:end])


def test_mark_before_publication_records_nothing(ledger, monkeypatch):
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30", "targets": ["A"]})
    days = _sessions("2026-10-20", "2026-10-30")  # nothing published for 11-02 yet
    _patch_market(monkeypatch, {"A": _flat(days)}, days)
    before = ledger.read_text()
    with pytest.raises(RuntimeError, match="retry later"):
        runner.run_mark(pd.Timestamp("2026-11-02"), "p", live.read_ledger(ledger))
    assert ledger.read_text() == before


def test_mark_waits_for_a_late_holding_then_spans_its_gap(ledger, monkeypatch):
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30", "targets": ["A", "B"]})
    days = _sessions("2026-10-20", "2026-11-05")
    a = _flat(days, 10.0, 0.01)
    b = _flat(days, 20.0)
    _patch_market(monkeypatch, {"A": a, "B": b}, days)
    runner.run_mark(pd.Timestamp("2026-11-02"), "p", live.read_ledger(ledger))  # executes both
    # 11-03: QQQ is published but A's row is not yet.
    late = a.drop(pd.Timestamp("2026-11-03"))
    _patch_market(monkeypatch, {"A": late.loc[:"2026-11-03"], "B": b}, days)
    with pytest.raises(RuntimeError, match="no 2026-11-03 close yet for A"):
        runner.run_mark(pd.Timestamp("2026-11-03"), "p", live.read_ledger(ledger))
    # A day later A still has no 11-03 row (a halt): 11-03 counts as missing,
    # and the 11-04 valuation carries A's whole move since 11-02.
    _patch_market(monkeypatch, {"A": late, "B": b}, days)
    result = runner.run_mark(pd.Timestamp("2026-11-04"), "p", live.read_ledger(ledger))
    assert result["sessions"] == ["2026-11-03", "2026-11-04"]
    events = live.read_ledger(ledger)
    bought = events[2]["payload"]["book"]["positions"]["A"]
    marks = [e["payload"] for e in events if e["event_type"] == "VALUATION_APPENDED"]
    assert marks[-2]["book"]["missing_sessions"] == {"A": 1}
    assert marks[-1]["book"]["positions"]["A"] == pytest.approx(
        bought * a[pd.Timestamp("2026-11-04")] / a[pd.Timestamp("2026-11-02")])
    assert marks[-1]["book"]["missing_sessions"] == {}


def test_every_pending_signal_executes_on_its_own_session(ledger, monkeypatch):
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30", "targets": ["A"]})
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2026-11-30", "targets": ["B"]})
    days = _sessions("2026-10-20", "2026-12-02")
    _patch_market(monkeypatch, {"A": _flat(days, 10.0, 0.001), "B": _flat(days, 20.0)}, days)
    result = runner.run_mark(pd.Timestamp("2026-12-02"), "p", live.read_ledger(ledger))
    assert [(t["signal_date"], t["execution_date"], t["bought"], t["sold"]) for t in result["trades"]] == [
        ("2026-10-30", "2026-11-02", ["A"], []), ("2026-11-30", "2026-12-01", ["B"], ["A"])]
    assert set(live.read_ledger(ledger)[-1]["payload"]["book"]["positions"]) == {"B"}


def test_valuation_past_an_unexecuted_signal_is_refused(ledger):
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30", "targets": ["A"]})
    live.append_event(ledger, "p", "VALUATION_APPENDED", {"as_of": "2026-10-30"})
    with pytest.raises(RuntimeError, match="never executed"):
        live.append_event(ledger, "p", "VALUATION_APPENDED", {"as_of": "2026-11-02"})


def test_a_session_the_market_did_not_trade_is_skipped(ledger, monkeypatch):
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30", "targets": ["A"]})
    days = _sessions("2026-10-20", "2026-11-05")
    closed = pd.Timestamp("2026-11-03")  # as if closed without notice
    traded = [d for d in days if d != closed]
    _patch_market(monkeypatch, {"A": _flat(traded, 10.0, 0.01)}, traded)
    result = runner.run_mark(pd.Timestamp("2026-11-05"), "p", live.read_ledger(ledger))
    assert result["skipped_calendar_sessions"] == ["2026-11-03"]
    assert result["sessions"] == ["2026-11-02", "2026-11-04", "2026-11-05"]
    assert live.read_ledger(ledger)[-1]["payload"]["book"]["missing_sessions"] == {}


def test_signal_after_a_closed_execution_day_trades_the_next_session(ledger, monkeypatch):
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30", "targets": ["A"]})
    days = [d for d in _sessions("2026-10-20", "2026-11-04") if d != pd.Timestamp("2026-11-02")]
    _patch_market(monkeypatch, {"A": _flat(days)}, days)
    result = runner.run_mark(pd.Timestamp("2026-11-04"), "p", live.read_ledger(ledger))
    assert result["trades"][0]["execution_date"] == "2026-11-03"


# ------------------------------------------------------------------ evaluation


def test_falling_far_behind_does_not_end_the_observation(ledger, monkeypatch):
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30", "targets": ["A"]})
    days = _sessions("2026-10-20", "2026-11-10")
    prices = _flat(days, 10.0)
    prices.loc["2026-11-05":] = 6.0  # the account falls 40% while QQQ is flat
    _patch_market(monkeypatch, {"A": prices}, days)
    result = runner.run_mark(pd.Timestamp("2026-11-10"), "p", live.read_ledger(ledger))
    assert result["terminal"] is None and result["sessions"][-1] == "2026-11-10"
    assert result["points_behind_qqq"] > 39
    assert runner.decide(_utc("2026-11-30T23:00:00"), live.read_ledger(ledger))["action"] == "RUN_SIGNAL"


def test_the_terminal_record_ends_the_ledger(ledger):
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2028-09-29", "targets": ["A"]})
    live.append_event(ledger, "p", "TRADES_EXECUTED", {"signal_date": "2028-09-29", "execution_date": "2028-10-02"})
    live.append_event(ledger, "p", "VALUATION_APPENDED", {"as_of": "2028-10-31"})
    live.append_event(ledger, "p", "TERMINAL_RECORDED", {"as_of": "2028-10-31"})
    assert runner.decide(_utc("2028-11-02T02:00:00"), live.read_ledger(ledger))["action"] == "NOTHING_DUE"
    with pytest.raises(RuntimeError, match="terminal"):
        live.append_event(ledger, "p", "VALUATION_APPENDED", {"as_of": "2028-11-01"})


def test_points_behind_is_reported_in_points_of_the_starting_cash():
    assert live.points_behind(12_400.0, 15_000.0) == pytest.approx(26.0)
    assert live.terminal_record(pd.Timestamp("2027-06-01"), 5_000.0, 15_000.0) is None
    end = live.terminal_record(live.END_DATE, 15_100.0, 15_000.0)
    assert end["reason"] == "END_DATE" and end["outcome"] == "BEAT_QQQ"
    assert live.terminal_record(pd.Timestamp("2028-10-30"), 1.0, 2.0, final=True)["outcome"] == "DID_NOT_BEAT_QQQ"


def test_no_signal_on_or_after_the_end_date(ledger):
    events = live.read_ledger(ledger)
    assert runner.decide(_utc("2028-09-30T02:00:00"), events)["as_of"] == "2028-09-29"
    decision = runner.decide(_utc("2028-11-01T02:00:00"), events)
    assert decision["action"] == "NOTHING_DUE" and "2028-10-31" not in decision["missed"]
    with pytest.raises(RuntimeError, match="end date"):
        live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2028-10-31", "targets": []})


# ------------------------------------------------------------------ SIGNAL inputs


def _panel(names, days=300):
    dates = pd.bdate_range("2025-01-01", periods=days)
    close = pd.DataFrame(50.0, index=dates, columns=names)
    volume = pd.DataFrame({n: 1e6 * (i + 1) for i, n in enumerate(names)}, index=dates)
    return close, close * volume


def test_price_gate_waits_for_a_late_name_near_the_pool(monkeypatch):
    monkeypatch.setattr(live, "POOL", 3)
    names = ["N0", "N1", "N2", "N3", "N4", "N5"]
    close, dollar_volume = _panel(names)
    as_of = close.index[-1]
    close.loc[as_of, "N5"] = float("nan")  # most liquid, traded yesterday, no close yet
    close.loc[close.index[-2:], "N4"] = float("nan")  # no close for two sessions: halted
    gate = runner.price_gate(close, dollar_volume, set(names), as_of,
                             {"GONE": 9e9, "SMALL": 1.0, "NEW": None})
    assert gate["unpriced"] == ["N5"] and gate["halted"] == ["N4"]
    assert gate["failed_near_pool"] == ["GONE"]


def test_sue_gaps_say_why_a_pool_name_has_no_score():
    quarterly = pd.DataFrame({
        "ticker": ["OLD", "FEW"], "metric": "net_income", "value": 1.0,
        "fiscal_end": pd.to_datetime(["2026-03-31", "2026-06-30"]),
        "available_date": pd.to_datetime(["2026-05-05", "2026-08-05"]),
    })
    gaps = runner.sue_gaps(["HAS", "NOCIK", "FAIL", "NONE", "OLD", "FEW"], {"HAS": 1.0}, quarterly,
                           pd.Timestamp("2026-08-31"), pd.Timestamp("2026-05-29"),
                           {t: 1 for t in ["HAS", "FAIL", "NONE", "OLD", "FEW"]}, {"FAIL": "HTTP 503"})
    assert {t: g["reason"] for t, g in gaps.items()} == {
        "NOCIK": "no_sec_cik", "FAIL": "sec_refresh_failed", "NONE": "no_quarterly_net_income",
        "OLD": "latest_quarter_before_window", "FEW": "too_few_year_over_year_changes"}


def test_sec_refresh_writes_only_inside_its_work_directory(tmp_path, monkeypatch):
    seen = {}

    def fake_update(**kwargs):
        module = runner.fundamentals_update
        seen.update({name: str(getattr(module, name)) for name in (
            "FUNDAMENTALS_REFRESH_STATE_FILE", "FUNDAMENTALS_COVERAGE_FILE",
            "QUARTERLY_FUNDAMENTALS_COVERAGE_FILE", "VALIDATED_FOREIGN_QUARTERLY_FILE",
            "NASDAQ_300M_STOCK_LIST_FILE")})
        seen["map"] = module.fetch_sec_ticker_map()
        seen["cache"] = str(kwargs["cache_dir"])
        return {"failures": [{"ticker": "b", "reason": "no_sec_fundamentals"}]}

    original = runner.fundamentals_update.FUNDAMENTALS_REFRESH_STATE_FILE
    monkeypatch.setattr(runner.fundamentals_update, "update_fundamentals", fake_update)
    failures = runner.refresh_sec(pd.Timestamp("2026-08-31"), tmp_path, tmp_path / "universe.csv",
                                  ["A", "B"], {"A": 1, "B": 2})
    assert failures == {"B": "no_sec_fundamentals"}
    assert all(value.startswith(str(tmp_path)) for key, value in seen.items() if key != "map")
    assert seen["map"] == {"A": 1, "B": 2}
    assert runner.fundamentals_update.FUNDAMENTALS_REFRESH_STATE_FILE == original


def test_prices_are_downloaded_fresh_not_spliced_onto_stored_rows(tmp_path, monkeypatch):
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    (price_dir / "spcx.csv").write_text("date,close,volume\n2025-04-10,24.0,150\n")
    requested = []

    def fake_fetch(symbol, start, end, **kwargs):
        requested.append((symbol, start))
        return pd.DataFrame({"date": pd.to_datetime(["2026-06-12", "2026-06-15"]), "open": 1.0, "high": 1.0,
                             "low": 1.0, "close": [160.95, 150.0], "volume": [5e8, 4e8]})

    monkeypatch.setattr(runner.nasdaq_update, "fetch_history", fake_fetch)
    report = runner.download_prices(["SPCX"], pd.Timestamp("2026-06-15"), price_dir, 1)
    stored = pd.read_csv(price_dir / "spcx.csv")
    assert list(stored["date"]) == ["2026-06-12", "2026-06-15"] and report["failed"] == {}
    assert requested[0][1] == (pd.Timestamp("2026-06-15") - pd.Timedelta(days=runner.DOWNLOAD_DAYS)).date()


def test_minimum_buy_leaves_dust_as_cash():
    book = {**live.new_book(), "cash": 0.5}
    book, orders = live.execute(book, ["A"], {"A": 10.0})
    assert orders == [] and book["positions"] == {} and book["cash"] == 0.5


# ------------------------------------------------------------------ after review of the fixes


def _signal_and_hold(ledger, monkeypatch, days, series):
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2028-09-29", "targets": sorted(series)})
    _patch_market(monkeypatch, series, days)
    runner.run_mark(pd.Timestamp("2028-10-30"), "p", live.read_ledger(ledger), now=_utc("2028-10-31T02:00:00"))


def test_final_mark_stops_waiting_once_a_later_session_has_closed(ledger, monkeypatch):
    days = _sessions("2028-09-15", "2028-11-02")
    a, b = _flat(days, 10.0), _flat(days, 20.0)
    _signal_and_hold(ledger, monkeypatch, days, {"A": a, "B": b})
    halted = b.drop(pd.Timestamp("2028-10-31"))  # no END_DATE close for B
    _patch_market(monkeypatch, {"A": a, "B": halted}, days)
    events = live.read_ledger(ledger)
    with pytest.raises(RuntimeError, match="retry later"):
        runner.run_mark(live.END_DATE, "p", events, now=_utc("2028-11-01T02:00:00"))
    # After 11-01 closes, the END_DATE rows are final: B counts as missing.
    decision = runner.decide(_utc("2028-11-02T02:00:00"), events)
    assert (decision["action"], decision["as_of"]) == ("RUN_MARK", "2028-10-31")
    result = runner.run_mark(live.END_DATE, "p", events, now=_utc("2028-11-02T02:00:00"))
    assert result["sessions"] == ["2028-10-31"] and result["terminal"]["reason"] == "END_DATE"
    assert runner.decide(_utc("2028-11-03T02:00:00"), live.read_ledger(ledger))["action"] == "NOTHING_DUE"


def test_an_unscheduled_closure_on_the_end_date_ends_on_the_session_before(ledger, monkeypatch):
    days = _sessions("2028-09-15", "2028-11-02")
    traded = [d for d in days if d != live.END_DATE]
    a = _flat(traded, 10.0)
    _signal_and_hold(ledger, monkeypatch, traded, {"A": a})
    events = live.read_ledger(ledger)
    assert events[-1]["payload"]["as_of"] == "2028-10-30"
    result = runner.run_mark(live.END_DATE, "p", events, now=_utc("2028-11-02T02:00:00"))
    assert result["sessions"] == [] and result["terminal"]["as_of"] == "2028-10-30"
    assert live.read_ledger(ledger)[-1]["event_type"] == "TERMINAL_RECORDED"


def test_an_empty_response_for_a_holding_waits_instead_of_counting_a_halt(ledger, monkeypatch):
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30", "targets": ["A", "B"]})
    days = _sessions("2026-10-20", "2026-11-05")
    a, b = _flat(days, 10.0), _flat(days, 20.0)
    _patch_market(monkeypatch, {"A": a, "B": b}, days)
    runner.run_mark(pd.Timestamp("2026-11-02"), "p", live.read_ledger(ledger))
    _patch_market(monkeypatch, {"A": a}, days)  # B's history comes back empty
    with pytest.raises(RuntimeError, match="no 2026-11-03 close yet for B"):
        runner.run_mark(pd.Timestamp("2026-11-03"), "p", live.read_ledger(ledger))


def test_an_empty_response_for_a_target_on_its_execution_day_waits(ledger, monkeypatch):
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30", "targets": ["A", "B"]})
    days = _sessions("2026-10-20", "2026-11-05")
    _patch_market(monkeypatch, {"A": _flat(days)}, days)
    with pytest.raises(RuntimeError, match="no 2026-11-02 close yet for B"):
        runner.run_mark(pd.Timestamp("2026-11-02"), "p", live.read_ledger(ledger))


def test_a_split_sized_move_waits_one_day_then_is_booked(ledger, monkeypatch):
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30", "targets": ["A"]})
    days = _sessions("2026-10-20", "2026-11-05")
    a = _flat(days, 10.0)
    _patch_market(monkeypatch, {"A": a}, days)
    runner.run_mark(pd.Timestamp("2026-11-02"), "p", live.read_ledger(ledger))
    moved = a.copy()
    moved.loc["2026-11-03":] = 20.0  # a 2:1 reverse split the provider has not back-adjusted
    _patch_market(monkeypatch, {"A": moved}, days)
    with pytest.raises(RuntimeError, match="split-like move"):
        runner.run_mark(pd.Timestamp("2026-11-03"), "p", live.read_ledger(ledger))
    result = runner.run_mark(pd.Timestamp("2026-11-04"), "p", live.read_ledger(ledger))
    assert result["sessions"] == ["2026-11-03", "2026-11-04"]


def test_price_gate_waits_for_a_failed_holding_or_last_pool_name(monkeypatch):
    monkeypatch.setattr(live, "POOL", 3)
    names = ["N0", "N1", "N2", "N3"]
    close, dollar_volume = _panel(names)
    gate = runner.price_gate(close, dollar_volume, set(names), close.index[-1],
                             {"HELD": None, "LAST": None, "OTHER": None}, {"HELD", "LAST"})
    assert gate["failed_near_pool"] == ["HELD", "LAST"]


def test_empty_downloads_get_the_short_retries_only(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(symbol, start, end, **kwargs):
        calls.append(symbol)
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    monkeypatch.setattr(runner.nasdaq_update, "fetch_history", fake_fetch)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    report = runner.download_prices(["NEW"], pd.Timestamp("2026-08-31"), tmp_path / "prices", 1)
    assert report["no_data"] == ["NEW"] and len(calls) == 1 + len(runner.nasdaq_update.FINAL_RETRY_PAUSES_SECONDS)


def test_sec_requests_that_were_refused_are_asked_again(tmp_path, monkeypatch):
    rounds = [{"A": "HTTP Error 503", "B": "no_sec_fundamentals"}, {}]
    asked = []

    def fake_refresh(as_of, work, universe_path, tickers, ticker_map):
        asked.append(list(tickers))
        return rounds.pop(0)

    monkeypatch.setattr(runner, "refresh_sec", fake_refresh)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    failures = runner.refresh_sec_with_retries(pd.Timestamp("2026-08-31"), tmp_path, tmp_path / "u.csv",
                                               ["A", "B", "C"], {})
    assert asked == [["A", "B", "C"], ["A"]] and failures == {"B": "no_sec_fundamentals"}


# ------------------------------------------------------------------ second review


def test_an_unpriced_target_waits_one_day_then_is_left_unbought(ledger, monkeypatch):
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30", "targets": ["A", "T"]})
    days = _sessions("2026-10-20", "2026-11-05")
    t = _flat(days, 20.0).loc[:"2026-10-30"]  # T stops trading after the signal day
    _patch_market(monkeypatch, {"A": _flat(days), "T": t}, days)
    with pytest.raises(RuntimeError, match="close yet for T"):
        runner.run_mark(pd.Timestamp("2026-11-02"), "p", live.read_ledger(ledger))
    result = runner.run_mark(pd.Timestamp("2026-11-03"), "p", live.read_ledger(ledger))
    assert result["trades"][0]["bought"] == ["A"] and result["sessions"] == ["2026-11-02", "2026-11-03"]


def test_a_split_sized_move_waits_even_in_a_run_over_two_sessions(ledger, monkeypatch):
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30", "targets": ["A", "B"]})
    days = _sessions("2026-10-20", "2026-11-06")
    a, b = _flat(days, 10.0), _flat(days, 20.0)
    _patch_market(monkeypatch, {"A": a, "B": b}, days)
    runner.run_mark(pd.Timestamp("2026-11-02"), "p", live.read_ledger(ledger))
    moved = a.copy()
    moved.loc["2026-11-04":] = 20.0
    _patch_market(monkeypatch, {"A": moved, "B": b}, days)
    with pytest.raises(RuntimeError, match="split-like move on 2026-11-04 for A"):
        runner.run_mark(pd.Timestamp("2026-11-04"), "p", live.read_ledger(ledger))


def test_after_the_end_date_the_gate_looks_at_the_latest_session(ledger):
    live.append_event(ledger, "p", "SIGNAL_FROZEN", {"signal_date": "2028-09-29", "targets": ["A"]})
    live.append_event(ledger, "p", "TRADES_EXECUTED", {"signal_date": "2028-09-29", "execution_date": "2028-10-02"})
    live.append_event(ledger, "p", "VALUATION_APPENDED", {"as_of": "2028-10-30"})
    decision = runner.decide(_utc("2028-11-02T02:00:00"), live.read_ledger(ledger))
    assert (decision["as_of"], decision["gate_session"]) == ("2028-10-31", "2028-11-01")
    assert "gate_session" not in runner.decide(_utc("2028-11-01T02:00:00"), live.read_ledger(ledger))


def test_a_settled_final_mark_still_waits_for_proof_that_the_end_date_did_not_trade(ledger, monkeypatch):
    days = _sessions("2028-09-15", "2028-10-30")  # nothing published after 10-30 yet
    _signal_and_hold(ledger, monkeypatch, days, {"A": _flat(days)})
    with pytest.raises(RuntimeError, match="retry later"):
        runner.run_mark(live.END_DATE, "p", live.read_ledger(ledger), now=_utc("2028-11-02T02:00:00"))


def test_the_sec_ticker_map_is_asked_again_before_the_downloads(monkeypatch):
    answers = [OSError("timed out"), {"aapl": 320193}]

    def fake_map():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(runner.fundamentals_update, "fetch_sec_ticker_map", fake_map)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    assert runner.sec_ticker_map() == {"AAPL": 320193}
    monkeypatch.setattr(runner.fundamentals_update, "fetch_sec_ticker_map",
                        lambda: (_ for _ in ()).throw(OSError("refused")))
    with pytest.raises(RuntimeError, match="retry later"):
        runner.sec_ticker_map()


def test_a_cik_with_404_in_it_is_still_retried():
    assert not runner._permanent_sec_failure("CIK 1415404 (ECHO): CIK 1415404: HTTP Error 503: Service Unavailable")
    assert runner._permanent_sec_failure("CIK 937966 (ASML): HTTP Error 404: Not Found")
    assert runner._permanent_sec_failure("no_sec_fundamentals")
