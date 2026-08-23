from __future__ import annotations

import pandas as pd
import json
from pathlib import Path

from scripts.research_v6_shadow_signal import (
    build_v6_recommendations,
    risk_sleeves_as_of,
    record_signal,
)


def test_risk_signal_uses_only_history_through_decision() -> None:
    dates = pd.bdate_range("2024-01-02", periods=120)
    base = pd.Series(0.001, index=dates)
    qqq = pd.Series(100.0, index=dates)

    result = risk_sleeves_as_of(
        base, qqq, dates[-1], lookbacks=(42, 45), trend_window=100
    )

    assert result["risk_on_sleeves"] == 2
    assert all(item["stock_leads"] for item in result["sleeves"])


def test_v6_target_is_25pct_stock_plus_qqq_and_cash() -> None:
    base = pd.DataFrame({
        "ticker": ["AAA", "BBB"],
        "target_weight": [0.6, 0.4],
    })
    risk = {"risk_on_sleeves": 1, "sleeves": []}

    result = build_v6_recommendations(
        base, risk,
        decision_date=pd.Timestamp("2026-08-31"),
        execution_date="2026-09-01",
    )

    weights = result.set_index("ticker")["target_weight"]
    assert weights["AAA"] == 0.15
    assert weights["BBB"] == 0.10
    assert weights["QQQ"] == 0.375
    assert weights["__CASH__"] == 0.375
    assert weights.sum() == 1.0
    assert result["broker_action_authorized"].eq(False).all()


def test_month_end_waits_when_source_data_has_not_reached_signal(
    tmp_path: Path, monkeypatch
) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({
        "model_version": "can-slim-v6-walkforward-defensive-ensemble-shadow",
        "policy_status": "FROZEN_FORWARD_ONLY",
        "release_status": "BLOCKED",
        "forward_evidence_start": "2026-09-01",
    }))
    fake = pd.DataFrame({
        "execution_date": ["2026-08-03"],
        "ticker": ["AAA"],
        "target_weight": [1.0],
    })
    monkeypatch.setattr(
        "scripts.research_v6_shadow_signal.generate_can_slim_shadow_recommendations",
        lambda **kwargs: (fake, {
            "as_of": "2026-08-07", "signal_date": "2026-07-31"
        }),
    )
    universe = tmp_path / "universe.csv"
    universe.write_text("Symbol\nAAA\n")

    result = record_signal(
        decision_date="2026-08-31",
        summary_path=summary,
        base_state_path=tmp_path / "state.csv",
        qqq_path=tmp_path / "qqq.csv",
        output_dir=tmp_path / "out",
        universe_path=universe,
    )

    assert result["status"] == "WAITING_FOR_MONTH_END_SOURCE_DATA"
    assert result["written"] is False
