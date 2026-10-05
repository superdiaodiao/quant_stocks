import importlib.util
from pathlib import Path

import pandas as pd
import pytest

spec = importlib.util.spec_from_file_location(
    "audit_data_v1_qc", Path(__file__).resolve().parents[1] / "scripts/audit_data_v1_qc.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

WEEKS = ["2012-01-06", "2012-01-13", "2012-01-20"]


def test_week_index_assigns_window_after_previous_week_end():
    idx = mod.week_index(pd.Series(["2012-01-06", "2012-01-09", "2012-01-13", "2012-01-17"]), WEEKS)
    assert list(idx) == [0, 1, 1, 2]


def test_weekly_returns_compound_and_flags():
    panel = pd.DataFrame({
        "security_id": ["a"] * 4 + ["b"] * 2,
        "date": ["2012-01-06", "2012-01-09", "2012-01-10", "2012-01-13", "2012-01-12", "2012-01-13"],
        "tr": [0.0, 0.10, -0.05, 0.02, None, 0.01],
    })
    w = mod.weekly_returns(panel, WEEKS).set_index(["security_id", "wi"])
    assert w.loc[("a", 1), "ret"] == pytest.approx(1.10 * 0.95 * 1.02 - 1)
    assert bool(w.loc[("a", 1), "has_prior"]) is True
    assert bool(w.loc[("b", 1), "any_nan"]) is True       # series start inside the window
    assert bool(w.loc[("b", 1), "has_prior"]) is False


def test_qc_week_is_monday_based():
    # a Thursday week end (Good Friday 2012) and the Friday before it fall in different weeks
    assert mod.qc_week("2012-04-05") == mod.qc_week("2012-04-02") == mod.qc_week("2012-03-30") + 1


def test_join_chunks_orders_numerically():
    stats = {"P10": "c", "P2": "b", "P0": "a ", "PLEN": "x", "U140": "z"}
    assert mod.join_chunks(stats, "P") == "a bc"


def test_parse_token():
    t = mod.parse_token("AAPL.c3.-229.p-250.s7")
    assert (t["ticker"], t["week"], t["qc_tr_bp"], t["qc_pr_bp"], t["qc_split"]) == ("AAPL", 12 * 36 + 3, -229, -250, 7)
    assert mod.stratum(t) == "S"
    f = mod.parse_token("TMUS.1y.1161.p-2641.s0.5.d3406")
    assert (f["qc_split"], f["qc_dist_bp"], f["qc_pr_bp"]) == (0.5, 3406, -2641)
    r = mod.parse_token("MSFT.1.12")
    assert r["qc_pr_bp"] == 12 and mod.stratum(r) == "R"
    d = mod.parse_token("XYZ.2.na.L9")
    assert d["qc_tr_bp"] is None and "qc_last_date" not in d
    e = mod.parse_token("XYZ.2.-55.L3")
    assert e["qc_last_date"] == "2012-01-12" and mod.stratum(e) == "L" and e["wide"] is False
    assert mod.parse_token("XYZ.2.-55.L3.W")["wide"] is True


def test_factor_method_matches_crsp_without_dividend_and_differs_with_one():
    g = pd.DataFrame({"close_raw": [42.64, 26.06], "split_factor": [1.0, 1.0], "div_cash": [0.0, 15.0],
                      "tr": [0.0, (26.06 + 15) / 42.64 - 1]})
    alt = mod.factor_method_returns(g)
    assert alt.iloc[0] == 0.0
    assert alt.iloc[1] == pytest.approx(26.06 / (42.64 - 15) - 1)
