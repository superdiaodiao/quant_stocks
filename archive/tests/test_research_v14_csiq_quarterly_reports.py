from pathlib import Path

import pandas as pd
import pytest

from scripts.research_v14_csiq_quarterly_reports import (
    EXPECTED_ENDS,
    MAX_RECONCILIATION_DELTA_USD,
    _annual_reconciliation,
    _period_columns,
    _row_value,
)


def test_csiq_parser_selects_direct_consolidated_gaap_rows() -> None:
    table = pd.DataFrame([
        [None, "Three months ended", "Three months ended"],
        [None, "March 31, 2021", "March 31, 2020"],
        ["Net revenues", "1,089,339", "825,635"],
        ["Net income (loss)", "13,596", "111,247"],
        ["Non-GAAP net income", "22,000", "118,000"],
    ])
    columns = _period_columns(
        table,
        fiscal_end=pd.Timestamp("2021-03-31"),
        period_phrase="Three months ended",
    )
    assert columns == [1]
    assert _row_value(
        table, labels=("Net revenues",), columns=columns
    ) == ("Net revenues", 1_089_339)
    assert _row_value(
        table, labels=("Net income (loss)", "Net income"), columns=columns
    ) == ("Net income (loss)", 13_596)


def test_csiq_annual_reconciliation_retains_two_thousand_dollar_disclosure_gap() -> None:
    frame = pd.DataFrame({
        "fiscal_end": pd.to_datetime([
            "2017-03-31", "2017-06-30", "2017-09-30", "2017-12-31"
        ]),
        "revenue": [677_042_000, 692_366_000, 912_223_000, 1_108_764_000],
        "net_income": [-13_743_000, 40_354_000, 13_592_000, 62_780_000],
    })
    checks = _annual_reconciliation(frame, {
        2017: {"revenue": 3_390_393_000, "net_income": 102_983_000}
    })
    assert checks[0]["delta_usd"] == {
        "revenue": MAX_RECONCILIATION_DELTA_USD,
        "net_income": 0,
    }
    assert checks[0]["exact_match"] is False
    assert checks[0]["within_disclosed_filing_tolerance"] is True


def test_csiq_annual_reconciliation_rejects_larger_unexplained_gap() -> None:
    frame = pd.DataFrame({
        "fiscal_end": pd.to_datetime([
            "2021-03-31", "2021-06-30", "2021-09-30", "2021-12-31"
        ]),
        "revenue": [1.0, 2.0, 3.0, 4.0],
        "net_income": [1.0, 2.0, 3.0, 4.0],
    })
    with pytest.raises(RuntimeError, match="do not reconcile annual"):
        _annual_reconciliation(frame, {
            2021: {"revenue": 10.0 + MAX_RECONCILIATION_DELTA_USD + 1, "net_income": 10.0}
        })


def test_csiq_registry_is_complete_sec_only_and_excludes_proxy_accessions() -> None:
    registry = pd.read_csv(
        Path("stocks_list_dir/nasdaq/csiq_quarterly_reports.csv"),
        parse_dates=["fiscal_end", "filed_date"],
    )
    assert len(registry) == 20
    assert set(registry["ticker"]) == {"CSIQ"}
    assert set(registry["cik"]) == {1375877}
    assert set(registry["form"]) == {"6-K"}
    assert registry["fiscal_end"].tolist() == EXPECTED_ENDS
    assert registry["source_url"].str.startswith(
        "https://www.sec.gov/Archives/edgar/data/1375877/"
    ).all()
    assert not set(registry["accession"]).intersection({
        "0001104659-17-036614",
        "0001104659-19-029724",
        "0001104659-21-073039",
    })
