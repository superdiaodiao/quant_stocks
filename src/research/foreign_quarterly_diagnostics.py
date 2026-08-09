"""Read-only audit of foreign-issuer quarterly facts in the SEC raw cache.

This module deliberately does not feed the production fundamentals parser.
Foreign issuers often put single-quarter, year-to-date, and annual values in
6-K/20-F filings with inconsistent ``fp`` labels.  The audit reconstructs only
arithmetically defensible quarters and reports whether revenue and net income
form a same-currency continuous history.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.io.fundamentals_update import (
    METRIC_CONCEPTS,
    SEC_COMPANYFACTS_CACHE_DIR,
    _companyfacts_cache_files,
    _read_companyfacts_cache_envelope,
    verify_companyfacts_cache_manifest,
)


FOREIGN_FORMS = {"6-K", "20-F", "20-F/A", "40-F", "40-F/A"}
MAX_AVAILABILITY_LAG_DAYS = 150
FOREIGN_METRIC_CONCEPTS = {
    **METRIC_CONCEPTS,
    "revenue": (
        *METRIC_CONCEPTS["revenue"],
        "Revenue",
        "RevenueFromContractsWithCustomers",
    ),
}
TARGET_ACTION = "NEEDS_FOREIGN_QUARTERLY_SOURCE"
DEFAULT_PRIORITY_FILE = Path(
    "output/can_slim_technical_candidate_financial_priorities.csv"
)
DEFAULT_DETAIL_OUTPUT = Path(
    "output/can_slim_foreign_quarterly_diagnostics.csv"
)
DEFAULT_SUMMARY_OUTPUT = Path(
    "output/can_slim_foreign_quarterly_diagnostics_summary.json"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raw_metric_facts(
    payload: dict,
    metric: str,
    concepts: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    rows = []
    facts = payload.get("facts") or {}
    for taxonomy_priority, taxonomy in enumerate(("us-gaap", "ifrs-full")):
        namespace = facts.get(taxonomy) or {}
        for concept_priority, concept in enumerate(
            concepts or FOREIGN_METRIC_CONCEPTS[metric]
        ):
            units = (namespace.get(concept) or {}).get("units") or {}
            for unit, unit_rows in units.items():
                for raw in unit_rows:
                    if raw.get("form") not in FOREIGN_FORMS:
                        continue
                    start = pd.to_datetime(raw.get("start"), errors="coerce")
                    end = pd.to_datetime(raw.get("end"), errors="coerce")
                    filed = pd.to_datetime(raw.get("filed"), errors="coerce")
                    value = pd.to_numeric(raw.get("val"), errors="coerce")
                    if (
                        pd.isna(start)
                        or pd.isna(end)
                        or pd.isna(filed)
                        or pd.isna(value)
                    ):
                        continue
                    duration = int((end - start).days)
                    if not 60 <= duration <= 400:
                        continue
                    rows.append({
                        "metric": metric,
                        "taxonomy": taxonomy,
                        "concept": concept,
                        "unit": str(unit),
                        "start": start,
                        "end": end,
                        "filed": filed,
                        "value": float(value),
                        "duration": duration,
                        "form": str(raw.get("form")),
                        "accession": str(raw.get("accn") or ""),
                        "priority": taxonomy_priority * 100 + concept_priority,
                    })
    if not rows:
        return pd.DataFrame(columns=[
            "metric", "taxonomy", "concept", "unit", "start", "end",
            "filed", "value", "duration", "form", "accession", "priority",
        ])
    return pd.DataFrame(rows)


def _deduplicate_quarters(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    return (
        frame.sort_values(
            ["priority", "filed", "source_rank", "accession"],
            kind="stable",
        )
        .drop_duplicates(["metric", "unit", "end"], keep="first")
        .sort_values(["metric", "unit", "end"])
        .reset_index(drop=True)
    )


def reconstruct_foreign_quarters(
    payload: dict,
    metric: str,
    concepts: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """Build explicit, YTD-difference, and annual-residual quarter candidates."""
    raw = _raw_metric_facts(payload, metric, concepts)
    if raw.empty:
        return raw.assign(source=pd.Series(dtype=str))

    explicit = raw.loc[raw["duration"].between(60, 135)].copy()
    explicit["source"] = "explicit"
    explicit["source_rank"] = 0

    derived_ytd = []
    group_columns = ["taxonomy", "concept", "unit", "start"]
    for _, group in raw.groupby(group_columns, sort=False):
        ordered = group.sort_values(["end", "filed"])
        for current in ordered.itertuples(index=False):
            if not 136 <= current.duration <= 320:
                continue
            prior = ordered.loc[
                (ordered["end"] < current.end)
                & (ordered["filed"] <= current.filed)
                & ordered["duration"].between(60, current.duration - 1)
            ]
            if prior.empty:
                continue
            previous = prior.sort_values(["end", "filed"]).iloc[-1]
            gap = int((current.end - previous["end"]).days)
            if not 60 <= gap <= 135:
                continue
            derived_ytd.append({
                **current._asdict(),
                "start": previous["end"],
                "value": current.value - float(previous["value"]),
                "duration": gap,
                "source": "derived_ytd",
                "source_rank": 1,
            })
    ytd = pd.DataFrame(derived_ytd)

    base = _deduplicate_quarters(
        pd.concat([explicit, ytd], ignore_index=True)
    )
    derived_q4 = []
    annual = raw.loc[raw["duration"].between(330, 400)]
    for annual_row in annual.itertuples(index=False):
        candidates = base.loc[
            (base["taxonomy"] == annual_row.taxonomy)
            & (base["concept"] == annual_row.concept)
            & (base["unit"] == annual_row.unit)
            & (base["end"] < annual_row.end)
            & (base["end"] >= annual_row.end - pd.Timedelta(days=330))
            & (base["filed"] <= annual_row.filed)
        ]
        quarters = (
            candidates.sort_values(["end", "filed"])
            .drop_duplicates("end", keep="first")
            .nlargest(3, "end")
            .sort_values("end")
        )
        if len(quarters) != 3:
            continue
        ends = quarters["end"].tolist() + [annual_row.end]
        if not pd.Series(ends).diff().dt.days.dropna().between(60, 135).all():
            continue
        derived_q4.append({
            **annual_row._asdict(),
            "start": quarters.iloc[-1]["end"],
            "value": annual_row.value - float(quarters["value"].sum()),
            "duration": int((annual_row.end - quarters.iloc[-1]["end"]).days),
            "source": "derived_q4",
            "source_rank": 2,
        })
    q4 = pd.DataFrame(derived_q4)
    return _deduplicate_quarters(
        pd.concat([base, q4], ignore_index=True)
    )


def _longest_continuous_chain(ends: list[pd.Timestamp]) -> int:
    if not ends:
        return 0
    ordered = sorted(set(ends))
    longest = current = 1
    for prior, value in zip(ordered, ordered[1:]):
        if 60 <= (value - prior).days <= 135:
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    return longest


def _concept_continuity(
    payload: dict,
    metric: str,
    selected: pd.DataFrame,
) -> dict:
    """Require adjacent concept generations to agree on an overlap quarter."""
    if selected.empty:
        return {
            "concept_switch_count": 0,
            "unverified_concept_transition_count": 0,
            "maximum_overlap_relative_difference": None,
        }
    ordered = selected.sort_values("end")
    concepts = ordered["concept"].astype(str).tolist()
    transitions = {
        (left, right)
        for left, right in zip(concepts, concepts[1:])
        if left != right
    }
    unverified = 0
    differences = []
    series = {}
    for concept in set(concepts):
        frame = reconstruct_foreign_quarters(
            payload, metric, concepts=(concept,)
        )
        series[concept] = frame.loc[
            frame["unit"].isin(set(selected["unit"]))
        ][["end", "value"]].drop_duplicates("end")
    for left, right in transitions:
        overlap = series[left].merge(
            series[right], on="end", suffixes=("_left", "_right")
        )
        if overlap.empty:
            unverified += 1
            continue
        denominator = overlap[
            ["value_left", "value_right"]
        ].abs().max(axis=1).clip(lower=1.0)
        relative = (
            overlap["value_left"] - overlap["value_right"]
        ).abs() / denominator
        differences.extend(relative.tolist())
        if not relative.le(1e-6).any():
            unverified += 1
    return {
        "concept_switch_count": len(transitions),
        "unverified_concept_transition_count": unverified,
        "maximum_overlap_relative_difference": (
            max(differences) if differences else None
        ),
    }


def diagnose_foreign_payload(symbol: str, cik: int, payload: dict) -> dict:
    revenue = reconstruct_foreign_quarters(payload, "revenue")
    net_income = reconstruct_foreign_quarters(payload, "net_income")
    revenue_units = set(revenue["unit"]) if len(revenue) else set()
    income_units = set(net_income["unit"]) if len(net_income) else set()
    common_units = sorted(revenue_units & income_units)

    unit_results = []
    for unit in common_units:
        revenue_unit = revenue.loc[revenue["unit"].eq(unit)]
        income_unit = net_income.loc[net_income["unit"].eq(unit)]
        paired_frame = revenue_unit[["end", "filed"]].merge(
            income_unit[["end", "filed"]],
            on="end",
            suffixes=("_revenue", "_net_income"),
            validate="one_to_one",
        )
        paired_frame["available_date"] = paired_frame[
            ["filed_revenue", "filed_net_income"]
        ].max(axis=1)
        paired_frame["availability_lag_days"] = (
            paired_frame["available_date"] - paired_frame["end"]
        ).dt.days
        paired = sorted(paired_frame["end"])
        timely = sorted(
            paired_frame.loc[
                paired_frame["availability_lag_days"].between(
                    0, MAX_AVAILABILITY_LAG_DAYS
                ),
                "end",
            ]
        )
        unit_results.append({
            "unit": unit,
            "paired_quarter_count": len(paired),
            "longest_continuous_paired_quarters": (
                _longest_continuous_chain(paired)
            ),
            "timely_paired_quarter_count": len(timely),
            "longest_continuous_timely_paired_quarters": (
                _longest_continuous_chain(timely)
            ),
            "first_paired_quarter": (
                min(paired).strftime("%Y-%m-%d") if paired else ""
            ),
            "last_paired_quarter": (
                max(paired).strftime("%Y-%m-%d") if paired else ""
            ),
        })
    best = max(
        unit_results,
        key=lambda row: (
            row["longest_continuous_timely_paired_quarters"],
            row["timely_paired_quarter_count"],
            row["longest_continuous_paired_quarters"],
            row["unit"],
        ),
        default=None,
    )
    selected_currency = best["unit"] if best else ""
    revenue_selected = (
        revenue.loc[revenue["unit"].eq(selected_currency)]
        if selected_currency else revenue.iloc[0:0]
    )
    income_selected = (
        net_income.loc[net_income["unit"].eq(selected_currency)]
        if selected_currency else net_income.iloc[0:0]
    )
    revenue_continuity = _concept_continuity(
        payload, "revenue", revenue_selected
    )
    income_continuity = _concept_continuity(
        payload, "net_income", income_selected
    )
    unverified_transitions = (
        revenue_continuity["unverified_concept_transition_count"]
        + income_continuity["unverified_concept_transition_count"]
    )

    if revenue.empty:
        reason = "NO_REVENUE_QUARTER_CANDIDATES"
    elif net_income.empty:
        reason = "NO_NET_INCOME_QUARTER_CANDIDATES"
    elif not common_units:
        reason = "NO_COMMON_CURRENCY"
    elif best["longest_continuous_timely_paired_quarters"] < 8:
        reason = "LESS_THAN_8_TIMELY_CONTINUOUS_PAIRED_QUARTERS"
    elif unverified_transitions:
        reason = "UNVERIFIED_CONCEPT_TRANSITION"
    else:
        reason = "PASS_DIAGNOSTIC_ONLY"

    def source_count(frame: pd.DataFrame, source: str) -> int:
        return int(frame["source"].eq(source).sum()) if len(frame) else 0

    return {
        "ticker": symbol.upper(),
        "cik": int(cik),
        "diagnostic_status": reason,
        "eligible_for_parser_research": reason == "PASS_DIAGNOSTIC_ONLY",
        "selected_currency": selected_currency,
        "paired_quarter_count": best["paired_quarter_count"] if best else 0,
        "longest_continuous_paired_quarters": (
            best["longest_continuous_paired_quarters"] if best else 0
        ),
        "timely_paired_quarter_count": (
            best["timely_paired_quarter_count"] if best else 0
        ),
        "longest_continuous_timely_paired_quarters": (
            best["longest_continuous_timely_paired_quarters"] if best else 0
        ),
        "maximum_availability_lag_days": MAX_AVAILABILITY_LAG_DAYS,
        "first_paired_quarter": best["first_paired_quarter"] if best else "",
        "last_paired_quarter": best["last_paired_quarter"] if best else "",
        "revenue_quarter_candidates": int(len(revenue)),
        "net_income_quarter_candidates": int(len(net_income)),
        "revenue_explicit_count": source_count(revenue, "explicit"),
        "revenue_derived_ytd_count": source_count(revenue, "derived_ytd"),
        "revenue_derived_q4_count": source_count(revenue, "derived_q4"),
        "net_income_explicit_count": source_count(net_income, "explicit"),
        "net_income_derived_ytd_count": source_count(
            net_income, "derived_ytd"
        ),
        "net_income_derived_q4_count": source_count(
            net_income, "derived_q4"
        ),
        "candidate_revenue_currencies": "|".join(sorted(revenue_units)),
        "candidate_net_income_currencies": "|".join(sorted(income_units)),
        "revenue_concept_switch_count": revenue_continuity[
            "concept_switch_count"
        ],
        "net_income_concept_switch_count": income_continuity[
            "concept_switch_count"
        ],
        "unverified_concept_transition_count": unverified_transitions,
        "maximum_revenue_overlap_relative_difference": revenue_continuity[
            "maximum_overlap_relative_difference"
        ],
        "maximum_net_income_overlap_relative_difference": income_continuity[
            "maximum_overlap_relative_difference"
        ],
    }


def foreign_quarters_to_point_in_time(
    symbol: str,
    payload: dict,
    fetched_at,
    selected_currency: str | None = None,
) -> pd.DataFrame:
    """Convert diagnostic candidates to the normal quarterly row schema."""
    rows = []
    for metric in ("revenue", "net_income"):
        candidates = reconstruct_foreign_quarters(payload, metric)
        if selected_currency:
            candidates = candidates.loc[
                candidates["unit"].eq(selected_currency)
            ]
        for row in candidates.itertuples(index=False):
            rows.append({
                "ticker": symbol.upper(),
                "fiscal_end": row.end,
                "available_date": row.filed,
                "metric": metric,
                "value": row.value,
                "taxonomy": row.taxonomy,
                "concept": f"foreign_{row.source}:{row.concept}",
                "form": row.form,
                "accession": row.accession,
                "fetched_at": pd.Timestamp(fetched_at).tz_localize(
                    None
                ).normalize(),
            })
    return pd.DataFrame(rows)


def run_foreign_quarterly_diagnostics(
    priority_file: Path = DEFAULT_PRIORITY_FILE,
    cache_dir: Path = SEC_COMPANYFACTS_CACHE_DIR,
) -> tuple[pd.DataFrame, dict]:
    """Audit prioritized foreign symbols without modifying cache or inputs."""
    priority_file = Path(priority_file)
    cache_dir = Path(cache_dir)
    verification = verify_companyfacts_cache_manifest(cache_dir)
    priorities = pd.read_csv(priority_file)
    targets = set(
        priorities.loc[
            priorities["recommended_data_action"].eq(TARGET_ACTION),
            "ticker",
        ].astype(str).str.upper()
    )

    payloads = {}
    for path in _companyfacts_cache_files(cache_dir):
        envelope = _read_companyfacts_cache_envelope(path)
        for raw_symbol in envelope.get("symbols", []):
            symbol = str(raw_symbol).strip().upper()
            if symbol in targets:
                payloads[symbol] = (
                    int(envelope["cik"]),
                    envelope.get("payload") or {},
                )

    rows = []
    for symbol in sorted(targets):
        cached = payloads.get(symbol)
        if cached is None:
            rows.append({
                "ticker": symbol,
                "cik": pd.NA,
                "diagnostic_status": "TARGET_NOT_CACHED",
                "eligible_for_parser_research": False,
            })
            continue
        rows.append(diagnose_foreign_payload(symbol, *cached))
    detail = pd.DataFrame(rows).sort_values(
        [
            "eligible_for_parser_research",
            "longest_continuous_timely_paired_quarters",
            "longest_continuous_paired_quarters",
            "paired_quarter_count",
            "ticker",
        ],
        ascending=[False, False, False, False, True],
        na_position="last",
    )
    statuses = detail["diagnostic_status"].value_counts().sort_index()
    summary = {
        "purpose": "read_only_foreign_quarterly_cache_diagnostic",
        "formal_fundamentals_modified": False,
        "target_action": TARGET_ACTION,
        "target_symbol_count": len(targets),
        "cached_target_count": len(payloads),
        "eligible_for_parser_research_count": int(
            detail["eligible_for_parser_research"].sum()
        ),
        "status_counts": {
            str(key): int(value) for key, value in statuses.items()
        },
        "priority_file": str(priority_file),
        "priority_file_sha256": _sha256(priority_file),
        "cache_manifest": verification["manifest"],
        "cache_manifest_sha256": _sha256(Path(verification["manifest"])),
    }
    return detail, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--priority-file", type=Path, default=DEFAULT_PRIORITY_FILE)
    parser.add_argument("--cache-dir", type=Path, default=SEC_COMPANYFACTS_CACHE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_DETAIL_OUTPUT)
    parser.add_argument(
        "--summary-output", type=Path, default=DEFAULT_SUMMARY_OUTPUT
    )
    args = parser.parse_args()
    detail, summary = run_foreign_quarterly_diagnostics(
        args.priority_file, args.cache_dir
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    detail.to_csv(args.output, index=False)
    args.summary_output.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
