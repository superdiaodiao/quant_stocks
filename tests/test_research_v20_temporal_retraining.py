import pandas as pd
import pytest

from scripts import research_v20_recent_holdout as holdout
from scripts import research_v20_temporal_retraining as v20


def _result(years, excesses):
    dates = pd.to_datetime([
        "2026-07-17" if year == 2026 else f"{year}-12-30"
        for year in years
    ])
    benchmark = [0.05] * len(years)
    strategy = [base + excess for base, excess in zip(benchmark, excesses)]
    frame = pd.DataFrame({
        "strategy": strategy,
        "benchmark": benchmark,
        "qqq": benchmark,
        "turnover": [0.1] * len(years),
        "transaction_cost": [0.0] * len(years),
        "nav": (1.0 + pd.Series(strategy)).cumprod().to_numpy(),
    }, index=dates)
    frame["drawdown"] = frame["nav"].div(frame["nav"].cummax()).sub(1.0)
    return frame


def _costs(frame):
    return {10: frame.copy(), 30: frame.copy(), 50: frame.copy()}


def test_development_selector_uses_only_2022_and_2023():
    weaker = _costs(_result([2022, 2023], [0.01, 0.02]))
    stronger = _costs(_result([2022, 2023], [0.03, 0.04]))

    selected, ranking, _ = v20.select_development_variant({
        "weaker": weaker,
        "stronger": stronger,
    })

    assert selected == "stronger"
    assert ranking[0]["variant"] == "stronger"
    assert ranking[0]["eligible"] is True


def test_development_selector_rejects_any_future_row():
    leaked = _costs(_result([2022, 2023, 2025], [0.01, 0.02, 10.0]))

    with pytest.raises(RuntimeError, match="future data"):
        v20.select_development_variant({"leaked": leaked})


def test_validation_rejects_post_2024_data():
    leaked = _costs(_result([2022, 2023, 2024, 2025], [0.01] * 4))

    with pytest.raises(RuntimeError, match="post-2024"):
        v20.validate_selected_variant(leaked)


def test_recent_holdout_requires_both_years_at_every_cost():
    passing = _costs(_result([2025, 2026], [0.01, 0.02]))
    failing = _costs(_result([2025, 2026], [0.01, 0.02]))
    failing[50] = _result([2025, 2026], [-0.01, 0.02])

    passed = holdout.evaluate_recent_holdout(passing)
    failed = holdout.evaluate_recent_holdout(failing)

    assert passed["all_predeclared_holdout_gates_passed"] is True
    assert failed["all_predeclared_holdout_gates_passed"] is False
    assert failed["costs"]["50"]["both_annual_excesses_positive"] is False


def test_full_retraining_stops_before_recent_holdout(tmp_path):
    report = v20.run(tmp_path / "freeze")

    assert report["selected_variant"] == "lookback_84_crowded_stock_0.20"
    assert report["validation_status"] == "PASS"
    assert report["model_data_isolation"] == "PASS"
    assert report["recent_holdout_executed"] is False
    assert report["recent_holdout_results_inspected"] is False
    assert report["release_status"] == "BLOCKED"
    assert report["promotion_eligible"] is False
