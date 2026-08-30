import pandas as pd

from scripts import research_v47_hybrid_entry_portfolio_stop as v47


def _targets(date, tickers):
    return pd.DataFrame({
        "effective_date": [date] * len(tickers),
        "ticker": tickers,
        "target_weight": [1.0 / len(tickers)] * len(tickers),
    })


def test_v47_has_one_candidate_and_no_new_threshold_grid():
    spec = v47.candidate_spec()

    assert spec["entry_loss_fraction"] == 0.20
    assert spec["portfolio_trailing_stop_fraction"] == 0.25
    assert v47.ENTRY_LOSS_FRACTION == 0.20
    assert v47.PORTFOLIO_TRAILING_STOP_FRACTION == 0.25


def test_hybrid_entry_stop_exits_only_triggered_stock_next_close():
    dates = pd.bdate_range("2020-01-02", periods=6)
    close = pd.DataFrame({
        "A": [100.0, 100.0, 79.0, 78.0, 80.0, 82.0],
        "B": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
    }, index=dates)

    result = v47.replay_with_hybrid_stop(
        close,
        pd.Series(1000.0, index=dates),
        _targets(dates[0], ["A", "B"]),
        dates[0],
        dates[-1],
        entry_loss_fraction=0.20,
        portfolio_stop_fraction=0.50,
        transaction_cost_bps=0.0,
    )

    assert result.loc[dates[2], "stock_stop_exits"] == 0
    assert result.loc[dates[3], "stock_stop_exits"] == 1
    assert result.loc[dates[3], "holdings"] == 1


def test_hybrid_portfolio_stop_exits_entire_portfolio_next_close():
    dates = pd.bdate_range("2020-01-02", periods=6)
    close = pd.DataFrame({
        "A": [100.0, 100.0, 80.0, 79.0, 78.0, 77.0],
        "B": [100.0, 100.0, 80.0, 79.0, 78.0, 77.0],
    }, index=dates)

    result = v47.replay_with_hybrid_stop(
        close,
        pd.Series(1000.0, index=dates),
        _targets(dates[0], ["A", "B"]),
        dates[0],
        dates[-1],
        entry_loss_fraction=0.50,
        portfolio_stop_fraction=0.15,
        transaction_cost_bps=0.0,
    )

    assert result.loc[dates[2], "portfolio_stop_exits"] == 0
    assert result.loc[dates[3], "portfolio_stop_exits"] == 1
    assert result.loc[dates[3], "holdings"] == 0


def test_protocol_is_training_only_and_blocked(tmp_path):
    protocol = v47.freeze_protocol(tmp_path / "protocol.json")

    assert protocol["candidate_count"] == 1
    assert protocol["source_diagnosis"]["new_threshold_search"] is False
    assert protocol["evaluation_boundary"]["2026_used_for_parameter_selection"] is False
    assert protocol["release_status"] == "BLOCKED"
    assert protocol["promotion_eligible"] is False
