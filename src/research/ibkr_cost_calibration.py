"""Offline IBKR US-stock commission calibration for reference order plans."""

from __future__ import annotations

import numpy as np
import pandas as pd


IBKR_US_STOCK_COMMISSION_SOURCE = (
    "https://www.interactivebrokers.com/en/pricing/commissions-stocks.php"
)
SCHEDULE_ACCESSED_AT = "2026-08-30"
FIXED_RATE_PER_SHARE_USD = 0.005
FIXED_MINIMUM_PER_ORDER_USD = 1.00
TIERED_MINIMUM_PER_ORDER_USD = 0.35
MAXIMUM_COMMISSION_FRACTION = 0.01
TIERED_RATES = (
    (300_000, 0.0035),
    (3_000_000, 0.0020),
    (20_000_000, 0.0015),
    (100_000_000, 0.0010),
    (float("inf"), 0.0005),
)


def tiered_rate_per_share(monthly_volume_shares: float) -> float:
    if (
        not np.isfinite(monthly_volume_shares)
        or monthly_volume_shares < 0
    ):
        raise ValueError("monthly_volume_shares must be finite and non-negative")
    for upper_bound, rate in TIERED_RATES:
        if monthly_volume_shares <= upper_bound:
            return float(rate)
    raise AssertionError("unreachable tiered commission band")


def base_stock_commission_usd(
    shares: float,
    price: float,
    *,
    pricing_plan: str,
    monthly_volume_shares: float = 0.0,
) -> float:
    """Calculate published base commission without venue or regulatory fees."""
    if not np.isfinite(shares) or shares < 0:
        raise ValueError("shares must be finite and non-negative")
    if not np.isfinite(price) or price <= 0:
        raise ValueError("price must be positive and finite")
    if shares == 0:
        return 0.0
    normalized_plan = str(pricing_plan).strip().lower()
    if normalized_plan == "fixed":
        rate = FIXED_RATE_PER_SHARE_USD
        minimum = FIXED_MINIMUM_PER_ORDER_USD
    elif normalized_plan == "tiered":
        rate = tiered_rate_per_share(monthly_volume_shares)
        minimum = TIERED_MINIMUM_PER_ORDER_USD
    else:
        raise ValueError("pricing_plan must be 'fixed' or 'tiered'")
    notional = float(shares) * float(price)
    uncapped = max(float(shares) * rate, minimum)
    return float(min(uncapped, notional * MAXIMUM_COMMISSION_FRACTION))


def calibrate_reference_orders(
    plan: pd.DataFrame,
    *,
    pricing_plan: str,
    monthly_volume_shares: float = 0.0,
    stress_cost_bps: tuple[float, ...] = (10.0, 30.0, 50.0),
) -> tuple[pd.DataFrame, dict]:
    """Add base commissions and residual friction budgets to a local plan."""
    required = {
        "ticker",
        "share_change_reference",
        "current_price",
        "trade_notional_reference",
    }
    missing = required - set(plan.columns)
    if missing:
        raise ValueError(f"reference plan missing columns: {sorted(missing)}")
    if not stress_cost_bps:
        raise ValueError("stress_cost_bps must not be empty")
    stresses = tuple(float(value) for value in stress_cost_bps)
    if any(not np.isfinite(value) or value < 0 for value in stresses):
        raise ValueError("stress cost values must be finite and non-negative")

    frame = plan.copy()
    frame["absolute_share_change"] = pd.to_numeric(
        frame["share_change_reference"], errors="coerce"
    ).abs()
    frame["current_price"] = pd.to_numeric(
        frame["current_price"], errors="coerce"
    )
    frame["trade_notional_reference"] = pd.to_numeric(
        frame["trade_notional_reference"], errors="coerce"
    )
    invalid = (
        frame[[
            "absolute_share_change",
            "current_price",
            "trade_notional_reference",
        ]].isna().any(axis=1)
        | (~np.isfinite(frame["absolute_share_change"]))
        | (~np.isfinite(frame["current_price"]))
        | (~np.isfinite(frame["trade_notional_reference"]))
        | frame["absolute_share_change"].lt(0)
        | frame["current_price"].le(0)
        | frame["trade_notional_reference"].lt(0)
    )
    if invalid.any():
        raise ValueError("reference plan contains invalid order values")

    frame["ibkr_base_commission_usd"] = frame.apply(
        lambda row: base_stock_commission_usd(
            float(row["absolute_share_change"]),
            float(row["current_price"]),
            pricing_plan=pricing_plan,
            monthly_volume_shares=monthly_volume_shares,
        ),
        axis=1,
    )
    frame["ibkr_base_commission_bps"] = np.where(
        frame["trade_notional_reference"].gt(0),
        frame["ibkr_base_commission_usd"]
        / frame["trade_notional_reference"]
        * 10_000.0,
        0.0,
    )
    for stress in stresses:
        label = f"remaining_to_{stress:g}bps"
        frame[label] = stress - frame["ibkr_base_commission_bps"]

    traded = float(frame["trade_notional_reference"].sum())
    commission = float(frame["ibkr_base_commission_usd"].sum())
    weighted_bps = commission / traded * 10_000.0 if traded > 0 else 0.0
    order_count = int(frame["trade_notional_reference"].gt(0).sum())
    summary = {
        "status": "OFFLINE_REFERENCE_ONLY_NOT_AN_ORDER",
        "pricing_plan": str(pricing_plan).strip().lower(),
        "monthly_volume_shares": float(monthly_volume_shares),
        "order_count": order_count,
        "traded_notional_usd": traded,
        "ibkr_base_commission_usd": commission,
        "weighted_base_commission_bps": weighted_bps,
        "remaining_total_friction_budget_bps": {
            f"{stress:g}": float(stress - weighted_bps)
            for stress in stresses
        },
        "included": "published IBKR base stock commission",
        "excluded": [
            "exchange and regulatory fees",
            "bid-ask spread",
            "fill slippage versus arrival midpoint",
            "market impact",
            "FX conversion",
        ],
        "commission_source": IBKR_US_STOCK_COMMISSION_SOURCE,
        "schedule_accessed_at": SCHEDULE_ACCESSED_AT,
        "warning": (
            "This is an offline cost reference. It does not connect to IBKR, "
            "create an order, or authorize trading."
        ),
    }
    return frame, summary
