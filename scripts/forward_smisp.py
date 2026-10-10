"""B3 S-MISP short overlay, forward observation (docs/forward_observation_checklist.md section B3; owner decision
2026-10-10). Used by scripts/forward_observation.py. Not QuantConnect. Observed, not traded.

Configuration S-MISP N=10 k=20% MN of docs/research_ledger_short_overlay.md section 0: (1 + k) QQQ (the extra k on
the IBKR margin loan) and a short of the 10 worst names of the mispricing composite at k notional, with the
frozen rules of scripts/research_short_overlay.py. Nothing of the rule is reimplemented here: the composite
(``misp_score``), the borrow tiers (``borrow_rates``), the buffered selection (``loser_orders`` / ``schedule``), the
daily simulation (``simulate``), the monthly returns and the short-leg diagnostic (``basket_diagnostics``) are the
research functions; the U300 panel is built with the research_fundamentals / research_megacap /
research_ml_cross_section functions that built the frozen panel (``facts_from_payload``, ``company_states``,
``asof_states``, ``market_factors``, ``composites``, ``universes``, ``reject_small_mcaps``, ``market_caps``,
``price_feature_frames``).

What this file adds is the fresh, point-in-time data the frozen data v2 (prices to 2026-08-31, universe weeks to
2026-07-17) cannot give (section B3.4 of the checklist lists the remaining gaps):

- listing: the Nasdaq Trader symbol directory ``nasdaqlisted.txt`` (the source of the frozen listing snapshots),
  one snapshot fetched after each signal close; common stock = ``investable_common_equities`` on it;
- security identity: the frozen v2 ``weekly_listed`` last week (2026-07-17) by ticker, then SEC
  ``company_tickers_exchange.json`` (ticker -> CIK) for renames and new listings; new listings are classified from
  their SEC submissions (SIC, foreign filer, SPAC / investment company);
- prices: Alpaca SIP daily bars of every universe-base name (scripts/forward_prices.py: raw / split / all
  adjustments, written as Yahoo-format charts), Yahoo v8 charts for the names Alpaca does not return (one request
  per 2 s, stop at 401/403/429); bars only up to the signal date;
- fundamentals and share counts: SEC companyfacts of the U300 names, fetched after the signal (2 requests/s,
  User-Agent from ``src/io/sec_contact.py``), used only for facts filed strictly before the signal;
- EFFR: the NY Fed CSV (same file format as the research input);
- borrow fees: the daily IBKR snapshots of scripts/record_borrow_fees.py (report only).

Each month's signal (scores and ranks of the U300 names, no price levels) is computed once, right after the signal
close, and frozen in ``state/forward_observation/smisp/s_<date>.csv`` (committed, so a GitHub Actions run can use
it); later runs reuse it. Raw vendor bodies stay in the local cache (``cache_root()``), never in Git.
"""
from __future__ import annotations

import os

os.environ.setdefault("REVERSAL_DATA_VERSION", "v2")   # the frozen panel inputs (security master, lists) are v2

import gzip  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.error import HTTPError  # noqa: E402
from urllib.request import Request, urlopen  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from scripts import forward_prices as fp  # noqa: E402
from scripts import research_canslim_dev as cs  # noqa: E402
from scripts import research_fundamentals as rf  # noqa: E402
from scripts import research_livermore as lv  # noqa: E402
from scripts import research_megacap as mc  # noqa: E402
from scripts import research_ml_cross_section as ml  # noqa: E402
from scripts import research_reversal_dev as rev  # noqa: E402
from scripts import research_selective_t as st  # noqa: E402
from scripts import research_short_overlay as ro  # noqa: E402
from scripts import study_data_version as dv  # noqa: E402
from src.io.security_universe import investable_common_equities  # noqa: E402

NY = ZoneInfo("America/New_York")


def cache_root() -> Path:
    """Local, git-ignored working files (raw vendor bodies): $FORWARD_OBS_CACHE, else the main checkout's
    research_cache/forward_observation, else .cache/forward_observation in this checkout (GitHub Actions)."""
    if os.environ.get("FORWARD_OBS_CACHE"):
        return Path(os.environ["FORWARD_OBS_CACHE"])
    main = st.MAIN / "research_cache"
    return main / "forward_observation" if main.is_dir() else st.ROOT / ".cache/forward_observation"


BASE = cache_root() / "smisp"
STATE = fp.STATE_DIR / "smisp"            # committed: frozen monthly signals (scores, ranks; no price levels)
FROZEN_BASE_STATE = fp.STATE_DIR / "smisp_frozen_base_2026-07-17.csv"
BORROW_DIR = Path(os.environ.get("FORWARD_BORROW_DIR", st.MAIN / "research_cache/borrow_fees"))
LISTED = lv.CACHE / "universe/weekly_listed.csv.gz"
MASTER = lv.INPUTS / "security_master.csv"
TOP300 = lv.INPUTS / "weekly_universe_top300.csv.gz"

CFG = ro.Cfg(signal="S-MISP", n=10, k=0.20, mode="MN")
SIGNAL = CFG.signal
DECISION_CLOSE = "2026-10-09"
FIRST_SIGNAL = "2026-10-30"          # last session of October 2026; executed at the next close (2026-11-02)
LOOKBACK_DAYS = 460                  # price history per signal: 12-1 momentum (252 sessions), share anchors (400 d)
CLOSE_STALE_SESSIONS = 5             # universe builder: week close = last close at most 5 sessions before it
DV_MIN_ROWS = 25                     # universe builder: dv50 needs at least 25 rows
MIN_PRICE = 10.0
UNIVERSE_SIZE = 300
SEC_PER_SECOND = 2.0
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
EFFR_URL = "https://markets.newyorkfed.org/api/rates/unsecured/effr/search.csv?startDate={a}&endDate={b}"
FOREIGN_PERIODIC = ("20-F", "40-F")
DOMESTIC_PERIODIC = ("10-K", "10-Q")
FOREIGN_REGISTRATION = ("F-1", "F-3", "F-4", "F-10")
INVESTMENT_FORMS = ("N-2", "N-54A", "N-8A", "N-CSR", "N-CEN", "N-PORT")
NO_DATA = ("400", "404", "empty")    # Yahoo results meaning "no rows in the window" (e.g. a listing after the signal)


# ======================================================================== small helpers

def ydate(d) -> str:
    return pd.Timestamp(d).strftime("%Y-%m-%d")


def sdir_of(s, base: Path = BASE) -> Path:
    return base / f"s_{ydate(s)}"


def yahoo_symbol(sym: str) -> str:
    return str(sym).strip().replace(".", "-").replace("/", "-")


def ibkr_symbol(sym: str) -> str:
    """IBKR's short-stock file writes class separators as a space (BRK B)."""
    return re.sub(r"[.\-/]", " ", str(sym).strip().upper())


def base_form(form: str) -> str:
    return str(form).split("/")[0].strip()


def default_getter(url: str, headers: dict, timeout: int = 60) -> bytes:
    with urlopen(Request(url, headers=headers), timeout=timeout) as resp:
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data


def sec_headers() -> dict:
    from src.io.sec_contact import sec_user_agent
    return {"User-Agent": sec_user_agent(st.ROOT), "Accept-Encoding": "gzip"}


def signal_dates(sessions: pd.DatetimeIndex, first: str = FIRST_SIGNAL) -> list:
    """Forward signals: the last session of each month from ``first`` on (``research_megacap.signal_sessions``:
    a month counts once the data hold a later session)."""
    return [d for d in mc.signal_sessions(sessions) if d >= pd.Timestamp(first)]


def week_end_on_or_before(s, sessions: pd.DatetimeIndex) -> pd.Timestamp:
    """The universe builder's week end: the last session of a calendar week, latest one <= s. ``sessions`` may hold
    sessions after s (dates only); when it ends at s, s counts as a week end only on a Friday."""
    s = pd.Timestamp(s)
    ses = sessions[sessions <= s]
    wk = ses.to_period("W-SUN")
    later = sessions[sessions > s]
    s_is_end = (later[0].to_period("W-SUN") != s.to_period("W-SUN")) if len(later) else s.weekday() == 4
    if s_is_end:
        return s
    prev = ses[wk < s.to_period("W-SUN")]
    return prev[-1]


# ======================================================================== fetchers (polite, resumable)

def fetch_listing(base: Path = BASE, getter=None, now: datetime | None = None) -> Path:
    """One Nasdaq Trader symbol-directory snapshot, saved as listings/nasdaqlisted_<UTC stamp>.txt."""
    now = now or datetime.now(timezone.utc)
    out = base / "listings" / f"nasdaqlisted_{now.strftime('%Y%m%dT%H%M%SZ')}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = (getter or default_getter)(NASDAQ_LISTED_URL, {"User-Agent": "Mozilla/5.0"})
    text = data.decode("utf-8", errors="replace")
    if not text.startswith("Symbol|Security Name") or text.count("\n") < 1000:
        raise RuntimeError("nasdaqlisted.txt: unexpected content")
    out.write_text(text)
    return out


def listing_for_signal(s, base: Path = BASE) -> tuple[Path | None, str]:
    """The first snapshot fetched after the signal close (16:00 New York); else the latest earlier one (flagged)."""
    files = sorted((base / "listings").glob("nasdaqlisted_*.txt"))
    close_utc = pd.Timestamp(ydate(s) + " 16:00", tz=NY).tz_convert("UTC")
    stamps = [(pd.Timestamp(datetime.strptime(f.stem.split("_")[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)), f)
              for f in files]
    after = [(t, f) for t, f in stamps if t >= close_utc]
    if after:
        return after[0][1], "after_signal"
    before = [(t, f) for t, f in stamps if t < close_utc]
    return (before[-1][1], "before_signal") if before else (None, "none")


def parse_listing(text: str) -> pd.DataFrame:
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("File Creation Time")]
    df = pd.read_csv(io.StringIO("\n".join(lines)), sep="|", dtype=str, keep_default_na=False)
    df = df.rename(columns={"Security Name": "Name"})
    df["Symbol"] = df["Symbol"].str.strip().str.upper()
    return df


def fetch_json(url: str, path: Path, headers: dict, getter=None, sleep=time.sleep, wait: float = 0.0) -> dict | None:
    """GET ``url`` once (cached at ``path``, gzip). None on HTTP 404."""
    if path.exists():
        return json.loads(gzip.decompress(path.read_bytes()))
    if wait:
        sleep(wait)
    try:
        data = (getter or default_getter)(url, headers)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(data, mtime=0))
    tmp.rename(path)
    return json.loads(data)


def sec_ticker_map(sdir: Path, getter=None) -> dict:
    """Ticker -> CIK from SEC company_tickers_exchange.json (saved once per signal)."""
    j = fetch_json(SEC_TICKERS_URL, sdir / "sec_company_tickers_exchange.json.gz", sec_headers(), getter)
    f = j["fields"]
    i_c, i_t = f.index("cik"), f.index("ticker")
    out = {}
    for row in j["data"]:
        t = str(row[i_t] or "").upper()
        if t and t not in out:
            out[t] = int(row[i_c])
    return out


def fetch_sec_many(ciks, sdir: Path, kind: str, getter=None, sleep=time.sleep) -> dict:
    """SEC submissions or companyfacts for ``ciks`` (<= 2 requests/s), cached in the signal folder. A CIK SEC does
    not know (404) maps to None; any other HTTP error raises (the signal is then not computed; re-run resumes)."""
    url = SEC_SUBMISSIONS_URL if kind == "submissions" else SEC_FACTS_URL
    out, headers = {}, sec_headers()
    last = 0.0
    for c in sorted({int(c) for c in ciks}):
        path = sdir / kind / f"CIK{c:010d}.json.gz"
        if not path.exists():
            wait = max(0.0, 1.0 / SEC_PER_SECOND - (time.time() - last))
            last = time.time() + wait
            try:
                out[c] = fetch_json(url.format(cik=c), path, headers, getter, sleep, wait)
            except HTTPError as exc:       # 404 is returned as None by fetch_json; anything else stops the signal
                raise RuntimeError(f"SEC {kind} CIK {c}: HTTP {exc.code}; stopped (re-run resumes)") from exc
            continue
        out[c] = json.loads(gzip.decompress(path.read_bytes()))
    return out


def load_sec(sdir: Path, kind: str) -> dict:
    out = {}
    for p in sorted((sdir / kind).glob("CIK*.json.gz")):
        out[int(p.name[3:13])] = json.loads(gzip.decompress(p.read_bytes()))
    return out


def fetch_yahoo(symbols, out_dir: Path, period1: str, period2: str, getter=None, sleep=time.sleep) -> dict:
    """Yahoo v8 daily charts (research_selective_t URL / headers / stop codes), at most one request every 2 s,
    resumable: symbols with a file, or logged as having no data in the window (HTTP 400 "data doesn't exist",
    404, empty body), are skipped."""
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "fetch_log.csv"
    done = set()
    if log.exists():
        lg = pd.read_csv(log, dtype=str)
        done = set(lg.loc[lg["result"].isin(NO_DATA), "symbol"])
    else:
        log.write_text("fetched_utc,symbol,result,bytes\n")
    res, last = {}, None
    for sym in symbols:
        path = out_dir / f"{sym}.json.gz"
        if path.exists() or sym in done:
            res[sym] = "cached"
            continue
        if last is not None:
            wait = st.SECONDS_PER_REQUEST - (time.time() - last)
            if wait > 0:
                sleep(wait)
        last = time.time()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        url = st.chart_url(sym, period1, period2)
        try:
            data = getter(url) if getter is not None else default_getter(url, st.HEADERS, 30)
        except HTTPError as exc:
            with log.open("a") as fh:
                fh.write(f"{now},{sym},{exc.code},0\n")
            res[sym] = f"HTTP {exc.code}"
            if exc.code in st.STOP_CODES:
                print(f"yahoo: HTTP {exc.code} on {sym}; stopping (re-run resumes)")
                break
            continue
        except OSError as exc:              # timeouts, resets: retried by the next run
            res[sym] = f"error {type(exc).__name__}"
            continue
        payload = json.loads(data)
        r = (payload.get("chart") or {}).get("result") or [None]
        ok = bool(r[0]) and (r[0].get("meta") or {}).get("dataGranularity") == "1d" and r[0].get("timestamp")
        if ok:
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(data, mtime=0))
            tmp.rename(path)
        res[sym] = "ok" if ok else "empty"
        with log.open("a") as fh:
            fh.write(f"{now},{sym},{res[sym]},{len(data)}\n")
    return res


def fetch_charts(symbols: dict, out_dir: Path, period1: str, period2: str, getters: dict | None = None,
                 sleep=time.sleep, use_alpaca: bool = True) -> dict:
    """Charts for {yahoo symbol: exchange symbol}: Alpaca first (many symbols per request; written as Yahoo-format
    bodies), Yahoo for the symbols Alpaca does not return. Returns {yahoo symbol: source} and keeps a source log."""
    getters = getters or {}
    out_dir.mkdir(parents=True, exist_ok=True)
    src_log = out_dir / "chart_sources.csv"
    src = pd.read_csv(src_log, dtype=str).set_index("symbol")["source"].to_dict() if src_log.exists() else {}
    todo = {y: x for y, x in symbols.items() if not (out_dir / f"{y}.json.gz").exists()}
    headers = fp.alpaca_headers() if use_alpaca else None
    if todo and headers is not None:
        end_alp = ydate(pd.Timestamp(period2) - pd.Timedelta(days=1))
        got = fp.alpaca_charts(list(todo.values()), period1, end_alp, headers, getters.get("alpaca"), sleep)
        for y, x in todo.items():
            if x in got and got[x]["chart"]["result"][0]["timestamp"]:
                fp.save_payload(got[x], out_dir / f"{y}.json.gz")
                src[y] = "alpaca"
    rest = [y for y in symbols if not (out_dir / f"{y}.json.gz").exists()]
    if rest:
        res = fetch_yahoo(rest, out_dir, period1, period2, getters.get("yahoo"), sleep)
        for y, v in res.items():
            if v == "ok":
                src[y] = "yahoo"
    pd.DataFrame({"symbol": list(src), "source": list(src.values())}).to_csv(src_log, index=False)
    return src


def fetch_effr(path: Path, start: str = "2026-06-01", getter=None) -> Path:
    """NY Fed EFFR CSV (same columns as the research input), from ``start`` to today."""
    a = pd.Timestamp(start).strftime("%m/%d/%Y")
    b = datetime.now(timezone.utc).strftime("%m/%d/%Y")
    data = (getter or default_getter)(EFFR_URL.format(a=a, b=b), {"User-Agent": "Mozilla/5.0"})
    text = data.decode()
    if not text.startswith("Effective Date,Rate Type"):
        raise RuntimeError("EFFR CSV: unexpected content")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ======================================================================== prices

def load_chart(path: Path, end: str) -> pd.DataFrame | None:
    """One Yahoo chart -> daily frame (real close, split-adjusted close and volume, total return), rows <= end."""
    payload = json.loads(gzip.decompress(path.read_bytes()))
    try:
        df, splits = st.ohlc_frame(payload, end)
    except Exception:  # noqa: BLE001 - an unparsable body is treated as missing
        return None
    if len(df) < 2:                    # a single bar has no return; it cannot be ranked or held
        return None
    j = payload["chart"]["result"][0]
    ts = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert(NY).strftime("%Y-%m-%d")
    vol = pd.Series(j["indicators"]["quote"][0].get("volume") or [np.nan] * len(ts), index=ts, dtype=float)
    vol = vol[~vol.index.duplicated(keep="last")]
    m = st.market_from_frame(path.name.split(".json")[0], df.copy(), splits)
    out = pd.DataFrame({"close": m.s.close, "close_adj": df["close"].to_numpy(float),
                        "volume": df["date"].map(vol).to_numpy(float), "tr": m.s.tr.to_numpy(float)},
                       index=pd.DatetimeIndex(df["date"]))
    out.loc[out.index[0], "tr"] = np.nan
    if out.index.max() > pd.Timestamp(end):
        raise cs.DateGuardError(f"{path.name}: row after {end}")
    return out


def price_frames(files: dict, grid: pd.DatetimeIndex, end: str) -> dict:
    """{security_id: chart path} -> session x security frames on ``grid`` (the QQQ sessions): real close, split-
    adjusted close and volume, daily total return, total-return index (``research_reversal_dev.make_index``)."""
    cols = {"close": {}, "close_adj": {}, "volume": {}, "tr": {}}
    dropped = {}
    for sid, path in files.items():
        if path is None or not Path(path).exists():
            continue
        f = load_chart(Path(path), end)
        if f is None:
            continue
        extra = f.index.difference(grid)
        if len(extra):
            dropped[sid] = len(extra)
        f = f.reindex(grid)
        for k in cols:
            cols[k][sid] = f[k]
    fr = {k: pd.DataFrame(v, index=grid) for k, v in cols.items()}
    fr["idx"] = rev.make_index(fr["tr"], fr["close"])
    fr["dropped_rows"] = dropped
    return fr


# ======================================================================== universe base

def frozen_base(path: Path = FROZEN_BASE_STATE) -> pd.DataFrame:
    """The frozen v2 universe base at its last week (2026-07-17): one row per listed security with its frozen
    eligibility flags, CIK, SIC (latest top-300 header SIC, else the security master) and multi-class group.
    Read from the committed state file (identity and flags only); built from the v2 inputs when it is missing."""
    if path.exists():
        f = pd.read_csv(path, dtype={"security_id": str, "ticker": str, "cik": str, "sic": str,
                                     "multi_class_group": str, "frozen_week": str})
        for c in ("eligible", "non_common", "spac_shell", "foreign", "investment_company"):
            f[c] = f[c].astype(str) == "True"
        return f
    if not dv.IS_V2:
        raise SystemExit("B3 reads the frozen data v2 inputs: REVERSAL_DATA_VERSION must be v2")
    wl = pd.read_csv(LISTED, dtype=str, usecols=["week_end", "security_id", "ticker", "eligible", "non_common",
                                                 "spac_shell", "foreign", "investment_company"])
    last = wl[wl["week_end"] == wl["week_end"].max()].copy()
    master = pd.read_csv(MASTER, dtype=str, usecols=["security_id", "cik", "sic", "multi_class_group"])
    top = pd.read_csv(TOP300, dtype=str, usecols=["week_end", "security_id", "sic"])
    top = top[top["sic"].notna()].sort_values("week_end").drop_duplicates("security_id", keep="last")
    last = last.merge(master, on="security_id", how="left")
    last["sic"] = last["security_id"].map(top.set_index("security_id")["sic"]).fillna(last["sic"])
    for c in ("eligible", "non_common", "spac_shell", "foreign", "investment_company"):
        last[c] = last[c] == "True"
    last["frozen_week"] = last["week_end"]
    out = last.drop(columns=["week_end"]).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    return out


def classify_submissions(sub: dict | None) -> dict:
    """SIC and foreign / SPAC / investment-company flags of a new listing from its SEC submissions."""
    if not sub:
        return {"sic": np.nan, "foreign": None, "spac": None, "investment": None}
    try:
        sic = float(sub.get("sic") or "nan")
    except ValueError:
        sic = np.nan
    forms = [base_form(f) for f in (sub.get("filings", {}).get("recent", {}).get("form") or [])]
    periodic = [f for f in forms if f in FOREIGN_PERIODIC + DOMESTIC_PERIODIC]
    if periodic:
        foreign = periodic[0] in FOREIGN_PERIODIC       # the latest periodic report decides (recent first)
    elif "6-K" in forms:
        foreign = True
    else:
        reg = [f for f in forms if f in FOREIGN_REGISTRATION or f in ("S-1", "S-3", "S-4", "S-11", "10-12B")]
        foreign = bool(reg) and reg[0] in FOREIGN_REGISTRATION
    return {"sic": sic, "foreign": bool(foreign), "spac": sic == 6770,
            "investment": sic == 6726 or any(f in INVESTMENT_FORMS for f in forms)}


def base_rows(listing: pd.DataFrame, frozen: pd.DataFrame, tickmap: dict) -> pd.DataFrame:
    """Every Nasdaq common-stock symbol of the snapshot with its security id and flags.
    source: 'frozen' (ticker of the frozen last week), 'frozen_cik' (a frozen security under a new ticker, by SEC
    CIK), 'new' (a security not in the frozen base), 'no_cik' (left out: no SEC identity)."""
    common = investable_common_equities(listing)
    # a known security keeps the builder's common-stock flag (a temporary name such as "Common Stock When-Issued"
    # on the same symbol, seen for CEG and SNDK in 2026-10, must not drop it); the name test applies to new symbols
    flags = listing[~listing["Symbol"].isin(common["Symbol"])]
    flags = flags[~flags.get("ETF", pd.Series("N", index=flags.index)).astype(str).str.upper().eq("Y")
                  & ~flags.get("Test Issue", pd.Series("N", index=flags.index)).astype(str).str.upper().eq("Y")]
    known = set(frozen.loc[~frozen["non_common"].astype(bool), "ticker"])
    common = pd.concat([common, flags[flags["Symbol"].isin(known)]]).sort_values("Symbol")
    fz = frozen.sort_values(["eligible", "security_id"], ascending=[False, True])
    by_t = fz.drop_duplicates("ticker").set_index("ticker")
    listed_syms = set(common["Symbol"])
    by_cik = {}
    for r in fz.itertuples():
        if pd.notna(r.cik):
            by_cik.setdefault(int(float(r.cik)), []).append(r)
    frozen_ids = set(fz["security_id"])
    rows = []
    for r in common.itertuples():
        sym = r.Symbol
        base = {"ticker": sym, "yahoo": yahoo_symbol(sym), "name": r.Name}
        if sym in by_t.index:
            f = by_t.loc[sym]
            rows.append({**base, "security_id": f["security_id"], "cik": f["cik"], "sic": f["sic"],
                         "multi_class_group": f["multi_class_group"], "source": "frozen",
                         "foreign": f["foreign"], "investment_company": f["investment_company"],
                         "frozen_spac_shell": f["spac_shell"]})
            continue
        cik = tickmap.get(sym)
        if cik is None:
            rows.append({**base, "security_id": None, "cik": None, "sic": np.nan, "multi_class_group": None,
                         "source": "no_cik", "foreign": None, "investment_company": None, "frozen_spac_shell": False})
            continue
        cand = [f for f in by_cik.get(cik, []) if f.ticker not in listed_syms]
        if len(cand) == 1:
            f = cand[0]
            rows.append({**base, "security_id": f.security_id, "cik": f.cik, "sic": f.sic,
                         "multi_class_group": f.multi_class_group, "source": "frozen_cik", "foreign": f.foreign,
                         "investment_company": f.investment_company, "frozen_spac_shell": f.spac_shell})
            continue
        sid = str(cik) if str(cik) not in frozen_ids else f"{cik}.{sym}"
        rows.append({**base, "security_id": sid, "cik": str(cik), "sic": np.nan, "multi_class_group": None,
                     "source": "new", "foreign": None, "investment_company": None, "frozen_spac_shell": False})
    out = pd.DataFrame(rows)
    out["sic"] = pd.to_numeric(out["sic"], errors="coerce")
    return out


def needs_submissions(base: pd.DataFrame) -> list:
    """CIKs whose SEC submissions are read: new listings, and frozen names that were SPAC shells (SIC 6770) in
    the frozen data (a completed merger changes the SIC)."""
    m = (base["source"] == "new") | base["frozen_spac_shell"].astype(bool) | (base["sic"] == 6770)
    return sorted({int(float(c)) for c in base.loc[m & base["cik"].notna(), "cik"]})


def finish_base(base: pd.DataFrame, subs: dict) -> pd.DataFrame:
    """Apply the SEC classification and the universe-base rule (not foreign, not SPAC shell, not investment company;
    non-common names are already out). Duplicate security ids keep the first symbol."""
    b = base.copy()
    for i, r in b.iterrows():
        if r["cik"] is None or pd.isna(r["cik"]):
            continue
        c = int(float(r["cik"]))
        if c not in subs:
            continue
        k = classify_submissions(subs[c])
        if np.isfinite(k["sic"]):
            b.at[i, "sic"] = k["sic"]
        if r["source"] == "new":
            b.at[i, "foreign"], b.at[i, "investment_company"] = k["foreign"], k["investment"]
    b["spac_shell"] = b["sic"] == 6770
    b["eligible"] = ((b["source"] != "no_cik") & (b["foreign"] == False) & (b["investment_company"] == False)  # noqa: E712
                     & ~b["spac_shell"])
    b = b[b["security_id"].notna()].drop_duplicates("security_id", keep="first")
    return b.reset_index(drop=True)


# ======================================================================== the U300 panel at one signal

def universe_ranks(base: pd.DataFrame, fr: dict, week_end) -> pd.DataFrame:
    """dv50 ranks at ``week_end`` as the universe builder: dv50 = median of real close x volume over the last 50
    sessions (>= 25 rows); week close = last close at most 5 sessions before the week end; rank (descending, ties
    by security id) among eligible base names with a week close >= $10 and a dv50."""
    w = pd.Timestamp(week_end)
    grid = fr["close"].index
    k = grid.get_loc(w)
    elig = base[base["eligible"]].copy()
    ids = [s for s in elig["security_id"] if s in fr["close"].columns]
    elig = elig[elig["security_id"].isin(ids)].copy()
    dvf = (fr["close"][ids] * fr["volume"][ids]).rolling(50, min_periods=DV_MIN_ROWS).median()
    wclose = fr["close"][ids].iloc[max(0, k - CLOSE_STALE_SESSIONS): k + 1].ffill().iloc[-1]
    elig["week_close"] = elig["security_id"].map(wclose)
    elig["dv50"] = elig["security_id"].map(dvf.loc[w])
    ok = elig["week_close"].notna() & (elig["week_close"] >= MIN_PRICE) & elig["dv50"].notna()
    r = elig[ok].sort_values("security_id").copy()
    r["dv50_rank"] = r["dv50"].rank(ascending=False, method="first")
    elig["dv50_rank"] = elig["security_id"].map(r.set_index("security_id")["dv50_rank"])
    elig["universe_week"] = w
    return elig


def u300_candidates(ranked: pd.DataFrame, fr: dict, s) -> pd.DataFrame:
    """research_ml_cross_section.u300_candidates on the fresh ranks: rank <= 300, a close at s (last close at most
    5 sessions before s: still trading), SIC as in the base."""
    s = pd.Timestamp(s)
    c = ranked[ranked["dv50_rank"] <= UNIVERSE_SIZE].copy()
    c["in300"], c["s"] = True, s
    k = fr["close"].index.get_loc(s)
    last = fr["close"][list(c["security_id"])].iloc[max(0, k - CLOSE_STALE_SESSIONS): k + 1].ffill().iloc[-1]
    c["close_s"] = c["security_id"].map(last)
    c = c[np.isfinite(c["close_s"].astype(float))].copy()
    c["cik"] = pd.to_numeric(c["cik"], errors="coerce").astype("Int64")
    c["sic"] = pd.to_numeric(c["sic"], errors="coerce")
    keep = ["security_id", "ticker", "yahoo", "cik", "dv50_rank", "sic", "multi_class_group", "in300", "s",
            "universe_week", "close_s", "source"]
    return c[keep].reset_index(drop=True)


def sec_tables(facts: dict, s) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(annual facts, quarterly-EPS states, share facts) from fresh companyfacts payloads, as
    research_fundamentals.extract_all / research_megacap.extract_share_facts read the local copies; only facts
    filed strictly before s are kept."""
    s = ydate(s)
    ann, states, sh = [], [], []
    for cik, pl in facts.items():
        if not pl:
            continue
        pl = pl.get("payload", pl)
        rows, eps = rf.facts_from_payload(cik, pl)
        rows = [r for r in rows if str(r[5]) < s]
        eps = [e for e in eps if str(e[6]) < s]
        if rows:
            ann.append(pd.DataFrame(rows, columns=rf.ANNUAL_COLS).drop_duplicates(["group", "prio", "kind", "end",
                                                                                   "filed", "val"]))
        if eps:
            ef = pd.DataFrame(eps, columns=rf.EPS_COLS).drop_duplicates(["concept", "start", "end", "val", "filed"])
            stt = cs.eps_states(ef)
            if len(stt):
                states.append(stt)
        sh += [r for r in mc._facts_from_payload(cik, pl) if str(r[3]) < s]
    a = pd.concat(ann, ignore_index=True) if ann else pd.DataFrame(columns=rf.ANNUAL_COLS)
    e = pd.concat(states, ignore_index=True) if states else pd.DataFrame(
        columns=["cik", "filed", "avail", "q_end", "c_growth"])
    f = pd.DataFrame(sh, columns=["cik", "pri", "end", "filed", "val", "start"]).drop_duplicates(
        ["cik", "pri", "end", "filed", "val"])
    return a, e, f


def build_panel(cand: pd.DataFrame, fr: dict, facts: dict, qqq_tr: pd.Series, s,
                lists: pd.DataFrame | None = None) -> pd.DataFrame:
    """The U300 rows at s with the frozen panel's columns (research_ml_cross_section.build_panel steps)."""
    s = pd.Timestamp(s)
    annual, eps, sh = sec_tables(facts, s)
    lists = mc.load_company_lists() if lists is None else lists
    lists = lists[lists["as_of"] <= s]
    ids = list(cand["security_id"])
    close = fr["close"][ids].ffill()
    dv50 = (fr["close_adj"][ids] * fr["volume"][ids]).rolling(50, min_periods=20).median()
    saved = mc.SHARES_VS_FLOAT
    mc.SHARES_VS_FLOAT = np.inf          # fundamentals data rule 1.1a (as build_panel)
    try:
        capped = mc.market_caps(cand[["s", "security_id", "ticker", "cik", "dv50_rank", "universe_week"]], sh, lists,
                                close, fr["idx"][ids], dv50)
    finally:
        mc.SHARES_VS_FLOAT = saved
    cand = cand.copy()
    cand["mcap"] = capped["mcap"].to_numpy()
    cand["mcap_src"] = capped["mcap_src"].to_numpy()
    cand.loc[cand["mcap_src"].astype(str).str.startswith("dollar_volume"), "mcap"] = np.nan
    cand["dv50"] = mc._value_at(dv50, cand["security_id"], cand["s"])
    cand = rf.reject_small_mcaps(cand)
    states = pd.concat([rf.company_states(g) for _, g in annual.groupby("cik")], ignore_index=True) \
        if len(annual) else pd.DataFrame(columns=["cik", "filed", "fy0_end"])
    rows = rf.universes(cand)["U300"]
    m = rf.asof_states(rows, states, eps)
    m = rf.market_factors(m)
    m = m.sort_values(["s", "security_id"]).reset_index(drop=True)
    m = pd.concat([m, rf.composites(m)], axis=1)
    pids = sorted(m["security_id"].unique())
    pf = ml.price_features(fr["idx"][pids], fr["close_adj"][pids], fr["volume"][pids], qqq_tr, m["security_id"],
                           m["s"])
    for f in ml.PRICE_FEATURES:
        m[f] = pf[f].to_numpy()
    return m


def scores_of(panel: pd.DataFrame) -> pd.DataFrame:
    """research_short_overlay.signal_scores on the panel (S-MISP composite and borrow tiers; no GBRT here)."""
    pred = pd.DataFrame({"s": pd.Series(dtype="datetime64[ns]"), "security_id": pd.Series(dtype=str),
                         "GBRT": pd.Series(dtype=float)})
    p = panel.copy()
    p["security_id"] = p["security_id"].astype(str)
    p["s"] = pd.to_datetime(p["s"]).astype("datetime64[ns]")
    sc = ro.signal_scores(p, pred)
    for c in ("yahoo", "cik", "mcap_src", "filed", "fy0_end", "source", *ro.MISP_PARTS):
        if c in p.columns and c not in sc.columns:
            sc[c] = p[c].to_numpy()
    return sc


SIGNAL_COLS = ["s", "security_id", "ticker", "yahoo", "cik", "source", "dv50_rank", "mcap", "mcap_src", "vol_3m",
               "borrow", SIGNAL, *[c for c in ro.MISP_PARTS if c != "mom_12_1"], "mom_12_1", "filed", "fy0_end"]


def compute_signal(s, base: pd.DataFrame, chart_dir: Path, facts_for, qqq: st.Market, sessions_full,
                   lists: pd.DataFrame | None = None) -> tuple[pd.DataFrame, dict]:
    """Scores of the U300 names at s from local files. ``facts_for(ciks)`` returns {cik: companyfacts payload}
    for the U300 CIKs (fetched or read from the signal folder). Every price frame is cut at s (asserted); every SEC
    fact used was filed strictly before s (``sec_tables`` drops later ones, ``asof_states`` asserts it)."""
    s = pd.Timestamp(s)
    grid = qqq.s.sessions[(qqq.s.sessions <= s) & (qqq.s.sessions >= s - pd.Timedelta(days=LOOKBACK_DAYS))]
    if grid[-1] != s:
        raise ValueError(f"no QQQ session on the signal date {s.date()}")
    files = {r.security_id: chart_dir / f"{r.yahoo}.json.gz" for r in base[base["eligible"]].itertuples()}
    fr = price_frames(files, grid, ydate(s))
    cs.assert_window(fr["close"].dropna(how="all").index, None, ydate(s), "forward S-MISP prices")
    w = week_end_on_or_before(s, pd.DatetimeIndex(sessions_full))
    ranked = universe_ranks(base, fr, w)
    cand = u300_candidates(ranked, fr, s)
    facts = facts_for(sorted(cand["cik"].dropna().astype(int).unique()))
    qtr = qqq.s.tr.reindex(grid)
    panel = build_panel(cand, fr, facts, qtr, s, lists)
    pt = panel["filed"].notna()
    if not (pd.to_datetime(panel.loc[pt, "filed"]) < panel.loc[pt, "s"]).all():
        raise cs.DateGuardError("annual fact used on or before its filing date")
    sc = scores_of(panel)
    sc = sc[[c for c in SIGNAL_COLS if c in sc.columns]].sort_values([SIGNAL, "security_id"], na_position="last")
    meta = {"s": ydate(s), "week_end": ydate(w), "base_names": int(len(base)), "eligible": int(base["eligible"].sum()),
            "with_prices": int(fr["close"].notna().any().sum()), "ranked": int(ranked["dv50_rank"].notna().sum()),
            "top300": int((ranked["dv50_rank"] <= UNIVERSE_SIZE).sum()), "u300": int(len(panel)),
            "scored": int(sc[SIGNAL].notna().sum()), "new_listings_in_u300": int((cand["source"] == "new").sum()),
            "u300_without_companyfacts": int(sum(1 for c in panel["cik"].dropna() if not facts.get(int(c)))),
            "mcap_missing": int(panel["mcap"].isna().sum()), "rows_off_calendar": fr["dropped_rows"],
            "worst_2n": list(sc.dropna(subset=[SIGNAL]).head(2 * CFG.n)["ticker"])}
    return sc.reset_index(drop=True), meta


def signal_path(s, state: Path = STATE) -> Path:
    return state / f"s_{ydate(s)}.csv"


def prepare_signal(s, qqq: st.Market, base_dir: Path = BASE, fetch: bool = True, getters: dict | None = None,
                   sleep=time.sleep, frozen: pd.DataFrame | None = None, state: Path = STATE,
                   use_alpaca: bool = True) -> pd.DataFrame | None:
    """Fetch (if allowed) and compute the frozen signal of s once; the scores go to the committed state
    (``state/forward_observation/smisp/s_<date>.csv``, no price levels), raw bodies to the local cache. Returns the
    scores, or None when the inputs are not complete yet (re-run resumes)."""
    getters = getters or {}
    sdir = sdir_of(s, base_dir)
    out = signal_path(s, state)
    if out.exists():
        return load_signal(out)
    if pd.Timestamp(datetime.now(timezone.utc)) < pd.Timestamp(ydate(s) + " 18:00", tz=NY):
        print(f"S-MISP: signal {ydate(s)} is not complete before 18:00 New York time")
        return None
    snap, how = listing_for_signal(s, base_dir)
    if how != "after_signal":
        if not fetch:
            print(f"S-MISP: no Nasdaq listing snapshot after the {ydate(s)} close; run with --fetch-smisp")
            return None
        snap, how = fetch_listing(base_dir, getters.get("nasdaq")), "after_signal"
    if not fetch and not (sdir / "charts").exists():
        print(f"S-MISP: no fresh data for the {ydate(s)} signal; run with --fetch-smisp")
        return None
    sdir.mkdir(parents=True, exist_ok=True)
    listing = parse_listing(Path(snap).read_text())
    tick = sec_ticker_map(sdir, getters.get("sec"))
    base = base_rows(listing, frozen if frozen is not None else frozen_base(), tick)
    subs = fetch_sec_many(needs_submissions(base), sdir, "submissions", getters.get("sec"), sleep)
    base = finish_base(base, subs)
    base.to_csv(sdir / "universe_base.csv", index=False)
    s_ts = pd.Timestamp(s)
    period1 = ydate(s_ts - pd.Timedelta(days=LOOKBACK_DAYS))
    period2 = ydate(s_ts + pd.Timedelta(days=1))
    el = base[base["eligible"]]
    syms = dict(zip(el["yahoo"], el["ticker"]))
    charts = sdir / "charts"
    if fetch:
        fetch_charts(syms, charts, period1, period2, getters, sleep, use_alpaca)
    missing = [y for y in syms if not (charts / f"{y}.json.gz").exists()]
    logged = set()
    if (charts / "fetch_log.csv").exists():
        lg = pd.read_csv(charts / "fetch_log.csv", dtype=str)
        logged = set(lg.loc[lg["result"].isin(NO_DATA), "symbol"])
    pending = [y for y in missing if y not in logged]
    if pending:
        print(f"S-MISP: {len(pending)} charts still missing for {ydate(s)} (e.g. {pending[:5]}); re-run")
        return None

    def facts_for(ciks):
        return fetch_sec_many(ciks, sdir, "companyfacts", getters.get("sec"), sleep) if fetch else load_sec(sdir,
                                                                                                     "companyfacts")

    sc, meta = compute_signal(s_ts, base, charts, facts_for, qqq, qqq.s.sessions)
    srcs = pd.read_csv(charts / "chart_sources.csv", dtype=str) if (charts / "chart_sources.csv").exists() else None
    meta.update({"listing_snapshot": Path(snap).name, "listing_timing": how,
                 "charts_no_data_in_window": sorted(set(missing) & logged),
                 "chart_sources": srcs["source"].value_counts().to_dict() if srcs is not None else {},
                 "u300_chart_sources": (srcs.set_index("symbol")["source"].reindex(sc["yahoo"]).value_counts()
                                        .to_dict() if srcs is not None else {}),
                 "computed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "base_sources": base["source"].value_counts().to_dict()})
    out.parent.mkdir(parents=True, exist_ok=True)
    sc.drop(columns=[c for c in ("mcap",) if c in sc.columns]).to_csv(out, index=False, float_format="%.10g")
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, indent=1, default=str) + "\n")
    sc.to_csv(sdir / "signal_full.csv", index=False)          # local copy with the market caps
    print(f"S-MISP signal {ydate(s)}: {meta['u300']} U300 names, {meta['scored']} scored; worst 2N {meta['worst_2n']}")
    return load_signal(out)


def load_signal(path: Path) -> pd.DataFrame:
    sc = pd.read_csv(path, dtype={"security_id": str, "ticker": str, "yahoo": str}, parse_dates=["s"])
    return sc


# ======================================================================== IBKR borrow-fee snapshots (report only)

def parse_borrow_text(text: str, symbols=None) -> tuple[pd.Timestamp, pd.DataFrame]:
    """IBKR usa.txt -> (file time as New York wall clock, rows SYM / FEERATE (% a year) / AVAILABLE / REBATERATE).
    AVAILABLE '>10000000' becomes 10,000,001; ``symbols`` keeps only those rows."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("#BOF|"):
        raise ValueError("missing #BOF header")
    stamp = pd.Timestamp(datetime.strptime(lines[0][5:].strip(), "%Y.%m.%d|%H:%M:%S"))
    head = lines[1].lstrip("#").rstrip("|").split("|")
    want = set(symbols) if symbols is not None else None
    rows = []
    for ln in lines[2:]:
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split("|")
        rec = dict(zip(head, parts))
        sym = rec.get("SYM", "").strip()
        if want is not None and sym not in want:
            continue
        av = rec.get("AVAILABLE", "").strip()
        avn = 10_000_001.0 if av.startswith(">") else pd.to_numeric(av, errors="coerce")
        rows.append({"SYM": sym, "CUR": rec.get("CUR", ""), "FEERATE": pd.to_numeric(rec.get("FEERATE"), errors="coerce"),
                     "REBATERATE": pd.to_numeric(rec.get("REBATERATE"), errors="coerce"), "AVAILABLE": avn,
                     "AVAILABLE_raw": av})
    df = pd.DataFrame(rows, columns=["SYM", "CUR", "FEERATE", "REBATERATE", "AVAILABLE", "AVAILABLE_raw"])
    return stamp, df[df["CUR"].isin(["USD", ""])].drop_duplicates("SYM", keep="first").set_index("SYM")


class BorrowSnapshots:
    """The daily snapshots of scripts/record_borrow_fees.py. ``on(day)`` = the latest snapshot whose own file time
    (New York) falls on or before ``day``."""

    def __init__(self, root: Path = BORROW_DIR):
        self.root = root
        self.index = self._index()
        self._cache: dict = {}

    def _index(self) -> list:
        out = []
        log = self.root / "log.csv"
        known = {}
        if log.exists():
            for ln in log.read_text().splitlines():
                p = ln.split(",")
                if len(p) >= 5 and p[4].endswith(".txt.gz"):
                    known[p[4]] = p[2]
        for f in sorted((self.root / "raw").glob("usa_*.txt.gz")):
            stamp = known.get(f.name)
            if stamp is None:
                with gzip.open(f, "rt", encoding="utf-8", errors="replace") as fh:
                    stamp = fh.readline()[5:].strip()
            try:
                t = pd.Timestamp(datetime.strptime(stamp, "%Y.%m.%d|%H:%M:%S"))
            except ValueError:
                continue
            out.append((t, f))
        out.sort(key=lambda x: (x[0], x[1].name))
        return out

    def file_on(self, day) -> tuple[pd.Timestamp, Path] | None:
        end = pd.Timestamp(ydate(day)) + pd.Timedelta(days=1)
        cand = [x for x in self.index if x[0] < end]
        return cand[-1] if cand else None

    def rows(self, path: Path, symbols) -> pd.DataFrame:
        key = (path, tuple(sorted(symbols)))
        if key not in self._cache:
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                self._cache[key] = parse_borrow_text(fh.read(), symbols)[1]
        return self._cache[key]

    def on(self, day, symbols) -> tuple[pd.Timestamp | None, dict]:
        """{sym: (feerate % a year, available) or None when the name is not in the file}."""
        hit = self.file_on(day)
        if hit is None:
            return None, {s: None for s in symbols}
        df = self.rows(hit[1], symbols)
        return hit[0], {s: ((float(df.at[s, "FEERATE"]), float(df.at[s, "AVAILABLE"])) if s in df.index else None)
                        for s in symbols}


# ======================================================================== daily simulation (research functions)

def held_files(signals: dict, held_dir: Path) -> dict:
    """security_id -> chart path for every name in any month's worst-2N list."""
    out = {}
    for sc in signals.values():
        for r in ro.loser_orders(sc, SIGNAL, CFG.n).values():
            for sid in r:
                y = sc.loc[sc["security_id"] == sid, "yahoo"].iloc[0]
                out[sid] = held_dir / f"{y}.json.gz"
    return out


def fetch_held(signals: dict, held_dir: Path, getters: dict | None = None, sleep=time.sleep,
               use_alpaca: bool = True) -> dict:
    """Re-download the charts of every worst-2N name since the first signal (Alpaca first, Yahoo fallback); a
    failed download keeps the previous file (a delisted name then ends at its last fetched row)."""
    files = held_files(signals, held_dir)
    syms = {}
    for sc in signals.values():
        for r in sc.itertuples():
            if r.security_id in files:
                syms[r.yahoo] = r.ticker
    tmp_dir = held_dir / "incoming"
    if tmp_dir.exists():
        for p in tmp_dir.glob("*"):
            p.unlink()
    period1 = ydate(pd.Timestamp(FIRST_SIGNAL) - pd.Timedelta(days=40))
    period2 = ydate(pd.Timestamp(datetime.now(timezone.utc).date()) + pd.Timedelta(days=1))
    res = fetch_charts(syms, tmp_dir, period1, period2, getters, sleep, use_alpaca)
    for y in syms:
        p = tmp_dir / f"{y}.json.gz"
        if p.exists():
            p.replace(held_dir / f"{y}.json.gz")
    return res


def market(files: dict, qqq: st.Market, effr: pd.Series, end: str, start: str) -> ro.Market:
    """research_short_overlay.Market on the QQQ sessions [start, end]: total-return index (forward-filled, flat after
    the last row) and raw close of each name; a name whose rows stop before the last session is booked (covered)
    the next session at its last value (no terminal value is known in the forward data: 0%)."""
    ses = qqq.s.sessions
    grid = ses[(ses >= pd.Timestamp(start)) & (ses <= pd.Timestamp(end))]
    fr = price_frames(files, grid, end)
    sids = sorted(fr["close"].columns)
    tr = fr["tr"][sids].copy()
    close = fr["close"][sids]
    has = close.notna()
    last_row = has[::-1].idxmax()
    booked = {}
    for j, sid in enumerate(sids):
        lr = last_row[sid]
        if not has[sid].any() or lr >= grid[-1]:
            continue
        nxt = grid[grid > lr][0]
        tr.loc[nxt, sid] = 0.0
        booked[j] = int(grid.get_loc(nxt))
    idx = rev.make_index(tr, close).ffill()
    e = effr.reindex(effr.index.union(grid)).ffill().reindex(grid).fillna(0.0)
    return ro.Market(sessions=grid, sids=sids, col={s: i for i, s in enumerate(sids)}, idx=idx.to_numpy(float),
                     close=close.ffill().to_numpy(float), booked=booked,
                     qqq_tr=qqq.s.tr.reindex(grid).fillna(0.0).to_numpy(float),
                     qqq_px=pd.Series(qqq.s.close, index=ses).reindex(grid).ffill().to_numpy(float),
                     effr=e.to_numpy(float), idx_variants={"base": idx.to_numpy(float)})


def ibkr_fee_adjustment(res: dict, mk: ro.Market, sched: list, tick: dict, snaps: BorrowSnapshots | None) -> dict:
    """Report-only sensitivity: the borrow fee actually charged by IBKR (FEERATE of the latest snapshot on or before
    each calendar day) instead of the registered tier. Each short's value is approximated as an equal share of the
    short book at the previous close (the simulator does not expose per-name values). Returns the cumulative NAV
    adjustment per session (tier cost - IBKR cost) and per-day fee records."""
    nav = res["nav"]
    if snaps is None or not snaps.index:
        return {"adj": pd.Series(0.0, index=nav.index), "fees": pd.DataFrame(
            columns=["day", "session", "security_id", "ticker", "sym", "tier", "fee", "available", "in_file",
                     "snapshot", "t_reb"])}
    held = sorted(res["held"], key=lambda x: x[0])
    tier = {}
    for t, s, order, info in sched:
        tier[t] = {sid: v[1] for sid, v in info.items()}
    adj = pd.Series(0.0, index=nav.index)
    recs = []
    cum = 0.0
    dates = list(nav.index)
    for k in range(1, len(dates)):
        t_prev = mk.sessions.get_loc(dates[k - 1])
        cur = [x for x in held if x[0] <= t_prev]
        if not cur:
            adj.iloc[k] = cum
            continue
        t_reb, names = cur[-1]
        names = [n for n in names if not (mk.col.get(n) in mk.booked and mk.booked[mk.col[n]] <= t_prev)]
        n_short = int(nav["n_short"].iloc[k - 1])
        if not names or n_short == 0:
            adj.iloc[k] = cum
            continue
        value = float(nav["short"].iloc[k - 1]) / n_short
        days = pd.date_range(dates[k - 1], dates[k] - pd.Timedelta(days=1), freq="D")
        syms = {n: ibkr_symbol(tick.get(n, n)) for n in names}
        for d in days:
            stamp, fees = snaps.on(d, list(syms.values()))
            for n, sym in syms.items():
                trate = tier.get(t_reb, {}).get(n, ro.BORROW_REST)
                f = fees.get(sym)
                actual = f[0] / 100.0 if f is not None and np.isfinite(f[0]) else trate
                cum += value * (trate - actual) / ro.DAYCOUNT
                recs.append({"day": d, "session": dates[k], "security_id": n, "ticker": tick.get(n, n), "sym": sym,
                             "tier": trate, "fee": (f[0] / 100.0 if f is not None else np.nan),
                             "available": (f[1] if f is not None else np.nan), "in_file": f is not None,
                             "snapshot": stamp, "t_reb": t_reb})
        adj.iloc[k] = cum
    return {"adj": adj, "fees": pd.DataFrame(recs)}


def run_line(signals: dict, qqq: st.Market, oneq: st.Market, as_of: str, held_dir: Path, effr: pd.Series,
             snaps: BorrowSnapshots | None = None) -> dict:
    """Simulate the frozen configuration from the first signal's trade day through the as-of date."""
    sc_all = pd.concat(list(signals.values()), ignore_index=True)
    sc_all["s"] = pd.to_datetime(sc_all["s"]).astype("datetime64[ns]")
    files = held_files(signals, held_dir)
    start = ydate(min(signals))
    mk = market(files, qqq, effr, as_of, start)
    sched = ro.schedule(mk, sc_all, SIGNAL, CFG.n)
    if not sched:
        return {"started": False}
    t0 = sched[0][0]
    t_end = len(mk.sessions) - 1
    res = ro.simulate(mk, sched, CFG, t0, t_end)
    legs = ro.basket_diagnostics(mk, sched, CFG.n, t0, t_end)
    tick = dict(zip(sc_all["security_id"], sc_all["ticker"]))
    fee = ibkr_fee_adjustment(res, mk, sched, tick, snaps)
    nav = res["nav"]["nav"]
    ses = mk.sessions[t0: t_end + 1]
    q = (1 + pd.Series(mk.qqq_tr, index=mk.sessions).loc[ses].iloc[1:]).cumprod()
    q = pd.concat([pd.Series([1.0], index=ses[:1]), q])
    o_tr = oneq.s.tr.reindex(ses).fillna(0.0)
    o = pd.concat([pd.Series([1.0], index=ses[:1]), (1 + o_tr.iloc[1:]).cumprod()])
    return {"started": True, "mk": mk, "sched": sched, "res": res, "legs": legs, "tick": tick, "fees": fee["fees"],
            "nav": nav, "nav_ibkr": nav + fee["adj"], "qqq": q, "oneq": o, "t0": t0, "t_end": t_end,
            "first_trade": mk.sessions[t0], "last_session": mk.sessions[t_end]}


def month_rows(line: dict) -> list:
    """One row per calendar month from the first trade (research ``month_returns``: the first month from the first
    value, i.e. the close of the first trade). Shorts = names held after the month's rebalance (the close of the
    month's first session); the short leg = the research ideal basket over that holding period (trade day ->
    next trade day) and QQQ over the same days; IBKR fees over the same holding period."""
    nav, q, o, ni = line["nav"], line["qqq"], line["oneq"], line["nav_ibkr"]
    mr, mq, mo, mi = ro.month_returns(nav), ro.month_returns(q), ro.month_returns(o), ro.month_returns(ni)
    mk, res, legs, fees, tick = line["mk"], line["res"], line["legs"], line["fees"], line["tick"]
    last = line["last_session"]
    held = sorted(res["held"], key=lambda x: x[0])
    rows = []
    cum_s = cum_o = 1.0
    ex_hist = []
    for n, mon in enumerate(mr.index, start=1):
        reb = [x for x in held if mk.sessions[x[0]].to_period("M") == mon]
        names = reb[0][1] if reb else []
        t_reb = reb[0][0] if reb else None
        leg = legs[legs["t0"] == mk.sessions[t_reb]] if (t_reb is not None and len(legs)) else pd.DataFrame()
        cum_s *= 1 + mr[mon]
        cum_o *= 1 + mo[mon]
        ex = mr[mon] - mo[mon]
        ex_hist.append(ex)
        f = fees[fees["t_reb"] == t_reb] if t_reb is not None and len(fees) else pd.DataFrame()
        later = mk.sessions[mk.sessions.to_period("M") > mon]
        final = len(later) > 0 and later[0] <= last and (len(leg) == 0 or leg["t1"].iloc[0] < last
                                                          or leg["t1"].iloc[0] == later[0])
        rows.append({
            "month": str(mon), "n": n,
            "shorts": " ".join(sorted(tick.get(s_, s_) for s_ in names)) if names else "none",
            "ret": mr[mon], "qqq": mq[mon], "oneq": mo[mon], "excess": ex,
            "leg": float(leg["basket"].iloc[0]) if len(leg) else np.nan,
            "leg_qqq": float(leg["qqq"].iloc[0]) if len(leg) else np.nan,
            "leg_period": (f"{leg['t0'].iloc[0]:%m-%d}→{leg['t1'].iloc[0]:%m-%d}" if len(leg) else ""),
            "fee_mean": float(f.dropna(subset=["fee"]).groupby("security_id")["fee"].mean().mean())
            if len(f) and f["fee"].notna().any() else np.nan,
            "fee_max": float(f["fee"].max()) if len(f) and f["fee"].notna().any() else np.nan,
            "unborrowable": sorted(set(f.loc[f["available"] == 0, "ticker"])) if len(f) else [],
            "not_in_file": sorted(set(f.loc[~f["in_file"].astype(bool), "ticker"])) if len(f) else [],
            "fee_days": int(f["day"].nunique()) if len(f) else 0,
            "ret_ibkr": mi[mon], "cum_excess": cum_s - cum_o,
            "t": ro.tstat(pd.Series(ex_hist)) if len(ex_hist) >= 3 else float("nan"),
            "status": "final" if final else "provisional"})
    return rows
