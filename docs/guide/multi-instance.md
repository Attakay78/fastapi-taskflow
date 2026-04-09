# Multi-Instance Deployments

fastapi-taskflow supports running multiple instances of your application behind a load balancer. The level of coordination available depends on which backend you use.

## Same host, multiple processes

Use SQLite when all instances share the same host (for example, multiple Uvicorn workers started by Gunicorn).

```python
task_manager = TaskManager(
    snapshot_db="/var/app/tasks.db",
    requeue_pending=True,
)
```

All processes must point to the same file path. WAL journal mode is enabled automatically so concurrent reads and writes do not conflict.

Atomic requeue claiming ensures only one process picks up each pending task on startup.

This does not work across separate machines. Each machine would have its own file.

## Multiple hosts (Redis)

Use Redis when instances run on separate machines.

```bash
pip install "fastapi-taskflow[redis]"
```

```python
from fastapi_taskflow.backends import RedisBackend

task_manager = TaskManager(
    snapshot_backend=RedisBackend("redis://your-redis-host:6379/0"),
    requeue_pending=True,
)
```

All instances share the same Redis instance. The following features work across hosts:

- Atomic requeue claiming: only one instance picks up each pending task on restart
- Idempotency keys: cross-instance deduplication using Redis `SET NX`
- Completed task history: all instances flush to the same Redis keys
- Dashboard history view: completed tasks from all instances visible with a short cache window

## Idempotency keys

Pass an `idempotency_key` to `add_task()` to prevent the same logical operation from running twice, even if two separate instances receive the same request simultaneously.

```python
task_id = tasks.add_task(
    notify_order,
    order_id,
    idempotency_key=f"order-{order_id}-notified",
)
```

If an instance has already completed this operation, the original `task_id` is returned and the task is not enqueued again.

!!! note
    Idempotency key cross-instance dedup only works when a shared backend is configured. Without a backend, dedup is in-process only.

## Dashboard in multi-instance deployments

The dashboard SSE stream is connected to a single instance. That instance shows its own live tasks (PENDING, RUNNING) and completed tasks from all instances via the shared backend.

The poll interval controls how often the stream refreshes from the backend to pick up other instances' completed tasks:

```python
TaskAdmin(app, task_manager, poll_interval=5.0)
```

Live PENDING and RUNNING tasks from other instances are not visible. Each instance only holds its own in-memory state.

To get a consistent view of live tasks, route all dashboard traffic to a single instance using sticky sessions at the load balancer.

## Sticky sessions for the dashboard

### Nginx

Route all `/tasks/` traffic to a dedicated instance while the rest of the application is load balanced normally.

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
        # The default is 60s which will drop the stream between keep-alive pings.
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

If you cannot dedicate a fixed instance to the dashboard, `ip_hash` routes the same client IP to the same upstream on every request.

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
    `ip_hash` has two drawbacks. First, it does not rebalance well when instances are added or removed. Second, users behind a corporate NAT or shared proxy all share the same source IP and will all land on the same instance, which defeats the load balancing for those users.

### AWS Application Load Balancer

Enable stickiness on the target group so the ALB routes the same client to the same instance on every request.

1. Open the EC2 console and go to **Target Groups**.
2. Select your target group.
3. On the **Attributes** tab, click **Edit attributes**.
4. Under **Stickiness**, enable it and set the type to **Load balancer generated cookie**.
5. Set a stickiness duration appropriate for your use case (for example, 1 day).
6. Save changes.

The ALB will set an `AWSALB` cookie on the first response. Subsequent requests from the same browser carrying that cookie are routed to the same target.

!!! note
    ALB stickiness applies at the target group level. Both the initial page load request and the SSE stream connection need to reach the same instance for the dashboard to work correctly. Because browsers open the SSE connection from the same session, the stickiness cookie covers both.

### Traefik

Define a service with sticky cookies and a router that directs dashboard traffic to it. The example below uses Traefik v2 dynamic configuration in YAML format.

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

If you are using Docker labels instead of a file provider, the equivalent is:

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
| Live PENDING/RUNNING from other instances | Not visible. Each instance holds its own in-memory state. |
| SQLite across separate hosts | Not supported. Use Redis for multi-host deployments. |
| Hard crash recovery | Tasks running at the time of a SIGKILL or OOM kill cannot be recovered. Only clean shutdowns write the pending store. |
| Dashboard real-time cross-instance events | The dashboard polls the shared backend on a configurable interval (default 30s). It does not receive push notifications from other instances. |
