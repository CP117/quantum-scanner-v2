/*
 * Prediction Tracker page logic.
 *
 * Lists every user-saved 10-day forecast from the SQLite tracker DB,
 * supports sortable table headers, market+status filters, auto-refresh,
 * and renders two charts:
 *
 *   1. Scatter: predicted vs actual price for every evaluated row
 *      (clusters near the diagonal indicate accuracy).
 *   2. Bar:     directional accuracy % by predicted direction
 *      (bull / bear / neutral) for at-a-glance comparison.
 *
 * No frameworks, no external deps. Charts use native <canvas>.
 */
(function () {
  const apiBase = window.location.origin;
  const state = {
    rows: [],
    accuracy: null,
    sortKey: 'created_at',
    sortDir: 'desc',  // 'asc' | 'desc'
    market: '',
    status: '',
    source: '',
  };

  const COLUMNS = [
    { key: 'symbol',         label: 'Symbol',     sortable: true,  align: 'left'  },
    { key: 'market',         label: 'Mkt',        sortable: true,  align: 'left'  },
    { key: 'source',         label: 'Source',     sortable: true,  align: 'left'  },
    { key: 'direction',      label: 'Dir',        sortable: true,  align: 'left'  },
    { key: 'anchor_price',   label: 'Anchor',     sortable: true,  align: 'right' },
    { key: 'target_price',   label: 'Target',     sortable: true,  align: 'right' },
    { key: 'actual_close',   label: 'Actual',     sortable: true,  align: 'right' },
    { key: 'error_pct',      label: 'Err %',      sortable: true,  align: 'right' },
    { key: 'directional_hit',label: 'Dir hit',    sortable: true,  align: 'left'  },
    { key: 'magnitude_hit',  label: 'Mag hit',    sortable: true,  align: 'left'  },
    { key: 'confidence_pct', label: 'Conf %',     sortable: true,  align: 'right' },
    { key: 'created_at',     label: 'Saved',      sortable: true,  align: 'left'  },
    { key: 'expires_at',     label: 'Expires',    sortable: true,  align: 'left'  },
    { key: 'status',         label: 'Status',     sortable: true,  align: 'left'  },
    { key: 'notes',          label: 'Notes',      sortable: false, align: 'left'  },
    { key: '_actions',       label: '',           sortable: false, align: 'right' },
  ];

  function $(id) { return document.getElementById(id); }
  function fmtNum(v, digits = 2) {
    if (v === null || v === undefined || isNaN(v)) return '\u2014';
    return Number(v).toFixed(digits);
  }
  function fmtDate(s) {
    if (!s) return '\u2014';
    try {
      const d = new Date(s);
      return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
        + ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    } catch (e) { return s; }
  }
  function fmtDateShort(s) {
    if (!s) return '\u2014';
    try { return new Date(s).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }); }
    catch (e) { return s; }
  }
  function dirClass(d) {
    return d === 'bull' ? 'dir-bull' : d === 'bear' ? 'dir-bear' : 'dir-neutral';
  }

  async function fetchJson(path) {
    const res = await fetch(`${apiBase}${path}`);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  }

  async function refreshAll() {
    try {
      const qs = new URLSearchParams();
      if (state.market) qs.set('market', state.market);
      if (state.status) qs.set('status', state.status);
      if (state.source) qs.set('source', state.source);
      qs.set('limit', '1000');
      const list = await fetchJson(`/api/predictions/list?${qs.toString()}`);
      state.rows = list.rows || [];
      const accQs = new URLSearchParams();
      if (state.market) accQs.set('market', state.market);
      if (state.source) accQs.set('source', state.source);
      state.accuracy = await fetchJson(`/api/predictions/accuracy?${accQs.toString()}`);
      // Independent of whatever source filter is selected above, always
      // fetch the unfiltered breakdown so the honest auto_scan number is
      // never hidden just because someone left the filter on "All" or
      // "My saved picks". Skipped when the filter IS auto_scan/user-only
      // since state.accuracy already has by_source in that case only
      // when source is omitted -- so we fetch it separately here to
      // guarantee it's always present regardless of the filter state.
      const compareQs = state.market ? `?market=${state.market}` : '';
      state.accuracyUnfiltered = await fetchJson(`/api/predictions/accuracy${compareQs}`);
    } catch (e) {
      console.error('tracker refresh failed:', e);
      state.rows = [];
      state.accuracy = null;
      state.accuracyUnfiltered = null;
    }
    renderSourceCompare();
    renderStats();
    renderTable();
    renderScatter();
    renderBar();
  }

  function renderSourceCompare() {
    const el = $('sourceCompare');
    if (!el) return;
    const bySource = (state.accuracyUnfiltered && state.accuracyUnfiltered.by_source) || {};
    const auto = bySource.auto_scan || {};
    const user = bySource.user || {};
    const fmtPct = (v) => (v != null ? `${v}%` : '\u2014 (not enough evaluated yet)');
    el.innerHTML = `
      <div class="source-card unbiased" data-testid="source-card-auto">
        <h4>\u2713 Scanner accuracy (auto-logged, unbiased)</h4>
        <div class="big">${fmtPct(auto.directional_accuracy_pct)}</div>
        <div class="note">${auto.evaluated ?? 0} evaluated of ${auto.total_saved ?? 0} logged \u00b7
          every symbol the scanner scores gets logged automatically -- no cherry-picking.</div>
      </div>
      <div class="source-card biased" data-testid="source-card-user">
        <h4>My saved picks (manual, selection-biased)</h4>
        <div class="big">${fmtPct(user.directional_accuracy_pct)}</div>
        <div class="note">${user.evaluated ?? 0} evaluated of ${user.total_saved ?? 0} saved \u00b7
          reflects only what a human chose to save, which tends to skew toward
          picks that already looked promising -- not a fair accuracy measure.</div>
      </div>
    `;
  }

  function renderStats() {
    const g = $('statGrid');
    if (!g) return;
    const a = state.accuracy || {};
    const items = [
      { label: 'Total saved',       value: a.total_saved ?? '\u2014', sub: '',                       cls: 'blue'  },
      { label: 'Open',              value: a.open ?? '\u2014',        sub: 'awaiting expiration',    cls: 'amber' },
      { label: 'Evaluated',         value: a.evaluated ?? '\u2014',   sub: 'auto-scored',            cls: 'green' },
      { label: 'Unresolved',        value: a.unresolved ?? '\u2014',  sub: 'history void',           cls: ''      },
      { label: 'Directional accuracy',
        value: a.directional_accuracy_pct != null ? `${a.directional_accuracy_pct}%` : '\u2014',
        sub: 'sign matched',          cls: 'green' },
      { label: 'Magnitude accuracy',
        value: a.magnitude_accuracy_pct != null ? `${a.magnitude_accuracy_pct}%` : '\u2014',
        sub: 'inside 95% band',       cls: 'blue'  },
      { label: 'Mean abs error',
        value: a.mean_abs_error_pct != null ? `${a.mean_abs_error_pct}%` : '\u2014',
        sub: '|actual - target| / anchor', cls: 'amber' },
      { label: 'Median abs error',
        value: a.median_abs_error_pct != null ? `${a.median_abs_error_pct}%` : '\u2014',
        sub: 'robust to outliers',    cls: 'amber' },
    ];
    g.innerHTML = items.map(it => `
      <div class="stat-card ${it.cls}" data-testid="stat-${it.label.toLowerCase().replace(/\s+/g, '-')}">
        <div class="label">${it.label}</div>
        <div class="value">${it.value}</div>
        <div class="sub">${it.sub}</div>
      </div>
    `).join('');
  }

  function renderTable() {
    const head = $('trackerHead');
    const body = $('trackerBody');
    if (!head || !body) return;

    head.innerHTML = '<tr>' + COLUMNS.map(c => {
      const isSorted = c.sortable && c.key === state.sortKey;
      const arrow = isSorted ? (state.sortDir === 'asc' ? '\u2191' : '\u2193') : '';
      const onClick = c.sortable ? `onclick="window.__trackerSort('${c.key}')"` : '';
      const align = c.align === 'right' ? 'style="text-align:right"' : '';
      return `<th ${onClick} ${align} data-testid="th-${c.key}">${c.label}${arrow ? `<span class="sort-arrow">${arrow}</span>` : ''}</th>`;
    }).join('') + '</tr>';

    if (!state.rows.length) {
      body.innerHTML = `<tr><td colspan="${COLUMNS.length}" class="empty" data-testid="empty-row">
        No saved predictions yet. Generate a 10-day forecast on the main
        dashboard, then click <strong>Save prediction</strong> to track it here.
      </td></tr>`;
      return;
    }

    const sorted = [...state.rows].sort((a, b) => {
      const k = state.sortKey;
      const va = a[k], vb = b[k];
      const aN = (va === null || va === undefined) ? -Infinity : (typeof va === 'number' ? va : String(va));
      const bN = (vb === null || vb === undefined) ? -Infinity : (typeof vb === 'number' ? vb : String(vb));
      const cmp = aN < bN ? -1 : aN > bN ? 1 : 0;
      return state.sortDir === 'asc' ? cmp : -cmp;
    });

    body.innerHTML = sorted.map(r => {
      const dh = r.directional_hit;
      const mh = r.magnitude_hit;
      const dhCell = dh === true ? '<span class="hit-yes" data-testid="dir-hit-yes">\u2713 hit</span>'
                  : dh === false ? '<span class="hit-no" data-testid="dir-hit-no">\u2717 miss</span>'
                  : '<span class="hit-na">\u2014</span>';
      const mhCell = mh === true ? '<span class="hit-yes">\u2713 in-band</span>'
                  : mh === false ? '<span class="hit-no">\u2717 out</span>'
                  : '<span class="hit-na">\u2014</span>';
      const errCell = r.error_pct != null
        ? `<span class="${r.error_pct >= 0 ? 'dir-bull' : 'dir-bear'}">${r.error_pct >= 0 ? '+' : ''}${fmtNum(r.error_pct, 2)}%</span>`
        : '\u2014';
      const sourceCell = r.source === 'auto_scan'
        ? '<span class="hit-yes" title="Logged automatically by the scanner for every symbol it scores -- no cherry-picking">auto</span>'
        : '<span class="hit-na" title="Manually saved by a user -- selection-biased, not a fair accuracy read">manual</span>';
      return `<tr data-testid="row-${r.id}">
        <td class="sym" data-testid="row-symbol-${r.id}">${r.symbol}</td>
        <td>${r.market}</td>
        <td>${sourceCell}</td>
        <td class="${dirClass(r.direction)}">${r.direction || '\u2014'}</td>
        <td class="num">$${fmtNum(r.anchor_price, 4)}</td>
        <td class="num">$${fmtNum(r.target_price, 4)}</td>
        <td class="num">${r.actual_close != null ? '$' + fmtNum(r.actual_close, 4) : '\u2014'}</td>
        <td class="num">${errCell}</td>
        <td>${dhCell}</td>
        <td>${mhCell}</td>
        <td class="num">${r.confidence_pct != null ? fmtNum(r.confidence_pct, 1) + '%' : '\u2014'}</td>
        <td>${fmtDate(r.created_at)}</td>
        <td>${fmtDateShort(r.expires_at)}</td>
        <td class="status-${r.status}">${r.status}</td>
        <td>${(r.notes || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').slice(0, 80)}${(r.notes || '').length > 80 ? '\u2026' : ''}</td>
        <td class="row-actions"><button onclick="window.__trackerDelete('${r.id}')" data-testid="delete-${r.id}">Delete</button></td>
      </tr>`;
    }).join('');
  }

  // ---- Charts (vanilla canvas, no dependencies) -----------------------
  function renderScatter() {
    const canvas = $('scatterCanvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    const pts = state.rows
      .filter(r => r.status === 'evaluated' && r.target_price > 0 && r.actual_close > 0)
      .map(r => ({
        x: r.target_price,
        y: r.actual_close,
        d: r.direction,
        sym: r.symbol,
      }));

    if (!pts.length) {
      ctx.fillStyle = '#9ca3af';
      ctx.font = '13px Arial';
      ctx.textAlign = 'center';
      ctx.fillText('No evaluated predictions yet.', w / 2, h / 2);
      ctx.fillText('Predictions auto-evaluate on their expiration date.', w / 2, h / 2 + 18);
      return;
    }

    const padding = { top: 20, right: 20, bottom: 36, left: 56 };
    const plotW = w - padding.left - padding.right;
    const plotH = h - padding.top - padding.bottom;
    const allVals = pts.flatMap(p => [p.x, p.y]);
    const minV = Math.min(...allVals) * 0.95;
    const maxV = Math.max(...allVals) * 1.05;
    const xScale = (v) => padding.left + ((v - minV) / (maxV - minV)) * plotW;
    const yScale = (v) => padding.top + plotH - ((v - minV) / (maxV - minV)) * plotH;

    // Axes
    ctx.strokeStyle = '#374151';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padding.left, padding.top);
    ctx.lineTo(padding.left, padding.top + plotH);
    ctx.lineTo(padding.left + plotW, padding.top + plotH);
    ctx.stroke();

    // Diagonal (perfect prediction line)
    ctx.strokeStyle = '#5eead4';
    ctx.setLineDash([6, 6]);
    ctx.beginPath();
    ctx.moveTo(xScale(minV), yScale(minV));
    ctx.lineTo(xScale(maxV), yScale(maxV));
    ctx.stroke();
    ctx.setLineDash([]);

    // Axis labels
    ctx.fillStyle = '#9ca3af';
    ctx.font = '11px Arial';
    ctx.textAlign = 'center';
    ctx.fillText('Predicted (target) →', padding.left + plotW / 2, h - 8);
    ctx.save();
    ctx.translate(14, padding.top + plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText('← Actual close', 0, 0);
    ctx.restore();

    // Tick labels (4 evenly spaced)
    ctx.textAlign = 'right';
    for (let i = 0; i <= 4; i++) {
      const v = minV + ((maxV - minV) / 4) * i;
      const yT = yScale(v);
      ctx.fillText('$' + v.toFixed(2), padding.left - 6, yT + 3);
      ctx.beginPath();
      ctx.moveTo(padding.left - 3, yT);
      ctx.lineTo(padding.left, yT);
      ctx.strokeStyle = '#374151';
      ctx.stroke();
    }
    ctx.textAlign = 'center';
    for (let i = 0; i <= 4; i++) {
      const v = minV + ((maxV - minV) / 4) * i;
      const xT = xScale(v);
      ctx.fillText('$' + v.toFixed(2), xT, padding.top + plotH + 16);
      ctx.beginPath();
      ctx.moveTo(xT, padding.top + plotH);
      ctx.lineTo(xT, padding.top + plotH + 3);
      ctx.stroke();
    }

    // Points
    pts.forEach(p => {
      ctx.fillStyle = p.d === 'bull' ? 'rgba(52,211,153,.85)'
                    : p.d === 'bear' ? 'rgba(248,113,113,.85)'
                    : 'rgba(148,163,184,.85)';
      ctx.beginPath();
      ctx.arc(xScale(p.x), yScale(p.y), 4, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  function renderBar() {
    const canvas = $('barCanvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    const bd = (state.accuracy && state.accuracy.by_direction) || {};
    const groups = [
      { key: 'bull',    label: 'Bull',    fill: '#34d399' },
      { key: 'bear',    label: 'Bear',    fill: '#f87171' },
      { key: 'neutral', label: 'Neutral', fill: '#94a3b8' },
    ];

    const totalEvals = groups.reduce((acc, g) => acc + ((bd[g.key] || {}).total || 0), 0);
    if (totalEvals === 0) {
      ctx.fillStyle = '#9ca3af';
      ctx.font = '13px Arial';
      ctx.textAlign = 'center';
      ctx.fillText('Accuracy chart will appear once', w / 2, h / 2 - 8);
      ctx.fillText('predictions have been auto-evaluated.', w / 2, h / 2 + 12);
      return;
    }

    const padding = { top: 20, right: 14, bottom: 36, left: 44 };
    const plotW = w - padding.left - padding.right;
    const plotH = h - padding.top - padding.bottom;
    const barW = plotW / groups.length * 0.45;
    const groupW = plotW / groups.length;

    // Y axis (0-100%)
    ctx.strokeStyle = '#374151';
    ctx.beginPath();
    ctx.moveTo(padding.left, padding.top);
    ctx.lineTo(padding.left, padding.top + plotH);
    ctx.lineTo(padding.left + plotW, padding.top + plotH);
    ctx.stroke();
    ctx.fillStyle = '#9ca3af';
    ctx.font = '11px Arial';
    ctx.textAlign = 'right';
    [0, 25, 50, 75, 100].forEach(pct => {
      const y = padding.top + plotH - (pct / 100) * plotH;
      ctx.fillText(pct + '%', padding.left - 6, y + 3);
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(padding.left + plotW, y);
      ctx.strokeStyle = 'rgba(55,65,81,.5)';
      ctx.setLineDash([3, 4]);
      ctx.stroke();
      ctx.setLineDash([]);
    });

    // Bars: directional accuracy + magnitude accuracy side-by-side per group.
    groups.forEach((g, i) => {
      const entry = bd[g.key] || {};
      const dirAcc = entry.directional_accuracy_pct;
      const magAcc = entry.magnitude_accuracy_pct;
      const cx = padding.left + groupW * i + groupW / 2;

      // directional accuracy bar
      const xDir = cx - barW * 0.55;
      const hDir = (dirAcc != null ? dirAcc : 0) / 100 * plotH;
      ctx.fillStyle = g.fill;
      ctx.fillRect(xDir, padding.top + plotH - hDir, barW * 0.5, hDir);

      // magnitude accuracy bar (lighter shade)
      const xMag = cx + barW * 0.05;
      const hMag = (magAcc != null ? magAcc : 0) / 100 * plotH;
      ctx.fillStyle = g.fill + 'aa';
      ctx.fillRect(xMag, padding.top + plotH - hMag, barW * 0.5, hMag);

      // values above bars
      ctx.fillStyle = '#e5e7eb';
      ctx.font = '11px Arial';
      ctx.textAlign = 'center';
      if (dirAcc != null) ctx.fillText(dirAcc + '%', xDir + barW * 0.25, padding.top + plotH - hDir - 4);
      if (magAcc != null) ctx.fillText(magAcc + '%', xMag + barW * 0.25, padding.top + plotH - hMag - 4);

      // group label
      ctx.fillStyle = '#cbd5e1';
      ctx.font = '12px Arial';
      ctx.fillText(`${g.label} (${entry.total || 0})`, cx, padding.top + plotH + 16);
    });

    // Legend
    ctx.fillStyle = '#9ca3af';
    ctx.textAlign = 'left';
    ctx.font = '11px Arial';
    ctx.fillText('\u25A0 directional   \u25A0 magnitude (in-band)', padding.left, padding.top - 6);
  }

  // ---- Public handlers used by inline onclick --------------------------
  window.__trackerSort = function (key) {
    if (state.sortKey === key) {
      state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      state.sortKey = key;
      state.sortDir = 'desc';
    }
    renderTable();
  };

  window.__trackerDelete = async function (id) {
    if (!confirm('Delete this saved prediction? This cannot be undone.')) return;
    try {
      const res = await fetch(`${apiBase}/api/predictions/${encodeURIComponent(id)}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(`status ${res.status}`);
      await refreshAll();
    } catch (e) {
      alert('Delete failed: ' + (e.message || e));
    }
  };

  // ---- Wire filters / buttons ------------------------------------------
  document.addEventListener('DOMContentLoaded', () => {
    $('filterMarket').addEventListener('change', (e) => { state.market = e.target.value; refreshAll(); });
    $('filterStatus').addEventListener('change', (e) => { state.status = e.target.value; refreshAll(); });
    $('filterSource').addEventListener('change', (e) => { state.source = e.target.value; refreshAll(); });
    $('refreshBtn').addEventListener('click', refreshAll);
    $('evaluateNowBtn').addEventListener('click', async () => {
      const btn = $('evaluateNowBtn');
      const status = $('evalStatus');
      btn.disabled = true;
      status.textContent = 'Evaluating expired predictions\u2026';
      try {
        const res = await fetch(`${apiBase}/api/predictions/evaluate-now`, { method: 'POST' });
        const data = await res.json();
        status.textContent = `Evaluated ${data.evaluated || 0} \u00b7 unresolved ${data.unresolved || 0} \u00b7 still open ${data.still_open || 0}`;
      } catch (e) {
        status.textContent = 'Eval failed: ' + (e.message || e);
      } finally {
        btn.disabled = false;
        await refreshAll();
      }
    });

    refreshAll();
    // Light auto-refresh — every 60s the table picks up any newly auto-evaluated rows.
    setInterval(refreshAll, 60_000);
    // Redraw charts on resize.
    window.addEventListener('resize', () => { renderScatter(); renderBar(); });
  });
})();
