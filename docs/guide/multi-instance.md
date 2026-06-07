# Multi-Instance Deployments

This page covers the two main ways to run fastapi-taskflow with multiple instances, how task history and live task state are shared between them, and how to configure your load balancer so the dashboard works correctly.

## Two deployment topologies

How you configure fastapi-taskflow depends on where your instances run.

**Same host, multiple processes** applies when you run Gunicorn with multiple Uvicorn workers on a single machine. All processes share the same filesystem, so SQLite is a practical choice.

**Multiple hosts** applies when instances run on separate machines (different VMs, containers in a Kubernetes cluster, etc.). Each machine has its own filesystem, so you need a shared network backend like Redis.

## Same host with SQLite

Point every worker at the same database file:

```python
from fastapi_taskflow import TaskManager

task_manager = TaskManager(
    snapshot_db="/var/app/tasks.db",
    requeue_pending=True,
)
```

WAL journal mode is enabled automatically so concurrent reads and writes do not conflict. Atomic requeue claiming ensures only one process picks up each pending task on startup.

!!! warning
    SQLite only works when all instances share the same host filesystem. If you deploy to separate machines, each instance would have its own separate database file and the instances would not see each other's tasks.

## Multiple hosts with Redis

Install the Redis extra and point every instance at a shared Redis server:

```bash
pip install "fastapi-taskflow[redis]"
```

```python
from fastapi_taskflow import TaskManager
from fastapi_taskflow.backends import RedisBackend

task_manager = TaskManager(
    snapshot_backend=RedisBackend("redis://your-redis-host:6379/0"),
    requeue_pending=True,
)
```

All instances share the same Redis backend. The following features work across hosts:

- **Atomic requeue claiming**: only one instance picks up each pending task on restart.
- **Idempotency keys**: cross-instance deduplication using Redis `SET NX`.
- **Completed task history**: all instances flush finished tasks to the same Redis keys.
- **Dashboard history view**: completed tasks from all instances are visible with a short cache window.

## How task history is shared

Completed tasks (SUCCESS, FAILED, INTERRUPTED) are flushed to the shared backend. Any instance can read them back and show them in its dashboard history view.

Live tasks (PENDING, RUNNING) are held in each instance's in-memory store. Without instance registration, they are not pushed to the shared backend until they complete, so other instances cannot see them in real time.

## Instance registration and fan-out

When you set `instance_url` on `TaskManager`, each instance registers itself in the shared backend under a metadata key. The dashboard can then fan out to all registered peers and return a unified view of live tasks from every instance.

```python
from fastapi_taskflow import TaskManager
from fastapi_taskflow.backends import RedisBackend

task_manager = TaskManager(
    snapshot_backend=RedisBackend("redis://redis:6379/0"),
    instance_url="http://10.0.0.1:8000",
)
```

When `instance_url` is set:

1. This instance writes its URL and a heartbeat timestamp to the shared backend registry at startup.
2. The heartbeat refreshes every `registry_heartbeat` seconds (default 30).
3. When `merged_list()` runs (on every dashboard load and SSE tick), it reads the registry, finds live peers, and fans out to each peer's `/__peer/tasks` endpoint concurrently.
4. Peer tasks are merged with this instance's local tasks and the backend snapshot. Local in-memory tasks always win on conflict; peer tasks override the backend snapshot for the same task ID.
5. At shutdown, this instance removes itself from the registry.

Peers that are unreachable or return an error are skipped without affecting the response. An instance is excluded from the peer list when its last heartbeat is older than `registry_ttl` seconds (default 90).

### Same-machine multiple instances

This works for separate processes on the same machine, each listening on a different port:

```python
import os

task_manager = TaskManager(
    snapshot_backend=RedisBackend("redis://localhost:6379/0"),
    instance_url=os.environ["INSTANCE_URL"],  # e.g. "http://localhost:8001"
)
```

```bash
INSTANCE_URL=http://localhost:8001 uvicorn app:app --port 8001
INSTANCE_URL=http://localhost:8002 uvicorn app:app --port 8002
```

The dashboard on either instance shows live tasks from both.

### Tasks prefix

If your tasks router is mounted at a non-root prefix (for example `/api/tasks`), set `instance_tasks_prefix` so peers build the correct fan-out URL:

```python
task_manager = TaskManager(
    snapshot_backend=RedisBackend("redis://redis:6379/0"),
    instance_url="http://10.0.0.1:8000",
    instance_tasks_prefix="/api/tasks",
)
```

The fan-out URL becomes `http://10.0.0.1:8000/api/tasks/__peer/tasks`. Leave `instance_tasks_prefix` at its default (`""`) when the tasks router is mounted at root.

### Registration parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `instance_url` | `None` | Public base URL of this instance. No registration or fan-out occurs when `None`. |
| `instance_tasks_prefix` | `""` | URL prefix where the tasks router is mounted on this instance. |
| `registry_ttl` | `90` | Seconds after which a peer entry is considered stale. Should be at least `2 * registry_heartbeat`. |
| `registry_heartbeat` | `30` | Seconds between heartbeat writes that keep this instance's entry fresh. |

## Dashboard behaviour in multi-instance deployments

Without `instance_url`, the dashboard SSE stream connects to a single instance. That instance shows:

- Its own live tasks (PENDING, RUNNING) from local memory.
- Completed tasks from all instances, loaded from the shared backend.

With `instance_url` configured, the dashboard additionally shows:

- Live PENDING and RUNNING tasks from all registered peer instances, fetched in real time on each SSE tick.

!!! note
    The `/__peer/tasks` endpoint returns only local in-memory tasks, not a merged view. This prevents circular fan-out chains where peers call each other recursively.

## Dashboard and load balancers

The dashboard uses Server-Sent Events (SSE), which is a persistent HTTP connection. To avoid split views, route all dashboard traffic to the same instance, either by pinning it to a single upstream or using sticky sessions.

### Nginx: dedicated instance for the dashboard

Route all `/tasks/` traffic to a fixed instance while the rest of the application is load balanced normally:

```nginx
upstream app {
    server instance-a:8000;
    server instance-b:8000;
    server instance-c:8000;
}

server {
    listen 80;

    # Dashboard always goes to instance-a
    location /tasks/ {
        proxy_pass http://instance-a:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;

        # Required for SSE: disable buffering so events are pushed immediately
        proxy_buffering off;
        proxy_cache off;
        proxy_set_header X-Accel-Buffering no;

        # Increase timeout so Nginx does not close the SSE connection early.
        proxy_read_timeout 3600s;
    }

    # All other traffic is load balanced
    location / {
        proxy_pass http://app;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

When `instance_url` is configured on all instances, the pinned instance fans out to its peers and shows their live tasks too. You get a unified view without routing all traffic through one instance.

### Nginx with ip_hash

If you cannot dedicate a fixed instance to the dashboard, `ip_hash` routes the same client IP to the same upstream on every request:

```nginx
upstream app {
    ip_hash;
    server instance-a:8000;
    server instance-b:8000;
    server instance-c:8000;
}

server {
    listen 80;

    location / {
        proxy_pass http://app;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;

        proxy_buffering off;
        proxy_set_header X-Accel-Buffering no;
        proxy_read_timeout 3600s;
    }
}
```

!!! warning
    `ip_hash` does not rebalance well when instances are added or removed. Users behind a corporate NAT or shared proxy all share the same source IP, so they all land on the same instance.

### AWS Application Load Balancer

Enable stickiness on the target group so the ALB routes the same client to the same instance on every request:

1. Open the EC2 console and go to **Target Groups**.
2. Select your target group.
3. On the **Attributes** tab, click **Edit attributes**.
4. Under **Stickiness**, enable it and set the type to **Load balancer generated cookie**.
5. Set a stickiness duration appropriate for your use case (for example, 1 day).
6. Save changes.

The ALB sets an `AWSALB` cookie on the first response. Subsequent requests from the same browser carrying that cookie are routed to the same target.

### Traefik

Define a service with sticky cookies and a router that directs dashboard traffic to it:

```yaml
http:
  routers:
    dashboard:
      rule: "PathPrefix(`/tasks/`)"
      service: app
      entryPoints:
        - web

  services:
    app:
      loadBalancer:
        sticky:
          cookie:
            name: lb_sticky
            httpOnly: true
        servers:
          - url: "http://instance-a:8000"
          - url: "http://instance-b:8000"
          - url: "http://instance-c:8000"
```

## Gunicorn workers

Gunicorn workers share a single port. The master process binds the port and distributes connections across workers. Workers are not individually addressable from the network, so `instance_url`-based fan-out does not apply to them.

For gunicorn deployments:

- Use a shared backend (Redis, Postgres) for cross-worker task history.
- Completed tasks from all workers are visible via the dashboard once flushed.
- Live PENDING and RUNNING tasks from other workers are not visible in real time.

If live cross-worker visibility is a requirement, run with `--workers 1` and scale horizontally across machines using `instance_url` instead.

## Known limitations

| Limitation | Notes |
|---|---|
| Gunicorn multi-worker live tasks | Workers share a port and cannot be individually addressed. Only completed tasks are shared via the backend. |
| SQLite across separate hosts | Not supported. Use Redis for multi-host deployments. |
| Hard crash recovery | Tasks running at the time of a SIGKILL or OOM kill cannot be recovered. Only clean shutdowns write the pending store. |
| Fan-out peer timeout | Each peer fan-out request times out after 5 seconds. Slow or unreachable peers are skipped and do not block the dashboard. |

## See also

- [File Logging](file-logging.md)
- [Setting Up TaskAdmin](task-admin.md)
- [TaskManager reference](../api/task-manager.md)
