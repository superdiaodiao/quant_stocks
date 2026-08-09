import pandas as pd
import pytest

from scripts.sec_ticker_alias_price_repair import _cross_validate


def _frame(volume=(100.0, 100.0)):
    return pd.DataFrame({
        "date": pd.to_datetime(["2025-01-01", "2025-01-02"]),
        "open": [1.0, 1.1], "high": [1.2, 1.3], "low": [0.9, 1.0],
        "close": [1.1, 1.2], "volume": list(volume),
    })


def test_alias_cross_validation_keeps_ohlc_gate_and_records_volume_warning():
    left = pd.concat([_frame((100.0, 100.0))] * 60, ignore_index=True)
    right = left.copy()
    right.loc[::2, "volume"] = right.loc[::2, "volume"] * 1.2
    result = _cross_validate(left, right)
    assert result["passed"] is True
    assert result["volume_warning"] is True
    assert result["fields"]["close"]["within_1pct"] == 1.0


def test_alias_cross_validation_rejects_ohlc_mismatch():
    left = pd.concat([_frame()] * 60, ignore_index=True)
    right = left.copy()
    right.loc[::2, "close"] = right.loc[::2, "close"] * 1.5
    with pytest.raises(ValueError, match="OHLC cross-validation failed"):
        _cross_validate(left, right)
