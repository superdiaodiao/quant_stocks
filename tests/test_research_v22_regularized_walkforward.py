import pandas as pd
import pytest

from scripts import research_v22_regularized_walkforward as v22


def _result(years, excesses):
    dates = pd.to_datetime([f"{year}-12-30" for year in years])
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


def test_regularized_selector_prefers_lower_exposure_within_tolerance():
    best = _costs(_result([2022, 2023], [0.03, 0.04]))
    conservative = _costs(_result([2022, 2023], [0.02, 0.03]))

    selected, ranking, _ = v22.regularized_select_variant({
        "lookback_42_crowded_stock_0.20": best,
        "lookback_84_crowded_stock_0.10": conservative,
    }, years=(2022, 2023))

    assert selected == "lookback_84_crowded_stock_0.10"
    assert ranking[0]["near_best"] is True


def test_regularized_selector_does_not_cross_tolerance():
    best = _costs(_result([2022, 2023], [0.04, 0.05]))
    too_weak = _costs(_result([2022, 2023], [0.01, 0.02]))

    selected, ranking, _ = v22.regularized_select_variant({
        "lookback_42_crowded_stock_0.20": best,
        "lookback_84_crowded_stock_0.10": too_weak,
    }, years=(2022, 2023))

    assert selected == "lookback_42_crowded_stock_0.20"
    weak = next(
        row for row in ranking
        if row["variant"] == "lookback_84_crowded_stock_0.10"
    )
    assert weak["near_best"] is False


def test_regularized_selector_rejects_future_data():
    leaked = _costs(_result([2022, 2023, 2024], [0.02, 0.03, 10.0]))

    with pytest.raises(RuntimeError, match="future data"):
        v22.regularized_select_variant({
            "lookback_84_crowded_stock_0.10": leaked,
        }, years=(2022, 2023))


def test_ibkr_envelope_preserves_noncommission_budget():
    envelope = v22._ibkr_cost_envelope({
        "account_equities_usd": [10_000],
        "pricing_plans": ["fixed"],
        "calibration": [{"weighted_base_commission_bps": 5.0}],
    })

    assert envelope["primary_total_cost_bps"] == 30.0
    assert envelope[
        "primary_remaining_noncommission_budget_bps_at_maximum_commission"
    ] == 25.0
    assert envelope["includes_realized_spread_or_slippage"] is False


def test_v22_full_run_freezes_future_only_protocol(tmp_path):
    report = v22.run(tmp_path / "freeze")

    assert report["selected_variant"] == "lookback_84_crowded_stock_0.10"
    assert report["walk_forward_status"] == "PASS"
    assert report["walk_forward_diagnostic"]["passed_fold_count"] == 3
    assert report["development_status"] == "PASS"
    assert report["research_forward_observation_ready"] is True
    assert report["development_data"][
        "existing_2026_data_used_for_selection_or_evaluation"
    ] is False
    assert report["future_forward_protocol"][
        "data_must_be_later_than"
    ] == "2026-08-30"
    assert report["release_status"] == "BLOCKED"
    assert report["promotion_eligible"] is False
    assert report["brokerage_or_trading_authorized"] is False
