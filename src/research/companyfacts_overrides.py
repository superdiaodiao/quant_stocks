"""Strict issuer-specific Company Facts overrides for research-only datasets.

These rules deliberately live outside the formal SEC parser.  They are narrow,
manifestable exceptions for issuers whose otherwise standard US-GAAP facts are
reported in one non-USD presentation currency.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.io.fundamentals_update import (
    OUTPUT_COLUMNS,
    parse_companyfacts_quarterly,
)


RESEARCH_CURRENCY_OVERRIDES = {
    "CGC": {
        "cik": 1737927,
        "currency": "CAD",
        "taxonomy": "us-gaap",
        "concepts": (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "NetIncomeLoss",
        ),
        "minimum_paired_quarters": 8,
        "maximum_reporting_lag_days": 150,
    },
}

RESEARCH_CONCEPT_OVERRIDES = {
    "AVXL": {
        "cik": 1314052,
        "taxonomy": "us-gaap",
        "source_concept": (
            "ResearchAndDevelopmentArrangementContractToPerformForOthers"
            "CompensationEarned"
        ),
        "source_unit": "USD",
        "target_metric": "revenue",
        "minimum_paired_quarters": 8,
        "maximum_reporting_lag_days": 150,
        "semantic_basis": (
            "US-GAAP label and definition identify compensation earned as "
            "contract income for R&D performed for others"
        ),
    },
    "IGMS": {
        "cik": 1496323,
        "taxonomy": "us-gaap",
        "source_concept": (
            "RevenueFromCollaborativeArrangementExcludingRevenueFromContract"
            "WithCustomer"
        ),
        "source_unit": "USD",
        "target_metric": "revenue",
        "minimum_paired_quarters": 8,
        "maximum_reporting_lag_days": 150,
        "semantic_basis": (
            "US-GAAP label and definition explicitly identify collaborative "
            "arrangement revenue"
        ),
    },
}

RESEARCH_TRANSITION_OVERRIDES = {
    "AMED": {
        "cik": 896262,
        "taxonomy": "us-gaap",
        "old_concept": "HealthCareOrganizationPatientServiceRevenue",
        "new_concept": (
            "HealthCareOrganizationRevenueNetOfPatientServiceRevenueProvisions"
        ),
        "source_unit": "USD",
        "old_concept_last_fiscal_end": "2018-09-30",
        "transition_fiscal_end": "2018-12-31",
        "transition_start": "2018-10-01",
        "transition_first_filed": "2019-02-28",
        "new_concept_first_fiscal_end": "2019-03-31",
        "overlap_fiscal_ends": (
            "2018-03-31", "2018-06-30", "2018-09-30"
        ),
        "minimum_paired_quarters": 8,
        "maximum_reporting_lag_days": 150,
        "semantic_basis": (
            "AMED migrated between two US-GAAP health-care revenue concepts; "
            "three overlapping 2018 quarters agree exactly, and the transition "
            "quarter is an explicit three-month fact in the first filed 2018 10-K"
        ),
    },
    "EXLS": {
        "cik": 1297989,
        "taxonomy": "us-gaap",
        "old_concept": "SalesRevenueServicesNet",
        "new_concept": (
            "RevenueFromContractWithCustomerExcludingAssessedTax"
        ),
        "source_unit": "USD",
        "old_concept_last_fiscal_end": "2018-03-31",
        "transition_fiscal_end": "2018-12-31",
        "transition_start": "2018-10-01",
        "transition_first_filed": "2019-02-28",
        "new_concept_first_fiscal_end": "2018-06-30",
        "overlap_fiscal_ends": (
            "2017-03-31", "2017-06-30", "2017-09-30"
        ),
        "minimum_paired_quarters": 8,
        "maximum_reporting_lag_days": 150,
        "semantic_basis": (
            "EXLS adopted ASC 606 between SalesRevenueServicesNet and "
            "RevenueFromContractWithCustomerExcludingAssessedTax; three "
            "overlapping 2017 quarters agree exactly, and the missing 2018Q4 "
            "value is an explicit three-month fact in the first filed 2018 10-K"
        ),
    },
}

RESEARCH_HISTORICAL_CIK_OVERRIDES = {
    "UVSP": {
        "cik": 102212,
        "successor_cik": 102212,
        "minimum_fiscal_end": "2017-03-31",
        "maximum_fiscal_end": "2021-12-31",
        "minimum_paired_quarters": 20,
        "maximum_reporting_lag_days": 150,
        "semantic_basis": (
            "UVSP remains under CIK 102212; this bounded same-CIK reparse "
            "applies the validated bank zero-Revenues placeholder rule so "
            "annual net-interest plus noninterest income, rather than a zero "
            "generic total, supplies contemporaneous Q4 revenue"
        ),
    },
    "MRVL": {
        "cik": 1058057,
        "successor_cik": 1835632,
        "minimum_fiscal_end": "2016-01-30",
        "maximum_fiscal_end": "2021-01-30",
        "minimum_paired_quarters": 16,
        "maximum_reporting_lag_days": 150,
        "semantic_basis": (
            "SEC predecessor CIK 1058057 is MARVELL TECHNOLOGY GROUP LTD and "
            "contains MRVL's contemporaneously filed quarterly facts before "
            "the successor issuer began reporting under CIK 1835632"
        ),
    },
    "TTGT": {
        "cik": 1293282,
        "successor_cik": 2018064,
        "minimum_fiscal_end": "2016-03-31",
        "maximum_fiscal_end": "2024-09-30",
        "minimum_paired_quarters": 20,
        "maximum_reporting_lag_days": 150,
        "semantic_basis": (
            "SEC predecessor CIK 1293282 was TechTarget Inc through the "
            "December 2024 combination, after which ticker TTGT is reported "
            "by former Toro CombineCo under successor CIK 2018064"
        ),
    },
    "AZPN": {
        "cik": 929940,
        "successor_cik": 1897982,
        "minimum_fiscal_end": "2016-03-31",
        "maximum_fiscal_end": "2022-03-31",
        "minimum_paired_quarters": 20,
        "maximum_reporting_lag_days": 150,
        "reporting_lag_exceptions": ({
            "fiscal_end": "2020-06-30",
            "available_date": "2020-12-09",
            "accession": "0000929940-20-000069",
            "maximum_lag_days": 180,
            "required_metrics": ("revenue", "net_income"),
        },),
        "semantic_basis": (
            "SEC predecessor CIK 929940 was Aspen Technology Inc before the "
            "May 2022 transaction, after which the AZPN reporting entity used "
            "former Emersub CX under successor CIK 1897982. The delayed 2020 "
            "10-K supplies the first-filed 2020Q4 pair on 2020-12-09; it is "
            "admitted only at that actual filing date"
        ),
    },
    "RCM": {
        "cik": 1472595,
        "successor_cik": 1910851,
        "minimum_fiscal_end": "2018-03-31",
        "maximum_fiscal_end": "2022-03-31",
        "minimum_paired_quarters": 17,
        "maximum_reporting_lag_days": 150,
        "semantic_basis": (
            "SEC predecessor CIK 1472595 was R1 RCM Inc before the June 2022 "
            "transaction, after which former Project Roadrunner Parent became "
            "the reporting parent under successor CIK 1910851"
        ),
    },
    "IAC": {
        "cik": 891103,
        "successor_cik": 1800227,
        "minimum_fiscal_end": "2016-03-31",
        "maximum_fiscal_end": "2020-03-31",
        "minimum_paired_quarters": 17,
        "maximum_reporting_lag_days": 150,
        "semantic_basis": (
            "SEC separation 8-K identifies CIK 891103 as Old IAC and CIK "
            "1800227 as IAC Holdings/New IAC; the 2020-06-30 separation "
            "moved the non-Match IAC businesses to New IAC, while the "
            "pre-separation IAC security and contemporaneous filings remain "
            "under predecessor CIK 891103"
        ),
    },
    "UNIT": {
        "cik": 1620280,
        "successor_cik": 2020795,
        "minimum_fiscal_end": "2016-03-31",
        "maximum_fiscal_end": "2025-06-30",
        "minimum_paired_quarters": 38,
        "maximum_reporting_lag_days": 150,
        "semantic_basis": (
            "SEC Form 8-K12B states that on 2025-08-01 former Windstream "
            "Parent CIK 2020795 became New Uniti, while old Uniti CIK "
            "1620280 survived as a subsidiary; contemporaneous UNIT facts "
            "before the closing therefore come from CIK 1620280"
        ),
    },
}

RESEARCH_CONCEPT_CUTOVER_OVERRIDES = {
    "ILPT": {
        "cik": 1717307,
        "taxonomy": "us-gaap",
        "unit": "USD",
        "old_concept": "RealEstateRevenueNet",
        "new_concept": "OperatingLeaseLeaseIncome",
        "overlap_fiscal_ends": [
            "2018-03-31", "2018-06-30", "2018-09-30"
        ],
        "old_concept_last_fiscal_end": "2018-12-31",
        "new_concept_first_fiscal_end": "2019-03-31",
        "maximum_reporting_lag_days": 150,
        "historical_comparative_max_fiscal_end": "2017-12-31",
        "maximum_historical_comparative_lag_days": 500,
        "minimum_paired_quarters": 16,
        "semantic_basis": (
            "ILPT's REIT lease revenue migrated from deprecated "
            "RealEstateRevenueNet to OperatingLeaseLeaseIncome; both concepts "
            "report identical 2018Q1-Q3 values in the Company Facts payload. "
            "Its first post-IPO 2018 filings also disclose 2017 predecessor "
            "comparatives; those facts remain point-in-time at their actual "
            "filing dates rather than being backdated"
        ),
    },
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_cache_envelope(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    required = {"cik", "symbols", "fetched_at", "source_url", "payload"}
    missing = required - set(envelope)
    if missing:
        raise RuntimeError(
            f"Company Facts cache envelope {path} is missing {sorted(missing)}"
        )
    return envelope


def _translated_payload(payload: dict, rule: dict) -> dict:
    """Expose one declared source currency to the existing parser as USD.

    Values are not converted: growth calculations are scale invariant, and a
    currency conversion would add exchange-rate lookahead and another data
    dependency.  The output concept records the original source currency.
    """
    taxonomy = rule["taxonomy"]
    currency = rule["currency"]
    source_namespace = payload.get("facts", {}).get(taxonomy, {})
    namespace = {}
    for concept in rule["concepts"]:
        source = source_namespace.get(concept)
        if not source:
            continue
        rows = source.get("units", {}).get(currency)
        if not rows:
            continue
        translated = copy.deepcopy(source)
        translated["units"] = {"USD": copy.deepcopy(rows)}
        namespace[concept] = translated
    if not namespace:
        raise RuntimeError(
            f"No declared {currency} facts found for issuer override"
        )
    return {"facts": {taxonomy: namespace}}


def parse_research_currency_override(
    symbol: str,
    cik: int,
    payload: dict,
    fetched_at,
) -> tuple[pd.DataFrame, dict]:
    """Parse and validate one predeclared issuer/currency exception."""
    normalized = str(symbol).strip().upper()
    rule = RESEARCH_CURRENCY_OVERRIDES.get(normalized)
    if rule is None:
        raise ValueError(f"No research currency override for {normalized}")
    if int(cik) != int(rule["cik"]):
        raise RuntimeError(
            f"{normalized} override expected CIK {rule['cik']}, got {cik}"
        )
    parsed = parse_companyfacts_quarterly(
        normalized, _translated_payload(payload, rule), fetched_at
    )
    parsed = parsed.loc[
        parsed["metric"].isin({"revenue", "net_income"})
    ].copy()
    if parsed.empty:
        raise RuntimeError(f"{normalized} override parsed no quarterly rows")
    lag = (
        pd.to_datetime(parsed["available_date"])
        - pd.to_datetime(parsed["fiscal_end"])
    ).dt.days
    parsed = parsed.loc[
        lag.between(0, int(rule["maximum_reporting_lag_days"]))
    ].copy()
    parsed["concept"] = (
        "research_currency_override:"
        + rule["currency"]
        + ":"
        + parsed["concept"].astype(str)
    )
    paired = (
        parsed.pivot_table(
            index=["fiscal_end", "available_date"],
            columns="metric",
            values="value",
            aggfunc="first",
        )
        .dropna(subset=["revenue", "net_income"])
        .reset_index()
    )
    paired_quarters = int(paired["fiscal_end"].nunique())
    minimum = int(rule["minimum_paired_quarters"])
    if paired_quarters < minimum:
        raise RuntimeError(
            f"{normalized} override has {paired_quarters} timely paired "
            f"quarters; requires {minimum}"
        )
    evidence = {
        "ticker": normalized,
        "cik": int(cik),
        "currency": rule["currency"],
        "taxonomy": rule["taxonomy"],
        "concepts": list(rule["concepts"]),
        "maximum_reporting_lag_days": int(
            rule["maximum_reporting_lag_days"]
        ),
        "minimum_paired_quarters": minimum,
        "timely_paired_quarters": paired_quarters,
        "output_rows": int(len(parsed)),
        "first_fiscal_end": str(pd.to_datetime(parsed["fiscal_end"]).min().date()),
        "last_fiscal_end": str(pd.to_datetime(parsed["fiscal_end"]).max().date()),
        "validation_rule": (
            "single_declared_source_currency; no FX conversion; standard "
            "quarter-duration parser; paired revenue/net-income; point-in-time "
            "reporting lag"
        ),
    }
    return parsed[OUTPUT_COLUMNS].sort_values(
        ["ticker", "available_date", "fiscal_end", "metric"]
    ), evidence


def parse_research_concept_override(
    symbol: str,
    cik: int,
    payload: dict,
    fetched_at,
) -> tuple[pd.DataFrame, dict]:
    """Map one issuer-specific US-GAAP income concept to revenue."""
    normalized = str(symbol).strip().upper()
    rule = RESEARCH_CONCEPT_OVERRIDES.get(normalized)
    if rule is None:
        raise ValueError(f"No research concept override for {normalized}")
    if int(cik) != int(rule["cik"]):
        raise RuntimeError(
            f"{normalized} override expected CIK {rule['cik']}, got {cik}"
        )
    taxonomy = rule["taxonomy"]
    concept = rule["source_concept"]
    unit = rule["source_unit"]
    source = (
        payload.get("facts", {})
        .get(taxonomy, {})
        .get(concept, {})
        .get("units", {})
        .get(unit)
    )
    if not source:
        raise RuntimeError(
            f"{normalized} has no {unit} facts for {concept}"
        )
    translated = copy.deepcopy(payload)
    translated.setdefault("facts", {}).setdefault(taxonomy, {})[
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    ] = {"units": {"USD": copy.deepcopy(source)}}
    parsed = parse_companyfacts_quarterly(
        normalized, translated, fetched_at
    )
    parsed = parsed.loc[
        parsed["metric"].isin({"revenue", "net_income"})
    ].copy()
    parsed = parsed.loc[
        parsed["metric"].eq("net_income")
        | parsed["concept"].astype(str).str.contains(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            regex=False,
        )
    ].copy()
    lag = (
        pd.to_datetime(parsed["available_date"])
        - pd.to_datetime(parsed["fiscal_end"])
    ).dt.days
    parsed = parsed.loc[
        lag.between(0, int(rule["maximum_reporting_lag_days"]))
    ].copy()
    revenue_mask = parsed["metric"].eq("revenue")
    parsed.loc[revenue_mask, "concept"] = (
        "research_concept_override:" + concept
    )
    paired = (
        parsed.pivot_table(
            index=["fiscal_end", "available_date"],
            columns="metric",
            values="value",
            aggfunc="first",
        )
        .dropna(subset=["revenue", "net_income"])
        .reset_index()
    )
    paired_quarters = int(paired["fiscal_end"].nunique())
    minimum = int(rule["minimum_paired_quarters"])
    if paired_quarters < minimum:
        raise RuntimeError(
            f"{normalized} concept override has {paired_quarters} timely "
            f"paired quarters; requires {minimum}"
        )
    evidence = {
        "ticker": normalized,
        "cik": int(cik),
        "taxonomy": taxonomy,
        "source_concept": concept,
        "source_unit": unit,
        "target_metric": rule["target_metric"],
        "semantic_basis": rule["semantic_basis"],
        "maximum_reporting_lag_days": int(
            rule["maximum_reporting_lag_days"]
        ),
        "minimum_paired_quarters": minimum,
        "timely_paired_quarters": paired_quarters,
        "output_rows": int(len(parsed)),
        "first_fiscal_end": str(pd.to_datetime(parsed["fiscal_end"]).min().date()),
        "last_fiscal_end": str(pd.to_datetime(parsed["fiscal_end"]).max().date()),
        "validation_rule": (
            "issuer-and-CIK exact match; one declared US-GAAP concept; "
            "standard quarter-duration parser; paired revenue/net-income; "
            "point-in-time reporting lag"
        ),
    }
    return parsed[OUTPUT_COLUMNS].sort_values(
        ["ticker", "available_date", "fiscal_end", "metric"]
    ), evidence


def _parse_declared_revenue_concept(
    symbol: str,
    payload: dict,
    fetched_at,
    *,
    taxonomy: str,
    concept: str,
    unit: str,
) -> pd.DataFrame:
    source = (
        payload.get("facts", {})
        .get(taxonomy, {})
        .get(concept, {})
        .get("units", {})
        .get(unit)
    )
    if not source:
        raise RuntimeError(f"{symbol} has no {unit} facts for {concept}")
    translated = copy.deepcopy(payload)
    translated.setdefault("facts", {}).setdefault(taxonomy, {})[
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    ] = {"units": {"USD": copy.deepcopy(source)}}
    parsed = parse_companyfacts_quarterly(symbol, translated, fetched_at)
    revenue = parsed.loc[parsed["metric"].eq("revenue")].copy()
    revenue = _prefer_explicit_quarter_rows(revenue, symbol)
    revenue["concept"] = "research_concept_override:" + concept
    return revenue


def _prefer_explicit_quarter_rows(
    rows: pd.DataFrame,
    symbol: str,
) -> pd.DataFrame:
    """Resolve same-filing direct-versus-derived Q4 alternatives.

    A filing can contain both an explicit three-month fact and an annual-minus-
    nine-month derived Q4.  They occasionally differ by rounding.  The explicit
    quarter is the narrower disclosed fact and is preferred.  Conflicting facts
    of the same kind remain an error rather than an arbitrary first-row choice.
    """
    if rows.empty:
        return rows.copy()
    keys = ["ticker", "fiscal_end", "available_date", "metric"]
    selected = []
    for identity, group in rows.groupby(keys, dropna=False, sort=False):
        group = group.drop_duplicates(subset=["value", "concept", "accession"])
        if len(group) == 1:
            selected.append(group.iloc[[0]])
            continue
        explicit = group.loc[
            ~group["concept"].astype(str).str.startswith("derived_q4:")
        ]
        candidates = explicit if not explicit.empty else group
        values = candidates["value"].astype(float).unique()
        if len(values) != 1:
            raise RuntimeError(
                f"{symbol} has conflicting same-filing quarterly facts for "
                f"{identity}: {sorted(values.tolist())}"
            )
        selected.append(candidates.iloc[[0]])
    return pd.concat(selected, ignore_index=True)


def _historical_cik_reporting_lag_filter(
    parsed: pd.DataFrame,
    *,
    normalized: str,
    rule: dict,
) -> tuple[pd.DataFrame, list[dict]]:
    """Apply the standard lag bound plus exact declared filing exceptions."""
    lag = (
        pd.to_datetime(parsed["available_date"])
        - pd.to_datetime(parsed["fiscal_end"])
    ).dt.days
    accepted = lag.between(0, int(rule["maximum_reporting_lag_days"]))
    evidence = []
    for exception in rule.get("reporting_lag_exceptions", ()):
        mask = (
            pd.to_datetime(parsed["fiscal_end"])
            .eq(pd.Timestamp(exception["fiscal_end"]))
            & pd.to_datetime(parsed["available_date"])
            .eq(pd.Timestamp(exception["available_date"]))
            & parsed["accession"].astype(str).eq(exception["accession"])
            & lag.between(0, int(exception["maximum_lag_days"]))
        )
        matched = parsed.loc[mask]
        required = set(exception["required_metrics"])
        actual = set(matched["metric"].astype(str))
        if actual != required:
            raise RuntimeError(
                f"{normalized} reporting-lag exception expected metrics "
                f"{sorted(required)}, found {sorted(actual)}"
            )
        evidence.append({
            "fiscal_end": exception["fiscal_end"],
            "available_date": exception["available_date"],
            "accession": exception["accession"],
            "maximum_lag_days": int(exception["maximum_lag_days"]),
            "metrics": sorted(actual),
            "values": {
                row.metric: float(row.value)
                for row in matched.itertuples(index=False)
            },
        })
        accepted |= mask
    return parsed.loc[accepted].copy(), evidence


def parse_research_transition_override(
    symbol: str,
    cik: int,
    payload: dict,
    fetched_at,
) -> tuple[pd.DataFrame, dict]:
    """Bridge one declared issuer concept transition without lookahead."""
    normalized = str(symbol).strip().upper()
    rule = RESEARCH_TRANSITION_OVERRIDES.get(normalized)
    if rule is None:
        raise ValueError(f"No research transition override for {normalized}")
    if int(cik) != int(rule["cik"]):
        raise RuntimeError(
            f"{normalized} transition expected CIK {rule['cik']}, got {cik}"
        )
    taxonomy = rule["taxonomy"]
    unit = rule["source_unit"]
    old_concept = rule["old_concept"]
    new_concept = rule["new_concept"]
    old_rows = _parse_declared_revenue_concept(
        normalized, payload, fetched_at,
        taxonomy=taxonomy, concept=old_concept, unit=unit,
    )
    new_rows = _parse_declared_revenue_concept(
        normalized, payload, fetched_at,
        taxonomy=taxonomy, concept=new_concept, unit=unit,
    )

    overlap = []
    for fiscal_end_text in rule["overlap_fiscal_ends"]:
        fiscal_end = pd.Timestamp(fiscal_end_text)
        old_values = set(old_rows.loc[
            pd.to_datetime(old_rows["fiscal_end"]).eq(fiscal_end), "value"
        ].astype(float))
        new_values = set(new_rows.loc[
            pd.to_datetime(new_rows["fiscal_end"]).eq(fiscal_end), "value"
        ].astype(float))
        agreeing = sorted(old_values & new_values)
        if len(agreeing) != 1:
            raise RuntimeError(
                f"{normalized} concept transition lacks one agreeing value "
                f"for {fiscal_end_text}: old={sorted(old_values)}, "
                f"new={sorted(new_values)}"
            )
        overlap.append({
            "fiscal_end": fiscal_end_text,
            "agreeing_value": agreeing[0],
        })

    transition_end = pd.Timestamp(rule["transition_fiscal_end"])
    transition_start = pd.Timestamp(rule["transition_start"])
    transition_filed = pd.Timestamp(rule["transition_first_filed"])
    raw_transition = []
    source = (
        payload.get("facts", {})
        .get(taxonomy, {})
        .get(new_concept, {})
        .get("units", {})
        .get(unit, [])
    )
    for row in source:
        start = pd.to_datetime(row.get("start"), errors="coerce")
        end = pd.to_datetime(row.get("end"), errors="coerce")
        filed = pd.to_datetime(row.get("filed"), errors="coerce")
        value = pd.to_numeric(row.get("val"), errors="coerce")
        if (
            start == transition_start
            and end == transition_end
            and filed == transition_filed
            and row.get("form") in {"10-K", "10-K/A"}
            and pd.notna(value)
        ):
            raw_transition.append((float(value), str(row.get("accn"))))
    raw_transition = sorted(set(raw_transition))
    if len(raw_transition) != 1:
        raise RuntimeError(
            f"{normalized} expected one first-filed transition-quarter fact, "
            f"found {raw_transition}"
        )
    transition_value, transition_accession = raw_transition[0]
    transition_row = pd.DataFrame([{
        "ticker": normalized,
        "fiscal_end": transition_end,
        "available_date": transition_filed,
        "metric": "revenue",
        "value": transition_value,
        "taxonomy": taxonomy,
        "concept": (
            "research_transition_override:unframed_10k_quarter:"
            + new_concept
        ),
        "form": "10-K",
        "accession": transition_accession,
        "fetched_at": pd.Timestamp(fetched_at).tz_localize(None).normalize(),
    }])

    old_cutoff = pd.Timestamp(rule["old_concept_last_fiscal_end"])
    new_start = pd.Timestamp(rule["new_concept_first_fiscal_end"])
    revenue = pd.concat([
        old_rows.loc[pd.to_datetime(old_rows["fiscal_end"]).le(old_cutoff)],
        transition_row,
        new_rows.loc[pd.to_datetime(new_rows["fiscal_end"]).ge(new_start)],
    ], ignore_index=True)
    standard = parse_companyfacts_quarterly(normalized, payload, fetched_at)
    net_income = standard.loc[standard["metric"].eq("net_income")].copy()
    net_income = _prefer_explicit_quarter_rows(net_income, normalized)
    parsed = pd.concat([revenue, net_income], ignore_index=True)
    lag = (
        pd.to_datetime(parsed["available_date"])
        - pd.to_datetime(parsed["fiscal_end"])
    ).dt.days
    parsed = parsed.loc[
        lag.between(0, int(rule["maximum_reporting_lag_days"]))
    ].copy()
    paired = (
        parsed.pivot_table(
            index=["fiscal_end", "available_date"],
            columns="metric",
            values="value",
            aggfunc="first",
        )
        .dropna(subset=["revenue", "net_income"])
        .reset_index()
    )
    paired_ends = sorted(pd.to_datetime(paired["fiscal_end"]).unique())
    longest = current = 1 if paired_ends else 0
    for left, right in zip(paired_ends, paired_ends[1:]):
        if 60 <= (right - left).days <= 135:
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    minimum = int(rule["minimum_paired_quarters"])
    if longest < minimum:
        raise RuntimeError(
            f"{normalized} transition has longest paired chain {longest}; "
            f"requires {minimum}"
        )
    evidence = {
        "ticker": normalized,
        "cik": int(cik),
        "taxonomy": taxonomy,
        "source_unit": unit,
        "old_concept": old_concept,
        "new_concept": new_concept,
        "semantic_basis": rule["semantic_basis"],
        "overlap_checks": overlap,
        "transition_quarter": {
            "fiscal_end": rule["transition_fiscal_end"],
            "available_date": rule["transition_first_filed"],
            "value": transition_value,
            "accession": transition_accession,
        },
        "maximum_reporting_lag_days": int(
            rule["maximum_reporting_lag_days"]
        ),
        "minimum_paired_quarters": minimum,
        "longest_timely_paired_chain": longest,
        "timely_paired_quarters": int(paired["fiscal_end"].nunique()),
        "output_rows": int(len(parsed)),
        "validation_rule": (
            "issuer-and-CIK exact match; three exact overlap quarters; "
            "first-filed explicit transition quarter; point-in-time concept "
            "cutover; paired revenue/net-income; reporting-lag bound"
        ),
    }
    return parsed[OUTPUT_COLUMNS].sort_values(
        ["ticker", "available_date", "fiscal_end", "metric"]
    ), evidence


def parse_research_historical_cik_override(
    symbol: str,
    cik: int,
    payload: dict,
    fetched_at,
) -> tuple[pd.DataFrame, dict]:
    """Parse a declared predecessor CIK using only contemporaneous filings."""
    normalized = str(symbol).strip().upper()
    rule = RESEARCH_HISTORICAL_CIK_OVERRIDES.get(normalized)
    if rule is None:
        raise ValueError(f"No research historical CIK override for {normalized}")
    if int(cik) != int(rule["cik"]):
        raise RuntimeError(
            f"{normalized} historical override expected CIK {rule['cik']}, got {cik}"
        )
    parsed = parse_companyfacts_quarterly(normalized, payload, fetched_at)
    parsed = parsed.loc[
        parsed["metric"].isin({"revenue", "net_income"})
    ].copy()
    parsed = _prefer_explicit_quarter_rows(parsed, normalized)
    fiscal_end = pd.to_datetime(parsed["fiscal_end"])
    parsed = parsed.loc[
        fiscal_end.between(
            pd.Timestamp(rule["minimum_fiscal_end"]),
            pd.Timestamp(rule["maximum_fiscal_end"]),
        )
    ].copy()
    parsed, reporting_lag_exceptions = _historical_cik_reporting_lag_filter(
        parsed, normalized=normalized, rule=rule
    )
    parsed["concept"] = (
        "research_historical_cik_override:"
        + parsed["concept"].astype(str)
    )
    paired = (
        parsed.pivot_table(
            index=["fiscal_end", "available_date"],
            columns="metric",
            values="value",
            aggfunc="first",
        )
        .dropna(subset=["revenue", "net_income"])
        .reset_index()
    )
    paired_quarters = int(paired["fiscal_end"].nunique())
    minimum = int(rule["minimum_paired_quarters"])
    if paired_quarters < minimum:
        raise RuntimeError(
            f"{normalized} historical CIK override has {paired_quarters} "
            f"timely paired quarters; requires {minimum}"
        )
    evidence = {
        "ticker": normalized,
        "cik": int(cik),
        "successor_cik": int(rule["successor_cik"]),
        "semantic_basis": rule["semantic_basis"],
        "minimum_fiscal_end": rule["minimum_fiscal_end"],
        "maximum_fiscal_end": rule["maximum_fiscal_end"],
        "maximum_reporting_lag_days": int(
            rule["maximum_reporting_lag_days"]
        ),
        "reporting_lag_exceptions": reporting_lag_exceptions,
        "minimum_paired_quarters": minimum,
        "timely_paired_quarters": paired_quarters,
        "output_rows": int(len(parsed)),
        "validation_rule": (
            "declared predecessor-and-successor CIK pair; standard SEC "
            "quarter parser; explicit-quarter preference; point-in-time "
            "filing date; fiscal cutover; standard reporting-lag bound plus "
            "exact predeclared filing exceptions"
        ),
    }
    return parsed[OUTPUT_COLUMNS].sort_values(
        ["ticker", "available_date", "fiscal_end", "metric"]
    ), evidence


def parse_research_concept_cutover_override(
    symbol: str,
    cik: int,
    payload: dict,
    fetched_at,
) -> tuple[pd.DataFrame, dict]:
    """Join two semantically proven issuer concepts at a fiscal cutover."""
    normalized = str(symbol).strip().upper()
    rule = RESEARCH_CONCEPT_CUTOVER_OVERRIDES.get(normalized)
    if rule is None:
        raise ValueError(f"No research concept cutover for {normalized}")
    if int(cik) != int(rule["cik"]):
        raise RuntimeError(
            f"{normalized} concept cutover expected CIK {rule['cik']}, got {cik}"
        )
    old_rows = _parse_declared_revenue_concept(
        normalized, payload, fetched_at,
        taxonomy=rule["taxonomy"], concept=rule["old_concept"],
        unit=rule["unit"],
    )
    new_rows = _parse_declared_revenue_concept(
        normalized, payload, fetched_at,
        taxonomy=rule["taxonomy"], concept=rule["new_concept"],
        unit=rule["unit"],
    )
    overlap = []
    for end_text in rule["overlap_fiscal_ends"]:
        end = pd.Timestamp(end_text)
        old_values = set(old_rows.loc[
            pd.to_datetime(old_rows["fiscal_end"]).eq(end), "value"
        ].astype(float))
        new_values = set(new_rows.loc[
            pd.to_datetime(new_rows["fiscal_end"]).eq(end), "value"
        ].astype(float))
        agreeing = sorted(old_values & new_values)
        if len(agreeing) != 1:
            raise RuntimeError(
                f"{normalized} concept cutover lacks one agreeing value for "
                f"{end_text}: old={sorted(old_values)}, new={sorted(new_values)}"
            )
        overlap.append({"fiscal_end": end_text, "agreeing_value": agreeing[0]})
    old_cutoff = pd.Timestamp(rule["old_concept_last_fiscal_end"])
    new_start = pd.Timestamp(rule["new_concept_first_fiscal_end"])
    revenue = pd.concat([
        old_rows.loc[pd.to_datetime(old_rows["fiscal_end"]).le(old_cutoff)],
        new_rows.loc[pd.to_datetime(new_rows["fiscal_end"]).ge(new_start)],
    ], ignore_index=True)
    standard = parse_companyfacts_quarterly(normalized, payload, fetched_at)
    net_income = _prefer_explicit_quarter_rows(
        standard.loc[standard["metric"].eq("net_income")].copy(), normalized
    )
    parsed = pd.concat([revenue, net_income], ignore_index=True)
    lag = (
        pd.to_datetime(parsed["available_date"])
        - pd.to_datetime(parsed["fiscal_end"])
    ).dt.days
    standard_lag = lag.between(0, int(rule["maximum_reporting_lag_days"]))
    historical_cutoff_text = rule.get("historical_comparative_max_fiscal_end")
    historical_lag_days = rule.get("maximum_historical_comparative_lag_days")
    historical_comparative = (
        pd.to_datetime(parsed["fiscal_end"])
        .le(pd.Timestamp(historical_cutoff_text))
        & lag.between(0, int(historical_lag_days))
    )
    parsed = parsed.loc[standard_lag | historical_comparative].copy()
    paired = (
        parsed.pivot_table(
            index=["fiscal_end", "available_date"], columns="metric",
            values="value", aggfunc="first",
        )
        .dropna(subset=["revenue", "net_income"])
        .reset_index()
    )
    paired_ends = sorted(pd.to_datetime(paired["fiscal_end"]).unique())
    longest = current = 1 if paired_ends else 0
    for left, right in zip(paired_ends, paired_ends[1:]):
        if 60 <= (right - left).days <= 135:
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    minimum = int(rule["minimum_paired_quarters"])
    if longest < minimum:
        raise RuntimeError(
            f"{normalized} concept cutover has longest paired chain {longest}; "
            f"requires {minimum}"
        )
    evidence = {
        "ticker": normalized,
        "cik": int(cik),
        "old_concept": rule["old_concept"],
        "new_concept": rule["new_concept"],
        "semantic_basis": rule["semantic_basis"],
        "overlap_checks": overlap,
        "old_concept_last_fiscal_end": rule["old_concept_last_fiscal_end"],
        "new_concept_first_fiscal_end": rule["new_concept_first_fiscal_end"],
        "maximum_reporting_lag_days": int(rule["maximum_reporting_lag_days"]),
        "historical_comparative_max_fiscal_end": historical_cutoff_text,
        "maximum_historical_comparative_lag_days": (
            int(historical_lag_days)
            if historical_lag_days is not None else None
        ),
        "minimum_paired_quarters": minimum,
        "longest_timely_paired_chain": longest,
        "timely_paired_quarters": int(paired["fiscal_end"].nunique()),
        "output_rows": int(len(parsed)),
        "validation_rule": (
            "issuer-and-CIK exact match; three agreeing overlap quarters; "
            "predeclared fiscal cutover; actual filing dates; standard lag "
            "bound plus a predeclared pre-IPO comparative lag bound; paired "
            "revenue and net income"
        ),
    }
    return parsed[OUTPUT_COLUMNS].sort_values(
        ["ticker", "available_date", "fiscal_end", "metric"]
    ), evidence


def research_companyfacts_override_rows(
    cache_dir: Path,
) -> tuple[pd.DataFrame, dict]:
    """Load every declared override from a manifest-bound isolated cache."""
    cache_dir = Path(cache_dir)
    frames = []
    issuers = []
    registries = (
        ("currency", RESEARCH_CURRENCY_OVERRIDES, parse_research_currency_override),
        ("concept", RESEARCH_CONCEPT_OVERRIDES, parse_research_concept_override),
        (
            "transition",
            RESEARCH_TRANSITION_OVERRIDES,
            parse_research_transition_override,
        ),
        (
            "historical_cik",
            RESEARCH_HISTORICAL_CIK_OVERRIDES,
            parse_research_historical_cik_override,
        ),
        (
            "concept_cutover",
            RESEARCH_CONCEPT_CUTOVER_OVERRIDES,
            parse_research_concept_cutover_override,
        ),
    )
    for override_type, registry, parser in registries:
        for symbol, rule in sorted(registry.items()):
            path = cache_dir / f"CIK{int(rule['cik']):010d}.json.gz"
            if not path.exists():
                raise RuntimeError(
                    f"Missing isolated Company Facts payload for {symbol}: {path}"
                )
            envelope = _read_cache_envelope(path)
            if symbol not in {
                str(item).strip().upper() for item in envelope["symbols"]
            }:
                raise RuntimeError(f"{path} is not bound to {symbol}")
            rows, evidence = parser(
                symbol,
                int(envelope["cik"]),
                envelope["payload"],
                envelope["fetched_at"],
            )
            evidence.update({
                "override_type": override_type,
                "payload_path": str(path),
                "payload_sha256": _sha256(path),
                "source_url": envelope["source_url"],
            })
            frames.append(rows)
            issuers.append(evidence)
    combined = (
        pd.concat(frames, ignore_index=True)
        if frames else pd.DataFrame(columns=OUTPUT_COLUMNS)
    )
    return combined, {
        "schema_version": 1,
        "research_only": True,
        "formal_parser_modified": False,
        "issuer_count": len(issuers),
        "row_count": int(len(combined)),
        "issuers": issuers,
    }


def research_currency_override_rows(
    cache_dir: Path,
) -> tuple[pd.DataFrame, dict]:
    """Backward-compatible name for the combined strict override dataset."""
    return research_companyfacts_override_rows(cache_dir)
