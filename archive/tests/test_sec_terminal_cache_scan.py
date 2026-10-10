import gzip
import json

from scripts.sec_terminal_cache_scan import scan


def test_scan_finds_fixed_common_cash_and_excludes_cvr(tmp_path) -> None:
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"unresolved_terminal_return_histories": [
        {"ticker": "CASH"}, {"ticker": "CVRX"}, {"ticker": "ZERO"}
    ]}), encoding="utf-8")
    cache = tmp_path / "cache"
    cache.mkdir()
    samples = {
        "cash_x.json.gz": (
            "https://sec/cash",
            b"Each outstanding share of Company common stock was converted into "
            b"the right to receive an amount in cash equal to $12.50 per share."
        ),
        "cvrx_x.json.gz": (
            "https://sec/cvr",
            b"Each outstanding share of Company common stock was converted into "
            b"the right to receive $9.00 per share in cash plus one contingent CVR."
        ),
        "zero_x.json.gz": (
            "https://sec/zero",
            b"The plan became effective. Holders of existing common stock "
            b"will receive no distribution or recovery under the plan."
        ),
    }
    for name, (url, payload) in samples.items():
        envelope = {
            "source_url": url,
            "payload_sha256": "sha-" + name,
            "payload_hex": payload.hex(),
        }
        with gzip.open(cache / name, "wt", encoding="utf-8") as handle:
            json.dump(envelope, handle)
    rows = scan(cache, audit)
    assert [(row["ticker"], row["status"]) for row in rows] == [
        ("CASH", "REVIEW_FIXED_COMMON_SHARE_CASH"),
        ("CVRX", "REVIEW_EXCLUDED_COMPLEX_TERM"),
        ("ZERO", "REVIEW_ZERO_COMMON_EQUITY_DISTRIBUTION"),
    ]
