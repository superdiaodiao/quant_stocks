import pytest

from scripts.research_v14_csiq_2016q4_pit import (
    EXPECTED_AUDITED,
    EXPECTED_NINE_MONTHS,
    EXPECTED_RELEASE,
    audit_signal,
    parse_annual,
    parse_nine_months,
    parse_q4_release,
    validate_quarter,
)


def _statement_table(period_label: str, total_label: str, rows: list[tuple]) -> bytes:
    body = "".join(
        "<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return f"""
    <table>
      <tr><td>{period_label}</td><td>current</td><td>prior</td><td>prior year</td><td>current cumulative</td><td>prior cumulative</td></tr>
      {body}
    </table>
    """.encode()


def test_q4_parser_uses_total_profit_loss_not_parent_attributable() -> None:
    raw = _statement_table(
        "Three Months Ended and Twelve Months Ended (In Thousands of US Dollars)",
        "Net income (loss)",
        [
            ("Net revenues", "668,428", "657,323", "1,120,278", "2,853,078", "3,467,626"),
            ("Net income (loss)", "(13,776)", "15,185", "62,329", "65,275", "173,316"),
            (
                "Less: Net income (loss) attributable to non-controlling interests",
                "(448)",
                "(429)",
                "31",
                "26",
                "1,455",
            ),
            (
                "Net income (loss) attributable to Canadian Solar Inc.",
                "(13,328)",
                "15,614",
                "62,298",
                "65,249",
                "171,861",
            ),
        ],
    )
    assert parse_q4_release(raw) == EXPECTED_RELEASE
    assert parse_q4_release(raw)["q4"][1] == -13_776_000.0
    assert parse_q4_release(raw)["q4_parent"] == -13_328_000.0


def test_parsers_keep_nine_month_and_annual_periods_separate() -> None:
    q3 = _statement_table(
        "Three Months Ended and Nine Months Ended (In Thousands of US Dollars)",
        "Net income",
        [
            ("Net revenues", "657,323", "805,906", "849,806", "2,184,650", "2,347,348"),
            ("Net income", "15,185", "40,077", "30,644", "79,051", "110,987"),
        ],
    )
    annual = _statement_table(
        "Years Ended December 31 (In Thousands of US Dollars)",
        "Net income (loss)",
        [
            ("Net revenues", "2,960,627", "3,467,626", "2,853,078"),
            ("Net income (loss)", "243,887", "173,316", "65,275"),
            (
                "Net income (loss) attributable to Canadian Solar Inc.",
                "239,502",
                "171,861",
                "65,249",
            ),
        ],
    )
    assert parse_nine_months(q3) == EXPECTED_NINE_MONTHS
    assert parse_annual(annual) == EXPECTED_AUDITED


def test_fy_q4_and_ownership_identities_are_mandatory() -> None:
    assert (
        validate_quarter(EXPECTED_RELEASE, EXPECTED_NINE_MONTHS, EXPECTED_AUDITED)
        == EXPECTED_RELEASE["q4"]
    )
    changed = dict(EXPECTED_RELEASE)
    changed["q4_parent"] = -13_327_000.0
    with pytest.raises(RuntimeError, match="release values changed"):
        validate_quarter(changed, EXPECTED_NINE_MONTHS, EXPECTED_AUDITED)


def test_signal_audit_closes_exact_eight_quarter_window() -> None:
    audit = audit_signal(EXPECTED_RELEASE["q4"])
    assert audit["signal_date"] == "2019-02-28"
    assert audit["missing_observation_count"] == 6
    assert audit["quarter_window"] == [
        "2016-12-31",
        "2017-03-31",
        "2017-06-30",
        "2017-09-30",
        "2017-12-31",
        "2018-03-31",
        "2018-06-30",
        "2018-09-30",
    ]
    assert audit["previous_ttm"] == {
        "revenue": 2_950_059_000.0,
        "net_income": 26_427_000.0,
    }
    assert audit["current_ttm"] == {
        "revenue": 3_952_235_000.0,
        "net_income": 191_111_000.0,
    }
    assert audit["growth"]["revenue"] == pytest.approx(0.3397138836884279)
    assert audit["growth"]["net_income"] == pytest.approx(6.231657017444281)
    assert audit["deterministic_result"].startswith("PASS_")
