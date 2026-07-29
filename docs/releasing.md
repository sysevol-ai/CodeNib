<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Releasing CodeNib

`pyproject.toml` is the only authored package-version source. Runtime
`codenib.__version__`, wheel metadata, and `codenib --version` read the
installed distribution metadata generated from it.

## Automated Gates

The reusable Release verification workflow runs from both publishing
workflows. It:

1. Tests and production-builds the packaged Wiki frontend.
2. Builds one sdist and one universal wheel.
3. Runs `twine check` and validates package contents and metadata.
4. Installs the wheel on every supported Python minor version.
5. Builds a real BM25 index through the installed `codenib` command.
6. Exercises the installed Wiki and MCP services end to end.
7. Runs sparse Ask through a local OpenAI-compatible endpoint, including a
   real BM25 tool call, final answer, and source citation, without installing
   semantic or graph extras.
8. Installs the graph extra and a pinned Python SCIP provider, then verifies
   repository-aware diagnostics, a real caller-to-callee graph edge, source
   anchors, and the installed Dependency Map API.

Manually dispatching the TestPyPI Release workflow from `main` runs these gates
and then publishes to TestPyPI. Production publishing remains in the separate
Release workflow and requires a `v<version>` tag that exactly matches
`project.version`.

## Trusted Publisher Setup

For the first release, create a pending publisher on both the
[PyPI](https://pypi.org/manage/account/publishing/) and
[TestPyPI](https://test.pypi.org/manage/account/publishing/) account pages.
Register these publisher identities:

| Registry | Project | Owner | Repository | Workflow | Environment |
|---|---|---|---|---|---|
| TestPyPI | `codenib` | `sysevol-ai` | `CodeNib` | `release-test.yml` | `testpypi` |
| PyPI | `codenib` | `sysevol-ai` | `CodeNib` | `release.yml` | `pypi` |

These values must match exactly; an unregistered or mismatched publisher fails
the OIDC exchange with `invalid-publisher`. Do not register the reusable
`release-verify.yml` workflow as a publisher: it never receives an OIDC token
or uploads a distribution.

PyPI and TestPyPI are separate registries: configuring one does not configure
the other. A GitHub `pypi` or `testpypi` deployment environment scopes the
workflow job but does not register a publisher with either registry. Confirm
that each pending publisher appears on the corresponding registry account page
before dispatching a publish workflow.

The corresponding GitHub environments should restrict deployments to trusted
maintainers. Each publishing workflow receives `id-token: write` only in its
publishing job; no long-lived PyPI token or runner-local `.pypirc` is used.

## Public Surface Gate

Before the production tag, make the repository public and manually dispatch
the Docs workflow from `main`. Confirm that the documentation site and the two
README assets are anonymously reachable:

```text
https://sysevol-ai.github.io/CodeNib/
https://raw.githubusercontent.com/sysevol-ai/CodeNib/main/assets/codenib_logo.svg
https://raw.githubusercontent.com/sysevol-ai/CodeNib/main/assets/codenib_wiki.png
```

The README uses absolute asset URLs because it is also the PyPI project
description; repository-relative images have no repository base when rendered
on PyPI.

## Release Checklist

1. Move relevant entries from `Unreleased` into a dated version section in
   `CHANGELOG.md`.
2. Confirm `pyproject.toml` uses the release version and the README `Citation`
   section still matches the current arXiv entry.
3. Run `pre-commit run --all-files` and the local package smoke.
4. Merge the release commit to `main` and confirm its package gates pass.
5. Confirm the TestPyPI pending publisher is visible, then dispatch the
   TestPyPI Release workflow from `main`.
6. Install and test the TestPyPI artifact in a clean environment.
7. Complete the public-surface gate above.
8. Confirm the PyPI pending publisher is visible.
9. Create and push an annotated `v<version>` tag.
10. Confirm the PyPI deployment and generated GitHub Release.

Do not reuse a published version. If publication partially succeeds, increment
the version and produce new artifacts.
