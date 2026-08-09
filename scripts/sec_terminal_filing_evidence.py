"""Cache SEC terminal-event filings and extract reviewable cash-price leads.

The output is evidence triage, not a formal terminal-return update. Exact SEC
filing bytes are cached in gzip envelopes and SHA-verified on offline replay.
Candidate per-share amounts retain their surrounding filing text so a reviewer
can reject option, award, CVR, stock-conversion, or other non-cash terms.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH
from src.io.financial_update import SEC_HEADERS


DEFAULT_TRIAGE = Path(PROJECT_PATH) / "output/data_provenance/sec_submission_triage_unresolved_terminal_2026-08-08.json"
DEFAULT_CACHE_DIR = Path(PROJECT_PATH) / "output/data_provenance/sec_terminal_filing_cache"
DEFAULT_OUTPUT = Path(PROJECT_PATH) / "output/data_provenance/sec_terminal_filing_evidence_2026-08-08.json"
TERMINAL_REVIEWS = {"PRICE_SOURCE_AND_TERMINAL_RETURN_REVIEW", "TERMINAL_RETURN_REVIEW"}
AMOUNT_RE = re.compile(r"\$\s*([0-9]+(?:\.[0-9]{1,6})?)")
PER_SHARE_RE = re.compile(r"\b(per\s+(?:ordinary\s+|common\s+)?share|for\s+each\s+(?:ordinary\s+|common\s+)?share|each\s+(?:ordinary\s+|common\s+)?share)\b", re.I)
POSITIVE_TERMS = re.compile(r"\b(merger|consideration|receive|converted|conversion|cash)\b", re.I)
NEGATIVE_TERMS = re.compile(r"\b(exercise price|strike price|option award|restricted stock|warrant)\b", re.I)
DIRECT_COMMON_CASH_RE = re.compile(
    r"(?:each\s+(?:outstanding\s+)?share|shares?\s+of\s+(?:the\s+)?(?:company\s+)?common\s+stock)"
    r".{0,900}?converted\s+into\s+the\s+right\s+to\s+receive.{0,240}$",
    re.I,
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _filing_text(payload: bytes) -> str:
    parser = _TextExtractor()
    parser.feed(payload.decode("utf-8", errors="replace"))
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def _extract_amount_leads(payload: bytes) -> list[dict]:
    text = _filing_text(payload)
    leads: list[dict] = []
    seen: set[tuple[float, str]] = set()
    for match in AMOUNT_RE.finditer(text):
        start = max(0, match.start() - 240)
        end = min(len(text), match.end() + 240)
        context = text[start:end].strip()
        direct_prefix = text[max(0, match.start() - 700):match.start()]
        direct_common_cash = bool(DIRECT_COMMON_CASH_RE.search(direct_prefix))
        if (
            not (PER_SHARE_RE.search(context) or direct_common_cash)
            or not POSITIVE_TERMS.search(context)
        ):
            continue
        amount = float(match.group(1))
        if amount <= 0:
            continue
        if direct_common_cash:
            classification = "REVIEW_FIXED_COMMON_SHARE_CASH"
        else:
            classification = (
                "REJECT_LIKELY_SECURITY_AWARD_TERM"
                if NEGATIVE_TERMS.search(context)
                else "REVIEW_PER_SHARE_AMOUNT"
            )
        key = (amount, context)
        if key in seen:
            continue
        seen.add(key)
        leads.append({
            "amount": amount,
            "classification": classification,
            "context": context,
        })
    return leads


def _cache_path(cache_dir: str | Path, ticker: str, accession: str) -> Path:
    safe_accession = re.sub(r"[^0-9A-Za-z_-]+", "_", accession)
    return Path(cache_dir) / f"{ticker.lower()}_{safe_accession}.json.gz"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fetch(url: str, timeout: int = 30) -> bytes:
    request = Request(url, headers=SEC_HEADERS)
    with urlopen(request, timeout=timeout) as response:
        payload = response.read()
    if not payload:
        raise ValueError(f"empty SEC filing response: {url}")
    return payload


def _write_cache(path: Path, url: str, payload: bytes) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "format_version": 1,
        "source_url": url,
        "fetched_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "payload_sha256": _sha256(payload),
        "payload_hex": payload.hex(),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(envelope, handle, sort_keys=True)
    os.replace(temporary, path)
    return envelope


def _read_cache(path: Path, expected_url: str) -> tuple[dict, bytes]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    if envelope.get("source_url") != expected_url:
        raise ValueError(f"cached filing URL mismatch: {path}")
    payload = bytes.fromhex(envelope["payload_hex"])
    if _sha256(payload) != envelope.get("payload_sha256"):
        raise ValueError(f"cached filing payload hash mismatch: {path}")
    return envelope, payload


def _last_close(ticker: str, price_dir: str | Path) -> float | None:
    path = Path(price_dir) / f"{ticker.lower()}.csv"
    if not path.exists():
        return None
    frame = pd.read_csv(path, usecols=["date", "close"])
    if frame.empty:
        return None
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna().sort_values("date")
    return None if frame.empty else float(frame.iloc[-1]["close"])


def build_evidence(
    triage_path: str | Path,
    *,
    cache_dir: str | Path,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    refresh: bool = False,
    refresh_missing_only: bool = False,
    offset: int = 0,
    limit: int | None = None,
    filing_selection: str = "first",
    fetcher=_fetch,
) -> dict:
    triage_path = Path(triage_path)
    triage_bytes = triage_path.read_bytes()
    triage = json.loads(triage_bytes)
    candidates = []
    for record in triage.get("records", []):
        if record.get("resolution_review") not in TERMINAL_REVIEWS:
            continue
        filings = [
            filing for filing in record.get("event_filings_after_local_price", [])
            if filing.get("form") in {"8-K", "8-K/A", "6-K", "6-K/A"}
        ]
        if not filings:
            continue
        selected_filings = (
            filings if filing_selection == "all" else
            [filings[-1]] if filing_selection == "latest" else
            [filings[0]]
        )
        candidates.extend((record, filing) for filing in selected_filings)
    selected = candidates[offset : None if limit is None else offset + limit]
    rows = []
    for record, filing in selected:
        ticker = record["ticker"]
        path = _cache_path(cache_dir, ticker, filing["accession"])
        try:
            should_fetch = refresh or (refresh_missing_only and not path.exists())
            envelope = _write_cache(path, filing["source_url"], fetcher(filing["source_url"])) if should_fetch else _read_cache(path, filing["source_url"])[0]
            if should_fetch:
                payload = bytes.fromhex(envelope["payload_hex"])
            else:
                envelope, payload = _read_cache(path, filing["source_url"])
            leads = _extract_amount_leads(payload)
            close = _last_close(ticker, price_dir)
            for lead in leads:
                lead["implied_terminal_return"] = (
                    lead["amount"] / close - 1.0 if close and close > 0 else None
                )
            rows.append({
                "ticker": ticker,
                "cik": record.get("cik"),
                "last_local_price_date": record.get("last_local_price_date"),
                "last_local_close": close,
                "filing": filing,
                "cache_path": str(path),
                "payload_sha256": envelope["payload_sha256"],
                "amount_leads": leads,
                "status": "REVIEW_REQUIRED" if leads else "NO_PER_SHARE_AMOUNT_FOUND",
            })
        except Exception as exc:
            rows.append({
                "ticker": ticker,
                "filing": filing,
                "status": "FETCH_OR_CACHE_ERROR",
                "error": str(exc),
            })
    return {
        "format_version": 1,
        "research_only": True,
        "triage_path": str(triage_path),
        "triage_sha256": _sha256(triage_bytes),
        "mode": (
            "refresh" if refresh else
            "refresh_missing_only" if refresh_missing_only else
            "offline_cache"
        ),
        "candidate_count": len(candidates),
        "offset": offset,
        "requested_count": len(selected),
        "filing_selection": filing_selection,
        "records": rows,
    }


def _write_json(path: str | Path, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--triage", default=str(DEFAULT_TRIAGE))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--refresh-missing-only", action="store_true")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--filing-selection",
        choices=["first", "latest", "all"],
        default="first",
    )
    args = parser.parse_args()
    report = build_evidence(
        args.triage,
        cache_dir=args.cache_dir,
        price_dir=args.price_dir,
        refresh=args.refresh,
        refresh_missing_only=args.refresh_missing_only,
        offset=args.offset,
        limit=args.limit,
        filing_selection=args.filing_selection,
    )
    _write_json(args.output, report)
    counts = pd.Series(
        [row["status"] for row in report["records"]], dtype="object"
    ).value_counts()
    print(json.dumps({
        "candidate_count": report["candidate_count"],
        "requested_count": report["requested_count"],
        "counts": {str(key): int(value) for key, value in counts.items()},
    }, indent=2))


if __name__ == "__main__":
    main()
