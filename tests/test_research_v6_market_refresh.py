from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from scripts import research_v6_market_refresh as market_refresh
from scripts.research_v6_market_refresh import seed_cache


def test_seed_cache_never_writes_formal_source(tmp_path: Path, monkeypatch) -> None:
    formal = tmp_path / "formal"
    formal.mkdir()
    source = formal / "aaa.csv"
    source.write_text("date,close\n2026-08-07,10\n")
    index_source = tmp_path / "formal_index.csv"
    index_source.write_text("date,close\n2026-08-07,100\n")
    monkeypatch.setattr(
        "scripts.research_v6_market_refresh.CLEANED_PRICE_DATA_DIR", str(formal)
    )
    monkeypatch.setattr(
        "scripts.research_v6_market_refresh.NASDAQ_INDEX_FILE", str(index_source)
    )
    root = tmp_path / "research"

    result = seed_cache(
        ["AAA"], price_dir=root / "prices", index_path=root / "index.csv"
    )

    assert (root / "prices/aaa.csv").read_text() == source.read_text()
    assert source.read_text() == "date,close\n2026-08-07,10\n"
    assert result["formal_market_files_modified"] is False
    assert result["formal_financial_files_modified"] is False


def test_refresh_requests_common_equity_universe_only(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "research"
    summary = tmp_path / "summary.json"
    summary.write_text("{}")
    calls = []

    def fake_refresh_universe(_end, **kwargs):
        calls.append(kwargs)
        target = Path(kwargs["target_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"Symbol": ["LIVE"], "Name": ["Live Common Stock"]}).to_csv(
            target, index=False
        )
        return {"count": 1}

    monkeypatch.setattr(market_refresh, "refresh_universe", fake_refresh_universe)
    monkeypatch.setattr(
        market_refresh, "seed_cache", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        market_refresh, "update_all", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        market_refresh, "reconcile_research_index", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        market_refresh, "refresh_core_price", lambda _path: None
    )
    monkeypatch.setattr(
        market_refresh, "build_readiness",
        lambda **_kwargs: {"ready_for_v6_signal": False},
    )

    market_refresh.refresh(
        expected_session="2026-08-10", summary_path=summary,
        root=root, qqq_path=tmp_path / "qqq.csv", workers=1,
    )

    assert calls == [{
        "min_market_cap": 0,
        "target_path": root / "current_universe.csv",
        "common_equities_only": True,
    }]


def test_reconcile_index_uses_audited_close_then_historical_replaces_it(
    tmp_path: Path, monkeypatch
) -> None:
    index = tmp_path / "index.csv"
    provenance = tmp_path / "provenance.json"
    pd.DataFrame([{
        "date": "2026-08-07", "open": 1, "high": 1, "low": 1,
        "close": 26690.62, "volume": None, "change_rate": 0.01,
    }]).to_csv(index, index=False)
    historical_old = pd.DataFrame([{
        "date": pd.Timestamp("2026-08-07"), "open": 1, "high": 1,
        "low": 1, "close": 26690.62, "volume": None,
    }])
    monkeypatch.setattr(
        market_refresh, "fetch_history",
        lambda *_args, **_kwargs: historical_old.copy(),
    )
    monkeypatch.setattr(
        market_refresh, "fetch_closed_index_snapshot",
        lambda *_args, **_kwargs: {
            "date": "2026-08-10", "close": 26605.36,
            "source": "nasdaq_official_closed_chart_info",
            "source_urls": {}, "market_status": "Closed",
            "chart_time_as_of": "Aug 10, 2026",
            "last_trade_timestamp": "Aug 10, 2026",
            "payload_sha256": "a" * 64,
        },
    )

    first = market_refresh.reconcile_research_index(
        pd.Timestamp("2026-08-10"), index_path=index,
        provenance_path=provenance,
    )

    saved = pd.read_csv(index)
    assert first["fallback_used"] is True
    assert saved.iloc[-1]["date"] == "2026-08-10"
    assert saved.iloc[-1]["close"] == 26605.36
    assert "2026-08-10" in json.loads(provenance.read_text())["records"]

    monkeypatch.setattr(
        market_refresh, "fetch_closed_index_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("verified fallback should be retained")
        ),
    )
    retained = market_refresh.reconcile_research_index(
        pd.Timestamp("2026-08-10"), index_path=index,
        provenance_path=provenance,
    )
    assert retained["source"] == "retained_official_close_fallback"
    assert retained["source_verified"] is True

    historical_new = pd.concat([
        historical_old,
        pd.DataFrame([{
            "date": pd.Timestamp("2026-08-10"), "open": 2, "high": 2,
            "low": 2, "close": 26605.35, "volume": None,
        }]),
    ], ignore_index=True)
    monkeypatch.setattr(
        market_refresh, "fetch_history",
        lambda *_args, **_kwargs: historical_new.copy(),
    )
    monkeypatch.setattr(
        market_refresh, "fetch_closed_index_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("fallback should not be called")
        ),
    )

    second = market_refresh.reconcile_research_index(
        pd.Timestamp("2026-08-10"), index_path=index,
        provenance_path=provenance,
    )

    saved = pd.read_csv(index)
    assert second["fallback_used"] is False
    assert saved.iloc[-1]["close"] == 26605.35
    assert json.loads(provenance.read_text())["records"] == {}
