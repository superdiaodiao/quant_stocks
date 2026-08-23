from scripts.research_v14_github_universe_audit import raw_url


def test_raw_url_preserves_pinned_blob_ref_and_path() -> None:
    result = raw_url({
        "html_url": (
            "https://github.com/example/project/blob/abc123/"
            "fixtures/nasdaqlisted.txt"
        )
    })
    assert result == (
        "https://raw.githubusercontent.com/example/project/abc123/"
        "fixtures/nasdaqlisted.txt"
    )


def test_raw_url_quotes_spaces_in_path() -> None:
    result = raw_url({
        "html_url": (
            "https://github.com/example/project/blob/abc123/"
            "sample data/nasdaqlisted.txt"
        )
    })
    assert result.endswith("/sample%20data/nasdaqlisted.txt")
