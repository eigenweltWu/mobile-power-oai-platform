// OAI channel impulse response (CIR) daemon.
//
// Reads the complex UL channel impulse response from the gNB websrv scope
// (ws://127.0.0.1:8090/softscope, chartid=5) and serves the latest CIR plus
// multipath metrics as JSON over HTTP (default 0.0.0.0:8091). The platform
// backend polls GET /channel and stores the result in the oai_channel table.
//
// The CIR is the complex time-domain channel estimate (gNBulDelay), so
// metrics are the power-delay-profile derived:
//   - rmsDelayNs  : RMS delay spread (circularly aligned to the peak)
//   - kFactorDb   : Ricean K-factor (peak vs remaining tapped power)
//   - tapCount    : taps above the noise floor (within 25 dB of peak)
//   - peakDb      : strongest tap power (dB, |h|^2)
//   - noiseDb     : estimated noise floor (median of |h|^2)
const http = require('http');

const CTRL = 'http://127.0.0.1:8090/oaisoftmodem/scopectrl/';
const WS = 'ws://127.0.0.1:8090/softscope';
const LISTEN_PORT = parseInt(process.env.OAI_CHANNEL_PORT || '8091', 10);
const LISTEN_HOST = process.env.OAI_CHANNEL_HOST || '0.0.0.0';

const NFFT = 4096;       // ofdm_symbol_size for 106 PRB @ 30 kHz
const SCS_HZ = 30000;    // subcarrier spacing
const DT_NS = 1e9 / (NFFT * SCS_HZ); // ~8.138 ns per time-domain sample

let latest = {
  ok: false,
  tsUtc: null,
  nSamples: 0,
  metrics: null,
  cir: null, // downsampled |h| for compactness, or full Re/Im
  error: '',
};

function metricsFrom(re, im) {
  const n = re.length;
  const p = new Float64Array(n);
  let peak = 0;
  let peakIdx = 0;
  for (let i = 0; i < n; i++) {
    const v = re[i] * re[i] + im[i] * im[i];
    p[i] = v;
    if (v > peak) { peak = v; peakIdx = i; }
  }
  if (peak <= 0) return null;

  // Noise floor: median of |h|^2 (sparse channel => median ~ noise).
  const sorted = Array.from(p).sort((a, b) => a - b);
  const noise = sorted[Math.floor(n / 2)];

  // Real taps: above max(noise + 6 dB, peak - 25 dB).
  const thresh = Math.max(noise * 4.0, peak * 0.00316);
  let sumP = 0;
  let sumTau = 0;
  let taps = 0;
  for (let i = 0; i < n; i++) {
    if (p[i] >= thresh) {
      // circular delay relative to the peak tap
      let d = i - peakIdx;
      if (d < 0) d += n;
      const tau = d * DT_NS;
      sumP += p[i];
      sumTau += tau * p[i];
      taps += 1;
    }
  }
  if (sumP <= 0) return null;

  const tauMean = sumTau / sumP;
  let varTau = 0;
  for (let i = 0; i < n; i++) {
    if (p[i] >= thresh) {
      let d = i - peakIdx;
      if (d < 0) d += n;
      const tau = d * DT_NS;
      varTau += (tau - tauMean) * (tau - tauMean) * p[i];
    }
  }
  const rms = Math.sqrt(varTau / sumP);
  const rest = sumP - peak;
  const kLin = rest > 0 ? peak / rest : Infinity;

  return {
    peakDb: 10 * Math.log10(peak),
    noiseDb: 10 * Math.log10(Math.max(noise, 1e-30)),
    rmsDelayNs: rms,
    kFactorDb: isFinite(kLin) ? 10 * Math.log10(kLin) : null,
    tapCount: taps,
    peakIdx,
    meanDelayNs: tauMean,
  };
}

async function ctrlPost(name, value, graphid = 0) {
  await fetch(CTRL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, value, graphid }),
  });
}

let ws = null;
let restartTimer = null;

async function connect() {
  try {
    const desc = await (await fetch(CTRL)).json();
    const graphs = desc.graphs || [];
    const cir = graphs.filter((g) => (g.title || '').includes('Impulse Response'));
    const cirIdx = cir.length ? cir[0].srvidx : -1;
    if (cirIdx < 0) {
      latest = { ...latest, ok: false, error: 'CIR graph not found in scope description' };
      scheduleRestart();
      return;
    }

    ws = new WebSocket(WS);
    ws.binaryType = 'arraybuffer';

    ws.onopen = async () => {
      try {
        const cmds = [
          ['TargetSelect', '0'], ['refrate', '10'], ['DataAck', 'false'],
          ['xmin', '-32767'], ['xmax', '32767'], ['ymin', '-32767'], ['ymax', '32767'],
          ['llrythresh', '5'], ['llrxmin', '0'], ['llrxmax', '200000'],
        ];
        for (const [n, v] of cmds) await ctrlPost(n, v);
        await ctrlPost('startstop', 'start', 0);
        for (const g of graphs) {
          await ctrlPost('enabled', g.srvidx === cirIdx ? 'true' : 'false', g.srvidx);
        }
        latest = { ...latest, ok: true, error: '', cirIdx };
      } catch (e) {
        latest = { ...latest, ok: false, error: String(e && e.message) };
        scheduleRestart();
      }
    };

    ws.onmessage = (ev) => {
      const buf = ev.data;
      if (buf.byteLength < 8) return;
      const view = new DataView(buf);
      const type = view.getUint8(1);
      if (type !== 10) return;
      const chart = view.getUint8(3);
      if (chart !== 5) return; // SCOPEMSG_DATAID_CIR
      const payload = new DataView(buf, 8);
      const n = Math.floor((buf.byteLength - 8) / 8);
      if (n === 0) return;
      const re = new Float32Array(n);
      const im = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        re[i] = payload.getFloat32(i * 8, true);
        im[i] = payload.getFloat32(i * 8 + 4, true);
      }
      const m = metricsFrom(re, im);
      latest = {
        ok: true,
        tsUtc: new Date().toISOString(),
        nSamples: n,
        dtNs: DT_NS,
        metrics: m,
        cirRe: Array.from(re),
        cirIm: Array.from(im),
        error: '',
      };
    };

    ws.onerror = () => { latest = { ...latest, ok: false, error: 'ws error' }; };
    ws.onclose = () => { latest = { ...latest, ok: false }; scheduleRestart(); };
  } catch (e) {
    latest = { ...latest, ok: false, error: String(e && e.message) };
    scheduleRestart();
  }
}

function scheduleRestart() {
  if (restartTimer) return;
  restartTimer = setTimeout(() => {
    restartTimer = null;
    try { if (ws) ws.close(); } catch (_) { /* ignore */ }
    connect();
  }, 3000);
}

const server = http.createServer((req, res) => {
  if (req.url === '/channel' || req.url === '/') {
    res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
    res.end(JSON.stringify(latest));
    return;
  }
  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: 'not found' }));
});

server.listen(LISTEN_PORT, LISTEN_HOST, () => {
  console.log(`oai_channel_daemon listening on http://${LISTEN_HOST}:${LISTEN_PORT}/channel`);
  connect();
});
