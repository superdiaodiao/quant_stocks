from scripts.research_v14_mlco_quarterly_reports import parse_quarter


def test_mlco_parser_excludes_adjusted_and_per_share_rows() -> None:
    raw = b"""
    <table>
      <tr><td>Total operating revenues</td><td></td><td>528,002</td><td>1,450,641</td></tr>
      <tr><td>Net (loss) income attributable to Melco Resorts &amp; Entertainment Limited per share:</td></tr>
    </table>
    <table>
      <tr><td>Adjusted net (loss) income attributable to Melco Resorts &amp; Entertainment Limited</td><td>$</td><td>(188,484)</td></tr>
      <tr><td>Net (loss) income attributable to Melco Resorts &amp; Entertainment Limited</td><td>$</td><td>(199,734)</td><td>$</td><td>68,139</td></tr>
    </table>
    """
    assert parse_quarter(raw) == {"revenue": 528_002_000.0, "net_income": -199_734_000.0}
