import pandas as pd

from scripts.research_v11_monthly_trend_scaled_v7 import scale_sleeve_monthly


def test_trend_decision_uses_only_closes_before_rebalance():
    dates = pd.bdate_range("2024-01-02", periods=70)
    sleeve = pd.DataFrame({
        "return": 0.0, "benchmark_return": 0.0, "qqq_return": 0.0,
    }, index=dates)
    close = pd.Series(range(1, 71), index=dates, dtype=float)
    _, decisions = scale_sleeve_monthly(
        sleeve, close, trend_window=5, risk_off_exposure=0.5,
        transaction_cost_bps=0.0,
    )
    mature = decisions.loc[decisions["trend_ready"]].iloc[0]
    position = dates.get_loc(pd.Timestamp(mature["date"]))
    assert mature["prior_close"] == close.iloc[position - 1]
    assert mature["prior_trend_mean"] == close.iloc[:position].tail(5).mean()


def test_downtrend_moves_half_the_sleeve_to_cash():
    dates = pd.bdate_range("2024-01-02", periods=70)
    sleeve = pd.DataFrame({
        "return": 0.0, "benchmark_return": 0.0, "qqq_return": 0.0,
    }, index=dates)
    close = pd.Series(range(70, 0, -1), index=dates, dtype=float)
    _, decisions = scale_sleeve_monthly(
        sleeve, close, trend_window=5, risk_off_exposure=0.5,
        transaction_cost_bps=0.0,
    )
    mature = decisions.loc[decisions["trend_ready"]]
    assert (~mature["trend_on"]).all()
    assert (mature["exposure"] == 0.5).all()
