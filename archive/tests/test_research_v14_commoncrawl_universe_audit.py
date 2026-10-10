from scripts.research_v14_commoncrawl_universe_audit import archive_source_url


def test_archive_source_url_binds_exact_warc_range() -> None:
    result = archive_source_url({
        "filename": "crawl-data/example.warc.gz",
        "offset": "123",
        "length": "456",
        "timestamp": "20190102030405",
    })
    assert result == (
        "https://data.commoncrawl.org/crawl-data/example.warc.gz"
        "#offset=123&length=456&timestamp=20190102030405"
    )
