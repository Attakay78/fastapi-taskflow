# Contributing

Contributions are welcome. This page covers how to report issues, suggest features, and submit pull requests.

## Reporting issues

Open an issue on [GitHub](https://github.com/Attakay78/fastapi-taskflow/issues) if you:

- Found a bug
- Hit unexpected behaviour that is not documented
- Want to request a feature

Include a minimal reproducible example where possible. The more specific the report, the faster it gets resolved.

## Pull requests

Before opening a pull request for a non-trivial change, open an issue first to discuss the approach. This avoids effort on work that may not align with the direction of the project.

For smaller changes like typo fixes, documentation improvements, or obvious bug fixes, a PR without prior discussion is fine.

### Local setup

The project uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
git clone https://github.com/Attakay78/fastapi-taskflow
cd fastapi-taskflow
uv sync
```

PRs should target the `develop` branch, not `main`.

### Checks to run before opening a PR

Run all of the following and make sure they pass cleanly.

**Lint**

```bash
uv run ruff check .
```

**Format**

```bash
uv run ruff format --check .
```

To auto-fix formatting:

```bash
uv run ruff format .
```

**Type checking**

```bash
uv run mypy fastapi_taskflow
```

**Tests**

```bash
uv run pytest
```

All tests must pass. If you are adding a feature, include tests that cover the new behaviour.

### Docs and README

If your change affects user-facing behaviour, update the relevant page under `docs/` or `README.md` before opening the PR.

To preview the docs site locally:

```bash
uv run mkdocs serve
```

Then open [http://127.0.0.1:8000/fastapi-taskflow](http://127.0.0.1:8000/fastapi-taskflow) in your browser. The server reloads automatically as you edit files under `docs/` or `mkdocs.yml`.

To do a one-off build without serving:

```bash
uv run mkdocs build
```

The output goes to `site/`. That directory is gitignored and does not need to be committed.

### Code style

- Follow the existing code style in the file you are editing
- No commented-out code
- No print statements in library code
- Type hints on all public functions

---

## Contact

**Quaicoe Richard (Attakay)**

For questions or feedback about the project that do not fit a GitHub issue, reach out directly:

- Email: [richardquaicoe78@gmail.com](mailto:richardquaicoe78@gmail.com)
- LinkedIn: [linkedin.com/in/richard-quaicoe-545ba211b](https://www.linkedin.com/in/richard-quaicoe-545ba211b/)
- X: [@richman_khay](https://x.com/richman_khay)
