import gzip
import hashlib
import json
from pathlib import Path

from scripts.sec_terminal_filing_evidence import _filing_text
from src.conf import PROJECT_PATH
from src.io.terminal_returns import load_observed_terminal_returns


EVIDENCE = Path(PROJECT_PATH) / "output/data_provenance/sec_foreign_cash_terminal_evidence_2026-08-09.json"


def _payload(record: dict, path_key: str, sha_key: str) -> bytes:
    with gzip.open(Path(PROJECT_PATH) / record[path_key], "rt") as handle:
        envelope = json.load(handle)
    payload = bytes.fromhex(envelope["payload_hex"])
    assert hashlib.sha256(payload).hexdigest() == record[sha_key]
    return payload


def test_foreign_cash_terminal_evidence_replays_sec_fx_and_formal_return() -> None:
    record = json.loads(EVIDENCE.read_text())["records"][0]
    sec_payload = _payload(record, "sec_cache_path", "sec_filing_payload_sha256")
    assert record["sec_required_phrase"] in _filing_text(sec_payload)
    fx_payload = _payload(record, "fx_cache_path", "fx_payload_sha256")
    observations = json.loads(fx_payload)["observations"]
    fx = next(
        float(row["FXCADUSD"]["v"])
        for row in observations if row["d"] == record["fx_observation_date"]
    )
    assert fx == record["cad_usd"]
    terminal_value = record["cash_per_share_cad"] * fx
    assert abs(terminal_value - record["terminal_value_usd"]) < 1e-12
    expected = terminal_value / record["last_close_usd"] - 1.0
    assert abs(expected - record["terminal_return"]) < 1e-12
    formal = load_observed_terminal_returns().set_index(
        ["ticker", "last_price_date"]
    ).loc[(record["ticker"], record["last_price_date"])]
    assert abs(float(formal["terminal_return"]) - expected) < 1e-12
