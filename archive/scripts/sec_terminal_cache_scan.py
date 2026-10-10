"""Scan cached SEC filings for reviewable fixed common-share consideration."""

from __future__ import annotations

import argparse
import gzip
import json
import re
from pathlib import Path

from scripts.historicaldata_price_import import _atomic_write_json
from scripts.sec_terminal_filing_evidence import _filing_text


FIXED_CASH_PATTERNS = [
    re.compile(
        r"(?:each|all) (?:outstanding )?shares? of (?:the )?(?:Company |[A-Za-z]+ )?"
        r"(?:Class A )?common stock.{0,700}?converted into the right to receive"
        r".{0,180}?an amount in cash equal to \$([0-9]+(?:\.[0-9]+)?) per share",
        re.I,
    ),
    re.compile(
        r"(?:each|all) (?:outstanding )?shares? of (?:the )?(?:Company |[A-Za-z]+ )?"
        r"(?:Class A )?common stock.{0,700}?converted into the right to receive"
        r".{0,180}?\$([0-9]+(?:\.[0-9]+)?) per share in cash",
        re.I,
    ),
    re.compile(
        r"each share of [A-Za-z ]+ common stock.{0,250}?converted into the right "
        r"to receive \$([0-9]+(?:\.[0-9]+)?) in cash",
        re.I,
    ),
]
EXCLUSIONS = re.compile(
    r"\b(CVR|contingent|divested asset|election|subject to proration)\b", re.I
)
ZERO_COMMON_EQUITY_PATTERNS = [
    re.compile(
        r"holders? of (?:the )?(?:existing )?(?:common stock|equity interests?)"
        r".{0,500}?(?:will|shall|did|would) receive no (?:distribution|recovery)",
        re.I,
    ),
    re.compile(
        r"all (?:issued and outstanding )?shares? of [A-Za-z ]*common stock"
        r".{0,500}?(?:cancelled|canceled|extinguished).{0,200}?"
        r"(?:without (?:any )?consideration|no (?:distribution|recovery))",
        re.I,
    ),
    re.compile(
        r"(?:existing equity interests?|common stock).{0,500}?"
        r"(?:cancelled|canceled|extinguished).{0,300}?"
        r"(?:no distribution|no recovery)",
        re.I,
    ),
]
BANKRUPTCY_TERMS = re.compile(r"\b(?:chapter 11|reorganization plan|plan of reorganization)\b", re.I)
PLAN_EFFECTIVE_TERMS = re.compile(r"\b(?:plan became effective|effective date of the plan|plan effective date)\b", re.I)
EQUITY_CANCELLATION_TERMS = re.compile(
    r"\b(?:common stock|equity interests?).{0,500}?"
    r"(?:cancelled|canceled|extinguished|no recovery|no distribution)",
    re.I,
)


def scan(cache_dir: str | Path, audit_path: str | Path) -> list[dict]:
    audit = json.loads(Path(audit_path).read_text(encoding="utf-8"))
    unresolved = {
        row["ticker"] for row in audit["unresolved_terminal_return_histories"]
    }
    records = []
    for path in sorted(Path(cache_dir).glob("*.json.gz")):
        ticker = path.name.split("_", 1)[0].upper()
        if ticker not in unresolved:
            continue
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            envelope = json.load(handle)
        payload = bytes.fromhex(envelope["payload_hex"])
        text = _filing_text(payload)
        matched = False
        for pattern in FIXED_CASH_PATTERNS:
            match = pattern.search(text)
            if match is None:
                continue
            context = text[match.start():min(len(text), match.end() + 350)]
            records.append({
                "ticker": ticker,
                "amount": float(match.group(1)),
                "status": "REVIEW_EXCLUDED_COMPLEX_TERM" if EXCLUSIONS.search(context) else "REVIEW_FIXED_COMMON_SHARE_CASH",
                "context": context,
                "cache_path": str(path),
                "source_url": envelope["source_url"],
                "payload_sha256": envelope["payload_sha256"],
            })
            matched = True
            break
        if matched:
            continue
        for pattern in ZERO_COMMON_EQUITY_PATTERNS:
            match = pattern.search(text)
            if match is None:
                continue
            records.append({
                "ticker": ticker,
                "amount": 0.0,
                "status": "REVIEW_ZERO_COMMON_EQUITY_DISTRIBUTION",
                "context": text[max(0, match.start() - 250):min(len(text), match.end() + 350)],
                "cache_path": str(path),
                "source_url": envelope["source_url"],
                "payload_sha256": envelope["payload_sha256"],
            })
            matched = True
            break
        if matched:
            continue
        bankruptcy = BANKRUPTCY_TERMS.search(text)
        effective = PLAN_EFFECTIVE_TERMS.search(text)
        cancellation = EQUITY_CANCELLATION_TERMS.search(text)
        if bankruptcy and effective and cancellation:
            records.append({
                "ticker": ticker,
                "amount": 0.0,
                "status": "REVIEW_BANKRUPTCY_PLAN_AND_EQUITY_CANCELLATION",
                "context": text[
                    max(0, cancellation.start() - 500):
                    min(len(text), cancellation.end() + 700)
                ],
                "bankruptcy_term": bankruptcy.group(0),
                "plan_effective_term": effective.group(0),
                "cache_path": str(path),
                "source_url": envelope["source_url"],
                "payload_sha256": envelope["payload_sha256"],
            })
    unique = {}
    for record in records:
        unique[(record["ticker"], record["amount"], record["payload_sha256"])] = record
    return list(unique.values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--historical-audit", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    records = scan(args.cache_dir, args.historical_audit)
    _atomic_write_json(Path(args.output), {
        "format_version": 1,
        "research_only": True,
        "records": records,
        "review_fixed_common_share_cash": sum(
            row["status"] == "REVIEW_FIXED_COMMON_SHARE_CASH" for row in records
        ),
        "review_zero_common_equity_distribution": sum(
            row["status"] == "REVIEW_ZERO_COMMON_EQUITY_DISTRIBUTION"
            for row in records
        ),
        "review_bankruptcy_plan_and_equity_cancellation": sum(
            row["status"] == "REVIEW_BANKRUPTCY_PLAN_AND_EQUITY_CANCELLATION"
            for row in records
        ),
    })


if __name__ == "__main__":
    main()
