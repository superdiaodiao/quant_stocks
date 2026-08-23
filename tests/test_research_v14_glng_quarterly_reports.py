import pytest

from scripts.research_v14_glng_quarterly_reports import (
    EXPECTED_ANNUALS,
    EXPECTED_QUARTERS,
    audit_signals,
    build_quarters,
    parse_annual,
    parse_press_release,
)


def _table(
    current_revenue: str,
    current_income: str,
    *,
    second_revenue: str = "2,000",
    second_income: str = "(2,000)",
    ytd_revenue: str = "3,000",
    ytd_income: str = "(3,000)",
    prior_ytd_revenue: str = "4,000",
    prior_ytd_income: str = "(4,000)",
) -> bytes:
    return f"""
    <table>
      <tr><td>(in thousands of $)</td><td>current</td><td>prior</td><td>YTD</td><td>prior YTD</td></tr>
      <tr><td>Total operating revenues</td><td>{current_revenue}</td><td>{second_revenue}</td><td>{ytd_revenue}</td><td>{prior_ytd_revenue}</td></tr>
      <tr><td>Net income/(loss)</td><td>999,999</td><td>999,999</td><td>999,999</td><td>999,999</td></tr>
      <tr><td>Net income attributable to non-controlling interests</td><td>888,888</td></tr>
      <tr><td>Net income/(loss) attributable to Golar LNG Limited</td><td>{current_income}</td><td>{second_income}</td><td>{ytd_income}</td><td>{prior_ytd_income}</td></tr>
    </table>
    """.encode()


def test_parser_uses_parent_attributable_current_quarter_usd() -> None:
    parsed = parse_press_release(
        _table("104,287", "471,433", ytd_revenue="230,114", ytd_income="496,797"),
        "q2",
    )
    assert parsed == {
        "current": (104_287_000.0, 471_433_000.0),
        "ytd": (230_114_000.0, 496_797_000.0),
    }


def test_annual_parser_skips_note_references() -> None:
    raw = b"""
    <table>
      <tr><td>(in thousands of $)</td><td>Notes</td><td>2020</td></tr>
      <tr><td>Total operating revenues</td><td>6, 25</td><td>438,637</td></tr>
      <tr><td>Net loss attributable to stockholders of Golar LNG Limited</td><td></td><td>(273,557)</td></tr>
    </table>
    """
    assert parse_annual(raw) == (438_637_000.0, -273_557_000.0)


def test_build_quarters_derives_audited_q4_and_checks_cumulative_periods() -> None:
    q = EXPECTED_QUARTERS
    press = {
        "2019-09-30": {"current": q["2019-09-30"], "ytd": (309_702_000.0, -236_724_000.0)},
        "2019-12-31": {"current": q["2019-12-31"]},
        "2020-03-31": {"current": q["2020-03-31"]},
        "2020-06-30": {"current": q["2020-06-30"], "ytd": (224_801_000.0, -259_881_000.0)},
        "2020-09-30": {"current": q["2020-09-30"], "ytd": (319_953_000.0, -281_683_000.0)},
        "2021-03-31": {"current": q["2021-03-31"]},
        "2021-06-30": {"current": q["2021-06-30"], "ytd": (230_114_000.0, 496_797_000.0)},
        "2021-09-30": {"current": q["2021-09-30"], "ytd": (336_717_000.0, 405_842_000.0)},
    }
    assert build_quarters(press, EXPECTED_ANNUALS) == EXPECTED_QUARTERS
    bad = {key: dict(value) for key, value in press.items()}
    bad["2021-06-30"]["ytd"] = (230_115_000.0, 496_797_000.0)
    with pytest.raises(RuntimeError, match="2021 H1 cumulative identity failed"):
        build_quarters(bad, EXPECTED_ANNUALS)


def test_signal_audit_enforces_filed_cutoffs_and_eight_quarters() -> None:
    available = {
        "2019-09-30": "2019-11-26",
        "2019-12-31": "2020-02-25",
        "2020-03-31": "2020-05-28",
        "2020-06-30": "2020-08-13",
        "2020-09-30": "2020-11-30",
        "2020-12-31": "2021-04-22",
        "2021-03-31": "2021-05-20",
        "2021-06-30": "2021-08-09",
        "2021-09-30": "2021-11-09",
    }
    audits = audit_signals(EXPECTED_QUARTERS, available)
    assert [row["signal_date"] for row in audits] == [
        "2021-09-30",
        "2021-10-29",
        "2021-12-31",
    ]
    assert audits[0]["quarter_window"][-1] == "2021-06-30"
    assert audits[1]["quarter_window"][-1] == "2021-06-30"
    assert audits[2]["quarter_window"][-1] == "2021-09-30"
    assert all(row["growth"]["revenue"] < 0 for row in audits)
