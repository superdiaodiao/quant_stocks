import hashlib
import json

from scripts import research_v14_freeze_protocol as protocol


def test_protocol_build_is_deterministic_and_does_not_execute_results(
    tmp_path,
) -> None:
    output = tmp_path / "protocol.json"
    first = protocol.build(output)
    first_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    second = protocol.build(output)
    second_sha = hashlib.sha256(output.read_bytes()).hexdigest()

    assert first_sha == second_sha == first["output"]["sha256"]
    assert second["protocol_status"] == "FROZEN_RESEARCH_PROTOCOL"
    assert second["parameters_frozen"] is True
    assert second["final_data_replay_executed"] is False
    assert second["results_inspected"] is False
    assert second["release_status"] == "BLOCKED"
    assert second["promotion_eligible"] is False


def test_protocol_locks_exact_grid_selector_and_exclusions(tmp_path) -> None:
    report = protocol.build(tmp_path / "protocol.json")
    grid = report["model_grid"]
    configs = grid["configs"]

    assert grid["candidate_count"] == len(configs) == 18
    assert {row["top_n"] for row in configs} == {3, 5, 10}
    assert {row["minimum_median_dollar_volume"] for row in configs} == {
        2_000_000.0,
        10_000_000.0,
    }
    assert {row["maximum_financial_age_days"] for row in configs} == {
        150,
        365,
        550,
    }
    assert {row["minimum_eps_growth"] for row in configs} == {0.25}
    assert {row["minimum_revenue_growth"] for row in configs} == {0.10}
    assert report["selector"]["tie_break_order"][-1] == "config_id ascending"
    assert report["selector"]["no_evidence_fallback"] is False
    assert tuple(report["execution"]["excluded_signal_dates"]) == (
        "2019-03-29",
        "2019-04-30",
        "2019-05-31",
        "2019-08-30",
        "2019-09-30",
    )


def test_protocol_accounts_for_all_live_financial_gaps(tmp_path) -> None:
    report = protocol.build(tmp_path / "protocol.json")
    evidence = report["input_bindings"]["financial_gap_evidence"]

    assert tuple(evidence) == protocol.EXPECTED_GAP_SYMBOLS
    assert report["input_bindings"]["missing_financial_observation_count"] == 40
    assert evidence["ITOS"]["classification"] == (
        "UNRECOVERABLE_ZERO_REVENUE_DENOMINATOR"
    )
    assert evidence["OZK"]["classification"] == (
        "SOURCE_LOCKED_PIT_HISTORY_LIMIT"
    )
    assert all(item["manifests"] for item in evidence.values())


def test_protocol_labels_prior_human_exposure_and_predeclares_gates(
    tmp_path,
) -> None:
    report = protocol.build(tmp_path / "protocol.json")
    final_data = report["data_split"]["final_data_historical_confirmation"]
    gates = report["predeclared_gates"]

    assert final_data["exposure_status"] == "HUMAN_EXPOSURE_CONTAMINATED"
    assert final_data["statistically_untouched"] is False
    assert gates["annual_excess_win_count"] == {
        "10_bps": {"required": 4, "total_years": 5},
        "30_bps": {"required": 3, "total_years": 5},
        "50_bps": {"required": 3, "total_years": 5},
    }
    assert gates["compounded_excess"]["cost_bps"] == [10, 30, 50]
    assert gates["leave_one_out"]["compounded_excess_threshold"] == 0.0
    assert gates["leave_one_out"]["removed_weight_behavior"] == (
        "leave as cash; do not renormalize"
    )


def test_protocol_json_contains_no_execution_results(tmp_path) -> None:
    output = tmp_path / "protocol.json"
    protocol.build(output)
    raw = json.loads(output.read_text(encoding="utf-8"))

    assert "walk_forward_results" not in raw
    assert "selected_config_ids" not in raw
    assert raw["final_data_replay_executed"] is False
