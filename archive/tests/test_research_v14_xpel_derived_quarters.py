from __future__ import annotations

import pytest

from scripts.research_v14_xpel_derived_quarters import EXPECTED, METRICS, derived_quarters


def _payload() -> dict:
    facts = {}
    values = {
        (2018, "revenue"): (53_912_410, 28_790_891, 29_215_325, 109_920_614),
        (2019, "revenue"): (54_819_600, 30_094_154, 35_617_998, 129_932_881),
        (2018, "net_income"): (4_652_468, 2_555_110, 2_166_192, 8_712_534),
        (2019, "net_income"): (4_864_604, 3_006_017, 4_502_683, 13_977_625),
    }
    for metric, concept in METRICS.items():
        rows = []
        for year in (2018, 2019):
            h1, q2, q3, annual = values[(year, metric)]
            rows.extend([
                {"start": f"{year}-01-01", "end": f"{year}-06-30", "val": h1,
                 "filed": "2019-08-21", "form": "10-Q", "accn": "0001767258-19-000018"},
                {"start": f"{year}-04-01", "end": f"{year}-06-30", "val": q2,
                 "filed": "2019-08-21", "form": "10-Q", "accn": "0001767258-19-000018"},
                {"start": f"{year}-07-01", "end": f"{year}-09-30", "val": q3,
                 "filed": "2019-11-08", "form": "10-Q", "accn": "0001767258-19-000030"},
                {"start": f"{year}-01-01", "end": f"{year}-12-31", "val": annual,
                 "filed": "2020-03-16", "form": "10-K", "accn": "0001767258-20-000011"},
            ])
        facts[concept] = {"units": {"USD": rows}}
    return {"facts": {"us-gaap": facts}}


def test_derived_quarters_match_exact_values_and_availability() -> None:
    rows, bindings = derived_quarters(_payload())
    observed = {
        (int(row["fiscal_end"][:4]), row["metric"], 1 if row["fiscal_end"].endswith("03-31") else 4): row["value"]
        for row in rows
    }
    assert observed == EXPECTED
    assert len(rows) == len(bindings) == 8
    assert {row["available_date"] for row in rows if row["fiscal_end"].endswith("03-31")} == {"2019-08-21"}
    assert {row["available_date"] for row in rows if row["fiscal_end"].endswith("12-31")} == {"2020-03-16"}


def test_derived_quarters_reject_duplicate_duration_fact() -> None:
    payload = _payload()
    concept = METRICS["revenue"]
    payload["facts"]["us-gaap"][concept]["units"]["USD"].append(
        dict(payload["facts"]["us-gaap"][concept]["units"]["USD"][0])
    )
    with pytest.raises(RuntimeError, match="not unique"):
        derived_quarters(payload)
