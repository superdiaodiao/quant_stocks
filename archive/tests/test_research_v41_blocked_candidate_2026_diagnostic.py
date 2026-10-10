import pandas as pd

from scripts import research_v41_blocked_candidate_2026_diagnostic as v41


def test_training_failure_cannot_be_overridden_by_diagnostic():
    source = v41._validate_training_source()

    assert source["v40_development_status"] == "BLOCKED"
    assert source["v40_training_eligible"] is False
    assert source["training_years_counted_as_final_wins"] is False


def test_protocol_marks_diagnostic_and_contamination_boundaries(tmp_path):
    protocol = v41.freeze_protocol(tmp_path / "protocol.json")
    boundary = protocol["evaluation_boundary"]

    assert boundary["training_years_excluded_from_comparison"] == list(
        v41.TRAINING_YEARS
    )
    assert boundary["diagnostic_only"] is True
    assert boundary["cannot_override_blocked_training_protocol"] is True
    assert boundary["parameter_isolated_from_2026"] is True
    assert boundary["architecture_isolated_from_2026"] is False


def test_evaluation_counts_no_training_wins(monkeypatch):
    dates = pd.to_datetime(["2026-01-30", "2026-02-27"])
    result = pd.DataFrame({
        "strategy": [0.02, 0.02],
        "benchmark": [0.01, 0.01],
        "turnover": [0.1, 0.1],
    }, index=dates)
    targets = pd.DataFrame({
        "effective_date": dates,
        "ticker": ["A", "A"],
    })
    monkeypatch.setattr(v41, "OBSERVATION_MONTHS", ("2026-01", "2026-02"))
    monkeypatch.setattr(v41, "MINIMUM_MONTHLY_WINS", 2)

    evaluation = v41.evaluate_diagnostic(
        {cost: result for cost in v41.COSTS}, targets
    )

    assert evaluation["training_years_counted_as_wins"] == 0
    assert evaluation["costs"]["50"]["monthly_wins_vs_nasdaq"] == 2
