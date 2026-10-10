#!/usr/bin/env python3
"""Recover TOWN quarterly PIT vintages from issuer-submitted news archives."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
from io import StringIO
import json
from pathlib import Path
import re
import time
from typing import Any
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, Tag
import pandas as pd

from src.financial.quarterly_fundamentals import quarterly_growth_snapshot
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/town_issuer_archive_quarters")
SIGNAL_DATES = (
    "2019-07-31", "2019-08-30", "2019-09-30", "2019-10-31",
    "2019-11-29", "2019-12-31", "2020-01-31", "2020-02-28",
    "2020-03-31", "2020-04-30", "2020-05-29", "2020-06-30",
    "2020-07-31", "2020-08-31", "2020-09-30", "2020-10-30",
    "2020-11-30", "2020-12-31", "2021-01-29", "2021-02-26",
    "2021-03-31", "2021-04-30", "2021-05-28", "2021-06-30",
    "2021-07-30", "2021-08-31", "2021-09-30", "2021-10-29",
    "2021-11-30", "2021-12-31",
)
MAXIMUM_AGES = (150, 365, 550)
LIQUIDITY_THRESHOLDS = (2_000_000, 10_000_000)
BASELINE_GAPS = (
    ("2019-07-31", "no_raw_pit_financial_facts"),
    ("2019-09-30", "no_raw_pit_financial_facts"),
    ("2019-10-31", "no_raw_pit_financial_facts"),
    ("2019-12-31", "insufficient_growth_history"),
    ("2020-01-31", "insufficient_growth_history"),
    ("2020-12-31", "stale_growth_snapshot"),
    ("2021-01-29", "stale_growth_snapshot"),
    ("2021-07-30", "stale_growth_snapshot"),
    ("2021-08-31", "stale_growth_snapshot"),
    ("2021-09-30", "stale_growth_snapshot"),
    ("2021-10-29", "stale_growth_snapshot"),
    ("2021-11-30", "stale_growth_snapshot"),
    ("2021-12-31", "stale_growth_snapshot"),
)


@dataclass(frozen=True)
class SourceSpec:
    release_id: str
    url: str
    published_at: str
    article_text_sha256: str
    # fiscal_end, Total Revenue, Net income; source units are USD thousands.
    expected: tuple[tuple[str, int, int], ...]

    @property
    def available_date(self) -> str:
        return self.published_at[:10]


def _source(
    release_id: str,
    date: str,
    slug: str,
    published_at: str,
    article_text_sha256: str,
    periods: tuple[str, ...],
    revenue: tuple[int, ...],
    net_income: tuple[int, ...],
) -> SourceSpec:
    if not (len(periods) == len(revenue) == len(net_income) == 5):
        raise ValueError("each TOWN release must contain exactly five quarters")
    return SourceSpec(
        release_id=release_id,
        url=(
            f"https://www.globenewswire.com/news-release/{date}/"
            f"{release_id}/10357/en/{slug}.html"
        ),
        published_at=published_at,
        article_text_sha256=article_text_sha256,
        expected=tuple(zip(periods, revenue, net_income, strict=True)),
    )


SOURCES = (
    _source(
        "1627134", "2018/10/25",
        "townebank-reports-third-quarter-2018-earnings",
        "2018-10-25T12:30:00Z",
        "96fe1112283f604cabe2d0ce2d7c0b589a1846a4da734f1534d6c91291f51d97",
        ("2018-09-30", "2018-06-30", "2018-03-31", "2017-12-31", "2017-09-30"),
        (137_915, 137_058, 126_276, 109_141, 115_339),
        (39_252, 36_138, 25_943, 13_287, 28_595),
    ),
    _source(
        "1888012", "2019/07/25",
        "townebank-reports-second-quarter-2019-earnings",
        "2019-07-25T12:30:00Z",
        "951f9b2fa477e9ecbff3d51f3b15548d49bd964687746f5cd27152923632f9ff",
        ("2019-06-30", "2019-03-31", "2018-12-31", "2018-09-30", "2018-06-30"),
        (144_537, 133_854, 131_417, 137_914, 137_058),
        (36_242, 32_082, 36_440, 39_252, 36_138),
    ),
    _source(
        "1934966", "2019/10/24",
        "townebank-reports-third-quarter-2019-earnings",
        "2019-10-24T12:30:00Z",
        "78575b0ca47f136a8170d5ebd61df553946af343b1b120c845831524761f2ed6",
        ("2019-09-30", "2019-06-30", "2019-03-31", "2018-12-31", "2018-09-30"),
        (145_879, 144_537, 133_854, 131_417, 137_914),
        (39_400, 36_242, 32_082, 36_440, 39_252),
    ),
    _source(
        "1974318", "2020/01/23",
        "townebank-reports-full-year-and-fourth-quarter-financial-results-for-2019",
        "2020-01-23T13:30:00Z",
        "24029c537964f05dc0e8f203e39e3fd19078af857e2b0a9358a4557d9eec7df7",
        ("2019-12-31", "2019-09-30", "2019-06-30", "2019-03-31", "2018-12-31"),
        (139_671, 145_879, 144_537, 133_854, 131_417),
        (35_948, 39_400, 36_242, 32_082, 36_440),
    ),
    _source(
        "2022456", "2020/04/27",
        "townebank-reports-first-quarter-2020-earnings",
        "2020-04-27T12:30:00Z",
        "2779dd4d9957e4194287c891600ba17ef3a4c2c2092e50b84131adbac379f670",
        ("2020-03-31", "2019-12-31", "2019-09-30", "2019-06-30", "2019-03-31"),
        (137_696, 139_671, 145_879, 144_537, 133_854),
        (27_605, 35_948, 39_400, 36_242, 32_082),
    ),
    _source(
        "2066581", "2020/07/23",
        "townebank-reports-second-quarter-2020-earnings",
        "2020-07-23T12:30:00Z",
        "8053db6778e8ba986eb68751d34401e7c4c6f23ec8f3862071c9ee773090ac96",
        ("2020-06-30", "2020-03-31", "2019-12-31", "2019-09-30", "2019-06-30"),
        (162_656, 137_696, 139_671, 145_879, 144_537),
        (37_222, 27_605, 35_948, 39_400, 36_242),
    ),
    _source(
        "2112814", "2020/10/22",
        "townebank-reports-third-quarter-2020-earnings",
        "2020-10-22T12:30:00Z",
        "056eaaad2be8aa582a4481aacbb3825339ea4b0fb345d8450b0fb5bc3f678c1d",
        ("2020-09-30", "2020-06-30", "2020-03-31", "2019-12-31", "2019-09-30"),
        (192_135, 162_656, 137_696, 139_671, 145_879),
        (50_715, 37_222, 27_605, 35_948, 39_400),
    ),
    _source(
        "2165848", "2021/01/28",
        "townebank-reports-full-year-and-fourth-quarter-financial-results-for-2020",
        "2021-01-28T13:30:00Z",
        "83d3a27c48a77bcf6185e743633c70e31b565f50530839967dbb1a36a684cccb",
        ("2020-12-31", "2020-09-30", "2020-06-30", "2020-03-31", "2019-12-31"),
        (171_848, 192_135, 162_656, 137_696, 139_671),
        (53_891, 50_715, 37_222, 27_605, 35_948),
    ),
    _source(
        "2219757", "2021/04/29",
        "townebank-reports-record-first-quarter-2021-earnings",
        "2021-04-29T12:30:00Z",
        "940ee23e9e5da4207179a1e65017a2141366cf3eabecd0defb583e20f39a0a3a",
        ("2021-03-31", "2020-12-31", "2020-09-30", "2020-06-30", "2020-03-31"),
        (182_509, 171_848, 192_135, 162_656, 137_696),
        (72_631, 53_891, 50_715, 37_222, 27_605),
    ),
    _source(
        "2271315", "2021/07/29",
        "townebank-reports-second-quarter-2021-earnings",
        "2021-07-29T12:30:00Z",
        "3779e306752e544cc8c6ef9c14aee0333149d25f406eedea961554d791bbacb8",
        ("2021-06-30", "2021-03-31", "2020-12-31", "2020-09-30", "2020-06-30"),
        (167_321, 182_509, 171_848, 192_135, 162_656),
        (58_002, 72_631, 53_891, 50_715, 37_222),
    ),
    _source(
        "2322778", "2021/10/28",
        "townebank-reports-third-quarter-2021-earnings",
        "2021-10-28T12:30:00Z",
        "1bc8e0c2b431838c725143feb62b28275b09a0146b1d1e3d16a13098277d2f8c",
        ("2021-09-30", "2021-06-30", "2021-03-31", "2020-12-31", "2020-09-30"),
        (170_076, 167_321, 182_509, 171_848, 192_135),
        (52_743, 58_002, 72_631, 53_891, 50_715),
    ),
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalized_article_text(body: Tag) -> str:
    return " ".join(body.get_text(" ", strip=True).split())


def _organization_names(value: Any) -> set[str]:
    if isinstance(value, dict):
        names = {str(value.get("name", ""))}
        return names | _organization_names(value.get("sourceOrganization"))
    if isinstance(value, list):
        names: set[str] = set()
        for item in value:
            names |= _organization_names(item)
        return names
    return set()


def strict_article(raw: bytes, spec: SourceSpec) -> tuple[Tag, dict[str, Any]]:
    soup = BeautifulSoup(raw, "html.parser")
    articles: list[dict[str, Any]] = []
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            decoded = json.loads(tag.string or tag.get_text())
        except json.JSONDecodeError:
            continue
        candidates = decoded if isinstance(decoded, list) else [decoded]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("@type") == "NewsArticle":
                articles.append(candidate)
    matches = [
        article for article in articles
        if article.get("@id") == spec.url and article.get("url") == spec.url
    ]
    if len(matches) != 1:
        raise RuntimeError(f"TOWN release {spec.release_id} identity is not unique")
    article = matches[0]
    if (
        article.get("datePublished") != spec.published_at
        or article.get("dateModified") != spec.published_at
    ):
        raise RuntimeError(f"TOWN release {spec.release_id} publication date changed")
    if "TowneBank" not in _organization_names(article.get("author")):
        raise RuntimeError(f"TOWN release {spec.release_id} author is not TowneBank")
    if "TowneBank" not in _organization_names(article.get("sourceOrganization")):
        raise RuntimeError(f"TOWN release {spec.release_id} source is not TowneBank")
    body = soup.select_one("#main-body-container")
    if not isinstance(body, Tag):
        raise RuntimeError(f"TOWN release {spec.release_id} article body is absent")
    text = normalized_article_text(body)
    if _sha256_bytes(text.encode()) != spec.article_text_sha256:
        raise RuntimeError(f"TOWN release {spec.release_id} article text SHA256 changed")
    lower = text.lower()
    required = (
        "nasdaq: town",
        "selected financial highlights",
        "unaudited",
        "dollars in thousands",
    )
    if any(marker not in lower for marker in required):
        raise RuntimeError(f"TOWN release {spec.release_id} source/unit proof changed")
    return body, article


_PERIOD_RE = re.compile(r"(March|June|September|December)\s+(30|31),")
_MONTHS = {"March": 3, "June": 6, "September": 9, "December": 12}


def _cell_text(value: Any) -> str:
    if pd.isna(value):
        return ""
    return " ".join(str(value).replace("\xa0", " ").split())


def _period_groups(frame: pd.DataFrame) -> list[tuple[str, int, int]]:
    candidates: list[list[tuple[str, int, int]]] = []
    for row_index in range(len(frame) - 1):
        labels = [_cell_text(value) for value in frame.iloc[row_index]]
        years = [_cell_text(value) for value in frame.iloc[row_index + 1]]
        starts: list[tuple[str, int]] = []
        previous = ""
        for column, label in enumerate(labels):
            match = _PERIOD_RE.fullmatch(label)
            if match and label != previous and re.fullmatch(r"20\d{2}", years[column]):
                year = int(years[column])
                month = _MONTHS[match.group(1)]
                day = int(match.group(2))
                starts.append((f"{year:04d}-{month:02d}-{day:02d}", column))
            previous = label
        if len(starts) == 5:
            groups = []
            for index, (period, start) in enumerate(starts):
                end = starts[index + 1][1] if index + 1 < len(starts) else len(labels)
                groups.append((period, start, end))
            candidates.append(groups)
    if len(candidates) != 1:
        raise RuntimeError("TOWN selected highlights period header is not unique")
    return candidates[0]


def _parse_number(value: Any) -> float | None:
    text = _cell_text(value)
    if not text or text == "$":
        return None
    if not re.fullmatch(r"\(?[\d,]+\)?", text):
        return None
    negative = text.startswith("(")
    number = float(re.sub(r"\D", "", text))
    return -number if negative else number


def _metric_values(
    frame: pd.DataFrame,
    label: str,
    groups: list[tuple[str, int, int]],
) -> list[int]:
    rows = []
    for row_index in range(len(frame)):
        cells = [_cell_text(value) for value in frame.iloc[row_index]]
        if label in cells:
            rows.append(row_index)
    if len(rows) != 1:
        raise RuntimeError(f"TOWN selected highlights {label} row is not unique")
    row = frame.iloc[rows[0]]
    values = []
    for _, start, end in groups:
        observed = {
            number for number in (_parse_number(value) for value in row.iloc[start:end])
            if number is not None
        }
        if len(observed) != 1:
            raise RuntimeError(f"TOWN selected highlights {label} value is ambiguous")
        values.append(int(observed.pop()))
    return values


def strict_release_values(raw: bytes, spec: SourceSpec) -> dict[str, tuple[float, float]]:
    body, _ = strict_article(raw, spec)
    tables = []
    for table in body.find_all("table"):
        text = normalized_article_text(table)
        if (
            "Selected Financial Highlights" in text
            and "Total Revenue" in text
            and "Net income" in text
        ):
            tables.append(table)
    if len(tables) != 1:
        raise RuntimeError("TOWN selected financial highlights table is not unique")
    frames = pd.read_html(StringIO(str(tables[0])), displayed_only=False)
    if len(frames) != 1:
        raise RuntimeError("TOWN selected highlights HTML table is ambiguous")
    frame = frames[0]
    groups = _period_groups(frame)
    periods = [period for period, _, _ in groups]
    revenue = _metric_values(frame, "Total Revenue", groups)
    net_income = _metric_values(frame, "Net income", groups)
    observed_thousands = tuple(zip(periods, revenue, net_income, strict=True))
    if observed_thousands != spec.expected:
        raise RuntimeError(
            f"TOWN release {spec.release_id} exact quarterly values changed: "
            f"{observed_thousands}"
        )
    return {
        period: (float(revenue_value) * 1000.0, float(net_income_value) * 1000.0)
        for period, revenue_value, net_income_value in observed_thousands
    }


def _download_source(spec: SourceSpec, raw_dir: Path) -> tuple[SourceSpec, bytes]:
    path = raw_dir / f"globenewswire_{spec.release_id}.html"
    if path.exists():
        raw = path.read_bytes()
        strict_release_values(raw, spec)
        return spec, raw
    request = Request(
        spec.url,
        headers={"User-Agent": "Mozilla/5.0 quant-research contact@example.com"},
    )
    error: Exception | None = None
    for attempt in range(4):
        try:
            with urlopen(request, timeout=120) as response:
                raw = response.read()
            strict_release_values(raw, spec)
            path.write_bytes(raw)
            return spec, raw
        except Exception as exc:  # pragma: no cover - network retry
            error = exc
            if attempt < 3:
                time.sleep(attempt + 1)
    raise RuntimeError(f"failed to fetch strict TOWN release {spec.release_id}") from error


def facts_from_release_values(
    releases: list[tuple[SourceSpec, dict[str, tuple[float, float]]]],
    fetched_at: pd.Timestamp,
) -> pd.DataFrame:
    records = []
    for spec, values in releases:
        for fiscal_end, (revenue, net_income) in values.items():
            for metric, value, concept in (
                ("revenue", revenue, "SelectedFinancialHighlightsTotalRevenue"),
                ("net_income", net_income, "SelectedFinancialHighlightsNetIncome"),
            ):
                records.append({
                    "ticker": "TOWN",
                    "fiscal_end": fiscal_end,
                    "available_date": spec.available_date,
                    "metric": metric,
                    "value": value,
                    "taxonomy": "issuer-newswire-usd-thousands",
                    "concept": concept,
                    "form": "EARNINGS-RELEASE",
                    "accession": f"GNW-{spec.release_id}",
                    "fetched_at": fetched_at,
                })
    facts = pd.DataFrame(records, columns=OUTPUT_COLUMNS).sort_values(
        ["available_date", "fiscal_end", "metric"]
    ).reset_index(drop=True)
    if len(facts) != 110 or facts["fiscal_end"].nunique() != 17:
        raise RuntimeError("TOWN issuer archive must prove 110 vintages over 17 quarters")
    if facts.duplicated(["ticker", "fiscal_end", "available_date", "metric"]).any():
        raise RuntimeError("TOWN issuer archive contains duplicate PIT coordinates")
    return facts


def validate_growth_snapshots(facts: pd.DataFrame) -> dict[str, Any]:
    growth_input = facts.copy()
    growth_input["fiscal_end"] = pd.to_datetime(growth_input["fiscal_end"])
    growth_input["available_date"] = pd.to_datetime(growth_input["available_date"])
    scenarios = []
    for liquidity in LIQUIDITY_THRESHOLDS:
        for maximum_age in MAXIMUM_AGES:
            for signal_date in SIGNAL_DATES:
                as_of = pd.Timestamp(signal_date)
                snapshot = quarterly_growth_snapshot(
                    growth_input, as_of, maximum_age_days=maximum_age
                )
                if "TOWN" not in snapshot.index:
                    raise RuntimeError(
                        f"TOWN remains absent at {signal_date}, age {maximum_age}"
                    )
                row = snapshot.loc["TOWN"]
                if row["growth_available_date"] > as_of:
                    raise RuntimeError("TOWN snapshot used a future release")
                if not 0 <= int(row["financial_age_days"]) <= maximum_age:
                    raise RuntimeError("TOWN snapshot violates the financial age limit")
            scenarios.append({
                "minimum_average_daily_dollar_volume": liquidity,
                "maximum_financial_age_days": maximum_age,
                "first_signal_date": SIGNAL_DATES[0],
                "last_signal_date": SIGNAL_DATES[-1],
                "validated_signal_count": len(SIGNAL_DATES),
            })
    return {
        "scenario_count": len(scenarios),
        "snapshot_check_count": len(scenarios) * len(SIGNAL_DATES),
        "all_supplement_only_snapshots_usable": True,
        "scenarios": scenarios,
    }


def recover(output_dir: Path = OUTPUT_DIR) -> dict[str, Any]:
    output_dir = Path(output_dir)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=3) as executor:
        downloaded = list(executor.map(
            lambda spec: _download_source(spec, raw_dir), SOURCES
        ))
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    parsed = [
        (spec, strict_release_values(raw, spec))
        for spec, raw in downloaded
    ]
    facts = facts_from_release_values(parsed, fetched_at)
    snapshot_validation = validate_growth_snapshots(facts)

    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    sources = []
    for spec, raw in downloaded:
        source_path = raw_dir / f"globenewswire_{spec.release_id}.html"
        sources.append({
            "release_id": spec.release_id,
            "accession": f"GNW-{spec.release_id}",
            "form": "EARNINGS-RELEASE",
            "issuer": "TowneBank",
            "url": spec.url,
            "published_at": spec.published_at,
            "available_date": spec.available_date,
            "currency": "USD",
            "source_unit": "thousands",
            "article_text_sha256": spec.article_text_sha256,
            "download_sha256": _sha256_bytes(raw),
            "path": str(source_path),
            "periods": [period for period, _, _ in spec.expected],
        })
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "shared_candidate_modified": False,
        "ticker": "TOWN",
        "fdic_certificate": 35095,
        "accepted_release_count": len(SOURCES),
        "accepted_unique_quarter_count": facts["fiscal_end"].nunique(),
        "accepted_fact_observation_count": len(facts),
        "first_fiscal_end": facts["fiscal_end"].min(),
        "last_fiscal_end": facts["fiscal_end"].max(),
        "baseline_gap_binding": {
            "audit": "step_dkng_imab_audit_v2",
            "minimum_average_daily_dollar_volume": 2_000_000,
            "age_150_unique_gap_count": len(BASELINE_GAPS),
            "age_365_gap_count": 5,
            "age_550_gap_count": 5,
            "gaps": [
                {"signal_date": signal_date, "reason": reason}
                for signal_date, reason in BASELINE_GAPS
            ],
        },
        "snapshot_validation": snapshot_validation,
        "sources": sources,
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path),
            "sha256": _sha256_bytes(facts_path.read_bytes()),
        }},
        "revision_guardrail": {
            "2018-03-31_release_vintage": {
                "revenue": 126_276_000.0,
                "net_income": 25_943_000.0,
            },
            "2019-03-31_release_vintage": {
                "revenue": 133_854_000.0,
                "net_income": 32_082_000.0,
            },
            "rule": (
                "Every five-quarter table remains a separate available-date "
                "vintage; later annual-report revisions are never backdated."
            ),
        },
        "guardrail": (
            "The source is an issuer-authored GlobeNewswire archive whose "
            "NewsArticle metadata, publication timestamp, normalized article "
            "SHA256, USD-thousands caption, five fiscal periods, Total Revenue, "
            "and consolidated Net income are all locked. Net income available "
            "to common shareholders and non-GAAP revenue are rejected. This "
            "isolated supplement does not modify or integrate any shared "
            "candidate or formal financial file."
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
    args = parser.parse_args()
    report = recover(args.output_dir)
    print(json.dumps({
        "manifest": report["manifest"],
        "accepted_release_count": report["accepted_release_count"],
        "accepted_unique_quarter_count": report["accepted_unique_quarter_count"],
        "accepted_fact_observation_count": report["accepted_fact_observation_count"],
        "snapshot_check_count": report["snapshot_validation"]["snapshot_check_count"],
        "promotion_eligible": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
