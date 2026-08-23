from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.research_v6_data_readiness import build_readiness


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_readiness_requires_fresh_market_and_frozen_bindings(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.py"
    runtime.write_text("frozen = True\n")
    quarterly = tmp_path / "quarterly.csv"
    quarterly.write_text("ticker,quarter\nAAA,2026Q1\n")
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({
        "quarterly_input": {"path": str(quarterly), "sha256": _sha(quarterly)},
        "bindings": {"runtime_code": {str(runtime): _sha(runtime)}},
    }))
    prices = tmp_path / "prices"
    prices.mkdir()
    (prices / "aaa.csv").write_text("date,close\n2026-08-07,10\n")
    index = tmp_path / "index.csv"
    index.write_text("date,close\n2026-08-07,100\n")
    qqq = tmp_path / "qqq.csv"
    qqq.write_text("date,close\n2026-08-07,100\n")

    result = build_readiness(
        expected_session="2026-08-07",
        summary_path=summary,
        price_dir=prices,
        index_path=index,
        qqq_path=qqq,
        universe=["AAA"],
    )

    assert result["ready_for_v6_signal"] is True
    assert all(result["gates"].values())
