import json

from src.research.can_slim import CanSlimConfig
from src.research.can_slim_daily_recommendations import (
    configs_for_decision_date,
    refresh_parameter_snapshot_if_due,
)


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
