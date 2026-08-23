from __future__ import annotations

import pytest

from scripts.research_v14_rgld_legacy_revenue import strict_facts


def _fact(start: str, end: str, value: float, filed: str, accn: str = "0001558370-17-006462") -> dict:
    return {"start": start, "end": end, "val": value, "filed": filed,
            "form": "10-K", "accn": accn}


def _payload(facts: list[dict]) -> dict:
    return {"facts": {"us-gaap": {"RoyaltyRevenue": {
        "units": {"USD": facts}
    }}}}


def test_strict_fact_accepts_only_original_three_month_revenue() -> None:
    direct = _fact("2017-04-01", "2017-06-30", 108_934_000, "2017-08-10")
    direct_2018 = _fact("2018-04-01", "2018-06-30", 116_235_000,
                        "2018-08-09", "0001558370-18-006805")
    cumulative = _fact("2016-07-01", "2017-06-30", 440_814_000, "2017-08-10")
    later = _fact("2017-04-01", "2017-06-30", 108_934_000, "2018-08-09",
                  "0001558370-18-006805")
    assert strict_facts(_payload([direct, direct_2018, cumulative, later])) == [
        direct, direct_2018
    ]


def test_strict_fact_rejects_missing_conflicting_or_wrong_unit_fact() -> None:
    with pytest.raises(RuntimeError, match="not unique"):
        strict_facts(_payload([]))
    direct = _fact("2017-04-01", "2017-06-30", 1, "2017-08-10")
    with pytest.raises(RuntimeError, match="value changed"):
        strict_facts(_payload([direct]))
    payload = _payload([])
    payload["facts"]["us-gaap"]["RoyaltyRevenue"]["units"] = {"CAD": []}
    with pytest.raises(ValueError, match="only USD"):
        strict_facts(payload)
