#!/usr/bin/env python3
"""Recover ZS's ten directly disclosed pre-IPO quarters from its S-1."""

from __future__ import annotations
import argparse, hashlib, json, re
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen
import pandas as pd

REGISTRY = Path("stocks_list_dir/nasdaq/zs_preipo_quarterly_reports.csv")
OUTPUT_DIR = Path("output/research_only/v14/zs_preipo_quarters")
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
EXPECTED_ENDS = [
    "2015-10-31", "2016-01-31", "2016-04-30", "2016-07-31",
    "2016-10-31", "2017-01-31", "2017-04-30", "2017-07-31",
    "2017-10-31", "2018-01-31",
]
EXPECTED_FIRST = {"revenue": 17_132_000.0, "net_income": -6_815_000.0}
EXPECTED_LAST = {"revenue": 44_976_000.0, "net_income": -6_515_000.0}

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _value(value: object) -> float:
    text = str(value).replace(",", "").replace("$", "").strip()
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match: raise ValueError(value)
    result = float(match.group())
    return -result if "(" in text else result

def extract_quarters(path: Path) -> list[dict]:
    candidates = []
    for table in pd.read_html(BytesIO(path.read_bytes())):
        if len(table) < 20 or len(table.columns) < 40: continue
        labels = table.iloc[:, 0].astype(str).str.strip().str.casefold()
        if labels.eq("revenue").sum() != 1 or labels.eq("net loss").sum() != 1:
            continue
        if not table.iloc[1].astype(str).str.contains("Three Months Ended").any():
            continue
        revenue = table.loc[labels.eq("revenue")].iloc[0]
        income = table.loc[labels.eq("net loss")].iloc[0]
        rows = []
        for column in table.columns:
            header = str(table.iloc[2][column]).replace("\xa0", " ")
            parsed = pd.to_datetime(header, errors="coerce")
            if pd.isna(parsed): continue
            try:
                rev, profit = _value(revenue[column]), _value(income[column])
            except ValueError:
                continue
            rows.append({
                "fiscal_end": parsed.date().isoformat(),
                "revenue": rev * 1_000.0,
                "net_income": profit * 1_000.0,
            })
        if (
            [row["fiscal_end"] for row in rows] == EXPECTED_ENDS
            and {k: rows[0][k] for k in EXPECTED_FIRST} == EXPECTED_FIRST
            and {k: rows[-1][k] for k in EXPECTED_LAST} == EXPECTED_LAST
        ):
            candidates.append(rows)
    if len(candidates) != 1: raise ValueError(f"expected one ZS monetary table: {len(candidates)}")
    rows = candidates[0]
    if [row["fiscal_end"] for row in rows] != EXPECTED_ENDS:
        raise ValueError("ZS S-1 quarter sequence changed")
    if {k: rows[0][k] for k in EXPECTED_FIRST} != EXPECTED_FIRST:
        raise ValueError("ZS first quarter changed")
    if {k: rows[-1][k] for k in EXPECTED_LAST} != EXPECTED_LAST:
        raise ValueError("ZS last quarter changed")
    return rows

def run(registry_path: Path = REGISTRY, output_dir: Path = OUTPUT_DIR) -> dict:
    registry = pd.read_csv(registry_path, dtype={"cik": str, "accession": str})
    if len(registry) != 1 or registry.iloc[0]["cik"] != "1713683":
        raise ValueError("ZS registry must bind CIK 1713683")
    row = registry.iloc[0]; source = Path(row["local_path"])
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        with urlopen(Request(row["source_url"], headers=HEADERS), timeout=120) as r:
            source.write_bytes(r.read())
    quarters = extract_quarters(source); facts = []
    for quarter in quarters:
        for metric in ("revenue", "net_income"):
            facts.append({
                "ticker":"ZS", "fiscal_end":quarter["fiscal_end"],
                "available_date":row["available_date"], "metric":metric,
                "value":quarter[metric], "taxonomy":"ZS_US_GAAP_S1",
                "concept":f"sec_s1_selected_quarterly_{metric}", "form":"S-1",
                "accession":row["accession"], "fetched_at":"2026-08-13",
            })
    frame = pd.DataFrame(facts).sort_values(["fiscal_end","metric"])
    output_dir.mkdir(parents=True, exist_ok=True); facts_path=output_dir/"strict_quarterly_facts.csv"
    frame.to_csv(facts_path,index=False)
    report={"schema_version":1,"research_only":True,"ticker":"ZS",
        "point_in_time_proven":True,"parameters_frozen":False,
        "policy_status":"RESEARCH_PRETRAINING_ONLY_UNFROZEN","release_status":"BLOCKED",
        "promotion_eligible":False,"accepted_quarter_count":10,"fact_count":20,
        "source":{**row.to_dict(),"sha256":_sha(source)},
        "outputs":{"strict_quarterly_facts":{"path":str(facts_path),"sha256":_sha(facts_path)}},
        "guardrail":"Only ten explicitly labelled S-1 single quarters are accepted; every row becomes available on the original S-1 filing date."}
    manifest=output_dir/"manifest.json";manifest.write_text(json.dumps(report,indent=2,sort_keys=True)+"\n");report["manifest"]=str(manifest);return report

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--registry-path",type=Path,default=REGISTRY);p.add_argument("--output-dir",type=Path,default=OUTPUT_DIR);a=p.parse_args();print(json.dumps(run(a.registry_path,a.output_dir),indent=2,sort_keys=True))
if __name__ == "__main__": main()
