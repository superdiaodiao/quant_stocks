import pandas as pd

from scripts import research_v32_model_isolated_stop_observation as v32


def test_training_years_are_explicitly_excluded_from_comparison():
    assert v32.TRAINING_YEARS == tuple(range(2020, 2026))
    assert v32.OBSERVATION_START == "2026-01-01"
    assert v32.OBSERVATION_END == "2026-07-31"
    assert v32.TRUE_PROSPECTIVE_START == "2026-08-31"


def test_observation_boundary_distinguishes_threshold_and_architecture_isolation(tmp_path):
    protocol = v32.freeze_protocol(tmp_path / "protocol.json")
    boundary = protocol["evaluation_boundary"]

    assert boundary["training_years_excluded_from_comparison"] == list(
        v32.TRAINING_YEARS
    )
    assert boundary["threshold_isolated_from_2026"] is True
    assert boundary["architecture_isolated_from_2026"] is False
    assert boundary["pristine_forward_test"] is False


def test_evaluation_counts_only_observation_months_and_nasdaq(monkeypatch):
    dates = pd.to_datetime(["2026-01-30", "2026-02-27"])
    result = pd.DataFrame({
        "strategy": [0.02, 0.02],
        "benchmark": [0.01, 0.01],
        "qqq": [0.015, 0.015],
        "turnover": [0.1, 0.1],
        "stop_exits": [0, 1],
    }, index=dates)
    targets = pd.DataFrame({
        "effective_date": dates,
        "ticker": ["A", "A"],
    })
    monkeypatch.setattr(v32, "OBSERVATION_MONTHS", ("2026-01", "2026-02"))
    monkeypatch.setattr(v32, "MINIMUM_MONTHLY_WINS", 2)

    evaluation = v32.evaluate_observation(
        {cost: result for cost in v32.COSTS}, targets
    )

    assert evaluation["training_years_excluded_from_comparison"] == list(
        v32.TRAINING_YEARS
    )
    assert evaluation["costs"]["50"]["monthly_wins_vs_nasdaq"] == 2
    assert evaluation["costs"]["50"]["compounded_excess_vs_nasdaq"] > 0.0
