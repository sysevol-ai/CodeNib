<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Publish With GitHub Pages

CodeNib can build repository context in GitHub Actions, deploy a source-linked
static Wiki to GitHub Pages, and retain the matching context views as one
downloadable artifact. The default path uses BM25 and needs no model, API key,
or model download.

## No-Model Starter

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
    uses: sysevol-ai/CodeNib/.github/workflows/codenib-pages.yml@<release-sha>
```

Replace `<release-sha>` with a published CodeNib release commit. A commit SHA
keeps the compiler, frontend, action, and artifact schema on one reviewed
revision. In the repository's **Settings > Pages**, select **GitHub Actions** as
the source.

The workflow checks out the caller's exact commit, incrementally builds the
`fast` preset, exports the Wiki at the Pages-provided mount path, and deploys it
through the `github-pages` environment. It also uploads an artifact named from
the repository and commit. BM25 belongs to that context artifact; the static
Wiki serves precomputed pages, citations, and navigation without executing a
query engine in the browser.

## Build Semantic Context With A Local Model

Select the semantic preset to build BM25 and dense-vector views. It defaults to
a local Hugging Face embedding model and needs no API credential:

```yaml
jobs:
  publish:
    uses: sysevol-ai/CodeNib/.github/workflows/codenib-pages.yml@<release-sha>
    with:
      preset: semantic
```

The first build downloads the default embedding model into the ephemeral Action
runner. The resulting vector view is stored in the commit-addressed context
artifact and reused through CodeNib's repository cache on later builds. Serve
that artifact through the local or MCP runtime for semantic queries; the Pages
site remains a precomputed inspection surface. Keep the default `fast` preset
when a model download is undesirable.

## Bring Your Own Embedding Endpoint

An OpenAI-compatible endpoint can replace the local model without changing the
artifact or Pages workflow:

```yaml
jobs:
  publish:
    uses: sysevol-ai/CodeNib/.github/workflows/codenib-pages.yml@<release-sha>
    with:
      preset: semantic
      embedding-provider: openai
      embedding-model: text-embedding-3-small
      embedding-dimension: "1536"
      embedding-endpoint: https://embeddings.example.com/v1
    secrets:
      embedding_api_key: ${{ secrets.CODENIB_EMBEDDING_API_KEY }}
```

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
- uses: sysevol-ai/CodeNib/.github/actions/publish@<release-sha>
  id: codenib
  with:
    preset: fast
    base-path: /repository
```

Its outputs include `site-path`, `context-path`, `context-manifest`,
`artifact-name`, `cache-hit`, `cache-key`, and `source-commit`.
