import pytest

from scripts.research_v14_vnet_quarterly_reports import validate_statement


def _statement() -> bytes:
    return b"""
      <html><body>21VIANET GROUP, INC.
      CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS
      (Amount in thousands of Renminbi (RMB))
      Three months ended March 31, 2019 March 31, 2020
      Net revenues 871,859 1,090,797
      Cost of revenues (631,084) (856,686)
      Net gain (loss) 6,582 (137,522)
      Net gain (loss) attributable to ordinary shareholders 5,640 (138,763)
      Adjusted net income 999,999 999,999
      </body></html>
    """


def test_validate_statement_accepts_consolidated_gaap_lines() -> None:
    validate_statement(_statement(), "2020-03-31", "1,090,797", "(137,522)")


def test_validate_statement_rejects_wrong_revenue_or_shareholder_income() -> None:
    with pytest.raises(ValueError, match="total net revenues"):
        validate_statement(_statement(), "2020-03-31", "999,999", "(137,522)")
    with pytest.raises(ValueError, match="consolidated GAAP"):
        validate_statement(_statement(), "2020-03-31", "1,090,797", "(138,763)")


def test_validate_statement_rejects_identity_period_and_currency() -> None:
    with pytest.raises(ValueError, match="identity"):
        validate_statement(_statement().replace(b"21VIANET", b"OTHER"),
                           "2020-03-31", "1,090,797", "(137,522)")
    with pytest.raises(ValueError, match="quarter"):
        validate_statement(_statement(), "2020-06-30", "1,090,797", "(137,522)")
    with pytest.raises(ValueError, match="currency"):
        validate_statement(_statement().replace(b"Renminbi (RMB)", b"USD"),
                           "2020-03-31", "1,090,797", "(137,522)")


def test_validate_statement_accepts_2020q2_cumulative_less_q1() -> None:
    statement = b"""
      <html><body>21VIANET GROUP, INC.
      CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS
      (Amounts in thousands of RMB)
      For the six months periods ended June 30, 2019 2020 RMB RMB US$
      Net revenues Hosting and related services 1,759,879 2,234,858 316,324
      Cost of revenues (1,290,856) (1,728,415) (244,641)
      Net loss (92,693) (1,786,299) (252,833)
      Net loss attributable to ordinary shareholders (96,420) (2,261,756)
      </body></html>
    """
    validate_statement(statement, "2020-06-30", "1,144,061", "(1,648,777)")
