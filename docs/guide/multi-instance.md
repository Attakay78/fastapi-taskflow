# Multi-Instance Deployments

This page covers the two main ways to run fastapi-taskflow with multiple instances, how task history is shared between them, and how to configure your load balancer so the dashboard works correctly.

## Two deployment topologies

How you configure fastapi-taskflow depends on where your instances run.

**Same host, multiple processes** applies when you run Gunicorn with multiple Uvicorn workers on a single machine. All processes share the same filesystem, so SQLite is a practical choice.

**Multiple hosts** applies when instances run on separate machines (different VMs, containers in a Kubernetes cluster, etc.). Each machine has its own filesystem, so you need a shared network backend like Redis.

## Same host with SQLite

Point every worker at the same database file:

```python hl_lines="4 5"
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

```python hl_lines="5 6"
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

Live tasks (PENDING, RUNNING) are held in each instance's in-memory store. They are not pushed to the shared backend until they complete, so other instances cannot see them in real time.

## Dashboard behaviour in multi-instance deployments

The dashboard SSE stream connects to a single instance. That instance shows:

- Its own live tasks (PENDING, RUNNING) from local memory.
- Completed tasks from all instances, loaded from the shared backend.

The poll interval controls how often the stream refreshes from the backend to pick up other instances' completed tasks:

```python
from fastapi_taskflow import TaskAdmin, TaskManager

task_manager = TaskManager(
    snapshot_backend=RedisBackend("redis://your-redis-host:6379/0"),
)
app = FastAPI()

TaskAdmin(app, task_manager, poll_interval=5.0)
```

Live PENDING and RUNNING tasks from other instances are not visible. Each instance only holds its own in-memory state.

To get a consistent view of live tasks, route all dashboard traffic to a single instance using sticky sessions at the load balancer.

## Why sticky sessions are needed

The dashboard uses Server-Sent Events (SSE), which is a persistent HTTP connection. Once the browser opens an SSE connection to an instance, it stays connected to that instance for the lifetime of the page.

If the load balancer routes the initial page load to instance A but then routes the SSE connection to instance B, the dashboard will show instance B's tasks, which may be different from what the page expects.

Sticky sessions fix this by ensuring both the page load and the SSE connection always reach the same instance.

## Sticky session configuration

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
        # The default 60s will drop the stream between keep-alive pings.
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

        # SSE settings
        proxy_buffering off;
        proxy_set_header X-Accel-Buffering no;
        proxy_read_timeout 3600s;
    }
}
```

!!! warning
    `ip_hash` does not rebalance well when instances are added or removed. Users behind a corporate NAT or shared proxy all share the same source IP, so they all land on the same instance, which defeats the load balancing for those users.

### AWS Application Load Balancer

Enable stickiness on the target group so the ALB routes the same client to the same instance on every request:

1. Open the EC2 console and go to **Target Groups**.
2. Select your target group.
3. On the **Attributes** tab, click **Edit attributes**.
4. Under **Stickiness**, enable it and set the type to **Load balancer generated cookie**.
5. Set a stickiness duration appropriate for your use case (for example, 1 day).
6. Save changes.

The ALB sets an `AWSALB` cookie on the first response. Subsequent requests from the same browser carrying that cookie are routed to the same target.

!!! note
    ALB stickiness applies at the target group level. Both the initial page load request and the SSE stream connection need to reach the same instance for the dashboard to work correctly. Because browsers open the SSE connection from the same session, the stickiness cookie covers both.

### Traefik

Define a service with sticky cookies and a router that directs dashboard traffic to it. The example below uses Traefik v2 dynamic configuration in YAML:

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

The `httpOnly: true` flag prevents client-side JavaScript from reading the cookie.

If you are using Docker labels instead of a file provider:

```yaml
# docker-compose.yml (excerpt)
services:
  app:
    labels:
      - "traefik.http.services.app.loadbalancer.sticky.cookie=true"
      - "traefik.http.services.app.loadbalancer.sticky.cookie.name=lb_sticky"
      - "traefik.http.services.app.loadbalancer.sticky.cookie.httponly=true"
```

## Known limitations

| Limitation | Notes |
|---|---|
| Live PENDING/RUNNING from other instances | Not visible. Each instance holds its own in-memory state only. |
| SQLite across separate hosts | Not supported. Use Redis for multi-host deployments. |
| Hard crash recovery | Tasks running at the time of a SIGKILL or OOM kill cannot be recovered. Only clean shutdowns write the pending store. |
| Dashboard real-time cross-instance events | The dashboard polls the shared backend on a configurable interval (default 30 s). It does not receive push notifications from other instances. |

## See also

- [File Logging](file-logging.md)
- [Setting Up TaskAdmin](task-admin.md)
- [TaskManager reference](../api/task-manager.md)
