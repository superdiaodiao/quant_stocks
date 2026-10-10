import json

import pandas as pd

from scripts.yahoo_historical_price_repair import (
    _merge_missing,
    _overlap_validation,
    _parse_yahoo,
)


def _payload() -> bytes:
    timestamps = [int(value.timestamp()) for value in pd.date_range("2025-10-31", periods=4, freq="B", tz="UTC")]
    # A 1:10 reverse split occurs after the first two rows. Yahoo reports
    # split-adjusted historical quotes, so the first two rows are ten times
    # the contemporaneous local price and one tenth of the local volume.
    document = {
        "chart": {
            "result": [
                {
                    "timestamp": timestamps,
                    "meta": {"symbol": "TEST", "instrumentType": "EQUITY"},
                    "indicators": {
                        "quote": [
                            {
                                "open": [100.0, 110.0, 12.0, 13.0],
                                "high": [101.0, 111.0, 12.5, 13.5],
                                "low": [99.0, 109.0, 11.5, 12.5],
                                "close": [100.0, 110.0, 12.0, 13.0],
                                "volume": [10.0, 20.0, 300.0, 400.0],
                            }
                        ]
                    },
                    "events": {
                        "splits": {
                            str(timestamps[2]): {
                                "numerator": 1.0,
                                "denominator": 10.0,
                                "splitRatio": "1:10",
                            }
                        }
                    },
                }
            ],
            "error": None,
        }
    }
    return json.dumps(document).encode()


def test_parse_yahoo_retains_raw_quotes_and_split_provenance() -> None:
    frame, metadata = _parse_yahoo(_payload(), "TEST")
    assert metadata["instrument_type"] == "EQUITY"
    assert frame["close"].tolist() == [100.0, 110.0, 12.0, 13.0]
    assert frame["volume"].tolist() == [10.0, 20.0, 300.0, 400.0]
    assert metadata["split_events"][0]["split_ratio"] == "1:10"


def test_overlap_validation_requires_stable_scale_and_ohlc() -> None:
    source, _ = _parse_yahoo(_payload(), "TEST")
    local = source.copy()
    report = _overlap_validation(source, local)
    assert report["sessions"] == 4
    assert report["passed"] is False  # fewer than the production minimum
    expanded = pd.concat([local] * 6, ignore_index=True)
    expanded["date"] = pd.date_range("2024-01-01", periods=len(expanded), freq="B")
    source_expanded = expanded.copy()
    report = _overlap_validation(source_expanded, expanded)
    assert report["passed"] is True


def test_overlap_validation_can_be_scoped_to_a_stable_recent_tail() -> None:
    source, _ = _parse_yahoo(_payload(), "TEST")
    local = source.copy()
    # Simulate an old-provider scale discontinuity while retaining a stable
    # final tail, as happens around a historical reverse split.
    local.loc[:1, ["open", "high", "low", "close"]] *= 10
    full = _overlap_validation(source, local)
    tail = _overlap_validation(source.iloc[2:], local.iloc[2:])
    assert full["passed"] is False
    assert tail["passed"] is False  # still below the 20-session gate


def test_merge_missing_preserves_existing_rows(tmp_path) -> None:
    path = tmp_path / "test.csv"
    old = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-01", "2024-01-02"]),
            "ticker": ["TEST", "TEST"],
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [10.0, 20.0],
        }
    )
    old.to_csv(path, index=False)
    incoming = old.copy()
    incoming.loc[0, "close"] = 99.0
    incoming.loc[1, "date"] = pd.Timestamp("2024-01-03")
    added = _merge_missing(path, incoming, "TEST")
    result = pd.read_csv(path, parse_dates=["date"])
    assert added == 1
    assert result.loc[result["date"] == pd.Timestamp("2024-01-01"), "close"].iloc[0] == 1.0
    assert result["date"].tolist() == list(pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]))
