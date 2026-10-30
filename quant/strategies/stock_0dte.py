"""Decoder for the single-stock 0DTE grid's QuantConnect export (docs/research_ledger_stock_0dte.md, 0.3 and 0.7).

qc/stock_0dte/main.py queues, per 0DTE day (a Friday, or the Thursday before a Friday holiday), a day record and then
one block per member name with same-day-expiry contracts: a HEADER-value header followed by one row per (entry time,
structure); at the next session's first bar each of those names gets a next-open record (raw open, split flag, the
book day's official close). qc/stock_0dte/fetch.js rebuilds the queue from the chart points and saves it as gzipped
JSON under output/research_only/stock_0dte/raw/ together with the run's name dictionary (runtime statistics D00..).
``parse_stream`` turns one queue into ``(names, rows)``: ``names`` has one row per name-day, ``rows`` one row per
(name-day, entry, shape) with the unpacked position fields (NaN where the position was not opened; ``code`` says
why).

The structure catalogue (``SHAPES``) is imported from qc/stock_0dte/sim.py so the decoder cannot drift from the
engine.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import math
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from quant.paths import ROOT

QC_DIR = ROOT / "qc" / "stock_0dte"
HEADER = 29
OFF = 5_000_000
CODES = {1: "nosel", 2: "wingmiss", 3: "covmiss", 4: "stale", 5: "nocredit", 9: "too_late"}
MGMT = ("H", "M1", "M2", "M3")
DAY_MARK = 950_000_000_000_000
NAME_MARK = 900_000_000_000_000
END_MARK = 920_000_000_000_000
OPEN_MARK = 970_000_000_000_000


def load_sim():
    """qc/stock_0dte/sim.py as a module (scipy.special stubbed when the local venv lacks scipy). The engine files
    import each other by name (sim, simx, optmath), as on QuantConnect, so the directory goes on sys.path."""
    try:
        import scipy.special  # noqa: F401
    except ImportError:
        sp, sps = types.ModuleType("scipy"), types.ModuleType("scipy.special")
        erf = np.vectorize(math.erf, otypes=[float])
        sps.ndtr = lambda x: 0.5 * (1.0 + erf(np.asarray(x, float) / math.sqrt(2.0)))
        sp.special = sps
        sys.modules.setdefault("scipy", sp)
        sys.modules["scipy.special"] = sps
    if str(QC_DIR) not in sys.path:
        sys.path.insert(0, str(QC_DIR))
    mod = importlib.import_module("sim")
    if Path(mod.__file__).resolve() != (QC_DIR / "sim.py").resolve():
        raise ImportError(f"'sim' resolves to {mod.__file__}, not {QC_DIR / 'sim.py'}")
    return mod


SIM = load_sim()
SHAPES = SIM.SHAPES
FAM = SIM.FAM
KIND = SIM.KIND
ENTRY = SIM.ENTRY
U = SIM.U
NS, NE, NB = len(SHAPES), len(ENTRY), SIM.NB                # NB row blocks: the five entries, then the overnight arm


def shape_name(si: int) -> str:
    f, d, w = SHAPES[si]
    fam = FAM[f]
    wl = "W1" if w == 0.0 else f"W{w * 100:g}%"
    if fam in ("PCS", "CCS", "IC"):
        return f"{fam}{d:.2f}-{wl}"
    if fam in ("IF", "IFP", "IFM"):
        return f"{fam}-{wl}"
    if fam in ("SD", "LD", "SDH", "RRB", "DVC", "DVP", "MPD", "ORB", "PRF"):
        return fam
    return f"{fam}{d:.2f}"


def _two(x: int) -> tuple[int, int]:
    return x // 10**7, x % 10**7


def _date(ymd: int) -> pd.Timestamp:
    return pd.Timestamp(year=2000 + ymd // 10_000, month=(ymd // 100) % 100, day=ymd % 100)


def parse_stream(vals: list, ids: dict[int, str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(name-days, position rows, day records). ``ids`` maps the run's name ids to tickers (runtime statistics
    D00.., "id:ticker:cik" tokens). Every last session of a week has a day record, also with no name."""
    names, recs, opens, days = [], [], {}, []
    i, n = 0, len(vals)
    day, left = None, 0
    while i < n:
        v = vals[i]
        if v is None:
            raise ValueError(f"missing value at {i}")
        if v == END_MARK:
            i += 1
            continue
        if OPEN_MARK <= v < OPEN_MARK + 10**10:
            if left:
                raise ValueError(f"next-open record at {i} while {left} name blocks of {day} are missing")
            x = int(v - OPEN_MARK)
            opens[(_date(x // 1000), x % 1000)] = (vals[i + 1] / U, vals[i + 2], vals[i + 3] / U)
            i += 4
            continue
        if DAY_MARK <= v < DAY_MARK + 10**10:
            if left:
                raise ValueError(f"day record at {i} while {left} name blocks of {day} are missing")
            x = int(v - DAY_MARK)
            day, left = _date(x // 100), x % 100
            days.append({"date": day, "n_names": left})
            i += 1
            continue
        if not (NAME_MARK <= v < NAME_MARK + 10**10):
            raise ValueError(f"expected a name header at {i}, got {v}")
        x = int(v - NAME_MARK)
        d, nid = _date(x // 1000), x % 1000
        if day is None or d != day or left <= 0:
            raise ValueError(f"name header {d.date()} at {i} outside its day record ({day}, {left} left)")
        left -= 1
        h = [int(t) for t in vals[i:i + HEADER]]
        flags = h[1]
        half, status = (flags // 10**12) % 10, (flags // 10**11) % 10
        nchain, nload = (flags // 10**6) % 10**4, (flags // 10**2) % 10**4
        penny, sig_ok = (flags // 10) % 10, flags % 10
        nrows = h[28]
        names.append({"date": d, "nid": nid, "name": ids.get(nid, f"#{nid}"), "half": half, "status": status,
                      "nchain": nchain, "nload": nload, "penny": penny, "sig_ok": sig_ok,
                      "S_open": h[2] / U, "S_close": h[3] / U, "prev_close": h[4] / U, "sig": h[5] / 1e6 if sig_ok else np.nan,
                      "kmp": h[6] / U if h[6] else np.nan, "rank": h[7] // 10**6, "nreal": h[7] % 10**6,
                      "vix_prev": h[8] / 100 if h[8] else np.nan,
                      **{f"S_e{e}": h[9 + e] / U for e in range(NE)}, **{f"strad_e{e}": h[14 + e] / U for e in range(NE)},
                      **{f"pin_e{e}": (h[19 + e] / U if h[19 + e] else np.nan) for e in range(NE)},
                      "rank_on": h[24], "S_on": h[25] / U if h[25] else np.nan, "strad_on": h[26] / U if h[26] else np.nan,
                      "vix_on": h[27] / 100 if h[27] else np.nan})
        i += HEADER
        if nrows and nrows != NS * NB:
            raise ValueError(f"{d.date()} {nid}: {nrows} rows, expected {NS * NB}")
        k = len(names) - 1
        for r in range(nrows):
            e, si = divmod(r, NS)
            x1 = vals[i]
            if x1 < 0:
                recs.append((k, e, si, int(-x1)))
                i += 1
                continue
            x = [int(t) for t in vals[i:i + 10]]
            i += 10
            c, den = _two(x[0])
            ph, p1 = _two(x[1])
            p2, p3 = _two(x[2])
            im0, dc = _two(x[3])
            fe0, fef = _two(x[7])
            x7 = x[6]
            recs.append((k, e, si, 0, (c - OFF) / U, den / U, (ph - OFF) / U, (p1 - OFF) / U, (p2 - OFF) / U,
                         (p3 - OFF) / U, im0 / U, (dc - OFF) / U,
                         (x[4] // 10**10) % 1000, (x[4] // 10**7) % 1000, (x[4] // 10**4) % 1000, (x[4] % 10**4) / 100,
                         (x[5] // 10**8) / 100, ((x[5] // 10**4) % 10**4) / 100, (x[5] % 10**4) / 100,
                         ((x7 // 10**9) % 1000) / 1000, ((x7 // 10**5) % 10**4) / 1e4, (x7 // 10**4) % 10,
                         (x7 // 10**3) % 10, (x7 // 100) % 10, (x7 // 10) % 10, x7 % 10, fe0 / U, fef / U,
                         (x7 // 10**13) % 10 - 2, (x7 // 10**12) % 10 - 2, ((x[8] % 10**7) - OFF) / U,
                         (x[8] // 10**7) / 100, ((x[9] // 10**6) % 1000) / 100, ((x[9] // 10**3) % 1000) / 100,
                         (x[9] % 1000) / 100, x[9] // 10**12, ((x[9] // 10**9) % 1000) / 100, x[4] // 10**13,
                         x7 // 10**14))
    if left:
        raise ValueError(f"stream ended with {left} name blocks of {day} missing")
    cols = ["k", "entry", "shape", "code", "credit", "den", "pnl_H", "pnl_M1", "pnl_M2", "pnl_M3", "imp_entry",
            "d_close", "x_M1", "x_M2", "x_M3", "spr_entry", "imp_M1", "imp_M2", "imp_M3", "delta", "width_frac",
            "n_entry", "n_flat", "n_M1", "n_M2", "n_M3", "fee_entry", "fee_flat", "nsh_hold", "nsh_flat", "d15",
            "s_fill", "xfee_M1", "xfee_M2", "xfee_M3", "flat_orders", "flat_surcharge", "stuck", "direction"]
    rows = pd.DataFrame([r + (np.nan,) * (len(cols) - len(r)) for r in recs], columns=cols)
    nm = pd.DataFrame(names)
    if len(nm):
        got = [opens.get((d, k)) for d, k in zip(nm["date"], nm["nid"])]
        nm["next_open"] = [g[0] if g and g[0] > 0 else np.nan for g in got]
        nm["split_next"] = [int(g[1]) if g else -1 for g in got]   # 1 = a split took effect at the next open
        nm["close_official"] = [g[2] if g and g[2] > 0 else np.nan for g in got]
        # status 2 (no real minute bar): the header close is 0 and the engine settled the overnight block at the
        # daily close, which the next-open record carries as close_official (NaN when neither is known)
        nm["S_close"] = nm["S_close"].where(nm["S_close"] > 0, nm["close_official"])
    return nm, rows, pd.DataFrame(days, columns=["date", "n_names"])


def name_ids(meta: dict) -> dict[int, str]:
    """The run's id -> ticker dictionary from its runtime statistics (D00, D01, ... joined; "id:ticker:cik")."""
    text = "".join(meta["rs"][k] for k in sorted(meta["rs"]) if k.startswith("D") and k[1:].isdigit())
    out = {}
    for tok in text.split():
        a, b = tok.split(":", 1)
        out[int(a)] = b.split(":")[0]
    return out


def name_ciks(meta: dict) -> dict[str, str]:
    """ticker -> SEC CIK (QC's Morningstar company reference) of the run's names; "" where QC has none."""
    text = "".join(meta["rs"][k] for k in sorted(meta["rs"]) if k.startswith("D") and k[1:].isdigit())
    out = {}
    for tok in text.split():
        parts = tok.split(":")
        if len(parts) == 3:
            out[parts[1]] = parts[2]
    return out


def read_exports(paths) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[dict]]:
    """Concatenate export files (chunks of the book): name-days, rows (``k`` re-based to the concatenated name-days),
    day records and the run metas."""
    names, rows, days, metas = [], [], [], []
    base = 0
    for p in paths:
        with gzip.open(p, "rt") as fh:
            obj = json.load(fh)
        for run in obj.get("runs", [obj]):
            if run.get("bad"):
                raise ValueError(f"{Path(p).name}: {run['bad']} bad slots")
            nm, r, d = parse_stream(run["vals"], name_ids(run["meta"]))
            r["k"] += base
            base += len(nm)
            names.append(nm)
            rows.append(r)
            days.append(d)
            metas.append(run["meta"])
    return (pd.concat(names, ignore_index=True), pd.concat(rows, ignore_index=True), pd.concat(days, ignore_index=True),
            metas)


# ---------------------------------------------------------------------------------------------------- panel
# Per-position arrays over (shape, block, management, name-day), the input of studies/stock_0dte.py (ledger 0.5).
# Block e < NE is the same-day entry ENTRY[e]; block NE is the overnight arm (entered at the previous close).
TP_MGMT = {0: (3,), 1: (3,), 2: (2, 3), 3: (3,), 4: (), 6: (2, 3)}   # management slots that exit as take-profits
CONS_FEE = 0.30                                                 # extra per contract in the conservative execution
DEN_FLOOR = 0.25                                                # risk base of defined-risk structures >= 0.25 x width


@dataclass
class Panel:
    names: pd.DataFrame         # one row per name-day (header fields)
    opened: np.ndarray          # bool [NS, NB, NK]
    code: np.ndarray            # int  [NS, NB, NK]: 0 opened, >0 why not, -1 no row
    pnl: np.ndarray             # [NS, NB, 4, NK] 15:50-flatten convention, dollars per share (0 where not opened)
    hold: np.ndarray            # [NS, NB, 4, NK] held to the close at intrinsic
    touch: np.ndarray           # [NS, NB, 4, NK] touch-fill adjustment (<= 0) to add to pnl: entry and take-profits at
                                #   the bid / ask (stops and the flatten already fill at the touch)
    cons: np.ndarray            # [NS, NB, 4, NK] conservative adjustment (<= 0): touch plus CONS_FEE per contract traded
    n_exit: np.ndarray          # [NS, NB, 4, NK] contracts in the order closing each slot (the slot's exit, else the
                                #   15:50 flatten)
    fee_exit: np.ndarray        # [NS, NB, 4, NK] fees of that closing, dollars per contract-share
    d15: np.ndarray             # [NS, NB, NK] FLAT15 minus FLAT for the hold-to-15:50 slot
    nsh_hold: np.ndarray        # [NS, NB, NK] shares delivered per contract-share if held to the close
    nsh_flat: np.ndarray        # [NS, NB, NK] shares delivered by legs left to expire after the flatten
    den: np.ndarray             # [NS, NB, NK] risk base after the floor (NaN where not opened)
    width: np.ndarray           # [NS, NB, NK] wing width per share (verticals / condors / flies, else NaN)
    net: np.ndarray             # [NS, NB, NK] credit kept after entry fees (debit: negative)
    s0: np.ndarray              # [NB, NK] stock at the selection minute (overnight: the previous session's fill)
    xret: np.ndarray            # [NB, NK] stock return from the selection minute (overnight: the fill) to the close
    xmin: np.ndarray            # [NS, NB, 3, NK] exit minute of M1..M3 (0 = none)
    fee_entry: np.ndarray       # [NS, NB, NK]
    fee_flat: np.ndarray        # [NS, NB, NK]
    n_legs: np.ndarray          # [NS, NB, NK] contracts traded at entry
    xfee: np.ndarray            # [NS, NB, 3, NK] fees of the M1..M3 exit orders, dollars per contract-share
    xpos: np.ndarray            # [NS, NB, NK] stock return from each position's own fill to the close
    orb_dir: np.ndarray         # [NK] breakout direction (1 call, 2 put, 0 none) and its fill minute
    orb_min: np.ndarray
    flat_orders: np.ndarray     # [NS, NB, NK] orders the 15:50 flatten sent
    flat_sur: np.ndarray        # [NS, NB, NK] their IBKR $1-per-order surcharge at one lot, dollars per contract-share
    stuck: np.ndarray           # [NS, NB, NK] legs that mattered but settled at intrinsic without a trade
    direction: np.ndarray       # [NS, NB, NK] 1 rise, 2 fall (MPD, IFP, IFM), 0 otherwise


def build_panel(names: pd.DataFrame, rows: pd.DataFrame) -> Panel:
    nk = len(names)
    shp = (NS, NB, nk)
    opened = np.zeros(shp, bool)
    code = np.full(shp, -1, int)
    pnl = np.zeros((NS, NB, 4, nk))
    hold = np.zeros((NS, NB, 4, nk))
    cons = np.zeros((NS, NB, 4, nk))
    touch = np.zeros((NS, NB, 4, nk))
    n_exit = np.zeros((NS, NB, 4, nk), int)
    fee_exit = np.zeros((NS, NB, 4, nk))
    d15 = np.zeros(shp)
    nsh_hold = np.zeros(shp, int)
    nsh_flat = np.zeros(shp, int)
    den = np.full(shp, np.nan)
    width = np.full(shp, np.nan)
    net = np.full(shp, np.nan)
    xmin = np.zeros((NS, NB, 3, nk), int)
    fee_entry = np.zeros(shp)
    fee_flat = np.zeros(shp)
    n_legs = np.zeros(shp, int)
    xfee = np.zeros((NS, NB, 3, nk))
    xpos = np.zeros(shp)
    orb_dir = np.zeros(nk, int)
    orb_min = np.zeros(nk, int)
    flat_orders = np.zeros(shp, int)
    flat_sur = np.zeros(shp)
    stuck = np.zeros(shp, int)
    direction = np.zeros(shp, int)
    S = np.vstack([names[[f"S_e{x}" for x in range(NE)]].to_numpy().T, names["S_on"].to_numpy()[None, :]])
    if len(rows):
        k = rows["k"].to_numpy(int)
        s, e = rows["shape"].to_numpy(int), rows["entry"].to_numpy(int)
        code[s, e, k] = rows["code"].to_numpy(int)
        o = rows["code"].to_numpy(int) == 0
        r = rows[o]
        s, e, k = s[o], e[o], k[o]
        opened[s, e, k] = True
        kind = np.array([KIND[SHAPES[x][0]] for x in s])
        vert = np.array([SHAPES[x][0] in SIM.VERT_DEBIT for x in s])
        nt = (r["credit"] - r["fee_entry"]).to_numpy()
        dn = r["den"].to_numpy()
        wd = np.where(kind == 0, dn + nt, np.nan)               # den = width - net for defined-risk structures
        dn_eff = np.where(kind == 0, np.maximum(dn, DEN_FLOOR * wd), dn)
        den[s, e, k], net[s, e, k] = dn_eff, nt
        # debit verticals and the ratio fly export their width as a share of the parity price; the selection-minute
        # stock price stands in
        wf = r["width_frac"].to_numpy() * S[e, k]
        width[s, e, k] = np.where(kind == 0, wd, np.where(vert | (kind == 6), wf, np.nan))
        fee_entry[s, e, k] = r["fee_entry"].to_numpy()
        fee_flat[s, e, k] = r["fee_flat"].to_numpy()
        n_legs[s, e, k] = r["n_entry"].to_numpy()
        nsh_hold[s, e, k] = r["nsh_hold"].to_numpy(int)
        nsh_flat[s, e, k] = r["nsh_flat"].to_numpy(int)
        d15[s, e, k] = r["d15"].to_numpy()
        flat = r[["pnl_H", "pnl_M1", "pnl_M2", "pnl_M3"]].to_numpy()
        xm = r[["x_M1", "x_M2", "x_M3"]].to_numpy().copy()
        orb = np.array([SHAPES[x][0] == 20 for x in s])
        if orb.any():                                           # the breakout's slots: exit, fill minute, direction
            orb_min[k[orb]] = xm[orb, 1]
            orb_dir[k[orb]] = xm[orb, 2]
            xm[orb, 1:] = xm[orb, :1]
        xfee[s, e, :, k] = r[["xfee_M1", "xfee_M2", "xfee_M3"]].to_numpy() / 100.0
        flat_orders[s, e, k] = r["flat_orders"].to_numpy(int)
        flat_sur[s, e, k] = r["flat_surcharge"].to_numpy() / 100.0
        stuck[s, e, k] = r["stuck"].to_numpy(int)
        direction[s, e, k] = r["direction"].to_numpy(int)
        sc = names["S_close"].to_numpy()[k]
        sf = r["s_fill"].to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            xpos[s, e, k] = np.where((sf > 0) & (sc > 0), sc / sf - 1.0, 0.0)
        live = np.column_stack([np.ones(len(r), bool), xm == 0])  # still open at the flatten
        hd = flat + live * r["d_close"].to_numpy()[:, None]
        for m in range(4):
            pnl[s, e, m, k] = flat[:, m]
            hold[s, e, m, k] = hd[:, m]
        xmin[s, e, :, k] = xm
        # touch fills: the entry and take-profit exits at the bid / ask; conservative: also CONS_FEE per contract
        # traded (entry and the slot's closing order)
        imp = r[["imp_M1", "imp_M2", "imp_M3"]].to_numpy()
        nx = r[["n_M1", "n_M2", "n_M3"]].to_numpy().astype(int)
        xf = r[["xfee_M1", "xfee_M2", "xfee_M3"]].to_numpy() / 100.0
        n_fl = r["n_flat"].to_numpy().astype(int)
        f_fl = r["fee_flat"].to_numpy()
        n_en = r["n_entry"].to_numpy()
        for m in range(4):
            if m == 0:
                t_adj, nn, fx = -r["imp_entry"].to_numpy(), n_fl, f_fl
            else:
                exited = xm[:, m - 1] > 0
                tp = np.array([m in TP_MGMT.get(q, ()) for q in kind])
                t_adj = -r["imp_entry"].to_numpy() - np.where(exited & tp, imp[:, m - 1], 0.0)
                nn = np.where(exited, nx[:, m - 1], n_fl)
                fx = np.where(exited, xf[:, m - 1], f_fl)
            # the breakout fills every slot with one closing order (take-profit, stop, the 15:30 stop or the
            # flatten), exported in slot M1: its fee, contracts and improvement (0 unless a take-profit)
            t_adj = np.where(orb, -r["imp_entry"].to_numpy() - imp[:, 0], t_adj)
            nn, fx = np.where(orb, nx[:, 0], nn), np.where(orb, xf[:, 0], fx)
            touch[s, e, m, k] = t_adj
            cons[s, e, m, k] = t_adj - CONS_FEE / 100.0 * (n_en + nn)
            n_exit[s, e, m, k] = nn
            fee_exit[s, e, m, k] = fx
    Sc = names["S_close"].to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        xret = np.where((S > 0) & (Sc[None, :] > 0), Sc[None, :] / S - 1.0, 0.0)
    return Panel(names, opened, code, pnl, hold, touch, cons, n_exit, fee_exit, d15, nsh_hold, nsh_flat, den, width,
                 net, S, np.nan_to_num(xret), xmin, fee_entry, fee_flat, n_legs, xfee, xpos, orb_dir, orb_min,
                 flat_orders, flat_sur, stuck, direction)


# ---------------------------------------------------------------------------------------------------- earnings days
EARN_FROM = "2011-06-01"        # releases needed: the book starts 2012-02-03 and 'known' looks 200 days back


def acceptance_et(cik: int, accession: str, filing_date: str) -> str:
    """A filing's acceptance time in New York wall-clock time from its EDGAR header ('' when EDGAR has none). The
    submissions JSON labels its times UTC, but about 16% of them are New York time (the earnings pipeline compared
    90,962 Item 2.02 filings with their headers: 15,047 'et_labelled_z'), so the header decides. The pipeline's
    header cache is read first (read-only); a missing header is fetched through quant.data.sources.sec_cache (the
    index headers from 2014-06, the .hdr.sgml before)."""
    from pipelines.reversal_data import earnings as E
    from quant.data.sources.sec_cache import sec_get

    kind = E.cached_header_kind(cik, accession)
    text = None
    if kind:
        text = gzip.decompress(E.header_path(cik, accession, kind).read_bytes()).decode("utf-8", "replace")
    else:
        for kind in E.header_kinds(filing_date):
            data = sec_get(E.header_url(cik, accession, kind), "sec_headers", name=f"{cik}_{accession}_{kind}")
            if data:
                text = data.decode("utf-8", "replace")
                break
    return E.parse_header(text, cik)["acceptance_header_et"] if text else ""


def earnings_releases(ciks) -> pd.DataFrame:
    """Earnings releases of the given SEC CIKs from EDGAR (ledger 0.2): every 8-K / 8-K/A with Item 2.02 filed from
    EARN_FROM, with its acceptance time in New York time (``t_et``: the header's, else the JSON's read as UTC;
    ``t_source`` says which). Companies that file no 8-K (foreign issuers, e.g. BABA) have none: their earnings
    days are unknown."""
    from quant.data.sources.sec_cache import submissions

    rows = []
    for cik in sorted({int(c) for c in ciks if c}):
        rec = submissions(cik)
        if rec is None:
            continue
        f = rec["all_filings"]
        for form, acc, items, accession, fdate in zip(f["form"], f["acceptanceDateTime"], f["items"],
                                                      f["accessionNumber"], f["filingDate"]):
            if form in ("8-K", "8-K/A") and "2.02" in (items or "").split(",") and (fdate or "") >= EARN_FROM:
                rows.append((str(cik), form, acc, accession, fdate))
    df = pd.DataFrame(rows, columns=["cik", "form", "acceptance", "accession", "filing_date"])
    if df.empty:
        return df.assign(t_et=pd.Series(dtype="datetime64[ns]"), t_source=pd.Series(dtype=str))
    df = df.drop_duplicates(["cik", "accession"]).reset_index(drop=True)
    head = [acceptance_et(int(c), a, fd) for c, a, fd in zip(df["cik"], df["accession"], df["filing_date"])]
    head = pd.to_datetime(pd.Series(head).replace("", None), errors="coerce")
    js = pd.to_datetime(df["acceptance"], utc=True, errors="coerce").dt.tz_convert("America/New_York").dt.tz_localize(None)
    df["t_et"] = head.fillna(js)
    df["t_source"] = np.where(head.notna(), "header", "json_utc")
    return df[df["t_et"].notna()].reset_index(drop=True)
