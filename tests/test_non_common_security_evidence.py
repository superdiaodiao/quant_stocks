import gzip
import json
from pathlib import Path

import pandas as pd

from src.conf import PROJECT_PATH


def test_sec_blank_check_evidence_replays_cached_submissions():
    evidence = pd.read_csv(
        Path(PROJECT_PATH) / "stocks_list_dir/nasdaq/non_common_security_evidence.csv"
    )
    assert set(evidence["security_category"]) == {"SEC_BLANK_CHECK"}
    for row in evidence.itertuples(index=False):
        cache = Path(PROJECT_PATH) / (
            f"output/data_provenance/sec_submission_triage_cache/CIK{int(row.cik):010d}.json.gz"
        )
        envelope = json.loads(gzip.decompress(cache.read_bytes()))
        payload = envelope["payload"]
        assert envelope["payload_sha256"] == row.payload_sha256
        assert envelope["source_url"] == row.source_url
        assert int(payload["sic"]) == 6770
        assert payload["sicDescription"] == "Blank Checks"
