from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from src.research import daily_pipeline, data_audit
from src.research.can_slim_daily_recommendations import (
    save_can_slim_shadow_recommendations,
)
from src.strategy.common import (
    online_monthly_rebalance_context,
    online_rebalance_context,
    scheduled_signal_dates,
)


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)


def test_data_audit_fails_when_financials_are_stale(tmp_path, monkeypatch):
    universe_path = tmp_path / "universe.csv"
    index_path = tmp_path / "index.csv"
    eps_path = tmp_path / "eps.csv"
    price_dir = tmp_path / "prices"
    _write_csv(universe_path, pd.DataFrame({"Symbol": ["ABC"], "Name": ["ABC Common Stock"]}))
    _write_csv(index_path, pd.DataFrame({"date": ["2026-07-17"]}))
    _write_csv(
        price_dir / "abc.csv",
        pd.DataFrame({"date": ["2026-07-17"], "close": [10.0]}),
    )
    _write_csv(
        eps_path,
        pd.DataFrame({
            "ticker": ["ABC"],
            "period_end": ["2024-09-30"],
            "available_date": ["2024-11-01"],
            "quarterly_eps": [1.0],
            "source": ["sec_companyfacts"],
            "fetched_at": ["2024-11-01"],
            "exact_report_date": [True],
        }),
    )
    monkeypatch.setattr(data_audit, "NASDAQ_300M_STOCK_LIST_FILE", str(universe_path))
    monkeypatch.setattr(data_audit, "NASDAQ_INDEX_FILE", str(index_path))
    monkeypatch.setattr(data_audit, "POINT_IN_TIME_EPS_FILE", str(eps_path))
    monkeypatch.setattr(data_audit, "CLEANED_PRICE_DATA_DIR", str(price_dir))
    monkeypatch.setattr(data_audit, "PROJECT_PATH", str(tmp_path))

    report = data_audit.audit_project_data(date(2026, 7, 17))

    assert report["status"] == "FAIL"
    assert not report["checks"]["financial_coverage"]
    assert report["missing_or_stale_financials"] == ["ABC"]
    with pytest.raises(RuntimeError, match="financial_coverage"):
        data_audit.require_project_data(date(2026, 7, 17))


def test_data_audit_rejects_an_entire_market_snapshot_that_is_stale(tmp_path, monkeypatch):
    universe_path = tmp_path / "universe.csv"
    index_path = tmp_path / "index.csv"
    eps_path = tmp_path / "eps.csv"
    price_dir = tmp_path / "prices"
    _write_csv(universe_path, pd.DataFrame({"Symbol": ["ABC"], "Name": ["ABC Common Stock"]}))
    _write_csv(index_path, pd.DataFrame({"date": ["2026-06-01"]}))
    _write_csv(price_dir / "abc.csv", pd.DataFrame({"date": ["2026-06-01"], "close": [10.0]}))
    _write_csv(eps_path, pd.DataFrame({
        "ticker": ["ABC"], "period_end": ["2026-03-31"],
        "available_date": ["2026-05-01"], "quarterly_eps": [1.0],
        "source": ["sec_companyfacts"], "fetched_at": ["2026-05-01"],
        "exact_report_date": [True],
    }))
    monkeypatch.setattr(data_audit, "NASDAQ_300M_STOCK_LIST_FILE", str(universe_path))
    monkeypatch.setattr(data_audit, "NASDAQ_INDEX_FILE", str(index_path))
    monkeypatch.setattr(data_audit, "POINT_IN_TIME_EPS_FILE", str(eps_path))
    monkeypatch.setattr(data_audit, "CLEANED_PRICE_DATA_DIR", str(price_dir))
    monkeypatch.setattr(data_audit, "PROJECT_PATH", str(tmp_path))

    report = data_audit.audit_project_data(date(2026, 7, 17))
    assert report["checks"]["price_coverage"]
    assert not report["checks"]["benchmark_fresh"]
    assert report["status"] == "FAIL"


def test_daily_pipeline_does_not_recommend_after_failed_data_gate(monkeypatch):
    calls = []

    def reject(_as_of):
        calls.append("audit")
        raise RuntimeError("Data readiness failed: financial_coverage")

    monkeypatch.setattr(daily_pipeline, "require_project_data", reject)
    monkeypatch.setattr(
        daily_pipeline,
        "generate_can_slim_shadow_recommendations",
        lambda: calls.append("recommend") or (pd.DataFrame(), {}),
    )
    monkeypatch.setattr(
        daily_pipeline,
        "save_can_slim_shadow_recommendations",
        lambda *_: calls.append("save"),
    )
    monkeypatch.setattr("sys.argv", ["daily_pipeline", "--skip-update"])

    with pytest.raises(RuntimeError, match="financial_coverage"):
        daily_pipeline.main()

    assert calls == ["audit"]


def test_online_monthly_signal_is_last_close_of_previous_month_and_then_holds():
    dates = pd.to_datetime(["2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02"])
    pending = online_monthly_rebalance_context(dates[:2], pd.Timestamp("2026-07-01"))
    assert pending["signal_date"] == pd.Timestamp("2026-06-30")
    assert pending["execution_date"] is None
    assert pending["order_pending"]

    executed = online_monthly_rebalance_context(dates, pd.Timestamp("2026-07-03"))
    assert executed["signal_date"] == pd.Timestamp("2026-06-30")
    assert executed["execution_date"] == pd.Timestamp("2026-07-01")
    assert not executed["order_pending"]

    historical = online_monthly_rebalance_context(
        dates, pd.Timestamp("2026-07-01")
    )
    assert historical["as_of"] == pd.Timestamp("2026-07-01")
    assert historical["execution_date"] == pd.Timestamp("2026-07-01")


def test_weekly_signals_and_online_recommendation_share_completed_week_logic():
    dates = pd.bdate_range("2026-06-29", "2026-07-10")
    signals = scheduled_signal_dates(dates, "2026-06-29", "2026-07-10", "weekly")
    assert signals.tolist() == [pd.Timestamp("2026-07-03"), pd.Timestamp("2026-07-10")]

    pending = online_rebalance_context(dates[:5], pd.Timestamp("2026-07-06"), "weekly")
    assert pending["signal_date"] == pd.Timestamp("2026-07-03")
    assert pending["execution_date"] is None
    assert pending["order_pending"]

    executed = online_rebalance_context(dates[:6], pd.Timestamp("2026-07-06"), "weekly")
    assert executed["execution_date"] == pd.Timestamp("2026-07-06")
    assert not executed["order_pending"]


def test_same_day_recommendation_rerun_replaces_entire_old_list(tmp_path):
    metadata = {
        "as_of": "2026-07-17",
        "model_version": "can-slim-top3-v1",
    }
    first = pd.DataFrame({
        "as_of": ["2026-07-17", "2026-07-17"],
        "ticker": ["OLD", "KEEP"],
        "model_version": ["factor-v1", "factor-v1"],
    })
    second = pd.DataFrame({
        "as_of": ["2026-07-17"],
        "ticker": ["NEW"],
        "model_version": ["factor-v1"],
    })
    save_can_slim_shadow_recommendations(first, metadata, tmp_path)
    save_can_slim_shadow_recommendations(second, metadata, tmp_path)
    history = pd.read_csv(
        tmp_path / "can-slim-top3-v1" / "recommendation_history.csv"
    )
    assert history["ticker"].tolist() == ["NEW"]
