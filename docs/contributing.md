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

```bash
git clone https://github.com/Attakay78/fastapi-taskflow
cd fastapi-taskflow
pip install -e ".[dev]"
```

### Running tests

```bash
pytest
```

All tests must pass before a PR will be reviewed. If you are adding a feature, include tests that cover the new behaviour.

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
