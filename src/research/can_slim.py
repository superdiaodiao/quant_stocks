"""A reproducible CAN SLIM-inspired scheduled stock selector.

This implements public, observable CAN SLIM ideas only.  It does not claim to
replicate IBD's proprietary ratings, chart annotations, or recommendations.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.financial.eps import eps_snapshot
from src.financial.quarterly_fundamentals import quarterly_growth_snapshot
from src.io.security_identity import (
    issuer_rename_transitions,
    remap_weights_after_issuer_rename,
)
from src.research.data_quality import back_adjust_common_splits, stock_returns_with_delisting_penalty
from src.strategy.common import market_regime_is_on, next_trading_date, scheduled_signal_dates


def calculate_keltner_upper_panel(
    close: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    window: int = 20,
    atr_window: int = 14,
    multiplier: float = 1.5,
) -> pd.DataFrame:
    """Prior-close Keltner upper band for all symbols, computed once per replay."""
    prior_close = close.shift(1)
    true_range = (high - low).where(
        (high - low) >= (high - prior_close).abs(),
        (high - prior_close).abs(),
    ).where(
        lambda current: current >= (low - prior_close).abs(),
        (low - prior_close).abs(),
    )
    middle = close.rolling(window).mean().shift(1)
    atr = true_range.ewm(
        alpha=1 / atr_window, adjust=False, min_periods=atr_window
    ).mean().shift(1)
    return middle + multiplier * atr


@dataclass(frozen=True)
class CanSlimConfig:
    start: str = "2021-01-01"
    end: str = "2026-07-17"
    top_n: int = 10
    market_ma_days: int = 200
    transaction_cost_bps: float = 10.0
    minimum_price: float = 10.0
    minimum_median_dollar_volume: float = 2_000_000.0
    minimum_eps_growth: float = 0.25
    minimum_relative_volume: float = 0.80
    minimum_52_week_high_ratio: float = 0.85
    maximum_financial_age_days: int = 550
    maximum_position_weight: float = 0.20
    signal_frequency: str = "monthly"
    use_quarterly_fundamentals: bool = False
    minimum_revenue_growth: float = 0.10
    price_channel: str = "none"
    keltner_window: int = 20
    keltner_atr_window: int = 14
    keltner_multiplier: float = 1.5
    selection_mode: str = "growth"
    ensemble_weight: float = 1.0


def build_can_slim_technical_cross_section(
    date: pd.Timestamp,
    close: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    index_close: pd.Series,
    config: CanSlimConfig,
    eligible_symbols: set[str] | None = None,
    keltner_upper: pd.DataFrame | None = None,
    eligibility_close: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build the point-in-time non-financial cross section used by CAN SLIM."""
    history = close.loc[:date]
    if len(history) < 253:
        return pd.DataFrame()
    index_history = index_close.reindex(history.index).ffill()
    if index_history.isna().any():
        return pd.DataFrame()
    dollar_history = dollar_volume.loc[:date]
    eligibility_history = (
        eligibility_close.loc[:date].reindex_like(history)
        if eligibility_close is not None else history
    )
    frame = pd.DataFrame({
        "price": history.iloc[-1],
        "eligibility_price": eligibility_history.iloc[-1],
        "median_dollar_volume_50d": dollar_history.iloc[-50:].median(),
        "relative_volume": dollar_history.iloc[-1] / dollar_history.iloc[-50:].median(),
        "high_52_week": history.iloc[-252:].max(),
        "stock_return_12_1": history.iloc[-22] / history.iloc[-253] - 1,
        "stock_return_3m": history.iloc[-1] / history.iloc[-64] - 1,
    }).replace([np.inf, -np.inf], np.nan)
    frame["near_52_week_high"] = frame["price"] / frame["high_52_week"]
    index_return = index_history.iloc[-22] / index_history.iloc[-253] - 1
    frame["relative_strength_12_1"] = frame["stock_return_12_1"] - index_return
    index_return_3m = index_history.iloc[-1] / index_history.iloc[-64] - 1
    frame["relative_strength_3m"] = frame["stock_return_3m"] - index_return_3m
    if config.price_channel == "keltner":
        if keltner_upper is None:
            raise ValueError("Keltner scoring requires a precomputed upper-band panel")
        frame["keltner_upper"] = keltner_upper.reindex_like(close).loc[date]
        frame["keltner_breakout"] = frame["price"] > frame["keltner_upper"]
    else:
        frame["keltner_breakout"] = True
    if eligible_symbols is not None:
        frame = frame.loc[frame.index.intersection(sorted(eligible_symbols))]
    return frame


def can_slim_nonfinancial_candidate_mask(
    frame: pd.DataFrame,
    config: CanSlimConfig,
) -> pd.Series:
    """Return symbols that could qualify if their financial tests passed."""
    mask = (
        frame["eligibility_price"].ge(config.minimum_price)
        & frame["median_dollar_volume_50d"].ge(
            config.minimum_median_dollar_volume
        )
        & frame["relative_volume"].ge(config.minimum_relative_volume)
        & frame["keltner_breakout"]
    )
    if config.selection_mode == "recovery":
        mask &= frame["relative_strength_3m"].gt(0)
        required = ["price", "median_dollar_volume_50d", "relative_volume",
                    "relative_strength_3m"]
    else:
        mask &= frame["near_52_week_high"].ge(
            config.minimum_52_week_high_ratio
        )
        required = ["price", "median_dollar_volume_50d", "relative_volume",
                    "near_52_week_high", "relative_strength_12_1"]
    return mask & frame[required].notna().all(axis=1)


def score_can_slim_cross_section(
    date: pd.Timestamp,
    close: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    index_close: pd.Series,
    eps: pd.DataFrame,
    config: CanSlimConfig,
    eligible_symbols: set[str] | None = None,
    quarterly_fundamentals: pd.DataFrame | None = None,
    keltner_upper: pd.DataFrame | None = None,
    eligibility_close: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Score C/A/N/S/L signals that were knowable at ``date``.

    C/A use point-in-time trailing EPS growth; N is proximity to a 52-week
    high; S is relative dollar-volume; L is 12-month relative strength.
    Market direction (M) is applied when positions are formed, not here.
    """
    frame = build_can_slim_technical_cross_section(
        date,
        close,
        dollar_volume,
        index_close,
        config,
        eligible_symbols,
        keltner_upper,
        eligibility_close,
    )
    if frame.empty:
        return frame
    eps_known = eps_snapshot(eps, date, config.maximum_financial_age_days)
    if config.use_quarterly_fundamentals and quarterly_fundamentals is not None:
        scored = frame.join(eps_known, how="left")
        quarterly = quarterly_growth_snapshot(
            quarterly_fundamentals, date, config.maximum_financial_age_days
        ).rename(columns={"financial_age_days": "quarterly_financial_age_days"})
        quarterly = quarterly.reindex(columns=[
            "net_income_ttm", "net_income_growth", "revenue_growth",
            "quarterly_financial_age_days",
        ])
        scored = scored.join(quarterly, how="left")
        scored["profit_growth"] = scored["net_income_growth"]
        scored["profit_positive"] = scored["net_income_ttm"].gt(0)
        scored["revenue_ok"] = (
            scored["revenue_growth"] >= config.minimum_revenue_growth
        )
        scored["financial_source"] = "sec_quarterly"
        scored["financial_coverage_ok"] = scored[[
            "net_income_ttm", "net_income_growth", "revenue_growth"
        ]].notna().all(axis=1)
    else:
        scored = frame.join(eps_known, how="inner")
        scored["profit_growth"] = scored["eps_growth"]
        scored["profit_positive"] = scored["trailing_eps"].gt(0)
        scored["revenue_ok"] = True
        scored["financial_source"] = "eps"
        scored["financial_coverage_ok"] = True
    nonfinancial_eligible = can_slim_nonfinancial_candidate_mask(
        scored, config
    )
    financial_eligible = (
        scored["profit_positive"] & scored["financial_coverage_ok"]
    )
    if config.selection_mode == "recovery":
        financial_eligible &= (
            scored["financial_source"].eq("sec_quarterly")
            & scored["revenue_growth"].ge(0)
        )
        scored["leadership_metric"] = scored["relative_strength_3m"]
    else:
        financial_eligible &= (
            scored["profit_growth"].ge(config.minimum_eps_growth)
            & scored["revenue_ok"]
        )
        scored["leadership_metric"] = scored["relative_strength_12_1"]
    eligible = scored.loc[
        nonfinancial_eligible & financial_eligible
    ].dropna(subset=[
        "price", "median_dollar_volume_50d", "relative_volume",
        "near_52_week_high", "leadership_metric", "profit_growth",
    ])
    if eligible.empty:
        return eligible
    eligible["earnings_score"] = eligible["profit_growth"].rank(pct=True)
    eligible["sales_score"] = eligible.get(
        "revenue_growth", pd.Series(index=eligible.index, dtype=float)
    ).rank(pct=True).fillna(0.5)
    eligible["new_high_score"] = eligible["near_52_week_high"].rank(pct=True)
    eligible["supply_demand_score"] = eligible["relative_volume"].rank(pct=True)
    eligible["leadership_score"] = eligible["leadership_metric"].rank(pct=True)
    eligible["score"] = (
        0.25 * eligible["earnings_score"]
        + 0.10 * eligible["sales_score"]
        + 0.25 * eligible["leadership_score"]
        + 0.25 * eligible["new_high_score"]
        + 0.15 * eligible["supply_demand_score"]
    )
    return eligible.sort_values("score", ascending=False)


def select_can_slim_portfolio(
    date: pd.Timestamp,
    close: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    index_close: pd.Series,
    eps: pd.DataFrame,
    config: CanSlimConfig,
    eligible_symbols: set[str] | None = None,
    quarterly_fundamentals: pd.DataFrame | None = None,
    keltner_upper: pd.DataFrame | None = None,
    eligibility_close: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Canonical selector shared by the replay and daily recommendation path."""
    selected = score_can_slim_cross_section(
        date, close, dollar_volume, index_close, eps, config, eligible_symbols,
        quarterly_fundamentals, keltner_upper, eligibility_close,
    ).head(config.top_n).copy()
    selected["target_weight"] = (
        min(1 / len(selected), config.maximum_position_weight)
        if len(selected) else 0.0
    )
    return selected


def select_can_slim_ensemble_portfolio(
    date: pd.Timestamp,
    close: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    index_close: pd.Series,
    eps: pd.DataFrame,
    configs: list[CanSlimConfig],
    eligible_symbols: set[str] | None = None,
    quarterly_fundamentals: pd.DataFrame | None = None,
    keltner_upper: pd.DataFrame | None = None,
    eligibility_close: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Average canonical portfolio weights across time-frozen configurations."""
    if not configs:
        return pd.DataFrame()
    selections = [
        select_can_slim_portfolio(
            date, close, dollar_volume, index_close, eps, config, eligible_symbols,
            quarterly_fundamentals, keltner_upper, eligibility_close,
        )
        for config in configs
    ]
    union = sorted(set().union(*(set(frame.index) for frame in selections)))
    if not union:
        return pd.DataFrame()
    weights = pd.DataFrame(
        {
            config_id: frame["target_weight"].reindex(union).fillna(0.0)
            for config_id, frame in enumerate(selections)
        }
    )
    model_weights = pd.Series(
        [config.ensemble_weight for config in configs], index=weights.columns,
        dtype=float,
    )
    model_weights = model_weights / model_weights.sum()
    stacked = pd.concat(selections, keys=range(len(selections)), names=["config", "ticker"])
    numeric = stacked.select_dtypes(include=["number"]).columns.drop(
        ["target_weight"], errors="ignore"
    )
    result = stacked[numeric].groupby(level="ticker").mean().reindex(union)
    if "financial_source" in stacked.columns:
        result["financial_source"] = stacked["financial_source"].groupby(
            level="ticker"
        ).agg(
            lambda sources: (
                "sec_quarterly"
                if sources.eq("sec_quarterly").any()
                else sources.iloc[0]
            )
        ).reindex(union)
    result["target_weight"] = weights.mul(model_weights, axis=1).sum(axis=1)
    result["ensemble_votes"] = weights.gt(0).sum(axis=1)
    result = result.loc[result["target_weight"] > 0]
    return result.sort_values(
        ["target_weight", "ensemble_votes"], ascending=False
    )


def calculate_can_slim_returns(
    close: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    index_close: pd.Series,
    eps: pd.DataFrame,
    config: CanSlimConfig,
    universe_as_of,
    quarterly_fundamentals: pd.DataFrame | None = None,
    high: pd.DataFrame | None = None,
    low: pd.DataFrame | None = None,
    adjust_splits: bool = True,
    eligibility_close: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Replay scheduled signals with a self-financing buy-and-hold portfolio."""
    result, _ = calculate_can_slim_returns_with_ledger(
        close, dollar_volume, index_close, eps, config, universe_as_of,
        quarterly_fundamentals, high, low, adjust_splits=adjust_splits,
        eligibility_close=eligibility_close,
    )
    return result


def calculate_can_slim_returns_with_ledger(
    close: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    index_close: pd.Series,
    eps: pd.DataFrame,
    config: CanSlimConfig,
    universe_as_of,
    quarterly_fundamentals: pd.DataFrame | None = None,
    high: pd.DataFrame | None = None,
    low: pd.DataFrame | None = None,
    initial_capital: float = 1_000_000.0,
    adjust_splits: bool = True,
    eligibility_close: pd.DataFrame | None = None,
    identity_transitions: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Replay the canonical policy and return its daily series and trade ledger.

    Signals use the scheduled close and execute at the next trading-day close.
    Fractional shares are used so the simulation is independent of account
    size. Between rebalances shares remain fixed and weights drift naturally.
    """
    if initial_capital <= 0:
        raise ValueError("initial_capital must be positive")
    prices = (
        back_adjust_common_splits(close) if adjust_splits else close.copy()
    ).sort_index()
    adjustment = prices / close.reindex_like(prices)
    adjusted_high = high.reindex_like(prices) * adjustment if high is not None else None
    adjusted_low = low.reindex_like(prices) * adjustment if low is not None else None
    keltner_upper = None
    if config.price_channel == "keltner":
        if adjusted_high is None or adjusted_low is None:
            raise ValueError("Keltner replay requires high and low price panels")
        keltner_upper = calculate_keltner_upper_panel(
            prices, adjusted_high, adjusted_low, config.keltner_window,
            config.keltner_atr_window, config.keltner_multiplier,
        )
    dollar_volume = dollar_volume.reindex_like(prices)
    eligibility_close = (
        eligibility_close if eligibility_close is not None else close
    ).reindex_like(prices)
    index_close = index_close.reindex(prices.index).ffill()
    stock_returns = stock_returns_with_delisting_penalty(prices).fillna(0.0)
    identity_transitions = (
        issuer_rename_transitions()
        if identity_transitions is None
        else identity_transitions
    )
    targets: dict[pd.Timestamp, tuple[pd.Series, pd.Timestamp, str]] = {}
    for signal_date in scheduled_signal_dates(
        prices.index, config.start, config.end, config.signal_frequency
    ):
        symbols = universe_as_of(signal_date)
        if symbols is None:
            continue
        selected = select_can_slim_portfolio(
            signal_date, prices, dollar_volume, index_close, eps, config, symbols,
            quarterly_fundamentals,
            keltner_upper,
            eligibility_close,
        )
        effective = next_trading_date(prices.index, signal_date)
        if effective is None:
            continue
        target = pd.Series(0.0, index=prices.columns)
        risk_on = market_regime_is_on(
            signal_date, index_close, config.market_ma_days
        )
        if not selected.empty and risk_on:
            target.loc[selected.index] = selected["target_weight"]
        if target.gt(0).any():
            reason = "MONTHLY_REBALANCE"
        elif not risk_on:
            reason = "MARKET_REGIME_TO_CASH"
        else:
            reason = "NO_QUALIFYING_STOCKS_TO_CASH"
        target = remap_weights_after_issuer_rename(
            target, effective, identity_transitions
        )
        targets[effective] = (target, signal_date, reason)

    valuation_prices = prices.ffill()
    position_values = pd.Series(0.0, index=prices.columns)
    shares = pd.Series(0.0, index=prices.columns)
    average_cost = pd.Series(np.nan, index=prices.columns)
    entry_dates: dict[str, pd.Timestamp] = {}
    cash = float(initial_capital)
    nav = float(initial_capital)
    cost_rate = config.transaction_cost_bps / 10_000
    rows = []
    ledger_rows: list[dict] = []
    trade_id = 0
    for current_date, returns in stock_returns.iterrows():
        previous_nav = nav
        for transition in identity_transitions.itertuples(index=False):
            if current_date != transition.current_ticker_first_date:
                continue
            old = transition.historical_ticker
            new = transition.provider_ticker
            if old not in prices or new not in prices:
                continue
            old_value = float(position_values[old])
            if abs(old_value) <= 1e-12:
                continue
            position_values[new] += old_value
            position_values[old] = 0.0
            old_shares = float(shares[old])
            new_shares = float(shares[new])
            old_basis = (
                old_shares * float(average_cost[old])
                if old_shares > 0 and np.isfinite(average_cost[old])
                else 0.0
            )
            new_basis = (
                new_shares * float(average_cost[new])
                if new_shares > 0 and np.isfinite(average_cost[new])
                else 0.0
            )
            shares[new] = new_shares + old_shares
            shares[old] = 0.0
            if shares[new] > 0:
                average_cost[new] = (old_basis + new_basis) / shares[new]
            average_cost[old] = np.nan
            old_entry = entry_dates.pop(old, None)
            if old_entry is not None:
                current_entry = entry_dates.get(new)
                entry_dates[new] = (
                    min(old_entry, current_entry)
                    if current_entry is not None
                    else old_entry
                )
            old_history = prices.loc[prices.index < current_date, old].dropna()
            if len(old_history):
                old_price = float(old_history.iloc[-1])
                new_price = float(valuation_prices.loc[current_date, new])
                if (
                    np.isfinite(old_price) and old_price > 0
                    and np.isfinite(new_price) and new_price > 0
                ):
                    returns = returns.copy()
                    returns.loc[new] = new_price / old_price - 1
        position_values = position_values.mul(1 + returns)
        pre_trade_nav = float(cash + position_values.sum())
        scheduled = targets.get(current_date)
        turnover = 0.0
        if scheduled is not None:
            target, signal_date, reason = scheduled
            post_trade_nav = pre_trade_nav
            for _ in range(20):
                desired = target * post_trade_nav
                traded = float((desired - position_values).abs().sum())
                updated = pre_trade_nav - traded * cost_rate
                if abs(updated - post_trade_nav) < 1e-8:
                    post_trade_nav = updated
                    break
                post_trade_nav = updated
            desired = target * post_trade_nav
            deltas = desired - position_values
            costs = deltas.abs() * cost_rate
            total_cost = float(costs.sum())
            turnover = float(deltas.abs().sum() / pre_trade_nav) if pre_trade_nav else 0.0
            execution_prices = valuation_prices.loc[current_date]
            trade_indexes = deltas.index[deltas.abs() > 1e-8]
            trade_snapshots = []
            for ticker in trade_indexes:
                price = float(execution_prices[ticker])
                if not np.isfinite(price) or price <= 0:
                    raise ValueError(
                        f"Missing execution price for {ticker} on "
                        f"{current_date.date()}"
                    )
                delta_value = float(deltas[ticker])
                delta_shares = delta_value / price
                old_shares = float(shares[ticker])
                old_average_cost = float(average_cost[ticker])
                old_entry_date = entry_dates.get(str(ticker))
                action = (
                    "BUY" if old_shares <= 1e-12 else "INCREASE"
                ) if delta_shares > 0 else (
                    "SELL" if old_shares + delta_shares <= 1e-12 else "REDUCE"
                )
                realized_pnl = np.nan
                realized_return = np.nan
                if delta_shares < 0 and np.isfinite(old_average_cost):
                    sold_shares = -delta_shares
                    realized_pnl = (
                        (price - old_average_cost) * sold_shares
                        - float(costs[ticker])
                    )
                    realized_return = price / old_average_cost - 1
                trade_snapshots.append((
                    ticker, price, delta_value, delta_shares, old_shares,
                    old_average_cost, old_entry_date, action,
                    realized_pnl, realized_return,
                ))

            cash = float(
                pre_trade_nav - desired.sum() - total_cost
            )
            position_values = desired
            nav = float(cash + position_values.sum())
            for (
                ticker, price, delta_value, delta_shares, old_shares,
                old_average_cost, old_entry_date, action,
                realized_pnl, realized_return,
            ) in trade_snapshots:
                new_shares = old_shares + delta_shares
                if delta_shares > 0:
                    total_basis = (
                        old_shares * old_average_cost
                        if old_shares > 0 and np.isfinite(old_average_cost)
                        else 0.0
                    ) + delta_shares * price
                    average_cost[ticker] = total_basis / new_shares
                    if old_shares <= 1e-12:
                        entry_dates[str(ticker)] = current_date
                elif new_shares <= 1e-12:
                    average_cost[ticker] = np.nan
                    entry_dates.pop(str(ticker), None)
                    new_shares = 0.0
                shares[ticker] = new_shares
                trade_id += 1
                ledger_rows.append({
                    "trade_id": trade_id,
                    "signal_date": signal_date,
                    "execution_date": current_date,
                    "ticker": ticker,
                    "side": "BUY" if delta_shares > 0 else "SELL",
                    "action": action,
                    "reason": reason,
                    "weight_before": (
                        float((position_values[ticker] - delta_value) / pre_trade_nav)
                        if pre_trade_nav else 0.0
                    ),
                    "target_weight_after": float(target[ticker]),
                    "execution_price": price,
                    "shares": abs(delta_shares),
                    "gross_notional": abs(delta_value),
                    "transaction_cost": float(costs[ticker]),
                    "cash_after": cash,
                    "portfolio_value_after": nav,
                    "entry_date": old_entry_date,
                    "entry_price": old_average_cost,
                    "realized_pnl": realized_pnl,
                    "realized_return": realized_return,
                })
        else:
            nav = pre_trade_nav

        rows.append((
            nav / previous_nav - 1 if previous_nav else 0.0,
            float(index_close.pct_change(fill_method=None).loc[current_date])
            if current_date != prices.index[0] else 0.0,
            float(position_values.sum() / nav) if nav else 0.0,
            turnover,
            int((position_values > 1e-8).sum()),
            nav,
            cash,
        ))
    result = pd.DataFrame(rows, index=prices.index, columns=[
        "strategy", "benchmark", "invested", "turnover", "holdings",
        "portfolio_value", "cash",
    ]).loc[config.start:config.end]
    ledger = pd.DataFrame(ledger_rows)
    if not ledger.empty:
        ledger = ledger.loc[
            ledger["execution_date"].between(
                pd.Timestamp(config.start), pd.Timestamp(config.end)
            )
        ].reset_index(drop=True)
    return result, ledger


def calculate_can_slim_scheduled_returns(
    close: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    index_close: pd.Series,
    eps: pd.DataFrame,
    start: str,
    end: str,
    config_as_of,
    universe_as_of,
    signal_frequency: str = "monthly",
    quarterly_fundamentals: pd.DataFrame | None = None,
    high: pd.DataFrame | None = None,
    low: pd.DataFrame | None = None,
    adjust_splits: bool = True,
    eligibility_close: pd.DataFrame | None = None,
    return_targets: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, pd.DataFrame]:
    """Replay a time-frozen parameter schedule without resetting the portfolio."""
    prices = (
        back_adjust_common_splits(close) if adjust_splits else close.copy()
    ).sort_index()
    adjustment = prices / close.reindex_like(prices)
    adjusted_high = high.reindex_like(prices) * adjustment if high is not None else None
    adjusted_low = low.reindex_like(prices) * adjustment if low is not None else None
    keltner_upper = None
    dollar_volume = dollar_volume.reindex_like(prices)
    eligibility_close = (
        eligibility_close if eligibility_close is not None else close
    ).reindex_like(prices)
    index_close = index_close.reindex(prices.index).ffill()
    stock_returns = stock_returns_with_delisting_penalty(prices).fillna(0.0)
    targets: dict[pd.Timestamp, tuple[pd.Series, float]] = {}
    # Include the signal immediately before ``start``.  A live run on the first
    # trading day of a new snapshot period scores the last completed
    # day/week/month with the newly effective frozen parameters and trades at
    # the next close; the walk-forward replay must do exactly the same.
    replay_start = pd.Timestamp(start) - pd.Timedelta(days=62)
    for signal_date in scheduled_signal_dates(
        prices.index, replay_start, end, signal_frequency
    ):
        effective = next_trading_date(prices.index, signal_date)
        if (
            effective is None
            or effective < pd.Timestamp(start)
            or effective > pd.Timestamp(end)
        ):
            continue
        config = config_as_of(effective)
        if config is None:
            continue
        symbols = universe_as_of(signal_date)
        if symbols is None:
            continue
        configs = list(config) if isinstance(config, (list, tuple)) else [config]
        if keltner_upper is None and any(
            item.price_channel == "keltner" for item in configs
        ):
            if adjusted_high is None or adjusted_low is None:
                raise ValueError("Keltner replay requires high and low price panels")
            channel_config = next(
                item for item in configs if item.price_channel == "keltner"
            )
            keltner_upper = calculate_keltner_upper_panel(
                prices, adjusted_high, adjusted_low, channel_config.keltner_window,
                channel_config.keltner_atr_window, channel_config.keltner_multiplier,
            )
        selected = select_can_slim_ensemble_portfolio(
            signal_date, prices, dollar_volume, index_close, eps, configs, symbols,
            quarterly_fundamentals,
            keltner_upper,
            eligibility_close,
        )
        risk_on = market_regime_is_on(
            signal_date, index_close, configs[0].market_ma_days
        )
        target = pd.Series(0.0, index=prices.columns)
        if not selected.empty and risk_on:
            target.loc[selected.index] = selected["target_weight"]
        targets[effective] = (
            target,
            sum(item.transaction_cost_bps for item in configs) / len(configs),
        )
    position_values = pd.Series(0.0, index=prices.columns)
    cash = 1.0
    nav = 1.0
    rows = []
    for current_date, returns in stock_returns.iterrows():
        previous_nav = nav
        position_values = position_values.mul(1 + returns)
        pre_trade_nav = float(cash + position_values.sum())
        scheduled = targets.get(current_date)
        target = scheduled[0] if scheduled is not None else None
        turnover = 0.0
        cost_bps = scheduled[1] if scheduled is not None else 0.0
        if target is not None:
            cost_rate = cost_bps / 10_000
            post_trade_nav = pre_trade_nav
            for _ in range(20):
                desired = target * post_trade_nav
                traded = float((desired - position_values).abs().sum())
                updated = pre_trade_nav - traded * cost_rate
                if abs(updated - post_trade_nav) < 1e-12:
                    post_trade_nav = updated
                    break
                post_trade_nav = updated
            desired = target * post_trade_nav
            turnover = (
                float((desired - position_values).abs().sum() / pre_trade_nav)
                if pre_trade_nav else 0.0
            )
            cost = float((desired - position_values).abs().sum() * cost_rate)
            cash = float(pre_trade_nav - desired.sum() - cost)
            position_values = desired
            nav = float(cash + position_values.sum())
        else:
            nav = pre_trade_nav
        rows.append((
            nav / previous_nav - 1 if previous_nav else 0.0,
            float(index_close.pct_change(fill_method=None).loc[current_date])
            if current_date != prices.index[0] else 0.0,
            float(position_values.sum() / nav) if nav else 0.0,
            turnover,
            int((position_values > 1e-8).sum()),
        ))
    result = pd.DataFrame(rows, index=prices.index, columns=[
        "strategy", "benchmark", "invested", "turnover", "holdings"
    ]).loc[start:end]
    if not return_targets:
        return result
    target_rows = []
    for effective_date, (target, cost_bps) in targets.items():
        positive = target.loc[target.gt(0.0)]
        if positive.empty:
            target_rows.append({
                "effective_date": effective_date,
                "ticker": "__CASH__",
                "target_weight": 0.0,
                "base_transaction_cost_bps": float(cost_bps),
            })
        for ticker, target_weight in positive.items():
            target_rows.append({
                "effective_date": effective_date,
                "ticker": str(ticker),
                "target_weight": float(target_weight),
                "base_transaction_cost_bps": float(cost_bps),
            })
    return result, pd.DataFrame(target_rows)
