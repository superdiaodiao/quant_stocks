from scripts.research_v14_bldp_quarterly_reports import (
    EXPECTED,
    derive_quarters,
    parse_interim,
)


def test_bldp_uses_explicit_quarters_and_annual_minus_nine_months() -> None:
    parsed = {}
    for year in (2019, 2020, 2021):
        expected = {
            quarter: EXPECTED[f"{year}-{end}"]
            for quarter, end in (
                (1, "03-31"), (2, "06-30"), (3, "09-30"), (4, "12-31")
            )
        }
        for quarter in (1, 2, 3):
            revenue, net_income = expected[quarter]
            values = {
                "revenue": [revenue, revenue - 1.0],
                "net_income": [net_income, net_income - 1.0],
            }
            if quarter > 1:
                cumulative_revenue = sum(expected[q][0] for q in range(1, quarter + 1))
                cumulative_income = sum(expected[q][1] for q in range(1, quarter + 1))
                values["revenue"] += [cumulative_revenue, cumulative_revenue - 1.0]
                values["net_income"] += [cumulative_income, cumulative_income - 1.0]
            parsed[f"{year}_q{quarter}"] = values
        parsed[f"{year}_fy"] = {
            "revenue": sum(expected[q][0] for q in range(1, 5)),
            "net_income": sum(expected[q][1] for q in range(1, 5)),
        }

    observed = {
        fiscal_end: (values["revenue"], values["net_income"])
        for fiscal_end, values in derive_quarters(parsed).items()
    }

    assert observed == EXPECTED


def test_bldp_interim_parser_anchors_values_to_currency_cells() -> None:
    raw = b"""
    <table>
      <tr><td>Product and service revenues</td><td>Product and service revenues</td>
          <td>16</td><td>16</td><td>$</td><td>24,785</td>
          <td>$</td><td>21,574</td><td>$</td><td>64,444</td>
          <td>$</td><td>68,109</td></tr>
      <tr><td>Net loss for the period</td><td>Net loss for the period</td>
          <td></td><td></td><td>$</td><td>(9,782)</td>
          <td>$</td><td>(6,024)</td><td>$</td><td>(28,777)</td>
          <td>$</td><td>(15,847)</td></tr>
    </table>
    """

    assert parse_interim(raw) == {
        "revenue": [24_785_000.0, 21_574_000.0, 64_444_000.0, 68_109_000.0],
        "net_income": [-9_782_000.0, -6_024_000.0, -28_777_000.0, -15_847_000.0],
    }
