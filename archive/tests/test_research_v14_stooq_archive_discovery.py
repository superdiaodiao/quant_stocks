import hashlib

from scripts.research_v14_stooq_archive_discovery import archive_links, solve_pow


def test_archive_links_select_zip_only() -> None:
    assert archive_links(
        '<a href="a.zip">A</a><a href="db/d/?b=d_us_txt">US</a>'
        '<a href="x.html">X</a>'
    ) == ["a.zip", "db/d/?b=d_us_txt"]


def test_solve_pow_matches_requested_prefix() -> None:
    nonce = solve_pow("token", 2)
    assert hashlib.sha256(f"token{nonce}".encode()).hexdigest().startswith("00")
