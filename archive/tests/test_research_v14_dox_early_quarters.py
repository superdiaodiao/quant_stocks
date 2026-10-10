from scripts.research_v14_dox_early_quarters import parse_quarter


def test_dox_parser_uses_current_quarter_gaap_values() -> None:
    raw = b"""
    <table>
      <tr><td>Revenue</td><td></td><td>$</td><td>966,695</td>
          <td></td><td></td><td>$</td><td>930,133</td>
          <td></td><td></td><td>$</td><td>2,887,431</td>
          <td></td><td></td><td>$</td><td>2,777,573</td></tr>
      <tr><td>Net income</td><td></td><td>$</td><td>119,264</td>
          <td></td><td></td><td>$</td><td>105,060</td>
          <td></td><td></td><td>$</td><td>329,617</td>
          <td></td><td></td><td>$</td><td>313,622</td></tr>
    </table>
    """

    assert parse_quarter(raw) == {
        "revenue": 966_695_000.0,
        "net_income": 119_264_000.0,
    }
