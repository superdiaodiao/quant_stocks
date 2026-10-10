from scripts.research_v14_reg_2019q1_sec_report import parse_statement


def test_parse_statement_requires_bound_headers_and_values():
    html = """
    <table>
      <tr><th rowspan="2">Consolidated Statements of Operations</th>
          <th colspan="2">3 Months Ended</th></tr>
      <tr><th>Mar. 31, 2019</th><th>Mar. 31, 2018</th></tr>
      <tr><td>Total revenues</td><td>286,257</td><td>276,693</td></tr>
      <tr><td>Net income attributable to the Company</td>
          <td>90,446</td><td>52,660</td></tr>
    </table>
    """

    assert parse_statement(html) == (286257000.0, 90446000.0)
