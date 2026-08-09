import gzip
import json

import pandas as pd
import pytest

from scripts.stockanalysis_price_triage import (
    _read_cached_page,
    parse_stockanalysis_history,
    triage_stockanalysis_prices,
)


def _page(rows: list[dict]) -> bytes:
    serialized_rows = []
    for row in rows:
        serialized_rows.append(
            "{"
            f"c:{row['close']},h:{row['high']},l:{row['low']},"
            f"o:{row['open']},t:\"{row['date']}\",v:{row['volume']}"
            "}"
        )
    return (
        "<!doctype html><html><body>"
        "<p>Historical price data is provided by "
        "<a href=\"https://example.com\">S&amp;P Global Market Intelligence</a>."
        "</p><script>data:{id:1,symbol:\"OLD\",data:["
        + ",".join(serialized_rows)
        + "],form:null}</script></body></html>"
    ).encode("utf-8")


def _source_rows(dates: list[str]) -> list[dict]:
    return [
        {
            "date": date,
            "open": 10.0 + index,
            "high": 11.0 + index,
            "low": 9.0 + index,
            "close": 10.0 + index,
            "volume": 100.0 + index,
        }
        for index, date in enumerate(dates)
    ]


def _write_local(path, dates: list[str]) -> None:
    rows = _source_rows(dates)
    pd.DataFrame({
        "date": [row["date"] for row in rows],
        "ticker": ["OLD"] * len(rows),
        "open": [row["open"] for row in rows],
        "high": [row["high"] for row in rows],
        "low": [row["low"] for row in rows],
        "close": [row["close"] for row in rows],
        "volume": [row["volume"] for row in rows],
    }).to_csv(path, index=False)


def test_price_source_triage_caches_offline_and_never_writes_local_prices(tmp_path):
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    price_path = price_dir / "old.csv"
    local_dates = ["2025-07-07", "2025-07-08", "2025-07-09"]
    _write_local(price_path, local_dates)
    before = price_path.read_bytes()
    cache_dir = tmp_path / "cache"
    page = _page(_source_rows([
        *local_dates, "2025-07-10", "2025-07-11",
    ]))
    benchmark = pd.to_datetime([
        *local_dates, "2025-07-10", "2025-07-11",
    ])

    refreshed = triage_stockanalysis_prices(
        ["old"],
        price_dir=price_dir,
        cache_dir=cache_dir,
        refresh=True,
        analysis_end="2025-07-11",
        benchmark_dates=benchmark,
        fetcher=lambda ticker: page if ticker == "OLD" else b"",
        minimum_overlap_sessions=3,
    )

    record = refreshed["records"][0]
    assert refreshed["research_only"] is True
    assert record["status"] == "RESEARCH_LEAD_ONLY"
    assert record["assessment"] == (
        "REVIEW_REQUIRES_LICENSE_AND_FORMAL_DATA_AUTHORIZATION"
    )
    assert record["source_provider"] == "S&P Global Market Intelligence"
    assert record["coverage"]["source_covers_full_gap"] is True
    assert record["overlap"]["overlap_sessions"] == 3
    assert (cache_dir / "OLD.json.gz").exists()
    assert price_path.read_bytes() == before

    offline = triage_stockanalysis_prices(
        ["OLD"],
        price_dir=price_dir,
        cache_dir=cache_dir,
        analysis_end="2025-07-11",
        benchmark_dates=benchmark,
        fetcher=lambda _ticker: (_ for _ in ()).throw(
            AssertionError("network used")
        ),
        minimum_overlap_sessions=3,
    )
    assert offline["mode"] == "offline_cache"
    assert offline["records"][0]["cache_payload_sha256"] == (
        record["cache_payload_sha256"]
    )
    assert price_path.read_bytes() == before


def test_price_source_triage_reports_incomplete_source_coverage(tmp_path):
    report = triage_stockanalysis_prices(
        ["OLD"],
        price_dir=tmp_path / "prices",
        cache_dir=tmp_path / "cache",
        refresh=True,
        analysis_end="2025-07-11",
        benchmark_dates=pd.to_datetime([
            "2025-07-09", "2025-07-10", "2025-07-11",
        ]),
        fetcher=lambda _ticker: _page(_source_rows(["2025-07-10"])),
    )

    record = report["records"][0]
    assert record["status"] == "RESEARCH_LEAD_ONLY"
    assert record["assessment"] == "REVIEW_SOURCE_GAP_BEFORE_SOURCE_END"
    assert record["coverage"]["last_local_price_date"] is None
    assert record["coverage"]["expected_gap_sessions"] == 3
    assert record["coverage"]["source_covered_gap_sessions"] == 1
    assert record["coverage"]["source_missing_gap_sessions"] == 2
    assert record["coverage"]["source_missing_gap_session_sample"] == [
        "2025-07-09", "2025-07-11",
    ]
    assert record["coverage"]["source_covers_full_gap"] is False


def test_price_source_triage_requires_terminal_evidence_after_continuous_bridge(
    tmp_path,
):
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    price_path = price_dir / "old.csv"
    local_dates = ["2025-07-07", "2025-07-08", "2025-07-09"]
    _write_local(price_path, local_dates)
    report = triage_stockanalysis_prices(
        ["OLD"],
        price_dir=price_dir,
        cache_dir=tmp_path / "cache",
        refresh=True,
        analysis_end="2025-07-15",
        benchmark_dates=pd.to_datetime([
            *local_dates, "2025-07-10", "2025-07-11", "2025-07-14", "2025-07-15",
        ]),
        fetcher=lambda _ticker: _page(_source_rows([
            *local_dates, "2025-07-10", "2025-07-11",
        ])),
        minimum_overlap_sessions=3,
        sec_review_context={"OLD": "PRICE_SOURCE_AND_TERMINAL_RETURN_REVIEW"},
    )

    record = report["records"][0]
    assert record["coverage"]["source_bridges_from_local_to_source_end"]
    assert record["assessment"] == (
        "REVIEW_CONTINUOUS_BRIDGE_REQUIRES_TERMINAL_EVIDENCE"
    )


def test_price_source_cache_rejects_tampered_raw_bytes(tmp_path):
    cache_dir = tmp_path / "cache"
    triage_stockanalysis_prices(
        ["OLD"],
        price_dir=tmp_path / "prices",
        cache_dir=cache_dir,
        refresh=True,
        fetcher=lambda _ticker: _page(_source_rows(["2025-07-10"])),
    )
    cache_path = cache_dir / "OLD.json.gz"
    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    envelope["payload_base64"] = envelope["payload_base64"][:-4] + "AAAA"
    with gzip.open(cache_path, "wt", encoding="utf-8") as handle:
        json.dump(envelope, handle)

    with pytest.raises(ValueError, match="payload hash mismatch"):
        _read_cached_page(cache_dir, "OLD")


def test_price_page_parser_rejects_missing_ohlcv_field():
    payload = (
        b'<!doctype html><script>data:{id:1,data:[{c:1,h:1,l:1,o:1,t:"2025-07-10"}],form:null}</script>'
    )

    with pytest.raises(ValueError, match="missing volume"):
        parse_stockanalysis_history(payload)


def test_price_page_parser_accepts_javascript_leading_decimal_literals():
    payload = (
        b'<!doctype html><script>data:{id:1,data:[{c:1,h:1,l:1,o:1,t:"2025-07-10",v:1,ch:-.1}],form:null}</script>'
    )

    frame, _ = parse_stockanalysis_history(payload)

    assert frame["close"].tolist() == [1.0]
