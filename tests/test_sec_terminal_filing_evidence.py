import json

import pandas as pd

from scripts.sec_terminal_filing_evidence import build_evidence


def test_terminal_filing_evidence_caches_and_replays_exact_bytes(tmp_path):
    prices = tmp_path / "prices"
    prices.mkdir()
    pd.DataFrame({"date": ["2025-01-02"], "close": [9.5]}).to_csv(
        prices / "old.csv", index=False
    )
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"records": [{
        "ticker": "OLD",
        "cik": 123,
        "last_local_price_date": "2025-01-02",
        "resolution_review": "PRICE_SOURCE_AND_TERMINAL_RETURN_REVIEW",
        "event_filings_after_local_price": [{
            "filing_date": "2025-01-03",
            "form": "8-K",
            "accession": "0001-25-000001",
            "source_url": "https://www.sec.gov/example.htm",
        }],
    }]}), encoding="utf-8")
    html = b"<html>At the merger, each common share was converted into the right to receive $10.00 in cash per share.</html>"

    online = build_evidence(
        triage, cache_dir=tmp_path / "cache", price_dir=prices,
        refresh=True, fetcher=lambda _url: html,
    )
    lead = online["records"][0]["amount_leads"][0]
    assert lead["amount"] == 10.0
    assert lead["classification"] == "REVIEW_PER_SHARE_AMOUNT"
    assert abs(lead["implied_terminal_return"] - (10 / 9.5 - 1)) < 1e-12

    offline = build_evidence(
        triage, cache_dir=tmp_path / "cache", price_dir=prices,
        fetcher=lambda _url: (_ for _ in ()).throw(AssertionError("network used")),
    )
    assert offline["mode"] == "offline_cache"
    assert offline["records"][0]["payload_sha256"] == online["records"][0]["payload_sha256"]

    missing_only = build_evidence(
        triage, cache_dir=tmp_path / "cache", price_dir=prices,
        refresh_missing_only=True,
        fetcher=lambda _url: (_ for _ in ()).throw(AssertionError("network used")),
    )
    assert missing_only["mode"] == "refresh_missing_only"


def test_terminal_filing_evidence_does_not_treat_option_price_as_cash_lead(tmp_path):
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"records": [{
        "ticker": "OLD", "resolution_review": "TERMINAL_RETURN_REVIEW",
        "event_filings_after_local_price": [{
            "form": "8-K", "accession": "x", "source_url": "https://www.sec.gov/x"
        }],
    }]}), encoding="utf-8")
    report = build_evidence(
        triage, cache_dir=tmp_path / "cache", price_dir=tmp_path,
        refresh=True,
        fetcher=lambda _url: b"Each share option award had an exercise price of $3.00 per share in connection with the merger.",
    )
    assert report["records"][0]["amount_leads"][0]["classification"] == "REJECT_LIKELY_SECURITY_AWARD_TERM"


def test_terminal_filing_evidence_keeps_direct_common_cash_when_awards_follow(tmp_path):
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"records": [{
        "ticker": "OLD", "resolution_review": "TERMINAL_RETURN_REVIEW",
        "event_filings_after_local_price": [{
            "form": "8-K", "accession": "x", "source_url": "https://www.sec.gov/x"
        }],
    }]}), encoding="utf-8")
    report = build_evidence(
        triage, cache_dir=tmp_path / "cache", price_dir=tmp_path,
        refresh=True,
        fetcher=lambda _url: (
            b"Each outstanding share of Company Common Stock was converted into "
            b"the right to receive an amount in cash equal to $38.50 per share. "
            b"In addition, each restricted stock award was cancelled."
        ),
    )
    lead = report["records"][0]["amount_leads"][0]
    assert lead["amount"] == 38.5
    assert lead["classification"] == "REVIEW_FIXED_COMMON_SHARE_CASH"


def test_terminal_filing_evidence_finds_direct_cash_without_per_share_suffix(
    tmp_path,
):
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"records": [{
        "ticker": "OLD", "resolution_review": "TERMINAL_RETURN_REVIEW",
        "event_filings_after_local_price": [{
            "form": "8-K", "accession": "x", "source_url": "https://www.sec.gov/x"
        }],
    }]}), encoding="utf-8")
    report = build_evidence(
        triage, cache_dir=tmp_path / "cache", price_dir=tmp_path,
        refresh=True,
        fetcher=lambda _url: (
            b"Each share of Company Common Stock was converted into the right "
            b"to receive cash in an amount equal to $10.00 (the Merger "
            b"Consideration), payable to the holder without interest."
        ),
    )
    lead = report["records"][0]["amount_leads"][0]
    assert lead["amount"] == 10.0
    assert lead["classification"] == "REVIEW_FIXED_COMMON_SHARE_CASH"


def test_terminal_filing_evidence_can_select_latest_event_filing(tmp_path):
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"records": [{
        "ticker": "OLD", "resolution_review": "TERMINAL_RETURN_REVIEW",
        "event_filings_after_local_price": [
            {"form": "8-K", "accession": "first", "source_url": "https://www.sec.gov/first"},
            {"form": "8-K", "accession": "last", "source_url": "https://www.sec.gov/last"},
        ],
    }]}), encoding="utf-8")
    report = build_evidence(
        triage, cache_dir=tmp_path / "cache", price_dir=tmp_path,
        refresh=True, filing_selection="latest",
        fetcher=lambda url: url.encode(),
    )

    assert report["records"][0]["filing"]["accession"] == "last"
    assert report["filing_selection"] == "latest"
