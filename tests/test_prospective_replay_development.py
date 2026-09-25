"""The prospective replay reproduces the frozen v50 development results.

Needs the local data release (cleaned_stocks_data/price and the Nasdaq index);
listed in tests/data_dependent_test_files.txt.
"""
from pathlib import Path

import pandas as pd
import pytest

from scripts import research_v33_portfolio_stop_development as v33
from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE
from src.research.corrected_stock_policy import load_corporate_action_validation
from src.research.panel_data import load_panel
from src.research.prospective_replay import replay_live

RESULTS = Path("output/research_only/v50/corrected_v47_20260831_r1/development_results")


@pytest.mark.skipif(
    not Path(CLEANED_PRICE_DATA_DIR).is_dir(), reason="needs the local data release"
)
def test_live_replay_reproduces_the_frozen_development_daily_results() -> None:
    raw_close, _dollar_volume = load_panel(CLEANED_PRICE_DATA_DIR, "2018-11-27", "2025-12-31")
    nasdaq = pd.read_csv(
        NASDAQ_INDEX_FILE, index_col="date", parse_dates=True
    )["close"].sort_index().loc[:"2025-12-31"]
    targets = pd.read_csv(RESULTS / "corrected_targets.csv", parse_dates=["effective_date"])
    columns = sorted(set(targets["ticker"]) - {"__CASH__"})
    validation = load_corporate_action_validation()
    for cost in (10, 50):
        daily = replay_live(
            raw_close[columns], nasdaq, targets, "2020-01-01", "2025-12-31",
            validation=validation, entry_loss_fraction=0.2,
            portfolio_stop_fraction=0.25, transaction_cost_bps=float(cost),
        )
        result = v33._canonicalize_result(daily, nasdaq, "2020-01-01", "2025-12-31")
        reference = pd.read_csv(
            RESULTS / f"selected_daily_{cost}bps.csv", index_col="date", parse_dates=True
        )
        assert list(result.index) == list(reference.index)
        for column in ("strategy", "benchmark", "portfolio_value", "turnover"):
            assert (result[column] - reference[column]).abs().max() < 1e-12, column
        for column in ("stock_stop_exits", "portfolio_stop_exits", "holdings"):
            assert (result[column] == reference[column]).all(), column
