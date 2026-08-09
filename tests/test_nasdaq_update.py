from datetime import date

import pandas as pd

from src.io import nasdaq_update


def _configure_partial_update(tmp_path, monkeypatch):
    universe = tmp_path / "universe.csv"
    index = tmp_path / "index.csv"
    price_dir = tmp_path / "prices"
    pd.DataFrame({
        "Symbol": ["ABC", "DEF"],
        "Name": ["ABC Common Stock", "DEF Common Stock"],
    }).to_csv(universe, index=False)
    pd.DataFrame({
        "date": ["2026-07-29"],
        "close": [100],
    }).to_csv(index, index=False)
    monkeypatch.setattr(
        nasdaq_update, "NASDAQ_300M_STOCK_LIST_FILE", str(universe)
    )
    monkeypatch.setattr(nasdaq_update, "NASDAQ_INDEX_FILE", str(index))
    monkeypatch.setattr(nasdaq_update, "CLEANED_PRICE_DATA_DIR", str(price_dir))
    monkeypatch.setattr(
        nasdaq_update,
        "refresh_universe",
        lambda _end: (_ for _ in ()).throw(
            AssertionError("partial update refreshed formal universe")
        ),
    )
    return universe


def test_targeted_update_requests_only_explicit_tickers(
    tmp_path, monkeypatch
):
    universe = _configure_partial_update(tmp_path, monkeypatch)
    requested = []

    def fake_update(ticker, _end, _price_dir):
        requested.append(ticker)
        return {"ticker": ticker, "status": "current", "rows": 0}

    monkeypatch.setattr(nasdaq_update, "update_ticker", fake_update)
    before = universe.read_bytes()

    result = nasdaq_update.update_all(
        date(2026, 7, 29),
        workers=1,
        tickers=["metcb", "METCB"],
    )

    assert requested == ["METCB"]
    assert result["requested_ticker_count"] == 1
    assert result["requested_tickers"] == ["METCB"]
    assert result["counts"] == {"current": 1}
    assert result["universe"]["mode"] == "retained_for_partial_update"
    assert result["universe"]["snapshot"] is None
    assert universe.read_bytes() == before


def test_limited_update_does_not_advance_formal_universe(
    tmp_path, monkeypatch
):
    universe = _configure_partial_update(tmp_path, monkeypatch)
    requested = []

    def fake_update(ticker, _end, _price_dir):
        requested.append(ticker)
        return {"ticker": ticker, "status": "current", "rows": 0}

    monkeypatch.setattr(nasdaq_update, "update_ticker", fake_update)
    before = universe.read_bytes()

    result = nasdaq_update.update_all(
        date(2026, 7, 29),
        workers=1,
        limit=1,
    )

    assert requested == ["ABC"]
    assert result["requested_ticker_count"] == 1
    assert result["requested_tickers"] is None
    assert result["universe"]["mode"] == "retained_for_partial_update"
    assert universe.read_bytes() == before
