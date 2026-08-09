import hashlib
import json
from pathlib import Path

import pandas as pd

from src.conf import PROJECT_PATH


EVIDENCE = Path(PROJECT_PATH) / (
    "output/data_provenance/"
    "companyfacts_layered_candidate_sensitivity_2026-08-09.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_layered_candidate_is_bound_and_excludes_unproven_fallbacks() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    layer = evidence["proven_fallback_layer"]
    report_path = Path(PROJECT_PATH) / layer["report_path"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    quarterly_path = Path(PROJECT_PATH) / layer["quarterly_path"]

    assert evidence["research_only"] is True
    assert evidence["release_status"] == "BLOCKED"
    assert evidence["formal_outputs_written"] is False
    assert _sha256(report_path) == layer["report_sha256"]
    assert _sha256(quarterly_path) == layer["quarterly_sha256"]
    assert report["restored_row_count"] == layer["restored_proven_rows"] == 8
    assert layer["unproven_rows_not_restored"] == 41

    quarterly = pd.read_csv(quarterly_path)
    assert len(quarterly) == layer["quarterly_rows"] == 319508
    keys = {
        (
            str(row.ticker),
            str(row.fiscal_end)[:10],
            str(row.available_date)[:10],
            str(row.metric),
            str(row.accession),
        )
        for row in quarterly.itertuples(index=False)
    }
    for row in report["restored_rows"]:
        assert (
            row["ticker"],
            row["fiscal_end"],
            row["available_date"],
            row["metric"],
            row["accession"],
        ) in keys


def test_layered_candidate_sensitivity_and_current_price_completion_are_explicit() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    sensitivity = evidence["in_memory_fixed_parameter_sensitivity_2021_2026"]
    price = evidence["historical_price_completion"]

    assert sensitivity["wins_vs_nasdaq"] == 5
    assert sensitivity["failed_years"] == [2023]
    assert sensitivity["annual_results"]["2025"]["strategy"] > (
        sensitivity["annual_results"]["2025"]["nasdaq"]
    )
    assert price["listed_price_histories_complete"] is True
    assert price["observed_terminal_returns"] == 204
    assert price["unresolved_terminal_returns"] == 125
    for prefix in ("bcan_fmto", "bcan_fmtof_muln_bini", "bgxx_bhil"):
        path = Path(PROJECT_PATH) / price[f"{prefix}_report_path"]
        assert _sha256(path) == price[f"{prefix}_report_sha256"]

    for ticker in ("BCAN", "MULN", "BGXX", "BHIL"):
        frame = pd.read_csv(
            Path(PROJECT_PATH) / f"cleaned_stocks_data/price/{ticker.lower()}.csv"
        )
        assert str(frame.iloc[-1]["date"])[:10] == "2026-07-17"
