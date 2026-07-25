<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CI/CD

GitHub Actions has two maintenance workflows:

- `.github/workflows/ci.yml` runs the Python, graph, SCIP, slow, and C++ parity
  test tiers.
- `.github/workflows/docs.yml` runs a lightweight strict documentation build for
  docs-only changes that the main CI intentionally ignores.

## Triggers

| Event | Notes |
|-------|-------|
| `push` to `main` / `master` | Subject to `paths-ignore` (see [Skip mechanisms](#skip-mechanisms)). |
| `pull_request` to any branch (`"*"`) | Subject to `paths-ignore`. Concurrency cancels older in-flight runs for the same PR head ref. |
| `schedule` — `cron: "0 7 * * *"` | 07:00 UTC daily. Forces a **full serial-chain run** so the `graph.pkl` cache and C++ parity never silently rot on light-only weeks. |
| `workflow_dispatch` | Manual run with a `skip_tests` boolean input. |

The separate docs workflow runs on pushes/PRs that touch Markdown, `docs/**`,
`mkdocs.yml`, `pyproject.toml`, or the docs workflow itself. It installs
`mkdocs-material` on `ubuntu-latest` and runs:

```bash
python -m mkdocs build --strict
```

This keeps docs-only PRs covered without forcing the self-hosted test runner to
run unit, integration, slow, or serial graph jobs for prose-only edits.

!!! note "Concurrency"
    The concurrency group is keyed by `github.head_ref || github.run_id`, and
    `cancel-in-progress` is `true` **only for `pull_request` events**. Pushes to
    `main`/`master` and scheduled runs always run to completion and never cancel
    each other.

## Jobs

The workflow is **7 jobs** wired into a dependency chain rooted at a `preflight`
decision job. All test jobs run on a **self-hosted runner**; `preflight` runs on
`ubuntu-latest`.

```
preflight ─┬─ unit ─┬─ integration ─ integration-serial ─┬─ scip-core
           │        │                                     └─ graph-consumer
           │        └─ (unit is also a dep of slow)
           └─ slow
```

| Job | `needs` | Runner | Marker / command | Timeout |
|-----|---------|--------|------------------|---------|
| **preflight** | — | `ubuntu-latest` | Decision job; no tests | — |
| **unit** | `preflight` | self-hosted | `not slow and not integration and not integration_serial and not integration_serial_consumer` | 20 min |
| **integration** | `preflight`, `unit` | self-hosted | `integration` (~2 min) | 30 min |
| **integration-serial** | `preflight`, `integration` | self-hosted | `integration_serial` (~25 min) | 45 min |
| **scip-core** | `preflight`, `integration-serial` | self-hosted | `test/scip/test_scip_core.py` (C++ decoder parity) | 30 min |
| **graph-consumer** | `preflight`, `integration-serial` | self-hosted | `integration_serial_consumer` (~5 min) | 15 min |
| **slow** | `preflight`, `unit` | self-hosted | `slow` (~15 min) | 60 min |

### preflight — the decision job

`preflight` runs once on `ubuntu-latest` and exposes two outputs every other job
gates on:

- **`should-run`** — `true` unless the run was explicitly skipped (see
  [Skip mechanisms](#skip-mechanisms)). Every test job is guarded by
  `if: needs.preflight.outputs.should-run == 'true'`.
- **`run-serial`** — whether the heavy serial chain
  (`integration-serial` → `scip-core` / `graph-consumer`) should run.

`run-serial` is computed **fail-closed** (defaults to `true`):

- `schedule` and `workflow_dispatch` events always force `run-serial=true`.
- For `push` / `pull_request`, it is only set to `false` when the path filter
  ran cleanly and found **no heavy files**.

The heavy/light filter uses `dorny/paths-filter@v3` with
`predicate-quantifier: "every"`. A file counts as **heavy** only if it matches
`**` *and* survives all of the following negations — i.e. it lies outside the
"light" set:

```yaml
heavy:
  - "**"
  - "!scripts/**"
  - "!examples/**"
  - "!docs/**"
  - "!**/*.md"
  - "!LICENSE"
  - "!.gitignore"
```

So a PR that touches only docs, scripts, examples, Markdown, `LICENSE`, or
`.gitignore` reports `heavy=false`, which makes `preflight` set
`run-serial=false` and the serial chain (`integration-serial`, `scip-core`,
`graph-consumer`) is skipped. `unit`, `integration`, and `slow` still run.

### unit

Pure logic with mocks only (~1 min). Sets up a Python 3.12 conda env
(`codenib-test`), preinstalls the configured CPU-only `torch` wheel, installs
`pip install -e ".[test]"`, then runs (verbatim):

```bash
pytest -n auto -m "not slow and not integration and not integration_serial and not integration_serial_consumer" -x --tb=short
```

This is the canonical definition of a **unit** test: anything not carrying one
of the four non-unit markers. Parallelized with `pytest-xdist` (`-n auto`) and
fails fast (`-x`).

The CPU `torch` preinstall keeps the non-GPU unit tier from resolving PyPI's
default CUDA wheels through `sentence-transformers`. Tests that require real
HuggingFace downloads, CUDA, or LLM credentials must be marked `slow` instead of
running in this tier.

### integration

Read-only, parallel-safe tests (~2 min): chunkers and fixture-based SCIP. This
tier runs with `pytest-xdist`, so tests that load HuggingFace embedding models,
consume GPU memory, or depend on LLM credentials do **not** belong here; mark
those `slow` instead. Uses the shared `./.github/actions/setup-env` composite
action (with `install-clangd` and `install-bear`), then runs:

```bash
pytest -n auto -m "integration" --tb=short
```

### integration-serial

Tests that **mutate shared repos** — SCIP indexing, `process_instance`,
`git checkout`/`apply` — so they must run sequentially (~25 min). The job
symlinks `$HOME/.codenib` to a persistent runner cache so SCIP outputs at
`~/.codenib/<instance_id>/` survive across runs and are visible to the
downstream `scip-core` and `graph-consumer` jobs. It runs:

```bash
pytest -m "integration_serial" -v --tb=short --timeout=900
```

!!! note "`--timeout=900` per-test guardrail"
    The slowest healthy serial test is ~7.5 min, so a 15-min (`900 s`) per-test
    cap leaves ~2x headroom while still failing a genuinely hung test fast
    (e.g. scip-python indexing of sympy has been observed hanging ~27 min)
    instead of letting it run out the 45-min job cap and hog the single
    self-hosted runner.

### scip-core

Builds the `core/` C++ pybind module and **parity-checks the C++ decoder against
the Python implementation** using the serial graphs persisted by
`integration-serial`. Gated on both `should-run` and `run-serial`. Key steps:

- Checks out with `submodules: recursive` (libigraph is vendored via
  `FetchContent` in `core/CMakeLists.txt`; only `re2` + `cmake` come from the
  system).
- Installs `pybind11`, caches `build/core`, then builds:

  ```bash
  cmake -S core -B build/core \
    -DCMAKE_BUILD_TYPE=Release \
    -DCODENIB_BUILD_PYBIND=ON \
    -DPython_EXECUTABLE="$(which python)"
  cmake --build build/core -j "$(nproc)"
  ```

- Runs the parity tests with `PYTHONPATH=build/core` and an `LD_PRELOAD` of the
  system `libstdc++.so.6` (to match the GCC that compiled the pybind `.so`):

  ```bash
  pytest test/scip/test_scip_core.py -v --tb=short
  ```

### graph-consumer

Consumes the `graph.pkl` written by `integration-serial` (specifically
`test_scip_multilingual`) and runs query / range / anchor checks against it via
`skip_level="graph"` (~5 min). Like `scip-core`, it depends on
`integration-serial` at the job level so the cached pickle is always fresh from
the most recent decoder run. Gated on both `should-run` and `run-serial`:

```bash
pytest -m "integration_serial_consumer" -v --tb=short
```

### slow

LLM API calls, HuggingFace downloads, and GPU embeddings (~15 min). Depends on
`preflight` and `unit` (not on the serial chain). This tier is intentionally not
xdist-parallelized because embedding model loads can exhaust shared GPU memory
when started by multiple workers. Sets up GCP credentials and
`VERTEXAI_PROJECT`, then:

```bash
pytest -m "slow" --tb=short
```

CUDA-specific tests should declare an explicit skip when the active `torch`
install is not CUDA-capable. This keeps slow LLM coverage available on CPU-only
runners while making GPU coverage opt-in to a runner/toolchain that actually
provides CUDA.

## Pytest markers

Defined in `pyproject.toml` under `[tool.pytest.ini_options].markers`:

| Marker | Meaning |
|--------|---------|
| `slow` | Needs LLM API, GPU, or HuggingFace downloads. |
| `integration` | Uses external repos but is read-only / parallel-safe (chunkers, fixture-based SCIP). |
| `integration_serial` | Mutates shared repos (`process_instance`, `git checkout`/`apply`) and must run sequentially. |
| `integration_serial_consumer` | Consumes `graph.pkl` written by `integration_serial`; runs in a separate job after `integration-serial` (mirrors `scip-core`). |

A **unit** test is simply one that carries *none* of the above markers — the CI
`unit` job selects them with
`not slow and not integration and not integration_serial and not integration_serial_consumer`.

### Running locally

```bash
# Unit tests only (matches the CI unit job)
pytest -n auto -m "not slow and not integration and not integration_serial and not integration_serial_consumer"

# Read-only integration tests
pytest -m "integration"

# Serial (repo-mutating) integration tests
pytest -m "integration_serial"

# Graph-cache consumer tests (require a graph.pkl from a prior serial run)
pytest -m "integration_serial_consumer"

# Slow tests (LLM + embeddings)
pytest -m "slow"

# Everything
pytest
```

## Skip mechanisms

There are two distinct ways CI work is skipped: **path-based** (the event never
fires, or the serial chain is gated off) and **explicit** (`should-run=false`).

### Path-based

- **`paths-ignore`** — `push`/`pull_request` events are not triggered at all when
  *every* changed file matches one of: `**.md`, `docs/**`, `LICENSE`,
  `.gitignore`.
- **Heavy/light serial gating** — even when CI does run, the heavy path filter
  may set `run-serial=false`, skipping `integration-serial`, `scip-core`, and
  `graph-consumer` for changes confined to `scripts/**`, `examples/**`,
  `docs/**`, `**/*.md`, `LICENSE`, or `.gitignore`. See
  [preflight](#preflight-the-decision-job).
- **Slow tier gating** — `slow` is skipped by default on PR and push events.
  It runs for scheduled full CI, `workflow_dispatch` with `test_tier=full`, or
  PRs labeled `full-ci` or `slow-ci`.

### Explicit (sets `should-run=false`)

`preflight` evaluates these and, when matched, sets `should-run=false`, skipping
**all** test jobs:

- **Commit message** (push): contains `[skip tests]`.
- **PR title** (pull_request): contains `[skip tests]`.
- **PR label** (pull_request): the `skip-tests` label is present.
- **Manual dispatch**: `workflow_dispatch` run with `skip_tests: true`.

## Shared environment setup

`integration`, `integration-serial`, `scip-core`, `graph-consumer`, and `slow`
use the `./.github/actions/setup-env` composite action, which provisions:

- **conda** env `codenib-test` (Python 3.12 by default) plus a separate
  `scip-env` from `codenib/scip_interface/scip-environment.yml`.
- **CPU torch preinstall** — enabled by default through
  `preinstall-cpu-torch`, `torch-version`, and `torch-index-url`, so non-GPU
  jobs do not accidentally download CUDA wheels through transitive embedding
  dependencies.
- **SCIP Python** — built from the `third_party/scip-python` submodule.
- **Rust** — stable + a pinned nightly toolchain with the `rust-analyzer`
  component (toggle `install-rust`).
- **scip-typescript + yarn** — installed via npm (toggle `install-scip-typescript`).
- **scip-go** — installed via `go install` inside `scip-env` (toggle `install-scip-go`).
- **clangd** — optional, via `install-clangd` (enabled for the integration /
  serial / core / consumer jobs).
- **bear** — optional C/C++ compilation-database tool, via `install-bear`.

## Failure triage

Two CI failure modes are easy to confuse:

- A self-hosted job that is `cancelled` with no steps and no `runner_name` did
  not fail tests. It sat in the self-hosted runner queue until GitHub cancelled
  it. Check runner availability before changing code.
- An `integration` failure with `torch.OutOfMemoryError` means a GPU/HuggingFace
  embedding test is in the wrong tier or is running concurrently with another
  GPU workload. Move that test to `slow` or make it use a mock/vector fixture.

## Pre-commit hooks

Pre-commit hooks run locally on every commit (`.pre-commit-config.yaml`):

| Hook | Scope |
|------|-------|
| trailing-whitespace, mixed-line-ending, end-of-file-fixer | All files |
| check-merge-conflict, requirements-txt-fixer, check-added-large-files | All files |
| check-json, check-yaml, check-toml | Config files |
| fix-encoding-pragma (`--remove`), debug-statements | Python |
| clang-format (`-style=file`) | C/C++ (`.c`, `.cc`, `.cpp`, `.cxx`, `.cu`, `.cuh`, `.h`, `.hh`, `.hpp`, `.hxx`) |
| black (`--line-length=88`) | Python |
| isort (`--profile black --filter-files`) | Python |
| flake8 + flake8-bugbear | Python |

## See also

- [MCP Server](mcp.md)
