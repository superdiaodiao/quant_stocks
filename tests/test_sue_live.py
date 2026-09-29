import numpy as np
import pandas as pd
import pytest

from scripts import research_sue_low_turnover as backtest
from src.research import sue_live as live


def _market(seed=3, days=90, names=12):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2026-01-02", periods=days)
    returns = rng.normal(0.0005, 0.02, size=(days, names))
    close = pd.DataFrame(50 * np.cumprod(1 + returns, axis=0), index=dates,
                         columns=[f"T{i}" for i in range(names)])
    return close


def _schedule(close):
    months = close.groupby(close.index.to_period("M")).tail(1).index[:-1]
    rows = []
    for number, signal in enumerate(months):
        effective = close.index[close.index.get_loc(signal) + 1]
        for ticker in [f"T{(number * 3 + k) % 12}" for k in range(live.HOLDINGS if live.HOLDINGS < 12 else 10)]:
            rows.append({"effective_date": effective, "ticker": ticker})
    return pd.DataFrame(rows)


def test_live_book_matches_the_backtest_replay():
    close = _market()
    schedule = _schedule(close)
    book_frame = backtest.simulate(close, close, schedule, {}, str(close.index[-1].date()), "ibkr_tiered")
    wanted = {day: list(group["ticker"]) for day, group in schedule.groupby("effective_date")}
    book = live.new_book()
    navs = {}
    previous = None
    for day in close.loc[schedule["effective_date"].min():].index:
        if previous is not None:
            returns = {t: float(close.at[day, t] / close.at[previous, t] - 1) for t in book["positions"]}
            book = live.roll_forward(book, returns, day)
        if day in wanted:
            book, _orders = live.execute(book, wanted[day], {t: float(close.at[day, t]) for t in close.columns})
        navs[day] = live.nav(book)
        previous = day
    ours = pd.Series(navs)
    assert np.allclose(ours.values, book_frame["nav"].reindex(ours.index).values, rtol=1e-12, atol=1e-9)


def test_stale_position_retires_to_cash_at_last_value():
    book = live.new_book()
    book, _ = live.execute(book, ["A"], {"A": 10.0})
    held = book["positions"]["A"]
    for day in pd.bdate_range("2026-02-02", periods=live.STALE_SESSIONS):
        book = live.roll_forward(book, {"A": None}, day)
    book, retired = live.retire_stale(book)
    assert retired and retired[0]["ticker"] == "A" and book["positions"] == {}
    assert book["cash"] == pytest.approx(live.START_CASH - (live.START_CASH - held))


def test_unpriced_seller_is_kept_until_it_trades():
    book = live.new_book()
    book, _ = live.execute(book, ["A", "B"], {"A": 10.0, "B": 20.0})
    book, orders = live.execute(book, ["B"], {"B": 20.0})
    assert "A" in book["positions"] and orders == []


def test_ledger_rules(tmp_path):
    path = tmp_path / "ledger.jsonl"
    with pytest.raises(RuntimeError):
        live.append_event(path, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30"})
    path.unlink(missing_ok=True)
    live.append_event(path, "p", "PROTOCOL_FROZEN", {})
    with pytest.raises(RuntimeError):
        live.append_event(path, "p", "SIGNAL_FROZEN", {"signal_date": "2026-09-30"})
    live.append_event(path, "p", "SIGNAL_FROZEN", {"signal_date": "2026-10-30"})
    with pytest.raises(RuntimeError):
        live.append_event(path, "p", "TRADES_EXECUTED", {"signal_date": "2026-10-30", "execution_date": "2026-10-30"})
    live.append_event(path, "p", "TRADES_EXECUTED", {"signal_date": "2026-10-30", "execution_date": "2026-11-02"})
    live.append_event(path, "p", "VALUATION_APPENDED", {"as_of": "2026-11-02"})
    with pytest.raises(RuntimeError):
        live.append_event(path, "p", "VALUATION_APPENDED", {"as_of": "2026-11-02"})
    with pytest.raises(RuntimeError):
        live.append_event(path, "other", "VALUATION_APPENDED", {"as_of": "2026-11-03"})
    events = live.read_ledger(path)
    assert [e["event_type"] for e in events] == ["PROTOCOL_FROZEN", "SIGNAL_FROZEN", "TRADES_EXECUTED", "VALUATION_APPENDED"]
    lines = path.read_text().splitlines()
    lines[1] = lines[1].replace("2026-10-30", "2026-10-31")
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(RuntimeError):
        live.read_ledger(path)


def test_pool_uses_signal_day_price_and_history():
    dates = pd.bdate_range("2025-01-01", periods=300)
    close = pd.DataFrame({"BIG": 100.0, "CHEAP": 4.0, "NEW": 100.0}, index=dates)
    close.loc[dates[:100], "NEW"] = np.nan
    dollar_volume = pd.DataFrame({"BIG": 1e8, "CHEAP": 4e9, "NEW": 1e11}, index=dates)
    assert live.liquidity_pool(close, dollar_volume, {"BIG", "CHEAP", "NEW"}, dates[-1]) == ["BIG"]
