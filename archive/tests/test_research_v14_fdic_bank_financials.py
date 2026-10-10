import pandas as pd
import pytest

from scripts.research_v14_fdic_bank_financials import fdic_ytd_to_quarters


def _document(name: str = "BANK OZK", cert: int = 110) -> dict:
    return {
        "data": [
            {"data": {
                "CERT": cert, "REPDTE": "20200331", "NAME": name,
                "NIM": 100, "NONII": 20, "NETINC": 30,
            }},
            {"data": {
                "CERT": cert, "REPDTE": "20200630", "NAME": name,
                "NIM": 230, "NONII": 50, "NETINC": 70,
            }},
            {"data": {
                "CERT": cert, "REPDTE": "20210331", "NAME": name,
                "NIM": 150, "NONII": 40, "NETINC": 60,
            }},
        ]
    }


def test_fdic_ytd_conversion_derives_single_quarters_and_resets_year() -> None:
    rows = fdic_ytd_to_quarters("OZK", _document())
    values = rows.pivot(index="fiscal_end", columns="metric", values="value")
    assert values.loc[pd.Timestamp("2020-03-31"), "revenue"] == 120_000
    assert values.loc[pd.Timestamp("2020-06-30"), "revenue"] == 160_000
    assert values.loc[pd.Timestamp("2020-06-30"), "net_income"] == 40_000
    assert values.loc[pd.Timestamp("2021-03-31"), "revenue"] == 190_000
    assert set(
        pd.to_datetime(rows["available_date"])
        - pd.to_datetime(rows["fiscal_end"])
    ) == {pd.Timedelta(days=60)}


def test_fdic_ytd_conversion_rejects_wrong_institution() -> None:
    with pytest.raises(RuntimeError, match="Unexpected FDIC institution"):
        fdic_ytd_to_quarters("OZK", _document(name="OTHER BANK"))
