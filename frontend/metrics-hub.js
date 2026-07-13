/**
 * Metrics Hub — frontend logic (Phase 26.61).
 *
 * Responsibilities:
 *   1. Fetch & render the algorithm catalog, metric registry,
 *      provider/data-flow health, and cache stats from
 *      `GET /api/metrics_hub/status`.
 *   2. Drive the Weight Tuner: sliders for pillar weights +
 *      multiplier exponents, checkboxes for per-metric enable masks,
 *      pipeline-tuning numeric inputs.  Persists via
 *      `POST /api/metrics_hub/weights` and resets via
 *      `POST /api/metrics_hub/weights/reset`.
 *   3. Hot-keys + accessibility (tab navigation, Enter to save).
 *
 * Defensive design: every fetch is wrapped in try/catch with a
 * fallback "—" state.  An unreachable backend never blocks page
 * load — sliders still render at their default positions so the
 * page is functional offline.
 */

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));

const STATE = {
  status: null,         // raw /api/metrics_hub/status payload
  weights: null,        // current (in-memory) weights
  defaults: null,       // server-side defaults — for reset
  dirty: false,         // any unsaved changes?
  previewRows: null,    // last-fetched /api/metrics_hub/preview_snapshot rows
  previewMeta: null,    // freshness metadata for the preview table
};

// Debounce timer for slider-driven preview recomputes.
let _previewDebounceTimer = null;
function _schedulePreviewRecompute() {
  if (_previewDebounceTimer) clearTimeout(_previewDebounceTimer);
  _previewDebounceTimer = setTimeout(() => {
    renderLivePreview();
    _previewDebounceTimer = null;
  }, 80);
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------
function activateTab(name) {
  $$('.mh-tab').forEach((b) => b.classList.toggle('is-active', b.dataset.tab === name));
  $$('.mh-section').forEach((s) => s.classList.toggle('is-active', s.dataset.section === name));
}
$$('.mh-tab').forEach((b) => b.addEventListener('click', () => activateTab(b.dataset.tab)));

// ---------------------------------------------------------------------------
// Generic fetch helper — never throws
// ---------------------------------------------------------------------------
async function safeFetchJson(url, options = {}) {
  try {
    const r = await fetch(url, options);
    if (!r.ok) return { ok: false, status: r.status, error: r.statusText, data: null };
    return { ok: true, status: r.status, data: await r.json() };
  } catch (exc) {
    return { ok: false, status: 0, error: String(exc), data: null };
  }
}

function setFreshness(text, isError = false) {
  const el = $('#mhFreshness');
  if (!el) return;
  el.textContent = text;
  el.style.color = isError ? '#f87171' : '';
}

function setWeightsStatus(text, kind) {
  const el = $('#mhWeightsStatus');
  if (!el) return;
  el.textContent = text;
  el.classList.remove('is-saved', 'is-error');
  if (kind === 'saved') el.classList.add('is-saved');
  if (kind === 'error') el.classList.add('is-error');
}

// ---------------------------------------------------------------------------
// Rendering — Algorithms
// ---------------------------------------------------------------------------
function renderAlgorithms(algos, caches) {
  const grid = $('#mhAlgorithmsGrid');
  if (!grid) return;
  if (!Array.isArray(algos) || algos.length === 0) {
    grid.innerHTML = '<div class="mh-empty">No algorithms reported.</div>';
    return;
  }
  grid.innerHTML = algos.map((a) => {
    const cache = a.cache_ref ? caches[a.cache_ref] : null;
    const cacheHits = cache ? Number(cache.hits || 0) : null;
    const cacheMisses = cache ? Number(cache.misses || 0) : null;
    const hitRate = cache && Number.isFinite(cache.hit_rate)
      ? `${(Number(cache.hit_rate) * 100).toFixed(1)}%`
      : null;
    const tierChip =
      a.tier === 'core' ? '<span class="mh-chip is-ok" title="Production-quality tier">core</span>' :
      a.tier === 'experimental' ? '<span class="mh-chip is-experimental">experimental</span>' :
      '<span class="mh-chip is-info">' + (a.tier || 'infrastructure') + '</span>';
    const multiplierChip = a.multiplier
      ? `<span class="mh-chip is-info" title="Output multiplier">${escapeHtml(a.multiplier)}</span>`
      : '';
    const tunableChip = a.tunable
      ? '<span class="mh-chip is-info">tunable</span>'
      : '<span class="mh-chip">read-only</span>';
    const cacheLine = cache
      ? `<div class="mh-card-meta">
           Cache hit-rate <strong>${hitRate ?? '—'}</strong>
           (hits=${cacheHits ?? 0}, misses=${cacheMisses ?? 0},
           latency EMA ${Number(cache.miss_latency_ms_ema || 0).toFixed(1)}ms)
         </div>`
      : '';
    return `
      <article class="mh-card" data-algo-id="${escapeHtml(a.id)}">
        <header class="mh-card-header">
          <div>
            <div class="mh-card-title">${escapeHtml(a.label)}</div>
            <div class="mh-card-meta">${escapeHtml(a.id)} → <code>${escapeHtml(a.output_field || '')}</code></div>
          </div>
          ${tierChip}
        </header>
        <p class="mh-card-desc">${escapeHtml(a.description || '')}</p>
        ${cacheLine}
        <div class="mh-card-foot">
          ${tunableChip}
          ${multiplierChip}
          ${(a.input_sources || []).map((s) => `<span class="mh-chip">${escapeHtml(s)}</span>`).join('')}
        </div>
      </article>
    `;
  }).join('');
}

// ---------------------------------------------------------------------------
// Rendering — Metrics catalog
// ---------------------------------------------------------------------------
function renderMetrics(registry) {
  const wrap = $('#mhMetricsTable');
  if (!wrap) return;
  const sections = [
    { key: 'standard_metrics',          title: 'Phase 26.60 — Standard metrics' },
    { key: 'composite_multipliers',     title: 'Phase 26.60 — Composite multipliers' },
    { key: 'reality_breaker_overlays',  title: 'Phase 26.60 — Reality-Breaker overlays (experimental+)' },
  ];
  let html = '';
  for (const sec of sections) {
    const rows = (registry && registry[sec.key]) || [];
    if (!rows.length) continue;
    html += `
      <h3 style="margin:14px 12px 6px; font-size: .92rem; color: var(--text,#e6edf3)">${escapeHtml(sec.title)}</h3>
      <table class="mh-table" data-section="${escapeHtml(sec.key)}">
        <thead>
          <tr>
            <th>Key</th>
            <th>Label</th>
            <th>Group</th>
            <th>Units</th>
            <th>Range</th>
            <th>Ranking role</th>
            <th>Direction</th>
            <th>Description</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((m) => {
            const sign = Number(m.higherIsBetterSign ?? 0);
            const arrow = sign > 0 ? '↑ higher = better' : sign < 0 ? '↓ higher = worse' : '↔ neutral';
            const arrowChip = sign > 0
              ? '<span class="mh-chip is-ok">' + escapeHtml(arrow) + '</span>'
              : sign < 0
                ? '<span class="mh-chip is-warn">' + escapeHtml(arrow) + '</span>'
                : '<span class="mh-chip is-info">' + escapeHtml(arrow) + '</span>';
            const rangeStr = Array.isArray(m.rangeHint)
              ? `[${m.rangeHint[0]}, ${m.rangeHint[1]}]`
              : '—';
            return `
              <tr class="${m.experimental ? 'is-experimental' : ''}">
                <td><code>${escapeHtml(m.key)}</code></td>
                <td>${escapeHtml(m.label || '')}</td>
                <td><span class="mh-chip">${escapeHtml(m.group || '')}</span></td>
                <td>${escapeHtml(m.units || '')}</td>
                <td>${escapeHtml(rangeStr)}</td>
                <td>${escapeHtml(m.rankingRole || '')}</td>
                <td>${arrowChip}</td>
                <td>${escapeHtml(m.description || '')}</td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    `;
  }
  wrap.innerHTML = html || '<div class="mh-empty">No metrics registry available.</div>';
}

// ---------------------------------------------------------------------------
// Rendering — Data flows
// ---------------------------------------------------------------------------
function renderDataFlows(providers, caches, lane) {
  const provList = $('#mhProvidersList');
  if (provList) {
    if (!Array.isArray(providers) || !providers.length) {
      provList.innerHTML = '<div class="mh-empty">No provider data.</div>';
    } else {
      provList.innerHTML = providers.map((p) => {
        const state = String(p.live_state || 'unknown').toLowerCase();
        const stateClass =
          state === 'normal' || state === 'ok'        ? 'is-ok' :
          state === 'optional' || state === 'best_effort' ? 'is-info' :
          state === 'degraded' || state === 'warn'    ? 'is-warn' :
          'is-info';
        return `
          <div class="mh-flow-row" data-testid="provider-${escapeHtml(p.id)}">
            <div>
              <div class="mh-flow-name">${escapeHtml(p.label)}</div>
              <div class="mh-flow-sub">${escapeHtml(p.role || '')}</div>
            </div>
            <div>
              <span class="mh-chip ${stateClass}">${escapeHtml(state)}</span>
              ${p.failure_count > 0 ? `<span class="mh-chip is-error" title="failure count">${p.failure_count} fail${p.failure_count === 1 ? '' : 's'}</span>` : ''}
            </div>
          </div>
        `;
      }).join('');
    }
  }
  const cacheList = $('#mhCachesList');
  if (cacheList) {
    const keys = Object.keys(caches || {});
    if (!keys.length) {
      cacheList.innerHTML = '<div class="mh-empty">No cache data.</div>';
    } else {
      cacheList.innerHTML = keys.map((k) => {
        const c = caches[k] || {};
        const hr = Number.isFinite(c.hit_rate) ? (Number(c.hit_rate) * 100).toFixed(1) + '%' : '—';
        const hitClass = Number(c.hit_rate) >= 0.8 ? 'is-ok'
                       : Number(c.hit_rate) >= 0.5 ? 'is-info'
                       : 'is-warn';
        return `
          <div class="mh-flow-row" data-testid="cache-${escapeHtml(k)}">
            <div>
              <div class="mh-flow-name">${escapeHtml(k.replace(/_/g, ' '))}</div>
              <div class="mh-flow-sub">
                size ${c.size ?? '—'} · hits ${c.hits ?? 0} ·
                misses ${c.misses ?? 0} ·
                latency EMA ${Number(c.miss_latency_ms_ema ?? 0).toFixed(1)}ms
              </div>
            </div>
            <span class="mh-chip ${hitClass}">${hr}</span>
          </div>
        `;
      }).join('');
    }
  }
  const laneList = $('#mhLaneList');
  if (laneList) {
    if (!lane || !lane.running) {
      laneList.innerHTML = '<div class="mh-empty">Priority lane not running.</div>';
    } else {
      laneList.innerHTML = `
        <div class="mh-flow-row" data-testid="lane-uptime">
          <div>
            <div class="mh-flow-name">Top-N priority lane</div>
            <div class="mh-flow-sub">
              uptime ${Number(lane.uptime_seconds || 0).toFixed(0)}s ·
              ticks ${lane.total_ticks ?? 0} ·
              symbols rescored ${lane.total_symbols_rescored ?? 0}
            </div>
          </div>
          <span class="mh-chip ${lane.monitor_only ? 'is-warn' : 'is-ok'}">${lane.monitor_only ? 'monitor-only' : 'live'}</span>
        </div>
        <div class="mh-flow-row" data-testid="lane-last-tick">
          <div>
            <div class="mh-flow-name">Last tick</div>
            <div class="mh-flow-sub">
              ${Number(lane.last_tick_seconds_ago || 0).toFixed(1)}s ago ·
              ${lane.last_tick_symbols_rescored ?? 0} symbols ·
              elapsed ${Number(lane.last_tick_elapsed_seconds || 0).toFixed(2)}s
            </div>
          </div>
        </div>
        <div class="mh-flow-row" data-testid="lane-viewport">
          <div>
            <div class="mh-flow-name">Viewport first-pass tracker</div>
            <div class="mh-flow-sub">
              tracked ${lane.viewport_first_pass?.tracked_total ?? 0} ·
              TTL ${lane.viewport_first_pass?.ttl_seconds ?? '—'}s
            </div>
          </div>
        </div>
      `;
    }
  }
}

// ---------------------------------------------------------------------------
// Rendering — Cache dedupe status
// ---------------------------------------------------------------------------
function _fmtRelUtc(iso) {
  if (!iso) return '—';
  try {
    const then = new Date(iso).getTime();
    const secs = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (secs < 60) return `${secs}s ago`;
    if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
    if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
    return `${Math.floor(secs / 86400)}d ago`;
  } catch (_) { return '—'; }
}

function renderDedupe(status) {
  const list = $('#mhDedupeList');
  if (!list) return;
  if (!status || typeof status !== 'object') {
    list.innerHTML = '<div class="mh-empty">No dedupe status available.</div>';
    return;
  }
  const wt = status.write_time || {};
  const runs = Number(status.runs_completed || 0);
  const lastFull = status.last_full_run_utc;
  const domains = [
    { key: 'daily_history',       label: 'Daily history',       icon: '⌚' },
    { key: 'options_chain',       label: 'Options chain',       icon: '⌗' },
    { key: 'reaction_clustering', label: 'Reaction clustering', icon: '◇' },
  ];
  const domainRows = domains.map((d) => {
    const s = status[d.key] || {};
    const removed = Number(s.removed || 0);
    const chip = removed > 0
      ? `<span class="mh-chip is-info" title="Duplicate records removed by the last pass">${removed} removed</span>`
      : '<span class="mh-chip is-ok" title="No duplicates found">clean</span>';
    return `
      <div class="mh-flow-row" data-testid="dedupe-domain-${d.key}">
        <div>
          <div class="mh-flow-name">${d.icon} ${escapeHtml(d.label)}</div>
          <div class="mh-flow-sub">
            scanned ${s.scanned ?? 0} · retained ${s.retained ?? 0} ·
            groups ${s.duplicate_groups ?? 0} · quarantined ${s.quarantined ?? 0} ·
            last ${_fmtRelUtc(s.last_run_utc)}
          </div>
        </div>
        ${chip}
      </div>
    `;
  }).join('');
  const writeRow = `
    <div class="mh-flow-row" data-testid="dedupe-writetime">
      <div>
        <div class="mh-flow-name">Write-time canonicalization</div>
        <div class="mh-flow-sub">
          history bars ${wt.history_bars_deduped ?? 0} ·
          option rows ${wt.option_rows_deduped ?? 0} ·
          reaction zones ${wt.reaction_zones_deduped ?? 0}
        </div>
      </div>
      <span class="mh-chip">continuous</span>
    </div>
  `;
  const runsRow = `
    <div class="mh-flow-row" data-testid="dedupe-runs">
      <div>
        <div class="mh-flow-name">Full-run history</div>
        <div class="mh-flow-sub">
          completed ${runs} · last ${_fmtRelUtc(lastFull)}
        </div>
      </div>
      <span class="mh-chip ${runs > 0 ? 'is-ok' : 'is-info'}">${runs > 0 ? 'audited' : 'pending'}</span>
    </div>
  `;
  list.innerHTML = domainRows + writeRow + runsRow;
}

async function loadDedupeStatus() {
  const r = await safeFetchJson('/api/cache/dedupe/status');
  if (r.ok && r.data) renderDedupe(r.data);
  else {
    const list = $('#mhDedupeList');
    if (list) list.innerHTML = `<div class="mh-empty">Dedupe status unavailable (${escapeHtml(r.error || r.status)}).</div>`;
  }
}

async function runDedupe() {
  const btn = $('#mhDedupeRun');
  if (btn) { btn.disabled = true; btn.textContent = 'Running…'; }
  const r = await safeFetchJson('/api/cache/dedupe/run?trigger=metrics_hub_ui', { method: 'POST' });
  if (r.ok && r.data && r.data.status) {
    renderDedupe(r.data.status);
    if (btn) btn.textContent = '✓ Ran ' + (r.data.result?.elapsed_ms ?? '?') + 'ms';
    setTimeout(() => { if (btn) btn.textContent = '⟲ Run dedupe'; }, 2500);
  } else {
    if (btn) btn.textContent = '⚠ Failed';
    setTimeout(() => { if (btn) btn.textContent = '⟲ Run dedupe'; }, 2500);
  }
  if (btn) btn.disabled = false;
}

// ---------------------------------------------------------------------------
// Rendering — Weight tuner
// ---------------------------------------------------------------------------
const PILLAR_LABELS = {
  momentum_strength:         'Momentum strength',
  trend_volume_delta:        'Trend volume Δ',
  institutional_confluence:  'Institutional confluence',
  options_positioning:       'Options positioning',
  institutional_order_block: 'Institutional order blocks',
  dark_pool_attraction:      'Dark-pool attraction',
  reaction_clustering:       'Reaction clustering',
  volume_sentiment:          'Volume sentiment',
  effort_vs_result:          'Effort vs result',
  predictive_consensus:      'Predictive consensus',
  fundamentals:              'Fundamentals',
  regulatory_signal:         'Regulatory signal',
};

const EXPONENT_LABELS = {
  lab_rank_multiplier:         'Lab Mode',
  strategy_rank_multiplier:    'Strategy Tier',
  strategy_v2_rank_multiplier: 'Strategy V2',
  regime_risk_multiplier:      'Regime Risk',
  liq_kelly_factor:            'Liquidity Kelly',
  ml_rank_multiplier:          'ML Overlay',
  reality_breaker_multiplier:  'Reality-Breaker',
};

const EXPERIMENTAL_METRICS = new Set([
  'local_causal_cone_signal',
  'quantum_path_interference_index',
  'local_lyapunov_volatility_exponent',
  'temporal_renormalization_score',
]);

function renderWeightSlider(key, label, value, defaultValue, min, max, step) {
  const isChanged = Math.abs(Number(value) - Number(defaultValue)) > 1e-6;
  return `
    <div class="mh-weight-row ${isChanged ? 'is-changed' : ''}" data-weight-key="${escapeHtml(key)}">
      <label for="w-${escapeHtml(key)}">${escapeHtml(label)}</label>
      <span class="mh-weight-value" data-testid="weight-value-${escapeHtml(key)}">${Number(value).toFixed(2)}×</span>
      <input
        id="w-${escapeHtml(key)}"
        type="range"
        min="${min}" max="${max}" step="${step}"
        value="${value}"
        data-default="${defaultValue}"
        data-testid="weight-slider-${escapeHtml(key)}"
      >
    </div>
  `;
}

function renderFactorWeights() {
  const list = $('#mhFactorWeightsList');
  if (!list) return;
  const fw = STATE.weights?.factor_weights || {};
  const dw = STATE.defaults?.factor_weights || {};
  const keys = Object.keys(fw);
  if (!keys.length) {
    list.innerHTML = '<div class="mh-empty">No factor weights available.</div>';
    return;
  }
  list.innerHTML = keys.map((k) => {
    const label = PILLAR_LABELS[k] || k;
    return renderWeightSlider(`factor:${k}`, label, fw[k], dw[k] ?? 1.0, 0, 2, 0.05);
  }).join('');
  wireSliders(list);
}

function renderExponents() {
  const list = $('#mhExponentsList');
  if (!list) return;
  const me = STATE.weights?.multiplier_exponents || {};
  const dm = STATE.defaults?.multiplier_exponents || {};
  const keys = Object.keys(me);
  if (!keys.length) {
    list.innerHTML = '<div class="mh-empty">No multiplier exponents available.</div>';
    return;
  }
  list.innerHTML = keys.map((k) => {
    const label = EXPONENT_LABELS[k] || k;
    return renderWeightSlider(`exponent:${k}`, label, me[k], dm[k] ?? 1.0, 0, 2, 0.05);
  }).join('');
  wireSliders(list);
}

function renderPipelineTuning() {
  const list = $('#mhPipelineTuningList');
  if (!list) return;
  const pt = STATE.weights?.pipeline_tuning || {};
  const dp = STATE.defaults?.pipeline_tuning || {};
  list.innerHTML = `
    ${renderWeightSlider('pipeline:multiplier_floor',   'Multiplier floor (compound)',   pt.multiplier_floor   ?? 0.05, dp.multiplier_floor   ?? 0.05, 0.01, 1.0, 0.01)}
    ${renderWeightSlider('pipeline:multiplier_ceiling', 'Multiplier ceiling (compound)', pt.multiplier_ceiling ?? 20.0, dp.multiplier_ceiling ?? 20.0, 1.0,  100.0, 0.5)}
  `;
  wireSliders(list);
}

function renderEnableMask() {
  const list = $('#mhEnableMaskList');
  if (!list) return;
  const em = STATE.weights?.enabled_metrics || {};
  const keys = Object.keys(em);
  if (!keys.length) {
    list.innerHTML = '<div class="mh-empty">No metric enable mask available.</div>';
    return;
  }
  list.innerHTML = keys.map((k) => {
    const isExp = EXPERIMENTAL_METRICS.has(k);
    const checked = em[k] ? 'checked' : '';
    return `
      <div class="mh-mask-row ${isExp ? 'is-experimental' : ''}" data-mask-key="${escapeHtml(k)}">
        <label>
          <input type="checkbox" data-testid="mask-${escapeHtml(k)}" ${checked}>
          <span class="mh-mask-label-text">
            ${escapeHtml(k)}
            ${isExp ? '<span class="mh-mask-sub">opt-in; needs Advanced Experimental Mode</span>' : ''}
          </span>
        </label>
      </div>
    `;
  }).join('');
  $$('.mh-mask-row input[type="checkbox"]', list).forEach((cb) => {
    cb.addEventListener('change', () => {
      const row = cb.closest('.mh-mask-row');
      const key = row?.dataset?.maskKey;
      if (!key || !STATE.weights?.enabled_metrics) return;
      STATE.weights.enabled_metrics[key] = !!cb.checked;
      STATE.dirty = true;
      setWeightsStatus('Unsaved changes', '');
      renderDiffBadge();
      _schedulePreviewRecompute();
    });
  });
}

function wireSliders(root) {
  $$('.mh-weight-row input[type="range"]', root).forEach((slider) => {
    slider.addEventListener('input', () => {
      const row = slider.closest('.mh-weight-row');
      const key = row?.dataset?.weightKey;
      if (!key) return;
      const [group, name] = key.split(':');
      const v = Number(slider.value);
      const valueCell = row.querySelector('.mh-weight-value');
      if (valueCell) valueCell.textContent = v.toFixed(2) + '×';
      const def = Number(slider.dataset.default);
      row.classList.toggle('is-changed', Math.abs(v - def) > 1e-6);
      if (group === 'factor') {
        STATE.weights.factor_weights[name] = v;
      } else if (group === 'exponent') {
        STATE.weights.multiplier_exponents[name] = v;
      } else if (group === 'pipeline') {
        STATE.weights.pipeline_tuning[name] = v;
      }
      STATE.dirty = true;
      setWeightsStatus('Unsaved changes', '');
      renderDiffBadge();
      _schedulePreviewRecompute();
    });
  });
}

function renderWeights() {
  renderFactorWeights();
  renderExponents();
  renderPipelineTuning();
  renderEnableMask();
  renderDiffBadge();
}

// ===========================================================================
// Diff vs defaults badge — counts every field that differs from defaults.
// ===========================================================================
function _countDiffs() {
  if (!STATE.weights || !STATE.defaults) return 0;
  let n = 0;
  const _compare = (cur, def) => {
    if (cur == null || def == null) return;
    for (const k of Object.keys(def)) {
      if (typeof def[k] === 'number') {
        if (Math.abs((Number(cur[k]) || 0) - Number(def[k])) > 1e-6) n++;
      } else {
        // bool / string equality
        if (cur[k] !== def[k]) n++;
      }
    }
  };
  _compare(STATE.weights.factor_weights,       STATE.defaults.factor_weights);
  _compare(STATE.weights.multiplier_exponents, STATE.defaults.multiplier_exponents);
  _compare(STATE.weights.enabled_metrics,      STATE.defaults.enabled_metrics);
  _compare(STATE.weights.pipeline_tuning,      STATE.defaults.pipeline_tuning);
  return n;
}

function renderDiffBadge() {
  const badge = $('#mhDiffBadge');
  if (!badge) return;
  const n = _countDiffs();
  badge.textContent = n === 0
    ? '0 modified'
    : `${n} modified ${n === 1 ? 'weight' : 'weights'}`;
  badge.classList.toggle('is-dirty', n > 0);
  badge.title = n === 0
    ? 'All weights match defaults.'
    : `${n} weight${n === 1 ? '' : 's'} differ${n === 1 ? 's' : ''} from defaults — click "Reset" to restore.`;
}

// ---------------------------------------------------------------------------
// Save / reset
// ---------------------------------------------------------------------------
async function saveWeights() {
  setWeightsStatus('Saving…', '');
  const r = await safeFetchJson('/api/metrics_hub/weights', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(STATE.weights),
  });
  if (r.ok && r.data && r.data.weights) {
    STATE.weights = r.data.weights;
    STATE.defaults = r.data.defaults;
    STATE.dirty = false;
    setWeightsStatus('Saved ✓', 'saved');
    renderWeights();
    renderLivePreview();
  } else {
    setWeightsStatus(`Save failed (${r.error || r.status})`, 'error');
  }
}

async function resetWeights() {
  if (!confirm('Reset every weight back to its default? This cannot be undone.')) return;
  setWeightsStatus('Resetting…', '');
  const r = await safeFetchJson('/api/metrics_hub/weights/reset', { method: 'POST' });
  if (r.ok && r.data && r.data.weights) {
    STATE.weights = r.data.weights;
    STATE.defaults = r.data.defaults;
    STATE.dirty = false;
    setWeightsStatus('Defaults restored', 'saved');
    renderWeights();
    renderLivePreview();
  } else {
    setWeightsStatus(`Reset failed (${r.error || r.status})`, 'error');
  }
}

// ===========================================================================
// Live ranking preview
//
// Pulls a thinned snapshot (top-50 with multipliers + pillar scores
// pre-computed) and applies the user's CURRENT in-memory weight
// matrix on the client to project the new ranking.  Recomputes on
// every slider / checkbox change (debounced).
// ===========================================================================
function _composedMultiplier(fm, weights) {
  // Replicates the spec multiplication order:
  //   eff_kelly
  //     × lab^expLab
  //     × strategy^expStrat
  //     × strategy_v2^expSV2
  //     × regime_risk^expRR
  //     × liq_kelly^expLiq
  //     × ml^expML
  //     × reality_breaker^expRB
  // Then clamped to pipeline_tuning floor/ceiling.
  const exps = (weights && weights.multiplier_exponents) || {};
  const enabled = (weights && weights.enabled_metrics) || {};

  // Match the backend's metric-mask logic: when a multiplier's
  // backing metrics are all OFF, neutralise the multiplier itself.
  const sv2Masked = ['ts_nonlinear_dependence', 'trend_curvature_pct',
                     'lead_lag_influence', 'multiscale_consistency',
                     'drawdown_memory_score']
    .every((k) => enabled[k] === false);
  const rrMasked  = ['msm_drift_premium', 'volofvol_regime_score',
                     'entropy_regime_stability']
    .every((k) => enabled[k] === false);
  const mlMasked  = enabled.ml_residual_edge === false;
  const liqMasked = enabled.liq_adjusted_signal === false;

  const _pow = (base, expoKey) => {
    const expo = Number(exps[expoKey] ?? 1.0);
    if (!Number.isFinite(expo) || !Number.isFinite(base) || base <= 0) return base;
    try { return Math.pow(base, expo); } catch (e) { return base; }
  };

  const m = Number(fm.effective_kelly_rank) || 0;
  const sign = Math.sign(m);
  let mag = Math.abs(m);

  mag *= _pow(Number(fm.lab_rank_multiplier) || 1, 'lab_rank_multiplier');
  mag *= _pow(Number(fm.strategy_rank_multiplier) || 1, 'strategy_rank_multiplier');
  mag *= _pow(sv2Masked ? 1 : (Number(fm.strategy_v2_rank_multiplier) || 1), 'strategy_v2_rank_multiplier');
  mag *= _pow(rrMasked  ? 1 : (Number(fm.regime_risk_multiplier) || 1),      'regime_risk_multiplier');
  mag *= _pow(liqMasked ? 1 : (Number(fm.liq_kelly_factor) || 1),            'liq_kelly_factor');
  mag *= _pow(mlMasked  ? 1 : (Number(fm.ml_rank_multiplier) || 1),          'ml_rank_multiplier');
  // Reality breaker — only applied when the user has at least one
  // reality_breaker metric enabled in the mask.
  const anyRbOn = ['local_causal_cone_signal', 'quantum_path_interference_index',
                   'local_lyapunov_volatility_exponent', 'temporal_renormalization_score']
    .some((k) => enabled[k] === true);
  if (anyRbOn) {
    mag *= _pow(Number(fm.reality_breaker_multiplier) || 1, 'reality_breaker_multiplier');
  }

  // Apply pipeline-tuning floor/ceiling (compound clamps).
  const floor   = Number((weights.pipeline_tuning || {}).multiplier_floor   ?? 0.05);
  const ceiling = Number((weights.pipeline_tuning || {}).multiplier_ceiling ?? 20.0);
  if (mag > 0) {
    if (mag < floor)   mag = floor;
    if (mag > ceiling) mag = ceiling;
  }
  return sign * mag;
}

async function fetchPreviewSnapshot() {
  const r = await safeFetchJson('/api/metrics_hub/preview_snapshot?limit=50');
  if (!r.ok || !r.data || !Array.isArray(r.data.rows)) {
    STATE.previewRows = null;
    STATE.previewMeta = null;
    return false;
  }
  STATE.previewRows = r.data.rows;
  STATE.previewMeta = {
    generatedAtMs: r.data.generated_at_ms,
    limit: r.data.limit,
  };
  return true;
}

function renderLivePreview() {
  const wrap = $('#mhPreviewTable');
  const metaEl = $('#mhPreviewMeta');
  if (!wrap) return;
  if (!Array.isArray(STATE.previewRows) || !STATE.previewRows.length) {
    wrap.innerHTML = '<div class="mh-empty">Click "Refresh snapshot" to load the current leaderboard.</div>';
    if (metaEl) metaEl.textContent = '—';
    return;
  }
  if (!STATE.weights) {
    wrap.innerHTML = '<div class="mh-empty">Weights still loading…</div>';
    return;
  }
  const useAbs = !!$('#mhPreviewDirectional')?.checked;
  // 1. Current ranking — uses the SERVER-side multipliers as-is (the
  //    snapshot is shipped already-multiplied), so we just sort by
  //    eff_kelly with no client transformation.
  const current = STATE.previewRows.map((row, idx) => ({
    symbol: row.symbol,
    score:  useAbs ? Math.abs(row.forward_metrics.effective_kelly_rank)
                   : row.forward_metrics.effective_kelly_rank,
    serverIdx: idx,
  }));
  current.sort((a, b) => b.score - a.score);
  const currentRankBySymbol = new Map(current.map((r, i) => [r.symbol, i + 1]));

  // 2. Projected ranking — apply the user's CURRENT in-memory
  //    weights to a fresh score per row.
  const projected = STATE.previewRows.map((row) => {
    const projScore = _composedMultiplier(row.forward_metrics, STATE.weights);
    return {
      symbol:        row.symbol,
      currentScore:  row.forward_metrics.effective_kelly_rank,
      projectedScore: projScore,
      direction:     row.forward_metrics.direction_cf,
      drift:         row.forward_metrics.drift_pct,
    };
  });
  const sortKey = useAbs ? (x) => Math.abs(x.projectedScore) : (x) => x.projectedScore;
  projected.sort((a, b) => sortKey(b) - sortKey(a));

  // 3. Render table — projected rank, current rank, Δ, scores.
  const rowsHtml = projected.slice(0, 50).map((p, idx) => {
    const newRank = idx + 1;
    const oldRank = currentRankBySymbol.get(p.symbol) ?? null;
    const delta = (oldRank == null) ? 0 : (oldRank - newRank);
    let deltaCell, isMover = false;
    if (delta > 0)      { deltaCell = `<span class="delta-up">▲ ${delta}</span>`;   isMover = delta >= 3; }
    else if (delta < 0) { deltaCell = `<span class="delta-down">▼ ${-delta}</span>`; isMover = -delta >= 3; }
    else                { deltaCell = `<span class="delta-flat">—</span>`; }
    const dirCls = p.direction === 'Bullish' ? 'dir-bull'
                 : p.direction === 'Bearish' ? 'dir-bear' : 'dir-flat';
    return `
      <tr class="${isMover ? 'is-mover' : ''}" data-testid="preview-row-${escapeHtml(p.symbol)}">
        <td class="num">${newRank}</td>
        <td class="num">${oldRank ?? '—'}</td>
        <td class="num">${deltaCell}</td>
        <td><strong>${escapeHtml(p.symbol)}</strong></td>
        <td class="${dirCls}">${escapeHtml(p.direction || 'Neutral')}</td>
        <td class="num">${Number(p.currentScore).toFixed(5)}</td>
        <td class="num"><strong>${Number(p.projectedScore).toFixed(5)}</strong></td>
        <td class="num">${Number(p.drift || 0).toFixed(3)}%</td>
      </tr>
    `;
  }).join('');

  wrap.innerHTML = `
    <table class="mh-table" data-testid="preview-rerank-table">
      <thead>
        <tr>
          <th title="Projected rank with your unsaved weights">Proj #</th>
          <th title="Current live rank from the server">Live #</th>
          <th title="Change in rank (Δ)">Δ</th>
          <th>Symbol</th>
          <th>Direction</th>
          <th title="Server-side effective_kelly_rank">Live score</th>
          <th title="Client-projected score with unsaved weights">Proj. score</th>
          <th title="forward_1h drift_pct">Drift</th>
        </tr>
      </thead>
      <tbody>${rowsHtml}</tbody>
    </table>
  `;
  if (metaEl) {
    const ageS = STATE.previewMeta?.generatedAtMs
      ? Math.max(0, (Date.now() - STATE.previewMeta.generatedAtMs) / 1000)
      : null;
    metaEl.textContent = ageS != null
      ? `Snapshot ${ageS.toFixed(0)}s old · ${STATE.previewRows.length} symbols`
      : `${STATE.previewRows.length} symbols`;
  }
}

async function refreshPreviewSnapshot() {
  const meta = $('#mhPreviewMeta');
  if (meta) { meta.textContent = 'Loading…'; meta.style.color = ''; }
  const ok = await fetchPreviewSnapshot();
  if (!ok) {
    if (meta) { meta.textContent = 'Snapshot unavailable'; meta.style.color = '#f87171'; }
    return;
  }
  renderLivePreview();
}

// ===========================================================================
// Export / Import JSON
// ===========================================================================
function exportWeightsJson() {
  if (!STATE.weights) return;
  const payload = {
    schema:         'market-refinement-dashboard.metrics-hub.weights',
    schema_version: '26.61',
    exported_at:    new Date().toISOString(),
    persisted:      !STATE.dirty,
    weights:        STATE.weights,
    defaults:       STATE.defaults,
  };
  const blob = new Blob(
    [JSON.stringify(payload, null, 2)],
    { type: 'application/json' },
  );
  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `metrics-hub-weights-${ts}.json`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    URL.revokeObjectURL(a.href);
    document.body.removeChild(a);
  }, 100);
  setWeightsStatus('Exported JSON ✓', 'saved');
}

async function importWeightsJson(file) {
  if (!file) return;
  setWeightsStatus('Importing…', '');
  let text;
  try {
    text = await file.text();
  } catch (exc) {
    setWeightsStatus(`Import failed: ${exc.message || exc}`, 'error');
    return;
  }
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (exc) {
    setWeightsStatus('Import failed: invalid JSON', 'error');
    return;
  }
  // Accept either { weights: {...} } (full export) or a bare weight matrix.
  const candidate = (parsed && parsed.weights && typeof parsed.weights === 'object')
    ? parsed.weights
    : parsed;
  if (!candidate || typeof candidate !== 'object') {
    setWeightsStatus('Import failed: payload does not look like a weight matrix', 'error');
    return;
  }
  // Round-trip through the server's sanitiser by POSTing.  This way
  // the user gets clamping + unknown-key rejection for free.
  const r = await safeFetchJson('/api/metrics_hub/weights', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(candidate),
  });
  if (r.ok && r.data && r.data.weights) {
    STATE.weights = r.data.weights;
    STATE.defaults = r.data.defaults;
    STATE.dirty = false;
    setWeightsStatus('Imported + saved ✓', 'saved');
    renderWeights();
    renderLivePreview();
  } else {
    setWeightsStatus(`Import failed (${r.error || r.status})`, 'error');
  }
}

// ---------------------------------------------------------------------------
// Master load
// ---------------------------------------------------------------------------
async function loadAll() {
  setFreshness('Loading…');
  const r = await safeFetchJson('/api/metrics_hub/status');
  if (!r.ok || !r.data) {
    setFreshness(`Backend unreachable (${r.error || r.status})`, true);
    return;
  }
  STATE.status = r.data;
  STATE.weights = r.data.weights;
  STATE.defaults = r.data.defaults;
  renderAlgorithms(r.data.algorithms || [], r.data.caches || {});
  renderMetrics(r.data.phase_2660_registry || {});
  renderDataFlows(r.data.providers || [], r.data.caches || {}, r.data.priority_lane || {});
  renderWeights();
  setFreshness(`Updated ${new Date(r.data.generated_at_ms || Date.now()).toLocaleTimeString()}`);
  // Cache dedupe status lives on a separate admin endpoint; fire-and-forget.
  loadDedupeStatus();
}

// ---------------------------------------------------------------------------
// HTML escape helper
// ---------------------------------------------------------------------------
function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
$('#mhRefresh')?.addEventListener('click', loadAll);
$('#mhSaveWeights')?.addEventListener('click', saveWeights);
$('#mhResetWeights')?.addEventListener('click', resetWeights);
$('#mhExportWeights')?.addEventListener('click', exportWeightsJson);
$('#mhImportWeights')?.addEventListener('change', (ev) => {
  const f = ev.target?.files?.[0];
  if (f) importWeightsJson(f);
  // Reset the input so the same file can be re-imported.
  if (ev.target) ev.target.value = '';
});
$('#mhPreviewRefresh')?.addEventListener('click', refreshPreviewSnapshot);
$('#mhPreviewDirectional')?.addEventListener('change', renderLivePreview);
$('#mhDedupeRun')?.addEventListener('click', runDedupe);

// Auto-load the preview snapshot the FIRST time the user opens the
// Weights tab — saves them a click.
let _previewAutoLoaded = false;
$$('.mh-tab').forEach((b) => b.addEventListener('click', () => {
  if (b.dataset.tab === 'weights' && !_previewAutoLoaded) {
    _previewAutoLoaded = true;
    refreshPreviewSnapshot();
  }
}));

window.addEventListener('beforeunload', (ev) => {
  if (STATE.dirty) {
    ev.preventDefault();
    ev.returnValue = 'You have unsaved weight changes.';
    return ev.returnValue;
  }
});

loadAll();
