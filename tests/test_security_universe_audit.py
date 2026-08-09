import pandas as pd

from src.research.security_universe_audit import classify_security_names


def test_security_name_classification_keeps_categories_explicit():
    frame = pd.DataFrame({"Name": [
        "Example Technologies Inc. Common Stock",
        "Example Strategic Total Return Common Stock",
        "Example Holdings L.P. Common Units",
        "Example Acquisition Corp. Class A Common Stock",
        "Example plc Ordinary Shares",
    ]})

    result = classify_security_names(frame)

    assert result.tolist() == [
        "OPERATING_COMMON_EQUITY",
        "POOLED_INVESTMENT",
        "PARTNERSHIP",
        "SPAC_OR_SHELL",
        "FOREIGN_OR_DEPOSITARY",
    ]
