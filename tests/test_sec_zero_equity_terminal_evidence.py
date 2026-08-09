import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.sec_terminal_filing_evidence import _filing_text
from src.conf import PROJECT_PATH
from src.io.terminal_returns import load_observed_terminal_returns


EVIDENCE = Path(PROJECT_PATH) / "output/data_provenance/sec_zero_equity_terminal_evidence_2026-08-09.json"


def test_zero_equity_evidence_requires_effective_plan_and_no_consideration() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    terminal = load_observed_terminal_returns().set_index(["ticker", "last_price_date"])
    assert evidence["research_only"] is True
    for record in evidence["records"]:
        with gzip.open(Path(PROJECT_PATH) / record["sec_cache_path"], "rt", encoding="utf-8") as handle:
            envelope = json.load(handle)
        payload = bytes.fromhex(envelope["payload_hex"])
        assert hashlib.sha256(payload).hexdigest() == record["sec_filing_payload_sha256"]
        text = _filing_text(payload)
        assert all(phrase in text for phrase in record["required_phrases"])
        if "plan_cache_path" in record:
            with gzip.open(
                Path(PROJECT_PATH) / record["plan_cache_path"],
                "rt",
                encoding="utf-8",
            ) as handle:
                plan_envelope = json.load(handle)
            plan_payload = bytes.fromhex(plan_envelope["payload_hex"])
            assert hashlib.sha256(plan_payload).hexdigest() == (
                record["plan_payload_sha256"]
            )
            plan_text = _filing_text(plan_payload)
            assert all(
                phrase in plan_text for phrase in record["plan_required_phrases"]
            )
        if "price_path" in record:
            price_path = Path(PROJECT_PATH) / record["price_path"]
            assert hashlib.sha256(price_path.read_bytes()).hexdigest() == (
                record["price_sha256"]
            )
            prices = pd.read_csv(price_path, parse_dates=["date"]).sort_values("date")
            last = prices.iloc[-1]
            assert last["date"].strftime("%Y-%m-%d") == record["last_price_date"]
            assert float(last["close"]) == record["last_close"]
        assert record["terminal_return"] == -1.0
        formal = terminal.loc[(record["ticker"], record["last_price_date"])]
        assert float(formal["terminal_return"]) == -1.0
