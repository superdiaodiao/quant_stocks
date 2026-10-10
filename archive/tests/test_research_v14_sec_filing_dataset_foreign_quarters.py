import zipfile

import pandas as pd

from scripts.research_v14_sec_filing_dataset_foreign_quarters import (
    load_custom_registry,
    reconstruct_quarters,
    scan_archives,
    scan_unmapped_candidates,
)


def _archive(path, *, submissions, presentation, numbers):
    with zipfile.ZipFile(path, "w") as target:
        target.writestr("sub.txt", pd.DataFrame(submissions).to_csv(sep="\t", index=False))
        target.writestr("pre.txt", pd.DataFrame(presentation).to_csv(sep="\t", index=False))
        target.writestr("num.txt", pd.DataFrame(numbers).to_csv(sep="\t", index=False))


def _submission(adsh, filed):
    return {
        "adsh": adsh,
        "cik": "1704292",
        "name": "ZAI LAB LTD",
        "form": "6-K",
        "filed": filed,
        "fy": "2019",
        "fp": "Q3",
    }


def _presentation(adsh, tag, label):
    return {
        "adsh": adsh,
        "stmt": "IS",
        "tag": tag,
        "version": "us-gaap/2019",
        "plabel": label,
    }


def _number(adsh, tag, end, qtrs, value, segments=""):
    return {
        "adsh": adsh,
        "tag": tag,
        "version": "us-gaap/2019",
        "ddate": end,
        "qtrs": str(qtrs),
        "uom": "USD",
        "segments": segments,
        "coreg": "",
        "value": str(value),
        "footnote": "",
    }


def test_adjacent_ytd_is_differenced_but_isolated_ytd_is_not_quarter(tmp_path):
    q2 = tmp_path / "2019q2.zip"
    q3 = tmp_path / "2019q3.zip"
    revenue = "RevenueFromContractWithCustomerExcludingAssessedTax"
    net_income = "NetIncomeLoss"
    _archive(
        q2,
        submissions=[_submission("a1", "20190510")],
        presentation=[
            _presentation("a1", revenue, "Revenue"),
            _presentation("a1", net_income, "Net loss"),
        ],
        numbers=[
            _number("a1", revenue, "20190331", 1, 10),
            _number("a1", net_income, "20190331", 1, -4),
        ],
    )
    _archive(
        q3,
        submissions=[_submission("a2", "20190809")],
        presentation=[
            _presentation("a2", revenue, "Revenue"),
            _presentation("a2", net_income, "Net loss"),
        ],
        numbers=[
            _number("a2", revenue, "20190630", 2, 35),
            _number("a2", net_income, "20190630", 2, -11),
            _number("a2", revenue, "20190930", 3, 70),
        ],
    )
    raw = scan_archives([q2, q3], {1704292: "ZLAB"})
    quarters, conflicts = reconstruct_quarters(raw)

    assert conflicts.empty
    q2_rows = quarters.loc[quarters["fiscal_end"].eq(pd.Timestamp("2019-06-30"))]
    assert q2_rows.set_index("metric")["value"].to_dict() == {
        "net_income": -7.0,
        "revenue": 25.0,
    }
    assert set(q2_rows["source"]) == {"derived_ytd"}
    # The nine-month revenue has an adjacent qtrs=2 basis and is valid.
    q3_revenue = quarters.loc[
        quarters["fiscal_end"].eq(pd.Timestamp("2019-09-30"))
        & quarters["metric"].eq("revenue")
    ]
    assert q3_revenue.iloc[0]["value"] == 35.0
    # No qtrs=3 net-income fact exists, so no fabricated Q3 income row appears.
    assert quarters.loc[
        quarters["fiscal_end"].eq(pd.Timestamp("2019-09-30"))
        & quarters["metric"].eq("net_income")
    ].empty


def test_isolated_qtrs_three_value_is_rejected(tmp_path):
    archive = tmp_path / "2020q1.zip"
    revenue = "RevenueFromContractWithCustomerExcludingAssessedTax"
    _archive(
        archive,
        submissions=[_submission("zlab-nine-month", "20200121")],
        presentation=[_presentation("zlab-nine-month", revenue, "Revenue")],
        numbers=[_number("zlab-nine-month", revenue, "20190930", 3, 8339732)],
    )
    raw = scan_archives([archive], {1704292: "ZLAB"})
    quarters, conflicts = reconstruct_quarters(raw)
    assert quarters.empty
    assert conflicts.empty


def test_segmented_fact_is_excluded(tmp_path):
    archive = tmp_path / "2020q1.zip"
    revenue = "RevenueFromContractWithCustomerExcludingAssessedTax"
    _archive(
        archive,
        submissions=[_submission("a1", "20200121")],
        presentation=[_presentation("a1", revenue, "Revenue")],
        numbers=[
            _number("a1", revenue, "20191231", 1, 10),
            _number("a1", revenue, "20191231", 1, 3, "Geography=China"),
        ],
    )
    raw = scan_archives([archive], {1704292: "ZLAB"})
    quarters, _ = reconstruct_quarters(raw)
    assert quarters["value"].tolist() == [10.0]


def test_custom_tag_requires_exact_registry_label(tmp_path):
    registry_path = tmp_path / "registry.csv"
    pd.DataFrame([
        {
            "ticker": "ZLAB",
            "cik": 1704292,
            "tag": "CollaborationRevenue",
            "metric": "revenue",
            "statement_label": "Collaboration revenue",
        }
    ]).to_csv(registry_path, index=False)
    archive = tmp_path / "2020q1.zip"
    _archive(
        archive,
        submissions=[_submission("a1", "20200121")],
        presentation=[
            _presentation("a1", "CollaborationRevenue", "Other income")
        ],
        numbers=[_number("a1", "CollaborationRevenue", "20191231", 1, 10)],
    )
    registry = load_custom_registry(registry_path)
    raw = scan_archives([archive], {1704292: "ZLAB"}, registry)
    assert raw.empty


def test_custom_tag_with_exact_registry_label_is_accepted(tmp_path):
    registry_path = tmp_path / "registry.csv"
    pd.DataFrame([
        {
            "ticker": "ZLAB",
            "cik": 1704292,
            "tag": "CollaborationRevenue",
            "metric": "revenue",
            "statement_label": "Collaboration revenue",
        }
    ]).to_csv(registry_path, index=False)
    archive = tmp_path / "2020q1.zip"
    _archive(
        archive,
        submissions=[_submission("a1", "20200121")],
        presentation=[
            _presentation("a1", "CollaborationRevenue", "Collaboration revenue")
        ],
        numbers=[_number("a1", "CollaborationRevenue", "20191231", 1, 10)],
    )
    registry = load_custom_registry(registry_path)
    raw = scan_archives([archive], {1704292: "ZLAB"}, registry)
    quarters, conflicts = reconstruct_quarters(raw)
    assert conflicts.empty
    assert quarters[["ticker", "metric", "value"]].to_dict("records") == [
        {"ticker": "ZLAB", "metric": "revenue", "value": 10.0}
    ]


def test_unmapped_candidate_audit_requires_is_numeric_unsegmented_currency(tmp_path):
    archive = tmp_path / "2020q1.zip"
    _archive(
        archive,
        submissions=[_submission("a1", "20200121")],
        presentation=[
            _presentation("a1", "CollaborationRevenue", "Collaboration revenue"),
            {
                **_presentation("a1", "OtherRevenue", "Other revenue"),
                "stmt": "BS",
            },
        ],
        numbers=[
            _number("a1", "CollaborationRevenue", "20191231", 1, 10),
            _number(
                "a1", "CollaborationRevenue", "20191231", 1, 3,
                "Geography=China",
            ),
            _number("a1", "OtherRevenue", "20191231", 1, 99),
        ],
    )
    candidates = scan_unmapped_candidates([archive], {1704292: "ZLAB"})
    assert candidates[["ticker", "tag", "plabel", "value"]].to_dict("records") == [
        {
            "ticker": "ZLAB",
            "tag": "CollaborationRevenue",
            "plabel": "Collaboration revenue",
            "value": 10,
        }
    ]
