from __future__ import annotations

import pytest

from scripts.research_v14_cgc_selected_quarters import (
    EXPECTED,
    strict_selected_quarters,
)


def _html(second_table_value: str = "242,650") -> bytes:
    return f"""
    <html><body>
    The following tables present our unaudited quarterly results of operations for the eight consecutive quarters ended March 31, 2020:
    QUARTER ENDED June 30, September 30, December 31, March 31,
    2019 2019 2019 2020 Full year
    Net revenue $ 90,482 $ 76,613 $ 123,764 $ 107,913 $ 398,772
    Gross margin $ 18,290 $ 3,643 $ 38,208 $ (91,825) $ (31,684)
    Net (loss) income $ (194,051) $ {second_table_value} $ (109,634) $ (1,326,405) $ (1,387,440)
    Net (loss) income attributable to Canopy Growth Corporation $ (185,869) $ 258,918 $ (91,354) $ (1,303,021) $ (1,321,326)
    QUARTER ENDED June 30, September 30, December 31, March 31,
    2018 2018 2018 2019 Full year
    Net revenue $ 25,916 $ 23,327 $ 83,048 $ 94,050 $ 226,341
    Gross margin $ 7,464 $ (19,336) $ 19,072 $ 21,045 $ 28,245
    Net (loss) income $ (93,299) $ (310,428) $ 39,194 $ (347,492) $ (712,025)
    Net (loss) income attributable to Canopy Growth Corporation $ (89,671) $ (317,830) $ 50,736 $ (379,516) $ (736,281)
    Critical Accounting Policies and Estimates
    </body></html>
    """.encode()


def test_strict_selected_quarters_accepts_exact_same_basis_table() -> None:
    assert strict_selected_quarters(_html()) == EXPECTED


def test_strict_selected_quarters_rejects_changed_or_duplicate_table() -> None:
    with pytest.raises(RuntimeError, match="values or ordering changed"):
        strict_selected_quarters(_html("242,651"))
    duplicated = _html().replace(b"Critical Accounting", _html() + b"Critical Accounting")
    with pytest.raises(RuntimeError, match="not unique"):
        strict_selected_quarters(duplicated)
