import pandas as pd
import pytest

from src.research import shadow_evaluation
from src.research.shadow_evaluation import evaluate_history, evaluate_recorded_portfolio


def _records(generated_at):
    return pd.DataFrame({
        "ticker": ["A", "B"],
        "target_weight": [0.5, 0.5],
        "signal_date": ["2026-06-30", "2026-06-30"],
        "execution_date": ["2026-07-01", "2026-07-01"],
        "generated_at": [generated_at, generated_at],
    })


def test_missing_history_is_a_valid_zero_position_state(tmp_path):
    output = tmp_path / "shadow.json"

    result = evaluate_history(tmp_path / "missing.csv", output)

    assert result["status"] == "NO_RECORDED_POSITIONS"
    assert result["forward_sessions"] == 0
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
    })
    history_file = tmp_path / "history.csv"
    history.to_csv(history_file, index=False)
    monkeypatch.setattr(shadow_evaluation, "CLEANED_PRICE_DATA_DIR", str(price_dir))
    monkeypatch.setattr(shadow_evaluation, "NASDAQ_INDEX_FILE", str(benchmark_file))
    result = evaluate_history(history_file, tmp_path / "result.json", transaction_cost_bps=0)
    assert result["recorded_periods"] == 2
    assert result["forward_periods"] == 2
    assert result["forward_sessions"] == 3
    assert result["forward_strategy_return"] == pytest.approx(0.3)
