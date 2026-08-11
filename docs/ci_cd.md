<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CI/CD

GitHub Actions has ten workflow files:

- `.github/workflows/ci.yml` runs the pull-request unit gate on an ephemeral
  GitHub-hosted runner.
- `.github/workflows/ci-full.yml` runs the trusted Python, graph, SCIP, slow,
  and C++ parity tiers after pushes to the default branch, on the daily
  schedule, or by manual dispatch.
- `.github/workflows/docs.yml` checks display branding and the generated
  language matrix, runs a strict documentation build, and verifies the
  public-document boundary for changes that the main CI intentionally ignores.
- `.github/workflows/auto-label.yml` ("Label PRs") applies the path-based
  `scope/*` / `type/*` label taxonomy to pull requests with `actions/labeler@v5`,
  driven by `.github/labeler.yml`. It uses `pull_request_target` solely for
  metadata writes and never checks out or executes pull-request content.
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
- `.github/workflows/codenib-pages.yml` is the reusable Pages and context
  artifact workflow for downstream repositories.
- `.github/workflows/codenib-publish-smoke.yml` exercises the local publish
  action on relevant pull requests and default-branch pushes.

## Triggers

| Event | Notes |
|-------|-------|
| `pull_request` to any branch (`"*"`) | `ci.yml` runs the hosted unit gate. Drafts defer it until ready for review unless labeled `full-ci`; concurrency cancels an older run for the same PR head ref. |
| `push` to `main` / `master` | `ci-full.yml` runs the trusted full workflow, subject to `paths-ignore` and serial-path classification. |
| `schedule` — `cron: "0 7 * * *"` | 07:00 UTC daily. Forces a **full serial-chain run** so the `graph.pkl` cache and C++ parity never silently rot on light-only weeks. |
| `workflow_dispatch` | Runs `ci-full.yml` with `skip_tests`, a `test_tier` choice (`light` / `full`, default `full`), and an optional `cold_parser_cache` bootstrap check. |

The separate docs workflow runs on pushes/PRs that touch Markdown, `docs/**`,
`mkdocs.yml`, `pyproject.toml`, the `Makefile`, Python under `codenib/`, the
C++ core, the web package manifests, the public-docs and language-capability
checks, or the docs workflow itself. On a GitHub-hosted runner it first runs
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
    Pull requests cancel an older docs run for the same ref; main and manual
    publication runs always complete.

## Jobs

Pull requests run one hosted `unit` job from `ci.yml`. The trusted
`ci-full.yml` workflow has **7 jobs** wired into a dependency chain rooted at a
hosted `preflight` decision job; its six test jobs use the persistent
self-hosted runner for toolchain and graph-cache reuse. Documentation,
labeling, packaging, release, and publication workflows use ephemeral
GitHub-hosted runners.

```
preflight ─ unit ─ integration ─ integration-serial ─┬─ scip-core ──────┐
                                                     └─ graph-consumer ─┴─ slow
```

`slow` lists the **entire chain** in `needs` and runs last; its `if` tolerates
*skipped* serial-chain jobs (see [slow](#slow)).

| Job | `needs` | Runner | Marker / command | Timeout |
|-----|---------|--------|------------------|---------|
| **preflight** | — | ubuntu-latest | Decision job; no tests | — |
| **unit** | `preflight` | self-hosted | `not slow and not integration and not integration_serial and not integration_serial_consumer` | 20 min |
| **integration** | `preflight`, `unit` | self-hosted | `integration and not slow` | 30 min |
| **integration-serial** | `preflight`, `integration` | self-hosted | `integration_serial` | 45 min |
| **scip-core** | `preflight`, `integration-serial` | self-hosted | `make core-test` (C++ executables plus SCIP/Fact/clangd parity) | 30 min |
| **graph-consumer** | `preflight`, `integration-serial` | self-hosted | `integration_serial_consumer` | 15 min |
| **slow** | `preflight`, `unit`, `integration`, `integration-serial`, `scip-core`, `graph-consumer` | self-hosted | `slow` | 60 min |

### preflight — the decision job

`preflight` runs once on a GitHub-hosted runner and exposes the outputs every
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
- the default-branch push touches the **serial-chain path allowlist** below
  (classified by `scripts/classify_ci_changes.py`).

The allowlist names the sources that can affect the expensive serial-chain
jobs — repo mutation, SCIP/LSP indexing, graph patching, dataset location, and
C++ decoder parity:

```yaml
serial:
  - ".github/workflows/ci.yml"
  - ".github/workflows/ci-full.yml"
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
faster unit + integration tier on the post-merge push. When `run-serial` stays
`false`, the serial chain (`integration-serial`, `scip-core`, `graph-consumer`)
is skipped while `unit` and `integration` still run. Maintainers can run the
entire chain before merge locally or through a reviewed manual dispatch; the
checked-in PR workflow does not route pull-request code to the persistent
runner.

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

Unit prewarms all configured tree-sitter languages. PR runs use an ephemeral
hosted workspace; trusted full runs reuse a persistent self-hosted-runner cache
keyed by the pinned `tree-sitter-language-pack` version and runner platform.
Integration reuses that full-run cache instead of downloading the payload
again. Each cold preload attempt has a process-level timeout and two attempts
with cache diagnostics. Scheduled CI uses a unique run-scoped empty cache
before integration reuses it; manual dispatch can select the same path with
`cold_parser_cache=true`. Integration removes that run-scoped payload after its
final use, failed unit runs clean it immediately, and later cold runs prune
abandoned directories older than seven days.

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

Builds the `core/` C++ pybind module and runs the maintained C++, SCIP, Fact,
and clangd gate, including decoder parity against the serial graphs persisted
by `integration-serial`. Gated on both `should-run` and `run-serial`. Key
steps:

- Checks out with `submodules: recursive` (libigraph is vendored via
  `FetchContent` in `core/CMakeLists.txt`; `re2`, zlib, and build tools come
  from the system).
- Installs `pybind11`, caches `build/core`, then builds:

  ```bash
  cmake -S core -B build/core \
    -DCMAKE_BUILD_TYPE=Release \
    -DCODENIB_BUILD_PYBIND=ON \
    -DPython_EXECUTABLE="$(which python)"
  cmake --build build/core -j "$(nproc)"
  ```

- Runs the maintained native gate with an `LD_PRELOAD` of the system
  `libstdc++.so.6` (to match the GCC that compiled the pybind `.so`):

  ```bash
  make core-test
  ```

  This incrementally reuses the configured build, runs all C++ executables
  (including the content-digest vectors), verifies the required clangd decode,
  contract, and snapshot bindings are present, and executes the SCIP, Fact,
  clangd receipt/parity/fallback, and profiler-contract Python tests with
  `build/core` first on `PYTHONPATH`. The same `make core-test` command is the
  local reproduction.

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
`VERTEXAI_PROJECT`, selects `VERTEXAI_LOCATION` from the repository variable
with `us-east5` as the maintained-model fallback, installs the `test,vertex`
project extras, then:

```bash
pytest -m "slow" --tb=short
```

CUDA-specific tests should declare an explicit skip when the active `torch`
install is not CUDA-capable. This keeps slow LLM coverage available on CPU-only
runners while making GPU coverage opt-in to a runner/toolchain that actually
provides CUDA.

Locally, `make test` runs only the unit tier and cannot make billed model calls;
use `make test-slow` explicitly for this tier. CI requires a non-empty, valid
`GOOGLE_APPLICATION_CREDENTIALS_JSON` secret and exports its ADC path before
running provider tests. Missing credentials fail the selected slow job, while
an unconfigured local run skips provider-only cases before expensive fixtures.
The generated agent embedding index is reused while its resolved model revision
matches. A revision mismatch takes the builder's explicit `force_rebuild` path
once, so a legacy self-hosted-runner cache cannot keep the slow tier red.
Live agent routing remains intentionally non-deterministic: provider/index smoke
tests validate any BM25 call that occurs without rejecting the always-on
`read`/`grep`/`glob`/`bash` tools.

The checked-in pull-request workflows route executable PR content only to
ephemeral hosted runners. Drafts run hosted documentation, packaging, and
publish-smoke checks but defer the hosted unit gate until they become ready for
review; maintainers can apply `full-ci` only to override that draft deferral.
Integration, serial, graph, and credentialed tiers run from trusted
default-branch, scheduled, or manually dispatched workflow revisions.
Auto-labeling, CI preflight classification, distribution builds, and release
publication also run on ephemeral hosted runners.

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
- **Serial-chain path gating** — on a default-branch push, the serial chain
  (`integration-serial`, `scip-core`, `graph-consumer`) is skipped unless the
  change touches the serial-chain path allowlist. Scheduled runs and
  `workflow_dispatch` with `test_tier=full` always run it. See
  [preflight](#preflight-the-decision-job).
- **Slow tier gating** — `slow` is skipped on default-branch pushes. It runs
  for scheduled full CI or `workflow_dispatch` with `test_tier=full`.

### Explicit (sets `should-run=false`)

Full-CI `preflight` evaluates these and, when matched, sets
`should-run=false`, skipping **all** trusted test jobs:

- **Commit message** (push): contains `[skip tests]`.
- **Manual dispatch**: `workflow_dispatch` run with `skip_tests: true`.

The PR `unit` job is skipped when the title contains `[skip tests]`, the
`skip-tests` label is present, or the PR is still a draft without `full-ci`.

## Shared environment setup

`integration`, `integration-serial`, `scip-core`, `graph-consumer`, and `slow`
use the `./.github/actions/setup-env` composite action, which provisions:

- **conda** env `codenib-test` (Python 3.12 by default) plus a separate
  `scip-env` from `codenib/scip_interface/scip-environment.yml`.
- **Editable project extras** — `project-extras` defaults to `test`; jobs that
  exercise an optional provider must opt in explicitly (`slow` uses
  `test,vertex`). The action validates the comma-separated value before using
  it in the pip requirement.
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

!!! warning "Public-repository runner boundary"
    Hosted-only PR workflow definitions reduce accidental routing but are not a
    security boundary: a fork can propose a workflow diff that requests any
    repository-level runner label. A persistent runner for this public
    repository must therefore be registered at organization scope in a runner
    group restricted to
    `sysevol-ai/CodeNib/.github/workflows/ci-full.yml@refs/heads/main`, or be an
    isolated ephemeral runner that is destroyed after one job. Do not expose a
    reusable repository-level runner to pull-request workflows.

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
