import gzip
import hashlib
import io
import json
from pathlib import Path

import pandas as pd
import pytest
from pypdf import PdfReader

from scripts.finra_zero_equity_terminal_import import (
    _validate_zero_equity_notice,
    import_zero_equity_terminal,
)


NOTICE_TEXT = """
UNIFORM PRACTICE ADVISORY (UPC # 125-25) Nikola Corp. (NKLAQ)
Notice has been received that the above Company's Modified Combined Disclosure
Statement and Chapter 11 Plan has become effective on 12/12/2025. Pursuant to
the plan, On the Effective Date, the existing Interests in the Debtors will be
canceled. Each holder of an Equity Interest in a Debtor shall not receive
anything on account of such Interest. The entry of the Confirmation Order shall
act as an order approving and effecting the cancellation of all shares of the
common stock of Nikola Corp. outstanding immediately prior to the Effective Date
without any conversion thereof or distribution with respect thereto.
FINRA Rule 11530 applies where securities have been deemed worthless.
"""


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    prices = tmp_path / "prices"
    prices.mkdir()
    pd.DataFrame({
        "date": ["2025-02-24", "2025-02-25"],
        "ticker": ["NKLA", "NKLA"],
        "open": [0.8, 0.7],
        "high": [0.9, 0.8],
        "low": [0.7, 0.6],
        "close": [0.75, 0.65],
        "volume": [100, 200],
    }).to_csv(prices / "nkla.csv", index=False)
    terminal = tmp_path / "terminal_returns.csv"
    pd.DataFrame(columns=[
        "ticker", "last_price_date", "terminal_return",
        "consideration_per_share", "source_url", "verified_at",
    ]).to_csv(terminal, index=False)
    return prices, terminal


def test_zero_equity_notice_requires_all_strict_proofs() -> None:
    result = _validate_zero_equity_notice(NOTICE_TEXT, "NKLAQ")
    assert result["effective_date"] == "2025-12-12"
    assert result["validation_scope"] == "specific_common_stock_zero_distribution"
    with pytest.raises(ValueError, match="strict proof family"):
        _validate_zero_equity_notice(
            NOTICE_TEXT.replace("shall not receive", "may receive"), "NKLAQ"
        )


def test_zero_equity_notice_accepts_all_equity_no_value_variant() -> None:
    text = """
    UNIFORM PRACTICE ADVISORY Fat Brands Inc (FABTQ, FATPQ FATAQ)
    The Company's Joint Plan of Liquidation has become effective on 07/31/2026.
    On the Effective Date, all Equity Interests shall be cancelled, released and
    extinguished, and each holder of an Existing Equity Interest shall not receive
    or retain any Distribution, property, or other value on account of its Equity
    Interest. FINRA Rule 11530 applies where securities have been deemed worthless.
    """
    result = _validate_zero_equity_notice(text, "FATAQ")
    assert result["effective_date"] == "2026-07-31"
    assert result["validation_scope"] == "all_equity_cancelled_no_value"


def test_zero_equity_import_writes_exact_terminal_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prices, terminal = _inputs(tmp_path)
    monkeypatch.setattr(
        "scripts.finra_zero_equity_terminal_import._load_or_fetch",
        lambda cache_path, source_url, refresh: (b"%PDF-test", Path(cache_path)),
    )
    monkeypatch.setattr(
        "scripts.finra_zero_equity_terminal_import._extract_pdf_text",
        lambda payload: (" ".join(NOTICE_TEXT.split()), 1),
    )
    cache = tmp_path / "notice.pdf.gz"
    cache.write_bytes(gzip.compress(b"%PDF-test", mtime=0))
    report = import_zero_equity_terminal(
        ticker="NKLA",
        notice_ticker="NKLAQ",
        source_url="https://www.finra.org/notice.pdf",
        verified_at="2026-08-09T00:00:00Z",
        price_dir=prices,
        terminal_returns_path=terminal,
        cache_path=cache,
        output=tmp_path / "report.json",
        apply=True,
    )
    assert report["status"] == "UPDATED"
    assert report["terminal_row"]["terminal_return"] == -1.0
    written = pd.read_csv(terminal)
    assert written.loc[0, "ticker"] == "NKLA"
    assert written.loc[0, "last_price_date"] == "2025-02-25 00:00:00"
    assert written.loc[0, "terminal_return"] == -1.0


def test_nkla_finra_evidence_replays_offline_when_present() -> None:
    report_path = Path(
        "output/data_provenance/finra_zero_equity_terminal_nkla_apply_2026-08-09.json"
    )
    if not report_path.exists():
        pytest.skip("formal NKLA evidence has not been applied")
    report = json.loads(report_path.read_text())
    cache_path = Path(report["raw_pdf_cache_path"])
    compressed = cache_path.read_bytes()
    payload = gzip.decompress(compressed)
    assert hashlib.sha256(compressed).hexdigest() == report["raw_pdf_cache_sha256"]
    assert hashlib.sha256(payload).hexdigest() == report["raw_pdf_sha256"]
    text = " ".join(
        "\n".join(
            page.extract_text() or "" for page in PdfReader(io.BytesIO(payload)).pages
        ).split()
    )
    validation = _validate_zero_equity_notice(text, "NKLAQ")
    assert validation["effective_date"] == "2025-12-12"
    assert report["terminal_row"]["terminal_return"] == -1.0
