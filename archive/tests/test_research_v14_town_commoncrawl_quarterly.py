from scripts.research_v14_town_commoncrawl_quarterly import (
    parse_town_2019q3,
)


def test_town_parser_rejects_unproven_identity() -> None:
    try:
        parse_town_2019q3(b"%PDF-1.4\n%%EOF")
    except Exception as error:
        assert (
            "pdf" in type(error).__name__.lower()
            or "identity" in str(error).lower()
        )
    else:
        raise AssertionError("invalid TOWN filing should be rejected")
