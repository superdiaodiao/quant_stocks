import pandas as pd

from scripts import research_v36_correlation_diversified_momentum as v36


def test_candidate_grid_is_small_and_training_only():
    specs = v36.candidate_specs()

    assert len(specs) == 4
    assert {spec["maximum_pairwise_correlation"] for spec in specs} == {
        0.50,
        0.60,
        0.70,
        0.80,
    }
    assert v36.DEVELOPMENT_YEARS == tuple(range(2020, 2026))
    assert v36.FINAL_COMPARISON_YEARS == ()


def test_greedy_selection_rejects_correlated_names_and_keeps_rank_order():
    dates = pd.bdate_range("2020-01-02", periods=70)
    a = pd.Series([0.01 if index % 2 == 0 else -0.005 for index in range(70)], index=dates)
    returns = pd.DataFrame({
        "A": a,
        "B": a * 1.1,
        "C": -a,
        "D": [0.002 * ((index % 3) - 1) for index in range(70)],
    }, index=dates)

    selected, audit = v36.select_correlation_diversified(
        ["A", "B", "C", "D"],
        returns,
        dates[-1],
        maximum_pairwise_correlation=0.50,
        top_n=3,
        lookback_days=63,
        minimum_pair_observations=40,
    )

    assert selected == ["A", "C", "D"]
    assert audit["selected_count"] == 3
    assert any(
        rejection["ticker"] == "B"
        and rejection["reason"] == "CORRELATION_ABOVE_THRESHOLD"
        for rejection in audit["rejections"]
    )


def test_selection_uses_returns_only_through_signal_date():
    dates = pd.bdate_range("2020-01-02", periods=80)
    returns = pd.DataFrame({
        "A": [0.01 if index % 2 == 0 else -0.005 for index in range(80)],
        "B": [0.009 if index % 2 == 0 else -0.004 for index in range(80)],
        "C": [0.001 * ((index % 3) - 1) for index in range(80)],
    }, index=dates)
    signal = dates[69]
    changed = returns.copy()
    changed.loc[dates[70]:, "B"] = -changed.loc[dates[70]:, "A"]

    first, _ = v36.select_correlation_diversified(
        ["A", "B", "C"],
        returns,
        signal,
        maximum_pairwise_correlation=0.50,
        top_n=2,
    )
    second, _ = v36.select_correlation_diversified(
        ["A", "B", "C"],
        changed,
        signal,
        maximum_pairwise_correlation=0.50,
        top_n=2,
    )

    assert first == second


def test_protocol_excludes_training_years_from_final_comparison(tmp_path):
    protocol = v36.freeze_protocol(tmp_path / "protocol.json")
    boundary = protocol["evaluation_boundary"]

    assert boundary["training_years_excluded_from_final_comparison"] is True
    assert boundary["final_comparison_years"] == []
    assert boundary["2026_used_for_parameter_selection"] is False
    assert boundary["architecture_isolated_from_2026"] is False
    assert boundary["parameter_isolated_from_2026"] is True
