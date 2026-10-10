"""Offline tests for the round-9 Form 25 changes (plan step 3): hand classifications into the plan's four
classes, the builder's own SEC limiter and the scratch-build redirect."""
from pipelines.reversal_data import form25 as f25

PLAN_CLASSES = {"common_delisting", "other_class", "reorg", "transfer"}


def test_every_hand_classification_is_a_plan_class_with_its_filing():
    assert len(f25.HAND_CLASSIFICATIONS) == 22
    for accession, hand in f25.HAND_CLASSIFICATIONS.items():
        assert hand["classification"] in PLAN_CLASSES, accession
        assert hand["subject_exit"] in (None, "Y", "N"), accession
        assert hand["url"].startswith("https://www.sec.gov/Archives/edgar/data/"), accession
        assert hand["evidence"] and isinstance(hand["successor"], bool), accession
        # a delisting never keeps a successor link; only a reorganisation does
        assert not (hand["classification"] == "common_delisting" and hand["successor"]), accession


def test_holding_company_reorganisations_keep_the_link_and_mergers_lose_it():
    note = "substituted_merger_or_exchange; ticker GOOG GOOGL passed at the next snapshot to new Nasdaq registrant"
    label, text, keep, forced = f25.apply_hand_classification("0001354457-15-000182", "reorg_review", note, True)
    assert (label, keep, forced) == ("reorg", True, None)
    assert text.startswith("hand: holding-company reorganisation") and "rule said reorg_review" in text
    # Globe Specialty Metals into Ferroglobe: a combination led by FerroAtlantica, no successor link.
    label, text, keep, forced = f25.apply_hand_classification("0001354457-15-000251", "reorg_review", note, True)
    assert (label, keep, forced) == ("common_delisting", False, None)
    # Liberty Media's 2023-08-03 reclassification: same CIK; the Braves link the rule attached is dropped.
    label, _, keep, _ = f25.apply_hand_classification("0001354457-23-000564", "reorg_review", note, True)
    assert (label, keep) == ("reorg", False)


def test_shiftpixy_withdrawal_before_listing_ends_nothing():
    label, text, keep, forced = f25.apply_hand_classification(
        "0001477932-17-000756", "unlisted_withdrawal", "issuer_withdrawal; no Nasdaq snapshot row", False)
    assert (label, keep, forced) == ("common_delisting", False, "N")
    assert "before any share traded" in text


def test_other_filings_keep_the_rule_classification():
    assert f25.apply_hand_classification("0000000000-00-000000", "transfer", "x", False) == ("transfer", "x", False, None)


def test_the_builder_has_its_own_three_a_second_limiter():
    assert f25.SEC_PER_SECOND == 3 and f25.SEC_LIMITER.windows == {1: 3}
    assert f25.SEC_LIMITER is not f25.common.SEC_LIMITER


def test_redirect_moves_outputs_and_reads_a_scratch_master(tmp_path, monkeypatch):
    for name in ("OUTPUT", "COMPARISON", "DERIVED", "RAW_ROWS", "SNAPSHOT_DATES"):
        monkeypatch.setattr(f25, name, getattr(f25, name))
    f25.redirect(tmp_path / "out", tmp_path / "master")
    assert f25.OUTPUT == tmp_path / "out" / "form25_nasdaq_2012_2026.csv"
    assert f25.DERIVED == tmp_path / "out" / "derived"
    assert f25.COMPARISON == tmp_path / "out" / "derived" / "form25_vs_sue_lt_2020_2026.json"
    assert f25.RAW_ROWS == tmp_path / "master" / "ticker_rows_raw.csv.gz"
    assert f25.SNAPSHOT_DATES == tmp_path / "master" / "snapshot_dates.json"
