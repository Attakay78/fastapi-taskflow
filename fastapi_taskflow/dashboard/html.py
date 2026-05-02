DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Task Dashboard · FastAPI-TaskFlow</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200'><rect width='200' height='200' rx='40' ry='40' fill='%23009688'/><polyline points='20,100 55,100 80,145 120,55 145,100 180,100' fill='none' stroke='white' stroke-width='18' stroke-linecap='round' stroke-linejoin='round'/></svg>">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Geist:wght@300;400;500;600;700&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
  <script>(function(){var t=localStorage.getItem('tf-theme')||'light';document.documentElement.setAttribute('data-theme',t);})();</script>
  <style>
__CSS_BLOCK__
  </style>
</head>
<body>

<header class="header">
  <svg class="header-icon" width="26" height="26" viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg">
    <rect width="200" height="200" rx="40" ry="40" fill="#009688"/>
    <polyline points="20,100 55,100 80,145 120,55 145,100 180,100" fill="none" stroke="white" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>
  <span class="header-title">Task Dashboard</span>
  <span class="header-badge">__TITLE__</span>
  <div style="margin-left:auto;display:flex;align-items:center;gap:10px">
    <span class="dot dot--connecting" id="live-dot"></span>
    <span class="status-label" id="live-label">Connecting&#8230;</span>
    <button class="pause-btn" id="pause-btn" onclick="togglePause()" title="Pause live updates">
      <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
      Pause
    </button>
    <button class="theme-btn" id="theme-btn" onclick="toggleTheme()" title="Switch to dark mode">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
    </button>
    __LOGOUT_BUTTON__
  </div>
</header>

<main class="main">

  <!-- Top row: metrics left, filters right -->
  <div class="top-row">
    <!-- Metrics -->
    <div class="metrics">
      <div class="metric-card mc-total">  <div class="metric-label">Total</div>        <div class="metric-value" id="metric-total">&#8212;</div></div>
      <div class="metric-card mc-pending"><div class="metric-label">Pending</div>       <div class="metric-value" id="metric-pending">&#8212;</div></div>
      <div class="metric-card mc-running"><div class="metric-label">Running</div>       <div class="metric-value" id="metric-running">&#8212;</div></div>
      <div class="metric-card mc-success"><div class="metric-label">Success</div>       <div class="metric-value" id="metric-success">&#8212;</div></div>
      <div class="metric-card mc-failed"> <div class="metric-label">Failed</div>        <div class="metric-value" id="metric-failed">&#8212;</div></div>
      <div class="metric-card mc-interrupted"><div class="metric-label">Interrupted</div>  <div class="metric-value" id="metric-interrupted">&#8212;</div></div>
      <div class="metric-card mc-cancelled"><div class="metric-label">Cancelled</div>    <div class="metric-value" id="metric-cancelled">&#8212;</div></div>
      <div class="metric-card mc-rate">   <div class="metric-label">Success Rate</div> <div class="metric-value" id="metric-rate">&#8212;</div></div>
      <div class="metric-card mc-avg">    <div class="metric-label">Avg Duration</div> <div class="metric-value" id="metric-avg">&#8212;</div></div>
    </div>
    <!-- Filters -->
    <div class="filters-right">
      <select class="sel" id="status-filter">
        <option value="all">All statuses</option>
        <option value="pending">Pending</option>
        <option value="running">Running</option>
        <option value="success">Success</option>
        <option value="failed">Failed</option>
        <option value="interrupted">Interrupted</option>
        <option value="cancelled">Cancelled</option>
      </select>
      <select class="sel" id="func-filter">
        <option value="all">All functions</option>
      </select>
      <button class="filter-trigger-btn" id="time-filter-btn" onclick="openTimeFilterPopup()">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        <span id="time-filter-label">All time</span>
      </button>
      <button class="filter-trigger-btn" onclick="exportCsv()" title="Export current view as CSV">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        Export CSV
      </button>
    </div>
  </div>

  <!-- Search + clear history row -->
  <div class="search-row">
    <input type="search" class="search" id="search-input" placeholder="Search by ID or function&#8230;" autocomplete="off">
    <button class="filter-trigger-btn filter-trigger-btn--danger" onclick="openClearHistoryPopup()">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4h6v2"/></svg>
      Clear history
    </button>
  </div>

  <!-- Tab bar -->
  <div class="tab-bar">
    <button class="tab-btn tab-btn--active" id="tab-view" onclick="showTab('view')">View<span class="tab-count" id="tab-view-count"></span></button>
    <button class="tab-btn" id="tab-deadletters" onclick="showTab('deadletters')">Dead Letters<span class="tab-count" id="tab-deadletters-count" style="background:rgba(220,38,38,.12);color:#dc2626"></span></button>
    <button class="tab-btn" id="tab-audit" onclick="showTab('audit')" style="display:none">Audit<span class="tab-count" id="tab-audit-count"></span></button>
    <button class="tab-btn" id="tab-schedules" onclick="showTab('schedules')">Schedules<span class="tab-count" id="tab-schedules-count"></span></button>
    <button class="tab-btn" id="tab-tasks" onclick="showTab('tasks')">Tasks<span class="tab-count" id="tab-tasks-count"></span></button>
  </div>

  <!-- View panel (task run history) -->
  <div id="panel-view">

  <!-- Bulk retry toolbar -->
  <div class="bulk-bar" id="bulk-bar">
    <span id="bulk-label">0 tasks selected</span>
    <button class="bulk-btn" id="bulk-retry-btn" onclick="bulkRetry()">Retry selected</button>
    <button class="bulk-btn bulk-btn--cancel" onclick="clearSelection()">Clear</button>
  </div>

  <!-- Table -->
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="th th--check"><input type="checkbox" class="row-check" id="select-all-check" onclick="toggleSelectAll(this)" title="Select all retryable on this page"></th>
          <th class="th" data-sort="task_id" onclick="setSort('task_id')">ID <span class="sort-icon">&#8661;</span></th>
          <th class="th" data-sort="func_name" onclick="setSort('func_name')">Function <span class="sort-icon">&#8661;</span></th>
          <th class="th" data-sort="status" onclick="setSort('status')">Status <span class="sort-icon">&#8661;</span></th>
          <th class="th" data-sort="duration" onclick="setSort('duration')">Duration <span class="sort-icon">&#8661;</span></th>
          <th class="th" data-sort="retries_used" onclick="setSort('retries_used')">Retries <span class="sort-icon">&#8661;</span></th>
          <th class="th" data-sort="created_at" onclick="setSort('created_at')">Created <span class="sort-icon">&#8595;</span></th>
          <th class="th" data-sort="priority" onclick="setSort('priority')">Priority <span class="sort-icon">&#8661;</span></th>
          <th class="th">Error</th>
        </tr>
      </thead>
      <tbody id="tasks-tbody">
        <tr><td colspan="9" class="empty">Connecting&#8230;</td></tr>
      </tbody>
    </table>
  </div>
  <div id="pagination"></div>

  </div>

  <!-- Dead Letters panel -->
  <div id="panel-deadletters" style="display:none">

    <!-- Toolbar: time-window replay -->
    <div class="dlq-toolbar">
      <span class="dlq-label">Replay window:</span>
      <select id="dlq-window-select" class="dlq-select">
        <option value="1h">Last 1 hour</option>
        <option value="6h" selected>Last 6 hours</option>
        <option value="24h">Last 24 hours</option>
        <option value="7d">Last 7 days</option>
        <option value="all">All time</option>
      </select>
      <button class="bulk-btn" onclick="dlqReplayWindow()">Replay window</button>
    </div>

    <!-- Bulk bar: shown when rows are checked -->
    <div class="bulk-bar" id="dlq-bulk-bar">
      <span id="dlq-bulk-label">0 tasks selected</span>
      <button class="bulk-btn" onclick="dlqReplaySelected()">Replay selected</button>
      <button class="bulk-btn bulk-btn--cancel" onclick="dlqClearSelection()">Clear</button>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th class="th th--check"><input type="checkbox" class="row-check" id="dlq-select-all" onclick="dlqToggleAll(this)" title="Select all"></th>
            <th class="th">ID</th>
            <th class="th">Function</th>
            <th class="th">Status</th>
            <th class="th">Duration</th>
            <th class="th">Created</th>
            <th class="th">Error</th>
          </tr>
        </thead>
        <tbody id="deadletters-tbody"></tbody>
      </table>
    </div>
  </div>


  <!-- Audit panel -->
  <div id="panel-audit" style="display:none">
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th class="th">Time</th>
          <th class="th">Action</th>
          <th class="th">Task ID</th>
          <th class="th">Actor</th>
          <th class="th">Detail</th>
        </tr></thead>
        <tbody id="audit-tbody"><tr><td colspan="5" class="audit-empty">Loading&#8230;</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- Schedules panel -->
  <div id="panel-schedules" style="display:none">
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th class="th">Function</th>
          <th class="th">Trigger</th>
          <th class="th">Next Run</th>
          <th class="th">Last Status</th>
        </tr></thead>
        <tbody id="schedules-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- Tasks panel (registered functions) -->
  <div id="panel-tasks" style="display:none">
    <div id="registered-tasks-list"></div>
  </div>

</main>

<div class="toast" id="toast"></div>
<div class="backdrop" id="detail-backdrop"></div>

<!-- Time filter popup -->
<div class="popup-backdrop" id="time-filter-popup" onclick="closeTimeFilterPopup(event)">
  <div class="popup" onclick="event.stopPropagation()">
    <div class="popup-title">Filter by time</div>
    <div class="popup-desc">Show tasks created within the last N units. Leave empty to show all.</div>
    <div class="popup-field">
      <label class="popup-label">Time window</label>
      <div class="popup-row">
        <input type="number" class="popup-input" id="tf-popup-val" placeholder="e.g. 6" min="1" autocomplete="off">
        <select class="popup-select" id="tf-popup-unit">
          <option value="min">Minutes</option>
          <option value="hour" selected>Hours</option>
          <option value="day">Days</option>
        </select>
      </div>
    </div>
    <div class="popup-actions">
      <button class="popup-btn popup-btn--cancel" onclick="clearTimeFilter()">Clear filter</button>
      <button class="popup-btn popup-btn--primary" onclick="applyTimeFilter()">Apply</button>
    </div>
  </div>
</div>

<!-- DLQ replay window confirmation popup -->
<div class="popup-backdrop" id="dlq-replay-popup" onclick="closeDlqReplayPopup(event)">
  <div class="popup" onclick="event.stopPropagation()">
    <div class="popup-title">Replay failed tasks</div>
    <div class="popup-desc" id="dlq-replay-popup-desc">Re-enqueue all failed tasks in the selected window. Each task will run again with its original arguments.</div>
    <div class="popup-actions">
      <button class="popup-btn popup-btn--cancel" onclick="closeDlqReplayPopup()">Cancel</button>
      <button class="popup-btn popup-btn--primary" id="dlq-replay-confirm-btn" onclick="confirmDlqReplayWindow()">Replay</button>
    </div>
  </div>
</div>

<!-- Clear history popup -->
<div class="popup-backdrop" id="clear-history-popup" onclick="closeClearHistoryPopup(event)">
  <div class="popup" onclick="event.stopPropagation()">
    <div class="popup-title">Clear task history</div>
    <div class="popup-desc">Delete completed tasks (success, failed, interrupted) older than the entered period.</div>
    <div class="popup-field">
      <label class="popup-label">Delete tasks older than</label>
      <div class="popup-row">
        <input type="number" class="popup-input" id="ch-popup-val" placeholder="e.g. 7" min="1" autocomplete="off">
        <select class="popup-select" id="ch-popup-unit">
          <option value="min">Minutes</option>
          <option value="hour">Hours</option>
          <option value="day" selected>Days</option>
        </select>
      </div>
    </div>
    <div class="popup-warning">This action cannot be undone. Pending and running tasks are never deleted.</div>
    <div class="popup-actions">
      <button class="popup-btn popup-btn--cancel" onclick="closeClearHistoryPopup()">Cancel</button>
      <button class="popup-btn popup-btn--danger" onclick="confirmClearHistory()">Delete</button>
    </div>
  </div>
</div>

<div class="panel" id="detail-panel">
  <div class="panel-resize" id="panel-resize-handle"></div>
  <div class="panel-header">
    <div class="panel-header-row">
      <span class="panel-title">Task Detail</span>
      <button class="panel-close" id="detail-close" aria-label="Close">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      </button>
    </div>
    <div class="panel-tabs" id="detail-tabs"></div>
  </div>
  <div class="panel-body" id="detail-content"></div>
</div>

<script>
__JS_BLOCK__
</script>
</body>
</html>"""
