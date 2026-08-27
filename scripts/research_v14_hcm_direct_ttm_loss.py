#!/usr/bin/env python3
"""Recover exact HCM TTM losses and audit the remaining age-150 PIT gaps.

HCM reported financial results on a six-month cadence in the affected period.
The exact 2020-H1 TTM loss was already 154 and 183 days old at the two signal
dates, while FY2020 was filed only on 2021-03-04.  Immutable SEC quarterly
master indexes plus every intervening primary 6-K are source-locked here so
those observations remain explicit unrecoverable gaps instead of invented Q3
facts.
"""

from __future__ import annotations

import argparse
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import gzip
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import warnings

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/hcm_direct_ttm_loss")
SEC_HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}
TICKER = "HCM"
CIK = 1_648_257
LATEST_VALID_FISCAL_END = "2020-06-30"
LATEST_VALID_AVAILABLE_DATE = "2020-07-30"
PIT_CUTOFF = "2021-01-29"
SOURCES = {
    "2019_fy": {
        "accession": "0001104659-20-028220",
        "filed": "2020-03-03",
        "document": "hcm-20191231x20fe6f6a8.htm",
        "local_path": "sources/financial/hcm-20191231x20fe6f6a8.htm",
        "expected_sha256": (
            "857b5ddb202e211747b390ca0bf511c633590875e9d45843b8b64dd497620b4d"
        ),
        "context": "Duration_1_1_2019_To_12_31_2019",
        "expected": -106_024_000.0,
    },
    "2020_h1": {
        "accession": "0001104659-20-088202",
        "filed": "2020-07-30",
        "document": "hcm-20200630xex991.htm",
        "local_path": "sources/financial/hcm-20200630xex991.htm",
        "expected_sha256": (
            "6c1aa5082a56f077ba949a421c510a357806c9815227f2cb3e88a9bdcab777e7"
        ),
        "context": "Duration_1_1_2020_To_6_30_2020",
        "expected": -49_694_000.0,
    },
    "2019_h1_comparative": {
        "accession": "0001104659-20-088202",
        "filed": "2020-07-30",
        "document": "hcm-20200630xex991.htm",
        "local_path": "sources/financial/hcm-20200630xex991.htm",
        "expected_sha256": (
            "6c1aa5082a56f077ba949a421c510a357806c9815227f2cb3e88a9bdcab777e7"
        ),
        "context": "Duration_1_1_2019_To_6_30_2019",
        "expected": -45_369_000.0,
    },
    "2020_fy": {
        "accession": "0001104659-21-031897",
        "filed": "2021-03-04",
        "document": "hcm-20201231x20f.htm",
        "local_path": "sources/financial/hcm-20201231x20f.htm",
        "expected_sha256": (
            "80de0a6be6d24a049653956dac1ab835940ad78bbf06c02e002afa473c0b0b3d"
        ),
        "context": "Duration_1_1_2020_To_12_31_2020",
        "expected": -125_730_000.0,
    },
    "2021_h1": {
        "accession": "0001104659-21-096648",
        "filed": "2021-07-28",
        "document": "hcm-20210630xex991.htm",
        "local_path": "sources/financial/hcm-20210630xex991.htm",
        "expected_sha256": (
            "f0dc27d13883d9ef56eb7763b3b74bc5dc45ebab2312aea474ff87d66d7a7bca"
        ),
        "context": "Duration_1_1_2021_To_6_30_2021",
        "expected": -102_397_000.0,
    },
}

INDEX_SOURCES = {
    "2020_q3": {
        "url": "https://www.sec.gov/Archives/edgar/full-index/2020/QTR3/master.gz",
        "local_path": "sources/index/2020_QTR3_master.gz",
        "expected_content_sha256": (
            "9abd019518757e7bdd145d056f41f627a973509c5a8e4d275bef91f4d7f60b0a"
        ),
    },
    "2020_q4": {
        "url": "https://www.sec.gov/Archives/edgar/full-index/2020/QTR4/master.gz",
        "local_path": "sources/index/2020_QTR4_master.gz",
        "expected_content_sha256": (
            "c764de83dd6c95984fbcebaa8993a7ca9d11816063c67151c28f4031eaccefb4"
        ),
    },
    "2021_q1": {
        "url": "https://www.sec.gov/Archives/edgar/full-index/2021/QTR1/master.gz",
        "local_path": "sources/index/2021_QTR1_master.gz",
        "expected_content_sha256": (
            "54c3877f050abca82f2b4089d04522e526f5d44ac3d22032bd6b41edc155014f"
        ),
    },
}


def _filing(
    filed: str,
    accession: str,
    document: str,
    expected_sha256: str,
    topic_fragment: str,
) -> dict:
    return {
        "filed": filed,
        "accession": accession,
        "document": document,
        "local_path": f"sources/filings/{accession}_{document}",
        "expected_sha256": expected_sha256,
        "topic_fragment": topic_fragment,
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1648257/"
            f"{accession.replace('-', '')}/{document}"
        ),
    }


NONFINANCIAL_6K_DOCUMENTS = (
    _filing("2020-07-30", "0001648257-20-000072", "hcm-20200730x6k.htm", "7a000ffa208467db6546527468337cae158fd010654fcf2e0da136f6dc5d5236", "announcement relating to block admission application"),
    _filing("2020-08-10", "0001648257-20-000074", "hcm-20200810x6k.htm", "4f4d06e0f124999f44a62530a4cd1dbee9d91b8b58e486a46e82035780843950", "plan to submit marketing authorization application"),
    _filing("2020-08-12", "0001648257-20-000076", "hcm-20200812x6k.htm", "76bb92123988b3ff8ca3c3dddcb8e1f204ab0465044e1db0dce4f5ffe621df70", "grant of share options under share option scheme"),
    _filing("2020-08-24", "0001648257-20-000078", "hcm-20200824x6k.htm", "9fd13bf7a2ee9206824d2dbbc8ee2aa433301e8ab3f59ec48f6f724c7e1c5402", "presentation of clinical data at the upcoming esmo virtual congress 2020"),
    _filing("2020-08-28", "0001648257-20-000080", "hcm-20200828x6k.htm", "bc654e8be13608a55ed3d2e7fa5975547893e6fb40348c1c795e5d385f49c401", "announcement relating to total voting rights"),
    _filing("2020-09-03", "0001648257-20-000085", "hcm-20200903x6k.htm", "d326c8e70fc7eebceb2a4a4445c103002da5c9121607761a928832563445916c", "initiation of a phase ii trial of hmpl-453"),
    _filing("2020-09-04", "0001648257-20-000087", "hcm-20200904x6k.htm", "eca25925132879b115e7ce18e1127d1a5634b3100c89579c2e9f128adbad4099", "initiation of fresco-2, a global phase iii trial"),
    _filing("2020-09-17", "0001648257-20-000089", "hcm-20200917x6k.htm", "2621b183d640ae963655d807ea31c3e694dec32be26e4c1c83017f30699345fd", "second new drug application acceptance in china"),
    _filing("2020-09-21", "0001648257-20-000091", "hcm-20200921x6k.htm", "fd59783fc2fadc6e803f308a8c1b59f30e6aade1c38d64a273c2839da643f67e", "presentation of surufatinib phase iii results"),
    _filing("2020-09-30", "0001648257-20-000093", "hcm-20200930x6k.htm", "a74aea9bf80c71d2ddf5a8a7d37071d3242260cb3dae0dd86915c3a3691bba10", "announcement relating to total voting rights"),
    _filing("2020-10-30", "0001648257-20-000099", "hcm-20201030x6k.htm", "0a51242452a070460d24dcd91e417da65ef859ef36d6aa2630846fcfd66b4a03", "announcement relating to total voting rights"),
    _filing("2020-10-30", "0001648257-20-000101", "hcm-20201030x6k.htm", "40c378178181378453713225c3a3420e68ff67a33c2fb207d06d7099d315a993", "attend upcoming industry and investor virtual conferences"),
    _filing("2020-11-05", "0001648257-20-000103", "hcm-20201105x6k.htm", "b4a253e82dd45c6cfcf589aaf4d48081b0819dfcae8882c5612feac0c857a555", "hmpl-689 clinical data to be presented"),
    _filing("2020-11-17", "0001648257-20-000106", "hcm-20201117x6k.htm", "c185ea8cf25359e9231e7aa8b9bcb4e456952c64e9922eec17bbfdbc7d532825", "us$100 million equity investment"),
    _filing("2020-11-27", "0001648257-20-000108", "hcm-20201127x6k.htm", "3f57bf49417e15c1da4eefdc4df70aa53d4423e85aaff7bd7a181d32892d06a6", "notification of dilution of voting rights"),
    _filing("2020-11-30", "0001648257-20-000110", "hcm-20201130x6k.htm", "7482566c5486a74cd71a848443e4e2376165edd6e871269db66fb536b62c897d", "announcement relating to total voting rights"),
    _filing("2020-12-15", "0001648257-20-000112", "hcm-20201215x6k.htm", "40877d5ce157fc84953cadd04bb5f1424b2b866c386d97b49d9624bfdec476de", "grant of share options under share option scheme"),
    _filing("2020-12-22", "0001648257-20-000114", "hcm-20201222x6k.htm", "c04e1338305c9dff3cefe08ea649f1ad8680209ad3ea98566865cd710df33546", "presentation at the 39th annual jp morgan healthcare conference"),
    _filing("2020-12-29", "0001648257-20-000118", "hcm-20201229x6k.htm", "4ea5716386c4eca474428bb5cc2d7f77cc6e78afe3bc0a5c5243ae029e6ad5f7", "initiation of rolling submission of nda to u.s. fda"),
    _filing("2020-12-31", "0001648257-20-000120", "hcm-20201231x6k.htm", "6cc44b553a524a477a996bdd0e8c2a87ee13e1003f13685e94498eeaf395db3e", "announcement relating to blocklisting six monthly return"),
    _filing("2021-01-11", "0001648257-21-000002", "hcm-20210111x6k.htm", "8766dfed68e945f8dba54207227a7a4d189331d0865a4441a7b1334791057606", "strategic partnership to develop and commercialize portfolio of drug candidates"),
    _filing("2021-01-14", "0001648257-21-000004", "hcm-20210114x6k.htm", "99d04cddb9e070a25be64b83592040e98466a5893af2c257618ba1dc7a47d283", "savolitinib clinical data to be presented at virtual wclc"),
)

AUDIT_OBSERVATIONS = (
    ("liq2000000-age150-growth", "2020-12-31", 150),
    ("liq2000000-age150-growth", "2021-01-29", 150),
)

FORBIDDEN_FINANCIAL_TOPIC_FRAGMENTS = (
    "financial results",
    "interim results",
    "annual results",
    "half year results",
    "nine month results",
    "third quarter results",
)
EXPECTED_TTM = {
    "2019-12-31": -106_024_000.0,
    "2020-06-30": -110_349_000.0,
    "2020-12-31": -125_730_000.0,
    "2021-06-30": -178_433_000.0,
}


def _url(spec: dict) -> str:
    accession = spec["accession"].replace("-", "")
    return (
        "https://www.sec.gov/Archives/edgar/data/1648257/"
        f"{accession}/{spec['document']}"
    )


def _fetch_url(url: str) -> bytes:
    for attempt in range(5):
        try:
            with urlopen(
                Request(url, headers=SEC_HEADERS), timeout=120
            ) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError, OSError):
            if attempt == 4:
                raise
            time.sleep(1.0 + attempt * 2.0)
    raise AssertionError("unreachable HCM source-download retry state")


def _fetch(spec: dict) -> bytes:
    return _fetch_url(_url(spec))


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _normalize_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split()).casefold()


def _local_source(output_dir: Path, spec: dict) -> tuple[bytes, Path, bool]:
    relative = Path(spec["local_path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe HCM source path: {relative}")
    path = output_dir / relative
    if path.exists():
        return path.read_bytes(), path, False
    url = spec["url"] if "url" in spec else _url(spec)
    raw = _fetch_url(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw, path, True


def validate_source_lock(
    sources: dict[str, dict] | None = None,
    indexes: dict[str, dict] | None = None,
    filings: tuple[dict, ...] | None = None,
) -> None:
    financial_sources = SOURCES if sources is None else sources
    index_sources = INDEX_SOURCES if indexes is None else indexes
    filing_sources = (
        NONFINANCIAL_6K_DOCUMENTS if filings is None else filings
    )
    if set(financial_sources) != set(SOURCES):
        raise ValueError("HCM financial source set changed")
    if set(index_sources) != set(INDEX_SOURCES):
        raise ValueError("HCM SEC index source set changed")
    for source_id, source in financial_sources.items():
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError(f"invalid HCM financial SHA-256: {source_id}")
        if source["accession"].replace("-", "") not in _url(source):
            raise ValueError(f"HCM financial accession URL changed: {source_id}")
    for source_id, source in index_sources.items():
        if not re.fullmatch(
            r"[0-9a-f]{64}", source["expected_content_sha256"]
        ):
            raise ValueError(f"invalid HCM SEC index SHA-256: {source_id}")
        if not source["url"].endswith("/master.gz"):
            raise ValueError(f"HCM SEC index URL changed: {source_id}")
    accessions = [source["accession"] for source in filing_sources]
    if len(accessions) != len(set(accessions)):
        raise ValueError("duplicate HCM nonfinancial 6-K accession")
    if len(filing_sources) != 22:
        raise ValueError("HCM nonfinancial 6-K source count changed")
    for source in filing_sources:
        if not LATEST_VALID_AVAILABLE_DATE <= source["filed"] <= PIT_CUTOFF:
            raise ValueError("HCM nonfinancial 6-K violates PIT window")
        if source["accession"].replace("-", "") not in source["url"]:
            raise ValueError("HCM 6-K URL does not lock accession")
        if not source["url"].endswith("/" + source["document"]):
            raise ValueError("HCM 6-K URL does not lock document")
        if not re.fullmatch(r"[0-9a-f]{64}", source["expected_sha256"]):
            raise ValueError("invalid HCM 6-K SHA-256")


def _prepare_financial_sources(
    output_dir: Path,
) -> tuple[dict[str, bytes], list[dict]]:
    raw_by_url: dict[str, bytes] = {}
    provenance = []
    for name, spec in SOURCES.items():
        url = _url(spec)
        if url in raw_by_url:
            continue
        raw, path, downloaded = _local_source(output_dir, {**spec, "url": url})
        actual_sha = _sha256(raw)
        if actual_sha != spec["expected_sha256"]:
            raise RuntimeError(
                f"HCM financial source SHA-256 mismatch for {name}: {actual_sha}"
            )
        raw_by_url[url] = raw
        provenance.append({
            "name": name,
            "accession": spec["accession"],
            "filed": spec["filed"],
            "document": spec["document"],
            "url": url,
            "local_path": str(path),
            "expected_sha256": spec["expected_sha256"],
            "actual_sha256": actual_sha,
            "bytes": len(raw),
            "downloaded": downloaded,
        })
    return raw_by_url, provenance


def _index_rows(content: bytes) -> list[dict]:
    rows = []
    for line in content.decode("latin-1").splitlines():
        fields = line.split("|")
        if len(fields) != 5 or fields[0] != str(CIK):
            continue
        cik, company, form, filed, filename = fields
        if (
            form == "6-K"
            and LATEST_VALID_AVAILABLE_DATE <= filed <= PIT_CUTOFF
        ):
            accession = Path(filename).stem
            rows.append({
                "cik": int(cik),
                "company": company,
                "form": form,
                "filed": filed,
                "accession": accession,
                "filename": filename,
            })
    return rows


def _verify_nonfinancial_6k(raw: bytes, source: dict) -> None:
    actual_sha = _sha256(raw)
    if actual_sha != source["expected_sha256"]:
        raise RuntimeError(
            "HCM nonfinancial 6-K SHA-256 mismatch for "
            f"{source['accession']}: {actual_sha}"
        )
    text = _normalize_text(BeautifulSoup(raw, "lxml").get_text(" ", strip=True))
    topic = _normalize_text(source["topic_fragment"])
    if topic not in text:
        raise RuntimeError(
            f"HCM 6-K topic changed for {source['accession']}: {topic}"
        )
    forbidden = [
        fragment
        for fragment in FORBIDDEN_FINANCIAL_TOPIC_FRAGMENTS
        if fragment in text
    ]
    if forbidden:
        raise RuntimeError(
            "HCM filing previously classified as nonfinancial now has "
            f"financial topic text: {source['accession']} {forbidden}"
        )


def _prepare_negative_evidence(
    output_dir: Path,
) -> tuple[list[dict], list[dict], pd.DataFrame]:
    index_provenance = []
    all_index_rows = []
    for source_id, source in INDEX_SOURCES.items():
        raw, path, downloaded = _local_source(output_dir, source)
        try:
            content = gzip.decompress(raw)
        except gzip.BadGzipFile as exc:
            raise RuntimeError(f"HCM SEC index is not gzip: {source_id}") from exc
        content_sha = _sha256(content)
        if content_sha != source["expected_content_sha256"]:
            raise RuntimeError(
                f"HCM SEC index content SHA-256 mismatch for {source_id}: "
                f"{content_sha}"
            )
        all_index_rows.extend(_index_rows(content))
        index_provenance.append({
            "source_id": source_id,
            **source,
            "local_path": str(path),
            "compressed_sha256": _sha256(raw),
            "actual_content_sha256": content_sha,
            "compressed_bytes": len(raw),
            "content_bytes": len(content),
            "downloaded": downloaded,
        })

    rows_by_accession = {row["accession"]: row for row in all_index_rows}
    expected_accessions = {
        SOURCES["2020_h1"]["accession"],
        *(source["accession"] for source in NONFINANCIAL_6K_DOCUMENTS),
    }
    if set(rows_by_accession) != expected_accessions:
        raise RuntimeError(
            "HCM SEC index filing inventory changed: "
            f"{sorted(set(rows_by_accession) ^ expected_accessions)}"
        )

    filing_provenance = []
    topic_by_accession = {}
    for source in NONFINANCIAL_6K_DOCUMENTS:
        raw, path, downloaded = _local_source(output_dir, source)
        actual_sha = _sha256(raw)
        _verify_nonfinancial_6k(raw, source)
        topic_by_accession[source["accession"]] = source["topic_fragment"]
        filing_provenance.append({
            **source,
            "local_path": str(path),
            "actual_sha256": actual_sha,
            "bytes": len(raw),
            "downloaded": downloaded,
            "classification": "nonfinancial_6k",
        })

    inventory = pd.DataFrame(sorted(all_index_rows, key=lambda row: (
        row["filed"], row["accession"]
    )))
    inventory["classification"] = inventory["accession"].map(
        lambda accession: (
            "latest_valid_h1_financial_report"
            if accession == SOURCES["2020_h1"]["accession"]
            else "nonfinancial_6k"
        )
    )
    inventory["topic_fragment"] = inventory["accession"].map(topic_by_accession)
    return index_provenance, filing_provenance, inventory


def resolve_audit_observations() -> pd.DataFrame:
    last_available = pd.Timestamp(LATEST_VALID_AVAILABLE_DATE)
    rows = []
    for scenario, signal_date, maximum_age_days in AUDIT_OBSERVATIONS:
        age = int((pd.Timestamp(signal_date) - last_available).days)
        rows.append({
            "scenario": scenario,
            "ticker": TICKER,
            "signal_date": signal_date,
            "maximum_age_days": maximum_age_days,
            "latest_valid_fiscal_end": LATEST_VALID_FISCAL_END,
            "latest_valid_available_date": LATEST_VALID_AVAILABLE_DATE,
            "financial_age_days": age,
            "resolved": False,
            "decision": "unrecoverable_six_month_reporting_cadence",
            "reason": (
                "2020-H1 exact TTM is older than the age-150 limit; every "
                "intervening SEC 6-K through the signal date is nonfinancial, "
                "and FY2020 was filed only on 2021-03-04"
            ),
        })
    return pd.DataFrame(rows)


def rejected_derivations() -> list[dict]:
    return [
        {
            "candidate": "reuse exact 2020-H1 TTM loss",
            "fiscal_end": LATEST_VALID_FISCAL_END,
            "available_date": LATEST_VALID_AVAILABLE_DATE,
            "net_income_ttm": EXPECTED_TTM[LATEST_VALID_FISCAL_END],
            "signal_ages_days": [154, 183],
            "maximum_age_days": 150,
            "rejected": True,
            "reason": "the exact snapshot is stale at both signal dates",
        },
        {
            "candidate": "use FY2020 20-F",
            "fiscal_end": "2020-12-31",
            "available_date": SOURCES["2020_fy"]["filed"],
            "net_income_ttm": EXPECTED_TTM["2020-12-31"],
            "rejected": True,
            "reason": "the 20-F was filed after both signal dates",
        },
        {
            "candidate": "invent 2020-Q3 or split 2020-H2",
            "rejected": True,
            "reason": (
                "no interim financial filing exists after 2020-H1 and before "
                "the signal dates; splitting a six-month period would not be "
                "an exact issuer-reported observation"
            ),
        },
    ]


def validate_unrecoverable_conclusion() -> None:
    observations = resolve_audit_observations()
    if observations["resolved"].any():
        raise RuntimeError("HCM gap unexpectedly marked recoverable")
    if observations["financial_age_days"].tolist() != [154, 183]:
        raise RuntimeError("HCM age-150 gap ages changed")
    if not observations["financial_age_days"].gt(
        observations["maximum_age_days"]
    ).all():
        raise RuntimeError("HCM stale-snapshot classification changed")
    if not all(item["rejected"] for item in rejected_derivations()):
        raise RuntimeError("HCM rejected derivation unexpectedly accepted")


def _number(tag) -> float:
    cleaned = re.sub(r"[^0-9.]", "", tag.get_text(" ", strip=True))
    if not cleaned:
        raise ValueError("inline XBRL fact has no numeric value")
    value = float(cleaned) * (10 ** int(tag.get("scale", "0")))
    return -abs(value) if tag.get("sign") == "-" else value


def _extract(raw: bytes, context_fragment: str) -> float:
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(raw, "lxml")
    candidates = {
        _number(tag)
        for tag in soup.find_all(
            lambda item: item.name
            and item.name.casefold().endswith("nonfraction")
            and str(item.get("name", "")).casefold()
            == "us-gaap:netincomeloss"
            and str(item.get("contextref", "")).casefold()
            .startswith(context_fragment.casefold())
            and "axis" not in str(item.get("contextref", "")).casefold()
        )
    }
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected one HCM net income fact for {context_fragment}, "
            f"found {sorted(candidates)}"
        )
    return candidates.pop()


def recover(output_dir: Path = OUTPUT_DIR) -> dict:
    validate_source_lock()
    validate_unrecoverable_conclusion()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_by_url, financial_provenance = _prepare_financial_sources(output_dir)
    index_provenance, filing_provenance, filing_inventory = (
        _prepare_negative_evidence(output_dir)
    )
    values = {}
    for name, spec in SOURCES.items():
        url = _url(spec)
        raw = raw_by_url[url]
        value = _extract(raw, spec["context"])
        if value != spec["expected"]:
            raise RuntimeError(f"HCM {name} source changed: {value}")
        values[name] = value
    ttm = {
        "2019-12-31": values["2019_fy"],
        "2020-06-30": (
            values["2019_fy"] - values["2019_h1_comparative"]
            + values["2020_h1"]
        ),
        "2020-12-31": values["2020_fy"],
        "2021-06-30": (
            values["2020_fy"] - values["2020_h1"] + values["2021_h1"]
        ),
    }
    if ttm != EXPECTED_TTM:
        raise RuntimeError(f"HCM exact TTM values changed: {ttm}")
    available_dates = {
        "2019-12-31": SOURCES["2019_fy"]["filed"],
        "2020-06-30": SOURCES["2020_h1"]["filed"],
        "2020-12-31": SOURCES["2020_fy"]["filed"],
        "2021-06-30": SOURCES["2021_h1"]["filed"],
    }
    accessions = {
        "2019-12-31": SOURCES["2019_fy"]["accession"],
        "2020-06-30": "+".join([
            SOURCES["2019_fy"]["accession"],
            SOURCES["2020_h1"]["accession"],
        ]),
        "2020-12-31": SOURCES["2020_fy"]["accession"],
        "2021-06-30": "+".join([
            SOURCES["2020_fy"]["accession"],
            SOURCES["2021_h1"]["accession"],
        ]),
    }
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    facts = pd.DataFrame([{
        "ticker": TICKER,
        "fiscal_end": fiscal_end,
        "available_date": available_dates[fiscal_end],
        "metric": "net_income_ttm",
        "value": value,
        "taxonomy": "us-gaap",
        "concept": "sec_exact_ttm:NetIncomeLoss",
        "form": "20-F" if fiscal_end.endswith("12-31") else "20-F_PLUS_6-K_H1",
        "accession": accessions[fiscal_end],
        "fetched_at": fetched_at,
    } for fiscal_end, value in ttm.items()], columns=OUTPUT_COLUMNS)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    inventory_path = output_dir / "filing_inventory.csv"
    observations_path = output_dir / "unrecoverable_observations.csv"
    rejected_path = output_dir / "rejected_derivations.json"
    facts.to_csv(facts_path, index=False)
    filing_inventory.to_csv(inventory_path, index=False)
    observations = resolve_audit_observations()
    observations.to_csv(observations_path, index=False)
    rejected = rejected_derivations()
    rejected_path.write_text(
        json.dumps(rejected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    output_paths = {
        "strict_quarterly_facts": facts_path,
        "filing_inventory": inventory_path,
        "unrecoverable_observations": observations_path,
        "rejected_derivations": rejected_path,
    }
    report = {
        "schema_version": 2,
        "research_only": True,
        "point_in_time_proven": True,
        "negative_evidence_source_locked": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "ticker": TICKER,
        "cik": CIK,
        "pit_cutoff": PIT_CUTOFF,
        "accepted_exact_ttm_count": len(facts),
        "resolved_audit_observation_count": int(observations["resolved"].sum()),
        "unrecoverable_audit_observation_count": len(observations),
        "intervening_6k_count": len(filing_inventory) - 1,
        "nonfinancial_6k_source_count": len(filing_provenance),
        "rejected_derivation_count": len(rejected),
        "financial_sources": financial_provenance,
        "sec_quarterly_index_sources": index_provenance,
        "nonfinancial_6k_sources": filing_provenance,
        "outputs": {
            name: {
                "path": str(path),
                "sha256": _sha256(path.read_bytes()),
            }
            for name, path in output_paths.items()
        },
        "conclusion": (
            "The four exact annual/H1 TTM loss facts remain accepted. The two "
            "age-150 observations on 2020-12-31 and 2021-01-29 are source-"
            "exhausted and unrecoverable: the last exact snapshot was 154/183 "
            "days old, all 22 other 6-Ks in the PIT window were nonfinancial, "
            "and FY2020 was filed only on 2021-03-04."
        ),
        "guardrail": (
            "Uses exact annual TTM losses and exact FY-minus-H1-plus-next-H1 "
            "rolling losses. It does not split six-month values into quarters, "
            "backdate FY2020, or create a positive-growth eligibility record."
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
    report = recover(args.output_dir)
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
