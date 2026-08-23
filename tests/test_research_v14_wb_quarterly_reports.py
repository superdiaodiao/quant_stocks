from scripts.research_v14_wb_quarterly_reports import parse_quarter


def test_wb_parser_uses_gaap_current_quarter_columns() -> None:
    raw = b"""
    <table>
      <tr><td>Net revenues:</td></tr>
      <tr><td>Net revenues</td><td>458,896</td><td>323,389</td><td>513,410</td></tr>
      <tr><td>Net income attributable to Weibo's shareholders</td><td>$</td><td>49,820</td><td>$</td><td>52,108</td><td>$</td><td>29,042</td></tr>
      <tr><td>Non-GAAP net income attributable to Weibo's shareholders</td><td>$</td><td>130,700</td></tr>
    </table>
    """
    assert parse_quarter(raw) == {"revenue": 458_896_000.0, "net_income": 49_820_000.0}
