"""Source-locked price views and risk replay for corrected stock research.

The legacy research path inferred splits from integer-like one-day price
jumps.  That is useful for finding events to review, but it is not evidence
that an adjustment is valid.  This module applies only sourced actions,
preserves sourced real market moves, and fails closed when an unresolved jump
could affect a ranked liquid pool or a held security.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from scripts import research_v24_stock_momentum_development as v24
from scripts import research_v28_stock_trailing_stop_development as v28
from src.research.data_quality import (
    apply_confirmed_price_adjustments,
    detect_common_split_events,
    restore_contemporaneous_prices,
    stock_returns_with_delisting_penalty,
)


VALIDATION_PATH = Path("output/can_slim_all_corporate_action_validation.csv")
RESOLVED_STATUSES = frozenset({"CONFIRMED", "CONFIRMED_MARKET_MOVE"})
UNRESOLVED_STATUSES = frozenset(
    {"UNRESOLVED_PRICE_JUMP", "SOURCE_FETCH_FAILED"}
)


def load_corporate_action_validation(
    path: str | Path = VALIDATION_PATH,
) -> pd.DataFrame:
    """Load the frozen event adjudication used by the corrected policy."""
    frame = pd.read_csv(path)
    required = {
        "ticker",
        "split_date",
        "validation_status",
        "confirmed_adjustment_factor",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(
            "Corporate-action validation missing columns: "
            + ", ".join(sorted(missing))
        )
    frame = frame.copy()
    frame["ticker"] = frame["ticker"].astype(str).str.upper().str.strip()
    frame["split_date"] = pd.to_datetime(
        frame["split_date"], errors="raise"
    ).dt.normalize()
    unknown = sorted(set(frame["validation_status"]) - (
        RESOLVED_STATUSES | UNRESOLVED_STATUSES
    ))
    if unknown:
        raise ValueError(
            "Unsupported corporate-action validation statuses: "
            + ", ".join(unknown)
        )
    return frame


def confirmed_actions_for_panel(
    validation: pd.DataFrame,
    close: pd.DataFrame,
) -> pd.DataFrame:
    """Return sourced adjustment factors applicable to one price panel."""
    actions = validation.loc[
        validation["validation_status"].eq("CONFIRMED")
    ].rename(
        columns={
            "split_date": "effective_date",
            "confirmed_adjustment_factor": "adjustment_factor",
        }
    )
    actions = actions[
        ["ticker", "effective_date", "adjustment_factor"]
    ].copy()
    actions["adjustment_factor"] = pd.to_numeric(
        actions["adjustment_factor"], errors="coerce"
    )
    actions = actions.loc[
        actions["ticker"].isin({str(column).upper() for column in close.columns})
        & actions["effective_date"].le(close.index.max())
    ]
    if actions["adjustment_factor"].isna().any():
        raise ValueError("Confirmed corporate actions require adjustment factors")
    return actions


def corrected_price_views(
    raw_close: pd.DataFrame,
    validation: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build continuous-return and contemporaneous-eligibility price views."""
    raw_close = raw_close.sort_index()
    actions = confirmed_actions_for_panel(validation, raw_close)
    continuous = apply_confirmed_price_adjustments(raw_close, actions)
    eligibility = restore_contemporaneous_prices(raw_close, validation)
    return continuous.sort_index(), eligibility.sort_index()


def unresolved_events_in_window(
    validation: pd.DataFrame,
    symbols: set[str] | list[str] | pd.Index,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> pd.DataFrame:
    """Return unsourced integer-like jumps for symbols in a decision window."""
    names = {str(symbol).upper() for symbol in symbols}
    return validation.loc[
        validation["validation_status"].isin(UNRESOLVED_STATUSES)
        & validation["ticker"].isin(names)
        & validation["split_date"].between(
            pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
        )
    ].copy()


def technical_ranking(
    signal_date: pd.Timestamp,
    spec: dict,
    inputs: dict,
) -> pd.DataFrame:
    """Rank on sourced continuous returns and contemporaneous nominal price."""
    lookback = int(spec["lookback_sessions"])
    skip = int(spec["skip_recent_sessions"])
    cache_key = (pd.Timestamp(signal_date), lookback, skip, "corrected")
    cached = inputs["technical_cache"].get(cache_key)
    if cached is not None:
        return cached.copy()
    close = inputs["close"]
    eligibility_close = inputs["eligibility_close"].reindex_like(close)
    position = int(close.index.get_loc(signal_date))
    required = max(lookback, v24.STOCK_MA_DAYS - 1, 49)
    if position < required or position - skip < 0:
        return pd.DataFrame()
    current = close.iloc[position]
    eligibility_price = eligibility_close.iloc[position]
    momentum_end = close.iloc[position - skip]
    momentum_start = close.iloc[position - lookback]
    momentum = momentum_end.div(momentum_start).sub(1.0)
    stock_ma = close.iloc[
        position - v24.STOCK_MA_DAYS + 1 : position + 1
    ].mean()
    liquidity = inputs["dollar_volume"].iloc[position - 49 : position + 1].median()
    index_history = inputs["nasdaq"].reindex(close.index).ffill()
    index_momentum = (
        float(index_history.iloc[position - skip])
        / float(index_history.iloc[position - lookback])
        - 1.0
    )
    frame = pd.DataFrame(
        {
            "price": current,
            "eligibility_price": eligibility_price,
            "median_dollar_volume_50d": liquidity,
            "momentum": momentum,
            "stock_ma": stock_ma,
        }
    ).replace([np.inf, -np.inf], np.nan)
    symbols = inputs["universe"](signal_date)
    if symbols is None:
        return pd.DataFrame()
    frame = frame.loc[frame.index.intersection(sorted(symbols))]
    frame = frame.loc[
        frame["eligibility_price"].ge(v24.MINIMUM_PRICE)
        & frame["median_dollar_volume_50d"].ge(
            v24.MINIMUM_MEDIAN_DOLLAR_VOLUME
        )
        & frame["price"].gt(frame["stock_ma"])
        & frame["momentum"].gt(index_momentum)
    ].dropna()
    frame["momentum_excess_vs_nasdaq"] = frame["momentum"] - index_momentum
    frame = frame.sort_values(
        ["momentum_excess_vs_nasdaq", "median_dollar_volume_50d"],
        ascending=[False, False],
    )
    inputs["technical_cache"][cache_key] = frame.copy()
    return frame


def large_liquid_ranking(
    signal_date: pd.Timestamp,
    spec: dict,
    inputs: dict,
) -> pd.DataFrame:
    """Apply the frozen profitability/liquidity policy and fail closed."""
    key = (pd.Timestamp(signal_date), str(spec["key"]), "corrected")
    cached = inputs["large_liquid_cache"].get(key)
    if cached is not None:
        return cached.copy()
    ranking = technical_ranking(signal_date, spec, inputs)
    if not ranking.empty:
        ranking = ranking.loc[
            ranking.index.isin(v24._profitable_symbols(signal_date, inputs))
        ]
        ranking = ranking.nlargest(
            int(spec["liquid_pool_size"]),
            "median_dollar_volume_50d",
            keep="first",
        ).sort_values(
            ["momentum_excess_vs_nasdaq", "median_dollar_volume_50d"],
            ascending=[False, False],
        )
        close = inputs["close"]
        position = int(close.index.get_loc(signal_date))
        relevant_sessions = max(
            int(spec["lookback_sessions"]), v24.STOCK_MA_DAYS - 1
        )
        window_start = close.index[max(0, position - relevant_sessions)]
        unresolved = unresolved_events_in_window(
            inputs["corporate_action_validation"],
            ranking.index,
            window_start,
            signal_date,
        )
        if len(unresolved):
            details = ", ".join(
                f"{row.ticker}@{row.split_date:%Y-%m-%d}"
                for row in unresolved.itertuples(index=False)
            )
            raise RuntimeError(
                "Unresolved corporate action affects the ranked liquid pool: "
                + details
            )
    inputs["large_liquid_cache"][key] = ranking.copy()
    return ranking


def _unresolved_target_events(
    raw_close: pd.DataFrame,
    target_schedule: pd.DataFrame,
    validation: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    target_symbols = set(
        target_schedule.loc[
            target_schedule["ticker"].ne("__CASH__"), "ticker"
        ].astype(str)
    )
    if not target_symbols:
        return pd.DataFrame()
    detected = detect_common_split_events(raw_close.loc[:end])
    detected["ticker"] = detected["ticker"].astype(str).str.upper()
    detected["split_date"] = pd.to_datetime(
        detected["split_date"]
    ).dt.normalize()
    resolved = validation.loc[
        validation["validation_status"].isin(RESOLVED_STATUSES)
    ]
    known = set(
        zip(resolved["ticker"], resolved["split_date"], strict=False)
    )
    return detected.loc[
        detected["ticker"].isin(target_symbols)
        & detected["split_date"].between(start, end)
        & ~detected.apply(
            lambda row: (row["ticker"], row["split_date"]) in known,
            axis=1,
        )
    ].copy()


def replay_with_sourced_hybrid_stop(
    raw_close: pd.DataFrame,
    index_close: pd.Series,
    target_schedule: pd.DataFrame,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    validation: pd.DataFrame,
    entry_loss_fraction: float,
    portfolio_stop_fraction: float,
    transaction_cost_bps: float,
) -> pd.DataFrame:
    """Replay the fixed risk rule without inferred splits or stop blind spots."""
    if not 0.0 < entry_loss_fraction < 1.0:
        raise ValueError("entry loss fraction must be between zero and one")
    if not 0.0 < portfolio_stop_fraction < 1.0:
        raise ValueError("portfolio stop fraction must be between zero and one")
    if transaction_cost_bps < 0.0:
        raise ValueError("transaction cost must be non-negative")
    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()
    unresolved = _unresolved_target_events(
        raw_close, target_schedule, validation, start, end
    )
    if len(unresolved):
        details = ", ".join(
            f"{row.ticker}@{row.split_date:%Y-%m-%d}"
            for row in unresolved.itertuples(index=False)
        )
        raise RuntimeError(
            "Unresolved corporate action affects a strategy target: " + details
        )
    prices, _eligibility = corrected_price_views(raw_close, validation)
    returns = stock_returns_with_delisting_penalty(prices).fillna(0.0)
    benchmark = index_close.reindex(prices.index).ffill().pct_change(
        fill_method=None
    ).fillna(0.0)
    targets = v28._target_dict(prices, target_schedule, transaction_cost_bps)
    position_values = pd.Series(0.0, index=prices.columns)
    entry_prices: dict[str, float] = {}
    pending_stock_exits: set[str] = set()
    pending_portfolio_exit = False
    cash = 1.0
    nav = 1.0
    portfolio_peak = 1.0
    cost_rate = float(transaction_cost_bps) / 10_000.0
    rows = []
    dates = prices.index
    for position, (current_date, daily_returns) in enumerate(returns.iterrows()):
        previous_nav = nav
        position_values = position_values.mul(1.0 + daily_returns)
        pre_trade_nav = float(cash + position_values.sum())
        turnover = 0.0
        transaction_cost = 0.0
        stock_stop_exits = 0
        portfolio_stop_exits = 0
        coincident_stop_veto = False
        target = targets.get(pd.Timestamp(current_date))
        if target is not None:
            effective_target = target.copy()
            active_before = position_values.index[position_values.gt(1e-12)]
            if pending_portfolio_exit:
                effective_target.loc[:] = 0.0
                portfolio_stop_exits = int(bool(len(active_before)))
                coincident_stop_veto = True
            elif pending_stock_exits:
                stopped = sorted(
                    set(effective_target.index) & pending_stock_exits
                )
                effective_target.loc[stopped] = 0.0
                stock_stop_exits = sum(
                    float(position_values.get(ticker, 0.0)) > 1e-12
                    for ticker in stopped
                )
                coincident_stop_veto = bool(stopped)
            positive = effective_target.index[effective_target.gt(1e-12)]
            missing_entries = [
                str(ticker)
                for ticker in positive
                if pd.isna(prices.at[current_date, ticker])
            ]
            if missing_entries:
                raise RuntimeError(
                    "Monthly target has no executable close: "
                    + ", ".join(missing_entries)
                )
            post_trade_nav = pre_trade_nav
            for _ in range(20):
                desired = effective_target * post_trade_nav
                traded = float((desired - position_values).abs().sum())
                updated = pre_trade_nav - traded * cost_rate
                if abs(updated - post_trade_nav) < 1e-12:
                    post_trade_nav = updated
                    break
                post_trade_nav = updated
            desired = effective_target * post_trade_nav
            traded = float((desired - position_values).abs().sum())
            transaction_cost = traded * cost_rate
            turnover = traded / pre_trade_nav if pre_trade_nav else 0.0
            cash = float(pre_trade_nav - desired.sum() - transaction_cost)
            position_values = desired
            entry_prices = {
                str(ticker): float(prices.at[current_date, ticker])
                for ticker in positive
            }
            pending_stock_exits.clear()
            pending_portfolio_exit = False
            nav = float(cash + position_values.sum())
            portfolio_peak = nav
        elif pending_portfolio_exit:
            active = position_values.index[position_values.gt(1e-12)]
            tradable = [
                ticker
                for ticker in active
                if pd.notna(prices.at[current_date, ticker])
            ]
            sold = float(position_values.loc[tradable].sum()) if tradable else 0.0
            transaction_cost = sold * cost_rate
            turnover = sold / pre_trade_nav if pre_trade_nav else 0.0
            cash += sold - transaction_cost
            position_values.loc[tradable] = 0.0
            portfolio_stop_exits = int(bool(tradable))
            for ticker in tradable:
                entry_prices.pop(str(ticker), None)
            pending_stock_exits.clear()
            pending_portfolio_exit = bool(
                position_values.gt(1e-12).any()
            )
            nav = float(cash + position_values.sum())
        elif pending_stock_exits:
            active = [
                ticker
                for ticker in sorted(pending_stock_exits)
                if float(position_values.get(ticker, 0.0)) > 1e-12
                and pd.notna(prices.at[current_date, ticker])
            ]
            sold = float(position_values.loc[active].sum()) if active else 0.0
            transaction_cost = sold * cost_rate
            turnover = sold / pre_trade_nav if pre_trade_nav else 0.0
            cash += sold - transaction_cost
            position_values.loc[active] = 0.0
            stock_stop_exits = len(active)
            for ticker in active:
                entry_prices.pop(str(ticker), None)
                pending_stock_exits.discard(str(ticker))
            nav = float(cash + position_values.sum())
        else:
            nav = pre_trade_nav

        next_date = dates[position + 1] if position + 1 < len(dates) else None
        if next_date is not None:
            portfolio_peak = max(portfolio_peak, nav)
            if nav <= portfolio_peak * (1.0 - portfolio_stop_fraction):
                pending_portfolio_exit = True
                pending_stock_exits.clear()
            else:
                for ticker in position_values.index[position_values.gt(1e-12)]:
                    price = prices.at[current_date, ticker]
                    reference = entry_prices.get(str(ticker))
                    if (
                        pd.notna(price)
                        and reference is not None
                        and float(price)
                        <= reference * (1.0 - entry_loss_fraction)
                    ):
                        pending_stock_exits.add(str(ticker))
        rows.append(
            {
                "strategy": nav / previous_nav - 1.0 if previous_nav else 0.0,
                "benchmark": float(benchmark.loc[current_date]),
                "invested": float(position_values.sum() / nav) if nav else 0.0,
                "turnover": turnover,
                "transaction_cost": transaction_cost,
                "holdings": int(position_values.gt(1e-12).sum()),
                "stock_stop_exits": stock_stop_exits,
                "portfolio_stop_exits": portfolio_stop_exits,
                "stop_exits": stock_stop_exits + portfolio_stop_exits,
                "coincident_stop_veto": coincident_stop_veto,
                "portfolio_value": nav,
                "cash": cash,
            }
        )
    return pd.DataFrame(rows, index=prices.index).loc[start:end]
