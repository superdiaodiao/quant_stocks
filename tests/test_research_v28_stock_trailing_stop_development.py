import pandas as pd
import pytest

from scripts import research_v28_stock_trailing_stop_development as v28


def test_candidate_grid_uses_standard_individual_stop_thresholds():
    specs = v28.candidate_specs()

    assert len(specs) == 4
    assert {spec["trailing_stop_fraction"] for spec in specs} == {
        0.10,
        0.15,
        0.20,
        0.25,
    }
    assert {spec["stop_signal_frequency"] for spec in specs} == {"daily"}
    assert v28.DEVELOPMENT_YEARS == tuple(range(2020, 2026))
    assert v28.OBSERVATION_START == "2026-08-01"


def test_trailing_stop_exits_next_close_and_leaves_cash():
    dates = pd.bdate_range("2020-01-02", periods=6)
    close = pd.DataFrame({
        "A": [100.0, 110.0, 95.0, 90.0, 100.0, 105.0],
    }, index=dates)
    nasdaq = pd.Series(1000.0, index=dates)
    targets = pd.DataFrame({
        "effective_date": [dates[0]],
        "ticker": ["A"],
        "target_weight": [1.0],
        "base_transaction_cost_bps": [0.0],
    })

    result = v28.replay_with_individual_trailing_stop(
        close,
        nasdaq,
        targets,
        dates[0],
        dates[-1],
        trailing_stop_fraction=0.10,
        transaction_cost_bps=0.0,
    )

    assert result.loc[dates[2], "holdings"] == 1
    assert result.loc[dates[2], "stop_exits"] == 0
    assert result.loc[dates[3], "stop_exits"] == 1
    assert result.loc[dates[3], "holdings"] == 0
    assert result.loc[dates[3], "invested"] == pytest.approx(0.0)
    assert result.loc[dates[4], "holdings"] == 0


def test_monthly_target_has_priority_over_pending_stop_exit():
    dates = pd.bdate_range("2020-01-02", periods=5)
    close = pd.DataFrame({
        "A": [100.0, 110.0, 95.0, 100.0, 105.0],
    }, index=dates)
    nasdaq = pd.Series(1000.0, index=dates)
    targets = pd.DataFrame({
        "effective_date": [dates[0], dates[3]],
        "ticker": ["A", "A"],
        "target_weight": [1.0, 1.0],
        "base_transaction_cost_bps": [0.0, 0.0],
    })

    result = v28.replay_with_individual_trailing_stop(
        close,
        nasdaq,
        targets,
        dates[0],
        dates[-1],
        trailing_stop_fraction=0.10,
        transaction_cost_bps=0.0,
    )

    assert result.loc[dates[3], "stop_exits"] == 0
    assert result.loc[dates[3], "holdings"] == 1


def test_trailing_stop_rejects_invalid_threshold():
    dates = pd.bdate_range("2020-01-02", periods=2)
    close = pd.DataFrame({"A": [100.0, 100.0]}, index=dates)
    targets = pd.DataFrame({
        "effective_date": [dates[0]],
        "ticker": ["A"],
        "target_weight": [1.0],
        "base_transaction_cost_bps": [0.0],
    })

    with pytest.raises(ValueError, match="between zero and one"):
        v28.replay_with_individual_trailing_stop(
            close,
            pd.Series(1000.0, index=dates),
            targets,
            dates[0],
            dates[-1],
            trailing_stop_fraction=0.0,
            transaction_cost_bps=0.0,
        )


def test_selection_requires_five_of_six_complete_years():
    dates = pd.to_datetime([f"{year}-06-30" for year in v28.DEVELOPMENT_YEARS])
    good = pd.DataFrame({
        "strategy": [0.20, 0.20, 0.20, 0.20, 0.20, 0.05],
        "benchmark": [0.10] * 6,
        "qqq": [0.10] * 6,
        "turnover": [1.0] * 6,
        "stop_exits": [1] * 6,
    }, index=dates)
    bad = good.copy()
    bad["strategy"] = [0.20, 0.20, 0.20, 0.05, 0.05, 0.05]

    selected, ranking = v28.select_candidate({
        "good": v28._summary({cost: good for cost in v28.COSTS}),
        "bad": v28._summary({cost: bad for cost in v28.COSTS}),
    })

    assert selected == "good"
    assert ranking[0]["required_wins"] == 5
    assert ranking[0]["wins_vs_qqq_50bps"] == 5
    assert next(row for row in ranking if row["candidate"] == "bad")[
        "eligible"
    ] is False
