import pytest

from scripts.record_borrow_fees import parse_header

SAMPLE = """#BOF|2026.10.09|22:47:23
#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|
AAPL|USD|APPLE INC|265598|US0378331005|3.6300|0.2500|>10000000|BBG000B9XRY4|
GME|USD|GAMESTOP CORP|36285627|US36467W1099|-8.1000|12.0000|150000|BBG000BB5BF6|
#EOF|2
"""


def test_parse_header_counts_data_rows():
    stamp, rows = parse_header(SAMPLE)
    assert stamp == "2026.10.09|22:47:23"
    assert rows == 2


def test_parse_header_rejects_non_ibkr_text():
    with pytest.raises(ValueError):
        parse_header("<html>error</html>")
