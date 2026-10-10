from __future__ import annotations

import hashlib
import json

from bs4 import BeautifulSoup
import pandas as pd
import pytest

from scripts.research_v14_town_issuer_archive_quarters import (
    BASELINE_GAPS,
    SIGNAL_DATES,
    SOURCES,
    SourceSpec,
    facts_from_release_values,
    normalized_article_text,
    strict_release_values,
    validate_growth_snapshots,
)


def _fixture(
    *,
    published_at: str = "2019-07-25T12:30:00Z",
    net_income_label: str = "Net income",
) -> bytes:
    url = "https://example.test/town-release.html"
    metadata = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "@id": url,
        "url": url,
        "datePublished": published_at,
        "dateModified": published_at,
        "author": {"@type": "Organization", "name": "TowneBank"},
        "sourceOrganization": [{"@type": "Organization", "name": "TowneBank"}],
    }
    return f"""
    <html><head><script type="application/ld+json">{json.dumps(metadata)}</script></head>
    <body><div id="main-body-container">
      <p>TowneBank (NASDAQ: TOWN)</p>
      <table>
        <tr><th colspan="11">Selected Financial Highlights (unaudited)</th></tr>
        <tr><td colspan="11">(dollars in thousands)</td></tr>
        <tr><td></td><td></td><td colspan="2">June 30,</td><td></td>
          <td colspan="2">March 31,</td><td></td>
          <td colspan="2">December 31,</td><td></td>
          <td colspan="2">September 30,</td><td></td>
          <td colspan="2">June 30,</td></tr>
        <tr><td></td><td></td><td colspan="2">2019</td><td></td>
          <td colspan="2">2019</td><td></td>
          <td colspan="2">2018</td><td></td>
          <td colspan="2">2018</td><td></td>
          <td colspan="2">2018</td></tr>
        <tr><td></td><td>Total Revenue</td><td>$</td><td>144,537</td><td></td>
          <td>$</td><td>133,854</td><td></td>
          <td>$</td><td>131,417</td><td></td>
          <td>$</td><td>137,914</td><td></td>
          <td>$</td><td>137,058</td></tr>
        <tr><td></td><td>{net_income_label}</td><td colspan="2">36,242</td><td></td>
          <td colspan="2">32,082</td><td></td>
          <td colspan="2">36,440</td><td></td>
          <td colspan="2">39,252</td><td></td>
          <td colspan="2">36,138</td></tr>
        <tr><td></td><td>Net income available to common shareholders</td>
          <td colspan="2">34,638</td></tr>
      </table>
    </div></body></html>
    """.encode()


def _fixture_spec(raw: bytes) -> SourceSpec:
    body = BeautifulSoup(raw, "html.parser").select_one("#main-body-container")
    assert body is not None
    return SourceSpec(
        release_id="fixture",
        url="https://example.test/town-release.html",
        published_at="2019-07-25T12:30:00Z",
        article_text_sha256=hashlib.sha256(
            normalized_article_text(body).encode()
        ).hexdigest(),
        expected=SOURCES[1].expected,
    )


def test_strict_release_proves_period_currency_and_consolidated_values() -> None:
    raw = _fixture()
    values = strict_release_values(raw, _fixture_spec(raw))

    assert values["2019-06-30"] == (144_537_000.0, 36_242_000.0)
    assert values["2019-03-31"] == (133_854_000.0, 32_082_000.0)
    assert values["2018-06-30"] == (137_058_000.0, 36_138_000.0)


def test_strict_release_rejects_changed_available_date() -> None:
    raw = _fixture(published_at="2019-07-26T12:30:00Z")
    with pytest.raises(RuntimeError, match="publication date changed"):
        strict_release_values(raw, _fixture_spec(raw))


def test_strict_release_rejects_attributable_income_substitution() -> None:
    raw = _fixture(net_income_label="Net income attributable to TowneBank")
    with pytest.raises(RuntimeError, match="Net income row is not unique"):
        strict_release_values(raw, _fixture_spec(raw))


def test_all_release_vintages_restore_every_age_and_date_snapshot() -> None:
    releases = [
        (spec, {
            period: (float(revenue) * 1000.0, float(net_income) * 1000.0)
            for period, revenue, net_income in spec.expected
        })
        for spec in SOURCES
    ]
    facts = facts_from_release_values(releases, pd.Timestamp("2026-08-23"))
    validation = validate_growth_snapshots(facts)

    assert len(facts) == 110
    assert facts["fiscal_end"].nunique() == 17
    assert facts["available_date"].nunique() == 11
    assert validation["scenario_count"] == 6
    assert validation["snapshot_check_count"] == 180
    assert validation["all_supplement_only_snapshots_usable"] is True
    assert len(BASELINE_GAPS) == 13
    assert {date for date, _ in BASELINE_GAPS}.issubset(SIGNAL_DATES)


def test_release_era_revisions_are_not_replaced_by_later_annual_values() -> None:
    releases = [
        (spec, {
            period: (float(revenue) * 1000.0, float(net_income) * 1000.0)
            for period, revenue, net_income in spec.expected
        })
        for spec in SOURCES
    ]
    facts = facts_from_release_values(releases, pd.Timestamp("2026-08-23"))

    q1_2018 = facts.loc[
        facts["fiscal_end"].eq("2018-03-31")
    ].set_index("metric")["value"]
    assert q1_2018.to_dict() == {
        "net_income": 25_943_000.0,
        "revenue": 126_276_000.0,
    }
    q1_2019 = facts.loc[
        facts["fiscal_end"].eq("2019-03-31")
        & facts["available_date"].eq("2019-07-25")
    ].set_index("metric")["value"]
    assert q1_2019.to_dict() == {
        "net_income": 32_082_000.0,
        "revenue": 133_854_000.0,
    }
