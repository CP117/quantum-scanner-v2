/*
 * help.js - inline contextual help blurbs for every metric / control.
 *
 * Strategy: a single content table maps CSS-selector -> blurb text.  On
 * DOMContentLoaded we inject a 16px "ⓘ" icon next to each matched element.
 * Desktop users get a native-feeling tooltip on hover; mobile users tap
 * the icon and a small popover appears below it (and closes on the next
 * tap anywhere).
 *
 * Why an external content table rather than `title=""` attributes on
 * every element:
 *   1. all the blurbs live in one place so they stay consistent and
 *      easy to update,
 *   2. mobile Safari ignores `title=""`,
 *   3. the popover supports multiple lines of formatted text without
 *      relying on browser tooltip rendering.
 */
(function () {
  'use strict';

  /* eslint-disable no-multi-str */
  const HELP = {
    // ===== Header metric cards =====
    '[data-card="scan"]': 'How far through the universe the background scanner has gotten this sweep. Batch X / Y means it has scored X out of Y batches (25 symbols each). When it reaches the end it wraps to batch 0 and re-scores, picking up newly-stale data and refreshing the snapshot every device mirrors.',
    '[data-card="universe"]': 'Total tradable symbols loaded. The number on top is what is currently visible after your filters; the sub-line shows the normalized universe size (~12,300 US stocks, ~2,500 crypto).',
    '[data-card="pool"]': 'Active pool = symbols in the live top-10 page right now. Tracked = symbols that have survived in the top-25 across multiple consecutive scan sweeps. Higher tracked count = more persistent strength.',
    '[data-card="reactions"]': 'Counts how the recent supply/demand reaction zones near each scored ticker classified out. P = propel (price broke through), R = reject (price bounced), C = chop (sideways). A propel-heavy bar means most setups are trending; reject-heavy = mean-reverting market.',
    '[data-card="providers"]': 'Hit-rate of the multi-provider live-quote cascade: yfinance fast_info -> Yahoo Chart API -> Stooq -> CryptoCompare -> on-disk cache. >90% real means the scanner is getting fresh prices end-to-end. <50% means upstream providers are degrading and you are mostly seeing cached quotes.',
    '[data-card="options"]': 'CBOE options chain pulls in the form real : cached : skipped. Real are fresh chain reads; cached are <30s reads reused from RAM; skipped means options data is unavailable for that ticker (e.g. crypto, ETFs without listed options). Big "skipped" counts are normal for crypto-heavy filters.',
    '[data-card="history"]': '90-day daily OHLCV cache. Fresh = pulled this run from yfinance; cache = on-disk from a previous run. This cache powers the institutional confluence, volume sentiment and reaction-clustering factor families - if it is empty those factors fall back to a warming "insufficient_history" status.',
    '[data-card="coverage"]': 'Percentage of scored rows whose factor families returned "implemented" (real bars + real calculation) vs "warming" / "unavailable" (no data yet, fell back to a 50 placeholder). 100% means every visible row is using real data; lower numbers mean the snapshot is still warming up.',

    // ===== Ranked-results table column headers =====
    'th.col-symbol': 'Ticker symbol. Click any row to load full per-symbol detail (factor breakdowns, charts, history) into the right-hand panel.',
    'th.col-name': 'Company / coin display name as resolved from the universe metadata.',
    'th.col-score': 'Composite 0-100 score blended across all 7 factor families, weighted by historical predictive power. Roughly: >75 = strong setup, 50-65 = neutral / cautious, <40 = avoid / counter-trend. A small warning flag means the row was scored against incomplete data and the score is provisional.',
    'th.col-tier': 'A = top 10% of the universe by composite score, B = top 25%, C = top 50%, D = bottom 50%. Tiers re-rank each batch as new rows arrive.',
    'th.col-dir': 'Blended directional bias from the factor consensus (institutional + options + volume sentiment + reaction outcome).',
    'th.col-pill[title="Institutional confluence"]': 'Institutional confluence (INST): 0-100. Cross-timeframe RS + RRG quadrant + sector breadth alignment. "leading" quadrant = institutions accumulating, "lagging" = distributing. Dash (-) = warming (no 90-day history cached yet).',
    'th.col-pill[title="Options positioning"]': 'Options positioning (OPTS): 0-100. Put/call ratio + dealer gamma exposure + IV skew. Bullish = call-heavy gamma + flatter skew. Bearish = put-heavy + steep skew. Empty cell = no listed options or chain unavailable.',
    'th.col-pill[title="Dark pool proxy"]': 'Dark pool proxy (DP): off-exchange volume estimate + price-vs-volume disagreement. Attracting = volume building under VWAP (potential accumulation). Repelling = volume building above VWAP (potential distribution). Neutral = no signal.',
    'th.col-src': 'Where this row\'s live quote came from. yfinance = Yahoo fast_info (~real-time); yahoo-chart = Yahoo intraday API; stooq = backup feed; cryptocompare = crypto tail; cache = last-good quote when all live providers degraded.',
    'th.col-fresh': 'Quote age. fresh = pulled within the last 15 s. stale-ok = pulled within 5 min (still useful). stale = older than 5 min (treat with caution). preview = scored from cache only; no live quote was available.',
    'th.col-pass': 'How many consecutive scan sweeps this symbol has survived in the top-25. Higher = more persistent, less likely to be a one-batch fluke.',

    // ===== Sidebar - main filters =====
    'label[for="symbolSearch"]': 'Type any ticker prefix (AAPL, NVDA, BTC). Matches show below; click one to jump straight to its detail view.',
    'label[for="presetFilter"]': 'Pre-built filter combinations. Leaders = top tier by composite. Bullish bias = positive directional consensus. Reversal watch = effort-vs-result divergence. Institutional bullish = STRONG_BULL RRG quadrant. Options bullish = call-heavy gamma. Pin risk = high gamma flip price within 1%.',
    'label[for="directionFilter"]': 'Show only Bullish / Neutral / Bearish biased rows. Bias comes from the blended factor consensus, not just price action.',
    'label[for="tierFilter"]': 'Tier filter: A (top 10%), B (top 25%), C (top 50%), D (bottom 50%). Tiers are relative to the visible universe.',
    'label[for="minScoreFilter"]': 'Hide any row with composite score below this value. 0 = show everything. 70 = only strong-signal rows.',
    'label[for="minInstitutionalConfluenceFilter"]': 'Hide rows whose institutional confluence (INST) score is below this. Useful when you want to focus on RRG-leading names.',
    'label[for="minOptionsPositioningFilter"]': 'Hide rows whose options-positioning (OPTS) score is below this. Higher = stronger dealer gamma + put/call skew bias.',
    'label[for="minVolumeSentimentConvictionFilter"]': 'Hide rows whose Wyckoff/VSA conviction score is below this. Higher = stronger evidence of supply absorption or distribution effort.',
    'label[for="institutionalBiasFilter"]': 'Filter by Relative Rotation Graph (RRG) quadrant. STRONG_BULL = leading + improving; BULLISH = leading; NEUTRAL = lagging-but-improving / leading-but-weakening; BEARISH = lagging; STRONG_BEAR = lagging + weakening.',
    'label[for="optionsBiasFilter"]': 'Filter by options-positioning consensus. Bullish = call-heavy gamma + bullish put/call ratio. Bearish = inverse. Neutral = balanced.',
    'label[for="iobStateFilter"]': 'Institutional order block state. Fresh = just printed and untested. Holding = price still respecting the block. Tested = price has visited once and bounced. Stale = touched too many times or aged out.',
    'label[for="darkPoolAttractionFilter"]': 'Off-exchange flow direction. Attracting = dark-pool volume building below VWAP (accumulation pattern). Repelling = building above VWAP (distribution pattern). Neutral = no edge either way.',
    'label[for="optionsGammaFilter"]': 'Dealer gamma exposure level: where the market-maker hedging flow is concentrated. High call pressure / high put pressure = lots of nearby gamma to absorb price moves.',
    'label[for="reactionClassificationFilter"]': 'Dominant outcome from the reaction-clustering analysis of recent supply/demand zones. Propel = breakout-through-zone. Reject = bounce off zone. Chop = oscillating inside zone.',
    'label[for="dominantZoneTierFilter"]': 'Strength tier of the closest supply/demand reaction zone. Major = wide range + high volume zone. Intermediate = mid. Minor = narrow / low-volume.',
    'label[for="volumeSentimentBiasFilter"]': 'Directional reading from Wyckoff-style volume analysis (effort vs result, accumulation vs distribution).',
    'label[for="effortVsResultFilter"]': 'VSA primitives. Absorbing = heavy volume with no price progress (someone is soaking up supply). Capitulating = heavy volume + big move (climactic). Efficient = volume and price move together (trending). Neutral = no extreme.',
    'label[for="sortByFilter"]': 'Reorder the table by the selected factor (always descending). Default = composite score. Use this to drill into a specific factor family.',
    'label[for="addSymbolInput"]': 'Manually add a ticker that is not in the default universe. Useful for thinly-traded names, ADRs, or recently-IPO\'d symbols. Persisted across restarts.',

    // ===== Sidebar action buttons =====
    '#viewMainButton': 'Show the US equity universe (~12,300 stocks). Default view on startup.',
    '#viewCryptoButton': 'Switch to the crypto universe (~2,500 ranked coins by CoinGecko + CryptoCompare market cap order). Top-40 majors (BTC, ETH, ...) are anchored at ranks 1-40.',
    '#viewTrackedButton': 'Show only symbols that have been tracked through multiple scan sweeps (i.e. that consistently rank in the top-25). High-conviction watchlist view.',
    '#applyFilters': 'Apply every filter / slider / select you have changed since the last apply. Until you click Apply, the table is showing the previous filter set.',
    '#clearFilters': 'Reset every filter to its default value (any direction, any tier, min score 0, etc.).',
    '#manualRefresh': 'Re-pull live quotes for the currently visible rows right now, bypassing the snapshot cache. Useful after a market event when you want fresh prices on the rows you can see.',

    // ===== Tracker block =====
    '.tracker-card .eyebrow': 'A symbol becomes "tracked" once it has appeared in the live top-25 across at least two consecutive scan sweeps. The tracker filters out one-batch noise so you can focus on names with persistent strength.',

    // =====================================================================
    // Regulatory Monitor (regulatory.html) - panels, stat cards, controls.
    // Selectors here use data-testid attributes so they survive any inline
    // restyling on that page.
    // =====================================================================
    '[data-testid="reg-autoscan-pill"]': 'Live progress of the background universe auto-scan. Format: "autoscan: X/Y (Z%) · N hits · CURRENT_TICKER". X = tickers scanned this sweep, Y = total resolvable tickers (~7,000), N = how many of those came back with any insider or contract activity. The whole sweep takes ~4 hours by default; results stream in continuously.',
    '[data-testid="reg-trigger-autoscan"]': 'Forces an immediate full sweep of the resolvable universe right now, regardless of the scheduler interval. Safe to spam; concurrent requests are coalesced into a single sweep via an asyncio lock.',
    '[data-testid="reg-auto-results"]': 'Auto-populating ranked list of every ticker in the universe that has insider activity OR a recent federal contract win. Sorted by absolute impact on the scanner composite score (freshest first, biggest move first). Click any row to open that ticker in the main scanner.',

    '[data-testid="reg-input-cik"]': 'SEC Central Index Key for a single issuer (e.g. 320193 = Apple). Used by the manual "Run scan" path to pull Forms 3/4/5 and SC 13D/G filings for one specific company.',
    '[data-testid="reg-input-recipient"]': 'Company name (case-insensitive prefix match) used to query USAspending.gov for federal contract awards. Best paired with the issuer CIK above so both halves of the scan target the same company.',
    '[data-testid="reg-input-limit"]': 'How many of the most recent filings + awards to pull per manual scan. Default 8. Bigger limits = more API calls + slower response.',
    '[data-testid="reg-run-scan"]': 'Fires a one-off scan against just the CIK + recipient above. Results land in the Insider / Awards / Alerts panels below. Does NOT add the company to the tracked-list — for that, save a watchlist.',
    '[data-testid="reg-save-watchlist"]': 'Persists the current CIK + recipient combination to SQLite. Saved watchlists are re-polled automatically by the scheduler (when enabled) so you keep getting fresh insider events without re-running the manual scan.',
    '[data-testid="reg-poll-watchlists"]': 'Manually triggers the watchlist scheduler tick once — iterates every saved watchlist, pulls new SEC filings + USAspending awards, raises correlation alerts if both fire on the same entity.',
    '[data-testid="reg-discover"]': 'Pulls the SEC EDGAR "latest filings" Atom feed and auto-discovers brand-new public companies as they file Form 3 (first insider statement of ownership). Discovered companies are added to the tracked list and scanned on the next sweep.',
    '[data-testid="reg-scan-tracked"]': 'Re-scans every company in the tracked-companies table (the union of saved watchlists + auto-discovered + universe-autoscan hits). Useful after toggling settings to re-baseline the dataset.',

    '[data-testid="reg-stat-watch"]': 'Count of explicit watchlists you have saved (CIK + recipient pairs). Watchlists are the only entities the legacy scheduler polls; the universe auto-scan runs independently.',
    '[data-testid="reg-stat-tracked"]': 'Count of distinct companies the monitor knows about — the union of saved watchlists, auto-discovered Form 3 filers, and every universe-autoscan hit.',
    '[data-testid="reg-stat-filings"]': 'Total unique insider filings (Forms 3/4/5 + SC 13D/G) persisted to the local SQLite DB. Grows steadily as the auto-scan walks the universe.',
    '[data-testid="reg-stat-awards"]': 'Total unique federal contract awards (USAspending.gov) persisted locally. Each award carries a recipient name, agency, amount, and action date.',
    '[data-testid="reg-stat-alerts"]': 'Correlation alerts raised when entity linking finds the same company in BOTH the insider stream AND the contract-award stream within the lookback window — a particularly high-signal event.',

    '[data-testid="reg-insider-panel"]': 'All insider-activity events the monitor has captured: SEC Forms 3/4/5 (insider buys/sells of company shares) and SC 13D/G (5%+ beneficial-owner disclosures). Click any card to see the full filing details.',
    '[data-testid="reg-awards-panel"]': 'All federal contract awards captured from USAspending.gov. Each card shows recipient, awarding agency, amount, and description. Click for full detail including award ID and dates.',
    '[data-testid="reg-signal-panel"]': 'Live index of which tickers are currently nudging composite scores in the main scanner. Each row shows the applied delta (±points), confidence, event count and freshness. Refreshes every 60s from SQLite.',
    '[data-testid="reg-refresh-signal"]': 'Force-rebuild the active signal index right now instead of waiting for the 60s background refresh tick.',
    '[data-testid="reg-alerts-panel"]': 'Correlation alerts raised when insider activity AND contract awards appear for the same entity within the lookback window. Highest-signal events in the system.',
    '[data-testid="reg-watchlists-panel"]': 'All watchlists you have saved (CIK + recipient pairs). Click any row to re-run that scan in isolation.',
    '[data-testid="reg-tracked-panel"]': 'Full list of companies the monitor is tracking — the union of saved watchlists, auto-discovered filers, and universe-autoscan hits.',

    '[data-testid="reg-set-enable-autoscan"]': 'Master toggle for the universe-wide background auto-scan. ON by default. When ON, the monitor continuously walks the scanner universe and pulls SEC + USAspending data for every CIK-resolvable ticker.',
    '[data-testid="reg-set-autoscan-interval"]': 'How often (in seconds) the universe-wide sweep restarts after completing. Default 14400 (4 hours). Smaller = fresher data but more SEC API load.',
    '[data-testid="reg-set-autoscan-gap"]': 'Delay between consecutive SEC requests during the sweep, in milliseconds. Default 120ms = ~8 req/sec (well under SEC\'s 10/sec policy). Raise this if you start hitting 429 rate limits.',
    '[data-testid="reg-set-autoscan-perlim"]': 'How many of the most recent filings to pull per ticker during the sweep. Default 3 — keeps the sweep fast and focused on fresh activity.',
    '[data-testid="reg-set-autoscan-cap"]': 'Hard cap on how many tickers to scan per sweep. 0 = scan the entire resolvable universe (~7,000). Useful for capping API usage on slower networks.',
    '[data-testid="reg-set-enable-scheduler"]': 'Toggle the legacy watchlist scheduler (polls only your saved watchlists, NOT the whole universe). OFF by default — the universe auto-scan above already covers your watchlist tickers.',
    '[data-testid="reg-set-enable-discovery"]': 'Toggle SEC Atom-feed auto-discovery of brand-new public-company filers. OFF by default. ON adds a low-traffic poller that catches IPOs / spin-offs on day one.',
    '[data-testid="reg-set-enable-linking"]': 'Toggle entity-linking between insider issuers and contract-award recipients. ON by default. Powers the correlation alerts shown above.',
    '[data-testid="reg-set-scheduler-interval"]': 'Legacy watchlist scheduler poll interval, in seconds. Only used when the watchlist scheduler is toggled ON above.',
    '[data-testid="reg-set-scan-limit"]': 'Default `limit` value for one-off scans and scheduler ticks. Maximum number of filings + awards to pull per company per tick.',
    '[data-testid="reg-set-discovery-interval"]': 'How often (seconds) the SEC Atom auto-discovery poller fires. Only used when Atom auto-discovery is toggled ON.',
    '[data-testid="reg-set-award-threshold"]': 'Dollar threshold above which a single contract award is flagged as "large" (raised in alerts even without insider correlation). Default $1,000,000.',
    '[data-testid="reg-set-ownership-threshold"]': 'Beneficial-ownership percentage that triggers a high-priority alert on SC 13D/G filings. Default 5% (matches the SEC filing trigger).',
    '[data-testid="reg-set-signal-max-age"]': 'How many days old an insider/award event can be before it is excluded from the composite-score signal entirely. Default 5 days. Older events have no influence on the scanner ranking.',
    '[data-testid="reg-set-signal-decay"]': 'Event age (days) at which the signal\'s contribution to the composite begins to halve and its confidence is multiplied by 0.6. Default 3 days. Bridges fresh-vs-stale.',
    '[data-testid="reg-set-signal-max-boost"]': 'Hard ceiling on how many composite points a single ticker can be nudged by regulatory activity. Default ±8 points. Per-event scores squash through a tanh into this cap so 1×$10M and 10×$1M both converge gracefully.',
    '[data-testid="reg-set-signal-min-dollar"]': 'Dollar notional below which an event gets near-zero weight in the composite nudge. Default $25,000. Stops tiny option-grant filings from moving the score.',
    '[data-testid="reg-set-signal-strong-dollar"]': 'Dollar notional at which an event gets FULL weight in the composite nudge. Default $1,000,000. Between the min ($25k) and this value, the weight ramps up linearly.',
    '[data-testid="reg-save-settings"]': 'Persist every setting in this panel to SQLite. The scheduler picks up the new values on its next tick (no restart needed).',

    'a.backlink[data-testid="back-to-scanner"]': 'Return to the main Market Refinement Dashboard. Your regulatory monitor keeps scanning in the background regardless of which tab is open.',

    // Detail panel action buttons (Phase 18)
    '#predictBtn, [data-testid="predict-btn"]': 'Generate a 10-day forward price target by aggregating every factor family (TVD, ICF, OPS, IOB, DP, VS, RC) plus reaction classification and volume conviction. Output includes target, 95% confidence band, per-factor contributions, and reasoning bullets so you can audit the math.',
    '#backtestBtn': 'Walk-forward evaluate the reaction-clustering classifier on this symbol over ~250 daily bars. Reports raw hit rate, confident hit rate (high-conviction calls only), balanced accuracy, and per-class precision so you can judge whether the classifier has real edge.',

    // =====================================================================
    // Detail panel (right side of dashboard) - per-factor card labels +
    // operational context. These selectors target the eyebrow labels
    // inside each .factor-card and .rating-card.
    // =====================================================================
    '.detail-card:nth-child(1) .eyebrow': 'The ticker symbol you are inspecting. Provenance badge shows which provider supplied the live quote.',
    '.detail-card:nth-child(2) .eyebrow': 'The blended 0-100 composite score: 80% from the core M/Q/T/S algorithm + 20% from the 7 factor families, with a +/-5pt predictive-consensus modifier on top.',
    '.detail-card:nth-child(3) .eyebrow': 'Tier (A-D, relative to the visible universe) plus the directional bias derived from the factor consensus.',
    '.detail-card:nth-child(4) .eyebrow': 'How many consecutive scan sweeps this symbol has appeared in the live top-25. Higher = more persistent strength.',

    '.rating-card:nth-child(1) .eyebrow': 'Momentum (35% weight): short-term price thrust. Combines change-vs-previous-close with the consistency of intraday moves.',
    '.rating-card:nth-child(2) .eyebrow': 'Quality (25% weight): intraday tradability + participation. Folds in relative volume, turnover, session extension, range position, gap efficiency, intraday volatility, and bid-ask spread.',
    '.rating-card:nth-child(3) .eyebrow': 'Trend (20% weight): directional persistence over the session. Captures whether price is sustaining or fading.',
    '.rating-card:nth-child(4) .eyebrow': 'Stability (20% weight): whether the move is holding together intraday. Penalises large adverse retracements and rewards directional consistency.',

    '.composite-breakdown .cs-title': 'How the final composite was computed. Shows each of the 7 factor family scores, the core M/Q/T/S blend, and any +/- modifier from predictive consensus.',
    '.composite-breakdown .cs-formula': 'Exact formula used for this row: core x 0.80 + extended-avg x 0.20, optionally plus a predictive-consensus modifier capped at +/-5 points.',

    '.factor-extended:nth-of-type(1) .eyebrow': 'Trend-volume delta: directional bucket derived from price change x relative volume. Strong-bullish/bearish flags trigger when volume is more than ~1x average AND price moves more than 1 ATR in the same direction.',
    '.factor-extended:nth-of-type(2) .eyebrow': 'Institutional confluence: composite of relative-rotation (RRG), unusual-volume flow, ATR-based regime, liquidity-sweep evidence and session timing. The single most reliable bull/bear signal in the suite.',
    '.factor-extended:nth-of-type(3) .eyebrow': 'Volume sentiment: Wyckoff/VSA substrate. Buy/sell volume pressure, accumulation/distribution, effort-vs-result label, regime, and z-score. Used to modulate IOB + Options reactions.',
    '.factor-extended:nth-of-type(4) .eyebrow': 'Reaction clustering: detects supply/demand zones from pivots, ranks by evidence (touches, rejection magnitude, volume at level, recency), classifies the dominant outcome as PROPEL/REJECT/CHOP using volume-sentiment alignment.',
    '.factor-extended:nth-of-type(5) .eyebrow': 'Options positioning: weighted put/call pressure across near-term + monthly expirations. Real chain when available, inferred from intraday shape otherwise. Pressure score is adjusted by the volume-sentiment substrate.',
    '.factor-extended:nth-of-type(6) .eyebrow': 'Institutional order block: V1 heuristic for detecting a recent strong impulse followed by a retest of the impulse origin. Expected reaction blends zone evidence with the live volume sentiment.',
    '.factor-extended:nth-of-type(7) .eyebrow': 'Dark pool proxy: V1 heuristic surfacing print clusters (large-volume bars inside tight ranges = hidden absorption). High pinning effect = price likely to mean-revert to the cluster.',
  };
  /* eslint-enable no-multi-str */

  // ---------------------------------------------------------------------
  // 1) Build the icon + popover infrastructure (single shared element).
  // ---------------------------------------------------------------------
  let activeIcon = null;
  const popover = document.createElement('div');
  popover.className = 'help-popover';
  popover.setAttribute('role', 'tooltip');
  popover.style.display = 'none';
  document.body.appendChild(popover);

  function hidePopover() {
    popover.style.display = 'none';
    if (activeIcon) {
      activeIcon.setAttribute('aria-expanded', 'false');
      activeIcon = null;
    }
  }

  function showPopoverFor(icon) {
    if (activeIcon === icon) { hidePopover(); return; }
    const text = icon.getAttribute('data-help-text') || '';
    if (!text) return;
    popover.textContent = text;
    popover.style.display = 'block';

    // Position below the icon, but flip above if there is not enough room.
    const r = icon.getBoundingClientRect();
    const pr = popover.getBoundingClientRect();
    let top = window.scrollY + r.bottom + 6;
    let left = window.scrollX + r.left - 8;
    // Keep within viewport horizontally
    const overflowRight = (left + pr.width) - (window.scrollX + window.innerWidth - 12);
    if (overflowRight > 0) left -= overflowRight;
    if (left < window.scrollX + 8) left = window.scrollX + 8;
    // Flip above if needed
    if (r.bottom + 6 + pr.height > window.innerHeight - 8 && r.top - pr.height - 6 > 8) {
      top = window.scrollY + r.top - pr.height - 6;
    }
    popover.style.top = top + 'px';
    popover.style.left = left + 'px';

    activeIcon = icon;
    icon.setAttribute('aria-expanded', 'true');
  }

  // ---------------------------------------------------------------------
  // 2) Inject icons next to every targeted element.
  // ---------------------------------------------------------------------
  function makeIcon(text) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'help-icon';
    btn.setAttribute('aria-label', 'Help: ' + text.slice(0, 80));
    btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('data-help-text', text);
    btn.title = text;
    btn.textContent = 'i';
    return btn;
  }

  function injectAll() {
    Object.entries(HELP).forEach(([selector, text]) => {
      document.querySelectorAll(selector).forEach((el) => {
        // Don't double-inject
        if (el.querySelector(':scope > .help-icon')) return;
        // For table headers, append inside the <th>; for labels too.
        const icon = makeIcon(text);
        el.appendChild(icon);
      });
    });
  }

  // ---------------------------------------------------------------------
  // 3) Wire up interaction (click toggle, click-outside close, hover).
  // ---------------------------------------------------------------------
  document.addEventListener('click', (e) => {
    const icon = e.target.closest && e.target.closest('.help-icon');
    if (icon) {
      e.preventDefault();
      e.stopPropagation();
      showPopoverFor(icon);
      return;
    }
    if (activeIcon && !popover.contains(e.target)) hidePopover();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') hidePopover();
  });
  window.addEventListener('scroll', hidePopover, true);
  window.addEventListener('resize', hidePopover);

  // ---------------------------------------------------------------------
  // 4) Initialise after the DOM is ready (and once more after app.js
  //    finishes rendering filters etc.).
  // ---------------------------------------------------------------------
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => { injectAll(); });
  } else {
    injectAll();
  }
  // Re-run a moment later to catch any elements rendered by app.js's first
  // render pass (preset chips, etc.).
  setTimeout(injectAll, 400);
  setTimeout(injectAll, 1500);
})();
