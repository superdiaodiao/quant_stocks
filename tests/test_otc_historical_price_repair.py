import json

import pandas as pd

from scripts.otc_historical_price_repair import (
    _eligible,
    _count_missing,
    _merge_missing,
    _local_overlap_validation,
    _overlap,
    _parse_edgar,
)


def _prices(values: list[float], ticker: str = "TEST") -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(values), freq="D")
    return pd.DataFrame(
        {
            "date": dates,
            "ticker": ticker,
            "open": values,
            "high": values,
            "low": values,
            "close": values,
            "volume": [100] * len(values),
        }
    )


def test_overlap_ignores_sub_cent_precision_noise() -> None:
    edgar = _prices([10.0] * 20 + [0.000001] * 2)
    yahoo = _prices([10.0] * 20 + [0.0001] * 2)
    report = _overlap(edgar, yahoo)
    assert report["sessions"] == 22
    assert report["meaningful_sessions"] == 20
    assert report["micro_price_sessions"] == 2
    assert report["price_within_1pct"] == 1.0
    assert _eligible(report)


def test_local_overlap_requires_same_ticker_ohlcv_and_minimum_sessions() -> None:
    local = _prices([10.0] * 25)
    matching = local.copy()
    report = _local_overlap_validation(matching, local)
    assert report["passed"] is True
    assert report["sessions"] == 25
    mismatched = matching.copy()
    mismatched["close"] *= 2
    assert _local_overlap_validation(mismatched, local)["passed"] is False
    assert _local_overlap_validation(matching.iloc[:19], local)["passed"] is False


def test_merge_missing_never_replaces_existing_dates(tmp_path) -> None:
    path = tmp_path / "test.csv"
    old = _prices([1.0, 2.0])
    old.to_csv(path, index=False)
    incoming = _prices([99.0, 3.0, 4.0])
    added = _merge_missing(path, incoming, "TEST")
    result = pd.read_csv(path, parse_dates=["date"])
    assert added == 1
    assert result.loc[result["date"] == pd.Timestamp("2024-01-01"), "close"].iloc[0] == 1.0
    assert result["close"].tolist() == [1.0, 2.0, 4.0]


def test_count_missing_does_not_modify_price_file(tmp_path) -> None:
    path = tmp_path / "test.csv"
    old = _prices([1.0, 2.0])
    old.to_csv(path, index=False)
    before = path.read_bytes()
    incoming = _prices([99.0, 3.0, 4.0])
    assert _count_missing(path, incoming) == 1
    assert path.read_bytes() == before


def test_parse_edgar_requires_positive_close() -> None:
    payload = json.dumps(
        {
            "companyName": "TEST INC",
            "marketData": [
                {"Date": "2024-01-02 00:00:00", "Open": 1, "High": 2, "Low": 1, "Close": 1.5, "Volume": 3},
                {"Date": "2024-01-03 00:00:00", "Open": 1, "High": 1, "Low": 1, "Close": 0, "Volume": 4},
            ],
        }
    ).encode()
    frame, name = _parse_edgar(payload, "TEST")
    assert name == "TEST INC"
    assert frame["date"].tolist() == [pd.Timestamp("2024-01-02")]
    assert frame["close"].tolist() == [1.5]
