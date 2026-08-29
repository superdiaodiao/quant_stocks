import pandas as pd

from scripts import research_v19_source_locked_v10_feasibility as v19


def _daily(yearly_strategy, yearly_benchmark):
    dates = pd.to_datetime([
        "2022-12-30",
        "2023-12-29",
        "2024-12-31",
        "2025-12-31",
        "2026-07-17",
    ])
    frame = pd.DataFrame({
        "strategy": yearly_strategy,
        "benchmark": yearly_benchmark,
        "qqq": yearly_benchmark,
        "turnover": 0.0,
        "nav": (1.0 + pd.Series(yearly_strategy)).cumprod().to_numpy(),
    }, index=dates)
    frame["drawdown"] = frame["nav"].div(frame["nav"].cummax()).sub(1.0)
    return frame


def test_summary_requires_five_of_five_at_every_cost():
    passing = _daily([0.10] * 5, [0.05] * 5)
    failing = _daily([0.10, 0.10, 0.10, 0.04, 0.10], [0.05] * 5)

    passed = v19.summarize_variant({10: passing, 30: passing, 50: passing})
    failed = v19.summarize_variant({10: passing, 30: passing, 50: failing})

    assert passed["all_costs_five_of_five"] is True
    assert failed["all_costs_five_of_five"] is False
    assert failed["costs"]["50"]["nasdaq_annual_win_count"] == 4


def test_monthly_decision_uses_only_prior_relative_returns():
    index = pd.bdate_range("2022-01-03", periods=90)
    stock = pd.DataFrame({
        "strategy": 0.0,
        "benchmark": 0.0,
    }, index=index)
    qqq = pd.Series(0.0, index=index)
    relative = pd.Series(0.001, index=index)

    _, baseline = v19.simulate_source_locked_contrarian_sleeves(
        stock,
        qqq,
        relative,
        lookback=42,
        crowded_stock_weight=0.10,
        transfer_cost_bps=10.0,
    )
    changed_future = relative.copy()
    cutoff = pd.Timestamp(baseline.iloc[2]["date"])
    changed_future.loc[changed_future.index >= cutoff] = -0.50
    _, changed = v19.simulate_source_locked_contrarian_sleeves(
        stock,
        qqq,
        changed_future,
        lookback=42,
        crowded_stock_weight=0.10,
        transfer_cost_bps=10.0,
    )

    prior = baseline["date"].lt(cutoff)
    pd.testing.assert_frame_equal(
        baseline.loc[prior].reset_index(drop=True),
        changed.loc[prior].reset_index(drop=True),
    )


def test_current_bound_grid_has_one_retrospective_five_of_five_variant(tmp_path):
    report = v19.run(tmp_path)

    assert report["historical_fit_status"] == "PASS"
    assert report["eligible_variants"] == [
        "lookback_84_crowded_stock_0.10"
    ]
    assert report["selected_variant"] == (
        "lookback_84_crowded_stock_0.10"
    )
    assert report["selected_variant_result"][
        "all_costs_five_of_five"
    ] is True
    assert report["release_status"] == "BLOCKED"
    assert report["promotion_eligible"] is False
    assert report["historical_selection_contaminated"] is True
    assert report["statistically_untouched"] is False
    assert report["v14_cost_path_reconciliation"][
        "maximum_absolute_error"
    ] < 1e-12
