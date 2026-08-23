from __future__ import annotations

import pytest

from scripts.research_v14_pntg_selected_quarters import EXPECTED, strict_selected_quarters


def _html(q1_2018: str = "3,381") -> bytes:
    return f"""
    <html><body>
    Dec. 31, 2019 Sept. 30, 2019 June 30, 2019 Mar. 31, 2019 Dec. 31, 2018 Sept. 30, 2018 June 30, 2018 Mar. 31, 2018
    (In thousands, except per share data)
    Revenues $ 89,492 $ 88,398 $ 82,734 $ 77,907 $ 75,337 $ 72,953 $ 69,789 $ 67,979
    Cost of Services 68,888 68,286 63,038 58,729 56,313 54,167 51,860 50,081
    Total Expenses 90,887 86,472 79,422 76,080 70,621 67,150 64,256 63,400
    Net income (loss) $ (3,799) $ 1,803 $ 3,687 $ 1,484 $ 3,952 $ 4,415 $ 4,442 $ 3,470
    Income attributable to noncontrolling interests $ — $ 279 $ 200 $ 150 $ 182 $ 43 $ 281 $ 89
    Net income (loss) attributable to The Pennant Group, Inc. $ (3,799) $ 1,524 $ 3,487 $ 1,334 $ 3,770 $ 4,372 $ 4,161 $ {q1_2018}
    The summation of quarterly per share information
    </body></html>
    """.encode()


def test_strict_selected_quarters_accepts_exact_table() -> None:
    assert strict_selected_quarters(_html()) == EXPECTED


def test_strict_selected_quarters_rejects_changed_or_duplicate_table() -> None:
    with pytest.raises(RuntimeError, match="values or ordering changed"):
        strict_selected_quarters(_html("3,382"))
    duplicated = _html().replace(
        b"The summation", _html() + b"The summation", 1
    )
    with pytest.raises(RuntimeError, match="not unique"):
        strict_selected_quarters(duplicated)
