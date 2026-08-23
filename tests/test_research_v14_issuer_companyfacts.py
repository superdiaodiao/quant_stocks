import json

import pandas as pd
import pytest

from scripts.research_v14_prepare_issuer_companyfacts import (
    DMRC_TRANSITION,
    MRVL_TRANSITION,
    TTGT_TRANSITION,
    AZPN_TRANSITION,
    RCM_TRANSITION,
    IAC_TRANSITION,
    UNIT_TRANSITION,
    declare_dmrc_transition,
    declare_mrvl_transition,
    declare_ttgt_transition,
    declare_azpn_transition,
    declare_rcm_transition,
    declare_iac_transition,
    declare_unit_transition,
)
from src.research.companyfacts_overrides import (
    RESEARCH_HISTORICAL_CIK_OVERRIDES,
    RESEARCH_TRANSITION_OVERRIDES,
    _prefer_explicit_quarter_rows,
    parse_research_concept_override,
    parse_research_currency_override,
    parse_research_historical_cik_override,
    parse_research_concept_cutover_override,
    parse_research_transition_override,
)


def test_exls_transition_is_predeclared_without_backdating() -> None:
    rule = RESEARCH_TRANSITION_OVERRIDES["EXLS"]
    assert rule["cik"] == 1297989
    assert rule["transition_fiscal_end"] == "2018-12-31"
    assert rule["transition_first_filed"] == "2019-02-28"
    assert rule["transition_start"] == "2018-10-01"
    assert len(rule["overlap_fiscal_ends"]) == 3


def _cgc_payload(
    quarter_count: int = 8,
    start: str = "2020-03-31",
) -> dict:
    revenue = []
    net_income = []
    for index, fiscal_end in enumerate(
        pd.date_range(start, periods=quarter_count, freq="QE")
    ):
        start = fiscal_end - pd.Timedelta(days=89)
        filed = fiscal_end + pd.Timedelta(days=40)
        common = {
            "start": str(start.date()),
            "end": str(fiscal_end.date()),
            "filed": str(filed.date()),
            "form": "10-Q",
            "fp": f"Q{index % 4 + 1}",
            "accn": f"000000-{index}",
        }
        revenue.append({**common, "val": 100 + index})
        net_income.append({**common, "val": -10 + index})
    return {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"CAD": revenue}
                },
                "NetIncomeLoss": {"units": {"CAD": net_income}},
            }
        }
    }


def test_currency_override_requires_and_emits_timely_paired_quarters() -> None:
    rows, evidence = parse_research_currency_override(
        "CGC", 1737927, _cgc_payload(), "2022-01-01"
    )
    assert set(rows["metric"]) == {"revenue", "net_income"}
    assert rows["concept"].str.startswith(
        "research_currency_override:CAD:"
    ).all()
    assert evidence["timely_paired_quarters"] == 8
    assert evidence["currency"] == "CAD"


def test_currency_override_rejects_insufficient_history() -> None:
    with pytest.raises(RuntimeError, match="requires 8"):
        parse_research_currency_override(
            "CGC", 1737927, _cgc_payload(7), "2022-01-01"
        )


def test_concept_override_maps_only_declared_contract_income() -> None:
    payload = _cgc_payload()
    source = payload["facts"]["us-gaap"].pop(
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    )
    source["units"]["USD"] = source["units"].pop("CAD")
    net_income = payload["facts"]["us-gaap"]["NetIncomeLoss"]["units"]
    net_income["USD"] = net_income.pop("CAD")
    payload["facts"]["us-gaap"][
        "ResearchAndDevelopmentArrangementContractToPerformForOthers"
        "CompensationEarned"
    ] = source
    rows, evidence = parse_research_concept_override(
        "AVXL", 1314052, payload, "2022-01-01"
    )
    revenue = rows.loc[rows["metric"].eq("revenue")]
    assert len(revenue) == 8
    assert revenue["concept"].str.startswith(
        "research_concept_override:"
    ).all()
    assert evidence["timely_paired_quarters"] == 8


def test_transition_prefers_explicit_quarter_over_rounded_derived_q4() -> None:
    rows = pd.DataFrame([
        {
            "ticker": "AMED", "fiscal_end": "2020-12-31",
            "available_date": "2021-02-25", "metric": "revenue",
            "value": 550700000.0, "concept": "Revenue",
            "accession": "direct",
        },
        {
            "ticker": "AMED", "fiscal_end": "2020-12-31",
            "available_date": "2021-02-25", "metric": "revenue",
            "value": 550705000.0, "concept": "derived_q4:Revenue",
            "accession": "derived",
        },
    ])
    selected = _prefer_explicit_quarter_rows(rows, "AMED")
    assert selected["value"].tolist() == [550700000.0]


def test_transition_rejects_conflicting_explicit_quarter_facts() -> None:
    rows = pd.DataFrame([
        {
            "ticker": "AMED", "fiscal_end": "2020-12-31",
            "available_date": "2021-02-25", "metric": "revenue",
            "value": value, "concept": "Revenue", "accession": accession,
        }
        for value, accession in [(1.0, "a"), (2.0, "b")]
    ])
    with pytest.raises(RuntimeError, match="conflicting same-filing"):
        _prefer_explicit_quarter_rows(rows, "AMED")


def test_dmrc_transition_is_idempotent_and_rejects_conflict(tmp_path) -> None:
    first = declare_dmrc_transition(tmp_path)
    second = declare_dmrc_transition(tmp_path)
    assert first["sha256"] == second["sha256"]
    document = json.loads(
        (tmp_path / "historical_ticker_ciks.json").read_text()
    )
    assert document["entries"]["DMRC"] == DMRC_TRANSITION
    document["entries"]["DMRC"]["predecessor_ciks"] = [999]
    (tmp_path / "historical_ticker_ciks.json").write_text(
        json.dumps(document)
    )
    with pytest.raises(RuntimeError, match="Conflicting"):
        declare_dmrc_transition(tmp_path)


def test_mrvl_transition_is_idempotent_and_rejects_conflict(tmp_path) -> None:
    first = declare_mrvl_transition(tmp_path)
    second = declare_mrvl_transition(tmp_path)
    assert first["sha256"] == second["sha256"]
    document = json.loads(
        (tmp_path / "historical_ticker_ciks.json").read_text()
    )
    assert document["entries"]["MRVL"] == MRVL_TRANSITION
    document["entries"]["MRVL"]["predecessor_ciks"] = [999]
    (tmp_path / "historical_ticker_ciks.json").write_text(
        json.dumps(document)
    )
    with pytest.raises(RuntimeError, match="Conflicting"):
        declare_mrvl_transition(tmp_path)


def test_ttgt_transition_is_idempotent(tmp_path) -> None:
    first = declare_ttgt_transition(tmp_path)
    second = declare_ttgt_transition(tmp_path)
    assert first["sha256"] == second["sha256"]
    document = json.loads(
        (tmp_path / "historical_ticker_ciks.json").read_text()
    )
    assert document["entries"]["TTGT"] == TTGT_TRANSITION


def test_azpn_transition_is_idempotent(tmp_path) -> None:
    first = declare_azpn_transition(tmp_path)
    second = declare_azpn_transition(tmp_path)
    assert first["sha256"] == second["sha256"]
    document = json.loads(
        (tmp_path / "historical_ticker_ciks.json").read_text()
    )
    assert document["entries"]["AZPN"] == AZPN_TRANSITION


def test_azpn_delayed_2020_10k_exception_is_exact_and_pit() -> None:
    rule = RESEARCH_HISTORICAL_CIK_OVERRIDES["AZPN"]
    exceptions = rule["reporting_lag_exceptions"]
    assert exceptions == ({
        "fiscal_end": "2020-06-30",
        "available_date": "2020-12-09",
        "accession": "0000929940-20-000069",
        "maximum_lag_days": 180,
        "required_metrics": ("revenue", "net_income"),
    },)


def test_uvsp_same_cik_reparse_is_bounded_and_strict() -> None:
    rule = RESEARCH_HISTORICAL_CIK_OVERRIDES["UVSP"]
    assert rule["cik"] == rule["successor_cik"] == 102212
    assert rule["minimum_fiscal_end"] == "2017-03-31"
    assert rule["maximum_fiscal_end"] == "2021-12-31"
    assert rule["minimum_paired_quarters"] == 20


def test_rcm_transition_is_idempotent(tmp_path) -> None:
    first = declare_rcm_transition(tmp_path)
    second = declare_rcm_transition(tmp_path)
    assert first["sha256"] == second["sha256"]
    document = json.loads(
        (tmp_path / "historical_ticker_ciks.json").read_text()
    )
    assert document["entries"]["RCM"] == RCM_TRANSITION


def test_rcm_transition_enriches_current_cik_without_predecessor(tmp_path) -> None:
    path = tmp_path / "historical_ticker_ciks.json"
    path.write_text(json.dumps({
        "format_version": 1,
        "entries": {"RCM": {"cik": 1910851, "source_url": "lookup"}},
    }))
    result = declare_rcm_transition(tmp_path)
    assert result["predecessor_ciks"] == [1472595]
    document = json.loads(path.read_text())
    assert document["entries"]["RCM"]["predecessor_ciks"] == [1472595]


def test_iac_transition_is_idempotent_and_rejects_conflict(tmp_path) -> None:
    first = declare_iac_transition(tmp_path)
    second = declare_iac_transition(tmp_path)
    assert first["sha256"] == second["sha256"]
    document = json.loads(
        (tmp_path / "historical_ticker_ciks.json").read_text()
    )
    assert document["entries"]["IAC"] == IAC_TRANSITION
    document["entries"]["IAC"]["predecessor_ciks"] = [999]
    (tmp_path / "historical_ticker_ciks.json").write_text(
        json.dumps(document)
    )
    with pytest.raises(RuntimeError, match="Conflicting"):
        declare_iac_transition(tmp_path)


def test_unit_transition_is_idempotent_and_rejects_conflict(tmp_path) -> None:
    first = declare_unit_transition(tmp_path)
    second = declare_unit_transition(tmp_path)
    assert first["sha256"] == second["sha256"]
    document = json.loads(
        (tmp_path / "historical_ticker_ciks.json").read_text()
    )
    assert document["entries"]["UNIT"] == UNIT_TRANSITION
    document["entries"]["UNIT"]["predecessor_ciks"] = [999]
    (tmp_path / "historical_ticker_ciks.json").write_text(
        json.dumps(document)
    )
    with pytest.raises(RuntimeError, match="Conflicting"):
        declare_unit_transition(tmp_path)


def test_historical_cik_override_is_point_in_time_and_cik_bound() -> None:
    payload = _cgc_payload(16, start="2017-03-31")
    for concept in payload["facts"]["us-gaap"].values():
        concept["units"]["USD"] = concept["units"].pop("CAD")
    rows, evidence = parse_research_historical_cik_override(
        "MRVL", 1058057, payload, "2022-01-01"
    )
    assert evidence["timely_paired_quarters"] == 16
    assert rows["concept"].str.startswith(
        "research_historical_cik_override:"
    ).all()
    assert pd.to_datetime(rows["fiscal_end"]).max() <= pd.Timestamp("2021-01-30")
    with pytest.raises(RuntimeError, match="expected CIK 1058057"):
        parse_research_historical_cik_override(
            "MRVL", 1835632, payload, "2022-01-01"
        )


def test_ttgt_historical_cik_override_uses_pre_combination_facts() -> None:
    payload = _cgc_payload(20, start="2017-03-31")
    for concept in payload["facts"]["us-gaap"].values():
        concept["units"]["USD"] = concept["units"].pop("CAD")
    rows, evidence = parse_research_historical_cik_override(
        "TTGT", 1293282, payload, "2022-01-01"
    )
    assert evidence["successor_cik"] == 2018064
    assert evidence["timely_paired_quarters"] == 20
    assert set(rows["ticker"]) == {"TTGT"}


def test_azpn_historical_cik_override_uses_pre_transaction_facts() -> None:
    payload = _cgc_payload(20, start="2017-03-31")
    for concept in payload["facts"]["us-gaap"].values():
        concept["units"]["USD"] = concept["units"].pop("CAD")
        for row in concept["units"]["USD"]:
            if row["end"] == "2020-06-30":
                row["filed"] = "2020-12-09"
                row["accn"] = "0000929940-20-000069"
                row["form"] = "10-K"
    rows, evidence = parse_research_historical_cik_override(
        "AZPN", 929940, payload, "2022-01-01"
    )
    assert evidence["successor_cik"] == 1897982
    assert evidence["timely_paired_quarters"] == 20
    assert set(rows["ticker"]) == {"AZPN"}
    assert evidence["reporting_lag_exceptions"][0]["available_date"] == (
        "2020-12-09"
    )


def test_rcm_historical_cik_override_uses_pre_transaction_facts() -> None:
    payload = _cgc_payload(17, start="2018-03-31")
    for concept in payload["facts"]["us-gaap"].values():
        concept["units"]["USD"] = concept["units"].pop("CAD")
    rows, evidence = parse_research_historical_cik_override(
        "RCM", 1472595, payload, "2022-01-01"
    )
    assert evidence["successor_cik"] == 1910851
    assert evidence["timely_paired_quarters"] == 17
    assert set(rows["ticker"]) == {"RCM"}


def test_iac_historical_cik_override_stops_before_separation() -> None:
    payload = _cgc_payload(18, start="2016-03-31")
    for concept in payload["facts"]["us-gaap"].values():
        concept["units"]["USD"] = concept["units"].pop("CAD")
    rows, evidence = parse_research_historical_cik_override(
        "IAC", 891103, payload, "2022-01-01"
    )
    assert evidence["successor_cik"] == 1800227
    assert evidence["timely_paired_quarters"] == 17
    assert pd.to_datetime(rows["fiscal_end"]).max() == pd.Timestamp(
        "2020-03-31"
    )
    assert set(rows["ticker"]) == {"IAC"}


def test_unit_historical_cik_override_stops_before_combination() -> None:
    payload = _cgc_payload(38, start="2016-03-31")
    for concept in payload["facts"]["us-gaap"].values():
        concept["units"]["USD"] = concept["units"].pop("CAD")
    rows, evidence = parse_research_historical_cik_override(
        "UNIT", 1620280, payload, "2026-08-12"
    )
    assert evidence["successor_cik"] == 2020795
    assert evidence["timely_paired_quarters"] == 38
    assert pd.to_datetime(rows["fiscal_end"]).max() == pd.Timestamp(
        "2025-06-30"
    )
    assert set(rows["ticker"]) == {"UNIT"}


def test_ilpt_concept_cutover_requires_agreeing_overlap() -> None:
    payload = _cgc_payload(20, start="2017-03-31")
    facts = payload["facts"]["us-gaap"]
    source = facts[
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    ]["units"]["CAD"]
    for row in source:
        row["filed"] = (
            pd.Timestamp(row["end"]) + pd.Timedelta(days=40)
        ).strftime("%Y-%m-%d")
    facts["RealEstateRevenueNet"] = {"units": {"USD": source}}
    facts["OperatingLeaseLeaseIncome"] = {
        "units": {"USD": [dict(row) for row in source]}
    }
    facts["NetIncomeLoss"]["units"]["USD"] = facts[
        "NetIncomeLoss"
    ]["units"].pop("CAD")
    rows, evidence = parse_research_concept_cutover_override(
        "ILPT", 1717307, payload, "2022-01-01"
    )
    assert evidence["longest_timely_paired_chain"] >= 16
    assert set(rows["metric"]) == {"revenue", "net_income"}
    broken = json.loads(json.dumps(payload))
    for row in broken["facts"]["us-gaap"][
        "OperatingLeaseLeaseIncome"
    ]["units"]["USD"]:
        if row["end"] == "2018-03-31":
            row["val"] += 1
    with pytest.raises(RuntimeError, match="lacks one agreeing value"):
        parse_research_concept_cutover_override(
            "ILPT", 1717307, broken, "2022-01-01"
        )


def test_ilpt_cutover_retains_late_filed_preipo_comparatives_pit() -> None:
    payload = _cgc_payload(20, start="2017-03-31")
    facts = payload["facts"]["us-gaap"]
    source = facts[
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    ]["units"]["CAD"]
    for row in source:
        end = pd.Timestamp(row["end"])
        row["filed"] = (
            end + pd.Timedelta(days=400 if end.year == 2017 else 40)
        ).strftime("%Y-%m-%d")
    facts["RealEstateRevenueNet"] = {"units": {"USD": source}}
    facts["OperatingLeaseLeaseIncome"] = {
        "units": {"USD": [dict(row) for row in source]}
    }
    net_income = facts["NetIncomeLoss"]["units"].pop("CAD")
    for row in net_income:
        end = pd.Timestamp(row["end"])
        row["filed"] = (
            end + pd.Timedelta(days=400 if end.year == 2017 else 40)
        ).strftime("%Y-%m-%d")
    facts["NetIncomeLoss"]["units"]["USD"] = net_income

    rows, evidence = parse_research_concept_cutover_override(
        "ILPT", 1717307, payload, "2022-01-01"
    )
    historical = rows.loc[
        pd.to_datetime(rows["fiscal_end"]).dt.year.eq(2017)
    ]
    assert historical["fiscal_end"].nunique() == 4
    assert set(historical["metric"]) == {"revenue", "net_income"}
    assert (
        pd.to_datetime(historical["available_date"])
        > pd.to_datetime(historical["fiscal_end"])
    ).all()
    assert evidence["historical_comparative_max_fiscal_end"] == "2017-12-31"
    assert evidence["maximum_historical_comparative_lag_days"] == 500
