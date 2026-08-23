from scripts.research_v14_ttd_legacy_revenue import _strict_quarter_rows


def _fact(start, end, value, filed, form="10-Q", accn="a"):
    return {"start": start, "end": end, "val": value, "filed": filed,
            "form": form, "accn": accn}


def test_strict_rows_bridge_both_revenue_concepts_and_derive_q4():
    legacy = [
        _fact("2017-01-01", "2017-03-31", 10, "2017-05-11"),
        _fact("2017-04-01", "2017-06-30", 20, "2017-08-11"),
        _fact("2017-07-01", "2017-09-30", 30, "2017-11-13"),
        _fact("2017-01-01", "2017-12-31", 100, "2018-02-28", "10-K"),
        _fact("2018-01-01", "2018-03-31", 40, "2018-05-10"),
    ]
    successor = [
        _fact("2018-04-01", "2018-06-30", 50, "2018-08-09"),
        _fact("2018-07-01", "2018-09-30", 60, "2018-11-09"),
        _fact("2018-01-01", "2018-12-31", 220, "2019-02-22", "10-K"),
    ]
    income = [
        _fact("2017-01-01", "2017-03-31", 1, "2017-05-11"),
        _fact("2017-04-01", "2017-06-30", 2, "2017-08-11"),
        _fact("2017-07-01", "2017-09-30", 3, "2017-11-13"),
        _fact("2017-01-01", "2017-12-31", 10, "2018-02-28", "10-K"),
        _fact("2018-01-01", "2018-03-31", 4, "2018-05-10"),
        _fact("2018-04-01", "2018-06-30", 5, "2018-08-09"),
        _fact("2018-07-01", "2018-09-30", 6, "2018-11-09"),
        _fact("2018-01-01", "2018-12-31", 22, "2019-02-22", "10-K"),
    ]
    payload = {"facts": {"us-gaap": {
        "SalesRevenueServicesNet": {"units": {"USD": legacy}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {
            "units": {"USD": successor}
        },
        "NetIncomeLoss": {"units": {"USD": income}},
    }}}
    rows = _strict_quarter_rows(payload)
    assert len(rows) == 16
    assert set(rows.loc[
        rows["derivation"].str.startswith("annual_less"), "value"
    ]) == {4.0, 7.0, 40.0, 70.0}
