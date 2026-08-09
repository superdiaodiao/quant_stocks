import pandas as pd

from src.io.financial_update import (
    audit_financial_coverage,
    investable_common_equities,
    merge_point_in_time_eps,
)


def test_non_common_securities_are_removed_from_investable_universe():
    universe = pd.DataFrame({
        "Symbol": ["A", "AP", "AW"],
        "Name": ["A Corp Common Stock", "A Corp Preferred Stock", "A Corp Warrant"],
    })
    assert investable_common_equities(universe)["Symbol"].tolist() == ["A"]


def test_spelled_out_exchange_traded_notes_are_removed():
    universe = pd.DataFrame({
        "Symbol": ["GLDI", "SLVO", "COMMON"],
        "Name": [
            "Issuer Gold Covered Call Exchange Traded Notes",
            "Issuer Silver Covered Call ETNs due April 21, 2033",
            "Example Common Stock",
        ],
        "ETF": ["N", "N", "N"],
    })

    assert investable_common_equities(universe)["Symbol"].tolist() == [
        "COMMON"
    ]


def test_security_filter_does_not_confuse_company_words_with_units_or_rights():
    universe = pd.DataFrame({
        "Symbol": ["UAL", "BRLT", "W"],
        "Name": [
            "United Airlines Holdings, Inc. Common Stock",
            "Brilliant Earth Group, Inc. Class A Common Stock",
            "Example Corp. Warrant",
        ],
    })
    assert investable_common_equities(universe)["Symbol"].tolist() == ["UAL", "BRLT"]


def test_preference_and_bare_depositary_shares_are_removed_but_adrs_remain():
    universe = pd.DataFrame({
        "Symbol": ["ADR", "PREF", "DEP"],
        "Name": [
            "Issuer plc - American Depositary Shares",
            "Issuer - Depository Shares 7.00% Perpetual Preference Shares",
            "Bank Corp. - Depositary Shares",
        ],
    })

    assert investable_common_equities(universe)["Symbol"].tolist() == ["ADR"]


def test_singular_depositary_share_representing_preferred_is_removed():
    universe = pd.DataFrame({
        "Symbol": ["FITBI", "COMMON"],
        "Name": [
            "Fifth Third Bancorp - Depositary Share repstg 1/1000th Ownership Interest Perp Pfd Series I",
            "Operating Company Common Stock",
        ],
    })
    assert investable_common_equities(universe)["Symbol"].tolist() == ["COMMON"]


def test_when_issued_and_spac_common_shares_are_removed():
    universe = pd.DataFrame({
        "Symbol": ["LIVE", "WI", "SPAC"],
        "Name": [
            "Operating Company Common Stock",
            "Operating Company Common Stock When Issued",
            "Example Acquisition II Corp. - Class A Ordinary Shares",
        ],
    })

    assert investable_common_equities(universe)["Symbol"].tolist() == ["LIVE"]


def test_additional_blank_check_company_name_variants_are_removed():
    universe = pd.DataFrame({
        "Symbol": ["LIVE", "ACQ", "MERGER", "CAPITAL", "GROWTH", "EQUITY"],
        "Name": [
            "Operating Company Class A Ordinary Shares",
            "99 Acquisition Group Inc. - Class A Common Stock",
            "Trailblazer Merger Corporation I - Class A Common Stock",
            "Perception Capital Corp. III - Class A Ordinary Share",
            "Cartesian Growth Corporation - Class A Ordinary Share",
            "Cantor Equity Partners III, Inc. - Class A Ordinary Shares",
        ],
    })
    assert investable_common_equities(universe)["Symbol"].tolist() == ["LIVE"]


def test_funds_and_limited_partnerships_are_not_common_equities():
    universe = pd.DataFrame({
        "Symbol": ["LIVE", "FUND", "TRUST", "LP"],
        "Name": [
            "Operating Company Common Stock",
            "Example Strategic Total Return Common Stock",
            "Example Opportunities Trust Common Stock",
            "Example Enterprises, L.P. Common Stock",
        ],
    })

    assert investable_common_equities(universe)["Symbol"].tolist() == ["LIVE"]


def test_exact_report_date_replaces_conservative_legacy_row():
    legacy = pd.DataFrame([{
        "ticker": "A", "period_end": "2025-03-31", "available_date": "2025-05-30",
        "quarterly_eps": 1.0, "source": "legacy_conservative_60d_lag", "fetched_at": pd.NaT,
    }])
    exact = pd.DataFrame([{
        "ticker": "A", "period_end": "2025-03-31", "available_date": "2025-05-01",
        "quarterly_eps": 1.1, "source": "sec_companyfacts", "fetched_at": "2026-07-18",
    }])
    merged = merge_point_in_time_eps(legacy, exact)
    assert len(merged) == 1
    assert merged.loc[0, "available_date"] == pd.Timestamp("2025-05-01")
    assert merged.loc[0, "quarterly_eps"] == 1.1


def test_coverage_counts_only_information_available_and_fresh():
    frame = pd.DataFrame({
        "ticker": ["A", "B", "C"],
        "available_date": pd.to_datetime(
            ["2026-05-01", "2025-01-01", "2025-01-01"]
        ),
        "exact_report_date": [True, True, False],
    })
    audit = audit_financial_coverage(
        frame, ["A", "B", "C", "D"], pd.Timestamp("2026-07-17").date()
    )
    assert audit["fresh_tickers"] == 1
    assert audit["fresh_coverage"] == 0.25
    assert audit["tickers_with_exact_recent_report_date"] == 1
    assert audit["exact_recent_report_date_coverage"] == 0.25
    assert audit["missing"] == ["D"]
    assert audit["stale"] == ["B", "C"]
