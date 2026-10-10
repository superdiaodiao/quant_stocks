from scripts.research_v14_banr_legacy_bank_revenue import (
    ANNUAL_PERIODS,
    DIRECT_QUARTERS,
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


def test_strict_rows_sum_original_bank_components_and_derive_q4():
    direct = [
        (str(start.date()), str(end.date()), filed, index * 10, index, index + 1)
        for index, (end, (start, filed)) in enumerate(
            DIRECT_QUARTERS.items(), start=1
        )
    ]
    net_interest = [_fact(a, b, n, f) for a, b, f, n, _o, _i in direct]
    noninterest = [_fact(a, b, o, f) for a, b, f, _n, o, _i in direct]
    income = [_fact(a, b, i, f) for a, b, f, _n, _o, i in direct]
    for year_index, (end, spec) in enumerate(ANNUAL_PERIODS.items(), start=1):
        quarter_rows = [
            row for row in direct if row[1] in spec["quarter_ends"]
        ]
        net_interest.append(_fact(
            str(spec["start"].date()), str(end.date()),
            sum(row[3] for row in quarter_rows) + 100 + year_index,
            spec["filed"], "10-K",
        ))
        noninterest.append(_fact(
            str(spec["start"].date()), str(end.date()),
            sum(row[4] for row in quarter_rows) + 10 + year_index,
            spec["filed"], "10-K",
        ))
        income.append(_fact(
            str(spec["start"].date()), str(end.date()),
            sum(row[5] for row in quarter_rows) + 20 + year_index,
            spec["filed"], "10-K",
        ))
    noninterest.append(_fact(
        "2017-01-01", "2017-03-31", 99, "2018-05-04"
    ))
    payload = {"facts": {"us-gaap": {
        "InterestIncomeExpenseNet": {"units": {"USD": net_interest}},
        "OtherOperatingIncome": {"units": {"USD": noninterest}},
        "NetIncomeLoss": {"units": {"USD": income}},
    }}}

    rows = _strict_quarter_rows(payload)

    assert len(rows) == 40
    first = rows.loc[
        rows["fiscal_end"].eq("2017-03-31") & rows["metric"].eq("revenue")
    ].iloc[0]
    assert first["value"] == 11
    q4_2017 = rows.loc[rows["fiscal_end"].eq("2017-12-31")].set_index("metric")
    assert q4_2017.loc["revenue", "value"] == 112
    assert q4_2017.loc["net_income", "value"] == 21
    q4_2021 = rows.loc[rows["fiscal_end"].eq("2021-12-31")].set_index("metric")
    assert q4_2021.loc["revenue", "value"] == 120
    assert q4_2021.loc["net_income", "value"] == 25
