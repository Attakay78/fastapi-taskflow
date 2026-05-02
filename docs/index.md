---
hide:
  - navigation
  - toc
---

<div class="hero">
  <h1>Retries, persistence, and visibility for <span class="hero-accent">FastAPI's BackgroundTasks</span></h1>
  <p class="sub">Production-grade background tasks. No workers, no brokers, drop-in.</p>
  <div class="hero-actions">
    <a class="btn btn-primary" href="getting-started/installation/">Get Started</a>
    <a class="btn btn-secondary" href="getting-started/quickstart/">Quick Start</a>
  </div>
</div>

<div class="home-page">

<div class="problem-section">
  <div class="problem-header">
    <p class="section-label">The problem</p>
    <p class="section-title">BackgroundTasks is fine, until production finds the gaps</p>
    <p class="problem-desc">You shipped <code>background_tasks.add_task(send_email, address=email)</code> and it worked in dev. Then you deployed it, a task failed, and you had no idea. No retry happened. No log survived. The user never got their email. You found out three days later when they complained.</p>
  </div>
  <div class="compare-grid">
    <div class="compare-col before">
      <h4>Without fastapi-taskflow</h4>
      <ul>
        <li>Tasks fail silently, no retry or backoff</li>
        <li>Tasks compete with request handlers for the same event loop</li>
        <li>Pending tasks lost on every restart</li>
        <li>No task IDs, no status, no history</li>
        <li>No priority, every task waits in the same queue</li>
        <li>Retried requests run the task again with no deduplication</li>
        <li>No dashboard, no logs, no stack traces on failure</li>
        <li>No persistence, everything lives in memory</li>
      </ul>
    </div>
    <div class="compare-col">
      <h4>With fastapi-taskflow</h4>
      <ul>
        <li>Automatic retries with configurable delay and backoff</li>
        <li>Concurrency caps keep tasks from starving request handlers</li>
        <li>Pending tasks requeued automatically on the next startup</li>
        <li>UUID per task, full lifecycle tracking and status history</li>
        <li>Priority queues so urgent tasks never wait behind batch jobs</li>
        <li>Idempotency keys prevent duplicate execution on retried requests</li>
        <li>Live dashboard over SSE with per-task logs and stack traces</li>
        <li>SQLite in dev, Redis, PostgreSQL, or MySQL in prod</li>
      </ul>
    </div>
  </div>
</div>

<div class="code-section">
  <p class="section-label">Quick look</p>
  <p class="section-title">Looks like FastAPI. Works like a task system.</p>

```python
from fastapi import BackgroundTasks, FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager, task_log

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()
TaskAdmin(app, task_manager, auto_install=True)


@task_manager.task(retries=3, delay=1.0, backoff=2.0)
def send_email(address: str) -> None:
    task_log(f"Sending to {address}")
    ...  # your logic here


@app.post("/signup")
def signup(email: str, background_tasks: BackgroundTasks):
    task_id = background_tasks.add_task(send_email, address=email)
    return {"task_id": task_id}
```

  <p class="code-note">The route signature does not change. <code>auto_install=True</code> handles the rest. <a href="getting-started/quickstart/">Read the Quick Start &rarr;</a></p>
</div>

<div class="dashboard-section">
  <p class="section-label">Dashboard</p>
  <p class="section-title">Live visibility out of the box</p>
  <div class="preview-tabs">
    <button class="preview-tab preview-tab--active" onclick="showPreview(this,'preview-dashboard')">Dashboard</button>
    <button class="preview-tab" onclick="showPreview(this,'preview-logs')">Logs</button>
    <button class="preview-tab" onclick="showPreview(this,'preview-error')">Error</button>
  </div>
  <div class="dashboard-preview">
    <div id="preview-dashboard" class="preview-panel preview-panel--active">
      <a href="assets/images/dashboard.png" target="_blank" class="img-link">
        <img src="assets/images/dashboard.png" alt="Task dashboard overview" class="dashboard-img" />
      </a>
    </div>
    <div id="preview-logs" class="preview-panel">
      <a href="assets/images/logs.png" target="_blank" class="img-link">
        <img src="assets/images/logs.png" alt="Task logs panel" class="dashboard-img" />
      </a>
    </div>
    <div id="preview-error" class="preview-panel">
      <a href="assets/images/error.png" target="_blank" class="img-link">
        <img src="assets/images/error.png" alt="Task error and stack trace panel" class="dashboard-img" />
      </a>
    </div>
  </div>
  <script>
  function showPreview(btn, panelId) {
    btn.closest('.dashboard-section').querySelectorAll('.preview-tab').forEach(function(t){ t.classList.remove('preview-tab--active'); });
    btn.closest('.dashboard-section').querySelectorAll('.preview-panel').forEach(function(p){ p.classList.remove('preview-panel--active'); });
    btn.classList.add('preview-tab--active');
    document.getElementById(panelId).classList.add('preview-panel--active');
  }
  </script>
</div>

<div class="who-section">
  <p class="section-label">Fit</p>
  <p class="section-title">Who this is for</p>
  <div class="who-grid">
    <div class="who-col">
      <h4>Good fit</h4>
      <ul>
        <li>Teams already using FastAPI's native <code>BackgroundTasks</code></li>
        <li>Apps that need retries, status tracking, and a dashboard without adding Celery</li>
        <li>Services where background work runs inside the same process as the web server</li>
        <li>Multi-instance deployments on SQLite (same host) or Redis, PostgreSQL, MySQL (any host)</li>
        <li>Teams who want structured task logs, encryption, and observability hooks without new infrastructure</li>
      </ul>
    </div>
    <div class="who-col">
      <h4>Not a good fit</h4>
      <ul>
        <li>Tasks that must run on dedicated worker machines completely separate from the web server</li>
        <li>Workflows that need message broker routing, fan-out, or cross-language workers</li>
      </ul>
    </div>
  </div>
</div>

<div class="section">
  <p class="section-label">Features</p>
  <p class="section-title">What you get</p>
  <div class="feature-list">

  <div class="feature-item">
    <p class="feature-item-label">Reliability</p>
    <h3>Tasks that survive failures and restarts</h3>
    <p>Automatic retries with configurable delay and exponential backoff. Unfinished tasks are saved on shutdown and re-dispatched on the next startup. Idempotency keys prevent duplicate execution across retried requests and webhooks.</p>
  </div>

  <div class="feature-item">
    <p class="feature-item-label">Visibility</p>
    <h3>See what happened without digging</h3>
    <p>Live admin panel at <code>/tasks/dashboard</code>. Every task has a UUID, a status, per-task logs, and a full stack trace on failure. All in one place, nothing to configure.</p>
  </div>

  <div class="feature-item">
    <p class="feature-item-label">Observability</p>
    <h3>Structured logs from inside your tasks</h3>
    <p>Call <code>task_log(message, level=, **extra)</code> from anywhere in a running task. Extras flow to observers as structured fields. <code>FileLogger</code>, <code>StdoutLogger</code>, and <code>InMemoryLogger</code> included.</p>
  </div>

  <div class="feature-item">
    <p class="feature-item-label">Persistence</p>
    <h3>SQLite in dev, PostgreSQL in prod</h3>
    <p>SQLite works with zero setup. Swap to Redis, PostgreSQL, or MySQL with one line. All backends support task history, requeue, idempotency, and scheduled task locking.</p>
  </div>

  <div class="feature-item">
    <p class="feature-item-label">Multi-instance</p>
    <h3>Scale out without coordination overhead</h3>
    <p>Requeue claiming is atomic, so only one instance picks up each pending task. Task history is shared across all nodes. Idempotency keys work cross-instance.</p>
  </div>

  <div class="feature-item">
    <p class="feature-item-label">Control</p>
    <h3>Run tasks exactly the way you need</h3>
    <p>Priority queues, eager dispatch, async concurrency semaphore, dedicated sync thread pool, and a process executor for CPU-bound work. Set defaults on the decorator and override per call.</p>
  </div>

  <div class="feature-item">
    <p class="feature-item-label">Scheduling</p>
    <h3>Interval and cron, distributed-safe</h3>
    <p>Register periodic tasks with <code>every=</code> or a cron expression. A distributed lock ensures only one instance fires each entry per interval.</p>
  </div>

  <div class="feature-item">
    <p class="feature-item-label">Security</p>
    <h3>Sensitive args, never in plain text</h3>
    <p>Fernet encryption for task args and kwargs at enqueue time. Never stored in the task store, the database, or any log file. One key, one config option.</p>
  </div>

  <div class="feature-item">
    <p class="feature-item-label">Adoption</p>
    <h3>Keep your existing route signatures</h3>
    <p>One line at startup hooks into FastAPI's injection. Your <code>background_tasks: BackgroundTasks</code> annotations stay exactly as they are. You get task IDs on every existing route with no other changes.</p>
  </div>

  </div>
</div>

<div class="note-section">
  <p class="section-label">Positioning</p>
  <p class="section-title">Not a Celery replacement</p>
  <p>fastapi-taskflow does not compete with Celery, ARQ, Taskiq, or Dramatiq. Those tools are built for distributed workers, message brokers, and high-throughput task routing across separate machines.</p>
  <p>This library is for teams using FastAPI's native <code>BackgroundTasks</code> who want retries, visibility, and resilience without adding worker infrastructure. It supports multi-instance deployments with a shared SQLite file (same host) or Redis, PostgreSQL, MySQL (any host), including atomic requeue claiming, idempotency keys, and shared task history across instances.</p>
  <p>CPU-bound tasks can run in a <code>ProcessPoolExecutor</code> via <code>executor='process'</code>, which bypasses the GIL and runs workers in separate OS processes. This covers most in-process CPU workloads. If your tasks need to run on separate machines entirely, use a proper task queue.</p>
  <p><a href="getting-started/installation/">Get started &rarr;</a></p>
</div>

<div class="note-section">
  <p class="section-label">Contributing</p>
  <p class="section-title">Get involved</p>
  <p>Bug reports, feature requests, and pull requests are welcome. For questions or direct feedback, see the contributing page.</p>
  <p><a href="contributing/">Contributing guide &rarr;</a></p>
</div>

</div>
