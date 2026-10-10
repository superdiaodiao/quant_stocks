import pytest

from scripts.research_v14_ttek_legacy_revenue import strict_revenue_rows


def _payload() -> dict:
    return {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        {
            "start": "2018-01-01", "end": "2018-04-01",
            "val": 700_262_000, "accn": "0000831641-18-000097",
            "form": "10-K", "filed": "2018-11-16",
        },
        {
            "start": "2018-07-02", "end": "2018-09-30",
            "val": 739_343_000, "accn": "0000831641-18-000097",
            "form": "10-K", "filed": "2018-11-16",
        },
    ]}}}}}


def test_strict_revenue_rows_accepts_only_predeclared_direct_facts():
    rows = strict_revenue_rows(_payload())
    assert rows[["fiscal_end", "value"]].astype({"fiscal_end": str}).to_dict(
        "records"
    ) == [
        {"fiscal_end": "2018-04-01", "value": 700_262_000.0},
        {"fiscal_end": "2018-09-30", "value": 739_343_000.0},
    ]
    assert set(rows["derivation"]) == {
        "direct_three_month_sec_fact_without_frame"
    }


def test_strict_revenue_rows_rejects_duplicates_and_changed_values():
    duplicate = _payload()
    duplicate["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(
        duplicate["facts"]["us-gaap"]["Revenues"]["units"]["USD"][0].copy()
    )
    with pytest.raises(ValueError, match="not unique"):
        strict_revenue_rows(duplicate)

    changed = _payload()
    changed["facts"]["us-gaap"]["Revenues"]["units"]["USD"][0]["val"] = 1
    with pytest.raises(ValueError, match="predeclared"):
        strict_revenue_rows(changed)
