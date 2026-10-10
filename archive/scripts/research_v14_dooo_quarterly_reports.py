#!/usr/bin/env python3
"""Recover historical ticker DOOO quarterly IFRS facts from SEC 6-K filings."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
from bs4 import BeautifulSoup


DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/dooo_quarterly_reports_2018_01_2021_10"
)
SEC_BASE = "https://www.sec.gov/Archives/edgar/data/1748797"
HEADERS = {"User-Agent": "quant_stocks research contact@example.com"}

# BRP's fiscal year ends January 31. Values are direct three-month IFRS values
# in millions of Canadian dollars, not natural-calendar quarter assignments.
PERIOD_EVIDENCE = {
    # The September 2018 F-10 exhibit reports direct three-month IFRS values
    # for these historical fiscal quarters.  The January 2018 values are the
    # IFRS 15/9-restated comparatives disclosed in that exhibit, rather than
    # the superseded values in the earlier annual MD&A.
    "2018-01-31": ("2018-09-11", "0001193125-18-270412", "d607448dex45.htm", 1_226.0, 70.0),
    "2018-04-30": ("2018-09-11", "0001193125-18-270412", "d607448dex45.htm", 1_136.7, 13.4),
    "2018-07-31": ("2018-09-11", "0001193125-18-270412", "d607448dex45.htm", 1_207.0, 41.0),
    "2018-10-31": ("2018-11-30", "0001193125-18-338752", "d664085dex991.htm", 1_394.2, 90.2),
    "2019-01-31": ("2019-03-22", "0001193125-19-082711", "d726682dex991.htm", 1_505.9, 82.7),
    "2019-04-30": ("2019-05-30", "0001193125-19-160255", "d753816dex991.htm", 1_333.7, 23.8),
    "2019-07-31": ("2019-08-29", "0001193125-19-233023", "d768382dex991.htm", 1_459.5, 93.3),
    "2019-10-31": ("2019-11-27", "0001193125-19-301727", "d834788dex991.htm", 1_643.6, 135.3),
    "2020-01-31": ("2020-03-20", "0001193125-20-079953", "d883406dex991.htm", 1_615.9, 118.2),
    "2020-04-30": ("2020-05-28", "0001193125-20-153739", "d921226dex991.htm", 1_229.8, -226.1),
    "2020-07-31": ("2020-08-27", "0001193125-20-231925", "d929929dex991.htm", 1_233.3, 126.1),
    "2020-10-31": ("2020-11-25", "0001193125-20-303033", "d33932dex991.htm", 1_674.7, 198.7),
    "2021-01-31": ("2021-03-25", "0001193125-21-093359", "d150628dex991.htm", 1_815.1, 264.2),
    "2021-04-30": ("2021-06-03", "0001193125-21-180600", "d179598dex991.htm", 1_808.6, 244.4),
    "2021-07-31": ("2021-09-02", "0001193125-21-263680", "d169521dex991.htm", 1_903.8, 212.9),
    "2021-10-31": ("2021-12-01", "0001193125-21-344370", "d201255dex991.htm", 1_588.0, 127.7),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number_patterns(value: float) -> tuple[str, ...]:
    absolute = f"{abs(value):,.1f}"
    if value < 0:
        return (rf"\(\s*{re.escape(absolute)}\s*\)", rf"-\s*{re.escape(absolute)}")
    return (rf"\$?\s*{re.escape(absolute)}",)


def validate_statement(raw: bytes, fiscal_end: str, revenue: float,
                       net_income: float) -> None:
    if fiscal_end not in PERIOD_EVIDENCE:
        raise ValueError("DOOO fiscal period is not predeclared")
    declared = PERIOD_EVIDENCE[fiscal_end]
    if revenue != declared[3] or net_income != declared[4]:
        raise ValueError("DOOO values do not match predeclared direct-quarter evidence")
    text = " ".join(BeautifulSoup(raw, "html.parser").get_text(" ").split())
    if not re.search(r"\bBRP Inc\.", text, re.I):
        raise ValueError("DOOO issuer identity is not proven")
    if not re.search(r"millions of Canadian dollars", text, re.I):
        raise ValueError("DOOO CAD-millions scale is not proven")
    period = pd.Timestamp(fiscal_end).strftime("%B %d, %Y").replace(" 0", " ")
    if period.lower() not in text.lower():
        raise ValueError("DOOO requested fiscal period is not proven")
    period_starts = [match.start() for match in re.finditer(re.escape(period), text, re.I)]
    evidence_windows = [text[max(0, start - 500):start + 6_000]
                        for start in period_starts]
    revenue_patterns = _number_patterns(revenue)
    income_patterns = _number_patterns(net_income)
    direct_window_proven = any(
        re.search(r"three-month periods? ended", window, re.I)
        and re.search(r"(?:Total )?Revenues?", window, re.I)
        and re.search(r"Net income(?: \(loss\))?|Net loss", window, re.I)
        and any(re.search(pattern, window) for pattern in revenue_patterns)
        and any(re.search(pattern, window) for pattern in income_patterns)
        for window in evidence_windows
    )
    if not direct_window_proven:
        raise ValueError(
            "DOOO direct three-month revenue and net income are not proven "
            "for the requested fiscal period"
        )


def _row(*, fiscal_end: str, available_date: str, metric: str, value: float,
         accession: str, archive: str, archive_sha256: str) -> dict:
    return {
        "ticker": "DOOO", "fiscal_end": fiscal_end,
        "available_date": available_date, "metric": metric,
        "value": float(value * 1_000_000), "taxonomy": "ifrs-full",
        "concept": "Revenue" if metric == "revenue" else "ProfitLoss",
        "form": "6-K", "accession": accession, "unit": "CAD",
        "source": "sec_brp_contemporaneous_quarterly_statement",
        "source_archive": archive, "source_archive_sha256": archive_sha256,
        "derivation": "direct_sec_three_month_ifrs_statement",
    }


def run(*, output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    rows, sources = [], []
    for fiscal_end, item in PERIOD_EVIDENCE.items():
        filed, accession, document, revenue, net_income = item
        url = f"{SEC_BASE}/{accession.replace('-', '')}/{document}"
        path = raw_dir / f"{accession}_{document}"
        if not path.exists():
            with urlopen(Request(url, headers=HEADERS), timeout=120) as response:
                path.write_bytes(response.read())
        sha = _sha256(path)
        validate_statement(path.read_bytes(), fiscal_end, revenue, net_income)
        sources.append({"fiscal_end": fiscal_end, "filed": filed,
                        "accession": accession, "document": document,
                        "url": url, "path": str(path), "sha256": sha})
        rows.extend([
            _row(fiscal_end=fiscal_end, available_date=filed, metric="revenue",
                 value=revenue, accession=accession, archive=path.name,
                 archive_sha256=sha),
            _row(fiscal_end=fiscal_end, available_date=filed, metric="net_income",
                 value=net_income, accession=accession, archive=path.name,
                 archive_sha256=sha),
        ])
    facts = pd.DataFrame(rows).sort_values(["fiscal_end", "metric"])
    expected_quarters = len(PERIOD_EVIDENCE)
    if (len(facts) != expected_quarters * 2
            or facts["fiscal_end"].nunique() != expected_quarters):
        raise RuntimeError("DOOO recovery does not match the predeclared paired quarters")
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "promotion_eligible": False, "release_status": "BLOCKED",
        "ticker": "DOOO", "current_sec_ticker": "DO",
        "accepted_quarter_count": expected_quarters,
        "recovered_quarters": [
            {"ticker": "DOOO", "fiscal_end": end, "available_date": item[0],
             "revenue": item[3] * 1_000_000,
             "net_income": item[4] * 1_000_000}
            for end, item in PERIOD_EVIDENCE.items()
        ],
        "filing_sources": sources,
        "outputs": {"quarters": {"path": str(facts_path),
                                   "sha256": _sha256(facts_path)}},
        "guardrail": (
            "Fiscal periods follow BRP's January year-end, not calendar-quarter "
            "labels. Only direct three-month consolidated IFRS revenue and net "
            "income/loss in CAD are accepted, including explicitly restated "
            "direct-quarter tables; normalized and attributable metrics and "
            "cumulative-period differences are rejected."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run(output_dir=args.output_dir)
    print(json.dumps({"accepted_quarter_count": result["accepted_quarter_count"],
                      "manifest": result["manifest"],
                      "release_status": result["release_status"]},
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
