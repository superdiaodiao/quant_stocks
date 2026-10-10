import json
from pathlib import Path

import pandas as pd

import scripts.research_v14_adaptive_pretrain as pretrain_module
from scripts.research_v14_adaptive_pretrain import research_summary


def test_research_summary_cannot_promote_historical_pass() -> None:
    result = research_summary(
        {"release_status": "PASS", "wins_vs_nasdaq": 5},
        {"gates": {"adaptive_training_eligible": False}},
    )
    assert result["historical_diagnostic_status"] == "PASS"
    assert result["release_status"] == "BLOCKED"
    assert result["promotion_eligible"] is False
    assert result["parameters_frozen"] is False


def test_run_forwards_and_binds_research_price_overlay(
    tmp_path: Path, monkeypatch
) -> None:
    quarterly = tmp_path / "quarterly.csv"
    quarterly.write_text("ticker\nAAA\n", encoding="utf-8")
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    audit = tmp_path / "audit.json"
    audit.write_text(
        json.dumps(
            {
                "gates": {
                    "research_pretraining_allowed": True,
                    "adaptive_training_eligible": False,
                }
            }
        ),
        encoding="utf-8",
    )
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    (price_dir / "aaa.csv").write_text(
        "date,ticker,open,high,low,close,volume\n"
        "2025-01-02,AAA,10,11,9,10,100\n",
        encoding="utf-8",
    )
    captured = {}

    def walk_forward(**kwargs):
        captured.update(kwargs)
        return (
            pd.DataFrame(),
            pd.DataFrame(),
            {
                "release_status": "BLOCKED",
                "candidate_count": 18,
                "out_of_sample_years": 5,
                "wins_vs_nasdaq": 3,
            },
        )

    monkeypatch.setattr(pretrain_module, "run_walk_forward", walk_forward)
    result = pretrain_module.run(
        quarterly_path=quarterly,
        snapshot_dir=snapshots,
        data_audit_path=audit,
        output_dir=tmp_path / "out",
        price_dir=price_dir,
    )

    assert captured["price_dir"] == price_dir
    assert result["input_bindings"]["price_directory"]["path"] == str(price_dir)
    assert result["input_bindings"]["price_directory"]["file_count"] == 1
    assert result["input_bindings"]["price_directory"][
        "content_manifest_sha256"
    ]
