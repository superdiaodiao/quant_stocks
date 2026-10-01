import gzip
import time

from scripts import reversal_data_common as common


def test_keys_are_redacted_from_logged_urls():
    url = "https://data.nasdaq.com/api/v3/datatables/WIKI/PRICES?ticker=A&api_key=SECRET123&x=1"
    assert "SECRET123" not in common.redact(url)
    assert common.redact(url).endswith("api_key=REDACTED&x=1")


def test_env_key_is_read_without_quotes(tmp_path):
    path = tmp_path / ".env.test"
    path.write_text('OTHER=1\nTIINGO_API_KEY="abc"\n')
    assert common.read_env_key(path, "TIINGO_API_KEY") == "abc"


def test_limiter_spaces_requests():
    limiter = common.SlidingWindowLimiter({0.2: 2})
    started = time.monotonic()
    for _ in range(3):
        limiter.wait()
    assert time.monotonic() - started >= 0.19


def test_cached_get_reads_the_cache_without_a_request(tmp_path, monkeypatch):
    cache = tmp_path / "x.json.gz"
    cache.write_bytes(gzip.compress(b'{"a": 1}'))
    monkeypatch.setattr(common, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request")))
    assert common.cached_get("https://example.com/x", cache, source="test") == b'{"a": 1}'


def test_parallel_map_keeps_order_and_returns_errors():
    out = common.parallel_map(lambda x: 10 // x, [1, 2, 0, 5], workers=3)
    assert out[:2] == [10, 5] and isinstance(out[2], ZeroDivisionError) and out[3] == 2


def test_presigned_links_lose_their_whole_query():
    url = "https://bucket.s3.amazonaws.com/f.zip?X-Amz-Credential=AKIA%2F&X-Amz-Signature=abc"
    assert common.redact(url) == "https://bucket.s3.amazonaws.com/f.zip?REDACTED"
