import json

from scripts.research_v14_import_github_universe import import_missing


def test_import_missing_skips_existing_and_conflicting_dates(tmp_path, monkeypatch):
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    (snapshot_dir / "nasdaq_listed_2019-01-01.csv").write_text(
        "Symbol,Name\nA,A Common Stock\n", encoding="utf-8"
    )
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({
        "research_only": True,
        "records": [
            {"observed_at": "2019-01-01", "payload_sha256": "a", "source": "old"},
            {"observed_at": "2019-02-01", "payload_sha256": "b", "source": "new"},
            {"observed_at": "2019-03-01", "payload_sha256": "c", "source": "c1"},
            {"observed_at": "2019-03-01", "payload_sha256": "d", "source": "c2"},
        ],
    }), encoding="utf-8")

    def fake_import(sources, minimum_rows, snapshot_dir):
        assert sources == ["new"]
        return {"imported": [], "skipped": []}

    monkeypatch.setattr(
        "scripts.research_v14_import_github_universe.import_nasdaq_trader_files",
        fake_import,
    )
    result = import_missing(
        catalog_path=catalog, snapshot_dir=snapshot_dir, minimum_rows=1
    )
    assert result["already_present_dates"] == ["2019-01-01"]
    assert [row["observed_at"] for row in result["selected"]] == ["2019-02-01"]
    assert result["conflicts"][0]["observed_at"] == "2019-03-01"
