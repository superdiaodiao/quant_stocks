import pandas as pd
import pytest

from src.research.ibkr_cost_calibration import (
    base_stock_commission_usd,
    calibrate_reference_orders,
    tiered_rate_per_share,
)


def test_fixed_commission_matches_published_examples():
    assert base_stock_commission_usd(
        100, 25.0, pricing_plan="fixed"
    ) == pytest.approx(1.0)
    assert base_stock_commission_usd(
        1_000, 25.0, pricing_plan="fixed"
    ) == pytest.approx(5.0)


def test_tiered_rate_tracks_monthly_volume_band():
    assert tiered_rate_per_share(0) == pytest.approx(0.0035)
    assert tiered_rate_per_share(300_001) == pytest.approx(0.0020)
    assert tiered_rate_per_share(100_000_001) == pytest.approx(0.0005)


def test_reference_calibration_exposes_remaining_friction_budget():
    plan = pd.DataFrame({
        "ticker": ["A", "B"],
        "share_change_reference": [100.0, 0.0],
        "current_price": [25.0, 50.0],
        "trade_notional_reference": [2_500.0, 0.0],
    })

    detail, summary = calibrate_reference_orders(
        plan, pricing_plan="fixed"
    )

    assert summary["status"] == "OFFLINE_REFERENCE_ONLY_NOT_AN_ORDER"
    assert summary["order_count"] == 1
    assert summary["weighted_base_commission_bps"] == pytest.approx(4.0)
    assert summary["remaining_total_friction_budget_bps"]["10"] == (
        pytest.approx(6.0)
    )
    assert detail.loc[detail["ticker"].eq("B"), "ibkr_base_commission_usd"].item() == 0


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"shares": -1, "price": 10, "pricing_plan": "fixed"}, "shares"),
        ({"shares": 1, "price": 0, "pricing_plan": "fixed"}, "price"),
        ({"shares": 1, "price": 10, "pricing_plan": "unknown"}, "pricing_plan"),
    ],
)
def test_invalid_commission_inputs_are_rejected(kwargs, error):
    with pytest.raises(ValueError, match=error):
        base_stock_commission_usd(**kwargs)
