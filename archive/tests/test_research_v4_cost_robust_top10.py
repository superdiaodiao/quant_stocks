from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

import pytest

from scripts.research_v4_cost_robust_top10 import (
    challenger_config,
    validate_selection_evidence,
)


def test_challenger_is_liquid_fresh_and_diversified() -> None:
    config = challenger_config()

    assert config.top_n == 10
    assert config.maximum_position_weight == 0.1
    assert config.minimum_median_dollar_volume == 10_000_000.0
    assert config.maximum_financial_age_days == 150


def test_selection_evidence_must_bind_the_quarterly_input(
    tmp_path: Path,
) -> None:
    quarterly = tmp_path / "quarterly.csv"
    quarterly.write_text("ticker\nTEST\n")
    artifact = tmp_path / "screen.csv"
    artifact.write_text("config_id\n15\n")
    configs = [{} for _ in range(16)]
    configs[15] = asdict(challenger_config())
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "diagnostic_only": True,
                "promotion_eligible": False,
                "robust_candidate_ids": [15],
                "quarterly_input": {"sha256": "wrong"},
                "candidate_configs": configs,
                "artifact": {"path": str(artifact), "sha256": "wrong"},
            }
        )
    )

    with pytest.raises(ValueError, match="quarterly input"):
        validate_selection_evidence(evidence, quarterly)
