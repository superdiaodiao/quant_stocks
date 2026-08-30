import pytest

from scripts import research_v25_buffered_weekly_can_slim as v25


def test_candidate_grid_is_small_and_stock_only():
    specs = v25.candidate_specs()

    assert len(specs) == 9
    assert {spec["maximum_financial_age_days"] for spec in specs} == {
        150,
        365,
        550,
    }
    assert {spec["rank_buffer_multiple"] for spec in specs} == {2, 3, 4}
    for spec in specs:
        config = v25._base_config(spec)
        assert config.signal_frequency == "weekly"
        assert config.top_n == 3
        assert config.maximum_position_weight == pytest.approx(1.0 / 3.0)
    assert "QQQ" in v25.FORBIDDEN_ETFS


def test_rank_buffer_retains_incumbents_and_fills_from_current_top_three():
    selected = v25.buffered_selection(
        ["A", "B", "C"],
        ["D", "E", "A", "B", "F", "G", "C"],
        top_n=3,
        rank_buffer_multiple=2,
    )

    assert selected == ["A", "B", "D"]


def test_rank_buffer_replaces_name_only_after_it_leaves_buffer():
    retained = v25.buffered_selection(
        ["A", "B", "C"],
        ["D", "E", "F", "A", "B", "C"],
        top_n=3,
        rank_buffer_multiple=2,
    )
    replaced = v25.buffered_selection(
        retained,
        ["D", "E", "F", "A", "B", "G"],
        top_n=3,
        rank_buffer_multiple=2,
    )

    assert retained == ["A", "B", "C"]
    assert replaced == ["A", "B", "D"]


def test_rank_buffer_rejects_invalid_parameters():
    with pytest.raises(ValueError, match="positive"):
        v25.buffered_selection([], [], top_n=0, rank_buffer_multiple=2)
