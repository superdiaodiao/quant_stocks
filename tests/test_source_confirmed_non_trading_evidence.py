import hashlib
import json

from src.research.historical_data_audit import (
    load_source_confirmed_non_trading_evidence,
    partition_terminal_candidates,
)


def test_non_trading_evidence_is_bound_to_exact_price_bytes(tmp_path) -> None:
    price_dir = tmp_path / "price"
    price_dir.mkdir()
    price = price_dir / "test.csv"
    price.write_text("date,ticker,close\n2025-01-02,TEST,10\n", encoding="utf-8")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"records": [{
        "ticker": "TEST",
        "last_price_date": "2025-01-02",
        "last_membership_date": "2025-02-01",
        "resolution": "EXCHANGE_HALT_CONFIRMED",
        "price_file_sha256": hashlib.sha256(price.read_bytes()).hexdigest(),
        "event_filing_sha256": "a" * 64,
    }]}), encoding="utf-8")
    rows = load_source_confirmed_non_trading_evidence(
        evidence, price_dir=price_dir
    )
    assert rows[0]["ticker"] == "TEST"
    price.write_text("changed", encoding="utf-8")
    try:
        load_source_confirmed_non_trading_evidence(evidence, price_dir=price_dir)
    except ValueError as exc:
        assert "price SHA changed" in str(exc)
    else:
        raise AssertionError("stale evidence must be rejected")


def test_terminal_candidates_require_a_mature_observation_window() -> None:
    rows = [
        {"ticker": "OLD", "last_price_date": "2025-05-01"},
        {"ticker": "NEW", "last_price_date": "2025-07-20"},
    ]
    candidates, right_censored = partition_terminal_candidates(
        rows, analysis_end="2025-07-31", observation_lag_days=40
    )
    assert [row["ticker"] for row in candidates] == ["OLD"]
    assert [row["ticker"] for row in right_censored] == ["NEW"]
