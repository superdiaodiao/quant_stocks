import pandas as pd

from scripts.research_v14_dltr_legacy_revenue import (
    EXPECTED_QUARTERS,
    _strict_quarter_rows,
)


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
    revenue, income = [], []
    successor_revenue = []
    for ordinal, (end, (start, filed, revenue_concept)) in enumerate(
        EXPECTED_QUARTERS.items(), start=1
    ):
        form = "10-K" if end in {
            pd.Timestamp("2017-01-28"), pd.Timestamp("2018-02-03")
        } else "10-Q"
        target = revenue if revenue_concept == "SalesRevenueGoodsNet" else successor_revenue
        target.append(_fact(
            start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
            ordinal * 100, filed, form,
        ))
        income.append(_fact(
            start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
            ordinal * 10, filed, form,
        ))
    revenue.extend([
        _fact("2017-01-29", "2017-07-29", 999, "2017-08-24"),
        _fact("2017-01-29", "2017-04-29", 100, "2018-05-31"),
    ])
    payload = {
        "facts": {"us-gaap": {
            "SalesRevenueGoodsNet": {"units": {"USD": revenue}},
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": successor_revenue}
            },
            "NetIncomeLoss": {"units": {"USD": income}},
        }}
    }
    rows = _strict_quarter_rows(payload)
    assert len(rows) == 16
    assert rows.groupby("fiscal_end")["metric"].nunique().eq(2).all()
    assert rows["available_date"].max() == pd.Timestamp("2019-03-27")
