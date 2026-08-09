from scripts.sec_filing_dataset_supplement_candidate import _identity


def test_semantic_identity_normalizes_dates_and_values():
    columns = [
        "ticker", "fiscal_end", "available_date", "metric", "value", "accession"
    ]
    left = {
        "ticker": "LE",
        "fiscal_end": "2020-01-31",
        "available_date": "2020-03-23",
        "metric": "net_income",
        "value": 25_516_000,
        "accession": "a",
    }
    right = {**left, "value": 25_516_000.0}
    assert _identity(left, columns) == _identity(right, columns)
