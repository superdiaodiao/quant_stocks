import gzip
import json
from pathlib import Path

from scripts.cross_market_terminal_evidence import (
    ECB_ILS_URL,
    ECB_USD_URL,
    TASE_REQUEST,
    TASE_URL,
    _read_cache,
    build_mgic_evidence,
)


def _payloads():
    tase = json.dumps(
        {
            "Items": [
                {
                    "TradeDate": "24/02/2026",
                    "CloseRate": 9795.0,
                    "AdjustmentRate": 9586.863,
                }
            ]
        }
    ).encode()
    header = "TIME_PERIOD,CURRENCY,OBS_VALUE\n"
    ils = (header + "2026-02-24,ILS,3.6695\n").encode()
    usd = (header + "2026-02-24,USD,1.1777\n").encode()
    return {TASE_URL: tase, ECB_ILS_URL: ils, ECB_USD_URL: usd}


def test_cross_market_evidence_uses_unadjusted_tase_close_and_ecb_cross(tmp_path):
    payloads = _payloads()

    def fetcher(url, request_body):
        return payloads[url]

    report = build_mgic_evidence(
        cache_dir=tmp_path,
        refresh_missing_only=True,
        fetcher=fetcher,
    )

    assert report["successor_close_ils"] == 97.95
    expected_usd = 97.95 / 3.6695 * 1.1777
    assert abs(report["successor_close_usd"] - expected_usd) < 1e-12
    consideration = 0.5878202 * expected_usd
    assert abs(report["total_consideration_usd"] - consideration) < 1e-12
    assert abs(report["terminal_return"] - (consideration / 17.38 - 1)) < 1e-12


def test_cross_market_cache_rejects_tampered_payload(tmp_path):
    payloads = _payloads()
    build_mgic_evidence(
        cache_dir=tmp_path,
        refresh_missing_only=True,
        fetcher=lambda url, request_body: payloads[url],
    )
    cache = tmp_path / "tase_mtrx_page4.json.gz"
    with gzip.open(cache, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    envelope["payload_hex"] = b"tampered".hex()
    cache.write_bytes(gzip.compress(json.dumps(envelope).encode(), mtime=0))

    try:
        _read_cache(cache, source_url=TASE_URL, request_body=TASE_REQUEST)
    except ValueError as error:
        assert "payload SHA mismatch" in str(error)
    else:
        raise AssertionError("tampered cache was accepted")
