"""The registered rev-ind-v1 test: the rule, the window and the decision (studies/reversal_test_v1.py)."""
from studies import reversal_test_v1 as rt


def test_the_rule_and_window_are_the_registered_ones():
    assert rt.RULE.name == "dv50_N100_K3_monday_ov30"
    assert (rt.RULE.account, rt.RULE.hold_weeks, rt.RULE.spread_mult, rt.RULE.borrow_rate) == (10_000.0, 1, 1.0, 0.003)
    assert (rt.EVAL_START, rt.TEST_END, rt.PASS_T, rt.DEV_TRIALS) == ("2017-01-01", "2022-12-31", 2.0, 148)
    assert rt.TEST_END < "2023-01-01"          # the 2023-2026 holdout is never read


def test_verdict_needs_every_condition():
    good = {"excess_net_ann": 0.05, "t_stat": 2.1}
    halves = {"2017_2019": {"excess_net_ann": 0.01}, "2020_2022": {"excess_net_ann": 0.02}}
    assert rt.verdict(good, halves)["pass"]
    assert not rt.verdict({**good, "t_stat": 1.99}, halves)["pass"]
    assert not rt.verdict({**good, "excess_net_ann": -0.01}, halves)["pass"]
    assert not rt.verdict(good, {**halves, "2020_2022": {"excess_net_ann": -0.001}})["pass"]
