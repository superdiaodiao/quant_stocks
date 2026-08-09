"""Create a local, non-executable share plan from a recommendation file."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.strategy.common import CASH_SENTINEL


def _file_fingerprint(path: str | Path) -> dict:
    """Return stable input identity for a reference-only plan."""
    target = Path(path)
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(target),
        "bytes": target.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def build_manual_position_plan(
    recommendations: pd.DataFrame,
    account_equity_usd: float,
    holdings: pd.DataFrame | None = None,
    *,
    transaction_cost_bps: float = 10.0,
    fractional_shares: bool = False,
) -> tuple[pd.DataFrame, dict]:
    """Return reference target shares without creating broker instructions."""
    if not np.isfinite(account_equity_usd) or account_equity_usd <= 0:
        raise ValueError("account_equity_usd must be positive and finite")
    if transaction_cost_bps < 0 or not np.isfinite(transaction_cost_bps):
        raise ValueError("transaction_cost_bps must be finite and non-negative")
    required = {"ticker", "target_weight", "current_price"}
    missing = required - set(recommendations.columns)
    if missing:
        raise ValueError(
            f"recommendations missing columns: {sorted(missing)}"
        )
    targets = recommendations.loc[
        recommendations["ticker"].astype(str).ne(CASH_SENTINEL)
    ].copy()
    targets["ticker"] = targets["ticker"].astype(str).str.upper().str.strip()
    if targets["ticker"].duplicated().any():
        raise ValueError("recommendations contain duplicate tickers")
    targets["target_weight"] = pd.to_numeric(
        targets["target_weight"], errors="coerce"
    )
    targets["current_price"] = pd.to_numeric(
        targets["current_price"], errors="coerce"
    )
    weights = targets.set_index("ticker")["target_weight"]
    if (
        weights.isna().any()
        or (~np.isfinite(weights)).any()
        or weights.lt(0).any()
        or weights.sum() > 1 + 1e-9
    ):
        raise ValueError(
            "target weights must be finite, non-negative, and sum to at most 1"
        )

    if holdings is None:
        current = pd.DataFrame(
            columns=["ticker", "shares", "current_price"]
        )
    else:
        current = holdings.copy()
        holding_required = {"ticker", "shares"}
        missing = holding_required - set(current.columns)
        if missing:
            raise ValueError(f"holdings missing columns: {sorted(missing)}")
        if "current_price" not in current:
            current["current_price"] = np.nan
    current["ticker"] = current["ticker"].astype(str).str.upper().str.strip()
    if current["ticker"].duplicated().any():
        raise ValueError("holdings contain duplicate tickers")
    current["shares"] = pd.to_numeric(current["shares"], errors="coerce")
    current["current_price"] = pd.to_numeric(
        current["current_price"], errors="coerce"
    )
    if (
        current["shares"].isna().any()
        or (~np.isfinite(current["shares"])).any()
        or current["shares"].lt(0).any()
    ):
        raise ValueError("holding shares must be finite and non-negative")

    target_prices = targets.set_index("ticker")["current_price"]
    holding_prices = current.set_index("ticker")["current_price"]
    tickers = sorted(set(weights.index) | set(current["ticker"]))
    frame = pd.DataFrame(index=pd.Index(tickers, name="ticker"))
    frame["target_weight"] = weights.reindex(frame.index).fillna(0.0)
    frame["current_shares"] = (
        current.set_index("ticker")["shares"].reindex(frame.index).fillna(0.0)
    )
    frame["current_price"] = target_prices.reindex(frame.index).combine_first(
        holding_prices.reindex(frame.index)
    )
    needs_price = frame["target_weight"].gt(0) | frame["current_shares"].gt(0)
    invalid_price = (
        frame["current_price"].isna()
        | (~np.isfinite(frame["current_price"]))
        | frame["current_price"].le(0)
    )
    if (needs_price & invalid_price).any():
        raise ValueError(
            "missing positive current prices for: "
            f"{sorted(frame.index[needs_price & invalid_price])}"
        )
    frame["current_value"] = (
        frame["current_shares"] * frame["current_price"]
    ).fillna(0.0)

    cost_rate = transaction_cost_bps / 10_000
    post_trade_equity = float(account_equity_usd)
    for _ in range(100):
        desired = frame["target_weight"] * post_trade_equity
        traded = float((desired - frame["current_value"]).abs().sum())
        updated = account_equity_usd - traded * cost_rate
        if abs(updated - post_trade_equity) < 1e-9:
            post_trade_equity = updated
            break
        post_trade_equity = updated
    target_value = frame["target_weight"] * post_trade_equity
    fractional_target = (
        target_value / frame["current_price"]
    ).where(frame["target_weight"].gt(0), 0.0)
    frame["target_shares"] = (
        fractional_target
        if fractional_shares
        else np.floor(fractional_target + 1e-12)
    )
    frame["share_change_reference"] = (
        frame["target_shares"] - frame["current_shares"]
    )
    frame["target_value"] = (
        frame["target_shares"] * frame["current_price"]
    ).fillna(0.0)
    frame["trade_notional_reference"] = (
        frame["share_change_reference"].abs() * frame["current_price"]
    ).fillna(0.0)
    frame["estimated_transaction_cost"] = (
        frame["trade_notional_reference"] * cost_rate
    )
    frame["reference_action"] = np.select(
        [
            frame["share_change_reference"].gt(1e-12),
            frame["share_change_reference"].lt(-1e-12),
        ],
        ["REFERENCE_INCREASE", "REFERENCE_DECREASE"],
        default="REFERENCE_HOLD",
    )
    liquidity = targets.set_index("ticker").get(
        "current_median_dollar_volume_50d",
        pd.Series(dtype=float),
    )
    frame["current_median_dollar_volume_50d"] = pd.to_numeric(
        liquidity.reindex(frame.index), errors="coerce"
    )
    frame["full_target_participation_for_account"] = (
        frame["target_weight"]
        * account_equity_usd
        / frame["current_median_dollar_volume_50d"]
    )
    total_cost = float(frame["estimated_transaction_cost"].sum())
    residual_cash = float(
        account_equity_usd - frame["target_value"].sum() - total_cost
    )
    if residual_cash < -1e-6:
        raise ValueError(
            "reference plan exceeds account equity after estimated costs"
        )
    summary = {
        "status": "REFERENCE_ONLY_NOT_AN_ORDER",
        "account_equity_usd": float(account_equity_usd),
        "transaction_cost_bps": float(transaction_cost_bps),
        "fractional_shares": bool(fractional_shares),
        "target_exposure": float(frame["target_weight"].sum()),
        "estimated_turnover_notional": float(
            frame["trade_notional_reference"].sum()
        ),
        "estimated_transaction_cost": total_cost,
        "estimated_residual_cash": max(residual_cash, 0.0),
        "warning": (
            "Prices are references, fills can differ, and this file is not "
            "a broker order or authorization to trade."
        ),
    }
    return frame.reset_index(), summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a local reference-only position plan."
    )
    parser.add_argument("--recommendations", required=True)
    parser.add_argument("--account-equity-usd", required=True, type=float)
    parser.add_argument("--holdings")
    parser.add_argument("--transaction-cost-bps", type=float, default=10.0)
    parser.add_argument("--fractional-shares", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    recommendations = pd.read_csv(args.recommendations)
    holdings = pd.read_csv(args.holdings) if args.holdings else None
    plan, summary = build_manual_position_plan(
        recommendations,
        args.account_equity_usd,
        holdings,
        transaction_cost_bps=args.transaction_cost_bps,
        fractional_shares=args.fractional_shares,
    )
    summary["provenance_format_version"] = 1
    summary["input_provenance"] = {
        "recommendations": _file_fingerprint(args.recommendations),
        "holdings": (
            _file_fingerprint(args.holdings) if args.holdings else None
        ),
    }
    for column, label in (
        ("portfolio_strategy_sha256", "strategy_sha256"),
        ("portfolio_data_manifest_sha256", "data_manifest_sha256"),
    ):
        if column in recommendations:
            values = recommendations[column].dropna().astype(str).unique()
            if len(values) == 1:
                summary[label] = values[0]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    plan.to_csv(output, index=False)
    output.with_suffix(".json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
