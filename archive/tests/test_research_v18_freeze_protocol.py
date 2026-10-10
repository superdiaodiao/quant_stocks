import hashlib
import json

from scripts import research_v18_freeze_protocol as protocol


def test_v18_protocol_is_deterministic_and_pre_execution(tmp_path):
    output = tmp_path / "protocol.json"
    first = protocol.build(output)
    first_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    second = protocol.build(output)
    second_sha = hashlib.sha256(output.read_bytes()).hexdigest()

    assert first_sha == second_sha == first["output"]["sha256"]
    assert second["parameters_frozen"] is True
    assert second["historical_robustness_replay_executed"] is False
    assert second["post_development_results_inspected"] is False
    assert second["release_status"] == "BLOCKED"
    assert second["promotion_eligible"] is False


def test_v18_protocol_locks_source_architecture_and_strict_gates(tmp_path):
    report = protocol.build(tmp_path / "protocol.json")
    architecture = report["frozen_architecture"]
    gates = report["predeclared_gates"]

    assert architecture["stock_weight"] == 0.20
    assert architecture["qqq_weight"] == 0.80
    assert architecture["new_weight_grid_searched"] is False
    assert gates["full_history_nasdaq_annual_win_count"] == {
        f"{cost}_bps": {"required": 5, "total_years": 5}
        for cost in (10, 30, 50)
    }
    assert gates["full_history_qqq_annual_win_count"] == {
        f"{cost}_bps": {"required": 3, "total_years": 5}
        for cost in (10, 30, 50)
    }
    assert gates["leave_one_satellite_out"]["removed_weight_behavior"] == (
        "leave as cash; do not renormalize"
    )


def test_v18_protocol_labels_repeated_exposure_and_contains_no_results(tmp_path):
    output = tmp_path / "protocol.json"
    protocol.build(output)
    raw = json.loads(output.read_text(encoding="utf-8"))
    robustness = raw["data_split"]["historical_robustness"]

    assert robustness["exposure_status"] == "REPEATEDLY_HUMAN_EXPOSED"
    assert robustness["statistically_untouched"] is False
    assert robustness["may_be_called_confirmation"] is False
    assert "historical_results" not in raw
