import json

from scripts.research_v14_recover_commoncrawl_universe import recover


def test_recover_uses_isolated_directory_and_binds_catalog(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    baseline_snapshot = baseline / "nasdaq_listed_2020-01-01.csv"
    baseline_snapshot.write_text("Symbol,Name\nA,A Common Stock\n", encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({
        "research_only": True,
        "errors": [],
        "captures": [{"archive_source": "https://example.test/archive"}],
    }), encoding="utf-8")
    destination = tmp_path / "v14"

    def fake_import(sources, minimum_rows, snapshot_dir):
        assert sources == ["https://example.test/archive"]
        assert snapshot_dir == destination
        return {"imported": [], "skipped": []}

    monkeypatch.setattr(
        "scripts.research_v14_recover_commoncrawl_universe.import_nasdaq_trader_files",
        fake_import,
    )
    result = recover(
        catalog_path=catalog, output_dir=destination,
        baseline_dir=baseline, minimum_rows=1,
    )
    assert (destination / baseline_snapshot.name).read_bytes() == baseline_snapshot.read_bytes()
    assert result["formal_universe_modified"] is False
    assert len(result["catalog"]["sha256"]) == 64
