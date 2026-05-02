DASHBOARD_JS = r"""
  const STREAM_URL        = '__STREAM_URL__';
  const SHOW_ARGS         = __SHOW_ARGS__;
  const TASKS_PREFIX      = '__TASKS_PREFIX__';
  const REGISTERED_TASKS  = __REGISTERED_TASKS__;
  const SHOW_AUDIT        = __SHOW_AUDIT__;

  // ── State ──────────────────────────────────────────────────────
  const PAGE_SIZE = 30;
  let state = { tasks: [], metrics: {}, schedules: [] };
  let sortCol      = localStorage.getItem('tf-sort-col') || 'created_at';
  let sortDir      = localStorage.getItem('tf-sort-dir') || 'desc';
  let statusFilter = localStorage.getItem('tf-status')  || 'all';
  let funcFilter   = localStorage.getItem('tf-func')    || 'all';
  let searchQuery  = localStorage.getItem('tf-search')  || '';
  let timeFilterVal  = localStorage.getItem('tf-time-val')  || '';
  let timeFilterUnit = localStorage.getItem('tf-time-unit') || 'hour';

  let selectedId   = null;
  let currentPage  = 0;
  let paused       = localStorage.getItem('tf-paused') === '1';
  let pendingState = null;
  let pendingNewTasks = 0;
  let selectedIds  = new Set();

  // ── SSE ────────────────────────────────────────────────────────
  function connect() {
    const src = new EventSource(STREAM_URL);
    src.addEventListener('state', function(e) {
      var incoming = JSON.parse(e.data);
      if (paused) {
        var prevCount = pendingState ? pendingState.tasks.length : state.tasks.length;
        var newCount  = incoming.tasks.length - prevCount;
        if (newCount > 0) pendingNewTasks += newCount;
        pendingState = incoming;
        var btn = document.getElementById('pause-btn');
        if (btn) btn.innerHTML =
          '<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><polygon points="5,3 19,12 5,21"/></svg>'
          + ' Resume' + (pendingNewTasks > 0 ? ' <span class="new-badge">+' + pendingNewTasks + '</span>' : '');
        return;
      }
      applyState(incoming);
    });
    src.onopen  = () => setStatus('live');
    src.onerror = () => { setStatus('error'); src.close(); setTimeout(connect, 3000); };
  }

  function applyState(newState) {
    state = newState;
    renderMetrics();
    populateFuncFilter();
    renderTable();
    if (selectedId) {
      const t = state.tasks.find(function(t) { return t.task_id === selectedId; });
      if (t) renderDetail(t); else closeDetail();
    }
    renderSchedules();
    renderDeadLetters();
    var vcnt = document.getElementById('tab-view-count');
    if (vcnt) vcnt.textContent = state.tasks.length > 0 ? state.tasks.length : '';
  }

  function showTab(name) {
    ['view', 'deadletters', 'audit', 'schedules', 'tasks'].forEach(function(t) {
      document.getElementById('panel-' + t).style.display = name === t ? '' : 'none';
      var tabEl = document.getElementById('tab-' + t);
      if (tabEl) tabEl.classList.toggle('tab-btn--active', name === t);
    });
    if (name === 'audit') fetchAudit();
  }

  function renderSchedules() {
    var tbody = document.getElementById('schedules-tbody');
    if (!tbody) return;
    var entries = state.schedules || [];
    var scnt = document.getElementById('tab-schedules-count');
    if (scnt) scnt.textContent = entries.length > 0 ? entries.length : '';
    if (entries.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:#9ca3af;padding:40px;font-size:0.9rem">No scheduled tasks registered.</td></tr>';
      return;
    }
    tbody.innerHTML = entries.map(function(e) {
      var statusHtml = e.last_status
        ? '<span class="badge badge--' + e.last_status + '">' + esc(e.last_status) + '</span>'
        : '<span class="muted">—</span>';
      var nextRun = e.next_run ? fmtDate(e.next_run) : '—';
      return '<tr>'
        + '<td class="td td--func">' + esc(e.func_name) + '</td>'
        + '<td class="td"><code>' + esc(e.trigger) + '</code></td>'
        + '<td class="td">' + nextRun + '</td>'
        + '<td class="td">' + statusHtml + '</td>'
        + '</tr>';
    }).join('');
  }

  function syncPauseBtn() {
    var btn = document.getElementById('pause-btn');
    if (!btn) return;
    if (paused) {
      btn.classList.add('pause-btn--paused');
      btn.title = 'Resume live updates';
      btn.innerHTML =
        '<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><polygon points="5,3 19,12 5,21"/></svg>'
        + ' Resume' + (pendingNewTasks > 0 ? ' <span class="new-badge">+' + pendingNewTasks + '</span>' : '');
    } else {
      btn.classList.remove('pause-btn--paused');
      btn.title = 'Pause live updates';
      btn.innerHTML =
        '<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>'
        + ' Pause';
    }
  }

  function togglePause() {
    paused = !paused;
    localStorage.setItem('tf-paused', paused ? '1' : '0');
    if (paused) {
      pendingNewTasks = 0;
      pendingState    = null;
    } else {
      if (pendingState) {
        applyState(pendingState);
        pendingState    = null;
        pendingNewTasks = 0;
      }
    }
    syncPauseBtn();
  }

  function setStatus(s) {
    const dot = document.getElementById('live-dot');
    const lbl = document.getElementById('live-label');
    dot.className = 'dot dot--' + s;
    lbl.className = 'status-label status-label--' + s;
    lbl.textContent = s === 'live' ? 'Live' : s === 'error' ? 'Disconnected' : 'Connecting…';
  }

  // ── Metrics ────────────────────────────────────────────────────
  function renderMetrics() {
    const m = state.metrics;
    const map = {
      total:        m.total       ?? 0,
      pending:      m.pending     ?? 0,
      running:      m.running     ?? 0,
      success:      m.success     ?? 0,
      failed:       m.failed      ?? 0,
      interrupted:  m.interrupted ?? 0,
      cancelled:    m.cancelled   ?? 0,
      rate:    m.success_rate    != null ? m.success_rate    + '%'  : '—',
      avg:     m.avg_duration_ms != null ? fmtDur(m.avg_duration_ms) : '—',
    };
    for (const [k, v] of Object.entries(map)) {
      const el = document.getElementById('metric-' + k);
      if (el) el.textContent = v;
    }
  }

  // ── Filter + Sort ──────────────────────────────────────────────
  function timeFilterCutoff() {
    var v = parseInt(timeFilterVal, 10);
    if (!v || v <= 0) return null;
    var msMap = { min: 60000, hour: 3600000, day: 86400000 };
    var ms = msMap[timeFilterUnit] || 3600000;
    return Date.now() - (v * ms);
  }

  function filteredSorted() {
    var cutoff = timeFilterCutoff();
    var q = searchQuery.trim().toLowerCase();
    return state.tasks
      .filter(function(t) {
        if (statusFilter !== 'all' && t.status !== statusFilter) return false;
        if (funcFilter   !== 'all' && t.func_name !== funcFilter) return false;
        if (cutoff !== null) {
          var ts = t.created_at ? new Date(t.created_at).getTime() : 0;
          if (ts < cutoff) return false;
        }
        if (q) {
          if (!t.task_id.toLowerCase().includes(q) && !t.func_name.toLowerCase().includes(q)) return false;
        }
        return true;
      })
      .sort((a, b) => {
        let va = a[sortCol] ?? '', vb = b[sortCol] ?? '';
        if (typeof va === 'string') { va = va.toLowerCase(); vb = String(vb).toLowerCase(); }
        if (va < vb) return sortDir === 'asc' ? -1 : 1;
        if (va > vb) return sortDir === 'asc' ?  1 : -1;
        return 0;
      });
  }

  // ── Bulk selection helpers ─────────────────────────────────────
  function isRetryable(t) { return t.status === 'failed' || t.status === 'interrupted'; }

  function updateBulkBar() {
    var bar = document.getElementById('bulk-bar');
    var lbl = document.getElementById('bulk-label');
    var n   = selectedIds.size;
    if (n > 0) {
      bar.classList.add('bulk-bar--on');
      lbl.textContent = n + ' task' + (n !== 1 ? 's' : '') + ' selected';
    } else {
      bar.classList.remove('bulk-bar--on');
    }
    // Update select-all checkbox state
    var allCheck = document.getElementById('select-all-check');
    if (allCheck) {
      var pageRetryable = Array.from(document.querySelectorAll('.row-check:not(#select-all-check):not(#dlq-select-all):not(.dlq-row-check)')).filter(function(c) { return !c.disabled; });
      allCheck.checked       = pageRetryable.length > 0 && pageRetryable.every(function(c) { return c.checked; });
      allCheck.indeterminate = !allCheck.checked && pageRetryable.some(function(c) { return c.checked; });
    }
  }

  function toggleSelect(checkbox, taskId) {
    if (checkbox.checked) { selectedIds.add(taskId); } else { selectedIds.delete(taskId); }
    updateBulkBar();
  }

  function toggleSelectAll(masterCb) {
    document.querySelectorAll('.row-check:not(#select-all-check):not(#dlq-select-all):not(.dlq-row-check)').forEach(function(cb) {
      if (!cb.disabled) {
        cb.checked = masterCb.checked;
        var id = cb.dataset.id;
        if (!id) return;
        if (masterCb.checked) { selectedIds.add(id); } else { selectedIds.delete(id); }
      }
    });
    updateBulkBar();
  }

  function clearSelection() {
    selectedIds.clear();
    document.querySelectorAll('.row-check:not(#select-all-check):not(#dlq-select-all):not(.dlq-row-check)').forEach(function(cb) { cb.checked = false; });
    var allCheck = document.getElementById('select-all-check');
    if (allCheck) { allCheck.checked = false; allCheck.indeterminate = false; }
    updateBulkBar();
  }

  function bulkRetry() {
    var ids = Array.from(selectedIds).filter(Boolean);
    if (!ids.length) return;
    var btn = document.getElementById('bulk-retry-btn');
    btn.disabled = true;
    btn.textContent = 'Retrying ' + ids.length + '...';
    var done = 0, failed = 0;
    ids.forEach(function(taskId) {
      fetch(TASKS_PREFIX + '/' + taskId + '/retry', { method: 'POST' })
        .then(function(r) { if (!r.ok) throw new Error(); })
        .catch(function() { failed++; })
        .finally(function() {
          done++;
          if (done === ids.length) {
            btn.disabled = false;
            btn.textContent = 'Retry selected';
            clearSelection();
            if (failed) showToast(failed + ' retries failed');
          }
        });
    });
  }

  // ── Time filter popup ──────────────────────────────────────────
  function openTimeFilterPopup() {
    document.getElementById('tf-popup-val').value = timeFilterVal;
    document.getElementById('tf-popup-unit').value = timeFilterUnit;
    document.getElementById('time-filter-popup').classList.add('popup-backdrop--open');
    document.getElementById('tf-popup-val').focus();
  }
  function closeTimeFilterPopup(e) {
    if (!e || e.target === document.getElementById('time-filter-popup')) {
      document.getElementById('time-filter-popup').classList.remove('popup-backdrop--open');
    }
  }
  function applyTimeFilter() {
    var val = document.getElementById('tf-popup-val').value;
    var unit = document.getElementById('tf-popup-unit').value;
    timeFilterVal = val; timeFilterUnit = unit; currentPage = 0;
    localStorage.setItem('tf-time-val', val);
    localStorage.setItem('tf-time-unit', unit);
    var btn = document.getElementById('time-filter-btn');
    var label = document.getElementById('time-filter-label');
    if (val && parseInt(val, 10) > 0) {
      var unitLabels = { min: 'min', hour: 'hr', day: 'd' };
      label.textContent = 'Last ' + val + unitLabels[unit];
      btn.classList.add('filter-trigger-btn--active');
    } else {
      label.textContent = 'All time';
      btn.classList.remove('filter-trigger-btn--active');
    }
    document.getElementById('time-filter-popup').classList.remove('popup-backdrop--open');
    renderTable();
  }
  function clearTimeFilter() {
    document.getElementById('tf-popup-val').value = '';
    applyTimeFilter();
  }

  // ── Clear history popup ────────────────────────────────────────
  function openClearHistoryPopup() {
    document.getElementById('ch-popup-val').value = '';
    document.getElementById('clear-history-popup').classList.add('popup-backdrop--open');
    document.getElementById('ch-popup-val').focus();
  }
  function closeClearHistoryPopup(e) {
    if (!e || e.target === document.getElementById('clear-history-popup')) {
      document.getElementById('clear-history-popup').classList.remove('popup-backdrop--open');
    }
  }
  function confirmClearHistory() {
    var val = parseInt(document.getElementById('ch-popup-val').value, 10);
    var unit = document.getElementById('ch-popup-unit').value;
    if (!val || val <= 0) { showToast('Enter a number first'); return; }
    document.getElementById('clear-history-popup').classList.remove('popup-backdrop--open');
    fetch(TASKS_PREFIX + '/history?value=' + val + '&unit=' + unit, { method: 'DELETE' })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        showToast('Deleted ' + data.deleted + ' task' + (data.deleted !== 1 ? 's' : ''));
      })
      .catch(function() { showToast('Failed to clear history'); });
  }

  // ── Table ──────────────────────────────────────────────────────
  function renderTable() {
    var all   = filteredSorted();
    var total = all.length;

    // Clamp page
    var totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    if (currentPage >= totalPages) currentPage = totalPages - 1;

    var tasks = all.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);

    var tbody = document.getElementById('tasks-tbody');
    if (!tasks.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="empty">No tasks match the current filters.</td></tr>';
      renderPagination(total);
      updateBulkBar();
      return;
    }

    tbody.innerHTML = tasks.map(function(t) {
      var dur  = t.duration != null ? fmtDur(t.duration * 1000) : '—';
      var date = t.created_at ? fmtDate(t.created_at) : '—';
      var err  = t.error
        ? '<span class="err-text" title="' + esc(t.error) + '">' + esc(t.error.slice(0,48)) + (t.error.length > 48 ? '…' : '') + '</span>'
        : '<span class="muted">—</span>';
      var sel = t.task_id === selectedId ? ' row--selected' : '';
      var retryable = isRetryable(t);
      var checked   = selectedIds.has(t.task_id) ? ' checked' : '';
      var disabled  = retryable ? '' : ' disabled';
      var checkCell = '<td class="td td--check" onclick="event.stopPropagation()">'
        + '<input type="checkbox" class="row-check" data-id="' + esc(t.task_id) + '"'
        + checked + disabled + ' onchange="toggleSelect(this, this.dataset.id)">'
        + '</td>';
      return '<tr class="row' + sel + '" onclick="openDetail(this.dataset.id)" data-id="' + esc(t.task_id) + '">'
        + checkCell
        + '<td class="td td--mono" style="white-space:nowrap">' + esc(t.task_id.slice(0,8)) + '…'
        + '<button class="copy-btn copy-btn--row" data-val="' + esc(t.task_id) + '" onclick="event.stopPropagation();copyId(this)" title="Copy full ID"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>'
        + '</td>'
        + '<td class="td td--func">' + esc(t.func_name) + (t.source === 'scheduled' ? ' <span class="source-badge">scheduled</span>' : '') + '</td>'
        + '<td class="td">'           + badge(t.status) + '</td>'
        + '<td class="td">'     + dur + '</td>'
        + '<td class="td">'     + t.retries_used + '</td>'
        + '<td class="td td--date">'  + date + '</td>'
        + '<td class="td">'     + (t.priority != null ? priorityBadge(t.priority) : '<span class="muted">—</span>') + '</td>'
        + '<td class="td td--err">'   + err + '</td>'
        + '</tr>';
    }).join('');

    renderPagination(total);
    updateBulkBar();
  }

  // ── Pagination ────────────────────────────────────────────────
  function renderPagination(total) {
    const el = document.getElementById('pagination');
    if (!el) return;
    const totalPages = Math.ceil(total / PAGE_SIZE);
    if (totalPages <= 1) { el.innerHTML = ''; return; }
    const start = currentPage * PAGE_SIZE + 1;
    const end   = Math.min((currentPage + 1) * PAGE_SIZE, total);
    el.innerHTML =
      '<div class="pagination">'
      + '<button class="pg-btn" onclick="goPage(' + (currentPage - 1) + ')"'
      +   (currentPage === 0 ? ' disabled' : '') + '>← Prev</button>'
      + '<span class="pg-info">' + start + '–' + end + ' of ' + total + '</span>'
      + '<button class="pg-btn" onclick="goPage(' + (currentPage + 1) + ')"'
      +   (currentPage >= totalPages - 1 ? ' disabled' : '') + '>Next →</button>'
      + '</div>';
  }

  function goPage(p) { currentPage = p; renderTable(); }

  function badge(status) {
    var cls = { pending:'badge--pending', running:'badge--running', success:'badge--success', failed:'badge--failed', interrupted:'badge--interrupted', cancelled:'badge--cancelled' };
    return '<span class="badge ' + (cls[status] || 'badge--pending') + '">' + esc(status) + '</span>';
  }

  function priorityBadge(p) {
    // Color scale: high priority (>=8) teal, medium (5-7) amber, low (<=4) gray.
    var bg = p >= 8 ? 'rgba(0,150,136,.12)' : p >= 5 ? 'rgba(245,158,11,.12)' : 'rgba(107,114,128,.1)';
    var fg = p >= 8 ? '#009688' : p >= 5 ? '#b45309' : '#6b7280';
    return '<span style="background:' + bg + ';color:' + fg + ';padding:1px 7px;border-radius:4px;font-size:11px;font-weight:600;font-variant-numeric:tabular-nums">' + p + '</span>';
  }

  function executorBadge(e) {
    var map = { async: ['rgba(99,102,241,.12)', '#6366f1'], thread: ['rgba(245,158,11,.12)', '#b45309'], process: ['rgba(16,185,129,.12)', '#059669'] };
    var c = map[e] || ['rgba(107,114,128,.1)', '#6b7280'];
    return '<span style="background:' + c[0] + ';color:' + c[1] + ';padding:1px 7px;border-radius:4px;font-size:11px;font-weight:600">' + esc(e) + '</span>';
  }

  function showToast(msg) {
    var t = document.getElementById('toast');
    t.textContent = msg;
    t.style.opacity = '1';
    t.style.transform = 'translateY(0)';
    setTimeout(function() { t.style.opacity = '0'; t.style.transform = 'translateY(8px)'; }, 3000);
  }

  // ── Sort ───────────────────────────────────────────────────────
  function setSort(col) {
    if (sortCol === col) {
      sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      sortCol = col;
      sortDir = (col === 'created_at' || col === 'duration' || col === 'priority') ? 'desc' : 'asc';
    }
    localStorage.setItem('tf-sort-col', sortCol);
    localStorage.setItem('tf-sort-dir', sortDir);
    document.querySelectorAll('[data-sort]').forEach(function(th) {
      var c = th.dataset.sort;
      th.querySelector('.sort-icon').textContent = c !== sortCol ? '⇕' : sortDir === 'asc' ? '↑' : '↓';
      th.classList.toggle('th--active', c === sortCol);
    });
    currentPage = 0;
    renderTable();
  }

  // ── Function filter ────────────────────────────────────────────
  function populateFuncFilter() {
    const sel  = document.getElementById('func-filter');
    const prev = sel.value;
    const fns  = [...new Set(state.tasks.map(t => t.func_name))].sort();
    sel.innerHTML = '<option value="all">All functions</option>'
      + fns.map(f => '<option value="' + esc(f) + '"' + (f === prev ? ' selected' : '') + '>' + esc(f) + '</option>').join('');
  }

  // ── Detail Panel ───────────────────────────────────────────────
  function openDetail(taskId) {
    selectedId = taskId;
    const task = state.tasks.find(t => t.task_id === taskId);
    if (!task) return;
    renderDetail(task);
    document.getElementById('detail-panel').classList.add('panel--open');
    document.getElementById('detail-backdrop').classList.add('backdrop--on');
    document.body.style.overflow = 'hidden';
    document.querySelectorAll('.row').forEach(r => r.classList.toggle('row--selected', r.dataset.id === taskId));
  }

  function closeDetail() {
    selectedId = null;
    document.getElementById('detail-panel').classList.remove('panel--open');
    document.getElementById('detail-backdrop').classList.remove('backdrop--on');
    document.getElementById('detail-tabs').innerHTML = '';
    document.body.style.overflow = '';
    document.querySelectorAll('.row').forEach(r => r.classList.remove('row--selected'));
  }

  function renderDetail(task) {
    const ft = state.tasks.filter(t => t.func_name === task.func_name);
    const fTotal   = ft.length;
    const fSuccess = ft.filter(t => t.status === 'success').length;
    const fFailed  = ft.filter(t => t.status === 'failed').length;
    const fRunning = ft.filter(t => t.status === 'running').length;
    const fPending = ft.filter(t => t.status === 'pending').length;
    const fDurs    = ft.filter(t => t.duration != null).map(t => t.duration * 1000);
    const fAvg  = fDurs.length ? fDurs.reduce((a,b)=>a+b,0)/fDurs.length : null;
    const fMin  = fDurs.length ? Math.min(...fDurs) : null;
    const fMax  = fDurs.length ? Math.max(...fDurs) : null;
    const fP95  = fDurs.length >= 2 ? (function(){ var s=[...fDurs].sort(function(a,b){return a-b;}); return s[Math.floor(s.length*0.95)]; })() : null;
    const fRate = fTotal ? (fSuccess / fTotal * 100).toFixed(1) : null;
    const rateCls = fRate == null ? 'neutral' : fRate >= 80 ? 'success' : fRate >= 50 ? 'warning' : 'danger';

    const recent = [...ft].sort((a,b)=>(b.created_at||'').localeCompare(a.created_at||'')).slice(0,5);

    const dur     = task.duration   != null ? fmtDur(task.duration * 1000) : '—';
    const created = task.created_at ? fmtDate(task.created_at) : '—';
    const started = task.start_time ? fmtDate(task.start_time) : '—';
    const ended   = task.end_time   ? fmtDate(task.end_time)   : '—';

    const hasLogs  = task.logs  && task.logs.length;
    const hasError = task.error;

    // ── Build header tabs ────────────────────────────────────────
    const tabsEl = document.getElementById('detail-tabs');
    tabsEl.innerHTML = '';

    function makeTab(label, panelId, extraClass) {
      var btn = document.createElement('button');
      btn.className = 'panel-tab' + (extraClass ? ' ' + extraClass : '');
      btn.dataset.panel = panelId;
      btn.onclick = function() { switchTab(this); };
      btn.textContent = label;
      tabsEl.appendChild(btn);
      return btn;
    }

    makeTab('Details', 'panel-details').classList.add('panel-tab--active');
    if (hasLogs)  makeTab('Logs (' + task.logs.length + ')', 'panel-logs');
    if (hasError) makeTab('Error', 'panel-error', 'panel-tab--error');

    // ── Details panel ────────────────────────────────────────────
    var detailsHtml =
      '<div class="d-section"><div class="d-label">Task ID</div>'
      + '<div style="display:flex;align-items:center;gap:8px;margin-top:4px">'
      + '<div class="d-mono" style="flex:1;word-break:break-all">' + esc(task.task_id) + '</div>'
      + '<button class="copy-btn" onclick="copyId(this)" data-val="' + esc(task.task_id) + '" title="Copy task ID">'
      + '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
      + '<rect x="9" y="9" width="13" height="13" rx="2"/>'
      + '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'
      + '</svg></button>'
      + '</div></div>'

      + '<div class="d-row2">'
      + '<div class="d-section"><div class="d-label">Function</div><div class="d-val d-func">' + esc(task.func_name) + '</div></div>'
      + '<div class="d-section"><div class="d-label">Status</div><div>' + badge(task.status) + '</div></div>'
      + '</div>'

      + ((task.status === 'pending' || task.status === 'running') ?
          '<div class="d-section" style="margin-top:4px">'
          + '<button class="cancel-btn" id="cancel-btn-' + esc(task.task_id) + '" data-task-id="' + esc(task.task_id) + '" onclick="cancelTask(this.dataset.taskId, this)">'
          + '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>'
          + 'Cancel task'
          + '</button>'
          + (task.status === 'running' ?
              '<div style="font-size:11px;color:var(--db-text-muted);margin-top:5px">Async tasks are cancelled immediately. Sync tasks stop waiting but the underlying thread runs to completion.</div>'
            : '')
          + '</div>'
        : '')

      + ((task.status === 'failed' || task.status === 'interrupted') ?
          '<div class="d-section" style="margin-top:4px">'
          + '<button class="retry-btn retry-btn--warn" id="retry-btn-' + esc(task.task_id) + '" data-task-id="' + esc(task.task_id) + '" onclick="retryTask(this.dataset.taskId, this)">'
          + '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-3.5"/></svg>'
          + 'Retry this task'
          + '</button>'
          + (task.status === 'interrupted' ?
              '<div style="font-size:11px;color:var(--db-text-muted);margin-top:5px">This task was mid-execution when the app shut down. Retry only if you are sure the function did not already complete its side effects.</div>'
            : '')
          + '</div>'
        : '')

      + '<div class="d-row3">'
      + dSec('Created', '<span style="font-size:12px">' + created + '</span>')
      + dSec('Started', '<span style="font-size:12px">' + started + '</span>')
      + dSec('Ended',   '<span style="font-size:12px">' + ended   + '</span>')
      + '</div>'

      + '<div class="d-row3">'
      + dSec('Duration',     '<span class="d-val">' + dur + '</span>')
      + dSec('Retries Used', '<span class="d-val">' + task.retries_used + '</span>')
      + dSec('Priority', task.priority != null ? priorityBadge(task.priority) : '<span class="d-val muted">—</span>')
      + '</div>'

      + '<div class="d-row3">'
      + dSec('Executor', task.executor != null ? executorBadge(task.executor) : '<span class="d-val muted">—</span>')
      + dSec('Source', '<span class="d-val">' + esc(task.source || '—') + '</span>')
      + '<div class="d-section"></div>'
      + '</div>'

      + (SHOW_ARGS && ((task.args && task.args.length) || (task.kwargs && Object.keys(task.kwargs).length)) ?
          '<div class="divider"></div>'
          + '<div class="section-title">'
          + '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>'
          + 'Arguments</div>'
          + (task.args && task.args.length ?
              '<div class="d-section"><div class="d-label">Positional args</div>'
              + task.args.map(function(a, i) {
                  return '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:4px">'
                    + '<span class="arg-idx">' + i + '</span>'
                    + '<code class="d-mono arg-code">' + esc(a) + '</code>'
                    + '</div>';
                }).join('')
              + '</div>'
            : '')
          + (task.kwargs && Object.keys(task.kwargs).length ?
              '<div class="d-section"><div class="d-label">Keyword args</div>'
              + Object.entries(task.kwargs).map(function(kv) {
                  return '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:4px">'
                    + '<span class="arg-key">' + esc(kv[0]) + '</span>'
                    + '<span class="arg-eq">=</span>'
                    + '<code class="d-mono arg-code">' + esc(kv[1]) + '</code>'
                    + '</div>';
                }).join('')
              + '</div>'
            : '')
        : '')

      + '<div class="divider"></div>'

      + '<div class="section-title">'
      + '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>'
      + 'Function Analytics · ' + esc(task.func_name) + '</div>'
      + '<div class="analytics-grid" style="margin-bottom:4px">'
      + aCard('Total Runs',    fTotal,                                        'neutral')
      + aCard('Success',       fSuccess,                                      'success')
      + aCard('Failed',        fFailed,  fFailed  > 0 ? 'danger'  : 'neutral')
      + aCard('Running',       fRunning, fRunning > 0 ? 'running' : 'neutral')
      + aCard('Success Rate',  fRate  != null ? fRate  + '%'  : '—', rateCls)
      + aCard('Avg Duration',  fAvg != null ? fmtDur(fAvg) : '—', 'neutral')
      + aCard('Min Duration',  fMin != null ? fmtDur(fMin) : '—', 'neutral')
      + aCard('Max Duration',  fMax != null ? fmtDur(fMax) : '—', 'neutral')
      + aCard('P95 Duration',  fP95 != null ? fmtDur(fP95) : '—', 'neutral')
      + '</div>'

      + '<div class="divider"></div>'

      + '<div class="section-title">'
      + '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>'
      + 'Recent Runs · ' + esc(task.func_name) + '</div>'
      + '<div class="recent-runs">'
      + recent.map(r =>
          '<div class="r-run' + (r.task_id === task.task_id ? ' r-run--cur' : '') + '" onclick="openDetail(this.dataset.id)" data-id="' + r.task_id + '">'
          + '<span class="d-mono" style="font-size:11px">' + esc(r.task_id.slice(0,8)) + '…</span>'
          + badge(r.status)
          + '<span class="r-dur">'
          + (r.duration != null ? fmtDur(r.duration*1000) : '—')
          + '</span></div>'
        ).join('')
      + '</div>';

    // ── Logs panel ───────────────────────────────────────────────
    var logsHtml = hasLogs
      ? '<div class="d-logs">'
        + task.logs.map(function(line) {
            if (line.startsWith('--- ')) return '<div class="log-sep">' + esc(line) + '</div>';
            var ts = line.slice(0, 19), msg = line.slice(20);
            return '<div class="log-line"><span class="log-ts">' + esc(ts) + '</span><span>' + esc(msg) + '</span></div>';
          }).join('')
        + '</div>'
      : '';

    // ── Error panel ──────────────────────────────────────────────
    var errorHtml = hasError
      ? '<div class="d-error-msg">' + esc(task.error) + '</div>'
        + (task.stacktrace ? '<div class="d-stacktrace">' + esc(task.stacktrace) + '</div>' : '')
      : '';

    // ── Render all three panels into detail-content ──────────────
    document.getElementById('detail-content').innerHTML =
        '<div id="panel-details" class="d-tab-panel d-tab-panel--active">' + detailsHtml + '</div>'
      + (hasLogs  ? '<div id="panel-logs"  class="d-tab-panel">' + logsHtml  + '</div>' : '')
      + (hasError ? '<div id="panel-error" class="d-tab-panel">' + errorHtml + '</div>' : '');
  }

  function switchTab(btn) {
    var panelId = btn.dataset.panel;
    document.getElementById('detail-tabs').querySelectorAll('.panel-tab').forEach(function(t) {
      t.classList.remove('panel-tab--active');
    });
    document.getElementById('detail-content').querySelectorAll('.d-tab-panel').forEach(function(p) {
      p.classList.remove('d-tab-panel--active');
    });
    btn.classList.add('panel-tab--active');
    var el = document.getElementById(panelId);
    if (el) el.classList.add('d-tab-panel--active');
  }

  function dSec(label, inner) {
    return '<div class="d-section"><div class="d-label">' + label + '</div>' + inner + '</div>';
  }

  function aCard(label, value, cls) {
    return '<div class="a-card"><div class="a-label">' + label + '</div>'
      + '<div class="a-value a-value--' + cls + '">' + value + '</div></div>';
  }

  // ── Toast ──────────────────────────────────────────────────────
  let _toastTimer = null;
  function showToast(msg) {
    const el = document.getElementById('toast');
    el.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#17c964" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg> ' + msg;
    el.classList.add('toast--on');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function() { el.classList.remove('toast--on'); }, 2200);
  }

  // ── Copy ───────────────────────────────────────────────────────
  function copyId(btn) {
    navigator.clipboard.writeText(btn.dataset.val).then(function() {
      const prev = btn.innerHTML;
      btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
      btn.style.color = '#16a34a';
      btn.style.borderColor = '#bbf7d0';
      setTimeout(function() { btn.innerHTML = prev; btn.style.color = ''; btn.style.borderColor = ''; }, 1500);
      showToast('Task ID copied to clipboard');
    });
  }

  // ── CSV Export ────────────────────────────────────────────────
  function exportCsv() {
    var tasks = filteredSorted();
    var headers = ['ID', 'Function', 'Status', 'Duration (ms)', 'Retries', 'Created', 'Error'];
    function csvCell(v) {
      var s = String(v ?? '');
      if (s.includes(',') || s.includes('"') || s.includes('\n') || s.includes('\r')) {
        return '"' + s.replace(/"/g, '""') + '"';
      }
      return s;
    }
    var rows = [headers].concat(tasks.map(function(t) {
      return [
        t.task_id,
        t.func_name,
        t.status,
        t.duration != null ? (t.duration * 1000).toFixed(0) : '',
        t.retries_used,
        t.created_at || '',
        t.error ? t.error.replace(/[\r\n]+/g, ' ') : '',
      ].map(csvCell);
    }));
    var csv = rows.map(function(r) { return r.join(','); }).join('\r\n');
    var blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'tasks-' + new Date().toISOString().slice(0, 10) + '.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast('Exporting ' + tasks.length + ' task' + (tasks.length !== 1 ? 's' : ''));
  }

  // ── Utilities ──────────────────────────────────────────────────
  function esc(s) {
    return String(s ?? '')
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }

  function fmtDate(iso) {
    try {
      return new Date(iso).toLocaleString(undefined, { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit' });
    } catch(e) { return iso; }
  }

  function fmtDur(ms) {
    if (ms == null) return '—';
    if (ms < 1000)  return ms.toFixed(0) + 'ms';
    if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
    if (ms < 3600000) {
      var m = Math.floor(ms / 60000);
      var s = Math.floor((ms % 60000) / 1000);
      return m + 'm ' + s + 's';
    }
    var h = Math.floor(ms / 3600000);
    var m = Math.floor((ms % 3600000) / 60000);
    return h + 'h ' + m + 'm';
  }

  // ── Event listeners ────────────────────────────────────────────
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      closeTimeFilterPopup();
      closeClearHistoryPopup();
    }
  });
  document.getElementById('tf-popup-val').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') applyTimeFilter();
  });
  document.getElementById('search-input').addEventListener('input', function(e) {
    searchQuery = e.target.value; currentPage = 0;
    localStorage.setItem('tf-search', searchQuery); renderTable();
  });
  document.getElementById('status-filter').addEventListener('change', function(e) {
    statusFilter = e.target.value; currentPage = 0;
    localStorage.setItem('tf-status', statusFilter); renderTable();
  });
  document.getElementById('func-filter').addEventListener('change', function(e) {
    funcFilter = e.target.value; currentPage = 0;
    localStorage.setItem('tf-func', funcFilter); renderTable();
  });
  document.getElementById('detail-close').addEventListener('click', closeDetail);
  document.getElementById('detail-backdrop').addEventListener('click', closeDetail);

  // ── Restore persisted filter UI state ──────────────────────────
  (function() {
    var si = document.getElementById('search-input');
    if (si) si.value = searchQuery;
    var sf = document.getElementById('status-filter');
    if (sf) sf.value = statusFilter;
    // Restore time filter button label
    if (timeFilterVal && parseInt(timeFilterVal, 10) > 0) {
      var unitLabels = { min: 'min', hour: 'hr', day: 'd' };
      var btn = document.getElementById('time-filter-btn');
      var label = document.getElementById('time-filter-label');
      if (label) label.textContent = 'Last ' + timeFilterVal + unitLabels[timeFilterUnit];
      if (btn) btn.classList.add('filter-trigger-btn--active');
    }
    // Show audit tab only when auth is enabled
    if (SHOW_AUDIT) {
      var auditTab = document.getElementById('tab-audit');
      if (auditTab) auditTab.style.display = '';
    }
    // Sort indicators
    document.querySelectorAll('[data-sort]').forEach(function(th) {
      var c = th.dataset.sort;
      var icon = th.querySelector('.sort-icon');
      if (icon) icon.textContent = c !== sortCol ? '⇕' : sortDir === 'asc' ? '↑' : '↓';
      th.classList.toggle('th--active', c === sortCol);
    });
  })();

  // ── Theme ──────────────────────────────────────────────────────
  function toggleTheme() {
    var cur = document.documentElement.getAttribute('data-theme') || 'light';
    var next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('tf-theme', next);
    updateThemeBtn(next);
  }
  function updateThemeBtn(theme) {
    var btn = document.getElementById('theme-btn');
    if (!btn) return;
    btn.title = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
    btn.innerHTML = theme === 'dark'
      ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>'
      : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
  }

  function retryTask(taskId, btn) {
    btn.disabled = true;
    btn.textContent = 'Retrying...';
    fetch(TASKS_PREFIX + '/' + taskId + '/retry', { method: 'POST' })
      .then(function(res) {
        if (!res.ok) {
          return res.json().then(function(body) {
            throw new Error(body.detail || 'Retry failed');
          });
        }
        return res.json();
      })
      .then(function(data) {
        btn.textContent = 'Queued as ' + data.task_id.slice(0, 8) + '...';
        btn.style.borderColor = '#16a34a';
        btn.style.color = '#16a34a';
      })
      .catch(function(err) {
        btn.disabled = false;
        btn.textContent = 'Retry this task';
        alert('Could not retry task: ' + err.message);
      });
  }

  // ── Registered Tasks tab ──────────────────────────────────────
  function renderRegisteredTasks() {
    var container = document.getElementById('registered-tasks-list');
    if (!container) return;
    var tcnt = document.getElementById('tab-tasks-count');
    if (!REGISTERED_TASKS || REGISTERED_TASKS.length === 0) {
      container.innerHTML = '<div class="empty">No tasks registered.</div>';
      return;
    }
    if (tcnt) tcnt.textContent = REGISTERED_TASKS.length;
    var cards = REGISTERED_TASKS.map(function(t) {
      var pills = [];
      pills.push(rtaskPill(t.retries + (t.retries === 1 ? ' retry' : ' retries'), t.retries > 0));
      if (t.delay > 0) pills.push(rtaskPill(t.delay + 's delay', false));
      if (t.backoff !== 1.0 && t.delay > 0) pills.push(rtaskPill(t.backoff + 'x backoff', false));
      if (t.persist) pills.push(rtaskPill('persist', true));
      if (t.requeue_on_interrupt) pills.push(rtaskPill('requeue', true));
      if (t.eager) pills.push(rtaskPill('eager', true));
      if (t.priority != null) pills.push(rtaskPill('priority ' + t.priority, true));
      return '<div class="rtask-card">'
        + '<div class="rtask-name">' + esc(t.name) + '</div>'
        + '<div class="rtask-pills">' + pills.join('') + '</div>'
        + '</div>';
    });
    container.innerHTML = '<div class="rtask-grid">' + cards.join('') + '</div>';
  }

  function rtaskPill(text, accent) {
    return '<span class="rtask-pill' + (accent ? ' rtask-pill--accent' : '') + '">' + esc(text) + '</span>';
  }


  // ── Dead Letters ───────────────────────────────────────────────
  var dlqSelected = new Set();

  function dlqToggleRow(id, cb, evt) {
    evt && evt.stopPropagation();
    if (cb.checked) dlqSelected.add(id);
    else dlqSelected.delete(id);
    dlqUpdateBar();
  }

  function dlqToggleAll(cb) {
    document.querySelectorAll('.dlq-row-check').forEach(function(c) {
      c.checked = cb.checked;
      var id = c.dataset.id;
      if (cb.checked) dlqSelected.add(id);
      else dlqSelected.delete(id);
    });
    dlqUpdateBar();
  }

  function dlqUpdateBar() {
    var bar   = document.getElementById('dlq-bulk-bar');
    var label = document.getElementById('dlq-bulk-label');
    if (!bar) return;
    if (dlqSelected.size > 0) {
      bar.classList.add('bulk-bar--on');
      label.textContent = dlqSelected.size + (dlqSelected.size === 1 ? ' task selected' : ' tasks selected');
    } else {
      bar.classList.remove('bulk-bar--on');
    }
  }

  function dlqClearSelection() {
    dlqSelected.clear();
    document.querySelectorAll('.dlq-row-check').forEach(function(c) { c.checked = false; });
    var all = document.getElementById('dlq-select-all');
    if (all) all.checked = false;
    dlqUpdateBar();
  }

  function dlqReplaySelected() {
    var ids = Array.from(dlqSelected);
    if (!ids.length) return;
    fetch(TASKS_PREFIX + '/bulk-retry', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({task_ids: ids})
    }).then(function(r) {
      return r.json().then(function(data) {
        if (!r.ok) { showToast('Replay failed: ' + (data.detail || 'unknown error')); return; }
        dlqClearSelection();
        showToast('Replayed ' + data.dispatched + ' task(s)' + (data.skipped ? ', ' + data.skipped + ' skipped' : ''));
      });
    }).catch(function(e) { showToast('Network error'); });
  }

  function dlqReplayWindow() {
    var since = document.getElementById('dlq-window-select').value;
    var labels = {'1h':'last 1 hour','6h':'last 6 hours','24h':'last 24 hours','7d':'last 7 days','all':'all time'};
    var desc = document.getElementById('dlq-replay-popup-desc');
    if (desc) desc.textContent = 'Re-enqueue all failed tasks from the ' + (labels[since] || since) + '. Each task will run again with its original arguments.';
    document.getElementById('dlq-replay-popup').classList.add('popup-backdrop--open');
  }

  function closeDlqReplayPopup(e) {
    if (e && e.target !== document.getElementById('dlq-replay-popup')) return;
    document.getElementById('dlq-replay-popup').classList.remove('popup-backdrop--open');
  }

  function confirmDlqReplayWindow() {
    var since = document.getElementById('dlq-window-select').value;
    var btn = document.getElementById('dlq-replay-confirm-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Replaying…'; }
    document.getElementById('dlq-replay-popup').classList.remove('popup-backdrop--open');
    fetch(TASKS_PREFIX + '/retry-failed?since=' + encodeURIComponent(since), {
      method: 'POST'
    }).then(function(r) {
      return r.json().then(function(data) {
        if (btn) { btn.disabled = false; btn.textContent = 'Replay'; }
        if (!r.ok) { showToast('Replay failed: ' + (data.detail || 'unknown error')); return; }
        showToast('Replayed ' + data.dispatched + ' task(s)' + (data.skipped ? ', ' + data.skipped + ' skipped' : ''));
      });
    }).catch(function(e) {
      if (btn) { btn.disabled = false; btn.textContent = 'Replay'; }
      showToast('Network error');
    });
  }

  function renderDeadLetters() {
    var tbody = document.getElementById('deadletters-tbody');
    var cnt   = document.getElementById('tab-deadletters-count');
    if (!tbody) return;
    var failed = state.tasks.filter(function(t) { return t.status === 'failed'; });
    failed.sort(function(a, b) { return (b.created_at || '').localeCompare(a.created_at || ''); });
    if (cnt) cnt.textContent = failed.length > 0 ? failed.length : '';
    dlqSelected.clear();
    dlqUpdateBar();
    if (!failed.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty">No failed tasks.</td></tr>';
      return;
    }
    tbody.innerHTML = failed.map(function(t) {
      var dur  = t.duration != null ? fmtDur(t.duration * 1000) : '—';
      var date = t.created_at ? fmtDate(t.created_at) : '—';
      var err  = t.error
        ? '<span class="err-text" title="' + esc(t.error) + '">' + esc(t.error.slice(0,60)) + (t.error.length > 60 ? '…' : '') + '</span>'
        : '<span class="muted">—</span>';
      var chk  = '<td class="td td--check" onclick="event.stopPropagation()">'
        + '<input type="checkbox" class="row-check dlq-row-check" data-id="' + esc(t.task_id) + '"'
        + ' onclick="dlqToggleRow(\'' + esc(t.task_id) + '\', this, event)">'
        + '</td>';
      return '<tr class="row" onclick="openDetail(this.dataset.id)" data-id="' + esc(t.task_id) + '">'
        + chk
        + '<td class="td td--mono">' + esc(t.task_id.slice(0,8)) + '…</td>'
        + '<td class="td td--func">' + esc(t.func_name) + '</td>'
        + '<td class="td">'           + badge(t.status) + '</td>'
        + '<td class="td">'           + dur + '</td>'
        + '<td class="td td--date">'  + date + '</td>'
        + '<td class="td td--err">'   + err + '</td>'
        + '</tr>';
    }).join('');
  }

  // ── Audit log ─────────────────────────────────────────────────
  function fetchAudit() {
    var tbody = document.getElementById('audit-tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="5" class="audit-empty">Loading…</td></tr>';
    fetch(TASKS_PREFIX + '/audit')
      .then(function(r) { return r.json(); })
      .then(function(entries) {
        var cnt = document.getElementById('tab-audit-count');
        if (cnt) cnt.textContent = entries.length > 0 ? entries.length : '';
        if (!entries.length) {
          tbody.innerHTML = '<tr><td colspan="5" class="audit-empty">No audit entries yet.</td></tr>';
          return;
        }
        tbody.innerHTML = entries.map(function(e) {
          var detail = e.detail && Object.keys(e.detail).length
            ? Object.entries(e.detail).map(function(kv){ return kv[0] + ': ' + String(kv[1]).slice(0,8) + '…'; }).join(', ')
            : '—';
          var actionCls = e.action === 'cancel' ? 'color:#dc2626' : 'color:#7c3aed';
          return '<tr>'
            + '<td class="td" style="white-space:nowrap;font-size:12px">' + esc(fmtDate(e.timestamp)) + '</td>'
            + '<td class="td"><span style="font-weight:600;' + actionCls + '">' + esc(e.action) + '</span></td>'
            + '<td class="td td--mono" style="cursor:pointer" onclick="openDetail(\'' + esc(e.task_id) + '\')">' + esc(e.task_id.slice(0,8)) + '…</td>'
            + '<td class="td">' + esc(e.actor) + '</td>'
            + '<td class="td" style="font-size:12px;color:var(--db-text-muted)">' + esc(detail) + '</td>'
            + '</tr>';
        }).join('');
      })
      .catch(function() {
        tbody.innerHTML = '<tr><td colspan="5" class="audit-empty">Failed to load audit log.</td></tr>';
      });
  }

  // ── Cancel task ────────────────────────────────────────────────
  function cancelTask(taskId, btn) {
    btn.disabled = true;
    btn.textContent = 'Cancelling…';
    fetch(TASKS_PREFIX + '/' + taskId + '/cancel', { method: 'POST' })
      .then(function(res) {
        if (!res.ok) return res.json().then(function(b) { throw new Error(b.detail || 'Cancel failed'); });
        return res.json();
      })
      .then(function() {
        btn.textContent = 'Cancelled';
        btn.style.borderColor = '#be185d';
        btn.style.color = '#be185d';
        showToast('Task cancelled');
      })
      .catch(function(err) {
        btn.disabled = false;
        btn.textContent = 'Cancel task';
        showToast('Could not cancel: ' + err.message);
      });
  }


  // ── Panel resize ───────────────────────────────────────────────
  (function() {
    var panel  = document.getElementById('detail-panel');
    var handle = document.getElementById('panel-resize-handle');
    if (!panel || !handle) return;

    var MIN_W = 320;
    function maxW() { return Math.min(900, Math.round(window.innerWidth * 0.82)); }

    // Restore persisted width
    var saved = parseInt(localStorage.getItem('tf-panel-width'), 10);
    if (saved && saved >= MIN_W && saved <= maxW()) {
      panel.style.width = saved + 'px';
    }

    handle.addEventListener('mousedown', function(e) {
      e.preventDefault();
      handle.classList.add('panel-resize--active');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      // Disable the open/close transition while dragging so movement is instant
      panel.style.transition = 'none';

      function onMove(e) {
        var w = Math.max(MIN_W, Math.min(maxW(), window.innerWidth - e.clientX));
        panel.style.width = w + 'px';
      }

      function onUp() {
        handle.classList.remove('panel-resize--active');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        panel.style.transition = '';
        var w = parseInt(panel.style.width, 10);
        if (w >= MIN_W) localStorage.setItem('tf-panel-width', w);
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      }

      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });

    // Double-click the handle to reset to the default width
    handle.addEventListener('dblclick', function() {
      panel.style.width = '440px';
      localStorage.removeItem('tf-panel-width');
    });
  })();

  connect();
  syncPauseBtn();
  renderRegisteredTasks();
  updateThemeBtn(document.documentElement.getAttribute('data-theme') || 'light');
"""
