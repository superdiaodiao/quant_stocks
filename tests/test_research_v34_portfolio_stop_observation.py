import pandas as pd

from scripts import research_v34_portfolio_stop_observation as v34


def test_training_years_are_not_observation_wins():
    assert v34.TRAINING_YEARS == tuple(range(2020, 2026))
    assert v34.OBSERVATION_START == "2026-01-01"
    assert v34.OBSERVATION_END == "2026-07-31"
    assert v34.TRUE_PROSPECTIVE_START == "2026-08-31"


def test_freeze_distinguishes_threshold_from_architecture_isolation(tmp_path):
    protocol = v34.freeze_protocol(tmp_path / "protocol.json")
    boundary = protocol["evaluation_boundary"]

    assert boundary["training_years_excluded_from_comparison"] == list(
        v34.TRAINING_YEARS
    )
    assert boundary["threshold_isolated_from_2026"] is True
    assert boundary["architecture_isolated_from_2026"] is False
    assert boundary["pristine_forward_test"] is False


def test_evaluation_counts_only_observation_months(monkeypatch):
    dates = pd.to_datetime(["2026-01-30", "2026-02-27"])
    result = pd.DataFrame({
        "strategy": [0.02, 0.02],
        "benchmark": [0.01, 0.01],
        "turnover": [0.1, 0.1],
        "stop_exits": [0, 1],
    }, index=dates)
    targets = pd.DataFrame({
        "effective_date": dates,
        "ticker": ["A", "A"],
    })
    monkeypatch.setattr(v34, "OBSERVATION_MONTHS", ("2026-01", "2026-02"))
    monkeypatch.setattr(v34, "MINIMUM_MONTHLY_WINS", 2)

    evaluation = v34.evaluate_observation(
        {cost: result for cost in v34.COSTS}, targets
    )

    assert evaluation["training_years_counted_as_wins"] == 0
    assert evaluation["training_years_excluded_from_comparison"] == list(
        v34.TRAINING_YEARS
    )
    assert evaluation["costs"]["50"]["monthly_wins_vs_nasdaq"] == 2
    assert evaluation["costs"]["50"]["compounded_excess_vs_nasdaq"] > 0.0
