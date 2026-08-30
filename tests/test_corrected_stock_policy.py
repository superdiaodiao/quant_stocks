import pandas as pd
import pytest

from src.research.corrected_stock_policy import (
    corrected_price_views,
    replay_with_sourced_hybrid_stop,
    technical_ranking,
)


def _validation(rows=()):
    columns = [
        "ticker",
        "split_date",
        "validation_status",
        "confirmed_action_date",
        "confirmed_action_type",
        "confirmed_adjustment_factor",
    ]
    return pd.DataFrame(list(rows), columns=columns).assign(
        split_date=lambda frame: pd.to_datetime(frame["split_date"]),
        confirmed_action_date=lambda frame: pd.to_datetime(
            frame["confirmed_action_date"]
        ),
    )


def _targets(date, tickers):
    return pd.DataFrame(
        {
            "effective_date": [date] * len(tickers),
            "ticker": tickers,
            "target_weight": [1.0 / len(tickers)] * len(tickers),
        }
    )


def test_sourced_price_view_preserves_reviewed_fifty_percent_crash():
    dates = pd.bdate_range("2025-01-02", periods=3)
    raw = pd.DataFrame({"A": [100.0, 50.0, 51.0]}, index=dates)
    validation = _validation(
        [
            (
                "A",
                dates[1],
                "CONFIRMED_MARKET_MOVE",
                dates[1],
                "MARKET_MOVE_NO_ADJUSTMENT",
                pd.NA,
            )
        ]
    )

    continuous, eligibility = corrected_price_views(raw, validation)

    pd.testing.assert_frame_equal(continuous, raw)
    pd.testing.assert_frame_equal(eligibility, raw)


def test_ranking_uses_contemporaneous_price_for_minimum_price_gate():
    dates = pd.bdate_range("2018-01-02", periods=300)
    inputs = {
        "close": pd.DataFrame({"A": range(100, 400)}, index=dates, dtype=float),
        "eligibility_close": pd.DataFrame(
            {"A": range(100, 400)}, index=dates, dtype=float
        ),
        "dollar_volume": pd.DataFrame({"A": 20_000_000.0}, index=dates),
        "nasdaq": pd.Series(range(1000, 1300), index=dates, dtype=float),
        "universe": lambda _date: {"A"},
        "technical_cache": {},
    }
    inputs["eligibility_close"].loc[dates[-1], "A"] = 20.0
    spec = {"lookback_sessions": 63, "skip_recent_sessions": 0}

    ranking = technical_ranking(dates[-1], spec, inputs)

    assert list(ranking.index) == ["A"]
    assert ranking.loc["A", "eligibility_price"] == 20.0
    assert ranking.loc["A", "price"] == 399.0


def test_unresolved_integer_jump_on_target_fails_closed():
    dates = pd.bdate_range("2025-01-02", periods=4)
    raw = pd.DataFrame({"A": [100.0, 50.0, 51.0, 52.0]}, index=dates)

    with pytest.raises(RuntimeError, match="Unresolved corporate action"):
        replay_with_sourced_hybrid_stop(
            raw,
            pd.Series(1000.0, index=dates),
            _targets(dates[0], ["A"]),
            dates[0],
            dates[-1],
            validation=_validation(),
            entry_loss_fraction=0.20,
            portfolio_stop_fraction=0.50,
            transaction_cost_bps=0.0,
        )


def test_recorded_but_unresolved_target_jump_also_fails_closed():
    dates = pd.bdate_range("2025-01-02", periods=4)
    raw = pd.DataFrame({"A": [100.0, 50.0, 51.0, 52.0]}, index=dates)
    validation = _validation(
        [
            (
                "A",
                dates[1],
                "UNRESOLVED_PRICE_JUMP",
                pd.NaT,
                pd.NA,
                pd.NA,
            )
        ]
    )

    with pytest.raises(RuntimeError, match="Unresolved corporate action"):
        replay_with_sourced_hybrid_stop(
            raw,
            pd.Series(1000.0, index=dates),
            _targets(dates[0], ["A"]),
            dates[0],
            dates[-1],
            validation=validation,
            entry_loss_fraction=0.20,
            portfolio_stop_fraction=0.50,
            transaction_cost_bps=0.0,
        )


def test_stop_before_rebalance_vetoes_same_close_reentry():
    dates = pd.bdate_range("2025-01-02", periods=6)
    raw = pd.DataFrame(
        {
            "A": [100.0, 100.0, 79.0, 78.0, 80.0, 82.0],
            "B": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
        },
        index=dates,
    )
    targets = pd.concat(
        [_targets(dates[0], ["A", "B"]), _targets(dates[3], ["A", "B"])],
        ignore_index=True,
    )

    result = replay_with_sourced_hybrid_stop(
        raw,
        pd.Series(1000.0, index=dates),
        targets,
        dates[0],
        dates[-1],
        validation=_validation(),
        entry_loss_fraction=0.20,
        portfolio_stop_fraction=0.50,
        transaction_cost_bps=0.0,
    )

    assert result.loc[dates[3], "stock_stop_exits"] == 1
    assert bool(result.loc[dates[3], "coincident_stop_veto"])
    assert result.loc[dates[3], "holdings"] == 1
    assert result.loc[dates[3], "invested"] == pytest.approx(0.5)


def test_missing_monthly_entry_close_fails_instead_of_disabling_stop():
    dates = pd.bdate_range("2025-01-02", periods=3)
    raw = pd.DataFrame(
        {"A": [float("nan"), 100.0, 101.0]}, index=dates, dtype=float
    )

    with pytest.raises(RuntimeError, match="no executable close"):
        replay_with_sourced_hybrid_stop(
            raw,
            pd.Series(1000.0, index=dates),
            _targets(dates[0], ["A"]),
            dates[0],
            dates[-1],
            validation=_validation(),
            entry_loss_fraction=0.20,
            portfolio_stop_fraction=0.50,
            transaction_cost_bps=0.0,
        )
