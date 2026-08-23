import pytest

from scripts.research_v14_ocsl_legacy_quarterly_reports import (
    _first_accounting_value,
    extract_statement_values,
    run,
)


def _statement() -> bytes:
    return b"""
      <html><body>Oaktree Specialty Lending Corporation
      CONSOLIDATED STATEMENTS OF OPERATIONS
      (dollars in thousands)
      For the three months ended March 31, 2020
      <table>
        <tr><td></td><td>Three months ended March 31, 2020</td></tr>
        <tr><td>Total investment income</td><td>$</td><td>34,171</td></tr>
        <tr><td>Net investment income</td><td>18,000</td></tr>
        <tr><td>Net increase (decrease) in net assets resulting from operations</td>
            <td>$</td><td>(165,467</td><td>)</td></tr>
      </table></body></html>
    """


def test_extract_statement_values_uses_total_gaap_result() -> None:
    assert extract_statement_values(_statement(), "2020-03-31") == (
        34_171, -165_467,
    )


def test_first_accounting_value_handles_sec_parenthesis_cells() -> None:
    assert _first_accounting_value(["label", "$", "(30,441", ")"]) == -30_441
    assert _first_accounting_value(["label", None, "38220", "38220"]) == 38_220


def test_extract_statement_values_rejects_wrong_identity_period_or_scale() -> None:
    with pytest.raises(ValueError, match="identity"):
        extract_statement_values(
            _statement().replace(b"Oaktree Specialty Lending", b"Other"),
            "2020-03-31",
        )
    with pytest.raises(ValueError, match="quarter"):
        extract_statement_values(_statement(), "2020-06-30")
    with pytest.raises(ValueError, match="scale"):
        extract_statement_values(
            _statement().replace(b"dollars in thousands", b"amounts in shares"),
            "2020-03-31",
        )


def test_extract_statement_values_rejects_net_investment_income_substitute() -> None:
    broken = _statement().replace(
        b"Net increase (decrease) in net assets resulting from operations",
        b"Adjusted net income",
    )
    with pytest.raises(ValueError, match="statement"):
        extract_statement_values(broken, "2020-03-31")


def test_run_declares_every_recovered_quarter(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "scripts.research_v14_ocsl_legacy_quarterly_reports.PERIOD_EVIDENCE",
        {"2020-03-31": (
            "2020-05-07", "10-Q", "0001414932-20-000008",
            "ocsl-033120x10xq.htm", 34_171, -165_467,
        )},
    )
    monkeypatch.setattr(
        "scripts.research_v14_ocsl_legacy_quarterly_reports.urlopen",
        lambda *args, **kwargs: type("Response", (), {
            "__enter__": lambda self: self,
            "__exit__": lambda self, *exc: None,
            "read": lambda self: _statement(),
        })(),
    )
    # The production exact-count guard intentionally requires all 15 quarters;
    # the declaration itself is covered by the real-script integration test.
    with pytest.raises(RuntimeError, match="fifteen paired quarters"):
        run(output_dir=tmp_path)
