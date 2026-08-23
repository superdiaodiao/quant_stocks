from collections import Counter
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from src.research.metrics import annual_returns
from src.research import can_slim_validation
from src.research.data_quality import (
    apply_confirmed_price_adjustments,
    back_adjust_common_splits,
    detect_common_split_events,
    restore_contemporaneous_prices,
    stock_returns_with_delisting_penalty,
)
from src.strategy.common import (
    calculate_bollinger_bands,
    calculate_donchian_channel,
    calculate_keltner_channel,
)


def test_stale_snapshot_diagnostic_bounds_impact_without_resolving_pit(
    monkeypatch,
):
    snapshots = {
        pd.Timestamp("2023-04-18"): {"A", "B"},
        pd.Timestamp("2023-06-16"): {"A", "C"},
    }

    def fake_score(
        _date, _close, _dollar_volume, _nasdaq, _eps, _config,
        eligible_symbols, _quarterly,
    ):
        symbols = {"A", "B", "C", "X"} if eligible_symbols is None else eligible_symbols
        ordered = [symbol for symbol in ("A", "B", "C", "X") if symbol in symbols]
        return pd.DataFrame(
            {"score": range(len(ordered), 0, -1)}, index=ordered
        )

    monkeypatch.setattr(
        can_slim_validation, "score_can_slim_cross_section", fake_score
    )
    report = can_slim_validation.stale_snapshot_selection_diagnostics(
        ["2023-05-31"],
        snapshots,
        pd.DataFrame(),
        pd.DataFrame(),
        pd.Series(dtype=float),
        pd.DataFrame(),
        can_slim_validation.fixed_top3_config(),
        pd.DataFrame(),
    )

    signal = report["signals"][0]
    assert signal["prior_snapshot_age_days"] == 43
    assert signal["later_snapshot_lead_days"] == 16
    assert signal["later_added_symbols"] == 1
    assert signal["later_removed_symbols"] == 1
    assert signal["later_added_eligible_symbols"] == ["C"]
    assert signal["top3_stable_across_prior_later_and_union"] is False
    assert report["all_bracketed_top3_stable"] is False
    assert report["point_in_time_gap_resolved"] is False


def test_candidate_financial_coverage_uses_shared_technical_filters(
    monkeypatch,
):
    dates = pd.bdate_range("2024-01-02", periods=270)
    close = pd.DataFrame(
        {"A": 20.0, "LOW": 5.0},
        index=dates,
    )
    dollar_volume = pd.DataFrame(
        {"A": 20_000_000.0, "LOW": 20_000_000.0},
        index=dates,
    )
    nasdaq = pd.Series(
        np.linspace(100.0, 200.0, len(dates)),
        index=dates,
    )
    monkeypatch.setattr(
        can_slim_validation,
        "quarterly_growth_snapshot",
        lambda *_args, **_kwargs: pd.DataFrame(),
    )
    monkeypatch.setattr(
        can_slim_validation,
        "quarterly_profit_ttm_snapshot",
        lambda *_args, **_kwargs: pd.DataFrame(),
    )
    monkeypatch.setattr(
        can_slim_validation,
        "back_adjust_common_splits",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pre-adjusted prices were ignored")
        ),
    )
    config = replace(
        can_slim_validation.fixed_top3_config(),
        end=dates[-1].strftime("%Y-%m-%d"),
        market_ma_days=2,
    )
    report = can_slim_validation.technical_candidate_financial_coverage(
        close,
        dollar_volume,
        nasdaq,
        pd.DataFrame({
            "ticker": pd.Series(dtype=str),
            "available_date": pd.Series(dtype="datetime64[ns]"),
            "metric": pd.Series(dtype=str),
        }),
        {dates[0]: {"A", "LOW"}},
        config,
        start=dates[0].strftime("%Y-%m-%d"),
        adjusted_close=close,
    )

    assert report["technical_candidate_observations"] > 0
    assert report["missing_financial_symbols"] == ["A"]
    assert report["missing_financial_priorities"][0]["ticker"] == "A"
    assert report["missing_financial_priorities"][0][
        "no_raw_pit_financial_facts_signal_count"
    ] == report["missing_financial_priorities"][0]["missing_signal_count"]
    assert report["missing_financial_priorities"][0][
        "reporting_profile"
    ] == "NO_PARSED_SEC_FINANCIALS"
    assert report["complete"] is False


def test_candidate_financial_coverage_resolves_recent_ttm_loss(
    monkeypatch,
):
    dates = pd.bdate_range("2024-01-02", periods=270)
    close = pd.DataFrame({"LOSS": 20.0}, index=dates)
    dollar_volume = pd.DataFrame({"LOSS": 20_000_000.0}, index=dates)
    nasdaq = pd.Series(np.linspace(100.0, 200.0, len(dates)), index=dates)
    monkeypatch.setattr(
        can_slim_validation, "quarterly_growth_snapshot",
        lambda *_args, **_kwargs: pd.DataFrame(),
    )
    monkeypatch.setattr(
        can_slim_validation, "quarterly_profit_ttm_snapshot",
        lambda *_args, **_kwargs: pd.DataFrame(
            {"net_income_ttm": [-10.0]}, index=["LOSS"]
        ),
    )
    config = replace(
        can_slim_validation.fixed_top3_config(),
        end=dates[-1].strftime("%Y-%m-%d"), market_ma_days=2,
    )
    fundamentals = pd.DataFrame({
        "ticker": ["LOSS"], "available_date": [dates[0]],
        "metric": ["net_income"],
    })
    report = can_slim_validation.technical_candidate_financial_coverage(
        close, dollar_volume, nasdaq, fundamentals,
        {dates[0]: {"LOSS"}}, config,
        start=dates[0].strftime("%Y-%m-%d"), adjusted_close=close,
    )
    assert report["missing_financial_observations"] == 0
    assert report["known_nonpositive_profit_observations"] > 0
    assert report["missing_financial_symbols"] == []
    assert report["complete"] is True


def test_cost_stress_selector_cache_ignores_only_transaction_cost(
    monkeypatch,
):
    calls = []

    def fake_select(
        date,
        close,
        dollar_volume,
        index_close,
        eps,
        config,
        eligible_symbols=None,
        quarterly_fundamentals=None,
        keltner_upper=None,
        eligibility_close=None,
    ):
        calls.append((
            pd.Timestamp(date),
            config.transaction_cost_bps,
            frozenset(eligible_symbols or set()),
        ))
        return pd.DataFrame(
            {"target_weight": [1.0]}, index=["ABC"]
        )

    monkeypatch.setattr(
        can_slim_validation.can_slim_module,
        "select_can_slim_portfolio",
        fake_select,
    )
    base = can_slim_validation.fixed_top3_config(0.0)
    stressed = replace(base, transaction_cost_bps=50.0)
    arguments = (
        pd.Timestamp("2026-01-30"),
        pd.DataFrame(),
        pd.DataFrame(),
        pd.Series(dtype=float),
        pd.DataFrame(),
    )

    with can_slim_validation._memoized_cost_stress_selection():
        first = (
            can_slim_validation.can_slim_module.select_can_slim_portfolio(
                *arguments, base, {"ABC"}
            )
        )
        first.loc["ABC", "target_weight"] = 0.5
        second = (
            can_slim_validation.can_slim_module.select_can_slim_portfolio(
                *arguments, stressed, {"ABC"}
            )
        )
        can_slim_validation.can_slim_module.select_can_slim_portfolio(
            *arguments, stressed, {"XYZ"}
        )

    assert len(calls) == 2
    assert second.loc["ABC", "target_weight"] == 1.0
    assert (
        can_slim_validation.can_slim_module.select_can_slim_portfolio
        is fake_select
    )


def test_validation_artifact_set_rolls_back_on_partial_replace(
    tmp_path, monkeypatch
):
    artifact_names = [
        "can_slim_fixed_top3_backtest.csv",
        "can_slim_fixed_top3_trade_ledger.csv",
        "can_slim_fixed_top3_annual.csv",
        "can_slim_fixed_top3_cost_stress.csv",
        "can_slim_fixed_top3_liquidity_capacity.csv",
        "can_slim_fixed_top3_summary.json",
        "can_slim_technical_candidate_financial_coverage.json",
        "can_slim_technical_candidate_financial_priorities.csv",
        "can_slim_validation_artifacts_manifest.json",
    ]
    before = {}
    for name in artifact_names:
        target = tmp_path / name
        target.write_text(f"old:{name}\n", encoding="utf-8")
        before[name] = target.read_bytes()
    validation = (
        pd.DataFrame({"strategy": [0.0]}),
        pd.DataFrame({"strategy": [0.0]}),
        pd.DataFrame({"cost_bps": [10.0]}),
        pd.DataFrame({"ticker": ["ABC"]}),
        pd.DataFrame({"ticker": ["ABC"]}),
        {"release_status": "BLOCKED"},
        {"missing_financial_priorities": []},
    )
    real_replace = can_slim_validation.os.replace
    failed = False

    def fail_middle_replace(source, destination):
        nonlocal failed
        if (
            destination
            == tmp_path / "can_slim_fixed_top3_cost_stress.csv"
            and str(source).endswith(".tmp")
            and not failed
        ):
            failed = True
            raise OSError("injected validation artifact failure")
        return real_replace(source, destination)

    monkeypatch.setattr(
        can_slim_validation.os, "replace", fail_middle_replace
    )

    with pytest.raises(
        OSError, match="injected validation artifact failure"
    ):
        can_slim_validation.write_can_slim_validation_outputs(
            validation, tmp_path
        )

    assert {
        name: (tmp_path / name).read_bytes()
        for name in artifact_names
    } == before
    assert not list(tmp_path.glob(".*.tmp"))
    assert not list(tmp_path.glob(".*.bak.*"))


def test_financial_reporting_profile_separates_one_sided_sec_facts():
    reasons = Counter({"insufficient_growth_history": 3})

    assert can_slim_validation._financial_reporting_profile(
        {"10-K"}, reasons, {"net_income"}
    ) == "SEC_NET_INCOME_ONLY_NO_REVENUE_FACTS"
    assert can_slim_validation._financial_reporting_profile(
        {"10-K"}, reasons, {"revenue"}
    ) == "SEC_REVENUE_ONLY_NO_NET_INCOME_FACTS"
    assert can_slim_validation._financial_reporting_profile(
        {"10-K"}, reasons, {"net_income", "revenue"}
    ) == "SEC_QUARTERLY_PARTIAL"
    assert can_slim_validation._financial_reporting_profile(
        {"20-F"}, reasons, {"net_income"}
    ) == "FOREIGN_ANNUAL_ONLY_NEEDS_QUARTERLY_SOURCE"
    assert (
        can_slim_validation._sec_cache_refresh_tier(
            "SEC_QUARTERLY_PARTIAL", 3
        )
        < can_slim_validation._sec_cache_refresh_tier(
            "NO_PARSED_SEC_FINANCIALS", 20
        )
        < can_slim_validation._sec_cache_refresh_tier(
            "SEC_QUARTERLY_PARTIAL", 2
        )
        < can_slim_validation._sec_cache_refresh_tier(
            "NO_PARSED_SEC_FINANCIALS", 2
        )
        < can_slim_validation._sec_cache_refresh_tier(
            "FOREIGN_ANNUAL_ONLY_NEEDS_QUARTERLY_SOURCE", 30
        )
    )


def test_recommended_financial_data_action_uses_raw_cache_structure():
    assert can_slim_validation._recommended_financial_data_action(
        "NO_PARSED_SEC_FINANCIALS", "NOT_CACHED"
    ) == "FETCH_SEC_COMPANYFACTS"
    assert can_slim_validation._recommended_financial_data_action(
        "NO_PARSED_SEC_FINANCIALS", "FOREIGN_PERIODIC_NO_10Q"
    ) == "NEEDS_FOREIGN_QUARTERLY_SOURCE"
    assert can_slim_validation._recommended_financial_data_action(
        "NO_PARSED_SEC_FINANCIALS", "US_GAAP_WITH_10Q"
    ) == "REVIEW_US_GAAP_PARSER"
    assert can_slim_validation._recommended_financial_data_action(
        "SEC_QUARTERLY_PARTIAL",
        "FDIC_EXCHANGE_ACT_NO_SEC_COMPANYFACTS",
    ) == "NEEDS_FDIC_ARCHIVED_QUARTERLY_SOURCE"
    assert can_slim_validation._recommended_financial_data_action(
        "SEC_ANNUAL_ONLY_OR_UNMAPPED_QUARTERLY",
        "US_GAAP_WITH_10Q",
        has_supported_revenue_source=True,
    ) == "REPARSE_OR_ACCEPT_HISTORY_LIMIT"
    assert can_slim_validation._recommended_financial_data_action(
        "SEC_NET_INCOME_ONLY_NO_REVENUE_FACTS",
        "US_GAAP_WITH_10Q",
        has_supported_revenue_source=False,
    ) == "CONFIRM_NO_OPERATING_REVENUE"


def test_fdic_taxonomy_routes_non_sec_filer_away_from_companyfacts():
    profile = can_slim_validation._effective_raw_financial_profile(
        "NOT_CACHED", {"fdic-10q"}
    )

    assert profile == "FDIC_EXCHANGE_ACT_NO_SEC_COMPANYFACTS"
    assert can_slim_validation._sec_cache_refresh_tier(
        "SEC_QUARTERLY_PARTIAL", 17, profile
    ) == 98


def test_annual_cost_capacity_reports_bracketed_and_unprofitable_years():
    costs = pd.DataFrame([
        {"year": 2024, "cost_bps": 0.0, "excess_vs_nasdaq": 0.04},
        {"year": 2024, "cost_bps": 10.0, "excess_vs_nasdaq": 0.02},
        {"year": 2024, "cost_bps": 30.0, "excess_vs_nasdaq": -0.02},
        {"year": 2025, "cost_bps": 0.0, "excess_vs_nasdaq": -0.01},
        {"year": 2025, "cost_bps": 10.0, "excess_vs_nasdaq": -0.02},
        {"year": 2026, "cost_bps": 0.0, "excess_vs_nasdaq": 0.08},
        {"year": 2026, "cost_bps": 50.0, "excess_vs_nasdaq": 0.01},
    ])

    report = can_slim_validation.annual_cost_capacity_diagnostics(costs)

    assert report["2024"][
        "estimated_break_even_one_way_cost_bps"
    ] == pytest.approx(20.0)
    assert report["2024"]["classification"] == "BRACKETED_INTERPOLATION"
    assert report["2025"][
        "estimated_break_even_one_way_cost_bps"
    ] == 0.0
    assert report["2025"]["classification"] == "NONPOSITIVE_BEFORE_COST"
    assert report["2026"]["break_even_cost_lower_bound_bps"] == 50.0


def test_cost_stress_attribution_separates_preexisting_breadth_failure():
    assert can_slim_validation.transaction_cost_stress_failure_attribution(
        compounded_alpha_positive=True,
        annual_breadth_passed=False,
        incremental_failed_years=[],
    ) == ["ANNUAL_BREADTH_BELOW_THRESHOLD_BEFORE_STRESS_COST"]
    assert can_slim_validation.transaction_cost_stress_failure_attribution(
        compounded_alpha_positive=True,
        annual_breadth_passed=False,
        incremental_failed_years=[2025],
    ) == [
        "ANNUAL_BREADTH_WORSENED_UNDER_STRESS_COST",
        "ADDITIONAL_FAILED_YEARS_CREATED_BY_STRESS_COST",
    ]
    assert can_slim_validation.transaction_cost_stress_failure_attribution(
        compounded_alpha_positive=True,
        annual_breadth_passed=True,
        incremental_failed_years=[],
    ) == ["PASS"]


def test_trade_liquidity_capacity_uses_only_pre_execution_history():
    dates = pd.to_datetime([
        "2025-06-27", "2025-06-30", "2025-07-01"
    ])
    dollar_volume = pd.DataFrame(
        {"A": [10_000_000.0, 20_000_000.0, 1_000.0]}, index=dates
    )
    ledger = pd.DataFrame([{
        "trade_id": 1,
        "signal_date": dates[1],
        "execution_date": dates[2],
        "ticker": "A",
        "side": "BUY",
        "gross_notional": 250_000.0,
        "portfolio_value_after": 1_000_000.0,
    }])

    detail, summary = (
        can_slim_validation.trade_liquidity_capacity_diagnostics(
            ledger, dollar_volume, account_sizes=(1_000_000.0,)
        )
    )

    assert detail.iloc[0]["prior_50d_median_dollar_volume"] == 15_000_000.0
    assert detail.iloc[0][
        "participation_at_1000000_account"
    ] == pytest.approx(1 / 60)
    assert detail.iloc[0][
        "account_capacity_at_1pct_participation"
    ] == pytest.approx(600_000.0)
    assert summary["trades_missing_liquidity"] == 0


def test_donchian_uses_prior_window():
    df = pd.DataFrame({"high": [1, 2, 3, 4], "low": [0, 0, 0, 0], "close": [1, 2, 3, 4]})
    calculate_donchian_channel(df, window=2)
    assert df.loc[2, "donchian_upper"] == 2
    assert df.loc[2, "donchian_buy_signal"] == 1


def test_keltner_band_is_frozen_before_the_signal_close():
    dates = pd.bdate_range("2024-01-01", periods=25)
    close = pd.Series([10.0] * 24 + [20.0], index=dates)
    df = pd.DataFrame({"close": close, "high": close + 0.5, "low": close - 0.5})

    calculate_keltner_channel(df, window=20, atr_window=14, multiplier=1.5)

    assert df.loc[dates[-1], "keltner_middle"] == 10.0
    assert df.loc[dates[-1], "keltner_buy_signal"] == 1


def test_bollinger_does_not_fire_every_day_below_mean():
    close = pd.Series([10.0] * 20 + [8.0, 8.1, 8.2])
    df = pd.DataFrame({"close": close})
    calculate_bollinger_bands(df, window=20, num_std=1)
    assert df["bollinger_buy_signal"].sum() <= 1


def test_annual_returns_compounds_by_calendar_year():
    idx = pd.to_datetime(["2023-12-29", "2024-01-02"])
    result = pd.DataFrame({"strategy": [0.1, 0.2], "benchmark": [0.05, 0.1]}, index=idx)
    annual = annual_returns(result)
    assert annual.loc[2023, "strategy"] == pytest.approx(0.1)
    assert annual.loc[2024, "excess"] == pytest.approx(0.1)


def test_split_adjustment_removes_ten_for_one_price_jump():
    idx = pd.date_range("2025-01-01", periods=3, freq="B")
    close = pd.DataFrame({"NFLX": [1000.0, 100.0, 101.0]}, index=idx)
    adjusted = back_adjust_common_splits(close)
    assert adjusted.loc[idx[0], "NFLX"] == pytest.approx(100.0)
    assert adjusted.pct_change(fill_method=None).loc[idx[1], "NFLX"] == pytest.approx(0.0)


def test_sourced_reverse_split_supports_factor_above_heuristic_range():
    dates = pd.to_datetime(["2026-07-17", "2026-07-20"])
    close = pd.DataFrame({"PRPL": [0.30, 7.50]}, index=dates)
    actions = pd.DataFrame([{
        "ticker": "PRPL",
        "effective_date": "2026-07-20",
        "adjustment_factor": 25.0,
    }])

    adjusted = back_adjust_common_splits(
        close, confirmed_actions=actions
    )

    assert adjusted.loc[dates[0], "PRPL"] == 7.50
    assert adjusted.loc[dates[1], "PRPL"] == 7.50


def test_future_sourced_split_does_not_change_pre_event_panel():
    dates = pd.to_datetime(["2026-07-16", "2026-07-17"])
    close = pd.DataFrame({"PRPL": [0.29, 0.30]}, index=dates)
    actions = pd.DataFrame([{
        "ticker": "PRPL",
        "effective_date": "2026-07-20",
        "adjustment_factor": 25.0,
    }])

    adjusted = back_adjust_common_splits(
        close, confirmed_actions=actions
    )

    pd.testing.assert_frame_equal(adjusted, close)


def test_split_event_detection_exposes_heuristic_matches():
    idx = pd.date_range("2025-01-01", periods=3, freq="B")
    close = pd.DataFrame({
        "HALF": [100.0, 50.0, 51.0],
        "ORDINARY": [100.0, 70.0, 72.0],
    }, index=idx)

    events = detect_common_split_events(close)

    assert events[["ticker", "split_date"]].to_dict("records") == [{
        "ticker": "HALF",
        "split_date": idx[1],
    }]
    assert events.iloc[0]["matched_factor"] == 0.5


def test_confirmed_price_adjustments_require_explicit_factors():
    dates = pd.to_datetime(["2026-04-24", "2026-04-27", "2026-04-28"])
    close = pd.DataFrame({"VISN": [19.0, 19.53, 9.90]}, index=dates)
    actions = pd.DataFrame([{
        "ticker": "VISN",
        "effective_date": "2026-04-28",
        "adjustment_factor": (19.53 - 10.0) / 19.53,
    }])

    adjusted = apply_confirmed_price_adjustments(close, actions)

    assert adjusted.loc[pd.Timestamp("2026-04-27"), "VISN"] == pytest.approx(9.53)
    assert adjusted.loc[pd.Timestamp("2026-04-28"), "VISN"] == pytest.approx(9.90)


def test_confirmed_price_adjustments_reject_unknown_tickers():
    close = pd.DataFrame(
        {"ABC": [10.0, 5.0]},
        index=pd.to_datetime(["2025-01-01", "2025-01-02"]),
    )
    actions = pd.DataFrame([{
        "ticker": "MISSING",
        "effective_date": "2025-01-02",
        "adjustment_factor": 0.5,
    }])

    with pytest.raises(ValueError, match="unknown tickers"):
        apply_confirmed_price_adjustments(close, actions)


def test_restore_contemporaneous_prices_only_between_boundary_and_split():
    dates = pd.to_datetime([
        "2025-06-23", "2025-06-24", "2025-11-14", "2025-11-17"
    ])
    close = pd.DataFrame(
        {"NFLX": [1253.54, 127.911, 111.0, 11.2]}, index=dates
    )
    actions = pd.DataFrame([{
        "ticker": "NFLX",
        "split_date": "2025-06-24",
        "confirmed_action_date": "2025-11-17",
        "confirmed_action_type": "PROVIDER_ADJUSTMENT_DISCONTINUITY",
        "confirmed_adjustment_factor": 0.1,
    }])

    restored = restore_contemporaneous_prices(close, actions)

    assert restored.loc[dates[0], "NFLX"] == 1253.54
    assert restored.loc[dates[1], "NFLX"] == pytest.approx(1279.11)
    assert restored.loc[dates[2], "NFLX"] == pytest.approx(1110.0)
    assert restored.loc[dates[3], "NFLX"] == 11.2


def test_missing_prices_resume_from_last_trade_and_ended_history_is_penalized_once():
    idx = pd.date_range("2025-01-01", periods=5, freq="B")
    close = pd.DataFrame({
        "RESUMES": [10.0, np.nan, 11.0, 12.0, 13.0],
        "ENDS": [10.0, 9.0, 8.0, np.nan, np.nan],
        "NOT_LISTED": [np.nan, np.nan, 5.0, 6.0, 7.0],
    }, index=idx)
    returns = stock_returns_with_delisting_penalty(close)
    assert returns.loc[idx[1], "RESUMES"] == 0
    assert returns.loc[idx[2], "RESUMES"] == pytest.approx(0.1)
    assert returns.loc[idx[3], "ENDS"] == -1
    assert returns.loc[idx[4], "ENDS"] == 0
    assert pd.isna(returns.loc[idx[1], "NOT_LISTED"])
