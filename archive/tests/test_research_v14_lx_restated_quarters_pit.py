import pytest

from scripts.research_v14_lx_restated_quarters_pit import (
    EXPECTED_2018_ANNUAL,
    EXPECTED_2019_9M,
    EXPECTED_2019_H1,
    EXPECTED_QUARTERS,
    audit_signal,
    build_quarters,
    parse_periods,
)


def _table(period: str, columns: list[tuple[str, str, str]], rows: list[tuple]) -> str:
    return f"""
    <table>
      <tr><td></td>{''.join(f'<td>{period}</td>' for _ in columns)}</tr>
      <tr><td></td>{''.join(f'<td>{date}</td>' for date, _, _ in columns)}</tr>
      <tr><td></td>{''.join(f'<td>{currency}</td>' for _, currency, _ in columns)}</tr>
      {''.join('<tr>' + ''.join(f'<td>{cell}</td>' for cell in row) + '</tr>' for row in rows)}
    </table>
    """


def test_parser_selects_current_quarter_cny_total_gaap_rows() -> None:
    raw = _table(
        "For the Three Months Ended",
        [("March 31, 2019*", "RMB", "cny"), ("March 31, 2019*", "US$", "usd")],
        [
            ("Total operating revenue*", "1,774,510", "258,488"),
            ("Net income*", "424,300", "61,811"),
            ("Adjusted net income", "622,391", "92,738"),
            ("Net income attributable to ordinary shareholders", "1", "1"),
        ],
    ).encode()
    assert parse_periods(raw, ("2019-03-31",), "three months ended") == {
        "2019-03-31": (1_774_510_000.0, 424_300_000.0)
    }


def test_parser_keeps_quarters_and_cumulative_periods_separate() -> None:
    raw = (
        _table(
            "For the Three Months Ended",
            [("June 30, 2019", "RMB", "cny")],
            [
                ("Total operating revenue*", "2,492,940"),
                ("Net income*", "627,964"),
            ],
        )
        + _table(
            "For the Six Months Ended",
            [("June 30, 2019", "RMB", "cny")],
            [
                ("Total operating revenue*", "4,267,450"),
                ("Net income*", "1,052,264"),
            ],
        )
    ).encode()
    assert parse_periods(raw, ("2019-06-30",), "three months ended")["2019-06-30"] == EXPECTED_QUARTERS["2019-06-30"]
    assert parse_periods(raw, ("2019-06-30",), "six months ended")["2019-06-30"] == EXPECTED_2019_H1


def test_build_requires_revised_fy_h1_and_nine_month_identities(monkeypatch) -> None:
    expected_calls = {
        ("2017", "three months ended"): {"2017-12-31": EXPECTED_QUARTERS["2017-12-31"]},
        ("2018", "three months ended"): {end: value for end, value in EXPECTED_QUARTERS.items() if end.startswith("2018")},
        ("2019h1", "three months ended"): {end: EXPECTED_QUARTERS[end] for end in ("2019-03-31", "2019-06-30")},
        ("2019q3", "three months ended"): {"2019-09-30": EXPECTED_QUARTERS["2019-09-30"]},
        ("2018", "year ended"): {"2018-12-31": EXPECTED_2018_ANNUAL},
        ("20f", "year ended"): {"2018-12-31": EXPECTED_2018_ANNUAL},
        ("2019h1", "six months ended"): {"2019-06-30": EXPECTED_2019_H1},
        ("2019q3", "nine months ended"): {"2019-09-30": EXPECTED_2019_9M},
    }

    def fake_parse(raw, fiscal_ends, period_phrase):
        return expected_calls[(raw.decode(), period_phrase)]

    monkeypatch.setattr("scripts.research_v14_lx_restated_quarters_pit.parse_periods", fake_parse)
    raw = {
        "2017_q4": b"2017",
        "2018_q4_revision": b"2018",
        "2018_20f": b"20f",
        "2019_q2_restatement": b"2019h1",
        "2019_q3": b"2019q3",
    }
    assert build_quarters(raw) == EXPECTED_QUARTERS


def test_signal_audit_closes_all_six_observations_with_real_growth() -> None:
    audit = audit_signal(EXPECTED_QUARTERS)
    assert audit["signal_date"] == "2019-12-31"
    assert audit["missing_observation_count"] == 6
    assert audit["previous_ttm"] == {
        "revenue": 7_096_953_000.0,
        "net_income": 1_389_327_000.0,
    }
    assert audit["current_ttm"] == {
        "revenue": 9_549_091_000.0,
        "net_income": 2_465_047_000.0,
    }
    assert audit["growth"]["revenue"] == pytest.approx(0.34551983083444404)
    assert audit["growth"]["net_income"] == pytest.approx(0.7742741629580365)
    assert audit["deterministic_result"].startswith("PASS_")
