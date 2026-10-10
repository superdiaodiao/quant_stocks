from scripts.research_v14_reparse_candidate_companyfacts import mapped_tickers


def test_mapped_tickers_excludes_explicit_unresolved() -> None:
    assert mapped_tickers({
        "requested_tickers": ["A", "B", "C"],
        "unresolved_tickers": ["B"],
        "cache_refresh": {"failures": [{"ticker": "C"}]},
    }) == ["A"]


def test_mapped_tickers_requires_an_actual_cached_payload_when_given() -> None:
    backfill = {
        "requested_tickers": ["A", "B"],
        "unresolved_tickers": [],
        "cache_refresh": {"failures": []},
    }
    assert mapped_tickers(backfill, {"A"}) == ["A"]
