# Installation

This page covers everything you need to get fastapi-taskflow installed, including which optional extras to add depending on what you plan to use.

## Requirements

fastapi-taskflow requires **Python 3.10 or higher**.

## Basic install

If you are just getting started, the base package is all you need.

```bash
pip install fastapi-taskflow
```

This gives you the task manager, the in-memory and SQLite backends, the dashboard, and interval-based scheduling with `every=`. No extra dependencies required.

!!! tip "Not sure what you need?"
    Start with the base package. You can always add extras later as your needs grow. Most features work without any extras at all.

## Optional extras

Each extra unlocks a specific feature. Install only what you actually need.

| Extra | Install command | When you need it |
|-------|----------------|-----------------|
| `redis` | `pip install "fastapi-taskflow[redis]"` | You want to persist tasks in Redis instead of SQLite |
| `postgres` | `pip install "fastapi-taskflow[postgres]"` | You want to persist tasks in a PostgreSQL database |
| `mysql` | `pip install "fastapi-taskflow[mysql]"` | You want to persist tasks in MySQL or MariaDB |
| `scheduler` | `pip install "fastapi-taskflow[scheduler]"` | You want to schedule tasks with cron expressions like `cron="0 9 * * *"` |
| `encryption` | `pip install "fastapi-taskflow[encryption]"` | You want task arguments encrypted at rest |
| `process` | `pip install "fastapi-taskflow[process]"` | You want to run CPU-bound tasks in separate OS processes via `executor='process'` |

!!! note "Interval vs. cron schedules"
    Interval-based schedules using `every=` have no extra dependencies and work with the base install. Only `cron=`-style schedules require the `[scheduler]` extra. See [Scheduled Tasks](../guide/scheduled-tasks.md) for details.

!!! note "Process executor without the extra"
    `executor='process'` works without the `[process]` extra. Without cloudpickle, argument serialization falls back to standard `pickle`, which is stricter about what types it accepts. Install `[process]` to allow lambdas and closures as task arguments.

You can combine extras in a single install command:

```bash
pip install "fastapi-taskflow[redis,encryption]"
```

### Install everything at once

If you want every optional feature available without thinking about it, install the `all` extra:

```bash
pip install "fastapi-taskflow[all]"
```

This installs `redis[asyncio]`, `psycopg2-binary`, `PyMySQL`, `croniter`, `cryptography`, and `cloudpickle` together.

!!! warning "Production tip"
    In production, prefer installing only the extras you use. Installing `[all]` pulls in database drivers you may not need, which adds unnecessary weight to your deployment.

## Development install

If you want to contribute to fastapi-taskflow or run the test suite locally, clone the repo and install with the `dev` extra:

```bash
git clone https://github.com/Attakay78/fastapi-taskflow
cd fastapi-taskflow
pip install -e ".[dev]"
```

The `-e` flag installs the package in editable mode, so changes to the source are reflected immediately without reinstalling.
