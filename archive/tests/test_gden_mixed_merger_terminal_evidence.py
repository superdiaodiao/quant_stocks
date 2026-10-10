import gzip
import hashlib
import json
import re
import zipfile
from pathlib import Path

import pandas as pd
import pytest
from bs4 import BeautifulSoup

from scripts.historicaldata_price_import import _member_sha256, _read_stooq_member
from src.conf import PROJECT_PATH


EVIDENCE_PATH = (
    Path(PROJECT_PATH)
    / "output/data_provenance/sec_mixed_merger_terminal_evidence_gden_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_gden_terminal_value_replays_fixed_cash_and_vici_stock_components():
    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    sec = evidence["sec_evidence"]
    original = evidence["original_price_evidence"]
    successor = evidence["successor_price_evidence"]

    cache_path = Path(PROJECT_PATH) / sec["cache_path"]
    assert _sha256(cache_path) == sec["cache_sha256"]
    envelope = json.load(gzip.open(cache_path, "rt", encoding="utf-8"))
    payload = bytes.fromhex(envelope["payload_hex"])
    assert hashlib.sha256(payload).hexdigest() == sec["payload_sha256"]
    text = re.sub(
        r"\s+", " ", BeautifulSoup(payload, "html.parser").get_text(" ")
    )
    assert all(phrase in text for phrase in sec["required_phrases"])

    original_path = Path(PROJECT_PATH) / original["path"]
    assert _sha256(original_path) == original["sha256"]
    prices = pd.read_csv(original_path, parse_dates=["date"])
    last = prices.sort_values("date").iloc[-1]
    assert last["date"].strftime("%Y-%m-%d") == original["last_price_date"]
    assert last["close"] == pytest.approx(original["last_close"])

    archive_path = Path(successor["archive_path"])
    assert _sha256(archive_path) == successor["archive_sha256"]
    with zipfile.ZipFile(archive_path) as archive:
        member = successor["archive_member"]
        info = archive.getinfo(member)
        assert _member_sha256(archive, member) == successor["member_sha256"]
        assert f"{info.CRC:08x}" == successor["member_crc32"]
        assert info.file_size == successor["member_size_bytes"]
        vici = _read_stooq_member(archive, member, evidence["successor_ticker"])
    close = float(
        vici.loc[
            vici["date"].eq(pd.Timestamp(successor["price_date"])), "close"
        ].iloc[0]
    )
    assert close == pytest.approx(successor["close"])

    value = evidence["cash_distribution_per_share"] + evidence["exchange_ratio"] * close
    terminal_return = value / float(last["close"]) - 1.0
    assert value == pytest.approx(evidence["terminal_value"])
    assert terminal_return == pytest.approx(evidence["terminal_return"])

    terminal = pd.read_csv(
        Path(PROJECT_PATH) / "stocks_list_dir/nasdaq/terminal_returns.csv"
    )
    row = terminal.loc[
        terminal["ticker"].eq("GDEN")
        & pd.to_datetime(terminal["last_price_date"]).eq(
            pd.Timestamp(evidence["last_price_date"])
        )
    ].iloc[0]
    assert row["consideration_per_share"] == pytest.approx(value)
    assert row["terminal_return"] == pytest.approx(terminal_return)
    assert evidence["formal_financial_files_modified"] is False
