// Decoder + analysis for qc/options_grid/main.py chart streams (ledger docs/research_ledger_qc_options_grid.md).
// Runs in the browser page that holds the QuantConnect session (results never leave the page except the
// compact summaries returned by these functions, which are saved under output/research_only/qc_options_grid/).
// Usage: G.parse(vals) -> run object; G.stats(run) -> per-config stats; G.report(run, st) -> summary object.
(function () {
  const N = 3584, S = ['CSP', 'CC', 'WH', 'PO'], D = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50],
    T = [7, 14, 30, 45], M = ['H', 'TP', 'R', 'SL'], F = ['ALL', 'V20', 'V25', 'VT3', 'BW', 'DD10', 'UP'];
  const NTRIALS = 7168, HURDLE_A = 4.49, HURDLE_OOS = 2.24;
  const cfg = k => { const f = k % 7, m = Math.floor(k / 7) % 4, t = Math.floor(k / 28) % 4, d = Math.floor(k / 112) % 8, s = Math.floor(k / 896); return { s, d, t, m, f }; };
  const name = k => { const c = cfg(k); return `${S[c.s]}-${D[c.d].toFixed(2)}-${T[c.t]}-${M[c.m]}-${F[c.f]}`; };
  const inH1 = ym => ym >= 201203 && ym <= 201812, inH2 = ym => ym >= 201901 && ym <= 202605;

  function parse(v) {
    const months = []; let i = 0;
    while (i < v.length && v[i] >= 9e14 && v[i] < 9.1e14) {
      const ym = v[i] - 9e14, b = v.slice(i + 1, i + 9);
      const bench = b.slice(0, 4).map(x => (x - 5e9) / 1e8), vix = b[4] / 100;
      const bdd = b.slice(5, 8).map(x => [Math.floor(x / 1e4) / 1e4, (x % 1e4) / 1e4]);
      const r = new Float64Array(N), ddh = new Float64Array(N), ddf = new Float64Array(N);
      for (let k = 0; k < N; k++) { const x = v[i + 9 + k]; r[k] = (Math.floor(x / 1e8) - 50000) / 1e4; ddh[k] = Math.floor((x % 1e8) / 1e4) / 1e4; ddf[k] = (x % 1e4) / 1e4; }
      months.push({ ym, bench, vix, bdd, r, ddh, ddf }); i += 9 + N;
    }
    if (v[i] !== 9.1e14) throw new Error('diag marker missing at ' + i);
    const diag = [];
    for (let k = 0; k < N; k++) { const x = v[i + 1 + k]; diag.push({ entries: Math.floor(x / 1e11), inpos: Math.floor((x % 1e11) / 1e8) / 1000, credit: Math.floor((x % 1e8) / 1e3) / 1e5, itm: x % 1e3 }); }
    if (v[i + 1 + N] !== 9.2e14) throw new Error('end marker missing');
    return { months, diag };
  }

  const mean = a => a.reduce((x, y) => x + y, 0) / a.length;
  const sd = a => { const m = mean(a); return Math.sqrt(a.reduce((x, y) => x + (y - m) * (y - m), 0) / (a.length - 1)); };
  const ann = a => Math.pow(a.reduce((x, y) => x * (1 + y), 1), 12 / a.length) - 1;
  const tstat = a => { const s = sd(a); return s > 0 ? mean(a) / (s / Math.sqrt(a.length)) : 0; };
  function moments(a) { const m = mean(a), n = a.length; let m2 = 0, m3 = 0, m4 = 0; for (const x of a) { const d = x - m; m2 += d * d; m3 += d * d * d; m4 += d * d * d * d; } m2 /= n; m3 /= n; m4 /= n; return { skew: m3 / Math.pow(m2, 1.5), kurt: m4 / (m2 * m2) }; }
  function ncdf(x) { const t = 1 / (1 + 0.2316419 * Math.abs(x)); const d = 0.3989423 * Math.exp(-x * x / 2); const p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274)))); return x > 0 ? 1 - p : p; }
  function ninv(p) { let lo = -10, hi = 10; for (let i = 0; i < 100; i++) { const m = (lo + hi) / 2; if (ncdf(m) < p) lo = m; else hi = m; } return (lo + hi) / 2; }

  function stats(run) {
    const ms = run.months, idx1 = [], idx2 = [];
    ms.forEach((m, i) => { if (inH1(m.ym)) idx1.push(i); if (inH2(m.ym)) idx2.push(i); });
    const last1 = idx1[idx1.length - 1], last2 = idx2[idx2.length - 1];
    const bench = j => {
      const h1 = idx1.map(i => ms[i].bench[j]), h2 = idx2.map(i => ms[i].bench[j]);
      const dd = j < 3 ? [ms[last1].bdd[j][0], ms[last2].bdd[j][0], ms[last2].bdd[j][1]] : [0, 0, 0];
      return { a1: ann(h1), a2: ann(h2), aF: ann(h1.concat(h2)), dd1: dd[0], dd2: dd[1], ddF: dd[2] };
    };
    const B = { ONEQ: bench(0), QQQ: bench(1), SPY: bench(2), BIL: bench(3) };
    const J1 = idx1.map(i => ms[i].bench[0]), J2 = idx2.map(i => ms[i].bench[0]), R1 = idx1.map(i => ms[i].bench[3]), R2 = idx2.map(i => ms[i].bench[3]);
    // QQQ buy-and-hold vs ONEQ, for context
    const qx = idx1.concat(idx2).map(i => ms[i].bench[1] - ms[i].bench[0]);
    B.QQQ.tF = tstat(qx);
    const out = [];
    for (let k = 0; k < N; k++) {
      const r1 = idx1.map(i => ms[i].r[k]), r2 = idx2.map(i => ms[i].r[k]);
      const e1 = r1.map((x, i) => x - J1[i]), e2 = r2.map((x, i) => x - J2[i]), eF = e1.concat(e2);
      const x1 = r1.map((x, i) => x - R1[i]), x2 = r2.map((x, i) => x - R2[i]);
      const o = {
        k, a1: ann(r1), a2: ann(r2), aF: ann(r1.concat(r2)), dd1: ms[last1].ddh[k], dd2: ms[last2].ddh[k], ddF: ms[last2].ddf[k],
        t1: tstat(e1), t2: tstat(e2), tF: tstat(eF), v1: sd(r1) * Math.sqrt(12), v2: sd(r2) * Math.sqrt(12), vF: sd(r1.concat(r2)) * Math.sqrt(12),
        sh1: mean(x1) / sd(x1) * Math.sqrt(12), sh2: mean(x2) / sd(x2) * Math.sqrt(12), irm: mean(eF) / sd(eF), e1, e2, ...run.diag[k]
      };
      o.x1 = o.a1 - B.ONEQ.a1; o.x2 = o.a2 - B.ONEQ.a2; o.xF = o.aF - B.ONEQ.aF;
      o.A = o.x1 > 0 && o.x2 > 0 && o.tF >= HURDLE_A;
      o.B = (B.ONEQ.dd1 - o.dd1 >= 0.10 && o.a1 >= B.ONEQ.a1 - 0.03) && (B.ONEQ.dd2 - o.dd2 >= 0.10 && o.a2 >= B.ONEQ.a2 - 0.03);
      o.both = o.x1 > 0 && o.x2 > 0;
      out.push(o);
    }
    return { B, cf: out, n1: idx1.length, n2: idx2.length };
  }

  const pct = x => (x * 100).toFixed(1);
  const row = (o, B) => `${name(o.k)} H1 ${pct(o.a1)}/${pct(-o.dd1)} H2 ${pct(o.a2)}/${pct(-o.dd2)} F ${pct(o.aF)}/${pct(-o.ddF)} t1 ${o.t1.toFixed(2)} t2 ${o.t2.toFixed(2)} tF ${o.tF.toFixed(2)} ent ${o.entries} in ${pct(o.inpos)}%`;
  function median(a) { const b = a.slice().sort((x, y) => x - y); const n = b.length; return n % 2 ? b[(n - 1) / 2] : (b[n / 2 - 1] + b[n / 2]) / 2; }
  function rank(a) { const ix = a.map((x, i) => [x, i]).sort((p, q) => p[0] - q[0]); const r = new Array(a.length); ix.forEach((p, j) => { r[p[1]] = j; }); return r; }
  function corr(a, b) { const ma = mean(a), mb = mean(b); let s = 0, sa = 0, sb = 0; for (let i = 0; i < a.length; i++) { s += (a[i] - ma) * (b[i] - mb); sa += (a[i] - ma) ** 2; sb += (b[i] - mb) ** 2; } return s / Math.sqrt(sa * sb); }

  function report(st, selectable) {
    const B = st.B, C = st.cf, J = B.ONEQ, rep = {};
    rep.bench = Object.fromEntries(Object.entries(B).map(([k, b]) => [k, `H1 ${pct(b.a1)}/${pct(-b.dd1)} H2 ${pct(b.a2)}/${pct(-b.dd2)} F ${pct(b.aF)}/${pct(-b.ddF)}` + (b.tF !== undefined ? ` tF(vsONEQ) ${b.tF.toFixed(2)}` : '')]));
    rep.counts = { A: C.filter(o => o.A).length, B: C.filter(o => o.B).length, bothHalves: C.filter(o => o.both).length, tF_ge_2: C.filter(o => o.tF >= 2).length, tF_ge_hurdle: C.filter(o => o.tF >= HURDLE_A).length, beatF: C.filter(o => o.xF > 0).length, n: C.length };
    rep.byStrategy = S.map((s, si) => { const c = C.filter(o => cfg(o.k).s === si); return `${s}: both ${c.filter(o => o.both).length}/896 medF ${pct(median(c.map(o => o.aF)))} medX1 ${pct(median(c.map(o => o.x1)))} medX2 ${pct(median(c.map(o => o.x2)))} maxtF ${Math.max(...c.map(o => o.tF)).toFixed(2)} B ${c.filter(o => o.B).length}`; });
    const sorted = C.slice().sort((a, b) => a.xF - b.xF); const med = sorted[Math.floor(C.length / 2)];
    rep.median = row(med, B);
    rep.medianStats = `medF ${pct(median(C.map(o => o.aF)))} medX1 ${pct(median(C.map(o => o.x1)))} medX2 ${pct(median(C.map(o => o.x2)))} medDDF ${pct(-median(C.map(o => o.ddF)))} medtF ${median(C.map(o => o.tF)).toFixed(2)}`;
    rep.topTF = C.slice().sort((a, b) => b.tF - a.tF).slice(0, 8).map(o => row(o, B));
    rep.topXF = C.slice().sort((a, b) => b.xF - a.xF).slice(0, 5).map(o => row(o, B));
    rep.bestB = C.filter(o => o.B).slice(0, 5).map(o => row(o, B));
    // deflated Sharpe on the best full-period information ratio (monthly)
    const irs = C.map(o => o.irm).filter(isFinite), V = sd(irs) ** 2, g = 0.5772156649;
    const best = C.slice().sort((a, b) => b.irm - a.irm)[0], eF = best.e1.concat(best.e2), mo = moments(eF), Tn = eF.length;
    const SR0 = Math.sqrt(V) * ((1 - g) * ninv(1 - 1 / NTRIALS) + g * ninv(1 - 1 / (NTRIALS * Math.E)));
    const dsr = ncdf((best.irm - SR0) * Math.sqrt(Tn - 1) / Math.sqrt(1 - mo.skew * best.irm + (mo.kurt - 1) / 4 * best.irm * best.irm));
    rep.dsr = `best IR ${name(best.k)} IRm ${best.irm.toFixed(3)} (ann ${(best.irm * Math.sqrt(12)).toFixed(2)}) SR0m ${SR0.toFixed(3)} skew ${mo.skew.toFixed(2)} kurt ${mo.kurt.toFixed(2)} T ${Tn} DSR ${dsr.toFixed(3)} sdIRm ${Math.sqrt(V).toFixed(3)}`;
    if (selectable) {
      const fold = (selX, selT, selA, selDD, Jsel, testH) => {
        const a = C.slice().sort((p, q) => (q[selX] - p[selX]) || (q[selT] - p[selT]))[0];
        const okB = C.filter(o => o[selA] >= Jsel.a - 0.03).sort((p, q) => (p[selDD] - q[selDD]) || (q[selX] - p[selX]));
        return { A: a, B: okB[0] || null };
      };
      const f1 = fold('x1', 't1', 'a1', 'dd1', { a: J.a1, dd: J.dd1 }), f2 = fold('x2', 't2', 'a2', 'dd2', { a: J.a2, dd: J.dd2 });
      const pooled = f1.A.e2.concat(f2.A.e1), tp = tstat(pooled);
      const passA = f1.A.x2 > 0 && f2.A.x1 > 0 && tp >= HURDLE_OOS;
      const bOK2 = o => o && J.dd2 - o.dd2 >= 0.10 && o.a2 >= J.a2 - 0.03, bOK1 = o => o && J.dd1 - o.dd1 >= 0.10 && o.a1 >= J.a1 - 0.03;
      const passB = bOK2(f1.B) && bOK1(f2.B);
      rep.oos = {
        fold1_A: row(f1.A, B), fold2_A: row(f2.A, B), fold1_B: f1.B ? row(f1.B, B) : 'none', fold2_B: f2.B ? row(f2.B, B) : 'none',
        pooledOOS_t: tp.toFixed(2), pooledOOS_meanExcessAnn: pct(mean(pooled) * 12), passA, passB,
        fold1_A_test: `H2 ${pct(f1.A.a2)} vs ONEQ ${pct(J.a2)} t2 ${f1.A.t2.toFixed(2)} dd ${pct(-f1.A.dd2)} vs ${pct(-J.dd2)}`,
        fold2_A_test: `H1 ${pct(f2.A.a1)} vs ONEQ ${pct(J.a1)} t1 ${f2.A.t1.toFixed(2)} dd ${pct(-f2.A.dd1)} vs ${pct(-J.dd1)}`,
        fold1_B_test: f1.B ? `H2 ${pct(f1.B.a2)} dd ${pct(-f1.B.dd2)} (ONEQ ${pct(J.a2)}/${pct(-J.dd2)}) ok ${bOK2(f1.B)}` : 'none',
        fold2_B_test: f2.B ? `H1 ${pct(f2.B.a1)} dd ${pct(-f2.B.dd1)} (ONEQ ${pct(J.a1)}/${pct(-J.dd1)}) ok ${bOK1(f2.B)}` : 'none',
        keys: [f1.A.k, f2.A.k, f1.B ? f1.B.k : -1, f2.B ? f2.B.k : -1]
      };
      const top = (x, y, q) => { const s = C.slice().sort((p, r) => r[x] - p[x]); const t10 = s.slice(0, 10), t1p = s.slice(0, Math.ceil(C.length * 0.01)); return `top10 ${pct(mean(t10.map(o => o[y])))} top1% ${pct(mean(t1p.map(o => o[y])))} all ${pct(mean(C.map(o => o[y])))}`; };
      rep.persistence = { selH1_testH2_excess: top('x1', 'x2'), selH2_testH1_excess: top('x2', 'x1'), spearman_x1_x2: corr(rank(C.map(o => o.x1)), rank(C.map(o => o.x2))).toFixed(3) };
    }
    return rep;
  }

  // aggregated tables: median full/half excess by two axes (others pooled)
  function table(st, ax1, ax2, field) {
    const L = { s: S, d: D, t: T, m: M, f: F }, out = {};
    for (let i = 0; i < L[ax1].length; i++) for (let j = 0; j < L[ax2].length; j++) {
      const c = st.cf.filter(o => cfg(o.k)[ax1] === i && cfg(o.k)[ax2] === j);
      out[`${L[ax1][i]}|${L[ax2][j]}`] = +(median(c.map(o => o[field])) * 100).toFixed(1);
    }
    return out;
  }
  function tableBy(st, fixAx, fixVal, ax1, ax2, field) {
    const L = { s: S, d: D, t: T, m: M, f: F }, rows = [];
    for (let i = 0; i < L[ax1].length; i++) { const r = []; for (let j = 0; j < L[ax2].length; j++) { const c = st.cf.filter(o => cfg(o.k)[fixAx] === fixVal && cfg(o.k)[ax1] === i && cfg(o.k)[ax2] === j); r.push(+(median(c.map(o => o[field])) * 100).toFixed(1)); } rows.push(r.join(',')); }
    return rows.join(';');
  }
  // compact per-config surface: 2 base64 chars per value
  const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  const enc = (x, lo, step) => { let q = Math.round((x - lo) / step); q = Math.max(0, Math.min(4095, q)); return B64[q >> 6] + B64[q & 63]; };
  function surface(st) {
    // per config: a1,a2 (-100%..+104.75%, 0.05%), dd1,dd2,ddF (0..100%, 0.025%), tF (-20.48..20.47, 0.01), entries, inpos permille
    return st.cf.map(o => enc(o.a1, -1, 0.0005) + enc(o.a2, -1, 0.0005) + enc(o.dd1, 0, 0.00025) + enc(o.dd2, 0, 0.00025) + enc(o.ddF, 0, 0.00025) + enc(o.tF, -20.48, 0.01) + enc(o.entries, 0, 1) + enc(o.inpos, 0, 0.001)).join('');
  }
  window.G = { parse, stats, report, table, tableBy, surface, name, cfg, N };
})();

// Fetch + decode helpers used in the page (same code as run; __api = POST /api/v2/<path> with the session cookie).
// fetchCharts(pid, bid): reads charts P00..P39 via backtests/chart/read; decode(): orders points by time, slot 0 is the
// header 8e14 + seq*1000 + count, slots 1..count carry the payload in queue order. Checks: seq contiguous, no missing slot.
window.GX = {
  fetchCharts: async (api, pid, bid, store, from, to) => {
    for (let c = from; c < to; c++) {
      const nm = 'P' + String(c).padStart(2, '0'); if (store[nm]) continue;
      const q = await api('backtests/chart/read', { projectId: pid, backtestId: bid, name: nm, count: 100000, start: 0, end: 2000000000 });
      const ser = (q.chart || {}).series || {}, m = {};
      for (const [sn, v] of Object.entries(ser)) m[sn] = (v.values || []).map(p => Array.isArray(p) ? p : [p.x, p.y]);
      store[nm] = m;
    }
  },
  decode: (store, NC) => {
    const slot = {};
    for (let c = 0; c < NC; c++) { const m = store['P' + String(c).padStart(2, '0')]; for (let j = 0; j < 10; j++) for (const [t, y] of (m['s' + j] || [])) (slot[t] = slot[t] || {})[c * 10 + j] = y; }
    const ts = Object.keys(slot).map(Number).sort((a, b) => a - b), vals = []; let bad = 0, last = 0;
    for (const t of ts) { const h = slot[t][0]; if (h === undefined) { bad++; continue; } const seq = Math.floor((h - 8e14) / 1000), k = Math.round(h - 8e14 - seq * 1000); if (seq !== last + 1) bad++; last = seq; for (let i = 1; i <= k; i++) { const v = slot[t][i]; if (v === undefined) { bad++; vals.push(NaN); } else vals.push(v); } }
    return { vals, bad, nts: ts.length, lastseq: last };
  }
};

// ---- report-only helpers used for the ledger (same code as run in the page) ----
// beta(run, k, half): OLS of monthly (config - ONEQ) on (ONEQ - BIL); returns total beta (1 + slope),
// annualised alpha (pct) and its t. half: 0 full, 1 = 2012-03..2018-12, 2 = 2019-01..2026-05.
window.GX.beta = (run, k, half) => {
  const ms = run.months.filter(m => half === 1 ? (m.ym <= 201812) : half === 2 ? (m.ym >= 201901) : true);
  const y = ms.map(m => m.r[k] - m.bench[0]), x = ms.map(m => m.bench[0] - m.bench[3]), n = y.length;
  const mx = x.reduce((a, b) => a + b, 0) / n, my = y.reduce((a, b) => a + b, 0) / n;
  let sxy = 0, sxx = 0; for (let i = 0; i < n; i++) { sxy += (x[i] - mx) * (y[i] - my); sxx += (x[i] - mx) ** 2; }
  const b = sxy / sxx, a = my - b * mx; let se = 0; for (let i = 0; i < n; i++) se += (y[i] - a - b * x[i]) ** 2;
  const sea = Math.sqrt(se / (n - 2) * (1 / n + mx * mx / sxx));
  return { b: +(1 + b).toFixed(2), alphaAnn: +(a * 1200).toFixed(1), ta: +(a / sea).toFixed(2) };
};
// yearly(run, keys): calendar-year compounded returns (pct) for ONEQ, QQQ TR and the given configs.
window.GX.yearly = (run, keys) => {
  const ms = run.months, yrs = [...new Set(ms.map(m => Math.floor(m.ym / 100)))];
  const yr = f => yrs.map(y => (100 * (ms.filter(m => Math.floor(m.ym / 100) === y).reduce((a, m) => a * (1 + f(m)), 1) - 1)).toFixed(1)).join(' ');
  return ['year ' + yrs.join(' '), 'ONEQ ' + yr(m => m.bench[0]), 'QQQ ' + yr(m => m.bench[1])].concat(keys.map(k => window.G.name(k) + ' ' + yr(m => m.r[k]))).join('\n');
};
// agg(run, st, rep): the aggregate object saved as output/research_only/qc_options_grid/agg_<run>.json
// (median grids by delta x DTE, filter x management, filter x delta per strategy; one-axis summaries;
// A list; beta/alpha of named, selected and top-t configs). See agg_*.json for the field list.
window.GX.agg = (run, st, rep) => {
  const G = window.G, C = st.cf, S = ['CSP', 'CC', 'WH', 'PO'], D = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50], T = [7, 14, 30, 45], M = ['H', 'TP', 'R', 'SL'], F = ['ALL', 'V20', 'V25', 'VT3', 'BW', 'DD10', 'UP'];
  const med = a => { const b = a.slice().sort((x, y) => x - y), n = b.length; return n ? (n % 2 ? b[(n - 1) / 2] : (b[n / 2 - 1] + b[n / 2]) / 2) : null; };
  const r1 = x => x === null ? null : Math.round(x * 1000) / 10, cf = k => G.cfg(k), beta = window.GX.beta;
  const grid = (si, a1, n1, a2, n2, fn) => { const out = []; for (let i = 0; i < n1; i++) { const row = []; for (let j = 0; j < n2; j++) { const c = C.filter(o => { const q = cf(o.k); return q.s === si && q[a1] === i && q[a2] === j; }); row.push(fn(c)); } out.push(row); } return out; };
  const g = {};
  S.forEach((s, si) => { g[s] = {
    dt_xF: grid(si, 'd', 8, 't', 4, c => r1(med(c.map(o => o.xF)))), dt_x1: grid(si, 'd', 8, 't', 4, c => r1(med(c.map(o => o.x1)))), dt_x2: grid(si, 'd', 8, 't', 4, c => r1(med(c.map(o => o.x2)))),
    dt_both: grid(si, 'd', 8, 't', 4, c => c.filter(o => o.both).length), dt_ddF: grid(si, 'd', 8, 't', 4, c => r1(med(c.map(o => o.ddF)))),
    fm_xF: grid(si, 'f', 7, 'm', 4, c => r1(med(c.map(o => o.xF)))), fd_xF: grid(si, 'f', 7, 'd', 8, c => r1(med(c.map(o => o.xF)))), fm_both: grid(si, 'f', 7, 'm', 4, c => c.filter(o => o.both).length) }; });
  const axes = { s: S, d: D, t: T, m: M, f: F }, one = {};
  for (const [ax, vals] of Object.entries(axes)) one[ax] = vals.map((v, i) => { const c = C.filter(o => cf(o.k)[ax] === i); return `${v}: medXF ${r1(med(c.map(o => o.xF)))} medX1 ${r1(med(c.map(o => o.x1)))} medX2 ${r1(med(c.map(o => o.x2)))} both ${c.filter(o => o.both).length}/${c.length} A ${c.filter(o => o.A).length} maxtF ${Math.max(...c.map(o => o.tF)).toFixed(2)} medDDF ${r1(med(c.map(o => o.ddF)))} medInpos ${r1(med(c.map(o => o.inpos)))} medEnt ${med(c.map(o => o.entries))}`; });
  const sf = S.map((s, si) => F.map((f, fi) => { const c = C.filter(o => { const q = cf(o.k); return q.s === si && q.f === fi; }); return `${c.filter(o => o.A).length}/${c.filter(o => o.both).length}`; }).join(' '));
  const row = o => `${G.name(o.k)} H1 ${r1(o.a1)}/${r1(-o.dd1)} H2 ${r1(o.a2)}/${r1(-o.dd2)} F ${r1(o.aF)}/${r1(-o.ddF)} t1 ${o.t1.toFixed(2)} t2 ${o.t2.toFixed(2)} tF ${o.tF.toFixed(2)} sh ${o.sh1.toFixed(2)}/${o.sh2.toFixed(2)} ent ${o.entries} in ${r1(o.inpos)} cr ${(o.credit * 100).toFixed(3)} itm ${o.itm}`;
  const bestPer = S.map((s, si) => { const c = C.filter(o => cf(o.k).s === si); const bt = c.slice().sort((a, b) => b.tF - a.tF)[0], bx = c.slice().sort((a, b) => b.xF - a.xF)[0], bd = c.slice().sort((a, b) => a.ddF - b.ddF)[0]; return { tF: row(bt), xF: row(bx), minDD: row(bd) }; });
  const Alist = C.filter(o => o.A).sort((a, b) => b.tF - a.tF).map(o => G.name(o.k));
  const ix = (s, d, t, m, f) => ((((s * 8 + d) * 4 + t) * 4 + m) * 7 + f);
  const named = { replica_CSP_025_30_H_ALL: ix(0, 4, 2, 0, 0), CC_025_30_H_ALL: ix(1, 4, 2, 0, 0), WH_025_30_H_ALL: ix(2, 4, 2, 0, 0), PO_025_30_H_ALL: ix(3, 4, 2, 0, 0), CSP_050_30_H_ALL: ix(0, 7, 2, 0, 0), CC_050_30_H_ALL: ix(1, 7, 2, 0, 0) };
  const namedRows = Object.fromEntries(Object.entries(named).map(([n, k]) => [n, row(C[k]) + ' beta ' + JSON.stringify(beta(run, k, 0))]));
  const sel = rep.oos ? rep.oos.keys.filter(k => k >= 0).map(k => row(C[k]) + ' betaF ' + JSON.stringify(beta(run, k, 0)) + ' H1 ' + JSON.stringify(beta(run, k, 1)) + ' H2 ' + JSON.stringify(beta(run, k, 2))) : [];
  const topBeta = C.slice().sort((a, b) => b.tF - a.tF).slice(0, 5).map(o => G.name(o.k) + ' ' + JSON.stringify(beta(run, o.k, 0)));
  const Ab = C.filter(o => o.A).map(o => beta(run, o.k, 0));
  const aKeep = `A passers ${Ab.length}: alpha t>=4.49 ${Ab.filter(x => x.ta >= 4.49).length}, >=2 ${Ab.filter(x => x.ta >= 2).length}, median beta ${med(Ab.map(x => x.b))}`;
  const Bj = st.B.ONEQ, benchJ = { a1: Bj.a1, a2: Bj.a2, aF: Bj.aF, dd1: Bj.dd1, dd2: Bj.dd2, ddF: Bj.ddF };
  return { bench: rep.bench, benchJ, counts: rep.counts, byStrategy: rep.byStrategy, median: rep.median, medianStats: rep.medianStats, dsr: rep.dsr, oos: rep.oos, persistence: rep.persistence, sf, one, bestPer, Alist, aKeep, namedRows, sel, topBeta, grids: g };
};
