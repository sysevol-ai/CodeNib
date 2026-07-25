<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Releasing CodeNib

`pyproject.toml` is the only authored package-version source. Runtime
`codenib.__version__`, wheel metadata, and `codenib --version` read the
installed distribution metadata generated from it.

## Automated Gates

The Release workflow runs for packaging changes, `main`, manual dispatches, and
version tags. It:

1. Tests and production-builds the packaged Wiki frontend.
2. Builds one sdist and one universal wheel.
3. Runs `twine check` and validates package contents and metadata.
4. Installs the wheel on every supported Python minor version.
5. Builds a real BM25 index through the installed `codenib` command.

A manual run with target `testpypi` publishes only to TestPyPI. Production
publishing has no manual input: it requires a `v<version>` tag that exactly
matches `project.version`.

## Trusted Publisher Setup

Create PyPI and TestPyPI trusted publishers with:

| Field | Value |
|---|---|
| Owner | `sysevol-ai` |
| Repository | `CodeNib` |
| Workflow | `release.yml` |
| Environment | `pypi` or `testpypi` |

The corresponding GitHub environments should restrict deployments to trusted
maintainers. The workflow receives `id-token: write` only in publishing jobs;
no long-lived PyPI token is stored in GitHub.

## Release Checklist

1. Move relevant entries from `Unreleased` into a dated version section in
   `CHANGELOG.md`.
2. Confirm `CITATION.cff` and `pyproject.toml` use the release version.
3. Run `pre-commit run --all-files` and the local package smoke.
4. Dispatch the Release workflow with `testpypi`.
5. Install and test the TestPyPI artifact in a clean environment.
6. Merge the release commit to `main`.
7. Create and push an annotated `v<version>` tag.
8. Confirm the PyPI deployment and generated GitHub Release.

Do not reuse a published version. If publication partially succeeds, increment
the version and produce new artifacts.
