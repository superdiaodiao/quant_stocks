import pandas as pd
import pytest

from scripts import research_v33_portfolio_stop_development as v33


def test_candidate_grid_is_small_and_standard():
    specs = v33.candidate_specs()

    assert len(specs) == 4
    assert {spec["portfolio_trailing_stop_fraction"] for spec in specs} == {
        0.10,
        0.15,
        0.20,
        0.25,
    }
    assert v33.DEVELOPMENT_YEARS == tuple(range(2020, 2026))
    assert v33.FINAL_COMPARISON_YEARS == ()


def test_portfolio_stop_exits_next_close_and_waits_for_monthly_target():
    dates = pd.bdate_range("2020-01-02", periods=6)
    close = pd.DataFrame({
        "A": [100.0, 110.0, 95.0, 90.0, 100.0, 105.0],
        "B": [100.0, 110.0, 95.0, 90.0, 100.0, 105.0],
    }, index=dates)
    targets = pd.DataFrame({
        "effective_date": [dates[0], dates[0]],
        "ticker": ["A", "B"],
        "target_weight": [0.5, 0.5],
    })

    result = v33.replay_with_portfolio_trailing_stop(
        close,
        pd.Series(1000.0, index=dates),
        targets,
        dates[0],
        dates[-1],
        portfolio_stop_fraction=0.10,
        transaction_cost_bps=0.0,
    )

    assert result.loc[dates[2], "stop_pending"] == 1
    assert result.loc[dates[2], "stop_exits"] == 0
    assert result.loc[dates[3], "stop_exits"] == 1
    assert result.loc[dates[3], "holdings"] == 0
    assert result.loc[dates[4], "invested"] == pytest.approx(0.0)


def test_monthly_target_has_priority_over_pending_portfolio_exit():
    dates = pd.bdate_range("2020-01-02", periods=5)
    close = pd.DataFrame({"A": [100.0, 110.0, 95.0, 100.0, 105.0]}, index=dates)
    targets = pd.DataFrame({
        "effective_date": [dates[0], dates[3]],
        "ticker": ["A", "A"],
        "target_weight": [1.0, 1.0],
    })

    result = v33.replay_with_portfolio_trailing_stop(
        close,
        pd.Series(1000.0, index=dates),
        targets,
        dates[0],
        dates[-1],
        portfolio_stop_fraction=0.10,
        transaction_cost_bps=0.0,
    )

    assert result.loc[dates[2], "stop_pending"] == 0
    assert result.loc[dates[3], "stop_exits"] == 0
    assert result.loc[dates[3], "holdings"] == 1


def test_protocol_excludes_training_years_from_final_comparison(tmp_path):
    protocol = v33.freeze_protocol(tmp_path / "protocol.json")
    boundary = protocol["evaluation_boundary"]

    assert boundary["training_years"] == list(v33.DEVELOPMENT_YEARS)
    assert boundary["training_years_excluded_from_final_comparison"] is True
    assert boundary["final_comparison_years"] == []
    assert boundary["2026_used_for_threshold_selection"] is False
    assert boundary["architecture_isolated_from_2026"] is False
    assert boundary["threshold_isolated_from_2026"] is True


def test_selection_metadata_is_training_only():
    dates = pd.to_datetime([f"{year}-06-30" for year in v33.DEVELOPMENT_YEARS])
    result = pd.DataFrame({
        "strategy": [0.20] * 6,
        "benchmark": [0.10] * 6,
        "turnover": [1.0] * 6,
        "stop_exits": [1] * 6,
    }, index=dates)

    selected, ranking = v33.select_candidate({
        "candidate": {cost: result for cost in v33.COSTS},
    })

    assert selected == "candidate"
    assert ranking[0]["final_evidence"] is False
    assert ranking[0]["positive_training_years_50bps"] == 6


def test_portfolio_stop_rejects_invalid_threshold():
    dates = pd.bdate_range("2020-01-02", periods=2)
    close = pd.DataFrame({"A": [100.0, 100.0]}, index=dates)
    targets = pd.DataFrame({
        "effective_date": [dates[0]],
        "ticker": ["A"],
        "target_weight": [1.0],
    })

    with pytest.raises(ValueError, match="between zero and one"):
        v33.replay_with_portfolio_trailing_stop(
            close,
            pd.Series(1000.0, index=dates),
            targets,
            dates[0],
            dates[-1],
            portfolio_stop_fraction=0.0,
            transaction_cost_bps=0.0,
        )
