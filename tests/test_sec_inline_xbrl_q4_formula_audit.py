import json
import zipfile

import pandas as pd

from scripts.sec_inline_xbrl_q4_formula_audit import audit_q4_formulas


def test_formula_audit_combines_annual_xbrl_with_three_pit_quarters(tmp_path):
    html = """
    <xbrli:context id="annual"><xbrli:period>
      <xbrli:startDate>2020-01-01</xbrli:startDate>
      <xbrli:endDate>2020-12-31</xbrli:endDate>
    </xbrli:period></xbrli:context>
    <ix:nonFraction name="us-gaap:NetIncomeLoss" contextRef="annual"
      unitRef="USD">100</ix:nonFraction>
    """
    filing = tmp_path / "filing.zip"
    with zipfile.ZipFile(filing, "w") as archive:
        archive.writestr("filing.htm", html)
    target = {
        "ticker": "EXM",
        "fiscal_end": "2020-12-31",
        "available_date": "2021-03-01",
        "metric": "net_income",
        "value": 40,
        "concept": "derived_q4:NetIncomeLoss",
        "accession": "a",
    }
    target_manifest = tmp_path / "targets.json"
    target_manifest.write_text(json.dumps({"unmatched_target_rows": [target]}))
    inline_batch = tmp_path / "batch.json"
    inline_batch.write_text(json.dumps({"records": [{
        "ticker": "EXM",
        "accession": "a",
        "status": "NO_EXACT_MATCH",
        "xbrl_path": str(filing),
    }]}))
    quarters = pd.DataFrame([
        {
            "ticker": "EXM",
            "fiscal_end": end,
            "available_date": available,
            "metric": "net_income",
            "value": value,
            "taxonomy": "us-gaap",
            "concept": "NetIncomeLoss",
            "form": "10-Q",
            "accession": accession,
            "fetched_at": "2026-08-01",
        }
        for end, available, value, accession in (
            ("2020-03-31", "2020-05-01", 10, "q1"),
            ("2020-06-30", "2020-08-01", 20, "q2"),
            ("2020-09-30", "2020-11-01", 30, "q3"),
        )
    ])
    quarterly_path = tmp_path / "quarterly.csv"
    quarters.to_csv(quarterly_path, index=False)

    report = audit_q4_formulas(
        target_manifest,
        inline_batch,
        quarterly_path,
        tmp_path / "formula.json",
    )

    assert report["formula_match_count"] == 1
    proof = report["proofs"][0]
    assert proof["reason"] == "formula_proven"
    assert proof["selected_attempt"]["expected_q4_value"] == 40
    assert len(proof["selected_attempt"]["quarter_operands"]) == 3
