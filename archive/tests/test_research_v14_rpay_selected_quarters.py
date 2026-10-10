from __future__ import annotations

import pytest

from scripts.research_v14_rpay_selected_quarters import (
    EXPECTED,
    TRANSITION_EXPECTED,
    strict_selected_quarters,
    strict_transition_quarters,
)


def _html(total_revenue: str = "39,249") -> bytes:
    return f"""
    <html><body>
      <h2>Selected Quarterly Results of Operations</h2>
      <p>The following table sets forth selected unaudited quarterly statements
      of operations data for the last eight quarters.</p>
      <p>Three Months Ended March 31, 2019 December 31, 2018
      September 30, 2018 June 30, 2018 March 31, 2018
      December 31, 2017 September 30, 2017 June 30, 2017</p>
      <p>Total revenue $ {total_revenue} $ 33,858 $ 32,292 $ 31,066
      $ 32,797 $ 25,559 $ 22,804 $ 21,747</p>
      <p>Net income $ 4,864 $ 2,145 $ 3,727 $ 4,484 $ 181
      $ 4,431 $ 368 $ 1,632</p>
      <h2>Seasonality</h2>
    </body></html>
    """.encode()


def test_selected_quarters_require_exact_values_and_order() -> None:
    assert strict_selected_quarters(_html()) == EXPECTED


def test_selected_quarters_reject_changed_value() -> None:
    with pytest.raises(RuntimeError, match="values or ordering changed"):
        strict_selected_quarters(_html(total_revenue="39,250"))


def _transition_html(successor_revenue: str = "37,156") -> bytes:
    return f"""
    <html><body>
      <p>Successor Predecessor (in $ thousands)
      July 11, 2019 through September 30, 2019
      July 1, 2019 through July 10, 2019
      January 1, 2019 through July 10, 2019
      Three Months Ended September 30, 2018
      Nine Months Ended September 30, 2018</p>
      <p>Total Revenue $ {successor_revenue} $ 3,907 $ 79,390
      $ 32,292 $ 96,155</p>
      <p>Net income (loss) attributable to the Company
      $ (8,481) $ (32,763) $ (23,743) $ 3,727 $ 8,392</p>
      <h2>Three Months Ended September 30, 2019 Compared</h2>
    </body></html>
    """.encode()


def test_transition_quarters_combine_actual_predecessor_and_successor() -> None:
    assert strict_transition_quarters(_transition_html(), EXPECTED) == (
        TRANSITION_EXPECTED
    )


def test_transition_quarters_reject_changed_value() -> None:
    with pytest.raises(RuntimeError, match="values or ordering changed"):
        strict_transition_quarters(
            _transition_html(successor_revenue="37,157"), EXPECTED
        )
