import pandas as pd

from scripts import research_v27_stock_only_2026_observation as v27


def _results(strategy=0.02, qqq=0.01):
    dates = pd.to_datetime([f"2026-{month:02d}-15" for month in range(1, 8)])
    frame = pd.DataFrame({
        "strategy": [strategy] * 7,
        "benchmark": [qqq] * 7,
        "qqq": [qqq] * 7,
        "turnover": [0.2] * 7,
    }, index=dates)
    return {cost: frame.copy() for cost in v27.COSTS}


def _targets(ticker="A"):
    return pd.DataFrame({
        "effective_date": pd.to_datetime(
            [f"2026-{month:02d}-02" for month in range(1, 8)]
        ),
        "ticker": [ticker] * 7,
        "target_weight": [0.2] * 7,
        "base_transaction_cost_bps": [10.0] * 7,
    })


def test_precommitted_observation_passes_seven_positive_months():
    report = v27.evaluate_observation(_results(), _targets())

    assert report["all_precommitted_gates_passed"] is True
    assert report["observed_months"] == list(v27.OBSERVATION_MONTHS)
    assert report["decision_months"] == list(v27.OBSERVATION_MONTHS)
    assert report["costs"]["50"]["monthly_wins_vs_qqq"] == 7
    assert report["gates"]["positive_excess_50bps"] is True


def test_precommitted_observation_blocks_negative_excess():
    report = v27.evaluate_observation(
        _results(strategy=0.005, qqq=0.01), _targets()
    )

    assert report["all_precommitted_gates_passed"] is False
    assert report["gates"]["positive_excess_30bps"] is False
    assert report["gates"]["monthly_wins_50bps"] is False


def test_precommitted_observation_rejects_etf_target():
    report = v27.evaluate_observation(_results(), _targets("QQQ"))

    assert report["all_precommitted_gates_passed"] is False
    assert report["gates"]["no_forbidden_etf_targets"] is False


def test_selected_candidate_is_exact_v26_development_winner():
    specification = v27._selected_specification()

    assert specification["key"] == v27.SELECTED_CANDIDATE
    assert specification["signal_frequency"] == "monthly"
    assert specification["top_n"] == 5
    assert specification["liquid_pool_size"] == 25
    assert specification["quality_mode"] == "profitable"
