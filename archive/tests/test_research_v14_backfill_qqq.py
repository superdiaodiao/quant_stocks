import pandas as pd

from datetime import date

from scripts.research_v14_backfill_qqq import (
    build_history,
    fetch_price_chunks,
    validate_overlap,
)


def test_build_history_binds_cash_dividends_to_ex_dates() -> None:
    prices = pd.DataFrame({
        "date": ["2015-01-02", "2015-01-05"],
        "open": [1, 2], "high": [2, 3], "low": [0.5, 1.5],
        "close": [1.5, 2.5], "volume": [100, 200],
    })
    dividends = pd.DataFrame({
        "date": ["2015-01-05"], "cash_dividend": [0.25]
    })
    result = build_history(prices, dividends)
    assert result["cash_dividend"].tolist() == [0.0, 0.25]


def test_price_fetch_is_split_into_bounded_history_requests() -> None:
    calls = []

    def fake_fetcher(_ticker, start, end, **_kwargs):
        calls.append((start, end))
        return pd.DataFrame({"date": [pd.Timestamp(start)]})

    result = fetch_price_chunks(
        date(2013, 1, 1), date(2020, 1, 1),
        chunk_years=5, fetcher=fake_fetcher,
    )
    assert calls == [
        (date(2013, 1, 1), date(2017, 12, 31)),
        (date(2018, 1, 1), date(2020, 1, 1)),
    ]
    assert len(result) == 2


def test_overlap_validation_rejects_secondary_price_drift() -> None:
    dates = pd.date_range("2018-01-01", periods=500, freq="D")
    nasdaq = pd.DataFrame({"date": dates, "close": 100.0})
    yahoo = pd.DataFrame({"date": dates, "close": 100.0})
    assert validate_overlap(yahoo, nasdaq)["within_tolerance_fraction"] == 1.0
    yahoo.loc[:10, "close"] = 90.0
    try:
        validate_overlap(yahoo, nasdaq)
    except ValueError as exc:
        assert "overlap mismatch" in str(exc)
    else:
        raise AssertionError("material secondary-source drift must fail")
