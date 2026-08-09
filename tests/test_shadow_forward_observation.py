import json

import pandas as pd

from scripts import shadow_forward_observation as observation
from scripts import shadow_forward_status as status


def test_shadow_observation_is_overlap_checked_and_idempotent(tmp_path, monkeypatch):
    price_dir = tmp_path / "price"
    price_dir.mkdir()
    anchor = pd.Timestamp("2026-07-31")
    for ticker, close in {"KNSA": 74.34, "ROKU": 145.01, "VISN": 11.74}.items():
        pd.DataFrame({"date": [anchor], "close": [close]}).to_csv(
            price_dir / f"{ticker.lower()}.csv", index=False
        )
    index_path = tmp_path / "index.csv"
    pd.DataFrame({"date": [anchor], "close": [25373.85]}).to_csv(
        index_path, index=False
    )
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    pd.DataFrame({
        "as_of": ["2026-07-31"] * 3,
        "ticker": ["KNSA", "ROKU", "VISN"],
        "action": ["BUY_NEXT_CLOSE"] * 3,
        "model_version": ["can-slim-top3-v1"] * 3,
        "target_weight": [1 / 3] * 3,
    }).to_csv(model_dir / "recommendation_history.csv", index=False)

    monkeypatch.setattr(observation, "CLEANED_PRICE_DATA_DIR", str(price_dir))
    monkeypatch.setattr(observation, "NASDAQ_INDEX_FILE", str(index_path))
    monkeypatch.setattr(
        observation,
        "fetch_history",
        lambda *args, **kwargs: pd.DataFrame(columns=["date", "close"]),
    )

    def fake_chart(ticker, start, end):
        values = {
            "KNSA": (74.34, 72.50, 73.50),
            "ROKU": (145.01, 145.97, 146.50),
            "VISN": (11.74, 11.88, 12.00),
            "^IXIC": (25373.85, 25916.50, 26000.00),
        }[ticker]
        payload = f"{ticker}-payload".encode()
        return payload, pd.DataFrame({
            "date": [
                pd.Timestamp("2026-07-31"),
                pd.Timestamp("2026-08-03"),
                pd.Timestamp("2026-08-04"),
            ],
            "close": values,
        })

    monkeypatch.setattr(observation, "yahoo_chart", fake_chart)
    first = observation.record_observation(
        observation_date="2026-08-03", model_dir=model_dir
    )
    second = observation.record_observation(
        observation_date="2026-08-03", model_dir=model_dir
    )
    assert first["status"] == "EXECUTION_ANCHOR_ONLY"
    assert first["retrieved_at_utc"]
    assert all(row["source_provider"] == "Yahoo Chart API fallback" for row in first["tickers"])
    assert first["forward_sessions"] == 0
    assert second["status"] == "ALREADY_RECORDED"
    assert first["formal_price_cache_modified"] is False
    index = json.loads(
        (model_dir / "shadow_observations" / "index.json").read_text()
    )
    assert len(index["observations"]) == 1
    assert index["observations"][0]["observation_date"] == "2026-08-03"
    forward = observation.record_observation(
        observation_date="2026-08-04", model_dir=model_dir
    )
    assert forward["status"] == "UNANCHORED_FORWARD_OBSERVATION"
    assert forward["forward_sessions"] == 1
    assert forward["accounting_method"] == "standalone_fixed_positions"
    assert forward["transaction_cost_bps"] == 10.0
    assert forward["strategy_return"] is not None


def test_shadow_forward_status_keeps_release_gate_precommitted(tmp_path):
    directory = tmp_path / "observations"
    directory.mkdir()
    (directory / "index.json").write_text(json.dumps({
        "observations": [{
            "observation_date": "2026-08-03",
            "status": "UNANCHORED_FORWARD_OBSERVATION",
            "forward_sessions": 1,
        }],
    }))
    result = status.build_status(directory)
    assert result["observed_sessions"] == 1
    assert result["remaining_sessions_lower_bound"] == 251
    assert result["unanchored_observations"] == 1
    assert result["release_eligible"] is False
