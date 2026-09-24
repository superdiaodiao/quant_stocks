from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from scripts import research_v50r3_source_probe as probe


def _at(text: str) -> datetime:
    return pd.Timestamp(text).to_pydatetime()


def test_probe_reports_each_source_and_the_98pct_stock_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = pd.Timestamp("2026-09-30")
    rows = {"COMP": "2026-09-29", "QQQ": "2026-09-30", "AAA": "2026-09-30"}
    monkeypatch.setattr(
        probe, "_latest_row",
        lambda symbol, _cls, _session: rows.get(symbol, "2026-09-29"),
    )
    monkeypatch.setattr(probe, "_quote_fields", lambda *_args: {"info": {}})

    def after_hours(_symbol, _session):
        raise ValueError("COMP official index market is not closed")

    monkeypatch.setattr(
        probe.nasdaq_update, "fetch_closed_index_snapshot", after_hours
    )
    record = probe.probe(session, sample=["AAA", "BBB"], now=_at("2026-09-30T20:35:00Z"))

    assert record["minutes_after_official_close"] == 35.0
    assert record["comp_official_close_fallback"]["ok"] is False
    assert record["ready"] == {
        "comp_close": False,
        "qqq_row": True,
        "stock_rows_sample_at_least_98pct": False,
    }
    assert record["stock_sample"]["not_exact"] == {"BBB": "2026-09-29"}
    assert record["signal_sources_ready"] is False

    rows.update({"COMP": "2026-09-30", "BBB": "2026-09-30"})
    ready = probe.probe(session, sample=["AAA", "BBB"], now=_at("2026-09-30T21:00:00Z"))
    assert ready["signal_sources_ready"] is True


def test_probe_samples_common_equities_or_a_fallback(tmp_path: Path) -> None:
    assert probe.sample_tickers(3, tmp_path / "absent.csv") == ["AAPL", "MSFT", "NVDA"]
    universe = tmp_path / "universe.csv"
    pd.DataFrame({
        "Symbol": ["AAA", "BBBW", "NA"],
        "Name": [
            "Alpha Inc. Common Stock",
            "Beta Corp. Warrant",
            "Nano Labs Ltd Class A Ordinary Shares",
        ],
    }).to_csv(universe, index=False)
    assert sorted(probe.sample_tickers(10, universe)) == ["AAA", "NA"]


def test_latest_completed_close_uses_the_official_close() -> None:
    assert probe.latest_completed_close(_at("2026-09-30T19:59:00Z")) == pd.Timestamp(
        "2026-09-29"
    )
    assert probe.latest_completed_close(_at("2026-09-30T20:00:00Z")) == pd.Timestamp(
        "2026-09-30"
    )
