from pathlib import Path

import pandas as pd
import pytest

from scripts import research_v14_yy_quarterly_reports as yy


def _statement(
    *, issuer: str = "YY Inc.", prior_date: str = "March 31, 2018",
    current_date: str = "March 31, 2019", prior_revenue: str = "3,248,931",
    current_revenue: str = "4,780,584", prior_income: str = "968,913",
    current_income: str = "3,149,982",
) -> bytes:
    return f"""
    <html><body><p>{issuer}</p><table>
      <tr><td></td><td>{prior_date}</td><td>{prior_date}</td>
          <td>{current_date}</td><td>{current_date}</td></tr>
      <tr><td></td><td>RMB</td><td>RMB</td><td>RMB</td><td>RMB</td></tr>
      <tr><td>Total net revenues</td><td></td><td>{prior_revenue}</td>
          <td></td><td>{current_revenue}</td></tr>
      <tr><td>Net income (loss)</td><td></td><td>{prior_income}</td>
          <td></td><td>{current_income}</td></tr>
    </table></body></html>
    """.encode()


def test_parse_quarter_selects_requested_date_and_rmb_thousands():
    raw = _statement(prior_income="(276,483)", prior_date="June 30, 2018")
    assert yy.parse_quarter(raw, "2019-03-31", "YY Inc.") == {
        "revenue": 4_780_584_000.0,
        "net_income": 3_149_982_000.0,
    }
    assert yy.parse_quarter(raw, "2018-06-30", "YY Inc.") == {
        "revenue": 3_248_931_000.0,
        "net_income": -276_483_000.0,
    }


def test_parse_quarter_accepts_yy_net_loss_income_label():
    raw = _statement(prior_income="(276,483)", prior_date="June 30, 2018")
    raw = raw.replace(b"Net income (loss)", b"Net (loss) income")
    assert yy.parse_quarter(raw, "2018-06-30", "YY Inc.")["net_income"] == (
        -276_483_000.0
    )


def test_parse_quarter_ignores_same_date_us_dollar_translation():
    raw = _statement().replace(
        b"</tr>\n      <tr><td></td><td>RMB</td><td>RMB</td><td>RMB</td><td>RMB</td>",
        b"<td>March 31, 2019</td><td>March 31, 2019</td></tr>\n"
        b"<tr><td></td><td>RMB</td><td>RMB</td><td>RMB</td><td>RMB</td>"
        b"<td>US$</td><td>US$</td>",
    ).replace(
        b"<td></td><td>4,780,584</td></tr>",
        b"<td></td><td>4,780,584</td><td></td><td>705,340</td></tr>",
    ).replace(
        b"<td></td><td>3,149,982</td></tr>",
        b"<td></td><td>3,149,982</td><td></td><td>464,756</td></tr>",
    )
    assert yy.parse_quarter(raw, "2019-03-31", "YY Inc.") == {
        "revenue": 4_780_584_000.0,
        "net_income": 3_149_982_000.0,
    }


def test_parse_quarter_requires_issuer_identity():
    with pytest.raises(ValueError, match="issuer identity"):
        yy.parse_quarter(_statement(issuer="Different issuer"), "2019-03-31", "YY Inc.")


def test_recover_keeps_first_disclosure_dates_and_historical_ticker(
    monkeypatch, tmp_path: Path,
):
    def fake_fetch(spec):
        fiscal_end = next(end for end, value in yy.SOURCES.items() if value is spec)
        kwargs = {
            "issuer": spec["issuer_marker"],
            "current_date": pd.Timestamp(fiscal_end).strftime("%B %d, %Y"),
            "current_revenue": f"{int(spec['revenue'] / 1000):,}",
            "current_income": (
                f"({abs(int(spec['net_income'] / 1000)):,})"
                if spec["net_income"] < 0
                else f"{int(spec['net_income'] / 1000):,}"
            ),
        }
        if "comparative_fiscal_end" in spec:
            kwargs.update({
                "prior_date": pd.Timestamp(
                    spec["comparative_fiscal_end"]
                ).strftime("%B %d, %Y"),
                "prior_revenue": f"{int(spec['comparative_revenue'] / 1000):,}",
                "prior_income": (
                    f"({abs(int(spec['comparative_net_income'] / 1000)):,})"
                    if spec["comparative_net_income"] < 0
                    else f"{int(spec['comparative_net_income'] / 1000):,}"
                ),
            })
        else:
            kwargs.update({
                "prior_date": "January 01, 1900",
                "prior_revenue": "1",
                "prior_income": "1",
            })
        return _statement(**kwargs)

    monkeypatch.setattr(yy, "_fetch", fake_fetch)
    report = yy.recover(tmp_path)
    facts = pd.read_csv(tmp_path / "strict_quarterly_facts.csv")
    assert report["accepted_quarter_count"] == 11
    assert report["release_status"] == "BLOCKED"
    assert not report["promotion_eligible"]
    assert facts["ticker"].eq("YY").all()
    assert facts["fiscal_end"].nunique() == 11
    assert set(facts.loc[facts["fiscal_end"].eq("2018-03-31"), "available_date"]) == {
        "2019-05-29"
    }
    assert set(facts.loc[facts["fiscal_end"].eq("2020-09-30"), "available_date"]) == {
        "2020-11-17"
    }
