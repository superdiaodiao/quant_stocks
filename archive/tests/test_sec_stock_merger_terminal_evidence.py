import hashlib
import gzip
import json
from pathlib import Path

import pandas as pd

from scripts.sina_historical_price_repair import _decoder_source, _parse_prices
from scripts.sec_terminal_filing_evidence import _filing_text
from src.conf import PROJECT_PATH
from src.io.terminal_returns import load_observed_terminal_returns


EVIDENCE = Path(PROJECT_PATH) / "output/data_provenance/sec_stock_merger_terminal_evidence_2026-08-08.json"
SEC_CACHE = Path(PROJECT_PATH) / "output/data_provenance/sec_terminal_filing_cache"


def test_sec_stock_merger_terminal_evidence_replays_exact_formula_and_prices():
    payload = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    terminal = load_observed_terminal_returns().set_index(
        ["ticker", "last_price_date"]
    )
    assert payload["research_only"] is True
    assert len(payload["records"]) == 56

    for record in payload["records"]:
        if "sec_cache_path" in record:
            with gzip.open(
                Path(PROJECT_PATH) / record["sec_cache_path"], "rt",
                encoding="utf-8",
            ) as handle:
                filing_envelope = json.load(handle)
            filing_payload = bytes.fromhex(filing_envelope["payload_hex"])
            assert hashlib.sha256(filing_payload).hexdigest() == (
                record["sec_filing_payload_sha256"]
            )
            if "exchange_ratio_required_phrase" in record:
                filing_text = _filing_text(filing_payload)
                assert record["exchange_ratio_required_phrase"] in filing_text
        if "completion_sec_cache_path" in record:
            with gzip.open(
                Path(PROJECT_PATH) / record["completion_sec_cache_path"], "rt",
                encoding="utf-8",
            ) as handle:
                completion_envelope = json.load(handle)
            completion_payload = bytes.fromhex(completion_envelope["payload_hex"])
            assert hashlib.sha256(completion_payload).hexdigest() == (
                record["completion_sec_filing_payload_sha256"]
            )
            completion_text = _filing_text(completion_payload)
            assert record["completion_required_phrase"] in completion_text
        if "legal_exchange_ratio" in record:
            assert record["exchange_ratio"] == (
                record["legal_exchange_ratio"]
                / record["successor_reverse_split_ratio"]
            )
            if "successor_split_evidence_path" in record:
                split_evidence_path = (
                    Path(PROJECT_PATH) / record["successor_split_evidence_path"]
                )
                assert hashlib.sha256(split_evidence_path.read_bytes()).hexdigest() == (
                    record["successor_split_evidence_sha256"]
                )
                split_evidence = json.loads(
                    split_evidence_path.read_text(encoding="utf-8")
                )
                ratio = 1
                for split_record in split_evidence["records"]:
                    ratio *= split_record["reverse_split_ratio"]
                assert ratio == record["successor_reverse_split_ratio"]
            else:
                assert len(record["successor_split_filing_payload_sha256"]) == 64
                split_cache = SEC_CACHE / "asst_0000950103-26-001560.json.gz"
                with gzip.open(split_cache, "rt", encoding="utf-8") as handle:
                    envelope = json.load(handle)
                split_payload = bytes.fromhex(envelope["payload_hex"])
                assert hashlib.sha256(split_payload).hexdigest() == (
                    record["successor_split_filing_payload_sha256"]
                )
                split_text = split_payload.decode("utf-8", errors="ignore")
                assert "1-for-twenty reverse stock split" in split_text
                assert "February 6, 2026" in split_text
        if "cross_market_evidence_path" in record:
            cross_market_path = (
                Path(PROJECT_PATH) / record["cross_market_evidence_path"]
            )
            assert hashlib.sha256(cross_market_path.read_bytes()).hexdigest() == (
                record["cross_market_evidence_sha256"]
            )
            cross_market = json.loads(cross_market_path.read_text(encoding="utf-8"))
            assert cross_market["successor_close_usd"] == record["successor_close"]
            assert cross_market["terminal_return"] == record["terminal_return"]
            assert {source["role"] for source in cross_market["sources"]} == {
                "successor_close",
                "ils_per_eur",
                "usd_per_eur",
            }
        elif "successor_price_path" in record:
            price_path = Path(PROJECT_PATH) / record["successor_price_path"]
            assert hashlib.sha256(price_path.read_bytes()).hexdigest() == (
                record["successor_price_sha256"]
            )
            prices = pd.read_csv(price_path, parse_dates=["date"])
            price = prices.loc[
                prices["date"].eq(pd.Timestamp(record["successor_price_date"])),
                "close",
            ]
            assert len(price) == 1
            assert abs(float(price.iloc[0]) - record["successor_close"]) < 1e-12
        elif record.get("successor_price_source") == "sina_raw_cache_sec_identity_bound":
            cache_path = Path(PROJECT_PATH) / record["sina_cache_path"]
            assert hashlib.sha256(cache_path.read_bytes()).hexdigest() == (
                record["sina_cache_sha256"]
            )
            raw = gzip.decompress(cache_path.read_bytes())
            assert hashlib.sha256(raw).hexdigest() == record["sina_payload_sha256"]
            decoder, _ = _decoder_source()
            prices = _parse_prices(raw, record["successor_ticker"], decoder)
            price = prices.loc[
                prices["date"].eq(pd.Timestamp(record["successor_price_date"])),
                "close",
            ]
            assert len(price) == 1
            assert float(price.iloc[0]) == record["successor_close"]
            probe_path = Path(PROJECT_PATH) / record["successor_sec_probe_path"]
            assert hashlib.sha256(probe_path.read_bytes()).hexdigest() == (
                record["successor_sec_probe_sha256"]
            )
            probe = json.loads(probe_path.read_text())
            assert {
                issuer["cik"]
                for row in probe["results"]
                for issuer in row["issuers"]
            } == {record["successor_cik"]}
        else:
            assert record["successor_price_source"] == (
                "stooq_official_us_daily_archive"
            )
            assert len(record["stooq_archive_sha256"]) == 64
            assert len(record["stooq_member_sha256"]) == 64
            if "alias_import_evidence_path" in record:
                alias_path = Path(PROJECT_PATH) / record["alias_import_evidence_path"]
                assert hashlib.sha256(alias_path.read_bytes()).hexdigest() == (
                    record["alias_import_evidence_sha256"]
                )
                historical_path = (
                    Path(PROJECT_PATH) / "cleaned_stocks_data/price"
                    / f"{record['ticker'].lower()}.csv"
                )
                assert hashlib.sha256(historical_path.read_bytes()).hexdigest() == (
                    record["historical_price_sha256"]
                )
        consideration = (
            record["cash_component"]
            + record["exchange_ratio"] * record["successor_close"]
        )
        if "removed_invalid_tail_dates" in record:
            historical_path = Path(PROJECT_PATH) / record["historical_price_path"]
            assert hashlib.sha256(historical_path.read_bytes()).hexdigest() == (
                record["historical_price_sha256"]
            )
            historical = pd.read_csv(historical_path, parse_dates=["date"])
            assert historical["date"].max() == pd.Timestamp(record["last_price_date"])
            assert not historical["date"].isin(
                pd.to_datetime(record["removed_invalid_tail_dates"])
            ).any()
        if "stock_allocation_fraction" in record:
            assert record["stock_allocation_fraction"] + record["cash_allocation_fraction"] == 1.0
            assert abs(
                record["exchange_ratio"]
                - record["stock_allocation_fraction"]
                * record["per_stock_share_exchange_ratio"]
            ) < 1e-12
            filing_cache = SEC_CACHE / {
                "HONE": "hone_0001104659-25-105934.json.gz",
                "OPOF": "opof_0001104659-25-086430.json.gz",
                "PVBC": "pvbc_0001104659-25-112963.json.gz",
                "PBBK": "pbbk_0001104659-25-105479.json.gz",
            }[record["ticker"]]
            with gzip.open(filing_cache, "rt", encoding="utf-8") as handle:
                envelope = json.load(handle)
            filing_payload = bytes.fromhex(envelope["payload_hex"])
            assert hashlib.sha256(filing_payload).hexdigest() == (
                record["sec_filing_payload_sha256"]
            )
            filing_text = filing_payload.decode("utf-8", errors="ignore")
            expected = {
                "HONE": ("84.99%", "15.01%"),
                "OPOF": ("60%", "40%"),
                "PVBC": ("50%", "$13.00"),
                "PBBK": ("80%", "20%"),
            }[record["ticker"]]
            assert all(value in filing_text for value in expected)
        assert abs(consideration - record["total_consideration"]) < 1e-12
        expected_return = consideration / record["last_close"] - 1.0
        assert abs(expected_return - record["terminal_return"]) < 1e-12
        formal = terminal.loc[
            (record["ticker"], pd.Timestamp(record["last_price_date"]))
        ]
        assert abs(float(formal["terminal_return"]) - expected_return) < 1e-12
