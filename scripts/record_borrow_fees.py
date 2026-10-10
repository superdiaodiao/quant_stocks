"""Daily snapshot of IBKR's public short-stock availability and borrow-fee file.

IBKR publishes ``usa.txt`` on its public FTP (user ``shortstock``, empty password):
symbol, rebate rate, fee rate and shares available, refreshed through the day.
History is not offered, so this keeps one raw snapshot per run under
``research_cache/borrow_fees/raw/`` (git-ignored). It feeds the forward observation
of the S-MISP short overlay (docs/forward_observation_checklist.md).

    PYTHONPATH=. .venv/bin/python scripts/record_borrow_fees.py
"""
from __future__ import annotations

import argparse
import gzip
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MAIN_ROOT = REPO.parents[2] if REPO.parent.name == "worktrees" else REPO
OUT = MAIN_ROOT / "research_cache" / "borrow_fees"
HOSTS = ("ftp2.interactivebrokers.com", "ftp3.interactivebrokers.com")
MIN_ROWS = 1000


def parse_header(text: str) -> tuple[str, int]:
    """Return the file's own timestamp ('2026.10.09|22:47:23') and its data-row count."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("#BOF|"):
        raise ValueError("missing #BOF header")
    stamp = lines[0][len("#BOF|"):]
    rows = sum(1 for ln in lines if ln and not ln.startswith("#"))
    return stamp, rows


def fetch(host: str) -> str:
    out = subprocess.run(
        ["curl", "-sS", "--max-time", "120", "-u", "shortstock:", f"ftp://{host}/usa.txt"],
        capture_output=True, check=True)
    return out.stdout.decode("utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    raw_dir = args.out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    errors = []
    for host in HOSTS:
        try:
            text = fetch(host)
            stamp, rows = parse_header(text)
            if rows < MIN_ROWS:
                raise ValueError(f"only {rows} rows")
        except (subprocess.CalledProcessError, ValueError) as exc:
            errors.append(f"{host}: {exc}")
            continue
        now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = raw_dir / f"usa_{now}.txt.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(text)
        with open(args.out / "log.csv", "a", encoding="utf-8") as fh:
            fh.write(f"{now},{host},{stamp},{rows},{path.name}\n")
        print(f"saved {path.name}: file time {stamp}, {rows} rows")
        return 0
    print("failed: " + "; ".join(errors), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
