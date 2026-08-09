"""Import a strictly proven fixed-cash SEC merger terminal return."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.sec_terminal_filing_evidence import _filing_text
from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH
from src.io.terminal_returns import TERMINAL_RETURNS_FILE, load_observed_terminal_returns


DEFAULT_OUTPUT = Path(PROJECT_PATH) / (
    "output/data_provenance/sec_fixed_cash_terminal_import.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_fixed_cash_completion(text: str, amount: float) -> dict[str, Any]:
    amount_matches = list(re.finditer(
        r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s+per\s+Share"
        r"[^.]{0,120}\b(?:cash|Offer Price)\b",
        text,
        re.I,
    ))
    amount_match = next((
        match for match in amount_matches
        if abs(float(match.group(1).replace(",", "")) - amount) <= 1e-9
    ), None)
    completion_match = re.search(
        r"effective time of the Merger.{0,900}?each Share.{0,700}?"
        r"(?:canceled|cancelled).{0,160}?converted into the right to receive "
        r"(?:the )?(?:Offer Price|Merger Consideration)",
        text,
        re.I,
    )
    merger_match = re.search(
        r"(?:merged with and into|consummation of the Merger|completion of the Merger)",
        text,
        re.I,
    )
    contingent_match = re.search(r"\bCVR\b|contingent value right", text, re.I)
    passed = bool(
        amount_match and completion_match and merger_match and not contingent_match
    )
    return {
        "passed": passed,
        "fixed_cash_amount_match": bool(amount_match),
        "completed_common_share_conversion_match": bool(completion_match),
        "merger_completion_match": bool(merger_match),
        "contingent_value_right_absent": not bool(contingent_match),
        "validation_scope": "sec_completed_fixed_cash_no_cvr",
    }


def import_fixed_cash_terminal(
    *,
    ticker: str,
    consideration_per_share: float,
    evidence_report: str | Path,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    terminal_returns_path: str | Path = TERMINAL_RETURNS_FILE,
    output: str | Path = DEFAULT_OUTPUT,
    verified_at: str,
    apply: bool = False,
) -> dict[str, Any]:
    ticker = ticker.upper().strip()
    evidence_path = Path(evidence_report).resolve()
    price_dir = Path(price_dir)
    terminal_path = Path(terminal_returns_path)
    output = Path(output)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    matches = [row for row in evidence.get("records", []) if row.get("ticker") == ticker]
    if len(matches) != 1:
        raise ValueError("evidence report must contain exactly one ticker record")
    record = matches[0]
    cache_path = Path(record["cache_path"])
    envelope = json.loads(gzip.decompress(cache_path.read_bytes()))
    raw = bytes.fromhex(envelope["payload_hex"])
    raw_sha = hashlib.sha256(raw).hexdigest()
    if raw_sha != record["payload_sha256"] or raw_sha != envelope["payload_sha256"]:
        raise ValueError("SEC filing payload SHA mismatch")
    if envelope["source_url"] != record["filing"]["source_url"]:
        raise ValueError("SEC filing URL mismatch")
    text = _filing_text(raw)
    validation = _validate_fixed_cash_completion(text, consideration_per_share)
    if not validation["passed"]:
        raise ValueError(f"fixed cash completion validation failed: {validation}")

    price_path = price_dir / f"{ticker.lower()}.csv"
    prices = pd.read_csv(price_path)
    prices["date"] = pd.to_datetime(prices["date"], errors="raise").dt.normalize()
    last = prices.sort_values("date").iloc[-1]
    last_date = last["date"].strftime("%Y-%m-%d")
    last_close = float(last["close"])
    if last_date != record["last_local_price_date"]:
        raise ValueError("evidence report is stale for the local price history")
    if abs(last_close - float(record["last_local_close"])) > 1e-9:
        raise ValueError("evidence report last close does not match local history")

    existing = load_observed_terminal_returns(terminal_path)
    key_exists = bool((
        existing["ticker"].eq(ticker)
        & existing["last_price_date"].eq(pd.Timestamp(last_date))
    ).any())
    if key_exists:
        raise ValueError("terminal return already exists for ticker/date")
    terminal_return = float(consideration_per_share / last_close - 1.0)
    new_row = {
        "ticker": ticker,
        "last_price_date": last_date,
        "terminal_return": terminal_return,
        "consideration_per_share": float(consideration_per_share),
        "source_url": record["filing"]["source_url"],
        "verified_at": pd.Timestamp(verified_at).isoformat().replace("+00:00", "Z"),
    }
    before_sha = _sha256(terminal_path)
    if apply:
        persisted = pd.read_csv(terminal_path)
        persisted = pd.concat([persisted, pd.DataFrame([new_row])], ignore_index=True)
        temporary = terminal_path.with_suffix(terminal_path.suffix + ".tmp")
        persisted.to_csv(temporary, index=False)
        os.replace(temporary, terminal_path)
        load_observed_terminal_returns(terminal_path)

    report = {
        "format_version": 1,
        "research_only": False,
        "formal_financial_files_modified": False,
        "terminal_returns_modified": bool(apply),
        "status": "UPDATED" if apply else "DRY_RUN_ELIGIBLE",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "evidence_report": str(evidence_path),
        "evidence_report_sha256": _sha256(evidence_path),
        "cache_path": str(cache_path.resolve()),
        "cache_sha256": _sha256(cache_path),
        "payload_sha256": raw_sha,
        "source_url": record["filing"]["source_url"],
        "validation": validation,
        "terminal_row": new_row,
        "terminal_returns_path": str(terminal_path.resolve()),
        "terminal_returns_sha256_before": before_sha,
        "terminal_returns_sha256_after": _sha256(terminal_path),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--consideration-per-share", type=float, required=True)
    parser.add_argument("--evidence-report", required=True)
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--terminal-returns", default=str(TERMINAL_RETURNS_FILE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--verified-at", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = import_fixed_cash_terminal(
        ticker=args.ticker,
        consideration_per_share=args.consideration_per_share,
        evidence_report=args.evidence_report,
        price_dir=args.price_dir,
        terminal_returns_path=args.terminal_returns,
        output=args.output,
        verified_at=args.verified_at,
        apply=args.apply,
    )
    print(json.dumps({
        "status": report["status"],
        "ticker": report["ticker"],
        "terminal_return": report["terminal_row"]["terminal_return"],
    }, indent=2))


if __name__ == "__main__":
    main()
