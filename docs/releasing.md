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
workflows. Pull requests build and inspect the distribution once. Pushes to
`main`, version tags, and explicit TestPyPI dispatches run the complete gate:

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

Manually dispatching the TestPyPI Release workflow from `main` runs these gates,
publishes to TestPyPI, then downloads that exact version from TestPyPI's public
simple index. The workflow byte-compares the downloaded wheel with the verified
build before installing it in a fresh environment and exercising a real BM25
index build. Production publishing remains in the separate Release workflow
and requires a `v<version>` tag that exactly matches `project.version`. After
PyPI publication, the production workflow waits for that exact package and its
MCP ownership marker, publishes `ai.codenib/codenib` to the official MCP
Registry, verifies Registry discovery, and then creates the prerelease GitHub
Release containing the distributions and `SHA256SUMS`. TestPyPI does not feed
the MCP Registry because the Registry accepts only official PyPI packages.

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
maintainers. TestPyPI currently publishes through OIDC. Production PyPI uses
the environment-scoped `PYPI_API_TOKEN` fallback added after the `v0.1.0`
bootstrap; [issue #384](https://github.com/sysevol-ai/CodeNib/issues/384)
tracks restoring its trusted publisher and revoking that token. No publishing
workflow reads a runner-local `.pypirc`.

## MCP Registry Namespace

The production tag publishes the branded `ai.codenib/codenib` namespace from
`server.json`. The package README carries the matching hidden `mcp-name` marker,
and release verification keeps both Registry versions plus the fixed `uvx`
extra synchronized with `project.version`.

DNS authentication requires one Ed25519 proof record at the `codenib.ai` apex.
Generate the key once on a trusted machine with OpenSSL 3, add the printed TXT
record through the DNS provider, and store only the extracted private scalar in
the protected GitHub environment:

```bash
openssl genpkey -algorithm Ed25519 -out key.pem
PUBLIC_KEY="$(openssl pkey -in key.pem -pubout -outform DER | tail -c 32 | base64)"
echo "codenib.ai. IN TXT \"v=MCPv1; k=ed25519; p=${PUBLIC_KEY}\""
PRIVATE_KEY="$(openssl pkey -in key.pem -noout -text | grep -A3 'priv:' | tail -n +2 | tr -d ' :\n')"
printf '%s' "$PRIVATE_KEY" | gh secret set MCP_PRIVATE_KEY \
  --repo sysevol-ai/CodeNib --env mcp-registry-publish
```

Do not commit `key.pem`. Configure `mcp-registry-publish` to allow only release
tags and require a maintainer approval. The publish job deliberately runs on
`ubuntu-latest`, not a self-hosted runner, and verifies the pinned publisher
archive before exposing the environment secret. The DNS record belongs at the
domain apex, not `_mcp-auth.codenib.ai`.

## Public Surface Gate

Before the production tag, make the repository public and manually dispatch
the Docs workflow from `main`. Confirm that the documentation site and the two
README assets are anonymously reachable:

```text
https://docs.codenib.ai/
https://raw.githubusercontent.com/sysevol-ai/CodeNib/main/assets/codenib_logo.svg
https://raw.githubusercontent.com/sysevol-ai/CodeNib/main/assets/codenib_wiki.png
```

The README uses absolute asset URLs because it is also the PyPI project
description; repository-relative images have no repository base when rendered
on PyPI.

## Release Checklist

1. Move relevant entries from `Unreleased` into a dated version section in
   `CHANGELOG.md`.
2. Confirm `pyproject.toml` uses the release version, `server.json` matches it,
   and the README citation and `mcp-name` marker remain present.
3. Run `pre-commit run --all-files` and the local package smoke.
4. Merge the release commit to `main` and confirm its package gates pass.
5. Confirm the TestPyPI pending publisher is visible, then dispatch the
   TestPyPI Release workflow from `main`.
6. Confirm the TestPyPI registry-download and installed-CLI smoke job passes.
7. Complete the public-surface gate above.
8. Confirm the production `pypi` environment still has its scoped publisher
   credential, or complete #384 and update the workflow to OIDC first.
9. Confirm the `codenib.ai` MCP proof TXT record and protected
   `mcp-registry-publish` environment secret are active.
10. Create and push an annotated `v<version>` tag.
11. Confirm PyPI, MCP Registry discovery, and the generated prerelease GitHub
    Release all identify the same version.

Do not reuse a published version. If publication partially succeeds, increment
the version and produce new artifacts.
