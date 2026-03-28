# CI/CD

GitHub Actions pipeline that runs on every push to `main` and on all pull requests.

## Workflow Overview

The CI workflow (`.github/workflows/ci.yml`) splits tests into **three parallel jobs** based on pytest markers:

| Job | Marker filter | What it runs | Runtime |
|-----|--------------|--------------|---------|
| **unit** | `not slow and not integration` | Pure logic, mocks only | ~1 min |
| **integration** | `integration` | Repo cloning, SCIP indexing, chunkers | ~15 min |
| **slow** | `slow` | LLM API calls, GPU embeddings | ~15 min |

All three jobs run on a **self-hosted runner** with conda (Python 3.12).

## Pytest Markers

Defined in `pyproject.toml`:

```python
@pytest.mark.slow          # LLM API, GPU, or HuggingFace downloads
@pytest.mark.integration   # Clone repos, build SCIP indexes, chunker tests
# (no marker)              # Unit tests — fast, no external deps
```

### Running locally

```bash
# Unit tests only (default CI behavior)
pytest -m "not slow and not integration"

# Integration tests only
pytest -m "integration"

# Slow tests only
pytest -m "slow"

# Everything
pytest
```

## Skipping CI

You can skip all test jobs via any of these:

- **Commit message**: include `[skip tests]`
- **PR title**: include `[skip tests]`
- **PR label**: add the `skip-tests` label
- **Manual dispatch**: set `skip_tests: true` in the workflow_dispatch UI

## Pre-commit Hooks

Pre-commit hooks run locally on every commit (`.pre-commit-config.yaml`):

| Hook | Scope |
|------|-------|
| trailing-whitespace, mixed-line-ending, end-of-file-fixer | All files |
| check-merge-conflict, check-added-large-files | All files |
| check-json, check-yaml, check-toml | Config files |
| fix-encoding-pragma, debug-statements | Python |
| clang-format | C/C++ (`.c`, `.cpp`, `.h`, etc.) |
| black | Python (line-length 88) |
| isort | Python (black profile) |
| flake8 + flake8-bugbear | Python |

## Integration Job Toolchain

The integration and slow jobs install additional toolchains:

- **SCIP Python**: built from `third_party/scip-python` submodule into a separate `scip-env` conda environment
- **Rust**: stable + nightly toolchains, rust-analyzer (nightly component)
- **clangd**: for C/C++ language server features
- **scip-typescript + yarn**: for TypeScript/JS SCIP indexing
- **bear**: for generating C/C++ compilation databases
