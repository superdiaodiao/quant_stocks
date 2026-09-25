"""The prospective (v50r3) valuation replay.

The frozen v50 development replay is ``corrected_stock_policy.
replay_with_sourced_hybrid_stop``; it stays byte-identical because earlier
protocols bind that file.  A prospective observation needs two repairs that
the development period never exercised (its one portfolio stop closed 0.06%
above the threshold, and no monthly target lacked its execution close), so
the replay below is that function with exactly those two changes.  A test
replays the development targets with both and requires identical results.
"""

from __future__ import annotations

import pandas as pd

from src.research import corrected_stock_policy as policy


def replay_live(
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
    """r1's sourced hybrid-stop replay with the two prospective repairs.

    Identical to ``corrected_stock_policy.replay_with_sourced_hybrid_stop``
    except that (1) the portfolio stop is armed only while positions are held,
    and (2) a monthly target without a close on its execution session is left
    in cash until the next target instead of stopping every later valuation.
    The corrected policy's helpers are looked up on its module, so a runtime
    that rebinds them there rebinds them here too.
    """
    if not 0.0 < entry_loss_fraction < 1.0:
        raise ValueError("entry loss fraction must be between zero and one")
    if not 0.0 < portfolio_stop_fraction < 1.0:
        raise ValueError("portfolio stop fraction must be between zero and one")
    if transaction_cost_bps < 0.0:
        raise ValueError("transaction cost must be non-negative")
    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()
    unresolved = policy._unresolved_target_events(
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
    prices, _eligibility = policy.corrected_price_views(raw_close, validation)
    returns = policy.stock_returns_with_delisting_penalty(prices).fillna(0.0)
    benchmark = index_close.reindex(prices.index).ffill().pct_change(
        fill_method=None
    ).fillna(0.0)
    targets = policy.v28._target_dict(prices, target_schedule, transaction_cost_bps)
    position_values = pd.Series(0.0, index=prices.columns)
    entry_prices: dict[str, float] = {}
    pending_stock_exits: set[str] = set()
    pending_portfolio_exit = False
    cash = 1.0
    nav = 1.0
    portfolio_peak = 1.0
    cost_rate = float(transaction_cost_bps) / 10_000.0
    rows = []
    unexecutable_targets: list[dict] = []
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
                # (2) Not executable at this close: the weight stays in cash.
                effective_target.loc[missing_entries] = 0.0
                unexecutable_targets.extend(
                    {"date": f"{current_date:%Y-%m-%d}", "ticker": ticker}
                    for ticker in missing_entries
                )
                positive = effective_target.index[effective_target.gt(1e-12)]
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
            # (1) Once a stop has left the book in cash, the drawdown against
            # the old peak persists; re-arming it would veto the next monthly
            # target, which re-enters.
            if position_values.gt(1e-12).any() and nav <= portfolio_peak * (
                1.0 - portfolio_stop_fraction
            ):
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
    result = pd.DataFrame(rows, index=prices.index).loc[start:end]
    result.attrs["unexecutable_targets"] = unexecutable_targets
    return result
