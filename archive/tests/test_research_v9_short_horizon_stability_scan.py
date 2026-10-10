from scripts.research_v9_short_horizon_stability_scan import admissible


def test_admissible_requires_return_risk_and_benchmark_constraints():
    metrics = {
        "wins_vs_nasdaq": 5,
        "wins_vs_qqq": 4,
        "cagr": 0.20,
        "maximum_drawdown": -0.25,
    }
    assert admissible(metrics)
    for key, bad in (
        ("wins_vs_nasdaq", 4),
        ("wins_vs_qqq", 3),
        ("cagr", 0.17),
        ("maximum_drawdown", -0.31),
    ):
        changed = dict(metrics)
        changed[key] = bad
        assert not admissible(changed)
