import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.sec_terminal_filing_evidence import _filing_text
from src.conf import PROJECT_PATH
from src.io.terminal_returns import load_observed_terminal_returns


EVIDENCE = Path(PROJECT_PATH) / "output/data_provenance/sec_cash_merger_terminal_evidence_2026-08-09.json"


def test_sec_cash_merger_terminal_evidence_replays_exact_cash_and_payloads() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    terminal = load_observed_terminal_returns().set_index(["ticker", "last_price_date"])
    assert evidence["research_only"] is True
    assert len(evidence["records"]) == 21
    for record in evidence["records"]:
        path = Path(PROJECT_PATH) / record["sec_cache_path"]
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            envelope = json.load(handle)
        payload = bytes.fromhex(envelope["payload_hex"])
        assert hashlib.sha256(payload).hexdigest() == record["sec_filing_payload_sha256"]
        assert record["required_phrase"] in _filing_text(payload)
        if record.get("completion_required_phrase") and not record.get(
            "completion_sec_cache_path"
        ):
            assert record["completion_required_phrase"] in _filing_text(payload)
        if record.get("suspension_required_phrase"):
            assert record["suspension_required_phrase"] in _filing_text(payload)
        if record.get("fee_required_phrase"):
            assert record["fee_required_phrase"] in _filing_text(payload)
            assert abs(
                record["gross_cash_per_ads"]
                - record["ads_cancellation_fee"]
                - record["cash_per_share"]
            ) < 1e-12
        if record.get("completion_sec_cache_path"):
            completion_path = (
                Path(PROJECT_PATH) / record["completion_sec_cache_path"]
            )
            with gzip.open(completion_path, "rt", encoding="utf-8") as handle:
                completion_envelope = json.load(handle)
            completion_payload = bytes.fromhex(
                completion_envelope["payload_hex"]
            )
            assert (
                hashlib.sha256(completion_payload).hexdigest()
                == record["completion_sec_filing_payload_sha256"]
            )
            assert record["completion_required_phrase"] in _filing_text(
                completion_payload
            )
        if record.get("price_path"):
            price_path = Path(PROJECT_PATH) / record["price_path"]
            assert hashlib.sha256(price_path.read_bytes()).hexdigest() == (
                record["price_sha256"]
            )
            prices = pd.read_csv(price_path)
            last = prices.iloc[-1]
            assert last["date"] == record["last_price_date"]
            assert float(last["close"]) == record["last_close"]
            provenance_path = Path(PROJECT_PATH) / record["price_provenance_path"]
            assert hashlib.sha256(provenance_path.read_bytes()).hexdigest() == (
                record["price_provenance_sha256"]
            )
            sina_cache = Path(PROJECT_PATH) / record["sina_cache_path"]
            assert hashlib.sha256(sina_cache.read_bytes()).hexdigest() == (
                record["sina_cache_sha256"]
            )
            assert hashlib.sha256(gzip.decompress(sina_cache.read_bytes())).hexdigest() == (
                record["sina_payload_sha256"]
            )
        expected = record["cash_per_share"] / record["last_close"] - 1.0
        assert abs(expected - record["terminal_return"]) < 1e-12
        formal = terminal.loc[(record["ticker"], record["last_price_date"])]
        assert abs(float(formal["terminal_return"]) - expected) < 1e-12
