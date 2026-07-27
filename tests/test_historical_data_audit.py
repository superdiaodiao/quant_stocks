import pandas as pd

from src.research import historical_data_audit


def test_snapshot_price_coverage_requires_current_price_and_full_lookback(
    tmp_path, monkeypatch
):
    dates = pd.date_range("2024-01-01", periods=260, freq="B")
    pd.DataFrame({"date": dates, "close": 10.0}).to_csv(tmp_path / "a.csv", index=False)
    pd.DataFrame({"date": dates[-10:], "close": 20.0}).to_csv(tmp_path / "b.csv", index=False)
    monkeypatch.setattr(historical_data_audit, "CLEANED_PRICE_DATA_DIR", tmp_path)
    observed_at = dates[-1]
    report = historical_data_audit.audit_snapshot_price_coverage(
        {observed_at: {"A", "B", "C"}}, start=str(observed_at.date())
    )
    row = report["by_snapshot"][0]
    assert row["price_current"] == 2
    assert row["lookback_ready"] == 1
    assert report["missing_price_symbols"] == ["C"]
    assert not report["complete"]
