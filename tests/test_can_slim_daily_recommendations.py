import hashlib
import json

import pandas as pd
import pytest

from src.research.can_slim import CanSlimConfig
from src.research.can_slim_daily_recommendations import (
    add_current_liquidity_guidance,
    configs_for_decision_date,
    generate_can_slim_shadow_recommendations,
    quarterly_input_from_summary,
    refresh_parameter_snapshot_if_due,
    reuse_recorded_signal_portfolio,
    save_can_slim_shadow_recommendations,
)


def test_quarterly_input_from_summary_verifies_frozen_sha(tmp_path):
    quarterly = tmp_path / "quarterly.csv"
    quarterly.write_bytes(b"ticker,date\nTEST,2026-01-01\n")
    expected = hashlib.sha256(quarterly.read_bytes()).hexdigest()

    path, fingerprint = quarterly_input_from_summary(
        {"quarterly_input": {"path": str(quarterly), "sha256": expected}}
    )

    assert path == quarterly.resolve()
    assert fingerprint["sha256"] == expected
    with pytest.raises(ValueError, match="SHA-256"):
        quarterly_input_from_summary(
            {"quarterly_input": {"path": str(quarterly), "sha256": "wrong"}}
        )


def test_current_liquidity_guidance_is_full_target_not_order_sizing():
    dates = pd.to_datetime(["2026-07-29", "2026-07-30"])
    recommendations = pd.DataFrame({
        "ticker": ["A", "__CASH__"],
        "target_weight": [1 / 3, 0.0],
    })
    dollar_volume = pd.DataFrame(
        {"A": [10_000_000.0, 20_000_000.0]}, index=dates
    )

    result = add_current_liquidity_guidance(
        recommendations, dollar_volume, dates[-1],
        account_sizes=(100_000.0,),
    )

    assert result.loc[0, "current_median_dollar_volume_50d"] == 15_000_000.0
    assert result.loc[
        0, "full_target_participation_at_100000_account"
    ] == pytest.approx((100_000 / 3) / 15_000_000)
    assert result.loc[
        0, "full_target_account_capacity_at_1pct"
    ] == pytest.approx(450_000.0)
    assert pd.isna(
        result.loc[1, "full_target_account_capacity_at_1pct"]
    )


def test_recommendation_history_is_idempotent_for_same_signal(tmp_path):
    metadata = {
        "model_version": "can-slim-top3-v1",
        "as_of": "2026-07-31",
    }
    first = pd.DataFrame([{
        "as_of": "2026-07-31",
        "ticker": "AAPL",
        "model_version": "can-slim-top3-v1",
        "signal_date": "2026-07-30",
        "generated_at": "2026-07-31T00:00:00+00:00",
    }])
    second = first.copy()
    second["generated_at"] = "2026-07-31T00:05:00+00:00"

    save_can_slim_shadow_recommendations(first, metadata, tmp_path)
    save_can_slim_shadow_recommendations(second, metadata, tmp_path)

    history = pd.read_csv(
        tmp_path / "can-slim-top3-v1" / "recommendation_history.csv"
    )
    assert len(history) == 1
    assert history["generated_at"].tolist() == [
        "2026-07-31T00:00:00+00:00",
    ]


def test_recommendation_history_rejects_changed_duplicate_signal(tmp_path):
    metadata = {
        "model_version": "can-slim-top3-v1",
        "as_of": "2026-07-31",
    }
    first = pd.DataFrame([{
        "as_of": "2026-07-31",
        "ticker": "AAPL",
        "model_version": "can-slim-top3-v1",
        "signal_date": "2026-07-30",
        "generated_at": "2026-07-31T00:00:00+00:00",
    }])
    changed = first.copy()
    changed["ticker"] = "MSFT"

    save_can_slim_shadow_recommendations(first, metadata, tmp_path)
    with pytest.raises(RuntimeError, match="different frozen portfolio"):
        save_can_slim_shadow_recommendations(changed, metadata, tmp_path)


def test_daily_recommendation_uses_time_frozen_model_snapshot():
    old = CanSlimConfig(top_n=5, ensemble_weight=2)
    new = CanSlimConfig(top_n=10, ensemble_weight=1)
    summary = {
        "current_shadow_configs": [new.__dict__],
        "model_snapshots": [
            {
                "effective_start": "2025-01-01", "training_end": "2024-12-31",
                "configs": [old.__dict__],
            },
            {
                "effective_start": "2026-01-01", "training_end": "2025-12-31",
                "configs": [new.__dict__],
            },
        ],
    }

    configs, snapshot = configs_for_decision_date(summary, "2025-07-01")

    assert configs[0].top_n == 5
    assert configs[0].ensemble_weight == 2
    assert snapshot["training_end"] == "2024-12-31"


def test_frozen_policy_is_not_backfilled_before_its_effective_date():
    config = CanSlimConfig(top_n=3)
    summary = {
        "current_shadow_configs": [config.__dict__],
        "model_snapshots": [{
            "effective_start": "2026-07-18",
            "training_end": "2026-07-17",
            "configs": [config.__dict__],
        }],
    }

    configs, snapshot = configs_for_decision_date(summary, "2026-07-17")

    assert configs == []
    assert snapshot is None


def test_shadow_metadata_keeps_signal_date_on_no_signal_cash_path(
    tmp_path, monkeypatch
):
    """The pipeline's downstream audits must work for a cash-only signal."""
    import src.research.can_slim_daily_recommendations as recommendations_module

    config = CanSlimConfig(top_n=3)
    summary = {
        "current_shadow_configs": [config.__dict__],
        "model_version": "can-slim-top3-v1",
        "release_status": "BLOCKED",
        "signal_frequency": "monthly",
    }
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    index_path = tmp_path / "index.csv"
    pd.DataFrame({
        "date": ["2026-07-31"],
        "close": [100.0],
    }).to_csv(index_path, index=False)
    dates = pd.DatetimeIndex(["2026-07-30", "2026-07-31"])
    close = pd.DataFrame({"AAA": [10.0, 10.5]}, index=dates)
    dollar_volume = pd.DataFrame({"AAA": [1_000_000.0, 1_100_000.0]}, index=dates)

    monkeypatch.setattr(
        recommendations_module,
        "refresh_parameter_snapshot_if_due",
        lambda *_args, **_kwargs: (summary, False),
    )
    monkeypatch.setattr(
        recommendations_module,
        "load_panel",
        lambda *_args, **_kwargs: (close, dollar_volume),
    )
    monkeypatch.setattr(
        recommendations_module,
        "back_adjust_common_splits",
        lambda frame: frame,
    )
    monkeypatch.setattr(
        recommendations_module,
        "NASDAQ_INDEX_FILE",
        index_path,
    )
    monkeypatch.setattr(
        recommendations_module,
        "online_rebalance_context",
        lambda *_args, **_kwargs: {
            "as_of": pd.Timestamp("2026-07-31"),
            "signal_date": pd.Timestamp("2026-07-31"),
            "execution_date": None,
            "order_pending": False,
        },
    )
    monkeypatch.setattr(
        recommendations_module,
        "configs_for_decision_date",
        lambda *_args, **_kwargs: ([], None),
    )
    monkeypatch.setattr(
        recommendations_module,
        "universe_as_of",
        lambda *_args, **_kwargs: ["AAA"],
    )
    monkeypatch.setattr(
        recommendations_module,
        "load_universe_snapshots",
        lambda: [],
    )
    monkeypatch.setattr(
        recommendations_module,
        "can_slim_input_fingerprints",
        lambda: {
            "strategy_code": {"sha256": "strategy-sha"},
            "data_manifest": {"sha256": "data-sha", "components": {}},
        },
    )
    monkeypatch.setattr(
        recommendations_module,
        "portfolio_source_columns",
        lambda: {},
    )

    recommendations, metadata = generate_can_slim_shadow_recommendations(
        summary_file=summary_path,
        history_file=tmp_path / "history.csv",
    )

    assert metadata["signal_date"] == "2026-07-31"
    assert recommendations.loc[0, "ticker"] == "__CASH__"
    assert recommendations.loc[0, "action_reason"] == (
        "MODEL_NOT_YET_EFFECTIVE_AT_EXECUTION"
    )


def test_midyear_initial_snapshot_prevents_same_year_refit(
    tmp_path, monkeypatch
):
    config = CanSlimConfig(top_n=3)
    summary_file = tmp_path / "summary.json"
    summary_file.write_text(json.dumps({
        "parameter_update_frequency": "annual",
        "current_shadow_configs": [config.__dict__],
        "model_snapshots": [{
            "effective_start": "2026-07-18",
            "training_end": "2026-07-17",
            "configs": [config.__dict__],
        }],
    }), encoding="utf-8")

    monkeypatch.setattr(
        "src.research.can_slim_daily_recommendations."
        "fit_annual_parameter_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("2026 already has its frozen snapshot")
        ),
    )

    _, refreshed = refresh_parameter_snapshot_if_due(
        summary_file, "2026-07-26"
    )

    assert refreshed is False


def test_parameter_refresh_fits_new_year_from_prior_year_only(
    tmp_path, monkeypatch
):
    old = CanSlimConfig(top_n=5)
    new = CanSlimConfig(top_n=10)
    summary_file = tmp_path / "summary.json"
    summary_file.write_text(json.dumps({
        "signal_frequency": "monthly",
        "uses_quarterly_fundamentals": True,
        "uses_adaptive_channel": True,
        "current_shadow_config_ids": [0],
        "current_shadow_configs": [old.__dict__],
        "model_snapshots": [{
            "effective_start": "2026-01-01",
            "effective_end": "2026-12-31",
            "training_end": "2025-12-31",
            "config_ids": [0],
            "configs": [old.__dict__],
        }],
    }), encoding="utf-8")
    calls = []

    def fake_fit(year, **kwargs):
        calls.append((year, kwargs))
        return ({
            "effective_start": "2027-01-01",
            "effective_end": "2027-12-31",
            "training_end": "2026-12-31",
            "config_ids": [1],
            "configs": [new.__dict__],
        }, __import__("pandas").DataFrame({"config_id": [1]}))

    monkeypatch.setattr(
        "src.research.can_slim_daily_recommendations."
        "fit_annual_parameter_snapshot",
        fake_fit,
    )

    summary, refreshed = refresh_parameter_snapshot_if_due(
        summary_file, "2027-01-03"
    )

    assert refreshed is True
    assert calls == [(2027, {
        "signal_frequency": "monthly",
        "use_quarterly_fundamentals": True,
        "adaptive_channel": True,
    })]
    assert summary["model_snapshots"][-1]["training_end"] == "2026-12-31"
    assert summary["current_shadow_configs"][0]["top_n"] == 10


def test_parameter_refresh_is_idempotent_within_year(
    tmp_path, monkeypatch
):
    config = CanSlimConfig(top_n=5)
    summary_file = tmp_path / "summary.json"
    summary_file.write_text(json.dumps({
        "current_shadow_configs": [config.__dict__],
        "model_snapshots": [{
            "effective_start": "2027-01-01",
            "effective_end": "2027-12-31",
            "training_end": "2026-12-31",
            "configs": [config.__dict__],
        }],
    }), encoding="utf-8")

    def unexpected_fit(*args, **kwargs):
        raise AssertionError("snapshot must stay frozen within its year")

    monkeypatch.setattr(
        "src.research.can_slim_daily_recommendations."
        "fit_annual_parameter_snapshot",
        unexpected_fit,
    )

    _, refreshed = refresh_parameter_snapshot_if_due(
        summary_file, "2027-08-01"
    )

    assert refreshed is False


def test_frozen_policy_never_refits(tmp_path, monkeypatch):
    config = CanSlimConfig(top_n=3)
    summary_file = tmp_path / "summary.json"
    summary_file.write_text(json.dumps({
        "parameter_update_frequency": "frozen",
        "current_shadow_configs": [config.__dict__],
        "model_snapshots": [],
    }), encoding="utf-8")

    monkeypatch.setattr(
        "src.research.can_slim_daily_recommendations."
        "fit_annual_parameter_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("frozen policy must never refit")
        ),
    )

    _, refreshed = refresh_parameter_snapshot_if_due(
        summary_file, "2027-01-03"
    )

    assert refreshed is False


def test_rerun_preserves_the_first_portfolio_github_run_source(tmp_path):
    history_file = tmp_path / "history.csv"
    pd.DataFrame({
        "as_of": ["2026-08-01"],
        "ticker": ["A"],
        "rank": [1],
        "model_version": ["model"],
        "signal_date": ["2026-07-31"],
        "generated_at": ["2026-08-01T01:00:00Z"],
        "portfolio_generated_at": ["2026-08-01T01:00:00Z"],
        "target_weight": [1.0],
        "portfolio_source_kind": ["github_actions_run"],
        "portfolio_repository": ["owner/repository"],
        "portfolio_workflow": ["shadow"],
        "portfolio_run_id": ["111"],
        "portfolio_run_attempt": ["1"],
        "portfolio_run_url": [
            "https://github.com/owner/repository/actions/runs/111"
        ],
        "action_reason": ["OLD_REASON"],
    }).to_csv(history_file, index=False)
    rerun = pd.DataFrame({
        "ticker": ["B"],
        "model_version": ["model"],
        "signal_date": ["2026-07-31"],
        "generated_at": ["2026-08-02T01:00:00Z"],
        "target_weight": [1.0],
        "portfolio_source_kind": ["github_actions_run"],
        "portfolio_run_id": ["222"],
    })

    frozen = reuse_recorded_signal_portfolio(
        rerun,
        history_file,
        pd.Series({"A": 10.0, "B": 20.0}),
        as_of=pd.Timestamp("2026-08-02"),
        execution_date=pd.Timestamp("2026-08-03"),
        generated_at="2026-08-02T01:00:00Z",
        action="BUY_NEXT_CLOSE",
        mode="SHADOW",
        action_reason="ACTIVE_SELECTION",
    )

    assert frozen["ticker"].tolist() == ["A"]
    assert frozen["portfolio_run_id"].astype(str).tolist() == ["111"]
    assert frozen["portfolio_generated_at"].tolist() == [
        "2026-08-01T01:00:00Z"
    ]
    assert frozen["action_reason"].tolist() == ["ACTIVE_SELECTION"]


def test_legacy_zero_weight_cash_history_migrates_to_unique_sentinel(
    tmp_path,
):
    history_file = tmp_path / "history.csv"
    pd.DataFrame({
        "as_of": ["2026-08-01"],
        "ticker": ["CASH"],
        "rank": [1],
        "model_version": ["model"],
        "signal_date": ["2026-07-31"],
        "generated_at": ["2026-08-01T01:00:00Z"],
        "target_weight": [0.0],
    }).to_csv(history_file, index=False)
    rerun = pd.DataFrame({
        "ticker": ["__CASH__"],
        "model_version": ["model"],
        "signal_date": ["2026-07-31"],
        "generated_at": ["2026-08-02T01:00:00Z"],
        "target_weight": [0.0],
    })

    frozen = reuse_recorded_signal_portfolio(
        rerun,
        history_file,
        pd.Series({"CASH": 86.15}),
        as_of=pd.Timestamp("2026-08-02"),
        execution_date=pd.Timestamp("2026-08-03"),
        generated_at="2026-08-02T01:00:00Z",
        action="HOLD_CASH",
        mode="SHADOW",
    )

    assert frozen["ticker"].tolist() == ["__CASH__"]
    assert frozen["current_price"].tolist() == [1.0]
