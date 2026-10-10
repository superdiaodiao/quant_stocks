"""IBKR's public short-stock file (``usa.txt``: fee rate, rebate rate and shares available per symbol): the parser
and the reader of the daily snapshots (moved unchanged from quant/observation/smisp.py, phase 3).

The recorder is ``scripts/record_borrow_fees.py``. It stays a self-contained script at that path: the borrow-fee
workflow (.github/workflows/borrow_fees.yml) checks out only that one file and runs it with a bare ``python3``, so it
cannot import this package. Its ``parse_header`` is the recorder's own row count check; ``parse_borrow_text`` below
reads the rows.
"""
from __future__ import annotations

import gzip
from datetime import datetime
from pathlib import Path

import pandas as pd

from quant.paths import CACHE_ROOT

BORROW_DIR = CACHE_ROOT / "borrow_fees"     # the forward observation passes its own (FORWARD_BORROW_DIR)

def parse_borrow_text(text: str, symbols=None) -> tuple[pd.Timestamp, pd.DataFrame]:
    """IBKR usa.txt -> (file time as New York wall clock, rows SYM / FEERATE (% a year) / AVAILABLE / REBATERATE).
    AVAILABLE '>10000000' becomes 10,000,001; ``symbols`` keeps only those rows."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("#BOF|"):
        raise ValueError("missing #BOF header")
    stamp = pd.Timestamp(datetime.strptime(lines[0][5:].strip(), "%Y.%m.%d|%H:%M:%S"))
    head = lines[1].lstrip("#").rstrip("|").split("|")
    want = set(symbols) if symbols is not None else None
    rows = []
    for ln in lines[2:]:
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split("|")
        rec = dict(zip(head, parts))
        sym = rec.get("SYM", "").strip()
        if want is not None and sym not in want:
            continue
        av = rec.get("AVAILABLE", "").strip()
        avn = 10_000_001.0 if av.startswith(">") else pd.to_numeric(av, errors="coerce")
        rows.append({"SYM": sym, "CUR": rec.get("CUR", ""), "FEERATE": pd.to_numeric(rec.get("FEERATE"), errors="coerce"),
                     "REBATERATE": pd.to_numeric(rec.get("REBATERATE"), errors="coerce"), "AVAILABLE": avn,
                     "AVAILABLE_raw": av})
    df = pd.DataFrame(rows, columns=["SYM", "CUR", "FEERATE", "REBATERATE", "AVAILABLE", "AVAILABLE_raw"])
    return stamp, df[df["CUR"].isin(["USD", ""])].drop_duplicates("SYM", keep="first").set_index("SYM")


class BorrowSnapshots:
    """The daily snapshots of scripts/record_borrow_fees.py. ``on(day)`` = the latest snapshot whose own file time
    (New York) falls on or before ``day``."""

    def __init__(self, root: Path = BORROW_DIR):
        self.root = root
        self.index = self._index()
        self._cache: dict = {}

    def _index(self) -> list:
        out = []
        log = self.root / "log.csv"
        known = {}
        if log.exists():
            for ln in log.read_text().splitlines():
                p = ln.split(",")
                if len(p) >= 5 and p[4].endswith(".txt.gz"):
                    known[p[4]] = p[2]
        for f in sorted((self.root / "raw").glob("usa_*.txt.gz")):
            stamp = known.get(f.name)
            if stamp is None:
                with gzip.open(f, "rt", encoding="utf-8", errors="replace") as fh:
                    stamp = fh.readline()[5:].strip()
            try:
                t = pd.Timestamp(datetime.strptime(stamp, "%Y.%m.%d|%H:%M:%S"))
            except ValueError:
                continue
            out.append((t, f))
        out.sort(key=lambda x: (x[0], x[1].name))
        return out

    def file_on(self, day) -> tuple[pd.Timestamp, Path] | None:
        end = pd.Timestamp(pd.Timestamp(day).strftime("%Y-%m-%d")) + pd.Timedelta(days=1)
        cand = [x for x in self.index if x[0] < end]
        return cand[-1] if cand else None

    def rows(self, path: Path, symbols) -> pd.DataFrame:
        key = (path, tuple(sorted(symbols)))
        if key not in self._cache:
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                self._cache[key] = parse_borrow_text(fh.read(), symbols)[1]
        return self._cache[key]

    def on(self, day, symbols) -> tuple[pd.Timestamp | None, dict]:
        """{sym: (feerate % a year, available) or None when the name is not in the file}."""
        hit = self.file_on(day)
        if hit is None:
            return None, {s: None for s in symbols}
        df = self.rows(hit[1], symbols)
        return hit[0], {s: ((float(df.at[s, "FEERATE"]), float(df.at[s, "AVAILABLE"])) if s in df.index else None)
                        for s in symbols}
