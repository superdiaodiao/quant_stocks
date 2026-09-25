from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from scripts import research_v6_market_refresh as v6
from scripts import v50r3_event_prices as event_prices

NOW = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)


def _prices(path: Path, dates: list[str], close: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "date": dates, "close": close, "volume": 1000,
    }).to_csv(path, index=False)


@pytest.fixture
def layout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    r3 = event_prices.r3
    monkeypatch.setattr(r3, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(r3, "BUNDLES_DIR", tmp_path / "bundles")
    monkeypatch.setattr(r3, "LEDGER_PATH", tmp_path / "ledger" / "ledger.jsonl")
    monkeypatch.setattr(event_prices.v43, "read_ledger", lambda _path: [])
    formal = tmp_path / "formal"
    _prices(formal / "abcd.csv", ["2026-07-17", "2026-07-20"], 40.0)
    index = tmp_path / "formal_index.csv"
    pd.DataFrame({"date": ["2026-07-20"], "close": [1.0]}).to_csv(index, index=False)
    monkeypatch.setattr(v6, "CLEANED_PRICE_DATA_DIR", formal)
    monkeypatch.setattr(v6, "NASDAQ_INDEX_FILE", index)
    calls: list[dict] = []

    def update_all(**kwargs):
        calls.append(kwargs)
        path = Path(kwargs["price_dir"]) / "abcd.csv"
        frame = pd.read_csv(path)
        frame.loc[len(frame)] = {"date": "2026-10-01", "close": 4.0, "volume": 900}
        frame.to_csv(path, index=False)
        return {"failures": [], "provider_adjustments_recorded": []}

    monkeypatch.setattr(event_prices.nasdaq_update, "update_all", update_all)
    return {"tmp": tmp_path, "calls": calls}


def test_a_stock_no_mark_priced_starts_from_the_formal_baseline(
    layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(event_prices.r3, "_latest_valued_bundle", lambda *_args: None)

    result = event_prices.stage_event_prices("abcd", now=NOW)

    assert result["price_file_present"] and result["through"] == "2026-10-01"
    assert result["copied_from_formal_baseline"] == 1
    assert result["seeded_from_valued_bundle"] is False
    assert [row["date"] for row in result["latest_rows"]] == [
        "2026-07-17", "2026-07-20", "2026-10-01",
    ]
    (call,) = layout["calls"]
    assert call["tickers"] == ["ABCD"] and call["workers"] == 1
    assert str(call["end"]) == "2026-10-01"
    assert Path(call["price_dir"]) == layout["tmp"] / "work" / "market" / "prices"


def test_a_stock_a_mark_priced_starts_from_the_valued_bundle(
    layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = layout["tmp"] / "ledger" / "latest_valued_bundle"
    _prices(copy / "prices" / "abcd.csv", ["2026-09-29", "2026-09-30"], 8.0)
    monkeypatch.setattr(
        event_prices.r3, "_latest_valued_bundle",
        lambda *_args: {"as_of": pd.Timestamp("2026-09-30"), "path": copy},
    )

    result = event_prices.stage_event_prices("ABCD", now=NOW)

    assert result["seeded_from_valued_bundle"] is True
    assert result["copied_from_formal_baseline"] == 0
    assert [row["close"] for row in result["latest_rows"]] == [8.0, 8.0, 4.0]


def test_the_cli_fails_when_no_price_file_could_be_staged(
    layout, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(event_prices.r3, "_latest_valued_bundle", lambda *_args: None)
    monkeypatch.setattr(
        event_prices, "stage_event_prices",
        lambda ticker: {"ticker": ticker, "price_file_present": False},
    )
    assert event_prices.main(["ZZZZ"]) == 1
    assert '"price_file_present": false' in capsys.readouterr().out
