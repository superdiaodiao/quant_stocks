import json

import pandas as pd

from src.io import nasdaq_update


def test_import_json_catalog_uses_explicit_observation_date(tmp_path, monkeypatch):
    source = tmp_path / "nasdaq.json"
    source.write_text(
        json.dumps([
            {"symbol": "A", "name": "A Common Stock"},
            {"symbol": "BW", "name": "B Warrant"},
        ]),
        encoding="utf-8",
    )
    destination = tmp_path / "universe" / "nasdaq_300M.csv"
    monkeypatch.setattr(nasdaq_update, "NASDAQ_300M_STOCK_LIST_FILE", destination)

    result = nasdaq_update.import_nasdaq_json_catalog(
        source, pd.Timestamp("2022-06-24").date(), minimum_rows=2
    )

    frame = pd.read_csv(result["snapshot"])
    assert frame["Symbol"].tolist() == ["A", "BW"]
    assert frame["Observed At"].astype(str).unique().tolist() == ["2022-06-24"]
    assert frame["Source Format"].unique().tolist() == ["json_catalog"]
