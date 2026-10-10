from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from scripts.research_v14_argx_quarterly_reports import (
    AUDIT_PATH,
    DEFAULT_REGISTRY,
    EXPECTED_AUDIT_SHA256,
    EXPECTED_SOURCE_SHA256,
    _agree,
    _period_columns,
    _row_value,
    _sha256,
    _subtract,
    build_negative_evidence,
    resolve_unrecoverable_observations,
    run,
    validate_audit_binding,
    validate_negative_source_text,
)


def test_argx_original_and_later_comparators_can_validate_same_values() -> None:
    original = {"revenue": 52_264_000.0, "net_income": -70_057_000.0}
    later = {"revenue": 52_264_000.0, "net_income": -70_057_000.0}

    assert _agree(original, later)


def test_argx_period_columns_do_not_mix_three_and_nine_months() -> None:
    table = pd.DataFrame([
        [None, "Three Months Ended", "Three Months Ended", "Nine Months Ended", "Nine Months Ended"],
        [None, 2021, 2021, 2021, 2021],
        ["Collaboration revenue", "$", 857, "$", 471255],
        ["Owners of the parent", "$", "(233,614)", "$", "(170,447)"],
    ])
    columns = _period_columns(table, "Three Months Ended", 2021)
    assert _row_value(table, "Collaboration revenue", columns) == 857_000.0


def test_argx_cumulative_difference_preserves_loss_sign() -> None:
    half = {"revenue": 22_388_000.0, "net_income": -205_637_000.0}
    first = {"revenue": 19_171_000.0, "net_income": -80_046_000.0}
    assert _subtract(half, first) == {
        "revenue": 3_217_000.0,
        "net_income": -125_591_000.0,
    }


def test_argx_currency_boundary_is_not_silently_convertible() -> None:
    eur_annual = {"revenue": 36_425_000.0, "net_income": -528_923_000.0}
    usd_annual = {"revenue": 41_243_000.0, "net_income": -608_455_000.0}
    assert eur_annual != usd_annual


def test_argx_all_sec_sources_are_sha_locked() -> None:
    registry = pd.read_csv(DEFAULT_REGISTRY)
    assert set(registry["source_id"]) == set(EXPECTED_SOURCE_SHA256)
    assert len(EXPECTED_SOURCE_SHA256) == 17
    for row in registry.itertuples(index=False):
        assert _sha256(Path(row.local_path)) == EXPECTED_SOURCE_SHA256[row.source_id]


def test_argx_currency_change_disclosures_are_verbatim_locked() -> None:
    registry = pd.read_csv(DEFAULT_REGISTRY)
    paths = {
        row.source_id: Path(row.local_path)
        for row in registry.itertuples(index=False)
    }
    checks = validate_negative_source_text(paths)
    assert len(checks) == 4
    assert {row["source_id"] for row in checks} == {"2021_q1", "2021_h1"}


def test_argx_current_audit_binds_both_repeated_observations() -> None:
    binding = validate_audit_binding(AUDIT_PATH, EXPECTED_AUDIT_SHA256)
    observations = resolve_unrecoverable_observations()
    assert binding["scenario_count"] == 2
    assert binding["missing_observation_count"] == 2
    assert len(observations) == 2
    assert set(observations["decision"]) == {
        "unrecoverable_pre_signal_usd_ttm_chain_absent"
    }


def test_argx_late_usd_annual_changes_the_invalid_mixed_ttm() -> None:
    observed = {
        "2020_fy": {"revenue": 36_425_000.0, "net_income": -528_923_000.0},
        "2020_q1_usd_comparison": {
            "revenue": 21_139_000.0,
            "net_income": -88_264_000.0,
        },
        "2020_h1_usd_comparison": {
            "revenue": 24_683_000.0,
            "net_income": -226_590_000.0,
        },
        "2021_h1": {"revenue": 470_398_000.0, "net_income": 63_167_000.0},
        "2020_fy_usd_late": {
            "revenue": 41_243_000.0,
            "net_income": -608_455_000.0,
        },
    }
    sources = {
        "2021_fy": SimpleNamespace(
            available_date="2022-03-03",
            accession="0001104659-22-029609",
        )
    }
    operands, rejected = build_negative_evidence(observed, sources)
    assert operands["late_comparator"]["derived_current_ttm_usd"] == {
        "revenue": 486_958_000.0,
        "net_income": -318_698_000.0,
    }
    assert rejected[0]["invalid_mixed_current_ttm"] == {
        "revenue": 482_140_000.0,
        "net_income": -239_166_000.0,
    }
    assert all(row["rejected"] for row in rejected)


def test_argx_manifest_rebuild_is_deterministic(tmp_path: Path) -> None:
    output_dir = tmp_path / "argx"
    first = run(output_dir=output_dir)
    first_bytes = Path(first["manifest"]).read_bytes()
    second = run(output_dir=output_dir)
    assert Path(second["manifest"]).read_bytes() == first_bytes
    assert second["accepted_fact_count"] == 32
    assert second["blocked_observation_count"] == 2
    assert second["formal_financials_modified"] is False
    assert second["outputs"]["rejected_derivations"]["row_count"] == 3
