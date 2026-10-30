// Browser-side reader for the chart streams of qc/stock_0dte/main.py (ledger docs/research_ledger_stock_0dte.md, 0.7):
// qc/zero_dte/fetch.js generalised to nch charts (main.py's NCH: 100 charts x 10 series, chartcap.py checked that
// 1,000 series keep 4,000 exact points each). Runs in a QuantConnect page that holds the session; api = POST
// /api/v2/<path> with the session cookie.
// SX.fetch(api, pid, bid, st, en, nch): one backtests/chart/read per chart P00.. over [st, en] (unix seconds) with
//   count = 1e7 so the server keeps every point.
// SX.stream(store, nch): orders points by time; slot 0 of each time is the header 8e14 + seq*1000 + k and slots 1..k
//   carry the queue in order. Returns the flat value list plus integrity counters (bad = missing slots or seq gaps).
// SX.save(obj, name): gzipped JSON download (the local parser is quant/strategies/stock_0dte.py).
window.SX = {
  async fetch(api, pid, bid, st, en, nch) {
    const store = {};
    for (let c = 0; c < nch; c++) {
      const nm = 'P' + String(c).padStart(2, '0');
      const q = await api('backtests/chart/read', { projectId: pid, backtestId: bid, name: nm, count: 10000000, start: st, end: en });
      const m = {};
      for (const [sn, v] of Object.entries((q.chart || {}).series || {})) m[sn] = (v.values || []).map(p => Array.isArray(p) ? p : [p.x, p.y]);
      store[nm] = m;
    }
    return store;
  },
  stream(store, nch) {
    const slot = {};
    for (let c = 0; c < nch; c++) {
      const m = store['P' + String(c).padStart(2, '0')] || {};
      for (let j = 0; j < 10; j++) for (const [t, y] of (m['s' + j] || [])) (slot[t] = slot[t] || {})[c * 10 + j] = y;
    }
    const ts = Object.keys(slot).map(Number).sort((a, b) => a - b), vals = [];
    let bad = 0, last = 0, maxPts = 0;
    for (const c of Object.values(store)) for (const s of Object.values(c)) maxPts = Math.max(maxPts, s.length);
    for (const t of ts) {
      const h = slot[t][0];
      if (h === undefined) { bad++; continue; }
      const seq = Math.floor((h - 8e14) / 1000), k = Math.round(h - 8e14 - seq * 1000);
      if (seq !== last + 1) bad++;
      last = seq;
      for (let i = 1; i <= k; i++) { const v = slot[t][i]; if (v === undefined || !Number.isInteger(v)) { bad++; vals.push(null); } else vals.push(v); }
    }
    return { vals, bad, nts: ts.length, lastseq: last, maxPts };
  },
  async save(obj, name) {
    const gz = new Blob([JSON.stringify(obj)]).stream().pipeThrough(new CompressionStream('gzip'));
    const blob = await new Response(gz).blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    return blob.size;
  },
};
