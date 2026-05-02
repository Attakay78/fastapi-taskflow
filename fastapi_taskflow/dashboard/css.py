DASHBOARD_CSS = r"""
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --db-bg: #f5f5f5; --db-surface: #ffffff; --db-surface-2: #fafafa;
      --db-surface-3: #f3f4f6; --db-surface-hover: #f9fafb;
      --db-border: #e5e7eb; --db-border-2: #f3f4f6;
      --db-text: #111111; --db-text-2: #374151; --db-text-3: #6b7280;
      --db-text-muted: #888888; --db-text-faint: #aaaaaa; --db-text-xfaint: #cccccc;
    }
    [data-theme="dark"] {
      --db-bg: #0d1117; --db-surface: #161b22; --db-surface-2: #1c2128;
      --db-surface-3: #21262d; --db-surface-hover: #1c2128;
      --db-border: #30363d; --db-border-2: #21262d;
      --db-text: #e6edf3; --db-text-2: #c9d1d9; --db-text-3: #8b949e;
      --db-text-muted: #8b949e; --db-text-faint: #6e7681; --db-text-xfaint: #484f58;
    }

    body {
      font-family: 'Geist', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      font-size: 14px;
      background: var(--db-bg);
      color: var(--db-text);
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
    }

    /* ── Header ─────────────────────────────────────────────── */
    .header {
      background: var(--db-surface);
      border-bottom: 1px solid var(--db-border);
      height: 52px;
      display: flex;
      align-items: center;
      padding: 0 24px;
      position: sticky;
      top: 0;
      z-index: 100;
      gap: 10px;
    }
    .header-icon { width: 26px; height: 26px; flex-shrink: 0; }
    .header-title { font-size: 14px; font-weight: 600; color: #009688; }
    .logout-btn { font-size: 12px; color: var(--db-text-3); text-decoration: none; padding: 4px 10px; border: 1px solid var(--db-border); border-radius: 5px; font-weight: 500; transition: color .15s, background .15s; }
    .logout-btn:hover { color: var(--db-text); background: var(--db-surface-3); }
    .header-badge { font-size: 11px; color: #009688; background: rgba(0,150,136,.08); border: 1px solid rgba(0,150,136,.25); border-radius: 4px; padding: 1px 7px; font-weight: 500; }
    .theme-btn { width: 28px; height: 28px; border: 1px solid var(--db-border); border-radius: 6px; background: var(--db-surface); cursor: pointer; color: var(--db-text-muted); display: flex; align-items: center; justify-content: center; transition: background .1s, border-color .1s, color .1s; flex-shrink: 0; }

    /* ── Main ───────────────────────────────────────────────── */
    .main { max-width: 1440px; margin: 0 auto; padding: 24px 24px 64px; }

    .dot { width: 7px; height: 7px; border-radius: 50%; background: #d1d5db; flex-shrink: 0; transition: background .3s; }
    .dot--live  { background: #17c964; animation: pulse 2s ease-in-out infinite; }
    .dot--error { background: #f31260; }
    .dot--connecting { background: #f59e0b; }
    @keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:.35 } }
    .status-label { font-size: 12px; color: var(--db-text-muted); font-weight: 500; }
    .status-label--live  { color: #16a34a; }
    .status-label--error { color: #dc2626; }

    /* ── Top row (metrics + filters) ────────────────────────── */
    .top-row { display: flex; gap: 16px; align-items: flex-start; margin-bottom: 8px; }
    .search-row { display: flex; gap: 16px; align-items: center; margin-bottom: 16px; }
    .search-row .search { flex: 1; height: 34px; }
    .search-row .filter-trigger-btn { width: 280px; flex-shrink: 0; height: 34px; font-size: 12px; justify-content: center; }
    .metrics {
      flex: 1;
      display: grid;
      grid-template-columns: repeat(9, minmax(0, 1fr));
      gap: 8px;
    }
    .filters-right {
      width: 280px;
      flex-shrink: 0;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      align-content: center;
    }
    .filters-right .sel,
    .filters-right .filter-trigger-btn { width: 100%; justify-content: center; height: 34px; font-size: 12px; }
    .filters-right .sel { -webkit-appearance: none; appearance: none; padding: 0 28px 0 10px; background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%236b7280' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 8px center; }
    .filters-right .filter-trigger-btn { gap: 5px; }


    /* ── Inputs ─────────────────────────────────────────────── */
    .sel, .search {
      height: 32px;
      border: 1px solid var(--db-border); border-radius: 6px;
      padding: 0 10px; font-size: 13px; font-family: inherit;
      background: var(--db-surface); color: var(--db-text); outline: none;
      transition: border-color .15s, box-shadow .15s; cursor: pointer;
      width: 100%;
    }
    .sel option { background: var(--db-surface); color: var(--db-text); }
    .sel:hover, .search:hover { border-color: var(--db-text-muted); }
    .sel:focus, .search:focus { border-color: #009688; box-shadow: 0 0 0 3px rgba(0,150,136,.12); }
    .search { cursor: text; }
    .search::placeholder { color: var(--db-text-xfaint); }
    .metric-card {
      border: 1px solid transparent;
      border-radius: 10px;
      padding: 10px 12px;
    }
    .metric-label {
      font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
      font-weight: 500; margin-bottom: 6px; opacity: .75;
    }
    .metric-value { font-size: 20px; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: -.5px; }

    .mc-total   { background: #f5f3ff; border-color: #e9d5ff; }
    .mc-total   .metric-label { color: #6d28d9; }
    .mc-total   .metric-value { color: #5b21b6; }

    .mc-pending { background: #f9fafb; border-color: #e5e7eb; }
    .mc-pending .metric-label { color: #6b7280; }
    .mc-pending .metric-value { color: #374151; }

    .mc-running { background: #eef2ff; border-color: #c7d2fe; }
    .mc-running .metric-label { color: #4338ca; }
    .mc-running .metric-value { color: #3730a3; }

    .mc-success { background: #f0fdf4; border-color: #bbf7d0; }
    .mc-success .metric-label { color: #15803d; }
    .mc-success .metric-value { color: #166534; }

    .mc-failed  { background: #fef2f2; border-color: #fecaca; }
    .mc-failed  .metric-label { color: #b91c1c; }
    .mc-failed  .metric-value { color: #991b1b; }

    .mc-interrupted { background: #fffbeb; border-color: #fcd34d; }
    .mc-interrupted .metric-label { color: #b45309; }
    .mc-interrupted .metric-value { color: #92400e; }

    .mc-rate    { background: #fffbeb; border-color: #fde68a; }
    .mc-rate    .metric-label { color: #b45309; }
    .mc-rate    .metric-value { color: #92400e; }

    .mc-avg     { background: #fdf4ff; border-color: #f0abfc; }
    .mc-avg     .metric-label { color: #a21caf; }
    .mc-avg     .metric-value { color: #86198f; }

    [data-theme="dark"] .mc-total   { background: rgba(124,58,237,.1); border-color: rgba(124,58,237,.25); }
    [data-theme="dark"] .mc-total   .metric-label { color: #a78bfa; }
    [data-theme="dark"] .mc-total   .metric-value { color: #c4b5fd; }
    [data-theme="dark"] .mc-pending { background: var(--db-surface-2); border-color: var(--db-border); }
    [data-theme="dark"] .mc-pending .metric-label { color: #9ca3af; }
    [data-theme="dark"] .mc-pending .metric-value { color: #d1d5db; }
    [data-theme="dark"] .mc-running { background: rgba(67,56,202,.12); border-color: rgba(99,102,241,.3); }
    [data-theme="dark"] .mc-running .metric-label { color: #818cf8; }
    [data-theme="dark"] .mc-running .metric-value { color: #a5b4fc; }
    [data-theme="dark"] .mc-success { background: rgba(21,128,61,.1); border-color: rgba(21,128,61,.3); }
    [data-theme="dark"] .mc-success .metric-label { color: #4ade80; }
    [data-theme="dark"] .mc-success .metric-value { color: #86efac; }
    [data-theme="dark"] .mc-failed  { background: rgba(185,28,28,.1); border-color: rgba(220,38,38,.25); }
    [data-theme="dark"] .mc-failed  .metric-label { color: #f87171; }
    [data-theme="dark"] .mc-failed  .metric-value { color: #fca5a5; }
    [data-theme="dark"] .mc-interrupted { background: rgba(180,83,9,.1); border-color: rgba(217,119,6,.3); }
    [data-theme="dark"] .mc-interrupted .metric-label { color: #fbbf24; }
    [data-theme="dark"] .mc-interrupted .metric-value { color: #fcd34d; }
    [data-theme="dark"] .mc-rate    { background: rgba(180,83,9,.1); border-color: rgba(245,158,11,.25); }
    [data-theme="dark"] .mc-rate    .metric-label { color: #fbbf24; }
    [data-theme="dark"] .mc-rate    .metric-value { color: #fcd34d; }
    [data-theme="dark"] .mc-avg     { background: rgba(162,28,175,.1); border-color: rgba(217,70,239,.25); }
    [data-theme="dark"] .mc-avg     .metric-label { color: #e879f9; }
    [data-theme="dark"] .mc-avg     .metric-value { color: #f0abfc; }

    /* ── Table ──────────────────────────────────────────────── */
    .table-wrap { background: var(--db-surface); border: 1px solid var(--db-border); border-radius: 10px; overflow: hidden; }
    table { width: 100%; border-collapse: collapse; }
    .th { padding: 9px 14px; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--db-text-muted); font-weight: 600; background: var(--db-surface-2); border-bottom: 1px solid var(--db-border); cursor: pointer; user-select: none; white-space: nowrap; }
    .th:hover { color: var(--db-text-2); background: var(--db-surface-3); }
    .th--active { color: #009688; }
    .th--r { text-align: right; }
    .sort-icon { opacity: .4; font-size: 10px; margin-left: 3px; }
    .th--active .sort-icon { opacity: 1; }
    .td { padding: 10px 14px; border-bottom: 1px solid var(--db-border-2); font-size: 13px; color: var(--db-text-2); vertical-align: middle; }
    .row { cursor: pointer; transition: background .1s; }
    .row:hover { background: var(--db-surface-hover); }
    .row--selected { background: rgba(0,150,136,.07) !important; }
    .row:last-child .td { border-bottom: none; }
    .td--mono { font-family: 'Geist Mono', 'JetBrains Mono', 'Fira Code', monospace; font-size: 11.5px; color: var(--db-text-faint); }
    .td--func { font-weight: 500; color: var(--db-text); }
    .td--r    { text-align: right; color: var(--db-text-3); font-variant-numeric: tabular-nums; }
    .td--date { color: var(--db-text-faint); font-size: 12px; white-space: nowrap; padding-left: 24px; }
    .td--err  { max-width: 180px; }
    .err-text { color: #dc2626; font-size: 12px; }
    .muted { color: var(--db-text-xfaint); }
    .empty { text-align: center; padding: 52px 16px; color: var(--db-text-faint); font-size: 13px; }

    /* ── Badges ─────────────────────────────────────────────── */
    .badge { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; letter-spacing: .02em; }
    .badge--pending { background: #f3f4f6; color: #6b7280; }
    .badge--running { background: #ede9fe; color: #7c3aed; }
    .badge--success { background: #dcfce7; color: #16a34a; }
    .badge--failed       { background: #fee2e2; color: #dc2626; }
    .badge--interrupted  { background: #fffbeb; color: #d97706; }
    [data-theme="dark"] .badge--pending     { background: var(--db-surface-3); color: #9ca3af; }
    [data-theme="dark"] .badge--running     { background: rgba(124,58,237,.2); color: #a78bfa; }
    [data-theme="dark"] .badge--success     { background: rgba(22,163,74,.2); color: #4ade80; }
    [data-theme="dark"] .badge--failed      { background: rgba(220,38,38,.2); color: #f87171; }
    [data-theme="dark"] .badge--interrupted { background: rgba(217,119,6,.15); color: #fbbf24; }

    /* ── Detail Panel ───────────────────────────────────────── */
    .backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.2); opacity: 0; pointer-events: none; transition: opacity .2s; z-index: 200; }
    .backdrop--on { opacity: 1; pointer-events: auto; }
    [data-theme="dark"] .backdrop { background: rgba(0,0,0,.55); }
    .panel { position: fixed; top: 0; right: 0; bottom: 0; width: 440px; max-width: 100vw; background: var(--db-surface); border-left: 1px solid var(--db-border); transform: translateX(100%); transition: transform .25s cubic-bezier(.4,0,.2,1); z-index: 201; display: flex; flex-direction: column; }
    .panel--open { transform: translateX(0); }
    .panel-header { display: flex; flex-direction: column; padding: 12px 20px 0; border-bottom: 1px solid var(--db-border-2); flex-shrink: 0; gap: 0; }
    .panel-header-row { display: flex; align-items: center; gap: 10px; padding-bottom: 10px; }
    .panel-title { font-size: 14px; font-weight: 600; color: var(--db-text); }
    .panel-tabs { display: flex; gap: 2px; margin: 0 -20px; padding: 0 20px; }
    .panel-tab { padding: 5px 11px; font-size: 12px; font-weight: 500; color: var(--db-text-3); background: none; border: none; border-bottom: 2px solid transparent; cursor: pointer; margin-bottom: -1px; border-radius: 4px 4px 0 0; transition: color .12s; }
    .panel-tab:hover { color: var(--db-text); }
    .panel-tab.panel-tab--active { color: #009688; border-bottom-color: #009688; }
    .panel-tab--error.panel-tab--active { color: #dc2626; border-bottom-color: #dc2626; }
    .panel-close { margin-left: auto; width: 28px; height: 28px; border: 1px solid var(--db-border); border-radius: 6px; background: var(--db-surface); cursor: pointer; display: flex; align-items: center; justify-content: center; color: var(--db-text-3); transition: background .1s, border-color .1s; }
    .panel-close:hover { background: var(--db-surface-3); color: var(--db-text); }
    .retry-btn { display: inline-flex; align-items: center; gap: 5px; padding: 5px 12px; font-size: 12px; font-weight: 500; border-radius: 5px; border: 1px solid var(--db-border); background: var(--db-surface); color: var(--db-text-2); cursor: pointer; transition: background .1s, border-color .1s, color .1s; }
    .retry-btn:hover { background: var(--db-surface-3); border-color: #6366f1; color: #6366f1; }
    .retry-btn:disabled { opacity: .5; cursor: not-allowed; }
    .retry-btn--warn { border-color: #f59e0b; color: #b45309; }
    .retry-btn--warn:hover { background: #fffbeb; border-color: #d97706; color: #92400e; }
    .panel-body { flex: 1; overflow-y: auto; padding: 20px; overscroll-behavior: contain; }
    .panel-resize { position: absolute; top: 0; left: 0; width: 5px; height: 100%; cursor: col-resize; z-index: 1; }
    .panel-resize::after { content: ''; position: absolute; top: 50%; left: 1px; transform: translateY(-50%); width: 3px; height: 32px; border-radius: 2px; background: var(--db-border); transition: background .15s; }
    .panel-resize:hover::after, .panel-resize--active::after { background: #009688; }

    /* Copy button */
    .copy-btn { display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px; flex-shrink: 0; border: 1px solid var(--db-border); border-radius: 5px; background: var(--db-surface); cursor: pointer; color: var(--db-text-faint); transition: background .1s, border-color .1s, color .1s; }
    .copy-btn:hover { background: var(--db-surface-3); border-color: var(--db-text-faint); color: var(--db-text); }
    .copy-btn--row { width: 18px; height: 18px; border-radius: 3px; opacity: 0; margin-left: 5px; vertical-align: middle; flex-shrink: 0; }
    .row:hover .copy-btn--row { opacity: 1; }

    /* Toast */
    .toast { position: fixed; bottom: 24px; right: 24px; z-index: 400; background: var(--db-text); color: var(--db-bg); font-size: 13px; font-weight: 500; padding: 10px 16px; border-radius: 8px; display: flex; align-items: center; gap: 8px; box-shadow: 0 4px 14px rgba(0,0,0,.2); opacity: 0; transform: translateY(6px); transition: opacity .2s, transform .2s; pointer-events: none; }
    .toast--on { opacity: 1; transform: translateY(0); }

    /* Pagination */
    .pagination { display: flex; align-items: center; gap: 8px; margin-top: 10px; justify-content: flex-end; }
    .pg-btn { height: 30px; min-width: 30px; padding: 0 10px; border: 1px solid var(--db-border); border-radius: 6px; background: var(--db-surface); cursor: pointer; font-size: 12px; font-family: inherit; color: var(--db-text-2); transition: background .1s, border-color .1s; }
    .pg-btn:hover:not(:disabled) { background: var(--db-surface-3); }
    .pg-btn:disabled { opacity: .35; cursor: default; }
    .pg-info { font-size: 12px; color: var(--db-text-muted); }

    /* Detail content */
    .d-section { margin-bottom: 16px; }
    .d-label { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--db-text-faint); font-weight: 500; margin-bottom: 4px; }
    .d-val { font-size: 13px; color: var(--db-text); }
    .d-mono { font-family: 'Geist Mono', monospace; font-size: 11.5px; color: var(--db-text-3); word-break: break-all; }
    .d-func { font-weight: 600; font-size: 14px; }
    .d-error { font-size: 12px; color: #dc2626; background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; padding: 10px 12px; font-family: 'Geist Mono', monospace; white-space: pre-wrap; word-break: break-word; }
    [data-theme="dark"] .d-error { background: rgba(220,38,38,.1); border-color: rgba(220,38,38,.3); color: #fca5a5; }
    .d-tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--db-border); margin-bottom: 12px; }
    .d-tab { padding: 5px 12px; font-size: 12px; font-weight: 500; color: var(--db-text-3); background: none; border: none; border-bottom: 2px solid transparent; cursor: pointer; margin-bottom: -1px; border-radius: 4px 4px 0 0; transition: color .12s; }
    .d-tab:hover { color: var(--db-text); }
    .d-tab.d-tab--active { color: #009688; border-bottom-color: #009688; }
    .d-tab.d-tab--error.d-tab--active { color: #dc2626; border-bottom-color: #dc2626; }
    .d-tab-panel { display: none; }
    .d-tab-panel.d-tab-panel--active { display: block; }
    .d-logs { font-family: 'Geist Mono', monospace; font-size: 11.5px; color: var(--db-text-2); background: var(--db-surface-2); border: 1px solid var(--db-border); border-radius: 6px; padding: 10px 12px; white-space: pre-wrap; word-break: break-all; line-height: 1.6; }
    .d-logs .log-line { display: flex; gap: 8px; }
    .d-logs .log-ts { color: var(--db-text-faint); flex-shrink: 0; }
    .d-logs .log-sep { color: var(--db-text-faint); font-style: italic; }
    .d-error-msg { font-size: 12px; color: #dc2626; background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; padding: 10px 12px; font-weight: 500; margin-bottom: 8px; }
    [data-theme="dark"] .d-error-msg { background: rgba(220,38,38,.1); border-color: rgba(220,38,38,.3); color: #f87171; }
    .d-stacktrace { font-size: 11px; color: #7f1d1d; background: #fff5f5; padding: 10px 12px; font-family: 'Geist Mono', monospace; white-space: pre-wrap; word-break: break-all; border: 1px solid #fecaca; border-radius: 6px; }
    [data-theme="dark"] .d-stacktrace { color: #fca5a5; background: rgba(220,38,38,.08); border-color: rgba(220,38,38,.25); }
    .d-row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; margin-bottom: 16px; }
    .d-row3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0 10px; margin-bottom: 16px; }
    .divider { height: 1px; background: var(--db-border-2); margin: 20px 0; }
    .section-title { display: flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 600; color: var(--db-text-3); text-transform: uppercase; letter-spacing: .05em; margin-bottom: 12px; }
    .analytics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .a-card { background: var(--db-surface-2); border: 1px solid var(--db-border-2); border-radius: 6px; padding: 10px 12px; }
    .a-label { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: var(--db-text-xfaint); font-weight: 500; margin-bottom: 3px; }
    .a-value { font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; }
    .a-value--neutral { color: var(--db-text); }
    .a-value--success { color: #16a34a; }
    .a-value--danger  { color: #dc2626; }
    .a-value--running { color: #7c3aed; }
    .a-value--warning { color: #d97706; }
    [data-theme="dark"] .a-value--success { color: #4ade80; }
    [data-theme="dark"] .a-value--danger  { color: #f87171; }
    [data-theme="dark"] .a-value--running { color: #a78bfa; }
    [data-theme="dark"] .a-value--warning { color: #fbbf24; }
    .recent-runs { display: flex; flex-direction: column; gap: 6px; }
    .r-run { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border: 1px solid var(--db-border-2); border-radius: 6px; cursor: pointer; transition: background .1s; }
    .r-run:hover { background: var(--db-surface-2); }
    .r-run--cur { background: rgba(0,150,136,.07); border-color: rgba(0,150,136,.25); }
    .r-dur { font-size: 11px; margin-left: auto; color: var(--db-text-faint); }
    .arg-idx  { font-size: 10px; color: var(--db-text-xfaint); min-width: 18px; text-align: right; }
    .arg-key  { font-size: 11px; color: var(--db-text-3); }
    .arg-eq   { font-size: 11px; color: var(--db-text-xfaint); }
    .arg-code { background: var(--db-surface-2); border: 1px solid var(--db-border-2); border-radius: 4px; padding: 2px 7px; font-size: 11.5px; }

    /* ── Pause button ───────────────────────────────────────── */
    .pause-btn { display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; font-size: 12px; font-weight: 500; border-radius: 5px; border: 1px solid var(--db-border); background: var(--db-surface); color: var(--db-text-2); cursor: pointer; transition: background .1s, border-color .1s; }
    .pause-btn:hover { background: var(--db-surface-3); }
    .pause-btn--paused { border-color: #f59e0b; color: #b45309; background: #fffbeb; }
    [data-theme="dark"] .pause-btn--paused { background: rgba(180,83,9,.15); color: #fbbf24; border-color: rgba(217,119,6,.4); }
    .new-badge { display: inline-block; background: #f59e0b; color: #fff; font-size: 10px; font-weight: 700; border-radius: 10px; padding: 1px 6px; margin-left: 2px; }
    .source-badge { display: inline-block; background: rgba(0,150,136,.12); color: #009688; font-size: 10px; font-weight: 700; border-radius: 10px; padding: 1px 6px; margin-left: 4px; letter-spacing: .02em; }
    [data-theme="dark"] .source-badge { background: rgba(0,150,136,.2); color: #4db6ac; }

    /* ── Filter / Clear history trigger buttons ─────────────── */
    .filter-trigger-btn { display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; font-size: 12px; font-weight: 500; border-radius: 5px; border: 1px solid var(--db-border); background: var(--db-surface); color: var(--db-text-2); cursor: pointer; transition: background .1s, border-color .1s; white-space: nowrap; }
    .filter-trigger-btn:hover { background: var(--db-surface-3); }
    .filter-trigger-btn--active { border-color: #009688; color: #009688; background: rgba(0,150,136,.07); }
    [data-theme="dark"] .filter-trigger-btn--active { background: rgba(0,150,136,.15); }
    .filter-trigger-btn--danger { color: #dc2626; }
    .filter-trigger-btn--danger:hover { background: #fef2f2; border-color: #fca5a5; }
    [data-theme="dark"] .filter-trigger-btn--danger { color: #f87171; }
    [data-theme="dark"] .filter-trigger-btn--danger:hover { background: rgba(220,38,38,.12); border-color: rgba(248,113,113,.3); }

    /* ── Popup modals ────────────────────────────────────────── */
    .popup-backdrop { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 200; align-items: center; justify-content: center; }
    .popup-backdrop--open { display: flex; }
    .popup { background: var(--db-surface); border: 1px solid var(--db-border); border-radius: 12px; padding: 24px; width: 340px; max-width: 95vw; box-shadow: 0 20px 60px rgba(0,0,0,.18); }
    [data-theme="dark"] .popup { box-shadow: 0 20px 60px rgba(0,0,0,.5); }
    .popup-title { font-size: 15px; font-weight: 700; color: var(--db-text); margin: 0 0 4px; letter-spacing: -.01em; }
    .popup-desc { font-size: 12px; color: var(--db-text-2); margin: 0 0 18px; line-height: 1.5; }
    .popup-field { margin-bottom: 14px; }
    .popup-label { display: block; font-size: 11px; font-weight: 600; color: var(--db-text-2); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 6px; }
    .popup-row { display: flex; gap: 8px; }
    .popup-input { flex: 1; padding: 8px 10px; font-size: 13px; border: 1px solid var(--db-border); border-radius: 7px; background: var(--db-bg); color: var(--db-text); outline: none; transition: border-color .15s; font-family: inherit; }
    .popup-input:focus { border-color: #009688; }
    .popup-select { padding: 8px 10px; font-size: 13px; border: 1px solid var(--db-border); border-radius: 7px; background: var(--db-bg); color: var(--db-text); outline: none; cursor: pointer; font-family: inherit; }
    .popup-select:focus { border-color: #009688; }
    .popup-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 20px; }
    .popup-btn { padding: 7px 16px; font-size: 12px; font-weight: 600; border-radius: 7px; cursor: pointer; border: 1px solid transparent; transition: background .1s, opacity .1s; font-family: inherit; }
    .popup-btn--primary { background: #009688; color: #fff; border-color: #009688; }
    .popup-btn--primary:hover { background: #00796b; border-color: #00796b; }
    .popup-btn--danger { background: #dc2626; color: #fff; border-color: #dc2626; }
    .popup-btn--danger:hover { background: #b91c1c; border-color: #b91c1c; }
    .popup-btn--cancel { background: transparent; color: var(--db-text-2); border-color: var(--db-border); }
    .popup-btn--cancel:hover { background: var(--db-surface-3); }
    .popup-warning { font-size: 12px; color: #dc2626; background: #fef2f2; border: 1px solid #fca5a5; border-radius: 7px; padding: 8px 10px; margin-top: 12px; line-height: 1.5; }
    [data-theme="dark"] .popup-warning { background: rgba(220,38,38,.1); border-color: rgba(248,113,113,.3); color: #f87171; }

    /* ── Bulk retry toolbar ─────────────────────────────────── */
    .bulk-bar { display: none; align-items: center; gap: 10px; padding: 8px 12px; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 7px; margin-bottom: 10px; font-size: 13px; color: #1e40af; }
    .bulk-bar--on { display: flex; }
    [data-theme="dark"] .bulk-bar { background: rgba(37,99,235,.12); border-color: rgba(99,162,235,.25); color: #93c5fd; }
    .bulk-btn { padding: 5px 14px; font-size: 12px; font-weight: 600; border-radius: 5px; border: 1px solid #3b82f6; background: #3b82f6; color: #fff; cursor: pointer; transition: background .1s; }
    .bulk-btn:hover { background: #2563eb; }
    .bulk-btn:disabled { opacity: .5; cursor: not-allowed; }
    .bulk-btn--cancel { background: transparent; border-color: var(--db-border); color: var(--db-text-2); }
    .bulk-btn--cancel:hover { background: var(--db-surface-3); }

    /* ── Checkbox column ────────────────────────────────────── */
    .th--check, .td--check { width: 36px; padding: 0 0 0 12px; }
    .td--check { vertical-align: middle; }
    input.row-check { cursor: pointer; accent-color: #3b82f6; width: 14px; height: 14px; }

    /* ── DLQ toolbar ────────────────────────────────────────── */
    .dlq-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
    .dlq-label { font-size: 13px; color: var(--db-text-2); }
    .dlq-select { font-size: 13px; padding: 4px 8px; border-radius: 5px; border: 1px solid var(--db-border); background: var(--db-surface-2); color: var(--db-text); cursor: pointer; }

    /* ── Tab bar (segmented control) ────────────────────────── */
    .tab-bar { display: inline-flex; gap: 2px; margin-bottom: 16px; background: var(--db-surface-3); border: 1px solid var(--db-border); border-radius: 9px; padding: 3px; }
    .tab-btn { background: none; border: none; border-radius: 7px; padding: 5px 14px; font-size: 12.5px; font-weight: 500; color: var(--db-text-muted); cursor: pointer; transition: color .15s, background .15s, box-shadow .15s; white-space: nowrap; }
    .tab-btn:hover { color: var(--db-text-2); background: rgba(0,0,0,.04); }
    [data-theme="dark"] .tab-btn:hover { background: rgba(255,255,255,.05); }
    .tab-btn--active { background: var(--db-surface); color: var(--db-text); box-shadow: 0 1px 3px rgba(0,0,0,.10), 0 0 0 1px rgba(0,0,0,.06); }
    [data-theme="dark"] .tab-btn--active { box-shadow: 0 1px 3px rgba(0,0,0,.4), 0 0 0 1px rgba(255,255,255,.06); }
    .tab-count { display: inline-block; background: rgba(0,150,136,.12); color: #009688; font-size: 10px; font-weight: 700; border-radius: 10px; padding: 1px 6px; margin-left: 4px; }
    [data-theme="dark"] .tab-count { background: rgba(0,150,136,.2); color: #4db6ac; }
    .tab-btn--active .tab-count { background: rgba(0,150,136,.15); }


    /* ── Cancelled badge ────────────────────────────────────── */
    .badge--cancelled { background: #fce7f3; color: #be185d; }
    [data-theme="dark"] .badge--cancelled { background: rgba(190,24,93,.2); color: #f472b6; }

    /* ── Cancelled metric card ──────────────────────────────── */
    .mc-cancelled { background: #fce7f3; border-color: #f9a8d4; }
    .mc-cancelled .metric-label { color: #9d174d; }
    .mc-cancelled .metric-value { color: #831843; }
    [data-theme="dark"] .mc-cancelled { background: rgba(190,24,93,.1); border-color: rgba(244,114,182,.25); }
    [data-theme="dark"] .mc-cancelled .metric-label { color: #f472b6; }
    [data-theme="dark"] .mc-cancelled .metric-value { color: #fbcfe8; }

    /* ── Cancel button ──────────────────────────────────────── */
    .cancel-btn { display: inline-flex; align-items: center; gap: 5px; padding: 5px 12px; font-size: 12px; font-weight: 500; border-radius: 5px; border: 1px solid #fca5a5; background: var(--db-surface); color: #dc2626; cursor: pointer; transition: background .1s, border-color .1s; }
    .cancel-btn:hover { background: #fef2f2; border-color: #ef4444; }
    .cancel-btn:disabled { opacity: .5; cursor: not-allowed; }

    /* ── Audit table ────────────────────────────────────────── */
    .audit-empty { text-align: center; padding: 40px; color: var(--db-text-faint); font-size: 13px; }

    /* ── Registered tasks grid ───────────────────────────────── */
    .rtask-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
    .rtask-card { background: var(--db-surface); border: 1px solid var(--db-border); border-radius: 8px; padding: 12px 14px; transition: border-color .15s, box-shadow .15s; }
    .rtask-card:hover { border-color: #009688; box-shadow: 0 2px 8px rgba(0,150,136,.08); }
    .rtask-name { font-family: 'Geist Mono', monospace; font-size: 12.5px; font-weight: 600; color: var(--db-text); margin-bottom: 10px; word-break: break-all; line-height: 1.4; }
    .rtask-pills { display: flex; flex-wrap: wrap; gap: 4px; }
    .rtask-pill { font-size: 10.5px; font-weight: 500; padding: 2px 7px; border-radius: 4px; background: var(--db-surface-3); color: var(--db-text-3); border: 1px solid var(--db-border-2); white-space: nowrap; }
    .rtask-pill--accent { background: rgba(0,150,136,.08); color: #009688; border-color: rgba(0,150,136,.2); }
    [data-theme="dark"] .rtask-pill--accent { background: rgba(0,150,136,.15); color: #4db6ac; }
"""
