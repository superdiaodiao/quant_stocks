from datetime import date
import json

import pandas as pd

from src.io import nasdaq_update


class _JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def test_refresh_universe_can_persist_only_investable_common_equities(
    tmp_path, monkeypatch
):
    template = tmp_path / "template.csv"
    target = tmp_path / "research" / "current_universe.csv"
    pd.DataFrame({
        "Symbol": ["OLD"],
        "Name": ["Old Company Common Stock"],
    }).to_csv(template, index=False)
    payload = {"data": {"table": {"rows": [
        {"symbol": "LIVE", "name": "Live Company Common Stock", "marketCap": "100"},
        {"symbol": "LIVEW", "name": "Live Company Warrant", "marketCap": "20"},
        {"symbol": None, "name": "Malformed row", "marketCap": "10"},
    ]}}}
    monkeypatch.setattr(nasdaq_update, "NASDAQ_300M_STOCK_LIST_FILE", str(template))
    monkeypatch.setattr(
        nasdaq_update, "urlopen", lambda *_args, **_kwargs: _JsonResponse(payload)
    )

    result = nasdaq_update.refresh_universe(
        date(2026, 8, 10), min_market_cap=0, target_path=target,
        common_equities_only=True,
    )

    assert pd.read_csv(target)["Symbol"].tolist() == ["LIVE"]
    assert pd.read_csv(result["snapshot"])["Symbol"].tolist() == ["LIVE"]
    assert result["unfiltered_count"] == 2
    assert result["excluded_non_common_securities"] == 1
    assert result["common_equities_only"] is True


def test_closed_index_snapshot_requires_matching_closed_official_session(
    monkeypatch,
):
    chart = {"data": {
        "timeAsOf": "Aug 10, 2026",
        "lastSalePrice": "26,605.36",
    }}
    info = {"data": {
        "marketStatus": "Closed",
        "primaryData": {"lastTradeTimestamp": "Aug 10, 2026"},
    }}

    def fake_urlopen(request, **_kwargs):
        payload = chart if "/chart" in request.full_url else info
        return _JsonResponse(payload)

    monkeypatch.setattr(nasdaq_update, "urlopen", fake_urlopen)

    result = nasdaq_update.fetch_closed_index_snapshot(
        "COMP", date(2026, 8, 10)
    )

    assert result["date"] == "2026-08-10"
    assert result["close"] == 26605.36
    assert result["market_status"] == "Closed"
    assert len(result["payload_sha256"]) == 64


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


def test_full_update_retries_only_failed_tickers_at_lower_concurrency(
    tmp_path, monkeypatch
):
    universe = _configure_partial_update(tmp_path, monkeypatch)
    attempts = {"ABC": 0, "DEF": 0}

    def fake_update(ticker, _end, _price_dir):
        attempts[ticker] += 1
        if ticker == "DEF" and attempts[ticker] == 1:
            raise RuntimeError("HTTP Error 403: Forbidden")
        return {"ticker": ticker, "status": "current", "rows": 0}

    monkeypatch.setattr(nasdaq_update, "update_ticker", fake_update)
    monkeypatch.setattr(nasdaq_update.time, "sleep", lambda _seconds: None)

    result = nasdaq_update.update_all(
        date(2026, 7, 29), workers=8, tickers=["ABC", "DEF"],
    )

    assert attempts == {"ABC": 1, "DEF": 2}
    assert result["counts"] == {"current": 2}
    assert result["failures"] == []
