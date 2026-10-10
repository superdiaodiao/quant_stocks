import pytest

from scripts.research_v14_nymt_legacy_reit_revenue import (
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


def _payload(*, identity_offset=0):
    direct = [
        (str(start.date()), str(end.date()), filed, index * 10, index, index + 1)
        for index, (end, (start, filed)) in enumerate(
            DIRECT_QUARTERS.items(), start=1
        )
    ]
    net_interest = [_fact(a, b, n, f) for a, b, f, n, _o, _i in direct]
    other_operating = [
        _fact(a, b, o, f)
        for i, (a, b, f, _n, o, _income) in enumerate(direct)
        if i % 2 == 0
    ]
    noninterest = [
        _fact(a, b, o, f)
        for i, (a, b, f, _n, o, _income) in enumerate(direct)
        if i % 2 == 1
    ]
    income = [_fact(a, b, i, f) for a, b, f, _n, _o, i in direct]
    operating_income = [
        _fact(a, b, n + o - 5 + identity_offset, f)
        for a, b, f, n, o, _i in direct
    ]
    operating_expenses = [_fact(a, b, 5, f) for a, b, f, *_ in direct]
    for year_index, (end, spec) in enumerate(ANNUAL_PERIODS.items(), start=1):
        quarter_rows = [row for row in direct if row[1] in spec["quarter_ends"]]
        annual_net = sum(row[3] for row in quarter_rows) + 100 + year_index
        annual_other = sum(row[4] for row in quarter_rows) + 10 + year_index
        net_interest.append(_fact(
            str(spec["start"].date()), str(end.date()), annual_net,
            spec["filed"], "10-K",
        ))
        target = other_operating if year_index % 2 else noninterest
        target.append(_fact(
            str(spec["start"].date()), str(end.date()), annual_other,
            spec["filed"], "10-K",
        ))
        income.append(_fact(
            str(spec["start"].date()), str(end.date()),
            sum(row[5] for row in quarter_rows) + 20 + year_index,
            spec["filed"], "10-K",
        ))
    return {"facts": {"us-gaap": {
        "InterestIncomeExpenseNet": {"units": {"USD": net_interest}},
        "OtherOperatingIncomeExpenseNet": {"units": {"USD": other_operating}},
        "NoninterestIncome": {"units": {"USD": noninterest}},
        "OperatingIncomeLoss": {"units": {"USD": operating_income}},
        "OperatingExpenses": {"units": {"USD": operating_expenses}},
        "NetIncomeLoss": {"units": {"USD": income}},
    }}}


def test_strict_rows_use_original_reit_components_and_derive_q4():
    rows, identity_checks = _strict_quarter_rows(_payload())

    assert len(rows) == 40
    assert identity_checks == 15
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


def test_strict_rows_reject_operating_statement_identity_mismatch():
    with pytest.raises(ValueError, match="fail operating-statement identity"):
        _strict_quarter_rows(_payload(identity_offset=1))
