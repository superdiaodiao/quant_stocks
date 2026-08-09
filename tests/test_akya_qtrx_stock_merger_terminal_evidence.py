import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.sec_terminal_filing_evidence import _filing_text
from src.conf import PROJECT_PATH
from src.io.terminal_returns import load_observed_terminal_returns


EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "sec_stock_merger_akya_qtrx_terminal_evidence_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _replay_filing(section: dict) -> str:
    cache_path = Path(PROJECT_PATH) / section["cache_path"]
    assert _sha256(cache_path) == section["cache_sha256"]
    envelope = json.loads(gzip.decompress(cache_path.read_bytes()))
    raw = bytes.fromhex(envelope["payload_hex"])
    assert hashlib.sha256(raw).hexdigest() == section["payload_sha256"]
    assert envelope["source_url"] == section["source_url"]
    return _filing_text(raw)


def test_akya_terminal_return_replays_final_adjusted_merger_consideration() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    completion_text = _replay_filing(evidence["completion_filing"])
    consideration_text = _replay_filing(evidence["final_consideration_filing"])
    for phrase in evidence["completion_filing"]["required_phrases"]:
        assert phrase in completion_text
    for phrase in evidence["final_consideration_filing"]["required_phrases"]:
        assert phrase in consideration_text

    for key in ["historical", "successor"]:
        price_path = Path(PROJECT_PATH) / evidence["price_evidence"][f"{key}_price_path"]
        assert _sha256(price_path) == evidence["price_evidence"][f"{key}_price_sha256"]
    historical = pd.read_csv(
        Path(PROJECT_PATH) / evidence["price_evidence"]["historical_price_path"]
    )
    successor = pd.read_csv(
        Path(PROJECT_PATH) / evidence["price_evidence"]["successor_price_path"]
    )
    last_close = float(
        historical.loc[historical["date"].eq(evidence["last_price_date"]), "close"].iloc[0]
    )
    successor_close = float(
        successor.loc[
            successor["date"].eq(evidence["successor_price_date"]), "close"
        ].iloc[0]
    )
    consideration = (
        evidence["final_cash_per_share"]
        + evidence["final_stock_exchange_ratio"] * successor_close
    )
    terminal_return = consideration / last_close - 1.0
    assert consideration == pytest.approx(evidence["consideration_per_share"])
    assert terminal_return == pytest.approx(evidence["terminal_return"])

    rows = load_observed_terminal_returns()
    recorded = rows.loc[
        rows["ticker"].eq("AKYA")
        & rows["last_price_date"].eq(pd.Timestamp(evidence["last_price_date"]))
    ]
    assert len(recorded) == 1
    assert float(recorded.iloc[0]["terminal_return"]) == pytest.approx(terminal_return)
    assert float(recorded.iloc[0]["consideration_per_share"]) == pytest.approx(
        consideration
    )
