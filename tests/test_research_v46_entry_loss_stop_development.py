import pandas as pd
import pytest

from scripts import research_v46_entry_loss_stop_development as v46


def test_candidate_grid_changes_only_entry_loss_threshold():
    specs = v46.candidate_specs()

    assert len(specs) == 4
    assert {spec["entry_loss_fraction"] for spec in specs} == {
        0.10,
        0.15,
        0.20,
        0.25,
    }
    assert {spec["reference_price"] for spec in specs} == {
        "adjusted close at latest monthly rebalance"
    }
    assert v46.DEVELOPMENT_YEARS == tuple(range(2020, 2026))
    assert v46.FINAL_COMPARISON_YEARS == ()


def test_entry_loss_stop_exits_next_close_after_entry_loss():
    dates = pd.bdate_range("2020-01-02", periods=6)
    close = pd.DataFrame({"A": [100.0, 105.0, 89.0, 88.0, 90.0, 92.0]}, index=dates)
    targets = pd.DataFrame({
        "effective_date": [dates[0]],
        "ticker": ["A"],
        "target_weight": [1.0],
        "base_transaction_cost_bps": [0.0],
    })

    result = v46.replay_with_entry_loss_stop(
        close,
        pd.Series(1000.0, index=dates),
        targets,
        dates[0],
        dates[-1],
        entry_loss_fraction=0.10,
        transaction_cost_bps=0.0,
    )

    assert result.loc[dates[2], "holdings"] == 1
    assert result.loc[dates[2], "stop_exits"] == 0
    assert result.loc[dates[3], "stop_exits"] == 1
    assert result.loc[dates[3], "holdings"] == 0


def test_profitable_peak_retrace_above_entry_does_not_exit():
    dates = pd.bdate_range("2020-01-02", periods=6)
    close = pd.DataFrame({"A": [100.0, 140.0, 115.0, 110.0, 108.0, 112.0]}, index=dates)
    targets = pd.DataFrame({
        "effective_date": [dates[0]],
        "ticker": ["A"],
        "target_weight": [1.0],
        "base_transaction_cost_bps": [0.0],
    })

    result = v46.replay_with_entry_loss_stop(
        close,
        pd.Series(1000.0, index=dates),
        targets,
        dates[0],
        dates[-1],
        entry_loss_fraction=0.10,
        transaction_cost_bps=0.0,
    )

    assert result["stop_exits"].sum() == 0
    assert result.iloc[-1]["holdings"] == 1


def test_entry_loss_stop_rejects_invalid_inputs():
    dates = pd.bdate_range("2020-01-02", periods=2)
    close = pd.DataFrame({"A": [100.0, 100.0]}, index=dates)
    targets = pd.DataFrame({
        "effective_date": [dates[0]],
        "ticker": ["A"],
        "target_weight": [1.0],
    })

    with pytest.raises(ValueError, match="between zero and one"):
        v46.replay_with_entry_loss_stop(
            close,
            pd.Series(1000.0, index=dates),
            targets,
            dates[0],
            dates[-1],
            entry_loss_fraction=0.0,
            transaction_cost_bps=0.0,
        )


def test_protocol_is_training_only_and_preserves_v43(tmp_path):
    protocol = v46.freeze_protocol(tmp_path / "protocol.json")

    boundary = protocol["evaluation_boundary"]
    assert boundary["training_years_excluded_from_final_comparison"] is True
    assert boundary["2026_used_for_parameter_selection"] is False
    assert boundary["architecture_isolated_from_2026"] is False
    assert protocol["risk_policy"]["not_a_trailing_stop"] is True
    assert protocol["release_status"] == "BLOCKED"
    assert protocol["promotion_eligible"] is False


def _fake_results(excess_by_year):
    dates = pd.to_datetime([f"{year}-06-30" for year in v46.DEVELOPMENT_YEARS])
    benchmark = pd.Series(0.10, index=dates)
    frame = pd.DataFrame({
        "strategy": benchmark + pd.Series(excess_by_year, index=dates),
        "benchmark": benchmark,
        "turnover": [1.0] * len(dates),
        "stop_exits": [0] * len(dates),
    }, index=dates)
    return {cost: frame.copy() for cost in v46.COSTS}


def test_selection_requires_every_training_year_positive(monkeypatch):
    monkeypatch.setattr(v46, "_baseline_drawdown_50bps", lambda: 1.0)
    good = _fake_results([0.02] * 6)
    bad = _fake_results([0.02, 0.02, 0.02, -0.001, 0.02, 0.02])

    selected, ranking = v46.select_candidate({"good": good, "bad": bad})

    assert selected == "good"
    assert ranking[0]["positive_training_years_50bps"] == 6
    assert next(row for row in ranking if row["candidate"] == "bad")[
        "training_eligible"
    ] is False
