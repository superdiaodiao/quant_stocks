import json

import pandas as pd

from scripts.sec_ticker_transition_probe import (
    _display_ticker_cik,
    _display_exact_name_cik,
    _missing_cik_tickers,
    _snapshot_name_queries,
)


def test_display_ticker_cik_accepts_multi_ticker_sec_display_name() -> None:
    display = "ClimateRock (CLRC, CLRCR, CLRCU, CLRCW) (CIK 0001903392)"
    assert _display_ticker_cik(display, "CLRC") == "0001903392"
    assert _display_ticker_cik(display, "CLR") is None


def test_exact_sourced_company_name_can_resolve_current_ticker_display() -> None:
    display = "Altus Midstream Co  (KNTK)  (CIK 0001692787)"
    assert _display_exact_name_cik(display, "Altus Midstream Company") == (
        "0001692787"
    )
    assert _display_exact_name_cik(display, "Unrelated Company") is None


def test_missing_cik_tickers_and_snapshot_company_name_queries(tmp_path) -> None:
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"records": [
        {"ticker": "OLD", "status": "MISSING_CIK_MAPPING"},
        {"ticker": "SAFE", "status": "RESEARCH_LEAD_ONLY"},
    ]}), encoding="utf-8")
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    pd.DataFrame({
        "Symbol": ["OLD"], "Name": ["Old Company, Inc. Common Stock"]
    }).to_csv(snapshots / "nasdaq_listed_2025-01-01.csv", index=False)
    pd.DataFrame({
        "Symbol": ["OLD"], "Name": ["Renamed Holdings - Class A Common Stock"]
    }).to_csv(snapshots / "nasdaq_listed_2025-02-01.csv", index=False)

    tickers = _missing_cik_tickers(triage)
    queries, sources = _snapshot_name_queries(snapshots, tickers)

    assert tickers == ["OLD"]
    assert queries == {"OLD": "Renamed Holdings"}
    assert sources["OLD"]["snapshot_date"] == "2025-02-01"
