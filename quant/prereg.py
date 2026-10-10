"""Pre-registration of a study: the ledger block between ``<!-- PREREG-BEGIN -->`` and ``<!-- PREREG-END -->``, its
SHA-256, and the frozen-registration file written once before the run.

Factored out of the identical ``prereg_block`` / ``prereg_hash`` / ``register`` functions of
scripts/research_fundamentals.py, research_ml_cross_section.py, research_short_overlay.py and
research_index_exclusion.py (phase 2); the bound methods keep those names in each study.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd


class Preregistration:
    def __init__(self, ledger: Path, frozen: Path, out: Path):
        self.ledger, self.frozen, self.out = ledger, frozen, out

    def block(self, text: str | None = None) -> str:
        t = text if text is not None else self.ledger.read_text()
        a, b = t.index("<!-- PREREG-BEGIN -->"), t.index("<!-- PREREG-END -->")
        return t[a:b]

    def hash(self, text: str | None = None) -> str:
        return hashlib.sha256(self.block(text).encode()).hexdigest()

    def register(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        if self.frozen.exists():
            raise SystemExit(f"already registered: {json.loads(self.frozen.read_text())}")
        self.frozen.write_text(json.dumps({"sha256": self.hash(), "registered_at": pd.Timestamp.now("UTC").isoformat()},
                                          indent=1) + "\n")
        print("registered", self.hash())
