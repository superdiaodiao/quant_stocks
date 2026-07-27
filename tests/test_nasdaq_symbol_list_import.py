import pandas as pd

from src.io import nasdaq_update


def test_import_symbol_list_uses_explicit_first_known_date(tmp_path, monkeypatch):
    source = tmp_path / "symbols.txt"
    source.write_text("A\nB\nnot a symbol\n", encoding="utf-8")
    destination = tmp_path / "universe" / "nasdaq_300M.csv"
    monkeypatch.setattr(nasdaq_update, "NASDAQ_300M_STOCK_LIST_FILE", destination)
    result = nasdaq_update.import_nasdaq_symbol_list(
        source, pd.Timestamp("2019-06-17").date(), minimum_rows=2
    )
    frame = pd.read_csv(result["snapshot"])
    assert frame["Symbol"].tolist() == ["A", "B"]
    assert frame["Observed At"].astype(str).unique().tolist() == ["2019-06-17"]
