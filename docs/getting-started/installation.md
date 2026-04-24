# Installation

## Requirements

- Python 3.10+

## Install

```bash
pip install fastapi-taskflow
```

## All optional dependencies

```bash
pip install "fastapi-taskflow[all]"
```

Installs `redis[asyncio]`, `croniter`, and `cryptography` in one go.

## Individual extras

| Extra | Installs | Required for |
|-------|----------|--------------|
| `redis` | `redis[asyncio]` | Redis persistence backend |
| `scheduler` | `croniter` | Cron-based scheduled tasks |
| `encryption` | `cryptography` | Argument encryption at rest |

```bash
pip install "fastapi-taskflow[redis]"
pip install "fastapi-taskflow[scheduler]"
pip install "fastapi-taskflow[encryption]"
```

Interval-based schedules (`every=`) have no extra dependencies. `cron=` requires `[scheduler]`. See [Scheduled Tasks](../guide/scheduled-tasks.md) and [Argument Encryption](../guide/encryption.md).

## Development install

```bash
git clone https://github.com/Attakay78/fastapi-taskflow
cd fastapi-taskflow
pip install -e ".[dev]"
```
