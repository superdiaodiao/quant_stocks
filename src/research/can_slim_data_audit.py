"""Audit the exact securities selected by the expanding walk-forward replay."""

from __future__ import annotations

import json
import argparse
from pathlib import Path

import pandas as pd

from src.conf import (
    CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE, POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
)
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.io.terminal_returns import observed_terminal_return_map
from src.research.can_slim import (
    CanSlimConfig,
    calculate_can_slim_returns,
    calculate_keltner_upper_panel,
    select_can_slim_ensemble_portfolio,
)
from src.research.can_slim_validation import fixed_top3_config
from src.research.can_slim_walk_forward import candidate_configs
from src.research.data_quality import back_adjust_common_splits
from src.research.panel_data import load_ohlc_panel, load_panel
from src.research.universe_history import load_universe_snapshots, universe_as_of
from src.strategy.common import market_regime_is_on, next_trading_date, scheduled_signal_dates


def _config_ids_as_of(value: str | list[int] | dict, signal_date) -> list[int]:
    """Resolve legacy config lists or dated walk-forward snapshots."""
    parsed = json.loads(value) if isinstance(value, str) else value
    if isinstance(parsed, list):
        return [int(config_id) for config_id in parsed]
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError("config_ids must be a non-empty list or dated mapping")
    signal_date = pd.Timestamp(signal_date).normalize()
    eligible = [
        (pd.Timestamp(effective).normalize(), config_ids)
        for effective, config_ids in parsed.items()
        if pd.Timestamp(effective).normalize() <= signal_date
    ]
    if not eligible:
        raise ValueError(f"no configuration effective by {signal_date.date()}")
    _, config_ids = max(eligible, key=lambda item: item[0])
    return [int(config_id) for config_id in config_ids]


def _holding_calendar(
    benchmark_index: pd.DatetimeIndex,
    effective,
    next_effective,
) -> pd.DatetimeIndex:
    """Use the U.S. benchmark calendar, excluding the next rebalance day."""
    effective = pd.Timestamp(effective).normalize()
    next_effective = pd.Timestamp(next_effective).normalize()
    return benchmark_index[
        (benchmark_index >= effective) & (benchmark_index < next_effective)
    ]


def audit_selected_histories(
    walk_forward_path: str | Path = "output/can_slim_walk_forward.csv",
    signal_frequency: str = "monthly",
    use_quarterly_fundamentals: bool = False,
    adaptive_channel: bool = False,
    fixed_config: CanSlimConfig | None = None,
    maximum_financial_age_days: tuple[int, ...] = (550,),
    quarterly_path: str | Path = POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
) -> tuple[pd.DataFrame, dict]:
    selected_years = (
        pd.DataFrame(index=range(2021, 2027))
        if fixed_config is not None
        else pd.read_csv(walk_forward_path).set_index("test_year")
    )
    if fixed_config is not None:
        signal_frequency = fixed_config.signal_frequency
        use_quarterly_fundamentals = fixed_config.use_quarterly_fundamentals
        adaptive_channel = fixed_config.price_channel == "keltner"
    if adaptive_channel:
        close, dollar_volume, high, low = load_ohlc_panel(
            CLEANED_PRICE_DATA_DIR, "2017-11-28", "2026-07-17"
        )
    else:
        close, dollar_volume = load_panel(
            CLEANED_PRICE_DATA_DIR, "2017-11-28", "2026-07-17"
        )
        high = low = None
    adjusted = back_adjust_common_splits(close)
    adjustment = adjusted / close.reindex_like(adjusted)
    keltner_upper = (
        calculate_keltner_upper_panel(
            adjusted, high.reindex_like(adjusted) * adjustment,
            low.reindex_like(adjusted) * adjustment,
        ) if adaptive_channel else None
    )
    nasdaq = pd.read_csv(NASDAQ_INDEX_FILE, index_col="date", parse_dates=True)["close"]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    quarterly = (
        load_quarterly_fundamentals(quarterly_path)
        if use_quarterly_fundamentals else None
    )
    snapshots = load_universe_snapshots()
    snapshot_dates = sorted(snapshots)
    terminal = observed_terminal_return_map()
    configs = (
        [fixed_config]
        if fixed_config is not None
        else candidate_configs(
            signal_frequency,
            use_quarterly_fundamentals,
            adaptive_channel,
            maximum_financial_age_days=maximum_financial_age_days,
        )
    )
    rows = []

    for year, chosen in selected_years.iterrows():
        end = pd.Timestamp("2026-07-17") if year == 2026 else pd.Timestamp(year, 12, 31)
        signals = scheduled_signal_dates(
            close.index, f"{year}-01-01", str(end.date()), signal_frequency
        )
        for signal_index, signal_date in enumerate(signals):
            if fixed_config is not None:
                selected_configs = [fixed_config]
            else:
                config_ids = _config_ids_as_of(chosen.config_ids, signal_date)
                selected_configs = [configs[config_id] for config_id in config_ids]
            snapshot_date = max(date for date in snapshot_dates if date <= signal_date)
            selected = select_can_slim_ensemble_portfolio(
                signal_date, adjusted, dollar_volume, nasdaq, eps, selected_configs,
                universe_as_of(snapshots, signal_date),
                quarterly, keltner_upper,
            )
            if not market_regime_is_on(
                signal_date, nasdaq, selected_configs[0].market_ma_days
            ):
                selected = selected.iloc[0:0]
            effective = next_trading_date(close.index, signal_date)
            if effective is None:
                continue
            next_signal = signals[signal_index + 1] if signal_index + 1 < len(signals) else end
            next_effective = next_trading_date(close.index, next_signal) or end
            holding_dates = _holding_calendar(
                nasdaq.index, effective, next_effective
            )
            for ticker, score in selected.iterrows():
                prices = adjusted.loc[holding_dates, ticker]
                missing = int(prices.isna().sum())
                observed = prices.dropna()
                last_date = adjusted[ticker].last_valid_index()
                unresolved_end = bool(
                    last_date is not None and last_date < next_effective
                    and (ticker, pd.Timestamp(last_date).normalize()) not in terminal
                )
                daily = observed.pct_change(fill_method=None).abs()
                signed_daily = observed.pct_change(fill_method=None).dropna()
                maximum_event_date = (
                    daily.idxmax() if daily.notna().any() else None
                )
                maximum_event_return = (
                    float(signed_daily.loc[maximum_event_date])
                    if maximum_event_date in signed_daily.index else 0.0
                )
                entry_price = float(observed.iloc[0]) if len(observed) else float("nan")
                path_return = observed / entry_price - 1 if len(observed) else observed
                weight = float(score.target_weight)
                financial_age = score.get(
                    "quarterly_financial_age_days", score.get("financial_age_days", 0)
                )
                rows.append({
                    "test_year": int(year), "signal_date": signal_date.date(),
                    "effective_date": effective.date(), "ticker": ticker,
                    "snapshot_age_days": int((signal_date - snapshot_date).days),
                    "financial_age_days": (
                        int(financial_age) if pd.notna(financial_age) else None
                    ),
                    "selection_mode": score.get("selection_mode", "ensemble"),
                    "financial_source": score.get("financial_source", "unknown"),
                    "target_weight": weight,
                    "holding_sessions": len(holding_dates), "missing_holding_prices": missing,
                    "holding_return": float((1 + signed_daily).prod() - 1) if len(signed_daily) else 0.0,
                    "return_contribution": weight * float(signed_daily.sum()),
                    "minimum_return_from_entry": float(path_return.min()) if len(path_return) else 0.0,
                    "maximum_absolute_daily_return": float(daily.max()) if len(daily) else 0.0,
                    "maximum_event_date": (
                        maximum_event_date.date()
                        if maximum_event_date is not None else None
                    ),
                    "maximum_event_return": maximum_event_return,
                    "unresolved_terminal_while_held": unresolved_end,
                })

    ledger = pd.DataFrame(rows)
    summary = {
        "selected_positions": len(ledger),
        "selected_symbols": int(ledger["ticker"].nunique()) if len(ledger) else 0,
        "maximum_signal_snapshot_age_days": int(ledger["snapshot_age_days"].max()) if len(ledger) else 0,
        "positions_with_missing_holding_prices": int(ledger["missing_holding_prices"].gt(0).sum()) if len(ledger) else 0,
        "maximum_missing_holding_sessions": int(ledger["missing_holding_prices"].max()) if len(ledger) else 0,
        "positions_with_unresolved_terminal_return": int(ledger["unresolved_terminal_while_held"].sum()) if len(ledger) else 0,
        "positions_with_over_50pct_daily_move": int(ledger["maximum_absolute_daily_return"].gt(0.5).sum()) if len(ledger) else 0,
    }
    if fixed_config is not None and len(ledger):
        largest = ledger.loc[ledger["maximum_absolute_daily_return"].idxmax()]
        event_date = pd.Timestamp(largest["maximum_event_date"])
        replay = calculate_can_slim_returns(
            close, dollar_volume, nasdaq, eps, fixed_config,
            lambda date: universe_as_of(snapshots, date),
            quarterly, high, low,
        )
        event_year = event_date.year
        without_event = replay.loc[
            replay.index.year == event_year, "strategy"
        ].copy()
        without_event.loc[event_date] -= (
            float(largest["target_weight"])
            * float(largest["maximum_event_return"])
        )
        benchmark = replay.loc[
            replay.index.year == event_year, "benchmark"
        ]
        zeroed_strategy = float((1 + without_event).prod() - 1)
        benchmark_return = float((1 + benchmark).prod() - 1)
        summary["largest_observed_holding_event"] = {
            "ticker": largest["ticker"],
            "date": event_date.strftime("%Y-%m-%d"),
            "daily_return": float(largest["maximum_event_return"]),
            "portfolio_weight": float(largest["target_weight"]),
        }
        summary["largest_event_zeroed_sensitivity"] = {
            "year": event_year,
            "strategy": zeroed_strategy,
            "nasdaq": benchmark_return,
            "excess_vs_nasdaq": zeroed_strategy - benchmark_return,
        }
    return ledger, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--signal-frequency", choices=("monthly", "weekly", "daily"), default="monthly"
    )
    parser.add_argument("--walk-forward-path", default="output/can_slim_walk_forward.csv")
    parser.add_argument("--use-quarterly-fundamentals", action="store_true")
    parser.add_argument("--adaptive-channel", action="store_true")
    parser.add_argument("--fixed-top3", action="store_true")
    parser.add_argument("--maximum-financial-age-days", default="550")
    parser.add_argument(
        "--quarterly-input",
        type=Path,
        default=Path(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE),
    )
    parser.add_argument("--artifact-tag")
    args = parser.parse_args()
    financial_age_days = tuple(
        int(value.strip())
        for value in args.maximum_financial_age_days.split(",")
        if value.strip()
    )
    suffix_parts = [] if args.signal_frequency == "monthly" else [args.signal_frequency]
    if args.use_quarterly_fundamentals:
        suffix_parts.append("quarterly_financials")
    if args.adaptive_channel:
        suffix_parts.append("adaptive_channel")
    if financial_age_days != (550,):
        suffix_parts.append(
            "financial_age_" + "_".join(str(value) for value in financial_age_days)
        )
    if args.artifact_tag:
        suffix_parts.append(args.artifact_tag)
    if args.fixed_top3:
        suffix_parts = ["fixed_top3"]
    suffix = f"_{'_'.join(suffix_parts)}" if suffix_parts else ""
    ledger, summary = audit_selected_histories(
        args.walk_forward_path, args.signal_frequency,
        args.use_quarterly_fundamentals, args.adaptive_channel,
        fixed_top3_config() if args.fixed_top3 else None,
        financial_age_days,
        args.quarterly_input,
    )
    ledger.to_csv(f"output/can_slim_selected_data_audit{suffix}.csv", index=False)
    Path(f"output/can_slim_selected_data_audit{suffix}.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
