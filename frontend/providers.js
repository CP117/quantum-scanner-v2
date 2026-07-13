// Phase 26.2: Data Providers Health page client.
//
// - Polls /api/providers/status every 10 seconds when auto-refresh is on.
// - Renders an aggregate health summary, the quote-provider cascade table
//   (with health bars + last-error tooltips), API-key-unlocked providers,
//   options/daily-history/reaction-clustering telemetry, cache+warmer,
//   blacklist counts, and recent failure context.
// - Fully read-only; no mutating actions live on this page.

(function () {
  'use strict';

  const REFRESH_MS = 10000;
  const $ = (id) => document.getElementById(id);
  const fmtInt = (n) => (Number.isFinite(n) ? Number(n).toLocaleString() : '\u2014');
  const fmtPct = (frac) => {
    if (frac == null || !Number.isFinite(frac)) return '\u2014';
    return (frac * 100).toFixed(1) + '%';
  };
  const fmtTs = (s) => {
    if (!s) return '\u2014';
    try {
      const d = new Date(s);
      if (isNaN(d.getTime())) return s;
      return d.toLocaleTimeString([], { hour12: false }) + ' \u00b7 ' + d.toLocaleDateString();
    } catch (_) {
      return s;
    }
  };
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  let refreshTimer = null;
  let inFlight = false;

  async function fetchStatus() {
    if (inFlight) return null;
    inFlight = true;
    try {
      const res = await fetch('/api/providers/status', { credentials: 'same-origin' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return await res.json();
    } catch (err) {
      console.warn('providers: fetch failed', err);
      return { error: String(err && err.message ? err.message : err) };
    } finally {
      inFlight = false;
    }
  }

  function renderAggregate(data) {
    const agg = data.aggregate || {};
    const stateLabel = data.state === 'ok' ? 'OK' : (data.state || 'unknown').toUpperCase();
    const stateClass = data.state === 'ok' ? 'green' : data.degraded_mode ? 'amber' : 'red';
    const offlineCard = data.offline_mode
      ? `<div class="stat-card red"><div class="label">Mode</div><div class="value">OFFLINE</div><div class="sub">All providers failing + cache empty</div></div>`
      : '';
    const cards = [
      `<div class="stat-card ${stateClass}" data-testid="agg-state">
         <div class="label">Overall state</div>
         <div class="value">${esc(stateLabel)}</div>
         <div class="sub">${data.degraded_mode ? 'Degraded mode active' : 'No active degradation'}</div>
       </div>`,
      `<div class="stat-card blue" data-testid="agg-total-calls">
         <div class="label">Total calls</div>
         <div class="value">${fmtInt(agg.total_calls)}</div>
         <div class="sub">${fmtInt(agg.total_hits)} hits / ${fmtInt(agg.total_misses)} misses</div>
       </div>`,
      `<div class="stat-card ${agg.hit_rate != null && agg.hit_rate >= 0.7 ? 'green' : 'amber'}" data-testid="agg-hit-rate">
         <div class="label">Aggregate hit rate</div>
         <div class="value">${fmtPct(agg.hit_rate)}</div>
         <div class="sub">${fmtInt(agg.total_errors)} errors total</div>
       </div>`,
      `<div class="stat-card ${agg.error_rate != null && agg.error_rate >= 0.1 ? 'red' : 'slate'}" data-testid="agg-error-rate">
         <div class="label">Error rate</div>
         <div class="value">${fmtPct(agg.error_rate)}</div>
         <div class="sub">${data.recent_fetch_error_summary ? 'Latest error in panel below' : 'Clean'}</div>
       </div>`,
      `<div class="stat-card ${agg.total_rate_limits > 0 ? 'red' : 'slate'}" data-testid="agg-rate-limits">
         <div class="label">429 / rate-limit</div>
         <div class="value">${fmtInt(agg.total_rate_limits)}</div>
         <div class="sub">${fmtInt(agg.total_timeouts)} timeouts</div>
       </div>`,
      offlineCard,
    ].filter(Boolean).join('');
    $('aggregateGrid').innerHTML = cards;
  }

  function renderQuoteProviders(data) {
    const rows = data.quote_providers || [];
    if (!rows.length) {
      $('quoteProvidersTable').innerHTML = '<div class="empty-state">No quote-provider activity recorded yet.</div>';
      return;
    }
    const html = `
      <table class="providers" data-testid="quote-providers-table">
        <thead>
          <tr>
            <th>Provider</th>
            <th>Health</th>
            <th>Hit rate</th>
            <th class="num">Calls</th>
            <th class="num">Hits</th>
            <th class="num">Misses</th>
            <th class="num">Errors</th>
            <th class="num" title="HTTP 429 / quota / throttle responses">429</th>
            <th class="num" title="Connection or read timeouts">Timeouts</th>
            <th>Last success</th>
            <th>Last error</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((r) => {
            const hitPct = r.hit_rate != null ? Math.round(r.hit_rate * 100) : 0;
            const barClass = r.health === 'critical' ? 'critical'
              : r.health === 'degraded' ? 'degraded'
              : r.health === 'idle' ? 'idle' : '';
            const barWidth = r.health === 'idle' ? 0 : Math.max(2, Math.min(100, hitPct));
            const errMsg = r.last_error;
            const errTs = r.last_error_utc;
            const errCell = errMsg
              ? `<span class="err-cell" title="${esc(errMsg)}${errTs ? ' (' + fmtTs(errTs) + ')' : ''}">${errTs ? `<span class="muted" style="font-size:.7rem">${fmtTs(errTs)}</span><br>` : ''}${esc(errMsg)}</span>`
              : '<span class="muted">\u2014</span>';
            const successCell = r.last_success_utc
              ? `<span style="font-size:.74rem;color:#cbd5e1" title="${esc(r.last_success_utc)}">${fmtTs(r.last_success_utc)}</span>`
              : '<span class="muted">\u2014</span>';
            return `
              <tr data-testid="quote-provider-row-${esc(r.name)}">
                <td class="prov-name">${esc(r.name)}</td>
                <td>
                  <span class="health-badge health-${esc(r.health)}">${esc(r.health)}</span>
                  <div class="health-bar-wrap" style="margin-top:6px">
                    <div class="health-bar-fill ${barClass}" style="width:${barWidth}%"></div>
                  </div>
                </td>
                <td class="num">${fmtPct(r.hit_rate)}</td>
                <td class="num">${fmtInt(r.calls)}</td>
                <td class="num">${fmtInt(r.hits)}</td>
                <td class="num">${fmtInt(r.misses)}</td>
                <td class="num" style="color:${r.errors > 0 ? '#f87171' : 'inherit'}">${fmtInt(r.errors)}</td>
                <td class="num" style="color:${r.rate_limits > 0 ? '#fbbf24' : 'inherit'}">${fmtInt(r.rate_limits)}</td>
                <td class="num" style="color:${r.timeouts > 0 ? '#fbbf24' : 'inherit'}">${fmtInt(r.timeouts)}</td>
                <td>${successCell}</td>
                <td>${errCell}</td>
              </tr>`;
          }).join('')}
        </tbody>
      </table>`;
    $('quoteProvidersTable').innerHTML = html;
  }

  function renderCircuitBreakers(data) {
    const rows = data.circuit_breakers || [];
    const el = $('circuitBreakersTable');
    if (!el) return;
    if (!rows.length) {
      el.innerHTML = '<div class="empty-state">No circuit-breaker telemetry yet.</div>';
      return;
    }
    const fmtSec = (s) => {
      if (s == null || !Number.isFinite(s) || s <= 0) return '\u2014';
      if (s < 90) return s.toFixed(0) + 's';
      if (s < 3600) return (s / 60).toFixed(1) + 'm';
      return (s / 3600).toFixed(1) + 'h';
    };
    const html = `
      <table class="providers" data-testid="circuit-breakers-table">
        <thead>
          <tr>
            <th>Provider</th>
            <th>Kind</th>
            <th>State</th>
            <th class="num">Consecutive failures</th>
            <th class="num">Trip threshold</th>
            <th>Cooldown</th>
            <th>Remaining</th>
            <th class="num">Trips ever</th>
            <th>Last trip</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((r) => {
            const isOpen = r.state === 'open';
            const isHalfOpen = r.state === 'half_open';
            const stateClass = isOpen ? 'health-critical'
              : isHalfOpen ? 'health-degraded' : 'health-healthy';
            const stateLabel = isOpen ? 'open (paused)'
              : isHalfOpen ? 'half-open (probe)' : 'closed';
            const failsCls = (r.consecutive_failures || 0) >= (r.threshold || 5) ? 'color:#f87171' : '';
            return `
              <tr data-testid="circuit-breaker-row-${esc(r.provider)}">
                <td class="prov-name">${esc(r.provider)}</td>
                <td><span class="muted" style="font-size:.74rem">${esc(r.kind)}</span></td>
                <td><span class="health-badge ${stateClass}">${esc(stateLabel)}</span></td>
                <td class="num" style="${failsCls}">${fmtInt(r.consecutive_failures)}</td>
                <td class="num">${fmtInt(r.threshold)}</td>
                <td>${fmtSec(r.cooldown_seconds)}</td>
                <td style="color:${isOpen ? '#fbbf24' : 'inherit'}">${fmtSec(r.remaining_seconds)}</td>
                <td class="num" style="color:${r.trip_count > 0 ? '#fbbf24' : 'inherit'}">${fmtInt(r.trip_count)}</td>
                <td><span class="muted" style="font-size:.74rem">${r.last_trip_utc ? fmtTs(r.last_trip_utc) : '\u2014'}</span></td>
              </tr>`;
          }).join('')}
        </tbody>
      </table>`;
    el.innerHTML = html;
  }

  function renderOptionsChainProviders(data) {
    const el = $('optionsChainProvidersTable');
    if (!el) return;
    const rows = data.options_chain_providers || [];
    const stats = data.options_chain || {};
    if (!rows.length) {
      el.innerHTML = '<div class="empty-state">No options-chain telemetry yet.</div>';
      return;
    }
    const totalAttempts = rows.reduce((s, r) => s + (r.attempts || 0), 0);
    const totalHits = rows.reduce((s, r) => s + (r.hits || 0), 0);
    const aggHit = totalAttempts > 0 ? (totalHits / totalAttempts) : null;
    const cacheHits = stats.cache_hits || 0;
    const fallbackHits = stats.fallback_to_yahoo || 0;
    const noOptionsSkips = stats.no_options_skips || 0;
    const throttleSkips = stats.throttle_skips || 0;
    const summaryHtml = `
      <div class="provider-summary" data-testid="options-chain-summary" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:14px">
        <div style="padding:10px 12px;background:#0f172a;border:1px solid #374151;border-radius:8px"><div style="color:#9ca3af;font-size:.74rem;text-transform:uppercase;letter-spacing:.04em">Total fetches</div><div style="font-variant-numeric:tabular-nums;color:#e5e7eb;font-weight:700;font-size:1.05rem;margin-top:4px">${fmtInt(totalAttempts)}</div></div>
        <div style="padding:10px 12px;background:#0f172a;border:1px solid #374151;border-radius:8px"><div style="color:#9ca3af;font-size:.74rem;text-transform:uppercase;letter-spacing:.04em">Cascade hit-rate</div><div style="font-variant-numeric:tabular-nums;color:${aggHit != null && aggHit >= 0.6 ? '#34d399' : '#e5e7eb'};font-weight:700;font-size:1.05rem;margin-top:4px">${aggHit == null ? '\u2014' : (aggHit * 100).toFixed(1) + '%'}</div></div>
        <div style="padding:10px 12px;background:#0f172a;border:1px solid #374151;border-radius:8px"><div style="color:#9ca3af;font-size:.74rem;text-transform:uppercase;letter-spacing:.04em">Cache hits</div><div style="font-variant-numeric:tabular-nums;color:#e5e7eb;font-weight:700;font-size:1.05rem;margin-top:4px">${fmtInt(cacheHits)}</div></div>
        <div style="padding:10px 12px;background:#0f172a;border:1px solid #374151;border-radius:8px"><div style="color:#9ca3af;font-size:.74rem;text-transform:uppercase;letter-spacing:.04em">CBOE \u2192 Yahoo fallbacks</div><div style="font-variant-numeric:tabular-nums;color:${fallbackHits > 0 ? '#fbbf24' : '#e5e7eb'};font-weight:700;font-size:1.05rem;margin-top:4px">${fmtInt(fallbackHits)}</div></div>
        <div style="padding:10px 12px;background:#0f172a;border:1px solid #374151;border-radius:8px"><div style="color:#9ca3af;font-size:.74rem;text-transform:uppercase;letter-spacing:.04em">No-options skips</div><div style="font-variant-numeric:tabular-nums;color:#94a3b8;font-weight:700;font-size:1.05rem;margin-top:4px">${fmtInt(noOptionsSkips)}</div></div>
        <div style="padding:10px 12px;background:#0f172a;border:1px solid #374151;border-radius:8px"><div style="color:#9ca3af;font-size:.74rem;text-transform:uppercase;letter-spacing:.04em">Throttle skips</div><div style="font-variant-numeric:tabular-nums;color:#94a3b8;font-weight:700;font-size:1.05rem;margin-top:4px">${fmtInt(throttleSkips)}</div></div>
      </div>`;
    const tableHtml = `
      <table class="providers" data-testid="options-chain-providers-table">
        <thead>
          <tr>
            <th>Source</th>
            <th>Role</th>
            <th class="num">Attempts</th>
            <th class="num">Hits</th>
            <th class="num">Misses</th>
            <th class="num">Errors</th>
            <th class="num">Hit rate</th>
            <th>Circuit</th>
            <th>Health</th>
            <th>Last success</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((r) => {
            const healthClass = r.health === 'healthy' ? 'health-healthy'
              : r.health === 'degraded' ? 'health-degraded'
              : r.health === 'critical' ? 'health-critical'
              : 'health-idle';
            const cbState = r.circuit_state || '\u2014';
            const cbClass = cbState === 'open' ? 'health-critical'
              : cbState === 'half_open' ? 'health-degraded'
              : cbState === 'closed' ? 'health-healthy' : '';
            const kindLabel = r.kind === 'primary' ? 'PRIMARY' : 'FALLBACK';
            const kindCls = r.kind === 'primary' ? 'color:#10b981;font-weight:600' : 'color:#94a3b8';
            const hitRate = r.hit_rate == null ? '\u2014' : (r.hit_rate * 100).toFixed(1) + '%';
            return `
              <tr data-testid="options-chain-row-${esc(r.provider)}">
                <td class="prov-name">${esc(r.provider)}</td>
                <td><span style="${kindCls};font-size:.74rem;letter-spacing:.05em">${kindLabel}</span></td>
                <td class="num">${fmtInt(r.attempts)}</td>
                <td class="num" style="color:#10b981">${fmtInt(r.hits)}</td>
                <td class="num" style="color:#94a3b8">${fmtInt(r.misses)}</td>
                <td class="num" style="color:${r.errors > 0 ? '#f87171' : 'inherit'}">${fmtInt(r.errors)}</td>
                <td class="num">${hitRate}</td>
                <td>${cbClass ? `<span class="health-badge ${cbClass}">${esc(cbState)}</span>` : '<span class="muted">n/a</span>'}</td>
                <td><span class="health-badge ${healthClass}">${esc(r.health || 'idle')}</span></td>
                <td><span class="muted" style="font-size:.74rem">${r.last_success_utc ? fmtTs(r.last_success_utc) : '\u2014'}</span></td>
              </tr>`;
          }).join('')}
        </tbody>
      </table>`;
    el.innerHTML = summaryHtml + tableHtml;
  }

  function renderApiKeys(data) {
    const cfg = data.api_keys_configured || {};
    const keys = Object.keys(cfg);
    if (!keys.length) {
      $('apiKeysGrid').innerHTML = '<div class="empty-state">No optional-provider unlocks tracked. Add API keys from the main dashboard sidebar to unlock additional providers.</div>';
      return;
    }
    const grid = keys.sort().map((name) => {
      const on = !!cfg[name];
      return `<div data-testid="api-key-status-${esc(name)}" style="display:flex;justify-content:space-between;gap:1rem;padding:7px 10px;border:1px solid #374151;border-radius:8px;background:#0f172a">
        <span style="text-transform:uppercase;letter-spacing:.05em;font-size:.78rem;color:#cbd5e1">${esc(name)}</span>
        <span class="pill ${on ? 'pill-on' : 'pill-off'}">${on ? 'configured' : 'not configured'}</span>
      </div>`;
    }).join('');
    $('apiKeysGrid').innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px">${grid}</div>`;
  }

  function renderKvGrid(elId, obj, opts) {
    const el = $(elId);
    if (!el) return;
    if (!obj || typeof obj !== 'object' || !Object.keys(obj).length) {
      el.innerHTML = '<div class="empty-state">No telemetry recorded yet.</div>';
      return;
    }
    const order = (opts && opts.order) || Object.keys(obj);
    const visible = order.filter((k) => k in obj);
    const extras = Object.keys(obj).filter((k) => !visible.includes(k));
    const all = visible.concat(extras);
    const rows = all.map((k) => {
      const v = obj[k];
      const display = v == null
        ? '\u2014'
        : (typeof v === 'number' && !Number.isInteger(v)
            ? v.toFixed(3)
            : (typeof v === 'object' ? JSON.stringify(v) : esc(String(v))));
      return `<div><span class="k">${esc(k)}</span><span class="v">${display}</span></div>`;
    }).join('');
    el.innerHTML = rows;
  }

  function renderBlacklist(data) {
    const bl = data.blacklist || {};
    const el = $('blacklistKv');
    if (!Object.keys(bl).length) {
      el.innerHTML = '<div class="empty-state">No blacklist activity yet.</div>';
      return;
    }
    renderKvGrid('blacklistKv', bl);
  }

  function renderCacheWarmer(data) {
    const merged = Object.assign({},
      data.cache ? Object.fromEntries(Object.entries(data.cache).map(([k, v]) => [`cache.${k}`, v])) : {},
      data.warmer ? Object.fromEntries(Object.entries(data.warmer).map(([k, v]) => [`warmer.${k}`, v])) : {},
    );
    renderKvGrid('cacheWarmerKv', merged);
  }

  function renderRecentErrors(data) {
    const payload = {
      'state': data.state,
      'degraded_mode': data.degraded_mode,
      'offline_mode': data.offline_mode,
      'last_refresh_utc': data.last_refresh_utc,
      'last_success_utc': data.last_success_utc,
      'last_failure_utc': data.last_failure_utc,
      'recent_fetch_error_summary': data.recent_fetch_error_summary,
    };
    const fc = data.failure_classes || {};
    for (const [k, v] of Object.entries(fc)) {
      payload[`failure_class.${k}`] = v;
    }
    renderKvGrid('recentErrors', payload);
  }

  function renderLastRefresh(data) {
    const label = $('lastRefresh');
    if (!label) return;
    if (data && data.error) {
      label.innerHTML = `<span style="color:#f87171">Error fetching /api/providers/status: ${esc(data.error)}</span>`;
      return;
    }
    const now = new Date();
    label.textContent = `Updated ${now.toLocaleTimeString([], { hour12: false })} \u00b7 last backend refresh ${fmtTs(data && data.last_refresh_utc)}`;
  }

  // Phase 26.7: counter-rotation timeline + reset-all wiring. The rotation
  // info comes from /api/admin/maintenance; the buttons hit /api/admin/*.
  async function refreshRotationStatus() {
    try {
      const res = await fetch('/api/admin/maintenance', { credentials: 'same-origin' });
      if (!res.ok) return;
      const data = await res.json();
      const ageEl = $('rotationAge');
      const countEl = $('rotationCount');
      const nextEl = $('rotationNext');
      if (!ageEl) return;
      const last = data.last_counter_rotation_utc;
      if (last) {
        const lastDt = new Date(last);
        const ageSec = Math.max(0, (Date.now() - lastDt.getTime()) / 1000);
        const ageLabel = ageSec < 60 ? `${ageSec.toFixed(0)}s ago`
          : ageSec < 3600 ? `${(ageSec / 60).toFixed(0)}m ago`
          : ageSec < 86400 ? `${(ageSec / 3600).toFixed(1)}h ago`
          : `${(ageSec / 86400).toFixed(1)}d ago`;
        ageEl.textContent = ageLabel;
        ageEl.title = `Rotated at ${fmtTs(last)}`;
      } else {
        ageEl.textContent = 'never (since process start)';
        ageEl.title = '';
      }
      countEl.textContent = data.counter_rotations > 0
        ? `\u00b7 ${data.counter_rotations} rotation${data.counter_rotations === 1 ? '' : 's'} so far`
        : '';
      const interval = Number(data.counter_rotate_interval_seconds) || 0;
      const intervalLabel = interval >= 3600
        ? `${(interval / 3600).toFixed(1)}h`
        : `${(interval / 60).toFixed(0)}m`;
      nextEl.textContent = `Auto-rotate every ${intervalLabel} \u00b7 DB prune every ${(Number(data.db_prune_interval_seconds) / 3600).toFixed(0)}h (regulatory ${data.regulatory_retention_days}d / predictions ${data.prediction_retention_days}d retention)`;
    } catch (err) {
      console.warn('rotation status fetch failed', err);
    }
  }

  function _wireResetButtons() {
    const rotateBtn = $('rotateNowBtn');
    const resetAllBtn = $('resetAllBtn');
    const doPost = async (url) => {
      const res = await fetch(url, { method: 'POST', credentials: 'same-origin' });
      if (!res.ok) throw new Error(`HTTP ${res.status} on ${url}`);
      return await res.json();
    };
    if (rotateBtn) {
      rotateBtn.addEventListener('click', async () => {
        rotateBtn.disabled = true;
        const original = rotateBtn.textContent;
        rotateBtn.textContent = 'Rotating\u2026';
        try {
          await doPost('/api/admin/rotate-counters');
          rotateBtn.textContent = 'Rotated';
          await Promise.all([refresh(), refreshRotationStatus()]);
        } catch (err) {
          rotateBtn.textContent = 'Failed';
          console.warn('rotate failed', err);
        } finally {
          setTimeout(() => {
            rotateBtn.disabled = false;
            rotateBtn.textContent = original;
          }, 1500);
        }
      });
    }
    if (resetAllBtn) {
      resetAllBtn.addEventListener('click', async () => {
        if (!confirm(
          'Reset ALL counters?\n\nThis clears provider call/hit/miss/error counters '
          + 'AND per-market scan-pass counters. Last-success/error timestamps and '
          + 'circuit-breaker state are preserved.'
        )) return;
        resetAllBtn.disabled = true;
        const original = resetAllBtn.textContent;
        resetAllBtn.textContent = 'Resetting\u2026';
        try {
          await Promise.all([
            doPost('/api/admin/rotate-counters'),
            doPost('/api/admin/reset-scan-counters'),
          ]);
          resetAllBtn.textContent = 'Reset complete';
          await Promise.all([refresh(), refreshRotationStatus()]);
        } catch (err) {
          resetAllBtn.textContent = 'Failed';
          console.warn('reset all failed', err);
        } finally {
          setTimeout(() => {
            resetAllBtn.disabled = false;
            resetAllBtn.textContent = original;
          }, 1800);
        }
      });
    }
  }

  async function refresh() {
    const data = await fetchStatus();
    if (!data) return;
    if (data.error) {
      renderLastRefresh(data);
      return;
    }
    try {
      renderAggregate(data);
      renderQuoteProviders(data);
      renderCircuitBreakers(data);
      renderApiKeys(data);
      renderOptionsChainProviders(data);
      renderKvGrid('optionsKv', data.options_chain);
      renderKvGrid('dailyHistoryKv', data.daily_history);
      renderKvGrid('reactionKv', data.reaction_clustering);
      renderKvGrid('stooqKv', data.stooq_diagnostics);
      renderCacheWarmer(data);
      renderBlacklist(data);
      renderRecentErrors(data);
      renderLastRefresh(data);
    } catch (err) {
      console.error('providers: render failed', err);
    }
  }

  function startAutoRefresh() {
    stopAutoRefresh();
    refreshTimer = setInterval(refresh, REFRESH_MS);
  }
  function stopAutoRefresh() {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = null;
  }

  document.addEventListener('DOMContentLoaded', () => {
    const toggle = $('autoRefreshToggle');
    const manual = $('manualRefresh');
    if (toggle) {
      toggle.addEventListener('change', () => {
        if (toggle.checked) startAutoRefresh();
        else stopAutoRefresh();
      });
    }
    if (manual) {
      manual.addEventListener('click', refresh);
    }
    _wireResetButtons();
    refresh();
    refreshRotationStatus();
    if (!toggle || toggle.checked) startAutoRefresh();
    // Rotation status only needs to be re-checked roughly every minute -
    // it changes on the maintenance loop's 6h schedule, not at /status's
    // 10s polling cadence.
    setInterval(refreshRotationStatus, 60000);
    // Pause polling when tab not visible to be polite to the scanner host.
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        stopAutoRefresh();
      } else if (toggle && toggle.checked) {
        refresh();
        startAutoRefresh();
      }
    });
  });
})();
