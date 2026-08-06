<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CI/CD

GitHub Actions has seven workflow files:

- `.github/workflows/ci.yml` runs the Python, graph, SCIP, slow, and C++ parity
  test tiers.
- `.github/workflows/docs.yml` checks display branding and the generated
  language matrix, runs a strict documentation build, and verifies the
  public-document boundary for changes that the main CI intentionally ignores.
- `.github/workflows/auto-label.yml` ("Label PRs") applies the path-based
  `scope/*` / `type/*` label taxonomy to pull requests with `actions/labeler@v5`,
  driven by `.github/labeler.yml`.
- `.github/workflows/label-sync.yml` ("Sync labels") syncs the repository label
  set from `.github/labels.yml` with `EndBug/label-sync@v2` — on pushes to
  `main` that touch that file or the workflow file itself, or on manual
  dispatch (with an optional `delete_unused` input that removes labels absent
  from `labels.yml`).
- `.github/workflows/release-verify.yml` is the reusable distribution, Python
  compatibility, installed CLI, Wiki, and MCP verification pipeline.
- `.github/workflows/release-test.yml` manually verifies and publishes a
  candidate through the trusted TestPyPI environment.
- `.github/workflows/release.yml` invokes the same verification pipeline and
  publishes version tags through the trusted production PyPI environment
  before creating a GitHub Release. See [Releasing](releasing.md).

## Triggers

| Event | Notes |
|-------|-------|
| `push` to `main` / `master` | Subject to `paths-ignore` (see [Skip mechanisms](#skip-mechanisms)). |
| `pull_request` to any branch (`"*"`) | Subject to `paths-ignore`. Concurrency cancels older in-flight runs for the same PR head ref. |
| `schedule` — `cron: "0 7 * * *"` | 07:00 UTC daily. Forces a **full serial-chain run** so the `graph.pkl` cache and C++ parity never silently rot on light-only weeks. |
| `workflow_dispatch` | Manual run with `skip_tests`, a `test_tier` choice (`light` / `full`, default `full`), and an optional `cold_parser_cache` bootstrap check. |

The separate docs workflow runs on pushes/PRs that touch Markdown, `docs/**`,
`mkdocs.yml`, `pyproject.toml`, the `Makefile`, Python under `codenib/`, the
C++ core, the web package manifests, the public-docs and language-capability
checks, or the docs workflow itself. On the self-hosted runner it first runs
the display-branding guard:

```bash
python scripts/check_namespace.py
```

which rejects former product identifiers outside the allowlisted external
addresses (see [Naming](branding.md)) — so a docs-only PR can fail the Docs job
on a legacy-name occurrence before mkdocs even runs. It then installs
`mkdocs-material`, verifies that the checked-in capability matrix still matches
the language registry, and builds the site:

```bash
python scripts/language_capability_matrix.py \
  --check docs/language_capabilities.md
python -m mkdocs build --strict
```

After the build it runs:

```bash
python scripts/check_public_docs.py
```

That check verifies that internal plans, experiment outputs, and
publisher-only procedures are absent from generated files, search, and the
sitemap. This keeps docs-only PRs covered without running the unit,
integration, slow, or serial graph jobs for prose-only edits.

!!! note "Concurrency"
    The concurrency group is keyed by the workflow name and full Git ref.
    `cancel-in-progress` is always enabled, so a newer docs run for the same
    branch or tag supersedes the older one.

## Jobs

The main CI workflow is **7 jobs** wired into a dependency chain rooted at a
`preflight` decision job. Every job in `ci.yml`, including `preflight`, runs on
the **self-hosted runner**. The docs build and label workflows are also
self-hosted; only the public GitHub Pages deployment job uses
`ubuntu-latest`.

```
preflight ─ unit ─ integration ─ integration-serial ─┬─ scip-core ──────┐
                                                     └─ graph-consumer ─┴─ slow
```

`slow` lists the **entire chain** in `needs` and runs last; its `if` tolerates
*skipped* serial-chain jobs (see [slow](#slow)).

| Job | `needs` | Runner | Marker / command | Timeout |
|-----|---------|--------|------------------|---------|
| **preflight** | — | self-hosted | Decision job; no tests | — |
| **unit** | `preflight` | self-hosted | `not slow and not integration and not integration_serial and not integration_serial_consumer` | 20 min |
| **integration** | `preflight`, `unit` | self-hosted | `integration and not slow` | 30 min |
| **integration-serial** | `preflight`, `integration` | self-hosted | `integration_serial` | 45 min |
| **scip-core** | `preflight`, `integration-serial` | self-hosted | `test/scip/test_scip_core.py` (C++ decoder parity) | 30 min |
| **graph-consumer** | `preflight`, `integration-serial` | self-hosted | `integration_serial_consumer` | 15 min |
| **slow** | `preflight`, `unit`, `integration`, `integration-serial`, `scip-core`, `graph-consumer` | self-hosted | `slow` | 60 min |

### preflight — the decision job

`preflight` runs once on the self-hosted runner and exposes the outputs every
other job gates on:

- **`should-run`** — `true` unless the run was explicitly skipped (see
  [Skip mechanisms](#skip-mechanisms)). Every test job is guarded by
  `if: needs.preflight.outputs.should-run == 'true'`.
- **`run-serial`** — whether the heavy serial chain
  (`integration-serial` → `scip-core` / `graph-consumer`) should run.
- **`run-slow`** — whether the opt-in `slow` tier should run (see
  [Skip mechanisms](#skip-mechanisms)).

Both decisions also expose a human-readable `serial-reason` / `slow-reason`
output explaining the choice (echoed into the job log).

`run-serial` is computed conservatively: an unreadable or ambiguous diff runs
the chain. It is set to `true` when one of these holds:

- the event is a `schedule` run (the daily full run);
- the event is a `workflow_dispatch` with `test_tier=full` (the `light` tier
  leaves it `false`);
- the push / PR touches the **serial-chain path allowlist** below (classified
  by `scripts/classify_ci_changes.py`); or
- the PR carries the `full-ci` or `serial-ci` label.

The allowlist names the sources that can affect the expensive serial-chain
jobs — repo mutation, SCIP/LSP indexing, graph patching, dataset location, and
C++ decoder parity:

```yaml
serial:
  - ".github/workflows/ci.yml"
  - ".github/actions/prewarm-parsers/**"
  - ".github/actions/setup-env/**"
  - "Makefile"
  - "pyproject.toml"
  - "uv.lock"
  - "setup.py"
  - "third_party/**"
  - "core/**"
  - "codenib/code_chunking/**"
  - "codenib/dataset/**"
  - "codenib/graph/**"
  - "codenib/index/**"
  - "codenib/ls_index/**"
  - "codenib/ls_router.py"
  - "codenib/types.py"
  - "scripts/check_graph_route_alignment.py"
  - "scripts/classify_ci_changes.py"
  - "scripts/smoke_lsp_graph.py"
  - "scripts/smoke_scip_cold_start.py"
  - "scripts/swebench_graph_index.py"
  - "test/chunker/**"
  - "test/dataset/**"
  - "test/graph/**"
  - "test/index/**"
  - "test/ls_index/**"
  - "test/scip/**"
```

The classifier parses `pyproject.toml` and `uv.lock` rather than trusting file
names alone. A synchronized change to only the editable CodeNib package version
skips the serial chain; dependency, build, source, schema, test, malformed, or
unpaired metadata changes run it. Unit, integration, and release-artifact checks
still run for a release-only version change.

Changes outside this list — agent, runtime, model, retrieval, eval — use the
faster unit + integration tier by default; a maintainer opts a PR into the
serial chain with the `full-ci` or `serial-ci` label. When `run-serial` stays
`false`, the serial chain (`integration-serial`, `scip-core`, `graph-consumer`)
is skipped while `unit` and `integration` still run.

### unit

Pure logic with mocks only. Sets up a Python 3.12 conda env
(`codenib-test`), preinstalls the configured CPU-only `torch` wheel, installs
`pip install -e ".[test]"`, then runs (verbatim):

```bash
pytest -n auto -m "not slow and not integration and not integration_serial and not integration_serial_consumer" -x --tb=short --timeout=180 --durations=20
```

This is the canonical definition of a **unit** test: anything not carrying one
of the four non-unit markers. Parallelized with `pytest-xdist` (`-n auto`) and
fails fast (`-x`).

The CPU `torch` preinstall keeps the non-GPU unit tier from resolving PyPI's
default CUDA wheels through `sentence-transformers`. Tests that require real
HuggingFace downloads, CUDA, or LLM credentials must be marked `slow` instead of
running in this tier.

Unit prewarms all configured tree-sitter languages into a persistent
self-hosted-runner cache keyed by the pinned `tree-sitter-language-pack`
version and runner platform. Integration uses the same cache instead of
downloading the payload again. Each cold preload attempt has a process-level
timeout and two attempts with cache diagnostics. Scheduled CI uses a unique
run-scoped empty cache before integration reuses it; manual dispatch can select
the same path with `cold_parser_cache=true`. Integration removes that run-scoped
payload after its final use, failed unit runs clean it immediately, and later
cold runs prune abandoned directories older than seven days.

### integration

Read-only, parallel-safe tests: chunkers and fixture-based SCIP. This
tier runs with `pytest-xdist`, so tests that load HuggingFace embedding models,
consume GPU memory, or depend on LLM credentials do **not** belong here; mark
those `slow` instead. Uses the shared `./.github/actions/setup-env` composite
action (with `install-clangd` and `install-bear`), then runs:

```bash
pytest -n 4 -m "integration and not slow" --tb=short --timeout=600 --durations=20
```

### integration-serial

Tests that **mutate shared repos** — SCIP indexing, `process_instance`,
`git checkout`/`apply` — so they must run sequentially. The job
symlinks `$HOME/.codenib` to a persistent runner cache so SCIP outputs at
`~/.codenib/<instance_id>/` survive across runs and are visible to the
downstream `scip-core` and `graph-consumer` jobs. It runs:

```bash
pytest -m "integration_serial" -x -v --tb=short --timeout=900 --durations=20
```

!!! note "`--timeout=900` per-test guardrail"
    The 15-minute (`900 s`) per-test cap stops a hung test before the
    45-minute job timeout. It applies to each test, not to the whole command.

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
`skip_level="graph"`. Like `scip-core`, it depends on
`integration-serial` at the job level so the cached pickle is always fresh from
the most recent decoder run. Gated on both `should-run` and `run-serial`:

```bash
pytest -m "integration_serial_consumer" -v --tb=short
```

### slow

LLM API calls, HuggingFace downloads, and GPU embeddings. Runs
**last**: its `needs` lists the entire chain (`preflight`, `unit`,
`integration`, `integration-serial`, `scip-core`, `graph-consumer`) under an
`if: always() && ...` guard that requires `unit` and `integration` to have
**succeeded** and each serial-chain job to be **success or skipped**. A light
run (serial chain gated off) can therefore still reach `slow`, but any
serial-chain failure blocks it. The job itself only runs when `preflight` set
`run-slow=true` (see [Skip mechanisms](#skip-mechanisms)). This tier is
intentionally not xdist-parallelized because embedding model loads can exhaust
shared GPU memory when started by multiple workers. Sets up GCP credentials and
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

# Read-only integration tests (matches the CI integration job's marker expression)
pytest -m "integration and not slow"

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
- **Serial-chain path gating** — even when CI does run, the serial chain
  (`integration-serial`, `scip-core`, `graph-consumer`) is skipped unless the
  change touches the serial-chain path allowlist, the PR carries the `full-ci`
  or `serial-ci` label, the run is scheduled, or it is a `workflow_dispatch`
  with `test_tier=full`. See [preflight](#preflight-the-decision-job).
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

## Self-Hosted Runner Service

The runner must be installed as an operating-system service. Starting
`./run.sh` in a shell is useful only for diagnosis: the listener disappears
when that shell, SSH session, or machine restarts, leaving jobs queued even
though no test has failed.

On Linux, wait until GitHub reports the runner as idle, stop the foreground
`run.sh` process with `Ctrl-C`, and use the service helper shipped with that
runner installation:

```bash
cd /path/to/actions-runner
sudo ./svc.sh install "$(id -un)"
sudo ./svc.sh start
sudo ./svc.sh status
```

`svc.sh install` creates and enables an `actions.runner.*.service` systemd unit,
so the listener starts after a reboot without an interactive login. Confirm
both the local unit and GitHub registration before dispatching expensive jobs:

```bash
systemctl list-units 'actions.runner.*' --all
gh api repos/sysevol-ai/CodeNib/actions/runners \
  --jq '.runners[] | {name, status, busy}'
```

Do not start the service while a manual listener or `Runner.Worker` is still
active. Do not stop a busy runner merely to install the service; wait for its
current job to finish first.

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
| codenib-namespace (local hook: `python scripts/check_namespace.py`) | Whole repo (`pass_filenames: false`); rejects former product identifiers — the same guard the Docs workflow runs |

## See also

- [MCP Server](mcp.md)
