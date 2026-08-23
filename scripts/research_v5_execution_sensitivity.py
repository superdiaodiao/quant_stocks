#!/usr/bin/env python3
"""Audit whole-share target rounding for the research-v5 allocation path."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR
from src.io.security_identity import issuer_rename_transitions
from src.io.terminal_returns import observed_terminal_return_map


DEFAULT_SELECTED = Path(
    "output/research_v4_cost_robust_top10_proven_only_bank_v3_selected_ledger.csv"
)
DEFAULT_V5_DAILY = Path("output/research_v5_qqq_relative_trend_core_30bps_daily.csv")
DEFAULT_QQQ = Path("output/research_only/qqq_nasdaq_history.csv")
DEFAULT_OUTPUT = Path("output/research_v5_execution_sensitivity.json")
CORPORATE_ACTIONS_PATH = Path(
    "stocks_list_dir/nasdaq/corporate_actions.csv"
)
ACCOUNT_SIZES = (10_000.0, 25_000.0, 100_000.0)
EVALUATION_START = pd.Timestamp("2022-01-01")


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _price_on(ticker: str, effective_date: pd.Timestamp) -> float:
    path = Path(CLEANED_PRICE_DATA_DIR) / f"{ticker.lower()}.csv"
    frame = pd.read_csv(path, usecols=["date", "close"], parse_dates=["date"])
    values = frame.loc[frame["date"].eq(effective_date), "close"]
    if values.empty or not np.isfinite(values.iloc[-1]) or values.iloc[-1] <= 0:
        raise ValueError(f"missing positive execution close for {ticker} on {effective_date.date()}")
    return float(values.iloc[-1])


def build_target_panel(
    selected: pd.DataFrame,
    allocation: pd.DataFrame,
    qqq_close: pd.Series,
) -> pd.DataFrame:
    selected = selected.copy()
    selected["effective_date"] = pd.to_datetime(selected["effective_date"])
    selected = selected.loc[selected["effective_date"].ge(EVALUATION_START)]
    allocation = allocation.copy()
    allocation.index = pd.to_datetime(allocation.index)
    allocation = allocation.loc[allocation.index >= EVALUATION_START]
    allocation = allocation[["satellite_weight", "core_weight"]]
    selected_by_date = {
        effective_date: positions
        for effective_date, positions in selected.groupby("effective_date")
    }
    month_starts = allocation.groupby(allocation.index.to_period("M")).apply(
        lambda frame: frame.index.min(), include_groups=False
    )
    rows = []
    for effective_date in month_starts:
        positions = selected_by_date.get(effective_date, selected.iloc[0:0])
        weights = allocation.loc[effective_date]
        if isinstance(weights, pd.DataFrame):
            weights = weights.iloc[-1]
        rows_before = len(rows)
        for position in positions.itertuples(index=False):
            rows.append({
                "effective_date": effective_date,
                "ticker": str(position.ticker),
                "sleeve": "v4",
                "target_weight": float(position.target_weight) * float(weights["satellite_weight"]),
                "execution_close": _price_on(str(position.ticker), effective_date),
            })
        core_weight = float(weights["core_weight"])
        if core_weight > 0:
            qqq_price = qqq_close.reindex([effective_date]).iloc[0]
            if not np.isfinite(qqq_price) or qqq_price <= 0:
                raise ValueError(f"QQQ close missing on {effective_date.date()}")
            rows.append({
                "effective_date": effective_date,
                "ticker": "QQQ",
                "sleeve": "core",
                "target_weight": core_weight,
                "execution_close": float(qqq_price),
            })
        if len(rows) == rows_before:
            rows.append({
                "effective_date": effective_date,
                "ticker": "__CASH__",
                "sleeve": "cash",
                "target_weight": 0.0,
                "execution_close": 1.0,
            })
    result = pd.DataFrame(rows)
    if result.empty:
        raise ValueError("no v5 execution targets in evaluation window")
    return result.sort_values(["effective_date", "sleeve", "ticker"]).reset_index(drop=True)


def load_stock_close_panel(
    tickers: list[str],
    index: pd.DatetimeIndex,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
) -> pd.DataFrame:
    panel = pd.DataFrame({
        ticker: pd.read_csv(
            Path(price_dir) / f"{ticker.lower()}.csv",
            usecols=["date", "close"],
            parse_dates=["date"],
        ).set_index("date")["close"]
        for ticker in tickers
    }).reindex(index)
    return panel


def simulate_continuous_whole_share(
    targets: pd.DataFrame,
    stock_close: pd.DataFrame,
    qqq_close: pd.Series,
    qqq_dividend: pd.Series,
    benchmark_return: pd.Series,
    *,
    account_size: float,
    transaction_cost_bps: float = 30.0,
    execution_slippage_bps: float = 0.0,
    fill_fraction: float = 1.0,
    identity_transitions: pd.DataFrame | None = None,
    corporate_actions: pd.DataFrame | None = None,
    terminal_returns: dict[tuple[str, pd.Timestamp], float] | None = None,
) -> pd.DataFrame:
    if account_size <= 0 or not np.isfinite(account_size):
        raise ValueError("account_size must be finite and positive")
    if transaction_cost_bps < 0 or not np.isfinite(transaction_cost_bps):
        raise ValueError("transaction_cost_bps must be finite and non-negative")
    if execution_slippage_bps < 0 or not np.isfinite(execution_slippage_bps):
        raise ValueError("execution_slippage_bps must be finite and non-negative")
    if not np.isfinite(fill_fraction) or not 0 < fill_fraction <= 1:
        raise ValueError("fill_fraction must be in (0, 1]")
    index = benchmark_return.index
    stock_close = stock_close.reindex(index)
    qqq_close = qqq_close.reindex(index)
    qqq_dividend = qqq_dividend.reindex(index).fillna(0.0)
    zero_market_rows = benchmark_return.fillna(0.0).eq(0.0)
    missing_qqq = qqq_close.isna()
    if (missing_qqq & ~zero_market_rows).any():
        raise ValueError("QQQ price missing on a non-zero benchmark session")
    qqq_close = qqq_close.ffill()
    prices = stock_close.copy()
    prices["QQQ"] = qqq_close
    valuation = prices.ffill()
    target_schedule = {
        pd.Timestamp(effective_date): group.set_index("ticker")["target_weight"].astype(float)
        for effective_date, group in targets.groupby("effective_date")
    }
    identity_transitions = (
        issuer_rename_transitions()
        if identity_transitions is None
        else identity_transitions.copy()
    )
    corporate_actions = (
        pd.read_csv(
            CORPORATE_ACTIONS_PATH,
            parse_dates=["last_price_date", "effective_date"],
        )
        if corporate_actions is None
        else corporate_actions.copy()
    )
    corporate_actions["last_price_date"] = pd.to_datetime(
        corporate_actions["last_price_date"]
    )
    corporate_actions["effective_date"] = pd.to_datetime(
        corporate_actions["effective_date"]
    )
    terminal_returns = (
        observed_terminal_return_map()
        if terminal_returns is None
        else dict(terminal_returns)
    )
    last_price_dates = {
        ticker: prices[ticker].dropna().index.max()
        for ticker in prices.columns
        if ticker != "QQQ" and prices[ticker].notna().any()
    }
    shares = pd.Series(0, index=prices.columns, dtype=int)
    cash = float(account_size)
    nav = float(account_size)
    cost_rate = transaction_cost_bps / 10_000.0
    slippage_rate = execution_slippage_bps / 10_000.0
    pending_target_shares: pd.Series | None = None
    rows = []
    for stamp in index:
        previous_nav = nav
        for transition in identity_transitions.itertuples(index=False):
            if stamp != transition.current_ticker_first_date:
                continue
            old = str(transition.historical_ticker)
            new = str(transition.provider_ticker)
            if old not in shares or new not in shares or shares[old] <= 0:
                continue
            shares[new] += shares[old]
            shares[old] = 0
        for action in corporate_actions.itertuples(index=False):
            if stamp != action.effective_date:
                continue
            old = str(action.predecessor)
            new = str(action.successor)
            if old not in shares or shares[old] <= 0:
                continue
            old_shares = int(shares[old])
            cash += old_shares * float(action.cash_per_share)
            successor_entitlement = old_shares * float(action.share_ratio)
            whole_successor = int(np.floor(successor_entitlement))
            fractional_successor = successor_entitlement - whole_successor
            if whole_successor or fractional_successor:
                if new not in shares:
                    raise ValueError(f"successor price column missing for {new}")
                successor_price = valuation.loc[stamp, new]
                if not np.isfinite(successor_price) or successor_price <= 0:
                    raise ValueError(f"successor price missing for {new} on {stamp.date()}")
                shares[new] += whole_successor
                cash += fractional_successor * float(successor_price)
            shares[old] = 0
        for ticker in shares.index[shares.gt(0)]:
            if ticker == "QQQ" or pd.notna(prices.loc[stamp, ticker]):
                continue
            last_date = last_price_dates.get(str(ticker))
            terminal_return = terminal_returns.get((str(ticker), last_date))
            pending_action = corporate_actions.loc[
                corporate_actions["predecessor"].eq(ticker)
                & corporate_actions["last_price_date"].eq(last_date)
                & corporate_actions["effective_date"].ge(stamp)
            ]
            if terminal_return is None or not pending_action.empty:
                continue
            last_price = float(valuation.loc[stamp, ticker])
            cash += int(shares[ticker]) * last_price * (1.0 + terminal_return)
            shares[ticker] = 0
        current_prices = valuation.loc[stamp]
        held = shares.gt(0)
        raw_current_prices = prices.loc[stamp]
        missing_held = (
            held & raw_current_prices.isna()
            if not bool(zero_market_rows.loc[stamp])
            else pd.Series(False, index=held.index)
        )
        if missing_held.any():
            for ticker in missing_held.index[missing_held]:
                pending = corporate_actions.loc[
                    corporate_actions["predecessor"].eq(ticker)
                    & corporate_actions["last_price_date"].lt(stamp)
                    & corporate_actions["effective_date"].ge(stamp)
                ]
                if not pending.empty:
                    missing_held.loc[ticker] = False
        if missing_held.any():
            raise ValueError(
                "held security price missing: "
                + ", ".join(current_prices.index[missing_held])
                + f" on {stamp.date()}"
            )
        if shares.get("QQQ", 0) > 0:
            cash += float(shares["QQQ"] * qqq_dividend.loc[stamp])
        position_values = shares.astype(float) * current_prices.fillna(0.0)
        pre_trade_nav = float(cash + position_values.sum())
        turnover = 0.0
        transaction_cost = 0.0
        slippage_cost = 0.0
        requested_deltas = pd.Series(0, index=shares.index, dtype=int)
        deltas = requested_deltas.copy()
        scheduled = target_schedule.get(stamp)
        if scheduled is not None:
            target = pd.Series(0.0, index=prices.columns)
            target.update(scheduled)
            estimate = pre_trade_nav
            target_shares = shares.copy()
            for _ in range(30):
                desired = np.floor(
                    target.mul(estimate).div(current_prices).fillna(0.0)
                ).astype(int)
                traded_notional = float(
                    (desired - shares).abs().mul(current_prices.fillna(0.0)).sum()
                )
                updated = pre_trade_nav - traded_notional * (
                    cost_rate + slippage_rate
                )
                if desired.equals(target_shares) and abs(updated - estimate) < 1e-8:
                    target_shares = desired
                    estimate = updated
                    break
                target_shares = desired
                estimate = updated
            pending_target_shares = target_shares.copy()
        if pending_target_shares is not None:
            requested_deltas = pending_target_shares - shares
            if fill_fraction < 1.0:
                filled_magnitude = np.ceil(
                    requested_deltas.abs() * fill_fraction
                ).astype(int)
                deltas = requested_deltas.apply(np.sign).astype(int) * filled_magnitude
                target_shares = shares + deltas
            else:
                deltas = requested_deltas
                target_shares = pending_target_shares.copy()
            traded_notional = float(
                deltas.abs().mul(current_prices.fillna(0.0)).sum()
            )
            transaction_cost = traded_notional * cost_rate
            slippage_cost = traded_notional * slippage_rate
            turnover = traded_notional / pre_trade_nav if pre_trade_nav else 0.0
            target_values = target_shares.astype(float) * current_prices.fillna(0.0)
            cash = float(
                pre_trade_nav - target_values.sum() - transaction_cost - slippage_cost
            )
            while cash < -1e-6:
                buyable = deltas.loc[deltas.gt(0)]
                if buyable.empty:
                    break
                ticker = current_prices.reindex(buyable.index).idxmax()
                target_shares[ticker] -= 1
                deltas[ticker] -= 1
                traded_notional = float(
                    deltas.abs().mul(current_prices.fillna(0.0)).sum()
                )
                transaction_cost = traded_notional * cost_rate
                slippage_cost = traded_notional * slippage_rate
                turnover = traded_notional / pre_trade_nav if pre_trade_nav else 0.0
                target_values = target_shares.astype(float) * current_prices.fillna(0.0)
                cash = float(
                    pre_trade_nav
                    - target_values.sum()
                    - transaction_cost
                    - slippage_cost
                )
            if cash < -1e-6:
                raise ValueError("whole-share rebalance produced negative cash")
            shares = target_shares
            position_values = target_values
            if shares.equals(pending_target_shares):
                pending_target_shares = None
        nav = float(cash + position_values.sum())
        rows.append({
            "date": stamp,
            "return": nav / previous_nav - 1.0 if previous_nav else 0.0,
            "benchmark_return": float(benchmark_return.loc[stamp]),
            "nav": nav,
            "cash": cash,
            "invested_fraction": float(position_values.sum() / nav) if nav else 0.0,
            "holdings": int(shares.gt(0).sum()),
            "turnover": turnover,
            "transaction_cost": transaction_cost,
            "slippage_cost": slippage_cost,
            "requested_share_delta": int(requested_deltas.abs().sum()),
            "filled_share_delta": int(deltas.abs().sum()),
        })
    result = pd.DataFrame(rows).set_index("date")
    result["drawdown"] = result["nav"].div(result["nav"].cummax()).sub(1.0)
    return result


def summarize_continuous_path(
    result: pd.DataFrame,
    fractional_return: pd.Series,
) -> dict:
    evaluation = result.loc[result.index >= EVALUATION_START]
    fractional = fractional_return.reindex(evaluation.index)
    annual = pd.DataFrame({
        "whole_share": evaluation["return"],
        "fractional": fractional,
        "nasdaq": evaluation["benchmark_return"],
    }).groupby(evaluation.index.year).apply(
        lambda group: pd.Series({
            column: float((1.0 + group[column]).prod() - 1.0)
            for column in group.columns
        }),
        include_groups=False,
    )
    annual["whole_share_excess_vs_nasdaq"] = annual["whole_share"] - annual["nasdaq"]
    annual["whole_minus_fractional"] = annual["whole_share"] - annual["fractional"]
    drawdown = evaluation["nav"].div(evaluation["nav"].cummax()).sub(1.0)
    return {
        "wins_vs_nasdaq": int(annual["whole_share_excess_vs_nasdaq"].gt(0).sum()),
        "median_excess_vs_nasdaq": float(annual["whole_share_excess_vs_nasdaq"].median()),
        "minimum_excess_vs_nasdaq": float(annual["whole_share_excess_vs_nasdaq"].min()),
        "maximum_drawdown": float(drawdown.min()),
        "time_underwater_fraction": float(drawdown.lt(0).mean()),
        "final_nav": float(result["nav"].iloc[-1]),
        "cumulative_transaction_cost": float(result["transaction_cost"].sum()),
        "cumulative_slippage_cost": float(result["slippage_cost"].sum()),
        "requested_shares": int(result["requested_share_delta"].sum()),
        "filled_shares": int(result["filled_share_delta"].sum()),
        "annual": annual.reset_index(names="year").to_dict(orient="records"),
    }


def summarize_whole_share_rounding(
    targets: pd.DataFrame,
    account_size: float,
) -> dict:
    if not np.isfinite(account_size) or account_size <= 0:
        raise ValueError("account_size must be finite and positive")
    frame = targets.copy()
    frame["target_notional"] = frame["target_weight"] * account_size
    frame["whole_shares"] = np.floor(
        frame["target_notional"] / frame["execution_close"]
    ).astype(int)
    frame["realized_notional"] = frame["whole_shares"] * frame["execution_close"]
    frame["rounding_shortfall"] = frame["target_notional"] - frame["realized_notional"]
    frame["rounding_shortfall_bps"] = frame["rounding_shortfall"] / account_size * 10_000.0
    stock = frame.loc[frame["sleeve"].eq("v4")]
    periods = frame.groupby("effective_date").agg(
        target_weight=("target_weight", "sum"),
        realized_notional=("realized_notional", "sum"),
        rounding_shortfall=("rounding_shortfall", "sum"),
    )
    periods["rounding_cash_drag_fraction"] = periods["rounding_shortfall"] / account_size
    zero_by_period = stock.assign(zero=stock["whole_shares"].eq(0)).groupby(
        "effective_date"
    )["zero"].sum()
    return {
        "account_size": account_size,
        "target_rows": int(len(frame)),
        "stock_target_rows": int(len(stock)),
        "zero_share_stock_targets": int(stock["whole_shares"].eq(0).sum()),
        "zero_share_stock_target_fraction": float(stock["whole_shares"].eq(0).mean()),
        "periods": int(len(periods)),
        "periods_with_unbuyable_stock": int(zero_by_period.gt(0).sum()),
        "period_fraction_with_unbuyable_stock": float(zero_by_period.gt(0).mean()),
        "median_position_rounding_shortfall_bps": float(frame["rounding_shortfall_bps"].median()),
        "maximum_position_rounding_shortfall_bps": float(frame["rounding_shortfall_bps"].max()),
        "median_period_rounding_cash_drag_fraction": float(periods["rounding_cash_drag_fraction"].median()),
        "maximum_period_rounding_cash_drag_fraction": float(periods["rounding_cash_drag_fraction"].max()),
    }


def run(
    *,
    selected_path: str | Path = DEFAULT_SELECTED,
    v5_daily_path: str | Path = DEFAULT_V5_DAILY,
    qqq_path: str | Path = DEFAULT_QQQ,
    output_path: str | Path = DEFAULT_OUTPUT,
) -> dict:
    selected_path = Path(selected_path)
    v5_daily_path = Path(v5_daily_path)
    qqq_path = Path(qqq_path)
    output_path = Path(output_path)
    selected = pd.read_csv(selected_path)
    allocation = pd.read_csv(v5_daily_path, parse_dates=["date"]).set_index("date")
    qqq_frame = pd.read_csv(qqq_path, parse_dates=["date"]).set_index("date")
    qqq_close = qqq_frame["close"]
    qqq_dividend = qqq_frame.get(
        "cash_dividend", pd.Series(0.0, index=qqq_frame.index)
    )
    targets = build_target_panel(selected, allocation, qqq_close)
    stock_tickers = sorted(
        targets.loc[targets["sleeve"].eq("v4"), "ticker"].unique()
    )
    transitions = issuer_rename_transitions()
    transition_rows = transitions.loc[
        transitions["historical_ticker"].isin(stock_tickers)
    ]
    corporate_actions = pd.read_csv(
        CORPORATE_ACTIONS_PATH, parse_dates=["effective_date"]
    )
    relevant_actions = corporate_actions.loc[
        corporate_actions["predecessor"].isin(stock_tickers)
    ]
    stock_tickers = sorted(
        set(stock_tickers)
        | set(transition_rows["provider_ticker"])
        | set(relevant_actions["successor"].dropna().astype(str))
    )
    stock_close = load_stock_close_panel(stock_tickers, allocation.index)
    continuous_results = {}
    execution_stress_results = {}
    for size in ACCOUNT_SIZES:
        path = simulate_continuous_whole_share(
            targets,
            stock_close,
            qqq_close,
            qqq_dividend,
            allocation["benchmark_return"],
            account_size=size,
            transaction_cost_bps=30.0,
            identity_transitions=transitions,
            corporate_actions=corporate_actions,
        )
        continuous_results[str(int(size))] = summarize_continuous_path(
            path, allocation["return"]
        )
        stressed_path = simulate_continuous_whole_share(
            targets,
            stock_close,
            qqq_close,
            qqq_dividend,
            allocation["benchmark_return"],
            account_size=size,
            transaction_cost_bps=30.0,
            execution_slippage_bps=10.0,
            fill_fraction=0.75,
            identity_transitions=transitions,
            corporate_actions=corporate_actions,
        )
        execution_stress_results[str(int(size))] = summarize_continuous_path(
            stressed_path, allocation["return"]
        )
    result = {
        "schema_version": 1,
        "research_only": True,
        "scope": "monthly_target_rounding_and_continuous_whole_share_close_replay",
        "fractional_share_baseline": "exact_target_weights_without_rounding_drag",
        "whole_share_results": {
            str(int(size)): summarize_whole_share_rounding(targets, size)
            for size in ACCOUNT_SIZES
        },
        "continuous_whole_share_30bps": continuous_results,
        "execution_stress": {
            "transaction_cost_bps": 30.0,
            "additional_slippage_bps": 10.0,
            "deterministic_fill_fraction": 0.75,
            "rounding_rule": (
                "ceil_each_requested_share_delta_times_fill_fraction; "
                "retry_remaining shares on subsequent sessions"
            ),
            "results": execution_stress_results,
        },
        "target_panel": {
            "rows": int(len(targets)),
            "periods": int(targets["effective_date"].nunique()),
            "minimum_date": targets["effective_date"].min().strftime("%Y-%m-%d"),
            "maximum_date": targets["effective_date"].max().strftime("%Y-%m-%d"),
        },
        "selected_path_integrity": {
            "positions_with_missing_holding_prices": int(
                selected["missing_holding_prices"].fillna(0).gt(0).sum()
            ),
            "positions_with_unresolved_terminal_return": int(
                selected["unresolved_terminal_while_held"].fillna(False).astype(bool).sum()
            ),
        },
        "inputs": {
            "selected_ledger": {"path": str(selected_path.resolve()), "sha256": _sha256(selected_path)},
            "v5_daily": {"path": str(v5_daily_path.resolve()), "sha256": _sha256(v5_daily_path)},
            "qqq_price": {"path": str(qqq_path.resolve()), "sha256": _sha256(qqq_path)},
        },
        "interpretation": (
            "The continuous replay holds integer shares between monthly close rebalances, "
            "keeps rounding residuals in cash, includes QQQ cash dividends, and charges the "
            "declared transaction cost. One deterministic 75% partial-fill plus 10 bps "
            "slippage stress is reported separately; it is not empirical broker fill, "
            "market-impact, cutoff, or order-rejection evidence."
        ),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selected", type=Path, default=DEFAULT_SELECTED)
    parser.add_argument("--v5-daily", type=Path, default=DEFAULT_V5_DAILY)
    parser.add_argument("--qqq", type=Path, default=DEFAULT_QQQ)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(run(
        selected_path=args.selected,
        v5_daily_path=args.v5_daily,
        qqq_path=args.qqq,
        output_path=args.output,
    ), indent=2))


if __name__ == "__main__":
    main()
