from scripts.research_v14_tsco_legacy_revenue import _strict_quarter_rows


def _fact(start, end, value, filed, form="10-Q", accn="a"):
    return {
        "start": start,
        "end": end,
        "val": value,
        "filed": filed,
        "form": form,
        "accn": accn,
    }


def test_strict_rows_derive_q4_only_from_originally_available_facts():
    revenue = [
        _fact("2017-01-01", "2017-04-01", 10, "2017-05-10"),
        _fact("2017-04-02", "2017-07-01", 20, "2017-08-10"),
        _fact("2017-07-02", "2017-09-30", 30, "2017-11-09"),
        _fact("2017-01-01", "2017-12-30", 100, "2018-02-22", "10-K"),
        _fact("2017-12-31", "2018-03-31", 40, "2018-05-11"),
        _fact("2018-04-01", "2018-06-30", 50, "2018-08-09"),
        _fact("2018-07-01", "2018-09-29", 60, "2018-11-08"),
        _fact("2017-12-31", "2018-12-29", 220, "2019-02-21", "10-K"),
        _fact("2017-01-01", "2017-04-01", 10, "2018-05-11"),
    ]
    income = [
        _fact("2017-01-01", "2017-04-01", 1, "2017-05-10"),
        _fact("2017-04-02", "2017-07-01", 2, "2017-08-10"),
        _fact("2017-07-02", "2017-09-30", 3, "2017-11-09"),
        _fact("2017-01-01", "2017-12-30", 10, "2018-02-22", "10-K"),
        _fact("2017-12-31", "2018-03-31", 4, "2018-05-11"),
        _fact("2018-04-01", "2018-06-30", 5, "2018-08-09"),
        _fact("2018-07-01", "2018-09-29", 6, "2018-11-08"),
        _fact("2017-12-31", "2018-12-29", 22, "2019-02-21", "10-K"),
    ]
    payload = {
        "facts": {"us-gaap": {
            "SalesRevenueGoodsNet": {"units": {"USD": revenue}},
            "NetIncomeLoss": {"units": {"USD": income}},
        }}
    }
    rows = _strict_quarter_rows(payload)
    assert len(rows) == 16
    derived = rows[rows["derivation"].str.startswith("annual_less")]
    assert set(derived["value"]) == {4.0, 7.0, 40.0, 70.0}
