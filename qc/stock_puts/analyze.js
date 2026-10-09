// Decoder + analysis for qc/stock_puts/main.py chart streams (ledger docs/research_ledger_qc_stock_puts.md).
// Runs in the browser page holding the QuantConnect session; only compact summaries leave the page.
// Usage: SP.fetch(api, pid, bid) -> store; SP.decode(store) -> vals; SP.parse(vals) -> run; SP.stats(run) -> st; SP.report(run, st)
(function () {
  const U = ['A5', 'A10', 'N5', 'N10'], S = ['CSP', 'WH', 'PO'], D = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40],
    T = [7, 14, 30, 45], M = ['H', 'TP', 'R', 'SL'], F = ['ALL', 'V20', 'IVR', 'UP', 'DD10'];
  const K = 1680, C = 3360, NB = 4 * C, NTR = 4 * K, HA = 4.48, HOOS = 2.24;
  // trial index j = u*1680 + k, k = (((s*7+d)*4+t)*4+m)*5+f ; book index (fill x) = u*3360 + 2k + x
  const cfg = j => { const u = Math.floor(j / K), k = j % K; return { u, f: k % 5, m: Math.floor(k / 5) % 4, t: Math.floor(k / 20) % 4, d: Math.floor(k / 80) % 7, s: Math.floor(k / 560) }; };
  const name = j => { const c = cfg(j); return `${U[c.u]}-${S[c.s]}-${D[c.d].toFixed(2)}-${T[c.t]}-${M[c.m]}-${F[c.f]}`; };
  const idx = (u, s, d, t, m, f) => u * K + (((s * 7 + d) * 4 + t) * 4 + m) * 5 + f;
  const inH1 = ym => ym >= 201203 && ym <= 201812, inH2 = ym => ym >= 201901 && ym <= 202605;
  const unpack = (x, k, base) => { const o = new Array(k); for (let i = k - 1; i >= 0; i--) { o[i] = x % base; x = Math.floor(x / base); } return o; };

  async function fetch(api, pid, bid, store) {
    store = store || {};
    for (let c = 0; c < 40; c++) {
      const nm = 'P' + String(c).padStart(2, '0'); if (store[nm]) continue;
      const q = await api('backtests/chart/read', { projectId: pid, backtestId: bid, name: nm, count: 100000, start: 0, end: 2000000000 });
      const ser = (q.chart || {}).series || {}, m = {};
      for (const [sn, v] of Object.entries(ser)) m[sn] = (v.values || []).map(p => Array.isArray(p) ? p : [p.x, p.y]);
      store[nm] = m;
    }
    return store;
  }
  function decode(store) {
    const slot = {};
    for (let c = 0; c < 40; c++) { const m = store['P' + String(c).padStart(2, '0')] || {}; for (let j = 0; j < 10; j++) for (const [t, y] of (m['s' + j] || [])) (slot[t] = slot[t] || {})[c * 10 + j] = y; }
    const ts = Object.keys(slot).map(Number).sort((a, b) => a - b), vals = []; let bad = 0, last = 0;
    for (const t of ts) { const h = slot[t][0]; if (h === undefined) { bad++; continue; } const seq = Math.floor((h - 8e14) / 1000), k = Math.round(h - 8e14 - seq * 1000); if (seq !== last + 1) bad++; last = seq; for (let i = 1; i <= k; i++) { const v = slot[t][i]; if (v === undefined) { bad++; vals.push(NaN); } else vals.push(v); } }
    return { vals, bad, nts: ts.length, lastseq: last };
  }
  function parse(v) {
    const months = []; let i = 0; const run = { months, ddH1: null, bddH1: null, diag: null, ddH2: null, ddF: null, bdd2: null };
    const nPack = Math.ceil(NB / 3), nDD = Math.ceil(NTR / 5);
    const readDD = (at) => { const o = new Float64Array(NTR); for (let q = 0; q < nDD; q++) { const a = unpack(v[at + q], 5, 1000); for (let z = 0; z < 5; z++) { const j = q * 5 + z; if (j < NTR) o[j] = a[z] / 1000; } } return o; };
    while (i < v.length) {
      const x = v[i];
      if (x >= 9e14 && x < 9.1e14) {
        const ym = x - 9e14, b = v.slice(i + 1, i + 13);
        const bench = b.slice(0, 3).map(y => (y - 5e9) / 1e8), vix = b[3] / 100, basket = b.slice(4, 8).map(y => (y - 5e9) / 1e8), nm = unpack(b[8], 4, 100).reverse();
        const r = new Float64Array(NB);
        for (let q = 0; q < nPack; q++) { const a = unpack(v[i + 10 + q], 3, 100000); for (let z = 0; z < 3; z++) { const j = q * 3 + z; if (j < NB) r[j] = (a[z] - 50000) / 1e4; } }
        months.push({ ym, bench, vix, basket, nm, r }); i += 10 + nPack;
      } else if (x === 9.3e14) { run.ddH1 = readDD(i + 1); run.bddH1 = v.slice(i + 1 + nDD, i + 7 + nDD).map(y => y / 1e4); i += 7 + nDD; }
      else if (x === 9.4e14) {
        const dg = []; for (let c = 0; c < C; c++) { const a = v[i + 1 + c], b2 = v[i + 1 + C + c]; dg.push({ ent: Math.floor(a / 1e8), itm: Math.floor((a % 1e8) / 1e3), inpos: (a % 1e3) / 1000, early: Math.floor(b2 / 1e5), credit: (b2 % 1e5) / 1e5 }); }
        run.diag = dg; i += 1 + 2 * C;
      } else if (x === 9.5e14) {
        run.ddH2 = readDD(i + 1); run.ddF = readDD(i + 1 + nDD);
        run.bdd2 = v.slice(i + 1 + 2 * nDD, i + 7 + 2 * nDD).map(y => [Math.floor(y / 1e4) / 1e4, (y % 1e4) / 1e4]); i += 7 + 2 * nDD;
        if (v[i] !== 9.6e14) throw new Error('end marker missing at ' + i); i++; run.complete = true; break;
      } else throw new Error('unexpected ' + x + ' at ' + i);
    }
    return run;
  }
  const mean = a => a.reduce((x, y) => x + y, 0) / a.length;
  const sd = a => { const m = mean(a); return Math.sqrt(a.reduce((x, y) => x + (y - m) * (y - m), 0) / (a.length - 1)); };
  const ann = a => Math.pow(a.reduce((x, y) => x * (1 + y), 1), 12 / a.length) - 1;
  const tstat = a => { const s = sd(a); return s > 0 ? mean(a) / (s / Math.sqrt(a.length)) : 0; };
  function moments(a) { const m = mean(a), n = a.length; let m2 = 0, m3 = 0, m4 = 0; for (const x of a) { const d = x - m; m2 += d * d; m3 += d * d * d; m4 += d * d * d * d; } m2 /= n; m3 /= n; m4 /= n; return { skew: m3 / Math.pow(m2, 1.5), kurt: m4 / (m2 * m2) }; }
  function ncdf(x) { const t = 1 / (1 + 0.2316419 * Math.abs(x)); const d = 0.3989423 * Math.exp(-x * x / 2); const p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274)))); return x > 0 ? 1 - p : p; }
  function ninv(p) { let lo = -10, hi = 10; for (let i = 0; i < 100; i++) { const m = (lo + hi) / 2; if (ncdf(m) < p) lo = m; else hi = m; } return (lo + hi) / 2; }
  const median = a => { const b = a.slice().sort((x, y) => x - y), n = b.length; return n ? (n % 2 ? b[(n - 1) / 2] : (b[n / 2 - 1] + b[n / 2]) / 2) : NaN; };
  function rank(a) { const ix = a.map((x, i) => [x, i]).sort((p, q) => p[0] - q[0]); const r = new Array(a.length); ix.forEach((p, j) => { r[p[1]] = j; }); return r; }
  function corr(a, b) { const ma = mean(a), mb = mean(b); let s = 0, sa = 0, sb = 0; for (let i = 0; i < a.length; i++) { s += (a[i] - ma) * (b[i] - mb); sa += (a[i] - ma) ** 2; sb += (b[i] - mb) ** 2; } return s / Math.sqrt(sa * sb); }
  // OLS of (y - rf) on (x - rf): total beta, annualised alpha (pct), alpha t
  function reg(y, x, rf) {
    const Y = y.map((v, i) => v - rf[i]), X = x.map((v, i) => v - rf[i]), n = Y.length, mx = mean(X), my = mean(Y);
    let sxy = 0, sxx = 0; for (let i = 0; i < n; i++) { sxy += (X[i] - mx) * (Y[i] - my); sxx += (X[i] - mx) ** 2; }
    const b = sxy / sxx, a = my - b * mx; let se = 0; for (let i = 0; i < n; i++) se += (Y[i] - a - b * X[i]) ** 2;
    const sea = Math.sqrt(se / (n - 2) * (1 / n + mx * mx / sxx));
    return { b, a: a * 12, ta: a / sea };
  }
  const bookIdx = (j, x) => { const u = Math.floor(j / K), k = j % K; return u * C + 2 * k + x; };

  function stats(run, x) {
    x = x || 0;
    const ms = run.months, i1 = [], i2 = [];
    ms.forEach((m, i) => { if (inH1(m.ym)) i1.push(i); if (inH2(m.ym)) i2.push(i); });
    const iF = i1.concat(i2);
    const ser = f => ({ h1: i1.map(i => f(ms[i])), h2: i2.map(i => f(ms[i])) });
    const J = ser(m => m.bench[0]), Q = ser(m => m.bench[1]), R = ser(m => m.bench[2]);
    const BK = [0, 1, 2, 3].map(u => ser(m => m.basket[u]));
    const bsum = (s, dd) => ({ a1: ann(s.h1), a2: ann(s.h2), aF: ann(s.h1.concat(s.h2)), dd1: dd ? dd[0] : NaN, dd2: dd ? dd[1] : NaN, ddF: dd ? dd[2] : NaN });
    const bd = j => run.bddH1 && run.bdd2 ? [run.bddH1[j], run.bdd2[j][0], run.bdd2[j][1]] : null;
    const B = { ONEQ: bsum(J, bd(4)), QQQ: bsum(Q, bd(5)), BIL: bsum(R, null) };
    U.forEach((u, k) => { B['EW_' + u] = bsum(BK[k], bd(k)); });
    const JF = J.h1.concat(J.h2), RF = R.h1.concat(R.h2);
    B.QQQ.tF = tstat(Q.h1.concat(Q.h2).map((v, i) => v - JF[i]));
    U.forEach((u, k) => { const s = BK[k].h1.concat(BK[k].h2); B['EW_' + u].tF = tstat(s.map((v, i) => v - JF[i])); const rr = reg(s, JF, RF); B['EW_' + u].betaJ = rr.b; B['EW_' + u].alphaJ = rr.a; B['EW_' + u].taJ = rr.ta; });
    const out = [];
    for (let j = 0; j < NTR; j++) {
      const bi = bookIdx(j, x), u = Math.floor(j / K);
      const r1 = i1.map(i => ms[i].r[bi]), r2 = i2.map(i => ms[i].r[bi]), rF = r1.concat(r2);
      const e1 = r1.map((v, i) => v - J.h1[i]), e2 = r2.map((v, i) => v - J.h2[i]), eF = e1.concat(e2);
      const o = { j, a1: ann(r1), a2: ann(r2), aF: ann(rF), t1: tstat(e1), t2: tstat(e2), tF: tstat(eF), irm: mean(eF) / sd(eF), e1, e2, vF: sd(rF) * Math.sqrt(12) };
      if (x === 0 && run.ddH1 && run.ddH2) { o.dd1 = run.ddH1[j]; o.dd2 = run.ddH2[j]; o.ddF = run.ddF[j]; } else { o.dd1 = o.dd2 = o.ddF = NaN; }
      o.x1 = o.a1 - B.ONEQ.a1; o.x2 = o.a2 - B.ONEQ.a2; o.xF = o.aF - B.ONEQ.aF;
      o.both = o.x1 > 0 && o.x2 > 0; o.A = o.both && o.tF >= HA;
      o.B = (B.ONEQ.dd1 - o.dd1 >= 0.10 && o.a1 >= B.ONEQ.a1 - 0.03) && (B.ONEQ.dd2 - o.dd2 >= 0.10 && o.a2 >= B.ONEQ.a2 - 0.03);
      const bk = BK[u].h1.concat(BK[u].h2);
      const rb = reg(rF, bk, RF), rj = reg(rF, JF, RF);
      o.bB = rb.b; o.aB = rb.a; o.taB = rb.ta; o.bJ = rj.b; o.aJ = rj.a; o.taJ = rj.ta;
      o.xB = o.aF - B['EW_' + U[u]].aF;
      out.push(o);
    }
    return { B, cf: out, n1: i1.length, n2: i2.length, x };
  }
  const pct = v => (v * 100).toFixed(1);
  const row = o => `${name(o.j)} H1 ${pct(o.a1)}/${pct(-o.dd1)} H2 ${pct(o.a2)}/${pct(-o.dd2)} F ${pct(o.aF)}/${pct(-o.ddF)} t1 ${o.t1.toFixed(2)} t2 ${o.t2.toFixed(2)} tF ${o.tF.toFixed(2)} vsEW ${pct(o.xB)} bEW ${o.bB.toFixed(2)} aEW ${pct(o.aB)}(t${o.taB.toFixed(2)}) bJ ${o.bJ.toFixed(2)} aJ ${pct(o.aJ)}(t${o.taJ.toFixed(2)})`;

  function report(run, st) {
    const B = st.B, Cf = st.cf, J = B.ONEQ, rep = {};
    rep.bench = Object.fromEntries(Object.entries(B).map(([k, b]) => [k, `H1 ${pct(b.a1)}/${pct(-b.dd1)} H2 ${pct(b.a2)}/${pct(-b.dd2)} F ${pct(b.aF)}/${pct(-b.ddF)}` + (b.tF !== undefined ? ` tF ${b.tF.toFixed(2)}` : '') + (b.betaJ !== undefined ? ` bJ ${b.betaJ.toFixed(2)} aJ ${pct(b.alphaJ)} t ${b.taJ.toFixed(2)}` : '')]));
    rep.counts = { n: Cf.length, A: Cf.filter(o => o.A).length, B: Cf.filter(o => o.B).length, both: Cf.filter(o => o.both).length, beatF: Cf.filter(o => o.xF > 0).length, tFge2: Cf.filter(o => o.tF >= 2).length, beatEW_F: Cf.filter(o => o.xB > 0).length, aEW_t_ge_hurdle: Cf.filter(o => o.taB >= HA).length, aEW_t_ge2: Cf.filter(o => o.taB >= 2).length, aEW_t_le_m2: Cf.filter(o => o.taB <= -2).length };
    const sorted = Cf.slice().sort((a, b) => a.xF - b.xF);
    rep.median = row(sorted[Math.floor(Cf.length / 2)]);
    rep.medianStats = `medF ${pct(median(Cf.map(o => o.aF)))} medX1 ${pct(median(Cf.map(o => o.x1)))} medX2 ${pct(median(Cf.map(o => o.x2)))} medDDF ${pct(-median(Cf.map(o => o.ddF)))} medtF ${median(Cf.map(o => o.tF)).toFixed(2)} medBetaEW ${median(Cf.map(o => o.bB)).toFixed(2)} medAlphaEW ${pct(median(Cf.map(o => o.aB)))} medBetaJ ${median(Cf.map(o => o.bJ)).toFixed(2)}`;
    rep.topTF = Cf.slice().sort((a, b) => b.tF - a.tF).slice(0, 6).map(row);
    rep.topAlphaEW = Cf.slice().sort((a, b) => b.taB - a.taB).slice(0, 5).map(row);
    const irs = Cf.map(o => o.irm).filter(isFinite), V = sd(irs) ** 2, g = 0.5772156649;
    const best = Cf.slice().sort((a, b) => b.irm - a.irm)[0], eF = best.e1.concat(best.e2), mo = moments(eF), Tn = eF.length;
    const SR0 = Math.sqrt(V) * ((1 - g) * ninv(1 - 1 / NTR) + g * ninv(1 - 1 / (NTR * Math.E)));
    const dsr = ncdf((best.irm - SR0) * Math.sqrt(Tn - 1) / Math.sqrt(1 - mo.skew * best.irm + (mo.kurt - 1) / 4 * best.irm * best.irm));
    rep.dsr = `best IR ${name(best.j)} IRm ${best.irm.toFixed(3)} SR0m ${SR0.toFixed(3)} skew ${mo.skew.toFixed(2)} kurt ${mo.kurt.toFixed(2)} T ${Tn} DSR ${dsr.toFixed(3)}`;
    if (st.x === 0) {
      const fold = (sx, stt, sa) => { const a = Cf.slice().sort((p, q) => (q[sx] - p[sx]) || (q[stt] - p[stt]))[0]; const dk = sa === 'a1' ? 'dd1' : 'dd2', Ja = sa === 'a1' ? J.a1 : J.a2; const okB = Cf.filter(o => o[sa] >= Ja - 0.03).sort((p, q) => (p[dk] - q[dk]) || (q[sx] - p[sx])); return { A: a, B: okB[0] || null }; };
      const f1 = fold('x1', 't1', 'a1'), f2 = fold('x2', 't2', 'a2');
      const pooled = f1.A.e2.concat(f2.A.e1), tp = tstat(pooled);
      const bOK2 = o => o && J.dd2 - o.dd2 >= 0.10 && o.a2 >= J.a2 - 0.03, bOK1 = o => o && J.dd1 - o.dd1 >= 0.10 && o.a1 >= J.a1 - 0.03;
      rep.oos = { fold1_A: row(f1.A), fold2_A: row(f2.A), fold1_B: f1.B ? row(f1.B) : 'none', fold2_B: f2.B ? row(f2.B) : 'none', pooled_t: tp.toFixed(2), pooled_excess_ann: pct(mean(pooled) * 12),
        passA: f1.A.x2 > 0 && f2.A.x1 > 0 && tp >= HOOS, passB: !!(bOK2(f1.B) && bOK1(f2.B)), keys: [f1.A.j, f2.A.j, f1.B ? f1.B.j : -1, f2.B ? f2.B.j : -1] };
      const top = (sx, ty) => { const s = Cf.slice().sort((p, r) => r[sx] - p[sx]); return `top10 ${pct(mean(s.slice(0, 10).map(o => o[ty])))} top1% ${pct(mean(s.slice(0, Math.ceil(Cf.length / 100)).map(o => o[ty])))} all ${pct(mean(Cf.map(o => o[ty])))}`; };
      rep.persist = { h1toh2: top('x1', 'x2'), h2toh1: top('x2', 'x1'), spearman: corr(rank(Cf.map(o => o.x1)), rank(Cf.map(o => o.x2))).toFixed(3) };
    }
    return rep;
  }
  // one-axis and two-axis median tables
  function axes(st) {
    const Cf = st.cf, L = { u: U, s: S, d: D, t: T, m: M, f: F }, out = {};
    for (const [ax, vals] of Object.entries(L)) out[ax] = vals.map((v, i) => { const c = Cf.filter(o => cfg(o.j)[ax] === i); return `${v}: medXF ${pct(median(c.map(o => o.xF)))} medX1 ${pct(median(c.map(o => o.x1)))} medX2 ${pct(median(c.map(o => o.x2)))} medVsEW ${pct(median(c.map(o => o.xB)))} both ${c.filter(o => o.both).length}/${c.length} A ${c.filter(o => o.A).length} B ${c.filter(o => o.B).length} medDDF ${pct(-median(c.map(o => o.ddF)))} medBetaEW ${median(c.map(o => o.bB)).toFixed(2)} medAlphaEW ${pct(median(c.map(o => o.aB)))} maxtF ${Math.max(...c.map(o => o.tF)).toFixed(2)}`; });
    return out;
  }
  function grid(st, fix, a1, a2, field) {
    const L = { u: U, s: S, d: D, t: T, m: M, f: F }, rows = [];
    for (let i = 0; i < L[a1].length; i++) { const r = []; for (let j = 0; j < L[a2].length; j++) { const c = st.cf.filter(o => { const q = cfg(o.j); return Object.entries(fix).every(([k, v]) => q[k] === v) && q[a1] === i && q[a2] === j; }); r.push(field === 'both' ? c.filter(o => o.both).length : +(median(c.map(o => o[field])) * 100).toFixed(1)); } rows.push(r); }
    return rows;
  }
  window.SP = { fetch, decode, parse, stats, report, axes, grid, name, cfg, idx, row, reg, bookIdx, K, C, NTR };
})();
