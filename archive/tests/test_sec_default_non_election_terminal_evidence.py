import gzip
import base64
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.sec_terminal_filing_evidence import _filing_text
from src.conf import PROJECT_PATH
from src.io.terminal_returns import load_observed_terminal_returns


EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "sec_default_non_election_terminal_evidence_2026-08-09.json"
)


def test_default_non_election_terminal_evidence_replays_exact_cash_result():
    payload = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert payload["research_only"] is True
    assert payload["analysis_end"] == "2026-07-17"
    assert len(payload["records"]) == 2

    for record in payload["records"]:
        cache_path = Path(PROJECT_PATH) / record["sec_cache_path"]
        assert hashlib.sha256(cache_path.read_bytes()).hexdigest() == (
            record["sec_cache_sha256"]
        )
        with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
            envelope = json.load(handle)
        filing_payload = (
            bytes.fromhex(envelope["payload_hex"])
            if "payload_hex" in envelope
            else base64.b64decode(envelope["payload_base64"])
        )
        assert hashlib.sha256(filing_payload).hexdigest() == (
            record["sec_filing_payload_sha256"]
        )
        filing_text = _filing_text(filing_payload)
        assert record["required_cash_phrase"] in filing_text
        assert record["required_default_phrase"] in filing_text

        if "completion_sec_cache_path" in record:
            completion_path = (
                Path(PROJECT_PATH) / record["completion_sec_cache_path"]
            )
            assert hashlib.sha256(completion_path.read_bytes()).hexdigest() == (
                record["completion_sec_cache_sha256"]
            )
            with gzip.open(completion_path, "rt", encoding="utf-8") as handle:
                completion_envelope = json.load(handle)
            completion_payload = bytes.fromhex(completion_envelope["payload_hex"])
            assert hashlib.sha256(completion_payload).hexdigest() == (
                record["completion_sec_filing_payload_sha256"]
            )
            assert record["completion_required_phrase"] in _filing_text(
                completion_payload
            )

        price_path = Path(PROJECT_PATH) / record["historical_price_path"]
        assert hashlib.sha256(price_path.read_bytes()).hexdigest() == (
            record["historical_price_sha256"]
        )
        prices = pd.read_csv(price_path, parse_dates=["date"])
        final = prices.loc[
            prices["date"].eq(pd.Timestamp(record["last_price_date"]))
        ]
        assert len(final) == 1
        assert float(final.iloc[0]["close"]) == record["last_close"]

        expected = record["consideration_per_share"] / record["last_close"] - 1.0
        assert abs(expected - record["terminal_return"]) < 1e-15
        terminal = load_observed_terminal_returns().set_index(
            ["ticker", "last_price_date"]
        )
        formal = terminal.loc[
            (record["ticker"], pd.Timestamp(record["last_price_date"]))
        ]
        assert abs(float(formal["terminal_return"]) - expected) < 1e-15
        assert float(formal["consideration_per_share"]) == (
            record["consideration_per_share"]
        )
