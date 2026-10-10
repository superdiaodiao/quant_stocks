"""Import a FINRA-confirmed zero-equity terminal return with raw PDF evidence."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import pandas as pd
from pypdf import PdfReader

from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH
from src.io.terminal_returns import TERMINAL_RETURNS_FILE, load_observed_terminal_returns


DEFAULT_OUTPUT = Path(PROJECT_PATH) / (
    "output/data_provenance/finra_zero_equity_terminal_import.json"
)
DEFAULT_CACHE_DIR = Path(PROJECT_PATH) / (
    "output/data_provenance/finra_terminal_notice_cache"
)
PDF_HEADERS = {
    "User-Agent": "quant-stocks-research/1.0 contact=local-research@example.invalid"
}


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _atomic_write_csv(path: Path, frame: pd.DataFrame) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False)
    os.replace(temporary, path)


def _fetch_pdf(url: str, retries: int = 3) -> bytes:
    error: Exception | None = None
    for attempt in range(retries):
        try:
            return urlopen(Request(url, headers=PDF_HEADERS), timeout=45).read()
        except Exception as exc:  # pragma: no cover - network-dependent
            error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"FINRA PDF request failed: {error}")


def _load_or_fetch(
    cache_path: Path, source_url: str, refresh: bool
) -> tuple[bytes, Path]:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists() and not refresh:
        payload = gzip.decompress(cache_path.read_bytes())
    else:
        payload = _fetch_pdf(source_url)
        cache_path.write_bytes(gzip.compress(payload, mtime=0))
    if not payload.startswith(b"%PDF-"):
        raise ValueError("FINRA source did not return a PDF")
    return payload, cache_path


def _extract_pdf_text(payload: bytes) -> tuple[str, int]:
    reader = PdfReader(io.BytesIO(payload))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return re.sub(r"\s+", " ", text).strip(), len(reader.pages)


def _validate_zero_equity_notice(text: str, notice_ticker: str) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", text).strip()
    notice_ticker = notice_ticker.upper().strip()
    common_patterns = {
        "notice_ticker": rf"\b{re.escape(notice_ticker)}\b",
        "effective_date": (
            r"\bPlan(?:\s+of\s+[A-Za-z ]{1,80})?\s+has become effective\s+"
            r"on\s+(\d{1,2}/\d{1,2}/\d{4})"
        ),
        "worthless_delivery_notice": r"securities have been deemed worthless",
    }
    proof_families = {
        "specific_common_stock_zero_distribution": {
        "existing_equity_cancelled": (
            r"existing Interests in the Debtors will\s+be\s+canceled"
        ),
        "holders_receive_nothing": (
            r"Each\s+holder of an Equity Interest in a Debtor shall not receive\s+"
            r"anything on account of such\s+Interest"
        ),
        "common_stock_cancelled": (
            r"cancellation of all shares\s+of the common stock of .{1,500}?"
            r"outstanding immediately prior to the\s+Effective Date"
        ),
        "no_conversion_or_distribution": (
            r"without any conversion thereof or distribution with respect\s+thereto"
        ),
        },
        "all_equity_cancelled_no_value": {
            "all_equity_cancelled": (
                r"all Equity Interests shall be cancel(?:led|ed),\s+released\s+"
                r"and extinguished"
            ),
            "holders_receive_no_value": (
                r"each holder of an Existing Equity Interest shall not receive or "
                r"retain any Distribution, property, or other value on account of "
                r"its Equity Interest"
            ),
        },
    }
    matches: dict[str, str] = {}
    for name, pattern in common_patterns.items():
        match = re.search(pattern, text, re.I)
        if match is None:
            raise ValueError(f"FINRA notice is missing required proof: {name}")
        matches[name] = match.group(0)
    validation_scope = None
    family_errors: dict[str, list[str]] = {}
    for family, patterns in proof_families.items():
        family_matches: dict[str, str] = {}
        missing = []
        for name, pattern in patterns.items():
            match = re.search(pattern, text, re.I)
            if match is None:
                missing.append(name)
            else:
                family_matches[name] = match.group(0)
        if not missing:
            validation_scope = family
            matches.update(family_matches)
            break
        family_errors[family] = missing
    if validation_scope is None:
        raise ValueError(
            "FINRA notice does not prove zero equity under a strict proof family: "
            f"{family_errors}"
        )
    effective = re.search(common_patterns["effective_date"], text, re.I)
    assert effective is not None
    effective_date = pd.Timestamp(effective.group(1)).strftime("%Y-%m-%d")
    return {
        "passed": True,
        "validation_scope": validation_scope,
        "effective_date": effective_date,
        "required_matches": matches,
    }


def import_zero_equity_terminal(
    *,
    ticker: str,
    notice_ticker: str,
    source_url: str,
    verified_at: str,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    terminal_returns_path: str | Path = TERMINAL_RETURNS_FILE,
    cache_path: str | Path | None = None,
    output: str | Path = DEFAULT_OUTPUT,
    refresh: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    ticker = ticker.upper().strip()
    notice_ticker = notice_ticker.upper().strip()
    price_dir = Path(price_dir)
    terminal_returns_path = Path(terminal_returns_path)
    output = Path(output)
    if cache_path is None:
        cache_path = DEFAULT_CACHE_DIR / f"{notice_ticker.lower()}.pdf.gz"
    cache_path = Path(cache_path)

    pdf_payload, cache_path = _load_or_fetch(cache_path, source_url, refresh)
    pdf_text, page_count = _extract_pdf_text(pdf_payload)
    validation = _validate_zero_equity_notice(pdf_text, notice_ticker)

    price_path = price_dir / f"{ticker.lower()}.csv"
    prices = pd.read_csv(price_path)
    prices["date"] = pd.to_datetime(prices["date"], errors="raise")
    prices["close"] = pd.to_numeric(prices["close"], errors="raise")
    prices = prices.sort_values("date")
    last = prices.iloc[-1]
    last_price_date = pd.Timestamp(last["date"]).strftime("%Y-%m-%d")
    last_close = float(last["close"])
    if last_close <= 0:
        raise ValueError("Last local close must be positive")

    terminal_before = load_observed_terminal_returns(terminal_returns_path)
    existing = terminal_before.loc[
        terminal_before["ticker"].eq(ticker)
        & terminal_before["last_price_date"].eq(pd.Timestamp(last_price_date))
    ]
    if not existing.empty and not existing["terminal_return"].eq(-1.0).all():
        raise ValueError("Conflicting terminal return already exists")

    terminal_row = {
        "ticker": ticker,
        "last_price_date": last_price_date,
        "terminal_return": -1.0,
        "consideration_per_share": 0.0,
        "source_url": source_url,
        "verified_at": verified_at,
    }
    terminal_sha_before = _sha256(terminal_returns_path)
    report: dict[str, Any] = {
        "schema_version": 1,
        "research_only": True,
        "status": "DRY_RUN_ELIGIBLE",
        "applied": bool(apply),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_provider": "FINRA Uniform Practice Advisory",
        "source_url": source_url,
        "notice_ticker": notice_ticker,
        "raw_pdf_cache_path": str(cache_path),
        "raw_pdf_cache_sha256": _sha256(cache_path),
        "raw_pdf_sha256": _sha256_bytes(pdf_payload),
        "raw_pdf_size_bytes": len(pdf_payload),
        "raw_pdf_page_count": page_count,
        "extracted_text_sha256": _sha256_bytes(pdf_text.encode()),
        "validation": validation,
        "price_path": str(price_path),
        "price_sha256": _sha256(price_path),
        "last_close": last_close,
        "terminal_row": terminal_row,
        "terminal_returns_path": str(terminal_returns_path),
        "terminal_returns_sha256_before": terminal_sha_before,
        "formal_financial_files_modified": False,
    }
    if apply and existing.empty:
        persisted_row = {
            **terminal_row,
            "last_price_date": pd.Timestamp(last_price_date).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        }
        updated = pd.concat(
            [terminal_before, pd.DataFrame([persisted_row])], ignore_index=True
        )
        _atomic_write_csv(terminal_returns_path, updated)
        report["status"] = "UPDATED"
    elif apply:
        report["status"] = "ALREADY_PRESENT"
    report["terminal_returns_sha256_after"] = _sha256(terminal_returns_path)
    _atomic_write_json(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--notice-ticker", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--verified-at", required=True)
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--terminal-returns", default=str(TERMINAL_RETURNS_FILE))
    parser.add_argument("--cache-path")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = import_zero_equity_terminal(
        ticker=args.ticker,
        notice_ticker=args.notice_ticker,
        source_url=args.source_url,
        verified_at=args.verified_at,
        price_dir=args.price_dir,
        terminal_returns_path=args.terminal_returns,
        cache_path=args.cache_path,
        output=args.output,
        refresh=args.refresh,
        apply=args.apply,
    )
    print(json.dumps({
        "status": report["status"],
        "ticker": report["terminal_row"]["ticker"],
        "effective_date": report["validation"]["effective_date"],
    }, indent=2))


if __name__ == "__main__":
    main()
