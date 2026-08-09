import pandas as pd

from scripts.survivorship_audit import audit_survivorship_by_year


def test_survivorship_audit_reports_later_absence_and_price_coverage():
    snapshots = {
        pd.Timestamp("2021-12-31"): {"KEEP", "DROP"},
        pd.Timestamp("2022-12-30"): {"KEEP"},
    }
    benchmark = pd.Series(pd.to_datetime([
        "2021-12-31", "2022-01-03", "2022-12-30",
    ]))
    prices = {
        "DROP": pd.Series(pd.to_datetime(["2021-12-31", "2022-01-03"])),
        "KEEP": pd.Series(pd.to_datetime([
            "2021-12-31", "2022-01-03", "2022-12-30",
        ])),
    }

    report = audit_survivorship_by_year(
        snapshots, benchmark, prices, start="2021-01-01"
    )

    row = report["years"][0]
    assert row["later_absent_proxy_count"] == 1
    assert row["price_file_count"] == 1
    assert row["price_complete_count"] == 1
    assert row["price_session_coverage"] == 1.0
    assert row["later_absent_proxy_sample"] == ["DROP"]
    assert row["unresolved_later_absence_proxy_count"] == 1
    assert row["evidence_breakdown"]["unresolved_later_absence_proxy"][
        "price_complete_count"
    ] == 1


def test_survivorship_audit_separates_sec_terminal_and_rename_evidence():
    snapshots = {
        pd.Timestamp("2021-12-31"): {"EXIT", "RENAMED", "UNRESOLVED"},
        pd.Timestamp("2022-01-03"): {"NEW"},
        pd.Timestamp("2022-12-30"): {"NEW"},
    }
    benchmark = pd.Series(pd.to_datetime([
        "2021-12-31", "2022-01-03", "2022-12-30",
    ]))
    prices = {
        ticker: pd.Series(pd.to_datetime([
            "2021-12-31", "2022-01-03",
        ]))
        for ticker in ("EXIT", "RENAMED", "UNRESOLVED", "NEW")
    }
    terminal_returns = pd.DataFrame({
        "ticker": ["EXIT", "UNRESOLVED"],
        "last_price_date": ["2022-01-03", "2022-01-03"],
        "source_url": [
            "https://www.sec.gov/Archives/edgar/data/1/exit.htm",
            "https://example.test/unresolved",
        ],
    })
    issuer_renames = pd.DataFrame({
        "provider_ticker": ["NEW"],
        "historical_ticker": ["RENAMED"],
        "current_ticker_first_date": ["2022-01-01"],
        "identity_type": ["issuer_rename"],
    })

    report = audit_survivorship_by_year(
        snapshots,
        benchmark,
        prices,
        start="2021-01-01",
        terminal_returns=terminal_returns,
        issuer_renames=issuer_renames,
    )

    row = report["years"][0]
    assert row["later_absent_proxy_count"] == 3
    assert row["official_sec_terminal_return_count"] == 1
    assert row["sourced_issuer_rename_transition_count"] == 1
    assert row["unresolved_later_absence_proxy_count"] == 1
    assert row["evidence_breakdown"]["official_sec_terminal_return"][
        "sample"
    ] == ["EXIT"]
    assert row["evidence_breakdown"]["sourced_issuer_rename_transition"][
        "sample"
    ] == ["RENAMED"]
    assert row["evidence_breakdown"]["unresolved_later_absence_proxy"][
        "sample"
    ] == ["UNRESOLVED"]
