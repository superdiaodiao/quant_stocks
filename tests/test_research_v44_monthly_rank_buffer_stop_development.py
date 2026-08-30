import json

import pandas as pd
import pytest

from scripts import research_v44_monthly_rank_buffer_stop_development as v44


def test_candidate_grid_changes_only_rank_buffer():
    specs = v44.candidate_specs()

    assert len(specs) == 3
    assert {spec["rank_buffer_multiple"] for spec in specs} == {1, 2, 3}
    assert {spec["top_n"] for spec in specs} == {5}
    assert {spec["individual_trailing_stop_fraction"] for spec in specs} == {
        0.25
    }
    assert v44.DEVELOPMENT_YEARS == tuple(range(2020, 2026))
    assert v44.FINAL_COMPARISON_YEARS == ()


def test_buffered_selection_retains_names_inside_buffer_then_fills_rank():
    ranked = list("ABCDEFGHIJKLMNO")

    assert v44.buffered_selection(
        ranked,
        ["F", "B", "K", "Z", "A"],
        top_n=5,
        rank_buffer_multiple=2,
    ) == ["F", "B", "A", "C", "D"]


def test_buffered_selection_rejects_invalid_or_duplicate_inputs():
    with pytest.raises(ValueError, match="positive"):
        v44.buffered_selection(["A"], [], top_n=0, rank_buffer_multiple=1)
    with pytest.raises(ValueError, match="unique"):
        v44.buffered_selection(
            ["A", "A"], [], top_n=1, rank_buffer_multiple=1
        )


def _fake_results(excess_by_year):
    dates = pd.to_datetime([f"{year}-06-30" for year in v44.DEVELOPMENT_YEARS])
    benchmark = pd.Series(0.10, index=dates)
    strategy = benchmark + pd.Series(excess_by_year, index=dates)
    frame = pd.DataFrame({
        "strategy": strategy,
        "benchmark": benchmark,
        "turnover": [1.0] * len(dates),
        "stop_exits": [0] * len(dates),
    }, index=dates)
    return {cost: frame.copy() for cost in v44.COSTS}


def test_selection_requires_every_training_year_positive_at_50bps():
    good = _fake_results([0.02] * 6)
    bad = _fake_results([0.02, 0.02, 0.02, -0.001, 0.02, 0.02])

    selected, ranking = v44.select_candidate({
        v44.BASELINE_CANDIDATE: bad,
        "rank_buffer_2x_individual_stop_25pct": good,
    })

    assert selected == "rank_buffer_2x_individual_stop_25pct"
    good_row = next(row for row in ranking if row["candidate"] == selected)
    bad_row = next(
        row for row in ranking if row["candidate"] == v44.BASELINE_CANDIDATE
    )
    assert good_row["positive_training_years_50bps"] == 6
    assert good_row["training_eligible"] is True
    assert bad_row["training_eligible"] is False


def test_protocol_is_training_only_and_preserves_v43(tmp_path):
    protocol_path = tmp_path / "protocol.json"
    protocol = v44.freeze_protocol(protocol_path)

    boundary = protocol["evaluation_boundary"]
    assert boundary["training_years_excluded_from_final_comparison"] is True
    assert boundary["final_comparison_years"] == []
    assert boundary["2026_used_for_parameter_selection"] is False
    assert boundary["architecture_isolated_from_2026"] is False
    assert protocol["v43_replacement_rule"][
        "v43_remains_frozen_baseline_unless_v44_development_status_passes"
    ] is True
    assert protocol["release_status"] == "BLOCKED"
    assert protocol["promotion_eligible"] is False
    assert json.loads(v44.V43_PROTOCOL.read_text(encoding="utf-8"))[
        "status"
    ] == "FROZEN_WAITING_FOR_FIRST_SIGNAL"

    with pytest.raises(RuntimeError, match="will not be overwritten"):
        v44.freeze_protocol(protocol_path)
