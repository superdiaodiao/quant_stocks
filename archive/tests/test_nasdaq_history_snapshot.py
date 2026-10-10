import json
from pathlib import Path

import pandas as pd

import scripts.nasdaq_history_snapshot as snapshot_module


def test_create_snapshot_writes_replayable_canonical_frame(tmp_path: Path, monkeypatch):
    frame = pd.DataFrame([
        {"date": "2025-01-02", "open": 10, "high": 11, "low": 9,
         "close": 10.5, "volume": 100},
        {"date": "2025-01-03", "open": 11, "high": 12, "low": 10,
         "close": 11.5, "volume": 200},
    ])
    frame["date"] = pd.to_datetime(frame["date"])
    monkeypatch.setattr(snapshot_module, "fetch_history", lambda *args, **kwargs: frame.copy())
    output = tmp_path / "snapshot.json"

    report = snapshot_module.create_snapshot(
        "test", "2025-01-01", "2025-01-03", output
    )

    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert report == persisted
    assert report["ticker"] == "TEST"
    assert report["rows"] == 2
    assert "fromdate=2025-01-01" in report["source_url"]
    assert report["frame_sha256"]
