from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.research import daily_pipeline, data_audit
from src.research.can_slim_daily_recommendations import (
    save_can_slim_shadow_recommendations,
)
from src.research.shadow_ledger import write_shadow_ledger_manifest
from src.strategy.common import (
    online_monthly_rebalance_context,
    online_rebalance_context,
    scheduled_signal_dates,
)


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)


def test_security_identity_integrity_rejects_ambiguous_rename():
    frame = pd.DataFrame({
        "provider_ticker": ["NEW1", "NEW2"],
        "historical_ticker": ["OLD", "OLD"],
        "last_historical_date": pd.to_datetime([
            "2025-06-30", "2025-06-30"
        ]),
        "current_ticker_first_date": pd.to_datetime([
            "2025-07-01", "2025-07-01"
        ]),
        "verified_at": [
            "2026-07-30T00:00:00Z",
            "not-a-timestamp",
        ],
        "identity_type": ["issuer_rename", "issuer_rename"],
    })

    result = data_audit.audit_security_identity_integrity(frame)

    assert result["complete"] is False
    assert result["issues"] == [
        "invalid_verified_at",
        "issuer_rename_old_ticker_not_one_to_one",
    ]


def test_selected_positive_weight_tickers_ignores_unselected_and_cash():
    recommendations = pd.DataFrame({
        "ticker": ["BAD", "UNSELECTED", "__CASH__"],
        "target_weight": [1 / 3, 0.0, 0.0],
    })

    assert daily_pipeline.selected_positive_weight_tickers(
        recommendations
    ) == ["BAD"]


def test_selected_price_calendar_uses_exact_signal_window(tmp_path):
    _write_csv(
        tmp_path / "bad.csv",
        pd.DataFrame({
            "date": ["2026-07-27", "2026-07-29"],
            "close": [20.0, 21.0],
            "volume": [1_000_000, 1_000_000],
        }),
    )

    result = data_audit.audit_selected_price_calendars(
        ["BAD"],
        "2026-07-29",
        history_sessions=3,
        price_dir=tmp_path,
    )

    assert result["complete"] is False
    assert result["gaps"] == [{
        "ticker": "BAD",
        "status": "INCOMPLETE_PRICE_CALENDAR",
        "missing_session_count": 1,
        "missing_sessions": ["2026-07-28"],
    }]


def test_quarterly_conflicts_are_reported_and_scoped_to_selected_signal():
    frame = pd.DataFrame({
        "ticker": ["BAD", "BAD", "OLD", "OLD"],
        "fiscal_end": pd.to_datetime([
            "2025-03-31", "2025-03-31",
            "2015-03-31", "2015-03-31",
        ]),
        "metric": ["net_income"] * 4,
        "available_date": pd.to_datetime([
            "2025-11-18", "2025-11-18",
            "2016-01-01", "2016-01-01",
        ]),
        "value": [1.0, 2.0, 3.0, 4.0],
        "accession": ["a", "b", "c", "d"],
    })
    conflicts = data_audit.quarterly_value_conflicts(frame)

    assert len(conflicts) == 2
    assert daily_pipeline.selected_quarterly_conflict_tickers(
        ["BAD", "OLD"],
        {"quarterly_value_conflicts": conflicts},
        "2026-06-30",
    ) == ["BAD"]


def test_quarterly_conflict_order_sensitivity_reports_metric_not_gate_change():
    fiscal_ends = pd.date_range("2020-03-31", periods=8, freq="QE")
    rows = []
    for index, fiscal_end in enumerate(fiscal_ends, start=1):
        available_date = fiscal_end + pd.Timedelta(days=45)
        rows.extend([
            {
                "ticker": "BAD",
                "fiscal_end": fiscal_end,
                "metric": "net_income",
                "available_date": available_date,
                "value": float(100 - index),
                "accession": f"profit-{index}",
            },
            {
                "ticker": "BAD",
                "fiscal_end": fiscal_end,
                "metric": "revenue",
                "available_date": available_date,
                "value": float(1_000 + 100 * index),
                "accession": f"revenue-{index}",
            },
        ])
    rows.append({
        **rows[-2],
        "value": rows[-2]["value"] - 1,
        "accession": "profit-conflict",
    })
    result = data_audit.quarterly_conflict_order_sensitivity(
        pd.DataFrame(rows),
        pd.to_datetime(["2022-02-28"]),
    )

    assert result["analyzable"] is True
    assert result["affected_signal_count"] == 1
    assert result["affected_tickers"] == ["BAD"]
    assert result[
        "financial_eligibility_changed_ticker_signal_count"
    ] == 0
    assert set(result["details"][0]["differences"]) == {
        "net_income_ttm",
        "net_income_growth",
    }


@pytest.fixture(autouse=True)
def _quarterly_fundamentals_file(tmp_path, monkeypatch):
    path = tmp_path / "quarterly_fundamentals.csv"
    fiscal_ends = pd.to_datetime([
        "2024-03-31",
        "2024-06-30",
        "2024-09-30",
        "2024-12-31",
        "2025-03-31",
        "2025-06-30",
        "2025-09-30",
        "2025-12-31",
    ])
    rows = []
    for index, fiscal_end in enumerate(fiscal_ends, start=1):
        for metric, multiplier in (("net_income", 10), ("revenue", 100)):
            rows.append({
                "ticker": "ABC",
                "fiscal_end": fiscal_end,
                "available_date": fiscal_end + pd.Timedelta(days=45),
                "metric": metric,
                "value": index * multiplier,
                "fetched_at": fiscal_end + pd.Timedelta(days=45),
            })
    _write_csv(
        path,
        pd.DataFrame(rows),
    )
    monkeypatch.setattr(
        data_audit,
        "POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE",
        str(path),
    )


@pytest.mark.parametrize(
    ("as_of", "benchmark_date", "expected_session", "passes"),
    [
        (date(2026, 8, 2), "2026-07-31", "2026-07-31", True),
        (date(2026, 7, 4), "2026-07-02", "2026-07-02", True),
        (date(2026, 7, 29), "2026-07-28", "2026-07-29", False),
    ],
)
def test_data_audit_uses_latest_completed_nasdaq_session(
    tmp_path,
    monkeypatch,
    as_of,
    benchmark_date,
    expected_session,
    passes,
):
    universe_path = tmp_path / "universe.csv"
    index_path = tmp_path / "index.csv"
    eps_path = tmp_path / "eps.csv"
    price_dir = tmp_path / "prices"
    _write_csv(
        universe_path,
        pd.DataFrame({"Symbol": ["ABC"], "Name": ["ABC Common Stock"]}),
    )
    _write_csv(
        index_path,
        pd.DataFrame({"date": [benchmark_date], "close": [100.0]}),
    )
    _write_csv(
        price_dir / "abc.csv",
        pd.DataFrame({
            "date": [benchmark_date],
            "close": [20.0],
            "volume": [1_000_000],
        }),
    )
    _write_csv(
        eps_path,
        pd.DataFrame({
            "ticker": ["ABC"],
            "period_end": ["2026-03-31"],
            "available_date": ["2026-05-01"],
            "quarterly_eps": [1.0],
            "source": ["sec_companyfacts"],
            "fetched_at": ["2026-05-01"],
            "exact_report_date": [True],
        }),
    )
    monkeypatch.setattr(
        data_audit, "NASDAQ_300M_STOCK_LIST_FILE", str(universe_path)
    )
    monkeypatch.setattr(data_audit, "NASDAQ_INDEX_FILE", str(index_path))
    monkeypatch.setattr(
        data_audit, "POINT_IN_TIME_EPS_FILE", str(eps_path)
    )
    monkeypatch.setattr(
        data_audit, "CLEANED_PRICE_DATA_DIR", str(price_dir)
    )
    monkeypatch.setattr(data_audit, "PROJECT_PATH", str(tmp_path))

    report = data_audit.audit_project_data(
        as_of, strategy_market_moving_average_sessions=1
    )

    assert report["checks"]["benchmark_fresh"] is passes
    assert report["expected_latest_benchmark_session"] == expected_session
    assert report["status"] == ("PASS" if passes else "FAIL")


def test_data_audit_rejects_duplicate_and_non_session_price_dates(
    tmp_path, monkeypatch
):
    universe_path = tmp_path / "universe.csv"
    index_path = tmp_path / "index.csv"
    eps_path = tmp_path / "eps.csv"
    price_dir = tmp_path / "prices"
    _write_csv(
        universe_path,
        pd.DataFrame({
            "Symbol": ["ABC", "BAD"],
            "Name": ["ABC Common Stock", "BAD Common Stock"],
        }),
    )
    _write_csv(
        index_path,
        pd.DataFrame({
            "date": ["2026-07-25", "2026-07-29", "2026-07-29"],
            "close": [0.0, 100.0, 100.0],
        }),
    )
    _write_csv(
        price_dir / "abc.csv",
        pd.DataFrame({
            "date": ["2026-07-25", "2026-07-29", "2026-07-29"],
            "close": [0.0, 20.0, 20.0],
            "volume": [-1, 1_000_000, 1_000_000],
        }),
    )
    _write_csv(
        price_dir / "bad.csv",
        pd.DataFrame({"date": ["2026-07-29"], "close": [20.0]}),
    )
    _write_csv(
        eps_path,
        pd.DataFrame({
            "ticker": ["ABC"],
            "period_end": ["2026-03-31"],
            "available_date": ["2026-05-01"],
            "quarterly_eps": [1.0],
            "source": ["sec_companyfacts"],
            "fetched_at": ["2026-05-01"],
            "exact_report_date": [True],
        }),
    )
    monkeypatch.setattr(
        data_audit, "NASDAQ_300M_STOCK_LIST_FILE", str(universe_path)
    )
    monkeypatch.setattr(data_audit, "NASDAQ_INDEX_FILE", str(index_path))
    monkeypatch.setattr(
        data_audit, "POINT_IN_TIME_EPS_FILE", str(eps_path)
    )
    monkeypatch.setattr(
        data_audit, "CLEANED_PRICE_DATA_DIR", str(price_dir)
    )
    monkeypatch.setattr(data_audit, "PROJECT_PATH", str(tmp_path))

    report = data_audit.audit_project_data(
        date(2026, 7, 29),
        strategy_market_moving_average_sessions=3,
    )

    assert report["checks"]["no_duplicate_price_dates"] is False
    assert report["checks"]["price_dates_on_nasdaq_sessions"] is False
    assert report["checks"]["price_schema_complete"] is False
    assert report["checks"]["price_values_valid"] is False
    assert report["checks"]["benchmark_dates_unique"] is False
    assert report["checks"][
        "benchmark_dates_on_nasdaq_sessions"
    ] is False
    assert report["checks"]["benchmark_close_valid"] is False
    assert report["benchmark_duplicate_dates"] == ["2026-07-29"]
    assert report["benchmark_non_session_dates"] == ["2026-07-25"]
    assert report["benchmark_invalid_close_rows"] == 1
    assert report["checks"][
        "benchmark_recent_history_complete"
    ] is False
    assert report["benchmark_recent_missing_sessions"] == [
        "2026-07-27",
        "2026-07-28",
    ]
    assert report["duplicate_price_dates"] == [{
        "ticker": "ABC",
        "dates": ["2026-07-29"],
    }]
    assert report["non_session_price_rows"] == [{
        "ticker": "ABC",
        "dates": ["2026-07-25"],
    }]
    assert report["invalid_price_schema"][0]["ticker"] == "BAD"
    assert report["invalid_price_values"] == [{
        "ticker": "ABC",
        "invalid_row_count": 1,
        "invalid_date_count": 0,
        "invalid_close_count": 1,
        "invalid_nonempty_volume_count": 1,
    }]
    assert report["status"] == "FAIL"


def test_data_audit_rejects_stale_addressable_quarterly_fundamentals(
    tmp_path, monkeypatch
):
    universe_path = tmp_path / "universe.csv"
    index_path = tmp_path / "index.csv"
    eps_path = tmp_path / "eps.csv"
    price_dir = tmp_path / "prices"
    _write_csv(
        universe_path,
        pd.DataFrame({"Symbol": ["ABC"], "Name": ["ABC Common Stock"]}),
    )
    _write_csv(
        index_path,
        pd.DataFrame({"date": ["2026-07-29"], "close": [100.0]}),
    )
    _write_csv(
        price_dir / "abc.csv",
        pd.DataFrame({
            "date": ["2026-07-29"],
            "close": [20.0],
            "volume": [1_000_000],
        }),
    )
    _write_csv(
        eps_path,
        pd.DataFrame({
            "ticker": ["ABC"],
            "period_end": ["2026-03-31"],
            "available_date": ["2026-05-01"],
            "quarterly_eps": [1.0],
            "source": ["sec_companyfacts"],
            "fetched_at": ["2026-05-01"],
            "exact_report_date": [True],
        }),
    )
    quarterly_path = Path(
        data_audit.POINT_IN_TIME_QUARTERLY_FUNDAMENTALS_FILE
    )
    _write_csv(
        quarterly_path,
        pd.DataFrame({
            "ticker": ["ABC", "ABC"],
            "fiscal_end": ["2024-03-31", "2024-03-31"],
            "available_date": ["2024-05-01", "2024-05-01"],
            "metric": ["net_income", "revenue"],
            "value": [10.0, 100.0],
            "fetched_at": ["2024-05-01", "2024-05-01"],
        }),
    )
    monkeypatch.setattr(
        data_audit, "NASDAQ_300M_STOCK_LIST_FILE", str(universe_path)
    )
    monkeypatch.setattr(data_audit, "NASDAQ_INDEX_FILE", str(index_path))
    monkeypatch.setattr(
        data_audit, "POINT_IN_TIME_EPS_FILE", str(eps_path)
    )
    monkeypatch.setattr(
        data_audit, "CLEANED_PRICE_DATA_DIR", str(price_dir)
    )
    monkeypatch.setattr(data_audit, "PROJECT_PATH", str(tmp_path))

    report = data_audit.audit_project_data(
        date(2026, 7, 29),
        strategy_market_moving_average_sessions=1,
    )

    assert not report["checks"]["quarterly_fundamentals_coverage"]
    assert report["quarterly_fundamentals_addressable_tickers"] == 1
    assert report["quarterly_fundamentals_coverage"] == 0
    assert report["quarterly_fundamentals_coverage_basis"] == (
        "fresh_among_formal_growth_usable"
    )
    assert (
        report["quarterly_fundamentals_fresh_universe_coverage"]
        == 0
    )
    assert (
        report["quarterly_fundamentals_fresh_addressable_coverage"]
        == 0
    )
    assert report["status"] == "FAIL"


def test_data_audit_fails_when_financials_are_stale(tmp_path, monkeypatch):
    universe_path = tmp_path / "universe.csv"
    index_path = tmp_path / "index.csv"
    eps_path = tmp_path / "eps.csv"
    price_dir = tmp_path / "prices"
    _write_csv(universe_path, pd.DataFrame({"Symbol": ["ABC"], "Name": ["ABC Common Stock"]}))
    _write_csv(
        index_path,
        pd.DataFrame({"date": ["2026-07-17"], "close": [100.0]}),
    )
    _write_csv(
        price_dir / "abc.csv",
        pd.DataFrame({
            "date": ["2026-07-17"],
            "close": [10.0],
            "volume": [1_000_000],
        }),
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

    report = data_audit.audit_project_data(
        date(2026, 7, 17),
        strategy_market_moving_average_sessions=1,
    )

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
    _write_csv(
        index_path,
        pd.DataFrame({"date": ["2026-06-01"], "close": [100.0]}),
    )
    _write_csv(price_dir / "abc.csv", pd.DataFrame({
        "date": ["2026-06-01"],
        "close": [10.0],
        "volume": [1_000_000],
    }))
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

    report = data_audit.audit_project_data(
        date(2026, 7, 17),
        strategy_market_moving_average_sessions=1,
    )
    assert report["checks"]["price_coverage"]
    assert not report["checks"]["benchmark_fresh"]
    assert report["status"] == "FAIL"


def test_data_audit_blocks_a_liquid_strategy_candidate_with_stale_price(
    tmp_path, monkeypatch
):
    universe_path = tmp_path / "universe.csv"
    index_path = tmp_path / "index.csv"
    eps_path = tmp_path / "eps.csv"
    price_dir = tmp_path / "prices"
    symbols = [f"S{i:02d}" for i in range(20)]
    _write_csv(universe_path, pd.DataFrame({
        "Symbol": symbols,
        "Name": [f"{symbol} Common Stock" for symbol in symbols],
    }))
    _write_csv(
        index_path,
        pd.DataFrame({"date": ["2026-07-17"], "close": [100.0]}),
    )
    for symbol in symbols[:-1]:
        _write_csv(
            price_dir / f"{symbol.lower()}.csv",
            pd.DataFrame({
                "date": ["2026-07-17"],
                "close": [20.0],
                "volume": [1_000_000],
            }),
        )
    stale_dates = pd.bdate_range(end="2026-07-16", periods=253)
    _write_csv(
        price_dir / f"{symbols[-1].lower()}.csv",
        pd.DataFrame({
            "date": stale_dates,
            "close": 20.0,
            "volume": 1_000_000,
        }),
    )
    _write_csv(eps_path, pd.DataFrame({
        "ticker": [symbols[0]],
        "period_end": ["2026-03-31"],
        "available_date": ["2026-05-01"],
        "quarterly_eps": [1.0],
        "source": ["sec_companyfacts"],
        "fetched_at": ["2026-05-01"],
        "exact_report_date": [True],
    }))
    monkeypatch.setattr(
        data_audit, "NASDAQ_300M_STOCK_LIST_FILE", str(universe_path)
    )
    monkeypatch.setattr(data_audit, "NASDAQ_INDEX_FILE", str(index_path))
    monkeypatch.setattr(
        data_audit, "POINT_IN_TIME_EPS_FILE", str(eps_path)
    )
    monkeypatch.setattr(
        data_audit, "CLEANED_PRICE_DATA_DIR", str(price_dir)
    )
    monkeypatch.setattr(data_audit, "PROJECT_PATH", str(tmp_path))

    report = data_audit.audit_project_data(
        date(2026, 7, 17),
        minimum_financial_coverage=0,
        strategy_market_moving_average_sessions=1,
    )

    assert report["checks"]["price_coverage"]
    assert not report["checks"]["no_material_missing_strategy_prices"]
    assert report["material_missing_strategy_prices"][0]["ticker"] == "S19"
    assert report["status"] == "FAIL"


def test_daily_pipeline_does_not_recommend_after_failed_data_gate(
    tmp_path, monkeypatch
):
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
    monkeypatch.setattr(
        "sys.argv",
        [
            "daily_pipeline",
            "--skip-update",
            "--output-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(RuntimeError, match="financial_coverage"):
        daily_pipeline.main()

    assert calls == ["audit"]
    status = pd.read_json(
        tmp_path
        / "can-slim-top3-v1"
        / "pipeline_status.json",
        typ="series",
    )
    assert status["status"] == "FAIL"
    assert status["error_type"] == "RuntimeError"
    assert "financial_coverage" in status["error"]


def test_daily_pipeline_does_not_recommend_after_invalid_validation_manifest(
    tmp_path,
    monkeypatch,
):
    calls = []

    monkeypatch.setattr(
        daily_pipeline,
        "require_project_data",
        lambda _as_of: calls.append("audit") or {},
    )

    def reject_manifest(_output_dir):
        calls.append("manifest")
        raise RuntimeError("Validation artifact integrity mismatch")

    monkeypatch.setattr(
        daily_pipeline,
        "verify_validation_artifact_manifest",
        reject_manifest,
    )
    monkeypatch.setattr(
        daily_pipeline,
        "generate_can_slim_shadow_recommendations",
        lambda: calls.append("recommend") or (pd.DataFrame(), {}),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "daily_pipeline",
            "--skip-update",
            "--output-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(RuntimeError, match="integrity mismatch"):
        daily_pipeline.main()

    assert calls == ["audit", "manifest"]
    status = pd.read_json(
        tmp_path
        / "can-slim-top3-v1"
        / "pipeline_status.json",
        typ="series",
    )
    assert status["status"] == "FAIL"
    assert status["error_type"] == "RuntimeError"
    assert "integrity mismatch" in status["error"]


def test_daily_pipeline_compacts_update_failures_for_artifacts():
    result = daily_pipeline.compact_update_report({
        "end": "2026-07-30",
        "counts": {"updated": 10, "failed": 2},
        "failures": [
            {"ticker": "AAA", "error": "timeout"},
            {"ticker": "BBB", "error": "timeout"},
        ],
    })

    assert result == {
        "end": "2026-07-30",
        "counts": {"updated": 10, "failed": 2},
        "failure_count": 2,
        "failure_tickers": ["AAA", "BBB"],
    }


def test_daily_pipeline_accepts_explicit_reproducible_as_of(
    tmp_path,
    monkeypatch,
):
    observed = []
    monkeypatch.setattr(
        daily_pipeline,
        "run_pipeline",
        lambda _args, as_of: observed.append(as_of),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "daily_pipeline",
            "--skip-update",
            "--as-of",
            "2026-07-24",
            "--output-dir",
            str(tmp_path),
        ],
    )

    daily_pipeline.main()

    assert observed == [date(2026, 7, 24)]


def test_daily_pipeline_uses_selected_output_ledger_for_freeze_reuse(
    tmp_path,
    monkeypatch,
):
    captured = {}
    monkeypatch.setattr(
        daily_pipeline,
        "require_project_data",
        lambda _as_of: {},
    )
    monkeypatch.setattr(
        daily_pipeline,
        "verify_validation_artifact_manifest",
        lambda _output_dir: {"verified": True},
    )

    def capture_generate(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("captured")

    monkeypatch.setattr(
        daily_pipeline,
        "generate_can_slim_shadow_recommendations",
        capture_generate,
    )
    args = SimpleNamespace(
        skip_update=True,
        skip_market_update=False,
        skip_financial_update=False,
        workers=1,
        output_dir=str(tmp_path),
    )

    with pytest.raises(RuntimeError, match="captured"):
        daily_pipeline.run_pipeline(args, date(2026, 7, 29))

    assert captured["history_file"] == (
        tmp_path
        / "can-slim-top3-v1"
        / "recommendation_history.csv"
    )


def test_daily_pipeline_refuses_unsealed_existing_shadow_ledger(
    tmp_path,
    monkeypatch,
):
    history = (
        tmp_path
        / "can-slim-top3-v1"
        / "recommendation_history.csv"
    )
    history.parent.mkdir(parents=True)
    history.write_text(
        "ticker,target_weight\n__CASH__,0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        daily_pipeline,
        "require_project_data",
        lambda _as_of: {},
    )
    monkeypatch.setattr(
        daily_pipeline,
        "verify_validation_artifact_manifest",
        lambda _output_dir: {"verified": True},
    )
    monkeypatch.setattr(
        daily_pipeline,
        "generate_can_slim_shadow_recommendations",
        lambda **_kwargs: pytest.fail("recommendation should not run"),
    )
    args = SimpleNamespace(
        skip_update=True,
        skip_market_update=False,
        skip_financial_update=False,
        workers=1,
        output_dir=str(tmp_path),
    )

    with pytest.raises(RuntimeError, match="MISSING_MANIFEST"):
        daily_pipeline.run_pipeline(args, date(2026, 7, 29))


def test_daily_pipeline_allows_integral_local_shadow_ledger(tmp_path):
    history = tmp_path / "recommendation_history.csv"
    history.write_text(
        "ticker,target_weight\n__CASH__,0\n",
        encoding="utf-8",
    )
    write_shadow_ledger_manifest(history, environment={})

    result = daily_pipeline.require_reusable_shadow_ledger(history)

    assert result["integrity_verified"] is True
    assert result["externally_anchored"] is False


def test_daily_pipeline_refuses_local_reuse_of_externally_anchored_ledger(
    tmp_path,
):
    history = tmp_path / "recommendation_history.csv"
    history.write_text(
        "ticker,target_weight\n__CASH__,0\n",
        encoding="utf-8",
    )
    write_shadow_ledger_manifest(
        history,
        environment={
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "example/research",
            "GITHUB_WORKFLOW": "shadow",
            "GITHUB_RUN_ID": "100",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_SERVER_URL": "https://github.com",
            "SHADOW_DEFAULT_BRANCH": "main",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_EVENT_NAME": "schedule",
        },
    )

    with pytest.raises(
        RuntimeError,
        match="only be extended by a verified GitHub Actions run",
    ):
        daily_pipeline.require_reusable_shadow_ledger(
            history,
            environment={},
        )


def test_daily_pipeline_refuses_cross_repository_shadow_ledger_reuse(
    tmp_path,
):
    history = tmp_path / "recommendation_history.csv"
    history.write_text(
        "ticker,target_weight\n__CASH__,0\n",
        encoding="utf-8",
    )
    github_environment = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "example/research",
        "GITHUB_WORKFLOW": "shadow",
        "GITHUB_RUN_ID": "100",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SERVER_URL": "https://github.com",
        "SHADOW_DEFAULT_BRANCH": "main",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_EVENT_NAME": "schedule",
    }
    write_shadow_ledger_manifest(history, environment=github_environment)

    with pytest.raises(
        RuntimeError,
        match="canonical repository workflow",
    ):
        daily_pipeline.require_reusable_shadow_ledger(
            history,
            environment={
                **github_environment,
                "GITHUB_REPOSITORY": "other/research",
                "GITHUB_RUN_ID": "101",
                "SHADOW_PREVIOUS_ARTIFACT_ID": "200",
            },
        )


def test_daily_pipeline_logs_compact_provenance_without_losing_artifact_data():
    recommendations = pd.DataFrame([{
        "as_of": "2026-07-29",
        "ticker": "AAPL",
        "target_weight": 1 / 3,
        "portfolio_source_kind": "github_actions_run",
        "portfolio_data_components_json": "very-large-json",
        "portfolio_strategy_sha256": "a" * 64,
    }])
    metadata = {
        "as_of": "2026-07-29",
        "model_version": "can-slim-top3-v1",
        "input_fingerprints": {
            "strategy_code": {"sha256": "a" * 64},
            "data_manifest": {"sha256": "b" * 64},
        },
        "pipeline_data_status": {"requested_as_of": "2026-07-29"},
    }

    compact_rows = daily_pipeline.compact_recommendations_for_log(
        recommendations
    )
    compact_metadata = daily_pipeline.compact_metadata_for_log(metadata)

    assert "portfolio_data_components_json" not in compact_rows
    assert "portfolio_strategy_sha256" not in compact_rows
    assert recommendations.loc[
        0, "portfolio_data_components_json"
    ] == "very-large-json"
    assert "input_fingerprints" not in compact_metadata
    assert compact_metadata["strategy_sha256"] == "a" * 64
    assert compact_metadata["data_manifest_sha256"] == "b" * 64


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
