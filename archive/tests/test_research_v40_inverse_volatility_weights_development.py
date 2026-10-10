import pandas as pd

from scripts import research_v40_inverse_volatility_weights_development as v40


def test_grid_is_small_and_caps_are_fixed():
    specs = v40.candidate_specs()

    assert len(specs) == 3
    assert {spec["inverse_volatility_power"] for spec in specs} == {
        0.5,
        1.0,
        1.5,
    }
    assert {spec["maximum_single_stock_weight"] for spec in specs} == {0.30}
    assert {spec["volatility_lookback_trading_days"] for spec in specs} == {63}


def test_capped_inverse_volatility_weights_sum_to_one_and_respect_cap():
    volatility = pd.Series({
        "A": 0.10,
        "B": 0.20,
        "C": 0.30,
        "D": 0.40,
        "E": 0.50,
    })

    weights = v40.capped_inverse_volatility_weights(
        volatility, power=1.0, maximum_weight=0.30
    )

    assert weights.sum() == 1.0
    assert weights.max() <= 0.30 + 1e-12
    assert weights["A"] > weights["E"]


def test_weighting_rejects_impossible_cap():
    volatility = pd.Series({"A": 0.10, "B": 0.20, "C": 0.30})

    try:
        v40.capped_inverse_volatility_weights(
            volatility, power=1.0, maximum_weight=0.30
        )
    except ValueError as error:
        assert "cannot support full investment" in str(error)
    else:
        raise AssertionError("impossible cap should fail")


def test_protocol_excludes_training_years_from_final_comparison(tmp_path):
    protocol = v40.freeze_protocol(tmp_path / "protocol.json")
    boundary = protocol["evaluation_boundary"]

    assert boundary["training_years_excluded_from_final_comparison"] is True
    assert boundary["final_comparison_years"] == []
    assert boundary["2026_used_for_parameter_selection"] is False
    assert boundary["architecture_isolated_from_2026"] is False
    assert boundary["parameter_isolated_from_2026"] is True
