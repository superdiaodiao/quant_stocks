from __future__ import annotations

import json

import pandas as pd
import pytest

from scripts import v50r3_sources_ready as sources


def _frame(*dates: str) -> pd.DataFrame:
    return pd.DataFrame({"date": pd.to_datetime(list(dates)), "close": 1.0})


@pytest.fixture
def nasdaq(monkeypatch: pytest.MonkeyPatch) -> dict:
    state = {"published": set(), "snapshot": False, "refused": set()}

    def fetch_history(symbol, start, _end, asset_class="stocks", retries=3):
        assert retries == 1
        if symbol in state["refused"]:
            raise RuntimeError(f"{symbol}: HTTP Error 403")
        if (symbol, asset_class) in state["published"]:
            return _frame("2026-09-29", "2026-09-30")
        return _frame("2026-09-29")

    def fetch_closed_index_snapshot(symbol, session):
        if not state["snapshot"]:
            raise ValueError(f"{symbol} official index market is not closed")
        return {"close": 1.0}

    monkeypatch.setattr(sources.nasdaq_update, "fetch_history", fetch_history)
    monkeypatch.setattr(
        sources.nasdaq_update, "fetch_closed_index_snapshot", fetch_closed_index_snapshot
    )
    return state


def test_nothing_is_published_right_after_the_close(nasdaq) -> None:
    result = sources.sources_ready("2026-09-30")
    assert result["ready"] is False
    assert result["checks"] == {
        "stock_sample": False, "qqq": False, "nasdaq_composite": False,
    }
    assert result["nasdaq_composite"] == (
        "ValueError: COMP official index market is not closed"
    )


def test_published_rows_and_the_official_closed_composite_are_ready(nasdaq) -> None:
    nasdaq["published"] |= {(symbol, "stocks") for symbol in sources.STOCK_SAMPLE}
    nasdaq["published"].add(("QQQ", "etf"))
    assert sources.sources_ready("2026-09-30")["checks"]["nasdaq_composite"] is False
    nasdaq["snapshot"] = True
    result = sources.sources_ready("2026-09-30")
    assert result["ready"] is True
    assert result["nasdaq_composite_source"] == "official_closed_snapshot"
    # The Composite history row, once published, needs no snapshot.
    nasdaq["snapshot"] = False
    nasdaq["published"].add(("COMP", "index"))
    assert sources.sources_ready("2026-09-30")["nasdaq_composite_source"] == "history"


def test_a_few_missing_sample_stocks_do_not_hold_the_session(
    nasdaq, capsys: pytest.CaptureFixture
) -> None:
    nasdaq["published"] |= {(symbol, "stocks") for symbol in sources.STOCK_SAMPLE[2:]}
    nasdaq["published"] |= {("QQQ", "etf"), ("COMP", "index")}
    nasdaq["refused"].add(sources.STOCK_SAMPLE[0])
    assert sources.main(["--as-of", "2026-09-30"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["stock_sample_priced"] == "8/10"
    assert set(report["stock_sample"]) == set(sources.STOCK_SAMPLE[:2])
    nasdaq["published"].discard(("QQQ", "etf"))
    assert sources.main(["--as-of", "2026-09-30"]) == 1
