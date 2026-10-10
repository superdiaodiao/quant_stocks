import hashlib
import json

from scripts import research_v16_freeze_protocol as protocol


def test_v16_protocol_is_deterministic_and_pre_execution(tmp_path):
    output = tmp_path / "protocol.json"
    first = protocol.build(output)
    first_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    second = protocol.build(output)
    second_sha = hashlib.sha256(output.read_bytes()).hexdigest()

    assert first_sha == second_sha == first["output"]["sha256"]
    assert second["parameters_frozen"] is True
    assert second["historical_confirmation_executed"] is False
    assert second["confirmation_results_inspected"] is False
    assert second["frozen_parameter"]["qqq_sma_sessions"] == 50
    assert second["release_status"] == "BLOCKED"
    assert second["promotion_eligible"] is False


def test_v16_protocol_labels_exposure_and_locks_confirmation_gates(tmp_path):
    report = protocol.build(tmp_path / "protocol.json")
    confirmation = report["data_split"]["historical_confirmation"]
    gates = report["predeclared_gates"]

    assert confirmation["start"] == "2025-01-01"
    assert confirmation["end"] == "2026-07-17"
    assert confirmation["exposure_status"] == "HUMAN_EXPOSURE_CONTAMINATED"
    assert confirmation["statistically_untouched"] is False
    assert gates["confirmation_annual_excess_win_count"] == {
        "10_bps": {"required": 2, "total_years": 2},
        "30_bps": {"required": 1, "total_years": 2},
        "50_bps": {"required": 1, "total_years": 2},
    }
    assert gates["full_history_annual_excess_win_count"]["10_bps"] == {
        "required": 4,
        "total_years": 5,
    }
    assert gates["leave_one_out"]["removed_weight_behavior"] == (
        "leave as cash; do not renormalize"
    )


def test_v16_protocol_contains_no_confirmation_results(tmp_path):
    output = tmp_path / "protocol.json"
    protocol.build(output)
    raw = json.loads(output.read_text(encoding="utf-8"))

    assert "confirmation_results" not in raw
    assert raw["historical_confirmation_executed"] is False
    assert raw["confirmation_results_inspected"] is False
