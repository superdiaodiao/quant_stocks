# region imports
from AlgorithmImports import *
# endregion
# Constants, config indexing and the per-name Sleeve state for main.py (split because QC caps files at 32,000 chars).
import math
import time as _time
from datetime import date, timedelta
import numpy as np

# Registered in docs/research_ledger_qc_stock_puts.md (section 0) before any grid result.
MECH = False       # True only for the registered 2012-02..2012-06 mechanism check

STRATS = ("CSP", "WH", "PO")
DELTAS = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40)
TDTE = (7, 14, 30, 45)
WIN = ((4, 10), (10, 18), (24, 38), (38, 52))
ROLL = (3, 7, 14, 21)
MGMT = ("H", "TP", "R", "SL")
FILTS = ("ALL", "V20", "IVR", "UP", "DD10")
FILLS = (0.25, 0.5)
UNIS = ("A5", "A10", "N5", "N10")
NS, ND, NT, NM, NF, NX = 3, 7, 4, 4, 5, 2
C = NS * ND * NT * NM * NF * NX          # 3360 sleeve configs per name
NU = len(UNIS)
NB = NU * C                               # 13440 books (6720 at fill 0.25 are the registered trials)

CAP = 100_000.0               # notional sleeve capital (only matters for per-contract fees)
EQC = 0.0011                  # stock trade cost (0.1% slippage + ~0.01% IBKR per-share fee)
NEG = 0.015 / 252             # extra daily financing on negative cash (benchmark + 1.5%/yr)
START = date(2012, 2, 1)
END_BOOKS = date(2012, 6, 29) if MECH else date(2026, 5, 29)   # QC daily data ends 2026-07-09
END_ALGO = date(2012, 8, 31) if MECH else date(2026, 7, 10)
DIAG_YM = (2012, 5) if MECH else (2025, 12)
SLOTS = 400                   # 40 charts x 10 series, slot 0 = header

# config index c = ((((s*ND + d)*NT + t)*NM + m)*NF + f)*NX + x
_ix = np.arange(C)
CX = _ix % NX
CF = (_ix // NX) % NF
CM = (_ix // (NX * NF)) % NM
CT = (_ix // (NX * NF * NM)) % NT
CD = (_ix // (NX * NF * NM * NT)) % ND
CS = _ix // (NX * NF * NM * NT * ND)
FILLV = np.array(FILLS)[CX]
ISCSP, ISWH, ISPO = CS == 0, CS == 1, CS == 2
ROLLV = np.array(ROLL)[CT]
DT = CD * NT + CT                         # (d, t) cell
REPL = int(((((0 * ND + 4) * NT + 2) * NM + 0) * NF + 0) * NX + 0)   # CSP-0.25-30-H-ALL fill 0.25


def fees(px):
    return np.where(px >= 0.10, 0.65, np.where(px >= 0.05, 0.50, 0.25))


class Sleeve:
    """State of all C configs for one name (scale-free paper sleeves, fractional contracts)."""
    def __init__(self, sym):
        self.sym = sym
        self.cash = np.full(C, CAP)
        self.sh = np.zeros(C)
        self.n = np.zeros(C)
        self.cid = np.full(C, -1, dtype=np.int64)
        self.sv = np.zeros(C)        # sale price
        self.K = np.zeros(C)
        self.eff = np.zeros(C, dtype=np.int64)
        self.rt = np.zeros(C, dtype=np.int64)   # 0 put, 1 call
        self.mark = np.zeros(C)
        self.spr = np.zeros(C)
        self.nav = np.full(C, CAP)
        self.base = np.full(C, CAP)
        self.new = True
        self.block = 0               # days with no entries (split)
        self.ctab = {}               # contract value -> id
        self.csym = []               # id -> Symbol
        self.ucl = []                # split-adjusted closes (prior days)
        self.ivs = []                # ATM IV history
        self.tri = 1.0               # total-return index
        self.trib = 1.0              # tri at month start
        self.pS = None
        self.chosen = {}             # window index -> expiry date (set by the universe filter)

    def cidx(self, c):
        v = c.symbol.value
        k = self.ctab.get(v)
        if k is None:
            k = len(self.csym)
            self.ctab[v] = k
            self.csym.append(c.symbol)
        return k

    def compact(self):
        held = np.unique(self.cid[self.n > 0])
        keep = {int(k): i for i, k in enumerate(held)}
        newsym = [self.csym[int(k)] for k in held]
        m = self.n > 0
        if m.any():
            self.cid[m] = np.array([keep[int(k)] for k in self.cid[m]], dtype=np.int64)
        self.cid[~m] = -1
        self.csym = newsym
        self.ctab = {s.value: i for i, s in enumerate(newsym)}


