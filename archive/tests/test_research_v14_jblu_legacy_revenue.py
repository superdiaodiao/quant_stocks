from scripts.research_v14_jblu_legacy_revenue import _strict_quarter_rows


def _fact(start, end, value, filed, form="10-Q", accn="a"):
    return {
        "start": start,
        "end": end,
        "val": value,
        "filed": filed,
        "form": form,
        "accn": accn,
    }


def test_strict_rows_use_original_filings_and_direct_q4_income():
    legacy = [
        _fact("2017-01-01", "2017-03-31", 10, "2017-04-28"),
        _fact("2017-04-01", "2017-06-30", 20, "2017-07-28"),
        _fact("2017-07-01", "2017-09-30", 30, "2017-10-27"),
        _fact("2017-10-01", "2017-12-31", 40, "2018-02-16", "10-K"),
        _fact("2018-01-01", "2018-03-31", 50, "2018-04-27"),
        _fact("2017-01-01", "2017-03-31", 9, "2018-04-27"),
    ]
    successor = [
        _fact("2018-04-01", "2018-06-30", 60, "2018-07-26"),
        _fact("2018-07-01", "2018-09-30", 70, "2018-10-26"),
        _fact("2018-01-01", "2018-12-31", 260, "2019-02-21", "10-K"),
    ]
    income = [
        _fact("2017-01-01", "2017-03-31", 1, "2017-04-28"),
        _fact("2017-04-01", "2017-06-30", 2, "2017-07-28"),
        _fact("2017-07-01", "2017-09-30", 3, "2017-10-27"),
        _fact("2017-10-01", "2017-12-31", 4, "2018-02-16", "10-K"),
        _fact("2018-01-01", "2018-03-31", 5, "2018-04-27"),
        _fact("2018-04-01", "2018-06-30", 6, "2018-07-26"),
        _fact("2018-07-01", "2018-09-30", 7, "2018-10-26"),
        _fact("2018-10-01", "2018-12-31", 8, "2019-02-21", "10-K"),
        _fact("2017-01-01", "2017-03-31", -1, "2018-04-27"),
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
    first_revenue = rows.loc[
        rows["fiscal_end"].eq("2017-03-31") & rows["metric"].eq("revenue")
    ].iloc[0]
    assert first_revenue["value"] == 10
    q4_2018 = rows.loc[rows["fiscal_end"].eq("2018-12-31")].set_index("metric")
    assert q4_2018.loc["revenue", "value"] == 80
    assert q4_2018.loc["revenue", "derivation"].startswith("annual_less")
    assert q4_2018.loc["net_income", "value"] == 8
    assert q4_2018.loc["net_income", "derivation"] == "direct_three_month_sec_fact"
