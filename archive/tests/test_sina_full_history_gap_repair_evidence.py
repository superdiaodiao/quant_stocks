import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.sina_historical_price_repair import _decoder_source, _parse_prices
from src.conf import PROJECT_PATH


EVIDENCE = (
    Path(PROJECT_PATH)
    / "output/data_provenance/sina_full_history_gap_repair_applied_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_sina_full_history_gap_repair_replays_cached_rows() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    decoder, _ = _decoder_source()

    assert evidence["research_only"] is True
    assert evidence["applied"] is True
    assert evidence["status"] == "COMPLETE"
    assert len(evidence["records"]) == 9
    assert sum(record["rows_added"] for record in evidence["records"]) == 731

    for record in evidence["records"]:
        assert record["status"] == "UPDATED"
        validation = record["cross_validation"]
        assert validation["passed"] is True
        assert validation["validation_scope"] == "full_history"
        assert validation["sessions"] >= 20

        cache_path = Path(record["raw_cache_path"])
        raw = gzip.decompress(cache_path.read_bytes())
        assert hashlib.sha256(raw).hexdigest() == record["raw_payload_sha256"]
        source = _parse_prices(raw, record["ticker"], decoder).set_index("date")

        price_path = Path(record["price_path"])
        assert _sha256(price_path) == record["local_sha256_after"]
        prices = pd.read_csv(price_path, parse_dates=["date"]).set_index("date")
        assert source.index.difference(prices.index).empty
        for date_key in ["first_missing_date", "last_missing_date"]:
            date = pd.Timestamp(record[date_key])
            pd.testing.assert_series_equal(
                prices.loc[date, ["open", "high", "low", "close", "volume"]],
                source.loc[date, ["open", "high", "low", "close", "volume"]],
                check_dtype=False,
                check_names=False,
            )
