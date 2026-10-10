"""Point-in-time EPS from SEC companyfacts (the CAN SLIM C / A inputs, also used by O'Neil, stops, fundamentals and
the S-MISP forward signal).

Extracted unchanged from scripts/research_canslim_dev.py (``CF_DIRS``, ``_cf_path``, ``fetch_companyfacts``,
``extract_eps_facts``, ``eps_states``, ``eps_asof``, ``attach_eps``, ``build_eps_states``). The companyfacts copies
are searched in ``quant.data.market_cap.COMPANYFACTS_DIRS`` order; ``build_eps_states`` caches per data version.
"""
from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data import version as dv
from quant.data.market_cap import COMPANYFACTS_DIRS
from quant.paths import CACHE_ROOT, ROOT

INPUTS = dv.INPUTS
CF_DIRS = COMPANYFACTS_DIRS
CF_FETCH_DIR = CF_DIRS[0]
EPS_CACHE = dv.versioned(CACHE_ROOT / "canslim_dev")   # derived from INPUTS earnings_events: per version
EPS_CONCEPTS = ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic")


def _cf_path(cik: int) -> Path | None:
    for d in CF_DIRS:
        p = d / f"CIK{int(cik):010d}.json.gz"
        if p.is_file():
            return p
    return None


def fetch_companyfacts(ciks, max_per_second: float = 3.0) -> dict:
    """Fetch SEC companyfacts for CIKs not cached anywhere (<= 3 requests/s, User-Agent from sec_contact)."""
    import requests
    from src.io.sec_contact import sec_user_agent
    CF_FETCH_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": sec_user_agent(ROOT), "Accept-Encoding": "gzip, deflate"}
    missing = [c for c in sorted(set(int(c) for c in ciks)) if _cf_path(c) is None]
    log = {"requested": len(missing), "ok": 0, "not_found": [], "error": {}}
    last = 0.0
    for c in missing:
        wait = 1.0 / max_per_second - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.time()
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{c:010d}.json"
        try:
            r = requests.get(url, headers=headers, timeout=60)
        except Exception as exc:  # noqa: BLE001
            log["error"][c] = str(exc)[:200]
            continue
        if r.status_code == 404:
            log["not_found"].append(c)
            continue
        if r.status_code != 200:
            log["error"][c] = f"http {r.status_code}"
            continue
        env = {"cik": c, "fetched_at": pd.Timestamp.now("UTC").isoformat(), "source_url": url, "payload": r.json()}
        (CF_FETCH_DIR / f"CIK{c:010d}.json.gz").write_bytes(gzip.compress(json.dumps(env).encode(), 6))
        log["ok"] += 1
    return log


def extract_eps_facts(cik: int) -> pd.DataFrame:
    """All EPS facts (USD/shares) of one CIK: start, end, val, filed, form, priority (0 = diluted)."""
    p = _cf_path(cik)
    cols = ["cik", "concept", "priority", "start", "end", "val", "filed", "form", "accn"]
    if p is None:
        return pd.DataFrame(columns=cols)
    env = json.loads(gzip.decompress(p.read_bytes()))
    payload = env.get("payload", env)
    gaap = payload.get("facts", {}).get("us-gaap", {})
    rows = []
    for pri, concept in enumerate(EPS_CONCEPTS):
        for unit, facts in gaap.get(concept, {}).get("units", {}).items():
            if unit != "USD/shares":
                continue
            for f in facts:
                if "start" not in f or f.get("val") is None:
                    continue
                rows.append((int(cik), concept, pri, f["start"], f["end"], float(f["val"]), f["filed"],
                             f.get("form", ""), f.get("accn", "")))
    return pd.DataFrame(rows, columns=cols)


def _days(a: str, b: str) -> int:
    return (pd.Timestamp(b) - pd.Timestamp(a)).days


def eps_states(facts: pd.DataFrame, d0_by_qend: dict | None = None) -> pd.DataFrame:
    """Point-in-time EPS state of one company after each filing date.

    At filing date f only facts with ``filed <= f`` are known; for each period the most recently filed value wins
    (so later restatements, e.g. for stock splits, replace earlier ones from their filing date on). The state is
    usable for signal dates strictly after ``avail`` (= f, or the earnings-release session for a new quarter when
    ``d0_by_qend`` is given and the release came first).
    """
    out_cols = ["cik", "filed", "avail", "q_end", "q_eps", "q_ya_end", "q_ya_eps", "c_growth", "q_derived",
                "fy0_end", "fy0", "fy1", "fy2", "fy3", "a_up3", "a_cagr3"]
    if facts.empty:
        return pd.DataFrame(columns=out_cols)
    f = facts.copy()
    f["dur"] = (pd.to_datetime(f["end"]) - pd.to_datetime(f["start"])).dt.days
    f["cls"] = np.select([f["dur"].between(75, 105), f["dur"].between(250, 290), f["dur"].between(340, 390)],
                         ["Q", "Y9", "FY"], "")
    f = f[f["cls"] != ""].sort_values(["filed", "priority"], ascending=[True, False])
    if f.empty:
        return pd.DataFrame(columns=out_cols)
    cik = int(f["cik"].iloc[0])
    known: dict = {}          # (cls, end) -> (start, val)
    rows = []
    prev_qend, prev_avail = None, None
    for filed, g in f.groupby("filed", sort=True):
        for r in g.itertuples():          # priority descending inside a date, so diluted (0) is written last
            known[(r.cls, r.end)] = (r.start, r.val)
        q = {e: v for (c, e), (s, v) in known.items() if c == "Q"}
        derived = set()
        fys = sorted(((e, s, v) for (c, e), (s, v) in known.items() if c == "FY"), reverse=True)
        for e, s, v in fys:
            if any(abs(_days(e, qe)) <= 7 for qe in q):
                continue
            y9 = [(ye, yv) for (c, ye), (ys, yv) in known.items()
                  if c == "Y9" and abs(_days(ys, s)) <= 10 and 80 <= _days(ye, e) <= 105]
            if y9:
                q[e] = v - y9[0][1]
                derived.add(e)
                continue
            three = [qv for qe, qv in q.items() if qe not in derived and _days(s, qe) >= 75 and _days(qe, e) >= 75]
            if len(three) == 3:
                q[e] = v - sum(three)
                derived.add(e)
        state = {"cik": cik, "filed": filed}
        if q:
            qe = max(q)
            ya = [e for e in q if 350 <= _days(e, qe) <= 380]
            state.update(q_end=qe, q_eps=q[qe], q_derived=qe in derived)
            if ya:
                ye = max(ya)
                state.update(q_ya_end=ye, q_ya_eps=q[ye])
                state["c_growth"] = (q[qe] - q[ye]) / abs(q[ye]) if q[ye] > 0 else np.nan
        chain = []
        for e, s, v in fys:
            if not chain or 350 <= _days(e, chain[-1][0]) <= 380:
                chain.append((e, v))
            if len(chain) == 4:
                break
        if chain:
            state["fy0_end"] = chain[0][0]
            for i, (e, v) in enumerate(chain):
                state[f"fy{i}"] = v
        if len(chain) == 4:
            v = [c[1] for c in chain]
            state["a_up3"] = bool(v[3] > 0 and v[2] > v[3] and v[1] > v[2] and v[0] > v[1])
            state["a_cagr3"] = (v[0] / v[3]) ** (1 / 3) - 1 if v[3] > 0 and v[0] > 0 else np.nan
        avail = filed
        qe = state.get("q_end")
        if d0_by_qend and qe is not None and qe != prev_qend:
            for k_end, d0 in d0_by_qend.items():
                if abs(_days(k_end, qe)) <= 10 and qe < d0 < filed:
                    avail = d0
                    break
        if prev_avail is not None and avail < prev_avail:
            avail = prev_avail
        state["avail"] = avail
        prev_qend, prev_avail = qe, avail
        rows.append(state)
    out = pd.DataFrame(rows)
    for c in out_cols:
        if c not in out:
            out[c] = np.nan
    return out[out_cols]


def eps_asof(states: pd.DataFrame, cik: int, t: str) -> dict | None:
    """The state of one CIK usable at signal date t: latest with avail < t (strictly)."""
    s = states[(states["cik"] == cik) & (states["avail"].astype(str) < str(t))]
    if s.empty:
        return None
    return s.sort_values(["avail", "filed"]).iloc[-1].to_dict()


def build_eps_states(ciks, timing: str = "filed") -> pd.DataFrame:
    cache = EPS_CACHE / f"eps_states_{timing}.csv.gz"
    if cache.is_file():
        return pd.read_csv(cache, dtype={"filed": str, "avail": str, "q_end": str, "q_ya_end": str, "fy0_end": str})
    d0 = {}
    if timing == "d0":
        ev = pd.read_csv(INPUTS / "earnings_events.csv", dtype=str,
                         usecols=["cik", "event_kind", "d0_session", "fiscal_quarter_end"])
        ev = ev[(ev["event_kind"] == "results_release") & ev["fiscal_quarter_end"].notna() & ev["d0_session"].notna()]
        for c, g in ev.groupby("cik"):
            d0[int(c)] = dict(zip(g["fiscal_quarter_end"], g["d0_session"]))
    parts = []
    for c in sorted(set(int(x) for x in ciks)):
        parts.append(eps_states(extract_eps_facts(c), d0.get(c) if timing == "d0" else None))
    out = pd.concat([p for p in parts if len(p)], ignore_index=True)
    EPS_CACHE.mkdir(parents=True, exist_ok=True)
    out.to_csv(cache, index=False)
    return out


def attach_eps(feat: pd.DataFrame, states: pd.DataFrame, prefix: str = "") -> pd.DataFrame:
    """As-of merge: for each (week_end, cik) the latest state with avail < week_end."""
    st = states.copy()
    st["avail_ts"] = pd.to_datetime(st["avail"]) + pd.Timedelta(days=1)    # usable strictly after avail
    st = st.sort_values(["avail_ts", "filed"])
    left = feat.copy()
    left["cik_i"] = pd.to_numeric(left["cik"], errors="coerce")
    left = left[left["cik_i"].notna()].copy()
    left["cik_i"] = left["cik_i"].astype(int)
    st["cik_i"] = st["cik"].astype(int)
    left = left.sort_values("week_end")
    cols = ["q_end", "q_eps", "q_ya_eps", "c_growth", "fy0_end", "a_up3", "a_cagr3", "avail", "filed"]
    m = pd.merge_asof(left, st[["avail_ts", "cik_i"] + cols].rename(columns={c: prefix + c for c in cols}),
                      left_on="week_end", right_on="avail_ts", by="cik_i", direction="backward")
    out = feat.merge(m[["week_end", "security_id"] + [prefix + c for c in cols]], on=["week_end", "security_id"],
                     how="left")
    return out
