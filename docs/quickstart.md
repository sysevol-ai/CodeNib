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

The default command updates an existing manifest when the repository changes.
To launch without checking or updating it:

```bash
codenib wiki /path/to/repository --no-index
```

Force a clean rebuild with:

```bash
codenib wiki /path/to/repository --rebuild
```

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

The semantic preset downloads its embedding model on first use. The `graph`
extra supplies the Python graph and protobuf runtimes, while each repository
language still needs its own SCIP/LSP executable. Check the exact repository
instead of testing for an unrelated tool:

```bash
codenib doctor /path/to/repository --require graph
```

The full preset also needs Zoekt binaries. Follow
[SCIP Indexing](scip_index.md) and check the
[Language Capabilities](language_capabilities.md) matrix for backend setup.

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
  --api-base http://127.0.0.1:8000/v1 \
  --api-key-env LOCAL_LLM_KEY
```

Provider-native LiteLLM routes use their normal model prefix and credentials:

```bash
export ANTHROPIC_API_KEY=...
codenib wiki . --generate --model anthropic/claude-sonnet-4-5

gcloud auth application-default login
codenib wiki . --generate \
  --model vertex_ai/gemini-2.5-flash \
  --model-option vertex_project=my-project \
  --model-option vertex_location=us-central1
```

Repeat `--model-option KEY=VALUE` for provider-specific LiteLLM parameters.
Values are JSON-decoded and dotted keys create nested payloads:

```bash
codenib wiki . --generate \
  --model openai/qwen3 \
  --api-base http://127.0.0.1:8000/v1 \
  --model-option extra_body.chat_template_kwargs.enable_thinking=false
```

CodeNib manages `model`, credentials, token budgets, messages, and tools;
those fields cannot be overridden through `--model-option`. Keep secrets in
provider environment variables or `--api-key-env`, not option values. Run the
same model arguments through `codenib doctor --probe-model` before generating
pages.

Provider and model configuration is documented in
[Web UI](web_demo.md). Search, source links, and deterministic pages remain
available without this extra.

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
- Check that ports 3000 and 8000 are free, or select alternatives.
- Pass `--language` when a repository contains no detectable supported source
  extension.
