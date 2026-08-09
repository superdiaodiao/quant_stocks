import json
import zipfile

import pandas as pd

from scripts.sina_historical_price_repair import (
    _decode_response,
    _fixed_mirror_sec_cross_validation,
    _longest_stable_tail_validation,
    _parse_prices,
    _sec_identity_cross_validation,
    _stooq_cross_validation,
)


DECODER = "function d(value) { return JSON.parse(value); }"


def test_decode_response_uses_sina_assignment_payload() -> None:
    rows = [{"date": "2025-01-02", "close": 3.5}]
    payload = f'var TEST={json.dumps(json.dumps(rows))};'.encode()
    assert _decode_response(payload, DECODER) == rows


def test_parse_prices_normalizes_dates_and_columns() -> None:
    rows = [
        {
            "date": "2025-01-02T00:00:00.000Z",
            "open": 1,
            "high": 2,
            "low": 0.5,
            "close": 1.5,
            "volume": 10,
            "amount": 15,
        }
    ]
    payload = f'var TEST={json.dumps(json.dumps(rows))};'.encode()
    frame = _parse_prices(payload, "TEST", DECODER)
    assert frame.columns.tolist() == [
        "date", "ticker", "open", "high", "low", "close", "volume"
    ]
    assert frame.loc[0, "date"] == pd.Timestamp("2025-01-02")
    assert frame.loc[0, "ticker"] == "TEST"


def test_stooq_cross_validation_requires_independent_full_overlap(tmp_path) -> None:
    dates = pd.date_range("2025-01-02", periods=25, freq="B")
    sina = pd.DataFrame({
        "date": dates,
        "ticker": "TEST",
        "open": range(1, 26),
        "high": [value + 1 for value in range(1, 26)],
        "low": [value - 0.5 for value in range(1, 26)],
        "close": [value + 0.25 for value in range(1, 26)],
        "volume": [value * 100 for value in range(1, 26)],
    })
    lines = ["<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>"]
    lines.extend(
        f"TEST.US,D,{row.date.strftime('%Y%m%d')},000000,{row.open},{row.high},{row.low},{row.close},{row.volume},0"
        for row in sina.itertuples(index=False)
    )
    archive = tmp_path / "stooq.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(
            "data/daily/us/nasdaq stocks/1/test.us.txt", "\n".join(lines)
        )
    report = _stooq_cross_validation(archive, "TEST", sina)
    assert report["passed"] is True
    assert report["member_validations"][0]["cross_validation"]["sessions"] == 25


def test_fixed_mirror_sec_fallback_requires_complete_identity_bound_local_file(tmp_path) -> None:
    dates = pd.date_range("2025-07-07", periods=3, freq="B")
    local = pd.DataFrame({
        "date": dates,
        "ticker": "TEST",
        "open": [10.0, 10.1, 10.2],
        "high": [10.2, 10.3, 10.4],
        "low": [9.9, 10.0, 10.1],
        "close": [10.1, 10.2, 10.3],
        "volume": [100, 200, 300],
    })
    mirror = tmp_path / "mirror.json"
    mirror.write_text(json.dumps({"runs": [{"results": [{
        "ticker": "TEST", "status": "updated", "rows": 3,
        "first_date": "2025-07-07", "last_date": "2025-07-09",
        "source_url": "https://raw.githubusercontent.com/org/repo/"
        + "a" * 40 + "/daily/us/nasdaq%20stocks/1/test.us.txt",
    }]}]}), encoding="utf-8")
    sec = tmp_path / "sec.json"
    sec.write_text(json.dumps({"results": [{
        "ticker": "TEST", "status": "ok", "search_url": "https://sec.example/search",
        "search_payload_sha256": "search-sha",
        "matches": [{"cik": "123", "display_name": "Test Corp (TEST, TESTW) (CIK 0000000123)"}],
        "issuers": [{"cik": "0000000123", "submission_payload_sha256": "issuer-sha", "current_tickers": ["NEXT"]}],
    }]}), encoding="utf-8")
    overlap = {
        "sessions": 3, "ohlc_within_1pct": 1.0,
        "field_within_1pct": {field: 1.0 for field in ["open", "high", "low", "close"]},
        "volume_median_ratio": 1.0005,
        "volume_within_0_1pct": 1.0,
    }
    report = _fixed_mirror_sec_cross_validation(
        ticker="TEST", local=local, overlap=overlap,
        mirror_provenance_path=mirror, sec_probe_path=sec,
    )
    assert report["passed"] is True
    assert report["exact_recent_overlap"] is True
    assert report["sec_cik"] == "0000000123"
    assert report["mirror_commit"] == "a" * 40

    rejected = _fixed_mirror_sec_cross_validation(
        ticker="TEST",
        local=local,
        overlap={**overlap, "volume_median_ratio": 1.0011},
        mirror_provenance_path=mirror,
        sec_probe_path=sec,
    )
    assert rejected["passed"] is False
    assert rejected["exact_recent_overlap"] is False


def test_longest_stable_tail_ignores_an_older_split_scale() -> None:
    dates = pd.date_range("2025-01-02", periods=55, freq="B")
    source = pd.DataFrame({
        "date": dates, "ticker": "TEST", "open": 1.0, "high": 1.1,
        "low": 0.9, "close": 1.0, "volume": 100,
    })
    local = source.copy()
    for field in ["open", "high", "low", "close"]:
        local.loc[:29, field] *= 10
    report = _longest_stable_tail_validation(source, local)
    assert report["passed"] is True
    assert report["sessions"] == 25
    assert report["close_median_ratio"] == 1.0


def test_sec_identity_cross_validation_accepts_unique_multi_ticker_cik(tmp_path) -> None:
    sec = tmp_path / "sec.json"
    sec.write_text(json.dumps({"results": [{
        "ticker": "TEST", "status": "ok", "search_url": "https://sec.example/search",
        "search_query": "Test Corp", "search_payload_sha256": "search-sha",
        "matches": [{"cik": "123", "display_name": "Test Corp (TEST, TESTW) (CIK 0000000123)"}],
        "issuers": [{"cik": "0000000123", "submission_payload_sha256": "issuer-sha", "current_tickers": ["NEXT"]}],
    }]}), encoding="utf-8")
    report = _sec_identity_cross_validation("TEST", sec)
    assert report["passed"] is True
    assert report["sec_cik"] == "0000000123"
    assert report["current_tickers"] == ["NEXT"]
