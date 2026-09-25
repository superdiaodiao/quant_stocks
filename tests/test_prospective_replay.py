import numpy as np
import pandas as pd

from src.research import corrected_stock_policy as policy
from src.research.prospective_replay import replay_live

TICKERS = ["AAA1", "BBB1", "CCC1", "DDD1", "EEE1"]
VALIDATION = pd.DataFrame(columns=[
    "ticker", "split_date", "validation_status", "confirmed_adjustment_factor",
    "confirmed_action_date", "confirmed_action_type",
])
VALIDATION["split_date"] = pd.to_datetime(VALIDATION["split_date"])


def _panel(crash_days: tuple[str, str] | None = None) -> tuple[pd.DataFrame, pd.Series]:
    dates = pd.bdate_range("2024-01-02", "2024-04-30")
    path, value = [], 100.0
    for day in dates:
        if crash_days and pd.Timestamp(crash_days[0]) <= day <= pd.Timestamp(crash_days[1]):
            value *= 0.93
        elif path:
            value *= 1.001
        path.append(value)
    prices = pd.DataFrame(
        {ticker: np.array(path) * (1 + 0.01 * i) for i, ticker in enumerate(TICKERS)},
        index=dates,
    )
    return prices, pd.Series(np.linspace(100, 110, len(dates)), index=dates)


def _schedule(dates: pd.DatetimeIndex) -> pd.DataFrame:
    effective = [dates[dates.month == month][0] for month in (1, 2, 3, 4)]
    return pd.DataFrame([
        {"effective_date": day, "ticker": ticker, "target_weight": 0.2}
        for day in effective for ticker in TICKERS
    ])


def _replay(function, prices, index, schedule):
    return function(
        prices, index, schedule, prices.index[0], prices.index[-1],
        validation=VALIDATION, entry_loss_fraction=0.2,
        portfolio_stop_fraction=0.25, transaction_cost_bps=50.0,
    )


def test_live_replay_matches_the_development_replay_without_the_repaired_cases() -> None:
    prices, index = _panel()
    schedule = _schedule(prices.index)
    pd.testing.assert_frame_equal(
        _replay(replay_live, prices, index, schedule),
        _replay(policy.replay_with_sourced_hybrid_stop, prices, index, schedule),
    )


def test_a_portfolio_stop_re_enters_at_the_next_monthly_target() -> None:
    prices, index = _panel(("2024-01-16", "2024-01-22"))
    schedule = _schedule(prices.index)

    development = _replay(policy.replay_with_sourced_hybrid_stop, prices, index, schedule)
    live = _replay(replay_live, prices, index, schedule)

    assert live.loc["2024-01-22", "portfolio_stop_exits"] == 1
    # The development replay re-armed the stop in cash and skipped February.
    assert development.loc["2024-02-01", "holdings"] == 0
    assert bool(development.loc["2024-02-01", "coincident_stop_veto"])
    assert live.loc["2024-02-01", "holdings"] == 5
    assert not bool(live.loc["2024-02-01", "coincident_stop_veto"])


def test_a_target_without_an_execution_close_stays_in_cash() -> None:
    prices, index = _panel()
    prices.loc["2024-02-01", "EEE1"] = np.nan
    schedule = _schedule(prices.index)

    live = _replay(replay_live, prices, index, schedule)

    assert live.loc["2024-02-01", "holdings"] == 4
    assert abs(live.loc["2024-02-01", "invested"] - 0.8) < 0.01
    assert live.attrs["unexecutable_targets"] == [{"date": "2024-02-01", "ticker": "EEE1"}]
    assert live.loc["2024-03-01", "holdings"] == 5
