/*  state.js — Quantum Market Scanner
 *
 *  Owns the top-level `state` object plus every helper that operates
 *  purely on state, cookies, presets, filter serialization and URL
 *  synchronization.  No DOM rendering, no polling.  Loaded via
 *  <script src="/frontend/state.js" defer> BEFORE app.js so its
 *  declarations are in scope for the rest of the bundle.
 *
 *  Extracted from app.js on 2026-07-02 (session 10) as the first
 *  step of the vanilla-JS modular split.
 */
/* eslint-env browser */
/* global refreshCycle, renderResults, renderPagination, bumpFilterSortCache */

const state = {
  apiBase: '',
  batchIndex: 0,
  totalBatches: 1,
  allRowsMap: new Map(),
  masterRowsMap: new Map(),
  trackedRowsMap: new Map(),
  top25PassCounts: new Map(),
  marketTrackedRows: { stocks: new Map(), crypto: new Map() },
  marketTop25PassCounts: { stocks: new Map(), crypto: new Map() },
  marketBatchIndex: { stocks: 0, crypto: 0 },
  marketTotalBatches: { stocks: 1, crypto: 1 },
  selectedSymbol: null,
  isRefreshing: false,
  searchTimer: null,
  status: null,
  pageIndex: 0,
  pageSize: 25,
  resultsFailureCount: 0,
  resultsPollMs: 250,
  refreshHandle: null,
  currentView: 'main',
  currentMarket: 'stocks',
  marketRowsMap: { stocks: new Map(), crypto: new Map() },
  filters: {
    preset: '', direction: '', tier: '', min_score: 0, max_exit_risk: 100, exit_flag: '',
    // Extended factor-family filters
    min_institutional_confluence: 0,
    min_options_positioning: 0,
    institutional_bias_in: '',
    options_bias_in: '',
    iob_state_in: '',
    dark_pool_attraction_state_in: '',
    options_gamma_level_in: '',
    sort_by: '',
    // Phase 4b: reaction + volume sentiment filters
    reaction_classification_in: '',
    dominant_zone_tier_in: '',
    volume_sentiment_bias_in: '',
    effort_vs_result_in: '',
    min_volume_sentiment_conviction: 0,
    // Scanner-context filters: short pressure / predicted volume intensity /
    // options expiration proximity.
    min_predicted_volume_intensity: 0,
    predicted_volume_intensity_bucket_in: '',
    min_short_selling_pressure: 0,
    short_selling_pressure_label_in: '',
    max_days_to_options_expiration: '',
    expiration_risk_only: false,
  },
  // Predicted-volume-first ordering: dedicated pre-filter sort mode that
  // surfaces likely upcoming high-volume names before refinement filters.
  pviPriority: false,
  // Per-row Future Forecast Activator state — lives in state (not DOM) so
  // it persists across renderResults() refresh passes.
  inlineForecast: null,          // { symbol, loading, error, payload, open }
  forecastInflight: new Set(),   // duplicate-request guard
  // Experimental composite (vibe) breakdown dropdown state — lifted to a
  // stable parent keyed by symbol+timeframe so details-column refresh
  // passes can't despawn the open breakdown.
  expOpenTf: null,               // { symbol, key }
  // Details panel stability audit counters (operational clarity: was a
  // destabilization caused by data shape, refresh orchestration, or
  // frontend state loss?).
  detailAudit: { ticks: 0, failures: 0, rebuilds: 0, statePreserved: 0, stateResets: 0, lastGoodUtc: null },
  activeScanPool: new Map(),
  activeScanUniverseSymbols: new Set(),
  marketActivePools: { stocks: new Map(), crypto: new Map() },
  marketActiveSymbols: { stocks: new Set(), crypto: new Set() },
  activeScanLimit: 250,
  activeScanIntervalMs: 60 * 1000,
  activeScanHandle: null,
  activeScanPasses: 0,
  activeScanLastRunUtc: null,
  activeScanLastRefreshCount: 0,
  lastScanProgress: null,
  ageRenderCounter: 0,
  maxActiveRefreshPerPass: 120,
  cryptoBatchLoadSize: 100,
  // Phase 26.39: live-tick state.  `variant` is populated once via the
  // /api/system/variant call during init.  When `live_tick_enabled` is
  // true (leveraged-only build) we (1) shrink the snapshot poll
  // cadence to `live_tick_interval_ms` and (2) auto-refresh the detail
  // panel at the same cadence while it's open.
  variant: { universe_mode: 'full', live_tick_enabled: false, live_tick_interval_ms: 0, live_tick_top_n: 0 },
  detailLiveTimer: null,        // setInterval handle for the open detail panel
  detailLiveSymbol: null,       // which symbol the timer is currently refreshing
  detailLiveTickCount: 0,       // Phase 26.43: counter so the user can SEE the timer is firing
  // Phase 26.45: circuit breaker for the detail-panel live-tick.
  // After N consecutive failures (typically caused by a backend
  // lockup / overload) we pause the tick for `detailLiveBackoffMs`
  // and surface a clear "connection paused" pill instead of letting
  // "TypeError: Failed to fetch" cascade through the UI forever.
  detailLiveFailureStreak: 0,
  detailLiveBackoffUntil: 0,    // epoch ms; while now < this, the tick skips its fetch
  // Phase 26.43: cached prediction + backtest result HTML so they
  // survive the live-tick re-render of the detail panel.
  cachedPredictionCard: null,
  cachedBacktestCard: null,
  // Phase 26.40: trading-style re-weighting.  Leveraged variant only.
  // Default = use the raw composite final_score (no reweighting).
  // Other modes blend the four algorithm ratings (Momentum, Quality,
  // Trend, Stability) using style-specific weights.  Persisted via
  // cookie so user choice survives reloads.
  tradingStyle: 'default',
  // Phase 26.42: quant-grade engine toggles.  Both default off so the
  // experience is identical to the main app until the user opts in.
  useAdvancedPrediction: false,   // routes /api/predict/* → /api/predict/advanced/*
  useAdvancedRanking: false,      // re-ranks the leaderboard by Kelly-like score
  // Phase 26.47 — Future Mode.  When enabled, the leaderboard is
  // re-sorted by `forward_metrics[<horizon>].effective_kelly_rank_abs`
  // (server-side hybrid: GARCH(1,1) + Bayesian blend + Cornish-Fisher
  // + jump-diffusion + Hurst regime), and the Future-Mode columns in
  // the table render the per-row predictive snapshot.  Default
  // horizon = 1-hour hold per user spec; persisted to cookie.
  futureMode: false,
  futureHorizon: '1h_hold',
  // Phase 26.49 — Future Mode filter (All / Bulls Only / Bears Only).
  futureFilter: 'all',
  // Phase 26.50 — intensity band preset (All / Moderate / Strong / Max)
  futureIntensity: 'all',
  // Phase 26.49 — Lab Mode (experimental signals overlay).
  useLabMode: false,
  blendLabIntoRanking: false,
  // Phase 26.50 — Strategy Tier (10 predictive algos).
  useStrategyMode: false,
  blendStrategyIntoRanking: false,
  // Phase 26.60 — Predictive Expansion Pack (10 standard metrics +
  // 4 reality_breaker overlays + 5 composite multipliers).  The
  // three "blends" are independent multiplicative factors on the
  // effective Kelly rank — same cascading semantics as Lab/Strategy.
  useStrategyV2Mode: false,
  blendStrategyV2IntoRanking: false,
  useRegimeRiskMode: false,
  blendRegimeRiskIntoRanking: false,
  useMlOverlayMode: false,
  blendMlOverlayIntoRanking: false,
  // Liquidity Kelly factor — multiplicative scaling, always-on once
  // ANY of the Phase 26.60 blends is active (the Kelly factor lives
  // alongside the rest of the pack).  Persisted independently so the
  // user can disable it explicitly.
  blendLiqKellyFactor: false,
  // ----- Advanced Experimental Mode (gated 4 reality_breaker overlays) -----
  // DEFAULT OFF.  When OFF: reality_breaker fields are NEVER multiplied
  // into the rank, never displayed in the popover, and the backend
  // doesn't compute them.  When ON: each of the 4 overlays has its
  // own toggle, AND the user can opt-in to "Blend reality_breaker
  // into ranking" which adds reality_breaker_multiplier to the chain.
  advancedExperimentalMode: false,
  // Phase 26.61c — "Unlocked" experimental mode.  When ON (and the
  // master Advanced Experimental Mode is also ON), the reality_breaker
  // multiplier:
  //   * Bypasses the [0.5, 1.5] clamp band (raw value used).
  //   * Bypasses the 5% deadband on direction adjustment (any
  //     non-1.0 multiplier flips/dampens direction).
  //   * Bypasses the pipeline_tuning floor/ceiling clamps in the
  //     client-side preview.
  // SAFETY: A confirmation dialog fires when the user first enables
  // this; the cookie is wiped when Advanced Experimental Mode is OFF.
  advancedExperimentalUnlocked: false,
  showLocalCausalCone: false,
  showQuantumPathInterference: false,
  showLocalLyapunov: false,
  showTemporalRenormalization: false,
  blendRealityBreakerIntoRanking: false,
  // Phase 26.65 — "Bull × Bull" priority sort.  When ON, rows whose
  // classical Direction AND forward F-DIR are BOTH Bullish float to the
  // top of the leaderboard (a priority sort, not a hard filter — other
  // rows still appear below).  Persisted to cookie.
  bullBullPriority: false,
  // Phase 26.65 — Reality-Breaker overall-rating list filter.  Only
  // active when Advanced Experimental Mode is on.  Buckets rows by the
  // reality_breaker_multiplier: 'endorse' (>1.03), 'caution' (<0.97),
  // 'neutral' (≈1.0).  'all' = no filter.
  rbFilter: 'all',
  // Phase 26.68 — 7-timeframe consensus filter + sort.
  //   consensusFilter: 'all' | 'up6' | 'up7' | 'down6' | 'down7'
  //   consensusSort:   'off' | 'desc' (most net-Up first) | 'asc'
  consensusFilter: 'all',
  consensusSort: 'off',
  // Phase 26.49 — pinned metric info popover state.  Stored as
  // `{ metric_id, symbol }` so the popover re-attaches automatically
  // after every 2-second live-tick re-render.  Null when no popover
  // is open.
  pinnedMetric: null,
};

async function fetchJson(url) {
  const res = await fetch(url, { credentials: 'same-origin' });
  // 304 Not Modified is a valid revalidation signal — surface a sentinel
  // so callers can choose to keep their previous state instead of
  // treating it as a hard failure. (Phase 26.18 hotfix: previously the
  // `if (!res.ok)` branch below threw for 304s coming back from the
  // snapshot endpoint, which surfaced as "Results unavailable, showing
  // last good snapshot" in the UI.)
  if (res.status === 304) return { __not_modified__: true };
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`${res.status} ${res.statusText}${text ? ` :: ${text}` : ''} for ${url}`);
  }
  return await res.json();
}

function byId(id) { return document.getElementById(id); }

function setCookie(name, value, maxAgeSeconds = 2592000) {
  document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=${maxAgeSeconds}; samesite=lax`;
}

function getCookie(name) {
  const prefix = `${name}=`;
  return document.cookie.split('; ').find((row) => row.startsWith(prefix))?.slice(prefix.length) || '';
}

function saveUiPrefs() {
  setCookie('mrd_view', state.currentView);
  setCookie('mrd_market', state.currentMarket);
  setCookie('mrd_filters', JSON.stringify(state.filters));
  setCookie('mrd_trading_style', state.tradingStyle || 'default');
  setCookie('mrd_use_adv_prediction', state.useAdvancedPrediction ? '1' : '0');
  setCookie('mrd_use_adv_ranking', state.useAdvancedRanking ? '1' : '0');
  setCookie('mrd_future_mode', state.futureMode ? '1' : '0');
  setCookie('mrd_future_horizon', state.futureHorizon || '1h_hold');
  setCookie('mrd_future_filter', state.futureFilter || 'all');
  setCookie('mrd_future_intensity', state.futureIntensity || 'all');
  setCookie('mrd_lab_mode', state.useLabMode ? '1' : '0');
  setCookie('mrd_lab_blend', state.blendLabIntoRanking ? '1' : '0');
  setCookie('mrd_strategy_mode', state.useStrategyMode ? '1' : '0');
  setCookie('mrd_strategy_blend', state.blendStrategyIntoRanking ? '1' : '0');
  // Phase 26.60 — Predictive Expansion toggles
  setCookie('mrd_strategy_v2_mode', state.useStrategyV2Mode ? '1' : '0');
  setCookie('mrd_strategy_v2_blend', state.blendStrategyV2IntoRanking ? '1' : '0');
  setCookie('mrd_regime_risk_mode', state.useRegimeRiskMode ? '1' : '0');
  setCookie('mrd_regime_risk_blend', state.blendRegimeRiskIntoRanking ? '1' : '0');
  setCookie('mrd_ml_overlay_mode', state.useMlOverlayMode ? '1' : '0');
  setCookie('mrd_ml_overlay_blend', state.blendMlOverlayIntoRanking ? '1' : '0');
  setCookie('mrd_liq_kelly_blend', state.blendLiqKellyFactor ? '1' : '0');
  setCookie('mrd_adv_exp_mode', state.advancedExperimentalMode ? '1' : '0');
  setCookie('mrd_adv_exp_unlocked', state.advancedExperimentalUnlocked ? '1' : '0');
  setCookie('mrd_show_lcc', state.showLocalCausalCone ? '1' : '0');
  setCookie('mrd_show_qpii', state.showQuantumPathInterference ? '1' : '0');
  setCookie('mrd_show_llve', state.showLocalLyapunov ? '1' : '0');
  setCookie('mrd_show_trs', state.showTemporalRenormalization ? '1' : '0');
  setCookie('mrd_reality_breaker_blend', state.blendRealityBreakerIntoRanking ? '1' : '0');
  setCookie('mrd_bull_bull_priority', state.bullBullPriority ? '1' : '0');
  setCookie('mrd_pvi_priority', state.pviPriority ? '1' : '0');
  setCookie('mrd_rb_filter', state.rbFilter || 'all');
  setCookie('mrd_consensus_filter', state.consensusFilter || 'all');
  setCookie('mrd_consensus_sort', state.consensusSort || 'off');
}

function loadUiPrefs() {
  const savedView = getCookie('mrd_view');
  const savedFilters = getCookie('mrd_filters');
  const savedMarket = getCookie('mrd_market');
  const savedStyle = getCookie('mrd_trading_style');
  const savedAdvPred = getCookie('mrd_use_adv_prediction');
  const savedAdvRank = getCookie('mrd_use_adv_ranking');
  const savedFutMode = getCookie('mrd_future_mode');
  const savedFutHor = getCookie('mrd_future_horizon');
  if (savedView === 'main' || savedView === 'tracked') state.currentView = savedView;
  if (savedMarket === 'stocks' || savedMarket === 'crypto') state.currentMarket = savedMarket;
  if (['default', 'short', 'swing', 'long'].includes(savedStyle)) state.tradingStyle = savedStyle;
  if (savedAdvPred === '1') state.useAdvancedPrediction = true;
  if (savedAdvRank === '1') state.useAdvancedRanking = true;
  if (savedFutMode === '1') state.futureMode = true;
  if (['1h_hold', '5h_hold', 'overnight_hold', 'weekend_hold', 'short', 'swing', 'long'].includes(savedFutHor)) state.futureHorizon = savedFutHor;
  const savedFilter = getCookie('mrd_future_filter');
  if (['all', 'bulls', 'bears'].includes(savedFilter)) state.futureFilter = savedFilter;
  const savedIntensity = getCookie('mrd_future_intensity');
  if (['all', 'moderate', 'strong', 'max'].includes(savedIntensity)) state.futureIntensity = savedIntensity;
  if (getCookie('mrd_lab_mode') === '1') state.useLabMode = true;
  if (getCookie('mrd_lab_blend') === '1') state.blendLabIntoRanking = true;
  if (getCookie('mrd_strategy_mode') === '1') state.useStrategyMode = true;
  if (getCookie('mrd_strategy_blend') === '1') state.blendStrategyIntoRanking = true;
  // Phase 26.60 — Predictive Expansion toggles (all default OFF)
  if (getCookie('mrd_strategy_v2_mode') === '1') state.useStrategyV2Mode = true;
  if (getCookie('mrd_strategy_v2_blend') === '1') state.blendStrategyV2IntoRanking = true;
  if (getCookie('mrd_regime_risk_mode') === '1') state.useRegimeRiskMode = true;
  if (getCookie('mrd_regime_risk_blend') === '1') state.blendRegimeRiskIntoRanking = true;
  if (getCookie('mrd_ml_overlay_mode') === '1') state.useMlOverlayMode = true;
  if (getCookie('mrd_ml_overlay_blend') === '1') state.blendMlOverlayIntoRanking = true;
  if (getCookie('mrd_liq_kelly_blend') === '1') state.blendLiqKellyFactor = true;
  // Advanced Experimental Mode + reality_breaker overlays — DEFAULT OFF.
  // We deliberately read these into the SAME defaults (false) so a
  // brand-new install ships with reality_breaker disabled even when
  // the cookie value is absent (a missing cookie is treated as OFF).
  if (getCookie('mrd_adv_exp_mode') === '1') state.advancedExperimentalMode = true;
  if (getCookie('mrd_adv_exp_unlocked') === '1') state.advancedExperimentalUnlocked = true;
  if (getCookie('mrd_show_lcc') === '1') state.showLocalCausalCone = true;
  if (getCookie('mrd_show_qpii') === '1') state.showQuantumPathInterference = true;
  if (getCookie('mrd_show_llve') === '1') state.showLocalLyapunov = true;
  if (getCookie('mrd_show_trs') === '1') state.showTemporalRenormalization = true;
  if (getCookie('mrd_reality_breaker_blend') === '1') state.blendRealityBreakerIntoRanking = true;
  // Belt-and-suspenders enforcement: if Advanced Experimental Mode is
  // OFF, every reality_breaker child toggle MUST also be OFF on
  // restore — defends against a hand-edited cookie jar.
  if (!state.advancedExperimentalMode) {
    state.showLocalCausalCone = false;
    state.showQuantumPathInterference = false;
    state.showLocalLyapunov = false;
    state.showTemporalRenormalization = false;
    state.blendRealityBreakerIntoRanking = false;
    // Belt + suspenders: Unlocked mode requires the master to be ON.
    state.advancedExperimentalUnlocked = false;
    state.rbFilter = 'all';   // RB list filter only meaningful when master ON
  }
  if (getCookie('mrd_bull_bull_priority') === '1') state.bullBullPriority = true;
  if (getCookie('mrd_pvi_priority') === '1') state.pviPriority = true;
  const savedRbFilter = getCookie('mrd_rb_filter');
  if (['all', 'endorse', 'neutral', 'caution'].includes(savedRbFilter)) state.rbFilter = savedRbFilter;
  if (!state.advancedExperimentalMode) state.rbFilter = 'all';
  const savedCsFilter = getCookie('mrd_consensus_filter');
  if (['all', 'up6', 'up7', 'down6', 'down7'].includes(savedCsFilter)) state.consensusFilter = savedCsFilter;
  const savedCsSort = getCookie('mrd_consensus_sort');
  if (['off', 'desc', 'asc'].includes(savedCsSort)) state.consensusSort = savedCsSort;
  if (savedFilters) {
    try {
      const parsed = JSON.parse(decodeURIComponent(savedFilters));
      state.filters = { ...state.filters, ...parsed };
    } catch (_) {
      // saved filter cookie is malformed/legacy — ignore and keep defaults
    }
  }
}

function applyPrefsToControls() {
  if (byId('presetFilter')) byId('presetFilter').value = state.filters.preset || '';
  if (byId('directionFilter')) byId('directionFilter').value = state.filters.direction || '';
  if (byId('tierFilter')) byId('tierFilter').value = state.filters.tier || '';
  if (byId('minScoreFilter')) byId('minScoreFilter').value = String(state.filters.min_score || 0);
  if (byId('minScoreValue')) byId('minScoreValue').textContent = String(state.filters.min_score || 0);
  if (byId('exitFlagFilter')) byId('exitFlagFilter').value = state.filters.exit_flag || '';
  if (byId('maxExitRiskFilter')) byId('maxExitRiskFilter').value = String(state.filters.max_exit_risk ?? 100);
  if (byId('maxExitRiskValue')) byId('maxExitRiskValue').textContent = String(state.filters.max_exit_risk ?? 100);
  if (byId('minInstitutionalConfluenceFilter')) byId('minInstitutionalConfluenceFilter').value = String(state.filters.min_institutional_confluence || 0);
  if (byId('minInstitutionalConfluenceValue')) byId('minInstitutionalConfluenceValue').textContent = String(state.filters.min_institutional_confluence || 0);
  if (byId('minOptionsPositioningFilter')) byId('minOptionsPositioningFilter').value = String(state.filters.min_options_positioning || 0);
  if (byId('minOptionsPositioningValue')) byId('minOptionsPositioningValue').textContent = String(state.filters.min_options_positioning || 0);
  if (byId('institutionalBiasFilter')) byId('institutionalBiasFilter').value = state.filters.institutional_bias_in || '';
  if (byId('optionsBiasFilter')) byId('optionsBiasFilter').value = state.filters.options_bias_in || '';
  if (byId('iobStateFilter')) byId('iobStateFilter').value = state.filters.iob_state_in || '';
  if (byId('darkPoolAttractionFilter')) byId('darkPoolAttractionFilter').value = state.filters.dark_pool_attraction_state_in || '';
  if (byId('optionsGammaFilter')) byId('optionsGammaFilter').value = state.filters.options_gamma_level_in || '';
  if (byId('sortByFilter')) byId('sortByFilter').value = state.filters.sort_by || '';
  if (byId('reactionClassificationFilter')) byId('reactionClassificationFilter').value = state.filters.reaction_classification_in || '';
  if (byId('dominantZoneTierFilter')) byId('dominantZoneTierFilter').value = state.filters.dominant_zone_tier_in || '';
  if (byId('volumeSentimentBiasFilter')) byId('volumeSentimentBiasFilter').value = state.filters.volume_sentiment_bias_in || '';
  if (byId('effortVsResultFilter')) byId('effortVsResultFilter').value = state.filters.effort_vs_result_in || '';
  if (byId('minVolumeSentimentConvictionFilter')) byId('minVolumeSentimentConvictionFilter').value = String(state.filters.min_volume_sentiment_conviction || 0);
  if (byId('minVolumeSentimentConvictionValue')) byId('minVolumeSentimentConvictionValue').textContent = String(state.filters.min_volume_sentiment_conviction || 0);
  // Scanner-context filters
  if (byId('pviPriorityToggle')) byId('pviPriorityToggle').checked = !!state.pviPriority;
  if (byId('minPviFilter')) byId('minPviFilter').value = String(state.filters.min_predicted_volume_intensity || 0);
  if (byId('minPviValue')) byId('minPviValue').textContent = String(state.filters.min_predicted_volume_intensity || 0);
  if (byId('pviBucketFilter')) byId('pviBucketFilter').value = state.filters.predicted_volume_intensity_bucket_in || '';
  if (byId('minShortPressureFilter')) byId('minShortPressureFilter').value = String(state.filters.min_short_selling_pressure || 0);
  if (byId('minShortPressureValue')) byId('minShortPressureValue').textContent = String(state.filters.min_short_selling_pressure || 0);
  if (byId('shortPressureLabelFilter')) byId('shortPressureLabelFilter').value = state.filters.short_selling_pressure_label_in || '';
  if (byId('maxDteFilter')) byId('maxDteFilter').value = state.filters.max_days_to_options_expiration === '' || state.filters.max_days_to_options_expiration == null ? '' : String(state.filters.max_days_to_options_expiration);
  if (byId('expirationRiskOnly')) byId('expirationRiskOnly').checked = !!state.filters.expiration_risk_only;
  updatePresetChips();
}

const PRESET_FILTERS = {
  'all': { direction: '', tier: '', min_score: 0, max_exit_risk: 100, exit_flag: '' },
  'bullish': { direction: 'Bullish', tier: '', min_score: 0, max_exit_risk: 100, exit_flag: '' },
  'leaders': { direction: 'Bullish', tier: '', min_score: 55, max_exit_risk: 100, exit_flag: '' },
  'value-safe': { direction: '', tier: '', min_score: 45, max_exit_risk: 100, exit_flag: '' },
  'degraded-view': { direction: '', tier: '', min_score: 0, max_exit_risk: 100, exit_flag: '' },
  'low-exit-risk': { direction: '', tier: '', min_score: 45, max_exit_risk: 45, exit_flag: 'hold' },
  'reversal-watch': { direction: '', tier: '', min_score: 0, max_exit_risk: 100, exit_flag: 'caution' },
  // ---- Scanner-context presets (short pressure / PVI / expiration) ----
  // Client-side mirror of the server-side SCANNER_PRESETS entries so the
  // sidebar controls visually reflect what the preset actually filtered on.
  'squeeze-watch': {
    direction: '', tier: '', min_score: 0, max_exit_risk: 100, exit_flag: '',
    min_short_selling_pressure: 60,
    short_selling_pressure_label_in: 'squeeze_risk_bullish,elevated_squeeze_watch',
    min_predicted_volume_intensity: 55,
    max_days_to_options_expiration: 14,
  },
  'volume-storm': {
    direction: '', tier: '', min_score: 0, max_exit_risk: 100, exit_flag: '',
    min_predicted_volume_intensity: 65,
    predicted_volume_intensity_bucket_in: 'high,extreme',
  },
  'bearish-pressure': {
    direction: '', tier: '', min_score: 0, max_exit_risk: 100, exit_flag: '',
    min_short_selling_pressure: 55,
    short_selling_pressure_label_in: 'bearish_pressure',
  },
  'expiration-pin': {
    direction: '', tier: '', min_score: 0, max_exit_risk: 100, exit_flag: '',
    expiration_risk_only: true,
    max_days_to_options_expiration: 7,
  },
};

function updatePresetChips() {
  document.querySelectorAll('.preset-chip').forEach((btn) => {
    btn.classList.toggle('is-active', btn.dataset.preset === (state.filters.preset || ''));
  });
}

// ---------------------------------------------------------------------------
// URL-shareable state
// ---------------------------------------------------------------------------
// A tiny helper that mirrors preset + selected symbol into the browser
// query string using history.replaceState (no page reload, no history
// spam).  Any user who pastes / bookmarks the URL comes back to the same
// preset + focused symbol.  Reads on boot via readShareableStateFromUrl().
function _syncShareableUrl() {
  try {
    const url = new URL(window.location.href);
    const params = url.searchParams;
    // Preset — write when set, delete when cleared.
    if (state.filters.preset) params.set('preset', state.filters.preset);
    else params.delete('preset');
    // Symbol — deep link to the current detail panel selection.
    if (state.selectedSymbol) params.set('symbol', state.selectedSymbol);
    else params.delete('symbol');
    // Market — only persist when non-default (crypto).
    if (state.currentMarket && state.currentMarket !== 'stocks') {
      params.set('market', state.currentMarket);
    } else {
      params.delete('market');
    }
    const next = url.pathname + (params.toString() ? '?' + params.toString() : '') + url.hash;
    if (next !== url.pathname + url.search + url.hash) {
      window.history.replaceState({}, '', next);
    }
  } catch (_) { /* URL API unavailable — no-op */ }
}

function readShareableStateFromUrl() {
  try {
    const params = new URLSearchParams(window.location.search);
    return {
      preset: (params.get('preset') || '').trim() || null,
      symbol: (params.get('symbol') || '').trim().toUpperCase() || null,
      market: (params.get('market') || '').trim() || null,
    };
  } catch (_) { return { preset: null, symbol: null, market: null }; }
}

function applyPreset(preset) {
  const key = preset || '';
  if (byId('presetFilter')) byId('presetFilter').value = key;
  syncFilterState();
  const mapped = PRESET_FILTERS[key] || null;
  if (mapped) {
    state.filters = { ...state.filters, ...mapped, preset: key };
  }
  state.pageIndex = 0;
  saveUiPrefs();
  applyPrefsToControls();
  updatePresetChips();
  _syncShareableUrl();
  refreshCycle();
}

function syncFilterState() {
  if (!byId('presetFilter')) return;
  state.filters.preset = byId('presetFilter').value;
  state.filters.direction = byId('directionFilter').value;
  state.filters.tier = byId('tierFilter').value;
  state.filters.min_score = Number(byId('minScoreFilter').value || 0);
  state.filters.exit_flag = byId('exitFlagFilter') ? byId('exitFlagFilter').value : '';
  state.filters.max_exit_risk = byId('maxExitRiskFilter') ? Number(byId('maxExitRiskFilter').value || 100) : 100;
  state.filters.min_institutional_confluence = byId('minInstitutionalConfluenceFilter') ? Number(byId('minInstitutionalConfluenceFilter').value || 0) : 0;
  state.filters.min_options_positioning = byId('minOptionsPositioningFilter') ? Number(byId('minOptionsPositioningFilter').value || 0) : 0;
  state.filters.institutional_bias_in = byId('institutionalBiasFilter') ? byId('institutionalBiasFilter').value : '';
  state.filters.options_bias_in = byId('optionsBiasFilter') ? byId('optionsBiasFilter').value : '';
  state.filters.iob_state_in = byId('iobStateFilter') ? byId('iobStateFilter').value : '';
  state.filters.dark_pool_attraction_state_in = byId('darkPoolAttractionFilter') ? byId('darkPoolAttractionFilter').value : '';
  state.filters.options_gamma_level_in = byId('optionsGammaFilter') ? byId('optionsGammaFilter').value : '';
  state.filters.sort_by = byId('sortByFilter') ? byId('sortByFilter').value : '';
  state.filters.reaction_classification_in = byId('reactionClassificationFilter') ? byId('reactionClassificationFilter').value : '';
  state.filters.dominant_zone_tier_in = byId('dominantZoneTierFilter') ? byId('dominantZoneTierFilter').value : '';
  state.filters.volume_sentiment_bias_in = byId('volumeSentimentBiasFilter') ? byId('volumeSentimentBiasFilter').value : '';
  state.filters.effort_vs_result_in = byId('effortVsResultFilter') ? byId('effortVsResultFilter').value : '';
  state.filters.min_volume_sentiment_conviction = byId('minVolumeSentimentConvictionFilter') ? Number(byId('minVolumeSentimentConvictionFilter').value || 0) : 0;
  // Scanner-context filters
  state.pviPriority = byId('pviPriorityToggle') ? !!byId('pviPriorityToggle').checked : state.pviPriority;
  state.filters.min_predicted_volume_intensity = byId('minPviFilter') ? Number(byId('minPviFilter').value || 0) : 0;
  state.filters.predicted_volume_intensity_bucket_in = byId('pviBucketFilter') ? byId('pviBucketFilter').value : '';
  state.filters.min_short_selling_pressure = byId('minShortPressureFilter') ? Number(byId('minShortPressureFilter').value || 0) : 0;
  state.filters.short_selling_pressure_label_in = byId('shortPressureLabelFilter') ? byId('shortPressureLabelFilter').value : '';
  state.filters.max_days_to_options_expiration = byId('maxDteFilter') && byId('maxDteFilter').value !== '' ? Number(byId('maxDteFilter').value) : '';
  state.filters.expiration_risk_only = byId('expirationRiskOnly') ? !!byId('expirationRiskOnly').checked : false;
  if (byId('minPviValue')) byId('minPviValue').textContent = String(state.filters.min_predicted_volume_intensity);
  if (byId('minShortPressureValue')) byId('minShortPressureValue').textContent = String(state.filters.min_short_selling_pressure);
  if (byId('minScoreValue')) byId('minScoreValue').textContent = String(state.filters.min_score);
  if (byId('maxExitRiskValue')) byId('maxExitRiskValue').textContent = String(state.filters.max_exit_risk);
  if (byId('minInstitutionalConfluenceValue')) byId('minInstitutionalConfluenceValue').textContent = String(state.filters.min_institutional_confluence);
  if (byId('minOptionsPositioningValue')) byId('minOptionsPositioningValue').textContent = String(state.filters.min_options_positioning);
  if (byId('minVolumeSentimentConvictionValue')) byId('minVolumeSentimentConvictionValue').textContent = String(state.filters.min_volume_sentiment_conviction);
  // Any filter change must invalidate the memoized filter+sort cache.
  if (typeof bumpFilterSortCache === 'function') bumpFilterSortCache();
  saveUiPrefs();
  _syncShareableUrl();
  // Phase 26.9: re-render immediately so the table reflects every filter
  // tweak the moment it happens, instead of waiting up to 5s for the next
  // snapshot poll. The "Apply Filters" button still triggers a full
  // refreshCycle() so a manual click also re-fetches the snapshot.
  state.pageIndex = 0;
  if (typeof renderResults === 'function') renderResults();
  if (typeof renderPagination === 'function') renderPagination();
}

function getFiltersQuery() {
  const params = new URLSearchParams();
  if (state.filters.preset) params.set('preset', state.filters.preset);
  if (state.filters.direction) params.set('direction', state.filters.direction);
  if (state.filters.tier) params.set('tier', state.filters.tier);
  if (Number(state.filters.min_score) > 0) params.set('min_score', String(state.filters.min_score));
  if (state.filters.exit_flag) params.set('exit_flag', state.filters.exit_flag);
  if (Number(state.filters.max_exit_risk) < 100) params.set('max_exit_risk', String(state.filters.max_exit_risk));
  // Extended factor filters
  if (Number(state.filters.min_institutional_confluence) > 0) params.set('min_institutional_confluence', String(state.filters.min_institutional_confluence));
  if (Number(state.filters.min_options_positioning) > 0) params.set('min_options_positioning', String(state.filters.min_options_positioning));
  if (state.filters.institutional_bias_in) params.set('institutional_bias_in', state.filters.institutional_bias_in);
  if (state.filters.options_bias_in) params.set('options_bias_in', state.filters.options_bias_in);
  if (state.filters.iob_state_in) params.set('iob_state_in', state.filters.iob_state_in);
  if (state.filters.dark_pool_attraction_state_in) params.set('dark_pool_attraction_state_in', state.filters.dark_pool_attraction_state_in);
  if (state.filters.options_gamma_level_in) params.set('options_gamma_level_in', state.filters.options_gamma_level_in);
  if (state.filters.sort_by) params.set('sort_by', state.filters.sort_by);
  // Phase 4b
  if (state.filters.reaction_classification_in) params.set('reaction_classification_in', state.filters.reaction_classification_in);
  if (state.filters.dominant_zone_tier_in) params.set('dominant_zone_tier_in', state.filters.dominant_zone_tier_in);
  if (state.filters.volume_sentiment_bias_in) params.set('volume_sentiment_bias_in', state.filters.volume_sentiment_bias_in);
  if (state.filters.effort_vs_result_in) params.set('effort_vs_result_in', state.filters.effort_vs_result_in);
  if (Number(state.filters.min_volume_sentiment_conviction) > 0) params.set('min_volume_sentiment_conviction', String(state.filters.min_volume_sentiment_conviction));
  // Scanner-context filters
  if (Number(state.filters.min_predicted_volume_intensity) > 0) params.set('min_predicted_volume_intensity', String(state.filters.min_predicted_volume_intensity));
  if (state.filters.predicted_volume_intensity_bucket_in) params.set('predicted_volume_intensity_bucket_in', state.filters.predicted_volume_intensity_bucket_in);
  if (Number(state.filters.min_short_selling_pressure) > 0) params.set('min_short_selling_pressure', String(state.filters.min_short_selling_pressure));
  if (state.filters.short_selling_pressure_label_in) params.set('short_selling_pressure_label_in', state.filters.short_selling_pressure_label_in);
  if (state.filters.max_days_to_options_expiration !== '' && state.filters.max_days_to_options_expiration != null) params.set('max_days_to_options_expiration', String(state.filters.max_days_to_options_expiration));
  if (state.filters.expiration_risk_only) params.set('expiration_risk_flag', 'true');
  return params.toString();
}
