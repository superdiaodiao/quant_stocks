import pandas as pd

from scripts.research_v14_odfl_legacy_revenue import _strict_quarter_rows


def _fact(start, end, value, filed, form="10-Q", accn="a"):
    return {
        "start": start,
        "end": end,
        "val": value,
        "filed": filed,
        "form": form,
        "accn": accn,
    }


def test_strict_rows_exclude_cumulative_and_later_comparatives():
    revenue = [
        _fact("2017-01-01", "2017-03-31", 10, "2017-05-10"),
        _fact("2017-01-01", "2017-06-30", 30, "2017-08-07"),
        _fact("2017-04-01", "2017-06-30", 20, "2017-08-07"),
        _fact("2017-07-01", "2017-09-30", 30, "2017-11-07"),
        _fact("2017-10-01", "2017-12-31", 40, "2018-02-27", "10-K"),
        _fact("2017-01-01", "2017-03-31", 10, "2018-05-07"),
        _fact("2018-01-01", "2018-03-31", 45, "2018-05-07"),
    ]
    income = [
        _fact("2017-01-01", "2017-03-31", 1, "2017-05-10"),
        _fact("2017-04-01", "2017-06-30", 2, "2017-08-07"),
        _fact("2017-07-01", "2017-09-30", 3, "2017-11-07"),
        _fact("2017-10-01", "2017-12-31", 4, "2018-02-27", "10-K"),
        _fact("2018-01-01", "2018-03-31", 4.5, "2018-05-07"),
        _fact("2018-10-01", "2018-12-31", 5, "2019-02-27", "10-K"),
    ]
    successor_revenue = [
        _fact("2018-10-01", "2018-12-31", 50, "2019-02-27", "10-K"),
    ]
    payload = {
        "facts": {"us-gaap": {
            "SalesRevenueServicesNet": {"units": {"USD": revenue}},
            "RevenueFromContractWithCustomerIncludingAssessedTax": {
                "units": {"USD": successor_revenue}
            },
            "NetIncomeLoss": {"units": {"USD": income}},
        }}
    }
    rows = _strict_quarter_rows(payload)
    assert len(rows) == 12
    assert set(rows["available_date"]) == {
        pd.Timestamp("2017-05-10"),
        pd.Timestamp("2017-08-07"),
        pd.Timestamp("2017-11-07"),
        pd.Timestamp("2018-02-27"),
        pd.Timestamp("2018-05-07"),
        pd.Timestamp("2019-02-27"),
    }
    assert rows.loc[
        rows["fiscal_end"].eq(pd.Timestamp("2017-06-30"))
        & rows["metric"].eq("revenue"),
        "value",
    ].item() == 20
