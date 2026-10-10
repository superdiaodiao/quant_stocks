from __future__ import annotations

from io import BytesIO

import pandas as pd
import pytest

from scripts.research_v14_xp_quarterly_reports import parse_interim


def _statement(*, revenue: str = "2,628,041", income: str = "734,148") -> bytes:
    table = pd.DataFrame([
        ["", "Three months period ended March 31", "Three months period ended March 31"],
        ["", "2021", "2020"],
        ["Net revenue from services rendered", "1,454,656", "1,151,946"],
        ["Net income from financial instruments at amortized cost", "30,884", "202,497"],
        ["Total revenue and income", revenue, "1,734,841"],
        ["Adjusted total revenue", "9,999,999", "9,999,999"],
        ["Net income for the period", income, "397,554"],
        ["Adjusted net income", "9,999,999", "9,999,999"],
    ])
    return table.to_html(index=False, header=False).encode()


def test_parse_interim_accepts_only_total_ifrs_quarter_rows() -> None:
    parsed = parse_interim(
        _statement(), [("2021-03-31", 2021), ("2020-03-31", 2020)]
    )
    assert parsed == {
        "2021-03-31": (2_628_041_000.0, 734_148_000.0),
        "2020-03-31": (1_734_841_000.0, 397_554_000.0),
    }


def test_parse_interim_rejects_component_or_adjusted_substitutes() -> None:
    component = _statement().replace(
        b"Total revenue and income", b"Net revenue from services rendered", 1
    )
    with pytest.raises(RuntimeError, match="lacks"):
        parse_interim(component, [("2021-03-31", 2021)])
    adjusted = _statement().replace(
        b"Net income for the period", b"Adjusted net income", 1
    )
    with pytest.raises(RuntimeError, match="lacks"):
        parse_interim(adjusted, [("2021-03-31", 2021)])


def test_parse_interim_rejects_cumulative_or_ambiguous_columns() -> None:
    cumulative = _statement().replace(b"Three months", b"Six months")
    with pytest.raises(RuntimeError, match="not uniquely proven"):
        parse_interim(cumulative, [("2021-03-31", 2021)])
    duplicate = pd.read_html(BytesIO(_statement()))[0]
    duplicate[3] = duplicate[1]
    duplicate.iloc[0, 3] = "Three months period ended March 31"
    duplicate.iloc[1, 3] = "2021"
    duplicate.iloc[4, 3] = "2,600,000"
    with pytest.raises(RuntimeError, match="not uniquely proven"):
        parse_interim(
            duplicate.to_html(index=False, header=False).encode(),
            [("2021-03-31", 2021)],
        )


def test_parse_interim_accepts_bound_column_for_split_sec_table() -> None:
    split = _statement().replace(b"Three months period ended March 31", b"")
    assert parse_interim(
        split, [("2021-03-31", 2021)], column_hints={"2021-03-31": 1}
    ) == {"2021-03-31": (2_628_041_000.0, 734_148_000.0)}
