import pandas as pd

from scripts.research_v14_integrate_foreign_quarters import eligible_symbols


def test_eligible_symbols_requires_diagnostic_pass() -> None:
    frame = pd.DataFrame([
        {"ticker": "B", "eligible_for_parser_research": False},
        {"ticker": "A", "eligible_for_parser_research": True},
        {"ticker": "A", "eligible_for_parser_research": True},
    ])
    assert eligible_symbols(frame) == ["A"]
