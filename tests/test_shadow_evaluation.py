import json

import pandas as pd
import pytest

from src.research import shadow_evaluation
from src.research.data_fingerprint import (
    CAN_SLIM_DATA_COMPONENTS,
    data_manifest_sha256_from_components,
)
from src.research.shadow_evaluation import (
    evaluate_forward_account,
    evaluate_history,
    evaluate_recorded_portfolio,
    execution_close_utc,
    recorded_signal_provenance,
    validate_shadow_history,
)
from src.research.shadow_ledger import write_shadow_ledger_manifest


def _records(generated_at):
    return pd.DataFrame({
        "ticker": ["A", "B"],
        "target_weight": [0.5, 0.5],
        "signal_date": ["2026-06-30", "2026-06-30"],
        "execution_date": ["2026-07-01", "2026-07-01"],
        "generated_at": [generated_at, generated_at],
    })


def test_shadow_history_rejects_mixed_model_versions():
    history = _records("2026-07-01T12:00:00Z")
    history["model_version"] = ["model-a", "model-b"]

    with pytest.raises(ValueError, match="exactly one.*model_version"):
        validate_shadow_history(history)


def test_shadow_history_rejects_conflicting_execution_dates():
    history = _records("2026-07-01T12:00:00Z")
    history["model_version"] = "model-a"
    history["execution_date"] = ["2026-07-01", "2026-07-02"]

    with pytest.raises(ValueError, match="conflicting execution dates"):
        validate_shadow_history(history)


@pytest.mark.parametrize("invalid_signal_date", [None, "not-a-date"])
def test_shadow_history_rejects_invalid_signal_dates(invalid_signal_date):
    history = _records("2026-07-01T12:00:00Z")
    history["model_version"] = "model-a"
    history.loc[1, "signal_date"] = invalid_signal_date

    with pytest.raises(ValueError, match="invalid signal_date"):
        validate_shadow_history(history)


def test_recorded_portfolio_rejects_duplicate_tickers():
    records = _records("2026-07-01T12:00:00Z")
    records["ticker"] = ["A", "A"]

    with pytest.raises(ValueError, match="duplicate tickers"):
        evaluate_recorded_portfolio(
            records,
            pd.DataFrame(),
            pd.Series(dtype=float),
        )


def test_recorded_portfolio_rejects_leveraged_weights():
    records = _records("2026-07-01T12:00:00Z")
    records["target_weight"] = [0.6, 0.5]

    with pytest.raises(ValueError, match="sum to at most 1"):
        evaluate_recorded_portfolio(
            records,
            pd.DataFrame(),
            pd.Series(dtype=float),
        )


@pytest.mark.parametrize("cost", [-1.0, float("inf"), float("nan")])
def test_shadow_evaluation_rejects_invalid_transaction_cost(cost):
    with pytest.raises(
        ValueError,
        match="finite and non-negative",
    ):
        evaluate_recorded_portfolio(
            _records("2026-07-01T01:00:00Z"),
            pd.DataFrame(),
            pd.Series(dtype=float),
            transaction_cost_bps=cost,
        )


@pytest.mark.parametrize(
    "tickers,weights,error",
    [
        (
            ["A", "B", "C", "D"],
            [0.25] * 4,
            "more than 3 stocks",
        ),
        (
            ["A", "B"],
            [0.5, 0.5],
            "must each equal 1/3",
        ),
        (
            ["A", "__CASH__"],
            [1 / 3, 0.0],
            "cannot include the cash sentinel",
        ),
        (
            ["A"],
            [0.0],
            "zero-weight stock rows",
        ),
    ],
)
def test_fixed_top3_rejects_noncanonical_targets(
    tickers,
    weights,
    error,
):
    records = pd.DataFrame({
        "ticker": tickers,
        "target_weight": weights,
        "signal_date": ["2026-06-30"] * len(tickers),
        "execution_date": ["2026-07-01"] * len(tickers),
        "generated_at": ["2026-07-01T01:00:00Z"] * len(tickers),
    })

    with pytest.raises(ValueError, match=error):
        evaluate_recorded_portfolio(
            records,
            pd.DataFrame(),
            pd.Series(dtype=float),
            model_version="can-slim-top3-v1",
        )


@pytest.mark.parametrize("count", [1, 2, 3])
def test_fixed_top3_allows_fewer_than_three_equal_slots(count):
    tickers = [chr(ord("A") + index) for index in range(count)]
    records = pd.DataFrame({
        "ticker": tickers,
        "target_weight": [1 / 3] * count,
        "signal_date": ["2026-06-30"] * count,
        "execution_date": ["2026-07-01"] * count,
        "generated_at": ["2026-07-01T01:00:00Z"] * count,
    })
    idx = pd.to_datetime(["2026-07-01", "2026-07-02"])
    close = pd.DataFrame(
        {ticker: [10, 11] for ticker in tickers},
        index=idx,
    )
    benchmark = pd.Series([100, 101], index=idx)

    result = evaluate_recorded_portfolio(
        records,
        close,
        benchmark,
        model_version="can-slim-top3-v1",
    )

    assert result["target_exposure"] == pytest.approx(count / 3)


def test_fixed_top3_allows_explicit_cash_sentinel():
    records = pd.DataFrame({
        "ticker": ["__CASH__"],
        "target_weight": [0.0],
        "signal_date": ["2026-06-30"],
        "execution_date": ["2026-07-01"],
        "generated_at": ["2026-07-01T01:00:00Z"],
    })
    idx = pd.to_datetime(["2026-07-01", "2026-07-02"])
    benchmark = pd.Series([100, 101], index=idx)

    result = evaluate_recorded_portfolio(
        records,
        pd.DataFrame(index=idx),
        benchmark,
        model_version="can-slim-top3-v1",
    )

    assert result["target_exposure"] == 0.0


def test_cash_sentinel_cannot_hide_nonzero_exposure():
    records = _records("2026-07-01T12:00:00Z").iloc[[0]].copy()
    records["ticker"] = "__CASH__"
    records["target_weight"] = 1.0

    with pytest.raises(ValueError, match="cash sentinel"):
        evaluate_recorded_portfolio(
            records,
            pd.DataFrame(),
            pd.Series(dtype=float),
        )


def test_signal_with_mixed_origin_runs_is_not_anchored():
    records = pd.DataFrame({
        "portfolio_source_kind": ["github_actions_run"] * 2,
        "portfolio_repository": ["owner/repository"] * 2,
        "portfolio_workflow": ["shadow"] * 2,
        "portfolio_run_id": ["123", "456"],
        "portfolio_run_attempt": ["1", "1"],
        "portfolio_run_url": [
            "https://github.com/owner/repository/actions/runs/123",
            "https://github.com/owner/repository/actions/runs/456",
        ],
        "portfolio_default_branch": ["master"] * 2,
        "portfolio_git_ref": ["refs/heads/master"] * 2,
        "portfolio_git_sha": ["a" * 40, "b" * 40],
        "portfolio_event_name": ["schedule"] * 2,
    })

    result = recorded_signal_provenance(records)

    assert result["status"] == "INCONSISTENT_SOURCE"
    assert result["externally_anchored"] is False


def test_signal_with_partially_missing_origin_is_not_anchored():
    records = pd.DataFrame({
        "portfolio_source_kind": ["github_actions_run"] * 2,
        "portfolio_repository": ["owner/repository", None],
        "portfolio_workflow": ["shadow"] * 2,
        "portfolio_run_id": ["123"] * 2,
        "portfolio_run_attempt": ["1"] * 2,
        "portfolio_run_url": [
            "https://github.com/owner/repository/actions/runs/123"
        ] * 2,
        "portfolio_default_branch": ["master"] * 2,
        "portfolio_git_ref": ["refs/heads/master"] * 2,
        "portfolio_git_sha": ["a" * 40] * 2,
        "portfolio_event_name": ["schedule"] * 2,
    })

    result = recorded_signal_provenance(records)

    assert result["status"] == "INCOMPLETE_SOURCE"
    assert result["externally_anchored"] is False
    assert result["incomplete_column"] == "portfolio_repository"
    assert result["missing_value_count"] == 1


def test_signal_source_must_belong_to_ledger_chain():
    records = pd.DataFrame({
        "portfolio_source_kind": ["github_actions_run"],
        "portfolio_repository": ["owner/repository"],
        "portfolio_workflow": ["shadow"],
        "portfolio_run_id": ["123"],
        "portfolio_run_attempt": ["1"],
        "portfolio_run_url": [
            "https://github.com/owner/repository/actions/runs/123"
        ],
        "portfolio_default_branch": ["master"],
        "portfolio_git_ref": ["refs/heads/master"],
        "portfolio_git_sha": ["a" * 40],
        "portfolio_event_name": ["schedule"],
    })
    trusted = [{
        "kind": "github_actions_run",
        "repository": "owner/repository",
        "workflow": "shadow",
        "run_id": "456",
        "run_attempt": "1",
        "run_url": (
            "https://github.com/owner/repository/actions/runs/456"
        ),
        "default_branch": "master",
        "git_ref": "refs/heads/master",
        "git_sha": "b" * 40,
        "event_name": "schedule",
    }]

    result = recorded_signal_provenance(
        records,
        trusted_sources=trusted,
    )

    assert result["status"] == "SOURCE_NOT_IN_LEDGER_CHAIN"
    assert result["externally_anchored"] is False


def test_signal_strategy_fingerprint_must_match_frozen_summary():
    records = pd.DataFrame({
        "portfolio_source_kind": ["github_actions_run"],
        "portfolio_repository": ["owner/repository"],
        "portfolio_workflow": ["shadow"],
        "portfolio_run_id": ["123"],
        "portfolio_run_attempt": ["1"],
        "portfolio_run_url": [
            "https://github.com/owner/repository/actions/runs/123"
        ],
        "portfolio_default_branch": ["master"],
        "portfolio_git_ref": ["refs/heads/master"],
        "portfolio_git_sha": ["a" * 40],
        "portfolio_event_name": ["schedule"],
        "portfolio_strategy_sha256": ["b" * 64],
    })

    result = recorded_signal_provenance(
        records,
        expected_strategy_sha256="a" * 64,
    )

    assert result["status"] == "STRATEGY_FINGERPRINT_MISMATCH"
    assert result["externally_anchored"] is False


def test_signal_data_manifest_must_match_its_components():
    components = {
        name: f"{index:064x}"
        for index, name in enumerate(
            CAN_SLIM_DATA_COMPONENTS,
            start=1,
        )
    }
    records = pd.DataFrame({
        "portfolio_source_kind": ["github_actions_run"],
        "portfolio_repository": ["owner/repository"],
        "portfolio_workflow": ["shadow"],
        "portfolio_run_id": ["123"],
        "portfolio_run_attempt": ["1"],
        "portfolio_run_url": [
            "https://github.com/owner/repository/actions/runs/123"
        ],
        "portfolio_default_branch": ["master"],
        "portfolio_git_ref": ["refs/heads/master"],
        "portfolio_git_sha": ["a" * 40],
        "portfolio_event_name": ["schedule"],
        "portfolio_strategy_sha256": ["a" * 64],
        "portfolio_data_manifest_sha256": ["b" * 64],
        "portfolio_data_components_json": [
            json.dumps(
                components,
                sort_keys=True,
                separators=(",", ":"),
            )
        ],
    })

    result = recorded_signal_provenance(
        records,
        expected_strategy_sha256="a" * 64,
    )

    assert result["status"] == "DATA_FINGERPRINT_INTEGRITY_MISMATCH"
    assert result["externally_anchored"] is False

    records["portfolio_data_manifest_sha256"] = (
        data_manifest_sha256_from_components(components)
    )
    valid = recorded_signal_provenance(
        records,
        expected_strategy_sha256="a" * 64,
    )
    assert valid["status"] == "VERIFIED_GITHUB_ACTIONS"
    assert valid["externally_anchored"] is True


def test_recorded_portfolio_propagates_ledger_chain_trust():
    records = _records("2026-07-01T01:00:00Z")
    records["portfolio_source_kind"] = "github_actions_run"
    records["portfolio_repository"] = "owner/repository"
    records["portfolio_workflow"] = "shadow"
    records["portfolio_run_id"] = "123"
    records["portfolio_run_attempt"] = "1"
    records["portfolio_run_url"] = (
        "https://github.com/owner/repository/actions/runs/123"
    )
    records["portfolio_default_branch"] = "master"
    records["portfolio_git_ref"] = "refs/heads/master"
    records["portfolio_git_sha"] = "a" * 40
    records["portfolio_event_name"] = "schedule"
    idx = pd.to_datetime(["2026-07-01", "2026-07-02"])
    close = pd.DataFrame({"A": [10, 11], "B": [20, 22]}, index=idx)
    benchmark = pd.Series([100, 105], index=idx)

    result = evaluate_recorded_portfolio(
        records,
        close,
        benchmark,
        trusted_sources=[],
    )

    assert result["signal_provenance"]["status"] == (
        "SOURCE_NOT_IN_LEDGER_CHAIN"
    )
    assert result["signal_provenance"]["externally_anchored"] is False


def test_real_nasdaq_cash_ticker_is_evaluated_as_a_stock():
    records = pd.DataFrame({
        "signal_date": ["2026-06-30"],
        "execution_date": ["2026-07-01"],
        "generated_at": ["2026-07-01T12:00:00Z"],
        "ticker": ["CASH"],
        "target_weight": [1.0],
    })
    dates = pd.to_datetime(["2026-07-01", "2026-07-02"])
    close = pd.DataFrame({"CASH": [10.0, 11.0]}, index=dates)
    benchmark = pd.Series([100.0, 100.0], index=dates)

    result = evaluate_recorded_portfolio(
        records, close, benchmark, transaction_cost_bps=10
    )

    assert result["strategy_return"] > 0.09
    assert result["forward_sessions"] == 1


def test_legacy_zero_weight_cash_row_remains_cash_reserve():
    records = pd.DataFrame({
        "signal_date": ["2026-06-30"],
        "execution_date": ["2026-07-01"],
        "generated_at": ["2026-07-01T12:00:00Z"],
        "ticker": ["CASH"],
        "target_weight": [0.0],
    })
    dates = pd.to_datetime(["2026-07-01", "2026-07-02"])
    close = pd.DataFrame(index=dates)
    benchmark = pd.Series([100.0, 100.0], index=dates)

    result = evaluate_recorded_portfolio(
        records, close, benchmark, transaction_cost_bps=10
    )

    assert result["strategy_return"] == pytest.approx(0.0)
    assert result["transaction_cost"] == pytest.approx(0.0)


def test_missing_history_is_a_valid_zero_position_state(tmp_path):
    output = tmp_path / "shadow.json"

    result = evaluate_history(tmp_path / "missing.csv", output)

    assert result["status"] == "NO_RECORDED_POSITIONS"
    assert result["forward_sessions"] == 0
    assert output.exists()


def test_header_only_legacy_history_is_a_valid_zero_position_state(tmp_path):
    history = tmp_path / "history.csv"
    history.write_text(
        "as_of,ticker,signal_date,generated_at,target_weight\n",
        encoding="utf-8",
    )
    output = tmp_path / "shadow.json"

    result = evaluate_history(history, output)

    assert result["status"] == "NO_RECORDED_POSITIONS"
    assert result["recorded_periods"] == 0
    assert output.exists()


def test_recorded_cash_is_forward_evidence_against_nasdaq():
    records = pd.DataFrame({
        "ticker": ["CASH"],
        "target_weight": [0.0],
        "signal_date": ["2026-07-31"],
        "execution_date": ["2026-08-03"],
        "generated_at": ["2026-08-01T01:00:00Z"],
    })
    dates = pd.to_datetime(["2026-08-03", "2026-08-04"])
    close = pd.DataFrame(index=dates)
    benchmark = pd.Series([100.0, 102.0], index=dates)

    result = evaluate_recorded_portfolio(records, close, benchmark)

    assert result["forward_eligible"] is True
    assert result["forward_sessions"] == 1
    assert result["strategy_return"] == 0.0
    assert result["benchmark_return"] == pytest.approx(0.02)


def test_shadow_evaluation_counts_only_returns_after_execution_close():
    idx = pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03"])
    close = pd.DataFrame({"A": [10, 11, 12], "B": [20, 22, 24]}, index=idx)
    benchmark = pd.Series([100, 105, 110], index=idx)
    result = evaluate_recorded_portfolio(
        _records("2026-07-01T01:00:00Z"), close, benchmark, transaction_cost_bps=0
    )
    assert result["forward_eligible"]
    assert result["forward_sessions"] == 2
    assert result["strategy_return"] == pytest.approx(0.2)
    assert result["benchmark_return"] == pytest.approx(0.1)


def test_period_detail_uses_fixed_positions_not_daily_constant_weights():
    dates = pd.to_datetime([
        "2026-08-03", "2026-08-04", "2026-08-05"
    ])
    records = pd.DataFrame({
        "ticker": ["A", "B"],
        "target_weight": [0.5, 0.5],
        "signal_date": ["2026-07-31", "2026-07-31"],
        "execution_date": ["2026-08-03", "2026-08-03"],
        "generated_at": ["2026-08-01T01:00:00Z"] * 2,
    })
    close = pd.DataFrame({
        "A": [10.0, 20.0, 20.0],
        "B": [10.0, 10.0, 20.0],
    }, index=dates)
    benchmark = pd.Series([100.0, 100.0, 100.0], index=dates)

    result = evaluate_recorded_portfolio(
        records, close, benchmark, transaction_cost_bps=0
    )

    assert result["accounting_method"] == "standalone_fixed_positions"
    assert result["strategy_return"] == pytest.approx(1.0)


def test_backdated_seed_never_counts_as_forward_evidence():
    idx = pd.to_datetime(["2026-07-01", "2026-07-02"])
    close = pd.DataFrame({"A": [10, 11], "B": [20, 22]}, index=idx)
    benchmark = pd.Series([100, 105], index=idx)
    result = evaluate_recorded_portfolio(
        _records("2026-07-18T01:00:00Z"), close, benchmark
    )
    assert result["status"] == "RETROSPECTIVE_SEED"
    assert result["forward_sessions"] == 0
    assert result["observed_sessions"] == 1


def test_after_close_rerun_keeps_original_portfolio_eligibility():
    idx = pd.to_datetime(["2026-07-01", "2026-07-02"])
    close = pd.DataFrame({"A": [10, 11], "B": [20, 22]}, index=idx)
    benchmark = pd.Series([100, 105], index=idx)
    records = _records("2026-07-01T21:00:00Z")
    records["portfolio_generated_at"] = "2026-07-01T01:00:00Z"
    result = evaluate_recorded_portfolio(records, close, benchmark)
    assert result["forward_eligible"]
    assert result["portfolio_generated_at"] == "2026-07-01T01:00:00+00:00"


def test_latest_portfolio_row_must_precede_execution_close():
    idx = pd.to_datetime(["2026-07-01", "2026-07-02"])
    close = pd.DataFrame({"A": [10, 11], "B": [20, 22]}, index=idx)
    benchmark = pd.Series([100, 105], index=idx)
    records = _records("2026-07-01T19:00:00Z")
    records["portfolio_generated_at"] = [
        "2026-07-01T19:00:00Z",
        "2026-07-01T20:00:01Z",
    ]

    result = evaluate_recorded_portfolio(records, close, benchmark)

    assert result["forward_eligible"] is False
    assert result["status"] == "RETROSPECTIVE_SEED"
    assert result["portfolio_generated_at"] == (
        "2026-07-01T20:00:01+00:00"
    )


def test_missing_portfolio_row_timestamp_is_not_forward_evidence():
    idx = pd.to_datetime(["2026-07-01", "2026-07-02"])
    close = pd.DataFrame({"A": [10, 11], "B": [20, 22]}, index=idx)
    benchmark = pd.Series([100, 105], index=idx)
    records = _records("2026-07-01T19:00:00Z")
    records["portfolio_generated_at"] = [
        "2026-07-01T19:00:00Z",
        None,
    ]

    result = evaluate_recorded_portfolio(records, close, benchmark)

    assert result["forward_eligible"] is False
    assert result["portfolio_timestamp_complete"] is False
    assert result["portfolio_generated_at"] is None


@pytest.mark.parametrize(
    ("first_timestamp", "eligible"),
    [
        ("2026-06-30T19:59:59Z", False),
        ("2026-06-30T20:00:00Z", True),
        ("2026-06-30T20:00:01Z", True),
    ],
)
def test_all_rows_must_follow_completed_signal_close(
    first_timestamp,
    eligible,
):
    idx = pd.to_datetime(["2026-07-01", "2026-07-02"])
    close = pd.DataFrame({"A": [10, 11], "B": [20, 22]}, index=idx)
    benchmark = pd.Series([100, 105], index=idx)
    records = _records("2026-06-30T20:00:01Z")
    records["portfolio_generated_at"] = [
        first_timestamp,
        "2026-06-30T20:00:01Z",
    ]

    result = evaluate_recorded_portfolio(records, close, benchmark)

    assert result["forward_eligible"] is eligible
    assert result["portfolio_timestamp_window_valid"] is eligible
    assert result["signal_close_utc"] == "2026-06-30T20:00:00+00:00"


@pytest.mark.parametrize(
    ("generated_at", "eligible"),
    [
        ("2026-07-01T19:59:59Z", True),
        ("2026-07-01T20:00:00Z", False),
        ("2026-07-01T20:00:01Z", False),
    ],
)
def test_same_execution_day_uses_exact_dst_market_close(generated_at, eligible):
    idx = pd.to_datetime(["2026-07-01", "2026-07-02"])
    close = pd.DataFrame({"A": [10, 11], "B": [20, 22]}, index=idx)
    benchmark = pd.Series([100, 105], index=idx)
    result = evaluate_recorded_portfolio(_records(generated_at), close, benchmark)
    assert result["forward_eligible"] is eligible
    assert result["execution_close_utc"] == "2026-07-01T20:00:00+00:00"


@pytest.mark.parametrize(
    ("generated_at", "eligible"),
    [
        ("2026-12-01T20:59:59Z", True),
        ("2026-12-01T21:00:00Z", False),
    ],
)
def test_same_execution_day_uses_exact_standard_time_market_close(generated_at, eligible):
    records = _records(generated_at)
    records["signal_date"] = "2026-11-30"
    records["execution_date"] = "2026-12-01"
    idx = pd.to_datetime(["2026-12-01", "2026-12-02"])
    close = pd.DataFrame({"A": [10, 11], "B": [20, 22]}, index=idx)
    benchmark = pd.Series([100, 105], index=idx)
    result = evaluate_recorded_portfolio(records, close, benchmark)
    assert result["forward_eligible"] is eligible
    assert result["execution_close_utc"] == "2026-12-01T21:00:00+00:00"


@pytest.mark.parametrize(
    ("generated_at", "eligible"),
    [
        ("2026-11-27T17:59:59Z", True),
        ("2026-11-27T18:00:00Z", False),
        ("2026-11-27T20:00:00Z", False),
    ],
)
def test_same_execution_day_uses_official_early_close(generated_at, eligible):
    close_time = execution_close_utc(pd.Timestamp("2026-11-27"))
    generated = pd.Timestamp(generated_at)

    assert (generated < close_time) is eligible
    assert close_time.isoformat() == "2026-11-27T18:00:00+00:00"


def test_non_session_execution_date_is_rejected():
    records = _records("2026-07-03T12:00:00Z")
    records["execution_date"] = "2026-07-03"
    idx = pd.to_datetime(["2026-07-02", "2026-07-06"])
    close = pd.DataFrame({"A": [10, 11], "B": [20, 22]}, index=idx)
    benchmark = pd.Series([100, 105], index=idx)

    with pytest.raises(
        ValueError,
        match="not a Nasdaq trading session: 2026-07-03",
    ):
        evaluate_recorded_portfolio(records, close, benchmark)


@pytest.mark.parametrize(
    "execution_date",
    ["2026-06-30", "2026-07-02"],
)
def test_execution_must_be_first_session_after_signal(execution_date):
    records = _records("2026-06-30T21:00:00Z")
    records["execution_date"] = execution_date
    idx = pd.to_datetime(["2026-06-30", "2026-07-01", "2026-07-02"])
    close = pd.DataFrame(
        {"A": [9, 10, 11], "B": [19, 20, 21]},
        index=idx,
    )
    benchmark = pd.Series([99, 100, 101], index=idx)

    with pytest.raises(
        ValueError,
        match="first Nasdaq trading session.*expected 2026-07-01",
    ):
        evaluate_recorded_portfolio(records, close, benchmark)


def test_year_end_signal_executes_on_next_year_session():
    records = _records("2026-12-31T21:01:00Z")
    records["signal_date"] = "2026-12-31"
    records["execution_date"] = "2027-01-04"
    idx = pd.to_datetime(["2027-01-04", "2027-01-05"])
    close = pd.DataFrame({"A": [10, 11], "B": [20, 22]}, index=idx)
    benchmark = pd.Series([100, 105], index=idx)

    result = evaluate_recorded_portfolio(records, close, benchmark)

    assert result["forward_eligible"] is True


def test_midmonth_signal_cannot_count_as_monthly_evidence():
    records = _records("2026-07-15T21:00:00Z")
    records["signal_date"] = "2026-07-15"
    records["execution_date"] = "2026-07-16"
    idx = pd.to_datetime(["2026-07-16", "2026-07-17"])
    close = pd.DataFrame({"A": [10, 11], "B": [20, 22]}, index=idx)
    benchmark = pd.Series([100, 105], index=idx)

    with pytest.raises(
        ValueError,
        match="final Nasdaq trading session.*expected 2026-07-31",
    ):
        evaluate_recorded_portfolio(records, close, benchmark)


def test_weekend_month_end_uses_last_session_of_calendar_month():
    records = _records("2026-05-29T21:00:00Z")
    records["signal_date"] = "2026-05-29"
    records["execution_date"] = "2026-06-01"
    idx = pd.to_datetime(["2026-06-01", "2026-06-02"])
    close = pd.DataFrame({"A": [10, 11], "B": [20, 22]}, index=idx)
    benchmark = pd.Series([100, 105], index=idx)

    result = evaluate_recorded_portfolio(records, close, benchmark)

    assert result["forward_eligible"] is True


def test_history_evaluation_compounds_nonoverlapping_forward_periods(tmp_path, monkeypatch):
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    dates = pd.to_datetime([
        "2026-07-01", "2026-07-02", "2026-07-31", "2026-08-03", "2026-08-04"
    ])
    pd.DataFrame({"date": dates, "close": [10, 11, 12, 12, 13]}).to_csv(
        price_dir / "a.csv", index=False
    )
    benchmark_file = tmp_path / "index.csv"
    pd.DataFrame({
        "date": dates,
        "close": [100, 101, 102, 103, 104],
        "total_return_index": [100, 101, 102, 103, 104],
    }).to_csv(
        benchmark_file, index=False
    )
    history = pd.DataFrame({
        "ticker": ["A", "A"],
        "target_weight": [1.0, 1.0],
        "signal_date": ["2026-06-30", "2026-07-31"],
        "execution_date": ["2026-07-01", "2026-08-03"],
        "generated_at": ["2026-07-01T01:00:00Z", "2026-08-03T01:00:00Z"],
        "model_version": ["m", "m"],
        "portfolio_source_kind": ["github_actions_run"] * 2,
        "portfolio_repository": ["owner/repository"] * 2,
        "portfolio_workflow": ["shadow"] * 2,
        "portfolio_run_id": ["123", "456"],
        "portfolio_run_attempt": ["1", "1"],
        "portfolio_run_url": [
            "https://github.com/owner/repository/actions/runs/123",
            "https://github.com/owner/repository/actions/runs/456",
        ],
        "portfolio_default_branch": ["master"] * 2,
        "portfolio_git_ref": ["refs/heads/master"] * 2,
        "portfolio_git_sha": ["a" * 40, "b" * 40],
        "portfolio_event_name": ["schedule"] * 2,
    })
    history_file = tmp_path / "history.csv"
    history.iloc[:1].to_csv(history_file, index=False)
    write_shadow_ledger_manifest(history_file, environment={
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "owner/repository",
        "GITHUB_WORKFLOW": "shadow",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "SHADOW_DEFAULT_BRANCH": "master",
        "GITHUB_REF": "refs/heads/master",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_EVENT_NAME": "schedule",
    })
    history.to_csv(history_file, index=False)
    write_shadow_ledger_manifest(history_file, environment={
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "owner/repository",
        "GITHUB_WORKFLOW": "shadow",
        "GITHUB_RUN_ID": "456",
        "GITHUB_RUN_ATTEMPT": "1",
        "SHADOW_PREVIOUS_ARTIFACT_ID": "987",
        "SHADOW_DEFAULT_BRANCH": "master",
        "GITHUB_REF": "refs/heads/master",
        "GITHUB_SHA": "b" * 40,
        "GITHUB_EVENT_NAME": "schedule",
    })
    monkeypatch.setattr(shadow_evaluation, "CLEANED_PRICE_DATA_DIR", str(price_dir))
    monkeypatch.setattr(shadow_evaluation, "NASDAQ_INDEX_FILE", str(benchmark_file))
    result = evaluate_history(history_file, tmp_path / "result.json", transaction_cost_bps=0)
    assert result["recorded_periods"] == 2
    assert result["forward_periods"] == 2
    assert result["completed_forward_periods"] == 1
    assert result["open_forward_periods"] == 1
    assert result["completed_period_wins_vs_nasdaq"] == 1
    assert result["completed_period_win_rate"] == pytest.approx(1.0)
    assert result["contiguous_forward_strategy_return"] == pytest.approx(
        0.3
    )
    assert result["contiguous_forward_benchmark_return"] == pytest.approx(
        0.04
    )
    assert result["continuous_periods"][0]["completed"] is True
    assert result["continuous_periods"][0][
        "strategy_return"
    ] == pytest.approx(0.2)
    assert result["continuous_periods"][1]["completed"] is False
    assert result["forward_sessions"] == 4
    assert result["forward_strategy_return"] == pytest.approx(0.3)
    assert result["ledger_provenance"]["integrity_verified"] is True
    assert result["ledger_provenance"]["externally_anchored"] is True
    assert result["anchored_forward_periods"] == 2
    assert result["unanchored_forward_periods"] == 0
    assert result["all_forward_periods_externally_anchored"] is True


def test_history_evaluation_uses_canonical_split_adjustment(
    tmp_path,
    monkeypatch,
):
    price_dir = tmp_path / "prices"
    price_dir.mkdir()
    dates = pd.to_datetime([
        "2026-07-01",
        "2026-07-02",
        "2026-07-06",
    ])
    pd.DataFrame({
        "date": dates,
        "close": [100.0, 50.0, 55.0],
    }).to_csv(price_dir / "splt.csv", index=False)
    benchmark_file = tmp_path / "index.csv"
    pd.DataFrame({
        "date": dates,
        "close": [100.0, 100.0, 100.0],
    }).to_csv(benchmark_file, index=False)
    history = pd.DataFrame({
        "ticker": ["SPLT"],
        "target_weight": [1.0],
        "signal_date": ["2026-06-30"],
        "execution_date": ["2026-07-01"],
        "generated_at": ["2026-07-01T01:00:00Z"],
        "model_version": ["test-model"],
    })
    history_file = tmp_path / "history.csv"
    history.to_csv(history_file, index=False)
    write_shadow_ledger_manifest(history_file, environment={})
    monkeypatch.setattr(
        shadow_evaluation,
        "CLEANED_PRICE_DATA_DIR",
        str(price_dir),
    )
    monkeypatch.setattr(
        shadow_evaluation,
        "NASDAQ_INDEX_FILE",
        str(benchmark_file),
    )

    result = evaluate_history(
        history_file,
        tmp_path / "result.json",
        transaction_cost_bps=0,
    )

    assert result["forward_strategy_return"] == pytest.approx(0.10)
    assert result["price_adjustment_policy"] == (
        "confirmed-actions-plus-common-split-heuristic"
    )


def test_forward_account_holds_fixed_positions_instead_of_daily_rebalancing():
    dates = pd.to_datetime([
        "2026-08-03", "2026-08-04", "2026-08-05"
    ])
    close = pd.DataFrame({
        "A": [10.0, 20.0, 20.0],
        "B": [10.0, 10.0, 20.0],
    }, index=dates)
    benchmark = pd.Series([100.0, 100.0, 100.0], index=dates)
    schedules = [{
        "signal_date": pd.Timestamp("2026-07-31"),
        "execution_date": dates[0],
        "evaluation_end": dates[-1],
        "weights": pd.Series({"A": 0.5, "B": 0.5}),
    }]

    result = evaluate_forward_account(
        schedules, close, benchmark, transaction_cost_bps=0
    )

    assert result["forward_strategy_return"] == pytest.approx(1.0)
    assert result["forward_sessions"] == 2
    assert result["completed_forward_periods"] == 0
    assert result["open_forward_periods"] == 1


def test_forward_account_charges_actual_turnover_not_full_monthly_exposure():
    dates = pd.to_datetime(["2026-08-03", "2026-08-31"])
    close = pd.DataFrame({"A": [10.0, 10.0]}, index=dates)
    benchmark = pd.Series([100.0, 100.0], index=dates)
    schedules = [
        {
            "signal_date": pd.Timestamp("2026-07-31"),
            "execution_date": dates[0],
            "evaluation_end": dates[-1],
            "weights": pd.Series({"A": 1.0}),
        },
        {
            "signal_date": pd.Timestamp("2026-08-28"),
            "execution_date": dates[-1],
            "evaluation_end": dates[-1],
            "weights": pd.Series({"A": 1.0}),
        },
    ]

    result = evaluate_forward_account(
        schedules, close, benchmark, transaction_cost_bps=10
    )

    assert result["forward_strategy_return"] == pytest.approx(
        1 / 1.001 - 1
    )
    assert result["rebalances"][1]["turnover"] == pytest.approx(0.0)
    assert result["completed_forward_periods"] == 1
    assert result["open_forward_periods"] == 1
    assert result["continuous_periods"][0][
        "strategy_return"
    ] == pytest.approx(1 / 1.001 - 1)
    assert result["continuous_periods"][1][
        "strategy_return"
    ] == pytest.approx(0.0)


def test_forward_account_migrates_issuer_rename_without_trade_or_cost():
    dates = pd.to_datetime([
        "2025-06-30", "2025-07-01", "2025-07-02"
    ])
    close = pd.DataFrame({
        "OLD": [10.0, float("nan"), float("nan")],
        "NEW": [float("nan"), 11.0, 12.0],
    }, index=dates)
    benchmark = pd.Series([100.0] * 3, index=dates)
    schedules = [
        {
            "signal_date": pd.Timestamp("2025-06-27"),
            "execution_date": dates[0],
            "evaluation_end": dates[-1],
            "weights": pd.Series({"OLD": 1.0}),
        },
        {
            "signal_date": pd.Timestamp("2025-06-30"),
            "execution_date": dates[1],
            "evaluation_end": dates[-1],
            "weights": pd.Series({"OLD": 1.0}),
        },
    ]
    transitions = pd.DataFrame({
        "provider_ticker": ["NEW"],
        "historical_ticker": ["OLD"],
        "last_historical_date": pd.to_datetime(["2025-06-30"]),
        "current_ticker_first_date": pd.to_datetime(["2025-07-01"]),
        "identity_type": ["issuer_rename"],
    })

    result = evaluate_forward_account(
        schedules, close, benchmark, transaction_cost_bps=10,
        identity_transitions=transitions,
    )

    assert result["forward_strategy_return"] == pytest.approx(
        (1 / 1.001) * 1.2 - 1
    )
    assert result["rebalances"][1]["turnover"] == pytest.approx(0.0)
    assert result["rebalances"][1]["transaction_cost"] == pytest.approx(0.0)


def test_forward_account_resets_contiguous_evidence_after_missed_month():
    dates = pd.to_datetime([
        "2026-07-01",
        "2026-09-01",
        "2026-10-01",
        "2026-10-02",
    ])
    close = pd.DataFrame({"A": [10.0] * 4}, index=dates)
    benchmark = pd.Series([100.0] * 4, index=dates)
    provenance = {"externally_anchored": True}
    schedules = [
        {
            "signal_date": pd.Timestamp("2026-06-30"),
            "execution_date": dates[0],
            "evaluation_end": dates[-1],
            "weights": pd.Series({"A": 1.0}),
            "signal_provenance": provenance,
        },
        {
            "signal_date": pd.Timestamp("2026-08-31"),
            "execution_date": dates[1],
            "evaluation_end": dates[-1],
            "weights": pd.Series({"A": 1.0}),
            "signal_provenance": provenance,
        },
        {
            "signal_date": pd.Timestamp("2026-09-30"),
            "execution_date": dates[2],
            "evaluation_end": dates[-1],
            "weights": pd.Series({"A": 1.0}),
            "signal_provenance": provenance,
        },
    ]

    result = evaluate_forward_account(
        schedules, close, benchmark, transaction_cost_bps=0
    )

    assert result["completed_forward_periods"] == 2
    assert result["evidence_gap_count"] == 1
    assert result["continuous_periods"][0]["signal_month_gap"] == 2
    assert not result["continuous_periods"][0][
        "monthly_evidence_contiguous"
    ]
    assert result["contiguous_completed_forward_periods"] == 1
    assert result["contiguous_forward_sessions"] == 2
    assert result["contiguous_forward_strategy_return"] == pytest.approx(
        0.0
    )
    assert result["contiguous_forward_benchmark_return"] == pytest.approx(
        0.0
    )
    assert result[
        "all_contiguous_forward_periods_externally_anchored"
    ]


def test_pending_execution_is_recorded_but_not_counted_as_forward_period(
    tmp_path, monkeypatch
):
    benchmark_file = tmp_path / "index.csv"
    pd.DataFrame({
        "date": ["2026-07-31"],
        "close": [100.0],
    }).to_csv(benchmark_file, index=False)
    history = pd.DataFrame({
        "ticker": ["CASH"],
        "target_weight": [0.0],
        "signal_date": ["2026-07-31"],
        "execution_date": [None],
        "generated_at": ["2026-08-01T01:00:00Z"],
        "model_version": ["m"],
    })
    history_file = tmp_path / "history.csv"
    history.to_csv(history_file, index=False)
    monkeypatch.setattr(
        shadow_evaluation, "NASDAQ_INDEX_FILE", str(benchmark_file)
    )

    result = evaluate_history(history_file, tmp_path / "result.json")

    assert result["recorded_periods"] == 1
    assert result["pending_periods"] == 1
    assert result["forward_periods"] == 0
    assert result["forward_sessions"] == 0
    assert result["forward_strategy_return"] is None
