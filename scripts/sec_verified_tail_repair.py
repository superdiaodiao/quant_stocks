"""Append same-issuer ticker tails after a documented symbol transition.

The historical universe is keyed by the ticker used at the time.  When a
provider keeps the post-transition history only under the new symbol, this
script copies only the missing tail from the already imported new-symbol file.
It requires an exact-date overlap with the old file and records the SEC
identity evidence plus the overlap checks before writing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH


ANALYSIS_END = pd.Timestamp("2026-07-17")
PRICE_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
SPECS = (
    {
        "old": "MINM",
        "new": "FIEE",
        "effective_date": "2025-07-10",
        "cik": "0001467761",
        "sec_url": "https://www.sec.gov/Archives/edgar/data/1467761/000182912625004763/fiee_8k.htm",
        "sec_note": "SEC filing identifies FiEE, Inc. f/k/a Minim, Inc.; post-change provider history begins 2025-07-10.",
    },
    {
        "old": "SRM",
        "new": "TRON",
        "effective_date": "2025-07-17",
        "cik": "0001956744",
        "sec_url": "https://www.sec.gov/Archives/edgar/data/1956744/000164117225019830/form8-k.htm",
        "sec_note": "SEC filing states the Symbol Change takes effect on Nasdaq on 2025-07-17.",
    },
    {
        "old": "VLCN",
        "new": "EMPD",
        "effective_date": "2025-07-31",
        "cik": "0001829794",
        "sec_url": "https://www.sec.gov/Archives/edgar/data/1829794/000168316825005537/empery_8k.htm",
        "sec_note": "SEC filing states EMPD began trading at market open on 2025-07-31; appended dates are pre-transition.",
    },
)


def _read(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repair(price_dir: str | Path = CLEANED_PRICE_DATA_DIR, output: str | Path = Path(PROJECT_PATH) / "output/data_provenance/sec_verified_tail_repair.json") -> dict:
    price_dir, output = Path(price_dir), Path(output)
    results = []
    for spec in SPECS:
        old_path = price_dir / f"{spec['old'].lower()}.csv"
        new_path = price_dir / f"{spec['new'].lower()}.csv"
        old = _read(old_path)
        new = _read(new_path)
        overlap = old.merge(new, on="date", suffixes=("_old", "_new"))
        if len(overlap) < 20:
            raise ValueError(f"{spec['old']} overlap too small: {len(overlap)}")
        checks = {}
        for col in ("open", "high", "low", "close"):
            ratio = overlap[f"{col}_old"] / overlap[f"{col}_new"]
            checks[col] = {
                "median_ratio": float(ratio.median()),
                "within_1pct": float((ratio.sub(1).abs() <= 0.01).mean()),
            }
            if checks[col]["within_1pct"] < 0.99:
                raise ValueError(f"{spec['old']} {col} overlap check failed")
        first_allowed = max(old["date"].max() + pd.Timedelta(days=1), pd.Timestamp(spec["effective_date"]))
        tail = new[(new["date"] >= first_allowed) & (new["date"] <= ANALYSIS_END)].copy()
        tail = tail[PRICE_COLUMNS]
        if not tail.empty:
            tail.insert(1, "ticker", spec["old"])
            old = old[["date", "ticker", "open", "high", "low", "close", "volume"]]
            merged = pd.concat([old, tail], ignore_index=True).sort_values("date").drop_duplicates("date", keep="first")
            merged.to_csv(old_path, index=False, date_format="%Y-%m-%d")
        results.append({
            "historical_ticker": spec["old"],
            "provider_ticker": spec["new"],
            "cik": spec["cik"],
            "effective_date": spec["effective_date"],
            "sec_source_url": spec["sec_url"],
            "sec_note": spec["sec_note"],
            "overlap_sessions": int(len(overlap)),
            "cross_validation": checks,
            "rows_added": int(len(tail)),
            "source_first_date": tail["date"].min().strftime("%Y-%m-%d") if not tail.empty else None,
            "source_last_date": tail["date"].max().strftime("%Y-%m-%d") if not tail.empty else None,
            "source_file_sha256": _sha(new_path),
            "formal_financial_files_modified": False,
            "terminal_returns_modified": False,
            "research_only": True,
        })
    payload = {"analysis_end": ANALYSIS_END.strftime("%Y-%m-%d"), "results": results, "research_only": True}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--output", default=str(Path(PROJECT_PATH) / "output/data_provenance/sec_verified_tail_repair.json"))
    args = parser.parse_args()
    print(json.dumps(repair(args.price_dir, args.output), indent=2, sort_keys=True))
