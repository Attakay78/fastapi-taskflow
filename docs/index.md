---
hide:
  - navigation
  - toc
---

<div class="hero">
  <h1>Turn FastAPI BackgroundTasks into a production-ready task system</h1>
  <p class="sub">Retries, control, and visibility without workers or brokers.</p>
  <div class="hero-actions">
    <a class="btn btn-primary" href="getting-started/installation/">Get Started</a>
    <a class="btn btn-secondary" href="getting-started/quickstart/">Quick Start</a>
  </div>
</div>

<div class="home-page">

<div class="problem-section">
  <div class="problem-header">
    <p class="section-label">The problem</p>
    <p class="section-title">BackgroundTasks is fine, until it isn't</p>
    <p class="problem-desc">FastAPI's <code>BackgroundTasks</code> is great for simple work. But in production you hit the same gaps fast, tasks fail silently, nothing is tracked, and restarts wipe all state.</p>
  </div>
  <div class="compare-grid">
    <div class="compare-col before">
      <h4>Without fastapi-taskflow</h4>
      <ul>
        <li>No retries on failure</li>
        <li>No task IDs or status</li>
        <li>No visibility into what ran</li>
        <li>No history after restart</li>
        <li>No metrics or duration</li>
      </ul>
    </div>
    <div class="compare-col">
      <h4>With fastapi-taskflow</h4>
      <ul>
        <li>Automatic retries with backoff</li>
        <li>UUID per task, full lifecycle</li>
        <li>Live dashboard over SSE</li>
        <li>SQLite or Redis persistence</li>
        <li>Success rate and duration metrics</li>
      </ul>
    </div>
  </div>
</div>

<div class="code-section">
  <p class="section-label">Quick look</p>
  <p class="section-title">Looks like FastAPI. Works like a task system.</p>

```python
from fastapi import BackgroundTasks, FastAPI
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager(snapshot_db="tasks.db")
app = FastAPI()
TaskAdmin(app, task_manager, auto_install=True)


@task_manager.task(retries=3, delay=1.0, backoff=2.0)
def send_email(address: str) -> None:
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
  <div class="dashboard-preview">
    <img src="assets/images/dashboard.png" alt="fastapi-taskflow live dashboard" class="dashboard-img" />
  </div>
</div>

<div class="section">
  <p class="section-label">Features</p>
  <p class="section-title">Everything you need, nothing you don't</p>
  <div class="feature-grid">

  <div class="feature-card">
    <span class="icon">↩</span>
    <h3>Automatic Retries</h3>
    <p>Configure retries, delay, and exponential backoff per function using a single decorator.</p>
  </div>

  <div class="feature-card">
    <span class="icon">◎</span>
    <h3>Task Lifecycle Tracking</h3>
    <p>Every task gets a UUID and moves through PENDING, RUNNING, SUCCESS, and FAILED states.</p>
  </div>

  <div class="feature-card">
    <span class="icon">▦</span>
    <h3>Live Dashboard</h3>
    <p>A real-time admin panel at <code>/tasks/dashboard</code> with filtering, search, and task detail.</p>
  </div>

  <div class="feature-card">
    <span class="icon">⊞</span>
    <h3>Pluggable Persistence</h3>
    <p>SQLite out of the box. Redis available as an optional extra. Custom backends via a simple ABC.</p>
  </div>

  <div class="feature-card">
    <span class="icon">⟳</span>
    <h3>Pending Requeue</h3>
    <p>Tasks that did not finish before shutdown are saved and re-dispatched automatically on next startup.</p>
  </div>

  <div class="feature-card">
    <span class="icon">⇌</span>
    <h3>Zero-Migration Injection</h3>
    <p>Keep your existing <code>background_tasks: BackgroundTasks</code> signatures. One line at startup is all it takes.</p>
  </div>

  </div>
</div>

<div class="note-section">
  <p class="section-label">Positioning</p>
  <p class="section-title">Not a Celery replacement</p>
  <p>fastapi-taskflow does not compete with Celery, ARQ, Taskiq, or Dramatiq. Those tools are built for distributed systems, message brokers, and multi-worker setups.</p>
  <p>This library is for teams already using FastAPI's native <code>BackgroundTasks</code> for lightweight in-process work who want retries, visibility, and persistence without adding infrastructure. If your tasks need to survive across multiple app instances or run on separate workers, use a proper task queue.</p>
  <p><a href="getting-started/installation/">Get started &rarr;</a></p>
</div>

</div>
