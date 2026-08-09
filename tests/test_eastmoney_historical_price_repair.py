import gzip
import json

import pandas as pd

from scripts.eastmoney_historical_price_repair import (
    _load_or_fetch,
    _parse_prices,
    _source_url,
    repair_tickers,
)


def _payload(ticker: str, dates: pd.DatetimeIndex) -> bytes:
    return json.dumps({
        "rc": 0,
        "data": {
            "code": ticker,
            "name": "Test Corp",
            "klines": [
                f"{date:%Y-%m-%d},10,10.5,11,9.5,1000,0,0,0,0,0"
                for date in dates
            ],
        },
    }).encode()


def test_parse_prices_preserves_response_identity() -> None:
    dates = pd.date_range("2025-01-02", periods=2, freq="B")
    frame, identity = _parse_prices(_payload("TEST", dates), "TEST")
    assert frame.columns.tolist() == [
        "date", "ticker", "open", "high", "low", "close", "volume"
    ]
    assert frame["ticker"].unique().tolist() == ["TEST"]
    assert identity == {
        "response_code": 0,
        "provider_code": "TEST",
        "provider_name": "Test Corp",
    }


def test_source_url_uses_explicit_market_and_unadjusted_daily_data() -> None:
    url = _source_url("TEST", "106")
    assert "secid=106.TEST" in url
    assert "klt=101" in url
    assert "fqt=0" in url


def test_cached_payload_is_reused_without_network(tmp_path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    payload = _payload("TEST", pd.date_range("2025-01-02", periods=2, freq="B"))
    path = cache / "test_105.json.gz"
    path.write_bytes(gzip.compress(payload, mtime=0))
    actual, actual_path = _load_or_fetch(cache, "TEST", "105", "unused", False)
    assert actual == payload
    assert actual_path == path


def test_repair_requires_exactly_one_identity_and_overlap_validated_market(
    tmp_path, monkeypatch
) -> None:
    dates = pd.date_range("2025-01-02", periods=25, freq="B")
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    pd.DataFrame({
        "date": dates,
        "ticker": "TEST",
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "close": 10.5,
        "volume": 1000,
    }).to_csv(price_dir / "test.csv", index=False)

    def fake_fetch(cache_dir, ticker, market_id, url, refresh):
        payload = (
            _payload(ticker, dates)
            if market_id == "106"
            else json.dumps({"rc": 0, "data": None}).encode()
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{ticker.lower()}_{market_id}.json.gz"
        path.write_bytes(gzip.compress(payload, mtime=0))
        return payload, path

    monkeypatch.setattr(
        "scripts.eastmoney_historical_price_repair._load_or_fetch", fake_fetch
    )
    result = repair_tickers(
        ["TEST"],
        start="2025-01-01",
        end="2025-12-31",
        price_dir=price_dir,
        cache_dir=tmp_path / "cache",
        output=tmp_path / "report.json",
    )
    record = result["records"][0]
    assert record["selected_market_id"] == "106"
    assert record["status"] == "NO_NEW_ROWS"
    assert [item["status"] for item in record["market_attempts"]] == [
        "NO_DATA", "IDENTITY_AND_OVERLAP_CONFIRMED", "NO_DATA"
    ]
