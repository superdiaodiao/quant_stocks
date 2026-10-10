"""Compare frozen Top-3 selections with and without newly recovered snapshots."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pandas as pd

from src.conf import (
    CLEANED_PRICE_DATA_DIR,
    NASDAQ_INDEX_FILE,
    POINT_IN_TIME_EPS_FILE,
    POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE,
    PROJECT_PATH,
)
from src.financial.eps import load_eps_history
from src.financial.quarterly_fundamentals import load_quarterly_fundamentals
from src.research.can_slim import (
    market_regime_is_on,
    scheduled_signal_dates,
    select_can_slim_portfolio,
)
from src.research.can_slim_validation import fixed_top3_config
from src.research.panel_data import load_panel
from src.research.data_quality import back_adjust_common_splits
from src.research.universe_history import load_universe_snapshots


DEFAULT_OUTPUT = Path(PROJECT_PATH) / (
    "output/data_provenance/universe_snapshot_gap_selection_impact_2026-08-08.json"
)


def _latest(snapshots: dict[pd.Timestamp, set[str]], date: pd.Timestamp):
    dates = [item for item in snapshots if item <= date]
    return max(dates) if dates else None


def compare_recovered_snapshots(
    recovered_dates: list[str], output: str | Path = DEFAULT_OUTPUT
) -> dict:
    config = fixed_top3_config()
    load_start = (pd.Timestamp(config.start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    close, dollar_volume = load_panel(CLEANED_PRICE_DATA_DIR, load_start, config.end)
    prices = back_adjust_common_splits(close).sort_index()
    nasdaq = pd.read_csv(NASDAQ_INDEX_FILE, index_col="date", parse_dates=True)["close"]
    eps = load_eps_history(POINT_IN_TIME_EPS_FILE)
    quarterly = load_quarterly_fundamentals(POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE)
    current = load_universe_snapshots()
    recovered = {pd.Timestamp(item) for item in recovered_dates}
    prior = {date: symbols for date, symbols in current.items() if date not in recovered}
    records = []
    for signal_date in scheduled_signal_dates(
        prices.index, config.start, config.end, config.signal_frequency
    ):
        current_date = _latest(current, signal_date)
        prior_date = _latest(prior, signal_date)
        if current_date not in recovered or current_date == prior_date:
            continue
        current_universe = current[current_date]
        prior_universe = prior[prior_date]
        current_selected = select_can_slim_portfolio(
            signal_date, prices, dollar_volume, nasdaq, eps, config,
            current_universe, quarterly, eligibility_close=close,
        )
        prior_selected = select_can_slim_portfolio(
            signal_date, prices, dollar_volume, nasdaq, eps, config,
            prior_universe, quarterly, eligibility_close=close,
        )
        current_tickers = current_selected.index.astype(str).tolist()
        prior_tickers = prior_selected.index.astype(str).tolist()
        records.append({
            "signal_date": signal_date.strftime("%Y-%m-%d"),
            "prior_snapshot_date": prior_date.strftime("%Y-%m-%d"),
            "recovered_snapshot_date": current_date.strftime("%Y-%m-%d"),
            "prior_universe_size": len(prior_universe),
            "recovered_universe_size": len(current_universe),
            "new_members": sorted(current_universe - prior_universe),
            "removed_members": sorted(prior_universe - current_universe),
            "prior_selected": prior_tickers,
            "recovered_selected": current_tickers,
            "selection_changed": prior_tickers != current_tickers,
            "market_regime_on": market_regime_is_on(
                signal_date, nasdaq.reindex(prices.index).ffill(), config.market_ma_days
            ),
        })
    report = {
        "schema_version": 1,
        "status": "COMPLETE",
        "research_only": True,
        "frozen_parameters_modified": False,
        "formal_validation_rerun": False,
        "recovered_dates": sorted(item.strftime("%Y-%m-%d") for item in recovered),
        "affected_signal_count": len(records),
        "changed_selection_count": sum(row["selection_changed"] for row in records),
        "records": records,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
    os.replace(temporary, output)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recovered-dates", required=True)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    report = compare_recovered_snapshots(
        [item.strip() for item in args.recovered_dates.split(",") if item.strip()],
        args.output,
    )
    print(json.dumps({
        "affected_signal_count": report["affected_signal_count"],
        "changed_selection_count": report["changed_selection_count"],
        "records": report["records"],
    }, indent=2))


if __name__ == "__main__":
    main()
