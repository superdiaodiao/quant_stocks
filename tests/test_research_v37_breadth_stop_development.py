from scripts import research_v37_breadth_stop_development as v37


def test_grid_changes_only_breadth_and_standard_stop():
    specs = v37.candidate_specs()

    assert len(specs) == 6
    assert {spec["top_n"] for spec in specs} == {5, 10}
    assert {spec["individual_trailing_stop_fraction"] for spec in specs} == {
        0.10,
        0.15,
        0.20,
    }
    assert {spec["liquid_candidate_pool"] for spec in specs} == {25}
    assert {spec["momentum_lookback_sessions"] for spec in specs} == {63}


def test_top10_base_spec_keeps_other_parameters_frozen():
    base = v30 = v37.v30.selected_specification()
    widened = v37.base_specification(10)

    for key, value in base.items():
        if key not in {"key", "top_n"}:
            assert widened[key] == value
    assert widened["top_n"] == 10


def test_protocol_excludes_training_years_from_final_comparison(tmp_path):
    protocol = v37.freeze_protocol(tmp_path / "protocol.json")
    boundary = protocol["evaluation_boundary"]

    assert boundary["training_years_excluded_from_final_comparison"] is True
    assert boundary["final_comparison_years"] == []
    assert boundary["2026_used_for_parameter_selection"] is False
    assert boundary["architecture_isolated_from_2026"] is False
    assert boundary["parameter_isolated_from_2026"] is True
