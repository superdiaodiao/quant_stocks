import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.sec_terminal_filing_evidence import _filing_text
from src.conf import PROJECT_PATH
from src.io.terminal_returns import load_observed_terminal_returns


EVIDENCE = (
    Path(PROJECT_PATH)
    / "output/data_provenance/cutr_zero_equity_terminal_evidence_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cached_payload(path: Path) -> bytes:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    payload = bytes.fromhex(envelope["payload_hex"])
    assert hashlib.sha256(payload).hexdigest() == envelope["payload_sha256"]
    return payload


def test_cutr_zero_equity_replays_effective_plan_and_no_distribution() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert evidence["research_only"] is True
    assert evidence["terminal_value_per_share"] == 0.0
    assert evidence["terminal_return"] == -1.0

    price_path = Path(PROJECT_PATH) / evidence["price_path"]
    assert _sha256(price_path) == evidence["price_sha256"]
    prices = pd.read_csv(price_path, parse_dates=["date"]).sort_values("date")
    last = prices.iloc[-1]
    assert last["date"].strftime("%Y-%m-%d") == evidence["last_price_date"]
    assert float(last["close"]) == evidence["last_close"]

    price_cache = Path(PROJECT_PATH) / evidence["price_cache_path"]
    assert hashlib.sha256(gzip.decompress(price_cache.read_bytes())).hexdigest() == (
        evidence["price_payload_sha256"]
    )

    plan_cache = Path(PROJECT_PATH) / evidence["sec_plan_cache_path"]
    assert _sha256(plan_cache) == evidence["sec_plan_cache_sha256"]
    plan_payload = _cached_payload(plan_cache)
    assert hashlib.sha256(plan_payload).hexdigest() == evidence["sec_plan_payload_sha256"]
    plan_text = _filing_text(plan_payload)
    assert all(phrase in plan_text for phrase in evidence["sec_plan_required_phrases"])

    completion_cache = Path(PROJECT_PATH) / evidence["issuer_completion_cache_path"]
    assert _sha256(completion_cache) == evidence["issuer_completion_cache_sha256"]
    completion_payload = _cached_payload(completion_cache)
    assert hashlib.sha256(completion_payload).hexdigest() == (
        evidence["issuer_completion_payload_sha256"]
    )
    completion_text = _filing_text(completion_payload)
    assert all(
        phrase in completion_text
        for phrase in evidence["issuer_completion_required_phrases"]
    )

    terminal = load_observed_terminal_returns().set_index(["ticker", "last_price_date"])
    formal = terminal.loc[("CUTR", pd.Timestamp(evidence["last_price_date"]))]
    assert float(formal["terminal_return"]) == -1.0
    assert float(formal["consideration_per_share"]) == 0.0
