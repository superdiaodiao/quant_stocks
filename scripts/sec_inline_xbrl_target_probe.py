"""Probe filing-local Inline XBRL for exact target facts with SHA evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import warnings
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from lxml import etree


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number(text: str, scale: str | None, sign: str | None) -> float | None:
    cleaned = re.sub(r"[^0-9.()-]", "", text)
    if not cleaned or cleaned in {"-", "()"}:
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        value = float(cleaned) * (10 ** int(scale or 0))
    except ValueError:
        return None
    if negative or sign == "-":
        value = -abs(value)
    return value


def _parse_inline_document(document: bytes) -> list[dict[str, Any]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(document, "lxml")
    contexts = {}
    for context in soup.find_all(
        lambda tag: tag.name and tag.name.lower() in {"xbrli:context", "context"}
    ):
        context_id = context.get("id")
        if not context_id:
            continue
        start = context.find(
            lambda tag: tag.name and tag.name.lower().endswith("startdate")
        )
        end = context.find(
            lambda tag: tag.name and tag.name.lower().endswith("enddate")
        )
        instant = context.find(
            lambda tag: tag.name and tag.name.lower().endswith("instant")
        )
        segment = context.find(
            lambda tag: tag.name and tag.name.lower().endswith("segment")
        )
        contexts[context_id] = {
            "start": start.get_text(strip=True) if start else None,
            "end": (
                end.get_text(strip=True)
                if end else instant.get_text(strip=True) if instant else None
            ),
            "segmented": segment is not None,
        }
    facts = []
    for fact in soup.find_all(
        lambda tag: tag.name
        and tag.name.lower() in {"ix:nonfraction", "nonfraction"}
    ):
        context_ref = fact.get("contextref") or fact.get("contextRef")
        context = contexts.get(context_ref, {})
        value = _number(
            fact.get_text("", strip=True), fact.get("scale"), fact.get("sign")
        )
        if value is None:
            continue
        facts.append({
            "name": fact.get("name"),
            "context_ref": context_ref,
            "start": context.get("start"),
            "end": context.get("end"),
            "segmented": bool(context.get("segmented")),
            "unit": fact.get("unitref") or fact.get("unitRef"),
            "value": value,
            "scale": int(fact.get("scale") or 0),
            "decimals": fact.get("decimals"),
        })
    return facts


def _parse_classic_instance(document: bytes) -> list[dict[str, Any]]:
    root = etree.fromstring(document)
    contexts = {}
    for context in root.xpath('.//*[local-name()="context"]'):
        context_id = context.get("id")
        if not context_id:
            continue
        start = context.xpath('.//*[local-name()="startDate"]/text()')
        end = context.xpath('.//*[local-name()="endDate"]/text()')
        instant = context.xpath('.//*[local-name()="instant"]/text()')
        contexts[context_id] = {
            "start": str(start[0]) if start else None,
            "end": (
                str(end[0])
                if end else str(instant[0]) if instant else None
            ),
            "segmented": bool(context.xpath('.//*[local-name()="segment"]')),
        }
    namespace_prefix = {
        uri: prefix for prefix, uri in root.nsmap.items() if prefix and uri
    }
    facts = []
    for fact in root.iter():
        context_ref = fact.get("contextRef")
        unit_ref = fact.get("unitRef")
        if not context_ref or not unit_ref or fact.text is None:
            continue
        value = _number(fact.text, None, None)
        if value is None:
            continue
        qname = etree.QName(fact)
        prefix = namespace_prefix.get(qname.namespace)
        context = contexts.get(context_ref, {})
        facts.append({
            "name": f"{prefix}:{qname.localname}" if prefix else qname.localname,
            "context_ref": context_ref,
            "start": context.get("start"),
            "end": context.get("end"),
            "segmented": bool(context.get("segmented")),
            "unit": unit_ref,
            "value": value,
            "scale": 0,
            "decimals": fact.get("decimals"),
        })
    return facts


def parse_inline_xbrl(zip_path: str | Path) -> tuple[str, list[dict[str, Any]]]:
    """Parse either modern Inline XBRL HTML or a classic XBRL instance."""
    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path) as archive:
        html_members = sorted(
            (
                item for item in archive.infolist()
                if item.filename.lower().endswith((".htm", ".html"))
            ),
            key=lambda item: item.file_size,
            reverse=True,
        )
        for member in html_members:
            facts = _parse_inline_document(archive.read(member))
            if facts:
                return member.filename, facts
        xml_members = [
            item for item in archive.infolist()
            if item.filename.lower().endswith(".xml")
            and not any(
                token in item.filename.lower()
                for token in ("_cal", "_def", "_lab", "_pre", "filingsummary")
            )
        ]
        best: tuple[str, list[dict[str, Any]]] | None = None
        for member in xml_members:
            try:
                facts = _parse_classic_instance(archive.read(member))
            except etree.XMLSyntaxError:
                continue
            if best is None or len(facts) > len(best[1]):
                best = (member.filename, facts)
        if best is not None and best[1]:
            return best
    raise ValueError(f"XBRL ZIP has no parseable fact document: {zip_path}")


def probe_targets(
    zip_path: str | Path,
    targets: list[dict[str, Any]],
    output: str | Path,
    *,
    source_url: str | None = None,
    index_path: str | Path | None = None,
) -> dict[str, Any]:
    zip_path = Path(zip_path).resolve()
    output = Path(output).resolve()
    member, facts = parse_inline_xbrl(zip_path)
    results = []
    for target in targets:
        target_concept = str(target["concept"])
        concept = target_concept.split(":")[-1]
        target_end = pd.Timestamp(target["fiscal_end"])
        candidates = [
            fact for fact in facts
            if str(fact.get("name") or "").split(":")[-1] == concept
            and not fact["segmented"]
            and fact.get("end")
            and abs((pd.Timestamp(fact["end"]) - target_end).days) <= 7
            and (
                not target_concept.startswith("derived_q4:")
                or (
                    fact.get("start")
                    and 60
                    <= (pd.Timestamp(fact["end"]) - pd.Timestamp(fact["start"])).days
                    <= 135
                )
            )
        ]
        exact = [
            fact for fact in candidates
            if abs(float(fact["value"]) - float(target["value"])) <= 1e-6
        ]
        results.append({
            "target": target,
            "concept": concept,
            "candidate_count": len(candidates),
            "exact_value_match_count": len(exact),
            "exact_matches": exact,
            "near_end_candidates": candidates,
        })
    index_path = Path(index_path).resolve() if index_path else None
    report = {
        "format_version": 1,
        "research_only": True,
        "source_url": source_url,
        "zip_path": str(zip_path),
        "zip_sha256": _sha256(zip_path),
        "inline_member": member,
        "parsed_fact_count": len(facts),
        "index_path": str(index_path) if index_path else None,
        "index_sha256": _sha256(index_path) if index_path else None,
        "targets": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--target-json", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-url")
    parser.add_argument("--index-path", type=Path)
    args = parser.parse_args()
    targets = [json.loads(value) for value in args.target_json]
    report = probe_targets(
        args.zip,
        targets,
        args.output,
        source_url=args.source_url,
        index_path=args.index_path,
    )
    print(json.dumps({
        "targets": len(report["targets"]),
        "exact_matches": sum(
            row["exact_value_match_count"] for row in report["targets"]
        ),
        "research_only": True,
    }, indent=2))


if __name__ == "__main__":
    main()
