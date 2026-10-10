import pandas as pd

from scripts import research_v39_risk_adjusted_momentum_development as v39


def test_grid_is_small_and_keeps_base_structure_fixed():
    specs = v39.candidate_specs()

    assert len(specs) == 3
    assert {spec["volatility_penalty_power"] for spec in specs} == {
        0.5,
        1.0,
        1.5,
    }
    assert {spec["momentum_lookback_sessions"] for spec in specs} == {63}
    assert {spec["liquid_candidate_pool"] for spec in specs} == {25}
    assert {spec["top_n"] for spec in specs} == {5}


def test_risk_adjusted_ranking_prefers_lower_volatility_at_equal_momentum():
    dates = pd.bdate_range("2020-01-02", periods=70)
    ranking = pd.DataFrame({
        "momentum_excess_vs_nasdaq": [0.20, 0.20],
        "median_dollar_volume_50d": [100.0, 100.0],
    }, index=["HIGH", "LOW"])
    returns = pd.DataFrame({
        "HIGH": [0.02 if index % 2 == 0 else -0.02 for index in range(70)],
        "LOW": [0.005 if index % 2 == 0 else -0.005 for index in range(70)],
    }, index=dates)

    adjusted = v39.risk_adjusted_ranking(
        ranking,
        returns,
        dates[-1],
        volatility_penalty_power=1.0,
    )

    assert adjusted.index.tolist() == ["LOW", "HIGH"]


def test_ranking_does_not_use_returns_after_signal():
    dates = pd.bdate_range("2020-01-02", periods=80)
    ranking = pd.DataFrame({
        "momentum_excess_vs_nasdaq": [0.20, 0.19],
        "median_dollar_volume_50d": [100.0, 90.0],
    }, index=["A", "B"])
    returns = pd.DataFrame({
        "A": [0.01 if index % 2 == 0 else -0.01 for index in range(80)],
        "B": [0.005 if index % 2 == 0 else -0.005 for index in range(80)],
    }, index=dates)
    changed = returns.copy()
    signal = dates[69]
    changed.loc[dates[70]:, "B"] *= 20.0

    first = v39.risk_adjusted_ranking(
        ranking, returns, signal, volatility_penalty_power=1.0
    )
    second = v39.risk_adjusted_ranking(
        ranking, changed, signal, volatility_penalty_power=1.0
    )

    assert first.index.tolist() == second.index.tolist()


def test_protocol_excludes_training_years_from_final_comparison(tmp_path):
    protocol = v39.freeze_protocol(tmp_path / "protocol.json")
    boundary = protocol["evaluation_boundary"]

    assert boundary["training_years_excluded_from_final_comparison"] is True
    assert boundary["final_comparison_years"] == []
    assert boundary["2026_used_for_parameter_selection"] is False
    assert boundary["architecture_isolated_from_2026"] is False
    assert boundary["parameter_isolated_from_2026"] is True
