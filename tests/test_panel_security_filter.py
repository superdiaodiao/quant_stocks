import pandas as pd

from src.research import panel_data


def test_price_panel_excludes_known_non_common_securities(tmp_path, monkeypatch):
    dates = pd.date_range("2024-01-01", periods=160, freq="B")
    for ticker in ("common", "warrant", "unknown"):
        pd.DataFrame({
            "date": dates,
            "close": range(100, 260),
            "volume": [1000] * len(dates),
        }).to_csv(tmp_path / f"{ticker}.csv", index=False)
    monkeypatch.setattr(panel_data, "known_non_common_symbols", lambda: {"WARRANT"})
    close, _ = panel_data.load_panel(tmp_path, "2024-06-01", None)
    assert set(close.columns) == {"COMMON", "UNKNOWN"}
