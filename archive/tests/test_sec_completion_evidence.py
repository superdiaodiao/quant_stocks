import base64
import gzip
import json

import pytest

from scripts.sec_completion_evidence import (
    FILING_SPECS,
    _cache_path,
    _html_text,
    build_completion_evidence,
)


def _price_triage(path, ticker: str, assessment: str) -> None:
    path.write_text(
        json.dumps(
            {
                "research_only": True,
                "records": [
                    {
                        "ticker": ticker,
                        "assessment": assessment,
                        "cache_path": f"cache/{ticker}.json.gz",
                        "cache_payload_sha256": "source-sha",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_html_text_removes_nontext_and_normalizes_nonbreaking_spaces():
    payload = (
        b"<style>ignore $41.00</style><p>January&nbsp;14, 2026</p>"
        b"<script>ignore VISN</script>"
    )
    assert _html_text(payload) == "January 14, 2026"


def test_completion_evidence_caches_raw_avdx_and_replays_offline(tmp_path):
    cache_dir = tmp_path / "cache"
    price_triage = tmp_path / "price_triage.json"
    _price_triage(
        price_triage,
        "AVDX",
        "REVIEW_CONTINUOUS_BRIDGE_REQUIRES_TERMINAL_EVIDENCE",
    )
    formal_price_file = tmp_path / "formal_prices.csv"
    formal_price_file.write_bytes(b"must not be changed\n")
    before = formal_price_file.read_bytes()
    html = b"<html><body>The merger was completed. $10.00 per share.</body></html>"

    report = build_completion_evidence(
        ["avdx"],
        cache_dir=cache_dir,
        price_triage_path=price_triage,
        refresh=True,
        fetcher=lambda spec: html if spec["ticker"] == "AVDX" else b"",
    )

    record = report["records"][0]
    assert report["research_only"] is True
    assert report["formal_data_written"] is False
    assert record["evidence_status"] == (
        "RESEARCH_EVIDENCE_CACHED_REQUIRES_FORMAL_REVIEW"
    )
    assert all(item["found"] for item in record["marker_evidence"])
    assert record["price_triage_link"]["assessment"] == (
        "REVIEW_CONTINUOUS_BRIDGE_REQUIRES_TERMINAL_EVIDENCE"
    )
    assert _cache_path(cache_dir, FILING_SPECS["AVDX"]).exists()
    assert formal_price_file.read_bytes() == before

    offline = build_completion_evidence(
        ["AVDX"],
        cache_dir=cache_dir,
        price_triage_path=price_triage,
        fetcher=lambda _spec: (_ for _ in ()).throw(AssertionError("network used")),
    )
    assert offline["mode"] == "offline_cache"
    assert offline["records"][0]["raw_cache"]["payload_sha256"] == (
        record["raw_cache"]["payload_sha256"]
    )
    assert formal_price_file.read_bytes() == before


def test_completion_evidence_rejects_tampered_raw_bytes(tmp_path):
    cache_dir = tmp_path / "cache"
    report = build_completion_evidence(
        ["AVDX"],
        cache_dir=cache_dir,
        price_triage_path=tmp_path / "missing.json",
        refresh=True,
        fetcher=lambda _spec: b"<p>completed $10.00</p>",
    )
    assert report["records"][0]["evidence_status"] == (
        "RESEARCH_EVIDENCE_CACHED_REQUIRES_FORMAL_REVIEW"
    )

    path = _cache_path(cache_dir, FILING_SPECS["AVDX"])
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    envelope["payload_base64"] = base64.b64encode(b"tampered").decode("ascii")
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(envelope, handle)

    with pytest.raises(ValueError, match="payload hash mismatch"):
        build_completion_evidence(
            ["AVDX"],
            cache_dir=cache_dir,
            price_triage_path=tmp_path / "missing.json",
        )


def test_ppbi_evidence_preserves_stock_conversion_as_review_only(tmp_path):
    price_triage = tmp_path / "price_triage.json"
    _price_triage(
        price_triage,
        "PPBI",
        "REVIEW_CONTINUOUS_BRIDGE_REQUIRES_TERMINAL_EVIDENCE",
    )

    report = build_completion_evidence(
        ["PPBI"],
        cache_dir=tmp_path / "cache",
        price_triage_path=price_triage,
        refresh=True,
        fetcher=lambda _spec: (
            b"<p>The merger completed. Each share converted into 0.9150 shares. "
            b"Nasdaq will suspend trading.</p>"
        ),
    )

    record = report["records"][0]
    assert "stock-consideration" in record["event_scope"]
    assert all(item["found"] for item in record["marker_evidence"])
    assert any(
        "must not be represented as a cash terminal return" in requirement
        for requirement in record["required_before_formal_use"]
    )
    assert record["formal_data_actions_performed"] == []


@pytest.mark.parametrize(
    ("ticker", "expected_scope", "required_phrase"),
    [
        ("APLS", "contingent-value-right", "do not assume a CVR value"),
        ("CPRX", "cash-consideration", "$31.50 per-share"),
        ("COMM", "ticker-transition", "must not be represented as a cash"),
    ],
)
def test_reviewed_price_gap_events_remain_research_only(
    tmp_path, ticker, expected_scope, required_phrase
):
    spec = FILING_SPECS[ticker]
    html = (
        "<p>"
        + " ".join(str(marker) for marker in spec["expected_markers"])
        + "</p>"
    ).encode()

    report = build_completion_evidence(
        [ticker],
        cache_dir=tmp_path / "cache",
        price_triage_path=tmp_path / "price_triage.json",
        refresh=True,
        fetcher=lambda _spec: html,
    )

    record = report["records"][0]
    assert report["formal_data_written"] is False
    assert record["evidence_status"] == (
        "RESEARCH_EVIDENCE_CACHED_REQUIRES_FORMAL_REVIEW"
    )
    assert expected_scope in record["event_scope"]
    assert all(item["found"] for item in record["marker_evidence"])
    assert any(
        required_phrase in requirement
        for requirement in record["required_before_formal_use"]
    )
    assert record["formal_data_actions_performed"] == []


def test_completion_evidence_rejects_unreviewed_ticker(tmp_path):
    with pytest.raises(ValueError, match="no reviewed SEC completion filing spec"):
        build_completion_evidence(
            ["UNKNOWN"],
            cache_dir=tmp_path / "cache",
            price_triage_path=tmp_path / "price_triage.json",
        )
