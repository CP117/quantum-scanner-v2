/*  app.js — Quantum Market Scanner (main bundle)
 *
 *  Depends on state.js which is loaded first via <script defer> and
 *  provides:  state, fetchJson, byId, setCookie/getCookie,
 *  saveUiPrefs/loadUiPrefs/applyPrefsToControls, PRESET_FILTERS,
 *  updatePresetChips, _syncShareableUrl/readShareableStateFromUrl,
 *  applyPreset, syncFilterState, getFiltersQuery.
 *
 *  This file contains: renderers, polling loop, detail panel logic,
 *  scoring helpers, and every bootstrap / event-wiring path.
 */
/* eslint-env browser */
/* global state, fetchJson, byId, setCookie, getCookie,
   saveUiPrefs, loadUiPrefs, applyPrefsToControls,
   PRESET_FILTERS, updatePresetChips,
   _syncShareableUrl, readShareableStateFromUrl,
   applyPreset, syncFilterState, getFiltersQuery */


// =========================================================================
// Phase 5: user-added symbols, manual refresh, backtest, per-zone display
// =========================================================================

async function refreshUserAddedList() {
  try {
    const res = await fetch(`${state.apiBase}/universe/added`);
    const payload = await res.json();
    const list = payload.symbols || [];
    const container = byId('userAddedList');
    if (!container) return;
    if (!list.length) {
      container.innerHTML = '<span class="add-status">No user-added symbols yet.</span>';
      return;
    }
    container.innerHTML = list.map((row) =>
      `<span class="user-added-chip" data-symbol="${row.symbol}">${row.symbol}<button title="Remove" data-action="remove-symbol" data-symbol="${row.symbol}">\u00d7</button></span>`
    ).join('');
    container.querySelectorAll('button[data-action="remove-symbol"]').forEach((btn) => {
      btn.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        const sym = btn.dataset.symbol;
        await fetch(`${state.apiBase}/universe/remove/${encodeURIComponent(sym)}`, { method: 'DELETE' });
        refreshUserAddedList();
      });
    });
  } catch (e) { /* swallow */ }
}

async function handleAddSymbol() {
  const input = byId('addSymbolInput');
  const statusLine = byId('addSymbolStatus');
  if (!input) return;
  const sym = (input.value || '').trim().toUpperCase();
  if (!sym) return;
  statusLine.className = 'add-status';
  statusLine.textContent = `Adding ${sym}\u2026`;
  try {
    const res = await fetch(`${state.apiBase}/universe/add`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: sym, name: sym, exchange: 'USER_ADDED' }),
    });
    const payload = await res.json();
    if (payload.ok) {
      statusLine.classList.add('is-ok');
      statusLine.textContent = payload.reason === 'already_present'
        ? `${sym} already in universe.`
        : `${sym} added \u2014 will appear next scan cycle.`;
      input.value = '';
      refreshUserAddedList();
    } else {
      statusLine.classList.add('is-err');
      statusLine.textContent = `Failed: ${payload.detail || payload.reason || 'unknown'}`;
    }
  } catch (e) {
    statusLine.classList.add('is-err');
    statusLine.textContent = `Network error: ${e}`;
  }
}

async function manualRefreshSymbol(symbol) {
  if (!symbol) return;
  const btn = byId('manualRefreshBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Refreshing\u2026'; }
  try {
    // Phase 22 fix: forward the current market context so crypto symbols
    // (e.g. DOGE-USD, BTC-USD) hit the crypto provider cascade instead of
    // being misrouted to the yfinance stock path.  Without this the
    // backend defaulted to market='stocks' and crypto refreshes returned
    // the unavailable stub.
    const market = state.currentMarket || 'stocks';
    const res = await fetch(
      `${state.apiBase}/stock/${encodeURIComponent(symbol)}/refresh?market=${encodeURIComponent(market)}`,
      { method: 'POST' },
    );
    if (!res.ok) throw new Error(`status ${res.status}`);
    const payload = await res.json().catch(() => null);
    // Reload the detail panel — this re-renders the button label off the
    // freshly returned row's stale/lkg/data_source flags.
    await loadDetail(symbol);
    // If the backend signalled a failed live fetch (all providers
    // unavailable), make the failure visible at the button level so the
    // user knows the data is still last-known-good rather than fresh.
    if (payload && payload.refresh_failed && btn) {
      btn.disabled = false;
      btn.textContent = '\u26a0 Refresh failed (providers unavailable) - retry';
      btn.classList.add('is-stale');
    }
  } catch (e) {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Refresh failed - retry';
    }
  }
}

async function runBacktest(symbol) {
  if (!symbol) return;
  const initialBtn = byId('backtestBtn');
  if (initialBtn) { initialBtn.disabled = true; initialBtn.textContent = 'Backtesting\u2026'; }
  try {
    const res = await fetch(`${state.apiBase}/backtest/${encodeURIComponent(symbol)}?lookback=180&forward_bars=5`);
    const payload = await res.json();
    if (state.selectedSymbol !== symbol) return;
    renderBacktest(payload);
  } catch (e) {
    const out = byId('backtestResults');
    const errHtml = `<div class="add-status is-err">Backtest failed: ${String(e).slice(0, 200)}</div>`;
    if (out) out.innerHTML = errHtml;
    state.cachedBacktestCard = { symbol, html: errHtml };
    console.error('[runBacktest] failed:', e);
  } finally {
    const btn = byId('backtestBtn');
    if (btn) { btn.disabled = false; btn.textContent = 'Run backtest'; }
  }
}

// Phase 18: forward price-point prediction. Aggregates every factor
// family the scanner computes (7 family scores + composite + reaction
// clustering + volume sentiment + ATR) into a single forward price
// target with a 95% CI band.  Phase 26.41 extends to 5 horizons:
//   1h / 5h / 10h (sub-daily intraday math)
//   1d (forward_days=1, next-day directional read)
//   10d (the original daily projection)
// The horizon param is a short code ('1h'|'5h'|'10h'|'1d'|'10d').
async function runPrediction(symbol, horizon = '10d') {
  if (!symbol) return;
  const horizonSpec = {
    '1h':  { label: '1-hour',   query: 'forward_hours=1',  buttonId: 'predictBtn1h',  btnText: '1H' },
    '5h':  { label: '5-hour',   query: 'forward_hours=5',  buttonId: 'predictBtn5h',  btnText: '5H' },
    '10h': { label: '10-hour',  query: 'forward_hours=10', buttonId: 'predictBtn10h', btnText: '10H' },
    '1d':  { label: 'Next-day', query: 'forward_days=1',   buttonId: 'predictBtn1d',  btnText: 'Next Day' },
    '10d': { label: '10-day',   query: 'forward_days=10',  buttonId: 'predictBtn10d', btnText: '10-Day' },
  }[horizon] || { label: '10-day', query: 'forward_days=10', buttonId: 'predictBtn10d', btnText: '10-Day' };
  const market = state.currentMarket || 'stocks';
  const engineBasePath = state.useAdvancedPrediction
    ? `${state.apiBase}/api/predict/advanced`
    : `${state.apiBase}/api/predict`;
  // Phase 26.44 bugfix: only disable the BUTTON during the fetch.  DO
  // NOT clobber `#predictionResults` with a "loading…" message — that
  // overwrote the cached card AND created a race where the cache
  // re-injection (during a concurrent live-tick) would overwrite the
  // freshly-loaded card with the previous one.  Leaving the previous
  // card visible during the fetch is also nicer UX.
  const initialBtn = byId(horizonSpec.buttonId);
  if (initialBtn) { initialBtn.disabled = true; initialBtn.textContent = '\u2026'; }
  try {
    const res = await fetch(`${engineBasePath}/${encodeURIComponent(symbol)}?${horizonSpec.query}&market=${encodeURIComponent(market)}`);
    const payload = await res.json();
    state.lastPredictionPayload = payload;
    state.lastPredictionMarket = market;
    state.lastPredictionHorizon = horizon;
    // Phase 26.44: defensive — only the user-clicked symbol's panel
    // should render the result.  If the user clicked a different row
    // while the fetch was in flight, drop the payload silently.
    if (state.selectedSymbol !== symbol) {
      return;
    }
    renderPrediction(payload, horizonSpec.label);
  } catch (e) {
    // Phase 26.44: re-lookup the output div RIGHT NOW.  If a live-tick
    // rebuilt the detail panel during the await, the closure-cached
    // reference would be detached and our error message would never
    // appear on screen.
    const out = byId('predictionResults');
    const errHtml = `<div class="add-status is-err">Prediction failed: ${String(e).slice(0, 200)}</div>`;
    if (out) out.innerHTML = errHtml;
    // Stash error in cache too so the next live-tick re-injection
    // doesn't blink it away in favour of the previous successful card.
    state.cachedPredictionCard = { symbol, engine: 'error', html: errHtml };
    console.error('[runPrediction] failed:', e);
  } finally {
    // Phase 26.44: also re-lookup the button.  The button from the
    // start of this function may have been detached + replaced if a
    // live-tick rebuilt the panel during the fetch.
    const btn = byId(horizonSpec.buttonId);
    if (btn) { btn.disabled = false; btn.textContent = horizonSpec.btnText; }
  }
}

// Phase 26.42: dedicated next-day-open direction call.  This is NOT
// a price-target prediction — it's a binary "Up / Down / Even"
// directional call computed from late-session signals (last 30 min
// drift, intraday VWAP deviation, dealer-GEX sign, etc.).  Renders
// a slimmed-down card without the target/range/factor breakdown.
async function runNextDayOpenDirection(symbol) {
  if (!symbol) return;
  const initialBtn = byId('predictBtnNDO');
  if (initialBtn) { initialBtn.disabled = true; initialBtn.textContent = '\u2026'; }
  const market = state.currentMarket || 'stocks';
  try {
    const res = await fetch(`${state.apiBase}/api/predict/next-day-direction/${encodeURIComponent(symbol)}?market=${encodeURIComponent(market)}`);
    const payload = await res.json();
    state.lastPredictionPayload = payload;
    state.lastPredictionMarket = market;
    if (state.selectedSymbol !== symbol) return;
    renderNextDayDirection(payload);
  } catch (e) {
    const out = byId('predictionResults');
    const errHtml = `<div class="add-status is-err">Next-day direction failed: ${String(e).slice(0, 200)}</div>`;
    if (out) out.innerHTML = errHtml;
    state.cachedPredictionCard = { symbol, engine: 'error', html: errHtml };
    console.error('[runNextDayOpenDirection] failed:', e);
  } finally {
    const btn = byId('predictBtnNDO');
    if (btn) { btn.disabled = false; btn.textContent = 'Next-Day Open'; }
  }
}

function renderNextDayDirection(payload) {
  const out = byId('predictionResults');
  if (!out) return;
  if (!payload || payload.status !== 'ok') {
    out.innerHTML = `<div class="predict-card"><div class="eyebrow">Next-day open direction</div>
      <div class="add-status">Unavailable: ${payload?.reason || 'unknown'}.</div></div>`;
    return;
  }
  const dirClass = payload.direction === 'Up' ? 'predict-bull'
                 : payload.direction === 'Down' ? 'predict-bear' : 'predict-neutral';
  const arrow = payload.direction === 'Up' ? '\u2191' : payload.direction === 'Down' ? '\u2193' : '\u2192';
  const conf = Number(payload.confidence || 0);
  const confClass = conf >= 50 ? 'predict-conf-high' : conf >= 25 ? 'predict-conf-mid' : 'predict-conf-low';
  const comp = payload.signal_components || {};
  out.innerHTML = `
    <div class="predict-card ${dirClass}" data-testid="next-day-direction-card">
      <div class="eyebrow">Next-Day Open Direction \u00b7 ${payload.symbol}</div>
      <div class="predict-target">
        <span class="predict-arrow">${arrow}</span>
        <span class="predict-target-price">${payload.direction}</span>
        <span class="predict-move">P(up) ${(payload.p_up * 100).toFixed(1)}%</span>
      </div>
      <div class="predict-meta">
        <span>From close <strong>$${payload.current_price}</strong></span>
        <span>P(down) <strong>${(payload.p_down * 100).toFixed(1)}%</strong></span>
        <span class="predict-conf ${confClass}" title="Directional certainty = 2 × |P - 0.5|">
          Certainty <strong>${payload.directional_certainty_pct.toFixed(1)}%</strong>
        </span>
      </div>
      <div class="predict-section-title">Late-session signal components</div>
      <div class="predict-meta">
        <span>Last-30m drift <strong>${comp.last_30m_drift_pct >= 0 ? '+' : ''}${comp.last_30m_drift_pct?.toFixed(3)}%</strong></span>
        <span>VWAP dev <strong>${comp.vwap_deviation_pct >= 0 ? '+' : ''}${comp.vwap_deviation_pct?.toFixed(3)}%</strong></span>
        <span>Reaction <strong>${comp.reaction_class}</strong></span>
        <span>Inst.-z <strong>${comp.institutional_z >= 0 ? '+' : ''}${comp.institutional_z?.toFixed(2)}</strong></span>
        <span>GEX sign <strong>${comp.gex_sign > 0 ? '+ (mean-revert)' : comp.gex_sign < 0 ? '- (amplify)' : '0'}</strong></span>
        <span>Composite z <strong>${comp.composite_z?.toFixed(3)}</strong></span>
      </div>
      <details class="predict-reasoning">
        <summary>How was this computed?</summary>
        <ul>${(payload.reasoning || []).map((r) => `<li>${r}</li>`).join('')}</ul>
      </details>
    </div>`;
  // Phase 26.43: cache so live-tick re-render doesn't wipe it.
  state.cachedPredictionCard = {
    symbol: payload.symbol,
    engine: 'next_day_open_direction',
    html: out.innerHTML,
  };
}

// Phase 22: persist the most recently generated prediction to the
// Prediction Tracker DB so the user can audit hit/miss accuracy later
// from the dedicated tracker page.  Reads the payload from state.
async function savePrediction() {
  const payload = state.lastPredictionPayload;
  const market = state.lastPredictionMarket || state.currentMarket || 'stocks';
  if (!payload || payload.status !== 'ok') return;
  const status = byId('savePredictionStatus');
  const btn = byId('savePredictionBtn');
  const notesEl = byId('savePredictionNotes');
  const notes = notesEl ? (notesEl.value || '').slice(0, 1000) : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Saving\u2026'; }
  try {
    const body = {
      symbol: payload.symbol,
      market,
      anchor_price: Number(payload.current_price),
      target_price: Number(payload.target_price),
      direction: payload.direction === 'Bullish' ? 'bull'
               : payload.direction === 'Bearish' ? 'bear'
               : 'neutral',
      confidence_pct: Number(payload.confidence || 0),
      forward_days: Number(payload.forward_days || 10),
      notes,
      full_payload: payload,
    };
    const res = await fetch(`${state.apiBase}/api/predictions/save`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `status ${res.status}`);
    }
    if (status) {
      status.textContent = '\u2713 Saved \u00b7 expires in 10 trading days. View on the Prediction Tracker page.';
      status.classList.add('is-ok');
    }
    if (btn) { btn.textContent = 'Saved'; }
  } catch (e) {
    if (status) {
      status.textContent = `Save failed: ${e.message || e}`;
      status.classList.add('is-err');
    }
    if (btn) { btn.disabled = false; btn.textContent = 'Save prediction'; }
  }
}

function renderPrediction(payload, horizonLabel = '10-day') {
  const out = byId('predictionResults');
  if (!out) return;
  if (!payload || payload.status !== 'ok') {
    const errHtml = `<div class="predict-card"><div class="eyebrow">Price prediction</div>
      <div class="add-status">Unavailable: ${payload?.reason || 'unknown'}.
      ${payload?.hint ? '<br><span class="rating-meta">' + payload.hint + '</span>' : ''}</div></div>`;
    out.innerHTML = errHtml;
    if (payload?.symbol) {
      state.cachedPredictionCard = { symbol: payload.symbol, engine: 'error', html: errHtml };
    }
    return;
  }
  // Phase 26.44: defensive numeric helpers.  The advanced engine
  // returns a different field set than the legacy engine — many of
  // the original template's `.toFixed(...)` calls were running on
  // undefined and silently throwing on the advanced path.  These
  // helpers absorb the difference so a single template renders
  // both shapes cleanly.
  const num = (v, fallback = 0) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : fallback;
  };
  const fixed = (v, digits = 1, fallback = '\u2014') => {
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(digits) : fallback;
  };
  // Backend's own label trumps the caller's hint when present.
  const effectiveLabel = payload.horizon_label || horizonLabel;
  const isAdvanced = payload.engine === 'advanced';
  const arrow = payload.direction === 'Bullish' ? '\u2191' : payload.direction === 'Bearish' ? '\u2193' : '\u2192';
  const moveSign = num(payload.expected_pct_move) >= 0 ? '+' : '';
  const dirClass = payload.direction === 'Bullish' ? 'predict-bull' : payload.direction === 'Bearish' ? 'predict-bear' : 'predict-neutral';
  const conf = num(payload.confidence);
  const confClass = conf >= 50 ? 'predict-conf-high' : conf >= 25 ? 'predict-conf-mid' : 'predict-conf-low';
  // Legacy: family_contributions, Advanced: bayesian_blend.contributions
  const fams = (payload.family_contributions || []);
  const bayes = payload.bayesian_blend || {};
  const bayesContribs = bayes.contributions || [];
  const totalAbsContrib = fams.reduce((a, f) => a + Math.abs(f.contribution_pct || 0), 0) || 1;
  const mods = payload.modulators || {};
  const gammaDriftMult = mods.options_gamma_drift_multiplier;
  const gammaSigmaMult = mods.options_gamma_sigma_multiplier;
  const iobDriftMult = mods.iob_drift_multiplier;
  const pcConfBonus = mods.predictive_consensus_confidence_bonus;
  const intradayMods = (gammaDriftMult != null && gammaDriftMult !== 1) || (iobDriftMult != null && iobDriftMult !== 1) || (pcConfBonus != null && pcConfBonus > 0);
  const capCeiling = payload.forward_hours ? 8 : 25;
  // Advanced-only badges
  const vol = payload.volatility_model || {};
  const regime = payload.regime || {};
  const advancedBadges = isAdvanced ? `
    <span class="predict-advanced-badge" title="GARCH(1,1) + Bayesian factor blend + Hurst-regime + GEX-conditional posterior">ADVANCED</span>
    <span title="Probit P(up) via Φ(μ_post/σ_post)">P(up) <strong>${(num(payload.p_up) * 100).toFixed(1)}%</strong></span>
    <span title="Directional certainty = 2·|P − 0.5|">Cert <strong>${fixed(payload.directional_certainty_pct, 1)}%</strong></span>
    <span title="Posterior precision τ from Bayesian blend (higher = tighter posterior)">τ_post <strong>${fixed(bayes.posterior_precision, 2)}</strong></span>
    <span title="James-Stein shrinkage factor (1.0 = no shrinkage)">Shrink <strong>${fixed(bayes.shrinkage_factor, 2)}</strong></span>
    <span title="Hurst exponent: >0.55 trending, <0.45 mean-reverting">Hurst <strong>${fixed(regime.hurst_exponent, 3)}</strong></span>
    <span title="GARCH-projected per-period sigma">σ_h <strong>${fixed(vol.sigma_horizon_pct, 3)}%</strong></span>
    <span title="Annualised volatility from GARCH (1-day σ × √252)">Ann.vol <strong>${fixed(vol.annualised_vol_pct, 1)}%</strong></span>
  ` : '';
  out.innerHTML = `
    <div class="predict-card ${dirClass}" data-testid="prediction-card" data-horizon="${effectiveLabel}">
      <div class="eyebrow">${effectiveLabel} price prediction \u00b7 ${payload.symbol}${isAdvanced ? ' \u00b7 advanced engine' : ''}</div>
      <div class="predict-target">
        <span class="predict-arrow">${arrow}</span>
        <span class="predict-target-price">$${fixed(payload.target_price, 4)}</span>
        <span class="predict-move">${moveSign}${fixed(payload.expected_pct_move, 2)}%</span>
        ${payload.capped ? `<span class="predict-capped" title="Hard-capped at \u00b1${capCeiling}% to prevent anomaly-driven blow-outs">(capped)</span>` : ''}
      </div>
      <div class="predict-meta">
        <span>From <strong>$${fixed(payload.current_price, 4)}</strong></span>
        <span>Range <strong>$${fixed(payload.low_price, 4)} \u2013 $${fixed(payload.high_price, 4)}</strong> <em>(95% CI)</em></span>
        <span class="predict-conf ${confClass}" title="${isAdvanced ? 'Combines GARCH sample richness, posterior precision shrinkage, signal-to-noise z' : 'Combines composite strength, factor agreement, signal-to-noise, regulatory + consensus bonuses.'}">
          Confidence <strong>${conf.toFixed(1)}%</strong>
        </span>
      </div>
      <div class="predict-meta">
        <span>Direction <strong>${payload.direction}</strong></span>
        <span>Composite <strong>${fixed(payload.composite_score, 1)}/100</strong> (${payload.composite_direction || 'Neutral'})</span>
        ${isAdvanced ? '' : `
        <span>Strength <strong>${fixed(payload.strength_pct, 0)}%</strong></span>
        <span>Agreement <strong>${fixed(payload.agreement_pct, 0)}%</strong></span>
        <span>ATR <strong>${fixed(payload.atr_pct, 2)}%</strong> per ${payload.horizon_unit_label || 'day'}</span>
        `}
        ${advancedBadges}
      </div>
      ${isAdvanced && bayesContribs.length ? `
      <div class="predict-section-title">Bayesian factor contributions (posterior share)</div>
      <div class="predict-factor-grid">
        ${bayesContribs.map((c) => {
          const share = num(c.posterior_share_pct);
          const max = Math.max(0.001, ...bayesContribs.map(x => Math.abs(num(x.posterior_share_pct))));
          const pct = (Math.abs(share) / max) * 100;
          const cls = share > 0 ? 'predict-bar-bull' : share < 0 ? 'predict-bar-bear' : 'predict-bar-flat';
          return `<div class="predict-factor-row">
            <span class="predict-factor-name">${c.family.replace(/_/g, ' ')}</span>
            <span class="predict-factor-score">${fixed(c.raw_score, 0)}</span>
            <div class="predict-factor-bar-wrap"><div class="predict-factor-bar ${cls}" style="width:${pct.toFixed(1)}%"></div></div>
            <span class="predict-factor-contrib">${share >= 0 ? '+' : ''}${share.toFixed(4)}%</span>
          </div>`;
        }).join('')}
      </div>
      <div class="predict-section-title">GARCH + regime</div>
      <div class="predict-meta">
        <span title="GARCH(1,1) source: 'garch' (full fit) or 'fallback' (sample-var × h when n<20)">Vol model <strong>${vol.source || '\u2014'}</strong> (n=${vol.n_observations || 0})</span>
        <span>α=${fixed(vol.garch_alpha, 2)}, β=${fixed(vol.garch_beta, 2)}, persistence=${fixed(vol.persistence, 3)}</span>
        <span>Reaction <strong>${regime.reaction_classification || 'NEUTRAL'}</strong> drift×${fixed(regime.reaction_drift_mult, 2)}</span>
        <span>GEX sign <strong>${regime.gex_sign != null ? (regime.gex_sign > 0 ? '+ (mean-revert)' : regime.gex_sign < 0 ? '- (amplify)' : '0') : '\u2014'}</strong> drift×${fixed(regime.gex_drift_mult, 2)}</span>
      </div>
      ` : `
      <div class="predict-section-title">Factor contributions</div>
      <div class="predict-factor-grid">
        ${fams.map((f) => {
          const c = num(f.contribution_pct);
          const pct = (Math.abs(c) / totalAbsContrib) * 100;
          const cls = c > 0.05 ? 'predict-bar-bull' : c < -0.05 ? 'predict-bar-bear' : 'predict-bar-flat';
          return `<div class="predict-factor-row">
            <span class="predict-factor-name">${f.family.replace(/_/g, ' ')}</span>
            <span class="predict-factor-score">${f.score != null ? fixed(f.score, 0) : '\u2014'}</span>
            <div class="predict-factor-bar-wrap"><div class="predict-factor-bar ${cls}" style="width:${pct.toFixed(1)}%"></div></div>
            <span class="predict-factor-contrib">${c >= 0 ? '+' : ''}${c.toFixed(2)}%</span>
          </div>`;
        }).join('')}
      </div>
      <div class="predict-section-title">Modulators</div>
      <div class="predict-meta">
        <span>Reaction <strong>${mods.reaction_classification || 'NEUTRAL'}</strong> (\u00d7${fixed(mods.reaction_multiplier, 2)})</span>
        <span>Volume conviction <strong>${fixed(mods.volume_conviction, 0)}/100</strong> (\u00d7${fixed(mods.volume_multiplier, 2)})</span>
        ${mods.regulatory_multiplier != null ? `
        <span class="${mods.regulatory_multiplier > 1.01 ? 'predict-reg-amp' : mods.regulatory_multiplier < 0.99 ? 'predict-reg-damp' : ''}" title="Insider transactions + federal contract awards from the regulatory monitor">
          Regulator <strong>${mods.regulatory_event_count || 0} ev / ${num(mods.regulatory_score_delta) >= 0 ? '+' : ''}${fixed(mods.regulatory_score_delta, 2)}</strong>
          (\u00d7${fixed(mods.regulatory_multiplier, 2)}${num(mods.regulatory_confidence_bonus) > 0 ? `, +${fixed(mods.regulatory_confidence_bonus, 1)}pp conf` : ''})
        </span>` : ''}
        ${intradayMods ? `
        <span class="predict-mod-intraday" title="Intraday-aware accuracy boosts">
          Intraday boosts
          ${gammaDriftMult != null && gammaDriftMult !== 1 ? ` \u00b7 gamma drift \u00d7${gammaDriftMult}` : ''}
          ${gammaSigmaMult != null && gammaSigmaMult !== 1 ? ` \u00b7 gamma sigma \u00d7${gammaSigmaMult}` : ''}
          ${iobDriftMult != null && iobDriftMult !== 1 ? ` \u00b7 IOB dampener \u00d7${iobDriftMult}` : ''}
          ${pcConfBonus != null && pcConfBonus > 0 ? ` \u00b7 +${fixed(pcConfBonus, 1)}pp consensus` : ''}
        </span>` : ''}
      </div>
      `}
      <details class="predict-reasoning">
        <summary>How was this computed?</summary>
        <ul>${(payload.reasoning || []).map((r) => `<li>${r}</li>`).join('')}</ul>
      </details>
      <div class="predict-save-row" data-testid="predict-save-row">
        <textarea id="savePredictionNotes" class="predict-notes" rows="2"
          placeholder="Optional notes (e.g. catalyst, position size, conviction)\u2026"
          maxlength="1000" data-testid="save-prediction-notes"></textarea>
        <button id="savePredictionBtn" type="button" class="predict-save-button"
          data-testid="save-prediction-btn">Save prediction</button>
        <div id="savePredictionStatus" class="predict-save-status" data-testid="save-prediction-status"></div>
      </div>
    </div>`;
  // Bind the Save button now that it's in the DOM.
  const saveBtn = byId('savePredictionBtn');
  if (saveBtn) saveBtn.addEventListener('click', savePrediction);
  // Phase 26.43: cache the rendered HTML so live-tick re-renders
  // don't wipe the prediction the user just generated.
  state.cachedPredictionCard = {
    symbol: payload.symbol,
    engine: payload.engine || 'legacy',
    html: out.innerHTML,
  };
}

function renderBacktest(payload) {
  const out = byId('backtestResults');
  if (!out) return;
  if (!payload || payload.status !== 'implemented') {
    out.innerHTML = `<div class="backtest-card"><div class="eyebrow">Backtest</div><div class="add-status">Unavailable: ${payload?.reason || 'unknown'}. Symbol may need to enter the active-scan pool first so daily history is cached.</div></div>`;
    return;
  }
  const hit = (payload.hit_rate * 100).toFixed(1);
  const baseline = (payload.baseline_random * 100).toFixed(1);
  const pc = payload.per_class || {};
  const samples = (payload.sample_predictions || []).slice(0, 10);
  out.innerHTML = `
    <div class="backtest-card">
      <div class="eyebrow">Backtest \u00b7 ${payload.symbol}</div>
      <div class="backtest-stats">
        <span>Hit rate <strong>${hit}%</strong></span>
        <span>Random baseline <strong>${baseline}%</strong></span>
        ${payload.balanced_hit_rate != null ? `<span>Balanced <strong>${(payload.balanced_hit_rate * 100).toFixed(1)}%</strong></span>` : ''}
        ${payload.confident_hit_rate != null ? `<span>Confident <strong>${(payload.confident_hit_rate * 100).toFixed(1)}%</strong> (${payload.confident_total || 0})</span>` : ''}
        <span>Predictions <strong>${payload.total_predictions}</strong></span>
        <span>Bars <strong>${payload.bars_used}</strong></span>
        <span>Forward window <strong>${payload.forward_bars} bars</strong></span>
      </div>
      <div class="backtest-stats">
        ${Object.entries(pc).map(([cls, info]) =>
          `<span>${cls} <strong>${(info.precision * 100).toFixed(1)}%</strong> (${info.correct_count}/${info.predicted_count})</span>`
        ).join('')}
      </div>
      <div class="bt-table-wrap">
        <table class="bt-table"><thead><tr><th>Bar</th><th>Price</th><th>Zone</th><th>Tier</th><th>Predicted</th><th>Actual</th><th>P / R / C</th></tr></thead><tbody>
          ${samples.map((s) =>
            `<tr><td>${s.bar_index}</td><td>${s.price}</td><td>${s.zone_midpoint}</td><td>${s.zone_tier}</td><td class="${s.correct ? 'bt-correct' : 'bt-wrong'}">${s.predicted}</td><td class="${s.correct ? 'bt-correct' : 'bt-wrong'}">${s.actual}</td><td>${(s.probabilities.propel * 100).toFixed(0)}/${(s.probabilities.reject * 100).toFixed(0)}/${(s.probabilities.chop * 100).toFixed(0)}</td></tr>`
          ).join('')}
        </tbody></table>
      </div>
    </div>`;
  // Phase 26.43: cache so live-tick re-render doesn't wipe it.
  state.cachedBacktestCard = { symbol: payload.symbol, html: out.innerHTML };
}

function renderZoneList(zones) {
  if (!zones || !zones.length) return '<div class="add-status">No detected zones yet \u2014 awaiting daily history.</div>';
  const miniBar = (label, prob, cls) => {
    const pct = Math.max(0, Math.min(100, Math.round((Number(prob) || 0) * 100)));
    return `<div class="zone-mini-bar"><span class="zone-mini-label">${label}</span><div class="prob-bar"><div class="prob-fill" style="width:${pct}%"></div></div><span class="zone-mini-pct">${pct}%</span></div>`;
  };
  return `<div class="zones-list">${zones.map((z) => `
    <div class="zone-row">
      <div>
        <div class="zone-tier zone-tier-${z.tier}">${z.tier}</div>
        <div style="font-size:.7rem;color:var(--muted);margin-top:.2rem">${z.midpoint}<br>${z.distance_pct}% off</div>
      </div>
      <div style="font-size:.74rem;line-height:1.5;color:var(--muted)">
        ${z.touch_count}x touches \u00b7 ${z.kind_mix}<br>
        evidence ${Number(z.evidence_score).toFixed(0)} \u00b7 reject ${z.rejection_strength}%<br>
        cls <strong style="color:var(--text)">${z.classification}</strong>
      </div>
      <div class="zone-mini-bars">
        ${miniBar('P', z.propel_probability)}
        ${miniBar('R', z.reject_probability)}
        ${miniBar('C', z.chop_probability)}
      </div>
    </div>
  `).join('')}</div>`;
}

function provenanceBadge(row) {
  if (!row) return '';
  if (row.state === 'stale-ok' || row.lkg_fallback) return '<span class="badge badge-stale">STALE-OK</span>';
  if (row.preview_only) return '<span class="badge badge-preview">PREVIEW</span>';
  const ds = String(row.data_source || '');
  // Live providers (any cascade tier that fetched real data this pass)
  if (ds.startsWith('yfinance') || ds === 'coingecko' || ds === 'yahoo-chart' || ds === 'cryptocompare') {
    const label = ds.toUpperCase().replace('-', ' ');
    return `<span class="badge badge-live" title="Live data from ${ds}">${label}</span>`;
  }
  if (ds === 'stooq') return '<span class="badge badge-live" title="EOD data from Stooq">STOOQ</span>';
  if (ds.startsWith('cache')) return '<span class="badge badge-cache">CACHE</span>';
  if (ds === 'inferred') return '<span class="badge badge-inferred">INFERRED</span>';
  if (ds === 'unavailable' || ds === 'preview_fallback') return '<span class="badge badge-unavailable">N/A</span>';
  return `<span class="badge">${ds || 'unknown'}</span>`;
}

function factorBadge(score, low = 40, high = 60) {
  const v = Number(score || 0);
  if (v >= high) return 'badge-strong';
  if (v <= low) return 'badge-weak';
  return 'badge-neutral';
}

function resetFiltersToDefault() {
  state.filters = { preset: '', direction: '', tier: '', min_score: 0, max_exit_risk: 100, exit_flag: '' };
  saveUiPrefs();
  applyPrefsToControls();
}

function refreshVisibleAgeCells() {
  document.querySelectorAll('#resultsBody tr[data-symbol]').forEach((tr) => {
    const symbol = tr.dataset.symbol;
    const row = (state.currentView === 'tracked' ? activeTrackedMap().get(symbol) : activeMarketMap().get(symbol)) || {};
    const ageCell = tr.querySelector('.col-age');
    if (ageCell) {
      ageCell.title = `Age ${humanAge(row.age_seconds ?? 0)} \u00b7 captured ${row.as_of_utc || 'unknown'}`;
    }
  });
}

function humanAge(seconds) {
  const s = Math.max(0, Number(seconds || 0));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function activeMarketMap() {
  return state.marketRowsMap[state.currentMarket] || state.marketRowsMap.stocks;
}

function marketMapFor(market) {
  return state.marketRowsMap[market] || new Map();
}

function activeTrackedMap() {
  return state.marketTrackedRows[state.currentMarket] || state.marketTrackedRows.stocks;
}

function trackedMapFor(market) {
  return state.marketTrackedRows[market] || new Map();
}

function passCountMapFor(market) {
  return state.marketTop25PassCounts[market] || new Map();
}

function activePassCountMap() {
  return state.marketTop25PassCounts[state.currentMarket] || state.marketTop25PassCounts.stocks;
}

function mergeResults(rows, market = state.currentMarket) {
  const now = new Date().toISOString();
  const marketMap = marketMapFor(market);
  let orderChanged = false; // Phase 26.37b: split "any mutation" from "sort-affecting mutation"
  for (const row of rows || []) {
    if (!row || !row.symbol) continue;
    const existingMaster = marketMap.get(row.symbol) || {};
    const merged = { ...existingMaster, ...row, market, last_seen_utc: row.as_of_utc || existingMaster.last_seen_utc || now, as_of_utc: row.as_of_utc || existingMaster.as_of_utc || now, age_seconds: Number(row.age_seconds ?? existingMaster.age_seconds ?? 0), freshness_label: row.freshness_label || existingMaster.freshness_label || 'unknown' };
    // Phase 26.37b — Frontend sort/render sweep:
    // Previously we set `mutated = true` on every row, which invalidated
    // the memoized filter+sort cache on every poll even when nothing
    // sort-relevant changed.  At ~5,000 rows that's a full re-sort
    // every 1-5 s of polling.
    //
    // The leaderboard is ordered purely by `final_score`, so we only
    // need to dirty the sort cache when:
    //   (a) this is a brand-new symbol (changes the row set), OR
    //   (b) `final_score` actually changed for an existing symbol.
    // Freshness ticks, age_seconds drift, and provenance label
    // refreshes don't reorder rows — those still update the row dict
    // (so the detail panel sees fresh data) but the table memoization
    // stays valid.
    if (!existingMaster.symbol) {
      orderChanged = true;
    } else if ((existingMaster.final_score ?? null) !== (row.final_score ?? existingMaster.final_score ?? null)) {
      orderChanged = true;
    }
    marketMap.set(row.symbol, merged);
    state.allRowsMap.set(`${market}:${row.symbol}`, merged);
  }
  state.marketRowsMap[market] = marketMap;
  if (orderChanged) bumpFilterSortCache();
}

function rowExitModel(row) {
  return ((row.factor_breakdown || {}).exit_model || {});
}

function rowMatchesFilters(row) {
  if (!row) return false;
  // Core filters
  if (state.filters.direction && row.final_direction !== state.filters.direction) return false;
  if (state.filters.tier && row.tier !== state.filters.tier) return false;
  if (Number(row.final_score || 0) < Number(state.filters.min_score || 0)) return false;
  const exitModel = rowExitModel(row);
  const exitScore = Number(exitModel.score ?? 0);
  const exitFlag = String(exitModel.exit_flag || '');
  if (Number(state.filters.max_exit_risk ?? 100) < 100 && exitScore > Number(state.filters.max_exit_risk)) return false;
  if (state.filters.exit_flag && exitFlag !== state.filters.exit_flag) return false;

  // --- Extended factor filters (formerly server-side only) ---
  const sm = row.scanner_metrics || {};
  const market = ((row.factor_breakdown || {}).market) || {};
  const icf = market.institutional_confluence || {};
  const opt = market.options_positioning || {};
  const iob = market.institutional_order_block || {};
  const dp = market.dark_pool_proxy || {};

  // Numeric thresholds (these compare against the scanner_metrics 0-100 normalized values)
  if (Number(state.filters.min_institutional_confluence || 0) > 0) {
    const v = Number(sm.institutional_confluence ?? icf.score ?? 0);
    if (v < Number(state.filters.min_institutional_confluence)) return false;
  }
  if (Number(state.filters.min_options_positioning || 0) > 0) {
    const v = Number(sm.options_positioning ?? opt.score ?? 0);
    if (v < Number(state.filters.min_options_positioning)) return false;
  }

  // Categorical *_in filters: comma-separated whitelist semantics matching the backend.
  const matchIn = (filterVal, candidates) => {
    if (!filterVal) return true;
    const accepted = String(filterVal).split(',').map((s) => s.trim()).filter(Boolean);
    if (!accepted.length) return true;
    const cand = candidates.filter((c) => c !== undefined && c !== null && c !== '').map((c) => String(c));
    return cand.some((c) => accepted.includes(c));
  };

  if (!matchIn(state.filters.institutional_bias_in, [icf.bias, sm.institutional_bias])) return false;
  if (!matchIn(state.filters.options_bias_in, [opt.bias, sm.options_bias])) return false;
  if (!matchIn(state.filters.iob_state_in, [iob.state, sm.iob_state])) return false;
  if (!matchIn(state.filters.dark_pool_attraction_state_in, [dp.attraction_state, sm.dark_pool_attraction_state])) return false;
  if (!matchIn(state.filters.options_gamma_level_in, [opt.gamma_level_label, sm.options_gamma_level_label])) return false;

  // Phase 4b
  const reaction = market.reaction_map || {};
  const vs = market.volume_sentiment || {};
  if (!matchIn(state.filters.reaction_classification_in, [reaction.reaction_classification, sm.reaction_classification])) return false;
  if (!matchIn(state.filters.dominant_zone_tier_in, [reaction.dominant_zone_tier, sm.dominant_zone_tier])) return false;
  if (!matchIn(state.filters.volume_sentiment_bias_in, [vs.bias, sm.volume_sentiment_bias])) return false;
  if (!matchIn(state.filters.effort_vs_result_in, [vs.effort_vs_result, sm.effort_vs_result])) return false;

  if (Number(state.filters.min_volume_sentiment_conviction || 0) > 0) {
    const v = Number(sm.volume_sentiment_conviction ?? vs.conviction ?? 0);
    if (v < Number(state.filters.min_volume_sentiment_conviction)) return false;
  }

  // --- Scanner-context filters (short pressure / PVI / expirations) ---
  if (Number(state.filters.min_predicted_volume_intensity || 0) > 0) {
    if (Number(row.predicted_volume_intensity_score ?? 0) < Number(state.filters.min_predicted_volume_intensity)) return false;
  }
  if (!matchIn(state.filters.predicted_volume_intensity_bucket_in, [row.predicted_volume_intensity_bucket])) return false;
  if (Number(state.filters.min_short_selling_pressure || 0) > 0) {
    if (Number(row.short_selling_pressure_score ?? 50) < Number(state.filters.min_short_selling_pressure)) return false;
  }
  if (!matchIn(state.filters.short_selling_pressure_label_in, [row.short_selling_pressure_label])) return false;
  if (state.filters.max_days_to_options_expiration !== '' && state.filters.max_days_to_options_expiration != null) {
    const dte = row.days_to_options_expiration;
    if (dte == null || Number(dte) > Number(state.filters.max_days_to_options_expiration)) return false;
  }
  if (state.filters.expiration_risk_only && !row.expiration_risk_flag) return false;

  return true;
}

// ---------- Memoization of expensive list operations ----------
// At 3k+ symbols, re-running filter+sort across the entire universe on
// every batch + every status render hits double-digit-ms latency per call.
// Cache the result keyed by a tag bumped only when the underlying data
// (filters, sort, market, row content) actually changes.
let _filterSortCacheTag = 0;
let _filterSortCacheKey = null;
let _filterSortCacheValue = null;

function bumpFilterSortCache() { _filterSortCacheTag += 1; }

function _filterSortKey() {
  return [
    state.currentMarket,
    state.currentView,
    _filterSortCacheTag,
    state.tradingStyle || 'default',  // Phase 26.40: invalidate cache on style change
    state.useAdvancedRanking ? 'adv' : 'std', // Phase 26.42: invalidate on ranking-engine change
    state.futureMode ? `fm:${state.futureHorizon || '1h_hold'}:${state.futureFilter || 'all'}:${state.futureIntensity || 'all'}` : 'fm:off', // Phase 26.47 + 26.49 + 26.50
    state.useLabMode ? (state.blendLabIntoRanking ? 'lab:blend' : 'lab:on') : 'lab:off', // Phase 26.49
    state.pviPriority ? 'pvi:first' : 'pvi:off', // predicted-volume-first ordering mode
    state.useStrategyMode ? (state.blendStrategyIntoRanking ? 'strat:blend' : 'strat:on') : 'strat:off', // Phase 26.50
    state.bullBullPriority ? 'bb:1' : 'bb:0',  // Phase 26.65 — Bull×Bull priority
    state.advancedExperimentalMode ? `rbf:${state.rbFilter || 'all'}` : 'rbf:off', // Phase 26.65 — RB rating filter
    `csf:${state.consensusFilter || 'all'}`,   // Phase 26.68 — consensus filter
    `css:${state.consensusSort || 'off'}`,     // Phase 26.68 — consensus sort
    JSON.stringify(state.filters || {}),
  ].join('|');
}

// =========================================================================
// Phase 26.40: trading-style score re-blending (leveraged variant only).
//
// The user's "trading style" picks one of four weight presets that
// re-blend the four algorithm ratings (momentum / quality / trend /
// stability) plus two extended-factor families (options_positioning,
// volume_sentiment) into a single 0-100 "style-adjusted" score.  The
// table is sorted by THAT score in non-default modes.  In default
// mode the function falls through and just returns the row's own
// `final_score` (no behavioural change vs. main app).
// =========================================================================
const _TRADING_STYLE_WEIGHTS = {
  // Short-term: lean hard on momentum + intraday flows.  Drop trend /
  // quality / stability — those reflect long-haul structure that's
  // irrelevant for a 1-2-day hold.
  short: { momentum: 0.40, options_positioning: 0.25, volume_sentiment: 0.20, stability: 0.05, quality: 0.05, trend: 0.05 },
  // Swing (close to the current composite blend).
  swing: { momentum: 0.25, trend: 0.25, quality: 0.15, stability: 0.15, options_positioning: 0.10, volume_sentiment: 0.10 },
  // Long: emphasise structural fundamentals + persistent trend.
  long:  { trend: 0.35, quality: 0.30, stability: 0.20, momentum: 0.10, options_positioning: 0.03, volume_sentiment: 0.02 },
};

function _pickStyleInputScore(row, key) {
  // First check the algorithm_ratings shape (4 rating cards).
  const ratings = row.algorithm_ratings || {};
  const r = ratings[key];
  if (r && typeof r.score === 'number') return Math.max(0, Math.min(100, r.score));
  // Fallback to factor_breakdown.{key}.score (cheap-pass exposes raw
  // composite components here for M/Q/T/S).
  const fb = row.factor_breakdown || {};
  if (typeof fb[key] === 'number') return Math.max(0, Math.min(100, fb[key]));
  // Extended-factor families live under factor_breakdown.market.{key}.
  const mkt = fb.market || {};
  const ext = mkt[key];
  if (ext) {
    const candidates = [ext.score, ext.composite, ext.bias_score];
    for (const c of candidates) {
      if (typeof c === 'number') return Math.max(0, Math.min(100, c));
    }
  }
  return null;
}

function styleAdjustedScore(row, style = state.tradingStyle) {
  if (!row) return 0;
  if (!style || style === 'default') return Number(row.final_score || 0);
  const weights = _TRADING_STYLE_WEIGHTS[style];
  if (!weights) return Number(row.final_score || 0);
  let blended = 0;
  let usedWeight = 0;
  for (const [key, w] of Object.entries(weights)) {
    const s = _pickStyleInputScore(row, key);
    if (s != null) {
      blended += s * w;
      usedWeight += w;
    }
  }
  // If we got at least 60% of the requested weight, trust the blended
  // score.  Otherwise (cheap-pass row with no extended families)
  // fall back to final_score so we don't generate phantom rankings.
  if (usedWeight >= 0.6) return blended / usedWeight;
  return Number(row.final_score || 0);
}

// =========================================================================
// Phase 26.42 — Client-side Bayesian-Kelly rank score.
//
// Mirrors `app/services/bayesian_factor_blend.py` (server-side
// inverse-variance posterior over factor families) but skips the
// GARCH volatility forecast.  Instead we use a fixed σ_per_period =
// 2 % (the long-run cross-sectional median for liquid US equities)
// as the noise proxy for the directional certainty calc.  That's
// good enough to RANK rows; the detail panel still calls the full
// /api/predict/advanced/* route which DOES run GARCH for the price
// target + bands.
//
// Per-factor drift coefficients (daily, % per period per +1σ z) are
// the same literature-cited values used server-side.  The intraday
// column is unused here — ranking is over the daily horizon.
// =========================================================================
const _CLIENT_BAYES_FACTOR_BPS = {
  momentum:                   10.0,
  trend:                       8.0,
  volume_sentiment:            5.0,
  options_positioning:         7.0,
  institutional_confluence:    6.0,
  institutional_order_block:   4.0,
  dark_pool_proxy:             3.0,
  reaction_clustering:         5.0,
  quality:                     5.0,
  stability:                   3.0,
};
const _CLIENT_BAYES_CONF_RATIO = 3.0;
const _CLIENT_BAYES_HORIZON_DAYS = 5;     // 5-day Kelly horizon for ranking
const _CLIENT_BAYES_SIGMA_DAILY_PCT = 2.0;  // ~ cross-sectional median

function advancedRankScore(row) {
  if (!row) return 0;
  const pickScore = (key) => _pickStyleInputScore(row, key);   // 0-100 or null
  let sumPrec = 0, sumWeighted = 0;
  for (const [name, dailyBps] of Object.entries(_CLIENT_BAYES_FACTOR_BPS)) {
    const score = pickScore(name);
    if (score == null) continue;
    const z = Math.max(-1, Math.min(1, (score - 50) / 50));
    const perUnitDrift = dailyBps * 0.01;       // bps → percent
    const drift = z * perUnitDrift;
    const sigma = Math.abs(perUnitDrift) * _CLIENT_BAYES_CONF_RATIO;
    if (sigma <= 0) continue;
    const prec = 1.0 / (sigma * sigma);
    sumPrec += prec;
    sumWeighted += prec * drift;
  }
  if (sumPrec <= 0) return Number(row.final_score || 0) * 0;  // no factor coverage -> 0 advanced score
  // James-Stein-style shrinkage toward zero
  const shrinkRef = 5.0;
  const shrink = sumPrec / (sumPrec + shrinkRef);
  const postDrift = (sumWeighted / sumPrec) * shrink;
  // Horizon scaling: drift × h, sigma × sqrt(h)
  const driftHorizon = postDrift * _CLIENT_BAYES_HORIZON_DAYS;
  const sigmaHorizon = _CLIENT_BAYES_SIGMA_DAILY_PCT * Math.sqrt(_CLIENT_BAYES_HORIZON_DAYS);
  // Probit direction probability
  const z = sigmaHorizon > 0 ? driftHorizon / sigmaHorizon : 0;
  const pUp = 0.5 * (1.0 + _erf(z / Math.SQRT2));
  const directionalCertainty = Math.max(0, 2 * Math.abs(pUp - 0.5));
  // Kelly-like: signed expected return × sqrt(precision) × directional certainty
  return driftHorizon * Math.sqrt(sumPrec) * directionalCertainty;
}

// Pure-JS Abramowitz/Stegun erf approximation; max error ≈ 1.5e-7.
function _erf(x) {
  const sign = x < 0 ? -1 : 1;
  x = Math.abs(x);
  const a1 =  0.254829592, a2 = -0.284496736, a3 =  1.421413741;
  const a4 = -1.453152027, a5 =  1.061405429, p  =  0.3275911;
  const t = 1.0 / (1.0 + p * x);
  const y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * Math.exp(-x * x);
  return sign * y;
}

function rankScoreForRow(row) {
  // Single source of truth for "what scalar does the leaderboard sort by".
  // Future Mode wins if enabled and the row has forward_metrics;
  // advanced ranking is next; otherwise trading-style if non-default;
  // otherwise the raw composite final_score.
  if (state.futureMode) {
    const fm = futureMetricsForRow(row);
    if (fm) {
      let rank = Number.isFinite(fm.effective_kelly_rank) ? fm.effective_kelly_rank : 0;
      // Phase 26.49 — Lab Mode blend.  Multiply by composite Lab
      // multiplier in [0.6, 1.4] when the user has opted in.
      if (state.useLabMode && state.blendLabIntoRanking
          && Number.isFinite(fm.lab_rank_multiplier)) {
        rank *= fm.lab_rank_multiplier;
      }
      // Phase 26.50 — Strategy Tier blend.  Multiply by composite
      // Strategy multiplier in [0.6, 1.4] when its blend toggle is on.
      // When BOTH Lab and Strategy blends are on, they multiply
      // multiplicatively (range [0.36, 1.96]).
      if (state.useStrategyMode && state.blendStrategyIntoRanking
          && Number.isFinite(fm.strategy_rank_multiplier)) {
        rank *= fm.strategy_rank_multiplier;
      }
      // ---------------------------------------------------------------
      // Phase 26.60 — Predictive Expansion Pack multipliers.
      // Final pipeline (per the spec) is:
      //   effective_kelly_rank
      //     × lab_rank_multiplier^1_Lab
      //     × strategy_rank_multiplier^1_Strategy
      //     × strategy_v2_rank_multiplier^1_StrategyV2
      //     × regime_risk_multiplier^1_RegimeRisk
      //     × liq_kelly_factor^1_LiqKelly
      //     × ml_rank_multiplier^1_ML
      //     × reality_breaker_multiplier^1_RealityBreaker   (only when
      //       Advanced Experimental Mode is ON AND the user opts in)
      // ---------------------------------------------------------------
      if (state.useStrategyV2Mode && state.blendStrategyV2IntoRanking
          && Number.isFinite(fm.strategy_v2_rank_multiplier)) {
        rank *= fm.strategy_v2_rank_multiplier;
      }
      if (state.useRegimeRiskMode && state.blendRegimeRiskIntoRanking
          && Number.isFinite(fm.regime_risk_multiplier)) {
        rank *= fm.regime_risk_multiplier;
      }
      if (state.blendLiqKellyFactor && Number.isFinite(fm.liq_kelly_factor)) {
        rank *= fm.liq_kelly_factor;
      }
      if (state.useMlOverlayMode && state.blendMlOverlayIntoRanking
          && Number.isFinite(fm.ml_rank_multiplier)) {
        rank *= fm.ml_rank_multiplier;
      }
      // Reality breaker — strictly gated.  Triple-guard:
      //   1) advancedExperimentalMode (master)
      //   2) blendRealityBreakerIntoRanking (per-blend opt-in)
      //   3) reality_breaker_multiplier present + finite
      if (state.advancedExperimentalMode
          && state.blendRealityBreakerIntoRanking
          && Number.isFinite(fm.reality_breaker_multiplier)) {
        let rbMult = Number(fm.reality_breaker_multiplier);
        // Phase 26.61c — Unlocked mode amplifies the reality_breaker
        // multiplier's effect: raw multiplier squared, no [0.5, 1.5]
        // clamp.  In guarded (default) mode, the multiplier is already
        // clamped server-side and uses its natural value here.
        if (state.advancedExperimentalUnlocked) {
          // Square the deviation around 1.0 to amplify (mult^2 keeps
          // the bull/bear sign while doubling the rank impact).
          rbMult = Math.pow(rbMult, 2);
        }
        rank *= rbMult;
      }
      // Filter mode decides sort direction:
      const filter = state.futureFilter || 'all';
      if (filter === 'bulls') return rank > 0 ? rank : 0;
      if (filter === 'bears') return rank < 0 ? -rank : 0;
      return Math.abs(rank);
    }
    // Fall through — row didn't carry forward_metrics (cheap-pass).
  }
  if (state.useAdvancedRanking) return advancedRankScore(row);
  return styleAdjustedScore(row);
}

// Phase 26.50 — intensity-band thresholds (applied via passesFutureFilter).
const _FUTURE_INTENSITY_THRESHOLDS = {
  'all':      0.0,
  'moderate': 0.30,
  'strong':   0.55,
  'max':      0.75,
};

// Phase 26.49 — Future Mode directional filter.  When `futureFilter`
// is 'bulls' or 'bears', exclude rows whose CF direction disagrees
// with the chosen side.  Rows without forward_metrics are kept
// (they retain their classical rank).
// Phase 26.50 — also enforces the intensity band:  drops rows whose
// directional_certainty_cf is below the band threshold.
function passesFutureFilter(row) {
  if (!state.futureMode) return true;
  const filter = state.futureFilter || 'all';
  const intensity = state.futureIntensity || 'all';
  const minCert = _FUTURE_INTENSITY_THRESHOLDS[intensity] || 0.0;
  const fm = futureMetricsForRow(row);
  if (!fm) {
    // No forecast info — only drop if the user is asking for a
    // non-trivial intensity (in which case rows without forward
    // metrics simply cannot meet the bar).
    return minCert <= 0.0;
  }
  const dir = fm.direction_cf || fm.direction || 'Neutral';
  if (filter === 'bulls' && dir !== 'Bullish') return false;
  if (filter === 'bears' && dir !== 'Bearish') return false;
  const cert = Number.isFinite(fm.directional_certainty_cf) ? fm.directional_certainty_cf : 0;
  if (cert < minCert) return false;
  return true;
}

// Phase 26.65 — Reality-Breaker overall-rating list filter.  Only active
// when Advanced Experimental Mode is ON.  Buckets each row by its
// reality_breaker_multiplier (the overlays' net "overall rating"):
//   endorse  → multiplier > 1.03  (overlays reinforce the base call)
//   caution  → multiplier < 0.97  (overlays push back / dampen)
//   neutral  → 0.97 ≤ multiplier ≤ 1.03
// Rows lacking the multiplier are kept only under the 'all' selection.
const _RB_FILTER_ENDORSE = 1.03;
const _RB_FILTER_CAUTION = 0.97;
function passesRealityBreakerFilter(row) {
  const sel = state.rbFilter || 'all';
  if (sel === 'all') return true;
  if (!state.advancedExperimentalMode) return true;
  const fm = futureMetricsForRow(row);
  const mult = fm && Number.isFinite(fm.reality_breaker_multiplier)
    ? fm.reality_breaker_multiplier : null;
  if (mult === null) return false;
  if (sel === 'endorse') return mult > _RB_FILTER_ENDORSE;
  if (sel === 'caution') return mult < _RB_FILTER_CAUTION;
  if (sel === 'neutral') return mult >= _RB_FILTER_CAUTION && mult <= _RB_FILTER_ENDORSE;
  return true;
}

// =========================================================================
// Phase 26.47 — Future Mode helpers.
//
// `futureMetricsForRow(row, horizonOverride?)` returns the per-horizon
// block (GARCH-tier preferred when available; falls back to fast tier)
// for the current state.futureHorizon.  Returns null when no forward
// metrics are attached to the row.
//
// The horizon → forward_metrics key mapping mirrors the backend's
// `TRADING_STYLE_HORIZONS` table in `future_mode_service.py`.
// =========================================================================
const _FUTURE_HORIZON_KEY = {
  '1h_hold': 'forward_1h',
  '5h_hold': 'forward_5h',
  'overnight_hold': 'forward_overnight',
  'weekend_hold': 'forward_weekend',
  'short':   'forward_1d',
  'swing':   'forward_5d',
  'long':    'forward_20d',
};

const _FUTURE_HORIZON_LABEL = {
  '1h_hold': '1h hold',
  '5h_hold': '5h hold',
  'overnight_hold': 'Overnight',
  'weekend_hold': 'Weekend',
  'short':   '1-2d hold',
  'swing':   '3-10d swing',
  'long':    '10+d long',
};

// =========================================================================
// Phase 26.49 — Metric explanation dictionary for click-to-pin info
// popovers in the Future Forecast detail card.  Each entry covers ALL
// metrics the card surfaces (fast tier, GARCH tier, advanced signals,
// lab signals).  The popover persists across the 2s live-tick
// re-render because `state.pinnedMetric` is preserved on the global
// state object and re-attached on every render.
// =========================================================================
const FF_METRIC_INFO = {
  // -------- Per-horizon block (fast + GARCH tier) ----------
  'direction_cf': {
    label: 'Direction (CF)',
    summary: 'The trade direction picked by the Cornish-Fisher fat-tail-adjusted P(up).  When CF disagrees with the Gaussian drift, the rank is dampened automatically.',
    interpretation: 'Bullish (P(up)CF > 0.55), Bearish (< 0.45), Neutral otherwise.',
    impact: 'Controls the SIGN of the effective Kelly rank.  Bulls Only / Bears Only filters use this field.',
  },
  'p_up_cf': {
    label: 'P(up) CF',
    summary: 'Cornish-Fisher fat-tail-adjusted probability that the return over the horizon is positive.  Accounts for realized skew and excess kurtosis of daily returns.',
    interpretation: '> 0.55 = strongly bullish, 0.45–0.55 = neutral, < 0.45 = bearish.  Reduces to plain Gaussian Φ(drift/σ) when higher moments are negligible.',
    impact: 'Drives directional certainty and the effective Kelly rank — this is the canonical Future Mode ranking input.',
  },
  'p_up_gauss': {
    label: 'P(up) Gaussian',
    summary: 'Naive Gaussian probability of upside — assumes normally distributed returns.  Shown for comparison against the CF-adjusted estimate.',
    interpretation: 'For fat-tailed distributions this typically over- or under-estimates P(up).  Watch the gap vs P(up) CF to spot left-tail risk.',
    impact: 'Not used in ranking — informational only.',
  },
  'drift_pct': {
    label: 'Drift %',
    summary: 'Bayesian-blended expected return over the horizon.  Combines factor scores with horizon-scaled weights via posterior precision.',
    interpretation: 'Positive = bullish drift; magnitude in % of price.  Add jump-drift contribution for the full expected move.',
    impact: 'Primary input to effective Kelly rank; signed.',
  },
  'sigma_pct': {
    label: 'Sigma %',
    summary: 'Conditional volatility over the horizon.  Fast tier uses ATR-derived σ; GARCH tier uses fitted GARCH(1,1).',
    interpretation: 'Higher σ = wider expected outcome range.  Used to scale VaR/CVaR and Kelly fraction.',
    impact: 'Reduces effective Kelly rank quadratically through the Kelly fraction.',
  },
  'jump_drift_pct': {
    label: '+ Jump drift',
    summary: 'Expected drift contribution from rare large moves, derived from the jump-diffusion proxy (λ · μ_j · horizon).',
    interpretation: 'Positive when historical jumps tilt upward; negative for chronic downward shocks.',
    impact: 'Added directly to the drift before computing the effective Kelly rank.',
  },
  'var95_pct': {
    label: 'VaR 95%',
    summary: 'Cornish-Fisher Value-at-Risk: the magnitude of the worst expected loss at 95% confidence over the horizon.',
    interpretation: 'A VaR95 of 2.1% means "we expect to lose ≤ 2.1% with 95% confidence."',
    impact: 'Risk-management context.  Not used in ranking but flags asymmetric downside.',
  },
  'cvar95_pct': {
    label: 'CVaR 95%',
    summary: 'Conditional VaR / Expected Shortfall: the average loss in the worst 5% of outcomes.',
    interpretation: 'Always ≥ VaR.  Wider CVaR-VaR gap → fatter left tail.',
    impact: 'Risk diagnostic; supplements VaR with tail-mean info.',
  },
  'directional_certainty_cf': {
    label: 'Certainty CF',
    summary: 'Cornish-Fisher directional certainty = 2 · |P(up)CF − 0.5|.  Range [0, 1].',
    interpretation: '0 = coin flip, 1 = absolute certainty in the direction implied by P(up)CF.',
    impact: 'Multiplied into the effective Kelly rank.  This is the magnitude of conviction Future Mode places on the trade.',
  },
  'kelly_fraction': {
    label: 'Kelly Fraction',
    summary: 'Half-Kelly (Thorp 2006) position-sizing recommendation: half of drift/σ² per the log-utility optimum.',
    interpretation: 'Positive = long; negative = short.  Clamped to ±1 (no infinite leverage).',
    impact: 'Actionable — tells you what fraction of capital to allocate per the model.',
  },
  'effective_kelly_rank': {
    label: 'Effective Kelly Rank',
    summary: 'Final Future Mode sort key: sign(CF direction) · |drift+jump_drift| · √precision · certainty_cf · regime_weight · agreement.',
    interpretation: 'Positive = long opportunity, negative = short opportunity.  Magnitude = conviction × expected move.',
    impact: 'The leaderboard sorts by this when Future Mode is on.  Bulls Only / Bears Only filter by sign.',
  },
  'regime_label': {
    label: 'Regime',
    summary: 'Hurst-derived market regime: trending (H ≥ 0.55), mean-reverting (H ≤ 0.45), or random-walk (0.45–0.55).',
    interpretation: 'Trending regimes boost rank when direction agrees with the trend; mean-reverting regimes do the opposite.',
    impact: 'Modulates the effective Kelly rank via `regime_weight`.',
  },
  // -------- Per-symbol advanced signals ----------
  'hurst_exponent': {
    label: 'Hurst Exponent',
    summary: 'R/S Hurst exponent measuring long-memory in the return series.  Range (0, 1).',
    interpretation: '> 0.5 = trending (persistent); < 0.5 = mean-reverting (anti-persistent); ≈ 0.5 = random walk.',
    impact: 'Drives the regime classification and the regime weight applied to ranking.',
  },
  'realized_skew': {
    label: 'Realized Skew',
    summary: 'Sample skewness of daily log returns (Fisher-Pearson, sample-bias corrected).',
    interpretation: 'Negative = left tail dominates (rare big losses); positive = right tail dominates.',
    impact: 'Feeds the Cornish-Fisher expansion → directly shapes P(up) CF.',
  },
  'realized_excess_kurt': {
    label: 'Excess Kurtosis',
    summary: 'Sample excess kurtosis (Fisher form, normal = 0).  Measures tail thickness.',
    interpretation: '> 1 = fatter-than-normal tails (typical for equities); > 5 = extremely fat-tailed.',
    impact: 'Feeds the Cornish-Fisher expansion → tightens VaR and shifts P(up) CF.',
  },
  'jump_intensity_per_day': {
    label: 'Jump λ / day',
    summary: 'Estimated daily jump intensity from the threshold-jump detector (3.5 · MAD).',
    interpretation: '0 = no jumps in window; 0.05 = roughly one jump every 20 trading days.',
    impact: 'Multiplied by mean jump return to produce the jump-drift overlay on each horizon.',
  },
  'jump_mean_return_pct': {
    label: 'Jump Mean μ',
    summary: 'Average return magnitude on jump days (%).',
    interpretation: 'Negative = jumps tend to be crashes; positive = jumps tend to be rallies.',
    impact: 'Sets the sign and scale of the jump-drift overlay.',
  },
  'ou_half_life_days': {
    label: 'OU Half-Life',
    summary: 'Ornstein-Uhlenbeck mean-reversion half-life fitted on log prices.  Days for a deviation to revert halfway.',
    interpretation: '0 = no mean reversion (random walk); short half-life = strong mean reversion.',
    impact: 'Informational — not currently fed into ranking but useful for short-horizon trade timing.',
  },
  'rv_har_sigma_pct': {
    label: 'HAR-RV σ',
    summary: 'Corsi (2009) Heterogeneous AR realized-vol forecast for the next day, in %.',
    interpretation: 'Combines daily / weekly / monthly realized variance components.',
    impact: 'Reference σ shown for comparison against GARCH.  Not yet ranked-on directly.',
  },
  // -------- Lab Mode (experimental) signals ----------
  'rsv_upside_share': {
    label: 'RSV+ Share',
    summary: 'Realized semi-variance ratio (Barndorff-Nielsen 2010): RSV+ / (RSV+ + RSV−).',
    interpretation: '> 0.55 = volatility driven mostly by gains; < 0.45 = driven mostly by losses.',
    impact: 'Component of the Lab composite multiplier.',
  },
  'egarch_leverage_gamma': {
    label: 'EGARCH γ',
    summary: 'Asymmetry coefficient in the EGARCH(1,1) log-variance equation.  Captures whether negative shocks raise vol more than positive ones.',
    interpretation: 'γ < 0 = classic leverage effect (most equities).  γ ≈ 0 = symmetric.  γ > 0 = anti-leverage (rare).',
    impact: 'Diagnostic; not blended into rank.',
  },
  'garch_m_premium_bps_per_sigma': {
    label: 'GARCH-M Premium',
    summary: 'Expected return premium per unit of σ from a GARCH-in-Mean fit, in basis points per sigma.',
    interpretation: 'Positive = market demands a positive risk premium on this asset.',
    impact: 'Diagnostic; not blended into rank.',
  },
  'permutation_entropy': {
    label: 'Permutation Entropy',
    summary: 'Bandt-Pompe (2002) ordinal-pattern entropy of the return series, normalized to [0, 1].',
    interpretation: '≈ 1.0 = random / unpredictable; lower = more recurring patterns.',
    impact: 'Component of the Lab composite multiplier (low entropy boosts rank).',
  },
  'mahalanobis_outlier_z': {
    label: 'Mahalanobis z',
    summary: 'Multivariate distance of the latest trading day from the centroid of the prior days, in std units.',
    interpretation: '> 2 = regime-anomalous day (95%+ confidence outlier).',
    impact: 'Component of the Lab composite multiplier (anomalous days dampen rank).',
  },
  'approximate_entropy': {
    label: 'ApEn',
    summary: 'Pincus (1991) Approximate Entropy ApEn(2, 0.2σ).  Measures regularity / self-similarity.',
    interpretation: 'Low ApEn = regular, predictable.  High ApEn = irregular.',
    impact: 'Component of the Lab composite multiplier.',
  },
  'lab_qi_certainty': {
    label: 'QI Certainty',
    summary: 'Quantum-inspired interference fusion of fast-tier and GARCH-tier P(up) values.  Constructive interference when tiers agree, destructive when they disagree.',
    interpretation: '0 = total tier disagreement; 1 = perfect agreement at high conviction.',
    impact: 'Currently informational; not blended into the composite multiplier (it operates per-horizon).',
  },
  'lab_rank_multiplier': {
    label: 'Lab Rank Multiplier',
    summary: 'Composite multiplier in [0.6, 1.4] derived from RSV share, permutation entropy, ApEn, Mahalanobis outlier, DFA α, SSA trend slope, and 2-state Vol HMM.',
    interpretation: '> 1 = lab signals favor this trade; < 1 = lab signals counsel caution.',
    impact: 'When "Blend Lab into ranking" is ON, this multiplies the effective Kelly rank.',
  },
  // Phase 26.50 — Lab Mode additions
  'dfa_alpha': {
    label: 'DFA α',
    summary: 'Detrended Fluctuation Analysis exponent (Peng et al. 1994).  Robust Hurst alternative that handles non-stationary trends.',
    interpretation: 'α ≈ 0.5 = random walk; α > 0.5 = persistent / trending; α < 0.5 = anti-persistent / mean-reverting.',
    impact: 'Contributes to the Lab composite multiplier.',
  },
  'ssa_trend_slope_pct_per_day': {
    label: 'SSA Trend Slope',
    summary: 'Slope of the leading Singular Spectrum Analysis component (smoothed trend) over the last 5 days, in %/day.',
    interpretation: 'Positive = trend up; negative = trend down.  Robust to short-term noise.',
    impact: 'Signed contributor to the Lab composite multiplier.',
  },
  'vol_hmm_p_stressed': {
    label: 'Vol HMM P(stressed)',
    summary: '2-state Hidden Markov Model probability that the LATEST day is in the stressed (high-vol) regime.',
    interpretation: '0 = clearly in calm regime; 1 = clearly in stressed regime.',
    impact: 'Stressed + sticky regime dampens the Lab composite multiplier.',
  },
  'vol_hmm_p_stay_stressed': {
    label: 'Vol HMM P(stay stressed)',
    summary: 'Empirical transition probability that the stressed regime persists tomorrow.',
    interpretation: '> 0.7 = sticky stressed regime; < 0.3 = quick mean-reversion to calm.',
    impact: 'Combined with P(stressed) drives the HMM contribution to the Lab multiplier.',
  },
  // -------- Strategy Tier signals ----------
  'strategy_vr5': {
    label: 'Variance Ratio (5-day)',
    summary: 'Lo–MacKinlay (1988) variance ratio at q=5 days.  VR(q) = Var(q-sum) / [q · Var(1)].',
    interpretation: '= 1 random walk; > 1.1 = positive autocorrelation (momentum); < 0.9 = mean reversion.',
    impact: 'Signed contributor to the Strategy composite multiplier.',
  },
  'strategy_vr22': {
    label: 'Variance Ratio (22-day)',
    summary: 'Lo–MacKinlay variance ratio at q=22 days — captures monthly autocorrelation structure.',
    interpretation: 'Same scale as VR(5); equity literature commonly finds VR(22) near 1 with modest momentum/MR depending on regime.',
    impact: 'Signed contributor to the Strategy composite multiplier.',
  },
  'strategy_ar1': {
    label: 'AR(1) Coefficient',
    summary: 'Lag-1 OLS regression coefficient.  Direct tomorrow-prediction slope.',
    interpretation: 'Bounded [-1, 1].  Positive = momentum; negative = mean reversion.  Magnitudes > 0.05 are unusual.',
    impact: 'Signed contributor to the Strategy composite multiplier.',
  },
  'strategy_mi_lag1': {
    label: 'Mutual Info (lag-1)',
    summary: 'Shannon mutual information I(r_t; r_{t-1}) in bits, using 10-bin discretisation.',
    interpretation: '0 = independent; 0.05+ bits = meaningful nonlinear dependence.  Captures structure AR(1) misses.',
    impact: 'Predictability contributor to the Strategy composite multiplier.',
  },
  'strategy_spectral_beta': {
    label: 'Spectral Slope β',
    summary: 'Power-spectrum slope from the FFT periodogram.  Power ∝ 1/fᵝ.',
    interpretation: 'β ≈ 0 white noise; ≈ 1 pink noise (typical); β > 1.5 = strong long-memory structure.',
    impact: 'Predictability contributor to the Strategy composite multiplier.',
  },
  'strategy_welch_cycle_days': {
    label: 'Welch Cycle (days)',
    summary: 'Period of the LARGEST Welch-periodogram peak that exceeds the noise floor by 2σ.',
    interpretation: '0 = no dominant cycle detected; otherwise the dominant oscillation length in days.',
    impact: 'Informational — useful for timing entries when a clear cycle exists.',
  },
  'strategy_rqa_determinism': {
    label: 'RQA Determinism %',
    summary: 'Recurrence-plot fraction of points on diagonal lines of length ≥ 2.  Computed on a 64-day window.',
    interpretation: '> 0.5 = deterministic / predictable trajectory; < 0.2 = chaotic.',
    impact: 'Predictability contributor to the Strategy composite multiplier.',
  },
  'strategy_lz_complexity': {
    label: 'LZ Complexity',
    summary: 'Lempel-Ziv 1976 complexity of the sign-discretised return sequence, normalised to [0, 1].',
    interpretation: '≈ 1 = maximally random (unpredictable); ≈ 0 = highly structured patterns.',
    impact: 'Inverse predictability contributor — high LZ dampens the Strategy multiplier.',
  },
  'strategy_emd_slope_pct': {
    label: 'EMD IMF1 Slope',
    summary: 'Slope of the first intrinsic-mode oscillation (high-freq component after stripping the rolling-mean trend), in %/day over the last 5 days.',
    interpretation: 'Signed.  Positive = short-term momentum up; negative = short-term momentum down.',
    impact: 'Signed contributor to the Strategy composite multiplier.',
  },
  'strategy_vol_regime_mom': {
    label: 'Vol-Regime Momentum',
    summary: 'log(σ_recent_5d / σ_long_30d).  Positive = volatility is rising relative to long-term; negative = cooling.',
    interpretation: '> 0.3 = stress entering; < -0.3 = stress exiting; near 0 = stable regime.',
    impact: 'Cooling-vol regime contributes positively to the Strategy multiplier.',
  },
  'strategy_rank_multiplier': {
    label: 'Strategy Rank Multiplier',
    summary: 'Composite multiplier in [0.6, 1.4] derived from VR(5), VR(22), AR(1), MI, spectral β, RQA determinism, LZ complexity, EMD slope, and vol-regime momentum.',
    interpretation: '> 1 = strategy signals favor this trade; < 1 = strategy signals counsel caution.',
    impact: 'When "Blend Strategy into ranking" is ON, this multiplies the effective Kelly rank.',
  },
  // ====================================================================
  // Phase 26.60 — Predictive Expansion Pack (10 standard + 4 reality_breaker)
  // ====================================================================
  'msm_drift_premium': {
    label: 'MSM Drift Premium',
    summary: 'Expected drift of the current hidden Markov regime relative to the long-run mean of returns (2-state Gaussian mixture).',
    interpretation: '> 0 = favourable latent regime; < 0 = low-mean / crash-prone regime.',
    impact: 'Additive overlay on drift_pct (medium horizons); feeds regime_risk_multiplier.',
  },
  'ts_nonlinear_dependence': {
    label: 'Nonlinear Dependence',
    summary: 'Threshold-AR R² lift over a linear AR(2) baseline. Range [0, 1].',
    interpretation: '≈ 0 = no exploitable nonlinear structure; ≈ 1 = strong state-dependent memory.',
    impact: 'Feeds strategy_v2_rank_multiplier; amplifies conviction in memory-based signals.',
  },
  'trend_curvature_pct': {
    label: 'Trend Curvature',
    summary: 'Local quadratic coefficient of smoothed log-price (% per step²).',
    interpretation: '+ curvature with + slope = breakout continuation; − curvature with + slope = trend exhaustion.',
    impact: 'Signed contributor to strategy_v2_rank_multiplier; may reduce Kelly sizing in late-trend conditions.',
  },
  'lead_lag_influence': {
    label: 'Lead-Lag Influence',
    summary: 'Predictive lift over an AR-only baseline when sector / index driver lags are included. Range [0, 1].',
    interpretation: 'High = strong exogenous driver influence; low = mostly idiosyncratic.',
    impact: 'Feeds strategy_v2_rank_multiplier.',
  },
  'volofvol_regime_score': {
    label: 'Vol-of-Vol Regime',
    summary: 'Probability the volatility process itself is in a high-vol-of-vol regime. Range [0, 1].',
    interpretation: 'High = unstable volatility, elevated forecast fragility.',
    impact: 'Risk input to regime_risk_multiplier; shrinks Kelly more than sigma_pct alone.',
  },
  'multiscale_consistency': {
    label: 'Multi-Scale Consistency',
    summary: 'Weighted sign agreement across 1h/5h/1d/5d/20d drifts (heavier weights on longer horizons). Range [-1, 1].',
    interpretation: 'High positive = multi-scale confluence; negative = fragmented structure.',
    impact: 'Core input to strategy_v2_rank_multiplier; high values visibly increase conviction.',
  },
  'entropy_regime_stability': {
    label: 'Regime Stability',
    summary: '1 - normalised entropy of recent regime labels (bull/bear/flat windows). Range [0, 1].',
    interpretation: 'High = persistent regime; low = rapidly flipping structure.',
    impact: 'Feeds regime_risk_multiplier; low stability dampens both Strategy and Regime blends.',
  },
  'drawdown_memory_score': {
    label: 'Drawdown Memory',
    summary: 'Mean forward 5-day return after deep drawdowns minus mean during calm periods, in percent.',
    interpretation: '+ = oversold bounce behaviour; − = crash continuation behaviour.',
    impact: 'Signed contributor to strategy_v2_rank_multiplier.',
  },
  'liq_adjusted_signal': {
    label: 'Liq-Adj. Predictability',
    summary: 'Predictability score × normalised liquidity score. Range [0, 1].',
    interpretation: 'High = both predictable AND tradable; low = signal that may not survive execution friction.',
    impact: 'Drives liq_kelly_factor (multiplicative Kelly sizing scale).',
  },
  'ml_residual_edge': {
    label: 'ML Residual Edge',
    summary: 'Z-scored difference between a saturating nonlinear blend and an in-sample ridge baseline.',
    interpretation: '+ = ML sees additional structure beyond linear; − = ML disagreement or hidden fragility.',
    impact: 'Drives ml_rank_multiplier (controlled [0.8, 1.2]) when ML blend is enabled.',
  },
  // Composite multipliers (Phase 26.60)
  'strategy_v2_rank_multiplier': {
    label: 'Strategy V2 ×',
    summary: 'Composite of nonlinear dependence, curvature, lead-lag, multi-scale consistency, drawdown memory. Clamp [0.6, 1.4].',
    interpretation: '> 1 = predictive-expansion structure favours this trade; < 1 = structure counsels caution.',
    impact: 'When "Blend Strategy V2 into ranking" is ON, multiplies the effective Kelly rank.',
  },
  'regime_risk_multiplier': {
    label: 'Regime Risk ×',
    summary: 'Composite of MSM drift premium, vol-of-vol regime, entropy stability. Clamp [0.6, 1.4].',
    interpretation: '> 1 = stable / favourable regime; < 1 = unstable / crash-prone regime.',
    impact: 'When "Blend Regime Risk into ranking" is ON, multiplies the effective Kelly rank.',
  },
  'ml_rank_multiplier': {
    label: 'ML Overlay ×',
    summary: 'Bounded mapping of ml_residual_edge via tanh. Clamp [0.8, 1.2] (conservative; ML provides nudge, not override).',
    interpretation: '> 1 = ML model sees extra edge; < 1 = ML model dissents.',
    impact: 'When "Blend ML into ranking" is ON, multiplies the effective Kelly rank.',
  },
  'liq_kelly_factor': {
    label: 'Liquidity Kelly ×',
    summary: '0.7 + 0.6·liq_adjusted_signal. Range [0.7, 1.3].',
    interpretation: '> 1 = liquid and predictable; < 1 = fragile / illiquid edges sized down.',
    impact: 'When "Apply Liquidity Kelly Factor" is ON, multiplies the effective Kelly rank.',
  },
  // Reality breakers (experimental+)
  'local_causal_cone_signal': {
    label: 'Local Causal Cone (LCC)',
    summary: 'Cone-weighted directional driver-field signal, σ-scaled. EXPERIMENTAL.',
    interpretation: 'Large + = coherent upstream driver pressure; large − = adverse field pressure.',
    impact: 'Direction confirmation / veto layer on top of direction_cf when Advanced Experimental Mode is ON.',
  },
  'quantum_path_interference_index': {
    label: 'Quantum Path Interference (QPII)',
    summary: 'Complex-amplitude Monte-Carlo path interference vs classical expectation. EXPERIMENTAL.',
    interpretation: '+ = constructive path interference beyond classical; − = destructive cancellation.',
    impact: 'Horizon-local drift / conviction adjustment. Clamped to ±2.',
  },
  'local_lyapunov_volatility_exponent': {
    label: 'Local Lyapunov Exponent (LLVE)',
    summary: 'Local nonlinear trajectory divergence rate on delay-embedded returns. EXPERIMENTAL.',
    interpretation: 'High + = sensitive / unstable dynamics; ≈ 0 = stable local geometry.',
    impact: 'Risk-damping overlay; high values aggressively shrink Kelly.',
  },
  'temporal_renormalization_score': {
    label: 'Temporal Renormalisation (TRS)',
    summary: '−|slope| of drift/σ vs log-horizon. EXPERIMENTAL.',
    interpretation: 'Near zero = stable cross-scale flow; large − = scale fragility.',
    impact: 'Modulates trust in multi-scale consistency claims.',
  },
  'reality_breaker_multiplier': {
    label: 'Reality-Breaker ×',
    summary: 'EXPERIMENTAL composite: 0.30·z(LCC) + 0.25·z(QPII) − 0.25·z(LLVE) + 0.20·z(TRS) → tanh-mapped to [0.5, 1.5].',
    interpretation: '> 1 = reality-breaker layer endorses trade direction; < 1 = layer counsels caution.',
    impact: 'When Advanced Experimental Mode AND "Blend Reality-Breaker into ranking" are both ON, multiplies effective Kelly rank. Never flips direction by itself.',
  },
};

// =========================================================================
// Phase 26.50 — Live reading engine for the click-to-pin info popover.
//
// Each metric is given a small rule that converts the *current numeric
// value* into one of:
//   * tone:      'bull' | 'bear' | 'neutral' | 'caution' | 'info'
//   * label:     'Bullish' | 'Bearish' | 'Neutral' | 'Stressed' | etc.
//   * intensity: 'extreme' | 'strong' | 'moderate' | 'weak' | null
//
// This drives the colored "live reading" pill at the top of the popover
// so the user immediately sees whether the metric is calling
// bullish/bearish at its current value, without having to mentally
// reconcile thresholds.
//
// `category` controls the rendering style:
//   * 'directional' — colored bull/bear/neutral pill + intensity
//   * 'intensity'   — single-axis pill (no direction, just magnitude)
//   * 'categorical' — value is already a string label (regime, etc.)
// =========================================================================
function _intensityBand(absVal, scale) {
  // `scale` = [moderate, strong, extreme] cutoffs (in absVal units).
  const a = Math.abs(Number(absVal) || 0);
  if (a >= scale[2]) return 'extreme';
  if (a >= scale[1]) return 'strong';
  if (a >= scale[0]) return 'moderate';
  return 'weak';
}

function _directionalThreshold(v, bullAt, bearAt, intensityScale) {
  const num = Number(v);
  if (!Number.isFinite(num)) return { tone: 'info', label: 'No data', intensity: null };
  let tone, label;
  if (num >= bullAt) { tone = 'bull'; label = 'Bullish'; }
  else if (num <= bearAt) { tone = 'bear'; label = 'Bearish'; }
  else { tone = 'neutral'; label = 'Neutral'; }
  // intensity is measured from the neutral midpoint
  const mid = (bullAt + bearAt) / 2;
  const intensity = intensityScale ? _intensityBand(num - mid, intensityScale) : null;
  return { tone, label, intensity };
}

function _signedDrift(v, intensityScale) {
  const num = Number(v);
  if (!Number.isFinite(num) || num === 0) return { tone: 'neutral', label: 'Flat', intensity: 'weak' };
  const tone = num > 0 ? 'bull' : 'bear';
  const label = num > 0 ? 'Bullish drift' : 'Bearish drift';
  const intensity = intensityScale ? _intensityBand(num, intensityScale) : null;
  return { tone, label, intensity };
}

function _magnitudeOnly(v, intensityScale, lowLabel, highLabel, polarity) {
  // `polarity` controls the tone of the resulting chip:
  //   'positive'  — high = GOOD (conviction, tier-alignment, signal-memory).
  //                 Strong/extreme reads use tone='bull' (greenish);
  //                 weak/moderate reads use tone='info' (blue, informational).
  //   'negative'  — high = BAD (volatility, tail-risk, fat tails, jumps,
  //                 stress, anomalies).  Strong/extreme → tone='caution'
  //                 (amber); weak/moderate → tone='info'.
  //   'neutral'   — magnitude is just *informational* (cycle length, complexity,
  //                 half-life, randomness).  All reads → tone='info'.
  // Default is 'negative' for backwards compat with the original caller.
  polarity = polarity || 'negative';
  const num = Number(v);
  if (!Number.isFinite(num)) return { tone: 'info', label: 'No data', intensity: null };
  const intensity = _intensityBand(num, intensityScale);
  const label = (intensity === 'extreme' || intensity === 'strong') ? (highLabel || 'Elevated')
              : (intensity === 'moderate') ? 'Moderate'
              : (lowLabel || 'Low');
  let tone;
  if (polarity === 'positive') {
    tone = (intensity === 'extreme' || intensity === 'strong') ? 'bull' : 'info';
  } else if (polarity === 'neutral') {
    tone = 'info';
  } else {
    tone = (intensity === 'extreme' || intensity === 'strong') ? 'caution' : 'info';
  }
  return { tone, label, intensity };
}

// Helper for *regime* metrics (Hurst, DFA, variance ratios, spectral β,
// regime_label) where the value picks a regime label but does NOT
// imply a direction.  Trending in a regime metric could be bullish or
// bearish trending — only the actual direction call resolves that.
// We use tone='info' so the chip is clearly informational and doesn't
// fight the directional pill elsewhere in the popover.
function _regimeReading(label, intensity) {
  return { tone: 'info', label: label || 'Regime', intensity: intensity || null };
}

// Helper for *inverted* drift — metrics where the *positive* sign is a
// caution signal (e.g. vol_regime_momentum > 0 = vol is rising).
function _invertedDrift(v, intensityScale) {
  const num = Number(v);
  if (!Number.isFinite(num) || num === 0) {
    return { tone: 'neutral', label: 'Flat', intensity: 'weak' };
  }
  const tone = num > 0 ? 'caution' : 'info';
  const label = num > 0 ? 'Vol regime rising' : 'Vol regime cooling';
  const intensity = intensityScale ? _intensityBand(num, intensityScale) : null;
  return { tone, label, intensity };
}

// Lookup the raw current-value of a metric on the live detail payload.
// Returns null if we can't find a number (e.g., metric isn't surfaced
// yet for this symbol).
function _ffRawMetricValue(metricId, horizonKey) {
  const detail = state.detailPayload;
  if (!detail) return null;
  const block = (detail.forward_metrics_garch && detail.forward_metrics_garch[horizonKey])
              || (detail.forward_metrics && detail.forward_metrics[horizonKey])
              || {};
  const adv = detail.advanced_signals || {};
  const lab = detail.lab_signals || {};
  const strategy = detail.strategy_signals || {};
  // Map metricId -> raw source.
  // Per-horizon block fields:
  if (metricId === 'direction_cf')              return block.direction_cf;
  if (metricId === 'p_up_cf')                   return block.p_up_cf;
  if (metricId === 'p_up_gauss')                return block.p_up_gauss;
  if (metricId === 'drift_pct')                 return block.drift_pct;
  if (metricId === 'jump_drift_pct')            return block.jump_drift_pct;
  if (metricId === 'sigma_pct')                 return block.sigma_pct;
  if (metricId === 'var95_pct')                 return block.var95_pct;
  if (metricId === 'cvar95_pct')                return block.cvar95_pct;
  if (metricId === 'directional_certainty_cf')  return block.directional_certainty_cf;
  if (metricId === 'kelly_fraction')            return block.kelly_fraction;
  if (metricId === 'effective_kelly_rank')      return block.effective_kelly_rank;
  if (metricId === 'regime_label')              return block.regime_label || adv.regime_label;
  if (metricId === 'garch_ann_vol')             return block.garch_annualised_vol_pct;
  // Per-symbol advanced signals:
  if (metricId === 'hurst_exponent')            return adv.hurst_exponent;
  if (metricId === 'realized_skew')             return adv.realized_skew;
  if (metricId === 'realized_excess_kurt')      return adv.realized_excess_kurt;
  if (metricId === 'jump_intensity_per_day')    return adv.jump_intensity_per_day;
  if (metricId === 'jump_mean_return_pct')      return adv.jump_mean_return_pct;
  if (metricId === 'ou_half_life_days')         return adv.ou_half_life_days;
  if (metricId === 'rv_har_sigma_pct')          return adv.rv_har_sigma_pct;
  // Lab signals:
  if (metricId === 'rsv_upside_share')          return lab.rsv_upside_share;
  if (metricId === 'egarch_leverage_gamma')     return lab.egarch_leverage_gamma;
  if (metricId === 'garch_m_premium_bps_per_sigma') return lab.garch_m_premium_bps_per_sigma;
  if (metricId === 'permutation_entropy')       return lab.permutation_entropy;
  if (metricId === 'approximate_entropy')       return lab.approximate_entropy;
  if (metricId === 'mahalanobis_outlier_z')     return lab.mahalanobis_outlier_z;
  if (metricId === 'dfa_alpha')                 return lab.dfa_alpha;
  if (metricId === 'ssa_trend_slope_pct_per_day') return lab.ssa_trend_slope_pct_per_day;
  if (metricId === 'vol_hmm_p_stressed')        return lab.vol_hmm_p_stressed;
  if (metricId === 'vol_hmm_p_stay_stressed')   return lab.vol_hmm_p_stay_stressed;
  if (metricId === 'lab_qi_certainty')          return block.lab_qi_certainty;
  if (metricId === 'lab_rank_multiplier')       return block.lab_rank_multiplier;
  // Strategy signals:
  if (metricId === 'strategy_vr5')              return strategy.variance_ratio_5d;
  if (metricId === 'strategy_vr22')             return strategy.variance_ratio_22d;
  if (metricId === 'strategy_ar1')              return strategy.ar1_coefficient;
  if (metricId === 'strategy_mi_lag1')          return strategy.mutual_information_lag1;
  if (metricId === 'strategy_spectral_beta')    return strategy.spectral_slope_beta;
  if (metricId === 'strategy_welch_cycle_days') return strategy.welch_dominant_cycle_days;
  if (metricId === 'strategy_rqa_determinism')  return strategy.rqa_determinism_pct;
  if (metricId === 'strategy_lz_complexity')    return strategy.lempel_ziv_complexity;
  if (metricId === 'strategy_emd_slope_pct')    return strategy.emd_imf1_slope_pct_per_day;
  if (metricId === 'strategy_vol_regime_mom')   return strategy.vol_regime_momentum;
  if (metricId === 'strategy_rank_multiplier')  return block.strategy_rank_multiplier;
  // ---- Phase 26.60 Predictive Expansion fields ----
  const predictive = detail.predictive_expansion_signals || {};
  if (metricId === 'msm_drift_premium')              return predictive.msm_drift_premium;
  if (metricId === 'ts_nonlinear_dependence')        return predictive.ts_nonlinear_dependence;
  if (metricId === 'trend_curvature_pct')            return predictive.trend_curvature_pct;
  if (metricId === 'lead_lag_influence')             return predictive.lead_lag_influence;
  if (metricId === 'volofvol_regime_score')          return predictive.volofvol_regime_score;
  if (metricId === 'multiscale_consistency')         return predictive.multiscale_consistency;
  if (metricId === 'entropy_regime_stability')       return predictive.entropy_regime_stability;
  if (metricId === 'drawdown_memory_score')          return predictive.drawdown_memory_score;
  if (metricId === 'liq_adjusted_signal')            return predictive.liq_adjusted_signal;
  if (metricId === 'ml_residual_edge')               return predictive.ml_residual_edge;
  if (metricId === 'local_causal_cone_signal')       return predictive.local_causal_cone_signal;
  if (metricId === 'quantum_path_interference_index') return predictive.quantum_path_interference_index;
  if (metricId === 'local_lyapunov_volatility_exponent') return predictive.local_lyapunov_volatility_exponent;
  if (metricId === 'temporal_renormalization_score') return predictive.temporal_renormalization_score;
  if (metricId === 'strategy_v2_rank_multiplier')    return block.strategy_v2_rank_multiplier;
  if (metricId === 'regime_risk_multiplier')         return block.regime_risk_multiplier;
  if (metricId === 'ml_rank_multiplier')             return block.ml_rank_multiplier;
  if (metricId === 'liq_kelly_factor')               return block.liq_kelly_factor;
  if (metricId === 'reality_breaker_multiplier')     return block.reality_breaker_multiplier;
  return null;
}

// Compute the live reading {tone, label, intensity} for a metric.
function _ffLiveReading(metricId, explicitValue) {
  const horizonKey = _FUTURE_HORIZON_KEY[state.futureHorizon || '1h_hold'] || 'forward_1h';
  // Phase 26.62 — callers can pass the metric's value for the exact
  // block being rendered (e.g. the fast tier vs GARCH tier), so a
  // cell's COLOR always matches the value shown in THAT cell rather
  // than the state-horizon/GARCH-preferred lookup used by the popover.
  const v = (explicitValue !== undefined) ? explicitValue : _ffRawMetricValue(metricId, horizonKey);
  if (v === undefined || v === null || (typeof v === 'number' && !Number.isFinite(v))) {
    return { tone: 'info', label: 'No data', intensity: null };
  }
  // ---- Per-horizon directional metrics ----
  if (metricId === 'direction_cf') {
    const s = String(v);
    return { tone: s === 'Bullish' ? 'bull' : s === 'Bearish' ? 'bear' : 'neutral', label: s || 'Neutral', intensity: null };
  }
  if (metricId === 'p_up_cf' || metricId === 'p_up_gauss') {
    return _directionalThreshold(v, 0.55, 0.45, [0.05, 0.12, 0.20]);
  }
  if (metricId === 'drift_pct')                return _signedDrift(v, [0.05, 0.20, 0.60]);
  if (metricId === 'jump_drift_pct')           return _signedDrift(v, [0.02, 0.10, 0.30]);
  if (metricId === 'sigma_pct')                return _magnitudeOnly(v, [0.5, 1.5, 3.0], 'Calm', 'Elevated σ', 'negative');
  if (metricId === 'var95_pct' || metricId === 'cvar95_pct')
                                                return _magnitudeOnly(v, [0.8, 2.0, 5.0], 'Low risk', 'Tail risk', 'negative');
  // Conviction metric — HIGH is GOOD (reinforces whatever direction is called).
  if (metricId === 'directional_certainty_cf') return _magnitudeOnly(v, [0.10, 0.30, 0.60], 'Low conviction', 'High conviction', 'positive');
  if (metricId === 'kelly_fraction')           return _signedDrift(v, [0.02, 0.10, 0.25]);
  if (metricId === 'effective_kelly_rank')     return _signedDrift(v, [0.0005, 0.005, 0.05]);
  // Regime metrics — tone='info' because "trending"/"mean-reverting" do
  // not pick a direction on their own.
  if (metricId === 'regime_label') {
    return _regimeReading(String(v), null);
  }
  if (metricId === 'garch_ann_vol')            return _magnitudeOnly(v, [20, 40, 80], 'Calm vol', 'High vol', 'negative');
  // ---- Per-symbol advanced ----
  if (metricId === 'hurst_exponent') {
    if (v >= 0.55) return _regimeReading('Trending', _intensityBand(v - 0.5, [0.05, 0.10, 0.20]));
    if (v <= 0.45) return _regimeReading('Mean-reverting', _intensityBand(0.5 - v, [0.05, 0.10, 0.20]));
    return _regimeReading('Random walk', 'weak');
  }
  if (metricId === 'realized_skew')            return _signedDrift(v, [0.3, 0.8, 1.5]);
  if (metricId === 'realized_excess_kurt')     return _magnitudeOnly(v, [1, 3, 7], 'Normal tails', 'Fat tails', 'negative');
  if (metricId === 'jump_intensity_per_day')   return _magnitudeOnly(v, [0.02, 0.05, 0.10], 'Quiet', 'Jumpy', 'negative');
  if (metricId === 'jump_mean_return_pct')     return _signedDrift(v, [0.5, 1.5, 3.0]);
  // OU half-life — very short half-life = fast mean-reversion (good
  // for fade strategies, caution for momentum); very long = slow drift
  // (info for both styles).  Show as info with intensity bands.
  if (metricId === 'ou_half_life_days') {
    const num = Number(v);
    if (!Number.isFinite(num)) return { tone: 'info', label: 'No data', intensity: null };
    if (num <= 3)   return { tone: 'caution', label: 'Very fast revert', intensity: 'strong' };
    if (num <= 10)  return { tone: 'info',    label: 'Fast revert',      intensity: 'moderate' };
    if (num >= 40)  return { tone: 'info',    label: 'Slow drift',       intensity: 'moderate' };
    return            { tone: 'info',    label: 'Moderate revert',  intensity: 'weak' };
  }
  if (metricId === 'rv_har_sigma_pct')         return _magnitudeOnly(v, [1, 2.5, 5], 'Low realized', 'High realized', 'negative');
  // ---- Lab signals ----
  if (metricId === 'rsv_upside_share')         return _directionalThreshold(v, 0.55, 0.45, [0.05, 0.12, 0.20]);
  if (metricId === 'egarch_leverage_gamma') {
    // negative gamma = classic leverage effect (bearish asymmetry)
    if (v <= -0.05) return { tone: 'bear', label: 'Leverage effect', intensity: _intensityBand(v, [0.05, 0.10, 0.20]) };
    if (v >=  0.05) return { tone: 'bull', label: 'Reverse leverage', intensity: _intensityBand(v, [0.05, 0.10, 0.20]) };
    return { tone: 'neutral', label: 'Symmetric vol', intensity: 'weak' };
  }
  if (metricId === 'garch_m_premium_bps_per_sigma') return _signedDrift(v, [2, 8, 20]);
  // Permutation entropy — HIGH = random/unpredictable = caution for
  // any strategy that relies on predictability.  Low = structured = info.
  if (metricId === 'permutation_entropy')      return _magnitudeOnly(v, [0.85, 0.92, 0.98], 'Structured', 'Random', 'negative');
  // Approximate entropy high = erratic price behavior, which IS a caution
  // signal for systems that rely on predictability.
  if (metricId === 'approximate_entropy')      return _magnitudeOnly(v, [0.3, 0.6, 1.0], 'Predictable', 'Erratic', 'negative');
  if (metricId === 'mahalanobis_outlier_z')    return _magnitudeOnly(v, [1, 2, 3], 'Typical', 'Anomalous', 'negative');
  // DFA α describes a regime, not a direction.
  if (metricId === 'dfa_alpha') {
    if (v >= 0.6) return _regimeReading('Persistent trend', _intensityBand(v - 0.5, [0.05, 0.10, 0.20]));
    if (v <= 0.4) return _regimeReading('Anti-persistent', _intensityBand(0.5 - v, [0.05, 0.10, 0.20]));
    return _regimeReading('Random scaling', 'weak');
  }
  if (metricId === 'ssa_trend_slope_pct_per_day') return _signedDrift(v, [0.05, 0.15, 0.40]);
  // Vol HMM: stressed = caution, calm = info (calm vol ≠ bullish on its own).
  if (metricId === 'vol_hmm_p_stressed') {
    if (v >= 0.7) return { tone: 'caution', label: 'Stressed regime', intensity: _intensityBand(v - 0.5, [0.1, 0.2, 0.4]) };
    if (v <= 0.3) return { tone: 'info',    label: 'Calm regime',     intensity: _intensityBand(0.5 - v, [0.1, 0.2, 0.4]) };
    return { tone: 'neutral', label: 'Mixed regime', intensity: 'weak' };
  }
  if (metricId === 'vol_hmm_p_stay_stressed')  return _magnitudeOnly(v, [0.4, 0.6, 0.8], 'Brief stress', 'Sticky stress', 'negative');
  // Tier alignment — HIGH is GOOD.
  if (metricId === 'lab_qi_certainty')         return _magnitudeOnly(v, [0.10, 0.30, 0.60], 'Disagreement', 'Tier alignment', 'positive');
  if (metricId === 'lab_rank_multiplier') {
    if (v > 1.03) return { tone: 'bull', label: 'Lab boosts rank', intensity: _intensityBand(v - 1, [0.03, 0.10, 0.25]) };
    if (v < 0.97) return { tone: 'bear', label: 'Lab dampens rank', intensity: _intensityBand(1 - v, [0.03, 0.10, 0.25]) };
    return { tone: 'neutral', label: 'Neutral lab impact', intensity: 'weak' };
  }
  // ---- Strategy signals ----
  // Variance ratios describe a regime (trending / mean-reverting), not a
  // direction.  Use tone='info' so the chip doesn't fight the directional
  // pill elsewhere in the popover.
  if (metricId === 'strategy_vr5' || metricId === 'strategy_vr22') {
    if (v > 1.05) return _regimeReading('Trending', _intensityBand(v - 1, [0.05, 0.15, 0.40]));
    if (v < 0.95) return _regimeReading('Mean-reverting', _intensityBand(1 - v, [0.05, 0.15, 0.40]));
    return _regimeReading('Random-walk', 'weak');
  }
  if (metricId === 'strategy_ar1')             return _signedDrift(v, [0.03, 0.10, 0.25]);
  // Mutual information lag-1 — HIGH = strong predictive memory = GOOD.
  if (metricId === 'strategy_mi_lag1')         return _magnitudeOnly(v, [0.02, 0.08, 0.20], 'Memoryless', 'Strong memory', 'positive');
  // Spectral β describes the noise color of the price series, not a
  // direction.  1/f trending vs white-noise is regime information.
  if (metricId === 'strategy_spectral_beta') {
    if (v >= 1.2)  return _regimeReading('1/f trend',   _intensityBand(v - 1, [0.2, 0.5, 1.0]));
    if (v <= 0.5)  return _regimeReading('White noise', _intensityBand(1 - v, [0.2, 0.5, 1.0]));
    return _regimeReading('Pink noise', 'weak');
  }
  // Welch dominant cycle — long cycles (slow regime) are informational,
  // short cycles (rapid oscillation) hint at higher-frequency noise
  // and lower predictability for swing strategies → soft caution at
  // the short end, info elsewhere.  Use the inverted polarity:
  // higher cycle = info (calmer); lower = caution-ish (noisier).
  if (metricId === 'strategy_welch_cycle_days') {
    const num = Number(v);
    if (!Number.isFinite(num)) return { tone: 'info', label: 'No data', intensity: null };
    if (num <= 2)   return { tone: 'caution', label: 'Very short cycle', intensity: 'strong' };
    if (num <= 8)   return { tone: 'info',    label: 'Short cycle',      intensity: 'moderate' };
    if (num <= 22)  return { tone: 'info',    label: 'Mid cycle',        intensity: 'weak' };
    return            { tone: 'bull',    label: 'Long cycle',        intensity: 'strong' };
  }
  // Determinism — HIGH = predictable structure = GOOD.
  if (metricId === 'strategy_rqa_determinism')  return _magnitudeOnly(v, [0.10, 0.30, 0.60], 'Stochastic', 'Deterministic', 'positive');
  // Lempel-Ziv complexity — HIGH = chaotic, hard to predict (caution
  // for any deterministic strategy); LOW = compressible / trending
  // (bullish for trend-followers).  Was previously 'neutral' which
  // gave grey chips regardless of value; users couldn't tell whether
  // a "Complex" reading was good or bad at a glance.
  if (metricId === 'strategy_lz_complexity') {
    const num = Number(v);
    if (!Number.isFinite(num)) return { tone: 'info', label: 'No data', intensity: null };
    const intensity = _intensityBand(num, [0.20, 0.50, 0.80]);
    if (num >= 0.70) return { tone: 'caution', label: 'Complex',     intensity };
    if (num <= 0.30) return { tone: 'bull',    label: 'Compressible', intensity };
    return            { tone: 'info',    label: 'Mixed structure', intensity };
  }
  if (metricId === 'strategy_emd_slope_pct')    return _signedDrift(v, [0.05, 0.20, 0.50]);
  // Vol regime momentum — POSITIVE is a *caution* signal (vol is rising),
  // not a bullish drift.  Use the inverted-drift helper.
  if (metricId === 'strategy_vol_regime_mom')   return _invertedDrift(v, [0.15, 0.40, 0.80]);
  if (metricId === 'strategy_rank_multiplier') {
    if (v > 1.03) return { tone: 'bull', label: 'Strategy boosts rank', intensity: _intensityBand(v - 1, [0.03, 0.10, 0.25]) };
    if (v < 0.97) return { tone: 'bear', label: 'Strategy dampens rank', intensity: _intensityBand(1 - v, [0.03, 0.10, 0.25]) };
    return { tone: 'neutral', label: 'Neutral strategy impact', intensity: 'weak' };
  }
  // ====================================================================
  // Phase 26.60 — Predictive Expansion tone/intensity mappings.
  //
  // Mapping rationale (per registry.higherIsBetterSign):
  //   +1  → higher-is-better        → _magnitudeOnly(..., 'positive')
  //         (or _signedDrift for signed scales like trend curvature)
  //   -1  → higher-is-worse         → _magnitudeOnly(..., 'negative')
  //    0  → regime / informational  → _regimeReading
  // Multipliers all follow the same Lab/Strategy pattern:
  //   >1.03 → bull (rank-boosting), <0.97 → bear (rank-dampening).
  // ====================================================================
  // ---- Standard 10 ----
  if (metricId === 'msm_drift_premium')        return _signedDrift(v, [0.10, 0.50, 1.50]);
  if (metricId === 'ts_nonlinear_dependence')  return _magnitudeOnly(v, [0.15, 0.35, 0.65], 'No structure', 'Strong dep.', 'positive');
  if (metricId === 'trend_curvature_pct')      return _signedDrift(v, [0.05, 0.20, 0.80]);
  if (metricId === 'lead_lag_influence')       return _magnitudeOnly(v, [0.10, 0.30, 0.60], 'Idiosyncratic', 'Driver-led', 'positive');
  // Vol-of-vol regime → high = unstable vol → caution.
  if (metricId === 'volofvol_regime_score')    return _magnitudeOnly(v, [0.35, 0.55, 0.75], 'Stable vol', 'Unstable vol', 'negative');
  if (metricId === 'multiscale_consistency')   return _signedDrift(v, [0.20, 0.45, 0.75]);
  if (metricId === 'entropy_regime_stability') return _magnitudeOnly(v, [0.20, 0.45, 0.70], 'Flippy regime', 'Persistent regime', 'positive');
  if (metricId === 'drawdown_memory_score')    return _signedDrift(v, [0.30, 1.00, 3.00]);
  if (metricId === 'liq_adjusted_signal')      return _magnitudeOnly(v, [0.15, 0.35, 0.60], 'Fragile', 'Liq+predictable', 'positive');
  if (metricId === 'ml_residual_edge')         return _signedDrift(v, [0.20, 0.50, 1.20]);
  // ---- Reality-breaker overlays (always 'caution'-flavoured visual) ----
  // LCC sign is directional; we use _signedDrift but the user must
  // understand context — the `experimental+` superscript carries that.
  if (metricId === 'local_causal_cone_signal')          return _signedDrift(v, [0.30, 0.80, 1.80]);
  if (metricId === 'quantum_path_interference_index')   return _signedDrift(v, [0.20, 0.60, 1.20]);
  // LLVE: high = unstable dynamics = caution.
  if (metricId === 'local_lyapunov_volatility_exponent') {
    const num = Number(v);
    if (!Number.isFinite(num)) return { tone: 'info', label: 'No data', intensity: null };
    if (num <= -0.10) return { tone: 'bull',    label: 'Stable dynamics',  intensity: _intensityBand(Math.abs(num), [0.05, 0.15, 0.40]) };
    if (num >=  0.10) return { tone: 'caution', label: 'Sensitive dynamics', intensity: _intensityBand(Math.abs(num), [0.05, 0.15, 0.40]) };
    return { tone: 'info', label: 'Near-neutral λ', intensity: 'weak' };
  }
  // TRS near zero is best (stable cross-scale flow).  Use absolute
  // magnitude as the caution intensity — opposite sign convention
  // from the standard 'negative' polarity helper.
  if (metricId === 'temporal_renormalization_score') {
    const num = Number(v);
    if (!Number.isFinite(num)) return { tone: 'info', label: 'No data', intensity: null };
    const mag = Math.abs(num);
    const intensity = _intensityBand(mag, [0.05, 0.15, 0.40]);
    if (mag <= 0.05) return { tone: 'bull',    label: 'Stable scaling',     intensity };
    return { tone: 'caution', label: 'Scale fragility', intensity };
  }
  // ---- 5 composite multipliers — bull/bear around 1.0 ----
  const _multReading = (val, lo, hi, bullLabel, bearLabel, neutralLabel) => {
    if (val > 1.03) return { tone: 'bull', label: bullLabel, intensity: _intensityBand(val - 1, [0.03, 0.10, 0.25]) };
    if (val < 0.97) return { tone: 'bear', label: bearLabel, intensity: _intensityBand(1 - val, [0.03, 0.10, 0.25]) };
    return { tone: 'neutral', label: neutralLabel || 'Neutral impact', intensity: 'weak' };
  };
  if (metricId === 'strategy_v2_rank_multiplier')
    return _multReading(v, 0.6, 1.4, 'Strategy V2 boosts',  'Strategy V2 dampens',  'Neutral V2 impact');
  if (metricId === 'regime_risk_multiplier')
    return _multReading(v, 0.6, 1.4, 'Regime supports',     'Regime suppresses',    'Neutral regime');
  if (metricId === 'ml_rank_multiplier')
    return _multReading(v, 0.8, 1.2, 'ML endorses',         'ML dissents',          'Neutral ML');
  if (metricId === 'liq_kelly_factor')
    return _multReading(v, 0.7, 1.3, 'Liq+pred. sized up',  'Fragile sized down',   'Neutral sizing');
  if (metricId === 'reality_breaker_multiplier')
    return _multReading(v, 0.5, 1.5, 'RB endorses',         'RB counsels caution',  'Neutral RB');
  return { tone: 'info', label: 'No reading', intensity: null };
}

// Phase 26.62 — single source of truth for metric coloring.
// Maps a live-reading `tone` onto the `.ff-value` color class so a
// metric cell's color is ALWAYS identical to the colored badge shown
// in its click-to-pin popover.  Eliminates the old bug where e.g. a
// 63% (bullish) P(up) cell was painted red because it inherited the
// row's blended direction class.
function _ffToneClass(tone) {
  switch (tone) {
    case 'bull':    return 'bull';
    case 'bear':    return 'bear';
    case 'caution': return 'caution';
    case 'neutral': return 'neutral';
    default:        return 'info';
  }
}
function _ffCellColor(metricId, value) {
  return _ffToneClass(_ffLiveReading(metricId, value).tone);
}

function _ffInfoPopoverHtml(metricId, currentValue) {
  const info = FF_METRIC_INFO[metricId];
  if (!info) return '';
  const reading = _ffLiveReading(metricId);
  const valueLine = (currentValue !== undefined && currentValue !== null && currentValue !== '')
    ? `<span class="ff-info-current">Current value: ${esc(String(currentValue))}</span>` : '';
  const intensityChip = reading.intensity
    ? `<span class="ff-info-reading-intensity int-${esc(reading.intensity)}">${esc(reading.intensity)} intensity</span>`
    : '';
  const readingBar = `
    <div class="ff-info-reading" data-tone="${esc(reading.tone)}">
      <span class="ff-info-reading-pill tone-${esc(reading.tone)}">${esc(reading.label)}</span>
      ${intensityChip}
    </div>`;
  return `
    <div class="ff-info-popover" data-testid="ff-info-popover" data-metric="${esc(metricId)}">
      <h4>${esc(info.label)}</h4>
      ${readingBar}
      <p>${esc(info.summary)}</p>
      <p><strong>Interpretation:</strong> ${esc(info.interpretation)}</p>
      <p><strong>Impact:</strong> ${esc(info.impact)}</p>
      ${valueLine ? `<div class="ff-info-meta">${valueLine}</div>` : ''}
    </div>`;
}

// Wrap a metric cell so clicking pins/unpins its info popover.
function _ffCellWrap(metricId, innerHtml, currentValue) {
  const isPinned = state.pinnedMetric && state.pinnedMetric.metricId === metricId;
  const cls = isPinned ? 'ff-pinned' : '';
  const pinnedTail = isPinned ? _ffInfoPopoverHtml(metricId, currentValue) : '';
  return `<div class="${cls}" data-ff-metric="${esc(metricId)}" data-ff-metric-current="${esc(String(currentValue ?? ''))}">${innerHtml}</div>${pinnedTail}`;
}

function futureMetricsForRow(row, horizonOverride) {
  if (!row || typeof row !== 'object') return null;
  const horizon = horizonOverride || state.futureHorizon || '1h_hold';
  const key = _FUTURE_HORIZON_KEY[horizon] || 'forward_1h';
  const garch = row.forward_metrics_garch && row.forward_metrics_garch[key];
  if (garch && typeof garch === 'object') {
    return Object.assign({}, garch, { _tier_source: 'garch' });
  }
  const fast = row.forward_metrics && row.forward_metrics[key];
  if (fast && typeof fast === 'object') {
    return Object.assign({}, fast, { _tier_source: 'fast' });
  }
  return null;
}

// =========================================================================
// Phase 26.52 — Effective Future-Forecast direction.
//
// The raw `direction_cf` field on the forward-metrics block is derived
// from p_up alone (a CF-shifted normal CDF on the drift / sigma ratio).
// When Lab Mode and/or Strategy Mode is active AND the user has the
// blend toggle on, the global ranking already multiplies the Kelly
// rank by the Lab + Strategy multipliers (see `rankScoreForRow`) —
// but the direction column itself stayed untouched, which produced
// the "Lab/Strategy summaries aren't impacting main FF direction
// reliably" report.
//
// This helper returns the blended direction the leaderboard should
// SHOW: when both blend toggles agree (one favours Bull while the
// other suppresses it), we fall back to Neutral.  A multiplier > 1.05
// reinforces the base direction; a multiplier < 0.95 flips it.  The
// 5% threshold keeps the column quiet when the blends are
// near-unity (no opinion).
// =========================================================================
function effectiveFutureDirection(fm) {
  if (!fm) return { dir: 'Neutral', adjusted: false };
  const baseDir = fm.direction_cf || fm.direction || 'Neutral';
  const labOn = !!(state.useLabMode && state.blendLabIntoRanking);
  const stratOn = !!(state.useStrategyMode && state.blendStrategyIntoRanking);
  // Phase 26.60 — Predictive Expansion blends now also influence the
  // visible direction so the column stays coherent with the rank.
  const sv2On = !!(state.useStrategyV2Mode && state.blendStrategyV2IntoRanking);
  const regRiskOn = !!(state.useRegimeRiskMode && state.blendRegimeRiskIntoRanking);
  const mlOn = !!(state.useMlOverlayMode && state.blendMlOverlayIntoRanking);
  const rbOn = !!(state.advancedExperimentalMode && state.blendRealityBreakerIntoRanking);
  if (!labOn && !stratOn && !sv2On && !regRiskOn && !mlOn && !rbOn) {
    return { dir: baseDir, adjusted: false };
  }
  // Compose effective multiplier from EVERY active blend (default 1.0 each).
  const _mult = (cond, val) =>
    cond && Number.isFinite(val) ? Math.max(0.2, Math.min(5.0, Number(val))) : 1.0;
  const labMult = _mult(labOn, fm.lab_rank_multiplier);
  const stratMult = _mult(stratOn, fm.strategy_rank_multiplier);
  const sv2Mult = _mult(sv2On, fm.strategy_v2_rank_multiplier);
  const regRiskMult = _mult(regRiskOn, fm.regime_risk_multiplier);
  const mlMult = _mult(mlOn, fm.ml_rank_multiplier);
  const rbMult = _mult(rbOn, fm.reality_breaker_multiplier);
  const composed = labMult * stratMult * sv2Mult * regRiskMult * mlMult * rbMult;
  // Phase 26.61c — Unlocked Experimental Mode bypasses the deadband
  // so even tiny multiplier deviations can shift the direction.  In
  // guarded mode, the 5% deadband keeps the column quiet on
  // near-unity multipliers.
  const unlocked = !!(state.advancedExperimentalMode && state.advancedExperimentalUnlocked);
  const REINFORCE_THRESHOLD = unlocked ? 1.0 + 1e-6 : 1.05;
  const DAMPEN_THRESHOLD = unlocked ? 1.0 - 1e-6 : 0.95;
  let dir = baseDir;
  let adjusted = false;
  if (baseDir === 'Bullish') {
    if (composed >= REINFORCE_THRESHOLD) { dir = 'Bullish'; adjusted = true; }
    else if (composed <= DAMPEN_THRESHOLD) {
      // Lab/Strategy actively pushing back against the base bull.
      // Guarded: → Neutral.  Unlocked: → Bearish (full flip allowed).
      dir = unlocked ? 'Bearish' : 'Neutral';
      adjusted = true;
    }
  } else if (baseDir === 'Bearish') {
    if (composed >= REINFORCE_THRESHOLD) { dir = 'Bearish'; adjusted = true; }
    else if (composed <= DAMPEN_THRESHOLD) {
      dir = unlocked ? 'Bullish' : 'Neutral';
      adjusted = true;
    }
  } else {
    // Base is Neutral — a strong reinforce in EITHER direction nudges
    // off Neutral.  Use the SIGN of (composed - 1) and the sign of the
    // current drift / kelly to pick a direction.
    if (composed >= REINFORCE_THRESHOLD || composed <= DAMPEN_THRESHOLD) {
      const driftSign = Math.sign(Number(fm.drift_pct ?? 0) + Number(fm.jump_drift_pct ?? 0));
      const kellySign = Math.sign(Number(fm.effective_kelly_rank ?? 0));
      const tieSign = driftSign || kellySign;
      if (composed >= REINFORCE_THRESHOLD && tieSign > 0) { dir = 'Bullish'; adjusted = true; }
      else if (composed >= REINFORCE_THRESHOLD && tieSign < 0) { dir = 'Bearish'; adjusted = true; }
      // composed < 1 with base=Neutral → stays Neutral (suppression on
      // an already-neutral row is a no-op).
    }
  }
  return {
    dir, adjusted,
    composed_multiplier: composed,
    lab_multiplier: labMult,
    strategy_multiplier: stratMult,
    strategy_v2_multiplier: sv2Mult,
    regime_risk_multiplier: regRiskMult,
    ml_multiplier: mlMult,
    reality_breaker_multiplier: rbMult,
  };
}

// =========================================================================
// Phase 26.63 — Conviction-weighted directional consensus engine.
//
// The "Overall blended forecast" banner and the per-section summaries all
// need to fuse many heterogeneous signals into ONE directional call.  We do
// this with a transparent, bounded scheme:
//
//   * DIRECTIONAL signals each cast a signed vote in [-1, +1] (bull = +,
//     bear = -) scaled by their own conviction (the live-reading intensity
//     band).  We sum the votes and squash with tanh so agreement compounds
//     but the score stays in (-1, +1).
//
//   * QUALITY signals (predictability, stability, liquidity, tail-risk,
//     chaos) never vote on direction.  Instead they raise (bull-toned) or
//     lower (caution-toned) the overall CONVICTION multiplier M ∈ [0.65,
//     1.4], which scales the intensity band — a strong directional call on
//     a fragile, chaotic, illiquid tape is correctly down-weighted.
//
// Both helpers reuse `_ffLiveReading` so the consensus can NEVER disagree
// with the colored chip/badge a user sees for the same metric.
// =========================================================================
const _FF_INTENSITY_WEIGHT = { extreme: 1.0, strong: 0.72, moderate: 0.42, weak: 0.16 };
function _ffReadingWeight(intensity) {
  if (!intensity) return 0.5;            // directional read with no band → solid default
  return _FF_INTENSITY_WEIGHT[intensity] || 0.3;
}
// Signed directional vote from a metric's reading (bull/bear only).
function _ffDirVote(metricId, value) {
  const r = _ffLiveReading(metricId, value);
  if (r.tone === 'bull') return +_ffReadingWeight(r.intensity);
  if (r.tone === 'bear') return -_ffReadingWeight(r.intensity);
  return 0;
}
// Signed CONVICTION vote: bull-toned quality (good) raises conviction,
// caution-toned quality (bad) lowers it; everything else is neutral.
function _ffQualityVote(metricId, value) {
  const r = _ffLiveReading(metricId, value);
  if (r.tone === 'bull')    return +_ffReadingWeight(r.intensity);
  if (r.tone === 'caution') return -_ffReadingWeight(r.intensity);
  return 0;
}
function _ffScoreToDir(score) {
  return score >  0.12 ? { tone: 'bull', label: 'Bullish' }
       : score < -0.12 ? { tone: 'bear', label: 'Bearish' }
       : { tone: 'neutral', label: 'Neutral' };
}
function _ffIntensityLabel(mag) {
  return mag >= 0.62 ? 'extreme'
       : mag >= 0.38 ? 'strong'
       : mag >= 0.16 ? 'moderate' : 'weak';
}

// Fuse EVERY active section's signals into one blended directional forecast.
// `blk` is the active-horizon block (GARCH preferred), plus the per-symbol
// lab / strategy / predictive bundles.  Section toggles gate which signals
// participate, mirroring exactly what the user sees rendered on the card.
function _ffBlendedForecast(blk, lab, strategy, predictive) {
  blk = blk || {};
  const pUpCf = Number.isFinite(blk.p_up_cf) ? blk.p_up_cf : (Number.isFinite(blk.p_up) ? blk.p_up : 0.5);
  const drift = Number(blk.drift_pct || 0) + Number(blk.jump_drift_pct || 0);
  const kelly = Number.isFinite(blk.effective_kelly_rank) ? blk.effective_kelly_rank : Number(blk.kelly_rank || 0);
  const rawDir = blk.direction_cf || blk.direction || 'Neutral';

  // ---- DIRECTIONAL VOTES ----
  const dirVotes = [];   // { id, label, v }
  const pushDir = (id, label, value, scale) => {
    const v = _ffDirVote(id, value) * (scale || 1);
    if (v !== 0) dirVotes.push({ id, label, v });
  };
  // Base Cornish-Fisher core — the canonical anchor (weighted ×1.5).
  pushDir('direction_cf', 'CF direction', rawDir, 1.5);
  pushDir('p_up_cf', 'P(up) CF', pUpCf, 1.2);
  pushDir('drift_pct', 'Drift+jump', drift, 1.0);
  pushDir('effective_kelly_rank', 'Eff. Kelly', kelly, 0.8);

  const labOn = !!(state.useLabMode && lab);
  const stratOn = !!(state.useStrategyMode && strategy);
  const predOn = !!((state.useStrategyV2Mode || state.useRegimeRiskMode
                  || state.useMlOverlayMode || state.blendLiqKellyFactor
                  || state.advancedExperimentalMode) && predictive);
  const rbOn = !!(state.advancedExperimentalMode && predictive);

  if (labOn) {
    pushDir('ssa_trend_slope_pct_per_day', 'SSA slope', lab.ssa_trend_slope_pct_per_day, 0.9);
    pushDir('rsv_upside_share', 'RSV+ share', lab.rsv_upside_share, 0.7);
    pushDir('egarch_leverage_gamma', 'EGARCH γ', lab.egarch_leverage_gamma, 0.5);
  }
  if (stratOn) {
    pushDir('strategy_emd_slope_pct', 'EMD slope', strategy.emd_imf1_slope_pct_per_day, 0.9);
    pushDir('strategy_ar1', 'AR(1)', strategy.ar1_coefficient, 0.6);
  }
  if (predOn) {
    pushDir('msm_drift_premium', 'MSM drift', predictive.msm_drift_premium, 0.9);
    pushDir('trend_curvature_pct', 'Trend curvature', predictive.trend_curvature_pct, 0.8);
    pushDir('multiscale_consistency', 'Multi-scale', predictive.multiscale_consistency, 0.9);
    pushDir('ml_residual_edge', 'ML edge', predictive.ml_residual_edge, 0.8);
    pushDir('drawdown_memory_score', 'Drawdown memory', predictive.drawdown_memory_score, 0.4);
  }
  if (rbOn) {
    pushDir('local_causal_cone_signal', 'LCC', predictive.local_causal_cone_signal, 0.6);
    pushDir('quantum_path_interference_index', 'QPII', predictive.quantum_path_interference_index, 0.6);
  }

  const sumV = dirVotes.reduce((a, b) => a + b.v, 0);
  const score = Math.tanh(sumV / 3.5);   // bounded consensus in (-1, 1)

  // ---- QUALITY / CONVICTION VOTES ----
  const qVotes = [];
  const pushQ = (id, label, value) => {
    const v = _ffQualityVote(id, value);
    if (v !== 0) qVotes.push({ id, label, v });
  };
  pushQ('directional_certainty_cf', 'CF certainty', blk.directional_certainty_cf);
  if (predOn) {
    pushQ('ts_nonlinear_dependence', 'Nonlinear dep.', predictive.ts_nonlinear_dependence);
    pushQ('lead_lag_influence', 'Lead-lag', predictive.lead_lag_influence);
    pushQ('liq_adjusted_signal', 'Liq-adj.', predictive.liq_adjusted_signal);
    pushQ('entropy_regime_stability', 'Regime stability', predictive.entropy_regime_stability);
    pushQ('volofvol_regime_score', 'Vol-of-vol', predictive.volofvol_regime_score);
  }
  if (rbOn) {
    pushQ('local_lyapunov_volatility_exponent', 'LLVE', predictive.local_lyapunov_volatility_exponent);
    pushQ('temporal_renormalization_score', 'TRS', predictive.temporal_renormalization_score);
  }
  if (labOn) {
    pushQ('vol_hmm_p_stressed', 'Vol HMM', lab.vol_hmm_p_stressed);
    pushQ('permutation_entropy', 'Perm entropy', lab.permutation_entropy);
    pushQ('lab_qi_certainty', 'QI certainty', blk.lab_qi_certainty);
  }
  if (stratOn) {
    pushQ('strategy_rqa_determinism', 'RQA determ.', strategy.rqa_determinism_pct);
  }
  const qMean = qVotes.length ? qVotes.reduce((a, b) => a + b.v, 0) / qVotes.length : 0;
  const conviction = Math.max(0.65, Math.min(1.4, 1 + 0.35 * qMean));

  const dir = _ffScoreToDir(score);
  const intensity = _ffIntensityLabel(Math.abs(score) * conviction);

  // Top contributors (by |vote|) for the rationale line.
  const reasons = dirVotes
    .slice()
    .sort((a, b) => Math.abs(b.v) - Math.abs(a.v))
    .slice(0, 4)
    .map((c) => `${c.label} ${c.v > 0 ? '↑' : '↓'}`);
  if (qMean <= -0.15) reasons.push('low-conviction tape');
  else if (qMean >= 0.2) reasons.push('high-conviction tape');

  return {
    score, conviction, dir, intensity,
    n_signals: dirVotes.length,
    n_quality: qVotes.length,
    reasons,
    sections: { labOn, stratOn, predOn, rbOn },
  };
}

// =========================================================================
// Phase 26.67 — Experimental all-metrics composite forecast.
//
// A deliberately MORE-THOROUGH, SEPARATE system from the per-horizon
// readings and the next-day-open predictor.  It throws EVERY available
// signal on the stock (regardless of which blend toggles are active) into
// one conviction-weighted vote and resolves a SINGLE Up/Down/Neutral call
// — with a confidence % — for each of seven hold timeframes.
//
//   * Base directional core is rescaled to each timeframe by trading-time:
//       μ_T = μ_day · f      σ_T = σ_day · √f      P(up) = Φ(μ_T / σ_T)
//     so the Sharpe-style directional edge grows with the horizon
//     (overnight uses the dedicated session structure instead).
//   * Every metric votes ±(conviction) and is weighted by its timeframe
//     AFFINITY: micro-scale signals (jumps, AR(1), entropy) dominate
//     intraday; macro trend/regime signals (SSA/EMD slope, MSM drift,
//     multi-scale, curvature) dominate week/month.
//   * Quality signals (predictability, stability, liquidity, tail-risk,
//     chaos) modulate the confidence, never the direction.   EXPERIMENTAL.
// =========================================================================
function _normCdf(z) { return 0.5 * (1.0 + _erf(z / Math.SQRT2)); }

const _EXP_TIMEFRAMES = [
  { key: '15m',       label: '15-min',    f: 15 / 390,  band: 'micro' },
  { key: '1h',        label: '1-hour',    f: 60 / 390,  band: 'micro' },
  { key: '4h',        label: '4-hour',    f: 240 / 390, band: 'mid' },
  { key: '10h',       label: '10-hour',   f: 600 / 390, band: 'mid' },
  { key: 'overnight', label: 'Overnight', f: null,      band: 'mid', session: 'overnight' },
  { key: 'week',      label: '1-week',    f: 5,         band: 'macro' },
  { key: 'month',     label: '1-month',   f: 21,        band: 'macro' },
];

// [metricId, value-getter(ctx), scale-band, base-weight]
const _EXP_DIR_METRICS = [
  ['direction_cf', (c) => c.blk.direction_cf, 'mid', 1.4],
  ['p_up_cf', (c) => c.blk.p_up_cf, 'mid', 1.2],
  ['drift_pct', (c) => Number(c.blk.drift_pct || 0) + Number(c.blk.jump_drift_pct || 0), 'mid', 1.0],
  ['jump_drift_pct', (c) => c.blk.jump_drift_pct, 'micro', 0.6],
  ['effective_kelly_rank', (c) => c.blk.effective_kelly_rank, 'mid', 0.8],
  ['ssa_trend_slope_pct_per_day', (c) => c.lab.ssa_trend_slope_pct_per_day, 'macro', 0.9],
  ['rsv_upside_share', (c) => c.lab.rsv_upside_share, 'mid', 0.6],
  ['egarch_leverage_gamma', (c) => c.lab.egarch_leverage_gamma, 'mid', 0.4],
  ['strategy_emd_slope_pct', (c) => c.strategy.emd_imf1_slope_pct_per_day, 'macro', 0.8],
  ['strategy_ar1', (c) => c.strategy.ar1_coefficient, 'micro', 0.7],
  ['strategy_vol_regime_mom', (c) => c.strategy.vol_regime_momentum, 'mid', 0.5],
  ['msm_drift_premium', (c) => c.pred.msm_drift_premium, 'macro', 0.9],
  ['trend_curvature_pct', (c) => c.pred.trend_curvature_pct, 'macro', 0.7],
  ['multiscale_consistency', (c) => c.pred.multiscale_consistency, 'macro', 0.9],
  ['ml_residual_edge', (c) => c.pred.ml_residual_edge, 'mid', 0.8],
  ['drawdown_memory_score', (c) => c.pred.drawdown_memory_score, 'macro', 0.4],
  ['local_causal_cone_signal', (c) => c.pred.local_causal_cone_signal, 'mid', 0.5],
  ['quantum_path_interference_index', (c) => c.pred.quantum_path_interference_index, 'mid', 0.5],
];

const _EXP_QUAL_METRICS = [
  ['directional_certainty_cf', (c) => c.blk.directional_certainty_cf],
  ['ts_nonlinear_dependence', (c) => c.pred.ts_nonlinear_dependence],
  ['lead_lag_influence', (c) => c.pred.lead_lag_influence],
  ['liq_adjusted_signal', (c) => c.pred.liq_adjusted_signal],
  ['entropy_regime_stability', (c) => c.pred.entropy_regime_stability],
  ['volofvol_regime_score', (c) => c.pred.volofvol_regime_score],
  ['vol_hmm_p_stressed', (c) => c.lab.vol_hmm_p_stressed],
  ['permutation_entropy', (c) => c.lab.permutation_entropy],
  ['lab_qi_certainty', (c) => c.blk.lab_qi_certainty],
  ['strategy_rqa_determinism', (c) => c.strategy.rqa_determinism_pct],
  ['local_lyapunov_volatility_exponent', (c) => c.pred.local_lyapunov_volatility_exponent],
  ['temporal_renormalization_score', (c) => c.pred.temporal_renormalization_score],
];

function _expAffinity(metricBand, tfBand) {
  if (metricBand === tfBand) return 1.0;
  if (metricBand === 'mid' || tfBand === 'mid') return 0.75;
  return 0.45;   // micro vs macro — opposite ends of the scale ladder
}

function _experimentalComposite(detail, tf) {
  const garch = detail.forward_metrics_garch && detail.forward_metrics_garch.forward_1d;
  const fast = detail.forward_metrics && detail.forward_metrics.forward_1d;
  const daily = garch || fast || {};
  const muDay = Number(daily.drift_pct || 0);
  const sigDay = Number(daily.sigma_pct || 0);
  let basePUp = 0.5;
  if (tf.session === 'overnight') {
    const onBlk = (detail.forward_metrics_garch && detail.forward_metrics_garch.forward_overnight)
               || (detail.forward_metrics && detail.forward_metrics.forward_overnight);
    if (onBlk && Number.isFinite(onBlk.p_up_cf)) basePUp = onBlk.p_up_cf;
    else if (sigDay > 0) basePUp = _normCdf((0.55 * muDay) / (sigDay * Math.sqrt(0.40)));
  } else if (sigDay > 0 && tf.f) {
    basePUp = _normCdf((muDay * tf.f) / (sigDay * Math.sqrt(tf.f)));
  }
  const baseVote = Math.max(-1, Math.min(1, 2 * (basePUp - 0.5)));

  const ctx = {
    blk: daily,
    lab: detail.lab_signals || {},
    strategy: detail.strategy_signals || {},
    pred: detail.predictive_expansion_signals || {},
  };
  let sumV = 1.6 * baseVote;   // base core anchored
  let up = 0, down = 0, n = 0;
  _EXP_DIR_METRICS.forEach(([id, getter, band, w]) => {
    let val; try { val = getter(ctx); } catch (_) { val = undefined; }
    if (val === undefined || val === null) return;
    const vote = _ffDirVote(id, val);
    if (vote === 0) return;
    sumV += w * _expAffinity(band, tf.band) * vote;
    n += 1;
    if (vote > 0) up += 1; else down += 1;
  });
  const score = Math.tanh(sumV / 4.5);

  const qv = [];
  _EXP_QUAL_METRICS.forEach(([id, getter]) => {
    let val; try { val = getter(ctx); } catch (_) { val = undefined; }
    if (val === undefined || val === null) return;
    const q = _ffQualityVote(id, val);
    if (q !== 0) qv.push(q);
  });
  const qMean = qv.length ? qv.reduce((a, b) => a + b, 0) / qv.length : 0;
  const quality = Math.max(0.6, Math.min(1.35, 1 + 0.35 * qMean));

  const dir = score > 0.06 ? 'Up' : score < -0.06 ? 'Down' : 'Neutral';
  const tone = score > 0.06 ? 'bull' : score < -0.06 ? 'bear' : 'neutral';
  const confidence = dir === 'Neutral'
    ? Math.round(50 + Math.abs(score) * 20)
    : Math.round(Math.min(99, 50 + Math.abs(score) * 49 * quality));
  return { tf, dir, tone, score, confidence, up, down, n, basePUp, quality };
}

function renderExperimentalCompositeCard(detail) {
  if (!detail) return '';
  if (!(detail.forward_metrics || detail.forward_metrics_garch)) return '';
  const results = _EXP_TIMEFRAMES.map((tf) => _experimentalComposite(detail, tf));
  const arrowOf = (d) => (d === 'Up' ? '\u25B2' : d === 'Down' ? '\u25BC' : '\u25C6');
  // State-preservation: the open breakdown key is lifted to `state.expOpenTf`
  // (keyed by symbol) so details-column refresh passes re-render the SAME
  // open state instead of despawning the dropdown. Only an explicit user
  // click, a symbol change, or a section reset closes it.
  const openKey = (state.expOpenTf && state.expOpenTf.symbol === detail.symbol) ? state.expOpenTf.key : null;
  if (openKey) state.detailAudit.statePreserved += 1;
  const btns = results.map((r) => `
    <button type="button" class="exp-tf-btn tone-${r.tone}${openKey === r.tf.key ? ' is-active' : ''}" data-exp-tf="${r.tf.key}" data-testid="exp-composite-${r.tf.key}">
      <span class="exp-tf-label">${r.tf.label}</span>
      <span class="exp-tf-call">${arrowOf(r.dir)} ${r.dir}</span>
      <span class="exp-tf-conf">${r.confidence}%</span>
    </button>`).join('');
  const details = results.map((r) => `
    <div class="exp-tf-detail" data-exp-detail="${r.tf.key}" ${openKey === r.tf.key ? '' : 'hidden'}>
      <strong>${r.tf.label} \u2192 ${arrowOf(r.dir)} ${r.dir}</strong> &middot; confidence ${r.confidence}%
      &middot; ${r.n} directional signals (${r.up}\u2191 / ${r.down}\u2193)
      &middot; base P(up) ${(r.basePUp * 100).toFixed(1)}%
      &middot; conviction \u00d7${r.quality.toFixed(2)}
      &middot; raw score ${r.score.toFixed(3)}
    </div>`).join('');
  return `
    <div class="exp-composite-card" data-testid="exp-composite-card">
      <h3 class="section-title">Experimental composite \u2014 all-metrics vibe <span class="exp-badge">EXPERIMENTAL</span></h3>
      <div class="exp-composite-hint">Throws every available signal on this stock (used or not) into a single conviction-weighted, timeframe-affinity-scaled vote &mdash; resolving one Up/Down call per hold. More thorough than the per-horizon readings; click a timeframe for its breakdown.</div>
      <div class="exp-tf-grid">${btns}</div>
      <div class="exp-tf-details">${details}</div>
    </div>`;
}

function _expCompositeClick(ev) {
  const btn = ev.target.closest('.exp-tf-btn');
  if (!btn) return;
  const card = btn.closest('.exp-composite-card');
  if (!card) return;
  ev.preventDefault();
  const key = btn.getAttribute('data-exp-tf');
  const wasActive = btn.classList.contains('is-active');
  card.querySelectorAll('.exp-tf-btn').forEach((b) => b.classList.remove('is-active'));
  card.querySelectorAll('.exp-tf-detail').forEach((d) => { d.hidden = true; });
  if (!wasActive) {
    btn.classList.add('is-active');
    const det = card.querySelector(`.exp-tf-detail[data-exp-detail="${key}"]`);
    if (det) det.hidden = false;
    // Persist to state so the next detail refresh pass re-renders the
    // breakdown open instead of collapsing it.
    state.expOpenTf = { symbol: state.selectedSymbol, key };
  } else {
    state.expOpenTf = null;
  }
}

// Phase 26.67 — compact per-row consensus strip for the leaderboard.
// Renders the 7-timeframe experimental composite as tiny arrows so the
// user can spot multi-horizon agreement at a glance.  Memoised on the row
// object (keyed by a cheap value signature) so the 1-2s live-tick
// re-renders don't recompute ~30 readings × 7 timeframes per row.
function _rowConsensusCounts(row) {
  const fm = row.forward_metrics_garch || row.forward_metrics;
  const blk = fm && fm.forward_1d;
  if (!blk) return { up: 0, down: 0, net: 0, has: false };
  const pred = row.predictive_expansion_signals || {};
  const sig = `${blk.drift_pct}|${blk.sigma_pct}|${blk.p_up_cf}|${pred.msm_drift_premium}|${pred.multiscale_consistency}`;
  if (row.__csSig === sig && row.__csCounts) return row.__csCounts;
  let up = 0, down = 0;
  const cells = _EXP_TIMEFRAMES.map((tf) => {
    const r = _experimentalComposite(row, tf);
    if (r.dir === 'Up') up += 1; else if (r.dir === 'Down') down += 1;
    const glyph = r.dir === 'Up' ? '\u25B2' : r.dir === 'Down' ? '\u25BC' : '\u00B7';
    const op = Math.max(0.35, Math.min(1, (r.confidence - 50) / 49)).toFixed(2);
    return `<span class="cs-cell tone-${r.tone}" style="opacity:${op}" title="${tf.label}: ${r.dir} ${r.confidence}%">${glyph}</span>`;
  }).join('');
  const agree = up >= 6 ? 'cs-strong-up' : down >= 6 ? 'cs-strong-down' : '';
  row.__csSig = sig;
  row.__csCounts = { up, down, net: up - down, has: true };
  row.__csHtml = `<span class="cs-strip ${agree}" title="Experimental 7-timeframe consensus (15m·1h·4h·10h·ON·Wk·Mo) — ${up}↑ / ${down}↓">${cells}</span>`;
  return row.__csCounts;
}
function _rowConsensusStrip(row) {
  const c = _rowConsensusCounts(row);
  if (!c.has) return '<span class="cs-empty" title="No forecast depth on this row yet">—</span>';
  return row.__csHtml;
}

// =========================================================================
// Phase 26.47 — Future Forecast detail card.
//
// Surfaces the full server-side forward_metrics block(s) for the
// currently-selected detail symbol, plus the per-symbol
// advanced-math bundle (Hurst regime, jump intensity, OU half-life,
// HAR-RV daily sigma).  When both fast + GARCH tiers are attached,
// shows them side-by-side.
// =========================================================================

// =========================================================================
// Phase 26.49 — Deep Refresh + Click-to-Pin + System Reset helpers.
// =========================================================================

// Deep refresh a single symbol through the full Future Mode pipeline.
// Disables the button, calls POST /api/future_mode/refresh/{symbol},
// then re-renders both detail panel and snapshot table.
async function triggerDeepRefresh(symbol, btn) {
  if (!symbol) return;
  const market = state.currentMarket || 'stocks';
  const originalHtml = btn ? btn.innerHTML : '';
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="ff-refresh-spin">↻</span> Refreshing…';
  }
  try {
    const url = `${state.apiBase}/api/future_mode/refresh/${encodeURIComponent(symbol)}?market=${encodeURIComponent(market)}`;
    const resp = await fetch(url, { method: 'POST', credentials: 'same-origin' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const payload = await resp.json();
    // Refresh detail panel by reloading from state.detailPayload —
    // the upserted row will land in the snapshot store on the next
    // poll automatically.
    state.detailPayload = payload;
    if (state.selectedSymbol) {
      await loadDetail(state.selectedSymbol);
    }
    showResetBanner(`Deep refresh complete for ${symbol} (${payload.deep_refresh_elapsed_ms || 0} ms)`, 'info');
  } catch (e) {
    showResetBanner(`Deep refresh failed for ${symbol}: ${e.message}`, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = originalHtml || '↻ Deep Refresh';
    }
  }
}

// Toggle the pinned info popover for a metric.  Only one popover can
// be open at a time.  The popover persists through 2-second live-tick
// refreshes because `renderFutureForecastCard` checks `state.pinnedMetric`
// on every render and re-attaches the popover inline.
function togglePinnedMetric(metricId, currentValue) {
  if (!metricId) return;
  if (state.pinnedMetric && state.pinnedMetric.metricId === metricId) {
    state.pinnedMetric = null;
  } else {
    state.pinnedMetric = { metricId, currentValue, symbol: state.selectedSymbol || null };
  }
  // Re-render JUST the future forecast card without reloading the
  // detail panel.  Find the existing card and rebuild its innerHTML.
  const card = document.querySelector('.future-forecast-card');
  if (card && state.detailPayload) {
    const rebuilt = renderFutureForecastCard(state.detailPayload);
    // The rebuilt HTML wraps in a NEW root div — replace.
    const wrapper = document.createElement('div');
    wrapper.innerHTML = rebuilt;
    const newCard = wrapper.firstElementChild;
    if (newCard && card.parentNode) {
      card.parentNode.replaceChild(newCard, card);
    }
  }
}

// Single delegated click handler for the entire Future Forecast card.
// Wired ONCE at startup; survives all the card's internal re-renders.
function _ffDelegatedClick(ev) {
  const card = ev.target.closest('.future-forecast-card');
  if (!card) return;
  // 1) Deep refresh button.
  const refreshBtn = ev.target.closest('.ff-deep-refresh');
  if (refreshBtn) {
    ev.preventDefault();
    const sym = refreshBtn.getAttribute('data-symbol') || state.selectedSymbol;
    triggerDeepRefresh(sym, refreshBtn);
    return;
  }
  // 2) Metric cell click → pin/unpin popover.
  const cell = ev.target.closest('[data-ff-metric]');
  if (cell) {
    ev.preventDefault();
    const metricId = cell.getAttribute('data-ff-metric');
    const currentValue = cell.getAttribute('data-ff-metric-current') || '';
    togglePinnedMetric(metricId, currentValue);
  }
}

// Soft / Hard reset modal management.
function showResetModal(mode) {
  const modal = byId('resetConfirmModal');
  if (!modal) return;
  const title = byId('resetModalTitle');
  const body = byId('resetModalBody');
  const confirmBtn = byId('resetModalConfirm');
  if (title) title.textContent = mode === 'hard' ? 'Confirm HARD reset' : 'Confirm soft reset';
  if (body) {
    body.textContent = mode === 'hard'
      ? 'This will reset all in-memory state AND wipe on-disk shard caches (cached_crypto_universe.json, coingecko caches). User data, regulatory.db and daily_history_cache are preserved. Cached data continues to serve the dashboard until the fresh scan repopulates. Continue?'
      : 'This will reset all in-memory state (snapshot, circuit breakers, Future Mode caches, priority lane). Cached data continues to serve the dashboard until the fresh scan repopulates. Continue?';
  }
  if (confirmBtn) {
    confirmBtn.className = mode === 'hard'
      ? 'reset-btn reset-btn-hard'
      : 'reset-btn reset-btn-soft';
    confirmBtn.textContent = mode === 'hard' ? 'Hard reset' : 'Soft reset';
    confirmBtn.onclick = () => {
      hideResetModal();
      triggerSystemReset(mode);
    };
  }
  const cancelBtn = byId('resetModalCancel');
  if (cancelBtn) cancelBtn.onclick = hideResetModal;
  modal.hidden = false;
}

function hideResetModal() {
  const modal = byId('resetConfirmModal');
  if (modal) modal.hidden = true;
}

async function triggerSystemReset(mode) {
  try {
    const url = `${state.apiBase}/api/system/reset?mode=${encodeURIComponent(mode)}${mode === 'hard' ? '&confirm=true' : ''}`;
    const resp = await fetch(url, { method: 'POST', credentials: 'same-origin' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const cleared = data.in_memory_cleared || {};
    const disk = data.disk_caches ? ` · ${data.disk_caches.count} disk files` : '';
    showResetBanner(
      `System ${mode} reset complete: cleared ${cleared.snapshot_rows || 0} snapshot rows, ${cleared.advanced_cache || 0} advanced + ${cleared.lab_cache || 0} lab cache entries, ${cleared.circuit_breakers_reset || 0} breakers${disk}.  Cached data will be served while the next scan repopulates.`,
      'info',
    );
    bumpFilterSortCache();
    // Snapshot picks up reset on next poll; detail reloads if open.
    if (state.selectedSymbol) loadDetail(state.selectedSymbol);
  } catch (e) {
    showResetBanner(`Reset failed: ${e.message}`, 'error');
  }
}

function showResetBanner(message, level) {
  const banner = byId('resetBanner');
  if (!banner) return;
  banner.textContent = message;
  banner.className = `reset-banner${level === 'error' ? ' error' : ''}`;
  banner.hidden = false;
  banner.classList.remove('fading');
  // Auto-fade after 8s.
  clearTimeout(banner._fadeTimer);
  banner._fadeTimer = setTimeout(() => {
    banner.classList.add('fading');
    setTimeout(() => { banner.hidden = true; }, 400);
  }, 8000);
}


function renderFutureForecastCard(detail) {
  if (!detail) return '';
  const horizon = state.futureHorizon || '1h_hold';
  const key = _FUTURE_HORIZON_KEY[horizon] || 'forward_1h';
  // -------------------------------------------------------------------
  // Phase 26.62 — live-tick continuity.  The 2-second detail refresh
  // can momentarily land on a transient snapshot row that lacks the
  // forward bundle (a cheap-pass re-score writes forward_metrics=None),
  // which used to blank the WHOLE Future Forecast card for that tick
  // and made the metrics "randomly disappear" while scrolling.  We
  // cache the last-good bundle per symbol and transparently reuse it
  // when the live payload is missing it, so the panel never flickers.
  // -------------------------------------------------------------------
  const _sym0 = detail.symbol || '';
  const _liveHasBundle = !!(detail.forward_metrics || detail.forward_metrics_garch
    || detail.advanced_signals || detail.lab_signals || detail.strategy_signals
    || detail.predictive_expansion_signals);
  if (_liveHasBundle) {
    state._lastGoodForecast = {
      symbol: _sym0,
      forward_metrics: detail.forward_metrics,
      forward_metrics_garch: detail.forward_metrics_garch,
      advanced_signals: detail.advanced_signals,
      lab_signals: detail.lab_signals,
      strategy_signals: detail.strategy_signals,
      predictive_expansion_signals: detail.predictive_expansion_signals,
    };
  } else if (state._lastGoodForecast && state._lastGoodForecast.symbol === _sym0) {
    const _lg = state._lastGoodForecast;
    detail = Object.assign({}, detail, {
      forward_metrics: _lg.forward_metrics,
      forward_metrics_garch: _lg.forward_metrics_garch,
      advanced_signals: _lg.advanced_signals,
      lab_signals: _lg.lab_signals,
      strategy_signals: _lg.strategy_signals,
      predictive_expansion_signals: _lg.predictive_expansion_signals,
    });
  }
  const fast = detail.forward_metrics && detail.forward_metrics[key];
  const garch = detail.forward_metrics_garch && detail.forward_metrics_garch[key];
  const adv = detail.advanced_signals || null;
  const lab = detail.lab_signals || null;
  const strategy = detail.strategy_signals || null;
  // Phase 26.60 — Predictive Expansion bundle on the row.
  const predictive = detail.predictive_expansion_signals || null;
  if (!fast && !garch && !adv && !lab && !strategy && !predictive) return '';

  const symbol = detail.symbol || '';

  const renderTierBlock = (block, tierName, badgeClass) => {
    if (!block) return '';
    const rawDirCf = block.direction_cf || block.direction || 'Neutral';
    // Phase 26.52 — Apply the same Lab/Strategy blending used by the
    // main leaderboard so the popover and the row agree on direction.
    const dirInfo = effectiveFutureDirection(block);
    const dirCf = dirInfo.dir;
    const dirAdjusted = dirInfo.adjusted;
    const dirClass = dirCf === 'Bullish' ? 'bull' : dirCf === 'Bearish' ? 'bear' : '';
    const pUpCf = Number.isFinite(block.p_up_cf) ? block.p_up_cf : (Number.isFinite(block.p_up) ? block.p_up : 0.5);
    const pUpGauss = Number.isFinite(block.p_up_gauss) ? block.p_up_gauss : (Number.isFinite(block.p_up) ? block.p_up : 0.5);
    const drift = Number.isFinite(block.drift_pct) ? block.drift_pct : 0;
    const jumpDrift = Number.isFinite(block.jump_drift_pct) ? block.jump_drift_pct : 0;
    const sigma = Number.isFinite(block.sigma_pct) ? block.sigma_pct : 0;
    const var95 = Number.isFinite(block.var95_pct) ? block.var95_pct : 0;
    const cvar95 = Number.isFinite(block.cvar95_pct) ? block.cvar95_pct : 0;
    const kelly = Number.isFinite(block.effective_kelly_rank) ? block.effective_kelly_rank : (block.kelly_rank || 0);
    const kellyFrac = Number.isFinite(block.kelly_fraction) ? block.kelly_fraction : 0;
    const regime = block.regime_label || (adv ? adv.regime_label : 'unknown');
    const certCf = Number.isFinite(block.directional_certainty_cf) ? block.directional_certainty_cf : 0;
    const garchAnn = block.garch_annualised_vol_pct;
    // Each metric cell is click-to-pin (info popover persists across 2s tick).
    return `
      <div class="ff-tier-row">
        <span class="ff-tier-badge ${badgeClass}">${tierName}</span>
        <span class="ff-value ${dirClass}">${esc(dirCf)}${dirAdjusted ? ` <span title="Direction adjusted by Lab/Strategy blends (raw: ${esc(rawDirCf)})" style="color:#fbbf24;font-size:.75em">*</span>` : ''}</span>
        <span class="ff-label">@ ${esc(_FUTURE_HORIZON_LABEL[horizon] || horizon)}</span>
      </div>
      <div class="ff-grid">
        ${_ffCellWrap('direction_cf', `<span class="ff-label">Direction CF</span><span class="ff-value ${_ffCellColor('direction_cf', rawDirCf)}">${esc(rawDirCf)}</span>`, rawDirCf)}
        ${_ffCellWrap('p_up_cf', `<span class="ff-label">P(up) CF</span><span class="ff-value ${_ffCellColor('p_up_cf', pUpCf)}">${(pUpCf*100).toFixed(1)}%</span>`, `${(pUpCf*100).toFixed(1)}%`)}
        ${_ffCellWrap('p_up_gauss', `<span class="ff-label">P(up) Gauss</span><span class="ff-value ${_ffCellColor('p_up_gauss', pUpGauss)}">${(pUpGauss*100).toFixed(1)}%</span>`, `${(pUpGauss*100).toFixed(1)}%`)}
        ${_ffCellWrap('drift_pct', `<span class="ff-label">Drift</span><span class="ff-value ${_ffCellColor('drift_pct', drift)}">${drift.toFixed(3)}%</span>`, `${drift.toFixed(3)}%`)}
        ${_ffCellWrap('jump_drift_pct', `<span class="ff-label">+ Jump drift</span><span class="ff-value ${_ffCellColor('jump_drift_pct', jumpDrift)}">${jumpDrift.toFixed(3)}%</span>`, `${jumpDrift.toFixed(3)}%`)}
        ${_ffCellWrap('sigma_pct', `<span class="ff-label">Sigma</span><span class="ff-value ${_ffCellColor('sigma_pct', sigma)}">${sigma.toFixed(3)}%</span>`, `${sigma.toFixed(3)}%`)}
        ${_ffCellWrap('var95_pct', `<span class="ff-label">VaR 95%</span><span class="ff-value ${_ffCellColor('var95_pct', var95)}">-${var95.toFixed(2)}%</span>`, `-${var95.toFixed(2)}%`)}
        ${_ffCellWrap('cvar95_pct', `<span class="ff-label">CVaR 95%</span><span class="ff-value ${_ffCellColor('cvar95_pct', cvar95)}">-${cvar95.toFixed(2)}%</span>`, `-${cvar95.toFixed(2)}%`)}
        ${_ffCellWrap('directional_certainty_cf', `<span class="ff-label">Cert. CF</span><span class="ff-value ${_ffCellColor('directional_certainty_cf', certCf)}">${(certCf*100).toFixed(1)}%</span>`, `${(certCf*100).toFixed(1)}%`)}
        ${_ffCellWrap('kelly_fraction', `<span class="ff-label">Kelly frac</span><span class="ff-value ${_ffCellColor('kelly_fraction', kellyFrac)}">${(kellyFrac*100).toFixed(1)}%</span>`, `${(kellyFrac*100).toFixed(1)}%`)}
        ${_ffCellWrap('effective_kelly_rank', `<span class="ff-label">Eff. Kelly rank</span><span class="ff-value ${_ffCellColor('effective_kelly_rank', kelly)}">${kelly.toFixed(5)}</span>`, kelly.toFixed(5))}
        ${_ffCellWrap('regime_label', `<span class="ff-label">Regime</span><span class="ff-value ${_ffCellColor('regime_label', regime)}">${esc(regime)}</span>`, regime)}
        ${Number.isFinite(garchAnn) ? `<div data-ff-metric="garch_ann_vol"><span class="ff-label">GARCH ann. vol</span><span class="ff-value ${_ffCellColor('garch_ann_vol', garchAnn)}">${garchAnn.toFixed(1)}%</span></div>` : ''}
      </div>`;
  };

  const renderSignalsBlock = () => {
    if (!adv) return '';
    return `
      <div class="ff-divider"></div>
      <div class="ff-label" style="text-transform:uppercase;letter-spacing:.04em;font-size:.68rem;">Per-symbol advanced signals</div>
      <div class="ff-signals">
        ${_ffCellWrap('hurst_exponent', `<span class="ff-sig-name">Hurst</span><br><span class="ff-sig-val">${Number(adv.hurst_exponent || 0).toFixed(3)} (${esc(adv.regime_label || 'unknown')})</span>`, `${Number(adv.hurst_exponent || 0).toFixed(3)}`)}
        ${_ffCellWrap('realized_skew', `<span class="ff-sig-name">Realized skew</span><br><span class="ff-sig-val">${Number(adv.realized_skew || 0).toFixed(3)}</span>`, Number(adv.realized_skew || 0).toFixed(3))}
        ${_ffCellWrap('realized_excess_kurt', `<span class="ff-sig-name">Excess kurt</span><br><span class="ff-sig-val">${Number(adv.realized_excess_kurt || 0).toFixed(3)}</span>`, Number(adv.realized_excess_kurt || 0).toFixed(3))}
        ${_ffCellWrap('jump_intensity_per_day', `<span class="ff-sig-name">Jump λ/day</span><br><span class="ff-sig-val">${Number(adv.jump_intensity_per_day || 0).toFixed(4)}</span>`, Number(adv.jump_intensity_per_day || 0).toFixed(4))}
        ${_ffCellWrap('jump_mean_return_pct', `<span class="ff-sig-name">Jump μ%</span><br><span class="ff-sig-val">${Number(adv.jump_mean_return_pct || 0).toFixed(3)}%</span>`, `${Number(adv.jump_mean_return_pct || 0).toFixed(3)}%`)}
        ${_ffCellWrap('ou_half_life_days', `<span class="ff-sig-name">OU half-life</span><br><span class="ff-sig-val">${Number(adv.ou_half_life_days || 0).toFixed(1)}d</span>`, `${Number(adv.ou_half_life_days || 0).toFixed(1)}d`)}
        ${_ffCellWrap('rv_har_sigma_pct', `<span class="ff-sig-name">HAR-RV σ daily</span><br><span class="ff-sig-val">${Number(adv.rv_har_sigma_pct || 0).toFixed(3)}%</span>`, `${Number(adv.rv_har_sigma_pct || 0).toFixed(3)}%`)}
      </div>`;
  };

  // -------------------------------------------------------------------
  // Phase 26.50 — Lab Mode overall prediction summary.
  //
  // Aggregates the 10 lab signals + lab_rank_multiplier into a single
  // headline reading (direction + intensity + rationale).  Surfaces
  // BOTH the standalone reading AND its impact on the main Future
  // Forecast ranking (which depends on whether "Blend Lab into
  // ranking" is on).
  // -------------------------------------------------------------------
  const renderLabSummary = () => {
    if (!lab) return '';
    // Prefer the tier that actually carries the multiplier field — both
    // tiers have it in practice, but be defensive about cold-start rows.
    const blk = (garch && Number.isFinite(Number(garch.lab_rank_multiplier))) ? garch
              : (fast  && Number.isFinite(Number(fast.lab_rank_multiplier)))  ? fast
              : (fast || garch || {});
    const labMult = Number(blk.lab_rank_multiplier);
    const ssaSlope = Number(lab.ssa_trend_slope_pct_per_day || 0);
    const rsvUp = Number(lab.rsv_upside_share || 0.5);
    const stressed = Number(lab.vol_hmm_p_stressed || 0.5);
    const egGamma = Number(lab.egarch_leverage_gamma || 0);
    const dfaAlpha = Number(lab.dfa_alpha || 0.5);
    // Composite direction score [-1..+1]:
    //   * lab_rank_multiplier — overall fusion across all 10 metrics
    //   * SSA slope sign — short-horizon trend
    //   * RSV+ share above 0.5 — recent upside bias
    //   * EGARCH gamma — negative = downside-amplifying leverage
    //   * vol_hmm_p_stressed — caution gate, not directional
    let dirScore = 0;
    if (Number.isFinite(labMult)) {
      dirScore += Math.max(-1, Math.min(1, (labMult - 1) / 0.2));
    }
    if (Number.isFinite(ssaSlope)) {
      dirScore += Math.max(-1, Math.min(1, ssaSlope / 0.4));
    }
    if (Number.isFinite(rsvUp)) {
      dirScore += Math.max(-1, Math.min(1, (rsvUp - 0.5) * 4));
    }
    if (Number.isFinite(egGamma)) {
      dirScore += Math.max(-0.5, Math.min(0.5, -egGamma * 5));
    }
    if (Number.isFinite(dfaAlpha)) {
      dirScore += Math.max(-0.5, Math.min(0.5, (dfaAlpha - 0.5) * 2));
    }
    const dir = dirScore >  0.25 ? { tone: 'bull', label: 'Bullish' }
              : dirScore < -0.25 ? { tone: 'bear', label: 'Bearish' }
              : { tone: 'neutral', label: 'Neutral' };
    const intensity = Math.abs(dirScore) >= 1.5 ? 'extreme'
                    : Math.abs(dirScore) >= 0.9 ? 'strong'
                    : Math.abs(dirScore) >= 0.4 ? 'moderate' : 'weak';
    // Rationale: 1-2 short clauses about why
    const reasons = [];
    if (Number.isFinite(labMult)) {
      if (labMult > 1.05) reasons.push(`lab multiplier ${labMult.toFixed(2)}× boosts rank`);
      else if (labMult < 0.95) reasons.push(`lab multiplier ${labMult.toFixed(2)}× dampens rank`);
    }
    if (Math.abs(ssaSlope) > 0.05) {
      reasons.push(`SSA trend ${ssaSlope > 0 ? '+' : ''}${ssaSlope.toFixed(2)}%/d`);
    }
    if (Math.abs(rsvUp - 0.5) > 0.05) {
      reasons.push(`RSV+ ${(rsvUp*100).toFixed(0)}% upside bias`);
    }
    if (stressed >= 0.65) reasons.push(`vol HMM ${(stressed*100).toFixed(0)}% stressed`);
    if (egGamma <= -0.05) reasons.push('EGARCH leverage (downside-amplifying)');
    const rationale = reasons.length ? reasons.slice(0, 3).join(' · ') : 'signals are balanced';
    // Impact on ranking: blend toggle determines whether it actually affects sort
    const blending = !!state.blendLabIntoRanking;
    const fmActive = !!state.futureMode;
    const multStr = Number.isFinite(labMult) ? `${labMult.toFixed(3)}×` : '—';
    let impactText, impactTone;
    if (!fmActive) {
      impactText = `Enable Future Mode to apply this multiplier (${multStr})`;
      impactTone = 'info';
    } else if (!blending) {
      impactText = `Standalone reading only — turn on "Blend Lab into ranking" to apply ${multStr}`;
      impactTone = 'info';
    } else {
      impactText = `Applying to leaderboard ranking via ${multStr} multiplier`;
      impactTone = Number.isFinite(labMult) && labMult > 1.0 ? 'bull' : labMult < 1.0 ? 'bear' : 'neutral';
    }
    return `
      <div class="ff-tier-summary ff-lab-summary" data-tone="${dir.tone}">
        <div class="ff-tier-summary-head">
          <span class="ff-tier-summary-eyebrow">Overall Lab prediction</span>
          <span class="ff-info-reading-pill tone-${dir.tone}">${dir.label}</span>
          <span class="ff-info-reading-intensity int-${intensity}">${intensity}</span>
        </div>
        <div class="ff-tier-summary-rationale">${esc(rationale)}</div>
        <div class="ff-tier-summary-impact tone-${impactTone}">
          <span class="ff-tier-summary-impact-label">Impact on ranking:</span>
          <span class="ff-tier-summary-impact-text">${esc(impactText)}</span>
        </div>
      </div>`;
  };

  // -------------------------------------------------------------------
  // Phase 26.50 — Strategy Tier overall prediction summary (twin of
  // the Lab summary above).  Uses AR(1), variance-ratios, EMD slope,
  // and strategy_rank_multiplier.
  // -------------------------------------------------------------------
  const renderStrategySummary = () => {
    if (!strategy) return '';
    const blk = (garch && Number.isFinite(Number(garch.strategy_rank_multiplier))) ? garch
              : (fast  && Number.isFinite(Number(fast.strategy_rank_multiplier)))  ? fast
              : (fast || garch || {});
    const stratMult = Number(blk.strategy_rank_multiplier);
    const ar1 = Number(strategy.ar1_coefficient || 0);
    const vr5 = Number(strategy.variance_ratio_5d || 1);
    const vr22 = Number(strategy.variance_ratio_22d || 1);
    const emdSlope = Number(strategy.emd_imf1_slope_pct_per_day || 0);
    const rqaDet = Number(strategy.rqa_determinism_pct || 0);
    const volRegimeMom = Number(strategy.vol_regime_momentum || 0);
    let dirScore = 0;
    if (Number.isFinite(stratMult)) {
      dirScore += Math.max(-1, Math.min(1, (stratMult - 1) / 0.2));
    }
    if (Number.isFinite(ar1))      dirScore += Math.max(-0.6, Math.min(0.6, ar1 * 6));
    if (Number.isFinite(emdSlope)) dirScore += Math.max(-1, Math.min(1, emdSlope / 0.3));
    // VR>1 = trending. Combined with positive AR(1) = bullish; with negative = bearish.
    if (Number.isFinite(vr5) && Number.isFinite(ar1)) {
      const trendSign = (vr5 > 1.05 ? 1 : vr5 < 0.95 ? -1 : 0);
      dirScore += 0.5 * trendSign * Math.sign(ar1);
    }
    // Rising vol regime is a caution signal — drag dirScore toward 0
    if (Number.isFinite(volRegimeMom) && volRegimeMom > 0.3) {
      dirScore *= 0.7;
    }
    const dir = dirScore >  0.25 ? { tone: 'bull', label: 'Bullish' }
              : dirScore < -0.25 ? { tone: 'bear', label: 'Bearish' }
              : { tone: 'neutral', label: 'Neutral' };
    const intensity = Math.abs(dirScore) >= 1.5 ? 'extreme'
                    : Math.abs(dirScore) >= 0.9 ? 'strong'
                    : Math.abs(dirScore) >= 0.4 ? 'moderate' : 'weak';
    const reasons = [];
    if (Number.isFinite(stratMult)) {
      if (stratMult > 1.05) reasons.push(`strategy multiplier ${stratMult.toFixed(2)}× boosts rank`);
      else if (stratMult < 0.95) reasons.push(`strategy multiplier ${stratMult.toFixed(2)}× dampens rank`);
    }
    if (Math.abs(ar1) > 0.05) reasons.push(`AR(1) ${ar1 > 0 ? '+' : ''}${ar1.toFixed(3)}`);
    if (vr5 > 1.1) reasons.push(`VR(5d) ${vr5.toFixed(2)} — trending`);
    else if (vr5 < 0.9) reasons.push(`VR(5d) ${vr5.toFixed(2)} — mean-reverting`);
    if (Math.abs(emdSlope) > 0.1) reasons.push(`EMD slope ${emdSlope > 0 ? '+' : ''}${emdSlope.toFixed(2)}%/d`);
    if (rqaDet > 0.4) reasons.push(`RQA ${(rqaDet*100).toFixed(0)}% deterministic`);
    if (volRegimeMom > 0.3) reasons.push('vol regime rising');
    const rationale = reasons.length ? reasons.slice(0, 3).join(' · ') : 'strategy signals are mixed';
    const blending = !!state.blendStrategyIntoRanking;
    const fmActive = !!state.futureMode;
    const multStr = Number.isFinite(stratMult) ? `${stratMult.toFixed(3)}×` : '—';
    let impactText, impactTone;
    if (!fmActive) {
      impactText = `Enable Future Mode to apply this multiplier (${multStr})`;
      impactTone = 'info';
    } else if (!blending) {
      impactText = `Standalone reading only — turn on "Blend Strategy into ranking" to apply ${multStr}`;
      impactTone = 'info';
    } else {
      impactText = `Applying to leaderboard ranking via ${multStr} multiplier`;
      impactTone = Number.isFinite(stratMult) && stratMult > 1.0 ? 'bull' : stratMult < 1.0 ? 'bear' : 'neutral';
    }
    return `
      <div class="ff-tier-summary ff-strategy-summary" data-tone="${dir.tone}">
        <div class="ff-tier-summary-head">
          <span class="ff-tier-summary-eyebrow">Overall Strategy prediction</span>
          <span class="ff-info-reading-pill tone-${dir.tone}">${dir.label}</span>
          <span class="ff-info-reading-intensity int-${intensity}">${intensity}</span>
        </div>
        <div class="ff-tier-summary-rationale">${esc(rationale)}</div>
        <div class="ff-tier-summary-impact tone-${impactTone}">
          <span class="ff-tier-summary-impact-label">Impact on ranking:</span>
          <span class="ff-tier-summary-impact-text">${esc(impactText)}</span>
        </div>
      </div>`;
  };

  // -------------------------------------------------------------------
  // Phase 26.63 — Predictive Expansion (26.60) overall prediction.
  // Twin of the Lab + Strategy summaries: fuses the 10 standard
  // predictive metrics (+ reality breakers when active) into one
  // directional call and reports its impact on ranking via the 4
  // composite multipliers (Strategy V2 / Regime Risk / ML / Liq Kelly).
  // -------------------------------------------------------------------
  const renderPredictiveSummary = () => {
    if (!predictive) return '';
    const blk = garch || fast || {};
    const dv = (id, value, scale) => _ffDirVote(id, value) * (scale || 1);
    let sumV = 0;
    sumV += dv('msm_drift_premium', predictive.msm_drift_premium, 1.0);
    sumV += dv('trend_curvature_pct', predictive.trend_curvature_pct, 0.9);
    sumV += dv('multiscale_consistency', predictive.multiscale_consistency, 1.0);
    sumV += dv('ml_residual_edge', predictive.ml_residual_edge, 0.9);
    sumV += dv('drawdown_memory_score', predictive.drawdown_memory_score, 0.5);
    if (state.advancedExperimentalMode) {
      sumV += dv('local_causal_cone_signal', predictive.local_causal_cone_signal, 0.6);
      sumV += dv('quantum_path_interference_index', predictive.quantum_path_interference_index, 0.6);
    }
    const score = Math.tanh(sumV / 2.5);
    const qv = (id, value) => _ffQualityVote(id, value);
    const qs = [
      qv('ts_nonlinear_dependence', predictive.ts_nonlinear_dependence),
      qv('lead_lag_influence', predictive.lead_lag_influence),
      qv('liq_adjusted_signal', predictive.liq_adjusted_signal),
      qv('entropy_regime_stability', predictive.entropy_regime_stability),
      qv('volofvol_regime_score', predictive.volofvol_regime_score),
    ].filter((x) => x !== 0);
    const qMean = qs.length ? qs.reduce((a, b) => a + b, 0) / qs.length : 0;
    const conviction = Math.max(0.65, Math.min(1.4, 1 + 0.35 * qMean));
    const dir = _ffScoreToDir(score);
    const intensity = _ffIntensityLabel(Math.abs(score) * conviction);
    const reasons = [];
    const _r = (label, val, unit) => { if (Number.isFinite(val) && Math.abs(val) > 1e-6) reasons.push(`${label} ${val > 0 ? '+' : ''}${Number(val).toFixed(3)}${unit || ''}`); };
    _r('MSM drift', predictive.msm_drift_premium, '%/d');
    _r('curvature', predictive.trend_curvature_pct);
    _r('multi-scale', predictive.multiscale_consistency);
    _r('ML edge', predictive.ml_residual_edge, 'σ');
    const rationale = reasons.length ? reasons.slice(0, 3).join(' · ') : 'predictive signals are balanced';
    const activeMults = [];
    if (state.useStrategyV2Mode && state.blendStrategyV2IntoRanking && Number.isFinite(blk.strategy_v2_rank_multiplier)) activeMults.push(`SV2 ${Number(blk.strategy_v2_rank_multiplier).toFixed(2)}×`);
    if (state.useRegimeRiskMode && state.blendRegimeRiskIntoRanking && Number.isFinite(blk.regime_risk_multiplier)) activeMults.push(`Regime ${Number(blk.regime_risk_multiplier).toFixed(2)}×`);
    if (state.useMlOverlayMode && state.blendMlOverlayIntoRanking && Number.isFinite(blk.ml_rank_multiplier)) activeMults.push(`ML ${Number(blk.ml_rank_multiplier).toFixed(2)}×`);
    if (state.blendLiqKellyFactor && Number.isFinite(blk.liq_kelly_factor)) activeMults.push(`LiqK ${Number(blk.liq_kelly_factor).toFixed(2)}×`);
    let impactText, impactTone;
    if (!state.futureMode) { impactText = 'Enable Future Mode to apply predictive multipliers'; impactTone = 'info'; }
    else if (!activeMults.length) { impactText = 'Standalone reading only — turn on a 26.60 blend toggle to apply'; impactTone = 'info'; }
    else { impactText = `Applying to ranking via ${activeMults.join(' · ')}`; impactTone = dir.tone; }
    return `
      <div class="ff-tier-summary ff-predictive-summary" data-tone="${dir.tone}">
        <div class="ff-tier-summary-head">
          <span class="ff-tier-summary-eyebrow">Overall Predictive prediction</span>
          <span class="ff-info-reading-pill tone-${dir.tone}">${dir.label}</span>
          <span class="ff-info-reading-intensity int-${intensity}">${intensity}</span>
        </div>
        <div class="ff-tier-summary-rationale">${esc(rationale)}</div>
        <div class="ff-tier-summary-impact tone-${impactTone}">
          <span class="ff-tier-summary-impact-label">Impact on ranking:</span>
          <span class="ff-tier-summary-impact-text">${esc(impactText)}</span>
        </div>
      </div>`;
  };

  const renderLabBlock = () => {
    if (!state.useLabMode || !lab) return '';
    return `
      <div class="ff-lab-section">
        <h4>Experimental signals (Lab Mode)</h4>
        ${renderLabSummary()}
        <div class="ff-lab-grid">
          ${_ffCellWrap('rsv_upside_share', `<span class="ff-label">RSV+ share</span><span class="ff-value ${_ffCellColor('rsv_upside_share', Number(lab.rsv_upside_share || 0.5))}">${(Number(lab.rsv_upside_share || 0.5)*100).toFixed(1)}%</span>`, `${(Number(lab.rsv_upside_share || 0.5)*100).toFixed(1)}%`)}
          ${_ffCellWrap('egarch_leverage_gamma', `<span class="ff-label">EGARCH γ</span><span class="ff-value ${_ffCellColor('egarch_leverage_gamma', Number(lab.egarch_leverage_gamma || 0))}">${Number(lab.egarch_leverage_gamma || 0).toFixed(4)}</span>`, Number(lab.egarch_leverage_gamma || 0).toFixed(4))}
          ${_ffCellWrap('garch_m_premium_bps_per_sigma', `<span class="ff-label">GARCH-M bps/σ</span><span class="ff-value ${_ffCellColor('garch_m_premium_bps_per_sigma', Number(lab.garch_m_premium_bps_per_sigma || 0))}">${Number(lab.garch_m_premium_bps_per_sigma || 0).toFixed(1)}</span>`, Number(lab.garch_m_premium_bps_per_sigma || 0).toFixed(1))}
          ${_ffCellWrap('permutation_entropy', `<span class="ff-label">Perm entropy</span><span class="ff-value ${_ffCellColor('permutation_entropy', Number(lab.permutation_entropy || 1))}">${Number(lab.permutation_entropy || 1).toFixed(3)}</span>`, Number(lab.permutation_entropy || 1).toFixed(3))}
          ${_ffCellWrap('approximate_entropy', `<span class="ff-label">ApEn</span><span class="ff-value ${_ffCellColor('approximate_entropy', Number(lab.approximate_entropy || 0))}">${Number(lab.approximate_entropy || 0).toFixed(3)}</span>`, Number(lab.approximate_entropy || 0).toFixed(3))}
          ${_ffCellWrap('mahalanobis_outlier_z', `<span class="ff-label">Mahal. z</span><span class="ff-value ${_ffCellColor('mahalanobis_outlier_z', Number(lab.mahalanobis_outlier_z || 0))}">${Number(lab.mahalanobis_outlier_z || 0).toFixed(2)}</span>`, Number(lab.mahalanobis_outlier_z || 0).toFixed(2))}
          ${_ffCellWrap('dfa_alpha', `<span class="ff-label">DFA α</span><span class="ff-value ${_ffCellColor('dfa_alpha', Number(lab.dfa_alpha || 0.5))}">${Number(lab.dfa_alpha || 0.5).toFixed(3)}</span>`, Number(lab.dfa_alpha || 0.5).toFixed(3))}
          ${_ffCellWrap('ssa_trend_slope_pct_per_day', `<span class="ff-label">SSA slope</span><span class="ff-value ${_ffCellColor('ssa_trend_slope_pct_per_day', Number(lab.ssa_trend_slope_pct_per_day || 0))}">${Number(lab.ssa_trend_slope_pct_per_day || 0).toFixed(3)}%/d</span>`, `${Number(lab.ssa_trend_slope_pct_per_day || 0).toFixed(3)}%/d`)}
          ${_ffCellWrap('vol_hmm_p_stressed', `<span class="ff-label">Vol HMM stressed</span><span class="ff-value ${_ffCellColor('vol_hmm_p_stressed', Number(lab.vol_hmm_p_stressed || 0.5))}">${(Number(lab.vol_hmm_p_stressed || 0.5)*100).toFixed(0)}%</span>`, `${(Number(lab.vol_hmm_p_stressed || 0.5)*100).toFixed(0)}%`)}
          ${_ffCellWrap('vol_hmm_p_stay_stressed', `<span class="ff-label">Stay stressed</span><span class="ff-value ${_ffCellColor('vol_hmm_p_stay_stressed', Number(lab.vol_hmm_p_stay_stressed || 0.5))}">${(Number(lab.vol_hmm_p_stay_stressed || 0.5)*100).toFixed(0)}%</span>`, `${(Number(lab.vol_hmm_p_stay_stressed || 0.5)*100).toFixed(0)}%`)}
          ${(fast || garch) ? _ffCellWrap('lab_qi_certainty', `<span class="ff-label">QI certainty</span><span class="ff-value ${_ffCellColor('lab_qi_certainty', Number((fast || garch).lab_qi_certainty || 0))}">${(Number((fast || garch).lab_qi_certainty || 0)*100).toFixed(1)}%</span>`, `${(Number((fast || garch).lab_qi_certainty || 0)*100).toFixed(1)}%`) : ''}
          ${(fast || garch) ? _ffCellWrap('lab_rank_multiplier', `<span class="ff-label">Lab multiplier</span><span class="ff-value ${_ffCellColor('lab_rank_multiplier', Number((fast || garch).lab_rank_multiplier || 1))}">${Number((fast || garch).lab_rank_multiplier || 1).toFixed(3)}×</span>`, Number((fast || garch).lab_rank_multiplier || 1).toFixed(3)) : ''}
        </div>
      </div>`;
  };

  // Phase 26.50 — Strategy Tier section.  Only shown when the user
  // toggles "Strategy Tier" on.  Renders the 10 predictive metrics
  // + the composite multiplier.
  const renderStrategyBlock = () => {
    if (!state.useStrategyMode || !strategy) return '';
    const sBlock = fast || garch || {};
    return `
      <div class="ff-strategy-section">
        <h4>Strategy Tier (predictive)</h4>
        ${renderStrategySummary()}
        <div class="ff-strategy-grid">
          ${_ffCellWrap('strategy_vr5', `<span class="ff-label">VR(5d)</span><span class="ff-value ${_ffCellColor('strategy_vr5', Number(strategy.variance_ratio_5d || 1))}">${Number(strategy.variance_ratio_5d || 1).toFixed(3)}</span>`, Number(strategy.variance_ratio_5d || 1).toFixed(3))}
          ${_ffCellWrap('strategy_vr22', `<span class="ff-label">VR(22d)</span><span class="ff-value ${_ffCellColor('strategy_vr22', Number(strategy.variance_ratio_22d || 1))}">${Number(strategy.variance_ratio_22d || 1).toFixed(3)}</span>`, Number(strategy.variance_ratio_22d || 1).toFixed(3))}
          ${_ffCellWrap('strategy_ar1', `<span class="ff-label">AR(1)</span><span class="ff-value ${_ffCellColor('strategy_ar1', Number(strategy.ar1_coefficient || 0))}">${Number(strategy.ar1_coefficient || 0).toFixed(4)}</span>`, Number(strategy.ar1_coefficient || 0).toFixed(4))}
          ${_ffCellWrap('strategy_mi_lag1', `<span class="ff-label">MI lag-1</span><span class="ff-value ${_ffCellColor('strategy_mi_lag1', Number(strategy.mutual_information_lag1 || 0))}">${Number(strategy.mutual_information_lag1 || 0).toFixed(4)} bits</span>`, `${Number(strategy.mutual_information_lag1 || 0).toFixed(4)} bits`)}
          ${_ffCellWrap('strategy_spectral_beta', `<span class="ff-label">Spectral β</span><span class="ff-value ${_ffCellColor('strategy_spectral_beta', Number(strategy.spectral_slope_beta || 0))}">${Number(strategy.spectral_slope_beta || 0).toFixed(3)}</span>`, Number(strategy.spectral_slope_beta || 0).toFixed(3))}
          ${_ffCellWrap('strategy_welch_cycle_days', `<span class="ff-label">Welch cycle</span><span class="ff-value ${_ffCellColor('strategy_welch_cycle_days', Number(strategy.welch_dominant_cycle_days || 0))}">${Number(strategy.welch_dominant_cycle_days || 0).toFixed(1)}d</span>`, `${Number(strategy.welch_dominant_cycle_days || 0).toFixed(1)}d`)}
          ${_ffCellWrap('strategy_rqa_determinism', `<span class="ff-label">RQA determ.</span><span class="ff-value ${_ffCellColor('strategy_rqa_determinism', Number(strategy.rqa_determinism_pct || 0))}">${(Number(strategy.rqa_determinism_pct || 0)*100).toFixed(1)}%</span>`, `${(Number(strategy.rqa_determinism_pct || 0)*100).toFixed(1)}%`)}
          ${_ffCellWrap('strategy_lz_complexity', `<span class="ff-label">LZ complexity</span><span class="ff-value ${_ffCellColor('strategy_lz_complexity', Number(strategy.lempel_ziv_complexity || 0))}">${Number(strategy.lempel_ziv_complexity || 0).toFixed(3)}</span>`, Number(strategy.lempel_ziv_complexity || 0).toFixed(3))}
          ${_ffCellWrap('strategy_emd_slope_pct', `<span class="ff-label">EMD slope</span><span class="ff-value ${_ffCellColor('strategy_emd_slope_pct', Number(strategy.emd_imf1_slope_pct_per_day || 0))}">${Number(strategy.emd_imf1_slope_pct_per_day || 0).toFixed(3)}%/d</span>`, `${Number(strategy.emd_imf1_slope_pct_per_day || 0).toFixed(3)}%/d`)}
          ${_ffCellWrap('strategy_vol_regime_mom', `<span class="ff-label">Vol regime mom.</span><span class="ff-value ${_ffCellColor('strategy_vol_regime_mom', Number(strategy.vol_regime_momentum || 0))}">${Number(strategy.vol_regime_momentum || 0).toFixed(3)}</span>`, Number(strategy.vol_regime_momentum || 0).toFixed(3))}
          ${Number.isFinite(sBlock.strategy_rank_multiplier) ? _ffCellWrap('strategy_rank_multiplier', `<span class="ff-label">Strategy mult.</span><span class="ff-value ${_ffCellColor('strategy_rank_multiplier', Number(sBlock.strategy_rank_multiplier))}">${Number(sBlock.strategy_rank_multiplier).toFixed(3)}×</span>`, Number(sBlock.strategy_rank_multiplier).toFixed(3)) : ''}
        </div>
      </div>`;
  };

  // =================================================================
  // Phase 26.60 — Predictive Expansion section.
  //
  // Shown when ANY of the four parent toggles (Strategy V2 / Regime
  // Risk / ML Overlay / Liquidity Kelly) is on.  Renders:
  //   * Standard 10 metrics with consistent tone/intensity chips
  //   * 5 composite multipliers (whether blended or not — they're
  //     always informational; blend toggles control rank-only)
  //   * 4 reality_breaker overlays ONLY when Advanced Experimental
  //     Mode is ON AND the per-overlay toggle is ON.  Each carries
  //     the `experimental+` warning badge per spec.
  // =================================================================
  const renderPredictiveBlock = () => {
    const anyOn = !!(state.useStrategyV2Mode || state.useRegimeRiskMode
                  || state.useMlOverlayMode || state.blendLiqKellyFactor
                  || state.advancedExperimentalMode);
    if (!anyOn || !predictive) return '';
    const pBlock = fast || garch || {};
    const _pct = (v) => Number.isFinite(v) ? (v * 100).toFixed(1) + '%' : '—';
    const _num = (v, d = 4) => Number.isFinite(v) ? Number(v).toFixed(d) : '—';
    const _mult = (v) => Number.isFinite(v) ? Number(v).toFixed(3) + '×' : '—';
    // Standard section — always render when any 26.60 parent is on.
    const standardGrid = `
      <div class="ff-strategy-grid">
        ${_ffCellWrap('msm_drift_premium',          `<span class="ff-label">MSM drift premium</span><span class="ff-value ${_ffCellColor('msm_drift_premium', predictive.msm_drift_premium)}">${_num(predictive.msm_drift_premium, 3)}%/d</span>`, `${_num(predictive.msm_drift_premium, 3)}%/d`)}
        ${_ffCellWrap('ts_nonlinear_dependence',    `<span class="ff-label">Nonlinear dep.</span><span class="ff-value ${_ffCellColor('ts_nonlinear_dependence', predictive.ts_nonlinear_dependence)}">${_pct(predictive.ts_nonlinear_dependence)}</span>`, _pct(predictive.ts_nonlinear_dependence))}
        ${_ffCellWrap('trend_curvature_pct',        `<span class="ff-label">Trend curvature</span><span class="ff-value ${_ffCellColor('trend_curvature_pct', predictive.trend_curvature_pct)}">${_num(predictive.trend_curvature_pct, 4)}%/s²</span>`, `${_num(predictive.trend_curvature_pct, 4)}%/s²`)}
        ${_ffCellWrap('lead_lag_influence',         `<span class="ff-label">Lead-lag influence</span><span class="ff-value ${_ffCellColor('lead_lag_influence', predictive.lead_lag_influence)}">${_pct(predictive.lead_lag_influence)}</span>`, _pct(predictive.lead_lag_influence))}
        ${_ffCellWrap('volofvol_regime_score',      `<span class="ff-label">Vol-of-vol regime</span><span class="ff-value ${_ffCellColor('volofvol_regime_score', predictive.volofvol_regime_score)}">${_pct(predictive.volofvol_regime_score)}</span>`, _pct(predictive.volofvol_regime_score))}
        ${_ffCellWrap('multiscale_consistency',     `<span class="ff-label">Multi-scale consistency</span><span class="ff-value ${_ffCellColor('multiscale_consistency', predictive.multiscale_consistency)}">${_num(predictive.multiscale_consistency, 3)}</span>`, _num(predictive.multiscale_consistency, 3))}
        ${_ffCellWrap('entropy_regime_stability',   `<span class="ff-label">Regime stability</span><span class="ff-value ${_ffCellColor('entropy_regime_stability', predictive.entropy_regime_stability)}">${_pct(predictive.entropy_regime_stability)}</span>`, _pct(predictive.entropy_regime_stability))}
        ${_ffCellWrap('drawdown_memory_score',      `<span class="ff-label">Drawdown memory</span><span class="ff-value ${_ffCellColor('drawdown_memory_score', predictive.drawdown_memory_score)}">${_num(predictive.drawdown_memory_score, 3)}%</span>`, `${_num(predictive.drawdown_memory_score, 3)}%`)}
        ${_ffCellWrap('liq_adjusted_signal',        `<span class="ff-label">Liq-adj. predictability</span><span class="ff-value ${_ffCellColor('liq_adjusted_signal', predictive.liq_adjusted_signal)}">${_pct(predictive.liq_adjusted_signal)}</span>`, _pct(predictive.liq_adjusted_signal))}
        ${_ffCellWrap('ml_residual_edge',           `<span class="ff-label">ML residual edge</span><span class="ff-value ${_ffCellColor('ml_residual_edge', predictive.ml_residual_edge)}">${_num(predictive.ml_residual_edge, 3)}σ</span>`, `${_num(predictive.ml_residual_edge, 3)}σ`)}
      </div>
    `;

    // Composite multipliers — always visible when section is open.
    const sv2Active = !!(state.useStrategyV2Mode && state.blendStrategyV2IntoRanking);
    const regRiskActive = !!(state.useRegimeRiskMode && state.blendRegimeRiskIntoRanking);
    const mlActive = !!(state.useMlOverlayMode && state.blendMlOverlayIntoRanking);
    const liqActive = !!state.blendLiqKellyFactor;
    const _multCell = (key, label, val, active) => {
      const blendedTag = active ? '<sup style="color:#34d399;font-size:.55em;letter-spacing:.05em">blend</sup>'
                                : '<sup style="color:#94a3b8;font-size:.55em;letter-spacing:.05em">info</sup>';
      return _ffCellWrap(key,
        `<span class="ff-label">${label}</span><span class="ff-value ${_ffCellColor(key, val)}">${_mult(val)}${blendedTag}</span>`,
        _mult(val));
    };
    const multipliersGrid = `
      <div class="ff-strategy-grid" style="margin-top:8px">
        ${_multCell('strategy_v2_rank_multiplier', 'Strategy V2 ×',  pBlock.strategy_v2_rank_multiplier, sv2Active)}
        ${_multCell('regime_risk_multiplier',      'Regime Risk ×',  pBlock.regime_risk_multiplier,      regRiskActive)}
        ${_multCell('ml_rank_multiplier',          'ML Overlay ×',   pBlock.ml_rank_multiplier,          mlActive)}
        ${_multCell('liq_kelly_factor',            'Liq Kelly ×',    pBlock.liq_kelly_factor,            liqActive)}
      </div>
    `;

    // Reality_breaker subsection — ONLY when Advanced Experimental Mode
    // is ON and at least one child overlay is ON.
    let realityBreakerSection = '';
    const advOn = !!state.advancedExperimentalMode;
    if (advOn) {
      const showLcc  = !!state.showLocalCausalCone;
      const showQpii = !!state.showQuantumPathInterference;
      const showLlve = !!state.showLocalLyapunov;
      const showTrs  = !!state.showTemporalRenormalization;
      const showRbMult = !!state.blendRealityBreakerIntoRanking;
      const anyChild = showLcc || showQpii || showLlve || showTrs || showRbMult;
      if (anyChild) {
        const _rb = (key, label, value, on) => on
          ? _ffCellWrap(key,
              `<span class="ff-label">${label} <sup style="color:#fca5a5;font-size:.55em">exp+</sup></span><span class="ff-value ${_ffCellColor(key, value)}">${_num(value, 4)}</span>`,
              _num(value, 4))
          : '';
        realityBreakerSection = `
          <div class="ff-strategy-section" style="border:1px solid #7c1d28;border-radius:6px;padding:8px;margin-top:10px;background:rgba(124,29,40,0.08)">
            <h4 style="color:#ffb4bc;margin:0 0 6px 0">
              Reality-Breaker overlays <span class="rb-badge" style="background:#7c1d28;color:#ffd9dd;padding:1px 6px;border-radius:4px;font-size:.7em;margin-left:4px">experimental+</span>
            </h4>
            <div style="color:#ffb4bc;font-size:.8em;margin-bottom:6px">
              These overlays are CLAMPED and OPT-IN.  Never use as the sole signal for live trading.
            </div>
            <div class="rb-help-note" data-testid="rb-blend-help" style="color:#e7c6ce;font-size:.75em;margin-bottom:6px;font-style:italic;border-left:2px solid #7c1d28;padding-left:6px">
              Reality-Breaker blend composite score &mdash; best read: the closer this score is to 1.0 or higher, the stronger and more accurate the positive forecast. The farther it falls below 0.99, the weaker and more bearish the forecast becomes.
            </div>
            <div class="ff-strategy-grid">
              ${_rb('local_causal_cone_signal',           'LCC (σ)',  predictive.local_causal_cone_signal,           showLcc)}
              ${_rb('quantum_path_interference_index',   'QPII',     predictive.quantum_path_interference_index,    showQpii)}
              ${_rb('local_lyapunov_volatility_exponent','LLVE',     predictive.local_lyapunov_volatility_exponent, showLlve)}
              ${_rb('temporal_renormalization_score',    'TRS',      predictive.temporal_renormalization_score,     showTrs)}
              ${showRbMult ? _ffCellWrap('reality_breaker_multiplier',
                  `<span class="ff-label">Reality-Breaker × <sup style="color:#fca5a5;font-size:.55em">blend</sup></span><span class="ff-value ${_ffCellColor('reality_breaker_multiplier', pBlock.reality_breaker_multiplier)}">${_mult(pBlock.reality_breaker_multiplier)}</span>`,
                  _mult(pBlock.reality_breaker_multiplier)) : ''}
            </div>
          </div>`;
      }
    }

    return `
      <div class="ff-strategy-section">
        <h4>Predictive Expansion (Phase 26.60)</h4>
        ${renderPredictiveSummary()}
        ${standardGrid}
        ${multipliersGrid}
        ${realityBreakerSection}
      </div>`;
  };

  // Phase 26.63 — Overall blended forecast.
  //
  // Fuses EVERY active section (base Cornish-Fisher core + Lab +
  // Strategy + Predictive Expansion 26.60 + Reality-Breaker overlays
  // when active) into a single conviction-weighted directional call.
  // Shown whenever a forecast tier exists so the user always has a
  // top-of-cell holistic read.  Quality signals (predictability,
  // stability, liquidity, tail-risk, chaos) modulate the conviction
  // band rather than the direction.
  let overallBanner = '';
  {
    const blk = garch || fast || null;
    if (blk) {
      const bf = _ffBlendedForecast(blk, lab, strategy, predictive);
      const secTags = [];
      if (bf.sections.labOn) secTags.push('Lab');
      if (bf.sections.stratOn) secTags.push('Strategy');
      if (bf.sections.predOn) secTags.push('Predictive');
      if (bf.sections.rbOn) secTags.push('Reality-Breaker');
      const sectionsStr = secTags.length ? ` + ${secTags.join(' + ')}` : '';
      const convStr = bf.conviction >= 1.08 ? 'high-conviction tape'
                    : bf.conviction <= 0.92 ? 'low-conviction tape' : 'balanced tape';
      overallBanner = `
        <div class="ff-overall-banner" data-tone="${bf.dir.tone}" data-testid="ff-overall-banner">
          <div class="ff-overall-head">
            <span class="ff-overall-eyebrow">Overall blended forecast</span>
            <span class="ff-info-reading-pill tone-${bf.dir.tone}">${bf.dir.label}</span>
            <span class="ff-info-reading-intensity int-${bf.intensity}">${bf.intensity}</span>
          </div>
          <div class="ff-overall-rationale">${esc(bf.reasons.join(' · ') || 'signals balanced')} <span style="opacity:.6">· ${bf.n_signals} signals${sectionsStr} · ${convStr}</span></div>
        </div>`;
    }
  }

  return `
    <div class="future-forecast-card" data-testid="future-forecast-card" data-symbol="${esc(symbol)}">
      <div class="ff-header">
        <h3>Future Forecast</h3>
        <button type="button" class="ff-deep-refresh" data-testid="ff-deep-refresh-btn" data-symbol="${esc(symbol)}" title="Force a deep refresh (fresh provider pull + GARCH overlay) for this symbol">
          ↻ Deep Refresh
        </button>
      </div>
      ${overallBanner}
      ${garch ? renderTierBlock(garch, 'GARCH tier', 'garch') : ''}
      ${garch && fast ? '<div class="ff-divider"></div>' : ''}
      ${fast ? renderTierBlock(fast, 'Fast tier (ATR-σ)', '') : ''}
      ${renderSignalsBlock()}
      ${renderLabBlock()}
      ${renderStrategyBlock()}
      ${renderPredictiveBlock()}
    </div>`;
}

// =========================================================================
// Scanner-context forecast card — shows HOW short selling pressure,
// predicted volume intensity and the nearest options expiration changed
// the forward outlook (not just the changed final numbers), plus any
// reliability caveats tied to proxy/degraded inputs.
// =========================================================================
function renderForecastContextCard(detail) {
  if (!detail) return '';
  const fm = detail.forward_metrics_garch || detail.forward_metrics;
  const ctx = (fm && fm.forecast_context) || null;
  if (!ctx) return '';
  const sp = ctx.short_pressure_effect || {};
  const vi = ctx.volume_intensity_effect || {};
  const ex = ctx.expiration_effect || {};
  const expl = (ctx.explanations || []).map((e) => `<li>${esc(e)}</li>`).join('');
  const relCaveat = ctx.reliability === 'reduced'
    ? '<div class="fc-caveat" data-testid="forecast-reliability-caveat">\u26a0 Reduced-confidence forecast — one or more inputs came from proxy/partial data rather than live sources.</div>'
    : '';
  return `
    <div class="forecast-context-card" data-testid="forecast-context-card">
      <h3 class="section-title">Forecast context \u2014 short pressure / volume intensity / expiration</h3>
      <div class="fc-grid">
        <div class="fc-cell" title="How short selling pressure shifted the forward outlook">
          <div class="eyebrow">Short pressure</div>
          <div class="fc-value">${Number(sp.score ?? 50).toFixed(0)} \u00b7 ${String(sp.label || 'neutral').replace(/_/g, ' ')}</div>
          <div class="fc-meta">P(up) shift ${sp.p_up_shift > 0 ? '+' : ''}${((sp.p_up_shift || 0) * 100).toFixed(1)}pp \u00b7 source ${sp.source || 'n/a'}</div>
        </div>
        <div class="fc-cell" title="How predicted volume intensity shaped event probabilities">
          <div class="eyebrow">Volume intensity</div>
          <div class="fc-value">${Number(vi.score ?? 0).toFixed(0)} \u00b7 ${vi.bucket || 'low'}</div>
          <div class="fc-meta">${vi.event_flag ? 'High-volume event likely' : 'No event flag'}</div>
        </div>
        <div class="fc-cell" title="How the nearest options expiration modulated confidence">
          <div class="eyebrow">Options expiration</div>
          <div class="fc-value">${ex.days_to_expiration != null ? `${ex.days_to_expiration}d away` : 'n/a'}</div>
          <div class="fc-meta">${ex.high_sensitivity_window ? 'High-sensitivity window' : 'Outside sensitivity window'}${ex.risk_flag ? ' \u00b7 \u26a1 risk' : ''} \u00b7 confidence \u00d7${Number(ctx.confidence_modifier ?? 1).toFixed(2)}</div>
        </div>
        <div class="fc-cell" title="Event probabilities derived from the combined context">
          <div class="eyebrow">Event probabilities</div>
          <div class="fc-value">Squeeze ${(Number(ctx.squeeze_probability || 0) * 100).toFixed(0)}%</div>
          <div class="fc-meta">Volatility event ${(Number(ctx.volatility_event_probability || 0) * 100).toFixed(0)}%</div>
        </div>
      </div>
      ${expl ? `<ul class="fc-expl">${expl}</ul>` : ''}
      ${relCaveat}
    </div>`;
}

function filteredMainRows() {
  const marketMap = activeMarketMap();
  return Array.from(marketMap.values())
    .filter(rowMatchesFilters)
    .filter(passesFutureFilter)   // Phase 26.49 — drop bulls/bears that don't match the filter
    .filter(passesRealityBreakerFilter)   // Phase 26.65 — RB overall-rating bucket filter
    .filter(passesConsensusFilter);        // Phase 26.68 — 7-timeframe consensus quick-filter
}

// Phase 26.68 — 7-timeframe consensus quick-filter.  Surfaces rows where
// a strong majority (or all) of the experimental composite timeframes
// agree in one direction.
function passesConsensusFilter(row) {
  const sel = state.consensusFilter || 'all';
  if (sel === 'all') return true;
  const c = _rowConsensusCounts(row);
  if (!c.has) return false;
  if (sel === 'up6') return c.up >= 6;
  if (sel === 'up7') return c.up >= 7;
  if (sel === 'down6') return c.down >= 6;
  if (sel === 'down7') return c.down >= 7;
  return true;
}

// Phase 26.65 — "Bull × Bull" priority bit: 1 when the row's classical
// Direction AND its forward F-DIR are BOTH Bullish.  Used as the primary
// sort key when state.bullBullPriority is on so these float to the top.
function bullBullPriorityBit(row) {
  if ((row.final_direction || '') !== 'Bullish') return 0;
  const fm = futureMetricsForRow(row);
  if (!fm) return 0;
  const fdir = effectiveFutureDirection(fm).dir;
  return fdir === 'Bullish' ? 1 : 0;
}

// Client-side sort accessors for the scanner-context sort_by modes.
const _CONTEXT_SORT_KEYS = {
  predicted_volume_intensity: (r) => Number(r.predicted_volume_intensity_score || 0),
  short_selling_pressure: (r) => Number(r.short_selling_pressure_score ?? 50),
  // Negated: nearest expirations sort to the top under descending order.
  days_to_options_expiration: (r) => -(r.days_to_options_expiration == null ? 999 : Number(r.days_to_options_expiration)),
};

function allSortedRows() {
  const key = _filterSortKey();
  if (key === _filterSortCacheKey && _filterSortCacheValue) {
    return _filterSortCacheValue;
  }
  const bbp = !!state.bullBullPriority;
  const cs = state.consensusSort;
  const ctxSortFn = _CONTEXT_SORT_KEYS[state.filters.sort_by] || null;
  const sorted = filteredMainRows().sort((a, b) => {
    // Predicted-volume-first ordering: explicit ranking stage that runs
    // BEFORE all secondary sort preferences, so likely upcoming
    // high-volume names surface first and filters refine that set.
    if (state.pviPriority) {
      const d = Number(b.predicted_volume_intensity_score || 0) - Number(a.predicted_volume_intensity_score || 0);
      if (d !== 0) return d;
    }
    if (ctxSortFn) {
      const d = ctxSortFn(b) - ctxSortFn(a);
      if (d !== 0) return d;
    }
    if (cs === 'desc' || cs === 'asc') {   // Phase 26.68 — sort by net consensus
      const d = _rowConsensusCounts(b).net - _rowConsensusCounts(a).net;
      if (d !== 0) return cs === 'desc' ? d : -d;
    }
    if (bbp) {
      const pb = bullBullPriorityBit(b) - bullBullPriorityBit(a);
      if (pb !== 0) return pb;   // Bull×Bull rows first, then rank within groups
    }
    return rankScoreForRow(b) - rankScoreForRow(a);
  });
  _filterSortCacheKey = key;
  _filterSortCacheValue = sorted;
  return sorted;
}

function topTenPageRows() {
  return allSortedRows().slice(0, state.activeScanLimit);
}

function rebuildActiveScanPool() {
  const topRows = topTenPageRows();
  const nextSymbols = new Set(topRows.map((row) => row.symbol).filter(Boolean));
  const nextPool = new Map();
  for (const row of topRows) {
    const existing = state.activeScanPool.get(row.symbol) || {};
    nextPool.set(row.symbol, {
      ...existing,
      ...row,
      active_scan_enabled: true,
      active_scan_cycles: existing.active_scan_cycles || row.active_scan_cycles || 0,
      active_scan_last_seen_top_10_pages_utc: row.as_of_utc || existing.active_scan_last_seen_top_10_pages_utc || new Date().toISOString(),
      as_of_utc: row.as_of_utc || existing.as_of_utc || '',
      age_seconds: Number(row.age_seconds ?? existing.age_seconds ?? 0),
      freshness_label: row.freshness_label || existing.freshness_label || 'unknown',
      active_scan_last_refresh_utc: existing.active_scan_last_refresh_utc || row.active_scan_last_refresh_utc || '',
      score_revision_utc: existing.score_revision_utc || row.score_revision_utc || '',
      fresh_rescore: existing.fresh_rescore || row.fresh_rescore || false,
      active_scan_removed_reason: '',
    });
  }
  state.activeScanPool = nextPool;
  state.activeScanUniverseSymbols = nextSymbols;
  state.marketActivePools[state.currentMarket] = nextPool;
  state.marketActiveSymbols[state.currentMarket] = nextSymbols;
}

function updateTop25Tracker() {
  const topRows = allSortedRows().slice(0, 25);
  const currentTopSymbols = new Set();
  const nextTrackedMap = new Map();
  for (const row of topRows) {
    if (!row?.symbol || currentTopSymbols.has(row.symbol)) continue;
    currentTopSymbols.add(row.symbol);
    const passMap = activePassCountMap();
    const trackedMap = activeTrackedMap();
    const count = (passMap.get(row.symbol) || 0) + 1;
    passMap.set(row.symbol, count);
    if (count >= 2) {
      const existing = trackedMap.get(row.symbol) || {};
      nextTrackedMap.set(row.symbol, {
        ...existing,
        ...row,
        persistence_passes: count,
        tracked_since_utc: existing.tracked_since_utc || row.as_of_utc || new Date().toISOString(),
      });
    }
  }
  state.trackedRowsMap = nextTrackedMap;
  state.marketTrackedRows[state.currentMarket] = nextTrackedMap;
}

function trackedSortedRows() {
  state.trackedRowsMap = activeTrackedMap();
  return Array.from(state.trackedRowsMap.values()).sort((a, b) => {
    const passDiff = (b.persistence_passes ?? 0) - (a.persistence_passes ?? 0);
    if (passDiff !== 0) return passDiff;
    // Phase 26.40 + 26.42: tracked view also honors the active ranking engine.
    return rankScoreForRow(b) - rankScoreForRow(a);
  });
}

function activeRows() {
  return state.currentView === 'tracked' ? trackedSortedRows() : allSortedRows();
}

function pageCount() {
  return Math.max(1, Math.ceil(activeRows().length / state.pageSize));
}

function visibleRows() {
  const rows = activeRows();
  const start = state.pageIndex * state.pageSize;
  return rows.slice(start, start + state.pageSize);
}

function goToPage(index) {
  state.pageIndex = Math.max(0, Math.min(pageCount() - 1, index));
  renderResults();
}

function setView(viewName) {
  state.currentView = viewName;
  state.pageIndex = 0;
  // Phase 26.39: view switches clear the live-tick timer.  The detail
  // panel's content remains visible but the auto-refresh stops because
  // the user has navigated away.  A subsequent row click in the new
  // view will re-arm the timer via loadDetail().
  stopDetailLiveRefresh();
  const isTracked = viewName === 'tracked';
  if (byId('viewMainButton')) byId('viewMainButton').classList.toggle('is-active', !isTracked && state.currentMarket === 'stocks');
  if (byId('viewCryptoButton')) byId('viewCryptoButton').classList.toggle('is-active', !isTracked && state.currentMarket === 'crypto');
  if (byId('viewTrackedButton')) byId('viewTrackedButton').classList.toggle('is-active', isTracked);
  if (byId('viewTitle')) byId('viewTitle').textContent = isTracked ? 'Tracked top 25 survivors' : (state.currentMarket === 'crypto' ? 'Crypto market dashboard' : 'Market refinement dashboard');
  if (byId('tableTitle')) byId('tableTitle').textContent = isTracked ? 'Top 25 persistence list' : (state.currentMarket === 'crypto' ? 'Crypto ranked results' : 'Ranked results');
  saveUiPrefs();
  renderResults();
}

function jumpToSymbol(symbol) {
  if (state.currentView === 'tracked' && !state.trackedRowsMap.has(symbol)) setView('main');
  let rows = activeRows();
  let index = rows.findIndex((row) => row.symbol === symbol);
  if (index < 0) {
    // Phase 26.9: symbol exists in the raw market map but is being filtered
    // out. The user explicitly asked for THIS symbol, so dropping them
    // back to "all rows" is the right behavior. Relax min_score and
    // max_exit_risk (the two filters most likely to exclude lower-tier
    // names), notify the user, and try again.
    const marketMap = (typeof activeMarketMap === 'function') ? activeMarketMap() : null;
    if (marketMap && marketMap.has(symbol)) {
      const previousFilters = JSON.stringify(state.filters);
      state.filters.min_score = 0;
      state.filters.max_exit_risk = 100;
      state.filters.tier = 'all';
      state.filters.direction = 'all';
      state.filters.preset = 'all';
      state.filters.exit_flag = 'all';
      state.filters.min_institutional_confluence = 0;
      state.filters.min_options_positioning = 0;
      if ('min_volume_sentiment_conviction' in state.filters) state.filters.min_volume_sentiment_conviction = 0;
      state.filters.institutional_bias = 'all';
      state.filters.options_bias = 'all';
      state.filters.iob_state = 'all';
      state.filters.dark_pool_attraction = 'all';
      if (typeof applyPrefsToControls === 'function') applyPrefsToControls();
      if (typeof bumpFilterSortCache === 'function') bumpFilterSortCache();
      if (JSON.stringify(state.filters) !== previousFilters) {
        showToast(`Filters cleared to show ${symbol}`);
      }
      rows = activeRows();
      index = rows.findIndex((row) => row.symbol === symbol);
    }
  }
  if (index >= 0) {
    state.pageIndex = Math.floor(index / state.pageSize);
    renderResults();
    if (typeof renderPagination === 'function') renderPagination();
    // Wait one paint cycle so the row exists in the DOM before scrolling.
    requestAnimationFrame(() => {
      const target = document.querySelector(`tr[data-symbol="${symbol}"]`);
      if (target) {
        target.scrollIntoView({ block: 'center', behavior: 'smooth' });
        // Brief highlight so the eye lands on the right row.
        target.classList.add('row-jump-highlight');
        setTimeout(() => target.classList.remove('row-jump-highlight'), 1800);
      }
    });
  } else {
    showToast(`${symbol} is not in the current ${state.currentMarket} universe yet`);
  }
}

// Phase 26.9: lightweight non-blocking toast used by the search + jump
// helpers. Lives at the top-right so it doesn't fight the sidebar.
function showToast(msg, ms = 2400) {
  let host = document.getElementById('mrdToastHost');
  if (!host) {
    host = document.createElement('div');
    host.id = 'mrdToastHost';
    host.style.cssText = 'position:fixed;top:14px;right:14px;z-index:9000;pointer-events:none;display:flex;flex-direction:column;gap:8px;';
    document.body.appendChild(host);
  }
  const el = document.createElement('div');
  el.className = 'mrd-toast';
  el.textContent = msg;
  el.style.cssText = 'background:#1f2937;color:#e5e7eb;padding:.55rem .9rem;border-radius:.5rem;border:1px solid #4f98a3;font-size:.86rem;font-weight:500;box-shadow:0 6px 24px rgba(0,0,0,.45);pointer-events:auto;opacity:0;transition:opacity .2s';
  host.appendChild(el);
  requestAnimationFrame(() => { el.style.opacity = '1'; });
  setTimeout(() => {
    el.style.opacity = '0';
    setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 250);
  }, ms);
}

function renderPagination() {
  const total = pageCount();
  const current = state.pageIndex;
  const holder = byId('pageNumbers');
  const badge = byId('pageBadge');
  if (badge) badge.textContent = `${current + 1} / ${total}`;
  if (!holder) return;
  let start = Math.max(0, current - 2);
  let end = Math.min(total, start + 5);
  start = Math.max(0, end - 5);
  const pages = [];
  for (let i = start; i < end; i += 1) pages.push(`<button type="button" class="page-number ${i === current ? 'active' : ''}" data-page="${i}">${i + 1}</button>`);
  holder.innerHTML = pages.join('');
  holder.querySelectorAll('button[data-page]').forEach((btn) => btn.addEventListener('click', (e) => {
    e.stopPropagation();
    e.preventDefault();
    goToPage(Number(btn.dataset.page));
  }));
}

function renderTrackerSummary() {
  const tracked = trackedSortedRows();
  const activePool = Array.from(state.activeScanPool.values()).sort((a, b) => (b.final_score ?? 0) - (a.final_score ?? 0));
  if (byId('trackedBadge')) byId('trackedBadge').textContent = `${tracked.length} tracked`;
  if (byId('activeBadge')) byId('activeBadge').textContent = `${activePool.length} active scans`;
  if (byId('trackerSummary')) {
    byId('trackerSummary').textContent = tracked.length
      ? `${tracked.length} symbols are still inside the current main top 25 after multiple scan passes. The leader is ${tracked[0].symbol} with ${tracked[0].persistence_passes} passes.`
      : 'No active repeat names are in the current top 25.';
  }
  if (byId('trackerMeta')) {
    const maxPass = tracked.length ? tracked[0].persistence_passes : 0;
    byId('trackerMeta').textContent = `A symbol appears here only while it remains in the current main top 25 and has reached pass 2 or higher. Current max persistence: ${maxPass}.`;
  }
  if (byId('activeScanMeta')) {
    const lastRun = state.activeScanLastRunUtc ? new Date(state.activeScanLastRunUtc).toLocaleTimeString() : 'not run yet';
    byId('activeScanMeta').textContent = `Active scan pool covers the current top 10 pages, up to ${state.activeScanLimit} symbols. Last active sweep: ${lastRun}. Completed active passes: ${state.activeScanPasses}. Last refresh touched ${state.activeScanLastRefreshCount} symbols.`;
  }
}

// ---------------------------------------------------------------------------
// Phase 26.51 — Viewport-driven priority registration.
//
// Whenever the leaderboard re-renders we collect the symbols currently
// visible on the user's first page (after all filters / sorts) and POST
// them to `/api/future_mode/visible_symbols`.  The backend priority lane
// reads that registry on every tick and ALWAYS includes those symbols
// in its full re-score + GARCH-overlay pass — guaranteeing continuous
// deep-scan coverage of whatever the user is looking at right now.
//
// Throttled:
//   * minimum 2 s between identical pings (handles tick-driven re-renders)
//   * any change to the symbol set triggers an immediate push (handles
//     filter toggles, sort changes, page navigation)
// ---------------------------------------------------------------------------
let _lastVisiblePushSig = '';
let _lastVisiblePushAtMs = 0;
const _MIN_VISIBLE_PUSH_INTERVAL_MS = 2000;
function pushVisibleSymbols(symbols) {
  if (!Array.isArray(symbols) || symbols.length === 0) return;
  const market = state.currentMarket || 'stocks';
  const sig = market + '|' + symbols.slice().sort().join(',');
  const now = Date.now();
  const sameSet = (sig === _lastVisiblePushSig);
  if (sameSet && (now - _lastVisiblePushAtMs) < _MIN_VISIBLE_PUSH_INTERVAL_MS) {
    return;
  }
  _lastVisiblePushSig = sig;
  _lastVisiblePushAtMs = now;
  try {
    const url = (window.API_BASE || '') + '/api/future_mode/visible_symbols';
    const body = JSON.stringify({ market, symbols });
    if (navigator.sendBeacon) {
      const blob = new Blob([body], { type: 'application/json' });
      if (navigator.sendBeacon(url, blob)) return;
    }
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body, keepalive: true,
    }).catch(() => { /* swallow */ });
  } catch (_) { /* swallow */ }
}


// ===========================================================================
// Phase 26.61c — Toggle visibility of the 7 per-blend leaderboard columns.
//
// Each <th data-blend="..."> in market-refinement-dashboard.html gets
// `is-hidden` added when its corresponding blend toggle is OFF.  The
// matching <td class="fm-blend fm-*"> cells inherit the same rule
// from CSS (`.fm-blend.is-hidden { display:none }`).
// ===========================================================================
function _syncBlendColumnVisibility() {
  const blendStateBy = {
    lab:             !!(state.useLabMode         && state.blendLabIntoRanking),
    strategy:        !!(state.useStrategyMode    && state.blendStrategyIntoRanking),
    strategy_v2:     !!(state.useStrategyV2Mode  && state.blendStrategyV2IntoRanking),
    regime_risk:     !!(state.useRegimeRiskMode  && state.blendRegimeRiskIntoRanking),
    liq_kelly:       !!state.blendLiqKellyFactor,
    ml:              !!(state.useMlOverlayMode   && state.blendMlOverlayIntoRanking),
    reality_breaker: !!(state.advancedExperimentalMode && state.blendRealityBreakerIntoRanking),
  };
  document.querySelectorAll('th.col-fm.fm-blend[data-blend]').forEach((th) => {
    const key = th.getAttribute('data-blend');
    const on = !!blendStateBy[key];
    th.classList.toggle('is-hidden', !on);
  });
}

// =========================================================================
// Scanner-context row cells + per-row Future Forecast Activator.
// State lives on `state.inlineForecast` so the expansion survives every
// renderResults() refresh pass (state-driven rendering, no DOM-only state).
// =========================================================================
function renderPviCell(row) {
  const score = Number(row.predicted_volume_intensity_score ?? 0);
  const bucket = String(row.predicted_volume_intensity_bucket || 'low');
  const flag = !!row.predicted_volume_event_flag;
  const tone = bucket === 'extreme' ? 'pvi-extreme' : bucket === 'high' ? 'pvi-high' : bucket === 'moderate' ? 'pvi-moderate' : 'pvi-low';
  const tip = `Predicted volume intensity ${score.toFixed(1)} (${bucket})${flag ? ' — upcoming high-volume event likely' : ''}`;
  return `<span class="ctx-pill ${tone}" title="${esc(tip)}">${score.toFixed(0)}${flag ? '<sup class="pvi-flag">!</sup>' : ''}</span>`;
}

function renderSspCell(row) {
  const score = Number(row.short_selling_pressure_score ?? 50);
  const label = String(row.short_selling_pressure_label || 'neutral');
  const src = String(row.short_selling_pressure_source || 'unavailable');
  const tone = (label === 'squeeze_risk_bullish' || label === 'elevated_squeeze_watch') ? 'ssp-squeeze'
    : label === 'bearish_pressure' ? 'ssp-bear'
    : label === 'elevated' ? 'ssp-elevated' : 'ssp-neutral';
  const glyph = (label === 'squeeze_risk_bullish' || label === 'elevated_squeeze_watch') ? '\u25B2' : label === 'bearish_pressure' ? '\u25BC' : '';
  const tip = `Short selling pressure ${score.toFixed(1)} — ${label.replace(/_/g, ' ')} (source: ${src})`;
  return `<span class="ctx-pill ${tone}" title="${esc(tip)}">${score.toFixed(0)}${glyph}</span>`;
}

function renderExpCell(row) {
  const dte = row.days_to_options_expiration;
  if (dte == null) return '<span class="ctx-exp ctx-exp-none" title="No liquid options chain / expiration data unavailable">—</span>';
  const risk = !!row.expiration_risk_flag;
  const date = row.nearest_options_expiration || '?';
  const tone = risk ? 'ctx-exp-risk' : Number(dte) <= 5 ? 'ctx-exp-near' : 'ctx-exp-far';
  const tip = `Nearest options expiration ${date} (${dte}d)${risk ? ' — high-sensitivity expiration window: pinning/hedging flows may amplify moves' : ''}`;
  return `<span class="ctx-exp ${tone}" title="${esc(tip)}">${dte}d${risk ? '\u26a1' : ''}</span>`;
}

function renderForecastActionCell(row) {
  const inflight = state.forecastInflight.has(row.symbol);
  const active = state.inlineForecast && state.inlineForecast.symbol === row.symbol && state.inlineForecast.open;
  const ready = !!row.future_forecast_ready;
  const label = inflight ? '\u2026' : active ? 'Close' : 'Forecast';
  const tip = row.future_forecast_summary
    ? `Future forecast: ${row.future_forecast_summary}\nClick to ${active ? 'close' : 'run/expand'} the row forecast.`
    : `Run the future forecast for ${row.symbol} (short pressure + volume intensity + expiration aware).`;
  return `<button type="button" class="row-forecast-btn${active ? ' is-open' : ''}${ready ? ' is-ready' : ''}" data-symbol="${row.symbol}" data-testid="forecast-btn-${row.symbol}" title="${esc(tip)}" ${inflight ? 'disabled' : ''}>\u26a1 ${label}</button>`;
}

const _TABLE_COLSPAN = 30;

function renderInlineForecastRow(row) {
  const f = state.inlineForecast;
  if (!f || f.symbol !== row.symbol || !f.open) return '';
  let inner;
  if (f.loading) {
    inner = `<div class="if-loading" data-testid="inline-forecast-loading">Running future forecast for <strong>${row.symbol}</strong>\u2026</div>`;
  } else if (f.error) {
    inner = `<div class="if-error" data-testid="inline-forecast-error">\u26a0 Forecast failed: ${esc(String(f.error))} <button type="button" class="row-forecast-btn" data-symbol="${row.symbol}">Retry</button></div>`;
  } else if (f.payload) {
    const p = f.payload;
    const ctx = p.context || {};
    const sp = ctx.short_pressure_effect || {};
    const vi = ctx.volume_intensity_effect || {};
    const ex = ctx.expiration_effect || {};
    const hor = p.horizons || {};
    const horStrip = ['forward_1h', 'forward_1d', 'forward_5d'].map((k) => {
      const h = hor[k];
      if (!h) return '';
      const lbl = k === 'forward_1h' ? '1H' : k === 'forward_1d' ? '1D' : '5D';
      const cls = h.direction === 'Bullish' ? 'if-bull' : h.direction === 'Bearish' ? 'if-bear' : 'if-flat';
      const shifted = h.p_up !== h.p_up_base ? ` (base ${(h.p_up_base * 100).toFixed(0)}%)` : '';
      return `<span class="if-horizon ${cls}" title="P(up) ${(h.p_up * 100).toFixed(1)}%${shifted} · drift ${h.drift_pct}% · σ ${h.sigma_pct}%">${lbl}: ${h.direction} ${(h.p_up * 100).toFixed(0)}%</span>`;
    }).join('');
    const expl = (ctx.explanations || []).map((e) => `<li>${esc(e)}</li>`).join('');
    const relNote = (p.reliability === 'reduced')
      ? '<span class="if-reduced" title="Built from proxy/partial inputs — reduced-confidence forecast">reduced-confidence</span>' : '';
    inner = `
      <div class="if-body" data-testid="inline-forecast-body">
        <div class="if-head">
          <strong>\u26a1 Future forecast — ${row.symbol}</strong>
          <span class="if-summary">${esc(p.summary || '')}</span>
          ${relNote}
          ${p.cached ? '<span class="if-cached" title="Served from the 30s forecast cache">cached</span>' : ''}
          <button type="button" class="forecast-inline-close" data-testid="inline-forecast-close" title="Close forecast">\u2715</button>
        </div>
        <div class="if-strip">${horStrip}
          <span class="if-prob" title="Probability of a short-squeeze-driven expansion event">Squeeze ${(Number(ctx.squeeze_probability || 0) * 100).toFixed(0)}%</span>
          <span class="if-prob" title="Probability of a volatility/high-volume event">Vol-event ${(Number(ctx.volatility_event_probability || 0) * 100).toFixed(0)}%</span>
        </div>
        <div class="if-influences">
          <span class="if-inf" title="Short selling pressure influence on the forecast">Short pressure ${Number(sp.score ?? 50).toFixed(0)} (${String(sp.label || 'neutral').replace(/_/g, ' ')}, ${sp.source || 'n/a'}) \u2192 P(up) shift ${sp.p_up_shift > 0 ? '+' : ''}${((sp.p_up_shift || 0) * 100).toFixed(1)}pp</span>
          <span class="if-inf" title="Predicted volume intensity influence">Volume intensity ${Number(vi.score ?? 0).toFixed(0)} (${vi.bucket || 'low'})${vi.event_flag ? ' \u2192 event likely' : ''}</span>
          <span class="if-inf" title="Options expiration influence">Expiration ${ex.days_to_expiration != null ? `${ex.days_to_expiration}d` : 'n/a'}${ex.high_sensitivity_window ? ' \u2192 high-sensitivity window' : ''}${ex.risk_flag ? ' \u26a1' : ''} \u00b7 confidence \u00d7${Number(ctx.confidence_modifier ?? 1).toFixed(2)}</span>
        </div>
        ${expl ? `<ul class="if-expl">${expl}</ul>` : ''}
      </div>`;
  } else {
    inner = '<div class="if-loading">\u2026</div>';
  }
  return `<tr class="forecast-inline-row" data-forecast-for="${row.symbol}" data-testid="inline-forecast-row"><td colspan="${_TABLE_COLSPAN}">${inner}</td></tr>`;
}

async function runRowForecast(symbol) {
  if (!symbol || state.forecastInflight.has(symbol)) return;
  // Toggle-close when the same symbol's forecast is already expanded.
  if (state.inlineForecast && state.inlineForecast.symbol === symbol && state.inlineForecast.open && !state.inlineForecast.loading && !state.inlineForecast.error) {
    state.inlineForecast = null;
    renderResults();
    return;
  }
  state.forecastInflight.add(symbol);
  state.inlineForecast = { symbol, loading: true, error: null, payload: null, open: true };
  renderResults();
  try {
    const res = await fetch(`${state.apiBase}/api/forecast/run/${encodeURIComponent(symbol)}?market=${encodeURIComponent(state.currentMarket)}`, { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    if (payload.state === 'error' || payload.state === 'unavailable') {
      state.inlineForecast = { symbol, loading: false, error: payload.message || payload.error || 'Forecast unavailable for this row', payload: null, open: true };
    } else {
      state.inlineForecast = { symbol, loading: false, error: null, payload, open: true };
    }
  } catch (err) {
    state.inlineForecast = { symbol, loading: false, error: String((err && err.message) || err), payload: null, open: true };
  } finally {
    state.forecastInflight.delete(symbol);
    renderResults();
  }
}

function renderResults() {
  // Phase 26.51 helper state for visible-symbols pings.
  // See `pushVisibleSymbols()` declared below.
  // (Module-level guards live on `window` so hot-reload keeps them.)
  state.allRowsMap = new Map(filteredMainRows().map((row) => [`${state.currentMarket}:${row.symbol}`, row]));
  // Phase 26.61c — Sync the per-blend column-header visibility BEFORE
  // we render the table body so the layout reflows in one pass.
  _syncBlendColumnVisibility();
  const body = byId('resultsBody');
  if (!body) return;
  const rows = visibleRows();
  body.innerHTML = rows.map((row) => {
    const sm = row.scanner_metrics || {};
    const market = ((row.factor_breakdown || {}).market) || {};
    const icfStatus = ((market.institutional_confluence) || {}).status;
    const opStatus = ((market.options_positioning) || {}).status;
    const dpStatus = ((market.dark_pool_proxy) || {}).status;
    // When the family explicitly reports it has no usable input yet, show a
    // muted em-dash instead of the misleading "50" placeholder.
    const pendingStatuses = new Set(['insufficient_history', 'unavailable', 'symbol_unavailable',
                                      'no_expirations', 'options_unavailable']);
    // Phase 15: pluck the per-factor narratives so we can attach them to
    // each pill via data-narrative-* attributes.  app.js's narrative
    // popover (initFactorNarrativePopover) wires the click handler on
    // every cell with a narrative.
    const fns = (row.factor_breakdown || {}).factor_narratives || {};
    const renderPill = (value, status, label, provenance, familyKey) => {
      const narr = (familyKey && fns[familyKey]) || null;
      const dataAttrs = narr ? ` data-narrative-title="${label}" data-narrative-cell="${esc(narr.cell_text)}" data-narrative-detail="${esc(narr.detail_text)}" data-narrative-pred="${esc(narr.prediction)}"` : '';
      if (status && pendingStatuses.has(status)) {
        return `<span class="metric-pill metric-pill-pending narrative-target" title="${label}: still warming (${status})"${dataAttrs}>\u2014</span>`;
      }
      const cls = factorBadge(value);
      const titleSuffix = provenance ? ` \u00b7 ${provenance}` : '';
      return `<span class="metric-pill ${cls} narrative-target" title="${label}${titleSuffix} \u2014 click for narrative"${dataAttrs}>${Number(value ?? 50).toFixed(0)}</span>`;
    };
    const ageLabel = humanAge(row.age_seconds ?? 0);
    // Phase 26: row-level regulatory signal payload. Hoisted to row
    // scope so BOTH the `renderRegBadge` closure (which emits the in-cell
    // pill) AND the row-styling block below (which adds the `reg-signal-
    // row` class + tooltip) read the SAME reference. The previous agent
    // declared `reg` inside the closure only, which threw
    // `ReferenceError: reg is not defined` at line 1038 the moment a row
    // with a non-trivial regulatory delta tried to render.
    const reg = row.regulatory;
    // Phase 26: row-level regulatory badge. Snapshot store surfaces a compact
    // `regulatory` object on rows that have a non-trivial active signal.
    const renderRegBadge = () => {
      if (!reg || !Number.isFinite(reg.applied_delta) || Math.abs(reg.applied_delta) < 0.05) return '';
      const dir = reg.direction === 'up' ? 'up' : reg.direction === 'down' ? 'down' : 'flat';
      const sign = reg.applied_delta > 0 ? '+' : '';
      // Cluster tier label inside the badge (e.g., "x3" or "x5") when a
      // 7-day clustering bonus has triggered.
      let clusterChip = '';
      if (reg.cluster_bonus && reg.cluster_bonus > 0) {
        const count = Math.max(reg.bull_cluster_count || 0, reg.bear_cluster_count || 0);
        if (count >= 5) clusterChip = ' \u00b7 \u00d75';      // ≥5 confirming -> +25%
        else if (count >= 3) clusterChip = ' \u00b7 \u00d73'; // ≥3 confirming -> +10%
        else clusterChip = ` \u00b7 +${Math.round(reg.cluster_bonus * 100)}%`;
      }
      const cluster = (reg.cluster_bonus && reg.cluster_bonus > 0)
        ? ` · cluster +${Math.round(reg.cluster_bonus * 100)}% (${Math.max(reg.bull_cluster_count || 0, reg.bear_cluster_count || 0)} confirming in 7d)`
        : '';
      const role = reg.top_role_weight >= 0.99 ? ' · C-suite'
                 : reg.top_role_weight >= 0.84 ? ' · senior exec'
                 : '';
      const stale = (reg.staleness_days != null) ? ` · ${Number(reg.staleness_days).toFixed(1)}d ago` : '';
      const title = `Regulatory signal ${sign}${Number(reg.applied_delta).toFixed(2)} pts · ${reg.event_count} event${reg.event_count === 1 ? '' : 's'}${cluster}${role}${stale}`;
      return `<span class="reg-row-badge reg-row-${dir}" title="${title}" data-testid="row-reg-badge-${row.symbol}">REG ${sign}${Number(reg.applied_delta).toFixed(1)}${clusterChip}</span>`;
    };
    const regBadge = renderRegBadge();
    // Phase 26.17: rows that carry an active regulatory signal get their
    // own distinct visual treatment (subtle colored left-border + tinted
    // surface), separate from the "Low-confidence composite" warning the
    // ⚠ icon represents. The two indicators can coexist on the same row -
    // a thinly-traded micro-cap CAN simultaneously have an insider buy AND
    // missing intraday inputs - but they should NOT look like the same
    // thing. The user feedback: "every instance of one of these signals
    // shouldn't garner a low-confidence scoring" - i.e. the regulatory
    // boost is a high-confidence input even when the underlying ratings
    // are thin.
    let regRowClass = '';
    let regRowTitle = '';
    if (reg && Math.abs(Number(reg.applied_delta) || 0) >= 0.5) {
      const regDir = reg.direction === 'up' ? 'up' : reg.direction === 'down' ? 'down' : 'flat';
      regRowClass = ` reg-signal-row reg-signal-${regDir}`;
      const sign = Number(reg.applied_delta) > 0 ? '+' : '';
      regRowTitle = `Regulatory signal applied ${sign}${Number(reg.applied_delta).toFixed(2)} pts from ${reg.event_count || 0} event${reg.event_count === 1 ? '' : 's'}`;
    }
    // Phase 26.17: the score-explanation (Low-confidence composite) warning
    // is now displayed ONLY on the ⚠ dot inside the score cell, never as
    // the row's primary tooltip. The row's own tooltip now describes the
    // regulatory signal when present.
    const rowTitle = regRowTitle || '';
    // Phase 26.40 + 26.42: when a non-default trading style OR the
    // advanced ranking engine is active, show both scores: the raw
    // composite + the style/advanced-adjusted blend.  The cell is
    // what the table is sorted by; the [bracketed] number is the raw
    // composite for reference.
    const rawScore = Number(row.final_score ?? 0);
    let displayScore;
    let tooltip;
    if (state.useAdvancedRanking) {
      displayScore = advancedRankScore(row);
      tooltip = `Advanced (Bayesian Kelly): drift_horizon × √precision × directional_certainty. Composite (raw): ${rawScore.toFixed(1)}`;
    } else if (state.tradingStyle && state.tradingStyle !== 'default') {
      displayScore = styleAdjustedScore(row);
      tooltip = `${state.tradingStyle} style blend. Composite (raw): ${rawScore.toFixed(1)}`;
    } else {
      displayScore = rawScore;
      tooltip = '';
    }
    const scoreCellHtml = (state.useAdvancedRanking || (state.tradingStyle && state.tradingStyle !== 'default'))
      ? `${displayScore.toFixed(state.useAdvancedRanking ? 3 : 1)} <span class="score-style-raw" title="${tooltip}">[${rawScore.toFixed(0)}]</span>`
      : displayScore.toFixed(1);
    // Phase 26.47 — Future Mode cells.  Always render the cells (the
    // user can see the feature even when off) but content is empty
    // unless Future Mode is enabled AND the row has forward_metrics.
    const fmCells = (() => {
      // ------------------------------------------------------------
      // Phase 26.61c — Per-blend multiplier columns.
      // 7 trailing <td> cells (lab / strategy / sv2 / rr / liq / ml / rb)
      // Each one is HIDDEN via the matching `is-hidden` class unless
      // the corresponding blend toggle is active.  Visibility is
      // also toggled on the <th> via _syncBlendColumnVisibility().
      // ------------------------------------------------------------
      const blendCellOff = (cls, hidden) =>
        `<td class="col-fm fm-blend ${cls}${hidden ? ' is-hidden' : ''}"></td>`;
      const renderBlendCells = (fm) => {
        const cells = [];
        const _mult = (key, isOn, isExperimental) => {
          const visible = !!isOn;
          const val = fm && Number.isFinite(fm[key]) ? Number(fm[key]) : null;
          // Per-cell tone: > 1.03 bull, < 0.97 bear, else flat.
          let toneCls = 'is-flat';
          let strong = '';
          if (val != null) {
            if (val > 1.03) toneCls = 'is-bull';
            else if (val < 0.97) toneCls = 'is-bear';
            // "Strong" highlight when the multiplier is clearly biting
            // (≥10% deviation in either direction).
            if (Math.abs(val - 1.0) >= 0.10) strong = ' is-active-strong';
          }
          if (isExperimental) toneCls = visible ? toneCls : 'is-flat';
          return `<td class="col-fm fm-blend fm-${key.replace('_rank_multiplier','').replace('_factor','').replace('_multiplier','')} ${toneCls}${strong}${visible ? '' : ' is-hidden'}${isExperimental ? ' is-experimental' : ''}" title="${esc(`${key} = ${val == null ? '—' : val.toFixed(4) + '×'}`)}">${val == null ? '—' : val.toFixed(3) + '×'}</td>`;
        };
        cells.push(_mult('lab_rank_multiplier',         !!(state.useLabMode         && state.blendLabIntoRanking),         false));
        cells.push(_mult('strategy_rank_multiplier',    !!(state.useStrategyMode    && state.blendStrategyIntoRanking),    false));
        cells.push(_mult('strategy_v2_rank_multiplier', !!(state.useStrategyV2Mode  && state.blendStrategyV2IntoRanking),  false));
        cells.push(_mult('regime_risk_multiplier',      !!(state.useRegimeRiskMode  && state.blendRegimeRiskIntoRanking),  false));
        cells.push(_mult('liq_kelly_factor',            !!state.blendLiqKellyFactor, false));
        cells.push(_mult('ml_rank_multiplier',          !!(state.useMlOverlayMode   && state.blendMlOverlayIntoRanking),   false));
        cells.push(_mult('reality_breaker_multiplier',  !!(state.advancedExperimentalMode && state.blendRealityBreakerIntoRanking), true));
        return cells.join('');
      };
      if (!state.futureMode) {
        // OFF: 7 empty FM cells + 7 empty blend cells (hidden if no blend on).
        return '<td class="col-fm fm-dir"></td>'
             + '<td class="col-fm fm-pup"></td>'
             + '<td class="col-fm fm-pupctx"></td>'
             + '<td class="col-fm fm-drift"></td>'
             + '<td class="col-fm fm-kelly"></td>'
             + '<td class="col-fm fm-regime"></td>'
             + '<td class="col-fm fm-var"></td>'
             + renderBlendCells(null);
      }
      const fm = futureMetricsForRow(row);
      if (!fm) {
        // ON but no forward_metrics on this row (cheap pass) — show em-dashes.
        return '<td class="col-fm fm-dir col-fm-flat" title="No factor depth on this row yet">—</td>'
             + '<td class="col-fm fm-pup">—</td>'
             + '<td class="col-fm fm-pupctx">—</td>'
             + '<td class="col-fm fm-drift">—</td>'
             + '<td class="col-fm fm-kelly">—</td>'
             + '<td class="col-fm fm-regime">—</td>'
             + '<td class="col-fm fm-var">—</td>'
             + renderBlendCells(null);
      }
      const dirCfRaw = (fm.direction_cf || fm.direction || 'Neutral');
      // Phase 26.52 — blend Lab/Strategy multipliers into the visible direction.
      const dirInfo = effectiveFutureDirection(fm);
      const dirCf = dirInfo.dir;
      const dirAdjusted = dirInfo.adjusted;
      const dirClass = dirCf === 'Bullish' ? 'col-fm-bull' : dirCf === 'Bearish' ? 'col-fm-bear' : 'col-fm-flat';
      const dirIcon = dirCf === 'Bullish' ? '▲' : dirCf === 'Bearish' ? '▼' : '◆';
      const adjustedMark = dirAdjusted ? '<sup style="color:#fbbf24;font-size:.55em;letter-spacing:.05em">*</sup>' : '';
      const pUp = Number.isFinite(fm.p_up_cf) ? fm.p_up_cf : (Number.isFinite(fm.p_up) ? fm.p_up : 0.5);
      const pUpCtxRaw = Number.isFinite(fm.p_up_ctx) ? fm.p_up_ctx : null;
      const pUpCtxShift = Number.isFinite(fm.p_up_ctx_shift) ? fm.p_up_ctx_shift : 0;
      const drift = Number.isFinite(fm.drift_pct) ? fm.drift_pct : 0;
      const jumpDrift = Number.isFinite(fm.jump_drift_pct) ? fm.jump_drift_pct : 0;
      const effectiveDrift = drift + jumpDrift;
      const kelly = Number.isFinite(fm.effective_kelly_rank) ? fm.effective_kelly_rank : 0;
      const regime = fm.regime_label || '—';
      const var95 = Number.isFinite(fm.var95_pct) ? fm.var95_pct : 0;
      const cvar95 = Number.isFinite(fm.cvar95_pct) ? fm.cvar95_pct : 0;
      const kellyFrac = Number.isFinite(fm.kelly_fraction) ? fm.kelly_fraction : 0;
      const tier = fm._tier_source === 'garch' ? 'GARCH' : 'fast';
      const tierBadge = fm._tier_source === 'garch' ? '<sup style="color:#c4b5fd;font-size:.55em;letter-spacing:.05em">G</sup>' : '';
      const directionalCertaintyCf = Number.isFinite(fm.directional_certainty_cf) ? fm.directional_certainty_cf : 0;
      const labOnTip = !!(state.useLabMode && state.blendLabIntoRanking);
      const stratOnTip = !!(state.useStrategyMode && state.blendStrategyIntoRanking);
      const blendNote = (labOnTip || stratOnTip)
        ? `\nLab mult: ${(dirInfo.lab_multiplier ?? 1).toFixed(3)}× · Strategy mult: ${(dirInfo.strategy_multiplier ?? 1).toFixed(3)}× · combined: ${(dirInfo.composed_multiplier ?? 1).toFixed(3)}×${dirAdjusted ? ` (direction ${dirCfRaw} → ${dirCf})` : ''}`
        : '';
      const tip = `Future Mode (${tier}, ${_FUTURE_HORIZON_LABEL[state.futureHorizon] || state.futureHorizon})\nDirection (CF): ${dirCf}${dirAdjusted ? ` (Lab/Strategy adjusted; raw=${dirCfRaw})` : ''}\nP(up) CF: ${(pUp*100).toFixed(1)}%  (Gaussian: ${(Number(fm.p_up_gauss ?? fm.p_up ?? 0.5)*100).toFixed(1)}%)\nDrift: ${drift.toFixed(3)}%  + jump ${jumpDrift.toFixed(3)}%\nKelly rank: ${kelly.toFixed(5)}\nDirectional certainty (CF): ${(directionalCertaintyCf*100).toFixed(1)}%\nRegime: ${regime}\nVaR95: ${var95.toFixed(2)}%  CVaR95: ${cvar95.toFixed(2)}%\nKelly fraction: ${(kellyFrac*100).toFixed(1)}%${blendNote}`;
      return `<td class="col-fm fm-dir col-fm-on ${dirClass}" title="${esc(tip)}">${dirIcon} ${dirCf.slice(0,4)}${tierBadge}${adjustedMark}</td>`
           + `<td class="col-fm fm-pup col-fm-on">${(pUp*100).toFixed(1)}%</td>`
           + (() => {
               // Context-adjusted P(up): shows the short-pressure / PVI /
               // expiration-proximity overlay's net effect on directional
               // probability. If the row has no context overlay yet
               // (older/cheap-pass), render an em-dash without a delta.
               if (pUpCtxRaw == null) {
                 return '<td class="col-fm fm-pupctx col-fm-on" title="Context overlay pending — no short-pressure / PVI / expiration adjustments applied yet.">—</td>';
               }
               const deltaBps = Math.round(pUpCtxShift * 1000) / 10; // percentage points
               const deltaCls = deltaBps > 0.3 ? 'col-fm-bull' : deltaBps < -0.3 ? 'col-fm-bear' : 'col-fm-flat';
               const deltaSign = deltaBps > 0 ? '+' : '';
               const deltaGlyph = deltaBps > 0.3 ? '▲' : deltaBps < -0.3 ? '▼' : '·';
               const ctxTip = `Context-adjusted P(up): ${(pUpCtxRaw*100).toFixed(1)}%\nBase P(up) CF: ${(pUp*100).toFixed(1)}%\nΔ from context overlay: ${deltaSign}${deltaBps.toFixed(1)}pp\n\nShort-selling pressure, predicted volume intensity and options-expiration proximity shift the base probability up or down. Positive Δ = context reinforces bullish; negative Δ = context reinforces bearish.`;
               return `<td class="col-fm fm-pupctx col-fm-on ${deltaCls}" title="${esc(ctxTip)}"><span class="fm-pupctx-val">${(pUpCtxRaw*100).toFixed(1)}%</span><span class="fm-pupctx-delta"> ${deltaGlyph}${Math.abs(deltaBps).toFixed(1)}</span></td>`;
             })()
           + `<td class="col-fm fm-drift col-fm-on">${effectiveDrift.toFixed(3)}%</td>`
           + `<td class="col-fm fm-kelly col-fm-on ${dirClass}">${kelly.toFixed(4)}</td>`
           + `<td class="col-fm fm-regime col-fm-on">${regime}</td>`
           + `<td class="col-fm fm-var col-fm-on">${var95.toFixed(2)}%</td>`
           + renderBlendCells(fm);
    })();
    return `
    <tr data-symbol="${row.symbol}" class="${state.selectedSymbol === row.symbol ? 'selected' : ''}${regRowClass}" title="${rowTitle}">
      <td class="col-symbol">${row.symbol ?? '-'}${regBadge}</td>
      <td class="col-name" title="${row.name ?? ''}">${row.name ?? '-'}</td>
      <td class="col-score">${scoreCellHtml}${row.score_explanation ? ` <span class="warn-dot" title="${row.score_explanation.replace(/"/g, '&quot;')}">\u26a0</span>` : ''}</td>
      <td class="col-tier">${row.tier ?? '-'}</td>
      <td class="col-dir">${(row.final_direction ?? '-').slice(0,4)}</td>
      <td class="col-pill">${renderPill(sm.institutional_confluence, icfStatus, 'Institutional confluence', null, 'institutional_confluence')}</td>
      <td class="col-pill">${renderPill(sm.options_positioning, opStatus, 'Options positioning', sm.options_provenance || 'inferred', 'options_positioning')}</td>
      <td class="col-pill">${renderPill(sm.dark_pool_proxy, dpStatus, 'Dark pool proxy', null, 'dark_pool_proxy')}</td>
      <td class="col-src">${provenanceBadge(row)}</td>
      <td class="col-fresh col-age" title="Age ${ageLabel} \u00b7 captured ${row.as_of_utc || 'unknown'}">${row.freshness_label ?? 'unknown'}</td>
      <td class="col-pass">${row.persistence_passes ?? activePassCountMap().get(row.symbol) ?? 0}</td>
      <td class="col-pill col-pvi" data-testid="row-pvi-${row.symbol}">${renderPviCell(row)}</td>
      <td class="col-pill col-ssp" data-testid="row-ssp-${row.symbol}">${renderSspCell(row)}</td>
      <td class="col-exp" data-testid="row-exp-${row.symbol}">${renderExpCell(row)}</td>
      ${fmCells}
      <td class="col-fm col-consensus" data-testid="row-consensus-${row.symbol}">${_rowConsensusStrip(row)}</td>
      <td class="col-actions" data-testid="row-details-${row.symbol}">${renderForecastActionCell(row)}</td>
    </tr>${renderInlineForecastRow(row)}`;
  }).join('');
  body.querySelectorAll('tr[data-symbol]').forEach((tr) => tr.addEventListener('click', (ev) => {
    if (ev.target.closest('.row-forecast-btn')) return; // forecast button handles itself
    loadDetail(tr.dataset.symbol);
  }));
  // Future Forecast Activator buttons (table rows + inline expansion retry).
  body.querySelectorAll('.row-forecast-btn').forEach((btn) => btn.addEventListener('click', (ev) => {
    ev.stopPropagation();
    ev.preventDefault();
    runRowForecast(btn.dataset.symbol);
  }));
  body.querySelectorAll('.forecast-inline-close').forEach((btn) => btn.addEventListener('click', (ev) => {
    ev.stopPropagation();
    state.inlineForecast = null;
    renderResults();
  }));
  // Phase 26.51 — push the symbols visible on the user's first page
  // to the backend so the priority lane keeps them under continuous
  // deep GARCH + Lab + Strategy scan.  Throttled internally to one
  // ping per ~3 seconds so re-renders triggered by tick updates don't
  // spam the registry, but filter changes still get an immediate push
  // because the list of visible symbols differs from the last push.
  try {
    const visibleSyms = Array.from(body.querySelectorAll('tr[data-symbol]'))
      .map((tr) => tr.dataset.symbol)
      .filter(Boolean)
      .slice(0, 60);
    pushVisibleSymbols(visibleSyms);
  } catch (_) {
    // Non-fatal — the dashboard works even if the push fails.
  }
  // Phase 26.50 — flag the table when Future Mode is active so CSS can
  // hide the regular factor-pill columns (Inst/Opts/DP) and let the
  // Future-Mode columns (F-Dir/F-P(up)/F-Drift/F-Kelly/F-Regime/F-VaR)
  // claim the freed real estate.  Critical on narrow screens (handheld
  // PCs etc) where the FM columns otherwise scroll off to the right.
  // The columns themselves are always RENDERED — we only toggle their
  // visibility — so the swap is purely cosmetic and reversible.
  const table = body.closest('table');
  if (table) {
    table.classList.toggle('fm-on', !!state.futureMode);
    table.classList.toggle('pvi-priority-on', !!state.pviPriority);
    table.classList.toggle('lab-on', !!state.useLabMode);
    table.classList.toggle('strategy-on', !!state.useStrategyMode);
    // Blend indicators help the user see whether the blends are actively
    // multiplying into the rank ordering.
    table.classList.toggle('blend-lab',      !!(state.useLabMode && state.blendLabIntoRanking));
    table.classList.toggle('blend-strategy', !!(state.useStrategyMode && state.blendStrategyIntoRanking));
  }
  // Visible mode indicator: predicted-volume-first ordering.
  const pviBadge = byId('pviModeBadge');
  if (pviBadge) pviBadge.style.display = state.pviPriority ? '' : 'none';
  const meta = byId('resultsMeta');
  const loaded = byId('loadedBadge');
  const totalRows = activeRows();
  if (meta) meta.textContent = state.currentView === 'tracked'
    ? `${totalRows.length} tracked survivors loaded, showing ${rows.length} on this page`
    : `${allSortedRows().length} loaded from the combined scan set, showing ${rows.length} on this page`;
  if (loaded) loaded.textContent = `${allSortedRows().length} loaded`;
  renderPagination();
  renderTrackerSummary();
}

function setText(id, txt) { const el = byId(id); if (el) el.textContent = txt; }
function setHTML(id, html) { const el = byId(id); if (el) el.innerHTML = html; }
function fmtPct(num, den) { if (!den) return '0%'; return `${Math.round((num / den) * 100)}%`; }
function fmtNum(n) { return new Intl.NumberFormat().format(Math.round(Number(n || 0))); }

// Phase 15: escape strings for safe insertion into HTML attribute values.
// Used by the factor-pill narrative attrs so quotes / angle brackets in
// generated text never break the markup.
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// Phase 15: shared narrative popover.  Click any element with
// `.narrative-target` and the popover shows the cell-text + prediction.
// Initialised once on first render so we don't double-bind on re-renders.
(function initFactorNarrativePopover() {
  if (window.__factorNarrativePopoverInited) return;
  window.__factorNarrativePopoverInited = true;
  const pop = document.createElement('div');
  pop.className = 'narrative-popover';
  pop.style.display = 'none';
  document.body.appendChild(pop);
  function hide() { pop.style.display = 'none'; pop.__owner = null; }
  function showFor(target) {
    if (pop.__owner === target) { hide(); return; }
    const title = target.getAttribute('data-narrative-title') || 'Factor';
    const cell  = target.getAttribute('data-narrative-cell') || '';
    const det   = target.getAttribute('data-narrative-detail') || '';
    const pred  = target.getAttribute('data-narrative-pred') || '';
    if (!cell && !det && !pred) return;
    pop.innerHTML = `<div class="narr-title">${title}</div>
      ${cell ? `<div class="narr-cell">${cell}</div>` : ''}
      ${det  ? `<div class="narr-detail">${det}</div>`  : ''}
      ${pred ? `<div class="narr-pred"><span class="narr-pred-label">Prediction:</span> ${pred}</div>` : ''}`;
    pop.style.display = 'block';
    const r = target.getBoundingClientRect();
    const pr = pop.getBoundingClientRect();
    let top  = window.scrollY + r.bottom + 6;
    let left = window.scrollX + r.left;
    if (left + pr.width > window.scrollX + window.innerWidth - 12) left = window.scrollX + window.innerWidth - pr.width - 12;
    if (left < window.scrollX + 8) left = window.scrollX + 8;
    if (r.bottom + 6 + pr.height > window.innerHeight - 8 && r.top - pr.height - 6 > 8) {
      top = window.scrollY + r.top - pr.height - 6;
    }
    pop.style.top = top + 'px';
    pop.style.left = left + 'px';
    pop.__owner = target;
  }
  document.addEventListener('click', (e) => {
    const t = e.target.closest && e.target.closest('.narrative-target');
    if (t) {
      e.stopPropagation();
      showFor(t);
      return;
    }
    if (!pop.contains(e.target)) hide();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hide(); });
  window.addEventListener('scroll', hide, true);
  window.addEventListener('resize', hide);
})();

// ---------- Lightweight throttle for expensive render passes ----------
// At 3k+ symbols, renderStatus() walks the full universe to compute
// scanned/cached counts.  Calling it on every websocket-style update
// drowns the main thread.  Throttle to at most once per N ms; trailing
// edge guarantees the final state is always reflected.
function makeThrottler(fn, delayMs) {
  let lastRun = 0;
  let pending = null;
  let trailingArgs = null;
  return function throttled(...args) {
    const now = Date.now();
    trailingArgs = args;
    if (now - lastRun >= delayMs) {
      lastRun = now;
      fn.apply(null, args);
    } else if (!pending) {
      const wait = delayMs - (now - lastRun);
      pending = setTimeout(() => {
        pending = null;
        lastRun = Date.now();
        fn.apply(null, trailingArgs);
      }, wait);
    }
  };
}

function _renderStatusImpl() {
  if (!state.status) return;
  const provider = state.status.provider || {};
  const lkg = state.status.last_known_good || {};
  const df = state.status.defaulted_fields || {};
  const failureClasses = state.status.failure_classes || {};
  const loadedRows = allSortedRows();
  const fullMarketRows = Array.from(activeMarketMap().values());
  const scannedVisibleCount = fullMarketRows.filter((r) => {
    const ds = String(r.source_state_data_source || r.data_source || '');
    const outcome = String(r.provider_outcome || '');
    return ds.length > 0 || outcome.length > 0 || !!r.as_of_utc;
  }).length;
  const cachedVisibleCount = fullMarketRows.filter((r) => {
    const ds = String(r.source_state_data_source || r.data_source || '');
    const outcome = String(r.provider_outcome || '');
    return ds.includes('cache') || outcome.includes('cache');
  }).length;
  const staleOkCount = fullMarketRows.filter((r) => r.state === 'stale-ok' || r.lkg_fallback).length;
  const providerIsDegraded = !!state.status.degraded_mode;
  const throttleState = String(provider.throttle_state || 'normal');
  const providerIsBusy = !providerIsDegraded && (throttleState === 'budget-exhausted' || throttleState === 'backoff');

  // ------- top-line status (single sentence) -------
  const totalAccumulated = fullMarketRows.length;
  const filteredOut = Math.max(0, totalAccumulated - loadedRows.length);
  const filterTag = filteredOut > 0 ? ` (${fmtNum(filteredOut)} hidden by filters)` : '';
  const statusText = providerIsDegraded
    ? 'Provider degraded'
    : providerIsBusy
    ? 'Provider busy'
    : 'Live pipeline healthy';
  setText('statusLine', `${statusText} \u00b7 ${fmtNum(loadedRows.length)} of ${fmtNum(totalAccumulated)} symbols visible across ${state.totalBatches} batches${filterTag}`);
  setText('batchBadge', `Batch ${state.status.current_batch ?? state.batchIndex}`);
  const providerBadgeText = providerIsDegraded
    ? 'Provider degraded'
    : providerIsBusy
    ? 'Provider busy'
    : provider.session_ready
    ? 'Provider ready'
    : 'Provider cold';
  setText('providerBadge', providerBadgeText);
  const providerBadge = byId('providerBadge');
  if (providerBadge) {
    providerBadge.classList.toggle('status-chip-warn', providerIsBusy);
    providerBadge.classList.toggle('status-chip-err', providerIsDegraded);
    providerBadge.classList.toggle('status-chip-ok', !providerIsBusy && !providerIsDegraded && provider.session_ready);
  }

  // ------- card: scan progress -------
  setText('mScanProgress', `${state.batchIndex + 1} / ${state.totalBatches}`);
  const progress = state.lastScanProgress || {};
  const kept = typeof progress.loaded_rows === 'number' ? `kept ${progress.loaded_rows}/${progress.slice_rows ?? '?'}` : 'first batch';
  setText('mScanSub', `${kept} \u00b7 ${lkg.batches_cached || 0} LKG batches`);

  // ------- card: universe -------
  // Phase 25: the BIG headline number is the per-sweep monotonic counter,
  // not the bucket size.  It counts 0 → universe_size → 0 every sweep so
  // the user can visually confirm the scanner walks the full universe.
  // The bucket size (top-N retained) is now a sub-line metric.
  const scanProg = state.lastScanProgress || {};
  const sweepScanned = Number(scanProg.current_sweep_scanned || 0);
  const totalUniverseSize = Number(scanProg.universe_size || 0)
    || Number((state.status || {}).universe_size || 0)
    || totalAccumulated;
  const bucketRetained = totalAccumulated;
  setText('mUniverseVisible', fmtNum(sweepScanned));
  const evalEver = Number(scanProg.evaluations_ever || 0);
  const evalEverPart = evalEver > 0 ? ` \u00b7 ${fmtNum(evalEver)} cumulative` : '';
  const bucketPart = bucketRetained > 0 ? ` \u00b7 ${fmtNum(bucketRetained)} retained` : '';
  setText(
    'mUniverseSub',
    `${fmtNum(sweepScanned)} of ${fmtNum(totalUniverseSize)} this sweep${bucketPart}${evalEverPart} \u00b7 ${fmtNum(df.rows_normalized || 0)} normalized`,
  );

  // ------- card: active pool + tracked -------
  const poolStats = state.status.active_scan_pool || {};
  setText('mPool', `${poolStats.size || state.activeScanPool.size || 0} / ${poolStats.cap || 0}`);
  setText('mPoolSub', `${trackedSortedRows().length} survivors tracked${staleOkCount ? ` \u00b7 ${staleOkCount} stale-ok` : ''}`);

  // ------- card: reaction clustering -------
  const rc = state.status.reaction_clustering_stats || {};
  const rcComputed = rc.computed || 0;
  if (rcComputed > 0) {
    setText('mReactions', fmtNum(rcComputed));
    const propel = rc.classified_propel || 0;
    const reject = rc.classified_reject || 0;
    const chop = rc.classified_chop || 0;
    const neutral = rc.classified_neutral || 0;
    const tot = propel + reject + chop + neutral || 1;
    setHTML('mReactionsSub', `
      <span class="rb rb-propel" title="Propel ${propel} (${fmtPct(propel, tot)})"><span class="rb-dot"></span>P ${fmtPct(propel, tot)}</span>
      <span class="rb rb-reject" title="Reject ${reject} (${fmtPct(reject, tot)})"><span class="rb-dot"></span>R ${fmtPct(reject, tot)}</span>
      <span class="rb rb-chop"   title="Chop ${chop} (${fmtPct(chop, tot)})"><span class="rb-dot"></span>C ${fmtPct(chop, tot)}</span>
      <span class="rb rb-neut"   title="Neutral ${neutral} (${fmtPct(neutral, tot)})"><span class="rb-dot"></span>N ${fmtPct(neutral, tot)}</span>
    `);
  } else {
    setText('mReactions', '0');
    setText('mReactionsSub', 'no zones classified yet');
  }

  // ------- card: providers -------
  const ps = state.status.provider_stats || {};
  const psEntries = Object.entries(ps).map(([name, info]) => ({ name, hits: info.hits || 0, calls: info.calls || 0 }));
  const totalCalls = psEntries.reduce((a, b) => a + b.calls, 0);
  const totalHits = psEntries.reduce((a, b) => a + b.hits, 0);
  setText('mProvidersHead', totalCalls > 0 ? `${fmtPct(totalHits, totalCalls)} hit-rate` : '\u2014');
  // If stooq is in the list with 0 hits and any attempts, attach a diagnostic note.
  const stooqDiag = state.status.stooq_diagnostics || {};
  let stooqNote = '';
  if (stooqDiag.attempts && stooqDiag.successes === 0) {
    const reasons = [];
    if (stooqDiag.timeouts) reasons.push(`${fmtNum(stooqDiag.timeouts)} timeouts`);
    if (stooqDiag.network_errors) reasons.push(`${fmtNum(stooqDiag.network_errors)} net-errors`);
    if (stooqDiag.http_errors) reasons.push(`${fmtNum(stooqDiag.http_errors)} HTTP-errors`);
    if (stooqDiag.no_data) reasons.push(`${fmtNum(stooqDiag.no_data)} no-data`);
    let circuit = '';
    if (stooqDiag.circuit_open) {
      const rem = Number(stooqDiag.circuit_remaining_seconds || 0);
      const remHuman = rem > 3600 ? `${(rem / 3600).toFixed(1)}h` : `${Math.ceil(rem / 60)}m`;
      const trip = stooqDiag.circuit_trip_count ? ` #${stooqDiag.circuit_trip_count}` : '';
      circuit = ` \u00b7 paused${trip} ${remHuman}`;
    }
    stooqNote = reasons.length
      ? ` <span class="prov-note" title="Stooq is unreachable from this network. ${reasons.join(', ')}. The provider auto-disables with exponential backoff (30m → 1h → 2h → 6h → 24h cap) after 50 consecutive failures.">(${reasons.join(' \u00b7 ')}${circuit})</span>`
      : (stooqDiag.circuit_open ? ` <span class="prov-note" title="Circuit breaker open">(paused${stooqDiag.circuit_trip_count ? ' #' + stooqDiag.circuit_trip_count : ''})</span>` : '');
  }
  const providerLines = psEntries
    .sort((a, b) => b.calls - a.calls)
    .slice(0, 4)
    .map((p) => {
      const note = (p.name === 'stooq') ? stooqNote : '';
      return `<span class="prov-row"><span class="prov-name">${p.name}</span><span class="prov-counts">${p.hits}/${p.calls}${note}</span></span>`;
    })
    .join('');
  setHTML('mProvidersSub', providerLines || 'first batch pending');

  // ------- card: options chain -------
  const oc = state.status.options_chain_stats || {};
  const ocAttempts = oc.attempts || 0;
  const ocReal = oc.hits_real || 0;
  const ocCache = oc.cache_hits || 0;
  const ocNoOpts = oc.no_options_skips || 0;
  const ocFetchErr = oc.fetch_error_skips || 0;
  const ocThrottle = oc.throttle_skips || 0;
  const ocSkipTotal = ocThrottle + (oc.cooldown_skips || 0);
  const ocNoOptsUnique = oc.no_options_unique_symbols || 0;
  if (ocAttempts || ocReal || ocCache) {
    setText('mOptions', `${ocReal}/${ocAttempts}`);
    // Build a tooltip-friendly skip breakdown so the operator knows WHY rows are skipped.
    const skipBreakdown = [];
    if (ocNoOpts) skipBreakdown.push(`${fmtNum(ocNoOpts)} no-options (${fmtNum(ocNoOptsUnique)} symbols)`);
    if (ocFetchErr) skipBreakdown.push(`${fmtNum(ocFetchErr)} fetch-err`);
    if (ocThrottle) skipBreakdown.push(`${fmtNum(ocThrottle)} rate-limited`);
    const skipSummary = skipBreakdown.length
      ? `${fmtNum(ocSkipTotal)} skipped (${skipBreakdown.join(' \u00b7 ')})`
      : `${fmtNum(ocSkipTotal)} skipped`;
    setText('mOptionsSub', `real \u00b7 ${ocCache} cached \u00b7 ${skipSummary}`);
    const optEl = byId('mOptionsSub');
    if (optEl) optEl.title = `Options-chain skip breakdown:
- no_options_listed: ${ocNoOpts} attempts spared (${ocNoOptsUnique} unique symbols have no options at all — warrants, micro-caps, units, etc. We back off for 6 hrs on each.)
- fetch_error: ${ocFetchErr} attempts skipped due to recent fetch failures (10-min cooldown)
- rate-limited: ${ocThrottle} attempts deferred to next cycle to respect Yahoo throttling`;
  } else {
    setText('mOptions', '\u2014');
    setText('mOptionsSub', 'no requests yet');
  }

  // ------- card: daily history -------
  const dh = state.status.daily_history_stats || {};
  const dhAttempts = dh.attempts || 0;
  const dhReal = dh.hits_real || 0;
  const dhCache = dh.cache_hits || 0;
  if (dhAttempts || dhCache) {
    setText('mHistory', fmtNum(dhReal + dhCache));
    setText('mHistorySub', `${dhReal} fresh \u00b7 ${fmtNum(dhCache)} from cache`);
  } else {
    setText('mHistory', '\u2014');
    setText('mHistorySub', 'cache warming');
  }

  // ------- card: factor coverage -------
  // df.top is the list of defaulted fields; ideally length 0 = full coverage.
  const totalRows = df.rows_normalized || 0;
  const topDefaulted = (df.top || []).slice(0, 3);
  if (totalRows > 0) {
    const worstDefault = topDefaulted[0]?.count || 0;
    const covered = Math.max(0, totalRows - worstDefault);
    setText('mCoverage', fmtPct(covered, totalRows));
    if (topDefaulted.length === 0) {
      setText('mCoverageSub', 'all families computed \u2713');
    } else {
      const summary = topDefaulted
        .map((d) => {
          const short = d.field.split('.').pop() || d.field;
          return `${short} warming (${d.count})`;
        })
        .join(' \u00b7 ');
      setText('mCoverageSub', summary);
    }
  } else {
    setText('mCoverage', '\u2014');
    setText('mCoverageSub', 'rows scored');
  }

  // ------- failures summary (folded into status chip) -------
  const failParts = Object.entries(failureClasses).map(([k, v]) => `${k}=${v}`).join(', ');
  if (failParts && providerBadge) providerBadge.title = `Failures: ${failParts}`;
}

// Throttled public entry-point.  Limits status rerenders to one every 400ms.
const renderStatus = makeThrottler(_renderStatusImpl, 400);

function renderError(message) {
  if (byId('statusLine')) byId('statusLine').textContent = `Error: ${message}`;
}

function detailSection(title, content) {
  return `<section class="factor-card"><div class="eyebrow">${title}</div><div>${content}</div></section>`;
}

// =========================================================================
// Phase 26.39: leveraged-variant tick-mode helpers.
//
// startDetailLiveRefresh(symbol):
//   Tear down any prior interval, then if the variant is the leveraged
//   build (`state.variant.live_tick_enabled` === true) install a new
//   setInterval that calls _reloadDetailPanelInPlace() on the requested
//   cadence (default 2 s).  We deliberately re-call `loadDetail` itself
//   — it's idempotent (just overwrites `#detailBody`) and the recursion
//   guard `_detailRefreshInFlight` prevents request stacking when a
//   tick lands while the previous fetch is still pending.
//
// stopDetailLiveRefresh():
//   Called whenever the panel context changes (different symbol clicked,
//   detail tab closed, etc.).  Safe to call when no timer is running.
// =========================================================================
function stopDetailLiveRefresh() {
  if (state.detailLiveTimer) {
    clearInterval(state.detailLiveTimer);
    state.detailLiveTimer = null;
  }
  state.detailLiveSymbol = null;
}

// =========================================================================
// Detail-panel share widget.  Renders a dropdown menu with intent-URLs
// for the major social platforms + a copy-link + a native mobile share
// button.  The shared content is a rich text summary built from every
// numeric metric currently visible in the detail panel, plus a deep
// link (?symbol=SYM) back to this dashboard.
// =========================================================================
const _SHARE_TARGETS = [
  { id: 'copy',     label: 'Copy link',        icon: '\u29c9', kind: 'clipboard' },
  { id: 'copyfull', label: 'Copy summary',     icon: '\u2398', kind: 'clipboard-full' },
  { id: 'native',   label: 'Device share…',    icon: '\u2197', kind: 'native' },
  { id: 'twitter',  label: 'X / Twitter',      icon: '\u{1D54F}', kind: 'intent',
    build: (u, s) => `https://twitter.com/intent/tweet?text=${encodeURIComponent(s)}&url=${encodeURIComponent(u)}` },
  { id: 'facebook', label: 'Facebook',         icon: 'f', kind: 'intent',
    build: (u, s) => `https://www.facebook.com/sharer/sharer.php?u=${encodeURIComponent(u)}&quote=${encodeURIComponent(s)}` },
  { id: 'linkedin', label: 'LinkedIn',         icon: 'in', kind: 'intent',
    build: (u, s) => `https://www.linkedin.com/sharing/share-offsite/?url=${encodeURIComponent(u)}&summary=${encodeURIComponent(s)}` },
  { id: 'reddit',   label: 'Reddit',           icon: 'r/', kind: 'intent',
    build: (u, s, title) => `https://www.reddit.com/submit?url=${encodeURIComponent(u)}&title=${encodeURIComponent(title)}` },
  { id: 'whatsapp', label: 'WhatsApp',         icon: '\u{1F4AC}', kind: 'intent',
    build: (u, s) => `https://api.whatsapp.com/send?text=${encodeURIComponent(s + '\n' + u)}` },
  { id: 'telegram', label: 'Telegram',         icon: '\u2708', kind: 'intent',
    build: (u, s) => `https://t.me/share/url?url=${encodeURIComponent(u)}&text=${encodeURIComponent(s)}` },
  { id: 'email',    label: 'Email',            icon: '\u2709', kind: 'intent',
    build: (u, s, title) => `mailto:?subject=${encodeURIComponent(title)}&body=${encodeURIComponent(s + '\n\n' + u)}` },
];

// Build a compact, high-signal one-liner for platforms with character
// limits (Twitter etc.) — mirrors what a trader would want to skim.
function buildShareOneLiner(detail) {
  const sym = detail.symbol || '';
  const score = Number(detail.final_score ?? 0).toFixed(1);
  const tier = detail.tier || '?';
  const dir = detail.final_direction || 'Neutral';
  const fm = (detail.forward_metrics_garch && detail.forward_metrics_garch.forward_1d)
          || (detail.forward_metrics && detail.forward_metrics.forward_1d) || {};
  const pUp = Number(fm.p_up_cf ?? fm.p_up ?? 0.5);
  const pCtx = Number.isFinite(fm.p_up_ctx) ? Number(fm.p_up_ctx) : null;
  const kelly = Number.isFinite(fm.effective_kelly_rank) ? Number(fm.effective_kelly_rank) : null;
  const parts = [`$${sym} · Tier ${tier} · ${dir} · Score ${score}`];
  parts.push(`1d P(up) ${(pUp * 100).toFixed(1)}%${pCtx != null ? ` (ctx ${(pCtx * 100).toFixed(1)}%)` : ''}`);
  if (kelly != null) parts.push(`Kelly ${kelly.toFixed(4)}`);
  const fc = ((detail.forward_metrics_garch && detail.forward_metrics_garch.forecast_context)
            || (detail.forward_metrics && detail.forward_metrics.forecast_context) || {});
  const sq = Number(fc.squeeze_probability || 0);
  const ve = Number(fc.volatility_event_probability || 0);
  if (sq >= 0.35) parts.push(`Squeeze ${(sq * 100).toFixed(0)}%`);
  if (ve >= 0.45) parts.push(`Vol-event ${(ve * 100).toFixed(0)}%`);
  return parts.join(' · ');
}

// Multi-line rich summary — every metric visible in the detail panel.
// This gets attached to Facebook `quote`, LinkedIn `summary`, Reddit body,
// email body, and the "Copy summary" clipboard target.
function buildShareRichSummary(detail) {
  const lines = [];
  const sym = detail.symbol || '';
  const name = detail.name || '';
  const exch = detail.exchange || '';
  lines.push(`$${sym}${name ? ' — ' + name : ''}${exch ? ' (' + exch + ')' : ''}`);
  lines.push(`Composite score ${Number(detail.final_score ?? 0).toFixed(2)} · Tier ${detail.tier ?? '?'} · ${detail.final_direction ?? '-'}`);
  lines.push(`Freshness ${detail.freshness_label ?? 'unknown'} · Source ${detail.data_source ?? '?'}`);

  const fb = detail.factor_breakdown || {};
  const market = fb.market || {};

  // Ratings block
  const ar = detail.algorithm_ratings || {};
  const ratingsLine = ['momentum','quality','trend','stability']
    .map((k) => `${k}=${Number((ar[k] || {}).score ?? 0).toFixed(1)}`).join(' · ');
  if (ratingsLine) lines.push('Ratings — ' + ratingsLine);

  // Core factor families
  const tvd = market.trend_volume_delta || {};
  if (tvd.score != null) lines.push(`Trend/Volume Δ ${Number(tvd.score).toFixed(1)} (${tvd.bucket ?? 'neutral'})`);
  const icf = market.institutional_confluence || {};
  if (icf.score != null) lines.push(`Institutional confluence ${Number(icf.score).toFixed(1)} (${icf.bias ?? 'neutral'})`);
  const op = market.options_positioning || {};
  if (op.score != null) lines.push(`Options positioning ${Number(op.pressure_score_adjusted ?? op.score).toFixed(1)} (${op.bias ?? 'neutral'}, gamma ${op.gamma_level_label ?? '?'}, pin ${op.pin_risk ?? '?'})`);
  const iob = market.institutional_order_block || {};
  if (iob.score != null) lines.push(`Institutional order block ${Number(iob.score).toFixed(1)} (${iob.state ?? '?'}, ${iob.bias ?? 'neutral'})`);
  const dp = market.dark_pool_proxy || {};
  if (dp.score != null) lines.push(`Dark pool proxy ${Number(dp.score).toFixed(1)} (${dp.bias ?? 'neutral'}, attraction ${dp.attraction_state ?? '?'})`);
  const vs = market.volume_sentiment || {};
  if (vs.directional_score != null) lines.push(`Volume sentiment ${Number(vs.directional_score).toFixed(1)} (${vs.bias ?? 'neutral'}, conviction ${Number(vs.conviction_score ?? 0).toFixed(0)})`);
  const rmap = market.reaction_map || {};
  if (rmap.classification) lines.push(`Reaction clustering — ${rmap.classification} (propel ${Math.round((rmap.propel_probability || 0) * 100)}% · reject ${Math.round((rmap.reject_probability || 0) * 100)}% · chop ${Math.round((rmap.chop_probability || 0) * 100)}%)`);

  // Scanner-context families
  const ssp = market.short_selling_pressure || {};
  if (ssp.score != null) lines.push(`Short-selling pressure ${Number(ssp.score).toFixed(1)} (${String(ssp.label ?? 'neutral').replace(/_/g,' ')}, ${ssp.source ?? '?'})`);
  const pvi = market.predicted_volume_intensity || {};
  if (pvi.score != null) lines.push(`Predicted volume intensity ${Number(pvi.score).toFixed(1)} (${pvi.bucket ?? 'low'}${pvi.event_flag ? ', event flagged' : ''})`);
  const oe = market.options_expiration || {};
  if (oe.nearest_expiration) lines.push(`Nearest expiration ${oe.nearest_expiration} · ${oe.days_to_expiration}d${oe.risk_flag ? ' · risk flagged' : ''}`);

  // Future forecast block (per horizon)
  const horizonKeys = ['forward_1h','forward_5h','forward_10h','forward_1d','forward_overnight','forward_10d'];
  const fmAll = detail.forward_metrics_garch || detail.forward_metrics || {};
  const fcast = [];
  for (const k of horizonKeys) {
    const b = fmAll[k];
    if (!b || typeof b !== 'object') continue;
    const p = Number(b.p_up_cf ?? b.p_up ?? 0.5);
    const pc = Number.isFinite(b.p_up_ctx) ? Number(b.p_up_ctx) : null;
    const drift = Number(b.drift_pct ?? 0);
    const jd = Number(b.jump_drift_pct ?? 0);
    const kelly = Number.isFinite(b.effective_kelly_rank) ? Number(b.effective_kelly_rank) : null;
    const dir = b.direction_cf || b.direction || 'Neutral';
    const label = k.replace('forward_', '');
    fcast.push(`  ${label}: ${dir} · P(up) ${(p * 100).toFixed(1)}%${pc != null ? ` (ctx ${(pc * 100).toFixed(1)}%)` : ''} · drift ${(drift + jd).toFixed(3)}%${kelly != null ? ` · Kelly ${kelly.toFixed(4)}` : ''}`);
  }
  if (fcast.length) {
    lines.push('Future forecast:');
    lines.push(...fcast);
  }
  const fc = fmAll.forecast_context || {};
  const fcbits = [];
  if (Number(fc.squeeze_probability || 0) > 0) fcbits.push(`squeeze ${Math.round(fc.squeeze_probability * 100)}%`);
  if (Number(fc.volatility_event_probability || 0) > 0) fcbits.push(`vol-event ${Math.round(fc.volatility_event_probability * 100)}%`);
  if (fc.reliability) fcbits.push(`reliability ${fc.reliability}`);
  if (fcbits.length) lines.push('Context — ' + fcbits.join(' · '));

  // Exit + fundamentals context
  const exit = fb.exit_model || {};
  if (exit.data_ready) lines.push(`Exit model ${Number(exit.score ?? 0).toFixed(2)} (${exit.exit_flag ?? 'hold'})`);
  const fund = fb.fundamentals || {};
  if (fund.sector) lines.push(`Fundamentals — ${fund.sector}/${fund.industry ?? '?'} · PE ${fund.trailing_pe ?? 0} · Fwd PE ${fund.forward_pe ?? 0}`);

  lines.push('');
  lines.push('via Quantum Market Scanner');
  return lines.join('\n');
}

function _buildShareUrl(symbol) {
  const u = new URL(window.location.href);
  u.searchParams.set('symbol', symbol);
  // Preserve the currently-active preset in the shared link.
  if (state.filters && state.filters.preset) u.searchParams.set('preset', state.filters.preset);
  else u.searchParams.delete('preset');
  return u.toString();
}

function _renderShareMenu(menuEl, symbol, detail) {
  const oneLiner = buildShareOneLiner(detail);
  const rich = buildShareRichSummary(detail);
  const shareUrl = _buildShareUrl(symbol);
  const title = `${symbol} — ${detail.final_direction ?? 'Neutral'} · Score ${Number(detail.final_score ?? 0).toFixed(1)}`;
  const items = _SHARE_TARGETS.map((t) => {
    return `<button type="button" class="share-menu-item" data-share-id="${t.id}" data-testid="share-${t.id}"><span class="share-menu-icon">${t.icon}</span><span>${t.label}</span></button>`;
  }).join('');
  menuEl.innerHTML = `
    <div class="share-menu-summary" data-testid="share-summary-preview">${escapeHtmlLocal(oneLiner)}</div>
    <div class="share-menu-grid">${items}</div>
    <div class="share-menu-footer" data-testid="share-menu-footer">Shared link: <code>${escapeHtmlLocal(shareUrl)}</code></div>
  `;
  menuEl.querySelectorAll('.share-menu-item').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.shareId;
      const target = _SHARE_TARGETS.find((t) => t.id === id);
      if (!target) return;
      if (target.kind === 'clipboard') {
        await _copyToClipboard(shareUrl);
        _flashBtn(btn, 'Copied \u2713');
      } else if (target.kind === 'clipboard-full') {
        await _copyToClipboard(`${title}\n\n${rich}\n\n${shareUrl}`);
        _flashBtn(btn, 'Copied \u2713');
      } else if (target.kind === 'native') {
        try {
          if (navigator.share) {
            await navigator.share({ title, text: oneLiner + '\n\n' + rich, url: shareUrl });
            _flashBtn(btn, 'Shared \u2713');
          } else {
            await _copyToClipboard(`${title}\n\n${rich}\n\n${shareUrl}`);
            _flashBtn(btn, 'Copied (no native)');
          }
        } catch (_) { /* user cancelled */ }
      } else if (target.kind === 'intent' && target.build) {
        // Facebook / LinkedIn only scrape OG tags from the URL — we
        // route through the backend /share/{symbol} page so the link
        // preview renders the metric summary properly.
        const ogUrl = _buildOgShareUrl(symbol);
        const intentUrl = target.build(ogUrl, oneLiner, title);
        window.open(intentUrl, '_blank', 'noopener,width=720,height=640');
        _flashBtn(btn, 'Opened \u2197');
      }
    });
  });
}

function _buildOgShareUrl(symbol) {
  // Backend serves an HTML page at /share/{symbol} with proper OG meta
  // tags scrapable by Facebook / LinkedIn / Twitter cards; the page
  // itself redirects a real visitor to the dashboard with the deep link.
  const base = state.apiBase || '';
  const preset = (state.filters && state.filters.preset) ? `?preset=${encodeURIComponent(state.filters.preset)}` : '';
  return `${base.replace(/\/$/, '')}/share/${encodeURIComponent(symbol)}${preset}`;
}

async function _copyToClipboard(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) { /* fall through */ }
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } catch (_) { /* no-op */ }
  document.body.removeChild(ta);
  return true;
}

function _flashBtn(btn, text) {
  const prev = btn.innerHTML;
  btn.innerHTML = `<span class="share-menu-icon">\u2713</span><span>${text}</span>`;
  btn.classList.add('is-flash');
  setTimeout(() => { btn.innerHTML = prev; btn.classList.remove('is-flash'); }, 1400);
}

function escapeHtmlLocal(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function wireDetailShareWidget(symbol, detail) {
  const btn = byId('detailShareBtn');
  const menu = byId('detailShareMenu');
  if (!btn || !menu) return;
  const closeOnOutside = (ev) => {
    if (!menu.contains(ev.target) && ev.target !== btn) {
      menu.hidden = true;
      document.removeEventListener('click', closeOnOutside, true);
    }
  };
  btn.addEventListener('click', (ev) => {
    ev.stopPropagation();
    if (menu.hidden) {
      _renderShareMenu(menu, symbol, detail);
      menu.hidden = false;
      setTimeout(() => document.addEventListener('click', closeOnOutside, true), 0);
    } else {
      menu.hidden = true;
      document.removeEventListener('click', closeOnOutside, true);
    }
  });
}


function startDetailLiveRefresh(symbol) {
  // Always clear the prior timer — protects against double-binds when
  // the user clicks rapidly between rows.
  stopDetailLiveRefresh();
  if (!state.variant || !state.variant.live_tick_enabled) return;
  const intervalMs = state.variant.live_tick_interval_ms || 2000;
  state.detailLiveSymbol = symbol;
  let _detailRefreshInFlight = false;
  state.detailLiveTimer = setInterval(async () => {
    // Re-validate that the panel still owns this symbol — the user may
    // have clicked a different row in the table since the timer fired.
    if (state.detailLiveSymbol !== symbol || state.selectedSymbol !== symbol) {
      stopDetailLiveRefresh();
      return;
    }
    // Phase 26.45: respect the circuit-breaker backoff window.
    // While the backoff is active we skip the fetch entirely — the
    // "connection paused" pill stays visible and the user knows the
    // panel is intentionally idling.
    if (state.detailLiveBackoffUntil && Date.now() < state.detailLiveBackoffUntil) {
      return;
    }
    if (_detailRefreshInFlight) return; // skip overlapping ticks
    _detailRefreshInFlight = true;
    try {
      await loadDetail(symbol);
    } catch (err) {
      console.warn('[detail-live-tick] refresh failed:', err);
    } finally {
      _detailRefreshInFlight = false;
    }
  }, intervalMs);
}

async function loadDetail(symbol) {
  // Phase 26.43: when the user clicks a different row, the cached
  // prediction + backtest cards from the prior symbol must NOT bleed
  // through into the new panel.  Reset both caches eagerly here.
  // Inside the live-tick recursion this is a no-op (symbol is the
  // same as the cached one, so the cache is preserved by the
  // re-injection step at the bottom of the try block).
  if (state.selectedSymbol !== symbol) {
    state.cachedPredictionCard = null;
    state.cachedBacktestCard = null;
    state.detailLiveTickCount = 0;
    state._lastGoodForecast = null;   // Phase 26.62 — drop prior symbol's forecast cache
    // Symbol identity truly changed — this is the ONLY passive path
    // allowed to reset expandable-section UI state (vibe dropdown).
    if (state.expOpenTf) {
      state.expOpenTf = null;
      state.detailAudit.stateResets += 1;
    }
  }
  state.selectedSymbol = symbol;
  if (byId('detailMeta')) byId('detailMeta').textContent = symbol;
  _syncShareableUrl();
  renderResults();
  // Phase 26.39: leveraged-variant only — start (or restart) a 2 s
  // background timer that re-loads this same symbol's detail payload
  // so the panel ticks live without the user clicking "Refresh".
  // Calling `loadDetail` again with a different symbol below will
  // cancel + restart the timer cleanly (single-source-of-truth).
  startDetailLiveRefresh(symbol);
  try {
    const detail = await fetchJson(`${state.apiBase}/stock/${encodeURIComponent(symbol)}?market=${state.currentMarket}&require_fresh=true`);
    // Phase 26.71 hotfix — Race guard: if the user selected a different
    // symbol while our fetch was in flight, DROP this response.  Without
    // this, an old symbol's stale-but-just-arrived payload can overwrite
    // the panel a fraction of a second after the user clicked a new row,
    // producing the "bouncing back to a previously-selected stock"
    // behaviour reported on 2026-07-02.
    if (state.selectedSymbol && state.selectedSymbol !== symbol) {
      return;
    }
    // Phase 26.49 — cache the latest detail payload so the click-to-pin
    // info popover system can re-render the Future Forecast card
    // without doing another network call.
    state.detailPayload = detail;
    const trackedPasses = activePassCountMap().get(symbol) || 0;
    const activeMeta = state.activeScanPool.get(symbol) || {};
    const liveRow = state.allRowsMap.get(symbol) || {};
    const ratings = detail.algorithm_ratings || {};
    const fb = detail.factor_breakdown || {};
    const sourceRow = state.activeScanPool.get(symbol) || (Object.keys(liveRow).length ? liveRow : detail);
    const sourceFb = sourceRow.factor_breakdown || {};
    const market = fb.market || sourceFb.market || fb || {};
    const fundamentals = fb.fundamentals || sourceFb.fundamentals || {};
    const ratingCards = ['momentum', 'quality', 'trend', 'stability'].map((key) => {
      const item = ratings[key] || { score: fb[key] || 0, rating: 'Available' };
      const friendly = { momentum: 'Momentum captures short-term thrust.', quality: 'Quality scores intraday tradeability and participation.', trend: 'Trend reflects directional persistence.', stability: 'Stability checks whether the move is holding together intraday.' }[key];
      return `<div class="rating-card"><div class="eyebrow">${key}</div><div class="rating-score">${Number(item.score ?? 0).toFixed(2)} \u00b7 ${item.rating ?? 'Unknown'}</div><div class="rating-meta">${friendly}</div></div>`;
    }).join('');

    // Extended factor-family cards
    const tvd = market.trend_volume_delta || {};
    const icf = market.institutional_confluence || {};
    const op = market.options_positioning || {};
    const iob = market.institutional_order_block || {};
    const dp = market.dark_pool_proxy || {};
    const vs = market.volume_sentiment || {};
    const rmap = market.reaction_map || {};
    const opComposite = op.composite || {};
    const dz = rmap.dominant_zone || {};
    const ier = iob.expected_reaction || {};
    // Phase 15: pull per-factor narratives (cell_text/detail_text/prediction)
    // so each factor card can render an "in plain English" + prediction line.
    const narrAll = ((detail.factor_breakdown || {}).factor_narratives) || ((fb.factor_narratives) || {});
    const renderNarrative = (key) => {
      const n = narrAll[key];
      if (!n) return '';
      const detTxt = n.detail_text || '';
      const predTxt = n.prediction || '';
      return `<div class="factor-narrative">
        ${detTxt ? `<div class="factor-narrative-text">${detTxt}</div>` : ''}
        ${predTxt ? `<div class="factor-narrative-pred"><strong>Prediction:</strong> ${predTxt}</div>` : ''}
      </div>`;
    };
    const familyExplain = {
      trend_volume_delta: 'Combines short-term return direction with relative volume to flag where price is moving with conviction versus drifting on thin volume.',
      institutional_confluence: 'Aggregates relative-rotation, unusual-flow buy/sell pressure, ATR-based regime, and liquidity-sweep evidence into one institutional bias.',
      options_positioning: 'Reads weighted strike pressure across near-term and monthly expirations. Real chain when available, otherwise an inferred heuristic from intraday structure. Modulated by the volume sentiment substrate.',
      institutional_order_block: 'V1 heuristic: detects a recent strong impulse plus a retest of the impulse origin. Expected reaction (propel / reject / chop) blends zone evidence with the live volume sentiment profile.',
      dark_pool_proxy: 'V1 heuristic: large-volume bars inside tight ranges suggest hidden absorption. Surfaces nearby print clusters and a pinning effect score.',
      volume_sentiment: 'Shared volume-sentiment substrate that powers reject/chop/propel prediction and modulates the options pressure read. Built from buy/sell volume pressure, accumulation/distribution, effort-vs-result, and regime.',
      reaction_map: 'Multi-level reaction-clustering engine. Pivots are clustered into zones, ranked by evidence (touches, rejection magnitude, volume at level, recency), and the dominant zone is classified PROPEL / REJECT / CHOP using volume sentiment alignment.',
    };
    const classificationBadge = (cls) => {
      if (cls === 'PROPEL') return '<span class="badge badge-live">PROPEL</span>';
      if (cls === 'REJECT') return '<span class="badge badge-stale">REJECT</span>';
      if (cls === 'CHOP') return '<span class="badge badge-preview">CHOP</span>';
      return '<span class="badge badge-unavailable">NEUTRAL</span>';
    };
    const pctBar = (p, label) => {
      const pct = Math.max(0, Math.min(100, Math.round((Number(p) || 0) * 100)));
      return `<div class="prob-row"><span class="prob-label">${label}</span><div class="prob-bar"><div class="prob-fill" style="width:${pct}%"></div></div><span class="prob-pct">${pct}%</span></div>`;
    };
    const familyCards = `
      <div class="factor-card factor-extended">
        <div class="eyebrow">Trend / volume delta</div>
        <div class="factor-headline"><strong>${Number(tvd.score ?? 50).toFixed(1)}</strong> \u00b7 ${tvd.bias ?? 'neutral'} \u00b7 bucket <em>${tvd.bucket ?? 'neutral'}</em></div>
        <div class="rating-meta">${familyExplain.trend_volume_delta}</div>
        ${renderNarrative('trend_volume_delta')}
        <div class="factor-meta">delta_pct ${tvd.delta_pct ?? '-'} \u00b7 source ${tvd.provenance ?? 'derived'}</div>
      </div>
      <div class="factor-card factor-extended">
        <div class="eyebrow">Institutional confluence</div>
        <div class="factor-headline"><strong>${Number(icf.score ?? 50).toFixed(1)}</strong> \u00b7 ${icf.bias ?? 'neutral'}</div>
        <div class="rating-meta">${familyExplain.institutional_confluence}</div>
        ${renderNarrative('institutional_confluence')}
        <div class="factor-meta">RRG ${(icf.rrg||{}).quadrant ?? '-'} (${Number((icf.rrg||{}).score ?? 50).toFixed(0)}) \u00b7 Flow ${(icf.flow||{}).bias ?? '-'} (${Number((icf.flow||{}).score ?? 50).toFixed(0)}) \u00b7 Regime ${(icf.regime||{}).state ?? '-'} \u00b7 Liquidity ${(icf.liquidity||{}).signal ?? '-'}</div>
      </div>
      <div class="factor-card factor-extended">
        <div class="eyebrow">Volume sentiment <span class="badge ${vs.status === 'implemented' ? 'badge-live' : 'badge-unavailable'}">${vs.status === 'implemented' ? vs.provenance || 'real_history' : 'unavailable'}</span></div>
        <div class="factor-headline"><strong>${Number(vs.directional_score ?? 50).toFixed(1)}</strong> \u00b7 ${vs.bias ?? 'neutral'} \u00b7 conviction <em>${Number(vs.conviction_score ?? 0).toFixed(0)}</em></div>
        <div class="rating-meta">${familyExplain.volume_sentiment}</div>
        ${renderNarrative('volume_sentiment')}
        <div class="factor-meta">Regime ${vs.regime ?? 'normal'} \u00b7 A/D ${Number(vs.accumulation_distribution ?? 50).toFixed(1)} \u00b7 Effort/result ${vs.effort_vs_result_label ?? 'neutral'} \u00b7 Vol z ${Number(vs.volume_z_score ?? 0).toFixed(2)} \u00b7 Buy/Sell ratio ${Number(vs.buy_sell_ratio ?? 1).toFixed(2)} \u00b7 ${vs.bars_used ?? 0} bars</div>
      </div>
      <div class="factor-card factor-extended">
        <div class="eyebrow">Reaction clustering ${classificationBadge(rmap.classification)} <span class="badge ${rmap.status === 'implemented' ? 'badge-live' : 'badge-unavailable'}">${rmap.status === 'implemented' ? rmap.provenance || 'real_history' : 'unavailable'}</span></div>
        <div class="rating-meta">${familyExplain.reaction_map}</div>
        ${renderNarrative('reaction_clustering')}
        <div class="prob-grid">
          ${pctBar(rmap.propel_probability, 'Propel')}
          ${pctBar(rmap.reject_probability, 'Reject')}
          ${pctBar(rmap.chop_probability, 'Chop')}
        </div>
        <div class="factor-meta">Dominant zone tier <strong>${dz.tier ?? 'MINOR'}</strong> at ${dz.midpoint ?? '-'} (\u00b1 ${dz.distance_pct ?? '-'}%) \u00b7 evidence ${Number(dz.evidence_score ?? 0).toFixed(1)} \u00b7 touches ${dz.touches ?? 0} \u00b7 rejection ${dz.rejection_strength ?? '-'}% \u00b7 ${rmap.zone_count ?? 0} zones detected \u00b7 alignment ${rmap.volume_sentiment_alignment ?? 'mixed'}</div>
      </div>
      <div class="factor-card factor-extended">
        <div class="eyebrow">Options positioning <span class="badge ${op.provenance === 'real_chain' ? 'badge-live' : 'badge-inferred'}">${op.provenance ?? 'inferred'}</span></div>
        <div class="factor-headline"><strong>${Number(op.pressure_score_adjusted ?? op.score ?? 50).toFixed(1)}</strong> ${op.pressure_score_adjusted != null && Math.abs(Number(op.pressure_score_adjusted) - Number(op.score ?? 50)) > 0.5 ? `<span class="adj-note">(base ${Number(op.score ?? 50).toFixed(1)}, volume-adjusted)</span>` : ''} \u00b7 ${op.bias ?? 'neutral'} \u00b7 gamma <em>${op.gamma_level_label ?? 'moderate'}</em></div>
        <div class="rating-meta">${familyExplain.options_positioning}</div>
        ${renderNarrative('options_positioning')}
        <div class="factor-meta">Volume alignment <strong>${op.volume_alignment ?? 'unavailable'}</strong> \u00b7 Pin risk ${op.pin_risk ?? 'low'} \u00b7 Composite target ${opComposite.target_price ?? '-'} \u00b7 Call wall ${opComposite.call_wall ?? '-'} \u00b7 Put wall ${opComposite.put_wall ?? '-'} \u00b7 Expirations ${op.expirations_used ?? 0} \u00b7 Sentiment bias ${opComposite.volume_sentiment_bias ?? 'neutral'} (conv ${opComposite.volume_sentiment_conviction ?? 0})</div>
      </div>
      <div class="factor-card factor-extended">
        <div class="eyebrow">Institutional order block ${classificationBadge(iob.reaction_classification)}</div>
        <div class="factor-headline"><strong>${Number(iob.score ?? 50).toFixed(1)}</strong> \u00b7 ${iob.bias ?? 'neutral'} \u00b7 state <em>${iob.state ?? 'unknown'}</em></div>
        <div class="rating-meta">${familyExplain.institutional_order_block}</div>
        ${renderNarrative('institutional_order_block')}
        <div class="prob-grid">
          ${pctBar(ier.propel_probability, 'Propel')}
          ${pctBar(ier.reject_probability, 'Reject')}
          ${pctBar(ier.chop_probability, 'Chop')}
        </div>
        <div class="factor-meta">Reaction source <strong>${ier.source ?? 'fallback'}</strong> \u00b7 Volume alignment <strong>${ier.volume_alignment ?? 'unavailable'}</strong> (score ${Number(iob.volume_alignment_score ?? 50).toFixed(0)}) \u00b7 Zone ${iob.zone_low ?? '-'} \u2192 ${iob.zone_high ?? '-'} \u00b7 Midpoint ${iob.midpoint ?? '-'} \u00b7 Distance ${iob.distance_from_price_pct ?? '-'}% \u00b7 Respect ${iob.respect_rate ?? '-'} \u00b7 Touches ${iob.touch_count ?? 0}</div>
      </div>
      <div class="factor-card factor-extended">
        <div class="eyebrow">Dark pool proxy</div>
        <div class="factor-headline"><strong>${Number(dp.score ?? 50).toFixed(1)}</strong> \u00b7 ${dp.bias ?? 'neutral'} \u00b7 attraction <em>${dp.attraction_state ?? 'neutral'}</em></div>
        <div class="rating-meta">${familyExplain.dark_pool_proxy}</div>
        ${renderNarrative('dark_pool_proxy')}
        <div class="factor-meta">Nearest print ${dp.nearest_print_level ?? '-'} \u00b7 Distance ${dp.distance_to_print_pct ?? '-'}% \u00b7 Zone density ${dp.zone_density ?? 0} \u00b7 Pinning ${dp.pinning_effect ?? '-'}</div>
      </div>
      ${(() => {
        const ssp = market.short_selling_pressure || {};
        const comp = ssp.components || {};
        const compTxt = Object.keys(comp).map((k) => `${k.replace(/_/g, ' ')} ${Number(comp[k]).toFixed(0)}`).join(' \u00b7 ') || 'no components available';
        const badgeCls = ssp.source === 'live' ? 'badge-live' : (ssp.source === 'unavailable' || !ssp.source) ? 'badge-unavailable' : 'badge-inferred';
        return `
      <div class="factor-card factor-extended" data-testid="detail-short-pressure-card">
        <div class="eyebrow">Short selling pressure <span class="badge ${badgeCls}">${ssp.source ?? 'unavailable'}</span></div>
        <div class="factor-headline"><strong>${Number(ssp.score ?? 50).toFixed(1)}</strong> \u00b7 ${String(ssp.label ?? 'neutral').replace(/_/g, ' ')} \u00b7 confidence <em>${ssp.confidence ?? 'low'}</em></div>
        <div class="rating-meta">Blended short-pressure inference: live short interest when available, otherwise downside persistence, gap-down continuation, price suppression and options-conflict proxies. Feeds the future forecast (squeeze vs bearish suppression) — not display-only.</div>
        <div class="factor-meta">${compTxt}</div>
      </div>`;
      })()}
      ${(() => {
        const pvi = market.predicted_volume_intensity || {};
        const reasons = (pvi.reasons || []).join(' \u00b7 ') || 'no forward volume signals detected';
        const badgeCls = pvi.source === 'derived' ? 'badge-live' : (pvi.source === 'unavailable' || !pvi.source) ? 'badge-unavailable' : 'badge-inferred';
        return `
      <div class="factor-card factor-extended" data-testid="detail-pvi-card">
        <div class="eyebrow">Predicted volume intensity <span class="badge ${badgeCls}">${pvi.source ?? 'unavailable'}</span></div>
        <div class="factor-headline"><strong>${Number(pvi.score ?? 0).toFixed(1)}</strong> \u00b7 ${pvi.bucket ?? 'low'} ${pvi.event_flag ? '\u00b7 <em>high-volume event likely</em>' : ''}</div>
        <div class="rating-meta">Forward-looking estimate of an upcoming high-volume event (participation acceleration, pre-breakout compression + creep, options concentration, expiration proximity) — not a restatement of current volume ratio.</div>
        <div class="factor-meta">${reasons}</div>
      </div>`;
      })()}
      ${(() => {
        const oe = market.options_expiration || {};
        const badgeCls = oe.source === 'chain' ? 'badge-live' : (oe.source === 'unavailable' || !oe.source) ? 'badge-unavailable' : 'badge-inferred';
        const body = oe.nearest_expiration
          ? `Nearest expiration <strong>${oe.nearest_expiration}</strong> (${oe.days_to_expiration}d, ${oe.expiration_type ?? '?'})${oe.high_sensitivity_window ? ' \u00b7 <em>high-sensitivity window</em>' : ''}${oe.risk_flag ? ' \u00b7 \u26a1 expiration-risk flagged' : ''}`
          : 'No liquid options chain / expiration data unavailable for this symbol.';
        return `
      <div class="factor-card factor-extended" data-testid="detail-expiration-card">
        <div class="eyebrow">Options expiration <span class="badge ${badgeCls}">${oe.source ?? 'unavailable'}</span></div>
        <div class="factor-headline">${body}</div>
        <div class="rating-meta">Expiration proximity modulates predicted volume intensity, squeeze/suppression interpretation and forecast confidence (pinning, hedging flows, gamma behavior near expiry).</div>
      </div>`;
      })()}
    `;
    const detailBody = byId('detailBody');
    if (!detailBody) return;
    detailBody.innerHTML = `
      <div class="detail-summary">
        <div class="detail-card"><div class="eyebrow">Symbol</div><div class="value">${detail.symbol ?? '-'} ${provenanceBadge(detail)}</div></div>
        <div class="detail-card"><div class="eyebrow">Composite score</div><div class="value">${Number(detail.final_score ?? 0).toFixed(2)}</div></div>
        <div class="detail-card"><div class="eyebrow">Tier and bias</div><div class="value">${detail.tier ?? '-'} \u00b7 ${detail.final_direction ?? '-'}</div></div>
        <div class="detail-card"><div class="eyebrow">Top 25 passes</div><div class="value">${trackedPasses}</div></div>
      </div>
      <div class="detail-actions">
        <button id="manualRefreshBtn" type="button" class="refresh-button ${detail.stale || detail.state === 'stale-ok' || detail.lkg_fallback ? 'is-stale' : ''}" title="Force a full provider re-download (intraday + daily + live quote) — use to break a lockup or clear a rate-limit stall. Normal refreshes happen automatically every 2 seconds while the panel is open.">${detail.stale || detail.state === 'stale-ok' || detail.lkg_fallback ? '\u26a0 Unblock refresh' : '\u21bb Force refresh'}</button>
        <button id="backtestBtn" type="button" class="backtest-button">Run backtest</button>
        <div class="share-widget" data-testid="detail-share-widget">
          <button id="detailShareBtn" type="button" class="share-button" data-testid="detail-share-btn" title="Share this analysis on social platforms or copy the link">\u2197 Share</button>
          <div id="detailShareMenu" class="share-menu" hidden data-testid="detail-share-menu"></div>
        </div>
        <span class="predict-button-group" data-testid="predict-button-group">
          <span class="predict-button-group-label">Predict:</span>
          <button id="predictBtn1h"   type="button" class="predict-button predict-button-sm" data-horizon="1h"  data-testid="predict-btn-1h">1H</button>
          <button id="predictBtn5h"   type="button" class="predict-button predict-button-sm" data-horizon="5h"  data-testid="predict-btn-5h">5H</button>
          <button id="predictBtn10h"  type="button" class="predict-button predict-button-sm" data-horizon="10h" data-testid="predict-btn-10h">10H</button>
          <button id="predictBtn1d"   type="button" class="predict-button predict-button-sm" data-horizon="1d"  data-testid="predict-btn-1d">Next Day</button>
          <button id="predictBtn10d"  type="button" class="predict-button"                    data-horizon="10d" data-testid="predict-btn">10-Day</button>
          <button id="predictBtnNDO"  type="button" class="predict-button predict-button-sm predict-button-direction" data-testid="predict-btn-ndo">Next-Day Open</button>
        </span>
      </div>
      <div class="detail-explainer">${detail.name ?? '-'} on ${detail.exchange ?? '-'} is currently marked <strong>${detail.freshness_label ?? 'unknown'}</strong> from <strong>${detail.data_source ?? 'unknown'}</strong>${detail.state === 'stale-ok' ? ' (serving last-known-good payload)' : ''}.</div>
      ${detail.score_explanation ? `<div class="score-warning"><strong>\u26a0 Low-confidence composite</strong><div class="score-warning-text">${detail.score_explanation}</div></div>` : ''}
      ${(() => {
        const sc = ((detail.factor_breakdown || {}).secondary_composite) || {};
        if (!sc.family_scores) return '';
        const fs = sc.family_scores;
        const fmtScore = (k) => `<span class="cs-family"><span class="cs-name">${k}</span><span class="cs-bar"><span class="cs-bar-fill" style="width:${Math.round(fs[k] || 0)}%"></span></span><span class="cs-val">${Number(fs[k] || 0).toFixed(1)}</span></span>`;
        const families = ['trend_volume_delta','institutional_confluence','options_positioning','institutional_order_block','dark_pool_proxy','volume_sentiment','reaction_clustering'];
        const mod = Number(sc.predictive_modifier || 0);
        const modBadge = mod > 0 ? `<span class="cs-modifier cs-mod-pos">+${mod.toFixed(1)} (consensus agreement)</span>` : (mod < 0 ? `<span class="cs-modifier cs-mod-neg">${mod.toFixed(1)} (consensus contradiction)</span>` : '');
        const notesHtml = (sc.modifier_notes || []).map((n) => `<li>${n}</li>`).join('');
        return `
        <div class="composite-breakdown">
          <div class="cs-head">
            <span class="cs-title">Composite breakdown</span>
            <span class="cs-formula">core ${Number(sc.core_final || 0).toFixed(1)} \u00d7 0.80 + extended ${Number(sc.extended_avg || 0).toFixed(1)} \u00d7 0.20${mod ? ` + modifier` : ''} = ${Number(sc.blended_final || 0).toFixed(2)}</span>
            ${modBadge}
          </div>
          <div class="cs-families">${families.map(fmtScore).join('')}</div>
          ${notesHtml ? `<ul class="cs-notes">${notesHtml}</ul>` : ''}
        </div>`;
      })()}
      <div class="rating-grid">${ratingCards}</div>
      <h3 class="section-title">Ultra-scanner factor families</h3>
      <div class="factor-grid">${familyCards}</div>
      <h3 class="section-title">Detected reaction zones (per-zone classification)</h3>
      ${renderZoneList(rmap.zones || [])}
      <div id="backtestResults"></div>
      <div id="predictionResults"></div>
      ${renderFutureForecastCard(detail)}
      ${renderForecastContextCard(detail)}
      ${renderExperimentalCompositeCard(detail)}
      <h3 class="section-title">Operational context</h3>
      <div class="factor-grid">
        ${detailSection('Price snapshot', `${market.last_price ?? '-'} last \u00b7 ${market.previous_close ?? '-'} prior close \u00b7 ${market.change_pct ?? '-'}% change`)}
        ${detailSection('Fundamentals', `${fundamentals.sector ?? 'unknown'} / ${fundamentals.industry ?? 'unknown'}<br>PE: ${fundamentals.trailing_pe ?? 0} \u00b7 Fwd PE: ${fundamentals.forward_pe ?? 0}`)}
        ${detailSection('Persistence', trackedPasses >= 2 ? `Currently holds a tracked slot with ${trackedPasses} top-25 passes.` : `Seen ${trackedPasses} top-25 passes so far. It appears on the tracked page only while it remains in the live top 25 after pass 2.`)}
        ${detailSection('Active scan pool', state.activeScanPool.has(symbol) ? `Actively rescanned. Cycles: ${activeMeta.active_scan_cycles || 0}.` : 'Not in the current top-10-page active pool.')}
        ${detailSection('Exit risk', `${(((detail.factor_breakdown || {}).exit_model || {}).data_ready) ? ((((detail.factor_breakdown || {}).exit_model || {}).score ?? 0).toFixed(2) + ' \u00b7 ' + ((((detail.factor_breakdown || {}).exit_model || {}).exit_flag) ?? 'hold')) : 'Unavailable from current provider snapshot'}`)}
        ${detailSection('Source state', `${detail.data_source ?? 'unknown'} \u00b7 freshness ${detail.freshness_label ?? 'unknown'} \u00b7 age ${humanAge(detail.age_seconds ?? 0)} \u00b7 state ${detail.state ?? 'ready'}`)}
        ${(() => {
          const rs = ((detail.factor_breakdown || {}).market || {}).regulatory_signal;
          if (!rs) return detailSection('Regulatory signal', '<span class="muted">No active SEC/insider signal within staleness window.</span>');
          const delta = Number(rs.applied_delta || 0);
          const sign = delta >= 0 ? '+' : '';
          const cls = delta >= 0 ? 'pos' : 'neg';
          return detailSection('Regulatory signal', `<span class="reg-delta ${cls}">${sign}${delta.toFixed(2)} pts applied</span> \u00b7 weight ${(rs.weight || 0).toFixed(2)} \u00b7 ${rs.event_count} event${rs.event_count === 1 ? '' : 's'}<br><span class="muted">${rs.reason || ''}${rs.staleness_days != null ? ` (freshest ${Number(rs.staleness_days).toFixed(1)}d ago)` : ''}</span><br><a href="/regulatory.html" target="_blank" rel="noopener" style="color:#5eead4">Open regulatory monitor \u2192</a>`);
        })()}
      </div>`;
    // Wire Phase 5 buttons after the panel is rendered
    const refreshBtn = byId('manualRefreshBtn');
    if (refreshBtn) refreshBtn.addEventListener('click', () => manualRefreshSymbol(symbol));
    // Share widget wiring — toggles the share menu, populates share URLs
    // + a rich metrics summary drawn from the CURRENT detail payload.
    wireDetailShareWidget(symbol, detail);
    const btBtn = byId('backtestBtn');
    if (btBtn) btBtn.addEventListener('click', () => runBacktest(symbol));
    // Phase 26.41: per-horizon prediction buttons.  Each one carries
    // a `data-horizon` attribute (1h/5h/10h/1d/10d) that the handler
    // translates into the right `forward_hours` or `forward_days`
    // query-string param.
    ['predictBtn1h', 'predictBtn5h', 'predictBtn10h', 'predictBtn1d', 'predictBtn10d'].forEach((id) => {
      const b = byId(id);
      if (b) b.addEventListener('click', () => runPrediction(symbol, b.getAttribute('data-horizon') || '10d'));
    });
    // Phase 26.42: next-day open direction is its own dedicated path.
    const ndoBtn = byId('predictBtnNDO');
    if (ndoBtn) ndoBtn.addEventListener('click', () => runNextDayOpenDirection(symbol));

    // ----------------------------------------------------------------
    // Phase 26.43: re-inject previously-rendered prediction +
    // backtest cards.  The live-tick refresh re-writes detailBody
    // from scratch every 2 s, which (before this fix) wiped any
    // prediction the user had just generated.  We persist the
    // last-rendered HTML keyed by symbol and paint it back into the
    // fresh DOM here.  When the user clicks a different row,
    // `state.cachedPredictionCard.symbol` won't match and we leave
    // the divs empty, which is the correct behaviour.
    // ----------------------------------------------------------------
    const cachedPred = state.cachedPredictionCard;
    const predOut = byId('predictionResults');
    if (predOut && cachedPred && cachedPred.symbol === symbol) {
      predOut.innerHTML = cachedPred.html;
      const saveBtn = byId('savePredictionBtn');
      if (saveBtn) saveBtn.addEventListener('click', savePrediction);
    }
    const cachedBacktest = state.cachedBacktestCard;
    const backOut = byId('backtestResults');
    if (backOut && cachedBacktest && cachedBacktest.symbol === symbol) {
      backOut.innerHTML = cachedBacktest.html;
    }

    // Phase 26.43: visible live-tick indicator.  In the leveraged
    // variant the timer fires every 2 s but the underlying snapshot
    // values often barely move tick-to-tick — the user has no way
    // to know the panel is actually refreshing.  Stamp a counter +
    // timestamp into the Symbol card so they can SEE the refreshes.
    if (state.variant && state.variant.live_tick_enabled
        && state.detailLiveSymbol === symbol) {
      state.detailLiveTickCount = (state.detailLiveTickCount || 0) + 1;
      const symCard = document.querySelector('#detailBody .detail-card .eyebrow');
      if (symCard) {
        const tickStamp = new Date().toLocaleTimeString([], {hour12: false});
        // Append a small visual cue (won't clobber the existing label).
        symCard.innerHTML = `Symbol <span class="tick-indicator" title="Live-tick refresh counter">tick #${state.detailLiveTickCount} @ ${tickStamp}</span>`;
      }
    }
    // Phase 26.45: explicitly signal success so the finally-block
    // circuit-breaker reset only fires when the whole try-block
    // completed without throwing.
    state._detailLoadSucceeded = true;
    // Details-panel stability audit: track refresh passes + last-good time.
    state.detailAudit.ticks += 1;
    state.detailAudit.rebuilds += 1;
    state.detailAudit.lastGoodUtc = new Date().toISOString();
    if (state.detailAudit.ticks % 30 === 0) {
      console.info('[detail-audit]', JSON.stringify(state.detailAudit));
    }
  } catch (err) {
    console.error('[loadDetail] failed:', err);
    // Details-panel stability audit: count render/refresh failures so
    // destabilizations are attributable (data shape vs refresh vs state).
    state.detailAudit.ticks += 1;
    state.detailAudit.failures += 1;
    console.warn('[detail-audit] refresh failure', state.detailAudit.failures, 'lastGood:', state.detailAudit.lastGoodUtc);
    // Phase 26.45: circuit-breaker for the detail-panel live-tick.
    // After 3 consecutive failures (typically a backend lockup or
    // overload) we pause the auto-refresh for 30 s and show a
    // dedicated "connection paused" pill instead of leaving the
    // user with raw "TypeError: Failed to fetch" on every tick.
    state.detailLiveFailureStreak = (state.detailLiveFailureStreak || 0) + 1;
    const detailBody = byId('detailBody');
    if (state.detailLiveFailureStreak >= 3) {
      const backoffMs = 30000;
      state.detailLiveBackoffUntil = Date.now() + backoffMs;
      if (detailBody) {
        detailBody.innerHTML = `
          <div class="detail-card">
            <div class="eyebrow">${symbol} \u00b7 connection paused</div>
            <div class="add-status is-err">
              Backend unreachable after ${state.detailLiveFailureStreak} consecutive tries
              (last error: ${String(err).slice(0, 140)}).<br>
              <strong>Auto-refresh paused for 30 s.</strong>
              The scanner is likely catching up after a busy cycle \u2014
              the panel will start refreshing automatically once the
              backend responds again.  Click any row to retry immediately.
            </div>
          </div>`;
      }
    } else if (detailBody) {
      // First and second failures: keep the existing detail visible if
      // we have one cached, otherwise show a softer "retrying" message.
      const haveDetail = detailBody.querySelector('.detail-card');
      if (!haveDetail) {
        detailBody.textContent = `Detail load failed (try ${state.detailLiveFailureStreak}/3 \u2014 retrying): ${err}`;
      } else {
        // Append a small "retrying" pill into the symbol card eyebrow.
        const eyebrow = detailBody.querySelector('.detail-card .eyebrow');
        if (eyebrow) {
          const existing = eyebrow.querySelector('.retry-pill');
          if (existing) existing.remove();
          const pill = document.createElement('span');
          pill.className = 'retry-pill';
          pill.title = `Last refresh failed (try ${state.detailLiveFailureStreak}/3): ${err}`;
          pill.textContent = `retry ${state.detailLiveFailureStreak}/3`;
          eyebrow.appendChild(pill);
        }
      }
    }
  } finally {
    // Phase 26.45: success path clears the failure streak so the
    // breaker doesn't trip on transient blips.  We can detect "success"
    // by the absence of an exception bubbling out of the try block,
    // which is exactly what `finally` after `catch` gives us if the
    // catch didn't re-throw — i.e. we need a separate flag.  Set the
    // flag at the end of the try block above (the renderResults call
    // is the last meaningful thing) and read it here.
    if (state._detailLoadSucceeded) {
      state.detailLiveFailureStreak = 0;
      state.detailLiveBackoffUntil = 0;
      state._detailLoadSucceeded = false;  // reset for next call
    }
  }
}

async function runSearch(query) {
  const box = byId('searchResults');
  if (!box) return;
  if (!query.trim()) {
    box.innerHTML = '';
    return;
  }
  try {
    const payload = await fetchJson(`${state.apiBase}/search/symbols?q=${encodeURIComponent(query)}`);
    box.innerHTML = (payload.results || []).map((row) => `
      <button type="button" data-symbol="${row.symbol}">
        <div>${row.symbol} · ${row.name}</div>
        <div class="jump-hint">Open detail and jump in table</div>
      </button>`).join('');
    box.querySelectorAll('button[data-symbol]').forEach((btn) => btn.addEventListener('click', async () => {
      const symbol = btn.dataset.symbol;
      await loadDetail(symbol);
      jumpToSymbol(symbol);
    }));
  } catch (err) {
    console.error(err);
    box.innerHTML = `<div class="rating-meta">Search failed.</div>`;
  }
}

async function refreshActiveScanPool(forcedMarket = null) {
  const market = forcedMarket || state.currentMarket;
  const pool = state.marketActivePools[market] || new Map();
  const symbolSet = state.marketActiveSymbols[market] || new Set();
  const symbols = Array.from(symbolSet).slice(0, state.maxActiveRefreshPerPass);
  if (!symbols.length) {
    renderTrackerSummary();
    return;
  }
  let payload;
  try {
    payload = await fetchJson(`${state.apiBase}/active-scan/results?market=${market}&limit=${symbols.length || Math.min(state.activeScanLimit, state.maxActiveRefreshPerPass)}`);
  } catch (err) {
    console.error('Active scan batch failure', err);
    return;
  }
  const refreshedRows = (payload.results || []).filter((row) => symbols.includes(row.symbol));
  const now = new Date().toISOString();
  for (const row of refreshedRows) {
    const marketMap = marketMapFor(market);
    const existingMain = marketMap.get(row.symbol) || {};
    const activeMeta = pool.get(row.symbol) || {};
    const merged = {
      ...existingMain,
      ...row,
      as_of_utc: row.as_of_utc || existingMain.as_of_utc || now,
      age_seconds: Number(row.age_seconds ?? existingMain.age_seconds ?? 0),
      freshness_label: row.freshness_label || existingMain.freshness_label || 'unknown',
      active_scan_cycles: (activeMeta.active_scan_cycles || existingMain.active_scan_cycles || 0) + 1,
      active_scan_last_refresh_utc: now,
      score_revision_utc: row.score_revision_utc || row.as_of_utc || now,
      fresh_rescore: row.data_source !== 'cache',
      active_scan_enabled: true,
      source_state_data_source: row.data_source || existingMain.data_source || 'unknown',
      source_state_freshness_label: row.freshness_label || existingMain.freshness_label || 'unknown',
      source_state_age_seconds: Number(row.age_seconds ?? existingMain.age_seconds ?? 0),
      source_state_state: row.state || existingMain.state || 'ready',
      source_state_provider_note: (((row.factor_breakdown || {}).market || {}).provider_note) || '',
      provider_outcome: row.data_source === 'cache' ? 'cache_fallback' : 'live_success',
    };
    marketMap.set(row.symbol, merged);
    state.marketRowsMap[market] = marketMap;
    state.allRowsMap.set(`${market}:${row.symbol}`, merged);
    if (pool.has(row.symbol)) pool.set(row.symbol, { ...activeMeta, ...merged });
  }
  if (refreshedRows.length) bumpFilterSortCache();
  state.marketActivePools[market] = pool;
  if (state.currentMarket === market) {
    state.activeScanPool = pool;
    state.activeScanPasses += 1;
    state.activeScanLastRunUtc = now;
    state.activeScanLastRefreshCount = refreshedRows.length;
    rebuildActiveScanPool();
    updateTop25Tracker();
    renderResults();
    renderStatus();
  }
}

function scheduleActiveScanPool() {
  if (state.activeScanHandle) clearInterval(state.activeScanHandle);
  state.activeScanHandle = setInterval(refreshActiveScanPool, state.activeScanIntervalMs);
}

function ageTick() {
  const now = Date.now();
  const touchMap = (mp) => {
    for (const [symbol, row] of mp.entries()) {
      const asOf = row.as_of_utc ? Date.parse(row.as_of_utc) : NaN;
      if (!Number.isNaN(asOf)) row.age_seconds = Math.max(0, Math.floor((now - asOf) / 1000));
      mp.set(symbol, row);
    }
  };
  touchMap(activeMarketMap());
  touchMap(activeTrackedMap());
  touchMap(state.activeScanPool);
  state.ageRenderCounter = (state.ageRenderCounter || 0) + 1;
  if (state.ageRenderCounter % 5 === 0) {
    renderStatus();
  }
  refreshVisibleAgeCells();
}

async function refreshCycle(forcedMarket = null) {
  const requestedMarket = forcedMarket || state.currentMarket;
  if (state.isRefreshing && !forcedMarket) return;
  if (state.isRefreshing && forcedMarket && requestedMarket !== state.currentMarket) return;
  state.isRefreshing = true;
  try {
    // PHASE 12 - BROADCAST MIRROR MODE
    // -----------------------------------------------------------------
    // The backend now runs a single background scan loop and stores the
    // latest scored row per (market, symbol) in an in-memory snapshot.
    // Every connected client - host PC, phone, second laptop - just
    // mirrors that snapshot in one HTTP call. No per-client batch
    // sweeping, no duplicate scoring work, and every secondary device
    // sees the SAME rows as the host the moment it connects.
    //
    // The legacy /stocks/results endpoint still exists (manual refresh,
    // detail re-scoring, etc.) but the dashboard's main scan loop now
    // talks exclusively to /api/scan/snapshot.
    // Phase 26.15: keep the snapshot fetch at the default top-N (1500) to
    // bound wire size at ~6 MB per poll. The actual "vanishing rows" bug
    // is fixed below by switching from absence-based to AGE-based pruning,
    // which is independent of the snapshot top-N cutoff.
    const snapUrl = `${state.apiBase}/api/scan/snapshot?market=${encodeURIComponent(requestedMarket)}${state.pviPriority ? '&sort=predicted_volume_intensity' : ''}`;
    const snap = await fetchJson(snapUrl);
    // Phase 26.18 hotfix: if the snapshot endpoint returned a 304, keep
    // the existing rows + meta and just reschedule. Re-rendering on a
    // 304 would no-op anyway (no data changed) but skipping the merge
    // path is cheaper.
    if (snap && snap.__not_modified__) {
      state.resultsFailureCount = 0;
      // Hold the warming cadence; the next tick will revalidate again.
      state.resultsPollMs = state.resultsPollMs || 3000;
      // Phase 26.39: live-tick override still applies on 304 path.
      if (state.variant && state.variant.live_tick_enabled) {
        state.resultsPollMs = state.variant.live_tick_interval_ms || 2000;
      }
      renderStatus();
      return;
    }
    const rows = snap.results || [];

    // Replace (don't accumulate) the per-market row map so symbols that
    // dropped out of the universe between sweeps don't linger forever.
    // We do this only when the snapshot has actually advanced; otherwise
    // we keep what we have so a transient empty response can't blank the
    // UI mid-refresh.
    if (rows.length > 0) {
      const targetMap = requestedMarket === 'crypto'
        ? state.marketTrackedRows.crypto
        : state.marketTrackedRows.stocks;
      // Use mergeResults so all the downstream bookkeeping (tracked
      // top-25 counts, active scan pool, etc.) stays consistent.
      mergeResults(rows, requestedMarket);
      // Phase 26.15: AGE-BASED pruning replaces the previous absence-from-
      // snapshot prune. The old logic deleted any symbol from the master
      // map that wasn't in the latest snapshot top-N, which meant any row
      // scored below the snapshot limit was instantly removed even though
      // the backend bucket still contained it. The new rule keeps rows
      // alive as long as they were last seen within the staleness window
      // (default 6 min, generous enough to survive a full sweep at the
      // current 12k-symbol universe + 2.5 s/batch cadence). Genuinely
      // delisted symbols simply never get refreshed and age out cleanly.
      const masterMap = requestedMarket === 'crypto'
        ? (state.marketMasterRows && state.marketMasterRows.crypto)
        : (state.marketMasterRows && state.marketMasterRows.stocks);
      if (masterMap) {
        const staleMs = 6 * 60 * 1000;
        const cutoffMs = Date.now() - staleMs;
        for (const [sym, row] of Array.from(masterMap.entries())) {
          const lastSeenIso = row.last_seen_utc || row.as_of_utc;
          if (!lastSeenIso) continue;  // never-seen rows are kept (shouldn't happen)
          const lastSeenMs = Date.parse(lastSeenIso);
          if (Number.isFinite(lastSeenMs) && lastSeenMs < cutoffMs) {
            masterMap.delete(sym);
          }
        }
      }
    }

    // Mirror the backend's scan progress into the UI's state so the
    // status bar / progress pill renders the broadcast progress (which
    // is the SAME for all connected devices) instead of per-client.
    state.lastScanProgress = {
      batch_index: snap.current_batch_index,
      batch_size: 0,
      loaded_rows: snap.rows_scored,
      universe_size: snap.universe_size,
      evaluations_ever: snap.evaluations_ever || 0,
      // Phase 25: monotonic per-sweep counter (0 → universe_size → 0).
      // This is what the UNIVERSE headline number reads from now so the
      // user sees the scanner walk the full universe every sweep
      // instead of plateauing at the bucket cap.
      current_sweep_scanned: snap.current_sweep_scanned || 0,
      scan_state: snap.rows_scored > 0 ? 'ok' : 'warming',
    };
    state.marketTotalBatches[requestedMarket] = Math.max(1, Number(snap.total_batches || 1));
    state.marketBatchIndex[requestedMarket] = Number(snap.current_batch_index || 0);
    if (state.currentMarket === requestedMarket) {
      state.totalBatches = state.marketTotalBatches[requestedMarket];
      state.batchIndex = state.marketBatchIndex[requestedMarket];
    }

    rebuildActiveScanPool();
    updateTop25Tracker();

    // System status is independent of the snapshot; poll it on the same
    // cadence so the provider / cache / regulatory pills stay live.
    try {
      state.status = await fetchJson(`${state.apiBase}/system/status?batch=${state.batchIndex || 0}`);
    } catch (e) { /* keep previous status */ }

    state.resultsFailureCount = 0;

    // Cadence: poll the snapshot every 3 s while the universe is still
    // warming (rows_scored < universe_size) and every 5 s once a full
    // sweep has completed. Keeps the UI lively without hammering the
    // server, and is independent of how many devices are connected.
    const warming = (snap.rows_scored || 0) < (snap.universe_size || 1);
    state.resultsPollMs = warming ? 3000 : 5000;
    // Phase 26.39: leveraged-variant live-tick mode pins the cadence to
    // the variant.json-driven interval (typically 2 s) regardless of
    // warming state.  The universe is only ~838 symbols so the server
    // can sustain this without ever sweeping more than ~30 s behind
    // realtime — and the top-10 backend priority lane keeps the high-
    // score rows fresh on the same cadence.
    if (state.variant && state.variant.live_tick_enabled) {
      state.resultsPollMs = state.variant.live_tick_interval_ms || 2000;
    }

    // Phase 26.18.d hotfix: if the user is parked on the 'tracked' view
    // but the tracked-rows map is empty AND main has data, auto-switch
    // back to main. This was the "list empty but status reports 2000+
    // symbols" symptom: the cookie-persisted view was 'tracked' from a
    // prior session, so visibleRows() pulled from an empty trackedMap
    // even though the snapshot was healthy.
    if (state.currentView === 'tracked'
        && state.trackedRowsMap.size === 0
        && filteredMainRows().length > 0) {
      console.warn('[refreshCycle] tracked view empty + main has rows; switching to main');
      setView('main');
      // setView already calls renderResults; renderStatus next.
      renderStatus();
      return;
    }

    // Phase 26.18.d: each render step gets its own try/catch so a single
    // bad row in the new Tier 3.3 cheap-row shape can't blank the UI on
    // every poll. If renderResults throws, we still want renderStatus
    // to fire so the user sees fresh provider health, and vice-versa.
    let renderError1 = null, renderError2 = null;
    try { renderResults(); } catch (e) { renderError1 = e; console.error('renderResults threw:', e); }
    try { renderStatus();  } catch (e) { renderError2 = e; console.error('renderStatus threw:',  e); }
    if (renderError1 || renderError2) {
      // Surface a softer, instructive error message so users can
      // copy-paste it back to us for diagnosis.
      const which = [];
      if (renderError1) which.push(`renderResults: ${renderError1.message || renderError1}`);
      if (renderError2) which.push(`renderStatus: ${renderError2.message || renderError2}`);
      renderError(`UI render warning - ${which.join(' | ')}. Open browser DevTools console for stack.`);
    }
  } catch (err) {
    // Phase 26.18.d: keep the existing rendered rows visible (don't blank
    // them) and report the SPECIFIC step that threw so debugging is
    // a copy-paste away.
    console.error('[refreshCycle] fetch/merge failed:', err);
    state.resultsFailureCount += 1;
    state.resultsPollMs = Math.min(30000, 4000 * (2 ** Math.min(state.resultsFailureCount, 3)));
    const detail = (err && (err.message || String(err))) || 'unknown error';
    renderError(`Results unavailable, showing last good snapshot - ${detail}`);
  } finally {
    state.isRefreshing = false;
    scheduleRefresh();
  }
}

function scheduleRefresh() {
  if (state.refreshHandle) clearTimeout(state.refreshHandle);
  state.refreshHandle = setTimeout(refreshCycle, state.resultsPollMs);
}

function wireEvents() {
  if (byId('manualRefresh')) byId('manualRefresh').addEventListener('click', refreshCycle);
  if (byId('viewMainButton')) byId('viewMainButton').addEventListener('click', () => { state.currentMarket = 'stocks'; state.pageIndex = 0; state.activeScanPool = state.marketActivePools.stocks || new Map(); state.activeScanUniverseSymbols = state.marketActiveSymbols.stocks || new Set(); state.batchIndex = state.marketBatchIndex.stocks || 0; state.totalBatches = state.marketTotalBatches.stocks || 1; setView('main'); renderResults(); refreshCycle('stocks'); });
  if (byId('viewCryptoButton')) byId('viewCryptoButton').addEventListener('click', () => { state.currentMarket = 'crypto'; state.pageIndex = 0; if (Number(state.filters.min_score || 0) > 0) { state.filters.min_score = 0; applyPrefsToControls(); saveUiPrefs(); } state.activeScanPool = state.marketActivePools.crypto || new Map(); state.activeScanUniverseSymbols = state.marketActiveSymbols.crypto || new Set(); state.batchIndex = state.marketBatchIndex.crypto || 0; state.totalBatches = state.marketTotalBatches.crypto || 1; setView('main'); renderResults(); refreshCycle('crypto'); });
  if (byId('viewTrackedButton')) byId('viewTrackedButton').addEventListener('click', () => setView('tracked'));
  if (byId('symbolSearch')) byId('symbolSearch').addEventListener('input', (event) => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => runSearch(event.target.value), 180);
  });
  if (byId('prevPage')) byId('prevPage').addEventListener('click', (e) => { e.stopPropagation(); e.preventDefault(); goToPage(state.pageIndex - 1); });
  if (byId('nextPage')) byId('nextPage').addEventListener('click', (e) => { e.stopPropagation(); e.preventDefault(); goToPage(state.pageIndex + 1); });
  if (byId('applyFilters')) byId('applyFilters').addEventListener('click', () => {
    syncFilterState();
    state.pageIndex = 0;
    updatePresetChips();
    // Invalidate the memoized filter+sort cache so the new filter values take effect immediately.
    bumpFilterSortCache();
    renderResults();
    // Also re-issue a backend fetch so server-side filtering picks up the new params
    // and any newly-arriving rows are filtered correctly.
    refreshCycle();
  });
  if (byId('clearFilters')) byId('clearFilters').addEventListener('click', () => {
    state.filters = {
      preset: '', direction: '', tier: '', min_score: 0, max_exit_risk: 100, exit_flag: '',
      min_institutional_confluence: 0, min_options_positioning: 0,
      institutional_bias_in: '', options_bias_in: '',
      iob_state_in: '', dark_pool_attraction_state_in: '',
      options_gamma_level_in: '', sort_by: '',
      reaction_classification_in: '', dominant_zone_tier_in: '',
      volume_sentiment_bias_in: '', effort_vs_result_in: '',
      min_volume_sentiment_conviction: 0,
    };
    applyPrefsToControls();
    saveUiPrefs();
    refreshCycle();
  });
  if (byId('presetFilter')) byId('presetFilter').addEventListener('change', () => { syncFilterState(); updatePresetChips(); });
  if (byId('directionFilter')) byId('directionFilter').addEventListener('change', syncFilterState);
  if (byId('tierFilter')) byId('tierFilter').addEventListener('change', syncFilterState);
  if (byId('minScoreFilter')) byId('minScoreFilter').addEventListener('input', syncFilterState);
  if (byId('exitFlagFilter')) byId('exitFlagFilter').addEventListener('change', syncFilterState);
  if (byId('maxExitRiskFilter')) byId('maxExitRiskFilter').addEventListener('input', syncFilterState);
  if (byId('minInstitutionalConfluenceFilter')) byId('minInstitutionalConfluenceFilter').addEventListener('input', syncFilterState);
  if (byId('minOptionsPositioningFilter')) byId('minOptionsPositioningFilter').addEventListener('input', syncFilterState);
  if (byId('institutionalBiasFilter')) byId('institutionalBiasFilter').addEventListener('change', syncFilterState);
  if (byId('optionsBiasFilter')) byId('optionsBiasFilter').addEventListener('change', syncFilterState);
  if (byId('iobStateFilter')) byId('iobStateFilter').addEventListener('change', syncFilterState);
  if (byId('darkPoolAttractionFilter')) byId('darkPoolAttractionFilter').addEventListener('change', syncFilterState);
  if (byId('optionsGammaFilter')) byId('optionsGammaFilter').addEventListener('change', syncFilterState);
  if (byId('sortByFilter')) byId('sortByFilter').addEventListener('change', () => { syncFilterState(); refreshCycle(); });
  if (byId('reactionClassificationFilter')) byId('reactionClassificationFilter').addEventListener('change', syncFilterState);
  if (byId('dominantZoneTierFilter')) byId('dominantZoneTierFilter').addEventListener('change', syncFilterState);
  if (byId('volumeSentimentBiasFilter')) byId('volumeSentimentBiasFilter').addEventListener('change', syncFilterState);
  if (byId('effortVsResultFilter')) byId('effortVsResultFilter').addEventListener('change', syncFilterState);
  if (byId('minVolumeSentimentConvictionFilter')) byId('minVolumeSentimentConvictionFilter').addEventListener('input', syncFilterState);
  // Scanner-context filter controls
  if (byId('pviPriorityToggle')) byId('pviPriorityToggle').addEventListener('change', () => { syncFilterState(); refreshCycle(); });
  if (byId('minPviFilter')) byId('minPviFilter').addEventListener('input', syncFilterState);
  if (byId('pviBucketFilter')) byId('pviBucketFilter').addEventListener('change', syncFilterState);
  if (byId('minShortPressureFilter')) byId('minShortPressureFilter').addEventListener('input', syncFilterState);
  if (byId('shortPressureLabelFilter')) byId('shortPressureLabelFilter').addEventListener('change', syncFilterState);
  if (byId('maxDteFilter')) byId('maxDteFilter').addEventListener('input', syncFilterState);
  if (byId('expirationRiskOnly')) byId('expirationRiskOnly').addEventListener('change', syncFilterState);
  // Phase 5: add-symbol input + button
  if (byId('addSymbolButton')) byId('addSymbolButton').addEventListener('click', handleAddSymbol);
  if (byId('addSymbolInput')) byId('addSymbolInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') handleAddSymbol(); });
  refreshUserAddedList();
  document.querySelectorAll('.preset-chip').forEach((btn) => btn.addEventListener('click', () => applyPreset(btn.dataset.preset || '')));
}

// Phase 15: API key Settings panel wiring.  Loads which providers are
// configured on first render and lets the user save/clear keys without
// reloading.  The raw key value is NEVER read back from the server -
// we only ask for the masked preview ("fhn1...3xz") to confirm storage.
async function loadApiKeyStatuses() {
  try {
    const [list, preview] = await Promise.all([
      fetchJson(`${state.apiBase}/api/api-keys`),
      fetchJson(`${state.apiBase}/api/api-keys/preview`),
    ]);
    const cfg = (list && list.configured) || {};
    document.querySelectorAll('.api-key-row [data-provider]').forEach((el) => {
      const provider = el.getAttribute('data-provider');
      const statusEl = document.getElementById('apiKeyStatus' + provider.charAt(0).toUpperCase() + provider.slice(1).replace('data','Data').replace('vantage','Vantage'));
      if (!statusEl) return;
      if (cfg[provider]) {
        statusEl.textContent = preview && preview[provider] ? preview[provider] : 'configured';
        statusEl.classList.add('configured');
      } else {
        statusEl.textContent = 'not set';
        statusEl.classList.remove('configured');
      }
    });
  } catch (err) {
    // Non-fatal: panel just shows defaults.
  }
}

function wireApiKeyPanel() {
  document.querySelectorAll('[data-action="save-api-key"]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const provider = btn.getAttribute('data-provider');
      const input = document.querySelector(`.api-key-row input[data-provider="${provider}"]`);
      if (!input) return;
      const value = (input.value || '').trim();
      try {
        const resp = await fetch(`${state.apiBase}/api/api-keys/${provider}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        input.value = '';   // never leave the raw key sitting in the input
        await loadApiKeyStatuses();
      } catch (err) {
        alert(`Failed to save ${provider} key: ${err.message || err}`);
      }
    });
  });
}

// =========================================================================
// Phase 26.50 bugfix — toggle wiring helpers.
//
// These were previously inlined inside the `live_tick_enabled` branch of
// the variant-fetch handler, which meant they only ran in the leveraged
// build AND only after a successful variant fetch.  That left the
// always-visible HTML checkboxes (`useLabMode`, `blendLabIntoRanking`,
// `useStrategyMode`, `blendStrategyIntoRanking`, Future Mode, reset
// buttons) without click handlers in any other code path — clicking
// them appeared to do nothing.
//
// Solution: split into three idempotent wiring helpers and run them
// =========================================================================
// Phase 26.66 — toggleable universe groups panel.
async function loadUniversesPanel() {
  const panel = byId('universesPanel');
  if (!panel) return;
  try {
    const [stocks, crypto] = await Promise.all([
      fetchJson(`${state.apiBase}/api/universes?market=stocks`),
      fetchJson(`${state.apiBase}/api/universes?market=crypto`),
    ]);
    renderUniversesPanel(stocks, crypto);
  } catch (_) {
    panel.innerHTML = '<div class="engine-toggle-hint">Universe groups unavailable.</div>';
  }
}

function _universeGroupRow(g) {
  const cnt = Number(g.count || 0).toLocaleString();
  return `<label class="universe-group ${g.active ? 'is-active' : ''}" data-testid="universe-group-${g.key}">
    <input type="checkbox" data-universe-key="${g.key}" data-universe-market="${g.market}" ${g.active ? 'checked' : ''} data-testid="universe-toggle-${g.key}">
    <span class="universe-group-label">${esc(g.label)}</span>
    <span class="universe-group-count">${cnt}</span>
  </label>`;
}

function renderUniversesPanel(stocks, crypto) {
  const panel = byId('universesPanel');
  if (!panel) return;
  const stockGroups = (stocks && stocks.groups) || [];
  const cryptoGroups = (crypto && crypto.groups) || [];
  // Group stock shards by their exchange label for readability.
  const byExch = {};
  const order = [];
  stockGroups.forEach((g) => {
    if (!(g.exchange in byExch)) { byExch[g.exchange] = []; order.push(g.exchange); }
    byExch[g.exchange].push(g);
  });
  let html = '';
  order.forEach((exch) => {
    html += `<div class="universe-exch-head">${esc(exch)}</div>`;
    html += byExch[exch].map(_universeGroupRow).join('');
  });
  if (cryptoGroups.length) {
    html += `<div class="universe-exch-head">Crypto market</div>`;
    html += cryptoGroups.map(_universeGroupRow).join('');
  }
  panel.innerHTML = html;
  // Summary line.
  const sumEl = byId('universesSummary');
  if (sumEl) {
    const sActive = stockGroups.filter((g) => g.active);
    const cActive = cryptoGroups.filter((g) => g.active);
    const sSyms = sActive.reduce((a, g) => a + (g.count || 0), 0);
    const cSyms = cActive.reduce((a, g) => a + (g.count || 0), 0);
    sumEl.textContent = `${sActive.length} stock · ${cActive.length} crypto · ${(sSyms + cSyms).toLocaleString()} symbols`;
  }
}

async function toggleUniverseGroup(key, market, active) {
  try {
    await fetch(`${state.apiBase}/api/universes/toggle`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ key, market, active }),
    });
  } catch (_) { /* best-effort */ }
  // Toggling clears the market snapshot server-side — refresh the panel
  // (updated counts/active flags) and the leaderboard.
  await loadUniversesPanel();
  bumpFilterSortCache();
  // UX: when the user activates a crypto universe while viewing the
  // stocks tab, auto-switch to the Crypto tab so the list they see
  // reflects the market they just enabled.  Previously the user would
  // toggle on Crypto — Core / Extended, expect data to appear, but
  // stay on the Stocks tab (which is empty if they also cleared the
  // stock universes) and think "crypto isn't populating".  Mirror the
  // opposite path too: if they activate a stock universe while on the
  // Crypto tab, switch back to stocks.
  const targetBtn = market === 'crypto'
    ? byId('viewCryptoButton')
    : byId('viewMainButton');
  if (active && targetBtn && state.currentMarket !== market) {
    try { targetBtn.click(); return; } catch (_) { /* fall through */ }
  }
  try { renderResults(); } catch (_) {}
}

function wireUniversesPanel() {
  const panel = byId('universesPanel');
  if (!panel || panel.__mrdWired) return;
  panel.__mrdWired = true;
  panel.addEventListener('change', (ev) => {
    const cb = ev.target;
    if (!cb || cb.getAttribute('data-universe-key') === null) return;
    const key = cb.getAttribute('data-universe-key');
    const market = cb.getAttribute('data-universe-market') || 'stocks';
    toggleUniverseGroup(key, market, !!cb.checked);
  });
  loadUniversesPanel();
}

// =========================================================================
// helper checks for the element first and bails if missing, so the
// build that hides certain blocks is still safe.
// =========================================================================
function wireFutureModeControls() {
  const futureCb = byId('useFutureMode');
  if (futureCb && !futureCb.__mrdWired) {
    futureCb.__mrdWired = true;
    futureCb.checked = !!state.futureMode;
    futureCb.addEventListener('change', (ev) => {
      state.futureMode = !!ev.target.checked;
      saveUiPrefs();
      bumpFilterSortCache();
      renderResults();
      if (state.selectedSymbol) loadDetail(state.selectedSymbol);
    });
  }
  const futureHorizonSel = byId('futureHorizonSelect');
  if (futureHorizonSel && !futureHorizonSel.__mrdWired) {
    futureHorizonSel.__mrdWired = true;
    futureHorizonSel.value = state.futureHorizon || '1h_hold';
    futureHorizonSel.addEventListener('change', (ev) => {
      state.futureHorizon = ev.target.value || '1h_hold';
      saveUiPrefs();
      bumpFilterSortCache();
      renderResults();
      if (state.selectedSymbol) loadDetail(state.selectedSymbol);
    });
  }
  const fmFilterButtons = document.querySelectorAll('[data-fm-filter]');
  fmFilterButtons.forEach((btn) => {
    if (btn.__mrdWired) return;
    btn.__mrdWired = true;
    if (btn.getAttribute('data-fm-filter') === (state.futureFilter || 'all')) {
      btn.classList.add('active');
    } else {
      btn.classList.remove('active');
    }
    btn.addEventListener('click', () => {
      state.futureFilter = btn.getAttribute('data-fm-filter') || 'all';
      fmFilterButtons.forEach((b) => b.classList.toggle('active', b === btn));
      saveUiPrefs();
      bumpFilterSortCache();
      renderResults();
    });
  });
  const fmIntensityButtons = document.querySelectorAll('[data-fm-intensity]');
  fmIntensityButtons.forEach((btn) => {
    if (btn.__mrdWired) return;
    btn.__mrdWired = true;
    if (btn.getAttribute('data-fm-intensity') === (state.futureIntensity || 'all')) {
      btn.classList.add('active');
    } else {
      btn.classList.remove('active');
    }
    btn.addEventListener('click', () => {
      state.futureIntensity = btn.getAttribute('data-fm-intensity') || 'all';
      fmIntensityButtons.forEach((b) => b.classList.toggle('active', b === btn));
      saveUiPrefs();
      bumpFilterSortCache();
      renderResults();
    });
  });
  // Phase 26.65 — Bull × Bull priority toggle.
  const bbBtn = byId('bullBullPriorityBtn');
  if (bbBtn && !bbBtn.__mrdWired) {
    bbBtn.__mrdWired = true;
    bbBtn.classList.toggle('active', !!state.bullBullPriority);
    bbBtn.addEventListener('click', () => {
      state.bullBullPriority = !state.bullBullPriority;
      bbBtn.classList.toggle('active', state.bullBullPriority);
      saveUiPrefs();
      bumpFilterSortCache();
      renderResults();
    });
  }
  // Phase 26.65 — Reality-Breaker overall-rating list filter.
  const rbFilterButtons = document.querySelectorAll('[data-rb-filter]');
  rbFilterButtons.forEach((btn) => {
    if (btn.__mrdWired) return;
    btn.__mrdWired = true;
    btn.classList.toggle('active', btn.getAttribute('data-rb-filter') === (state.rbFilter || 'all'));
    btn.addEventListener('click', () => {
      state.rbFilter = btn.getAttribute('data-rb-filter') || 'all';
      rbFilterButtons.forEach((b) => b.classList.toggle('active', b === btn));
      saveUiPrefs();
      bumpFilterSortCache();
      renderResults();
    });
  });
  // Phase 26.68 — 7-timeframe consensus quick-filter buttons.
  const consensusFilterButtons = document.querySelectorAll('[data-consensus-filter]');
  consensusFilterButtons.forEach((btn) => {
    if (btn.__mrdWired) return;
    btn.__mrdWired = true;
    btn.classList.toggle('active', btn.getAttribute('data-consensus-filter') === (state.consensusFilter || 'all'));
    btn.addEventListener('click', () => {
      state.consensusFilter = btn.getAttribute('data-consensus-filter') || 'all';
      consensusFilterButtons.forEach((b) => b.classList.toggle('active', b === btn));
      saveUiPrefs();
      bumpFilterSortCache();
      renderResults();
    });
  });
  // Phase 26.68 — clickable 7-TF header cycles consensus sort off→desc→asc.
  const csHeader = byId('consensusSortHeader');
  if (csHeader && !csHeader.__mrdWired) {
    csHeader.__mrdWired = true;
    const reflectCsSort = () => {
      const ind = byId('consensusSortIndicator');
      if (ind) ind.textContent = state.consensusSort === 'desc' ? ' \u25BC' : state.consensusSort === 'asc' ? ' \u25B2' : '';
      csHeader.classList.toggle('is-sorted', state.consensusSort !== 'off');
    };
    reflectCsSort();
    csHeader.addEventListener('click', () => {
      state.consensusSort = state.consensusSort === 'off' ? 'desc'
        : state.consensusSort === 'desc' ? 'asc' : 'off';
      reflectCsSort();
      saveUiPrefs();
      bumpFilterSortCache();
      renderResults();
    });
  }
}

function wireLabAndStrategyControls() {
  // -----------------------------------------------------------------
  // Lab Mode parent + Blend
  // -----------------------------------------------------------------
  const labCb = byId('useLabMode');
  if (labCb && !labCb.__mrdWired) {
    labCb.__mrdWired = true;
    labCb.checked = !!state.useLabMode;
    labCb.addEventListener('change', (ev) => {
      state.useLabMode = !!ev.target.checked;
      // If the user turns the parent OFF, the blend is meaningless;
      // turn it off too so the UI doesn't lie about its effective state.
      if (!state.useLabMode && state.blendLabIntoRanking) {
        state.blendLabIntoRanking = false;
        const lbc = byId('blendLabIntoRanking');
        if (lbc) lbc.checked = false;
      }
      saveUiPrefs();
      bumpFilterSortCache();
      renderResults();
      if (state.selectedSymbol) loadDetail(state.selectedSymbol);
    });
  }
  const labBlendCb = byId('blendLabIntoRanking');
  if (labBlendCb && !labBlendCb.__mrdWired) {
    labBlendCb.__mrdWired = true;
    labBlendCb.checked = !!state.blendLabIntoRanking;
    labBlendCb.addEventListener('change', (ev) => {
      const checked = !!ev.target.checked;
      state.blendLabIntoRanking = checked;
      // Auto-cascade: blending lab signals requires Lab Mode + Future
      // Mode to be on for the blend to have any effect.  Rather than
      // silently doing nothing (the previous symptom), enable the
      // prerequisites and reflect that in the UI.
      if (checked) {
        if (!state.useLabMode) {
          state.useLabMode = true;
          const parent = byId('useLabMode');
          if (parent) parent.checked = true;
        }
        if (!state.futureMode) {
          state.futureMode = true;
          const fm = byId('useFutureMode');
          if (fm) fm.checked = true;
        }
      }
      saveUiPrefs();
      bumpFilterSortCache();
      renderResults();
      if (state.selectedSymbol) loadDetail(state.selectedSymbol);
    });
  }
  // -----------------------------------------------------------------
  // Strategy Tier parent + Blend
  // -----------------------------------------------------------------
  const stratCb = byId('useStrategyMode');
  if (stratCb && !stratCb.__mrdWired) {
    stratCb.__mrdWired = true;
    stratCb.checked = !!state.useStrategyMode;
    stratCb.addEventListener('change', (ev) => {
      state.useStrategyMode = !!ev.target.checked;
      if (!state.useStrategyMode && state.blendStrategyIntoRanking) {
        state.blendStrategyIntoRanking = false;
        const sbc = byId('blendStrategyIntoRanking');
        if (sbc) sbc.checked = false;
      }
      saveUiPrefs();
      bumpFilterSortCache();
      renderResults();
      if (state.selectedSymbol) loadDetail(state.selectedSymbol);
    });
  }
  const stratBlendCb = byId('blendStrategyIntoRanking');
  if (stratBlendCb && !stratBlendCb.__mrdWired) {
    stratBlendCb.__mrdWired = true;
    stratBlendCb.checked = !!state.blendStrategyIntoRanking;
    stratBlendCb.addEventListener('change', (ev) => {
      const checked = !!ev.target.checked;
      state.blendStrategyIntoRanking = checked;
      if (checked) {
        if (!state.useStrategyMode) {
          state.useStrategyMode = true;
          const parent = byId('useStrategyMode');
          if (parent) parent.checked = true;
        }
        if (!state.futureMode) {
          state.futureMode = true;
          const fm = byId('useFutureMode');
          if (fm) fm.checked = true;
        }
      }
      saveUiPrefs();
      bumpFilterSortCache();
      renderResults();
      if (state.selectedSymbol) loadDetail(state.selectedSymbol);
    });
  }

  // ===================================================================
  // Phase 26.60 — Predictive Expansion Pack toggles.
  // Pattern is identical to Lab / Strategy: parent + blend, with
  // cascading semantics (blend ON auto-enables parent + Future Mode;
  // parent OFF auto-disables blend).
  // ===================================================================
  /** Wire a parent/blend pair using the standard cascade pattern. */
  function _wirePair(parentId, blendId, parentKey, blendKey) {
    const parent = byId(parentId);
    if (parent && !parent.__mrdWired) {
      parent.__mrdWired = true;
      parent.checked = !!state[parentKey];
      parent.addEventListener('change', (ev) => {
        state[parentKey] = !!ev.target.checked;
        if (!state[parentKey] && state[blendKey]) {
          state[blendKey] = false;
          const bcb = byId(blendId);
          if (bcb) bcb.checked = false;
        }
        saveUiPrefs();
        bumpFilterSortCache();
        renderResults();
        if (state.selectedSymbol) loadDetail(state.selectedSymbol);
      });
    }
    const blend = byId(blendId);
    if (blend && !blend.__mrdWired) {
      blend.__mrdWired = true;
      blend.checked = !!state[blendKey];
      blend.addEventListener('change', (ev) => {
        const checked = !!ev.target.checked;
        state[blendKey] = checked;
        if (checked) {
          if (!state[parentKey]) {
            state[parentKey] = true;
            const p = byId(parentId);
            if (p) p.checked = true;
          }
          if (!state.futureMode) {
            state.futureMode = true;
            const fm = byId('useFutureMode');
            if (fm) fm.checked = true;
          }
        }
        saveUiPrefs();
        bumpFilterSortCache();
        renderResults();
        if (state.selectedSymbol) loadDetail(state.selectedSymbol);
      });
    }
  }
  _wirePair('useStrategyV2Mode', 'blendStrategyV2IntoRanking',
            'useStrategyV2Mode', 'blendStrategyV2IntoRanking');
  _wirePair('useRegimeRiskMode', 'blendRegimeRiskIntoRanking',
            'useRegimeRiskMode', 'blendRegimeRiskIntoRanking');
  _wirePair('useMlOverlayMode', 'blendMlOverlayIntoRanking',
            'useMlOverlayMode', 'blendMlOverlayIntoRanking');

  // Liquidity Kelly factor — single toggle (no parent, just a blend).
  const liqKellyCb = byId('blendLiqKellyFactor');
  if (liqKellyCb && !liqKellyCb.__mrdWired) {
    liqKellyCb.__mrdWired = true;
    liqKellyCb.checked = !!state.blendLiqKellyFactor;
    liqKellyCb.addEventListener('change', (ev) => {
      state.blendLiqKellyFactor = !!ev.target.checked;
      if (state.blendLiqKellyFactor && !state.futureMode) {
        state.futureMode = true;
        const fm = byId('useFutureMode');
        if (fm) fm.checked = true;
      }
      saveUiPrefs();
      bumpFilterSortCache();
      renderResults();
      if (state.selectedSymbol) loadDetail(state.selectedSymbol);
    });
  }

  // -----------------------------------------------------------------
  // Advanced Experimental Mode (reality_breaker master toggle).
  // When the master is OFF every child reality_breaker toggle MUST
  // also be OFF — we enforce on every change to prevent any
  // configuration drift.
  // -----------------------------------------------------------------
  const advCb = byId('advancedExperimentalMode');
  if (advCb && !advCb.__mrdWired) {
    advCb.__mrdWired = true;
    advCb.checked = !!state.advancedExperimentalMode;
    advCb.addEventListener('change', (ev) => {
      const checked = !!ev.target.checked;
      state.advancedExperimentalMode = checked;
      if (!checked) {
        // Cascade-disable EVERY reality_breaker child.
        const childKeys = [
          'showLocalCausalCone', 'showQuantumPathInterference',
          'showLocalLyapunov', 'showTemporalRenormalization',
          'blendRealityBreakerIntoRanking',
          'advancedExperimentalUnlocked',
        ];
        for (const k of childKeys) {
          state[k] = false;
          const id = ({
            showLocalCausalCone: 'showLocalCausalCone',
            showQuantumPathInterference: 'showQuantumPathInterference',
            showLocalLyapunov: 'showLocalLyapunov',
            showTemporalRenormalization: 'showTemporalRenormalization',
            blendRealityBreakerIntoRanking: 'blendRealityBreakerIntoRanking',
            advancedExperimentalUnlocked: 'advancedExperimentalUnlocked',
          })[k];
          const node = byId(id);
          if (node) node.checked = false;
        }
        const banner = byId('advExpWarning');
        if (banner) banner.classList.add('hidden');
      } else {
        const banner = byId('advExpWarning');
        if (banner) banner.classList.remove('hidden');
      }
      saveUiPrefs();
      bumpFilterSortCache();
      renderResults();
      if (state.selectedSymbol) loadDetail(state.selectedSymbol);
    });
  }

  // Reality_breaker child toggles — each refuses to enable unless the
  // master is on.  Wire identically.
  function _wireRbChild(id, stateKey) {
    const node = byId(id);
    if (node && !node.__mrdWired) {
      node.__mrdWired = true;
      node.checked = !!state[stateKey];
      node.addEventListener('change', (ev) => {
        const checked = !!ev.target.checked;
        if (checked && !state.advancedExperimentalMode) {
          // Master OFF — revoke immediately, surface a console warning.
          // (The UI also disables the child <input> via CSS:disabled
          // attribute when master is OFF — this is belt + suspenders.)
          ev.target.checked = false;
          state[stateKey] = false;
          return;
        }
        state[stateKey] = checked;
        saveUiPrefs();
        bumpFilterSortCache();
        renderResults();
        if (state.selectedSymbol) loadDetail(state.selectedSymbol);
      });
    }
  }
  _wireRbChild('showLocalCausalCone',          'showLocalCausalCone');
  _wireRbChild('showQuantumPathInterference',  'showQuantumPathInterference');
  _wireRbChild('showLocalLyapunov',            'showLocalLyapunov');
  _wireRbChild('showTemporalRenormalization',  'showTemporalRenormalization');
  _wireRbChild('blendRealityBreakerIntoRanking', 'blendRealityBreakerIntoRanking');

  // Phase 26.61c — Unlocked Experimental Mode toggle.  Same gating
  // as the reality_breaker children (refuses to enable unless master
  // is on), PLUS a confirmation dialog on first enable.
  const unlockedCb = byId('advancedExperimentalUnlocked');
  if (unlockedCb && !unlockedCb.__mrdWired) {
    unlockedCb.__mrdWired = true;
    unlockedCb.checked = !!state.advancedExperimentalUnlocked;
    unlockedCb.addEventListener('change', (ev) => {
      const checked = !!ev.target.checked;
      if (checked && !state.advancedExperimentalMode) {
        ev.target.checked = false;
        state.advancedExperimentalUnlocked = false;
        alert('Unlocked Experimental Mode requires "Advanced Experimental Mode" to be enabled first.');
        return;
      }
      if (checked && !state.advancedExperimentalUnlocked) {
        const proceed = confirm(
          'Unlock experimental ranking guardrails?\n\n' +
          'When ON:\n' +
          '  • reality_breaker_multiplier value is SQUARED (no [0.5, 1.5] clamp).\n' +
          '  • Any non-1.0 composite multiplier shifts direction (no 5% deadband).\n' +
          '  • Bullish rows can flip to Bearish (and vice versa).\n\n' +
          'Use ONLY for research / paper-trade experiments.  Cancel to keep guardrails ON.'
        );
        if (!proceed) {
          ev.target.checked = false;
          return;
        }
      }
      state.advancedExperimentalUnlocked = checked;
      saveUiPrefs();
      bumpFilterSortCache();
      renderResults();
      if (state.selectedSymbol) loadDetail(state.selectedSymbol);
    });
  }
}

function wireResetControls() {
  const softResetBtn = byId('softResetBtn');
  if (softResetBtn && !softResetBtn.__mrdWired) {
    softResetBtn.__mrdWired = true;
    softResetBtn.addEventListener('click', () => showResetModal('soft'));
  }
  const hardResetBtn = byId('hardResetBtn');
  if (hardResetBtn && !hardResetBtn.__mrdWired) {
    hardResetBtn.__mrdWired = true;
    hardResetBtn.addEventListener('click', () => showResetModal('hard'));
  }
}



document.addEventListener('DOMContentLoaded', async () => {
  loadUiPrefs();
  // URL-shareable state — preset + symbol from ?preset=…&symbol=… override
  // cookies so a shared link is deterministic for the recipient.
  const _urlShare = readShareableStateFromUrl();
  if (_urlShare.market && (_urlShare.market === 'stocks' || _urlShare.market === 'crypto')) {
    state.currentMarket = _urlShare.market;
  }
  if (_urlShare.preset) {
    state.filters.preset = _urlShare.preset;
    const mapped = PRESET_FILTERS[_urlShare.preset];
    if (mapped) state.filters = { ...state.filters, ...mapped, preset: _urlShare.preset };
  }
  applyPrefsToControls();
  wireEvents();
  wireApiKeyPanel();
  loadApiKeyStatuses();
  setView(state.currentView || 'main');
  // Phase 26.49 — single delegated click handler for the Future
  // Forecast card.  Survives all internal re-renders (every 2s live
  // tick rebuilds the card's HTML).  Handles both the Deep Refresh
  // button and the click-to-pin metric info popovers.
  document.body.addEventListener('click', _ffDelegatedClick);
  document.body.addEventListener('click', _expCompositeClick);
  // Phase 26.49 — Escape closes any open reset modal.
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const modal = byId('resetConfirmModal');
      if (modal && !modal.hidden) hideResetModal();
    }
  });
  // Phase 26.50 bugfix — defensive wiring so the modal is *always*
  // dismissible.  Previously the cancel button's onclick was only set
  // inside showResetModal(); if the modal somehow rendered without
  // showResetModal() ever being called (e.g. a CSS regression that
  // overrode the `hidden` attribute), the user could not close it.
  // Also: clicking outside the inner card now dismisses the modal,
  // matching standard modal UX.
  const _resetModalEl = byId('resetConfirmModal');
  if (_resetModalEl) {
    // Ensure modal starts hidden no matter what other code does.
    _resetModalEl.hidden = true;
    _resetModalEl.addEventListener('click', (ev) => {
      // Clicks on the backdrop (the modal element itself, not its
      // inner card) should close the modal.
      if (ev.target === _resetModalEl) hideResetModal();
    });
    const _cancelBtnStartup = byId('resetModalCancel');
    if (_cancelBtnStartup) _cancelBtnStartup.addEventListener('click', hideResetModal);
  }

  // -------------------------------------------------------------------
  // Phase 26.50 bugfix — wire ALL toggles that have always-visible UI
  // controls UNCONDITIONALLY (was: gated inside `live_tick_enabled`
  // block, which left them dead in any non-leveraged variant AND any
  // build where the variant fetch fell through).  Run this BEFORE the
  // variant fetch so click handlers exist as soon as the DOM is ready.
  //
  // Covers:
  //   * Future Mode toggle + horizon
  //   * Future-mode filter buttons (All / Bulls / Bears)
  //   * Intensity buttons (All / Moderate / Strong / Max)
  //   * Lab Mode + "Blend Lab into ranking"
  //   * Strategy Tier + "Blend Strategy into ranking"
  //   * Soft / Hard reset buttons
  //
  // The leveraged-mode-only blocks (trading-style dropdown reveal,
  // advanced engine reveal, fast poll cadence) stay inside the
  // `live_tick_enabled` gate below.
  // -------------------------------------------------------------------
  wireFutureModeControls();
  wireLabAndStrategyControls();
  wireResetControls();
  wireUniversesPanel();

  // Phase 26.39: fetch variant config BEFORE the first refresh cycle so
  // the leveraged build kicks off at the fast cadence (2 s) from the
  // very first poll.  Best-effort: if the endpoint fails the dashboard
  // still works in default (full-universe) mode.
  try {
    const v = await fetchJson(`${state.apiBase}/api/system/variant`);
    if (v && !v.__not_modified__) {
      state.variant = {
        universe_mode: v.universe_mode || 'full',
        live_tick_enabled: !!v.live_tick_enabled,
        live_tick_interval_ms: Number(v.live_tick_interval_ms || 0),
        live_tick_top_n: Number(v.live_tick_top_n || 0),
        disable_crypto: !!v.disable_crypto,
        build: v.build || 'market-refinement-dashboard',
      };
      if (state.variant.live_tick_enabled) {
        // Eagerly shrink the cadence so the very first refreshCycle
        // doesn't wait on the default 3-5 s warming/idle value.
        state.resultsPollMs = state.variant.live_tick_interval_ms || 2000;
        console.info('[variant] leveraged live-tick ENABLED:',
          `interval=${state.resultsPollMs}ms, top_n=${state.variant.live_tick_top_n}`);
        // Phase 26.40: surface the trading-style dropdown.  It's
        // hidden in the main app build because the dropdown only
        // makes sense once the priority lane is guaranteeing full
        // factor-breakdown depth for the top-10 — without that, the
        // re-blend can't see the extended factor families it needs
        // for short/long differentiation.
        const styleBlock = byId('tradingStyleBlock');
        if (styleBlock) styleBlock.removeAttribute('hidden');
        const styleSelect = byId('tradingStyleSelect');
        if (styleSelect) {
          styleSelect.value = state.tradingStyle || 'default';
          styleSelect.addEventListener('change', (ev) => {
            state.tradingStyle = ev.target.value || 'default';
            saveUiPrefs();
            bumpFilterSortCache();
            renderResults();
          });
        }
        // Phase 26.42: advanced engine + advanced ranking toggles.
        const advBlock = byId('advancedEngineBlock');
        if (advBlock) advBlock.removeAttribute('hidden');
        const advPredCb = byId('useAdvancedPredictionEngine');
        if (advPredCb) {
          advPredCb.checked = !!state.useAdvancedPrediction;
          advPredCb.addEventListener('change', (ev) => {
            state.useAdvancedPrediction = !!ev.target.checked;
            saveUiPrefs();
          });
        }
        const advRankCb = byId('useAdvancedRanking');
        if (advRankCb) {
          advRankCb.checked = !!state.useAdvancedRanking;
          advRankCb.addEventListener('change', (ev) => {
            state.useAdvancedRanking = !!ev.target.checked;
            saveUiPrefs();
            bumpFilterSortCache();
            renderResults();
          });
        }
        // Phase 26.47 — Future Mode toggle + horizon selector.
        // (Wired unconditionally in `wireFutureModeControls()` above.)
        // Phase 26.49 — Future Mode filter (All / Bulls / Bears).
        // (Wired unconditionally in `wireFutureModeControls()` above.)
        // Phase 26.50 — intensity-band buttons (All / Moderate / Strong / Max).
        // (Wired unconditionally in `wireFutureModeControls()` above.)
        // Phase 26.49 — Lab Mode toggle + Blend.
        // (Wired unconditionally in `wireLabAndStrategyControls()` above.)
        // Phase 26.50 — Strategy Tier toggle + Blend.
        // (Wired unconditionally in `wireLabAndStrategyControls()` above.)
        // Phase 26.49 — Soft / Hard reset buttons.
        // (Wired unconditionally in `wireResetControls()` above.)
      }
    }
  } catch (err) {
    console.warn('[variant] /api/system/variant fetch failed, defaulting to full-universe mode:', err);
  }
  refreshCycle();
  scheduleActiveScanPool();
  setInterval(ageTick, 1000);
  initPublicUrlBanner();
  // Deep-link symbol: after the first refresh has kicked off, if the URL
  // has ?symbol=XYZ, open the detail panel for it.  Fire-and-forget —
  // loadDetail() is idempotent and gracefully no-ops for unknown symbols.
  if (_urlShare.symbol) {
    setTimeout(() => { try { loadDetail(_urlShare.symbol); } catch (_) { /* no-op */ } }, 400);
  }
});

// =========================================================================
// Phase 26.5: Public URL banner under the "Market Refinement" sidebar title.
// Surfaces the cloudflared / LAN URL captured by start.bat / start.sh so the
// user doesn't have to keep the console window open to share the dashboard.
// =========================================================================
function initPublicUrlBanner() {
  const banner = document.getElementById('publicUrlBanner');
  if (!banner) return;
  const link = document.getElementById('publicUrlLink');
  const badge = document.getElementById('publicUrlBadge');
  const copyBtn = document.getElementById('publicUrlCopy');
  const altsWrap = document.getElementById('publicUrlAltsWrap');
  const altsList = document.getElementById('publicUrlAlts');
  const subtext = document.getElementById('publicUrlSubtext');
  let lastUrl = null;

  const setBadge = (kind) => {
    badge.classList.remove('is-lan', 'is-local');
    let label = 'PUBLIC';
    if (kind === 'lan') { label = 'LAN'; badge.classList.add('is-lan'); }
    else if (kind === 'local') { label = 'LOCAL'; badge.classList.add('is-local'); }
    badge.textContent = label;
  };

  const setSubtext = (kind) => {
    if (kind === 'public') {
      subtext.textContent = 'Share this address from any network. Generated fresh each launch.';
    } else if (kind === 'lan') {
      subtext.textContent = 'Reachable from devices on the same WiFi. Public tunnel not active.';
    } else {
      subtext.textContent = 'Local-only. Run start.bat / start.sh to enable network sharing.';
    }
  };

  const setActive = (entry, alts) => {
    const fullUrl = entry.url.replace(/\/$/, '') + '/ui';
    link.textContent = entry.url.replace(/^https?:\/\//, '').replace(/\/+$/, '');
    link.href = fullUrl;
    link.title = `${entry.label} \u00b7 ${fullUrl}`;
    setBadge(entry.kind);
    setSubtext(entry.kind);
    lastUrl = fullUrl;

    altsList.innerHTML = '';
    const others = (alts || []).filter((a) => a.url !== entry.url);
    if (others.length === 0) {
      altsWrap.hidden = true;
    } else {
      altsWrap.hidden = false;
      others.forEach((a) => {
        const li = document.createElement('li');
        const bd = document.createElement('span');
        bd.className = 'public-url-banner__badge';
        if (a.kind === 'lan') bd.classList.add('is-lan');
        else if (a.kind === 'local') bd.classList.add('is-local');
        bd.style.fontSize = '.6rem';
        bd.textContent = (a.kind || 'other').toUpperCase();
        const link2 = document.createElement('a');
        const target = a.url.replace(/\/$/, '') + '/ui';
        link2.href = target;
        link2.target = '_blank';
        link2.rel = 'noopener';
        link2.textContent = a.url.replace(/^https?:\/\//, '').replace(/\/+$/, '');
        li.appendChild(bd);
        li.appendChild(link2);
        altsList.appendChild(li);
      });
    }
    banner.hidden = false;
  };

  const refresh = async () => {
    try {
      const res = await fetch(`${state.apiBase}/api/public-url`, { credentials: 'same-origin' });
      if (!res.ok) return;
      const data = await res.json();
      const items = (data && data.urls) || [];
      if (!items.length) { banner.hidden = true; return; }
      // Prefer public; then LAN; then local. The backend already sorts but
      // we re-sort defensively.
      const ordered = items.slice().sort((a, b) => {
        const rank = (k) => k === 'public' ? 0 : k === 'lan' ? 1 : k === 'local' ? 2 : 3;
        return rank(a.kind) - rank(b.kind);
      });
      setActive(ordered[0], ordered);
    } catch (err) {
      console.warn('public-url: refresh failed', err);
    }
  };

  copyBtn.addEventListener('click', async () => {
    if (!lastUrl) return;
    try {
      await navigator.clipboard.writeText(lastUrl);
      copyBtn.textContent = 'Copied!';
      copyBtn.classList.add('is-copied');
      setTimeout(() => {
        copyBtn.textContent = 'Copy';
        copyBtn.classList.remove('is-copied');
      }, 1500);
    } catch (err) {
      console.warn('clipboard write failed', err);
      // Manual selection fallback.
      const range = document.createRange();
      range.selectNode(link);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
  });

  // Regenerate cloudflared tunnel button. Kicks off the backend
  // POST /api/public-url/regenerate worker, polls /regenerate-status until
  // the new URL is captured, then refreshes the banner.
  const regenBtn = document.getElementById('publicUrlRegen');
  if (regenBtn) {
    let regenPollTimer = null;
    const stopRegenPoll = () => {
      if (regenPollTimer) { clearInterval(regenPollTimer); regenPollTimer = null; }
    };
    const pollRegen = async () => {
      try {
        const res = await fetch(`${state.apiBase}/api/public-url/regenerate-status`, { credentials: 'same-origin' });
        if (!res.ok) return;
        const s = await res.json();
        subtext.textContent = (s.message || 'Regenerating...');
        if (s.status === 'success') {
          stopRegenPoll();
          regenBtn.disabled = false;
          regenBtn.textContent = 'Regenerate';
          regenBtn.classList.remove('is-copied');
          // Refresh the banner contents from the (now-updated) URL file.
          await refresh();
        } else if (s.status === 'error') {
          stopRegenPoll();
          regenBtn.disabled = false;
          regenBtn.textContent = 'Regenerate';
          regenBtn.classList.remove('is-copied');
          subtext.textContent = `Error: ${s.message || 'regenerate failed'}`;
        }
      } catch (err) {
        console.warn('regenerate poll failed', err);
      }
    };
    regenBtn.addEventListener('click', async () => {
      if (regenBtn.disabled) return;
      regenBtn.disabled = true;
      regenBtn.textContent = 'Working\u2026';
      regenBtn.classList.add('is-copied');
      subtext.textContent = 'Spawning fresh Cloudflare Quick Tunnel...';
      try {
        const res = await fetch(`${state.apiBase}/api/public-url/regenerate`, {
          method: 'POST',
          credentials: 'same-origin',
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        await res.json();
        stopRegenPoll();
        regenPollTimer = setInterval(pollRegen, 1500);
        // Safety timeout - drop the polling after 90 s so a stalled backend
        // doesn't pin the button in the "Working..." state forever.
        setTimeout(() => {
          stopRegenPoll();
          if (regenBtn.disabled) {
            regenBtn.disabled = false;
            regenBtn.textContent = 'Regenerate';
            regenBtn.classList.remove('is-copied');
          }
        }, 90000);
      } catch (err) {
        console.warn('regenerate POST failed', err);
        regenBtn.disabled = false;
        regenBtn.textContent = 'Regenerate';
        regenBtn.classList.remove('is-copied');
        subtext.textContent = `Error: ${err.message || err}`;
      }
    });
  }

  // Initial + periodic refresh (every 30s) so a late-arriving cloudflared
  // URL is picked up automatically without requiring a page reload.
  refresh();
  setInterval(refresh, 30000);
}
