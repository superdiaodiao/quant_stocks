"""Audit transformed formal Company Facts rows against raw operands.

The formal-source audit proves that direct rows still exist in the immutable
SEC snapshot.  This companion audit checks the deterministic transformations
used for the remaining rows: YTD differences, annual residual (Q4), bank
revenue sums, and the reviewed foreign-quarter parser.  It is research-only:
it never reparses or writes annual/quarterly formal outputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from scripts.companyfacts_release_selection_manifest import _row_sha256, _row_values

from scripts.companyfacts_cache_snapshot import verify_companyfacts_cache_snapshot
from src.io.fundamentals_update import (
    OUTPUT_COLUMNS,
    _coalesce_equivalent_quarter_ends,
    _read_companyfacts_cache,
    cached_companyfacts_cik_chains_for_symbols,
    companyfacts_full_rebuild_recipe_sha256,
)
from src.research.foreign_quarterly_diagnostics import (
    foreign_quarters_to_point_in_time,
)


REPORT_FORMAT_VERSION = 2
TRANSFORMED_PREFIXES = ("derived_", "foreign_")
RAW_UNITS = {
    "USD", "EUR", "GBP", "CAD", "DKK", "SEK", "CHF", "NOK", "AUD", "JPY"
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _date_days(later: str, earlier: str) -> int:
    try:
        return (date.fromisoformat(later) - date.fromisoformat(earlier)).days
    except (TypeError, ValueError):
        return -99999


def _canonical_date(value) -> str:
    text = str(value or "")
    return text[:10] if len(text) >= 10 else text


def _read_formal(path: Path, dataset: str) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = set(OUTPUT_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"formal {dataset} file is missing columns: {sorted(missing)}")
    frame = frame[OUTPUT_COLUMNS].copy()
    frame["ticker"] = frame["ticker"].str.strip().str.upper()
    frame["fiscal_end_s"] = frame["fiscal_end"].map(_canonical_date)
    frame["available_s"] = frame["available_date"].map(_canonical_date)
    frame["value_num"] = pd.to_numeric(frame["value"], errors="coerce")
    frame["dataset"] = dataset
    frame["_row_ordinal"] = range(len(frame))
    return frame


def _raw_indexes(
    ticker: str,
    ciks: tuple[int, ...],
    snapshot: Path,
    needed_concepts: set[str],
):
    """Load only concepts needed by one ticker into exact and YTD indexes."""
    exact: dict[tuple[str, str, str, str, str, str, str], list[dict]] = defaultdict(list)
    any_taxonomy: dict[tuple[str, str, str, str, str, str], list[dict]] = defaultdict(list)
    ytd: dict[tuple[str, str, str, str, str], list[dict]] = defaultdict(list)
    foreign_payload = None
    for cik in ciks:
        payload, fetched_at = _read_companyfacts_cache(cik, snapshot)
        if foreign_payload is None:
            foreign_payload = (payload, fetched_at, cik)
        for taxonomy, concepts in payload.get("facts", {}).items():
            for concept in needed_concepts:
                fact = concepts.get(concept)
                if not fact:
                    continue
                for unit, rows in fact.get("units", {}).items():
                    if unit not in RAW_UNITS:
                        continue
                    for raw in rows:
                        end = _canonical_date(raw.get("end"))
                        filed = _canonical_date(raw.get("filed"))
                        start = _canonical_date(raw.get("start"))
                        try:
                            value = float(raw.get("val"))
                        except (TypeError, ValueError):
                            continue
                        if len(end) != 10 or len(filed) != 10:
                            continue
                        record = {
                            "taxonomy": str(taxonomy),
                            "concept": str(concept),
                            "unit": str(unit),
                            "start": start,
                            "end": end,
                            "filed": filed,
                            "value": value,
                            "form": str(raw.get("form") or ""),
                            "accession": str(raw.get("accn") or ""),
                            "cik": int(cik),
                        }
                        exact[
                            (
                                record["taxonomy"], record["concept"],
                                end, filed, record["form"], record["accession"],
                            )
                        ].append(record)
                        any_taxonomy[
                            (
                                record["concept"], end, filed,
                                record["form"], record["accession"],
                            )
                        ].append(record)
                        ytd[(record["taxonomy"], record["concept"], start, unit)].append(record)
    for rows in ytd.values():
        rows.sort(key=lambda item: (item["end"], item["filed"]))
    return exact, any_taxonomy, ytd, foreign_payload


def _direct_raw(
    row: dict,
    concept: str,
    exact: dict,
    any_taxonomy: dict,
) -> dict | None:
    key = (
        str(row["taxonomy"]), concept, row["fiscal_end_s"],
        row["available_s"], str(row["form"]), str(row["accession"]),
    )
    candidates = exact.get(key) or any_taxonomy.get(
        (concept, row["fiscal_end_s"], row["available_s"],
         str(row["form"]), str(row["accession"])),
        [],
    )
    return candidates[0] if candidates else None


def _explicit_quarter_operand(
    candidates: list[dict],
    dataset: str = "quarterly",
) -> dict | None:
    """Mirror the parser's explicit quarter or annual duration guard."""
    eligible = []
    for candidate in candidates:
        start = candidate.get("start")
        end = candidate.get("end")
        if len(str(start)) != 10 or len(str(end)) != 10:
            continue
        duration = _date_days(str(end), str(start))
        if (
            dataset == "quarterly" and 60 <= duration <= 135
        ) or (
            dataset == "annual" and 300 <= duration <= 400
        ):
            eligible.append(candidate)
    return eligible[0] if eligible else None


def _derived_ytd_value(
    row: dict,
    concept: str,
    exact: dict,
    any_taxonomy: dict,
    ytd: dict,
) -> tuple[float, list[dict]] | None:
    current = _direct_raw(row, concept, exact, any_taxonomy)
    if current is None or len(current["start"]) != 10:
        return None
    candidates = ytd.get(
        (current["taxonomy"], concept, current["start"], current["unit"]), []
    )
    prior = [
        item for item in candidates
        if item["end"] < current["end"]
        and item["filed"] <= current["filed"]
        and 60 <= _date_days(item["end"], item["start"]) < _date_days(current["end"], current["start"])
        and 60 <= _date_days(current["end"], item["end"]) <= 135
    ]
    if not prior:
        return None
    previous = max(prior, key=lambda item: (item["end"], item["filed"]))
    return current["value"] - previous["value"], [current, previous]


def _q4_value(
    row: dict,
    annual_concept: str,
    quarter_groups: dict[tuple[str, str], list[dict]],
    exact: dict,
    any_taxonomy: dict,
) -> tuple[float, list[dict]] | None:
    annual = _direct_raw(row, annual_concept, exact, any_taxonomy)
    if annual is None:
        return None
    candidates = [
        item for item in quarter_groups.get((row["ticker"], row["metric"]), [])
        if item["fiscal_end_s"] < row["fiscal_end_s"]
        and 0 <= _date_days(row["fiscal_end_s"], item["fiscal_end_s"]) <= 330
        and item["available_s"] <= row["available_s"]
        and not str(item["concept"]).startswith((
            "derived_q4", "foreign_derived_q4", "foreign_"
        ))
    ]
    # The parser sorts by filing date, keeps the last candidate per fiscal end,
    # then selects the three most recent ends.
    by_end: dict[str, dict] = {}
    for item in sorted(candidates, key=lambda value: value["available_s"]):
        by_end[item["fiscal_end_s"]] = item
    quarter_frame = pd.DataFrame(by_end.values()).assign(
        fiscal_end=lambda value: pd.to_datetime(value["fiscal_end_s"]),
        available_date=lambda value: pd.to_datetime(value["available_s"]),
        value=lambda value: pd.to_numeric(value["value_num"]),
    )
    quarter_frame = _coalesce_equivalent_quarter_ends(quarter_frame)
    quarters = (
        quarter_frame.sort_values("fiscal_end_s")
        .tail(3)
        .drop(columns=["fiscal_end", "available_date", "value"])
        .to_dict("records")
    )
    ends = [item["fiscal_end_s"] for item in quarters] + [row["fiscal_end_s"]]
    gaps = [_date_days(later, earlier) for earlier, later in zip(ends, ends[1:])]
    if len(quarters) != 3 or not all(60 <= gap <= 135 for gap in gaps):
        return None
    return annual["value"] - sum(float(item["value_num"]) for item in quarters), [annual, *quarters]


def _record_result(
    row: dict,
    prefix: str,
    expected: float | None,
    reason: str = "",
    operands: list[dict] | None = None,
) -> dict:
    actual = float(row["value_num"]) if pd.notna(row["value_num"]) else None
    matched = (
        expected is not None and actual is not None
        and bool(np.isclose(expected, actual, rtol=1e-9, atol=1e-6))
    )
    if expected is None:
        matched = False
    if expected is not None and not matched and not reason:
        reason = "formula_value_mismatch"
    row_sha256 = (
        _row_sha256(_row_values(row))
        if set(OUTPUT_COLUMNS).issubset(row)
        else None
    )
    return {
        "dataset": row["dataset"],
        "ordinal": (
            int(row["_row_ordinal"])
            if row.get("_row_ordinal") is not None
            else None
        ),
        "row_sha256": row_sha256,
        "ticker": row["ticker"],
        "concept": row["concept"],
        "prefix": prefix,
        "matched": bool(matched),
        "reason": reason,
        "expected_value": expected,
        "actual_value": actual,
        "operand_count": len(operands or []),
    }


def audit_companyfacts_formal_formulas(
    snapshot_dir: str | Path,
    *,
    annual_output: str | Path,
    quarterly_output: str | Path,
    sample_limit: int = 20,
) -> dict:
    """Audit all transformed formal rows without modifying any input."""
    if sample_limit < 0:
        raise ValueError("sample_limit must be non-negative")
    snapshot = Path(snapshot_dir)
    verified = verify_companyfacts_cache_snapshot(snapshot)
    annual = _read_formal(Path(annual_output), "annual")
    quarterly = _read_formal(Path(quarterly_output), "quarterly")
    formal = pd.concat([annual, quarterly], ignore_index=True)
    transformed = formal.loc[
        formal["concept"].str.startswith(TRANSFORMED_PREFIXES)
    ].copy()
    tickers = sorted(transformed["ticker"].unique())
    chains = cached_companyfacts_cik_chains_for_symbols(tickers, snapshot)
    quarter_groups = {
        key: group.to_dict("records")
        for key, group in quarterly.groupby(["ticker", "metric"], sort=False)
    }
    needed: dict[str, set[str]] = defaultdict(set)
    foreign_tickers: set[str] = set()
    for row in transformed.to_dict("records"):
        prefix, rest = row["concept"].split(":", 1)
        if prefix in {"derived_ytd", "derived_q4"}:
            needed[row["ticker"]].add(rest)
        elif prefix == "derived_bank_revenue":
            for component in rest.split("+", 1):
                needed[row["ticker"]].add(
                    component.split(":", 1)[1]
                    if component.startswith("derived_ytd:") else component
                )
        elif prefix.startswith("foreign"):
            foreign_tickers.add(row["ticker"])

    results: list[dict] = []
    for ticker in tickers:
        exact, any_taxonomy, ytd, foreign_payload = _raw_indexes(
            ticker, chains[ticker], snapshot, needed[ticker]
        )
        rows = transformed.loc[transformed["ticker"].eq(ticker)].to_dict("records")
        for row in rows:
            prefix, rest = row["concept"].split(":", 1)
            expected = None
            operands = None
            reason = ""
            if prefix == "derived_ytd":
                derived = _derived_ytd_value(row, rest, exact, any_taxonomy, ytd)
                if derived is not None:
                    expected, operands = derived
                else:
                    reason = "ytd_operands_unresolved"
            elif prefix == "derived_q4":
                derived = _q4_value(row, rest, quarter_groups, exact, any_taxonomy)
                if derived is not None:
                    expected, operands = derived
                else:
                    reason = "q4_operands_unresolved"
            elif prefix == "derived_bank_revenue":
                values: list[float] = []
                operands = []
                for component in rest.split("+", 1):
                    if component.startswith("derived_ytd:"):
                        derived = _derived_ytd_value(
                            row, component.split(":", 1)[1],
                            exact, any_taxonomy, ytd,
                        )
                        if derived is None:
                            values = []
                            break
                        value, component_operands = derived
                        values.append(value)
                        operands.extend(component_operands)
                    else:
                        candidates = any_taxonomy.get(
                            (
                                component, row["fiscal_end_s"],
                                row["available_s"], str(row["form"]),
                                str(row["accession"]),
                            ),
                            [],
                        )
                        operand = _explicit_quarter_operand(
                            candidates, row["dataset"]
                        )
                        if operand is None:
                            values = []
                            break
                        values.append(operand["value"])
                        operands.append(operand)
                if len(values) == 2:
                    expected = sum(values)
                else:
                    reason = "bank_operands_unresolved"
            elif prefix.startswith("foreign"):
                # Foreign rows are restricted to the reviewed registry and
                # are independently reconstructed by its diagnostics parser.
                continue
            results.append(_record_result(row, prefix, expected, reason, operands))

        if ticker in foreign_tickers:
            if foreign_payload is None:
                foreign_rows = transformed.loc[
                    transformed["ticker"].eq(ticker)
                    & transformed["concept"].str.startswith("foreign_")
                ]
                for row in foreign_rows.to_dict("records"):
                    results.append(_record_result(
                        row, row["concept"].split(":", 1)[0], None,
                        "foreign_payload_unresolved",
                    ))
            else:
                payload, fetched_at, _cik = foreign_payload
                parsed = foreign_quarters_to_point_in_time(
                    ticker, payload, fetched_at
                )
                if not parsed.empty:
                    parsed["fiscal_end_s"] = parsed["fiscal_end"].map(_canonical_date)
                    parsed["available_s"] = parsed["available_date"].map(_canonical_date)
                    parsed["value_num"] = pd.to_numeric(parsed["value"], errors="coerce")
                foreign_rows = transformed.loc[
                    transformed["ticker"].eq(ticker)
                    & transformed["concept"].str.startswith("foreign_")
                ]
                for row in foreign_rows.to_dict("records"):
                    candidates = parsed.loc[
                        (parsed["fiscal_end_s"] == row["fiscal_end_s"])
                        & (parsed["available_s"] == row["available_s"])
                        & (parsed["metric"] == row["metric"])
                        & (parsed["taxonomy"] == row["taxonomy"])
                        & (parsed["concept"] == row["concept"])
                        & (parsed["form"] == row["form"])
                        & (parsed["accession"] == row["accession"])
                    ]
                    expected = (
                        float(candidates.iloc[0]["value_num"])
                        if not candidates.empty else None
                    )
                    results.append(_record_result(
                        row, row["concept"].split(":", 1)[0], expected,
                        "foreign_parser_mismatch" if expected is None else "",
                        [{}] if expected is not None else None,
                    ))

    result_frame = pd.DataFrame(results)
    datasets = {}
    for dataset in ("annual", "quarterly"):
        subset = result_frame.loc[result_frame["dataset"].eq(dataset)]
        by_prefix = {}
        for prefix, group in subset.groupby("prefix", sort=True):
            by_prefix[prefix] = {
                "row_count": int(len(group)),
                "matched_count": int(group["matched"].sum()),
                "unresolved_or_mismatched_count": int((~group["matched"]).sum()),
            }
        failures = subset.loc[~subset["matched"]]
        datasets[dataset] = {
            "transformed_row_count": int(len(subset)),
            "formula_match_count": int(subset["matched"].sum()),
            "formula_failure_count": int((~subset["matched"]).sum()),
            "by_prefix": by_prefix,
            "failure_by_reason": {
                str(reason): int(count)
                for reason, count in failures["reason"].value_counts().items()
            },
            "failure_sample": failures.head(sample_limit).to_dict("records"),
        }
    total = len(result_frame)
    matched = int(result_frame["matched"].sum()) if total else 0
    row_proofs = [
        {
            key: result.get(key)
            for key in (
                "dataset", "ordinal", "row_sha256", "matched", "reason",
                "operand_count",
            )
        }
        for result in results
    ]
    return {
        "format_version": REPORT_FORMAT_VERSION,
        "research_only": True,
        "rebuild_recipe_sha256": companyfacts_full_rebuild_recipe_sha256(),
        "snapshot": {
            "snapshot_dir": str(snapshot),
            "snapshot_id": verified["snapshot_id"],
            "cache_manifest_sha256": verified["cache_manifest_sha256"],
            "verified": True,
        },
        "formal_outputs": {
            "annual": {"path": str(annual_output), "sha256": _sha256_file(Path(annual_output))},
            "quarterly": {"path": str(quarterly_output), "sha256": _sha256_file(Path(quarterly_output))},
        },
        "transformed_row_count": int(total),
        "formula_match_count": matched,
        "formula_failure_count": int(total - matched),
        "all_transformed_rows_verified": bool(total == matched),
        "row_proof_identity_missing_count": sum(
            1
            for proof in row_proofs
            if proof["ordinal"] is None or not proof["row_sha256"]
        ),
        "row_proofs": row_proofs,
        "datasets": datasets,
        "warning": (
            "This is a research-only operand/formula audit. It does not claim "
            "that a formal output may be replaced without an explicit, "
            "scope-bound rebuild and release decision."
        ),
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--annual-output", required=True)
    parser.add_argument("--quarterly-output", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sample-limit", type=int, default=20)
    args = parser.parse_args()
    report = audit_companyfacts_formal_formulas(
        args.snapshot,
        annual_output=args.annual_output,
        quarterly_output=args.quarterly_output,
        sample_limit=args.sample_limit,
    )
    _write_json(Path(args.output), report)
    print(json.dumps({
        "output": args.output,
        "snapshot_id": report["snapshot"]["snapshot_id"],
        "transformed_row_count": report["transformed_row_count"],
        "formula_match_count": report["formula_match_count"],
        "formula_failure_count": report["formula_failure_count"],
        "research_only": True,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
