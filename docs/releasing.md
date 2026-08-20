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
workflows. Pull requests build and inspect the complete distribution contract,
including the native ABI smoke. Pushes to `main`, version tags, and explicit
TestPyPI dispatches additionally run the heavier installed-service gates:

1. Tests and production-builds the packaged Wiki frontend.
2. Builds one source-only sdist and two `cp310-abi3-manylinux_2_28` wheels, one
   each for x86-64 and AArch64.
3. Compiles the C extension against CPython 3.10's limited API with
   `-Wall -Wextra -Werror`, then runs `twine check` and validates artifact
   contents, metadata, and tags.
4. Installs the sdist with compilation deliberately disabled, proving that the
   core remains usable and local workspace support fails closed.
5. Installs the native wheel on Python 3.10 through 3.14 and publishes, retains,
   and closes one real `LocalWorkspaceProvider` generation.
6. Builds a real BM25 index through the installed `codenib` command.
7. Installs the public 0.1.0 package, upgrades it to the candidate wheel,
   verifies that an incompatible BM25 view rebuilds once, and then verifies
   that the rebuilt view is reused.
8. Exercises the installed Wiki and MCP services end to end.
9. Runs sparse Ask through a local OpenAI-compatible endpoint, including a
   real BM25 tool call, final answer, and source citation, without installing
   semantic or graph extras.
10. Installs the graph extra and a pinned Python SCIP provider, then verifies
   repository-aware diagnostics, a real caller-to-callee graph edge, source
   anchors, and the installed Dependency Map API.

Manually dispatching the TestPyPI Release workflow from `main` runs these gates,
publishes to TestPyPI, then downloads that exact version from TestPyPI's public
simple index. The workflow selects the current host's compatible wheel and
byte-compares it with the corresponding verified build before installing it in
a fresh environment and exercising a real BM25 index build. Production
publishing remains in the separate Release workflow
and requires a `v<version>` tag that exactly matches `project.version`. Before
any upload, the production workflow proves MCP DNS ownership so a missing
record cannot leave a partial release. It then publishes to PyPI,
downloads and byte-compares the compatible public wheel, exercises its
installed Wiki and MCP services, publishes `ai.codenib/codenib` to the official
MCP Registry, and verifies Registry discovery. The final GitHub Release
contains both Linux wheels, the sdist, and `SHA256SUMS`; stable versions are
marked latest and PEP 440 prereleases remain prereleases. TestPyPI does not feed
the MCP Registry because the Registry accepts only official PyPI packages.

## Native Artifact Contract

The production artifact set is exact:

- `codenib-<version>-cp310-abi3-manylinux_2_28_x86_64.whl`
- `codenib-<version>-cp310-abi3-manylinux_2_28_aarch64.whl`
- `codenib-<version>.tar.gz`

Each wheel contains exactly one `_workspace_owner_impl.abi3.so`. The sdist
contains `native/workspace_owner.c` and no compiled extension. This lets
compiler-less, macOS, Windows, and other environments without a compatible
native extension install the Python core from source; the local workspace
provider then remains unavailable and fails before mutation. CodeNib does not
publish a prebuilt musllinux wheel, although a Linux source build can provide
the extension when a compatible C toolchain is available. Do not replace this
set with a universal wheel: its tag would falsely claim that the native
ownership boundary works on every platform.

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
maintainers. Both registries publish through OIDC; neither workflow accepts an
API-token password or reads a runner-local `.pypirc`. After the first successful
production OIDC publication, revoke any remaining bootstrap API token.

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
archive before exposing the environment secret. A tag preflight performs this
authentication before PyPI upload; the Registry job repeats it immediately
before publication. The DNS record belongs at the domain apex, not
`_mcp-auth.codenib.ai`.

A manual Release workflow dispatch from a branch performs release-artifact and
Registry-metadata verification but deliberately skips protected DNS
authentication, because the `mcp-registry-publish` environment admits only
`v*` tags. To repeat only the authenticated ownership preflight, dispatch the
workflow against a `v*` tag whose checked-in `release.yml` contains the manual
dispatch guard:

```bash
gh workflow run release.yml --ref v<version>
```

Inspect older tags before dispatching them. Production PyPI, MCP Registry, and
GitHub Release jobs must require both a `push` event and a `v*` tag; a manual
tag dispatch must never replay publication.

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
   `docs/releases/<version>.md` contains the curated public release notes, and
   the README citation and `mcp-name` marker remain present.
3. Run `pre-commit run --all-files` and the local package smoke.
4. Merge the release commit to `main` and confirm its package gates pass.
5. Confirm the TestPyPI trusted publisher is visible, then dispatch the
   TestPyPI Release workflow from `main`.
6. Confirm the TestPyPI registry-download and installed-CLI smoke job passes.
   Record its exact head SHA, freeze the release surface, and require the
   production tag to resolve to that same commit.
7. Complete the public-surface gate above.
8. Confirm the production PyPI publisher exactly names owner `sysevol-ai`,
   repository `CodeNib`, workflow `release.yml`, and environment `pypi`.
9. Confirm the `codenib.ai` MCP proof TXT record and protected
   `mcp-registry-publish` environment secret are active.
10. Create and push an annotated `v<version>` tag at the accepted TestPyPI SHA.
    Do not include intervening `main` changes without repeating the candidate
    workflow.
11. Confirm PyPI, MCP Registry discovery, and the generated GitHub Release all
    identify the same version and that a stable release is marked latest.
12. After the first successful production OIDC publication, revoke the
    bootstrap PyPI token from the GitHub environment and local configuration.

Do not reuse a published version. If publication partially succeeds, increment
the version and produce new artifacts.
