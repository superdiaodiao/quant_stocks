import pandas as pd

from src.io import nasdaq_update


def test_import_csv_catalog_uses_explicit_observation_date(tmp_path, monkeypatch):
    source = tmp_path / "symbols.csv"
    source.write_text("A,Alpha Common Stock\nBW,Beta Warrant\n", encoding="utf-8")
    destination = tmp_path / "universe" / "nasdaq_300M.csv"
    monkeypatch.setattr(nasdaq_update, "NASDAQ_300M_STOCK_LIST_FILE", destination)

    result = nasdaq_update.import_nasdaq_csv_catalog(
        source, pd.Timestamp("2024-01-26").date(), minimum_rows=2
    )

    frame = pd.read_csv(result["snapshot"])
    assert frame["Symbol"].tolist() == ["A", "BW"]
    assert frame["Observed At"].astype(str).unique().tolist() == ["2024-01-26"]
    assert frame["Source Format"].unique().tolist() == ["csv_catalog"]
