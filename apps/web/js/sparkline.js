// sparkline.js — tiny SVG sparkline for live run history
// No dependencies, zero-build friendly. Renders cumulative cost/tokens.

export function renderSparkline(container, samples, opts = {}) {
  if (!container) return;
  const width = opts.width || 120;
  const height = opts.height || 28;
  const stroke = opts.stroke || 'var(--accent-emerald)';
  const fill = opts.fill || 'rgba(16,185,129,0.08)';

  if (!samples || samples.length < 2) {
    container.innerHTML = '<span class="sparkline-empty">—</span>';
    return;
  }

  // Build cumulative cost series
  let cum = 0;
  const points = samples.map(s => {
    cum += Number(s.cost_usd || 0);
    return cum;
  });

  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;

  const stepX = width / (points.length - 1);
  const coords = points.map((v, i) => {
    const x = i * stepX;
    const y = height - ((v - min) / range) * (height - 4) - 2;
    return [x, y];
  });

  const pathD = coords.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const areaD = `${pathD} L${width},${height} L0,${height} Z`;

  container.innerHTML = `
    <svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" class="sparkline-svg" aria-hidden="true">
      <path d="${areaD}" fill="${fill}" />
      <path d="${pathD}" fill="none" stroke="${stroke}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" />
    </svg>`;

  // Tooltip on hover
  const last = points[points.length - 1];
  container.title = `Total cost $${last.toFixed(4)} over ${samples.length} calls`;
}

export async function fetchHistory(runId) {
  if (!runId) return [];
  try {
    const { api } = await import('./api.js?v=20260831-clean-ui');
    const data = await api(`/api/v1/runs/${encodeURIComponent(runId)}/history?limit=200`);
    return data.samples || [];
  } catch (_) {
    return [];
  }
}

// Live sampler: maintains in-memory history per runId, accumulates HUD values.
const liveBuffers = new Map(); // runId -> HistorySample[]

export function pushLiveSample(runId, { input_tokens, output_tokens, cost_usd, model }) {
  if (!runId) return;
  const buf = liveBuffers.get(runId) || [];
  buf.push({
    timestamp: new Date().toISOString(),
    input_tokens: Number(input_tokens) || 0,
    output_tokens: Number(output_tokens) || 0,
    cost_usd: Number(cost_usd) || 0,
    model: String(model || ''),
  });
  // keep last 80 live points + history cap
  if (buf.length > 80) buf.splice(0, buf.length - 80);
  liveBuffers.set(runId, buf);
}

export function getLiveHistory(runId, persistedSamples = []) {
  const live = liveBuffers.get(runId) || [];
  if (!persistedSamples.length) return live;
  // Merge persisted + live (live appended)
  return [...persistedSamples, ...live];
}

export function clearLiveHistory(runId) {
  if (runId) liveBuffers.delete(runId);
  else liveBuffers.clear();
}
