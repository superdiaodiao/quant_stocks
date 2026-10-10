import base64
import gzip
import hashlib
import json
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scripts.yahoo_historical_price_repair import _overlap_validation, _read_prices
from src.conf import PROJECT_PATH
from src.io.security_identity import issuer_rename_transitions


EVIDENCE_PATH = (
    Path(PROJECT_PATH)
    / "output/data_provenance/sec_security_identity_comm_visn_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_comm_visn_identity_transition_replays_sec_and_price_evidence():
    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    sec = evidence["sec_evidence"]
    continuity = evidence["price_continuity"]

    cache_path = Path(PROJECT_PATH) / sec["cache_path"]
    assert _sha256(cache_path) == sec["cache_sha256"]
    envelope = json.loads(gzip.decompress(cache_path.read_bytes()))
    payload = base64.b64decode(envelope["payload_base64"])
    assert hashlib.sha256(payload).hexdigest() == sec["payload_sha256"]
    text = re.sub(
        r"\s+", " ", BeautifulSoup(payload, "html.parser").get_text(" ")
    )
    assert all(phrase in text for phrase in sec["required_phrases"])

    historical_path = Path(PROJECT_PATH) / continuity["historical_price_path"]
    provider_path = Path(PROJECT_PATH) / continuity["provider_price_path"]
    assert _sha256(historical_path) == continuity["historical_price_sha256"]
    assert _sha256(provider_path) == continuity["provider_price_sha256"]
    validation = _overlap_validation(
        _read_prices(historical_path), _read_prices(provider_path)
    )
    assert validation["passed"] is True
    assert validation["scale_consistent"] is True
    assert validation["sessions"] == continuity["overlap_sessions"]
    assert validation["close_median_ratio"] == pytest.approx(
        continuity["close_median_ratio"]
    )
    assert validation["ohlc_within_1pct"] == pytest.approx(
        continuity["ohlc_within_1pct"]
    )

    transition = issuer_rename_transitions()
    row = transition.loc[
        transition["historical_ticker"].eq("COMM")
        & transition["provider_ticker"].eq("VISN")
    ].iloc[0]
    assert row.last_historical_date.strftime("%Y-%m-%d") == "2026-01-13"
    assert row.current_ticker_first_date.strftime("%Y-%m-%d") == "2026-01-14"
    assert evidence["formal_price_rows_modified"] is False
    assert evidence["terminal_returns_modified"] is False
