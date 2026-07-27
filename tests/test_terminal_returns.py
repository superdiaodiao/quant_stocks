from pathlib import Path

import pandas as pd
import pytest

from src.io.terminal_returns import load_observed_terminal_returns
from src.research.data_quality import stock_returns_with_delisting_penalty


def test_observed_terminal_return_replaces_stress_fallback() -> None:
    dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"])
    close = pd.DataFrame({"MERGER": [10.0, 12.0, None]}, index=dates)

    returns = stock_returns_with_delisting_penalty(
        close,
        terminal_returns={("MERGER", pd.Timestamp("2025-01-03")): 0.25},
    )

    assert returns.loc[pd.Timestamp("2025-01-06"), "MERGER"] == pytest.approx(0.25)


def test_terminal_return_file_requires_provenance(tmp_path: Path) -> None:
    path = tmp_path / "terminal_returns.csv"
    pd.DataFrame(
        [{
            "ticker": "ABC",
            "last_price_date": "2025-01-03",
            "terminal_return": -0.25,
            "source_url": "",
            "verified_at": "2026-07-18T00:00:00Z",
        }]
    ).to_csv(path, index=False)

    with pytest.raises(ValueError, match="source_url"):
        load_observed_terminal_returns(path)


def test_terminal_return_can_be_positive_for_cash_merger(tmp_path: Path) -> None:
    path = tmp_path / "terminal_returns.csv"
    pd.DataFrame(
        [{
            "ticker": "ABC",
            "last_price_date": "2025-01-03",
            "terminal_return": 0.12,
            "source_url": "https://example.com/filing",
            "verified_at": "2026-07-18T00:00:00Z",
        }]
    ).to_csv(path, index=False)

    loaded = load_observed_terminal_returns(path)

    assert loaded.loc[0, "terminal_return"] == pytest.approx(0.12)
