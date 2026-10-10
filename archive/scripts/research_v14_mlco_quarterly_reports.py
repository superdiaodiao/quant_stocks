#!/usr/bin/env python3
"""Recover MLCO 2017Q1-2020Q4 from contemporaneous SEC 6-K exhibits."""

from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/mlco_quarterly_reports")
CIK = 1_381_640
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
SOURCES = {
    "2017-03-31": ("2017-05-04", "0001193125-17-157353", "d326709dex991.htm", "c2e35834c338ab56f4fcbd83f2f4e9096c97e952f0e821d536347174599544ff"),
    "2017-06-30": ("2017-07-27", "0001193125-17-237446", "d430542dex991.htm", "12eacf5e747358a360aa62a3a64053e1d23f86c73dc8bb5f95794a5035e40b2b"),
    "2017-09-30": ("2017-11-02", "0001193125-17-330300", "d476925dex991.htm", "ba5819a8784296739ea5d92e3901f0fed52215f541b03a4f9b96eb9faa6560e4"),
    "2017-12-31": ("2018-02-08", "0001193125-18-035472", "d531649dex991.htm", "9eda6a9643137b300e968d5ce1a6434e154ef1f1ecd5417c32faa38241bb4cd0"),
    "2018-03-31": ("2018-05-03", "0001193125-18-149604", "d577389dex991.htm", "4bc3d9b0b0270383d7e5508899007f6de628e011beda1b7bd7043f763de1992e"),
    "2018-06-30": ("2018-07-24", "0001193125-18-224213", "d556435dex991.htm", "85f4afae53780e5793f9f0339a954288a909719ba027f377365a2b11b63d23c9"),
    "2018-09-30": ("2018-11-08", "0001193125-18-321902", "d617535dex991.htm", "7551e098b3138e5ac193d7533905105f9dcae049bf66b4acfb89f8c1ea0a61fb"),
    "2018-12-31": ("2019-02-19", "0001193125-19-043530", "d709900dex991.htm", "191d589b7c89cbfac069e095d1c98cd89a602ba6d5d3cd1a8b7251df92fbc9fa"),
    "2019-03-31": ("2019-05-07", "0001193125-19-138890", "d743526dex991.htm", "409c4e053fbed416306823ce5bd7cf9bb0f2575ff1fcfaa1ab00aa7342a4028a"),
    "2019-06-30": ("2019-07-24", "0001193125-19-200579", "d729483dex991.htm", "b06b323c0119921edfe1a7aa54325baa0f98309053ea3bca5721b9403371fb83"),
    "2019-09-30": ("2019-10-30", "0001193125-19-278391", "d824575dex991.htm", "c0da71a0fb89bf2ea22ff23d858300e015ffb51effae0f973a898ec85e22c2db"),
    "2019-12-31": ("2020-02-20", "0001193125-20-042683", "d885014dex991.htm", "7cc4be5242962c575bb30f5f64f481229620fc61bb4f7e131f2fe8243926243a"),
    "2020-03-31": ("2020-05-14", "0001193125-20-142247", "d926483dex991.htm", "f6cb20efe9c3a6dafabdd7241da6f63d400b7c58eb14060ce986ffafd54ecd33"),
    "2020-06-30": ("2020-08-20", "0001193125-20-225115", "d887996dex991.htm", "a872d15c0f60600162ee937041f900f6b48eda1fdb957d0c90e9aa5ddec66657"),
    "2020-09-30": ("2020-11-05", "0001193125-20-286267", "d40377dex991.htm", "19783de9bf5c91bbbd7f85ede63cf7979927b793748f3b3f98fff3782d6b7709"),
    "2020-12-31": ("2021-02-25", "0001193125-21-055780", "d18510dex991.htm", "4a55c67c737cfd5e3ca36163c6739a783edcbdf14b521c882fbd89d14f911e8d"),
}
EXPECTED = {
    "2017-03-31": (1_277_220_000.0, 113_446_000.0),
    "2017-06-30": (1_298_220_000.0, 36_477_000.0),
    "2017-09-30": (1_376_827_000.0, 115_907_000.0),
    "2017-12-31": (1_332_556_000.0, 81_172_000.0),
    "2018-03-31": (1_313_148_000.0, 156_633_000.0),
    "2018-06-30": (1_228_630_000.0, 57_273_000.0),
    "2018-09-30": (1_220_277_000.0, 9_602_000.0),
    "2018-12-31": (1_396_454_000.0, 128_007_000.0),
    "2019-03-31": (1_362_046_000.0, 117_355_000.0),
    "2019-06-30": (1_442_653_000.0, 100_312_000.0),
    "2019-09-30": (1_438_656_000.0, 83_190_000.0),
    "2019-12-31": (1_450_641_000.0, 68_139_000.0),
    "2020-03-31": (811_175_000.0, -364_048_000.0),
    "2020-06-30": (175_850_000.0, -368_129_000.0),
    "2020-09-30": (212_896_000.0, -331_581_000.0),
    "2020-12-31": (528_002_000.0, -199_734_000.0),
}


def _spec(values: tuple[str, str, str, str]) -> dict:
    filed, accession, document, sha256 = values
    return {"filed": filed, "accession": accession, "document": document, "sha256": sha256}


def _url(spec: dict) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{CIK}/{spec['accession'].replace('-', '')}/{spec['document']}"


def _fetch(spec: dict) -> bytes:
    request = Request(_url(spec), headers=SEC_HEADERS)
    error = None
    for attempt in range(4):
        try:
            with urlopen(request, timeout=120) as response:
                return response.read()
        except Exception as exc:  # pragma: no cover
            error = exc
            if attempt < 3:
                time.sleep(attempt + 1)
    raise RuntimeError(f"failed to fetch MLCO source {_url(spec)}") from error


def _number(value: object) -> float:
    text = str(value).strip()
    negative = text.startswith("(") or text.startswith("-")
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        raise RuntimeError(f"MLCO filing value is not numeric: {value!r}")
    return (-1.0 if negative else 1.0) * float(cleaned) * 1000.0


def parse_quarter(raw: bytes) -> dict[str, float]:
    tables = pd.read_html(BytesIO(raw))
    revenue = None
    net_income = None
    for table in tables:
        first = table.iloc[:, 0].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
        revenue_mask = first.str.fullmatch(r"(?i)(Net revenues|Total operating revenues)")
        if revenue is None and revenue_mask.any():
            row = table.loc[revenue_mask].iloc[0]
            values = [value for value in row.iloc[1:] if pd.notna(value)]
            if values:
                revenue = _number(values[0])
        income_mask = (
            first.str.contains(r"(?i)^Net .*attributable to Melco (?:Resorts|Crown)", regex=True)
            & ~first.str.contains(r"(?i)adjusted|per share|per ADS|calculation", regex=True)
        )
        if net_income is None and income_mask.any():
            row = table.loc[income_mask].iloc[0]
            currency = [i for i in range(len(row) - 1) if str(row.iloc[i]).strip() == "$"]
            if currency:
                net_income = _number(row.iloc[currency[0] + 1])
    if revenue is None or net_income is None:
        raise RuntimeError("MLCO filing lacks unique issuer quarterly facts")
    return {"revenue": revenue, "net_income": net_income}


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows, sources, observed = [], [], {}
    for fiscal_end, raw_spec in SOURCES.items():
        spec = _spec(raw_spec)
        raw = _fetch(spec)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec["sha256"]:
            raise RuntimeError(f"MLCO source changed for {fiscal_end}: {digest}")
        facts = parse_quarter(raw)
        observed[fiscal_end] = (facts["revenue"], facts["net_income"])
        sources.append({"fiscal_end": fiscal_end, "filed": spec["filed"], "accession": spec["accession"], "url": _url(spec), "sha256": digest, "bytes": len(raw)})
        for metric, value in facts.items():
            rows.append({"ticker": "MLCO", "fiscal_end": fiscal_end, "available_date": spec["filed"], "metric": metric, "value": value, "taxonomy": "us-gaap", "concept": f"sec_strict_quarter:{metric}", "form": "6-K", "accession": spec["accession"], "fetched_at": fetched_at})
    if observed != EXPECTED:
        raise RuntimeError(f"MLCO recovered quarters changed: {observed}")
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(["available_date", "fiscal_end", "metric"]).reset_index(drop=True)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {"schema_version": 1, "research_only": True, "point_in_time_proven": True, "parameters_frozen": False, "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN", "release_status": "BLOCKED", "promotion_eligible": False, "formal_financials_modified": False, "ticker": "MLCO", "cik": CIK, "accepted_quarter_count": 16, "accepted_fact_count": 32, "sources": sources, "outputs": {"strict_quarterly_facts": {"path": str(facts_path), "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest()}}, "guardrail": "Every observation is an explicit current-quarter USD GAAP issuer fact in a contemporaneous SEC 6-K exhibit. Studio City subsidiary exhibits, adjusted measures, cumulative facts, per-share rows and later comparatives are excluded."}
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
        report["candidate"] = integrate_candidate(base_dir=args.base_dir, supplement_dir=args.output_dir, output_dir=args.candidate_output_dir)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
