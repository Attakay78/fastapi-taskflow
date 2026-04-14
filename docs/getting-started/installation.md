# Installation

## Requirements

- Python 3.10+

## Install

```bash
pip install fastapi-taskflow
```

## Optional: Redis backend

```bash
pip install "fastapi-taskflow[redis]"
```

## Optional: Argument encryption

```bash
pip install "fastapi-taskflow[encryption]"
```

Required when using `encrypt_args_key` on `TaskManager` to encrypt task args and kwargs at rest. See [Argument Encryption](../guide/encryption.md).

## Development install

```bash
git clone https://github.com/Attakay78/fastapi-taskflow
cd fastapi-taskflow
pip install -e ".[dev]"
```
