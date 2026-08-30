import json
from pathlib import Path

import pandas as pd
import pytest

from scripts import research_v47_hybrid_entry_portfolio_stop as v47
from scripts import research_v48_isolated_prospective_v47_observation as v48
from scripts import research_v49_v47_2026_exposed_oos as v49


def _targets() -> pd.DataFrame:
    return pd.DataFrame({
        "effective_date": pd.to_datetime(
            [f"2026-{month:02d}-02" for month in range(1, 8)]
        ),
        "ticker": ["A"] * 7,
        "target_weight": [1.0] * 7,
    })


def _results(strategy_returns=None) -> dict[int, pd.DataFrame]:
    dates = pd.to_datetime([f"2026-{month:02d}-02" for month in range(1, 8)])
    strategy_returns = strategy_returns or [0.02] * 7
    frame = pd.DataFrame({
        "strategy": strategy_returns,
        "benchmark": [0.0] * 7,
        "qqq": [0.0] * 7,
        "turnover": [0.1] * 7,
        "stock_stop_exits": [0] * 7,
        "portfolio_stop_exits": [0] * 7,
        "stop_exits": [0] * 7,
    }, index=dates)
    return {cost: frame.copy() for cost in v49.COSTS}


def test_protocol_precommits_exposed_oos_boundary_and_one_month_consequence(
    tmp_path: Path,
) -> None:
    before = v48.read_ledger(v48.LEDGER_PATH)
    protocol = v49.freeze_protocol(tmp_path / "protocol.json")

    assert protocol["frozen_model"]["parameters_used_2026"] is False
    assert protocol["frozen_model"]["architecture_isolated_from_2026"] is False
    assert protocol["evaluation_boundary"]["parameter_out_of_sample"] is True
    assert protocol["evaluation_boundary"]["pristine_forward_test"] is False
    assert protocol["precommitted_gates"][
        "minimum_monthly_wins_vs_nasdaq_at_50bps"
    ] == 4
    assert "one complete true-prospective month" in protocol[
        "decision_policy_frozen_before_result"
    ]["if_all_gates_pass"]
    assert protocol["release_status"] == "BLOCKED"
    assert v48.read_ledger(v48.LEDGER_PATH) == before


def test_evaluation_passes_all_precommitted_gates() -> None:
    evaluation = v49.evaluate_observation(_results(), _targets())

    assert evaluation["all_precommitted_gates_passed"] is True
    assert evaluation["training_years_counted_as_wins"] == 0
    assert evaluation["costs"]["50"]["monthly_wins_vs_nasdaq"] == 7
    assert all(evaluation["gates"].values())


def test_evaluation_blocks_large_drawdown_even_with_positive_prior_months() -> None:
    evaluation = v49.evaluate_observation(
        _results([0.20, 0.20, 0.20, 0.20, 0.20, 0.20, -0.50]),
        _targets(),
    )

    assert evaluation["gates"]["absolute_drawdown_50bps"] is False
    assert evaluation["gates"]["drawdown_lag_vs_nasdaq_50bps"] is False
    assert evaluation["all_precommitted_gates_passed"] is False


def test_observe_uses_exact_frozen_v47_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets_path = tmp_path / "targets.csv"
    _targets().to_csv(targets_path, index=False)
    monkeypatch.setattr(v49, "V27_TARGETS", targets_path)
    monkeypatch.setattr(
        v49,
        "_validated_protocol",
        lambda _path: (
            {"evaluation_boundary": {"evidence_class": "EXPOSED"}},
            "a" * 64,
        ),
    )
    monkeypatch.setattr(
        v49.v27,
        "_load_inputs",
        lambda: {"raw_close": object(), "nasdaq": object(), "qqq": object()},
    )
    calls = []

    def fake_replay(*_args, **kwargs):
        calls.append(kwargs)
        return _results()[50].drop(columns="qqq")

    monkeypatch.setattr(v47, "replay_with_hybrid_stop", fake_replay)
    monkeypatch.setattr(
        v49.v27,
        "_canonicalize_result",
        lambda daily, _nasdaq, _qqq: daily.assign(qqq=0.0),
    )

    report = v49.observe(
        tmp_path / "protocol.json",
        tmp_path / "results",
    )

    assert report["observation_status"] == "PASS_EXPOSED_OUT_OF_SAMPLE"
    assert report["decision_consequence"] == (
        "ONE_COMPLETE_TRUE_PROSPECTIVE_MONTH_FOR_FIRST_DECISION"
    )
    assert {call["transaction_cost_bps"] for call in calls} == {10.0, 30.0, 50.0}
    assert all(call["entry_loss_fraction"] == 0.20 for call in calls)
    assert all(call["portfolio_stop_fraction"] == 0.25 for call in calls)
    manifest = json.loads(
        (tmp_path / "results" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["parameters_changed_after_result"] is False
    assert manifest["release_status"] == "BLOCKED"


def test_observe_refuses_to_overwrite_result_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "results"
    output.mkdir()
    (output / "existing.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        v49,
        "_validated_protocol",
        lambda _path: ({"evaluation_boundary": {}}, "a" * 64),
    )

    with pytest.raises(RuntimeError, match="will not be overwritten"):
        v49.observe(tmp_path / "protocol.json", output)
