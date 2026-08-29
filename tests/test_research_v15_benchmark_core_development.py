import pandas as pd
import pytest

from scripts import research_v15_benchmark_core_development as v15


def test_core_fill_preserves_stocks_and_assigns_only_residual_weight():
    targets = pd.DataFrame({
        "effective_date": pd.to_datetime([
            "2023-01-03",
            "2023-02-01",
            "2023-02-01",
            "2025-01-02",
        ]),
        "ticker": ["__CASH__", "A", "B", "FUTURE"],
        "target_weight": [0.0, 0.3, 0.2, 1.0],
        "base_transaction_cost_bps": [10.0, 10.0, 10.0, 10.0],
    })

    filled = v15.fill_uninvested_target_weight(targets, end="2024-12-31")

    assert set(filled["ticker"]) == {v15.CORE_TICKER, "A", "B"}
    by_date = filled.groupby("effective_date")["target_weight"].sum()
    assert by_date.eq(1.0).all()
    feb = filled.loc[filled["effective_date"].eq(pd.Timestamp("2023-02-01"))]
    assert feb.set_index("ticker")["target_weight"].to_dict() == {
        "A": 0.3,
        "B": 0.2,
        v15.CORE_TICKER: 0.5,
    }


def test_qqq_total_return_index_includes_dividend_and_rejects_real_gap():
    index = pd.to_datetime(["2023-01-03", "2023-01-04", "2023-01-05"])
    qqq = pd.DataFrame({
        "close": [100.0, 100.0, 101.0],
        "cash_dividend": [0.0, 1.0, 0.0],
    }, index=index)
    wealth = v15.qqq_total_return_index(
        qqq,
        index,
        allowed_market_closed=pd.Series(False, index=index),
    )
    assert wealth.iloc[-1] == pytest.approx(102.01)

    broken = qqq.drop(index[1])
    with pytest.raises(ValueError, match="non-market-closure gaps"):
        v15.qqq_total_return_index(
            broken,
            index,
            allowed_market_closed=pd.Series(False, index=index),
        )


def test_development_summary_never_accepts_fewer_than_expected_years():
    dates = pd.to_datetime(["2022-12-30", "2023-12-29"])
    result = pd.DataFrame({
        "strategy": [0.1, 0.1],
        "benchmark": [0.0, 0.0],
    }, index=dates)
    v14 = result.copy()
    with pytest.raises(RuntimeError, match="year envelope changed"):
        v15.summarize_development(
            {10: result, 30: result, 50: result}, v14
        )
