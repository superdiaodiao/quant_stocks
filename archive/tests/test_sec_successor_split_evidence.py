import gzip
import hashlib
import json
from pathlib import Path

from scripts.sec_terminal_filing_evidence import _filing_text
from src.conf import PROJECT_PATH


EVIDENCE = Path(PROJECT_PATH) / "output/data_provenance/sec_successor_split_veee_2026-08-09.json"


def test_veee_successor_split_evidence_replays_offline() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert evidence["verified"] is True
    assert [record["reverse_split_ratio"] for record in evidence["records"]] == [10, 37]
    for record in evidence["records"]:
        with gzip.open(record["cache_path"], "rt", encoding="utf-8") as handle:
            envelope = json.load(handle)
        payload = bytes.fromhex(envelope["payload_hex"])
        assert hashlib.sha256(payload).hexdigest() == record["payload_sha256"]
        text = _filing_text(payload)
        assert all(
            phrase.lower() in text.lower() for phrase in record["required_phrases"]
        )
