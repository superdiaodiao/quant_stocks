import gzip
import hashlib
import json

import pandas as pd
import pytest

from scripts.sec_fixed_cash_terminal_import import import_fixed_cash_terminal


def _inputs(tmp_path, *, with_cvr=False):
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    pd.DataFrame([{
        "date": "2026-06-15", "ticker": "TEST", "open": 23.4,
        "high": 23.5, "low": 23.4, "close": 23.5, "volume": 100,
    }]).to_csv(price_dir / "test.csv", index=False)
    terminal = tmp_path / "terminal.csv"
    pd.DataFrame(columns=[
        "ticker", "last_price_date", "terminal_return",
        "consideration_per_share", "source_url", "verified_at",
    ]).to_csv(terminal, index=False)
    cvr = " No CVR is issued." if with_cvr else ""
    html = (
        "<html><body>Purchaser merged with and into the Company. "
        "At the effective time of the Merger, each Share was automatically "
        "canceled and converted into the right to receive the Offer Price. "
        "The Offer Price was $23.50 per Share, payable in cash, without interest."
        f"{cvr}</body></html>"
    ).encode()
    raw_sha = hashlib.sha256(html).hexdigest()
    cache = tmp_path / "cache.json.gz"
    cache.write_bytes(gzip.compress(json.dumps({
        "payload_hex": html.hex(), "payload_sha256": raw_sha,
        "source_url": "https://www.sec.gov/test",
    }).encode(), mtime=0))
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"records": [{
        "ticker": "TEST", "cache_path": str(cache), "payload_sha256": raw_sha,
        "last_local_price_date": "2026-06-15", "last_local_close": 23.5,
        "filing": {"source_url": "https://www.sec.gov/test"},
    }]}))
    return price_dir, terminal, evidence


def test_fixed_cash_terminal_import_writes_exact_calculated_return(tmp_path):
    price_dir, terminal, evidence = _inputs(tmp_path)
    report = import_fixed_cash_terminal(
        ticker="TEST", consideration_per_share=23.5,
        evidence_report=evidence, price_dir=price_dir,
        terminal_returns_path=terminal, output=tmp_path / "report.json",
        verified_at="2026-08-09T00:00:00Z", apply=True,
    )
    assert report["status"] == "UPDATED"
    assert report["terminal_row"]["terminal_return"] == 0.0
    assert pd.read_csv(terminal).iloc[0]["consideration_per_share"] == 23.5


def test_fixed_cash_terminal_import_rejects_cvr_consideration(tmp_path):
    price_dir, terminal, evidence = _inputs(tmp_path, with_cvr=True)
    with pytest.raises(ValueError, match="fixed cash completion validation failed"):
        import_fixed_cash_terminal(
            ticker="TEST", consideration_per_share=23.5,
            evidence_report=evidence, price_dir=price_dir,
            terminal_returns_path=terminal, output=tmp_path / "report.json",
            verified_at="2026-08-09T00:00:00Z", apply=False,
        )
