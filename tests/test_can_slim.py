import numpy as np
import pandas as pd
import pytest

from src.research.can_slim import (
    CanSlimConfig,
    calculate_can_slim_returns_with_ledger,
    calculate_can_slim_scheduled_returns,
    calculate_keltner_upper_panel,
    select_can_slim_ensemble_portfolio,
    select_can_slim_portfolio,
)


def test_can_slim_selects_profitable_leader_near_high_with_volume():
    dates = pd.bdate_range("2024-01-01", periods=260)
    close = pd.DataFrame({
        "LEAD": range(20, 280),
        "LAG": list(range(100, 330)) + list(range(330, 300, -1)),
    }, index=dates, dtype=float)
    volume = pd.DataFrame(3_000_000.0, index=dates, columns=close.columns)
    index = pd.Series(range(1_000, 1_260), index=dates, dtype=float)
    eps = pd.DataFrame({
        "ticker": ["LEAD", "LAG"],
        "period_end": [dates[-80], dates[-80]],
        "available_date": [dates[-70], dates[-70]],
        "quarterly_eps": [1.0, 1.0],
        "trailing_eps": [4.0, 4.0],
        "prior_trailing_eps": [2.0, 2.0],
        "eps_growth": [1.0, 1.0],
        "source": ["test", "test"],
    })
    selected = select_can_slim_portfolio(
        dates[-1], close, volume, index, eps, CanSlimConfig(top_n=1), {"LEAD", "LAG"}
    )
    assert selected.index.tolist() == ["LEAD"]
    assert selected.loc["LEAD", "target_weight"] == 0.2


def test_can_slim_keltner_variant_requires_true_range_breakout():
    dates = pd.bdate_range("2024-01-01", periods=260)
    close = pd.DataFrame({"LEAD": range(20, 280)}, index=dates, dtype=float)
    high = close + 0.5
    low = close - 0.5
    volume = pd.DataFrame(3_000_000.0, index=dates, columns=close.columns)
    index = pd.Series(range(1_000, 1_260), index=dates, dtype=float)
    eps = pd.DataFrame({
        "ticker": ["LEAD"], "period_end": [dates[-80]],
        "available_date": [dates[-70]], "quarterly_eps": [1.0],
        "trailing_eps": [4.0], "prior_trailing_eps": [2.0],
        "eps_growth": [1.0], "source": ["test"],
    })

    selected = select_can_slim_portfolio(
        dates[-1], close, volume, index, eps,
        CanSlimConfig(top_n=1, price_channel="keltner"), {"LEAD"},
        None, calculate_keltner_upper_panel(close, high, low),
    )

    assert selected.index.tolist() == ["LEAD"]
    assert bool(selected.loc["LEAD", "keltner_breakout"])


def test_ensemble_averages_zero_weight_for_configs_that_do_not_select_a_stock():
    dates = pd.bdate_range("2024-01-01", periods=260)
    close = pd.DataFrame({
        "A": range(20, 280),
        "B": range(19, 279),
    }, index=dates, dtype=float)
    volume = pd.DataFrame(20_000_000.0, index=dates, columns=close.columns)
    index = pd.Series(range(1_000, 1_260), index=dates, dtype=float)
    eps = pd.DataFrame({
        "ticker": ["A", "B"], "period_end": [dates[-80]] * 2,
        "available_date": [dates[-70]] * 2, "quarterly_eps": [1.0] * 2,
        "trailing_eps": [4.0] * 2, "prior_trailing_eps": [2.0] * 2,
        "eps_growth": [1.0] * 2, "source": ["test"] * 2,
    })

    selected = select_can_slim_ensemble_portfolio(
        dates[-1], close, volume, index, eps,
        [CanSlimConfig(top_n=1), CanSlimConfig(top_n=2)], {"A", "B"},
    )

    assert round(selected["target_weight"].sum(), 10) == 0.3
    assert sorted(selected["ensemble_votes"].tolist()) == [1, 2]


def test_ensemble_uses_frozen_model_weights():
    dates = pd.bdate_range("2024-01-01", periods=260)
    close = pd.DataFrame({"A": range(20, 280), "B": range(30, 290)}, index=dates, dtype=float)
    volume = pd.DataFrame(20_000_000.0, index=dates, columns=close.columns)
    index = pd.Series(range(1_000, 1_260), index=dates, dtype=float)
    eps = pd.DataFrame({
        "ticker": ["A", "B"], "period_end": [dates[-80]] * 2,
        "available_date": [dates[-70]] * 2, "quarterly_eps": [1.0] * 2,
        "trailing_eps": [4.0] * 2, "prior_trailing_eps": [2.0] * 2,
        "eps_growth": [1.0] * 2, "source": ["test"] * 2,
    })

    selected = select_can_slim_ensemble_portfolio(
        dates[-1], close, volume, index, eps,
        [
            CanSlimConfig(top_n=1, ensemble_weight=2),
            CanSlimConfig(top_n=2, ensemble_weight=1),
        ], {"A", "B"},
    )

    assert selected["target_weight"].sum() == pytest.approx(0.2666666667)


def test_quarterly_profit_and_sales_can_replace_missing_eps_without_lookahead():
    dates = pd.bdate_range("2024-01-01", periods=260)
    close = pd.DataFrame({"SEC": range(20, 280)}, index=dates, dtype=float)
    volume = pd.DataFrame(20_000_000.0, index=dates, columns=close.columns)
    index = pd.Series(range(1_000, 1_260), index=dates, dtype=float)
    ends = pd.date_range("2022-12-31", periods=8, freq="QE")
    rows = []
    for index_value, end in enumerate(ends):
        multiplier = 2 if index_value >= 4 else 1
        for metric, value in (("net_income", 10), ("revenue", 100)):
            rows.append({
                "ticker": "SEC", "fiscal_end": end,
                "available_date": end + pd.Timedelta(days=30),
                "metric": metric, "value": value * multiplier,
            })
    quarterly = pd.DataFrame(rows)
    selected = select_can_slim_portfolio(
        dates[-1], close, volume, index, pd.DataFrame(columns=[
            "ticker", "period_end", "available_date", "quarterly_eps"
        ]), CanSlimConfig(top_n=1, use_quarterly_fundamentals=True), {"SEC"},
        quarterly,
    )
    assert selected.index.tolist() == ["SEC"]
    assert selected.loc["SEC", "financial_source"] == "sec_quarterly"


def test_recovery_variant_accepts_flat_profits_only_with_sec_sales_and_breakout():
    dates = pd.bdate_range("2024-01-01", periods=260)
    close = pd.DataFrame({"REC": range(20, 280)}, index=dates, dtype=float)
    high, low = close + 0.5, close - 0.5
    volume = pd.DataFrame(20_000_000.0, index=dates, columns=close.columns)
    index = pd.Series(range(1_000, 1_260), index=dates, dtype=float)
    rows = []
    for end in pd.date_range("2022-12-31", periods=8, freq="QE"):
        for metric, value in (("net_income", 10), ("revenue", 100)):
            rows.append({
                "ticker": "REC", "fiscal_end": end,
                "available_date": end + pd.Timedelta(days=30),
                "metric": metric, "value": value,
            })
    empty_eps = pd.DataFrame(columns=[
        "ticker", "period_end", "available_date", "quarterly_eps"
    ])
    config = CanSlimConfig(
        top_n=1, use_quarterly_fundamentals=True,
        price_channel="keltner", selection_mode="recovery",
    )

    selected = select_can_slim_portfolio(
        dates[-1], close, volume, index, empty_eps, config, {"REC"},
        pd.DataFrame(rows), calculate_keltner_upper_panel(close, high, low),
    )

    assert selected.index.tolist() == ["REC"]
    assert selected.loc["REC", "profit_growth"] == 0


def test_quarterly_model_does_not_fallback_to_legacy_eps():
    dates = pd.bdate_range("2024-01-01", periods=260)
    close = pd.DataFrame({"LEGACY": range(20, 280)}, index=dates, dtype=float)
    volume = pd.DataFrame(20_000_000.0, index=dates, columns=close.columns)
    index = pd.Series(range(1_000, 1_260), index=dates, dtype=float)
    eps = pd.DataFrame({
        "ticker": ["LEGACY"], "period_end": [dates[-80]],
        "available_date": [dates[-70]], "quarterly_eps": [1.0],
        "trailing_eps": [4.0], "prior_trailing_eps": [2.0],
        "eps_growth": [1.0], "source": ["legacy"],
    })

    selected = select_can_slim_portfolio(
        dates[-1], close, volume, index, eps,
        CanSlimConfig(top_n=1, use_quarterly_fundamentals=True), {"LEGACY"},
        pd.DataFrame(columns=[
            "ticker", "fiscal_end", "available_date", "metric", "value"
        ]),
    )

    assert selected.empty


def test_scheduled_replay_uses_new_snapshot_on_prior_month_signal(
    monkeypatch,
):
    dates = pd.bdate_range("2022-01-03", "2023-01-06")
    close = pd.DataFrame({"A": range(100, 100 + len(dates))}, index=dates)
    volume = pd.DataFrame(20_000_000.0, index=dates, columns=["A"])
    index = pd.Series(range(1_000, 1_000 + len(dates)), index=dates)
    config_dates = []

    def config_as_of(effective):
        config_dates.append(effective)
        return [CanSlimConfig(top_n=1)]

    monkeypatch.setattr(
        "src.research.can_slim.select_can_slim_ensemble_portfolio",
        lambda *args, **kwargs: pd.DataFrame(
            {"target_weight": [1.0]}, index=["A"]
        ),
    )
    monkeypatch.setattr(
        "src.research.can_slim.market_regime_is_on",
        lambda *args, **kwargs: True,
    )

    result = calculate_can_slim_scheduled_returns(
        close,
        volume,
        index,
        pd.DataFrame(),
        "2023-01-01",
        "2023-01-06",
        config_as_of,
        lambda signal_date: {"A"},
    )

    assert config_dates[0] == pd.Timestamp("2023-01-02")
    assert result.loc["2023-01-02", "turnover"] == pytest.approx(
        1 / 1.001
    )


def test_trade_ledger_holds_fixed_shares_and_reconciles_portfolio_value(
    monkeypatch,
):
    dates = pd.bdate_range("2023-01-02", periods=280)
    close = pd.DataFrame(
        {"A": np.linspace(50, 100, len(dates))}, index=dates
    )
    volume = pd.DataFrame(20_000_000.0, index=dates, columns=["A"])
    index = pd.Series(np.linspace(1_000, 1_100, len(dates)), index=dates)
    monkeypatch.setattr(
        "src.research.can_slim.select_can_slim_portfolio",
        lambda *args, **kwargs: pd.DataFrame(
            {"target_weight": [1.0]}, index=["A"]
        ),
    )
    monkeypatch.setattr(
        "src.research.can_slim.market_regime_is_on",
        lambda *args, **kwargs: True,
    )
    config = CanSlimConfig(
        start=str(dates[255].date()),
        end=str(dates[-1].date()),
        top_n=1,
        maximum_position_weight=1.0,
        transaction_cost_bps=10,
    )

    result, ledger = calculate_can_slim_returns_with_ledger(
        close, volume, index, pd.DataFrame(), config, lambda _: {"A"}
    )

    first_buy = ledger.iloc[0]
    assert first_buy["side"] == "BUY"
    assert first_buy["transaction_cost"] == pytest.approx(
        first_buy["gross_notional"] * 0.001
    )
    signed_shares = ledger["shares"].where(
        ledger["side"].eq("BUY"), -ledger["shares"]
    ).sum()
    assert result.iloc[-1]["portfolio_value"] == pytest.approx(
        result.iloc[-1]["cash"] + signed_shares * close.iloc[-1]["A"],
        rel=1e-6,
    )
    compounded = (1 + result["strategy"]).prod() * 1_000_000
    assert compounded == pytest.approx(result.iloc[-1]["portfolio_value"])
