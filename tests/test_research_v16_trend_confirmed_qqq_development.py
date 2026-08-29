import pandas as pd

from scripts import research_v16_trend_confirmed_qqq_development as v16


def test_trend_schedule_uses_prior_session_and_preserves_stock_target():
    dates = pd.date_range("2023-01-02", periods=7, freq="D")
    targets = pd.DataFrame({
        "effective_date": [dates[0], dates[5]],
        "ticker": ["__CASH__", "A"],
        "target_weight": [0.0, 1.0],
        "base_transaction_cost_bps": [10.0, 10.0],
    })
    qqq_close = pd.Series(
        [100.0, 90.0, 95.0, 96.0, 97.0, 98.0, 99.0],
        index=dates,
    )

    schedule = v16.trend_confirmed_target_schedule(
        targets,
        qqq_close,
        dates,
        lookback=2,
        end=dates[-1],
    )

    assert schedule[["effective_date", "ticker"]].values.tolist() == [
        [dates[0], "__CASH__"],
        [dates[3], v16.CORE_TICKER],
        [dates[5], "A"],
    ]
    assert schedule.loc[schedule["ticker"].eq("A"), "target_weight"].item() == 1.0


def test_trend_schedule_never_emits_past_development_end():
    dates = pd.date_range("2024-12-27", periods=7, freq="D")
    targets = pd.DataFrame({
        "effective_date": [dates[0], dates[-1]],
        "ticker": ["__CASH__", "FUTURE"],
        "target_weight": [0.0, 1.0],
        "base_transaction_cost_bps": [10.0, 10.0],
    })
    qqq_close = pd.Series(range(100, 107), index=dates, dtype=float)

    schedule = v16.trend_confirmed_target_schedule(
        targets,
        qqq_close,
        dates,
        lookback=2,
        end="2024-12-31",
    )

    assert schedule["effective_date"].max() <= pd.Timestamp("2024-12-31")
    assert "FUTURE" not in set(schedule["ticker"])


def test_candidate_selection_requires_full_gate_pass_and_uses_maximin():
    def summary(passed: bool, floor: float, compounded: float) -> dict:
        return {
            "all_development_gates_passed": passed,
            "costs": {"10": {
                "annual": [
                    {"excess_vs_nasdaq": floor},
                    {"excess_vs_nasdaq": floor + 0.1},
                ],
                "compounded_excess": compounded,
            }},
        }

    assert v16.select_candidate({
        20: summary(False, 0.5, 1.0),
        50: summary(True, -0.01, 0.4),
        100: summary(True, 0.02, 0.3),
        200: summary(True, 0.02, 0.3),
    }) == 200
    assert v16.select_candidate({20: summary(False, 1.0, 1.0)}) is None
