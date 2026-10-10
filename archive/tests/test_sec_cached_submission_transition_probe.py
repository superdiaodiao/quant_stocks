import gzip
import hashlib
import json

import pytest

from scripts.sec_cached_submission_transition_probe import build_probe
from scripts.sec_submission_triage import _payload_sha256


def _write_cache(path, payload, cik=123):
    envelope = {
        "format_version": 1,
        "cik": cik,
        "source_url": f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
        "fetched_at": "2026-08-09T00:00:00+00:00",
        "payload_sha256": _payload_sha256(payload),
        "payload": payload,
    }
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(envelope, handle, sort_keys=True)
    return envelope


def test_build_probe_replays_cached_submission_and_filters_current_audit(tmp_path):
    cache = tmp_path / "CIK0000000123.json.gz"
    payload = {
        "name": "Old Corp",
        "tickers": ["NEW", "NEWW"],
        "exchanges": ["Nasdaq", "Nasdaq"],
        "formerNames": [],
    }
    envelope = _write_cache(cache, payload)
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"records": [{
        "ticker": "OLD",
        "cik": 123,
        "current_sec_tickers": ["NEWW", "NEW"],
        "cache_path": str(cache),
        "cache_payload_sha256": envelope["payload_sha256"],
    }, {
        "ticker": "DONE",
        "cik": 456,
        "current_sec_tickers": ["OTHER"],
        "cache_path": "not-read.json.gz",
        "cache_payload_sha256": "unused",
    }]}))
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({
        "unresolved_terminal_return_histories": [{"ticker": "OLD"}]
    }))
    output = tmp_path / "probe.json"
    report = build_probe(triage, audit_path=audit, output=output)
    assert report["completed_ticker_count"] == 1
    result = report["results"][0]
    assert result["ticker"] == "OLD"
    assert result["matches"][0]["cik"] == "0000000123"
    assert result["issuers"][0]["current_tickers"] == ["NEW", "NEWW"]
    assert result["issuers"][0]["submission_payload_sha256"] == (
        envelope["payload_sha256"]
    )
    assert output.exists()


def test_build_probe_rejects_tampered_triage_sha(tmp_path):
    cache = tmp_path / "cache.json.gz"
    _write_cache(cache, {"name": "Old Corp", "tickers": ["NEW"]})
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"records": [{
        "ticker": "OLD",
        "cik": 123,
        "current_sec_tickers": ["NEW"],
        "cache_path": str(cache),
        "cache_payload_sha256": hashlib.sha256(b"wrong").hexdigest(),
    }]}))
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({
        "unresolved_terminal_return_histories": [{"ticker": "OLD"}]
    }))
    with pytest.raises(ValueError, match="evidence mismatch"):
        build_probe(triage, audit_path=audit, output=tmp_path / "out.json")
