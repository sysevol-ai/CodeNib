<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Publish With GitHub Pages

CodeNib can build repository context in GitHub Actions, deploy a source-linked
static Wiki to GitHub Pages, and retain the matching context views as one
downloadable artifact. The default path builds BM25 and dense-vector views with
a cached local embedding model and needs no API key.

## Publish Hybrid Context

Create a caller workflow in the repository that should receive a Wiki:

```yaml
name: CodeNib Pages

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

jobs:
  publish:
    uses: sysevol-ai/CodeNib/.github/workflows/codenib-pages.yml@v0.2.0
```

The prerelease tag keeps the compiler, frontend, Action, and artifact schema on
one reviewed version. Production deployments may replace it with the tag's
resolved commit SHA. In the repository's **Settings > Pages**, select
**GitHub Actions** as the source.

The workflow checks out the caller's exact commit, incrementally builds the
`semantic` preset, exports the Wiki at the Pages-provided mount path, and
deploys it through the `github-pages` environment. It also uploads an artifact
named from the repository and commit. The workflow caches both repository
views and the pinned Hugging Face model. The static Wiki serves precomputed
pages, citations, and navigation without executing a query engine in the
browser; BM25 and vector views remain reusable through local or MCP serving.

## Use The No-Model Fallback

Select `fast` when cold-start time or avoiding a model download matters more
than natural-language retrieval quality:

```yaml
jobs:
  publish:
    uses: sysevol-ai/CodeNib/.github/workflows/codenib-pages.yml@v0.2.0
    with:
      preset: fast
```

This builds only the deterministic BM25 artifact and downloads no model. It is
an explicit compatibility and resource-constrained route rather than the
recommended retrieval default.

## Bring Your Own Embedding Endpoint

An OpenAI-compatible endpoint can replace the local model without changing the
artifact or Pages workflow:

```yaml
jobs:
  publish:
    uses: sysevol-ai/CodeNib/.github/workflows/codenib-pages.yml@v0.2.0
    with:
      preset: semantic
      embedding-provider: openai
      embedding-model: text-embedding-3-small
      embedding-dimension: "1536"
      embedding-endpoint: https://embeddings.example.com/v1
    secrets:
      embedding_api_key: ${{ secrets.CODENIB_EMBEDDING_API_KEY }}
```

Create `CODENIB_EMBEDDING_API_KEY` under **Settings > Secrets and variables >
Actions > New repository secret**. The caller maps that repository secret to
the reusable workflow's `embedding_api_key`; the workflow never places its
value in the cache key, artifact, manifest, or Pages output.

Provider, model, vector dimension, endpoint, Python version, and CodeNib source
revision participate in cache compatibility. The credential value does not.
Endpoints containing user information, a query, or a fragment are rejected.

## What Gets Published

The Pages artifact is a serverless inspection surface. It contains generated
pages, source slices used by citations, page-level dependency data when
available, and `codenib-static.json`. It does not contain an API endpoint,
credential, interactive Ask backend, or unrestricted source-reading service.

The separate context artifact contains:

- `codenib-context.json`, with repository, commit, schema, capabilities, and
  file hashes;
- an artifact-relative `repo_manifest.json`;
- the BM25 view and, for `semantic`, FAISS indexes plus repository-relative
  document locations.

Mutable vector maintenance caches are deliberately excluded. The downloadable
artifact represents query-serving state for one commit; it is not a substitute
for the Action cache used to update a later commit. Portable publication
currently supports the `fast` and `semantic` presets. Graph and Zoekt indexes
remain available in the local/MCP runtime but are not yet promised as portable
Pages artifacts.

## Incremental Builds

The Action caches `~/.codenib/repositories` under a key that includes the
repository, platform, Python version, profile, provider identity, and CodeNib
revision. A prefix restore may supply the previous commit's state, but it never
declares that state current. The compiler compares the checkout and manifest,
updates supported views, and rebuilds when reuse is not valid. The newly
uploaded context artifact always records the indexed checkout's resolved Git
commit rather than assuming that it matches the surrounding event SHA.

## Security Boundary

The reusable workflow rejects `pull_request_target` and skips pull requests
whose head repository differs from the base repository. It therefore does not
pass BYO credentials to untrusted fork code. All shipped
third-party Actions are pinned to immutable commits, checkout credentials are
not persisted, and publication fails if an output contains a configured secret,
a symbolic link, or a build-machine source/index path.

Use `push` or `workflow_dispatch` for normal publication. Do not wrap the
reusable workflow in `pull_request_target`.

## Build Without Deployment

The composite Action can be used directly when another static host or artifact
store owns deployment:

```yaml
- uses: sysevol-ai/CodeNib/.github/actions/publish@v0.2.0
  id: codenib
  with:
    preset: fast
    base-path: /repository
```

Its outputs include `site-path`, `context-path`, `context-manifest`,
`artifact-name`, `cache-hit`, `cache-key`, and `source-commit`.

## Reuse the Artifact Through MCP

The uploaded context artifact can serve an exact local checkout without
rebuilding its BM25 or vector views. Check out the commit first, then fetch the
artifact with a token that has **Actions: read** permission:

```bash
git -C /path/to/repository checkout <full-commit>
export GH_TOKEN="$(gh auth token)"

codenib artifact fetch owner/repository \
  --repo /path/to/repository \
  --commit <full-commit>
```

CodeNib resolves the newest non-expired artifact whose workflow
`head_sha` exactly matches the commit. It checks GitHub's archive digest,
extracts with file-count, expanded-size, symlink, and traversal limits, verifies
every inventoried file, and compares the local checkout's commit and source
fingerprint before an index loader runs. The downloaded artifact uses JSON for
portable vector documents; CodeNib never loads a pickle from this path.

These checks establish artifact integrity and source compatibility, not trust in
an arbitrary workflow publisher. Fetch only artifacts produced by a workflow
and pinned CodeNib revision that you trust. Semantic artifacts also contain a
provider and endpoint identity; review that identity before exposing model
credentials to the MCP process.

The command prints the verified cache directory. Start MCP directly:

```bash
codenib mcp \
  --artifact ~/.codenib/artifacts/owner/repository/<full-commit> \
  --repo /path/to/repository \
  --repository owner/repository
```

Or generate a reviewable client configuration command:

```bash
codenib artifact mcp-config \
  ~/.codenib/artifacts/owner/repository/<full-commit> \
  --repo /path/to/repository \
  --repository owner/repository \
  --host codex
```

`--host claude` emits the corresponding `claude mcp add-json` command;
`--host json` emits a project-scoped `.mcp.json` document. Review the output
before running or placing it. CodeNib does not edit a client configuration
automatically.

Local semantic and BM25 serving require no model credential. A semantic
artifact reuses its stored vectors but still needs the manifest-selected
embedding provider for each query embedding. Provider credentials stay in the
MCP process environment and are never copied into client configuration or the
context artifact. When GitHub CLI is unavailable, set `GH_TOKEN` to a
fine-grained token with **Actions: read** access to the repository.
