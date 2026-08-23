import json
from pathlib import Path

from scripts.research_v8_shadow_manifest import build_manifest


def test_manifest_freezes_v8_without_enabling_runtime(tmp_path: Path):
    research = tmp_path / "research.json"
    research.write_text(json.dumps({
        "release_status": "BLOCKED",
        "historical_selection_contaminated": True,
        "configuration": {"v7_capital_weight": 0.75},
        "forward_review_policy": {"final_decision": {"calendar_weeks": 39}},
        "inputs": {},
    }))
    execution = tmp_path / "execution.json"
    execution.write_text(json.dumps({
        "research_only": True,
        "robustness_gate": {
            "baseline_passed": True,
            "execution_stress_passed": True,
            "supported_account_sizes": [25000, 100000],
            "unsupported_account_sizes": {"10000": "fails stress"},
        },
    }))
    components = []
    for name in ("v6", "v7"):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({
            "release_status": "BLOCKED",
            **({"model_snapshots": [{
                "effective_start": "2026-01-01",
                "configs": [{"top_n": 10}],
            }]} if name == "v7" else {}),
        }))
        components.append(path)
    v6_base = tmp_path / "v6_base.json"
    v6_base.write_text(json.dumps({
        "model_snapshots": [{"effective_start": "2026-01-01"}],
    }))
    robustness = tmp_path / "robustness.json"
    robustness.write_text(json.dumps({"independent_forward_evidence": False}))
    output = tmp_path / "manifest.json"
    v7_component = tmp_path / "v7_component.json"
    quarterly = tmp_path / "quarterly.csv"
    quarterly.write_text("ticker,quarter\nAAA,2026Q1\n")
    v6_base.write_text(json.dumps({
        "model_snapshots": [{"effective_start": "2026-01-01"}],
        "quarterly_input": {"path": "quarterly.csv", "sha256": "abc"},
    }))
    result = build_manifest(
        research, execution, components[0], v6_base, components[1], robustness,
        output, v7_component, quarterly
    )
    assert result["release_status"] == "BLOCKED"
    assert result["observation_runtime_enabled"] is False
    assert result["supported_account_sizes"] == [25000, 100000]
    assert json.loads(v7_component.read_text())["parameter_update_frequency"] == "frozen"
    assert result["evidence_boundaries"]["thirty_nine_weeks_is_final_accept_or_reject_review"]
