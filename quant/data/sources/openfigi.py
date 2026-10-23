"""OpenFIGI's public mapping API (https://api.openfigi.com/v3/mapping), used without a key: CUSIP -> FIGI records
(ticker, exchange code, name, security type). Without a key the API allows 25 requests a minute and 10 identifiers
per request; ``map_cusips`` stays at one request every 3 seconds.

Each CUSIP's answer is cached as its own JSON file (``<cache>/figi_<CUSIP>.json``), so a stopped run resumes without
asking twice; every request is logged to the caller's ``Ledger`` (source ``openfigi``). OpenFIGI answers only for
identifiers it still lists: CUSIPs of companies that were delisted long ago usually come back as "No identifier
found", which the caller maps by hand.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

from quant.data.sources.http import Ledger, SlidingWindowLimiter, atomic_write, log_request

URL = "https://api.openfigi.com/v3/mapping"
BATCH = 10
LIMITER = SlidingWindowLimiter({3: 1, 60: 20})


def cache_path(cache: Path, cusip: str) -> Path:
    return Path(cache) / f"figi_{cusip}.json"


def map_cusips(cusips, cache: Path, ledger: Ledger, log=print) -> dict:
    """CUSIP -> the API's answer for it ({"data": [...]} or {"warning": ...}), from the cache or fetched."""
    out, todo = {}, []
    for c in sorted(set(cusips)):
        p = cache_path(cache, c)
        if p.exists():
            out[c] = json.loads(p.read_text())
        else:
            todo.append(c)
    for k in range(0, len(todo), BATCH):
        batch = todo[k:k + BATCH]
        body = json.dumps([{"idType": "ID_CUSIP", "idValue": c} for c in batch]).encode()
        for attempt in range(4):
            LIMITER.wait()
            try:
                req = Request(URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(req, timeout=60) as r:
                    data = r.read()
                    status = r.status
                log_request(ledger, "openfigi", URL, status, data, None, ",".join(batch))
                break
            except Exception as exc:  # noqa: BLE001  (429 or network: wait and retry)
                log_request(ledger, "openfigi", URL, type(exc).__name__, None, None, ",".join(batch))
                time.sleep(20 * (attempt + 1))
        else:
            raise RuntimeError(f"OpenFIGI failed for {batch}")
        for c, ans in zip(batch, json.loads(data)):
            atomic_write(cache_path(cache, c), json.dumps(ans).encode())
            out[c] = ans
        log(f"openfigi: {min(k + BATCH, len(todo))}/{len(todo)}")
    return out


def us_listing(answer: dict) -> dict | None:
    """The US composite record (exchCode 'US') of an answer, else None."""
    for x in answer.get("data") or []:
        if x.get("exchCode") == "US":
            return x
    return None
