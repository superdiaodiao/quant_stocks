import json

import pandas as pd

from scripts.stockanalysis_price_gap_selection_impact import (
    CONTINUOUS_BRIDGE_ASSESSMENT,
    _bridge_eligibility,
    active_bridges_for_signal,
    apply_bridges_for_signal,
    bridge_observations,
    write_selection_impact_outputs,
)


def _bridge(ticker: str, source_end: str = "2025-07-11") -> dict:
    return {
        "ticker": ticker,
        "source": pd.DataFrame(
            {
                "date": pd.to_datetime(["2025-07-09", "2025-07-10", "2025-07-11"]),
                "close": [10.0, 11.0, 12.0],
                "volume": [100.0, 200.0, 300.0],
            }
        ),
        "last_local_price_date": pd.Timestamp("2025-07-09"),
        "source_end_date": pd.Timestamp(source_end),
        "price_scale": 2.0,
        "volume_scale": 3.0,
    }


def test_active_bridges_only_cover_their_continuous_window():
    bridges = {"AAA": _bridge("AAA")}

    assert active_bridges_for_signal(bridges, "2025-07-09") == {}
    assert set(active_bridges_for_signal(bridges, "2025-07-10")) == {"AAA"}
    assert active_bridges_for_signal(bridges, "2025-07-14") == {}


def test_bridge_eligibility_requires_triage_sha_continuity_and_overlap():
    triage_record = {
        "status": "RESEARCH_LEAD_ONLY",
        "assessment": CONTINUOUS_BRIDGE_ASSESSMENT,
        "cache_payload_sha256": "verified",
    }
    envelope = {"payload_sha256": "verified"}
    coverage = {"source_bridges_from_local_to_source_end": True}
    overlap = {
        "overlap_sessions": 20,
        "price_ratio_within_tolerance_fraction": 1.0,
        "volume_ratio_within_tolerance_fraction": 1.0,
    }

    assert _bridge_eligibility(
        triage_record=triage_record,
        envelope=envelope,
        coverage=coverage,
        overlap=overlap,
        minimum_overlap_sessions=20,
    ) is None
    assert _bridge_eligibility(
        triage_record={**triage_record, "cache_payload_sha256": "stale"},
        envelope=envelope,
        coverage=coverage,
        overlap=overlap,
        minimum_overlap_sessions=20,
    ) == "TRIAGE_CACHE_SHA_MISMATCH"
    assert _bridge_eligibility(
        triage_record=triage_record,
        envelope=envelope,
        coverage={"source_bridges_from_local_to_source_end": False},
        overlap=overlap,
        minimum_overlap_sessions=20,
    ) == "SOURCE_NOT_CONTINUOUS_FROM_FORMAL_PRICE"


def test_apply_bridges_for_signal_is_in_memory_and_pit_limited():
    index = pd.to_datetime(["2025-07-09", "2025-07-10", "2025-07-11", "2025-07-14"])
    close = pd.DataFrame({"AAA": [10.0, float("nan"), float("nan"), float("nan")]}, index=index)
    dollar_volume = pd.DataFrame({"AAA": [1000.0, float("nan"), float("nan"), float("nan")]}, index=index)
    original_close = close.copy(deep=True)
    original_dollar_volume = dollar_volume.copy(deep=True)

    overlay_close, overlay_dollar_volume, applied = apply_bridges_for_signal(
        close,
        dollar_volume,
        {"AAA": _bridge("AAA")},
        "2025-07-10",
    )

    assert applied == {"AAA": 1}
    assert overlay_close.at[pd.Timestamp("2025-07-09"), "AAA"] == 10.0
    assert overlay_close.at[pd.Timestamp("2025-07-10"), "AAA"] == 22.0
    assert pd.isna(overlay_close.at[pd.Timestamp("2025-07-11"), "AAA"])
    assert overlay_dollar_volume.at[pd.Timestamp("2025-07-10"), "AAA"] == 13200.0
    pd.testing.assert_frame_equal(close, original_close)
    pd.testing.assert_frame_equal(dollar_volume, original_dollar_volume)


def test_bridge_observations_include_non_candidates():
    scores = pd.DataFrame({"score": [3.0]}, index=["OTHER"])

    observations = bridge_observations(
        pd.Timestamp("2025-07-31"),
        scores,
        scores,
        {"AAA": _bridge("AAA")},
        {"AAA": 16},
        top_n=3,
        risk_on=True,
    )

    assert observations.loc[0, "ticker"] == "AAA"
    assert observations.loc[0, "applied_source_rows"] == 16
    assert not bool(observations.loc[0, "baseline_candidate"])
    assert not bool(observations.loc[0, "bridge_candidate"])


def test_research_output_writer_never_targets_formal_price_paths(tmp_path):
    summary_path = tmp_path / "research" / "summary.json"
    signals_path = tmp_path / "research" / "signals.csv"
    details_path = tmp_path / "research" / "details.csv"
    formal_price = tmp_path / "formal_price.csv"
    formal_price.write_text("unchanged", encoding="utf-8")

    write_selection_impact_outputs(
        pd.DataFrame([{"signal_date": "2025-07-31", "raw_top3_changed": False}]),
        pd.DataFrame([{"ticker": "AAA"}]),
        pd.DataFrame([{"ticker": "AAA", "baseline_candidate": False}]),
        {"research_only": True, "warning": "no formal imports"},
        summary_path=summary_path,
        signals_path=signals_path,
        details_path=details_path,
    )

    assert json.loads(summary_path.read_text(encoding="utf-8"))["research_only"] is True
    assert signals_path.exists()
    assert details_path.exists()
    assert formal_price.read_text(encoding="utf-8") == "unchanged"
