import pandas as pd

from src.research import historical_data_audit


def _write_benchmark(path, dates, closes=None):
    pd.DataFrame({
        "date": dates,
        "close": closes if closes is not None else [100.0] * len(dates),
    }).to_csv(path, index=False)


def test_benchmark_calendar_accepts_every_official_session(tmp_path):
    path = tmp_path / "nasdaq.csv"
    sessions = [
        "2026-01-02",
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",
    ]
    _write_benchmark(path, sessions)

    report = historical_data_audit.audit_benchmark_calendar(
        "2026-01-02", "2026-01-09", path
    )

    assert report["complete"] is True
    assert report["expected_sessions"] == 6
    assert report["observed_unique_sessions"] == 6


def test_benchmark_calendar_rejects_missing_session(tmp_path):
    path = tmp_path / "nasdaq.csv"
    _write_benchmark(path, [
        "2026-01-02",
        "2026-01-05",
        "2026-01-06",
        "2026-01-08",
        "2026-01-09",
    ])

    report = historical_data_audit.audit_benchmark_calendar(
        "2026-01-02", "2026-01-09", path
    )

    assert report["complete"] is False
    assert report["missing_sessions"] == ["2026-01-07"]


def test_benchmark_calendar_rejects_duplicate_session(tmp_path):
    path = tmp_path / "nasdaq.csv"
    _write_benchmark(path, [
        "2026-01-02",
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",
    ])

    report = historical_data_audit.audit_benchmark_calendar(
        "2026-01-02", "2026-01-09", path
    )

    assert report["complete"] is False
    assert report["duplicate_dates"] == ["2026-01-07"]


def test_benchmark_calendar_rejects_non_session_row(tmp_path):
    path = tmp_path / "nasdaq.csv"
    _write_benchmark(path, [
        "2026-01-02",
        "2026-01-03",
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",
    ])

    report = historical_data_audit.audit_benchmark_calendar(
        "2026-01-02", "2026-01-09", path
    )

    assert report["complete"] is False
    assert report["non_session_rows"] == ["2026-01-03"]


def test_benchmark_calendar_rejects_invalid_close(tmp_path):
    path = tmp_path / "nasdaq.csv"
    sessions = [
        "2026-01-02",
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",
    ]
    _write_benchmark(path, sessions, [100.0, 101.0, "bad", 102.0, 103.0, 104.0])

    report = historical_data_audit.audit_benchmark_calendar(
        "2026-01-02", "2026-01-09", path
    )

    assert report["complete"] is False
    assert report["invalid_close_dates"] == ["2026-01-06"]


def test_snapshot_price_coverage_requires_current_price_and_full_lookback(
    tmp_path, monkeypatch
):
    dates = pd.date_range("2024-01-01", periods=260, freq="B")
    pd.DataFrame({"date": dates, "close": 10.0}).to_csv(tmp_path / "a.csv", index=False)
    pd.DataFrame({"date": dates[-10:], "close": 20.0}).to_csv(tmp_path / "b.csv", index=False)
    monkeypatch.setattr(historical_data_audit, "CLEANED_PRICE_DATA_DIR", tmp_path)
    observed_at = dates[-1]
    report = historical_data_audit.audit_snapshot_price_coverage(
        {observed_at: {"A", "B", "C"}}, start=str(observed_at.date())
    )
    row = report["by_snapshot"][0]
    assert row["price_current"] == 2
    assert row["lookback_ready"] == 1
    assert report["missing_price_symbols"] == ["C"]
    assert not report["complete"]


def test_snapshot_price_coverage_reuses_supplied_date_metadata(
    monkeypatch,
):
    observed_at = pd.Timestamp("2026-01-30")
    metadata = {
        "A": pd.Series(pd.date_range(
            "2025-01-01", observed_at, freq="B"
        ))
    }

    def fail_if_reloaded():
        raise AssertionError("price calendars were reloaded")

    monkeypatch.setattr(
        historical_data_audit,
        "load_price_date_metadata",
        fail_if_reloaded,
    )

    report = historical_data_audit.audit_snapshot_price_coverage(
        {observed_at: {"A"}},
        start="2026-01-30",
        price_date_metadata=metadata,
    )

    assert report["complete"]
    assert report["by_snapshot"][0]["price_current"] == 1


def test_known_price_row_count_includes_same_day_and_duplicate_rows():
    dates = pd.Series(pd.to_datetime([
        "2026-01-02",
        "2026-01-05",
        "2026-01-05",
        "2026-01-07",
    ]))

    assert historical_data_audit._known_price_row_count(
        dates, pd.Timestamp("2026-01-01")
    ) == 0
    assert historical_data_audit._known_price_row_count(
        dates, pd.Timestamp("2026-01-05")
    ) == 3
    assert historical_data_audit._known_price_row_count(
        dates, pd.Timestamp("2026-01-06")
    ) == 3
    assert historical_data_audit._known_price_row_count(
        dates, pd.Timestamp("2026-01-31")
    ) == 4


def test_signal_price_coverage_uses_actual_month_end_signal(
    tmp_path, monkeypatch
):
    dates = pd.bdate_range("2025-01-01", "2025-01-31")
    pd.DataFrame({"date": dates, "close": 10.0}).to_csv(
        tmp_path / "a.csv", index=False
    )
    pd.DataFrame({"date": dates[:-10], "close": 20.0}).to_csv(
        tmp_path / "b.csv", index=False
    )
    benchmark = tmp_path / "nasdaq.csv"
    pd.DataFrame({"date": dates, "close": 100.0}).to_csv(
        benchmark, index=False
    )
    monkeypatch.setattr(
        historical_data_audit, "CLEANED_PRICE_DATA_DIR", tmp_path
    )
    monkeypatch.setattr(
        historical_data_audit, "NASDAQ_INDEX_FILE", benchmark
    )
    monkeypatch.setattr(
        historical_data_audit,
        "quarterly_growth_snapshot",
        lambda *_args, **_kwargs: pd.DataFrame(
            {
                "net_income_ttm": [10.0],
                "net_income_growth": [0.5],
                "revenue_growth": [0.2],
            },
            index=["B"],
        ),
    )

    report = historical_data_audit.audit_signal_price_coverage(
        {pd.Timestamp("2025-01-01"): {"A", "B"}},
        "2025-01-01",
        "2025-01-31",
        quarterly_fundamentals=pd.DataFrame({"dummy": [1]}),
        confirmed_listings=pd.DataFrame({
            "ticker": ["B"],
            "first_trading_date": ["2025-01-20"],
        }),
    )

    assert report["signal_count"] == 1
    assert report["by_signal"][0]["signal_date"] == "2025-01-31"
    assert report["by_signal"][0]["price_current"] == 1
    assert report["by_signal"][0]["stale_or_ended_history_count"] == 1
    assert report["by_signal"][0]["absent_price_file_count"] == 0
    assert report["by_signal"][0]["history_starts_after_signal_count"] == 0
    assert report["by_signal"][0][
        "missing_passing_financial_screen_count"
    ] == 1
    assert report["by_signal"][0][
        "missing_passing_financial_screen_symbols"
    ] == "B"
    assert report["missing_passing_financial_screen_symbols"] == ["B"]
    assert report[
        "confirmed_insufficient_listing_history_symbols"
    ] == ["B"]
    assert report[
        "unresolved_observable_potential_competitor_symbols"
    ] == []
    assert report["missing_price_symbols"] == ["B"]
    assert report["usable_pit_financial_growth_complete"] is False
    assert report[
        "missing_usable_pit_financial_growth_symbols"
    ] == ["A"]
    assert report["missing_no_raw_pit_financial_facts_symbols"] == ["A"]
    assert report["missing_insufficient_financial_history_symbols"] == []
    assert report["missing_stale_financial_growth_symbols"] == []
    assert report[
        "minimum_usable_pit_financial_growth_coverage"
    ] == 0.5
    assert report["missing_never_with_pit_financial_data_symbols"] == []
    assert report[
        "missing_with_and_without_pit_financial_data_symbols"
    ] == []
    assert report["missing_with_pit_financial_data_details"] == [{
        "signal_date": "2025-01-31",
        "ticker": "B",
        "price_gap_type": "stale_or_ended_history",
        "net_income_ttm": 10.0,
        "net_income_growth": 0.5,
        "revenue_growth": 0.2,
        "financial_age_days": None,
        "passes_positive_profit": True,
        "passes_profit_growth": True,
        "passes_revenue_growth": True,
        "passes_financial_screen": True,
        "first_price_date": "2025-01-01",
        "first_trading_date": "2025-01-20",
        "benchmark_sessions_since_listing": 10,
        "listing_history_sufficient": False,
        "final_observable_classification": (
            "confirmed_insufficient_listing_history"
        ),
    }]


def test_signal_price_coverage_follows_confirmed_issuer_rename(
    tmp_path, monkeypatch
):
    benchmark = tmp_path / "nasdaq.csv"
    dates = pd.bdate_range("2026-01-01", "2026-01-30")
    pd.DataFrame({"date": dates, "close": 100.0}).to_csv(
        benchmark, index=False
    )
    monkeypatch.setattr(historical_data_audit, "NASDAQ_INDEX_FILE", benchmark)
    monkeypatch.setattr(
        historical_data_audit,
        "load_security_identity",
        lambda: pd.DataFrame([{
            "provider_ticker": "NEW",
            "historical_ticker": "OLD",
            "last_historical_date": pd.Timestamp("2026-01-13"),
            "current_ticker_first_date": pd.Timestamp("2026-01-14"),
            "identity_type": "issuer_rename",
        }]),
    )

    report = historical_data_audit.audit_signal_price_coverage(
        {pd.Timestamp("2026-01-01"): {"OLD"}},
        "2026-01-01",
        "2026-01-30",
        minimum_lookback_rows=1,
        price_date_metadata={"NEW": pd.Series(dates)},
        observed_terminal_returns=pd.DataFrame(),
    )

    assert report["by_signal"][0]["price_current"] == 1
    assert report["missing_price_symbols"] == []


def test_signal_price_coverage_excludes_confirmed_terminal_from_competitors(
    tmp_path, monkeypatch
):
    benchmark = tmp_path / "nasdaq.csv"
    dates = pd.bdate_range("2026-01-01", "2026-01-30")
    pd.DataFrame({"date": dates, "close": 100.0}).to_csv(
        benchmark, index=False
    )
    monkeypatch.setattr(historical_data_audit, "NASDAQ_INDEX_FILE", benchmark)
    monkeypatch.setattr(
        historical_data_audit, "load_security_identity", lambda: pd.DataFrame()
    )
    monkeypatch.setattr(
        historical_data_audit,
        "quarterly_growth_snapshot",
        lambda *_args, **_kwargs: pd.DataFrame({
            "net_income_ttm": [10.0],
            "net_income_growth": [0.5],
            "revenue_growth": [0.2],
        }, index=["DEAD"]),
    )
    observed = pd.DataFrame([{
        "ticker": "DEAD",
        "last_price_date": "2026-01-09",
        "terminal_return": 0.1,
    }])

    report = historical_data_audit.audit_signal_price_coverage(
        {pd.Timestamp("2026-01-01"): {"DEAD"}},
        "2026-01-01",
        "2026-01-30",
        minimum_lookback_rows=1,
        quarterly_fundamentals=pd.DataFrame({"dummy": [1]}),
        price_date_metadata={
            "DEAD": pd.Series(pd.bdate_range("2026-01-01", "2026-01-09"))
        },
        observed_terminal_returns=observed,
    )

    assert report["by_signal"][0]["confirmed_terminal_before_signal_count"] == 1
    assert report["confirmed_terminal_before_signal_symbols"] == ["DEAD"]
    assert report["unresolved_observable_potential_competitor_symbols"] == []
    assert report["missing_with_pit_financial_data_details"][0][
        "final_observable_classification"
    ] == "confirmed_terminal_before_signal"


def test_signal_price_coverage_accepts_completion_date_without_known_return(
    tmp_path, monkeypatch
):
    benchmark = tmp_path / "nasdaq.csv"
    dates = pd.bdate_range("2026-05-01", "2026-05-29")
    pd.DataFrame({"date": dates, "close": 100.0}).to_csv(
        benchmark, index=False
    )
    monkeypatch.setattr(historical_data_audit, "NASDAQ_INDEX_FILE", benchmark)
    monkeypatch.setattr(
        historical_data_audit, "load_security_identity", lambda: pd.DataFrame()
    )
    monkeypatch.setattr(
        historical_data_audit,
        "quarterly_growth_snapshot",
        lambda *_args, **_kwargs: pd.DataFrame({
            "net_income_ttm": [10.0],
            "net_income_growth": [0.5],
            "revenue_growth": [0.2],
        }, index=["APLS"]),
    )

    report = historical_data_audit.audit_signal_price_coverage(
        {pd.Timestamp("2026-05-01"): {"APLS"}},
        "2026-05-01",
        "2026-05-29",
        minimum_lookback_rows=1,
        quarterly_fundamentals=pd.DataFrame({"dummy": [1]}),
        price_date_metadata={
            "APLS": pd.Series(pd.bdate_range("2026-05-01", "2026-05-13"))
        },
        observed_terminal_returns=pd.DataFrame(),
        confirmed_terminal_dates=pd.DataFrame([{
            "ticker": "APLS",
            "terminal_date": "2026-05-14",
        }]),
    )

    assert report["confirmed_terminal_before_signal_symbols"] == ["APLS"]
    assert report["unresolved_observable_potential_competitor_symbols"] == []
    assert report["missing_with_pit_financial_data_details"][0][
        "final_observable_classification"
    ] == "confirmed_terminal_before_signal"


def test_apls_confirmed_terminal_registry_is_sec_payload_bound() -> None:
    frame = historical_data_audit.load_confirmed_terminal_dates()
    row = frame.set_index("ticker").loc["APLS"]

    assert row["terminal_date"] == pd.Timestamp("2026-05-14")
    assert row["event_type"] == "MERGER_COMPLETED"
    assert row["evidence_payload_sha256"] == (
        "52bac6231c8fc70058ee30d25934f70ad5c4886fcea4cf346eca60fcc79ad4d6"
    )


def test_signal_financial_coverage_is_independent_of_price_coverage(
    tmp_path, monkeypatch
):
    dates = pd.bdate_range("2025-01-01", "2025-01-31")
    for ticker in ("a", "b"):
        pd.DataFrame({"date": dates, "close": 10.0}).to_csv(
            tmp_path / f"{ticker}.csv", index=False
        )
    benchmark = tmp_path / "nasdaq.csv"
    pd.DataFrame({"date": dates, "close": 100.0}).to_csv(
        benchmark, index=False
    )
    monkeypatch.setattr(
        historical_data_audit, "CLEANED_PRICE_DATA_DIR", tmp_path
    )
    monkeypatch.setattr(
        historical_data_audit, "NASDAQ_INDEX_FILE", benchmark
    )
    monkeypatch.setattr(
        historical_data_audit,
        "quarterly_growth_snapshot",
        lambda *_args, **_kwargs: pd.DataFrame(
            {
                "net_income_ttm": [10.0, 20.0],
                "net_income_growth": [0.5, 0.6],
                "revenue_growth": [0.2, 0.3],
            },
            index=["A", "B"],
        ),
    )

    complete = historical_data_audit.audit_signal_price_coverage(
        {pd.Timestamp("2025-01-01"): {"A", "B"}},
        "2025-01-01",
        "2025-01-31",
        quarterly_fundamentals=pd.DataFrame({"dummy": [1]}),
    )
    assert complete["complete"] is True
    assert complete["usable_pit_financial_growth_complete"] is True
    assert complete["missing_usable_pit_financial_growth_symbols"] == []

    monkeypatch.setattr(
        historical_data_audit,
        "quarterly_growth_snapshot",
        lambda *_args, **_kwargs: pd.DataFrame(
            {
                "net_income_ttm": [10.0],
                "net_income_growth": [0.5],
                "revenue_growth": [0.2],
            },
            index=["A"],
        ),
    )
    incomplete = historical_data_audit.audit_signal_price_coverage(
        {pd.Timestamp("2025-01-01"): {"A", "B"}},
        "2025-01-01",
        "2025-01-31",
        quarterly_fundamentals=pd.DataFrame({"dummy": [1]}),
    )
    assert incomplete["complete"] is True
    assert incomplete["usable_pit_financial_growth_complete"] is False
    assert incomplete["missing_usable_pit_financial_growth_symbols"] == ["B"]
    assert incomplete["missing_no_raw_pit_financial_facts_symbols"] == ["B"]


def test_signal_financial_gaps_are_split_into_mutually_exclusive_reasons(
    tmp_path, monkeypatch
):
    dates = pd.bdate_range("2025-01-01", "2025-01-31")
    for ticker in ("usable", "stale", "insuff", "noraw"):
        pd.DataFrame({"date": dates, "close": 10.0}).to_csv(
            tmp_path / f"{ticker}.csv", index=False
        )
    benchmark = tmp_path / "nasdaq.csv"
    pd.DataFrame({"date": dates, "close": 100.0}).to_csv(
        benchmark, index=False
    )
    monkeypatch.setattr(
        historical_data_audit, "CLEANED_PRICE_DATA_DIR", tmp_path
    )
    monkeypatch.setattr(
        historical_data_audit, "NASDAQ_INDEX_FILE", benchmark
    )

    def snapshot(_fundamentals, _signal_date, maximum_age_days):
        tickers = ["USABLE", "STALE"] if maximum_age_days > 550 else ["USABLE"]
        return pd.DataFrame(
            {
                "net_income_ttm": [10.0] * len(tickers),
                "net_income_growth": [0.5] * len(tickers),
                "revenue_growth": [0.2] * len(tickers),
            },
            index=tickers,
        )

    monkeypatch.setattr(
        historical_data_audit, "quarterly_growth_snapshot", snapshot
    )
    raw = pd.DataFrame({
        "ticker": ["USABLE", "STALE", "INSUFF"],
        "available_date": pd.to_datetime(["2025-01-15"] * 3),
        "metric": ["net_income", "revenue", "net_income"],
    })
    report = historical_data_audit.audit_signal_price_coverage(
        {
            pd.Timestamp("2025-01-01"): {
                "USABLE", "STALE", "INSUFF", "NORAW"
            }
        },
        "2025-01-01",
        "2025-01-31",
        quarterly_fundamentals=raw,
    )

    assert report["missing_no_raw_pit_financial_facts_symbols"] == ["NORAW"]
    assert report["missing_insufficient_financial_history_symbols"] == [
        "INSUFF"
    ]
    assert report["missing_stale_financial_growth_symbols"] == ["STALE"]
    assert report["missing_financial_metric_gap_counts"] == {
        "NO_NET_INCOME_FACT": {"STALE": 1},
        "NO_REVENUE_AND_NET_INCOME_FACTS": {"NORAW": 1},
        "NO_REVENUE_FACT": {"INSUFF": 1},
    }
    assert report["missing_financial_metric_gap_observations"] == [
        {
            "signal_date": "2025-01-31",
            "ticker": "INSUFF",
            "missing_raw_pit_metrics": "revenue",
            "classification": "NO_REVENUE_FACT",
        },
        {
            "signal_date": "2025-01-31",
            "ticker": "NORAW",
            "missing_raw_pit_metrics": "net_income|revenue",
            "classification": "NO_REVENUE_AND_NET_INCOME_FACTS",
        },
        {
            "signal_date": "2025-01-31",
            "ticker": "STALE",
            "missing_raw_pit_metrics": "net_income",
            "classification": "NO_NET_INCOME_FACT",
        },
    ]
    row = report["by_signal"][0]
    assert row["missing_no_raw_pit_financial_facts_count"] == 1
    assert row["missing_insufficient_financial_history_count"] == 1
    assert row["missing_stale_financial_growth_count"] == 1


def test_signal_price_coverage_explains_each_financial_threshold(
    tmp_path, monkeypatch
):
    dates = pd.bdate_range("2025-01-01", "2025-01-31")
    benchmark = tmp_path / "nasdaq.csv"
    pd.DataFrame({"date": dates, "close": 100.0}).to_csv(
        benchmark, index=False
    )
    monkeypatch.setattr(
        historical_data_audit, "CLEANED_PRICE_DATA_DIR", tmp_path
    )
    monkeypatch.setattr(
        historical_data_audit, "NASDAQ_INDEX_FILE", benchmark
    )
    monkeypatch.setattr(
        historical_data_audit,
        "quarterly_growth_snapshot",
        lambda *_args, **_kwargs: pd.DataFrame(
            {
                "net_income_ttm": [-1.0, 10.0, 10.0],
                "net_income_growth": [0.5, 0.2, 0.5],
                "revenue_growth": [0.2, 0.2, 0.05],
                "financial_age_days": [30, 40, 50],
            },
            index=["LOSS", "SLOW_PROFIT", "SLOW_REVENUE"],
        ),
    )

    report = historical_data_audit.audit_signal_price_coverage(
        {
            pd.Timestamp("2025-01-01"): {
                "LOSS", "SLOW_PROFIT", "SLOW_REVENUE"
            }
        },
        "2025-01-01",
        "2025-01-31",
        quarterly_fundamentals=pd.DataFrame({"dummy": [1]}),
    )

    details = {
        row["ticker"]: row
        for row in report["missing_with_pit_financial_data_details"]
    }
    assert details["LOSS"]["passes_positive_profit"] is False
    assert details["LOSS"]["passes_profit_growth"] is True
    assert details["SLOW_PROFIT"]["passes_profit_growth"] is False
    assert details["SLOW_REVENUE"]["passes_revenue_growth"] is False
    assert {
        row["final_observable_classification"] for row in details.values()
    } == {"fails_financial_screen"}
    assert report[
        "unresolved_observable_potential_competitor_symbols"
    ] == []
    assert report["missing_never_with_pit_financial_data_symbols"] == []


def test_signal_price_coverage_distinguishes_never_from_sometimes_financial(
    tmp_path, monkeypatch
):
    dates = pd.bdate_range("2025-01-01", "2025-02-28")
    benchmark = tmp_path / "nasdaq.csv"
    pd.DataFrame({"date": dates, "close": 100.0}).to_csv(
        benchmark, index=False
    )
    monkeypatch.setattr(
        historical_data_audit, "CLEANED_PRICE_DATA_DIR", tmp_path
    )
    monkeypatch.setattr(
        historical_data_audit, "NASDAQ_INDEX_FILE", benchmark
    )

    def snapshot(_fundamentals, signal_date, _maximum_age_days):
        if signal_date.month == 1:
            return pd.DataFrame(
                {
                    "net_income_ttm": [10.0],
                    "net_income_growth": [0.0],
                    "revenue_growth": [0.0],
                },
                index=["SOMETIMES"],
            )
        return pd.DataFrame()

    monkeypatch.setattr(
        historical_data_audit, "quarterly_growth_snapshot", snapshot
    )
    report = historical_data_audit.audit_signal_price_coverage(
        {
            pd.Timestamp("2025-01-01"): {"NEVER", "SOMETIMES"},
        },
        "2025-01-01",
        "2025-02-28",
        quarterly_fundamentals=pd.DataFrame({"dummy": [1]}),
    )

    assert report["missing_with_pit_financial_data_symbols"] == [
        "SOMETIMES"
    ]
    assert report["missing_without_pit_financial_data_symbols"] == [
        "NEVER", "SOMETIMES"
    ]
    assert report["missing_never_with_pit_financial_data_symbols"] == [
        "NEVER"
    ]
    assert report[
        "missing_with_and_without_pit_financial_data_symbols"
    ] == ["SOMETIMES"]
    assert report["pit_gap_priorities"] == [{
        "ticker": "NEVER",
        "provider_ticker": "NEVER",
        "security_identity_type": None,
        "security_identity_source_url": None,
        "missing_signal_count": 2,
        "first_missing_signal_date": "2025-01-31",
        "last_missing_signal_date": "2025-02-28",
        "absent_price_file_signal_count": 2,
        "history_starts_after_signal_count": 0,
        "stale_or_ended_history_count": 0,
        "internal_price_gap_at_signal_count": 0,
        "no_raw_pit_financial_facts_signal_count": 2,
        "insufficient_growth_history_signal_count": 0,
        "stale_growth_snapshot_signal_count": 0,
        "observed_price_file_first_date": None,
        "observed_price_file_last_date": None,
        "remediation_scope": "ACQUIRE_PRICE_AND_PIT_FINANCIAL",
        "priority_rank": 1,
        "recovery_priority_rank": 1,
    }]


def test_signal_price_coverage_distinguishes_internal_price_gap(
    tmp_path, monkeypatch
):
    benchmark_dates = pd.bdate_range("2025-01-01", "2025-02-28")
    observed_dates = benchmark_dates[
        (benchmark_dates <= "2025-01-10")
        | (benchmark_dates >= "2025-02-03")
    ]
    pd.DataFrame({"date": observed_dates, "close": 10.0}).to_csv(
        tmp_path / "gap.csv", index=False
    )
    benchmark = tmp_path / "nasdaq.csv"
    pd.DataFrame({
        "date": benchmark_dates, "close": 100.0
    }).to_csv(benchmark, index=False)
    monkeypatch.setattr(
        historical_data_audit, "CLEANED_PRICE_DATA_DIR", tmp_path
    )
    monkeypatch.setattr(
        historical_data_audit, "NASDAQ_INDEX_FILE", benchmark
    )

    report = historical_data_audit.audit_signal_price_coverage(
        {pd.Timestamp("2025-01-01"): {"GAP"}},
        "2025-01-01",
        "2025-02-28",
    )

    assert report["internal_price_gap_at_signal_symbols"] == ["GAP"]
    assert report["stale_or_ended_history_symbols"] == []
    priority = report["pit_gap_priorities"][0]
    assert priority["internal_price_gap_at_signal_count"] == 1
    assert priority["remediation_scope"] == (
        "FILL_INTERNAL_PRICE_GAPS_PLUS_PIT_FINANCIAL"
    )


def test_signal_membership_coverage_uses_signal_date_snapshot_age(
    tmp_path, monkeypatch
):
    dates = pd.bdate_range("2023-05-01", "2023-05-31")
    pd.DataFrame({"date": dates, "close": 10.0}).to_csv(
        tmp_path / "a.csv", index=False
    )
    benchmark = tmp_path / "nasdaq.csv"
    pd.DataFrame({"date": dates, "close": 100.0}).to_csv(
        benchmark, index=False
    )
    monkeypatch.setattr(
        historical_data_audit, "CLEANED_PRICE_DATA_DIR", tmp_path
    )
    monkeypatch.setattr(
        historical_data_audit, "NASDAQ_INDEX_FILE", benchmark
    )

    report = historical_data_audit.audit_signal_price_coverage(
        {pd.Timestamp("2023-04-18"): {"A"}},
        "2023-05-01",
        "2023-05-31",
        maximum_signal_snapshot_age_days=40,
    )

    assert report["maximum_observed_signal_snapshot_age_days"] == 43
    assert report["stale_signal_snapshot_dates"] == ["2023-05-31"]
    assert report["signal_membership_snapshots_complete"] is False
def test_snapshot_coverage_diagnostics_lists_internal_gaps():
    snapshots = {
        pd.Timestamp("2025-01-01"): {"A"},
        pd.Timestamp("2025-01-20"): {"A"},
        pd.Timestamp("2025-03-15"): {"A"},
        pd.Timestamp("2025-03-31"): {"A"},
    }

    report = historical_data_audit.snapshot_coverage_diagnostics(
        snapshots,
        "2025-01-01",
        "2025-03-31",
        maximum_snapshot_gap_days=40,
    )

    assert report["relevant_snapshot_count"] == 4
    assert report["gaps_over_limit_count"] == 1
    assert report["gaps_over_limit"] == [{
        "left_date": "2025-01-20",
        "right_date": "2025-03-15",
        "gap_days": 54,
        "gap_type": "between_snapshots",
    }]
    assert not report["full_period_covered"]
