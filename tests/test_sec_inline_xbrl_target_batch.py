import io
import json
import zipfile

from scripts.sec_inline_xbrl_target_batch import batch_probe


def test_batch_probe_checkpoints_exact_inline_fact(tmp_path):
    target_manifest = tmp_path / "targets.json"
    target_manifest.write_text(json.dumps({
        "unmatched_target_rows": [{
            "ticker": "EXM",
            "accession": "0000000001-21-000001",
            "fiscal_end": "2020-12-31",
            "available_date": "2021-03-01",
            "metric": "net_income",
            "value": 19_000_000,
            "concept": "derived_q4:NetIncomeLoss",
        }]
    }))
    snapshot_manifest = tmp_path / "snapshot.json"
    snapshot_manifest.write_text(json.dumps({
        "entries": [{"cik": 1, "symbols": ["EXM"]}]
    }))
    html = """
    <xbrli:context id="d"><xbrli:period>
      <xbrli:startDate>2020-10-01</xbrli:startDate>
      <xbrli:endDate>2020-12-31</xbrli:endDate>
    </xbrli:period></xbrli:context>
    <ix:nonFraction name="us-gaap:NetIncomeLoss" contextRef="d"
      unitRef="USD" scale="3">19,000</ix:nonFraction>
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("filing.htm", html)
    index = json.dumps({
        "directory": {"item": [{"name": "filing-xbrl.zip"}]}
    }).encode()

    def fetcher(url):
        return index if url.endswith("index.json") else buffer.getvalue()

    report = batch_probe(
        target_manifest,
        snapshot_manifest,
        tmp_path / "cache",
        tmp_path / "report.json",
        fetcher=fetcher,
    )

    assert report["status"] == "COMPLETE"
    assert report["status_counts"] == {"EXACT_MATCH": 1}
    assert report["records"][0]["exact_match_count"] == 1
