import json

import pytest

from scripts import research_v44_monthly_rank_buffer_stop_development as v44
from scripts import research_v45_rank_buffer_runtime_repair as v45


def test_v45_copies_every_v44_hypothesis_field(tmp_path):
    path = tmp_path / "protocol.json"
    protocol = v45.freeze_protocol(path)
    source = json.loads(v44.PROTOCOL_PATH.read_text(encoding="utf-8"))

    for field in (
        "evaluation_boundary",
        "candidate_grid",
        "controlled_dimension",
        "fixed_model",
        "cost_bps",
        "training_eligibility_gates",
        "selection_order",
        "walk_forward_folds",
        "v43_replacement_rule",
    ):
        assert protocol[field] == source[field]
    assert protocol["source_diagnosis"]["candidate_grid_changed"] is False
    assert protocol["source_diagnosis"]["training_gates_changed"] is False
    assert protocol["runtime_input_policy"][
        "performance_results_read_from_failed_v44"
    ] is False


def test_v45_remains_research_only_and_blocked(tmp_path):
    protocol = v45.freeze_protocol(tmp_path / "protocol.json")

    assert protocol["brokerage_or_trading_authorized"] is False
    assert protocol["broker_connection_used"] is False
    assert protocol["order_created"] is False
    assert protocol["release_status"] == "BLOCKED"
    assert protocol["promotion_eligible"] is False


def test_v45_protocol_cannot_be_overwritten(tmp_path):
    path = tmp_path / "protocol.json"
    v45.freeze_protocol(path)

    with pytest.raises(RuntimeError, match="will not be overwritten"):
        v45.freeze_protocol(path)
