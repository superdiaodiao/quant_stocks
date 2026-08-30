import pandas as pd

from scripts import research_v30_2019_selection_path_adjudication as v30


def test_meta_identity_is_source_locked_and_effective_on_2022_06_09():
    evidence = v30.META_IDENTITY_EVIDENCE

    assert evidence["historical_ticker"] == "FB"
    assert evidence["provider_ticker"] == "META"
    assert evidence["last_historical_date"] == "2022-06-08"
    assert evidence["current_ticker_first_date"] == "2022-06-09"
    assert evidence["source_url"].startswith("https://www.sec.gov/Archives/")


def test_meta_identity_normalizes_only_historical_fb_membership():
    snapshots = {
        pd.Timestamp("2019-04-30"): {"FB", "A"},
        pd.Timestamp("2022-06-09"): {"META", "B"},
    }

    actual = v30.normalize_meta_identity(snapshots)

    assert actual[pd.Timestamp("2019-04-30")] == {"META", "A"}
    assert actual[pd.Timestamp("2022-06-09")] == {"META", "B"}


def test_gap_signal_classification_covers_all_four_missing_months():
    classified = set(v30.RISK_ON_GAP_SIGNALS) | set(v30.RISK_OFF_GAP_SIGNALS)

    assert classified == {
        pd.Timestamp("2019-04-30"),
        pd.Timestamp("2019-05-31"),
        pd.Timestamp("2019-08-30"),
        pd.Timestamp("2019-09-30"),
    }
    assert set(v30.RISK_ON_GAP_SIGNALS).isdisjoint(v30.RISK_OFF_GAP_SIGNALS)
