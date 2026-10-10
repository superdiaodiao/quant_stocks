import copy

import pytest

from scripts.research_v14_domestic_legacy_quarters import (
    DIRECT_QUARTERS,
    ENSG_PRIOR_QUARTERS,
    ENSG_Q4,
    strict_quarterly_rows,
)


def _fact(spec, concept, value):
    return {
        "start": spec["start"], "end": spec["fiscal_end"], "val": value,
        "accn": spec["accession"], "form": spec["form"],
        "filed": spec["available_date"],
    }


def _payloads():
    payloads = {
        ticker: {"facts": {"us-gaap": {}}}
        for ticker in ("ENSG", "SAIA", "GOOD")
    }

    def add(ticker, concept, fact):
        concepts = payloads[ticker]["facts"]["us-gaap"]
        concepts.setdefault(concept, {"units": {"USD": []}})
        concepts[concept]["units"]["USD"].append(fact)

    for spec in DIRECT_QUARTERS:
        add(spec["ticker"], spec["revenue_concept"],
            _fact(spec, spec["revenue_concept"], spec["revenue"]))
        add(spec["ticker"], "NetIncomeLoss",
            _fact(spec, "NetIncomeLoss", spec["net_income"]))

    add("ENSG", ENSG_Q4["revenue_concept"], {
        "start": ENSG_Q4["start"], "end": ENSG_Q4["fiscal_end"],
        "val": ENSG_Q4["annual_revenue"], "accn": ENSG_Q4["accession"],
        "form": ENSG_Q4["form"], "filed": ENSG_Q4["available_date"],
    })
    add("ENSG", "NetIncomeLoss", {
        "start": ENSG_Q4["start"], "end": ENSG_Q4["fiscal_end"],
        "val": ENSG_Q4["annual_net_income"], "accn": ENSG_Q4["accession"],
        "form": ENSG_Q4["form"], "filed": ENSG_Q4["available_date"],
    })
    for start, end, accession, filed, revenue, net_income in ENSG_PRIOR_QUARTERS:
        if end in {"2017-03-31", "2017-06-30"}:
            continue
        add("ENSG", ENSG_Q4["revenue_concept"], {
            "start": start, "end": end, "val": revenue, "accn": accession,
            "form": "10-Q", "filed": filed,
        })
        add("ENSG", "NetIncomeLoss", {
            "start": start, "end": end, "val": net_income, "accn": accession,
            "form": "10-Q", "filed": filed,
        })
    return payloads


def test_strict_quarterly_rows_preserves_original_pit_versions_and_good_q4():
    rows = strict_quarterly_rows(_payloads())
    assert len(rows) == 14
    assert rows[["ticker", "fiscal_end"]].drop_duplicates().shape[0] == 7
    values = rows.set_index(["ticker", "fiscal_end", "metric"])["value"]
    assert values[("ENSG", "2017-03-31", "net_income")] == 2_840_000
    assert values[("ENSG", "2017-12-31", "revenue")] == 487_705_000
    assert values[("GOOD", "2018-12-31", "net_income")] == 2_517_000
    q4 = rows.loc[
        rows["ticker"].eq("ENSG") & rows["fiscal_end"].eq("2017-12-31")
    ]
    assert set(q4["derivation"]) == {
        "annual_minus_original_pit_direct_q1_q2_q3"
    }


def test_strict_quarterly_rows_rejects_changed_or_duplicate_source_facts():
    changed = _payloads()
    changed["GOOD"]["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"][-1][
        "val"
    ] = 1
    with pytest.raises(ValueError, match="predeclared evidence"):
        strict_quarterly_rows(changed)

    duplicate = _payloads()
    facts = duplicate["SAIA"]["facts"]["us-gaap"][
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    ]["units"]["USD"]
    facts.append(copy.deepcopy(facts[0]))
    with pytest.raises(ValueError, match="not unique"):
        strict_quarterly_rows(duplicate)
