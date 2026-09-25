import pandas as pd

from src.financial.quarterly_fundamentals import latest_four_quarter_profit


def _rows(ticker, quarters, filed=None):
    rows = []
    for position, (fiscal_end, value) in enumerate(quarters):
        rows.append({
            "ticker": ticker,
            "fiscal_end": pd.Timestamp(fiscal_end),
            "available_date": pd.Timestamp(filed[position])
            if filed else pd.Timestamp(fiscal_end) + pd.Timedelta(days=35),
            "metric": "net_income",
            "value": float(value),
        })
    return rows


def test_the_latest_four_quarters_decide_without_an_older_fallback() -> None:
    frame = pd.DataFrame(
        # Profitable a year ago, losing money in each of the latest four.
        _rows("LOSS", [("2025-03-31", 900), ("2025-06-30", 400), ("2025-09-30", -700),
                        ("2025-12-31", -800), ("2026-03-31", -600), ("2026-06-30", -700)])
        # A gap in the latest four: absent, never an older complete window.
        + _rows("GAP", [("2024-09-30", 5), ("2024-12-31", 5), ("2025-03-31", 5),
                        ("2025-06-30", 5), ("2026-03-31", 5), ("2026-06-30", 5)])
        # A 52/53-week year: a 16-week fourth quarter.
        + _rows("WEEKS", [("2025-03-22", 10), ("2025-06-14", 10), ("2025-09-06", 10),
                          ("2025-12-27", 10), ("2026-03-21", 10), ("2026-06-13", 10)])
    )

    result = latest_four_quarter_profit(frame, pd.Timestamp("2026-08-31"))

    assert result.loc["LOSS", "net_income_ttm"] == -2800.0
    assert "GAP" not in result.index
    assert result.loc["WEEKS", "net_income_ttm"] == 40.0
    assert result.loc["WEEKS", "fiscal_end"] == pd.Timestamp("2026-06-13")


def test_only_filed_facts_count_and_a_comparative_does_not_refresh_age() -> None:
    quarters = [("2024-03-31", 1), ("2024-06-30", 1), ("2024-09-30", 1), ("2024-12-31", 1)]
    frame = pd.DataFrame(_rows("OLD", quarters, filed=[
        "2024-05-01", "2024-08-01", "2024-11-01", "2025-02-15",
    ]) + [{
        # The latest quarter repeated as a comparative much later.
        "ticker": "OLD", "fiscal_end": pd.Timestamp("2024-12-31"),
        "available_date": pd.Timestamp("2026-02-15"), "metric": "net_income",
        "value": 2.0,
    }] + _rows("LATE", [("2026-03-31", 1), ("2026-06-30", 1), ("2026-09-30", 1),
                        ("2026-12-31", 1)]))

    result = latest_four_quarter_profit(frame, pd.Timestamp("2026-08-31"))

    # First reported 2025-02-15, 562 days before the signal: too old.
    assert "OLD" not in result.index
    # Its later-filed restatement is used while the quarter is fresh enough.
    fresh = latest_four_quarter_profit(frame, pd.Timestamp("2026-03-01"))
    assert fresh.loc["OLD", "net_income_ttm"] == 5.0
    assert "LATE" not in result.index
