#!/usr/bin/env python3
"""Recover BLDP 2019Q1-2021Q4 from contemporaneous SEC filings."""

from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen

from lxml import etree
import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/bldp_quarterly_reports")
CIK = 1_453_015
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
SOURCES = {
    "2019_q1": {
        "accession": "0001453015-19-000018", "filed": "2019-05-02",
        "document": "bldp033119-ex991fs.htm", "kind": "interim",
        "sha256": "c65c44fb9cc1113801640541860f2e9b5bba811cbb782c9ffa57e8badd4b9a3f",
    },
    "2019_q2": {
        "accession": "0001453015-19-000025", "filed": "2019-08-01",
        "document": "bldp063019-ex991fs.htm", "kind": "interim",
        "sha256": "72b5b052941f1aeb6603b62c59edfa9d15fd043ff88265b32b9f730754fc6add",
    },
    "2019_q3": {
        "accession": "0001453015-19-000030", "filed": "2019-10-31",
        "document": "bldp093019-ex991fs.htm", "kind": "interim",
        "sha256": "5599764eb833eb2029ce953e81bf536888a37d001489e422f1643dffe80e91f8",
    },
    "2019_fy": {
        "accession": "0001453015-20-000010", "filed": "2020-03-05",
        "document": "bldp-20191231_d2_htm.xml", "kind": "annual",
        "sha256": "b828650d8b52ce918a4dcbb9118acd8b356686a9dedc8129fa2e58a6256fe00c",
    },
    "2020_q1": {
        "accession": "0001453015-20-000014", "filed": "2020-05-06",
        "document": "bldp033120-ex991fs.htm", "kind": "interim",
        "sha256": "9fecd29ec3b8852ac5e56325f335a8f7c0bc1d5310605302c145698bd6f740cb",
    },
    "2020_q2": {
        "accession": "0001453015-20-000020", "filed": "2020-08-06",
        "document": "bldp063020-ex991fs.htm", "kind": "interim",
        "sha256": "9c0e612105fdee98b44aa43289371872091926f6217bc1cba586b30fd896c6b4",
    },
    "2020_q3": {
        "accession": "0001453015-20-000030", "filed": "2020-11-06",
        "document": "bldp093020-ex991fs.htm", "kind": "interim",
        "sha256": "aabe2bae672bd1fdf862420c223436bb5e4460ff0967ef7dd65f996b24ded4da",
    },
    "2020_fy": {
        "accession": "0001453015-21-000012", "filed": "2021-03-11",
        "document": "bldp-20201231_d2_htm.xml", "kind": "annual",
        "sha256": "875ecdabd15e76e6ad57684d6d0b49a28f83cfb443a8fbb45efee0f21c39bd83",
    },
    "2021_q1": {
        "accession": "0001453015-21-000020", "filed": "2021-05-04",
        "document": "bldp033121-ex991fs.htm", "kind": "interim",
        "sha256": "61f32619da6d28313e265f4c851008f91eecfba76e9b84f1c7c5620095cac2a8",
    },
    "2021_q2": {
        "accession": "0001453015-21-000026", "filed": "2021-08-06",
        "document": "bldp063021-ex991fs.htm", "kind": "interim",
        "sha256": "7da973348519b045b8cd78993f75df4d34ac483bfb6de379975f041b6cde22e4",
    },
    "2021_q3": {
        "accession": "0001453015-21-000030", "filed": "2021-11-09",
        "document": "bldp093021-ex991fs.htm", "kind": "interim",
        "sha256": "d7c39c5293913397887127abcacb18a757f26ee3367751a54ba53b1c5486d782",
    },
    "2021_fy": {
        "accession": "0001453015-22-000003", "filed": "2022-03-14",
        "document": "bldp-20211231_htm.xml", "kind": "annual",
        "sha256": "4554044e3a1a8e23d9b13b8df62010ddbe4ce8c20d3b140e2b82f29cac766760",
    },
}
EXPECTED = {
    "2019-03-31": (16_008_000.0, -12_024_000.0),
    "2019-06-30": (23_651_000.0, -6_971_000.0),
    "2019-09-30": (24_785_000.0, -9_782_000.0),
    "2019-12-31": (41_883_000.0, -10_273_000.0),
    "2020-03-31": (24_026_000.0, -13_503_000.0),
    "2020-06-30": (25_818_000.0, -11_432_000.0),
    "2020-09-30": (25_624_000.0, -11_756_000.0),
    "2020-12-31": (28_588_000.0, -14_686_000.0),
    "2021-03-31": (17_619_000.0, -17_638_000.0),
    "2021-06-30": (24_961_000.0, -21_913_000.0),
    "2021-09-30": (25_220_000.0, -30_849_000.0),
    "2021-12-31": (36_705_000.0, -43_832_000.0),
}


def _url(spec: dict) -> str:
    return (
        f"https://www.sec.gov/Archives/edgar/data/{CIK}/"
        f"{spec['accession'].replace('-', '')}/{spec['document']}"
    )


def _fetch(spec: dict) -> bytes:
    request = Request(_url(spec), headers=SEC_HEADERS)
    error = None
    for attempt in range(4):
        try:
            with urlopen(request, timeout=120) as response:
                return response.read()
        except Exception as exc:  # pragma: no cover - network retry
            error = exc
            if attempt < 3:
                time.sleep(attempt + 1)
    raise RuntimeError(f"failed to fetch BLDP source {_url(spec)}") from error


def _number(value: object) -> float:
    text = str(value).strip()
    negative = text.startswith("(") or text.startswith("-")
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        raise RuntimeError(f"BLDP filing value is not numeric: {value!r}")
    number = float(cleaned) * 1000.0
    return -number if negative else number


def parse_interim(raw: bytes) -> dict[str, list[float]]:
    """Read explicit current-year quarter and cumulative statement values."""
    for table in pd.read_html(BytesIO(raw)):
        first = table.iloc[:, 0].astype(str).str.strip()
        revenue_mask = first.eq("Product and service revenues")
        loss_mask = first.isin({"Net loss for period", "Net loss for the period"})
        if not revenue_mask.any() or not loss_mask.any():
            continue
        output = {}
        for metric, mask in (("revenue", revenue_mask), ("net_income", loss_mask)):
            row = table.loc[mask].iloc[0]
            # Each reported value is preceded by a literal currency cell.
            # Some Workiva generations duplicate the label/note columns, so
            # fixed physical offsets are not stable across these filings.
            values = [
                _number(row.iloc[column + 1])
                for column in range(len(row) - 1)
                if str(row.iloc[column]).strip() == "$"
            ]
            if len(values) not in {2, 4}:
                raise RuntimeError(
                    f"unexpected BLDP {metric} statement values: {values}"
                )
            output[metric] = values
        return output
    raise RuntimeError("BLDP filing lacks the interim operations statement")


def parse_annual(raw: bytes, year: int) -> dict[str, float]:
    root = etree.parse(BytesIO(raw)).getroot()
    contexts = {}
    for context in root.xpath('//*[local-name()="context"]'):
        starts = context.xpath('.//*[local-name()="startDate"]/text()')
        ends = context.xpath('.//*[local-name()="endDate"]/text()')
        dimensions = context.xpath('.//*[local-name()="explicitMember"]')
        if starts and ends and not dimensions:
            contexts[context.get("id")] = (starts[0], ends[0])
    concepts = {
        "RevenueFromContractsWithCustomers": "revenue",
        "ProfitLoss": "net_income",
    }
    period = (f"{year}-01-01", f"{year}-12-31")
    facts: dict[str, set[float]] = {metric: set() for metric in concepts.values()}
    for element in root.iter():
        if not isinstance(element.tag, str) or element.text is None:
            continue
        metric = concepts.get(etree.QName(element).localname)
        if metric is not None and contexts.get(element.get("contextRef")) == period:
            text = element.text.strip()
            if re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", text):
                facts[metric].add(float(text))
    if any(len(values) != 1 for values in facts.values()):
        raise RuntimeError(f"BLDP annual filing lacks unique facts: {facts}")
    return {metric: values.pop() for metric, values in facts.items()}


def derive_quarters(parsed: dict[str, dict]) -> dict[str, dict[str, float]]:
    quarters: dict[str, dict[str, float]] = {}
    for year in (2019, 2020, 2021):
        for quarter, end in ((1, "03-31"), (2, "06-30"), (3, "09-30")):
            facts = parsed[f"{year}_q{quarter}"]
            quarters[f"{year}-{end}"] = {
                metric: values[0] for metric, values in facts.items()
            }
        q3 = parsed[f"{year}_q3"]
        annual = parsed[f"{year}_fy"]
        quarters[f"{year}-12-31"] = {
            metric: annual[metric] - q3[metric][2]
            for metric in ("revenue", "net_income")
        }
    return dict(sorted(quarters.items()))


def _source_for_quarter(fiscal_end: str) -> str:
    year = fiscal_end[:4]
    month = fiscal_end[5:7]
    return f"{year}_fy" if month == "12" else f"{year}_q{int(month) // 3}"


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    parsed = {}
    source_report = []
    for name, spec in SOURCES.items():
        raw = _fetch(spec)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec["sha256"]:
            raise RuntimeError(f"BLDP source changed for {name}: {digest}")
        parsed[name] = (
            parse_interim(raw) if spec["kind"] == "interim"
            else parse_annual(raw, int(name[:4]))
        )
        source_report.append({
            "name": name, "accession": spec["accession"],
            "filed": spec["filed"], "url": _url(spec),
            "sha256": digest, "bytes": len(raw),
        })
    quarters = derive_quarters(parsed)
    observed = {
        fiscal_end: (values["revenue"], values["net_income"])
        for fiscal_end, values in quarters.items()
    }
    if observed != EXPECTED:
        raise RuntimeError(f"BLDP recovered quarters changed: {observed}")
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows = []
    for fiscal_end, metrics in quarters.items():
        source_name = _source_for_quarter(fiscal_end)
        spec = SOURCES[source_name]
        for metric, value in metrics.items():
            rows.append({
                "ticker": "BLDP", "fiscal_end": fiscal_end,
                "available_date": spec["filed"], "metric": metric,
                "value": value, "taxonomy": "ifrs-full",
                "concept": f"sec_strict_quarter:{metric}",
                "form": "40-F" if source_name.endswith("fy") else "6-K",
                "accession": spec["accession"], "fetched_at": fetched_at,
            })
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["available_date", "fiscal_end", "metric"]
    ).reset_index(drop=True)
    if len(facts) != 24 or facts["fiscal_end"].nunique() != 12:
        raise RuntimeError("BLDP recovery must contain 12 paired quarters")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "formal_financials_modified": False, "ticker": "BLDP", "cik": CIK,
        "accepted_quarter_count": 12, "accepted_fact_count": 24,
        "sources": source_report,
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path),
            "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
        }},
        "guardrail": (
            "Q1-Q3 use explicit single-quarter facts from contemporaneous "
            "6-K financial statements. Q4 is the contemporaneous 40-F annual "
            "fact minus the nine-month fact in that year's Q3 filing. No "
            "cumulative period is divided evenly, no later comparative is "
            "backdated, and no formal financial file is changed."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = recover(args.output_dir)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir, supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
