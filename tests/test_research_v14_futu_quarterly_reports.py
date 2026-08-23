from scripts.research_v14_futu_quarterly_reports import SOURCES, parse_quarter


def test_futu_parser_uses_current_hkd_quarter_not_usd_or_ytd() -> None:
    html = b"""
    <table>
      <tr><td></td><td>2019 HK$</td><td>2020 HK$</td><td>2020 US$</td>
          <td>2019 YTD</td><td>2020 YTD</td><td>2020 YTD US$</td></tr>
      <tr><td>Total revenues</td><td>259854</td><td>687564</td><td>88716</td>
          <td>496303</td><td>1178206</td><td>152025</td></tr>
    </table>
    <table>
      <tr><td></td><td>2019 HK$</td><td>2020 HK$</td><td>2020 US$</td></tr>
      <tr><td>Net income</td><td>55330</td><td>236488</td><td>30513</td></tr>
    </table>
    """

    assert parse_quarter(html) == {
        "revenue": 687_564_000.0,
        "net_income": 236_488_000.0,
    }


def test_futu_source_dates_are_contemporaneous_and_contiguous() -> None:
    assert list(SOURCES) == [
        "2019-03-31", "2019-06-30", "2019-09-30", "2019-12-31",
        "2020-03-31", "2020-06-30", "2020-09-30",
    ]
    assert SOURCES["2020-03-31"]["filed"] == "2020-05-14"
    assert SOURCES["2020-06-30"]["filed"] == "2020-08-13"
    assert SOURCES["2019-03-31"]["comparative_fiscal_end"] == "2018-03-31"
    assert SOURCES["2019-12-31"]["comparative_fiscal_end"] == "2018-12-31"
