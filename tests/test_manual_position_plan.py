import hashlib

import pandas as pd
import pytest

from src.research.manual_position_plan import (
    _file_fingerprint,
    build_manual_position_plan,
)


def _recommendations():
    return pd.DataFrame({
        "ticker": ["A", "B"],
        "target_weight": [0.5, 0.5],
        "current_price": [60.0, 40.0],
        "current_median_dollar_volume_50d": [
            10_000_000.0, 20_000_000.0
        ],
    })


def test_whole_share_plan_reserves_cash_for_costs_and_rounding():
    plan, summary = build_manual_position_plan(
        _recommendations(), 1_000.0, transaction_cost_bps=10
    )

    assert plan.set_index("ticker").loc["A", "target_shares"] == 8
    assert plan.set_index("ticker").loc["B", "target_shares"] == 12
    assert summary["estimated_transaction_cost"] == pytest.approx(0.96)
    assert summary["estimated_residual_cash"] == pytest.approx(39.04)
    assert summary["status"] == "REFERENCE_ONLY_NOT_AN_ORDER"


def test_existing_non_target_holding_is_included_as_reference_decrease():
    holdings = pd.DataFrame({
        "ticker": ["A", "C"],
        "shares": [2.0, 3.0],
        "current_price": [60.0, 25.0],
    })

    plan, summary = build_manual_position_plan(
        _recommendations(), 1_000.0, holdings, transaction_cost_bps=0
    )
    indexed = plan.set_index("ticker")

    assert indexed.loc["C", "target_shares"] == 0
    assert indexed.loc["C", "share_change_reference"] == -3
    assert indexed.loc["C", "reference_action"] == "REFERENCE_DECREASE"
    assert summary["estimated_turnover_notional"] > 0


def test_fractional_plan_is_self_financing_after_costs():
    plan, summary = build_manual_position_plan(
        _recommendations(),
        1_000.0,
        transaction_cost_bps=10,
        fractional_shares=True,
    )

    assert (
        plan["target_value"].sum()
        + summary["estimated_transaction_cost"]
        + summary["estimated_residual_cash"]
    ) == pytest.approx(1_000.0)
    assert summary["estimated_residual_cash"] == pytest.approx(0.0, abs=1e-8)


@pytest.mark.parametrize(
    "recommendations,equity,error",
    [
        (
            pd.DataFrame({
                "ticker": ["A"],
                "target_weight": [1.1],
                "current_price": [10.0],
            }),
            1_000.0,
            "target weights",
        ),
        (_recommendations(), 0.0, "account_equity_usd"),
    ],
)
def test_invalid_manual_plan_inputs_are_rejected(
    recommendations, equity, error
):
    with pytest.raises(ValueError, match=error):
        build_manual_position_plan(recommendations, equity)


def test_reference_plan_input_fingerprint_is_stable(tmp_path):
    path = tmp_path / "recommendations.csv"
    payload = b"ticker,target_weight\nA,1.0\n"
    path.write_bytes(payload)

    fingerprint = _file_fingerprint(path)

    assert fingerprint == {
        "path": str(path),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
