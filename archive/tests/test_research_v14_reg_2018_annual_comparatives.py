from scripts.research_v14_reg_2018_annual_comparatives import (
    ACCESSION,
    FILED,
    _strict_quarter_rows,
)


def _fact(start, end, value, concept, *, filed=FILED, accn=ACCESSION):
    return {
        "start": start,
        "end": end,
        "val": value,
        "filed": filed,
        "form": "10-K",
        "accn": accn,
        "concept": concept,
    }


def test_strict_rows_select_only_original_2018_annual_comparatives():
    periods = [
        ("2018-01-01", "2018-03-31", 10, 1),
        ("2018-04-01", "2018-06-30", 20, 2),
        ("2018-07-01", "2018-09-30", 30, 3),
        ("2018-10-01", "2018-12-31", 40, 4),
    ]
    revenues = [_fact(a, b, r, "Revenues") for a, b, r, _n in periods]
    incomes = [
        _fact(a, b, n, "NetIncomeLossAvailableToCommonStockholdersBasic")
        for a, b, _r, n in periods
    ]
    revenues.append(_fact(
        "2018-01-01", "2018-03-31", 999, "Revenues",
        filed="2020-02-18", accn="later",
    ))
    payload = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": revenues}},
        "NetIncomeLossAvailableToCommonStockholdersBasic": {
            "units": {"USD": incomes}
        },
    }}}

    rows = _strict_quarter_rows(payload)

    assert len(rows) == 8
    assert set(rows["available_date"].dt.strftime("%Y-%m-%d")) == {FILED}
    first = rows.loc[rows["fiscal_end"].eq("2018-03-31")].set_index("metric")
    assert first.loc["revenue", "value"] == 10
    assert first.loc["net_income", "value"] == 1
    assert set(rows["derivation"]) == {
        "direct_three_month_annual_comparative_fact"
    }
