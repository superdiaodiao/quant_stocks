from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from scripts.research_v5_forward_state import append_state


def test_v5_state_is_append_only_and_does_not_count_as_promotion(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({
        "model_version": "can-slim-v5-qqq-relative-trend-core-shadow",
        "policy_status": "FROZEN_FORWARD_ONLY",
        "release_status": "BLOCKED",
        "forward_evidence_start": "2026-08-10",
    }))
    qqq = tmp_path / "qqq.csv"
    pd.DataFrame({
        "date": ["2026-08-31"],
        "close": [600.0],
        "cash_dividend": [0.0],
    }).to_csv(qqq, index=False)
    observation = tmp_path / "observation.json"
    observation.write_text(json.dumps({
        "model_version": "can-slim-v4-cost-robust-top10-shadow",
        "status": "EXECUTION_ANCHOR_ONLY",
        "signal_date": "2026-08-31",
        "execution_date": "2026-09-01",
        "observation_date": "2026-08-31",
        "strategy_return": None,
    }))
    output = tmp_path / "state.csv"

    first = append_state(
        v4_observation_path=observation,
        summary_path=summary,
        qqq_path=qqq,
        output_path=output,
    )
    second = append_state(
        v4_observation_path=observation,
        summary_path=summary,
        qqq_path=qqq,
        output_path=output,
    )

    assert first["written"] is True
    assert first["counts_as_promotion_evidence"] is False
    assert second["status"] == "ALREADY_RECORDED"
    history = pd.read_csv(output)
    assert len(history) == 1
    assert bool(history["counts_as_promotion_evidence"].iloc[0]) is False
    assert history["v4_nav_accounting_method"].iloc[0] == (
        "chained_monthly_standalone_fixed_positions_with_full_entry_cost"
    )
