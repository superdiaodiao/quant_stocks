import json
from pathlib import Path

import pandas as pd

import scripts.sec_suspension_tail_import as module


def test_imports_overlap_validated_tail_ending_before_suspension(tmp_path: Path, monkeypatch):
    prices = tmp_path / "prices"; prices.mkdir()
    dates = pd.bdate_range("2025-01-02", periods=22)
    local = pd.DataFrame({
        "date": dates, "ticker": "OLD", "open": 10.0, "high": 10.2,
        "low": 9.8, "close": 10.1, "volume": 1000.0,
    })
    local.iloc[:-1].to_csv(prices / "old.csv", index=False)
    lines = ["<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>"]
    lines += [f"NEW.US,D,{d:%Y%m%d},000000,10,10.2,9.8,10.1,1000,0" for d in dates]
    source = tmp_path / "new.txt"; source.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(module, "_load_or_fetch_sec", lambda *args, **kwargs: b"trading will be suspended")
    output = tmp_path / "evidence.json"
    report = module.import_tail(
        historical_ticker="OLD", successor_ticker="NEW", source_path=source,
        source_url="https://source", sec_cache_path=tmp_path / "sec.json.gz",
        sec_source_url="https://sec", expected_filing_phrase="trading will be suspended",
        suspension_date=(dates[-1] + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
        output=output, price_dir=prices, apply=True,
    )
    assert report["rows_added"] == 1
    assert report["overlap_validation"]["passed"] is True
    assert json.loads(output.read_text())["status"] == "UPDATED"
    assert pd.read_csv(prices / "old.csv").iloc[-1]["date"] == dates[-1].strftime("%Y-%m-%d")
