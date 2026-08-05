<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Quickstart

This guide turns a local repository into a source-linked CodeNib Wiki. The
default path uses deterministic page generation and BM25 search, so it needs
neither an API key nor a model download.

## Prerequisites

- Python 3.10 or newer
- Git

Install CodeNib and verify the local runtime:

```bash
pip install codenib
codenib doctor --require core --require wiki
```

## Launch A Repository Wiki

```bash
codenib wiki /path/to/repository
```

CodeNib performs four steps:

1. Detects supported source languages.
2. Builds or updates repository views under
   `~/.codenib/repositories/<repo>-<id>/indexes`.
3. Registers the repository with a local FastAPI service.
4. Serves the packaged production frontend and opens
   [http://localhost:3000](http://localhost:3000).

The release wheel contains the compiled frontend, so this path does not need
Node.js, npm, or a source checkout. CodeNib-owned indexes and manifests stay
outside the target repository; set `CODENIB_HOME` to relocate that state. The
`fast` and `semantic` presets leave the checkout unchanged. Some language-aware
graph backends must invoke the repository's build or package manager and may
prepare project-local dependencies such as `node_modules`; run those profiles
from a clean checkout when that distinction matters. Press `Ctrl-C` once to
stop both services.

Use different ports or keep the browser closed when needed:

```bash
codenib wiki . --port 3100 --api-port 8100 --no-open
```

## Reuse An Existing Index

The default command compares the repository with its existing manifest and
updates changed views. To reuse an already-built manifest without performing
that index update:

```bash
codenib wiki /path/to/repository --no-index
```

`--no-index` requires an existing `repo_manifest.json`. It skips rebuilding or
updating views, but still validates that the current checkout matches the
manifest's recorded source identity and commit; CodeNib refuses to launch on a
mismatch.

Force a clean rebuild with:

```bash
codenib wiki /path/to/repository --rebuild
```

## Export A Static Wiki

An existing manifest can be frozen into a serverless directory for GitHub
Pages or another static host:

```bash
codenib index /path/to/repository
codenib export /path/to/repository --output /tmp/repository-wiki
```

The export records the repository commit, source fingerprint, view
capabilities, source-location convention, generation mode, and content hashes
in `codenib-static.json`. It also embeds source slices used by page citations,
so reading a page does not require the local FastAPI process. CodeNib rejects
an export when the checkout no longer matches the manifest.

For a GitHub project Pages site, set its mount path to the repository name:

```bash
codenib export . \
  --output /tmp/my-project \
  --base-path /my-project
```

The output must be outside the target checkout. CodeNib can replace a prior
CodeNib export, but it refuses to delete an unrelated non-empty directory.

Static mode publishes only capabilities with a serverless implementation:
Wiki navigation, embedded citations, and precomputed page dependency views.
Interactive Ask, on-demand edge labels, and arbitrary dependency queries need
an authenticated CodeNib runtime and are not exposed by the export. No API key,
GitHub token, endpoint credential, or build-machine absolute path is serialized.

To build the index and both distribution surfaces in one command:

```bash
codenib publish . \
  --preset fast \
  --site-output /tmp/repository-wiki \
  --context-output /tmp/repository-context \
  --base-path /repository
```

The context directory is commit-addressed query-serving state with an
artifact-relative manifest and file hashes. It excludes mutable maintenance
caches. Portable publication currently supports `fast` and `semantic`; use the
local or MCP runtime for graph and Zoekt views. See
[Publish With GitHub Pages](github_pages.md) for the no-model Action, local
embedding model, and BYO endpoint configurations.

## Select Repository Views

| Preset | Required package | Views |
|---|---|---|
| `fast` | `codenib` | BM25 |
| `semantic` | `codenib[semantic]` | BM25 and dense vectors |
| `graph` | `codenib[graph]` | BM25 and symbol graph |
| `full` | `codenib[full]` | BM25, dense vectors, symbol graph, and Zoekt |

For natural-language search:

```bash
pip install "codenib[semantic]"
codenib wiki /path/to/repository --preset semantic
```

The semantic preset downloads CodeRankEmbed on first use. CodeNib pins the
built-in model to an immutable revision and enables remote model code only for
that revision; caller-supplied models or revisions are not trusted implicitly.
To keep embeddings out of the local process, use a BYO OpenAI-compatible
embedding service:

```bash
pip install "codenib[semantic-remote]"
export EMBEDDING_API_KEY=...
codenib doctor --require semantic \
  --embedding-provider openai \
  --embedding-endpoint https://inference.example.com/v1 \
  --embedding-api-key-env EMBEDDING_API_KEY \
  --probe-embedding
codenib wiki . --preset semantic \
  --embedding-provider openai \
  --embedding-endpoint https://inference.example.com/v1 \
  --embedding-api-key-env EMBEDDING_API_KEY
```

The remote default is `text-embedding-3-small` with dimension 1536. Select
another model with `--embedding-model` and declare its vector width with
`--embedding-dimension`; omit `--embedding-api-key-env` only when the endpoint
is intentionally unauthenticated.
Provider, model, endpoint, dimension, and vector-shaping options become part of
the vector artifact identity. Credentials, retries, timeouts, and batching stay
process-local, and CodeNib refuses to reopen an artifact through a different
provider or endpoint.

The `graph` extra supplies the Python graph and protobuf runtimes, while each
repository language still needs its own SCIP/LSP executable. Check the exact
repository instead of testing for an unrelated tool:

```bash
codenib doctor /path/to/repository --require graph
```

The full preset also needs `zoekt-git-index` and `zoekt-webserver` on `PATH`.
`codenib doctor --require graph` checks language-specific graph providers, not
Zoekt. Follow [SCIP Indexing](scip_index.md), check the
[Language Capabilities](language_capabilities.md) matrix, and verify the Zoekt
commands separately:

```bash
command -v zoekt-git-index
command -v zoekt-webserver
```

Override individual views or language detection:

```bash
codenib index . --view bm25 --view vector
codenib index . --language python --language typescript
```

Both options may also use comma-separated values.

## Enable Agent-Authored Pages

Static Wiki pages are the default. To generate conceptual page narratives
through a LiteLLM-supported provider:

```bash
pip install "codenib[agent]"
export OPENAI_API_KEY=...
codenib doctor --require agent \
  --model openai/gpt-4o-mini --api-key-env OPENAI_API_KEY --probe-model
codenib wiki . --generate --model openai/gpt-4o-mini
```

For an OpenAI-compatible local or hosted endpoint:

```bash
export LOCAL_LLM_KEY=...
codenib wiki . --generate \
  --model openai/local-model \
  --api-base http://127.0.0.1:8080/v1 \
  --api-key-env LOCAL_LLM_KEY
```

CodeNib passes BYO credentials only to the running client. They are never
written to `repo_manifest.json`, vector configuration, Wiki caches, or a static
Pages export.

Provider-native LiteLLM routes use their normal model prefix and credentials:

```bash
export ANTHROPIC_API_KEY=...
codenib wiki . --generate --model anthropic/claude-sonnet-4-5
```

Vertex AI additionally requires CodeNib's `vertex` extra:

```bash
pip install "codenib[agent,vertex]"
gcloud auth application-default login
codenib wiki . --generate \
  --model vertex_ai/gemini-2.5-flash \
  --model-option vertex_project=my-project \
  --model-option vertex_location=us-central1
```

Choose the route that matches the server actually receiving the request:

| Backend | `--model` shape | Endpoint and authentication |
| --- | --- | --- |
| OpenAI | `openai/<model>` | `OPENAI_API_KEY` |
| Anthropic | `anthropic/<model>` | `ANTHROPIC_API_KEY` |
| OpenAI-compatible gateway or vLLM | `openai/<served-model>` | `--api-base .../v1`; add `--api-key-env` only when the gateway requires it |
| Ollama | `ollama/<model>` | `--api-base http://localhost:11434` |
| Azure OpenAI | `azure/<deployment>` | API base and key, plus `--model-option api_version=...` |
| Vertex AI | `vertex_ai/<model>` | `vertex_project`, `vertex_location`, and Google Application Default Credentials |

The provider prefix is required even when `--api-base` points to a custom
gateway. For example, use `openai/qwen3`, not bare `qwen3`, for an
OpenAI-compatible Qwen endpoint.

Repeat `--model-option KEY=VALUE` for provider-specific LiteLLM parameters.
Values are JSON-decoded and dotted keys create nested payloads. This flag is
available on `codenib wiki` and `codenib doctor`; configure the standalone
`codenib-web` service through YAML or `CODENIB_DEMO_*` environment variables.

```bash
codenib wiki . --generate \
  --model openai/qwen3 \
  --api-base http://127.0.0.1:8080/v1 \
  --model-option extra_body.chat_template_kwargs.enable_thinking=false
```

CodeNib manages `model`, credentials, token budgets, messages, and tools;
those fields cannot be overridden through `--model-option`. Keep secrets in
provider environment variables or `--api-key-env`, not option values. Run the
same model arguments through `codenib doctor --probe-model` before generating
pages. The static doctor validates provider routing, endpoint shape, explicit
options, and known environment requirements. The probe then sends three tiny
requests to verify plain completion, tool calling for Ask, and structured output
for generated Wiki pages.

Provider and model configuration is documented in
[Web UI](web_demo.md) and the
[LiteLLM provider documentation](https://docs.litellm.ai/docs/providers).
Search, source links, and deterministic pages remain available without this
extra. Ask is model-backed: if its configured provider cannot authenticate or
cannot be reached, the question request fails while the rest of the Wiki
continues to work.

## Serve The Index Over MCP

```bash
pip install "codenib[mcp]"
codenib index /path/to/repository
codenib mcp /path/to/repository
```

`codenib mcp` accepts either a repository directory or the generated
`repo_manifest.json`. It uses stdio transport, so configure it as a local
process in an MCP-capable client. See [MCP Server](mcp.md) for an example.

## Troubleshooting

Run the capability report first:

```bash
codenib doctor
codenib doctor . --require semantic --require graph
```

Common fixes:

- Install the named extra when a command reports a missing optional module.
- Use `--rebuild` after intentionally changing index profiles or builders.
- Check that frontend port 3000 and API port 8000 are free, or select
  alternatives. Run a local OpenAI-compatible model on a separate port such as
  8080.
- Pass `--language` when a repository contains no detectable supported source
  extension.
